from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from core.collect import public_feed_collect as collector
from core.collect.public_feed_rows import feed_protocol
from core.public_feeds import reader
from core.public_feeds.catalog import confirmed_feeds


SAMPLES = Path(__file__).resolve().parents[2] / "public_feeds" / "tests" / "samples"
NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
FIXTURE_FILES = {
    "za_enca": "za_enca_20260930.html",
    "ng_turntable_top_100": "ng_turntable_ng_top_100_20260930.html",
    "ke_kbc_english_playlist": "ke_kbc_english_service_playlist_20260930.html",
}
RAW_COLUMNS = {
    "run_id", "job", "market", "route", "params_hash", "lane", "seed_key", "fetched_at",
    "http_status", "credits_quoted", "credits_charged", "cache_hit", "body",
}


class FakeTransport:
    def __init__(self, answers=None, default_page=(302, {}, b""), robots=None):
        self.answers = answers or {}
        self.default_page = default_page
        self.robots = robots or {}
        self.calls = []

    def __call__(self, url, *, timeout, max_bytes):
        self.calls.append((url, timeout, max_bytes))
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return self.robots.get(parts.netloc, (404, {"content-type": "text/plain"}, b""))
        return self.answers.get(url, self.default_page)


def item_id(kind, raw, platform=None):
    if kind != "sound" or not raw or not platform:
        raise ValueError("unsupported item")
    return f"{platform}:{raw.casefold()}"


def _feeds(*feed_ids):
    return tuple(feed for feed in confirmed_feeds() if feed.feed_id in feed_ids)


def _fixture_transport(feeds):
    answers = {}
    for feed in feeds:
        body = (SAMPLES / FIXTURE_FILES[feed.feed_id]).read_bytes()
        answers[feed.url] = (200, {"Content-Type": "text/html; charset=utf-8"}, body)
    return FakeTransport(answers=answers)


def _run(feeds, transport, **kwargs):
    return collector.run(
        date(2026, 9, 30),
        "collect-public-feed-test",
        transport=transport,
        clock=lambda: NOW,
        item_id_fn=item_id,
        **kwargs,
    )


def test_saved_reader_fixtures_flow_through_collector_and_keep_per_feed_counts(monkeypatch):
    feeds = _feeds(*FIXTURE_FILES)
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    transport = _fixture_transport(feeds)

    result = _run(feeds, transport)

    assert len(result["records"]) == len(result["raw_rows"]) == 3
    assert {record["protocol"] for record in result["records"]} == {
        feed_protocol(feed) for feed in feeds
    }
    expected_series = {"za_enca": "news_rss", "ng_turntable_top_100": "board_music_country",
                       "ke_kbc_english_playlist": "radio_playlist"}
    assert all(record["route"] == "public_feed" and record["series"] == expected_series[feed.feed_id]
               for record, feed in zip(result["records"], feeds))
    assert all(record["ok"] and record["calls"] == 1 and record["units_ok"] == 1 for record in result["records"])
    assert {row["seed_key"] for row in result["raw_rows"]} == {feed.feed_id for feed in feeds}
    assert all(set(row) == RAW_COLUMNS for row in result["raw_rows"])
    assert all(row["credits_quoted"] == row["credits_charged"] == 0 for row in result["raw_rows"])
    assert all(row["cache_hit"] is False for row in result["raw_rows"])
    assert all(row["body"]["decoded_count"] > 0 for row in result["raw_rows"])
    assert all(row["body"]["accepted_count"] > 0 for row in result["raw_rows"])
    assert all(row["body"]["accepted_count"] == record["items"]
               for row, record in zip(result["raw_rows"], result["records"]))
    assert all("headers" not in row["body"] and "html" not in row["body"] for row in result["raw_rows"])
    assert len(result["observations"]) > 0
    assert len(result["counters"]) > 0
    assert sum(url.endswith("/robots.txt") for url, _, _ in transport.calls) == 3
    assert sum(url in {feed.url for feed in feeds} for url, _, _ in transport.calls) == 3
    assert all(timeout == reader.TIMEOUT_SECONDS and max_bytes == reader.MAX_RESPONSE_BYTES
               for _, timeout, max_bytes in transport.calls)


