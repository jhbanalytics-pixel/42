"""Operator rotation words: exact grammar, dry run, read only preflight and guarded apply."""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration
from src.analysis.open_intelligence import execution_generations

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    ROOT
    / "infra"
    / "bigquery_routines"
    / "sp_rotate_open_intelligence_execution_generation_v2_script.sql"
)
FIRST_CUTOVER_PATH = (
    ROOT
    / "infra"
    / "bigquery_routines"
    / "sp_activate_open_intelligence_execution_generation_v2_cutover_script.sql"
)
SCRIPT_SHA256 = "edb954d0cad46a1d319f389a4ae225a2baf435beb65dac9fb4b44da1df7e77b9"
RETIRE_REGISTRY = "38d7db4b78b89e091a1c24fe3817df2e81d0a25fd71945326074adf1501a9f26"
RETIRE_MANIFEST = "f8ef93da7f4108bac7274d254648106d6c1ce6ce6ff32513d3a576ee9fe260e2"
# The code's active generation pair: the only pair a rotation may activate.
# Amendment g added the route A capture's artifact bucket to the bridge manifest, so its
# digest moved from 1643e4ce to ab7802f4; the registry stays 573deeea.
# The daily contract revision then registered three daily operations
# and bounded exposure, so the pair moved again: registry 573deeea to e6b95e35, manifest
# ab7802f4 to b7553041.
# Tightening the date regex to ASCII digits then moved them to f23001ed and 592ed45f.
ACTIVATE_REGISTRY = "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2"
ACTIVATE_MANIFEST = "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe"
ACTOR = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
# Trusted for reading but not the active pair: a rotation may never activate it.
AMENDMENT_E_PAIR = (
    "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09",
)
UNTRUSTED_PAIR = (
    "3fb8742c1df24a38f857b1daa916ad9347aacc1d4da5989b2215be6fd7f7ff26",
    "52548d3b2c4405fda9c8976d196a94343ade91d2f43c5b0288ebd507cbed118a",
)
PHRASE_FORMAT = (
    "I rotate the 42 staging execution generation from origin registry SHA256 %s and resource "
    "manifest SHA256 %s to origin registry SHA256 %s and resource manifest SHA256 %s. "
    "Production remains unchanged."
)
PHRASE = (
    "I rotate the 42 staging execution generation from origin registry SHA256 "
    f"{RETIRE_REGISTRY} and resource manifest SHA256 {RETIRE_MANIFEST} to origin registry "
    f"SHA256 {ACTIVATE_REGISTRY} and resource manifest SHA256 {ACTIVATE_MANIFEST}. "
    "Production remains unchanged."
)
PHRASE_SHA256 = hashlib.sha256(PHRASE.encode("utf-8")).hexdigest()
WORDS = ("rotate-v2-preflight", "rotate-v2-dry-run", "rotate-v2-apply")
OPTIONS = (
    "--retire-origin-registry-sha256",
    "--retire-resource-manifest-sha256",
    "--activate-origin-registry-sha256",
    "--activate-resource-manifest-sha256",
    "--expected-v2-lock-version",
    "--output",
    "--activation-phrase-file",
)
PARAMETERS = (
    "retire_origin_registry_sha256",
    "retire_resource_manifest_sha256",
    "activate_origin_registry_sha256",
    "activate_resource_manifest_sha256",
    "expected_v2_lock_version",
    "activation_phrase",
)
GRAMMAR_ERROR = b'{"error":"execution_approval_manifest_invalid"}\n'
SCRIPT_RELATIVE_PATH = (
    "infra/bigquery_routines/sp_rotate_open_intelligence_execution_generation_v2_script.sql"
)
ACTIVE_TABLE = "open_intelligence_execution_active_generation_v1"
LOCK_TABLE = "open_intelligence_execution_approval_lock_v2"
UPDATE_TABLES = (ACTIVE_TABLE, LOCK_TABLE)
UPDATE_DATA = "bigquery.tables.updateData"
UPDATE_GUARDS = tuple(f"update_data_{table}" for table in UPDATE_TABLES)
RECORD_TABLES = (
    "open_intelligence_execution_approvals_v2",
    "open_intelligence_execution_consumptions_v2",
    "open_intelligence_execution_results_v2",
)
EXPECTED_PRE_DML_REFUSALS = (
    "execution_approval_identity_invalid",
    "execution_approval_generation_invalid",
    "execution_approval_manifest_mismatch",
    "execution_rotation_lock_not_ready",
    "execution_rotation_active_generation_mismatch",
    "execution_rotation_live_records",
    "execution_approval_generation_invalid",
    "execution_origin_registry_digest_mismatch",
    "execution_origin_registry_invalid",
    "execution_approval_generation_invalid",
    "execution_approval_generation_invalid",
    "execution_origin_policy_invalid",
)
EXPECTED_POST_DML_REFUSALS = (
    "execution_approval_concurrent_conflict",
    "execution_approval_concurrent_conflict",
    "execution_approval_lock_invalid",
    "execution_approval_generation_invalid",
)
REDACTED_PARAMETERS = {
    "activate_origin_registry_sha256": ACTIVATE_REGISTRY,
    "activate_resource_manifest_sha256": ACTIVATE_MANIFEST,
    "activation_phrase": {"sha256": PHRASE_SHA256},
    "expected_v2_lock_version": "1",
    "retire_origin_registry_sha256": RETIRE_REGISTRY,
    "retire_resource_manifest_sha256": RETIRE_MANIFEST,
}
ROTATION = {
    "contract_version": "open_intelligence_execution_rotation_receipt_v2",
    "state": "ready",
    "rotated_at": "2026-09-20T10:00:00.000000Z",
    "rotated_by": ACTOR,
    "lock_version": 2,
    "retired_origin_registry_sha256": RETIRE_REGISTRY,
    "retired_resource_manifest_sha256": RETIRE_MANIFEST,
    "origin_registry_sha256": ACTIVATE_REGISTRY,
    "resource_manifest_sha256": ACTIVATE_MANIFEST,
}
SESSION_USER = "op\u00e9rateur@example.test"
JOB_HEX = "5f3c2a9e1b7d4c08a1b2c3d4e5f60718"
JOB_ID = "rotation-" + JOB_HEX
JOB_LOCATION = "EU"
JOB_PROJECT = "rotation-project"
SUBMITTED_MARKER = (
    '{"contract_version":"execution_rotation_submitted_v1",'
    '"job_id":"rotation-5f3c2a9e1b7d4c08a1b2c3d4e5f60718",'
    '"location":"EU","project":"rotation-project","state":"submitted"}'
)
RESULT_UNREAD = (
    '{"error":"execution_rotation_result_unread",'
    '"job_id":"rotation-5f3c2a9e1b7d4c08a1b2c3d4e5f60718",'
    '"location":"EU","project":"rotation-project"}'
)
INTERNAL_REFUSAL = '{"error":"execution_approval_internal_refusal"}'
# The exact receipt lines the fake client yields, pinned as bytes rather than parsed.
# Amendment g moved the activate manifest to ab7802f4, and with it the phrase digest
# (30dfa001 to ec81512b) inside these lines.
# The daily contract revision moved the activate pair to e6b95e35 and b7553041, and the
# phrase digest from ec81512b to c7983a65.
# Tightening the date regex to ASCII digits moved the pair to f23001ed and
# 592ed45f, and the phrase digest from c7983a65 to bda130e6.
DRY_RUN_LINE = (
    '{"contract_version":"execution_rotation_dry_run_v1","expected_v2_lock_version":1,"parame'
    'ters":{"activate_origin_registry_sha256":"f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e9'
    '2b3b53559cfc67b9e2","activate_resource_manifest_sha256":"592ed45ffdc1a0a10371d197bc4cc20'
    '57372b12f845b7ea9c0280bfd68b37efe","activation_phrase":{"sha256":"bda130e61faf597ca36d76'
    '84a80a1216c5f96244f5e0efca4358162ec8f0f0ee"},"expected_v2_lock_version":"1","retire_orig'
    'in_registry_sha256":"38d7db4b78b89e091a1c24fe3817df2e81d0a25fd71945326074adf1501a9f26","'
    'retire_resource_manifest_sha256":"f8ef93da7f4108bac7274d254648106d6c1ce6ce6ff32513d3a576'
    'ee9fe260e2"},"script_path":"infra/bigquery_routines/sp_rotate_open_intelligence_executio'
    'n_generation_v2_script.sql","script_sha256":"edb954d0cad46a1d319f389a4ae225a2baf435beb65'
    'dac9fb4b44da1df7e77b9"}'
)
APPLY_LINE = (
    '{"contract_version":"execution_rotation_apply_v1","expected_v2_lock_version":1,"paramete'
    'rs":{"activate_origin_registry_sha256":"f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b'
    '3b53559cfc67b9e2","activate_resource_manifest_sha256":"592ed45ffdc1a0a10371d197bc4cc2057'
    '372b12f845b7ea9c0280bfd68b37efe","activation_phrase":{"sha256":"bda130e61faf597ca36d7684'
    'a80a1216c5f96244f5e0efca4358162ec8f0f0ee"},"expected_v2_lock_version":"1","retire_origin'
    '_registry_sha256":"38d7db4b78b89e091a1c24fe3817df2e81d0a25fd71945326074adf1501a9f26","re'
    'tire_resource_manifest_sha256":"f8ef93da7f4108bac7274d254648106d6c1ce6ce6ff32513d3a576ee'
    '9fe260e2"},"preflight":{"checks":[{"name":"assert_01_execution_approval_identity_invalid'
    '","parameters":[],"refusal":"execution_approval_identity_invalid","result":"pass","sql_s'
    'ha256":"059d953a40a7268b4724ad1d8d2ff7635a5a5b2a74ff5bde895926152b840eb1"},{"name":"asse'
    'rt_02_execution_approval_generation_invalid","parameters":["retire_origin_registry_sha25'
    '6","retire_resource_manifest_sha256","activate_origin_registry_sha256","activate_resourc'
    'e_manifest_sha256"],"refusal":"execution_approval_generation_invalid","result":"pass","s'
    'ql_sha256":"fa19660e72254f8990d6ea511440133be8270d2c03ecfeef0d96b743be24d745"},{"name":"'
    'assert_03_execution_approval_manifest_mismatch","parameters":["retire_origin_registry_sh'
    'a256","retire_resource_manifest_sha256","activate_origin_registry_sha256","activate_reso'
    'urce_manifest_sha256","activation_phrase"],"refusal":"execution_approval_manifest_mismat'
    'ch","result":"pass","sql_sha256":"a9ca396633c031a8f5a67eb9748107c6a1684bc2f214a89501c6e1'
    '7e5b4094cf"},{"name":"assert_04_execution_rotation_lock_not_ready","parameters":["expect'
    'ed_v2_lock_version"],"refusal":"execution_rotation_lock_not_ready","result":"pass","sql_'
    'sha256":"f9502e4b716a289c5da838f36a326bc8b66c99e85a6f6d4bd3f2a0ecca7352f8"},{"name":"ass'
    'ert_05_execution_rotation_active_generation_mismatch","parameters":["retire_origin_regis'
    'try_sha256","retire_resource_manifest_sha256"],"refusal":"execution_rotation_active_gene'
    'ration_mismatch","result":"pass","sql_sha256":"0ce82461f3d757d9bc752136bae09197001d00d31'
    'eb2997e6d0784671db50549"},{"name":"assert_06_execution_rotation_live_records","parameter'
    's":["retire_origin_registry_sha256","retire_resource_manifest_sha256"],"refusal":"execut'
    'ion_rotation_live_records","result":"pass","sql_sha256":"532a842b86a32692f3f1e6267e53812'
    'cd507f8228a9bd01c108d6862762e698a"},{"name":"assert_07_execution_approval_generation_inv'
    'alid","parameters":["activate_origin_registry_sha256"],"refusal":"execution_approval_gen'
    'eration_invalid","result":"pass","sql_sha256":"617e20da6c078c86102caf2651a39519cab875ffa'
    'f648f0766b7414674682ac5"},{"name":"assert_08_execution_origin_registry_digest_mismatch",'
    '"parameters":["activate_origin_registry_sha256"],"refusal":"execution_origin_registry_di'
    'gest_mismatch","result":"pass","sql_sha256":"8eab902b2e55e07bbdfbd4898c852f1e8efca7d9043'
    'fe1634b1bbafe5a86995c"},{"name":"assert_09_execution_origin_registry_invalid","parameter'
    's":["activate_origin_registry_sha256"],"refusal":"execution_origin_registry_invalid","re'
    'sult":"pass","sql_sha256":"fd69a6d3f33cca235dad62350f6d78852da99b1e7ae4eb47857971d5d7086'
    'd48"},{"name":"assert_10_execution_approval_generation_invalid","parameters":["activate_'
    'origin_registry_sha256","activate_resource_manifest_sha256"],"refusal":"execution_approv'
    'al_generation_invalid","result":"pass","sql_sha256":"7c87cf1a1be9a0d55515557205911d2a16c'
    '96eaff3ed8ec3ce4ab8fb3c15a5a6"},{"name":"assert_11_execution_approval_generation_invalid'
    '","parameters":["activate_origin_registry_sha256","activate_resource_manifest_sha256"],"'
    'refusal":"execution_approval_generation_invalid","result":"pass","sql_sha256":"74a891542'
    '319fe1e2c9ee7efa575a15f7bb592b250d3a321ae4d0d3c0a9df28e"},{"name":"assert_12_execution_o'
    'rigin_policy_invalid","parameters":["activate_origin_registry_sha256"],"refusal":"execut'
    'ion_origin_policy_invalid","result":"pass","sql_sha256":"1181c4f48815f2613c803a66cae91b1'
    'cd28409c43aca69fe4a9529684e128aed"},{"name":"v2_lock_expected","parameters":[],"refusal"'
    ':"execution_rotation_lock_not_ready","result":"pass"},{"name":"active_generation_expecte'
    'd","parameters":[],"refusal":"execution_rotation_active_generation_mismatch","result":"p'
    'ass"},{"name":"live_records_expected","parameters":["retire_origin_registry_sha256","ret'
    'ire_resource_manifest_sha256"],"refusal":"execution_rotation_live_records","result":"pas'
    's"},{"name":"update_data_open_intelligence_execution_active_generation_v1","parameters":'
    '[],"permission":"bigquery.tables.updateData","refusal":"execution_rotation_update_data_m'
    'issing_open_intelligence_execution_active_generation_v1","result":"pass","table":"open_i'
    'ntelligence_execution_active_generation_v1"},{"name":"update_data_open_intelligence_exec'
    'ution_approval_lock_v2","parameters":[],"permission":"bigquery.tables.updateData","refus'
    'al":"execution_rotation_update_data_missing_open_intelligence_execution_approval_lock_v2'
    '","result":"pass","table":"open_intelligence_execution_approval_lock_v2"}],"contract_ver'
    'sion":"execution_rotation_preflight_v1","expected_v2_lock":{"lock_version":1,"state":"re'
    'ady"},"expected_v2_lock_version":1,"failed_checks":[],"observed_active_generation":[{"or'
    'igin_registry_sha256":"38d7db4b78b89e091a1c24fe3817df2e81d0a25fd71945326074adf1501a9f26"'
    ',"resource_manifest_sha256":"f8ef93da7f4108bac7274d254648106d6c1ce6ce6ff32513d3a576ee9fe'
    '260e2"}],"observed_live_records":{"approvals_v2":0,"consumptions_v2":0,"results_v2":0},"'
    'observed_v2_lock":{"lock_version":1,"state":"ready"},"parameters":{"activate_origin_regi'
    'stry_sha256":"f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2","activat'
    'e_resource_manifest_sha256":"592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b'
    '37efe","activation_phrase":{"sha256":"bda130e61faf597ca36d7684a80a1216c5f96244f5e0efca43'
    '58162ec8f0f0ee"},"expected_v2_lock_version":"1","retire_origin_registry_sha256":"38d7db4'
    'b78b89e091a1c24fe3817df2e81d0a25fd71945326074adf1501a9f26","retire_resource_manifest_sha'
    '256":"f8ef93da7f4108bac7274d254648106d6c1ce6ce6ff32513d3a576ee9fe260e2"},"post_mutation_'
    'asserts":[{"evaluated":false,"name":"assert_13_execution_approval_concurrent_conflict","'
    'refusal":"execution_approval_concurrent_conflict"},{"evaluated":false,"name":"assert_14_'
    'execution_approval_concurrent_conflict","refusal":"execution_approval_concurrent_conflic'
    't"},{"evaluated":false,"name":"assert_15_execution_approval_lock_invalid","refusal":"exe'
    'cution_approval_lock_invalid"},{"evaluated":false,"name":"assert_16_execution_approval_g'
    'eneration_invalid","refusal":"execution_approval_generation_invalid"}],"refusal":null,"s'
    'cript_path":"infra/bigquery_routines/sp_rotate_open_intelligence_execution_generation_v2'
    '_script.sql","script_sha256":"edb954d0cad46a1d319f389a4ae225a2baf435beb65dac9fb4b44da1df'
    '7e77b9","session_user":"opérateur@example.test","state":"ready"},"refusal":null,"rotatio'
    'n":{"contract_version":"open_intelligence_execution_rotation_receipt_v2","lock_version":'
    '2,"origin_registry_sha256":"f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67'
    'b9e2","resource_manifest_sha256":"592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280b'
    'fd68b37efe","retired_origin_registry_sha256":"38d7db4b78b89e091a1c24fe3817df2e81d0a25fd7'
    '1945326074adf1501a9f26","retired_resource_manifest_sha256":"f8ef93da7f4108bac7274d254648'
    '106d6c1ce6ce6ff32513d3a576ee9fe260e2","rotated_at":"2026-09-20T10:00:00.000000Z","rotate'
    'd_by":"usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab","state":"re'
    'ady"},"script_sha256":"edb954d0cad46a1d319f389a4ae225a2baf435beb65dac9fb4b44da1df7e77b9"'
    ',"state":"rotated"}'
)


