import ast
import copy
import json
import socket
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import requests
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence import production_snapshot_tables as subject
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit.test_open_intelligence_execution_approval_runtime import (
    SOURCE_SNAPSHOT_NOW,
    _consumed_authority,
    retained_capture_view,
)

NOW = datetime(2030, 1, 3, 12, tzinfo=UTC)
CUTOFF = date(2030, 1, 2)


@pytest.fixture(autouse=True)
def virtual_creation_wait(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(subject, "monotonic", lambda: elapsed[0])

    def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(subject, "sleep", sleep)


def source_metadata():
    return json.loads(source_metadata_bytes())["tables"]


def source_metadata_bytes():
    return (
        Path(__file__).resolve().parents[1]
        / "fixtures/open_intelligence/v3_source_relation_metadata.json"
    ).read_bytes()


def build_plan():
    return subject.build_snapshot_plan(CUTOFF, now=NOW, source_metadata=source_metadata_bytes())


def test_protected_expiration_derivation_preserves_structural_plan_bytes():
    before = build_plan()
    expected_digest = before.plan_digest
    statements = subject.derive_protected_creation_statements(before)
    assert len(statements) == 5
    for original, created in zip(before.statements, statements, strict=True):
        assert set(created) == {"lane", "sql", "sql_digest"}
        assert created["lane"] == original.lane
        assert (
            created["sql"]
            == original.sql
            + "\nOPTIONS (expiration_timestamp = TIMESTAMP '2030-04-03 00:00:00+00')"
        )
        assert created["sql_digest"] == subject.hashlib.sha256(created["sql"].encode()).hexdigest()
        assert "OR REPLACE" not in created["sql"]
        assert "IF NOT EXISTS" not in created["sql"]
    assert before == build_plan()
    assert before.plan_digest == expected_digest


def test_protected_derivation_rejects_resealed_arbitrary_ddl():
    before = build_plan()
    statements = list(before.statements)
    changed_sql = statements[0].sql + "; DROP TABLE `foreign.table`"
    statements[0] = replace(
        statements[0],
        sql=changed_sql,
        sql_digest=subject.hashlib.sha256(changed_sql.encode()).hexdigest(),
    )
    with pytest.raises(ValueError):
        subject.derive_protected_creation_statements(reseal(before, statements=tuple(statements)))


def validate(plan, metadata, rows, *, reviewed=None):
    return subject.validate_snapshot_readback(
        plan,
        source_metadata=reviewed or source_metadata_bytes(),
        snapshot_metadata=metadata,
        readback_rows=rows,
    )


def snapshot_metadata(plan):
    sources = source_metadata()
    return {
        item.lane: {
            "tableReference": {
                "projectId": "ogilvy-trends-v2",
                "datasetId": "trends_v2_staging",
                "tableId": item.destination_table.rsplit(".", 1)[-1],
            },
            "type": "SNAPSHOT",
            "etag": f"synthetic-{item.lane}",
            "numRows": "0",
            "schema": copy.deepcopy(sources[f"trends_v2_dev.{item.lane}"]["schema"]),
            "snapshotDefinition": {
                "baseTableReference": {
                    "projectId": "ogilvy-trends-v2",
                    "datasetId": "trends_v2_dev",
                    "tableId": item.lane,
                },
                "snapshotTime": plan.source_as_of.isoformat().replace("+00:00", "Z"),
            },
        }
        for item in plan.statements
    }


def readback(plan):
    return [
        {
            "lane": item.lane,
            "table_id": item.destination_table.rsplit(".", 1)[-1],
            "row_count": 0,
        }
        for item in plan.statements
    ]


def reseal(plan, **changes):
    value = replace(plan, **changes)
    digest = subject.canonical_digest(
        subject._plan_core(
            value.profile_id,
            value.cutoff_date,
            value.source_as_of,
            value.source_metadata_digest,
            value.statements,
        )
    )
    return replace(value, plan_digest=digest)


class CreationCredentials(AnonymousCredentials):
    service_account_email = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"


class CreationHTTP:
    is_mtls = False

    def __init__(self, plan):
        self.plan = plan
        self.calls = []
        self.jobs = {}
        self.lose_ack = False
        self.missing = False
        self.mutate_job = lambda value: value
        self.mutate_table = lambda value: value

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        assert parsed.netloc == "bigquery.googleapis.com"
        assert kwargs.get("allow_redirects") is False
        payload = json.loads(kwargs["data"]) if kwargs.get("data") else None
        self.calls.append((method, parsed.path, payload))
        status = 200
        if method == "POST":
            lane = next(
                item
                for item in subject.LANES
                if payload["jobReference"]["jobId"].endswith("_" + item)
            )
            item = next(item for item in self.plan.statements if item.lane == lane)
            body = copy.deepcopy(payload)
            body.update(
                user_email=CreationCredentials.service_account_email,
                status={"state": "DONE"},
                statistics={
                    "creationTime": str(
                        int((SOURCE_SNAPSHOT_NOW + timedelta(seconds=2)).timestamp() * 1000)
                    ),
                    "query": {
                        "statementType": "CREATE_SNAPSHOT_TABLE",
                        "ddlOperationPerformed": "CREATE",
                        "ddlTargetTable": {
                            "projectId": subject.PROJECT,
                            "datasetId": subject.DESTINATION_DATASET,
                            "tableId": item.destination_table.rsplit(".", 1)[1],
                        },
                        "totalBytesBilled": "0",
                    },
                },
            )
            self.jobs[payload["jobReference"]["jobId"]] = self.mutate_job(body)
            if self.lose_ack:
                raise requests.Timeout("PRIVATE synthetic acknowledgement text")
        elif "/tables/" in parsed.path:
            lane = next(item for item in subject.LANES if parsed.path.endswith("_" + item))
            body = snapshot_metadata(self.plan)[lane]
            body.update(
                location="US",
                numBytes="0",
                creationTime=str(
                    int((SOURCE_SNAPSHOT_NOW + timedelta(seconds=2)).timestamp() * 1000)
                ),
                expirationTime=str(
                    int((self.plan.source_as_of + timedelta(days=90)).timestamp() * 1000)
                ),
            )
            body = self.mutate_table(body)
        elif "/queries/" in parsed.path:
            body = {"jobComplete": True, "totalRows": "0", "rows": []}
        elif self.missing:
            status, body = 404, {"error": {"code": 404, "message": "PRIVATE missing"}}
        else:
            body = self.jobs[parsed.path.rsplit("/", 1)[1]]
        response = requests.Response()
        response.status_code = status
        response.headers["content-type"] = "application/json"
        response._content = json.dumps(body).encode()
        response.request = requests.Request(method, url).prepare()
        return response


def retained_closures(view):
    """Test stand-ins for the two closures the guarded entry builds over the live authority.
    recheck revalidates the retained consumption against the packaged registry and records
    the call; claim records the retained view once and refuses a second claim of the same
    view, as the live claim refuses the same authority object."""
    calls = []

    def recheck():
        subject._require_retained_consumption(
            view.consumption,
            "source_snapshot_capture",
            mode="historical_replay",
            registry=subject.retained_origin_registry(),
        )
        calls.append("recheck")

    def claim():
        if "claim" in calls:
            raise ValueError("snapshot_creation_already_attempted")
        calls.append("claim")
        claim.views.append(view)

    recheck.calls = claim.calls = calls
    claim.views = []
    return recheck, claim


def creation_setup(monkeypatch, *, recovery=None, reader=None):
    from src.analysis.open_intelligence.production_snapshot import _serialize_plan

    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    plan = subject.build_snapshot_plan(
        date(2026, 9, 7), now=SOURCE_SNAPSHOT_NOW, source_metadata=source_metadata_bytes()
    )
    statements = subject.derive_protected_creation_statements(plan)
    envelope = {
        "contract_version": "open_intelligence_protected_capture_plan_v1",
        "cutoff_date": "2026-09-07",
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "snapshot_plan": _serialize_plan(plan),
        "creation_statements": statements,
    }
    view = retained_capture_view(
        artifacts={
            "capture_plan": canonical_bytes(envelope),
            "source_metadata": source_metadata_bytes(),
            "recovery_context": canonical_bytes(recovery),
        },
        mode="recover" if recovery else "initial",
    )
    recheck, claim = retained_closures(view)
    http = CreationHTTP(plan)
    client = bigquery.Client(
        project=subject.PROJECT, location="US", credentials=CreationCredentials(), _http=http
    )
    return http, {
        "plan": plan,
        "creation_statements": statements,
        "source_metadata": source_metadata_bytes(),
        "capture_plan": envelope,
        "manifest": view.manifest,
        "consumption": view.consumption,
        "recheck": recheck,
        "claim": claim,
        "client": client,
        "now": SOURCE_SNAPSHOT_NOW + timedelta(seconds=3),
        "recovery_context": recovery,
        "initial_result_reader": reader,
    }


def test_native_creation_uses_consumed_plan_and_five_exact_insert_jobs(monkeypatch):
    http, args = creation_setup(monkeypatch)
    result = subject._execute_validated_snapshot_creations(**args)
    assert [row["state"] for row in result["creation_records"]] == ["succeeded"] * 5
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == 5
    for call, statement in zip(posts, args["creation_statements"], strict=True):
        payload = call[2]
        assert (
            payload["jobReference"]["jobId"]
            == f"oi_v3_snapshot_{args['consumption'].manifest_sha256}_{statement['lane']}"
        )
        assert payload["configuration"]["query"]["query"] == statement["sql"]
        assert "destinationTable" not in payload["configuration"]["query"]
    assert all(
        "expirationTime" in item["snapshot_metadata"] for item in result["creation_evidence"]
    )
    assert all(row["row_count"] == 0 for row in result["readback_rows"])


@pytest.mark.parametrize("mutation", ["authority", "envelope", "sql", "source", "expired"])
def test_creation_refuses_before_io_when_binding_invalid(monkeypatch, mutation):
    http, args = creation_setup(monkeypatch)
    if mutation == "authority":
        args["consumption"] = object()
    elif mutation == "envelope":
        args["capture_plan"]["client_scope_id"] = "foreign"
    elif mutation == "sql":
        args["creation_statements"][0]["sql"] += "; SELECT 1"
    elif mutation == "source":
        args["source_metadata"] += b" "
    else:
        args["now"] += timedelta(hours=2)
    with pytest.raises(ValueError):
        subject._execute_validated_snapshot_creations(**args)
    assert http.calls == []


@pytest.mark.parametrize(
    "mutation", ["creator", "sql", "mode", "target", "time", "state", "expiry"]
)
def test_native_creation_refuses_partial_mismatched_readback(monkeypatch, mutation):
    http, args = creation_setup(monkeypatch)

    def mutate(body):
        if mutation == "creator":
            body["user_email"] = "foreign@example.invalid"
        if mutation == "sql":
            body["configuration"]["query"]["query"] += "; SELECT 1"
        if mutation == "mode":
            body["configuration"]["query"]["useLegacySql"] = True
        if mutation == "target":
            body["statistics"]["query"]["ddlTargetTable"]["tableId"] = "foreign"
        if mutation == "time":
            body["statistics"].pop("creationTime")
        if mutation == "state":
            body["status"]["state"] = None
        return body

    http.mutate_job = mutate
    if mutation == "expiry":
        http.mutate_table = lambda body: {**body, "expirationTime": "1"}
    with pytest.raises(subject.SnapshotCreationError) as raised:
        subject._execute_validated_snapshot_creations(**args)
    assert len([c for c in http.calls if c[0] == "POST"]) == 1
    assert len(raised.value.creation_records) == 1
    assert raised.value.creation_records[0]["state"] != "succeeded"
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("missing", [False, True])
def test_unknown_ack_never_posts_same_job_again(monkeypatch, missing):
    http, args = creation_setup(monkeypatch)
    http.lose_ack, http.missing = True, missing
    if missing:
        with pytest.raises(subject.SnapshotCreationError) as raised:
            subject._execute_validated_snapshot_creations(**args)
        assert raised.value.creation_records[0]["state"] == "unresolved"
        assert len([c for c in http.calls if c[0] == "POST"]) == 1
    else:
        result = subject._execute_validated_snapshot_creations(**args)
        assert len(result["creation_records"]) == 5
        assert len([c for c in http.calls if c[0] == "POST"]) == 5


def test_recovery_missing_native_initial_result_never_means_unattempted(monkeypatch):
    context = {
        "contract_version": "open_intelligence_source_capture_recovery_v1",
        "initial_manifest_sha256": "1" * 64,
        "initial_consumption_id": "exc_" + "2" * 64,
        "initial_execution_name": "unresolved",
        "initial_result_id": "exr_" + "3" * 64,
        "initial_result_digest": "4" * 64,
    }
    http, args = creation_setup(monkeypatch, recovery=context, reader=lambda value: None)
    with pytest.raises(ValueError):
        subject._execute_validated_snapshot_creations(**args)
    assert http.calls == []


def test_exact_protected_expiration_is_compatible_with_old_readback(monkeypatch):
    plan = build_plan()
    metadata = snapshot_metadata(plan)
    old = validate(plan, metadata, readback(plan))
    for resource in metadata.values():
        resource["expirationTime"] = str(
            int((plan.source_as_of + timedelta(days=90)).timestamp() * 1000)
        )
    assert validate(plan, metadata, readback(plan)) == old


def initial_creation_result(args, records):
    from src.analysis.open_intelligence import execution_approval as approval

    payload = {
        "contract_version": "open_intelligence_protected_source_snapshot_v1",
        "cutoff_date": args["plan"].cutoff_date.isoformat(),
        "source_as_of": args["plan"].source_as_of.isoformat(),
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "snapshot_plan_digest": args["plan"].plan_digest,
        "creation_records": records,
        "query_count": 0,
        "captured_at": None,
        "snapshot_digest": None,
        "capture_receipt_digest": None,
        "artifact_attempt": None,
        "stored_artifact": None,
    }
    digest = subject.canonical_digest(payload)
    completed = SOURCE_SNAPSHOT_NOW + timedelta(seconds=4)
    consumption = args["consumption"]
    reference = "synthetic:initial-snapshot-result"
    result = approval.ExecutionResult(
        result_contract_version="open_intelligence_execution_result_v1",
        result_id=approval.result_id(
            consumption.consumption_id, reference, digest, "failed", completed
        ),
        consumption_id=consumption.consumption_id,
        approval_id=consumption.approval_id,
        manifest_sha256=consumption.manifest_sha256,
        operation="source_snapshot_capture",
        execution_name=consumption.execution_name,
        result_reference=reference,
        canonical_result_json=canonical_bytes(payload).decode(),
        result_digest=digest,
        status="failed",
        completed_at=completed,
    )
    context = {
        "contract_version": "open_intelligence_source_capture_recovery_v1",
        **{
            "initial_" + field: getattr(result, field)
            for field in (
                "manifest_sha256",
                "consumption_id",
                "execution_name",
                "result_id",
                "result_digest",
            )
        },
    }
    return result, context


@pytest.mark.parametrize("unresolved", [False, True])
def test_recovery_revalidates_original_job_and_only_creates_unattempted_lanes(
    monkeypatch, unresolved
):
    http, initial_args = creation_setup(monkeypatch)
    http.lose_ack, http.missing = True, True
    with pytest.raises(subject.SnapshotCreationError) as partial:
        subject._execute_validated_snapshot_creations(**initial_args)
    initial, context = initial_creation_result(initial_args, partial.value.creation_records)
    recovered_http, args = creation_setup(
        monkeypatch, recovery=context, reader=lambda requested: asdict(initial)
    )
    args["now"] = SOURCE_SNAPSHOT_NOW + timedelta(seconds=5)
    recovered_http.jobs = copy.deepcopy(http.jobs)
    recovered_http.missing = unresolved
    if unresolved:
        with pytest.raises(subject.SnapshotCreationError):
            subject._execute_validated_snapshot_creations(**args)
        assert not any(call[0] == "POST" for call in recovered_http.calls)
    else:
        result = subject._execute_validated_snapshot_creations(**args)
        assert len([call for call in recovered_http.calls if call[0] == "POST"]) == 4
        assert (
            result["creation_records"][0]["job_id"] == partial.value.creation_records[0]["job_id"]
        )
        assert result["creation_evidence"][0]["consumption_id"] == initial.consumption_id
        assert all(row["state"] == "succeeded" for row in result["creation_records"])


def failed_creation_recovery(
    monkeypatch,
    failed_index=0,
    *,
    target_missing=True,
    unresolved=False,
    mutate_payload=None,
    version=2,
    wrong_digest=False,
):
    http, initial_args = creation_setup(monkeypatch)
    lane = subject.LANES[failed_index]

    def fail_job(value):
        if value["jobReference"]["jobId"].endswith("_" + lane):
            value["status"]["errorResult"] = {"reason": "accessDenied", "message": "private"}
            value["statistics"]["query"].pop("ddlOperationPerformed")
        return value

    http.mutate_job = fail_job
    with pytest.raises(subject.SnapshotCreationError) as caught:
        subject._execute_validated_snapshot_creations(**initial_args)
    records = copy.deepcopy(caught.value.creation_records)
    if unresolved:
        records[-1].update(state="unresolved", native_job_digest=None)
    initial, context = initial_creation_result(initial_args, records)
    if version == 2:
        context.update(
            contract_version="open_intelligence_source_capture_recovery_v2",
            failed_creation_job_digest=(
                "a" * 64
                if wrong_digest
                else subject.canonical_digest(http.jobs[records[-1]["job_id"]])
            ),
        )
    if mutate_payload is not None:
        from src.analysis.open_intelligence import execution_approval

        payload = json.loads(initial.canonical_result_json)
        mutate_payload(payload)
        digest = subject.canonical_digest(payload)
        initial = replace(
            initial,
            canonical_result_json=canonical_bytes(payload).decode(),
            result_digest=digest,
            result_id=execution_approval.result_id(
                initial.consumption_id,
                initial.result_reference,
                digest,
                initial.status,
                initial.completed_at,
            ),
        )
        context.update(initial_result_id=initial.result_id, initial_result_digest=digest)
    recovered, args = creation_setup(
        monkeypatch, recovery=context, reader=lambda _: asdict(initial)
    )
    recovered.jobs = copy.deepcopy(http.jobs)
    args["now"] = SOURCE_SNAPSHOT_NOW + timedelta(seconds=5)
    original_request = recovered.request

    def request(method, url, **kwargs):
        if (
            target_missing
            and "/tables/" in url
            and urlparse(url).path.endswith("_" + lane)
            and not any(
                job["jobReference"]["jobId"].endswith("_" + lane)
                and not job["status"].get("errorResult")
                for job in recovered.jobs.values()
            )
        ):
            recovered.calls.append((method, urlparse(url).path, None))
            response = requests.Response()
            response.status_code = 404
            response._content = b'{"error":{"code":404,"message":"Not found"}}'
            response.request = requests.Request(method, url).prepare()
            return response
        return original_request(method, url, **kwargs)

    recovered.request = request
    return http, recovered, args, initial


@pytest.mark.parametrize("failed_index", [0, 2, 4])
@pytest.mark.parametrize("unresolved", [False, True])
def test_one_failed_last_creation_can_be_replaced_after_native_404(
    monkeypatch, failed_index, unresolved
):
    original, recovered, args, initial = failed_creation_recovery(
        monkeypatch, failed_index, unresolved=unresolved
    )
    result = subject._execute_validated_snapshot_creations(**args)
    assert sum(c[0] == "POST" for c in original.calls + recovered.calls) == 6
    assert len(result["creation_records"]) == 5
    assert len(result["creation_evidence"]) == 6
    failed, *successes = result["creation_evidence"]
    assert failed["lane"] == subject.LANES[failed_index]
    assert failed["snapshot_metadata"] is None
    assert failed["native_job"]["status"]["errorResult"]["reason"] == "accessDenied"
    assert failed["manifest_sha256"] == initial.manifest_sha256
    assert [entry["manifest_sha256"] for entry in successes] == (
        [initial.manifest_sha256] * failed_index
        + [args["consumption"].manifest_sha256] * (5 - failed_index)
    )


def test_failed_creation_never_replaces_existing_target(monkeypatch):
    _original, recovered, args, _initial = failed_creation_recovery(
        monkeypatch, target_missing=False
    )
    with pytest.raises(subject.SnapshotCreationError):
        subject._execute_validated_snapshot_creations(**args)
    assert not any(c[0] == "POST" for c in recovered.calls)


def continuation_setup(monkeypatch):
    _original_http, predecessor_http, predecessor_args, ancestor = failed_creation_recovery(
        monkeypatch
    )
    predecessor_http.mutate_table = lambda value: {**value, "creationTime": "0"}
    with pytest.raises(subject.SnapshotCreationError) as partial:
        subject._execute_validated_snapshot_creations(**predecessor_args)
    assert len(partial.value.creation_records) == 1
    partial.value.creation_records[-1]["native_job_digest"] = None
    predecessor, context = initial_creation_result(predecessor_args, partial.value.creation_records)
    context.update(
        contract_version="open_intelligence_source_capture_recovery_v3",
        ancestor_recovery_context=predecessor_args["recovery_context"],
    )
    values = {ancestor.result_id: asdict(ancestor), predecessor.result_id: asdict(predecessor)}
    http, args = creation_setup(monkeypatch, recovery=context, reader=values.get)
    http.jobs = copy.deepcopy(predecessor_http.jobs)
    args["now"] = SOURCE_SNAPSHOT_NOW + timedelta(seconds=6)
    return http, args, ancestor, predecessor


def test_v3_preserves_predecessor_snapshot_and_creates_only_four_unattempted_lanes(monkeypatch):
    http, args, ancestor, predecessor = continuation_setup(monkeypatch)
    result = subject._execute_validated_snapshot_creations(**args)
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == 4
    assert all(not call[2]["jobReference"]["jobId"].endswith("_event_ledger") for call in posts)
    assert [entry["manifest_sha256"] for entry in result["creation_evidence"]] == [
        ancestor.manifest_sha256,
        predecessor.manifest_sha256,
    ] + [args["consumption"].manifest_sha256] * 4


def test_v3_missing_predecessor_job_never_authorizes_another_replacement(monkeypatch):
    http, args, _ancestor, predecessor = continuation_setup(monkeypatch)
    del http.jobs[f"oi_v3_snapshot_{predecessor.manifest_sha256}_event_ledger"]
    with pytest.raises(subject.SnapshotCreationError):
        subject._execute_validated_snapshot_creations(**args)
    assert not any(call[0] == "POST" for call in http.calls)


def metadata_continuation_setup(monkeypatch):
    predecessor_http, predecessor_args, origin, ancestor = continuation_setup(monkeypatch)
    predecessor_http.mutate_table = lambda value: (
        {**value, "creationTime": "0"}
        if value["tableReference"]["tableId"].endswith("_enriched_content")
        else value
    )
    with pytest.raises(subject.SnapshotCreationError) as partial:
        subject._execute_validated_snapshot_creations(**predecessor_args)
    assert len(partial.value.creation_records) == 4
    predecessor, context = initial_creation_result(predecessor_args, partial.value.creation_records)
    context.update(
        contract_version="open_intelligence_source_capture_recovery_v4",
        ancestor_recovery_context=predecessor_args["recovery_context"],
    )
    rows = {row.result_id: asdict(row) for row in (origin, ancestor, predecessor)}
    http, args = creation_setup(monkeypatch, recovery=context, reader=rows.get)
    http.jobs = copy.deepcopy(predecessor_http.jobs)
    args["now"] = SOURCE_SNAPSHOT_NOW + timedelta(seconds=7)
    return http, args, origin, ancestor, predecessor


def test_v4_preserves_four_snapshots_and_creates_only_raw(monkeypatch):
    http, args, origin, ancestor, predecessor = metadata_continuation_setup(monkeypatch)
    result = subject._execute_validated_snapshot_creations(**args)
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2]["jobReference"]["jobId"].endswith("_raw_content")
    assert [entry["manifest_sha256"] for entry in result["creation_evidence"]] == [
        origin.manifest_sha256,
        ancestor.manifest_sha256,
    ] + [predecessor.manifest_sha256] * 3 + [args["consumption"].manifest_sha256]


