"""W8-DEC-11: a paid route whose calls succeed on two consecutive days and land zero posts and zero counters is
recorded invalid for the second and later days with reason zero_yield (core/collect/writers.py). No network."""

from datetime import date, timedelta

import pytest

from core.collect import writers
from core.detect.tests import duck
from core.detect.tests.fixtures import run

DAY = "2026-10-05"
PRIOR_KEY = ("ZA", "panel_culture_desk", "prism/profiles", "panel")


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


def test_a_prior_zero_of_another_market_series_route_or_lane_does_not_count():
    for other in (("NG", "panel_culture_desk", "prism/profiles", "panel"),
                  ("ZA", "panel_ig_gossip", "prism/profiles", "panel"),
                  ("ZA", "panel_culture_desk", "tiktok/feed", "panel"),
                  ("ZA", "panel_culture_desk", "prism/profiles", "watchlist")):
        [row] = health([rec()], prior_zero(other)).values()
        assert row["valid"] is True, other


def test_a_prior_zero_of_the_same_series_counts_whatever_the_protocol():
    for protocol in ("panel:abc", "panel:other", "panel:abc:v2"):
        [row] = health([rec(protocol=protocol)], prior_zero(PRIOR_KEY)).values()
        assert (row["valid"], row["invalid_reason"]) == (False, "zero_yield"), protocol


def test_a_desk_zero_day_does_not_mark_the_gossip_panel_on_its_first_zero_day():
    """G-1: the culture desk, the curated panel and the Instagram gossip panel share route prism/profiles and lane
    panel, but each is its own series. A zero of one is not the day before of another."""
    [row] = health([rec(series="panel_ig_gossip", protocol="panel:goss")], prior_zero(PRIOR_KEY)).values()
    assert (row["valid"], row["invalid_reason"]) == (True, None)


def desk_and_gossip(*days_and_zero):
    """(day, desk lands, gossip lands) records, the way the collect job writes the two panels of one market."""
    records = []
    for d, desk_lands, gossip_lands in days_and_zero:
        records.append(rec(d, series="panel_culture_desk", protocol="panel:desk",
                           items=4 if desk_lands else 0, post_ids=[f"d{d}"] if desk_lands else []))
        records.append(rec(d, series="panel_ig_gossip", protocol="panel:goss",
                           items=5 if gossip_lands else 0, post_ids=[f"g{d}"] if gossip_lands else []))
    return records


def test_probe_g1_alternating_desk_and_gossip_zero_days_mark_neither_panel_in_one_run():
    rows = health(desk_and_gossip(("2026-10-04", False, True), ("2026-10-05", True, False)))
    assert all(r["valid"] and r["invalid_reason"] is None for r in rows.values())


def test_probe_g1_alternating_desk_and_gossip_zero_days_mark_neither_panel_from_the_stored_prior():
    day2 = [r for r in desk_and_gossip(("2026-10-05", True, False))]
    rows = health(day2, prior_zero(PRIOR_KEY))
    assert all(r["valid"] and r["invalid_reason"] is None for r in rows.values())


def test_each_panel_is_marked_on_its_own_second_zero_day_beside_a_live_one():
    rows = health(desk_and_gossip(("2026-10-04", False, True), ("2026-10-05", False, True)))
    assert rows[("2026-10-05", "ZA", "panel_culture_desk", "panel:desk")]["invalid_reason"] == "zero_yield"
    assert rows[("2026-10-05", "ZA", "panel_ig_gossip", "panel:goss")]["invalid_reason"] is None
    rows = health(desk_and_gossip(("2026-10-04", True, False), ("2026-10-05", True, False)))
    assert rows[("2026-10-05", "ZA", "panel_ig_gossip", "panel:goss")]["invalid_reason"] == "zero_yield"
    assert rows[("2026-10-05", "ZA", "panel_culture_desk", "panel:desk")]["invalid_reason"] is None


def test_three_days_of_rotating_curated_protocols_mark_the_second_and_third_day():
    days = ["2026-10-03", "2026-10-04", "2026-10-05"]
    records = [rec(d, protocol=f"panel:rot{n}") for n, d in enumerate(days)]
    rows = health(records)
    got = [rows[(d, "ZA", "panel_culture_desk", f"panel:rot{n}")] for n, d in enumerate(days)]
    assert [(r["valid"], r["invalid_reason"]) for r in got] == [
        (True, None), (False, "zero_yield"), (False, "zero_yield")]


