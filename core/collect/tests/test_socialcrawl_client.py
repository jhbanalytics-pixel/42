"""Unit tests for core/collect/socialcrawl_client.py (BUILD.md task 1.1). No network: HTTP is a fake."""

import copy
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.collect.socialcrawl_client import (
    PRICED, Refused, SocialCrawlClient, floor_for, forbidden, list_price, load_caps, quote_for)
from core.collect.stores import BigQueryLedgerStore, MemoryLedgerStore, MemoryRawStore

HERE = Path(__file__).resolve().parent
COLLECT = HERE.parent
SAMPLES = json.loads((HERE / "fixtures" / "responses.json").read_text(encoding="utf-8"))

KEY_ENV = "SOCIALCRAWL_OGILVY_API_KEY"
SENTINEL = "sc-test-sentinel-4f1d"
NOW = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)  # 06:00 in Johannesburg
TODAY = date(2026, 9, 28)

GT = "".join(["google", "_trends"])

LEDGER_COLUMNS = {
    "trend_date", "run_id", "job", "lane", "agent", "market", "platform", "endpoint", "route",
    "params_hash", "item_id", "calls", "credits_quoted", "credits_charged", "cache_hit",
    "posts_new", "balance_after", "logged_at",
}
RAW_COLUMNS = {
    "run_id", "job", "market", "route", "params_hash", "lane", "seed_key", "fetched_at",
    "http_status", "credits_quoted", "credits_charged", "cache_hit", "body",
}
LABEL_KEYS = {"relevance", "labels", "judgments", "sponsored", "intent", "niche"}


class FakeHTTP:
    """Answers by route; a list is consumed in order, a tuple repeats. Records every request.

    An answer is (status, body) or (status, body, headers); the call returns (status, body, headers).
    """

    def __init__(self, answers=None, balance=250000):
        self.answers = dict(answers or {})
        if balance is not None:
            self.answers.setdefault("credits/balance", (200, {"success": True, "data": {"balance": balance}}))
        self.requests = []

    def __call__(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        route = url.split("/v1/", 1)[1]
        self.requests.append({"method": method, "route": route, "params": params, "json": json, "headers": headers})
        answer = self.answers.get(route)
        if answer is None:
            raise AssertionError(f"unexpected request to {route}")
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body, headers = (tuple(answer) + ({},))[:3]
        return status, copy.deepcopy(body), dict(headers)

    def routes(self):
        return [r["route"] for r in self.requests]


class NoHTTP:
    def __call__(self, *args, **kwargs):
        raise AssertionError("HTTP must not be called")


def ok(sample, credits=None):
    body = copy.deepcopy(SAMPLES[sample])
    if credits is not None:
        body["credits_used"] = credits
    return (200, body)


def make(share="collect", http=None, ledger=None, raw=None, mode="live", **kw):
    kw.setdefault("retry_wait", 0)
    return SocialCrawlClient(
        share=share,
        run_id="run-1",
        mode=mode,
        ledger=ledger if ledger is not None else MemoryLedgerStore(),
        raw=raw if raw is not None else MemoryRawStore(),
        http=http if http is not None else FakeHTTP({"tiktok/trending": ok("trending")}),
        clock=lambda: NOW,
        **kw,
    )


def paid(client):
    """Ledger rows for the calls themselves, without the balance read's zero-credit row."""
    return [r for r in client.ledger.rows if r["route"] != "credits/balance"]


def spent(ledger, job, credits, day=TODAY):
    ledger.append({"trend_date": day.isoformat(), "job": job, "lane": None, "credits_charged": credits, "cache_hit": False})


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, SENTINEL)


TRENDING = ("tiktok/trending", {"region": "ZA", "feed": "local"})
COLLECT_SHARE = load_caps()["ENGINE_DAILY"]["collect"]  # the morning job's share, read from core/config/caps.yaml
CONFIRM_SHARE = load_caps()["ENGINE_DAILY"]["confirm"]
RESERVE_SHARE = load_caps()["ENGINE_DAILY"]["reserve"]
MONTH = load_caps()["MONTHLY"]["total"]


# Caps per share -------------------------------------------------------------

SHARE_CAPS = [
    ("collect", {}, COLLECT_SHARE),
    ("confirm", {}, CONFIRM_SHARE),
    ("reserve", {}, RESERVE_SHARE),
    ("ask", {}, 600),
    ("eval", {"eval_refresh": True}, 100),
    ("build", {"schedule_started": False}, 600),
    ("build", {"schedule_started": True}, 150),
    ("pulse", {}, 60),
]
# VIDEO_DAILY's credits start at 0, so video reading's share is tested at a raised cap.
VIDEO_CAPS = {**load_caps(), "VIDEO_DAILY": {"clips": 20, "credits": 40}}


@pytest.mark.parametrize("share,flags,cap", SHARE_CAPS)
def test_share_cap_refuses_the_call_that_would_cross_it(share, flags, cap):
    ledger = MemoryLedgerStore()
    spent(ledger, share, cap - 4)
    http = FakeHTTP({"tiktok/trending": ok("trending")})
    r = make(share, http=http, ledger=ledger, **flags).call(*TRENDING, market="ZA")
    assert r.status == "cap_reached"
    assert r.credits_charged == 0
    assert "tiktok/trending" not in http.routes()


@pytest.mark.parametrize("share,flags,cap", SHARE_CAPS)
def test_share_cap_allows_the_call_that_lands_on_it(share, flags, cap):
    ledger = MemoryLedgerStore()
    spent(ledger, share, cap - 5)
    r = make(share, ledger=ledger, **flags).call(*TRENDING, market="ZA")
    assert r.status == "ok"


def test_eval_has_no_live_credits_by_default():
    r = make("eval").call(*TRENDING, market="ZA")
    assert r.status == "cap_reached"


def test_build_cap_defaults_to_the_after_schedule_value():
    ledger = MemoryLedgerStore()
    spent(ledger, "build", 146)
    assert make("build", ledger=ledger).call(*TRENDING).status == "cap_reached"


def test_other_shares_and_other_days_do_not_count_against_a_share():
    ledger = MemoryLedgerStore()
    spent(ledger, "ask", 600)
    spent(ledger, "collect", COLLECT_SHARE, day=TODAY - timedelta(days=1))
    assert make("collect", ledger=ledger).call(*TRENDING).status == "ok"


def test_spend_is_summed_by_job_not_by_lane():
    ledger = MemoryLedgerStore()
    ledger.append({"trend_date": TODAY.isoformat(), "job": "ask", "lane": "collect",
                   "credits_charged": COLLECT_SHARE})
    client = make("collect", ledger=ledger)
    assert client.call(*TRENDING, lane="collect").status == "ok"
    ledger.append({"trend_date": TODAY.isoformat(), "job": "collect", "lane": "exploration",
                   "credits_charged": COLLECT_SHARE - 8})
    assert client.call("tiktok/trending", {"region": "NG", "feed": "local"}, lane="sweep").status == "cap_reached"


def test_lane_defaults_to_none():
    client = make()
    client.call(*TRENDING)
    assert paid(client)[0]["lane"] is None and client.raw.rows[0]["lane"] is None


def test_cache_hit_ledger_row_carries_job_and_lane():
    client = make("confirm")
    client.call(*TRENDING, lane="confirm")
    client.call(*TRENDING, lane="expansion")
    hit = paid(client)[1]
    assert (hit["job"], hit["lane"], hit["cache_hit"]) == ("confirm", "expansion", True)


def test_pulse_share_reads_its_cap_from_pulse_daily():
    assert make("pulse").share_caps["pulse"] == load_caps()["PULSE_DAILY"] == 60
    caps = load_caps()
    caps["PULSE_DAILY"] = 30
    assert make("pulse", caps=caps).share_caps["pulse"] == 30


def test_pulse_share_refuses_past_60_and_charges_nothing():
    ledger = MemoryLedgerStore()
    spent(ledger, "pulse", 60)
    http = FakeHTTP({"tiktok/hashtag": ok("trending", credits=1)})
    r = make("pulse", http=http, ledger=ledger).call("tiktok/hashtag", {"hashtag": "amapiano"})
    assert r.status == "cap_reached" and "pulse share" in r.reason
    assert r.credits_charged == 0 and http.routes() == []


def test_adding_the_pulse_share_changes_no_other_cap():
    assert make("collect").share_caps == {
        "collect": 2000, "confirm": 300, "reserve": 100, "ask": 600, "eval": 0, "build": 150, "pulse": 60,
        "video": 600}


def test_video_share_reads_its_credits_from_video_daily_and_spends_nothing_at_zero():
    assert make("video").share_caps["video"] == load_caps()["VIDEO_DAILY"]["credits"] == 600
    zero = {**load_caps(), "VIDEO_DAILY": {"clips": 0, "credits": 0}}
    assert make("video", caps=zero).share_caps["video"] == 0
    http = FakeHTTP({"tiktok/video/screen-text": ok("trending", credits=5)})
    r = make("video", http=http, caps=zero).call("tiktok/video/screen-text", {"url": "https://www.tiktok.com/@a/video/1"})
    assert r.status == "cap_reached" and "video share" in r.reason
    assert r.credits_charged == 0 and http.routes() == []


def test_video_share_is_ledgered_as_video_and_stops_at_its_cap():
    ledger = MemoryLedgerStore()
    http = FakeHTTP({"tiktok/video/screen-text": ok("trending", credits=5)})
    client = make("video", http=http, ledger=ledger, caps=VIDEO_CAPS)
    assert client.call("tiktok/video/screen-text", {"url": "https://www.tiktok.com/@a/video/1"}).status == "ok"
    assert [(r["job"], r["credits_charged"]) for r in paid(client)] == [("video", 5)]
    spent(ledger, "video", 31)
    r = client.call("tiktok/video/screen-text", {"url": "https://www.tiktok.com/@a/video/2"})
    assert r.status == "cap_reached" and "video share" in r.reason


def test_unknown_share_is_refused_at_construction():
    with pytest.raises(ValueError):
        make("brief")


# Monthly cap ----------------------------------------------------------------


def month_ledger(total):
    ledger = MemoryLedgerStore()
    spent(ledger, "collect", total, day=date(2026, 9, 1))
    spent(ledger, "collect", 100000, day=date(2026, 8, 31))  # last month never counts
    return ledger


@pytest.mark.parametrize("share", ["ask", "build", "eval"])
def test_monthly_cap_throttles_ask_build_and_eval(share):
    r = make(share, ledger=month_ledger(MONTH - 4), eval_refresh=True).call(*TRENDING)
    assert r.status == "cap_reached"
    assert "month" in r.reason


def test_monthly_cap_lets_the_call_that_lands_on_it_through():
    assert make("ask", ledger=month_ledger(MONTH - 5)).call(*TRENDING).status == "ok"


@pytest.mark.parametrize("share", ["collect", "confirm", "reserve"])
def test_morning_shares_are_protected_from_the_monthly_cap(share):
    assert make(share, ledger=month_ledger(MONTH)).call(*TRENDING).status == "ok"


def test_monthly_cap_throttles_the_pulse():
    r = make("pulse", ledger=month_ledger(MONTH - 4)).call(*TRENDING)
    assert r.status == "cap_reached" and "month" in r.reason
    assert make("pulse", ledger=month_ledger(MONTH - 5)).call(*TRENDING).status == "ok"


def test_pulse_spend_counts_toward_the_monthly_cap():
    ledger = month_ledger(MONTH - 10)
    spent(ledger, "pulse", 6)
    r = make("ask", ledger=ledger).call(*TRENDING)
    assert r.status == "cap_reached" and str(MONTH - 4) in r.reason


def test_pulse_charges_are_ledgered_under_the_pulse_job():
    client = make("pulse")
    assert client.call(*TRENDING).status == "ok"
    assert [(r["job"], r["credits_charged"]) for r in paid(client)] == [("pulse", 5)]
    assert client.raw.rows[0]["job"] == "pulse"


# Balance floor --------------------------------------------------------------


def test_balance_below_floor_refuses():
    http = FakeHTTP({"tiktok/trending": ok("trending")}, balance=19999)
    r = make(http=http).call(*TRENDING)
    assert r.status == "balance_floor"
    assert http.routes() == ["credits/balance"]


