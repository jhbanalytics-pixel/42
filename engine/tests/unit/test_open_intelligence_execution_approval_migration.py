"""Structural proof for the isolated execution approval store bootstrap."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "infra" / "bigquery_schemas"
ROUTINES = ROOT / "infra" / "bigquery_routines"

TABLES = (
    "open_intelligence_execution_approvals_v1",
    "open_intelligence_execution_consumptions_v1",
    "open_intelligence_execution_results_v1",
    "open_intelligence_execution_approval_lock_v1",
)
ROUTINE_NAMES = (
    "sp_approve_open_intelligence_execution_v1",
    "sp_read_open_intelligence_execution_approval_v1",
    "sp_read_open_intelligence_execution_result_v1",
    "sp_consume_open_intelligence_execution_v1",
    "sp_record_open_intelligence_execution_result_v1",
    "sp_read_open_intelligence_execution_result_chain_v1",
    "sp_import_open_intelligence_execution_bootstrap_v1",
    "sp_disable_open_intelligence_execution_approval_v1",
    "sp_consume_open_intelligence_source_snapshot_v1",
)
READ_ONLY_ROUTINES = {
    "sp_read_open_intelligence_execution_approval_v1",
    "sp_read_open_intelligence_execution_result_v1",
    "sp_read_open_intelligence_execution_result_chain_v1",
}
EXPECTED_FIELDS = {
    TABLES[0]: (
        ("approval_contract_version", "STRING", "REQUIRED", "Approval row contract version."),
        ("approval_id", "STRING", "REQUIRED", "Deterministic immutable approval identifier."),
        ("manifest_version", "STRING", "REQUIRED", "Canonical execution manifest version."),
        ("operation", "STRING", "REQUIRED", "Approved staging operation."),
        ("contract_sha256", "STRING", "REQUIRED", "Human-approved operation contract digest."),
        ("manifest_sha256", "STRING", "REQUIRED", "Digest of exact canonical manifest bytes."),
        (
            "canonical_manifest_json",
            "STRING",
            "REQUIRED",
            "Complete canonical execution manifest JSON.",
        ),
        ("approved_by", "STRING", "REQUIRED", "Salted human SESSION_USER pseudonym."),
        ("approved_at", "TIMESTAMP", "REQUIRED", "BigQuery transaction-owned approval time."),
        ("expires_at", "TIMESTAMP", "REQUIRED", "Latest UTC time at which consumption may commit."),
        (
            "approval_phrase_sha256",
            "STRING",
            "REQUIRED",
            "Digest of the exact approval phrase bytes.",
        ),
    ),
    TABLES[1]: (
        ("consumption_contract_version", "STRING", "REQUIRED", "Consumption row contract version."),
        ("consumption_id", "STRING", "REQUIRED", "Deterministic immutable consumption identifier."),
        ("approval_id", "STRING", "REQUIRED", "Consumed approval identifier."),
        ("manifest_sha256", "STRING", "REQUIRED", "Consumed manifest digest."),
        ("operation", "STRING", "REQUIRED", "Consumed staging operation."),
        ("execution_name", "STRING", "REQUIRED", "Immutable Cloud Run Execution resource."),
        ("job_resource", "STRING", "REQUIRED", "Exact parent Cloud Run Job resource."),
        ("source_sha", "STRING", "REQUIRED", "Build-proven source Git SHA."),
        ("image_uri", "STRING", "REQUIRED", "Complete immutable Artifact Registry image URI."),
        ("consumed_at", "TIMESTAMP", "REQUIRED", "BigQuery transaction-owned consumption time."),
    ),
    TABLES[2]: (
        ("result_contract_version", "STRING", "REQUIRED", "Result link contract version."),
        ("result_id", "STRING", "REQUIRED", "Deterministic immutable result identifier."),
        ("consumption_id", "STRING", "REQUIRED", "Consumed execution authority identifier."),
        ("approval_id", "STRING", "REQUIRED", "Human approval identifier."),
        ("manifest_sha256", "STRING", "REQUIRED", "Approved manifest digest."),
        ("operation", "STRING", "REQUIRED", "Completed staging operation."),
        ("execution_name", "STRING", "REQUIRED", "Immutable Cloud Run Execution resource."),
        (
            "result_reference",
            "STRING",
            "REQUIRED",
            "Stable operation receipt or canonical output reference.",
        ),
        (
            "canonical_result_json",
            "STRING",
            "REQUIRED",
            "Complete canonical operation receipt or bootstrap proof JSON.",
        ),
        ("result_digest", "STRING", "REQUIRED", "Digest of the referenced operation result."),
        ("status", "STRING", "REQUIRED", "Exactly succeeded or failed."),
        ("completed_at", "TIMESTAMP", "REQUIRED", "BigQuery transaction-owned result-link time."),
    ),
    TABLES[3]: (
        ("lock_name", "STRING", "REQUIRED", "Singleton execution approval lock name."),
        (
            "approval_contract_version",
            "STRING",
            "REQUIRED",
            "Approval contract version guarded by this lock.",
        ),
        ("lock_version", "INT64", "REQUIRED", "Monotonic lock mutation version."),
        (
            "state",
            "STRING",
            "REQUIRED",
            "Exactly ready, approving, consuming, recording_result or disabled.",
        ),
        ("last_approval_id", "STRING", "NULLABLE", "Most recently committed approval identifier."),
        ("created_at", "TIMESTAMP", "REQUIRED", "BigQuery server-owned lock creation time."),
        ("updated_at", "TIMESTAMP", "REQUIRED", "BigQuery transaction-owned lock update time."),
    ),
}
EXPECTED_PARAMETERS = {
    ROUTINE_NAMES[0]: (
        ("canonical_manifest_json", "STRING"),
        ("manifest_sha256", "STRING"),
        ("approval_phrase", "STRING"),
    ),
    ROUTINE_NAMES[1]: (("manifest_sha256", "STRING"),),
    ROUTINE_NAMES[2]: (("p_consumption_id", "STRING"),),
    ROUTINE_NAMES[3]: (
        ("manifest_sha256", "STRING"),
        ("execution_name", "STRING"),
        ("job_resource", "STRING"),
        ("source_sha", "STRING"),
        ("image_uri", "STRING"),
    ),
    ROUTINE_NAMES[4]: (
        ("consumption_id", "STRING"),
        ("result_reference", "STRING"),
        ("canonical_result_json", "STRING"),
        ("result_digest", "STRING"),
        ("status", "STRING"),
    ),
    ROUTINE_NAMES[5]: (
        ("p_source_operation", "STRING"),
        ("p_run_id", "STRING"),
    ),
    ROUTINE_NAMES[6]: (
        ("canonical_manifest_json", "STRING"),
        ("manifest_sha256", "STRING"),
        ("manifest_object", "STRING"),
        ("manifest_generation", "INT64"),
        ("manifest_created_at", "TIMESTAMP"),
        ("signature_object", "STRING"),
        ("signature_generation", "INT64"),
        ("signature_sha256", "STRING"),
        ("kms_key_version", "STRING"),
        ("signature_created_at", "TIMESTAMP"),
        ("bootstrap_lock_object", "STRING"),
        ("bootstrap_lock_generation", "INT64"),
        ("bootstrap_lock_created_at", "TIMESTAMP"),
        ("execution_name", "STRING"),
        ("job_resource", "STRING"),
        ("source_sha", "STRING"),
        ("image_uri", "STRING"),
        ("migration_result_reference", "STRING"),
        ("migration_result_digest", "STRING"),
    ),
    ROUTINE_NAMES[7]: (("approval_phrase", "STRING"),),
    ROUTINE_NAMES[8]: tuple(
        (name, "STRING")
        for name in (
            "manifest_sha256",
            "execution_name",
            "job_resource",
            "source_sha",
            "image_uri",
            "capture_plan_json",
            "recovery_context_json",
            "storage_policy_json",
        )
    ),
}


def _bootstrap_manifest(*, expires_delta: timedelta = timedelta(hours=1)) -> bytes:
    manifest = {
        "arguments": [
            "scripts/migrations/create_open_intelligence_execution_approval_store.py",
            "apply",
        ],
        "build_resource": "projects/ogilvy-trends-v2/locations/us-central1/builds/build-1",
        "command": ["python"],
        "contract_sha256": migration.APPROVED_BOOTSTRAP_CONTRACT_SHA256,
        "datasets": ["trends_v2_staging_approvals"],
        "environment": [
            {"name": "BIGQUERY_DATASET", "value": "trends_v2_staging_approvals"},
            {"name": "GCP_PROJECT", "value": "ogilvy-trends-v2"},
            {"name": "TRENDS_ENV", "value": "staging"},
        ],
        "expires_at": (datetime.now(UTC) + expires_delta).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "image_uri": "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:"
        + "b" * 64,
        "input_artifacts": [
            {"name": name, "sha256": "c" * 64}
            for name in (
                "bootstrap_contract",
                "build_provenance",
                "iam_plan",
                "kms_public_key",
                "migration_dry_run",
                "migration_plan",
            )
        ],
        "job_resource": "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-approval-bootstrap-staging",
        "limits": {
            "max_bytes_billed": 0,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 4,
        },
        "location": "US",
        "manifest_version": "open_intelligence_execution_manifest_v1",
        "max_retries": 0,
        "operation": "bootstrap_migration_apply",
        "project": "ogilvy-trends-v2",
        "secrets": [],
        "service_identity": "trends-engine-oi-bootstrap@ogilvy-trends-v2.iam.gserviceaccount.com",
        "source_sha": "a" * 40,
        "timeout_seconds": 900,
    }
    return json.dumps(
        manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _fixture_inputs() -> migration.BootstrapInputs:
    return migration.BootstrapInputs(
        manifest_bytes=b"{}",
        signature_bytes=b"signature",
        public_key_pem=b"public-key",
        object_generations={"manifest": 1, "signature": 2},
        build_payload={},
    )


def _fixture_authority() -> migration.BootstrapAuthority:
    manifest_bytes = _bootstrap_manifest()
    return migration.BootstrapAuthority(
        manifest=json.loads(manifest_bytes),
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        object_generations={"manifest": 1, "signature": 2},
    )


def _fixture_receipt() -> migration.ApprovalStoreMigrationReceipt:
    return migration.ApprovalStoreMigrationReceipt(
        manifest_sha256="a" * 64,
        approval_id="exa_" + "b" * 64,
        consumption_id="exc_" + "c" * 64,
        result_id="exr_" + "d" * 64,
        migration_result_reference="gs://fixture/result.json",
        migration_result_digest="e" * 64,
        bootstrap_proof_digest="f" * 64,
    )


def _parse_schema(sql: str) -> tuple[tuple[str, str, str, str], ...]:
    import re

    return tuple(
        (
            match.group("name"),
            match.group("type"),
            "REQUIRED" if match.group("required") else "NULLABLE",
            match.group("description"),
        )
        for match in re.finditer(
            r"^  (?P<name>[a-z0-9_]+) (?P<type>STRING|INT64|TIMESTAMP)"
            r"(?P<required> NOT NULL)? OPTIONS\(description = '(?P<description>[^']+)'\),?$",
            sql,
            re.MULTILINE,
        )
    )


def test_plan_contains_exact_isolated_store_and_seed():
    plan = migration.build_plan()
    assert (plan.project, plan.dataset, plan.location) == (
        "ogilvy-trends-v2",
        "trends_v2_staging_approvals",
        "US",
    )
    assert plan.default_table_expiration is None
    assert plan.time_travel_hours == 168
    assert tuple(item.name for item in plan.tables) == TABLES
    assert tuple(item.name for item in plan.routines) == ROUTINE_NAMES
    assert plan.lock_seed == {
        "lock_name": "open_intelligence_execution_approval_v1",
        "approval_contract_version": "open_intelligence_execution_approval_v1",
        "lock_version": 0,
        "state": "ready",
        "last_approval_id": None,
    }


@pytest.mark.parametrize("table", TABLES)
def test_schema_is_exact_ordered_unpartitioned_and_without_expiry(table: str):
    sql = (SCHEMAS / f"{table}.sql").read_text(encoding="utf-8")
    assert f"`{{project}}.{{dataset}}.{table}`" in sql
    assert _parse_schema(sql) == EXPECTED_FIELDS[table]
    assert "PARTITION BY" not in sql
    assert "CLUSTER BY" not in sql
    assert "expiration_timestamp" not in sql


def test_schema_table_descriptions_are_exact():
    expected = {
        TABLES[0]: "Immutable human approvals for one exact staging execution manifest.",
        TABLES[1]: "Immutable one-use reservations binding an approval to one Cloud Run Execution.",
        TABLES[2]: "Immutable terminal links from one consumed approval to one operation receipt.",
        TABLES[
            3
        ]: "Mutable singleton serialization and emergency-disable state for execution approvals.",
    }
    for table, description in expected.items():
        sql = (SCHEMAS / f"{table}.sql").read_text(encoding="utf-8")
        assert f"description = '{description}'" in sql


@pytest.mark.parametrize("routine_name", ROUTINE_NAMES)
def test_authorized_routine_signature_and_transaction_markers_are_exact(routine_name: str):
    item = next(item for item in migration.build_plan().routines if item.name == routine_name)
    assert item.parameters == EXPECTED_PARAMETERS[routine_name]
    sql = item.sql
    if routine_name in READ_ONLY_ROUTINES:
        assert "BEGIN TRANSACTION" not in sql
    else:
        assert "BEGIN TRANSACTION" in sql
        assert "COMMIT TRANSACTION" in sql
    assert "SESSION_USER()" in sql


def test_routines_never_compare_a_parameter_to_itself():
    forbidden = (
        "manifest_sha256 = manifest_sha256",
        "canonical_manifest_json = canonical_manifest_json",
        "consumption_id = consumption_id",
        "result_reference = result_reference",
    )
    for routine in migration.build_plan().routines:
        assert all(fragment not in routine.sql for fragment in forbidden), routine.name


@pytest.mark.parametrize(
    "routine_name",
    [
        "sp_approve_open_intelligence_execution_v1",
        "sp_consume_open_intelligence_execution_v1",
        "sp_record_open_intelligence_execution_result_v1",
    ],
)
def test_transaction_begins_before_authority_load_and_server_time(routine_name: str):
    sql = (ROUTINES / f"{routine_name}.sql").read_text(encoding="utf-8")
    begin = sql.index("BEGIN TRANSACTION")
    assert begin < sql.index("CURRENT_TIMESTAMP()")
    authority_marker = {
        "sp_approve_open_intelligence_execution_v1": "SESSION_USER()",
        "sp_consume_open_intelligence_execution_v1": "SELECT AS STRUCT approval_id",
        "sp_record_open_intelligence_execution_result_v1": "SELECT AS STRUCT approval_id",
    }[routine_name]
    assert begin < sql.index(authority_marker)


@pytest.mark.parametrize(
    "routine_name",
    tuple(name for name in ROUTINE_NAMES if name not in READ_ONLY_ROUTINES),
)
def test_every_lock_transition_uses_the_full_versioned_predicate(routine_name: str):
    sql = (ROUTINES / f"{routine_name}.sql").read_text(encoding="utf-8")
    assert sql.count("approval_contract_version = 'open_intelligence_execution_approval_v1'") >= 3


@pytest.mark.parametrize(
    ("routine_name", "field_names"),
    [
        (
            "sp_approve_open_intelligence_execution_v1",
            tuple(name for name, *_ in EXPECTED_FIELDS[TABLES[0]]),
        ),
        (
            "sp_consume_open_intelligence_execution_v1",
            tuple(name for name, *_ in EXPECTED_FIELDS[TABLES[1]]),
        ),
        (
            "sp_record_open_intelligence_execution_result_v1",
            tuple(name for name, *_ in EXPECTED_FIELDS[TABLES[2]]),
        ),
    ],
)
def test_mutating_routine_readback_names_every_stored_field(
    routine_name: str,
    field_names: tuple[str, ...],
):
    sql = (ROUTINES / f"{routine_name}.sql").read_text(encoding="utf-8")
    marker = "AS 'execution_approval_schema_mismatch'"
    readback = sql[sql.rindex("ASSERT", 0, sql.rindex(marker)) : sql.rindex(marker)]
    for field_name in field_names:
        assert field_name in readback, f"{routine_name} readback omits {field_name}"


def test_routine_predicates_do_not_shadow_parameters():
    failures = []
    for routine_name, parameters in EXPECTED_PARAMETERS.items():
        sql = (ROUTINES / f"{routine_name}.sql").read_text(encoding="utf-8")
        for parameter, _type in parameters:
            pattern = (
                rf"(?:\b[a-z_][a-z0-9_]*\.)?{re.escape(parameter)}\s*=\s*"
                rf"{re.escape(parameter)}\b"
            )
            failures.extend((routine_name, parameter) for _ in re.finditer(pattern, sql, re.I))
    assert failures == []


def test_authority_rows_are_proven_before_identity_or_use():
    cases = {
        "sp_read_open_intelligence_execution_approval_v1": "SET v_expected_identity",
        "sp_consume_open_intelligence_execution_v1": "SET v_approval =",
        "sp_record_open_intelligence_execution_result_v1": "SET v_consumption =",
    }
    for routine_name, use_marker in cases.items():
        sql = (ROUTINES / f"{routine_name}.sql").read_text(encoding="utf-8")
        assert sql.index("execution_approval_unavailable") < sql.index(use_marker)


def test_consume_derives_expiry_timestamp_after_lock_and_reload():
    sql = (ROUTINES / "sp_consume_open_intelligence_execution_v1.sql").read_text(encoding="utf-8")
    assert sql.index("ASSERT @@row_count = 1") < sql.index("SET v_approval =")
    assert sql.index("SET v_approval =") < sql.index("SET v_consumed_at = CURRENT_TIMESTAMP()")
    assert sql.index("SET v_consumed_at = CURRENT_TIMESTAMP()") < sql.index(
        "ASSERT v_consumed_at < v_approval.expires_at"
    )


def test_approval_and_bootstrap_validation_cover_full_authority_shape():
    approval = (ROUTINES / "sp_approve_open_intelligence_execution_v1.sql").read_text(
        encoding="utf-8"
    )
    for marker in (
        "$.contract_sha256",
        "$.project",
        "$.datasets",
        "$.location",
        "$.job_resource",
        "$.service_identity",
        "$.source_sha",
        "$.image_uri",
        "$.build_resource",
        "$.command",
        "$.arguments",
        "$.environment",
        "$.secrets",
        "$.max_retries",
        "$.timeout_seconds",
        "$.input_artifacts",
        "$.limits",
    ):
        assert marker in approval

    bootstrap = (ROUTINES / "sp_import_open_intelligence_execution_bootstrap_v1.sql").read_text(
        encoding="utf-8"
    )
    for marker in (
        "$.manifest_version",
        "$.contract_sha256",
        "$.project",
        "$.location",
        "$.service_identity",
        "manifest_created_at <= signature_created_at",
        "signature_sha256",
        "v_execution_name",
        "v_migration_result_reference",
        "v_migration_result_digest",
        "v_completed_at < v_expires_at",
    ):
        assert marker in bootstrap


def test_sql_ids_hash_the_same_sorted_canonical_preimages_as_python():
    approval_key_order = (
        '{"approval_contract_version":"open_intelligence_execution_approval_v1",'
        '"approved_at":"%s","approved_by":"%s","manifest_sha256":"%s"}'
    )
    consumption_key_order = '{"approval_id":"%s","consumed_at":"%s","execution_name":"%s"}'
    result_key_order = (
        '{"completed_at":"%s","consumption_id":"%s","result_digest":"%s",'
        '"result_reference":"%s","status":"%s"}'
    )
    approval = (ROUTINES / "sp_approve_open_intelligence_execution_v1.sql").read_text()
    consumption = (ROUTINES / "sp_consume_open_intelligence_execution_v1.sql").read_text()
    result = (ROUTINES / "sp_record_open_intelligence_execution_result_v1.sql").read_text()
    bootstrap = (ROUTINES / "sp_import_open_intelligence_execution_bootstrap_v1.sql").read_text()

    assert approval_key_order in approval
    assert approval_key_order in bootstrap
    assert consumption_key_order in consumption
    assert consumption_key_order in bootstrap
    assert result_key_order in result
    assert '"completed_at":"%s","consumption_id":"%s"' in bootstrap


def test_routine_roles_and_source_digests_match_exact_files():
    plan = migration.build_plan()
    assert tuple(item.authorized_role for item in plan.routines) == (
        "roles/bigquery.routineDataEditor",
        "roles/bigquery.routineDataViewer",
        "roles/bigquery.routineDataViewer",
        "roles/bigquery.routineDataEditor",
        "roles/bigquery.routineDataEditor",
        "roles/bigquery.routineDataViewer",
        "roles/bigquery.routineDataEditor",
        "roles/bigquery.routineDataEditor",
        "roles/bigquery.routineDataEditor",
    )
    for item in (*plan.tables, *plan.routines):
        assert item.sha256 == hashlib.sha256(item.path.read_bytes()).hexdigest()


def test_iam_plan_has_exact_categories_without_direct_ledger_access():
    iam = migration.build_plan().iam_plan
    assert len(iam.proposed_job_user_grants) == 8
    assert len(iam.required_existing_job_user_bindings) == 1
    assert len(iam.routine_authorizations) == 9
    assert len(iam.principal_routine_bindings) == 36
    assert len(iam.run_viewer_bindings) == 9
    assert len(iam.cloudbuild_viewer_bindings) == 8
    assert iam.required_existing_dataset_writer.resource == (
        "projects/ogilvy-trends-v2/datasets/trends_v2_staging"
    )
    rendered = json.dumps(iam.to_dict(), sort_keys=True)
    assert rendered.count("roles/bigquery.dataEditor") == 1
    assert "roles/bigquery.dataOwner" not in rendered
    assert "roles/bigquery.dataViewer" in rendered

    result_chain = (
        "projects/ogilvy-trends-v2/datasets/trends_v2_staging_approvals/routines/"
        "sp_read_open_intelligence_execution_result_chain_v1"
    )
    bindings = [
        binding for binding in iam.principal_routine_bindings if binding.resource == result_chain
    ]
    assert bindings == [
        migration.IamBinding(
            principal=(
                "serviceAccount:trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"
            ),
            resource=result_chain,
            role="roles/bigquery.dataViewer",
            purpose="r3_apply routine metadata and invocation",
        ),
        migration.IamBinding(
            principal=(
                "serviceAccount:trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com"
            ),
            resource=result_chain,
            role="roles/bigquery.dataViewer",
            purpose="r3_proof_issue routine metadata and invocation",
        ),
        migration.IamBinding(
            principal=(
                "serviceAccount:trends-engine-oi-r3-release@ogilvy-trends-v2.iam."
                "gserviceaccount.com"
            ),
            resource=result_chain,
            role="roles/bigquery.dataViewer",
            purpose="r3_release routine metadata and invocation",
        ),
    ]


def test_iam_plan_denies_runtime_dataset_writers_production_and_direct_ledger_roles():
    plan = migration.build_plan()
    iam = plan.iam_plan
    bindings = (
        *iam.proposed_job_user_grants,
        *iam.required_existing_job_user_bindings,
        *iam.principal_routine_bindings,
        *iam.run_viewer_bindings,
        *iam.cloudbuild_viewer_bindings,
        *iam.temporary_bootstrap_bindings,
        iam.required_existing_dataset_writer,
    )
    protected_identities = {
        f"serviceAccount:{identity}" for identity, _job in migration.OPERATION_TARGETS.values()
    }
    assert len(protected_identities) == 8
    assert all("production" not in binding.resource.lower() for binding in bindings)
    assert all("production" not in binding.principal.lower() for binding in bindings)
    assert all(
        binding.role
        not in {
            "roles/bigquery.dataOwner",
            "roles/bigquery.admin",
        }
        for binding in bindings
    )
    direct_data_roles = {
        binding
        for binding in bindings
        if binding.role in {"roles/bigquery.dataEditor", "roles/bigquery.dataViewer"}
        and "/routines/" not in binding.resource
    }
    assert direct_data_roles == {iam.required_existing_dataset_writer}
    assert iam.required_existing_dataset_writer.resource == (
        "projects/ogilvy-trends-v2/datasets/trends_v2_staging"
    )
    assert all(
        migration.DATASET not in binding.resource
        for binding in bindings
        if binding.role.startswith("roles/bigquery.data") and "/routines/" not in binding.resource
    )


def test_operation_jobs_identities_and_viewer_bindings_are_one_to_one():
    iam = migration.build_plan().iam_plan
    expected = {
        (
            f"serviceAccount:{identity}",
            f"projects/{migration.PROJECT}/locations/us-central1/jobs/{job}",
        )
        for identity, job in migration.OPERATION_TARGETS.values()
    }
    assert {
        (binding.principal, binding.resource) for binding in iam.run_viewer_bindings
    } == expected
    assert {binding.principal for binding in iam.cloudbuild_viewer_bindings} == {
        principal for principal, _resource in expected
    }


def test_render_plan_is_canonical_and_records_every_source_digest():
    plan = migration.build_plan()
    rendered = migration.render_plan(plan)
    assert rendered.endswith("\n")
    payload = json.loads(rendered)
    assert payload["proof_kind"] == "code_only_structural_proof"
    assert tuple(item["name"] for item in payload["tables"]) == TABLES
    assert tuple(item["name"] for item in payload["routines"]) == ROUTINE_NAMES
    assert {item["sha256"] for item in payload["tables"] + payload["routines"]} == {
        item.sha256 for item in (*plan.tables, *plan.routines)
    }


def test_public_and_private_signatures_are_exact():
    assert str(inspect.signature(migration.build_plan)) == "() -> 'ApprovalStoreMigrationPlan'"
    assert str(inspect.signature(migration.render_plan)) == (
        "(plan: 'ApprovalStoreMigrationPlan') -> 'str'"
    )
    assert str(inspect.signature(migration.main)) == (
        "(argv: 'Sequence[str] | None' = None) -> 'int'"
    )
    assert tuple(inspect.signature(migration._verify_bootstrap_authority).parameters) == (
        "manifest_bytes",
        "signature_bytes",
        "public_key_pem",
        "object_generations",
        "build_payload",
    )
    assert tuple(inspect.signature(migration._apply_plan).parameters) == ("plan",)


def test_apply_refuses_before_credentials_without_approved_bootstrap_hash(monkeypatch):
    monkeypatch.setattr(migration, "APPROVED_BOOTSTRAP_CONTRACT_SHA256", None)
    monkeypatch.setattr(
        migration,
        "_load_credentials",
        lambda: pytest.fail("credentials crossed dark gate"),
    )
    assert migration.main(["apply"]) == 1
    assert json.loads(migration._LAST_ERROR) == {"error": "execution_approval_bootstrap_unapproved"}


def test_cli_grammar_refusal_is_one_error_line():
    process = subprocess.run(
        [sys.executable, str(migration.__file__), "apply", "extra"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert process.stdout == b""
    assert process.stderr == b'{"error":"execution_approval_manifest_invalid"}\n'


def test_bootstrap_signature_and_build_authority_are_verified():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    manifest_bytes = _bootstrap_manifest()
    manifest = json.loads(manifest_bytes)
    private_key = ec.generate_private_key(ec.SECP256R1())
    signature = private_key.sign(
        hashlib.sha256(manifest_bytes).digest(),
        ec.ECDSA(utils.Prehashed(hashes.SHA256())),
    )
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    authority = migration._verify_bootstrap_authority(
        manifest_bytes,
        signature,
        public_key,
        {"manifest": 11, "signature": 12},
        {
            "build_resource": manifest["build_resource"],
            "status": "SUCCESS",
            "resolved_source_sha": manifest["source_sha"],
            "image_uri": manifest["image_uri"],
        },
    )
    assert authority.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
    assert authority.object_generations == {"manifest": 11, "signature": 12}


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("signature", "execution_approval_manifest_mismatch"),
        ("generation", "execution_approval_bootstrap_unapproved"),
        ("expired", "execution_approval_expired"),
        ("build", "execution_approval_artifact_mismatch"),
    ],
)
def test_bootstrap_authority_mutations_refuse(mutation: str, error: str):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    manifest_bytes = _bootstrap_manifest(
        expires_delta=timedelta(seconds=-1) if mutation == "expired" else timedelta(hours=1)
    )
    manifest = json.loads(manifest_bytes)
    private_key = ec.generate_private_key(ec.SECP256R1())
    signature = private_key.sign(
        hashlib.sha256(manifest_bytes).digest(),
        ec.ECDSA(utils.Prehashed(hashes.SHA256())),
    )
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if mutation == "signature":
        signature = signature[:-1] + bytes([signature[-1] ^ 1])
    generations = {"manifest": 0 if mutation == "generation" else 1, "signature": 2}
    build = {
        "build_resource": manifest["build_resource"],
        "status": "SUCCESS",
        "resolved_source_sha": "f" * 40 if mutation == "build" else manifest["source_sha"],
        "image_uri": manifest["image_uri"],
    }
    with pytest.raises(migration.MigrationRefusal, match=error):
        migration._verify_bootstrap_authority(
            manifest_bytes, signature, public_key, generations, build
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "nondict_artifact",
        "nonhex_artifact_digest",
        "nonhex_source_sha",
        "nonhex_image_digest",
        "wrong_build_location",
        "nonnfc_build_resource",
    ],
)
def test_bootstrap_manifest_rejects_malformed_authority_values(mutation: str):
    manifest = json.loads(_bootstrap_manifest())
    if mutation == "nondict_artifact":
        manifest["input_artifacts"].append("ignored")
    elif mutation == "nonhex_artifact_digest":
        manifest["input_artifacts"][0]["sha256"] = "z" * 64
    elif mutation == "nonhex_source_sha":
        manifest["source_sha"] = "z" * 40
    elif mutation == "nonhex_image_digest":
        manifest["image_uri"] = manifest["image_uri"][:-64] + "z" * 64
    elif mutation == "wrong_build_location":
        manifest["build_resource"] = manifest["build_resource"].replace(
            "/locations/us-central1/",
            "/locations/europe-west1/",
        )
    else:
        manifest["build_resource"] += "-cafe\u0301"

    with pytest.raises(migration.MigrationRefusal):
        migration._validate_bootstrap_manifest(deepcopy(manifest))


@pytest.mark.parametrize(
    "expires_at",
    [
        "2026-08-31T12:00:00Z",
        "2026-08-31T12:00:00.123Z",
        "2026-08-31T12:00:00.1234567Z",
        "2026-08-31T12:00:00.123456+00:00",
    ],
)
def test_bootstrap_manifest_expiry_requires_exact_six_digit_utc(expires_at):
    manifest = json.loads(_bootstrap_manifest())
    manifest["expires_at"] = expires_at
    with pytest.raises(
        migration.MigrationRefusal,
        match="execution_approval_manifest_invalid",
    ):
        migration._validate_bootstrap_manifest(manifest)


def test_source_consumer_sql_binds_recovery_v2_digest_and_uncaptured_failure():
    sql = (ROUTINES / "sp_consume_open_intelligence_source_snapshot_v1.sql").read_text()
    assert "open_intelligence_source_capture_recovery_v1" in sql
    assert "open_intelligence_source_capture_recovery_v2" in sql
    assert (
        "contract_version,failed_creation_job_digest,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id"
        in sql
    )
    assert (
        "REGEXP_CONTAINS(JSON_VALUE(v_recovery, '$.failed_creation_job_digest'), r'^[0-9a-f]{64}$')"
        in sql
    )
    eligibility = sql.split("SET v_initial_result_count = (", 1)[1]
    assert "r.status = 'failed'" in eligibility
    assert "JSON_VALUE(r.canonical_result_json, '$.query_count') = '0'" in eligibility
    for field in (
        "captured_at",
        "snapshot_digest",
        "capture_receipt_digest",
        "artifact_attempt",
        "stored_artifact",
    ):
        assert (
            f"JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(r.canonical_result_json), '$.{field}')) = 'null'"
            in eligibility
        )


def test_approval_sql_enforces_exact_fields_artifacts_and_zero_limit_operations():
    sql = (ROUTINES / "sp_approve_open_intelligence_execution_v1.sql").read_text()
    assert "JSON_KEYS(v_manifest_json" in sql
    assert "ARRAY_LENGTH(v_top_level_keys) = 20" in sql
    assert (
        "ARRAY_TO_STRING(v_artifact_names, ',') = ARRAY_TO_STRING(v_expected_artifact_names, ',')"
        in sql
    )
    assert "!= 'name,sha256'" in sql
    for operation in (
        "migration_apply",
        "collection_exposure_issue",
        "r3_apply",
        "r3_proof_issue",
        "r3_release",
        "brain_read",
        "wave1_pilot",
    ):
        assert f"WHEN '{operation}'" in sql
    assert "WHEN 'r3_proof_issue'" in sql
    assert "max_rows_written') AS INT64) = 0" in sql
    assert "WHEN 'brain_read'" in sql


class _FakeResponse:
    def __init__(self, *, payload=None, content=b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _BootstrapReadSession:
    def __init__(
        self,
        manifest_bytes: bytes,
        *,
        manifest_generation=11,
        source_kind="resolvedRepoSource",
    ):
        self.manifest_bytes = manifest_bytes
        self.manifest_generation = manifest_generation
        self.signature = b"signature"
        manifest = json.loads(manifest_bytes)
        self.annotations = {
            "42.ogilvy/bootstrap-manifest-sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "42.ogilvy/bootstrap-manifest-generation": "11",
            "42.ogilvy/bootstrap-signature-generation": "12",
            "42.ogilvy/execution-approval-sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "42.ogilvy/source-sha": manifest["source_sha"],
        }
        self.manifest = manifest
        self.source_kind = source_kind

    def get(self, url, *, params=None):
        if url.startswith("https://run.googleapis.com/v2/projects/"):
            if "/executions/" in url:
                return _FakeResponse(
                    payload={
                        "name": (
                            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
                            "trends-engine-oi-approval-bootstrap-staging/executions/execution-1"
                        ),
                        "job": getattr(self, "execution_job", migration.BOOTSTRAP_JOB),
                        "annotations": dict(self.annotations),
                    }
                )
            return _FakeResponse(
                payload={
                    "name": migration.BOOTSTRAP_JOB,
                    "template": {"annotations": dict(self.annotations)},
                }
            )
        if url.startswith("https://storage.googleapis.com/storage/v1/"):
            is_manifest = "bootstrap%2Fmanifests%2F" in url
            generation = self.manifest_generation if is_manifest else 12
            if params == {"generation": generation, "alt": "media"}:
                return _FakeResponse(content=self.manifest_bytes if is_manifest else self.signature)
            return _FakeResponse(
                payload={
                    "generation": str(generation),
                    "timeCreated": "2026-08-31T12:00:00.000000Z",
                }
            )
        if url.startswith("https://cloudkms.googleapis.com/v1/"):
            return _FakeResponse(
                payload={
                    "name": migration.KMS_KEY_VERSION,
                    "algorithm": "EC_SIGN_P256_SHA256",
                    "pem": "PUBLIC KEY",
                }
            )
        if url.startswith("https://cloudbuild.googleapis.com/v1/"):
            if self.source_kind == "connectedRepository":
                image_repository, image_digest = self.manifest["image_uri"].split("@", 1)
                return _FakeResponse(
                    payload={
                        "name": self.manifest["build_resource"].replace(
                            "projects/ogilvy-trends-v2/", "projects/590353929363/"
                        ),
                        "id": self.manifest["build_resource"].rsplit("/", 1)[1],
                        "projectId": migration.PROJECT,
                        "status": "SUCCESS",
                        "createTime": "2026-09-02T06:12:49.590930825Z",
                        "source": {
                            "connectedRepository": {
                                "repository": getattr(
                                    self,
                                    "repository",
                                    "projects/ogilvy-trends-v2/locations/us-central1/connections/"
                                    "tev2-gh/repositories/jhbanalytics-pixel-trends-engine-v2",
                                ),
                                "revision": self.manifest["source_sha"],
                            }
                        },
                        "sourceProvenance": {},
                        "results": {
                            "images": [
                                {
                                    "name": (
                                        f"{getattr(self, 'image_repository', image_repository)}"
                                        f":{self.manifest['source_sha']}"
                                    ),
                                    "digest": image_digest,
                                    "pushTiming": {"startTime": "2026-09-02T06:15:00Z"},
                                }
                            ],
                            "buildStepImages": [""],
                        },
                        "finishTime": "2026-09-02T06:15:15.585863Z",
                    }
                )
            source = {
                (
                    "commitSha" if self.source_kind == "resolvedRepoSource" else "revision"
                ): self.manifest["source_sha"]
            }
            if self.source_kind == "resolvedRepoSource":
                source.update({"projectId": migration.PROJECT, "repoName": "trends-engine"})
            elif self.source_kind == "resolvedGitSource":
                source["url"] = "https://example.invalid/trends-engine.git"
            else:
                source["repository"] = (
                    "projects/ogilvy-trends-v2/locations/us-central1/connections/github/"
                    "repositories/trends-engine"
                )
            return _FakeResponse(
                payload={
                    "name": self.manifest["build_resource"],
                    "id": self.manifest["build_resource"].rsplit("/", 1)[1],
                    "projectId": migration.PROJECT,
                    "status": "SUCCESS",
                    "sourceProvenance": {self.source_kind: source, "fileHashes": {}},
                    "results": {
                        "images": [
                            {
                                "name": self.manifest["image_uri"].split("@", 1)[0],
                                "digest": self.manifest["image_uri"].split("@", 1)[1],
                            }
                        ]
                    },
                }
            )
        raise AssertionError(url)


def test_load_bootstrap_inputs_reads_exact_generation_pinned_authority(monkeypatch):
    manifest_bytes = _bootstrap_manifest()
    selector = hashlib.sha256(manifest_bytes).hexdigest()
    session = _BootstrapReadSession(manifest_bytes)
    monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_SHA256", selector)
    monkeypatch.setenv("CLOUD_RUN_JOB", "trends-engine-oi-approval-bootstrap-staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution-1")
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: session)

    inputs = migration._load_bootstrap_inputs(object())

    assert inputs.manifest_bytes == manifest_bytes
    assert inputs.signature_bytes == b"signature"
    assert inputs.public_key_pem == b"PUBLIC KEY"
    assert inputs.object_generations == {"manifest": 11, "signature": 12}
    assert inputs.build_payload == {
        "build_resource": json.loads(manifest_bytes)["build_resource"],
        "status": "SUCCESS",
        "resolved_source_sha": "a" * 40,
        "image_uri": json.loads(manifest_bytes)["image_uri"],
    }


@pytest.mark.parametrize(
    ("execution_job", "accepted"),
    [
        ("trends-engine-oi-approval-bootstrap-staging", True),
        ("trends-engine-oi-approval-bootstrap-staging-other", False),
        ("projects/ogilvy-trends-v2/locations/us-central1/jobs/other", False),
    ],
)
def test_load_bootstrap_inputs_binds_the_short_execution_job_id(
    monkeypatch, execution_job, accepted
):
    manifest_bytes = _bootstrap_manifest()
    selector = hashlib.sha256(manifest_bytes).hexdigest()
    session = _BootstrapReadSession(manifest_bytes)
    session.execution_job = execution_job
    monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_SHA256", selector)
    monkeypatch.setenv("CLOUD_RUN_JOB", "trends-engine-oi-approval-bootstrap-staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution-1")
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: session)
    if accepted:
        assert migration._load_bootstrap_inputs(object()).manifest_bytes == manifest_bytes
    else:
        with pytest.raises(
            migration.MigrationRefusal, match="execution_approval_bootstrap_unapproved"
        ):
            migration._load_bootstrap_inputs(object())


def test_iam_adapter_uses_each_service_own_policy_method():
    # Live probe 2 September 2026: Cloud KMS answers getIamPolicy on GET (POST is 404),
    # and conditional bindings only appear when policy version 3 is requested.
    adapter = migration._GoogleIamAdapter(object())
    kms_version = (
        "projects/ogilvy-trends-v2/locations/global/keyRings/open-intelligence-staging/"
        "cryptoKeys/execution-bootstrap/cryptoKeyVersions/1"
    )
    get_url, set_url, method = adapter._policy_endpoint(kms_version)
    assert method == "get"
    assert get_url == (
        "https://cloudkms.googleapis.com/v1/projects/ogilvy-trends-v2/locations/global/"
        "keyRings/open-intelligence-staging/cryptoKeys/execution-bootstrap:getIamPolicy"
        "?options.requestedPolicyVersion=3"
    )
    assert set_url.endswith("cryptoKeys/execution-bootstrap:setIamPolicy")

    job = "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-brain-staging"
    get_url, _set_url, method = adapter._policy_endpoint(job)
    assert method == "get"
    assert get_url == f"https://run.googleapis.com/v2/{job}:getIamPolicy"

    project_get, _project_set, project_method = adapter._policy_endpoint(
        "projects/ogilvy-trends-v2"
    )
    assert project_method == "post"
    assert project_get.endswith("projects/ogilvy-trends-v2:getIamPolicy")

    class _Session:
        def __init__(self):
            self.calls = []

        def get(self, url):
            self.calls.append(("get", url, None))
            return _FakeResponse(payload={"bindings": [], "version": 3})

        def post(self, url, json):
            self.calls.append(("post", url, json))
            return _FakeResponse(payload={"bindings": [], "version": 3})

    session = _Session()
    adapter._session = session
    adapter._get_policy(kms_version)
    adapter._get_policy("projects/ogilvy-trends-v2")
    assert session.calls[0][0] == "get"
    assert session.calls[1] == (
        "post",
        "https://cloudresourcemanager.googleapis.com/v1/projects/ogilvy-trends-v2:getIamPolicy",
        {"options": {"requestedPolicyVersion": 3}},
    )


def test_routine_authorizations_are_patched_in_one_dataset_update(monkeypatch):
    # BigQuery limits dataset metadata updates to a handful per ten seconds; the live
    # bootstrap hit 403 on the sixth single-entry patch (2 September 2026).
    plan = migration.build_plan()
    adapter = migration._GoogleIamAdapter(object())
    access = [{"role": "OWNER", "specialGroup": "projectOwners"}]

    class _Session:
        def __init__(self):
            self.patches = []

        def get(self, url, **_kwargs):
            return _FakeResponse(payload={"access": list(access)})

        def patch(self, url, *, params, json):
            self.patches.append((url, params, json))
            access[:] = json["access"]
            return _FakeResponse(payload={"access": list(access)})

    session = _Session()
    adapter._session = session
    missing = [
        item for item in plan.iam_plan.routine_authorizations if not adapter.has_authorization(item)
    ]
    assert len(missing) == 9
    adapter.add_authorizations(missing)
    assert len(session.patches) == 1
    assert session.patches[0][1] == {"fields": "access"}
    assert len(session.patches[0][2]["access"]) == 10
    assert all(adapter.has_authorization(item) for item in missing)

    adapter.remove_authorizations(missing[:3])
    assert len(session.patches) == 2
    assert len(session.patches[1][2]["access"]) == 7
    assert not any(adapter.has_authorization(item) for item in missing[:3])
    assert all(adapter.has_authorization(item) for item in missing[3:])

    adapter.add_authorizations([])
    adapter.remove_authorizations([])
    assert len(session.patches) == 2


def test_load_bootstrap_inputs_refuses_positive_generation_drift(monkeypatch):
    manifest_bytes = _bootstrap_manifest()
    selector = hashlib.sha256(manifest_bytes).hexdigest()
    session = _BootstrapReadSession(manifest_bytes, manifest_generation=13)
    monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_SHA256", selector)
    monkeypatch.setenv("CLOUD_RUN_JOB", "trends-engine-oi-approval-bootstrap-staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution-1")
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: session)

    with pytest.raises(
        migration.MigrationRefusal,
        match="execution_approval_bootstrap_unapproved",
    ):
        migration._load_bootstrap_inputs(object())


@pytest.mark.parametrize(
    "source_kind",
    [
        "resolvedRepoSource",
        "resolvedGitSource",
        "resolvedConnectedRepository",
        "connectedRepository",
    ],
)
def test_load_bootstrap_inputs_accepts_each_exact_cloud_build_source_shape(
    monkeypatch,
    source_kind,
):
    manifest_bytes = _bootstrap_manifest()
    selector = hashlib.sha256(manifest_bytes).hexdigest()
    session = _BootstrapReadSession(manifest_bytes, source_kind=source_kind)
    monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_SHA256", selector)
    monkeypatch.setenv("CLOUD_RUN_JOB", "trends-engine-oi-approval-bootstrap-staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution-1")
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: session)
    inputs = migration._load_bootstrap_inputs(object())
    assert inputs.manifest_bytes == manifest_bytes
    assert inputs.build_payload["resolved_source_sha"] == json.loads(manifest_bytes)["source_sha"]
    assert inputs.build_payload["image_uri"] == json.loads(manifest_bytes)["image_uri"]


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        (
            "repository",
            "projects/ogilvy-trends-v2/locations/us-central1/connections/tev2-gh/repositories/other",
        ),
        ("image_repository", "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/other"),
    ],
)
def test_load_bootstrap_inputs_refuses_a_connected_repository_outside_the_approved_one(
    monkeypatch, attribute, value
):
    manifest_bytes = _bootstrap_manifest()
    selector = hashlib.sha256(manifest_bytes).hexdigest()
    session = _BootstrapReadSession(manifest_bytes, source_kind="connectedRepository")
    setattr(session, attribute, value)
    monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_SHA256", selector)
    monkeypatch.setenv("CLOUD_RUN_JOB", "trends-engine-oi-approval-bootstrap-staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution-1")
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: session)
    with pytest.raises(migration.MigrationRefusal, match="execution_approval_artifact_mismatch"):
        migration._load_bootstrap_inputs(object())


@pytest.mark.parametrize(
    ("source", "expected_microsecond"),
    [
        ("2026-08-31T12:00:00Z", 0),
        ("2026-08-31T12:00:00.123Z", 123000),
        ("2026-08-31T12:00:00.123456Z", 123456),
        ("2026-08-31T12:00:00.123456000Z", 123456),
    ],
)
def test_server_owned_google_timestamps_normalize_to_six_digits(
    source,
    expected_microsecond,
):
    parsed = migration._parse_server_time(source)
    assert parsed.microsecond == expected_microsecond
    assert migration._format_server_time(parsed).endswith(f".{expected_microsecond:06d}Z")


class _MissingDataset(Exception):
    code = 404


class _PreflightBigQueryClient:
    def __init__(self, dataset=None):
        self.dataset = dataset

    def get_dataset(self, _dataset_id):
        if self.dataset is None:
            raise _MissingDataset()
        return self.dataset


def test_preflight_requires_the_isolated_store_to_be_absent(monkeypatch):
    authority = _fixture_authority()
    missing = _PreflightBigQueryClient()
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: missing)
    migration._read_preflight(migration.build_plan(), object(), authority)

    existing = _PreflightBigQueryClient(dataset=object())
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: existing)
    with pytest.raises(
        migration.MigrationRefusal,
        match="execution_approval_schema_mismatch",
    ):
        migration._read_preflight(migration.build_plan(), object(), authority)


class _LockBlob:
    def __init__(self, *, occupied=False):
        self.occupied = occupied
        self.generation = None
        self.time_created = None
        self.content = None

    def upload_from_string(self, content, *, if_generation_match):
        assert if_generation_match == 0
        if self.occupied:
            raise RuntimeError("precondition failed")
        self.content = bytes(content)
        self.generation = 1
        self.time_created = datetime(2026, 8, 31, 12, tzinfo=UTC)
        self.occupied = True

    def reload(self):
        return None

    def download_as_bytes(self, *, if_generation_match):
        assert if_generation_match == self.generation
        return self.content


class _LockBucket:
    def __init__(self, blob):
        self._blob = blob

    def blob(self, name):
        assert name == "bootstrap/locks/open-intelligence-execution-approval-v1.lock"
        return self._blob


class _LockStorageClient:
    def __init__(self, blob):
        self._bucket = _LockBucket(blob)

    def bucket(self, name):
        assert name == "ogilvy-trends-v2-execution-approvals-staging"
        return self._bucket


def test_bootstrap_lock_is_generation_zero_create_only_and_read_back(monkeypatch):
    authority = _fixture_authority()
    blob = _LockBlob()
    monkeypatch.setattr(
        migration,
        "_storage_client",
        lambda _credentials: _LockStorageClient(blob),
    )
    migration._create_bootstrap_lock(object(), authority)
    assert blob.content == authority.manifest_sha256.encode()
    assert blob.generation == 1

    with pytest.raises(
        migration.MigrationRefusal,
        match="execution_approval_bootstrap_used",
    ):
        migration._create_bootstrap_lock(object(), authority)


class _QueryJob:
    def __init__(self, rows=()):
        self._rows = tuple(rows)
        self.job_id = "job-1"

    def result(self):
        return self._rows


class _DdlBigQueryClient:
    def __init__(self):
        self.created_dataset = None
        self.queries = []

    def create_dataset(self, dataset):
        self.created_dataset = dataset
        return dataset

    def query(self, sql):
        self.queries.append(sql)
        return _QueryJob()


def test_apply_ddl_creates_only_the_store_sources_and_singleton_seed(monkeypatch):
    client = _DdlBigQueryClient()
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)
    plan = migration.build_plan()

    migration._apply_ddl(plan, object(), _fixture_authority())

    assert client.created_dataset.dataset_id == migration.DATASET
    assert client.created_dataset.project == migration.PROJECT
    assert client.created_dataset.location == migration.LOCATION
    assert client.created_dataset.default_table_expiration_ms is None
    assert client.created_dataset.max_time_travel_hours == 168
    assert len(client.queries) == 14
    assert sum("CREATE TABLE IF NOT EXISTS" in sql for sql in client.queries) == 4
    assert sum("CREATE OR REPLACE PROCEDURE" in sql for sql in client.queries) == 9
    assert "open_intelligence_execution_approval_lock_v1" in client.queries[-1]
    assert "WHERE NOT EXISTS" in client.queries[-1]
    # BigQuery refuses a WHERE clause on a SELECT without FROM (live bootstrap run
    # h8j22, 2 September 2026: "Query without FROM clause cannot have a WHERE clause").
    assert re.search(
        r"SELECT\s[\s\S]*?\sFROM \(SELECT 1\)\s+WHERE NOT EXISTS", client.queries[-1]
    ), client.queries[-1]
    assert all("trends_v2_staging_approvals" in sql for sql in client.queries)


class _SchemaField:
    def __init__(self, name, field_type, mode, description):
        self.name = name
        self.field_type = field_type
        self.mode = mode
        self.description = description


class _ReadbackTable:
    def __init__(self, name, *, mutate_description=False, mutate_table_description=False):
        fields = []
        for index, (field_name, field_type, mode, description) in enumerate(EXPECTED_FIELDS[name]):
            if mutate_description and index == 0:
                description = "drift"
            # The BigQuery API reports legacy type names on readback: INT64 comes back as
            # INTEGER (live approvals store readback, 2 September 2026).
            api_type = {"INT64": "INTEGER"}.get(field_type, field_type)
            fields.append(_SchemaField(field_name, api_type, mode, description))
        self.schema = fields
        self.table_type = "TABLE"
        self.expires = None
        expected = {
            TABLES[0]: "Immutable human approvals for one exact staging execution manifest.",
            TABLES[
                1
            ]: "Immutable one-use reservations binding an approval to one Cloud Run Execution.",
            TABLES[
                2
            ]: "Immutable terminal links from one consumed approval to one operation receipt.",
            TABLES[
                3
            ]: "Mutable singleton serialization and emergency-disable state for execution approvals.",
        }
        self.description = "drift" if mutate_table_description else expected[name]


class _ReadbackDataset:
    location = "US"
    default_table_expiration_ms = None
    default_partition_expiration_ms = None
    # The API reports this option as a decimal string on readback (live, 2 September 2026).
    max_time_travel_hours = "168"


class _ReadbackBigQueryClient:
    def __init__(
        self,
        plan,
        *,
        mutate_description=False,
        mutate_table_description=False,
        mutate_routine_parameter=False,
    ):
        self.plan = plan
        self.mutate_description = mutate_description
        self.mutate_table_description = mutate_table_description
        self.mutate_routine_parameter = mutate_routine_parameter

    def get_dataset(self, _dataset_id):
        return _ReadbackDataset()

    def get_table(self, table_id):
        name = table_id.rsplit(".", 1)[1]
        return _ReadbackTable(
            name,
            mutate_description=self.mutate_description,
            mutate_table_description=self.mutate_table_description,
        )

    def query(self, sql):
        if "INFORMATION_SCHEMA.PARAMETERS" in sql:
            rows = []
            for routine in self.plan.routines:
                for position, (name, data_type) in enumerate(routine.parameters, start=1):
                    rows.append(
                        {
                            "specific_name": routine.name,
                            "ordinal_position": position,
                            "parameter_name": (
                                "drift" if self.mutate_routine_parameter and not rows else name
                            ),
                            "data_type": data_type,
                            # INFORMATION_SCHEMA.PARAMETERS reports NULL parameter_mode for
                            # procedure inputs on live readback (2 September 2026).
                            "parameter_mode": None,
                        }
                    )
            return _QueryJob(rows)
        if "INFORMATION_SCHEMA.ROUTINES" in sql:
            # routine_definition is stored with {project}.{dataset} already rendered.
            return _QueryJob(
                {
                    "routine_name": item.name,
                    "routine_definition": migration._routine_body(item.sql)
                    .replace("{project}", migration.PROJECT)
                    .replace("{dataset}", migration.DATASET),
                }
                for item in self.plan.routines
            )
        return _QueryJob(
            [
                {
                    "lock_name": "open_intelligence_execution_approval_v1",
                    "approval_contract_version": "open_intelligence_execution_approval_v1",
                    "lock_version": 0,
                    "state": "ready",
                    "last_approval_id": None,
                    "created_at": datetime(2026, 8, 31, 12, tzinfo=UTC),
                    "updated_at": datetime(2026, 8, 31, 12, tzinfo=UTC),
                }
            ]
        )


def test_store_readback_proves_dataset_schema_routine_body_and_lock(monkeypatch):
    plan = migration.build_plan()
    client = _ReadbackBigQueryClient(plan)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)
    migration._readback_store(plan, object(), _fixture_authority())

    drifted = _ReadbackBigQueryClient(plan, mutate_description=True)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: drifted)
    with pytest.raises(
        migration.MigrationRefusal,
        match="execution_approval_schema_mismatch",
    ):
        migration._readback_store(plan, object(), _fixture_authority())

    for drifted in (
        _ReadbackBigQueryClient(plan, mutate_table_description=True),
        _ReadbackBigQueryClient(plan, mutate_routine_parameter=True),
    ):
        monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials, c=drifted: c)
        with pytest.raises(
            migration.MigrationRefusal,
            match="execution_approval_schema_mismatch",
        ):
            migration._readback_store(plan, object(), _fixture_authority())


class _FakeIamAdapter:
    def __init__(self, *, fail_after=None):
        self.bindings = set()
        self.authorizations = set()
        self.fail_after = fail_after
        self.add_count = 0
        self.direct_bootstrap_access = True

    def has_binding(self, binding):
        return binding in self.bindings

    def add_binding(self, binding):
        self.add_count += 1
        if self.fail_after is not None and self.add_count > self.fail_after:
            raise RuntimeError("iam failed")
        self.bindings.add(binding)

    def remove_binding(self, binding):
        self.bindings.discard(binding)

    def has_authorization(self, authorization):
        return authorization in self.authorizations

    def add_authorization(self, authorization):
        self.add_count += 1
        if self.fail_after is not None and self.add_count > self.fail_after:
            raise RuntimeError("iam failed")
        self.authorizations.add(authorization)

    def remove_authorization(self, authorization):
        self.authorizations.discard(authorization)

    def add_authorizations(self, authorizations):
        # The real adapter applies one dataset patch, so a batch lands whole or not at all.
        authorizations = list(authorizations)
        if self.fail_after is not None and self.add_count + len(authorizations) > self.fail_after:
            self.add_count += len(authorizations)
            raise RuntimeError("iam failed")
        for authorization in authorizations:
            self.add_authorization(authorization)

    def remove_authorizations(self, authorizations):
        for authorization in authorizations:
            self.remove_authorization(authorization)

    def remove_direct_bootstrap_access(self):
        self.direct_bootstrap_access = False

    def has_direct_bootstrap_access(self):
        return self.direct_bootstrap_access


def _iam_adapter_with_required_preexisting(plan, authority):
    adapter = _FakeIamAdapter()
    adapter.bindings.update(plan.iam_plan.required_existing_job_user_bindings)
    adapter.bindings.add(plan.iam_plan.required_existing_dataset_writer)
    adapter.bindings.update(
        migration._exact_bootstrap_binding(binding, authority)
        for binding in plan.iam_plan.temporary_bootstrap_bindings
    )
    return adapter


def test_iam_apply_returns_only_attempt_created_grants_and_rollback_preserves_existing(
    monkeypatch,
):
    plan = migration.build_plan()
    authority = _fixture_authority()
    adapter = _iam_adapter_with_required_preexisting(plan, authority)
    preexisting = next(iter(plan.iam_plan.proposed_job_user_grants))
    adapter.bindings.add(preexisting)
    monkeypatch.setattr(migration, "_iam_adapter", lambda _credentials: adapter)

    created = migration._apply_iam_plan(plan, object(), authority)

    assert preexisting not in created
    assert len(created) == (
        len(plan.iam_plan.proposed_job_user_grants)
        + len(plan.iam_plan.routine_authorizations)
        + len(plan.iam_plan.principal_routine_bindings)
        + len(plan.iam_plan.run_viewer_bindings)
        + len(plan.iam_plan.cloudbuild_viewer_bindings)
        - 1
    )
    migration._rollback_attempt_grants(plan, object(), authority, created)
    assert preexisting in adapter.bindings
    assert all(
        item not in adapter.bindings for item in created if isinstance(item, migration.IamBinding)
    )
    assert all(
        item not in adapter.authorizations
        for item in created
        if isinstance(item, migration.RoutineAuthorization)
    )


def test_iam_partial_failure_rolls_back_only_the_successful_prefix(monkeypatch):
    plan = migration.build_plan()
    authority = _fixture_authority()
    adapter = _iam_adapter_with_required_preexisting(plan, authority)
    adapter.fail_after = 2
    monkeypatch.setattr(migration, "_iam_adapter", lambda _credentials: adapter)

    with pytest.raises(RuntimeError, match="iam failed"):
        migration._apply_iam_plan(plan, object(), authority)

    assert adapter.bindings == {
        *plan.iam_plan.required_existing_job_user_bindings,
        plan.iam_plan.required_existing_dataset_writer,
        *(
            migration._exact_bootstrap_binding(binding, authority)
            for binding in plan.iam_plan.temporary_bootstrap_bindings
        ),
    }
    assert adapter.authorizations == set()


def test_remove_bootstrap_access_removes_temporary_routine_and_direct_grants(monkeypatch):
    plan = migration.build_plan()
    authority = _fixture_authority()
    adapter = _iam_adapter_with_required_preexisting(plan, authority)
    bootstrap_routine = next(
        binding
        for binding in plan.iam_plan.principal_routine_bindings
        if binding.principal.startswith("serviceAccount:trends-engine-oi-bootstrap@")
    )
    adapter.bindings.add(bootstrap_routine)
    monkeypatch.setattr(migration, "_iam_adapter", lambda _credentials: adapter)

    migration._remove_bootstrap_access(plan, object(), authority)

    assert all(
        migration._exact_bootstrap_binding(binding, authority) not in adapter.bindings
        for binding in plan.iam_plan.temporary_bootstrap_bindings
    )
    assert bootstrap_routine not in adapter.bindings
    assert adapter.has_direct_bootstrap_access() is False


def test_bootstrap_iam_conditions_are_exact_object_and_kms_version_scoped():
    authority = _fixture_authority()
    bindings = tuple(
        migration._exact_bootstrap_binding(binding, authority)
        for binding in migration.build_plan().iam_plan.temporary_bootstrap_bindings
    )
    storage_bindings = [binding for binding in bindings if binding.resource.startswith("gs://")]
    assert len(storage_bindings) == 3
    for binding in storage_bindings:
        condition = migration._GoogleIamAdapter._storage_condition(binding.resource)
        assert "resource.name ==" in condition["expression"]
        assert "startsWith" not in condition["expression"]
        assert "{manifest_sha256}" not in binding.resource

    kms_binding = next(binding for binding in bindings if "/cryptoKeyVersions/" in binding.resource)
    kms_condition = migration._GoogleIamAdapter._policy_condition(kms_binding)
    assert kms_condition is not None
    assert kms_binding.resource in kms_condition["expression"]


def test_authorized_procedure_dataset_entries_keep_the_exact_routine_role():
    for authorization in migration.build_plan().iam_plan.routine_authorizations:
        entry = migration._GoogleIamAdapter._authorization_entry(authorization)
        assert entry["role"] == authorization.role
        assert entry["routine"]["routineId"] == authorization.routine.rsplit("/", 1)[1]


def _test_identifier(prefix, payload):
    return (
        prefix
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


class _ImportBigQueryClient:
    def __init__(self, manifest_sha, execution_name):
        approved_at = "2026-08-31T12:00:00.000000Z"
        consumed_at = "2026-08-31T12:01:00.000000Z"
        completed_at = "2026-08-31T12:02:00.000000Z"
        self.approval_id = _test_identifier(
            "exa_",
            {
                "approval_contract_version": "open_intelligence_execution_approval_v1",
                "approved_at": approved_at,
                "approved_by": "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab",
                "manifest_sha256": manifest_sha,
            },
        )
        self.consumption_id = _test_identifier(
            "exc_",
            {
                "approval_id": self.approval_id,
                "consumed_at": consumed_at,
                "execution_name": execution_name,
            },
        )
        self.result_reference = execution_name + "#approval-store-readback"
        self.canonical_result_json = '{"proof":"ok"}'
        self.proof_digest = hashlib.sha256(self.canonical_result_json.encode()).hexdigest()
        self.result_id = _test_identifier(
            "exr_",
            {
                "completed_at": completed_at,
                "consumption_id": self.consumption_id,
                "result_digest": self.proof_digest,
                "result_reference": self.result_reference,
                "status": "succeeded",
            },
        )
        self.completed_at = datetime(2026, 8, 31, 12, 2, tzinfo=UTC)
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        if "USING (approval_id)" in sql:
            # results_v1 carries approval_id too, so BigQuery refuses this join as ambiguous.
            raise RuntimeError(
                "Column approval_id in USING clause is ambiguous on left side of join"
            )
        if sql.startswith("CALL"):
            return _QueryJob(
                [
                    {
                        "manifest_sha256": self.calls[0][1].query_parameters[1].value,
                        "approval_id": self.approval_id,
                        "consumption_id": self.consumption_id,
                        "result_id": self.result_id,
                        "migration_result_reference": self.result_reference,
                        "migration_result_digest": self.calls[0][1].query_parameters[-1].value,
                        "bootstrap_proof_digest": self.proof_digest,
                    }
                ]
            )
        return _QueryJob(
            [
                {
                    "approval_id": self.approval_id,
                    "consumption_id": self.consumption_id,
                    "result_id": self.result_id,
                    "result_reference": self.result_reference,
                    "canonical_result_json": self.canonical_result_json,
                    "result_digest": self.proof_digest,
                    "status": "succeeded",
                    "completed_at": self.completed_at,
                }
            ]
        )


def test_import_bootstrap_calls_exact_procedure_and_reproduces_all_ids(monkeypatch):
    manifest_bytes = _bootstrap_manifest()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    execution_name = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-approval-bootstrap-staging/executions/execution-1"
    )
    authority = migration.BootstrapAuthority(
        manifest=json.loads(manifest_bytes),
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha,
        object_generations={"manifest": 11, "signature": 12},
    )
    session = _BootstrapReadSession(manifest_bytes)
    lock = _LockBlob()
    lock.upload_from_string(manifest_sha.encode(), if_generation_match=0)
    lock.time_created = datetime(2026, 8, 31, 12, 1, tzinfo=UTC)
    client = _ImportBigQueryClient(manifest_sha, execution_name)
    monkeypatch.setenv("CLOUD_RUN_JOB", "trends-engine-oi-approval-bootstrap-staging")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "execution-1")
    monkeypatch.setattr(migration, "_authorized_session", lambda _credentials: session)
    monkeypatch.setattr(
        migration,
        "_storage_client",
        lambda _credentials: _LockStorageClient(lock),
    )
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)

    receipt = migration._import_bootstrap(migration.build_plan(), object(), authority)

    assert receipt.approval_id == client.approval_id
    assert receipt.consumption_id == client.consumption_id
    assert receipt.result_id == client.result_id
    assert receipt.bootstrap_proof_digest == client.proof_digest
    assert len(client.calls) == 2
    assert client.calls[0][0].startswith(
        "CALL `ogilvy-trends-v2.trends_v2_staging_approvals."
        "sp_import_open_intelligence_execution_bootstrap_v1`"
    )
    readback_sql = client.calls[1][0]
    assert "open_intelligence_execution_results_v1` r" in readback_sql
    assert "USING (consumption_id)" in readback_sql
    assert "ON a.approval_id = c.approval_id" in readback_sql
    assert f"WHERE r.result_id = '{client.result_id}'" in readback_sql


def test_apply_orders_lock_ddl_readback_iam_import_and_cleanup(monkeypatch):
    calls: list[str] = []
    manifest_bytes = _bootstrap_manifest()
    authority = migration.BootstrapAuthority(
        manifest=json.loads(manifest_bytes),
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        object_generations={"manifest": 1, "signature": 2},
    )
    monkeypatch.setattr(migration, "_load_credentials", lambda: calls.append("credentials"))
    monkeypatch.setattr(
        migration,
        "_load_bootstrap_inputs",
        lambda _credentials: calls.append("inputs") or _fixture_inputs(),
    )
    monkeypatch.setattr(
        migration,
        "_verify_bootstrap_authority",
        lambda *args: calls.append("authority") or authority,
    )
    monkeypatch.setattr(migration, "_read_preflight", lambda *_: calls.append("preflight"))
    monkeypatch.setattr(migration, "_create_bootstrap_lock", lambda *_: calls.append("lock"))
    monkeypatch.setattr(migration, "_apply_ddl", lambda *_: calls.append("ddl"))
    monkeypatch.setattr(migration, "_readback_store", lambda *_: calls.append("readback"))
    monkeypatch.setattr(migration, "_apply_iam_plan", lambda *_: calls.append("iam"))
    monkeypatch.setattr(
        migration,
        "_import_bootstrap",
        lambda *_: calls.append("import") or _fixture_receipt(),
    )
    monkeypatch.setattr(migration, "_remove_bootstrap_access", lambda *_: calls.append("cleanup"))

    migration._apply_plan(migration.build_plan())

    assert calls == [
        "credentials",
        "inputs",
        "authority",
        "preflight",
        "lock",
        "ddl",
        "readback",
        "iam",
        "import",
        "cleanup",
    ]


def test_iam_failure_removes_bootstrap_access_and_never_imports(monkeypatch):
    calls: list[str] = []
    inputs = _fixture_inputs()
    authority = _fixture_authority()
    monkeypatch.setattr(migration, "_load_credentials", lambda: object())
    monkeypatch.setattr(migration, "_load_bootstrap_inputs", lambda _: inputs)
    monkeypatch.setattr(migration, "_verify_bootstrap_authority", lambda *args: authority)
    monkeypatch.setattr(migration, "_read_preflight", lambda *_: None)
    monkeypatch.setattr(migration, "_create_bootstrap_lock", lambda *_: None)
    monkeypatch.setattr(migration, "_apply_ddl", lambda *_: None)
    monkeypatch.setattr(migration, "_readback_store", lambda *_: None)
    monkeypatch.setattr(
        migration,
        "_apply_iam_plan",
        lambda *_: (_ for _ in ()).throw(RuntimeError("iam failed")),
    )
    monkeypatch.setattr(migration, "_import_bootstrap", lambda *_: calls.append("import"))
    monkeypatch.setattr(
        migration,
        "_remove_bootstrap_access",
        lambda *_: calls.append("cleanup"),
    )

    with pytest.raises(RuntimeError, match="iam failed"):
        migration._apply_plan(migration.build_plan())

    assert calls == ["cleanup"]


def test_import_failure_rolls_back_attempt_grants_and_removes_bootstrap_access(monkeypatch):
    calls = []
    authority = _fixture_authority()
    created = (migration.build_plan().iam_plan.proposed_job_user_grants[0],)
    monkeypatch.setattr(migration, "_load_credentials", lambda: object())
    monkeypatch.setattr(migration, "_load_bootstrap_inputs", lambda _: _fixture_inputs())
    monkeypatch.setattr(migration, "_verify_bootstrap_authority", lambda *args: authority)
    monkeypatch.setattr(migration, "_read_preflight", lambda *_: None)
    monkeypatch.setattr(migration, "_create_bootstrap_lock", lambda *_: None)
    monkeypatch.setattr(migration, "_apply_ddl", lambda *_: None)
    monkeypatch.setattr(migration, "_readback_store", lambda *_: None)
    monkeypatch.setattr(migration, "_apply_iam_plan", lambda *_: created)
    monkeypatch.setattr(
        migration,
        "_import_bootstrap",
        lambda *_: (_ for _ in ()).throw(RuntimeError("import failed")),
    )
    monkeypatch.setattr(
        migration,
        "_rollback_attempt_grants",
        lambda *_: calls.append("rollback"),
    )
    monkeypatch.setattr(
        migration,
        "_remove_bootstrap_access",
        lambda *_: calls.append("cleanup"),
    )

    with pytest.raises(RuntimeError, match="import failed"):
        migration._apply_plan(migration.build_plan())

    assert calls == ["rollback", "cleanup"]


def test_source_snapshot_consumption_is_separate_locked_and_cross_manifest():
    sql = (ROUTINES / "sp_consume_open_intelligence_source_snapshot_v1.sql").read_text()
    ordinary = (ROUTINES / "sp_consume_open_intelligence_execution_v1.sql").read_text()
    assert "WHEN 'source_snapshot_capture'" not in ordinary
    assert (
        sql.index("BEGIN TRANSACTION")
        < sql.index("SET v_initial_count")
        < sql.index("INSERT INTO")
        < sql.index("COMMIT TRANSACTION")
    )
    assert "state = 'ready' AND lock_version = v_lock_version" in sql
    assert "v_initial_count = 0 AND v_recovery_count = 0" in sql
    assert "v_initial_count = 1 AND v_recovery_count = 0" in sql
    counts = sql.split("SET v_initial_count =", 1)[1].split("IF v_mode =", 1)[0]
    assert "v_manifest_sha256" not in counts
    assert "open_intelligence_execution_results_v1" not in counts
    assert "v_reserved_micro_usd + 500000 <= 1000000" in sql
    assert "DELETE FROM" not in sql
    assert "source_snapshot_recovery_unavailable" in sql
    assert "r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))" in sql
    assert "['capture_contract', 'capture_plan', 'source_metadata']" in sql
    assert "LOWER(TO_HEX(SHA256(supplied.body)))" in sql


def test_source_snapshot_result_guards_do_not_refund_or_replace_prior_capture():
    sql = (ROUTINES / "sp_record_open_intelligence_execution_result_v1.sql").read_text()
    assert "source_snapshot_result_invalid" in sql
    assert "source_snapshot_recovery_result_mismatch" in sql
    assert "source_snapshot_creation_replaced" in sql
    assert "v_source_initial_manifest" in sql
    assert "stored_artifact" in sql
    assert "artifact_attempt" in sql
    assert "query_count" in sql
    assert "DELETE FROM" not in sql
    assert "reserved_micro_usd" not in sql


def test_source_snapshot_migration_reuses_staging_principal_without_direct_ledger_grant():
    plan = migration.build_plan()
    source_routine = next(
        item
        for item in plan.routines
        if item.name == "sp_consume_open_intelligence_source_snapshot_v1"
    )
    assert source_routine.parameters == EXPECTED_PARAMETERS[ROUTINE_NAMES[8]]
    source = [
        item
        for item in plan.iam_plan.principal_routine_bindings
        if item.resource.endswith("/sp_consume_open_intelligence_source_snapshot_v1")
    ]
    assert len(source) == 1
    assert (
        source[0].principal
        == "serviceAccount:trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert plan.tables == migration.build_plan().tables


def test_source_snapshot_actual_count_predicates_keep_initial_reservation_across_manifests():
    sql = (ROUTINES / "sp_consume_open_intelligence_source_snapshot_v1.sql").read_text()
    with closing(sqlite3.connect(":memory:")) as db, db:
        db.execute(
            "CREATE TABLE open_intelligence_execution_approvals_v1 (approval_id,manifest_sha256,canonical_manifest_json)"
        )
        db.execute(
            "CREATE TABLE open_intelligence_execution_consumptions_v1 (approval_id,manifest_sha256,operation)"
        )

        def consume(manifest, cutoff, mode):
            document = json.dumps(
                {"arguments": ["script", "--cutoff-date", cutoff, "--mode", mode]}
            )
            db.execute(
                "INSERT INTO open_intelligence_execution_approvals_v1 VALUES (?,?,?)",
                (manifest, manifest, document),
            )
            db.execute(
                "INSERT INTO open_intelligence_execution_consumptions_v1 VALUES (?,?,?)",
                (manifest, manifest, "source_snapshot_capture"),
            )

        def values(cutoff):
            result = {}
            for name in ("v_initial_count", "v_recovery_count", "v_reserved_micro_usd"):
                start = sql.index(f"  SET {name} = ")
                fragment = sql[start : sql.index("\n  );", start)]
                query = fragment[fragment.index("SELECT COUNT(*)") :]
                query = re.sub(r"`\{project\}\.\{dataset\}\.([a-z0-9_]+)`", r"\1", query)
                query = query.replace("JSON_VALUE", "json_extract").replace("v_cutoff", ":cutoff")
                result[name] = db.execute(query, {"cutoff": cutoff}).fetchone()[0]
                if name == "v_reserved_micro_usd":
                    result[name] *= 500000
            return result

        def permitted(mode, cutoff):
            marker = (
                "ASSERT v_initial_count = 0" if mode == "initial" else "ASSERT v_initial_count = 1"
            )
            guard = sql.split(marker, 1)[1].split("\n", 1)[0]
            guard = marker.removeprefix("ASSERT ") + guard
            parameters = values(cutoff)
            for name in parameters:
                guard = guard.replace(name, ":" + name)
            return bool(db.execute("SELECT " + guard, parameters).fetchone()[0])

        assert permitted("initial", "2026-09-07")
        assert not permitted("recover", "2026-09-07")
        consume("first-manifest", "2026-09-07", "initial")
        assert not permitted("initial", "2026-09-07")
        assert permitted("recover", "2026-09-07")
        assert values("2026-09-07")["v_reserved_micro_usd"] == 500000
        consume("different-recovery-manifest", "2026-09-07", "recover")
        assert not permitted("initial", "2026-09-07")
        assert not permitted("recover", "2026-09-07")
        assert permitted("initial", "2026-09-08")
        consume("second-cutoff-manifest", "2026-09-08", "initial")
        assert values("2026-09-08")["v_reserved_micro_usd"] == 1000000
        assert not permitted("initial", "2026-09-08")


_SOURCE_WRITER_FAILED_JSON = (
    '{"artifact_attempt":null,"capture_receipt_digest":null,"captured_at":null,"client_scope_'
    'id":"ogilvy_default","contract_version":"open_intelligence_protected_source_snapshot_v1"'
    ',"creation_records":[{"destination":"ogilvy-trends-v2.trends_v2_staging.open_intelligenc'
    'e_v3_source_20260907_event_ledger","job_id":"oi_v3_snapshot_109ea182ba6f1b7042c76ecbea7d'
    '62cab753b3867cca57c909b422878b0a5433_event_ledger","lane":"event_ledger","native_job_dig'
    'est":null,"state":"unresolved"}],"cutoff_date":"2026-09-07","limitations":["upstream_col'
    'lection_completeness_unproven"],"market_scope":["ke","ng","za"],"missing_checks":["snaps'
    'hot_creation_incomplete"],"query_count":0,"snapshot_digest":null,"snapshot_plan_digest":'
    '"866bb6439e41eb21baef9d87e50f7168e236613208c428cead643836e3844e29","source_as_of":"2026-'
    '09-08T00:00:00+00:00","stored_artifact":null,"total_bytes_billed":null}'
)
_SOURCE_WRITER_INITIAL = "8064a2d1a641ba1e61edca95b8db2a98846d12936baf51efdfdedcdeeda48de9"
_SOURCE_WRITER_CURRENT = "109ea182ba6f1b7042c76ecbea7d62cab753b3867cca57c909b422878b0a5433"
_SOURCE_WRITER_V2_PIN = "ade29835ba6ca2192ad2ae7ae3d87d97567ce7ea4796390ce7f5c934fd1a1d95"


def _source_writer_creation_guard(
    previous,
    current,
    *,
    pin=_SOURCE_WRITER_V2_PIN,
    initial=_SOURCE_WRITER_INITIAL,
    status="failed",
    continuation_previous=None,
    continuation_manifest=_SOURCE_WRITER_CURRENT,
    current_manifest=_SOURCE_WRITER_CURRENT,
):
    sql = (ROUTINES / "sp_record_open_intelligence_execution_result_v1.sql").read_text()
    marker = "ASSERT NOT EXISTS (\n        SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_source_previous"
    guard = sql.split(marker, 1)[1].split(" AS 'source_snapshot_creation_replaced'", 1)[0]
    guard = marker.removeprefix("ASSERT ") + guard
    parameters = {
        "v_source_previous": json.dumps(previous),
        "v_source_result": json.dumps(current),
        "v_source_initial_manifest": initial,
        "v_source_previous_status": status,
        "v_source_manifest": json.dumps(
            {"input_artifacts": [{"name": "recovery_context", "sha256": pin}]}
        ),
        "current_manifest": current_manifest,
        "v_source_replacement_allowed": 0,
        "v_source_continuation_allowed": 0,
        "v_source_metadata_continuation_allowed": 0,
    }

    def translate(expression):
        expression = expression.replace("UNNEST(", "json_each(")
        expression = expression.replace(" WITH OFFSET prior_position", "")
        expression = expression.replace(" WITH OFFSET earlier_position", "")
        expression = expression.replace("prior_position", "CAST(prior.key AS INTEGER)")
        expression = expression.replace("earlier_position", "CAST(earlier.key AS INTEGER)")
        expression = expression.replace("v_consumption.manifest_sha256", ":current_manifest")
        expression = expression.replace("ARRAY<JSON>[]", "'[]'")
        for alias in ("prior", "current_item", "artifact", "earlier", "item"):
            expression = re.sub(
                r"(JSON_(?:VALUE|QUERY)\()" + alias + r"(?=,)", r"\1" + alias + ".value", expression
            )
        for name in parameters:
            if name != "current_manifest":
                expression = re.sub(r"\b" + name + r"\b", ":" + name, expression)
        return expression

    def field(value, path):
        return json.loads(value).get(path.removeprefix("$.")) if value is not None else None

    def query(value, path):
        if value is None or path.removeprefix("$.") not in json.loads(value):
            return None
        return json.dumps(field(value, path), separators=(",", ":"))

    def value(value, path):
        result = field(value, path)
        if result is None or isinstance(result, (dict, list)):
            return None
        return str(result) if not isinstance(result, bool) else str(result).lower()

    def json_type(value):
        if value is None:
            return None
        result = json.loads(value)
        return {
            type(None): "null",
            dict: "object",
            list: "array",
            bool: "boolean",
            int: "number",
            float: "number",
            str: "string",
        }[type(result)]

    with closing(sqlite3.connect(":memory:")) as db, db:
        db.create_function("JSON_VALUE", 2, value)
        db.create_function("JSON_QUERY", 2, query)
        db.create_function("JSON_QUERY_ARRAY", 2, query)
        db.create_function("JSON_TYPE", 1, json_type)
        db.create_function(
            "ARRAY_LENGTH", 1, lambda value: len(json.loads(value)) if value else None
        )
        db.create_function(
            "CONCAT", -1, lambda *values: "".join(values) if None not in values else None
        )
        db.create_function("IF", 3, lambda condition, yes, no: yes if condition else no)
        if "SET v_source_metadata_continuation_allowed = " in sql:
            metadata_gate = sql.split("SET v_source_metadata_continuation_allowed = ", 1)[1].split(
                ";", 1
            )[0]
            parameters["v_source_metadata_continuation_allowed"] = db.execute(
                "SELECT " + translate(metadata_gate), parameters
            ).fetchone()[0]
        if "SET v_source_continuation_allowed = " in sql:
            continuation = sql.split("SET v_source_continuation_allowed = ", 1)[1].split(";", 1)[0]
            parameters["v_source_continuation_allowed"] = db.execute(
                "SELECT " + translate(continuation), parameters
            ).fetchone()[0]
        if continuation_previous is not None:
            selected = initial
            if "SET v_source_previous_manifest = " in sql:
                selection = sql.split("SET v_source_previous_manifest = ", 1)[1].split(";", 1)[0]
                selected = db.execute("SELECT " + translate(selection), parameters).fetchone()[0]
            if selected == continuation_manifest:
                parameters["v_source_previous"] = json.dumps(continuation_previous)
        if parameters["v_source_continuation_allowed"]:
            validity = sql.split("IF v_source_continuation_allowed THEN\n        ASSERT ", 1)[
                1
            ].split(" AS 'source_snapshot_continuation_invalid'", 1)[0]
            if not db.execute("SELECT " + translate(validity), parameters).fetchone()[0]:
                return False
        if "SET v_source_replacement_allowed = " in sql:
            eligibility = sql.split("SET v_source_replacement_allowed = ", 1)[1].split(";", 1)[0]
            parameters["v_source_replacement_allowed"] = db.execute(
                "SELECT " + translate(eligibility), parameters
            ).fetchone()[0]
        allowed = bool(db.execute("SELECT " + translate(guard), parameters).fetchone()[0])
        tail = sql.split("      ) AS 'source_snapshot_creation_replaced';\n    END IF;", 1)[1]
        extra_guard = tail.split("ASSERT ", 1)[1].split(
            " AS 'source_snapshot_creation_replaced'", 1
        )[0]
        return allowed and bool(
            db.execute("SELECT " + translate(extra_guard), parameters).fetchone()[0]
        )


def _source_writer_failure_pair():
    current = json.loads(_SOURCE_WRITER_FAILED_JSON)
    previous = deepcopy(current)
    previous["creation_records"][0]["job_id"] = previous["creation_records"][0]["job_id"].replace(
        _SOURCE_WRITER_CURRENT, _SOURCE_WRITER_INITIAL
    )
    return previous, current


def test_source_writer_accepts_exact_captured_v2_failed_replacement():
    assert len(_SOURCE_WRITER_FAILED_JSON.encode()) == 863
    assert (
        hashlib.sha256(_SOURCE_WRITER_FAILED_JSON.encode()).hexdigest()
        == "466d2bafc12afd90fcc863ef300bb3f3b8875957a2e6d5d29c47ad3a8def2f23"
    )
    previous, current = _source_writer_failure_pair()
    assert _source_writer_creation_guard(previous, current)
    assert current["query_count"] == 0
    assert current["stored_artifact"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "v1_pin",
        "unknown_pin",
        "wrong_initial",
        "prior_succeeded",
        "query_count",
        "missing_query_count",
        "captured_at",
        "snapshot_digest",
        "capture_receipt_digest",
        "artifact_attempt",
        "stored_artifact",
        "wrong_current_job",
        "wrong_destination",
    ],
)
def test_source_writer_rejects_unbound_replacement(mutation):
    previous, current = _source_writer_failure_pair()
    kwargs = {}
    if mutation in {"v1_pin", "unknown_pin"}:
        kwargs["pin"] = "1" * 64 if mutation == "v1_pin" else None
    elif mutation == "wrong_initial":
        kwargs["initial"] = "2" * 64
    elif mutation == "prior_succeeded":
        kwargs["status"] = "succeeded"
    elif mutation == "query_count":
        previous["query_count"] = 1
    elif mutation == "missing_query_count":
        previous.pop("query_count")
    elif mutation == "wrong_current_job":
        current["creation_records"][0]["job_id"] = "oi_v3_snapshot_" + "3" * 64 + "_event_ledger"
    elif mutation == "wrong_destination":
        current["creation_records"][0]["destination"] += "_other"
    else:
        previous[mutation] = "present"
    assert not _source_writer_creation_guard(previous, current, **kwargs)


def _source_writer_prefix_pair():
    previous, current = _source_writer_failure_pair()
    prefix = deepcopy(previous["creation_records"][0])
    prefix.update(state="succeeded", native_job_digest="4" * 64)
    for document in (previous, current):
        last = document["creation_records"][0]
        for field in ("lane", "job_id", "destination"):
            last[field] = last[field].replace("event_ledger", "seed_graph")
        document["creation_records"].insert(0, deepcopy(prefix))
    return previous, current


def test_source_writer_preserves_successful_prefix_and_v1_identity_rules():
    previous, current = _source_writer_prefix_pair()
    assert _source_writer_creation_guard(previous, current)
    previous, _ = _source_writer_failure_pair()
    assert _source_writer_creation_guard(previous, deepcopy(previous), pin="1" * 64)


@pytest.mark.parametrize(
    "mutation", ["prefix_state", "prefix_digest", "second_replacement", "two_failed_prior"]
)
def test_source_writer_rejects_prefix_mutation_or_multiple_replacement(mutation):
    previous, current = _source_writer_prefix_pair()
    if mutation == "prefix_state":
        current["creation_records"][0]["state"] = "unresolved"
    elif mutation == "prefix_digest":
        current["creation_records"][0]["native_job_digest"] = "5" * 64
    elif mutation == "second_replacement":
        current["creation_records"][0]["job_id"] = current["creation_records"][0]["job_id"].replace(
            _SOURCE_WRITER_INITIAL, _SOURCE_WRITER_CURRENT
        )
    else:
        previous["creation_records"][0]["state"] = "unresolved"
        current["creation_records"][0]["state"] = "unresolved"
    assert not _source_writer_creation_guard(previous, current)


_SOURCE_CONSUMER_ANCESTOR = {
    "contract_version": "open_intelligence_source_capture_recovery_v2",
    "failed_creation_job_digest": "70fe5c118a8fa5fe1412cbb05eb8df31d59611db98bbfcd0496f300ec17652b0",
    "initial_consumption_id": "exc_bbd9638448ea84e64cae5da3c34da6f7317086198c78fe08e4db73a9d1abfbee",
    "initial_execution_name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/executions/trends-engine-oi-source-snapshot-staging-jh4gv",
    "initial_manifest_sha256": _SOURCE_WRITER_INITIAL,
    "initial_result_digest": "11ca447617d92fe3e597fe374c78430627c81c4bc7557425e0ba92dabc5ae247",
    "initial_result_id": "exr_5472f0d0f5270cb5a7d7ae05ee4f4b0c4ff1967bd01d7c930c9803c5d6afcea0",
}


def _source_consumer_v3_predecessor_count(mutation=None, *, version=3):
    sql = (ROUTINES / "sp_consume_open_intelligence_source_snapshot_v1.sql").read_text()
    query = sql.split("SET v_initial_result_count = (", 1)[1].split("\n    );", 1)[0]
    artifacts = [
        {"name": name, "sha256": "1" * 64}
        for name in ("capture_contract", "capture_plan", "source_metadata")
    ]
    previous_manifest = {
        "arguments": ["script", "--cutoff-date", "2026-09-07", "--mode", "recover"],
        "input_artifacts": [
            *artifacts,
            {"name": "recovery_context", "sha256": _SOURCE_WRITER_V2_PIN},
        ],
    }
    current_manifest = deepcopy(previous_manifest)
    payload = json.loads(_SOURCE_WRITER_FAILED_JSON)
    status = "failed"
    context = {
        "contract_version": "open_intelligence_source_capture_recovery_v3",
        "initial_consumption_id": "exc_" + "a" * 64,
        "initial_execution_name": "synthetic-predecessor-execution",
        "initial_manifest_sha256": "b" * 64,
        "initial_result_id": "exr_" + "c" * 64,
        "ancestor_recovery_context": deepcopy(_SOURCE_CONSUMER_ANCESTOR),
    }
    if version == 4:
        context = json.loads(_SOURCE_METADATA_CONTEXT_JSON)
        payload = json.loads(_SOURCE_METADATA_PREDECESSOR_JSON)
        previous_manifest["input_artifacts"][-1]["sha256"] = _SOURCE_WRITER_V3_PIN
    if mutation == "prior_initial":
        previous_manifest["arguments"][4] = "initial"
    elif mutation == "prior_success":
        status = "succeeded"
    elif mutation == "query_count":
        payload["query_count"] = 1
    elif mutation in {
        "captured_at",
        "snapshot_digest",
        "capture_receipt_digest",
        "artifact_attempt",
        "stored_artifact",
    }:
        payload[mutation] = "not-null"
    elif mutation == "source_changed":
        current_manifest["input_artifacts"][0]["sha256"] = "2" * 64
    elif mutation == "ancestor_binding_changed":
        previous_manifest["input_artifacts"][-1]["sha256"] = "3" * 64
    elif mutation == "ancestor_value_changed":
        context["ancestor_recovery_context"]["failed_creation_job_digest"] = "4" * 64
    if mutation == "inner_ancestor_changed":
        context["ancestor_recovery_context"]["ancestor_recovery_context"]["initial_result_id"] = (
            "changed"
        )
    if mutation == "wrong_predecessor_manifest":
        context["initial_manifest_sha256"] = "b" * 64
    if mutation == "wrong_predecessor_result":
        context["initial_result_id"] = "exr_" + "c" * 64
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    context["initial_result_digest"] = hashlib.sha256(raw.encode()).hexdigest()
    ancestor_digest = hashlib.sha256(
        json.dumps(
            context["ancestor_recovery_context"], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    query = re.sub(r"`\{project\}.\{dataset\}.([a-z0-9_]+)`", r"\1", query)
    query = query.replace(
        "UNNEST(['capture_contract', 'capture_plan', 'source_metadata']) name",
        'json_each(\'["capture_contract","capture_plan","source_metadata"]\') name',
    )
    query = re.sub(
        r"JSON_TYPE\(JSON_QUERY\(SAFE.PARSE_JSON\(r.canonical_result_json\), ('[^']+')\)\)",
        r"json_type(r.canonical_result_json, \1)",
        query,
    )
    query = query.replace("= 'number'", "IN ('integer', 'real')")
    query = query.replace("UNNEST(", "json_each(").replace("JSON_QUERY_ARRAY", "json_extract")
    for alias in ("item", "artifact"):
        query = query.replace("JSON_VALUE(" + alias + ",", "JSON_VALUE(" + alias + ".value,")
    query = query.replace(" = name)", " = name.value)")
    query = re.sub(
        r"JSON_VALUE\(([^(),]+), ('[^']+')\)", r"CAST(json_extract(\1, \2) AS TEXT)", query
    )
    query = query.replace("v_approval.canonical_manifest_json", ":new_manifest")
    for name in (
        "v_cutoff",
        "v_recovery",
        "v_consumed_at",
        "v_ancestor_context_digest",
        "v_metadata_ancestor_context_digest",
        "v_inner_ancestor_context_digest",
    ):
        query = re.sub(r"\b" + name + r"\b", ":" + name, query)
    with closing(sqlite3.connect(":memory:")) as db, db:
        db.create_function("IF", 3, lambda condition, yes, no: yes if condition else no)
        db.create_function("SHA256", 1, lambda value: hashlib.sha256(value.encode()).digest())
        db.create_function("TO_HEX", 1, lambda value: value.hex())
        db.execute(
            "CREATE TABLE open_intelligence_execution_approvals_v1 (approval_id,manifest_sha256,operation,canonical_manifest_json)"
        )
        db.execute(
            "CREATE TABLE open_intelligence_execution_consumptions_v1 (consumption_id,approval_id,manifest_sha256,operation,execution_name,consumed_at)"
        )
        db.execute(
            "CREATE TABLE open_intelligence_execution_results_v1 (result_id,consumption_id,approval_id,manifest_sha256,operation,execution_name,result_digest,canonical_result_json,status,completed_at)"
        )
        db.execute(
            "INSERT INTO open_intelligence_execution_approvals_v1 VALUES (?,?,?,?)",
            (
                "approval",
                context["initial_manifest_sha256"],
                "source_snapshot_capture",
                json.dumps(previous_manifest),
            ),
        )
        db.execute(
            "INSERT INTO open_intelligence_execution_consumptions_v1 VALUES (?,?,?,?,?,?)",
            (
                context["initial_consumption_id"],
                "approval",
                context["initial_manifest_sha256"],
                "source_snapshot_capture",
                context["initial_execution_name"],
                "2026-09-08T11:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO open_intelligence_execution_results_v1 VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                context["initial_result_id"],
                context["initial_consumption_id"],
                "approval",
                context["initial_manifest_sha256"],
                "source_snapshot_capture",
                context["initial_execution_name"],
                context["initial_result_digest"],
                raw,
                status,
                "2026-09-08T12:00:00Z",
            ),
        )
        return db.execute(
            query,
            {
                "v_recovery": json.dumps(context),
                "v_cutoff": "2026-09-07",
                "v_consumed_at": "2026-09-08T13:00:00Z",
                "new_manifest": json.dumps(current_manifest),
                "v_ancestor_context_digest": ancestor_digest if version == 3 else None,
                "v_metadata_ancestor_context_digest": ancestor_digest if version == 4 else None,
                "v_inner_ancestor_context_digest": hashlib.sha256(
                    json.dumps(
                        context["ancestor_recovery_context"]["ancestor_recovery_context"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if version == 4
                else None,
            },
        ).fetchone()[0]


def test_source_consumer_v3_selects_failed_recovery_predecessor():
    assert _source_consumer_v3_predecessor_count() == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "prior_initial",
        "prior_success",
        "query_count",
        "captured_at",
        "snapshot_digest",
        "capture_receipt_digest",
        "artifact_attempt",
        "stored_artifact",
        "source_changed",
        "ancestor_binding_changed",
        "ancestor_value_changed",
    ],
)
def test_source_consumer_v3_refuses_wrong_predecessor_or_ancestry(mutation):
    assert _source_consumer_v3_predecessor_count(mutation) == 0


def test_source_consumer_v3_ancestor_hash_uses_sorted_typed_struct():
    sql = (ROUTINES / "sp_consume_open_intelligence_source_snapshot_v1.sql").read_text()
    fragment = sql.split("SET v_ancestor_context_digest = ", 1)[1].split(";", 1)[0]
    fields = re.findall(
        r"JSON_VALUE\(v_recovery, '\$.ancestor_recovery_context.([a-z0-9_]+)'\) AS ([a-z0-9_]+)",
        fragment,
    )
    assert fields == [(key, key) for key in sorted(_SOURCE_CONSUMER_ANCESTOR)]
    assert "TO_JSON_STRING(STRUCT(" in fragment
    assert (
        hashlib.sha256(
            json.dumps(_SOURCE_CONSUMER_ANCESTOR, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == _SOURCE_WRITER_V2_PIN
    )
    assert (
        "ancestor_recovery_context,contract_version,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id"
        in sql
    )


def test_source_consumer_v3_single_continuation_does_not_rereserve_money():
    sql = (ROUTINES / "sp_consume_open_intelligence_source_snapshot_v1.sql").read_text()
    legacy = re.search(r"ASSERT (v_initial_count = 1 AND v_recovery_count = 0[^\n]+)", sql).group(1)
    continuation = re.search(
        r"ASSERT (v_initial_count = 1 AND v_recovery_count = 1[^\n]+)", sql
    ).group(1)
    with closing(sqlite3.connect(":memory:")) as db, db:

        def permitted(expression, initial, recoveries, reserved):
            for name in ("v_initial_count", "v_recovery_count", "v_reserved_micro_usd"):
                expression = expression.replace(name, ":" + name)
            return bool(
                db.execute(
                    "SELECT " + expression,
                    {
                        "v_initial_count": initial,
                        "v_recovery_count": recoveries,
                        "v_reserved_micro_usd": reserved,
                    },
                ).fetchone()[0]
            )

        assert permitted(legacy, 1, 0, 500000)
        assert not permitted(legacy, 1, 1, 500000)
        assert permitted(continuation, 1, 1, 500000)
        assert not permitted(continuation, 1, 2, 500000)
        assert not permitted(continuation, 0, 1, 0)
        assert not permitted(continuation, 1, 1, 1000001)
    assert "SET v_reserved_micro_usd = 500000 * (" in sql


_SOURCE_WRITER_V3_PIN = "656e88826f4a5c9178d754b5a3ce213c9e7d52127be3ef6427a56feed4bd9b7c"


def test_source_writer_v3_preserves_consumed_recovery_baseline():
    initial, previous = _source_writer_failure_pair()
    current = deepcopy(previous)
    current["creation_records"][0].update(state="succeeded", native_job_digest="6" * 64)
    assert _source_writer_creation_guard(
        initial,
        current,
        pin=_SOURCE_WRITER_V3_PIN,
        continuation_previous=previous,
        current_manifest="7" * 64,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_v3_pin",
        "v1_pin",
        "replacement",
        "destination",
        "missing_prior",
        "prior_success",
        "query_count",
        "captured_at",
        "artifact_attempt",
        "stored_artifact",
        "snapshot_digest",
        "capture_receipt_digest",
        "prefix_digest",
    ],
)
def test_source_writer_v3_refuses_unbound_or_changed_baseline(mutation):
    initial, previous = _source_writer_failure_pair()
    current = deepcopy(previous)
    pin, status = _SOURCE_WRITER_V3_PIN, "failed"
    if mutation in {"unknown_v3_pin", "v1_pin"}:
        pin = "8" * 64 if mutation == "unknown_v3_pin" else "1" * 64
    elif mutation == "replacement":
        current["creation_records"][0]["job_id"] = current["creation_records"][0]["job_id"].replace(
            _SOURCE_WRITER_CURRENT, "7" * 64
        )
    elif mutation == "destination":
        current["creation_records"][0]["destination"] += "_wrong"
    elif mutation == "missing_prior":
        current["creation_records"] = []
    elif mutation == "prior_success":
        status = "succeeded"
    elif mutation == "query_count":
        previous["query_count"] = 1
    elif mutation == "prefix_digest":
        previous["creation_records"][0].update(state="succeeded", native_job_digest="6" * 64)
        current["creation_records"][0].update(state="succeeded", native_job_digest="9" * 64)
    else:
        previous[mutation] = "not-null"
    assert not _source_writer_creation_guard(
        initial,
        current,
        pin=pin,
        status=status,
        continuation_previous=previous,
        current_manifest="7" * 64,
    )


def test_source_writer_v3_keeps_original_ancestor_artifact_prefix():
    sql = (ROUTINES / "sp_record_open_intelligence_execution_result_v1.sql").read_text()
    assert "r.manifest_sha256=v_source_previous_manifest" in sql
    assert (
        "'gs://ogilvy-trends-v2-oi-source-artifacts-staging/captures/',v_source_initial_manifest"
        in sql
    )


_SOURCE_METADATA_CONTEXT_JSON = (
    '{"ancestor_recovery_context":{"ancestor_recovery_context":{"contract_version":"open_inte'
    'lligence_source_capture_recovery_v2","failed_creation_job_digest":"70fe5c118a8fa5fe1412c'
    'bb05eb8df31d59611db98bbfcd0496f300ec17652b0","initial_consumption_id":"exc_bbd9638448ea8'
    '4e64cae5da3c34da6f7317086198c78fe08e4db73a9d1abfbee","initial_execution_name":"projects/'
    "ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/exe"
    'cutions/trends-engine-oi-source-snapshot-staging-jh4gv","initial_manifest_sha256":"8064a'
    '2d1a641ba1e61edca95b8db2a98846d12936baf51efdfdedcdeeda48de9","initial_result_digest":"11'
    'ca447617d92fe3e597fe374c78430627c81c4bc7557425e0ba92dabc5ae247","initial_result_id":"exr'
    '_5472f0d0f5270cb5a7d7ae05ee4f4b0c4ff1967bd01d7c930c9803c5d6afcea0"},"contract_version":"'
    'open_intelligence_source_capture_recovery_v3","initial_consumption_id":"exc_3178cc3f6011'
    'e245b21ade8f8677593f8cd23afedd7f23218ce13c209a2cbb47","initial_execution_name":"projects'
    "/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/ex"
    'ecutions/trends-engine-oi-source-snapshot-staging-v7k8f","initial_manifest_sha256":"109e'
    'a182ba6f1b7042c76ecbea7d62cab753b3867cca57c909b422878b0a5433","initial_result_digest":"4'
    '66d2bafc12afd90fcc863ef300bb3f3b8875957a2e6d5d29c47ad3a8def2f23","initial_result_id":"ex'
    'r_407b5ccbc5e2216f8e0582ea478af1cf9e726424c0aca5bb3ef0403ebc9f29bd"},"contract_version":'
    '"open_intelligence_source_capture_recovery_v4","initial_consumption_id":"exc_2aa600d87b7'
    'f29678cac363231de9d50e8feb2865ab033d9b593d8b670a66292","initial_execution_name":"project'
    "s/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/e"
    'xecutions/trends-engine-oi-source-snapshot-staging-4w2ck","initial_manifest_sha256":"1cb'
    '4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d16974","initial_result_digest":"'
    '72fe27188cd1ac022c4acc22ac096f91a56dc0ad601cd02020671980a144712c","initial_result_id":"e'
    'xr_1e53fdb9a6876cbc557efd6db93ff221883eb6e8b844276cc67960c3630a077d"}'
)


_SOURCE_METADATA_PREDECESSOR_JSON = (
    '{"artifact_attempt":null,"capture_receipt_digest":null,"captured_at":null,"client_scope_'
    'id":"ogilvy_default","contract_version":"open_intelligence_protected_source_snapshot_v1"'
    ',"creation_records":[{"destination":"ogilvy-trends-v2.trends_v2_staging.open_intelligenc'
    'e_v3_source_20260907_event_ledger","job_id":"oi_v3_snapshot_109ea182ba6f1b7042c76ecbea7d'
    '62cab753b3867cca57c909b422878b0a5433_event_ledger","lane":"event_ledger","native_job_dig'
    'est":"be521e66d35d6662a039f52de97330994634ac666554147ba5f2c85e8b6b11e0","state":"succeed'
    'ed"},{"destination":"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_2026'
    '0907_seed_graph","job_id":"oi_v3_snapshot_1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bc'
    'a7bdc09083a0d16974_seed_graph","lane":"seed_graph","native_job_digest":"752a21d94d825ac4'
    'd092df5546516408bd07114a4d8c80248f2c2613d2b97aea","state":"succeeded"},{"destination":"o'
    'gilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_seed_candidates",'
    '"job_id":"oi_v3_snapshot_1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d1697'
    '4_seed_candidates","lane":"seed_candidates","native_job_digest":"d217427db398a579372727f'
    'ba5f0f00f63fa2064444e20c09681210a899861fe","state":"succeeded"},{"destination":"ogilvy-t'
    'rends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_enriched_content","job_i'
    'd":"oi_v3_snapshot_1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d16974_enri'
    'ched_content","lane":"enriched_content","native_job_digest":"66aaa588bb92ce0c2313f16483c'
    '710788f58d7955c69615b76d6b2ca3621345b","state":"unresolved"}],"cutoff_date":"2026-09-07"'
    ',"limitations":["upstream_collection_completeness_unproven"],"market_scope":["ke","ng","'
    'za"],"missing_checks":["snapshot_creation_incomplete"],"query_count":0,"snapshot_digest"'
    ':null,"snapshot_plan_digest":"866bb6439e41eb21baef9d87e50f7168e236613208c428cead643836e3'
    '844e29","source_as_of":"2026-09-08T00:00:00+00:00","stored_artifact":null,"total_bytes_b'
    'illed":null}'
)


def test_source_consumer_v4_accepts_exact_failed_four_snapshot_predecessor():
    assert _source_consumer_v3_predecessor_count(version=4) == 1
    assert (
        hashlib.sha256(_SOURCE_METADATA_CONTEXT_JSON.encode()).hexdigest()
        == "cc8f820cdcc8603e5c1da881c16648a86a5c89132b67f78b84614587672e9e3a"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "prior_initial",
        "prior_success",
        "query_count",
        "captured_at",
        "snapshot_digest",
        "capture_receipt_digest",
        "artifact_attempt",
        "stored_artifact",
        "source_changed",
        "ancestor_binding_changed",
        "ancestor_value_changed",
        "inner_ancestor_changed",
        "wrong_predecessor_manifest",
        "wrong_predecessor_result",
    ],
)
def test_source_consumer_v4_rejects_predecessor_and_nested_changes(mutation):
    assert _source_consumer_v3_predecessor_count(mutation, version=4) == 0


def test_source_consumer_v4_single_use_count_guard():
    sql = (ROUTINES / "sp_consume_open_intelligence_source_snapshot_v1.sql").read_text()
    guard = re.search(r"ASSERT (v_initial_count = 1 AND v_recovery_count = 2[^\n]+)", sql).group(1)
    for name in ("v_initial_count", "v_recovery_count", "v_reserved_micro_usd"):
        guard = guard.replace(name, ":" + name)
    with closing(sqlite3.connect(":memory:")) as db, db:
        for count, expected in [(0, False), (1, False), (2, True), (3, False)]:
            assert (
                bool(
                    db.execute(
                        "SELECT " + guard,
                        {
                            "v_initial_count": 1,
                            "v_recovery_count": count,
                            "v_reserved_micro_usd": 500000,
                        },
                    ).fetchone()[0]
                )
                is expected
            )


def _source_writer_v4_guard(mutation=None):
    initial, _ = _source_writer_failure_pair()
    previous = json.loads(_SOURCE_METADATA_PREDECESSOR_JSON)
    current = deepcopy(previous)
    pin = "cc8f820cdcc8603e5c1da881c16648a86a5c89132b67f78b84614587672e9e3a"
    current["creation_records"][-1].update(state="succeeded", native_job_digest="6" * 64)
    current["creation_records"].append(
        {
            "lane": "raw_content",
            "job_id": "oi_v3_snapshot_" + "7" * 64 + "_raw_content",
            "destination": "ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_raw_content",
            "native_job_digest": "8" * 64,
            "state": "succeeded",
        }
    )
    if mutation == "wrong_pin":
        pin = "9" * 64
    elif mutation == "v3_pin":
        pin = _SOURCE_WRITER_V3_PIN
    elif mutation == "replace_existing":
        current["creation_records"][1]["job_id"] = "oi_v3_snapshot_" + "7" * 64 + "_seed_graph"
    elif mutation == "prefix_state":
        current["creation_records"][0]["state"] = "unresolved"
    elif mutation == "prefix_digest":
        current["creation_records"][0]["native_job_digest"] = "0" * 64
    elif mutation == "raw_owner":
        current["creation_records"][-1]["job_id"] = "oi_v3_snapshot_" + "9" * 64 + "_raw_content"
    return _source_writer_creation_guard(
        initial,
        current,
        pin=pin,
        continuation_previous=previous,
        continuation_manifest="1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d16974",
        current_manifest="7" * 64,
    )


def test_source_writer_v4_preserves_four_jobs_and_creates_only_raw():
    assert _source_writer_v4_guard()


@pytest.mark.parametrize(
    "mutation",
    ["wrong_pin", "v3_pin", "replace_existing", "prefix_state", "prefix_digest", "raw_owner"],
)
def test_source_writer_v4_refuses_changed_preserved_jobs(mutation):
    assert not _source_writer_v4_guard(mutation)
