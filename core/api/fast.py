"""Faster reads for the slow pages (Today, Discover, Radar, topics, Alerts, Compare, History, Communities).

Each page used to make its BigQuery reads one after another, 9 to 17 of them, a few seconds each. Two things here
make them quicker without changing what any read returns:

reads(store) is the store a page reads through. A pipeline read (the run's items, gate, briefs, a topic's series
and the like, written only by the daily jobs) is kept for CACHE_TTL seconds per process, so moving between pages
reads it once. The suppression list, finished asks, findings and anything not named in CACHED are always read
fresh, so a hide applies on the next page load as before. A cached read hands out a copy, so no page can change
what the next one sees. Concurrent identical pipeline reads share one result or failure; a failed read is tried
again on the next call.

prefetch(store, plan, ...) starts the reads a page is about to make together on a small thread pool, in the
phases their arguments allow (the latest run first, then everything that needs only the run). When the page then
makes the same call with the same arguments it gets that result; any other call goes to the store as before. A
plan never fails a page: a read the store does not have is skipped, a failed read raises only when the page itself
makes that call, and a plan that cannot run leaves the page to read one by one.
"""
import collections
import copy
import datetime as dt
import logging
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor

log = logging.getLogger("f42.api.fast")

CACHE_TTL = 300  # seconds; the jobs write these at most a few times a day, so a 5-minute lag is harmless
CACHE_MAX = 128  # entries per store, oldest dropped first
WORKERS = 16  # a topic page starts 13 reads at once; store.BigQueryStore keeps HTTP_POOL connections open
MARKETS = ("ZA", "NG", "KE")
CACHED = frozenset({
    "latest_detect_run", "item_states", "item_spread", "item_gate", "briefs", "latest_brief_date", "previous_brief",
    "first_ok_collect_date", "collection_health", "calendar", "runs", "search_signals", "item_history", "item_waves",
    "coord_signals", "item_evidence", "item_series", "item_origin", "news_followthrough", "item_reach",
    "health_days", "watch_matches", "detect_states", "sensitive_items", "generic_items", "board_items",
    "community_edges", "creators_by_handle", "creators_by_id", "creator_names", "map_items", "waves", "item_days",
    "nearest_items", "search_items", "compare_counts", "latest_aggregate_run", "health_range",
})  # not here: suppressed_creators and hidden_people_rows, which are read fresh on every request

_POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="f42-read")
_CACHES = weakref.WeakKeyDictionary()  # inner store -> its _Cache
_CACHES_LOCK = threading.Lock()
clock = time.monotonic


def _key(name, args, kwargs):
    return (name, _freeze(args), _freeze(sorted(kwargs.items())))


def _freeze(v):
    if isinstance(v, dict):
        return ("d", tuple(sorted((str(k), _freeze(x)) for k, x in v.items())))
    if isinstance(v, (set, frozenset)):
        return ("s", tuple(sorted((_freeze(x) for x in v), key=repr)))
    if isinstance(v, (list, tuple)):
        return ("l", tuple(_freeze(x) for x in v))
    return v


class _Cache:
    def __init__(self):
        self.lock, self.rows = threading.Lock(), collections.OrderedDict()
        self.pending = {}

    def get(self, key):
        with self.lock:
            hit = self.rows.get(key)
            if hit is not None:
                if clock() - hit[0] <= CACHE_TTL:
                    return hit, None, False
                del self.rows[key]
            future = self.pending.get(key)
            owner = future is None
            if owner:
                future = self.pending[key] = Future()
            return None, future, owner

    def put(self, key, value):
        with self.lock:
            self.rows[key] = (clock(), value)
            self.rows.move_to_end(key)
            while len(self.rows) > CACHE_MAX:
                self.rows.popitem(last=False)


def _cache_of(store):
    with _CACHES_LOCK:
        try:
            cache = _CACHES.get(store)
            if cache is None:
                cache = _CACHES[store] = _Cache()
            return cache
        except TypeError:  # a store that cannot be weakly referenced is read without a cache
            return None


class _Cached:
    """The store with CACHED reads kept for CACHE_TTL seconds; every other attribute is the store's own."""

    def __init__(self, store):
        self._store, self._cache = store, _cache_of(store)

    def __getattr__(self, name):
        value = getattr(self._store, name)
        if name not in CACHED or self._cache is None or not callable(value):
            return value

        def read(*args, **kwargs):
            key = _key(name, args, kwargs)
            hit, future, owner = self._cache.get(key)
            if hit is not None:
                return copy.deepcopy(hit[1])
            if not owner:
                return copy.deepcopy(future.result())
            try:
                result = value(*args, **kwargs)
                saved = copy.deepcopy(result)
                self._cache.put(key, saved)
            except BaseException as error:
                future.set_exception(error)
                raise
            else:
                future.set_result(saved)
                return result
            finally:
                with self._cache.lock:
                    self._cache.pending.pop(key, None)

        return read


def reads(store):
    """The store a page reads through: CACHED reads kept for CACHE_TTL seconds per process."""
    return store if isinstance(store, (_Cached, _Prefetched)) else _Cached(store)


