import hashlib
import inspect
import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from scripts.staging import approve_open_intelligence_execution as approval_cli
from src.analysis.open_intelligence import execution_approval

from tests.unit.test_open_intelligence_execution_approval_contract import APPROVED_BY
from tests.unit.test_operator_callers_v2 import (
    V1,
    canonical,
    generation,
    v1_manifest,
    v2_manifest,
)


@pytest.fixture(autouse=True)
def _packaged_generation(monkeypatch):
    monkeypatch.setattr(approval_cli, "_active_generation", generation)


class _Job:
    def __init__(self, rows):
        self._rows = rows

    def result(self, **_kwargs):
        return self._rows


class _Client:
    def __init__(self, rows, error=None):
        self.rows = rows
        self.error = error
        self.calls = []

    def query(self, sql, job_config=None, **kwargs):
        self.calls.append((sql, job_config, kwargs))
        if self.error is not None:
            raise self.error
        return _Job(self.rows)


def _manifest_file(tmp_path, operation="brain_read", *, version=V1):
    if version == V1:
        payload = v1_manifest(operation)
        raw = canonical(payload, "historical_read")
    else:
        payload = v2_manifest(operation)
        raw = canonical(payload, "new_approval")
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    return path, payload, hashlib.sha256(raw).hexdigest()


def test_public_signatures_accept_no_actor_time_phrase_or_clients():
    assert tuple(inspect.signature(approval_cli.review_manifest).parameters) == (
        "manifest_path",
        "expected_sha256",
    )
    assert tuple(inspect.signature(approval_cli.approve_manifest).parameters) == (
        "manifest_path",
        "expected_sha256",
    )
    assert (
        str(inspect.signature(approval_cli.main))
        == "(argv: 'Sequence[str] | None' = None) -> 'int'"
    )


def test_review_validates_exact_bytes_and_performs_no_cloud_call(tmp_path, monkeypatch):
    path, payload, digest = _manifest_file(tmp_path)
    monkeypatch.setattr(
        approval_cli,
        "_load_credentials",
        lambda: pytest.fail("review crossed credential boundary"),
    )
    receipt = approval_cli.review_manifest(path.resolve(), digest)
    assert receipt == {
        "contract_version": "open_intelligence_execution_review_v1",
        "manifest": payload,
        "manifest_sha256": digest,
        "operation": "brain_read",
    }


def test_review_refuses_relative_path_digest_drift_and_noncanonical_bytes(tmp_path):
    path, payload, digest = _manifest_file(tmp_path)
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_manifest_invalid"
    ):
        approval_cli.review_manifest(path.relative_to(tmp_path), digest)
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_manifest_mismatch"
    ):
        approval_cli.review_manifest(path.resolve(), "0" * 64)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_manifest_mismatch"
    ):
        approval_cli.review_manifest(path.resolve(), digest)


def test_approve_refuses_retained_v1_bytes_without_any_client_call(tmp_path, monkeypatch):
    path, payload, digest = _manifest_file(tmp_path)
    approved_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    phrase = (
        f"I approve one staging execution of brain_read for manifest SHA256 {digest}. "
        "Production remains unchanged."
    )
    approval_id = execution_approval.approval_id(digest, APPROVED_BY, approved_at)
    client = _Client(
        [
            {
                "contract_version": "open_intelligence_execution_approval_receipt_v1",
                "approval_id": approval_id,
                "approved_at": approved_at,
                "approved_by": APPROVED_BY,
                "expires_at": payload["expires_at"],
                "manifest_sha256": digest,
                "operation": "brain_read",
            }
        ]
    )
    monkeypatch.setattr(approval_cli, "_read_approval_phrase", lambda: phrase)
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: object())
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: client)
    with pytest.raises(approval_cli.ApprovalCliRefusal, match=r"^execution_origin_mode_forbidden$"):
        approval_cli.approve_manifest(path.resolve(), digest)
    assert client.calls == []
    assert "sp_approve_open_intelligence_execution_v1" in approval_cli._ROUTINES


