import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ROUTINES = ROOT / "engine/infra/bigquery_routines"
SCHEMAS = ROOT / "engine/infra/bigquery_schemas"

DAILY_SIGNATURES = {
    "sp_derive_open_intelligence_daily_execution_v1.sql": [
        "canonical_manifest_json STRING",
        "manifest_sha256 STRING",
        "canonical_operation_context_json STRING",
        "operation_context_sha256 STRING",
        "canonical_operation_artifact_set_json STRING",
        "authorizing_grant_digest STRING",
    ],
    "sp_select_open_intelligence_daily_derivation_v1.sql": [
        "child_job_resource STRING",
        "child_image_uri STRING",
        "authorizing_grant_digest STRING",
        "child_job_policy_digest STRING",
    ],
    "sp_consume_open_intelligence_daily_derivation_v1.sql": [
        "derivation_id STRING",
        "execution_name STRING",
        "canonical_operation_context_json STRING",
        "canonical_operation_artifact_set_json STRING",
        "canonical_execution_observation_json STRING",
        "execution_observation_sha256 STRING",
    ],
    "sp_cancel_open_intelligence_daily_derivation_v1.sql": [
        "derivation_id STRING",
        "reason_code STRING",
        "canonical_reconciliation_json STRING",
        "reconciliation_digest STRING",
    ],
    "sp_reconcile_open_intelligence_daily_consumption_v1.sql": [
        "derivation_id STRING",
        "consumption_id STRING",
        "execution_name STRING",
        "execution_terminal_state STRING",
        "canonical_reconciliation_json STRING",
        "reconciliation_digest STRING",
    ],
    "sp_read_open_intelligence_daily_derivation_v1.sql": ["derivation_id STRING"],
    "sp_read_open_intelligence_daily_chain_v1.sql": [
        "derivation_id STRING",
        "canonical_execution_observation_json STRING",
        "execution_observation_sha256 STRING",
    ],
    "sp_record_open_intelligence_daily_result_v1.sql": [
        "derivation_id STRING",
        "consumption_id STRING",
        "canonical_payload_json STRING",
        "payload_digest STRING",
        "execution_observation_sha256 STRING",
        "canonical_stage_metering_json STRING",
        "result_reference STRING",
        "terminal_state STRING",
        "effect_state STRING",
        "spend_state STRING",
    ],
}


def normalized(value):
    return re.sub(r"\s+", " ", value)


def test_consumption_returns_json_parts_for_both_insert_and_reissue():
    sql = (ROUTINES / "sp_consume_open_intelligence_daily_derivation_v1.sql").read_text()
    projections = re.findall(
        r"SELECT TO_JSON\(d\) AS derivation,(.*?)WHERE c\.consumption_id=v_consumption_id;",
        sql,
        re.DOTALL,
    )
    assert len(projections) == 2
    expected = {"authorizing_approval", "grant", "manifest", "operation_context", "consumption"}
    for projection in projections:
        fields = projection.split("FROM `", 1)[0]
        assert set(re.findall(r"\bAS (\w+)", fields)) == expected
        assert "TO_JSON(c) AS consumption" in fields
        assert "TO_JSON(a) AS authorizing_approval" in fields


def test_result_returns_json_physical_row_for_insert_and_reissue():
    sql = (ROUTINES / "sp_record_open_intelligence_daily_result_v1.sql").read_text()
    assert len(re.findall(r"SELECT TO_JSON\(r\) AS result FROM", sql)) == 2
    assert len(re.findall(r"COMMIT TRANSACTION;\s+SELECT TO_JSON\(r\) AS result", sql)) == 2
    assert "SELECT * FROM" not in sql


