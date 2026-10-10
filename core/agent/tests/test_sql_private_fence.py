"""N2, C5 v2 section 12: the private-storage fence in sql_query (tests B01 to B14 and B20 to B25).

Every expectation below is a literal written in this file. None is read from the module under test, so widening or
narrowing a list in the code fails here (the defect class: a value that proves something taken from what it proves
something about).
"""

import re
from datetime import datetime
from pathlib import Path

import pytest

from core.agent import ask, toolset
from core.agent.context import Refused, RunContext
from core.agent.tools import sql_query as module
from core.agent.tools.sql_query import (
    ALLOWED_TVFS,
    WAREHOUSE_EXAMPLE,
    WAREHOUSE_MAP,
    check_sql,
    check_sql_harness,
    model_sql_query,
    sql_query,
)

CORE, AGENT = "intelligence_42_core", "intelligence_42_agent"

# Class D, written out from C5 v2 12.1 plus claim_checks (ruling C1-C5, C5 condition 1).
CLASS_D = [
    f"{CORE}.suppressions", f"{CORE}.v_suppressed_creators", f"{CORE}.raw_responses", f"{CORE}.credit_ledger",
    f"{AGENT}.runs", f"{AGENT}.skins", f"{AGENT}.feedback", f"{AGENT}.schedules", f"{AGENT}.investigations",
    f"{AGENT}.dossier_versions", f"{AGENT}.dossier_reviews", f"{AGENT}.watches", f"{AGENT}.v_watches_current",
    f"{AGENT}.briefs", f"{AGENT}.v_briefs_current", f"{AGENT}.findings", f"{AGENT}.v_prior_findings",
    f"{AGENT}.v_item_evidence", f"{AGENT}.claim_checks",
]
CLASS_P = [f"{AGENT}.failure_text_private", f"{CORE}.anything_private", f"{AGENT}.Notes_PRIVATE"]
DENIED = CLASS_D + CLASS_P
FORECASTS = [f"{AGENT}.forecasts", f"{CORE}.v_forecasts_current", f"{AGENT}.forecast_score"]


class FakeWarehouse:
    def __init__(self, rows=None, tables=(f"ogilvy-trends-v2.{CORE}.posts",), bytes_=1_000):
        self.rows = rows if rows is not None else [{"post_id": "p1", "n": 3}]
        self.tables = list(tables)
        self.bytes = bytes_
        self.dry_runs, self.runs = [], []

    def dry_run(self, sql, params):
        self.dry_runs.append((sql, params))
        return {"bytes": self.bytes, "tables": list(self.tables)}

    def run(self, sql, params, max_bytes_billed):
        self.runs.append((sql, params, max_bytes_billed))
        return [dict(r) for r in self.rows]


@pytest.fixture
def ctx():
    return RunContext(run_id="run_fence", tier="T0", as_of=datetime(2026, 9, 28, 6, 0))


def forms(table):
    """Every spelling of C5 v2 12.2 for one table."""
    dataset, name = table.split(".")
    short = name[:-1]
    return [
        f"SELECT * FROM {table}",
        f"SELECT * FROM `ogilvy-trends-v2.{table}`",
        f"SELECT * FROM `ogilvy-trends-v2`.{dataset}.{name}",
        f"SELECT * FROM `{dataset}`.`{name}`",
        f"SELECT * FROM {dataset}.{name.title()}",
        f"SELECT * FROM {dataset}.{short}*",
        f"SELECT * FROM {dataset}.*",
        f"SELECT * FROM `{dataset}.{short}*`",
        f"SELECT * FROM {table}$20261008",
        f"SELECT * FROM {table} FOR SYSTEM_TIME AS OF TIMESTAMP '2026-10-01'",
        f"SELECT x.* FROM {table} AS x",
        f"SELECT 1 FROM {CORE}.posts p JOIN {table} t ON TRUE",
        f"SELECT 1 AS n UNION ALL SELECT 1 FROM {table}",
        f"SELECT (SELECT COUNT(*) FROM {table}) AS n",
        f"SELECT 1 AS n WHERE EXISTS (SELECT 1 FROM {table})",
        f"SELECT ARRAY(SELECT 1 FROM {table}) AS a",
        f"SELECT 1 FROM {CORE}.posts p, UNNEST((SELECT ARRAY_AGG(1) FROM {table})) AS u",
        f"SELECT * FROM {AGENT}.tvf_search_posts((SELECT MIN('x') FROM {table}), 10)",
        f"WITH x AS (SELECT * FROM {table}) SELECT * FROM x",
        f"SELECT JSON_QUERY(r.record, '$.answer') AS j FROM {table} r",
        f"SELECT TO_JSON_STRING(t) AS j FROM {table} t",
    ]


