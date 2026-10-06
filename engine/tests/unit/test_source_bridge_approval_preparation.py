"""Unissued v3 preparation fixtures preserve retained execution authority."""

import copy
import hashlib
import inspect
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_approval as approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.source_estate_bridge_plan import build_bridge_plan

from tests.unit import test_daily_runtime_walls as walls
from tests.unit import test_execution_runtime_v2 as native
from tests.unit.test_daily_runtime_walls import capture_generation
from tests.unit.test_open_intelligence_execution_approval_contract import _capture_rule
from tests.unit.test_source_estate_bridge_evidence import artifacts
from tests.unit.test_source_estate_bridge_plan import inputs

ROOT = Path(__file__).parents[2]
ROUTINES = ROOT / "infra/bigquery_routines"
SQL = ROUTINES / "sp_consume_open_intelligence_source_snapshot_v3.sql"
RETAINED_SQL = ROUTINES / "sp_consume_open_intelligence_source_snapshot_v2.sql"
V2_ROUTINE = "sp_consume_open_intelligence_source_snapshot_v2"
V3_ROUTINE = "sp_consume_open_intelligence_source_snapshot_v3"
PLAN_V2 = "open_intelligence_protected_capture_plan_v2"
PLAN_V3 = "open_intelligence_protected_capture_plan_v3"
PINS = {
    "bridge_policy": "bridge_policy_digest",
    "temporal_rules": "temporal_rules_digest",
    "collection_receipt_set": "collection_receipt_set_digest",
    "history_completion_set": "history_completion_set_digest",
}


def fixture():
    source = inputs()
    extra = artifacts()
    for name, field in PINS.items():
        source["profile"][field] = canonical_digest(extra[name])
    plan = build_bridge_plan(**source)
    rule = copy.deepcopy(_capture_rule()["arguments"])
    rule["plan_contract_version"] = plan["contract_version"]
    return source, extra, plan, rule


def build(source, extra, plan, rule):
    return approval.build_source_snapshot_capture_arguments_v2(
        plan,
        grant=source["storage_policy"]["grant"],
        mode="initial",
        now=source["now"],
        rule=rule,
        source_metadata=source["source_metadata"],
        storage_policy=source["storage_policy"],
        bridge_artifacts=extra,
    )


def test_v3_preparation_regenerates_exact_plan_without_issuing_authority():
    source, extra, plan, rule = fixture()
    vector = build(source, extra, plan, rule)
    assert vector == (
        "scripts/staging/capture_protected_production_snapshot.py",
        "--cutoff-date",
        plan["cutoff_date"],
        "--mode",
        "initial",
        "--grant",
        source["profile"]["grant_id"],
    )
    assert (
        approval.read_source_snapshot_capture_arguments_v2(
            vector, rule=rule
        ).cutoff_date.isoformat()
        == source["cutoff_date"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "old_rule",
        "unknown_rule",
        "sql_rehash",
        "metadata",
        "profile_pin",
        "artifact",
        "storage",
        "grant",
    ],
)
def test_v3_preparation_refuses_mismatches(mutation):
    source, extra, plan, rule = fixture()
    if mutation == "old_rule":
        rule["plan_contract_version"] = "open_intelligence_protected_capture_plan_v2"
    elif mutation == "unknown_rule":
        rule["plan_contract_version"] = "unsupported"
    elif mutation == "sql_rehash":
        statement = plan["creation_statements"][0]
        statement["sql"] = "SELECT 1"
        statement["sql_digest"] = hashlib.sha256(b"SELECT 1").hexdigest()
    elif mutation == "metadata":
        next(iter(source["source_metadata"]["tables"].values()))["schema"]["fields"][0]["type"] = (
            "STRING"
        )
    elif mutation == "profile_pin":
        plan["snapshot_plan"]["temporal_rules_digest"] = "f" * 64
    elif mutation == "artifact":
        extra["temporal_rules"]["extra"] = True
    elif mutation == "storage":
        source["storage_policy"]["grant"]["source_estate_digest"] = "f" * 64
    else:
        source["storage_policy"]["grant"]["grant_id"] = "other_grant"
    with pytest.raises(approval.ApprovalRefusal):
        build(source, extra, plan, rule)


def test_v3_preparation_reads_the_estate_from_the_storage_policy_grant():
    """The estate the profile names is compared with the one the storage grant carries;
    the grant id alone never stands in for it."""
    source, extra, plan, rule = fixture()
    grant = source["storage_policy"]["grant"]
    grant["source_estate_digest"] = "e" * 64
    with pytest.raises(approval.ApprovalRefusal, match=r"^source_bridge_estate_differs$"):
        build(source, extra, plan, rule)


