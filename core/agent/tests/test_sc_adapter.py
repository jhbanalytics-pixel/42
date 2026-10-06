import sys
import threading
import time
import types
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from core.agent.context import Refused
from core.agent.tools import sc_adapter
from core.agent.tools.sc_adapter import L1Adapter, make_client

# The full status set of core/collect/socialcrawl_client.py STATUSES on origin/full-42-l1.
L1_STATUSES = ("ok", "empty", "refunded", "cached", "not_in_replay", "cap_reached",
               "forbidden", "balance_floor", "insufficient_credits", "error")
AGENT_STATUSES = ("ok", "empty", "partial", "rate_limited", "auth_failed", "schema_drift", "not_in_replay",
                  "cap_reached", "error")
ITEM = {"id": "7412", "handle": "kasi_eats", "text": "amapiano at the braai"}


class L1Refused(Exception):
    pass


@dataclass
class Result:
    status: str
    route: str
    params_hash: str | None = None
    http_status: int | None = None
    credits_quoted: float = 0
    credits_charged: float = 0
    cache_hit: bool = False
    body: dict | None = None
    items: list = field(default_factory=list)
    vendor_labels: list = field(default_factory=list)
    reason: str = ""


class FakeL1Client:
    def __init__(self, result=None, delay=0.0, **kwargs):
        self.result = result
        self.delay = delay
        self.kwargs = kwargs
        self.run_id = kwargs.get("run_id")
        self.calls = []

    def call(self, route, params=None, **kwargs):
        self.calls.append((route, dict(params or {}), kwargs))
        if self.delay:
            time.sleep(self.delay)
        return self.result


def _quote_for(route, method, params):
    if route == "tiktok/search/top" and method == "GET":
        return 1 * int(params.get("max_pages", 1))
    if route in ("tiktok/post/comments", "tiktok/post/transcript", "youtube/video/transcript") and method == "GET":
        return 1
    raise L1Refused("not a priced 42 route")


# A copy of L1's _item_lists and _is_empty on origin/full-42-l1.
ITEM_KEYS = ("items", "posts", "results", "videos", "articles", "data")


def _item_lists(node):
    if isinstance(node, list):
        return [node]
    lists = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ITEM_KEYS and isinstance(value, list):
                lists.append(value)
            elif isinstance(value, dict):
                lists.extend(_item_lists(value))
    return lists


def _is_empty(body):
    data = body.get("data") if isinstance(body, dict) else None
    if not data:
        return True
    lists = _item_lists(data)
    return bool(lists) and not any(lists)


def _requests_http(*args, **kwargs):
    raise AssertionError("no HTTP in tests")


@pytest.fixture
def l1(monkeypatch):
    """A fake core.collect.socialcrawl_client and core.collect.stores, injected through sys.modules."""
    package = types.ModuleType("core.collect")
    package.__path__ = []
    client_mod = types.ModuleType("core.collect.socialcrawl_client")
    client_mod.Result = Result
    client_mod.Refused = L1Refused
    client_mod.quote_for = _quote_for
    client_mod._is_empty = _is_empty
    client_mod.SocialCrawlClient = FakeL1Client
    client_mod.requests_http = _requests_http
    stores = types.ModuleType("core.collect.stores")

    class BigQueryLedgerStore:
        def __init__(self, client, project):
            self.client, self.project = client, project

    class BigQueryRawStore(BigQueryLedgerStore):
        pass

    stores.BigQueryLedgerStore = BigQueryLedgerStore
    stores.BigQueryRawStore = BigQueryRawStore
    monkeypatch.setitem(sys.modules, "core.collect", package)
    monkeypatch.setitem(sys.modules, "core.collect.socialcrawl_client", client_mod)
    monkeypatch.setitem(sys.modules, "core.collect.stores", stores)
    return client_mod


def _adapter(result, lock=None, delay=0.0):
    client = FakeL1Client(result, delay=delay, run_id="ask-1")
    return L1Adapter(client, lock=lock or threading.Lock()), client


def _call(adapter, route="tiktok/search/top", params=None, max_credits=10):
    return adapter.call(route, params or {"query": "amapiano"}, lane="agent_live", run_id="ask-1",
                        max_credits=max_credits)


# Status mapping ------------------------------------------------------------------------------------


