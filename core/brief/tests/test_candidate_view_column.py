"""A new brief on the view a80's detect re-creates (W8-REL-B 3.6 prefix 'brief', carry item RB-C5, rulings WD2-1 and W1-6).

a80's detect re-creates v_item_market_scope on every run without market_news_posts7. The brief that ranks on that column then
fails in every market. _candidate_rows reads a third statement with CAST(NULL AS INT64) in its place when the error names the
column, so market-scope rows rank in the third tier for that run, and it records the missing column as its own reason: it is
not a locality view fault, so it is never printed or counted as locality_view_read_failed, and under v2 it holds nothing."""
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from core.brief import job
from core.brief.tests.test_brief_golden_path import ITEM, HonestModel, brief, world
from core.brief.tests.test_locality_authority import v2_world
from core.brief.tests.test_locality_shadow import add_locality
from core.detect import sqlrun
from core.detect.tests import duck

ROOT = Path(__file__).resolve().parents[3]
A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
DAY = date(2026, 10, 7)
COLUMN = "ms.market_news_posts7"
MISSING = "Unrecognized name: market_news_posts7"
BIGQUERY_MISSING = "Name market_news_posts7 not found inside ms at [66:49]"


def a80_views_sql():
    shown = subprocess.run(["git", "show", f"{A80}:core/detect/sql/views.sql"], cwd=ROOT, capture_output=True)
    if shown.returncode != 0:
        pytest.fail(f"commit {A80} is not available in this checkout")
    return shown.stdout.decode("utf-8")


def with_a80_scope_view(con):
    """What a80's detect job leaves behind: v_item_market_scope without market_news_posts7."""
    statements = [s for s in sqlrun.split(sqlrun.render(a80_views_sql(), "core", "agent")) if "v_item_market_scope" in s.split("AS", 1)[0]]
    assert len(statements) == 1
    con.execute(duck.create_statement(sqlrun._strip_leading_comments(statements[0])))
    assert "market_news_posts7" not in [r[0] for r in con.execute("DESCRIBE core.v_item_market_scope").fetchall()]
    return con


def test_the_statement_that_reads_a_null_column_is_the_first_with_only_that_column_replaced():
    first = job.QUERIES["candidates"]
    third = job.QUERIES["candidates_without_news_column"]
    assert first.count(COLUMN) == 2 and COLUMN not in third and third.count("CAST(NULL AS INT64)") == 2
    assert first.replace(COLUMN, "CAST(NULL AS INT64)") == third
    assert "v_item_locality_current" in third      # the locality view is still read: only the column is missing
    both = job.QUERIES["candidates_without_locality_or_news_column"]
    assert both == job.QUERIES["candidates_without_locality"].replace(COLUMN, "CAST(NULL AS INT64)")
    assert COLUMN in job.QUERIES["candidates_without_locality"]       # the second statement alone still reads it


def test_the_head_statements_fail_on_a80s_view_and_the_null_statement_binds():
    con = with_a80_scope_view(duck.connect())
    params = {"d": __import__("core.detect.tests.fixtures", fromlist=["D"]).D, "market": "ZA"}
    for name in ("candidates", "candidates_without_locality"):
        with pytest.raises(Exception, match="market_news_posts7"):
            duck.query(con, job.query_sql(name), params)
    for name in ("candidates_without_news_column", "candidates_without_locality_or_news_column"):
        duck.query(con, job.query_sql(name), params)


class Calls:
    """_query stub: raises the scripted error for a statement name, else returns the name's rows."""

    def __init__(self, errors):
        self.errors, self.names = errors, []

    def __call__(self, client, name, params, core, agent, receipt=None):
        self.names.append(name)
        if receipt is not None:
            receipt["from_" + name] = True
        if name in self.errors:
            raise RuntimeError(self.errors[name])
        return [{"statement": name}]


@pytest.mark.parametrize("message", [MISSING, BIGQUERY_MISSING])
@pytest.mark.parametrize("authority", ["v1", "v2"])
def test_a_missing_column_reads_the_null_statement_records_its_own_reason_and_holds_nothing(monkeypatch, capsys, message, authority):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, authority)
    calls = Calls({"candidates": message})
    monkeypatch.setattr(job, "_query", calls)
    failed = {}
    receipt = {}
    rows = job._candidate_rows(None, DAY, "ZA", "core", "agent", receipt, failed)
    assert receipt == {"from_candidates_without_news_column": True}      # the failed read leaves nothing in the receipt
    assert calls.names == ["candidates", "candidates_without_news_column"]
    assert rows == [{"statement": "candidates_without_news_column"}]
    assert failed == {"ZA": "missing_view_column"}
    err = capsys.readouterr().err
    assert "missing_view_column" in err and "locality_view_read_failed" not in err


