"""Run canary contract tests and kill bounded migration source mutants."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "migrations" / "create_gemini_canary_approval_store.py"
MIGRATION_TEST = "tests/unit/test_gemini_canary_approval_store_migration.py"
TESTS = (
    "tests/unit/test_gemini_canary_requests.py",
    "tests/unit/test_gemini_canary_receipts.py",
    "tests/unit/test_gemini_canary_callers.py",
    "tests/unit/test_gemini_canary_review_repairs.py",
    MIGRATION_TEST,
    "tests/unit/test_gemini_canary_approvals.py",
)
EXPRESSION = " or ".join(
    (
        "manifest_loader_fails_closed_on_digest_drift",
        "reconciliation_marks_missing_terminal_uncertain",
        "reconciliation_accepts_one_pair_and_blocks_orphans",
        "terminal_contract_version_must_match",
        "nonmember_output_id_is_rejected",
        "summary_rejects_an_audience_lens",
        "answer_rejects_nonmember_and_forbidden",
        "election_answer_cannot_remove",
        "reservation_readback_failure_stops",
        "terminal_readback_failure_never",
        "incomplete_usage_metadata_writes_partial",
        "all_eleven_answers_stop_before_count",
        "planning_rejects_every_frozen_field_drift",
        "answer_semantics_reject_unsupported_text",
        "summary_semantics_reject_demographic_and_causal_text",
        "noncomplete_terminal_requires_exact_postwrite_readback",
        "three_same_stage_attempts_get_distinct",
        "sinks_reject_forged_reservation_identity",
        "reconciliation_rejects_wrong_run_or_contract",
        "request_keeps_http_options_on_validated_injected_client",
        "schema_prose_coverage_is_exact",
        "all_seven_previous_semantic_escape_fields_are_rejected",
        "gemini_canary_approval_store_migration",
        "exact_eleven_plan_batch_reproduces",
        "batch_scope_and_manifest_mutations_fail",
        "action_request_has_no_actor_time_plan",
        "phrase_actor_and_server_clock_are_exact",
        "planning_outputs_and_claim_requirements_are_exact",
        "store_append_is_atomic_insert_only",
        "companion_and_readback_tamper_fail",
        "import_and_proposal_approval_create_no_records",
        "answer_enables_only_with_exact_companion_readback",
        "store_rejects_rehashed_record_scope_or_batch_identity_drift",
        "correction_appends_new_manifest_without_overwriting_old_companion",
    )
)


@dataclass(frozen=True, slots=True)
class _Mutant:
    name: str
    needle: str
    replacement: str
    test: str


MUTANTS = (
    _Mutant(
        "nullability",
        'suffix = " NOT NULL" if mode == "REQUIRED" else ""',
        'suffix = ""',
        "test_emitted_ddl_parses_exact_schema_and_nullability",
    ),
    _Mutant(
        "identity",
        "if identity != MIGRATION_IDENTITY:",
        "if False:",
        "test_apply_rejects_wrong_identity_before_client_construction",
    ),
    _Mutant(
        "credential_type_membership",
        """if credential_type not in (
        service_account.Credentials,
        compute_credentials.Credentials,
    ):""",
        """if not isinstance(
        credentials,
        (service_account.Credentials, compute_credentials.Credentials),
    ):""",
        "test_credential_type_rejection_happens_before_client_construction",
    ),
    _Mutant(
        "idempotence",
        "if discovered[spec.name] is None:",
        "if True:",
        "test_second_apply_is_idempotent",
    ),
    _Mutant(
        "server_timestamps",
        "  CURRENT_TIMESTAMP(),\n  CURRENT_TIMESTAMP()",
        "  CURRENT_TIMESTAMP(),\n  NULL",
        "test_lock_seed_uses_two_server_timestamps",
    ),
    _Mutant(
        "query_location",
        "client.query(spec.ddl, location=LOCATION)",
        "client.query(spec.ddl)",
        "test_every_query_uses_us_location",
    ),
    _Mutant(
        "where_not_exists",
        'WHERE NOT EXISTS (SELECT 1 FROM `{table_ref}`);"""',
        ';"""',
        "test_lock_seed_has_where_not_exists_guard",
    ),
)


def _pytest_command(*tests: str) -> list[str]:
    return [
        sys.executable,
        "-W",
        "ignore",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *tests,
        "-q",
    ]


def _run_baseline() -> None:
    command = [*_pytest_command(*TESTS), "-k", EXPRESSION]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def _mutated_source(source: str, mutant: _Mutant) -> str:
    if source.count(mutant.needle) != 1:
        raise RuntimeError(f"mutation anchor drift: {mutant.name}")
    return source.replace(mutant.needle, mutant.replacement, 1)


def _kill_mutant(source: str, mutant: _Mutant) -> bool:
    with tempfile.TemporaryDirectory(prefix=f"canary-{mutant.name}-") as directory:
        mutated_path = Path(directory) / SOURCE.name
        mutated_path.write_text(_mutated_source(source, mutant), encoding="utf-8")
        environment = os.environ.copy()
        environment["CANARY_MIGRATION_SCRIPT"] = str(mutated_path)
        completed = subprocess.run(
            [*_pytest_command(MIGRATION_TEST), "-k", mutant.test],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    output = completed.stdout + completed.stderr
    if completed.returncode == 0:
        print(f"SURVIVED {mutant.name}: {mutant.test}")
        return False
    if mutant.test not in output:
        print(f"INVALID {mutant.name}: expected failure marker missing")
        print(output)
        return False
    print(f"KILLED {mutant.name}: {mutant.test}")
    return True


def main() -> None:
    _run_baseline()
    source = SOURCE.read_text(encoding="utf-8")
    results = [_kill_mutant(source, mutant) for mutant in MUTANTS]
    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
