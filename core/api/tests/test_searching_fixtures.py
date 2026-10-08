"""Searching now without foreign fixtures (lead inventory, 4 October 2026): Albert's ZA Today strip was mostly
"croatia vs england"-style national team fixtures, the same fixture under several names and raw fetch times next
to plain dates. Display only: the table, the store read and Ask are unchanged."""
import pytest

from core.api import searching
from core.api.store import FixtureStore

NONE_HIDDEN = (set(), set(), set())


class Rows(FixtureStore):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    def search_signals(self, start, end, markets):
        return self._rows


def row(term, rank, market="ZA", source="google_trending", refreshed_at="2026-10-03", fetch_day="2026-10-04"):
    return {"term": term, "market": market, "source": source, "rank": rank, "refreshed_at": refreshed_at,
            "fetch_day": fetch_day}


def strip(rows, markets=("ZA", "NG", "KE")):
    return searching.searching_now(Rows(rows), list(markets), "2026-10-04", NONE_HIDDEN)


@pytest.mark.parametrize("term", ["croatia vs england", "wales vs norway", "spain vs czechia", "Spain v Czechia",
                                  "england vs. wales", "nigeria vs ghana", "Croatia National Football Team vs "
                                  "England National Football Team"])
def test_a_fixture_between_two_other_countries_is_left_out(term):
    assert [r["term"] for r in strip([row(term, 1), row("kept", 2)], ["ZA"])] == ["kept"]


@pytest.mark.parametrize("term", ["eritrea vs south africa", "South Africa vs Eritrea", "bafana bafana vs eritrea",
                                  "SA vs eritrea", "kaizer chiefs vs orlando pirates", "sundowns vs pirates"])
def test_a_fixture_naming_the_market_or_between_clubs_stays(term):
    assert [r["term"] for r in strip([row(term, 1)], ["ZA"])] == [term]


def test_each_market_keeps_only_its_own_country():
    rows = [row("nigeria vs ghana", 1, market="NG"), row("nigeria vs ghana", 1, market="ZA"),
            row("kenya vs uganda", 1, market="KE"), row("super eagles vs ghana", 2, market="NG")]
    assert [(r["market"], r["term"]) for r in strip(rows)] == [
        ("NG", "nigeria vs ghana"), ("NG", "super eagles vs ghana"), ("KE", "kenya vs uganda")]


def test_one_fixture_under_several_names_is_shown_once_at_its_best_rank():
    rows = [row("eritrea national football team vs south africa national soccer team standings", 2),
            row("eritrea vs south africa", 5), row("south africa vs eritrea", 7),
            row("eritrea vs south africa", 3, source="google_trending", refreshed_at="2026-10-04T05:00:00Z"),
            row("amapiano", 4)]
    assert [(r["term"], r["rank"]) for r in strip(rows, ["ZA"])] == [
        ("eritrea national football team vs south africa national soccer team standings", 2), ("amapiano", 4)]


def test_live_rows_keep_the_fetch_timestamp_and_daily_rows_keep_their_day():
    rows = [row("bq term", 1), row("trending late", 2, source="google_trending", refreshed_at="2026-10-03T22:45:59Z"),
            row("trending early", 3, source="google_trending", refreshed_at="2026-10-04T00:45:59Z")]
    assert [(r["term"], r["refreshed_at"]) for r in strip(rows, ["ZA"])] == [
        ("bq term", "2026-10-03"), ("trending late", "2026-10-03T22:45:59Z"),
        ("trending early", "2026-10-04T00:45:59Z")]


def test_left_out_fixtures_are_backfilled_to_ten_rows():
    rows = ([row(f"{a} vs {b}", n) for n, (a, b) in enumerate(
        [("croatia", "england"), ("wales", "norway"), ("spain", "czechia")], 1)]
        + [row(f"local term {n:02d}", n) for n in range(4, 20)])
    out = strip(rows, ["ZA"])
    assert len(out) == searching.PER_MARKET == 10
    assert [r["rank"] for r in out] == list(range(4, 14))


def test_google_trends_daily_rss_rows_are_triage_only_and_not_shown():
    rows = [row("rss term", 1, source="google_rss", refreshed_at="2026-10-04T04:10:00Z"), row("live term", 2)]
    assert [(r["term"], r["source"], r["refreshed_at"]) for r in strip(rows, ["ZA"])] == [
        ("live term", "google_trending", "2026-10-03")]
    assert all(set(r) == {"term", "market", "source", "rank", "refreshed_at"} for r in strip(rows, ["ZA"]))
