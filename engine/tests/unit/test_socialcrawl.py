"""Unit tests for the SocialCrawl connector. No network: requests.Session is
patched to return canned {computed, post} envelopes.

The date and envelope cases here are the live-probe findings from 23 Jul 2026
frozen as regressions: Twitter's own timestamp format, the google_news
`article` envelope, and the recency window that stops a six-year-old tweet
reaching enriched_content.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

RECENT = (datetime.now(UTC) - timedelta(days=1)).isoformat()
ANCIENT = (datetime.now(UTC) - timedelta(days=400)).isoformat()


def _cfg(**over):
    c = {
        "enabled": True,
        "budget_credits_per_run": 100,
        "max_age_days": 21,
        # One phase at a time keeps each test's call sequence deterministic.
        "phases": ["search"],
        "phase_share": dict.fromkeys(
            ("discover", "creators", "search", "reddit", "accounts", "news"), 1.0
        ),
        "caps": {"search_terms": 1, "threads_terms": 0},
        "markets": {"za": {"terms": ["amapiano"]}},
    }
    c.update(over)
    return {"socialcrawl": c}


def _item(vid="v1", views=1000, text="Amapiano #fyp", handle="dj.x", published=None):
    return {
        "computed": {"engagement_rate": 0.05, "language": "en"},
        "post": {
            "id": vid,
            "url": f"https://tiktok.com/@{handle}/video/{vid}",
            "published_at": published if published is not None else RECENT,
            "author": {"username": handle, "display_name": "DJ X", "verified": False},
            "content": {"text": text},
            "engagement": {"views": views, "likes": 50, "comments": 5, "shares": 0},
        },
    }


def _resp(
    items,
    credits_used=1,
    success=True,
    error=None,
    next_cursor=None,
    has_more=None,
    wrapped_cursor=None,
):
    r = MagicMock()
    r.raise_for_status.return_value = None
    body = {"success": success, "credits_used": credits_used, "data": {"items": items}}
    if next_cursor is not None:
        body["data"]["next_cursor"] = next_cursor
    if wrapped_cursor is not None or has_more is not None:
        pag = {}
        if wrapped_cursor is not None:
            pag["next_cursor"] = wrapped_cursor
        if has_more is not None:
            pag["has_more"] = has_more
        body["pagination"] = pag
    if error is not None:
        body["error"] = error
    r.json.return_value = body
    return r


def _cursors_sent(session, surface):
    """Every `cursor` param the connector sent to `surface`, page order."""
    out = []
    for call in session.get.call_args_list:
        if call.args and call.args[0].endswith(surface):
            out.append((call.kwargs.get("params") or {}).get("cursor"))
    return out


@contextmanager
def caplog_at(name):
    """Collect formatted records off one logger, ignoring propagation config."""
    records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger(name)
    sink = _Sink()
    logger.addHandler(sink)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous)


def _session(responses):
    s = MagicMock()
    s.headers = {}
    s.get.side_effect = responses
    return s


def _run(cfg, responses, market="za"):
    c, df, _ = _run_with_session(cfg, responses, market)
    return c, df


def _run_with_session(cfg, responses, market="za"):
    """As _run, but also hands back the patched session for call inspection."""
    SocialCrawlConnector.reset_credits()
    session = _session(responses)
    with (
        patch("src.ingestion.connectors.socialcrawl.load_sources", return_value=cfg),
        patch("src.ingestion.connectors.socialcrawl.get_secret", return_value="sc_key"),
        patch("requests.Session", return_value=session),
    ):
        c = SocialCrawlConnector(market=market)
        return c, c.fetch(), session


# -- gating ----------------------------------------------------------------


def test_disabled_returns_empty():
    _, df = _run(_cfg(enabled=False), [])
    assert df.empty


def test_missing_key_returns_empty():
    SocialCrawlConnector.reset_credits()
    with (
        patch("src.ingestion.connectors.socialcrawl.load_sources", return_value=_cfg()),
        patch("src.ingestion.connectors.socialcrawl.get_secret", return_value=""),
        patch.dict("os.environ", {"SOCIALCRAWL_API_KEY": ""}, clear=False),
    ):
        c = SocialCrawlConnector(market="za")
        df = c.fetch()
    assert df.empty
    assert any("SOCIALCRAWL_API_KEY" in f for f in c._fetch_failures)


# -- normalisation ---------------------------------------------------------


def test_happy_path_normalises():
    _, df = _run(_cfg(), [_resp([_item()]), _resp([])])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["source"] == "socialcrawl"
    assert row["market"] == "za"
    assert row["query_term"] == "amapiano"
    assert row["author_handle"] == "dj.x"
    assert row["views"] == 1000.0


def test_platform_is_real_not_socialcrawl():
    """Downstream diversity scoring and seed_graph key off the real platform."""
    _, df = _run(_cfg(), [_resp([_item()]), _resp([_item(vid="y1")])])
    assert set(df["platform"]) == {"tiktok", "youtube"}
    assert "socialcrawl" not in set(df["platform"])


# -- dates -----------------------------------------------------------------


def test_twitter_date_format_is_parsed():
    """twitter/user/tweets returns 'Wed Oct 30 05:45:03 +0000 2019', not ISO."""
    parsed = SocialCrawlConnector._parse_published("Wed Oct 30 05:45:03 +0000 2019")
    assert parsed.startswith("2019-10-30T05:45:03")


def test_dataforseo_space_date_is_parsed():
    parsed = SocialCrawlConnector._parse_published("2026-07-23 10:06:18 +00:00")
    assert parsed.startswith("2026-07-23T10:06:18")


def test_unparseable_date_becomes_empty_not_garbage():
    assert SocialCrawlConnector._parse_published("not a date") == ""
    assert SocialCrawlConnector._parse_published(None) == ""


def test_stale_post_is_dropped():
    """A 400-day-old tweet must not reach enriched_content."""
    _, df = _run(_cfg(), [_resp([_item(published=ANCIENT)]), _resp([])])
    assert df.empty


def test_missing_date_is_kept():
    """Absent dates are common on some surfaces; dropping them guts the feed."""
    _, df = _run(_cfg(), [_resp([_item(published=None)]), _resp([])])
    assert len(df) == 1


# -- budget ----------------------------------------------------------------


def test_credit_budget_stops_spending():
    cfg = _cfg(budget_credits_per_run=2, caps={"search_terms": 5, "threads_terms": 0})
    cfg["socialcrawl"]["markets"]["za"]["terms"] = ["a", "b", "c", "d", "e"]
    _run(cfg, [_resp([_item()], credits_used=1) for _ in range(20)])
    assert SocialCrawlConnector.credits_used() <= 2


def test_reported_credits_are_charged():
    _, _ = _run(_cfg(), [_resp([_item()], credits_used=5), _resp([], credits_used=3)])
    assert SocialCrawlConnector.credits_used() == 8


def test_cache_hit_charges_nothing():
    """credits_used=0 is a cache hit or a refund; the vendor does not bill it."""
    _, df = _run(_cfg(), [_resp([_item()], credits_used=0), _resp([], credits_used=0)])
    assert SocialCrawlConnector.credits_used() == 0
    assert len(df) == 1


def test_phase_share_bounds_one_phase():
    """A phase cannot eat more than its slice of the run budget."""
    cfg = _cfg(
        budget_credits_per_run=100,
        phase_share={"search": 0.02},
        caps={"search_terms": 20, "threads_terms": 0},
    )
    cfg["socialcrawl"]["markets"]["za"]["terms"] = [f"t{i}" for i in range(20)]
    _run(cfg, [_resp([_item()], credits_used=1) for _ in range(60)])
    assert SocialCrawlConnector._global_credits_by_phase.get("search", 0) <= 2


def test_insufficient_credits_halts_the_run():
    cfg = _cfg(caps={"search_terms": 5, "threads_terms": 0})
    cfg["socialcrawl"]["markets"]["za"]["terms"] = ["a", "b", "c", "d", "e"]
    c, df = _run(
        cfg,
        [_resp([], credits_used=0, success=False, error={"type": "INSUFFICIENT_CREDITS"})]
        + [_resp([_item()]) for _ in range(20)],
    )
    assert SocialCrawlConnector._global_out_of_credit is True
    assert df.empty
    assert any("INSUFFICIENT_CREDITS" in f for f in c._fetch_failures)


# -- failure handling ------------------------------------------------------


def test_http_error_is_non_fatal():
    import requests

    c, df = _run(_cfg(), [requests.RequestException("boom"), _resp([])])
    assert df.empty
    assert any("socialcrawl" in f for f in c._fetch_failures)


def test_api_success_false_is_non_fatal():
    c, df = _run(
        _cfg(),
        [
            _resp([], credits_used=0, success=False, error={"type": "RESOURCE_NOT_FOUND"}),
            _resp([_item(vid="y1")]),
        ],
    )
    assert len(df) == 1
    assert any("RESOURCE_NOT_FOUND" in f for f in c._fetch_failures)


# A vendor error that names only the surface cannot be traced back to the id
# that caused it. The 00:30 cron on 24 Aug 2026 logged 29 RESOURCE_NOT_FOUND
# lines across three creator surfaces and not one of them named a handle, so
# the dead watchlist entries could not be pruned without re-probing all of
# them. The identifier key differs per surface, so the first one present in
# the params wins.


def test_vendor_error_names_the_creator_handle():
    cfg = _cfg(phases=["creators"], caps={"creators_per_platform": 1})
    cfg["socialcrawl"]["markets"]["za"]["creator_tiers"] = ["tier_1"]
    with patch(
        "src.ingestion.connectors.socialcrawl.load_market_creators",
        return_value={"watchlists": {"tiktok": {"tier_1": ["dead.handle"]}}},
    ):
        c, _ = _run(
            cfg,
            [_resp([], credits_used=0, success=False, error={"type": "RESOURCE_NOT_FOUND"})],
        )
    assert any("dead.handle" in f for f in c._fetch_failures), c._fetch_failures


def test_vendor_error_names_the_search_term():
    c, _ = _run(
        _cfg(),
        [
            _resp([], credits_used=0, success=False, error={"type": "RESOURCE_NOT_FOUND"}),
            _resp([]),
        ],
    )
    assert any("amapiano" in f for f in c._fetch_failures), c._fetch_failures


def test_vendor_error_names_the_facebook_page_id():
    c, _, _ = _run_with_session(
        _fb_cfg(pages=["100068189748310"]),
        [_resp([], credits_used=0, success=False, error={"type": "RESOURCE_NOT_FOUND"})],
    )
    assert any("100068189748310" in f for f in c._fetch_failures), c._fetch_failures


def test_vendor_error_logs_the_identifier():
    with caplog_at("connector.socialcrawl") as records:
        _run(
            _cfg(),
            [
                _resp([], credits_used=0, success=False, error={"type": "RESOURCE_NOT_FOUND"}),
                _resp([]),
            ],
        )
    assert any("amapiano" in r for r in records), records


def test_vendor_error_without_an_identifier_param_keeps_the_old_shape():
    """tiktok/trending is keyed on region alone, so there is nothing to name."""
    cfg = _cfg(phases=["discover"])
    c, _ = _run(
        cfg,
        [
            _resp([], credits_used=0, success=False, error={"type": "RESOURCE_NOT_FOUND"}),
            _resp([]),
        ],
    )
    assert "socialcrawl tiktok/trending: RESOURCE_NOT_FOUND" in c._fetch_failures


# -- news envelope ---------------------------------------------------------


def test_news_article_envelope():
    """google_news nests items under `article`, not `post`.

    The date is built relative to now, like RECENT and ANCIENT above. It used to
    be the literal probe timestamp 2026-07-23, which sat inside the 21 day
    recency window when the test was written and fell outside it on 13 Aug 2026.
    The connector then dropped the row for being old and the envelope assertion
    failed for a reason that had nothing to do with envelopes. A fixture date
    that is compared against now has to be written against now.
    """
    cfg = _cfg(phases=["news"], caps={"news_queries": 1})
    cfg["socialcrawl"]["markets"]["za"]["news_queries"] = ["South Africa youth"]
    # SocialCrawl sends news dates space-separated with an explicit offset,
    # unlike the ISO form everywhere else. That is what this asserts on.
    published = datetime.now(UTC) - timedelta(days=2)
    article = {
        "article": {
            "title": "Preparing South Africa's youth for the future of work",
            "url": "https://vocfm.co.za/story",
            "source": "vocfm.co.za",
            "domain": "vocfm.co.za",
            "snippet": "As South Africa commemorated Youth Month...",
            "published_at": published.strftime("%Y-%m-%d %H:%M:%S +00:00"),
        }
    }
    _, df = _run(cfg, [_resp([article])])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["platform"] == "news"
    assert row["author_handle"] == "vocfm.co.za"
    assert row["published_at"].startswith(published.strftime("%Y-%m-%dT%H:%M:%S"))


# -- creators --------------------------------------------------------------


def test_creator_rows_carry_the_handle_for_watchlist_matching():
    """enrichment resolves creator_watchlist_tier off author_handle, so a null
    username from the vendor must fall back to the requested handle."""
    cfg = _cfg(phases=["creators"], caps={"creators_per_platform": 1})
    cfg["socialcrawl"]["markets"]["za"]["creator_tiers"] = ["tier_1"]
    item = _item(handle="x")
    item["post"]["author"]["username"] = None
    with patch(
        "src.ingestion.connectors.socialcrawl.load_market_creators",
        return_value={"watchlists": {"tiktok": {"tier_1": ["tyla"]}}},
    ):
        _, df = _run(cfg, [_resp([item])])
    assert len(df) == 1
    assert df.iloc[0]["author_handle"] == "tyla"
    assert df.iloc[0]["query_group"] == "creator_watchlist"


def test_creator_region_is_stamped_into_v2locations():
    """tiktok/profile/region is the geo primitive EnsembleData never had."""
    cfg = _cfg(phases=["creators"], caps={"creators_per_platform": 1, "geo_verify": 1})
    cfg["socialcrawl"]["markets"]["za"]["creator_tiers"] = ["tier_1"]
    region = MagicMock()
    region.raise_for_status.return_value = None
    region.json.return_value = {
        "success": True,
        "credits_used": 1,
        "data": {"author": {"username": "tyla", "location": "ZA"}},
    }
    with patch(
        "src.ingestion.connectors.socialcrawl.load_market_creators",
        return_value={"watchlists": {"tiktok": {"tier_1": ["tyla"]}}},
    ):
        _, df = _run(cfg, [_resp([_item(handle="tyla")]), region])
    assert len(df) == 1
    assert df.iloc[0]["v2locations"] == "ZA"


def test_geo_verify_off_by_default_makes_no_call():
    cfg = _cfg(phases=["creators"], caps={"creators_per_platform": 1})
    cfg["socialcrawl"]["markets"]["za"]["creator_tiers"] = ["tier_1"]
    with patch(
        "src.ingestion.connectors.socialcrawl.load_market_creators",
        return_value={"watchlists": {"tiktok": {"tier_1": ["tyla"]}}},
    ):
        _, df = _run(cfg, [_resp([_item(handle="tyla")])])
    # A second response was never queued; a stray region call would StopIteration.
    assert len(df) == 1
    assert df.iloc[0]["v2locations"] == ""


def test_creators_config_missing_is_non_fatal():
    cfg = _cfg(phases=["creators"])
    with patch(
        "src.ingestion.connectors.socialcrawl.load_market_creators",
        side_effect=FileNotFoundError("no such file"),
    ):
        c, df = _run(cfg, [])
    assert df.empty
    assert any("creators config" in f for f in c._fetch_failures)


# -- pagination ------------------------------------------------------------
# Live-probed 27 Jul 2026. All three paged surfaces answer with the same
# contract: `data.next_cursor` (the raw upstream token) alongside an envelope
# `pagination` block carrying `next_cursor` (a vendor-wrapped `sc.` token) and
# `has_more`. Both cursor forms are accepted back on the `cursor` param, each
# page costs one credit, and page two is almost entirely fresh rows.
#
# The docs call the TikTok cursor an integer. It is an opaque string. Anything
# that coerces it to int silently re-requests page one forever.


def _paged_cfg(**caps):
    c = _cfg(caps={"search_terms": 1, "threads_terms": 0, **caps})
    return c


def test_pagination_off_by_default_makes_one_call_per_surface():
    """No pages cap in config means today's behaviour, exactly one page."""
    _, df, s = _run_with_session(
        _cfg(),
        [
            _resp([_item()], next_cursor="c1", has_more=True),
            _resp([_item(vid="y1")], next_cursor="c1", has_more=True),
        ],
    )
    assert _cursors_sent(s, "tiktok/search/top") == [None]
    assert _cursors_sent(s, "youtube/search") == [None]
    assert len(df) == 2


