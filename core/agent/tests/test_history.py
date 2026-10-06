"""History tools (BUILD.md 3.3): history and analogues, run as real SQL on DuckDB fixture tables (transpiled from
BigQuery), plus the guard and parameter rules every agent query follows."""

import re
from datetime import date, datetime, timedelta

import pytest
import sqlglot
from sqlglot import exp

from core.agent.context import Refused, RunContext
from core.agent.tools.history import analogues, history
from core.agent.tools.sql_query import check_sql

AS_OF = datetime(2026, 9, 28, 6, 0)


def tables_in(sql):
    tree = sqlglot.parse_one(sql, dialect="bigquery")
    return sorted({f"{t.db}.{t.name}" for t in tree.find_all(exp.Table) if t.db})


class DuckWarehouse:
    """The Warehouse protocol over DuckDB: each query is transpiled from BigQuery and run on fixture tables. fail maps
    a table name to an exception raised for any query that reads it, as a missing view or column would."""

    def __init__(self, con, fail=None):
        self.con, self.fail = con, fail or {}
        self.runs = []

    def dry_run(self, sql, params):
        return {"bytes": 1_000, "tables": [f"ogilvy-trends-v2.{t}" for t in tables_in(sql)]}

    def run(self, sql, params, max_bytes_billed):
        self.runs.append((sql, params))
        for table, error in self.fail.items():
            if table in tables_in(sql):
                raise error
        duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
        used = {k: v for k, v in (params or {}).items() if re.search(rf"\${k}\b", duck)}
        cur = self.con.execute(duck, used)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


WAVES = [
    # item, market, start, end, peak day, peak posts
    ("i_heritage", "ZA", date(2024, 9, 10), date(2024, 9, 30), date(2024, 9, 24), 40),
    ("i_heritage", "ZA", date(2025, 9, 12), date(2025, 9, 29), date(2025, 9, 23), 55),
    ("i_heritage", "ZA", date(2026, 9, 15), date(2026, 9, 27), date(2026, 9, 24), 61),
    ("i_heritage", "NG", date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 2), 3),
    ("i_amapiano", "ZA", date(2026, 3, 1), date(2026, 3, 20), date(2026, 3, 9), 12),
    ("i_amapiano", "ZA", date(2026, 8, 1), date(2026, 8, 30), date(2026, 8, 18), 30),
    ("i_braai", "ZA", date(2026, 9, 20), date(2026, 9, 27), date(2026, 9, 26), 9),
    ("i_once", "KE", date(2026, 9, 1), date(2026, 9, 5), date(2026, 9, 4), 20),
]
MAP = [
    # item, kind, key, label, aliases, centroid, valid_from, valid_to
    ("i_heritage", "hashtag", "heritageday", "Heritage Day braai", ["shisa nyama"], [1.0, 0.0, 0.0],
     datetime(2026, 1, 1), None),
    ("i_heritage", "hashtag", "heritageday", "Old label", [], [0.0, 0.0, 1.0], datetime(2025, 1, 1),
     datetime(2026, 1, 1)),
    ("i_braai", "topic", "braai", "Braai season", [], [0.9, 0.1, 0.0], datetime(2026, 1, 1), None),
    ("i_amapiano", "sound", "amapiano", "Amapiano Friday", [], [0.0, 1.0, 0.0], datetime(2026, 1, 1), None),
    ("i_once", "meme", "once", "Nairobi traffic memes", [], [0.6, 0.6, 0.0], datetime(2026, 1, 1), None),
    ("i_nocentroid", "topic", "braaimaster", "Braai master", [], [], datetime(2026, 1, 1), None),
]
CALENDAR = [
    (date(2026, 9, 24), "ZA", "Heritage Day", "holiday", "nager", ["i_heritage"]),
    (date(2026, 12, 25), "ZA", "Christmas Day", "holiday", "nager", []),
    (date(2026, 9, 4), "KE", "Some KE moment", "hand", "moments.yaml", []),
]
ANALOGUES = [
    # moment, market, name, analogue date, status, computed at
    (date(2026, 9, 24), "ZA", "Heritage Day", date(2025, 9, 24), "stale_row", datetime(2026, 9, 1)),
    (date(2026, 9, 24), "ZA", "Heritage Day", date(2025, 9, 24), "found", datetime(2026, 9, 20)),
]