def test_result_envelope_keeps_canonical_payload_and_metering_bytes():
    sql = (ROUTINES / "sp_record_open_intelligence_daily_result_v1.sql").read_text()
    expression = sql.split("SET v_envelope=", 1)[1].split(";", 1)[0]
    assert expression.startswith("CONCAT(")
    assert "',\"operation_payload\":', canonical_payload_json" in expression
    assert "',\"stage_metering\":', canonical_stage_metering_json" in expression
    keys = re.findall(r'\'[,\{]?"([a-z0-9_]+)":\'', expression)
    assert len(keys) == 21
    assert keys == sorted(keys)


def test_daily_routine_signatures_and_common_lock_are_exact():
    assert {path.name for path in ROUTINES.glob("sp_*open_intelligence_daily*_v1.sql")} == set(
        DAILY_SIGNATURES
    )
    for name, parameters in DAILY_SIGNATURES.items():
        text = (ROUTINES / name).read_text(encoding="utf-8")
        header = normalized(text.split(")\nBEGIN", 1)[0])
        positions = [header.index(parameter) for parameter in parameters]
        assert positions == sorted(positions)
        if any(token in name for token in ("derive", "consume", "cancel", "result", "reconcile")):
            assert "open_intelligence_execution_approval_lock_v2" in text
            assert "BEGIN TRANSACTION" in text


def test_grant_routines_bind_actual_actor_and_exact_phrases():
    approve = (ROUTINES / "sp_approve_open_intelligence_recurring_grant_v2.sql").read_text(
        encoding="utf-8"
    )
    disable = (ROUTINES / "sp_disable_open_intelligence_recurring_grant_v2.sql").read_text(
        encoding="utf-8"
    )
    reader = (ROUTINES / "sp_read_open_intelligence_recurring_grant_v2.sql").read_text(
        encoding="utf-8"
    )
    assert "SESSION_USER()" in approve
    assert "$.issuing_principal') = v_actor" in approve
    assert "Approve recurring execution grant v2 %s" in approve
    assert "Disable recurring execution grant v2 %s" in disable
    assert "recurring_grant_revocation_v2" in disable
    assert "'revoked'" in reader
    assert "'not_yet_valid'" in reader
    assert "'expired'" in reader
    assert (
        "INSERT INTO `{project}.{dataset}.open_intelligence_execution_approvals_v2`\n    ("
        in approve
    )
    assert (
        "INSERT INTO `{project}.{dataset}.open_intelligence_execution_approvals_v2`\n    ("
        in disable
    )


def test_new_schema_columns_match_frozen_contract():
    derivation = (SCHEMAS / "open_intelligence_execution_derivations_v1.sql").read_text(
        encoding="utf-8"
    )
    tombstone = (SCHEMAS / "open_intelligence_execution_derivation_tombstones_v1.sql").read_text(
        encoding="utf-8"
    )
    expected_derivation = [
        "derivation_contract_version",
        "derivation_id",
        "authorizing_approval_id",
        "authorizing_grant_digest",
        "manifest_version",
        "operation",
        "contract_sha256",
        "manifest_sha256",
        "canonical_manifest_json",
        "operation_context_sha256",
        "canonical_operation_context_json",
        "business_attempt_id",
        "child_job_resource",
        "child_service_identity",
        "child_image_uri",
        "child_job_policy_digest",
        "derived_by",
        "origin_registry_sha256",
        "resource_manifest_sha256",
        "cost_policy_sha256",
        "derived_at",
        "expires_at",
        "reserved_micro_usd",
    ]
    expected_tombstone = [
        "tombstone_contract_version",
        "tombstone_id",
        "derivation_id",
        "authorizing_approval_id",
        "authorizing_grant_digest",
        "manifest_sha256",
        "operation_context_sha256",
        "business_attempt_id",
        "child_job_resource",
        "reason_code",
        "reconciliation_digest",
        "cancelled_by",
        "origin_registry_sha256",
        "resource_manifest_sha256",
        "cancelled_at",
    ]
    assert (
        re.findall(r"^  ([a-z0-9_]+) (?:STRING|TIMESTAMP|INT64) NOT NULL", derivation, re.M)
        == expected_derivation
    )
    assert (
        re.findall(r"^  ([a-z0-9_]+) (?:STRING|TIMESTAMP|INT64) NOT NULL", tombstone, re.M)
        == expected_tombstone
    )


