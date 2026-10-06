"""Tests for core/collect/pulse_job.py, the Cloud Run job f42-pulse (FEATURES.md row 30a).

No network and no BigQuery: bq is an object that fails on any use, every read and write goes through
monkeypatched core/collect/writers.py and pulse.read_hot, the runs store is chain.MemoryRunsStore, and the
client is test_pulse's fake on the pulse share answering from the pulse fixtures.
"""

import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from core.collect import chain, pulse, pulse_job, writers
from core.collect.tests.test_pulse import COHORT, DAY, FEED_ZA, NOW, FakeClient, fake_geo, fake_item_id, state

SCHEMA = Path(__file__).resolve().parents[2] / "schema" / "core.sql"


class Untouchable:
    """bq for the job: any direct use fails, so every read and write has to go through writers or pulse."""

    def __getattr__(self, name):
        raise AssertionError(f"the pulse job used bq.{name} directly")


BQ = Untouchable()


class Writes:
    """Fakes for pulse.read_hot and the writers the job may use; records each call in order."""

    def __init__(self, monkeypatch, hot=None, pulls=None, fail=None):
        self.calls = []
        self.hot = [state("ZA", "amapiano"), state("NG", "afrobeats")] if hot is None else hot
        self.pulls = {FEED_ZA: 7} if pulls is None else pulls
        self.fail = fail

        def read_hot(bq, day):
            assert bq is BQ
            self.calls.append(("read_hot", day))
            return self.hot

        def last_pulls(bq, day):
            assert bq is BQ
            self.calls.append(("last_pulls", day))
            return dict(self.pulls)

        def merge_posts(bq, rows, batch=writers.BATCH):
            assert bq is BQ
            self.calls.append(("merge_posts", list(rows)))
            return 1

        def append(bq, name, rows):
            assert bq is BQ
            if name == self.fail:
                raise RuntimeError(f"load into {name} failed")
            self.calls.append(("append", name, list(rows)))
            return len(rows)

        def deltas(bq, rows, run_id):
            assert bq is BQ
            self.calls.append(("deltas", run_id))
            return []

        def insert_post_items(bq, rows):
            assert bq is BQ
            self.calls.append(("insert_post_items", list(rows)))
            return len(rows)

        monkeypatch.setattr(pulse, "read_hot", read_hot)
        for name, fn in (("last_pulls", last_pulls), ("merge_posts", merge_posts), ("append", append),
                         ("deltas", deltas), ("insert_post_items", insert_post_items)):
            monkeypatch.setattr(writers, name, fn)

    def names(self):
        return [c[0] if c[0] != "append" else f"append {c[1]}" for c in self.calls]

    def appended(self, name):
        [rows] = [c[2] for c in self.calls if c[0] == "append" and c[1] == name]
        return rows


def collect_ok(runs, day=DAY, status="ok"):
    runs.append({"run_id": f"collect-{day:%Y%m%d}-x", "stage": "collect", "run_date": day.isoformat(),
                 "status": status, "started_at": "2026-10-05T00:00:00+00:00",
                 "finished_at": "2026-10-05T01:30:00+00:00", "counts": {}, "error": None})


class Clients:
    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.made = []

    def __call__(self, run_id):
        self.made.append(run_id)
        return self.client


def main(runs, clients, *, clock=lambda: NOW):
    return pulse_job.main([], runs=runs, bq=BQ, make_client=clients, fns=(fake_item_id, fake_geo), clock=clock)


def pulse_rows(runs):
    return [r for r in runs.rows if r["stage"] == "pulse"]


# The window guard