def test_second_page_follows_the_cursor():
    _, df, s = _run_with_session(
        _paged_cfg(tiktok_search_pages=2),
        [
            _resp([_item(vid="a")], next_cursor="cur-1", has_more=True),
            _resp([_item(vid="b")], next_cursor="cur-2", has_more=False),
            _resp([_item(vid="y1")]),
        ],
    )
    assert _cursors_sent(s, "tiktok/search/top") == [None, "cur-1"]
    tiktok_urls = set(df[df["platform"] == "tiktok"]["url"])
    assert any(u.endswith("/a") for u in tiktok_urls)
    assert any(u.endswith("/b") for u in tiktok_urls)


def test_raw_data_cursor_wins_over_the_wrapped_one():
    """The wrapped `sc.` token returns an EMPTY page on facebook/profile/posts.

    Both forms page correctly on TikTok, which is what the first cut of this
    code was written against, so preferring the wrapped one looked harmless.
    On Facebook it reads as "there is only one page" and drops every row after
    the third.
    """
    _, _, s = _run_with_session(
        _paged_cfg(tiktok_search_pages=2),
        [
            _resp(
                [_item(vid="a")], next_cursor="raw-1", wrapped_cursor="sc.wrapped-1", has_more=True
            ),
            _resp([_item(vid="b")], has_more=False),
            _resp([_item(vid="y1")]),
        ],
    )
    assert _cursors_sent(s, "tiktok/search/top") == [None, "raw-1"]


