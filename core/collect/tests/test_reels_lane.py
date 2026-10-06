"""Row 14i: Instagram reel searches with the creator card, read for the creator's declared country."""

from datetime import date

import pytest

from core.collect import chain, job, writers
from core.collect.parse import parse
from core.collect.socialcrawl_client import Refused, quote_for
from core.collect.tests.test_job import (NOW, FakeBQ, FakeClient, FakeGeo, MONDAY, TUESDAY, collect, fake_item_id,
                                        run_main)
from core.detect.geo import geo_for_post, is_local

FETCHED = "2026-09-28T00:30:00Z"
ROUTE = "instagram/search/reels"


def reel_body(country, *, post_id="ig_r9", region=None):
    ext = {"author_id": "51009", "author_followers": 1200}
    if country is not None:
        ext["author_country"] = country
    author = {"username": "reel.maker"}
    if region is not None:
        author["region"] = region
    return {"success": True, "data": {"items": [{"computed": {"language": "en"}, "post": {
        "id": post_id, "url": f"https://www.instagram.com/reel/{post_id}/", "published_at": "2026-09-27T09:00:00Z",
        "author": author, "content": {"text": "weekend plans"}, "engagement": {"views": 900, "likes": 40},
        "ext": ext}}]}}


def parsed(country, market, **kw):
    return parse(ROUTE, {"query": "weekend", "include": "creator"}, market, reel_body(country, **kw), FETCHED, "run1",
                 item_id_fn=lambda kind, raw, platform: f"{kind}|{raw}", geo_fn=geo_for_post, lane="expansion")


# Parse

@pytest.mark.parametrize("market, country", [("ZA", "South Africa"), ("NG", "Nigeria"), ("KE", "Kenya"),
                                             ("ZA", "ZA"), ("KE", "kenya")])
def test_the_creators_declared_country_is_their_home_market_at_0_8(market, country):
    post = parsed(country, market)["posts"][0]
    assert (post["geo_market"], post["geo_confidence"], post["geo_source"]) == (market, 0.8, "home_market")
    assert is_local(post["geo_market"], post["geo_confidence"], market)
    assert writers._located(post)


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_a_foreign_or_missing_declared_country_is_never_local(market):
    ghana = parsed("Ghana", market)["posts"][0]
    assert (ghana["geo_market"], ghana["geo_confidence"], ghana["geo_source"]) == ("GH", 0.8, "home_market")
    assert not is_local(ghana["geo_market"], ghana["geo_confidence"], market)
    for country in ("Spain", None, ""):
        post = parsed(country, market)["posts"][0]
        assert (post["geo_market"], post["geo_confidence"], post["geo_source"]) == (None, None, None)


def test_the_authors_own_region_still_comes_first():
    post = parsed("Kenya", "ZA", region="ZA")["posts"][0]
    assert (post["geo_market"], post["geo_source"]) == ("ZA", "home_market")


def test_reel_sightings_are_search_presence_with_no_source_market():
    out = parsed("South Africa", "ZA")
    obs = out["observations"][0]
    assert (obs["lane_class"], obs["series"], obs["lane"]) == ("search_presence", "search", "expansion")
    assert obs["source_market"] is None and obs["source_region"] is None
    assert out["posts"][0]["platform"] == "instagram" and out["posts"][0]["creator_tier_at_post"] == "nano"


# Price

def test_the_creator_card_holds_60_a_page_and_nothing_else_is_accepted():
    assert quote_for(ROUTE, "GET", {"query": "x"}) == 1
    assert quote_for(ROUTE, "GET", {"query": "x", "include": "creator"}) == 61
    with pytest.raises(Refused):
        quote_for(ROUTE, "GET", {"query": "x", "include": "profile"})
    with pytest.raises(Refused):
        quote_for(ROUTE, "GET", {"query": "x", "region": "ZA"})


# Plan

def reels(planned):
    return {m: [c for c in calls if c.route == ROUTE] for m, calls in planned["calls"].items()}


