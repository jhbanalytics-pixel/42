"""Brand24 connector for the Business-tier analytics API.

Brand24's paid API exposes both aggregate / insights endpoints and a raw
mention stream. The surfaces this connector pulls are:

- ``/project/{pid}/topics`` — AI-clustered topic groups with description,
  mentions count, reach, and sentiment breakdown. Each topic becomes one
  enrichment-ready row with title + description + tone_avg.
- ``/project/{pid}/trending-hashtags`` — top hashtags with mentions_count,
  social_media_reach, and optional sentiment_score. Useful term-level
  signal that feeds the slang / regional scoring paths.
- ``/project/{pid}/most-followers`` — top voices ranked by audience size.
  Each row becomes a creator-shaped row that the watchlist scorer can
  match against.
- ``/project/{pid}/trending-links`` — top shared URLs with mentions_count.
  Platform inferred from the URL host.
- ``/project/{pid}/mentions/list`` — the raw mention stream. T-1 to T
  window so late-arriving rows still land. Hard-capped per project per
  run via ``MAX_MENTIONS_PER_PROJECT_PER_RUN`` so the shared 100K /
  month tier cap is never blown. Gated dark via ``include_mentions``
  config flag; live flip is a follow-up commit.
- ``/project/{pid}/ai-insights`` — Brand24's own narrative AI summary of
  the project window. Returns four string fields (headline, trends,
  insights, recommendations) over a configurable date window. One call
  per project per run, normalised into four rows (one per field) so the
  brief generator + email digest can surface the narrative without
  re-running Gemini. Live probe 28 May 2026 confirmed only the
  top-level ``/ai-insights`` path returns 200; sub-paths (topics /
  competitors / sentiment-trajectory / themes / summary / clusters)
  all 404. Gated dark via ``include_ai_insights`` config flag.

- ``/project/{pid}/mentions/sentiment`` — per-day sentiment breakdown for
  the project (total mentions, positive count, negative count per date).
  NOT per-mention scores; this is a daily aggregate surface. One row per
  (project, date) lands in raw_content with content_type
  ``brand24_mention_sentiment`` so the brief generator can read project-
  level sentiment trajectory. Gated dark via ``include_mentions_sentiment``.
- ``/project/{pid}/mentions/reach`` — per-day reach breakdown for the
  project (social_media_reach and non_social_media_reach per date).
  Again, aggregate not per-mention. One row per (project, date) with
  content_type ``brand24_mention_reach``. Gated dark via
  ``include_mentions_reach``.
- ``/project/{pid}/daily-metrics`` — single richest aggregate surface:
  one entry per date with mentions_count, reach_total, sentiment
  proportions, engagement (likes / comments / shares). Vendor returns
  ~31 days of history per call regardless of the requested window
  (verified by live probe 28 May 2026), so this is one vendor call per
  project per run with up to ~31 rows back. One row per (project, date)
  with content_type ``brand24_daily_metric``. Gated dark via
  ``include_daily_metrics``.

The three Wave 2 endpoints above are all project-aggregate surfaces, not
per-mention rows. Cap math: 3 endpoints x 3 projects x 1 vendor call /
project / day = 9 calls / day = 270 calls / month against the shared
Business-tier 100K mention cap. The vendor does not return usage hints in
responses (probed 28 May 2026); assumption is these aggregate endpoints
share the cap with mentions but each call is cheap, so headroom against
the ~85K / month projection stays comfortable. If the vendor later
exposes a usage header, surface it via the connector logger.

Projects are declared per market in ``configs/sources.yaml`` under
``brand24.{za,ng,ke}.projects`` as a list of dicts with at minimum ``id``.
Auth is an ``X-Api-Key`` header populated from ``BRAND24_API_KEY``. When
either is missing the connector returns an empty DataFrame so the rest of
the pipeline keeps running untouched.

Reference: https://api-data.brand24.com/api-data-docs/documentation
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import requests

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

BRAND24_BASE_URL = "https://api-data.brand24.com"
DEFAULT_LOOKBACK_DAYS = 7
REQUESTS_TIMEOUT_SECONDS = 30
MAX_TOPICS_PER_PROJECT = 20
MAX_HASHTAGS_PER_PROJECT = 25
MAX_FOLLOWERS_PER_PROJECT = 25
MAX_LINKS_PER_PROJECT = 25
# ai-insights returns four narrative fields per project per call. One call
# per project per day: 3 projects x 30 days = 90 calls / month against the
# shared 100K Business-tier cap, trivial. Lookback window mirrors the
# documented vendor default observed in the 28 May live probe (~31 days),
# overridable per project via sources.yaml `ai_insights_lookback_days`.
AI_INSIGHTS_LOOKBACK_DAYS = 7
# Narrative fields surfaced by the vendor as separate strings. Order
# preserved so the email digest reads headline -> trends -> insights ->
# recommendations top-to-bottom when grouped per project.
_AI_INSIGHT_FIELDS: tuple[str, ...] = (
    "headline",
    "trends",
    "insights",
    "recommendations",
)
# Wave 2 lookback windows. mentions/sentiment + mentions/reach honour the
# requested date range; daily-metrics does NOT (vendor returns ~31 days
# regardless of params, verified by 28 May 2026 live probe). All three
# default to a 7-day window which is what the brief generator + email
# digest tend to surface; overridable per-project via sources.yaml.
MENTION_SENTIMENT_LOOKBACK_DAYS = 7
MENTION_REACH_LOOKBACK_DAYS = 7
DAILY_METRICS_LOOKBACK_DAYS = 7
# Mentions stream cap per project per run, counted in KEPT rows (after the
# pipeline trims and dedups), not vendor-served mentions. sources.yaml sets
# mentions_max_per_run=600 per project, so the live budget is 3 projects x
# 600 x 30 days = ~54K / month, ~46K headroom on the shared Business-tier
# 100K cap. This module constant (800) is only the fallback when a project
# omits the override. Vendor-served mentions are a separate, larger figure:
# the pagination loop pulls up to page_ceiling x page_size per project per
# run (the ceiling is derived from the per-project cap in _fetch_mentions,
# see _mentions_page_ceiling below), which the per-project cap then trims.
# Overridable per project via sources.yaml `mentions_max_per_run`.
MAX_MENTIONS_PER_PROJECT_PER_RUN = 800
# Vendor page size cap. Brand24 paginates mentions/list; pull in pages of
# 100 (their documented per-page maximum on Business tier) until we hit
# the per-project run cap or the vendor returns an empty page.
MENTIONS_PAGE_SIZE = 100
# Empty-ping inflation factor for the per-run page ceiling. The kept-rows
# cap counts rows that survive the _mention_has_body filter, but the
# vendor serves a page of MENTIONS_PAGE_SIZE raw mentions of which only a
# fraction carry text (the module docstring records 75-99% empty social
# pings on Day 1). A naive ceiling of ceil(cap / page_size) pages assumes
# every served mention is kept, so on an empty-heavy stream the page
# ceiling binds long before the cap and non-empty mentions are silently
# dropped. Multiply the naive page count by this factor so the cap stays
# reachable across the documented empty range. Factor 6 tolerates roughly
# an 83% empty rate before the ceiling binds (at the 800 cap: 8 naive
# pages -> 50 ceiling pages -> ~5000 served for 800 kept). It is NOT sized
# for the pathological ~99%-empty case: every served mention counts
# against the shared 100K/month vendor cap, so the ceiling stays bounded
# (see _MENTIONS_MAX_PAGES_HARD_CAP) and the loop logs a warning when the
# ceiling, not the cap, ends paging.
_MENTIONS_EMPTY_PING_PAGE_FACTOR = 6
# Small additive buffer on top of the inflated ceiling for the partial
# final page and one cursor-lag page, preserving the old +2 intent.
_MENTIONS_PAGE_BUFFER = 2
# Absolute hard cap on pages per project per run. Bounds vendor calls (and
# therefore the monthly mention budget) even when a per-project cap is set
# very high, so a runaway or all-empty vendor response cannot spin the
# loop unbounded. 50 pages x 100 = 5000 served mentions / project / run.
_MENTIONS_MAX_PAGES_HARD_CAP = 50


def _brand24_enabled(sources: dict) -> bool:
    """True unless the config marks Brand24 retired.

    Reads ``enabled`` from the ``brand24`` block. An absent key means enabled,
    so a config that never carried the flag behaves exactly as it did before
    this gate existed, and the per-market blocks under it are untouched.

    Kept module level and free of connector state so the gate is testable on
    its own and so any other caller reaches the same verdict.
    """
    block = sources.get("brand24")
    if isinstance(block, dict) and "enabled" in block:
        return bool(block["enabled"])
    return True


def _mentions_page_ceiling(per_project_cap: int) -> int:
    """Pages to allow for one project's mention pull.

    Derived from the per-project KEPT-rows cap (not the module constant) so
    a sources.yaml override above ~1000 is actually reachable, with an
    empty-ping allowance so an empty-heavy stream still reaches the cap, and
    a hard ceiling so vendor calls stay bounded.
    """
    if per_project_cap <= 0:
        return 0
    naive_pages = -(-per_project_cap // MENTIONS_PAGE_SIZE)  # ceil division
    inflated = naive_pages * _MENTIONS_EMPTY_PING_PAGE_FACTOR + _MENTIONS_PAGE_BUFFER
    return min(inflated, _MENTIONS_MAX_PAGES_HARD_CAP)


# Brand24 numeric sentiment field maps to the same -1/0/1 convention as
# the rest of the pipeline. Values outside this set fall back to None
# so the row inherits the empty tone default.
_SENTIMENT_VALUE_MAP: dict[int, int] = {-1: -1, 0: 0, 1: 1}
# Vendor source label -> canonical pipeline platform label.
_VENDOR_SOURCE_TO_PLATFORM: dict[str, str] = {
    "facebook": "facebook",
    "instagram": "instagram",
    "tiktok": "tiktok",
    "twitter": "twitter",
    "x": "twitter",
    "youtube": "youtube",
    "linkedin": "linkedin",
    "reddit": "reddit",
    "threads": "threads",
    "news": "news",
    "web": "web",
    "blog": "web",
    "blogs": "web",  # vendor `category` field uses plural form on /mentions
    "forum": "web",
    "forums": "web",
    "podcast": "podcast",
}

# Hostname substring -> canonical platform label. Matches what the rest of
# the pipeline uses elsewhere so creator-watchlist / platform-aware scoring
# can treat Brand24 rows on par with native connectors.
_DOMAIN_TO_PLATFORM: list[tuple[str, str]] = [
    ("facebook.com", "facebook"),
    ("fb.com", "facebook"),
    ("instagram.com", "instagram"),
    ("tiktok.com", "tiktok"),
    ("twitter.com", "twitter"),
    ("x.com", "twitter"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("linkedin.com", "linkedin"),
    ("reddit.com", "reddit"),
    ("bsky.app", "bluesky"),
    ("threads.net", "threads"),
]


def _platform_from_url(url: str) -> str:
    """Infer platform label from a URL. Falls back to 'web' for unknowns."""
    if not url:
        return "web"
    lowered = url.lower()
    for fragment, platform in _DOMAIN_TO_PLATFORM:
        if fragment in lowered:
            return platform
    return "web"


def _domain_from_url(url: str) -> str:
    """Strip to bare host for display; returns empty string on malformed URLs."""
    if not url:
        return ""
    try:
        # Cheap parse, good enough: strip scheme, keep host.
        host = url.split("://", 1)[-1].split("/", 1)[0]
        # removeprefix is the literal-prefix strip; lstrip("www.") would
        # strip any combination of w/. chars and corrupt hosts like
        # "wwww.example.com" or "w.example.com".
        return host.lower().removeprefix("www.")
    except Exception:
        return ""


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def _extract_payload(resp_json: dict[str, Any]) -> dict[str, Any]:
    """Brand24 wraps successful responses under either ``data`` or
    ``message`` depending on endpoint. Probe both so the caller does not
    have to care."""
    if not isinstance(resp_json, dict):
        return {}
    for key in ("message", "data"):
        val = resp_json.get(key)
        if isinstance(val, dict):
            return val
    return {}


def _sentiment_breakdown_to_tone(
    sentiment: dict[str, Any] | None,
) -> float | None:
    """Map a Brand24 sentiment proportion dict onto the pipeline's 0..1 tone
    scale (same convention GDELT V2Tone uses, so downstream scoring can blend
    both signals). Returns None when the payload is absent or unparseable.
    """
    if not isinstance(sentiment, dict):
        return None
    try:
        pos = float(sentiment.get("positive") or 0.0)
        neu = float(sentiment.get("neutral") or 0.0)
        neg = float(sentiment.get("negative") or 0.0)
    except (TypeError, ValueError):
        return None
    total = pos + neu + neg
    if total <= 0:
        return None
    return (pos * 1.0 + neu * 0.5 + neg * 0.0) / total


def _topic_to_row(
    topic: dict[str, Any],
    market: str,
    project_name: str,
) -> dict[str, Any]:
    """Shape one topic entry into the pipeline's RAW_COLUMNS schema."""
    title = str(topic.get("topic_name") or "").strip()
    description = str(topic.get("description") or "").strip()
    mentions = int(topic.get("mentions") or 0)
    reach = int(topic.get("reach") or 0)
    return {
        "source": "brand24",
        "platform": "web",
        "market": market,
        "content_type": "topic",
        "query_group": "brand24_topics",
        "query_term": title or project_name,
        "author_name": "",
        "author_handle": "",
        "title": title[:300],
        "text": description[:2000],
        "url": "",
        "published_at": datetime.now(UTC).isoformat(),
        "views": reach,
        "likes": mentions,  # mentions ~ engagement proxy for topic-level rows
        "comments": 0,
        "shares": 0,
        "v2tone": "",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


def _follower_to_row(
    author: dict[str, Any],
    market: str,
    project_name: str,
) -> dict[str, Any]:
    """Shape one most-followers entry into RAW_COLUMNS.

    most-followers returns top voices in the project ranked by audience size.
    Treat each as a creator row so creator_watchlist scoring has something to
    match against, and record reach + mentions for engagement signal.
    """
    name = str(author.get("name") or "").strip()
    url = str(author.get("url") or "").strip()
    platform = _platform_from_url(url)
    followers = int(author.get("followers_count") or 0)
    mentions = int(author.get("mentions_count") or 0)
    reach = int(author.get("reach") or 0)
    return {
        "source": "brand24",
        "platform": platform,
        "market": market,
        "content_type": "top_author",
        "query_group": "brand24_top_authors",
        "query_term": name or url,
        "author_name": name,
        "author_handle": url,
        "title": f"{name} ({followers:,} followers)"
        if name
        else f"Top author ({followers:,} followers)",
        "text": (
            f"{name} on {platform} has {followers:,} followers and drove "
            f"{mentions} mentions with reach {reach:,} in {project_name}."
        ),
        "url": url,
        "published_at": datetime.now(UTC).isoformat(),
        "views": reach,
        "likes": mentions,
        "comments": 0,
        "shares": 0,
        "v2tone": "",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


def _trending_link_to_row(
    link: dict[str, Any],
    market: str,
    project_name: str,
) -> dict[str, Any]:
    """Shape one trending-link entry into RAW_COLUMNS with platform inferred
    from the URL, so Facebook / TikTok / Instagram links slot in alongside
    rows from the native connectors."""
    url = str(link.get("url") or "").strip()
    mentions = int(link.get("mentions_count") or 0)
    platform = _platform_from_url(url)
    domain = _domain_from_url(url)
    return {
        "source": "brand24",
        "platform": platform,
        "market": market,
        "content_type": "trending_link",
        "query_group": "brand24_trending_links",
        "query_term": domain or url,
        "author_name": "",
        "author_handle": "",
        "title": f"Trending on {domain}" if domain else "Trending link",
        "text": f"Link shared {mentions} times in {project_name}.",
        "url": url,
        "published_at": datetime.now(UTC).isoformat(),
        "views": 0,
        "likes": mentions,
        "comments": 0,
        "shares": 0,
        "v2tone": "",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


def _normalise_handle(raw: Any) -> str:
    """Lower-case, strip leading @, return empty string for missing values."""
    if not raw:
        return ""
    handle = str(raw).strip().lstrip("@").lower()
    return handle


def _vendor_sentiment_to_tone(raw: Any) -> str:
    """Map Brand24 mention sentiment to a synthetic V2Tone string so the
    enrichment path picks it up on par with GDELT rows.

    Brand24 sentiment for the mentions endpoint is a signed int in
    ``{-1, 0, 1}``. -1 -> tone -100, 0 -> 0, 1 -> +100, matching the
    GDELT V2Tone main-tone range. Returns empty string when sentiment
    is absent or out of range.
    """
    if raw is None:
        return ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return ""
    mapped = _SENTIMENT_VALUE_MAP.get(value)
    if mapped is None:
        return ""
    return f"{float(mapped * 100):.2f},0,0,0,0,0,0"


def _mention_platform(raw_source: Any, url: str) -> str:
    """Resolve a mention's platform label.

    Prefers the vendor ``source`` field (twitter / facebook / instagram /
    tiktok / news / blog / forum / web / youtube). Falls back to URL host
    inference for unknown vendor labels so a "news" source still lands as
    its canonical platform when the URL host is recognised.
    """
    if isinstance(raw_source, str):
        label = raw_source.strip().lower()
        if label in _VENDOR_SOURCE_TO_PLATFORM:
            return _VENDOR_SOURCE_TO_PLATFORM[label]
    return _platform_from_url(url or "")


def _parse_published_at(raw: Any) -> str:
    """Parse a vendor timestamp into an ISO 8601 UTC string.

    Brand24 published_at is documented as ``YYYY-MM-DD HH:MM:SS`` in UTC
    on the mentions endpoint, but the public docs also show ISO 8601 with
    a Z suffix on newer projects. Handle both. Falls back to ``now`` so
    the row still lands rather than getting rejected by downstream
    pyarrow type coercion.
    """
    if not raw:
        return datetime.now(UTC).isoformat()
    text = str(raw).strip()
    if not text:
        return datetime.now(UTC).isoformat()
    # Common Brand24 shape: "2026-05-27 14:32:00" (UTC, no tz).
    candidates = [text, text.replace(" ", "T")]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
        except ValueError:
            continue
    return datetime.now(UTC).isoformat()


def _mention_has_body(mention: dict[str, Any]) -> bool:
    """True when a mention carries real text (title or content), not just metadata.

    Brand24's /mentions endpoint returns many social pings (X, Instagram,
    Facebook) with both ``title`` and ``content`` null: only host + category
    + sentiment. Day 1 (29 May 2026) those empty pings were 97% of the X
    rows, 99% of the Instagram rows, 75% of the Facebook rows, and every one
    classified at 0% downstream because there is no text to classify. They
    inflate the unclassified rate and burn the 100K/month mention cap for
    zero signal. Skip them at ingestion. Every non-social platform (news,
    blogs, forum, tiktok, reddit) returns a body and passes this guard.
    """
    title = str(mention.get("title") or "").strip()
    content = str(mention.get("content") or "").strip()
    return bool(title or content)


def _mention_to_row(
    mention: dict[str, Any],
    market: str,
    project_name: str,
) -> dict[str, Any]:
    """Shape one mention entry into the pipeline's RAW_COLUMNS schema.

    Vendor contract verified by live probe 28 May 2026 against
    `/api-data/v1/project/{pid}/mentions`. Response envelope is
    `{status: success, message: {results: [...], has_more_mentions,
    cursor}}` (NOT `data.mentions` as the documented endpoint
    `/mentions/list` suggested; that path 404s). Each result carries:

      - date: YYYY-MM-DD
      - time: HH:MM (UTC)
      - title: nullable (set for blogs + news, null for social posts)
      - content: nullable (the body text)
      - source: URL for blogs / news; "Tweet-ID: NNN" string for X;
        null for IG / FB / TikTok platform pings
      - host: the domain (foodformzansi.co.za, twitter.com,
        instagram.com, facebook.com, tiktok.com, etc.)
      - category: the platform proxy (blogs, x, instagram, facebook,
        tiktok, youtube, news, forum, web)
      - sentiment: signed int -1 / 0 / +1
      - tags: nullable list of vendor-assigned tags

    No author handle, no reach, no engagement metrics at this endpoint.
    Title + content nullable on social rows; we fall back to the host
    name so downstream consumers don't see fully blank rows.
    """
    title = str(mention.get("title") or "").strip()
    content = str(mention.get("content") or "").strip()
    source_field = str(mention.get("source") or "").strip()
    host = str(mention.get("host") or "").strip()
    category = str(mention.get("category") or "").strip().lower()
    # URL is either the source field (when it's a URL) or empty for
    # platform-only mentions. "Tweet-ID:" strings are not URLs.
    url = source_field if source_field.lower().startswith(("http://", "https://")) else ""
    # Platform mapping. Vendor "category" is the cleanest signal; map
    # x -> twitter and blogs -> blog so the downstream classifier sees
    # the same platform vocabulary used by other connectors.
    platform = _mention_platform(category or host, url)
    # Compose published_at from date + time (both UTC per vendor docs).
    date_str = str(mention.get("date") or "").strip()
    time_str = str(mention.get("time") or "00:00").strip()
    published_at = _parse_published_at(f"{date_str} {time_str}" if date_str else None)
    # When title is null but we have content, use the first 80 chars
    # of content as a synthetic title so the classifier + email card
    # have something to render. Pure platform-only pings (everything
    # null) fall back to "<host> mention" so the row is still
    # diagnosable.
    if not title:
        if content:
            title = content[:80]
        elif host:
            title = f"{host} mention"
    return {
        "source": "brand24",
        "platform": platform,
        "market": market,
        "content_type": "mention",
        "query_group": "brand24_mentions",
        # query_term: vendor does not return a keyword on this endpoint,
        # fall back to project_name so downstream grouping still works.
        "query_term": str(project_name)[:300],
        "author_name": "",
        "author_handle": "",
        "title": title[:300],
        "text": content[:4000],
        "url": url,
        "published_at": published_at,
        # No engagement metrics at this endpoint. The Phase 2 Brand24
        # ai-insights endpoint exposes reach + sentiment summary; queued
        # for a follow-up ship if signal warrants.
        "views": 0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "v2tone": _vendor_sentiment_to_tone(mention.get("sentiment")),
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


def _hashtag_to_row(
    hashtag: dict[str, Any],
    market: str,
    project_name: str,
) -> dict[str, Any]:
    """Shape one trending-hashtag entry into RAW_COLUMNS."""
    tag = str(hashtag.get("hashtag") or "").strip()
    mentions = int(hashtag.get("mentions_count") or 0)
    reach = int(hashtag.get("social_media_reach") or 0)
    return {
        "source": "brand24",
        "platform": "social",
        "market": market,
        "content_type": "trending_hashtag",
        "query_group": "brand24_hashtags",
        "query_term": tag,
        "author_name": "",
        "author_handle": "",
        "title": f"Trending hashtag {tag}",
        "text": (f"{tag} trending in {project_name}. {mentions} mentions, reach {reach:,}."),
        "url": "",
        "published_at": datetime.now(UTC).isoformat(),
        "views": reach,
        "likes": mentions,
        "comments": 0,
        "shares": 0,
        "v2tone": "",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


def _ai_insight_to_row(
    field: str,
    text: str,
    market: str,
    project_name: str,
    project_id: str,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """Shape one ai-insights narrative field into RAW_COLUMNS.

    Brand24 ai-insights returns four string fields per project per call
    (headline, trends, insights, recommendations) over a configurable
    date window. Each field becomes one row so the brief generator and
    email digest can surface them independently. The full narrative text
    lives in ``text`` (truncated to 4000 chars matching the mentions
    contract); ``title`` carries a composed "<Project> <field>" label so
    the row remains diagnosable in BQ scans.

    Vendor contract verified by live probe 28 May 2026 against
    ``/api-data/v1/project/{pid}/ai-insights``. Only the top-level path
    returns 200; sub-paths (topics, competitors, sentiment-trajectory,
    themes, summary, clusters) all 404 on Business tier.
    """
    cleaned = str(text or "").strip()
    label = field.replace("_", " ").title()
    title = f"{project_name} {label} ({date_from} to {date_to})"
    return {
        "source": "brand24",
        # platform="aggregate" so the row skips the trend-scoring loop in
        # run_rss_now.py (which excludes platform=="aggregate" and counts it
        # as neither scored nor unclassified), matching the sibling Wave-2
        # surfaces (mention_sentiment, mention_reach, daily_metric). An
        # ai-insight is a window-level narrative summary, not organic
        # cultural-topic content: published_at=now() and a fresh distinct
        # platform would otherwise let a 7-day summary look fresh and hand
        # topics the cross-source multiplier. content_type stays
        # "brand24_ai_insight" so the brief generator still reads these rows.
        "platform": "aggregate",
        "market": market,
        "content_type": "brand24_ai_insight",
        "query_group": "brand24_ai_insights",
        "query_term": f"{project_id}:{field}",
        "author_name": "",
        "author_handle": "",
        "title": title[:300],
        "text": cleaned[:4000],
        "url": "",
        "published_at": datetime.now(UTC).isoformat(),
        "views": 0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "v2tone": "",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


def _mention_sentiment_to_row(
    sample_date: str,
    total: int,
    positive: int,
    negative: int,
    market: str,
    project_name: str,
    project_id: str,
) -> dict[str, Any]:
    """Shape one (project, date) sentiment aggregate into RAW_COLUMNS.

    Vendor returns three same-keyed dicts under ``message`` (``mentions``,
    ``positive_mentions``, ``negative_mentions``) where each maps a date
    string to a daily count. We collapse them into per-date rows so the
    brief generator + email digest read project-level sentiment
    trajectory without a join.

    ``views`` carries total mentions, ``likes`` carries positive count,
    ``comments`` carries negative count. ``v2tone`` carries a synthetic
    GDELT-shaped tone derived from positive vs negative share so the
    enrichment pipeline picks it up on par with topic + ai-insights
    rows. Neutral count is total minus positive minus negative.
    """
    neutral = max(total - positive - negative, 0)
    # Daily tone: (positive - negative) / total, scaled to GDELT V2Tone
    # main-tone range [-100, +100]. Zero when total is zero.
    tone = 0.0
    if total > 0:
        tone = ((positive - negative) / float(total)) * 100.0
    text = (
        f"{project_name} on {sample_date}: {total:,} mentions "
        f"({positive:,} positive, {neutral:,} neutral, {negative:,} negative)."
    )
    title = f"{project_name} sentiment {sample_date}"
    # Compose a stable ISO 8601 UTC midnight for published_at so the row
    # sorts on its sample date, not the run timestamp.
    try:
        published_at = datetime.fromisoformat(sample_date).replace(tzinfo=UTC).isoformat()
    except ValueError:
        published_at = datetime.now(UTC).isoformat()
    return {
        "source": "brand24",
        "platform": "aggregate",
        "market": market,
        "content_type": "brand24_mention_sentiment",
        "query_group": "brand24_mention_sentiment",
        "query_term": f"{project_id}:{sample_date}",
        "author_name": "",
        "author_handle": "",
        "title": title[:300],
        "text": text[:4000],
        "url": "",
        "published_at": published_at,
        "views": int(total),
        "likes": int(positive),
        "comments": int(negative),
        "shares": 0,
        "v2tone": f"{tone:.2f},0,0,0,0,0,0",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


def _mention_reach_to_row(
    sample_date: str,
    social_reach: int,
    non_social_reach: int,
    market: str,
    project_name: str,
    project_id: str,
) -> dict[str, Any]:
    """Shape one (project, date) reach aggregate into RAW_COLUMNS.

    Vendor returns ``social_media_reach`` and ``non_social_media_reach``
    as date-keyed dicts. ``views`` carries the social reach, ``likes``
    carries the non-social reach so the trend_scores aggregator can pick
    up both signals without a schema change. Total reach is the sum;
    surfaced in the row text for diagnostics.
    """
    total = int(social_reach) + int(non_social_reach)
    text = (
        f"{project_name} on {sample_date}: total reach {total:,} "
        f"(social {social_reach:,}, non-social {non_social_reach:,})."
    )
    title = f"{project_name} reach {sample_date}"
    try:
        published_at = datetime.fromisoformat(sample_date).replace(tzinfo=UTC).isoformat()
    except ValueError:
        published_at = datetime.now(UTC).isoformat()
    return {
        "source": "brand24",
        "platform": "aggregate",
        "market": market,
        "content_type": "brand24_mention_reach",
        "query_group": "brand24_mention_reach",
        "query_term": f"{project_id}:{sample_date}",
        "author_name": "",
        "author_handle": "",
        "title": title[:300],
        "text": text[:4000],
        "url": "",
        "published_at": published_at,
        "views": int(social_reach),
        "likes": int(non_social_reach),
        "comments": 0,
        "shares": 0,
        "v2tone": "",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


def _daily_metric_to_row(
    day: dict[str, Any],
    market: str,
    project_name: str,
    project_id: str,
) -> dict[str, Any]:
    """Shape one daily-metrics entry into RAW_COLUMNS.

    Vendor day shape (verified by live probe 28 May 2026):
        {date, mentions_count, reach_total,
         sentiment: {positive, neutral, negative},
         engagement: {likes, comments, shares},
         by_source: [...]}

    Engagement fields land in ``likes`` / ``comments`` / ``shares``
    one-to-one. ``views`` carries reach_total. ``v2tone`` carries a
    synthetic GDELT-shaped tone from the sentiment proportions so the
    enrichment pipeline picks it up on par with topic + ai-insights rows.
    """
    sample_date = str(day.get("date") or "").strip()
    mentions_count = int(day.get("mentions_count") or 0)
    reach_total = int(day.get("reach_total") or 0)
    sentiment = day.get("sentiment") if isinstance(day.get("sentiment"), dict) else {}
    engagement = day.get("engagement") if isinstance(day.get("engagement"), dict) else {}
    likes = int(engagement.get("likes") or 0)
    comments = int(engagement.get("comments") or 0)
    shares = int(engagement.get("shares") or 0)
    tone_value = _sentiment_breakdown_to_tone(sentiment)
    v2tone = ""
    if tone_value is not None:
        # _sentiment_breakdown_to_tone returns 0..1; scale to GDELT
        # main-tone range [-100, +100] like the topics fetcher does.
        v2tone = f"{(tone_value * 200.0) - 100.0:.2f},0,0,0,0,0,0"
    text = (
        f"{project_name} on {sample_date}: {mentions_count:,} mentions, "
        f"reach {reach_total:,}, likes {likes:,}, comments {comments:,}, "
        f"shares {shares:,}."
    )
    title = f"{project_name} daily metric {sample_date}"
    try:
        published_at = datetime.fromisoformat(sample_date).replace(tzinfo=UTC).isoformat()
    except ValueError:
        published_at = datetime.now(UTC).isoformat()
    return {
        "source": "brand24",
        "platform": "aggregate",
        "market": market,
        "content_type": "brand24_daily_metric",
        "query_group": "brand24_daily_metrics",
        "query_term": f"{project_id}:{sample_date}",
        "author_name": "",
        "author_handle": "",
        "title": title[:300],
        "text": text[:4000],
        "url": "",
        "published_at": published_at,
        "views": reach_total,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "v2tone": v2tone,
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
    }


class Brand24Connector(BaseConnector):
    """Pulls Brand24 topics and trending hashtags per market."""

    SOURCE_NAME = "brand24"
    PLATFORM = "web"

    def _load_projects(self) -> list[dict[str, Any]]:
        """Read project list for this market from sources.yaml."""
        try:
            sources = load_sources()
        except Exception as exc:
            self.logger.warning("Brand24 config load failed for market=%s: %s", self.market, exc)
            return []
        block = (sources.get("brand24") or {}).get(self.market) or {}
        projects = block.get("projects") or []
        return [p for p in projects if isinstance(p, dict)]

    def _get(
        self,
        path: str,
        token: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET an `/api-data/v1/...` endpoint. Returns the unwrapped payload
        dict (under `data` or `message`) or {} on error.

        Non-JSON responses retry once with a short backoff. Brand24's
        analytics endpoints occasionally return an HTML error page
        (observed on KE trending-hashtags 2026-04-27) that is transient.
        Logs content-type and a body excerpt on parse failure so the
        operator can distinguish a transient hiccup from an upstream
        change.
        """
        headers = {"X-Api-Key": token, "Accept": "application/json"}
        url = f"{BRAND24_BASE_URL}{path}"

        payload: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                resp = self._session.request(
                    "GET",
                    url,
                    headers=headers,
                    params=params or {},
                    timeout=REQUESTS_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                self.logger.error("Brand24 HTTP %s for %s (market=%s)", status, path, self.market)
                return {}
            except requests.exceptions.RequestException as exc:
                self.logger.error(
                    "Brand24 request failed for %s (market=%s): %s",
                    path,
                    self.market,
                    str(exc)[:200],
                )
                return {}

            if not resp.content:
                return {}

            try:
                payload = resp.json()
                break
            except ValueError:
                content_type = resp.headers.get("Content-Type", "?")
                body_excerpt = resp.text[:200].replace("\n", " ").replace("\r", " ")
                if attempt == 0:
                    self.logger.warning(
                        "Brand24 non-JSON response for %s (market=%s) "
                        "content_type=%s body=%r, retrying once",
                        path,
                        self.market,
                        content_type,
                        body_excerpt,
                    )
                    time.sleep(1.0)
                    continue
                self.logger.error(
                    "Brand24 non-JSON response for %s (market=%s) after retry "
                    "content_type=%s body=%r",
                    path,
                    self.market,
                    content_type,
                    body_excerpt,
                )
                return {}

        if payload is None or payload.get("status") != "success":
            if payload is not None:
                self.logger.warning(
                    "Brand24 status=%r on %s, message=%r",
                    payload.get("status"),
                    path,
                    payload.get("message"),
                )
            return {}
        return _extract_payload(payload)

    def _fetch_topics(
        self,
        project_id: str,
        project_name: str,
        token: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        body = self._get(
            f"/api-data/v1/project/{project_id}/topics",
            token=token,
            params={"date_from": date_from, "date_to": date_to},
        )
        topics = body.get("topics") or []
        rows: list[dict[str, Any]] = []
        for topic in topics[:MAX_TOPICS_PER_PROJECT]:
            row = _topic_to_row(topic, self.market, project_name)
            tone = _sentiment_breakdown_to_tone(topic.get("sentiment"))
            if tone is not None:
                # Inject tone_avg via a synthetic V2Tone-like string so the
                # enrichment path picks it up on par with GDELT rows.
                row["v2tone"] = f"{(tone * 200.0) - 100.0:.2f},0,0,0,0,0,0"
            rows.append(row)
        self.logger.info(
            "Brand24 topics: project=%s market=%s pulled %d of %d",
            project_id,
            self.market,
            len(rows),
            len(topics),
        )
        return rows

    def _fetch_trending_hashtags(
        self,
        project_id: str,
        project_name: str,
        token: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        body = self._get(
            f"/api-data/v1/project/{project_id}/trending-hashtags",
            token=token,
            params={"date_from": date_from, "date_to": date_to},
        )
        tags = body.get("hashtags") or []
        rows: list[dict[str, Any]] = []
        for tag in tags[:MAX_HASHTAGS_PER_PROJECT]:
            rows.append(_hashtag_to_row(tag, self.market, project_name))
        self.logger.info(
            "Brand24 hashtags: project=%s market=%s pulled %d of %d",
            project_id,
            self.market,
            len(rows),
            len(tags),
        )
        return rows

    def _fetch_most_followers(
        self,
        project_id: str,
        project_name: str,
        token: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        body = self._get(
            f"/api-data/v1/project/{project_id}/most-followers",
            token=token,
            params={"date_from": date_from, "date_to": date_to},
        )
        authors = body.get("authors") or []
        rows = [
            _follower_to_row(a, self.market, project_name)
            for a in authors[:MAX_FOLLOWERS_PER_PROJECT]
        ]
        self.logger.info(
            "Brand24 most-followers: project=%s market=%s pulled %d of %d",
            project_id,
            self.market,
            len(rows),
            len(authors),
        )
        return rows

    def _fetch_trending_links(
        self,
        project_id: str,
        project_name: str,
        token: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        body = self._get(
            f"/api-data/v1/project/{project_id}/trending-links",
            token=token,
            params={"date_from": date_from, "date_to": date_to},
        )
        links = body.get("trending_links") or []
        rows = [
            _trending_link_to_row(link, self.market, project_name)
            for link in links[:MAX_LINKS_PER_PROJECT]
        ]
        self.logger.info(
            "Brand24 trending-links: project=%s market=%s pulled %d of %d",
            project_id,
            self.market,
            len(rows),
            len(links),
        )
        return rows

    def _fetch_mentions(
        self,
        project_id: str,
        project_name: str,
        token: str,
        per_project_cap: int,
    ) -> list[dict[str, Any]]:
        """Pull the raw mention stream for a project.

        Window is T-1 (yesterday) to T (today) inclusive, so late-arriving
        rows surface on the next-day cron rather than getting silently
        dropped. Pages through ``MENTIONS_PAGE_SIZE`` items per call until
        the per-project cap is hit or the vendor returns an empty page.

        The page ceiling is derived from ``per_project_cap`` (not the module
        constant) with an empty-ping allowance, so a sources.yaml override
        above ~1000 is reachable and an empty-heavy stream still reaches its
        cap. Each page is one Brand24 API call against the shared 100K
        mention monthly budget.
        """
        if per_project_cap <= 0:
            return []
        # Per-run page ceiling from the per-project cap, not the module
        # constant, so caps above ~1000 are reachable and the empty-ping
        # rate does not bind the loop before the cap.
        page_ceiling = _mentions_page_ceiling(per_project_cap)
        date_from = _iso_days_ago(1)
        date_to = _iso_days_ago(0)
        # Vendor URL confirmed by live probe 28 May 2026:
        # `/mentions` (NOT `/mentions/list` — that path 404s). Cursor
        # pagination via `cursor` param, not `page`. Response envelope
        # is `{status, message: {results, has_more_mentions, cursor}}`.
        path = f"/api-data/v1/project/{project_id}/mentions"

        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        skipped_empty = 0
        vendor_exhausted = False
        while pages < page_ceiling and len(rows) < per_project_cap:
            params: dict[str, str] = {
                "date_from": date_from,
                "date_to": date_to,
                "limit": str(MENTIONS_PAGE_SIZE),
            }
            if cursor:
                params["cursor"] = cursor
            body = self._get(path, token=token, params=params)
            # Vendor envelope: body["message"]["results"] etc. Accept
            # legacy shapes too so a mid-flight contract change does
            # not silently empty out the connector.
            message = body.get("message") if isinstance(body, dict) else None
            if isinstance(message, dict):
                mentions = message.get("results") or []
                has_more = bool(message.get("has_more_mentions"))
                next_cursor = message.get("cursor")
            else:
                mentions = body.get("mentions") or body.get("results") or []
                has_more = bool(body.get("has_more_mentions"))
                next_cursor = body.get("cursor")
            if not isinstance(mentions, list) or not mentions:
                vendor_exhausted = True
                break
            for mention in mentions:
                if not isinstance(mention, dict):
                    continue
                # Skip empty social pings (no title, no content). They carry
                # zero classifiable signal and burn the monthly mention cap.
                if not _mention_has_body(mention):
                    skipped_empty += 1
                    continue
                rows.append(_mention_to_row(mention, self.market, project_name))
                if len(rows) >= per_project_cap:
                    break
            pages += 1
            if not has_more or not next_cursor:
                vendor_exhausted = True
                break
            cursor = str(next_cursor)

        self.logger.info(
            "Brand24 mentions: project=%s market=%s pulled %d "
            "(cap %d, pages %d/%d, skipped %d empty)",
            project_id,
            self.market,
            len(rows),
            per_project_cap,
            pages,
            page_ceiling,
            skipped_empty,
        )
        # The page ceiling, not the cap, ended the loop: the vendor still had
        # mentions but we stopped paging before reaching per_project_cap.
        # Empty pings are likely eating the page budget; surface it so the
        # operator can raise the cap or the empty-ping factor rather than
        # silently dropping non-empty mentions.
        if not vendor_exhausted and len(rows) < per_project_cap:
            self.logger.warning(
                "Brand24 mentions: project=%s market=%s hit page ceiling %d "
                "with %d/%d kept rows and %d empty pings skipped; vendor had "
                "more mentions. Non-empty mentions may be dropped; consider "
                "raising mentions_max_per_run or the empty-ping page factor.",
                project_id,
                self.market,
                page_ceiling,
                len(rows),
                per_project_cap,
                skipped_empty,
            )
        return rows

    def _fetch_ai_insights(
        self,
        project_id: str,
        project_name: str,
        token: str,
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        """Pull Brand24's narrative AI summary for a project.

        One vendor call per project per run. Returns up to four rows
        (one per narrative field: headline, trends, insights,
        recommendations). Empty fields are skipped so a partial vendor
        response doesn't land hollow rows.

        Vendor envelope verified by 28 May 2026 live probe across all
        three SSA projects: ``{status: success, message: {project_id,
        date_from, date_to, headline, trends, insights, recommendations}}``.
        Both ``message.X`` and ``data.X`` shapes are accepted defensively
        so a mid-flight vendor envelope change does not silently empty
        out the connector.
        """
        date_from = _iso_days_ago(max(1, lookback_days))
        date_to = _iso_days_ago(0)
        body = self._get(
            f"/api-data/v1/project/{project_id}/ai-insights",
            token=token,
            params={"date_from": date_from, "date_to": date_to},
        )
        # _get already unwraps message / data. Echo back the vendor's
        # date window when present so the row title reflects the actual
        # window the narrative summarises, not the requested one.
        echoed_from = str(body.get("date_from") or date_from)
        echoed_to = str(body.get("date_to") or date_to)
        rows: list[dict[str, Any]] = []
        for field in _AI_INSIGHT_FIELDS:
            value = body.get(field)
            if not isinstance(value, str):
                continue
            cleaned = value.strip()
            if not cleaned:
                continue
            rows.append(
                _ai_insight_to_row(
                    field=field,
                    text=cleaned,
                    market=self.market,
                    project_name=project_name,
                    project_id=project_id,
                    date_from=echoed_from,
                    date_to=echoed_to,
                )
            )
        self.logger.info(
            "Brand24 ai-insights: project=%s market=%s pulled %d of %d fields",
            project_id,
            self.market,
            len(rows),
            len(_AI_INSIGHT_FIELDS),
        )
        return rows

    def _fetch_mention_sentiment(
        self,
        project_id: str,
        project_name: str,
        token: str,
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        """Pull per-day sentiment aggregate for a project.

        Vendor envelope (verified by 28 May 2026 live probe):
            {message: {mentions: {date: int, ...},
                       total_mentions: int,
                       positive_mentions: {date: int, ...},
                       total_positive_mentions: int,
                       negative_mentions: {date: int, ...},
                       total_negative_mentions: int}}

        One row per (project, date). Vendor honours the requested date
        window for this endpoint (unlike daily-metrics).
        """
        date_from = _iso_days_ago(max(1, lookback_days))
        date_to = _iso_days_ago(0)
        body = self._get(
            f"/api-data/v1/project/{project_id}/mentions/sentiment",
            token=token,
            params={"date_from": date_from, "date_to": date_to},
        )
        mentions = body.get("mentions") if isinstance(body.get("mentions"), dict) else {}
        positive = (
            body.get("positive_mentions") if isinstance(body.get("positive_mentions"), dict) else {}
        )
        negative = (
            body.get("negative_mentions") if isinstance(body.get("negative_mentions"), dict) else {}
        )
        rows: list[dict[str, Any]] = []
        for sample_date in sorted(mentions.keys()):
            try:
                total_val = int(mentions.get(sample_date) or 0)
                pos_val = int(positive.get(sample_date) or 0)
                neg_val = int(negative.get(sample_date) or 0)
            except (TypeError, ValueError):
                continue
            rows.append(
                _mention_sentiment_to_row(
                    sample_date=sample_date,
                    total=total_val,
                    positive=pos_val,
                    negative=neg_val,
                    market=self.market,
                    project_name=project_name,
                    project_id=project_id,
                )
            )
        self.logger.info(
            "Brand24 mention-sentiment: project=%s market=%s pulled %d daily rows",
            project_id,
            self.market,
            len(rows),
        )
        return rows

    def _fetch_mention_reach(
        self,
        project_id: str,
        project_name: str,
        token: str,
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        """Pull per-day reach aggregate for a project.

        Vendor envelope (verified by 28 May 2026 live probe):
            {message: {social_media_reach: {date: int, ...},
                       social_media_reach_total: int,
                       non_social_media_reach: {date: int, ...},
                       non_social_media_reach_total: int}}

        One row per (project, date). Vendor honours the requested date
        window for this endpoint.
        """
        date_from = _iso_days_ago(max(1, lookback_days))
        date_to = _iso_days_ago(0)
        body = self._get(
            f"/api-data/v1/project/{project_id}/mentions/reach",
            token=token,
            params={"date_from": date_from, "date_to": date_to},
        )
        social = (
            body.get("social_media_reach")
            if isinstance(body.get("social_media_reach"), dict)
            else {}
        )
        non_social = (
            body.get("non_social_media_reach")
            if isinstance(body.get("non_social_media_reach"), dict)
            else {}
        )
        all_dates = sorted(set(social.keys()) | set(non_social.keys()))
        rows: list[dict[str, Any]] = []
        for sample_date in all_dates:
            try:
                social_val = int(social.get(sample_date) or 0)
                non_social_val = int(non_social.get(sample_date) or 0)
            except (TypeError, ValueError):
                continue
            rows.append(
                _mention_reach_to_row(
                    sample_date=sample_date,
                    social_reach=social_val,
                    non_social_reach=non_social_val,
                    market=self.market,
                    project_name=project_name,
                    project_id=project_id,
                )
            )
        self.logger.info(
            "Brand24 mention-reach: project=%s market=%s pulled %d daily rows",
            project_id,
            self.market,
            len(rows),
        )
        return rows

    def _fetch_daily_metrics(
        self,
        project_id: str,
        project_name: str,
        token: str,
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        """Pull richest per-day aggregate for a project.

        Vendor envelope (verified by 28 May 2026 live probe):
            {message: {project_id, from, to, days: [{date, mentions_count,
                       reach_total, sentiment: {...},
                       engagement: {likes, comments, shares},
                       by_source: [...]}]}}

        One vendor call per project per run. Vendor returns ~31 days of
        rows regardless of the requested window so the lookback param
        is mostly a hint; the client trims to ``lookback_days`` so a
        7-day pulldown does not silently land 31 days of rollups in
        raw_content.
        """
        date_from = _iso_days_ago(max(1, lookback_days))
        date_to = _iso_days_ago(0)
        body = self._get(
            f"/api-data/v1/project/{project_id}/daily-metrics",
            token=token,
            params={"date_from": date_from, "date_to": date_to},
        )
        days = body.get("days") if isinstance(body.get("days"), list) else []
        # Client-side trim: vendor returns ~31 days regardless of window
        # (verified 28 May 2026). Honour the caller's lookback so the
        # raw_content footprint stays predictable.
        rows: list[dict[str, Any]] = []
        for day in days:
            if not isinstance(day, dict):
                continue
            sample_date = str(day.get("date") or "").strip()
            if not sample_date or sample_date < date_from:
                continue
            rows.append(
                _daily_metric_to_row(
                    day=day,
                    market=self.market,
                    project_name=project_name,
                    project_id=project_id,
                )
            )
        self.logger.info(
            "Brand24 daily-metrics: project=%s market=%s pulled %d of %d days",
            project_id,
            self.market,
            len(rows),
            len(days),
        )
        return rows

    def fetch(
        self,
        *,
        projects: list[dict[str, Any]] | None = None,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        api_key: str | None = None,
        include_topics: bool = True,
        include_hashtags: bool = True,
        include_followers: bool = True,
        include_links: bool = True,
        include_mentions: bool = True,
        include_ai_insights: bool | None = None,
        include_mentions_sentiment: bool | None = None,
        include_mentions_reach: bool | None = None,
        include_daily_metrics: bool | None = None,
        **_: Any,
    ) -> pd.DataFrame:
        """Pull topics and trending hashtags for every configured project.

        When ``projects`` is None the list is loaded from sources.yaml under
        ``brand24.{market}.projects``. Missing API key or missing project
        list both trigger an empty DataFrame return with a warning logged;
        the pipeline keeps running.
        """
        # The retirement gate runs BEFORE the credential read, because the
        # credential is not the off switch. Brand24 was cancelled on
        # 21 Aug 2026 and its monthly cycle ends on the 24th, so from that date
        # every call is against a dead account. Same defect class as the
        # EnsembleData gate in #300: fetch proceeded on a credential alone, so
        # a key reaching the job by any route would keep calling a cancelled
        # vendor and log errors nobody can act on over a decision already made.
        try:
            sources = load_sources()
        except Exception as exc:
            self.logger.warning("Brand24 config load failed for market=%s: %s", self.market, exc)
            sources = {}
        if not _brand24_enabled(sources):
            self.logger.info(
                "Brand24 disabled in config; skipping fetch for market=%s", self.market
            )
            return self.empty_dataframe()

        token = api_key or os.environ.get("BRAND24_API_KEY", "").strip()
        if not token:
            self.logger.warning(
                "BRAND24_API_KEY not set; Brand24 connector returning empty "
                "DataFrame for market=%s",
                self.market,
            )
            return self.empty_dataframe()

        if projects is None:
            projects = self._load_projects()
        if not projects:
            self.logger.info(
                "No Brand24 projects configured for market=%s; skipping",
                self.market,
            )
            return self.empty_dataframe()

        # Wave-2 top-level gates resolve from the per-project flags when not
        # passed explicitly. The orchestrator calls safe_fetch() with no
        # kwargs, so a False default would silently skip the whole branch even
        # though sources.yaml sets the per-project include flag true (the
        # 29 May flip put the flags per-project; this is what makes them fire).
        # An explicit True/False from a caller (tests) still wins.
        # _resolve also reports whether the top-level gate was resolved FROM
        # the per-project flags (kwarg was None) or set explicitly by a caller.
        # The per-project default below keys off that: when resolved from
        # config the gate is opt-in (default False) so a sibling project that
        # never set the key is not fetched just because one project opted in;
        # when a caller passed an explicit True the gate keeps its True default
        # so a project without the key still fires.
        def _resolve(flag: bool | None, key: str) -> tuple[bool, bool]:
            if flag is not None:
                return flag, False
            return any(bool(p.get(key)) for p in projects), True

        include_mentions_sentiment, mentions_sentiment_from_config = _resolve(
            include_mentions_sentiment, "include_mentions_sentiment"
        )
        include_mentions_reach, mentions_reach_from_config = _resolve(
            include_mentions_reach, "include_mentions_reach"
        )
        include_daily_metrics, daily_metrics_from_config = _resolve(
            include_daily_metrics, "include_daily_metrics"
        )
        include_ai_insights, ai_insights_from_config = _resolve(
            include_ai_insights, "include_ai_insights"
        )

        date_from = _iso_days_ago(lookback_days)
        date_to = _iso_days_ago(0)
        rows: list[dict[str, Any]] = []

        for project in projects:
            project_id = str(project.get("id", "")).strip()
            if not project_id:
                self.logger.warning(
                    "Brand24 project missing id (market=%s); skipping entry",
                    self.market,
                )
                continue
            project_name = str(project.get("name") or project.get("query_group") or project_id)

            if include_topics:
                rows.extend(self._fetch_topics(project_id, project_name, token, date_from, date_to))
            # Per-project opt-out: callers can disable hashtag pulls for
            # specific projects via `include_hashtags: false` in
            # configs/sources.yaml. Falls back to the connector-wide
            # include_hashtags kwarg when the per-project key is absent.
            project_include_hashtags = bool(project.get("include_hashtags", True))
            if include_hashtags and project_include_hashtags:
                rows.extend(
                    self._fetch_trending_hashtags(
                        project_id, project_name, token, date_from, date_to
                    )
                )
            elif include_hashtags and not project_include_hashtags:
                self.logger.info(
                    "Brand24 hashtags: project=%s market=%s skipped (disabled in config)",
                    project_id,
                    self.market,
                )
            if include_followers:
                rows.extend(
                    self._fetch_most_followers(project_id, project_name, token, date_from, date_to)
                )
            if include_links:
                rows.extend(
                    self._fetch_trending_links(project_id, project_name, token, date_from, date_to)
                )
            # Per-project opt-out: callers can disable mentions/list pulls
            # for specific projects via `include_mentions: false` in
            # configs/sources.yaml. Mirrors the include_hashtags pattern.
            project_include_mentions = bool(project.get("include_mentions", True))
            if include_mentions and project_include_mentions:
                # Honour per-project override of the monthly-budget cap.
                try:
                    per_project_cap = int(
                        project.get("mentions_max_per_run", MAX_MENTIONS_PER_PROJECT_PER_RUN)
                    )
                except (TypeError, ValueError):
                    per_project_cap = MAX_MENTIONS_PER_PROJECT_PER_RUN
                rows.extend(self._fetch_mentions(project_id, project_name, token, per_project_cap))
            elif include_mentions and not project_include_mentions:
                self.logger.info(
                    "Brand24 mentions: project=%s market=%s skipped (disabled in config)",
                    project_id,
                    self.market,
                )
            # Per-project opt-out: callers can disable ai-insights pulls
            # for specific projects via `include_ai_insights: false` in
            # configs/sources.yaml. Mirrors the include_mentions pattern.
            # Connector-wide default is False (dark-on-ship); flag flip
            # is a follow-up commit after live verification.
            project_include_ai_insights = bool(
                project.get("include_ai_insights", not ai_insights_from_config)
            )
            if include_ai_insights and project_include_ai_insights:
                try:
                    ai_lookback = int(
                        project.get("ai_insights_lookback_days", AI_INSIGHTS_LOOKBACK_DAYS)
                    )
                except (TypeError, ValueError):
                    ai_lookback = AI_INSIGHTS_LOOKBACK_DAYS
                rows.extend(self._fetch_ai_insights(project_id, project_name, token, ai_lookback))
            elif include_ai_insights and not project_include_ai_insights:
                self.logger.info(
                    "Brand24 ai-insights: project=%s market=%s skipped (disabled in config)",
                    project_id,
                    self.market,
                )

            # Wave 2 (28 May 2026): per-project aggregate endpoints.
            # All three default-off on ship; flip via sources.yaml after
            # Day 1 live validation. Per-project override reads
            # `include_<endpoint>` from the project dict. The default tracks
            # how the top-level gate resolved: opt-in (False) when resolved
            # from the per-project flags, True when a caller passed an
            # explicit gate, so a project without the key still fires then.
            project_include_mentions_sentiment = bool(
                project.get("include_mentions_sentiment", not mentions_sentiment_from_config)
            )
            if include_mentions_sentiment and project_include_mentions_sentiment:
                try:
                    sentiment_lookback = int(
                        project.get(
                            "mention_sentiment_lookback_days",
                            MENTION_SENTIMENT_LOOKBACK_DAYS,
                        )
                    )
                except (TypeError, ValueError):
                    sentiment_lookback = MENTION_SENTIMENT_LOOKBACK_DAYS
                rows.extend(
                    self._fetch_mention_sentiment(
                        project_id, project_name, token, sentiment_lookback
                    )
                )
            elif include_mentions_sentiment and not project_include_mentions_sentiment:
                self.logger.info(
                    "Brand24 mention-sentiment: project=%s market=%s skipped (disabled in config)",
                    project_id,
                    self.market,
                )

            project_include_mentions_reach = bool(
                project.get("include_mentions_reach", not mentions_reach_from_config)
            )
            if include_mentions_reach and project_include_mentions_reach:
                try:
                    reach_lookback = int(
                        project.get(
                            "mention_reach_lookback_days",
                            MENTION_REACH_LOOKBACK_DAYS,
                        )
                    )
                except (TypeError, ValueError):
                    reach_lookback = MENTION_REACH_LOOKBACK_DAYS
                rows.extend(
                    self._fetch_mention_reach(project_id, project_name, token, reach_lookback)
                )
            elif include_mentions_reach and not project_include_mentions_reach:
                self.logger.info(
                    "Brand24 mention-reach: project=%s market=%s skipped (disabled in config)",
                    project_id,
                    self.market,
                )

            project_include_daily_metrics = bool(
                project.get("include_daily_metrics", not daily_metrics_from_config)
            )
            if include_daily_metrics and project_include_daily_metrics:
                try:
                    daily_lookback = int(
                        project.get(
                            "daily_metrics_lookback_days",
                            DAILY_METRICS_LOOKBACK_DAYS,
                        )
                    )
                except (TypeError, ValueError):
                    daily_lookback = DAILY_METRICS_LOOKBACK_DAYS
                rows.extend(
                    self._fetch_daily_metrics(project_id, project_name, token, daily_lookback)
                )
            elif include_daily_metrics and not project_include_daily_metrics:
                self.logger.info(
                    "Brand24 daily-metrics: project=%s market=%s skipped (disabled in config)",
                    project_id,
                    self.market,
                )

        if not rows:
            return self.empty_dataframe()

        df = pd.DataFrame(rows)
        df = df.reindex(columns=list(RAW_COLUMNS), fill_value="")
        return df


__all__ = [
    "AI_INSIGHTS_LOOKBACK_DAYS",
    "BRAND24_BASE_URL",
    "DAILY_METRICS_LOOKBACK_DAYS",
    "MAX_MENTIONS_PER_PROJECT_PER_RUN",
    "MENTIONS_PAGE_SIZE",
    "MENTION_REACH_LOOKBACK_DAYS",
    "MENTION_SENTIMENT_LOOKBACK_DAYS",
    "Brand24Connector",
    "_ai_insight_to_row",
    "_daily_metric_to_row",
    "_mention_has_body",
    "_mention_reach_to_row",
    "_mention_sentiment_to_row",
    "_mention_to_row",
    "_mentions_page_ceiling",
    "_platform_from_url",
    "_sentiment_breakdown_to_tone",
    "_vendor_sentiment_to_tone",
]
