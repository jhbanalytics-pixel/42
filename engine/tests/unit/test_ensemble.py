"""Unit tests for EnsembleConnector."""

import logging
from datetime import UTC
from unittest.mock import patch

import pytest
import responses as responses_lib
from src.ingestion.connectors.ensemble import DEFAULT_ENDPOINTS, EnsembleConnector


@pytest.fixture(autouse=True)
def _reset_global_budget():
    """Reset the process-wide Ensemble budget ledger before each test."""
    EnsembleConnector.reset_global_budget()
    yield
    EnsembleConnector.reset_global_budget()


FAKE_TOKEN = "test-token-never-log-me-xyz"

BASE = "https://ensembledata.com/apis"
TT_HASHTAG_URL = f"{BASE}/tt/hashtag/posts"
TT_KEYWORD_URL = f"{BASE}/tt/keyword/search"
IG_HASHTAG_URL = f"{BASE}/instagram/hashtag/posts"

from src.ingestion.connectors.base import RAW_COLUMNS


def _sources(terms: dict, budget: int = 100, endpoints: list | None = None) -> dict:
    """Build a minimal sources.yaml dict using the V2 ``ensemble`` block shape."""
    block: dict = {"budget_units_per_run": budget, "markets": {"za": terms}}
    if endpoints is not None:
        block["endpoints"] = endpoints
    return {"ensemble": block}


def _tiktok_response(units_charged: int = 1, count: int = 1) -> dict:
    """Build a fake EnsembleData payload with ``units_charged`` and a post list."""
    posts = [
        {
            "id": f"vid{i}",
            "aweme_id": f"vid{i}",
            "desc": f"Amapiano vibes {i}. #amapiano",
            "create_time": 1_713_600_000 + i,
            "play_count": 10_000 + i,
            "digg_count": 500 + i,
            "comment_count": 50 + i,
            "share_count": 5 + i,
            "author": {
                "nickname": f"Creator {i}",
                "unique_id": f"creator_{i}",
            },
        }
        for i in range(count)
    ]
    return {"units_charged": units_charged, "data": posts}


def _mk_connector(market: str = "za") -> EnsembleConnector:
    c = EnsembleConnector(market=market)
    c.RATE_LIMIT_DELAY = 0
    return c


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_fetch_returns_raw_columns(mock_load_sources, _mock_secret):
    """fetch() returns the 16-column RAW_COLUMNS schema on a happy-path response."""
    mock_load_sources.return_value = _sources({"tiktok_hashtags": ["amapiano"]})

    responses_lib.add(
        responses_lib.GET,
        TT_HASHTAG_URL,
        json=_tiktok_response(units_charged=1, count=2),
        status=200,
    )

    df = _mk_connector().fetch()

    assert list(df.columns) == RAW_COLUMNS
    assert len(df) == 2
    assert (df["source"] == "EnsembleData").all()
    assert (df["platform"] == "tiktok").all()
    assert (df["market"] == "za").all()
    assert (df["content_type"] == "post").all()
    assert (df["query_term"] == "amapiano").all()


# ---------------------------------------------------------------------------
# Missing token
# ---------------------------------------------------------------------------


@patch("src.ingestion.connectors.ensemble.get_secret", return_value="")
@patch("src.ingestion.connectors.ensemble.load_sources", return_value={"ensemble": {}})
def test_fetch_when_token_missing_returns_empty(_mock_sources, _mock_secret):
    """fetch() returns an empty DataFrame when the API token is unset."""
    df = _mk_connector().fetch()
    assert len(df) == 0
    assert list(df.columns) == RAW_COLUMNS


# ---------------------------------------------------------------------------
# HTTP 495 circuit breaker
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_http_495_triggers_circuit_breaker(mock_load_sources, _mock_secret):
    """HTTP 495 flips _quota_exhausted, returns (not raises), stops further calls."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano", "mzansi"],
            "tiktok_keywords": ["south africa gen z"],
        }
    )

    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json={"error": "quota"}, status=495)

    connector = _mk_connector()
    df = connector.fetch()

    # Did not crash and returned a schema-shaped DataFrame.
    assert list(df.columns) == RAW_COLUMNS
    assert len(df) == 0
    # Circuit breaker tripped.
    assert connector._quota_exhausted is True
    # Only one call should have been made ,  the breaker stopped further terms.
    assert len(responses_lib.calls) == 1


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_generic_4xx_skips_term_without_tripping_breaker(mock_load_sources, _mock_secret):
    """A 422 on the first term logs a warning, returns [], and does NOT trip the
    circuit breaker, so the next term still fires and its rows survive."""
    mock_load_sources.return_value = _sources({"tiktok_hashtags": ["amapiano", "mzansi"]})

    # responses returns registered mocks FIFO: first term gets the 422, second
    # gets the 200.
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json={"error": "bad"}, status=422)
    responses_lib.add(
        responses_lib.GET,
        TT_HASHTAG_URL,
        json=_tiktok_response(units_charged=1, count=1),
        status=200,
    )

    connector = _mk_connector()
    df = connector.fetch()

    # Both terms fired: the 422 skip did not short-circuit the loop.
    assert len(responses_lib.calls) == 2
    # Generic 4xx is not the quota/auth sentinel, so the breaker stays closed.
    assert connector._quota_exhausted is False
    # The second term's row survived.
    assert len(df) == 1
    assert (df["query_term"] == "mzansi").all()


# ---------------------------------------------------------------------------
# Budget caps the number of calls
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_budget_caps_request_count(mock_load_sources, _mock_secret):
    """A budget of 10 units with responses costing 5 each allows only 2 calls."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano", "mzansi", "softlife", "jozi", "varsity"],
        },
        budget=10,
    )

    for _ in range(5):
        responses_lib.add(
            responses_lib.GET,
            TT_HASHTAG_URL,
            json=_tiktok_response(units_charged=5, count=1),
            status=200,
        )

    connector = _mk_connector()
    connector.fetch()

    assert len(responses_lib.calls) == 2
    assert connector._units_spent == 10


# ---------------------------------------------------------------------------
# Token must never appear in log output
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_token_not_leaked_in_logs(mock_load_sources, _mock_secret, caplog):
    """No log record emitted during fetch contains the raw token value."""
    mock_load_sources.return_value = _sources({"tiktok_hashtags": ["amapiano"]})
    responses_lib.add(
        responses_lib.GET,
        TT_HASHTAG_URL,
        json=_tiktok_response(units_charged=1, count=1),
        status=200,
    )

    with caplog.at_level(logging.DEBUG, logger="connector.ensemble"):
        _mk_connector().fetch()

    for record in caplog.records:
        rendered = record.getMessage()
        assert FAKE_TOKEN not in rendered, f"Token leaked in log record: {rendered!r}"
        # The raw format string should not include the token either.
        assert FAKE_TOKEN not in str(record.msg)
        for arg in record.args or ():
            assert FAKE_TOKEN not in str(arg)

    # And responses-captured call URLs will contain the token (that's expected at
    # the wire level) ,  but the request must be made with the token as a query
    # param, not a logged URL.
    assert any(FAKE_TOKEN in call.request.url for call in responses_lib.calls)


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_maps_tiktok_post_correctly(mock_load_sources, _mock_secret):
    """A known TikTok payload maps to the expected 16-column row shape."""
    mock_load_sources.return_value = _sources({"tiktok_hashtags": ["amapiano"]})

    payload = {
        "units_charged": 1,
        "data": [
            {
                "id": "7200000000000000001",
                "aweme_id": "7200000000000000001",
                "desc": "Amapiano set this weekend, linktree in bio.",
                "create_time": 1_713_600_000,  # 2024-04-20 08:00:00 UTC
                "play_count": 123_456,
                "digg_count": 9_876,
                "comment_count": 321,
                "share_count": 45,
                "author": {
                    "nickname": "DJ Test",
                    "unique_id": "djtest",
                },
            }
        ],
    }
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=payload, status=200)

    df = _mk_connector().fetch()

    assert len(df) == 1
    row = df.iloc[0]
    assert row["source"] == "EnsembleData"
    assert row["platform"] == "tiktok"
    assert row["market"] == "za"
    assert row["content_type"] == "post"
    assert row["query_group"] == "tiktok_hashtag"
    assert row["query_term"] == "amapiano"
    assert row["author_name"] == "DJ Test"
    assert row["author_handle"] == "djtest"
    assert row["text"] == "Amapiano set this weekend, linktree in bio."
    assert row["title"] == "Amapiano set this weekend, linktree in bio."[:100]
    assert "tiktok.com/@djtest/video/7200000000000000001" in row["url"]
    assert row["views"] == 123_456
    assert row["likes"] == 9_876
    assert row["comments"] == 321
    assert row["shares"] == 45
    # Timestamp converts from Unix epoch to a tz-aware UTC datetime.
    assert row["published_at"] is not None
    assert row["published_at"].tzinfo is not None


# ---------------------------------------------------------------------------
# URL redaction helper
# ---------------------------------------------------------------------------


def test_safe_url_strips_querystring():
    """_safe_url removes everything after the '?' so tokens cannot leak."""
    raw = "https://ensembledata.com/apis/tt/hashtag/posts?name=amapiano&token=SECRET"
    assert EnsembleConnector._safe_url(raw) == ("https://ensembledata.com/apis/tt/hashtag/posts")


# ---------------------------------------------------------------------------
# _extract_timestamp never returns a raw string
# ---------------------------------------------------------------------------


def test_extract_timestamp_returns_none_on_parse_failure():
    """Garbage strings produce None, never the raw input.

    raw_content.published_at is a TIMESTAMP column; a string leak there
    would break the BigQuery load with a mixed-type Series.
    """
    from datetime import datetime

    assert EnsembleConnector._extract_timestamp({"create_time": "not-a-date"}) is None
    assert EnsembleConnector._extract_timestamp({"create_time": "2024/04/20"}) is None
    assert EnsembleConnector._extract_timestamp({"create_time": ""}) is None
    assert EnsembleConnector._extract_timestamp({"create_time": None}) is None
    assert EnsembleConnector._extract_timestamp({"create_time": {"nested": 1}}) is None

    # Happy paths still work and return tz-aware UTC.
    epoch_result = EnsembleConnector._extract_timestamp({"create_time": 1_713_600_000})
    assert isinstance(epoch_result, datetime)
    assert epoch_result.tzinfo is not None

    # Naive ISO string gets coerced to UTC, not returned raw or as a naive datetime.
    naive_iso_result = EnsembleConnector._extract_timestamp({"create_time": "2024-04-20T08:00:00"})
    assert isinstance(naive_iso_result, datetime)
    assert naive_iso_result.tzinfo is not None
    assert naive_iso_result.utcoffset() == UTC.utcoffset(naive_iso_result)

    # "Z" suffix still works.
    z_result = EnsembleConnector._extract_timestamp({"create_time": "2024-04-20T08:00:00Z"})
    assert isinstance(z_result, datetime)
    assert z_result.tzinfo is not None


def test_extract_timestamp_parses_twitter_formats():
    """Twitter's created_at is 'Wed Apr 20 12:00:00 +0000 2024' (rejected by
    fromisoformat) and nests under legacy.created_at in the GraphQL shape.
    Both must parse to tz-aware UTC so tweets keep a real published_at."""
    from datetime import datetime

    twitter_str = "Wed Apr 20 12:00:00 +0000 2024"
    flat = EnsembleConnector._extract_timestamp({"created_at": twitter_str})
    assert isinstance(flat, datetime)
    assert flat.tzinfo is not None
    assert flat.year == 2024
    assert flat.hour == 12

    nested = EnsembleConnector._extract_timestamp({"legacy": {"created_at": twitter_str}})
    assert isinstance(nested, datetime)
    assert nested.tzinfo is not None


# ---------------------------------------------------------------------------
# Rate limit from BaseConnector._request is respected
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_ensemble_rate_limit_respected(mock_load_sources, _mock_secret, monkeypatch):
    """Ensemble routes HTTP calls through BaseConnector._request, which sleeps
    RATE_LIMIT_DELAY between consecutive calls."""
    from src.ingestion.connectors import base as base_module

    sleep_calls: list[float] = []
    monkeypatch.setattr(base_module.time, "sleep", lambda s: sleep_calls.append(s))

    mock_load_sources.return_value = _sources(
        {"tiktok_hashtags": ["amapiano", "mzansi", "softlife"]}
    )
    for _ in range(3):
        responses_lib.add(
            responses_lib.GET,
            TT_HASHTAG_URL,
            json=_tiktok_response(units_charged=1, count=1),
            status=200,
        )

    connector = EnsembleConnector(market="za")
    # Non-zero delay so we can observe that sleep is called between calls.
    connector.RATE_LIMIT_DELAY = 0.25
    connector.fetch()

    # 3 requests: no sleep before the first, one sleep before each subsequent.
    assert sleep_calls == [0.25, 0.25]