def test_v4_missing_predecessor_job_cannot_be_replaced(monkeypatch):
    http, args, _origin, _ancestor, predecessor = metadata_continuation_setup(monkeypatch)
    del http.jobs[f"oi_v3_snapshot_{predecessor.manifest_sha256}_enriched_content"]
    with pytest.raises(subject.SnapshotCreationError):
        subject._execute_validated_snapshot_creations(**args)
    assert not any(call[0] == "POST" for call in http.calls)


@pytest.mark.parametrize("mutation", [None, "nested_v4", "missing_v2", "extra"])
def test_v4_has_exact_three_baselines_and_fixed_ancestry_depth(monkeypatch, mutation):
    from scripts.staging.capture_protected_production_snapshot import (
        _initial_capture_manifest,
        _initial_result_reader,
        _validate_recovery,
    )

    _http, args, origin, ancestor, predecessor = metadata_continuation_setup(monkeypatch)
    context = copy.deepcopy(args["recovery_context"])
    if mutation == "nested_v4":
        context["ancestor_recovery_context"]["contract_version"] = context["contract_version"]
    elif mutation == "missing_v2":
        context["ancestor_recovery_context"].pop("ancestor_recovery_context")
    elif mutation == "extra":
        context["extra"] = None
    if mutation:
        with pytest.raises(ValueError, match="snapshot_recovery_invalid"):
            _validate_recovery(context, "recover")
        return
    _validate_recovery(context, "recover")
    assert _initial_capture_manifest(context, "unused") == origin.manifest_sha256
    rows = {value.consumption_id: [asdict(value)] for value in (origin, ancestor, predecessor)}
    calls = []
    read = _initial_result_reader(context, lambda cid: calls.append(cid) or rows[cid])
    for _ in range(2):
        for value in (predecessor, ancestor, origin):
            assert read(value.result_id)["result_id"] == value.result_id
    assert calls == [predecessor.consumption_id, ancestor.consumption_id, origin.consumption_id]