def test_v3_preparation_binds_the_parsed_artifacts_into_the_profile():
    """The profile check recomputes every artifact digest itself, so a history set whose
    last day is not the cutoff day refuses even when its digest is pinned."""
    source, extra, plan, rule = fixture()
    last = max(entry["product_date"] for entry in extra["history_completion_set"]["entries"])
    extra["history_completion_set"]["entries"] = [
        entry
        for entry in extra["history_completion_set"]["entries"]
        if entry["product_date"] != last
    ]
    digest = canonical_digest(extra["history_completion_set"])
    plan["snapshot_plan"]["history_completion_set_digest"] = digest
    with pytest.raises(approval.ApprovalRefusal, match=r"^source_bridge_history_window_invalid$"):
        build(source, extra, plan, rule)


def test_v3_cannot_use_v2_builder_without_comparison_context():
    source, _, plan, rule = fixture()
    with pytest.raises(approval.ApprovalRefusal):
        approval.build_source_snapshot_capture_arguments_v2(
            plan,
            grant=source["storage_policy"]["grant"],
            mode="initial",
            now=source["now"],
            rule=rule,
        )


def test_v3_pinned_bytes_are_checked_before_preparation():
    source, extra, plan, rule = fixture()
    contents = {
        "capture_plan": canonical_bytes(plan),
        "source_metadata": canonical_bytes(source["source_metadata"]),
        "storage_policy": canonical_bytes(source["storage_policy"]),
        **{name: canonical_bytes(value) for name, value in extra.items()},
    }
    expected = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}
    actual = approval.prepare_source_snapshot_capture_v3(
        expected, artifact_reader=contents.__getitem__, rule=rule, now=source["now"]
    )
    assert actual == plan
    contents["source_metadata"] = b"{}"
    with pytest.raises(approval.ApprovalRefusal, match="execution_approval_artifact_mismatch"):
        approval.prepare_source_snapshot_capture_v3(
            expected, artifact_reader=contents.__getitem__, rule=rule, now=source["now"]
        )


def test_sql_keeps_ten_parameters_and_admits_only_the_v3_plan():
    sql = SQL.read_text(encoding="utf-8")
    signature = sql.split("BEGIN", 1)[0]
    assert len(re.findall(r"\bSTRING\b", signature)) == 10
    assert "$.operation_validation.source_snapshot_capture.arguments.plan_contract_version" in sql
    # The retained v2 routine consumes v2 plans; this routine has no v2 branch at all.
    assert (
        "ASSERT v_plan_version = 'open_intelligence_protected_capture_plan_v3' "
        "AS 'source_snapshot_plan_invalid';"
    ) in sql
    assert "open_intelligence_protected_capture_plan_v2" not in sql
    assert "open_intelligence_protected_source_snapshot_v2" not in sql
    assert "IF v_plan_version" not in sql
    for field in PINS.values():
        assert f"$.snapshot_plan.{field}" in sql
    assert "ARRAY_LENGTH(JSON_QUERY_ARRAY(v_plan, '$.snapshot_plan.relation_bindings')) = 7" in sql
    assert "ARRAY_LENGTH(JSON_QUERY_ARRAY(v_plan, '$.creation_statements')) = 7" in sql
    assert "collection_evidence" in sql
    assert "derived_history" in sql
    assert "LOWER(TO_HEX(SHA256(JSON_VALUE(statement, '$.sql'))))" in sql
    assert "source_snapshot_slot_consumed" in sql
    assert "source_snapshot_grant_exhausted" in sql


def test_input_reader_accepts_four_additional_artifact_names_only():
    from src.analysis.open_intelligence.production_snapshot_storage import _INPUT_NAMES

    assert set(PINS) <= _INPUT_NAMES


def test_acting_consumer_prepares_before_reservation_query():
    source = inspect.getsource(approval)
    body = source.split("    def consume_source_snapshot_authority", 1)[1].split(
        "    def require_consumed_execution", 1
    )[0]
    assert body.index("prepare_source_snapshot_capture_v3(") < body.index(
        "        def write(request):"
    )


@pytest.mark.parametrize("field", ["snapshot_plan", "client_scope_id", "temporal_rules_digest"])
def test_malformed_v3_plan_uses_the_approval_refusal_boundary(field):
    source, extra, plan, rule = fixture()
    if field == "temporal_rules_digest":
        del plan["snapshot_plan"][field]
    else:
        del plan[field]
    with pytest.raises(approval.ApprovalRefusal):
        build(source, extra, plan, rule)


def test_sql_v3_checks_raw_plan_canonical_json():
    sql = SQL.read_text(encoding="utf-8")
    assert "fn_is_canonical_execution_json_v1`(v_capture_plan_json)" in sql