@pytest.mark.parametrize("l1_status, items, http_status, expected", [
    ("ok", [ITEM], 200, "ok"),
    ("ok", [], 200, "schema_drift"),  # L1 says ok only when data had content, so no items is drift
    ("cached", [ITEM], 200, "ok"),
    ("cached", [], 200, "empty"),
    ("empty", [], 200, "empty"),
    ("not_in_replay", [], None, "not_in_replay"),
    ("cap_reached", [], None, "cap_reached"),
    ("balance_floor", [], None, "cap_reached"),
    ("insufficient_credits", [], 402, "cap_reached"),
    ("refunded", [], 503, "error"),
    ("error", [], 401, "auth_failed"),
    ("error", [], 403, "auth_failed"),
    ("error", [], 429, "rate_limited"),
    ("error", [], 500, "error"),
    ("error", [], None, "error"),
])
def test_status_mapping(l1, l1_status, items, http_status, expected):
    adapter, _ = _adapter(Result(l1_status, "tiktok/search/top", http_status=http_status, items=items,
                                 reason="why"))
    out = _call(adapter)
    assert out["status"] == expected
    assert out["status"] in AGENT_STATUSES


def test_ok_with_no_items_is_schema_drift_with_a_reason(l1):
    body = {"data": {"comments": [{"id": "c1", "text": "hi"}]}}  # a list under a key L1 does not read
    adapter, _ = _adapter(Result("ok", "tiktok/search/top", http_status=200, body=body))
    out = _call(adapter)
    assert out["status"] == "schema_drift"
    assert out["items"] == [] and "shape" in out["reason"]


@pytest.mark.parametrize("body, expected", [
    ({"data": {"items": []}}, "empty"),
    ({"data": {}}, "empty"),
    (None, "empty"),
    ({"data": {"comments": [{"id": "c1"}]}}, "schema_drift"),
])
def test_cached_with_no_items_is_empty_only_when_l1_would_call_it_empty(l1, body, expected):
    adapter, _ = _adapter(Result("cached", "tiktok/search/top", http_status=200, cache_hit=True, body=body))
    assert _call(adapter)["status"] == expected


def _drift_adapter():
    body = {"data": {"comments": [{"id": "c1", "text": "hi"}]}}
    return _adapter(Result("ok", "tiktok/search/top", http_status=200, credits_charged=1, body=body))[0]


def _post_ctx():
    from core.agent.context import RunContext

    c = RunContext(run_id="ask-1", tier="T1", as_of=datetime.fromisoformat("2026-09-28T06:10:00+02:00"),
                   market="ZA")
    c.evidence["tiktok_7412"] = {"id": "tiktok_7412", "platform": "tiktok", "handle": "chef_za", "market": "ZA",
                                 "url": "https://www.tiktok.com/@chef_za/video/7412", "text": "Braai day"}
    return c


def test_drift_through_the_adapter_reaches_socialcrawl_call(l1):
    from core.agent.tools.socialcrawl import socialcrawl_call

    c = _post_ctx()
    out = socialcrawl_call(c, _drift_adapter(), "tiktok", "search/top", {"query": "braai"}, max_credits=5)
    assert out["status"] == "schema_drift" and out["reason"]
    assert out["evidence_ids"] == []
    assert c.events[-1]["status"] == "schema_drift"


def test_drift_through_the_adapter_reaches_get_comments(l1):
    from core.agent.tools.enrich_tools import get_comments

    c = _post_ctx()
    out = get_comments(c, _drift_adapter(), "tiktok_7412", limit=5, max_credits=5)
    assert out["status"] == "schema_drift" and out["reason"]
    assert out["evidence_ids"] == []


def test_drift_through_the_adapter_reaches_get_transcript_and_is_not_cached(l1):
    from core.agent.tools.enrich_tools import get_transcript

    c = _post_ctx()
    out = get_transcript(c, _drift_adapter(), "tiktok_7412")
    assert out["status"] == "schema_drift" and out["reason"]
    assert out["segments"] == [] and "tiktok_7412" not in c.transcripts


def test_every_l1_status_is_mapped_or_refused(l1):
    for status in L1_STATUSES:
        adapter, _ = _adapter(Result(status, "tiktok/search/top", items=[ITEM]))
        if status == "forbidden":
            with pytest.raises(Refused):
                _call(adapter)
        else:
            assert _call(adapter)["status"] in AGENT_STATUSES