@pytest.mark.parametrize("mutation", [None, "missing_ancestor", "nested_v1", "nested_v3", "extra"])
def test_v3_context_has_exact_fixed_depth_and_reader_ids(monkeypatch, mutation):
    from scripts.staging.capture_protected_production_snapshot import (
        _initial_result_reader,
        _validate_recovery,
    )

    _http, args, ancestor, predecessor = continuation_setup(monkeypatch)
    context = copy.deepcopy(args["recovery_context"])
    if mutation == "missing_ancestor":
        context.pop("ancestor_recovery_context")
    elif mutation in {"nested_v1", "nested_v3"}:
        context["ancestor_recovery_context"]["contract_version"] = (
            "open_intelligence_source_capture_recovery_" + mutation.removeprefix("nested_")
        )
    elif mutation == "extra":
        context["extra"] = None
    if mutation is not None:
        with pytest.raises(ValueError, match="snapshot_recovery_invalid"):
            _validate_recovery(context, "recover")
        return
    _validate_recovery(context, "recover")
    calls = []
    rows = {
        ancestor.consumption_id: [asdict(ancestor)],
        predecessor.consumption_id: [asdict(predecessor)],
    }
    reader = _initial_result_reader(context, lambda cid: calls.append(cid) or rows[cid])
    for _ in range(2):
        assert reader(predecessor.result_id)["result_id"] == predecessor.result_id
        assert reader(ancestor.result_id)["result_id"] == ancestor.result_id
    assert calls == [predecessor.consumption_id, ancestor.consumption_id]
    with pytest.raises(ValueError, match="snapshot_recovery_invalid"):
        reader("exr_" + "f" * 64)


@pytest.mark.parametrize("version,wrong_digest", [(1, False), (2, True)])
def test_failed_replacement_requires_manifest_pinned_native_digest(
    monkeypatch, version, wrong_digest
):
    _original, recovered, args, _initial = failed_creation_recovery(
        monkeypatch, version=version, wrong_digest=wrong_digest
    )
    with pytest.raises(subject.SnapshotCreationError):
        subject._execute_validated_snapshot_creations(**args)
    assert not any(c[0] == "POST" for c in recovered.calls)


@pytest.mark.parametrize(
    "mutation", [None, "missing", "extra", "uppercase", "short", "number", "v1", "unknown"]
)
def test_recovery_v2_requires_exact_keys_and_lowercase_native_digest(mutation):
    from scripts.staging.capture_protected_production_snapshot import _validate_recovery

    context = {
        "contract_version": "open_intelligence_source_capture_recovery_v2",
        "initial_manifest_sha256": "a" * 64,
        "initial_consumption_id": "exc_" + "b" * 64,
        "initial_execution_name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/executions/source-test-1",
        "initial_result_id": "exr_" + "c" * 64,
        "initial_result_digest": "d" * 64,
        "failed_creation_job_digest": "e" * 64,
    }
    if mutation is None:
        _validate_recovery(context, "recover")
        return
    if mutation == "missing":
        context.pop("failed_creation_job_digest")
    elif mutation == "extra":
        context["extra"] = None
    elif mutation in {"v1", "unknown"}:
        context["contract_version"] = "open_intelligence_source_capture_recovery_" + mutation
    else:
        context["failed_creation_job_digest"] = {
            "uppercase": "E" * 64,
            "short": "e" * 63,
            "number": 1,
        }[mutation]
    with pytest.raises(ValueError, match="snapshot_recovery_invalid"):
        _validate_recovery(context, "recover")


@pytest.mark.parametrize("mutation", ["queries", "capture", "artifact", "failed_prefix"])
def test_replacement_refuses_initial_capture_or_multiple_failed_lanes(monkeypatch, mutation):
    def mutate(payload):
        if mutation == "queries":
            payload["query_count"] = 1
        elif mutation == "capture":
            payload["captured_at"] = SOURCE_SNAPSHOT_NOW.isoformat()
        elif mutation == "artifact":
            payload["artifact_attempt"] = {}
        else:
            payload["creation_records"][0]["state"] = "failed"

    _original, recovered, args, _initial = failed_creation_recovery(
        monkeypatch, failed_index=2, mutate_payload=mutate
    )
    with pytest.raises(subject.SnapshotCreationError):
        subject._execute_validated_snapshot_creations(**args)
    assert not any(c[0] == "POST" for c in recovered.calls)


def test_reentry_with_same_consumed_authority_cannot_repost_after_unknown(monkeypatch):
    http, args = creation_setup(monkeypatch)
    http.lose_ack, http.missing = True, True
    with pytest.raises(subject.SnapshotCreationError):
        subject._execute_validated_snapshot_creations(**args)
    calls = len(http.calls)
    with pytest.raises(ValueError):
        subject._execute_validated_snapshot_creations(**args)
    assert len(http.calls) == calls


