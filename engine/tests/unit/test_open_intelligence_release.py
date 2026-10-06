"""The release step: a separate human act with a record.

Approved by 42-noncanary-run-release-approval-request-2026-08-29.md section
3.4. These tests guard the refusals: only a completed, complete-partitioned,
non-canary, still-blocked receipt releases, exactly once, and the record
names who approved, under which document, and the receipt digest the
decision was taken against.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from scripts.staging import release_open_intelligence_run as release
from src.analysis.open_intelligence import execution_approval, execution_generations, live_quality
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.run_receipts import (
    RUN_RECEIPT_ROW_FIELDS,
    build_run_receipt,
    run_receipt_digest,
)

RUN_ID = "run_20260903_dynamic_apply_v2_r16"
RELEASED_AT = datetime(2026, 8, 29, 23, 30, tzinfo=UTC)


def _receipt_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "run_contract_version": "open_intelligence_run_receipt_v1",
        "run_id": RUN_ID,
        "client_scope_id": "ogilvy_default",
        "market_scope": ("za", "ng", "ke"),
        "signal_date": date(2026, 9, 3),
        "observation_start": date(2026, 9, 3),
        "observation_end": date(2026, 9, 3),
        "observation_method": "dynamic_source_copy_apply_v1",
        "source_window_digest": "a" * 64,
        "cluster_build_version": "hybrid_graph_v1",
        "source_family_map_version": "channel_family_v1",
        "rule_version": "rules_v1",
        "status": "completed",
        "complete_partitions": True,
        "display_release_state": "blocked",
        "candidate_count": 1,
        "evidence_count": 2,
        "membership_count": 1,
        "lineage_count": 0,
        "analysis_count": 0,
        "prediction_count": 1,
        "row_set_digest": "b" * 64,
        "source_sha": "c" * 40,
        "completed_at": datetime(2026, 8, 29, 22, tzinfo=UTC),
    }
    fields.update(overrides)
    return fields


class _Recorder:
    def __init__(
        self,
        fields: dict[str, object],
        existing_records: int = 0,
        admission_overrides: dict[str, object] | None = None,
    ):
        self.calls: list[str] = []
        self._fields = fields
        self._existing = existing_records
        self._released = False
        self._admission = {
            "candidate_rows": fields["candidate_count"],
            "evidence_rows": fields["evidence_count"],
            "membership_rows": fields["membership_count"],
            "lineage_rows": fields["lineage_count"],
            "prediction_rows": fields["prediction_count"],
            "outcome_rows": 0,
            "analysis_rows": fields["analysis_count"],
            "qualifying_prediction_rows": fields["prediction_count"],
        }
        self._admission.update(admission_overrides or {})

    def __call__(self, sql: str):
        self.calls.append(sql)
        if sql.startswith("BEGIN TRANSACTION"):
            # The guarded update matches only the exact read state.
            if (
                self._fields["display_release_state"] == "blocked"
                and self._fields["status"] == "completed"
                and self._fields["complete_partitions"] is True
            ):
                self._released = True
            return []
        if release.RELEASE_RECORD_TABLE in sql and sql.startswith("SELECT run_id"):
            if not self._released:
                return []
            receipt = build_run_receipt(**self._fields)
            released_digest = run_receipt_digest(replace(receipt, display_release_state="enabled"))
            return [
                {
                    "run_id": RUN_ID,
                    "run_receipt_digest": released_digest,
                    "source_window_digest": self._fields["source_window_digest"],
                    "candidate_projection_digest": "3" * 64,
                    "packet_digest": "4" * 64,
                    "review_receipt_digest": "5" * 64,
                    "approval_addendum_sha256": "7" * 64,
                    "released_at": RELEASED_AT,
                    "release_contract_version": release.RELEASE_CONTRACT_VERSION,
                }
            ]
        if release.RELEASE_RECORD_TABLE in sql and "COUNT(*)" in sql:
            # After the transaction, the conditional insert has landed one
            # record (unless one already existed, which the insert skips).
            if self._released:
                return [{"existing": max(1, self._existing)}]
            return [{"existing": self._existing}]
        if "qualifying_prediction_rows" in sql:
            return [dict(self._admission)]
        if " AS row_set_digest" in sql:
            return [{"row_set_digest": self._fields["row_set_digest"]}]
        if release.PREDICTIONS_TABLE in sql and "eligible_rows" in sql:
            return [
                {
                    "prediction_rows": self._fields["prediction_count"],
                    "eligible_rows": 0,
                }
            ]
        if release.RECEIPT_TABLE in sql and sql.startswith("SELECT"):
            fields = dict(self._fields)
            if self._released:
                fields["display_release_state"] = "enabled"
            return [fields]
        return []


GENERATION = execution_generations.active_generation()


def _v2_authority(operation):
    binding = release._operation_binding(
        operation,
        manifest_version="open_intelligence_execution_manifest_v2",
        mode="new_consume",
        registry=GENERATION.registry,
    )[1]
    image_uri = (
        "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:" + "b" * 64
    )
    return SimpleNamespace(
        operation=operation,
        manifest=SimpleNamespace(
            job_resource=binding.job_resource,
            service_identity=binding.service_identity,
            image_uri=image_uri,
        ),
        approval=SimpleNamespace(manifest_sha256="8" * 64, approval_id="exa_" + "9" * 64),
        execution_name=binding.job_resource + "/executions/e",
        job_resource=binding.job_resource,
        image_uri=image_uri,
        generation=GENERATION,
    )


def _retained_apply_binding():
    return release._operation_binding(
        "r3_apply",
        manifest_version="open_intelligence_execution_manifest_v1",
        mode="historical_read",
        registry=GENERATION.registry,
    )[1]


def _release_evidence(blocked_digest: str):
    return release.ReleaseEvidence(
        blocked_run_receipt_digest=blocked_digest,
        candidate_projection_digest="3" * 64,
        packet_digest="4" * 64,
        quality_review_receipt_digest="5" * 64,
        execution_proof_digest="6" * 64,
        release_contract_digest="7" * 64,
        source_sha="c" * 40,
    )


def _execution_proof(
    blocked_digest: str, proof_manifest_sha256: str = "1" * 64
) -> dict[str, object]:
    return {
        "execution_proof_contract_version": "r3-execution-proof-v1",
        "r3_pre_execution_addendum_sha256": proof_manifest_sha256,
        "execution_name": (
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
            "trends-engine-oi-apply-staging/executions/exe-1"
        ),
        "job_resource": (
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-apply-staging"
        ),
        "image_digest": "sha256:" + "2" * 64,
        "source_sha": "c" * 40,
        "service_identity": ("trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"),
        "command": "python",
        "args": ["scripts/staging/replay_open_intelligence.py"],
        "config_digest": "3" * 64,
        "run_id": RUN_ID,
        "signal_date": "2026-09-03",
        "started_at": "2026-08-30T09:00:00.000000Z",
        "completed_at": "2026-08-30T10:00:00.000000Z",
        "status": "succeeded",
        "run_receipt_digest": blocked_digest,
        "row_set_digest": "b" * 64,
        "persisted_row_family_counts": [
            ["candidates", 1],
            ["evidence", 2],
            ["membership", 1],
            ["lineage", 0],
            ["predictions", 1],
            ["outcomes", 0],
            ["analysis", 0],
        ],
    }


def _run(recorder: _Recorder):
    blocked = run_receipt_digest(build_run_receipt(**recorder._fields))
    return release._release_open_intelligence_run(
        run_id=RUN_ID,
        released_at=RELEASED_AT,
        query_runner=recorder,
        evidence=_release_evidence(blocked),
    )


def test_release_uses_only_durable_execution_authority():
    source = inspect.getsource(release)
    for retired in (
        "R3_PRE_EXECUTION_ADDENDUM_SHA256",
        "R3_RELEASE_ADDENDUM_SHA256",
        "R3_EXECUTION_PROOF_DIGEST",
        "R3_BLOCKED_RUN_RECEIPT_DIGEST",
        "R3_CANDIDATE_PROJECTION_DIGEST",
        "R3_PACKET_DIGEST",
        "R3_QUALITY_REVIEW_RECEIPT_DIGEST",
        "R3_RELEASE_SOURCE_SHA",
        "ReleaseAuthority",
        "release-addendum-file",
        "review-receipt-file",
        "execution-proof-file",
    ):
        assert retired not in source
    assert tuple(inspect.signature(release.main).parameters) == ("argv",)


def test_main_consumes_before_release_and_records_result(monkeypatch, capsys):
    events = []
    receipt = build_run_receipt(**_receipt_fields())
    evidence = _release_evidence(run_receipt_digest(receipt))
    artifacts = {
        "blocked_run_receipt": b"blocked",
        "execution_proof": b"proof",
        "quality_review_receipt": b"review",
        "release_contract": b"contract",
    }
    inputs = release.ReleaseInputs(receipt=receipt, evidence=evidence, artifacts=artifacts)
    authority = _v2_authority("r3_release")
    consumption = SimpleNamespace(consumption_id="exc_" + "a" * 64)
    result = SimpleNamespace(result_id="exr_" + "b" * 64)
    report = release.ReleaseReport(
        run_id=RUN_ID,
        blocked_receipt_digest=evidence.blocked_run_receipt_digest,
        released_receipt_digest="d" * 64,
        run_receipt_digest="d" * 64,
        source_window_digest="a" * 64,
        candidate_projection_digest="3" * 64,
        packet_digest="4" * 64,
        review_receipt_digest="5" * 64,
        approval_addendum_sha256="7" * 64,
        released_at=RELEASED_AT,
        release_contract_version=release.RELEASE_CONTRACT_VERSION,
        approved_by="durable_execution_approval",
        approval_document="r3-release-contract-v1",
    )

    monkeypatch.setattr(release.bigquery, "Client", lambda **_kwargs: object())
    monkeypatch.setattr(
        release,
        "_read_release_inputs",
        lambda _client, **_kwargs: events.append("inputs") or inputs,
    )

    def load(operation, *, mode, artifact_reader=None):
        events.append("load")
        assert operation == "r3_release"
        assert mode == "new_consume"
        assert artifact_reader is release._execution_approval_artifact_bytes
        for name, content in artifacts.items():
            assert release._execution_approval_artifact_bytes(name) == content
        return authority

    monkeypatch.setattr(release.execution_approval, "_load_execution_authority", load)
    monkeypatch.setattr(
        release.execution_approval,
        "_consume_execution_authority",
        lambda _authority: events.append("consume") or consumption,
    )
    monkeypatch.setattr(
        release,
        "_release_open_intelligence_run",
        lambda **_kwargs: events.append("release") or report,
    )
    monkeypatch.setattr(
        release.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: events.append("result") or result,
    )

    assert release.main([]) == 0
    assert events == ["load", "inputs", "consume", "release", "result"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_approval"] == {
        "manifest_sha256": authority.approval.manifest_sha256,
        "approval_id": authority.approval.approval_id,
        "consumption_id": consumption.consumption_id,
        "result_id": result.result_id,
    }


def test_post_consumption_failure_records_one_sanitized_terminal_result(monkeypatch) -> None:
    authority = SimpleNamespace(
        approval=SimpleNamespace(
            manifest_sha256="a" * 64,
            approval_id="exa_" + "b" * 64,
        ),
        generation=GENERATION,
    )
    consumption = SimpleNamespace(
        consumption_id="exc_" + "c" * 64,
        origin_registry_sha256=GENERATION.origin_registry_sha256,
        resource_manifest_sha256=GENERATION.resource_manifest_sha256,
    )
    recorded = []

    def fail_after_consumption(_argv, *, _terminal_context):
        _terminal_context.extend((authority, consumption))
        raise RuntimeError("private release detail")

    monkeypatch.setattr(release, "_main_impl", fail_after_consumption)
    monkeypatch.setattr(
        release.execution_approval,
        "_record_execution_result",
        lambda *args: recorded.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
    )

    with pytest.raises(RuntimeError, match="private release detail"):
        release.main([])

    assert len(recorded) == 1
    payload = json.loads(recorded[0][3])
    assert recorded[0][5] == "failed"
    assert payload == {
        "error_code": "r3_release_failed",
        "run_id": release.R3_RUN_ID,
        "status": "failed",
    }
    assert "private release detail" not in recorded[0][3]
    assert release._DURABLE_ARTIFACT_CONTEXT is None


def test_unresolved_result_write_never_substitutes_a_failed_payload(monkeypatch) -> None:
    authority = SimpleNamespace()
    consumption = SimpleNamespace()

    def unresolved(_argv, *, _terminal_context):
        _terminal_context.extend((authority, consumption))
        raise release.execution_approval.ApprovalRefusal("execution_result_unresolved")

    monkeypatch.setattr(release, "_main_impl", unresolved)
    monkeypatch.setattr(
        release.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: pytest.fail("fallback result reached"),
    )
    with pytest.raises(release.execution_approval.ApprovalRefusal, match="unresolved"):
        release.main([])


def test_release_inputs_bind_the_complete_ledger_and_warehouse_chain():
    from tests.unit import test_dynamic_quality_review as review_fixtures

    receipt = build_run_receipt(**_receipt_fields())
    blocked_digest = run_receipt_digest(receipt)
    proof = _execution_proof(blocked_digest)
    review_packet = live_quality.build_review_packet(RUN_ID, review_fixtures._batch())
    review = review_fixtures._receipt(review_packet)
    inputs = release._build_release_inputs(
        receipt=receipt,
        execution_proof_bytes=canonical_bytes(proof),
        execution_proof_manifest_sha256="1" * 64,
        quality_review_receipt=review,
        review_packet=review_packet,
        candidate_projection_digest=review["candidate_projection_digest"],
        apply_binding=_retained_apply_binding(),
    )
    assert set(inputs.artifacts) == {
        "blocked_run_receipt",
        "execution_proof",
        "quality_review_receipt",
        "release_contract",
    }
    assert inputs.artifacts["blocked_run_receipt"] == canonical_bytes(receipt)
    assert inputs.artifacts["execution_proof"] == canonical_bytes(proof)
    assert inputs.artifacts["quality_review_receipt"] == canonical_bytes(review)
    assert inputs.evidence.blocked_run_receipt_digest == blocked_digest
    assert inputs.evidence.quality_review_receipt_digest == review["receipt_digest"]
    contract = json.loads(inputs.artifacts["release_contract"])
    assert contract["run_id"] == RUN_ID
    assert contract["candidate_projection_digest"] == review["candidate_projection_digest"]
    assert contract["packet_digest"] == review_packet["packet_digest"]

    for mutation in (
        {**proof, "run_id": "foreign_run"},
        {**proof, "r3_pre_execution_addendum_sha256": "9" * 64},
        {**proof, "run_receipt_digest": "9" * 64},
        {**proof, "row_set_digest": "9" * 64},
        {**proof, "status": "failed"},
    ):
        with pytest.raises(release.ReleaseRefusal):
            release._build_release_inputs(
                receipt=receipt,
                execution_proof_bytes=canonical_bytes(mutation),
                execution_proof_manifest_sha256="1" * 64,
                quality_review_receipt=review,
                review_packet=review_packet,
                candidate_projection_digest=review["candidate_projection_digest"],
                apply_binding=_retained_apply_binding(),
            )
    for field in proof:
        with pytest.raises(release.ReleaseRefusal):
            release._build_release_inputs(
                receipt=receipt,
                execution_proof_bytes=canonical_bytes(
                    {key: value for key, value in proof.items() if key != field}
                ),
                execution_proof_manifest_sha256="1" * 64,
                quality_review_receipt=review,
                review_packet=review_packet,
                candidate_projection_digest=review["candidate_projection_digest"],
                apply_binding=_retained_apply_binding(),
            )

    for field in live_quality.REVIEW_RECEIPT_FIELDS:
        mutation = {key: value for key, value in review.items() if key != field}
        with pytest.raises(release.ReleaseRefusal):
            release._build_release_inputs(
                receipt=receipt,
                execution_proof_bytes=canonical_bytes(proof),
                execution_proof_manifest_sha256="1" * 64,
                quality_review_receipt=mutation,
                review_packet=review_packet,
                candidate_projection_digest=review["candidate_projection_digest"],
                apply_binding=_retained_apply_binding(),
            )


def test_query_is_bounded_and_disables_retries():
    events = []

    class Job:
        def result(self, **kwargs):
            events.append(("result", kwargs))
            return ({"ok": True},)

    class Client:
        def query(self, sql, **kwargs):
            events.append(("query", sql, kwargs))
            return Job()

    assert release._query(Client(), "SELECT 1", max_results=2) == ({"ok": True},)
    assert events[0][2]["retry"] is None
    assert events[0][2]["job_retry"] is None
    assert events[1][1]["retry"] is None
    assert events[1][1]["job_retry"] is None
    assert events[1][1]["max_results"] == 2


def test_cli_accepts_no_caller_authority_or_target():
    assert release._parse_cli([]) == ()
    for argv in (["--run-id", RUN_ID], ["extra"]):
        with pytest.raises(release.ReleaseRefusal):
            release._parse_cli(argv)


def test_release_source_uses_only_authorized_result_and_review_routines():
    source = inspect.getsource(release)
    assert "open_intelligence_execution_results_v1" not in source
    assert "open_intelligence_execution_consumptions_v1" not in source
    assert "open_intelligence_execution_approvals_v1" not in source
    assert "sp_read_open_intelligence_execution_result_chain_v1" in source
    assert "sp_read_open_intelligence_quality_review_receipt_v1" in source
    assert "r3-quality-review-ledger-binding-v1" not in source


def test_proof_ledger_binding_replays_the_exact_target_apply_result():
    from tests.unit.test_open_intelligence_execution_approval_contract import valid_manifest

    apply_result = {
        "result_digest": "5" * 64,
        "result_reference": "bq://r3-apply-result",
    }
    proof_manifest = valid_manifest("r3_proof_issue")
    issuer_contract = canonical_bytes(
        {
            "contract_version": "r3-proof-issuer-durable-v1",
            "run_id": RUN_ID,
            "target_result_digest": apply_result["result_digest"],
            "target_result_reference": apply_result["result_reference"],
        }
    )
    for artifact in proof_manifest["input_artifacts"]:
        if artifact["name"] == "proof_issuer_contract":
            artifact["sha256"] = hashlib.sha256(issuer_contract).hexdigest()
    proof_manifest_sha = execution_approval.manifest_sha256(
        proof_manifest, mode="historical_read", registry=GENERATION.registry
    )
    proof = _execution_proof(
        run_receipt_digest(build_run_receipt(**_receipt_fields())), proof_manifest_sha
    )
    proof_bytes = canonical_bytes(proof)
    proof_result = {
        "canonical_manifest_json": canonical_bytes(proof_manifest).decode(),
        "manifest_sha256": proof_manifest_sha,
        "canonical_result_json": proof_bytes.decode(),
        "payload": proof,
    }
    retained = {"mode": "historical_read", "registry": GENERATION.registry}
    assert release._proof_ledger_binding(proof_result, apply_result, **retained) == (
        proof_bytes,
        proof_manifest_sha,
    )
    with pytest.raises(release.ReleaseRefusal):
        release._proof_ledger_binding(
            proof_result,
            {**apply_result, "result_digest": "9" * 64},
            **retained,
        )


def test_authorized_result_chain_reader_rejects_cardinality_and_digest_drift():
    proof = canonical_bytes({"run_id": RUN_ID, "status": "succeeded"}).decode()

    class Job:
        def __init__(self, rows):
            self.rows = rows

        def result(self, **_kwargs):
            return self.rows

    class Client:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *_args, **_kwargs):
            return Job(self.rows)

    retained = {
        "version": execution_approval._RESULT_VERSION,
        "mode": "historical_read",
        "generation": GENERATION,
    }
    with pytest.raises(release.ReleaseRefusal):
        release._read_operation_result_chain(Client([]), "r3_proof_issue", **retained)
    with pytest.raises(release.ReleaseRefusal):
        release._read_operation_result_chain(
            Client(
                [
                    {
                        "canonical_result_json": proof,
                        "result_digest": "0" * 64,
                        "status": "succeeded",
                    }
                ]
            ),
            "r3_proof_issue",
            **retained,
        )


def test_quality_review_reader_returns_exact_registered_receipt():
    from tests.unit import test_dynamic_quality_review as review_fixtures

    packet = live_quality.build_review_packet(RUN_ID, review_fixtures._batch())
    receipt = review_fixtures._receipt(packet)
    canonical = canonical_bytes(receipt).decode()
    row = {
        "review_store_contract_version": "open_intelligence_quality_review_store_v1",
        "run_id": RUN_ID,
        "canonical_review_receipt_json": canonical,
        "review_receipt_digest": receipt["receipt_digest"],
        "artifact_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "source_window_digest": receipt["source_window_digest"],
        "candidate_projection_digest": receipt["candidate_projection_digest"],
        "packet_digest": receipt["packet_digest"],
        "registered_by": "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab",
        "registered_at": datetime(2026, 8, 31, 12, tzinfo=UTC),
    }
    events = []

    class Job:
        def result(self, **_kwargs):
            return (row,)

    class Client:
        def query(self, sql, **_kwargs):
            events.append(sql)
            return Job()

    assert (
        release._read_quality_review_receipt(
            Client(),
            packet=packet,
            source_window_digest=receipt["source_window_digest"],
            candidate_projection_digest=receipt["candidate_projection_digest"],
        )
        == receipt
    )
    assert events == [
        "CALL `ogilvy-trends-v2.trends_v2_staging."
        "sp_read_open_intelligence_quality_review_receipt_v1`(@run_id)"
    ]


def test_executable_boundary_emits_one_canonical_refusal_line(capsys):
    assert release.run_executable(["extra"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "r3_release_authority_refused"}
    assert len(captured.err.splitlines()) == 1


def test_a_qualifying_blocked_receipt_releases_exactly_and_is_recorded() -> None:
    recorder = _Recorder(_receipt_fields())
    report = _run(recorder)
    assert report.run_id == RUN_ID
    blocked_digest = run_receipt_digest(build_run_receipt(**_receipt_fields()))
    assert report.blocked_receipt_digest == blocked_digest
    transaction = [sql for sql in recorder.calls if sql.startswith("BEGIN TRANSACTION")]
    assert len(transaction) == 1
    body = transaction[0]
    assert "SET display_release_state = 'enabled'" in body
    assert "SET display_eligible = TRUE" not in body
    assert "NOT p.display_eligible" in body
    assert release.RELEASE_RECORD_TABLE in body
    assert "3" * 64 in body
    assert "4" * 64 in body
    assert body.rstrip().endswith("COMMIT TRANSACTION;")


def test_release_report_is_the_exact_persisted_release_record() -> None:
    recorder = _Recorder(_receipt_fields())
    report = _run(recorder)
    assert report.run_receipt_digest == report.released_receipt_digest
    assert report.source_window_digest == "a" * 64
    assert report.candidate_projection_digest == "3" * 64
    assert report.packet_digest == "4" * 64
    assert report.review_receipt_digest == "5" * 64
    assert report.approval_addendum_sha256 == "7" * 64
    assert report.release_contract_version == release.RELEASE_CONTRACT_VERSION
    readbacks = [
        sql
        for sql in recorder.calls
        if sql.startswith("SELECT run_id") and release.RELEASE_RECORD_TABLE in sql
    ]
    assert len(readbacks) == 1


def test_release_record_field_drift_refuses_after_commit() -> None:
    class _RecordDrift(_Recorder):
        def __call__(self, sql: str):
            result = super().__call__(sql)
            if sql.startswith("SELECT run_id") and release.RELEASE_RECORD_TABLE in sql:
                result[0]["packet_digest"] = "9" * 64
            return result

    with pytest.raises(release.ReleaseRefusal, match="release record readback"):
        _run(_RecordDrift(_receipt_fields()))


def test_the_receipt_update_is_guarded_by_the_read_state() -> None:
    recorder = _Recorder(_receipt_fields())
    _run(recorder)
    body = next(sql for sql in recorder.calls if sql.startswith("BEGIN TRANSACTION"))
    assert "display_release_state = 'blocked'" in body
    assert "row_set_digest = '" + "b" * 64 + "'" in body


def test_release_revalidates_the_complete_display_admission_before_writing() -> None:
    recorder = _Recorder(_receipt_fields())
    _run(recorder)
    transaction = next(sql for sql in recorder.calls if sql.startswith("BEGIN TRANSACTION"))
    for table in (
        release.CANDIDATES_TABLE,
        release.EVIDENCE_TABLE,
        release.MEMBERSHIP_TABLE,
        release.PREDICTIONS_TABLE,
    ):
        assert table in transaction
    assert "ASSERT" in transaction
    assert "qualifies_evidence" in transaction
    assert "qualifying_prediction_rows" in transaction
    assert "m.member_identity = c.label_member_identity" in transaction
    assert "m.canonical_value = c.label AND m.qualifies_evidence) = 1" in transaction
    assert transaction.index("qualifying_prediction_rows") < transaction.index(
        "SET display_release_state = 'enabled'"
    )
    assert "UPDATE `ogilvy-trends-v2.trends_v2_staging.signal_predictions_v2`" not in transaction
    assert "ASSERT @@row_count = 1" in transaction
    assert "candidate_projection_digest" in transaction
    assert "packet_digest" in transaction


def test_release_recomputes_full_row_set_and_counts_inside_transaction_and_after_commit() -> None:
    recorder = _Recorder(_receipt_fields())
    _run(recorder)
    transaction = next(sql for sql in recorder.calls if sql.startswith("BEGIN TRANSACTION"))
    for family, table in {
        "candidates": "signal_candidates_v2",
        "evidence": "signal_evidence_v2",
        "membership": "signal_membership_v2",
        "lineage": "signal_lineage_v2",
        "predictions": "signal_predictions_v2",
        "outcomes": "signal_outcomes_v2",
        "analysis": "signal_analysis_v2",
    }.items():
        assert f'"{family}"' in transaction
        assert table in transaction
    assert "blocked_receipt_digest" in transaction
    assert transaction.count("row_set_digest") >= 3
    family_positions = [
        transaction.index(f'"{family}"')
        for family in (
            "analysis",
            "candidates",
            "evidence",
            "lineage",
            "membership",
            "outcomes",
            "predictions",
        )
    ]
    assert family_positions == sorted(family_positions)
    post_commit = [sql for sql in recorder.calls if " AS row_set_digest" in sql]
    assert len(post_commit) == 1
    assert "row_set_digest" in post_commit[0]


@pytest.mark.parametrize(
    ("receipt_overrides", "admission_overrides", "fragment"),
    [
        ({"prediction_count": 0}, {}, "displayable prediction"),
        ({"membership_count": 0}, {}, "membership"),
        ({"evidence_count": 0}, {}, "evidence"),
    ],
)
def test_an_incomplete_or_unlinked_row_set_refuses_release(
    receipt_overrides: dict[str, object],
    admission_overrides: dict[str, object],
    fragment: str,
) -> None:
    fields = _receipt_fields(**receipt_overrides)
    recorder = _Recorder(fields, admission_overrides=admission_overrides)
    with pytest.raises(release.ReleaseRefusal, match=fragment):
        _run(recorder)
    assert not any(sql.startswith("BEGIN TRANSACTION") for sql in recorder.calls)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"status": "failed"}, "not completed"),
        ({"complete_partitions": False}, "complete partitions"),
        ({"client_scope_id": "qa_canary"}, "canary"),
        ({"display_release_state": "enabled"}, "already released"),
    ],
)
def test_a_nonqualifying_receipt_refuses(overrides: dict, fragment: str) -> None:
    recorder = _Recorder(_receipt_fields(**overrides))
    with pytest.raises(release.ReleaseRefusal) as caught:
        _run(recorder)
    assert fragment in str(caught.value)
    assert not any(sql.startswith("BEGIN TRANSACTION") for sql in recorder.calls)


def test_a_second_release_of_the_same_run_refuses() -> None:
    recorder = _Recorder(_receipt_fields(), existing_records=1)
    with pytest.raises(release.ReleaseRefusal, match="already exists"):
        _run(recorder)
    assert not any(sql.startswith("BEGIN TRANSACTION") for sql in recorder.calls)


def test_a_missing_receipt_refuses() -> None:
    class _Empty(_Recorder):
        def __call__(self, sql: str):
            self.calls.append(sql)
            if release.RECEIPT_TABLE in sql and sql.startswith("SELECT"):
                return []
            return [{"existing": 0}]

    with pytest.raises(release.ReleaseRefusal, match="exactly one receipt"):
        _run(_Empty(_receipt_fields()))


def test_a_receipt_that_changed_between_read_and_release_refuses() -> None:
    class _Drifting(_Recorder):
        def __call__(self, sql: str):
            result = super().__call__(sql)
            if sql.startswith("BEGIN TRANSACTION"):
                # The database state drifted, so the guarded update matched
                # nothing and the flip never happened.
                self._released = False
            return result

    recorder = _Drifting(_receipt_fields())
    with pytest.raises(release.ReleaseRefusal, match="changed between read and release"):
        _run(recorder)


def test_post_commit_row_drift_refuses_after_repeating_the_full_digest() -> None:
    class _RowDrift(_Recorder):
        def __call__(self, sql: str):
            result = super().__call__(sql)
            if self._released and " AS row_set_digest" in sql:
                return [{"row_set_digest": "9" * 64}]
            return result

    with pytest.raises(release.ReleaseRefusal, match="post-commit row_set_digest"):
        _run(_RowDrift(_receipt_fields()))


def test_the_release_identity_and_timestamp_are_required() -> None:
    recorder = _Recorder(_receipt_fields())
    with pytest.raises(release.ReleaseRefusal):
        release._release_open_intelligence_run(
            run_id="foreign_run",
            released_at=RELEASED_AT,
            query_runner=recorder,
            evidence=_release_evidence("1" * 64),
        )
    with pytest.raises(release.ReleaseRefusal):
        release._release_open_intelligence_run(
            run_id=RUN_ID,
            released_at=datetime(2026, 8, 29, 23, 30),
            query_runner=recorder,
            evidence=_release_evidence("1" * 64),
        )


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"run_id": "x' OR '1'='1"}, "run_id"),
    ],
)
def test_release_identities_refuse_hostile_or_ambiguous_text_before_query(
    overrides: dict[str, object], fragment: str
) -> None:
    recorder = _Recorder(_receipt_fields())
    values = {
        "run_id": RUN_ID,
        "released_at": RELEASED_AT,
        "query_runner": recorder,
        "evidence": _release_evidence("1" * 64),
    }
    values.update(overrides)
    with pytest.raises(release.ReleaseRefusal, match=fragment):
        release._release_open_intelligence_run(**values)
    assert recorder.calls == []


def test_descriptive_approver_text_cannot_authorize_release():
    assert "approved_by" not in inspect.signature(release._release_open_intelligence_run).parameters
    assert (
        "approval_document"
        not in inspect.signature(release._release_open_intelligence_run).parameters
    )


def test_every_receipt_field_travels_through_the_reader() -> None:
    """The reader must reconstruct the full receipt, not a convenient subset,
    or the digest recorded at release would not be the receipt's digest."""
    recorder = _Recorder(_receipt_fields())
    _run(recorder)
    select = next(
        sql for sql in recorder.calls if sql.startswith("SELECT") and release.RECEIPT_TABLE in sql
    )
    for field in RUN_RECEIPT_ROW_FIELDS:
        assert field in select


