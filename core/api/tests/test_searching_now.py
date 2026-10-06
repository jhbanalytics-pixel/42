"""Searching now on Today and Discover (contract.md section 21): Google search interest rows the collect job wrote to
google_search_signals, scoped to the market and dated, in the five-field shape the app's SearchingNow strip reads.
Never posts or cards, and a missing table or a failed read gives an empty list, never a failed page."""
import datetime as dt
import logging

import pytest

from core.api import discover, searching, skins, today
from core.api.store import BigQueryStore, FixtureStore

SAST = dt.timezone(dt.timedelta(hours=2))
NOW = dt.datetime(2026, 9, 30, 16, 0, tzinfo=SAST)
FIELDS = {"term", "market", "source", "rank", "refreshed_at"}

ZA_ROWS = [
    {"term": "fixture za search one", "market": "ZA", "source": "google_bq", "rank": 1, "refreshed_at": "2026-09-28"},
    {"term": "fixture za search two", "market": "ZA", "source": "google_bq", "rank": 2, "refreshed_at": "2026-09-28"},
    {"term": "fixture za search rising", "market": "ZA", "source": "google_bq", "rank": None,
     "refreshed_at": "2026-09-28"},
]
NG_ROWS = [
    {"term": "fixture ng search", "market": "NG", "source": "google_trending", "rank": 3,
     "refreshed_at": "2026-09-30"},  # the fetch time 2026-09-30T05:12:30Z, sent as its SAST day
]


class Patched(FixtureStore):
    def __init__(self, **overrides):
        super().__init__()
        for name, fn in overrides.items():
            setattr(self, name, fn)


def row(**over):
    base = {"market": "ZA", "term": "fixture term", "source": "google_bq", "rank": 1, "refreshed_at": "2026-09-28",
            "fetch_day": "2026-09-30"}
    return {**base, **over}


# The store read.

def test_fixture_store_reads_rows_fetched_in_the_window_with_their_sast_fetch_day():
    rows = FixtureStore().search_signals("2026-09-28", "2026-09-30", ["ZA", "NG", "KE"])
    assert {(r["market"], r["term"], r["fetch_day"]) for r in rows} >= {
        ("ZA", "fixture za search old", "2026-09-29"), ("ZA", "fixture za search one", "2026-09-30"),
        ("NG", "fixture ng search", "2026-09-30")}
    assert all(r["fetch_day"] <= "2026-09-30" for r in rows)  # the 1 October NG row is after the window
    assert FixtureStore().search_signals("2026-09-28", "2026-09-30", ["KE"]) == []


def test_fixture_store_without_the_file_reads_none(tmp_path):
    assert FixtureStore(root=tmp_path).search_signals("2026-09-28", "2026-09-30", ["ZA"]) is None


class FakeJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return [FakeRow(r) for r in self.rows]


class FakeRow(dict):
    def items(self):
        return super().items()


class FakeClient:
    def __init__(self, rows=None):
        self.calls, self.rows = [], rows or []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.rows)


def test_bigquery_store_reads_the_table_by_window_and_market_with_the_cap(monkeypatch):
    client = FakeClient([{"market": "ZA", "term": "t", "source": "google_bq", "rank": 1, "refreshed_at": "2026-09-28",
                          "fetch_day": dt.date(2026, 9, 30)}])
    bq = BigQueryStore(client=client)
    monkeypatch.setattr(bq, "_find", lambda name: f"`p.intelligence_42_core.{name}`")
    rows = bq.search_signals("2026-09-28", "2026-09-30", ["ZA", "NG"])
    sql, cfg = client.calls[0]
    assert "google_search_signals" in sql and "@start" in sql and "@end" in sql and "@markets" in sql
    assert "2026-09" not in sql  # dates go in as parameters
    assert "fetched_at >=" in sql and "fetched_at <" in sql  # the partition column bounds the read
    assert cfg.maximum_bytes_billed == 2_000_000_000
    params = {p.name: p for p in cfg.query_parameters}
    assert params["markets"].values == ["ZA", "NG"]
    assert rows[0]["fetch_day"] == "2026-09-30"
    monkeypatch.setattr(bq, "_find", lambda name: None)
    assert bq.search_signals("2026-09-28", "2026-09-30", ["ZA"]) is None