def test_balance_is_read_once_then_decremented_by_charges():
    http = FakeHTTP({"tiktok/trending": ok("trending"), "tiktok/hashtag": ok("trending", credits=1)}, balance=20004)
    client = make(http=http)
    first = client.call(*TRENDING)
    second = client.call("tiktok/hashtag", {"hashtag": "amapiano"})
    assert first.status == "ok" and first.credits_charged == 5
    assert second.status == "balance_floor"
    assert http.routes().count("credits/balance") == 1
    assert paid(client)[0]["balance_after"] == 19999


def test_balance_floor_applies_to_the_pulse():
    http = FakeHTTP({"tiktok/trending": ok("trending")}, balance=19999)
    r = make("pulse", http=http).call(*TRENDING)
    assert r.status == "balance_floor"
    assert http.routes() == ["credits/balance"]


def test_unreadable_balance_refuses():
    http = FakeHTTP({"tiktok/trending": ok("trending"), "credits/balance": (500, None)}, balance=None)
    client = make(http=http)
    r = client.call(*TRENDING)
    assert r.status == "balance_floor"
    assert "tiktok/trending" not in http.routes()
    assert [(row["route"], row["credits_charged"], row["balance_after"]) for row in client.ledger.rows] == [
        ("credits/balance", 0, None)
    ]


FREE_CALLS = [
    ("credits/balance", {}),
    ("credits/transactions", {"limit": 50}),
    ("status", {}),
    ("utility/capabilities", {}),
    ("utility/endpoints", {"platform": "tiktok"}),
    ("utility/endpoint", {"id": "tiktok-trending"}),
]


@pytest.mark.parametrize("route,params", FREE_CALLS)
def test_free_routes_pass_under_the_balance_floor_while_a_priced_route_is_refused(route, params):
    answers = {"tiktok/trending": ok("trending")}
    if route != "credits/balance":
        answers[route] = ok("trending", credits=0)
    http = FakeHTTP(answers, balance=19999)
    client = make("reserve", http=http)
    r = client.call(route, params)
    assert r.status == "ok" and r.credits_charged == 0
    assert http.routes() == [route]
    assert [(row["route"], row["credits_charged"], row["calls"]) for row in client.ledger.rows] == [(route, 0, 1)]
    assert [row["route"] for row in client.raw.rows] == [route]
    priced = client.call(*TRENDING)
    assert priced.status == "balance_floor"
    assert "tiktok/trending" not in http.routes()


@pytest.mark.parametrize("route,params,reason", [
    ("credits/transactions", {"limit": 50, "page_size": 10}, "unpriced parameter"),
    ("credits/balance", {"q": GT}, "Google Trends"),
    ("credits/balance", {}, "not a priced 42 route"),
])
def test_free_routes_under_the_floor_still_meet_every_other_rule(route, params, reason):
    client = make("reserve", http=NoHTTP())
    r = client.call(route, params, method="POST" if reason == "not a priced 42 route" else None)
    assert r.status == "forbidden" and reason in r.reason
    assert client.ledger.rows == [] and client.raw.rows == []


def test_a_free_route_is_refused_on_a_share_already_over_its_cap():
    ledger = MemoryLedgerStore()
    spent(ledger, "reserve", RESERVE_SHARE + 1)
    client = make("reserve", http=NoHTTP(), ledger=ledger)
    assert client.call("credits/balance", {}).status == "cap_reached"


# Forbidden routes -----------------------------------------------------------

NEVER_USED = [
    GT + "/explore",
    GT + "/rising",
    GT + "/unknown",
    "prism/trend-board",
    "prism/earliness",
    "prism/audience-language",
    "tiktok/user/audience",
    "twitter/ai-search",
    "prism/investigate",
    "prism/answers",
    "prism/ai-visibility",
]


@pytest.mark.parametrize("route", [GT + "/trending"])
def test_exact_google_trends_client_routes_pass_the_route_gate(route):
    assert forbidden(route, "GET", {}) == ""


@pytest.mark.parametrize("route", [
    GT + "/explore",
    GT + "/rising",
    GT + "/unknown",
    GT + "/trending/",
    "googletrends/trending",
    "google-trends/trending",
    "Google_Trends/trending",
])
def test_other_google_trends_routes_remain_blocked(route):
    assert "Google Trends" in forbidden(route, "GET", {})


def test_an_approved_google_trends_route_keeps_the_parameter_guard():
    assert "Google Trends" in forbidden(GT + "/trending", "GET", {"query": GT})


@pytest.mark.parametrize("route", NEVER_USED)
def test_never_used_routes_are_refused(route):
    http = NoHTTP()
    r = make(http=http).call(route, {"q": "amapiano"})
    assert r.status == "forbidden"
    assert r.credits_charged == 0


@pytest.mark.parametrize("route", ["/" + GT + "/trending", "/v1/" + GT + "/trending", GT.upper() + "/trending"])
def test_forbidden_routes_cannot_be_dodged_by_spelling(route):
    result = make(http=NoHTTP()).call(route, {})
    assert result.status == "forbidden"
    assert "Google Trends" in result.reason


@pytest.mark.parametrize("route", [
    "/prism/trend-board", "/v1/PRISM/TREND-BOARD", "PRISM/TREND-BOARD",
    "/prism/earliness", "/v1/PRISM/EARLINESS", "PRISM/EARLINESS",
    "prism%2Ftrend-board", "prism%2fearliness",
])
def test_never_used_prism_routes_remain_blocked_when_aliased_or_encoded(route):
    client = make(http=NoHTTP())
    assert client.call(route, {}).status == "forbidden"
    assert client.ledger.rows == [] and client.raw.rows == []


def test_google_lane_of_a_prism_route_is_refused():
    r = make(http=NoHTTP()).call("prism/reputation", {"brand": "x", "sources": "trustpilot," + GT})
    assert r.status == "forbidden"


def test_refused_calls_write_no_rows():
    client = make(http=NoHTTP())
    client.call("prism/trend-board", {"country_code": "ZA"})
    assert client.ledger.rows == [] and client.raw.rows == []


# search/everywhere ----------------------------------------------------------

AI_LANES = {"perplexity", "tavily", "twitter-ai-search", "polymarket"}


def everywhere_http():
    return FakeHTTP({"search/everywhere": ok("search_top", credits=20)})


def test_search_everywhere_always_sends_the_exclude():
    http = everywhere_http()
    r = make("reserve", http=http).call("search/everywhere", {"query": "fuel price"})
    assert r.status == "ok"
    sent = [q for q in http.requests if q["route"] == "search/everywhere"][0]["params"]
    assert set(sent["exclude"].split(",")) == AI_LANES


def test_search_everywhere_keeps_extra_exclusions():
    http = everywhere_http()
    r = make("reserve", http=http).call(
        "search/everywhere", {"query": "x", "exclude": "polymarket,tavily,perplexity,twitter-ai-search,news"}
    )
    assert r.status == "ok"
    sent = [q for q in http.requests if q["route"] == "search/everywhere"][0]["params"]
    assert set(sent["exclude"].split(",")) == AI_LANES | {"news"}


@pytest.mark.parametrize(
    "params",
    [
        {"query": "x", "exclude": "perplexity,tavily"},
        {"query": "x", "exclude": ""},
        {"query": "x", "sources": "tiktok,perplexity"},
        {"query": "x", "sources": "twitter-ai-search"},
    ],
)
def test_search_everywhere_refuses_an_override(params):
    assert make("reserve", http=NoHTTP()).call("search/everywhere", params).status == "forbidden"


# Ledger and raw rows --------------------------------------------------------


def test_one_ledger_row_and_one_raw_row_per_live_call():
    client = make()
    r = client.call(*TRENDING, market="ZA", item_id="amapiano", seed_key="feed:za", agent="collector", lane="sweep")
    assert r.status == "ok"
    assert len(paid(client)) == 1 and len(client.raw.rows) == 1
    row = paid(client)[0]
    assert set(row) == LEDGER_COLUMNS
    assert row["trend_date"] == "2026-09-28"
    assert (row["run_id"], row["job"], row["lane"], row["agent"]) == ("run-1", "collect", "sweep", "collector")
    assert (row["market"], row["platform"], row["endpoint"], row["route"]) == ("ZA", "tiktok", "trending", "tiktok/trending")
    assert row["item_id"] == "amapiano"
    assert row["calls"] == 1 and row["cache_hit"] is False
    assert row["credits_quoted"] == 5 and row["credits_charged"] == 5
    assert row["balance_after"] == 250000 - 5
    assert row["params_hash"] == r.params_hash
    raw = client.raw.rows[0]
    assert set(raw) == RAW_COLUMNS
    assert raw["http_status"] == 200 and raw["seed_key"] == "feed:za" and raw["cache_hit"] is False
    assert raw["job"] == "collect" and raw["lane"] == "sweep"
    assert raw["route"] == "tiktok/trending" and raw["params_hash"] == r.params_hash


@pytest.mark.parametrize("reported,charged", [(7, 7), (1, 5), (0, 5), (None, 5)])
def test_charge_is_the_higher_of_quoted_and_reported(reported, charged):
    body = copy.deepcopy(SAMPLES["trending"])
    if reported is None:
        del body["credits_used"]
    else:
        body["credits_used"] = reported
    client = make(http=FakeHTTP({"tiktok/trending": (200, body)}))
    r = client.call(*TRENDING)
    assert r.credits_quoted == 5
    assert r.credits_charged == charged
    assert paid(client)[0]["credits_charged"] == charged
    assert client.raw.rows[0]["credits_charged"] == charged


@pytest.mark.parametrize("header_name", ["x-credit-cost", "X-Credit-Cost", "X-CREDIT-COST"])
def test_header_alone_reports_the_charge(header_name):
    body = copy.deepcopy(SAMPLES["trending"])
    del body["credits_used"]
    client = make("ask", http=FakeHTTP({"search/multi": (200, body, {header_name: "9"})}))
    r = client.call("search/multi", {"query": "fuel price"})
    assert r.credits_quoted == 16
    assert r.credits_charged == 9  # without the header this would be the hold, 16
    assert paid(client)[0]["credits_charged"] == 9


@pytest.mark.parametrize("header,body_credits,charged", [("7", 1, 7), ("2", 6, 6), ("junk", 6, 6)])
def test_reported_is_the_higher_of_header_and_body(header, body_credits, charged):
    body = copy.deepcopy(SAMPLES["trending"])
    body["credits_used"] = body_credits
    client = make(http=FakeHTTP({"tiktok/trending": (200, body, {"X-Credit-Cost": header})}))
    assert client.call(*TRENDING).credits_charged == charged


def test_spend_is_counted_from_the_ledger_across_calls():
    http = FakeHTTP({"tiktok/trending": (200, SAMPLES["trending"])})
    ledger = MemoryLedgerStore()
    spent(ledger, "reserve", RESERVE_SHARE - 10)
    client = make("reserve", http=http, ledger=ledger)
    assert client.call("tiktok/trending", {"region": "ZA", "feed": "local"}).status == "ok"
    assert client.call("tiktok/trending", {"region": "NG", "feed": "local"}).status == "ok"
    assert client.call("tiktok/trending", {"region": "KE", "feed": "local"}).status == "cap_reached"


@pytest.mark.parametrize("status_code,headers,charged", [(502, {}, 0), (503, {}, 0), (503, {"x-credit-cost": "2"}, 2)])
def test_502_and_503_charge_only_what_the_vendor_reports(status_code, headers, charged):
    client = make(http=FakeHTTP({"tiktok/trending": (status_code, None, headers)}))
    r = client.call(*TRENDING)
    assert r.status == "refunded" and r.credits_charged == charged
    assert paid(client)[0]["credits_charged"] == charged


@pytest.mark.parametrize("reported,charged", [(0, 0), (3, 3), (None, 0)])
def test_insufficient_credits_charges_only_what_the_vendor_reports(reported, charged):
    body = copy.deepcopy(SAMPLES["insufficient_credits"])
    if reported is None:
        del body["credits_used"]
    else:
        body["credits_used"] = reported
    client = make(http=FakeHTTP({"tiktok/trending": (402, body)}))
    r = client.call(*TRENDING)
    assert r.status == "insufficient_credits" and r.credits_charged == charged


@pytest.mark.parametrize("status_code", [502, 503])
def test_502_and_503_are_refunded(status_code):
    client = make(http=FakeHTTP({"tiktok/trending": (status_code, None)}))
    r = client.call(*TRENDING)
    assert r.status == "refunded" and r.credits_charged == 0
    assert paid(client)[0]["credits_charged"] == 0
    assert client.raw.rows[0]["http_status"] == status_code