def test_recovery_result_version_is_the_v3_result_contract():
    sql = SQL.read_text(encoding="utf-8")
    assert (
        "AND JSON_VALUE(r.canonical_result_json, '$.contract_version') = "
        "'open_intelligence_protected_source_snapshot_v3'"
    ) in sql


def test_v3_logic_lives_in_its_own_routine_and_the_retained_v2_bytes_are_kept():
    retained = RETAINED_SQL.read_bytes()
    assert hashlib.sha256(retained).hexdigest() == (
        "4432d55a8f53ad160139ed84244581c3e9d39dc06585092fe6d81670778fbc4d"
    )
    assert PLAN_V3.encode() not in retained
    sql = SQL.read_text(encoding="utf-8")
    assert sql.startswith(f"CREATE OR REPLACE PROCEDURE `{{project}}.{{dataset}}.{V3_ROUTINE}`(")
    assert V2_ROUTINE not in sql


def test_v3_routine_refuses_a_policy_that_names_another_routine():
    sql = SQL.read_text(encoding="utf-8")
    assert (
        "ASSERT JSON_VALUE(v_policy_json, '$.operation_validation.source_snapshot_capture"
        f".arguments.consume_routine') = '{V3_ROUTINE}' AS 'source_snapshot_contract_invalid';"
    ) in sql


V3_PARAMETERS = (
    "manifest_sha256",
    "execution_name",
    "job_resource",
    "source_sha",
    "image_uri",
    "capture_plan_json",
    "recovery_context_json",
    "storage_policy_json",
    "origin_registry_sha256",
    "resource_manifest_sha256",
)


def test_v3_routine_is_installed_by_the_v2_plan_but_not_yet_bound_to_capture():
    """Amendment e installs the v3 routine through the v2 SQL installation: one spec row
    with the store writer role and the header's ten STRING parameters in order, and one
    dataset authorization at that role. The active capture binding still consumes the
    retained v2 routine, no service account is bound to v3 here (the amendment e delta
    grants orchestration), and the retained v2 bytes stay installed."""
    from scripts.migrations import create_open_intelligence_execution_approval_store as migration

    specs = {name: (role, parameters) for name, role, parameters in migration.V2_ROUTINE_SPECS}
    assert V2_ROUTINE in specs
    assert specs[V3_ROUTINE] == (
        "roles/bigquery.routineDataEditor",
        tuple((name, "STRING") for name in V3_PARAMETERS),
    )
    assert migration.V2_SOURCE_SNAPSHOT_ROUTINE == V2_ROUTINE
    plan = migration.build_v2_plan()
    routines = [item for item in plan.routines if item.name == V3_ROUTINE]
    assert len(routines) == 1
    assert routines[0].sql == SQL.read_text(encoding="utf-8")
    assert routines[0].sha256 == hashlib.sha256(SQL.read_bytes()).hexdigest()
    resource = f"projects/ogilvy-trends-v2/datasets/{migration.DATASET}/routines/{V3_ROUTINE}"
    assert [
        (item.dataset, item.role)
        for item in plan.iam_plan.routine_authorizations
        if item.routine == resource
    ] == [(f"ogilvy-trends-v2.{migration.DATASET}", "roles/bigquery.routineDataEditor")]
    assert not [
        item for item in plan.iam_plan.principal_routine_bindings if item.resource == resource
    ]
    for group in (
        migration.V2_RUNTIME_ROUTINES,
        migration.V2_DAILY_ROUTINES,
        migration.V2_OPERATOR_ROUTINES,
    ):
        assert V3_ROUTINE not in group


def _policy_rule(plan, routine, digest=None):
    rule = copy.deepcopy(_capture_rule()["arguments"])
    rule["plan_contract_version"] = plan
    rule["consume_routine"] = routine
    rule["consume_routine_sha256"] = (
        digest or hashlib.sha256((ROUTINES / f"{routine}.sql").read_bytes()).hexdigest()
    )
    return rule


@pytest.mark.parametrize("plan,routine", [(PLAN_V2, V2_ROUTINE), (PLAN_V3, V3_ROUTINE)])
def test_capture_routine_is_the_one_the_policy_names(plan, routine):
    assert approval._source_snapshot_routine(_policy_rule(plan, routine)) == routine


@pytest.mark.parametrize(
    "plan,routine",
    [
        (PLAN_V2, V3_ROUTINE),
        (PLAN_V3, V2_ROUTINE),
        (PLAN_V3, "sp_consume_open_intelligence_source_snapshot_v4"),
        (PLAN_V3, "sp_consume_open_intelligence_execution_v2"),
        (PLAN_V3, "../sp_consume_open_intelligence_source_snapshot_v3"),
        ("unsupported", V3_ROUTINE),
    ],
)
def test_capture_routine_outside_the_closed_pairing_refuses(plan, routine):
    rule = copy.deepcopy(_capture_rule()["arguments"])
    rule["plan_contract_version"] = plan
    rule["consume_routine"] = routine
    with pytest.raises(approval.ApprovalRefusal, match="execution_approval_manifest_invalid"):
        approval._source_snapshot_routine(rule)