def test_wrapped_cursor_is_the_fallback_when_no_raw_one_is_sent():
    _, _, s = _run_with_session(
        _paged_cfg(tiktok_search_pages=2),
        [
            _resp([_item(vid="a")], wrapped_cursor="sc.wrapped-1", has_more=True),
            _resp([_item(vid="b")], has_more=False),
            _resp([_item(vid="y1")]),
        ],
    )
    assert _cursors_sent(s, "tiktok/search/top") == [None, "sc.wrapped-1"]


def test_paging_stops_when_has_more_is_false():
    _, _, s = _run_with_session(
        _paged_cfg(tiktok_search_pages=4),
        [
            _resp([_item(vid="a")], next_cursor="cur-1", has_more=True),
            _resp([_item(vid="b")], next_cursor="cur-2", has_more=False),
            _resp([_item(vid="y1")]),
        ],
    )
    assert _cursors_sent(s, "tiktok/search/top") == [None, "cur-1"]


def test_paging_stops_when_the_cursor_is_missing():
    _, _, s = _run_with_session(
        _paged_cfg(tiktok_search_pages=4),
        [
            _resp([_item(vid="a")]),
            _resp([_item(vid="y1")]),
        ],
    )
    assert _cursors_sent(s, "tiktok/search/top") == [None]


