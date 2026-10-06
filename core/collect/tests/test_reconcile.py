"""Unit tests for core/collect/reconcile.py, the nightly credit reconciliation and runway check.

No network: the real SocialCrawl client runs against a fake HTTP callable, memory stores stand in for
credit_ledger, raw_responses and runs, and a fake BigQuery client records the ledger SELECT.
"""

import copy
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from core.collect import chain
from core.collect import reconcile as rc
from core.collect.socialcrawl_client import SocialCrawlClient, load_caps
from core.collect.stores import MemoryLedgerStore, MemoryRawStore

KEY_ENV = "SOCIALCRAWL_OGILVY_API_KEY"
SENTINEL = "sc-test-sentinel-reconcile-9a2e"
NOW = datetime(2026, 9, 28, 21, 30, tzinfo=timezone.utc)  # 23:30 in Johannesburg
DAY = date(2026, 9, 27)
START = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)  # 00:00 SAST on DAY
FLOOR = 20000


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, SENTINEL)
    monkeypatch.delenv("RUN_DATE", raising=False)
    monkeypatch.delenv("FORCE_RERUN", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)


def at(hour, minute=0, second=0, day=DAY):
    """An instant given in SAST on day."""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=chain.SAST)


def txn(tid, when, amount=-1, route="tiktok/trending", kind="usage", request_id=None):
    platform, _, endpoint = route.partition("/")
    return {"id": tid, "amount": amount, "balance_after": 0,
            "created_at": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "credit_tier": "standard", "description": "", "endpoint": endpoint, "platform": platform,
            "request_id": request_id or f"req-{tid}", "type": kind, "vendor_labels": {}}


def page(items, cursor=None, has_more=None):
    has_more = cursor is not None if has_more is None else has_more
    return {"success": True, "cached": False, "credits_used": 0, "credits_remaining": 0,
            "data": {"items": items, "dropped": 0, "next_cursor": cursor},
            "pagination": {"has_more": has_more, "next_cursor": cursor, "page_size": len(items)},
            "platform": "credits", "endpoint": "transactions", "request_id": "r"}


class FakeHTTP:
    """credits/balance answers the balance; credits/transactions answers the page for the sent cursor."""

    def __init__(self, pages=None, balance=250000):
        self.pages = pages if pages is not None else {None: page([])}
        self.balance = balance
        self.requests = []

    def __call__(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        route = url.split("/v1/", 1)[1]
        self.requests.append({"method": method, "route": route, "params": copy.deepcopy(params)})
        if route == "credits/balance":
            return 200, {"success": True, "credits_used": 0,
                         "data": {"balance": self.balance, "recent_deductions": 0}}, {}
        if route == "credits/transactions":
            return 200, copy.deepcopy(self.pages[(params or {}).get("cursor")]), {}
        raise AssertionError(f"unexpected request to {route}")

    def transaction_requests(self):
        return [r for r in self.requests if r["route"] == "credits/transactions"]


class Ledger(MemoryLedgerStore):
    """credit_ledger in memory, with the reconcile day read."""

    def day_rows(self, day):
        return [dict(r) for r in self.rows if str(r["trend_date"]) == day.isoformat()]


def charge(ledger, when, credits, route="tiktok/trending", **extra):
    platform, _, endpoint = route.partition("/")
    ledger.append({"trend_date": when.astimezone(chain.SAST).date().isoformat(), "run_id": "collect-1",
                   "job": "collect", "route": route, "platform": platform, "endpoint": endpoint, "calls": 1,
                   "credits_charged": credits, "cache_hit": False,
                   "logged_at": when.astimezone(timezone.utc).isoformat(), **extra})


def client(http, ledger, raw=None):
    return SocialCrawlClient(share="reserve", run_id="reconcile-test", mode="live", ledger=ledger,
                             raw=raw if raw is not None else MemoryRawStore(), http=http, clock=lambda: NOW)


def go(ledger, http, runs=None, raw=None):
    runs = runs if runs is not None else chain.MemoryRunsStore()
    code = rc.run(DAY, runs=runs, ledger=ledger, make_client=lambda run_id: client(http, ledger, raw),
                  caps=load_caps())
    return code, runs


def final(runs):
    return runs.rows[-1]


# Vendor transaction shape ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform, endpoint, route", [
    ("tiktok", "trending", "tiktok/trending"),
    ("tiktok", "/v1/tiktok/trending", "tiktok/trending"),
    ("tiktok", "tiktok/trending", "tiktok/trending"),
    ("search", "multi", "search/multi"),
    ("youtube", "/videos/trending", "youtube/videos/trending"),
    ("", "/v1/prism/post-stats", "prism/post-stats"),
])
def test_vendor_route_is_normalised_to_the_ledger_route(platform, endpoint, route):
    assert rc.vendor_route({"platform": platform, "endpoint": endpoint}) == route