# B01. Every form, every denied name, with the class D and P sentence and never the forecasts one.
@pytest.mark.parametrize("table", DENIED)
def test_b01_every_form_of_a_denied_table_is_refused_with_its_own_sentence(table):
    for sql in forms(table):
        with pytest.raises(Refused) as err:
            check_sql(sql)
        text = str(err.value)
        assert "is not available to the agent" in text, sql
        assert "forecast" not in text.lower(), sql


def test_b01_class_d_is_exactly_the_contracted_list():
    assert sorted(module.FENCED_TABLES) == sorted(CLASS_D)


# B03. The dataset metadata tables.
@pytest.mark.parametrize("name", ["__TABLES__", "__TABLES_SUMMARY__", "__PARTITIONS_SUMMARY__", "__tables__"])
def test_b03_metadata_tables_are_refused(name):
    for sql in (f"SELECT * FROM {CORE}.{name}", f"SELECT * FROM {AGENT}.{name}", f"SELECT * FROM `{AGENT}.{name}`"):
        with pytest.raises(Refused):
            check_sql(sql)


# B02. The approved reads still pass.
APPROVED_VIEWS = [f"{AGENT}.v_items_today", f"{AGENT}.v_item_gate_current", f"{AGENT}.v_item_tone_daily",
                  f"{CORE}.v_item_market_scope", f"{CORE}.v_item_daily_current", f"{CORE}.v_item_state_current",
                  f"{CORE}.v_good_runs", f"{CORE}.v_item_waves", f"{CORE}.v_item_spread",
                  f"{AGENT}.v_item_origin", f"{AGENT}.v_news_followthrough"]


@pytest.mark.parametrize("sql", [f"SELECT * FROM {view}" for view in APPROVED_VIEWS] + [
    WAREHOUSE_EXAMPLE,
    f"SELECT * FROM {AGENT}.tvf_item_timeseries('i', 'ZA', 7)",
    f"SELECT * FROM {AGENT}.tvf_search_posts('braai', 10)",
    f"SELECT * FROM {CORE}.google_search_signals",
])
def test_b02_the_approved_reads_still_pass(sql):
    check_sql(sql)


def test_b02_every_warehouse_map_table_passes():
    for name in WAREHOUSE_MAP:
        table = name.split("(")[0]
        if "tvf_" in table:
            continue
        check_sql(f"SELECT * FROM {table}")
    assert set(ALLOWED_TVFS) == {f"{AGENT}.tvf_search_posts", f"{AGENT}.tvf_item_timeseries"}


# B04, B05, B06.
@pytest.mark.parametrize("qualified", [f"{CORE}.suppressions", f"{AGENT}.runs", f"{AGENT}.briefs", f"{AGENT}.findings"])
def test_b04_an_unqualified_cte_with_a_denied_name_passes_and_a_qualified_reference_is_not_laundered(qualified):
    table = qualified.split(".")[1]
    check_sql(f"WITH {table} AS (SELECT post_id FROM {CORE}.posts) SELECT * FROM {table}")
    check_sql(f"SELECT * FROM {CORE}.posts AS {table}")
    with pytest.raises(Refused):
        check_sql(f"WITH {table} AS (SELECT 1 AS x) SELECT * FROM {qualified}")