def test_approval_error_is_not_retried(tmp_path, monkeypatch):
    path, _payload, digest = _manifest_file(tmp_path, version="v2")
    client = _Client([], error=RuntimeError("ambiguous transaction"))
    monkeypatch.setattr(
        approval_cli,
        "_read_approval_phrase",
        lambda: "I approve one 42 staging execution of wrong",
    )
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: object())
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: client)
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match="execution_approval_internal_refusal"
    ):
        approval_cli.approve_manifest(path.resolve(), digest)
    assert len(client.calls) == 1


def test_disable_uses_one_fixed_parameter_and_exact_readback(monkeypatch):
    disabled_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    client = _Client(
        [
            {
                "contract_version": "open_intelligence_execution_disable_receipt_v1",
                "state": "disabled",
                "disabled_at": disabled_at,
                "disabled_by": APPROVED_BY,
                "lock_version": 4,
            }
        ]
    )
    monkeypatch.setattr(
        approval_cli,
        "_read_approval_phrase",
        lambda: "I disable new 42 staging execution approvals. Production remains unchanged.",
    )
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: object())
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: client)
    receipt = approval_cli._disable_approvals(V1)
    assert receipt["state"] == "disabled"
    assert tuple(item.name for item in client.calls[0][1].query_parameters) == ("approval_phrase",)
    assert client.calls[0][2] == {"retry": None, "job_retry": None}


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["enable"],
        ["review", "--manifest-sha256", "a" * 64, "--manifest-file", "x"],
        ["approve", "--manifest-file", "x", "--manifest-sha256", "a" * 64],
        ["disable"],
        ["disable", "--approval-phrase-stdin"],
        ["approve-grant", "--grant-file", "x", "--grant-sha256", "a" * 64],
        [
            "revoke-grant",
            "--grant-file",
            "x",
            "--manifest-sha256",
            "a" * 64,
            "--approval-phrase-stdin",
        ],
        [
            "approve-grant",
            "--manifest-file",
            "x",
            "--grant-sha256",
            "a" * 64,
            "--approval-phrase-stdin",
        ],
    ],
)
def test_cli_grammar_refuses_unknown_missing_or_reordered_flags(argv):
    assert approval_cli.main(argv) == 2


@pytest.mark.parametrize("word", ["approve-grant", "revoke-grant"])
def test_grant_words_dispatch_to_their_own_functions_and_emit_the_receipt(
    word, monkeypatch, capsys
):
    seen = []
    monkeypatch.setattr(
        approval_cli,
        word.replace("-", "_"),
        lambda path, digest: seen.append((path, digest)) or {"operation": word},
    )
    argv = [word, "--grant-file", "grant.json", "--grant-sha256", "b" * 64]
    assert approval_cli.main([*argv, "--approval-phrase-stdin"]) == 0
    assert seen == [(approval_cli.Path("grant.json"), "b" * 64)]
    assert json.loads(capsys.readouterr().out) == {"operation": word}
    assert "sp_approve_open_intelligence_execution_v3" in approval_cli._ROUTINES
    source = approval_cli._routine_source("sp_approve_open_intelligence_execution_v3")
    assert "'open_intelligence_execution_approval_receipt_v3' AS contract_version" in source


def test_sql_procedures_return_exact_receipt_contract_versions():
    approve_sql = approval_cli._routine_source("sp_approve_open_intelligence_execution_v1")
    disable_sql = approval_cli._routine_source("sp_disable_open_intelligence_execution_approval_v1")
    assert "open_intelligence_execution_approval_receipt_v1" in approve_sql
    assert "open_intelligence_execution_disable_receipt_v1" in disable_sql
    assert "SESSION_USER()" in approve_sql
    assert "CURRENT_TIMESTAMP()" in approve_sql
    assert "@approved_by" not in approve_sql
    assert "@approved_at" not in approve_sql


def test_manifest_digest_is_sha256_of_exact_canonical_bytes(tmp_path):
    path, _payload, digest = _manifest_file(tmp_path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_phrase_reader_accepts_one_terminal_newline_in_a_real_process(ending):
    phrase = "I approve one staging execution of brain_read for manifest SHA256 " + "a" * 64
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from scripts.staging.approve_open_intelligence_execution import "
                "_read_approval_phrase; print(_read_approval_phrase())"
            ),
        ],
        input=(phrase + ending).encode(),
        capture_output=True,
        check=False,
    )
    assert process.returncode == 0
    assert process.stdout.decode().strip() == phrase
    assert process.stderr == b""