def test_duplicate_items_across_pages_are_dropped():
    """TikTok page two overlapped page one by three of thirty on the live probe."""
    _, df, _ = _run_with_session(
        _paged_cfg(tiktok_search_pages=2),
        [
            _resp([_item(vid="a"), _item(vid="dupe")], next_cursor="cur-1", has_more=True),
            _resp([_item(vid="dupe"), _item(vid="b")], has_more=False),
            _resp([]),
        ],
    )
    tiktok = df[df["platform"] == "tiktok"]
    assert len(tiktok) == 3
    assert len(set(tiktok["url"])) == 3


def test_paging_respects_the_credit_budget():
    """A pages cap can never outrank the run budget."""
    cfg = _paged_cfg(tiktok_search_pages=10)
    cfg["socialcrawl"]["budget_credits_per_run"] = 3
    _run_with_session(
        cfg,
        [_resp([_item(vid=f"v{i}")], next_cursor=f"c{i}", has_more=True) for i in range(20)],
    )
    assert SocialCrawlConnector.credits_used() <= 3


def test_a_failed_page_two_keeps_page_one_rows():
    _, df, _ = _run_with_session(
        _paged_cfg(tiktok_search_pages=2),
        [
            _resp([_item(vid="a")], next_cursor="cur-1", has_more=True),
            _resp([], credits_used=0, success=False, error={"type": "API_ERROR"}),
            _resp([_item(vid="y1")]),
        ],
    )
    assert len(df) == 2


