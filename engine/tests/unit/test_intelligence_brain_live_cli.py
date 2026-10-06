from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "staging" / "run_live_intelligence_brain.py"
BRAIN_JOB = "intelligence-42-brain-staging"
_REAL_LOADER = __import__(
    "src.analysis.open_intelligence.execution_approval", fromlist=["_load_execution_authority"]
)._load_execution_authority
BRAIN_JOB_RESOURCE = f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{BRAIN_JOB}"


def _issued_brain_authority(monkeypatch, source_sha):
    # A real v2 authority for brain_read carrying the given source SHA; its job, principal
    # and image come from the packaged origin the loader binds. The real loader is put
    # back for the build because a test may already have replaced it.
    from src.analysis.open_intelligence import execution_approval

    from tests.unit import test_execution_manifest_origins as origins
    from tests.unit.test_execution_runtime_v2 import fixture, load

    monkeypatch.setattr(origins, "SOURCE_SHA", source_sha)
    monkeypatch.setattr(execution_approval, "_load_execution_authority", _REAL_LOADER)
    return load(fixture("brain_read"))


def test_brain_cli_starts_from_the_documented_repository_command() -> None:
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "error_code": "invalid_arguments",
        "model_calls": 0,
        "persisted": False,
        "run_id": None,
        "runtime_contract_version": "42_live_intelligence_runtime_v1_0_2",
        "signal_id": None,
    }


def _mock_durable_brain(monkeypatch, cli, source_sha):
    artifacts = {
        "brain_contract": b"brain",
        "run_receipt": b"receipt",
        "source_window_receipt_set": b"source-window",
    }
    inputs = cli.BrainControlInputs(
        receipt=SimpleNamespace(source_sha=source_sha),
        artifacts=artifacts,
    )
    authority = _issued_brain_authority(monkeypatch, source_sha)
    consumption = SimpleNamespace(consumption_id="exc_" + "e" * 64)
    result = SimpleNamespace(result_id="exr_" + "f" * 64)
    monkeypatch.setattr(cli, "_read_brain_control_artifacts", lambda **_kwargs: inputs)

    def load(operation, *, mode, artifact_reader=None):
        assert operation == "brain_read"
        assert mode == "new_consume"
        assert artifact_reader is cli._execution_approval_artifact_bytes
        for name, content in artifacts.items():
            assert cli._execution_approval_artifact_bytes(name) == content
        return authority

    monkeypatch.setattr(cli.execution_approval, "_load_execution_authority", load)
    monkeypatch.setattr(
        cli.execution_approval, "_consume_execution_authority", lambda _a: consumption
    )
    monkeypatch.setattr(
        cli.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: result,
    )


def test_cli_missing_and_unknown_arguments_emit_closed_one_line_refusal(capsys):
    from scripts.staging import run_live_intelligence_brain as cli

    assert cli.main([]) == 2
    first = capsys.readouterr()
    assert first.err == ""
    assert len(first.out.splitlines()) == 1
    payload = json.loads(first.out)
    assert payload == {
        "runtime_contract_version": "42_live_intelligence_runtime_v1_0_2",
        "error_code": "invalid_arguments",
        "run_id": None,
        "signal_id": None,
        "model_calls": 0,
        "persisted": False,
    }

    assert cli.main(["--unknown"]) == 2
    second = capsys.readouterr()
    assert second.err == ""
    assert "usage:" not in second.out.lower()
    assert json.loads(second.out)["error_code"] == "invalid_arguments"


