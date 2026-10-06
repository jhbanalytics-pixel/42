"""EnsembleData API connector for TikTok and Instagram content.

Port source: trends-mvp/trends-free-mvp/src/connectors/ensemble_connector.py.
Preserves MVP extraction helpers (_safe_get, _pick_first, _extract_list_from_payload,
_extract_next_cursor, _extract_author, _extract_text, _extract_title, _extract_url,
_extract_timestamp, _extract_metrics, _normalize_posts) with minor V2 adaptation:
  * Token read via src/utils/secrets.get_secret (not os.getenv direct).
  * URLs with tokens never logged. Querystring stripped before logging.
  * HTTP 495 (EnsembleData quota-exhausted sentinel) short-circuits the fetch.
  * Per-run unit budget tracked via response.units_charged.
  * market column populated from self.market (MVP left it empty).
  * Reuses BaseConnector session (retry adapter, rate limiting).
"""

import os
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests

from src.ingestion.connectors.base import BaseConnector
from src.utils.config_loader import load_sources
from src.utils.secrets import get_secret

ENSEMBLE_QUOTA_STATUS = 495
ENSEMBLE_AUTH_STATUS = 493  # Token revoked / account suspended / billing issue


def _ensemble_enabled(sources: dict) -> bool:
    """True unless the config marks EnsembleData retired.

    Reads ``enabled`` from whichever block shape is present: the preferred
    ``ensemble`` block, else the legacy ``ensemble_budget`` block that the live
    configs/sources.yaml uses. An absent key means enabled, so a config that
    never carried the flag behaves exactly as it did before this gate existed.

    Kept module level and free of connector state so the gate is testable on its
    own and so any other caller reaches the same verdict.
    """
    for key in ("ensemble", "ensemble_budget"):
        block = sources.get(key)
        if isinstance(block, dict) and "enabled" in block:
            return bool(block["enabled"])
    return True


DEFAULT_BUDGET_UNITS_PER_RUN = (
    800  # 10K/day cap from EnsembleData until 10 May, keeps headroom for manual re-runs
)
# CREATOR_INGEST_BOOST (default off) raises the per-run budget to this so the
# creator user-endpoints fire after hashtag + keyword instead of being starved
# by the 800 cap. Real Bronze spend stays gated by the 495 quota breaker and a
# shadow run; override via ensemble_budget.budget_units_per_run_boost.
DEFAULT_BOOST_BUDGET = 1500
DEFAULT_ESTIMATED_UNITS_PER_CALL = 1

# A real Instagram media id is an ~18-19 digit numeric string. Require at
# least this many digits before accepting a query_term as a media_id, so a
# short numeric hashtag seed (e.g. KE's "254") is rejected rather than fired
# at /instagram/post/comments where it earns a guaranteed vendor 4xx.
IG_MEDIA_ID_MIN_DIGITS = 10

# A non-quota 4xx is normally swallowed per call (a single dead post or a
# transient bad id is not worth a hard failure). But the same endpoint
# returning 4xx this many times in a row signals a wrong param contract that
# yields zero rows every cron, so it is recorded in the per-run failure
# summary the pipeline_runs writer surfaces.
NON_QUOTA_4XX_FAILURE_THRESHOLD = 3
# Consecutive timeouts / connection errors across ALL endpoints and markets
# before the transport breaker trips and the run stops spending wall clock
# on a hung vendor (13/14 Jul 2026 dead night).
TRANSPORT_FAILURE_THRESHOLD = 5


def _creator_boost_on() -> bool:
    """CREATOR_INGEST_BOOST flag. When true, prioritise and widen creator
    user-endpoint ingestion: user endpoints run first, tier_2 handles load, and
    the per-run budget is raised. Default off so the cron is byte-identical.
    """
    return os.environ.get("CREATOR_INGEST_BOOST", "false").lower() == "true"


_NON_LATIN_SCRIPT_RE = re.compile(
    "["
    "\u0400-\u04ff"  # Cyrillic
    "\u0500-\u052f"  # Cyrillic Supplement
    "\u0600-\u06ff"  # Arabic
    "\u0750-\u077f"  # Arabic Supplement
    "\u08a0-\u08ff"  # Arabic Extended-A
    "\u3040-\u309f"  # Hiragana
    "\u30a0-\u30ff"  # Katakana
    "\u3400-\u4dbf"  # CJK Unified Extension A
    "\u4e00-\u9fff"  # CJK Unified
    "\uac00-\ud7af"  # Hangul (Korean)
    "\u0e00-\u0e7f"  # Thai
    "\u0590-\u05ff"  # Hebrew
    "]"
)
_NON_LATIN_DOMINANCE_THRESHOLD = 0.3
_NON_LATIN_MIN_CONTENT_CHARS = 10


def _is_mostly_non_latin(text: str) -> bool:
    """Return True when the text is dominantly written in a non-Latin script.

    "Dominantly" here means at least `_NON_LATIN_DOMINANCE_THRESHOLD` (30%)
    of the alphanumeric characters in the text come from the Unicode ranges
    of scripts not natively used in the SSA markets we monitor (Arabic,
    CJK, Cyrillic, Hebrew, Thai, Korean). Short strings under
    `_NON_LATIN_MIN_CONTENT_CHARS` are kept because there's not enough
    signal to judge. Mixed-script captions (e.g. a Swahili post that
    quotes one Arabic phrase) fall under the 30% threshold and are kept.
    """
    if not text:
        return False
    content_chars = [c for c in text if c.isalnum()]
    if len(content_chars) < _NON_LATIN_MIN_CONTENT_CHARS:
        return False
    non_latin = sum(1 for c in content_chars if _NON_LATIN_SCRIPT_RE.match(c))
    return (non_latin / len(content_chars)) >= _NON_LATIN_DOMINANCE_THRESHOLD


DEFAULT_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "tiktok_hashtag",
        "path": "/tt/hashtag/posts",
        "platform": "tiktok",
        "query_param": "name",
        "terms_key": "tiktok_hashtags",
    },
    {
        # /tt/keyword/search requires a `period` query param (days window).
        # EnsembleData accepts 0, 1, 7, 30, 90, 180; 7 matches our trend
        # cadence and keeps results fresh without inflating unit cost.
        "name": "tiktok_keyword",
        "path": "/tt/keyword/search",
        "platform": "tiktok",
        "query_param": "name",
        "terms_key": "tiktok_keywords",
        "extra_params": {"period": "7"},
    },
    {
        "name": "instagram_hashtag",
        "path": "/instagram/hashtag/posts",
        "platform": "instagram",
        "query_param": "name",
        "terms_key": "instagram_hashtags",
    },
    # sources.yaml already carries threads_keywords per market. MVP ran them at
    # this endpoint. Response shape handled by the same _normalize_posts path.
    {
        "name": "threads_keyword",
        "path": "/threads/keyword/search",
        "platform": "threads",
        "query_param": "name",
        "terms_key": "threads_keywords",
    },
]

# NOTE: the /tt/trending termless endpoint was REMOVED 4 Jun 2026. The vendor
# dropped it (HTTP 404 on live probe, absent from openapi.json); it had been
# wired live but returned zero rows every cron, the 404 swallowed by the 4xx
# handler. The termless + country_param plumbing below stays generic for the
# live termless endpoints (comments, replies, post-info).

# Per-creator content endpoints (Phase 2, 27 May 2026). EnsembleData exposes
# /tt/user/posts, /threads/user/posts, /instagram/user/posts which return a
# single creator's recent uploads. Used to monitor the 44-creator watchlist
# added 26 May (Tyla, Mihlali, Lasizwe, Korty EO, Layi Wasabi, Davido,
# Burna Boy, Asake, Stivo, Azziad, Mulamwah, etc.) directly instead of
# relying on hashtag spillover. Each call costs ~6 units; capped at tier_1
# handles to stay within Bronze 5000/day budget. Opt-in via per-market
# ``creator_endpoints_enabled`` flag.
# Vendor contract live-probed 16 Jun 2026 (the endpoints had 422'd every cron
# since they shipped, yielding zero creator rows). The 422 detail bodies gave
# the real required params:
#   /tt/user/posts        -> username + depth   (username works directly)
#   /instagram/user/posts -> user_id (integer) + depth  (needs a username->pk
#                            resolve via /instagram/user/info first)
#   /threads/user/posts   -> id (integer) + chunk_size; the id is a
#                            THREADS-NATIVE pk, distinct from the Instagram pk.
#                            Resolve username->threads pk via
#                            /threads/user/search (NOT /instagram/user/info: the
#                            IG pk loads /threads/user/info but returns no posts).
# All three fixed below after a 16-Jun live probe. TikTok takes a username
# directly; Instagram + Threads each resolve the handle to a numeric id first,
# via different endpoints (instagram/user/info vs threads/user/search).
USER_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "tiktok_user",
        "path": "/tt/user/posts",
        "platform": "tiktok",
        "query_param": "username",
        "terms_key": "tiktok_user_handles",
        "extra_params": {"depth": 1},
    },
    {
        "name": "instagram_user",
        "path": "/instagram/user/posts",
        "platform": "instagram",
        "query_param": "user_id",
        "terms_key": "instagram_user_handles",
        "extra_params": {"depth": 1},
        "resolve_handle": "instagram",
    },
    {
        "name": "threads_user",
        "path": "/threads/user/posts",
        "platform": "threads",
        "query_param": "id",
        "terms_key": "threads_user_handles",
        "extra_params": {"chunk_size": 10},
        "resolve_handle": "threads",
    },
]

# Per-platform creator tier cap. tier_1 = peak Gen Z anchors per market
# (~5-9 handles per platform). Pulling tier_2 + tier_3 (~22+ handles)
# would push EnsembleData usage past the Bronze 5000/day soft ceiling
# once velocity baselines mature. Bump via per-market
# ``creator_tier_cap: tier_2`` when budget rebaseline lands.
DEFAULT_CREATOR_TIER_CAP = "tier_1"

# X / Twitter endpoints via EnsembleData. Patched 28 May 2026 against the
# live EnsembleData openapi.json: the vendor does NOT expose
# /twitter/keyword/search or /twitter/hashtag/posts (Wave 1 ship had
# guessed paths). The real surface is /twitter/user/tweets (takes
# numeric rest_id) and /twitter/user/info (resolves username ->
# rest_id). Handle config still carries string usernames; the
# connector resolves them to rest_ids via a process-level cache before
# firing /twitter/user/tweets so each handle burns the 2-unit resolve
# fee at most once per cron run.
TWITTER_USER_INFO_PATH = "/twitter/user/info"

TWITTER_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "twitter_user",
        "path": "/twitter/user/tweets",
        "platform": "twitter",
        "query_param": "id",
        "terms_key": "twitter_handles",
        "enabled_flag": "twitter_handles_enabled",
        "resolve_handle": True,
    },
]

# Process-level cache mapping a Twitter handle (lowercase, no leading @)
# to the numeric rest_id returned by /twitter/user/info. Persists across
# markets in one cron run so the same handle (e.g. tyla appearing in both
# ZA and pan-African pools) only costs 2 units to resolve once.
_TWITTER_HANDLE_REST_ID_CACHE: dict[str, str] = {}

# Instagram username -> numeric pk resolver path + process-level cache. The
# /instagram/user/posts endpoint takes user_id (integer), not a username, so
# each watchlisted handle resolves once via /instagram/user/info per cron run.
INSTAGRAM_USER_INFO_PATH = "/instagram/user/info"
_INSTAGRAM_HANDLE_PK_CACHE: dict[str, str] = {}

# Threads username -> threads-native pk resolver path + cache. /threads/user/posts
# takes a threads pk that is DISTINCT from the Instagram pk, so the handle
# resolves via /threads/user/search (which returns the threads pk under
# data[].node.pk), not /instagram/user/info. One resolve per handle per run.
THREADS_USER_SEARCH_PATH = "/threads/user/search"
_THREADS_HANDLE_PK_CACHE: dict[str, str] = {}

_STRUCTURED_CAPTURE_PREFIX = "__sc__:"


def _encode_structured_capture(captures: list[dict[str, str]]) -> str:
    if not captures:
        return ""
    import json

    return _STRUCTURED_CAPTURE_PREFIX + json.dumps(captures, separators=(",", ":"))


