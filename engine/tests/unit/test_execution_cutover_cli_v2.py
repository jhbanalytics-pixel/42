"""Operator cutover words: exact grammar, dry run, read only preflight and guarded apply."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    ROOT
    / "infra"
    / "bigquery_routines"
    / "sp_activate_open_intelligence_execution_generation_v2_cutover_script.sql"
)
SCRIPT_SHA256 = hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest()
# The plan's active pair is the code's active generation, the bridge generation.
REGISTRY_SHA = migration.REGISTRATION_DIGESTS["execution_origins_bridge_v3.json"]
MANIFEST_SHA = migration.REGISTRATION_DIGESTS["resource_manifest_bridge_v3.json"]
PHRASE = (
    "I activate one 42 staging execution generation for origin registry SHA256 "
    f"{REGISTRY_SHA} and resource manifest SHA256 {MANIFEST_SHA}. Production remains unchanged."
)
RESIDUAL_IDS = ("exc_" + "b" * 64, "exc_" + "a" * 64)
RESIDUAL_JSON = json.dumps(sorted(RESIDUAL_IDS), separators=(",", ":"))
RESIDUAL_DIGEST = hashlib.sha256(RESIDUAL_JSON.encode("utf-8")).hexdigest()
WORDS = ("cutover-v2-preflight", "cutover-v2-dry-run", "cutover-v2-apply")
GRAMMAR_ERROR = b'{"error":"execution_approval_manifest_invalid"}\n'
PROPOSAL_SKIP_REASON = "reviewed proposal packet is external to the source checkout"
EXPECTED_PRE_DML_REFUSALS = (
    "execution_approval_identity_invalid",
    "execution_approval_generation_invalid",
    "execution_approval_manifest_mismatch",
    "execution_approval_v1_lock_not_disabled",
    "execution_approval_lock_not_prepared",
    "execution_approval_generation_already_active",
    "execution_approval_generation_invalid",
    "execution_approval_generation_invalid",
    "execution_origin_registry_digest_mismatch",
    "execution_origin_registry_invalid",
    "execution_approval_generation_invalid",
    "execution_approval_generation_invalid",
    "execution_origin_policy_invalid",
    "execution_approval_residual_invalid",
    "execution_approval_residual_invalid",
    "execution_approval_residual_invalid",
    "execution_approval_residual_invalid",
    "execution_approval_residual_invalid",
)
EXPECTED_POST_DML_REFUSALS = (
    "execution_approval_generation_invalid",
    "execution_approval_concurrent_conflict",
    "execution_approval_lock_invalid",
)


def _proposal_resource_manifest() -> Path:
    proposal_dir_value = os.environ.get("R03_CANONICAL_PROPOSAL_DIR")
    if proposal_dir_value is None:
        pytest.skip(PROPOSAL_SKIP_REASON)
    return Path(proposal_dir_value) / "resource_manifest.json"


def _stub_plan() -> SimpleNamespace:
    return SimpleNamespace(active_pair=(REGISTRY_SHA, MANIFEST_SHA))


def _write_inputs(
    tmp_path: Path,
    *,
    residual_ids=RESIDUAL_IDS,
    disable_lock_version: object = 470,
    disable_receipt: object = None,
    inactivity_receipt: object = None,
    phrase: str = PHRASE,
) -> dict[str, Path]:
    paths = {
        "residuals": tmp_path / "residuals.json",
        "disable": tmp_path / "v1-disable-receipt.json",
        "inactivity": tmp_path / "inactivity-receipt.json",
        "phrase": tmp_path / "activation-phrase.txt",
        "output": tmp_path / "receipt.json",
    }
    paths["residuals"].write_text(
        json.dumps(
            {
                "residuals": [
                    {"consumption_id": item, "operation": "brain_read"} for item in residual_ids
                ]
            }
        ),
        encoding="utf-8",
    )
    if disable_receipt is None:
        disable_receipt = {
            "contract_version": "open_intelligence_execution_disable_receipt_v1",
            "lock_version": disable_lock_version,
            "state": "disabled",
        }
    if inactivity_receipt is None:
        inactivity_receipt = {
            "contract_version": "execution_inactivity_receipt_v1",
            "active_executions": 0,
        }
    paths["disable"].write_text(json.dumps(disable_receipt, indent=2), encoding="utf-8")
    paths["inactivity"].write_text(json.dumps(inactivity_receipt, indent=2), encoding="utf-8")
    paths["phrase"].write_text(phrase + "\n", encoding="utf-8")
    return paths


def _arguments(
    word: str,
    paths: dict[str, Path],
    *,
    lock_version: str = "470",
    phase: str | None = None,
) -> list[str]:
    if word == "cutover-v2-preflight" and phase is None:
        phase = "post-disable"
    extra = [] if phase is None else ["--phase", phase]
    return [
        word,
        "--residuals",
        str(paths["residuals"]),
        "--v1-disable-receipt",
        str(paths["disable"]),
        "--inactivity-receipt",
        str(paths["inactivity"]),
        "--expected-v1-lock-version",
        lock_version,
        "--output",
        str(paths["output"]),
        "--activation-phrase-file",
        str(paths["phrase"]),
        *extra,
    ]


class _Job:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeClient:
    """Answers the session user probe, the v1 lock read and one row per preflight check."""

    def __init__(
        self,
        *,
        failures=(),
        errors=(),
        session_user="operator@example.test",
        lock_version=470,
        lock_state="disabled",
        lock_rows=1,
    ):
        self.failures = set(failures)
        self.errors = set(errors)
        self.session_user = session_user
        self.lock_version = lock_version
        self.lock_state = lock_state
        self.lock_rows = lock_rows
        self.by_sql = {check.sql: check.name for check in migration.cutover_preflight_checks()}
        self.queries: list[tuple[str, object]] = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        if sql == "SELECT SESSION_USER() AS session_user":
            return _Job(({"session_user": self.session_user},))
        if sql.startswith("SELECT lock_name, ") and sql.endswith(
            "open_intelligence_execution_approval_lock_v1`"
        ):
            row = {
                "lock_name": "open_intelligence_execution_approval_v1",
                "approval_contract_version": "open_intelligence_execution_approval_v1",
                "lock_version": self.lock_version,
                "state": self.lock_state,
                "last_approval_id": None,
                "created_at": None,
                "updated_at": None,
            }
            return _Job(tuple(dict(row) for _ in range(self.lock_rows)))
        name = self.by_sql[sql]
        if name in self.errors:
            raise RuntimeError("query failed")
        return _Job(({"ok": name not in self.failures},))


def _install_fake(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(migration, "build_v2_plan", _stub_plan)
    monkeypatch.setattr(migration, "_load_credentials", lambda: object())
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)


def _forbid_native(monkeypatch) -> None:
    def _refuse(*_args, **_kwargs):
        raise AssertionError("native client constructed")

    monkeypatch.setattr(migration, "_load_credentials", _refuse)
    monkeypatch.setattr(migration, "_bigquery_client", _refuse)


def _check_names() -> tuple[str, ...]:
    return tuple(check.name for check in migration.cutover_preflight_checks())


# Grammar


def _malformed_cases(paths: dict[str, Path]) -> dict[str, list[str]]:
    base = _arguments("cutover-v2-dry-run", paths)
    cases = {
        "word_alone": ["cutover-v2-dry-run"],
        "missing_option": base[:-2],
        "extra_positional": [*base, "extra"],
        "repeated_option": [*base[:-2], "--residuals", str(paths["residuals"])],
        "unknown_option": [*base[:-2], "--preflight-receipt", str(paths["output"])],
        "odd_count": [*base, "--output"],
        "unknown_word": ["cutover-v3-apply", *base[1:]],
        "negative_lock_version": _arguments("cutover-v2-apply", paths, lock_version="-1"),
        "decimal_lock_version": _arguments("cutover-v2-apply", paths, lock_version="1.0"),
        "signed_lock_version": _arguments("cutover-v2-apply", paths, lock_version="+470"),
        "padded_lock_version": _arguments("cutover-v2-apply", paths, lock_version="0470"),
        "blank_lock_version": _arguments("cutover-v2-preflight", paths, lock_version=""),
        "empty_path_value": [*base[:-1], ""],
        "existing_word_with_options": ["apply-v2", *base[1:]],
        "existing_word_extra": ["plan", "extra"],
        "preflight_without_phase": _arguments("cutover-v2-preflight", paths)[:-2],
        "preflight_unknown_phase": _arguments("cutover-v2-preflight", paths, phase="both"),
        "preflight_blank_phase": _arguments("cutover-v2-preflight", paths, phase=""),
        "dry_run_with_phase": _arguments("cutover-v2-dry-run", paths, phase="pre-disable"),
        "apply_with_phase": _arguments("cutover-v2-apply", paths, phase="post-disable"),
    }
    return cases


def test_parser_refuses_each_malformed_grammar_with_exit_2(tmp_path, monkeypatch, capfd):
    _forbid_native(monkeypatch)
    monkeypatch.setattr(migration, "build_v2_plan", _stub_plan)
    paths = _write_inputs(tmp_path)
    for label, arguments in _malformed_cases(paths).items():
        assert migration.main(arguments) == 2, label
        captured = capfd.readouterr()
        assert captured.out == "", label
        assert captured.err.encode("utf-8") == GRAMMAR_ERROR, label
    assert not paths["output"].exists()


def test_words_and_options_are_exact(monkeypatch):
    assert migration.CUTOVER_WORDS == WORDS
    assert migration.CUTOVER_OPTIONS == (
        "--residuals",
        "--v1-disable-receipt",
        "--inactivity-receipt",
        "--expected-v1-lock-version",
        "--output",
        "--activation-phrase-file",
    )
    assert migration.CUTOVER_PREFLIGHT_OPTIONS[:-1] == migration.CUTOVER_OPTIONS
    assert migration.CUTOVER_PREFLIGHT_OPTIONS[-1] == "--phase"
    assert migration.CUTOVER_PHASES == ("pre-disable", "post-disable")


# Dry run


def test_dry_run_prints_the_script_sha256_and_never_opens_a_client(tmp_path, monkeypatch, capfd):
    _forbid_native(monkeypatch)
    monkeypatch.setattr(migration, "build_v2_plan", _stub_plan)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("cutover-v2-dry-run", paths)) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "contract_version": "execution_cutover_dry_run_v1",
        "expected_v1_lock_version": 470,
        "parameters": {
            "activation_phrase": PHRASE,
            "origin_registry_sha256": REGISTRY_SHA,
            "residual_consumption_ids_json": {"count": 2, "sha256": RESIDUAL_DIGEST},
            "residual_set_sha256": RESIDUAL_DIGEST,
            "resource_manifest_sha256": MANIFEST_SHA,
        },
        "script_path": (
            "infra/bigquery_routines/"
            "sp_activate_open_intelligence_execution_generation_v2_cutover_script.sql"
        ),
        "script_sha256": SCRIPT_SHA256,
    }
    assert "exc_" not in captured.out
    assert paths["output"].read_bytes() == (lines[0] + "\n").encode("utf-8")


def test_dry_run_keeps_the_pinned_residual_digest_method(tmp_path, monkeypatch, capfd):
    _forbid_native(monkeypatch)
    monkeypatch.setattr(migration, "build_v2_plan", _stub_plan)
    paths = _write_inputs(tmp_path)
    parameters = {
        name: value
        for name, _kind, value in migration.v2_cutover_parameters(
            _stub_plan(), residual_consumption_ids=RESIDUAL_IDS, activation_phrase=PHRASE
        )
    }
    assert parameters["residual_consumption_ids_json"] == RESIDUAL_JSON
    assert parameters["residual_set_sha256"] == RESIDUAL_DIGEST

    assert migration.main(_arguments("cutover-v2-dry-run", paths)) == 0

    payload = json.loads(capfd.readouterr().out)
    assert payload["parameters"]["residual_set_sha256"] == RESIDUAL_DIGEST
    assert payload["parameters"]["residual_consumption_ids_json"]["sha256"] == RESIDUAL_DIGEST


def test_dry_run_refuses_a_malformed_residual_file_before_any_plan(tmp_path, monkeypatch, capfd):
    _forbid_native(monkeypatch)
    monkeypatch.setattr(migration, "build_v2_plan", _stub_plan)
    paths = _write_inputs(tmp_path, residual_ids=("exc_" + "a" * 64, "exc_" + "a" * 64))
    assert migration.main(_arguments("cutover-v2-dry-run", paths)) == 1
    assert capfd.readouterr().err == '{"error":"execution_approval_residual_invalid"}\n'
    paths["residuals"].write_text('{"residuals": [{"operation": "brain_read"}]}', encoding="utf-8")
    assert migration.main(_arguments("cutover-v2-dry-run", paths)) == 1
    assert capfd.readouterr().err == '{"error":"execution_approval_residual_invalid"}\n'
    paths["residuals"].write_text("{not json", encoding="utf-8")
    assert migration.main(_arguments("cutover-v2-dry-run", paths)) == 1
    assert capfd.readouterr().err == '{"error":"execution_approval_residual_invalid"}\n'
    assert not paths["output"].exists()


# Preflight checks derived from the script


def test_preflight_checks_cover_every_assert_before_the_first_dml_as_read_only_selects():
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    before, separator, after = text.partition("\n  INSERT INTO ")
    assert separator
    checks = migration.cutover_preflight_checks()
    post = migration.cutover_post_mutation_asserts()
    assert len(checks) == before.count("ASSERT ") == 18
    assert len(post) == after.count("ASSERT ") == 3
    assert tuple(check.refusal for check in checks) == EXPECTED_PRE_DML_REFUSALS
    assert tuple(item.refusal for item in post) == EXPECTED_POST_DML_REFUSALS
    assert [check.name for check in checks] == [
        f"assert_{index:02d}_{code}"
        for index, code in enumerate(EXPECTED_PRE_DML_REFUSALS, start=1)
    ]
    assert [item.name for item in post] == [
        f"assert_{index:02d}_{code}"
        for index, code in enumerate(EXPECTED_POST_DML_REFUSALS, start=19)
    ]
    forbidden = re.compile(
        r"\b(INSERT|UPDATE|DELETE|MERGE|BEGIN|COMMIT|ROLLBACK|DECLARE|SET|ASSERT|CREATE|DROP|"
        r"TRUNCATE|ALTER)\b"
    )
    for check in checks:
        assert check.sql.startswith("SELECT (")
        assert check.sql.endswith(") AS ok")
        assert forbidden.search(check.sql) is None, check.name
        assert re.search(r"\bv_[a-z0-9_]+\b", check.sql) is None, check.name
        assert "{project}" not in check.sql
        assert "{dataset}" not in check.sql
        assert "@@" not in check.sql
        assert check.sql_sha256 == hashlib.sha256(check.sql.encode("utf-8")).hexdigest()
        referenced = {
            name
            for name in migration.V2_CUTOVER_PARAMETERS
            if re.search(rf"(?<!@)@{name}\b", check.sql)
        }
        assert set(check.parameters) == referenced, check.name
    assert f"`{migration.PROJECT}.{migration.DATASET}." in checks[3].sql
    assert "SESSION_USER()" in checks[0].sql
    assert "@activation_phrase = FORMAT(" in checks[2].sql
    assert "CURRENT_TIMESTAMP()" in checks[15].sql
    assert "JSON_VALUE_ARRAY(@residual_consumption_ids_json)" in checks[13].sql


# Preflight execution


def _preflight(client, *, expected=470, phase="post-disable"):
    return migration._run_cutover_preflight(
        _stub_plan(),
        object(),
        residual_consumption_ids=RESIDUAL_IDS,
        activation_phrase=PHRASE,
        expected_v1_lock_version=expected,
        phase=phase,
    )


PRE_DISABLE_ASSERT = "assert_04_execution_approval_v1_lock_not_disabled"


def test_preflight_post_disable_ready_receipt_hides_residual_identifiers(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    assert receipt["contract_version"] == "execution_cutover_preflight_v1"
    assert receipt["state"] == "ready"
    assert receipt["phase"] == "post-disable"
    assert receipt["expected_v1_lock"] == {"lock_version": 470, "state": "disabled"}
    assert receipt["refusal"] is None
    assert receipt["failed_checks"] == []
    assert receipt["script_sha256"] == SCRIPT_SHA256
    assert receipt["session_user"] == "operator@example.test"
    assert receipt["expected_v1_lock_version"] == 470
    assert receipt["observed_v1_lock"] == {"lock_version": 470, "state": "disabled"}
    assert receipt["parameters"] == {
        "activation_phrase": PHRASE,
        "origin_registry_sha256": REGISTRY_SHA,
        "residual_consumption_ids_json": {"count": 2, "sha256": RESIDUAL_DIGEST},
        "residual_set_sha256": RESIDUAL_DIGEST,
        "resource_manifest_sha256": MANIFEST_SHA,
    }
    assert [item["name"] for item in receipt["checks"]] == [
        *_check_names(),
        "v1_lock_expected",
    ]
    assert all(item["result"] == "pass" for item in receipt["checks"])
    assert receipt["checks"][-1]["refusal"] == "execution_approval_lock_invalid"
    assert [item["name"] for item in receipt["post_mutation_asserts"]] == [
        item.name for item in migration.cutover_post_mutation_asserts()
    ]
    assert all(item["evaluated"] is False for item in receipt["post_mutation_asserts"])
    serialised = json.dumps(receipt)
    assert "exc_" not in serialised
    assert client.queries[0][0] == "SELECT SESSION_USER() AS session_user"
    assert len(client.queries) == 1 + len(_check_names()) + 1
    for sql, job_config in client.queries:
        assert not any(item in sql for item in RESIDUAL_IDS)
        if job_config is not None:
            for parameter in job_config.query_parameters:
                assert parameter.type_ == "STRING"
                if parameter.name == "residual_consumption_ids_json":
                    assert parameter.value == RESIDUAL_JSON
    residual_checks = [
        job_config
        for sql, job_config in client.queries
        if sql in client.by_sql and "@residual_consumption_ids_json" in sql
    ]
    assert residual_checks
    assert all(
        any(item.name == "residual_consumption_ids_json" for item in config.query_parameters)
        for config in residual_checks
    )


@pytest.mark.parametrize("failing", _check_names())
def test_preflight_refuses_each_failed_check_with_the_script_refusal_code(monkeypatch, failing):
    client = _FakeClient(failures=(failing,))
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    by_name = {item["name"]: item for item in receipt["checks"]}
    assert receipt["state"] == "refused"
    assert receipt["failed_checks"] == [failing]
    assert by_name[failing]["result"] == "fail"
    assert receipt["refusal"] == by_name[failing]["refusal"]
    assert receipt["refusal"] == failing.split("_", 2)[2]
    assert all(item["result"] == "pass" for name, item in by_name.items() if name != failing)


def test_preflight_pre_disable_ready_for_disable_path(monkeypatch):
    client = _FakeClient(failures=(PRE_DISABLE_ASSERT,), lock_version=469, lock_state="ready")
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client, expected=469, phase="pre-disable")

    assert receipt["state"] == "ready_for_disable"
    assert receipt["phase"] == "pre-disable"
    assert receipt["refusal"] is None
    assert receipt["failed_checks"] == [PRE_DISABLE_ASSERT]
    assert receipt["expected_v1_lock"] == {"lock_version": 469, "state": "ready"}
    assert receipt["observed_v1_lock"] == {"lock_version": 469, "state": "ready"}
    by_name = {item["name"]: item for item in receipt["checks"]}
    assert by_name["v1_lock_expected"]["result"] == "pass"
    assert by_name[PRE_DISABLE_ASSERT]["result"] == "fail"


@pytest.mark.parametrize(
    "lock_version, lock_state, failures, refusal",
    [
        (469, "disabled", (), "execution_approval_lock_invalid"),
        (470, "ready", (PRE_DISABLE_ASSERT,), "execution_approval_lock_invalid"),
        (469, "ready", (), "execution_approval_lock_invalid"),
        (
            469,
            "ready",
            (PRE_DISABLE_ASSERT, "assert_06_execution_approval_generation_already_active"),
            "execution_approval_generation_already_active",
        ),
    ],
)
def test_preflight_pre_disable_refuses_wrong_lock_state_version_or_extra_failure(
    monkeypatch, lock_version, lock_state, failures, refusal
):
    client = _FakeClient(failures=failures, lock_version=lock_version, lock_state=lock_state)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client, expected=469, phase="pre-disable")

    assert receipt["state"] == "refused"
    assert receipt["refusal"] == refusal


@pytest.mark.parametrize(
    "lock_version, lock_state",
    [(470, "ready"), (471, "disabled"), (469, "disabled")],
)
def test_preflight_post_disable_refuses_wrong_lock_state_or_version(
    monkeypatch, lock_version, lock_state
):
    client = _FakeClient(lock_version=lock_version, lock_state=lock_state)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client, expected=470, phase="post-disable")

    assert receipt["state"] == "refused"
    assert receipt["refusal"] == "execution_approval_lock_invalid"
    assert receipt["failed_checks"] == ["v1_lock_expected"]
    assert receipt["observed_v1_lock"] == {"lock_version": lock_version, "state": lock_state}


def test_preflight_refuses_an_unknown_phase(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_approval_manifest_invalid$"):
        _preflight(client, phase="both")
    assert client.queries == []


def test_preflight_fails_closed_when_a_check_query_errors_or_the_lock_row_is_missing(
    monkeypatch,
):
    names = _check_names()
    client = _FakeClient(errors=(names[8],), lock_rows=0)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    by_name = {item["name"]: item for item in receipt["checks"]}
    assert receipt["state"] == "refused"
    assert by_name[names[8]] == {
        **by_name[names[8]],
        "result": "fail",
        "error": "query_failed",
        "refusal": "execution_origin_registry_digest_mismatch",
    }
    assert by_name["v1_lock_expected"]["result"] == "fail"
    assert receipt["observed_v1_lock"] is None
    assert receipt["failed_checks"] == [names[8], "v1_lock_expected"]
    assert receipt["refusal"] == "execution_origin_registry_digest_mismatch"


def test_preflight_word_writes_the_receipt_and_exits_by_state(tmp_path, monkeypatch, capfd):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("cutover-v2-preflight", paths)) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    receipt = json.loads(captured.out)
    assert receipt["state"] == "ready"
    assert paths["output"].read_bytes() == captured.out.encode("utf-8")

    refused = _FakeClient(failures=("assert_04_execution_approval_v1_lock_not_disabled",))
    _install_fake(monkeypatch, refused)
    paths["output"].unlink()
    assert migration.main(_arguments("cutover-v2-preflight", paths)) == 1
    captured = capfd.readouterr()
    assert captured.err == '{"error":"execution_approval_v1_lock_not_disabled"}\n'
    receipt = json.loads(captured.out)
    assert receipt["state"] == "refused"
    assert receipt["failed_checks"] == ["assert_04_execution_approval_v1_lock_not_disabled"]
    assert json.loads(paths["output"].read_text(encoding="utf-8")) == receipt


def test_pre_disable_preflight_word_needs_no_receipt_files_yet(tmp_path, monkeypatch, capfd):
    client = _FakeClient(failures=(PRE_DISABLE_ASSERT,), lock_version=469, lock_state="ready")
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)
    paths["disable"].unlink()
    paths["inactivity"].unlink()
    arguments = _arguments("cutover-v2-preflight", paths, lock_version="469", phase="pre-disable")

    assert migration.main(arguments) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    receipt = json.loads(captured.out)
    assert receipt["state"] == "ready_for_disable"
    assert receipt["observed_v1_lock"] == {"lock_version": 469, "state": "ready"}
    assert json.loads(paths["output"].read_text(encoding="utf-8")) == receipt


# Apply


def _record_apply(monkeypatch, *, result=None, refusal=None):
    calls = []

    def _fake(plan, credentials, *, residual_consumption_ids, activation_phrase):
        calls.append((plan, credentials, tuple(residual_consumption_ids), activation_phrase))
        if refusal is not None:
            raise migration.MigrationRefusal(refusal)
        return dict(result)

    monkeypatch.setattr(migration, "_apply_v2_cutover", _fake)
    return calls


ACTIVATION = {
    "contract_version": "open_intelligence_execution_activation_receipt_v2",
    "state": "ready",
    "activated_at": "2026-09-13T10:00:00.000000Z",
    "activated_by": "usr_" + "7" * 64,
    "lock_version": 1,
    "origin_registry_sha256": REGISTRY_SHA,
    "resource_manifest_sha256": MANIFEST_SHA,
}


def test_apply_refuses_without_a_ready_preflight(tmp_path, monkeypatch, capfd):
    client = _FakeClient(failures=("assert_06_execution_approval_generation_already_active",))
    _install_fake(monkeypatch, client)
    calls = _record_apply(monkeypatch, result=ACTIVATION)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("cutover-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert captured.err == '{"error":"execution_approval_generation_already_active"}\n'
    assert calls == []
    receipt = json.loads(captured.out)
    assert receipt["contract_version"] == "execution_cutover_apply_v1"
    assert receipt["state"] == "refused"
    assert receipt["activation"] is None
    assert receipt["preflight"]["state"] == "refused"
    assert json.loads(paths["output"].read_text(encoding="utf-8")) == receipt
    assert "exc_" not in captured.out


def test_apply_has_no_file_input_for_a_preflight_receipt():
    assert not any("preflight" in option for option in migration.CUTOVER_PREFLIGHT_OPTIONS)


def test_apply_refuses_a_pre_disable_receipt_even_when_ready_for_disable(
    tmp_path, monkeypatch, capfd
):
    client = _FakeClient(failures=(PRE_DISABLE_ASSERT,), lock_version=469, lock_state="ready")
    _install_fake(monkeypatch, client)
    calls = _record_apply(monkeypatch, result=ACTIVATION)
    real_preflight = migration._run_cutover_preflight
    asked = []

    def _pre_disable_preflight(plan, credentials, **kwargs):
        asked.append(kwargs["phase"])
        return real_preflight(plan, credentials, **{**kwargs, "phase": "pre-disable"})

    monkeypatch.setattr(migration, "_run_cutover_preflight", _pre_disable_preflight)
    paths = _write_inputs(tmp_path, disable_lock_version=469)

    assert migration.main(_arguments("cutover-v2-apply", paths, lock_version="469")) == 1

    captured = capfd.readouterr()
    assert asked == ["post-disable"]
    assert captured.err == '{"error":"execution_approval_lock_invalid"}\n'
    receipt = json.loads(captured.out)
    assert receipt["preflight"]["state"] == "ready_for_disable"
    assert receipt["preflight"]["phase"] == "pre-disable"
    assert receipt["state"] == "refused"
    assert receipt["activation"] is None
    assert calls == []


def test_apply_refuses_on_a_lock_version_mismatch_before_any_client(tmp_path, monkeypatch, capfd):
    _forbid_native(monkeypatch)
    monkeypatch.setattr(migration, "build_v2_plan", _stub_plan)
    calls = _record_apply(monkeypatch, result=ACTIVATION)
    paths = _write_inputs(tmp_path, disable_lock_version=469)

    assert migration.main(_arguments("cutover-v2-apply", paths)) == 1

    assert capfd.readouterr().err == '{"error":"execution_approval_lock_invalid"}\n'
    assert calls == []
    assert not paths["output"].exists()

    client = _FakeClient(lock_version=471)
    _install_fake(monkeypatch, client)
    paths["disable"].write_text(
        json.dumps(
            {
                "contract_version": "open_intelligence_execution_disable_receipt_v1",
                "lock_version": 470,
            }
        ),
        encoding="utf-8",
    )
    assert migration.main(_arguments("cutover-v2-apply", paths)) == 1
    captured = capfd.readouterr()
    assert captured.err == '{"error":"execution_approval_lock_invalid"}\n'
    assert json.loads(captured.out)["preflight"]["failed_checks"] == ["v1_lock_expected"]
    assert calls == []


@pytest.mark.parametrize(
    "disable_receipt, inactivity_receipt",
    [
        ({"lock_version": 470}, None),
        ({"contract_version": "", "lock_version": 470}, None),
        ({"contract_version": "x", "lock_version": "470"}, None),
        ({"contract_version": "x", "lock_version": True}, None),
        (None, {"active_executions": 0}),
        (None, {"contract_version": 1}),
    ],
)
def test_apply_refuses_receipt_files_without_contract_fields(
    tmp_path, monkeypatch, capfd, disable_receipt, inactivity_receipt
):
    _forbid_native(monkeypatch)
    monkeypatch.setattr(migration, "build_v2_plan", _stub_plan)
    calls = _record_apply(monkeypatch, result=ACTIVATION)
    paths = _write_inputs(
        tmp_path, disable_receipt=disable_receipt, inactivity_receipt=inactivity_receipt
    )

    assert migration.main(_arguments("cutover-v2-apply", paths)) == 1

    error = json.loads(capfd.readouterr().err)["error"]
    assert error in {"execution_approval_manifest_invalid", "execution_approval_lock_invalid"}
    assert calls == []
    assert not paths["output"].exists()


@pytest.mark.parametrize("word", WORDS)
def test_existing_output_path_refuses_before_any_read(tmp_path, monkeypatch, capfd, word):
    _forbid_native(monkeypatch)
    monkeypatch.setattr(migration, "build_v2_plan", _stub_plan)
    calls = _record_apply(monkeypatch, result=ACTIVATION)
    paths = _write_inputs(tmp_path)
    paths["output"].write_bytes(b"keep")
    for key in ("residuals", "disable", "inactivity", "phrase"):
        paths[key].unlink()

    assert migration.main(_arguments(word, paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"output_exists"}\n'
    assert paths["output"].read_bytes() == b"keep"
    assert calls == []


def test_apply_calls_the_cutover_once_on_the_ready_path(tmp_path, monkeypatch, capfd):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    calls = _record_apply(monkeypatch, result=ACTIVATION)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("cutover-v2-apply", paths)) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    assert len(calls) == 1
    plan, _credentials, residual_ids, phrase = calls[0]
    assert plan.active_pair == (REGISTRY_SHA, MANIFEST_SHA)
    assert residual_ids == RESIDUAL_IDS
    assert phrase == PHRASE
    rendered = migration.v2_cutover_parameters(
        plan, residual_consumption_ids=residual_ids, activation_phrase=phrase
    )
    receipt = json.loads(captured.out)
    assert receipt["contract_version"] == "execution_cutover_apply_v1"
    assert receipt["state"] == "activated"
    assert receipt["refusal"] is None
    assert receipt["activation"] == ACTIVATION
    assert receipt["preflight"]["state"] == "ready"
    assert receipt["preflight"]["phase"] == "post-disable"
    assert receipt["script_sha256"] == SCRIPT_SHA256
    assert receipt["expected_v1_lock_version"] == 470
    assert receipt["parameters"] == migration._redacted_cutover_parameters(rendered)
    assert receipt["v1_disable_receipt"] == {
        "contract_version": "open_intelligence_execution_disable_receipt_v1",
        "lock_version": 470,
        "sha256": hashlib.sha256(paths["disable"].read_bytes()).hexdigest(),
    }
    assert receipt["inactivity_receipt"] == {
        "contract_version": "execution_inactivity_receipt_v1",
        "sha256": hashlib.sha256(paths["inactivity"].read_bytes()).hexdigest(),
    }
    assert paths["output"].read_bytes() == captured.out.encode("utf-8")
    assert "exc_" not in captured.out
    assert client.queries[0][0] == "SELECT SESSION_USER() AS session_user"


def test_apply_records_a_native_refusal_in_the_receipt(tmp_path, monkeypatch, capfd):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    calls = _record_apply(monkeypatch, refusal="execution_approval_concurrent_conflict")
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("cutover-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert captured.err == '{"error":"execution_approval_concurrent_conflict"}\n'
    assert len(calls) == 1
    receipt = json.loads(captured.out)
    assert receipt["state"] == "refused"
    assert receipt["refusal"] == "execution_approval_concurrent_conflict"
    assert receipt["preflight"]["state"] == "ready"
    assert receipt["activation"] is None


# Plan rendering


def test_render_v2_plan_no_longer_carries_an_iam_derivation(monkeypatch):
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", _proposal_resource_manifest())
    plan = migration.build_v2_plan()
    payload = json.loads(migration.render_v2_plan(plan))
    assert "iam_plan" not in payload
    assert set(payload) == {
        "active_pair",
        "dataset",
        "lock_seed",
        "proof_kind",
        "registrations",
        "routines",
        "tables",
    }


# Subprocess


def test_subprocess_dry_run_prints_the_exact_stdout_line(tmp_path):
    proposal = _proposal_resource_manifest()
    paths = _write_inputs(tmp_path)
    bootstrap = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from scripts.migrations import create_open_intelligence_execution_approval_store as m; "
        "m.RESOURCE_MANIFEST_PATH = Path(sys.argv[2]); raise SystemExit(m.main(sys.argv[3:]))"
    )
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            bootstrap,
            str(ROOT),
            str(proposal),
            *_arguments("cutover-v2-dry-run", paths),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    expected = (
        json.dumps(
            {
                "contract_version": "execution_cutover_dry_run_v1",
                "expected_v1_lock_version": 470,
                "parameters": {
                    "activation_phrase": PHRASE,
                    "origin_registry_sha256": REGISTRY_SHA,
                    "residual_consumption_ids_json": {"count": 2, "sha256": RESIDUAL_DIGEST},
                    "residual_set_sha256": RESIDUAL_DIGEST,
                    "resource_manifest_sha256": MANIFEST_SHA,
                },
                "script_path": (
                    "infra/bigquery_routines/"
                    "sp_activate_open_intelligence_execution_generation_v2_cutover_script.sql"
                ),
                "script_sha256": SCRIPT_SHA256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    assert process.returncode == 0, process.stderr
    assert process.stderr == b""
    assert process.stdout == expected
    assert paths["output"].read_bytes() == expected


def test_subprocess_grammar_refusal_from_the_script_file(tmp_path):
    process = subprocess.run(
        [sys.executable, str(migration.__file__), "cutover-v2-dry-run", "--residuals", "x"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert process.stdout == b""
    assert process.stderr == GRAMMAR_ERROR