def test_youtube_and_reddit_page_on_their_own_caps():
    _, _, s = _run_with_session(
        _paged_cfg(youtube_search_pages=2),
        [
            _resp([_item(vid="a")], next_cursor="tt-1", has_more=True),
            _resp([_item(vid="y1")], next_cursor="yt-1", has_more=True),
            _resp([_item(vid="y2")], has_more=False),
        ],
    )
    assert _cursors_sent(s, "tiktok/search/top") == [None]
    assert _cursors_sent(s, "youtube/search") == [None, "yt-1"]


def test_reddit_subreddit_pages_on_reddit_pages_cap():
    cfg = _cfg(phases=["reddit"], caps={"subreddits": 1, "reddit_queries": 0, "reddit_pages": 2})
    cfg["socialcrawl"]["markets"]["za"]["subreddits"] = ["southafrica"]
    _, _, s = _run_with_session(
        cfg,
        [
            _resp([_item(vid="r1")], next_cursor="t3_abc", has_more=True),
            _resp([_item(vid="r2")], has_more=False),
            # Third call is the top/week leg of the sort sweep, unpaged.
            _resp([_item(vid="r3")], has_more=False),
        ],
    )
    assert _cursors_sent(s, "reddit/subreddit") == [None, "t3_abc", None]


# -- reddit sort sweep -----------------------------------------------------
# Measured 27 Jul 2026 on r/southafrica and r/Nigeria: `rising` returns the
# same set as `hot` (a wasted credit), while top/week and a second page of hot
# are almost entirely new rows. These pin the sweep and its dedupe.


