"""Audit F7D-ENG-001 (core side) and F7D-COL-003: the core SocialCrawl client sends its key in x-api-key, which
requests copies across a cross-host redirect, so it follows none; the free local getter (RSS, App Store, robots.txt)
follows redirects only on the same host without a downgrade, and bounds its body before anything is parsed.

The real requests redirect and streaming code runs; only HTTPAdapter.send is replaced, so nothing leaves the
machine."""

import io

import pytest
import requests
from urllib3.response import HTTPResponse

from core.collect import local_sources
from core.collect.local_sources import FreeReadRefused, http_get
from core.collect.socialcrawl_client import requests_http

EVIL = "https://collector.example.net/grab"


@pytest.fixture
def wire(monkeypatch):
    """routes: url -> (status, headers, body). Returns the list of requests sent."""
    routes, sent = {}, []

    def send(self, request, **kwargs):
        sent.append(request)
        status, headers, body = routes[request.url.split("?")[0]]
        raw = HTTPResponse(body=io.BytesIO(body), headers=headers, status=status, preload_content=False,
                           decode_content=False)
        return self.build_response(request, raw)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return routes, sent


def test_the_socialcrawl_client_does_not_send_its_key_to_a_redirect_target(wire):
    routes, sent = wire
    routes["https://www.socialcrawl.dev/v1/tiktok/search/top"] = (302, {"Location": EVIL}, b"")
    routes[EVIL] = (200, {"Content-Type": "application/json"}, b'{"success": true}')
    status, body, _ = requests_http("GET", "https://www.socialcrawl.dev/v1/tiktok/search/top",
                                    params={"query": "amapiano"}, headers={"x-api-key": "sc_secret_key"})
    assert [r.url.split("?")[0] for r in sent] == ["https://www.socialcrawl.dev/v1/tiktok/search/top"]
    assert status == 302 and body is None


def test_the_socialcrawl_client_reads_a_plain_answer(wire):
    routes, _ = wire
    routes["https://www.socialcrawl.dev/v1/x"] = (200, {"Content-Type": "application/json"}, b'{"success": true}')
    assert requests_http("GET", "https://www.socialcrawl.dev/v1/x", headers={"x-api-key": "k"})[:2] == \
        (200, {"success": True})


def test_a_free_read_follows_an_upgrade_and_a_new_path_on_its_own_host(wire):
    routes, sent = wire
    routes["http://feeds.example.org/rss"] = (301, {"Location": "https://feeds.example.org/rss"}, b"")
    routes["https://feeds.example.org/rss"] = (302, {"Location": "/rss2"}, b"")
    routes["https://feeds.example.org/rss2"] = (200, {"Content-Type": "application/rss+xml; charset=utf-8"},
                                                "<rss>Mzansi</rss>".encode())
    assert http_get("http://feeds.example.org/rss") == (200, "<rss>Mzansi</rss>")
    assert len(sent) == 3


@pytest.mark.parametrize("location", ["https://other.example.org/rss", "http://feeds.example.org/rss",
                                      "http://169.254.169.254/computeMetadata/v1/"])
def test_a_free_read_refuses_a_redirect_off_its_host_or_down_to_http(wire, location):
    routes, sent = wire
    routes["https://feeds.example.org/rss"] = (302, {"Location": location}, b"")
    routes[location] = (200, {}, b"<rss/>")
    with pytest.raises(FreeReadRefused, match="redirect"):
        http_get("https://feeds.example.org/rss")
    assert len(sent) == 1


def test_a_free_read_stops_a_redirect_loop(wire):
    routes, _ = wire
    routes["https://feeds.example.org/a"] = (302, {"Location": "/b"}, b"")
    routes["https://feeds.example.org/b"] = (302, {"Location": "/a"}, b"")
    with pytest.raises(FreeReadRefused, match="redirects"):
        http_get("https://feeds.example.org/a")


def test_a_free_read_returns_a_body_at_its_limit_and_refuses_one_over_it(wire):
    routes, _ = wire
    routes["https://feeds.example.org/ok"] = (200, {"Content-Type": "text/xml; charset=utf-8"}, b"x" * 128)
    routes["https://feeds.example.org/big"] = (200, {"Content-Type": "text/xml; charset=utf-8"}, b"x" * 129)
    assert http_get("https://feeds.example.org/ok", max_bytes=128) == (200, "x" * 128)
    with pytest.raises(FreeReadRefused, match="byte limit"):
        http_get("https://feeds.example.org/big", max_bytes=128)


def test_a_free_read_refuses_a_declared_length_over_the_limit_before_reading(wire):
    routes, _ = wire
    routes["https://feeds.example.org/big"] = (200, {"Content-Length": "999999999"}, b"x")
    with pytest.raises(FreeReadRefused, match="byte limit"):
        http_get("https://feeds.example.org/big")


def test_a_free_read_past_its_deadline_is_refused(wire):
    routes, _ = wire
    routes["https://feeds.example.org/slow"] = (200, {}, b"x" * 10)
    ticks = iter([0.0, 500.0, 1000.0])
    with pytest.raises(FreeReadRefused, match="seconds"):
        http_get("https://feeds.example.org/slow", monotonic=lambda: next(ticks))


def test_robots_reads_through_the_same_bounded_getter(wire):
    routes, sent = wire
    routes["https://feeds.example.org/robots.txt"] = (302, {"Location": EVIL}, b"")
    robots = local_sources.Robots(http_get, sleep=lambda s: None)
    assert robots.allowed("https://feeds.example.org/rss") is False
    assert all(not r.url.startswith(EVIL) for r in sent)
