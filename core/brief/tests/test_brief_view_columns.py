"""Release B carry item RB-C2: the brief's candidates statement names no view column that the views.sql of the same commit does
not give. On the B image the brief binds only after detect has re-created the views in the same chain, so a column the head
brief reads and the head views lack would fail the first B chain's brief. The views are built on the DuckDB harness from the
tree's own views.sql and locality_views.sql and described; the references are read from the statement's text."""
import re

import pytest

from core.brief import job
from core.detect import sqlrun
from core.detect.tests import duck

VIEW_JOIN = re.compile(r"(?:FROM|JOIN)\s+\{core\}\.(v_\w+)\s+(\w+)")
REFERENCE = re.compile(r"\b([a-z]\w*)\.([a-z_]\w*)\b")


def code(sql):
    return re.sub(r"--[^\n]*", "", sql)


def view_columns(con, view):
    return {r[0] for r in con.execute(f"DESCRIBE core.{view}").fetchall()}


def aliases(sql):
    """{alias: view} for every view the statement reads, and the aliases that mean two different things."""
    found, clash = {}, set()
    for view, alias in VIEW_JOIN.findall(sql):
        if found.get(alias, view) != view:
            clash.add(alias)
        found[alias] = view
    return found, clash


def missing_columns(sql, con):
    mapping, clash = aliases(code(sql))
    assert not clash, f"alias used for two views: {clash}"
    columns = {view: view_columns(con, view) for view in set(mapping.values())}
    return sorted({f"{mapping[a]}.{c}" for a, c in REFERENCE.findall(code(sql).replace("{core}.", "core_")) if a in mapping and c not in columns[mapping[a]]})


@pytest.fixture(scope="module")
def con():
    return duck.connect()


def test_rbc2_the_candidates_statement_reads_the_views_it_is_expected_to(con):
    mapping, clash = aliases(code(job.QUERIES["candidates"]))
    assert not clash
    assert mapping["ms"] == "v_item_market_scope" and mapping["s"] == "v_item_state_current" and mapping["lo"] == "v_item_locality_current"


@pytest.mark.parametrize("name", ["candidates", "candidates_without_locality"])
def test_rbc2_every_view_column_the_candidates_statement_names_is_in_the_views_of_the_same_commit(con, name):
    sql = job.QUERIES[name]
    refs = [(a, c) for a, c in REFERENCE.findall(code(sql)) if a == "ms"]
    assert {"market_scope", "market_posts7", "market_news_posts7", "total_posts7"} <= {c for _, c in refs}   # the scan sees the column RB-C2 is about
    assert missing_columns(sql, con) == []


def test_rbc2_the_null_column_variants_name_no_scope_view_column_the_views_lack(con):
    for name in ("candidates_without_news_column", "candidates_without_locality_or_news_column"):
        assert missing_columns(job.QUERIES[name], con) == []
        assert "market_news_posts7" not in code(job.QUERIES[name])


def test_rbc2_a_view_without_the_column_is_found_with_a_name():
    from core.brief.tests.test_candidate_view_column import with_a80_scope_view

    a80_like = with_a80_scope_view(duck.connect())          # what a80's detect leaves: no market_news_posts7
    assert missing_columns(job.QUERIES["candidates"], a80_like) == ["v_item_market_scope.market_news_posts7"]
    assert missing_columns(job.QUERIES["candidates_without_news_column"], a80_like) == []


def test_rbc2_a_statement_that_reads_a_column_no_view_has_is_found(con):
    sql = "SELECT ms.no_such_column FROM {core}.v_item_market_scope ms"
    assert missing_columns(sql, con) == ["v_item_market_scope.no_such_column"]