def _write_inputs(tmp_path: Path, *, phrase: str = PHRASE) -> dict[str, Path]:
    paths = {"phrase": tmp_path / "rotation-phrase.txt", "output": tmp_path / "receipt.json"}
    paths["phrase"].write_text(phrase + "\n", encoding="utf-8")
    return paths


def _arguments(
    word: str,
    paths: dict[str, Path],
    *,
    lock_version: str = "1",
    retire=(RETIRE_REGISTRY, RETIRE_MANIFEST),
    activate=(ACTIVATE_REGISTRY, ACTIVATE_MANIFEST),
) -> list[str]:
    return [
        word,
        "--retire-origin-registry-sha256",
        retire[0],
        "--retire-resource-manifest-sha256",
        retire[1],
        "--activate-origin-registry-sha256",
        activate[0],
        "--activate-resource-manifest-sha256",
        activate[1],
        "--expected-v2-lock-version",
        lock_version,
        "--output",
        str(paths["output"]),
        "--activation-phrase-file",
        str(paths["phrase"]),
    ]


class _Job:
    def __init__(self, rows, job_id=None, result_error=None):
        self._rows = rows
        self._result_error = result_error
        self.job_id = job_id
        self.location = JOB_LOCATION
        self.project = JOB_PROJECT

    def result(self):
        if self._result_error is not None:
            raise self._result_error
        return self._rows


class _FakeClient:
    """Answers the session probe, the derived checks, the three observations and the script."""

    location = JOB_LOCATION
    project = JOB_PROJECT

    def __init__(
        self,
        *,
        failures=(),
        errors=(),
        ok_values=None,
        session_user="operator@example.test",
        lock_version=1,
        lock_state="ready",
        lock_rows=1,
        active_rows=((RETIRE_REGISTRY, RETIRE_MANIFEST),),
        live_records=(0, 0, 0),
        script_rows=(ROTATION,),
        script_error=None,
        script_result_error=None,
        error_exception=None,
        guard_errors=(),
        iam_responses=None,
        iam_errors=(),
    ):
        self.iam_responses = dict(iam_responses or {})
        self.iam_errors = set(iam_errors)
        self.iam_calls: list[tuple[str, list[str]]] = []
        self.failures = set(failures)
        self.error_exception = error_exception
        self.guard_errors = set(guard_errors)
        self.errors = set(errors)
        self.ok_values = dict(ok_values or {})
        self.session_user = session_user
        self.lock_version = lock_version
        self.lock_state = lock_state
        self.lock_rows = lock_rows
        self.active_rows = active_rows
        self.live_records = live_records
        self.script_rows = script_rows
        self.script_error = script_error
        self.script_result_error = script_result_error
        self.by_sql = {check.sql: check.name for check in migration.rotation_preflight_checks()}
        self.script_sql = migration._render_sql(migration.V2_ROTATION_SCRIPT)
        self.queries: list[tuple[str, object]] = []
        self.script_job_ids: list[object] = []

    def query(self, sql, job_config=None, job_id=None):
        self.queries.append((sql, job_config))
        if sql == "SELECT SESSION_USER() AS session_user":
            return _Job(({"session_user": self.session_user},))
        if sql == self.script_sql:
            self.script_job_ids.append(job_id)
            if self.script_error is not None:
                raise RuntimeError(self.script_error)
            return _Job(
                tuple(dict(row) for row in self.script_rows),
                job_id=job_id,
                result_error=self.script_result_error,
            )
        if sql == migration._ROTATION_LOCK_SQL:
            if "lock" in self.guard_errors:
                raise self.error_exception
            row = {
                "lock_name": "open_intelligence_execution_approval_v2",
                "approval_contract_version": "open_intelligence_execution_approval_v2",
                "lock_version": self.lock_version,
                "state": self.lock_state,
                "last_approval_id": None,
                "created_at": None,
                "updated_at": None,
            }
            return _Job(tuple(dict(row) for _ in range(self.lock_rows)))
        if sql == migration._ROTATION_ACTIVE_GENERATION_SQL:
            if "active" in self.guard_errors:
                raise self.error_exception
            return _Job(
                tuple(
                    {"origin_registry_sha256": origin, "resource_manifest_sha256": manifest}
                    for origin, manifest in self.active_rows
                )
            )
        if sql == migration._ROTATION_LIVE_RECORDS_SQL:
            if "records" in self.guard_errors:
                raise self.error_exception
            approvals, consumptions, results = self.live_records
            return _Job(
                (
                    {
                        "approvals_v2": approvals,
                        "consumptions_v2": consumptions,
                        "results_v2": results,
                    },
                )
            )
        name = self.by_sql[sql]
        if name in self.errors:
            raise self.error_exception or RuntimeError("query failed")
        if name in self.ok_values:
            return _Job(({"ok": self.ok_values[name]},))
        return _Job(({"ok": name not in self.failures},))

    def test_iam_permissions(self, table, permissions):
        self.iam_calls.append((table, list(permissions)))
        short = table.rsplit(".", 1)[-1]
        if short in self.iam_errors:
            raise self.error_exception or RuntimeError("permission probe failed")
        if short in self.iam_responses:
            return self.iam_responses[short]
        return {"permissions": list(permissions)}

    def script_calls(self) -> list[object]:
        return [job_config for sql, job_config in self.queries if sql == self.script_sql]