def test_capture_routine_digest_is_recomputed_from_the_packaged_routine():
    rule = _policy_rule(PLAN_V3, V3_ROUTINE, digest="f" * 64)
    with pytest.raises(approval.ApprovalRefusal, match="execution_approval_manifest_invalid"):
        approval._source_snapshot_routine(rule)
    retained = _policy_rule(PLAN_V2, V2_ROUTINE)
    retained["consume_routine_sha256"] = hashlib.sha256(SQL.read_bytes()).hexdigest()
    with pytest.raises(approval.ApprovalRefusal, match="execution_approval_manifest_invalid"):
        approval._source_snapshot_routine(retained)


@pytest.mark.parametrize(
    "plan,routine",
    [(PLAN_V2, V3_ROUTINE), (PLAN_V3, V2_ROUTINE), (PLAN_V2, "sp_consume_x_v9")],
)
def test_consumer_refuses_a_policy_routine_outside_the_pairing_before_query(
    monkeypatch, request, plan, routine
):
    fx = request.getfixturevalue("capture_generation")
    issued = walls.capture_load(fx)
    original = approval._policy_bytes_for_origin

    def named(**kwargs):
        policy = json.loads(original(**kwargs))
        arguments = policy["operation_validation"]["source_snapshot_capture"]["arguments"]
        arguments.update(plan_contract_version=plan, consume_routine=routine)
        return canonical_bytes(policy)

    monkeypatch.setattr(approval, "_policy_bytes_for_origin", named)
    calls = []
    with native.refusal("execution_approval_manifest_invalid"):
        approval._consume_source_snapshot_authority(
            issued,
            artifact_reader=lambda name: fx.contents[name],
            query=walls.capture_query(fx, calls),
        )
    assert calls == []


POLICY = ROOT / "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"
_DATASET_BEGIN = "/* source_snapshot_dataset_predicate_begin */"
_DATASET_END = "/* source_snapshot_dataset_predicate_end */"


def test_candidate_policy_names_every_dataset_the_v3_plan_reads_or_writes():
    from src.analysis.open_intelligence import source_estate_bridge as bridge

    rule = json.loads(POLICY.read_bytes())["operation_validation"]["source_snapshot_capture"]
    # The collection lanes copy what the funded Wave 1 pilot writes to the product dataset.
    assert rule["datasets"] == ["trends_v2_staging"]
    declared = bridge.bridge_policy()
    assert set(rule["datasets"]) == {
        declared["collection_dataset"],
        declared["product_dataset"],
        declared["snapshot_dataset"],
    }
    # Every dataset literal the routine binds a relation to is one the policy names.
    sql = SQL.read_text(encoding="utf-8")
    bound = set(re.findall(r"'ogilvy-trends-v2[.]([a-z0-9_]+)[.]", sql))
    bound |= set(re.findall(r"'([a-z0-9_]+)[.]'", sql))
    assert bound == set(rule["datasets"])


def _dataset_predicate():
    sql = SQL.read_text(encoding="utf-8")
    assert sql.count(_DATASET_BEGIN) == sql.count(_DATASET_END) == 1
    predicate = sql.split(_DATASET_BEGIN, 1)[1].split(_DATASET_END, 1)[0]
    head = sql.split(_DATASET_BEGIN, 1)[0]
    # The predicate runs over both tables of every relation and refuses before the insert.
    assert head.rstrip().endswith("WHERE NOT (")
    assert (
        "UNNEST([JSON_VALUE(relation, '$.source_table'), JSON_VALUE(relation, '$.destination_table')]) named_table"
        in head[-600:]
    )
    assert sql.index(_DATASET_END) < sql.index("INSERT INTO")
    assert (
        "SET v_policy_datasets = JSON_VALUE_ARRAY(v_policy_json, '$.operation_validation.source_snapshot_capture.datasets');"
        in sql
    )
    return predicate