@pytest.mark.parametrize("value", [None, "1", True, "NaN", "-1"])
def test_creation_unknown_or_nonzero_billing_holds_native_measurement(monkeypatch, value):
    http, args = creation_setup(monkeypatch)

    def mutate(body):
        body["statistics"]["query"]["totalBytesBilled"] = value
        return body

    http.mutate_job = mutate
    with pytest.raises(subject.SnapshotCreationError) as raised:
        subject._execute_validated_snapshot_creations(**args)
    assert len([call for call in http.calls if call[0] == "POST"]) == 1
    assert (
        raised.value.creation_evidence[0]["native_job"]["statistics"]["query"]["totalBytesBilled"]
        == value
    )


def test_creation_preserves_original_native_job_timestamp_bytes(monkeypatch):
    _http, args = creation_setup(monkeypatch)
    result = subject._execute_validated_snapshot_creations(**args)
    assert type(result["creation_evidence"][0]["native_job"]["statistics"]["creationTime"]) is str


@pytest.mark.parametrize("size", [None, True, 0, "-1", "NaN", "", "\uff11\uff12"])
def test_creation_requires_known_native_retained_size(monkeypatch, size):
    http, args = creation_setup(monkeypatch)

    def mutate(resource):
        if size is None:
            resource.pop("numBytes")
        else:
            resource["numBytes"] = size
        return resource

    http.mutate_table = mutate
    with pytest.raises(subject.SnapshotCreationError) as raised:
        subject._execute_validated_snapshot_creations(**args)
    assert len([call for call in http.calls if call[0] == "POST"]) == 1
    assert raised.value.creation_records[0]["state"] == "unresolved"
    assert raised.value.creation_evidence[0]["snapshot_metadata"].get("numBytes") == size


@pytest.mark.parametrize(
    "first_size,second_size,submissions", [(5368709121, 0, 1), (5368709120, 1, 2)]
)
def test_creation_stops_at_cumulative_retained_size_overflow(
    monkeypatch, first_size, second_size, submissions
):
    http, args = creation_setup(monkeypatch)
    sizes = dict(
        zip(subject.LANES, (str(first_size), str(second_size), "0", "0", "0"), strict=True)
    )
    http.mutate_table = lambda resource: {
        **resource,
        "numBytes": sizes[
            next(
                lane
                for lane in subject.LANES
                if resource["tableReference"]["tableId"].endswith("_" + lane)
            )
        ],
    }
    with pytest.raises(subject.SnapshotCreationError) as raised:
        subject._execute_validated_snapshot_creations(**args)
    assert len([call for call in http.calls if call[0] == "POST"]) == submissions
    assert len(raised.value.creation_records) == submissions
    assert raised.value.creation_records[-1]["state"] == "unresolved"
    assert (
        sum(int(item["snapshot_metadata"]["numBytes"]) for item in raised.value.creation_evidence)
        > 5368709120
    )


def test_creation_accepts_exact_cumulative_retained_size_cap(monkeypatch):
    http, args = creation_setup(monkeypatch)
    sizes = iter(("5368709120", "0", "0", "0", "0"))
    http.mutate_table = lambda resource: {**resource, "numBytes": next(sizes)}
    result = subject._execute_validated_snapshot_creations(**args)
    assert len(result["creation_records"]) == 5
    assert sum(int(item["numBytes"]) for item in result["snapshot_metadata"].values()) == 5368709120


def test_recovery_rechecks_retained_size_before_new_creation(monkeypatch):
    http, initial_args = creation_setup(monkeypatch)
    http.lose_ack, http.missing = True, True
    with pytest.raises(subject.SnapshotCreationError) as partial:
        subject._execute_validated_snapshot_creations(**initial_args)
    initial, context = initial_creation_result(initial_args, partial.value.creation_records)
    recovered_http, args = creation_setup(
        monkeypatch, recovery=context, reader=lambda requested: asdict(initial)
    )
    args["now"] = SOURCE_SNAPSHOT_NOW + timedelta(seconds=5)
    recovered_http.jobs = copy.deepcopy(http.jobs)
    recovered_http.mutate_table = lambda resource: {**resource, "numBytes": "5368709121"}
    with pytest.raises(subject.SnapshotCreationError) as raised:
        subject._execute_validated_snapshot_creations(**args)
    assert not any(call[0] == "POST" for call in recovered_http.calls)
    assert raised.value.creation_evidence[0]["snapshot_metadata"]["numBytes"] == "5368709121"


@pytest.mark.parametrize("value", [None, "1", "9999999999999", True])
def test_creation_refuses_missing_or_incoherent_target_creation_time(monkeypatch, value):
    http, args = creation_setup(monkeypatch)
    http.mutate_table = lambda resource: {**resource, "creationTime": value}
    with pytest.raises(subject.SnapshotCreationError):
        subject._execute_validated_snapshot_creations(**args)
    assert len([call for call in http.calls if call[0] == "POST"]) == 1


def test_actual_requests_redirect_never_resends_ddl_to_foreign_host(monkeypatch):
    _http, args = creation_setup(monkeypatch)
    sent = []

    class RedirectAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            sent.append(request.url)
            response = requests.Response()
            response.status_code = 307
            response.headers["location"] = "https://foreign.invalid/steal"
            response._content = b'{"error":{"code":307,"message":"PRIVATE provider text"}}'
            response.request = request
            return response

        def close(self):
            pass

    session = requests.Session()
    session.is_mtls = False
    session.mount("https://", RedirectAdapter())
    args["client"] = bigquery.Client(
        project=subject.PROJECT, location="US", credentials=CreationCredentials(), _http=session
    )
    with pytest.raises(subject.SnapshotCreationError) as raised:
        subject._execute_validated_snapshot_creations(**args)
    assert len(sent) == 2
    assert all(urlparse(url).netloc == "bigquery.googleapis.com" for url in sent)
    assert "PRIVATE" not in str(raised.value)


def test_builds_five_fixed_non_overwriting_cutoff_close_statements():
    plan = build_plan()
    assert plan.profile_id == "trends_v2_dev_table_snapshot_v1"
    assert plan.source_as_of == datetime(2030, 1, 3, tzinfo=UTC)
    assert [item.lane for item in plan.statements] == list(subject.LANES)
    assert len({item.destination_table for item in plan.statements}) == 5
    for item in plan.statements:
        assert item.source_table == f"ogilvy-trends-v2.trends_v2_dev.{item.lane}"
        assert item.destination_table == (
            f"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20300102_{item.lane}"
        )
        assert f"CREATE SNAPSHOT TABLE `{item.destination_table}`" in item.sql
        assert f"CLONE `{item.source_table}`" in item.sql
        assert "FOR SYSTEM_TIME AS OF TIMESTAMP '2030-01-03 00:00:00+00'" in item.sql
        assert "OR REPLACE" not in item.sql
        assert "IF NOT EXISTS" not in item.sql
        assert ";" not in item.sql
        assert len(item.sql_digest) == len(item.source_schema_digest) == 64
    with pytest.raises(FrozenInstanceError):
        plan.statements[0].sql = "changed"


@pytest.mark.parametrize(
    "cutoff,now",
    [
        (datetime(2030, 1, 2, tzinfo=UTC), NOW),
        (CUTOFF, datetime(2030, 1, 2, 23, 59, tzinfo=UTC)),
        (date(2029, 12, 25), NOW),
        (CUTOFF, datetime(2030, 1, 3, 12)),
        (True, NOW),
    ],
)
def test_invalid_or_unavailable_cutoff_refuses(cutoff, now):
    with pytest.raises(ValueError, match="snapshot_plan_invalid"):
        subject.build_snapshot_plan(cutoff, now=now, source_metadata=source_metadata_bytes())


def test_readback_proves_physical_snapshots_without_collection_claim():
    plan = build_plan()
    result = validate(plan, snapshot_metadata(plan), readback(plan))
    assert result["physical_snapshot_complete"] is True
    assert result["collection_complete"] is False
    assert result["limitations"] == [
        "streaming_buffer_exclusion_unproven",
        "upstream_collection_completeness_unproven",
        "retention_and_cost_review_required",
    ]
    assert len(result["tables"]) == 5
    assert all(row["type"] == "SNAPSHOT" for row in result["tables"])


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "profile",
        "asof",
        "source",
        "destination",
        "sql",
        "order",
        "duplicate",
        "schema_digest",
    ],
)
def test_resealed_plan_cannot_change_fixed_five_statement_authority(mutation):
    plan = build_plan()
    statements = list(plan.statements)
    changes = {}
    if mutation == "empty":
        changes["statements"] = ()
    elif mutation == "profile":
        changes["profile_id"] = "other"
    elif mutation == "asof":
        changes["source_as_of"] = plan.source_as_of + timedelta(seconds=1)
    elif mutation == "order":
        statements[0], statements[1] = statements[1], statements[0]
        changes["statements"] = tuple(statements)
    elif mutation == "duplicate":
        statements[-1] = statements[0]
        changes["statements"] = tuple(statements)
    else:
        field = {
            "source": "source_table",
            "destination": "destination_table",
            "sql": "sql",
            "schema_digest": "source_schema_digest",
        }[mutation]
        replacement = {
            "source_table": "ogilvy-trends-v2.trends_v2_dev.foreign",
            "destination_table": "ogilvy-trends-v2.trends_v2_staging.foreign",
            "sql": "CREATE SNAPSHOT TABLE `foreign` CLONE `foreign`",
            "source_schema_digest": "f" * 64,
        }[field]
        statements[0] = replace(statements[0], **{field: replacement})
        if mutation == "sql":
            statements[0] = replace(
                statements[0],
                sql_digest=subject.hashlib.sha256(replacement.encode()).hexdigest(),
            )
        changes["statements"] = tuple(statements)
    changed = reseal(plan, **changes)
    with pytest.raises(ValueError, match="snapshot_readback_invalid"):
        validate(changed, snapshot_metadata(plan), readback(plan))