def test_cli_preserves_each_validated_identity_when_the_other_is_missing(capsys):
    from scripts.staging import run_live_intelligence_brain as cli

    code = cli.main(
        [
            "--target",
            "staging",
            "--run-id",
            "run_live_001",
            "--research-depth",
            "briefing",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["error_code"] == "invalid_arguments"
    assert payload["run_id"] == "run_live_001"
    assert payload["signal_id"] is None


def test_cli_success_is_one_canonical_json_line_and_has_no_side_effects(monkeypatch, capsys):
    from scripts.staging import run_live_intelligence_brain as cli

    expected = {
        "runtime_contract_version": "42_live_intelligence_runtime_v1_0_2",
        "run_id": "run_live_001",
        "signal_id": "sig_" + "a" * 64,
        "snapshot_id": "bsp_" + "b" * 64,
        "snapshot_digest": "c" * 64,
        "brain_result": {"overall_admission": {"availability_state": "unavailable"}},
        "brain_result_digest": "d" * 64,
        "read_receipt": {"run_receipt_verified": True},
        "model_calls": 0,
        "persisted": False,
    }
    monkeypatch.setattr(cli, "_execute", lambda **_kwargs: expected)

    code = cli.main(
        [
            "--target",
            "staging",
            "--run-id",
            "run_live_001",
            "--signal-id",
            "sig_" + "a" * 64,
            "--research-depth",
            "briefing",
        ]
    )

    output = capsys.readouterr()
    assert code == 0
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    assert json.loads(output.out) == expected


def test_cli_bounds_internal_failures_without_exception_or_query_text(monkeypatch, capsys):
    from scripts.staging import run_live_intelligence_brain as cli

    def fail(**_kwargs):
        raise RuntimeError("SELECT secret FROM forbidden_table")

    monkeypatch.setattr(cli, "_execute", fail)
    code = cli.main(
        [
            "--target",
            "staging",
            "--run-id",
            "run_live_001",
            "--signal-id",
            "sig_" + "a" * 64,
            "--research-depth",
            "briefing",
        ]
    )

    output = capsys.readouterr()
    assert code == 2
    assert output.err == ""
    assert "SELECT" not in output.out
    assert "forbidden" not in output.out
    assert json.loads(output.out)["error_code"] == "internal_refused"


def test_control_plane_and_receipt_source_authority_bracket_bigquery(monkeypatch):
    from scripts.staging import run_live_intelligence_brain as cli

    from tests.unit.test_intelligence_brain_live_reader import (
        RUN_ID,
        SIGNAL_ID,
        _Client,
        _receipt_and_rows,
    )

    _receipt, rows = _receipt_and_rows()
    events = []
    client = _Client(rows)
    identity = type("Identity", (), {"source_sha": _receipt.source_sha})()
    _mock_durable_brain(monkeypatch, cli, _receipt.source_sha)
    payload = cli._execute(
        run_id=RUN_ID,
        signal_id=SIGNAL_ID,
        research_depth="briefing",
        invocation_arguments=("--target", "staging"),
        runtime_identity_verifier=lambda _args: events.append("control") or identity,
        bigquery_factory=lambda: events.append("bigquery") or client,
    )
    assert events == ["control", "bigquery"]
    assert payload["persisted"] is False

    _receipt, mismatched_rows = _receipt_and_rows()
    mismatched_rows["open_intelligence_run_receipts_v1"][0]["source_sha"] = "f" * 40
    mismatched_client = _Client(mismatched_rows)
    with pytest.raises(cli.LiveBrainReadRefusal, match="source SHA") as error:
        cli._execute(
            run_id=RUN_ID,
            signal_id=SIGNAL_ID,
            research_depth="briefing",
            invocation_arguments=("--target", "staging"),
            runtime_identity_verifier=lambda _args: identity,
            bigquery_factory=lambda: mismatched_client,
        )
    assert error.value.code == "identity_refused"
    assert mismatched_client.queries


def test_control_plane_refusal_occurs_before_bigquery_construction():
    from scripts.staging import run_live_intelligence_brain as cli

    calls = []
    with pytest.raises(cli.RuntimeIdentityRefusal):
        cli._execute(
            run_id="run_20260903_dynamic_apply_v2_r16",
            signal_id="sig_" + "e" * 64,
            research_depth="briefing",
            invocation_arguments=("--target", "staging"),
            runtime_identity_verifier=lambda _args: (_ for _ in ()).throw(
                cli.RuntimeIdentityRefusal("control-plane identity refused")
            ),
            bigquery_factory=lambda: calls.append("constructed"),
        )
    assert calls == []


def test_brain_runtime_uses_dedicated_identity_and_durable_artifact_provider():
    from scripts.staging import run_live_intelligence_brain as cli

    source = inspect.getsource(cli)
    profile = cli._brain_profile()
    assert profile.service_identity == (
        "intelligence-42-brain@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert profile.job_resource == BRAIN_JOB_RESOURCE
    assert "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com" not in source
    assert "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com" not in source
    assert "_execution_approval_artifact_bytes" in source
    assert '"brain_read"' in source
    assert "_consume_execution_authority" in source
    assert "_record_execution_result" in source


def test_brain_consumes_durable_approval_before_full_evidence(monkeypatch):
    from scripts.staging import run_live_intelligence_brain as cli

    events = []
    run_id = "run_live_001"
    signal_id = "sig_" + "a" * 64
    receipt = SimpleNamespace(source_sha="b" * 40)
    artifacts = {
        "brain_contract": b"brain",
        "run_receipt": b"receipt",
        "source_window_receipt_set": b"source-window",
    }
    inputs = cli.BrainControlInputs(receipt=receipt, artifacts=artifacts)
    identity = SimpleNamespace(source_sha=receipt.source_sha)
    authority = _issued_brain_authority(monkeypatch, receipt.source_sha)
    consumption = SimpleNamespace(consumption_id="exc_" + "e" * 64)
    stored_result = SimpleNamespace(result_id="exr_" + "f" * 64)
    read = SimpleNamespace(receipt=receipt, read_receipt={"run_receipt_verified": True})
    snapshot = SimpleNamespace(snapshot_id="bsp_" + "1" * 64, snapshot_digest="2" * 64)
    brain_result = SimpleNamespace(result_digest="3" * 64)

    monkeypatch.setattr(
        cli,
        "_read_brain_control_artifacts",
        lambda **_kwargs: events.append("control_artifacts") or inputs,
    )

    def load(operation, *, mode, artifact_reader=None):
        events.append("load")
        assert operation == "brain_read"
        assert mode == "new_consume"
        assert artifact_reader is cli._execution_approval_artifact_bytes
        for name, content in artifacts.items():
            assert cli._execution_approval_artifact_bytes(name) == content
        return authority

    monkeypatch.setattr(cli.execution_approval, "_load_execution_authority", load)
    monkeypatch.setattr(
        cli.execution_approval,
        "_consume_execution_authority",
        lambda _authority: events.append("consume") or consumption,
    )
    monkeypatch.setattr(
        cli,
        "read_live_brain_authority",
        lambda **_kwargs: events.append("evidence") or read,
    )
    monkeypatch.setattr(
        cli,
        "_issue_live_brain_runtime_request",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        cli,
        "_issue_live_brain_evidence_snapshot",
        lambda *_args: snapshot,
    )
    monkeypatch.setattr(
        cli,
        "_run_intelligence_brain_from_authority",
        lambda *_args: events.append("brain") or brain_result,
    )
    monkeypatch.setattr(
        cli,
        "_plain",
        lambda value: {"result_digest": value.result_digest} if value is brain_result else value,
    )
    monkeypatch.setattr(
        cli.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: events.append("result") or stored_result,
    )

    payload = cli._execute(
        run_id=run_id,
        signal_id=signal_id,
        research_depth="briefing",
        invocation_arguments=(
            "--target",
            "staging",
            "--run-id",
            run_id,
            "--signal-id",
            signal_id,
            "--research-depth",
            "briefing",
        ),
        runtime_identity_verifier=lambda _args: events.append("identity") or identity,
        bigquery_factory=lambda: events.append("client") or object(),
    )
    assert events == [
        "identity",
        "client",
        "load",
        "control_artifacts",
        "consume",
        "evidence",
        "brain",
        "result",
    ]
    assert payload["execution_approval"] == {
        "manifest_sha256": authority.approval.manifest_sha256,
        "approval_id": authority.approval.approval_id,
        "consumption_id": consumption.consumption_id,
        "result_id": stored_result.result_id,
    }
    assert cli._DURABLE_ARTIFACT_CONTEXT is None


def test_default_identity_verifier_reads_the_full_execution_resource(monkeypatch):
    """Cloud Run exposes bare job and execution names; the control plane wants the resource path."""
    from scripts.staging import run_live_intelligence_brain as cli

    monkeypatch.setenv("CLOUD_RUN_JOB", BRAIN_JOB)
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", f"{BRAIN_JOB}-abcde")
    seen = []
    payload = {"execution": {}, "job": {}}
    identity = object()
    monkeypatch.setattr(
        cli,
        "_cloud_run_control_plane_reader",
        lambda name, expected_job: seen.append((name, expected_job)) or payload,
    )
    monkeypatch.setattr(
        cli,
        "_verify_runtime_identity",
        lambda **kwargs: identity if kwargs["payload"] is payload else None,
    )
    assert cli._default_runtime_identity_verifier(("--target", "staging")) is identity
    assert seen == [(f"{BRAIN_JOB_RESOURCE}/executions/{BRAIN_JOB}-abcde", BRAIN_JOB_RESOURCE)]


def test_default_identity_verifier_refuses_a_foreign_or_missing_job(monkeypatch):
    from scripts.staging import run_live_intelligence_brain as cli

    monkeypatch.setattr(
        cli, "_cloud_run_control_plane_reader", lambda name, expected_job: pytest.fail(name)
    )
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", f"{BRAIN_JOB}-abcde")
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    with pytest.raises(cli.RuntimeIdentityRefusal):
        cli._default_runtime_identity_verifier(())
    for foreign in ("trends-engine-oi-apply-staging", "trends-engine-oi-brain-staging"):
        monkeypatch.setenv("CLOUD_RUN_JOB", foreign)
        with pytest.raises(cli.RuntimeIdentityRefusal):
            cli._default_runtime_identity_verifier(())
    monkeypatch.setenv("CLOUD_RUN_JOB", BRAIN_JOB)
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "projects/x/executions/y")
    with pytest.raises(cli.RuntimeIdentityRefusal):
        cli._default_runtime_identity_verifier(())


def _execute_with_recorded_reference(monkeypatch, research_depth, decision_question):
    from scripts.staging import run_live_intelligence_brain as cli

    run_id = "run_live_001"
    signal_id = "sig_" + "a" * 64
    receipt = SimpleNamespace(source_sha="b" * 40)
    inputs = cli.BrainControlInputs(receipt=receipt, artifacts={"brain_contract": b"brain"})
    authority = _issued_brain_authority(monkeypatch, receipt.source_sha)
    consumption = SimpleNamespace(consumption_id="exc_" + "e" * 64)
    read = SimpleNamespace(receipt=receipt, read_receipt={"run_receipt_verified": True})
    snapshot = SimpleNamespace(snapshot_id="bsp_" + "1" * 64, snapshot_digest="2" * 64)
    brain_result = SimpleNamespace(result_digest="3" * 64)
    references = []

    monkeypatch.setattr(cli, "_read_brain_control_artifacts", lambda **_kwargs: inputs)
    monkeypatch.setattr(
        cli.execution_approval,
        "_load_execution_authority",
        lambda _operation, *, mode, artifact_reader=None: (
            artifact_reader("brain_contract") and mode == "new_consume" and authority
        ),
    )
    monkeypatch.setattr(
        cli.execution_approval, "_consume_execution_authority", lambda _authority: consumption
    )
    monkeypatch.setattr(cli, "read_live_brain_authority", lambda **_kwargs: read)
    monkeypatch.setattr(
        cli, "_issue_live_brain_runtime_request", lambda *_args, **_kwargs: SimpleNamespace()
    )
    monkeypatch.setattr(cli, "_issue_live_brain_evidence_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(cli, "_run_intelligence_brain_from_authority", lambda *_args: brain_result)
    monkeypatch.setattr(
        cli,
        "_plain",
        lambda value: {"result_digest": value.result_digest} if value is brain_result else value,
    )

    def record(_authority, _consumption, reference, *_args, **_kwargs):
        references.append(reference)
        return SimpleNamespace(result_id="exr_" + "f" * 64)

    monkeypatch.setattr(cli.execution_approval, "_record_execution_result", record)
    arguments = ["--target", "staging", "--run-id", run_id, "--signal-id", signal_id]
    arguments += ["--research-depth", research_depth]
    if decision_question is not None:
        arguments += ["--decision-question", decision_question]
    cli._execute(
        run_id=run_id,
        signal_id=signal_id,
        research_depth=research_depth,
        decision_question=decision_question,
        invocation_arguments=tuple(arguments),
        runtime_identity_verifier=lambda _args: SimpleNamespace(source_sha=receipt.source_sha),
        bigquery_factory=lambda: object(),
    )
    assert len(references) == 1
    return references[0]


def test_result_reference_is_unique_per_depth_and_question(monkeypatch):
    # The result store refuses a second row carrying an existing result_reference, so a
    # briefing and an investigation on the same snapshot must never share one. Observed
    # 3 Sep 2026: three investigations after one briefing all died on
    # execution_approval_conflict with their authority already consumed.
    briefing = _execute_with_recorded_reference(monkeypatch, "briefing", None)
    first = _execute_with_recorded_reference(monkeypatch, "investigation", "What drives it?")
    second = _execute_with_recorded_reference(monkeypatch, "investigation", "Who carries it?")
    repeat = _execute_with_recorded_reference(monkeypatch, "investigation", "What drives it?")

    prefix = "brain://run_live_001/sig_" + "a" * 64 + "/bsp_" + "1" * 64 + "/"
    assert briefing.startswith(prefix + "briefing/")
    assert first.startswith(prefix + "investigation/")
    assert len({briefing, first, second}) == 3
    assert repeat == first
    assert "What drives it?" not in first
