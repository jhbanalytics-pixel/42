"""A board or panel call answered with an HTTP 5xx is tried once more after 60 seconds, inside the run's credit
cap, and the receipt records it (W8-DEC-19). No network: HTTP is a fake and the wait is injected."""

import json
from datetime import date, datetime, timezone

import pytest

from core.collect import job, local_sources as ls, writers
from core.collect.socialcrawl_client import SERVER_RETRY_WAIT, SocialCrawlClient, quote_for
from core.collect.stores import MemoryLedgerStore, MemoryRawStore
from core.collect.tests.test_socialcrawl_client import (
    BOARD, COLLECT_SHARE, FailingRaw, FakeHTTP, ReadTimeout, SAMPLES, UNAVAILABLE, board_ok, make, paid, spent)

KEY_ENV = "SOCIALCRAWL_OGILVY_API_KEY"
ROUTE = BOARD[0]
PANEL = ("prism/profiles", {"items": [{"platform": "instagram", "handle": "zalebs"}], "include": "posts",
                           "since": "2026-09-28"})
ROOM = 100


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sc-test-sentinel-4f1d")


def retrying(answers, route=ROUTE, **kw):
    waits = []
    http = FakeHTTP({route: answers})
    client = make(http=http, sleep=waits.append, retry_wait=2, **kw)
    return client, http, waits


def board(client, room=ROOM, **kw):
    return client.call(*BOARD, market="ZA", server_retry_room=room, **kw)


# The rule ---------------------------------------------------------------------


def test_the_wait_is_sixty_seconds_and_is_a_client_setting():
    assert SERVER_RETRY_WAIT == 60
    assert make().server_retry_wait == 60
    assert make(server_retry_wait=7).server_retry_wait == 7


@pytest.mark.parametrize("status_code", [502, 503])
def test_a_board_refused_502_or_503_is_tried_free_at_two_seconds_and_then_once_after_sixty(status_code):
    client, http, waits = retrying([(status_code, UNAVAILABLE), (status_code, UNAVAILABLE), board_ok()])
    r = board(client)
    assert r.status == "ok" and r.attempts == 3 and r.first_http_status == status_code
    assert waits == [2, 60] and r.server_retry == "made"


@pytest.mark.parametrize("status_code", [500, 504, 599])
def test_a_board_answered_5xx_is_tried_once_more_after_sixty_seconds(status_code):
    client, http, waits = retrying([(status_code, None), board_ok()])
    r = board(client)
    assert r.status == "ok" and r.attempts == 2 and r.http_status == 200
    assert waits[-1] == 60 and waits.count(60) == 1
    assert http.routes().count(ROUTE) == 2
    assert r.first_http_status == status_code and r.server_retry == "made"


def test_a_panel_answered_5xx_is_tried_once_more_after_sixty_seconds():
    answers = [(500, None), (200, json.loads(open_fixture("local_profiles.json")))]
    client, http, waits = retrying(answers, route=PANEL[0])
    r = client.call(*PANEL, market="ZA", server_retry_room=ROOM)
    assert r.status in ("ok", "empty") and r.attempts == 2 and r.server_retry == "made"
    assert waits.count(60) == 1 and http.routes().count(PANEL[0]) == 2


def open_fixture(name):
    from pathlib import Path

    return (Path(__file__).resolve().parent / "fixtures" / name).read_text(encoding="utf-8")


def test_the_sixty_second_retry_is_made_exactly_once():
    client, http, waits = retrying([(500, None)] * 6)
    r = board(client)
    assert r.status == "error" and r.failure == "http_5xx" and r.server_retry == "made"
    assert waits == [60] and http.routes().count(ROUTE) == 2 and r.attempts == 2


def test_a_refunded_panel_gets_the_existing_free_retry_and_then_the_one_sixty_second_retry():
    client, http, waits = retrying([(503, UNAVAILABLE)] * 6, route=PANEL[0])
    r = client.call(*PANEL, market="ZA", server_retry_room=ROOM)
    assert r.status == "refunded" and r.attempts == 3 and r.first_http_status == 503
    assert waits == [2, 60] and http.routes().count(PANEL[0]) == 3


def test_without_a_room_the_call_is_as_it_was():
    client, http, waits = retrying([(500, None), board_ok()])
    r = client.call(*BOARD, market="ZA")
    assert r.status == "error" and r.attempts == 1 and r.server_retry == "" and r.first_http_status is None
    assert waits == [] and http.routes().count(ROUTE) == 1


