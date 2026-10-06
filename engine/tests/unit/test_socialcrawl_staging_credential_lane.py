"""The unfunded SocialCrawl lane on staging refuses out loud.

On staging the jhb_core lane would read SOCIALCRAWL_API_KEY, whose account
holds no credits, and a missing or dry key there used to come back as an empty
frame that read like a quiet source. Staging collection is funded collection:
the governed lane reads SOCIALCRAWL_OGILVY_API_KEY under its own allowance,
caps and authority hooks, and only the producer can prepare that lane. So the
connector refuses the unfunded lane on staging before any secret is read or
any vendor call is made, logs the refusal at ERROR, and records it as a named
fetch failure the producer writes into the run's errors and diagnostics.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from src.ingestion.connectors.socialcrawl import (
    STAGING_UNFUNDED_REFUSAL,
    SocialCrawlConnector,
)


def _cfg():
    return {
        "socialcrawl": {
            "enabled": True,
            "budget_credits_per_run": 100,
            "phases": ["search"],
            "caps": {"search_terms": 1, "threads_terms": 0},
            "markets": {"za": {"terms": ["amapiano"]}},
        }
    }


@contextmanager
def _error_records():
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("connector.socialcrawl")
    sink = _Sink(level=logging.DEBUG)
    logger.addHandler(sink)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous)


def _fetch_on_staging(monkeypatch, *, secret_value, lane=None, sources=None):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    if lane is None:
        monkeypatch.delenv("SOCIALCRAWL_CREDENTIAL_LANE", raising=False)
    else:
        monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", lane)
    SocialCrawlConnector.reset_credits()
    secret_calls: list[str] = []

    def _secret(secret_id, *_args, **_kwargs):
        secret_calls.append(secret_id)
        return secret_value

    session = MagicMock()
    session.headers = {}
    with (
        patch(
            "src.ingestion.connectors.socialcrawl.load_sources",
            return_value=sources if sources is not None else _cfg(),
        ),
        patch("src.ingestion.connectors.socialcrawl.get_secret", side_effect=_secret),
        patch("requests.Session", return_value=session),
        _error_records() as records,
    ):
        connector = SocialCrawlConnector(market="za")
        frame = connector.fetch()
    return connector, frame, secret_calls, session, records


@pytest.mark.parametrize("secret_value", ["zero_balance_key", ""])
@pytest.mark.parametrize("lane", [None, "jhb_core"])
def test_staging_unfunded_lane_refuses_before_reading_any_secret(monkeypatch, secret_value, lane):
    connector, frame, secret_calls, session, records = _fetch_on_staging(
        monkeypatch, secret_value=secret_value, lane=lane
    )

    assert frame.empty
    assert secret_calls == []
    session.get.assert_not_called()
    assert connector._fetch_failures == [STAGING_UNFUNDED_REFUSAL]
    errors = [r for r in records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == [STAGING_UNFUNDED_REFUSAL]


def test_the_refusal_names_the_zero_balance_secret_and_the_governed_lane():
    assert STAGING_UNFUNDED_REFUSAL.startswith("socialcrawl: ")
    assert "SOCIALCRAWL_API_KEY" in STAGING_UNFUNDED_REFUSAL
    assert "SOCIALCRAWL_OGILVY_API_KEY" in STAGING_UNFUNDED_REFUSAL
    assert "SOCIALCRAWL_CREDENTIAL_LANE=ogilvy_funded" in STAGING_UNFUNDED_REFUSAL
    assert "missing" not in STAGING_UNFUNDED_REFUSAL


def test_the_refusal_is_not_the_missing_key_degradation(monkeypatch):
    connector, _frame, _calls, _session, _records = _fetch_on_staging(monkeypatch, secret_value="")

    assert not any("SOCIALCRAWL_API_KEY missing" in f for f in connector._fetch_failures)


def test_a_disabled_source_on_staging_is_still_just_disabled(monkeypatch):
    sources = _cfg()
    sources["socialcrawl"]["enabled"] = False
    connector, frame, secret_calls, session, records = _fetch_on_staging(
        monkeypatch, secret_value="zero_balance_key", sources=sources
    )

    assert frame.empty
    assert secret_calls == []
    session.get.assert_not_called()
    assert connector._fetch_failures == []
    assert not [r for r in records if r.levelno >= logging.ERROR]


def test_the_funded_lane_on_staging_is_not_refused_by_this_gate(monkeypatch):
    # Without a prepared funded context the funded lane keeps its own refusal;
    # this gate is only for the unfunded lane.
    connector, frame, secret_calls, _session, _records = _fetch_on_staging(
        monkeypatch, secret_value="funded_key", lane="ogilvy_funded"
    )

    assert frame.empty
    assert secret_calls == []
    assert connector._fetch_failures == ["socialcrawl: funded lane preflight is missing"]


def test_outside_staging_the_unfunded_lane_still_reads_its_own_key(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "dev")
    monkeypatch.delenv("SOCIALCRAWL_CREDENTIAL_LANE", raising=False)
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "")
    SocialCrawlConnector.reset_credits()
    secret_calls: list[str] = []

    def _secret(secret_id, *_args, **_kwargs):
        secret_calls.append(secret_id)
        return ""

    with (
        patch("src.ingestion.connectors.socialcrawl.load_sources", return_value=_cfg()),
        patch("src.ingestion.connectors.socialcrawl.get_secret", side_effect=_secret),
    ):
        connector = SocialCrawlConnector(market="za")
        frame = connector.fetch()

    assert frame.empty
    assert secret_calls == ["SOCIALCRAWL_API_KEY"]
    assert connector._fetch_failures == ["socialcrawl: SOCIALCRAWL_API_KEY missing"]