class _Prefetched:
    """The store with some calls already started; each started call answers the page's first matching call."""

    def __init__(self, store):
        self._store, self._started, self._lock = store, {}, threading.Lock()

    def start(self, name, *args, **kwargs):
        reader = getattr(self._store, name, None)
        if not callable(reader):
            return
        future = _POOL.submit(reader, *args, **kwargs)
        with self._lock:
            self._started.setdefault(_key(name, args, kwargs), collections.deque()).append(future)

    def peek(self, name, *args, **kwargs):
        """The result of a started call without using it up, or None when it was not started or failed."""
        with self._lock:
            futures = self._started.get(_key(name, args, kwargs))
            future = futures[0] if futures else None
        if future is None:
            return None
        try:
            return future.result()
        except Exception:
            return None

    def unused(self):
        """(name, args) of every started call the page has not used yet."""
        with self._lock:
            return [(k[0], k[1]) for k, futures in self._started.items() for _ in futures]

    def __getattr__(self, name):
        value = getattr(self._store, name)
        if not callable(value):
            return value

        def read(*args, **kwargs):
            key = _key(name, args, kwargs)
            with self._lock:
                futures = self._started.get(key)
                future = futures.popleft() if futures else None
            return future.result() if future is not None else value(*args, **kwargs)

        return read


def start(store, name, *args, **kwargs):
    """Start a read the page is about to make when it reads through a plan (prefetch); on any other store, nothing.
    Pages call it for reads whose arguments they have only just worked out, so two or three of them run together."""
    if isinstance(store, _Prefetched):
        store.start(name, *args, **kwargs)


def prefetch(store, plan, *args):
    """reads(store) with plan's reads started; plan(started, *args) calls started.start and started.peek."""
    started = _Prefetched(reads(store))
    try:
        plan(started, *args)
    except Exception as e:  # the page still reads everything itself, one by one
        log.warning("prefetch %s stopped: %s: %s", getattr(plan, "__name__", plan), type(e).__name__, e)
    return started


# Plans: the reads each page always makes, with the arguments it makes them with (core/api/discover.py, today.py,
# people.py, history.py and compare.py). A read whose arguments depend on rows the page builds is left to the page.

def _days_before(day, n):
    return (dt.date.fromisoformat(day) - dt.timedelta(days=n)).isoformat()


def _hidden_start(p):
    """hidden_people's reads: the suppression list, then the creators rows of the ids on it."""
    p.start("suppressed_creators")


def _hidden_then(p):
    ids = p.peek("suppressed_creators")
    if ids:
        p.start("creators_by_id", sorted(ids))


def _run_cards(p, run, market):
    """discover._cards's reads for one run and market."""
    p.start("item_states", run, market)
    p.start("item_spread", run["run_date"], market)
    p.start("item_gate", market, run["run_date"])
    p.start("briefs", run["run_date"])


def _cards_plan(p, market):
    """discover._run, hidden_people and discover._cards: what Discover, Radar and a topic page all read first."""
    if market not in ("all", *MARKETS):
        return
    p.start("latest_detect_run")
    _hidden_start(p)
    p.start("first_ok_collect_date")
    run = p.peek("latest_detect_run")
    _hidden_then(p)
    if run:
        _run_cards(p, run, market)


def discover_plan(p, market):
    from core.api import searching, today

    _cards_plan(p, market)
    if market in ("all", *MARKETS) and p.peek("suppressed_creators") is not None:
        day = dt.datetime.now(today.SAST).date().isoformat()
        p.start("search_signals", _days_before(day, searching.SEARCH_DAYS - 1), day,
                list(MARKETS) if market == "all" else [market])


def radar_plan(p, market):
    from core.api import discover

    _cards_plan(p, market)
    run = p.peek("latest_detect_run")
    if run and market != "all":
        p.start("health_days", market, _days_before(run["run_date"], discover.REACH_DAYS - 1), run["run_date"])


def topic_plan(p, item_id, market):
    from core.api import discover

    if market not in MARKETS:
        return
    _cards_plan(p, market)
    run = p.peek("latest_detect_run")
    if not run:
        return
    end = run["run_date"]
    p.start("item_history", item_id, market, _days_before(end, discover.HISTORY_DAYS), end)
    p.start("item_waves", item_id, market)
    p.start("coord_signals", item_id, market, end)
    p.start("item_evidence", item_id, market)
    p.start("item_series", item_id, market, discover.SERIES_DAYS)
    p.start("item_spread", end, "all", item_id)
    p.start("item_origin", item_id, market)
    p.start("news_followthrough", item_id, market, end)


def alerts_plan(p, date):
    p.start("latest_detect_run", **({"until": date} if date else {}))
    _hidden_start(p)
    p.start("first_ok_collect_date")
    run = p.peek("latest_detect_run", **({"until": date} if date else {}))
    _hidden_then(p)
    if not run:
        return
    _run_cards(p, run, "all")
    p.start("watch_matches", run)
    until = _days_before(run["run_date"], 1)
    p.start("latest_detect_run", until=until)
    before = p.peek("latest_detect_run", until=until)
    if before:
        p.start("item_states", before, "all")
        p.start("watch_matches", before)