@pytest.mark.parametrize("utc", [
    datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc),    # 05:00 SAST, inside the morning chain window
    datetime(2026, 10, 5, 4, 29, tzinfo=timezone.utc),   # 06:29 SAST
    datetime(2026, 10, 5, 20, 31, tzinfo=timezone.utc),  # 22:31 SAST
    datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc),  # 01:30 SAST
])
def test_outside_the_window_it_exits_0_with_one_line_before_reading_anything(utc, monkeypatch, capsys):
    writes, runs, clients = Writes(monkeypatch), chain.MemoryRunsStore(), Clients()
    collect_ok(runs, utc.astimezone(chain.SAST).date())
    assert main(runs, clients, clock=lambda: utc) == 0
    [line] = capsys.readouterr().out.splitlines()
    assert line.startswith("pulse: not run, ") and "SAST" in line
    assert writes.calls == [] and clients.made == [] and pulse_rows(runs) == []


@pytest.mark.parametrize("utc", [
    datetime(2026, 10, 5, 4, 31, tzinfo=timezone.utc),   # 06:31 SAST
    datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc),    # 09:00 SAST, the first scheduled pulse
    datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc),   # 21:00 SAST, the last
])
def test_inside_the_window_it_runs(utc, monkeypatch):
    writes, runs, clients = Writes(monkeypatch), chain.MemoryRunsStore(), Clients()
    collect_ok(runs)
    assert main(runs, clients, clock=lambda: utc) == 0
    assert "read_hot" in writes.names() and len(clients.made) == 1


# The collect-finished guard


@pytest.mark.parametrize("setup", ["none", "running", "failed", "blocked", "yesterday"])
def test_it_never_runs_until_the_days_collect_finished_ok(setup, monkeypatch, capsys):
    writes, runs, clients = Writes(monkeypatch), chain.MemoryRunsStore(), Clients()
    if setup == "yesterday":
        collect_ok(runs, date(2026, 10, 4))
    elif setup != "none":
        collect_ok(runs, status=setup)
    assert main(runs, clients) == 0
    [line] = capsys.readouterr().out.splitlines()
    assert line.startswith("pulse: not run, collect for 2026-10-05 has not finished ok")
    assert writes.calls == [] and clients.made == [] and pulse_rows(runs) == []


def test_a_later_ok_collect_row_lets_it_run_and_a_later_failed_one_stops_it(monkeypatch):
    Writes(monkeypatch)
    runs = chain.MemoryRunsStore()
    collect_ok(runs, status="failed")
    collect_ok(runs)
    assert main(runs, Clients()) == 0 and pulse_rows(runs)[-1]["status"] == "ok"
    Writes(monkeypatch)
    runs = chain.MemoryRunsStore()
    collect_ok(runs)
    runs.append({**runs.rows[0], "status": "failed", "finished_at": "2026-10-05T02:00:00+00:00"})
    clients = Clients()
    assert main(runs, clients) == 0 and clients.made == [] and pulse_rows(runs) == []


# A run: reads, client, writes through the writers only, runs rows


def test_a_run_reads_calls_on_the_pulse_share_and_writes_only_through_the_writers(monkeypatch, capsys):
    writes, runs, clients = Writes(monkeypatch), chain.MemoryRunsStore(), Clients()
    collect_ok(runs)
    assert main(runs, clients) == 0
    assert writes.names() == ["read_hot", "last_pulls", "merge_posts", "deltas", "append post_observations",
                              "insert_post_items", "append item_counter_daily", "append item_hourly"]
    assert [c[1] for c in writes.calls[:2]] == [DAY, DAY]
    [run_id] = clients.made
    assert run_id.startswith("pulse-20261005-")
    client = clients.client
    assert client.share == pulse.PULSE_SHARE
    routes = [c["route"] for c in client.calls]
    assert routes.count("tiktok/hashtag") == 2 and routes.count("tiktok/trending") == 3
    assert all(c["use_cache"] is False for c in client.calls)
    observations = writes.appended("post_observations")
    assert observations and all(o["run_id"] == run_id for o in observations)
    # pull_seq runs on from the last stored pull of the ZA local feed.
    za_feed = {o["pull_seq"] for o in observations if o["route"] == "tiktok/trending" and o["market"] == "ZA"}
    assert za_feed == {8}
    scoped_feed = [o for o in observations if o["route"] == "tiktok/trending"]
    assert scoped_feed and all((o["source_market"], o["source_region"]) == (o["market"], o["market"])
                               for o in scoped_feed)
    counters = writes.appended("item_counter_daily")
    assert {c["unit"] for c in counters} >= {"total", "rank", "appearances"}
    assert all(c["run_id"] == run_id for c in counters)
    assert writes.appended("item_hourly")
    out = capsys.readouterr().out
    assert run_id in out


