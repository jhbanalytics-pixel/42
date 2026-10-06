from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from scripts.staging import issue_r3_execution_proof as issuer
from src.analysis.open_intelligence import execution_generations

GENERATION = execution_generations.active_generation()
V2 = "open_intelligence_execution_manifest_v2"


def _v2_binding(operation):
    return issuer._operation_binding(
        operation, manifest_version=V2, mode="historical_read", registry=GENERATION.registry
    )[1]


def _v2_authority(operation):
    binding = _v2_binding(operation)
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
        approval=SimpleNamespace(
            manifest_sha256="e" * 64,
            approval_id="exa_" + "f" * 64,
        ),
        execution_name=binding.job_resource + "/executions/exe-1",
        job_resource=binding.job_resource,
        image_uri=image_uri,
        generation=GENERATION,
    )


def test_cli_accepts_only_one_exact_r3_execution_name():
    target = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-apply-staging/executions/exe-1"
    )
    assert issuer._parse_cli(["--r3-execution-name", target]) == target
    for argv in (
        [],
        ["--r3-execution-name", "other"],
        ["--r3-execution", target],
        ["--r3-execution-name", target, "extra"],
    ):
        with pytest.raises(issuer.ProofIssuerRefusal):
            issuer._parse_cli(argv)


def test_public_main_accepts_only_argv_and_legacy_addendum_is_absent():
    assert tuple(inspect.signature(issuer.main).parameters) == ("argv",)
    source = inspect.getsource(issuer)
    assert "PROOF_ISSUER_ADDENDUM_SHA256" not in source
    assert "pre-execution-addendum" not in source
    assert "execution_authority_from_snapshots" not in source


def test_target_result_uses_only_the_authorized_result_chain_routine():
    source = inspect.getsource(issuer)
    assert "open_intelligence_execution_results_v1" not in source
    assert "open_intelligence_execution_consumptions_v1" not in source
    assert "open_intelligence_execution_approvals_v1" not in source
    assert "sp_read_open_intelligence_execution_result_chain_v1" in source


def test_durable_artifacts_bind_blocked_receipt_and_target_result():
    receipt = {"run_id": issuer.RUN_ID, "status": "completed"}
    target = {
        "result_digest": "a" * 64,
        "result_reference": "bq://target",
    }
    artifacts = issuer._build_artifacts(receipt, target)
    assert set(artifacts) == {"proof_issuer_contract", "blocked_run_receipt"}
    issuer._DURABLE_ARTIFACT_CONTEXT = issuer._ProofArtifactProvider(
        object(),
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-apply-staging/executions/exe-1",
        blocked_receipt=receipt,
        target_result=target,
        artifacts=artifacts,
    )
    try:
        assert issuer._execution_approval_artifact_bytes("proof_issuer_contract")
        assert issuer._execution_approval_artifact_bytes("blocked_run_receipt")
    finally:
        issuer._DURABLE_ARTIFACT_CONTEXT = None