@pytest.mark.parametrize("table", DENIED)
def test_b05_a_denied_name_in_a_string_literal_or_a_comment_passes(table):
    check_sql(f"SELECT '{table}' AS note FROM {CORE}.posts")
    check_sql(f"SELECT post_id FROM {CORE}.posts -- {table}")
    check_sql(f"SELECT post_id /* {table} */ FROM {CORE}.posts")


def test_b06_the_legacy_decorator_is_refused_as_unparseable():
    with pytest.raises(Refused):
        check_sql(f"SELECT * FROM {AGENT}.runs@-3600000")


# B13, B14. P by suffix, wildcards that could reach a P or D name.
@pytest.mark.parametrize("sql", [
    f"SELECT * FROM {AGENT}.some_new_thing_private",
    f"SELECT * FROM {CORE}.x_PRIVATE",
    f"SELECT * FROM `ogilvy-trends-v2.{AGENT}.y_private`",
    f"SELECT * FROM {AGENT}.y_private$20261008",
])
def test_b13_any_object_ending_private_is_refused_in_both_datasets(sql):
    with pytest.raises(Refused) as err:
        check_sql(sql)
    assert "is not available to the agent" in str(err.value)


@pytest.mark.parametrize("sql", [
    f"SELECT * FROM {AGENT}.*", f"SELECT * FROM {CORE}.*", f"SELECT * FROM {AGENT}.f*", f"SELECT * FROM {AGENT}.ru*",
    f"SELECT * FROM {CORE}.v_supp*", f"SELECT * FROM {AGENT}.*private", f"SELECT * FROM {AGENT}.post*",
    f"SELECT * FROM `{AGENT}.fa*`",
])
def test_b14_wildcards_that_could_reach_a_denied_name_are_refused(sql):
    with pytest.raises(Refused):
        check_sql(sql)


def test_b14_a_trailing_wildcard_on_an_allowed_table_is_refused_because_it_could_reach_a_private_name():
    with pytest.raises(Refused):
        check_sql(f"SELECT * FROM {CORE}.item_hourly*")  # could match item_hourly_private
    check_sql(f"SELECT * FROM {CORE}.item_hourly")


# B12. Refused before the warehouse is touched.
@pytest.mark.parametrize("table", DENIED)
def test_b12_the_query_is_refused_before_the_warehouse_is_touched(table, ctx):
    wh = FakeWarehouse()
    with pytest.raises(Refused):
        model_sql_query(ctx, wh, f"SELECT * FROM {table}", purpose="x")
    with pytest.raises(Refused):
        sql_query(ctx, wh, f"SELECT * FROM {table}", purpose="x")
    assert wh.dry_runs == [] and wh.runs == [] and ctx.queries == {}


# B21. The model tool path cannot supply hidden_ok.
def test_b21_the_model_tool_path_cannot_supply_hidden_ok(ctx):
    schema = toolset.SCHEMAS["sql_query"]
    assert "hidden_ok" not in schema["properties"] and schema["additionalProperties"] is False
    wh = FakeWarehouse()
    functions = toolset.build_functions(ctx, wh, None, None)
    with pytest.raises(TypeError):
        functions["sql_query"](sql=f"SELECT * FROM {CORE}.suppressions", purpose="x", hidden_ok=(f"{CORE}.suppressions",))
    with pytest.raises(Refused):
        functions["sql_query"](sql=f"SELECT * FROM {CORE}.suppressions", purpose="x")
    with pytest.raises(Refused):
        toolset.guard(ctx, "sql_query", {"sql": f"SELECT * FROM {CORE}.suppressions", "purpose": "x"})
    assert wh.dry_runs == [] and wh.runs == []


# B08, B09, B10. The dry-run layer.
def dry(ctx, sql, *tables):
    wh = FakeWarehouse(tables=tables)
    return model_sql_query(ctx, wh, sql, purpose="x")


