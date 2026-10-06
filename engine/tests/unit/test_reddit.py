"""Unit tests for RedditConnector.

Coverage:
- disabled flag short-circuits returns empty DataFrame
- enabled+populated fetches posts, comments, keyword search
- HTTP 404 marks subreddit as dead and skips future calls
- HTTP 495 (Ensemble quota) flips shared quota flag
- subreddit dedup across markets via _fetched_subreddit_sorts cache
- normalisation maps to 16-col RAW schema
- post-id dedup across sort modes
- url-level dedup across endpoints
- raw token never appears in logged URL (querystring redaction)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import responses as responses_lib
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.ensemble import EnsembleConnector
from src.ingestion.connectors.reddit import RedditConnector

FAKE_TOKEN = "test-reddit-token-do-not-log"
BASE = "https://ensembledata.com/apis"
SUBREDDIT_POSTS_URL = f"{BASE}/reddit/subreddit/posts"
POST_COMMENTS_URL = f"{BASE}/reddit/post/comments"
KEYWORD_SEARCH_URL = f"{BASE}/reddit/keyword/search"


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset class-level caches + shared Ensemble ledger between tests."""
    RedditConnector.reset_caches()
    EnsembleConnector.reset_global_budget()
    yield
    RedditConnector.reset_caches()
    EnsembleConnector.reset_global_budget()


def _post_payload(post_id: str, score: int = 10, sub: str = "amapiano", **overrides) -> dict:
    """Build one Reddit-shaped post dict."""
    p = {
        "id": post_id,
        "title": f"Post {post_id}",
        "selftext": f"Body for {post_id}",
        "author": f"user_{post_id}",
        "subreddit": sub,
        "permalink": f"/r/{sub}/comments/{post_id}/title/",
        "created_utc": 1_716_000_000,
        "score": score,
        "num_comments": 5,
        "url": f"https://www.reddit.com/r/{sub}/comments/{post_id}/title/",
    }
    p.update(overrides)
    return p


def _comment_payload(cid: str, body: str = "great track", score: int = 5) -> dict:
    return {
        "id": cid,
        "body": body,
        "author": f"commenter_{cid}",
        "score": score,
        "created_utc": 1_716_000_500,
        "permalink": f"/r/amapiano/comments/post1/title/{cid}/",
    }


def _vendor_response(items: list, units: int = 5) -> dict:
    return {"units_charged": units, "data": items}


def _sources(
    enabled: bool = True,
    subreddits_za: list | None = None,
    pan_african: list | None = None,
    slang: list | None = None,
    sort_modes: list | None = None,
    comment_posts_per_sub: int = 5,
    keyword_search_max: int = 25,
    budget: int = 1500,
) -> dict:
    return {
        "reddit": {
            "enabled": enabled,
            "budget_units_per_run": budget,
            "sort_modes": sort_modes or ["hot"],
            "top_period": "day",
            "posts_per_sort": 5,
            "comment_posts_per_sub": comment_posts_per_sub,
            "comments_per_post": 10,
            "keyword_search_max": keyword_search_max,
            "subreddits": {
                "za": subreddits_za if subreddits_za is not None else ["amapiano"],
                "pan_african": pan_african or [],
            },
            "slang_keywords": {"za": slang or []},
        }
    }


def _mk_connector(market: str = "za") -> RedditConnector:
    c = RedditConnector(market=market)
    c.RATE_LIMIT_DELAY = 0
    return c


# ---------------------------------------------------------------------------
# Disabled / unconfigured paths
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_fetch_when_disabled_returns_empty(mock_load_sources, _mock_secret):
    """enabled=false means zero HTTP calls and empty 20-col DataFrame."""
    mock_load_sources.return_value = _sources(enabled=False)
    df = _mk_connector("za").fetch()
    assert list(df.columns) == list(RAW_COLUMNS)
    assert df.empty
    assert len(responses_lib.calls) == 0


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=None)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_fetch_when_token_missing_returns_empty(mock_load_sources, _mock_secret):
    """Missing ENSEMBLEDATA_API_TOKEN means empty DataFrame, no calls."""
    mock_load_sources.return_value = _sources()
    df = _mk_connector("za").fetch()
    assert df.empty
    assert len(responses_lib.calls) == 0


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_fetch_when_quota_already_exhausted_returns_empty(mock_load_sources, _mock_secret):
    """If EnsembleConnector flagged quota exhausted, Reddit short-circuits."""
    EnsembleConnector._global_quota_exhausted = True
    mock_load_sources.return_value = _sources()
    df = _mk_connector("za").fetch()
    assert df.empty
    assert len(responses_lib.calls) == 0