@pytest.mark.parametrize("amount, kind, spend", [
    (-5, "usage", 5),
    (5, "usage", 5),
    (-3, "deduction", 3),
    (4, "refund", -4),
    (-4, "refund", -4),
    (10000, "purchase", None),
    (500, "topup", None),
    ("x", "usage", None),
])
def test_vendor_spend_reads_either_sign_and_leaves_top_ups_out(amount, kind, spend):
    assert rc.vendor_spend({"amount": amount, "type": kind}) == spend


# Matching -------------------------------------------------------------------------------------------


def v(tid, when, credits=1, route="tiktok/trending", request_id=None):
    return {"id": tid, "request_id": request_id or f"req-{tid}", "route": route, "at": when, "credits": credits}


def lrow(when, credits=1, route="tiktok/trending", **extra):
    return {"route": route, "at": when, "credits": credits, "run_id": "collect-1", **extra}


def test_request_id_matches_first_whatever_the_route_and_time():
    ledger = [lrow(at(3), 5, route="tiktok/trending", request_id="req-a")]
    vendor = [v("a", at(9), 5, route="search/multi", request_id="req-a")]
    out = rc.match(ledger, vendor)
    assert out["by_request_id"] == 1 and out["matched"] == 1
    assert out["unmatched_ledger"] == [] and out["unmatched_vendor"] == []


def test_route_and_nearest_time_within_the_window_match_the_rest():
    ledger = [lrow(at(3, 0, 10)), lrow(at(3, 0, 40)), lrow(at(4), route="search/news")]
    vendor = [v("late", at(3, 0, 38)), v("early", at(3, 0, 5)), v("far", at(3, 30)),
              v("other", at(4), route="youtube/videos/trending")]
    out = rc.match(ledger, vendor)
    assert out["matched"] == 2 and out["by_request_id"] == 0
    assert [r["id"] for r in out["unmatched_vendor"]] == ["far", "other"]
    assert [r["route"] for r in out["unmatched_ledger"]] == ["search/news"]


def test_the_same_route_outside_the_window_does_not_match():
    out = rc.match([lrow(at(3))], [v("later", at(3) + rc.MATCH_WINDOW + timedelta(seconds=1))])
    assert out["matched"] == 0
    assert len(out["unmatched_ledger"]) == 1 and [r["id"] for r in out["unmatched_vendor"]] == ["later"]


def test_a_vendor_row_is_matched_at_most_once():
    ledger = [lrow(at(3)), lrow(at(3, 0, 1))]
    out = rc.match(ledger, [v("only", at(3))])
    assert out["matched"] == 1 and len(out["unmatched_ledger"]) == 1


def test_matched_pairs_whose_amounts_differ_are_listed_as_price_mismatches():
    out = rc.match([lrow(at(3), 1, route="linkedin/search/posts")], [v("a", at(3), 5, route="linkedin/search/posts")])
    assert out["matched"] == 1
    assert out["price_mismatches"] == [{"route": "linkedin/search/posts", "ledger": 1, "vendor": 5,
                                        "vendor_id": "a"}]


# Pagination -----------------------------------------------------------------------------------------


def three_pages():
    return {
        None: page([txn("t1", at(20, day=date(2026, 9, 28))), txn("t2", at(1, day=date(2026, 9, 28)))], "c2"),
        "c2": page([txn("t3", at(22)), txn("t4", at(5))], "c3"),
        "c3": page([txn("t5", at(0, 30)), txn("t6", at(23, day=date(2026, 9, 26)))], "c4"),
        "c4": page([txn("t7", at(12, day=date(2026, 9, 26)))], None),
    }