def fixture_con(waves=WAVES, cmap=MAP, calendar=CALENDAR, analogue_rows=ANALOGUES):
    import duckdb  # test-only: runs the tools' SQL, transpiled from BigQuery, on fixture tables

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.v_item_waves (item_id VARCHAR, market VARCHAR, wave_start DATE, "
                "wave_end DATE, peak_date DATE, peak_posts BIGINT)")
    con.execute("CREATE TABLE intelligence_42_core.cultural_map (item_id VARCHAR, kind VARCHAR, canonical_key VARCHAR, "
                "label VARCHAR, aliases VARCHAR[], centroid DOUBLE[], valid_from TIMESTAMP, valid_to TIMESTAMP)")
    con.execute("CREATE TABLE intelligence_42_core.calendar (moment_date DATE, market VARCHAR, name VARCHAR, "
                "kind VARCHAR, source VARCHAR, item_ids VARCHAR[])")
    con.execute("CREATE TABLE intelligence_42_core.calendar_analogues (moment_date DATE, market VARCHAR, "
                "name VARCHAR, analogue_date DATE, status VARCHAR, computed_at TIMESTAMP)")
    con.executemany("INSERT INTO intelligence_42_core.v_item_waves VALUES (?, ?, ?, ?, ?, ?)", waves)
    con.executemany("INSERT INTO intelligence_42_core.cultural_map VALUES (?, ?, ?, ?, ?, ?, ?, ?)", cmap)
    con.executemany("INSERT INTO intelligence_42_core.calendar VALUES (?, ?, ?, ?, ?, ?)", calendar)
    con.executemany("INSERT INTO intelligence_42_core.calendar_analogues VALUES (?, ?, ?, ?, ?, ?)", analogue_rows)
    return con


@pytest.fixture
def ctx():
    return RunContext(run_id="run_test", tier="T0", as_of=AS_OF)


@pytest.fixture
def wh():
    return DuckWarehouse(fixture_con())


def assert_guarded(wh):
    """Every query passes the agent's SQL guard, reads only the two allowed datasets and binds scalar parameters
    only (BigQueryWarehouse binds str, int, float, bool, date and datetime; never a list or None)."""
    assert wh.runs
    for sql, params in wh.runs:
        check_sql(sql)
        assert "VECTOR_SEARCH" not in sql.upper() and "ML." not in sql.upper()
        for value in (params or {}).values():
            assert isinstance(value, (str, int, float, bool, date, datetime)), value


# history


def test_history_by_item_id_returns_every_wave_with_its_query_id(ctx, wh):
    out = history(ctx, wh, item_id="i_heritage", market="ZA")
    waves = out["waves"]
    assert [(w["wave_start"], w["peak_date"], w["peak_posts"]) for w in waves] == [
        (date(2024, 9, 10), date(2024, 9, 24), 40),
        (date(2025, 9, 12), date(2025, 9, 23), 55),
        (date(2026, 9, 15), date(2026, 9, 24), 61),
    ]
    assert {w["market"] for w in waves} == {"ZA"}
    assert [w["latest"] for w in waves] == [False, False, True]
    assert all(w["label"] == "Heritage Day braai" for w in waves)  # the open cultural_map row, not the closed one
    assert all(w["counts_for_recurring"] for w in waves)
    # Each wave's numbers are pinned to the query that produced them, and that query is recorded on the run.
    assert {w["query_id"] for w in waves} == {out["query_id"]}
    assert ctx.queries[out["query_id"]]["rows"][0]["peak_posts"] == 40
    assert "intelligence_42_core.v_item_waves" in tables_in(ctx.queries[out["query_id"]]["sql"])
    assert_guarded(wh)


def test_history_marks_a_wave_that_peaked_near_the_same_date_a_year_before(ctx, wh):
    waves = history(ctx, wh, item_id="i_heritage", market="ZA")["waves"]
    # 2025-09-23 is within 7 days of 2024-09-24 a year on; 2026-09-24 within 7 days of 2025-09-23; the first has none.
    assert [w["same_time_last_year"] for w in waves] == [False, True, True]