def test_a_call_that_succeeds_first_time_has_no_retry_on_it():
    client, http, waits = retrying([board_ok()])
    r = board(client)
    assert r.status == "ok" and r.attempts == 1 and r.server_retry == "" and r.first_http_status is None
    assert waits == []


@pytest.mark.parametrize("answer", [(400, None), (401, None), (403, None), (404, None), (422, None),
                                    (429, None), (495, None), (495, {"success": False, "error": {"type": "QUOTA"}}),
                                    (402, SAMPLES["insufficient_credits"])])
def test_no_retry_on_a_4xx_answer_and_none_on_495(answer):
    client, http, waits = retrying([answer, board_ok()])
    r = board(client)
    assert r.status != "ok" and r.attempts == 1 and r.server_retry == ""
    assert waits == [] and http.routes().count(ROUTE) == 1


def test_a_5xx_that_reports_insufficient_credits_halts_and_is_not_retried():
    client, http, waits = retrying([(503, SAMPLES["insufficient_credits"]), board_ok()])
    r = board(client)
    assert r.status == "insufficient_credits" and r.attempts == 1 and waits == []
    assert http.routes().count(ROUTE) == 1


def test_a_5xx_whose_raw_response_could_not_be_stored_is_not_retried():
    waits = []
    http = FakeHTTP({ROUTE: [(500, None), board_ok()]})
    client = make(http=http, raw=FailingRaw(), sleep=waits.append)
    r = board(client)
    assert r.status == "error" and r.failure == "store" and r.attempts == 1 and r.server_retry == ""
    assert waits == [] and http.routes().count(ROUTE) == 1


def test_a_200_that_reports_failure_is_not_retried():
    client, http, waits = retrying([(200, {"success": False, "error": {"type": "BAD_REQUEST"}}), board_ok()])
    r = board(client)
    assert r.status == "error" and r.failure == "vendor_error" and waits == [] and r.attempts == 1


@pytest.mark.parametrize("failure", [ReadTimeout("read timed out"), TimeoutError("timed out")])
def test_a_timeout_gets_no_sixty_second_retry_on_a_client_without_retry_routes(failure):
    client, http, waits = retrying([failure, board_ok()])
    r = board(client)
    assert r.status == "error" and r.failure == "timeout" and r.attempts == 1 and r.server_retry == ""
    assert waits == [] and http.routes().count(ROUTE) == 1


def test_a_timeout_is_retried_only_by_the_existing_quick_retry_and_never_after_sixty_seconds():
    client, http, waits = retrying([ReadTimeout("t"), ReadTimeout("t"), ReadTimeout("t"), board_ok()],
                                   retry_routes=(ROUTE,))
    r = board(client)
    assert r.status == "error" and r.failure == "timeout" and r.attempts == 3 and r.server_retry == ""
    assert waits == [2, 8] and 60 not in waits and http.routes().count(ROUTE) == 3


def test_a_5xx_after_the_quick_retries_still_gets_the_sixty_second_retry():
    client, http, waits = retrying([(500, None)] * 3 + [board_ok()], retry_routes=(ROUTE,))
    r = board(client)
    assert r.status == "ok" and r.attempts == 4 and r.first_http_status == 500
    assert waits == [2, 8, 60] and r.server_retry == "made"
    assert r.reason == "ok on attempt 4 after http_5xx, http_5xx, http_5xx"


def test_a_5xx_after_a_timeout_on_the_quick_retries_is_retried_once_after_sixty_seconds():
    client, http, waits = retrying([ReadTimeout("t"), (500, None), (500, None), board_ok()], retry_routes=(ROUTE,))
    r = board(client)
    assert r.status == "ok" and waits == [2, 8, 60] and r.attempts == 4 and r.first_http_status is None


# The credit cap -------------------------------------------------------------


def test_a_retry_that_would_cross_the_room_is_not_made_and_nothing_is_waited():
    client, http, waits = retrying([(500, None), board_ok()])
    first = quote_for(*[BOARD[0], "GET", BOARD[1]])
    r = board(client, room=first)  # the first attempt took all the room
    assert r.status == "error" and r.http_status == 500 and r.attempts == 1
    assert r.server_retry == "blocked: room" and waits == [] and http.routes().count(ROUTE) == 1