def test_b08_a_dry_run_that_lists_a_private_object_is_refused_even_when_the_parse_names_an_approved_view(ctx):
    with pytest.raises(Refused):
        dry(ctx, f"SELECT * FROM {AGENT}.v_items_today", f"ogilvy-trends-v2.{AGENT}.failure_text_private")
    with pytest.raises(Refused):
        dry(ctx, f"SELECT * FROM {AGENT}.v_items_today", f"ogilvy-trends-v2.{CORE}.thing_PRIVATE")


def test_b09_runs_in_the_dry_run_is_allowed_only_for_a_named_view_that_reads_it(ctx):
    runs = f"ogilvy-trends-v2.{AGENT}.runs"
    assert dry(ctx, f"SELECT * FROM {AGENT}.v_items_today", runs)["rows"]
    assert dry(ctx, f"SELECT * FROM {CORE}.v_item_daily_current", runs)["rows"]
    assert dry(ctx, f"SELECT * FROM {AGENT}.tvf_item_timeseries('i', 'ZA', 7)", runs)["rows"]
    with pytest.raises(Refused):
        dry(ctx, f"SELECT post_id FROM {CORE}.posts", runs)
    with pytest.raises(Refused):
        dry(ctx, f"SELECT * FROM {CORE}.v_item_market_scope", f"ogilvy-trends-v2.{AGENT}.skins")


def test_b09_the_suppression_objects_in_the_dry_run_are_allowed_only_under_v_item_market_scope(ctx):
    for table in ("suppressions", "v_suppressed_creators"):
        ref = f"ogilvy-trends-v2.{CORE}.{table}"
        assert dry(ctx, f"SELECT * FROM {CORE}.v_item_market_scope", ref)["rows"]
        with pytest.raises(Refused):
            dry(ctx, f"SELECT * FROM {AGENT}.v_items_today", ref)
        with pytest.raises(Refused):
            dry(ctx, f"SELECT post_id FROM {CORE}.posts", ref)


def test_b09_briefs_in_the_dry_run_are_allowed_only_under_the_two_views_that_read_them(ctx):
    ref = f"ogilvy-trends-v2.{AGENT}.briefs"
    assert dry(ctx, f"SELECT * FROM {AGENT}.v_item_gate_current", ref)["rows"]
    with pytest.raises(Refused):
        dry(ctx, f"SELECT * FROM {AGENT}.v_items_today", ref)


def test_b09_findings_and_the_other_d_tables_are_never_allowed_in_the_dry_run_of_a_model_query(ctx):
    for table in CLASS_D:
        if table.endswith((".runs", ".briefs", ".suppressions", ".v_suppressed_creators")):
            continue
        with pytest.raises(Refused):
            dry(ctx, f"SELECT * FROM {AGENT}.v_items_today", f"ogilvy-trends-v2.{table}")


def test_b10_a_dry_run_list_of_fifty_tables_is_refused(ctx):
    many = tuple(f"ogilvy-trends-v2.{CORE}.t{i}" for i in range(50))
    with pytest.raises(Refused):
        dry(ctx, f"SELECT post_id FROM {CORE}.posts", *many)
    assert dry(ctx, f"SELECT post_id FROM {CORE}.posts", *many[:49])["rows"]