# The selection.

def test_rows_are_the_five_fields_newest_fetch_day_deduplicated_and_ranked():
    out = searching.searching_now(FixtureStore(), ["ZA", "NG", "KE"], "2026-09-30", NONE_HIDDEN)
    assert out == ZA_ROWS + NG_ROWS
    assert all(set(r) == FIELDS for r in out)


def test_the_window_ends_on_the_asked_day_and_reaches_back_three_days():
    asked = []

    def read(start, end, markets):
        asked.append((start, end, list(markets)))
        return []

    assert searching.searching_now(Patched(search_signals=read), ["ZA"], "2026-09-30", NONE_HIDDEN) == []
    assert asked == [("2026-09-28", "2026-09-30", ["ZA"])]


def test_a_day_with_older_rows_only_shows_those_rows():
    out = searching.searching_now(FixtureStore(), ["ZA"], "2026-09-29", NONE_HIDDEN)
    assert out == [{"term": "fixture za search old", "market": "ZA", "source": "google_bq", "rank": 1,
                    "refreshed_at": "2026-09-27"}]


def test_each_market_and_source_keeps_its_own_newest_day():
    rows = [row(term="za new", fetch_day="2026-09-30"), row(term="za old", fetch_day="2026-09-29"),
            row(market="NG", term="ng old", fetch_day="2026-09-29"),
            row(source="google_trending", term="za trending", refreshed_at="2026-09-29T08:00:00Z",
                fetch_day="2026-09-29")]
    out = searching.searching_now(Patched(search_signals=lambda s, e, m: rows), ["ZA", "NG"], "2026-09-30", NONE_HIDDEN)
    assert [(r["market"], r["term"]) for r in out] == [("ZA", "za new"), ("ZA", "za trending"), ("NG", "ng old")]


def test_rows_outside_the_asked_markets_or_window_are_left_out():
    rows = [row(market="KE"), row(market="GH"), row(fetch_day="2026-10-01"), row(fetch_day="2026-09-27"),
            row(term="kept")]
    out = searching.searching_now(Patched(search_signals=lambda s, e, m: rows), ["ZA"], "2026-09-30", NONE_HIDDEN)
    assert [r["term"] for r in out] == ["kept"]


@pytest.mark.parametrize("bad", [
    {"term": ""}, {"term": "   "}, {"term": None}, {"source": "tiktok"}, {"rank": 0}, {"rank": -2},
    {"rank": True}, {"rank": 1.5}, {"refreshed_at": "28 Sept"}, {"refreshed_at": "2026-02-30"},
    {"refreshed_at": "2026-09-28T10:00:00+02:00"}, {"refreshed_at": None},
])
def test_rows_the_strip_would_reject_are_dropped_here(bad):
    rows = [row(**bad), row(term="kept", rank=2)]
    out = searching.searching_now(Patched(search_signals=lambda s, e, m: rows), ["ZA"], "2026-09-30", NONE_HIDDEN)
    assert [r["term"] for r in out] == ["kept"]


def test_terms_are_trimmed_and_a_repeated_term_keeps_its_best_rank():
    rows = [row(term=" Bafana ", rank=5), row(term="bafana", rank=2), row(term="BAFANA", rank=None)]
    out = searching.searching_now(Patched(search_signals=lambda s, e, m: rows), ["ZA"], "2026-09-30", NONE_HIDDEN)
    assert out == [{"term": "bafana", "market": "ZA", "source": "google_bq", "rank": 2, "refreshed_at": "2026-09-28"}]