def test_reels_counts_fill_the_room_market_by_market_in_run_order():
    assert job.reels_counts(183) == {"ZA": 1, "NG": 1, "KE": 1}
    assert job.reels_counts(150) == {"ZA": 1, "NG": 1, "KE": 0}
    assert job.reels_counts(60) == job.reels_counts(-5) == {"ZA": 0, "NG": 0, "KE": 0}
    assert job.reels_counts(130, {"ZA": 2, "NG": 1, "KE": 1}) == {"ZA": 2, "NG": 0, "KE": 0}
    assert job.reels_counts(10_000, {"ZA": 2, "NG": 0, "KE": 1}) == {"ZA": 2, "NG": 0, "KE": 1}


def test_a_planned_lane_searches_the_markets_first_row_14_queries_with_the_creator_card():
    planned = job.plan(TUESDAY, reels={"ZA": 1, "NG": 2, "KE": 1})
    for market, count in (("ZA", 1), ("NG", 2), ("KE", 1)):
        calls = reels(planned)[market]
        row14 = [c for c in planned["calls"][market] if c.row == "14" and c.route == "tiktok/search/top"
                 and c.lane != "placebo"]
        assert [c.params for c in calls] == [{"query": c.seed.query, "include": "creator"} for c in row14[:count]]
        for c in calls:
            assert c.row == job.REELS_ROW and c.hold() == 61 and c.seed is None
            assert c.lane_class() == "search_presence" and c.series() == "search"
            assert "include=creator" in c.series_protocol()
        # Made last in the market, after the row 22 evidence read.
        assert planned["calls"][market][-len(calls):] == calls


def test_a_zero_count_turns_a_markets_lane_off(monkeypatch):
    monkeypatch.setattr(job, "REELS_TOP", {"ZA": 0, "NG": 0, "KE": 0})
    assert not any(reels(job.plan(TUESDAY)).values())
    client = FakeClient()
    collect(client)
    assert not reel_calls(client)
    monkeypatch.setattr(job, "REELS_TOP", {"ZA": 0, "NG": 0, "KE": 1})
    planned = reels(job.plan(TUESDAY))
    assert len(planned["KE"]) == 1 and not planned["NG"] and not planned["ZA"]


@pytest.mark.parametrize("day", [TUESDAY, MONDAY, date(2026, 10, 6), date(2026, 10, 8)])
def test_the_lane_takes_only_room_left_so_no_other_call_moves_and_the_cap_holds(day):
    with_reels, without = job.plan(day), job.plan(day, reels={})
    cap = job.load_caps()["ENGINE_DAILY"]["collect"]
    local = job.local_sources.total_hold(job.local_sources.plan(day))
    assert with_reels["total"] + local <= cap
    for market, calls in with_reels["calls"].items():
        others = [c for c in calls if c.route != ROUTE]
        assert [(c.route, c.params) for c in others] == [(c.route, c.params) for c in without["calls"][market]]
    extra = sum(c.hold() for cs in reels(with_reels).values() for c in cs)
    assert with_reels["total"] == without["total"] + extra
    assert extra <= cap - without["total"] - local - job.google_trends.MAX_SC_CREDITS


def test_no_reels_under_a_cap_override():
    assert not any(reels(job.plan(TUESDAY, share_cap=150)).values())
    client = FakeClient()
    collect(client, share_cap=150)
    assert not reel_calls(client)


def test_a_run_makes_a_reel_search_only_while_the_share_less_its_charges_keeps_the_holds_after_it():
    client = FakeClient()
    run = collect(client)
    other = sum(c["credits"] for c in client.charged if c["route"] != ROUTE)
    made = reel_calls(client)
    cap = job.load_caps()["ENGINE_DAILY"]["collect"]
    assert [c["market"] for c in made] == ["ZA", "NG", "KE"][:len(made)]
    assert other + 61 * len(made) <= cap
    assert len(made) == 3 or cap < other + 61 * (len(made) + 1)
    unmade = [r for r in run.records if r["route"] == ROUTE and r["status"] == "over_share"]
    assert len(made) + len(unmade) == 3 and all(r["calls"] == 0 for r in unmade)