def _install_fake(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(migration, "_load_credentials", lambda: object())
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)


def _pin_job_id(monkeypatch) -> None:
    """Make the tool generate JOB_ID for the script job, so the marker is a literal."""

    monkeypatch.setattr(migration.uuid, "uuid4", lambda: uuid.UUID(hex=JOB_HEX))


def _forbid_native(monkeypatch) -> None:
    def _refuse(*_args, **_kwargs):
        raise AssertionError("native client constructed")

    monkeypatch.setattr(migration, "_load_credentials", _refuse)
    monkeypatch.setattr(migration, "_bigquery_client", _refuse)


def _check_names() -> tuple[str, ...]:
    return tuple(check.name for check in migration.rotation_preflight_checks())


def _statements() -> tuple[str, ...]:
    return migration._cutover_script_statements(migration.V2_ROTATION_SCRIPT)


# Grammar


def _malformed_cases(paths: dict[str, Path]) -> dict[str, list[str]]:
    base = _arguments("rotate-v2-dry-run", paths)
    return {
        "word_alone": ["rotate-v2-dry-run"],
        "missing_option": base[:-2],
        "extra_positional": [*base, "extra"],
        "repeated_option": [*base[:-2], "--retire-origin-registry-sha256", RETIRE_REGISTRY],
        "unknown_option": [*base[:-2], "--preflight-receipt", str(paths["output"])],
        "cutover_option": [*base[:-2], "--expected-v1-lock-version", "470"],
        "odd_count": [*base, "--output"],
        "unknown_word": ["rotate-v3-apply", *base[1:]],
        "cutover_word_with_rotation_options": ["cutover-v2-dry-run", *base[1:]],
        "rotation_word_with_phase": [*base, "--phase", "post-disable"],
        "negative_lock_version": _arguments("rotate-v2-apply", paths, lock_version="-1"),
        "decimal_lock_version": _arguments("rotate-v2-apply", paths, lock_version="1.0"),
        "signed_lock_version": _arguments("rotate-v2-apply", paths, lock_version="+1"),
        "padded_lock_version": _arguments("rotate-v2-apply", paths, lock_version="01"),
        "blank_lock_version": _arguments("rotate-v2-preflight", paths, lock_version=""),
        "empty_path_value": [*base[:-1], ""],
        "empty_digest_value": [*base[:2], "", *base[3:]],
        "existing_word_with_options": ["apply-v2", *base[1:]],
    }


def test_parser_refuses_each_malformed_grammar_with_exit_2(tmp_path, monkeypatch, capfd):
    _forbid_native(monkeypatch)
    paths = _write_inputs(tmp_path)
    for label, arguments in _malformed_cases(paths).items():
        assert migration.main(arguments) == 2, label
        captured = capfd.readouterr()
        assert captured.out == "", label
        assert captured.err.encode("utf-8") == GRAMMAR_ERROR, label
    assert not paths["output"].exists()


def test_parser_accepts_each_option_order_once(tmp_path):
    paths = _write_inputs(tmp_path)
    forward = _arguments("rotate-v2-preflight", paths)
    pairs = list(zip(forward[1::2], forward[2::2], strict=True))
    reordered = [forward[0], *(item for pair in reversed(pairs) for item in pair)]
    expected = migration.RotationOptions(
        word="rotate-v2-preflight",
        retire_origin_registry_sha256=RETIRE_REGISTRY,
        retire_resource_manifest_sha256=RETIRE_MANIFEST,
        activate_origin_registry_sha256=ACTIVATE_REGISTRY,
        activate_resource_manifest_sha256=ACTIVATE_MANIFEST,
        expected_v2_lock_version=1,
        output=paths["output"],
        activation_phrase_file=paths["phrase"],
    )
    assert migration._parse_cutover_arguments(forward) == expected
    assert migration._parse_cutover_arguments(reordered) == expected
    assert (
        migration._parse_cutover_arguments(
            _arguments("rotate-v2-apply", paths, lock_version="0")
        ).expected_v2_lock_version
        == 0
    )


def test_words_and_options_are_exact():
    assert migration.ROTATION_WORDS == WORDS
    assert migration.ROTATION_OPTIONS == OPTIONS
    assert migration.CUTOVER_WORDS == (
        "cutover-v2-preflight",
        "cutover-v2-dry-run",
        "cutover-v2-apply",
    )
    assert set(migration.ROTATION_WORDS).isdisjoint(migration.CUTOVER_WORDS)
    assert tuple(migration.V2_ROTATION_PARAMETERS) == PARAMETERS
    assert migration.V2_ROTATION_SCRIPT_PATH == SCRIPT_PATH
    assert SCRIPT_PATH.read_text(encoding="utf-8") == migration.V2_ROTATION_SCRIPT
    assert migration.ROTATION_PHRASE_FORMAT == PHRASE_FORMAT


# The script