def test_a_term_both_sources_report_is_shown_once_at_its_best_rank():
    rows = [row(term="match day", rank=4),
            row(term="Match Day", source="google_trending", rank=2, refreshed_at="2026-09-30T06:00:00Z")]
    out = searching.searching_now(Patched(search_signals=lambda s, e, m: rows), ["ZA"], "2026-09-30", NONE_HIDDEN)
    assert out == [{"term": "Match Day", "market": "ZA", "source": "google_trending", "rank": 2,
                    "refreshed_at": "2026-09-30"}]


def test_each_market_shows_at_most_the_cap_ranked_first():
    rows = [row(term=f"t{n:02d}", rank=n) for n in range(1, 16)] + [row(term="unranked", rank=None)]
    out = searching.searching_now(Patched(search_signals=lambda s, e, m: rows), ["ZA"], "2026-09-30", NONE_HIDDEN)
    assert len(out) == searching.PER_MARKET == 10
    assert [r["rank"] for r in out] == list(range(1, 11))


def test_a_missing_table_or_failed_read_gives_an_empty_list_and_a_warning(caplog):
    assert searching.searching_now(Patched(search_signals=lambda s, e, m: None), ["ZA"], "2026-09-30", NONE_HIDDEN) == []

    def boom(start, end, markets):
        raise RuntimeError("404 Not found: Table google_search_signals")

    with caplog.at_level(logging.WARNING, logger="f42.api.searching"):
        assert searching.searching_now(Patched(search_signals=boom), ["ZA"], "2026-09-30", NONE_HIDDEN) == []
    assert "RuntimeError" in caplog.text and "Not found" not in caplog.text


# Today and Discover.

def test_today_carries_every_market_as_of_today():
    t = today.build_today(FixtureStore(), now=NOW)
    assert t["searching_now"] == ZA_ROWS + NG_ROWS


def test_a_past_today_is_dated_to_that_day():
    t = today.build_today(FixtureStore(), date="2026-09-30", now=NOW + dt.timedelta(days=1))
    assert t["searching_now"] == ZA_ROWS + NG_ROWS
    asked = []
    store = Patched(search_signals=lambda s, e, m: asked.append((s, e)) or [])
    today.build_today(store, date="2026-09-30", now=NOW + dt.timedelta(days=5))
    assert asked == [("2026-09-28", "2026-09-30")]


def test_the_latest_today_reads_up_to_today_even_before_its_brief_is_published():
    asked = []
    store = Patched(search_signals=lambda s, e, m: asked.append((s, e)) or [])
    today.build_today(store, now=NOW + dt.timedelta(days=1))  # the latest brief is still 30 September
    assert asked == [("2026-09-29", "2026-10-01")]


def test_today_is_unchanged_apart_from_the_field_when_the_read_fails():
    def boom(start, end, markets):
        raise RuntimeError("table gone")

    good = today.build_today(FixtureStore(), now=NOW)
    bad = today.build_today(Patched(search_signals=boom), now=NOW)
    assert bad["searching_now"] == []
    assert {k: v for k, v in bad.items() if k != "searching_now"} == {
        k: v for k, v in good.items() if k != "searching_now"}


def test_discover_carries_only_its_market():
    za = discover.build_discover(FixtureStore(), "ZA", now=NOW)
    assert za["searching_now"] == ZA_ROWS
    assert discover.build_discover(FixtureStore(), "KE", now=NOW)["searching_now"] == []
    assert discover.build_discover(FixtureStore(), "all", now=NOW)["searching_now"] == ZA_ROWS + NG_ROWS


def test_discover_is_dated_to_today_and_survives_a_failed_read():
    asked = []
    store = Patched(search_signals=lambda s, e, m: asked.append((s, e, list(m))) or [])
    discover.build_discover(store, "NG", now=NOW)
    assert asked == [("2026-09-28", "2026-09-30", ["NG"])]

    def boom(start, end, markets):
        raise RuntimeError("table gone")

    out = discover.build_discover(Patched(search_signals=boom), "ZA", now=NOW)
    assert out["searching_now"] == []
    assert out["items"] == discover.build_discover(FixtureStore(), "ZA", now=NOW)["items"]