@pytest.mark.parametrize(
    "mutation",
    [
        "type",
        "base",
        "time",
        "schema",
        "target",
        "expiration",
        "streaming",
        "missing_lane",
        "count",
        "duplicate_readback",
        "boolean_count",
    ],
)
def test_mismatched_existing_snapshot_and_partial_readback_refuse(mutation):
    plan = build_plan()
    metadata = snapshot_metadata(plan)
    rows = readback(plan)
    first = plan.statements[0].lane
    if mutation == "type":
        metadata[first]["type"] = "TABLE"
    elif mutation == "base":
        metadata[first]["snapshotDefinition"]["baseTableReference"]["tableId"] = "raw_content"
    elif mutation == "time":
        metadata[first]["snapshotDefinition"]["snapshotTime"] = (
            plan.source_as_of + timedelta(seconds=1)
        ).isoformat()
    elif mutation == "schema":
        metadata[first]["schema"]["fields"][0]["type"] = "BYTES"
    elif mutation == "target":
        metadata[first]["tableReference"]["tableId"] = "existing_foreign_table"
    elif mutation == "expiration":
        metadata[first]["expirationTime"] = "9999999999999"
    elif mutation == "streaming":
        metadata[first]["streamingBuffer"] = {"estimatedRows": "1"}
    elif mutation == "missing_lane":
        metadata.pop(first)
    elif mutation == "count":
        rows[0]["row_count"] = 1
    elif mutation == "duplicate_readback":
        rows[-1] = copy.deepcopy(rows[0])
    else:
        rows[0]["row_count"] = True
    with pytest.raises(ValueError, match="snapshot_readback_invalid"):
        validate(plan, metadata, rows)


def test_source_streaming_buffer_is_retained_as_limitation_not_capture_prohibition():
    values = source_metadata()
    values["trends_v2_dev.event_ledger"]["streamingBuffer"] = {"estimatedRows": "1"}
    checked = subject._validate_source_metadata(values)
    assert checked["event_ledger"]["streaming_buffer_present"] is True
    plan = build_plan()
    result = validate(plan, snapshot_metadata(plan), readback(plan))
    assert result["collection_complete"] is False
    assert "streaming_buffer_exclusion_unproven" in result["limitations"]


def test_source_metadata_required_fields_are_derived_not_trusted_from_summary():
    values = source_metadata()
    values["trends_v2_dev.event_ledger"]["missing_required_columns"] = []
    values["trends_v2_dev.event_ledger"]["schema"]["fields"] = [
        field
        for field in values["trends_v2_dev.event_ledger"]["schema"]["fields"]
        if field["name"] != "ledger_id"
    ]
    with pytest.raises(ValueError, match="source_metadata_invalid"):
        subject._validate_source_metadata(values)


def test_partial_source_metadata_refuses():
    values = source_metadata()
    values.pop("trends_v2_dev.raw_content")
    with pytest.raises(ValueError, match="source_metadata_invalid"):
        subject._validate_source_metadata(values)


def test_old_plan_readback_uses_supplied_reviewed_metadata_after_inventory_disappears(
    monkeypatch, tmp_path
):
    reviewed = source_metadata_bytes()
    plan = subject.build_snapshot_plan(CUTOFF, now=NOW, source_metadata=reviewed)
    observed_metadata = snapshot_metadata(plan)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError("unexpected filesystem read")),
    )
    result = validate(
        plan,
        observed_metadata,
        readback(plan),
        reviewed=reviewed,
    )
    assert result["plan_digest"] == plan.plan_digest


def test_old_plan_refuses_different_reviewed_metadata_even_when_structure_is_valid():
    reviewed = source_metadata_bytes()
    plan = subject.build_snapshot_plan(CUTOFF, now=NOW, source_metadata=reviewed)
    changed = json.loads(reviewed)
    changed["checked_at"] = "2030-01-03T00:00:00+00:00"
    changed = json.dumps(changed, sort_keys=True).encode()
    with pytest.raises(ValueError, match="snapshot_readback_invalid"):
        validate(
            plan,
            snapshot_metadata(plan),
            readback(plan),
            reviewed=changed,
        )


def native_canary_job():
    native = {
        "configuration": {
            "dryRun": False,
            "jobTimeoutMs": "600000",
            "jobType": "QUERY",
            "query": {
                "destinationTable": {
                    "datasetId": "trends_v2_staging",
                    "projectId": "ogilvy-trends-v2",
                    "tableId": "qa_oi_snapshot_native_20260908",
                },
                "priority": "INTERACTIVE",
                "query": "CREATE SNAPSHOT TABLE "
                "`ogilvy-trends-v2.trends_v2_staging.qa_oi_snapshot_native_20260908` "
                "CLONE `ogilvy-trends-v2.trends_v2_dev.event_ledger` "
                "FOR SYSTEM_TIME AS OF TIMESTAMP('2026-09-08 "
                "00:00:00+00') "
                "OPTIONS(expiration_timestamp=TIMESTAMP('2026-09-08T12:30:54.548689+00:00'))",
                "useLegacySql": False,
                "useQueryCache": False,
            },
        },
        "etag": "h2liZinV+KXwY6cCB1Bfvw==",
        "id": "ogilvy-trends-v2:US.oi_snapshot_native_canary_20260908",
        "jobCreationReason": {"code": "REQUESTED"},
        "jobReference": {
            "jobId": "oi_snapshot_native_canary_20260908",
            "location": "US",
            "projectId": "ogilvy-trends-v2",
        },
        "kind": "bigquery#job",
        "principal_subject": "serviceAccount:trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "selfLink": "https://bigquery.googleapis.com/bigquery/v2/projects/ogilvy-trends-v2/jobs/oi_snapshot_native_canary_20260908?location=US",
        "statistics": {
            "creationTime": "1788868854931",
            "endTime": "1788868859353",
            "query": {
                "billingTier": 0,
                "cacheHit": False,
                "ddlOperationPerformed": "CREATE",
                "ddlTargetTable": {
                    "datasetId": "trends_v2_staging",
                    "projectId": "ogilvy-trends-v2",
                    "tableId": "qa_oi_snapshot_native_20260908",
                },
                "referencedTables": [
                    {
                        "datasetId": "trends_v2_dev",
                        "projectId": "ogilvy-trends-v2",
                        "tableId": "event_ledger@1788825600000",
                    }
                ],
                "statementType": "CREATE_SNAPSHOT_TABLE",
                "totalBytesBilled": "0",
                "totalBytesProcessed": "0",
                "totalPartitionsProcessed": "0",
                "totalSlotMs": "0",
                "transferredBytes": "0",
            },
            "startTime": "1788868855366",
            "totalBytesProcessed": "0",
            "totalSlotMs": "0",
        },
        "status": {"state": "DONE"},
        "user_email": "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    }
    return native


def test_native_canary_destination_echo_matches_exact_expected_target():
    native = native_canary_job()
    original = copy.deepcopy(native)
    target = native["statistics"]["query"]["ddlTargetTable"]
    limits = {
        "job_id": native["jobReference"]["jobId"],
        "sql": native["configuration"]["query"]["query"],
        "identity": native["user_email"],
        "earliest": datetime.fromisoformat("2026-09-08T11:59:41+00:00"),
        "latest": datetime.fromisoformat("2026-09-08T12:02:00+00:00"),
        "target": target,
    }
    result = subject._creation_job(
        bigquery.QueryJob.from_api_repr(
            copy.deepcopy(native), SimpleNamespace(project=subject.PROJECT)
        ),
        native=native,
        **limits,
    )
    assert result == original
    native["configuration"]["query"]["destinationTable"] = {**target, "tableId": "foreign"}
    with pytest.raises(ValueError, match="snapshot_creation_job_invalid"):
        subject._creation_job(
            bigquery.QueryJob.from_api_repr(
                copy.deepcopy(native), SimpleNamespace(project=subject.PROJECT)
            ),
            native=native,
            **limits,
        )


def pending_creation_setup(
    monkeypatch, *, final_mutation=None, pending_mutation=None, forever=False
):
    http, args = creation_setup(monkeypatch)
    elapsed, sleeps, reads = [0.0], [], {}
    monkeypatch.setattr(subject, "monotonic", lambda: elapsed[0])

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    monkeypatch.setattr(subject, "sleep", sleep, raising=False)
    monkeypatch.setattr(
        bigquery.QueryJob,
        "result",
        lambda *args, **kwargs: pytest.fail("Creation must not use query-result polling"),
    )

    def native_shape(value):
        native = native_canary_job()
        native["jobReference"] = value["jobReference"]
        native["configuration"] = copy.deepcopy(value["configuration"])
        native["configuration"]["query"]["destinationTable"] = copy.deepcopy(
            value["statistics"]["query"]["ddlTargetTable"]
        )
        native["statistics"]["creationTime"] = value["statistics"]["creationTime"]
        native["statistics"]["query"].update(value["statistics"]["query"])
        if final_mutation:
            final_mutation(native)
        return native

    http.mutate_job = native_shape
    original_request = http.request

    def request(method, url, **kwargs):
        response = original_request(method, url, **kwargs)
        if method == "GET" and "/jobs/" in url and response.status_code == 200:
            name = urlparse(url).path.rsplit("/", 1)[1]
            reads[name] = reads.get(name, 0) + 1
            if forever or reads[name] <= 2:
                native = response.json()
                native["status"] = {"state": "PENDING" if reads[name] == 1 else "RUNNING"}
                native.pop("user_email")
                native["statistics"] = {}
                native["configuration"] = {"query": {}}
                if pending_mutation:
                    pending_mutation(native)
                response._content = json.dumps(native).encode()
        return response

    http.request = request
    return http, args, sleeps, reads


def test_incomplete_nonterminal_native_shape_waits_for_exact_final_job(monkeypatch):
    http, args, sleeps, reads = pending_creation_setup(monkeypatch)
    result = subject._execute_validated_snapshot_creations(**args)
    assert len(result["creation_records"]) == 5
    assert all(record["state"] == "succeeded" for record in result["creation_records"])
    assert list(reads.values()) == [3] * 5
    assert sleeps == [1] * 10
    assert sum(call[0] == "POST" for call in http.calls) == 5
    assert not any("/queries/" in call[1] for call in http.calls)


@pytest.mark.parametrize("mutation", ["identity", "sql", "time"])
def test_pending_poll_does_not_weaken_final_native_validation(monkeypatch, mutation):
    def mutate(native):
        if mutation == "identity":
            native["user_email"] = "foreign@example.invalid"
        elif mutation == "sql":
            native["configuration"]["query"]["query"] += "; SELECT 1"
        else:
            native["statistics"]["creationTime"] = "0"

    http, args, sleeps, _reads = pending_creation_setup(monkeypatch, final_mutation=mutate)
    with pytest.raises(subject.SnapshotCreationError, match="snapshot_creation_job_invalid"):
        subject._execute_validated_snapshot_creations(**args)
    assert sleeps == [1, 1]
    assert sum(call[0] == "POST" for call in http.calls) == 1


@pytest.mark.parametrize("mutation", ["job", "unknown_state"])
def test_pending_wrong_job_or_unknown_state_refuses_without_poll_or_repost(monkeypatch, mutation):
    def mutate(native):
        if mutation == "job":
            native["jobReference"]["jobId"] = "foreign"
        else:
            native["status"]["state"] = "UNKNOWN"

    http, args, sleeps, _reads = pending_creation_setup(monkeypatch, pending_mutation=mutate)
    with pytest.raises(subject.SnapshotCreationError):
        subject._execute_validated_snapshot_creations(**args)
    assert sleeps == []
    assert sum(call[0] == "POST" for call in http.calls) == 1


def test_pending_poll_stops_at_original_deadline_without_resubmission(monkeypatch):
    http, args, sleeps, reads = pending_creation_setup(monkeypatch, forever=True)
    args["now"] = args["consumption"].consumed_at + timedelta(seconds=599.75)
    with pytest.raises(subject.SnapshotCreationError, match="snapshot_creation_expired"):
        subject._execute_validated_snapshot_creations(**args)
    assert sleeps == [0.25]
    assert list(reads.values()) == [1]
    assert sum(call[0] == "POST" for call in http.calls) == 1


def metadata_settling_setup(monkeypatch, mutation, *, forever=False):
    http, args = creation_setup(monkeypatch)
    elapsed, sleeps, reads = [0.0], [], [0]
    monkeypatch.setattr(subject, "monotonic", lambda: elapsed[0])

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    monkeypatch.setattr(subject, "sleep", sleep)
    real_metadata = retained_event_snapshot_metadata()
    real_metadata["creationTime"] = str(
        int((SOURCE_SNAPSHOT_NOW + timedelta(seconds=2)).timestamp() * 1000)
    )

    def mutate(value):
        if not value["tableReference"]["tableId"].endswith("_event_ledger"):
            return value
        reads[0] += 1
        value = copy.deepcopy(real_metadata)
        if mutation == "clock":
            value["creationTime"] = str(
                int((SOURCE_SNAPSHOT_NOW + timedelta(seconds=5)).timestamp() * 1000)
            )
        elif forever or reads[0] <= 2:
            if mutation == "incomplete":
                value.pop("numRows")
            else:
                value["snapshotDefinition"]["baseTableReference"]["tableId"] = "foreign"
        return value

    http.mutate_table = mutate
    return http, args, sleeps, reads


@pytest.mark.parametrize("mutation", ["incomplete", "contradictory", "clock"])
def test_snapshot_metadata_waits_for_fully_valid_retained_shape(monkeypatch, mutation):
    http, args, sleeps, reads = metadata_settling_setup(monkeypatch, mutation)
    warnings = []
    args["metadata_diagnostics"] = lambda lane, reason: warnings.append((lane, reason))
    result = subject._execute_validated_snapshot_creations(**args)
    assert reads == [3]
    assert sleeps == [1, 1]
    assert len(result["creation_records"]) == 5
    assert sum(call[0] == "POST" for call in http.calls) == 5
    assert result["snapshot_metadata"]["event_ledger"]["numRows"] == "23414"
    assert warnings == [
        (
            "event_ledger",
            {
                "incomplete": "row_count",
                "contradictory": "snapshot_definition",
                "clock": "creation_time",
            }[mutation],
        )
    ]


@pytest.mark.parametrize(
    "mutation,reason", [("incomplete", "row_count"), ("contradictory", "snapshot_definition")]
)
def test_permanent_metadata_failure_stops_after_thirty_seconds_with_safe_predicate(
    monkeypatch, mutation, reason
):
    http, args, sleeps, reads = metadata_settling_setup(monkeypatch, mutation, forever=True)
    with pytest.raises(
        subject.SnapshotCreationError, match="snapshot_creation_table_invalid"
    ) as caught:
        subject._execute_validated_snapshot_creations(**args)
    assert sum(sleeps) == 30
    assert 1 <= reads[0] <= 31
    assert reason in caught.value.failed_predicates
    assert sum(call[0] == "POST" for call in http.calls) == 1


def test_metadata_settling_never_extends_original_execution_deadline(monkeypatch):
    http, args, sleeps, _reads = metadata_settling_setup(monkeypatch, "incomplete", forever=True)
    args["now"] = args["consumption"].consumed_at + timedelta(seconds=599.25)
    with pytest.raises(subject.SnapshotCreationError, match="snapshot_creation_expired"):
        subject._execute_validated_snapshot_creations(**args)
    assert sleeps == [0.75]
    assert sum(call[0] == "POST" for call in http.calls) == 1


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("row_count", "snapshot_creation_metadata_row_count"),
        ("private source value", "snapshot_creation_table_invalid"),
    ],
)
def test_creation_diagnostic_emits_only_allowlisted_metadata_predicate(
    monkeypatch, reason, expected
):
    from tests.unit.test_protected_snapshot_integration import joined, operation_fixture

    _http, _storage, args, _view = joined(monkeypatch)
    events = []

    def refuse(*args, **kwargs):
        error = subject.SnapshotCreationError("snapshot_creation_table_invalid", [], [], {}, [])
        error.failed_predicates = (reason,)
        raise error

    monkeypatch.setattr(subject, "_execute_validated_snapshot_creations", refuse)
    payload, status = operation_fixture.subject()._execute_validated_operation(
        **args, diagnostics=events.append
    )
    assert status == "failed"
    assert payload["missing_checks"] == ["snapshot_creation_incomplete"]
    assert events[-1]["code"] == expected
    assert "private source value" not in json.dumps(events)