def test_script_is_a_multi_statement_transaction_with_exactly_two_updates():
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert text.startswith("BEGIN\n")
    assert text.rstrip().endswith("END;")
    assert "CREATE" not in text
    assert "{project}.{dataset}" in text
    statements = _statements()
    keywords = [statement.split(None, 1)[0] for statement in statements if statement]
    assert keywords.count("UPDATE") == 2
    assert not {"INSERT", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER"} & set(keywords)
    updates = [statement for statement in statements if statement.startswith("UPDATE ")]
    assert updates[0].startswith(f"UPDATE `{{project}}.{{dataset}}.{ACTIVE_TABLE}`")
    assert updates[1].startswith(f"UPDATE `{{project}}.{{dataset}}.{LOCK_TABLE}`")
    assert "SET origin_registry_sha256 = v_activate_origin_registry_sha256" in updates[0]
    assert "resource_manifest_sha256 = v_activate_resource_manifest_sha256" in updates[0]
    assert "WHERE origin_registry_sha256 = v_retire_origin_registry_sha256" in updates[0]
    assert "AND resource_manifest_sha256 = v_retire_resource_manifest_sha256" in updates[0]
    assert (
        "SET lock_version = lock_version + 1, state = 'ready', updated_at = v_rotated_at"
        in (updates[1])
    )
    assert "state = 'ready' AND lock_version = v_lock_version" in updates[1]
    assert keywords.count("BEGIN") == 1
    assert statements[keywords.index("BEGIN")] == "BEGIN TRANSACTION"
    assert keywords.count("COMMIT") == 1
    assert "ROLLBACK" not in text
    assert keywords.index("COMMIT") > keywords.index("UPDATE")
    assert keywords[-2:] == ["SELECT", "END"]
    for parameter in PARAMETERS:
        assert f" DEFAULT @{parameter};" in text, parameter
    assert text.count("DECLARE ") == 6 + 5
    assert "open_intelligence_execution_approval_lock_v1" not in text
    for table in (
        "open_intelligence_execution_approvals_v1",
        "open_intelligence_execution_consumptions_v1",
        "open_intelligence_execution_results_v1",
        "residual",
    ):
        assert table not in text, table


def test_script_bindings_cover_every_variable_from_the_six_parameters():
    bindings = migration._cutover_bindings(_statements())
    assert bindings == {
        "v_retire_origin_registry_sha256": "@retire_origin_registry_sha256",
        "v_retire_resource_manifest_sha256": "@retire_resource_manifest_sha256",
        "v_activate_origin_registry_sha256": "@activate_origin_registry_sha256",
        "v_activate_resource_manifest_sha256": "@activate_resource_manifest_sha256",
        "v_expected_lock_version_text": "@expected_v2_lock_version",
        "v_activation_phrase": "@activation_phrase",
        "v_actor": (
            "(CONCAT('usr_', LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:', "
            "SESSION_USER()))))))"
        ),
        "v_rotated_at": "(CURRENT_TIMESTAMP())",
        "v_lock_version": "(SAFE_CAST(v_expected_lock_version_text AS INT64))",
        "v_registry_json": (
            "((SELECT canonical_registry_json FROM `{project}.{dataset}."
            "open_intelligence_execution_origin_registries_v1` "
            "WHERE origin_registry_sha256 = v_activate_origin_registry_sha256))"
        ),
        "v_resource_json": (
            "((SELECT canonical_resource_manifest_json FROM `{project}.{dataset}."
            "open_intelligence_execution_resource_manifests_v1` "
            "WHERE resource_manifest_sha256 = v_activate_resource_manifest_sha256 "
            "AND origin_registry_sha256 = v_activate_origin_registry_sha256))"
        ),
    }


def test_script_copies_the_actor_derivation_and_the_registry_checks_from_the_first_cutover():
    first = FIRST_CUTOVER_PATH.read_text(encoding="utf-8").splitlines()
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert first[13] in lines
    assert first[14] in lines
    assert f"  ASSERT v_actor = '{ACTOR}' AS 'execution_approval_identity_invalid';" in lines
    renamed = (
        "\n".join(first[27:51])
        .replace("v_origin_registry_sha256", "v_activate_origin_registry_sha256")
        .replace("v_resource_manifest_sha256", "v_activate_resource_manifest_sha256")
    )
    assert renamed in text
    assert "ASSERT v_activation_phrase = FORMAT('" + PHRASE_FORMAT + "', " in text
    assert (
        "v_retire_origin_registry_sha256, v_retire_resource_manifest_sha256, "
        "v_activate_origin_registry_sha256, v_activate_resource_manifest_sha256) "
        "AS 'execution_approval_manifest_mismatch';"
    ) in text
    for table in RECORD_TABLES:
        assert (
            f"(SELECT COUNT(*) FROM `{{project}}.{{dataset}}.{table}` "
            "WHERE origin_registry_sha256 = v_retire_origin_registry_sha256 "
            "OR resource_manifest_sha256 = v_retire_resource_manifest_sha256) = 0"
        ) in text
    assert "AS 'execution_rotation_live_records';" in text
    assert "REGEXP_CONTAINS(v_expected_lock_version_text, r'^(0|[1-9][0-9]*)$')" in text
    assert "'open_intelligence_execution_rotation_receipt_v2' AS contract_version" in text
    for column in (
        "v_rotated_at AS rotated_at",
        "v_actor AS rotated_by",
        "v_lock_version + 1 AS lock_version",
        "v_retire_origin_registry_sha256 AS retired_origin_registry_sha256",
        "v_retire_resource_manifest_sha256 AS retired_resource_manifest_sha256",
        "v_activate_origin_registry_sha256 AS origin_registry_sha256",
        "v_activate_resource_manifest_sha256 AS resource_manifest_sha256",
    ):
        assert column in text, column


def test_preflight_checks_cover_every_assert_before_the_first_dml_as_read_only_selects():
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    before, separator, after = text.partition("\n  UPDATE ")
    assert separator
    checks = migration.rotation_preflight_checks()
    post = migration.rotation_post_mutation_asserts()
    assert len(checks) == before.count("ASSERT ") == 12
    assert len(post) == after.count("ASSERT ") == 4
    assert tuple(check.refusal for check in checks) == EXPECTED_PRE_DML_REFUSALS
    assert tuple(item.refusal for item in post) == EXPECTED_POST_DML_REFUSALS
    assert [check.name for check in checks] == [
        f"assert_{index:02d}_{code}"
        for index, code in enumerate(EXPECTED_PRE_DML_REFUSALS, start=1)
    ]
    assert [item.name for item in post] == [
        f"assert_{index:02d}_{code}"
        for index, code in enumerate(EXPECTED_POST_DML_REFUSALS, start=13)
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
        referenced = {name for name in PARAMETERS if re.search(rf"(?<!@)@{name}\b", check.sql)}
        assert set(check.parameters) == referenced, check.name
    assert "SESSION_USER()" in checks[0].sql
    assert f"'{ACTOR}'" in checks[0].sql
    assert all(f"@{name}" in checks[1].sql for name in PARAMETERS[:4])
    assert "@activation_phrase = FORMAT(" in checks[2].sql
    assert "@expected_v2_lock_version" in checks[3].sql
    assert f"`{migration.PROJECT}.{migration.DATASET}.{LOCK_TABLE}`" in checks[3].sql
    assert f"`{migration.PROJECT}.{migration.DATASET}.{ACTIVE_TABLE}`" in checks[4].sql
    assert all(table in checks[5].sql for table in RECORD_TABLES)
    assert set(checks[5].parameters) == set(PARAMETERS[:2])
    assert set(checks[7].parameters) == {"activate_origin_registry_sha256"}
    assert "@retire_" not in checks[11].sql
    assert set(migration.cutover_preflight_checks()) == set(
        migration.cutover_preflight_checks(
            migration.V2_CUTOVER_SCRIPT, migration.V2_CUTOVER_PARAMETERS
        )
    )
    shared = set(migration.cutover_preflight_checks()) & set(checks)
    assert shared == {checks[0]}
    assert checks[0].name == "assert_01_execution_approval_identity_invalid"


ACTIVE_COUNT = (
    "(SELECT COUNT(*) FROM `{project}.{dataset}."
    "open_intelligence_execution_active_generation_v1`) = 1"
)
ACTIVE_PAIR_COUNT = (
    "(SELECT COUNT(*) FROM `{project}.{dataset}."
    "open_intelligence_execution_active_generation_v1` "
    "WHERE origin_registry_sha256 = v_%s_origin_registry_sha256 "
    "AND resource_manifest_sha256 = v_%s_resource_manifest_sha256) = 1"
)
PAIR_PREDICATES = {
    2: (
        "REGEXP_CONTAINS(v_retire_origin_registry_sha256, r'^[0-9a-f]{64}$')\n"
        "    AND REGEXP_CONTAINS(v_retire_resource_manifest_sha256, r'^[0-9a-f]{64}$')\n"
        "    AND REGEXP_CONTAINS(v_activate_origin_registry_sha256, r'^[0-9a-f]{64}$')\n"
        "    AND REGEXP_CONTAINS(v_activate_resource_manifest_sha256, r'^[0-9a-f]{64}$')\n"
        "    AND (v_retire_origin_registry_sha256 != v_activate_origin_registry_sha256\n"
        "      OR v_retire_resource_manifest_sha256 != v_activate_resource_manifest_sha256)",
        "execution_approval_generation_invalid",
        False,
    ),
    5: (
        ACTIVE_COUNT + "\n    AND " + ACTIVE_PAIR_COUNT % ("retire", "retire"),
        "execution_rotation_active_generation_mismatch",
        False,
    ),
    16: (
        ACTIVE_COUNT + "\n    AND " + ACTIVE_PAIR_COUNT % ("activate", "activate"),
        "execution_approval_generation_invalid",
        True,
    ),
}


def test_asserts_2_5_and_16_are_pinned_by_text():
    asserts = {
        ordinal: (predicate, code, after_dml)
        for ordinal, predicate, code, after_dml in migration._cutover_asserts(
            migration.V2_ROTATION_SCRIPT
        )
    }
    assert len(asserts) == 16
    for ordinal, expected in PAIR_PREDICATES.items():
        assert asserts[ordinal] == expected, ordinal
    checks = migration.rotation_preflight_checks()
    assert "= 1\n" in checks[4].sql
    assert ">= 1" not in checks[4].sql
    assert "@retire_origin_registry_sha256 != @activate_origin_registry_sha256" in checks[1].sql
    assert "@retire_resource_manifest_sha256 != @activate_resource_manifest_sha256" in checks[1].sql


# Parameters and dry run


def test_rotation_parameters_render_six_strings_and_refuse_a_malformed_digest():
    rendered = migration.v2_rotation_parameters(
        retire_pair=(RETIRE_REGISTRY, RETIRE_MANIFEST),
        activate_pair=(ACTIVATE_REGISTRY, ACTIVATE_MANIFEST),
        expected_v2_lock_version=1,
        activation_phrase=PHRASE,
    )
    assert rendered == (
        ("retire_origin_registry_sha256", "STRING", RETIRE_REGISTRY),
        ("retire_resource_manifest_sha256", "STRING", RETIRE_MANIFEST),
        ("activate_origin_registry_sha256", "STRING", ACTIVATE_REGISTRY),
        ("activate_resource_manifest_sha256", "STRING", ACTIVATE_MANIFEST),
        ("expected_v2_lock_version", "STRING", "1"),
        ("activation_phrase", "STRING", PHRASE),
    )
    assert migration._redacted_rotation_parameters(rendered) == REDACTED_PARAMETERS
    for retire, activate in (
        ((RETIRE_REGISTRY.upper(), RETIRE_MANIFEST), (ACTIVATE_REGISTRY, ACTIVATE_MANIFEST)),
        ((RETIRE_REGISTRY, RETIRE_MANIFEST[:-1]), (ACTIVATE_REGISTRY, ACTIVATE_MANIFEST)),
        ((RETIRE_REGISTRY, RETIRE_MANIFEST), ("g" * 64, ACTIVATE_MANIFEST)),
        ((RETIRE_REGISTRY, RETIRE_MANIFEST), (ACTIVATE_REGISTRY, ACTIVATE_MANIFEST + "0")),
    ):
        with pytest.raises(
            migration.MigrationRefusal, match=r"^execution_approval_generation_invalid$"
        ):
            migration.v2_rotation_parameters(
                retire_pair=retire,
                activate_pair=activate,
                expected_v2_lock_version=1,
                activation_phrase=PHRASE,
            )


def test_rotation_parameters_refuse_a_retire_pair_equal_to_the_activate_pair():
    same = (RETIRE_REGISTRY, RETIRE_MANIFEST)
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_approval_generation_invalid$"
    ):
        migration.v2_rotation_parameters(
            retire_pair=same,
            activate_pair=same,
            expected_v2_lock_version=1,
            activation_phrase=PHRASE_FORMAT % (*same, *same),
        )
    # Only the exact pair is refused: a retire pair sharing one digest with the activate
    # pair (the code's active pair, the only one a rotation may activate) still renders.
    activate = (ACTIVATE_REGISTRY, ACTIVATE_MANIFEST)
    for retire in ((RETIRE_REGISTRY, ACTIVATE_MANIFEST), (ACTIVATE_REGISTRY, RETIRE_MANIFEST)):
        rendered = migration.v2_rotation_parameters(
            retire_pair=retire,
            activate_pair=activate,
            expected_v2_lock_version=1,
            activation_phrase=PHRASE_FORMAT % (*retire, *activate),
        )
        assert rendered[0][2] == retire[0]
        assert rendered[1][2] == retire[1]


@pytest.mark.parametrize("word", WORDS)
def test_same_pair_rotation_refuses_before_any_client(tmp_path, monkeypatch, capfd, word):
    _forbid_native(monkeypatch)
    same = (RETIRE_REGISTRY, RETIRE_MANIFEST)
    paths = _write_inputs(tmp_path, phrase=PHRASE_FORMAT % (*same, *same))

    assert migration.main(_arguments(word, paths, activate=same)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"execution_approval_generation_invalid"}\n'
    assert not paths["output"].exists()


def test_the_activate_pair_the_tests_rotate_to_is_the_code_active_pair():
    assert (ACTIVATE_REGISTRY, ACTIVATE_MANIFEST) == execution_generations.ACTIVE_GENERATION_PAIR


@pytest.mark.parametrize(
    "activate",
    [
        AMENDMENT_E_PAIR,
        UNTRUSTED_PAIR,
        (execution_generations.ACTIVE_GENERATION_PAIR[0], AMENDMENT_E_PAIR[1]),
        (AMENDMENT_E_PAIR[0], execution_generations.ACTIVE_GENERATION_PAIR[1]),
    ],
)
def test_rotation_parameters_refuse_an_activate_pair_other_than_the_code_active_pair(activate):
    """A rotation happens once, so it may only activate the pair the code will admit."""
    retire = (RETIRE_REGISTRY, RETIRE_MANIFEST)
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_rotation_activate_pair_inactive$"
    ):
        migration.v2_rotation_parameters(
            retire_pair=retire,
            activate_pair=activate,
            expected_v2_lock_version=1,
            activation_phrase=PHRASE_FORMAT % (*retire, *activate),
        )


def test_rotation_parameters_refuse_an_active_pair_the_catalogue_does_not_trust(monkeypatch):
    monkeypatch.setattr(execution_generations, "ACTIVE_GENERATION_PAIR", UNTRUSTED_PAIR)
    retire = (RETIRE_REGISTRY, RETIRE_MANIFEST)
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_rotation_activate_pair_inactive$"
    ):
        migration.v2_rotation_parameters(
            retire_pair=retire,
            activate_pair=UNTRUSTED_PAIR,
            expected_v2_lock_version=1,
            activation_phrase=PHRASE_FORMAT % (*retire, *UNTRUSTED_PAIR),
        )