# ---------------------------------------------------------------------------
# Happy path: posts -> comments -> keyword search
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_fetch_happy_path_yields_raw_schema(mock_load_sources, _mock_secret):
    """Single sub, hot only, with one comment fetch + one keyword search."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["amapiano"],
        slang=["amapiano"],
        sort_modes=["hot"],
        comment_posts_per_sub=1,
        keyword_search_max=1,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1", score=100)]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        POST_COMMENTS_URL,
        json=_vendor_response([_comment_payload("c1", body="this song slaps")]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        KEYWORD_SEARCH_URL,
        json=_vendor_response([_post_payload("k1", sub="Naija")]),
        status=200,
    )

    df = _mk_connector("za").fetch()

    assert list(df.columns) == list(RAW_COLUMNS)
    # 1 post + 1 comment + 1 keyword-search post = 3 rows
    assert len(df) == 3
    assert set(df["platform"]) == {"reddit"}
    assert set(df["market"]) == {"za"}
    assert "reddit_post" in set(df["content_type"])
    assert "reddit_comment" in set(df["content_type"])


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_reddit_spend_charges_own_ledger_not_ensemble_gate(mock_load_sources, _mock_secret):
    """Reddit must charge its own ledger, NOT EnsembleConnector._global_units_spent.
    The latter gates ensemble's per-run budget; if Reddit inflated it, the NG/KE
    ensemble passes that run after ZA Reddit would fire zero calls and lose all
    TikTok/IG/Threads ingestion. The shared 495 quota flag stays shared."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["amapiano"],
        slang=["amapiano"],
        sort_modes=["hot"],
        comment_posts_per_sub=1,
        keyword_search_max=1,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1", score=100)], units=5),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        POST_COMMENTS_URL,
        json=_vendor_response([_comment_payload("c1")], units=5),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        KEYWORD_SEARCH_URL,
        json=_vendor_response([_post_payload("k1", sub="Naija")], units=5),
        status=200,
    )

    _mk_connector("za").fetch()

    # Ensemble's budget-gate ledger is untouched by Reddit.
    assert EnsembleConnector._global_units_spent == 0
    # Reddit's own ledger carries its spend.
    assert EnsembleConnector._global_reddit_units_spent > 0

    # A following ensemble market is therefore not budget-starved.
    ens = EnsembleConnector(market="ng")
    ens._per_market_budget = 800
    assert ens._budget_reached(800) is False


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_subreddit_name_prepended_to_text(mock_load_sources, _mock_secret):
    """Topic classifier should see [r/<sub>] in text so r/amapiano routes via subname."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["Amapiano"],
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1", sub="Amapiano")]),
        status=200,
    )

    df = _mk_connector("za").fetch()

    assert len(df) == 1
    assert "[r/Amapiano]" in df.iloc[0]["text"]


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_post_id_dedup_across_sort_modes(mock_load_sources, _mock_secret):
    """Same post appearing in hot and rising collapses to one row."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["amapiano"],
        sort_modes=["hot", "rising"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
    )
    # Same post id "p1" returned in both sort responses
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1")]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1"), _post_payload("p2")]),
        status=200,
    )

    df = _mk_connector("za").fetch()
    assert len(df) == 2  # p1 deduped, p2 unique
    assert {
        c.request.url.split("token=")[0]
        for c in responses_lib.calls
        if "subreddit/posts" in c.request.url
    }


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_subreddit_404_marks_dead_and_continues(mock_load_sources, _mock_secret):
    """A 404 on subreddit/posts adds the sub to _dead_subreddits, pipeline survives."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["deadsub", "amapiano"],
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json={"error": "not found"},
        status=404,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1")]),
        status=200,
    )

    df = _mk_connector("za").fetch()

    assert "deadsub" in RedditConnector._dead_subreddits
    assert len(df) == 1
    assert df.iloc[0]["url"].endswith("p1/title/")


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_quota_495_flips_shared_flag_and_aborts(mock_load_sources, _mock_secret):
    """495 quota response sets EnsembleConnector._global_quota_exhausted."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["amapiano", "afrobeats"],
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json={"error": "quota exhausted"},
        status=495,
    )

    df = _mk_connector("za").fetch()

    assert df.empty
    assert EnsembleConnector._global_quota_exhausted is True


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_500_error_does_not_crash(mock_load_sources, _mock_secret):
    """Vendor 5xx returns empty for that sub, pipeline continues."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["amapiano"],
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json={"error": "internal"},
        status=500,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json={"error": "internal"},
        status=500,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json={"error": "internal"},
        status=500,
    )

    df = _mk_connector("za").fetch()

    assert df.empty  # 500 retries exhaust, fetch returns empty


# ---------------------------------------------------------------------------
# Cross-market subreddit dedup
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_pan_african_sub_fetched_once_across_markets(mock_load_sources, _mock_secret):
    """Africa appears in both ZA and NG pools; only one HTTP call fires."""
    mock_load_sources.return_value = {
        "reddit": {
            "enabled": True,
            "budget_units_per_run": 1500,
            "sort_modes": ["hot"],
            "top_period": "day",
            "posts_per_sort": 5,
            "comment_posts_per_sub": 0,
            "comments_per_post": 0,
            "keyword_search_max": 0,
            "subreddits": {
                "za": ["Africa"],
                "ng": ["Africa"],
                "pan_african": [],
            },
            "slang_keywords": {"za": [], "ng": []},
        }
    }
    # Only ONE stub. If the connector tries to fire twice, the second call
    # gets ConnectionError from responses_lib (no matching stub).
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1", sub="Africa")]),
        status=200,
    )

    df_za = _mk_connector("za").fetch()
    df_ng = _mk_connector("ng").fetch()

    africa_calls = [
        c
        for c in responses_lib.calls
        if "subreddit/posts" in c.request.url and "Africa" in c.request.url
    ]
    assert len(africa_calls) == 1
    assert len(df_za) == 1
    assert df_ng.empty  # cache hit -> no new fetch


# ---------------------------------------------------------------------------
# Global ledger ceiling: total Reddit spend bounded across markets
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_global_ledger_ceiling_stops_second_market(mock_load_sources, _mock_secret):
    """budget_units_per_run is a GLOBAL ceiling on the class ledger, not a
    per-market reset. Once the first market's spend reaches the ceiling, the
    second market must fire zero HTTP calls instead of being allowed its own
    fresh budget (the markets x budget over-spend bug)."""
    # Budget 5 == one call's unit cost. ZA's first call exhausts the ceiling;
    # NG must not call at all.
    mock_load_sources.return_value = {
        "reddit": {
            "enabled": True,
            "budget_units_per_run": 5,
            "sort_modes": ["hot"],
            "top_period": "day",
            "posts_per_sort": 5,
            "comment_posts_per_sub": 0,
            "comments_per_post": 0,
            "keyword_search_max": 0,
            "subreddits": {
                "za": ["amapiano"],
                "ng": ["afrobeats"],
                "pan_african": [],
            },
            "slang_keywords": {"za": [], "ng": []},
        }
    }
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1")], units=5),
        status=200,
    )

    df_za = _mk_connector("za").fetch()
    df_ng = _mk_connector("ng").fetch()

    assert len(df_za) == 1
    assert df_ng.empty
    # ZA fired exactly one subreddit call; NG fired none (ceiling reached).
    sub_calls = [c for c in responses_lib.calls if "subreddit/posts" in c.request.url]
    assert len(sub_calls) == 1
    assert EnsembleConnector._global_reddit_units_spent == 5


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_preloaded_ledger_blocks_first_call(mock_load_sources, _mock_secret):
    """The Phase 1 loop reads the class ledger before each call. If a prior
    market already spent the ceiling, this market fires zero calls even though
    its own local run tally starts at zero (proves the pre-check reads the
    shared ledger, not just the local counter)."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["amapiano", "kasi"],
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
        budget=10,
    )
    # Simulate a prior market having already spent the whole ceiling.
    EnsembleConnector._global_reddit_units_spent = 10

    df = _mk_connector("za").fetch()

    assert df.empty
    sub_calls = [c for c in responses_lib.calls if "subreddit/posts" in c.request.url]
    assert len(sub_calls) == 0