def communities_plan(p, market):
    from core.api import people

    if market not in MARKETS:
        return
    p.start("latest_detect_run")
    _hidden_start(p)
    _hidden_start(p)  # people read the list twice: once for the cards, once for the names
    p.start("first_ok_collect_date")
    p.start("sensitive_items")
    p.start("generic_items")
    run = p.peek("latest_detect_run")
    _hidden_then(p)
    if not run:
        return
    start, end = people._window(run)
    _run_cards(p, run, "all")
    p.start("detect_states", start, end)
    p.start("board_items", market, start, end)


def today_plan(p, date):
    from core.api import searching, today

    now = dt.datetime.now(today.SAST)
    current = date is None or date == now.date().isoformat()
    search_day = now.date().isoformat() if current else date
    _hidden_start(p)
    p.start("first_ok_collect_date")
    if date is None:
        p.start("latest_brief_date")
    if current:
        since = now.replace(second=0, microsecond=0) - dt.timedelta(hours=today.BREAKING_HOURS)
        p.start("breaking_signals", since.isoformat())
    day = date or p.peek("latest_brief_date")
    _hidden_then(p)
    if p.peek("suppressed_creators") is not None:  # searching_now reads nothing while the list cannot be read
        p.start("search_signals", _days_before(search_day, searching.SEARCH_DAYS - 1), search_day,
                list(searching.MARKETS))
    if not day:
        return
    p.start("briefs", day)
    p.start("collection_health", day)
    p.start("calendar", day, (dt.date.fromisoformat(day) + dt.timedelta(days=today.MOMENT_DAYS)).isoformat())
    p.start("runs", "collect", day)
    p.start("runs", "detect", day)
    for market in today.MARKETS:
        p.start("previous_brief", market, day)


def history_search_plan(p, q, market):
    from core.api import history

    q = (q or "").strip()
    if not 2 <= len(q) <= 100 or not (market is None or market in MARKETS):
        return
    p.start("search_items", q, history.SEARCH_LIMIT)
    p.start("latest_detect_run")
    p.start("hidden_people_rows")  # History reads who is hidden through core/api/privacy.py: one read
    items = p.peek("search_items", q, history.SEARCH_LIMIT)
    if items:
        p.start("waves", [i["item_id"] for i in items], market)


def history_item_plan(p, item_id, market):
    from core.api import history

    if market not in MARKETS:
        return
    p.start("latest_detect_run")
    p.start("map_items", [item_id])
    p.start("waves", [item_id], market)
    p.start("nearest_items", item_id, history.NEIGHBOURS)
    p.start("hidden_people_rows")
    waves = p.peek("waves", [item_id], market)
    if waves and p.peek("map_items", [item_id]):
        p.start("item_days", [item_id], market, *history._span(waves))
    if p.peek("map_items", [item_id]):  # the page stops at an item the map does not have
        near = p.peek("nearest_items", item_id, history.NEIGHBOURS)
        if near is not None:
            p.start("waves", [n["item_id"] for n in near], market)


def seed_path_plan(p, keyword, market):
    """seedpath.build_seed_path's reads: the aggregate run, then the matching posts beside the detect run and the
    cards its topics are named from (read only when a matching post links an item; started here beside the posts)."""
    from core.api import seedpath

    try:
        term, market = seedpath._keyword(keyword), seedpath._market(market)
    except seedpath.BadRequest:
        return
    p.start("latest_aggregate_run")
    _hidden_start(p)
    p.start("first_ok_collect_date")
    run = p.peek("latest_aggregate_run")
    if not run:
        return
    end = run["run_date"]
    p.start("seed_path_posts", market, [term], _days_before(end, seedpath.DAYS - 1), end, seedpath.ROW_LIMIT + 1)
    p.start("latest_detect_run", until=end)
    _hidden_then(p)
    detect = p.peek("latest_detect_run", until=end)
    if detect:
        _run_cards(p, detect, market)


def compare_plan(p, mode, items, market, markets, platforms, days):
    from core.api import compare

    if mode not in compare.MODES or days not in compare.DAYS:
        return
    scopes = compare._scopes(mode, compare._split(items), market, markets, platforms)
    p.start("latest_aggregate_run")
    p.start("map_items", list(dict.fromkeys(i for i, _, _ in scopes)))
    _hidden_start(p)
    run = p.peek("latest_aggregate_run")
    _hidden_then(p)
    if not run:
        return
    end = run["run_date"]
    start = _days_before(end, days - 1)
    wanted = {m for _, m, _ in scopes}
    p.start("latest_detect_run", until=end)
    p.start("health_range", start, end, sorted(wanted))
    detect = p.peek("latest_detect_run", until=end)
    if detect and detect["run_date"] == end:
        p.start("first_ok_collect_date")
        _run_cards(p, detect, wanted.pop() if len(wanted) == 1 else "all")