def test_history_keeps_values_out_of_the_sql(ctx, wh):
    history(ctx, wh, item_id="i_heritage'; DROP TABLE x; --", market="ZA", since="2025-01-01")
    for sql, params in wh.runs:
        assert "DROP" not in sql
        assert "i_heritage" not in sql
    assert_guarded(wh)


def test_history_without_a_market_reads_every_market_and_ctx_market_is_the_default(wh):
    every = history(RunContext(run_id="r", tier="T0", as_of=AS_OF), wh, item_id="i_heritage")
    assert {w["market"] for w in every["waves"]} == {"ZA", "NG"}
    ng = history(RunContext(run_id="r", tier="T0", as_of=AS_OF, market="NG"), wh, item_id="i_heritage")
    assert [(w["market"], w["peak_posts"], w["counts_for_recurring"]) for w in ng["waves"]] == [("NG", 3, False)]


def test_history_since_keeps_waves_that_ended_on_or_after_it(ctx, wh):
    waves = history(ctx, wh, item_id="i_heritage", market="ZA", since="2025-09-29")["waves"]
    assert [w["wave_end"] for w in waves] == [date(2025, 9, 29), date(2026, 9, 27)]
    assert waves[0]["same_time_last_year"] is True  # the check still sees the 2024 wave the filter drops
    assert any(date(2025, 9, 29) in params.values() for _, params in wh.runs)


def test_history_by_query_finds_items_by_label_key_or_alias(ctx, wh):
    out = history(ctx, wh, query="braai", market="ZA")
    assert [i["item_id"] for i in out["items"]] == ["i_nocentroid", "i_braai", "i_heritage"]  # by label
    assert out["items"][0]["query_id"] in ctx.queries
    assert {w["item_id"] for w in out["waves"]} == {"i_heritage", "i_braai"}
    by_alias = history(ctx, wh, query="Shisa nyama", market="ZA")
    assert [i["item_id"] for i in by_alias["items"]] == ["i_heritage"]
    assert_guarded(wh)


def test_history_by_query_with_no_match_says_so(ctx, wh):
    out = history(ctx, wh, query="nothing like this", market="ZA")
    assert out["items"] == [] and out["waves"] == [] and out["moments"] == []
    assert "No item in 42's cultural map" in out["note"]


def test_history_links_waves_to_calendar_moments_and_last_years_analogue(ctx, wh):
    out = history(ctx, wh, item_id="i_heritage", market="ZA")
    moments = out["moments"]
    # The 2026 wave peaked on Heritage Day; the 2025 wave peaked a day before Heritage Day's analogue date.
    assert [(m["peak_date"], m["name"], m["moment_date"], m["analogue_date"]) for m in moments] == [
        (date(2025, 9, 23), "Heritage Day", date(2026, 9, 24), date(2025, 9, 24)),
        (date(2026, 9, 24), "Heritage Day", date(2026, 9, 24), date(2025, 9, 24)),
    ]
    assert all(m["linked"] for m in moments)                   # calendar.item_ids names the item
    assert all(m["analogue_status"] == "found" for m in moments)  # the newest calendar_analogues row wins
    assert all(m["query_id"] in ctx.queries and m["query_id"] != out["query_id"] for m in moments)
    assert out["query_ids"] == [out["query_id"], moments[0]["query_id"]]


def test_a_moment_near_a_peak_that_the_calendar_does_not_tie_to_the_item_is_shown_unlinked(ctx, wh):
    moments = history(ctx, wh, item_id="i_once", market="KE")["moments"]
    assert [(m["name"], m["linked"], m["analogue_date"]) for m in moments] == [("Some KE moment", False, None)]