def test_pages_follow_the_cursor_until_the_day_is_covered_and_no_further():
    http, ledger = FakeHTTP(three_pages()), Ledger()
    got = rc.read_transactions(client(http, ledger), START)
    assert [r["params"].get("cursor") for r in http.transaction_requests()] == [None, "c2", "c3"]
    assert got["covered"] is True and got["pages"] == 3 and got["error"] == ""
    assert [t["id"] for t in got["items"]] == ["t1", "t2", "t3", "t4", "t5", "t6"]
    for r in http.transaction_requests():
        assert set(r["params"]) <= {"limit", "cursor"}
        assert r["params"]["limit"] == rc.PAGE_LIMIT


def test_the_end_of_the_history_covers_the_day():
    http = FakeHTTP({None: page([txn("t1", at(9))], None, has_more=False)})
    got = rc.read_transactions(client(http, Ledger()), START)
    assert got["covered"] is True and got["pages"] == 1


def test_a_page_cap_without_reaching_the_day_start_is_not_covered(monkeypatch):
    monkeypatch.setattr(rc, "MAX_PAGES", 2)
    http = FakeHTTP(three_pages())
    got = rc.read_transactions(client(http, Ledger()), START)
    assert got["covered"] is False and got["pages"] == 2
    assert len(http.transaction_requests()) == 2


def test_every_vendor_read_is_a_free_ledgered_call_on_the_reserve_share():
    http, ledger = FakeHTTP(three_pages()), Ledger()
    go(ledger, http)
    mine = [r for r in ledger.rows if r.get("run_id") != "collect-1"]
    assert mine and {r["route"] for r in mine} <= {"credits/balance", "credits/transactions"}
    assert {r["job"] for r in mine} == {"reserve"}
    assert all(r["credits_charged"] == 0 for r in mine)
    assert {r["trend_date"] for r in mine} == {"2026-09-28"}


# The nightly run ------------------------------------------------------------------------------------


def seeded(vendor_amounts, ledger_amounts):
    """A day with one ledger row and one vendor row per amount, paired by route and time."""
    ledger = Ledger()
    items = []
    for i, credits in enumerate(ledger_amounts):
        charge(ledger, at(3, i, 10), credits)
    for i, credits in enumerate(vendor_amounts):
        items.append(txn(f"t{i}", at(3, i), -credits))
    items.sort(key=lambda t: t["created_at"], reverse=True)
    items.append(txn("old", at(23, day=date(2026, 9, 26))))
    return ledger, FakeHTTP({None: page(items, None, has_more=False)})


def test_a_clean_day_reports_totals_and_logs_no_alert(caplog):
    ledger, http = seeded([5, 1, 6], [5, 1, 6])
    with caplog.at_level(logging.INFO, logger=rc.log.name):
        code, runs = go(ledger, http)
    counts = final(runs)["counts"]
    assert code == 0 and final(runs)["status"] == "ok"
    assert counts["ledger_total"] == 12 and counts["vendor_total"] == 12 and counts["difference"] == 0
    assert counts["matched"] == 3 and counts["unmatched_ledger"] == 0 and counts["unmatched_vendor"] == 0
    assert counts["vendor_covered"] is True and counts["alerts"] == []
    assert not [r for r in caplog.records if "42 ALERT" in r.getMessage()]


def test_unmatched_rows_on_both_sides_are_counted_with_their_credits():
    ledger, http = seeded([5, 1], [5, 1])
    charge(ledger, at(9), 3, route="search/news")
    http.pages[None]["data"]["items"].insert(0, txn("extra", at(15), -2, route="youtube/search/advanced"))
    _, runs = go(ledger, http)
    counts = final(runs)["counts"]
    assert counts["unmatched_ledger"] == 1 and counts["unmatched_ledger_credits"] == 3
    assert counts["unmatched_vendor"] == 1 and counts["unmatched_vendor_credits"] == 2
    assert counts["unmatched_ledger_sample"][0]["route"] == "search/news"
    assert counts["unmatched_vendor_sample"][0]["id"] == "extra"
    assert counts["difference"] == 1