@pytest.mark.parametrize("word", WORDS)
def test_rotation_to_an_inactive_pair_refuses_before_any_client(tmp_path, monkeypatch, capfd, word):
    _forbid_native(monkeypatch)
    retire = (RETIRE_REGISTRY, RETIRE_MANIFEST)
    paths = _write_inputs(tmp_path, phrase=PHRASE_FORMAT % (*retire, *AMENDMENT_E_PAIR))

    assert migration.main(_arguments(word, paths, activate=AMENDMENT_E_PAIR)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"execution_rotation_activate_pair_inactive"}\n'
    assert not paths["output"].exists()


@pytest.mark.parametrize("lock_version", [-1, -2, True, False, "1", 1.0, None])
def test_rotation_parameters_refuse_a_lock_version_that_is_not_a_non_negative_int(lock_version):
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_approval_lock_invalid$"):
        migration.v2_rotation_parameters(
            retire_pair=(RETIRE_REGISTRY, RETIRE_MANIFEST),
            activate_pair=(ACTIVATE_REGISTRY, ACTIVATE_MANIFEST),
            expected_v2_lock_version=lock_version,
            activation_phrase=PHRASE,
        )
    for accepted in (0, 1, 470):
        rendered = migration.v2_rotation_parameters(
            retire_pair=(RETIRE_REGISTRY, RETIRE_MANIFEST),
            activate_pair=(ACTIVATE_REGISTRY, ACTIVATE_MANIFEST),
            expected_v2_lock_version=accepted,
            activation_phrase=PHRASE,
        )
        assert rendered[4] == ("expected_v2_lock_version", "STRING", str(accepted))


def test_dry_run_prints_the_script_sha256_and_never_opens_a_client(tmp_path, monkeypatch, capfd):
    _forbid_native(monkeypatch)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("rotate-v2-dry-run", paths)) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "contract_version": "execution_rotation_dry_run_v1",
        "expected_v2_lock_version": 1,
        "parameters": REDACTED_PARAMETERS,
        "script_path": SCRIPT_RELATIVE_PATH,
        "script_sha256": SCRIPT_SHA256,
    }
    assert "I rotate" not in captured.out
    assert paths["output"].read_bytes() == (lines[0] + "\n").encode("utf-8")


def test_dry_run_refuses_a_malformed_digest_or_phrase_file_before_any_client(
    tmp_path, monkeypatch, capfd
):
    _forbid_native(monkeypatch)
    paths = _write_inputs(tmp_path)
    arguments = _arguments("rotate-v2-dry-run", paths, activate=("A" * 64, ACTIVATE_MANIFEST))
    assert migration.main(arguments) == 1
    assert capfd.readouterr().err == '{"error":"execution_approval_generation_invalid"}\n'
    paths["phrase"].write_text("", encoding="utf-8")
    assert migration.main(_arguments("rotate-v2-dry-run", paths)) == 1
    assert capfd.readouterr().err == '{"error":"execution_approval_manifest_mismatch"}\n'
    paths["phrase"].write_text(PHRASE + "\nsecond line\n", encoding="utf-8")
    assert migration.main(_arguments("rotate-v2-dry-run", paths)) == 1
    assert capfd.readouterr().err == '{"error":"execution_approval_manifest_mismatch"}\n'
    paths["phrase"].unlink()
    assert migration.main(_arguments("rotate-v2-dry-run", paths)) == 1
    assert capfd.readouterr().err == '{"error":"execution_approval_manifest_mismatch"}\n'
    assert not paths["output"].exists()


# Preflight execution


def _preflight(client, *, expected=1):
    return migration._run_rotation_preflight(
        object(),
        retire_pair=(RETIRE_REGISTRY, RETIRE_MANIFEST),
        activate_pair=(ACTIVATE_REGISTRY, ACTIVATE_MANIFEST),
        expected_v2_lock_version=expected,
        activation_phrase=PHRASE,
    )


GUARDS = (
    "v2_lock_expected",
    "active_generation_expected",
    "live_records_expected",
    *UPDATE_GUARDS,
)


def test_preflight_ready_receipt_reads_the_lock_the_active_row_and_the_counts(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    assert receipt["contract_version"] == "execution_rotation_preflight_v1"
    assert receipt["state"] == "ready"
    assert receipt["refusal"] is None
    assert receipt["failed_checks"] == []
    assert receipt["expected_v2_lock"] == {"lock_version": 1, "state": "ready"}
    assert receipt["expected_v2_lock_version"] == 1
    assert receipt["observed_v2_lock"] == {"lock_version": 1, "state": "ready"}
    assert receipt["observed_active_generation"] == [
        {"origin_registry_sha256": RETIRE_REGISTRY, "resource_manifest_sha256": RETIRE_MANIFEST}
    ]
    assert receipt["observed_live_records"] == {
        "approvals_v2": 0,
        "consumptions_v2": 0,
        "results_v2": 0,
    }
    assert receipt["parameters"] == REDACTED_PARAMETERS
    assert receipt["script_path"] == SCRIPT_RELATIVE_PATH
    assert receipt["script_sha256"] == SCRIPT_SHA256
    assert receipt["session_user"] == "operator@example.test"
    assert [item["name"] for item in receipt["checks"]] == [*_check_names(), *GUARDS]
    assert all(item["result"] == "pass" for item in receipt["checks"])
    by_name = {item["name"]: item for item in receipt["checks"]}
    assert by_name["v2_lock_expected"]["refusal"] == "execution_rotation_lock_not_ready"
    assert by_name["active_generation_expected"]["refusal"] == (
        "execution_rotation_active_generation_mismatch"
    )
    assert by_name["live_records_expected"]["refusal"] == "execution_rotation_live_records"
    assert [item["name"] for item in receipt["post_mutation_asserts"]] == [
        item.name for item in migration.rotation_post_mutation_asserts()
    ]
    assert all(item["evaluated"] is False for item in receipt["post_mutation_asserts"])
    serialised = json.dumps(receipt)
    assert "I rotate" not in serialised
    assert client.queries[0][0] == "SELECT SESSION_USER() AS session_user"
    assert len(client.queries) == 1 + len(_check_names()) + 3
    assert client.script_calls() == []
    for sql, job_config in client.queries:
        assert PHRASE not in sql
        if job_config is not None:
            for parameter in job_config.query_parameters:
                assert parameter.type_ == "STRING"
                assert parameter.name in PARAMETERS
    live = [
        job_config
        for sql, job_config in client.queries
        if sql == migration._ROTATION_LIVE_RECORDS_SQL
    ]
    assert len(live) == 1
    assert {(item.name, item.value) for item in live[0].query_parameters} == {
        ("retire_origin_registry_sha256", RETIRE_REGISTRY),
        ("retire_resource_manifest_sha256", RETIRE_MANIFEST),
    }


def test_preflight_probes_update_data_on_both_rotation_tables_as_the_session_user(monkeypatch):
    """The two UPDATEs run with the caller's own rights, which no SELECT can prove. The
    preflight asks BigQuery, read only, whether the caller holds updateData on each table."""
    client = _FakeClient()
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    assert receipt["state"] == "ready"
    assert client.iam_calls == [
        (f"ogilvy-trends-v2.trends_v2_staging_approvals.{table}", [UPDATE_DATA])
        for table in UPDATE_TABLES
    ]
    by_name = {item["name"]: item for item in receipt["checks"]}
    for table, name in zip(UPDATE_TABLES, UPDATE_GUARDS, strict=True):
        assert by_name[name] == {
            "name": name,
            "parameters": [],
            "permission": UPDATE_DATA,
            "refusal": f"execution_rotation_update_data_missing_{table}",
            "result": "pass",
            "table": table,
        }
    assert client.script_calls() == []


@pytest.mark.parametrize("table", UPDATE_TABLES)
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"permissions": []},
        {"permissions": ["bigquery.tables.getData"]},
        {"permissions": "bigquery.tables.updateData"},
        None,
    ],
)
def test_preflight_refuses_naming_the_table_the_caller_cannot_update(monkeypatch, table, response):
    client = _FakeClient(iam_responses={table: response})
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    assert receipt["state"] == "refused"
    assert receipt["failed_checks"] == [f"update_data_{table}"]
    assert receipt["refusal"] == f"execution_rotation_update_data_missing_{table}"
    assert client.script_calls() == []


def test_preflight_fails_closed_when_the_permission_probe_errors(monkeypatch):
    client = _FakeClient(
        iam_errors=(LOCK_TABLE,), error_exception=RuntimeError("Access Denied: " + "a" * 64)
    )
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    assert receipt["state"] == "refused"
    assert receipt["failed_checks"] == [f"update_data_{LOCK_TABLE}"]
    assert receipt["refusal"] == f"execution_rotation_update_data_missing_{LOCK_TABLE}"
    failed = {item["name"]: item for item in receipt["checks"]}[f"update_data_{LOCK_TABLE}"]
    assert failed["error"] == "query_failed"
    assert failed["error_reason"] == "RuntimeError"
    assert failed["error_message"] == "Access Denied: <sha256>"


def test_apply_never_calls_the_script_without_update_data(tmp_path, monkeypatch, capfd):
    client = _FakeClient(iam_responses={ACTIVE_TABLE: {"permissions": []}})
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    receipt = json.loads(capfd.readouterr().out)
    assert receipt["state"] == "refused"
    assert receipt["refusal"] == f"execution_rotation_update_data_missing_{ACTIVE_TABLE}"
    assert client.script_calls() == []


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


@pytest.mark.parametrize(
    "client_kwargs, failed, refusal",
    [
        ({"lock_version": 2}, ["v2_lock_expected"], "execution_rotation_lock_not_ready"),
        ({"lock_state": "prepared"}, ["v2_lock_expected"], "execution_rotation_lock_not_ready"),
        ({"lock_state": "disabled"}, ["v2_lock_expected"], "execution_rotation_lock_not_ready"),
        ({"lock_rows": 0}, ["v2_lock_expected"], "execution_rotation_lock_not_ready"),
        ({"lock_rows": 2}, ["v2_lock_expected"], "execution_rotation_lock_not_ready"),
        (
            {"active_rows": ()},
            ["active_generation_expected"],
            "execution_rotation_active_generation_mismatch",
        ),
        (
            {"active_rows": ((ACTIVATE_REGISTRY, ACTIVATE_MANIFEST),)},
            ["active_generation_expected"],
            "execution_rotation_active_generation_mismatch",
        ),
        (
            {"active_rows": ((RETIRE_REGISTRY, ACTIVATE_MANIFEST),)},
            ["active_generation_expected"],
            "execution_rotation_active_generation_mismatch",
        ),
        (
            {
                "active_rows": (
                    (RETIRE_REGISTRY, RETIRE_MANIFEST),
                    (ACTIVATE_REGISTRY, ACTIVATE_MANIFEST),
                )
            },
            ["active_generation_expected"],
            "execution_rotation_active_generation_mismatch",
        ),
        ({"live_records": (1, 0, 0)}, ["live_records_expected"], "execution_rotation_live_records"),
        ({"live_records": (0, 3, 0)}, ["live_records_expected"], "execution_rotation_live_records"),
        ({"live_records": (0, 0, 1)}, ["live_records_expected"], "execution_rotation_live_records"),
    ],
)
def test_preflight_refuses_on_the_observed_lock_active_row_or_live_records(
    monkeypatch, client_kwargs, failed, refusal
):
    client = _FakeClient(**client_kwargs)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    assert receipt["state"] == "refused"
    assert receipt["failed_checks"] == failed
    assert receipt["refusal"] == refusal
    assert client.script_calls() == []


