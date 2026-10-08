"""W8-DEC-11: a paid route whose calls succeed on two consecutive days and land zero posts and zero counters is
recorded invalid for the second and later days with reason zero_yield (core/collect/writers.py). No network."""

from datetime import date, timedelta

import pytest

from core.collect import writers
from core.detect.tests import duck
from core.detect.tests.fixtures import run

DAY = "2026-10-05"
PRIOR_KEY = ("ZA", "panel_culture_desk", "panel:abc")


def rec(day=DAY, *, route="prism/profiles", lane="panel", series="panel_culture_desk", protocol="panel:abc",
        market="ZA", calls=1, ok=True, items=0, post_ids=(), failure=""):
    return {"row": "23", "route": route, "market": market, "day": day, "platform": None, "series": series,
            "protocol": protocol, "lane_class": lane, "status": "ok" if ok else "http_503", "ok": ok,
            "calls": calls, "units_planned": calls, "units_ok": calls if ok else 0, "items": items,
            "post_ids": list(post_ids), "reason": "", "failure": failure}


def health(records, prior=None, refs=None):
    rows = writers.health_rows(records, [], refs or {}, "collect-test", prior)
    return {(r["day"], r["market"], r["series"], r["protocol"]): r for r in rows}


def prior_zero(*keys):
    return {DAY: set(keys)}


def test_one_zero_day_stays_valid():
    [row] = health([rec()]).values()
    assert (row["valid"], row["invalid_reason"]) == (True, None)


def test_second_consecutive_zero_day_is_invalid_with_reason_zero_yield():
    [row] = health([rec()], prior_zero(PRIOR_KEY)).values()
    assert (row["valid"], row["invalid_reason"]) == (False, "zero_yield")
    assert (row["calls"], row["calls_ok"], row["items"]) == (1, 1, 0)


def test_two_zero_days_in_one_run_mark_the_second_only():
    rows = health([rec("2026-10-04"), rec("2026-10-05")])
    assert rows[("2026-10-04", "ZA", "panel_culture_desk", "panel:abc")]["valid"] is True
    second = rows[("2026-10-05", "ZA", "panel_culture_desk", "panel:abc")]
    assert (second["valid"], second["invalid_reason"]) == (False, "zero_yield")


def test_a_prior_zero_of_another_market_series_or_protocol_does_not_count():
    for other in (("NG", "panel_culture_desk", "panel:abc"), ("ZA", "panel_ig_gossip", "panel:abc"),
                  ("ZA", "panel_culture_desk", "panel:other")):
        [row] = health([rec()], prior_zero(other)).values()
        assert row["valid"] is True, other


def test_a_prior_zero_two_days_back_does_not_count():
    [row] = health([rec()], {"2026-10-03": {PRIOR_KEY}}).values()
    assert row["valid"] is True


def test_a_day_with_one_counter_stays_valid():
    [row] = health([rec(route="tiktok/song", lane="watchlist", series="counter_tiktok_sound", protocol="p",
                        market="GLOBAL", items=1)],
                   prior_zero(("GLOBAL", "counter_tiktok_sound", "p"))).values()
    assert (row["valid"], row["invalid_reason"]) == (True, None)


def test_a_day_with_one_post_stays_valid():
    [row] = health([rec(items=1, post_ids=["p1"])], prior_zero(PRIOR_KEY)).values()
    assert row["valid"] is True


def test_posts_parsed_with_no_observation_are_not_a_zero_day():
    [row] = health([rec(items=0, post_ids=["p1"])], prior_zero(PRIOR_KEY)).values()
    assert row["valid"] is True


@pytest.mark.parametrize("route", ["public_feed", "rss", "apple_rss", "telegram_preview"])
def test_a_free_route_is_never_marked(route):
    [row] = health([rec(route=route, lane="unbiased_rank")], prior_zero(PRIOR_KEY)).values()
    assert (row["valid"], row["invalid_reason"]) == (True, None)


def test_a_search_lane_is_never_marked():
    [row] = health([rec(route="search/multi", lane="search_presence", series="search", protocol="p")],
                   prior_zero(("ZA", "search", "p"))).values()
    assert row["valid"] is True


def test_a_5xx_day_is_not_a_zero_yield_day():
    [row] = health([rec(ok=False, failure="http_5xx")], prior_zero(PRIOR_KEY)).values()
    assert (row["valid"], row["invalid_reason"]) == (False, "calls: http_5xx")


def test_a_day_with_one_failed_call_among_several_is_not_a_zero_yield_day():
    rows = health([rec(), rec(ok=False, failure="http_5xx")], prior_zero(PRIOR_KEY))
    [row] = rows.values()
    assert (row["calls"], row["calls_ok"], row["valid"]) == (2, 1, False)
    assert row["invalid_reason"] != "zero_yield"


def test_a_day_with_one_failed_call_in_five_is_not_a_zero_yield_day():
    records = [rec() for _ in range(4)] + [rec(ok=False, failure="http_5xx")]
    [row] = health(records, prior_zero(PRIOR_KEY)).values()
    assert (row["calls"], row["calls_ok"], row["valid"]) == (5, 4, True)


def test_a_day_with_no_calls_made_is_not_a_zero_yield_day_and_starts_no_run_of_them():
    [row] = health([rec(calls=0, ok=False)], prior_zero(PRIOR_KEY)).values()
    assert row["invalid_reason"] != "zero_yield"
    rows = health([rec("2026-10-04", calls=0, ok=False), rec("2026-10-05")])
    assert rows[("2026-10-05", "ZA", "panel_culture_desk", "panel:abc")]["valid"] is True