@pytest.mark.parametrize("ledger_total, vendor_total, alert", [
    (50, 50, False),
    (51.5, 50, False),   # 2% of 50 is 1, so the tolerance is the 2 credit minimum
    (52, 50, False),     # at 2 credits, not over
    (52.5, 50, True),    # over 2 credits
    (47, 50, True),      # the sign does not matter
    (100, 100, False),
    (102, 100, False),   # 2 credits and 2% of 100: at the line, not over it
    (103, 100, True),    # over it
    (97, 100, True),
    (500, 500, False),
    (503, 500, False),   # over 2 credits but inside 2% of 500, which is 10
    (510, 500, False),   # at 10, not over
    (511, 500, True),    # over 2% of 500
    (489, 500, True),
])
def test_the_reconcile_alert_fires_over_the_larger_of_2_credits_and_2_percent(ledger_total, vendor_total, alert,
                                                                               caplog):
    ledger, http = seeded([vendor_total], [ledger_total])
    with caplog.at_level(logging.INFO, logger=rc.log.name):
        go(ledger, http)
    lines = [r for r in caplog.records if r.getMessage().startswith("42 ALERT reconcile")]
    assert len(lines) == (1 if alert else 0)
    for r in lines:
        assert r.levelno == logging.ERROR


def test_a_day_the_vendor_history_does_not_cover_alerts_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(rc, "MAX_PAGES", 1)
    ledger = Ledger()
    http = FakeHTTP(three_pages())
    with caplog.at_level(logging.INFO, logger=rc.log.name):
        _, runs = go(ledger, http)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("42 ALERT reconcile")]
    assert len(lines) == 1 and "not covered" in lines[0]
    assert final(runs)["counts"]["vendor_covered"] is False


# Runway ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("balance, spend_30d, runway", [
    (50000, 30000, 30.0),     # 30000 above the floor at 1000 a day
    (50000, 31000, 29.03),
    (20000, 30000, 0.0),      # at the floor
    (15000, 30000, 0.0),      # below it
    (50000, 0, None),         # nothing spent: no runway limit
])
def test_runway_is_days_of_trailing_mean_spend_above_the_floor(balance, spend_30d, runway):
    assert rc.runway(balance, FLOOR, spend_30d / 30) == runway


def month_of_spend(ledger, total, days=30):
    for i in range(days):
        day = DAY - timedelta(days=i)
        ledger.append({"trend_date": day.isoformat(), "run_id": "old", "job": "collect", "route": "tiktok/trending",
                       "credits_charged": total / days, "logged_at": at(3, day=day).isoformat()})


@pytest.mark.parametrize("balance, spend_30d, alert", [(50000, 30000, False), (50000, 31000, True)])
def test_the_credits_low_alert_fires_under_30_days_of_runway(balance, spend_30d, alert, caplog):
    ledger = Ledger()
    month_of_spend(ledger, spend_30d)
    ledger.append({"trend_date": (DAY - timedelta(days=30)).isoformat(), "job": "collect", "route": "x/y",
                   "credits_charged": 99999, "logged_at": at(3, day=DAY - timedelta(days=30)).isoformat()})
    http = FakeHTTP({None: page([], None, has_more=False)}, balance=balance)
    with caplog.at_level(logging.INFO, logger=rc.log.name):
        _, runs = go(ledger, http)
    counts = final(runs)["counts"]
    assert counts["balance"] == balance and counts["floor"] == FLOOR
    assert counts["spend_30d"] == pytest.approx(spend_30d)
    lines = [r for r in caplog.records if r.getMessage().startswith("42 ALERT credits_low")]
    assert len(lines) == (1 if alert else 0)
    assert all(r.levelno == logging.ERROR for r in lines)