def test_later_discover_pages_carry_the_same_rows():
    first = discover.build_discover(FixtureStore(), "all", limit=1, now=NOW)
    assert first["next_cursor"]
    later = discover.build_discover(FixtureStore(), "all", limit=1, cursor=first["next_cursor"], now=NOW)
    assert later["searching_now"] == first["searching_now"]


def test_a_skin_today_keeps_only_its_markets_rows():
    t = today.build_today(FixtureStore(), now=NOW)
    skin = {"skin_id": "s1", "name": "Fixture skin", "markets": ["NG"], "terms": ["nothing matches"], "hashtags": []}
    assert skins.narrow_today(t, skin)["searching_now"] == NG_ROWS


# The people suppression list (contract.md sections 16 and 19).

NONE_HIDDEN = (set(), set(), set())
HIDDEN = ({"tiktok:fixture_hidden_handle", "youtube:uc0123456789abcdefghijkl", "x:fixture.dotted"},
          {"cr_hidden_01"}, {"Fixture Hidden Person", "F. Initial", "Fixture Accénted"})


@pytest.mark.parametrize("term", [
    "fixture hidden person", "Fixture Hidden Person new song", "FixtureHiddenPerson", "@fixture_hidden_handle",
    "fixture_hidden_handle live", "Fixture Hidden Handle", "cr_hidden_01", "UC0123456789abcdefghijkl",
    "Fixture Hidden Handle live", "news fixture dotted arrested", "fixture-hidden-person news",
    "fixture_hidden_person video", "fixturehiddenperson news", "f initial tour", "fixture accented",
    "new fixture.hidden.handle clip",
])
def test_a_term_naming_a_suppressed_person_is_dropped(term):
    rows = [row(term=term), row(term="kept", rank=2)]
    store = Patched(search_signals=lambda s, e, m: rows)
    assert [r["term"] for r in searching.searching_now(store, ["ZA"], "2026-09-30", HIDDEN)] == ["kept"]
    assert len(searching.searching_now(store, ["ZA"], "2026-09-30", NONE_HIDDEN)) == 2


def test_a_name_inside_a_longer_word_is_not_a_match():
    rows = [row(term="unfixture hidden personal")]
    out = searching.searching_now(Patched(search_signals=lambda s, e, m: rows), ["ZA"], "2026-09-30", HIDDEN)
    assert [r["term"] for r in out] == ["unfixture hidden personal"]


def test_an_unreadable_suppression_list_sends_no_terms():
    assert searching.searching_now(FixtureStore(), ["ZA", "NG"], "2026-09-30", None) == []


def test_today_drops_terms_naming_a_suppressed_person_and_fails_closed():
    names = Patched(suppressed_creators=lambda: {"cr_hidden_01"},
                    creators_by_id=lambda ids: [{"creator_id": "cr_hidden_01", "platform": "tiktok",
                                                 "handle": "fixture_za_search_two"}])
    t = today.build_today(names, now=NOW)
    assert [r["term"] for r in t["searching_now"]] == [
        "fixture za search one", "fixture za search rising", "fixture ng search"]

    def broken():
        raise RuntimeError("view gone")

    assert today.build_today(Patched(suppressed_creators=broken), now=NOW)["searching_now"] == []
    assert today.build_today(Patched(suppressed_creators=lambda: None), now=NOW)["searching_now"] == []


def test_discover_drops_terms_naming_a_suppressed_person_and_fails_closed():
    names = Patched(suppressed_creators=lambda: {"cr_hidden_01"},
                    creators_by_id=lambda ids: [{"creator_id": "cr_hidden_01", "platform": "x",
                                                 "handle": "fixture_ng_search"}])
    assert discover.build_discover(names, "all", now=NOW)["searching_now"] == ZA_ROWS

    def broken():
        raise RuntimeError("view gone")

    assert discover.build_discover(Patched(suppressed_creators=broken), "all", now=NOW)["searching_now"] == []