def test_the_pinned_dependency_literal_matches_the_sql_files():
    root = Path(__file__).resolve().parents[3]
    text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "core").rglob("*.sql"))
    for name in module.APPROVED_OBJECT_DEPS:
        dataset, obj = name.split(".")
        assert re.search(rf"(VIEW|FUNCTION)\s+(IF NOT EXISTS\s+)?[`\w{{}}.\-]*{obj}\b", text), name
    runs_readers = {name for name, deps in module.APPROVED_OBJECT_DEPS.items() if f"{AGENT}.runs" in deps}
    assert runs_readers == {
        f"{CORE}.v_good_runs", f"{CORE}.v_collection_health_current", f"{CORE}.v_item_daily_current",
        f"{CORE}.v_series_test_current", f"{CORE}.v_coord_signals_current", f"{CORE}.v_item_state_current",
        f"{CORE}.v_item_counter_daily_current", f"{CORE}.v_series_daily", f"{CORE}.v_item_spread",
        f"{AGENT}.v_item_origin", f"{CORE}.v_item_waves", f"{AGENT}.v_news_followthrough",
        f"{CORE}.v_breaking_signals_current", f"{AGENT}.v_items_today", f"{AGENT}.v_item_gate_current",
        f"{CORE}.v_item_market_scope", f"{CORE}.tvf_item_window", f"{CORE}.tvf_placebo_base",
        f"{CORE}.tvf_series_signal", f"{AGENT}.tvf_item_timeseries", f"{AGENT}.v_briefs_current"}
    assert {n for n, d in module.APPROVED_OBJECT_DEPS.items() if f"{AGENT}.briefs" in d} == {
        f"{AGENT}.v_item_gate_current", f"{AGENT}.v_briefs_current"}
    assert {n for n, d in module.APPROVED_OBJECT_DEPS.items() if f"{CORE}.suppressions" in d} == {
        f"{CORE}.v_item_market_scope", f"{CORE}.v_suppressed_creators"}
    assert {n for n, d in module.APPROVED_OBJECT_DEPS.items() if f"{CORE}.v_suppressed_creators" in d} == {
        f"{CORE}.v_item_market_scope"}


# B23. The review's relations are refused in every form, and each internal reader still reads its own.
REVIEW_ADDITIONS = [f"{AGENT}.v_item_evidence", f"{AGENT}.v_briefs_current", f"{AGENT}.briefs",
                    f"{AGENT}.v_prior_findings", f"{AGENT}.findings", f"{AGENT}.watches", f"{AGENT}.v_watches_current"]


@pytest.mark.parametrize("sql", [
    f"SELECT handle, url, excerpt, clip_uri FROM {AGENT}.v_item_evidence",
    f"SELECT payload FROM {AGENT}.v_briefs_current", f"SELECT payload FROM {AGENT}.briefs",
    f"SELECT answer, claims FROM {AGENT}.v_prior_findings", f"SELECT claims FROM {AGENT}.findings",
    f"SELECT label FROM {AGENT}.watches", f"SELECT label FROM {AGENT}.v_watches_current",
    f"SELECT reason FROM {AGENT}.claim_checks WHERE rule = 'K4'",
])
def test_b23_the_review_relations_are_refused_for_model_sql(sql):
    with pytest.raises(Refused):
        check_sql(sql)


def test_b23_each_review_relation_is_in_the_pinned_class_d_list():
    assert set(REVIEW_ADDITIONS) <= set(module.FENCED_TABLES)


def test_b23_calendar_analogues_and_media_stay_readable():
    check_sql(f"SELECT evidence FROM {CORE}.calendar_analogues")
    check_sql(f"SELECT gcs_uri FROM {CORE}.media")


# B20. INTERNAL_ALLOW lists exactly the functions that read a denied object, and only those functions use it.
def test_b20_internal_allow_lists_exactly_the_functions_that_read_denied_objects():
    registry = module.INTERNAL_ALLOW
    assert set(registry) == {"discover_creators", "fetch_posts_reuse", "recall_findings",
                             "get_trending_fallback_snapshot", "log_forecast"}
    assert set(registry["discover_creators"]) == {f"{CORE}.v_suppressed_creators", f"{CORE}.suppressions"}
    assert set(registry["fetch_posts_reuse"]) == {f"{CORE}.v_suppressed_creators", f"{CORE}.suppressions"}
    assert set(registry["recall_findings"]) == {f"{AGENT}.v_prior_findings", f"{AGENT}.findings"}
    assert set(registry["get_trending_fallback_snapshot"]) == {f"{AGENT}.v_briefs_current", f"{AGENT}.briefs"}
    assert set(registry["log_forecast"]) == {f"{AGENT}.forecasts"}