def test_empty_page_is_refunded():
    client = make(http=FakeHTTP({"tiktok/trending": ok("empty_page")}))
    r = client.call(*TRENDING)
    assert r.status == "empty" and r.credits_charged == 0
    assert paid(client)[0]["credits_charged"] == 0


@pytest.mark.parametrize("reported,charged", [(0, 0), (2, 2), (None, 5)])
def test_empty_page_is_charged_what_the_vendor_reports(reported, charged):
    body = copy.deepcopy(SAMPLES["empty_page"])
    if reported is None:
        del body["credits_used"]
    else:
        body["credits_used"] = reported
    client = make(http=FakeHTTP({"tiktok/trending": (200, body)}))
    r = client.call(*TRENDING)
    assert r.status == "empty" and r.credits_charged == charged
    assert paid(client)[0]["credits_charged"] == charged


def row(post_id, **computed):
    return {"computed": computed, "post": {"id": post_id}}


@pytest.mark.parametrize(
    "data,status,n_items",
    [
        ({"items": [], "posts": [row("p1")]}, "ok", 1),
        ({"sources": {"tiktok": {"items": []}, "instagram": {"items": []}}}, "empty", 0),
        ({"items": [], "sources": {"reddit": {"items": [row("r1", relevance=0.7)]}}}, "ok", 1),
        ({"author": {"id": "a1"}}, "ok", 0),
        ({}, "empty", 0),
    ],
)
def test_empty_means_every_item_list_is_empty(data, status, n_items):
    client = make(http=FakeHTTP({"tiktok/trending": (200, {"success": True, "credits_used": 5, "data": data})}))
    r = client.call(*TRENDING)
    assert r.status == status and len(r.items) == n_items
    assert label_keys_in(r.items) == set()


def test_vendor_error_is_charged_at_the_quote():
    client = make(http=FakeHTTP({"youtube/videos/trending": (500, None)}))
    r = client.call(*CHANNEL_TRENDING)
    assert r.status == "error" and r.credits_quoted == 6 and r.credits_charged == 6  # list price is 1


def test_reported_vendor_error_is_charged_the_list_price_or_more():
    client = make(http=FakeHTTP({"youtube/videos/trending": (404, SAMPLES["not_found"])}))
    r = client.call(*CHANNEL_TRENDING)
    assert r.status == "error" and r.credits_charged == 1
    assert "RESOURCE_NOT_FOUND" in r.reason


def unreported(status=200, sample="trending"):
    body = copy.deepcopy(SAMPLES[sample])
    del body["credits_used"]
    return (status, body)


@pytest.mark.parametrize(
    "route,params,charged",
    [
        ("prism/post-stats", {"urls": [f"https://www.linkedin.com/posts/p{i}" for i in range(100)]}, 500),
        ("youtube/transcripts", {"ids": [f"v{i}" for i in range(100)]}, 300),
        ("search/news", {"query": "x", "engines": "google,bing"}, 62),
        ("tiktok/hashtags/popular", {"countryCode": "ZA", "industry": "all"}, 96),
    ],
)
def test_unreported_charge_is_the_hold(route, params, charged):
    client = make("collect", http=FakeHTTP({route: unreported()}))
    r = client.call(route, params)
    assert r.status == "ok" and r.credits_charged == charged
    assert paid(client)[0]["credits_charged"] == charged


def test_unreported_empty_page_is_charged_the_hold():
    client = make(http=FakeHTTP({"youtube/videos/trending": unreported(sample="empty_page")}))
    r = client.call(*CHANNEL_TRENDING)
    assert r.status == "empty" and r.credits_charged == 6


@pytest.mark.parametrize("status_code", [500, 504])
def test_unreported_server_error_is_charged_the_hold(status_code):
    client = make(http=FakeHTTP({"youtube/videos/trending": (status_code, None)}))
    assert client.call(*CHANNEL_TRENDING).credits_charged == 6


def test_repeated_unreported_timeouts_stop_at_the_share_cap():
    fits = COLLECT_SHARE // 50  # holds of 50 that the collect share takes
    http = FakeHTTP({"twitter/search/tweets": [TimeoutError("read timed out")] * (fits + 4)})
    client = make("collect", http=http)
    params = {"query": "x", "relevant_to": "fuel", "max_pages": 10}  # hold 50
    results = [client.call("twitter/search/tweets", params) for _ in range(fits + 4)]
    assert [r.status for r in results] == ["error"] * fits + ["cap_reached"] * 4
    assert sum(r["credits_charged"] for r in paid(client)) == COLLECT_SHARE
    assert http.routes().count("twitter/search/tweets") == fits


class FailingRaw(MemoryRawStore):
    def append(self, row):
        raise RuntimeError("insert into raw_responses failed")


def test_raw_failure_after_a_paid_call_still_leaves_the_ledger_row():
    client = make(raw=FailingRaw())
    r = client.call(*TRENDING)
    assert r.status == "error" and "raw_responses" in r.reason
    assert r.credits_charged == 5
    assert len(paid(client)) == 1
    assert paid(client)[0]["credits_charged"] == 5 and paid(client)[0]["calls"] == 1


def test_balance_read_writes_a_zero_credit_ledger_row():
    http = FakeHTTP({"tiktok/trending": ok("trending"), "tiktok/hashtag": ok("trending", credits=1)})
    client = make(http=http)
    client.call(*TRENDING)
    client.call("tiktok/hashtag", {"hashtag": "amapiano"})
    rows = [r for r in client.ledger.rows if r["route"] == "credits/balance"]
    assert len(rows) == 1 and client.ledger.rows[0] is not None and client.ledger.rows[0]["route"] == "credits/balance"
    assert rows[0]["credits_charged"] == 0 and rows[0]["calls"] == 1 and rows[0]["balance_after"] == 250000
    assert rows[0]["job"] == "collect" and rows[0]["cache_hit"] is False
    assert len(client.ledger.rows) == http.routes().count("credits/balance") + len(paid(client))


def test_transport_failure_is_an_error_with_rows():
    client = make(http=FakeHTTP({"tiktok/trending": [TimeoutError("read timed out")]}))
    r = client.call(*TRENDING)
    assert r.status == "error"
    assert client.raw.rows[0]["http_status"] is None
    assert paid(client)[0]["credits_charged"] == 5


def test_insufficient_credits_halts_the_client():
    http = FakeHTTP({"tiktok/trending": (402, SAMPLES["insufficient_credits"]), "tiktok/hashtag": ok("trending")})
    client = make(http=http)
    first = client.call(*TRENDING)
    second = client.call("tiktok/hashtag", {"hashtag": "amapiano"})
    third = client.call(*TRENDING)
    assert first.status == "insufficient_credits" and first.credits_charged == 0
    assert second.status == "insufficient_credits" and third.status == "insufficient_credits"
    assert "tiktok/hashtag" not in http.routes()
    assert http.routes().count("tiktok/trending") == 1
    assert len(paid(client)) == 1


# Same-day cache ---------------------------------------------------------------


def test_same_day_repeat_is_a_free_cache_hit():
    http = FakeHTTP({"tiktok/trending": [ok("trending")]})
    client = make(http=http)
    first = client.call(*TRENDING, market="ZA")
    again = client.call("tiktok/trending", {"feed": "local", "region": "ZA"}, market="ZA")
    assert first.status == "ok"
    assert again.status == "cached" and again.cache_hit is True
    assert again.credits_charged == 0 and again.credits_quoted == 0
    assert again.body == first.body
    assert http.routes().count("tiktok/trending") == 1
    assert len(client.raw.rows) == 1
    hit = paid(client)[1]
    assert hit["cache_hit"] is True and hit["credits_charged"] == 0 and hit["calls"] == 0


def test_use_cache_false_goes_live_on_an_identical_repeat():
    http = FakeHTTP({"tiktok/search/top": [ok("search_top", credits=1), ok("search_top", credits=0)]})
    client = make(http=http)
    params = {"query": "amapiano", "country": "KE", "seen": "za-2026-09-28"}
    first = client.call("tiktok/search/top", params)
    again = client.call("tiktok/search/top", params, use_cache=False)
    assert first.status == again.status == "ok"
    assert again.cache_hit is False and again.credits_quoted == 1
    assert http.routes().count("tiktok/search/top") == 2
    assert len(client.raw.rows) == 2 and len(paid(client)) == 2
    assert [r["cache_hit"] for r in paid(client)] == [False, False]
    assert [r["calls"] for r in paid(client)] == [1, 1]


def test_use_cache_false_still_checks_the_cap():
    ledger = MemoryLedgerStore()
    client = make("reserve", ledger=ledger)
    client.call(*TRENDING)
    spent(ledger, "reserve", RESERVE_SHARE - 5)
    assert client.call(*TRENDING, use_cache=False).status == "cap_reached"


def test_cache_hit_is_served_even_at_the_cap():
    ledger = MemoryLedgerStore()
    client = make("reserve", ledger=ledger)
    client.call(*TRENDING)
    spent(ledger, "reserve", RESERVE_SHARE)
    assert client.call(*TRENDING).status == "cached"


def test_yesterdays_response_is_not_a_live_cache_hit():
    raw = MemoryRawStore()
    old = make(raw=raw)
    old.clock = lambda: NOW - timedelta(days=1)
    old.call(*TRENDING)
    http = FakeHTTP({"tiktok/trending": ok("trending")})
    r = make(raw=raw, http=http).call(*TRENDING)
    assert r.status == "ok" and "tiktok/trending" in http.routes()


def test_refunded_response_is_not_served_from_cache():
    # Two 502s: the first call and its one retry are both refunded, so the next call must go live.
    http = FakeHTTP({"tiktok/trending": [(502, None), (502, None), ok("trending")]})
    client = make(http=http)
    assert client.call(*TRENDING).status == "refunded"
    assert client.call(*TRENDING).status == "ok"


# Retry of a refunded call --------------------------------------------------------


UNAVAILABLE = {"success": False, "credits_used": 0, "error": {
    "type": "SERVICE_UNAVAILABLE", "message": "tiktok is temporarily unavailable. Your credits have been refunded.",
    "retryable": True}}


@pytest.mark.parametrize("status_code", [502, 503])
def test_a_refunded_call_is_retried_once_and_the_caller_sees_the_retry(status_code):
    waits = []
    http = FakeHTTP({"tiktok/trending": [(status_code, UNAVAILABLE), ok("trending")]})
    client = make(http=http, retry_wait=20, sleep=waits.append)
    r = client.call(*TRENDING, market="ZA", seed_key="feed:za", lane="sweep")
    assert r.status == "ok" and r.http_status == 200 and r.credits_charged == 5 and r.items
    assert waits == [20]
    assert http.routes().count("tiktok/trending") == 2
    rows = paid(client)
    assert [(x["calls"], x["credits_quoted"], x["credits_charged"]) for x in rows] == [(1, 5, 0), (1, 5, 5)]
    assert all(x["market"] == "ZA" and x["lane"] == "sweep" for x in rows)
    assert [x["http_status"] for x in client.raw.rows] == [status_code, 200]
    assert [x["credits_charged"] for x in client.raw.rows] == [0, 5]


@pytest.mark.parametrize("status_code", [502, 503])
def test_google_trends_unpaid_refund_is_returned_without_retry(status_code):
    route = GT + "/trending"
    waits = []
    http = FakeHTTP({route: [(status_code, None), ok("trending")]}, balance=21000)
    client = make(http=http, retry_wait=20, sleep=waits.append)

    result = client.call(route, {"location": "ZA", "hours": "24"}, market="ZA")

    assert result.status == "refunded" and result.http_status == status_code and result.credits_charged == 0
    assert waits == []
    assert http.routes().count(route) == 1
    assert [(row["calls"], row["credits_quoted"], row["credits_charged"]) for row in paid(client)] == [(1, 5, 0)]
    assert len(client.raw.rows) == 1 and client.raw.rows[0]["http_status"] == status_code


def test_the_live_retry_wait_defaults_to_two_seconds():
    waits = []
    http = FakeHTTP({"tiktok/trending": [(503, UNAVAILABLE), ok("trending")]})
    client = SocialCrawlClient(share="collect", run_id="r", mode="live", ledger=MemoryLedgerStore(),
                               raw=MemoryRawStore(), http=http, clock=lambda: NOW, sleep=waits.append)
    assert client.call(*TRENDING).status == "ok"
    assert client.retry_wait == 2
    assert waits == [2]


