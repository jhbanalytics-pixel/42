"""Unit tests for src/ingestion/connectors/app_charts.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
from src.ingestion.connectors.app_charts import (
    APP_CHARTS_RSS_BASE,
    AppChartsConnector,
)
from src.ingestion.connectors.base import RAW_COLUMNS


def _ok_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _sample_feed(n: int = 3, storefront: str = "za") -> dict:
    return {
        "feed": {
            "title": "Top Free Apps",
            "updated": "2026-07-04T05:00:00-07:00",
            "results": [
                {
                    "artistName": f"Developer {i}",
                    "id": str(2000 + i),
                    "name": f"App {i}",
                    "releaseDate": "2026-06-01",
                    "kind": "iosSoftware",
                    "artworkUrl100": f"https://example.com/art{i}.png",
                    "genres": [{"name": "Social Networking"}, {"name": "Utilities"}],
                    "url": f"https://apps.apple.com/{storefront}/app/id{2000 + i}",
                }
                for i in range(1, n + 1)
            ],
        }
    }


def test_fetch_returns_empty_when_disabled(monkeypatch):
    """Disabled config short-circuits with zero HTTP calls."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={"app_charts": {"enabled": False}},
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="za").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()


def test_fetch_happy_path_produces_chart_rows(monkeypatch):
    """Happy path: 3 results -> 3 rows with rank, genres, and app name in text."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=3, storefront="za"))
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="za").fetch()
    assert len(df) == 3
    assert list(df.columns) == list(RAW_COLUMNS)
    row0 = df.iloc[0]
    assert row0["title"] == "App 1"
    assert "rank 1" in row0["text"]
    assert "Social Networking" in row0["text"]
    assert row0["title"] == "App 1"
    assert row0["query_term"] == "App 1"
    assert row0["source"] == "app_charts"
    assert row0["platform"] == "app_store"
    assert row0["market"] == "za"
    assert row0["content_type"] == "app_chart"
    assert row0["url"] == "https://apps.apple.com/za/app/id2001"
    assert row0["views"] == 0
    assert row0["likes"] == 0
    assert row0["comments"] == 0
    assert row0["shares"] == 0

    row1 = df.iloc[1]
    assert "rank 2" in row1["text"]


def test_fetch_url_uses_storefront_code(monkeypatch):
    """Request URL embeds the configured storefront code for the market."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=1, storefront="ng"))
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"ng": {"storefront": "ng"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        AppChartsConnector(market="ng").fetch()
    called_url = session.get.call_args_list[0][0][0]
    assert called_url == f"{APP_CHARTS_RSS_BASE}/ng/apps/top-free/25/apps.json"


def test_top_paid_included_only_when_configured(monkeypatch):
    """top-paid chart is requested only when listed in configs/sources.yaml charts."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=1, storefront="za"))
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free", "top-paid"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        AppChartsConnector(market="za").fetch()
    called_urls = [c[0][0] for c in session.get.call_args_list]
    assert any("top-free" in u for u in called_urls)
    assert any("top-paid" in u for u in called_urls)
    assert len(called_urls) == 2


def test_top_paid_excluded_by_default(monkeypatch):
    """Only top-free is requested when charts config omits top-paid."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=1, storefront="za"))
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        AppChartsConnector(market="za").fetch()
    called_urls = [c[0][0] for c in session.get.call_args_list]
    assert len(called_urls) == 1
    assert "top-free" in called_urls[0]


def test_fetch_malformed_json_returns_empty(monkeypatch):
    """Malformed JSON body returns empty DataFrame, no raise."""
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 200
    bad.json.side_effect = ValueError("bad json")
    session.get.return_value = bad
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="za").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)


def test_fetch_http_500_returns_empty(monkeypatch):
    """HTTP 500 for one market/chart combo returns empty, no raise."""
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 500
    session.get.return_value = bad
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="za").fetch()
    assert df.empty


def test_fetch_network_exception_returns_empty(monkeypatch):
    """Network blip returns empty, never blocks the cron."""
    session = MagicMock()
    session.get.side_effect = ConnectionError("network down")
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="za").fetch()
    assert df.empty


def test_user_agent_header_present_on_requests(monkeypatch):
    """Requests use a session that carries a User-Agent header (base connector pattern)."""
    session = MagicMock()
    session.headers = {"User-Agent": "TrendsEngineV2/1.0 (+polite-bot)"}
    session.get.return_value = _ok_resp(_sample_feed(n=1, storefront="za"))
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        connector = AppChartsConnector(market="za")
        connector.fetch()
    assert "User-Agent" in connector._session.headers


def test_published_at_parsed_tz_aware_utc(monkeypatch):
    """feed.updated is parsed into a tz-aware UTC timestamp."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(n=1, storefront="za"))
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="za").fetch()
    from datetime import datetime as dt

    parsed = dt.fromisoformat(df.iloc[0]["published_at"])
    assert parsed.tzinfo is not None


def test_published_at_falls_back_to_now_when_missing(monkeypatch):
    """Missing/unparseable feed.updated falls back to now() tz-aware."""
    session = MagicMock()
    payload = _sample_feed(n=1, storefront="za")
    del payload["feed"]["updated"]
    session.get.return_value = _ok_resp(payload)
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="za").fetch()
    from datetime import datetime as dt

    parsed = dt.fromisoformat(df.iloc[0]["published_at"])
    assert parsed.tzinfo is not None


def test_fetch_empty_results_returns_empty(monkeypatch):
    session = MagicMock()
    session.get.return_value = _ok_resp(
        {"feed": {"updated": "2026-07-04T05:00:00-07:00", "results": []}}
    )
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="za").fetch()
    assert df.empty


def test_unsupported_market_returns_empty(monkeypatch):
    """Market with no storefront mapping returns empty without a vendor call."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.app_charts.load_sources",
            return_value={
                "app_charts": {
                    "enabled": True,
                    "limit": 25,
                    "charts": ["top-free"],
                    "markets": {"za": {"storefront": "za"}},
                }
            },
        ),
        patch.object(AppChartsConnector, "_build_session", return_value=session),
    ):
        df = AppChartsConnector(market="xx").fetch()
    assert df.empty
    session.get.assert_not_called()