def test_consume_hashes_complete_artifacts_and_observation_before_explicit_insert():
    text = (ROUTINES / "sp_consume_open_intelligence_daily_derivation_v1.sql").read_text(
        encoding="utf-8"
    )
    assert "FROM_BASE64" in text
    assert "daily_artifact_manifest_mismatch" in text
    assert all(name in text for name in ("capture_plan", "storage_policy", "recovery_context"))
    assert "daily_native_execution_observation_v1" in text
    assert "execution_observation_sha256" in text
    assert (
        "INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v2`\n    ("
        in text
    )
    assert "open_intelligence_execution_consumption_v3" in text


DAILY_COLLECTION_CREDIT_CAP = """\
  ASSERT v_derivation.operation!='daily_source_collection' OR (
    SELECT SAFE_CAST(JSON_VALUE(v_derivation.canonical_manifest_json,'$.limits.max_credits') AS INT64)
      + IFNULL(SUM(SAFE_CAST(JSON_VALUE(d.canonical_manifest_json,'$.limits.max_credits') AS INT64)),0)
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` r ON r.consumption_id=c.consumption_id
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` t
      ON t.derivation_id=d.derivation_id AND t.reason_code='owner_reconciliation_hold'
    WHERE d.operation='daily_source_collection' AND r.consumption_id IS NULL AND t.derivation_id IS NULL
  )<=620 AS 'daily_collection_credit_exhausted';
"""


def test_daily_collection_credit_cap_counts_in_flight_by_the_store_result_row():
    # A daily collection is in flight while its consumption has no result row,
    # which the result routine writes at the end of every daily run, and no owner
    # reconciliation hold, which the owner writes once the execution is observed
    # terminal without a result. The cap reads the store's own record of
    # completion and no table outside the store.
    text = (ROUTINES / "sp_consume_open_intelligence_daily_derivation_v1.sql").read_text(
        encoding="utf-8"
    )
    assert text.count("AS 'daily_collection_credit_exhausted';") == 1
    start = text.index("  ASSERT v_derivation.operation!='daily_source_collection' OR (")
    end = text.index("AS 'daily_collection_credit_exhausted';\n") + len(
        "AS 'daily_collection_credit_exhausted';\n"
    )
    assert text[start:end] == DAILY_COLLECTION_CREDIT_CAP
    assert "socialcrawl_funded_terminal_events_v1" not in text
    record = (ROUTINES / "sp_record_open_intelligence_daily_result_v1.sql").read_text(
        encoding="utf-8"
    )
    assert (
        "INSERT INTO `{project}.{dataset}.open_intelligence_execution_results_v2`\n"
        "    (result_contract_version,result_id,consumption_id,"
    ) in record
    schema = (SCHEMAS / "open_intelligence_execution_results_v2.sql").read_text(encoding="utf-8")
    assert "  consumption_id STRING NOT NULL" in schema


def test_result_metering_and_retry_fields_are_bound():
    text = (ROUTINES / "sp_record_open_intelligence_daily_result_v1.sql").read_text(
        encoding="utf-8"
    )
    for name in (
        "query_count",
        "total_bytes_billed",
        "storage_write_count",
        "storage_write_bytes",
        "vendor_credits",
        "model_calls",
        "complete",
    ):
        assert name in text
    assert "daily_result_observation_invalid" in text
    assert "SESSION_USER()=v_derivation.child_service_identity" in text
    assert "open_intelligence_execution_result_v3" in text
    assert "INSERT INTO `{project}.{dataset}.open_intelligence_execution_results_v2`\n    (" in text