def test_b20_only_internal_read_passes_hidden_ok_and_only_for_registered_functions():
    root = Path(__file__).resolve().parents[1]
    callers = {}
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or path.name == "sql_query.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "hidden_ok" not in source, f"{path.name} passes hidden_ok outside sql_query.internal_read"
        assert "_run_query" not in source, f"{path.name} reaches _run_query directly"
        for name in re.findall(r"internal_read\(\s*\"(\w+)\"", source):
            callers.setdefault(name, []).append(path.name)
    assert set(callers) == set(module.INTERNAL_ALLOW)
    assert callers["discover_creators"] == ["warehouse.py"] and callers["log_forecast"] == ["forecast.py"]


def test_b20_an_unregistered_function_cannot_read_a_denied_object(ctx):
    with pytest.raises(KeyError):
        module.internal_read("search_posts", ctx, FakeWarehouse(), f"SELECT * FROM {CORE}.suppressions", purpose="x")


def test_b20_an_internal_read_reaches_only_its_own_objects(ctx):
    wh = FakeWarehouse()
    module.internal_read("recall_findings", ctx, wh, f"SELECT finding_id FROM {AGENT}.v_prior_findings", purpose="x")
    for sql in (f"SELECT * FROM {CORE}.suppressions", f"SELECT * FROM {AGENT}.runs", f"SELECT * FROM {AGENT}.briefs"):
        with pytest.raises(Refused):
            module.internal_read("recall_findings", ctx, wh, sql, purpose="x")


# B24. SPEND_SQL by exact text and nothing else.
def test_b24_the_spend_query_passes_unamended_and_by_equality_only():
    check_sql(ask.SPEND_SQL)
    check_sql("  " + ask.SPEND_SQL.replace(" FROM ", "\n   FROM ") + "\n")
    for text in (f"SELECT * FROM ({ask.SPEND_SQL})", ask.SPEND_SQL + " UNION ALL SELECT handle FROM " + CORE + ".suppressions",
                 ask.SPEND_SQL.replace("SELECT", "SELECT 1 AS extra,", 1)):
        with pytest.raises(Refused):
            check_sql(text)


def test_b24_the_suppression_key_sql_is_not_allowed_by_equality():
    from core.agent.tools import enrich_tools, socialcrawl
    from core.agent import skills

    for text in (socialcrawl.SUPPRESSED_KEYS_SQL, skills.CREATOR_SQL, enrich_tools.SUPPRESSED_SQL):
        with pytest.raises(Refused):
            check_sql(text)


# B25. The harness check.
def test_b25_check_sql_harness_accepts_each_internal_shape_and_refuses_a_model_query_on_class_d():
    from core.agent.tools import enrich_tools, socialcrawl
    from core.agent import skills

    for text in (socialcrawl.SUPPRESSED_KEYS_SQL, skills.CREATOR_SQL, enrich_tools.SUPPRESSED_SQL, ask.SPEND_SQL,
                 f"SELECT claims FROM {AGENT}.v_prior_findings", f"SELECT payload FROM {AGENT}.v_briefs_current",
                 f"SELECT forecast_id FROM {AGENT}.forecasts"):
        check_sql_harness(text)
        if text != ask.SPEND_SQL:
            with pytest.raises(Refused):
                check_sql(text)
    for table in (f"{AGENT}.skins", f"{AGENT}.dossier_versions", f"{CORE}.raw_responses", f"{AGENT}.v_item_evidence",
                  f"{AGENT}.watches", f"{AGENT}.claim_checks", f"{AGENT}.anything_private"):
        with pytest.raises(Refused):
            check_sql_harness(f"SELECT * FROM {table}")


def test_b25_the_harness_allowance_is_the_internal_objects_plus_the_suppression_pair():
    expected = {f"{CORE}.v_suppressed_creators", f"{CORE}.suppressions", f"{AGENT}.v_prior_findings",
                f"{AGENT}.findings", f"{AGENT}.v_briefs_current", f"{AGENT}.briefs", f"{AGENT}.forecasts"}
    assert set(module.HARNESS_ALLOW) == expected