# ---------------------------------------------------------------------------
# 429 handling via the BaseConnector retry adapter
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_429_handled_via_retry_or_circuit_breaker(mock_load_sources, _mock_secret, monkeypatch):
    """429 is in BaseConnector.RETRY_STATUS_CODES, so the retry adapter retries
    with backoff. 3 x 429 then 200 lands as a single successful term fetch."""
    from src.ingestion.connectors import base as base_module

    # Kill both the base-level rate-limit sleep AND urllib3's internal Retry
    # backoff sleep so the test is instant.
    monkeypatch.setattr(base_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr("urllib3.util.retry.Retry.sleep", lambda *_a, **_k: None)

    mock_load_sources.return_value = _sources({"tiktok_hashtags": ["amapiano"]})

    for _ in range(3):
        responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json={}, status=429)
    responses_lib.add(
        responses_lib.GET,
        TT_HASHTAG_URL,
        json=_tiktok_response(units_charged=1, count=1),
        status=200,
    )

    connector = EnsembleConnector(market="za")
    connector.RATE_LIMIT_DELAY = 0
    df = connector.fetch()

    # Four calls fired: three 429s, one 200. Circuit breaker never trips.
    assert len(responses_lib.calls) == 4
    assert connector._quota_exhausted is False
    assert len(df) == 1


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_http_493_triggers_circuit_breaker(mock_load_sources, _mock_secret):
    """HTTP 493 is auth/billing suspension, not quota. Breaker trips, logs ERROR."""
    EnsembleConnector.reset_global_budget()
    mock_load_sources.return_value = _sources({"tiktok_hashtags": ["amapiano", "mzansi"]})
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json={"error": "auth"}, status=493)

    connector = _mk_connector()
    df = connector.fetch()

    assert list(df.columns) == RAW_COLUMNS
    assert len(df) == 0
    assert connector._quota_exhausted is True
    assert len(responses_lib.calls) == 1


def test_budget_is_shared_across_instances():
    """Global budget: second market's connector inherits first's spend."""
    EnsembleConnector.reset_global_budget()
    c1 = EnsembleConnector(market="za")
    c2 = EnsembleConnector(market="ng")
    c1._units_spent = 42
    assert c2._units_spent == 42
    c1._quota_exhausted = True
    assert c2._quota_exhausted is True
    EnsembleConnector.reset_global_budget()
    assert c1._units_spent == 0
    assert c2._quota_exhausted is False


def test_fallback_path_reads_budget_from_ensemble_budget():
    """The live sources.yaml shape (ensembledata + ensemble_budget, no top-level
    'ensemble' key) now feeds budget_units_per_run and per_market_units through,
    instead of silently hardcoding the 800 default."""
    sources = {
        "ensembledata": {"za": {"tiktok_hashtags": ["amapiano"]}},
        "ensemble_budget": {"budget_units_per_run": 2400, "per_market_units": 800},
    }
    config = EnsembleConnector(market="za")._resolve_ensemble_config(sources)
    assert config["budget_units_per_run"] == 2400
    assert config["per_market_units"] == 800
    assert config["terms"] == {"tiktok_hashtags": ["amapiano"]}


# --- Part C: CREATOR_INGEST_BOOST (default off) ----------------------------


def test_creator_boost_raises_budget_when_flag_on(monkeypatch):
    """CREATOR_INGEST_BOOST on: the effective per-run budget jumps to the boost
    default so the creator user-endpoints have room to fire after hashtag and
    keyword instead of being starved by the 800 cap."""
    monkeypatch.setenv("CREATOR_INGEST_BOOST", "true")
    sources = {
        "ensembledata": {"za": {"tiktok_hashtags": ["amapiano"]}},
        "ensemble_budget": {"budget_units_per_run": 800, "per_market_units": 800},
    }
    config = EnsembleConnector(market="za")._resolve_ensemble_config(sources)
    assert config["budget_units_per_run"] == 1500


def test_creator_boost_budget_unchanged_when_flag_off(monkeypatch):
    """Default (flag off): the live 800 budget is untouched, run byte-identical."""
    monkeypatch.delenv("CREATOR_INGEST_BOOST", raising=False)
    sources = {
        "ensembledata": {"za": {"tiktok_hashtags": ["amapiano"]}},
        "ensemble_budget": {"budget_units_per_run": 800, "per_market_units": 800},
    }
    config = EnsembleConnector(market="za")._resolve_ensemble_config(sources)
    assert config["budget_units_per_run"] == 800


def test_creator_boost_keeps_operator_budget_and_sub_cap(monkeypatch):
    """Under boost, an operator budget higher than the boost default wins, and
    the per_market_units sub-cap stays live so one market cannot spend the whole
    shared budget and starve the others. The live config raised these to
    3000 / 1000 for real volume, and boost was silently ignoring them (the
    per-market cap defaulted to the full budget, i.e. no sub-cap)."""
    monkeypatch.setenv("CREATOR_INGEST_BOOST", "true")
    sources = {
        "ensembledata": {"za": {"tiktok_hashtags": ["amapiano"]}},
        "ensemble_budget": {"budget_units_per_run": 3000, "per_market_units": 1000},
    }
    config = EnsembleConnector(market="za")._resolve_ensemble_config(sources)
    assert config["budget_units_per_run"] == 3000
    assert config["per_market_units"] == 1000


def test_creator_boost_orders_user_endpoints_first_when_flag_on(monkeypatch):
    """CREATOR_INGEST_BOOST on: user endpoints are prepended so watchlisted
    creators get budget priority over hashtag and keyword."""
    monkeypatch.setenv("CREATOR_INGEST_BOOST", "true")
    c = EnsembleConnector(market="za")
    terms = {"creator_endpoints_enabled": True, "creator_tier_cap": "tier_1"}
    endpoints = [
        {
            "name": "tiktok_hashtag",
            "path": "/tt/hashtag/posts",
            "platform": "tiktok",
            "terms_key": "tiktok_hashtags",
        }
    ]
    c._maybe_wire_creator_endpoints(terms, endpoints)
    names = [e["name"] for e in endpoints]
    assert names.index("tiktok_user") < names.index("tiktok_hashtag")


def test_creator_endpoints_appended_last_when_flag_off(monkeypatch):
    """Default (flag off): user endpoints stay appended last, exactly as today."""
    monkeypatch.delenv("CREATOR_INGEST_BOOST", raising=False)
    c = EnsembleConnector(market="za")
    terms = {"creator_endpoints_enabled": True, "creator_tier_cap": "tier_1"}
    endpoints = [
        {
            "name": "tiktok_hashtag",
            "path": "/tt/hashtag/posts",
            "platform": "tiktok",
            "terms_key": "tiktok_hashtags",
        }
    ]
    c._maybe_wire_creator_endpoints(terms, endpoints)
    names = [e["name"] for e in endpoints]
    assert names.index("tiktok_user") > names.index("tiktok_hashtag")


def test_creator_boost_defaults_to_tier1_when_flag_on(monkeypatch):
    """CREATOR_INGEST_BOOST on with no explicit cap: defaults to tier_1, the safe
    ~66-call first flip, not the full tier_2 roster."""
    monkeypatch.setenv("CREATOR_INGEST_BOOST", "true")
    c = EnsembleConnector(market="za")
    terms = {"creator_endpoints_enabled": True, "creator_tier_cap": "tier_1"}
    endpoints: list = []
    c._maybe_wire_creator_endpoints(terms, endpoints)
    # za tiktok tier_1 = 9; tier_2 would be >50. Default must stay in the tier_1 band.
    assert 0 < len(terms["tiktok_user_handles"]) <= 12


def test_creator_boost_uses_tier2_when_explicitly_configured(monkeypatch):
    """CREATOR_INGEST_BOOST on plus creator_tier_cap_boost: tier_2 widens beyond
    tier_1 alone (cumulative tiers), the budget-approved escalation.

    ZA tier_2 was trimmed 1 Jul 2026 from ~93 tiktok handles (mostly an 11 Jun
    client seeding blob of micro-influencers) down to 14 curated ones, so the
    cumulative tier_1+tier_2 count is 23, not the old 100+. See
    configs/creators/za.yaml.
    """
    monkeypatch.setenv("CREATOR_INGEST_BOOST", "true")
    c = EnsembleConnector(market="za")
    tier1_terms = {"creator_endpoints_enabled": True, "creator_tier_cap_boost": "tier_1"}
    c._maybe_wire_creator_endpoints(tier1_terms, [])
    tier1_count = len(tier1_terms["tiktok_user_handles"])

    terms = {"creator_endpoints_enabled": True, "creator_tier_cap_boost": "tier_2"}
    endpoints: list = []
    c._maybe_wire_creator_endpoints(terms, endpoints)
    assert len(terms["tiktok_user_handles"]) > tier1_count


def test_live_config_keeps_all_markets_tier1_pending_userposts_shadow():
    """Creator /user/posts was 422-broken until the 16-Jun vendor-contract fix
    (tiktok username+depth, instagram user_id+depth+resolve; threads deferred).
    The calls were previously free no-ops; now they fire and charge real units,
    and instagram costs an extra resolve call per handle. So the live config held
    ALL markets at tier_1 until a real-units shadow sized the tier_2 widen against
    the 5000/day Bronze cap. That shadow ran as a 3-day in-band baseline (account
    spend held ~2500/5000 across 2026-07-07..10), so NG is now flipped to
    creator_tier_cap_boost: tier_2 (the trimmed 41-handle pool). ZA and KE stay
    tier_1 pending their own baselines. Guards against an unsanctioned tier_2 flip
    landing on ZA or KE before their shadows run."""
    from pathlib import Path

    import yaml

    sources = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "configs" / "sources.yaml").read_text(
            encoding="utf-8"
        )
    )
    ed = sources["ensembledata"]
    for market in ("za", "ke"):
        assert "creator_tier_cap_boost" not in ed[market]
        assert ed[market].get("creator_tier_cap") == "tier_1"
    assert ed["ng"].get("creator_tier_cap") == "tier_1"
    assert ed["ng"].get("creator_tier_cap_boost") == "tier_2"


def test_per_market_units_caps_market_while_global_has_room():
    """A market that used its per-market slice stops even with global headroom,
    so a later market can still ingest from the shared ledger."""
    EnsembleConnector.reset_global_budget()
    spent = EnsembleConnector(market="za")
    spent._per_market_budget = 10
    spent._units_spent = 10  # this market and the global ledger both at 10
    assert spent._budget_reached(budget=100) is True  # market slice full

    later = EnsembleConnector(market="ke")
    later._per_market_budget = 10
    assert later._units_spent == 10  # shares the global ledger
    assert later._market_units_spent == 0  # but its own tally is fresh
    assert later._budget_reached(budget=100) is False  # room to ingest


def test_per_market_default_matches_global_only_check():
    """With per_market_units defaulting to the full budget, _budget_reached is
    equivalent to the old global-only check, so a run stays byte-identical."""
    EnsembleConnector.reset_global_budget()
    c = EnsembleConnector(market="za")
    c._per_market_budget = 100  # equal to the budget
    c._units_spent = 80
    assert c._budget_reached(budget=100) is False  # 80 + 1 <= 100
    c._units_spent = 100
    assert c._budget_reached(budget=100) is True  # 100 + 1 > 100


def test_market_units_spent_tracks_per_instance_not_global():
    """The per-market counter advances with this instance's spend only, while the
    global ledger stays shared across instances."""
    EnsembleConnector.reset_global_budget()
    za = EnsembleConnector(market="za")
    za._units_spent += 30
    assert za._market_units_spent == 30
    assert za._units_spent == 30  # global

    ke = EnsembleConnector(market="ke")
    assert ke._units_spent == 30  # shares the global ledger
    assert ke._market_units_spent == 0  # own tally fresh
    ke._units_spent += 20
    assert ke._market_units_spent == 20
    assert ke._units_spent == 50  # global advanced
    assert za._market_units_spent == 30  # za's tally untouched


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_per_market_units_zero_is_honoured_not_coalesced(mock_load_sources, _mock_secret):
    """per_market_units=0 means this market spends nothing; it must NOT be
    falsy-coalesced to the full budget, so no call fires."""
    EnsembleConnector.reset_global_budget()
    mock_load_sources.return_value = {
        "ensemble": {
            "budget_units_per_run": 100,
            "per_market_units": 0,
            "markets": {"za": {"tiktok_hashtags": ["amapiano"]}},
        }
    }
    responses_lib.add(
        responses_lib.GET,
        TT_HASHTAG_URL,
        json=_tiktok_response(units_charged=5, count=1),
        status=200,
    )

    connector = _mk_connector()
    df = connector.fetch()

    assert len(responses_lib.calls) == 0  # per-market cap of 0 stops before any call
    assert len(df) == 0


