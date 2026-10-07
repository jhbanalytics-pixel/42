"""The slow pages read faster without reading anything different (core/api/fast.py, store.get_store).

Live on staging, 4 October 2026, the topic page took 38.9 s, Alerts 33.5 s, Discover 25.8 s, Compare 22.1 s, Radar
19.9 s, Today 17.6 s, History 12 to 14 s and Communities 58 s: each made 9 to 17 BigQuery reads one after another,
each on a new client. These tests hold the fix to the same bodies, no extra reads and fresh suppressions."""
import contextvars
import copy
import datetime as dt
import itertools
import threading
import time
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from core.api import compare, discover, fast, history, people, today
from core.api import store as store_mod
from core.api.store import BigQueryStore, FixtureStore

STEP = "7107ad863306852108ebb88392f1d5e0293922af5f5e7a5a4b81d466930f646e"
HER = "afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156"
PASS = "speed-test-passcode"


# Reads one after another are counted as rounds, not timed, so a loaded machine cannot change the count. A read is
# one round after the latest read whose result its caller had when it asked; a read the pool runs starts from the
# round of whoever started it, and whoever takes its result is then at least that far along (_RoundPool). A page's
# rounds are its longest chain of reads each waiting on an earlier one: its time when every read takes as long.
# Rounds are kept per store, so whatever another store counted on a reused thread never adds to this one.
_ROUND = contextvars.ContextVar("round", default={})
_STORES = itertools.count()


def _later(rounds, more):
    """Each store's round as far along as either says."""
    return {k: max(rounds.get(k, 0), more.get(k, 0)) for k in {*rounds, *more}}


class Counting:
    """A FixtureStore that counts its reads, the round of each and, with delay, takes that long over each one."""

    def __init__(self, delay=0.0):
        self._inner, self.delay, self.calls, self.rounds = FixtureStore(), delay, [], []
        self._lock, self._key = threading.Lock(), next(_STORES)

    @property
    def depth(self):
        """Rounds: how many of its reads the store answered one after another."""
        return max(self.rounds, default=0)

    def __getattr__(self, name):
        value = getattr(self._inner, name)
        if not callable(value) or name.startswith("_"):
            return value

        def read(*args, **kwargs):
            level = _ROUND.get().get(self._key, 0) + 1
            with self._lock:
                self.calls.append(name)
                self.rounds.append(level)
            _ROUND.set({**_ROUND.get(), self._key: level})
            if self.delay:
                time.sleep(self.delay)
            return value(*args, **kwargs)

        return read


class _RoundFuture:
    def __init__(self, future):
        self._future = future

    def result(self, timeout=None):
        value, error, rounds = self._future.result(timeout)
        _ROUND.set(_later(_ROUND.get(), rounds))
        if error is not None:
            raise error
        return value


class _RoundPool:
    """fast's read pool, carrying the round from whoever starts a read to it, and from it to whoever takes it."""

    def __init__(self, pool):
        self._pool = pool

    def submit(self, fn, *args, **kwargs):
        def run():
            try:
                return fn(*args, **kwargs), None, _ROUND.get()
            except BaseException as e:
                return None, e, _ROUND.get()

        return _RoundFuture(self._pool.submit(contextvars.copy_context().run, run))


def _rounds(build, store):
    """(build(store), the store's rounds), built in a context of its own so it starts at round 0."""
    body = contextvars.Context().run(build, store)
    return body, store.depth


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(store_mod, "_CATALOG", {})
    monkeypatch.setattr(store_mod, "_SHARED", {}, raising=False)
    monkeypatch.setattr(fast, "_POOL", _RoundPool(fast._POOL))