def test_cancellation_sql_rejects_untyped_or_unknown_provider_state():
    text = (ROUTINES / "sp_cancel_open_intelligence_daily_derivation_v1.sql").read_text(
        encoding="utf-8"
    )
    assert "daily_dispatch_reconciliation_v1" in text
    assert "provider_terminal_no_execution" in text
    assert "done_no_execution" in text
    assert "owner_reconciliation_hold" not in text
    assert "JSON_VALUE(canonical_reconciliation_json,'$.observer_principal')=SESSION_USER()" in text


def _json_value(raw, path):
    value = json.loads(raw) if raw is not None else None
    for key in path.removeprefix("$.").split("."):
        value = value.get(key) if isinstance(value, dict) else None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if isinstance(value, (str, int, float)) else None


def test_actual_unresolved_predicate_allows_settled_known_result_only():
    text = (ROUTINES / "sp_derive_open_intelligence_daily_execution_v1.sql").read_text(
        encoding="utf-8"
    )
    predicate = text.split("/* daily_unresolved_predicate_begin */", 1)[1].split(
        "/* daily_unresolved_predicate_end */", 1
    )[0]
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _json_value)
        connection.executescript(
            "CREATE TABLE d(id); CREATE TABLE t(derivation_id,reason_code); CREATE TABLE c(consumption_id); CREATE TABLE r(result_id,canonical_result_json);"
        )
        connection.execute("INSERT INTO d VALUES (1)")

        def blocks(*, tombstone=False, consumption=False, result=None):
            for table in ("t", "c", "r"):
                connection.execute(f"DELETE FROM {table}")
            if tombstone:
                connection.execute("INSERT INTO t VALUES ('t','dispatch_not_attempted')")
            if consumption:
                connection.execute("INSERT INTO c VALUES ('c')")
            if result is not None:
                connection.execute("INSERT INTO r VALUES ('r',?)", (json.dumps(result),))
            query = f"SELECT COUNT(*) FROM d LEFT JOIN t ON 1=1 LEFT JOIN c ON 1=1 LEFT JOIN r ON 1=1 WHERE ({predicate}"
            return connection.execute(query).fetchone()[0] == 1

        assert blocks()
        assert not blocks(tombstone=True)
        assert blocks(consumption=True)
        assert not blocks(
            consumption=True,
            result={
                "effect_state": "effects_recorded",
                "spend_state": "measured",
                "stage_metering": {"complete": True},
            },
        )
        assert blocks(
            consumption=True, result={"effect_state": "unknown", "spend_state": "measured"}
        )
        assert blocks(
            consumption=True, result={"effect_state": "effects_recorded", "spend_state": "unknown"}
        )
        assert blocks(
            consumption=True,
            result={
                "effect_state": "effects_recorded",
                "spend_state": "measured",
                "stage_metering": {"complete": False},
            },
        )
        assert blocks(
            consumption=True, result={"effect_state": "effects_recorded", "spend_state": "measured"}
        )


def test_actual_result_metering_state_predicate_checks_incomplete_and_all_cost_measures():
    text = (ROUTINES / "sp_record_open_intelligence_daily_result_v1.sql").read_text(
        encoding="utf-8"
    )
    predicate = text.split("/* daily_metering_state_predicate_begin */", 1)[1].split(
        "/* daily_metering_state_predicate_end */", 1
    )[0]
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _json_value)
        connection.execute(
            "CREATE TABLE inputs(canonical_stage_metering_json,terminal_state,effect_state,spend_state)"
        )
        zero = {
            "complete": True,
            "query_count": 0,
            "total_bytes_billed": 0,
            "storage_write_count": 0,
            "storage_write_bytes": 0,
            "vendor_credits": "0",
            "model_calls": 0,
        }

        def allows(metering, effect="no_effect", spend="no_spend"):
            connection.execute("DELETE FROM inputs")
            connection.execute(
                "INSERT INTO inputs VALUES (?, 'failed', ?, ?)",
                (json.dumps(metering), effect, spend),
            )
            return connection.execute(f"SELECT ({predicate}) FROM inputs").fetchone()[0] == 1

        assert allows(zero)
        assert not allows(dict(zero, total_bytes_billed=1))
        assert not allows(dict(zero, storage_write_bytes=1))
        assert not allows(dict(zero, complete=False), effect="effects_recorded", spend="measured")
        assert allows(dict(zero, complete=False), effect="effects_recorded", spend="unknown")