def test_a_live_desk_protocol_does_not_hide_a_dead_rotating_curated_protocol_on_the_same_route():
    days = ["2026-10-03", "2026-10-04", "2026-10-05"]
    records = []
    for n, d in enumerate(days):
        records.append(rec(d, protocol="panel:desk", items=4, post_ids=[f"d{n}"]))
        records.append(rec(d, protocol=f"panel:rot{n}"))
    rows = health(records)
    assert all(rows[(d, "ZA", "panel_culture_desk", "panel:desk")]["valid"] for d in days)
    assert [rows[(d, "ZA", "panel_culture_desk", f"panel:rot{n}")]["invalid_reason"]
            for n, d in enumerate(days)] == [None, "zero_yield", "zero_yield"]


def test_the_token_switch_day_is_still_marked_when_the_protocol_changes():
    records = [rec("2026-10-04", protocol="panel:abc"), rec("2026-10-05", protocol="panel:abc:v2")]
    rows = health(records)
    assert rows[("2026-10-05", "ZA", "panel_culture_desk", "panel:abc:v2")]["invalid_reason"] == "zero_yield"
    switch = health([rec("2026-10-05", protocol="panel:abc:v2")], prior_zero(PRIOR_KEY))
    assert switch[("2026-10-05", "ZA", "panel_culture_desk", "panel:abc:v2")]["invalid_reason"] == "zero_yield"


def test_a_prior_zero_two_days_back_does_not_count():
    [row] = health([rec()], {"2026-10-03": {PRIOR_KEY}}).values()
    assert row["valid"] is True


def test_a_day_with_one_counter_stays_valid():
    [row] = health([rec(route="tiktok/song", lane="watchlist", series="counter_tiktok_sound", protocol="p",
                        market="GLOBAL", items=1)],
                   prior_zero(("GLOBAL", "counter_tiktok_sound", "tiktok/song", "watchlist"))).values()
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
                   prior_zero(("ZA", "search", "search/multi", "search_presence"))).values()
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
    refs = {DAY: {("ZA", "panel_culture_desk", "panel:abc"): (50.0, 5)}}
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
    keys = {(r["market"], r["series"], r["route"], r["lane_class"]) for r in prior_query(runs, rows)}
    assert keys == {PRIOR_KEY}


def test_the_prior_day_query_leaves_out_a_route_whose_only_row_landed_items():
    """G-2: a live day before is no zero day, so a route with only a live row has no key."""
    d4 = D5 - timedelta(days=1)
    live = written([rec("2026-10-04", items=4, post_ids=["a"])], "c4")
    assert prior_query({"c4": (d4, 8, "ok")}, live) == []
    live_beside_zero = live + written([rec("2026-10-04", series="panel_ig_gossip", protocol="panel:goss")], "c4")
    got = prior_query({"c4": (d4, 8, "ok")}, live_beside_zero)
    assert [(r["series"], r["route"]) for r in got] == [("panel_ig_gossip", "prism/profiles")]


def test_the_prior_day_query_keeps_each_series_of_a_route_apart():
    d4 = D5 - timedelta(days=1)
    rows = written([rec("2026-10-04"), rec("2026-10-04", series="panel_ig_gossip", protocol="panel:goss")], "c4")
    got = prior_query({"c4": (d4, 8, "ok")}, rows)
    assert {r["series"] for r in got} == {"panel_culture_desk", "panel_ig_gossip"}


def test_a_zero_route_written_day_after_day_is_invalid_from_the_second_day():
    runs, rows = {}, []
    for n in range(1, 5):
        d = date(2026, 10, n)
        prior = {d.isoformat(): {(r["market"], r["series"], r["route"], r["lane_class"])
                                 for r in prior_query(runs, rows, d)
                                 if writers.zero_yield_route(r["route"], r["lane_class"])}}
        runs[f"c{n}"] = (d, 8, "ok")
        rows += written([rec(d.isoformat())], f"c{n}", prior)
    assert [(r["day"].day, r["valid"], r["invalid_reason"]) for r in rows] == [
        (1, True, None), (2, False, "zero_yield"), (3, False, "zero_yield"), (4, False, "zero_yield")]