# ---------------------------------------------------------------------------
# Circuit breaker: repeated non-404 4xx aborts the phase
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_repeated_4xx_trips_circuit_breaker(mock_load_sources, _mock_secret):
    """A systemic vendor contract break returns the same non-404 4xx for every
    sub. The connector aborts the fetch after a small consecutive-failure
    threshold instead of burning a call per sub for the whole pool."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"],
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
    )
    # Every subreddit call returns 422 (contract break, not a dead sub).
    for _ in range(8):
        responses_lib.add(
            responses_lib.GET,
            SUBREDDIT_POSTS_URL,
            json={"detail": "unprocessable"},
            status=422,
        )

    df = _mk_connector("za").fetch()

    assert df.empty
    sub_calls = [c for c in responses_lib.calls if "subreddit/posts" in c.request.url]
    # Breaker trips well before all 8 subs are attempted.
    assert 0 < len(sub_calls) < 8
    # 422 is not a quota / auth sentinel, so the shared flag stays down.
    assert EnsembleConnector._global_quota_exhausted is False


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_404s_do_not_trip_circuit_breaker(mock_load_sources, _mock_secret):
    """404 marks a sub dead (existing behaviour) and must NOT count toward the
    consecutive-4xx breaker, so a live sub after a run of dead ones still
    fetches."""
    subs = [f"dead{i}" for i in range(6)] + ["amapiano"]
    mock_load_sources.return_value = _sources(
        subreddits_za=subs,
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
    )
    for _ in range(6):
        responses_lib.add(
            responses_lib.GET,
            SUBREDDIT_POSTS_URL,
            json={"error": "not found"},
            status=404,
        )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1")]),
        status=200,
    )

    df = _mk_connector("za").fetch()

    assert len(df) == 1  # the live sub after 6 dead ones still fetched
    for i in range(6):
        assert f"dead{i}" in RedditConnector._dead_subreddits


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_intermittent_4xx_does_not_trip_breaker(mock_load_sources, _mock_secret):
    """A single 4xx between good calls must reset the consecutive counter so a
    one-off transient does not abort the whole fetch."""
    mock_load_sources.return_value = _sources(
        subreddits_za=["s1", "s2", "s3"],
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=0,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p1")]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json={"detail": "transient"},
        status=400,
    )
    responses_lib.add(
        responses_lib.GET,
        SUBREDDIT_POSTS_URL,
        json=_vendor_response([_post_payload("p3")]),
        status=200,
    )

    df = _mk_connector("za").fetch()

    sub_calls = [c for c in responses_lib.calls if "subreddit/posts" in c.request.url]
    assert len(sub_calls) == 3  # all three attempted, breaker never tripped
    assert len(df) == 2  # p1 + p3 (the 400 yielded nothing)


# ---------------------------------------------------------------------------
# Normalisation safety
# ---------------------------------------------------------------------------


def test_normalise_post_handles_missing_fields():
    """Post dict with only an id still returns a valid row (16+4 cols)."""
    c = _mk_connector("za")
    row = c._normalise_post({"id": "abc"}, subreddit="test", query_term="hot")
    assert row is not None
    assert row["source"] == "Reddit"
    assert row["platform"] == "reddit"
    assert row["url"].startswith("https://www.reddit.com")
    assert row["views"] == 0.0  # Reddit doesn't expose post views


def test_normalise_post_returns_none_when_no_id():
    """Without an id we can't dedup -> reject the row."""
    c = _mk_connector("za")
    assert c._normalise_post({"title": "no id"}, subreddit="x", query_term="hot") is None