# Pages: (name, plan and its arguments, the builder that reads through the store)
PAGES = [
    ("discover", (fast.discover_plan, "all"), lambda s: discover.build_discover(s, "all", limit=200)),
    ("discover ZA", (fast.discover_plan, "ZA"), lambda s: discover.build_discover(s, "ZA")),
    ("radar", (fast.radar_plan, "ZA"), lambda s: discover.build_radar(s, "ZA")),
    ("topic", (fast.topic_plan, STEP, "ZA"), lambda s: discover.build_topic(s, STEP, "ZA")),
    ("alerts", (fast.alerts_plan, None), lambda s: discover.build_alerts(s, [], date=None)),
    ("alerts dated", (fast.alerts_plan, "2026-09-30"), lambda s: discover.build_alerts(s, [], date="2026-09-30")),
    ("communities", (fast.communities_plan, "ZA"), lambda s: people.build_communities(s, "ZA")),
    ("today", (fast.today_plan, None), lambda s: today.build_today(s)),
    ("today dated", (fast.today_plan, "2026-09-30"), lambda s: today.build_today(s, date="2026-09-30")),
    ("history search", (fast.history_search_plan, "fixture_za", "ZA"),
     lambda s: history.build_history_search(s, "fixture_za", "ZA")),
    ("history item", (fast.history_item_plan, STEP, "ZA"), lambda s: history.build_history_item(s, STEP, "ZA")),
    ("compare", (fast.compare_plan, "items", f"{STEP},{HER}", "ZA", None, None, 28),
     lambda s: compare.build_compare(s, "items", items=f"{STEP},{HER}", market="ZA", days=28)),
]


@pytest.mark.parametrize("name,plan,build", PAGES, ids=[p[0] for p in PAGES])
def test_a_prefetched_page_returns_the_same_body_and_uses_every_read_it_started(name, plan, build):
    plain = build(FixtureStore())
    store = Counting()
    started = fast.prefetch(store, *plan)
    assert build(started) == plain
    assert started.unused() == []


@pytest.mark.parametrize("name,plan,build", PAGES, ids=[p[0] for p in PAGES])
def test_a_prefetched_page_reads_no_more_than_it_did_one_by_one(name, plan, build):
    serial, quick = Counting(), Counting()
    build(serial)
    build(fast.prefetch(quick, *plan))
    assert len(quick.calls) <= len(serial.calls), (sorted(quick.calls), sorted(serial.calls))


@pytest.mark.parametrize("name,plan,build", [p for p in PAGES if p[0] in ("topic", "communities", "today", "alerts")],
                         ids=["topic", "alerts", "communities", "today"])
def test_reads_a_page_always_makes_run_together(name, plan, build):
    _, one_by_one = _rounds(build, Counting())
    _, together = _rounds(lambda s: build(fast.prefetch(s, *plan)), Counting())
    assert one_by_one >= 9
    assert together < one_by_one / 2, (together, one_by_one)


def test_a_plan_that_fails_leaves_the_page_reading_one_by_one():
    def broken(p):
        p.start("latest_detect_run")
        raise RuntimeError("plan bug")

    plain = discover.build_discover(FixtureStore(), "ZA")
    started = fast.prefetch(Counting(), broken)
    assert discover.build_discover(started, "ZA") == plain


def test_a_started_read_that_fails_raises_only_when_the_page_makes_that_call():
    class Failing(Counting):
        def __getattr__(self, name):
            if name == "item_gate":
                def boom(*a, **k):
                    raise ValueError("gate read failed")
                return boom
            return super().__getattr__(name)

    started = fast.prefetch(Failing(), fast.discover_plan, "ZA")
    with pytest.raises(ValueError, match="gate read failed"):
        discover.build_discover(started, "ZA")
    started = fast.prefetch(Failing(), fast.history_item_plan, STEP, "ZA")
    history.build_history_item(started, STEP, "ZA")


def test_a_plan_skips_reads_the_store_does_not_have():
    class Bare:
        def latest_detect_run(self):
            return None

    started = fast.prefetch(Bare(), fast.topic_plan, STEP, "ZA")
    with pytest.raises(discover.NotReady):
        discover.build_topic(started, STEP, "ZA")


# The short cache of pipeline reads

