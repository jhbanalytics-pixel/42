import copy
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import general_question_snapshot as snapshot
from src.analysis.open_intelligence.production_snapshot_tables import retained_origin_registry


def completed_recovery(monkeypatch):
    import hashlib
    from dataclasses import asdict
    from datetime import timedelta

    from tests.unit import test_protected_snapshot_replay_integration as protected
    from tests.unit.test_open_intelligence_execution_approval_runtime import (
        retained_capture_view,
    )

    f = protected.capture_fixture
    http, storage_http, args, view = f.joined(monkeypatch, lose_ack=True, hide_readback=True)
    operation = f.operation_fixture.subject()
    initial_payload, status = operation._execute_validated_operation(**args)
    assert status == "failed"
    assert initial_payload["missing_checks"] == ["snapshot_artifact_unavailable"]
    rows = {}

    def record(view, payload, status):
        # The result row is the v1 record a historical capture left, built directly from
        # the payload the validated body returned (retained view, successor 1).
        result = view.result_record(
            payload,
            status=status,
            completed_at=f.OBJECT_CREATED + timedelta(seconds=1),
            reference=view.consumption.execution_name + "#source-snapshot",
        )
        rows[result.consumption_id] = {
            "result": asdict(result),
            "approval": asdict(view.approval),
            "consumption": asdict(view.consumption),
        }
        return result

    original = record(view, initial_payload, status)
    recovery = {
        "contract_version": "open_intelligence_source_capture_recovery_v1",
        **{
            "initial_" + key: getattr(original, key)
            for key in (
                "manifest_sha256",
                "consumption_id",
                "execution_name",
                "result_id",
                "result_digest",
            )
        },
    }
    artifacts = {**args["artifacts"], "recovery_context": snapshot.canonical_bytes(recovery)}
    recovered = retained_capture_view(artifacts=artifacts, mode="recover")
    recheck, claim = f.creation_fixture.retained_closures(recovered)
    storage_http.hide_readback = False
    recovered_args = {
        **args,
        "artifacts": artifacts,
        "manifest": recovered.manifest,
        "consumption": recovered.consumption,
        "recheck": recheck,
        "claim": claim,
        "mode": "recover",
        "now": f.OPERATION_NOW + timedelta(seconds=5),
        "initial_result_reader": lambda _: asdict(original),
    }
    before = (len(http.creation.calls), len(http.capture.calls))
    events = []
    payload, status = operation._execute_validated_operation(
        **recovered_args, diagnostics=events.append
    )
    assert status == "succeeded", (payload["missing_checks"], events)
    assert len(http.capture.calls) == before[1]
    assert payload["stored_artifact"]["generation"] == 1
    result = record(recovered, payload, status)
    for name, raw in artifacts.items():
        storage_http.seed(f"inputs/{hashlib.sha256(raw).hexdigest()}/{name}.json", raw)
    ledger = SimpleNamespace(
        result=asdict(result),
        approval=asdict(recovered.approval),
        consumption=recovered.consumption,
        rows=rows,
        original=rows[original.consumption_id],
    )
    inputs = {
        key: getattr(result, key)
        for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
    }
    inputs["objects"] = args["objects"]
    return http, storage_http, ledger, inputs