def _extract_structured_capture(
    item: dict,
    platform: str,
    author_handle: str,
    *,
    query_term: str = "",
) -> list[dict[str, str]]:
    """Side-channel IDs for seed_graph (A8): music, handles, channels."""
    out: list[dict[str, str]] = []
    handle = (author_handle or "").strip().lstrip("@").lower()
    if handle and len(handle) >= 2:
        out.append({"type": "handle", "value": handle})
    plat = (platform or "").lower()
    if plat == "tiktok":
        music = item.get("music") if isinstance(item.get("music"), dict) else {}
        mid = str(music.get("id") or music.get("mid") or item.get("music_id") or "")
        title = str(music.get("title") or music.get("music_name") or "")
        if mid:
            out.append({"type": "music", "value": mid, "title": title[:80]})
    if plat == "youtube":
        cid = str(
            item.get("channelId")
            or item.get("channel_id")
            or (item.get("snippet") or {}).get("channelId")
            or query_term
            or ""
        )
        if cid and cid.startswith("UC"):
            out.append({"type": "channel", "value": cid})
    return out


# TikTok comments endpoint. Phase-2 enrichment that mirrors the YouTube
# commentThreads pattern shipped earlier: post fetch first, then rank
# the top N videos by like_count and call `/tt/post/comments` for the
# comment body text. Comments are where SSA slang lives (same gold-mine
# as YouTube). Gated default-off via per-market `tt_comments_enabled`.
# Budget: ~5 units per call * 5 videos * 3 markets = 75 units/day.
TIKTOK_COMMENTS_ENDPOINT: dict[str, Any] = {
    "name": "tiktok_comments",
    "path": "/tt/post/comments",
    "platform": "tiktok",
    "query_param": "aweme_id",
    "terms_key": None,  # populated dynamically from post results
}
DEFAULT_TT_COMMENT_VIDEOS_PER_RUN = 5


# ---------------------------------------------------------------------------
# Wave 3 dark expansion (28 May 2026)
# ---------------------------------------------------------------------------
# All endpoints below are gated default-off per-market and wired into the
# existing dispatch loop via _maybe_wire_wave3_endpoints. Each ships dark
# until a live vendor probe + Albert flag-flip. Paths confirmed from
# EnsembleData openapi.json on 28 May 2026 (Twitter ship surfaced that
# guessed paths break in production; Wave 3 starts with verified paths).

# YouTube (via EnsembleData) dark surfaces.
YOUTUBE_WAVE3_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "yt_channel_shorts",
        "path": "/youtube/channel/shorts",
        "platform": "youtube",
        "query_param": "browseId",
        "terms_key": "yt_channel_browse_ids",
        "enabled_flag": "yt_shorts_enabled",
        "extra_params": {"depth": 1},
    },
    {
        "name": "yt_channel_videos",
        "path": "/youtube/channel/videos",
        "platform": "youtube",
        "query_param": "browseId",
        "terms_key": "yt_channel_browse_ids",
        "enabled_flag": "yt_channel_videos_enabled",
        "extra_params": {"depth": 1},
    },
    {
        "name": "yt_keyword_search",
        "path": "/youtube/search",
        "platform": "youtube",
        "query_param": "keyword",
        "terms_key": "yt_keywords",
        "enabled_flag": "yt_keyword_search_enabled",
        "extra_params": {"depth": 1, "period": "overall", "sorting": "relevance"},
    },
]

# /youtube/video/comments is fetched in a Phase 2-style enrichment step
# similar to /tt/post/comments, not the simple per-term loop. Top N
# videos collected by /youtube/search (or /youtube/channel/videos) get
# ranked by views and the top N have their comments fetched.
YOUTUBE_VIDEO_COMMENTS_ENDPOINT: dict[str, Any] = {
    "name": "yt_video_comments",
    "path": "/youtube/video/comments",
    "platform": "youtube",
    "query_param": "id",
    "terms_key": None,
}
DEFAULT_YT_COMMENT_VIDEOS_PER_RUN = 5

# Instagram dark surfaces.
INSTAGRAM_WAVE3_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "ig_user_tagged_posts",
        "path": "/instagram/user/tagged-posts",
        "platform": "instagram",
        "query_param": "user_id",
        "terms_key": "ig_user_ids",
        "enabled_flag": "ig_user_tagged_posts_enabled",
        "extra_params": {"cursor": ""},
    },
    {
        "name": "ig_user_reels",
        "path": "/instagram/user/reels",
        "platform": "instagram",
        "query_param": "user_id",
        "terms_key": "ig_user_ids",
        "enabled_flag": "ig_user_reels_enabled",
        "extra_params": {"depth": 1},
    },
]

# /instagram/post/comments is fetched per-post in an enrichment step
# similar to /tt/post/comments. The terms_key is None because the post
# ids are sourced dynamically from the IG posts collected in the main
# fetch loop.
INSTAGRAM_POST_COMMENTS_ENDPOINT: dict[str, Any] = {
    "name": "ig_post_comments",
    "path": "/instagram/post/comments",
    "platform": "instagram",
    "query_param": "media_id",
    "terms_key": None,
}
DEFAULT_IG_COMMENT_POSTS_PER_RUN = 5

# Threads dark surfaces.
THREADS_WAVE3_ENDPOINTS: list[dict[str, Any]] = []
# /threads/post/replies returns the reply thread for a given post id.
# Same wrap pattern as /threads/keyword/search (node.thread.thread_items).
THREADS_POST_REPLIES_ENDPOINT: dict[str, Any] = {
    "name": "threads_post_replies",
    "path": "/threads/post/replies",
    "platform": "threads",
    "query_param": "id",
    "terms_key": None,
}
DEFAULT_THREADS_REPLIES_POSTS_PER_RUN = 5

# TikTok dark surfaces beyond the existing /tt/* set.
TIKTOK_WAVE3_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "tt_music_posts",
        "path": "/tt/music/posts",
        "platform": "tiktok",
        "query_param": "music_id",
        "terms_key": "tt_music_ids",
        "enabled_flag": "tt_music_posts_enabled",
        "extra_params": {"cursor": 0},
    },
]

# /tt/post/info takes a single TikTok URL. Used as a per-post enrichment
# step when tt_post_info_enabled is set, similar to the comments phase.
TIKTOK_POST_INFO_ENDPOINT: dict[str, Any] = {
    "name": "tt_post_info",
    "path": "/tt/post/info",
    "platform": "tiktok",
    "query_param": "url",
    "terms_key": None,
}
DEFAULT_TT_POST_INFO_POSTS_PER_RUN = 5

# /tt/post/comments-replies. Note the double-plural in the actual vendor
# path. Fetches replies to a specific top-level comment.
TIKTOK_COMMENT_REPLIES_ENDPOINT: dict[str, Any] = {
    "name": "tt_comment_replies",
    "path": "/tt/post/comments-replies",
    "platform": "tiktok",
    "query_param": "aweme_id",
    "terms_key": None,
}
DEFAULT_TT_COMMENT_REPLIES_PER_RUN = 5