def test_the_runs_rows_are_a_running_row_then_an_ok_row_with_stage_pulse_and_the_counts(monkeypatch):
    writes, runs, clients = Writes(monkeypatch), chain.MemoryRunsStore(), Clients()
    collect_ok(runs)
    assert main(runs, clients) == 0
    running, ok = pulse_rows(runs)
    [run_id] = clients.made
    assert (running["run_id"], running["status"], running["finished_at"]) == (run_id, "running", None)
    assert (ok["run_id"], ok["status"], ok["run_date"], ok["error"]) == (run_id, "ok", "2026-10-05", None)
    assert ok["started_at"] == running["started_at"] and ok["finished_at"] is not None
    counts = ok["counts"]
    assert counts["hot_rows"] == 2
    assert counts["credits"] == sum(1 if c["route"] == "tiktok/hashtag" else 5 for c in clients.client.calls)
    assert counts["observations"] == len(writes.appended("post_observations"))
    assert counts["counters"] == len(writes.appended("item_counter_daily"))
    assert counts["item_hourly"] == len(writes.appended("item_hourly"))
    assert counts["statuses"] == {"ok": 5}
    assert counts["stopped"] is None
    assert "pulse" not in chain.STAGES and "pulse" not in chain.JOBS


def test_a_failed_write_finishes_the_run_failed_and_exits_1(monkeypatch):
    Writes(monkeypatch, fail="item_hourly")
    runs, clients = chain.MemoryRunsStore(), Clients()
    collect_ok(runs)
    assert main(runs, clients) == 1
    running, failed = pulse_rows(runs)
    assert failed["run_id"] == running["run_id"] and failed["status"] == "failed"
    assert "RuntimeError" in failed["error"] and "item_hourly" in failed["error"]
    assert failed["counts"]["observations"] > 0


def test_a_failed_read_finishes_the_run_failed_before_any_call(monkeypatch):
    writes = Writes(monkeypatch)

    def broken(bq, day):
        raise RuntimeError("read failed")

    monkeypatch.setattr(pulse, "read_hot", broken)
    runs, clients = chain.MemoryRunsStore(), Clients()
    collect_ok(runs)
    assert main(runs, clients) == 1
    assert [r["status"] for r in pulse_rows(runs)] == ["running", "failed"]
    assert clients.client.calls == [] and writes.calls == []


def test_the_live_client_spends_only_from_the_pulse_share(monkeypatch):
    from core.collect import socialcrawl_client, stores

    made = {}

    class Recorder:
        def __init__(self, **kw):
            made.update(kw)

    monkeypatch.setattr(socialcrawl_client, "SocialCrawlClient", Recorder)
    monkeypatch.setattr(stores, "BigQueryLedgerStore", lambda bq, project: ("ledger", project))
    monkeypatch.setattr(stores, "BigQueryRawStore", lambda bq, project: ("raw", project))
    pulse_job.live_client("pulse-1", BQ, lambda: NOW)
    assert made["share"] == pulse.PULSE_SHARE == "pulse"
    assert (made["run_id"], made["mode"]) == ("pulse-1", "live")
    assert made["ledger"] == ("ledger", chain.PROJECT) and made["raw"] == ("raw", chain.PROJECT)


# item_hourly


def obs(post_id, at, *, market="ZA", route="tiktok/trending", lane_class="unbiased_rank", seed_key=None):
    return {"post_id": post_id, "observed_at": at, "market": market, "platform": "tiktok", "route": route,
            "lane_class": lane_class, "seed_key": seed_key}


def post(post_id, creator, hashtags=(), sound=None):
    return {"post_id": post_id, "platform": "tiktok", "creator_id": creator, "hashtags": list(hashtags),
            "sound_id": sound}