def test_tiktok_keyword_endpoint_passes_period_query_param():
    """EnsembleData /tt/keyword/search errors 422 without the `period` query
    param. The endpoint default must carry it so the connector stops tripping
    on every run."""
    tk = next((ep for ep in DEFAULT_ENDPOINTS if ep["name"] == "tiktok_keyword"), None)
    assert tk is not None, "tiktok_keyword endpoint missing from DEFAULT_ENDPOINTS"
    extra = tk.get("extra_params") or {}
    assert extra.get("period"), "tiktok_keyword endpoint must declare extra_params.period"


def test_fetch_endpoint_merges_extra_params_into_request():
    """extra_params on an endpoint dict should land in the outgoing request
    params so API-required fields like `period` are always sent."""
    captured: dict = {}

    class _Capturer(EnsembleConnector):
        def _request(self, method, url, **kwargs):  # type: ignore[override]
            captured["params"] = kwargs.get("params") or {}

            class _Resp:
                status_code = 200

                def json(self_inner):
                    return {"data": {"aweme_list": []}}

            return _Resp()

    conn = _Capturer(market="za")
    endpoint = {
        "name": "tiktok_keyword",
        "path": "/tt/keyword/search",
        "platform": "tiktok",
        "query_param": "name",
        "terms_key": "tiktok_keywords",
        "extra_params": {"period": "7", "country": "ZA"},
    }
    conn._fetch_endpoint(endpoint, "loadshedding", token="tok")
    assert captured["params"].get("name") == "loadshedding"
    assert captured["params"].get("token") == "tok"
    assert captured["params"].get("period") == "7"
    assert captured["params"].get("country") == "ZA"


def test_is_mostly_non_latin_detects_dominant_scripts():
    from src.ingestion.connectors.ensemble import _is_mostly_non_latin

    # Dominantly Arabic -> True
    assert _is_mostly_non_latin("هذا منشور عربي كامل للاختبار") is True
    # Dominantly Chinese -> True
    assert _is_mostly_non_latin("这是一个完整的中文帖子用于测试过滤器") is True
    # Dominantly Cyrillic -> True
    assert _is_mostly_non_latin("Это полностью русский пост для проверки") is True
    # Dominantly Japanese -> True
    assert _is_mostly_non_latin("これは日本語で書かれたテストポストです") is True
    # Dominantly Thai -> True
    assert _is_mostly_non_latin("นี่คือโพสต์ภาษาไทยสำหรับการทดสอบ") is True


def test_is_mostly_non_latin_keeps_latin_and_mixed_content():
    from src.ingestion.connectors.ensemble import _is_mostly_non_latin

    # Pure English -> False (keep)
    assert _is_mostly_non_latin("amapiano challenge was fire today #mzansi") is False
    # Swahili (Latin script) -> False (keep)
    assert _is_mostly_non_latin("Nimefurahi sana leo Nairobi iko moto") is False
    # Afrikaans -> False (keep)
    assert _is_mostly_non_latin("Hierdie is 'n lekker braai dag in Pretoria") is False
    # Mixed English with one Arabic emoji-adjacent phrase below 30% threshold
    assert _is_mostly_non_latin("great amapiano track today from جميل mzansi") is False
    # Empty / very short -> False (not enough signal to judge)
    assert _is_mostly_non_latin("") is False
    assert _is_mostly_non_latin("hi") is False
    assert _is_mostly_non_latin("123") is False


def test_finalise_drops_non_latin_posts(monkeypatch):
    """Integration: _finalise strips dominantly-non-Latin posts before returning."""
    from src.ingestion.connectors.ensemble import EnsembleConnector

    conn = EnsembleConnector(market="za")
    rows = [
        {
            "source": "tiktok",
            "platform": "tiktok",
            "market": "za",
            "content_type": "post",
            "query_group": "tiktok_hashtag",
            "query_term": "southafrica",
            "text": "Great new amapiano track from Mzansi #southafrica",
            "url": "https://tiktok.com/@a/video/1",
            "views": 1000,
        },
        {
            "source": "tiktok",
            "platform": "tiktok",
            "market": "za",
            "content_type": "post",
            "query_group": "tiktok_hashtag",
            "query_term": "southafrica",
            "text": "这是一个完整的中文帖子与视频内容无关 South Africa",
            "url": "https://tiktok.com/@b/video/2",
            "views": 500,
        },
        {
            "source": "tiktok",
            "platform": "tiktok",
            "market": "za",
            "content_type": "post",
            "query_group": "tiktok_hashtag",
            "query_term": "southafrica",
            "text": "هذا منشور كامل بالعربية يحتوي على جنوب أفريقيا ",
            "url": "https://tiktok.com/@c/video/3",
            "views": 300,
        },
    ]
    df = conn._finalise(rows)
    # Only the English post survives.
    assert len(df) == 1
    assert df.iloc[0]["url"] == "https://tiktok.com/@a/video/1"


# ---------------------------------------------------------------------------
# Threads (/threads/keyword/search) response unwrap
# ---------------------------------------------------------------------------


def test_unwrap_threads_post_extracts_inner_post():
    """Live Threads response wraps the post under
    node.thread.thread_items[0].post; unwrap returns the inner dict."""
    item = {
        "node": {
            "thread": {"thread_items": [{"post": {"pk": "abc", "caption": {"text": "hi"}}}]},
            "__typename": "XDTSearchThread",
        },
        "cursor": "x",
    }
    post = EnsembleConnector._unwrap_threads_post(item)
    assert post == {"pk": "abc", "caption": {"text": "hi"}}