class EnsembleConnector(BaseConnector):
    """Fetches TikTok and Instagram posts from EnsembleData per market."""

    SOURCE_NAME = "ensemble"
    PLATFORM = "tiktok"
    BASE_URL = "https://ensembledata.com/apis"

    # EnsembleData's documented rate limit is 60 requests per minute (1s
    # average) on Bronze tier. The BaseConnector default of 0.5s sleep
    # leaves the connector well below that ceiling, but with 40-44 calls
    # per market per day across 3 markets, total forced-sleep time was
    # ~60 seconds per run. Lowering to 0.25s saves ~30s of wall-clock per
    # run without risking 429s (cluster of 4 requests per second is still
    # comfortably under the documented 60 rpm bucket and the retry adapter
    # will catch any anomaly). Cron observation window margin analysis
    # 21 May 2026: weekday delivery margin was 1-3 min; 30s reclaim moves
    # most days into a comfortable 3-6 min margin.
    RATE_LIMIT_DELAY = 0.25

    # Budget tracking shared across all instances in this process so the
    # per-run quota (trial 50, Bronze 5000) is respected across markets.
    # First market exhausting the budget short-circuits subsequent markets.
    _global_units_spent: int = 0
    _global_quota_exhausted: bool = False
    # Reddit (which shares the EnsembleData token) charges its per-run spend
    # here, NOT into _global_units_spent. Keeping it separate means Reddit's
    # spend cannot trip ensemble's per-run budget gate (which reads
    # _global_units_spent) and starve the markets that run after the first
    # Reddit pass. The shared daily quota is still protected by the
    # _global_quota_exhausted (HTTP 495) flag, which both connectors honour.
    _global_reddit_units_spent: int = 0
    # Consecutive transport failures (timeouts / connection errors) across all
    # instances. A sustained vendor hang never returns 495/493 and never
    # advances the unit budget, so without this it costs pure wall clock
    # (the 13/14 Jul dead night: both scheduled runs hit the Cloud Run task
    # timeout). At TRANSPORT_FAILURE_THRESHOLD the quota breaker is tripped so
    # every existing short-circuit site stops the remaining EnsembleData work.
    _global_transport_failures: int = 0

    def __init__(self, market: str = ""):
        super().__init__(market=market)
        # Per-instance (per-market) spend, tracked alongside the shared global
        # ledger so one market cannot eat the whole shared budget and starve
        # the markets that run after it (the za->ng->ke ordering problem).
        self._market_units_spent = 0
        self._per_market_budget = DEFAULT_BUDGET_UNITS_PER_RUN
        # Per-run, per-endpoint consecutive non-quota 4xx counter and the
        # resulting failure summary. A wrong param contract returns 4xx every
        # call and is otherwise swallowed silently; once an endpoint crosses
        # NON_QUOTA_4XX_FAILURE_THRESHOLD in a row it lands in
        # _endpoint_failures, which the pipeline_runs writer folds into the
        # existing per-market errors list (no new BQ column).
        self._consecutive_4xx: dict[str, int] = {}
        self._endpoint_failures: list[str] = []

    @classmethod
    def reset_global_budget(cls) -> None:
        """Reset the process-wide budget ledger. Intended for tests."""
        cls._global_units_spent = 0
        cls._global_quota_exhausted = False
        cls._global_reddit_units_spent = 0
        cls._global_transport_failures = 0

    @property
    def _units_spent(self) -> int:
        return type(self)._global_units_spent

    @_units_spent.setter
    def _units_spent(self, value: int) -> None:
        old = type(self)._global_units_spent
        type(self)._global_units_spent = int(value)
        # Mirror the delta into the per-instance counter so every existing
        # ``self._units_spent += units`` call site also advances the market tally
        # with no change to those call sites.
        self._market_units_spent = getattr(self, "_market_units_spent", 0) + (int(value) - old)

    @property
    def _quota_exhausted(self) -> bool:
        return type(self)._global_quota_exhausted

    @_quota_exhausted.setter
    def _quota_exhausted(self, value: bool) -> None:
        type(self)._global_quota_exhausted = bool(value)

    def _budget_reached(self, budget: int) -> bool:
        """True when the next call would exceed this market's slice or the
        shared per-run budget.

        The per-market cap defaults to the full per-run budget, so with the
        defaults this is identical to the old ``self._units_spent + est > budget``
        check (the shared ledger is always >= a single market's spend, so the
        shared side trips first). Setting ``per_market_units`` below the budget
        gives each market a fair slice of the shared ledger.
        """
        est = DEFAULT_ESTIMATED_UNITS_PER_CALL
        if self._market_units_spent + est > self._per_market_budget:
            return True
        return self._units_spent + est > budget

    def _note_non_quota_4xx(self, endpoint_key: str, status: int, safe_url: str) -> None:
        """Track consecutive non-quota 4xx for one endpoint.

        Per-call 4xx stays swallowed (a single dead id is not a run failure).
        Once an endpoint returns 4xx NON_QUOTA_4XX_FAILURE_THRESHOLD times in
        a row it is recorded once in _endpoint_failures so the pipeline_runs
        writer can surface a wrong param contract that would otherwise produce
        zero rows every cron with nothing flagged.
        """
        count = self._consecutive_4xx.get(endpoint_key, 0) + 1
        self._consecutive_4xx[endpoint_key] = count
        if count == NON_QUOTA_4XX_FAILURE_THRESHOLD:
            self._endpoint_failures.append(
                f"{endpoint_key}: {count} consecutive HTTP {status} on {safe_url}"
            )

    def _note_endpoint_ok(self, endpoint_key: str) -> None:
        """Reset an endpoint's consecutive-4xx counter after a clean call."""
        if self._consecutive_4xx.get(endpoint_key):
            self._consecutive_4xx[endpoint_key] = 0
        self._note_transport_ok()

    def _handle_request_exception(self, safe_url: str, exc: Exception) -> None:
        """Count a transport failure (timeout / connection error) toward the
        breaker; log any other network error without counting it.

        At TRANSPORT_FAILURE_THRESHOLD consecutive failures the existing quota
        breaker is tripped so every short-circuit site stops the remaining
        EnsembleData work for the run. A hang then costs minutes, not the
        night: neither 495/493 nor the unit budget ever fires on a hang.
        """
        if not isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            self.logger.warning("EnsembleData network error for %s: %s", safe_url, str(exc)[:200])
            return
        cls = type(self)
        cls._global_transport_failures += 1
        count = cls._global_transport_failures
        self.logger.warning(
            "EnsembleData transport failure %d/%d for %s: %s",
            count,
            TRANSPORT_FAILURE_THRESHOLD,
            safe_url,
            str(exc)[:200],
        )
        if count >= TRANSPORT_FAILURE_THRESHOLD and not self._quota_exhausted:
            self.logger.error(
                "EnsembleData transport breaker tripped after %d consecutive "
                "timeouts/connection errors; skipping remaining EnsembleData "
                "calls this run",
                count,
            )
            self._endpoint_failures.append(
                f"transport: {count} consecutive timeouts/connection errors, breaker tripped"
            )
            self._quota_exhausted = True

    def _note_transport_ok(self) -> None:
        """A successful call proves the vendor is up; reset the streak."""
        type(self)._global_transport_failures = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(self, *, surfaces: frozenset[str] | None = None, **kwargs: Any) -> pd.DataFrame:
        """Fetch posts from all configured EnsembleData endpoints for this market.

        Reads the ``ensemble`` block from configs/sources.yaml for the endpoint
        list, per-call budget, and per-market query terms. Falls back to the
        legacy ``ensembledata`` + ``ensemble_budget`` structure.

        surfaces: when set, only endpoints whose ``name`` is in the set run
        (e.g. frozenset({"twitter_user"}) for a twitter-only backfill).
        """
        # The retirement gate runs BEFORE the secret lookup, because the secret
        # is not the off switch. configs/sources.yaml has carried
        # ensemble_budget.enabled: false since the vendor was cancelled on
        # 23 Jul 2026, and nothing read it on this path, so a token reaching the
        # job by any route restarted a cancelled account. Proven live on
        # 2026-08-20: the cron logged HTTP 493 against
        # ensembledata.com/apis/twitter/user/info.
        sources = load_sources()
        if not _ensemble_enabled(sources):
            self.logger.info(
                "EnsembleData disabled in config; skipping fetch for market=%s", self.market
            )
            return self.empty_dataframe()

        token = get_secret("ENSEMBLEDATA_API_TOKEN")
        if not token:
            self.logger.warning("ENSEMBLEDATA_API_TOKEN not configured; skipping ensemble fetch")
            return self.empty_dataframe()

        config = self._resolve_ensemble_config(sources)
        budget = int(config.get("budget_units_per_run", DEFAULT_BUDGET_UNITS_PER_RUN))
        # Per-market slice of the shared per-run budget. Absent (None) defaults
        # to the full budget (no extra cap), so a run is byte-identical until
        # per_market_units is set below the budget in configs/sources.yaml. An
        # explicit 0 is honoured (this market spends nothing); do not coalesce it.
        per_market = config.get("per_market_units")
        self._per_market_budget = int(per_market if per_market is not None else budget)
        endpoints = config.get("endpoints") or DEFAULT_ENDPOINTS
        terms_by_key = config.get("terms", {})

        if not terms_by_key:
            self.logger.warning("No EnsembleData query terms configured for market=%s", self.market)
            return self.empty_dataframe()

        all_rows: list[dict[str, Any]] = []
        for endpoint in endpoints:
            if surfaces is not None and endpoint.get("name") not in surfaces:
                continue
            if self._quota_exhausted:
                break
            terms_key = endpoint.get("terms_key")
            # terms_key=None marks a termless endpoint (e.g. /tt/post/comments,
            # fed dynamically). Fire once per market; do not iterate terms.
            if terms_key is None:
                if self._budget_reached(budget):
                    self.logger.info(
                        "EnsembleData budget reached (%d/%d units); stopping",
                        self._units_spent,
                        budget,
                    )
                    return self._finalise(all_rows)
                rows = self._fetch_endpoint(endpoint, term="", token=token)
                all_rows.extend(rows)
                continue
            terms = terms_by_key.get(terms_key, []) or []
            for term in terms:
                if self._quota_exhausted:
                    break
                if self._budget_reached(budget):
                    self.logger.info(
                        "EnsembleData budget reached (%d/%d units); stopping",
                        self._units_spent,
                        budget,
                    )
                    return self._finalise(all_rows)
                rows = self._fetch_endpoint(endpoint, term, token)
                all_rows.extend(rows)

        # Phase 2: TikTok comments enrichment. Mirror the YouTube
        # commentThreads pattern: after all post-fetching endpoints
        # complete, rank the TikTok rows collected above by like_count
        # DESC, take the top N, and fetch comments per video. Comments
        # are normalised as separate rows with content_type=tiktok_comment.
        if not self._quota_exhausted and terms_by_key.get("tt_comments_enabled"):
            # None check, not `or`: an explicit 0 in YAML is an opt-out and
            # must NOT coalesce back to the default per-run cap.
            _v = terms_by_key.get("tt_comment_videos_per_run")
            top_n = DEFAULT_TT_COMMENT_VIDEOS_PER_RUN if _v is None else int(_v)
            comment_rows = self._fetch_tiktok_comments(
                posts=all_rows,
                top_n=top_n,
                budget=budget,
                token=token,
            )
            all_rows.extend(comment_rows)

        # Wave 3 enrichment endpoints. Each takes IDs/URLs from the rows
        # already collected above and fires a per-item enrichment call.
        # Each gated default-off via its own per-market flag. All write
        # additional child rows (comments / replies / detail). Budget +
        # quota checks live inside each method so a tripped breaker
        # short-circuits the rest of the chain.
        if not self._quota_exhausted and terms_by_key.get("yt_video_comments_enabled"):
            _v = terms_by_key.get("yt_video_comments_per_run")
            top_n = DEFAULT_YT_COMMENT_VIDEOS_PER_RUN if _v is None else int(_v)
            all_rows.extend(
                self._fetch_youtube_video_comments_enrichment(
                    posts=all_rows, top_n=top_n, budget=budget, token=token
                )
            )

        if not self._quota_exhausted and terms_by_key.get("ig_post_comments_enabled"):
            _v = terms_by_key.get("ig_post_comments_per_run")
            top_n = DEFAULT_IG_COMMENT_POSTS_PER_RUN if _v is None else int(_v)
            all_rows.extend(
                self._fetch_instagram_post_comments_enrichment(
                    posts=all_rows, top_n=top_n, budget=budget, token=token
                )
            )

        if not self._quota_exhausted and terms_by_key.get("threads_post_replies_enabled"):
            _v = terms_by_key.get("threads_post_replies_per_run")
            top_n = DEFAULT_THREADS_REPLIES_POSTS_PER_RUN if _v is None else int(_v)
            all_rows.extend(
                self._fetch_threads_post_replies_enrichment(
                    posts=all_rows, top_n=top_n, budget=budget, token=token
                )
            )

        if not self._quota_exhausted and terms_by_key.get("tt_post_info_enabled"):
            _v = terms_by_key.get("tt_post_info_per_run")
            top_n = DEFAULT_TT_POST_INFO_POSTS_PER_RUN if _v is None else int(_v)
            if top_n > 0:
                all_rows.extend(
                    self._fetch_tiktok_post_info_enrichment(
                        posts=all_rows, top_n=top_n, budget=budget, token=token
                    )
                )

        # Cross-flag dependency: this enriches the tiktok_comment rows that the
        # tt_comments_enabled block above collected into all_rows. With
        # tt_comments_enabled false there are no comments to reply-fetch, so this
        # silently no-ops no matter what tt_post_comment_replies_enabled is set to.
        if not self._quota_exhausted and terms_by_key.get("tt_post_comment_replies_enabled"):
            _v = terms_by_key.get("tt_post_comment_replies_per_run")
            top_n = DEFAULT_TT_COMMENT_REPLIES_PER_RUN if _v is None else int(_v)
            all_rows.extend(
                self._fetch_tiktok_comment_replies_enrichment(
                    comments=all_rows, top_n=top_n, budget=budget, token=token
                )
            )

        return self._finalise(all_rows)

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------

    def _resolve_ensemble_config(self, sources: dict) -> dict:
        """Return a unified config dict: endpoints, budget, per-market terms.

        Preferred shape (spec):
            ensemble:
              budget_units_per_run: 100
              endpoints:
                - name: tiktok_hashtag
                  path: /tt/hashtag/posts
                  ...
              markets:
                za:
                  tiktok_hashtags: [...]

        Fallback shape (existing sources.yaml):
            ensemble_budget: {...}
            ensembledata:
              za: {tiktok_hashtags: [...], tiktok_keywords: [...], ...}
        """
        if "ensemble" in sources and isinstance(sources["ensemble"], dict):
            block = sources["ensemble"]
            markets = block.get("markets", {}) or {}
            terms = dict(markets.get(self.market, {}) or {})
            endpoints = list(block.get("endpoints") or DEFAULT_ENDPOINTS)
            self._maybe_wire_creator_endpoints(terms, endpoints)
            self._maybe_wire_twitter_endpoints(terms, endpoints)
            self._maybe_wire_wave3_endpoints(terms, endpoints)
            return {
                "budget_units_per_run": block.get(
                    "budget_units_per_run", DEFAULT_BUDGET_UNITS_PER_RUN
                ),
                "per_market_units": block.get("per_market_units"),
                "endpoints": endpoints,
                "terms": terms,
            }

        legacy_terms = dict((sources.get("ensembledata", {}) or {}).get(self.market, {}) or {})
        budget_cfg = sources.get("ensemble_budget", {}) or {}
        endpoints = list(DEFAULT_ENDPOINTS)
        self._maybe_wire_creator_endpoints(legacy_terms, endpoints)
        self._maybe_wire_twitter_endpoints(legacy_terms, endpoints)
        self._maybe_wire_wave3_endpoints(legacy_terms, endpoints)
        if _creator_boost_on():
            # Boost raises the run budget to at least the boost default, but an
            # operator value that is already higher wins (the live config raised
            # budget_units_per_run to 3000 for real volume). An explicit
            # budget_units_per_run_boost still overrides when set.
            operator_budget = int(
                budget_cfg.get("budget_units_per_run", DEFAULT_BUDGET_UNITS_PER_RUN)
            )
            base_budget = int(
                budget_cfg.get(
                    "budget_units_per_run_boost",
                    max(operator_budget, DEFAULT_BOOST_BUDGET),
                )
            )
            # Keep the operator per-market sub-cap live under boost so one market
            # cannot spend the whole shared budget and starve the others. Falls
            # back to per_market_units_boost when set, else the non-boost
            # per_market_units, else the full budget (the pre-fix no-sub-cap).
            per_market = budget_cfg.get(
                "per_market_units_boost",
                budget_cfg.get("per_market_units", base_budget),
            )
        else:
            base_budget = int(budget_cfg.get("budget_units_per_run", DEFAULT_BUDGET_UNITS_PER_RUN))
            per_market = budget_cfg.get("per_market_units")
        return {
            "budget_units_per_run": base_budget,
            "per_market_units": per_market,
            "endpoints": endpoints,
            "terms": legacy_terms,
        }

    def _maybe_wire_twitter_endpoints(
        self,
        terms: dict[str, Any],
        endpoints: list[dict[str, Any]],
    ) -> None:
        """Prepend TWITTER_ENDPOINTS when the market flag is on.

        Post 28 May patch only `twitter_handles_enabled` is supported.
        The legacy `twitter_keywords_enabled` and `twitter_hashtags_enabled`
        flags are dropped: EnsembleData does not expose those paths so
        keeping them wired would burn 4xx responses every run.

        Prepended (not appended) so creator + hashtag spend cannot exhaust
        the shared boost ledger before /twitter/user/tweets runs. ZA flipped
        2 Jul 2026 but landed zero Ensemble twitter rows until this ordering
        fix because USER + DEFAULT endpoints consumed the 1500-unit slice first.
        """
        wired: list[dict[str, Any]] = []
        for endpoint in TWITTER_ENDPOINTS:
            flag = endpoint.get("enabled_flag")
            if flag and terms.get(flag):
                wired.append(endpoint)
        if wired:
            endpoints[:0] = wired

    def _maybe_wire_wave3_endpoints(
        self,
        terms: dict[str, Any],
        endpoints: list[dict[str, Any]],
    ) -> None:
        """Append Wave 3 dark surfaces per the per-market flag set.

        Each endpoint in YOUTUBE_WAVE3_ENDPOINTS, INSTAGRAM_WAVE3_ENDPOINTS,
        THREADS_WAVE3_ENDPOINTS, TIKTOK_WAVE3_ENDPOINTS carries its own
        ``enabled_flag``. The flag drives whether the endpoint is appended;
        the per-call dispatch loop in fetch() then iterates the configured
        terms for that endpoint's ``terms_key``.
        """
        for endpoint in (
            *YOUTUBE_WAVE3_ENDPOINTS,
            *INSTAGRAM_WAVE3_ENDPOINTS,
            *THREADS_WAVE3_ENDPOINTS,
            *TIKTOK_WAVE3_ENDPOINTS,
        ):
            flag = endpoint.get("enabled_flag")
            if flag and terms.get(flag):
                endpoints.append(endpoint)

    def _maybe_wire_creator_endpoints(
        self,
        terms: dict[str, Any],
        endpoints: list[dict[str, Any]],
    ) -> None:
        """Append USER_ENDPOINTS and load watchlist handles when enabled.

        Reads ``creator_endpoints_enabled`` from the per-market terms dict.
        When true, loads tier_1 handles (or whatever tier_cap is configured)
        from configs/creators/<market>.yaml for each platform and injects
        them into ``terms`` under the keys the USER_ENDPOINTS expect
        (tiktok_user_handles, instagram_user_handles, threads_user_handles).
        Mutates both arguments in place; safe because the caller passes
        copies built from the loaded sources.yaml block.
        """
        if not terms.get("creator_endpoints_enabled"):
            return
        boost = _creator_boost_on()
        if boost:
            # Default to tier_1 (the peak anchors, ~66 calls/run) for a safe first
            # flip that stays well under the Bronze cap. Set creator_tier_cap_boost:
            # tier_2 per market to widen to the seeded 200+ once a shadow confirms
            # the real per-call cost leaves budget headroom.
            tier_cap = terms.get("creator_tier_cap_boost") or "tier_1"
        else:
            tier_cap = terms.get("creator_tier_cap") or DEFAULT_CREATOR_TIER_CAP
        try:
            handles_by_platform = self._load_creator_handles(self.market, tier_cap)
        except FileNotFoundError:
            self.logger.warning(
                "Creator watchlist file missing for market=%s; skipping user endpoints",
                self.market,
            )
            return
        if boost:
            # Run creator endpoints FIRST so watchlisted creators claim budget
            # before hashtag + keyword spend it (the starvation that held
            # watchlist_score at 0). Default path keeps them appended last.
            endpoints[:0] = USER_ENDPOINTS
        else:
            endpoints.extend(USER_ENDPOINTS)
        # USER_ENDPOINTS expect handles under these terms_key names.
        terms.setdefault("tiktok_user_handles", handles_by_platform.get("tiktok", []))
        terms.setdefault("instagram_user_handles", handles_by_platform.get("instagram", []))
        terms.setdefault("threads_user_handles", handles_by_platform.get("threads", []))

    @staticmethod
    def _load_creator_handles(market: str, tier_cap: str) -> dict[str, list[str]]:
        """Read configs/creators/<market>.yaml and return handles per platform.

        tier_cap: highest tier to include ("tier_1", "tier_2", or "tier_3").
        tier_1 covers peak Gen Z anchors only; tier_2 adds secondary roster.
        Returns {"tiktok": [...], "instagram": [...], "threads": [...]}.
        Deduped per platform.
        """
        import pathlib

        import yaml

        # configs/creators sits two directories above src/ingestion/connectors
        # (the connector file). Resolve from the connector path so the lookup
        # works whether the pipeline runs from the repo root or a worktree.
        creators_dir = pathlib.Path(__file__).resolve().parents[3] / "configs" / "creators"
        creators_path = creators_dir / f"{market.lower()}.yaml"
        if not creators_path.exists():
            raise FileNotFoundError(str(creators_path))
        data = yaml.safe_load(creators_path.read_text(encoding="utf-8")) or {}
        watchlists = (data.get("watchlists") or {}) if isinstance(data, dict) else {}

        tier_order = ["tier_1", "tier_2", "tier_3"]
        cap_idx = tier_order.index(tier_cap) if tier_cap in tier_order else 0
        included_tiers = tier_order[: cap_idx + 1]

        out: dict[str, list[str]] = {}
        for platform in ("tiktok", "instagram", "threads"):
            platform_block = watchlists.get(platform, {}) or {}
            handles: list[str] = []
            seen: set[str] = set()
            for tier in included_tiers:
                for handle in platform_block.get(tier, []) or []:
                    h = str(handle).strip().lstrip("@")
                    if h and h.lower() not in seen:
                        seen.add(h.lower())
                        handles.append(h)
            out[platform] = handles
        return out

    # ------------------------------------------------------------------
    # Single endpoint call
    # ------------------------------------------------------------------

    def _fetch_endpoint(self, endpoint: dict, term: str, token: str) -> list[dict[str, Any]]:
        """Hit one endpoint for one term. Returns normalised rows (may be empty).

        When ``term`` is empty the query_param is omitted (termless endpoints
        whose ids are fed dynamically). A configured ``country_param`` wires
        the market code through when an endpoint declares one.
        """
        path = endpoint["path"]
        platform = endpoint.get("platform", self.PLATFORM)
        query_param = endpoint.get("query_param", "name")
        query_group = endpoint.get("name", path.strip("/").replace("/", "_"))

        # Twitter handles ship as string usernames in sources.yaml but the
        # vendor's /twitter/user/tweets only accepts a numeric rest_id.
        # Resolve via /twitter/user/info before the main call so the term
        # carried into the request is the rest_id, not the handle.
        resolver = endpoint.get("resolve_handle")
        if term and resolver:
            if resolver == "instagram":
                resolved = self._resolve_instagram_handle_to_id(term, token)
            elif resolver == "threads":
                resolved = self._resolve_threads_handle_to_id(term, token)
            else:  # True or "twitter" -> the original Twitter rest_id resolve
                resolved = self._resolve_twitter_handle_to_id(term, token)
            if not resolved:
                return []
            term = resolved

        url = f"{self.BASE_URL}{path}"
        params: dict[str, Any] = {"token": token}
        if term:
            params[query_param] = term
        country_param = endpoint.get("country_param")
        if country_param and self.market:
            params[country_param] = self.market.lower()
        extra = endpoint.get("extra_params") or {}
        if extra:
            params.update(extra)
        safe_url = self._safe_url(url)

        self.logger.info("EnsembleData request: %s term=%r", safe_url, term)

        # Route through BaseConnector._request so the shared retry adapter and
        # rate-limit delay apply. _request calls raise_for_status() and raises
        # HTTPError on any 4xx or 5xx, so we must catch it and inspect the
        # status code for the 495 quota-exhausted sentinel.
        try:
            resp = self._request("GET", url, params=params)
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == ENSEMBLE_QUOTA_STATUS:
                self.logger.error("EnsembleData quota exhausted (HTTP 495) on %s", safe_url)
                self._quota_exhausted = True
                return []
            if status == ENSEMBLE_AUTH_STATUS:
                # 493 is auth / billing suspension per EnsembleData docs, not
                # a daily cap. Flip the circuit breaker and flag so operator
                # can rotate token or check account status.
                self.logger.error(
                    "EnsembleData auth/billing error (HTTP 493) on %s. "
                    "Rotate ENSEMBLEDATA_API_TOKEN or check account billing.",
                    safe_url,
                )
                self._quota_exhausted = True
                return []
            if status is not None and 400 <= status < 500:
                # 429 lands here only after the retry adapter has exhausted its
                # backoff budget (429 is in BaseConnector.RETRY_STATUS_CODES).
                self.logger.warning(
                    "EnsembleData HTTP %d for %s; skipping",
                    status,
                    safe_url,
                )
                self._note_non_quota_4xx(query_group, status, safe_url)
                return []
            self.logger.warning("EnsembleData HTTPError for %s: %s", safe_url, str(exc)[:200])
            return []
        except requests.exceptions.RequestException as exc:
            self._handle_request_exception(safe_url, exc)
            return []

        try:
            payload = resp.json()
        except ValueError:
            self.logger.warning("EnsembleData non-JSON response from %s", safe_url)
            return []

        self._note_endpoint_ok(query_group)
        self._units_spent += int(self._extract_units_charged(payload))
        self._note_transport_ok()

        items = self._extract_list_from_payload(payload)
        return self._normalise_posts(
            items,
            platform=platform,
            query_group=query_group,
            query_term=term,
        )

    # ------------------------------------------------------------------
    # TikTok comments phase (Phase 2 enrichment)
    # ------------------------------------------------------------------

    def _fetch_tiktok_comments(
        self,
        posts: list[dict[str, Any]],
        top_n: int,
        budget: int,
        token: str,
    ) -> list[dict[str, Any]]:
        """Rank collected TikTok post rows by likes, fetch comments for top N.

        Mirrors the YouTube commentThreads pattern. Pulls comments from
        EnsembleData's `/tt/post/comments` endpoint for the highest-engagement
        TikTok videos collected in the first fetch phase. Each comment lands
        as a separate row with content_type=tiktok_comment and parent_id
        set to the source video id so downstream enrichment can stitch
        them back to the parent post if needed.

        Returns [] on quota exhaustion or budget cap. Empty result also fine
        when no TikTok posts were collected in the prior fetch phase.
        """
        if not posts:
            return []
        # Only consider TikTok rows; ignore Instagram + Threads + Twitter.
        tiktok_posts = [p for p in posts if p.get("platform") == "tiktok" and p.get("url")]
        if not tiktok_posts:
            return []
        # Rank by likes DESC; the comments API needs the tiktok video id,
        # which we extract from the canonical URL we built earlier.
        ranked = sorted(
            tiktok_posts,
            key=lambda r: float(r.get("likes") or 0),
            reverse=True,
        )
        rows: list[dict[str, Any]] = []
        videos_called = 0
        # The same video can be collected via two hashtags/keywords, so dedup
        # on video_id to avoid double-charging units and emitting duplicate
        # child rows for one item.
        seen_video_ids: set[str] = set()
        for post in ranked:
            if videos_called >= top_n:
                break
            if self._quota_exhausted:
                break
            if self._budget_reached(budget):
                self.logger.info(
                    "EnsembleData budget reached during TikTok comments (%d/%d units); stopping",
                    self._units_spent,
                    budget,
                )
                break
            video_id = self._extract_tiktok_video_id(post.get("url", ""))
            if not video_id:
                continue
            if video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            comment_rows = self._fetch_one_comments_call(
                endpoint=TIKTOK_COMMENTS_ENDPOINT,
                video_id=video_id,
                token=token,
            )
            rows.extend(comment_rows)
            videos_called += 1
        return rows

    @staticmethod
    def _extract_tiktok_video_id(url: str) -> str:
        """Pull the numeric video id from a TikTok canonical URL.

        Expected shape: https://www.tiktok.com/@<user>/video/<id>
        Returns "" on any other shape so caller can skip.
        """
        if not url:
            return ""
        marker = "/video/"
        idx = url.find(marker)
        if idx == -1:
            return ""
        tail = url[idx + len(marker) :]
        # Cut at next non-digit so trailing query strings or path segments
        # do not poison the id.
        out_chars: list[str] = []
        for ch in tail:
            if ch.isdigit():
                out_chars.append(ch)
            else:
                break
        return "".join(out_chars)

    def _fetch_one_comments_call(
        self,
        endpoint: dict,
        video_id: str,
        token: str,
    ) -> list[dict[str, Any]]:
        """Single call to /tt/post/comments. Returns normalised comment rows."""
        path = endpoint["path"]
        query_param = endpoint.get("query_param", "aweme_id")
        query_group = endpoint.get("name", "tiktok_comments")

        url = f"{self.BASE_URL}{path}"
        params: dict[str, Any] = {"token": token, query_param: video_id}
        safe_url = self._safe_url(url)
        self.logger.info("EnsembleData request: %s aweme_id=%s", safe_url, video_id)

        try:
            resp = self._request("GET", url, params=params)
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == ENSEMBLE_QUOTA_STATUS:
                self.logger.error("EnsembleData quota exhausted (HTTP 495) on %s", safe_url)
                self._quota_exhausted = True
                return []
            if status == ENSEMBLE_AUTH_STATUS:
                self.logger.error(
                    "EnsembleData auth/billing error (HTTP 493) on %s. "
                    "Rotate ENSEMBLEDATA_API_TOKEN or check account billing.",
                    safe_url,
                )
                self._quota_exhausted = True
                return []
            if status is not None and 400 <= status < 500:
                self.logger.warning(
                    "EnsembleData HTTP %d for %s; skipping",
                    status,
                    safe_url,
                )
                self._note_non_quota_4xx(query_group, status, safe_url)
                return []
            self.logger.warning("EnsembleData HTTPError for %s: %s", safe_url, str(exc)[:200])
            return []
        except requests.exceptions.RequestException as exc:
            self._handle_request_exception(safe_url, exc)
            return []

        try:
            payload = resp.json()
        except ValueError:
            self.logger.warning("EnsembleData non-JSON response from %s", safe_url)
            return []

        self._note_endpoint_ok(query_group)
        self._units_spent += int(self._extract_units_charged(payload))
        self._note_transport_ok()
        items = self._extract_list_from_payload(payload)
        return self._normalise_tiktok_comments(
            items,
            query_group=query_group,
            parent_video_id=video_id,
        )

    def _normalise_tiktok_comments(
        self,
        items: list,
        query_group: str,
        parent_video_id: str,
    ) -> list[dict[str, Any]]:
        """Build RAW rows for TikTok comments. parent_id is the source video id."""
        rows: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return rows
        for item in items:
            if not isinstance(item, dict):
                continue
            author_name, author_handle = self._extract_author(item)
            text = str(self._extract_text(item) or "")
            # Empty comment bodies happen (vendor returns deleted / hidden
            # comments without text); drop them rather than emit blank rows.
            if not text.strip():
                continue
            published_at = self._extract_timestamp(item)
            _, likes, _, _ = self._extract_metrics(item)
            # Vendor comment id is `cid` (confirmed against the EnsembleData
            # openapi for /tt/post/comments + /tt/post/comments-replies).
            # Required so the comment-replies enrichment can resolve the
            # (aweme_id, comment_id) pair; without it every reply is dropped.
            # aweme_id deliberately excluded from the candidate list (the
            # comment object also carries it as the PARENT video id).
            comment_id = str(self._pick_first(item, ["cid", "comment_id", "id", "pk"], "") or "")
            rows.append(
                {
                    "source": "EnsembleData",
                    "platform": "tiktok",
                    "market": self.market,
                    "content_type": "tiktok_comment",
                    "query_group": query_group,
                    "query_term": parent_video_id,
                    "comment_id": comment_id,
                    "author_name": author_name or "",
                    "author_handle": author_handle or "",
                    "title": text[:100],
                    "text": text,
                    "url": "",
                    "published_at": published_at,
                    "views": 0.0,
                    "likes": likes,
                    "comments": 0.0,
                    "shares": 0.0,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Wave 3 enrichment endpoints (28 May 2026)
    # ------------------------------------------------------------------
    # All 5 share the same per-item HTTP+quota pattern as the existing
    # _fetch_one_comments_call. The common request shell lives in
    # _enrichment_request below; each per-endpoint method extracts the
    # relevant ID/URL from already-collected rows, fires the request, and
    # normalises the response into RAW-schema rows.

    def _enrichment_request(
        self,
        path: str,
        params: dict[str, Any],
        token: str,
    ) -> dict | list | None:
        """Single GET against an enrichment endpoint. Token-redacted log.

        Returns the decoded JSON payload on success. Returns None on any
        4xx, 5xx, network error, or non-JSON body so callers treat it as
        empty. Handles 495 (quota) and 493 (auth) by flipping the global
        circuit breaker.
        """
        url = f"{self.BASE_URL}{path}"
        full_params: dict[str, Any] = {"token": token, **params}
        safe_url = self._safe_url(url)
        safe_params = {k: v for k, v in params.items() if k != "token"}
        self.logger.info("EnsembleData request: %s params=%s", safe_url, safe_params)
        try:
            resp = self._request("GET", url, params=full_params)
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == ENSEMBLE_QUOTA_STATUS:
                self.logger.error("EnsembleData quota exhausted (HTTP 495) on %s", safe_url)
                self._quota_exhausted = True
                return None
            if status == ENSEMBLE_AUTH_STATUS:
                self.logger.error(
                    "EnsembleData auth/billing error (HTTP 493) on %s. "
                    "Rotate ENSEMBLEDATA_API_TOKEN or check account billing.",
                    safe_url,
                )
                self._quota_exhausted = True
                return None
            if status is not None and 400 <= status < 500:
                self.logger.warning("EnsembleData HTTP %d for %s; skipping", status, safe_url)
                self._note_non_quota_4xx(path, status, safe_url)
                return None
            self.logger.warning("EnsembleData HTTPError for %s: %s", safe_url, str(exc)[:200])
            return None
        except requests.exceptions.RequestException as exc:
            self._handle_request_exception(safe_url, exc)
            return None
        try:
            payload = resp.json()
        except ValueError:
            self.logger.warning("EnsembleData non-JSON response from %s", safe_url)
            return None
        self._note_endpoint_ok(path)
        self._units_spent += int(self._extract_units_charged(payload))
        self._note_transport_ok()
        return payload

    # --- YouTube video comments ---------------------------------------

    def _fetch_youtube_video_comments_enrichment(
        self,
        posts: list[dict[str, Any]],
        top_n: int,
        budget: int,
        token: str,
    ) -> list[dict[str, Any]]:
        """Rank collected YouTube post rows by views, fetch comments for top N.

        Sources video ids from rows produced by Wave 3 youtube endpoints
        (yt_channel_shorts, yt_channel_videos, yt_keyword_search). Each
        call costs 2 units; 1 page per video.
        """
        if not posts:
            return []
        youtube_posts = [p for p in posts if p.get("platform") == "youtube"]
        if not youtube_posts:
            return []
        ranked = sorted(youtube_posts, key=lambda r: float(r.get("views") or 0), reverse=True)
        rows: list[dict[str, Any]] = []
        called = 0
        # Dedup on video_id: the same video can arrive via two keyword/channel
        # endpoints, so skip ids already fetched to avoid double-charging units
        # and emitting duplicate child rows.
        seen_video_ids: set[str] = set()
        for post in ranked:
            if called >= top_n:
                break
            if self._quota_exhausted:
                break
            if self._budget_reached(budget):
                self.logger.info(
                    "EnsembleData budget reached during YT video comments (%d/%d units); stopping",
                    self._units_spent,
                    budget,
                )
                break
            video_id = self._extract_youtube_video_id(post.get("url", ""))
            if not video_id:
                continue
            if video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            payload = self._enrichment_request(
                path=YOUTUBE_VIDEO_COMMENTS_ENDPOINT["path"],
                params={"id": video_id, "cursor": ""},
                token=token,
            )
            called += 1
            if payload is None:
                continue
            items = self._extract_youtube_comment_items(payload)
            rows.extend(
                self._normalise_simple_comments(
                    items,
                    platform="youtube",
                    content_type="youtube_video_comment",
                    query_group=YOUTUBE_VIDEO_COMMENTS_ENDPOINT["name"],
                    parent_id=video_id,
                    parent_url=post.get("url", ""),
                )
            )
        return rows

    @staticmethod
    def _extract_youtube_video_id(url: str) -> str:
        """Pull the YouTube video id from a watch / shorts URL."""
        if not url:
            return ""
        for marker in ("watch?v=", "/shorts/", "youtu.be/"):
            idx = url.find(marker)
            if idx != -1:
                tail = url[idx + len(marker) :]
                out: list[str] = []
                for ch in tail:
                    if ch.isalnum() or ch in ("-", "_"):
                        out.append(ch)
                    else:
                        break
                return "".join(out)
        return ""

    @staticmethod
    def _extract_youtube_comment_items(payload: Any) -> list:
        """Drill into the YouTube comments envelope.

        Documented shape:
        ``{"data": {"info": {"reloadContinuationItemsCommand":
        {"continuationItems": [...]}}, "nextCursor": "..."}}``. Falls back
        to the generic _extract_list_from_payload walker so a vendor
        response shape change does not zero out the row count silently.
        """
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                info = data.get("info")
                if isinstance(info, dict):
                    cmd = info.get("reloadContinuationItemsCommand")
                    if isinstance(cmd, dict):
                        items = cmd.get("continuationItems")
                        if isinstance(items, list) and items:
                            return items
        return EnsembleConnector._extract_list_from_payload(payload)

    # --- Instagram post comments --------------------------------------

    def _fetch_instagram_post_comments_enrichment(
        self,
        posts: list[dict[str, Any]],
        top_n: int,
        budget: int,
        token: str,
    ) -> list[dict[str, Any]]:
        """Rank collected IG post rows by likes, fetch comments for top N.

        /instagram/post/comments requires media_id + cursor + sorting.
        2 units per call. Sorting=popular returns ~15 highest-engagement
        comments without pagination (1 call covers it).
        """
        if not posts:
            return []
        ig_posts = [
            p for p in posts if p.get("platform") == "instagram" and p.get("content_type") == "post"
        ]
        if not ig_posts:
            return []
        ranked = sorted(ig_posts, key=lambda r: float(r.get("likes") or 0), reverse=True)
        rows: list[dict[str, Any]] = []
        called = 0
        # Dedup on media_id: the same post can arrive via two hashtags, so skip
        # ids already fetched to avoid double-charging units and emitting
        # duplicate child rows.
        seen_media_ids: set[str] = set()
        for post in ranked:
            if called >= top_n:
                break
            if self._quota_exhausted:
                break
            if self._budget_reached(budget):
                self.logger.info(
                    "EnsembleData budget reached during IG post comments (%d/%d units); stopping",
                    self._units_spent,
                    budget,
                )
                break
            media_id = self._extract_instagram_media_id(post)
            if not media_id:
                continue
            if media_id in seen_media_ids:
                continue
            seen_media_ids.add(media_id)
            payload = self._enrichment_request(
                path=INSTAGRAM_POST_COMMENTS_ENDPOINT["path"],
                params={"media_id": media_id, "cursor": "", "sorting": "popular"},
                token=token,
            )
            called += 1
            if payload is None:
                continue
            items = self._extract_instagram_comment_items(payload)
            rows.extend(
                self._normalise_simple_comments(
                    items,
                    platform="instagram",
                    content_type="instagram_post_comment",
                    query_group=INSTAGRAM_POST_COMMENTS_ENDPOINT["name"],
                    parent_id=media_id,
                    parent_url=post.get("url", ""),
                )
            )
        return rows

    @staticmethod
    def _extract_instagram_media_id(post: dict[str, Any]) -> str:
        """Recover a usable media_id for /instagram/post/comments.

        The vendor now validates ``media_id`` as an integer. Shortcodes from
        ``instagram.com/p/<code>/`` return HTTP 422, so only numeric ids are
        safe to fire.
        """
        native = str(post.get("native_id") or "").strip()
        if native.isdigit() and len(native) >= IG_MEDIA_ID_MIN_DIGITS:
            return native

        qt_raw = post.get("query_term")
        qt = str(qt_raw).strip() if qt_raw else ""
        if qt.isdigit() and len(qt) >= IG_MEDIA_ID_MIN_DIGITS:
            return qt

        return ""

    @staticmethod
    def _extract_instagram_comment_items(payload: Any) -> list:
        """Unwrap ``{"data": {"comments": [{"node": {...}}, ...]}}``."""
        out: list = []
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                comments = data.get("comments")
                if isinstance(comments, list):
                    for entry in comments:
                        if isinstance(entry, dict):
                            node = entry.get("node")
                            out.append(node if isinstance(node, dict) and node else entry)
                    if out:
                        return out
        return EnsembleConnector._extract_list_from_payload(payload)

    # --- Threads post replies -----------------------------------------

    def _fetch_threads_post_replies_enrichment(
        self,
        posts: list[dict[str, Any]],
        top_n: int,
        budget: int,
        token: str,
    ) -> list[dict[str, Any]]:
        """Fetch reply trees for top Threads posts by likes.

        /threads/post/replies wraps replies in the same node.thread.thread_items
        envelope as /threads/keyword/search; _unwrap_threads_post handles it.
        Cost is variable (~#replies, capped ~30 by vendor).
        """
        if not posts:
            return []
        threads_posts = [
            p for p in posts if p.get("platform") == "threads" and p.get("content_type") == "post"
        ]
        if not threads_posts:
            return []
        ranked = sorted(threads_posts, key=lambda r: float(r.get("likes") or 0), reverse=True)
        rows: list[dict[str, Any]] = []
        called = 0
        # Dedup on post_id: the same post can arrive via two keyword searches,
        # so skip ids already fetched to avoid double-charging units and
        # emitting duplicate child rows.
        seen_post_ids: set[str] = set()
        for post in ranked:
            if called >= top_n:
                break
            if self._quota_exhausted:
                break
            if self._budget_reached(budget):
                self.logger.info(
                    "EnsembleData budget reached during Threads replies (%d/%d units); stopping",
                    self._units_spent,
                    budget,
                )
                break
            post_id = self._extract_threads_post_id(post)
            if not post_id:
                continue
            if post_id in seen_post_ids:
                continue
            seen_post_ids.add(post_id)
            payload = self._enrichment_request(
                path=THREADS_POST_REPLIES_ENDPOINT["path"],
                params={"id": post_id},
                token=token,
            )
            called += 1
            if payload is None:
                continue
            items = self._extract_list_from_payload(payload)
            rows.extend(
                self._normalise_threads_replies(
                    items,
                    query_group=THREADS_POST_REPLIES_ENDPOINT["name"],
                    parent_id=post_id,
                    parent_url=post.get("url", ""),
                )
            )
        return rows

    @staticmethod
    def _extract_threads_post_id(post: dict[str, Any]) -> str:
        """Return the post id for /threads/post/replies (needs the numeric pk).

        Only ``native_id`` (the pk stashed by _normalise_posts) works here.
        The replies endpoint rejects both the URL code and the keyword search
        seed with HTTP 422, so the old query_term / URL-slug fallbacks just
        spent a call per post on a guaranteed rejection. Return "" when
        native_id is absent so the caller skips the post instead.
        """
        return str(post.get("native_id") or "").strip()

    def _normalise_threads_replies(
        self,
        items: list,
        query_group: str,
        parent_id: str,
        parent_url: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return rows
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item = self._unwrap_threads_post(raw_item)
            if not item:
                continue
            author_name, author_handle = self._extract_author(item)
            text = str(self._extract_text(item) or "")
            if not text.strip():
                continue
            published_at = self._extract_timestamp(item)
            _, likes, _, _ = self._extract_metrics(item)
            rows.append(
                {
                    "source": "EnsembleData",
                    "platform": "threads",
                    "market": self.market,
                    "content_type": "threads_reply",
                    "query_group": query_group,
                    "query_term": parent_id,
                    "author_name": author_name or "",
                    "author_handle": author_handle or "",
                    "title": text[:100],
                    "text": text,
                    "url": parent_url or "",
                    "published_at": published_at,
                    "views": 0.0,
                    "likes": likes,
                    "comments": 0.0,
                    "shares": 0.0,
                }
            )
        return rows

    # --- TikTok post info (per-post detail) ---------------------------

    def _fetch_tiktok_post_info_enrichment(
        self,
        posts: list[dict[str, Any]],
        top_n: int,
        budget: int,
        token: str,
    ) -> list[dict[str, Any]]:
        """Re-fetch top N TikTok posts via /tt/post/info for full detail.

        Used as a single-post probe / re-verification path. The per-run cap
        defaults to 5 when the YAML omits tt_post_info_per_run; set it to 0
        in the YAML to opt out (the caller guards on top_n > 0). 2 units
        per call.
        """
        if not posts:
            return []
        tiktok_posts = [
            p
            for p in posts
            if p.get("platform") == "tiktok" and p.get("content_type") == "post" and p.get("url")
        ]
        if not tiktok_posts:
            return []
        ranked = sorted(tiktok_posts, key=lambda r: float(r.get("likes") or 0), reverse=True)
        rows: list[dict[str, Any]] = []
        called = 0
        # Dedup on url: the same post can arrive via two hashtags/keywords, so
        # skip urls already fetched to avoid double-charging units and emitting
        # duplicate detail rows.
        seen_urls: set[str] = set()
        for post in ranked:
            if called >= top_n:
                break
            if self._quota_exhausted:
                break
            if self._budget_reached(budget):
                self.logger.info(
                    "EnsembleData budget reached during TT post info (%d/%d units); stopping",
                    self._units_spent,
                    budget,
                )
                break
            url = str(post.get("url") or "")
            if not url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            payload = self._enrichment_request(
                path=TIKTOK_POST_INFO_ENDPOINT["path"],
                params={"url": url},
                token=token,
            )
            called += 1
            if payload is None:
                continue
            items = self._extract_list_from_payload(payload)
            rows.extend(
                self._normalise_posts(
                    items,
                    platform="tiktok",
                    query_group=TIKTOK_POST_INFO_ENDPOINT["name"],
                    query_term=url,
                )
            )
        # _normalise_posts emits content_type=post; tag these as detail rows so
        # downstream consumers can distinguish the re-fetched detail from the
        # original hashtag/keyword row that seeded them.
        for r in rows:
            r["content_type"] = "tiktok_post_info"
        return rows

    # --- TikTok comment replies ---------------------------------------

    def _fetch_tiktok_comment_replies_enrichment(
        self,
        comments: list[dict[str, Any]],
        top_n: int,
        budget: int,
        token: str,
    ) -> list[dict[str, Any]]:
        """Fetch reply threads for top-ranked TikTok comments by likes.

        Needs (aweme_id, comment_id) pairs. Sources from existing
        tiktok_comment rows whose query_term carries the parent video id.
        Comment id is taken from the row's url field when populated, else
        from a `comment_id` stash set by the upstream normaliser. Cost 1
        unit per call.
        """
        if not comments:
            return []
        tiktok_comments = [
            c
            for c in comments
            if c.get("platform") == "tiktok"
            and c.get("content_type") == "tiktok_comment"
            and c.get("query_term")
        ]
        if not tiktok_comments:
            return []
        ranked = sorted(tiktok_comments, key=lambda r: float(r.get("likes") or 0), reverse=True)
        rows: list[dict[str, Any]] = []
        called = 0
        # Dedup on the (aweme_id, comment_id) pair: the same comment can appear
        # twice when its parent video was collected via two hashtags, so skip
        # pairs already fetched to avoid double-charging units and emitting
        # duplicate reply rows.
        seen_pairs: set[tuple[str, str]] = set()
        for cmt in ranked:
            if called >= top_n:
                break
            if self._quota_exhausted:
                break
            if self._budget_reached(budget):
                self.logger.info(
                    "EnsembleData budget reached during TT comment replies (%d/%d units); stopping",
                    self._units_spent,
                    budget,
                )
                break
            aweme_id = str(cmt.get("query_term") or "")
            comment_id = str(cmt.get("comment_id") or cmt.get("url") or "")
            if not aweme_id or not comment_id:
                continue
            pair = (aweme_id, comment_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            payload = self._enrichment_request(
                path=TIKTOK_COMMENT_REPLIES_ENDPOINT["path"],
                params={"aweme_id": aweme_id, "comment_id": comment_id, "cursor": 0},
                token=token,
            )
            called += 1
            if payload is None:
                continue
            items = self._extract_list_from_payload(payload)
            rows.extend(
                self._normalise_simple_comments(
                    items,
                    platform="tiktok",
                    content_type="tiktok_comment_reply",
                    query_group=TIKTOK_COMMENT_REPLIES_ENDPOINT["name"],
                    parent_id=f"{aweme_id}:{comment_id}",
                    parent_url="",
                )
            )
        return rows

    # --- Shared comment-shape normaliser ------------------------------

    def _normalise_simple_comments(
        self,
        items: list,
        platform: str,
        content_type: str,
        query_group: str,
        parent_id: str,
        parent_url: str,
    ) -> list[dict[str, Any]]:
        """Build RAW rows for a flat comment list (YT / IG / TT-reply).

        Drops empty bodies (deleted / hidden vendor entries) so the row
        count stays meaningful downstream.
        """
        rows: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return rows
        for item in items:
            if not isinstance(item, dict):
                continue
            author_name, author_handle = self._extract_author(item)
            text = str(self._extract_text(item) or "")
            if not text.strip():
                continue
            published_at = self._extract_timestamp(item)
            _, likes, _, _ = self._extract_metrics(item)
            rows.append(
                {
                    "source": "EnsembleData",
                    "platform": platform,
                    "market": self.market,
                    "content_type": content_type,
                    "query_group": query_group,
                    "query_term": parent_id,
                    "author_name": author_name or "",
                    "author_handle": author_handle or "",
                    "title": text[:100],
                    "text": text,
                    "url": parent_url or "",
                    "published_at": published_at,
                    "views": 0.0,
                    "likes": likes,
                    "comments": 0.0,
                    "shares": 0.0,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Twitter handle -> rest_id resolver (Wave 1 patch, 28 May 2026)
    # ------------------------------------------------------------------

    def _resolve_twitter_handle_to_id(self, handle: str, token: str) -> str:
        """Resolve a Twitter username to numeric rest_id via /twitter/user/info.

        Result cached in the process-level ``_TWITTER_HANDLE_REST_ID_CACHE``
        so the same handle appearing across markets only burns one resolve
        call per cron run. Empty string returned on any failure so the caller
        skips that handle for the run.
        """
        if not handle:
            return ""
        key = handle.lstrip("@").lower()
        cached = _TWITTER_HANDLE_REST_ID_CACHE.get(key)
        if cached is not None:
            return cached

        url = f"{self.BASE_URL}{TWITTER_USER_INFO_PATH}"
        params = {"token": token, "name": key}
        safe_url = self._safe_url(url)
        self.logger.info("EnsembleData request: %s name=%r", safe_url, key)
        try:
            resp = self._request("GET", url, params=params)
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == ENSEMBLE_QUOTA_STATUS:
                self.logger.error("EnsembleData quota exhausted (HTTP 495) on %s", safe_url)
                self._quota_exhausted = True
                _TWITTER_HANDLE_REST_ID_CACHE[key] = ""
                return ""
            if status == ENSEMBLE_AUTH_STATUS:
                self.logger.error(
                    "EnsembleData auth/billing error (HTTP 493) on %s. "
                    "Rotate ENSEMBLEDATA_API_TOKEN or check account billing.",
                    safe_url,
                )
                self._quota_exhausted = True
                _TWITTER_HANDLE_REST_ID_CACHE[key] = ""
                return ""
            self.logger.warning(
                "EnsembleData /twitter/user/info HTTPError for %s: %s",
                safe_url,
                str(exc)[:200],
            )
            _TWITTER_HANDLE_REST_ID_CACHE[key] = ""
            return ""
        except requests.exceptions.RequestException as exc:
            self._handle_request_exception(safe_url, exc)
            _TWITTER_HANDLE_REST_ID_CACHE[key] = ""
            return ""

        try:
            payload = resp.json()
        except ValueError:
            self.logger.warning("EnsembleData non-JSON response from %s", safe_url)
            _TWITTER_HANDLE_REST_ID_CACHE[key] = ""
            return ""

        self._units_spent += int(self._extract_units_charged(payload))
        self._note_transport_ok()

        data = payload.get("data") if isinstance(payload, dict) else None
        rest_id = ""
        if isinstance(data, dict):
            rest_id = str(data.get("rest_id") or data.get("id") or "")
        _TWITTER_HANDLE_REST_ID_CACHE[key] = rest_id
        return rest_id

    @classmethod
    def reset_twitter_handle_cache(cls) -> None:
        """Clear the process-level Twitter handle resolution cache.

        Intended for tests; production code reuses the cache across cron
        runs because each call burns 2 units.
        """
        _TWITTER_HANDLE_REST_ID_CACHE.clear()

    def _resolve_instagram_handle_to_id(self, handle: str, token: str) -> str:
        """Resolve an Instagram username to its numeric pk via /instagram/user/info.

        /instagram/user/posts takes user_id (integer), not a username, so each
        handle is resolved once. Cached in ``_INSTAGRAM_HANDLE_PK_CACHE`` so a
        handle that appears across markets only burns one resolve call per cron
        run. Empty string on any failure so the caller skips that handle.
        """
        if not handle:
            return ""
        key = handle.lstrip("@").lower()
        cached = _INSTAGRAM_HANDLE_PK_CACHE.get(key)
        if cached is not None:
            return cached

        url = f"{self.BASE_URL}{INSTAGRAM_USER_INFO_PATH}"
        params = {"token": token, "username": key}
        safe_url = self._safe_url(url)
        self.logger.info("EnsembleData request: %s username=%r", safe_url, key)
        try:
            resp = self._request("GET", url, params=params)
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == ENSEMBLE_QUOTA_STATUS:
                self.logger.error("EnsembleData quota exhausted (HTTP 495) on %s", safe_url)
                self._quota_exhausted = True
                _INSTAGRAM_HANDLE_PK_CACHE[key] = ""
                return ""
            if status == ENSEMBLE_AUTH_STATUS:
                self.logger.error(
                    "EnsembleData auth/billing error (HTTP 493) on %s. "
                    "Rotate ENSEMBLEDATA_API_TOKEN or check account billing.",
                    safe_url,
                )
                self._quota_exhausted = True
                _INSTAGRAM_HANDLE_PK_CACHE[key] = ""
                return ""
            self.logger.warning(
                "EnsembleData /instagram/user/info HTTPError for %s: %s",
                safe_url,
                str(exc)[:200],
            )
            _INSTAGRAM_HANDLE_PK_CACHE[key] = ""
            return ""
        except requests.exceptions.RequestException as exc:
            self._handle_request_exception(safe_url, exc)
            _INSTAGRAM_HANDLE_PK_CACHE[key] = ""
            return ""

        try:
            payload = resp.json()
        except ValueError:
            self.logger.warning("EnsembleData non-JSON response from %s", safe_url)
            _INSTAGRAM_HANDLE_PK_CACHE[key] = ""
            return ""

        self._units_spent += int(self._extract_units_charged(payload))
        self._note_transport_ok()

        data = payload.get("data") if isinstance(payload, dict) else None
        pk = ""
        if isinstance(data, dict):
            pk = str(data.get("pk") or data.get("pk_id") or data.get("id") or "")
        _INSTAGRAM_HANDLE_PK_CACHE[key] = pk
        return pk

    @classmethod
    def reset_instagram_handle_cache(cls) -> None:
        """Clear the process-level Instagram handle->pk cache. For tests."""
        _INSTAGRAM_HANDLE_PK_CACHE.clear()

    def _resolve_threads_handle_to_id(self, handle: str, token: str) -> str:
        """Resolve a Threads username to its threads-native pk via /threads/user/search.

        /threads/user/posts takes a threads pk that differs from the Instagram
        pk, so the handle is resolved against the search endpoint, which returns
        candidates under ``data[].node`` each carrying ``username`` + ``pk``. We
        take the exact username match (case-insensitive). Cached in
        ``_THREADS_HANDLE_PK_CACHE`` so a handle resolves once per cron run.
        Empty string on any failure so the caller skips that handle.
        """
        if not handle:
            return ""
        key = handle.lstrip("@").lower()
        cached = _THREADS_HANDLE_PK_CACHE.get(key)
        if cached is not None:
            return cached

        url = f"{self.BASE_URL}{THREADS_USER_SEARCH_PATH}"
        params = {"token": token, "name": key}
        safe_url = self._safe_url(url)
        self.logger.info("EnsembleData request: %s name=%r", safe_url, key)
        try:
            resp = self._request("GET", url, params=params)
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == ENSEMBLE_QUOTA_STATUS:
                self.logger.error("EnsembleData quota exhausted (HTTP 495) on %s", safe_url)
                self._quota_exhausted = True
                _THREADS_HANDLE_PK_CACHE[key] = ""
                return ""
            if status == ENSEMBLE_AUTH_STATUS:
                self.logger.error(
                    "EnsembleData auth/billing error (HTTP 493) on %s. "
                    "Rotate ENSEMBLEDATA_API_TOKEN or check account billing.",
                    safe_url,
                )
                self._quota_exhausted = True
                _THREADS_HANDLE_PK_CACHE[key] = ""
                return ""
            self.logger.warning(
                "EnsembleData /threads/user/search HTTPError for %s: %s",
                safe_url,
                str(exc)[:200],
            )
            _THREADS_HANDLE_PK_CACHE[key] = ""
            return ""
        except requests.exceptions.RequestException as exc:
            self._handle_request_exception(safe_url, exc)
            _THREADS_HANDLE_PK_CACHE[key] = ""
            return ""

        try:
            payload = resp.json()
        except ValueError:
            self.logger.warning("EnsembleData non-JSON response from %s", safe_url)
            _THREADS_HANDLE_PK_CACHE[key] = ""
            return ""

        self._units_spent += int(self._extract_units_charged(payload))
        self._note_transport_ok()

        results = payload.get("data") if isinstance(payload, dict) else None
        pk = ""
        if isinstance(results, list):
            nodes = [r.get("node") or {} for r in results if isinstance(r, dict)]
            match = next(
                (n for n in nodes if str(n.get("username", "")).lower() == key),
                nodes[0] if nodes else {},
            )
            pk = str(match.get("pk") or match.get("id") or "")
        _THREADS_HANDLE_PK_CACHE[key] = pk
        return pk

    @classmethod
    def reset_threads_handle_cache(cls) -> None:
        """Clear the process-level Threads handle->pk cache. For tests."""
        _THREADS_HANDLE_PK_CACHE.clear()

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    def _finalise(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        if not rows:
            return self.empty_dataframe()
        df = pd.DataFrame(rows)
        # Deduplicate by (url, text, author_handle) where a url is present.
        # url alone collapses comment and reply enrichment rows: they carry
        # their parent post's url, so N comments on one post would dedup to a
        # single row. A real duplicate post (same url fetched via two hashtags)
        # still collapses because its text and author match too, so the live
        # post path is unchanged.
        if "url" in df.columns:
            non_empty = df["url"].fillna("").astype(str) != ""
            if non_empty.any():
                dedup_keys = [c for c in ("url", "text", "author_handle") if c in df.columns]
                df = pd.concat(
                    [df[~non_empty], df[non_empty].drop_duplicates(subset=dedup_keys)],
                    ignore_index=True,
                )
        # Post-fetch non-Latin-script filter. TikTok's API returns global posts
        # for any hashtag / keyword, and its own platform does not expose
        # reliable geographic metadata. The cheapest viable filter for SSA
        # relevance is to drop posts whose caption text is dominantly written
        # in a script no SSA market uses natively (Arabic, CJK, Cyrillic,
        # Thai, Korean). Modern Swahili and Afrikaans both use Latin script,
        # so this does not hit diaspora creator content.
        if "text" in df.columns and len(df):
            mask_keep = ~df["text"].fillna("").astype(str).map(_is_mostly_non_latin)
            dropped = int((~mask_keep).sum())
            if dropped:
                self.logger.info(
                    "Ensemble dropped %d non-Latin-script posts (market=%s)",
                    dropped,
                    self.market,
                )
            df = df[mask_keep].reset_index(drop=True)
        # Post-fetch recency filter (ENSEMBLE_MAX_POST_AGE_DAYS, default 0 = off
        # so the cron is byte-identical). Threads keyword search and the TikTok
        # hashtag feed return multi-year archives that pollute recency + velocity;
        # when a window is set, drop posts older than it. Rows with no timestamp
        # are kept (no signal to judge), matching the engagement-damping convention.
        try:
            max_age_days = int(os.environ.get("ENSEMBLE_MAX_POST_AGE_DAYS", "0") or "0")
        except ValueError:
            max_age_days = 0
        if max_age_days > 0 and "published_at" in df.columns and len(df):
            from datetime import timedelta

            cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
            ts = pd.to_datetime(df["published_at"], utc=True, errors="coerce")
            age_keep = ts.isna() | (ts >= cutoff)
            dropped_age = int((~age_keep).sum())
            if dropped_age:
                self.logger.info(
                    "Ensemble dropped %d posts older than %d days (market=%s)",
                    dropped_age,
                    max_age_days,
                    self.market,
                )
            df = df[age_keep].reset_index(drop=True)
        # reindex (not project) so non-applicable columns like GKG v2tone get
        # filled with "" instead of raising KeyError when the RAW schema grows.
        return df.reindex(columns=list(self.empty_dataframe().columns), fill_value="")

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_url(url: str) -> str:
        """Strip querystring from a URL so any token embedded there is not logged."""
        try:
            return urlparse(url)._replace(query="").geturl()
        except Exception:
            return url.split("?", 1)[0]

    # ------------------------------------------------------------------
    # Payload helpers (ported from MVP)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_units_charged(payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0
        for key in ("units_charged", "cost_units", "units"):
            value = payload.get(key)
            if isinstance(value, int | float):
                return int(value)
        return 0

    @staticmethod
    def _safe_get(obj: Any, path: list, default: Any = None) -> Any:
        cur = obj
        for key in path:
            if isinstance(cur, dict):
                cur = cur.get(key)
            elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
                cur = cur[key]
            else:
                return default
            if cur is None:
                return default
        return cur

    @classmethod
    def _pick_first(cls, item: dict, candidates: list, default: Any = None) -> Any:
        for candidate in candidates:
            if isinstance(candidate, str):
                value = item.get(candidate) if isinstance(item, dict) else None
                if value is not None:
                    return value
            elif isinstance(candidate, list):
                value = cls._safe_get(item, candidate, default=None)
                if value is not None:
                    return value
        return default

    @classmethod
    def _extract_list_from_payload(cls, payload: Any) -> list:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []

        preferred = [
            "data",
            "posts",
            "results",
            "items",
            "aweme_list",
            "list",
            "response",
            "edges",
            "threads",
            "videos",
            "tweets",
            "tweet_items",
            "comments",
        ]

        for key in preferred:
            value = payload.get(key)
            if isinstance(value, list) and value:
                return value

        for outer in payload.values():
            if isinstance(outer, dict):
                for key in preferred:
                    value = outer.get(key)
                    if isinstance(value, list) and value:
                        return value

        found: list[list] = []

        def walk(obj: Any) -> None:
            if isinstance(obj, list):
                if obj and all(isinstance(x, dict) for x in obj):
                    found.append(obj)
                for x in obj:
                    walk(x)
            elif isinstance(obj, dict):
                for v in obj.values():
                    walk(v)

        walk(payload)
        if found:
            found.sort(key=len, reverse=True)
            return found[0]
        return []

    @classmethod
    def _extract_author(cls, item: dict) -> tuple[str, str]:
        # WATCHLIST_HANDLE_FIX (default off): a numeric user id is an opaque
        # identifier, not a username, so it can never join the username-based
        # creator watchlist. When on, drop the numeric-id keys from the handle
        # pick-lists (so a nested username is reached) and reject any handle that
        # is still purely numeric. The OFF path is byte-identical to the cron.
        handle_fix = os.environ.get("WATCHLIST_HANDLE_FIX", "false").lower() == "true"
        flat_handle_keys = [
            "author_handle",
            "username",
            "unique_id",
            "user_id",
            "owner_username",
            "screen_name",
        ]
        nested_handle_keys = ["username", "unique_id", "screen_name", "id"]
        if handle_fix:
            flat_handle_keys = [
                "author_handle",
                "username",
                "unique_id",
                "owner_username",
                "screen_name",
            ]
            nested_handle_keys = ["username", "unique_id", "screen_name"]

        flat_name = cls._pick_first(
            item,
            [
                "author_name",
                "username",
                "unique_id",
                "user_name",
                "screen_name",
                "owner_username",
            ],
            "",
        )
        flat_handle = cls._pick_first(item, flat_handle_keys, "")

        name = flat_name if not isinstance(flat_name, dict) else ""
        handle = flat_handle if not isinstance(flat_handle, dict) else ""
        if handle_fix and str(handle or "").strip().isdigit():
            handle = ""
        if name or handle:
            return str(name or ""), str(handle or "")

        for path in (
            ["author"],
            ["user"],
            ["owner"],
            ["node", "owner"],
            ["author_info"],
            # Twitter / X shapes (v1.1 + GraphQL Tweet object)
            ["legacy", "user"],
            ["core", "user_results", "result", "legacy"],
        ):
            nested = cls._safe_get(item, path, default={})
            if isinstance(nested, dict):
                nested_name = cls._pick_first(
                    nested,
                    ["nickname", "full_name", "name", "username", "unique_id", "screen_name"],
                    "",
                )
                nested_handle = cls._pick_first(nested, nested_handle_keys, "")
                if handle_fix and str(nested_handle or "").strip().isdigit():
                    nested_handle = ""
                if nested_name or nested_handle:
                    return str(nested_name or ""), str(nested_handle or "")

        return "", ""

    @classmethod
    def _extract_text(cls, item: dict) -> str:
        # Nested paths come first because some platforms (IG, Threads) carry
        # the caption text under `caption.text` while leaving a top-level
        # `caption` dict that would otherwise short-circuit the pick. Bare
        # string keys (TikTok / others) act as the flat-shape fallback.
        value = cls._pick_first(
            item,
            [
                ["caption", "text"],
                ["node", "edge_media_to_caption", "edges", 0, "node", "text"],
                ["node", "text"],
                ["post", "text"],
                # Twitter / X carries the tweet body under full_text on
                # extended tweets and text on truncated ones. legacy.full_text
                # is the GraphQL shape; full_text + text are the v1.1 shapes.
                ["legacy", "full_text"],
                ["legacy", "text"],
                "full_text",
                "text",
                "caption",
                "desc",
                "description",
                "body",
                "content",
            ],
            "",
        )
        # Defensive: if a flat candidate returned a dict (rare connector
        # quirks), reject so the row text stays a string. Empty rather than
        # `str(dict)` which would JSON-encode a dict into the haystack.
        if isinstance(value, dict):
            return ""
        return str(value or "")

    @classmethod
    def _extract_title(cls, item: dict) -> str:
        return (
            cls._pick_first(
                item,
                ["title", "headline", ["node", "title"]],
                "",
            )
            or ""
        )

    @classmethod
    def _extract_url(cls, item: dict, platform: str = "") -> str:
        url = cls._pick_first(
            item,
            [
                "url",
                "post_url",
                "share_url",
                "web_url",
                "permalink",
                ["node", "url"],
                ["post", "url"],
            ],
            "",
        )
        if url:
            return str(url)

        item_id = cls._pick_first(item, ["id", "aweme_id", "pk"], "")
        username = cls._pick_first(
            item,
            [
                "username",
                "unique_id",
                "owner_username",
                ["author", "username"],
                ["author", "unique_id"],
                ["user", "username"],
                ["user", "unique_id"],
            ],
            "",
        )
        shortcode = cls._pick_first(item, [["node", "shortcode"], "shortcode"], "")

        if platform == "tiktok" and item_id and username:
            return f"https://www.tiktok.com/@{username}/video/{item_id}"
        if platform == "instagram" and shortcode:
            return f"https://www.instagram.com/p/{shortcode}/"
        if platform == "twitter":
            # Twitter / X exposes the tweet id under id, id_str, tweet_id,
            # or rest_id depending on the EnsembleData payload shape.
            tweet_id = (
                item_id
                or cls._pick_first(item, ["id_str", "tweet_id", "rest_id", "conversation_id"], "")
                or ""
            )
            tweet_user = username or cls._pick_first(
                item,
                [
                    "screen_name",
                    ["user", "screen_name"],
                    ["author", "screen_name"],
                    ["core", "user_results", "result", "legacy", "screen_name"],
                ],
                "",
            )
            if tweet_id and tweet_user:
                handle = str(tweet_user).lstrip("@")
                return f"https://twitter.com/{handle}/status/{tweet_id}"
        # Threads post URL pattern: https://www.threads.net/@{username}/post/{code}
        # The post dict carries the slug as `code` and username under user.username
        # (resolved by the username pick-list above which already covers both).
        if platform == "threads":
            code = cls._pick_first(item, ["code", "shortcode", "id", "pk"], "")
            if username and code:
                return f"https://www.threads.net/@{username}/post/{code}"
        return ""

    @classmethod
    def _extract_timestamp(cls, item: dict) -> Any:
        """Return a tz-aware UTC datetime, or None.

        Never returns the raw input. The raw_content BigQuery column is TIMESTAMP,
        so a string or bare int would produce a mixed-type Series and fail the load.
        """
        raw = cls._pick_first(
            item,
            [
                "create_time",
                "created_at",
                "published_at",
                "taken_at",
                "timestamp",
                ["node", "created_at"],
                ["node", "taken_at_timestamp"],
                ["post", "created_at"],
                # Twitter v1.1 / GraphQL nests created_at under legacy.
                ["legacy", "created_at"],
            ],
            None,
        )
        if raw is None or raw == "":
            return None
        # Unix epoch seconds
        if isinstance(raw, int | float):
            try:
                return datetime.fromtimestamp(int(raw), tz=UTC)
            except (OSError, ValueError, OverflowError):
                return None
        if isinstance(raw, str):
            if raw.isdigit():
                try:
                    return datetime.fromtimestamp(int(raw), tz=UTC)
                except (OSError, ValueError, OverflowError):
                    return None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                # Twitter's created_at is "Wed Apr 20 12:00:00 +0000 2024",
                # which fromisoformat rejects. Fall back to its strptime format
                # before giving up so tweets keep a real published_at (velocity
                # and recency scoring depend on it).
                try:
                    return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
                except ValueError:
                    return None
            # fromisoformat returns naive datetimes when the input lacks a tz marker.
            # Coerce to UTC so the column is uniformly tz-aware.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        return None

    @classmethod
    def _extract_metrics(cls, item: dict) -> tuple[float, float, float, float]:
        metrics_obj = cls._pick_first(
            item,
            [
                "statistics",
                "stats",
                "metrics",
                ["node", "statistics"],
                ["node", "metrics"],
            ],
            {},
        )

        def metric(candidates: list, fallback: float = 0.0) -> float:
            val = cls._pick_first(item, candidates, None)
            if val is None and isinstance(metrics_obj, dict):
                val = cls._pick_first(metrics_obj, candidates, None)
            try:
                return float(val) if val is not None else float(fallback)
            except (TypeError, ValueError):
                return float(fallback)

        views = metric(
            [
                "play_count",
                "view_count",
                # Nested before flat: the Twitter GraphQL shape carries a
                # `views` DICT ({"count": "51234"}), which the flat "views"
                # candidate would match first and coerce to 0.0.
                ["views", "count"],
                "views",
                "video_view_count",
                "video_play_count",
                "impression_count",
                ["legacy", "view_count"],
                ["views", "count"],
            ]
        )
        likes = metric(
            ["digg_count", "like_count", "likes", "favorite_count", ["legacy", "favorite_count"]]
        )
        comments = metric(
            [
                "comment_count",
                "comments_count",
                "comments",
                "reply_count",
                ["legacy", "reply_count"],
            ]
        )
        shares = metric(
            [
                "share_count",
                "shares",
                "repost_count",
                "reshare_count",
                "retweet_count",
                ["legacy", "retweet_count"],
            ]
        )
        return views, likes, comments, shares

    # ------------------------------------------------------------------
    # Row normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _unwrap_twitter_tweet(item: dict) -> dict:
        """Unwrap a tweet from the GraphQL timeline-entry envelope.

        /twitter/user/tweets returns timeline entries shaped
        ``entry['content']['itemContent']['tweet_results']['result']`` with the
        tweet fields under ``result['legacy']`` (probe 2026-07-02, doc
        docs/twitter-probe-2026-07-02.md). Some results add one more level:
        ``result['tweet']`` (TweetWithVisibilityResults). Cursor / module
        entries carry ``content`` without a tweet; those return {} so the
        caller skips them instead of emitting hollow rows. Flat v1.1 tweet
        dicts pass through unchanged.
        """
        try:
            content = item.get("content")
            if not isinstance(content, dict):
                return item
            result = EnsembleConnector._safe_get(
                content, ["itemContent", "tweet_results", "result"]
            )
            if isinstance(result, dict):
                inner = result.get("tweet")
                if isinstance(inner, dict) and inner:
                    return inner
                return result
            return {}
        except (AttributeError, TypeError):
            return item

    @staticmethod
    def _unwrap_threads_post(item: dict) -> dict:
        """Unwrap a Threads post from its envelope.

        /threads/keyword/search nests the post at
        ``item['node']['thread']['thread_items'][0]['post']``, while
        /threads/post/replies puts ``thread_items`` directly under ``node``
        (no ``thread`` level). Handle both. Other endpoints already return
        the post dict at the top level.

        Returns the inner post dict on success. Returns the original item
        unchanged when the structure does not match so a future Ensemble
        response change surfaces as empty rows rather than a crash.
        """
        try:
            node = item.get("node") or {}
            thread = node.get("thread") or {}
            thread_items = thread.get("thread_items") or node.get("thread_items") or []
            if thread_items and isinstance(thread_items[0], dict):
                post = thread_items[0].get("post")
                if isinstance(post, dict) and post:
                    return post
        except (AttributeError, TypeError):
            pass
        return item

    def _normalise_posts(
        self,
        items: list,
        platform: str,
        query_group: str,
        query_term: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return rows

        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            # Threads wraps the post under node.thread.thread_items[0].post;
            # TikTok keyword search wraps it under aweme_info (hashtag + post-info
            # hand back a flat dict). Unwrap both or the wrapped rows land hollow
            # (null timestamp, empty text/url, zero metrics) and pollute recency.
            if platform == "threads":
                item = self._unwrap_threads_post(raw_item)
            elif platform == "twitter":
                item = self._unwrap_twitter_tweet(raw_item)
            elif platform == "tiktok" and isinstance(raw_item.get("aweme_info"), dict):
                item = raw_item["aweme_info"]
            else:
                item = raw_item
            if not item:
                continue
            author_name, author_handle = self._extract_author(item)
            text = str(self._extract_text(item) or "")
            title_full = str(self._extract_title(item) or "") or text[:100]
            url = str(self._extract_url(item, platform=platform) or "")
            published_at = self._extract_timestamp(item)
            views, likes, comments, shares = self._extract_metrics(item)

            captures = _extract_structured_capture(
                item, platform, author_handle or "", query_term=query_term
            )
            row_dict = {
                "source": "EnsembleData",
                "platform": platform,
                "market": self.market,
                "content_type": "post",
                "query_group": query_group,
                "query_term": query_term,
                # Native vendor post id (pk preferred over composite id).
                # Dropped by _finalise (not a RAW_COLUMN); carried only so
                # reply / comment enrichment can address the post.
                # /threads/post/replies needs this numeric pk.
                "native_id": str(self._pick_first(item, ["pk", "id", "rest_id"], "") or ""),
                "author_name": author_name or "",
                "author_handle": author_handle or "",
                "title": title_full[:100] if title_full else "",
                "text": text,
                "url": url,
                "published_at": published_at,
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
            }
            enc = _encode_structured_capture(captures)
            if enc:
                row_dict["v2gcam"] = enc
            rows.append(row_dict)

        return rows