def test_a_retry_that_lands_exactly_on_the_room_is_made():
    client, http, waits = retrying([(500, None), board_ok()])
    hold = quote_for(BOARD[0], "GET", BOARD[1])
    r = board(client, room=2 * hold)
    assert r.status == "ok" and r.server_retry == "made" and waits == [60]


def test_the_room_counts_everything_the_call_has_already_been_charged():
    client, http, waits = retrying([(500, None)] * 3 + [board_ok()], retry_routes=(ROUTE,))
    hold = quote_for(BOARD[0], "GET", BOARD[1])
    r = board(client, room=3 * hold)  # three attempts used it all
    assert r.attempts == 3 and r.server_retry == "blocked: room" and waits == [2, 8]
    assert http.routes().count(ROUTE) == 3


def test_a_retry_over_the_share_cap_is_not_made_and_the_caller_keeps_the_5xx():
    ledger = MemoryLedgerStore()
    spent(ledger, "collect", COLLECT_SHARE - 1)  # room for the first attempt only
    client, http, waits = retrying([(500, None), board_ok()], ledger=ledger)
    r = board(client)
    assert r.status == "error" and r.http_status == 500 and r.attempts == 1
    assert r.server_retry == "blocked: cap" and waits == [] and http.routes().count(ROUTE) == 1
    assert ledger.spent(date(2026, 9, 28), date(2026, 9, 28), job="collect") == COLLECT_SHARE


def test_a_cap_crossed_during_the_wait_stops_the_retry_and_keeps_the_5xx_not_a_cap_status():
    ledger = MemoryLedgerStore()
    waits = []
    http = FakeHTTP({ROUTE: [(500, None), board_ok()]})
    client = make(http=http, ledger=ledger, sleep=lambda s: (waits.append(s), spent(ledger, "collect", COLLECT_SHARE)))
    r = board(client)
    assert r.status == "error" and r.http_status == 500 and r.credits_charged > 0
    assert waits == [60] and http.routes().count(ROUTE) == 1
    assert r.server_retry == "blocked: cap"


def test_a_balance_that_falls_below_the_floor_during_the_wait_stops_the_retry():
    http = FakeHTTP({ROUTE: [(500, None), board_ok()]}, balance=20001)
    client = make(http=http, sleep=lambda s: setattr(client, "_balance", 19999))
    r = board(client)
    assert r.status == "error" and r.http_status == 500 and http.routes().count(ROUTE) == 1
    assert r.server_retry == "blocked: balance"


def test_a_balance_below_the_floor_before_the_wait_stops_the_retry_without_waiting():
    http = FakeHTTP({ROUTE: [(500, None), board_ok()]}, balance=20000)
    waits = []
    client = make(http=http, sleep=waits.append)
    r = board(client)  # the first attempt's charge takes the balance under the floor
    assert r.server_retry == "blocked: balance" and waits == [] and http.routes().count(ROUTE) == 1


# Rows and receipt ------------------------------------------------------------


def test_both_attempts_are_ledgered_and_stored_and_the_caller_sees_their_sum():
    client, http, waits = retrying([(500, None), board_ok()])
    r = board(client)
    rows = [x for x in paid(client) if x["route"] == ROUTE]
    assert len(rows) == 2 and all(x["calls"] == 1 and x["market"] == "ZA" for x in rows)
    assert r.credits_charged == sum(x["credits_charged"] for x in rows) == 2 * quote_for(ROUTE, "GET", BOARD[1])
    assert [x["http_status"] for x in client.raw.rows] == [500, 200]


def test_the_retry_wait_for_a_refund_is_not_changed():
    client, http, waits = retrying([(503, UNAVAILABLE), board_ok()])
    r = client.call(*BOARD, market="ZA")
    assert r.status == "ok" and waits == [2] and r.server_retry == ""


def test_a_cache_hit_makes_no_retry():
    client, http, waits = retrying([board_ok(), (500, None)])
    assert board(client).status == "ok"
    again = board(client)
    assert again.status == "cached" and waits == [] and again.server_retry == ""


# The job passes the room to boards and panels only -----------------------------

BOARD_ROUTES = {"youtube/videos/trending", "youtube/shorts/trending", "tiktok/hashtags/popular",
                "instagram/music/trending", "apple_music/charts"}
PANEL_ROUTES = {"facebook/profile/posts", "prism/profiles", "twitter/user/tweets"}


def test_the_job_names_the_board_and_panel_routes():
    assert job.SLOW_RETRY_ROUTES == BOARD_ROUTES | PANEL_ROUTES


