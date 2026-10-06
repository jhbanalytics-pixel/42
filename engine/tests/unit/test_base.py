"""Unit tests for BaseConnector."""

import pytest
import requests
import responses as responses_lib
from src.ingestion.connectors.base import BaseConnector


class ConcreteConnector(BaseConnector):
    """Minimal concrete subclass for testing the base class."""

    SOURCE_NAME = "test"
    PLATFORM = "test"

    def fetch(self, **kwargs):
        return self.empty_dataframe()


def make_connector(market="za"):
    return ConcreteConnector(market=market)


def test_empty_dataframe_has_correct_columns():
    """empty_dataframe() columns match RAW_COLUMNS exactly and in order."""
    from src.ingestion.connectors.base import RAW_COLUMNS

    df = ConcreteConnector.empty_dataframe()
    expected = list(RAW_COLUMNS)
    assert list(df.columns) == expected
    assert len(df) == 0


def test_safe_fetch_returns_empty_dataframe_on_exception():
    """safe_fetch() catches connector errors and returns an empty DataFrame."""

    class BrokenConnector(BaseConnector):
        SOURCE_NAME = "broken"
        PLATFORM = "test"

        def fetch(self, **kwargs):
            raise RuntimeError("Simulated connector failure")

    connector = BrokenConnector(market="za")
    df = connector.safe_fetch()
    assert len(df) == 0
    assert "source" in df.columns
    assert len(connector._fetch_failures) == 1
    assert "broken" in connector._fetch_failures[0]


def test_market_stored_on_connector():
    """Connector stores the market code passed to __init__."""
    for market in ("za", "ng", "ke"):
        connector = make_connector(market=market)
        assert connector.market == market


def test_source_name_and_platform_set_on_subclass():
    """Subclass SOURCE_NAME and PLATFORM are accessible on the instance."""
    connector = make_connector()
    assert connector.SOURCE_NAME == "test"
    assert connector.PLATFORM == "test"


def test_session_built_with_retry_adapter():
    """The session has retry-capable adapters mounted for http and https."""
    connector = make_connector()
    assert "https://" in connector._session.adapters
    assert "http://" in connector._session.adapters


@responses_lib.activate
def test_base_connector_retries_on_429():
    """The session retries on HTTP 429 and returns the final 200 response."""
    url = "https://example.test/retry-429"
    # Three 429s then a 200. The Retry strategy in _build_session allows
    # MAX_RETRIES=3, so the fourth call must land and succeed.
    for _ in range(3):
        responses_lib.add(responses_lib.GET, url, json={"ok": False}, status=429)
    responses_lib.add(responses_lib.GET, url, json={"ok": True}, status=200)

    connector = make_connector()
    # Zero out the rate-limit sleep so the test runs instantly.
    connector.RATE_LIMIT_DELAY = 0
    resp = connector._request("GET", url)

    assert resp.status_code == 200
    assert len(responses_lib.calls) == 4


@responses_lib.activate
def test_base_connector_surfaces_5xx_after_retries():
    """Persistent 500s exhaust retries and _request raises an HTTPError."""
    url = "https://example.test/always-500"
    for _ in range(6):
        responses_lib.add(responses_lib.GET, url, json={"err": True}, status=500)

    connector = make_connector()
    connector.RATE_LIMIT_DELAY = 0

    with pytest.raises(requests.exceptions.RequestException):
        connector._request("GET", url)


def test_base_connector_rate_limit_sleeps_between_calls(monkeypatch):
    """_request sleeps RATE_LIMIT_DELAY seconds between consecutive requests."""
    from src.ingestion.connectors import base as base_module

    sleep_calls: list[float] = []
    monkeypatch.setattr(base_module.time, "sleep", lambda s: sleep_calls.append(s))

    url = "https://example.test/rate-limit"

    @responses_lib.activate
    def run():
        responses_lib.add(responses_lib.GET, url, json={"ok": True}, status=200)
        responses_lib.add(responses_lib.GET, url, json={"ok": True}, status=200)

        connector = make_connector()
        connector.RATE_LIMIT_DELAY = 0.25
        connector._request("GET", url)
        connector._request("GET", url)

    run()

    # First request: no sleep (count was 0). Second request: one sleep of 0.25s.
    assert sleep_calls == [0.25]


def test_request_timeout_is_split_tuple():
    """A (connect, read) tuple, not the old scalar 30. A vendor hang costs one
    read timeout, not a retried multiple of it."""
    connector = make_connector()
    assert connector.REQUEST_TIMEOUT == (10, 30)


def test_request_passes_timeout_tuple(monkeypatch):
    """_request forwards REQUEST_TIMEOUT to session.request."""
    connector = make_connector()
    connector.RATE_LIMIT_DELAY = 0
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

    def _fake_request(method, url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Resp()

    monkeypatch.setattr(connector._session, "request", _fake_request)
    connector._request("GET", "https://example.test/x")
    assert captured["timeout"] == (10, 30)


def test_read_timeouts_are_not_retried():
    """The mounted Retry disables read retries so a hung endpoint fails once."""
    connector = make_connector()
    adapter = connector._session.get_adapter("https://example.test")
    retry = adapter.max_retries
    assert retry.read == 0
    # Status-code (429/5xx) retries are still allowed.
    assert retry.total == connector.MAX_RETRIES