def _admits(named_table, datasets):
    predicate = _dataset_predicate().replace(
        "UNNEST(v_policy_datasets) named_dataset", "policy_datasets"
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function(
            "REGEXP_CONTAINS",
            2,
            lambda value, pattern: (
                None if value is None else int(re.search(pattern, value) is not None)
            ),
        )
        connection.create_function(
            "CONCAT", -1, lambda *parts: None if None in parts else "".join(parts)
        )
        connection.execute("CREATE TABLE policy_datasets(named_dataset)")
        connection.executemany(
            "INSERT INTO policy_datasets VALUES (?)", [(name,) for name in datasets]
        )
        connection.execute("CREATE TABLE inputs(named_table)")
        connection.execute("INSERT INTO inputs VALUES (?)", (named_table,))
        return connection.execute(f"SELECT ({predicate}) FROM inputs").fetchone()[0] == 1


NAMED = ["intelligence_42_sources_staging", "trends_v2_staging"]


@pytest.mark.parametrize(
    "table",
    [
        "ogilvy-trends-v2.intelligence_42_sources_staging.staging_bridge_v3_20260920_raw_content",
        "ogilvy-trends-v2.trends_v2_staging.event_ledger",
    ],
)
def test_dataset_predicate_admits_tables_in_named_datasets(table):
    assert _admits(table, NAMED)


@pytest.mark.parametrize(
    "table,datasets",
    [
        (
            "ogilvy-trends-v2.trends_v2_staging_approvals.staging_bridge_v3_20260920_raw_content",
            NAMED,
        ),
        (
            "ogilvy-trends-v2.intelligence_42_sources_staging.staging_bridge_v3_20260920_raw_content",
            ["trends_v2_staging"],
        ),
        ("other-project.trends_v2_staging.event_ledger", NAMED),
        ("ogilvy-trends-v2.trends_v2_staging.event_ledger.extra", NAMED),
        ("ogilvy-trends-v2.trends_v2_staging", NAMED),
        ("ogilvy-trends-v2.trends_v2_stagingx.event_ledger", NAMED),
        ("ogilvy-trends-v2.trends_v2_staging.event_ledger", []),
        (None, NAMED),
    ],
)
def test_dataset_predicate_refuses_a_table_outside_the_policy_datasets(table, datasets):
    assert not _admits(table, datasets)


_DESTINATION_BEGIN = "/* source_snapshot_destination_predicate_begin */"
_DESTINATION_END = "/* source_snapshot_destination_predicate_end */"


def _destination_predicate():
    sql = SQL.read_text(encoding="utf-8")
    assert sql.count(_DESTINATION_BEGIN) == sql.count(_DESTINATION_END) == 1
    head, rest = sql.split(_DESTINATION_BEGIN, 1)
    # The destination rule runs over every relation's destination, on its own, before the
    # insert, and under its own refusal code.
    assert head.rstrip().endswith("WHERE NOT (")
    assert "JSON_VALUE(relation, '$.destination_table') AS named_table" in head[-400:]
    assert "AS 'source_snapshot_destination_not_named'" in rest.split(_DESTINATION_END, 1)[1][:60]
    assert sql.index(_DESTINATION_END) < sql.index("INSERT INTO")
    return rest.split(_DESTINATION_END, 1)[0]


def _destination_admits(named_table):
    predicate = _destination_predicate()
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function(
            "REGEXP_CONTAINS",
            2,
            lambda value, pattern: (
                None if value is None else int(re.search(pattern, value) is not None)
            ),
        )
        connection.execute("CREATE TABLE inputs(named_table)")
        connection.execute("INSERT INTO inputs VALUES (?)", (named_table,))
        return connection.execute(f"SELECT ({predicate}) FROM inputs").fetchone()[0] == 1


def test_destination_predicate_admits_only_product_dataset_bridge_tables():
    assert _destination_admits(
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_raw_content"
    )


@pytest.mark.parametrize(
    "table",
    [
        "ogilvy-trends-v2.intelligence_42_sources_staging.staging_bridge_v3_20260920_raw_content",
        "ogilvy-trends-v2.trends_v2_staging_approvals.staging_bridge_v3_20260920_raw_content",
        "ogilvy-trends-v2.trends_v2_staging.event_ledger",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_2026092_raw_content",
        "other-project.trends_v2_staging.staging_bridge_v3_20260920_raw_content",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_raw_content.x",
        None,
    ],
)
def test_destination_outside_the_product_dataset_refuses(table):
    assert not _destination_admits(table)


def test_relation_rule_binds_destinations_to_the_product_dataset():
    sql = SQL.read_text(encoding="utf-8")
    assert (
        "JSON_VALUE(relation, '$.destination_table') IS DISTINCT FROM "
        "CONCAT('ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_', REPLACE(v_cutoff, '-', ''), '_', lane)"
    ) in sql
    assert "intelligence_42_sources_staging.staging_bridge_v3_" not in sql
    rule = json.loads(POLICY.read_bytes())["operation_validation"]["source_snapshot_capture"]
    assert "trends_v2_staging" in rule["datasets"]


def _marked(name):
    sql = SQL.read_text(encoding="utf-8")
    begin, end = f"/* {name}_begin */", f"/* {name}_end */"
    assert sql.count(begin) == sql.count(end) == 1
    assert sql.index(end) < sql.index("INSERT INTO")
    return sql.split(begin, 1)[1].split(end, 1)[0]


def _relation_json_value(raw, path):
    value = json.loads(raw) if raw is not None else None
    key = path.removeprefix("$.")
    return value.get(key) if isinstance(value, dict) else None


def _destination_binding_refuses(destination, *, cutoff="2026-09-20", lane="raw_content"):
    predicate = _marked("source_snapshot_destination_binding_predicate")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _relation_json_value)
        connection.create_function(
            "CONCAT", -1, lambda *parts: None if None in parts else "".join(parts)
        )
        connection.execute("CREATE TABLE inputs(relation, v_cutoff, lane)")
        connection.execute(
            "INSERT INTO inputs VALUES (?, ?, ?)",
            (json.dumps({"destination_table": destination}), cutoff, lane),
        )
        return connection.execute(f"SELECT ({predicate}) FROM inputs").fetchone()[0] == 1