def test_normalise_tiktok_keyword_unwraps_aweme_info():
    """/tt/keyword/search wraps each post under aweme_info; the normaliser must
    unwrap it or every keyword row is hollow (null published_at, empty text/url,
    zero metrics). Regression for the ~11.9k hollow keyword rows."""
    c = EnsembleConnector(market="za")
    item = {
        "aweme_info": {
            "aweme_id": "7650014163602771208",
            "desc": "amapiano set tonight",
            "create_time": 1_781_157_729,
            "statistics": {
                "digg_count": 50768,
                "play_count": 364442,
                "comment_count": 12,
                "share_count": 3,
            },
            "author": {"nickname": "Bubbles", "unique_id": "bubbles.flake"},
        },
        "doc_type": "aweme",
        "type": 1,
        "doc_id": "x",
    }
    rows = c._normalise_posts(
        [item], platform="tiktok", query_group="tiktok_keyword", query_term="amapiano"
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["published_at"] is not None
    assert r["text"] == "amapiano set tonight"
    assert r["author_handle"] == "bubbles.flake"
    assert r["likes"] == 50768
    assert r["views"] == 364442
    assert "7650014163602771208" in r["url"]


def test_normalise_tiktok_flat_post_unchanged():
    """A flat TikTok post (hashtag shape, no aweme_info wrapper) still maps,
    guarding the else branch so the unwrap is keyword-only."""
    c = EnsembleConnector(market="za")
    item = {
        "aweme_id": "7200000000000000001",
        "desc": "flat amapiano",
        "create_time": 1_713_600_000,
        "play_count": 100,
        "digg_count": 10,
        "comment_count": 1,
        "share_count": 0,
        "author": {"nickname": "DJ", "unique_id": "djflat"},
    }
    rows = c._normalise_posts(
        [item], platform="tiktok", query_group="tiktok_hashtag", query_term="amapiano"
    )
    assert len(rows) == 1
    assert rows[0]["author_handle"] == "djflat"
    assert rows[0]["text"] == "flat amapiano"
    assert rows[0]["published_at"] is not None


def _age_row(text, url, published_at):
    return {
        "source": "EnsembleData",
        "platform": "threads",
        "market": "za",
        "content_type": "post",
        "query_group": "threads_keyword",
        "query_term": "x",
        "author_name": "",
        "author_handle": text,
        "title": text,
        "text": text,
        "url": url,
        "published_at": published_at,
        "views": 0,
        "likes": 1,
        "comments": 0,
        "shares": 0,
    }


def test_finalise_drops_old_posts_when_age_filter_set(monkeypatch):
    """ENSEMBLE_MAX_POST_AGE_DAYS set: posts older than the window drop, fresh and
    null-timestamp rows stay. Fixes the Threads multi-year-archive staleness."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("ENSEMBLE_MAX_POST_AGE_DAYS", "30")
    c = EnsembleConnector(market="za")
    now = datetime.now(UTC)
    rows = [
        _age_row("fresh post", "https://t/1", now - timedelta(days=2)),
        _age_row("stale post", "https://t/2", now - timedelta(days=400)),
        _age_row("no ts", "https://t/3", None),
    ]
    df = c._finalise(rows)
    texts = set(df["text"])
    assert "fresh post" in texts
    assert "no ts" in texts
    assert "stale post" not in texts


def test_finalise_keeps_old_posts_when_age_filter_off(monkeypatch):
    """Default (unset): no age filtering, so _finalise is byte-identical to the
    live cron and the stale post is retained."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.delenv("ENSEMBLE_MAX_POST_AGE_DAYS", raising=False)
    c = EnsembleConnector(market="za")
    now = datetime.now(UTC)
    rows = [
        _age_row("fresh post", "https://t/1", now - timedelta(days=2)),
        _age_row("stale post", "https://t/2", now - timedelta(days=400)),
    ]
    df = c._finalise(rows)
    assert "stale post" in set(df["text"])


def test_extract_author_rejects_numeric_ig_id_when_flag_on(monkeypatch):
    """WATCHLIST_HANDLE_FIX on: a bare numeric user id is an ID, not a watchlist
    handle, so it is dropped rather than emitted as author_handle. IG ships
    numeric ids where the config carries usernames, so numeric handles can never
    match the watchlist and only pollute dedup + creator_spread."""
    monkeypatch.setenv("WATCHLIST_HANDLE_FIX", "true")
    item = {"pk": 123, "user": {"id": "28762313085"}}
    _name, handle = EnsembleConnector._extract_author(item)
    assert handle == ""


def test_extract_author_prefers_username_over_numeric_when_flag_on(monkeypatch):
    """WATCHLIST_HANDLE_FIX on: the real username is emitted even when a numeric
    id sits alongside it (the IG user/posts user object shape)."""
    monkeypatch.setenv("WATCHLIST_HANDLE_FIX", "true")
    item = {"pk": 123, "user": {"id": "62863166968", "username": "princejaysoo"}}
    _name, handle = EnsembleConnector._extract_author(item)
    assert handle == "princejaysoo"


def test_extract_author_numeric_id_preserved_when_flag_off(monkeypatch):
    """Default (flag off): the numeric id is still emitted, so the OFF path is
    byte-identical to the live cron."""
    monkeypatch.delenv("WATCHLIST_HANDLE_FIX", raising=False)
    item = {"pk": 123, "user": {"id": "28762313085"}}
    _name, handle = EnsembleConnector._extract_author(item)
    assert handle == "28762313085"


def test_unwrap_threads_post_returns_input_when_shape_unrecognised():
    """Connector should not crash when Ensemble changes the response shape."""
    flat = {"text": "already flat", "user": {"username": "x"}}
    assert EnsembleConnector._unwrap_threads_post(flat) is flat
    empty_thread = {"node": {"thread": {"thread_items": []}}}
    out = EnsembleConnector._unwrap_threads_post(empty_thread)
    assert out is empty_thread


def test_normalise_posts_threads_extracts_text_author_url():
    """End-to-end: a wrapped Threads item produces a fully populated row."""
    conn = _mk_connector(market="za")
    items = [
        {
            "node": {
                "thread": {
                    "thread_items": [
                        {
                            "post": {
                                "pk": 3489563294689421143,
                                "code": "DBta1P5NGdX",
                                "user": {"username": "princejaysoo", "id": "62863166968"},
                                "caption": {"text": "Amapiano vibes today", "pk": "180660"},
                                "like_count": 1128,
                                "taken_at": 1730208398,
                            }
                        }
                    ]
                },
                "__typename": "XDTSearchThread",
            },
            "cursor": "abc",
        }
    ]
    rows = conn._normalise_posts(
        items, platform="threads", query_group="threads_keyword", query_term="amapiano"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["platform"] == "threads"
    assert row["text"] == "Amapiano vibes today"
    # Title falls back to first 100 chars of text when no explicit title field.
    assert row["title"].startswith("Amapiano vibes today")
    assert row["author_handle"] == "princejaysoo"
    assert row["url"] == "https://www.threads.net/@princejaysoo/post/DBta1P5NGdX"
    assert row["likes"] == 1128
    assert row["published_at"] is not None


def test_normalise_posts_other_platforms_unchanged_by_threads_unwrap():
    """Non-threads platforms still receive flat items; unwrap is a no-op
    because the platform argument gates it."""
    conn = _mk_connector(market="za")
    items = [
        {
            "id": "vid1",
            "aweme_id": "vid1",
            "desc": "TikTok caption",
            "author": {"unique_id": "djtest", "nickname": "DJ Test"},
            "create_time": 1_713_600_000,
        }
    ]
    rows = conn._normalise_posts(
        items, platform="tiktok", query_group="tiktok_hashtag", query_term="amapiano"
    )
    assert len(rows) == 1
    assert rows[0]["text"] == "TikTok caption"
    assert rows[0]["author_handle"] == "djtest"


# ---------------------------------------------------------------------------
# Creator (per-user) endpoints (Phase 2, 27 May 2026)
# ---------------------------------------------------------------------------


TT_USER_URL = f"{BASE}/tt/user/posts"
IG_USER_URL = f"{BASE}/instagram/user/posts"
THREADS_USER_URL = f"{BASE}/threads/user/posts"
THREADS_USER_SEARCH_URL = f"{BASE}/threads/user/search"


def test_load_creator_handles_tier_1_only_by_default():
    """tier_cap=tier_1 returns only peak Gen Z anchors from the za watchlist."""
    handles = EnsembleConnector._load_creator_handles("za", "tier_1")
    # All three platforms must be present (even if a tier is empty)
    assert set(handles.keys()) == {"tiktok", "instagram", "threads"}
    # tier_1 cap: should NOT include any tier_2 / tier_3 names (kabzadesmall is t2)
    assert "kabzadesmall" not in handles["tiktok"]
    # Sanity: tier_1 tiktok contains some known anchors
    assert "tyla" in handles["tiktok"]


def test_load_creator_handles_tier_2_widens():
    """tier_cap=tier_2 includes both tier_1 and tier_2 handles."""
    handles = EnsembleConnector._load_creator_handles("za", "tier_2")
    assert "tyla" in handles["tiktok"]
    assert "kabzadesmall" in handles["tiktok"]


def test_load_creator_handles_missing_market_raises():
    """Unknown market -> FileNotFoundError so the connector logs and skips."""
    with pytest.raises(FileNotFoundError):
        EnsembleConnector._load_creator_handles("xx", "tier_1")


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_creator_endpoints_enabled_calls_user_paths(mock_load_sources, _mock_secret):
    """creator_endpoints_enabled=true fires per-handle calls on each platform."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "creator_endpoints_enabled": True,
        },
        budget=10_000,  # plenty so the tier_1 sweep doesn't bail early
    )

    # Match any handle to keep the test endpoint-agnostic. Each platform resolves
    # the handle differently: tiktok takes the username directly; instagram via
    # /instagram/user/info -> user_id; threads via /threads/user/search -> id.
    EnsembleConnector.reset_instagram_handle_cache()
    EnsembleConnector.reset_threads_handle_cache()
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    for _ in range(60):  # enough for any tier_1 sweep across platforms
        responses_lib.add(
            responses_lib.GET, IG_USER_INFO_URL, json={"data": {"pk": "1131049833"}}, status=200
        )
        responses_lib.add(
            responses_lib.GET,
            THREADS_USER_SEARCH_URL,
            json={"data": [{"node": {"username": "x", "pk": "63055343223"}}]},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET, TT_USER_URL, json=_tiktok_response(count=1), status=200
        )
        responses_lib.add(
            responses_lib.GET, IG_USER_URL, json=_tiktok_response(count=1), status=200
        )
        responses_lib.add(
            responses_lib.GET, THREADS_USER_URL, json=_tiktok_response(count=1), status=200
        )

    _mk_connector("za").fetch()

    tt_user_calls = [c for c in responses_lib.calls if TT_USER_URL in c.request.url]
    ig_user_calls = [c for c in responses_lib.calls if IG_USER_URL in c.request.url]
    threads_user_calls = [c for c in responses_lib.calls if THREADS_USER_URL in c.request.url]
    # At least one call per platform must have happened
    assert len(tt_user_calls) >= 1
    assert len(ig_user_calls) >= 1
    assert len(threads_user_calls) >= 1
    # tiktok takes username directly; instagram + threads take the resolved id
    assert "username=" in tt_user_calls[0].request.url
    assert "user_id=" in ig_user_calls[0].request.url
    assert "id=" in threads_user_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_creator_endpoints_disabled_does_not_call(mock_load_sources, _mock_secret):
    """Without creator_endpoints_enabled the connector never hits /<plat>/user/posts."""
    mock_load_sources.return_value = _sources({"tiktok_hashtags": ["amapiano"]})

    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)

    _mk_connector("za").fetch()

    user_calls = [
        c
        for c in responses_lib.calls
        if TT_USER_URL in c.request.url
        or IG_USER_URL in c.request.url
        or THREADS_USER_URL in c.request.url
    ]
    assert user_calls == []


# ---------------------------------------------------------------------------
# X / Twitter endpoints (28 May 2026)
# ---------------------------------------------------------------------------


TWITTER_USER_TWEETS_URL = f"{BASE}/twitter/user/tweets"
TWITTER_USER_INFO_URL = f"{BASE}/twitter/user/info"


def _twitter_response(units_charged: int = 1, count: int = 1) -> dict:
    """EnsembleData /twitter/* fake payload using `data.tweets` envelope."""
    tweets = [
        {
            "id": f"180{i:016d}",
            "id_str": f"180{i:016d}",
            "full_text": f"Amapiano vibes {i} tonight in Soweto",
            "created_at": 1_713_600_000 + i,
            "favorite_count": 100 + i,
            "retweet_count": 10 + i,
            "reply_count": 5 + i,
            "view_count": 50_000 + i,
            "user": {
                "screen_name": f"creator_{i}",
                "name": f"Creator {i}",
            },
        }
        for i in range(count)
    ]
    return {"units_charged": units_charged, "data": {"tweets": tweets}}


def _twitter_response_ctime(units_charged: int = 1, count: int = 1) -> dict:
    """Same shape as _twitter_response but created_at carries the real Twitter
    ctime string ('Wed Apr 20 08:00:00 +0000 2024') the live API returns,
    rather than an integer epoch. Exercises the _extract_timestamp ctime
    fallback through the full connector path."""
    tweets = [
        {
            "id": f"180{i:016d}",
            "id_str": f"180{i:016d}",
            "full_text": f"Amapiano vibes {i} tonight in Soweto",
            "created_at": "Wed Apr 20 08:00:00 +0000 2024",
            "favorite_count": 100 + i,
            "retweet_count": 10 + i,
            "reply_count": 5 + i,
            "view_count": 50_000 + i,
            "user": {
                "screen_name": f"creator_{i}",
                "name": f"Creator {i}",
            },
        }
        for i in range(count)
    ]
    return {"units_charged": units_charged, "data": {"tweets": tweets}}


@pytest.fixture(autouse=True)
def _reset_twitter_cache():
    """Reset the process-level Twitter handle resolution cache per test."""
    EnsembleConnector.reset_twitter_handle_cache()
    yield
    EnsembleConnector.reset_twitter_handle_cache()


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_twitter_handles_short_circuit_when_flag_disabled(mock_load_sources, _mock_secret):
    """Without twitter_handles_enabled the connector never hits /twitter/user/tweets."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "twitter_handles": ["tyla"],
            # No twitter_handles_enabled flag.
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    _mk_connector("za").fetch()
    twitter_calls = [
        c
        for c in responses_lib.calls
        if TWITTER_USER_TWEETS_URL in c.request.url or TWITTER_USER_INFO_URL in c.request.url
    ]
    assert twitter_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_twitter_endpoints_prepended_before_hashtags(mock_load_sources, _mock_secret):
    """Twitter must run before hashtag/creator spend so the boost ledger cannot skip it."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "twitter_handles": ["tyla"],
            "twitter_handles_enabled": True,
            "creator_endpoints_enabled": True,
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    responses_lib.add(
        responses_lib.GET,
        TWITTER_USER_INFO_URL,
        json={"units_charged": 2, "data": {"rest_id": "1234567890"}},
        status=200,
    )
    responses_lib.add(
        responses_lib.GET, TWITTER_USER_TWEETS_URL, json=_twitter_response(count=1), status=200
    )
    _mk_connector("za").fetch()
    first_twitter = next(
        i
        for i, c in enumerate(responses_lib.calls)
        if TWITTER_USER_TWEETS_URL in c.request.url or TWITTER_USER_INFO_URL in c.request.url
    )
    first_tiktok = next(
        i for i, c in enumerate(responses_lib.calls) if TT_HASHTAG_URL in c.request.url
    )
    assert first_twitter < first_tiktok


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_twitter_user_tweets_resolves_handle_via_user_info(mock_load_sources, _mock_secret):
    """/twitter/user/tweets requires numeric rest_id; resolver hits /user/info first."""
    mock_load_sources.return_value = _sources(
        {
            "twitter_handles": ["tyla"],
            "twitter_handles_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        TWITTER_USER_INFO_URL,
        json={"units_charged": 2, "data": {"rest_id": "1234567890"}},
        status=200,
    )
    responses_lib.add(
        responses_lib.GET, TWITTER_USER_TWEETS_URL, json=_twitter_response(count=1), status=200
    )
    _mk_connector("za").fetch()

    info_calls = [c for c in responses_lib.calls if TWITTER_USER_INFO_URL in c.request.url]
    tweets_calls = [c for c in responses_lib.calls if TWITTER_USER_TWEETS_URL in c.request.url]
    assert len(info_calls) == 1
    assert "name=tyla" in info_calls[0].request.url
    assert len(tweets_calls) == 1
    # rest_id from the resolver is wired through as the `id` param.
    assert "id=1234567890" in tweets_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_twitter_ctime_created_at_parses_through_connector_path(mock_load_sources, _mock_secret):
    """The real Twitter ctime created_at string parses to a tz-aware UTC
    published_at when it travels the full fetch -> normalise path, not just
    the _extract_timestamp unit."""
    from datetime import datetime

    mock_load_sources.return_value = _sources(
        {
            "twitter_handles": ["tyla"],
            "twitter_handles_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        TWITTER_USER_INFO_URL,
        json={"units_charged": 2, "data": {"rest_id": "1234567890"}},
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        TWITTER_USER_TWEETS_URL,
        json=_twitter_response_ctime(count=1),
        status=200,
    )
    df = _mk_connector("za").fetch()

    tweets = df[df["platform"] == "twitter"]
    assert len(tweets) == 1
    published = tweets.iloc[0]["published_at"]
    assert isinstance(published, datetime)
    assert published.tzinfo is not None
    assert published.utcoffset().total_seconds() == 0
    assert published.year == 2024
    assert published.hour == 8


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_twitter_user_info_cache_persists_across_terms(mock_load_sources, _mock_secret):
    """The same handle resolved once should not trigger a second /user/info call."""
    mock_load_sources.return_value = _sources(
        {
            # tyla appears twice in the handle pool (deliberately duplicated to
            # simulate a pan-African creator landing in two market lists).
            "twitter_handles": ["tyla", "tyla"],
            "twitter_handles_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        TWITTER_USER_INFO_URL,
        json={"units_charged": 2, "data": {"rest_id": "1234567890"}},
        status=200,
    )
    for _ in range(4):
        responses_lib.add(
            responses_lib.GET,
            TWITTER_USER_TWEETS_URL,
            json=_twitter_response(count=1),
            status=200,
        )
    _mk_connector("za").fetch()

    info_calls = [c for c in responses_lib.calls if TWITTER_USER_INFO_URL in c.request.url]
    tweets_calls = [c for c in responses_lib.calls if TWITTER_USER_TWEETS_URL in c.request.url]
    # Resolver only fires once even though tyla appears twice in the pool.
    assert len(info_calls) == 1
    assert len(tweets_calls) == 2


@responses_lib.activate
def test_instagram_handle_resolves_to_pk_and_caches():
    """/instagram/user/posts needs a numeric user_id, so the handle resolves to
    its pk via /instagram/user/info once, then serves from cache."""
    EnsembleConnector.reset_instagram_handle_cache()
    responses_lib.add(
        responses_lib.GET,
        IG_USER_INFO_URL,
        json={"units_charged": 2, "data": {"pk": "1131049833", "username": "tyla"}},
        status=200,
    )
    c = _mk_connector("za")
    assert c._resolve_instagram_handle_to_id("tyla", FAKE_TOKEN) == "1131049833"
    # Cache hit: a second lookup (case-insensitive, leading @ stripped) is free.
    assert c._resolve_instagram_handle_to_id("@Tyla", FAKE_TOKEN) == "1131049833"
    info_calls = [call for call in responses_lib.calls if IG_USER_INFO_URL in call.request.url]
    assert len(info_calls) == 1
    assert "username=tyla" in info_calls[0].request.url


@responses_lib.activate
def test_threads_handle_resolves_to_threads_pk_and_caches():
    """/threads/user/posts needs the threads-native pk (distinct from the IG pk),
    resolved via /threads/user/search by exact-username match, then cached."""
    EnsembleConnector.reset_threads_handle_cache()
    responses_lib.add(
        responses_lib.GET,
        THREADS_USER_SEARCH_URL,
        json={
            "units_charged": 2,
            "data": [
                {"node": {"username": "notzuck", "pk": "111"}},
                {"node": {"username": "zuck", "pk": "63055343223"}},
            ],
        },
        status=200,
    )
    c = _mk_connector("za")
    # Picks the exact username match, not the first result.
    assert c._resolve_threads_handle_to_id("zuck", FAKE_TOKEN) == "63055343223"
    # Cache hit: second lookup is free, no new search call.
    assert c._resolve_threads_handle_to_id("@Zuck", FAKE_TOKEN) == "63055343223"
    search_calls = [c2 for c2 in responses_lib.calls if THREADS_USER_SEARCH_URL in c2.request.url]
    assert len(search_calls) == 1
    assert "name=zuck" in search_calls[0].request.url


def test_twitter_post_unwrap_via_data_tweets():
    """`_extract_list_from_payload` finds tweets nested under data.tweets."""
    payload = {
        "units_charged": 1,
        "data": {
            "tweets": [
                {"id_str": "1", "full_text": "first"},
                {"id_str": "2", "full_text": "second"},
            ]
        },
    }
    items = EnsembleConnector._extract_list_from_payload(payload)
    assert len(items) == 2
    assert items[0]["id_str"] == "1"


def _twitter_graphql_entry(i: int = 0, created_at: str = "Wed Oct 30 05:45:03 +0000 2019") -> dict:
    """One timeline entry in the REAL /twitter/user/tweets GraphQL envelope
    captured by the 2026-07-02 live probe (docs/twitter-probe-2026-07-02.md)."""
    return {
        "entryId": f"tweet-118973536402022{i}",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {
                "itemType": "TimelineTweet",
                "tweet_results": {
                    "result": {
                        "rest_id": f"118973536402022{i}",
                        "core": {
                            "user_results": {
                                "result": {
                                    "rest_id": "14697575",
                                    "legacy": {
                                        "screen_name": "News24",
                                        "name": "News24",
                                    },
                                }
                            }
                        },
                        "legacy": {
                            "created_at": created_at,
                            "full_text": f"Breaking: load shedding update {i}",
                            "favorite_count": 14339 + i,
                            "retweet_count": 1991 + i,
                            "reply_count": 224 + i,
                            "quote_count": 12,
                            "bookmark_count": 7,
                        },
                        "views": {"count": "51234"},
                    }
                },
            },
        },
    }


def test_twitter_graphql_timeline_envelope_normalises_to_full_rows():
    """The live GraphQL envelope (content.itemContent.tweet_results.result.legacy)
    must unwrap to rows with text, metrics, author and a parsed published_at.
    Cursor entries are skipped, not emitted as hollow rows."""
    from datetime import datetime

    conn = _mk_connector(market="za")
    items = [
        _twitter_graphql_entry(0),
        _twitter_graphql_entry(1, created_at="Wed Jul 01 18:30:00 +0000 2026"),
        # Cursor entry, as the live timeline appends: no itemContent tweet.
        {
            "entryId": "cursor-bottom-99",
            "content": {"entryType": "TimelineTimelineCursor", "value": "DAABCg"},
        },
    ]
    rows = conn._normalise_posts(
        items, platform="twitter", query_group="twitter_user", query_term="14697575"
    )
    assert len(rows) == 2
    row = rows[0]
    assert row["text"] == "Breaking: load shedding update 0"
    assert row["author_handle"] == "News24"
    assert row["likes"] == 14339.0
    assert row["shares"] == 1991.0
    assert row["comments"] == 224.0
    assert row["views"] == 51234.0
    assert row["url"] == "https://twitter.com/News24/status/1189735364020220"
    assert isinstance(row["published_at"], datetime)
    assert row["published_at"].tzinfo is not None
    assert row["published_at"].year == 2019
    # Second entry proves recency keys off legacy.created_at, not list order.
    assert rows[1]["published_at"].year == 2026


def test_twitter_post_normalise_handles_missing_fields():
    """Defensive: missing user / metrics / timestamp produces an empty-default row,
    not a crash."""
    conn = _mk_connector(market="za")
    items = [
        {
            "id_str": "1800000000000000099",
            # No full_text, no user, no metrics, no created_at.
        }
    ]
    rows = conn._normalise_posts(
        items,
        platform="twitter",
        query_group="twitter_user",
        query_term="1234567890",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["platform"] == "twitter"
    assert row["text"] == ""
    assert row["author_handle"] == ""
    assert row["views"] == 0.0
    assert row["likes"] == 0.0
    assert row["published_at"] is None


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_twitter_post_token_redacted_from_logged_url(mock_load_sources, _mock_secret, caplog):
    """Token must not appear in any log record when hitting /twitter/* endpoints."""
    mock_load_sources.return_value = _sources(
        {
            "twitter_handles": ["tyla"],
            "twitter_handles_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        TWITTER_USER_INFO_URL,
        json={"units_charged": 2, "data": {"rest_id": "1234567890"}},
        status=200,
    )
    responses_lib.add(
        responses_lib.GET, TWITTER_USER_TWEETS_URL, json=_twitter_response(count=1), status=200
    )
    with caplog.at_level(logging.DEBUG, logger="connector.ensemble"):
        _mk_connector("za").fetch()
    for record in caplog.records:
        rendered = record.getMessage()
        assert FAKE_TOKEN not in rendered, f"Token leaked in log record: {rendered!r}"
        assert FAKE_TOKEN not in str(record.msg)
        for arg in record.args or ():
            assert FAKE_TOKEN not in str(arg)


# ---------------------------------------------------------------------------
# TikTok comments (/tt/post/comments) — Phase 2 enrichment (28 May 2026)
# ---------------------------------------------------------------------------


TT_COMMENTS_URL = f"{BASE}/tt/post/comments"


def _tiktok_comments_response(units_charged: int = 1, count: int = 1) -> dict:
    """EnsembleData /tt/post/comments fake payload using `data.comments`."""
    comments = [
        {
            "cid": f"cmt{i}",
            "text": f"slang comment {i} fire fire",
            "create_time": 1_713_600_000 + i,
            "digg_count": 20 + i,
            "user": {
                "unique_id": f"commenter_{i}",
                "nickname": f"Commenter {i}",
            },
        }
        for i in range(count)
    ]
    return {"units_charged": units_charged, "data": {"comments": comments}}


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_comments_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """Without tt_comments_enabled the connector never hits /tt/post/comments."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            # No tt_comments_enabled flag.
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=3), status=200)
    _mk_connector("za").fetch()
    comment_calls = [c for c in responses_lib.calls if TT_COMMENTS_URL in c.request.url]
    assert comment_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_comments_fetches_top_n_videos_by_likes(mock_load_sources, _mock_secret):
    """Ranks collected TikTok posts by likes DESC and pulls top N comments."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "tt_comments_enabled": True,
            "tt_comment_videos_per_run": 2,
        },
        budget=10_000,
    )
    # Three posts with distinct like counts.
    payload = {
        "units_charged": 1,
        "data": [
            {
                "id": "1000000000000000001",
                "aweme_id": "1000000000000000001",
                "desc": "low likes",
                "create_time": 1_713_600_000,
                "digg_count": 10,
                "author": {"unique_id": "a", "nickname": "A"},
            },
            {
                "id": "1000000000000000002",
                "aweme_id": "1000000000000000002",
                "desc": "high likes",
                "create_time": 1_713_600_000,
                "digg_count": 9999,
                "author": {"unique_id": "b", "nickname": "B"},
            },
            {
                "id": "1000000000000000003",
                "aweme_id": "1000000000000000003",
                "desc": "mid likes",
                "create_time": 1_713_600_000,
                "digg_count": 500,
                "author": {"unique_id": "c", "nickname": "C"},
            },
        ],
    }
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=payload, status=200)
    for _ in range(5):
        responses_lib.add(
            responses_lib.GET,
            TT_COMMENTS_URL,
            json=_tiktok_comments_response(count=1),
            status=200,
        )
    _mk_connector("za").fetch()
    comment_calls = [c for c in responses_lib.calls if TT_COMMENTS_URL in c.request.url]
    # Capped at tt_comment_videos_per_run=2.
    assert len(comment_calls) == 2
    # Highest-likes video (id ending 0002) fetched first.
    assert "aweme_id=1000000000000000002" in comment_calls[0].request.url
    # Second-highest (id ending 0003) next.
    assert "aweme_id=1000000000000000003" in comment_calls[1].request.url
    # Lowest-likes video (id ending 0001) NOT fetched.
    assert all("1000000000000000001" not in c.request.url for c in comment_calls)


def test_tt_comments_extract_via_data_comments():
    """`_extract_list_from_payload` finds comments nested under data.comments."""
    payload = {
        "units_charged": 1,
        "data": {
            "comments": [
                {"cid": "1", "text": "first"},
                {"cid": "2", "text": "second"},
            ]
        },
    }
    items = EnsembleConnector._extract_list_from_payload(payload)
    assert len(items) == 2
    assert items[0]["cid"] == "1"


def test_tt_comments_normalise_handles_missing_text():
    """Comments without body text (vendor returns deleted/hidden) are dropped."""
    conn = _mk_connector(market="za")
    items = [
        {"cid": "c1", "text": "real comment", "user": {"unique_id": "x"}},
        {"cid": "c2", "text": "", "user": {"unique_id": "y"}},
        {"cid": "c3", "text": "   ", "user": {"unique_id": "z"}},  # whitespace-only
        {"cid": "c4", "user": {"unique_id": "w"}},  # missing text entirely
    ]
    rows = conn._normalise_tiktok_comments(
        items,
        query_group="tiktok_comments",
        parent_video_id="1234",
    )
    assert len(rows) == 1
    assert rows[0]["text"] == "real comment"
    assert rows[0]["content_type"] == "tiktok_comment"
    assert rows[0]["query_term"] == "1234"


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_comments_respects_global_quota_exhausted(mock_load_sources, _mock_secret):
    """If quota is exhausted during post fetch, comments phase is skipped."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano", "mzansi"],
            "tt_comments_enabled": True,
            "tt_comment_videos_per_run": 5,
        }
    )
    # First hashtag call returns 495 -> trips the global circuit breaker.
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    comment_calls = [c for c in responses_lib.calls if TT_COMMENTS_URL in c.request.url]
    assert comment_calls == []


def test_extract_tiktok_video_id_from_canonical_url():
    """Helper extracts numeric video id from canonical TikTok URL."""
    fn = EnsembleConnector._extract_tiktok_video_id
    assert fn("https://www.tiktok.com/@djtest/video/7200000000000000001") == ("7200000000000000001")
    # Trailing path / querystring trimmed at first non-digit.
    assert fn("https://www.tiktok.com/@djtest/video/7200000000000000001?lang=en") == (
        "7200000000000000001"
    )
    # Non-TikTok URL returns empty.
    assert fn("https://twitter.com/x/status/1") == ""
    assert fn("") == ""


# ---------------------------------------------------------------------------
# Wave 3 dark expansion (28 May 2026): YouTube, Instagram, Threads, TikTok
# ---------------------------------------------------------------------------


YT_CHANNEL_SHORTS_URL = f"{BASE}/youtube/channel/shorts"
YT_CHANNEL_VIDEOS_URL = f"{BASE}/youtube/channel/videos"
YT_SEARCH_URL = f"{BASE}/youtube/search"
YT_VIDEO_COMMENTS_URL = f"{BASE}/youtube/video/comments"

IG_USER_REELS_URL = f"{BASE}/instagram/user/reels"
IG_USER_TAGGED_POSTS_URL = f"{BASE}/instagram/user/tagged-posts"
IG_USER_INFO_URL = f"{BASE}/instagram/user/info"
IG_POST_COMMENTS_URL = f"{BASE}/instagram/post/comments"

THREADS_POST_REPLIES_URL = f"{BASE}/threads/post/replies"
THREADS_USER_INFO_URL = f"{BASE}/threads/user/info"

TT_MUSIC_POSTS_URL = f"{BASE}/tt/music/posts"
TT_POST_INFO_URL = f"{BASE}/tt/post/info"
TT_COMMENT_REPLIES_URL = f"{BASE}/tt/post/comments-replies"


# YouTube Wave 3 -----------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_yt_shorts_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """yt_shorts_enabled off -> /youtube/channel/shorts never hit."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "yt_channel_browse_ids": ["UCabc"],
            # No yt_shorts_enabled flag.
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    _mk_connector("za").fetch()
    yt_calls = [c for c in responses_lib.calls if YT_CHANNEL_SHORTS_URL in c.request.url]
    assert yt_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_yt_shorts_fires_when_flag_true(mock_load_sources, _mock_secret):
    """yt_shorts_enabled=true wires /youtube/channel/shorts per browseId."""
    mock_load_sources.return_value = _sources(
        {
            "yt_channel_browse_ids": ["UCabc"],
            "yt_shorts_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        YT_CHANNEL_SHORTS_URL,
        json={"units_charged": 1, "data": {"shorts": [{"id": "s1", "title": "Short 1"}]}},
        status=200,
    )
    _mk_connector("za").fetch()
    yt_calls = [c for c in responses_lib.calls if YT_CHANNEL_SHORTS_URL in c.request.url]
    assert len(yt_calls) == 1
    assert "browseId=UCabc" in yt_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_yt_channel_videos_fires_when_flag_true(mock_load_sources, _mock_secret):
    """yt_channel_videos_enabled=true wires /youtube/channel/videos per browseId."""
    mock_load_sources.return_value = _sources(
        {
            "yt_channel_browse_ids": ["UCabc"],
            "yt_channel_videos_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        YT_CHANNEL_VIDEOS_URL,
        json={"units_charged": 1, "data": {"videos": [{"id": "v1", "title": "Video 1"}]}},
        status=200,
    )
    _mk_connector("za").fetch()
    yt_calls = [c for c in responses_lib.calls if YT_CHANNEL_VIDEOS_URL in c.request.url]
    assert len(yt_calls) == 1
    assert "browseId=UCabc" in yt_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_yt_keyword_search_fires_when_flag_true(mock_load_sources, _mock_secret):
    """yt_keyword_search_enabled=true wires /youtube/search per keyword."""
    mock_load_sources.return_value = _sources(
        {
            "yt_keywords": ["amapiano"],
            "yt_keyword_search_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        YT_SEARCH_URL,
        json={
            "units_charged": 1,
            "data": {
                "posts": [{"videoRenderer": {"videoId": "abc", "title": {"runs": [{"text": "x"}]}}}]
            },
        },
        status=200,
    )
    _mk_connector("za").fetch()
    yt_calls = [c for c in responses_lib.calls if YT_SEARCH_URL in c.request.url]
    assert len(yt_calls) == 1
    assert "keyword=amapiano" in yt_calls[0].request.url
    assert "period=overall" in yt_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_yt_wave3_respects_quota_exhausted(mock_load_sources, _mock_secret):
    """If quota trips during the YouTube search call, no further YouTube calls fire."""
    mock_load_sources.return_value = _sources(
        {
            "yt_keywords": ["amapiano", "mzansi"],
            "yt_keyword_search_enabled": True,
        }
    )
    responses_lib.add(responses_lib.GET, YT_SEARCH_URL, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    yt_calls = [c for c in responses_lib.calls if YT_SEARCH_URL in c.request.url]
    # First call trips the breaker; second keyword is skipped.
    assert len(yt_calls) == 1


# Instagram Wave 3 ---------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_ig_user_reels_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """Without ig_user_reels_enabled the connector never hits /instagram/user/reels."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "ig_user_ids": ["999"],
            # No ig_user_reels_enabled.
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    _mk_connector("za").fetch()
    ig_calls = [c for c in responses_lib.calls if IG_USER_REELS_URL in c.request.url]
    assert ig_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_ig_user_reels_fires_when_flag_true(mock_load_sources, _mock_secret):
    """ig_user_reels_enabled=true wires /instagram/user/reels per user_id."""
    mock_load_sources.return_value = _sources(
        {
            "ig_user_ids": ["999"],
            "ig_user_reels_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        IG_USER_REELS_URL,
        json={"units_charged": 1, "data": {"reels": [{"id": "r1", "caption": {"text": "x"}}]}},
        status=200,
    )
    _mk_connector("za").fetch()
    ig_calls = [c for c in responses_lib.calls if IG_USER_REELS_URL in c.request.url]
    assert len(ig_calls) == 1
    assert "user_id=999" in ig_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_ig_user_tagged_posts_fires_when_flag_true(mock_load_sources, _mock_secret):
    """ig_user_tagged_posts_enabled=true wires /instagram/user/tagged-posts."""
    mock_load_sources.return_value = _sources(
        {
            "ig_user_ids": ["999"],
            "ig_user_tagged_posts_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        IG_USER_TAGGED_POSTS_URL,
        json={"units_charged": 1, "data": {"posts": [{"id": "p1"}]}},
        status=200,
    )
    _mk_connector("za").fetch()
    ig_calls = [c for c in responses_lib.calls if IG_USER_TAGGED_POSTS_URL in c.request.url]
    assert len(ig_calls) == 1
    assert "user_id=999" in ig_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_ig_wave3_respects_quota_exhausted(mock_load_sources, _mock_secret):
    """Quota trip on Instagram reels short-circuits remaining IG ids."""
    mock_load_sources.return_value = _sources(
        {
            "ig_user_ids": ["111", "222"],
            "ig_user_reels_enabled": True,
        }
    )
    responses_lib.add(responses_lib.GET, IG_USER_REELS_URL, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    ig_calls = [c for c in responses_lib.calls if IG_USER_REELS_URL in c.request.url]
    assert len(ig_calls) == 1


# TikTok Wave 3 ------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_music_posts_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """Without tt_music_posts_enabled the connector never hits /tt/music/posts."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "tt_music_ids": ["7012345"],
            # No tt_music_posts_enabled.
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    _mk_connector("za").fetch()
    tt_calls = [c for c in responses_lib.calls if TT_MUSIC_POSTS_URL in c.request.url]
    assert tt_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_music_posts_fires_when_flag_true(mock_load_sources, _mock_secret):
    """tt_music_posts_enabled=true wires /tt/music/posts per music_id."""
    mock_load_sources.return_value = _sources(
        {
            "tt_music_ids": ["7012345"],
            "tt_music_posts_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        TT_MUSIC_POSTS_URL,
        json={"units_charged": 1, "data": {"aweme_list": [{"aweme_id": "x", "desc": "y"}]}},
        status=200,
    )
    _mk_connector("za").fetch()
    tt_calls = [c for c in responses_lib.calls if TT_MUSIC_POSTS_URL in c.request.url]
    assert len(tt_calls) == 1
    assert "music_id=7012345" in tt_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_music_quota_exhausted_short_circuits(mock_load_sources, _mock_secret):
    """495 on first music_id stops further music_ids."""
    mock_load_sources.return_value = _sources(
        {
            "tt_music_ids": ["1", "2"],
            "tt_music_posts_enabled": True,
        }
    )
    responses_lib.add(responses_lib.GET, TT_MUSIC_POSTS_URL, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    tt_calls = [c for c in responses_lib.calls if TT_MUSIC_POSTS_URL in c.request.url]
    assert len(tt_calls) == 1


# ---------------------------------------------------------------------------
# Wave 3 enrichment endpoints (28 May 2026)
# ---------------------------------------------------------------------------
# Each enrichment endpoint takes IDs/URLs from rows already collected by the
# per-term dispatch loop and fires a per-item secondary call.


def _yt_comment_payload(units_charged: int = 2, count: int = 1) -> dict:
    items = [
        {
            "id": f"yt_cmt_{i}",
            "text": f"yt comment {i} fire",
            "create_time": 1_713_600_000 + i,
            "like_count": 7 + i,
            "user": {"unique_id": f"yt_u_{i}", "nickname": f"YTU{i}"},
        }
        for i in range(count)
    ]
    return {
        "units_charged": units_charged,
        "data": {
            "info": {"reloadContinuationItemsCommand": {"continuationItems": items}},
            "nextCursor": "",
        },
    }


def _ig_comment_payload(units_charged: int = 2, count: int = 1) -> dict:
    items = [
        {
            "node": {
                "id": f"ig_cmt_{i}",
                "text": f"ig comment {i} cool",
                "created_at": 1_713_600_000 + i,
                "like_count": 3 + i,
                "user": {"username": f"ig_u_{i}", "nickname": f"IGU{i}"},
            }
        }
        for i in range(count)
    ]
    return {"units_charged": units_charged, "data": {"comments": items, "nextCursor": ""}}


def _threads_reply_payload(units_charged: int = 1, count: int = 1) -> dict:
    items = [
        {
            "node": {
                "thread": {
                    "thread_items": [
                        {
                            "post": {
                                "id": f"th_r_{i}",
                                "text": f"threads reply {i}",
                                "create_time": 1_713_600_000 + i,
                                "like_count": 2 + i,
                                "user": {"username": f"th_u_{i}"},
                            }
                        }
                    ]
                }
            }
        }
        for i in range(count)
    ]
    return {"units_charged": units_charged, "data": items}


def _tt_post_info_payload(units_charged: int = 2) -> dict:
    return {
        "units_charged": units_charged,
        "data": [
            {
                "id": "9999999999999999999",
                "aweme_id": "9999999999999999999",
                "desc": "tt post info detail",
                "create_time": 1_713_600_000,
                "play_count": 5000,
                "digg_count": 800,
                "author": {"unique_id": "info_u", "nickname": "InfoU"},
            }
        ],
    }


def _tt_comment_replies_payload(units_charged: int = 1, count: int = 1) -> dict:
    comments = [
        {
            "cid": f"reply_{i}",
            "text": f"tt reply {i} fire",
            "create_time": 1_713_600_000 + i,
            "digg_count": 1 + i,
            "user": {"unique_id": f"r_u_{i}", "nickname": f"RU{i}"},
        }
        for i in range(count)
    ]
    return {"units_charged": units_charged, "data": {"comments": comments, "nextCursor": 0}}


# YouTube video comments enrichment -----------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_yt_video_comments_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """yt_video_comments_enabled off -> /youtube/video/comments never hit."""
    mock_load_sources.return_value = _sources(
        {
            "yt_keywords": ["amapiano"],
            "yt_keyword_search_enabled": True,
        }
    )
    responses_lib.add(
        responses_lib.GET,
        YT_SEARCH_URL,
        json={
            "units_charged": 1,
            "data": {
                "posts": [
                    {
                        "videoRenderer": {
                            "videoId": "abc",
                            "title": {"runs": [{"text": "x"}]},
                        }
                    }
                ]
            },
        },
        status=200,
    )
    _mk_connector("za").fetch()
    cmt_calls = [c for c in responses_lib.calls if YT_VIDEO_COMMENTS_URL in c.request.url]
    assert cmt_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_yt_video_comments_fires_when_flag_true(mock_load_sources, _mock_secret):
    """yt_video_comments_enabled=true fetches comments for top YouTube videos."""
    mock_load_sources.return_value = _sources(
        {
            "yt_keywords": ["amapiano"],
            "yt_keyword_search_enabled": True,
            "yt_video_comments_enabled": True,
            "yt_video_comments_per_run": 1,
        },
        budget=10_000,
    )
    responses_lib.add(
        responses_lib.GET,
        YT_SEARCH_URL,
        json={
            "units_charged": 1,
            "data": {
                "posts": [
                    {
                        "id": "vidABC",
                        "title": "amapiano fire",
                        "url": "https://www.youtube.com/watch?v=vidABC",
                        "view_count": 5000,
                        "like_count": 100,
                    }
                ]
            },
        },
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        YT_VIDEO_COMMENTS_URL,
        json=_yt_comment_payload(count=2),
        status=200,
    )
    _mk_connector("za").fetch()
    cmt_calls = [c for c in responses_lib.calls if YT_VIDEO_COMMENTS_URL in c.request.url]
    assert len(cmt_calls) == 1
    assert "id=vidABC" in cmt_calls[0].request.url
    assert EnsembleConnector._global_units_spent > 0


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_yt_video_comments_respects_quota_exhausted(mock_load_sources, _mock_secret):
    """495 trip on the seeding search call skips the enrichment phase."""
    mock_load_sources.return_value = _sources(
        {
            "yt_keywords": ["a"],
            "yt_keyword_search_enabled": True,
            "yt_video_comments_enabled": True,
        }
    )
    responses_lib.add(responses_lib.GET, YT_SEARCH_URL, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    cmt_calls = [c for c in responses_lib.calls if YT_VIDEO_COMMENTS_URL in c.request.url]
    assert cmt_calls == []


# Instagram post comments enrichment ----------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_ig_post_comments_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """ig_post_comments_enabled off -> /instagram/post/comments never hit."""
    mock_load_sources.return_value = _sources(
        {
            "instagram_hashtags": ["amapiano"],
        }
    )
    responses_lib.add(
        responses_lib.GET,
        IG_HASHTAG_URL,
        json={
            "units_charged": 1,
            "data": [
                {
                    "shortcode": "ABCXYZ",
                    "caption": {"text": "ig post"},
                    "owner_username": "u",
                    "like_count": 50,
                }
            ],
        },
        status=200,
    )
    _mk_connector("za").fetch()
    cmt_calls = [c for c in responses_lib.calls if IG_POST_COMMENTS_URL in c.request.url]
    assert cmt_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_ig_post_comments_fires_when_flag_true(mock_load_sources, _mock_secret):
    """ig_post_comments_enabled=true fetches comments via native numeric media_id."""
    mock_load_sources.return_value = _sources(
        {
            "instagram_hashtags": ["amapiano"],
            "ig_post_comments_enabled": True,
            "ig_post_comments_per_run": 1,
        },
        budget=10_000,
    )
    responses_lib.add(
        responses_lib.GET,
        IG_HASHTAG_URL,
        json={
            "units_charged": 1,
            "data": [
                {
                    "shortcode": "ABCXYZ",
                    "pk": "3926123456789017826",
                    "caption": {"text": "ig post"},
                    "owner_username": "u",
                    "like_count": 50,
                }
            ],
        },
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        IG_POST_COMMENTS_URL,
        json=_ig_comment_payload(count=2),
        status=200,
    )
    _mk_connector("za").fetch()
    cmt_calls = [c for c in responses_lib.calls if IG_POST_COMMENTS_URL in c.request.url]
    assert len(cmt_calls) == 1
    assert "media_id=3926123456789017826" in cmt_calls[0].request.url
    assert "sorting=popular" in cmt_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_ig_post_comments_respects_quota_exhausted(mock_load_sources, _mock_secret):
    """495 on the seeding hashtag call skips IG comments enrichment."""
    mock_load_sources.return_value = _sources(
        {
            "instagram_hashtags": ["amapiano"],
            "ig_post_comments_enabled": True,
        }
    )
    responses_lib.add(responses_lib.GET, IG_HASHTAG_URL, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    cmt_calls = [c for c in responses_lib.calls if IG_POST_COMMENTS_URL in c.request.url]
    assert cmt_calls == []


# Threads post replies enrichment ------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_threads_post_replies_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """threads_post_replies_enabled off -> /threads/post/replies never hit."""
    mock_load_sources.return_value = _sources(
        {
            "threads_keywords": ["amapiano"],
        }
    )
    threads_kw_url = f"{BASE}/threads/keyword/search"
    responses_lib.add(
        responses_lib.GET,
        threads_kw_url,
        json={
            "units_charged": 1,
            "data": [
                {
                    "node": {
                        "thread": {
                            "thread_items": [
                                {
                                    "post": {
                                        "id": "th_p_1",
                                        "code": "thrcode1",
                                        "text": "threads post",
                                        "user": {"username": "u"},
                                        "like_count": 90,
                                    }
                                }
                            ]
                        }
                    }
                }
            ],
        },
        status=200,
    )
    _mk_connector("za").fetch()
    r_calls = [c for c in responses_lib.calls if THREADS_POST_REPLIES_URL in c.request.url]
    assert r_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_threads_post_replies_fires_when_flag_true(mock_load_sources, _mock_secret):
    """threads_post_replies_enabled=true fetches replies for top Threads posts."""
    mock_load_sources.return_value = _sources(
        {
            "threads_keywords": ["amapiano"],
            "threads_post_replies_enabled": True,
            "threads_post_replies_per_run": 1,
        },
        budget=10_000,
    )
    threads_kw_url = f"{BASE}/threads/keyword/search"
    responses_lib.add(
        responses_lib.GET,
        threads_kw_url,
        json={
            "units_charged": 1,
            "data": [
                {
                    "node": {
                        "thread": {
                            "thread_items": [
                                {
                                    "post": {
                                        "id": "th_p_1",
                                        "code": "thrcode1",
                                        "text": "threads post",
                                        "user": {"username": "u"},
                                        "like_count": 90,
                                    }
                                }
                            ]
                        }
                    }
                }
            ],
        },
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        THREADS_POST_REPLIES_URL,
        json=_threads_reply_payload(count=2),
        status=200,
    )
    _mk_connector("za").fetch()
    r_calls = [c for c in responses_lib.calls if THREADS_POST_REPLIES_URL in c.request.url]
    assert len(r_calls) == 1


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_threads_post_replies_respects_quota_exhausted(mock_load_sources, _mock_secret):
    """495 on the seeding threads keyword call skips replies enrichment."""
    mock_load_sources.return_value = _sources(
        {
            "threads_keywords": ["amapiano"],
            "threads_post_replies_enabled": True,
        }
    )
    threads_kw_url = f"{BASE}/threads/keyword/search"
    responses_lib.add(responses_lib.GET, threads_kw_url, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    r_calls = [c for c in responses_lib.calls if THREADS_POST_REPLIES_URL in c.request.url]
    assert r_calls == []


# TikTok post info enrichment ----------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_post_info_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """tt_post_info_enabled off -> /tt/post/info never hit."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    _mk_connector("za").fetch()
    info_calls = [c for c in responses_lib.calls if TT_POST_INFO_URL in c.request.url]
    assert info_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_post_info_fires_when_flag_true_and_cap_positive(mock_load_sources, _mock_secret):
    """tt_post_info_enabled=true + per_run>0 fires /tt/post/info per top URL."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "tt_post_info_enabled": True,
            "tt_post_info_per_run": 1,
        },
        budget=10_000,
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    responses_lib.add(responses_lib.GET, TT_POST_INFO_URL, json=_tt_post_info_payload(), status=200)
    _mk_connector("za").fetch()
    info_calls = [c for c in responses_lib.calls if TT_POST_INFO_URL in c.request.url]
    assert len(info_calls) == 1
    assert "url=" in info_calls[0].request.url


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_post_info_respects_quota_exhausted(mock_load_sources, _mock_secret):
    """495 on the seeding hashtag call skips /tt/post/info."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "tt_post_info_enabled": True,
            "tt_post_info_per_run": 5,
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    info_calls = [c for c in responses_lib.calls if TT_POST_INFO_URL in c.request.url]
    assert info_calls == []


# TikTok comment replies enrichment ----------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_comment_replies_short_circuits_when_flag_disabled(mock_load_sources, _mock_secret):
    """tt_post_comment_replies_enabled off -> /tt/post/comments-replies never hit."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "tt_comments_enabled": True,
            "tt_comment_videos_per_run": 1,
        },
        budget=10_000,
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=_tiktok_response(count=1), status=200)
    responses_lib.add(
        responses_lib.GET,
        TT_COMMENTS_URL,
        json=_tiktok_comments_response(count=1),
        status=200,
    )
    _mk_connector("za").fetch()
    rep_calls = [c for c in responses_lib.calls if TT_COMMENT_REPLIES_URL in c.request.url]
    assert rep_calls == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_comment_replies_fires_when_comment_id_present(mock_load_sources, _mock_secret):
    """Post-fix: the normaliser stashes comment_id=cid, so the (aweme_id,
    comment_id) pair resolves and the reply enrichment fires.

    Inverts the pre-fix test that asserted replies were always skipped:
    that skip was the bug (comment rows never carried comment_id), not the
    intended behaviour. The reply call must now fire and carry both the
    parent aweme_id (the seed video id) and the comment cid (cmt0 from
    _tiktok_comments_response).
    """
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "tt_comments_enabled": True,
            "tt_comment_videos_per_run": 1,
            "tt_post_comment_replies_enabled": True,
            "tt_post_comment_replies_per_run": 1,
        },
        budget=10_000,
    )
    seed = {
        "units_charged": 1,
        "data": [
            {
                "id": "7200000000000000001",
                "aweme_id": "7200000000000000001",
                "desc": "seed",
                "create_time": 1_713_600_000,
                "digg_count": 999,
                "author": {"unique_id": "djtest", "nickname": "DJ"},
            }
        ],
    }
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json=seed, status=200)
    responses_lib.add(
        responses_lib.GET,
        TT_COMMENTS_URL,
        json=_tiktok_comments_response(count=1),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        TT_COMMENT_REPLIES_URL,
        json=_tt_comment_replies_payload(count=1),
        status=200,
    )
    _mk_connector("za").fetch()
    rep_calls = [c for c in responses_lib.calls if TT_COMMENT_REPLIES_URL in c.request.url]
    assert len(rep_calls) >= 1
    url = rep_calls[0].request.url
    assert "cmt0" in url  # the comment cid stashed by the normaliser
    assert "7200000000000000001" in url  # the parent aweme_id


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_tt_comment_replies_respects_quota_exhausted(mock_load_sources, _mock_secret):
    """Quota trip during the seeding hashtag call skips comment-replies."""
    mock_load_sources.return_value = _sources(
        {
            "tiktok_hashtags": ["amapiano"],
            "tt_comments_enabled": True,
            "tt_post_comment_replies_enabled": True,
        }
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json={"error": "quota"}, status=495)
    _mk_connector("za").fetch()
    rep_calls = [c for c in responses_lib.calls if TT_COMMENT_REPLIES_URL in c.request.url]
    assert rep_calls == []


# Direct-method tests ------------------------------------------------------


def test_normalise_tiktok_comments_stashes_comment_id():
    """The normaliser must stash comment_id=cid and keep query_term=parent
    video id, so the comment-replies enrichment can resolve the
    (aweme_id, comment_id) pair. Pins the contract that the reply
    enrichment depends on (regression guard for the silent-drop bug)."""
    conn = _mk_connector("za")
    rows = conn._normalise_tiktok_comments(
        [
            {
                "cid": "abc123",
                "text": "fire comment",
                "create_time": 1_713_600_000,
                "digg_count": 5,
                "user": {"unique_id": "x", "nickname": "X"},
            }
        ],
        query_group="tt_comments",
        parent_video_id="7200000000000000099",
    )
    assert len(rows) == 1
    assert rows[0]["comment_id"] == "abc123"
    assert rows[0]["query_term"] == "7200000000000000099"


def test_extract_youtube_video_id_from_watch_url():
    fn = EnsembleConnector._extract_youtube_video_id
    assert fn("https://www.youtube.com/watch?v=abcDEF_123") == "abcDEF_123"
    assert fn("https://www.youtube.com/shorts/xyz-789") == "xyz-789"
    assert fn("https://youtu.be/short1") == "short1"
    assert fn("https://example.com/foo") == ""
    assert fn("") == ""


def test_extract_instagram_media_id_requires_numeric_id():
    fn = EnsembleConnector._extract_instagram_media_id
    assert fn({"url": "https://www.instagram.com/p/ABCDEF/"}) == ""
    assert fn({"url": "https://www.instagram.com/p/XYZ/?utm=1"}) == ""
    assert (
        fn(
            {
                "native_id": "3926123456789017826",
                "query_term": "media123",
                "url": "https://www.instagram.com/p/Z/",
            }
        )
        == "3926123456789017826"
    )
    assert fn({}) == ""


def test_extract_instagram_media_id_rejects_short_numeric_hashtag():
    # KE instagram_hashtags includes "254"; without a URL or native_id it must
    # NOT become media_id=254 (a guaranteed vendor 4xx that burns the budget).
    fn = EnsembleConnector._extract_instagram_media_id
    assert fn({"query_term": "254"}) == ""
    assert fn({"query_term": "amapiano"}) == ""
    # A real ~18-digit numeric media id with no URL is still accepted.
    assert fn({"query_term": "3216454718029387213"}) == "3216454718029387213"
    # native_id (stashed numeric pk) is used when there is no URL shortcode.
    assert fn({"native_id": "3216454718029387213", "query_term": "254"}) == "3216454718029387213"


def test_extract_threads_post_id_from_url():
    fn = EnsembleConnector._extract_threads_post_id
    # Only native_id works for /threads/post/replies. The URL code and the
    # keyword seed both earn HTTP 422, so they now resolve to "" and the
    # caller skips the post instead of firing a guaranteed rejection.
    assert fn({"url": "https://www.threads.net/@u/post/code1"}) == ""
    assert fn({"query_term": "directid"}) == ""
    assert fn({"native_id": "3882995852994334174"}) == "3882995852994334174"
    assert fn({}) == ""


def test_note_non_quota_4xx_records_only_after_threshold():
    """A single (or sub-threshold) 4xx stays swallowed; the failure summary is
    written only once the same endpoint hits the threshold consecutively."""
    from src.ingestion.connectors.ensemble import NON_QUOTA_4XX_FAILURE_THRESHOLD

    conn = _mk_connector("za")
    for _ in range(NON_QUOTA_4XX_FAILURE_THRESHOLD - 1):
        conn._note_non_quota_4xx("tiktok_hashtag", 422, "https://ensembledata.com/apis/tt/hashtag")
    assert conn._endpoint_failures == []
    # The threshold call records exactly one entry naming the endpoint + status.
    conn._note_non_quota_4xx("tiktok_hashtag", 422, "https://ensembledata.com/apis/tt/hashtag")
    assert len(conn._endpoint_failures) == 1
    assert "tiktok_hashtag" in conn._endpoint_failures[0]
    assert "422" in conn._endpoint_failures[0]


def test_note_endpoint_ok_resets_consecutive_counter():
    """A clean call resets the streak so the counter is consecutive, not
    cumulative: 2 fails, a success, then 2 more fails must not record."""
    from src.ingestion.connectors.ensemble import NON_QUOTA_4XX_FAILURE_THRESHOLD

    assert NON_QUOTA_4XX_FAILURE_THRESHOLD == 3  # test wording assumes this
    conn = _mk_connector("za")
    conn._note_non_quota_4xx("tiktok_keyword", 400, "u")
    conn._note_non_quota_4xx("tiktok_keyword", 400, "u")
    conn._note_endpoint_ok("tiktok_keyword")
    conn._note_non_quota_4xx("tiktok_keyword", 400, "u")
    conn._note_non_quota_4xx("tiktok_keyword", 400, "u")
    assert conn._endpoint_failures == []


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_repeated_4xx_on_one_endpoint_surfaces_in_failures(mock_load_sources, _mock_secret):
    """Three terms against an endpoint stuck on HTTP 422 produce zero rows but
    record the endpoint in _endpoint_failures so the empty cron is not silent."""
    mock_load_sources.return_value = _sources(
        {"tiktok_hashtags": ["a", "b", "c"]},
        budget=10_000,
    )
    responses_lib.add(responses_lib.GET, TT_HASHTAG_URL, json={"error": "bad param"}, status=422)
    conn = _mk_connector("za")
    df = conn.fetch()
    assert len(df) == 0
    # All three terms fired (422 is not retried) and the breaker never tripped.
    assert len(responses_lib.calls) == 3
    assert conn._quota_exhausted is False
    assert len(conn._endpoint_failures) == 1
    assert "tiktok_hashtag" in conn._endpoint_failures[0]


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_threads_replies_skips_posts_without_native_id(mock_load_sources, _mock_secret):
    """A Threads post carrying no pk (native_id) is skipped, not fired at
    /threads/post/replies, since the URL code / keyword seed earn HTTP 422."""
    mock_load_sources.return_value = _sources(
        {
            "threads_keywords": ["amapiano"],
            "threads_post_replies_enabled": True,
            "threads_post_replies_per_run": 5,
        },
        budget=10_000,
    )
    threads_kw_url = f"{BASE}/threads/keyword/search"
    # Post has a code but no pk/id, so _normalise_posts stashes native_id="".
    responses_lib.add(
        responses_lib.GET,
        threads_kw_url,
        json={
            "units_charged": 1,
            "data": [
                {
                    "node": {
                        "thread": {
                            "thread_items": [
                                {
                                    "post": {
                                        "code": "thrcode1",
                                        "text": "threads post",
                                        "user": {"username": "u"},
                                        "like_count": 90,
                                    }
                                }
                            ]
                        }
                    }
                }
            ],
        },
        status=200,
    )
    conn = _mk_connector("za")
    conn.fetch()
    r_calls = [c for c in responses_lib.calls if THREADS_POST_REPLIES_URL in c.request.url]
    assert r_calls == []


def test_extract_threads_post_id_prefers_native_pk():
    # /threads/post/replies needs the numeric pk. _normalise_posts stashes it
    # as native_id; _extract must return it ahead of the search seed
    # (query_term) and the URL code, both of which the endpoint rejects.
    # Regression for the 29 May verify_live probe (pk works, code -> HTTP 422).
    fn = EnsembleConnector._extract_threads_post_id
    post = {
        "native_id": "3882995852994334174",
        "query_term": "mzansi",
        "url": "https://www.threads.net/@afroplugs/post/DXjLCfCCs3e",
    }
    assert fn(post) == "3882995852994334174"


def test_unwrap_threads_post_handles_reply_envelope():
    # /threads/post/replies nests thread_items directly under node (no
    # .thread level, unlike keyword/search). Regression for 29 May probe:
    # without this, replies never unwrap, lose text, and are dropped.
    fn = EnsembleConnector._unwrap_threads_post
    reply = {"node": {"thread_items": [{"post": {"pk": "1", "caption": {"text": "a reply"}}}]}}
    assert fn(reply).get("caption", {}).get("text") == "a reply"
    search = {"node": {"thread": {"thread_items": [{"post": {"caption": {"text": "a post"}}}]}}}
    assert fn(search).get("caption", {}).get("text") == "a post"


def test_normalise_posts_threads_carries_pk_as_native_id():
    conn = _mk_connector(market="za")
    items = [
        {
            "node": {
                "thread": {
                    "thread_items": [
                        {
                            "post": {
                                "pk": 3882995852994334174,
                                "code": "DXjLCfCCs3e",
                                "user": {"username": "afroplugs"},
                                "caption": {"text": "mzansi vibes"},
                                "like_count": 12,
                            }
                        }
                    ]
                }
            }
        }
    ]
    rows = conn._normalise_posts(
        items, platform="threads", query_group="threads_keyword", query_term="mzansi"
    )
    assert rows[0]["native_id"] == "3882995852994334174"
    assert EnsembleConnector._extract_threads_post_id(rows[0]) == "3882995852994334174"


def test_normalise_simple_comments_drops_blank_bodies():
    conn = _mk_connector("za")
    items = [
        {"id": "1", "text": "real comment", "user": {"unique_id": "x"}},
        {"id": "2", "text": "", "user": {"unique_id": "y"}},
        {"id": "3", "text": "   ", "user": {"unique_id": "z"}},
    ]
    rows = conn._normalise_simple_comments(
        items,
        platform="youtube",
        content_type="youtube_video_comment",
        query_group="yt_video_comments",
        parent_id="vidABC",
        parent_url="https://www.youtube.com/watch?v=vidABC",
    )
    assert len(rows) == 1
    assert rows[0]["content_type"] == "youtube_video_comment"
    assert rows[0]["platform"] == "youtube"
    assert rows[0]["query_term"] == "vidABC"
    assert rows[0]["url"] == "https://www.youtube.com/watch?v=vidABC"


def _finalise_row(**kw):
    """Minimal EnsembleData row dict for _finalise dedup tests."""
    base = {
        "source": "EnsembleData",
        "platform": "tiktok",
        "market": "za",
        "content_type": "post",
        "query_group": "music_amapiano",
        "query_term": "amapiano",
        "author_name": "",
        "author_handle": "creator",
        "title": "title",
        "text": "Amapiano vibes tonight",
        "url": "",
        "published_at": None,
        "views": 0.0,
        "likes": 0.0,
        "comments": 0.0,
        "shares": 0.0,
    }
    base.update(kw)
    return base


def test_finalise_keeps_comments_that_share_a_parent_url():
    """Comment/reply rows carrying the same parent post url survive dedup
    because their text and author differ. url-only dedup collapsed them, which
    silently discarded the comment enrichment the connector paid to fetch."""
    connector = EnsembleConnector(market="za")
    parent = "https://www.threads.net/@poster/post/abc"
    rows = [
        _finalise_row(
            content_type="threads_reply", url=parent, text="first take", author_handle="u1"
        ),
        _finalise_row(
            content_type="threads_reply", url=parent, text="second take", author_handle="u2"
        ),
        _finalise_row(
            content_type="threads_reply", url=parent, text="third take", author_handle="u3"
        ),
    ]
    df = connector._finalise(rows)
    assert len(df) == 3


def test_finalise_still_collapses_a_duplicate_post():
    """A post fetched via two hashtags (identical url, text, author) still dedups
    to one row, so the live post path is byte-identical."""
    connector = EnsembleConnector(market="za")
    url = "https://www.tiktok.com/@x/video/123"
    rows = [
        _finalise_row(content_type="post", url=url, text="same caption", author_handle="x"),
        _finalise_row(content_type="post", url=url, text="same caption", author_handle="x"),
    ]
    df = connector._finalise(rows)
    assert len(df) == 1


# ---------------------------------------------------------------------------
# Transport circuit breaker (timeouts / connection errors)
# ---------------------------------------------------------------------------


def test_transport_breaker_trips_after_threshold():
    """N consecutive timeouts flip the quota breaker so the run stops spending
    wall clock on a hung vendor (the 13/14 Jul dead-night fix)."""
    import requests
    from src.ingestion.connectors.ensemble import TRANSPORT_FAILURE_THRESHOLD

    EnsembleConnector.reset_global_budget()
    c = _mk_connector()
    for i in range(TRANSPORT_FAILURE_THRESHOLD - 1):
        c._handle_request_exception("safe/url", requests.exceptions.Timeout("hang"))
        assert c._quota_exhausted is False, f"tripped too early at {i + 1}"
    # The threshold-th consecutive timeout trips it.
    c._handle_request_exception("safe/url", requests.exceptions.Timeout("hang"))
    assert c._quota_exhausted is True
    assert any("transport" in f for f in c._endpoint_failures)


def test_transport_streak_resets_on_success():
    """A successful call proves the vendor is up and clears the streak, so
    intermittent timeouts never trip the breaker."""
    import requests
    from src.ingestion.connectors.ensemble import TRANSPORT_FAILURE_THRESHOLD

    EnsembleConnector.reset_global_budget()
    c = _mk_connector()
    for _ in range(TRANSPORT_FAILURE_THRESHOLD - 1):
        c._handle_request_exception("safe/url", requests.exceptions.Timeout("hang"))
    c._note_transport_ok()
    # Streak reset: another failure is now #1, nowhere near the threshold.
    c._handle_request_exception("safe/url", requests.exceptions.Timeout("hang"))
    assert c._quota_exhausted is False


def test_connection_error_counts_toward_breaker():
    import requests
    from src.ingestion.connectors.ensemble import TRANSPORT_FAILURE_THRESHOLD

    EnsembleConnector.reset_global_budget()
    c = _mk_connector()
    for _ in range(TRANSPORT_FAILURE_THRESHOLD):
        c._handle_request_exception("safe/url", requests.exceptions.ConnectionError("down"))
    assert c._quota_exhausted is True


def test_non_transport_request_error_does_not_count():
    """A generic RequestException (not a timeout/connection error) is logged
    but never counts toward the transport breaker."""
    import requests
    from src.ingestion.connectors.ensemble import TRANSPORT_FAILURE_THRESHOLD

    EnsembleConnector.reset_global_budget()
    c = _mk_connector()
    for _ in range(TRANSPORT_FAILURE_THRESHOLD + 2):
        c._handle_request_exception("safe/url", requests.exceptions.RequestException("weird"))
    assert c._quota_exhausted is False
    assert EnsembleConnector._global_transport_failures == 0


# ---------------------------------------------------------------------------
# Retirement gate: a cancelled vendor must not be restarted by a live token
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_fetch_is_inert_when_legacy_block_disabled(mock_load_sources, _mock_secret):
    """A present token must not restart a vendor the config marks retired.

    configs/sources.yaml carries ``ensemble_budget.enabled: false`` with the note
    "RETIRED 23 Jul 2026, vendor cancelled", but fetch() read the token first and
    never consulted it, so the connector kept calling a cancelled account. Proven
    live on 2026-08-20: the cron logged HTTP 493 against
    ensembledata.com/apis/twitter/user/info.

    responses_lib with no registered endpoint fails any HTTP call, so a request
    escaping the gate raises rather than passing quietly.
    """
    mock_load_sources.return_value = {
        "ensemble_budget": {"enabled": False, "budget_units_per_run": 3000},
        "ensembledata": {"za": {"tiktok_hashtags": ["amapiano"]}},
    }

    df = _mk_connector().fetch()

    assert df.empty
    assert len(responses_lib.calls) == 0


@responses_lib.activate
@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_fetch_is_inert_when_spec_block_disabled(mock_load_sources, _mock_secret):
    """The same gate holds for the preferred ``ensemble`` block shape."""
    block = _sources({"tiktok_hashtags": ["amapiano"]})
    block["ensemble"]["enabled"] = False
    mock_load_sources.return_value = block

    df = _mk_connector().fetch()

    assert df.empty
    assert len(responses_lib.calls) == 0


@patch("src.ingestion.connectors.ensemble.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.ensemble.load_sources")
def test_fetch_gate_defaults_to_enabled_when_key_absent(mock_load_sources, _mock_secret):
    """No ``enabled`` key means enabled, so a config that never carried the flag
    behaves exactly as before. Asserted on the gate itself, not on a full fetch,
    so this test says nothing about the rest of the pipeline."""
    import src.ingestion.connectors.ensemble as ens

    assert ens._ensemble_enabled(_sources({"tiktok_hashtags": ["amapiano"]})) is True
    assert ens._ensemble_enabled({"ensemble_budget": {"budget_units_per_run": 10}}) is True
    assert ens._ensemble_enabled({}) is True