def test_below_the_floor_the_free_reads_still_run_and_both_alerts_fire(caplog):
    ledger = Ledger()
    month_of_spend(ledger, 3000)
    http = FakeHTTP(three_pages(), balance=19000)
    with caplog.at_level(logging.INFO, logger=rc.log.name):
        code, runs = go(ledger, http)
    counts = final(runs)["counts"]
    assert counts["balance"] == 19000 and counts["balance_error"] == ""
    assert counts["runway_days"] == 0 and counts["vendor_covered"] is True
    assert [r["route"] for r in http.requests] == ["credits/balance"] + ["credits/transactions"] * 3
    messages = [r.getMessage() for r in caplog.records]
    assert sum(m.startswith("42 ALERT credits_low: ") for m in messages) == 1
    assert sum(m.startswith("42 ALERT reconcile: ") for m in messages) == 1
    assert final(runs)["status"] == "ok" and code == 0


def test_both_alert_lines_use_the_shared_name_colon_prefix(caplog):
    ledger = Ledger()
    month_of_spend(ledger, 3000)
    http = FakeHTTP(three_pages(), balance=19000)
    with caplog.at_level(logging.INFO, logger=rc.log.name):
        go(ledger, http)
    alerts = sorted(r.getMessage() for r in caplog.records if "42 ALERT" in r.getMessage())
    assert len(alerts) == 2
    assert alerts[0].startswith(f"42 ALERT credits_low: {DAY.isoformat()} balance 19000, floor {FLOOR}, ")
    assert alerts[1].startswith(f"42 ALERT reconcile: {DAY.isoformat()} ledger 100 against vendor 3, ")


# Runs row, writes and secrets -----------------------------------------------------------------------


def test_one_running_row_then_one_final_row_on_stage_reconcile_and_no_next_job(monkeypatch):
    monkeypatch.setattr(chain, "start_next", lambda *a, **k: pytest.fail("reconcile starts no next job"))
    ledger, http = seeded([5], [5])
    _, runs = go(ledger, http)
    assert [r["status"] for r in runs.rows] == ["running", "ok"]
    assert {r["stage"] for r in runs.rows} == {"reconcile"}
    assert {r["run_date"] for r in runs.rows} == {DAY.isoformat()}
    assert runs.rows[0]["run_id"] == runs.rows[1]["run_id"]
    counts = final(runs)["counts"]
    json.dumps(counts)
    for key in ("ledger_total", "vendor_total", "difference", "unmatched_ledger", "unmatched_vendor", "balance",
                "floor", "mean_daily_spend", "runway_days", "vendor_pages", "alerts"):
        assert key in counts


def test_the_run_goes_through_chain_begin_and_finish(monkeypatch):
    seen = []
    begin, finish = chain.begin, chain.finish

    def spy_begin(stage, day, **kw):
        seen.append(("begin", stage, day))
        return begin(stage, day, **kw)

    def spy_finish(run, status, *a, **kw):
        seen.append(("finish", run.stage, status))
        return finish(run, status, *a, **kw)

    monkeypatch.setattr(chain, "begin", spy_begin)
    monkeypatch.setattr(chain, "finish", spy_finish)
    _, runs = go(*seeded([5], [5]))
    assert seen == [("begin", "reconcile", DAY), ("finish", "reconcile", "ok")]
    assert [r["status"] for r in runs.rows] == ["running", "ok"]


@pytest.mark.parametrize("first", ["ok", "running"])
def test_a_scheduler_retry_exits_0_as_already_done_and_reads_nothing(first, capsys):
    runs = chain.MemoryRunsStore()
    now = datetime.now(timezone.utc).isoformat()
    runs.append({"run_id": "reconcile-first", "stage": "reconcile", "run_date": DAY.isoformat(), "status": first,
                 "started_at": now, "finished_at": None if first == "running" else now, "counts": None,
                 "error": None})
    http = FakeHTTP()
    ledger = Ledger()
    code, runs = go(ledger, http, runs=runs)
    assert code == 0
    assert [r["status"] for r in runs.rows] == [first, "skipped_duplicate"]
    assert runs.rows[-1]["stage"] == "reconcile"
    assert http.requests == [] and ledger.rows == []
    assert "reconcile-first" in capsys.readouterr().out


