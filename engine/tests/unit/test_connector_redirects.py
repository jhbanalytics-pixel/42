"""Audit F7D-ENG-001 and the engine side of F7D-COL-003: a keyed supplier call must not follow a redirect, since requests strips Authorization on a
cross-host redirect but copies any other header, so an x-api-key went to wherever the Location pointed. Feed readers
carry no key, but follow a redirect only on the same host and never from https down to http."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
import requests
import responses as responses_lib

from src.ingestion.connectors.base import FeedTooLarge, RedirectRefused, get_feed
from src.ingestion.connectors.brand24 import Brand24Connector
from src.ingestion.connectors.google_trends_rss import GoogleTrendsRssConnector
from src.ingestion.connectors.rss import RSSConnector
from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

EVIL = "https://collector.example.net/grab"


def _evil_calls():
    return [c for c in responses_lib.calls if c.request.url.startswith(EVIL)]


@responses_lib.activate
def test_socialcrawl_does_not_send_its_key_to_a_redirect_target():
    responses_lib.add(responses_lib.GET, re.compile(r"https://www\.socialcrawl\.dev/.*"), status=302,
                      headers={"Location": EVIL})
    responses_lib.add(responses_lib.GET, EVIL, json={"success": True, "credits_used": 0, "data": {"items": []}})
    cfg = {"socialcrawl": {"enabled": True, "budget_credits_per_run": 100, "phases": ["search"],
                           "phase_share": dict.fromkeys(("discover", "creators", "search", "reddit", "accounts",
                                                         "news"), 1.0),
                           "caps": {"search_terms": 1, "threads_terms": 0},
                           "markets": {"za": {"terms": ["amapiano"]}}}}
    SocialCrawlConnector.reset_credits()
    session = requests.Session()
    with (
        patch("src.ingestion.connectors.socialcrawl.load_sources", return_value=cfg),
        patch("src.ingestion.connectors.socialcrawl.get_secret", return_value="sc_secret_key"),
        patch("requests.Session", return_value=session),
    ):
        connector = SocialCrawlConnector(market="za")
        df = connector.fetch()
    assert any("socialcrawl.dev" in c.request.url for c in responses_lib.calls)
    assert _evil_calls() == []
    assert df.empty
    assert any("redirect refused" in f for f in connector._fetch_failures)


@responses_lib.activate
def test_brand24_does_not_send_its_key_to_a_redirect_target(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "b24_secret_key")
    responses_lib.add(responses_lib.GET, re.compile(r"https://api-data\.brand24\.com/.*"), status=302,
                      headers={"Location": EVIL})
    responses_lib.add(responses_lib.GET, EVIL, json={"topics": []})
    with (
        patch("src.ingestion.connectors.brand24.load_sources", return_value={"brand24": {"enabled": True}}),
        patch.object(Brand24Connector, "_build_session", return_value=requests.Session()),
    ):
        df = Brand24Connector(market="za").fetch(projects=[{"id": "1"}])
    assert any("brand24.com" in c.request.url for c in responses_lib.calls)
    assert _evil_calls() == []
    assert df.empty


@responses_lib.activate
def test_a_feed_follows_an_upgrade_to_https_on_its_own_host():
    responses_lib.add(responses_lib.GET, "http://feeds.example.org/rss", status=301,
                      headers={"Location": "https://feeds.example.org/rss"})
    responses_lib.add(responses_lib.GET, "https://feeds.example.org/rss", body=b"<rss></rss>")
    response = get_feed(requests.Session(), "http://feeds.example.org/rss", timeout=5)
    assert response.status_code == 200 and response.content == b"<rss></rss>"


@responses_lib.activate
def test_the_rss_reader_does_not_follow_a_redirect_to_a_private_address():
    responses_lib.add(responses_lib.GET, "https://sabc.co.za/feed/", status=302,
                      headers={"Location": "http://169.254.169.254/computeMetadata/v1/"})
    responses_lib.add(responses_lib.GET, re.compile(r"http://169\.254\.169\.254/.*"), body=b"<rss></rss>")
    with patch("src.ingestion.connectors.rss.load_sources",
               return_value={"rss_feeds": {"za": [{"url": "https://sabc.co.za/feed/", "name": "SABC"}]}}):
        connector = RSSConnector(market="za")
        connector._session = requests.Session()
        df = connector.fetch()
    assert [c.request.url for c in responses_lib.calls] == ["https://sabc.co.za/feed/"]
    assert df.empty
    assert any("redirect off the feed's host" in f for f in connector._fetch_failures)


@responses_lib.activate
def test_google_trends_rss_does_not_follow_a_redirect_to_localhost():
    responses_lib.add(responses_lib.GET, re.compile(r"https://trends\.google\.com/.*"), status=302,
                      headers={"Location": "http://localhost:8080/admin"})
    responses_lib.add(responses_lib.GET, "http://localhost:8080/admin", body=b"<rss></rss>")
    with (
        patch("src.ingestion.connectors.google_trends_rss.load_sources",
              return_value={"google_trends_rss": {"enabled": True, "markets": {"za": {"geo": "ZA"}}}}),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=requests.Session()),
    ):
        df = GoogleTrendsRssConnector(market="za").fetch()
    assert all("localhost" not in c.request.url for c in responses_lib.calls)
    assert df.empty


@responses_lib.activate
def test_a_feed_follows_a_new_path_on_its_own_host_but_not_another_host_or_a_downgrade():
    responses_lib.add(responses_lib.GET, "https://feeds.example.org/old", status=301,
                      headers={"Location": "/new"})
    responses_lib.add(responses_lib.GET, "https://feeds.example.org/new", body=b"<rss/>")
    assert get_feed(requests.Session(), "https://feeds.example.org/old", timeout=5).content == b"<rss/>"
    for location in ("https://other.example.org/rss", "http://feeds.example.org/rss"):
        responses_lib.replace(responses_lib.GET, "https://feeds.example.org/old", status=301,
                              headers={"Location": location})
        with pytest.raises(RedirectRefused):
            get_feed(requests.Session(), "https://feeds.example.org/old", timeout=5)


@responses_lib.activate
def test_a_feed_redirect_loop_stops():
    responses_lib.add(responses_lib.GET, "https://feeds.example.org/a", status=302, headers={"Location": "/b"})
    responses_lib.add(responses_lib.GET, "https://feeds.example.org/b", status=302, headers={"Location": "/a"})
    with pytest.raises(RedirectRefused, match="redirects"):
        get_feed(requests.Session(), "https://feeds.example.org/a", timeout=5)


@responses_lib.activate
def test_a_feed_body_at_its_limit_is_read_and_one_over_it_is_refused():
    responses_lib.add(responses_lib.GET, "https://feeds.example.org/ok", body=b"x" * 128)
    responses_lib.add(responses_lib.GET, "https://feeds.example.org/big", body=b"x" * 129)
    response = get_feed(requests.Session(), "https://feeds.example.org/ok", max_bytes=128, timeout=5)
    assert response.content == b"x" * 128 and response.text == "x" * 128
    with pytest.raises(FeedTooLarge):
        get_feed(requests.Session(), "https://feeds.example.org/big", max_bytes=128, timeout=5)


@responses_lib.activate
def test_the_rss_reader_records_an_oversized_feed_as_failed(monkeypatch):
    monkeypatch.setitem(get_feed.__kwdefaults__, "max_bytes", 64)
    responses_lib.add(responses_lib.GET, "https://sabc.co.za/feed/", body=b"<rss>" + b"x" * 200 + b"</rss>")
    with patch("src.ingestion.connectors.rss.load_sources",
               return_value={"rss_feeds": {"za": [{"url": "https://sabc.co.za/feed/", "name": "SABC"}]}}):
        connector = RSSConnector(market="za")
        connector._session = requests.Session()
        df = connector.fetch()
    assert df.empty
    assert any("byte limit" in f for f in connector._fetch_failures)