def test_preflight_reports_the_first_failed_check_when_a_guard_also_fails(monkeypatch):
    names = _check_names()
    client = _FakeClient(failures=(names[7],), live_records=(2, 0, 0))
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    assert receipt["state"] == "refused"
    assert receipt["failed_checks"] == [names[7], "live_records_expected"]
    assert receipt["refusal"] == "execution_origin_registry_digest_mismatch"
    assert receipt["observed_live_records"] == {
        "approvals_v2": 2,
        "consumptions_v2": 0,
        "results_v2": 0,
    }


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
        "refusal": "execution_origin_registry_invalid",
    }
    assert by_name["v2_lock_expected"]["result"] == "fail"
    assert receipt["observed_v2_lock"] is None
    assert receipt["failed_checks"] == [names[8], "v2_lock_expected"]
    assert receipt["refusal"] == "execution_origin_registry_invalid"


class _BigQueryError(Exception):
    """Shaped like google.api_core's GoogleAPICallError: a message and an errors list."""

    def __init__(self, reason, message):
        super().__init__("400 " + message)
        self.message = message
        self.errors = [{"reason": reason, "message": message, "domain": "global"}]


# The live message names the routine, and here also echoes every bound value, a newline, a
# character outside ASCII and a backslash, and runs far past the bound.
ECHOING_MESSAGE = (
    "Access Denied: Routine ogilvy-trends-v2:trends_v2_staging_approvals."
    "fn_is_canonical_execution_json_v1: User does not have permission to query routine "
    f"for {ACTIVATE_REGISTRY} and {ACTIVATE_MANIFEST} and {RETIRE_REGISTRY} {RETIRE_MANIFEST}"
    f" phrase {PHRASE}\nsecond line op\u00e9rateur back\\slash " + "x" * 600
)
ERROR_TEXT = re.compile(r"[ -\[\]-~]{1,300}")


def _assert_bounded_detail(record):
    assert record["error"] == "query_failed"
    assert record["error_reason"] == "accessDenied"
    message = record["error_message"]
    assert ERROR_TEXT.fullmatch(message)
    assert message.startswith(
        "Access Denied: Routine ogilvy-trends-v2:trends_v2_staging_approvals."
        "fn_is_canonical_execution_json_v1: User does not have permission"
    )
    for value in (ACTIVATE_REGISTRY, ACTIVATE_MANIFEST, RETIRE_REGISTRY, RETIRE_MANIFEST):
        assert value not in message
        assert value[:16] not in message
    assert "I rotate" not in message
    assert "Production remains" not in message


def test_preflight_records_the_bounded_error_reason_and_message_of_a_failed_check(monkeypatch):
    names = _check_names()
    failing = (names[8], names[10], names[11])
    client = _FakeClient(
        errors=failing, error_exception=_BigQueryError("accessDenied", ECHOING_MESSAGE)
    )
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    by_name = {item["name"]: item for item in receipt["checks"]}
    assert receipt["state"] == "refused"
    assert receipt["failed_checks"] == list(failing)
    for name in failing:
        assert by_name[name]["result"] == "fail"
        _assert_bounded_detail(by_name[name])
    for name in names:
        if name not in failing:
            assert "error_reason" not in by_name[name]
            assert "error_message" not in by_name[name]
    assert "I rotate" not in json.dumps(receipt)


@pytest.mark.parametrize(
    ("guard", "name"),
    [
        ("lock", "v2_lock_expected"),
        ("active", "active_generation_expected"),
        ("records", "live_records_expected"),
    ],
)
def test_preflight_records_the_bounded_error_of_a_failed_guard_query(monkeypatch, guard, name):
    client = _FakeClient(
        guard_errors=(guard,), error_exception=_BigQueryError("accessDenied", ECHOING_MESSAGE)
    )
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    by_name = {item["name"]: item for item in receipt["checks"]}
    assert receipt["failed_checks"] == [name]
    _assert_bounded_detail(by_name[name])


@pytest.mark.parametrize(
    ("exception", "reason", "message"),
    [
        (RuntimeError("query failed"), "RuntimeError", "query failed"),
        (
            _BigQueryError("not a reason!", "Not found: Function x"),
            "unknown",
            "Not found: Function x",
        ),
        (_BigQueryError("notFound", ""), "notFound", ""),
    ],
    ids=["plain", "reason_outside_the_set", "empty_message"],
)
def test_preflight_error_detail_keeps_a_closed_reason(monkeypatch, exception, reason, message):
    names = _check_names()
    client = _FakeClient(errors=(names[8],), error_exception=exception)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    record = {item["name"]: item for item in receipt["checks"]}[names[8]]
    assert record["error"] == "query_failed"
    assert record["error_reason"] == reason
    assert record["error_message"] == message


@pytest.mark.parametrize("ok", [None, 1, "true", "TRUE", [True], {"ok": True}])
def test_preflight_fails_closed_when_a_check_answers_anything_but_true(monkeypatch, ok):
    names = _check_names()
    client = _FakeClient(ok_values={names[4]: ok})
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = _preflight(client)

    by_name = {item["name"]: item for item in receipt["checks"]}
    assert receipt["state"] == "refused"
    assert by_name[names[4]]["result"] == "fail"
    assert "error" not in by_name[names[4]]
    assert receipt["failed_checks"] == [names[4]]
    assert receipt["refusal"] == "execution_rotation_active_generation_mismatch"
    assert client.script_calls() == []


def test_preflight_refuses_an_empty_session_user(monkeypatch):
    client = _FakeClient(session_user="")
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_approval_identity_invalid$"):
        _preflight(client)
    assert len(client.queries) == 1


def test_preflight_word_writes_the_receipt_and_exits_by_state(tmp_path, monkeypatch, capfd):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("rotate-v2-preflight", paths)) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    receipt = json.loads(captured.out)
    assert receipt["state"] == "ready"
    assert receipt["contract_version"] == "execution_rotation_preflight_v1"
    assert paths["output"].read_bytes() == captured.out.encode("utf-8")
    assert client.script_calls() == []

    refused = _FakeClient(lock_version=2)
    _install_fake(monkeypatch, refused)
    paths["output"].unlink()
    assert migration.main(_arguments("rotate-v2-preflight", paths)) == 1
    captured = capfd.readouterr()
    assert captured.err == '{"error":"execution_rotation_lock_not_ready"}\n'
    receipt = json.loads(captured.out)
    assert receipt["state"] == "refused"
    assert receipt["failed_checks"] == ["v2_lock_expected"]
    assert receipt["observed_v2_lock"] == {"lock_version": 2, "state": "ready"}
    assert json.loads(paths["output"].read_text(encoding="utf-8")) == receipt
    assert refused.script_calls() == []


# Apply


def test_apply_calls_the_script_exactly_once_on_the_ready_path(tmp_path, monkeypatch, capfd):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    calls = client.script_calls()
    assert len(calls) == 1
    assert client.script_job_ids == [JOB_ID]
    bound = {(item.name, item.type_, item.value) for item in calls[0].query_parameters}
    assert bound == {
        ("retire_origin_registry_sha256", "STRING", RETIRE_REGISTRY),
        ("retire_resource_manifest_sha256", "STRING", RETIRE_MANIFEST),
        ("activate_origin_registry_sha256", "STRING", ACTIVATE_REGISTRY),
        ("activate_resource_manifest_sha256", "STRING", ACTIVATE_MANIFEST),
        ("expected_v2_lock_version", "STRING", "1"),
        ("activation_phrase", "STRING", PHRASE),
    }
    assert client.queries[0][0] == "SELECT SESSION_USER() AS session_user"
    assert client.queries[-1][0] == client.script_sql
    receipt = json.loads(captured.out)
    assert receipt["contract_version"] == "execution_rotation_apply_v1"
    assert receipt["state"] == "rotated"
    assert receipt["refusal"] is None
    assert receipt["rotation"] == ROTATION
    assert receipt["preflight"]["state"] == "ready"
    assert receipt["preflight"]["contract_version"] == "execution_rotation_preflight_v1"
    assert receipt["script_sha256"] == SCRIPT_SHA256
    assert receipt["expected_v2_lock_version"] == 1
    assert receipt["parameters"] == REDACTED_PARAMETERS
    assert paths["output"].read_bytes() == captured.out.encode("utf-8")
    assert "I rotate" not in captured.out


def test_apply_never_calls_the_script_on_a_refused_preflight(tmp_path, monkeypatch, capfd):
    client = _FakeClient(failures=("assert_05_execution_rotation_active_generation_mismatch",))
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert captured.err == '{"error":"execution_rotation_active_generation_mismatch"}\n'
    assert client.script_calls() == []
    receipt = json.loads(captured.out)
    assert receipt["contract_version"] == "execution_rotation_apply_v1"
    assert receipt["state"] == "refused"
    assert receipt["rotation"] is None
    assert receipt["preflight"]["state"] == "refused"
    assert json.loads(paths["output"].read_text(encoding="utf-8")) == receipt

    paths["output"].unlink()
    guarded = _FakeClient(lock_version=2)
    _install_fake(monkeypatch, guarded)
    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1
    assert capfd.readouterr().err == '{"error":"execution_rotation_lock_not_ready"}\n'
    assert guarded.script_calls() == []


@pytest.mark.parametrize(
    ("client", "refusal"),
    [
        (
            _FakeClient(script_error="execution_approval_concurrent_conflict"),
            INTERNAL_REFUSAL,
        ),
        (_FakeClient(script_result_error=TimeoutError("read timed out")), INTERNAL_REFUSAL),
        (_FakeClient(script_rows=()), '{"error":"execution_approval_lock_invalid"}'),
        (
            _FakeClient(script_rows=(ROTATION, ROTATION)),
            '{"error":"execution_approval_lock_invalid"}',
        ),
    ],
    ids=["submit_raises", "result_raises", "zero_rows", "two_rows"],
)
def test_error_after_the_submit_keeps_the_marker_instead_of_a_refused_receipt(
    tmp_path, monkeypatch, capfd, client, refusal
):
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert client.script_job_ids == [JOB_ID]
    assert captured.out == ""
    assert captured.err == RESULT_UNREAD + "\n" + refusal + "\n"
    assert paths["output"].read_bytes() == SUBMITTED_MARKER.encode("utf-8") + b"\n"