def test_a_running_row_older_than_15_minutes_is_a_dead_run_and_reconcile_runs_again():
    runs = chain.MemoryRunsStore()
    then = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    runs.append({"run_id": "reconcile-dead", "stage": "reconcile", "run_date": DAY.isoformat(), "status": "running",
                 "started_at": then, "finished_at": None, "counts": None, "error": None})
    code, runs = go(*seeded([5], [5]), runs=runs)
    assert code == 0 and [r["status"] for r in runs.rows] == ["running", "running", "ok"]


def test_a_crash_finishes_the_run_failed_with_the_error():
    class Broken(Ledger):
        def day_rows(self, day):
            raise RuntimeError("ledger read failed")

    _, runs = go(Broken(), FakeHTTP())
    assert final(runs)["status"] == "failed" and "ledger read failed" in final(runs)["error"]


def test_no_secret_reaches_logs_stdout_or_any_row(capsys, caplog):
    ledger, http = seeded([5, 9], [5, 1])
    raw = MemoryRawStore()
    with caplog.at_level(logging.DEBUG):
        _, runs = go(ledger, http, raw=raw)
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text + json.dumps(runs.rows, default=str) + \
        json.dumps(ledger.rows, default=str) + json.dumps(raw.rows, default=str)
    assert SENTINEL not in text


def test_alert_log_lines_are_json_with_severity_and_the_message_first():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "42 ALERT reconcile: %s over", ("2026-09-27",), None)
    line = json.loads(rc.JsonFormatter().format(record))
    assert line == {"severity": "ERROR", "message": "42 ALERT reconcile: 2026-09-27 over"}


# Ledger read, run date and plan ---------------------------------------------------------------------


class FakeBQ:
    def __init__(self, rows=()):
        self.queries = []
        self.rows = list(rows)

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        rows = self.rows

        class Job:
            def result(self):
                return rows

        return Job()


class Row(dict):
    pass


def test_the_ledger_day_read_is_one_parameterised_select():
    bq = FakeBQ([Row(route="tiktok/trending", credits_charged=5.0)])
    rows = rc.LedgerDay(bq, chain.PROJECT).day_rows(DAY)
    assert rows == [{"route": "tiktok/trending", "credits_charged": 5.0}]
    sql, config = bq.queries[0]
    assert sql.lstrip().upper().startswith("SELECT")
    assert "`ogilvy-trends-v2.intelligence_42_core.credit_ledger`" in sql
    assert "@day" in sql and DAY.isoformat() not in sql
    for word in ("DELETE", "MERGE", "UPDATE", "INSERT", "TRUNCATE", "DROP", "REPLACE"):
        assert word not in sql.upper()
    params = {p.name: (p.type_, p.value) for p in config.query_parameters}
    assert params == {"day": ("DATE", DAY)}


def test_the_run_date_is_yesterday_in_sast_unless_run_date_is_set(monkeypatch):
    late = datetime(2026, 9, 28, 22, 30, tzinfo=timezone.utc)  # 00:30 SAST on 29 September
    assert rc.run_day({}, late) == date(2026, 9, 28)
    assert rc.run_day({}, NOW) == date(2026, 9, 27)
    assert rc.run_day({"RUN_DATE": "2026-09-20"}, NOW) == date(2026, 9, 20)


def test_plan_prints_what_it_reads_and_compares_and_touches_nothing(capsys, monkeypatch):
    monkeypatch.setenv("RUN_DATE", DAY.isoformat())

    def boom(*a, **k):
        raise AssertionError("no network in --plan")

    monkeypatch.setattr(rc, "LedgerDay", boom)
    monkeypatch.setattr(chain, "BigQueryRunsStore", boom)
    assert rc.main(["--plan"], http=boom) == 0
    out = capsys.readouterr().out
    assert DAY.isoformat() in out
    assert "intelligence_42_core.credit_ledger" in out and "@day" in out
    assert "credits/balance" in out and "credits/transactions" in out and "cursor" in out
    assert "2026-09-26T22:00:00+00:00" in out and "2026-09-27T22:00:00+00:00" in out
    assert "the reconcile alert" in out and "the credits_low alert" in out
    assert "42 ALERT" not in out  # a --plan inside Cloud Run must never match a log-based alert policy
    assert "larger of 2 credits and 2%" in out
    assert str(FLOOR) in out and "reserve" in out
    assert SENTINEL not in out