def test_phrase_reader_rejects_embedded_newline_in_a_real_process():
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from scripts.staging.approve_open_intelligence_execution import "
                "_read_approval_phrase; _read_approval_phrase()"
            ),
        ],
        input=b"first\nsecond\n",
        capture_output=True,
        check=False,
    )
    assert process.returncode != 0
    assert b"execution_approval_manifest_mismatch" in process.stderr


def test_review_cli_process_has_exact_channels_and_exit_codes(tmp_path):
    path, _payload, digest = _manifest_file(tmp_path)
    script = approval_cli.__file__
    argv = ["review", "--manifest-file", str(path.resolve()), "--manifest-sha256", digest]
    code = (
        "import tests.unit.test_operator_callers_v2 as t;"
        "import scripts.staging.approve_open_intelligence_execution as m;"
        "m._active_generation=t.generation;"
        f"raise SystemExit(m.main({argv!r}))"
    )
    success = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
    )
    assert success.returncode == 0
    assert success.stderr == b""
    assert success.stdout.endswith(b"\n")
    assert len(success.stdout.splitlines()) == 1
    grammar = subprocess.run(
        [sys.executable, script, "enable"],
        capture_output=True,
        check=False,
    )
    assert grammar.returncode == 2
    assert grammar.stdout == b""
    assert json.loads(grammar.stderr) == {"error": "execution_approval_manifest_invalid"}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("identity", "execution_approval_identity_invalid"),
        ("jobs", "execution_approval_identity_invalid"),
        ("procedure", "execution_approval_manifest_mismatch"),
    ],
)
def test_approval_process_has_bounded_identity_permission_and_procedure_failures(
    tmp_path,
    failure,
    expected,
):
    path, _payload, digest = _manifest_file(tmp_path, version="v2")
    argv = [
        "approve",
        "--manifest-file",
        str(path.resolve()),
        "--manifest-sha256",
        digest,
        "--approval-phrase-stdin",
    ]
    if failure == "identity":
        setup = (
            "m._load_credentials=lambda:(_ for _ in ()).throw("
            "m.ApprovalCliRefusal('execution_approval_identity_invalid'))"
        )
    else:
        message = (
            "missing bigquery.jobs.create"
            if failure == "jobs"
            else "execution_approval_manifest_mismatch"
        )
        setup = (
            "m._load_credentials=lambda:object();"
            "m._bigquery_client=lambda _credentials:type('C',(),{"
            f"'query':lambda self,*a,**k:(_ for _ in ()).throw(RuntimeError('{message}'))"
            "})()"
        )
    code = (
        "import tests.unit.test_operator_callers_v2 as t;"
        "import scripts.staging.approve_open_intelligence_execution as m;"
        "m._active_generation=t.generation;" + setup + f";raise SystemExit(m.main({argv!r}))"
    )
    process = subprocess.run(
        [sys.executable, "-c", code],
        input=b"I approve one 42 staging execution of valid phrase\n",
        capture_output=True,
        check=False,
    )
    assert process.returncode == 1
    assert process.stdout == b""
    assert json.loads(process.stderr) == {"error": expected}


def test_approve_refuses_a_grant_phrase_before_any_client_call(tmp_path, monkeypatch):
    """The approve word names an execution manifest; a grant phrase is another kind."""
    from src.analysis.open_intelligence import recurring_grant_phrases

    path, _payload, digest = _manifest_file(tmp_path, version="v2")
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: pytest.fail("client built"))
    for phrase in (
        recurring_grant_phrases.approval_phrase("c" * 64),
        recurring_grant_phrases.revocation_phrase("recurring_execution_20260915_v1"),
        "I approve one staging execution of brain_read for manifest SHA256 " + digest,
    ):
        monkeypatch.setattr(approval_cli, "_read_approval_phrase", lambda phrase=phrase: phrase)
        with pytest.raises(
            approval_cli.ApprovalCliRefusal, match=r"^approval_phrase_kind_mismatch$"
        ):
            approval_cli.approve_manifest(path.resolve(), digest)