def test_a_dead_curated_route_is_marked_through_the_query_over_three_rotating_protocols():
    runs, rows = {}, []
    for n in range(1, 4):
        d = date(2026, 10, n)
        prior = {d.isoformat(): {(r["market"], r["series"], r["route"], r["lane_class"])
                                 for r in prior_query(runs, rows, d)
                                 if writers.zero_yield_route(r["route"], r["lane_class"])}}
        runs[f"c{n}"] = (d, 8, "ok")
        rows += written([rec(d.isoformat(), protocol="panel:desk", items=4, post_ids=[f"d{n}"]),
                         rec(d.isoformat(), protocol=f"panel:rot{n}")], f"c{n}", prior)
    dead = [(r["day"].day, r["protocol"], r["valid"], r["invalid_reason"]) for r in rows
            if r["protocol"].startswith("panel:rot")]
    assert dead == [(1, "panel:rot1", True, None), (2, "panel:rot2", False, "zero_yield"),
                    (3, "panel:rot3", False, "zero_yield")]
    assert all(r["valid"] for r in rows if r["protocol"] == "panel:desk")


def test_the_prior_day_query_gives_one_key_for_a_series_whatever_its_protocols():
    d4 = D5 - timedelta(days=1)
    rows = written([rec("2026-10-04", protocol="panel:rotA"), rec("2026-10-04", protocol="panel:rotB")], "c4")
    got = prior_query({"c4": (d4, 8, "ok")}, rows)
    assert {(r["market"], r["series"], r["route"], r["lane_class"]) for r in got} == {PRIOR_KEY}


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
    assert writers.zero_yield_prior(object(), D5) == {("ZA", "a", "prism/profiles", "panel")}
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


# A route whose series output is retired on purpose lands nothing every day by design (wave8/collect bdc1545: the
# tiktok/song/videos curve is a page sample and is written to no series), so it is never a zero-yield day.

def test_a_route_with_retired_series_output_is_never_marked_but_a_real_dead_route_still_is():
    assert "tiktok/song/videos" in writers.ZERO_YIELD_RETIRED_ROUTES
    curve = rec(route="tiktok/song/videos", lane="watchlist", series="curve_tiktok_sound", protocol="p",
                market="GLOBAL")
    dead = rec(route="tiktok/song", lane="watchlist", series="counter_tiktok_sound", protocol="p",
               market="GLOBAL")
    prior = prior_zero(("GLOBAL", "curve_tiktok_sound", "tiktok/song/videos", "watchlist"),
                       ("GLOBAL", "counter_tiktok_sound", "tiktok/song", "watchlist"))
    rows = health([curve, dead], prior)
    assert rows[(DAY, "GLOBAL", "curve_tiktok_sound", "p")]["valid"] is True
    assert rows[(DAY, "GLOBAL", "counter_tiktok_sound", "p")]["invalid_reason"] == "zero_yield"
    assert not writers.zero_yield_route("tiktok/song/videos", "watchlist")
    assert writers.zero_yield_route("tiktok/song", "watchlist")


def test_the_retired_route_does_not_count_as_a_prior_zero_day(monkeypatch):
    monkeypatch.setattr(writers, "_query", lambda bq, sql, params: [
        {"market": "GLOBAL", "series": "curve_tiktok_sound", "protocol": "p", "route": "tiktok/song/videos",
         "lane_class": "watchlist"},
        {"market": "GLOBAL", "series": "counter_tiktok_sound", "protocol": "p", "route": "tiktok/song",
         "lane_class": "watchlist"}])
    assert writers.zero_yield_prior(object(), D5) == {("GLOBAL", "counter_tiktok_sound", "tiktok/song", "watchlist")}


# write_run reads what it needs (the 28 day reference and the day before's zero-yield keys) before its first write.
# A read that fails after the observation and counter appends leaves the day with those rows and no collection_health
# rows, and the rerun appends the observations and counters a second time.

def failing_read(marker, other=()):
    from core.collect.tests.test_job import FakeBQ, FakeJob

    class ReadFails(FakeBQ):
        def query(self, sql, job_config=None, **kw):
            if marker in sql and not any(o in sql for o in other) and not getattr(job_config, "dry_run", False):
                raise RuntimeError("transient read failure")
            return super().query(sql, job_config, **kw)

    return ReadFails()


@pytest.mark.parametrize("name,marker,other", [
    ("zero_yield_prior", "l.calls_ok = l.calls", ()),
    ("reference", "collection_health", ("l.calls_ok = l.calls",)),
])
def test_a_failed_read_in_write_run_leaves_every_table_unwritten(name, marker, other):
    from core.collect.tests.test_job import FakeClient, collect

    run = collect(FakeClient())
    assert run.observations and run.counters, "the run must hold rows to append"
    bq = failing_read(marker, other)
    with pytest.raises(RuntimeError, match="transient read failure"):
        writers.write_run(bq, run, "collect-1")
    assert bq.loads == [], f"{name} failed after an append: {[t for t, _, _ in bq.loads]}"
    assert bq.posts == {} and bq.mapped == [] and bq.creators == {}