def test_apply_generates_a_fresh_job_id_per_run(tmp_path, monkeypatch, capfd):
    first, second = _FakeClient(), _FakeClient()
    paths = _write_inputs(tmp_path)
    ids = []
    for client, name in ((first, "first.json"), (second, "second.json")):
        _install_fake(monkeypatch, client)
        assert (
            migration.main(_arguments("rotate-v2-apply", {**paths, "output": tmp_path / name})) == 0
        )
        ids.extend(client.script_job_ids)

    assert capfd.readouterr().err == ""
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(re.fullmatch(r"rotation-[0-9a-f]{32}", item) for item in ids)


OTHER_DIGEST = "e" * 64
JOB_IDENTITY = {"job_id": JOB_ID, "location": JOB_LOCATION, "project": JOB_PROJECT}
UNVERIFIED_CASES = {
    "contract_version": {"contract_version": "open_intelligence_execution_cutover_receipt_v2"},
    "state": {"state": "prepared"},
    "lock_version": {"lock_version": 999},
    "lock_version_unmoved": {"lock_version": 1},
    "lock_version_text": {"lock_version": "2"},
    "lock_version_bool": {"lock_version": True},
    "origin_registry_sha256": {"origin_registry_sha256": OTHER_DIGEST},
    "resource_manifest_sha256": {"resource_manifest_sha256": OTHER_DIGEST},
    "retired_origin_registry_sha256": {"retired_origin_registry_sha256": OTHER_DIGEST},
    "retired_resource_manifest_sha256": {"retired_resource_manifest_sha256": OTHER_DIGEST},
}


@pytest.mark.parametrize("case", list(UNVERIFIED_CASES))
def test_script_row_that_disagrees_with_the_request_is_an_unverified_receipt(
    tmp_path, monkeypatch, capfd, case
):
    row = {**ROTATION, **UNVERIFIED_CASES[case]}
    field = next(iter(UNVERIFIED_CASES[case]))
    client = _FakeClient(script_rows=(row,))
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert client.script_job_ids == [JOB_ID]
    receipt = json.loads(captured.out)
    assert receipt["contract_version"] == "execution_rotation_apply_v1"
    assert receipt["state"] == "unverified"
    assert receipt["state"] != "rotated"
    assert receipt["refusal"] == "execution_rotation_receipt_unverified"
    assert receipt["mismatched_field"] == field
    assert receipt["job"] == JOB_IDENTITY
    assert receipt["observed_rotation"] == row
    assert receipt["rotation"] is None
    assert receipt["preflight"]["state"] == "ready"
    assert paths["output"].read_bytes() == captured.out.encode("utf-8")
    assert captured.err == (
        '{"error":"execution_rotation_receipt_unverified",'
        '"job_id":"rotation-5f3c2a9e1b7d4c08a1b2c3d4e5f60718","location":"EU",'
        f'"mismatched_field":"{field}","project":"rotation-project"}}\n'
    )


def test_reviewer_probe_row_is_not_recorded_as_rotated(monkeypatch):
    """The review probe: a row naming another pair, lock_version 999 and state prepared."""

    retire, activate = ("a" * 64, "b" * 64), (ACTIVATE_REGISTRY, ACTIVATE_MANIFEST)
    bogus = {
        "contract_version": "x",
        "state": "prepared",
        "rotated_at": None,
        "rotated_by": "usr_x",
        "lock_version": 999,
        "retired_origin_registry_sha256": "0" * 64,
        "retired_resource_manifest_sha256": "0" * 64,
        "origin_registry_sha256": "f" * 64,
        "resource_manifest_sha256": "f" * 64,
    }

    class _ProbeClient:
        location = JOB_LOCATION
        project = JOB_PROJECT

        def query(self, *_args, job_id=None, **_kwargs):
            return _Job((bogus,), job_id=job_id)

    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: _ProbeClient())
    monkeypatch.setattr(
        migration,
        "_run_rotation_preflight",
        lambda _credentials, **_kwargs: {
            "state": "ready",
            "refusal": None,
            "parameters": {},
            "script_sha256": "s",
        },
    )
    _pin_job_id(monkeypatch)
    monkeypatch.setattr(migration, "_SUBMITTED_JOB", None)
    monkeypatch.setattr(migration, "_OUTPUT_RESERVATION", None)

    out = migration._run_rotation_apply(
        None,
        retire_pair=retire,
        activate_pair=activate,
        expected_v2_lock_version=5,
        activation_phrase=PHRASE_FORMAT % (*retire, *activate),
    )

    assert out["state"] == "unverified"
    assert out["rotation"] is None
    assert out["mismatched_field"] == "contract_version"
    assert out["observed_rotation"] == bogus
    assert out["job"] == JOB_IDENTITY


def test_existing_unverified_receipt_names_the_job_before_output_exists(
    tmp_path, monkeypatch, capfd
):
    client = _FakeClient(script_rows=({**ROTATION, "lock_version": 999},))
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)
    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1
    written = paths["output"].read_bytes()
    capfd.readouterr()
    _forbid_native(monkeypatch)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == RESULT_UNREAD + "\n" + '{"error":"output_exists"}\n'
    assert paths["output"].read_bytes() == written


@pytest.mark.parametrize("word", WORDS)
def test_existing_output_path_refuses_before_any_read(tmp_path, monkeypatch, capfd, word):
    _forbid_native(monkeypatch)
    paths = _write_inputs(tmp_path)
    paths["output"].write_bytes(b"keep")
    paths["phrase"].unlink()

    assert migration.main(_arguments(word, paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"output_exists"}\n'
    assert paths["output"].read_bytes() == b"keep"


@pytest.mark.parametrize("word", WORDS)
def test_missing_output_directory_refuses_before_any_client(tmp_path, monkeypatch, capfd, word):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)
    paths["output"] = tmp_path / "missing" / "receipt.json"

    assert migration.main(_arguments(word, paths)) == 1

    assert client.script_calls() == []
    assert client.queries == []
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"output_unwritable"}\n'
    assert not paths["output"].exists()
    assert not paths["output"].parent.exists()


def test_apply_writes_the_receipt_to_disk_before_printing_it(tmp_path, monkeypatch, capfd):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)
    original = migration._write_exact_line
    on_disk_at_print: list[bytes] = []

    def _observe(stream, line):
        if stream is sys.stdout:
            on_disk_at_print.append(
                paths["output"].read_bytes() if paths["output"].exists() else b""
            )
        original(stream, line)

    monkeypatch.setattr(migration, "_write_exact_line", _observe)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 0

    captured = capfd.readouterr()
    assert len(client.script_calls()) == 1
    assert on_disk_at_print == [captured.out.encode("utf-8")]
    assert json.loads(captured.out)["state"] == "rotated"


# The reservation


class _InterruptingClient(_FakeClient):
    """Raises the operator's Ctrl+C from the first client call."""

    def query(self, sql, job_config=None):
        if sql == "SELECT SESSION_USER() AS session_user":
            raise KeyboardInterrupt()
        return super().query(sql, job_config)


def _open_with_failing_write(monkeypatch, path: Path) -> None:
    """Make the exclusive open of ``path`` hand back a handle whose write fails."""

    class _FullDisk(io.FileIO):
        def write(self, data):
            raise OSError(28, "No space left on device")

    real_open = open

    def _open(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and mode == "xb":
            return _FullDisk(file, "xb")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(migration, "open", _open, raising=False)


@pytest.mark.parametrize("word", ["rotate-v2-preflight", "rotate-v2-apply"])
def test_interrupt_inside_the_build_discards_the_reservation(tmp_path, monkeypatch, capfd, word):
    client = _InterruptingClient()
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        migration.main(_arguments(word, paths))

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert client.script_calls() == []
    assert not paths["output"].exists()

    retry = _FakeClient()
    _install_fake(monkeypatch, retry)
    assert migration.main(_arguments(word, paths)) == 0
    captured = capfd.readouterr()
    assert captured.err == ""
    assert paths["output"].read_bytes() == captured.out.encode("utf-8")


@pytest.mark.parametrize("word", ["rotate-v2-preflight", "rotate-v2-apply"])
def test_native_error_inside_the_build_discards_the_reservation(tmp_path, monkeypatch, capfd, word):
    def _no_credentials():
        raise RuntimeError("Could not automatically determine credentials")

    _forbid_native(monkeypatch)
    monkeypatch.setattr(migration, "_load_credentials", _no_credentials)
    paths = _write_inputs(tmp_path)

    assert migration.main(_arguments(word, paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"execution_approval_internal_refusal"}\n'
    assert not paths["output"].exists()


def test_write_failure_after_a_committed_apply_prints_the_receipt_first(
    tmp_path, monkeypatch, capfd
):
    client = _FakeClient(session_user=SESSION_USER)
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)
    _open_with_failing_write(monkeypatch, paths["output"])

    original = migration._write_exact_line
    order: list[tuple[str, str]] = []

    def _observe(stream, line):
        order.append(("out" if stream is sys.stdout else "err", line))
        original(stream, line)

    monkeypatch.setattr(migration, "_write_exact_line", _observe)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert len(client.script_calls()) == 1
    assert captured.err == '{"error":"execution_approval_internal_refusal"}\n'
    assert captured.out.encode("utf-8") == APPLY_LINE.encode("utf-8") + b"\n"
    assert order == [
        ("out", APPLY_LINE),
        ("err", '{"error":"execution_approval_internal_refusal"}'),
    ]
    assert paths["output"].read_bytes() == b""


@pytest.mark.parametrize("word", ["rotate-v2-dry-run", "rotate-v2-preflight"])
def test_write_failure_on_a_read_only_word_discards_the_reservation(
    tmp_path, monkeypatch, capfd, word
):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)
    _open_with_failing_write(monkeypatch, paths["output"])

    assert migration.main(_arguments(word, paths)) == 1

    captured = capfd.readouterr()
    assert client.script_calls() == []
    assert captured.err == '{"error":"execution_approval_internal_refusal"}\n'
    if word == "rotate-v2-dry-run":
        assert captured.out.encode("utf-8") == DRY_RUN_LINE.encode("utf-8") + b"\n"
    assert not paths["output"].exists()

    monkeypatch.setattr(migration, "open", open, raising=False)
    assert migration.main(_arguments(word, paths)) == 0
    retry = capfd.readouterr()
    assert retry.err == ""
    assert paths["output"].read_bytes() == retry.out.encode("utf-8")
    assert captured.out == retry.out


class _InterruptedWaitClient(_FakeClient):
    """Submits the script job, then the operator's Ctrl+C lands while its result is awaited."""

    def query(self, sql, job_config=None, job_id=None):
        job = super().query(sql, job_config, job_id)
        if sql == self.script_sql:

            class _Unfinished:
                job_id = job.job_id
                location = job.location
                project = job.project

                def result(self_inner):
                    raise KeyboardInterrupt()

            return _Unfinished()
        return job