def test_actual_observation_upper_bound_refuses_future_before_insert():
    from datetime import datetime

    text = (ROUTINES / "sp_consume_open_intelligence_daily_derivation_v1.sql").read_text(
        encoding="utf-8"
    )
    predicate = next(
        line.strip()[4:] for line in text.splitlines() if "'$.observed_at'))<=v_now" in line
    )
    assert text.index(predicate) < text.index("INSERT INTO")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _json_value)
        connection.create_function(
            "TIMESTAMP", 1, lambda value: datetime.fromisoformat(value).timestamp()
        )
        connection.execute("CREATE TABLE inputs(canonical_execution_observation_json,v_now)")
        connection.execute(
            "INSERT INTO inputs VALUES (?, ?)",
            (
                json.dumps({"observed_at": "2099-01-01T00:00:00+00:00"}),
                datetime.fromisoformat("2026-09-14T00:02:00+00:00").timestamp(),
            ),
        )
        assert connection.execute(f"SELECT ({predicate}) FROM inputs").fetchone()[0] == 0


def test_retained_authority_source_bytes_are_unchanged():
    expected = {
        "engine/infra/bigquery_routines/sp_consume_open_intelligence_execution_v2.sql": "100fd7b717d441c6b130e5e112c508ab6d9e7a12315d2c0863b63e8b37cd38fc",
        "engine/infra/bigquery_routines/sp_consume_open_intelligence_source_snapshot_v2.sql": "4432d55a8f53ad160139ed84244581c3e9d39dc06585092fe6d81670778fbc4d",
        # The approved native reply repair binds omitted consumption fields and
        # checks successful result writes through the durable result reader.
        "engine/src/analysis/open_intelligence/execution_approval.py": "a589ef3200b70ab1c0a8bdc4d8f354ed136ea038457508a7484a0973a226dc8c",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


DAILY_COLLECTION_MONTHLY_CREDIT_CAP = """\
  ASSERT v_derivation.operation!='daily_source_collection' OR (
    SELECT SAFE_CAST(JSON_VALUE(v_derivation.canonical_manifest_json,'$.limits.max_credits') AS INT64)
      + IFNULL(SUM(/* daily_collection_credit_charge_begin */CASE
        WHEN JSON_VALUE(r.canonical_result_json,'$.spend_state') IN ('measured','no_spend')
          AND JSON_VALUE(r.canonical_result_json,'$.stage_metering.complete')='true'
          AND SAFE_CAST(JSON_VALUE(r.canonical_result_json,'$.stage_metering.vendor_credits') AS NUMERIC) IS NOT NULL
        THEN CAST(CEIL(SAFE_CAST(JSON_VALUE(r.canonical_result_json,'$.stage_metering.vendor_credits') AS NUMERIC)) AS INT64)
        ELSE SAFE_CAST(JSON_VALUE(d.canonical_manifest_json,'$.limits.max_credits') AS INT64)
      END/* daily_collection_credit_charge_end */),0)
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` r ON r.consumption_id=c.consumption_id
    WHERE d.operation='daily_source_collection' AND d.authorizing_grant_digest=v_derivation.authorizing_grant_digest
      AND TIMESTAMP_TRUNC(c.consumed_at,MONTH)=TIMESTAMP_TRUNC(v_now,MONTH)
  )<=(SELECT SAFE_CAST(JSON_VALUE(a.canonical_manifest_json,'$.monthly_credit_cap') AS INT64)
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` a
    WHERE a.operation='recurring_grant_v2' AND a.manifest_sha256=v_derivation.authorizing_grant_digest)
    AS 'daily_collection_monthly_credit_exhausted';
"""


def test_daily_collection_monthly_credit_cap_charges_every_consumption_this_month():
    # The in flight cap stops counting a consumption once it has a result or an owner
    # hold, so the grant's monthly_credit_cap bounds the month: every collection
    # consumed under the same grant in the current UTC month is charged, a measured
    # result at its measured credits and anything else (in flight, held, unknown or
    # incomplete) at its manifest max_credits. The cap is read from the grant row in
    # the store, never from the request.
    text = (ROUTINES / "sp_consume_open_intelligence_daily_derivation_v1.sql").read_text(
        encoding="utf-8"
    )
    assert text.count("AS 'daily_collection_monthly_credit_exhausted';") == 1
    assert text.count(DAILY_COLLECTION_MONTHLY_CREDIT_CAP) == 1
    start = text.index(DAILY_COLLECTION_MONTHLY_CREDIT_CAP)
    assert (
        text.index(DAILY_COLLECTION_CREDIT_CAP)
        < start
        < text.index(
            "INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v2`"
        )
    )


def test_actual_monthly_credit_charge_uses_measured_credits_only_when_complete():
    import math

    text = (ROUTINES / "sp_consume_open_intelligence_daily_derivation_v1.sql").read_text(
        encoding="utf-8"
    )
    charge = text.split("/* daily_collection_credit_charge_begin */", 1)[1].split(
        "/* daily_collection_credit_charge_end */", 1
    )[0]
    # sqlite has CAST but not SAFE_CAST; every value below casts cleanly, so the
    # substitution changes no branch.
    charge = charge.replace("SAFE_CAST(", "CAST(")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _json_value)
        connection.create_function("CEIL", 1, lambda value: math.ceil(float(value)))
        connection.execute("CREATE TABLE d(canonical_manifest_json)")
        connection.execute("CREATE TABLE r(canonical_result_json)")
        connection.execute(
            "INSERT INTO d VALUES (?)", (json.dumps({"limits": {"max_credits": 620}}),)
        )

        def charged(result):
            connection.execute("DELETE FROM r")
            connection.execute(
                "INSERT INTO r VALUES (?)", (None if result is None else json.dumps(result),)
            )
            return connection.execute(f"SELECT {charge} FROM d, r").fetchone()[0]

        measured = {"spend_state": "measured", "stage_metering": {"complete": True}}
        assert charged(None) == 620
        assert (
            charged({**measured, "stage_metering": {"complete": True, "vendor_credits": "12.5"}})
            == 13
        )
        assert (
            charged({**measured, "stage_metering": {"complete": True, "vendor_credits": "700"}})
            == 700
        )
        assert (
            charged(
                {
                    "spend_state": "no_spend",
                    "stage_metering": {"complete": True, "vendor_credits": "0"},
                }
            )
            == 0
        )
        assert (
            charged(
                {
                    "spend_state": "unknown",
                    "stage_metering": {"complete": True, "vendor_credits": "5"},
                }
            )
            == 620
        )
        assert (
            charged(
                {
                    "spend_state": "measured",
                    "stage_metering": {"complete": False, "vendor_credits": "5"},
                }
            )
            == 620
        )


