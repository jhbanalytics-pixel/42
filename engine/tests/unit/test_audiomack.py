"""Unit tests for src/ingestion/connectors/audiomack.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
from src.ingestion.connectors.audiomack import (
    AUDIOMACK_TRENDING_URL,
    AudiomackConnector,
)
from src.ingestion.connectors.base import RAW_COLUMNS

CREDS_ENV = {
    "AUDIOMACK_CONSUMER_KEY": "test-key",
    "AUDIOMACK_CONSUMER_SECRET": "test-secret",
}


def _ok_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _sample_payload(n: int = 2) -> dict:
    return {
        "results": [
            {
                "id": str(100 + i),
                "title": f"Track {i}",
                "artist": f"Artist {i}",
                "url_slug": f"track-{i}",
                "uploader": {"url_slug": f"artist-{i}"},
                "stats": {"plays-raw": 1000 * i},
                "released": 1750000000,
            }
            for i in range(1, n + 1)
        ]
    }


def test_fetch_returns_empty_when_disabled(monkeypatch):
    """enabled: false short-circuits, zero HTTP calls."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={"audiomack": {"enabled": False}},
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="ng").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()


def test_fetch_returns_empty_when_credentials_missing(monkeypatch):
    """enabled: true but no consumer key/secret in env: empty, zero HTTP calls, info log."""
    session = MagicMock()
    monkeypatch.delenv("AUDIOMACK_CONSUMER_KEY", raising=False)
    monkeypatch.delenv("AUDIOMACK_CONSUMER_SECRET", raising=False)
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={"audiomack": {"enabled": True}},
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        connector = AudiomackConnector(market="ng")
        with patch.object(connector.logger, "info") as mock_info:
            df = connector.fetch()
    assert df.empty
    session.get.assert_not_called()
    assert mock_info.called
    assert "key/secret" in mock_info.call_args[0][0].lower()


def test_fetch_happy_path_produces_rows(monkeypatch):
    """Happy path: docs-derived JSON fixture parses into rows."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_payload(n=2))
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={
                "audiomack": {"enabled": True, "markets": {"ng": {"genres": ["afrobeats"]}}}
            },
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="ng").fetch()
    assert len(df) == 2
    assert list(df.columns) == list(RAW_COLUMNS)
    assert df.iloc[0]["source"] == "audiomack"
    assert df.iloc[0]["platform"] == "audiomack"
    assert df.iloc[0]["market"] == "ng"
    assert df.iloc[0]["query_term"] == "Track 1"
    assert df.iloc[0]["author_name"] == "Artist 1"
    assert df.iloc[0]["views"] == 1000
    assert "audiomack-1" in df.iloc[0]["url"] or "track-1" in df.iloc[0]["url"]


def test_fetch_malformed_json_returns_empty(monkeypatch):
    """Malformed JSON does not crash, returns empty."""
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 200
    bad.json.side_effect = ValueError("bad json")
    session.get.return_value = bad
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={"audiomack": {"enabled": True}},
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="ng").fetch()
    assert df.empty


def test_fetch_http_401_returns_empty(monkeypatch):
    """Vendor 401 (bad OAuth signature) returns empty, no exception."""
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 401
    session.get.return_value = resp
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={"audiomack": {"enabled": True}},
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="ng").fetch()
    assert df.empty


def test_fetch_http_500_returns_empty(monkeypatch):
    """Vendor 500 returns empty, no exception."""
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 500
    session.get.return_value = resp
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={"audiomack": {"enabled": True}},
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="ng").fetch()
    assert df.empty


def test_fetch_network_exception_returns_empty(monkeypatch):
    """Network blip returns empty, never blocks the cron."""
    session = MagicMock()
    session.get.side_effect = ConnectionError("network down")
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={"audiomack": {"enabled": True}},
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="ng").fetch()
    assert df.empty


def test_fetch_applies_oauth1_auth_and_headers(monkeypatch):
    """Request carries OAuth1 auth object and a User-Agent header."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_payload(n=1))
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={
                "audiomack": {"enabled": True, "markets": {"ng": {"genres": ["afrobeats"]}}}
            },
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        AudiomackConnector(market="ng").fetch()
    _, kwargs = session.get.call_args
    assert "auth" in kwargs
    from requests_oauthlib import OAuth1

    assert isinstance(kwargs["auth"], OAuth1)
    assert kwargs["headers"]["User-Agent"]


def test_fetch_url_carries_genre_param(monkeypatch):
    """Request URL includes the configured genre for the market."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_payload(n=1))
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={
                "audiomack": {"enabled": True, "markets": {"za": {"genres": ["amapiano"]}}}
            },
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        AudiomackConnector(market="za").fetch()
    called_url = session.get.call_args[0][0]
    assert called_url.startswith(AUDIOMACK_TRENDING_URL)
    assert "genre=amapiano" in called_url


def test_fetch_handles_empty_results(monkeypatch):
    session = MagicMock()
    session.get.return_value = _ok_resp({"results": []})
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={"audiomack": {"enabled": True}},
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="ng").fetch()
    assert df.empty


def test_dedupes_by_track_id_across_genres(monkeypatch):
    """Same track id appearing under two genres is only counted once."""
    session = MagicMock()
    payload = _sample_payload(n=1)
    session.get.return_value = _ok_resp(payload)
    monkeypatch.setenv("AUDIOMACK_CONSUMER_KEY", CREDS_ENV["AUDIOMACK_CONSUMER_KEY"])
    monkeypatch.setenv("AUDIOMACK_CONSUMER_SECRET", CREDS_ENV["AUDIOMACK_CONSUMER_SECRET"])
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={
                "audiomack": {
                    "enabled": True,
                    "markets": {"za": {"genres": ["amapiano", "house"]}},
                }
            },
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="za").fetch()
    assert len(df) == 1


def test_disabled_flag_zero_http_calls_and_empty_df(monkeypatch):
    """Full disabled-state proof: flag off means byte-identical empty output, no network."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.audiomack.load_sources",
            return_value={"audiomack": {"enabled": False, "markets": {"ng": {}}}},
        ),
        patch.object(AudiomackConnector, "_build_session", return_value=session),
    ):
        df = AudiomackConnector(market="ng").fetch()
    assert isinstance(df, pd.DataFrame)
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()
    session.post.assert_not_called()