def test_pipeline_reads_are_kept_for_the_ttl_then_read_again(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(fast, "clock", lambda: now[0])
    store = Counting()
    run = FixtureStore().latest_detect_run()
    first = fast.reads(store).item_states(run, "ZA")
    assert fast.reads(store).item_states(run, "ZA") == first
    assert store.calls.count("item_states") == 1
    now[0] += fast.CACHE_TTL + 1
    fast.reads(store).item_states(run, "ZA")
    assert store.calls.count("item_states") == 2


def test_the_ttl_is_short():
    assert 0 < fast.CACHE_TTL <= 300


@pytest.mark.parametrize("name,args", [("suppressed_creators", ()), ("suppressions", ()), ("ask_record", ("a_x",)),
                                       ("findings", ()), ("ask_history", (5, None)), ("breaking_signals",
                                                                                      ("2026-09-30T00:00:00+02:00",))])
def test_suppressions_asks_and_findings_are_always_read_fresh(name, args):
    store = Counting()
    for _ in range(3):
        getattr(fast.reads(store), name)(*args)
    assert store.calls.count(name) == 3
    assert name not in fast.CACHED


def test_a_cached_read_hands_out_a_copy():
    store = Counting()
    run = FixtureStore().latest_detect_run()
    rows = fast.reads(store).item_states(run, "ZA")
    clean = copy.deepcopy(rows)
    rows[0]["label"] = "changed by a page"
    rows.append({"item_id": "x"})
    assert fast.reads(store).item_states(run, "ZA") == clean


def test_different_arguments_are_read_separately_and_set_order_does_not_matter():
    store = Counting()
    run = FixtureStore().latest_detect_run()
    fast.reads(store).item_states(run, "ZA")
    fast.reads(store).item_states(run, "NG")
    assert store.calls.count("item_states") == 2
    fast.reads(store).creators_by_handle(["b:x", "a:y"])
    fast.reads(store).creators_by_handle(["b:x", "a:y"])
    assert store.calls.count("creators_by_handle") == 1
    assert fast._key("x", ({"b", "a", "c"},), {}) == fast._key("x", ({"c", "a", "b"},), {})


def test_each_store_has_its_own_cache():
    run = FixtureStore().latest_detect_run()
    one, two = Counting(), Counting()
    fast.reads(one).item_states(run, "ZA")
    fast.reads(two).item_states(run, "ZA")
    assert one.calls.count("item_states") == two.calls.count("item_states") == 1


def test_the_cache_holds_at_most_cache_max_entries():
    store = Counting()
    for n in range(fast.CACHE_MAX + 20):
        fast.reads(store).item_waves(f"i{n}", "ZA")
    assert len(fast._cache_of(store).rows) == fast.CACHE_MAX


# One BigQuery client per process, one round trip per read

def test_the_bigquery_store_is_built_once_per_process(monkeypatch):
    monkeypatch.setenv("F42_DATA", "bigquery")
    monkeypatch.setenv("F42_PROJECT", "test-project")
    made = []
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda *a, **k: made.append(k) or mock.MagicMock())
    first = store_mod.get_store()
    assert isinstance(first, BigQueryStore)
    assert store_mod.get_store() is first
    assert made == [{"project": "test-project"}]
    monkeypatch.setenv("F42_PROJECT", "other-project")
    assert store_mod.get_store() is not first


def test_fixtures_stay_one_store_a_call(monkeypatch):
    monkeypatch.delenv("F42_DATA", raising=False)
    assert store_mod.get_store() is not store_mod.get_store()


def real_client():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    return bigquery.Client(project="test-project", credentials=AnonymousCredentials())


def test_a_real_client_reads_in_one_round_trip_with_the_same_cap_and_parameters():
    client = real_client()
    seen = []
    client.query = mock.Mock(side_effect=AssertionError("jobs.insert path used"))
    client.query_and_wait = lambda sql, job_config=None: seen.append((sql, job_config)) or [
        mock.Mock(items=lambda: [("d", dt.date(2026, 9, 30))])]
    bq = BigQueryStore(client=client)
    assert bq.briefs("2026-09-30") == [{"d": "2026-09-30"}]
    (sql, cfg), = seen
    assert "@d" in sql and cfg.maximum_bytes_billed == 2_000_000_000
    assert [(p.name, p.type_, p.value) for p in cfg.query_parameters] == [("d", "DATE", dt.date(2026, 9, 30))]


