"""BigQueryStore, the read path behind Today, Discover and Topic, checked as a BigQuery dry run would check it.

No test used to run these statements or dry-run them (work item R4, finding X-tests-04), and the 48 dry-run tests
that exist are opt-in and some of them skip when a table is missing (X-tests-07). Here every public BigQueryStore
method is called against a recording client, with the catalog answering once as if every object the repository
creates exists and once as if none does, so both branches of each method run. Every statement it sends is then
read the way a dry run reads it:

  * it parses,
  * every table, view and table function it names is created by a SQL file under core/ (a missing one fails),
  * every column it uses on a table or view whose columns are known exists,
  * every @parameter it uses was bound, and the 2 GB bytes-billed cap is on the job.

The methods table below must list every public method, so a method added without a harness call fails.
The live counterpart, which sends the same statements to BigQuery with dry_run=True, runs only with F42_BQ=1 and
fails on a missing table instead of skipping.
"""
import inspect
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_bq as hb  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]
D, S0, P0 = "2026-09-30", "2026-09-23", "2026-09-16"
RUN = {"run_id": "detect-1", "run_date": D}
SUBJECT = {"key": "k0", "item_id": "i1", "market": "ZA", "platform": None, "terms": None}
SUBJECT_TERMS = {"key": "k1", "item_id": "i2", "market": "NG", "platform": "tiktok", "terms": ["amapiano", "gqom"]}
FINDING = {"finding_id": "f1", "question": "q", "answer": "{}", "as_of": D, "claims": "[]", "valid_from": D,
           "valid_to": None, "status": "open"}

CALLS = {
    "latest_brief_date": (), "briefs": (D,), "previous_brief": ("ZA", D), "collection_health": (D,),
    "latest_collection_day": (D,), "calendar": (S0, D), "runs": ("collect", D), "first_ok_collect_date": (),
    "ask_record": ("a_1",), "health": (), "latest_detect_run": (D,), "item_states": (RUN, "ZA"),
    "watch_matches": (RUN,), "item_gate": ("ZA", D), "item_history": ("i1", "ZA", S0, D),
    "item_series": ("i1", "ZA", 7), "item_waves": ("i1", "ZA"), "coord_signals": ("i1", "ZA", D),
    "item_spread": (D, "ZA", "i1"), "item_reach": (D, "ZA", ["i1"]), "health_days": ("ZA", S0, D),
    "creator_names": (["tiktok:c1"],), "item_origin": ("i1", "ZA"), "news_followthrough": ("i1", "ZA", D),
    "item_evidence": ("i1", "ZA"), "credits": (D,), "runs_of_day": (D,), "has_model_usd": (), "scorecard": (),
    "latest_aggregate_run": (), "map_items": (["i1"],), "breaking_signals": (D,),
    "search_signals": (S0, D, ["ZA"]), "health_range": (S0, D, ["ZA"]),
    "compare_counts": ([SUBJECT, SUBJECT_TERMS], S0, D), "creator": ("c1",), "creators_by_id": (["c1"],),
    "creators_by_handle": (["tiktok:x"],), "detect_states": (S0, D), "sensitive_complete": (),
    "sensitive_items": (), "generic_items": (), "board_items": ("ZA", S0, D), "suppressed_creators": (),
    "suppressions": (), "creator_posts": ("c1", ["c9"], 10), "window_posts": (["c1"], "ZA", S0, D),
    "community_edges": ("ZA", S0, D, ["c9"]), "posts_by_id": (["p1"],),
    "seed_path_posts": ("ZA", ["amapiano"], S0, D, 10), "seed_queue": ("ZA", S0, D, 10),
    "waves": (["i1"], "ZA"), "item_days": (["i1"], "ZA", S0, D), "nearest_items": ("i1", 5),
    "search_items": ("amapiano", 10), "lexicon_terms": ("ZA", ["hashtag"], S0, D, S0, P0, 10),
    "ask_history": (10, D, "ZA"), "brief_history": (10, D, "ZA"), "findings": ("i1",),
    "finding_rows": ("f1",), "insert_finding": (FINDING,),
    "hidden_people_rows": (), "post_creators": (["p1"], S0, D),
}
# Methods whose only call is a streaming insert, not a query.
NO_QUERY = {"insert_finding"}


@pytest.fixture(scope="module")
def registry():
    return hb.load_registry(ROOT)


