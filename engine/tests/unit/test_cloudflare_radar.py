"""Unit tests for src/ingestion/connectors/cloudflare_radar.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.cloudflare_radar import (
    RADAR_RANKING_URL,
    CloudflareRadarConnector,
)

TOKEN_ENV = "test-token"


def _ok_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _sample_payload(n: int = 2) -> dict:
    return {
        "success": True,
        "result": {
            "top_0": [
                {
                    "rank": i,
                    "domain": f"example{i}.com",
                    "categories": [{"name": "News"}],
                }
                for i in range(1, n + 1)
            ]
        },
    }


def test_fetch_returns_empty_when_disabled(monkeypatch):
    """enabled: false short-circuits, zero HTTP calls."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": False}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()


def test_fetch_returns_empty_when_token_missing(monkeypatch):
    """enabled: true but no CLOUDFLARE_RADAR_TOKEN: empty, zero HTTP calls, info log."""
    session = MagicMock()
    monkeypatch.delenv("CLOUDFLARE_RADAR_TOKEN", raising=False)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        connector = CloudflareRadarConnector(market="za")
        with patch.object(connector.logger, "info") as mock_info:
            df = connector.fetch()
    assert df.empty
    session.get.assert_not_called()
    assert mock_info.called
    assert "token" in mock_info.call_args[0][0].lower()


def test_fetch_happy_path_produces_rows(monkeypatch):
    """Happy path: docs-derived JSON fixture parses into rows."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_payload(n=2))
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True, "limit": 25}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert len(df) == 2
    assert list(df.columns) == list(RAW_COLUMNS)
    assert df.iloc[0]["source"] == "cloudflare_radar"
    assert df.iloc[0]["platform"] == "web_attention"
    assert df.iloc[0]["market"] == "za"
    assert df.iloc[0]["query_term"] == "example1.com"
    assert "rank 1" in df.iloc[0]["text"]
    assert "News" in df.iloc[0]["text"]


def test_fetch_malformed_json_returns_empty(monkeypatch):
    """Malformed JSON does not crash, returns empty."""
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 200
    bad.json.side_effect = ValueError("bad json")
    session.get.return_value = bad
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert df.empty


def test_fetch_http_401_returns_empty(monkeypatch):
    """Vendor 401 (bad token) returns empty, no exception."""
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 401
    session.get.return_value = resp
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert df.empty


def test_fetch_http_500_returns_empty(monkeypatch):
    """Vendor 500 returns empty, no exception."""
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 500
    session.get.return_value = resp
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert df.empty


def test_fetch_network_exception_returns_empty(monkeypatch):
    """Network blip returns empty, never blocks the cron."""
    session = MagicMock()
    session.get.side_effect = ConnectionError("network down")
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert df.empty


def test_fetch_applies_bearer_auth_header_and_user_agent(monkeypatch):
    """Request carries Authorization Bearer header and a User-Agent header."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_payload(n=1))
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        CloudflareRadarConnector(market="za").fetch()
    _, kwargs = session.get.call_args
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN_ENV}"
    assert kwargs["headers"]["User-Agent"]


def test_fetch_url_carries_location_param(monkeypatch):
    """Request URL includes the configured location code for the market."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_payload(n=1))
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={
                "cloudflare_radar": {
                    "enabled": True,
                    "limit": 10,
                    "markets": {"ng": {"location": "NG"}},
                }
            },
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        CloudflareRadarConnector(market="ng").fetch()
    called_url = session.get.call_args[0][0]
    assert called_url.startswith(RADAR_RANKING_URL)
    assert "location=NG" in called_url
    assert "limit=10" in called_url


def test_fetch_handles_empty_results(monkeypatch):
    session = MagicMock()
    session.get.return_value = _ok_resp({"success": True, "result": {"top_0": []}})
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert df.empty


def test_fetch_accepts_plain_list_result_variant(monkeypatch):
    """Some docs variants show result as a plain list; parser must accept it."""
    session = MagicMock()
    session.get.return_value = _ok_resp(
        {"success": True, "result": [{"rank": 1, "domain": "plainlist.com"}]}
    )
    monkeypatch.setenv("CLOUDFLARE_RADAR_TOKEN", TOKEN_ENV)
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": True}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert len(df) == 1
    assert df.iloc[0]["query_term"] == "plainlist.com"


def test_disabled_flag_zero_http_calls_and_empty_df(monkeypatch):
    """Full disabled-state proof: flag off means byte-identical empty output, no network."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.cloudflare_radar.load_sources",
            return_value={"cloudflare_radar": {"enabled": False, "markets": {"za": {}}}},
        ),
        patch.object(CloudflareRadarConnector, "_build_session", return_value=session),
    ):
        df = CloudflareRadarConnector(market="za").fetch()
    assert isinstance(df, pd.DataFrame)
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()
    session.post.assert_not_called()
