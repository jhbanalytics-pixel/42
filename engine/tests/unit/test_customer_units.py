"""Unit tests for src/ingestion/connectors/customer_units.py.

These helpers are 0-unit cost calls that engine_pulse uses to cross-check
the EnsembleData connector's internal ledger against vendor-side spend.
"""

import logging

import pytest
import responses as responses_lib
from src.ingestion.connectors.customer_units import (
    ENSEMBLE_BASE_URL,
    HISTORY_PATH,
    USED_UNITS_PATH,
    fetch_units_history,
    fetch_used_units,
)

FAKE_TOKEN = "fake-token-do-not-log-xyz"
USED_UNITS_URL = f"{ENSEMBLE_BASE_URL}{USED_UNITS_PATH}"
HISTORY_URL = f"{ENSEMBLE_BASE_URL}{HISTORY_PATH}"


# ---------------------------------------------------------------------------
# fetch_used_units
# ---------------------------------------------------------------------------


@responses_lib.activate
def test_fetch_used_units_happy_path():
    """Happy path returns the per-platform spend dict."""
    responses_lib.add(
        responses_lib.GET,
        USED_UNITS_URL,
        json={
            "data": {
                "tiktok": 1200,
                "instagram": 300,
                "reddit": 600,
                "youtube": 450,
                "twitch": 0,
            }
        },
        status=200,
    )
    out = fetch_used_units(FAKE_TOKEN, "2026-05-28")
    assert out == {
        "tiktok": 1200,
        "instagram": 300,
        "reddit": 600,
        "youtube": 450,
        "twitch": 0,
    }


def test_fetch_used_units_empty_token_returns_empty():
    """No token -> empty dict, no HTTP fired."""
    assert fetch_used_units("", "2026-05-28") == {}


def test_fetch_used_units_empty_date_returns_empty():
    """No date -> empty dict, no HTTP fired."""
    assert fetch_used_units(FAKE_TOKEN, "") == {}


@responses_lib.activate
def test_fetch_used_units_4xx_returns_empty():
    """4xx response -> empty dict, no exception bubbled."""
    responses_lib.add(responses_lib.GET, USED_UNITS_URL, json={"error": "bad"}, status=400)
    assert fetch_used_units(FAKE_TOKEN, "2026-05-28") == {}


@responses_lib.activate
def test_fetch_used_units_non_json_returns_empty():
    """Non-JSON response body -> empty dict."""
    responses_lib.add(responses_lib.GET, USED_UNITS_URL, body="not json", status=200)
    assert fetch_used_units(FAKE_TOKEN, "2026-05-28") == {}


@responses_lib.activate
def test_fetch_used_units_drops_non_int_values():
    """Non-int per-platform values are dropped rather than crashing."""
    responses_lib.add(
        responses_lib.GET,
        USED_UNITS_URL,
        json={"data": {"tiktok": 100, "instagram": "weird", "reddit": None}},
        status=200,
    )
    out = fetch_used_units(FAKE_TOKEN, "2026-05-28")
    assert out == {"tiktok": 100}


@responses_lib.activate
def test_fetch_used_units_does_not_log_token(caplog):
    """Token must never appear in any log record from the customer helper."""
    responses_lib.add(responses_lib.GET, USED_UNITS_URL, json={"data": {"tiktok": 1}}, status=200)
    with caplog.at_level(logging.DEBUG, logger="connector.ensemble.customer"):
        fetch_used_units(FAKE_TOKEN, "2026-05-28")
    for record in caplog.records:
        assert FAKE_TOKEN not in record.getMessage()
        assert FAKE_TOKEN not in str(record.msg)
        for arg in record.args or ():
            assert FAKE_TOKEN not in str(arg)


# ---------------------------------------------------------------------------
# fetch_units_history
# ---------------------------------------------------------------------------


@responses_lib.activate
def test_fetch_units_history_happy_path():
    """Happy path returns the list of daily spend dicts."""
    responses_lib.add(
        responses_lib.GET,
        HISTORY_URL,
        json={
            "data": [
                {"date": "2026-05-27", "tiktok": 500},
                {"date": "2026-05-28", "tiktok": 1200, "instagram": 300},
            ]
        },
        status=200,
    )
    out = fetch_units_history(FAKE_TOKEN, 2)
    assert len(out) == 2
    assert out[0]["date"] == "2026-05-27"
    assert out[1]["tiktok"] == 1200


@pytest.mark.parametrize(
    ("token", "days"),
    [("", 7), (FAKE_TOKEN, 0), (FAKE_TOKEN, -1), ("", 0)],
)
def test_fetch_units_history_invalid_inputs_return_empty(token, days):
    """Empty token or non-positive days -> empty list, no HTTP fired."""
    assert fetch_units_history(token, days) == []


@responses_lib.activate
def test_fetch_units_history_4xx_returns_empty():
    """4xx -> empty list, no exception."""
    responses_lib.add(responses_lib.GET, HISTORY_URL, json={"error": "x"}, status=403)
    assert fetch_units_history(FAKE_TOKEN, 7) == []


@responses_lib.activate
def test_fetch_units_history_malformed_data_returns_empty():
    """data key not a list -> empty list."""
    responses_lib.add(responses_lib.GET, HISTORY_URL, json={"data": {"not": "a list"}}, status=200)
    assert fetch_units_history(FAKE_TOKEN, 7) == []


@responses_lib.activate
def test_fetch_units_history_filters_non_dict_entries():
    """Non-dict entries in the data array are skipped."""
    responses_lib.add(
        responses_lib.GET,
        HISTORY_URL,
        json={"data": [{"date": "2026-05-27", "tiktok": 1}, "garbage", 42]},
        status=200,
    )
    out = fetch_units_history(FAKE_TOKEN, 3)
    assert len(out) == 1
    assert out[0]["date"] == "2026-05-27"


@responses_lib.activate
def test_fetch_units_history_does_not_log_token(caplog):
    """Token must never appear in any log record from the history helper."""
    responses_lib.add(responses_lib.GET, HISTORY_URL, json={"data": []}, status=200)
    with caplog.at_level(logging.DEBUG, logger="connector.ensemble.customer"):
        fetch_units_history(FAKE_TOKEN, 7)
    for record in caplog.records:
        assert FAKE_TOKEN not in record.getMessage()
        assert FAKE_TOKEN not in str(record.msg)
        for arg in record.args or ():
            assert FAKE_TOKEN not in str(arg)