def test_a_call_is_slow_retried_when_it_is_a_board_or_a_panel_and_not_a_seeded_search():
    from types import SimpleNamespace

    c = job.Call("8", "facebook/profile/posts", {"pageId": "p", "since": "2026-09-28"}, "ZA", "panel")
    assert c.slow_retry
    assert job.Call("3", ROUTE, {"region": "ZA"}, "ZA", "sweep", pull_seq=1).slow_retry
    assert not job.Call("1", "tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", "sweep", pull_seq=1).slow_retry
    seeded = job.Call("14", "twitter/user/tweets", {"handle": "h", "since": "2026-09-28"}, "ZA", "expansion",
                      seed=SimpleNamespace(), seed_key="s")
    assert not seeded.slow_retry


def run_with_room_log(**kw):
    from core.collect.tests.test_job import FakeClient, TUESDAY, collect

    client = FakeClient()
    run = collect(client, TUESDAY, **kw)
    return client, run


def test_the_job_passes_a_room_to_every_board_and_panel_call_and_to_no_other():
    client, run = run_with_room_log()
    rooms = {}
    for c in client.calls:
        rooms.setdefault(c["route"], set()).add(c.get("room") is not None)
    assert {r for r, v in rooms.items() if v == {True}} == BOARD_ROUTES | PANEL_ROUTES
    assert not any(True in v for r, v in rooms.items() if r not in BOARD_ROUTES | PANEL_ROUTES), rooms
    assert BOARD_ROUTES | PANEL_ROUTES <= set(rooms)


def test_a_seeded_call_on_a_panel_route_gets_no_room():
    client, run = run_with_room_log()
    seeded = [c for c in client.calls if c["route"] in PANEL_ROUTES and c["seed_key"] and c["lane"] not in ("panel",)]
    assert all(c.get("room") is None for c in seeded)


def test_the_room_is_the_run_cap_less_what_the_run_has_charged():
    client, run = run_with_room_log()
    cap = job.load_caps()["ENGINE_DAILY"]["collect"]
    before, seen = 0, 0
    for c, charge in zip(client.calls, client.charged):
        if c.get("room") is not None:
            seen += 1
            assert c["room"] == cap - before
        before += charge["credits"]
    assert seen and len(client.calls) == len(client.charged)


def test_under_a_cap_override_the_room_is_the_calls_share_left():
    from core.collect.tests.test_job import FakeClient, TUESDAY, collect

    client = FakeClient()
    run = collect(client, TUESDAY, share_cap=150)
    limits = job.shares(150)
    first, second = [c for c in client.calls if c.get("room") is not None][:2]
    assert (first["route"], second["route"]) == ("youtube/shorts/trending", "instagram/music/trending")
    assert first["room"] == limits[job.GLOBAL]  # nothing spent in the GLOBAL share yet
    assert second["room"] == limits[job.GLOBAL] - client.charged[0]["credits"]
    market_rooms = [c["room"] for c in client.calls if c.get("room") is not None and c["market"] != job.GLOBAL]
    assert market_rooms and max(market_rooms) <= max(limits[m] for m in job.MARKETS)
    assert max(market_rooms) < 150  # the share binds, not the run's cap


def test_the_receipt_carries_the_attempts_and_the_first_status_of_a_retried_call():
    from core.collect.socialcrawl_client import Result
    from core.collect.tests.test_job import FakeClient, TUESDAY, collect

    class Retrying(FakeClient):
        def call(self, route, params=None, **kw):
            result = super().call(route, params, **kw)
            if route == "instagram/music/trending" and result.status == "ok":
                result.attempts, result.first_http_status, result.server_retry = 2, 503, "made"
                result.credits_charged = 4
            return result

    run = collect(Retrying(), TUESDAY)
    [rec] = [r for r in run.records if r["route"] == "instagram/music/trending"]
    assert (rec["attempts"], rec["first_status"], rec["server_retry"]) == (2, 503, "made")
    assert rec["ok"] is True and rec["status"] == "ok"
    receipt = run.counts()["server_retries"]
    assert receipt == [{"route": "instagram/music/trending", "market": job.GLOBAL, "first_status": 503,
                        "attempts": 2, "server_retry": "made", "status": "ok", "credits": 4}]
    plain = [r for r in run.records if r["route"] == "youtube/shorts/trending"]
    assert plain and all("attempts" not in r and "server_retry" not in r for r in plain)


