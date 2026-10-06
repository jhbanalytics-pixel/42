"""The managed runtime runs the QA semantic canary as a dry run after every cycle."""

from __future__ import annotations

import io
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from ops.runners import managed_runtime

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "ops" / "deploy" / "resource_manifest.json").read_text())
RUN_DATE = date(2026, 9, 25)


def test_a_passing_canary_writes_nothing_to_stderr_and_makes_no_client() -> None:
    stderr = io.StringIO()
    status = managed_runtime.run_semantic_canary_check(
        ROOT, MANIFEST, today=RUN_DATE, stderr=stderr
    )
    assert status == "passed"
    assert stderr.getvalue() == ""


def test_a_broken_expectation_writes_one_error_line_and_fails() -> None:
    stderr = io.StringIO()
    status = managed_runtime.run_semantic_canary_check(
        ROOT, MANIFEST, today=RUN_DATE, stderr=stderr, drill_break="thin"
    )
    assert status == "failed"
    lines = stderr.getvalue().strip().splitlines()
    assert len(lines) == 1
    alarm = json.loads(lines[0])
    assert alarm["severity"] == "ERROR"
    assert alarm["message"] == "qa_semantic_canary_failed"
    assert alarm["failed_cases"] == ["thin"]
    assert alarm["run_date"] == RUN_DATE.isoformat()


def test_the_canary_is_found_in_the_image_layout(tmp_path: Path) -> None:
    staging = tmp_path / "scripts" / "staging"
    staging.mkdir(parents=True)
    for name in (
        "run_42_semantic_canaries.py",
        "canary_open_intelligence_persistence.py",
    ):
        shutil.copy(ROOT / "engine" / "scripts" / "staging" / name, staging / name)
    assert (
        managed_runtime.semantic_canary_script(tmp_path)
        == staging / "run_42_semantic_canaries.py"
    )
    assert (
        managed_runtime.semantic_canary_script(ROOT)
        == ROOT / "engine" / "scripts" / "staging" / "run_42_semantic_canaries.py"
    )


def test_a_missing_canary_refuses_with_a_named_code(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="semantic_canary_unavailable"):
        managed_runtime.semantic_canary_script(tmp_path)


def test_main_fails_the_execution_after_printing_the_cycle_result(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        managed_runtime,
        "load_runtime_configuration",
        lambda _p: {
            "scheduler_configuration_sha256": "a" * 64,
            "resource_manifest_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(
        managed_runtime, "load_scheduler_configuration", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        managed_runtime, "load_resource_manifest", lambda *_a, **_k: MANIFEST
    )
    monkeypatch.setattr(
        managed_runtime,
        "build_invocation",
        lambda *_a, **_k: {"mode": "verify-runtime"},
    )
    monkeypatch.setattr(
        managed_runtime, "run_managed_mode", lambda *_a, **_k: {"status": "ok"}
    )
    calls = []

    def check(root, manifest, **kwargs):
        calls.append((root, manifest))
        return "failed"

    monkeypatch.setattr(managed_runtime, "run_semantic_canary_check", check)
    assert managed_runtime.main([]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}
    assert calls == [(ROOT, MANIFEST)]
    monkeypatch.setattr(
        managed_runtime, "run_semantic_canary_check", lambda *_a, **_k: "passed"
    )
    assert managed_runtime.main([]) == 0


def test_a_refused_cycle_does_not_run_the_canary(monkeypatch, capsys) -> None:
    def refuse(_path):
        raise ValueError("runtime_configuration_invalid")

    monkeypatch.setattr(managed_runtime, "load_runtime_configuration", refuse)
    monkeypatch.setattr(
        managed_runtime,
        "run_semantic_canary_check",
        lambda *_a, **_k: pytest.fail("canary ran after a refusal"),
    )
    assert managed_runtime.main([]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_a_canary_that_cannot_run_still_writes_one_error_line(monkeypatch) -> None:
    def broken(_root):
        raise ValueError("semantic_canary_unavailable")

    monkeypatch.setattr(managed_runtime, "semantic_canary_script", broken)
    stderr = io.StringIO()
    status = managed_runtime.run_semantic_canary_check(
        ROOT, MANIFEST, today=RUN_DATE, stderr=stderr
    )
    assert status == "failed"
    lines = stderr.getvalue().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "severity": "ERROR",
        "message": "qa_semantic_canary_failed",
        "error": "qa_semantic_canary_unavailable",
        "detail": "ValueError",
    }


def test_the_dry_run_constructs_no_bigquery_client(monkeypatch) -> None:
    from google.cloud import bigquery

    def refuse(*_args, **_kwargs):
        raise AssertionError("dry run constructed a client")

    monkeypatch.setattr(bigquery, "Client", refuse)
    stderr = io.StringIO()
    assert (
        managed_runtime.run_semantic_canary_check(
            ROOT, MANIFEST, today=RUN_DATE, stderr=stderr
        )
        == "passed"
    )