def _monthly_cap_predicate():
    text = (ROUTINES / "sp_consume_open_intelligence_daily_derivation_v1.sql").read_text(
        encoding="utf-8"
    )
    end = text.index("\n    AS 'daily_collection_monthly_credit_exhausted';")
    start = text.rindex("  ASSERT v_derivation.operation!='daily_source_collection' OR (\n", 0, end)
    predicate = text[start + len("  ASSERT ") : end]
    # Only the dialect is translated: table names lose their template prefix, the casts
    # become functions with BigQuery's semantics, and MONTH becomes a literal part.
    predicate = re.sub(r"`\{project\}\.\{dataset\}\.([a-z0-9_]+)`", r"\1", predicate)
    predicate = re.sub(r"(?<!SAFE_)CAST\(", "HARD_CAST(", predicate)
    predicate = predicate.replace(" AS INT64)", ",'INT64')").replace(" AS NUMERIC)", ",'NUMERIC')")
    return predicate.replace(",MONTH)", ",'MONTH')")


def _bigquery_cast(value, kind, *, safe):
    from decimal import Decimal, InvalidOperation

    if value is None:
        return None
    if isinstance(value, int):
        return value
    if kind == "INT64":
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            return int(value)
    elif kind == "NUMERIC":
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            number = None
        if number is not None and number.is_finite():
            return float(number)
    if safe:
        return None
    raise ValueError(f"cannot cast {value!r} to {kind}")