def test_a_second_refund_is_final():
    waits = []
    http = FakeHTTP({"tiktok/trending": (503, UNAVAILABLE)})
    client = make(http=http, sleep=waits.append)
    r = client.call(*TRENDING)
    assert r.status == "refunded" and r.credits_charged == 0 and "503" in r.reason
    assert http.routes().count("tiktok/trending") == 2 and len(waits) == 1
    assert [x["credits_charged"] for x in paid(client)] == [0, 0]


@pytest.mark.parametrize("answer", [(500, None), (404, None), (503, None, {"x-credit-cost": "2"}),
                                    (402, SAMPLES["insufficient_credits"])])
def test_only_a_refund_at_no_charge_is_retried(answer):
    http = FakeHTTP({"tiktok/trending": [answer, ok("trending")]})
    client = make(http=http, sleep=lambda s: pytest.fail("no retry expected"))
    assert client.call(*TRENDING).status != "ok"
    assert http.routes().count("tiktok/trending") == 1 and len(paid(client)) == 1


def test_a_transport_failure_is_not_retried():
    http = FakeHTTP({"tiktok/trending": [TimeoutError("read timed out"), ok("trending")]})
    client = make(http=http)
    assert client.call(*TRENDING).status == "error"
    assert http.routes().count("tiktok/trending") == 1


def test_the_retry_does_not_double_hold_against_the_share():
    ledger = MemoryLedgerStore()
    spent(ledger, "collect", COLLECT_SHARE - 5)  # room for exactly one hold of 5
    http = FakeHTTP({"tiktok/trending": [(503, UNAVAILABLE), ok("trending")]})
    client = make(http=http, ledger=ledger)
    r = client.call(*TRENDING)
    assert r.status == "ok" and r.credits_charged == 5
    assert ledger.spent(TODAY, TODAY, job="collect") == COLLECT_SHARE
    assert [x["credits_charged"] for x in ledger.rows if x.get("route") == "tiktok/trending"] == [0, 5]


def test_the_retry_passes_the_share_cap_check():
    ledger = MemoryLedgerStore()
    http = FakeHTTP({"tiktok/trending": [(503, UNAVAILABLE), ok("trending")]})
    # Another caller on the same share spends all but 4 while this one waits, so the retry's hold of 5 crosses it.
    client = make(http=http, ledger=ledger, sleep=lambda s: spent(ledger, "collect", COLLECT_SHARE - 4))
    r = client.call(*TRENDING)
    assert r.status == "cap_reached" and r.credits_charged == 0 and "collect share" in r.reason
    assert http.routes().count("tiktok/trending") == 1
    assert [x["credits_charged"] for x in ledger.rows if x.get("route") == "tiktok/trending"] == [0]


def test_the_retry_passes_the_monthly_cap_check():
    ledger = month_ledger(MONTH - 10)
    http = FakeHTTP({"tiktok/trending": [(503, UNAVAILABLE), ok("trending")]})
    client = make("ask", http=http, ledger=ledger, sleep=lambda s: spent(ledger, "collect", 6))
    r = client.call(*TRENDING)
    assert r.status == "cap_reached" and "month" in r.reason
    assert http.routes().count("tiktok/trending") == 1


def test_the_retry_passes_the_balance_floor():
    http = FakeHTTP({"tiktok/trending": [(503, UNAVAILABLE), ok("trending")]}, balance=20000)
    client = make(http=http, sleep=lambda s: setattr(client, "_balance", 19999))
    r = client.call(*TRENDING)
    assert r.status == "balance_floor" and r.credits_charged == 0
    assert http.routes().count("tiktok/trending") == 1


def test_a_refund_at_no_charge_leaves_the_balance_for_the_retry():
    http = FakeHTTP({"tiktok/trending": [(503, UNAVAILABLE), ok("trending")]}, balance=20000)
    client = make(http=http)
    assert client.call(*TRENDING).status == "ok"
    assert http.routes().count("credits/balance") == 1 and client._balance == 20000 - 5


def test_health_counts_a_refunded_then_good_feed_unit_as_ok():
    from core.collect import job, writers

    def item_id(kind, raw, platform=None):
        return f"{kind}|{platform}:{raw}"

    def geo(platform, market, **kw):
        return None, None, None

    answers = [(503, UNAVAILABLE), ok("trending"), ok("trending"), ok("trending")]
    client = make(http=FakeHTTP({"tiktok/trending": answers}))
    run = job.Collected("run-1")
    runner = job._Runner(client, run, job._CountedIds(item_id), job.safe_geo(geo), lambda: NOW, job.Budget(),
                         None, TODAY)
    runner.calls([job.feed_pull("ZA", n) for n in (1, 2, 3)], "ZA")
    [feed] = [h for h in writers.health_rows(run.records, run.posts, {}, "run-1")
              if h["route"] == "tiktok/trending" and h["market"] == "ZA"]
    assert (feed["calls"], feed["calls_ok"]) == (3, 3)
    assert feed["invalid_reason"] != "calls"
    assert run.credits == 15 and len(paid(client)) == 4


# Replay mode ------------------------------------------------------------------


def recorded_raw():
    raw = MemoryRawStore()
    rec = make(raw=raw)
    rec.clock = lambda: NOW - timedelta(days=20)
    assert rec.call(*TRENDING, market="ZA").status == "ok"
    return raw


def test_replay_serves_from_the_raw_store_at_no_cost(monkeypatch):
    raw = recorded_raw()
    monkeypatch.delenv(KEY_ENV)
    ledger = MemoryLedgerStore()
    client = make("eval", mode="replay", http=NoHTTP(), raw=raw, ledger=ledger)
    r = client.call(*TRENDING, market="ZA")
    assert r.status == "cached" and r.credits_charged == 0 and r.items
    assert ledger.rows == []
    assert len(raw.rows) == 1


def test_replay_miss_is_not_in_replay():
    client = make("eval", mode="replay", http=NoHTTP())
    r = client.call("tiktok/hashtag", {"hashtag": "nothing-recorded"})
    assert r.status == "not_in_replay" and r.credits_charged == 0


def test_replay_ignores_caps_and_balance():
    ledger = MemoryLedgerStore()
    spent(ledger, "eval", 10**6)
    client = make("eval", mode="replay", http=NoHTTP(), raw=recorded_raw(), ledger=ledger)
    assert client.call(*TRENDING).status == "cached"


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        make(mode="dry")


# Vendor labels ----------------------------------------------------------------


def label_keys_in(node):
    found = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "vendor_labels":
                continue
            if k in LABEL_KEYS:
                found.add(k)
            found |= label_keys_in(v)
    elif isinstance(node, list):
        for v in node:
            found |= label_keys_in(v)
    return found


def test_vendor_labels_are_split_from_evidence():
    client = make("collect", http=FakeHTTP({"tiktok/search/top": ok("search_top")}))
    r = client.call("tiktok/search/top", {"query": "amapiano", "country": "ZA"})
    assert r.status == "ok"
    assert len(r.items) == 2 == len(r.vendor_labels)
    assert label_keys_in(r.items) == set()
    assert r.vendor_labels[0] == {
        "relevance": 0.91,
        "labels": {"sponsored": False, "intent": "entertain", "niche": "music"},
        "judgments": {"model": "vendor"},
    }
    assert r.vendor_labels[1] == {"relevance": 0.40, "sponsored": True, "intent": "sell", "niche": "fashion"}
    assert r.items[0]["computed"] == {"engagement_rate": 0.03, "language": "en"}
    stored = client.raw.rows[0]["body"]["data"]["items"]
    assert label_keys_in(stored) == set()
    assert [i["vendor_labels"] for i in stored] == r.vendor_labels


def test_items_without_labels_get_empty_vendor_labels():
    r = make().call(*TRENDING)
    assert r.vendor_labels == [{}]
    assert r.items[0]["post"]["id"] == "7400000000000000001"


def test_cached_and_replayed_bodies_stay_split():
    client = make("collect", http=FakeHTTP({"tiktok/search/top": ok("search_top")}))
    client.call("tiktok/search/top", {"query": "amapiano"})
    again = client.call("tiktok/search/top", {"query": "amapiano"})
    assert again.status == "cached"
    assert label_keys_in(again.items) == set() and again.vendor_labels[0]["relevance"] == 0.91


# PRICED ------------------------------------------------------------------------

SPEC = json.loads((COLLECT.parents[1] / "docs" / "full-42" / "reference" / "sc_routes.json").read_text(encoding="utf-8"))
SPEC_ROUTES = {(r["path"].strip("/"), r["method"]): {p["name"] for p in r["params"]} for r in SPEC["routes"]}
POST_BODY_ROUTES = {"prism/post-stats", "prism/profiles", "youtube/transcripts"}  # body keys are not in the spec


def test_every_priced_route_is_in_the_spec_with_the_same_method():
    for route, rule in PRICED.items():
        assert (route, rule.method) in SPEC_ROUTES, route


def test_every_accepted_parameter_is_one_the_spec_lists():
    for route, rule in PRICED.items():
        if route not in POST_BODY_ROUTES:
            assert rule.params <= SPEC_ROUTES[(route, rule.method)], (route, rule.params - SPEC_ROUTES[(route, rule.method)])


HOLDS = [
    ("tiktok/trending", {"region": "ZA", "feed": "local"}, 5),
    ("tiktok/hashtags/popular", {"countryCode": "ZA", "period": 7}, 6),
    ("tiktok/hashtags/popular", {"countryCode": "ZA", "period": 7, "industry": "all"}, 96),
    ("youtube/videos/trending", {"region": "NG", "category": 24}, 1),
    ("youtube/videos/trending", {"region": "NG", "include": "channel"}, 6),
    ("youtube/shorts/trending", {}, 5),
    ("youtube/shorts/trending", {"include": "channel"}, 15),
    ("apple_music/charts", {"country": "ng", "type": "songs"}, 1),
    ("reddit/subreddit", {"subreddit": "southafrica", "sort": "rising"}, 1),
    ("reddit/subreddit", {"subreddit": "southafrica", "label": "mention"}, 5),
    ("facebook/profile/posts", {"url": "https://facebook.com/x", "since": "2026-09-27"}, 1),
    ("facebook/profile/posts", {"url": "https://facebook.com/x", "include": "engagement"}, 4),
    ("facebook/events", {"url": "https://facebook.com/events/explore/johannesburg"}, 1),
    ("facebook/events", {"url": "https://facebook.com/events/explore/johannesburg", "include": "details"}, 13),
    ("instagram/search/location", {"query": "Soweto"}, 5),
    ("instagram/location/posts", {"location_id": "123"}, 5),
    ("tiktok/search/top", {"query": "x", "country": "KE", "seen": "s1"}, 1),
    ("tiktok/search/top", {"query": "x", "max_pages": 3}, 3),
    ("tiktok/song/videos", {"clipId": "1"}, 1),
    ("tiktok/hashtag", {"hashtag": "amapiano"}, 1),
    ("youtube/search/advanced", {"query": "x", "location": "6.52,3.37", "location_radius": "50km"}, 1),
    ("youtube/search/advanced", {"query": "x", "includeExtras": "true", "include": "channel"}, 11),
    ("youtube/search/advanced", {"query": "x", "includeExtras": "true", "max_pages": 2}, 12),
    ("twitter/search/tweets", {"query": "near:Lagos"}, 1),
    ("twitter/search/tweets", {"query": "x", "relevant_to": "fuel", "max_pages": 10}, 50),
    ("twitter/search/tweets", {"query": "x", "relevant_to": "fuel", "label": "mention", "max_pages": 10}, 90),
    ("reddit/search", {"query": "x"}, 1),
    ("reddit/search", {"query": "x", "include_body": "true"}, 26),
    ("threads/search", {"query": "x"}, 5),
    ("threads/search", {"query": "x", "expand": "false"}, 1),
    ("threads/search", {"query": "x", "expand": "false", "limit": 50}, 4),
    ("threads/search", {"query": "x", "expand": "false", "include": "engagement"}, 21),
    ("threads/post/comments", {"url": "u", "limit": 20}, 1),
    ("threads/post/comments", {"url": "u", "limit": 50}, 10),
    ("linkedin/search/posts", {"query": "x"}, 5),
    ("linkedin/search/posts", {"query": "x", "relevant_to": "y"}, 13),
    ("search/multi", {"query": "x"}, 16),
    ("search/multi", {"query": "x", "dry_run": 1}, 16),
    ("search/multi", {"query": "x", "platforms": "tiktok,instagram"}, 2),
    ("search/multi", {"query": "x", "platforms": "linkedin"}, 5),
    ("search/multi", {"query": "x", "platforms": "threads"}, 5),
    ("search/multi", {"query": "x", "platforms": "tiktok", "relevant_to": "y"}, 9),
    ("search/news", {"query": "x", "countries": "ZA,NG,KE", "max_legs": 3}, 5),
    ("search/news", {"query": "x", "countries": "ZA,NG,KE"}, 14),
    ("search/news", {"query": "x", "countries": "ZA"}, 7),
    ("search/news", {"query": "x", "engines": "google,bing"}, 62),
    ("search/everywhere", {"query": "x"}, 20),
    ("search/creators", {"query": "x"}, 10),
    ("search/creators", {"query": "x", "brief": "y"}, 12),
    ("tiktok/post/comments", {"url": "u", "sort": "top"}, 1),
    ("tiktok/post/comments", {"url": "u", "scan_pages": 7, "label": "spam"}, 35),
    ("instagram/post/comments", {"url": "u"}, 5),
    ("tiktok/post/transcript", {"url": "u"}, 10),
    ("youtube/video/transcript", {"url": "u"}, 3),
    ("tiktok/video/screen-text", {"url": "u"}, 5),
    ("prism/creator-card", {"handle": "x", "verify": "true"}, 5),
    ("prism/creator-card", {"handle": "x", "platforms": "tiktok,instagram,youtube,twitter,facebook,threads,linkedin"}, 8),
    ("web/scrape", {"url": "https://x.example"}, 1),
    ("web/scrape", {"url": "https://x.example", "proxy": "auto"}, 5),
    ("web/scrape", {"url": "https://x.example", "pdf_parse": "true"}, 5),
    ("credits/transactions", {"limit": 50}, 0),
    ("utility/capabilities", {}, 0),
]


