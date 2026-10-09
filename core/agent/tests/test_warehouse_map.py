"""Ask's researcher is told the warehouse's real tables, and a failed tool shows in plain words.

Live staging, 4 October: a TikTok sounds question ran sql_query five times and every call failed, because the model
was never told a table name and guessed ("items", "detection tables"). Each failure showed as "sql_query did not run".
"""

import logging
import re
from datetime import datetime
from pathlib import Path

import pytest

from core.agent import ask, toolset
from core.agent import plain
from core.agent.context import RunContext
from core.agent.tests.test_sql_tool import FakeWarehouse
from core.agent.tools import sql_query as sql_query_module
from core.agent.tools.sql_query import ALLOWED_TVFS, HIDDEN_TABLES, WAREHOUSE_MAP, check_sql, sql_query

ROOT = Path(__file__).resolve().parents[3]
DDL_FILES = [ROOT / "core/schema/core.sql", ROOT / "core/schema/agent.sql", ROOT / "core/detect/sql/agent_views.sql",
             ROOT / "core/schema/google_search_signals.sql", ROOT / "core/detect/sql/views.sql"]


def ddl_columns() -> dict[str, set[str]]:
    """dataset.name -> columns, read from the DDL the warehouse is built from (tables, ALTER ADD COLUMN, views by
    their select list are left out: the map names only tables and the one table function)."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in DDL_FILES)
    text = text.replace("{core}", "intelligence_42_core").replace("{agent}", "intelligence_42_agent")
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2\.(\w+\.\w+)` \(", text):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        body = text[m.end():i - 1]
        while re.search(r"<[^<>]*>", body):
            body = re.sub(r"<[^<>]*>", "", body)
        out[m.group(1)] = {c.strip().split()[0] for c in body.split(",") if c.strip()}
    for m in re.finditer(r"ALTER TABLE `ogilvy-trends-v2\.(\w+\.\w+)`\s*ADD COLUMN IF NOT EXISTS (\w+)", text):
        out.setdefault(m.group(1), set()).add(m.group(2))
    # A current view is its base table's columns: SELECT s.* FROM base s JOIN v_good_runs g ... (views.sql 3.1).
    for m in re.finditer(r"CREATE OR REPLACE VIEW (\w+\.v_\w+_current) AS\s+SELECT s\.\* FROM (\w+\.\w+) s\s", text):
        if m.group(2) in out:
            out[m.group(1)] = set(out[m.group(2)])
    return out


def test_the_sql_tool_description_names_the_tables_the_model_may_read():
    described = toolset.DESCRIPTIONS["sql_query"]
    for table in WAREHOUSE_MAP:
        assert table in described
    assert "intelligence_42_core.posts" in described and "sound_id" in described


def test_every_table_and_column_in_the_map_exists_in_the_warehouse_ddl():
    ddl = ddl_columns()
    for table, columns in WAREHOUSE_MAP.items():
        if "(" in table:  # the table function, whose name the guard allowlists
            assert table.split("(")[0] in ALLOWED_TVFS
            continue
        assert table in ddl, table
        missing = set(columns) - ddl[table]
        assert not missing, (table, missing)


def test_the_map_names_no_hidden_table():
    described = toolset.DESCRIPTIONS["sql_query"]
    assert not any(hidden in described for hidden in HIDDEN_TABLES)


def test_the_example_query_in_the_description_passes_the_guard():
    example = toolset.DESCRIPTIONS["sql_query"].split("Example: ", 1)[1]
    check_sql(example)
    # The model's params arrive as JSON strings, and BigQuery will not compare a DATE column to a STRING param.
    assert "DATE(@since)" in example and "DATE(@until)" in example


# Collect stores about 1,500 Google search terms a day in google_search_signals, but the map left it out, so Ask paid
# for a live Google Trends call instead (lead thread, 4 October). Readable as search interest, never as evidence.
SIGNALS = "intelligence_42_core.google_search_signals"
SIGNALS_SQL = (f"SELECT term, source, rank FROM {SIGNALS} WHERE market = @market "
               "AND DATE(fetched_at) BETWEEN DATE(@since) AND DATE(@until) ORDER BY rank LIMIT 100")


def test_the_map_names_the_stored_google_search_terms_as_context_never_evidence():
    described = toolset.DESCRIPTIONS["sql_query"]
    assert SIGNALS in described
    assert {"market", "term", "source", "kind", "rank", "refreshed_at"} <= set(WAREHOUSE_MAP[SIGNALS])
    assert "never evidence" in described.split("google_search_signals holds", 1)[1]


def test_the_map_names_every_source_collect_stores_in_google_search_signals():
    from core.api.searching import SOURCES
    from core.collect import google_rss
    described = toolset.DESCRIPTIONS["sql_query"].split("google_search_signals holds", 1)[1].split("never evidence")[0]
    assert google_rss.SOURCE == "google_rss" and set(SOURCES) == {"google_bq", "google_trending", "google_rss"}
    for source in SOURCES:
        assert source in described, source


def test_a_search_terms_query_gets_no_query_id_so_no_claim_can_cite_it():
    ctx = RunContext(run_id="run_test", tier="T0", as_of=datetime(2026, 10, 4, 6, 0))
    check_sql(SIGNALS_SQL)
    out = sql_query(ctx, FakeWarehouse(rows=[{"term": "springboks", "source": "google_trending", "rank": 1}],
                                       tables=(f"ogilvy-trends-v2.{SIGNALS}",)), SIGNALS_SQL, purpose="search terms")
    assert out["rows"] == [{"term": "springboks", "source": "google_trending", "rank": 1}]
    assert out["query_id"] is None and out["signals"]["signal_type"] == "search_interest"
    assert ctx.queries == {}


def test_a_join_of_posts_with_search_terms_is_context_too():
    ctx = RunContext(run_id="run_test", tier="T0", as_of=datetime(2026, 10, 4, 6, 0))
    out = sql_query(ctx, FakeWarehouse(tables=("ogilvy-trends-v2.intelligence_42_core.posts",
                                               f"ogilvy-trends-v2.{SIGNALS}")), "SELECT 1 AS x", purpose="x")
    assert out["query_id"] is None and ctx.queries == {}


def test_a_posts_query_is_still_recorded_for_citing():
    ctx = RunContext(run_id="run_test", tier="T0", as_of=datetime(2026, 10, 4, 6, 0))
    out = sql_query(ctx, FakeWarehouse(), "SELECT 1 AS x", purpose="x")
    assert out["query_id"] in ctx.queries


def test_the_search_terms_table_reads_in_plain_words():
    assert plain.tables(SIGNALS_SQL) == "Google search terms"


TOOLS = toolset.TOOL_NAMES


@pytest.mark.parametrize("tool", TOOLS)
def test_a_failed_tool_shows_plain_words_never_the_tool_name(tool):
    line = ask.failed_step(tool, "Refused: Table items must be dataset-qualified")
    assert not line.startswith(f"{tool} did not run") and "_" not in line
    assert line.endswith("did not run") and "dataset" not in line


def test_the_failed_sql_step_says_a_warehouse_count_did_not_run():
    assert ask.failed_step("sql_query", "x") == "A warehouse count did not run"


def test_an_unknown_tool_still_reads_plainly():
    assert ask.failed_step("run_shell", "x") == "A research step did not run"


def test_the_failure_detail_goes_to_the_log_for_diagnosis(caplog):
    with caplog.at_level(logging.WARNING, logger="f42-agent"):
        ask.failed_step("sql_query", "Refused: Dataset trends_v2_staging is not allowed.", run_id="r_test")
    assert any("sql_query" in r.getMessage() and "trends_v2_staging" in r.getMessage() and "r_test" in r.getMessage()
               for r in caplog.records)


def test_a_tool_error_through_the_default_research_shows_the_plain_line():
    from core.agent.tests.test_ask import consume_error
    assert consume_error("Refused: Table items must be dataset-qualified") == ["A warehouse count did not run"]


def test_the_gemini_loop_shows_the_plain_line(monkeypatch):
    from core.agent.tests.test_gemini_research import FakeClient, fcall, ftext, reply, run

    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "10")
    client = FakeClient(reply(fcall("sql_query", sql="SELECT COUNT(*) FROM items", purpose="sounds")),
                        reply(ftext("ok")))
    _, _, events = run(monkeypatch, client)
    notes = [e["text"] for e in events if e["event"] == "step" and e["kind"] == "note"]
    assert notes == ["A warehouse count did not run"]
    answer = client.calls[1]["contents"][-1].parts[0].function_response.response
    assert "dataset-qualified" in answer["error"]  # the model still gets the reason, and the table names


def test_a_tool_that_keeps_failing_is_closed_and_the_model_told_to_finish(monkeypatch):
    # Live staging, 4 October: 39 steps of failed warehouse guesses and no answer.
    from core.agent import gemini_research
    from core.agent.tests.test_gemini_research import FakeClient, fcall, ftext, reply, run

    bad = lambda: reply(fcall("sql_query", sql="SELECT COUNT(*) FROM items", purpose="sounds"))  # noqa: E731
    client = FakeClient(*[bad() for _ in range(gemini_research.MAX_TOOL_FAILURES + 2)], reply(ftext("done")))
    result, ctx, events = run(monkeypatch, client)
    notes = [e["text"] for e in events if e["event"] == "step" and e["kind"] == "note"]
    assert notes.count("A warehouse count did not run") == gemini_research.MAX_TOOL_FAILURES
    assert notes.count("A warehouse count kept failing, so research carried on without it") == 1
    counted = [e for e in events if e["event"] == "step" and e["text"].startswith("Counting in the warehouse")]
    assert len(counted) == gemini_research.MAX_TOOL_FAILURES  # closed calls show no step
    last = client.calls[-1]["contents"][-1].parts[0].function_response.response["error"]
    assert "closed" in last and "Finish your note" in last
    assert result["note"] == "done"


def test_a_closed_tool_leaves_the_others_running_in_the_same_turn(monkeypatch):
    from core.agent import gemini_research
    from core.agent.tests.test_gemini_research import FakeClient, fcall, ftext, reply, run

    bad = lambda: reply(fcall("sql_query", sql="SELECT 1 FROM items", purpose="x"))  # noqa: E731
    turns = [bad() for _ in range(gemini_research.MAX_TOOL_FAILURES)]
    turns.append(reply(fcall("sql_query", sql="SELECT 1 FROM items", purpose="x"),
                       fcall("resolve_dates", expression="last 7 days")))
    client = FakeClient(*turns, reply(ftext("done")))
    run(monkeypatch, client)
    parts = client.calls[-1]["contents"][-1].parts
    assert [p.function_response.name for p in parts] == ["sql_query", "resolve_dates"]
    assert "closed" in parts[0].function_response.response["error"]
    assert "from" in parts[1].function_response.response["output"]


def test_a_write_tool_is_never_closed_however_often_it_is_refused(monkeypatch):
    from core.agent import gemini_research
    from core.agent.tests.test_gemini_research import FakeClient, fcall, ftext, reply, run

    save = lambda: reply(fcall("save_finding", claim="s", evidence_ids=["nope"], query_ids=[], item_ids=[]))  # noqa: E731
    client = FakeClient(*[save() for _ in range(gemini_research.MAX_TOOL_FAILURES + 1)], reply(ftext("done")))
    run(monkeypatch, client)
    last = client.calls[-1]["contents"][-1].parts[0].function_response.response
    assert "closed" not in str(last)


# N39. The map pointed the model at the raw, append-only item_daily and item_state tables and called their grain "one
# row per item, market and day". item_daily holds a row per platform, lane class and panel series plus one '_any' row
# that counts each post again, and every aggregate run appends its own rows, so a plain SUM(posts) was at least twice
# the truth. Pipeline readers use the v_*_current views, which keep the one good run per day.
RAW_DERIVED = re.compile(r"intelligence_42_core\.item_(daily|state)\b")
POSTS = "intelligence_42_core.posts"


def test_the_map_points_item_counts_at_the_current_views_never_the_raw_append_only_tables():
    described = toolset.DESCRIPTIONS["sql_query"]
    assert not RAW_DERIVED.search(described)
    assert not [name for name in WAREHOUSE_MAP if RAW_DERIVED.fullmatch(name)]
    assert "intelligence_42_core.v_item_daily_current" in WAREHOUSE_MAP
    assert "intelligence_42_core.v_item_state_current" in WAREHOUSE_MAP


def test_the_notes_do_not_state_the_false_grain():
    described = toolset.DESCRIPTIONS["sql_query"]
    assert "hold one row per item, market and day" not in described
    for sentence in described.split(". "):  # the grain holds for the state view, never for the daily one
        if "item_daily" in sentence:
            assert "one row per item, market and day" not in sentence


def test_the_notes_name_the_any_row_as_the_per_item_total_and_warn_against_summing_lanes():
    notes = " ".join(sql_query_module.WAREHOUSE_NOTES.split())
    assert "lane_class = '_any'" in notes and "platform = '_all'" in notes
    assert "never sum posts across" in notes


def test_the_posts_map_lists_geo_source_and_the_notes_carry_the_located_test_the_ask_layer_uses():
    from core.agent.tools import warehouse

    assert "geo_source" in WAREHOUSE_MAP[POSTS]
    notes = " ".join(sql_query_module.WAREHOUSE_NOTES.split())
    assert f"IFNULL(p.geo_source, '') != '{warehouse.PROFILE_SOURCE}'" in notes
    # The test the notes teach is the one the Ask layer's own store counts use.
    ctx = RunContext(run_id="run_test", tier="T0", as_of=datetime(2026, 10, 4, 6, 0), market="ZA")
    located = warehouse._store_scope(ctx, ["tiktok"])[3]
    assert "IFNULL(p.geo_source, '') != @profile_source" in located


def test_the_notes_no_longer_call_a_post_located_on_geo_market_alone():
    notes = " ".join(sql_query_module.WAREHOUSE_NOTES.split())
    assert "geo_market is ZA, NG or KE when a post is located" not in notes
    assert "home_market" in notes  # a creator-profile market is named as not located


def test_the_example_filters_on_the_located_test_not_geo_market_alone():
    example = sql_query_module.WAREHOUSE_EXAMPLE
    assert "p.geo_market = @market" in example
    assert "IFNULL(p.geo_source, '') != 'home_market'" in example
