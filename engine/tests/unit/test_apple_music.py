"""Unit tests for src/ingestion/connectors/apple_music.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
from src.ingestion.connectors.apple_music import (
    APPLE_MUSIC_RSS_BASE,
    DEFAULT_TRACKS,
    FETCH_ATTEMPTS,
    AppleMusicConnector,
)
from src.ingestion.connectors.base import RAW_COLUMNS


def _ok_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _sample_feed(n: int = 3, market: str = "za") -> dict:
    return {
        "feed": {
            "title": "Top Songs",
            "country": market,
            "results": [
                {
                    "name": f"Track {i}",
                    "artistName": f"Artist {i}",
                    "url": f"https://music.apple.com/{market}/song/{i}",
                    "genres": [{"name": "Afrobeats" if i % 2 else "Amapiano"}],
                    "releaseDate": "2026-05-15",
                    "kind": "song",
                    "id": str(1000 + i),
                }
                for i in range(1, n + 1)
            ],
        }
    }


def test_fetch_returns_empty_when_inactive(monkeypatch):
    """Global active=false short-circuits to empty DataFrame."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": False}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        df = AppleMusicConnector(market="za").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()


def test_fetch_returns_empty_for_unsupported_market(monkeypatch):
    """Unknown market returns empty without calling vendor."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        df = AppleMusicConnector(market="xx").fetch()
    assert df.empty
    session.get.assert_not_called()


def test_per_market_active_false_short_circuits(monkeypatch):
    """Per-market active: false disables one market while keeping others."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={
                "apple_music": {
                    "active": True,
                    "markets": {"ke": {"active": False}},
                }
            },
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        df = AppleMusicConnector(market="ke").fetch()
    assert df.empty
    session.get.assert_not_called()


def test_fetch_happy_path_produces_chart_rows(monkeypatch):
    """Happy path: 3 results -> 3 rows with correct schema + ranking."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=3, market="za"))
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        df = AppleMusicConnector(market="za").fetch()
    assert len(df) == 3
    assert list(df.columns) == list(RAW_COLUMNS)
    assert df.iloc[0]["author_name"] == "Artist 1"
    assert df.iloc[0]["title"] == "Track 1 - Artist 1"
    assert df.iloc[0]["source"] == "apple_music"
    assert df.iloc[0]["platform"] == "apple_music"
    assert df.iloc[0]["market"] == "za"
    assert df.iloc[0]["content_type"] == "chart_track"
    assert df.iloc[0]["query_group"] == "apple_music_top_50"


def test_fetch_url_pattern_matches_vendor(monkeypatch):
    """Request URL uses /api/v2/{market}/music/most-played/{N}/songs.json shape."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=1))
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        AppleMusicConnector(market="ng").fetch()
    called_url = session.get.call_args[0][0]
    assert called_url == f"{APPLE_MUSIC_RSS_BASE}/ng/music/most-played/50/songs.json"


def test_tracks_count_respects_per_run_config(monkeypatch):
    """tracks=10 overrides default 50."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=1))
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True, "tracks": 10}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        AppleMusicConnector(market="za").fetch()
    called_url = session.get.call_args[0][0]
    assert "most-played/10/songs.json" in called_url


def test_tracks_count_capped_at_50(monkeypatch):
    """tracks=999 clamps to vendor max 50."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=1))
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True, "tracks": 999}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        AppleMusicConnector(market="za").fetch()
    assert "most-played/50/songs.json" in session.get.call_args[0][0]


def test_fetch_404_returns_empty(monkeypatch):
    """Vendor 404 returns empty DataFrame, no exception."""
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 404
    session.get.return_value = bad
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        df = AppleMusicConnector(market="ng").fetch()
    assert df.empty


def test_fetch_handles_empty_results(monkeypatch):
    session = MagicMock()
    session.get.return_value = _ok_resp({"feed": {"results": []}})
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        df = AppleMusicConnector(market="za").fetch()
    assert df.empty


def test_normalise_handles_missing_genre(monkeypatch):
    """Track without genres array stays consistent: query_term falls back to name."""
    session = MagicMock()
    payload = {
        "feed": {
            "results": [
                {
                    "name": "OnlyName",
                    "artistName": "OnlyArtist",
                    "url": "https://example.com/x",
                    "genres": [],
                    "releaseDate": "2026-05-15",
                }
            ]
        }
    }
    session.get.return_value = _ok_resp(payload)
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        df = AppleMusicConnector(market="za").fetch()
    assert len(df) == 1
    assert df.iloc[0]["query_term"] == "OnlyName"


def test_fetch_network_exception_returns_empty(monkeypatch):
    """Network blip returns empty + logs, never blocks the cron."""
    session = MagicMock()
    session.get.side_effect = ConnectionError("network down")
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
        patch("time.sleep"),
    ):
        df = AppleMusicConnector(market="za").fetch()
    assert df.empty
    assert session.get.call_count == FETCH_ATTEMPTS


def test_fetch_retries_once_after_timeout(monkeypatch):
    """A single vendor read timeout is retried; second attempt lands 50 rows.

    The 2026-07-20 KE flat-line: one 15s read timeout, no retry, market
    starved for the day while the endpoint was healthy minutes later.
    """
    session = MagicMock()
    session.get.side_effect = [
        TimeoutError("read timed out"),
        _ok_resp(_sample_feed(n=3, market="ke")),
    ]
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
        patch("time.sleep"),
    ):
        df = AppleMusicConnector(market="ke").fetch()
    assert len(df) == 3
    assert session.get.call_count == 2


def test_release_date_parsed_to_iso_with_tz(monkeypatch):
    """ReleaseDate YYYY-MM-DD becomes ISO-with-UTC."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=1))
    with (
        patch(
            "src.ingestion.connectors.apple_music.load_sources",
            return_value={"apple_music": {"active": True}},
        ),
        patch.object(AppleMusicConnector, "_build_session", return_value=session),
    ):
        df = AppleMusicConnector(market="za").fetch()
    from datetime import datetime as dt

    parsed = dt.fromisoformat(df.iloc[0]["published_at"])
    assert parsed.tzinfo is not None
    assert parsed.year == 2026


def test_constants_match_documented_values():
    assert DEFAULT_TRACKS == 50
    assert FETCH_ATTEMPTS == 2
    assert APPLE_MUSIC_RSS_BASE == "https://rss.marketingtools.apple.com/api/v2"