def test_token_redacted_from_safe_url():
    """The querystring carrying the token must not appear in the safe URL."""
    redacted = RedditConnector._safe_url(
        "https://ensembledata.com/apis/reddit/subreddit/posts?name=x&token=SECRET"
    )
    assert "SECRET" not in redacted
    assert redacted == "https://ensembledata.com/apis/reddit/subreddit/posts"


def test_extract_items_unwraps_reddit_kind_data():
    """Reddit's classic API wraps items in {kind, data}; unwrap to inner dicts."""
    payload = {
        "data": {
            "children": [
                {"kind": "t3", "data": {"id": "a", "title": "A"}},
                {"kind": "t3", "data": {"id": "b", "title": "B"}},
            ]
        }
    }
    items = RedditConnector._extract_items(payload)
    assert len(items) == 2
    assert items[0]["id"] == "a"
    assert items[1]["title"] == "B"


# ---------------------------------------------------------------------------
# Source-context guard: keyword search must drop foreign-geo subreddits
# ---------------------------------------------------------------------------


def test_resolve_keyword_exclude_default_and_override():
    """Default exclude set carries the known collisions; a yaml list replaces
    it and is normalised (r/ stripped, lower-cased).
    """
    from src.ingestion.connectors.reddit import DEFAULT_KEYWORD_SUBREDDIT_EXCLUDE

    c = _mk_connector("ng")
    assert c._resolve_keyword_exclude({}) == DEFAULT_KEYWORD_SUBREDDIT_EXCLUDE
    assert "ankara" in c._resolve_keyword_exclude({})
    got = c._resolve_keyword_exclude(
        {"keyword_search_exclude_subreddits": ["r/AskTurkey", "Bursa"]}
    )
    assert got == frozenset({"askturkey", "bursa"})
    # Names starting with 'r' must keep their leading r: only the literal
    # "r/" prefix is removed, not any leading run of r/ characters.
    got_r = c._resolve_keyword_exclude({"keyword_search_exclude_subreddits": ["r/Rwanda", "ramen"]})
    assert got_r == frozenset({"rwanda", "ramen"})