def test_history_still_returns_waves_when_the_calendar_cannot_be_read(ctx):
    wh = DuckWarehouse(fixture_con(), fail={"intelligence_42_core.calendar_analogues": RuntimeError(
        "Not found: Table ogilvy-trends-v2:intelligence_42_core.calendar_analogues https://x.example/?key=1")})
    out = history(ctx, wh, item_id="i_heritage", market="ZA")
    assert len(out["waves"]) == 3 and out["moments"] == []
    assert out["calendar"] == "unavailable"
    assert "calendar_analogues" in out["calendar_reason"] and "https" not in out["calendar_reason"]
    assert out["query_ids"] == [out["query_id"]]


@pytest.mark.parametrize("args", [{}, {"item_id": ""}, {"query": "   "}, {"item_id": "i", "market": "US"},
                                  {"item_id": "i", "since": "last year"}])
def test_history_refusals_run_nothing(ctx, wh, args):
    with pytest.raises(Refused):
        history(ctx, wh, **args)
    assert wh.runs == [] and ctx.queries == {}


# analogues


def test_analogues_rank_by_exact_cosine_over_stored_centroids(ctx, wh):
    out = analogues(ctx, wh, item_id="i_heritage", market="ZA", k=3)
    assert out["method"] == "centroid" and "note" not in out
    ranked = out["analogues"]
    assert [a["item_id"] for a in ranked] == ["i_braai", "i_once", "i_amapiano"]  # no self, no empty centroid
    assert ranked[0]["distance"] == pytest.approx(1 - 0.9 / (0.9 ** 2 + 0.1 ** 2) ** 0.5)
    assert ranked[2]["distance"] == pytest.approx(1.0)
    assert ranked[0]["label"] == "Braai season" and ranked[0]["kind"] == "topic"
    assert {a["query_id"] for a in ranked} == {out["query_id"]}
    assert_guarded(wh)


def test_each_analogue_carries_its_past_waves_with_their_query_id(ctx, wh):
    out = analogues(ctx, wh, item_id="i_heritage", market="ZA", k=3)
    by_id = {a["item_id"]: a for a in out["analogues"]}
    assert [(w["peak_date"], w["peak_posts"]) for w in by_id["i_amapiano"]["waves"]] == [
        (date(2026, 3, 9), 12), (date(2026, 8, 18), 30)]
    assert by_id["i_once"]["waves"] == []  # its only wave is in KE
    wave_qid = by_id["i_amapiano"]["waves"][0]["query_id"]
    assert wave_qid != out["query_id"] and wave_qid in ctx.queries
    assert out["query_ids"] == [out["query_id"], wave_qid]


def test_analogues_k_is_a_parameter_capped_at_twenty(ctx, wh):
    analogues(ctx, wh, item_id="i_heritage", k=500)
    assert 20 in wh.runs[0][1].values()
    with pytest.raises(Refused):
        analogues(ctx, wh, item_id="i_heritage", k=0)


def test_analogues_fall_back_to_label_words_when_the_item_has_no_centroid_and_say_so(ctx, wh):
    out = analogues(ctx, wh, item_id="i_nocentroid", market="ZA")
    assert out["method"] == "keyword"
    assert "centroid" in out["note"] and "label" in out["note"]
    assert [a["item_id"] for a in out["analogues"]] == ["i_braai", "i_heritage"]
    assert all(a["shared_words"] == 1 and a["query_id"] == out["query_id"] for a in out["analogues"])
    assert_guarded(wh)


def test_analogues_fall_back_when_the_centroid_query_fails(ctx):
    wh = DuckWarehouse(fixture_con())
    real_run = wh.run

    def run(sql, params, cap):
        if "centroid" in sql:
            raise RuntimeError("Unrecognized name: centroid")
        return real_run(sql, params, cap)

    wh.run = run
    out = analogues(ctx, wh, item_id="i_heritage", market="ZA")
    assert out["method"] == "keyword"
    assert "Unrecognized name: centroid" in out["centroid_reason"]
    assert [a["item_id"] for a in out["analogues"]] == ["i_braai", "i_nocentroid"]


def test_analogues_from_text_anchor_on_the_best_matching_map_item(ctx, wh):
    out = analogues(ctx, wh, text="amapiano", market="ZA", k=2)
    assert out["item_id"] == "i_amapiano" and out["method"] == "centroid"
    assert [a["item_id"] for a in out["analogues"]] == ["i_once", "i_braai"]