def test_interrupt_during_the_wait_keeps_the_job_id_in_the_reservation(
    tmp_path, monkeypatch, capfd
):
    client = _InterruptedWaitClient()
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        migration.main(_arguments("rotate-v2-apply", paths))

    captured = capfd.readouterr()
    assert len(client.script_calls()) == 1
    assert client.script_job_ids == [JOB_ID]
    assert captured.out == ""
    assert captured.err == RESULT_UNREAD + "\n"
    assert paths["output"].read_bytes() == SUBMITTED_MARKER.encode("utf-8") + b"\n"

    retry = _FakeClient()
    _install_fake(monkeypatch, retry)
    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == RESULT_UNREAD + "\n" + '{"error":"output_exists"}\n'
    assert retry.queries == []
    assert paths["output"].read_bytes() == SUBMITTED_MARKER.encode("utf-8") + b"\n"


def test_interrupt_during_the_wait_prints_the_marker_when_the_reservation_cannot_take_it(
    tmp_path, monkeypatch, capfd
):
    client = _InterruptedWaitClient()
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)
    _open_with_failing_write(monkeypatch, paths["output"])

    with pytest.raises(KeyboardInterrupt):
        migration.main(_arguments("rotate-v2-apply", paths))

    captured = capfd.readouterr()
    assert len(client.script_calls()) == 1
    assert captured.out == SUBMITTED_MARKER + "\n"
    assert captured.err == RESULT_UNREAD + "\n"
    assert paths["output"].read_bytes() == b""


class _InterruptedSubmitClient(_FakeClient):
    """The operator's Ctrl+C lands inside the submit call, after the insert left the socket."""

    def query(self, sql, job_config=None, job_id=None):
        if sql == self.script_sql:
            self.script_job_ids.append(job_id)
            raise KeyboardInterrupt()
        return super().query(sql, job_config, job_id)


def test_interrupt_inside_the_submit_keeps_the_pre_generated_job_id_in_the_reservation(
    tmp_path, monkeypatch, capfd
):
    client = _InterruptedSubmitClient()
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        migration.main(_arguments("rotate-v2-apply", paths))

    captured = capfd.readouterr()
    assert client.script_job_ids == [JOB_ID]
    assert captured.out == ""
    assert captured.err == RESULT_UNREAD + "\n"
    assert paths["output"].read_bytes() == SUBMITTED_MARKER.encode("utf-8") + b"\n"
    marker = json.loads(paths["output"].read_bytes())
    assert marker["job_id"] == client.script_job_ids[0]


class _DeadStdout:
    """A stdout whose pipe has gone: every write raises."""

    class _Buffer:
        def write(self, _data):
            raise BrokenPipeError(32, "Broken pipe")

    buffer = _Buffer()

    def write(self, _text):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        return None


@pytest.mark.parametrize(
    ("client", "exit_shape"),
    [
        (_InterruptedWaitClient(), KeyboardInterrupt),
        (_FakeClient(script_result_error=TimeoutError("read timed out")), 1),
    ],
    ids=["interrupt", "refusal"],
)
def test_double_write_failure_still_names_the_job_on_stderr(
    tmp_path, monkeypatch, capfd, client, exit_shape
):
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)
    _open_with_failing_write(monkeypatch, paths["output"])
    monkeypatch.setattr(sys, "stdout", _DeadStdout())

    if exit_shape is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            migration.main(_arguments("rotate-v2-apply", paths))
        expected_err = RESULT_UNREAD + "\n"
    else:
        assert migration.main(_arguments("rotate-v2-apply", paths)) == exit_shape
        expected_err = RESULT_UNREAD + "\n" + INTERNAL_REFUSAL + "\n"

    captured = capfd.readouterr()
    assert client.script_job_ids == [JOB_ID]
    assert captured.out == ""
    assert captured.err == expected_err
    assert paths["output"].read_bytes() == b""


class _InterruptedStdout:
    """A stdout on which the operator's second Ctrl+C lands during the fallback write."""

    class _Buffer:
        def write(self, _data):
            raise KeyboardInterrupt()

    buffer = _Buffer()

    def write(self, _text):
        raise KeyboardInterrupt()

    def flush(self):
        return None


def test_second_interrupt_during_the_fallback_finds_the_job_already_on_stderr(
    tmp_path, monkeypatch, capfd
):
    client = _FakeClient(script_result_error=TimeoutError("read timed out"))
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)
    _open_with_failing_write(monkeypatch, paths["output"])
    monkeypatch.setattr(sys, "stdout", _InterruptedStdout())

    with pytest.raises(KeyboardInterrupt):
        migration.main(_arguments("rotate-v2-apply", paths))

    captured = capfd.readouterr()
    assert client.script_job_ids == [JOB_ID]
    assert captured.out == ""
    assert captured.err == RESULT_UNREAD + "\n"
    assert paths["output"].read_bytes() == b""


class _MarkerReadingClient(_FakeClient):
    """Records what the reservation holds at the moment the script job is submitted."""

    def __init__(self, output: Path, **kwargs):
        super().__init__(**kwargs)
        self.output = output
        self.on_disk_at_submit: bytes | None = None

    def query(self, sql, job_config=None, job_id=None):
        if sql == self.script_sql:
            self.on_disk_at_submit = self.output.read_bytes()
        return super().query(sql, job_config, job_id)


def test_marker_is_on_disk_before_the_script_is_submitted(tmp_path, monkeypatch, capfd):
    paths = _write_inputs(tmp_path)
    client = _MarkerReadingClient(paths["output"], session_user=SESSION_USER)
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 0

    captured = capfd.readouterr()
    assert captured.err == ""
    assert client.on_disk_at_submit == SUBMITTED_MARKER.encode("utf-8") + b"\n"
    assert paths["output"].read_bytes() == captured.out.encode("utf-8")
    assert captured.out.encode("utf-8") == APPLY_LINE.encode("utf-8") + b"\n"


def test_error_after_the_wait_returned_keeps_the_marker(tmp_path, monkeypatch, capfd):
    client = _FakeClient()
    _install_fake(monkeypatch, client)
    _pin_job_id(monkeypatch)
    paths = _write_inputs(tmp_path)
    original = migration._apply_v2_rotation

    def _apply_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("receipt assembly failed after the wait")

    monkeypatch.setattr(migration, "_apply_v2_rotation", _apply_then_fail)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert client.script_job_ids == [JOB_ID]
    assert captured.out == ""
    assert captured.err == RESULT_UNREAD + "\n" + INTERNAL_REFUSAL + "\n"
    assert paths["output"].read_bytes() == SUBMITTED_MARKER.encode("utf-8") + b"\n"


@pytest.mark.parametrize("word", WORDS)
@pytest.mark.parametrize("race", [False, True], ids=["seen_by_exists", "seen_by_open"])
def test_existing_submitted_marker_names_the_job_before_output_exists(
    tmp_path, monkeypatch, capfd, word, race
):
    _forbid_native(monkeypatch)
    paths = _write_inputs(tmp_path)
    paths["output"].write_bytes(SUBMITTED_MARKER.encode("utf-8") + b"\n")
    if race:
        real_exists = Path.exists
        monkeypatch.setattr(
            Path, "exists", lambda self: False if self == paths["output"] else real_exists(self)
        )

    assert migration.main(_arguments(word, paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == RESULT_UNREAD + "\n" + '{"error":"output_exists"}\n'
    assert paths["output"].read_bytes() == SUBMITTED_MARKER.encode("utf-8") + b"\n"


@pytest.mark.parametrize(
    "content",
    [b"keep", b"", b'{"contract_version":"execution_rotation_apply_v1"}\n', b"{not json\n"],
    ids=["text", "empty", "receipt", "broken"],
)
def test_existing_output_that_is_not_a_marker_refuses_with_output_exists_only(
    tmp_path, monkeypatch, capfd, content
):
    _forbid_native(monkeypatch)
    paths = _write_inputs(tmp_path)
    paths["output"].write_bytes(content)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"output_exists"}\n'
    assert paths["output"].read_bytes() == content


def _open_with_failing_close(monkeypatch, path: Path) -> None:
    """Make the exclusive open of ``path`` hand back a handle whose first close fails."""

    class _Unclosable(io.FileIO):
        def close(self):
            if self.closed:
                return
            super().close()
            raise OSError(5, "Input/output error")

    real_open = open

    def _open(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and mode == "xb":
            return _Unclosable(file, "xb")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(migration, "open", _open, raising=False)


def test_reservation_that_cannot_be_closed_is_reported_and_still_removed(
    tmp_path, monkeypatch, capfd
):
    client = _FakeClient(session_user="")
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)
    _open_with_failing_close(monkeypatch, paths["output"])

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == (
        '{"error":"output_reservation_unclosed"}\n{"error":"execution_approval_identity_invalid"}\n'
    )
    assert client.script_calls() == []
    assert not paths["output"].exists()


@pytest.mark.parametrize("word", WORDS)
def test_output_that_appears_after_the_exists_check_is_refused_untouched(
    tmp_path, monkeypatch, capfd, word
):
    _forbid_native(monkeypatch)
    paths = _write_inputs(tmp_path)
    paths["output"].write_bytes(b"keep")
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda self: False if self == paths["output"] else real_exists(self)
    )

    assert migration.main(_arguments(word, paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"output_exists"}\n'
    assert paths["output"].read_bytes() == b"keep"


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError(13, "held by another process"),
        OSError(5, "Input/output error"),
        OSError(28, "No space left on device"),
    ],
    ids=["permission", "io_error", "disk_full"],
)
def test_reservation_that_cannot_be_removed_is_reported_before_the_refusal(
    tmp_path, monkeypatch, capfd, failure
):
    client = _FakeClient(session_user="")
    _install_fake(monkeypatch, client)
    paths = _write_inputs(tmp_path)
    real_unlink = Path.unlink

    def _unlink(self, *args, **kwargs):
        if self == paths["output"]:
            raise failure
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)

    assert migration.main(_arguments("rotate-v2-apply", paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == (
        '{"error":"output_reservation_retained"}\n{"error":"execution_approval_identity_invalid"}\n'
    )
    assert client.script_calls() == []
    assert paths["output"].read_bytes() == b""


@pytest.mark.parametrize("word", WORDS)
def test_wrong_phrase_refuses_before_any_client(tmp_path, monkeypatch, capfd, word):
    _forbid_native(monkeypatch)
    paths = _write_inputs(tmp_path, phrase=PHRASE.replace("I rotate", "I activate"))

    assert migration.main(_arguments(word, paths)) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"execution_approval_manifest_mismatch"}\n'
    assert not paths["output"].exists()


# Subprocess


def test_subprocess_dry_run_prints_the_exact_stdout_line(tmp_path):
    paths = _write_inputs(tmp_path)
    process = subprocess.run(
        [sys.executable, str(migration.__file__), *_arguments("rotate-v2-dry-run", paths)],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    expected = (
        json.dumps(
            {
                "contract_version": "execution_rotation_dry_run_v1",
                "expected_v2_lock_version": 1,
                "parameters": REDACTED_PARAMETERS,
                "script_path": SCRIPT_RELATIVE_PATH,
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
        [sys.executable, str(migration.__file__), "rotate-v2-dry-run", "--output", "x"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert process.stdout == b""
    assert process.stderr == GRAMMAR_ERROR