@pytest.mark.parametrize("route,params,hold", HOLDS)
def test_quote_is_the_true_maximum_bill(route, params, hold):
    assert quote_for(route, PRICED[route].method, params) == hold


GOOGLE_TRENDS_TRENDING = GT + "/trending"
GOOGLE_TRENDS_CATEGORIES = [
    "autos_and_vehicles", "beauty_and_fashion", "business_and_finance", "entertainment", "food_and_drink",
    "games", "health", "hobbies_and_leisure", "jobs_and_education", "law_and_government", "other",
    "pets_and_animals", "politics", "science", "shopping", "sports", "technology",
    "travel_and_transportation", "climate",
]


def test_google_trends_trending_uses_the_static_catalog_price_and_floor():
    params = {"location": "ZA", "hours": "24"}
    route = GOOGLE_TRENDS_TRENDING
    assert quote_for(route, "GET", params) == 5
    assert floor_for(route, "GET", params) == 5
    assert list_price(route, "GET") == 5


@pytest.mark.parametrize("param,values", [
    ("hours", ["4", "24", "48", "168"]),
    ("category", GOOGLE_TRENDS_CATEGORIES),
    ("status", ["all", "active"]),
    ("sort", ["relevance", "search_volume", "recency", "title"]),
])
def test_google_trends_trending_accepts_every_documented_enum_value(param, values):
    params = {"location": "ZA"}
    for value in values:
        assert quote_for(GOOGLE_TRENDS_TRENDING, "GET", {**params, param: value}) == 5


@pytest.mark.parametrize("params", [{}, {"location": "  "}, {"location": None}])
def test_google_trends_trending_refuses_missing_required_location(params):
    with pytest.raises(Refused):
        quote_for(GOOGLE_TRENDS_TRENDING, "GET", params)


def test_google_trends_trending_refuses_undocumented_params():
    with pytest.raises(Refused, match="unpriced parameter"):
        quote_for(GOOGLE_TRENDS_TRENDING, "GET", {"location": "ZA", "max_pages": 2})


@pytest.mark.parametrize("params", [
    {"location": "ZA", "hours": "12"},
    {"location": "ZA", "category": "unknown"},
    {"location": "ZA", "status": "paused"},
    {"location": "ZA", "sort": "newest"},
])
def test_google_trends_trending_refuses_undocumented_enum_values(params):
    with pytest.raises(Refused):
        quote_for(GOOGLE_TRENDS_TRENDING, "GET", params)


@pytest.mark.parametrize("route,params", [
    (GT + "/explore", {"keywords": "x"}),
    (GT + "/rising", {"keyword": "x"}),
])
def test_explore_and_rising_remain_parked_outside_priced(route, params):
    result = make(http=NoHTTP()).call(route, params)
    assert result.status == "forbidden" and "Google Trends" in result.reason
    with pytest.raises(Refused, match="not a priced 42 route"):
        quote_for(route, "GET", params)


def test_three_synthetic_google_trends_calls_book_fifteen_credits_at_collect_cap():
    route = GOOGLE_TRENDS_TRENDING
    markets = ("ZA", "NG", "KE")
    http = FakeHTTP({route: ok("trending", credits=5)}, balance=21000)
    ledger = MemoryLedgerStore()
    spent(ledger, "collect", 785)
    client = make("collect", http=http, ledger=ledger)

    results = [client.call(route, {"location": market, "hours": "24"}, market=market) for market in markets]

    assert [result.status for result in results] == ["ok", "ok", "ok"]
    assert [result.credits_quoted for result in results] == [5, 5, 5]
    assert [result.credits_charged for result in results] == [5, 5, 5]
    assert [route for route in http.routes() if route != "credits/balance"] == [route] * 3
    assert [request["params"] for request in http.requests if request["route"] == route] == [
        {"location": market, "hours": "24"} for market in markets
    ]
    rows = [row for row in client.ledger.rows if row.get("route") == route]
    assert sum(row["credits_charged"] for row in rows) == 15


def test_google_trends_five_credit_hold_still_obeys_collect_cap():
    ledger = MemoryLedgerStore()
    spent(ledger, "collect", COLLECT_SHARE - 4)
    client = make("collect", http=NoHTTP(), ledger=ledger)

    result = client.call(GT + "/trending", {"location": "ZA"})

    assert result.status == "cap_reached"


def test_post_stats_holds_the_summed_worst_case_per_url():
    urls = ["https://www.tiktok.com/@a/video/1", "https://www.instagram.com/p/x", "https://www.linkedin.com/posts/y"]
    assert quote_for("prism/post-stats", "POST", {"urls": urls}) == 8
    assert quote_for("prism/post-stats", "POST", {"urls": ["https://www.instagram.com/p/x"] * 100}) == 200
    assert quote_for("prism/post-stats", "POST", {"urls": ["https://unknown.example/p"]}) == 5


def test_prism_profiles_holds_per_item_and_per_posts_page():
    linkedin = [{"platform": "linkedin", "handle": f"h{i}"} for i in range(50)]
    assert quote_for("prism/profiles", "POST", {"items": linkedin}) == 250  # reviewer reproduction
    tiktok = [{"platform": "tiktok", "handle": f"h{i}"} for i in range(25)]
    assert quote_for("prism/profiles", "POST", {"items": tiktok, "include": "posts", "since": "2026-09-27"}) == 50


def test_youtube_transcripts_hold_three_per_id():
    assert quote_for("youtube/transcripts", "POST", {"ids": [f"v{i}" for i in range(100)]}) == 300


@pytest.mark.parametrize(
    "route,method,params",
    [
        ("prism/post-stats", "POST", {"urls": ["https://www.tiktok.com/@a/video/1"] * 101}),
        ("prism/post-stats", "POST", {"urls": []}),
        ("prism/post-stats", "POST", {}),
        ("prism/profiles", "POST", {"items": [{"platform": "tiktok", "handle": "h"}] * 51}),
        ("prism/profiles", "POST", {"items": [{"platform": "tiktok", "handle": "h"}] * 26, "include": "posts"}),
        ("youtube/transcripts", "POST", {"ids": ["v"] * 101}),
        ("twitter/search/tweets", "GET", {"query": "x", "max_pages": 20}),  # reviewer reproduction
        ("tiktok/search/top", "GET", {"query": "x", "max_pages": 0}),
        ("tiktok/post/comments", "GET", {"url": "u", "scan_pages": 8}),
        ("threads/search", "GET", {"query": "x", "limit": 101}),
        ("threads/post/comments", "GET", {"url": "u", "limit": 51}),
        ("youtube/videos/trending", "GET", {"region": "ZA", "max_results": 51}),
        ("search/news", "GET", {"query": "x", "max_legs": 13}),
        ("search/news", "GET", {"query": "x", "engines": "yandex"}),
        ("search/multi", "GET", {"query": "x", "platforms": "tiktok,myspace"}),
        ("prism/creator-card", "GET", {"handle": "x", "platforms": "a,b,c,d,e,f,g,h"}),
        ("tiktok/hashtags/popular", "GET", {"countryCode": "ZA", "industry": "beauty"}),
        ("youtube/videos/trending", "GET", {"region": "ZA", "include": "engagement"}),
        ("web/scrape", "GET", {"url": "u", "proxy": "stealth"}),
    ],
)
def test_quotes_above_a_route_limit_are_refused(route, method, params):
    with pytest.raises(Refused):
        quote_for(route, method, params)
    client = make(http=NoHTTP())
    r = client.call(route, params, method=method)
    assert r.status == "forbidden"
    assert client.ledger.rows == []


@pytest.mark.parametrize(
    "route,method,params",
    [
        ("web/batch-scrape", "POST", {"urls": ["https://x.example"] * 500}),  # reviewer reproduction
        ("reddit/subreddit/search", "GET", {"query": "x", "include_body": "true"}),  # reviewer reproduction
        ("prism/jobs", "POST", {}),
        ("instagram/search/profiles", "GET", {"query": "x"}),
        ("xiaohongshu/search", "GET", {"query": "x"}),
        ("xiaohongshu/trending", "GET", {}),
        ("prism/adverse-screen", "GET", {"handle": "x"}),
        ("prism/handle-audit", "GET", {"handle": "x"}),
        ("prism/comment-leads", "GET", {"query": "x"}),
        ("tiktok/no-such-route", "GET", {}),
    ],
)
def test_route_outside_priced_is_refused(route, method, params):
    client = make(http=NoHTTP())
    r = client.call(route, params, method=method)
    assert r.status == "forbidden" and r.reason == "not a priced 42 route"
    with pytest.raises(Refused):
        quote_for(route, method, params)


def test_wrong_method_on_a_priced_route_is_refused():
    r = make(http=NoHTTP()).call("tiktok/trending", {"region": "ZA"}, method="POST")
    assert r.status == "forbidden" and r.reason == "not a priced 42 route"


@pytest.mark.parametrize(
    "route,params",
    [
        ("tiktok/trending", {"region": "ZA", "foo": "1"}),
        ("twitter/search/tweets", {"query": "x", "fit": "goal"}),
        ("twitter/search/tweets", {"query": "x", "switch_from": "a"}),
        ("linkedin/search/posts", {"query": "x", "limit": 50}),
        ("search/everywhere", {"query": "x", "include_transcripts": "true"}),
        ("prism/creator-card", {"handle": "x", "include": "posts"}),
        ("google_news/search", {"keyword": "x", "depth": 100}),
    ],
)
def test_unpriced_parameter_is_refused(route, params):
    client = make(http=NoHTTP())
    r = client.call(route, params)
    assert r.status == "forbidden" and r.reason.startswith("unpriced parameter")


@pytest.mark.parametrize(
    "params,hold",
    [
        ({"query": "x", "relevant_to": "0"}, 5),
        ({"query": "x", "label": "no"}, 5),
        ({"query": "x", "exclude": "false"}, 5),
        ({"query": "x", "relevant_to": "   "}, 1),
        ({"query": "x", "label": ""}, 1),
    ],
)
def test_text_add_ons_are_priced_whenever_present(params, hold):
    assert quote_for("twitter/search/tweets", "GET", params) == hold


def test_text_brief_is_priced_whenever_present():
    assert quote_for("search/creators", "GET", {"query": "x", "brief": "no"}) == 12