def completed_v4_context(monkeypatch):
    import json
    from dataclasses import asdict
    from datetime import timedelta

    from src.analysis.open_intelligence import execution_approval as authority

    from tests.unit import test_protected_snapshot_replay_integration as protected
    from tests.unit.test_open_intelligence_execution_approval_runtime import SOURCE_SNAPSHOT_NOW

    reader, kwargs, _storage, result, _payload, state = protected.completed_replacement_capture(
        monkeypatch, failed_index=0, continuation=4
    )
    completed = reader(**kwargs)
    recovery = json.loads(state["artifacts"]["recovery_context"])
    contexts = [
        recovery,
        recovery["ancestor_recovery_context"],
        recovery["ancestor_recovery_context"]["ancestor_recovery_context"],
    ]
    rows = {}
    for identifier in [
        result["consumption_id"],
        *(context["initial_consumption_id"] for context in contexts),
    ]:
        row = kwargs["result_reader"](identifier)[0]
        approved = kwargs["approval_reader"](row["manifest_sha256"])
        manifest = authority.validate_execution_manifest(
            json.loads(approved.canonical_manifest_json),
            mode="historical_replay",
            registry=retained_origin_registry(),
        )
        consumed = authority.ExecutionConsumption(
            consumption_contract_version="open_intelligence_execution_consumption_v1",
            consumption_id=identifier,
            approval_id=row["approval_id"],
            manifest_sha256=row["manifest_sha256"],
            operation="source_snapshot_capture",
            execution_name=row["execution_name"],
            job_resource=manifest.job_resource,
            source_sha=manifest.source_sha,
            image_uri=manifest.image_uri,
            consumed_at=SOURCE_SNAPSHOT_NOW + timedelta(seconds=1),
        )
        rows[identifier] = {"result": row, "approval": asdict(approved), "consumption": consumed}
    metadata = completed["capture"]["assembly"]["snapshot_metadata"]

    class CaptureHTTP:
        def request(self, method, url, **kwargs):
            import json

            lane = next(lane for lane in metadata if url.split("?", 1)[0].endswith("_" + lane))
            return protected.storage_fixture.HTTP.response(
                method, url, 200, json.dumps(metadata[lane]).encode()
            )

    ledger = SimpleNamespace(
        result=result,
        approval=rows[result["consumption_id"]]["approval"],
        consumption=state["consumption"],
        rows=rows,
        original=rows[contexts[0]["initial_consumption_id"]],
    )
    return (
        CaptureHTTP(),
        None,
        ledger,
        {
            **{
                key: result[key]
                for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
            },
            "objects": kwargs["objects"],
        },
    )


