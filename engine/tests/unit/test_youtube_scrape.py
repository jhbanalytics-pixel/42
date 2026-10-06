"""Unit tests for the yt-dlp YouTube scrape connector.

yt_dlp is imported lazily inside fetch(), so the tests inject a fake yt_dlp
module into sys.modules. No network, no real yt_dlp needed.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from src.ingestion.connectors.youtube_scrape import YouTubeScrapeConnector


def _cfg(**over):
    market = {
        "queries": ["amapiano", "gqom"],
        "scrape_enabled": True,
        "scrape_max_results": 5,
        "scrape_max_terms": 25,
    }
    market.update(over)
    return {"youtube_queries": {"za": market}}


def _install_fake_ydl(monkeypatch, entries, raise_exc=None):
    class _YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, query, download=False):
            if raise_exc is not None:
                raise raise_exc
            return {"entries": list(entries)}

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_YDL))


def test_disabled_returns_empty():
    with patch(
        "src.ingestion.connectors.youtube_scrape.load_sources",
        return_value=_cfg(scrape_enabled=False),
    ):
        df = YouTubeScrapeConnector(market="za").fetch()
    assert df.empty


def test_no_terms_returns_empty():
    with patch(
        "src.ingestion.connectors.youtube_scrape.load_sources",
        return_value=_cfg(queries=[]),
    ):
        df = YouTubeScrapeConnector(market="za").fetch()
    assert df.empty


def test_happy_path_normalises(monkeypatch):
    entries = [
        {
            "id": "v1",
            "title": "Amapiano Mix 2026",
            "uploader": "DJ X",
            "view_count": 1000,
            "channel_id": "c1",
        },
        {"id": "v2", "title": "Gqom Anthem", "channel": "DJ Y", "view_count": 50},
    ]
    _install_fake_ydl(monkeypatch, entries)
    with patch(
        "src.ingestion.connectors.youtube_scrape.load_sources",
        return_value=_cfg(queries=["amapiano"]),
    ):
        df = YouTubeScrapeConnector(market="za").fetch()
    assert len(df) == 2
    r = df.iloc[0]
    assert r["source"] == "youtube_scrape"
    assert r["author_name"] == "DJ X"
    assert r["platform"] == "youtube"
    assert r["market"] == "za"
    assert r["query_group"] == "youtube_scrape"
    assert r["content_type"] == "video/scrape"
    assert r["query_term"] == "amapiano"
    assert r["title"] == "Amapiano Mix 2026"
    assert r["text"] == "Amapiano Mix 2026"
    assert r["views"] == 1000.0
    assert r["url"].endswith("v1")


def test_dedupes_video_across_terms(monkeypatch):
    entries = [{"id": "dup", "title": "T", "uploader": "U", "view_count": 1}]
    _install_fake_ydl(monkeypatch, entries)
    with patch(
        "src.ingestion.connectors.youtube_scrape.load_sources",
        return_value=_cfg(queries=["a", "b"]),
    ):
        df = YouTubeScrapeConnector(market="za").fetch()
    assert len(df) == 1


def test_yt_dlp_error_is_non_fatal(monkeypatch):
    _install_fake_ydl(monkeypatch, [], raise_exc=RuntimeError("bot challenge"))
    conn = YouTubeScrapeConnector(market="za")
    with patch(
        "src.ingestion.connectors.youtube_scrape.load_sources",
        return_value=_cfg(queries=["a"]),
    ):
        df = conn.fetch()
    assert df.empty
    assert any("youtube_scrape" in f for f in conn._fetch_failures)


def test_missing_yt_dlp_degrades(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    conn = YouTubeScrapeConnector(market="za")
    with patch(
        "src.ingestion.connectors.youtube_scrape.load_sources",
        return_value=_cfg(queries=["a"]),
    ):
        df = conn.fetch()
    assert df.empty
    assert any("yt_dlp not installed" in f for f in conn._fetch_failures)


def test_android_player_client_in_opts(monkeypatch):
    """The android player client must be tried first so Cloud Run datacenter IPs
    are less likely to draw YouTube's bot challenge."""
    captured = []

    class _YDL:
        def __init__(self, opts):
            captured.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, query, download=False):
            return {"entries": []}

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_YDL))
    with patch(
        "src.ingestion.connectors.youtube_scrape.load_sources",
        return_value=_cfg(queries=["a"]),
    ):
        YouTubeScrapeConnector(market="za").fetch()
    assert captured[0]["extractor_args"]["youtube"]["player_client"] == ["android"]


def test_player_client_fallback_when_first_client_empty(monkeypatch):
    """When android returns no usable entries, ios/mweb are tried before giving up."""
    calls = []

    class _YDL:
        def __init__(self, opts):
            self.client = opts["extractor_args"]["youtube"]["player_client"][0]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, query, download=False):
            calls.append(self.client)
            if self.client == "android":
                return {"entries": [None, None]}
            return {"entries": [{"id": "v1", "title": "Hit", "uploader": "DJ", "view_count": 10}]}

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_YDL))
    with patch(
        "src.ingestion.connectors.youtube_scrape.load_sources",
        return_value=_cfg(queries=["amapiano"]),
    ):
        df = YouTubeScrapeConnector(market="za").fetch()
    assert calls[:2] == ["android", "ios"]
    assert len(df) == 1