@pytest.fixture
def fresh_catalog(monkeypatch):
    from core.api import store

    monkeypatch.setattr(store, "_CATALOG", {})
    return store


def run_method(store_module, registry, method, exists):
    client = hb.RecordingClient(registry, exists=exists)
    store = store_module.BigQueryStore(project=hb.PROJECT, client=client)
    if method in NO_QUERY and not exists:
        # With no findings table the insert refuses before it sends anything.
        with pytest.raises(RuntimeError, match="unavailable"):
            getattr(store, method)(*CALLS[method])
        assert not client.inserts
        return client
    getattr(store, method)(*CALLS[method])
    return client


def public_methods():
    from core.api import store

    return sorted(name for name, fn in inspect.getmembers(store.BigQueryStore, inspect.isfunction)
                  if not name.startswith("_"))


def test_every_public_method_has_a_harness_call_and_none_is_stale():
    assert sorted(CALLS) == public_methods()


def test_the_registry_reads_the_schema_files(registry):
    assert len(registry.objects) >= 60
    for name in ("intelligence_42_core.posts", "intelligence_42_core.v_suppressed_creators",
                 "intelligence_42_agent.runs", "intelligence_42_agent.findings",
                 "intelligence_42_agent.v_briefs_current"):
        assert name in registry.objects, name
    assert registry.get("intelligence_42_core", "posts").columns
    assert "creator_id" in registry.get("intelligence_42_core", "posts").columns
    assert all(o.columns for o in registry.objects.values() if o.kind in ("table", "view"))


# Contract section 8 and the module docstring of core/api/store.py: every query carries a 2 GB bytes-billed cap.
BYTES_CAP = 2_000_000_000


def dry_run_failures(store_module, registry, method, exists):
    client = run_method(store_module, registry, method, exists)
    if exists and method not in NO_QUERY and not client.statements:
        return [f"{method} sent no statement when every object exists, so the harness did not run it"]
    failures = []
    for sql, config in client.statements:
        if "INFORMATION_SCHEMA" in sql and "UNION ALL" in sql:
            continue
        shown = " ".join(sql.split())[:160]
        failures += [" | ".join((str(problem), shown))
                     for problem in hb.check_statement(sql, hb.bound_names(config), registry)]
        if config.maximum_bytes_billed != BYTES_CAP:
            failures.append(" | ".join((f"bytes-billed cap is {config.maximum_bytes_billed}, not {BYTES_CAP}", shown)))
    return failures


@pytest.mark.parametrize("exists", [True, False], ids=["every object exists", "no object exists"])
@pytest.mark.parametrize("method", sorted(CALLS))
def test_every_statement_a_method_sends_passes_a_dry_run(fresh_catalog, registry, method, exists):
    failures = dry_run_failures(fresh_catalog, registry, method, exists)
    assert not failures, method + " sends statements a dry run would refuse: " + " ;; ".join(failures)


def test_the_harness_sends_a_floor_of_distinct_statements(fresh_catalog, registry):
    seen, tables = set(), set()
    for method in CALLS:
        client = run_method(fresh_catalog, registry, method, True)
        fresh_catalog._CATALOG.clear()
        for sql, _ in client.statements:
            seen.add(sql)
            import sqlglot

            for tree in sqlglot.parse(sql, read="bigquery"):
                tables |= {f"{d}.{n}" for d, n, _ in hb.referenced_tables(tree) if d}
    assert len(seen) >= 55, len(seen)
    assert len(tables) >= 25, sorted(tables)


