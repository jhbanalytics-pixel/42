"""Unit tests for src/ingestion/connectors/wikipedia.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.wikipedia import (
    DEFAULT_LIMIT,
    WIKIPEDIA_REST_BASE,
    WikipediaConnector,
)


def _ok_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _status_resp(code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = code
    return resp


def _sample_items(n: int = 3, country: str = "ZA") -> dict:
    """Envelope shape verified live 19 Jun 2026: items[].articles[]."""
    return {
        "items": [
            {
                "country": country,
                "access": "all-access",
                "year": "2026",
                "month": "06",
                "day": "18",
                "articles": [
                    {
                        "article": f"Article_{i}",
                        "project": "en.wikipedia",
                        "views_ceil": 10000 - i,
                        "rank": i,
                    }
                    for i in range(1, n + 1)
                ],
            }
        ]
    }


def test_fetch_returns_empty_when_disabled():
    """enabled:false short-circuits to empty without any HTTP call."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": False}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        df = WikipediaConnector(market="za").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()


def test_fetch_returns_empty_for_unsupported_market():
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": True}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        df = WikipediaConnector(market="xx").fetch()
    assert df.empty
    session.get.assert_not_called()


def test_fetch_happy_path_parses_envelope():
    """items[].articles[] -> RAW_COLUMNS rows, ranked, with views from views_ceil."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_items(n=3, country="ZA"))
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": True}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        df = WikipediaConnector(market="za").fetch()
    assert len(df) == 3
    assert list(df.columns) == list(RAW_COLUMNS)
    row = df.iloc[0]
    assert row["source"] == "wikipedia"
    assert row["platform"] == "wikipedia"
    assert row["market"] == "za"
    assert row["content_type"] == "encyclopedia_article"
    assert row["query_group"] == "wikipedia_top_per_country"
    # Underscores humanised in the title + query_term.
    assert row["title"] == "Article 1"
    assert row["query_term"] == "Article 1"
    # views_ceil mapped onto views.
    assert int(row["views"]) == 9999
    assert "en.wikipedia.org/wiki/Article_1" in row["url"]


def test_country_endpoint_url_shape():
    """Request hits top-per-country/{COUNTRY}/all-access/{Y}/{M}/{D}."""
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_items(n=1, country="NG"))
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": True}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        WikipediaConnector(market="ng").fetch()
    called_url = session.get.call_args[0][0]
    assert f"{WIKIPEDIA_REST_BASE}/top-per-country/NG/all-access/" in called_url


def test_skip_set_drops_chrome_titles():
    """Main_Page + Special:* carry no topic signal and are filtered."""
    session = MagicMock()
    payload = {
        "items": [
            {
                "articles": [
                    {
                        "article": "Main_Page",
                        "project": "en.wikipedia",
                        "views_ceil": 50000,
                        "rank": 1,
                    },
                    {
                        "article": "Special:Search",
                        "project": "en.wikipedia",
                        "views_ceil": 40000,
                        "rank": 2,
                    },
                    {
                        "article": "wiki.phtml",
                        "project": "en.wikipedia",
                        "views_ceil": 38000,
                        "rank": 3,
                    },
                    {
                        "article": "index.php",
                        "project": "en.wikipedia",
                        "views_ceil": 36000,
                        "rank": 4,
                    },
                    {
                        "article": "Burna_Boy",
                        "project": "en.wikipedia",
                        "views_ceil": 30000,
                        "rank": 3,
                    },
                ]
            }
        ]
    }
    session.get.return_value = _ok_resp(payload)
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": True}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        df = WikipediaConnector(market="ng").fetch()
    assert len(df) == 1
    assert df.iloc[0]["title"] == "Burna Boy"


def test_limit_caps_row_count():
    session = MagicMock()
    session.get.return_value = _ok_resp(_sample_items(n=10))
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": True, "limit": 4}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        df = WikipediaConnector(market="za").fetch()
    assert len(df) == 4


def test_walks_back_on_404():
    """First day 404 (data not published yet); connector walks back and parses."""
    session = MagicMock()
    session.get.side_effect = [
        _status_resp(404),
        _ok_resp(_sample_items(n=2)),
    ]
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": True, "days": 3}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        df = WikipediaConnector(market="za").fetch()
    assert len(df) == 2
    assert session.get.call_count == 2


def test_network_exception_returns_empty():
    session = MagicMock()
    session.get.side_effect = ConnectionError("network down")
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": True, "days": 1}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        df = WikipediaConnector(market="za").fetch()
    assert df.empty


def test_views_ceil_absent_falls_back_to_zero():
    payload = {"items": [{"articles": [{"article": "Sapa", "project": "en.wikipedia", "rank": 1}]}]}
    session = MagicMock()
    session.get.return_value = _ok_resp(payload)
    with (
        patch(
            "src.ingestion.connectors.wikipedia.load_sources",
            return_value={"wikipedia": {"enabled": True}},
        ),
        patch.object(WikipediaConnector, "_build_session", return_value=session),
    ):
        df = WikipediaConnector(market="za").fetch()
    assert len(df) == 1
    assert int(df.iloc[0]["views"]) == 0


def test_constants_match_documented_values():
    assert DEFAULT_LIMIT == 40
    assert WIKIPEDIA_REST_BASE == ("https://wikimedia.org/api/rest_v1/metrics/pageviews")