def _month(value, part):
    from datetime import datetime

    assert part == "MONTH"
    instant = datetime.fromisoformat(value)
    return f"{instant.year:04d}-{instant.month:02d}"


GRANT_DIGEST = "e" * 64
NOW = "2026-09-23T06:00:00+00:00"


def _monthly_cap_allows(
    *, history=(), cap=1000, max_credits=200, operation="daily_source_collection", grants=None
):
    import math

    predicate = _monthly_cap_predicate()
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _json_value)
        connection.create_function("CEIL", 1, lambda value: math.ceil(value))
        connection.create_function(
            "SAFE_CAST", 2, lambda value, kind: _bigquery_cast(value, kind, safe=True)
        )
        connection.create_function(
            "HARD_CAST", 2, lambda value, kind: _bigquery_cast(value, kind, safe=False)
        )
        connection.create_function("TIMESTAMP_TRUNC", 2, _month)
        connection.executescript(
            """
            CREATE TABLE v_derivation(operation, canonical_manifest_json, authorizing_grant_digest);
            CREATE TABLE procedure_variables(v_now);
            CREATE TABLE open_intelligence_execution_derivations_v1(
              derivation_id, operation, authorizing_grant_digest, canonical_manifest_json);
            CREATE TABLE open_intelligence_execution_consumptions_v2(
              consumption_id, approval_id, consumed_at);
            CREATE TABLE open_intelligence_execution_results_v2(
              consumption_id, canonical_result_json);
            CREATE TABLE open_intelligence_execution_approvals_v2(
              operation, manifest_sha256, canonical_manifest_json);
            """
        )
        # The derivation being consumed carries its own manifest, and a cap of its own
        # that the routine must never read.
        connection.execute(
            "INSERT INTO v_derivation VALUES (?, ?, ?)",
            (
                operation,
                json.dumps({"limits": {"max_credits": max_credits}, "monthly_credit_cap": 10**9}),
                GRANT_DIGEST,
            ),
        )
        connection.execute("INSERT INTO procedure_variables VALUES (?)", (NOW,))
        if grants is None:
            grants = [("recurring_grant_v2", GRANT_DIGEST, {"monthly_credit_cap": cap})]
        for grant_operation, digest, manifest in grants:
            connection.execute(
                "INSERT INTO open_intelligence_execution_approvals_v2 VALUES (?, ?, ?)",
                (grant_operation, digest, json.dumps(manifest)),
            )
        for index, row in enumerate(history):
            connection.execute(
                "INSERT INTO open_intelligence_execution_derivations_v1 VALUES (?, ?, ?, ?)",
                (
                    f"d{index}",
                    row.get("operation", "daily_source_collection"),
                    row.get("grant", GRANT_DIGEST),
                    json.dumps({"limits": {"max_credits": row.get("max_credits", 200)}}),
                ),
            )
            connection.execute(
                "INSERT INTO open_intelligence_execution_consumptions_v2 VALUES (?, ?, ?)",
                (f"c{index}", f"d{index}", row.get("consumed_at", "2026-09-10T06:00:00+00:00")),
            )
            if row.get("result") is not None:
                connection.execute(
                    "INSERT INTO open_intelligence_execution_results_v2 VALUES (?, ?)",
                    (f"c{index}", json.dumps(row["result"])),
                )
        value = connection.execute(
            f"SELECT ({predicate}) FROM v_derivation, procedure_variables"
        ).fetchone()[0]
    # ASSERT passes only on TRUE; FALSE and NULL both refuse.
    assert value in (0, 1, None)
    return value == 1