@responses_lib.activate
@patch("src.ingestion.connectors.reddit.get_secret", return_value=FAKE_TOKEN)
@patch("src.ingestion.connectors.reddit.load_sources")
def test_keyword_search_drops_foreign_subreddit(mock_load_sources, _mock_secret):
    """A keyword search for 'ankara' (the NG fabric) returns posts from
    r/ankara (the Turkish capital city sub). The source-context guard drops the
    foreign-sub hit at ingestion while keeping the legit NG one. This is the
    8-Jun "Central Europe debate" r/ankara meme the geo-blocklist could not
    catch (no Turkish token, only the word "ankara").
    """
    mock_load_sources.return_value = _sources(
        subreddits_za=[],  # no subreddit pool, only the keyword search runs
        slang=["ankara"],
        sort_modes=["hot"],
        comment_posts_per_sub=0,
        keyword_search_max=1,
    )
    responses_lib.add(
        responses_lib.GET,
        KEYWORD_SEARCH_URL,
        json=_vendor_response(
            [
                _post_payload("turkish1", sub="ankara"),  # Turkish city sub -> dropped
                _post_payload("naija1", sub="Naija"),  # legit NG hit -> kept
            ]
        ),
        status=200,
    )

    df = _mk_connector("za").fetch()
    blob = df.to_csv(index=False)
    assert "[r/Naija]" in blob  # the legit NG keyword-search hit survives
    assert "[r/ankara]" not in blob  # the Turkish r/ankara hit is dropped
    assert len(df) == 1


@patch("src.ingestion.connectors.reddit.get_secret")
@patch("src.ingestion.connectors.reddit.load_sources")
def test_disabled_reddit_never_reaches_secret_manager(mock_load_sources, mock_secret):
    """A disabled connector must not call Secret Manager.

    The gate was always correct; the ordering was not. Reading the token first
    meant one Secret Manager call per market for a vendor cancelled on
    23 Jul 2026, and those calls now fail: three ERROR lines on the 2026-08-20
    cron came from this path. Unactionable errors are what bury actionable ones.
    """
    mock_load_sources.return_value = {"reddit": {"enabled": False}}

    df = RedditConnector(market="za").fetch()

    assert df.empty
    mock_secret.assert_not_called()