def _reddit_cfg(**caps):
    cfg = _cfg(
        phases=["reddit"],
        caps={"subreddits": 1, "reddit_queries": 0, **caps},
    )
    cfg["socialcrawl"]["markets"]["za"]["subreddits"] = ["southafrica"]
    return cfg


def _sorts_sent(session):
    out = []
    for call in session.get.call_args_list:
        if call.args and call.args[0].endswith("reddit/subreddit"):
            p = call.kwargs.get("params") or {}
            out.append((p.get("sort"), p.get("timeframe")))
    return out


def test_default_sweep_is_hot_then_top_week_and_never_rising():
    _, _, s = _run_with_session(_reddit_cfg(), [_resp([_item(vid="a")]), _resp([_item(vid="b")])])
    assert _sorts_sent(s) == [("hot", None), ("top", "week")]
    assert "rising" not in [srt for srt, _ in _sorts_sent(s)]


def test_the_same_thread_under_two_sorts_is_stored_once():
    """Sorts overlap by design; without dedupe the row count inflates."""
    _, df, _ = _run_with_session(
        _reddit_cfg(),
        [_resp([_item(vid="dupe"), _item(vid="a")]), _resp([_item(vid="dupe"), _item(vid="b")])],
    )
    assert len(df) == 3
    assert len(set(df["url"])) == 3