AMAPIANO = fake_item_id("hashtag", "amapiano", "tiktok")
SOUND = fake_item_id("sound", "73", "tiktok")
H14 = "2026-10-05T12:00:00+00:00"  # 14:00 SAST
H15 = "2026-10-05T13:00:00+00:00"  # 15:00 SAST


def test_item_hourly_counts_posts_and_distinct_creators_per_market_item_platform_and_sast_hour():
    out = {"run_id": "pulse-1",
           "posts": [post("p1", "kasi", ["amapiano"], "73"), post("p2", "kasi", ["Amapiano"]),
                     post("p3", "lulu", ["amapiano"]), post("p4", None, ["amapiano"]),
                     post("p5", "zee", ["amapiano"])],
           "observations": [obs("p1", "2026-10-05T12:05:00+00:00"), obs("p2", "2026-10-05T12:40:00+00:00"),
                            obs("p3", "2026-10-05T12:59:59+00:00"), obs("p4", "2026-10-05T12:10:00+00:00"),
                            obs("p1", "2026-10-05T12:20:00+00:00"),        # the same post twice in one hour
                            obs("p5", "2026-10-05T13:00:00+00:00")]}      # the next hour
    rows = pulse_job.hourly_rows(out, fake_item_id)
    by = {(r["market"], r["item_id"], r["platform"], r["hour"]): r for r in rows}
    assert len(by) == len(rows)
    assert by["ZA", AMAPIANO, "tiktok", H14] == {
        "market": "ZA", "item_id": AMAPIANO, "platform": "tiktok", "hour": H14, "posts": 4, "creators": 2,
        "lane_class": "unbiased_rank", "run_id": "pulse-1"}
    assert by["ZA", AMAPIANO, "tiktok", H15]["posts"] == 1 and by["ZA", AMAPIANO, "tiktok", H15]["creators"] == 1
    assert by["ZA", SOUND, "tiktok", H14]["posts"] == 1
    kasi = fake_item_id("creator", "kasi", "tiktok")
    assert (by["ZA", kasi, "tiktok", H14]["posts"], by["ZA", kasi, "tiktok", H14]["creators"]) == (2, 1)


def test_item_hourly_hour_is_the_sast_hour_start_across_a_utc_day_boundary():
    out = {"run_id": "r", "posts": [post("p1", "a", ["amapiano"])],
           "observations": [obs("p1", "2026-10-04T22:59:00+00:00")]}  # 00:59 SAST on 5 October
    [row] = [r for r in pulse_job.hourly_rows(out, fake_item_id) if r["item_id"] == AMAPIANO]
    assert row["hour"] == "2026-10-04T22:00:00+00:00"
    assert datetime.fromisoformat(row["hour"]).astimezone(chain.SAST).hour == 0


def test_item_hourly_keeps_the_lane_class_as_written_and_names_the_item_a_counter_read_was_for():
    out = {"run_id": "r", "posts": [post("tag", None, [])],
           "observations": [obs("tag", H14, market="GLOBAL", route="tiktok/hashtag", lane_class="watchlist",
                                seed_key="amapiano")]}
    [row] = pulse_job.hourly_rows(out, fake_item_id)
    assert (row["market"], row["item_id"], row["lane_class"], row["posts"], row["creators"]) == (
        "GLOBAL", AMAPIANO, "watchlist", 1, 0)


def test_item_hourly_leaves_out_rule_1_keys_and_keys_item_id_fn_refuses():
    out = {"run_id": "r", "posts": [post("p1", None, [f"{COHORT}life", "", "has space", "amapiano"])],
           "observations": [obs("p1", H14)]}
    rows = pulse_job.hourly_rows(out, fake_item_id)
    assert [r["item_id"] for r in rows] == [AMAPIANO]
    assert not any(COHORT in r["item_id"] for r in rows)