def test_analogues_from_text_with_no_map_item_match_label_words(ctx, wh):
    out = analogues(ctx, wh, text="traffic in Lagos", market="KE")
    assert out["item_id"] is None and out["method"] == "keyword"
    assert [a["item_id"] for a in out["analogues"]] == ["i_once"]
    assert [w["peak_posts"] for w in out["analogues"][0]["waves"]] == [20]
    assert_guarded(wh)


@pytest.mark.parametrize("args", [{}, {"item_id": " ", "text": ""}, {"item_id": "i", "market": "GH"},
                                  {"item_id": "i", "k": "many"}])
def test_analogues_refusals_run_nothing(ctx, wh, args):
    with pytest.raises(Refused):
        analogues(ctx, wh, **args)
    assert wh.runs == []


# registration


def test_both_tools_are_registered_and_bound_to_the_warehouse(ctx, wh):
    from core.agent.toolset import DESCRIPTIONS, SCHEMAS, TOOL_NAMES, build_functions

    assert TOOL_NAMES[-2:] == ["history", "analogues"]
    functions = build_functions(ctx, wh, None, None)
    assert functions["history"](item_id="i_braai", market="ZA")["waves"][0]["peak_posts"] == 9
    assert functions["analogues"](item_id="i_heritage", k=1)["analogues"][0]["item_id"] == "i_braai"
    for name in ("history", "analogues"):
        assert DESCRIPTIONS[name]
        assert SCHEMAS[name]["additionalProperties"] is False
    assert SCHEMAS["history"]["properties"]["market"]["enum"] == ["ZA", "NG", "KE"]
    assert SCHEMAS["analogues"]["properties"]["k"] == {"type": "integer", "minimum": 1, "maximum": 20}


# review fixes


@pytest.mark.parametrize("posts, expected", [(2, False), (8, True)])
def test_same_time_last_year_needs_a_wave_that_counts_for_recurring(ctx, posts, expected):
    extra = [("i_amapiano", "ZA", date(2025, 8, 14), date(2025, 8, 16), date(2025, 8, 15), posts)]
    wh = DuckWarehouse(fixture_con(waves=WAVES + extra))
    waves = history(ctx, wh, item_id="i_amapiano", market="ZA")["waves"]
    assert [(w["peak_date"], w["same_time_last_year"]) for w in waves][-1] == (date(2026, 8, 18), expected)


def test_a_cut_off_wave_list_says_so_instead_of_reading_as_complete(ctx):
    many = [("i_many", "ZA", date(2020, 1, 1) + timedelta(days=40 * n), date(2020, 1, 2) + timedelta(days=40 * n),
             date(2020, 1, 1) + timedelta(days=40 * n), 9) for n in range(501)]
    wh = DuckWarehouse(fixture_con(waves=many))
    out = history(ctx, wh, item_id="i_many", market="ZA")
    assert len(out["waves"]) == 500
    assert out["truncated"] is True
    assert "500" in out["truncated_note"] and "no past waves" in out["truncated_note"]
    whole = history(ctx, DuckWarehouse(fixture_con()), item_id="i_heritage", market="ZA")
    assert whole["truncated"] is False and "truncated_note" not in whole


def test_each_moment_says_whether_it_matched_on_the_moment_or_last_years_analogue(ctx, wh):
    moments = history(ctx, wh, item_id="i_heritage", market="ZA")["moments"]
    assert [(m["peak_date"], m["matched_on"]) for m in moments] == [
        (date(2025, 9, 23), "analogue_date"), (date(2026, 9, 24), "moment_date")]


def test_a_zero_norm_centroid_never_ranks(ctx):
    cmap = MAP + [("i_zero", "topic", "zero", "Zero vector", [], [0.0, 0.0, 0.0], datetime(2026, 1, 1), None)]
    out = analogues(ctx, DuckWarehouse(fixture_con(cmap=cmap)), item_id="i_heritage", k=5)
    assert [a["item_id"] for a in out["analogues"]] == ["i_braai", "i_once", "i_amapiano"]
    assert all(a["distance"] is not None for a in out["analogues"])