def test_paging_applies_to_hot_only_not_to_top_week():
    """top/week is a small fixed window whose second page repeats the first."""
    _, _, s = _run_with_session(
        _reddit_cfg(reddit_pages=2),
        [
            _resp([_item(vid="a")], next_cursor="c1", has_more=True),
            _resp([_item(vid="b")], has_more=False),
            _resp([_item(vid="c")], next_cursor="c2", has_more=True),
        ],
    )
    assert _sorts_sent(s) == [("hot", None), ("hot", None), ("top", "week")]


def test_sort_modes_are_configurable():
    _, _, s = _run_with_session(
        _reddit_cfg(reddit_sort_modes=[{"sort": "new"}]), [_resp([_item(vid="a")])]
    )
    assert _sorts_sent(s) == [("new", None)]


def test_dedupe_is_per_subreddit_not_global():
    """The same thread cross-posted to two subreddits is two legitimate rows."""
    cfg = _cfg(phases=["reddit"], caps={"subreddits": 2, "reddit_queries": 0})
    cfg["socialcrawl"]["markets"]["za"]["subreddits"] = ["southafrica", "CapeTown"]
    _, df, _ = _run_with_session(cfg, [_resp([_item(vid="x")]) for _ in range(4)])
    assert len(df) == 2
    assert set(df["query_term"]) == {"r/southafrica", "r/CapeTown"}


# -- facebook --------------------------------------------------------------
# Live-probed 27 Jul 2026. Facebook has NO keyword post search at any price, so
# this phase is a curated id list by necessity, not by choice. Pages answer
# three posts per call, groups reject the url param and need a numeric id, and
# page slugs are unsafe: two of eight probed resolved to an impostor profile.


def _fb_cfg(pages=None, groups=None, **caps):
    cfg = _cfg(
        phases=["facebook"],
        caps={"facebook_pages": 5, "facebook_groups": 5, **caps},
        phase_share=dict.fromkeys(
            ("discover", "creators", "search", "reddit", "accounts", "news", "facebook"), 1.0
        ),
    )
    cfg["socialcrawl"]["markets"]["za"]["facebook_pages"] = pages or []
    cfg["socialcrawl"]["markets"]["za"]["facebook_groups"] = groups or []
    return cfg


def _params_for(session, surface):
    out = []
    for call in session.get.call_args_list:
        if call.args and call.args[0].endswith(surface):
            out.append(call.kwargs.get("params") or {})
    return out


def test_facebook_is_off_unless_pages_are_configured():
    """No ids in config means the phase makes zero calls and costs nothing."""
    _, df, s = _run_with_session(_fb_cfg(), [])
    assert df.empty
    assert SocialCrawlConnector.credits_used() == 0
    assert s.get.call_count == 0


def test_numeric_page_is_sent_as_pageid_not_url():
    """Slugs get hijacked; the immutable id cannot resolve to another profile."""
    _, _, s = _run_with_session(_fb_cfg(pages=["100068189748310"]), [_resp([_item(vid="p1")])])
    assert _params_for(s, "facebook/profile/posts") == [{"pageId": "100068189748310"}]


def test_non_numeric_page_falls_back_to_url_for_discovery():
    _, _, s = _run_with_session(_fb_cfg(pages=["eNCAnews"]), [_resp([_item(vid="p1")])])
    assert _params_for(s, "facebook/profile/posts") == [{"url": "eNCAnews"}]


def test_group_uses_numeric_id_and_chronological_sort():
    """The url param is rejected for every URL form, and RECENT_ACTIVITY is staler."""
    _, _, s = _run_with_session(_fb_cfg(groups=["369205615472953"]), [_resp([_item(vid="g1")])])
    assert _params_for(s, "facebook/group/posts") == [
        {"group_id": "369205615472953", "sort_by": "CHRONOLOGICAL"}
    ]


