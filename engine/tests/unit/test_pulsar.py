"""Unit tests for the Pulsar connector. No network: requests.Session is patched
to return canned GraphQL envelopes. The safe-disconnect contract (flag off ->
zero calls) is the most important thing pinned here."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.ingestion.connectors.pulsar import PulsarConnector


def _cfg(**over):
    c = {
        "enabled": True,
        "endpoint": "https://data.pulsarplatform.com/graphql/trac",
        "budget_requests_per_run": 12,
        "max_posts": 100,
        "markets": {"za": {"search_hashes": ["abc123"]}},
    }
    c.update(over)
    return {"pulsar": c}


def _post(url="https://x.com/p/1", text="Amapiano is moving", source="X", handle="djx"):
    return {
        "url": url,
        "content": text,
        "source": source,
        "publishedAt": "2026-07-14T09:00:00Z",
        "userName": "DJ X",
        "userScreenName": handle,
        "sentiment": "1.0",
        "engagement": 120,
        "likesCount": 80,
        "sharesCount": 10,
        "commentsCount": 5,
        "viewsCount": 3000,
        "countryName": "South Africa",
        "language": "en",
    }


def _resp(posts, total=None, errors=None):
    r = MagicMock()
    r.raise_for_status.return_value = None
    body = {
        "data": {
            "results": {
                "total": total if total is not None else len(posts),
                "nextCursor": None,
                "results": posts,
            }
        }
    }
    if errors is not None:
        body = {"errors": errors, "data": None}
    r.json.return_value = body
    return r


def _session(responses):
    s = MagicMock()
    s.headers = {}
    s.post.side_effect = responses
    return s


def _mk(market="za"):
    PulsarConnector.reset_requests()
    return PulsarConnector(market=market)


# --- the safe-disconnect contract ------------------------------------------


@patch("src.ingestion.connectors.pulsar.load_sources")
def test_flag_off_makes_no_network_call(mock_sources):
    mock_sources.return_value = _cfg(enabled=False)
    conn = _mk()
    with patch("requests.Session") as m_session:
        df = conn.fetch()
    assert df.empty
    # The whole point: disabled -> Session never even constructed.
    m_session.assert_not_called()


@patch("src.ingestion.connectors.pulsar.load_sources")
def test_missing_config_key_is_inert(mock_sources):
    mock_sources.return_value = {}  # no pulsar block at all
    conn = _mk()
    with patch("requests.Session") as m_session:
        df = conn.fetch()
    assert df.empty
    m_session.assert_not_called()


@patch("src.ingestion.connectors.pulsar.get_secret", return_value="")
@patch("src.ingestion.connectors.pulsar.load_sources")
def test_missing_token_degrades_not_raises(mock_sources, _secret):
    mock_sources.return_value = _cfg()
    conn = _mk()
    import os

    old = os.environ.pop("PULSAR_API_TOKEN", None)
    try:
        df = conn.fetch()
    finally:
        if old is not None:
            os.environ["PULSAR_API_TOKEN"] = old
    assert df.empty
    assert any("PULSAR_API_TOKEN missing" in f for f in conn._fetch_failures)


@patch("src.ingestion.connectors.pulsar.get_secret", return_value="tok")
@patch("src.ingestion.connectors.pulsar.load_sources")
def test_no_search_hashes_for_market_is_inert(mock_sources, _secret):
    mock_sources.return_value = _cfg(markets={"za": {"search_hashes": []}})
    conn = _mk()
    df = conn.fetch()
    assert df.empty


# --- the happy path --------------------------------------------------------


@patch("src.ingestion.connectors.pulsar.get_secret", return_value="tok")
@patch("src.ingestion.connectors.pulsar.load_sources")
def test_posts_normalize_to_raw_columns(mock_sources, _secret):
    mock_sources.return_value = _cfg()
    conn = _mk()
    with patch("requests.Session", return_value=_session([_resp([_post()])])):
        df = conn.fetch()
    assert len(df) == 1
    row = df.iloc[0]
    assert row["source"] == "pulsar"
    assert row["platform"] == "x"  # channel surfaced as the real platform
    assert row["market"] == "za"
    assert row["query_group"] == "pulsar"
    assert row["text"] == "Amapiano is moving"
    assert float(row["likes"]) == 80.0


@patch("src.ingestion.connectors.pulsar.get_secret", return_value="tok")
@patch("src.ingestion.connectors.pulsar.load_sources")
def test_browser_user_agent_is_set(mock_sources, _secret):
    """The Cloudflare WAF 403s a default UA; the connector must send a browser UA."""
    mock_sources.return_value = _cfg()
    conn = _mk()
    sess = _session([_resp([_post()])])
    with patch("requests.Session", return_value=sess):
        conn.fetch()
    ua = sess.headers.get("User-Agent", "")
    assert "Mozilla" in ua


@patch("src.ingestion.connectors.pulsar.get_secret", return_value="tok")
@patch("src.ingestion.connectors.pulsar.load_sources")
def test_graphql_errors_degrade_not_raise(mock_sources, _secret):
    mock_sources.return_value = _cfg()
    conn = _mk()
    err = [{"message": "Something went wrong", "path": ["results"]}]
    with patch("requests.Session", return_value=_session([_resp([], errors=err)])):
        df = conn.fetch()
    assert df.empty
    assert any("abc123" in f for f in conn._fetch_failures)


@patch("src.ingestion.connectors.pulsar.get_secret", return_value="tok")
@patch("src.ingestion.connectors.pulsar.load_sources")
def test_request_budget_caps_calls(mock_sources, _secret):
    mock_sources.return_value = _cfg(
        budget_requests_per_run=1,
        markets={"za": {"search_hashes": ["h1", "h2", "h3"]}},
    )
    conn = _mk()
    sess = _session([_resp([_post()]), _resp([_post()]), _resp([_post()])])
    with patch("requests.Session", return_value=sess):
        conn.fetch()
    # budget 1 -> only one POST made despite three hashes.
    assert sess.post.call_count == 1


@patch("src.ingestion.connectors.pulsar.get_secret", return_value="tok")
@patch("src.ingestion.connectors.pulsar.load_sources")
def test_network_error_degrades_not_raises(mock_sources, _secret):
    import requests

    mock_sources.return_value = _cfg()
    conn = _mk()
    sess = MagicMock()
    sess.headers = {}
    sess.post.side_effect = requests.exceptions.Timeout("hang")
    with patch("requests.Session", return_value=sess):
        df = conn.fetch()
    assert df.empty
    assert any("abc123" in f for f in conn._fetch_failures)
