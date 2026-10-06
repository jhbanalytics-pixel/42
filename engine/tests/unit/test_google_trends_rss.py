"""Unit tests for src/ingestion/connectors/google_trends_rss.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.google_trends_rss import (
    GEO_BY_MARKET,
    GOOGLE_TRENDS_RSS_BASE,
    GoogleTrendsRssConnector,
)


def _ok_resp(xml: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = xml.encode("utf-8")
    resp.text = xml
    return resp


def _sample_feed(items: int = 2) -> str:
    news_items = "".join(
        f"""
        <ht:news_item>
          <ht:news_item_title>News headline {i}</ht:news_item_title>
          <ht:news_item_url>https://example.com/news/{i}</ht:news_item_url>
          <ht:news_item_source>Example Source</ht:news_item_source>
        </ht:news_item>
        """
        for i in range(1, items + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss">
      <channel>
        <title>Daily Search Trends</title>
        <item>
          <title>Term One</title>
          <ht:approx_traffic>1000+</ht:approx_traffic>
          <pubDate>Fri, 03 Jul 2026 06:00:00 -0800</pubDate>
          <ht:picture>https://example.com/pic.jpg</ht:picture>
          {news_items}
        </item>
      </channel>
    </rss>"""


def test_fetch_returns_empty_when_disabled():
    """Flag off: no HTTP calls, empty DataFrame for all markets."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={"google_trends_rss": {"enabled": False}},
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        df = GoogleTrendsRssConnector(market="za").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()


def test_fetch_happy_path_parses_item():
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(items=2))
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={
                "google_trends_rss": {
                    "enabled": True,
                    "markets": {"za": {"geo": "ZA"}},
                }
            },
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        df = GoogleTrendsRssConnector(market="za").fetch()
    assert len(df) == 1
    row = df.iloc[0]
    assert row["query_term"] == "Term One"
    assert row["title"] == "Term One"
    assert "1000+" in row["text"]
    assert "News headline 1" in row["text"]
    assert "News headline 2" in row["text"]
    assert row["source"] == "google_trends_rss"
    assert row["platform"] == "google_search"
    assert row["market"] == "za"
    assert row["url"] == "https://example.com/news/1"
    assert row["published_at"].endswith("+00:00") or "T" in row["published_at"]


def test_fetch_uses_per_market_geo_code_in_url():
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(items=1))
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={
                "google_trends_rss": {
                    "enabled": True,
                    "markets": {"ng": {"geo": "NG"}},
                }
            },
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        GoogleTrendsRssConnector(market="ng").fetch()
    called_url = session.get.call_args[0][0]
    assert called_url == f"{GOOGLE_TRENDS_RSS_BASE}?geo=NG"


def test_fetch_defaults_geo_from_market_map_when_config_missing():
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(items=1))
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={"google_trends_rss": {"enabled": True}},
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        GoogleTrendsRssConnector(market="ke").fetch()
    called_url = session.get.call_args[0][0]
    assert called_url == f"{GOOGLE_TRENDS_RSS_BASE}?geo={GEO_BY_MARKET['ke']}"


def test_fetch_malformed_xml_returns_empty():
    session = MagicMock()
    session.get.return_value = _ok_resp("<not-valid-xml")
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={"google_trends_rss": {"enabled": True}},
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        df = GoogleTrendsRssConnector(market="za").fetch()
    assert df.empty


def test_fetch_http_500_returns_empty():
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 500
    session.get.return_value = bad
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={"google_trends_rss": {"enabled": True}},
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        df = GoogleTrendsRssConnector(market="za").fetch()
    assert df.empty


def test_fetch_network_exception_returns_empty():
    session = MagicMock()
    session.get.side_effect = ConnectionError("network down")
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={"google_trends_rss": {"enabled": True}},
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        df = GoogleTrendsRssConnector(market="za").fetch()
    assert df.empty


def test_fetch_sends_polite_ua_header():
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_feed(items=1))
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={"google_trends_rss": {"enabled": True}},
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        GoogleTrendsRssConnector(market="za").fetch()
    _, kwargs = session.get.call_args
    assert "User-Agent" in kwargs["headers"]
    assert "OgilvyTrendsEngineV2" in kwargs["headers"]["User-Agent"]


def test_fetch_no_news_items_falls_back_to_traffic_only_text():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss">
      <channel>
        <item>
          <title>Solo Term</title>
          <ht:approx_traffic>500+</ht:approx_traffic>
          <pubDate>Fri, 03 Jul 2026 06:00:00 -0800</pubDate>
        </item>
      </channel>
    </rss>"""
    session = MagicMock()
    session.get.return_value = _ok_resp(xml)
    with (
        patch(
            "src.ingestion.connectors.google_trends_rss.load_sources",
            return_value={"google_trends_rss": {"enabled": True}},
        ),
        patch.object(GoogleTrendsRssConnector, "_build_session", return_value=session),
    ):
        df = GoogleTrendsRssConnector(market="za").fetch()
    assert len(df) == 1
    assert df.iloc[0]["query_term"] == "Solo Term"
    assert df.iloc[0]["url"] == ""


def test_constants_match_documented_values():
    assert GOOGLE_TRENDS_RSS_BASE == "https://trends.google.com/trending/rss"
    assert GEO_BY_MARKET == {"za": "ZA", "ng": "NG", "ke": "KE"}