def retained_event_snapshot_metadata():
    return {
        "creationTime": "1788874245845",
        "description": "Event-state ledger: current resolved state per watched entity per market per "
        "day, from today factual rows alone.",
        "etag": "hEMax27YmISsDSoSjBg63g==",
        "expirationTime": "1796601600000",
        "id": "ogilvy-trends-v2:trends_v2_staging.open_intelligence_v3_source_20260907_event_ledger",
        "kind": "bigquery#table",
        "lastModifiedTime": "1788874245845",
        "location": "US",
        "numActiveLogicalBytes": "7971816",
        "numActivePhysicalBytes": "2628717",
        "numBytes": "7971816",
        "numCurrentPhysicalBytes": "2628717",
        "numLongTermBytes": "0",
        "numLongTermLogicalBytes": "0",
        "numLongTermPhysicalBytes": "0",
        "numPartitions": "70",
        "numRows": "23414",
        "numTimeTravelPhysicalBytes": "0",
        "numTotalLogicalBytes": "7971816",
        "numTotalPhysicalBytes": "2628717",
        "partitionDefinition": {"partitionedColumn": [{"field": "trend_date"}]},
        "schema": {
            "fields": [
                {"name": "ledger_id", "type": "STRING"},
                {"name": "trend_date", "type": "DATE"},
                {"name": "market", "type": "STRING"},
                {"name": "entity_key", "type": "STRING"},
                {"mode": "REPEATED", "name": "entity_aliases", "type": "STRING"},
                {"name": "event_kind", "type": "STRING"},
                {"name": "state_label", "type": "STRING"},
                {"name": "state_text", "type": "STRING"},
                {"name": "as_of", "type": "TIMESTAMP"},
                {"name": "evidence_quote", "type": "STRING"},
                {"mode": "REPEATED", "name": "corroborating_sources", "type": "STRING"},
                {"name": "source_count", "type": "INTEGER"},
                {"name": "confidence", "type": "FLOAT"},
                {"name": "resolved_by", "type": "STRING"},
                {"name": "gemini_model", "type": "STRING"},
                {
                    "defaultValueExpression": "CURRENT_TIMESTAMP()",
                    "name": "generated_at",
                    "type": "TIMESTAMP",
                },
            ]
        },
        "selfLink": "https://bigquery.googleapis.com/bigquery/v2/projects/ogilvy-trends-v2/datasets/trends_v2_staging/tables/open_intelligence_v3_source_20260907_event_ledger",
        "snapshotDefinition": {
            "baseTableReference": {
                "datasetId": "trends_v2_dev",
                "projectId": "ogilvy-trends-v2",
                "tableId": "event_ledger",
            },
            "snapshotTime": "2026-09-08T00:00:00Z",
        },
        "tableReference": {
            "datasetId": "trends_v2_staging",
            "projectId": "ogilvy-trends-v2",
            "tableId": "open_intelligence_v3_source_20260907_event_ledger",
        },
        "timePartitioning": {"field": "trend_date", "type": "DAY"},
        "type": "SNAPSHOT",
    }


def entry_arguments(args):
    return {
        key: value for key, value in args.items() if key not in {"manifest", "recheck", "claim"}
    }


def test_public_entry_refuses_a_foreign_authority_object_before_any_io(monkeypatch):
    """The guarded public entry keeps its guard: an object that is not an issued authority
    refuses at _require_consumed_execution before any closure is built or any IO happens."""
    http, args = creation_setup(monkeypatch)
    with pytest.raises(execution_approval.ApprovalRefusal) as refused:
        subject.execute_protected_snapshot_creations(**entry_arguments(args), authority=object())
    assert str(refused.value) == "execution_approval_identity_invalid"
    assert http.calls == []
    assert args["claim"].calls == []


def test_public_entry_refuses_an_issued_authority_of_another_operation_before_any_io(monkeypatch):
    """A real issued and consumed v2 authority for another operation reaches the guard and
    refuses execution_approval_identity_invalid because the issued operation differs from
    source_snapshot_capture; the retained consumption never lets it past, and no IO happens."""
    http, args = creation_setup(monkeypatch)
    foreign, _foreign_consumption = _consumed_authority("brain_read")
    with pytest.raises(
        execution_approval.ApprovalRefusal, match=r"^execution_approval_identity_invalid$"
    ):
        subject.execute_protected_snapshot_creations(**entry_arguments(args), authority=foreign)
    assert http.calls == []
    assert args["claim"].calls == []