def test_destination_binding_admits_only_the_cutoff_and_lane_bridge_table():
    assert not _destination_binding_refuses(
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_raw_content"
    )


@pytest.mark.parametrize(
    "destination,cutoff,lane",
    [
        # A product table or a source name in the named product dataset: the dataset
        # predicate admits these, so only the exact binding keeps writes off them.
        ("ogilvy-trends-v2.trends_v2_staging.event_ledger", "2026-09-20", "event_ledger"),
        ("ogilvy-trends-v2.trends_v2_staging.raw_content", "2026-09-20", "raw_content"),
        (
            "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260919_raw_content",
            "2026-09-20",
            "raw_content",
        ),
        (
            "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_enriched_content",
            "2026-09-20",
            "raw_content",
        ),
        (
            "ogilvy-trends-v2.intelligence_42_sources_staging.staging_bridge_v3_20260920_raw_content",
            "2026-09-20",
            "raw_content",
        ),
        (None, "2026-09-20", "raw_content"),
    ],
)
def test_destination_binding_refuses_any_other_table(destination, cutoff, lane):
    assert _destination_binding_refuses(destination, cutoff=cutoff, lane=lane)


def _source_binding_refuses(source, *, lane):
    predicate = _marked("source_snapshot_source_binding_predicate")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _relation_json_value)
        connection.create_function(
            "CONCAT", -1, lambda *parts: None if None in parts else "".join(parts)
        )
        connection.execute("CREATE TABLE inputs(relation, lane)")
        connection.execute(
            "INSERT INTO inputs VALUES (?, ?)", (json.dumps({"source_table": source}), lane)
        )
        return connection.execute(f"SELECT ({predicate}) FROM inputs").fetchone()[0] == 1


LANES = [
    "enriched_content",
    "event_ledger",
    "raw_content",
    "seed_candidates",
    "seed_graph",
    "trend_analysis",
    "trend_scores",
]


@pytest.mark.parametrize("lane", LANES)
def test_source_binding_reads_every_lane_from_the_product_dataset(lane):
    """The collection lanes copy what the funded Wave 1 pilot writes to trends_v2_staging,
    so every lane, collection and history alike, clones its own table there."""
    assert not _source_binding_refuses(f"ogilvy-trends-v2.trends_v2_staging.{lane}", lane=lane)


@pytest.mark.parametrize(
    "source,lane",
    [
        ("ogilvy-trends-v2.intelligence_42_sources_staging.raw_content", "raw_content"),
        ("ogilvy-trends-v2.intelligence_42_sources_staging.enriched_content", "enriched_content"),
        ("ogilvy-trends-v2.trends_v2_staging.enriched_content", "raw_content"),
        ("ogilvy-trends-v2.trends_v2_staging_funded.raw_content", "raw_content"),
        ("other-project.trends_v2_staging.raw_content", "raw_content"),
        (None, "raw_content"),
    ],
)
def test_source_binding_refuses_any_other_table(source, lane):
    assert _source_binding_refuses(source, lane=lane)


def _sqlite_expression(expression):
    """The marked BigQuery expression as SQLite text: string literals re-quoted with their
    backslash escapes decoded, and each day interval as its bare count."""
    out, index = [], 0
    while index < len(expression):
        char = expression[index]
        if char != "'":
            out.append(char)
            index += 1
            continue
        index += 1
        literal = []
        while expression[index] != "'":
            if expression[index] == "\\":
                escaped = expression[index + 1]
                literal.append({"n": "\n", "'": "'", "\\": "\\"}[escaped])
                index += 2
            else:
                literal.append(expression[index])
                index += 1
        index += 1
        out.append("'" + "".join(literal).replace("'", "''") + "'")
    return re.sub(r"INTERVAL (\d+) DAY", r"\1", "".join(out))