def test_a_day_already_invalid_for_a_drop_keeps_its_reason():
    refs = {DAY: {PRIOR_KEY: (50.0, 5)}}
    [row] = health([rec()], prior_zero(PRIOR_KEY), refs).values()
    assert (row["valid"], row["invalid_reason"]) == (False, "items")


def test_the_default_is_no_prior_day_so_existing_callers_are_unchanged():
    rows = writers.health_rows([rec()], [], {}, "c")
    assert [(r["valid"], r["invalid_reason"]) for r in rows] == [(True, None)]


# The query behind the prior-day set, run on DuckDB with the writer's own rows as its tables.

D5 = date(2026, 10, 5)


def prior_query(runs, rows, d=D5):
    con = duck.connect()
    duck.load(con, "agent.runs", [run("collect", day, run_id=run_id, hour=h, status=st)
                                  for run_id, (day, h, st) in runs.items()])
    duck.load(con, "core.collection_health", rows)
    sql = writers.ZERO_YIELD_PRIOR_SQL.format(runs="agent.runs", health="core.collection_health")
    return duck.query(con, sql.replace("`", ""), {"d": d})


def written(records, run_id, prior=None):
    return [dict(r, day=date.fromisoformat(r["day"]))
            for r in writers.health_rows(records, [], {}, run_id, prior)]


def test_the_prior_day_query_returns_only_ok_run_zero_rows_of_the_day_before():
    d4, d3 = D5 - timedelta(days=1), D5 - timedelta(days=2)
    rows = (
        written([rec("2026-10-04")], "c4")
        + written([rec("2026-10-04", series="panel_ig_gossip", items=3, post_ids=["x"])], "c4")
        + written([rec("2026-10-04", series="b", protocol="b", ok=False, failure="http_5xx")], "c4")
        + written([rec("2026-10-03", series="old", protocol="old")], "c3")
        + written([rec("2026-10-04", series="redone", protocol="r")], "c4a")
        + written([rec("2026-10-04", series="redone", protocol="r", items=2, post_ids=["y"])], "c4b")
        + written([rec("2026-10-04", series="failedrun", protocol="f")], "c4x")
        + written([rec("2026-10-04", series="nocalls", protocol="n", calls=0, ok=False)], "c4")
        + written([rec("2026-10-04", series="fiveth", protocol="v")] * 4
                  + [rec("2026-10-04", series="fiveth", protocol="v", ok=False, failure="http_5xx")], "c4"))
    runs = {"c4": (d4, 8, "ok"), "c3": (d3, 8, "ok"), "c4a": (d4, 9, "ok"), "c4b": (d4, 10, "ok"),
            "c4x": (d4, 11, "failed")}
    keys = {(r["market"], r["series"], r["protocol"]) for r in prior_query(runs, rows)}
    assert keys == {PRIOR_KEY}


def test_a_zero_route_written_day_after_day_is_invalid_from_the_second_day():
    runs, rows = {}, []
    for n in range(1, 5):
        d = date(2026, 10, n)
        prior = {d.isoformat(): {(r["market"], r["series"], r["protocol"])
                                 for r in prior_query(runs, rows, d)
                                 if writers.zero_yield_route(r["route"], r["lane_class"])}}
        runs[f"c{n}"] = (d, 8, "ok")
        rows += written([rec(d.isoformat())], f"c{n}", prior)
    assert [(r["day"].day, r["valid"], r["invalid_reason"]) for r in rows] == [
        (1, True, None), (2, False, "zero_yield"), (3, False, "zero_yield"), (4, False, "zero_yield")]


def test_the_prior_day_set_leaves_out_free_and_search_routes():
    assert writers.zero_yield_route("prism/profiles", "panel")
    assert writers.zero_yield_route("tiktok/song", "watchlist")
    assert not writers.zero_yield_route("public_feed", "unbiased_rank")
    assert not writers.zero_yield_route("rss", "unbiased_rank")
    assert not writers.zero_yield_route("search/multi", "search_presence")


def test_zero_yield_prior_reads_the_day_before_and_filters_by_route(monkeypatch):
    seen = []

    def fake_query(bq, sql, params):
        seen.append((sql, [(p.name, p.value) for p in params]))
        return [{"market": "ZA", "series": "a", "protocol": "p", "route": "prism/profiles", "lane_class": "panel"},
                {"market": "ZA", "series": "f", "protocol": "p", "route": "public_feed", "lane_class": "panel"}]

    monkeypatch.setattr(writers, "_query", fake_query)
    assert writers.zero_yield_prior(object(), D5) == {("ZA", "a", "p")}
    assert seen[0][1] == [("d", D5)]


# The write path: write_run asks for the day before and appends the marked row.

def test_write_run_appends_the_zero_yield_row_when_the_day_before_was_a_zero_day():
    from core.collect.tests.test_job import FakeBQ, FakeClient, collect

    run = collect(FakeClient())
    feeds = [r for r in run.records if r["market"] == "ZA" and r["series"] == "feed_tiktok"]
    for feed in feeds:
        feed.update(items=0, post_ids=[])
    feed = feeds[0]
    key = {"market": "ZA", "series": "feed_tiktok", "protocol": feed["protocol"], "route": feed["route"],
           "lane_class": feed["lane_class"]}
    marked = FakeBQ(prior_zero=[key])
    writers.write_run(marked, run, "collect-1")
    [row] = [r for r in marked.loaded("collection_health") if r["market"] == "ZA" and r["series"] == "feed_tiktok"]
    assert (row["valid"], row["invalid_reason"]) == (False, "zero_yield")
    plain = FakeBQ()
    writers.write_run(plain, run, "collect-2")
    [row] = [r for r in plain.loaded("collection_health") if r["market"] == "ZA" and r["series"] == "feed_tiktok"]
    assert (row["valid"], row["invalid_reason"]) == (True, None)