def test_a_locality_fault_is_still_a_locality_fault_and_a_non_column_error_is_not_mistaken_for_the_column(monkeypatch, capsys):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    calls = Calls({"candidates": "Not found: Table v_item_locality_current"})
    monkeypatch.setattr(job, "_query", calls)
    failed = {}
    assert job._candidate_rows(None, DAY, "ZA", "core", "agent", {}, failed) == [{"statement": "candidates_without_locality"}]
    assert calls.names == ["candidates", "candidates_without_locality"] and failed == {"ZA": "read_without_view"}
    err = capsys.readouterr().err
    assert "locality_view_read_failed" in err and "missing_view_column" not in err
    set_locality_authority(monkeypatch, "v2")
    calls = Calls({"candidates": "Not found: Table v_item_locality_current"})
    monkeypatch.setattr(job, "_query", calls)
    failed = {}
    assert job._candidate_rows(None, DAY, "ZA", "core", "agent", {}, failed) == [] and failed == {"ZA": "held"}
    assert calls.names == ["candidates"]


def test_a_locality_fault_that_hides_the_missing_column_falls_to_the_statement_without_both(monkeypatch, capsys):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    calls = Calls({"candidates": "Not found: Table v_item_locality_current", "candidates_without_locality": MISSING})
    monkeypatch.setattr(job, "_query", calls)
    failed = {}
    rows = job._candidate_rows(None, DAY, "ZA", "core", "agent", {}, failed)
    assert calls.names == ["candidates", "candidates_without_locality", "candidates_without_locality_or_news_column"]
    assert rows == [{"statement": "candidates_without_locality_or_news_column"}]
    assert failed == {"ZA": job.BOTH_VIEW_FAULTS} == {"ZA": "read_without_view+missing_view_column"}
    err = capsys.readouterr().err
    assert "locality_view_read_failed" in err and "missing_view_column" in err      # both faults are labelled


def test_an_unrelated_error_from_the_statement_without_the_view_is_not_swallowed(monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    calls = Calls({"candidates": "Not found: Table v_item_locality_current", "candidates_without_locality": "quota exceeded"})
    monkeypatch.setattr(job, "_query", calls)
    with pytest.raises(RuntimeError, match="quota exceeded"):
        job._candidate_rows(None, DAY, "ZA", "core", "agent", {}, {})
    assert calls.names == ["candidates", "candidates_without_locality"]


# RJ-5: the null statement serves only a missing column, and its own failure holds the market

DUCKDB_MISSING = 'Binder Error: Table "ms" does not have a column named "market_news_posts7"'
NOT_A_MISSING_COLUMN = [
    "Resources exceeded during query execution: The query could not be executed in the allotted memory ... ms.market_news_posts7",
    "Query exceeded the deadline reading ms.market_news_posts7",
    "Unrecognized name: market_news_posts70",
    "Unrecognized name: market_news_posts",
    "Access Denied: Table core.v_item_market_scope: column market_news_posts7",
]


@pytest.mark.parametrize("message", [MISSING, BIGQUERY_MISSING, DUCKDB_MISSING])
def test_rj5_each_of_the_three_missing_column_wordings_serves_the_null_statement(monkeypatch, message):
    calls = Calls({"candidates": message})
    monkeypatch.setattr(job, "_query", calls)
    failed = {}
    assert job._candidate_rows(None, DAY, "ZA", "core", "agent", {}, failed) == [{"statement": "candidates_without_news_column"}]
    assert failed == {"ZA": "missing_view_column"}


@pytest.mark.parametrize("message", NOT_A_MISSING_COLUMN)
@pytest.mark.parametrize("authority", ["v1", "v2"])
def test_rj5_an_error_that_only_names_the_column_does_not_serve_the_null_statement(monkeypatch, capsys, message, authority):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, authority)
    calls = Calls({"candidates": message})
    monkeypatch.setattr(job, "_query", calls)
    failed = {}
    rows = job._candidate_rows(None, DAY, "ZA", "core", "agent", {}, failed)
    assert "candidates_without_news_column" not in calls.names and "candidates_without_locality_or_news_column" not in calls.names
    if authority == "v1":
        assert rows == [{"statement": "candidates_without_locality"}] and failed == {"ZA": "read_without_view"}
    else:
        assert rows == [] and failed == {"ZA": "held"}
    assert "missing_view_column" not in capsys.readouterr().err