def test_a_live_run_keeps_the_google_trends_and_local_holds_out_of_the_reel_searches_room():
    client = FakeClient()
    assert run_main(["--run-date", "2026-09-29"], client=client) == 0
    other = sum(c["credits"] for c in client.charged if c["route"] != ROUTE and c["route"] != "web/scrape")
    room = job.load_caps()["ENGINE_DAILY"]["collect"] - job.google_trends.MAX_SC_CREDITS \
        - job.local_sources.total_hold(job.local_sources.plan(TUESDAY))
    made = reel_calls(client)
    assert made and other + 61 * len(made) <= room


def test_a_reel_search_past_the_room_is_not_made_and_costs_nothing():
    run = job.Collected("reels-room-test")
    run.credits = 1000
    client = FakeClient()
    runner = job._Runner(client, run, fake_item_id, FakeGeo(), lambda: NOW, job.Budget(), {}, TUESDAY,
                         reels_room=1100)
    calls = [job.Call(job.REELS_ROW, ROUTE, {"query": q, "include": "creator"}, "ZA", "expansion")
             for q in ("amapiano", "braai")]
    runner.calls(calls, "ZA")
    assert [c["params"]["query"] for c in client.calls] == ["amapiano"]
    assert [r["status"] for r in run.records] == ["ok", "over_share"] and run.credits == 1061
    assert run.records[1]["calls"] == 0 and run.stopped is None and not run.stopped_markets


# Failure is the call's own

def reel_calls(client):
    return [c for c in client.calls if c["route"] == ROUTE]


@pytest.mark.parametrize("status", ["error", "forbidden", "refunded", "empty"])
def test_a_failed_reel_search_changes_nothing_else_in_the_run(status):
    base_client = FakeClient()
    base = collect(base_client)
    assert reel_calls(base_client), "the default plan has room for the lane on a Tuesday"
    client = FakeClient(lambda n, route, market: status if route == ROUTE else None)
    run = collect(client)
    assert run.stopped is None and not run.stopped_markets
    assert [c for c in client.calls if c["route"] != ROUTE] == [c for c in base_client.calls if c["route"] != ROUTE]
    records = [r for r in run.records if r["route"] == ROUTE]
    assert records and all(r["lane_class"] == "search_presence" for r in records)
    baseline = [r for r in run.records if r["lane_class"] in ("unbiased_rank", "panel", "unbiased_counter")]
    assert baseline == [r for r in base.records if r["lane_class"] in ("unbiased_rank", "panel", "unbiased_counter")]


def test_an_unreadable_reel_body_fails_only_that_call(monkeypatch):
    real = job.parse_with_creators

    def broken(route, *a, **kw):
        if route == ROUTE:
            raise KeyError("unexpected body")
        return real(route, *a, **kw)

    monkeypatch.setattr(job, "parse_with_creators", broken)
    run = collect(FakeClient())
    records = [r for r in run.records if r["route"] == ROUTE]
    assert records and all(r["status"] == "unparsable" and not r["ok"] for r in records)
    assert run.posts and run.stopped is None


def test_a_run_whose_reel_searches_all_fail_finishes_ok_and_starts_detect():
    runs, bq = chain.MemoryRunsStore(), FakeBQ()
    client = FakeClient(lambda n, route, market: "error" if route == ROUTE else None)
    assert run_main(["--run-date", "2026-09-29"], client=client, runs=runs, bq=bq) == 0
    assert runs.rows[-1]["status"] == "ok"
    health = [h for h in bq.loaded("collection_health") if h["route"] == ROUTE]
    assert health and all(h["lane_class"] == "search_presence" and not h["valid"] for h in health)