def test_stand_in_clients_keep_the_query_path():
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []
    BigQueryStore(client=client).briefs("2026-09-30")
    assert client.query.call_count == 1
    assert client.query_and_wait.call_count == 0


def test_pages_read_together_read_the_catalog_once():
    calls, gate = [], threading.Event()

    class Client:
        def query(self, sql, job_config=None):
            calls.append(sql)
            gate.wait(1)
            return mock.Mock(result=lambda: [])

    bq = BigQueryStore(client=Client())
    threads = [threading.Thread(target=bq._catalog) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join()
    assert sum("INFORMATION_SCHEMA" in s for s in calls) == 1


# The routes read through the plans

@pytest.fixture
def slow_app(monkeypatch):
    from core.api import app as api_mod
    from core.api import auth

    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.delenv("F42_AUTH_MODE", raising=False)
    monkeypatch.delenv("IAP_AUDIENCE", raising=False)
    auth.auth_limiter.hits.clear()
    stores = []

    def get_store():
        stores.append(Counting(0.1))
        return stores[-1]

    monkeypatch.setattr(store_mod, "get_store", get_store)
    return TestClient(api_mod.app), stores


ROUTES = [
    ("/api/topics/" + STEP + "?market=ZA", lambda s: discover.build_topic(s, STEP, "ZA")),
    ("/api/discover?market=all&limit=200", lambda s: discover.build_discover(s, "all", limit=200)),
    ("/api/discover/radar?market=ZA", lambda s: discover.build_radar(s, "ZA")),
    ("/api/today", lambda s: today.build_today(s)),
    ("/api/communities?market=ZA", lambda s: people.build_communities(s, "ZA")),
    ("/api/history/search?q=fixture_za&market=ZA", lambda s: history.build_history_search(s, "fixture_za", "ZA")),
    ("/api/history/items/" + STEP + "?market=ZA", lambda s: history.build_history_item(s, STEP, "ZA")),
    (f"/api/compare?mode=items&items={STEP},{HER}&market=ZA&days=28",
     lambda s: compare.build_compare(s, "items", items=f"{STEP},{HER}", market="ZA", days=28)),
]


@pytest.mark.parametrize("path,build", ROUTES, ids=[r[0].split("?")[0] for r in ROUTES])
def test_slow_routes_read_together_and_answer_as_before(slow_app, path, build):
    client, stores = slow_app
    serial = Counting()
    expected, _ = _rounds(build, serial)
    r = client.get(path, headers={"X-Passcode": PASS})
    assert r.status_code == 200, r.text
    assert r.json() == _json(expected)
    assert len(stores[-1].calls) <= len(serial.calls)
    assert stores[-1].depth < len(serial.calls) * 0.7, (stores[-1].depth, len(serial.calls))


def _json(value):
    import json

    from fastapi.encoders import jsonable_encoder

    return json.loads(json.dumps(jsonable_encoder(value)))


# Round 19 (4 October 2026, evening): live, seed path took 21 to 24 s (ten reads one after another, no plan),
# communities 14 to 16 s, history items 7 s, history search 8 s and health 3 s (its checks one after another).

def test_seed_path_through_its_plan_returns_the_same_body_and_uses_every_read_it_started():
    from core.api import seedpath

    for keyword, market in (("amapiano", "ZA"), ("amapiano", "NG"), ("zzqx", "ZA"), ("#Amapiano", "ZA")):
        plain = seedpath.build_seed_path(FixtureStore(), keyword, market)
        started = fast.prefetch(Counting(), fast.seed_path_plan, keyword, market)
        assert seedpath.build_seed_path(started, keyword, market) == plain
        if plain["topics"]:
            assert started.unused() == []


def test_a_seed_path_plan_for_a_bad_keyword_or_market_reads_nothing():
    from core.api import seedpath

    for keyword, market in (("a", "ZA"), ("amapiano", "US"), (None, "ZA")):
        store = Counting()
        started = fast.prefetch(store, fast.seed_path_plan, keyword, market)
        with pytest.raises(seedpath.BadRequest):
            seedpath.build_seed_path(started, keyword, market)
        assert store.calls == []


PAGES_19 = [
    ("seed path", lambda s: fast.prefetch(s, fast.seed_path_plan, "amapiano", "ZA"),
     lambda s: __import__("core.api.seedpath", fromlist=["x"]).build_seed_path(s, "amapiano", "ZA"), 10, 4),
    ("communities NG", lambda s: fast.prefetch(s, fast.communities_plan, "NG"),
     lambda s: people.build_communities(s, "NG"), 9, 6),
    ("history item", lambda s: fast.prefetch(s, fast.history_item_plan, STEP, "ZA"),
     lambda s: history.build_history_item(s, STEP, "ZA"), 5, 4),
]


@pytest.mark.parametrize("name,plan,build,before,after", PAGES_19, ids=[p[0] for p in PAGES_19])
def test_dependent_reads_now_run_together(name, plan, build, before, after):
    """The page's reads one after another (before, in rounds) and through the plan now (fewer than after)."""
    plain, one_by_one = _rounds(build, Counting())
    body, together = _rounds(lambda s: build(plan(s)), Counting())
    assert body == plain
    assert one_by_one >= before
    assert together < after, (together, one_by_one)


def test_communities_with_a_community_read_no_more_than_one_by_one_and_use_every_started_read():
    serial = Counting()
    plain = people.build_communities(serial, "NG")
    assert plain["communities"]
    quick = Counting()
    started = fast.prefetch(quick, fast.communities_plan, "NG")
    assert people.build_communities(started, "NG") == plain
    assert started.unused() == []
    assert len(quick.calls) <= len(serial.calls)
    # The suppression list is read fresh each time the page asks for it, as before.
    assert quick.calls.count("suppressed_creators") == serial.calls.count("suppressed_creators")


def _a_community(market):
    return people.build_communities(FixtureStore(), market)["communities"][0]["community_id"]


def test_a_community_page_through_the_list_plan_answers_as_before_and_reads_together():
    """Visual QA, 5 October 2026: a community page took 37 to 44 s. It read every read one after another and
    uncached, while the list beside it read through communities_plan."""
    community = _a_community("NG")
    build = lambda s: people.build_community(s, community, "NG")  # noqa: E731
    plain, one_by_one = _rounds(build, Counting())
    started = fast.prefetch(Counting(), fast.communities_plan, "NG")
    assert build(started) == plain
    assert started.unused() == []
    body, together = _rounds(lambda s: build(fast.prefetch(s, fast.communities_plan, "NG")), Counting())
    assert body == plain
    assert together < one_by_one, (together, one_by_one)
    serial, quick = Counting(), Counting()
    build(serial)
    build(fast.prefetch(quick, fast.communities_plan, "NG"))
    assert len(quick.calls) <= len(serial.calls)
    assert quick.calls.count("suppressed_creators") == serial.calls.count("suppressed_creators")


def test_the_community_route_reads_together_and_answers_as_before(slow_app):
    client, stores = slow_app
    community = _a_community("NG")
    serial = Counting()
    expected, _ = _rounds(lambda s: people.build_community(s, community, "NG"), serial)
    r = client.get(f"/api/communities/{community}?market=NG", headers={"X-Passcode": PASS})
    assert r.status_code == 200, r.text
    assert r.json() == _json(expected)
    assert len(stores[-1].calls) <= len(serial.calls)
    assert stores[-1].depth < len(serial.calls) * 0.7, (stores[-1].depth, len(serial.calls))
    r = client.get(f"/api/communities/{community}", headers={"X-Passcode": PASS})
    assert r.status_code == 200 and r.json()["community"]["community_id"] == community


def test_history_search_reads_every_market_together():
    class Waves(Counting):
        """Waves in three markets, so the search reads three markets' days."""

        def __getattr__(self, name):
            if name == "waves":
                real = super().__getattr__(name)

                def waves(ids, market):
                    rows = real(ids, market) or []
                    return [dict(w, market=m) for w in rows[:1] for m in ("ZA", "NG", "KE")]
                return waves
            return super().__getattr__(name)

    plain, one_by_one = _rounds(lambda s: history.build_history_search(s, "fixture_za", None), Waves())
    quick = Waves()
    body, together = _rounds(lambda s: history.build_history_search(
        fast.prefetch(s, fast.history_search_plan, "fixture_za", None), "fixture_za", None), quick)
    assert body == plain and len({w["market"] for i in body["items"] for w in i["waves"] or []}) == 3
    assert quick.calls.count("item_days") == 3
    assert one_by_one >= 5
    assert together < 3.8, (together, one_by_one)


def test_the_seed_path_route_reads_together_and_answers_as_before(slow_app):
    from core.api import seedpath

    client, stores = slow_app
    serial = Counting()
    expected, _ = _rounds(lambda s: seedpath.build_seed_path(s, "amapiano", "ZA"), serial)
    r = client.get("/api/seed-path?keyword=amapiano&market=ZA", headers={"X-Passcode": PASS})
    assert r.status_code == 200, r.text
    assert r.json() == _json(expected)
    assert sorted(stores[-1].calls) == sorted(serial.calls)
    assert stores[-1].depth < 4.5, stores[-1].depth


def test_the_alerts_route_reads_the_run_while_the_watch_list_comes(slow_app, monkeypatch):
    import asyncio

    import httpx

    from core.api import app as api_mod

    client, _ = slow_app
    reading, stores, waited = threading.Event(), [], []

    class Signalling(Counting):
        def __getattr__(self, name):
            value = super().__getattr__(name)
            if not callable(value) or name.startswith("_"):
                return value

            def read(*args, **kwargs):
                reading.set()
                return value(*args, **kwargs)
            return read

    monkeypatch.setattr(store_mod, "get_store", lambda: stores.append(Signalling()) or stores[-1])

    async def watches(method, path, json_body=None, params=None):
        # The watch list answers only once the run's reads have begun (one after the other, they never would).
        waited.append(await asyncio.to_thread(reading.wait, 10))
        return httpx.Response(200, json={"watches": []})

    # The plan runs on one thread and the page on another: the page starts where the plan's reads left it.
    real_prefetch, real_build = fast.prefetch, discover.build_alerts

    def prefetch(*args):
        started = real_prefetch(*args)
        started.plan_round = _ROUND.get()
        return started

    def build_alerts(store, *args, **kwargs):
        _ROUND.set(_later(_ROUND.get(), store.plan_round))
        return real_build(store, *args, **kwargs)

    monkeypatch.setattr(fast, "prefetch", prefetch)
    monkeypatch.setattr(discover, "build_alerts", build_alerts)
    monkeypatch.setattr(api_mod, "_forward", watches)
    expected, _ = _rounds(lambda s: real_build(s, [], date=None), Counting())
    r = client.get("/api/alerts", headers={"X-Passcode": PASS})
    assert r.status_code == 200, r.text
    assert r.json() == _json(expected)
    # The run's reads (three rounds) went while the watch list was still coming: together, not one after the other.
    assert waited == [True]
    assert stores[-1].depth <= 3, stores[-1].depth


def test_the_alerts_route_still_refuses_when_the_watch_list_cannot_be_read(slow_app, monkeypatch):
    import httpx

    from core.api import app as api_mod

    client, _ = slow_app

    async def broken(method, path, json_body=None, params=None):
        return httpx.Response(503, json={})

    monkeypatch.setattr(api_mod, "_forward", broken)
    r = client.get("/api/alerts", headers={"X-Passcode": PASS})
    assert r.status_code == 502 and r.json()["error"] == "agent_unavailable"


def test_health_runs_its_checks_together_and_answers_as_before(monkeypatch):
    import asyncio

    from core.api import app as api_mod

    monkeypatch.delenv("MODEL_DAILY_USD", raising=False)
    # Each independent check waits for the other two to be under way: one after another, the first would wait in vain.
    barrier, met = threading.Barrier(3), []

    def meet():
        try:
            barrier.wait(10)
            met.append(True)
        except threading.BrokenBarrierError:
            met.append(False)

    def slow(value):
        def check():
            meet()
            return value
        return check

    async def agent():
        await asyncio.to_thread(meet)
        return "ok", True

    monkeypatch.setattr(api_mod, "_bigquery_check", slow("ok"))
    monkeypatch.setattr(api_mod, "_today_check", slow("published"))
    monkeypatch.setattr(api_mod, "_agent_check", agent)
    client = TestClient(api_mod.app)
    body = client.get("/api/health").json()
    assert body["checks"] == {"auth": body["checks"]["auth"], "bigquery": "ok", "agent": "ok", "today": "published"}
    assert body["t2_ready"] is True
    assert met == [True] * 3  # all three independent reads were under way at once

    async def down():
        return "unreachable", True

    monkeypatch.setattr(api_mod, "_bigquery_check", lambda: "ok")
    monkeypatch.setattr(api_mod, "_today_check", lambda: "published")
    monkeypatch.setattr(api_mod, "_agent_check", down)
    body = client.get("/api/health").json()
    assert body["ok"] is False and body["t2_ready"] is False  # t2 follows the agent check, as before


def test_bigquery_reads_the_sensitive_sources_together():
    from core.api.tests.test_people import catalog

    barrier, met = threading.Barrier(3), []

    class Client:
        """Answers a source only once all three are being read (one after another, the first would wait in vain),
        each source with its own item."""

        def __init__(self):
            self.calls = []

        def query(self, sql, job_config=None):
            self.calls.append(sql)
            if "INFORMATION_SCHEMA" in sql:
                rows = catalog("v_sensitive_items", "v_sensitive_items_complete", map_columns=("sensitive",))
            else:
                try:
                    barrier.wait(10)
                    met.append(True)
                except threading.BrokenBarrierError:
                    met.append(False)
                rows = [{"item_id": "c" if "complete" in sql else "m" if "cm.sensitive" in sql else "k"}]
            return mock.Mock(result=lambda: rows)

    client = Client()
    assert BigQueryStore(client=client).sensitive_items() == {"c", "k", "m"}
    assert met == [True] * 3  # the three sources were read at once
    assert len([s for s in client.calls if "INFORMATION_SCHEMA" not in s]) == 3


def test_bigquery_seed_path_groups_the_sightings_of_matching_posts_only():
    from core.api.tests.test_seedpath import CATALOG, FakeClient

    client = FakeClient(CATALOG)
    BigQueryStore(client=client).seed_path_posts("ZA", ["amapiano"], "2026-09-03", "2026-09-30", 5001)
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert sql.index("matched AS") < sql.index("sight AS")
    assert "po.post_id IN (SELECT o.post_id FROM matched o)" in sql
    assert sql.count("REGEXP_CONTAINS(IFNULL(p.text, ''), @pattern)") == 1


def test_the_bigquery_client_keeps_a_connection_for_each_read_started_together():
    import requests

    bq = BigQueryStore(client=real_client())
    adapter = bq.client._http.get_adapter("https://")
    assert isinstance(adapter, requests.adapters.HTTPAdapter)
    assert adapter._pool_maxsize >= fast.WORKERS
    client = mock.MagicMock()
    BigQueryStore(client=client)
    assert not client._http.mount.called  # a stand-in client is left alone


def test_reach_cache_reuses_the_same_item_set_in_a_different_order():
    store = Counting()
    run = FixtureStore().latest_detect_run()
    ids = [STEP, HER]
    before = list(ids)
    first = discover._reach(fast.reads(store), run, "ZA", ids)
    second = discover._reach(fast.reads(store), run, "ZA", list(reversed(ids)))
    assert first == second
    assert ids == before
    assert store.calls.count("item_reach") == 1


def test_all_market_reach_reads_are_prefetched_in_one_round():
    cards = [{"item_id": STEP, "market": "ZA"}, {"item_id": HER, "market": "NG"},
             {"item_id": STEP, "market": "KE"}]
    run = FixtureStore().latest_detect_run()
    expected = discover._with_reach7(FixtureStore(), run, copy.deepcopy(cards))
    store = Counting()
    started = fast.prefetch(store, lambda p: None)
    got, depth = _rounds(lambda s: discover._with_reach7(s, run, copy.deepcopy(cards)), started)
    assert got == expected
    assert [(c["item_id"], c["market"]) for c in got] == [(STEP, "ZA"), (HER, "NG"), (STEP, "KE")]
    assert store.calls.count("item_reach") == 3
    assert depth == 1
    assert started.unused() == []


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("result", [None, [{"item_id": "fixture", "posts": [1]}]])
def test_concurrent_pipeline_misses_share_one_result_or_error_and_retry_after_failure(monkeypatch, fails, result):
    entered, release, both_read = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()

    class Store:
        calls = 0

        def item_waves(self, item_id, market):
            with lock:
                self.calls += 1
                attempt = self.calls
            entered.set()
            if not release.wait(3):
                raise AssertionError("test read was not released")
            if fails and attempt == 1:
                raise ValueError("fixture read failed")
            return copy.deepcopy(result)

    store = Store()
    cached = fast.reads(store)
    cache = fast._cache_of(store)
    get = cache.get
    observed = [0]

    def observe(key):
        hit = get(key)
        with lock:
            observed[0] += 1
            if observed[0] == 2:
                both_read.set()
        return hit

    monkeypatch.setattr(cache, "get", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cached.item_waves, "fixture", "ZA")
        try:
            assert entered.wait(3)
            second = pool.submit(cached.item_waves, "fixture", "ZA")
            assert both_read.wait(3)
        finally:
            release.set()
        if fails:
            for future in (first, second):
                with pytest.raises(ValueError, match="fixture read failed"):
                    future.result(timeout=3)
        else:
            one, two = first.result(timeout=3), second.result(timeout=3)
            assert one == two == result
            if one is not None:
                one[0]["posts"].append(2)
                assert two[0]["posts"] == [1]
    assert store.calls == 1
    assert cached.item_waves("fixture", "ZA") == result
    assert store.calls == (2 if fails else 1)


def test_shared_pipeline_result_expires_from_completion_time(monkeypatch):
    now = [1000.0]
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(fast, "clock", lambda: now[0])

    class Store:
        calls = 0

        def item_waves(self, item_id, market):
            self.calls += 1
            entered.set()
            if not release.wait(3):
                raise AssertionError("test read was not released")
            return [{"attempt": self.calls}]

    store = Store()
    cached = fast.reads(store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cached.item_waves, "fixture", "ZA")
        try:
            assert entered.wait(3)
            now[0] = 5000.0
        finally:
            release.set()
        assert future.result(timeout=3) == [{"attempt": 1}]
    now[0] += fast.CACHE_TTL
    assert cached.item_waves("fixture", "ZA") == [{"attempt": 1}]
    now[0] += 0.1
    assert cached.item_waves("fixture", "ZA") == [{"attempt": 2}]


def test_concurrent_suppression_reads_stay_fresh():
    together = threading.Barrier(2)
    lock = threading.Lock()

    class Store:
        calls = 0

        def suppressed_creators(self):
            with lock:
                self.calls += 1
            together.wait(timeout=3)
            return {"fixture"}

    store = Store()
    cached = fast.reads(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cached.suppressed_creators) for _ in range(2)]
        assert [f.result(timeout=3) for f in futures] == [{"fixture"}, {"fixture"}]
    assert store.calls == 2


def test_different_pipeline_keys_can_read_together():
    together = threading.Barrier(2)
    calls = []
    lock = threading.Lock()

    class Store:
        def item_waves(self, item_id, market):
            with lock:
                calls.append((item_id, market))
            together.wait(timeout=3)
            return [{"market": market}]

    store = Store()
    cached = fast.reads(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cached.item_waves, "fixture", market) for market in ("ZA", "NG")]
        assert [f.result(timeout=3) for f in futures] == [[{"market": "ZA"}], [{"market": "NG"}]]
    assert sorted(calls) == [("fixture", "NG"), ("fixture", "ZA")]