class TestTheDryRunCanFail:
    def test_a_table_no_sql_file_creates_is_a_problem_not_a_skip(self, registry):
        sql = "SELECT 1 FROM `ogilvy-trends-v2.intelligence_42_core.no_such_table` t"
        problems = hb.check_statement(sql, set(), registry)
        assert [p.kind for p in problems] == ["table"] and "no_such_table" in problems[0].detail

    def test_an_unqualified_table_is_a_problem(self, registry):
        assert [p.kind for p in hb.check_statement("SELECT 1 FROM posts p", set(), registry)] == ["table"]

    def test_a_column_the_table_does_not_have_is_a_problem(self, registry):
        sql = "SELECT p.not_a_column FROM `ogilvy-trends-v2.intelligence_42_core.posts` p"
        assert [p.kind for p in hb.check_statement(sql, set(), registry)] == ["column"]

    def test_a_real_column_passes(self, registry):
        sql = "SELECT p.post_id, p.creator_id FROM `ogilvy-trends-v2.intelligence_42_core.posts` p WHERE p.post_id = @id"
        assert hb.check_statement(sql, {"id"}, registry) == []

    def test_a_parameter_that_was_not_bound_is_a_problem(self, registry):
        sql = "SELECT p.post_id FROM `ogilvy-trends-v2.intelligence_42_core.posts` p WHERE p.post_id = @id"
        assert [p.kind for p in hb.check_statement(sql, set(), registry)] == ["parameter"]

    def test_a_parameter_name_inside_a_string_is_not_a_parameter(self, registry):
        sql = "SELECT 'a@b.co' AS e FROM `ogilvy-trends-v2.intelligence_42_core.posts` p"
        assert hb.check_statement(sql, set(), registry) == []

    def test_a_cte_is_not_mistaken_for_a_table(self, registry):
        sql = ("WITH x AS (SELECT p.post_id FROM `ogilvy-trends-v2.intelligence_42_core.posts` p) "
               "SELECT x.post_id FROM x")
        assert hb.check_statement(sql, set(), registry) == []

    def test_information_schema_is_not_a_missing_table(self, registry):
        sql = "SELECT t.table_name FROM `ogilvy-trends-v2.intelligence_42_core.INFORMATION_SCHEMA.TABLES` t"
        assert hb.check_statement(sql, set(), registry) == []

    def test_a_table_function_called_with_the_wrong_number_of_arguments_is_a_problem(self, registry):
        sql = "SELECT t.* FROM `ogilvy-trends-v2.intelligence_42_agent.tvf_item_timeseries`(@a, @b) t"
        assert [p.kind for p in hb.check_statement(sql, {"a", "b"}, registry)] == ["arity"]

    def test_text_that_does_not_parse_is_a_problem(self, registry):
        assert [p.kind for p in hb.check_statement("SELEC 1 FROM", set(), registry)] == ["parse"]

    def test_a_query_without_the_bytes_cap_is_a_failure(self, fresh_catalog, registry, monkeypatch):
        monkeypatch.setattr(fresh_catalog, "MAX_BYTES", 5_000_000_000)
        failures = dry_run_failures(fresh_catalog, registry, "briefs", True)
        assert failures and "bytes-billed cap" in failures[0]

    def test_a_method_the_harness_cannot_make_send_a_statement_is_a_failure(self, fresh_catalog, registry,
                                                                           monkeypatch):
        monkeypatch.setattr(fresh_catalog.BigQueryStore, "briefs", lambda self, date: [])
        failures = dry_run_failures(fresh_catalog, registry, "briefs", True)
        assert failures and "sent no statement" in failures[0]

    def test_the_live_runner_reports_a_missing_table_as_a_failure(self):
        class NotFound(Exception):
            pass

        class Client:
            def query(self, sql, job_config=None):
                if "missing" in sql:
                    raise NotFound("404 Not found: Table ogilvy-trends-v2:intelligence_42_core.missing")

        problems = hb.live_dry_run(Client(), [("a", "SELECT 1"), ("b", "SELECT 1 FROM missing")], lambda label: None)
        assert [label for label, _ in problems] == ["b"] and "NotFound" in problems[0][1]

    def test_a_catalog_that_lists_nothing_takes_the_missing_object_branches(self, fresh_catalog, registry):
        client = run_method(fresh_catalog, registry, "item_gate", False)
        assert all("v_item_gate_current" not in sql for sql, _ in client.statements)
        fresh_catalog._CATALOG.clear()
        client = run_method(fresh_catalog, registry, "item_gate", True)
        assert any("v_item_gate_current" in sql for sql, _ in client.statements)


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_live_dry_run_of_every_statement_fails_on_a_missing_table(fresh_catalog, registry):
    from google.cloud import bigquery

    live = bigquery.Client(project=hb.PROJECT)
    statements = []
    for method in CALLS:
        client = run_method(fresh_catalog, registry, method, True)
        fresh_catalog._CATALOG.clear()
        statements += [((method, i), sql) for i, (sql, _) in enumerate(client.statements)
                       if "INFORMATION_SCHEMA" not in sql]
    problems = hb.live_dry_run(live, statements, lambda label: bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False))
    assert not problems, problems