def test_main_consumes_before_snapshot_and_records_result(monkeypatch, capsys):
    events = []
    apply_binding = _v2_binding("r3_apply")
    target_name = apply_binding.job_resource + "/executions/exe-1"
    receipt = {
        "run_id": issuer.RUN_ID,
        "signal_date": date(2026, 9, 3),
        "source_sha": "a" * 40,
    }
    target_proof = {
        "execution_name": target_name,
        "job_resource": apply_binding.job_resource,
        "image_digest": "sha256:" + "b" * 64,
        "source_sha": "a" * 40,
        "service_identity": apply_binding.service_identity,
        "command": "python",
        "args": ["scripts/staging/replay_open_intelligence.py"],
        "config_digest": "c" * 64,
        "started_at": "2026-08-31T12:00:00.000000Z",
        "completed_at": "2026-08-31T12:01:00.000000Z",
        "status": "succeeded",
    }
    target_result = {
        "canonical_result_json": json.dumps(target_proof, sort_keys=True, separators=(",", ":")),
        "result_digest": "d" * 64,
        "result_reference": "bq://r3-result",
        "proof": target_proof,
    }
    authority = _v2_authority("r3_proof_issue")
    consumption = SimpleNamespace(consumption_id="exc_" + "1" * 64)
    result = SimpleNamespace(result_id="exr_" + "2" * 64)

    class Client:
        pass

    monkeypatch.setattr(issuer.bigquery, "Client", lambda **_kwargs: Client())
    monkeypatch.setattr(
        issuer,
        "_blocked_receipt",
        lambda _client: events.append("blocked") or receipt,
    )
    monkeypatch.setattr(
        issuer,
        "_target_result",
        lambda _client, _name, **_kwargs: events.append("target") or target_result,
    )

    def load(operation, *, mode, artifact_reader=None):
        events.append("load")
        assert operation == "r3_proof_issue"
        assert mode == "new_consume"
        assert artifact_reader is issuer._execution_approval_artifact_bytes
        assert issuer._execution_approval_artifact_bytes("proof_issuer_contract")
        return authority

    monkeypatch.setattr(issuer.execution_approval, "_load_execution_authority", load)
    monkeypatch.setattr(
        issuer.execution_approval,
        "_consume_execution_authority",
        lambda _authority: events.append("consume") or consumption,
    )
    monkeypatch.setattr(
        issuer,
        "_snapshot",
        lambda _client: events.append("snapshot") or (receipt, {"candidates": 1}),
    )
    monkeypatch.setattr(
        issuer.replay,
        "build_r3_execution_proof",
        lambda **_kwargs: {"proof": "ready"},
    )
    monkeypatch.setattr(
        issuer.replay,
        "render_r3_execution_proof",
        lambda _proof: '{"proof":"ready"}\n',
    )
    monkeypatch.setattr(
        issuer.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: events.append("result") or result,
    )

    assert issuer.main(["--r3-execution-name", target_name]) == 0
    assert events == ["load", "blocked", "target", "consume", "snapshot", "result"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_approval"] == {
        "manifest_sha256": authority.approval.manifest_sha256,
        "approval_id": authority.approval.approval_id,
        "consumption_id": consumption.consumption_id,
        "result_id": result.result_id,
    }
    assert issuer._DURABLE_ARTIFACT_CONTEXT is None


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
        _terminal_context.extend((authority, consumption, "execution-1"))
        raise RuntimeError("private proof detail")

    monkeypatch.setattr(issuer, "_main_impl", fail_after_consumption)
    monkeypatch.setattr(
        issuer.execution_approval,
        "_record_execution_result",
        lambda *args: recorded.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
    )

    with pytest.raises(RuntimeError, match="private proof detail"):
        issuer.main([])

    assert len(recorded) == 1
    payload = json.loads(recorded[0][3])
    assert recorded[0][5] == "failed"
    assert payload == {
        "error_code": "r3_proof_issue_failed",
        "run_id": issuer.RUN_ID,
        "status": "failed",
    }
    assert "private proof detail" not in recorded[0][3]


def test_unresolved_result_write_never_substitutes_a_failed_payload(monkeypatch) -> None:
    authority = SimpleNamespace()
    consumption = SimpleNamespace()

    def unresolved(_argv, *, _terminal_context):
        _terminal_context.extend((authority, consumption, "execution-1"))
        raise issuer.execution_approval.ApprovalRefusal("execution_result_unresolved")

    monkeypatch.setattr(issuer, "_main_impl", unresolved)
    monkeypatch.setattr(
        issuer.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: pytest.fail("fallback result reached"),
    )
    with pytest.raises(issuer.execution_approval.ApprovalRefusal, match="unresolved"):
        issuer.main([])


def test_query_disables_retries_and_snapshot_is_one_bounded_select():
    source = inspect.getsource(issuer._query)
    assert "retry=None" in source
    assert "job_retry=None" in source
    snapshot = issuer._snapshot_sql()
    assert snapshot.lstrip().startswith("SELECT")
    assert "INSERT" not in snapshot
    assert "UPDATE" not in snapshot
    assert "DELETE" not in snapshot


def test_target_result_rejects_digest_or_cardinality_drift():
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

    target = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-apply-staging/executions/exe-1"
    )
    retained = {
        "version": issuer.execution_approval._RESULT_VERSION,
        "mode": "historical_read",
        "generation": GENERATION,
        "apply_binding": issuer._operation_binding(
            "r3_apply",
            manifest_version="open_intelligence_execution_manifest_v1",
            mode="historical_read",
            registry=GENERATION.registry,
        )[1],
    }
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer._target_result(Client([]), target, **retained)
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer._target_result(
            Client(
                [
                    {
                        "execution_name": target,
                        "status": "succeeded",
                        "canonical_result_json": '{"proof":"ready"}',
                        "result_digest": "0" * 64,
                        "result_reference": "bq://target",
                    }
                ]
            ),
            target,
            **retained,
        )


def test_executable_boundary_is_one_canonical_error_line(capsys):
    assert issuer.run_executable(["bad"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "r3_proof_authority_refused"}
    assert len(captured.err.splitlines()) == 1