def test_unknown_l1_status_is_an_error_not_ok(l1):
    adapter, _ = _adapter(Result("something_new", "tiktok/search/top", items=[ITEM]))
    assert _call(adapter)["status"] == "error"


def test_forbidden_raises_refused_with_the_reason(l1):
    adapter, _ = _adapter(Result("forbidden", "tiktok/search/top", reason="unpriced parameter: foo"))
    with pytest.raises(Refused, match="unpriced parameter: foo"):
        _call(adapter)


# What is passed through ----------------------------------------------------------------------------


def test_items_credits_and_cache_hit_pass_through(l1):
    items = [ITEM, {**ITEM, "id": "7413"}]
    adapter, _ = _adapter(Result("ok", "tiktok/search/top", http_status=200, credits_charged=2, items=items))
    out = _call(adapter)
    assert out["items"] == items
    assert out["credits_charged"] == 2.0
    assert out["cache_hit"] is False

    adapter, _ = _adapter(Result("cached", "tiktok/search/top", http_status=200, cache_hit=True, items=items))
    out = _call(adapter)
    assert out["items"] == items
    assert out["credits_charged"] == 0.0
    assert out["cache_hit"] is True


def test_call_passes_route_params_lane_and_market(l1):
    adapter, client = _adapter(Result("ok", "tiktok/search/top", items=[ITEM]))
    _call(adapter, params={"query": "amapiano", "country": "South Africa"})
    route, params, kwargs = client.calls[0]
    assert route == "tiktok/search/top"
    assert params == {"query": "amapiano", "country": "South Africa"}
    assert kwargs["lane"] == "agent_live"
    assert kwargs["market"] == "ZA"


def test_market_is_none_when_params_name_none(l1):
    adapter, client = _adapter(Result("ok", "tiktok/search/top", items=[ITEM]))
    _call(adapter)
    assert client.calls[0][2]["market"] is None


def test_reason_is_kept_without_urls(l1):
    reason = "HTTP 500: upstream https://www.socialcrawl.dev/v1/tiktok/search/top?query=x failed, see www.example.com/x"
    adapter, _ = _adapter(Result("error", "tiktok/search/top", http_status=500, reason=reason))
    out = _call(adapter)
    assert "HTTP 500: upstream" in out["reason"]
    assert "failed" in out["reason"]
    assert "socialcrawl.dev" not in out["reason"]
    assert "http" not in out["reason"].replace("HTTP", "")
    assert "www." not in out["reason"]


@pytest.mark.parametrize("body, expected", [
    ({"data": {"items": [ITEM]}, "pagination": {"next_cursor": "c2", "has_more": True}}, "c2"),
    ({"data": {"items": [ITEM], "pagination": {"next_cursor": "c3", "has_more": True}}}, "c3"),
    ({"data": {"items": [ITEM]}, "pagination": {"next_cursor": "c2", "has_more": False}}, None),
    ({"data": {"items": [ITEM]}, "pagination": {"next_cursor": ""}}, None),
    ({"data": {"items": [ITEM]}}, None),
    (None, None),
])
def test_next_cursor_from_body_pagination(l1, body, expected):
    adapter, _ = _adapter(Result("ok", "tiktok/search/top", http_status=200, body=body, items=[ITEM]))
    assert _call(adapter)["next_cursor"] == expected


# Quote and max_credits ------------------------------------------------------------------------------


def test_quote_uses_l1_quote_for_with_get(l1):
    adapter, _ = _adapter(Result("ok", "tiktok/search/top"))
    assert adapter.quote("tiktok/search/top", {"query": "x", "max_pages": 3}) == 3.0


def test_quote_turns_l1_refusal_into_agent_refused(l1):
    adapter, _ = _adapter(Result("ok", "tiktok/search"))
    with pytest.raises(Refused, match="not a priced 42 route"):
        adapter.quote("tiktok/search", {"query": "x"})


def test_call_over_max_credits_is_refused_before_the_client(l1):
    adapter, client = _adapter(Result("ok", "tiktok/search/top", items=[ITEM]))
    with pytest.raises(Refused, match="max_credits"):
        _call(adapter, params={"query": "x", "max_pages": 5}, max_credits=4)
    assert client.calls == []