def test_a_blocked_retry_is_on_the_receipt_with_the_5xx_the_caller_kept():
    from core.collect.socialcrawl_client import Result
    from core.collect.tests.test_job import FakeClient, TUESDAY, collect

    class Blocked(FakeClient):
        def call(self, route, params=None, **kw):
            result = super().call(route, params, **kw)
            if route == "instagram/music/trending":
                return Result("error", route, "h", 500, 1, 1, reason="HTTP 500", failure="http_5xx",
                              first_http_status=500, server_retry="blocked: room")
            return result

    run = collect(Blocked(), TUESDAY)
    [entry] = run.counts()["server_retries"]
    assert entry["server_retry"] == "blocked: room" and entry["status"] == "error" and entry["first_status"] == 500
    [h] = [x for x in writers.health_rows(run.records, run.posts, {}, "run-1")
           if x["route"] == "instagram/music/trending"]
    assert h["calls_ok"] == 0


def test_a_retry_that_clears_the_5xx_leaves_the_board_valid_in_health():
    from core.collect.tests.test_job import FakeClient, TUESDAY, collect

    class Recovers(FakeClient):
        def call(self, route, params=None, **kw):
            result = super().call(route, params, **kw)
            if route == "youtube/videos/trending":
                result.attempts, result.first_http_status, result.server_retry = 2, 500, "made"
            return result

    run = collect(Recovers(), TUESDAY)
    rows = [h for h in writers.health_rows(run.records, run.posts, {}, "run-1") if h["series"] == "board_youtube"]
    assert rows and all(h["calls"] == h["calls_ok"] and h["valid"] for h in rows)


# The local sources phase ------------------------------------------------------


def test_the_local_phase_passes_a_room_to_its_boards_and_its_panel_only_inside_the_plan():
    from core.collect.tests.test_local_sources import FakeClient, MONDAY, run

    client = FakeClient()
    run(client)
    fetches = [f for f in ls.plan(MONDAY) if f.paid and f.priced()]
    assert [c["route"] for c in client.calls] == [f.route for f in fetches]
    for i, (c, f) in enumerate(zip(client.calls, fetches)):
        later = sum(g.hold() for g in fetches[i + 1:])
        before = sum(g.hold() for g in fetches[:i])
        assert c["room"] == ls.LOCAL_DAILY_CAP - before - later, (i, f.source)


def test_the_local_phase_records_the_attempts_of_a_retried_board():
    from core.collect.socialcrawl_client import Result
    from core.collect.tests.test_local_sources import FakeClient, run

    class Retrying(FakeClient):
        def call(self, route, params=None, **kw):
            result = super().call(route, params, **kw)
            if route == "web/scrape" and params["url"].startswith("https://www.shazam.com/charts/top-200/nigeria"):
                result.attempts, result.first_http_status, result.server_retry = 2, 502, "made"
            return result

    out = run(Retrying())
    [rec] = [r for r in out["records"] if r.get("server_retry")]
    assert (rec["attempts"], rec["first_status"], rec["server_retry"], rec["market"]) == (2, 502, "made", "NG")
    assert all("attempts" not in r for r in out["records"] if r is not rec)


def test_the_reserve_for_the_local_phase_stays_out_of_the_room():
    from core.collect.tests.test_job import FakeClient, TUESDAY, collect, no_page

    client = FakeClient()
    collect(client, TUESDAY, local_get=no_page)
    cap = job.load_caps()["ENGINE_DAILY"]["collect"]
    hold = ls.total_hold(ls.plan(TUESDAY))
    first = next(c for c in client.calls if c.get("room") is not None)
    assert hold > 0 and first["room"] == cap - hold


def test_a_call_made_twice_by_the_quick_retry_alone_is_not_on_the_server_retry_receipt():
    from core.collect.tests.test_job import FakeClient, TUESDAY, collect

    class Quick(FakeClient):
        def call(self, route, params=None, **kw):
            result = super().call(route, params, **kw)
            if route == "youtube/shorts/trending":
                result.attempts, result.first_http_status = 2, 429
            return result

    run = collect(Quick(), TUESDAY)
    [rec] = [r for r in run.records if r["route"] == "youtube/shorts/trending"]
    assert rec["attempts"] == 2 and rec["first_status"] == 429 and rec["server_retry"] == ""
    assert run.counts()["server_retries"] == []