def test_the_fallback_note_says_why_there_was_no_centroid_match(ctx):
    alone = [row for row in MAP if row[0] in ("i_heritage", "i_nocentroid")]
    no_rows = analogues(ctx, DuckWarehouse(fixture_con(cmap=alone)), item_id="i_heritage")
    assert no_rows["method"] == "keyword" and no_rows["centroid_status"] == "no centroid rows"
    assert "no centroid rows" in no_rows["note"].lower()

    none_stored = analogues(ctx, DuckWarehouse(fixture_con()), item_id="i_nocentroid")
    assert none_stored["centroid_status"] == "no stored centroid"
    assert "no stored centroid" in none_stored["note"].lower()

    wh = DuckWarehouse(fixture_con())
    real_run = wh.run
    wh.run = lambda sql, params, cap: (_ for _ in ()).throw(RuntimeError("boom")) if "SAFE_DIVIDE" in sql \
        else real_run(sql, params, cap)
    failed = analogues(ctx, wh, item_id="i_heritage")
    assert failed["centroid_status"] == "centroid query failed"
    assert "centroid query failed" in failed["note"].lower() and failed["centroid_reason"] == "boom"


def test_creator_items_are_left_out_of_analogues_and_the_note_says_so(ctx):
    cmap = MAP + [("i_dj", "creator", "tiktok:djbraai", "DJ Braai", [], [1.0, 0.0, 0.0], datetime(2026, 1, 1), None)]
    wh = DuckWarehouse(fixture_con(cmap=cmap))
    by_centroid = analogues(ctx, wh, item_id="i_heritage", k=5)
    by_words = analogues(ctx, wh, item_id="i_nocentroid", k=5)
    for out in (by_centroid, by_words):
        assert "i_dj" not in [a["item_id"] for a in out["analogues"]]
        assert "creator" in out["creators_note"].lower()
    assert by_centroid["analogues"][0]["item_id"] == "i_braai"


# round 2 notes


def test_history_marks_its_own_query_records_so_save_finding_can_trust_their_item_ids(ctx, wh):
    out = history(ctx, wh, item_id="i_heritage", market="ZA")
    assert {ctx.queries[q].get("tool") for q in out["query_ids"]} == {"history"}
    found = analogues(ctx, wh, text="amapiano", market="ZA")
    assert {ctx.queries[q].get("tool") for q in found["query_ids"]} == {"analogues"}


def test_same_time_last_year_needs_the_current_wave_to_count_too(ctx):
    extra = [("i_amapiano", "ZA", date(2025, 8, 10), date(2025, 8, 25), date(2025, 8, 18), 30),
             ("i_amapiano", "ZA", date(2026, 8, 10), date(2026, 8, 25), date(2026, 8, 19), 3)]
    waves = [w for w in WAVES if w[0] != "i_amapiano"] + extra
    rows = history(ctx, DuckWarehouse(fixture_con(waves=waves)), item_id="i_amapiano", market="ZA")["waves"]
    assert [(w["peak_posts"], w["same_time_last_year"]) for w in rows] == [(30, False), (3, False)]


def test_analogues_from_text_return_the_anchors_label_and_kind_and_prefer_an_exact_label(ctx):
    cmap = MAP + [("i_abraai", "topic", "abraai", "A braai season", [], [0.0, 0.0, 1.0], datetime(2026, 1, 1), None)]
    out = analogues(ctx, DuckWarehouse(fixture_con(cmap=cmap)), text="braai season")
    assert (out["item_id"], out["label"], out["kind"]) == ("i_braai", "Braai season", "topic")


def test_find_items_leaves_out_creator_items(ctx):
    cmap = MAP + [("i_dj", "creator", "tiktok:djbraai", "DJ Braai", [], [1.0, 0.0, 0.0], datetime(2026, 1, 1), None)]
    wh = DuckWarehouse(fixture_con(cmap=cmap))
    assert "i_dj" not in [i["item_id"] for i in history(ctx, wh, query="braai", market="ZA")["items"]]
    assert analogues(ctx, wh, text="DJ Braai")["item_id"] != "i_dj"