@pytest.mark.parametrize("value", ["true", "1", True, "false", "0", "no", "", False, None])
def test_boolean_add_ons_are_on_whenever_present(value):
    assert quote_for("reddit/search", "GET", {"query": "x", "include_body": value}) == 26
    assert quote_for("youtube/search/advanced", "GET", {"query": "x", "includeExtras": value}) == 6
    assert quote_for("web/scrape", "GET", {"url": "u", "pdf_parse": value}) == 5


def test_boolean_add_ons_are_off_only_when_absent():
    assert quote_for("reddit/search", "GET", {"query": "x"}) == 1
    assert quote_for("youtube/search/advanced", "GET", {"query": "x"}) == 1
    assert quote_for("web/scrape", "GET", {"url": "u"}) == 1


# Item 1: a non-finite or negative report is no report.

SEARCH_MULTI = ("search/multi", {"query": "fuel price"})  # hold 16, floor 0


@pytest.mark.parametrize(
    "credits_used,header,charged",
    [
        (5, "NaN", 5),
        (5, "nan", 5),
        (3, "inf", 3),
        (3, "-inf", 3),
        (2, "-4", 2),
        (None, "-4", 16),
        (None, "NaN", 16),
        (None, "Infinity", 16),
        (float("nan"), None, 16),
        (float("inf"), None, 16),
        (-7, None, 16),
    ],
)
def test_non_finite_or_negative_report_is_not_a_report(credits_used, header, charged):
    body = copy.deepcopy(SAMPLES["trending"])
    if credits_used is None:
        del body["credits_used"]
    else:
        body["credits_used"] = credits_used
    headers = {} if header is None else {"x-credit-cost": header}
    client = make("ask", http=FakeHTTP({"search/multi": (200, body, headers)}))
    r = client.call(*SEARCH_MULTI)
    assert r.credits_charged == charged
    assert paid(client)[0]["credits_charged"] == charged
    assert client.ledger.spent(TODAY, TODAY, job="ask") == charged


def test_nan_header_on_an_empty_page_charges_the_hold():
    body = copy.deepcopy(SAMPLES["empty_page"])
    del body["credits_used"]
    client = make(http=FakeHTTP({"youtube/videos/trending": (200, body, {"X-Credit-Cost": "NaN"})}))
    r = client.call(*CHANNEL_TRENDING)
    assert r.status == "empty" and r.credits_charged == 6