def test_all_43_confirmed_feeds_get_one_record_and_one_page_attempt(monkeypatch):
    feeds = confirmed_feeds()
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    transport = FakeTransport()

    result = _run(feeds, transport)

    pages = [url for url, _, _ in transport.calls if not url.endswith("/robots.txt")]
    robots = [url for url, _, _ in transport.calls if url.endswith("/robots.txt")]
    assert len(feeds) == len(result["records"]) == len(result["raw_rows"]) == 43
    assert result["summary"]["feeds_planned"] == 43
    assert result["summary"]["feeds_attempted"] == 43
    assert pages == [feed.url for feed in feeds]
    assert len(robots) == len({urlsplit(feed.url).netloc for feed in feeds})
    assert all(record["calls"] == 1 and record["status"] == "http_302" for record in result["records"])
    assert all(row["http_status"] == 302 and row["body"]["decoded_count"] == 0 for row in result["raw_rows"])


def test_live_transport_is_anonymous_bounded_and_does_not_follow_redirects(monkeypatch):
    class Cookies:
        def __init__(self):
            self.values = ["old cookie"]

        def clear(self):
            self.values.clear()

    class Response:
        status_code = 302
        headers = {"Location": "https://elsewhere.invalid/"}

        def __init__(self):
            self.closed = False

        def iter_content(self, chunk_size):
            raise AssertionError("redirect response body should not be read")

        def close(self):
            self.closed = True

    class Session:
        def __init__(self):
            self.trust_env = True
            self.auth = "configured"
            self.cookies = Cookies()
            self.closed = False
            self.request = None

        def get(self, url, **kwargs):
            self.request = (url, kwargs, tuple(self.cookies.values), self.auth, self.trust_env)
            return response

        def close(self):
            self.closed = True

    response = Response()
    session = Session()
    monkeypatch.setattr(collector.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(requests, "Session", lambda: session)

    status, headers, body = collector.https_transport(
        "https://feed.example.za/listing", timeout=20, max_bytes=128
    )

    url, kwargs, cookies, auth, trust_env = session.request
    assert (status, headers, body) == (302, response.headers, b"")
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert kwargs["timeout"] == (20, 20) and kwargs["headers"]["User-Agent"] == reader.USER_AGENT
    assert cookies == () and auth is None and trust_env is False
    assert response.closed and session.closed


def test_live_transport_enforces_a_total_deadline_across_slow_drip_chunks(monkeypatch):
    now = {"value": 0.0}

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}

        def __init__(self):
            self.closed = False
            self.yielded = 0

        def iter_content(self, chunk_size):
            yield b"first"
            now["value"] = 21.0
            self.yielded += 1
            yield b"late"

        def close(self):
            self.closed = True

    class Session:
        def __init__(self):
            self.trust_env = True
            self.auth = None
            self.cookies = type("Cookies", (), {"clear": lambda self: None})()
            self.closed = False

        def get(self, url, **kwargs):
            self.kwargs = kwargs
            return response

        def close(self):
            self.closed = True

    response = Response()
    session = Session()
    monkeypatch.setattr(collector.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(requests, "Session", lambda: session)

    with pytest.raises(TimeoutError, match="total deadline"):
        collector.https_transport("https://feed.example.za/listing", timeout=20, max_bytes=128)

    assert response.yielded == 1
    assert response.closed and session.closed


@pytest.mark.parametrize("guard", ["stopped", "day_changed"])
def test_late_parsed_entries_are_normalized_for_raw_and_never_emitted_as_rows(monkeypatch, guard):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    current = {"now": NOW}
    stopped = {"value": False}
    private_marker = "PRIVATE_TOKEN_MARKER"
    unsafe_entry = {
        "text": f"<script>{private_marker}</script><h1>Safe headline</h1>",
        "url": f"{feed.url.rstrip('/')}/news/1?access_token={private_marker}",
        "raw_html": f"<script>{private_marker}</script>",
    }

    def transport(url, *, timeout, max_bytes):
        if url == feed.url:
            if guard == "stopped":
                stopped["value"] = True
            else:
                current["now"] = NOW + timedelta(days=1)
            return 200, {"Content-Type": "text/html"}, b"fixture"
        return 404, {"Content-Type": "text/plain"}, b""

    def parsed_response(_feed, *, fetched_at, transport):
        transport(_feed.url, timeout=reader.TIMEOUT_SECONDS, max_bytes=reader.MAX_RESPONSE_BYTES)
        return [unsafe_entry]

    monkeypatch.setattr(reader, "read_feed", parsed_response)
    result = collector.run(
        date(2026, 9, 30),
        "late-run",
        transport=transport,
        clock=lambda: current["now"],
        item_id_fn=item_id,
        stopped=lambda: stopped["value"],
    )

    assert result["records"][0]["status"] == guard
    assert result["records"][0]["calls"] == 1
    assert result["posts"] == result["observations"] == result["counters"] == []
    raw_body = result["raw_rows"][0]["body"]
    assert raw_body["decoded_count"] == 1
    assert raw_body["accepted_count"] == 0 and raw_body["held_count"] == 1
    assert raw_body["safe_entries"][0]["text"] == "Safe headline"
    assert raw_body["safe_entries"][0]["url"] is None
    assert private_marker not in repr(raw_body)
    assert "raw_html" not in repr(raw_body)


def test_robots_deny_stops_page_request_and_caches_the_host(monkeypatch):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    transport = FakeTransport(
        answers={feed.url: (200, {"Content-Type": "text/html"}, b"unused")},
        robots={urlsplit(feed.url).netloc: (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\n")},
    )

    result = _run(feeds, transport)

    assert result["records"][0]["status"] == "robots_disallowed"
    assert result["records"][0]["calls"] == 0
    assert [url for url, _, _ in transport.calls] == [f"{feed.url.rstrip('/')}/robots.txt"]
    assert result["raw_rows"][0]["http_status"] is None
    assert result["raw_rows"][0]["body"]["accepted_count"] == 0


@pytest.mark.parametrize(
    ("answer", "expected_status"),
    [
        ((302, {"Content-Type": "text/html", "Location": "https://elsewhere.invalid/"}, b"redirect"), "http_302"),
        ((200, {"Content-Type": "text/html"}, b"x" * (reader.MAX_RESPONSE_BYTES + 1)), "error"),
    ],
)
def test_redirect_and_oversize_body_fail_without_retry(monkeypatch, answer, expected_status):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    transport = FakeTransport(answers={feed.url: answer})

    result = _run(feeds, transport)

    assert result["records"][0]["status"] == expected_status
    assert result["records"][0]["calls"] == 1
    assert sum(url == feed.url for url, _, _ in transport.calls) == 1
    assert result["raw_rows"][0]["http_status"] == answer[0]
    assert result["raw_rows"][0]["body"]["accepted_count"] == 0


def test_day_change_after_robots_and_initial_stop_make_no_page_requests(monkeypatch):
    feeds = _feeds("za_enca", "ng_punch")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    next_day = NOW + timedelta(days=1)
    times = iter((NOW, NOW, next_day))

    def clock():
        return next(times, next_day)

    transport = FakeTransport()
    result = collector.run(date(2026, 9, 30), "run", transport=transport, clock=clock, item_id_fn=item_id)

    assert [record["status"] for record in result["records"]] == ["day_changed", "day_changed"]
    assert all(record["calls"] == 0 for record in result["records"])
    assert len(transport.calls) == 1 and transport.calls[0][0].endswith("/robots.txt")

    stopped_transport = FakeTransport()
    stopped = _run(feeds, stopped_transport, stopped=lambda: "budget stop")
    assert all(record["status"] == "stopped" and record["calls"] == 0 for record in stopped["records"])
    assert stopped_transport.calls == []


def test_one_feed_http_failure_does_not_skip_the_next_feed(monkeypatch):
    feeds = _feeds("za_enca", "ke_kbc_english_playlist")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    answers = {
        feeds[0].url: (503, {"Content-Type": "text/html"}, b""),
        feeds[1].url: (200, {"Content-Type": "text/html; charset=utf-8"},
                       (SAMPLES / FIXTURE_FILES[feeds[1].feed_id]).read_bytes()),
    }
    transport = FakeTransport(answers=answers)

    result = _run(feeds, transport)

    assert [record["status"] for record in result["records"]] == ["http_503", "ok"]
    assert [record["calls"] for record in result["records"]] == [1, 1]
    assert [row["http_status"] for row in result["raw_rows"]] == [503, 200]
    assert sum(url == feed.url for url, _, _ in transport.calls for feed in feeds) == 2



@pytest.mark.parametrize(
    ("robots_answer", "expected_words"),
    [
        ((403, {"Content-Type": "text/html"}, b"blocked"), ("robots.txt answered HTTP 403",)),
        ((429, {}, b""), ("robots.txt answered HTTP 429",)),
        ((503, {}, b""), ("robots.txt answered HTTP 503",)),
        ((301, {"Location": "http://elsewhere.invalid/robots.txt"}, b""),
         ("robots.txt answered HTTP 301", "redirect not followed")),
    ],
    ids=["403", "429", "503", "redirect"],
)
def test_unreadable_robots_names_its_answer_instead_of_a_rule_deny(monkeypatch, robots_answer, expected_words):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    transport = FakeTransport(
        answers={feed.url: (200, {"Content-Type": "text/html"}, b"unused")},
        robots={urlsplit(feed.url).netloc: robots_answer},
    )

    result = _run(feeds, transport)

    record, raw = result["records"][0], result["raw_rows"][0]
    assert record["status"] == "robots_disallowed" and record["calls"] == 0
    assert all(words in record["reason"] for words in expected_words)
    assert "did not allow" not in record["reason"]
    assert raw["body"]["reason"] == record["reason"]
    assert [url for url, _, _ in transport.calls] == [f"{feed.url.rstrip('/')}/robots.txt"]


def _robots_transport(feed, robots_answers, page=(200, {"Content-Type": "text/html"}, b"unused")):
    calls = []

    def transport(url, *, timeout, max_bytes):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return robots_answers[url]
        if url == feed.url:
            return page
        raise AssertionError(f"unexpected request {url}")

    return transport, calls


def test_robots_redirect_loop_through_http_counts_as_unavailable_and_the_page_is_read(monkeypatch):
    # SABC as served on 4 Oct 2026: https robots.txt answers 301 to the same URL over http, and http answers 301
    # back to https, so robots.txt never resolves. RFC 9309 2.3.1.2 treats a robots.txt reached through more than
    # five redirects as unavailable, the same as a 404, which this reader already treats as allowed.
    feeds = _feeds("za_sabc_news")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    robots = "https://www.sabcnews.com/robots.txt"
    transport, calls = _robots_transport(
        feed, {robots: (301, {"Location": "http://www.sabcnews.com/robots.txt"}, b"")})

    result = _run(feeds, transport)

    record = result["records"][0]
    assert record["status"] != "robots_disallowed" and record["calls"] == 1
    assert calls.count(feed.url) == 1
    assert 1 <= calls.count(robots) <= 6
    assert all(url.startswith("https://") for url in calls)


def test_robots_same_site_redirect_is_followed_and_its_rules_apply(monkeypatch):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    transport, calls = _robots_transport(feed, {
        "https://www.enca.com/robots.txt": (301, {"Location": "https://enca.com/robots.txt"}, b""),
        "https://enca.com/robots.txt": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\n"),
    })

    result = _run(feeds, transport)

    record = result["records"][0]
    assert record["status"] == "robots_disallowed" and record["calls"] == 0
    assert record["reason"] == "robots.txt did not allow the feed URL"
    assert calls == ["https://www.enca.com/robots.txt", "https://enca.com/robots.txt"]


def test_robots_same_site_redirect_to_an_allowing_file_reads_the_page(monkeypatch):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    transport, calls = _robots_transport(feed, {
        "https://www.enca.com/robots.txt": (302, {"Location": "/static/robots.txt"}, b""),
        "https://www.enca.com/static/robots.txt": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"),
    })

    result = _run(feeds, transport)

    assert result["records"][0]["calls"] == 1
    assert calls == ["https://www.enca.com/robots.txt", "https://www.enca.com/static/robots.txt", feed.url]


@pytest.mark.parametrize("answer", [
    (301, {"Location": "https://elsewhere.invalid/robots.txt"}, b""),
    (301, {"Location": "https://enca.com.elsewhere.invalid/robots.txt"}, b""),
    (301, {}, b""),
    (301, {"Location": "https://www.enca.com:99999/robots.txt"}, b""),
], ids=["other-host", "lookalike-host", "no-location", "bad-port"])
def test_robots_redirect_off_site_or_without_location_is_still_refused(monkeypatch, answer):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    transport, calls = _robots_transport(feed, {"https://www.enca.com/robots.txt": answer})

    result = _run(feeds, transport)

    record = result["records"][0]
    assert record["status"] == "robots_disallowed" and record["calls"] == 0
    assert "robots.txt answered HTTP 301" in record["reason"] and "redirect not followed" in record["reason"]
    assert calls == ["https://www.enca.com/robots.txt"]


def test_robots_redirect_chain_longer_than_five_counts_as_unavailable(monkeypatch):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    answers = {f"https://www.enca.com/r{i}/robots.txt": (301, {"Location": f"/r{i + 1}/robots.txt"}, b"")
               for i in range(1, 20)}
    answers["https://www.enca.com/robots.txt"] = (301, {"Location": "/r1/robots.txt"}, b"")
    transport, calls = _robots_transport(feed, answers)

    result = _run(feeds, transport)

    assert result["records"][0]["calls"] == 1
    assert len([url for url in calls if url.endswith("/robots.txt")]) == 6


def test_robots_request_failure_names_the_exception_class(monkeypatch):
    feeds = _feeds("za_enca")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]
    calls = []

    def transport(url, *, timeout, max_bytes):
        calls.append(url)
        if url.endswith("/robots.txt"):
            raise requests.ConnectTimeout("no answer")
        raise AssertionError("the page must not be requested")

    result = _run(feeds, transport)

    record = result["records"][0]
    assert record["status"] == "robots_disallowed" and record["calls"] == 0
    assert "robots.txt could not be read" in record["reason"] and "ConnectTimeout" in record["reason"]
    assert result["raw_rows"][0]["body"]["reason"] == record["reason"]
    assert calls == [f"{feed.url.rstrip('/')}/robots.txt"]


def test_robots_rule_deny_keeps_its_reason_for_every_feed_on_the_host(monkeypatch):
    feeds = _feeds("ke_kbc_english_playlist", "ke_capital_fm_playlist")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    transport = FakeTransport(
        robots={"radio.co.ke": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\n")},
    )

    result = _run(feeds, transport)

    assert [r["reason"] for r in result["records"]] == ["robots.txt did not allow the feed URL"] * 2


@pytest.mark.parametrize("make_error", [lambda: TimeoutError("request exceeded its total deadline"),
                                        lambda: requests.ReadTimeout("slow"),
                                        lambda: collector.ResponseTooLarge("response exceeds the body limit")],
                         ids=["deadline", "read-timeout", "too-large"])
def test_page_transport_failure_names_the_exception_class(monkeypatch, make_error):
    error = make_error()
    feeds = _feeds("ke_mdundo_top_songs")
    monkeypatch.setattr(collector, "confirmed_feeds", lambda: feeds)
    feed = feeds[0]

    def transport(url, *, timeout, max_bytes):
        if url.endswith("/robots.txt"):
            return 404, {}, b""
        raise error

    result = _run(feeds, transport)

    record = result["records"][0]
    assert record["status"] == "error" and record["calls"] == 1 and not record["ok"]
    assert record["reason"] == f"transport failed ({type(error).__name__})"
    assert result["raw_rows"][0]["body"]["reason"] == record["reason"]


def test_live_transport_raises_a_named_error_for_a_body_over_the_limit(monkeypatch):
    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}

        def iter_content(self, chunk_size):
            yield b"x" * 100
            yield b"x" * 100

        def close(self):
            pass

    class Session:
        trust_env, auth = True, None
        cookies = type("Cookies", (), {"clear": lambda self: None})()

        def get(self, url, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(collector.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(requests, "Session", Session)

    with pytest.raises(collector.ResponseTooLarge, match="body limit"):
        collector.https_transport("https://feed.example.za/listing", timeout=20, max_bytes=128)
    assert issubclass(collector.ResponseTooLarge, ValueError)