def _bigquery_format(pattern, *values):
    assert set(re.findall(r"%.", pattern)) <= {"%s"}
    return None if None in values else pattern % values


def _format_timestamp(pattern, value):
    assert pattern == "%Y-%m-%d %H:%M:%E6S"
    return None if value is None else datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S.%f")


def _timestamp(value):
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC).isoformat()


def _timestamp_add(value, days):
    return (datetime.fromisoformat(value) + timedelta(days=days)).isoformat()


def _routine_statement(relation):
    """The creation statement the routine requires for ``relation``, evaluated from its
    marked FORMAT expression."""
    expression = _sqlite_expression(_marked("source_snapshot_creation_statement"))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _relation_json_value)
        connection.create_function("FORMAT", -1, _bigquery_format)
        connection.create_function("FORMAT_TIMESTAMP", 2, _format_timestamp)
        connection.create_function("TIMESTAMP", 1, _timestamp)
        connection.create_function("TIMESTAMP_ADD", 2, _timestamp_add)
        connection.execute("CREATE TABLE inputs(relation)")
        connection.execute("INSERT INTO inputs VALUES (?)", (json.dumps(relation),))
        return connection.execute(f"SELECT ({expression}) FROM inputs").fetchone()[0]


def _built_plan(tmp_path, monkeypatch, origin):
    if origin == "bridge_plan":
        return fixture()[2]
    from tests.unit import bridge_route_fixture as route

    _, output = route.produced(tmp_path, monkeypatch)
    inputs_read, _ = route.read_outputs(output)
    return json.loads(inputs_read["capture_plan"])


@pytest.mark.parametrize("origin", ["bridge_plan", "route_a_producer"])
def test_the_routine_requires_exactly_the_creation_statements_the_plan_builds(
    tmp_path, monkeypatch, origin
):
    """Every one of the seven statements build_bridge_plan emits, directly and through the
    route A producer, is the one the routine's FORMAT requires, byte for byte."""
    plan = _built_plan(tmp_path, monkeypatch, origin)
    pairs = list(
        zip(plan["creation_statements"], plan["snapshot_plan"]["relation_bindings"], strict=True)
    )
    assert len(pairs) == 7
    differing = [
        statement["lane"]
        for statement, relation in pairs
        if _routine_statement(relation) != statement["sql"]
    ]
    assert differing == []
    # The statement clones the source table the routine's source binding admits.
    assert not any(
        _source_binding_refuses(relation["source_table"], lane=relation["lane"])
        for _, relation in pairs
    )


def test_the_routine_refuses_a_creation_statement_carrying_an_expiry():
    """The v3 clones carry no expiry, so a statement with an OPTIONS clause is not the one the
    routine requires."""
    plan = fixture()[2]
    expression = _marked("source_snapshot_creation_statement")
    assert "OPTIONS" not in expression
    assert "expiration" not in expression
    for statement, relation in zip(
        plan["creation_statements"], plan["snapshot_plan"]["relation_bindings"], strict=True
    ):
        stamp = datetime.fromisoformat(relation["snapshot_as_of"]) + timedelta(days=90)
        carried = (
            statement["sql"] + "\nOPTIONS (expiration_timestamp = TIMESTAMP "
            f"'{stamp:%Y-%m-%d %H:%M:%S.%f}+00')"
        )
        assert _routine_statement(relation) != carried


def _dataset_name_admitted(name):
    predicate = _marked("source_snapshot_dataset_name_predicate")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function(
            "REGEXP_CONTAINS",
            2,
            lambda value, pattern: (
                None if value is None else int(re.search(pattern, value) is not None)
            ),
        )
        connection.execute("CREATE TABLE inputs(named_dataset)")
        connection.execute("INSERT INTO inputs VALUES (?)", (name,))
        return connection.execute(f"SELECT ({predicate}) FROM inputs").fetchone()[0] == 1


@pytest.mark.parametrize("name", ["trends_v2_staging", "intelligence_42_sources_staging"])
def test_policy_dataset_name_check_admits_plain_names(name):
    assert _dataset_name_admitted(name)


@pytest.mark.parametrize(
    "name",
    [
        "trends_v2_staging|intelligence_42_sources_staging",
        "trends_v2_.*",
        "[a-z_]+",
        "trends_v2_staging$",
        "trends.v2",
        "Trends_v2_staging",
        "",
        None,
    ],
)
def test_policy_dataset_name_carrying_regex_characters_refuses(name):
    assert not _dataset_name_admitted(name)