# The lock --------------------------------------------------------------------------------------------


class _Overlap:
    def __init__(self):
        self.guard = threading.Lock()
        self.active = 0
        self.most = 0
        self.spans = []


def _run_two(first, second, overlap):
    barrier = threading.Barrier(2)

    def worker(adapter):
        barrier.wait()
        start = time.monotonic()
        _call(adapter)
        overlap.spans.append((start, time.monotonic()))

    threads = [threading.Thread(target=worker, args=(a,)) for a in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)


def _tracking_adapter(overlap, lock):
    class Tracking(FakeL1Client):
        def call(self, route, params=None, **kwargs):
            with overlap.guard:
                overlap.active += 1
                overlap.most = max(overlap.most, overlap.active)
            time.sleep(0.15)
            with overlap.guard:
                overlap.active -= 1
            return Result("ok", route, items=[ITEM])

    return L1Adapter(Tracking(run_id="ask"), lock=lock)


def test_two_threads_on_the_shared_lock_never_overlap(l1):
    overlap = _Overlap()
    _run_two(_tracking_adapter(overlap, sc_adapter.LOCK), _tracking_adapter(overlap, sc_adapter.LOCK), overlap)
    assert overlap.most == 1
    assert len(overlap.spans) == 2
    # The second caller waited for the first: together they took at least two sleeps.
    assert max(end for _, end in overlap.spans) - min(start for start, _ in overlap.spans) >= 0.28


def test_without_a_shared_lock_the_calls_do_overlap(l1):
    """Proves the overlap probe can fail, so the test above is not vacuous."""
    overlap = _Overlap()
    _run_two(_tracking_adapter(overlap, threading.Lock()), _tracking_adapter(overlap, threading.Lock()), overlap)
    assert overlap.most == 2


def test_lock_is_released_when_the_client_raises(l1):
    class Boom(FakeL1Client):
        def call(self, route, params=None, **kwargs):
            raise RuntimeError("credit_ledger append failed")

    lock = threading.Lock()
    adapter = L1Adapter(Boom(run_id="ask"), lock=lock)
    with pytest.raises(RuntimeError):
        _call(adapter)
    assert not lock.locked()


# make_client -----------------------------------------------------------------------------------------


def test_make_client_returns_none_when_core_collect_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.collect", None)
    monkeypatch.delitem(sys.modules, "core.collect.socialcrawl_client", raising=False)
    monkeypatch.delitem(sys.modules, "core.collect.stores", raising=False)
    assert make_client("live", run_id="ask-1") is None


def test_make_client_raises_on_an_unrelated_import_error(l1, monkeypatch):
    """A missing dependency after the merge is a broken image, not "not connected yet"."""
    import google.cloud

    monkeypatch.delattr(google.cloud, "bigquery", raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)
    with pytest.raises(ImportError):
        make_client("live", run_id="ask-1")