def test_recheck_closure_runs_before_the_first_post_of_each_lane(monkeypatch):
    http, args = creation_setup(monkeypatch)
    recheck = args["recheck"]
    posts_at_recheck = []

    def spy():
        posts_at_recheck.append(sum(call[0] == "POST" for call in http.calls))
        recheck()

    args["recheck"] = spy
    result = subject._execute_validated_snapshot_creations(**args)
    assert posts_at_recheck == [0, 1, 2, 3, 4]
    assert sum(call[0] == "POST" for call in http.calls) == 5
    assert recheck.calls == ["claim"] + ["recheck"] * 5
    assert len(result["creation_records"]) == 5


def test_claim_closure_runs_once_after_validation_and_before_the_first_io(monkeypatch):
    http, initial_args = creation_setup(monkeypatch)
    http.lose_ack, http.missing = True, True
    with pytest.raises(subject.SnapshotCreationError) as partial:
        subject._execute_validated_snapshot_creations(**initial_args)
    initial, context = initial_creation_result(initial_args, partial.value.creation_records)
    reads = []
    recovered_http, args = creation_setup(
        monkeypatch,
        recovery=context,
        reader=lambda requested: reads.append(requested) or asdict(initial),
    )
    args["now"] = SOURCE_SNAPSHOT_NOW + timedelta(seconds=5)
    recovered_http.jobs = copy.deepcopy(http.jobs)
    claim = args["claim"]
    order = []

    def spy():
        order.append(("claim", len(reads), len(recovered_http.calls)))
        claim()

    args["claim"] = spy
    result = subject._execute_validated_snapshot_creations(**args)
    assert order == [("claim", 1, 0)]
    assert claim.calls.count("claim") == 1
    assert claim.views == [claim.views[0]]
    assert claim.views[0].consumption is args["consumption"]
    assert len(recovered_http.calls) > 0
    assert all(row["state"] == "succeeded" for row in result["creation_records"])


@pytest.mark.parametrize("mutation", ["context", "recovery", "client"])
def test_claim_closure_is_not_reached_when_validation_refuses(monkeypatch, mutation):
    if mutation == "recovery":
        context = {
            "contract_version": "open_intelligence_source_capture_recovery_v1",
            "initial_manifest_sha256": "a" * 64,
            "initial_consumption_id": "exc_" + "b" * 64,
            "initial_execution_name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/executions/source-test-1",
            "initial_result_id": "exr_" + "c" * 64,
            "initial_result_digest": "d" * 64,
        }
        http, args = creation_setup(monkeypatch, recovery=context, reader=lambda requested: {})
        code = "snapshot_creation_recovery_invalid"
    else:
        http, args = creation_setup(monkeypatch)
        if mutation == "context":
            args["capture_plan"]["client_scope_id"] = "foreign"
            code = "snapshot_creation_context_invalid"
        else:
            args["client"] = SimpleNamespace(project=subject.PROJECT)
            code = "snapshot_creation_client_invalid"
    with pytest.raises(ValueError, match=f"^{code}$"):
        subject._execute_validated_snapshot_creations(**args)
    assert args["claim"].calls == []
    assert http.calls == []


def test_raising_claim_closure_stops_before_any_io(monkeypatch):
    http, args = creation_setup(monkeypatch)

    class Claimed(Exception):
        pass

    def claim():
        raise Claimed("snapshot_creation_already_attempted")

    args["claim"] = claim
    with pytest.raises(Claimed):
        subject._execute_validated_snapshot_creations(**args)
    assert http.calls == []
    assert args["recheck"].calls == []


@pytest.mark.parametrize("closure", ["recheck", "claim"])
def test_validated_body_requires_both_closures_before_any_io(monkeypatch, closure):
    http, args = creation_setup(monkeypatch)
    args[closure] = None
    with pytest.raises(ValueError, match=r"^snapshot_creation_context_invalid$"):
        subject._execute_validated_snapshot_creations(**args)
    assert http.calls == []


