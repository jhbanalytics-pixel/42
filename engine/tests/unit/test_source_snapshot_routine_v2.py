"""Text parity for the unregistered grant bound source snapshot consume routine (v2).

The routine file exists so the reservation policy has one versioned SQL expression. D03
registers it for installation (manifest row, migration entry); the v2 capture origin row
that would let an approval reach it is the activation step and is not bound yet.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from src.analysis.open_intelligence import staging_source_profile as policy
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit import test_execution_routines_v2 as s1

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / "infra" / "bigquery_routines"
NAME = "sp_consume_open_intelligence_source_snapshot_v2"
LEGACY = "sp_consume_open_intelligence_source_snapshot_v1"
PARAMETERS = (
    ("manifest_sha256", "STRING"),
    ("execution_name", "STRING"),
    ("job_resource", "STRING"),
    ("source_sha", "STRING"),
    ("image_uri", "STRING"),
    ("capture_plan_json", "STRING"),
    ("recovery_context_json", "STRING"),
    ("storage_policy_json", "STRING"),
    ("origin_registry_sha256", "STRING"),
    ("resource_manifest_sha256", "STRING"),
)
CODES = frozenset(
    {
        "execution_approval_v1_lock_not_disabled",
        "execution_approval_lock_not_ready",
        "execution_approval_concurrent_conflict",
        "execution_approval_generation_invalid",
        "execution_approval_generation_inactive",
        "execution_origin_registry_digest_mismatch",
        "execution_origin_registry_invalid",
        "execution_approval_unavailable",
        "execution_origin_pair_invalid",
        "execution_origin_policy_invalid",
        "execution_approval_identity_invalid",
        "execution_approval_manifest_mismatch",
        "execution_approval_execution_mismatch",
        "execution_approval_expired",
        "execution_approval_consumed",
        "execution_approval_schema_mismatch",
        "execution_approval_lock_invalid",
        "source_snapshot_operation_invalid",
        "source_snapshot_contract_invalid",
        "source_snapshot_artifact_mismatch",
        "source_snapshot_storage_policy_invalid",
        "source_snapshot_grant_invalid",
        "source_snapshot_grant_revoked",
        "source_snapshot_grant_expired",
        "source_snapshot_arguments_invalid",
        "source_snapshot_cutoff_not_permitted",
        "source_snapshot_plan_invalid",
        "source_snapshot_price_review_invalid",
        "source_snapshot_recovery_invalid",
        "source_snapshot_slot_consumed",
        "source_snapshot_grant_exhausted",
        "source_snapshot_recovery_unavailable",
        "source_snapshot_allowance_consumed",
        "source_snapshot_estate_mismatch",
    }
)
NEW_CODES = frozenset(
    {
        "source_snapshot_estate_mismatch",
        "source_snapshot_grant_invalid",
        "source_snapshot_grant_revoked",
        "source_snapshot_grant_expired",
        "source_snapshot_cutoff_not_permitted",
        "source_snapshot_slot_consumed",
        "source_snapshot_grant_exhausted",
    }
)
# Every refusal vector names the SQL branch that must refuse it and the code it raises.
REFUSE_VECTORS = (
    (
        "revoked grant",
        "JSON_VALUE(v_grant, '$.revocation_state') = 'active'",
        "source_snapshot_grant_revoked",
    ),
    (
        "expired grant",
        "v_consumed_at < TIMESTAMP(JSON_VALUE(v_grant, '$.valid_until'))",
        "source_snapshot_grant_expired",
    ),
    (
        "cutoff outside grant",
        "v_cutoff IN UNNEST(JSON_VALUE_ARRAY(v_grant, '$.allowed_cutoffs'))",
        "source_snapshot_cutoff_not_permitted",
    ),
    (
        "duplicate slot across renewals",
        "v_slot_initial_count = 0 AND v_slot_recovery_count = 0",
        "source_snapshot_slot_consumed",
    ),
    (
        "cumulative exhaustion",
        "v_grant_reserved_micro_usd + v_reserved_per_capture <= v_ceiling",
        "source_snapshot_grant_exhausted",
    ),
    (
        "recovery without initial",
        "v_slot_initial_count = 1",
        "source_snapshot_recovery_unavailable",
    ),
    (
        "grant argument mismatch",
        "JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[6]') = v_grant_id",
        "source_snapshot_arguments_invalid",
    ),
    (
        "contract outside grant",
        "v_contract_sha256 = JSON_VALUE(v_grant, '$.contract_sha256')",
        "source_snapshot_contract_invalid",
    ),
    (
        "ceiling below permitted cutoffs",
        "v_ceiling >= v_reserved_per_capture * ARRAY_LENGTH(JSON_VALUE_ARRAY(v_grant, '$.allowed_cutoffs'))",
        "source_snapshot_grant_invalid",
    ),
    (
        "headroom",
        "BETWEEN 0 AND v_reserved_per_capture - CAST(CEIL(v_reserved_per_capture * 0.10) AS INT64)",
        "source_snapshot_price_review_invalid",
    ),
    (
        "stale price review",
        "TIMESTAMP_ADD(TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.reviewed_at')), INTERVAL 24 HOUR)",
        "source_snapshot_price_review_invalid",
    ),
    (
        "estate outside grant",
        "JSON_VALUE(v_plan, '$.snapshot_plan.source_estate_digest') = JSON_VALUE(v_grant, '$.source_estate_digest')",
        "source_snapshot_estate_mismatch",
    ),
    (
        "recovery context shape",
        "ARRAY_TO_STRING(JSON_KEYS(v_recovery, 1), ',') = 'contract_version,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'",
        "source_snapshot_recovery_invalid",
    ),
    (
        "recovery chain unbound",
        "AND c.consumption_id = JSON_VALUE(v_recovery, '$.initial_consumption_id')",
        "source_snapshot_recovery_unavailable",
    ),
    (
        "recovery chain outside grant",
        "AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[6]') = v_grant_id\n        AND c.consumption_id",
        "source_snapshot_recovery_unavailable",
    ),
    (
        "snapshot before window",
        "TIMESTAMP(JSON_VALUE(v_plan, '$.snapshot_plan.snapshot_as_of')) >= TIMESTAMP(JSON_VALUE(v_plan, '$.snapshot_plan.observation_window_end'))",
        "source_snapshot_plan_invalid",
    ),
)
NOW = datetime(2026, 9, 13, 3, tzinfo=UTC)


def _sql() -> str:
    return (ROUTINES / f"{NAME}.sql").read_text(encoding="utf-8")


def _no_legacy_money(sql: str) -> None:
    """Refuse any legacy money or cutoff literal in the routine text (word bounded)."""
    for literal in (r"\b500000\b", r"\b1000000\b", "2026-09-07", "2026-09-08"):
        assert re.search(literal, sql) is None, literal
    for literal in ("trends-engine-staging@", "trends-engine-oi-"):
        assert literal not in sql, literal


TOUCHED_FILES = (
    "src/analysis/open_intelligence/capture_registry.py",
    "src/analysis/open_intelligence/staging_source_profile.py",
    "scripts/staging/capture_protected_production_snapshot.py",
    f"infra/bigquery_routines/{NAME}.sql",
    "tests/unit/test_capture_registry.py",
    "tests/unit/test_staging_source_profile.py",
    "tests/unit/test_source_snapshot_routine_v2.py",
    "tests/unit/test_protected_production_snapshot_cli.py",
)


def test_money_literal_check_bites_on_an_injected_literal():
    sql = _sql()
    _no_legacy_money(sql)
    injected = sql.replace("v_reserved_per_capture > 0", "v_reserved_per_capture > 500000", 1)
    assert injected != sql
    with pytest.raises(AssertionError, match="500000"):
        _no_legacy_money(injected)
    ceiling = sql.replace("<= v_ceiling AS", "<= 1000000 AS", 1)
    assert ceiling != sql
    with pytest.raises(AssertionError, match="1000000"):
        _no_legacy_money(ceiling)
    assert re.search(r"\b1000000\b", "BETWEEN 0 AND 1000000000") is None


def test_touched_files_carry_no_control_characters():
    for relative in TOUCHED_FILES:
        raw = (ROOT / relative).read_bytes()
        hits = sorted(set(re.findall(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", raw)))
        assert hits == [], (relative, hits)


def _grant_artifact() -> dict:
    grant = {
        "grant_id": "source_capture_grant_2026_09_v2",
        "environment": "staging",
        "source_estate_digest": "7" * 64,
        "contract_sha256": "8" * 64,
        "valid_from": datetime(2026, 9, 1, tzinfo=UTC),
        "valid_until": datetime(2026, 10, 1, tzinfo=UTC),
        "allowed_cutoffs": ["2026-09-12", "2026-09-13", "2026-09-14"],
        "reserved_micro_usd_per_capture": 750000,
        "cumulative_ceiling_micro_usd": 2250000,
        "revocation_state": "active",
    }
    price = {
        "observed_at": NOW - timedelta(hours=1),
        "pricing_sources": ["https://cloud.google.com/bigquery/pricing"],
        "storage_micro_usd_per_gib_month": 20000,
        "query_micro_usd_per_tib": 6250000,
        "operations_micro_usd_per_10k": 50000,
        "source_logical_bytes": 5 * 2**30,
        "max_bytes_billed_per_query": 5 * 2**30,
        "queries_per_cycle": 5,
        "jobs_per_cycle": 5,
        "retention_days": 90,
        "operations_per_cycle": 40,
        "permitted_retries": 1,
        "cadence_cycles_per_day": 1,
    }
    return policy.build_capture_policy_artifact(
        policy.GRANT_CAPTURE_POLICY_VERSION, grant=grant, price_inputs=price, now=NOW
    )


def test_routine_file_declares_the_v2_header_parameters_and_shape():
    sql = _sql()
    name, parameters = s1._header(sql)
    assert name == NAME
    assert parameters == PARAMETERS
    assert sql.rstrip().endswith("END;")
    assert sql.count("BEGIN TRANSACTION") == 1
    assert sql.count("COMMIT TRANSACTION") == 1
    assert "SESSION_USER()" in sql
    assert sql.index("BEGIN TRANSACTION") < sql.index(s1.V1_LOCK_DISABLED.split("\n")[0][:40])


def test_legacy_routine_is_byte_identical_and_the_v2_routine_is_registered_for_install():
    """D03 registers the routine for installation: a manifest row, a migration entry in the
    v2 plan and a parameter pin. The registry still binds no v2 capture origin, which the
    origins suite sentinel asserts separately; the origin row is the activation step."""
    legacy = ROUTINES / f"{LEGACY}.sql"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == s1.V1_ROUTINE_SHA256[legacy.name]
    from scripts.migrations import create_open_intelligence_execution_approval_store as migration

    assert NAME not in {name for name, *_ in migration.ROUTINE_SPECS}
    installed = {name: (role, parameters) for name, role, parameters in migration.V2_ROUTINE_SPECS}
    assert installed[NAME] == ("roles/bigquery.routineDataEditor", PARAMETERS)
    assert s1.V2_ROUTINE_PARAMETERS[NAME] == PARAMETERS
    assert migration.V2_SOURCE_SNAPSHOT_ROUTINE == NAME
    assert migration.V2_SOURCE_SNAPSHOT_OPERATION == "source_snapshot_capture"
    manifest = json.loads(migration.RESOURCE_MANIFEST_PATH.read_bytes())
    rows = {row["name"]: row["actions"] for row in manifest["resources"]}
    routine = f"//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/{migration.DATASET}/routines/{NAME}"
    assert rows[routine] == ["deploy", "invoke", "read"]
    assert rows[routine] == rows[routine.replace(NAME, "sp_consume_open_intelligence_execution_v2")]
    assert f"routines/{LEGACY}" not in json.dumps(manifest)
    referencing = sorted(
        path.relative_to(ROOT).as_posix()
        for folder in ("configs", "infra", "scripts", "src")
        for path in (ROOT / folder).rglob("*")
        if path.is_file()
        and path.suffix in {".json", ".sql", ".py", ".yaml", ".yml", ".md"}
        and NAME in path.read_text(encoding="utf-8", errors="ignore")
    )
    # The successor capture policy whose argument kind names it as the consume routine, the
    # manifest row, the routine file itself, the migration entry and the policy record that
    # names it as the grant routine, plus the dedicated Python caller. The origin row binds
    # it through the successor policy. Since amendment e the retained amendment d manifest,
    # still trusted for records written under ee809a4e, carries the same row, and the
    # retained amendment d delta the apply-v2 words still read holds its grant rows. The
    # inactive bridge candidate manifest keeps the retained row; the candidate bridge
    # policy names the v3 routine, never this one.
    assert referencing == [
        "configs/open_intelligence/iam_delta_amendment_d.json",
        "configs/open_intelligence/origin_contracts/successor-source-snapshot-capture.json",
        "configs/open_intelligence/resource_manifest_amendment_d.json",
        "configs/open_intelligence/resource_manifest_bridge_v3.json",
        "configs/open_intelligence/resource_manifest_v1.json",
        f"infra/bigquery_routines/{NAME}.sql",
        "scripts/migrations/create_open_intelligence_execution_approval_store.py",
        "src/analysis/open_intelligence/execution_approval.py",
        "src/analysis/open_intelligence/staging_source_profile.py",
    ]


def test_refusal_codes_are_exactly_the_documented_set_and_new_codes_are_named():
    sql = _sql()
    codes = set(re.findall(r"AS '([a-z0-9_]+)'", sql))
    assert codes == CODES
    legacy_codes = set(
        re.findall(r"AS '([a-z0-9_]+)'", (ROUTINES / f"{LEGACY}.sql").read_text(encoding="utf-8"))
    )
    execution_codes = set(
        re.findall(r"AS '([a-z0-9_]+)'", s1._sql("sp_consume_open_intelligence_execution_v2"))
    )
    assert codes - legacy_codes - execution_codes == NEW_CODES


@pytest.mark.parametrize("vector,branch,code", REFUSE_VECTORS)
def test_refuse_vectors_name_an_existing_sql_branch_and_code(vector, branch, code):
    sql = _sql()
    assert branch in sql, vector
    assert f"AS '{code}'" in sql, (vector, code)
    assert sql.index(branch) < sql.index(f"AS '{code}'", sql.index(branch)), vector


def test_consumption_preimage_and_record_match_the_v2_execution_pattern():
    sql = _sql()
    assert s1._preimage(sql, "exc_") == s1.CONSUMPTION_PREIMAGE
    assert s1.PREIMAGE_ARGUMENTS["sp_consume_open_intelligence_execution_v2"] in sql
    assert (
        "INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v2` VALUES ("
        in sql
    )
    assert (
        "'open_intelligence_execution_consumption_v2', v_consumption_id, v_approval.approval_id,"
        in sql
    )
    assert (
        "UNION ALL SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`"
        in sql
    )


def test_reservation_is_grant_bound_and_the_slot_spans_both_generations():
    sql = _sql()
    _no_legacy_money(sql)
    assert "SET v_grant_reserved_micro_usd = v_reserved_per_capture * v_grant_initial_count;" in sql
    assert "JSON_VALUE(a.canonical_manifest_json, '$.arguments[6]') = v_grant_id" in sql
    assert sql.count("JSON_VALUE(slot.canonical_manifest_json, '$.arguments[2]') = v_cutoff") == 2
    slot = sql[sql.index("SET v_slot_initial_count") : sql.index("SET v_slot_recovery_count")]
    assert "open_intelligence_execution_consumptions_v1" in slot
    assert "open_intelligence_execution_consumptions_v2" in slot
    assert "open_intelligence_execution_approvals_v1" in slot
    assert "open_intelligence_execution_approvals_v2" in slot
    assert (
        "SET v_reserved_per_capture = SAFE_CAST(JSON_VALUE(v_grant, '$.reserved_micro_usd_per_capture') AS INT64);"
        in sql
    )
    assert (
        "SET v_ceiling = SAFE_CAST(JSON_VALUE(v_grant, '$.cumulative_ceiling_micro_usd') AS INT64);"
        in sql
    )
    assert "JSON_QUERY(v_origin_row, '$.exact_operation_bindings.source_snapshot_capture')" in sql


def test_storage_and_grant_key_pins_match_the_python_policy_artifact():
    sql = _sql()
    artifact = _grant_artifact()
    canonical_bytes(artifact)
    storage_keys = ",".join(sorted(artifact))
    grant_keys = ",".join(sorted(artifact["grant"]))
    assert f"ARRAY_TO_STRING(JSON_KEYS(v_storage, 1), ',') = '{storage_keys}'" in sql
    assert f"ARRAY_TO_STRING(JSON_KEYS(v_grant, 1), ',') = '{grant_keys}'" in sql
    assert (
        f"JSON_VALUE(v_storage, '$.contract_version') = '{policy.GRANT_CAPTURE_POLICY_VERSION}'"
        in sql
    )
    assert f"JSON_VALUE(v_storage, '$.bucket') = '{artifact['bucket']}'" in sql
    assert (
        f"JSON_VALUE(v_storage, '$.max_source_logical_bytes') = '{artifact['max_source_logical_bytes']}'"
        in sql
    )
    assert (
        f"JSON_VALUE(v_storage, '$.max_artifact_bytes') = '{artifact['max_artifact_bytes']}'" in sql
    )
    assert (
        f"JSON_VALUE(v_storage, '$.snapshot_retention_days') = '{artifact['snapshot_retention_days']}'"
        in sql
    )
    assert f"JSON_VALUE(v_grant, '$.environment') = '{artifact['grant']['environment']}'" in sql
    fraction = policy.CAPTURE_POLICY_HEADROOM_MINIMUM
    assert f"CEIL(v_reserved_per_capture * {fraction.numerator / fraction.denominator:.2f})" in sql
    assert f"INTERVAL {policy.PRICE_OBSERVATION_VALIDITY.total_seconds() / 3600:.0f} HOUR" in sql
    assert f"'$.snapshot_plan.profile_version') = '{policy.NATIVE_IDENTITY_PROFILE_VERSION}'" in sql
    assert (
        f"'$.snapshot_plan.projection_version') = '{policy.NATIVE_IDENTITY_PROJECTION_VERSION}'"
        in sql
    )
    assert f"'$.snapshot_plan.source_dataset') = '{policy.STAGING_SOURCE_DATASET}'" in sql


def test_no_array_equality_or_multi_column_array_subquery():
    sql = _sql()
    assert re.search(r"ARRAY\(\s*SELECT\s+(?!AS STRUCT)[^\n]*,[^\n]*\sFROM\s", sql) is None
    assert re.search(r"\b(=|!=)\s*\[", sql) is None


# Behaviour: every refusal code the routine names is driven to its refusal in the Python
# policy layer. Where the Python layer names the same code (the reservation evaluator) the
# codes are equal; where the artifact and grant validators refuse earlier with their own
# code, the mapping is explicit.


def _reservation(**overrides):
    arguments = {
        "cutoff": "2026-09-12",
        "mode": "initial",
        "grant_id": "source_capture_grant_2026_09_v2",
        "source_estate_digest": "7" * 64,
        "ledger": {"slot_initial_count": 0, "slot_recovery_count": 0, "reserved_initial_count": 0},
        "recovery": None,
    }
    arguments.update(overrides)
    return policy.evaluate_capture_reservation(_grant_artifact()["grant"], now=NOW, **arguments)


def _price(**overrides):
    record = {
        "observed_at": NOW - timedelta(hours=1),
        "pricing_sources": ["https://cloud.google.com/bigquery/pricing"],
        "storage_micro_usd_per_gib_month": 20000,
        "query_micro_usd_per_tib": 6250000,
        "operations_micro_usd_per_10k": 50000,
        "source_logical_bytes": 5 * 2**30,
        "max_bytes_billed_per_query": 5 * 2**30,
        "queries_per_cycle": 5,
        "jobs_per_cycle": 5,
        "retention_days": 90,
        "operations_per_cycle": 40,
        "permitted_retries": 1,
        "cadence_cycles_per_day": 1,
    }
    record.update(overrides)
    return record


def _grant_with(**overrides):
    grant = dict(_grant_artifact()["grant"])
    grant.update(overrides)
    return grant


def _operator():
    from scripts.staging import capture_protected_production_snapshot

    return capture_protected_production_snapshot


def _profile_suite():
    from tests.unit import test_staging_source_profile

    return test_staging_source_profile


def _inputs_with_grant(**overrides):
    from tests.unit import test_protected_production_snapshot as input_fixture

    grant = _grant_with(
        allowed_cutoffs=["2026-09-07", "2026-09-09"], cumulative_ceiling_micro_usd=1500000
    )
    grant.update(overrides)
    artifact = policy.build_capture_policy_artifact(
        policy.GRANT_CAPTURE_POLICY_VERSION,
        grant=grant,
        price_inputs=_price(observed_at=input_fixture.NOW - timedelta(minutes=1)),
        now=input_fixture.NOW,
    )
    return (
        {**input_fixture.artifacts(), "storage_policy": canonical_bytes(artifact)},
        input_fixture.CUTOFF,
        input_fixture.NOW,
    )


def _contract_outside_grant():
    values, cutoff, now = _inputs_with_grant(contract_sha256="8" * 64)
    return _operator()._validate_inputs(values, cutoff, "initial", now)


RECOVERY = {"contract_version": "open_intelligence_source_capture_recovery_v1"}
BEHAVIOUR = {
    "source_snapshot_grant_revoked": (
        "capture_grant_revoked",
        lambda: policy.validate_capture_grant(_grant_with(revocation_state="revoked"), now=NOW),
    ),
    "source_snapshot_grant_expired": (
        "capture_grant_expired",
        lambda: policy.validate_capture_grant(_grant_with(valid_until=NOW.isoformat()), now=NOW),
    ),
    "source_snapshot_grant_invalid": (
        "capture_grant_ceiling_insufficient",
        lambda: policy.validate_capture_grant(
            _grant_with(cumulative_ceiling_micro_usd=2249999), now=NOW
        ),
    ),
    "source_snapshot_cutoff_not_permitted": (
        "source_snapshot_cutoff_not_permitted",
        lambda: _reservation(cutoff="2026-09-15"),
    ),
    "source_snapshot_slot_consumed": (
        "source_snapshot_slot_consumed",
        lambda: _reservation(
            ledger={"slot_initial_count": 1, "slot_recovery_count": 0, "reserved_initial_count": 0}
        ),
    ),
    "source_snapshot_grant_exhausted": (
        "source_snapshot_grant_exhausted",
        lambda: _reservation(
            ledger={"slot_initial_count": 0, "slot_recovery_count": 0, "reserved_initial_count": 3}
        ),
    ),
    "source_snapshot_recovery_unavailable": (
        "source_snapshot_recovery_unavailable",
        lambda: _reservation(mode="recover", recovery=RECOVERY),
    ),
    "source_snapshot_recovery_invalid": (
        "source_snapshot_recovery_invalid",
        lambda: _reservation(mode="initial", recovery=RECOVERY),
    ),
    "source_snapshot_allowance_consumed": (
        "source_snapshot_allowance_consumed",
        lambda: _reservation(
            mode="recover",
            recovery=RECOVERY,
            ledger={"slot_initial_count": 1, "slot_recovery_count": 1, "reserved_initial_count": 1},
        ),
    ),
    "source_snapshot_arguments_invalid": (
        "source_snapshot_arguments_invalid",
        lambda: _reservation(grant_id="another_grant"),
    ),
    "source_snapshot_estate_mismatch": (
        "source_snapshot_estate_mismatch",
        lambda: _reservation(source_estate_digest="9" * 64),
    ),
    "source_snapshot_contract_invalid": ("snapshot_inputs_invalid", _contract_outside_grant),
    "source_snapshot_price_review_invalid": (
        "capture_policy_headroom_insufficient",
        lambda: policy.build_capture_policy_artifact(
            policy.GRANT_CAPTURE_POLICY_VERSION,
            grant=_grant_with(),
            price_inputs=_price(queries_per_cycle=7),
            now=NOW,
        ),
    ),
    "source_snapshot_plan_invalid": (
        "source_run_snapshot_precedes_writes",
        lambda: _profile_suite().profile(snapshot_as_of=_profile_suite().WINDOW_END),
    ),
    "source_snapshot_storage_policy_invalid": (
        "snapshot_storage_policy_invalid",
        lambda: policy.validate_capture_policy_artifact(
            {**_grant_artifact(), "bucket": "other"}, now=NOW
        ),
    ),
}


def test_every_refusal_vector_code_has_a_behavioural_driver():
    assert {code for _vector, _branch, code in REFUSE_VECTORS} <= set(BEHAVIOUR)
    assert set(BEHAVIOUR) >= NEW_CODES


@pytest.mark.parametrize("code", sorted(BEHAVIOUR))
def test_python_policy_layer_is_driven_to_each_refusal(code):
    python_code, driver = BEHAVIOUR[code]
    with pytest.raises(ValueError, match=python_code):
        driver()


def test_reservation_evaluator_accepts_and_accounts_the_grant():
    initial = _reservation()
    assert initial == {
        "cutoff": "2026-09-12",
        "mode": "initial",
        "grant_id": "source_capture_grant_2026_09_v2",
        "reserved_micro_usd": 750000,
        "ceiling_micro_usd": 2250000,
    }
    third = _reservation(
        cutoff="2026-09-14",
        ledger={"slot_initial_count": 0, "slot_recovery_count": 0, "reserved_initial_count": 2},
    )
    assert third["reserved_micro_usd"] == 2250000
    recover = _reservation(
        mode="recover",
        recovery={"contract_version": "open_intelligence_source_capture_recovery_v3"},
        ledger={"slot_initial_count": 1, "slot_recovery_count": 1, "reserved_initial_count": 3},
    )
    assert recover["reserved_micro_usd"] == 2250000
    with pytest.raises(ValueError, match="source_snapshot_recovery_invalid"):
        _reservation(mode="recover", recovery={"contract_version": "other"})
    with pytest.raises(ValueError, match="source_snapshot_arguments_invalid"):
        _reservation(mode="replay")
    with pytest.raises(ValueError, match="source_snapshot_reservation_ledger_invalid"):
        _reservation(ledger={"slot_initial_count": -1})