def test_item_hourly_rows_hold_exactly_the_schema_columns():
    text = SCHEMA.read_text(encoding="utf-8")
    block = re.search(r"intelligence_42_core\.item_hourly` \((.*?)\)\s*PARTITION BY DATE\(hour\)", text, re.S).group(1)
    columns = [m.group(1) for m in re.finditer(r"(\w+) (?:STRING|INT64|TIMESTAMP)", block)]
    out = {"run_id": "r", "posts": [post("p1", "a", ["amapiano"])], "observations": [obs("p1", H14)]}
    for row in pulse_job.hourly_rows(out, fake_item_id):
        assert list(row) == columns


def test_item_hourly_is_empty_without_observations():
    assert pulse_job.hourly_rows({"run_id": "r", "posts": [], "observations": []}, fake_item_id) == []


# post_items (L2 Needs 34c): the pulse links its posts to their items as the daily aggregate step does


def test_post_items_rows_are_the_daily_aggregate_links_of_each_pulse_post():
    from core.detect import aggregate

    posts = [post("p1", "kasi", ["amapiano", "Amapiano"], "73"), post("p2", None, ["amapiano", ""]),
             post("p1", "kasi", ["amapiano"], "73")]          # a post seen twice in one pulse
    rows = pulse_job.post_items_rows({"run_id": "r", "posts": posts, "observations": []})
    daily, _ = aggregate.items_rows([dict(p, market="ZA", first_day=DAY) for p in posts[:2]])
    assert rows == daily
    assert {r["via"] for r in rows} == {"hashtag", "sound", "creator"}
    assert len({(r["post_id"], r["item_id"]) for r in rows}) == len(rows)


def test_post_items_name_the_same_items_as_item_hourly_with_the_live_item_ids():
    from core.collect.job import detect_fns

    item_id_fn, _ = detect_fns()
    out = {"run_id": "r", "posts": [post("p1", "kasi", ["amapiano"], "73")], "observations": [obs("p1", H14)]}
    linked = {r["item_id"] for r in pulse_job.post_items_rows(out)}
    assert linked == {r["item_id"] for r in pulse_job.hourly_rows(out, item_id_fn)}


def test_insert_post_items_is_the_daily_steps_insert_that_skips_links_already_written():
    from core.detect import aggregate, sqlrun

    class Recorder:
        def __init__(self):
            self.queries = []

        def query(self, sql, job_config=None):
            self.queries.append((sql, {p.name: p for p in job_config.query_parameters}))
            return type("Job", (), {"result": lambda self: []})()

    bq = Recorder()
    assert writers.insert_post_items(bq, []) == 0 and not bq.queries
    rows = [{"post_id": f"p{i}", "item_id": "i", "via": "hashtag"} for i in range(aggregate.CHUNK + 1)]
    assert writers.insert_post_items(bq, rows) == len(rows)
    assert len(bq.queries) == 2
    sql, params = bq.queries[0]
    assert sql == sqlrun.render(aggregate.POST_ITEMS_INSERT_SQL, writers.CORE, writers.AGENT)
    assert sql.lstrip().upper().startswith("INSERT INTO") and "NOT EXISTS" in sql
    assert not re.search(r"\b(DELETE|DROP|TRUNCATE|REPLACE|MERGE|UPDATE)\b", sql.upper())
    assert list(params) == ["rows"] and len(params["rows"].values) == aggregate.CHUNK
    assert dict(params["rows"].values[0].struct_types) == dict(aggregate.POST_ITEM_FIELDS)


def test_a_run_appends_post_items_for_its_posts_after_their_observations(monkeypatch):
    writes, runs, clients = Writes(monkeypatch), chain.MemoryRunsStore(), Clients()
    collect_ok(runs)
    assert main(runs, clients) == 0
    names = writes.names()
    assert names.index("append post_observations") < names.index("insert_post_items")
    [rows] = [c[1] for c in writes.calls if c[0] == "insert_post_items"]
    posts = {p["post_id"] for c in writes.calls if c[0] == "merge_posts" for p in c[1]}
    assert rows and {r["post_id"] for r in rows} <= posts
    assert pulse_rows(runs)[-1]["counts"]["post_items"] == len(rows)
