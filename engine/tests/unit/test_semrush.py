"""Unit tests for src/ingestion/connectors/semrush.py."""

from unittest.mock import MagicMock, patch

import pandas as pd
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.semrush import (
    SemrushConnector,
    _parse_keyword_entry,
    _response_indicates_budget_exhausted,
    _trends_to_velocity,
)


def test_parse_keyword_entry_string():
    assert _parse_keyword_entry("mpesa") == ("mpesa", "search_intent")


def test_parse_keyword_entry_dict():
    assert _parse_keyword_entry({"term": "sapa", "query_group": "economy_sapa_hustle"}) == (
        "sapa",
        "economy_sapa_hustle",
    )


def test_response_indicates_budget_exhausted():
    assert _response_indicates_budget_exhausted("ERROR 132 :: API UNITS BALANCE IS ZERO")
    assert not _response_indicates_budget_exhausted('{"meta":{"success":true}}')


def test_trends_to_velocity():
    assert _trends_to_velocity([50, 60, 100, 90]) == 0.5
    assert _trends_to_velocity([]) == 0.0


def test_fetch_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setenv("SEMRUSH_API_KEY", "tok")
    with patch(
        "src.ingestion.connectors.semrush.load_sources",
        return_value={"semrush": {"enabled": False}},
    ):
        df = SemrushConnector(market="za").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)


def test_fetch_returns_empty_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("SEMRUSH_API_KEY", raising=False)
    with (
        patch(
            "src.ingestion.connectors.semrush.load_sources",
            return_value={"semrush": {"enabled": True, "markets": {"za": {"keywords": ["mpesa"]}}}},
        ),
        patch("src.ingestion.connectors.semrush.get_secret", return_value=""),
    ):
        df = SemrushConnector(market="za").fetch()
    assert df.empty


def test_fetch_maps_metrics_row(monkeypatch):
    monkeypatch.setenv("SEMRUSH_API_KEY", "tok")
    payload = {
        "meta": {"success": True, "status_code": 200},
        "data": {
            "search_volume": "1200",
            "keyword_difficulty": 45,
            "cpc": "10",
            "intents": ["INFORMATIONAL"],
            "serp_features": ["AI_OVERVIEW"],
            "trends": [40, 50, 80, 100],
        },
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"meta":{"success":true},"data":{}}'
    mock_resp.json.return_value = payload

    cfg = {
        "semrush": {
            "enabled": True,
            "budget_units_per_run": 60,
            "units_per_keyword": 20,
            "markets": {
                "za": {
                    "keywords": [{"term": "mpesa", "query_group": "fintech_mpesa"}],
                }
            },
        }
    }

    with patch("src.ingestion.connectors.semrush.load_sources", return_value=cfg):
        conn = SemrushConnector(market="za")
        conn._session = MagicMock()
        conn._session.get.return_value = mock_resp
        df = conn.fetch(api_key="tok")

    assert len(df) == 1
    row = df.iloc[0]
    assert row["source"] == "semrush"
    assert row["platform"] == "semrush_search"
    assert row["content_type"] == "keyword_metric"
    assert row["query_group"] == "fintech_mpesa"
    assert row["query_term"] == "mpesa"
    # search metrics must not leak into engagement fields: a flip would
    # inflate engagement_total. Semrush contributes via search_velocity only.
    assert row["views"] == 0.0
    assert row["likes"] == 0.0
    assert "search_volume=1200" in row["text"]


def test_fetch_halts_on_error_132(monkeypatch):
    monkeypatch.setenv("SEMRUSH_API_KEY", "tok")
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.text = '{"meta":{"success":true,"status_code":200},"data":{"search_volume":"100","keyword_difficulty":10}}'
    ok_resp.json.return_value = {
        "meta": {"success": True, "status_code": 200},
        "data": {"search_volume": "100", "keyword_difficulty": 10},
    }

    err_resp = MagicMock()
    err_resp.status_code = 200
    err_resp.text = "ERROR 132 :: API UNITS BALANCE IS ZERO"
    err_resp.json.side_effect = ValueError("not json")

    cfg = {
        "semrush": {
            "enabled": True,
            "budget_units_per_run": 120,
            "units_per_keyword": 20,
            "markets": {
                "ng": {
                    "keywords": [
                        {"term": "sapa", "query_group": "economy_sapa_hustle"},
                        {"term": "afrobeats", "query_group": "music_afrobeats"},
                    ],
                }
            },
        }
    }

    with patch("src.ingestion.connectors.semrush.load_sources", return_value=cfg):
        conn = SemrushConnector(market="ng")
        conn._session = MagicMock()
        conn._session.get.side_effect = [ok_resp, err_resp]
        df = conn.fetch(api_key="tok")

    assert len(df) == 1
    assert conn._budget_exhausted is True
    assert conn._session.get.call_count == 2