def test_the_record_insert_is_conditional_and_read_back_exactly_after_commit() -> None:
    """Found by independent review: two releases interleaving between the
    pre-check and the commit could both insert a record and both report
    success. The insert is now conditional on no record existing, and the
    readback asserts exactly one record for the run."""
    recorder = _Recorder(_receipt_fields())
    _run(recorder)
    body = next(sql for sql in recorder.calls if sql.startswith("BEGIN TRANSACTION"))
    assert "WHERE NOT EXISTS" in body
    count_checks = [
        sql
        for sql in recorder.calls
        if sql.startswith("SELECT COUNT(*)") and release.RELEASE_RECORD_TABLE in sql
    ]
    assert len(count_checks) == 1
    exact_readbacks = [
        sql
        for sql in recorder.calls
        if sql.startswith("SELECT run_id") and release.RELEASE_RECORD_TABLE in sql
    ]
    assert len(exact_readbacks) == 1


def test_a_duplicate_record_after_commit_refuses_loudly() -> None:
    class _Doubled(_Recorder):
        def __call__(self, sql: str):
            result = super().__call__(sql)
            if sql.startswith("SELECT run_id") and release.RELEASE_RECORD_TABLE in sql:
                return [*result, *result]
            return result

    with pytest.raises(release.ReleaseRefusal, match="release record readback"):
        _run(_Doubled(_receipt_fields()))
