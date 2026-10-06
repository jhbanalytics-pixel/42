"""Tests for core/collect/pulse.py, the collect side of the intraday pulse (FEATURES.md row 30a).

No network: the cap tests drive the real SocialCrawlClient with in-memory stores and a fake HTTP
callable; the other tests use a fake client. The socket is shut for the --plan test.
"""

import copy
import json
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.collect import pulse, writers
from core.collect.parse import COUNTER_COLUMNS, OBSERVATION_COLUMNS
from core.collect.socialcrawl_client import Result, SocialCrawlClient, load_caps
from core.collect.stores import MemoryLedgerStore, MemoryRawStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BODIES = json.loads((FIXTURES / "pulse_responses.json").read_text(encoding="utf-8"))
DAY = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # 14:00 SAST, 13:00 WAT, 15:00 EAT
KEY_ENV = "SOCIALCRAWL_OGILVY_API_KEY"
KINDS = {"topic", "hashtag", "sound", "format", "meme", "creator", "brand", "event"}
FEED_ZA = ("ZA", "feed_tiktok", "tiktok/trending?feed=local&region=ZA")
COHORT = "".join(["te", "en"])  # a rule 1 word, joined at run time like the other collect tests


def fake_item_id(kind, raw, platform=None):
    """Lane L2's rules as the other collect tests fake them: sound and creator keys carry the platform."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    key = " ".join(str(raw or "").strip().lstrip("#@").casefold().split())
    if not key:
        raise ValueError("empty key")
    if kind in ("sound", "creator"):
        if not platform:
            raise ValueError(f"{kind} needs a platform")
        key = f"{platform}:{key}"
    return f"{kind}|{key}"


def fake_geo(platform, market, *, ext_region=None, home_market=None, profile_location=None, text=None,
             language=None):
    return None, None, None


def state(market, key, *, kind="hashtag", worth=0.9, st="rising", eligible=True, day=DAY, label=None,
          canonical=None):
    canonical = key if canonical is None else canonical
    item = fake_item_id(kind, key, "tiktok")
    return {"metric_date": day, "market": market, "item_id": item, "kind": kind, "state": st, "eligible": eligible,
            "worth_pct": worth, "label": label or key, "canonical_key": canonical}


def caps(cap):
    out = copy.deepcopy(load_caps())
    out.pop(pulse.CAP_KEY, None)
    if cap is not None:
        out[pulse.CAP_KEY] = cap
    return out


class FakeClient:
    """Answers every route from the fixtures at its hold; records each call."""

    def __init__(self, statuses=None, share=pulse.PULSE_SHARE, spent=0):
        self.share, self.calls, self.statuses = share, [], dict(statuses or {})
        self.ledger = MemoryLedgerStore()
        if spent:
            self.ledger.append({"trend_date": DAY.isoformat(), "job": pulse.PULSE_SHARE, "credits_charged": spent})

    def call(self, route, params=None, **kw):
        self.calls.append({"route": route, "params": dict(params or {}), **kw})
        status = self.statuses.get(route, "ok")
        if status != "ok":
            return Result(status, route, reason=f"fake {status}")
        body = copy.deepcopy(BODIES[route])
        return Result("ok", route, "h", 200, body["credits_used"], body["credits_used"], False, body,
                      body["data"].get("items", []))


class NoCallClient(FakeClient):
    def call(self, *args, **kwargs):
        raise AssertionError("the pulse must not call SocialCrawl")


def run(rows, client, *, cap=100, clock=None, last_pulls=None):
    return pulse.run("pulse-1", rows=rows, client=client, clock=clock or (lambda: NOW), item_id_fn=fake_item_id,
                     geo_fn=fake_geo, last_pulls=last_pulls, caps=caps(cap))


def made(out):
    return [r for r in out["records"] if r["status"] == "ok"]


# Hot-item selection


def test_hot_items_take_the_latest_day_per_market_and_the_documented_rule():
    rows = [
        state("ZA", "amapiano", worth=0.95),
        state("ZA", "kotarun", worth=0.85, st="new_to_42"),
        state("ZA", "braai", worth=0.99, st="fading"),                 # not a hot state
        state("ZA", "shisanyama", worth=0.79),                         # under the worth floor
        state("ZA", "lookalike", worth=0.97, eligible=False),          # held back by the state rules
        state("ZA", "yesterday", worth=0.99, day=DAY - timedelta(days=1)),  # ZA has today's rows
        state("NG", "afrobeats", worth=0.9, day=DAY - timedelta(days=1)),   # NG's latest is yesterday
        state("KE", "gengetone", worth=0.9, day=DAY - timedelta(days=3)),   # too old to be today's news
    ]
    hot = pulse.hot_items(rows, DAY)
    assert [r["canonical_key"] for r in hot["ZA"]] == ["amapiano", "kotarun"]
    assert [r["canonical_key"] for r in hot["NG"]] == ["afrobeats"]
    assert hot["KE"] == []


def test_hot_items_keep_the_top_n_by_worth_then_item_id():
    rows = [state("ZA", f"tag{i}", worth=0.8 + i / 100) for i in range(8)] + [state("ZA", "tie", worth=0.87)]
    hot = pulse.hot_items(rows, DAY, n=3)
    # tag7 and tie both hold 0.87; the item id breaks the tie ("hashtag|tag7" before "hashtag|tie").
    assert [r["canonical_key"] for r in hot["ZA"]] == ["tag7", "tie", "tag6"]
    tied = pulse.hot_items([state("ZA", "b", worth=0.9), state("ZA", "a", worth=0.9)], DAY, n=1)
    assert [r["canonical_key"] for r in tied["ZA"]] == ["a"]


def test_every_hot_state_counts_and_states_outside_it_do_not():
    rows = [state("ZA", f"k{i}", st=s) for i, s in enumerate(pulse.HOT_STATES + ("fading", "mainstream", None))]
    hot = pulse.hot_items(rows, DAY, n=20)
    assert {r["state"] for r in hot["ZA"]} == set(pulse.HOT_STATES)


def test_the_hot_sql_only_reads():
    sql = pulse.HOT_SQL.upper()
    assert sql.lstrip().startswith("SELECT")
    for word in ("INSERT", "MERGE", "UPDATE", "DELETE", "DROP", "TRUNCATE", "CREATE", "ALTER", "REPLACE"):
        assert word not in sql, word
    assert "@D" in sql and "STATUS = 'OK'" in sql and "VALID_TO IS NULL" in sql


def test_read_hot_runs_the_hot_sql_with_its_parameters():
    class Job:
        def __init__(self, rows):
            self.rows = rows

        def result(self):
            return self.rows

    class FakeBQ:
        def __init__(self):
            self.seen = []

        def query(self, sql, job_config=None):
            self.seen.append((sql, {p.name: p for p in job_config.query_parameters}))
            return Job([state("ZA", "amapiano")])

    bq = FakeBQ()
    rows = pulse.read_hot(bq, DAY)
    sql, params = bq.seen[0]
    assert sql == pulse.HOT_SQL and rows[0]["canonical_key"] == "amapiano"
    assert params["d"].value == DAY and list(params["states"].values) == list(pulse.HOT_STATES)
    assert params["min_worth"].value == pulse.MIN_WORTH


# Calls


def test_calls_re_read_hashtags_and_sounds_once_each_then_one_feed_pull_per_market():
    rows = [state("ZA", "amapiano", worth=0.95), state("NG", "amapiano", worth=0.95),
            state("ZA", "7300000000000000001", kind="sound", canonical="tiktok:7300000000000000001", worth=0.9),
            state("KE", "a topic", kind="topic", worth=0.9)]
    calls, skipped = pulse.pulse_calls(pulse.hot_items(rows, DAY), fake_item_id)
    counters = [c for c in calls if c.route != pulse.FEED]
    assert [(c.route, c.params) for c in counters] == [
        ("tiktok/hashtag", {"hashtag": "amapiano"}), ("tiktok/song", {"clipId": "7300000000000000001"})]
    assert all(c.market == "GLOBAL" and c.lane == "watchlist" and c.item_id for c in counters)
    feeds = [c for c in calls if c.route == pulse.FEED]
    assert calls[-3:] == feeds and [c.market for c in feeds] == ["ZA", "NG", "KE"]
    assert all(c.params == {"region": c.market, "feed": "local"} and c.lane == "sweep" for c in feeds)
    assert [(s["market"], s["kind"]) for s in skipped] == [("KE", "topic")]


def test_a_key_that_does_not_map_back_to_the_item_is_skipped():
    row = state("ZA", "amapiano")
    row["item_id"] = "hashtag|something else"
    calls, skipped = pulse.pulse_calls({"ZA": [row], "NG": [], "KE": []}, fake_item_id)
    assert [c.route for c in calls] == [pulse.FEED] * 3
    assert skipped[0]["status"] == "no_key"


def test_calls_interleave_markets_by_rank():
    rows = [state(m, f"{m.lower()}{i}", worth=0.95 - i / 100) for m in ("ZA", "NG", "KE") for i in range(2)]
    calls, _ = pulse.pulse_calls(pulse.hot_items(rows, DAY), fake_item_id)
    assert [c.seed_key for c in calls if c.route != pulse.FEED] == ["za0", "ng0", "ke0", "za1", "ng1", "ke1"]


# Rule 1


def test_rule_one_items_are_never_queried(monkeypatch):
    rows = [state("ZA", "dancechallenge", label=f"{COHORT} dance", worth=0.99),
            state("ZA", f"{COHORT}tok", worth=0.98),
            state("ZA", "amapiano", worth=0.9)]
    client = FakeClient()
    out = run(rows, client)
    queried = [c["params"] for c in client.calls if c["route"] != pulse.FEED]
    assert queried == [{"hashtag": "amapiano"}]
    held = [r for r in out["records"] if r["status"] == "rule_1"]
    assert len(held) == 2 and all("rule 1" in r["reason"] for r in held)
    assert all(r["seed_key"] is None and COHORT not in r["reason"] for r in held)
    assert not any(COHORT in json.dumps(c["params"]) for c in client.calls)


# Cap


def test_no_pulse_cap_in_the_caps_means_zero_and_no_call():
    assert pulse.pulse_cap(caps(None)) == 0
    rows = [state("ZA", "amapiano")]
    for client in (NoCallClient(), None):
        out = run(rows, client, cap=None)
        assert out["credits"] == 0 and out["observations"] == [] and out["counters"] == []
        assert {r["status"] for r in out["records"]} == {"no_cap"}
        assert len(out["records"]) == 4 and all("SETUP.md" in r["reason"] for r in out["records"])


def test_a_cap_of_zero_makes_no_call():
    out = run([state("ZA", "amapiano")], NoCallClient(), cap=0)
    assert {r["status"] for r in out["records"]} == {"no_cap"}


def test_a_bad_cap_is_refused():
    for bad in (-1, "ten", float("nan"), True):
        with pytest.raises(ValueError):
            pulse.pulse_cap(caps(bad))


def test_the_pulse_spends_only_from_its_own_share():
    with pytest.raises(ValueError, match="pulse"):
        run([state("ZA", "amapiano")], FakeClient(share="collect"))
    with pytest.raises(ValueError, match="pulse"):
        run([state("ZA", "amapiano")], None)


class FakeHTTP:
    """The vendor: answers from the fixtures, with an optional x-credit-cost header per route."""

    def __init__(self, cost=None):
        self.cost, self.requests = dict(cost or {}), []

    def __call__(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        route = url.split("/v1/", 1)[1]
        self.requests.append(route)
        if route == "credits/balance":
            return 200, {"success": True, "data": {"balance": 250000}}, {}
        body = copy.deepcopy(BODIES[route])
        cost = self.cost.get(route)
        return 200, body, ({"x-credit-cost": str(cost)} if cost is not None else {})

    def paid(self):
        return [r for r in self.requests if r != "credits/balance"]


def real_client(cap, http, ledger=None, raw=None):
    """The real client. It has no pulse share yet (reported in OPEN), so the test adds the entry it needs."""
    client = SocialCrawlClient(share="reserve", run_id="pulse-1", mode="live", ledger=ledger or MemoryLedgerStore(),
                               raw=raw or MemoryRawStore(), http=http, clock=lambda: NOW)
    client.share = pulse.PULSE_SHARE
    client.share_caps[pulse.PULSE_SHARE] = cap
    return client


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sc-test-sentinel-pulse")


def three_tags():
    return [state("ZA", "amapiano", worth=0.95), state("NG", "afrobeats", worth=0.95),
            state("KE", "gengetone", worth=0.95)]


def test_the_cap_stops_calls_that_would_pass_it(key):
    http = FakeHTTP()
    client = real_client(4, http)
    out = run(three_tags(), client, cap=4)
    assert http.paid() == ["tiktok/hashtag"] * 3  # each feed pull holds 5, over the 1 credit left
    assert out["credits"] == 3 and client.ledger.spent(DAY, DAY, job="pulse") == 3
    over = [r for r in out["records"] if r["status"] == "over_pulse_cap"]
    assert [r["route"] for r in over] == [pulse.FEED] * 3 and out["stopped"] is None


def test_an_over_reporting_vendor_is_charged_in_full_and_stops_the_pulse(key):
    http = FakeHTTP(cost={"tiktok/hashtag": 5})
    ledger = MemoryLedgerStore()
    out = run(three_tags(), real_client(3, http, ledger), cap=3)
    assert http.paid() == ["tiktok/hashtag"]
    assert out["credits"] == 5 and ledger.spent(DAY, DAY, job="pulse") == 5
    assert out["stopped"] and "cap" in out["stopped"]
    assert {r["status"] for r in out["records"][1:]} == {"not_made"}
    again = run(three_tags(), real_client(3, http, ledger), cap=3)  # the next pulse the same day
    assert http.paid() == ["tiktok/hashtag"] and again["credits"] == 0


def test_the_day_s_earlier_pulses_count_against_the_cap(key):
    ledger = MemoryLedgerStore()
    ledger.append({"trend_date": DAY.isoformat(), "job": "pulse", "credits_charged": 2})
    ledger.append({"trend_date": DAY.isoformat(), "job": "collect", "credits_charged": 700})  # another share
    ledger.append({"trend_date": (DAY - timedelta(days=1)).isoformat(), "job": "pulse", "credits_charged": 9})
    http = FakeHTTP()
    out = run(three_tags(), real_client(3, http, ledger), cap=3)
    assert http.paid() == ["tiktok/hashtag"] and out["credits"] == 1


def test_a_stop_status_from_the_client_ends_the_pulse():
    client = FakeClient(statuses={"tiktok/hashtag": "insufficient_credits"})
    out = run(three_tags(), client)
    assert len(client.calls) == 1 and out["stopped"] == "insufficient_credits"
    assert {r["status"] for r in out["records"][1:]} == {"not_made"}


# pull_seq, rows and the cache


def test_pull_seq_continues_from_the_last_stored_pull():
    client = FakeClient()
    out = run([state("ZA", "amapiano")], client, last_pulls={FEED_ZA: 3})
    za = [o for o in out["observations"] if o["market"] == "ZA" and o["route"] == pulse.FEED]
    assert za and {o["pull_seq"] for o in za} == {4}
    ranks = [c for c in out["counters"] if c["unit"] == "rank" and c["market"] == "ZA"]
    assert ranks and {c["pull_seq"] for c in ranks} == {4}
    ng = {o["pull_seq"] for o in out["observations"] if o["market"] == "NG"}
    assert ng == {1}
    assert {r["pull_seq"] for r in out["records"] if r["route"] == pulse.FEED and r["market"] == "ZA"} == {4}


def test_rows_carry_the_ddl_columns_and_delta_ready_totals():
    rows = [state("ZA", "amapiano"),
            state("ZA", "7300000000000000001", kind="sound", canonical="tiktok:7300000000000000001")]
    out = run(rows, FakeClient())
    assert all(tuple(o) == OBSERVATION_COLUMNS for o in out["observations"])
    assert all(tuple(c) == COUNTER_COLUMNS for c in out["counters"])
    totals = {c["item_id"]: c for c in out["counters"] if c["unit"] == "total"}
    assert totals[rows[0]["item_id"]]["value"] == 91000.0
    assert totals[rows[1]["item_id"]]["value"] == 15400.0
    assert all(t["market"] == "GLOBAL" and t["run_id"] == "pulse-1" for t in totals.values())
    assert out["posts"]


def test_every_call_skips_the_same_day_cache():
    client = FakeClient()
    run(three_tags(), client)
    assert client.calls and all(c["use_cache"] is False for c in client.calls)


def test_a_same_day_morning_response_is_not_served_to_the_pulse(key):
    raw = MemoryRawStore()
    http = FakeHTTP()
    morning = real_client(100, http, raw=raw)
    morning.call("tiktok/hashtag", {"hashtag": "amapiano"}, market="GLOBAL")
    run([state("ZA", "amapiano")], real_client(100, http, raw=raw), cap=100)
    assert http.paid().count("tiktok/hashtag") == 2


def test_the_ledger_rows_name_the_item(key):
    ledger = MemoryLedgerStore()
    rows = [state("ZA", "amapiano")]
    run(rows, real_client(100, FakeHTTP(), ledger), cap=100)
    hashtag = [r for r in ledger.rows if r["route"] == "tiktok/hashtag"]
    assert hashtag[0]["item_id"] == rows[0]["item_id"] and hashtag[0]["job"] == "pulse"


# Chain window


def sast(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=pulse.SAST)


def test_the_pulse_never_runs_inside_the_chain_window_or_across_a_market_midnight():
    for moment in (sast(2, 0), sast(3, 0), sast(6, 30), sast(22, 31), sast(23, 30), sast(1, 0), sast(0, 10)):
        assert pulse.window_reason(moment), moment
        with pytest.raises(pulse.OutsideWindow):
            run(three_tags(), NoCallClient(), clock=lambda m=moment: m)
    for moment in (sast(6, 31), sast(14, 0), sast(22, 30)):
        assert pulse.window_reason(moment) == "", moment
    assert "02:00 to 06:30" in pulse.window_reason(sast(4, 0))


def test_a_pulse_that_runs_into_the_closed_window_stops():
    moments = iter([sast(22, 29), sast(22, 29), sast(22, 29), sast(22, 31)])
    last = [sast(22, 31)]

    def clock():
        try:
            last[0] = next(moments)
        except StopIteration:
            pass
        return last[0]

    client = FakeClient()
    out = run(three_tags(), client, clock=clock)
    assert len(client.calls) == 1
    assert "window_closed" in {r["status"] for r in out["records"]}


# No writes


def test_the_pulse_writes_nothing(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the pulse must not write")

    for name in ("append", "merge_posts", "write_run", "_query", "last_pulls", "deltas"):
        monkeypatch.setattr(writers, name, refuse)
    out = run(three_tags(), FakeClient())
    assert out["counters"] and out["observations"]
    source = Path(pulse.__file__).read_text(encoding="utf-8")
    assert "insert_rows" not in source and "load_table" not in source


# --plan


def test_plan_cli_prints_the_calls_and_holds_with_no_network(monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("--plan opened a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    assert pulse.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert "tiktok/hashtag" in out and "tiktok/trending" in out and "02:00 to 06:30" in out
    for market in ("ZA", "NG", "KE"):
        assert market in out
    if pulse.pulse_cap() == 0:
        assert "no call is made" in out


def test_the_cli_refuses_a_live_run():
    with pytest.raises(SystemExit):
        pulse.main([])