def _measured(credits):
    return {
        "spend_state": "measured",
        "stage_metering": {"complete": True, "vendor_credits": credits},
    }


def test_actual_monthly_credit_cap_admits_up_to_the_cap_and_refuses_past_it():
    in_flight = {}
    assert _monthly_cap_allows()
    # Four in flight at 200 plus this one at 200 reach 1000 exactly: admitted.
    assert _monthly_cap_allows(history=[in_flight] * 4)
    # A fifth in flight would take the month to 1200.
    assert not _monthly_cap_allows(history=[in_flight] * 5)
    # One collection alone above the cap is refused on an empty month.
    assert not _monthly_cap_allows(max_credits=1001)
    assert _monthly_cap_allows(max_credits=1000)


def test_actual_monthly_credit_cap_charges_measured_results_rounded_up_and_the_rest_at_max():
    # Measured credits replace max_credits once metering is complete.
    assert _monthly_cap_allows(history=[{"result": _measured("10.2")}] * 20)
    assert _monthly_cap_allows(history=[{"result": _measured("200")}] * 4)
    # 200.4 rounds up to 201, so four of them and this one reach 1004.
    assert not _monthly_cap_allows(history=[{"result": _measured("200.4")}] * 4)
    # Unknown spend, incomplete metering and a missing figure are charged at max.
    unknown = {"spend_state": "unknown", "stage_metering": {"complete": True}}
    incomplete = {
        "spend_state": "measured",
        "stage_metering": {"complete": False, "vendor_credits": "1"},
    }
    for result in (unknown, incomplete, _measured(None)):
        assert _monthly_cap_allows(history=[{"result": result}] * 4)
        assert not _monthly_cap_allows(history=[{"result": result}] * 5)
    # A held consumption has no result row and stays charged at its max_credits.
    assert not _monthly_cap_allows(history=[{"result": _measured("1")}] * 20 + [{}] * 5)


def test_actual_monthly_credit_cap_counts_only_this_grant_this_month_and_collection():
    five = 5
    assert not _monthly_cap_allows(history=[{"consumed_at": "2026-09-01T00:00:00+00:00"}] * five)
    assert _monthly_cap_allows(history=[{"consumed_at": "2026-08-31T23:59:59+00:00"}] * five)
    assert _monthly_cap_allows(history=[{"consumed_at": "2025-09-23T06:00:00+00:00"}] * five)
    assert _monthly_cap_allows(history=[{"grant": "f" * 64}] * five)
    assert _monthly_cap_allows(history=[{"operation": "daily_source_snapshot_capture"}] * five)
    # A stage other than collection is not charged against the credit cap at all.
    assert _monthly_cap_allows(history=[{}] * five, operation="daily_source_snapshot_capture")


def test_actual_monthly_credit_cap_is_read_from_the_stored_grant_and_refuses_without_one():
    # The derivation's own manifest names a huge cap; only the grant row counts.
    assert not _monthly_cap_allows(history=[{}] * 5, cap=1000)
    assert _monthly_cap_allows(history=[{}] * 5, cap=1200)
    # No grant row, a grant of another digest, a row that is not a recurring grant, and
    # a grant without a usable cap all evaluate NULL and refuse.
    for grants in (
        [],
        [("recurring_grant_v2", "f" * 64, {"monthly_credit_cap": 10**6})],
        [("daily_source_collection", GRANT_DIGEST, {"monthly_credit_cap": 10**6})],
        [("recurring_grant_v2", GRANT_DIGEST, {})],
        [("recurring_grant_v2", GRANT_DIGEST, {"monthly_credit_cap": "a lot"})],
        [("recurring_grant_v2", GRANT_DIGEST, {"monthly_credit_cap": 1000.5})],
    ):
        assert not _monthly_cap_allows(grants=grants)
