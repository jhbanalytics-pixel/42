"""SocialCrawl connector: the engine's primary social ingestion vendor.

Replaces EnsembleData (cancelled 23 Jul 2026). One x-api-key, 44 platforms,
a unified {computed, post} envelope, pay-as-you-go credits. This connector
carries every social surface the engine used to buy from EnsembleData plus the
three it never could: per-country trending feeds, a real creator-region lookup,
and Google News.

Surfaces run in PHASES, cheapest-signal-first, so the credit breaker can only
ever trim the tail:

  A discover  geo-scoped trending (tiktok/trending, youtube/videos/trending)
  B creators  watchlist handles (tiktok, instagram, threads) -- the sole
              source of the creator_watchlist signal, so it spends before
              any keyword breadth
  C search    per-market term pools (tiktok/search/top, youtube/search,
              threads/search)
  D reddit    subreddit feeds + keyword search (replaces the EnsembleData
              Reddit connector)
  E accounts  twitter/user/tweets on the per-market handle list
  F news      google_news/search
  G facebook  curated pages and groups (facebook/profile/posts,
              facebook/group/posts). There is no keyword post search on
              Facebook at any price, so the surface is only what we name.

``PHASE_ORDER`` in this module is the authoritative sequence; count the phases
there rather than off this list.

Everything is bounded twice: a per-run credit budget shared across the three
markets via a class-level tally, and a per-phase share of it. Any HTTP or parse
error is a non-fatal _fetch_failure (a degradation, not a market failure, per
the fatal split in run_rss_now.log_pipeline_run).

LIVE-PROBED 23 Jul 2026 (see docs/socialcrawl-probe-2026-07-23.md). Three
findings the OpenAPI spec does not tell you, each of which silently returns
zero rows if you trust the docs instead:
  - tiktok/search with ANY date_posted or region value returns 0 items. Use
    tiktok/search/top with publish_time instead: 30 items, all inside 7 days.
  - youtube/search uploadDate is `this_week`, not `week`.
  - twitter/user/tweets returns published_at in Twitter's own format
    ("Wed Oct 30 05:45:03 +0000 2019"), leaves author.username null, and is
    NOT sorted newest-first. Unparsed it writes garbage timestamps and
    six-year-old posts into enriched_content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, ClassVar

import pandas as pd
import requests

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_market_creators, load_sources
from src.utils.secrets import get_secret

BASE_URL = "https://www.socialcrawl.dev/v1"
WAVE1_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
WAVE1_RESPONSE_MAX_ITEMS = 1000
WAVE1_AUTHORITY_COLUMNS = (
    "endpoint",
    "vendor_family",
    "channel_family",
    "source_family",
    "geo_method_id",
    "geo_receipt_id",
    "native_id",
    "source_family_map_version",
)


def _wave1_audio_id(item: Mapping) -> str | None:
    for value in (item.get("audio_id"), item.get("music_id")):
        if isinstance(value, str) and value:
            return value
    # The Instagram audio library (search/music, music/trending) returns tracks:
    # the reels audio page id is track.audio_cluster_id (vendor OpenAPI example,
    # 4 Sep 2026), with audio_asset_id as the fallback.
    track = item.get("track")
    if isinstance(track, Mapping):
        for key in ("audio_cluster_id", "audio_asset_id", "audio_id", "id"):
            value = track.get(key)
            if isinstance(value, str) and value:
                return value
    post = item.get("post")
    ext = post.get("ext") if isinstance(post, Mapping) else None
    if isinstance(ext, Mapping):
        for key in ("audio_id", "music_id"):
            value = ext.get(key)
            if isinstance(value, str) and value:
                return value
    url = item.get("url") if isinstance(item.get("url"), str) else None
    if url is None and isinstance(post, Mapping) and isinstance(post.get("url"), str):
        url = post["url"]
    if url:
        match = re.search(r"/reels?/audio/(\d+)", url)
        if match:
            return match.group(1)
    value = item.get("id")
    return value if isinstance(value, str) and value else None


def _wave1_item_url(item: Mapping) -> str | None:
    value = item.get("url")
    if isinstance(value, str) and value:
        return value
    post = item.get("post")
    if isinstance(post, Mapping) and isinstance(post.get("url"), str) and post["url"]:
        return post["url"]
    return None


def _wave1_chain_values(items, extract, *, limit: int) -> list[str]:
    values: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        value = extract(item)
        if value is not None and value not in values:
            values.append(value)
        if len(values) == limit:
            break
    return values


def _read_wave1_payload(response: object) -> object:
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        return response.json()
    content = bytearray()
    close = getattr(response, "close", None)
    try:
        for chunk in iterator(chunk_size=64 * 1024):
            if not isinstance(chunk, bytes):
                raise ValueError("Wave 1 response stream is invalid")
            content.extend(chunk)
            if len(content) > WAVE1_RESPONSE_MAX_BYTES:
                raise ValueError("Wave 1 response exceeds byte ceiling")
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Wave 1 response is invalid") from error
    finally:
        if callable(close):
            close()


# ISO 3166-1 alpha-2 per market, for the region-scoped surfaces.
MARKET_REGION: dict[str, str] = {"za": "ZA", "ng": "NG", "ke": "KE"}
# Google News takes a location NAME, not a code.
MARKET_LOCATION_NAME: dict[str, str] = {
    "za": "South Africa",
    "ng": "Nigeria",
    "ke": "Kenya",
}

# Per-call credit cost, from the vendor pricing table (probed 23 Jul 2026).
# Used to pre-check the budget BEFORE a call so an advanced-tier surface
# cannot overshoot the cap by its own price.
CREDIT_COST: dict[str, int] = {
    "tiktok/trending": 5,
    "tiktok/search/top": 1,
    "tiktok/profile/videos": 1,
    "tiktok/profile/region": 1,
    "youtube/videos/trending": 1,
    "youtube/search": 1,
    "threads/search": 1,
    "threads/user/posts": 1,
    "instagram/profile/posts": 1,
    "instagram/search/hashtag": 5,
    "reddit/subreddit": 1,
    "reddit/search": 1,
    "twitter/user/tweets": 1,
    "google_news/search": 1,
    "facebook/profile/posts": 1,
    "facebook/group/posts": 1,
    "tiktok/song": 1,
    "tiktok/song/videos": 1,
    "instagram/music/trending": 5,
    "instagram/audio/reels": 1,
    "instagram/search/reels": 9,
    "youtube/shorts/trending": 15,
    "youtube/video/comments": 5,
    "reddit/post/comments": 9,
}
DEFAULT_CREDIT_COST = 1

# Sort sweep per subreddit. `rising` is deliberately absent: probed 27 Jul 2026
# against r/southafrica and r/Nigeria it returned the SAME item set as `hot`,
# so it costs a credit and adds nothing. Override via caps.reddit_sort_modes.
DEFAULT_REDDIT_SORTS: list[dict[str, str]] = [
    {"sort": "hot"},
    {"sort": "top", "timeframe": "week"},
]

DEFAULT_BUDGET_CREDITS_PER_RUN = 900
# Share of the run budget each phase may consume. Ordered; a phase that comes
# in under its share leaves the remainder to later phases.
DEFAULT_PHASE_SHARE: dict[str, float] = {
    "discover": 0.10,
    "creators": 0.45,
    "search": 0.25,
    "reddit": 0.10,
    "accounts": 0.05,
    "news": 0.05,
    "facebook": 0.0,
}
PHASE_ORDER: tuple[str, ...] = (
    "discover",
    "creators",
    "search",
    "reddit",
    "accounts",
    "news",
    "facebook",
)


@dataclass(frozen=True, slots=True)
class Wave1RouteSpec:
    phase: str
    credit_cost: int
    maximum_calls: int
    maximum_debit: int
    vendor_family: str
    channel_family: str
    required_parameters: tuple[str, ...]
    maximum_pages: int = 1
    state: str = "blocked_fixture_unapproved"


WAVE1_ROUTE_SPECS = MappingProxyType(
    {
        "tiktok/song": Wave1RouteSpec(
            "wave1_tiktok_sound", 1, 5, 5, "socialcrawl", "short_video", ("clipId",)
        ),
        "tiktok/song/videos": Wave1RouteSpec(
            "wave1_tiktok_sound", 1, 5, 5, "socialcrawl", "short_video", ("clipId",)
        ),
        "instagram/music/trending": Wave1RouteSpec(
            "wave1_instagram_reels", 5, 2, 10, "socialcrawl", "short_video", ()
        ),
        "instagram/audio/reels": Wave1RouteSpec(
            "wave1_instagram_reels", 1, 5, 5, "socialcrawl", "short_video", ("audio_id",)
        ),
        "instagram/search/reels": Wave1RouteSpec(
            "wave1_instagram_reels", 9, 2, 18, "socialcrawl", "short_video", ("query",)
        ),
        "youtube/shorts/trending": Wave1RouteSpec(
            "wave1_youtube_shorts_comments", 15, 1, 15, "socialcrawl", "youtube", ()
        ),
        "youtube/video/comments": Wave1RouteSpec(
            "wave1_youtube_shorts_comments", 5, 3, 15, "socialcrawl", "youtube", ("url",)
        ),
        "reddit/post/comments": Wave1RouteSpec(
            "wave1_reddit_comments", 9, 2, 18, "socialcrawl", "reddit", ("url",)
        ),
    }
)


def wave1_route_set_sha256(specs: Mapping[str, Wave1RouteSpec]) -> str:
    """Content digest of the frozen Wave 1 route table with the ``state`` field excluded.

    The consumed ``wave1_pilot`` authority binds this digest through its ``wave1_contract``
    artifact, so a route table that differs from the approved one can never run under it.
    """
    projection = {
        route: {
            "phase": spec.phase,
            "credit_cost": spec.credit_cost,
            "maximum_calls": spec.maximum_calls,
            "maximum_debit": spec.maximum_debit,
            "vendor_family": spec.vendor_family,
            "channel_family": spec.channel_family,
            "required_parameters": list(spec.required_parameters),
            "maximum_pages": spec.maximum_pages,
        }
        for route, spec in sorted(specs.items())
    }
    encoded = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


WAVE1_ROUTE_SET_SHA256 = wave1_route_set_sha256(WAVE1_ROUTE_SPECS)


@dataclass(frozen=True, slots=True)
class Wave1RouteRequest:
    route: str
    params: Mapping[str, str]
    call_role: str
    page: int = 1
    # The market the seed came from. It is the retained geography of every row
    # the call returns; a call without one (a global trending surface) yields
    # rows the adapter cannot place and they never become evidence.
    market: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.route, str) or not self.route:
            raise ValueError("Wave 1 route is invalid")
        if self.market is not None and self.market not in MARKET_REGION:
            raise ValueError("Wave 1 seed market is invalid")
        if not isinstance(self.params, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in self.params.items()
        ):
            raise ValueError("Wave 1 parameters are invalid")
        if self.call_role not in {"qualification", "ingestion"}:
            raise ValueError("Wave 1 call role is invalid")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise ValueError("Wave 1 page is invalid")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


@dataclass(frozen=True, slots=True)
class SocialCrawlPhaseUsage:
    market: str
    phase: str
    calls: int
    budget_debit_credits: int
    vendor_reported_credits: int
    authorized_quoted_debit: int | None = None

    def __post_init__(self) -> None:
        if self.market not in MARKET_REGION:
            raise ValueError("market is unsupported")
        if self.phase not in PHASE_ORDER:
            raise ValueError("phase is unsupported")
        for field in ("calls", "budget_debit_credits", "vendor_reported_credits"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field.replace('_', ' ')} is invalid")
        if self.vendor_reported_credits > self.budget_debit_credits:
            raise ValueError("vendor reported credits cannot exceed budget debit")
        quoted = (
            self.budget_debit_credits
            if self.authorized_quoted_debit is None
            else self.authorized_quoted_debit
        )
        if (
            isinstance(quoted, bool)
            or not isinstance(quoted, int)
            or not 0 <= quoted <= self.budget_debit_credits
        ):
            raise ValueError("authorized quoted debit is invalid")
        object.__setattr__(self, "authorized_quoted_debit", quoted)


@dataclass(frozen=True, slots=True)
class FundedSocialCrawlContext:
    credential_lane: str
    secret_id: str
    run_allowance: int
    phase_close_hook: Callable[[object], None]
    pre_call_authority_hook: Callable[[int], None]
    stage_name: str | None = None
    execution_capability: object | None = None
    wave1_requests: tuple[Wave1RouteRequest, ...] = ()

    def __post_init__(self) -> None:
        if self.credential_lane != "ogilvy_funded":
            raise ValueError("funded credential lane is invalid")
        if self.secret_id != "SOCIALCRAWL_OGILVY_API_KEY":
            raise ValueError("funded secret ID is invalid")
        if self.run_allowance not in {100, 250, 750}:
            raise ValueError("funded run allowance is invalid")
        if not callable(self.phase_close_hook):
            raise ValueError("phase close hook is required")
        if not callable(self.pre_call_authority_hook):
            raise ValueError("pre-call authority hook is required")
        if not isinstance(self.wave1_requests, tuple) or any(
            not isinstance(item, Wave1RouteRequest) for item in self.wave1_requests
        ):
            raise ValueError("Wave 1 requests are invalid")
        if self.stage_name != "stage_1_wave_1" and self.wave1_requests:
            raise ValueError("Wave 1 requests require the Wave 1 stage")


# On staging the unfunded lane is refused, not attempted. Its secret,
# SOCIALCRAWL_API_KEY, holds no credits, and a missing or dry key there used to
# come back as an empty frame that read like a quiet source. Staging collection
# runs through the governed funded lane, which reads SOCIALCRAWL_OGILVY_API_KEY
# under its own allowance, caps and authority hooks; only the producer can
# prepare that lane, so this connector never switches lanes on its own.
STAGING_UNFUNDED_REFUSAL = (
    "socialcrawl: unfunded lane refused on staging: SOCIALCRAWL_API_KEY is not "
    "the funded credential; staging collection runs the governed lane "
    "(SOCIALCRAWL_CREDENTIAL_LANE=ogilvy_funded, secret SOCIALCRAWL_OGILVY_API_KEY)"
)

DEFAULT_TIMEOUT = 45
# Posts older than this are dropped. Hashtag and account timelines happily
# return 2019 content; a trends engine that ingests it scores noise.
DEFAULT_MAX_AGE_DAYS = 21

SOCIALCRAWL_QUERY_GROUP = "socialcrawl"

# Twitter's own timestamp format, e.g. "Wed Oct 30 05:45:03 +0000 2019".
TWITTER_DATE_FORMAT = "%a %b %d %H:%M:%S %z %Y"


class SocialCrawlConnector(BaseConnector):
    """Phased, credit-bounded multi-surface pull from SocialCrawl for one market.

    Reads sources.yaml `socialcrawl`: enabled, budget_credits_per_run,
    max_age_days, phases (per-phase share + per-surface caps), and the
    per-market block socialcrawl.markets.<market>.
    """

    SOURCE_NAME = "socialcrawl"
    PLATFORM = "socialcrawl"
    RATE_LIMIT_DELAY = 0.0

    # Shared across the three market instances in one process so the credit
    # budget bounds the whole run, not each market. Reset only in tests and by
    # run_rss_now before the market loop.
    _global_credits_used: ClassVar[int] = 0
    _global_credits_by_phase: ClassVar[dict[str, int]] = {}
    # Per-market split of the same tally. _global_credits_used is cumulative
    # across the market loop, so it cannot be written to a per-market row
    # without double counting; this is what pipeline_runs.socialcrawl_credits
    # reads, and it sums per day exactly like socialcrawl_rows.
    _global_credits_by_market: ClassVar[dict[str, int]] = {}
    _global_calls: ClassVar[int] = 0
    _global_calls_by_market_phase: ClassVar[dict[tuple[str, str], int]] = {}
    _global_budget_by_market_phase: ClassVar[dict[tuple[str, str], int]] = {}
    _global_vendor_by_market_phase: ClassVar[dict[tuple[str, str], int]] = {}
    _global_vendor_credits_used: ClassVar[int] = 0
    _global_out_of_credit: ClassVar[bool] = False
    _global_attribution_failed: ClassVar[bool] = False
    _funded_context: ClassVar[FundedSocialCrawlContext | None] = None
    _wave1_executed: ClassVar[bool] = False
    _wave1_unique_count: ClassVar[int] = 0
    _wave1_frames_by_market: ClassVar[dict[str, pd.DataFrame]] = {}
    _wave1_source_value_results: ClassVar[dict[str, object]] = {}

    @classmethod
    def reset_credits(cls) -> None:
        cls._global_credits_used = 0
        cls._global_credits_by_phase = {}
        cls._global_credits_by_market = {}
        cls._global_calls = 0
        cls._global_calls_by_market_phase = {}
        cls._global_budget_by_market_phase = {}
        cls._global_vendor_by_market_phase = {}
        cls._global_vendor_credits_used = 0
        cls._global_out_of_credit = False
        cls._global_attribution_failed = False
        cls._funded_context = None
        cls._wave1_executed = False
        cls._wave1_unique_count = 0
        cls._wave1_frames_by_market = {}
        cls._wave1_source_value_results = {}

    @classmethod
    def configure_funded_run(cls, context: FundedSocialCrawlContext) -> None:
        if not isinstance(context, FundedSocialCrawlContext):
            raise ValueError("funded SocialCrawl context is invalid")
        cls._funded_context = context

    @classmethod
    def credits_used(cls) -> int:
        return cls._global_credits_used

    @classmethod
    def credits_used_for(cls, market: str) -> int:
        """This market's own spend, not the running total."""
        return cls._global_credits_by_market.get(market, 0)

    @classmethod
    def usage_for(cls, market: str, phase: str) -> SocialCrawlPhaseUsage:
        if market not in MARKET_REGION:
            raise ValueError("market is unsupported")
        if phase not in PHASE_ORDER:
            raise ValueError("phase is unsupported")
        key = (market, phase)
        return SocialCrawlPhaseUsage(
            market=market,
            phase=phase,
            calls=cls._global_calls_by_market_phase.get(key, 0),
            budget_debit_credits=cls._global_budget_by_market_phase.get(key, 0),
            vendor_reported_credits=cls._global_vendor_by_market_phase.get(key, 0),
        )

    @classmethod
    def phase_usages(cls) -> tuple[SocialCrawlPhaseUsage, ...]:
        return tuple(
            cls.usage_for(market, phase) for market in ("za", "ng", "ke") for phase in PHASE_ORDER
        )

    @classmethod
    def wave1_unique_observations(cls) -> int:
        return cls._wave1_unique_count

    @classmethod
    def wave1_source_values(cls):
        return MappingProxyType(dict(cls._wave1_source_value_results))

    @classmethod
    def measure_wave1_source_values(cls, rows, exercised_routes=None):
        from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

        # A route the seeds could not fill made no call and measured nothing; it
        # stays inventory-only rather than rejected (attempt 17, 4 Sep 2026, when
        # every unfilled route counted as zero observations and blocked the close).
        measured_routes = (
            tuple(WAVE1_ROUTE_SPECS)
            if exercised_routes is None
            else tuple(route for route in WAVE1_ROUTE_SPECS if route in set(exercised_routes))
        )
        ordered = tuple(
            sorted(
                (dict(row) for row in rows),
                key=lambda row: (
                    str(row.get("endpoint")),
                    str(row.get("market")),
                    str(row.get("native_id")),
                    str(row.get("url")),
                ),
            )
        )

        def candidate_keys(values):
            return {
                (str(row.get("market")), str(row.get("query_term")))
                for row in values
                if isinstance(row.get("query_term"), str) and row.get("query_term")
            }

        def evidence_keys(values):
            return {
                (str(row.get("market")), str(row.get("native_id")), str(row.get("url")))
                for row in values
            }

        full_candidates = candidate_keys(ordered)
        full_evidence = evidence_keys(ordered)
        results = {}
        for route in measured_routes:
            route_rows = tuple(row for row in ordered if row.get("endpoint") == route)
            control = tuple(row for row in ordered if row.get("endpoint") != route)
            results[f"/v1/{route}"] = evaluate_wave1_source_value(
                unique_observations=len(evidence_keys(route_rows)),
                marginal_candidates=len(full_candidates - candidate_keys(control)),
                marginal_evidence=len(full_evidence - evidence_keys(control)),
            )
        return MappingProxyType(results)

    # -- budget ----------------------------------------------------------

    def _phase_ceiling(self, phase: str) -> int:
        share = float(self._phase_share.get(phase, DEFAULT_PHASE_SHARE.get(phase, 0.0)))
        return int(self._budget * share)

    def _can_spend(self, phase: str, cost: int) -> bool:
        """True when `cost` fits both the run budget and this phase's share."""
        if SocialCrawlConnector._global_out_of_credit:
            return False
        if SocialCrawlConnector._global_credits_used + cost > self._budget:
            return False
        spent = SocialCrawlConnector._global_credits_by_phase.get(phase, 0)
        return spent + cost <= self._phase_ceiling(phase)

    def _record_call(self, phase: str) -> None:
        key = (self.market, phase)
        SocialCrawlConnector._global_calls += 1
        SocialCrawlConnector._global_calls_by_market_phase[key] = (
            SocialCrawlConnector._global_calls_by_market_phase.get(key, 0) + 1
        )

    def _charge(self, phase: str, budget_debit: int, vendor_reported: int = 0) -> None:
        if vendor_reported > budget_debit:
            budget_debit = vendor_reported
        key = (self.market, phase)
        SocialCrawlConnector._global_credits_used += budget_debit
        SocialCrawlConnector._global_vendor_credits_used += vendor_reported
        SocialCrawlConnector._global_credits_by_phase[phase] = (
            SocialCrawlConnector._global_credits_by_phase.get(phase, 0) + budget_debit
        )
        SocialCrawlConnector._global_credits_by_market[self.market] = (
            SocialCrawlConnector._global_credits_by_market.get(self.market, 0) + budget_debit
        )
        SocialCrawlConnector._global_budget_by_market_phase[key] = (
            SocialCrawlConnector._global_budget_by_market_phase.get(key, 0) + budget_debit
        )
        SocialCrawlConnector._global_vendor_by_market_phase[key] = (
            SocialCrawlConnector._global_vendor_by_market_phase.get(key, 0) + vendor_reported
        )

    # -- transport -------------------------------------------------------

    def _call(
        self,
        session: requests.Session,
        surface: str,
        params: dict[str, str],
        phase: str,
    ) -> dict[str, Any] | None:
        """One GET. Returns the parsed payload, or None on any failure.

        Charges the ledger with max(quoted price, reported credits_used) so an
        omitted or zero credits_used still advances the budget and the breaker
        cannot become a no-op. Cache hits and refunds report 0 and are charged
        0 -- the vendor does not bill them, so neither do we.
        """
        if surface in WAVE1_ROUTE_SPECS:
            from src.analysis.open_intelligence.funded_lane import (
                _require_wave1_execution_capability,
            )

            supplied = (
                self._funded_context.execution_capability
                if self._funded_context is not None
                else None
            )
            _require_wave1_execution_capability(supplied, action=f"socialcrawl:{surface}")
            raise ValueError("Wave 1 routes require the dedicated Wave 1 runner")
        quoted = CREDIT_COST.get(surface, DEFAULT_CREDIT_COST)
        if not self._can_spend(phase, quoted):
            return None

        url = f"{BASE_URL}/{surface}"
        context = SocialCrawlConnector._funded_context
        if context is not None:
            context.pre_call_authority_hook(SocialCrawlConnector._global_credits_used + quoted)
        try:
            resp = session.get(url, params=params, timeout=self._timeout)
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            self._record_call(phase)
            self.logger.warning("socialcrawl %s failed: %s", surface, str(exc)[:200])
            self._fetch_failures.append(f"socialcrawl {surface}: {str(exc)[:150]}")
            # A transport failure is never billed by the vendor, but it does
            # consume wall clock; charge the quote so a dead endpoint cannot
            # spin the whole budget's worth of retries.
            self._charge(phase, quoted, 0)
            return None

        self._record_call(phase)
        reported = max(int(payload.get("credits_used", 0) or 0), 0)

        if payload.get("success") is False:
            err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            err_type = str(err.get("type") or "API_ERROR")
            if err_type == "INSUFFICIENT_CREDITS":
                # Account is dry. Stop the entire run, all markets, now.
                SocialCrawlConnector._global_out_of_credit = True
                self.logger.error("socialcrawl account out of credits, halting all surfaces")
                self._fetch_failures.append("socialcrawl: INSUFFICIENT_CREDITS, run halted")
                return None
            ident = self._ident(params)
            named = f"{surface} {ident}" if ident else surface
            self.logger.warning(
                "socialcrawl %s %s: %s", named, err_type, str(err.get("message"))[:150]
            )
            self._fetch_failures.append(f"socialcrawl {named}: {err_type}")
            self._charge(phase, reported, reported)
            return None

        self._charge(phase, reported, reported)
        return payload

    def _validate_wave1_plan(self) -> tuple[Wave1RouteRequest, ...]:
        from src.analysis.open_intelligence.funded_lane import (
            WAVE1_PHASE_CAPS,
            WAVE1_STAGE,
            _require_wave1_execution_capability,
        )

        context = SocialCrawlConnector._funded_context
        if context is None or context.stage_name != WAVE1_STAGE:
            raise ValueError("Wave 1 runner requires the Wave 1 stage context")
        capability = _require_wave1_execution_capability(
            context.execution_capability, action="wave1_dedicated_runner"
        )
        if getattr(capability, "route_set_sha256", None) != WAVE1_ROUTE_SET_SHA256:
            raise ValueError("Wave 1 route is blocked")
        requests = context.wave1_requests
        routes = frozenset(item.route for item in requests)
        frozen_routes = frozenset(WAVE1_ROUTE_SPECS)
        # A seed list the released run cannot fill is empty, not invalid (4 Sep
        # 2026: r16 carried no TikTok music seed and no reddit evidence, and the
        # twelfth pilot made zero calls because this check demanded every
        # route). The plan may omit a frozen route; it may not add one, and it
        # may not be empty. Every phase still closes, with zero calls where no
        # route ran.
        if not requests or not routes <= frozen_routes:
            raise ValueError("Wave 1 runner requires a non-empty subset of the frozen routes")
        route_calls: dict[str, int] = {}
        route_debits: dict[str, int] = {}
        phase_debits = dict.fromkeys(WAVE1_PHASE_CAPS, 0)
        qualification_seen: set[str] = set()
        ingestion_routes = {"instagram/music/trending", "youtube/shorts/trending"}
        total_debit = 0
        for request in requests:
            spec = WAVE1_ROUTE_SPECS.get(request.route)
            if spec is None:
                raise ValueError("Wave 1 route is unsupported")
            if tuple(sorted(request.params)) != tuple(sorted(spec.required_parameters)):
                raise ValueError("Wave 1 parameters are invalid")
            if request.page > spec.maximum_pages:
                raise ValueError("Wave 1 page limit exceeded")
            if request.call_role == "qualification":
                qualification_seen.add(request.route)
            elif request.route not in ingestion_routes or request.route not in qualification_seen:
                raise ValueError("Wave 1 call role is invalid")
            route_calls[request.route] = route_calls.get(request.route, 0) + 1
            route_debits[request.route] = route_debits.get(request.route, 0) + spec.credit_cost
            phase_debits[spec.phase] += spec.credit_cost
            total_debit += spec.credit_cost
            if route_calls[request.route] > spec.maximum_calls:
                raise ValueError("Wave 1 route call limit exceeded")
            if route_debits[request.route] > spec.maximum_debit:
                raise ValueError("Wave 1 route debit limit exceeded")
            if phase_debits[spec.phase] > WAVE1_PHASE_CAPS[spec.phase]:
                raise ValueError("Wave 1 phase debit limit exceeded")
            if total_debit > 63:
                raise ValueError("Wave 1 execution debit limit exceeded")
        return requests

    def _run_wave1_routes(self, session: requests.Session) -> tuple[dict[str, Any], ...]:
        from src.analysis.open_intelligence.candidates import adapt_wave1_evidence_rows
        from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS
        from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage

        plan = self._validate_wave1_plan()
        context = SocialCrawlConnector._funded_context
        if context is None:
            raise ValueError("Wave 1 context is unavailable")
        phase_calls = dict.fromkeys(WAVE1_PHASE_CAPS, 0)
        phase_budget = dict.fromkeys(WAVE1_PHASE_CAPS, 0)
        phase_quoted = dict.fromkeys(WAVE1_PHASE_CAPS, 0)
        phase_vendor = dict.fromkeys(WAVE1_PHASE_CAPS, 0)
        route_budget = dict.fromkeys(WAVE1_ROUTE_SPECS, 0)
        admitted = []
        adapted = ()
        try:
            queue = list(plan)
            initial_routes = {request.route for request in plan}
            index = 0

            def enqueue_chain(route: str, parameter: str, extract, items, market: str | None) -> None:
                spec = WAVE1_ROUTE_SPECS[route]
                remaining = queue[index:]
                reserved_route = sum(
                    WAVE1_ROUTE_SPECS[item.route].credit_cost
                    for item in remaining
                    if item.route == route
                )
                reserved_phase = sum(
                    WAVE1_ROUTE_SPECS[item.route].credit_cost
                    for item in remaining
                    if WAVE1_ROUTE_SPECS[item.route].phase == spec.phase
                )
                reserved_total = sum(
                    WAVE1_ROUTE_SPECS[item.route].credit_cost for item in remaining
                )
                slots = min(
                    5,
                    spec.maximum_calls
                    - route_budget[route] // spec.credit_cost
                    - sum(item.route == route for item in remaining),
                    (spec.maximum_debit - route_budget[route] - reserved_route)
                    // spec.credit_cost,
                    (WAVE1_PHASE_CAPS[spec.phase] - phase_budget[spec.phase] - reserved_phase)
                    // spec.credit_cost,
                    (63 - sum(phase_budget.values()) - reserved_total) // spec.credit_cost,
                )
                if slots > 0:
                    queue.extend(
                        Wave1RouteRequest(
                            route,
                            {parameter: value},
                            "qualification",
                            market=market,
                        )
                        for value in _wave1_chain_values(items, extract, limit=slots)
                    )

            while index < len(queue):
                request = queue[index]
                index += 1
                spec = WAVE1_ROUTE_SPECS[request.route]
                total_budget = sum(phase_quoted.values())
                if spec.credit_cost > spec.maximum_debit - route_budget[request.route]:
                    raise ValueError("Wave 1 route debit authority is insufficient")
                if spec.credit_cost > WAVE1_PHASE_CAPS[spec.phase] - phase_budget[spec.phase]:
                    raise ValueError("Wave 1 phase debit authority is insufficient")
                if spec.credit_cost > 63 - total_budget:
                    raise ValueError("Wave 1 execution debit authority is insufficient")
                context.pre_call_authority_hook(total_budget + spec.credit_cost)
                phase_calls[spec.phase] += 1
                phase_budget[spec.phase] += spec.credit_cost
                phase_quoted[spec.phase] += spec.credit_cost
                route_budget[request.route] += spec.credit_cost
                SocialCrawlConnector._global_calls += 1
                SocialCrawlConnector._global_credits_used += spec.credit_cost
                SocialCrawlConnector._global_credits_by_phase[spec.phase] = (
                    SocialCrawlConnector._global_credits_by_phase.get(spec.phase, 0)
                    + spec.credit_cost
                )
                response = session.get(
                    f"{BASE_URL}/{request.route}",
                    params=dict(request.params),
                    timeout=self._timeout,
                    stream=True,
                )
                payload = _read_wave1_payload(response)
                if not isinstance(payload, Mapping):
                    raise ValueError("Wave 1 response is invalid")
                reported = payload.get("credits_used", 0)
                if isinstance(reported, bool) or not isinstance(reported, int) or reported < 0:
                    raise ValueError("Wave 1 reported debit is invalid")
                delta = max(0, reported - spec.credit_cost)
                route_budget[request.route] += delta
                phase_budget[spec.phase] += delta
                phase_vendor[spec.phase] += reported
                SocialCrawlConnector._global_credits_used += delta
                SocialCrawlConnector._global_vendor_credits_used += reported
                SocialCrawlConnector._global_credits_by_phase[spec.phase] += delta
                if delta:
                    raise ValueError("Wave 1 vendor debit exceeds quoted authority")
                if route_budget[request.route] > spec.maximum_debit:
                    raise ValueError("Wave 1 reported route debit exceeds its maximum")
                if phase_budget[spec.phase] > WAVE1_PHASE_CAPS[spec.phase]:
                    raise ValueError("Wave 1 reported phase debit exceeds its maximum")
                if sum(phase_budget.values()) > 63:
                    raise ValueError("Wave 1 reported execution debit exceeds its maximum")
                if payload.get("success") is not True:
                    raise ValueError("Wave 1 response is invalid")
                data = payload.get("data")
                items = data.get("items", ()) if isinstance(data, Mapping) else ()
                if not isinstance(items, (tuple, list)):
                    raise ValueError("Wave 1 response rows are invalid")
                if len(items) > WAVE1_RESPONSE_MAX_ITEMS:
                    raise ValueError("Wave 1 response item ceiling exceeded")
                routed_items = tuple(
                    row
                    for row in (self._wave1_row_from_item(item, request, context) for item in items)
                    if row is not None
                )
                # A seeded call carries its market and its rows are evidence; an
                # ingestion call on a global surface is admitted as before and the
                # adapter places what it can.
                if request.call_role == "ingestion" or request.market is not None:
                    admitted.extend(routed_items)
                    if (
                        request.route == "instagram/music/trending"
                        and "instagram/audio/reels" not in initial_routes
                    ):
                        # The vendor nests the id under post.ext.music_id, and a page
                        # without one is a short chain, not a failed run (attempt 17,
                        # 4 Sep 2026, when a raise here dropped every admitted row).
                        enqueue_chain(
                            "instagram/audio/reels", "audio_id", _wave1_audio_id, items, request.market
                        )
                    if (
                        request.route == "youtube/shorts/trending"
                        and "youtube/video/comments" not in initial_routes
                    ):
                        enqueue_chain(
                            "youtube/video/comments", "url", _wave1_item_url, items, request.market
                        )
            total_budget = sum(phase_budget.values())
            if total_budget > 63 or any(
                phase_budget[phase] > cap for phase, cap in WAVE1_PHASE_CAPS.items()
            ):
                raise ValueError("Wave 1 reported debit exceeds execution authority")
            adapted = adapt_wave1_evidence_rows(admitted)
            return adapted
        finally:
            SocialCrawlConnector._wave1_unique_count = len(adapted)
            exercised = tuple(route for route, debit in route_budget.items() if debit > 0)
            SocialCrawlConnector._wave1_source_value_results = dict(
                SocialCrawlConnector.measure_wave1_source_values(adapted, exercised)
            )
            for phase in WAVE1_PHASE_CAPS:
                context.phase_close_hook(
                    Wave1PhaseUsage(
                        market=None,
                        phase=phase,
                        calls=phase_calls[phase],
                        budget_debit_credits=phase_budget[phase],
                        vendor_reported_credits=phase_vendor[phase],
                        authorized_quoted_debit=phase_quoted[phase],
                    )
                )

    @classmethod
    def _wave1_row_from_item(cls, item, request, context):
        """One vendor item as the engine row the Wave 1 adapter places.

        A row that already carries the engine shape (row_id and native_id at the
        root) passes through with its endpoint stamped. A vendor item is a
        {post} or {comment} wrapper (live-probed against the vendor OpenAPI, 4 Sep
        2026); it becomes a row only when the call was seeded, because the seed's
        market is the retained geography and the receipt names the seed.
        """
        if not isinstance(item, Mapping):
            return None
        if "row_id" in item and "native_id" in item:
            return {**dict(item), "endpoint": request.route}
        node = None
        content_type = None
        for envelope, kind in (("post", "post"), ("comment", "comment")):
            candidate = item.get(envelope)
            if isinstance(candidate, Mapping):
                node, content_type = candidate, kind
                break
        if node is None:
            return None
        native_id = node.get("id")
        if isinstance(native_id, int) and not isinstance(native_id, bool):
            native_id = str(native_id)
        if not isinstance(native_id, str) or not native_id:
            return None
        url = node.get("url")
        if (not isinstance(url, str) or not url) and content_type == "comment":
            # A comment on a seeded post carries no permalink of its own on every
            # surface (attempt 18, 4 Sep 2026: five YouTube comment calls, four
            # reported, zero rows). The seed URL plus the comment id is its locator.
            seed_url = request.params.get("url")
            if isinstance(seed_url, str) and seed_url:
                url = f"{seed_url}#comment-{native_id}"
        if not isinstance(url, str) or not url:
            return None
        platform = request.route.split("/", 1)[0]
        author = node.get("author") if isinstance(node.get("author"), Mapping) else {}
        content = node.get("content") if isinstance(node.get("content"), Mapping) else {}
        engagement = node.get("engagement") if isinstance(node.get("engagement"), Mapping) else {}
        handle = str(author.get("username") or "")
        name = str(author.get("display_name") or author.get("name") or handle)
        text = str(content.get("text") or node.get("text") or node.get("title") or "")
        seed_value = "|".join(f"{key}={request.params[key]}" for key in sorted(request.params))
        run_id = getattr(getattr(context, "execution_capability", None), "run_id", None)
        receipt_source = "|".join(
            (str(run_id or ""), request.route, seed_value, str(request.market or ""))
        )
        row = {
            "source": cls.SOURCE_NAME,
            "platform": platform,
            "market": request.market or "",
            "content_type": f"{platform}/{content_type}",
            "query_group": "wave1",
            "query_term": seed_value,
            "author_name": name,
            "author_handle": handle,
            "title": text[:200],
            "text": text,
            "url": url,
            "published_at": cls._parse_published(node.get("published_at")),
            "views": cls._as_float(engagement.get("views")),
            "likes": cls._as_float(engagement.get("likes")),
            "comments": cls._as_float(engagement.get("comments")),
            "shares": cls._as_float(engagement.get("shares")),
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2gcam": "",
            "row_id": "wave1_"
            + hashlib.sha256(f"{request.route}|{platform}|{native_id}".encode()).hexdigest()[:32],
            "native_id": native_id,
            "endpoint": request.route,
            "vendor_market": None,
            "retained_geo_market": request.market,
            "geo_method_id": "wave1_seed_market_v1" if request.market else None,
            "geo_receipt_id": (
                "geo_" + hashlib.sha256(receipt_source.encode()).hexdigest()[:32]
                if request.market
                else None
            ),
        }
        return row

    @staticmethod
    def _wave1_handoff_frame(rows) -> pd.DataFrame:
        frame = pd.DataFrame(tuple(rows))
        if frame.empty:
            return pd.DataFrame(columns=[*RAW_COLUMNS, *WAVE1_AUTHORITY_COLUMNS])
        missing = [field for field in WAVE1_AUTHORITY_COLUMNS if field not in frame.columns]
        if missing or frame[list(WAVE1_AUTHORITY_COLUMNS)].isna().any().any():
            raise ValueError("Wave 1 authority handoff is incomplete")
        return frame.reindex(columns=[*RAW_COLUMNS, *WAVE1_AUTHORITY_COLUMNS], fill_value="")

    def _call_paged(
        self,
        session: requests.Session,
        surface: str,
        params: dict[str, str],
        phase: str,
        pages: int,
    ) -> list[dict[str, Any]]:
        """Up to `pages` pages of one surface, deduplicated, in page order.

        LIVE-PROBED 27 Jul 2026. tiktok/search/top, youtube/search,
        reddit/subreddit and facebook/profile/posts share one contract:
        `data.next_cursor` carries the raw upstream token, and an envelope-level
        `pagination` block carries a vendor-wrapped `next_cursor` plus an
        explicit `has_more`. Every page bills one credit.

        Send back `data.next_cursor`, NOT the wrapped one. On TikTok both forms
        work, which is what the first cut of this code was written against, but
        on facebook/profile/posts the wrapped `sc.`-prefixed token returns an
        EMPTY item list while the raw token pages correctly. A preference for
        the wrapped form therefore reads as "Facebook has only one page" and
        loses every row after the third. `has_more` is still read off the
        wrapped block, since only it carries that flag.

        Two traps. The cursor is an OPAQUE STRING on all three surfaces even
        though the docs type TikTok's as an integer, so anything that coerces it
        re-requests page one forever. And pages overlap: the live TikTok probe
        repeated three of thirty rows on page two, so dedupe is not optional.

        pages <= 1 sends no cursor at all, which is the pre-pagination
        behaviour byte for byte.
        """
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str = ""

        for _ in range(max(1, pages)):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._call(session, surface, page_params, phase)
            # None is a spent budget, a transport failure or a vendor error.
            # Whatever pages already landed are still good rows.
            if not payload:
                break

            for item in self._extract_items(payload):
                key = self._item_key(item)
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                items.append(item)

            pagination = payload.get("pagination")
            pagination = pagination if isinstance(pagination, dict) else {}
            if pagination.get("has_more") is False:
                break
            data = payload.get("data")
            data = data if isinstance(data, dict) else {}
            cursor = str(data.get("next_cursor") or pagination.get("next_cursor") or "")
            if not cursor:
                break

        return items

    @staticmethod
    def _ident(params: dict[str, str]) -> str:
        """The handle, id or term a call was made for, or "" if it has none.

        A vendor error naming only the surface cannot be traced back to the
        entry that caused it: the 00:30 cron on 24 Aug 2026 logged 29
        RESOURCE_NOT_FOUND lines across three creator surfaces and named no
        handle, so the dead watchlist entries could not be pruned without
        re-probing every one. The key differs per surface, so take the first
        present and ignore the shape params (region, trim, cursor, depth)
        which say nothing about which entry died.
        """
        for key in (
            "handle",
            "username",
            "user_id",
            "subreddit",
            "pageId",
            "group_id",
            "id",
            "query",
            "q",
            "keyword",
            "url",
        ):
            value = params.get(key)
            if value:
                return str(value)[:120]
        return ""

    def _note_empty(self, surface: str, ident: str) -> None:
        """Record a curated id that came back with nothing.

        The vendor answers a page or group that no longer exists with
        `success: true, items: [], credits_used: 0`, which is byte-identical to
        a genuinely quiet source. A keyword search returning nothing is a normal
        day; a page, group or subreddit we NAME in config is an entity that is
        supposed to exist, so zero rows from one is a dead id.

        That distinction is not academic: 15 of 30 configured subreddits were
        dead for weeks behind exactly this silence, and the Facebook phase now
        depends on twelve hand-curated page ids that can be renamed or deleted
        without warning.
        """
        self.logger.warning("socialcrawl %s returned no rows for %s", surface, ident)
        self._fetch_failures.append(f"socialcrawl {surface}: no rows for {ident}")

    # -- parsing ---------------------------------------------------------

    @staticmethod
    def _item_key(item: dict[str, Any]) -> str:
        """Stable identity for one item, used to drop cross-page repeats."""
        for envelope in ("post", "article"):
            node = item.get(envelope)
            if isinstance(node, dict):
                return str(node.get("id") or node.get("url") or "")
        return str(item.get("id") or item.get("url") or "")

    @staticmethod
    def _extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "posts", "results", "videos", "articles", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    @staticmethod
    def _parse_published(raw: Any) -> str:
        """Normalise a vendor timestamp to ISO 8601, or "" when unparseable.

        Handles the three shapes seen live: ISO ("2026-07-23T12:55:16.000Z"),
        DataForSEO's space form ("2026-07-23 10:06:18 +00:00"), and Twitter's
        ("Wed Oct 30 05:45:03 +0000 2019").
        """
        if not raw:
            return ""
        text = str(raw).strip()
        try:
            return datetime.strptime(text, TWITTER_DATE_FORMAT).isoformat()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _too_old(self, published_iso: str) -> bool:
        """True when the post predates the recency window.

        An unparseable or absent date is NOT treated as old: several surfaces
        legitimately omit it and dropping them would silently gut the feed.
        """
        if not published_iso or self._max_age_days <= 0:
            return False
        try:
            when = datetime.fromisoformat(published_iso)
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when < datetime.now(UTC) - timedelta(days=self._max_age_days)

    def _row_from_post(
        self,
        item: dict[str, Any],
        platform: str,
        term: str,
        query_group: str,
        content_type: str,
        fallback_handle: str = "",
    ) -> dict[str, Any] | None:
        post = item.get("post") if isinstance(item.get("post"), dict) else item
        if not isinstance(post, dict):
            return None
        author = post.get("author") if isinstance(post.get("author"), dict) else {}
        content = post.get("content") if isinstance(post.get("content"), dict) else {}
        eng = post.get("engagement") if isinstance(post.get("engagement"), dict) else {}

        published = self._parse_published(post.get("published_at"))
        if self._too_old(published):
            return None

        # twitter/user/tweets and youtube trending both leave username null;
        # the requested handle is the only truthful answer we have.
        handle = str(author.get("username") or fallback_handle or "")
        name = str(author.get("display_name") or author.get("name") or handle)
        text = str(content.get("text") or post.get("text") or post.get("title") or "")
        if not text and not post.get("url"):
            return None

        return {
            "source": self.SOURCE_NAME,
            # Real platform (tiktok/instagram/...), not the literal
            # "socialcrawl", so downstream platform-diversity scoring, the
            # seed_graph channel map, and the creator watchlist all treat
            # these rows exactly as they treated the EnsembleData ones.
            "platform": platform,
            "market": self.market,
            "content_type": content_type,
            "query_group": query_group,
            "query_term": term,
            "author_name": name,
            "author_handle": handle,
            "title": text[:200],
            "text": text,
            "url": str(post.get("url") or ""),
            "published_at": published,
            "views": self._as_float(eng.get("views")),
            "likes": self._as_float(eng.get("likes")),
            "comments": self._as_float(eng.get("comments")),
            "shares": self._as_float(eng.get("shares")),
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2gcam": "",
        }

    def _row_from_article(self, item: dict[str, Any], term: str) -> dict[str, Any] | None:
        """google_news items nest under `article`, not `post`."""
        art = item.get("article") if isinstance(item.get("article"), dict) else item
        if not isinstance(art, dict):
            return None
        published = self._parse_published(art.get("published_at"))
        if self._too_old(published):
            return None
        title = str(art.get("title") or "")
        snippet = str(art.get("snippet") or "")
        if not title:
            return None
        domain = str(art.get("domain") or art.get("source") or "")
        return {
            "source": self.SOURCE_NAME,
            "platform": "news",
            "market": self.market,
            "content_type": "news/socialcrawl",
            "query_group": SOCIALCRAWL_QUERY_GROUP,
            "query_term": term,
            "author_name": domain,
            "author_handle": domain,
            "title": title[:200],
            "text": f"{title}. {snippet}".strip(),
            "url": str(art.get("url") or ""),
            "published_at": published,
            "views": 0.0,
            "likes": 0.0,
            "comments": 0.0,
            "shares": 0.0,
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2gcam": "",
        }

    # -- fetch -----------------------------------------------------------

    def fetch(self, **kwargs: Any) -> pd.DataFrame:
        sources = load_sources()
        config = sources.get("socialcrawl", {}) or {}
        if not config.get("enabled", False):
            return self.empty_dataframe()

        selector = os.environ.get("SOCIALCRAWL_CREDENTIAL_LANE", "jhb_core").strip()
        if selector not in {"jhb_core", "ogilvy_funded"}:
            self._fetch_failures.append("socialcrawl: credential lane is unsupported")
            return self.empty_dataframe()
        funded = selector == "ogilvy_funded"
        context = SocialCrawlConnector._funded_context
        if funded:
            if os.environ.get("TRENDS_ENV") != "staging":
                self._fetch_failures.append("socialcrawl: funded lane is staging only")
                return self.empty_dataframe()
            if context is None:
                self._fetch_failures.append("socialcrawl: funded lane preflight is missing")
                return self.empty_dataframe()
            if SocialCrawlConnector._global_attribution_failed:
                self._fetch_failures.append("socialcrawl: funded lane attribution failed")
                return self.empty_dataframe()
            if context.stage_name == "stage_1_wave_1":
                from src.analysis.open_intelligence.funded_lane import (
                    _require_wave1_execution_capability,
                )

                _require_wave1_execution_capability(
                    context.execution_capability, action="socialcrawl_secret_read"
                )
            secret_id = context.secret_id
        else:
            if os.environ.get("TRENDS_ENV") == "staging":
                self.logger.error(STAGING_UNFUNDED_REFUSAL)
                self._fetch_failures.append(STAGING_UNFUNDED_REFUSAL)
                return self.empty_dataframe()
            secret_id = "SOCIALCRAWL_API_KEY"

        api_key = (get_secret(secret_id) or os.environ.get(secret_id, "")).strip()
        if not api_key:
            self.logger.warning("%s missing, socialcrawl skipped for %s", secret_id, self.market)
            self._fetch_failures.append(f"socialcrawl: {secret_id} missing")
            return self.empty_dataframe()

        market_cfg = (config.get("markets", {}) or {}).get(self.market, {}) or {}
        self._budget = (
            context.run_allowance
            if funded
            else int(config.get("budget_credits_per_run", DEFAULT_BUDGET_CREDITS_PER_RUN))
        )
        self._phase_share = dict(DEFAULT_PHASE_SHARE)
        self._phase_share.update(config.get("phase_share", {}) or {})
        self._timeout = int(config.get("timeout", DEFAULT_TIMEOUT))
        self._max_age_days = int(config.get("max_age_days", DEFAULT_MAX_AGE_DAYS))
        caps = config.get("caps", {}) or {}
        phases_on = set(config.get("phases", PHASE_ORDER) or PHASE_ORDER)

        session = requests.Session()
        session.headers.update({"x-api-key": api_key})

        if funded and context.stage_name == "stage_1_wave_1":
            if SocialCrawlConnector._wave1_executed:
                return SocialCrawlConnector._wave1_frames_by_market.get(
                    self.market,
                    self._wave1_handoff_frame(()),
                ).copy()
            SocialCrawlConnector._wave1_executed = True
            wave1_rows = self._run_wave1_routes(session)
            frame = self._wave1_handoff_frame(wave1_rows)
            SocialCrawlConnector._wave1_frames_by_market = {
                market: frame.loc[frame["market"] == market].reset_index(drop=True)
                for market in MARKET_REGION
            }
            return SocialCrawlConnector._wave1_frames_by_market[self.market].copy()

        rows: list[dict[str, Any]] = []
        runners = {
            "discover": self._phase_discover,
            "creators": self._phase_creators,
            "search": self._phase_search,
            "reddit": self._phase_reddit,
            "accounts": self._phase_accounts,
            "news": self._phase_news,
            "facebook": self._phase_facebook,
        }
        for phase in PHASE_ORDER:
            if phase not in phases_on:
                continue
            if SocialCrawlConnector._global_out_of_credit:
                break
            before = len(rows)
            runners[phase](session, market_cfg, caps, rows)
            if funded:
                try:
                    context.phase_close_hook(SocialCrawlConnector.usage_for(self.market, phase))
                except Exception:
                    SocialCrawlConnector._global_attribution_failed = True
                    self._fetch_failures.append("socialcrawl: funded lane attribution failed")
                    raise
            self.logger.info(
                "socialcrawl %s phase=%s rows=%d credits=%d/%d",
                self.market,
                phase,
                len(rows) - before,
                SocialCrawlConnector._global_credits_used,
                self._budget,
            )

        if not rows:
            return self.empty_dataframe()
        df = pd.DataFrame(rows)
        df["market"] = self.market
        return df.reindex(columns=list(RAW_COLUMNS), fill_value="")

    # -- phases ----------------------------------------------------------

    def _phase_discover(self, session, market_cfg, caps, rows) -> None:
        """Per-country trending. The surface EnsembleData never had."""
        region = MARKET_REGION.get(self.market)
        if not region:
            return
        payload = self._call(
            session, "tiktok/trending", {"region": region, "trim": "true"}, "discover"
        )
        if payload:
            for item in self._extract_items(payload):
                row = self._row_from_post(
                    item, "tiktok", f"trending_{region}", "trending", "tiktok/trending"
                )
                if row:
                    rows.append(row)

        payload = self._call(
            session,
            "youtube/videos/trending",
            {"region": region, "max_results": str(int(caps.get("youtube_trending", 25)))},
            "discover",
        )
        if payload:
            for item in self._extract_items(payload):
                row = self._row_from_post(
                    item, "youtube", f"trending_{region}", "trending", "youtube/trending"
                )
                if row:
                    rows.append(row)

    def _creator_handles(self, tiers: list[str], per_platform_cap: int) -> dict[str, list[str]]:
        """Read configs/creators/<market>.yaml, one source of truth with ensemble.

        Returns {platform: [handle, ...]} for the requested tiers, in tier order
        so tier_1 always spends before tier_2 when the cap bites.
        """
        try:
            data = load_market_creators(self.market) or {}
        except (FileNotFoundError, OSError) as exc:
            self.logger.warning("socialcrawl creators config unreadable: %s", str(exc)[:150])
            self._fetch_failures.append(f"socialcrawl creators config: {str(exc)[:120]}")
            return {}
        out: dict[str, list[str]] = {}
        for platform, by_tier in (data.get("watchlists") or {}).items():
            handles: list[str] = []
            for tier in tiers:
                handles.extend(str(h) for h in ((by_tier or {}).get(tier) or []))
            if handles:
                out[platform] = handles[:per_platform_cap]
        return out

    def _phase_creators(self, session, market_cfg, caps, rows) -> None:
        """Watchlist handles. Sole source of the creator_watchlist signal."""
        tiers = list(market_cfg.get("creator_tiers", ["tier_1", "tier_2"]))
        creators = self._creator_handles(tiers, int(caps.get("creators_per_platform", 40)))
        # trim=true is a payload-size flag on every surface EXCEPT
        # instagram/profile/posts, where it returns success with zero items
        # (probed 23 Jul 2026: the same handle gives 12 posts untrimmed, 0
        # trimmed). Trimming does not change the price, so IG runs untrimmed.
        surfaces = {
            "tiktok": ("tiktok/profile/videos", "handle", True),
            "instagram": ("instagram/profile/posts", "handle", False),
            "threads": ("threads/user/posts", "handle", True),
        }
        tiktok_hits: list[str] = []
        for platform, handles in creators.items():
            surface = surfaces.get(platform)
            if surface is None:
                continue
            path, param, trim = surface
            for handle in list(handles or []):
                clean = str(handle).lstrip("@").strip()
                if not clean:
                    continue
                params = {param: clean}
                if trim:
                    params["trim"] = "true"
                payload = self._call(session, path, params, "creators")
                if payload is None:
                    if SocialCrawlConnector._global_out_of_credit:
                        return
                    continue
                produced = False
                for item in self._extract_items(payload):
                    row = self._row_from_post(
                        item,
                        platform,
                        clean,
                        "creator_watchlist",
                        f"{platform}/user_posts",
                        fallback_handle=clean,
                    )
                    if row:
                        rows.append(row)
                        produced = True
                if produced and platform == "tiktok":
                    tiktok_hits.append(clean)

        self._verify_creator_regions(session, tiktok_hits, caps, rows)

    def _verify_creator_regions(self, session, handles, caps, rows) -> None:
        """Stamp v2locations with the creator's VERIFIED country.

        tiktok/profile/region returns data.author.location, a real ISO country
        for a real account, for 1 credit. EnsembleData had no equivalent at any
        price: TikTok's own API exposes no geography, which is why the engine
        carries a regex plus blocklist geo defence and still leaked NG and KE
        collisions into ZA topics.

        This writes the answer into v2locations, the existing free-text
        location column, so no schema migration is needed and the geo-collision
        tooling can read a verified country instead of inferring one from slang.

        Cheap by construction: bounded by caps.geo_verify, and the vendor caches
        the lookup, so the same handle costs 1 credit the first day and 0 after.
        """
        limit = int(caps.get("geo_verify", 0))
        if limit <= 0 or not handles:
            return
        expected = MARKET_REGION.get(self.market, "")
        verified: dict[str, str] = {}
        for handle in handles[:limit]:
            payload = self._call(session, "tiktok/profile/region", {"handle": handle}, "creators")
            if payload is None:
                if SocialCrawlConnector._global_out_of_credit:
                    break
                continue
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            author = data.get("author") if isinstance(data.get("author"), dict) else {}
            location = str(author.get("location") or "").strip().upper()
            if location:
                verified[handle] = location

        if not verified:
            return
        mismatched = 0
        for row in rows:
            if row.get("platform") != "tiktok":
                continue
            location = verified.get(row.get("author_handle", ""))
            if not location:
                continue
            row["v2locations"] = location
            if expected and location != expected:
                mismatched += 1
        self.logger.info(
            "socialcrawl %s creator geo verified=%d mismatched_vs_%s=%d",
            self.market,
            len(verified),
            expected or "?",
            mismatched,
        )

    def _phase_search(self, session, market_cfg, caps, rows) -> None:
        """Per-market term pools across TikTok, YouTube, Threads."""
        region = MARKET_REGION.get(self.market, "")
        terms = list(market_cfg.get("terms", []) or [])
        max_terms = int(caps.get("search_terms", 8))

        for term in terms[:max_terms]:
            # tiktok/search/top + publish_time is the ONLY TikTok keyword
            # surface that returns fresh rows. tiktok/search returns 0 items
            # the moment date_posted or region is set (probed 23 Jul 2026).
            for item in self._call_paged(
                session,
                "tiktok/search/top",
                {"query": term, "publish_time": "this-week", "trim": "true"},
                "search",
                int(caps.get("tiktok_search_pages", 1)),
            ):
                row = self._row_from_post(
                    item, "tiktok", term, SOCIALCRAWL_QUERY_GROUP, "tiktok/search"
                )
                if row:
                    rows.append(row)

            for item in self._call_paged(
                session,
                "youtube/search",
                {"query": term, "uploadDate": "this_week", "region": region},
                "search",
                int(caps.get("youtube_search_pages", 1)),
            ):
                row = self._row_from_post(
                    item, "youtube", term, SOCIALCRAWL_QUERY_GROUP, "youtube/search"
                )
                if row:
                    rows.append(row)

        for term in terms[: int(caps.get("threads_terms", 5))]:
            payload = self._call(
                session, "threads/search", {"query": term, "trim": "true"}, "search"
            )
            if payload:
                for item in self._extract_items(payload):
                    row = self._row_from_post(
                        item, "threads", term, SOCIALCRAWL_QUERY_GROUP, "threads/search"
                    )
                    if row:
                        rows.append(row)

    def _phase_reddit(self, session, market_cfg, caps, rows) -> None:
        """Subreddit feeds plus keyword search, replacing the Ensemble Reddit connector.

        The retired EnsembleData connector pulled 1,657 rows/day by sweeping each
        subreddit under three sort modes; this one read a single page of `hot` and
        returned 419. Measured 27 Jul 2026 on r/southafrica and r/Nigeria, one
        credit per call either way:

            hot page 1      23 to 24 rows
            + rising        +0 unique, an IDENTICAL set to hot, pure waste
            + top/week      +13 to +17 unique
            + hot page 2    +23 to +24 unique

        So `rising` is dropped and the default sweep is hot (paged) then top/week.
        Sorts overlap by design, hence the per-subreddit dedupe: without it the
        same thread lands two or three times and inflates the row count.
        """
        subs = list(market_cfg.get("subreddits", []) or [])
        sort_modes = list(caps.get("reddit_sort_modes") or DEFAULT_REDDIT_SORTS)
        pages = int(caps.get("reddit_pages", 1))

        for sub in subs[: int(caps.get("subreddits", 6))]:
            seen: set[str] = set()
            before = len(self._fetch_failures)
            for mode in sort_modes:
                params = {"subreddit": sub, "trim": "true", **mode}
                # Paging only earns its credit on the primary sort; top/week is a
                # small fixed window and its second page repeats the first.
                mode_pages = pages if mode.get("sort") == "hot" else 1
                for item in self._call_paged(
                    session, "reddit/subreddit", params, "reddit", mode_pages
                ):
                    key = self._item_key(item)
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    row = self._row_from_post(
                        item, "reddit", f"r/{sub}", "reddit_sub", "reddit/subreddit"
                    )
                    if row:
                        rows.append(row)
            # Zero across EVERY sort mode is a dead community. Alive-on-one-sort
            # is normal: top/week is empty for any low-traffic week.
            if not seen and len(self._fetch_failures) == before:
                self._note_empty("reddit/subreddit", f"r/{sub}")

        for term in list(market_cfg.get("reddit_queries", []) or [])[
            : int(caps.get("reddit_queries", 4))
        ]:
            payload = self._call(
                session,
                "reddit/search",
                {"query": term, "sort": "new", "timeframe": "week", "trim": "true"},
                "reddit",
            )
            if payload:
                for item in self._extract_items(payload):
                    row = self._row_from_post(
                        item, "reddit", term, SOCIALCRAWL_QUERY_GROUP, "reddit/search"
                    )
                    if row:
                        rows.append(row)

    def _phase_facebook(self, session, market_cfg, caps, rows) -> None:
        """Curated Facebook pages and groups.

        Facebook has NO keyword post search at any price: `facebook/search` is
        not a resource, and the only search paths the vendor exposes are Ad
        Library, Marketplace and Events. Naming the pages and groups ourselves
        is the whole of the available strategy, which is why this phase is a
        config list rather than a term pool.

        LIVE-PROBED 27 Jul 2026. Three findings the spec does not tell you:
          - Pages answer THREE posts per call, so volume here is pagination,
            not breadth. eNCAnews page two was strictly older with zero overlap.
          - Groups take `group_id`, and sort_by=CHRONOLOGICAL is the freshness
            mode; RECENT_ACTIVITY returned posts days older. The `url` param
            rejected every form on 27 Jul but the vendor fixed it on 28 Jul and
            both vanity and numeric URLs now work, so either key is valid.
          - Page slugs are unsafe as identifiers. Of eight probed, two resolved
            to a hijacked or impostor profile serving 2015 and 2022 posts under
            the name we asked for. pageId is immutable and cannot silently
            resolve elsewhere, so config carries ids and `url` is discovery only.

        Zero-row and validation-error responses bill nothing, so a stale id in
        config costs coverage but never credits.
        """
        for page in list(market_cfg.get("facebook_pages", []) or [])[
            : int(caps.get("facebook_pages", 0))
        ]:
            key = "pageId" if str(page).isdigit() else "url"
            # A fault already reported by _call is not also an empty-id report:
            # one problem earns one line in the errors array, not two.
            before = len(self._fetch_failures)
            items = self._call_paged(
                session,
                "facebook/profile/posts",
                {key: str(page)},
                "facebook",
                int(caps.get("facebook_page_pages", 1)),
            )
            for item in items:
                # Pages leave author.username null and carry the masthead in
                # display_name only, so without the fallback the row lands with
                # no author and fails the usability bar downstream.
                row = self._row_from_post(
                    item,
                    "facebook",
                    str(page),
                    "facebook_page",
                    "facebook/profile/posts",
                    fallback_handle=str(page),
                )
                if row:
                    rows.append(row)
            if not items and len(self._fetch_failures) == before:
                self._note_empty("facebook/profile/posts", str(page))

        for group in list(market_cfg.get("facebook_groups", []) or [])[
            : int(caps.get("facebook_groups", 0))
        ]:
            before = len(self._fetch_failures)
            payload = self._call(
                session,
                "facebook/group/posts",
                {"group_id": str(group), "sort_by": "CHRONOLOGICAL"},
                "facebook",
            )
            got = 0
            if payload:
                for item in self._extract_items(payload):
                    got += 1
                    row = self._row_from_post(
                        item,
                        "facebook",
                        str(group),
                        "facebook_group",
                        "facebook/group/posts",
                        fallback_handle=str(group),
                    )
                    if row:
                        rows.append(row)
            if not got and len(self._fetch_failures) == before:
                self._note_empty("facebook/group/posts", str(group))

    def _phase_accounts(self, session, market_cfg, caps, rows) -> None:
        """Per-market X handles. Timeline is unsorted and long, so recency filtering
        in _row_from_post does the real work here."""
        for handle in list(market_cfg.get("twitter_handles", []) or [])[
            : int(caps.get("twitter_handles", 5))
        ]:
            clean = str(handle).lstrip("@").strip()
            if not clean:
                continue
            payload = self._call(
                session, "twitter/user/tweets", {"handle": clean, "trim": "true"}, "accounts"
            )
            if payload:
                for item in self._extract_items(payload):
                    row = self._row_from_post(
                        item,
                        "twitter",
                        clean,
                        "account_timeline",
                        "twitter/user_tweets",
                        fallback_handle=clean,
                    )
                    if row:
                        rows.append(row)

    def _phase_news(self, session, market_cfg, caps, rows) -> None:
        location = MARKET_LOCATION_NAME.get(self.market)
        if not location:
            return
        for term in list(market_cfg.get("news_queries", []) or [])[
            : int(caps.get("news_queries", 4))
        ]:
            payload = self._call(
                session,
                "google_news/search",
                {
                    "keyword": term,
                    "location_name": location,
                    "time_range": "day",
                    "depth": "10",
                },
                "news",
            )
            if payload:
                for item in self._extract_items(payload):
                    row = self._row_from_article(item, term)
                    if row:
                        rows.append(row)