def test_public_creation_entries_guard_before_io_and_the_validated_body_is_private():
    """Source level audit in the idiom of the retained reader mode audit. Every public
    creation entry calls _require_consumed_execution on the live authority before any
    statement that can reach IO, builds the recheck and claim closures over that authority
    and hands only the manifest and the consumption to the validated body. The body is
    private, names no authority and no invocation registry, calls claim once before its
    guarded transport block and recheck once inside the lane loop, and is referenced
    outside this module only by the capture runner's validated operation body, which
    is itself reachable only through the runner's guarded entries."""
    module_path = Path(subject.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    body_name = "_execute_validated_snapshot_creations"

    def call_name(node):
        target = node.func
        return target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")

    def calls(node):
        return [call_name(item) for item in ast.walk(node) if isinstance(item, ast.Call)]

    entries = [
        name
        for name, function in functions.items()
        if name != body_name and body_name in calls(function)
    ]
    assert entries == ["execute_protected_snapshot_creations"]
    for name in entries:
        assert not name.startswith("_")
        assert name in subject.__all__
        function = functions[name]
        ordered = [
            call_name(item)
            for statement in function.body
            if not isinstance(statement, ast.FunctionDef)
            for item in ast.walk(statement)
            if isinstance(item, ast.Call)
        ]
        guard = ordered.index("_require_consumed_execution")
        assert set(ordered[:guard]) <= {"_require_retained_consumption", "retained_origin_registry"}
        assert ordered.index(body_name) > guard
        closures = {node.name: node for node in function.body if isinstance(node, ast.FunctionDef)}
        assert set(closures) == {"recheck", "claim"}
        assert calls(closures["recheck"]) == ["_require_consumed_execution"]
        claim_names = {
            node.id for node in ast.walk(closures["claim"]) if isinstance(node, ast.Name)
        }
        assert {"authority", "_CREATION_INVOCATIONS", "_CREATION_LOCK"} <= claim_names
        body_call = next(
            item
            for item in ast.walk(function)
            if isinstance(item, ast.Call) and call_name(item) == body_name
        )
        keywords = {keyword.arg: keyword.value for keyword in body_call.keywords}
        assert "authority" not in keywords
        assert {"manifest", "consumption", "recheck", "claim"} <= set(keywords)
        assert isinstance(keywords["manifest"], ast.Attribute)
        assert keywords["manifest"].value.id == "authority"
        assert keywords["manifest"].attr == "manifest"
        assert all(
            isinstance(keywords[key], ast.Name) and keywords[key].id == key
            for key in ("consumption", "recheck", "claim")
        )
    body = functions[body_name]
    assert body_name not in subject.__all__
    parameters = [arg.arg for arg in body.args.args + body.args.kwonlyargs]
    assert "authority" not in parameters
    assert {"manifest", "consumption", "recheck", "claim"} <= set(parameters)
    names = {node.id for node in ast.walk(body) if isinstance(node, ast.Name)}
    assert not {"authority", "_CREATION_INVOCATIONS", "_CREATION_LOCK"} & names
    body_calls = calls(body)
    assert "_require_consumed_execution" not in body_calls
    assert body_calls.count("claim") == 1
    assert body_calls.count("recheck") == 1
    claim_index = next(
        index
        for index, statement in enumerate(body.body)
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and call_name(statement.value) == "claim"
    )
    try_index = next(
        index for index, statement in enumerate(body.body) if isinstance(statement, ast.Try)
    )
    assert claim_index < try_index
    assert "recheck" in calls(body.body[try_index])
    engine_root = module_path.parents[3]
    referencing = sorted(
        path.relative_to(engine_root).as_posix()
        for folder in ("src", "scripts")
        for path in (engine_root / folder).rglob("*.py")
        if body_name in path.read_text(encoding="utf-8")
    )
    assert referencing == [
        "scripts/staging/capture_protected_production_snapshot.py",
        "src/analysis/open_intelligence/production_snapshot_tables.py",
    ]
    runner = ast.parse((engine_root / referencing[0]).read_text(encoding="utf-8"))
    runner_functions = {
        node.name: node for node in runner.body if isinstance(node, ast.FunctionDef)
    }
    assert [name for name, node in runner_functions.items() if body_name in calls(node)] == [
        "_validated_operation"
    ]
    assert "authority" not in {
        node.id
        for node in ast.walk(runner_functions["_validated_operation"])
        if isinstance(node, ast.Name)
    }


# D03 capture plan v2 and result v2 contracts. The grant bound routine pins the plan's
# top level key set and the snapshot plan fields it reads; these tests pin the Python
# side to the same names so the two cannot drift apart silently.


def _v2_grant(**overrides):
    grant = {
        "grant_id": "source_capture_grant_2026_09_v2",
        "environment": "staging",
        "source_estate_digest": "7" * 64,
        "contract_sha256": "8" * 64,
        "valid_from": datetime(2026, 9, 1, tzinfo=UTC),
        "valid_until": datetime(2026, 10, 1, tzinfo=UTC),
        "allowed_cutoffs": ["2026-09-12", "2026-09-13"],
        "reserved_micro_usd_per_capture": 750000,
        "cumulative_ceiling_micro_usd": 1500000,
        "revocation_state": "active",
    }
    grant.update(overrides)
    return grant


def _v2_plan(**overrides):
    from tests.unit import test_staging_source_profile as profiles

    arguments = {
        "grant": _v2_grant(),
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "now": profiles.NOW,
    }
    arguments.update(overrides)
    profile = arguments.pop("profile", None)
    if profile is None:
        profile = profiles.profile()
    return subject.build_capture_plan_v2(profile, **arguments)


def test_capture_plan_v2_carries_exactly_the_fields_the_routine_reads():
    from src.analysis.open_intelligence import staging_source_profile as policy

    from tests.unit import test_staging_source_profile as profiles

    plan = _v2_plan()
    assert set(plan) == subject.CAPTURE_PLAN_FIELDS
    assert plan["contract_version"] == subject.CAPTURE_PLAN_V2_VERSION
    assert plan["contract_version"] == "open_intelligence_protected_capture_plan_v2"
    assert plan["cutoff_date"] == profiles.CUTOFF.isoformat()
    snapshot = plan["snapshot_plan"]
    assert set(snapshot) == subject.CAPTURE_PLAN_V2_SNAPSHOT_FIELDS
    assert snapshot["profile_version"] == policy.NATIVE_IDENTITY_PROFILE_VERSION
    assert snapshot["projection_version"] == policy.NATIVE_IDENTITY_PROJECTION_VERSION
    assert snapshot["source_dataset"] == policy.STAGING_SOURCE_DATASET
    assert snapshot["observation_window_end"] == profiles.WINDOW_END.isoformat()
    assert snapshot["snapshot_as_of"] == profiles.SNAPSHOT.isoformat()
    assert snapshot["source_estate_digest"] == "7" * 64
    assert snapshot["grant_id"] == "source_capture_grant_2026_09_v2"
    assert snapshot["schema_digest"] == "2" * 64
    assert [item["lane"] for item in snapshot["statements"]] == list(policy.SOURCE_LANES)
    assert [item["lane"] for item in plan["creation_statements"]] == list(policy.SOURCE_LANES)
    first = snapshot["statements"][0]
    dataset = policy.STAGING_SOURCE_DATASET
    assert first["source_table"] == f"ogilvy-trends-v2.{dataset}.enriched_content"
    assert first["destination_table"] == (
        f"ogilvy-trends-v2.{dataset}.staging_source_20260912_enriched_content"
    )
    stamp = "2026-09-13 01:05:00.000000+00"
    assert f"FOR SYSTEM_TIME AS OF TIMESTAMP '{stamp}'" in first["sql"]
    assert plan["creation_statements"][0]["sql"].startswith(first["sql"])
    expiry = "2026-12-12 01:05:00.000000+00"
    assert f"expiration_timestamp = TIMESTAMP '{expiry}'" in plan["creation_statements"][0]["sql"]
    canonical_bytes(plan)
    validated = subject.validate_capture_plan_v2(
        plan, cutoff=profiles.CUTOFF, grant=_v2_grant(), now=profiles.NOW
    )
    assert validated == plan


def test_capture_plan_v2_refuses_each_routine_assertion_in_python():
    from tests.unit import test_staging_source_profile as profiles

    plan = _v2_plan()
    validate = subject.validate_capture_plan_v2
    arguments = {"cutoff": profiles.CUTOFF, "grant": _v2_grant(), "now": profiles.NOW}
    with pytest.raises(ValueError, match="source_snapshot_cutoff_not_permitted"):
        validate(plan, **{**arguments, "grant": _v2_grant(allowed_cutoffs=["2026-09-13"])})
    with pytest.raises(ValueError, match="source_snapshot_cutoff_not_permitted"):
        _v2_plan(grant=_v2_grant(allowed_cutoffs=["2026-09-13"]))
    with pytest.raises(ValueError, match="source_snapshot_estate_mismatch"):
        validate(plan, **{**arguments, "grant": _v2_grant(source_estate_digest="9" * 64)})
    with pytest.raises(ValueError, match="source_snapshot_estate_mismatch"):
        validate(plan, **{**arguments, "grant": _v2_grant(grant_id="another_grant")})
    with pytest.raises(ValueError, match="source_snapshot_plan_invalid"):
        validate(plan, **{**arguments, "cutoff": profiles.CUTOFF + timedelta(days=1)})
    with pytest.raises(ValueError, match="source_snapshot_plan_invalid"):
        validate(plan, **{**arguments, "now": profiles.SNAPSHOT - timedelta(seconds=1)})
    for field, value in (
        ("contract_version", "open_intelligence_protected_capture_plan_v1"),
        ("cutoff_date", "2026-09-13"),
        ("market_scope", ["za", "ke"]),
        ("client_scope_id", " "),
        ("creation_statements", plan["creation_statements"][:4]),
    ):
        with pytest.raises(ValueError, match="source_snapshot_plan_invalid"):
            validate({**plan, field: value}, **arguments)
    with pytest.raises(ValueError, match="source_snapshot_plan_invalid"):
        validate({**plan, "extra": 1}, **arguments)
    for field, value in (
        ("profile_version", "42_capture_v1"),
        ("projection_version", "v3_absent_nullable_v1"),
        ("source_dataset", "trends_v2_dev"),
        ("snapshot_dataset", "trends_v2_staging"),
        ("observation_window_end", "2026-09-12T00:00:00+00:00"),
        ("snapshot_as_of", "2026-09-12T23:59:59+00:00"),
        ("snapshot_as_of", "2026-09-13T01:05:00"),
        ("schema_digest", "2" * 63),
        ("profile_id", "staging-2026-09-13-1111111111111111"),
        ("statements", plan["snapshot_plan"]["statements"][::-1]),
    ):
        mutated = {**plan, "snapshot_plan": {**plan["snapshot_plan"], field: value}}
        with pytest.raises(ValueError, match="source_snapshot_plan_invalid"):
            validate(mutated, **arguments)
    with pytest.raises(ValueError, match="source_snapshot_plan_invalid"):
        validate(canonical_bytes(plan), **arguments)
    with pytest.raises(ValueError, match="source_snapshot_plan_invalid"):
        _v2_plan(now=profiles.SNAPSHOT - timedelta(seconds=1))
    with pytest.raises(ValueError, match="source_snapshot_plan_invalid"):
        _v2_plan(profile=object())


def _v2_result(**overrides):
    from tests.unit import test_staging_source_profile as profiles

    plan = _v2_plan()
    payload = {
        "contract_version": "open_intelligence_protected_source_snapshot_v2",
        "cutoff_date": profiles.CUTOFF.isoformat(),
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "profile_id": plan["snapshot_plan"]["profile_id"],
        "profile_version": "42_staging_source_v2",
        "projection_version": "native_id_bound_v1",
        "source_dataset": "intelligence_42_sources_staging",
        "observation_window_end": profiles.WINDOW_END.isoformat(),
        "snapshot_as_of": profiles.SNAPSHOT.isoformat(),
        "source_estate_digest": "7" * 64,
        "grant_id": "source_capture_grant_2026_09_v2",
        "captured_at": None,
        "snapshot_plan_digest": subject.canonical_digest(plan),
        "snapshot_digest": None,
        "capture_receipt_digest": None,
        "creation_records": [],
        "artifact_attempt": None,
        "stored_artifact": None,
        "query_count": 0,
        "total_bytes_billed": 0,
        "limitations": ["upstream_collection_completeness_unproven"],
        "missing_checks": [],
    }
    payload.update(overrides)
    return payload


def test_source_snapshot_result_v2_is_a_distinct_contract_with_its_own_key_set():
    from tests.unit import test_staging_source_profile as profiles

    payload = _v2_result()
    validate = subject.validate_source_snapshot_result_v2
    assert set(payload) == subject.SOURCE_SNAPSHOT_RESULT_V2_FIELDS
    assert payload["contract_version"] == subject.SOURCE_SNAPSHOT_RESULT_V2_VERSION
    assert "source_as_of" not in subject.SOURCE_SNAPSHOT_RESULT_V2_FIELDS
    assert validate(payload, cutoff=profiles.CUTOFF) == payload
    captured = _v2_result(
        captured_at=profiles.NOW.isoformat(),
        snapshot_digest="a" * 64,
        capture_receipt_digest="b" * 64,
        query_count=5,
        total_bytes_billed=1000,
    )
    assert validate(captured, cutoff=profiles.CUTOFF) == captured
    for field, value in (
        ("contract_version", "open_intelligence_protected_source_snapshot_v1"),
        ("cutoff_date", "2026-09-13"),
        ("observation_window_end", "2026-09-12T00:00:00+00:00"),
        ("snapshot_as_of", "2026-09-12T23:00:00+00:00"),
        ("query_count", 6),
        ("query_count", "0"),
        ("total_bytes_billed", 1_000_000_001),
        ("snapshot_digest", "a" * 64),
        ("captured_at", profiles.NOW.isoformat()),
        ("creation_records", [{}] * 6),
        ("limitations", "text"),
        ("market_scope", ["za", "ke"]),
        ("grant_id", "Bad Grant"),
        ("source_estate_digest", "7" * 63),
        ("profile_version", "42_capture_v1"),
        ("source_dataset", "trends_v2_dev"),
    ):
        with pytest.raises(ValueError, match="source_snapshot_result_invalid"):
            validate(_v2_result(**{field: value}), cutoff=profiles.CUTOFF)
    with pytest.raises(ValueError, match="source_snapshot_result_invalid"):
        validate({**payload, "source_as_of": None}, cutoff=profiles.CUTOFF)
    with pytest.raises(ValueError, match="source_snapshot_result_invalid"):
        validate(
            _v2_result(captured_at=profiles.NOW.isoformat(), snapshot_digest="a" * 64),
            cutoff=profiles.CUTOFF,
        )


def test_v1_readers_keep_their_exact_key_set_beside_the_v2_contract():
    from scripts.staging import capture_protected_production_snapshot as operator

    assert {
        "contract_version",
        "cutoff_date",
        "client_scope_id",
        "market_scope",
        "source_as_of",
        "captured_at",
        "snapshot_plan_digest",
        "snapshot_digest",
        "capture_receipt_digest",
        "creation_records",
        "artifact_attempt",
        "stored_artifact",
        "query_count",
        "total_bytes_billed",
        "limitations",
        "missing_checks",
    } == operator._COMPACT_RESULT_FIELDS
    assert {
        "profile_id",
        "profile_version",
        "projection_version",
        "source_dataset",
        "observation_window_end",
        "snapshot_as_of",
        "source_estate_digest",
        "grant_id",
    } == subject.SOURCE_SNAPSHOT_RESULT_V2_FIELDS - operator._COMPACT_RESULT_FIELDS
    assert {
        "source_as_of"
    } == operator._COMPACT_RESULT_FIELDS - subject.SOURCE_SNAPSHOT_RESULT_V2_FIELDS


def test_the_shared_snapshot_naming_reads_a_closed_day_and_a_lane_name():
    """The one naming the plan and every guard over it read, checked at its own door."""
    cutoff = date(2026, 9, 11)
    assert (
        subject.snapshot_destination_table(cutoff, "raw_content")
        == "open_intelligence_v3_source_20260911_raw_content"
    )
    assert subject.snapshot_destination(cutoff, "raw_content") == (
        "ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260911_raw_content"
    )
    for bad_cutoff, bad_lane in (
        (datetime(2026, 9, 11, tzinfo=UTC), "raw_content"),
        ("2026-09-11", "raw_content"),
        (cutoff, 7),
        (cutoff, None),
    ):
        with pytest.raises(ValueError, match="snapshot_destination_invalid"):
            subject.snapshot_destination_table(bad_cutoff, bad_lane)