def test_make_client_raises_when_l1_is_present_but_its_dependency_is_not(l1, monkeypatch):
    class NoYaml:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "core.collect.socialcrawl_client":
                raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
            return None

    monkeypatch.delitem(sys.modules, "core.collect.socialcrawl_client")
    monkeypatch.setattr(sys, "meta_path", [NoYaml(), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError, match="yaml"):
        make_client("live", run_id="ask-1")


def test_make_client_builds_the_ask_share_on_l1_stores(l1, monkeypatch):
    from google.cloud import bigquery

    built = []

    class FakeBQ:
        def __init__(self, project):
            built.append(project)

    monkeypatch.setattr(bigquery, "Client", FakeBQ)
    adapter = make_client("replay", run_id="ask-7")
    assert isinstance(adapter, L1Adapter)
    assert adapter.lock is sc_adapter.LOCK
    kwargs = adapter.client.kwargs
    assert kwargs["share"] == "ask"
    assert kwargs["run_id"] == "ask-7"
    assert kwargs["mode"] == "replay"
    assert type(kwargs["ledger"]).__name__ == "BigQueryLedgerStore"
    assert type(kwargs["raw"]).__name__ == "BigQueryRawStore"
    assert kwargs["ledger"].project == kwargs["raw"].project == "ogilvy-trends-v2"
    assert kwargs["http"] is l1.requests_http
    assert kwargs["clock"]().tzinfo is not None
    assert isinstance(kwargs["clock"](), datetime)
    assert built == ["ogilvy-trends-v2"]


# Task 1.17: the YouTube transcript arrives as data.transcript, a key L1's ITEM_KEYS does not read. The agent path
# reads it for transcript routes only; L1's collect-wide parsing is unchanged.

TRANSCRIPT_ROWS = [
    {"duration": 3.5, "lang": "en", "offset": 0.0, "speechRate": 2, "text": "Sawubona, welcome back", "wordCount": 3},
    {"duration": 2.25, "lang": "en", "offset": 3.5, "speechRate": 2, "text": "Today we braai", "wordCount": 3},
]


def _transcript_body(rows):
    return {"cached": False, "credits_used": 10, "success": True, "endpoint": "youtube/video/transcript",
            "data": {"transcript": rows}}


@pytest.mark.parametrize("l1_status", ["ok", "cached"])
def test_a_transcript_route_reads_data_transcript_when_l1_found_no_items(l1, l1_status):
    adapter, _ = _adapter(Result(l1_status, "youtube/video/transcript", http_status=200, credits_charged=10,
                                 body=_transcript_body(TRANSCRIPT_ROWS)))
    out = _call(adapter, route="youtube/video/transcript", params={"url": "https://www.youtube.com/watch?v=dQw4"})
    assert out["status"] == "ok" and out["reason"] == ""
    assert out["items"] == TRANSCRIPT_ROWS


def test_an_empty_data_transcript_is_empty_not_drift(l1):
    adapter, _ = _adapter(Result("cached", "youtube/video/transcript", http_status=200, cache_hit=True,
                                 body=_transcript_body([])))
    out = _call(adapter, route="youtube/video/transcript", params={"url": "https://www.youtube.com/watch?v=dQw4"})
    assert out["status"] == "empty" and out["items"] == []


def test_data_transcript_is_not_read_on_a_route_that_is_not_a_transcript(l1):
    adapter, _ = _adapter(Result("ok", "tiktok/search/top", http_status=200, body=_transcript_body(TRANSCRIPT_ROWS)))
    out = _call(adapter)
    assert out["status"] == "schema_drift" and out["items"] == []


def test_data_transcript_is_not_read_on_a_failed_transcript_call(l1):
    adapter, _ = _adapter(Result("error", "youtube/video/transcript", http_status=500,
                                 body=_transcript_body(TRANSCRIPT_ROWS)))
    out = _call(adapter, route="youtube/video/transcript", params={"url": "https://www.youtube.com/watch?v=dQw4"})
    assert out["status"] == "error" and out["items"] == []


@pytest.mark.parametrize("transcript", [None, "Sawubona", {"text": "hi"}, 5])
def test_a_data_transcript_that_is_not_a_list_stays_drift(l1, transcript):
    adapter, _ = _adapter(Result("ok", "youtube/video/transcript", http_status=200,
                                 body={"data": {"transcript": transcript, "other": "x"}}))
    out = _call(adapter, route="youtube/video/transcript", params={"url": "https://www.youtube.com/watch?v=dQw4"})
    assert out["status"] == "schema_drift" and out["items"] == []


def test_the_youtube_transcript_body_reaches_get_transcript_as_segments(l1):
    from core.agent.tools.enrich_tools import get_transcript

    c = _post_ctx()
    c.evidence["youtube_dQw4"] = {"id": "youtube_dQw4", "platform": "youtube", "handle": "kasi_eats", "market": "ZA",
                                  "url": "https://www.youtube.com/watch?v=dQw4", "text": "Braai day"}
    adapter, _ = _adapter(Result("ok", "youtube/video/transcript", http_status=200, credits_charged=10,
                                 body=_transcript_body(TRANSCRIPT_ROWS)))
    out = get_transcript(c, adapter, "youtube_dQw4")
    assert out["status"] == "ok"
    assert c.transcripts["youtube_dQw4"]["segments"] == [
        {"start_s": 0.0, "end_s": 3.5, "text": "Sawubona, welcome back"},
        {"start_s": 3.5, "end_s": 5.75, "text": "Today we braai"},
    ]