def test_page_rows_carry_the_page_as_author_when_username_is_null():
    """Pages leave author.username null; without the fallback the row is unusable."""
    item = {
        "post": {
            "id": "p1",
            "url": "https://facebook.com/vanguardngr/posts/1",
            "published_at": RECENT,
            "author": {"username": None, "display_name": "Vanguard News"},
            "content": {"text": "Court orders"},
            "engagement": {"likes": 49, "comments": 5},
        }
    }
    _, df, _ = _run_with_session(_fb_cfg(pages=["vanguardngr"]), [_resp([item])])
    assert len(df) == 1
    assert df.iloc[0]["author_handle"] == "vanguardngr"
    assert df.iloc[0]["author_name"] == "Vanguard News"
    assert df.iloc[0]["platform"] == "facebook"


def test_pages_paginate_on_the_raw_cursor():
    """Three posts a call means volume here is paging, not breadth."""
    _, df, s = _run_with_session(
        _fb_cfg(pages=["eNCAnews"], facebook_page_pages=2),
        [
            _resp([_item(vid="a")], next_cursor="raw-1", wrapped_cursor="sc.dead", has_more=True),
            _resp([_item(vid="b")], has_more=False),
        ],
    )
    assert [p.get("cursor") for p in _params_for(s, "facebook/profile/posts")] == [None, "raw-1"]
    assert len(df) == 2


def test_groups_are_never_paged():
    """The vendor returns a single group page; a second call would rebuy page one."""
    _, _, s = _run_with_session(
        _fb_cfg(groups=["369205615472953"], facebook_page_pages=3),
        [_resp([_item(vid="g1")], next_cursor="c1", has_more=True)],
    )
    assert len(_params_for(s, "facebook/group/posts")) == 1


# -- dead curated ids ------------------------------------------------------
# The vendor answers `success: true, items: [], credits_used: 0` for a page or
# group that no longer exists. Downstream that is indistinguishable from a
# quiet day, which is how 15 of 30 configured subreddits stayed dead for weeks.
# A curated id is an entity we NAME, so zero rows from one is a defect report.


def test_dead_facebook_page_is_reported_not_silent():
    c, df, _ = _run_with_session(_fb_cfg(pages=["100068189748310"]), [_resp([], credits_used=0)])
    assert df.empty
    assert any("100068189748310" in f for f in c._fetch_failures), c._fetch_failures


def test_live_facebook_page_reports_nothing():
    c, df, _ = _run_with_session(_fb_cfg(pages=["100068189748310"]), [_resp([_item(vid="p1")])])
    assert len(df) == 1
    assert c._fetch_failures == []


def test_dead_facebook_group_is_reported():
    c, _, _ = _run_with_session(_fb_cfg(groups=["369205615472953"]), [_resp([], credits_used=0)])
    assert any("369205615472953" in f for f in c._fetch_failures), c._fetch_failures


def test_dead_subreddit_is_reported():
    """Zero across every sort mode is a dead community, not a quiet one."""
    cfg = _cfg(phases=["reddit"], caps={"subreddits": 1, "reddit_queries": 0})
    cfg["socialcrawl"]["markets"]["za"]["subreddits"] = ["MzansiTV"]
    c, _, _ = _run_with_session(cfg, [_resp([], credits_used=0), _resp([], credits_used=0)])
    assert any("MzansiTV" in f for f in c._fetch_failures), c._fetch_failures


def test_a_subreddit_alive_on_only_one_sort_is_not_reported():
    cfg = _cfg(phases=["reddit"], caps={"subreddits": 1, "reddit_queries": 0})
    cfg["socialcrawl"]["markets"]["za"]["subreddits"] = ["southafrica"]
    c, df, _ = _run_with_session(cfg, [_resp([]), _resp([_item(vid="r1")])])
    assert len(df) == 1
    assert c._fetch_failures == []


def test_an_errored_call_is_not_also_reported_as_empty():
    """One fault, one line. The HTTP error already says why it is empty."""
    c, _, _ = _run_with_session(
        _fb_cfg(pages=["100068189748310"]),
        [_resp([], credits_used=0, success=False, error={"type": "RESOURCE_NOT_FOUND"})],
    )
    assert len(c._fetch_failures) == 1, c._fetch_failures
    assert "RESOURCE_NOT_FOUND" in c._fetch_failures[0]