def test_nan_header_does_not_turn_the_caps_off():
    http = FakeHTTP({"search/multi": (200, {"success": True, "data": {"items": [{"post": {"id": "1"}}]}},
                                      {"x-credit-cost": "NaN"})})
    client = make("reserve", http=http)
    fits = RESERVE_SHARE // 16  # 16 a call against the reserve cap
    statuses = [client.call("search/multi", {"query": f"q{i}"}).status for i in range(fits + 2)]
    assert statuses == ["ok"] * fits + ["cap_reached", "cap_reached"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_memory_ledger_refuses_a_non_finite_spend(bad):
    ledger = MemoryLedgerStore()
    spent(ledger, "collect", 5)
    spent(ledger, "collect", bad)
    with pytest.raises(ValueError):
        ledger.spent(TODAY, TODAY, job="collect")


class NanLedger(MemoryLedgerStore):
    def spent(self, start, end, job=None):
        return float("nan")


def test_client_refuses_when_the_ledger_spend_is_not_finite():
    r = make(ledger=NanLedger(), http=NoHTTP()).call(*TRENDING)
    assert r.status == "cap_reached" and "finite" in r.reason


class FakeBigQuery:
    def __init__(self, rows):
        self.rows, self.sql = rows, None

    def query(self, sql, job_config=None):
        self.sql = sql
        return self

    def result(self):
        return self.rows


def test_bigquery_ledger_sums_finite_spend():
    bq = FakeBigQuery([{"spent": 7, "bad": 0}])
    assert BigQueryLedgerStore(bq, "p").spent(TODAY, TODAY, job="collect") == 7
    assert "IS_NAN" in bq.sql and "IS_INF" in bq.sql


def test_bigquery_ledger_refuses_non_finite_rows():
    with pytest.raises(ValueError):
        BigQueryLedgerStore(FakeBigQuery([{"spent": 7, "bad": 1}]), "p").spent(TODAY, TODAY, job="collect")


# Item 2: the charge floor when a report exists is the PRICED base price for the call.


@pytest.mark.parametrize(
    "route,params,reported,charged",
    [
        ("linkedin/search/posts", {"query": "x"}, 1, 5),
        ("linkedin/search/posts", {"query": "x", "relevant_to": "y"}, 1, 5),
        ("tiktok/search/top", {"query": "x", "max_pages": 3}, 1, 1),
        ("tiktok/hashtags/popular", {"countryCode": "ZA"}, 2, 6),
        ("search/multi", {"query": "x"}, 0, 0),
        ("prism/post-stats", {"urls": ["https://www.linkedin.com/posts/y"]}, 0, 0),
        ("web/scrape", {"url": "u", "proxy": "auto"}, 1, 5),
    ],
)
def test_charge_floor_is_the_priced_base_price(route, params, reported, charged):
    client = make("collect", http=FakeHTTP({route: ok("trending", credits=reported)}))
    assert client.call(route, params).credits_charged == charged


# Item 4: a failing ledger append halts the client.


class FailingLedger(MemoryLedgerStore):
    def __init__(self, fail_after):
        super().__init__()
        self.fail_after = fail_after

    def append(self, row):
        if len(self.rows) >= self.fail_after:
            raise RuntimeError("insert into credit_ledger failed")
        super().append(row)


def test_ledger_failure_after_a_paid_call_halts_the_client():
    http = FakeHTTP({"tiktok/trending": ok("trending"), "tiktok/hashtag": ok("trending", credits=1)})
    client = make(http=http, ledger=FailingLedger(fail_after=1))  # the balance row lands, the call row fails
    with pytest.raises(RuntimeError):
        client.call(*TRENDING)
    later = client.call("tiktok/hashtag", {"hashtag": "amapiano"})
    assert later.status == "error" and "credit_ledger" in later.reason
    assert "tiktok/hashtag" not in http.routes()


def test_ledger_failure_on_the_balance_read_halts_the_client():
    http = FakeHTTP({"tiktok/trending": ok("trending")})
    client = make(http=http, ledger=FailingLedger(fail_after=0))
    with pytest.raises(RuntimeError):
        client.call(*TRENDING)
    assert client.call(*TRENDING).status == "error"
    assert http.routes() == ["credits/balance"]


# Item 5: counts must be whole numbers; creator-card platforms must be known names.


@pytest.mark.parametrize(
    "route,params",
    [
        ("tiktok/search/top", {"query": "x", "max_pages": 3.7}),
        ("tiktok/search/top", {"query": "x", "max_pages": "3.7"}),
        ("tiktok/search/top", {"query": "x", "max_pages": 2.0}),
        ("tiktok/search/top", {"query": "x", "max_pages": True}),
        ("tiktok/search/top", {"query": "x", "max_pages": "two"}),
        ("tiktok/post/comments", {"url": "u", "scan_pages": 2.5}),
        ("threads/search", {"query": "x", "limit": 10.5}),
        ("threads/post/comments", {"url": "u", "limit": "30.2"}),
        ("search/news", {"query": "x", "max_legs": 3.3}),
        ("youtube/videos/trending", {"region": "ZA", "max_results": 10.5}),
    ],
)
def test_non_integer_counts_are_refused(route, params):
    with pytest.raises(Refused):
        quote_for(route, PRICED[route].method, params)


@pytest.mark.parametrize("value", [3, "3", " 3 "])
def test_whole_number_counts_are_accepted(value):
    assert quote_for("tiktok/search/top", "GET", {"query": "x", "max_pages": value}) == 3


@pytest.mark.parametrize("platforms", ["all", "tiktok,all", "", [], ["tiktok", "myspace"], "tiktok,,"])
def test_creator_card_platforms_must_be_known_names(platforms):
    with pytest.raises(Refused):
        quote_for("prism/creator-card", "GET", {"handle": "x", "platforms": platforms})


@pytest.mark.parametrize(
    "platforms,hold",
    [(["tiktok", "instagram"], 5), ("tiktok,tiktok", 5), (["tiktok", "instagram", "youtube", "twitter", "facebook"], 6)],
)
def test_creator_card_known_platforms_are_priced(platforms, hold):
    assert quote_for("prism/creator-card", "GET", {"handle": "x", "platforms": platforms}) == hold


@pytest.mark.parametrize("cursor,hold", [("abc", 1), ("  ", 5), ("", 5)])
def test_threads_relaxation_is_waived_only_by_a_real_cursor(cursor, hold):
    assert quote_for("threads/search", "GET", {"query": "x", "cursor": cursor}) == hold


def test_reviewer_reproductions_now_hold_at_least_the_vendor_charge():
    reddit = quote_for("reddit/search", "GET", {"query": "x", "include_body": "true"})
    assert reddit >= 26
    tweets = quote_for("twitter/search/tweets", "GET", {"query": "x", "relevant_to": "y", "max_pages": 10})
    assert tweets >= 50


@pytest.mark.parametrize(
    "route,method",
    [
        ("cohorts/c1/queries", "POST"),
        ("web/jobs", "GET"),
        ("monitors", "GET"),
        ("monitors/m1/runs", "GET"),
        ("prism/jobs/j1", "GET"),
        ("cohorts", "POST"),
    ],
)
def test_zero_price_route_off_the_free_list_is_refused(route, method):
    client = make(http=NoHTTP())
    r = client.call(route, {}, method=method)
    assert r.status == "forbidden"
    assert client.ledger.rows == [] and client.raw.rows == []


@pytest.mark.parametrize("route", ["credits/transactions", "status", "utility/capabilities", "utility/endpoints"])
def test_free_allowlist_routes_are_allowed_at_no_cost(route):
    client = make(http=FakeHTTP({route: ok("trending", credits=0)}))
    r = client.call(route, {})
    assert r.status == "ok" and r.credits_quoted == 0 and r.credits_charged == 0


@pytest.mark.parametrize(
    "route,method",
    [("monitors", "POST"), ("web/monitors", "POST"), ("monitors/m1", "PATCH"), ("web/monitors/m1", "PATCH")],
)
def test_monitor_creation_and_resume_are_refused(route, method):
    r = make(http=NoHTTP()).call(route, {"route": "tiktok/trending", "cadence_minutes": 60}, method=method)
    assert r.status == "forbidden"
    assert "monitor" in r.reason


@pytest.mark.parametrize("share,flags,cap", [s for s in SHARE_CAPS if s[2] > 0])
def test_a_call_at_exactly_the_cap_is_refused(share, flags, cap):
    ledger = MemoryLedgerStore()
    spent(ledger, share, cap)
    http = FakeHTTP({"tiktok/hashtag": ok("trending", credits=1)})
    r = make(share, http=http, ledger=ledger, **flags).call("tiktok/hashtag", {"hashtag": "amapiano"})
    assert r.status == "cap_reached"
    assert "tiktok/hashtag" not in http.routes()


CHANNEL_TRENDING = ("youtube/videos/trending", {"region": "ZA", "include": "channel"})


def test_cap_check_uses_the_hold_not_the_list_price():
    ledger = MemoryLedgerStore()
    spent(ledger, "reserve", RESERVE_SHARE - 5)  # 5 left: the list price of 1 fits, the hold of 6 does not
    http = FakeHTTP({"youtube/videos/trending": ok("trending", credits=1)})
    r = make("reserve", http=http, ledger=ledger).call(*CHANNEL_TRENDING)
    assert r.status == "cap_reached"


@pytest.mark.parametrize("reported,charged", [(1, 1), (3, 3), (0, 1), (None, 6)])
def test_list_priced_route_is_charged_the_list_price_or_more_or_the_hold_when_unreported(reported, charged):
    body = copy.deepcopy(SAMPLES["trending"])
    if reported is None:
        del body["credits_used"]
    else:
        body["credits_used"] = reported
    client = make(http=FakeHTTP({"youtube/videos/trending": (200, body)}))
    r = client.call(*CHANNEL_TRENDING)
    assert r.credits_quoted == 6
    assert r.credits_charged == charged


@pytest.mark.parametrize("reported,charged", [(12, 12), (0, 0), (None, 16)])
def test_metered_route_is_charged_what_the_vendor_reports_or_the_hold(reported, charged):
    body = copy.deepcopy(SAMPLES["trending"])
    if reported is None:
        del body["credits_used"]
    else:
        body["credits_used"] = reported
    client = make("ask", http=FakeHTTP({"search/multi": (200, body)}))
    r = client.call("search/multi", {"query": "fuel price"})
    assert r.credits_quoted == 16
    assert r.credits_charged == charged


def test_post_routes_send_a_json_body():
    http = FakeHTTP({"prism/post-stats": ok("trending", credits=1)})
    client = make("collect", http=http)
    r = client.call("prism/post-stats", {"urls": ["https://www.tiktok.com/@a/video/1"]})
    assert r.status == "ok"
    sent = [q for q in http.requests if q["route"] == "prism/post-stats"][0]
    assert sent["method"] == "POST" and sent["json"] == {"urls": ["https://www.tiktok.com/@a/video/1"]}


# The API key ------------------------------------------------------------------


def test_api_key_goes_only_into_the_request_header(capsys, caplog):
    caplog.set_level("DEBUG")
    http = FakeHTTP(
        {
            "tiktok/trending": [ok("trending")],
            "tiktok/hashtag": [RuntimeError("boom with header x-api-key=" + SENTINEL)],
            "tiktok/song": (402, SAMPLES["insufficient_credits"]),
        }
    )
    client = make(http=http)
    results = [
        client.call(*TRENDING),
        client.call("tiktok/hashtag", {"hashtag": "amapiano"}),
        client.call("tiktok/song", {"clipId": "1"}),
    ]
    assert all(q["headers"]["x-api-key"] == SENTINEL for q in http.requests)
    stored = json.dumps([client.ledger.rows, client.raw.rows], default=str)
    out = capsys.readouterr()
    for text in (stored, out.out, out.err, caplog.text, repr(results), repr(client), repr(vars(client))):
        assert SENTINEL not in text


def test_missing_key_is_an_error_without_rows(monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    client = make()
    r = client.call(*TRENDING)
    assert r.status == "error" and KEY_ENV in r.reason
    assert client.ledger.rows == [] and client.raw.rows == []


def test_key_is_not_read_before_a_live_call(monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    client = make(http=NoHTTP())
    assert client.call("prism/trend-board", {}).status == "forbidden"


# Ask routes: one post opened live per platform (L3 Needs 5), prism/mentions and instagram/tagged (L4 Needs 25)

# The spec's GET post-detail route per platform; X is twitter/tweet. The spec has no x/ route family.
POST_DETAIL = {
    "tiktok": "tiktok/post", "instagram": "instagram/post", "youtube": "youtube/video",
    "twitter": "twitter/tweet", "reddit": "reddit/post", "facebook": "facebook/post", "threads": "threads/post",
}

ASK_HOLDS = [
    ("tiktok/post", {"url": "u"}, 1),
    ("tiktok/post", {"url": "u", "region": "ZA", "trim": "true"}, 1),
    ("instagram/post", {"url": "u", "region": "ZA", "trim": "true"}, 1),
    ("youtube/video", {"url": "u", "language": "en", "hl": "en"}, 1),
    ("twitter/tweet", {"url": "u", "trim": "true"}, 1),
    ("reddit/post", {"url": "u"}, 1),
    ("facebook/post", {"url": "u"}, 1),
    ("threads/post", {"url": "u", "trim": "true"}, 1),
    ("instagram/tagged", {"handle": "h"}, 5),
    ("instagram/tagged", {"user_id": "1", "cursor": "c", "safe_url": "true"}, 5),
    # 1 an X or Reddit page, 5 an Instagram tag page, per search term (handle, url, name).
    ("prism/mentions", {"handle": "h", "platforms": "twitter"}, 1),
    ("prism/mentions", {"handle": "h", "platforms": "twitter,reddit"}, 2),
    ("prism/mentions", {"handle": "h", "platforms": "twitter,reddit,instagram"}, 7),
    ("prism/mentions", {"handle": "h", "platforms": ["reddit", "Twitter"], "since": "2026-09-22", "include_self": 0}, 2),
    ("prism/mentions", {"handle": "h", "url": "https://ogilvy.co.za", "platforms": "twitter,reddit"}, 4),
    ("prism/mentions", {"handle": "h", "url": "u", "name": "n", "platforms": "twitter,reddit,instagram"}, 21),
    ("prism/mentions", {"handle": "h", "name": " ", "platforms": "twitter,twitter,reddit"}, 2),
]


def test_every_platform_has_a_priced_post_detail_route_at_one_credit():
    for platform, route in POST_DETAIL.items():
        assert PRICED[route].method == "GET" and (route, "GET") in SPEC_ROUTES, platform
        assert "url" in PRICED[route].params
        assert floor_for(route, "GET", {"url": "u"}) == 1 == list_price(route, "GET")


@pytest.mark.parametrize("route,params,hold", ASK_HOLDS)
def test_ask_route_holds(route, params, hold):
    assert quote_for(route, PRICED[route].method, params) == hold


@pytest.mark.parametrize("route,params", [
    ("tiktok/post", {"url": "u", "download_media": "true"}),
    ("instagram/post", {"url": "u", "download_media": "true"}),
    ("facebook/post", {"url": "u", "get_comments": "true"}),
    ("facebook/post", {"url": "u", "get_transcript": "true"}),
])
def test_add_ons_the_spec_does_not_price_are_refused(route, params):
    client = make("ask", http=NoHTTP())
    r = client.call(route, params)
    assert r.status == "forbidden" and r.reason.startswith("unpriced parameter")
    assert client.ledger.rows == []


@pytest.mark.parametrize("params,reason", [
    ({"handle": "h", "platforms": "twitter,tiktok"}, "unpriced value platforms=tiktok"),
    ({"handle": "h", "platforms": "threads"}, "unpriced value platforms=threads"),
    ({"handle": "h", "platforms": "twitter", "cursor": "c"}, "unpriced parameter"),
    ({"platforms": "twitter,reddit"}, "needs a handle, url or name"),
    ({"handle": " ", "platforms": "twitter"}, "needs a handle, url or name"),
])
def test_prism_mentions_refuses_what_it_cannot_price(params, reason):
    with pytest.raises(Refused, match=reason):
        quote_for("prism/mentions", "GET", params)


def test_prism_mentions_rejects_x_selector_before_http():
    http = FakeHTTP({"prism/mentions": ok("trending", credits=7)})
    client = make("ask", http=http)
    result = client.call("prism/mentions", {"handle": "h", "platforms": "x,reddit,instagram"})
    assert (result.status, result.reason, http.routes()) == (
        "forbidden", "unpriced value platforms=x", [])


@pytest.mark.parametrize("params", [
    {"handle": "h"},
    {"handle": "h", "platforms": ""},
    {"query": "x"},
    {"handle": "h", "platforms": "twitter,web"},
    {"handle": "h", "platforms": "WEB"},
    {"handle": "h", "platforms": "google"},
    {"handle": "h", "platforms": ["reddit", "Google"]},
    {"handle": "h", "platforms": "twitter,google_news"},
    {"handle": "h", "platforms": "twitter," + GT},
])
def test_prism_mentions_never_searches_the_web_or_a_google_lane(params):
    """Rule 2: the default call includes the web lane, whose engine the spec does not name, so platforms
    must be named and may not include web or anything Google."""
    client = make("ask", http=NoHTTP())
    r = client.call("prism/mentions", params)
    assert r.status == "forbidden" and "rule 2" in r.reason
    assert client.ledger.rows == [] and client.raw.rows == []
    assert forbidden("prism/mentions", "GET", dict(params)) == r.reason


def test_a_google_lane_on_any_prism_route_is_refused():
    reason = forbidden("prism/creator-card", "GET", {"handle": "h", "platforms": "tiktok,google"})
    assert "rule 2" in reason
    assert forbidden("prism/creator-card", "GET", {"handle": "googlefan", "platforms": "tiktok"}) == ""
    assert forbidden("prism/mentions", "GET", {"handle": "google", "platforms": "twitter"}) == ""


@pytest.mark.parametrize("route,params,hold", [(r, p, h) for r, p, h in ASK_HOLDS if h <= 7])
def test_ask_share_books_each_new_route(route, params, hold):
    http = FakeHTTP({route: ok("trending", credits=hold)})
    client = make("ask", http=http)
    r = client.call(route, params)
    assert r.status == "ok" and r.credits_quoted == hold and r.credits_charged == hold
    row = paid(client)[0]
    assert (row["job"], row["route"], row["credits_charged"]) == ("ask", route, hold)
    assert client.raw.rows[0]["job"] == "ask"
    sent = [q for q in http.requests if q["route"] == route][0]
    assert sent["method"] == "GET" and sent["params"] == params


def test_ask_share_cap_holds_the_new_routes():
    ledger = MemoryLedgerStore()
    spent(ledger, "ask", 596)
    client = make("ask", http=NoHTTP(), ledger=ledger)
    assert client.call("instagram/tagged", {"handle": "h"}).status == "cap_reached"
    r = client.call("prism/mentions", {"handle": "h", "platforms": "twitter,reddit,instagram"})
    assert r.status == "cap_reached" and r.credits_quoted == 7


@pytest.mark.parametrize("reported,charged", [(0, 0), (1, 1), (7, 7), (None, 7)])
def test_prism_mentions_is_charged_what_the_vendor_reports_since_empty_pages_are_refunded(reported, charged):
    status, body = ok("trending", credits=reported)
    if reported is None:
        body.pop("credits_used")
    client = make("ask", http=FakeHTTP({"prism/mentions": (status, body)}))
    r = client.call("prism/mentions", {"handle": "h", "platforms": "twitter,reddit,instagram"})
    assert r.credits_charged == charged


@pytest.mark.parametrize("route,params,reported,charged", [
    ("tiktok/post", {"url": "u"}, 0, 1),
    ("youtube/video", {"url": "u"}, 3, 3),
    ("instagram/tagged", {"handle": "h"}, 1, 5),
])
def test_fixed_price_ask_routes_charge_at_least_the_spec_price(route, params, reported, charged):
    client = make("ask", http=FakeHTTP({route: ok("trending", credits=reported)}))
    assert client.call(route, params).credits_charged == charged


# Source hygiene ---------------------------------------------------------------


# Banned strings are joined at run time: CPython folds "a" + "b" into one constant in the bytecode.


def collect_files():
    return [p for p in COLLECT.rglob("*") if p.is_file()]


def test_zero_balance_key_name_is_not_in_the_code():
    name = "".join(["SOCIALCRAWL", "_API_KEY"])
    for path in collect_files():
        assert name.encode() not in path.read_bytes(), path


def test_no_banned_age_literals_in_collect_including_bytecode():
    words = ["".join(["gen", "z"]), "".join(["gen", " z"])]
    banned = re.compile("|".join(words).encode(), re.I)
    for path in collect_files():
        assert not banned.search(path.read_bytes()), path


def test_no_dashes_in_collect_text():
    marks = [chr(0x2014), chr(0x2013), "".join([" -", "- "])]
    for path in collect_files():
        if path.suffix in {".py", ".json", ".yaml"}:
            text = path.read_text(encoding="utf-8")
            assert not any(m in text for m in marks), path


def test_a_refunded_youtube_trending_category_call_is_retried_and_its_board_row_stays_valid():
    # Row 3 runs one call per category, and each category is its own collection_health row, so a lone
    # refunded 503 that was not retried would mark the YouTube trending board failed for the market-day.
    from core.collect import job, writers

    def item_id(kind, raw, platform=None):
        return f"{kind}|{platform}:{raw}"

    def geo(platform, market, **kw):
        return None, None, None

    body = json.loads((HERE / "fixtures" / "job_responses.json").read_text(encoding="utf-8"))[
        "youtube/videos/trending"]
    waits = []
    http = FakeHTTP({"youtube/videos/trending": [(503, UNAVAILABLE)] + [(200, body)] * 5})
    client = make(http=http, sleep=waits.append)
    run = job.Collected("run-1")
    runner = job._Runner(client, run, job._CountedIds(item_id), job.safe_geo(geo), lambda: NOW, job.Budget(),
                         None, TODAY)
    board = [c for c in job.harvest_calls("ZA", TODAY, job.load_config())[0] if c.route == "youtube/videos/trending"]
    assert len(board) == len(job.YOUTUBE_CATEGORIES)
    runner.calls(board, "ZA")
    assert http.routes().count("youtube/videos/trending") == len(board) + 1 and len(waits) == 1
    rows = [h for h in writers.health_rows(run.records, run.posts, {}, "run-1")
            if h["series"] == "board_youtube" and h["market"] == "ZA"]
    assert len(rows) == len(board)
    assert all((h["calls"], h["calls_ok"], h["valid"]) == (1, 1, True) for h in rows)
    assert sum(x["credits_charged"] for x in paid(client) if x["route"] == "youtube/videos/trending") == len(board)


# Transient retries on a client's retry routes -------------------------------

BOARD = ("youtube/videos/trending", {"region": "ZA"})  # hold 1


class ReadTimeout(Exception):
    """Named like requests.exceptions.ReadTimeout, which the client knows only by class name."""


class ConnectionError_(OSError):
    pass


ConnectionError_.__name__ = "ConnectionError"  # requests.exceptions.ConnectionError is an OSError of that name


def board_ok():
    return (200, json.loads((HERE / "fixtures" / "job_responses.json").read_text(encoding="utf-8"))[
        "youtube/videos/trending"])


def retrying(http, **kw):
    waits = []
    client = make(http=http, sleep=waits.append, retry_routes=("youtube/videos/trending",), retry_wait=2, **kw)
    return client, waits


@pytest.mark.parametrize("failure,cls", [
    (ReadTimeout("read timed out"), "timeout"), (TimeoutError("timed out"), "timeout"),
    (ConnectionError_("reset"), "connection"), ((429, None), "http_429"), ((500, None), "http_5xx"),
    ((504, None), "http_5xx"), ((503, UNAVAILABLE), "http_5xx")])
def test_a_transient_board_failure_is_retried_and_the_success_is_ok(failure, cls):
    http = FakeHTTP({"youtube/videos/trending": [failure, board_ok()]})
    client, waits = retrying(http)
    r = client.call(*BOARD)
    assert r.status == "ok" and r.attempts == 2 and r.failure == ""
    assert r.reason == f"ok on attempt 2 after {cls}"
    assert http.routes().count("youtube/videos/trending") == 2 and waits == [2]
    rows = [x for x in paid(client) if x["route"] == "youtube/videos/trending"]
    assert len(rows) == 2 and all(x["calls"] == 1 for x in rows)
    assert r.credits_charged == sum(x["credits_charged"] for x in rows)


def test_retries_back_off_and_stop_after_the_bound():
    http = FakeHTTP({"youtube/videos/trending": [(500, None)] * 5})
    client, waits = retrying(http)
    r = client.call(*BOARD)
    assert r.status == "error" and r.attempts == 3 and r.failure == "http_5xx"
    assert waits == [2, 8]
    assert http.routes().count("youtube/videos/trending") == 3
    assert "(3 attempts: http_5xx, http_5xx, http_5xx)" in r.reason
    # Every attempt is ledgered as its own call and the result carries what they cost together.
    rows = [x for x in paid(client) if x["route"] == "youtube/videos/trending"]
    assert len(rows) == 3 and r.credits_charged == sum(x["credits_charged"] for x in rows)


@pytest.mark.parametrize("answer", [(404, None), (400, None), (402, SAMPLES["insufficient_credits"]),
                                    (200, {"success": False, "error": {"type": "BAD_REQUEST"}})])
def test_a_failure_that_is_not_transient_is_not_retried(answer):
    http = FakeHTTP({"youtube/videos/trending": [answer, board_ok()]})
    client, waits = retrying(http)
    r = client.call(*BOARD)
    assert r.status != "ok" and r.attempts == 1 and r.failure not in ("", "timeout", "http_5xx", "http_429")
    assert http.routes().count("youtube/videos/trending") == 1 and waits == []


def test_other_routes_keep_one_attempt_on_a_retrying_client():
    http = FakeHTTP({"tiktok/trending": [TimeoutError("read timed out"), ok("trending")]})
    client, waits = retrying(http)
    r = client.call(*TRENDING)
    assert r.status == "error" and r.failure == "timeout" and r.attempts == 1 and waits == []
    assert http.routes().count("tiktok/trending") == 1


def test_a_route_whose_retries_all_failed_gets_no_more_retries_in_the_run():
    http = FakeHTTP({"youtube/videos/trending": [(500, None)] * 3 + [(500, None), board_ok()]})
    client, waits = retrying(http)
    assert client.call(*BOARD).attempts == 3
    second = client.call("youtube/videos/trending", {"region": "NG"})
    assert second.status == "error" and second.attempts == 1 and waits == [2, 8]
    assert http.routes().count("youtube/videos/trending") == 4


def test_a_refund_on_an_exhausted_route_still_gets_the_one_free_retry():
    http = FakeHTTP({"youtube/videos/trending": [(500, None)] * 3 + [(503, UNAVAILABLE), board_ok()]})
    client, waits = retrying(http)
    client.call(*BOARD)
    r = client.call("youtube/videos/trending", {"region": "NG"})
    assert r.status == "ok" and r.attempts == 2 and waits == [2, 8, 2]


def test_retry_holds_stay_within_the_run_retry_budget():
    http = FakeHTTP({"youtube/videos/trending": [(429, None), board_ok()] * 2 + [(429, None), board_ok()]})
    client, waits = retrying(http, retry_credits=2)
    assert [client.call("youtube/videos/trending", {"region": m}).status for m in ("ZA", "NG", "KE")] == \
        ["ok", "ok", "error"]
    assert len(waits) == 2 and http.routes().count("youtube/videos/trending") == 5


def test_each_retry_passes_the_share_cap_check():
    ledger = MemoryLedgerStore()
    http = FakeHTTP({"youtube/videos/trending": [(429, None), board_ok()]})
    # Another caller on the same share spends the rest of the cap while this one waits.
    client = make(http=http, ledger=ledger, retry_routes=("youtube/videos/trending",),
                  sleep=lambda s: spent(ledger, "collect", COLLECT_SHARE))
    r = client.call(*BOARD)
    assert r.status == "cap_reached" and "collect share" in r.reason and r.attempts == 2
    assert http.routes().count("youtube/videos/trending") == 1


def test_a_retried_call_never_spends_past_the_share_cap():
    ledger = MemoryLedgerStore()
    spent(ledger, "collect", COLLECT_SHARE - 1)  # room for exactly one attempt at hold 1
    http = FakeHTTP({"youtube/videos/trending": [TimeoutError("read timed out"), board_ok()]})
    client = make(http=http, ledger=ledger, retry_routes=("youtube/videos/trending",))
    r = client.call(*BOARD)
    assert r.status == "cap_reached" and http.routes().count("youtube/videos/trending") == 1
    assert ledger.spent(TODAY, TODAY, job="collect") == COLLECT_SHARE


def test_the_transport_failure_class_keeps_no_message_or_key():
    http = FakeHTTP({"youtube/videos/trending": [ReadTimeout(f"timed out for key {SENTINEL}")] * 3})
    client, _ = retrying(http)
    r = client.call(*BOARD)
    assert r.failure == "timeout" and SENTINEL not in r.reason


def test_the_collect_job_retries_rank_lists_and_boards_only():
    from core.collect import job

    assert job.RETRY_ROUTES == {"tiktok/trending", "youtube/videos/trending", "youtube/shorts/trending",
                                "reddit/subreddit", "tiktok/hashtags/popular", "instagram/music/trending",
                                "apple_music/charts"}
    assert "web/scrape" not in job.RETRY_ROUTES and "prism/profiles" not in job.RETRY_ROUTES


def test_the_live_collect_client_is_built_with_the_board_retry_routes(monkeypatch):
    from core.collect import job

    class Store:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr("core.collect.stores.BigQueryLedgerStore", Store)
    monkeypatch.setattr("core.collect.stores.BigQueryRawStore", Store)
    client = job.live_client("run-1", load_caps(), object(), lambda: NOW)
    assert client.retry_routes == job.RETRY_ROUTES and client.share == "collect"


def test_a_youtube_board_outage_that_clears_within_the_retries_leaves_the_board_valid():
    # 2 Oct 2026: board_youtube failed on every category call in ZA, NG and KE, which held every youtube-led
    # item for three days. A failure that clears within the retries now gives a valid board row.
    from core.collect import job, writers

    def item_id(kind, raw, platform=None):
        return f"{kind}|{platform}:{raw}"

    def geo(platform, market, **kw):
        return None, None, None

    http = FakeHTTP({"youtube/videos/trending": [TimeoutError("read timed out"), (502, None)] +
                     [board_ok()] * len(job.YOUTUBE_CATEGORIES)})
    client, waits = retrying(http)
    run = job.Collected("run-1")
    runner = job._Runner(client, run, job._CountedIds(item_id), job.safe_geo(geo), lambda: NOW, job.Budget(),
                         None, TODAY)
    board = [c for c in job.harvest_calls("ZA", TODAY, job.load_config())[0] if c.route == "youtube/videos/trending"]
    runner.calls(board, "ZA")
    rows = [h for h in writers.health_rows(run.records, run.posts, {}, "run-1")
            if h["series"] == "board_youtube" and h["market"] == "ZA"]
    assert all((h["calls"], h["calls_ok"], h["units_planned"], h["units_ok"], h["valid"]) == (1, 1, 1, 1, True)
               for h in rows)
    assert waits == [2, 8]
    first = next(r for r in run.records if r["series"] == "board_youtube")
    assert first["ok"] and first["reason"] == "ok on attempt 3 after timeout, http_5xx"


def test_invalid_reason_names_the_failure_classes_of_the_failed_calls():
    from core.collect import writers

    def rec(ok, status, failure="", calls=1):
        return {"day": "2026-10-02", "market": "ZA", "series": "board_youtube", "protocol": "p", "platform": "youtube",
                "route": "youtube/videos/trending", "lane_class": "unbiased_rank", "ok": ok, "status": status,
                "failure": failure, "calls": calls, "units_planned": 1, "units_ok": int(ok), "items": 0,
                "post_ids": []}

    def reason(records):
        [row] = writers.health_rows(records, [], {}, "run-1")
        return row["invalid_reason"]

    assert reason([rec(False, "error", "timeout")] * 2 + [rec(False, "refunded", "http_5xx")] * 3) == \
        "calls: http_5xx,timeout"
    assert reason([rec(False, "error", "http_429"), rec(False, "error", "connection"), rec(False, "error", "http_4xx"),
                   rec(False, "error", "vendor_error")]) == "calls: connection,http_429,http_4xx"
    assert reason([rec(False, "refunded")]) == "calls: http_5xx"  # a record from before the failure class
    assert reason([rec(False, "http_404")]) == "calls: http_4xx"
    assert reason([rec(False, "something new")]) == "calls: other"
    assert reason([rec(False, "decode_error")]) == "calls: decode_error"
    assert reason([rec(False, "error", "not a class: https://x.example/?key=1")]) == "calls: error"
    assert reason([rec(True, "ok")] * 4 + [rec(False, "error", "timeout")]) is None  # 4 of 5 is valid
    assert reason([rec(True, "ok")] * 3 + [rec(False, "error", "timeout")] * 2) == "calls: timeout"


def test_a_failed_youtube_board_call_says_its_failure_class_in_health():
    from core.collect import job, writers

    def item_id(kind, raw, platform=None):
        return f"{kind}|{platform}:{raw}"

    http = FakeHTTP({"youtube/videos/trending": [(500, None)] * 3 + [board_ok()] * 5})
    client, _ = retrying(http)
    run = job.Collected("run-1")
    runner = job._Runner(client, run, job._CountedIds(item_id), job.safe_geo(lambda *a, **k: (None, None, None)),
                         lambda: NOW, job.Budget(), None, TODAY)
    board = [c for c in job.harvest_calls("ZA", TODAY, job.load_config())[0] if c.route == "youtube/videos/trending"]
    runner.calls(board, "ZA")
    rows = [h for h in writers.health_rows(run.records, run.posts, {}, "run-1") if h["series"] == "board_youtube"]
    failed = [h for h in rows if not h["valid"]]
    assert len(failed) == 1 and failed[0]["invalid_reason"] == "calls: http_5xx"
    assert all(h["valid"] for h in rows if h is not failed[0])