@pytest.mark.parametrize("authority", ["v1", "v2"])
@pytest.mark.parametrize("message", ["quota exceeded", "Not found: Table v_item_locality_current was not found"])
def test_rj5_the_null_statement_failing_on_its_own_is_caught_recorded_under_its_own_reason_and_holds_the_market(
        monkeypatch, capsys, authority, message):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, authority)
    calls = Calls({"candidates": MISSING, "candidates_without_news_column": message})
    monkeypatch.setattr(job, "_query", calls)
    failed, receipt = {}, {}
    assert job._candidate_rows(None, DAY, "ZA", "core", "agent", receipt, failed) == []
    assert calls.names == ["candidates", "candidates_without_news_column"]
    assert failed == {"ZA": job.NULL_COLUMN_READ_FAILED} == {"ZA": "null_column_statement_failed"}
    assert receipt == {}
    err = capsys.readouterr().err
    assert "null_column_statement_failed" in err and "locality_view_read_failed" not in err


def test_rj5_the_statement_without_both_views_failing_on_its_own_is_held_the_same_way(monkeypatch, capsys):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    calls = Calls({"candidates": "Not found: Table v_item_locality_current", "candidates_without_locality": MISSING,
                   "candidates_without_locality_or_news_column": "quota exceeded"})
    monkeypatch.setattr(job, "_query", calls)
    failed = {}
    assert job._candidate_rows(None, DAY, "ZA", "core", "agent", {}, failed) == []
    assert failed == {"ZA": job.NULL_COLUMN_READ_FAILED}
    assert "null_column_statement_failed" in capsys.readouterr().err


def test_rj5_a_brief_whose_null_statement_fails_holds_every_market_with_a_data_issue_and_does_not_fail_the_run(monkeypatch, capsys):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    con = with_a80_scope_view(world(located=True))
    real = job._query

    def refuse_null(client, name, params, core, agent, receipt=None):
        if name == "candidates_without_news_column":
            raise RuntimeError("quota exceeded")
        return real(client, name, params, core, agent, receipt=receipt)

    monkeypatch.setattr(job, "_query", refuse_null)
    counts, payload, _ = brief(con, HonestModel(True), monkeypatch)
    assert payload["cards"] == [] and payload["status"] == "data_issue"
    assert any(b["kind"] == "data_issue" for b in payload["banners"])
    assert counts["view_column_read_failed"] == {"markets": list(job.MARKETS), "column": "market_news_posts7", "action": "held"}
    assert "view_column_missing" not in counts and "locality_view_failed" not in counts
    assert "null_column_statement_failed" in capsys.readouterr().err


def test_a_brief_run_on_a80s_view_publishes_and_records_the_missing_column_not_a_locality_failure(monkeypatch, capsys):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    con = with_a80_scope_view(world(located=True))
    counts, payload, _ = brief(con, HonestModel(True), monkeypatch)
    assert [c["item_id"] for c in payload["cards"]] == [ITEM]
    assert counts["view_column_missing"] == {"markets": list(job.MARKETS), "column": "market_news_posts7", "action": "null_column"}
    assert "locality_view_failed" not in counts
    err = capsys.readouterr().err
    assert "missing_view_column" in err and "locality_view_read_failed" not in err


def test_the_same_run_on_the_head_view_records_nothing_and_ranks_as_before(monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    counts, payload, _ = brief(world(located=True), HonestModel(True), monkeypatch)
    assert "view_column_missing" not in counts and "locality_view_failed" not in counts
    assert [c["item_id"] for c in payload["cards"]] == [ITEM]


def test_under_v2_a_missing_column_does_not_hold_the_market_as_a_locality_failure_would(monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v2")
    con = v2_world(status="local")
    add_locality(con, 20, 20)
    with_a80_scope_view(con)
    counts, payload, _ = brief(con, HonestModel(True), monkeypatch)
    assert payload["status"] != "data_issue" and [c["item_id"] for c in payload["cards"]] == [ITEM]
    assert counts["view_column_missing"]["action"] == "null_column" and "locality_view_failed" not in counts


def test_with_the_locality_view_and_the_scope_column_both_absent_the_third_statement_serves_and_both_faults_are_labelled(monkeypatch, capsys):
    """Live staging at b349a24 (DRYRUNS-6): the first read fails on v_item_locality_current, the retry on market_news_posts7."""
    from core.brief.tests.test_locality_shadow import RefusingClient
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    con = with_a80_scope_view(world(located=True))
    add_locality(con)
    monkeypatch.setattr("core.brief.tests.test_brief_golden_path.Client", RefusingClient)
    counts, payload, _ = brief(con, HonestModel(True), monkeypatch)
    assert [c["item_id"] for c in payload["cards"]] == [ITEM]
    assert counts["view_column_missing"] == {"markets": list(job.MARKETS), "column": "market_news_posts7", "action": "null_column"}
    assert counts["locality_view_failed"] == {"markets": list(job.MARKETS), "action": "read_without_view"}
    err = capsys.readouterr().err
    assert "locality_view_read_failed" in err and "missing_view_column" in err
