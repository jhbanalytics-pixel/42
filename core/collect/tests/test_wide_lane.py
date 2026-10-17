"""Row 16w: the morning run's wider net, the market's subreddit, LinkedIn, Bluesky and Pinterest searches on each
market's first queries."""

from datetime import date

import pytest

from core.collect import job
from core.collect.parse import parse
from core.collect.socialcrawl_client import Refused, quote_for
from core.collect.tests.test_job import NOW, FakeClient, FakeGeo, MONDAY, TUESDAY, collect, fake_item_id

FETCHED = "2026-09-28T00:30:00Z"


def wide(planned):
    return {m: [c for c in calls if c.row == job.WIDE_ROW] for m, calls in planned["calls"].items()}


def body(platform, post_id, url):
    return {"success": True, "data": {"items": [{"post": {
        "id": post_id, "platform": platform, "url": url, "published_at": "2026-09-27T09:00:00Z",
        "author": {"username": "someone"}, "content": {"text": "Sunday braai in Soweto #amapiano"},
        "engagement": {"likes": 12}}}]}}


# Prices

def test_the_new_routes_are_priced_at_their_list_prices_and_nothing_else_is_accepted():
    assert quote_for("bluesky/search", "GET", {"query": "x", "sort": "latest", "since": "2026-10-10"}) == 1
    assert quote_for("pinterest/search", "GET", {"query": "x"}) == 1
    assert quote_for("reddit/subreddit/search", "GET", {"subreddit": "Kenya", "query": "x", "sort": "new",
                                                        "timeframe": "week"}) == 1
    assert quote_for("search/multi", "GET", {"query": "x", "platforms": "linkedin", "since": "2026-10-10"}) == 5
    with pytest.raises(Refused):
        quote_for("pinterest/search", "GET", {"query": "x", "include": "engagement"})  # 1 a row, not priced


# Plan

def test_each_market_searches_its_first_row_16_queries_in_its_subreddit_and_on_linkedin_bluesky_and_pinterest():
    planned = job.plan(TUESDAY)
    for market in job.MARKETS:
        calls = wide(planned)[market]
        queries = []
        for c in planned["calls"][market]:
            if c.row == "16" and c.seed is not None and c.lane != "placebo" and c.seed.query not in queries:
                queries.append(c.seed.query)
        assert [(c.route, c.params["query"]) for c in calls] == [
            (route, q) for route, count in job.WIDE_TOP for q in queries[:count]]
        for c in calls:
            assert c.seed is None and c.lane_class() == "search_presence" and c.series() == "search"
            assert "planned=" in c.series_protocol()
        assert {c.params.get("platforms") for c in calls if c.route == "search/multi"} == {"linkedin"}
        assert {c.params["subreddit"] for c in calls if c.route == "reddit/subreddit/search"} == {job.SUBREDDITS[market]}
        assert sum(c.hold() for c in calls) == 24


@pytest.mark.parametrize("day", [TUESDAY, MONDAY, date(2026, 10, 6), date(2026, 10, 17)])
def test_the_lane_leaves_every_earlier_call_and_the_curated_panel_as_they_were_and_the_cap_holds(day):
    with_wide, without = job.plan(day, reels={}), job.plan(day, reels={}, wide=False)
    for market, calls in with_wide["calls"].items():
        others = [c for c in calls if c.row != job.WIDE_ROW]
        assert [(c.route, c.params) for c in others] == [(c.route, c.params) for c in without["calls"][market]]
    full = job.plan(day)
    cap = job.load_caps()["ENGINE_DAILY"]["collect"]
    local = job.local_sources.total_hold(job.local_sources.plan(day))
    assert full["total"] + local + job.google_trends.MAX_SC_CREDITS <= cap


def test_a_zero_count_turns_a_route_off(monkeypatch):
    monkeypatch.setattr(job, "WIDE_TOP", (("search/multi", 0), ("bluesky/search", 2), ("pinterest/search", 0)))
    calls = [c for cs in wide(job.plan(TUESDAY)).values() for c in cs]
    assert calls and {c.route for c in calls} == {"bluesky/search"}


def test_no_wide_searches_under_a_cap_override_or_in_a_repair():
    assert not any(wide(job.plan(TUESDAY, share_cap=150)).values())
    client = FakeClient()
    collect(client, share_cap=150)
    assert not [c for c in client.calls if c["route"] in ("bluesky/search", "pinterest/search",
                                                          "reddit/subreddit/search")]


# Parse

@pytest.mark.parametrize("route, platform, url", [
    ("bluesky/search", "bluesky", "https://bsky.app/profile/someone.bsky.social/post/3k1"),
    ("pinterest/search", "pinterest", "https://www.pinterest.com/pin/1234/"),
    ("reddit/subreddit/search", "reddit", "https://www.reddit.com/r/southafrica/comments/abc/braai/"),
])
def test_a_search_row_becomes_a_search_presence_post_on_its_platform(route, platform, url):
    out = parse(route, {"query": "braai"}, "ZA", body(platform, "p1", url), FETCHED, "run1",
                item_id_fn=lambda kind, raw, p: f"{kind}|{raw}", geo_fn=FakeGeo(), lane="expansion")
    assert [p["platform"] for p in out["posts"]] == [platform]
    assert [o["lane_class"] for o in out["observations"]] == ["search_presence"]


def test_a_linkedin_row_from_search_multi_keeps_its_platform():
    data = {"success": True, "data": {"sources": {"linkedin": {"items": body(
        "linkedin", "li1", "https://www.linkedin.com/posts/someone_activity-1")["data"]["items"]}}}}
    out = parse("search/multi", {"query": "braai", "platforms": "linkedin"}, "ZA", data, FETCHED, "run1",
                item_id_fn=lambda kind, raw, p: f"{kind}|{raw}", geo_fn=FakeGeo(), lane="expansion")
    assert [p["platform"] for p in out["posts"]] == ["linkedin"]


# Run

def test_a_wide_search_past_the_room_is_not_made_and_costs_nothing():
    run = job.Collected("wide-room-test")
    run.credits = 1000
    client = FakeClient()
    runner = job._Runner(client, run, fake_item_id, FakeGeo(), lambda: NOW, job.Budget(), {}, TUESDAY,
                         reels_room=1005)
    calls = [job.Call(job.WIDE_ROW, "search/multi", {"query": q, "platforms": "linkedin", "since": "2026-09-22"},
                      "ZA", "expansion") for q in ("amapiano", "braai")]
    runner.calls(calls, "ZA")
    assert [c["params"]["query"] for c in client.calls] == ["amapiano"]
    assert [r["status"] for r in run.records] == ["ok", "over_share"] and run.records[1]["calls"] == 0
    assert run.stopped is None and not run.stopped_markets


def test_an_unreadable_wide_body_fails_only_that_call(monkeypatch):
    real = job.parse_with_creators

    def broken(route, *a, **kw):
        if route in ("bluesky/search", "pinterest/search", "reddit/subreddit/search"):
            raise KeyError("unexpected body")
        return real(route, *a, **kw)

    monkeypatch.setattr(job, "parse_with_creators", broken)
    run = collect(FakeClient())
    records = [r for r in run.records if r["route"] in ("bluesky/search", "pinterest/search", "reddit/subreddit/search")]
    assert records and all(r["status"] == "unparsable" and not r["ok"] for r in records)
    assert run.posts and run.stopped is None
