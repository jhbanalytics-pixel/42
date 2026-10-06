"""Joined source pipeline through synthetic SDK transport and the real validators."""

import copy
import hashlib
import json
import socket
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery
from src.analysis.open_intelligence.general_question_calls import GeneralQuestionCalls
from src.analysis.open_intelligence.general_question_plan import validate_question_plan
from src.analysis.open_intelligence.general_question_planning import (
    build_question_planning_request,
)
from src.analysis.open_intelligence.general_question_queries import GeneralQuestionQueries

from tests.unit import test_general_question_copy_schema as copy_schema_fixture
from tests.unit import test_general_question_copy_validation as copy_fixture
from tests.unit import test_general_question_release_admission as release_fixture
from tests.unit import test_general_question_retrieval_queries as discovery_fixture
from tests.unit import test_general_question_runtime as runtime_fixture
from tests.unit import test_general_question_schema as source_schema_fixture
from tests.unit import test_general_question_source_query as source_fixture
from tests.unit import test_general_question_store as store_fixture
from tests.unit import test_open_intelligence_release as legacy_release_fixture


def _json(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value))


def _source_window():
    from scripts.staging import copy_open_intelligence_replay_sources as original

    completeness = sorted(
        (table.name, table.source_rows, table.source_rows)
        for table in original._table_contracts().values()
    )
    digest = hashlib.sha256(
        json.dumps(
            {"copy_run_id": original.COPY_RUN_ID, "completeness": completeness},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    manifests = [
        {
            "copy_run_id": original.COPY_RUN_ID,
            "target_table": table.name,
            "window_start": original.START_DATE,
            "window_end": original.END_DATE,
            "manifest_rows": table.source_rows,
        }
        for table in original._table_contracts().values()
    ]
    receipts = [
        {
            "copy_run_id": original.COPY_RUN_ID,
            "source_table": table.name,
            "window_start": original.START_DATE,
            "window_end": original.END_DATE,
            "filter_digest": table.filter_digest,
            "schema_digest": table.schema_digest,
            "source_set_digest": table.source_set_digest,
            "source_rows": table.source_rows,
        }
        for table in original._table_contracts().values()
    ]
    return digest, manifests, receipts


def _release_inputs(monkeypatch):
    from src.analysis.open_intelligence.persistence import (
        OpenIntelligenceRowBatch,
        build_run_receipt_row,
    )

    digest, manifests, copy_receipts = _source_window()
    original = legacy_release_fixture._receipt_fields
    monkeypatch.setattr(
        legacy_release_fixture,
        "_receipt_fields",
        lambda: {**original(), "source_window_digest": digest},
    )
    original_source_inputs = release_fixture.source_inputs

    def coherent_source_inputs(version="hybrid_graph_v1"):
        receipt, _run_rows, batch = original_source_inputs(version)

        def coherent(rows):
            output = []
            for row in rows:
                value = {
                    **row,
                    "client_scope_id": receipt.client_scope_id,
                    "run_id": receipt.run_id,
                    "contract_version": "2.0.0",
                }
                if "market_scope" in value:
                    value["market_scope"] = list(receipt.market_scope)
                if "audience_lens_ids" in value:
                    value["audience_lens_ids"] = []
                output.append(value)
            return tuple(output)

        batch = OpenIntelligenceRowBatch(
            coherent(batch.candidates),
            coherent(batch.evidence),
            coherent(batch.membership),
            coherent(batch.lineage),
            coherent(batch.predictions),
            coherent(batch.outcomes),
            cluster_build_version=batch.cluster_build_version,
        )
        receipt = build_run_receipt_row(
            batch,
            (),
            run_id=receipt.run_id,
            client_scope_id=receipt.client_scope_id,
            market_scope=receipt.market_scope,
            signal_date=receipt.signal_date,
            observation_start=receipt.observation_start,
            observation_end=receipt.observation_end,
            observation_method=receipt.observation_method,
            source_window_digest=digest,
            cluster_build_version=receipt.cluster_build_version,
            source_family_map_version=receipt.source_family_map_version,
            rule_version=receipt.rule_version,
            status=receipt.status,
            complete_partitions=receipt.complete_partitions,
            source_sha=receipt.source_sha,
            completed_at=receipt.completed_at,
        )
        receipt = replace(receipt, display_release_state="enabled")
        run_rows = {
            "signal_candidates_v2": batch.candidates,
            "signal_evidence_v2": batch.evidence,
            "signal_membership_v2": batch.membership,
            "signal_lineage_v2": batch.lineage,
            "signal_analysis_v2": (),
            "signal_predictions_v2": batch.predictions,
            "signal_outcomes_v2": batch.outcomes,
        }
        return receipt, run_rows, batch

    monkeypatch.setattr(release_fixture, "source_inputs", coherent_source_inputs)
    _request, _plan, _intake, receipt, run_rows, release_material = release_fixture.inputs()
    for row in (*manifests, *copy_receipts):
        row["window_start"] = receipt.observation_start
        row["window_end"] = receipt.observation_end
    return receipt, run_rows, release_material, manifests, copy_receipts


def _wire_rows(request, receipt, run_rows, manifests, copy_receipts):
    metadata = {
        "receipt_count": 1,
        "receipt_json": json.dumps(asdict(receipt), default=_json),
        "row_set_digest": receipt.row_set_digest,
        "family_counts": {
            family: len(run_rows[relation])
            for family, relation in source_fixture.module().FAMILY_RELATIONS.items()
        },
        "manifest_count": len(manifests),
        "copy_receipt_count": len(copy_receipts),
        "manifest": manifests,
        "copy_receipts": copy_receipts,
    }
    rows = [source_fixture.row("run_metadata", metadata, run_id=receipt.run_id)]
    for family, relation in source_fixture.module().FAMILY_RELATIONS.items():
        for value in run_rows[relation]:
            rows.append(
                source_fixture.row(
                    family,
                    value,
                    run_id=value["run_id"],
                    signal_id=value.get("signal_id"),
                    market=value["market"],
                )
            )
    content_count = len(rows) - 1
    rows.append(
        source_fixture.row(
            "request_metadata",
            {
                "request_digest": request["request_digest"],
                "candidate_count": content_count,
                "available_content_rows": content_count,
                "overflow_count": 0,
                "evidence_count": len(run_rows["signal_evidence_v2"]),
                "available_evidence_rows": len(run_rows["signal_evidence_v2"]),
                "schema_rows": [],
                "schema_row_count": 0,
            },
        )
    )
    return rows


def _field(name, value):
    if isinstance(value, (list, tuple)):
        sample = value[0] if value else ""
        child = _field(name, sample)
        return bigquery.SchemaField(
            name,
            child.field_type,
            mode="REPEATED",
            fields=child.fields,
        )
    if type(value) is dict:
        return bigquery.SchemaField(
            name,
            "RECORD",
            fields=tuple(_field(key, item) for key, item in value.items()),
        )
    if type(value) is bool:
        return bigquery.SchemaField(name, "BOOL")
    if type(value) is int:
        return bigquery.SchemaField(name, "INT64")
    if isinstance(value, datetime):
        return bigquery.SchemaField(name, "TIMESTAMP")
    if isinstance(value, date):
        return bigquery.SchemaField(name, "DATE")
    return bigquery.SchemaField(name, "STRING")


def _cell(field, value):
    def scalar(schema, item):
        if item is None:
            return None
        if schema.field_type == "RECORD":
            return {"f": [_cell(child, item[child.name]) for child in schema.fields]}
        if schema.field_type == "TIMESTAMP":
            return str(int(item.timestamp() * 1_000_000))
        if schema.field_type == "DATE":
            return item.isoformat()
        if schema.field_type == "BOOL":
            return "true" if item else "false"
        if schema.field_type == "INT64":
            return str(item)
        return item

    if field.mode == "REPEATED":
        item_field = bigquery.SchemaField(
            field.name,
            field.field_type,
            fields=field.fields,
        )
        return {"v": [{"v": scalar(item_field, item)} for item in value]}
    return {"v": scalar(field, value)}


def _query_result(resource, rows):
    fields = [_field(name, value) for name, value in rows[0].items()]
    return {
        "jobReference": resource["jobReference"],
        "jobComplete": True,
        "totalRows": str(len(rows)),
        "schema": {"fields": [field.to_api_repr() for field in fields]},
        "rows": [{"f": [_cell(field, row[field.name]) for field in fields]} for row in rows],
    }


class SyntheticBigQueryHTTP:
    def __init__(self, identity, bucket, routes, tables):
        self.identity = identity
        self.bucket = bucket
        self.routes = routes
        self.results = {}
        self.tables = tables
        self.jobs = {}
        self.calls = []

    def _response(self, request, code, body):
        response = requests.Response()
        response.status_code = code
        response.headers["content-type"] = "application/json"
        response._content = json.dumps(body).encode()
        response.request = request
        response.url = request.url
        return response

    def request(self, method, url, **kwargs):
        request = requests.Request(method, url).prepare()
        parsed = urlparse(url)
        self.calls.append((method, parsed.path, parse_qs(parsed.query)))
        if "/tables/" in parsed.path:
            table = parsed.path.rsplit("/", 1)[-1]
            return self._response(request, 200, copy.deepcopy(self.tables[table]))
        if method == "POST":
            native = json.loads(kwargs["data"])
            marker = next(
                name
                for name, needle in self.routes
                if needle in native["configuration"]["query"]["query"]
            )
            stored = copy.deepcopy(native)
            execution = next(
                json.loads(raw)
                for key, (_generation, raw) in self.bucket.objects.items()
                if key.endswith("/execution.json")
                and json.loads(raw)["job_id"] == native["jobReference"]["jobId"]
            )
            stored.update(
                user_email=self.identity["service_account_email"],
                status={"state": "DONE"},
                statistics={
                    "creationTime": str(
                        int(datetime.fromisoformat(execution["recorded_at"]).timestamp() * 1000)
                    ),
                    "query": {
                        "statementType": "SELECT",
                        "totalBytesBilled": "100",
                        "totalBytesProcessed": "100",
                    },
                },
                test_result=marker,
            )
            self.jobs[native["jobReference"]["jobId"]] = stored
            return self._response(request, 200, stored)
        job_id = parsed.path.rsplit("/", 1)[-1]
        resource = self.jobs.get(job_id)
        if resource is None:
            return self._response(request, 404, {"error": {"code": 404, "message": "missing"}})
        if "/queries/" in parsed.path:
            rows = self.results[resource["test_result"]]
            return self._response(request, 200, _query_result(resource, rows))
        native = {key: value for key, value in resource.items() if key != "test_result"}
        return self._response(request, 200, native)


@dataclass
class SourceIntegrationFixture:
    store: object
    invocation: dict
    runtime_identity: dict
    credentials: object
    plan: dict
    scope: dict
    planning_response: dict
    http: SyntheticBigQueryHTTP


def setup_fixture(monkeypatch, *, seed_planning=True):
    from src.analysis.open_intelligence import general_question_context_admission

    monkeypatch.setattr(general_question_context_admission, "_PROTECTED_CONTEXT_PROFILES", ())
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setattr(protected_context_registry, "_PROTECTED_CONTEXT_PROFILES", ())
    # No registered context at all: a committed bridge row would open the context route too.
    monkeypatch.setattr(protected_context_registry, "bridge_entries", lambda: ())
    store, bucket, invocation, identity, credentials = runtime_fixture.setup_runtime()
    scope = store_fixture.scope()
    context = store.read_request(invocation["request_id"], scope=scope)
    draft = copy.deepcopy(runtime_fixture.plan_draft())
    draft["requirements"][0]["search_terms"] = []
    sdk = build_question_planning_request(
        context["request"], context["intake"], policy=store.policy, remaining_seconds=60
    )
    calls = GeneralQuestionCalls(store)
    if seed_planning:
        permit = calls.claim_call(
            invocation["request_id"],
            stage="planning",
            scope=scope,
            input_digest=sdk.input_digest,
            system_instruction_digest=sdk.system_instruction_digest,
            response_schema_digest=sdk.response_schema_digest,
            counted_input_tokens=50,
            now=store_fixture.NOW,
        )
    planning_response = {
        "model_version": "gemini-3.5-flash",
        "usage_metadata": {
            "prompt_token_count": 50,
            "candidates_token_count": 10,
            "thoughts_token_count": 5,
            "total_token_count": 65,
        },
        "candidates": [
            {
                "finish_reason": "STOP",
                "content": {"role": "model", "parts": [{"text": json.dumps(draft)}]},
            }
        ],
    }
    if seed_planning:
        calls.record_response(
            permit,
            scope=scope,
            response=planning_response,
            received_at=store_fixture.NOW + timedelta(seconds=1),
        )
        calls.persist_usage(
            invocation["request_id"], stage="planning", scope=scope, persist=lambda e: e
        )
    plan = validate_question_plan(draft, request=context["request"], intake=context["intake"])
    if seed_planning:
        store._objects.create(f"requests/{invocation['request_id']}/plan.json", plan)

    receipt, run_rows, release_material, manifests, copy_receipts = _release_inputs(monkeypatch)
    release_fixture.module().validate_released_run_admission(
        context["request"],
        plan,
        context["intake"],
        receipt=receipt,
        run_rows=run_rows,
        release_material=release_material,
        producer_profile_id="legacy_r16_v2",
    )
    from google.cloud.bigquery import _helpers
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    encoded_release = _query_result({"jobReference": {}}, [release_material])
    release_schema = [
        bigquery.SchemaField.from_api_repr(field) for field in encoded_release["schema"]["fields"]
    ]
    converted = dict(
        zip(
            (field.name for field in release_schema),
            _helpers._row_tuple_from_json(encoded_release["rows"][0], release_schema),
            strict=True,
        )
    )
    converted = json.loads(canonical_bytes(converted))
    release_fixture.module().validate_released_run_admission(
        context["request"],
        plan,
        context["intake"],
        receipt=receipt,
        run_rows=run_rows,
        release_material=converted,
        producer_profile_id="legacy_r16_v2",
    )
    source_rows = _wire_rows(context["request"], receipt, run_rows, manifests, copy_receipts)
    counts = {
        "candidate_count": receipt.candidate_count,
        "evidence_count": receipt.evidence_count,
        "membership_count": receipt.membership_count,
        "lineage_count": receipt.lineage_count,
        "analysis_count": receipt.analysis_count,
        "prediction_count": receipt.prediction_count,
        "outcome_count_at_completion": len(run_rows["signal_outcomes_v2"]),
    }
    candidate = run_rows["signal_candidates_v2"][0]
    discovery = discovery_fixture.selection_row(
        receipt.run_id,
        candidate["signal_id"],
        client=context["request"]["client_scope_id"],
        market=candidate["market"],
        counts=counts,
    )
    discovery["market_scope"] = context["request"]["market_scope"]
    source_query = source_fixture.module().build_selected_source_query(
        context["request"],
        [
            {
                "run_id": receipt.run_id,
                "signal_id": candidate["signal_id"],
                "market": candidate["market"],
            }
        ],
        plan_window=plan["window"],
        candidate_limit=sum(counts.values()),
        evidence_limit=200,
    )
    source_fixture.module()._decode(source_rows, source_query)
    results = {
        "discovery": [discovery],
        "source": source_rows,
        "release": [release_material],
        "copy": copy_fixture.proof_rows(),
    }
    tables = {
        resource["tableReference"]["tableId"]: resource
        for resource in [*source_schema_fixture.resources(), *copy_schema_fixture.resources()]
    }
    http = SyntheticBigQueryHTTP(
        identity,
        bucket,
        [
            ("copy", "target_manifest_mismatches"),
            ("release", "execution_chains"),
            ("source", "@run_ids"),
            ("discovery", "full_matched_candidate_count"),
        ],
        tables,
    )
    http.results = results
    monkeypatch.setattr(
        AuthorizedSession,
        "request",
        lambda _self, *args, **kwargs: http.request(*args, **kwargs),
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))
    return SourceIntegrationFixture(
        store, invocation, identity, credentials, plan, scope, planning_response, http
    )


def test_joined_source_pipeline_admits_and_revalidates_against_actual_ledger(monkeypatch):
    fixture = setup_fixture(monkeypatch)
    from src.analysis.open_intelligence.general_question_snapshot import (
        build_general_question_snapshot,
        validate_stored_general_question_snapshot,
    )

    result = build_general_question_snapshot(
        fixture.invocation,
        store=fixture.store,
        scope=fixture.scope,
        runtime_identity=fixture.runtime_identity,
        credentials=fixture.credentials,
        plan=fixture.plan,
        candidate_limit=100,
        evidence_limit=200,
        discovery_bytes=100_000_000,
        source_bytes=200_000_000,
        release_bytes=50_000_000,
        now=store_fixture.NOW + timedelta(seconds=2),
    )
    assert result["status"] == "admitted", (
        result,
        len(fixture.http.calls),
        fixture.http.calls[-1][:2],
    )
    snapshot = result["snapshot"]
    queries = GeneralQuestionQueries(fixture.store)
    records = {
        ordinal: queries.read_query(
            fixture.invocation["request_id"], scope=fixture.scope, ordinal=ordinal
        )
        for ordinal in range(1, 5)
    }
    context = fixture.store.read_request(fixture.invocation["request_id"], scope=fixture.scope)
    validated = validate_stored_general_question_snapshot(
        snapshot,
        request=context["request"],
        intake=context["intake"],
        plan=fixture.plan,
        query_records=records,
    )
    assert validated == snapshot
    assert snapshot["readings"][0]["method"] == "selected_receipt_count"
    assert snapshot["readings"][0]["value"] == len(snapshot["receipts"])
    assert snapshot["fulfilled_requirement_ids"] == ["synthetic_mobility"]
    assert snapshot["provenance"]["evidence_limit"] == 200
    assert snapshot["provenance"]["selection"]["reserved_inspection_slots"] == 4
    assert snapshot["missing_work"] == [
        "window:2026-08-23/2026-09-02",
        "window:2026-09-04/2026-09-05",
    ]
    assert len(snapshot["limitations"]) == 3
    assert len([call for call in fixture.http.calls if call[0] == "POST"]) == 4
    assert len([call for call in fixture.http.calls if "/tables/" in call[1]]) == 14

    changed = copy.deepcopy(snapshot)
    changed["receipts"][0]["excerpt"] = "Resealed different excerpt"
    unsigned = {key: value for key, value in changed.items() if key != "snapshot_digest"}
    from src.analysis.open_intelligence.brain_contract import canonical_digest

    changed["snapshot_digest"] = canonical_digest(unsigned)
    with pytest.raises(ValueError, match="snapshot_invalid"):
        validate_stored_general_question_snapshot(
            changed,
            request=context["request"],
            intake=context["intake"],
            plan=fixture.plan,
            query_records=records,
        )

    changed_records = {
        ordinal: {
            "reservation": dict(record["reservation"]),
            "receipt": copy.deepcopy(record["receipt"]),
        }
        for ordinal, record in records.items()
    }
    changed_records[2]["receipt"]["result_digest"] = "f" * 64
    with pytest.raises(ValueError, match="snapshot_invalid"):
        validate_stored_general_question_snapshot(
            snapshot,
            request=context["request"],
            intake=context["intake"],
            plan=fixture.plan,
            query_records=changed_records,
        )


def test_the_joined_pipeline_holds_beside_a_committed_bridge_row(monkeypatch, tmp_path):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    test_joined_source_pipeline_admits_and_revalidates_against_actual_ledger(monkeypatch)


def test_the_joined_pipeline_reads_a_trial_pin_row_left_in_the_registry(monkeypatch, tmp_path):
    """Over the real committed document with a trial pin row left in, the joined pipeline
    reads the bridge row and takes the bridge context route, which refuses until a bridge
    provider is served, instead of the pipeline that runs while nothing is registered."""
    from src.analysis.open_intelligence import protected_context_registry
    from src.analysis.open_intelligence.general_question_snapshot import (
        build_general_question_snapshot,
    )

    from tests.unit.test_protected_context_registry import trial_pin

    read_bridge_rows = protected_context_registry.bridge_entries
    fixture = setup_fixture(monkeypatch)
    monkeypatch.setattr(protected_context_registry, "bridge_entries", read_bridge_rows)
    _w, row = trial_pin(monkeypatch, tmp_path)
    assert fixture.plan["window"]["end"] <= row["cutoff_date"]
    calls = len(fixture.http.calls)
    result = build_general_question_snapshot(
        fixture.invocation,
        store=fixture.store,
        scope=fixture.scope,
        runtime_identity=fixture.runtime_identity,
        credentials=fixture.credentials,
        plan=fixture.plan,
        candidate_limit=100,
        evidence_limit=200,
        discovery_bytes=100_000_000,
        source_bytes=200_000_000,
        release_bytes=50_000_000,
        now=store_fixture.NOW + timedelta(seconds=2),
    )
    assert result == {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "coverage_gap",
        "snapshot": None,
        "reason": "coverage_incomplete",
        "missing_work": ["bridge_clients_unavailable"],
    }
    assert len(fixture.http.calls) == calls