@pytest.mark.parametrize(
    "destination,cutoff,lane",
    [
        (
            "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260921_raw_content",
            "2026-09-21",
            "raw_content",
        ),
        (
            "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_enriched_content",
            "2026-09-20",
            "enriched_content",
        ),
        (
            "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260921_seed_graph",
            "2026-09-21",
            "seed_graph",
        ),
    ],
)
def test_destination_binding_follows_the_approved_cutoff_and_the_relation_lane(
    destination, cutoff, lane
):
    assert not _destination_binding_refuses(destination, cutoff=cutoff, lane=lane)


@pytest.mark.parametrize(
    "destination,cutoff,lane",
    [
        (
            "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_raw_content",
            "2026-09-21",
            "raw_content",
        ),
        (
            "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260921_raw_content",
            "2026-09-21",
            "enriched_content",
        ),
    ],
)
def test_destination_binding_refuses_the_right_name_under_another_cutoff_or_lane(
    destination, cutoff, lane
):
    assert _destination_binding_refuses(destination, cutoff=cutoff, lane=lane)


_DESTINATION_ASSERT_END = " AS 'source_snapshot_destination_not_named';"


def _destination_assert_passes(relations, datasets):
    """Run the routine's whole destination assert, its policy clause included, over the
    plan's relations and the policy's datasets."""
    sql = SQL.read_text(encoding="utf-8")
    assert sql.count(_DESTINATION_ASSERT_END) == 1
    head = sql.split(_DESTINATION_ASSERT_END, 1)[0]
    statement = head[head.rindex("ASSERT ") + len("ASSERT ") :]
    assert sql.index(_DESTINATION_ASSERT_END) < sql.index("INSERT INTO")
    source = "FROM UNNEST(JSON_QUERY_ARRAY(v_plan, '$.snapshot_plan.relation_bindings')) relation)"
    assert statement.count(source) == 1
    query = "SELECT " + statement.replace(source, "FROM relations)").replace(
        "UNNEST(v_policy_datasets)", "(SELECT named_dataset FROM policy_datasets)"
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _relation_json_value)
        connection.create_function(
            "REGEXP_CONTAINS",
            2,
            lambda value, pattern: (
                None if value is None else int(re.search(pattern, value) is not None)
            ),
        )
        connection.execute("CREATE TABLE policy_datasets(named_dataset)")
        connection.executemany(
            "INSERT INTO policy_datasets VALUES (?)", [(name,) for name in datasets]
        )
        connection.execute("CREATE TABLE relations(relation)")
        connection.executemany(
            "INSERT INTO relations VALUES (?)", [(json.dumps(row),) for row in relations]
        )
        return connection.execute(query).fetchone()[0] == 1


def routine_destination_checks_pass(plan, datasets):
    """Every marked destination check of the v3 routine, run over a plan as built."""
    relations = plan["snapshot_plan"]["relation_bindings"]
    return _destination_assert_passes(relations, datasets) and all(
        _destination_admits(row["destination_table"])
        and not _destination_binding_refuses(
            row["destination_table"], cutoff=plan["cutoff_date"], lane=row["lane"]
        )
        and _admits(row["destination_table"], datasets)
        and _admits(row["source_table"], datasets)
        for row in relations
    )


def _policy_datasets():
    return json.loads(POLICY.read_bytes())["operation_validation"]["source_snapshot_capture"][
        "datasets"
    ]


def test_a_built_bridge_plan_passes_every_destination_check_of_the_routine():
    _source, _extra, plan, _rule = fixture()
    relations = plan["snapshot_plan"]["relation_bindings"]
    assert len(relations) == 7
    assert routine_destination_checks_pass(plan, _policy_datasets())


def test_a_built_plan_moved_to_the_collection_dataset_fails_the_routine():
    _source, _extra, plan, _rule = fixture()
    row = plan["snapshot_plan"]["relation_bindings"][0]
    row["destination_table"] = row["destination_table"].replace(
        ".trends_v2_staging.", ".intelligence_42_sources_staging."
    )
    assert not routine_destination_checks_pass(plan, _policy_datasets())
    assert not _destination_assert_passes(
        plan["snapshot_plan"]["relation_bindings"], _policy_datasets()
    )


@pytest.mark.parametrize(
    "datasets",
    [["intelligence_42_sources_staging"], [], ["trends_v2_staging_approvals"]],
    ids=["collection_only", "none", "similar_name"],
)
def test_the_destination_assert_refuses_a_policy_without_the_product_dataset(datasets):
    _source, _extra, plan, _rule = fixture()
    relations = plan["snapshot_plan"]["relation_bindings"]
    assert _destination_assert_passes(relations, _policy_datasets())
    assert not _destination_assert_passes(relations, datasets)