@pytest.mark.parametrize("requirement_kind", ["content", "aggregate"])
@pytest.mark.parametrize("direct_content", [False, "tail", "title"])
@pytest.mark.parametrize("version", [1, 4])
@pytest.mark.parametrize("raw_fallback", [False, True])
def test_recovered_capture_owned_queries_admission_and_restore_join(
    monkeypatch, version, raw_fallback, direct_content, requirement_kind
):
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime

    from google.cloud import bigquery
    from src.analysis.open_intelligence import execution_approval as approval
    from src.analysis.open_intelligence import general_question_context_admission as admission
    from src.analysis.open_intelligence.general_question_queries import GeneralQuestionQueries

    from tests.unit import test_general_question_context_queries as wires
    from tests.unit import test_general_question_query_execution as native
    from tests.unit import test_protected_snapshot_replay_integration as protected

    capture_http, _, ledger, protected_inputs = (
        completed_v4_context if version == 4 else completed_recovery
    )(monkeypatch)
    monkeypatch.setattr(bigquery, "Client", protected._REAL_CLIENT)
    monkeypatch.setattr(native.f, "NOW", datetime(2026, 9, 9, 12, 1, tzinfo=UTC))
    original_draft = native.runtime.plan_draft()
    original_draft["requirements"][0]["kind"] = requirement_kind
    original_draft["window"] = {"start": "2026-09-01", "end": "2026-09-07", "closed": True}
    monkeypatch.setattr(native.runtime, "plan_draft", lambda: copy.deepcopy(original_draft))
    values = native.fixture()
    store, bucket, invocation, identity, credentials, plan = values
    pin = {
        key: protected_inputs[key]
        for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
    }
    pin.update(profile_id="protected_context_20260907_v1", cutoff_date="2026-09-07")
    monkeypatch.setattr(admission, "_PROTECTED_CONTEXT_PROFILES", (pin,))
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setattr(protected_context_registry, "_PROTECTED_CONTEXT_PROFILES", (pin,))
    closed = []
    monkeypatch.setattr(
        "google.cloud.storage.Client",
        lambda **_: SimpleNamespace(
            bucket=lambda _: protected_inputs["objects"].bucket,
            close=lambda: closed.append("storage"),
        ),
    )
    from src.analysis.open_intelligence.production_snapshot_capture import _read_protected_capture

    completed = _read_protected_capture(
        **{
            key: protected_inputs[key]
            for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
        },
        client_scope_id="ogilvy_default",
        market_scope=["ke", "ng", "za"],
        objects=protected_inputs["objects"],
        result_reader=lambda key: (ledger.rows[key]["result"],),
        approval_reader=lambda digest: approval.ExecutionApproval(
            **next(
                item["approval"]
                for item in ledger.rows.values()
                if item["result"]["manifest_sha256"] == digest
            )
        ),
    )
    assert (
        completed["binding"]["recovery_context"]["initial_result_id"]
        == ledger.original["result"]["result_id"]
    )
    assert completed["binding"]["stored_artifact"]["generation"] == 1
    binding = admission._source_binding(pin, completed)
    candidates = wires.candidate_rows(binding)
    candidates[-1]["sample_row_ids"] = ["fresh_native_row"]
    payload = json.loads(candidates[-1]["payload"])
    payload["sample_row_ids"] = ["fresh_native_row"]
    candidates[-1]["payload"] = json.dumps(payload)
    if direct_content:
        candidates = [wires.metadata(binding, 0, 0)]
    enriched = [
        wires.metadata(binding, 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "fresh_native_row",
            "match_count": 1,
            "payload": json.dumps(
                {
                    "id": "fresh_native_row",
                    "market": "za",
                    "source": "news",
                    "source_family": None,
                    "platform": "web",
                    "author_handle": None,
                    "url": None,
                    "title": "Mobility observations",
                    "text": "Mobility observations from a full native snapshot row.",
                    "published_at": None,
                    "collected_at": "2026-09-07T11:00:00+00:00",
                }
            ),
        },
    ]

    if direct_content:
        full_payload = json.loads(enriched[1]["payload"])
        full_payload["text"] = "x" * 4500 + (" mobility" if direct_content == "tail" else "")
        enriched[1]["payload"] = json.dumps(full_payload)
    enriched_index = [
        wires.metadata(binding, 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "fresh_native_row",
            "match_count": 1,
            "payload": json.dumps(
                {"market": "za", "id": "fresh_native_row", "collected_date": "2026-09-07"}
            ),
        },
    ]

    authority_by_id = {}
    for identifier, value in ledger.rows.items():
        consumed = value.get("consumption")
        if consumed is None:
            from datetime import timedelta

            from tests.unit.test_open_intelligence_execution_approval_runtime import (
                SOURCE_SNAPSHOT_NOW,
            )

            manifest = approval.validate_execution_manifest(
                json.loads(value["approval"]["canonical_manifest_json"]),
                mode="historical_replay",
                registry=retained_origin_registry(),
            )
            result_value = value["result"]
            consumed = approval.ExecutionConsumption(
                consumption_contract_version="open_intelligence_execution_consumption_v1",
                consumption_id=identifier,
                approval_id=result_value["approval_id"],
                manifest_sha256=result_value["manifest_sha256"],
                operation="source_snapshot_capture",
                execution_name=result_value["execution_name"],
                job_resource=manifest.job_resource,
                source_sha=manifest.source_sha,
                image_uri=manifest.image_uri,
                consumed_at=SOURCE_SNAPSHOT_NOW + timedelta(seconds=1),
            )
        authority_by_id[identifier] = {
            "approval_count": 1,
            "consumption_count": 1,
            "result_count": 1,
            "approval_json": snapshot.canonical_bytes(value["approval"]).decode(),
            "consumption_json": snapshot.canonical_bytes(
                consumed if isinstance(consumed, dict) else asdict(consumed)
            ).decode(),
            "result_json": snapshot.canonical_bytes(value["result"]).decode(),
        }
    raw_rows = copy.deepcopy(enriched)
    raw_rows[1]["lane"] = "raw_content"
    raw_index = copy.deepcopy(enriched_index)
    raw_index[1]["lane"] = "raw_content"
    empty = [wires.metadata(binding, 0, 0, evidence=True)]

    class JoinedHTTP(native.HTTP):
        def request(self, method, url, **kwargs):
            if "/tables/" in url:
                return capture_http.request(method, url, **kwargs)
            if method == "POST":
                body = json.loads(kwargs["data"])
                sql = body["configuration"]["query"]["query"]
                if "result_json" in sql:
                    parameters = body["configuration"]["query"]["queryParameters"]
                    identifiers = [item["parameterValue"]["value"] for item in parameters]
                    if "requested_consumption_id" in sql:
                        rows = [
                            {"requested_consumption_id": identifier, **authority_by_id[identifier]}
                            for identifier in identifiers
                        ]
                    else:
                        rows = [authority_by_id[identifiers[0]]]
                elif "partition_count" in sql:
                    rows = [
                        {"lane": "__metadata__", "partition_date": None, "partition_count": 2},
                        {
                            "lane": "enriched_content",
                            "partition_date": "2026-09-07",
                            "partition_count": 2,
                        },
                        {
                            "lane": "raw_content",
                            "partition_date": "2026-09-07",
                            "partition_count": 2,
                        },
                    ]
                elif "WITH candidates" in sql:
                    rows = candidates
                else:
                    raw = "'raw_content' AS lane" in sql
                    index = "CAST(DATE(evidence.collected_at) AS STRING)" in sql
                    rows = (
                        (raw_index if index else raw_rows)
                        if raw and raw_fallback
                        else (enriched_index if index else enriched)
                        if not raw and not raw_fallback
                        else empty
                    )
                native._context_wire(self, rows)
            return super().request(method, url, **kwargs)

    http = JoinedHTTP(identity, bucket=bucket)
    native.install(monkeypatch, http)
    result = snapshot.build_general_question_snapshot(
        invocation,
        store=store,
        scope=native.f.scope(),
        runtime_identity=identity,
        credentials=credentials,
        plan=plan,
        candidate_limit=100,
        evidence_limit=200,
        discovery_bytes=100000000,
        source_bytes=300000000,
        release_bytes=50000000,
        now=native.f.NOW,
    )
    assert result["status"] == "admitted", result
    if requirement_kind == "aggregate":
        assert result["snapshot"]["fulfilled_requirement_ids"] == []
        assert result["snapshot"]["missing_work"] == [plan["requirements"][0]["requirement_id"]]
    context = store.read_request(invocation["request_id"], scope=native.f.scope())
    queries = GeneralQuestionQueries(store)
    records = {
        ordinal: queries.read_query(
            invocation["request_id"], scope=native.f.scope(), ordinal=ordinal
        )
        for ordinal in sorted(int(key) for key in context["admission"]["execution"]["queries"])
    }
    assert set(records) == set(range(1, 9))
    restored = snapshot.validate_stored_general_question_snapshot(
        result["snapshot"],
        request=context["request"],
        intake=context["intake"],
        plan=plan,
        query_records=records,
    )
    assert restored == result["snapshot"]
    data_keys = ("enriched_index", "enriched", "raw_index", "raw")
    assert all(
        restored["provenance"]["captures"][key]["receipt"]["template_id"].endswith("_v4")
        for key in data_keys
    )
    for mutation in ("mixed_version", "policy_parameters"):
        changed = copy.deepcopy(restored)
        changed_records = {
            ordinal: {key: dict(value) for key, value in item.items()}
            for ordinal, item in records.items()
        }
        if mutation == "mixed_version":
            changed["provenance"]["captures"]["raw"]["receipt"]["template_id"] = (
                "protected_context_raw_v2"
            )
        else:
            changed["provenance"]["captures"]["enriched_index"]["receipt"]["parameters_digest"] = (
                "0" * 64
            )
            changed_records[5]["receipt"]["parameters_digest"] = "0" * 64
            changed_records[5]["reservation"]["parameters_digest"] = "0" * 64
        changed["snapshot_digest"] = snapshot.canonical_digest(
            {key: value for key, value in changed.items() if key != "snapshot_digest"}
        )
        with pytest.raises(ValueError, match="snapshot_invalid"):
            snapshot.validate_stored_general_question_snapshot(
                changed,
                request=context["request"],
                intake=context["intake"],
                plan=plan,
                query_records=changed_records,
            )
    with pytest.raises(ValueError, match="snapshot_invalid"):
        snapshot.validate_stored_general_question_snapshot(
            restored,
            request=context["request"],
            intake=context["intake"],
            plan=plan,
            query_records={key: value for key, value in records.items() if key != 8},
        )
    assert binding["market_scope"] == ["ke", "ng", "za"]
    assert plan["markets"] == ["za"]
    if requirement_kind == "aggregate":
        from src.analysis.open_intelligence.general_question_answer import (
            _context,
            project_question_answer,
        )
        from src.analysis.open_intelligence.general_question_result import observed_result_usage

        _context(context["request"], context["intake"], plan, restored)
        reading = restored["readings"][0]
        output = {
            "claims": [
                {
                    "claim_id": "observed",
                    "kind": "observation",
                    "segments": [{"kind": "reading", "reading_id": reading["reading_id"]}],
                    "receipt_ids": reading["source_receipt_ids"],
                    "reading_ids": [reading["reading_id"]],
                    "parent_claim_ids": [],
                    "support_state": "source_record",
                    "limitations": ["Selected source records only."],
                    "falsifier": None,
                }
            ],
            "sections": [{"kind": "answer", "claim_ids": ["observed"]}],
        }
        response = project_question_answer(
            output,
            request=context["request"],
            intake=context["intake"],
            plan=plan,
            snapshot=restored,
            usage=observed_result_usage(
                store, request_id=invocation["request_id"], scope=native.f.scope()
            ),
        )
        assert response["intelligence"]["status"] == "partial"
        assert plan["requirements"][0]["requirement_id"] in response["intelligence"]["missing_work"]
        assert "Selected-record counts do not satisfy" in " ".join(restored["limitations"])
        for mutation in ("fulfilled", "method"):
            forged = copy.deepcopy(restored)
            if mutation == "fulfilled":
                forged["fulfilled_requirement_ids"] = [plan["requirements"][0]["requirement_id"]]
                forged["missing_work"] = []
            else:
                forged["readings"][0]["method"] = "spend_amount"
            forged["snapshot_digest"] = snapshot.canonical_digest(
                {k: v for k, v in forged.items() if k != "snapshot_digest"}
            )
            with pytest.raises(ValueError, match="snapshot_invalid"):
                snapshot.validate_stored_general_question_snapshot(
                    forged,
                    request=context["request"],
                    intake=context["intake"],
                    plan=plan,
                    query_records=records,
                )
    assert restored["receipts"][0]["source_row_id"] == "fresh_native_row"
    if direct_content:
        assert "mobility" in restored["receipts"][0]["excerpt"].lower()
        assert len(restored["receipts"][0]["excerpt"]) <= 4000
        assert (
            restored["receipts"][0]["excerpt"] in full_payload["text"]
            or restored["receipts"][0]["excerpt"] == full_payload["title"]
        )
    assert restored["receipts"][0]["published_at"] is None
    assert closed == ["storage"]
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == 8
    expected_caps = [
        35000000,
        35000000,
        35000000,
        60000000,
        200000000,
        170000000,
        300000000,
        165000000,
    ]
    assert [
        int(call[3]["configuration"]["query"]["maximumBytesBilled"]) for call in posts
    ] == expected_caps
    assert sum(expected_caps) == 1000000000
    assert len(restored["provenance"]["authority_captures"]) == 2
    if "partitions" in restored["provenance"]["captures"]:
        altered = copy.deepcopy(restored)
        capture = altered["provenance"]["captures"]["partitions"]
        capture["rows"][1]["partition_date"] = "2026-09-06"
        digest = snapshot.canonical_digest(capture["rows"])
        capture["receipt"]["result_digest"] = digest
        altered_records = {
            ordinal: {key: dict(value) for key, value in record.items()}
            for ordinal, record in records.items()
        }
        altered_records[4]["receipt"]["result_digest"] = digest
        altered["snapshot_digest"] = snapshot.canonical_digest(
            {k: v for k, v in altered.items() if k != "snapshot_digest"}
        )
        with pytest.raises(ValueError, match="snapshot_invalid"):
            snapshot.validate_stored_general_question_snapshot(
                altered,
                request=context["request"],
                intake=context["intake"],
                plan=plan,
                query_records=altered_records,
            )
    if version == 4:
        assert restored["provenance"]["authority_captures"][1]["kind"] == "results"
        assert len(restored["provenance"]["authority_captures"][1]["rows"]) == 3

    if version == 4:
        for mutation in (
            "missing_ancestor",
            "duplicate_ancestor",
            "swapped_approval",
            "changed_nested_hash",
        ):
            altered_batch = copy.deepcopy(restored)
            altered_records = {
                ordinal: {key: dict(value) for key, value in record.items()}
                for ordinal, record in records.items()
            }
            capture = altered_batch["provenance"]["authority_captures"][1]
            if mutation == "missing_ancestor":
                capture["rows"].pop()
            elif mutation == "duplicate_ancestor":
                capture["rows"][1] = copy.deepcopy(capture["rows"][0])
            elif mutation == "swapped_approval":
                capture["rows"][0]["approval_json"] = capture["rows"][1]["approval_json"]
            else:
                source = altered_batch["provenance"]["source_binding"]
                source["recovery_context"]["ancestor_recovery_context"][
                    "ancestor_recovery_context"
                ]["failed_creation_job_digest"] = "0" * 64
                source["source_binding_digest"] = snapshot.canonical_digest(
                    {key: value for key, value in source.items() if key != "source_binding_digest"}
                )
            digest = snapshot.canonical_digest(capture["rows"])
            capture["receipt"]["result_digest"] = digest
            altered_records[capture["ordinal"]]["receipt"]["result_digest"] = digest
            altered_batch["snapshot_digest"] = snapshot.canonical_digest(
                {key: value for key, value in altered_batch.items() if key != "snapshot_digest"}
            )
            with pytest.raises(ValueError, match="snapshot_invalid"):
                snapshot.validate_stored_general_question_snapshot(
                    altered_batch,
                    request=context["request"],
                    intake=context["intake"],
                    plan=plan,
                    query_records=altered_records,
                )

    altered = copy.deepcopy(restored)
    altered["provenance"]["authority_captures"] = []
    altered["snapshot_digest"] = snapshot.canonical_digest(
        {key: value for key, value in altered.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError, match="snapshot_invalid"):
        snapshot.validate_stored_general_question_snapshot(
            altered,
            request=context["request"],
            intake=context["intake"],
            plan=plan,
            query_records=records,
        )

    for ordinal in (4, 6):
        unowned = dict(records)
        unowned[ordinal] = {**records[ordinal], "receipt": dict(records[ordinal]["receipt"])}
        unowned[ordinal]["receipt"]["result_digest"] = "0" * 64
        with pytest.raises(ValueError, match="snapshot_invalid"):
            snapshot.validate_stored_general_question_snapshot(
                restored,
                request=context["request"],
                intake=context["intake"],
                plan=plan,
                query_records=unowned,
            )
