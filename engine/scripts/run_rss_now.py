"""Production daily pipeline entrypoint.

Runs the full daily Trends Engine V2 job end-to-end for all three markets.
Ingests from the eight live connectors (RSS, BigQuery Trends, YouTube,
GDELT GKG, EnsembleData, Reddit, Brand24, Apple Music), classifies and
scores each topic on the deterministic composite, sends surviving topics
to Vertex Gemini for creative briefs, computes the BQML forecast, and
sends the stakeholder PULSE digest email. Writes to raw_content,
enriched_content, trend_scores, trend_analysis, and pipeline_runs.

Caller: the Dockerfile ENTRYPOINT (the Cloud Run daily job). Cloud Scheduler
runs this job at 00:30 UTC (primary) and 02:30 UTC (fallback); the run is
GCP-native end to end, GitHub holds no execution path.

Run from the project root:
    python scripts/run_rss_now.py

Requirements: .env file in project root, GCP ADC authenticated.
"""

import hashlib
import json
import logging
import math
import os
import re
import sys
import traceback
import uuid
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

# Load .env before any other imports
env_path = Path(__file__).parent.parent / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    # Fallback: hand-rolled parser. Note: os.environ.setdefault means stale
    # shell vars win. Install python-dotenv to get override=False semantics.
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from google.cloud import bigquery
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.execution_approval import ExecutionApprovalV2
from src.analysis.open_intelligence.execution_origins import select_origin
from src.analysis.open_intelligence.funded_lane import _issue_wave1_execution_capability
from src.ingestion.connectors.app_charts import AppChartsConnector
from src.ingestion.connectors.apple_music import AppleMusicConnector
from src.ingestion.connectors.audiomack import AudiomackConnector
from src.ingestion.connectors.bigquery_trends import BigQueryTrendsConnector
from src.ingestion.connectors.bluesky import BlueskyConnector
from src.ingestion.connectors.cloudflare_radar import CloudflareRadarConnector
from src.ingestion.connectors.ensemble import EnsembleConnector
from src.ingestion.connectors.gdelt import GDELTConnector
from src.ingestion.connectors.google_trends_rss import GoogleTrendsRssConnector
from src.ingestion.connectors.pulsar import PulsarConnector
from src.ingestion.connectors.reddit import RedditConnector
from src.ingestion.connectors.rss import RSSConnector
from src.ingestion.connectors.semrush import SemrushConnector
from src.ingestion.connectors.socialcrawl import SocialCrawlConnector
from src.ingestion.connectors.spotify import SpotifyConnector
from src.ingestion.connectors.wikipedia import WikipediaConnector
from src.ingestion.connectors.youtube import YouTubeConnector
from src.ingestion.connectors.youtube_scrape import SCRAPE_QUERY_GROUP, YouTubeScrapeConnector
from src.ingestion.enrichment import _search_velocity_on, embedding_is_enabled, enrich_dataframe
from src.observability.events import batch_events
from src.scoring.corroboration import compute_corroboration
from src.scoring.velocity import (
    compute_velocity_scores_for_today,
    compute_velocity_windows_for_today,
)
from src.utils.bigquery import get_dataset, insert_dataframe, merge_dataframe
from src.utils.config_loader import load_scoring
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MARKETS = ["za", "ng", "ke"]

# Connector registry. Each tuple is (source_key, connector_class). Column
# source_key matches the pipeline_runs.*_rows field name.
CONNECTORS = [
    ("rss", RSSConnector),
    # Apple Music RSS Charts (28 May 2026). Replaces Spotify for the Wave 1
    # chart-data slot because Spotify's Web API client_credentials flow
    # returns 403 / 404 on ALL playlist endpoints (editorial + community)
    # after the November 2024 deprecation. Apple's free RSS feed at
    # rss.marketingtools.apple.com/api/v2/{market}/music/most-played/N/songs.json
    # requires no auth, no rate-limit, returns 50 tracks per market with
    # name + artist + genre. Direct verification of music_amapiano /
    # music_afrobeats / music_gengetone scoring. Active by default.
    ("apple_music", AppleMusicConnector),
    # Spotify Charts: shipped 28 May 2026 (commit b16a3c0) but Spotify
    # deprecated client_credentials playlist access. Connector stays
    # wired in (dormant) in case Spotify reopens access or we add a
    # user-OAuth flow. `spotify.enabled` permanently false in YAML.
    ("spotify", SpotifyConnector),
    ("bigquery_trends", BigQueryTrendsConnector),
    ("youtube", YouTubeConnector),
    # yt-dlp uncapped YouTube search (free, no API key, no 10K quota). Live on
    # all three markets via youtube_queries.<market>.scrape_enabled. Supplements
    # the API connector with breadth; rows are source=youtube_scrape,
    # platform=youtube.
    ("youtube_scrape", YouTubeScrapeConnector),
    ("gdelt", GDELTConnector),
    ("ensemble", EnsembleConnector),
    # Reddit runs after Ensemble: it shares the EnsembleConnector class-level
    # _global_units_spent + _global_quota_exhausted ledger so the combined
    # daily spend stays under the Bronze 5000-unit cap. Disabled by default
    # via sources.yaml `reddit.enabled` so the connector is wired in but
    # produces zero rows until the operator flips the flag.
    ("reddit", RedditConnector),
    # SocialCrawl unified multi-platform search. Arrived as an eval channel on
    # 3 Jul 2026 and became the PRIMARY social vendor on 23 Jul 2026 when the
    # EnsembleData account was cancelled. Live via sources.yaml
    # socialcrawl.enabled; source=socialcrawl in enriched_content.
    ("socialcrawl", SocialCrawlConnector),
    # Wave 2 free no-auth open-web connectors. Wikipedia is LIVE
    # (wikipedia.enabled true): Wikimedia top-per-country pageviews, what a
    # whole market is reading on the encyclopedia. Bluesky has no config block
    # in sources.yaml at all, so it is wired into the registry and produces
    # zero rows until one is added; public searchPosts on per-market terms.
    ("wikipedia", WikipediaConnector),
    ("bluesky", BlueskyConnector),
    # Semrush keyword metrics. Dark via semrush.enabled in sources.yaml.
    # 20 API units per keyword; budget_units_per_run caps per-market spend.
    ("semrush", SemrushConnector),
    # Google Trends daily-trending RSS. Free, no auth. LIVE since 4 Jul 2026
    # via google_trends_rss.enabled in sources.yaml.
    ("google_trends_rss", GoogleTrendsRssConnector),
    # Apple App Store top-charts RSS. Free, no auth, same marketing-tools RSS
    # API family as apple_music. LIVE since 5 Jul 2026 via app_charts.enabled
    # in sources.yaml.
    ("app_charts", AppChartsConnector),
    # Audiomack trending (dark, 4 Jul 2026). NG-dominant streaming platform;
    # OAuth1 consumer keys pending, connector returns empty without them.
    # Live-probe the response shape before any flip.
    ("audiomack", AudiomackConnector),
    # Cloudflare Radar per-country domain ranking. Free API token, CC BY 4.0
    # data. LIVE via cloudflare_radar.enabled in sources.yaml, but returns
    # empty without CLOUDFLARE_RADAR_TOKEN.
    ("cloudflare_radar", CloudflareRadarConnector),
    # Pulsar social-listening eval (dark, 14 Jul 2026). Read-only GraphQL post
    # pull from configured search hashes. Dark via sources.yaml pulsar.enabled;
    # flag off = zero network calls, safe disconnect for the trial token.
    # Fully removable: delete this line + the config block + the connector file.
    ("pulsar", PulsarConnector),
]
if len({k for k, _ in CONNECTORS}) != len(CONNECTORS):
    raise RuntimeError("CONNECTORS has duplicate source keys")


def _connector_plan_for_run(durable_wave1: object | None):
    if durable_wave1 is None:
        return tuple(CONNECTORS)
    plan = tuple(item for item in CONNECTORS if item[0] == "socialcrawl")
    if len(plan) != 1:
        raise RuntimeError("Wave 1 funded connector is unavailable")
    return plan


# Connectors safe to fetch in parallel within a market. Ensemble must stay
# before Reddit (shared unit ledger); both run sequentially after the batch.
_CONNECTOR_PARALLEL_OK = frozenset(
    {
        "rss",
        "apple_music",
        "spotify",
        "bigquery_trends",
        "youtube",
        "youtube_scrape",
        "gdelt",
        "wikipedia",
        "bluesky",
        "socialcrawl",
        "google_trends_rss",
        "app_charts",
        "audiomack",
        "cloudflare_radar",
    }
)


def _s(value) -> str:
    """Defensive str cast: BigQuery STRING columns reject int/list/None cleanly,
    so coerce any non-string connector output before load.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _nan_to_none(value):
    """Convert NaN float to None so BigQuery FLOAT64 loads as NULL, not 'nan'."""
    import math

    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def build_raw_row(row: dict, run_id: str, collected_at: datetime) -> dict:
    raw = {
        "id": str(uuid.uuid4()),
        "source": _s(row.get("source")),
        "platform": _s(row.get("platform")) or "web",
        "market": _s(row.get("market")),
        "content_type": _s(row.get("content_type")) or "article",
        "query_group": _s(row.get("query_group")) or "news",
        "query_term": _s(row.get("query_term")),
        "author_name": _s(row.get("author_name")),
        "author_handle": _s(row.get("author_handle")),
        "title": _s(row.get("title")),
        "text": _s(row.get("text")),
        "url": _s(row.get("url")),
        "published_at": row.get("published_at"),
        "collected_at": collected_at,
        # Connectors that emit hashtags (Ensemble, Brand24) carry them on the
        # raw row dict; preserve so the topic classifier sees them. RSS,
        # YouTube, GDELT, BQ Trends produce empty by default.
        "hashtags": _s(row.get("hashtags")),
        "views": float(row.get("views", 0) or 0),
        "likes": float(row.get("likes", 0) or 0),
        "comments": float(row.get("comments", 0) or 0),
        "shares": float(row.get("shares", 0) or 0),
        "engagement_total": float(row.get("views", 0) or 0)
        + float(row.get("likes", 0) or 0)
        + float(row.get("comments", 0) or 0)
        + float(row.get("shares", 0) or 0),
        "pipeline_run_id": run_id,
        # GDELT GKG fields. Empty string for non-GDELT connectors.
        "v2tone": _s(row.get("v2tone")),
        "v2persons": _s(row.get("v2persons")),
        "v2orgs": _s(row.get("v2orgs")),
        "v2locations": _s(row.get("v2locations")),
    }
    # Bridge the bigquery_trends search_velocity_score across into the frame
    # enrich_dataframe receives. Dark behind SEARCH_VELOCITY_ENABLED: when off
    # the key is omitted (enrichment then hard-zeros every row) so the raw_row
    # is byte-identical to today; when on it is carried so enrichment can
    # preserve it. Other connectors do not emit it, so their rows default 0.0.
    if _search_velocity_on() and "search_velocity_score" in row:
        try:
            raw["search_velocity_score"] = float(row.get("search_velocity_score") or 0.0)
        except (TypeError, ValueError):
            raw["search_velocity_score"] = 0.0
    # Wave 0.2 GCAM emotion bridge (GKG-only), mirrors the search-velocity bridge
    # above. Dark behind gdelt.gcam_enabled: when off, GDELT _normalise_row yields
    # an empty v2gcam so the key is omitted here and df_raw carries no GCAM column,
    # so the merged branch stays load-safe with no migration. When the flag is on,
    # the parsed code:value string is carried so it reaches raw_content; the v2gcam
    # BQ column must exist first (scripts/migrations/add_v2gcam_column.py).
    # Backstop: a connector that forces the v2gcam column without empty-stringing
    # it leaves a float NaN, which is truthy and would stringify to the literal
    # "nan". Skip NaN so the key is omitted (matching the empty-string contract).
    _v2gcam = row.get("v2gcam")
    if _v2gcam is not None and not (isinstance(_v2gcam, float) and math.isnan(_v2gcam)):
        _v2gcam_str = _s(_v2gcam)
        if _v2gcam_str:
            raw["v2gcam"] = _v2gcam_str
    if row.get("source_family_map_version") == "channel_family_v2":
        from src.ingestion.connectors.socialcrawl import WAVE1_AUTHORITY_COLUMNS

        for field in WAVE1_AUTHORITY_COLUMNS:
            value = _s(row.get(field))
            if not value:
                raise ValueError(f"Wave 1 authority field is missing: {field}")
            raw[field] = value
    return raw


def build_enriched_row(raw_row: dict) -> dict:
    handle = raw_row.get("author_handle", "")
    return {
        **raw_row,
        "author_handle_norm": handle.lower().lstrip("@") if handle else "",
        "regional_score": 0.0,
        "genz_score": 0.0,
        "creator_watchlist_tier": "",
        "creator_watchlist_score": 0.0,
        "slang_terms": "",
        "slang_score": 0.0,
        "search_velocity_score": 0.0,
    }


# Cross-source confirmation counts independent CHANNEL FAMILIES, not raw
# source strings (6000+ news domains arrive via RSS and GDELT) and not platform
# strings (Brand24 alone emits web/facebook/tiktok/instagram from one feed).
# Each connector maps to one family; every news domain collapses to "news".
# Mapping fitted against the live source distribution (2026-06-16).
_CHANNEL_FAMILY_BY_SOURCE = {
    "ensembledata": "ensemble",
    "reddit": "reddit",
    "brand24": "brand24",
    "google trends": "search",
    "bigquery_trends": "search",
    "semrush": "search",
    "google_trends_rss": "search",
    "apple_music": "music",
    "app_charts": "apps",
    "youtube": "youtube",
    "youtube_scrape": "youtube",
    "gdelt": "news",
    # Wave 2 connectors (dark). Each is an independent channel, not news: Wikipedia
    # is open-web encyclopedia signal, Bluesky is social. Without these they fall
    # through to "news" and inflate news corroboration in the cross-source
    # multiplier the day either flag flips.
    "wikipedia": "wikipedia",
    "bluesky": "bluesky",
    # Audiomack is chart/streaming data like apple_music: the music family
    # (factual). Radar is aggregate web attention: its own family, never
    # factual.
    "audiomack": "music",
    "cloudflare_radar": "web_attention",
}

# Platform fallback for rows whose source string is not a known connector name:
# RSS rows carry source=feed_name (6000+ news domains) with platform "web", and
# YouTube rows carry source=channelTitle with platform "youtube". Same-platform
# content from a second vendor (SocialCrawl) is NOT an independent corroborating
# channel, so tiktok/instagram/threads/twitter collapse to the ensemble family.
_CHANNEL_FAMILY_BY_PLATFORM = {
    "web": "news",
    "news": "news",
    "google_search": "search",
    "semrush_search": "search",
    "apple_music": "music",
    "spotify": "music",
    "music": "music",
    "youtube": "youtube",
    "reddit": "reddit",
    "tiktok": "ensemble",
    "instagram": "ensemble",
    "threads": "ensemble",
    "twitter": "ensemble",
    "wikipedia": "wikipedia",
    "bluesky": "bluesky",
}

# Rows whose engagement is structurally zero (a creator's back catalogue pulled
# via the YouTube playlist path carries views/likes/comments = 0) are excluded
# from the engagement-intensity divisor so they do not dilute a shared topic's
# engagement_score. They still count for volume and classification.
_ZERO_ENGAGEMENT_CONTENT_TYPES = {"youtube_channel_upload"}


def _channel_family(source: str, platform: str = "") -> str:
    """Map a row's source to its independent ingestion channel family.

    YouTube rows carry source=channelTitle (e.g. "MrBeast") for run-log
    visibility, so when the source itself does not name a known family fall back
    to the platform string. Without this every real YouTube row defaulted to the
    "news" family and never counted as an independent corroborating channel.
    """
    fam = _CHANNEL_FAMILY_BY_SOURCE.get(str(source).strip().lower())
    if fam is not None:
        return fam
    plat = str(platform).strip().lower()
    fam = _CHANNEL_FAMILY_BY_SOURCE.get(plat) or _CHANNEL_FAMILY_BY_PLATFORM.get(plat)
    if fam is not None:
        return fam
    # Unknown source AND platform: an unmapped connector must never manufacture
    # a factual "news" family (it inflates the cross-source multiplier and the
    # corroboration tier). It still counts as one independent channel.
    return "other"


# Forward Phase 2 (semantic corroboration, shadow). Pull the entity-ish tokens a
# row names (hashtags + GDELT V2Persons + V2Orgs) so the aggregator can measure
# whether the SAME entity shows up across channel families, not just whether the
# topic keyword splattered across families by coincidence. A light proxy for the
# event_ledger entity resolver: split on the common delimiters, lowercase, keep
# tokens of >= 3 chars, cap per row so a token-heavy GDELT row cannot blow up.
_ENTITY_SPLIT = re.compile(r"[,;#|/]+|\s{2,}")
_ENTITY_MAX_PER_ROW = 40


def _entity_tokens(row) -> set[str]:
    tokens: set[str] = set()
    for field in ("hashtags", "v2persons", "v2orgs"):
        raw = row.get(field)
        if raw is None:
            continue
        # In production enrich_dataframe stores these as _s() strings ("" when
        # empty), but a raw/test DataFrame can carry a float NaN for an absent
        # column, which is truthy, so coerce and drop the "nan" sentinel.
        raw = str(raw).strip()
        if not raw or raw.lower() == "nan":
            continue
        for part in _ENTITY_SPLIT.split(raw):
            tok = part.strip().lower().strip("#@.,:'\"()[]")
            # GDELT person/org tokens can carry a trailing ",offset" pair; the
            # split on comma already drops the numeric tail, leaving the name.
            if len(tok) >= 3 and tok != "nan" and not tok.isdigit():
                tokens.add(tok)
                if len(tokens) >= _ENTITY_MAX_PER_ROW:
                    return tokens
    return tokens


def _aggregate_by_topic(
    df_enriched_full,
    market: str,
) -> tuple[dict[tuple[str, str], dict], int]:
    """Shape per-(market, topic_group) scoring stats with 1/N multi-assign weighting.

    For each enriched row, splits its contribution across the topics in its
    topic_groups list with weight 1/N. Rows with an empty topic_groups list
    increment the unclassified counter and are skipped (do not contribute
    to scoring).

    Returns:
        (market_counts, unclassified_count)
        market_counts is keyed by (market, topic_group). Each value carries
        the per-group scoring inputs (item_count, source_diversity,
        engagement_sum, *_avg, creator_spread, tone_avg_mean, tone_rows)
        that compute_trend_scores expects.
    """
    # Part B (WATCHLIST_MAX_AGG, default off): a topic's watchlist signal is the
    # single best creator match, not the per-row mean. The mean divides one real
    # tier hit across thousands of zero-score rows, collapsing watchlist_score to
    # ~0. Dark by default so the OFF path stays byte-identical to the live cron.
    watchlist_max_agg = os.environ.get("WATCHLIST_MAX_AGG", "false").lower() == "true"
    accum: dict[tuple[str, str], dict] = defaultdict(
        lambda: {
            "item_count": 0.0,
            "engagement_sum": 0.0,
            # Sum of weights for rows that carry real engagement signal (every
            # row except the structurally-zero catalogue types). Used as the
            # engagement-intensity divisor so zero-engagement back-catalogue
            # rows do not drag a topic's per-row engagement average down.
            "engagement_item_count": 0.0,
            "regional_sum": 0.0,
            "genz_sum": 0.0,
            "slang_sum": 0.0,
            "watchlist_sum": 0.0,
            "watchlist_max": 0.0,
            "search_velocity_sum": 0.0,
            "source_set": set(),
            # platform_set kept for diagnostics only. The cross-source
            # confirmation multiplier now keys on channel_set (independent
            # channel families via _channel_family) so 6000+ RSS/GDELT news
            # domains count as one "news" channel and Brand24's per-URL
            # platform inference counts as one "brand24" channel.
            "platform_set": set(),
            "channel_set": set(),
            "creator_set": set(),
            "tone_sum": 0.0,
            "tone_weight_sum": 0.0,
            "tone_rows": 0,
            # GCAM emotional-arousal intensity (GDELT-only), aggregated exactly
            # like tone: weight-summed per topic, gcam_rows gates present/absent.
            "gcam_sum": 0.0,
            "gcam_weight_sum": 0.0,
            "gcam_rows": 0,
            # Seed score: weighted item count per channel family, so the
            # scoring layer can read the share of a topic's items on the
            # visual/audio-native families (TikTok/IG, YouTube, music) that a
            # Nanobanana/Lyria activation rides on. Keyed by family so the
            # selection of which families count stays in scoring.yaml, not here.
            "channel_family_sum": defaultdict(float),
            # Forward Phase 2 (semantic corroboration, shadow): entity token ->
            # set of channel families that named it. Lets the output measure the
            # SAME entity confirmed across families, vs the structural
            # channel_diversity that counts any content across families.
            "entity_families": defaultdict(set),
            # Phase 0 corroboration scorer: the freshest published_at across the
            # topic's rows, so the scorer can decay corroboration by recency.
            # None until a row with a usable timestamp lands; NULLs are ignored.
            "freshest_published_at": None,
        }
    )
    unclassified = 0

    for row in df_enriched_full.to_dict(orient="records"):
        topics = row.get("topic_groups") or []
        # 26 May 2026: classifier may return ["__drop__"] sentinel for known
        # Brand24 aggregated-label noise (commercial spam, foreign news,
        # aggregator content). These rows are excluded from trend_scores
        # entirely and do not count toward unclassified either; they
        # simply leave the pipeline.
        if topics == ["__drop__"]:
            continue
        # Workstream E: Brand24 Wave-2 aggregate surfaces (mentions/sentiment,
        # mentions/reach, daily-metrics) land with platform='aggregate' and
        # content_type brand24_mention_sentiment / _reach / brand24_daily_metric.
        # They are per-project daily rollups, not cultural-topic content, so
        # they must NOT contribute to trend_scores AND must NOT count as
        # unclassified. They feed the brief generator as a per-project sentiment
        # trajectory instead. Read platform inline because platform_name is not
        # defined until further down this loop body.
        if str(row.get("platform") or "").lower() == "aggregate":
            continue
        if not topics:
            unclassified += 1
            continue
        weight = 1.0 / len(topics)
        engagement = float(row.get("engagement_weighted") or 0.0)
        regional = float(row.get("regional_score") or 0.0)
        genz = float(row.get("genz_score") or 0.0)
        slang = float(row.get("slang_score") or 0.0)
        watchlist = float(row.get("creator_watchlist_score") or 0.0)
        search_velocity = float(row.get("search_velocity_score") or 0.0)
        tone_val = row.get("tone_avg")
        gcam_val = row.get("gcam_intensity")
        # published_at is a tz-aware pandas Timestamp or NaT (coerced upstream).
        # published_at arrives as a mix of str and pandas Timestamp across
        # connectors, so coerce to one UTC Timestamp before the topic-max compare
        # below, else "str > Timestamp" raises and the whole market aborts. NaT or
        # None is ignored so a missing timestamp does not poison the max.
        published_val = pd.to_datetime(row.get("published_at"), errors="coerce", utc=True)
        if pd.isna(published_val):
            published_val = None
        source_name = str(row.get("source") or "")
        platform_name = str(row.get("platform") or "").lower()
        content_type = str(row.get("content_type") or "").lower()
        creator_name = str(row.get("author_handle_norm") or "")
        # Forward Phase 2 shadow: entity tokens are a row property, compute once.
        row_entities = _entity_tokens(row)

        for topic in topics:
            key = (market, topic)
            acc = accum[key]
            acc["item_count"] += weight
            acc["engagement_sum"] += engagement * weight
            if content_type not in _ZERO_ENGAGEMENT_CONTENT_TYPES:
                acc["engagement_item_count"] += weight
            acc["regional_sum"] += regional * weight
            acc["genz_sum"] += genz * weight
            acc["slang_sum"] += slang * weight
            acc["watchlist_sum"] += watchlist * weight
            # Max tracks the strongest single creator match in the bucket (raw
            # per-row score, not 1/N weighted) for the WATCHLIST_MAX_AGG path.
            acc["watchlist_max"] = max(acc["watchlist_max"], watchlist)
            acc["search_velocity_sum"] += search_velocity * weight
            if source_name:
                acc["source_set"].add(source_name)
                fam = _channel_family(source_name, platform_name)
                acc["channel_set"].add(fam)
                acc["channel_family_sum"][fam] += weight
                # Forward Phase 2 shadow: record which families named each entity.
                for tok in row_entities:
                    acc["entity_families"][tok].add(fam)
            if platform_name:
                acc["platform_set"].add(platform_name)
            if creator_name:
                acc["creator_set"].add(creator_name)
            if published_val is not None:
                prev = acc["freshest_published_at"]
                if prev is None or published_val > prev:
                    acc["freshest_published_at"] = published_val
            if tone_val is not None:
                try:
                    f_tone = float(tone_val)
                except (TypeError, ValueError):
                    f_tone = None
                # pandas stores missing tone as NaN; guard so NaN does not
                # poison tone_sum and null the downstream trend_score.
                if f_tone is not None and not math.isnan(f_tone):
                    acc["tone_sum"] += f_tone * weight
                    acc["tone_weight_sum"] += weight
                    acc["tone_rows"] += 1
            if gcam_val is not None:
                try:
                    f_gcam = float(gcam_val)
                except (TypeError, ValueError):
                    f_gcam = None
                # NaN guard mirrors tone: a missing gcam is pd.NA (no column)
                # or math.nan (unparseable). float(pd.NA) raises TypeError,
                # caught above; math.nan is caught by the isnan check below.
                if f_gcam is not None and not math.isnan(f_gcam):
                    acc["gcam_sum"] += f_gcam * weight
                    acc["gcam_weight_sum"] += weight
                    acc["gcam_rows"] += 1

    out: dict[tuple[str, str], dict] = {}
    for key, acc in accum.items():
        divisor = max(acc["item_count"], 1e-9)
        out[key] = {
            "item_count": acc["item_count"],
            "source_diversity": len(acc["source_set"]),
            "platform_diversity": len(acc["platform_set"]),
            "channel_diversity": len(acc["channel_set"]),
            "engagement_item_count": acc["engagement_item_count"],
            "regional_avg": acc["regional_sum"] / divisor,
            "genz_avg": acc["genz_sum"] / divisor,
            "slang_avg": acc["slang_sum"] / divisor,
            "watchlist_avg": (
                acc["watchlist_max"] if watchlist_max_agg else acc["watchlist_sum"] / divisor
            ),
            "search_velocity_avg": acc["search_velocity_sum"] / divisor,
            "engagement_sum": acc["engagement_sum"],
            "creator_spread": len(acc["creator_set"]),
            # Weight-weighted mean: divide the weight-scaled tone_sum by the sum
            # of those rows' weights, not the raw row count. Matches the
            # regional/genz/slang convention (weighted sum / item_count-as-sum-
            # of-weights). tone_rows stays as the "any tone contributed" gate.
            "tone_avg_mean": (
                acc["tone_sum"] / acc["tone_weight_sum"] if acc["tone_weight_sum"] > 0 else None
            ),
            "tone_rows": acc["tone_rows"],
            "gcam_avg_mean": (
                acc["gcam_sum"] / acc["gcam_weight_sum"] if acc["gcam_weight_sum"] > 0 else None
            ),
            "gcam_rows": acc["gcam_rows"],
            "channel_family_weights": dict(acc["channel_family_sum"]),
            # Forward Phase 2 (semantic corroboration, shadow). families: the most
            # channel families that any single shared entity reached in the topic
            # (1 = no cross-family entity, >=2 = a genuinely corroborated entity).
            # entities: how many distinct entities spanned >=2 families. Both are
            # stored only; trend_score is unchanged.
            "semantic_corroboration_families": max(
                (len(fams) for fams in acc["entity_families"].values()), default=0
            ),
            "semantic_corroboration_entities": sum(
                1 for fams in acc["entity_families"].values() if len(fams) >= 2
            ),
            "freshest_published_at": acc["freshest_published_at"],
        }
    return out, unclassified


# --- Wave 1 momentum + lifecycle (dark by default) -------------------------
# All four Wave 1 EngineCore features store additive columns unconditionally
# (they change nothing until something reads them). The compute that needs a
# gate (lifecycle classifier, the deferred composite feed) reads these flags.

# momentum_label band: 7d must beat 30d by this fraction of the 30d read to
# read "rising", or fall short by it to read "cooling". Inside the band the
# label is "building" when both windows carry signal, else "steady". A small
# absolute floor stops a 0.02-vs-0.00 pair (both ~zero) reading as "rising".
MOMENTUM_BAND = 0.15
MOMENTUM_MIN_SIGNAL = 0.05


def _momentum_label(velocity_7d: float, velocity_30d: float) -> str:
    """Classify rising / building / steady / cooling from the two windows.

    Display-only. rising: the short window is meaningfully hotter than the
    long window (trend accelerating). cooling: short window meaningfully
    colder (trend fading). building: both windows carry signal but the gap is
    inside the band (sustained warmth). steady: neither window carries real
    signal (flat / dormant).
    """
    v7 = float(velocity_7d or 0.0)
    v30 = float(velocity_30d or 0.0)
    if v7 < MOMENTUM_MIN_SIGNAL and v30 < MOMENTUM_MIN_SIGNAL:
        return "steady"
    # Band is relative to the long read, with the absolute floor as a guard so
    # a near-zero 30d does not make any positive 7d look like a huge accel.
    threshold = max(MOMENTUM_BAND * v30, MOMENTUM_MIN_SIGNAL)
    if v7 - v30 > threshold:
        return "rising"
    if v30 - v7 > threshold:
        return "cooling"
    return "building"


def _lifecycle_enabled() -> bool:
    return os.environ.get("LIFECYCLE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _tone_split_enabled() -> bool:
    return os.environ.get("TONE_SPLIT_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _seed_score_enabled() -> bool:
    # Default ON: seed_score is an additive signal and the chip/topline are the
    # Jo deliverable. Set SEED_SCORE_ENABLED=false to dark-switch the render
    # without a redeploy of the scoring layer.
    return os.environ.get("SEED_SCORE_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _seed_intelligence_enabled() -> bool:
    # Default ON: the seed-intelligence Gemini pass mines hidden behaviours.
    # One extra ~$0.01 Gemini call/day; non-fatal. Set SEED_INTELLIGENCE_ENABLED
    # =false to skip the call entirely.
    return os.environ.get("SEED_INTELLIGENCE_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _continuity_badges_enabled() -> bool:
    return os.environ.get("CONTINUITY_BADGES_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _micro_briefs_enabled() -> bool:
    return os.environ.get("MICRO_BRIEFS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _momentum_in_composite() -> bool:
    """Deferred composite feed. Wired but stays off in Wave 1.

    When this ever flips on, momentum would feed the weighted composite via the
    momentum weight in scoring.yaml. That weight is 0 in Wave 1 so the 10-signal
    bundle still sums to 1.0 and the live composite is byte-identical. Reading
    the flag here keeps the wiring real without changing behaviour.
    """
    return os.environ.get("MOMENTUM_IN_COMPOSITE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# lifecycle_phase thresholds. birth: real volume today and no usable prior
# history. growth: today's volume is rising past the 7-day median (and the
# topic is still warm). maturity: a positive composite with low velocity and a
# stable count near its median. decline: velocity has gone cold / volume below
# the prior median.
LIFECYCLE_GROWTH_MEDIAN_RATIO = 1.25
LIFECYCLE_DECLINE_MEDIAN_RATIO = 0.75
LIFECYCLE_LOW_VELOCITY = 0.10
LIFECYCLE_BIRTH_MIN_ITEMS = 3


def _lifecycle_phase(
    velocity_14d: float,
    velocity_7d: float,
    item_count: float,
    median_item_count_7d: float | None,
) -> str:
    """Classify birth / growth / maturity / decline from velocity + count history.

    median_item_count_7d is None when the topic has no prior 7-day history.
    Birth needs that no-history signal plus real volume today. With history:
    growth when today's count rises past the 7-day median while the 7-day
    velocity is still warm; decline when velocity is cold or the count has
    fallen below the prior median; maturity otherwise (a settled, still-present
    topic).
    """
    v14 = float(velocity_14d or 0.0)
    v7 = float(velocity_7d or 0.0)
    count = float(item_count or 0.0)

    if median_item_count_7d is None:
        # No usable prior history. A topic with real volume today is being
        # born; a thin/empty one is treated as decline (fading in, not a
        # trend). Mirrors the velocity new-topic volume gate.
        return "birth" if count >= LIFECYCLE_BIRTH_MIN_ITEMS else "decline"

    median = float(median_item_count_7d or 0.0)
    if median <= 0:
        return "birth" if count >= LIFECYCLE_BIRTH_MIN_ITEMS else "decline"

    ratio = count / median
    if ratio >= LIFECYCLE_GROWTH_MEDIAN_RATIO and v7 > LIFECYCLE_LOW_VELOCITY:
        return "growth"
    if ratio <= LIFECYCLE_DECLINE_MEDIAN_RATIO or v14 <= 0.0:
        return "decline"
    return "maturity"


def _continuity(
    prior_continuity_day: int | None,
    appeared_yesterday: bool,
    had_recent_gap: bool,
) -> tuple[int, str]:
    """Compute (continuity_day, continuity_state) from the prior-day lookup.

    continuity_day is 1 on first appearance and increments on each consecutive
    day, resetting to 1 after a gap. continuity_state:
      new        first ever appearance (no prior history at all)
      rebounding reappearing after a gap (prior history existed but not
                 yesterday)
      day2       second consecutive day
      day3plus   third consecutive day or beyond

    prior_continuity_day is the stored continuity_day from yesterday's row for
    this (market, query_group), or None if the topic did not appear yesterday.
    appeared_yesterday gates the consecutive-day increment. had_recent_gap is
    True when the topic appeared somewhere in the recent lookback window but
    not yesterday (a rebound).
    """
    if appeared_yesterday and prior_continuity_day is not None and prior_continuity_day >= 1:
        day = int(prior_continuity_day) + 1
        state = "day2" if day == 2 else "day3plus"
        return day, state
    # Not consecutive with yesterday: either a brand-new topic or a rebound.
    if had_recent_gap:
        return 1, "rebounding"
    return 1, "new"


# Continuity lookback: how many days back to scan for a prior appearance when
# deciding new vs rebounding. A topic seen anywhere in this window but not
# yesterday is a rebound; one never seen in the window is new.
CONTINUITY_LOOKBACK_DAYS = 14


def fetch_continuity_lookup(
    trend_date: date,
    lookback_days: int = CONTINUITY_LOOKBACK_DAYS,
) -> dict[tuple[str, str], dict]:
    """Prior-day continuity state per (market, query_group) in ONE query.

    Reads trend_scores for the lookback window before ``trend_date`` and
    returns, per (market, query_group): the stored continuity_day from
    yesterday (``prior_continuity_day``, None if it did not appear yesterday),
    whether it appeared yesterday (``appeared_yesterday``), and whether it
    appeared anywhere else in the window (``had_recent_gap`` True when it has
    history in the window but not yesterday). compute_trend_scores turns these
    into continuity_day / continuity_state.

    Defensive: any failure returns {} so the OFF-path and a BQ blip both leave
    every topic at day 1 / 'new', never crashing the run.
    """
    from src.utils.bigquery import run_query

    sql = """
        SELECT
          market,
          query_group,
          MAX(IF(trend_date = DATE_SUB(@trend_date, INTERVAL 1 DAY),
                 continuity_day, NULL)) AS prior_continuity_day,
          MAX(IF(trend_date = DATE_SUB(@trend_date, INTERVAL 1 DAY),
                 1, 0)) AS appeared_yesterday,
          MAX(IF(trend_date < DATE_SUB(@trend_date, INTERVAL 1 DAY),
                 1, 0)) AS appeared_before_yesterday
        FROM `{project}.{dataset}.trend_scores`
        WHERE trend_date BETWEEN
              DATE_SUB(@trend_date, INTERVAL @lookback_days DAY)
              AND DATE_SUB(@trend_date, INTERVAL 1 DAY)
        GROUP BY market, query_group
    """
    params = {"trend_date": trend_date, "lookback_days": int(lookback_days)}
    out: dict[tuple[str, str], dict] = {}
    try:
        df = run_query(sql, params=params)
    except Exception as exc:
        logger.warning("Continuity lookup failed, every topic treated as new: %s", exc)
        return out
    if df.empty:
        return out
    for _, row in df.iterrows():
        key = (str(row["market"]), str(row["query_group"]))
        appeared_yesterday = bool(int(row["appeared_yesterday"] or 0))
        appeared_before = bool(int(row["appeared_before_yesterday"] or 0))
        prior_day = row["prior_continuity_day"]
        out[key] = {
            "prior_continuity_day": None if pd.isna(prior_day) else int(prior_day),
            "appeared_yesterday": appeared_yesterday,
            # Rebound = had history in the window but did not appear yesterday.
            "had_recent_gap": appeared_before and not appeared_yesterday,
        }
    return out


# Seed score defaults. Live values come from scoring.yaml's seed_score block;
# these keep the helper correct and unit-testable when a key is absent.
_SEED_DEFAULTS = {
    "audience_weights": {
        "genz": 0.0,
        "slang": 0.25,
        "engagement": 0.40,
        "creator_spread": 0.35,
    },
    "creator_spread_cap": 25.0,
    "visual_audio_families": ["ensemble", "youtube", "music"],
    "format_floor": 0.5,
    "tone_safe_threshold": 0.35,
    "tone_unsafe_multiplier": 0.6,
}


def _seed_breakdown(
    *,
    genz: float,
    slang: float,
    engagement_score: float,
    creator_spread: int,
    visual_audio_share: float,
    tone_score: float,
    tone_rows: int,
    cfg: dict | None = None,
) -> dict:
    """The seed score and its components, so the tool can show WHY it scores.

    Returns {seed_score, audience_fit, format_fit, safety_gate,
    visual_audio_share}, each rounded to 4 dp. The decomposition is persisted on
    trend_scores so the tool's clickable breakdown reads the same numbers the
    score was built from, never a re-derivation. Pure: bad inputs clamp, never
    raises.

    audience_fit  weighted blend of slang, engagement, and creator reach.
    format_fit    floor + (1-floor) * share of items on visual/audio-native
                  channel families (TikTok/IG, YouTube, music).
    safety_gate   down-weight (not zero) when GDELT tone is present and below
                  the safe threshold, so a risky trend reads lower but still
                  shows its positioning.
    """
    cfg = cfg or {}

    def _clamp01(x: float) -> float:
        try:
            v = float(x)
        except (TypeError, ValueError):
            return 0.0
        if math.isnan(v):
            return 0.0
        return max(0.0, min(v, 1.0))

    aw = {**_SEED_DEFAULTS["audience_weights"], **(cfg.get("audience_weights") or {})}
    cap = float(cfg.get("creator_spread_cap", _SEED_DEFAULTS["creator_spread_cap"])) or 25.0
    floor = float(cfg.get("format_floor", _SEED_DEFAULTS["format_floor"]))
    tone_safe = float(cfg.get("tone_safe_threshold", _SEED_DEFAULTS["tone_safe_threshold"]))
    tone_mult = float(cfg.get("tone_unsafe_multiplier", _SEED_DEFAULTS["tone_unsafe_multiplier"]))

    creator_norm = min(max(float(creator_spread or 0), 0.0) / cap, 1.0)
    audience_fit = (
        aw["slang"] * _clamp01(slang)
        + aw["engagement"] * _clamp01(engagement_score)
        + aw["creator_spread"] * creator_norm
    )

    share = _clamp01(visual_audio_share)
    format_fit = floor + (1.0 - floor) * share

    # tone_rows == 0 means no GDELT signal: do not penalise (same convention as
    # the trend_score tone redistribution). Otherwise gate on the tone mean.
    if int(tone_rows or 0) > 0 and float(tone_score or 0.0) < tone_safe:
        safety_gate = tone_mult
    else:
        safety_gate = 1.0

    seed = round(min(audience_fit * format_fit * safety_gate, 1.0), 4)
    return {
        "seed_score": seed,
        "audience_fit": round(audience_fit, 4),
        "format_fit": round(format_fit, 4),
        "safety_gate": round(safety_gate, 4),
        "visual_audio_share": round(share, 4),
    }


def compute_seed_score(
    *,
    genz: float,
    slang: float,
    engagement_score: float,
    creator_spread: int,
    visual_audio_share: float,
    tone_score: float,
    tone_rows: int,
    cfg: dict | None = None,
) -> float:
    """The seed score as a single 0..1 float. Thin wrapper over _seed_breakdown
    so existing callers and tests keep their contract."""
    return _seed_breakdown(
        genz=genz,
        slang=slang,
        engagement_score=engagement_score,
        creator_spread=creator_spread,
        visual_audio_share=visual_audio_share,
        tone_score=tone_score,
        tone_rows=tone_rows,
        cfg=cfg,
    )["seed_score"]


def compute_trend_scores(
    market_counts: dict,
    trend_date: date,
    scored_at: datetime,
    velocity_scores: dict | None = None,
    velocity_windows: dict | None = None,
    continuity_lookup: dict | None = None,
) -> list[dict]:
    """Composite trend score per (market, query_group) using scoring.yaml weights.

    velocity_scores: optional dict[(market, query_group)] -> float. When provided,
    overrides the default 0.0 velocity contribution (from src.scoring.velocity).
    search_velocity_score is sourced from per-group enrichment averages.

    velocity_windows: optional dict[(market, query_group)] -> {velocity_score_7d,
    velocity_score_30d, median_item_count_7d} from compute_velocity_windows_for_today.
    Drives the Wave 1 momentum_label + lifecycle_phase. Display-only: the
    weighted composite is NOT changed. Absent -> the new columns stay 0/empty.

    continuity_lookup: optional dict[(market, query_group)] -> {prior_continuity_day,
    appeared_yesterday, had_recent_gap} from the prior-day trend_scores lookup.
    Drives continuity_day + continuity_state. Absent -> day 1 / 'new'.
    """
    scoring = load_scoring()
    w = scoring.get("weights", {})
    # Seed score (Jo, 22 Jun): worth-seeding-for-Nanobanana/Lyria signal.
    # Always computed and stored (additive column); the render flag controls
    # only the chip + topline. Disable the whole column via seed_score.enabled.
    seed_cfg = scoring.get("seed_score", {}) or {}
    seed_on = bool(seed_cfg.get("enabled", True))
    va_families = set(
        seed_cfg.get("visual_audio_families", _SEED_DEFAULTS["visual_audio_families"])
    )
    rows = []
    velocity_scores = velocity_scores or {}
    velocity_windows = velocity_windows or {}
    continuity_lookup = continuity_lookup or {}
    lifecycle_on = _lifecycle_enabled()
    continuity_on = _continuity_badges_enabled()
    momentum_in_composite = _momentum_in_composite()
    # Deferred composite feed (MOMENTUM_IN_COMPOSITE). Weight stays 0 in Wave 1
    # so reading it here cannot move any score; the bundle still sums to 1.0.
    momentum_weight = float(w.get("momentum", 0.0)) if momentum_in_composite else 0.0
    for (market, query_group), stats in market_counts.items():
        item_count = stats["item_count"]
        source_diversity = stats["source_diversity"]
        channel_diversity = int(stats.get("channel_diversity", 0))
        semantic_corroboration_families = int(stats.get("semantic_corroboration_families", 0))
        semantic_corroboration_entities = int(stats.get("semantic_corroboration_entities", 0))
        engagement_item_count = float(stats.get("engagement_item_count", 0.0))
        regional = stats.get("regional_avg", 0.0)
        genz = stats.get("genz_avg", 0.0)
        slang = stats.get("slang_avg", 0.0)
        watchlist = stats.get("watchlist_avg", 0.0)
        engagement_sum = stats.get("engagement_sum", 0.0)
        creator_spread = stats.get("creator_spread", 0)
        search_velocity = float(stats.get("search_velocity_avg", 0.0))

        diversity_score = min(source_diversity / 10.0, 1.0)
        creator_score = min(creator_spread / 25.0, 1.0)
        # Engagement normalised on PER-ROW intensity so row count doesn't
        # inflate score. Group with 50 high-signal vlogs and group with 1
        # mega-viral music hit score on average engagement per row, not sum.
        # engagement_sum here is ALREADY the sum of category-weighted,
        # days-since-published normalised per-row engagement (see
        # enrichment.engagement_weighted). Divide by the count of rows that
        # carry real engagement signal (engagement_item_count excludes the
        # structurally-zero catalogue rows, row 096) to get avg per row, then a
        # 5000 per-row ceiling. Fall back to item_count if every row was a
        # zero-engagement type.
        engagement_divisor = engagement_item_count if engagement_item_count > 0 else item_count
        per_row_engagement = float(engagement_sum) / max(engagement_divisor, 1e-9)
        engagement_score = min(per_row_engagement / 5000.0, 1.0)
        velocity_score = float(velocity_scores.get((market, query_group), 0.0))
        search_velocity_score = min(search_velocity, 1.0)

        # Wave 1 momentum windows (display-only). 7d/30d velocity reads plus the
        # 7-day median item_count for lifecycle. Absent -> 0.0 / None, the same
        # as a topic with no history.
        windows = velocity_windows.get((market, query_group), {})
        velocity_score_7d = float(windows.get("velocity_score_7d", 0.0) or 0.0)
        velocity_score_30d = float(windows.get("velocity_score_30d", 0.0) or 0.0)
        median_item_count_7d = windows.get("median_item_count_7d")
        momentum_label = _momentum_label(velocity_score_7d, velocity_score_30d)

        # Tone: when at least one row contributed a parseable V2Tone, use the
        # group mean. When zero rows contributed, redistribute the tone_weight
        # across the other signals rather than pinning to neutral 0.5 (which
        # would bias every no-GDELT group up by 0.025).
        tone_rows = int(stats.get("tone_rows") or 0)
        tone_avg_mean = stats.get("tone_avg_mean")
        tone_weight_nominal = float(w.get("tone_score", 0.05))

        if tone_rows > 0 and tone_avg_mean is not None:
            tone_score = float(tone_avg_mean)
            tone_weight_effective = tone_weight_nominal
        else:
            tone_score = 0.0
            tone_weight_effective = 0.0

        # GCAM emotional-arousal: a second GDELT-only signal, handled exactly like
        # tone. Present (gcam_rows > 0) it rides at its nominal weight; absent its
        # weight is redistributed across the always-present signals alongside any
        # absent tone weight. Ships at weight 0.00, so gcam_weight_nominal is 0:
        # the effective term and absent contribution are both 0 until the weight
        # is raised in scoring.yaml.
        gcam_rows = int(stats.get("gcam_rows") or 0)
        gcam_avg_mean = stats.get("gcam_avg_mean")
        gcam_weight_nominal = float(w.get("gcam_score", 0.0))

        if gcam_rows > 0 and gcam_avg_mean is not None:
            gcam_score = float(gcam_avg_mean)
            gcam_weight_effective = gcam_weight_nominal
        else:
            gcam_score = 0.0
            gcam_weight_effective = 0.0

        # Redistribute the nominal weight of any absent GDELT-only signal (tone
        # and/or gcam) across the always-present signals so composites stay on
        # 0..1 and no-GDELT groups are not penalised.
        absent_weight = (tone_weight_nominal if tone_weight_effective == 0.0 else 0.0) + (
            gcam_weight_nominal if gcam_weight_effective == 0.0 else 0.0
        )
        remaining_weight_sum = 1.0 - absent_weight
        weight_scale = 1.0 / remaining_weight_sum if remaining_weight_sum > 0 else 1.0

        # Momentum carve (MOMENTUM_IN_COMPOSITE). Momentum takes its weight FROM
        # the velocity weight, so the velocity family keeps its total influence and
        # the bundle still sums to 1.0. The momentum signal is min(7d, 30d): a topic
        # earns it only when up on BOTH windows (a sustained climb), so a one-day
        # flash loses velocity weight with no compensating bonus and is demoted.
        # Calibrated 20 Jun, see scripts/calibration/momentum_calibration.py. Off
        # path: momentum_weight is 0, so velocity_weight is the full velocity weight
        # and no momentum term is added, byte-identical to before.
        velocity_weight = w.get("velocity", 0.20) - momentum_weight
        signal_sum = (
            velocity_score * velocity_weight
            + diversity_score * w.get("diversity", 0.16)
            + engagement_score * w.get("engagement", 0.10)
            + creator_score * w.get("creator_spread", 0.12)
            + regional * w.get("regional_score", 0.12)
            + search_velocity_score * w.get("search_velocity_score", 0.15)
            + slang * w.get("slang_score", 0.10)
        )
        if momentum_in_composite and momentum_weight:
            signal_sum += min(velocity_score_7d, velocity_score_30d) * momentum_weight
        composite = (
            signal_sum * weight_scale
            + tone_score * tone_weight_effective
            + gcam_score * gcam_weight_effective
        )

        # Cross-source confirmation, graded by independent CHANNEL FAMILIES.
        # A topic corroborated across more channels (news, ensemble, reddit,
        # brand24, search, music, youtube) is structurally stronger than a
        # single-channel spike. The bonus scales with the channel count rather
        # than a flat bump on any 2-platform topic: the old flat multiplier
        # fired on ~95% of topics (near-constant inflation that just compressed
        # scores toward the cap, no discrimination). It keys on channel_set
        # (via _channel_family), not platform strings, so Brand24's per-URL
        # platform inference and the 6000+ RSS/GDELT news domains cannot
        # manufacture corroboration. bonus = min(beta*(n-1), max_bonus):
        # n=1 -> 1.00 (no corroboration), n=2 -> 1.05, n=3 -> 1.10, n>=4 -> 1.15.
        # beta and the cap are fitted so the live median topic (n=4) keeps its
        # prior 1.15, leaving the tier floors calibrated. Composite stays <= 1.0.
        beta = float(scoring.get("cross_source_bonus_per_channel", 0.05))
        max_bonus = float(scoring.get("cross_source_max_bonus", 0.15))
        cross_source_multiplier = 1.0 + min(beta * max(channel_diversity - 1, 0), max_bonus)
        trend_score = round(min(composite * cross_source_multiplier, 1.0), 4)

        # Phase 0 corroboration scorer (shadow / inert). Computes two honest
        # corroboration numbers (factual vs social, never combined), a confidence
        # tier, and a recency, from data already aggregated. Does NOT touch
        # trend_score. None-safe: an absent channel_family_weights or
        # freshest_published_at yields a "thin" / unknown-recency row.
        corroboration = compute_corroboration(
            channel_family_weights=stats.get("channel_family_weights", {}) or {},
            freshest_published_at=stats.get("freshest_published_at"),
            now=scored_at,
            search_velocity=search_velocity,
            tone_rows=tone_rows,
            config=scoring,
        )

        # Seed score (additive, audience + creative-format fit). Share of the
        # topic's items on the visual/audio-native channel families a Nanobanana
        # /Lyria activation rides on. Independent of trend_score by design.
        channel_family_weights = stats.get("channel_family_weights", {}) or {}
        va_sum = sum(wt for fam, wt in channel_family_weights.items() if fam in va_families)
        visual_audio_share = va_sum / max(float(item_count), 1e-9)
        seed = (
            _seed_breakdown(
                genz=genz,
                slang=slang,
                engagement_score=engagement_score,
                creator_spread=creator_spread,
                visual_audio_share=visual_audio_share,
                tone_score=tone_score,
                tone_rows=tone_rows,
                cfg=seed_cfg,
            )
            if seed_on
            else {
                "seed_score": 0.0,
                "audience_fit": 0.0,
                "format_fit": 0.0,
                "safety_gate": 0.0,
                "visual_audio_share": 0.0,
            }
        )
        seed_score = seed["seed_score"]

        # Lifecycle phase (gated). When LIFECYCLE_ENABLED is off the column
        # stays empty (None), render no-ops, byte-identical to today.
        lifecycle_phase = None
        if lifecycle_on:
            lifecycle_phase = _lifecycle_phase(
                velocity_14d=velocity_score,
                velocity_7d=velocity_score_7d,
                item_count=item_count,
                median_item_count_7d=median_item_count_7d,
            )

        # Continuity day + state (gated). When CONTINUITY_BADGES_ENABLED is off
        # the columns stay empty (None) and the badge render no-ops.
        continuity_day = None
        continuity_state = None
        if continuity_on:
            cont = continuity_lookup.get((market, query_group), {})
            continuity_day, continuity_state = _continuity(
                prior_continuity_day=cont.get("prior_continuity_day"),
                appeared_yesterday=bool(cont.get("appeared_yesterday", False)),
                had_recent_gap=bool(cont.get("had_recent_gap", False)),
            )

        rows.append(
            {
                "trend_date": trend_date,
                "market": market,
                "query_group": query_group,
                "cycle_id": None,
                # Cast float item_count (now fractional due to 1/N multi-assign
                # weights) back to INT64 at the BQ write boundary. Target
                # trend_scores.item_count column is INT64.
                "item_count": round(float(item_count)),
                "engagement_sum": float(engagement_sum),
                "source_diversity": source_diversity,
                # 024: persist the channel-family count the cross-source
                # multiplier keys on, so accuracy_watchdog can recompute the
                # exact multiplier for this row instead of accepting a band.
                "channel_diversity": channel_diversity,
                "creator_spread": creator_spread,
                "velocity_score": velocity_score,
                "engagement_score": engagement_score,
                "diversity_score": round(diversity_score, 4),
                "creator_score": round(creator_score, 4),
                "regional_score": round(regional, 4),
                "genz_score": round(genz, 4),
                "watchlist_score": round(watchlist, 4),
                "slang_score": round(slang, 4),
                "search_velocity_score": search_velocity_score,
                "tone_score": round(tone_score, 4),
                "tone_rows": tone_rows,
                "gcam_score": round(gcam_score, 4),
                "gcam_rows": gcam_rows,
                "trend_score": trend_score,
                "seed_score": seed_score,
                # Seed components, persisted so the tool's clickable breakdown
                # reads the same numbers the score was built from.
                "seed_audience_fit": seed["audience_fit"],
                "seed_format_fit": seed["format_fit"],
                "seed_safety_gate": seed["safety_gate"],
                "visual_audio_share": seed["visual_audio_share"],
                # Wave 1 momentum (display-only, stored unconditionally). 7d/30d
                # velocity reads + the rising/building/steady/cooling label.
                "velocity_score_7d": velocity_score_7d,
                "velocity_score_30d": velocity_score_30d,
                "momentum_label": momentum_label,
                # Wave 1 lifecycle (gated) + continuity (gated). None when their
                # flag is off so the BQ column stays NULL and the render no-ops.
                "lifecycle_phase": lifecycle_phase,
                "continuity_day": continuity_day,
                "continuity_state": continuity_state,
                # Phase 0 corroboration (shadow). Two honest numbers (factual vs
                # social, never combined) + tier + recency. Inert: nothing reads
                # them yet; trend_score is unchanged.
                "factual_corroboration": corroboration["factual_corroboration"],
                "social_corroboration": corroboration["social_corroboration"],
                "confidence_tier": corroboration["confidence_tier"],
                "corroboration_recency_hours": corroboration["corroboration_recency_hours"],
                **(
                    {
                        "n_factual": corroboration["n_factual"],
                        "n_social": corroboration["n_social"],
                    }
                    if os.environ.get("CORROB_COUNTS_ENABLED", "false").lower() == "true"
                    else {}
                ),
                # Forward Phase 2 (semantic corroboration, shadow). Inert: stored
                # for the 10-day comparison against the structural channel_diversity
                # multiplier, nothing reads them and trend_score is unchanged.
                "semantic_corroboration_families": semantic_corroboration_families,
                "semantic_corroboration_entities": semantic_corroboration_entities,
                "scored_at": scored_at,
            }
        )
    return rows


# Wave 1 classification-layer instrumentation. The five layers whose row counts
# land in pipeline_runs (drop / unclassified are not persisted as columns:
# unclassified feeds the labelling-rate denominator, drop is excluded from it).
_CLASSIFICATION_LAYER_COLUMNS = {
    "brand24": "classification_brand24_rows",
    "regex": "classification_regex_rows",
    "gdelt": "classification_gdelt_rows",
    "slang": "classification_slang_rows",
    "embedding": "classification_embedding_rows",
}


def _classification_layer_counts(df_enriched_full: pd.DataFrame) -> dict[str, int]:
    """Per-layer row counts from the enriched frame's classification_layer column.

    Returns {} when the column is absent (CLASSIFICATION_INSTRUMENTATION_ENABLED
    off) so the OFF path writes no per-layer counts. Keys are the raw layer
    names (brand24 / regex / gdelt / slang / embedding / unclassified / drop);
    log_pipeline_run maps the five persisted ones to columns and uses
    unclassified for the labelling rate.
    """
    if "classification_layer" not in df_enriched_full.columns:
        return {}
    counts: dict[str, int] = {}
    for value, n in df_enriched_full["classification_layer"].value_counts().items():
        if value is None:
            continue
        counts[str(value)] = int(n)
    return counts


def _classification_run_columns(layer_counts: dict[str, int]) -> dict[str, int | float | None]:
    """Map per-layer counts to the pipeline_runs classification columns.

    Returns the five per-layer count columns plus labelling_rate_percent. When
    layer_counts is empty (instrumentation off) every value is None so the
    columns stay NULL in BQ and the OFF path is byte-identical.

    labelling_rate_percent = 100 * classified / (classified + unclassified).
    The 'drop' bucket (foreign-guard / Brand24 aggregated-label noise) is
    excluded from both numerator and denominator: those rows are deliberately
    removed, not failures to classify.
    """
    columns: dict[str, int | float | None] = dict.fromkeys(_CLASSIFICATION_LAYER_COLUMNS.values())
    columns["labelling_rate_percent"] = None
    if not layer_counts:
        return columns

    for layer, column in _CLASSIFICATION_LAYER_COLUMNS.items():
        columns[column] = int(layer_counts.get(layer, 0))

    classified = sum(int(layer_counts.get(layer, 0)) for layer in _CLASSIFICATION_LAYER_COLUMNS)
    unclassified = int(layer_counts.get("unclassified", 0))
    denom = classified + unclassified
    if denom > 0:
        columns["labelling_rate_percent"] = round(100.0 * classified / denom, 2)
    return columns


def _briefs_per_market(briefs_by_topic: dict | None) -> dict[str, int]:
    """Count briefs per market from a {(market, topic): brief_dict} mapping.

    Accepts either the raw GenerateBriefsReport.briefs dict (keyed by
    (market, query_group) tuple) or the post-process briefs_by_topic dict
    (same key shape). Returns {} on None / unexpected shape so the caller
    never crashes; downstream code falls back to 0 per market.
    """
    counts: dict[str, int] = dict.fromkeys(MARKETS, 0)
    if not briefs_by_topic:
        return counts
    try:
        for key in briefs_by_topic:
            if isinstance(key, tuple) and len(key) >= 1:
                m = str(key[0])
                if m in counts:
                    counts[m] += 1
    except Exception as exc:
        logger.warning("Could not count briefs per market: %s", exc)
    return counts


# Per-surface row counts for feeds that ride inside a parent connector's
# dataframe: top_terms rides in bigquery_trends, youtube_playlist_items in
# youtube, the three brand24_* feeds in brand24. The parent connector key
# already counts these rows (and feeds total_rows), so they are surfaced
# separately for v_pipeline_health / morning-check visibility and are NOT
# re-added to the total. Each entry maps a pipeline_runs sub-source key to the
# dataframe column and value that identify its rows. brand24_daily_metric
# joins on content_type because its query_group is the plural
# "brand24_daily_metrics".
_SUB_SOURCE_COUNTS = (
    ("top_terms", "query_group", "google_trends_top"),
    ("youtube_playlist_items", "query_group", "youtube_playlist_items"),
    ("brand24_mention_sentiment", "content_type", "brand24_mention_sentiment"),
    ("brand24_mention_reach", "content_type", "brand24_mention_reach"),
    ("brand24_daily_metric", "content_type", "brand24_daily_metric"),
)


def _sub_source_counts(df_market: pd.DataFrame) -> dict[str, int]:
    """Row counts for sub-feeds bundled inside a parent connector's frame.

    Keyed to match the pipeline_runs sub-source columns. Defensive against a
    missing column: a connector that did not run leaves its query_group /
    content_type out of the concatenated frame, in which case the count is 0.
    """
    counts: dict[str, int] = {}
    for key, column, value in _SUB_SOURCE_COUNTS:
        if column in df_market.columns:
            counts[key] = int((df_market[column] == value).sum())
        else:
            counts[key] = 0
    return counts


def log_pipeline_run(
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    market_source_counts: dict,
    score_rows: list,
    errors: list,
    unclassified_by_market: dict[str, int] | None = None,
    skip_markets: set[str] | None = None,
    briefs_by_market: dict[str, int] | None = None,
    classification_layer_by_market: dict[str, dict[str, int]] | None = None,
):
    """Write one pipeline_runs row per market that actually ran this attempt.

    skip_markets carries any market that the idempotency guard short-
    circuited at the top of run(). Those markets already have a
    status='success' row from the prior attempt; writing another would
    double-count in the dashboard.

    briefs_by_market is the per-market count of Phase 2 briefs that
    landed in trend_analysis for this run. Defaults to 0 per market when
    None, which is the pre-fix behaviour (kept so callers that do not
    care about Phase 2 do not need to pass it).

    classification_layer_by_market is the Wave 1 per-market {layer_name: count}
    instrumentation. When None / empty (CLASSIFICATION_INSTRUMENTATION_ENABLED
    off) the five per-layer columns and labelling_rate_percent stay NULL.
    """
    env = os.environ.get("TRENDS_ENV", "dev")
    skip_markets = skip_markets or set()
    briefs_by_market = briefs_by_market or {}
    classification_layer_by_market = classification_layer_by_market or {}
    rows = []
    for market in MARKETS:
        if market in skip_markets:
            continue
        src_counts = market_source_counts.get(market, {})
        market_errors = [e for e in errors if isinstance(e, dict) and e.get("market") == market]
        market_error_strings = [
            f"{e.get('source', '?')}: {e.get('error', '')}" for e in market_errors
        ]
        market_scored = sum(1 for r in score_rows if r.get("market") == market)
        # Sum connector keys only. Sub-source rollups (top_terms, youtube
        # playlist items, the brand24_* feeds) also sit in src_counts for their
        # own pipeline_runs columns, but each rides inside a parent connector's
        # count, so summing all values here would double-count total_rows.
        total = sum(int(src_counts.get(k, 0) or 0) for k, _ in CONNECTORS)
        # Wave 1 classification instrumentation. layer_counts is empty when the
        # flag is off, so every per-layer column and the labelling rate stay
        # None and the OFF path is byte-identical. labelling_rate_percent =
        # 100 * (classified) / (classified + unclassified); the 'drop'
        # bucket (foreign-guard / Brand24 noise) is excluded from both.
        layer_counts = classification_layer_by_market.get(market, {}) or {}
        classification_columns = _classification_run_columns(layer_counts)
        # A per-feed or per-endpoint fetch failure (fatal=False) is expected
        # churn: the connector caught it and kept going, so it must not
        # downgrade the whole market. Only a fatal error (a connector or
        # pipeline crash, or any untagged error for backward-compat) moves a
        # market off success. The degraded feeds still ride in the errors
        # column above for visibility and pruning.
        market_fatal = [e for e in market_errors if e.get("fatal", True)]
        if total == 0 and not market_fatal:
            status = "empty"
        elif total == 0 and market_fatal:
            status = "failed"
        elif market_fatal:
            status = "partial"
        else:
            status = "success"
        rows.append(
            {
                "run_id": run_id,
                "environment": env,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": status,
                "market": market,
                "brand24_rows": int(src_counts.get("brand24", 0)),
                "bigquery_trends_rows": int(src_counts.get("bigquery_trends", 0)),
                "ensemble_rows": int(src_counts.get("ensemble", 0)),
                "youtube_rows": int(src_counts.get("youtube", 0)),
                "gdelt_rows": int(src_counts.get("gdelt", 0)),
                "rss_rows": int(src_counts.get("rss", 0)),
                # Reddit (7th connector, shipped 27 May 2026). Column added
                # via scripts/migrations/add_reddit_rows_column.py applied
                # before this code went live.
                "reddit_rows": int(src_counts.get("reddit", 0)),
                # Wave 1 + Wave 2 per-source rollups (28 May 2026). Columns
                # added via scripts/migrations/add_wave1_wave2_rows_columns.py.
                # apple_music is the 8th connector LIVE today (commit 66abb7a);
                # the rest stay 0 until their Wave 2 connector flags flip.
                "apple_music_rows": int(src_counts.get("apple_music", 0)),
                "top_terms_rows": int(src_counts.get("top_terms", 0)),
                "brand24_mention_sentiment_rows": int(
                    src_counts.get("brand24_mention_sentiment", 0)
                ),
                "brand24_mention_reach_rows": int(src_counts.get("brand24_mention_reach", 0)),
                "brand24_daily_metric_rows": int(src_counts.get("brand24_daily_metric", 0)),
                "youtube_playlist_items_rows": int(src_counts.get("youtube_playlist_items", 0)),
                # Wave 2 free no-auth connectors (19 Jun 2026), dark on ship.
                # Columns added via scripts/migrations/add_wave2_connector_rows.py
                # which MUST be applied before this code goes live: a key for a
                # column not yet in the pipeline_runs schema fails the
                # WRITE_APPEND load. Both stay 0 until wikipedia.enabled /
                # bluesky.enabled flip in sources.yaml.
                "wikipedia_rows": int(src_counts.get("wikipedia", 0)),
                "bluesky_rows": int(src_counts.get("bluesky", 0)),
                # Google Trends trending RSS (3 Jul 2026, dark). Column added
                # via scripts/migrations/add_google_trends_rss_rows_column.py
                # which MUST be applied before this key goes live. Stays 0
                # until google_trends_rss.enabled flips in sources.yaml.
                "google_trends_rss_rows": int(src_counts.get("google_trends_rss", 0)),
                # Apple App Store top-charts (4 Jul 2026, dark). Column added
                # via scripts/migrations/add_app_charts_rows_column.py which
                # MUST be applied before this key goes live. Stays 0 until
                # app_charts.enabled flips in sources.yaml.
                "app_charts_rows": int(src_counts.get("app_charts", 0)),
                # Audiomack + Cloudflare Radar (4 Jul 2026, dark). Columns
                # added via scripts/migrations/add_audiomack_radar_rows_columns.py
                # which MUST be applied before these keys go live. Both stay 0
                # until their flags flip and credentials exist.
                "audiomack_rows": int(src_counts.get("audiomack", 0)),
                "cloudflare_radar_rows": int(src_counts.get("cloudflare_radar", 0)),
                "youtube_scrape_rows": int(src_counts.get("youtube_scrape", 0)),
                "socialcrawl_rows": int(src_counts.get("socialcrawl", 0)),
                # This market's own credit spend, not the run's running total.
                # The rollout proposer medians the daily SUM of this to size
                # burn; without it the ramp reads a declared constant that has
                # no relationship to what the vendor actually billed. Column
                # added via scripts/migrations/add_socialcrawl_credits_column.py
                # which MUST be applied before this key goes live.
                "socialcrawl_credits": SocialCrawlConnector.credits_used_for(market),
                "total_rows": total,
                "trends_scored": market_scored,
                "unclassified_rows": int((unclassified_by_market or {}).get(market, 0)),
                # Per-market count of Phase 2 briefs that landed in
                # trend_analysis. Pre-28 May this was hardcoded to 0
                # regardless of the real brief count, which blinded the
                # health check keyed on this column.
                "briefs_generated": int(briefs_by_market.get(market, 0)),
                # Wave 1 classification instrumentation: five per-layer row
                # counts + labelling_rate_percent. All None when the flag is
                # off (NULL in BQ). Added via add_wave1_columns.py.
                **classification_columns,
                "errors": market_error_strings,
                "notes": "Manual multi-connector run via scripts/run_rss_now.py",
            }
        )
    insert_dataframe(pd.DataFrame(rows), "pipeline_runs")


# Email outcomes the marker row may carry. 'sent', 'dry_run',
# 'skipped_nothing_notable', and 'skipped_already_sent' are healthy (mail went
# out, the run was a deliberate dry run with sending disabled, a legitimately
# quiet day stayed silent, or a sibling run today already sent it); the rest are
# no-email days the morning-check must flag. 'dry_run' is the detector's status
# when EMAIL_ALERTS_ENABLED != true, which is a deliberate no-send, not a
# 'send_failed'.
_EMAIL_STATUS_VALUES = frozenset(
    {
        "sent",
        "dry_run",
        "send_failed",
        "skipped_nothing_notable",
        "skipped_already_sent",
        "render_error",
    }
)
_EMAIL_STATUS_HEALTHY = frozenset(
    {"sent", "dry_run", "skipped_nothing_notable", "skipped_already_sent"}
)


def _log_email_status(
    run_id: str,
    env: str,
    started_at: datetime,
    email_status: str,
) -> None:
    """Write one thin pipeline_runs marker row carrying the email outcome.

    Separate INSERT (not an UPDATE) because pipeline_runs is INSERT-only by
    convention in this repo and a post-load streaming-buffer UPDATE is
    unsafe right after the success rows land. The marker uses
    status='email_audit' (NOT 'success') so the idempotency guard in
    check_already_ran_today.py (WHERE status='success') ignores it and the
    cron skip / retry behaviour is unchanged. market='ALL' because the
    email is one digest covering every market, not per-market.

    Wrapped in its own try/except: a BQ blip writing the marker must never
    crash the run (exit 0 stays exit 0). If both the email AND this marker
    write fail, detection degrades from an explicit status to a missing
    email_audit row, which the bq-snapshot skill also flags.
    """
    if email_status not in _EMAIL_STATUS_VALUES:
        # Defensive: never persist an unexpected value into the column the
        # morning-check reads. Fall back to render_error (fail loud, not
        # silent) so an unknown outcome still surfaces as a no-email day.
        logger.warning("Unexpected email_status %r; recording as render_error", email_status)
        email_status = "render_error"
    try:
        row = {
            "run_id": run_id,
            "environment": env,
            "started_at": started_at,
            "finished_at": datetime.now(UTC),
            "status": "email_audit",
            "market": "ALL",
            "email_status": email_status,
            "notes": "Email outcome marker (see _log_email_status)",
        }
        insert_dataframe(pd.DataFrame([row]), "pipeline_runs")
        health = "ok" if email_status in _EMAIL_STATUS_HEALTHY else "NO EMAIL - investigate"
        print(f"  Logged email outcome to pipeline_runs: email_status={email_status} ({health})")
    except Exception as exc:
        logger.error("Email-status marker write failed (non-fatal): %s", exc, exc_info=True)
        print(f"  Email-status marker write error (non-fatal): {exc}")


def _markets_already_ingested_today() -> set[str]:
    """Return markets that already ingested today (success or partial).

    Used as a market-day idempotency guard at the top of run() so the
    workflow's retry loop does not double-write raw_content,
    enriched_content, or pipeline_runs for any market that already ran.

    Partial markets must be included: re-ingest appends rows (insert-only)
    and double-spends vendor units on the 02:30 fallback.

    Falls back to empty set on any BigQuery exception so the caller never
    crashes due to the idempotency check itself; the worst case is a
    duplicate row, which is the pre-fix behaviour, not a regression.
    """
    try:
        from src.utils.bigquery import get_client

        client = get_client()
        dataset = get_dataset()
        sql = f"""
        SELECT DISTINCT market
        FROM `{client.project}.{dataset}.pipeline_runs`
        WHERE DATE(started_at) = CURRENT_DATE()
          AND status IN ('success', 'partial')
          AND market IS NOT NULL
          AND market != 'ALL'
        """
        return {str(r.market) for r in client.query(sql).result()}
    except Exception as exc:
        logger.warning(
            "Could not check today's already-ingested markets, "
            "running all markets (no idempotency guard): %s",
            exc,
        )
        return set()


# Back-compat alias for tests and external callers.
_markets_already_succeeded_today = _markets_already_ingested_today


def _parse_force_reingest_markets() -> set[str]:
    """Markets to re-ingest despite the idempotency guard (B0b, per-execution only)."""
    raw = os.environ.get("FORCE_REINGEST_MARKETS", "").strip()
    if not raw:
        return set()
    return {m.strip().lower() for m in raw.split(",") if m.strip()}


def _cleanup_market_day_rows(markets: set[str]) -> None:
    """Delete today's ingest rows for forced markets before re-ingest (B0b)."""
    if not markets:
        return
    from google.cloud import bigquery as bq
    from src.utils.bigquery import get_client, get_dataset

    client = get_client()
    dataset = get_dataset()
    ds = f"`{client.project}.{dataset}`"
    params = [bq.ArrayQueryParameter("markets", "STRING", sorted(markets))]
    for table, date_col in (
        ("raw_content", "DATE(collected_at)"),
        ("enriched_content", "DATE(collected_at)"),
    ):
        sql = f"""
        DELETE FROM {ds}.{table}
        WHERE market IN UNNEST(@markets)
          AND {date_col} = CURRENT_DATE()
        """
        client.query(
            sql,
            job_config=bq.QueryJobConfig(query_parameters=params),
        ).result()
    sql_runs = f"""
    DELETE FROM {ds}.pipeline_runs
    WHERE market IN UNNEST(@markets)
      AND DATE(started_at) = CURRENT_DATE()
      AND market != 'ALL'
    """
    client.query(
        sql_runs,
        job_config=bq.QueryJobConfig(query_parameters=params),
    ).result()


def _rehydrate_fallback_email_inputs(
    trend_date: date,
    score_rows: list,
    briefs_by_topic: dict | None,
    daily_summary_dict: dict | None,
) -> tuple[list, dict | None, dict | None]:
    """Reload scores, briefs, and summary from BQ when every market was skipped.

    The fallback cron skips all ingestion but still needs in-memory state for
    the email step after a primary run that failed send or only partially ran.
    """
    from google.cloud import bigquery as bq
    from src.alerts.brief_loader import load_briefs_by_topic_from_bq
    from src.utils.bigquery import get_client

    client = get_client()
    dataset = get_dataset()
    ds_path = f"{client.project}.{dataset}"

    if not score_rows:
        try:
            sql = f"""
            SELECT market, query_group, trend_score, velocity_score, item_count
            FROM `{ds_path}.trend_scores`
            WHERE trend_date = @trend_date
            """
            job = client.query(
                sql,
                job_config=bq.QueryJobConfig(
                    query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
                ),
            )
            score_rows = [
                {
                    "market": str(r.market),
                    "query_group": str(r.query_group),
                    "trend_score": float(r.trend_score or 0.0),
                    "velocity_score": float(r.velocity_score or 0.0),
                    "item_count": int(r.item_count or 0),
                }
                for r in job.result()
            ]
            if score_rows:
                print(f"  Fallback rehydrate: loaded {len(score_rows)} trend_scores rows from BQ")
        except Exception as exc:
            logger.warning("Fallback score rehydrate failed (non-fatal): %s", exc)

    if not briefs_by_topic:
        try:
            briefs_by_topic = load_briefs_by_topic_from_bq(trend_date)
            if briefs_by_topic:
                print(f"  Fallback rehydrate: loaded {len(briefs_by_topic)} briefs from BQ")
        except Exception as exc:
            logger.warning("Fallback brief rehydrate failed (non-fatal): %s", exc)

    if daily_summary_dict is None:
        try:
            sql = f"""
            SELECT summary_text, through_line, call_to_action,
                   key_topics, rising_topics, seed_recommend
            FROM `{ds_path}.daily_summary`
            WHERE trend_date = @trend_date
            ORDER BY generated_at DESC
            LIMIT 1
            """
            job = client.query(
                sql,
                job_config=bq.QueryJobConfig(
                    query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
                ),
            )
            rows = list(job.result())
            if rows:
                r = rows[0]
                daily_summary_dict = {
                    "summary_text": r.summary_text or "",
                    "through_line": r.through_line or "",
                    "call_to_action": r.call_to_action or "",
                    "key_topics": list(r.key_topics or []),
                    "rising_topics": list(r.rising_topics or []),
                    "seed_recommend": r.seed_recommend or "",
                }
                print("  Fallback rehydrate: loaded daily_summary from BQ")
        except Exception as exc:
            logger.warning("Fallback daily_summary rehydrate failed (non-fatal): %s", exc)

    return score_rows, briefs_by_topic, daily_summary_dict


def _email_already_sent_today() -> bool:
    """True if today already carries an email_audit marker with email_status='sent'.

    Both the GitHub schedule (02:00-03:30 UTC) and the Cloud Run job (05:00 UTC)
    run this script and both reach the unconditional email send, but only the
    GitHub workflow has a full-skip guard step; the Cloud Run job runs the script
    directly and the market-day guard above skips only ingestion, not the send.
    Without this, a second run after the first already emailed would dispatch a
    DUPLICATE digest to stakeholders. This guards the send itself. It keys on the
    'sent' marker specifically, so if the first run's email failed (render_error)
    a later run still sends. Fail-safe: any error returns False so the email goes
    out (a duplicate is recoverable; a missed digest is worse).
    """
    try:
        from src.utils.bigquery import get_client

        client = get_client()
        dataset = get_dataset()
        sql = f"""
        SELECT COUNT(*) AS n
        FROM `{client.project}.{dataset}.pipeline_runs`
        WHERE DATE(started_at) = CURRENT_DATE()
          AND status = 'email_audit'
          AND email_status = 'sent'
        """
        row = next(iter(client.query(sql).result()))
        return int(row.n or 0) > 0
    except Exception as exc:
        logger.warning(
            "Could not check whether today's email already sent; sending anyway: %s",
            exc,
        )
        return False


def _fetch_one_connector(market: str, source_key: str, connector_class: type):
    """Run safe_fetch for one connector. Each thread owns its own instance."""
    try:
        connector = connector_class(market=market)
        df_fetched = connector.safe_fetch()
        failures = list(getattr(connector, "_fetch_failures", [])) + list(
            getattr(connector, "_endpoint_failures", [])
        )
        wave1_global = (
            source_key == "socialcrawl"
            and getattr(SocialCrawlConnector._funded_context, "stage_name", None)
            == "stage_1_wave_1"
        )
        if not df_fetched.empty and not wave1_global:
            df_fetched = df_fetched.copy()
            df_fetched["market"] = market
        return source_key, df_fetched, failures, None
    except Exception as exc:
        return source_key, connector_class.empty_dataframe(), [], exc


# YouTube video id inside a watch / youtu.be / shorts / embed url. Both the API
# connector and the yt-dlp scrape connector emit one of these shapes.
_YT_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def _youtube_video_id(url):
    """Extract the 11-char YouTube video id from a url, or None."""
    if not isinstance(url, str) or not url:
        return None
    match = _YT_VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def dedup_youtube_scrape_frames(market_frames, decisions=None):
    """Drop youtube_scrape rows whose video already came in via the youtube API.

    The API connector and the yt-dlp scrape connector run the same query terms,
    so the same video can land twice per market and inflate engagement and
    topic row_count in scoring. The API row wins (richer stats). Scrape rows
    whose url yields no video id (malformed) are kept. Frames without youtube
    rows pass through untouched. Audit finding E-1. When ``decisions`` is a
    list, every dropped row is recorded there by frame and row position with
    the reason, so telemetry reads the drop from this decision, never from a
    row count difference.
    """
    api_ids: set[str] = set()
    for df in market_frames:
        if df.empty or "url" not in df.columns or "query_group" not in df.columns:
            continue
        api_mask = (df.get("platform") == "youtube") & (df["query_group"] != SCRAPE_QUERY_GROUP)
        for url in df.loc[api_mask, "url"]:
            vid = _youtube_video_id(url)
            if vid:
                api_ids.add(vid)
    if not api_ids:
        return market_frames

    deduped = []
    for frame_index, df in enumerate(market_frames):
        if df.empty or "url" not in df.columns or "query_group" not in df.columns:
            deduped.append(df)
            continue
        scrape_mask = df["query_group"] == SCRAPE_QUERY_GROUP
        if not scrape_mask.any():
            deduped.append(df)
            continue
        dup_mask = scrape_mask & df["url"].map(lambda url: _youtube_video_id(url) in api_ids)
        dropped = int(dup_mask.sum())
        if dropped:
            print(f"  youtube_scrape: dropped {dropped} rows already in youtube API rows")
            if decisions is not None:
                decisions.extend(
                    {
                        "frame_index": frame_index,
                        "row_index": row_index,
                        "reason_code": "youtube_scrape_duplicate",
                    }
                    for row_index, flag in enumerate(dup_mask.tolist())
                    if flag
                )
            df = df.loc[~dup_mask].reset_index(drop=True)
        deduped.append(df)
    return deduped


def _ingest_market_frames(market, market_frames, run_id, started_at, telemetry=None):
    """Concat one market's connector frames, write raw_content, enrich, write
    enriched_content, and aggregate topics.

    Returns ``(rows_raw, rows_enriched, topic_counts, unclassified_run,
    sub_source_counts, layer_counts)``. layer_counts is the Wave 1 per-layer
    classification tally (empty unless instrumentation is on).
    Raises on any failure (a bad enrich, a BigQuery insert error, an
    aggregation crash) so the caller can record the market as a partial
    failure and continue with the other markets, instead of aborting the whole
    run before scoring, briefs, the forecast, and the email.
    ``telemetry`` is the collection only boundary emitter; it receives this
    body's own decision records (the fetched frames, the dedup decisions, the
    raw and enriched rows, the insert count) and is None outside that mode.
    """
    # Cross-connector dedup: the API and scrape youtube connectors share query
    # terms, so drop scrape rows whose video already arrived via the API.
    fetched_frames = market_frames
    dedup_decisions = [] if telemetry is not None else None
    market_frames = dedup_youtube_scrape_frames(market_frames, dedup_decisions)
    df_market = pd.concat(market_frames, ignore_index=True)
    # Per-feed counts for sub-sources that ride inside a parent connector
    # (top_terms, youtube playlist items, the brand24 feeds). Returned for
    # pipeline_runs + morning-check; not part of total_rows.
    sub_source_counts = _sub_source_counts(df_market)
    raw_rows = [
        build_raw_row(row, run_id, started_at) for row in df_market.to_dict(orient="records")
    ]
    df_raw = pd.DataFrame(raw_rows)
    # search_velocity_score rides the raw rows so enrich_dataframe (which reads
    # from raw_rows below) can preserve it, but raw_content has no such column.
    # Drop it from the raw_content frame only; the score lands in
    # enriched_content.search_velocity_score, never raw_content. Without this
    # the WRITE_APPEND load fails for ZA/NG the day SEARCH_VELOCITY_ENABLED flips.
    if "search_velocity_score" in df_raw.columns:
        df_raw = df_raw.drop(columns=["search_velocity_score"])
    # Connectors feed published_at in mixed shapes (ISO strings from
    # Ensemble / Brand24, datetime objects from RSS / YouTube, None for
    # missing). Pandas infers the column dtype as object, which PyArrow
    # then refuses to coerce into BigQuery's TIMESTAMP (int64 micros).
    # Normalise to datetime64[ns, UTC] here so the load is uniform.
    df_raw["published_at"] = pd.to_datetime(df_raw["published_at"], utc=True, errors="coerce")
    rows_raw = insert_dataframe(df_raw, "raw_content")
    print(f"  Wrote {rows_raw} rows to raw_content")
    if telemetry is not None:
        # Telemetry follows the write it describes; it cannot precede or stop it,
        # and the emitter contains its own failures.
        telemetry.emit_collection(market, fetched_frames, dedup_decisions, raw_rows)
        telemetry.count("physical_persisted_rows", int(rows_raw), market=market)

    df_enriched_full = enrich_dataframe(pd.DataFrame(raw_rows), market)
    enriched_rows = []
    for base, enr in zip(raw_rows, df_enriched_full.to_dict(orient="records"), strict=False):
        enriched_rows.append(
            {
                **base,
                "author_handle_norm": enr.get("author_handle_norm", ""),
                "regional_score": float(enr.get("regional_score", 0.0)),
                "genz_score": float(enr.get("genz_score", 0.0)),
                "creator_watchlist_tier": str(enr.get("creator_watchlist_tier", "")),
                "creator_watchlist_score": float(enr.get("creator_watchlist_score", 0.0)),
                "slang_terms": str(enr.get("slang_terms", "")),
                "slang_score": float(enr.get("slang_score", 0.0)),
                "search_velocity_score": float(enr.get("search_velocity_score", 0.0)),
                "tone_avg": _nan_to_none(enr.get("tone_avg")),
                "tone_polarity": _nan_to_none(enr.get("tone_polarity")),
                "query_group": str(enr.get("query_group", base.get("query_group", "")) or "other"),
                "topic_groups": list(enr.get("topic_groups") or []),
                # Wave 1 instrumentation: winning classification layer. None when
                # CLASSIFICATION_INSTRUMENTATION_ENABLED is off (enrichment did
                # not populate it), which lands as a NULL STRING in BQ.
                "classification_layer": (enr.get("classification_layer") or None),
                # Wave 2 social sentiment: lexicon score for social rows. pd.NA
                # (NULL in BQ) when SENTIMENT_LEXICON_ENABLED is off or the row
                # is GDELT; _nan_to_none keeps a genuine 0.0 score intact.
                "sentiment_lexicon_score": _nan_to_none(enr.get("sentiment_lexicon_score")),
                "near_topic": enr.get("near_topic"),
                "near_cosine": _nan_to_none(enr.get("near_cosine")),
            }
        )
    df_enriched = pd.DataFrame(enriched_rows)
    # Same dtype normalisation as df_raw above; enriched_content also
    # has published_at typed TIMESTAMP in BQ.
    df_enriched["published_at"] = pd.to_datetime(
        df_enriched["published_at"], utc=True, errors="coerce"
    )
    rows_enriched = insert_dataframe(df_enriched, "enriched_content")
    print(f"  Wrote {rows_enriched} rows to enriched_content")
    if telemetry is not None:
        telemetry.emit_enrichment(market, raw_rows, enriched_rows)

    topic_counts, unclassified_run = _aggregate_by_topic(df_enriched_full, market)
    print(
        f"  Topic aggregation: {len(topic_counts)} topic buckets, "
        f"{unclassified_run} unclassified rows"
    )
    if telemetry is not None:
        # One bucket per topic aggregated for this market day.
        telemetry.count("topic_day_scores", len(topic_counts), market=market)
    # Wave 1 per-layer classification counts (empty unless instrumentation on).
    layer_counts = _classification_layer_counts(df_enriched_full)
    return (
        rows_raw,
        rows_enriched,
        topic_counts,
        unclassified_run,
        sub_source_counts,
        layer_counts,
    )


def _record_collection_route_outcomes(telemetry, market_source_counts, errors):
    """Hand the terminal route tallies and the recorded failures to the telemetry.

    A route with zero rows and a recorded failure reads as failed, never as
    quiet; the failed source control lives in the coverage report.
    """
    if telemetry is None:
        return None
    telemetry.route_outcomes(market_source_counts, errors)
    return None


def _all_markets_failed(errors, markets_with_data):
    """True when at least one error occurred and no market produced any data.

    Replaces the old ``len(errors) == len(MARKETS)`` heuristic, which could
    false-raise when several connector errors landed on markets that still
    succeeded through their other connectors.
    """
    return bool(errors) and not markets_with_data


# Claim-gathering (markers + helper functions) lives in
# src/analysis/claim_gathering.py so a lightweight reporting script can reuse
# it without importing this module's full connector graph. Imported here so
# the names stay available at scripts.run_rss_now.* for existing call sites
# and test monkeypatches.
from src.analysis.claim_gathering import (
    _filter_claims_to_ledger,
    _reconcile_claims_for_market,
)


def _reconcile_shadow(trend_date, run_id, briefs_by_topic):
    """SHADOW reconcile stage: build the ledger, reconcile each market's brief
    claims against it, and persist the action rows to reconcile_actions.

    Computes and persists an audit trail ONLY. It mutates nothing: it does not
    touch briefs_by_topic, any rendered field, or the daily summary. Returns a
    per-market summary dict {market: (n_claims, n_stale, n_labelled)} for the
    one-line log the caller prints. The whole call is wrapped non-fatal by the
    caller, mirroring the FORECAST / PAN_AFRICAN stages.
    """
    from src.analysis.event_ledger import build_and_persist_ledger
    from src.analysis.gemini_client import GeminiClient
    from src.analysis.reconcile import persist_reconcile_actions, reconcile_claims

    try:
        gemini_client = GeminiClient()
    except Exception:
        logger.warning("reconcile shadow: GeminiClient unavailable, using deterministic states")
        gemini_client = None

    issue_date = trend_date.isoformat()
    ledger_by_market = build_and_persist_ledger(trend_date, client=gemini_client)

    summary = {}
    for market in MARKETS:
        claims = _reconcile_claims_for_market(market, briefs_by_topic)
        claims = _filter_claims_to_ledger(claims, ledger_by_market.get(market, []))
        if not claims:
            summary[market] = (0, 0, 0)
            continue
        actions = reconcile_claims(claims, ledger_by_market.get(market, []), issue_date)
        # SHADOW: persist the audit trail, render NOTHING, mutate NO brief.
        persist_reconcile_actions(trend_date, market, run_id, actions)
        n_stale = sum(1 for a in actions if a.get("action") == "stale")
        n_labelled = sum(1 for a in actions if a.get("action") == "labelled")
        summary[market] = (len(claims), n_stale, n_labelled)
    return summary


_DURABLE_WAVE1_ARTIFACT_CONTEXT = None
_ACTIVE_WAVE1_EXECUTION = None
GDELT_DRY_RUN_RECEIPT_SET_PATH = (
    Path(__file__).parent.parent
    / "configs"
    / "open_intelligence"
    / "gdelt_wave1_dry_run_receipts.json"
)


@dataclass(frozen=True, slots=True)
class Wave1ExecutionInputs:
    artifacts: dict[str, bytes]


@dataclass(frozen=True, slots=True)
class Wave1DurableExecution:
    authority: object
    consumption: object
    capability: object
    wave1_requests: tuple[object, ...]
    gdelt_receipts: tuple[object, ...]
    gdelt_entries: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class Wave1GDELTRuntimeProof:
    query_job_ids: tuple[tuple[str, str], ...]
    query_bytes_processed: tuple[tuple[str, int], ...]
    persistence: object


def _wave1_result_client():
    """The Wave 1 result client with its identity resolved.

    On Cloud Run the compute credential reports its service account as the
    literal "default" until it is refreshed, and the GDELT result query checks
    identity on a fresh client before any request (refused the first funded
    pilot on staging, 4 September 2026). Refreshed the way
    persistence.validate_real_client resolves the staging writer.
    """
    from google.auth.compute_engine.credentials import Credentials as ComputeCredentials
    from google.auth.transport.requests import Request as AuthRequest

    client = bigquery.Client(project="ogilvy-trends-v2", location="US")
    credentials = getattr(client, "_credentials", None)
    if (
        isinstance(credentials, ComputeCredentials)
        and getattr(credentials, "service_account_email", None) == "default"
    ):
        credentials.refresh(AuthRequest())
    return client


def _execute_wave1_gdelt(
    durable: Wave1DurableExecution,
    run_id: str,
) -> Wave1GDELTRuntimeProof:
    from src.analysis.open_intelligence.gdelt_wave1_persistence import (
        build_wave1_gdelt_batch,
        persist_wave1_gdelt_batch,
    )
    from src.ingestion.connectors.gdelt import (
        execute_wave1_gdelt_query,
        wave1_maximum_bytes_billed,
    )

    pairs = tuple(zip(durable.gdelt_entries, durable.gdelt_receipts, strict=True))
    if tuple((entry.name, receipt.name) for entry, receipt in pairs) != (
        ("events", "events"),
        ("gcam", "gcam"),
    ):
        raise RuntimeError("Wave 1 GDELT authority is incomplete")
    client = _wave1_result_client()
    rows_by_name = {}
    job_ids = []
    bytes_processed = []
    for entry, receipt in pairs:
        job = execute_wave1_gdelt_query(
            client,
            entry,
            receipt,
            execution_capability=durable.capability,
        )
        rows = tuple(
            dict(row)
            for row in job.result(
                max_results=10_001,
                retry=None,
                job_retry=None,
            )
        )
        job_id = getattr(job, "job_id", None)
        processed = getattr(job, "total_bytes_processed", None)
        ceiling = wave1_maximum_bytes_billed(
            receipt.total_bytes_processed,
            entry.maximum_bytes_billed,
        )
        if (
            getattr(job, "state", None) != "DONE"
            or not isinstance(job_id, str)
            or not job_id
            or isinstance(processed, bool)
            or not isinstance(processed, int)
            or processed < 0
            or processed > ceiling
        ):
            raise RuntimeError("Wave 1 GDELT result job exceeds its byte ceiling")
        rows_by_name[entry.name] = rows
        job_ids.append((entry.name, job_id))
        bytes_processed.append((entry.name, processed))
    batch = build_wave1_gdelt_batch(
        events_rows=rows_by_name["events"],
        gcam_rows=rows_by_name["gcam"],
        run_id=run_id,
        manifest_sha256=durable.authority.approval.manifest_sha256,
        events_dry_run_receipt_id=durable.gdelt_receipts[0].dry_run_receipt_id,
        gcam_dry_run_receipt_id=durable.gdelt_receipts[1].dry_run_receipt_id,
    )
    persistence = persist_wave1_gdelt_batch(
        client=client,
        batch=batch,
        execution_capability=durable.capability,
        dry_run=False,
    )
    if getattr(persistence, "complete", None) is not True:
        raise RuntimeError("Wave 1 GDELT persistence proof is incomplete")
    return Wave1GDELTRuntimeProof(
        query_job_ids=tuple(job_ids),
        query_bytes_processed=tuple(bytes_processed),
        persistence=persistence,
    )


@dataclass(slots=True)
class _Wave1ArtifactProvider:
    client: object
    observed_at: datetime
    inputs: Wave1ExecutionInputs | None = None

    def read(self, name: str) -> bytes:
        if self.inputs is None:
            self.inputs = Wave1ExecutionInputs(
                artifacts=_build_wave1_execution_artifacts(
                    client=self.client,
                    observed_at=self.observed_at,
                )
            )
        if name not in self.inputs.artifacts:
            raise RuntimeError("Wave 1 execution artifact is unavailable")
        return self.inputs.artifacts[name]


def _execution_approval_artifact_bytes(name: str) -> bytes:
    if not isinstance(_DURABLE_WAVE1_ARTIFACT_CONTEXT, _Wave1ArtifactProvider):
        raise RuntimeError("Wave 1 execution artifact is unavailable")
    return _DURABLE_WAVE1_ARTIFACT_CONTEXT.read(name)


def _wave1_contract(_client: object, _observed_at: datetime) -> dict[str, object]:
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SET_SHA256, WAVE1_ROUTE_SPECS

    routes = {
        name: {
            "phase": spec.phase,
            "credit_cost": spec.credit_cost,
            "maximum_calls": spec.maximum_calls,
            "maximum_debit": spec.maximum_debit,
            "vendor_family": spec.vendor_family,
            "channel_family": spec.channel_family,
            "required_parameters": spec.required_parameters,
            "maximum_pages": spec.maximum_pages,
        }
        for name, spec in sorted(WAVE1_ROUTE_SPECS.items())
    }
    return {
        "contract_version": "wave1-pilot-durable-v1",
        "stage": WAVE1_STAGE,
        "credential_lane": "ogilvy_funded",
        "max_credits": 63,
        "phase_caps": dict(WAVE1_PHASE_CAPS),
        "routes": routes,
        "route_set_sha256": WAVE1_ROUTE_SET_SHA256,
    }


def _wave1_control_query(client: object, sql: str, *, parameters=(), max_results: int):
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False,
            query_parameters=list(parameters),
        ),
        location="US",
        retry=None,
        job_retry=None,
    )
    return tuple(job.result(max_results=max_results, retry=None, job_retry=None))


# Every seed carries the market it came from, and that market is the retained
# geography of the rows the seeded call returns (4 Sep 2026: the v1 manifest
# listed bare terms and URLs, and no row from a seeded call could be placed).
WAVE1_SEED_MANIFEST_CONTRACT = "wave1-r3-seed-manifest-v2"
_WAVE1_SEED_KEYS = {
    "tiktok_music_ids": "music_id",
    "reddit_urls": "url",
    "instagram_search_terms": "term",
    "youtube_short_urls": "url",
}


def _validate_wave1_seed_manifest(payload: object) -> dict[str, object]:
    expected = {
        "replacement_run_id",
        "row_set_digest",
        "source_sha",
        "tiktok_music_ids",
        "reddit_urls",
        "instagram_search_terms",
        "youtube_short_urls",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RuntimeError("Wave 1 seed manifest is invalid")
    limits = {
        "tiktok_music_ids": 5,
        "reddit_urls": 2,
        "instagram_search_terms": 2,
        "youtube_short_urls": 3,
    }
    if (
        payload["replacement_run_id"] != "run_20260903_dynamic_apply_v2_r16"
        or not isinstance(payload["row_set_digest"], str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["row_set_digest"]) is None
        or not isinstance(payload["source_sha"], str)
        or re.fullmatch(r"[0-9a-f]{40}", payload["source_sha"]) is None
    ):
        raise RuntimeError("Wave 1 seed manifest is invalid")
    # A seed list the window cannot fill stays empty (4 Sep 2026); a manifest
    # with nothing to seed at all refuses below.
    for field, maximum in limits.items():
        values = payload[field]
        key = _WAVE1_SEED_KEYS[field]
        if not isinstance(values, list) or not 0 <= len(values) <= maximum:
            raise RuntimeError("Wave 1 seed manifest is invalid")
        seen = []
        for entry in values:
            if (
                not isinstance(entry, dict)
                or set(entry) != {key, "market"}
                or not isinstance(entry[key], str)
                or not entry[key]
                or entry[key] in seen
                or entry["market"] not in {"za", "ng", "ke"}
            ):
                raise RuntimeError("Wave 1 seed manifest is invalid")
            seen.append(entry[key])
    if not any(payload[field] for field in limits):
        raise RuntimeError("Wave 1 seed manifest is invalid")
    return payload


def _r3_seed_manifest(client: object, _observed_at: datetime) -> dict[str, object]:
    from google.cloud.bigquery.table import Row
    from scripts.staging.replay_open_intelligence import R3_RUN_ID

    rows = _wave1_control_query(
        client,
        (
            "WITH receipt AS ("
            "SELECT run_id, row_set_digest, source_sha FROM "
            "`ogilvy-trends-v2.trends_v2_staging.open_intelligence_run_receipts_v1` "
            "WHERE run_id = @run_id AND status = 'completed' AND complete_partitions), "
            "evidence AS (SELECT e.*, c.native_id, c.endpoint, c.collected_at "
            "FROM `ogilvy-trends-v2.trends_v2_staging.signal_evidence_v2` e "
            "LEFT JOIN `ogilvy-trends-v2.trends_v2_staging.enriched_content` c "
            "ON c.id = e.row_id WHERE e.run_id = @run_id), "
            "tiktok_music AS (SELECT term AS value, MIN(market) AS market, "
            "MIN(trend_date) AS first_seen "
            "FROM `ogilvy-trends-v2.trends_v2_staging.seed_graph` "
            "WHERE platform = 'tiktok' AND term_type = 'music' "
            "AND term IS NOT NULL AND term != '' GROUP BY term) "
            "SELECT r.run_id AS replacement_run_id, r.row_set_digest, r.source_sha, "
            "ARRAY(SELECT AS STRUCT 'music_id' AS identifier_type, value, market "
            "FROM tiktok_music ORDER BY first_seen, value LIMIT 5) "
            "AS tiktok_music_identifiers, "
            "ARRAY(SELECT AS STRUCT url, market FROM (SELECT url, MIN(market) AS market, "
            "MIN(evidence_id) first_id FROM evidence "
            "WHERE platform = 'reddit' AND url IS NOT NULL GROUP BY url) "
            "ORDER BY first_id, url LIMIT 2) AS reddit_urls, "
            "ARRAY(SELECT AS STRUCT label AS term, market FROM (SELECT label, "
            "MIN(market) AS market, MIN(signal_id) first_id FROM "
            "`ogilvy-trends-v2.trends_v2_staging.signal_candidates_v2` "
            "WHERE run_id = @run_id GROUP BY label) ORDER BY first_id, label LIMIT 2) "
            "AS instagram_search_terms, "
            "ARRAY(SELECT AS STRUCT url, market FROM (SELECT url, MIN(market) AS market, "
            "MIN(evidence_id) first_id FROM evidence "
            "WHERE platform = 'youtube' AND url IS NOT NULL GROUP BY url) "
            "ORDER BY first_id, url LIMIT 3) AS youtube_short_urls FROM receipt r"
        ),
        parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", R3_RUN_ID),),
        max_results=2,
    )
    if len(rows) != 1:
        raise RuntimeError("Wave 1 seed manifest is unavailable")
    payload = dict(rows[0])
    identifiers = payload.pop("tiktok_music_identifiers", None)
    # No TikTok music seed in the window is an empty list, not a refusal (4 Sep 2026).
    if not isinstance(identifiers, (list, tuple)) or not 0 <= len(identifiers) <= 5:
        raise RuntimeError("Wave 1 seed manifest is invalid")
    music_ids = []
    for identifier in identifiers:
        if isinstance(identifier, Mapping):
            typed = dict(identifier)
        elif isinstance(identifier, Row):
            typed = dict(identifier.items())
        else:
            raise RuntimeError("Wave 1 seed manifest is invalid")
        if (
            set(typed) != {"identifier_type", "value", "market"}
            or typed["identifier_type"] != "music_id"
            or not isinstance(typed["value"], str)
            or not typed["value"]
            or typed["value"] in {entry["music_id"] for entry in music_ids}
        ):
            raise RuntimeError("Wave 1 seed manifest is invalid")
        music_ids.append({"music_id": typed["value"], "market": typed["market"]})
    payload["tiktok_music_ids"] = music_ids
    for field, key in (
        ("reddit_urls", "url"),
        ("instagram_search_terms", "term"),
        ("youtube_short_urls", "url"),
    ):
        entries = []
        for entry in payload.get(field) or ():
            if isinstance(entry, Mapping):
                typed = dict(entry)
            elif isinstance(entry, Row):
                typed = dict(entry.items())
            else:
                raise RuntimeError("Wave 1 seed manifest is invalid")
            if set(typed) != {key, "market"}:
                raise RuntimeError("Wave 1 seed manifest is invalid")
            entries.append(typed)
        payload[field] = entries
    return {
        "contract_version": WAVE1_SEED_MANIFEST_CONTRACT,
        **_validate_wave1_seed_manifest(payload),
    }


def _wave1_requests_from_seed_manifest(payload: object):
    from src.ingestion.connectors.socialcrawl import Wave1RouteRequest

    if not isinstance(payload, dict) or payload.get("contract_version") != (
        WAVE1_SEED_MANIFEST_CONTRACT
    ):
        raise RuntimeError("Wave 1 seed manifest is invalid")
    seed = _validate_wave1_seed_manifest(
        {key: value for key, value in payload.items() if key != "contract_version"}
    )
    # Only seeded calls run: each carries its market as retained geography. The
    # Global trending surfaces return rows no market can claim, so the plan
    # omits them (attempt 17, 4 Sep 2026).
    requests = []
    for entry in seed["tiktok_music_ids"]:
        requests.extend(
            (
                Wave1RouteRequest(
                    "tiktok/song",
                    {"clipId": entry["music_id"]},
                    "qualification",
                    market=entry["market"],
                ),
                Wave1RouteRequest(
                    "tiktok/song/videos",
                    {"clipId": entry["music_id"]},
                    "qualification",
                    market=entry["market"],
                ),
            )
        )
    requests.extend(
        Wave1RouteRequest(
            "instagram/search/reels",
            {"query": entry["term"]},
            "qualification",
            market=entry["market"],
        )
        for entry in seed["instagram_search_terms"]
    )
    requests.extend(
        Wave1RouteRequest(
            "youtube/video/comments",
            {"url": entry["url"]},
            "qualification",
            market=entry["market"],
        )
        for entry in seed["youtube_short_urls"]
    )
    requests.extend(
        Wave1RouteRequest(
            "reddit/post/comments",
            {"url": entry["url"]},
            "qualification",
            market=entry["market"],
        )
        for entry in seed["reddit_urls"]
    )
    return tuple(requests)


def _gdelt_dry_run_set(_client: object, _observed_at: datetime) -> dict[str, object]:
    from src.ingestion.connectors.gdelt import (
        _dry_run_receipt_content,
        _dry_run_receipt_from_mapping,
        _wave1_plan,
    )

    try:
        payload = json.loads(GDELT_DRY_RUN_RECEIPT_SET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Wave 1 GDELT receipt artifact is unavailable") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"contract_version", "receipts"}
        or payload["contract_version"] != "wave1-gdelt-dry-run-set-v1"
        or not isinstance(payload["receipts"], list)
        or len(payload["receipts"]) != 2
    ):
        raise RuntimeError("Wave 1 GDELT receipt artifact is invalid")
    try:
        receipts = tuple(_dry_run_receipt_from_mapping(item) for item in payload["receipts"])
    except ValueError as error:
        raise RuntimeError("Wave 1 GDELT receipt artifact is invalid") from error
    if tuple(receipt.name for receipt in receipts) != ("events", "gcam") or any(
        hashlib.sha256(_wave1_plan(receipt.name).sql.encode()).hexdigest() != receipt.sql_digest
        for receipt in receipts
    ):
        raise RuntimeError("Wave 1 GDELT receipt artifact differs from approved SQL")
    return {
        "contract_version": "wave1-gdelt-dry-run-set-v1",
        "receipts": [
            {
                "dry_run_receipt_id": receipt.dry_run_receipt_id,
                **_dry_run_receipt_content(receipt),
            }
            for receipt in receipts
        ],
    }


def _validate_wave1_source_lab_rows(rows: object) -> list[dict[str, object]]:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("Wave 1 Source Lab snapshot is invalid")
    expected = {f"/v1/{route}" for route in WAVE1_ROUTE_SPECS}
    observed = [row.get("route_path") for row in rows]
    if (
        len(rows) != len(expected)
        or set(observed) != expected
        or len(observed) != len(set(observed))
    ):
        raise RuntimeError("Wave 1 Source Lab snapshot is incomplete")
    return rows


def _source_lab_snapshot(client: object, _observed_at: datetime) -> dict[str, object]:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    route_paths = tuple(f"/v1/{route}" for route in sorted(WAVE1_ROUTE_SPECS))
    rows = _wave1_control_query(
        client,
        (
            "SELECT TO_JSON_STRING(t) AS row_json FROM "
            "`ogilvy-trends-v2.trends_v2_staging.v_source_lab_v2` t "
            "WHERE route_path IN UNNEST(@route_paths) ORDER BY route_path"
        ),
        parameters=(bigquery.ArrayQueryParameter("route_paths", "STRING", route_paths),),
        max_results=9,
    )
    try:
        values = [json.loads(dict(row)["row_json"]) for row in rows]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Wave 1 Source Lab snapshot is invalid") from error
    return {
        "contract_version": "wave1-source-lab-snapshot-v1",
        "routes": _validate_wave1_source_lab_rows(values),
    }


def _funded_preflight(client: object, observed_at: datetime) -> dict[str, object]:
    from src.analysis.open_intelligence.funded_lane_reader import read_monthly_funded_lane

    if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
        raise RuntimeError("Wave 1 funded preflight time is invalid")
    snapshot = read_monthly_funded_lane(
        client=client,
        dataset="trends_v2_staging_funded",
        as_of=observed_at,
        credential_lane="ogilvy_funded",
    )
    required = (
        "month_start",
        "month_opening_balance",
        "monthly_ledger_debit",
        "monthly_vendor_reported",
        "unreconciled_execution_ids",
        "consecutive_complete_runs",
        "runs_today",
    )
    if any(not hasattr(snapshot, field) for field in required):
        raise RuntimeError("Wave 1 funded preflight is invalid")
    opening = snapshot.month_opening_balance
    debit = snapshot.monthly_ledger_debit
    vendor = snapshot.monthly_vendor_reported
    if (
        any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (debit, vendor)
        )
        or debit > Decimal("25000")
        or tuple(snapshot.unreconciled_execution_ids)
    ):
        raise RuntimeError("Wave 1 funded preflight is blocked")
    if opening is None:
        # A month with no funded run yet carries no preflight row, so the ledger
        # has no opening balance (4 Sep 2026). The artifact attests the ledger it
        # can read; the runtime reads the live vendor balance at execution and
        # refuses on balance_unavailable and reserve_floor_reached itself. An
        # untouched month is admitted only when nothing was spent or reported.
        if debit != 0 or vendor != 0:
            raise RuntimeError("Wave 1 funded preflight is blocked")
    elif (
        not isinstance(opening, Decimal)
        or not opening.is_finite()
        or opening < 0
        or opening - max(debit, vendor) < Decimal("225000")
    ):
        raise RuntimeError("Wave 1 funded preflight is blocked")
    return {
        "contract_version": "wave1-funded-preflight-v1",
        "month_start": snapshot.month_start,
        "month_opening_balance": None if opening is None else _wave1_decimal_text(opening),
        "monthly_ledger_debit": _wave1_decimal_text(debit),
        "monthly_vendor_reported": _wave1_decimal_text(vendor),
        "unreconciled_execution_ids": tuple(snapshot.unreconciled_execution_ids),
        "consecutive_complete_runs": snapshot.consecutive_complete_runs,
        "runs_today": snapshot.runs_today,
    }


def _wave1_artifact_client():
    """The client every Wave 1 input artifact is rebuilt through.

    The job reads through it before its approval is consumed, and the owner run
    manifest producer (scripts/staging/produce_wave1_execution_manifest.py) reads
    through the same factory, so both sides rebuild the same bytes from the same
    tables.
    """
    return bigquery.Client(project="ogilvy-trends-v2", location="US")


def _build_wave1_execution_artifacts(*, client: object, observed_at: datetime) -> dict[str, bytes]:
    values = {
        "wave1_contract": _wave1_contract(client, observed_at),
        "r3_seed_manifest": _r3_seed_manifest(client, observed_at),
        "gdelt_dry_run_set": _gdelt_dry_run_set(client, observed_at),
        "source_lab_snapshot": _source_lab_snapshot(client, observed_at),
        "funded_preflight": _funded_preflight(client, observed_at),
    }
    return {name: canonical_bytes(payload) for name, payload in values.items()}


def _wave1_decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise RuntimeError("Wave 1 decimal value is invalid")
    return format(Decimal("0") if value == 0 else value.normalize(), "f")


def _require_wave1_origin_authority(authority: object) -> None:
    """The pilot job, principal and image namespace are the wave1_pilot binding of the
    origin the issued authority's trusted generation selects; any other job, generation
    or record family is refused before the authority is consumed."""
    generation = getattr(authority, "generation", None)
    manifest = getattr(authority, "manifest", None)
    approval = getattr(authority, "approval", None)
    registry = getattr(generation, "registry", None)
    pair = (
        getattr(generation, "origin_registry_sha256", None),
        getattr(generation, "resource_manifest_sha256", None),
    )
    if (
        type(approval) is not ExecutionApprovalV2
        or registry is None
        or (approval.origin_registry_sha256, approval.resource_manifest_sha256) != pair
    ):
        raise RuntimeError("Wave 1 authority is not issued under a trusted origin generation")
    try:
        origin = select_origin(
            manifest_version=manifest.manifest_version,
            contract_sha256=manifest.contract_sha256,
            mode="new_consume",
            registry=registry,
        )
        binding = origin.operation_bindings["wave1_pilot"]
    except Exception as error:
        raise RuntimeError("Wave 1 authority origin is not admitted") from error
    image_uri = getattr(authority, "image_uri", None)
    if (
        getattr(authority, "operation", None) != "wave1_pilot"
        or getattr(authority, "job_resource", None) != binding.job_resource
        or manifest.job_resource != binding.job_resource
        or manifest.service_identity != binding.service_identity
        or not isinstance(image_uri, str)
        or re.fullmatch(origin.image_uri_regex, image_uri) is None
    ):
        raise RuntimeError("Wave 1 authority differs from its origin binding")


def _require_wave1_writer_identity(authority: object) -> None:
    """Refuse before the approval is consumed unless this execution can write.

    Every Wave 1 query and writer admits only the identity the trusted origin registry
    binds to wave1_pilot. The approved manifest must name it, and the credential this
    execution resolves (on the client the GDELT query and writers use) must be it, so a
    wrong identity is refused here and never spends its approval.
    """
    from src.analysis.open_intelligence.funded_lane import wave1_pilot_service_identity

    expected = wave1_pilot_service_identity()
    manifest = getattr(authority, "manifest", None)
    credentials = getattr(_wave1_result_client(), "_credentials", None)
    if (
        getattr(manifest, "service_identity", None) != expected
        or getattr(credentials, "service_account_email", None) != expected
    ):
        raise RuntimeError("Wave 1 execution identity cannot write; the approval is unspent")


def _begin_wave1_durable_execution(
    run_id: str, observed_at: datetime
) -> Wave1DurableExecution | None:
    global _ACTIVE_WAVE1_EXECUTION, _DURABLE_WAVE1_ARTIFACT_CONTEXT

    selector = os.environ.get("SOCIALCRAWL_CREDENTIAL_LANE", "jhb_core").strip()
    stage = os.environ.get("SOCIALCRAWL_FUNDED_STAGE_NAME", "").strip()
    if selector != "ogilvy_funded" or stage != "stage_1_wave_1":
        return None
    if embedding_is_enabled():
        raise RuntimeError("Wave 1 zero-model authority cannot run embedding")
    provider = _Wave1ArtifactProvider(_wave1_artifact_client(), observed_at)
    if _DURABLE_WAVE1_ARTIFACT_CONTEXT is not None:
        raise RuntimeError("Wave 1 execution artifact context is already active")
    _DURABLE_WAVE1_ARTIFACT_CONTEXT = provider
    try:
        authority = execution_approval._load_execution_authority(
            "wave1_pilot", mode="new_consume", artifact_reader=provider.read
        )
        _require_wave1_origin_authority(authority)
        if provider.inputs is None:
            raise RuntimeError("Wave 1 execution artifacts were not validated")
        _require_wave1_writer_identity(authority)
        consumption = execution_approval._consume_execution_authority(authority)
        _ACTIVE_WAVE1_EXECUTION = (authority, consumption, run_id)
    finally:
        _DURABLE_WAVE1_ARTIFACT_CONTEXT = None
    wave1_contract = json.loads(provider.inputs.artifacts["wave1_contract"])
    route_set_sha256 = wave1_contract.get("route_set_sha256")
    if not isinstance(route_set_sha256, str):
        raise RuntimeError("Wave 1 contract artifact carries no route set digest")
    capability = _issue_wave1_execution_capability(
        authority, consumption, route_set_sha256=route_set_sha256
    )
    seed_manifest = json.loads(provider.inputs.artifacts["r3_seed_manifest"])
    wave1_requests = _wave1_requests_from_seed_manifest(seed_manifest)
    from src.ingestion.connectors.gdelt import (
        _dry_run_receipt_from_mapping,
        bind_wave1_gdelt_manifest_entry,
    )

    gdelt_artifact = provider.inputs.artifacts["gdelt_dry_run_set"]
    try:
        gdelt_payload = json.loads(gdelt_artifact)
        gdelt_receipts = tuple(
            _dry_run_receipt_from_mapping(item) for item in gdelt_payload["receipts"]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Wave 1 GDELT receipt artifact is invalid") from error
    if tuple(receipt.name for receipt in gdelt_receipts) != ("events", "gcam"):
        raise RuntimeError("Wave 1 GDELT receipt artifact is invalid")
    gdelt_entries = tuple(
        bind_wave1_gdelt_manifest_entry(capability, gdelt_artifact, receipt.name)
        for receipt in gdelt_receipts
    )
    return Wave1DurableExecution(
        authority,
        consumption,
        capability,
        wave1_requests,
        gdelt_receipts,
        gdelt_entries,
    )


def _record_wave1_execution_result(
    execution: object,
    close_receipt: object,
    *,
    gdelt_runtime_proof: Wave1GDELTRuntimeProof,
) -> dict[str, object]:
    from src.analysis.open_intelligence.funded_lane_runtime import FundedRunCloseReceipt
    from src.analysis.open_intelligence.funded_source_values import wave1_close_succeeded

    if type(close_receipt) is not FundedRunCloseReceipt:
        raise RuntimeError("Wave 1 close receipt is invalid")
    if not isinstance(gdelt_runtime_proof, Wave1GDELTRuntimeProof):
        raise RuntimeError("Wave 1 GDELT runtime proof is invalid")
    gdelt_payload = asdict(gdelt_runtime_proof)
    if gdelt_payload.get("persistence", {}).get("complete") is not True:
        raise RuntimeError("Wave 1 GDELT runtime proof is incomplete")
    payload = {
        "attribution_state": close_receipt.attribution_state,
        "calls": close_receipt.calls,
        "budget_debit_credits": _wave1_decimal_text(close_receipt.budget_debit_credits),
        "vendor_reported_credits": _wave1_decimal_text(close_receipt.vendor_reported_credits),
        "balance_delta": (
            None
            if close_receipt.balance_delta is None
            else _wave1_decimal_text(close_receipt.balance_delta)
        ),
        "attribution_gap_credits": (
            None
            if close_receipt.attribution_gap_credits is None
            else _wave1_decimal_text(close_receipt.attribution_gap_credits)
        ),
        "balance_read_status": close_receipt.balance_read_status,
        "terminal_id": close_receipt.terminal_id,
        "run_balance_delta": (
            None
            if close_receipt.run_balance_delta is None
            else _wave1_decimal_text(close_receipt.run_balance_delta)
        ),
        "source_values_state": close_receipt.source_values_state,
        "source_values": {
            route: dict(values) for route, values in close_receipt.source_values.items()
        },
        "gdelt": gdelt_payload,
    }
    if close_receipt.attribution_state not in {"complete", "conservative", "gap_detected"}:
        raise RuntimeError("Wave 1 close receipt is invalid")
    canonical_result_json = canonical_bytes(payload).decode("utf-8")
    result_digest = hashlib.sha256(canonical_result_json.encode()).hexdigest()
    result_reference = getattr(execution.authority, "execution_name", None)
    if not isinstance(result_reference, str) or not result_reference:
        raise RuntimeError("Wave 1 execution reference is unavailable")
    result = execution_approval._record_execution_result(
        execution.authority,
        execution.consumption,
        result_reference + "#funded-run-close",
        canonical_result_json,
        result_digest,
        ("succeeded" if wave1_close_succeeded(close_receipt) else "failed"),
    )
    output = json.loads(canonical_result_json)
    output["execution_approval"] = {
        "manifest_sha256": execution.authority.approval.manifest_sha256,
        "approval_id": execution.authority.approval.approval_id,
        "consumption_id": execution.consumption.consumption_id,
        "result_id": result.result_id,
    }
    return output


def _prepare_funded_socialcrawl(
    run_id: str,
    started_at: datetime,
    execution_capability=None,
    wave1_requests=(),
):
    selector = os.environ.get("SOCIALCRAWL_CREDENTIAL_LANE", "jhb_core").strip()
    SocialCrawlConnector.reset_credits()
    if selector == "jhb_core":
        return None
    if selector != "ogilvy_funded":
        raise RuntimeError("SocialCrawl credential lane is unsupported")
    stage_name = os.environ.get("SOCIALCRAWL_FUNDED_STAGE_NAME", "").strip() or None
    if stage_name == "stage_1_wave_1":
        from src.analysis.open_intelligence.funded_lane import (
            _require_wave1_execution_capability,
        )

        _require_wave1_execution_capability(
            execution_capability, action="wave1_orchestrator_preflight"
        )
    if os.environ.get("TRENDS_ENV") != "staging":
        raise RuntimeError("funded SocialCrawl lane is staging only")
    execution_id = os.environ.get("CLOUD_RUN_EXECUTION", "").strip()
    if not execution_id:
        raise RuntimeError("funded SocialCrawl execution ID is missing")
    raw_stage = os.environ.get("SOCIALCRAWL_FUNDED_STAGE", "").strip()
    # The approved manifest environment names the stage (stage_1_wave_1) and
    # carries no numeric stage (refused the sixth Wave 1 pilot on staging,
    # 4 Sep 2026). The name carries the activation stage; a numeric variable,
    # when present, must agree with it.
    named_stage = re.fullmatch(r"stage_([123])_[a-z0-9_]+", stage_name or "")
    if not raw_stage and named_stage is not None:
        raw_stage = named_stage.group(1)
    try:
        activation_stage = int(raw_stage)
    except ValueError as error:
        raise RuntimeError("funded SocialCrawl stage is invalid") from error
    if str(activation_stage) != raw_stage or activation_stage not in {1, 2, 3}:
        raise RuntimeError("funded SocialCrawl stage is invalid")
    if named_stage is not None and int(named_stage.group(1)) != activation_stage:
        raise RuntimeError("funded SocialCrawl stage is invalid")

    from google.cloud import bigquery
    from src.analysis.open_intelligence.funded_lane_runtime import (
        make_funded_control_writer,
        make_funded_ledger_writer,
        make_funded_terminal_writer,
        prepare_funded_lane,
    )
    from src.ingestion.connectors.socialcrawl import FundedSocialCrawlContext

    client = bigquery.Client(project="ogilvy-trends-v2", location="US")
    dataset = "trends_v2_staging_funded"
    runtime = prepare_funded_lane(
        client=client,
        dataset=dataset,
        run_id=run_id,
        execution_id=execution_id,
        as_of=started_at,
        activation_stage=activation_stage,
        environment="staging",
        ledger_writer=make_funded_ledger_writer(
            client=client,
            dataset=dataset,
            stage_name=stage_name,
            execution_capability=execution_capability,
        ),
        control_writer=make_funded_control_writer(client=client, dataset=dataset),
        terminal_writer=make_funded_terminal_writer(
            client=client,
            dataset=dataset,
            stage_name=stage_name,
            execution_capability=execution_capability,
        ),
        stage_name=stage_name,
        execution_capability=execution_capability,
    )
    if stage_name == "stage_1_wave_1":
        if _ACTIVE_WAVE1_EXECUTION is None:
            raise RuntimeError("Wave 1 durable execution is not active")
        consumed_manifest_sha256 = _ACTIVE_WAVE1_EXECUTION[1].manifest_sha256
        if getattr(execution_capability, "manifest_sha256", None) != consumed_manifest_sha256:
            raise RuntimeError("Wave 1 capability does not match the consumed manifest")
    SocialCrawlConnector.configure_funded_run(
        FundedSocialCrawlContext(
            credential_lane="ogilvy_funded",
            secret_id="SOCIALCRAWL_OGILVY_API_KEY",
            run_allowance=runtime.run_allowance,
            phase_close_hook=runtime.phase_close,
            pre_call_authority_hook=runtime.assert_call_authority,
            stage_name=stage_name,
            execution_capability=execution_capability,
            wave1_requests=tuple(wave1_requests),
        )
    )
    return runtime


def _close_funded_socialcrawl(
    runtime,
    recorded_at: datetime,
    *,
    gdelt_runtime_proof: object = None,
):
    if runtime is None:
        return None
    from src.analysis.open_intelligence.funded_lane_runtime import read_funded_balance

    try:
        post_balance = read_funded_balance(
            observed_at=recorded_at,
            stage_name=getattr(runtime, "stage_name", None),
            execution_capability=getattr(runtime, "execution_capability", None),
        )
    except Exception:
        return runtime.close_unavailable(recorded_at=recorded_at)
    close_kwargs = {"post_balance": post_balance, "recorded_at": recorded_at}
    if getattr(runtime, "stage_name", None) == "stage_1_wave_1":
        close_kwargs["source_value_results"] = SocialCrawlConnector.wave1_source_values()
        close_kwargs["gdelt_runtime_proof"] = gdelt_runtime_proof
    return runtime.close(**close_kwargs)


@dataclass
class _RunDiagnostics:
    run_id: str = dataclass_field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = dataclass_field(default_factory=lambda: datetime.now(UTC))
    cloud_execution: str | None = dataclass_field(
        default_factory=lambda: os.environ.get("CLOUD_RUN_EXECUTION") or None
    )
    failed_stages: set[str] = dataclass_field(default_factory=set)

    def record(
        self,
        stage,
        status,
        *,
        reason_code=None,
        error=None,
        market=None,
        source="engine_cron",
        event_type="pipeline_stage",
        fatal=False,
    ):
        failed = status in ("failed", "degraded", "render_error", "send_failed")
        if failed and event_type != "cron_run":
            self.failed_stages.add(stage)
        severity = "ERROR" if failed and status != "degraded" else "INFO"
        if status in ("degraded", "unavailable"):
            severity = "WARN"
        try:
            from src.observability.events import record_event

            detail = (
                "".join(traceback.format_exception(error))
                if isinstance(error, BaseException)
                else str(error)
                if error is not None
                else None
            )
            record_event(
                source,
                severity,
                event_type,
                status=status,
                market=market,
                message=f"{stage}: {reason_code or status}",
                error_detail=detail,
                latency_ms=(
                    int((datetime.now(UTC) - self.started_at).total_seconds() * 1000)
                    if event_type == "cron_run"
                    else None
                ),
                meta={
                    "run_id": self.run_id,
                    "cloud_execution": self.cloud_execution,
                    "trend_date": self.started_at.date().isoformat(),
                    "stage": stage,
                    "reason_code": reason_code or status,
                    "error_type": type(error).__name__
                    if isinstance(error, BaseException)
                    else None,
                    "failed_stages": sorted(self.failed_stages),
                },
                fatal=fatal,
            )
        except Exception:
            logger.warning("pipeline diagnostic write unavailable: stage=%s", stage)


# Collection-only mode, the isolated staging producer. The receipt ledger the
# bound collector hands in is the durable store of collection receipts keyed by
# operation ID: the execution ID of the exact authority the daily collect stage
# hands in, which is its business attempt id, or the Cloud Run execution name
# when the manual job reads its identity from the environment. Its record is the
# single commit point of a collection-only run, and the market-day decision is
# taken from it, never from the fail-open pipeline_runs guard.
_COLLECTION_AUTHORITY_VARIABLES = {
    "execution_id": "CLOUD_RUN_EXECUTION",
    "source_sha": "COLLECTION_SOURCE_SHA",
    "image_uri": "COLLECTION_IMAGE_URI",
    "policy_sha256": "COLLECTION_POLICY_SHA256",
    "profile_sha256": "COLLECTION_PROFILE_SHA256",
}
_COLLECTION_DIGEST_FIELDS = ("source_sha", "image_uri", "policy_sha256", "profile_sha256")
_COLLECTION_PERSISTED_STATES = frozenset({"collected", "partial"})


@dataclass
class _CollectionOnly:
    authority: dict[str, str]
    ledger: object
    skip_markets: set[str]
    prior: dict | None = None
    profile: dict | None = None
    authority_kind: str | None = None


def _collection_authority(collection_authority=None) -> dict[str, str]:
    """Take the exact execution authority handed in, or read the identity from the environment.

    The daily collect stage hands the authority in process, keyed by the
    business attempt it issues, and the environment is not read at all: the
    mapping must carry exactly the five fields, each a non-empty string, and a
    missing or unexpected one refuses before any connector is constructed.

    The environment read below is reached by no production caller: the daily
    stage and the staging entry point both hand the authority in, and running
    this script directly never enters collection-only mode. It stands for a
    direct invocation of the producer in collection-only mode, which is how the
    collection-only tests drive it, and a missing variable refuses the same way.
    """
    if collection_authority is not None:
        if not isinstance(collection_authority, Mapping):
            raise RuntimeError("collection authority is not a mapping")
        unexpected = sorted(set(collection_authority) - set(_COLLECTION_AUTHORITY_VARIABLES))
        if unexpected:
            raise RuntimeError(
                f"collection authority carries unexpected fields: {', '.join(unexpected)}"
            )
        authority = {}
        for field in _COLLECTION_AUTHORITY_VARIABLES:
            value = collection_authority.get(field)
            value = value.strip() if isinstance(value, str) else ""
            if not value:
                raise RuntimeError(f"collection authority is missing: {field}")
            authority[field] = value
        return authority
    authority = {}
    for field, variable in _COLLECTION_AUTHORITY_VARIABLES.items():
        value = os.environ.get(variable, "").strip()
        if not value:
            raise RuntimeError(f"collection authority is missing: {variable}")
        authority[field] = value
    return authority


def _begin_collection_only(
    receipt_ledger,
    trend_date,
    collection_authority=None,
    collection_profile=None,
    authority_kind=None,
) -> _CollectionOnly:
    """Resolve the authority and consult the ledger before any connector runs.

    The receipt already recorded for this operation comes back as ``prior``
    when its identity matches, and a differing identity is a conflicting
    duplicate and refuses. Otherwise the day's receipts decide: a market
    persisted under another digest holds the day for recovery, neither
    skipped as already ingested nor deleted and collected again, and a market
    persisted under this authority is skipped.

    ``collection_profile`` is the source profile the caller placed this run
    under, and ``authority_kind`` the kind of authority it placed it under. Both
    travel to the receipt: the profile so the stamped profile digest is checked
    against the profile the run used rather than taken on the caller's word, and
    the kind so the record says which authority produced it.
    """
    authority = _collection_authority(collection_authority)
    if receipt_ledger is None:
        raise RuntimeError("collection receipt ledger is not bound")
    if _parse_force_reingest_markets():
        raise RuntimeError("collection-only mode never force deletes a market day")
    operation_id = authority["execution_id"]
    prior = receipt_ledger.operation(operation_id)
    if prior is not None:
        if any(prior.get(field) != value for field, value in authority.items()):
            raise RuntimeError(
                f"collection operation {operation_id} conflicts with its recorded receipt"
            )
        return _CollectionOnly(
            authority,
            receipt_ledger,
            set(),
            dict(prior),
            profile=collection_profile,
            authority_kind=authority_kind,
        )
    day = trend_date.isoformat()
    skip_markets: set[str] = set()
    for recorded in receipt_ledger.market_day(day):
        states = recorded.get("market_states") or {}
        persisted = {m for m, state in states.items() if state in _COLLECTION_PERSISTED_STATES}
        if not persisted:
            continue
        changed = [f for f in _COLLECTION_DIGEST_FIELDS if recorded.get(f) != authority[f]]
        if changed:
            raise RuntimeError(
                f"market day {day} was collected under another {', '.join(changed)}; "
                "held for recovery"
            )
        skip_markets |= persisted
    return _CollectionOnly(
        authority,
        receipt_ledger,
        skip_markets,
        profile=collection_profile,
        authority_kind=authority_kind,
    )


def _collection_skip_markets(collection: _CollectionOnly, guard_markets) -> set[str]:
    """Take the skip set from the ledger; rows the guard shows without a receipt hold the day."""
    unreceipted = sorted(set(guard_markets) - collection.skip_markets)
    if unreceipted:
        raise RuntimeError(
            f"market day rows for {unreceipted} carry no collection receipt; held for recovery"
        )
    return set(collection.skip_markets)


def _collection_market_states(skip_markets, errors, persisted_raw) -> dict[str, str]:
    """Name each market's terminal state from what the loop persisted and recorded."""
    fatal = {e.get("market") for e in errors if isinstance(e, dict) and e.get("fatal", True)}
    states = {}
    for market in MARKETS:
        if market in skip_markets:
            states[market] = "skipped"
        elif persisted_raw.get(market, 0) > 0:
            states[market] = "partial" if market in fatal else "collected"
        else:
            states[market] = "failed" if market in fatal else "empty"
    return states


def _collection_funded_close(close_receipt) -> dict | None:
    """Carry the funded terminal close into the receipt; an unread balance stays None."""
    if close_receipt is None:
        return None

    def text(value):
        return None if value is None else _wave1_decimal_text(value)

    return {
        "attribution_state": close_receipt.attribution_state,
        "balance_read_status": close_receipt.balance_read_status,
        "calls": close_receipt.calls,
        "balance_delta": text(close_receipt.balance_delta),
        "run_balance_delta": text(close_receipt.run_balance_delta),
        "terminal_id": close_receipt.terminal_id,
    }


def _run_impl(
    *,
    diagnostics=None,
    stop_after_ingestion=False,
    receipt_ledger=None,
    telemetry=None,
    collection_authority=None,
    collection_profile=None,
    authority_kind=None,
):
    global _ACTIVE_WAVE1_EXECUTION

    diagnostics = diagnostics or _RunDiagnostics()
    run_id = diagnostics.run_id
    started_at = diagnostics.started_at
    trend_date = started_at.date()

    project = os.environ.get("GCP_PROJECT", "<unset>")
    dataset = get_dataset()
    env = os.environ.get("TRENDS_ENV", "dev")
    print(f"Project:  {project}")
    print(f"Dataset:  {dataset}")
    print(f"Env:      {env}")
    print(f"Run ID:   {run_id}")
    print(f"Started:  {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()

    collection = None
    if stop_after_ingestion:
        collection = _begin_collection_only(
            receipt_ledger,
            trend_date,
            collection_authority,
            collection_profile=collection_profile,
            authority_kind=authority_kind,
        )
        if collection.prior is not None:
            print(
                f"  Collection only: operation {collection.prior['execution_id']} already recorded"
            )
            return collection.prior
        if telemetry is not None:
            # The boundary telemetry is bound to this operation and its source
            # binding; outside collection only mode there is no authority to bind.
            telemetry.bind(
                operation_id=collection.authority["execution_id"],
                source_binding_digest=collection.authority["profile_sha256"],
                observed_at=started_at,
            )
    else:
        telemetry = None

    funded_lane = (
        os.environ.get("SOCIALCRAWL_CREDENTIAL_LANE", "jhb_core").strip() == "ogilvy_funded"
    )
    # B0 vendor-truth pre-call (0 units, logs only). Confirms Ensemble spend
    # before ingestion claims budget headroom.
    if not funded_lane:
        try:
            from src.ingestion.connectors.customer_units import fetch_units_history
            from src.ingestion.connectors.ensemble import _ensemble_enabled
            from src.utils.config_loader import load_sources
            from src.utils.secrets import get_secret

            # Same gate as the connector (PR #300). This pre-call sits ahead of every
            # connector gate, so it was the last path still reaching a vendor
            # cancelled on 23 Jul 2026. It only stopped calling because it asked
            # Secret Manager for a name that does not exist and got nothing back;
            # fixing that lookup would have quietly restarted a dead account.
            if _ensemble_enabled(load_sources()):
                token = (
                    get_secret("ENSEMBLEDATA_API_TOKEN")
                    or os.environ.get("ENSEMBLEDATA_API_TOKEN", "")
                ).strip()
                if token:
                    history = fetch_units_history(token, 3) or []
                    if history:
                        latest = history[-1]
                        print(
                            f"  Ensemble vendor truth: {latest.get('total')} units on {latest.get('date')}"
                        )
        except Exception as exc:
            diagnostics.record("vendor_usage_probe", "failed", reason_code="probe_failed", error=exc)
            logger.warning("fetch_units_history pre-call skipped (non-fatal): %s", exc)

    # Market-day idempotency guard.
    # whole pipeline on non-zero exit; without this check, a partial-success
    # first attempt produces double-counted raw_content / enriched_content /
    # pipeline_runs rows. Any market whose status='success' row already
    # exists for today is skipped on the retry pass.
    skip_markets = _markets_already_ingested_today()
    if collection is not None:
        skip_markets = _collection_skip_markets(collection, skip_markets)
    force_reingest = _parse_force_reingest_markets()
    if funded_lane and force_reingest:
        raise RuntimeError("Funded Wave 1 cannot force reingest market-day rows")
    if force_reingest:
        allowed = force_reingest & set(MARKETS)
        if allowed:
            try:
                _cleanup_market_day_rows(allowed)
                print(f"  FORCE_REINGEST_MARKETS: cleaned day rows for {sorted(allowed)}")
            except Exception as exc:
                diagnostics.record(
                    "force_reingest_cleanup", "failed", reason_code="stage_failed", error=exc
                )
                logger.error("FORCE_REINGEST cleanup failed (non-fatal): %s", exc, exc_info=True)
                print(f"  FORCE_REINGEST cleanup error (non-fatal): {type(exc).__name__}")
            skip_markets = skip_markets - allowed
            print(f"  FORCE_REINGEST_MARKETS: re-ingesting {sorted(allowed)} despite guard")
    if skip_markets:
        print(f"  Idempotency: skipping markets already ingested today: {sorted(skip_markets)}")
        print()

    if funded_lane and skip_markets:
        raise RuntimeError("funded SocialCrawl lane cannot skip an already ingested market")
    durable_wave1 = _begin_wave1_durable_execution(run_id, started_at)
    connector_plan = _connector_plan_for_run(durable_wave1)
    gdelt_runtime_proof = (
        _execute_wave1_gdelt(durable_wave1, run_id) if durable_wave1 is not None else None
    )
    funded_socialcrawl = _prepare_funded_socialcrawl(
        run_id,
        started_at,
        durable_wave1.capability if durable_wave1 is not None else None,
        durable_wave1.wave1_requests if durable_wave1 is not None else (),
    )

    total_raw = 0
    total_enriched = 0
    # market_source_counts[market][source_key] -> row_count
    market_source_counts = {m: {k: 0 for k, _ in CONNECTORS} for m in MARKETS}
    market_counts = {}  # (market, query_group) -> aggregated stats
    unclassified_counters = dict.fromkeys(MARKETS, 0)
    # Wave 1: per-market {layer_name: count} from classification instrumentation.
    # Empty per market unless CLASSIFICATION_INSTRUMENTATION_ENABLED is on.
    classification_layer_counters: dict[str, dict[str, int]] = {m: {} for m in MARKETS}
    errors = []
    markets_with_data: set[str] = set()
    persisted_raw: dict[str, int] = {}

    for market in MARKETS:
        print(f"--- {market.upper()} ---")
        if market in skip_markets:
            diagnostics.record(
                "market_ingestion", "skipped", market=market, reason_code="already_ingested"
            )
            print(
                f"  Skipping {market}: pipeline_runs already shows ingest "
                f"for today (idempotency guard)."
            )
            print()
            continue
        market_frames = []
        parallel_batch = [
            (source_key, ConnectorClass)
            for source_key, ConnectorClass in connector_plan
            if source_key in _CONNECTOR_PARALLEL_OK
        ]
        parallel_results: dict[str, tuple] = {}
        if parallel_batch:
            workers = min(6, len(parallel_batch))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        _fetch_one_connector, market, source_key, ConnectorClass
                    ): source_key
                    for source_key, ConnectorClass in parallel_batch
                }
                for future in as_completed(futures):
                    source_key, df_fetched, failures, exc = future.result()
                    parallel_results[source_key] = (df_fetched, failures, exc)

        for source_key, ConnectorClass in connector_plan:
            if source_key in parallel_results:
                df_fetched, failures, exc = parallel_results[source_key]
            else:
                try:
                    connector = ConnectorClass(market=market)
                    df_fetched = connector.safe_fetch()
                    failures = list(getattr(connector, "_fetch_failures", [])) + list(
                        getattr(connector, "_endpoint_failures", [])
                    )
                    exc = None
                    wave1_global = (
                        source_key == "socialcrawl"
                        and getattr(SocialCrawlConnector._funded_context, "stage_name", None)
                        == "stage_1_wave_1"
                    )
                    if not df_fetched.empty and not wave1_global:
                        df_fetched = df_fetched.copy()
                        df_fetched["market"] = market
                except Exception as fetch_exc:
                    df_fetched = ConnectorClass.empty_dataframe()
                    failures = []
                    exc = fetch_exc

            try:
                n = len(df_fetched)
                market_source_counts[market][source_key] = n
                print(f"  {source_key}: {n} rows")
                for failure in failures:
                    # Per-feed / per-endpoint failure the connector caught and
                    # survived (a dead RSS feed, one 495'd endpoint). Non-fatal:
                    # visible in the errors column but it does not downgrade the
                    # market off success (feed churn is expected).
                    errors.append(
                        {"market": market, "source": source_key, "error": failure, "fatal": False}
                    )
                    print(f"  {source_key}: degraded (non-fatal): endpoint_failure")
                    diagnostics.record(
                        f"connector:{source_key}",
                        "degraded",
                        market=market,
                        reason_code="endpoint_failure",
                        error=failure,
                    )
                if exc is not None:
                    raise exc
                if not df_fetched.empty:
                    market_frames.append(df_fetched)
            except Exception as exc:
                logger.error("Market %s %s failed: %s", market, source_key, exc, exc_info=True)
                # Whole connector crashed: fatal, downgrades the market.
                errors.append(
                    {"market": market, "source": source_key, "error": str(exc), "fatal": True}
                )
                print(f"  {source_key}: FAILED {type(exc).__name__}")
                # System observability: record the connector failure with its
                # cause so the events history carries it. Non-fatal (the market
                # already continues); not flagged fatal so it does not page on a
                # single connector blip the digest survives.
                diagnostics.record(
                    f"connector:{source_key}",
                    "failed",
                    market=market,
                    reason_code="fetch_failed",
                    error=exc,
                    event_type="connector_fail",
                )

        if not market_frames:
            print(f"  No rows across any connector for {market}")
            print()
            continue

        # Guard the per-market ingest body. A failure here (a bad enrich, a
        # BigQuery insert error, an aggregation crash) is recorded as a partial
        # failure for this market and the loop moves on, instead of aborting
        # the whole run before scoring, briefs, the forecast, and the email.
        try:
            (
                rows_raw,
                rows_enriched,
                topic_counts,
                unclassified_run,
                sub_counts,
                layer_counts,
            ) = _ingest_market_frames(market, market_frames, run_id, started_at, telemetry)
        except Exception as exc:
            diagnostics.record(
                "market_ingestion", "failed", market=market, reason_code="stage_failed", error=exc
            )
            logger.error("Market %s ingest failed: %s", market, exc, exc_info=True)
            # Pipeline / ingest crash for this market: fatal, downgrades to partial.
            errors.append(
                {"market": market, "source": "pipeline", "error": str(exc), "fatal": True}
            )
            print(f"  {market}: ingest FAILED {type(exc).__name__}")
            print()
            continue

        total_raw += rows_raw
        total_enriched += rows_enriched
        persisted_raw[market] = rows_raw
        market_counts.update(topic_counts)
        # Sub-source counts (top_terms, youtube playlist, the brand24 feeds) ride
        # inside a parent connector; surface them for pipeline_runs + morning-check
        # without touching total_rows. Derived from df_market in _ingest_market_frames.
        market_source_counts[market].update(sub_counts)
        unclassified_counters[market] = unclassified_run
        # Wave 1 per-layer classification counts (empty unless instrumentation on).
        classification_layer_counters[market] = layer_counts
        markets_with_data.add(market)
        print()

    funded_close_receipt = _close_funded_socialcrawl(
        funded_socialcrawl,
        datetime.now(UTC),
        gdelt_runtime_proof=gdelt_runtime_proof,
    )
    if _all_markets_failed(errors, markets_with_data):
        raise RuntimeError(f"All markets failed: {errors}")
    if durable_wave1 is not None:
        result = _record_wave1_execution_result(
            durable_wave1,
            funded_close_receipt,
            gdelt_runtime_proof=gdelt_runtime_proof,
        )
        _ACTIVE_WAVE1_EXECUTION = None
        return result

    if stop_after_ingestion:
        # Post-collection boundary: the funded close and the durable execution
        # result are already recorded above, and nothing below this line runs
        # in collection-only mode. The ledger record is the commit point and
        # the pipeline_runs rows follow it, so a crash between the two leaves a
        # receipt the retry returns rather than a day the guard reads as done.
        from scripts.staging.collect_42_sources import collection_receipt

        print("Collection only: stopping at the post-collection boundary")
        _record_collection_route_outcomes(telemetry, market_source_counts, errors)
        # The receipt's cutoff is the closed observation day. trend_date is the
        # run's own start day and is still open while it collects, so the day
        # this run collected for, and the only day the completed run reader
        # admits (collection must start after the window closes), is the one
        # before. The instant collection finished is not a cutoff: it travels
        # separately, with started_at, to pipeline_runs.
        collection_completed_at = datetime.now(UTC)
        observation_day = trend_date - timedelta(days=1)
        receipt = collection_receipt(
            **collection.authority,
            run_id=run_id,
            cutoff=observation_day.isoformat(),
            market_states=_collection_market_states(skip_markets, errors, persisted_raw),
            raw_count=total_raw,
            enriched_count=total_enriched,
            funded_close=_collection_funded_close(funded_close_receipt),
            profile=collection.profile,
            authority_kind=collection.authority_kind,
        )
        collection.ledger.record(receipt, trend_date=trend_date.isoformat())
        log_pipeline_run(
            run_id,
            started_at,
            collection_completed_at,
            market_source_counts,
            [],
            errors,
            unclassified_by_market=unclassified_counters,
            skip_markets=skip_markets,
            classification_layer_by_market=classification_layer_counters,
        )
        print(f"  Receipt: complete={receipt['complete']} states={receipt['market_states']}")
        return receipt

    # Write trend scores
    print("--- SCORING ---")
    try:
        # baseline_period_days is the documented scoring knob for the rolling
        # baseline window. Honour it here so changing it in scoring.yaml takes
        # effect; default to 14 when absent, which matches the function default
        # so there is no behaviour change when the key is unset.
        baseline_days = int(load_scoring().get("baseline_period_days", 14))
        velocity_scores = compute_velocity_scores_for_today(
            market_counts, baseline_days=baseline_days
        )
        print(
            f"  Velocity lookup: {len(velocity_scores)} pairs, non-zero={sum(1 for v in velocity_scores.values() if v > 0)}"
        )
    except Exception as exc:
        diagnostics.record("velocity_scores", "failed", reason_code="query_failed", error=exc)
        logger.error("Velocity lookup failed, falling back to 0.0: %s", exc, exc_info=True)
        velocity_scores = {}
    # Wave 1 momentum windows (7d/30d + 7d median item_count). Display-only;
    # feeds momentum_label + lifecycle. Own try/except so a window-query blip
    # never blocks the live 14-day score: the new columns just stay 0/empty.
    try:
        velocity_windows = compute_velocity_windows_for_today(market_counts)
    except Exception as exc:
        diagnostics.record("velocity_windows", "failed", reason_code="query_failed", error=exc)
        logger.error("Momentum window lookup failed, leaving 7d/30d at 0.0: %s", exc, exc_info=True)
        velocity_windows = {}
    # Wave 1 continuity (gated). Only spend the prior-day query when the badge
    # flag is on; otherwise skip it entirely so the OFF path adds no BQ cost.
    continuity_lookup: dict = {}
    if _continuity_badges_enabled():
        continuity_lookup = fetch_continuity_lookup(trend_date)
    score_rows = compute_trend_scores(
        market_counts,
        trend_date,
        started_at,
        velocity_scores,
        velocity_windows=velocity_windows,
        continuity_lookup=continuity_lookup,
    )
    if score_rows:
        df_scores = pd.DataFrame(score_rows)
        rows_scored = merge_dataframe(
            df_scores,
            "trend_scores",
            merge_keys=["trend_date", "market", "query_group"],
        )
        print(f"  Wrote {rows_scored} rows to trend_scores")
    else:
        rows_scored = 0
        print("  No rows to score")

    # V3 seed_graph (dark, SEED_GRAPH_ENABLED default false). Non-fatal.
    if os.environ.get("SEED_GRAPH_ENABLED", "false").lower() == "true":
        print("--- SEED GRAPH ---")
        diagnostics.record("seed_graph", "started")
        try:
            from src.analysis.seed_graph import build_and_persist_seed_graph

            sg_counts = build_and_persist_seed_graph(trend_date)
            print(
                f"  seed_graph rows: za={sg_counts.get('za', 0)} "
                f"ng={sg_counts.get('ng', 0)} ke={sg_counts.get('ke', 0)}"
            )
            diagnostics.record(
                "seed_graph",
                "ok",
                reason_code="empty_result" if not any(sg_counts.values()) else "stage_completed",
            )
        except Exception as exc:
            diagnostics.record("seed_graph", "failed", reason_code="stage_failed", error=exc)
            logger.error("seed_graph stage failed (non-fatal): %s", exc, exc_info=True)
            print(f"  seed_graph error (non-fatal): {type(exc).__name__}")
    else:
        diagnostics.record("seed_graph", "skipped", reason_code="feature_disabled")

    # Wave 3 micro-briefs (dark, MICRO_BRIEFS_ENABLED default false). Non-fatal.
    # Pure selection over the seed_graph rows this run just wrote: no Gemini
    # call, no new BQ write. The read-back mirrors the wave1_badges
    # read-after-write shape (fetch_seed_graph_window is the same reader
    # seed_candidates uses). Results are stashed on daily_summary_dict so
    # render_html can pick them up without a new parameter threaded through
    # every caller.
    micro_briefs_by_market: dict[str, list[dict]] = {}
    if _micro_briefs_enabled():
        print("--- MICRO-BRIEFS: EARLY SIGNALS ---")
        try:
            from src.analysis.micro_briefs import select_micro_briefs
            from src.analysis.seed_candidates import (
                _stoplist_for,
                fetch_seed_graph_window,
                load_config_terms,
            )

            sg_rows = fetch_seed_graph_window(trend_date, window_days=1)
            for market in ("za", "ng", "ke"):
                items = select_micro_briefs(
                    sg_rows,
                    market,
                    trend_date,
                    config_terms=load_config_terms(market),
                    stoplist=_stoplist_for(market),
                )
                if items:
                    micro_briefs_by_market[market] = items
            total_items = sum(len(v) for v in micro_briefs_by_market.values())
            print(f"  micro-briefs selected: {total_items}")
        except Exception as exc:
            diagnostics.record("micro_briefs", "failed", reason_code="stage_failed", error=exc)
            logger.error("micro_briefs stage failed (non-fatal): %s", exc, exc_info=True)
            print(f"  micro-briefs error (non-fatal): {type(exc).__name__}")

    # V3 seed_candidates (dark, SEED_CANDIDATES_ENABLED default false). Non-fatal.
    if os.environ.get("SEED_CANDIDATES_ENABLED", "false").lower() == "true":
        print("--- SEED CANDIDATES ---")
        diagnostics.record("seed_candidates", "started")
        try:
            from src.analysis.seed_candidates import run_seed_candidates_stage

            sc_inserted = run_seed_candidates_stage(trend_date)
            print(f"  seed_candidates inserted: {sc_inserted}")
            diagnostics.record(
                "seed_candidates",
                "ok",
                reason_code="no_candidates_inserted" if sc_inserted == 0 else "stage_completed",
            )
        except Exception as exc:
            diagnostics.record("seed_candidates", "failed", reason_code="stage_failed", error=exc)
            logger.error("seed_candidates stage failed (non-fatal): %s", exc, exc_info=True)
            print(f"  seed_candidates error (non-fatal): {type(exc).__name__}")
    else:
        diagnostics.record("seed_candidates", "skipped", reason_code="feature_disabled")

    # Phase 2 brief generation. Gated behind PHASE_2_ENABLED so the cron
    # only hits Vertex AI once the GCP card is updated and we are
    # explicitly ready to spend. Default off; flip to "true" via env var
    # or GitHub Actions secret to enable. Failures here never break the
    # email digest path; the digest just falls back to the v1 layout
    # without per-topic briefs.
    #
    # Runs BEFORE log_pipeline_run so the per-market brief count can be
    # written into pipeline_runs.briefs_generated. Pre-28 May the column
    # was hardcoded to 0, blinding any health check that keyed on it.
    briefs_by_topic: dict[tuple[str, str], dict] | None = None
    briefs_skipped_count = 0
    daily_summary_dict: dict | None = None
    if os.environ.get("PHASE_2_ENABLED", "false").lower() == "true":
        print("--- PHASE 2: BRIEF GENERATION ---")
        try:
            from src.analysis.generate_briefs import generate_briefs

            report = generate_briefs(trend_date=trend_date)
            briefs_skipped_count = len(report.skipped_empty)
            print(
                f"  Briefs generated: {len(report.briefs)} successful, "
                f"{len(report.failures)} failed, "
                f"{briefs_skipped_count} skipped (empty after retry), "
                f"estimated_cost_usd={report.estimated_cost_usd:.5f}"
            )
            if report.skipped_empty:
                print("  Skipped topics: " + ", ".join(f"{m}/{t}" for m, t in report.skipped_empty))
            if report.failures:
                diagnostics.record("brief_generation", "failed", reason_code="reported_failures")
            # Surface a BQ persistence failure loudly. The previous behavior
            # was a silent log.error inside generate_briefs, which let the
            # email digest ship with per-topic cards while trend_analysis
            # stayed empty (Looker dashboard then read zero rows for today).
            # report.persist_succeeded is False whenever the INSERT raised.
            if report.persist_attempted and not report.persist_succeeded:
                diagnostics.record(
                    "brief_persistence",
                    "failed",
                    reason_code="write_failed",
                    error=report.persist_error,
                )
                logger.error(
                    "Phase 2 brief BQ persist FAILED: %d rows would have been "
                    "lost from trend_analysis. Email will still ship in-memory "
                    "briefs but the dashboard will not see them. Error: %s",
                    report.persist_row_count,
                    report.persist_error,
                )
                print(
                    f"  WARNING: BQ persist failed for {report.persist_row_count} "
                    f"briefs (email ships, dashboard will not see them today). "
                    "Reason: write_failed"
                )
            # Repackage as plain dicts so the email layer can stay free
            # of the analysis-layer dataclass dependency. Carries every
            # narrative + paste-ready field a card needs to render.
            #
            # The v1 keys (description_rationale / activation_idea / ...) are
            # what the live v1 email reads and are unchanged. The extra keys
            # (market / topic / headline / trend_score / display plus the
            # trend_synthesis + cultural_context aliases) are additive: the
            # v1 renderer ignores unknown keys, the PULSE v2 renderer reads
            # them. This is what makes the v2 wiring live the moment
            # MAILER_V2_ENABLED flips, with no further code change.
            briefs_by_topic = {
                key: {
                    "description_rationale": b.description_rationale,
                    "activation_idea": b.activation_idea,
                    "visual_anchor": b.visual_anchor,
                    "nano_banana_prompt": b.nano_banana_prompt,
                    "lyria_prompt": b.lyria_prompt,
                    "key_metrics": b.key_metrics,
                    "platforms": b.platforms,
                    "sentiment_summary": b.sentiment_summary,
                    "status_tag": b.status_tag,
                    "top_creators": b.top_creators,
                    "social_refs": b.social_refs,
                    "platform_counts": b.platform_counts,
                    "risk_flags": b.risk_flags,
                    "b24_sentiment_trajectory": b.b24_sentiment_trajectory,
                    # v2 mailer fields (additive; ignored by v1).
                    "market": b.market,
                    "topic": b.topic_group,
                    "headline": b.headline,
                    "trend_score": b.trend_score,
                    "trend_synthesis": b.description_rationale,
                    "cultural_context": b.activation_idea,
                    "display": b.display,
                    "seed_path": b.seed_path or {},
                }
                for key, b in report.briefs.items()
            }
            # Completeness: when a partial prior run today already briefed some
            # topics, force=False skips them (report.skipped_existing) so they are
            # absent from the in-memory set above even though BQ persisted them.
            # Load them back so the email always covers every persisted market
            # (the 28 Jun incident dropped ZA this way). Only fill the gaps so the
            # richer freshly-generated dicts win.
            if report.skipped_existing:
                try:
                    from src.alerts.brief_loader import load_briefs_by_topic_from_bq

                    recovered = load_briefs_by_topic_from_bq(
                        trend_date, keys=report.skipped_existing
                    )
                    added = 0
                    for k, v in recovered.items():
                        if k not in briefs_by_topic:
                            briefs_by_topic[k] = v
                            added += 1
                    print(
                        f"  Recovered {added} already-briefed topic(s) from BQ "
                        f"(skipped_existing={len(report.skipped_existing)}) so the "
                        "email covers every persisted market"
                    )
                except Exception as recover_exc:
                    diagnostics.record(
                        "brief_recovery", "failed", reason_code="query_failed", error=recover_exc
                    )
                    logger.warning(
                        "Could not recover %d skipped briefs from BQ; the email may "
                        "be partial, resend to recover. Error: %s",
                        len(report.skipped_existing),
                        recover_exc,
                    )
        except Exception as exc:
            diagnostics.record("brief_generation", "failed", reason_code="stage_failed", error=exc)
            logger.error("Phase 2 brief generation failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Phase 2 brief generation error (non-fatal): {type(exc).__name__}")

    # Every stage below only enriches briefs for the digest, or persists that
    # enrichment. When a sibling run has already emailed today, none of it can
    # reach a reader: the send further down is skipped, so the work decorates an
    # email that will not go out. The 02:30 fallback was re-running the whole
    # tail after the 00:30 primary had already shipped, spending a second full
    # set of Gemini calls every day and writing the reconcile audit trail twice.
    # This is deliberately its own query rather than a value shared with the
    # send-site guard: that one stays as late as possible, so a sibling that
    # sends while this run is mid-flight still suppresses the duplicate email.
    # Fail-safe follows _email_already_sent_today, which returns False on any
    # error, so a BigQuery blip runs the tail exactly as before rather than
    # silently dropping enrichment.
    enrichment_tail_enabled = not _email_already_sent_today()
    if not enrichment_tail_enabled:
        print("--- PHASE 2 TAIL: SKIPPED (digest already sent by a sibling run) ---")

    # 7-day forecast outlook (Layer 2). Gated behind FORECAST_ENABLED; default
    # off (held: the boosted-tree chip collapses to ~all-steady on a quiet week,
    # so a render reframe + accuracy gate land before any flip). Trains a BigQuery
    # ML BOOSTED_TREE_REGRESSOR over the deep trend_scores history and tags each
    # brief with a heating/steady/cooling outlook chip. Non-fatal: a failure logs
    # and leaves briefs untouched, so the email ships exactly as it would without
    # the forecast.
    if (
        os.environ.get("FORECAST_ENABLED", "false").lower() == "true"
        and briefs_by_topic
        and enrichment_tail_enabled
    ):
        print("--- FORECAST: 7-DAY OUTLOOK ---")
        try:
            from src.scoring.forecast import compute_forecast_outlook, tag_briefs_with_outlook

            outlook = compute_forecast_outlook(trend_date)
            tagged = tag_briefs_with_outlook(briefs_by_topic, outlook)
            print(f"  Forecast outlook tagged {tagged}/{len(briefs_by_topic)} briefs")
        except Exception as exc:
            diagnostics.record("forecast", "failed", reason_code="stage_failed", error=exc)
            logger.error("Forecast outlook failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Forecast outlook error (non-fatal): {type(exc).__name__}")

    # Pan-African story detection (Wave 2). Gated behind PAN_AFRICAN_ENABLED;
    # default off so the cron baseline is unchanged. Reads the day's trend_scores
    # across za/ng/ke, finds topic families rising in 2 or more markets at once,
    # and merges them into pan_african_stories. Non-fatal: a failure logs and the
    # run continues, so the email and the rest of the pipeline are unaffected.
    if os.environ.get("PAN_AFRICAN_ENABLED", "false").lower() == "true" and enrichment_tail_enabled:
        print("--- PAN-AFRICAN: CROSS-MARKET STORIES ---")
        try:
            from src.analysis.pan_african import run_pan_african_stage

            stories = run_pan_african_stage(trend_date)
            print(f"  Pan-African stories written: {len(stories)}")
        except Exception as exc:
            diagnostics.record("pan_african", "failed", reason_code="stage_failed", error=exc)
            logger.error("Pan-African stage failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Pan-African error (non-fatal): {type(exc).__name__}")

    # Comment sentiment (room card). Gated behind COMMENT_SENTIMENT_ENABLED; default
    # off so the cron baseline is unchanged. Pulls each briefed topic's Reddit comment
    # + post bodies, extracts a discussion-mood read and a theme split, and tags the
    # brief so the room card goes LIVE. Non-fatal: a failure logs and leaves the briefs
    # untouched, so the email ships exactly as it would without it.
    if (
        os.environ.get("COMMENT_SENTIMENT_ENABLED", "false").lower() == "true"
        and briefs_by_topic
        and enrichment_tail_enabled
    ):
        print("--- COMMENT SENTIMENT: ROOM CARD ---")
        try:
            from src.analysis.comment_sentiment import (
                compute_comment_sentiment,
                tag_briefs_with_comment_sentiment,
            )

            sentiment = compute_comment_sentiment(trend_date, briefs_by_topic)
            tagged = tag_briefs_with_comment_sentiment(briefs_by_topic, sentiment)
            print(f"  Comment sentiment tagged {tagged}/{len(briefs_by_topic)} briefs")
        except Exception as exc:
            diagnostics.record("comment_sentiment", "failed", reason_code="stage_failed", error=exc)
            logger.error("Comment sentiment failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Comment sentiment error (non-fatal): {type(exc).__name__}")

    # Driving hashtags (the conversation-intelligence slot, paired under the
    # room card). Gated behind DRIVING_HASHTAGS_ENABLED; default off so the cron
    # baseline is unchanged. For each briefed topic, ranks the hashtags driving
    # the conversation (regex-extracted from post text) and runs one Gemini call
    # per top tag for its own mood, then tags the brief so the hashtag slot goes
    # LIVE. Non-fatal: a failure logs and leaves the briefs untouched, so the
    # email ships exactly as it would without it. Runs after comment_sentiment
    # by design: the two together are the conversation-intelligence layer.
    if (
        os.environ.get("DRIVING_HASHTAGS_ENABLED", "false").lower() == "true"
        and briefs_by_topic
        and enrichment_tail_enabled
    ):
        print("--- DRIVING HASHTAGS: CONVERSATION SLOT ---")
        try:
            from src.scoring.driving_hashtags import (
                compute_driving_hashtags,
                tag_briefs_with_driving_hashtags,
            )

            hashtags = compute_driving_hashtags(trend_date, briefs_by_topic)
            tagged = tag_briefs_with_driving_hashtags(briefs_by_topic, hashtags)
            print(f"  Driving hashtags tagged {tagged}/{len(briefs_by_topic)} briefs")
        except Exception as exc:
            diagnostics.record("driving_hashtags", "failed", reason_code="stage_failed", error=exc)
            logger.error("Driving hashtags failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Driving hashtags error (non-fatal): {type(exc).__name__}")

    # Wave 1 continuity + lifecycle badges. The counter (new/day2/day3plus/
    # rebounding) and the lifecycle phase (birth/growth/maturity/decline) are
    # computed in compute_trend_scores and stored on trend_scores; this stage
    # reads them back and tags the briefs so the card render lights the badges.
    # Gated by the same two flags the producer honours: skip entirely when both
    # are off so the OFF path adds no BQ cost and the email is byte-identical.
    # Non-fatal: a failure logs and leaves the briefs untouched.
    if (
        (_lifecycle_enabled() or _continuity_badges_enabled())
        and briefs_by_topic
        and enrichment_tail_enabled
    ):
        print("--- WAVE 1: CONTINUITY + LIFECYCLE BADGES ---")
        try:
            from src.analysis.wave1_badges import (
                fetch_continuity_lifecycle,
                tag_briefs_with_continuity_lifecycle,
            )

            badge_lookup = fetch_continuity_lifecycle(trend_date)
            tagged = tag_briefs_with_continuity_lifecycle(briefs_by_topic, badge_lookup)
            print(f"  Continuity/lifecycle tagged {tagged}/{len(briefs_by_topic)} briefs")
        except Exception as exc:
            diagnostics.record(
                "continuity_lifecycle_badges", "failed", reason_code="stage_failed", error=exc
            )
            logger.error("Continuity/lifecycle tag failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Continuity/lifecycle tag error (non-fatal): {type(exc).__name__}")

    # Seed score (Jo, 22 Jun). compute_trend_scores stored a seed_score on every
    # trend_scores row; this stage reads it back and tags the briefs so the card
    # render lights the SEED chip. Gated by SEED_SCORE_ENABLED (default on).
    # Non-fatal: a failure logs and leaves the briefs untouched.
    if _seed_score_enabled() and briefs_by_topic and enrichment_tail_enabled:
        print("--- SEED SCORE BADGES ---")
        try:
            from src.analysis.wave1_badges import (
                fetch_seed_scores,
                tag_briefs_with_seed_score,
            )

            seed_lookup = fetch_seed_scores(trend_date)
            tagged = tag_briefs_with_seed_score(briefs_by_topic, seed_lookup)
            print(f"  Seed score tagged {tagged}/{len(briefs_by_topic)} briefs")
        except Exception as exc:
            diagnostics.record("seed_score_badges", "failed", reason_code="stage_failed", error=exc)
            logger.error("Seed score tag failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Seed score tag error (non-fatal): {type(exc).__name__}")

    # Wave 2 tone split. The lexicon scorer wrote one sentiment_lexicon_score per
    # enriched_content row; this stage rolls those up to a per-topic list and tags
    # the briefs so the tone-split section can bin them. Gated by the render flag
    # so the OFF path spends no BQ; the section self-hides when a brief has no list.
    # Non-fatal: a failure logs and leaves the briefs untouched.
    if _tone_split_enabled() and briefs_by_topic and enrichment_tail_enabled:
        print("--- WAVE 2: TONE SPLIT (LEXICON SCORES) ---")
        try:
            from src.analysis.tone_lexicon import (
                fetch_lexicon_scores,
                tag_briefs_with_lexicon_scores,
            )

            lexicon_lookup = fetch_lexicon_scores(trend_date)
            tagged = tag_briefs_with_lexicon_scores(briefs_by_topic, lexicon_lookup)
            print(f"  Tone-split lexicon tagged {tagged}/{len(briefs_by_topic)} briefs")
        except Exception as exc:
            diagnostics.record("tone_split", "failed", reason_code="stage_failed", error=exc)
            logger.error("Tone-split tag failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Tone-split tag error (non-fatal): {type(exc).__name__}")

    # PULSE Intelligence Core: RECONCILE in SHADOW mode. Gated behind
    # RECONCILE_ENABLED (default off) so the cron baseline is byte-identical:
    # when the flag is off the whole block is skipped and nothing is built,
    # reconciled, or persisted. When on (shadow) it builds the event ledger,
    # reconciles each market's brief claims against it, and persists ONLY an
    # audit trail to reconcile_actions. It renders NOTHING and mutates NO
    # existing data: briefs_by_topic, every rendered field, and the daily
    # summary are left untouched. RECONCILE_RENDER (default off) is reserved
    # for a later render wiring and is deliberately NOT read here yet, so even
    # with both flags considered the shadow path never alters the email.
    # Non-fatal: a failure logs and the run continues, exactly like FORECAST /
    # PAN_AFRICAN.
    if (
        os.environ.get("RECONCILE_ENABLED", "false").lower() == "true"
        and briefs_by_topic
        and enrichment_tail_enabled
    ):
        print("--- RECONCILE: SHADOW (LEDGER + AUDIT TRAIL) ---")
        try:
            summary = _reconcile_shadow(trend_date, run_id, briefs_by_topic)
            for market in MARKETS:
                n_claims, n_stale, n_labelled = summary.get(market, (0, 0, 0))
                print(
                    f"  reconcile shadow [{market}]: {n_claims} claims, "
                    f"{n_stale} stale, {n_labelled} labelled"
                )
        except Exception as exc:
            diagnostics.record("reconcile_shadow", "failed", reason_code="stage_failed", error=exc)
            logger.error("Reconcile shadow stage failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Reconcile shadow error (non-fatal): {type(exc).__name__}")

    # Persist the render bundle (display + the conversation fields the two
    # producers just tagged) onto the stored briefs, so any send that
    # re-reads briefs from BigQuery (the resend workflow, a preview, a
    # recovery day) carries the same Seen-on channels, state badges, and
    # conversation cards the live email rendered. Non-fatal: a failure logs
    # and the email path ships exactly as it would without it.
    if briefs_by_topic and enrichment_tail_enabled:
        try:
            from src.analysis.generate_briefs import persist_render_payloads

            persisted = persist_render_payloads(trend_date, briefs_by_topic)
            print(f"  Render payload persisted for {persisted}/{len(briefs_by_topic)} briefs")
        except Exception as exc:
            diagnostics.record(
                "render_payload_persistence", "failed", reason_code="write_failed", error=exc
            )
            logger.error("Render payload persist failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Render payload persist error (non-fatal): {type(exc).__name__}")

    # Log pipeline run
    briefs_by_market = _briefs_per_market(briefs_by_topic)
    finished_at = datetime.now(UTC)
    print()
    print("--- PIPELINE LOG ---")
    log_pipeline_run(
        run_id,
        started_at,
        finished_at,
        market_source_counts,
        score_rows,
        errors,
        unclassified_by_market=unclassified_counters,
        skip_markets=skip_markets,
        briefs_by_market=briefs_by_market,
        classification_layer_by_market=classification_layer_counters,
    )
    print(
        "  Logged run to pipeline_runs (briefs_generated: "
        + ", ".join(f"{m}={briefs_by_market.get(m, 0)}" for m in MARKETS)
        + ")"
    )

    elapsed = int((finished_at - started_at).total_seconds())
    print()
    print("=" * 40)
    print(f"raw_content:      {total_raw} rows")
    print(f"enriched_content: {total_enriched} rows")
    print(f"trend_scores:     {rows_scored} rows")
    print(f"pipeline_runs:    {len(MARKETS)} rows")
    print(f"Elapsed:          {elapsed}s")
    print()

    # Cross-trend daily summary. Second Gemini pass over the per-topic
    # briefs producing one cohesive narrative for the top of the email.
    # Failures are non-fatal; the email still ships with per-topic
    # cards even if the summary call dies.
    if os.environ.get("PHASE_2_ENABLED", "false").lower() == "true" and briefs_by_topic:
        try:
            from google.cloud import bigquery as _bq
            from src.analysis.generate_daily_summary import generate_daily_summary
            from src.utils.bigquery import get_client as _get_bq_client

            _bqc = _get_bq_client()
            _ds = get_dataset()
            _scores_sql = f"""
            SELECT market, query_group, trend_score, velocity_score, item_count, seed_score
            FROM `{_bqc.project}.{_ds}.trend_scores`
            WHERE trend_date = @trend_date
            """
            _job = _bqc.query(
                _scores_sql,
                job_config=_bq.QueryJobConfig(
                    query_parameters=[_bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
                ),
            )
            trend_scores_by_topic = {
                (str(r.market), str(r.query_group)): {
                    "trend_score": float(r.trend_score or 0.0),
                    "velocity_score": float(r.velocity_score or 0.0),
                    "item_count": int(r.item_count or 0),
                    "seed_score": float(getattr(r, "seed_score", 0.0) or 0.0),
                }
                for r in _job.result()
            }

            summary = generate_daily_summary(
                trend_date=trend_date,
                briefs_by_topic=briefs_by_topic,
                trend_scores_by_topic=trend_scores_by_topic,
            )
            if summary:
                daily_summary_dict = summary.to_email_dict()
                print(f"  Daily summary: through_line='{summary.through_line[:80]}'")
            else:
                # generate_daily_summary returned None: either the
                # idempotency guard skipped Gemini (a non-empty row
                # already exists for today, e.g. populated by a sibling
                # regen workflow), or the retry path still came back
                # empty. In the "row already exists" case the email
                # should still render the block, so fall back to a BQ
                # read of the existing row.
                try:
                    existing_sql = f"""
                    SELECT summary_text, through_line, call_to_action,
                           key_topics, rising_topics, seed_recommend
                    FROM `{_bqc.project}.{_ds}.daily_summary`
                    WHERE trend_date = @trend_date
                      AND COALESCE(summary_text, '') != ''
                    ORDER BY generated_at DESC
                    LIMIT 1
                    """
                    existing_job = _bqc.query(
                        existing_sql,
                        job_config=_bq.QueryJobConfig(
                            query_parameters=[
                                _bq.ScalarQueryParameter("trend_date", "DATE", trend_date)
                            ]
                        ),
                    )
                    existing_rows = list(existing_job.result())
                    if existing_rows:
                        r = existing_rows[0]
                        daily_summary_dict = {
                            "summary_text": r.summary_text or "",
                            "through_line": r.through_line or "",
                            "call_to_action": r.call_to_action or "",
                            "key_topics": list(r.key_topics or []),
                            "rising_topics": list(r.rising_topics or []),
                            "seed_recommend": r.seed_recommend or "",
                        }
                        print(
                            "  Daily summary: skipped Gemini call (row already "
                            "exists for today); rehydrated from BQ for the email."
                        )
                    else:
                        print(
                            "  Daily summary: skipped (no briefs OR retry-still-"
                            "empty); email will render without the summary block."
                        )
                except Exception as exc_fallback:
                    diagnostics.record(
                        "daily_summary_recovery",
                        "failed",
                        reason_code="query_failed",
                        error=exc_fallback,
                    )
                    logger.error(
                        "Daily summary BQ rehydrate failed (non-fatal): %s",
                        exc_fallback,
                    )
                    print(
                        f"  Daily summary BQ rehydrate error (non-fatal): {type(exc_fallback).__name__}"
                    )
        except Exception as exc:
            diagnostics.record("daily_summary", "failed", reason_code="stage_failed", error=exc)
            logger.error("Daily summary generation failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Daily summary error (non-fatal): {type(exc).__name__}")

    # Seed intelligence: a cross-topic Gemini pass mines the day's briefs for the
    # hidden seedable BEHAVIOURS (not topics) worth a Nanobanana/Lyria activation.
    # Additive and non-fatal: it persists to seed_insights for the tool + chat and
    # never touches the email or the briefs. seed_score rides on the briefs from
    # the tagging stage, so no trend_scores re-read is needed here.
    if _seed_intelligence_enabled() and briefs_by_topic:
        print("--- SEED INTELLIGENCE ---")
        try:
            from src.analysis.generate_seed_intelligence import generate_seed_intelligence

            seeds = generate_seed_intelligence(
                trend_date=trend_date,
                briefs_by_topic=briefs_by_topic,
            )
            print(f"  Seed intelligence: {len(seeds)} behaviours mined")
        except Exception as exc:
            diagnostics.record("seed_intelligence", "failed", reason_code="stage_failed", error=exc)
            logger.error("Seed intelligence failed (non-fatal): %s", exc, exc_info=True)
            print(f"  Seed intelligence error (non-fatal): {type(exc).__name__}")

    # Daily email digest. Fires when there is substance to report: tier
    # upgrades, material score movers, or pipeline issues. Quiet days with
    # nothing notable stay silent. Runs after pipeline_log so elapsed and
    # final row counts are available. Failures are caught so email delivery
    # never breaks the pipeline.
    print("--- EMAIL DIGEST ---")
    if skip_markets == set(MARKETS):
        score_rows, briefs_by_topic, daily_summary_dict = _rehydrate_fallback_email_inputs(
            trend_date,
            score_rows,
            briefs_by_topic,
            daily_summary_dict,
        )
    # Carry the micro-briefs selection onto daily_summary_dict so render_html
    # can pick it up without a new parameter threaded through send_daily_digest
    # / send_email_digest. Only set when the flag is on and there is at least
    # one item, so a flag-off or empty day never adds the key.
    if micro_briefs_by_market:
        if daily_summary_dict is None:
            daily_summary_dict = {}
        daily_summary_dict["micro_briefs_by_market"] = micro_briefs_by_market
    # Email outcome recorded on a separate pipeline_runs marker row after
    # the send resolves (see _log_email_status below). Initialised here so
    # both the success path and the except path below set a real value.
    # 'render_error' is the safe default: if render or dispatch raises
    # before status is reassigned, the marker records the failure rather
    # than a misleading 'sent'.
    email_status = "render_error"
    digest_exc: Exception | None = None
    try:
        from src.alerts.detector import send_daily_digest
        from src.alerts.email_digest import HealedIssue, IssueFlag
        from src.alerts.self_heal import heal_errors

        # Auto-heal RSS 404s before surfacing errors in the digest. Any
        # feed we can re-discover gets its URL rewritten in sources.yaml
        # and moves from "Still broken" to "Auto-resolved" in the email.
        sources_yaml_path = Path(__file__).parent.parent / "configs" / "sources.yaml"
        heal_results, remaining_errors = heal_errors(errors, sources_yaml_path)
        # Only surface fixes that actually reached disk. A kind="fixed"
        # result with persisted=False means the YAML rewrite failed
        # (permissions, disk full, file locked); advertising it in the
        # email would be a lie because next day's run sees the same 404.
        healed_fixed = [r for r in heal_results if r.kind == "fixed" and r.persisted]

        # Map each original broken URL back to its market via the errors list.
        url_to_market: dict[str, str] = {}
        for e in errors or []:
            if not isinstance(e, dict):
                continue
            mkt = str(e.get("market", "?")).upper()
            err_str = str(e.get("error", ""))
            for r in healed_fixed:
                if r.original_url and r.original_url in err_str:
                    url_to_market[r.original_url] = mkt

        healed_flags = [
            HealedIssue(
                label=f"RSS ({url_to_market.get(r.original_url, '?')})",
                original_url=r.original_url,
                new_url=r.new_url or "",
                reason=r.reason,
            )
            for r in healed_fixed
        ]
        if heal_results:
            fixed_unpersisted = sum(
                1 for r in heal_results if r.kind == "fixed" and not r.persisted
            )
            extra = f", {fixed_unpersisted} unpersisted" if fixed_unpersisted else ""
            print(
                f"  Self-heal: {len(healed_fixed)} fixed{extra}, "
                f"{sum(1 for r in heal_results if r.kind == 'still_broken')} still broken, "
                f"{sum(1 for r in heal_results if r.kind == 'skipped')} skipped"
            )

        stats = {
            "raw_rows": total_raw,
            "enriched_rows": total_enriched,
            "scored_rows": rows_scored,
            "elapsed_seconds": elapsed,
            "errors_count": len(remaining_errors) if remaining_errors else 0,
            "unclassified_rows": sum(unclassified_counters.values()),
            "briefs_skipped": briefs_skipped_count,
        }
        issue_flags = [
            IssueFlag(
                label=f"{e.get('source', '?').upper()} ({e.get('market', '?').upper()})",
                detail=str(e.get("error", ""))[:200],
            )
            for e in (remaining_errors or [])
            if isinstance(e, dict)
        ]
        if _email_already_sent_today():
            # A sibling run today (the GitHub schedule or the Cloud Run job)
            # already sent the digest. Skip the duplicate send so stakeholders
            # get one email, not two. Ingestion is separately guarded above.
            email_status = "skipped_already_sent"
            print(
                "  Skipping email: today's digest was already sent by a sibling run (idempotency guard)"
            )
        else:
            markets, upgrades, movers, status = send_daily_digest(
                score_rows,
                stats=stats,
                issues=issue_flags,
                healed=healed_flags,
                briefs_by_topic=briefs_by_topic,
                daily_summary=daily_summary_dict,
            )
            # send_daily_digest's 4th element separates a real SMTP/auth blip
            # ('send_failed') from a legitimately quiet day
            # ('skipped_nothing_notable'). Carry it verbatim onto the marker
            # row; the render-crash path is covered by the except below.
            email_status = status
            if status == "skipped_nothing_notable":
                print("  Skipped email: no upgrades, movers, heals, or issues to report")
            else:
                print(
                    f"  {upgrades} upgrade(s) across {markets} market(s), "
                    f"{movers} mover(s), {len(healed_flags)} auto-fixed, "
                    f"{len(issue_flags)} issue(s), status={status}"
                )
    except Exception as exc:
        # A render crash (render_html / render_text / _pulse_inbox_body /
        # archive_html_to_gcs) raises here rather than returning False.
        # email_status stays 'render_error' from its init above so the
        # marker row records this no-email path too.
        digest_exc = exc
        logger.error("Email dispatch failed (non-fatal): %s", exc, exc_info=True)
        print(f"  Email dispatch error (non-fatal): {type(exc).__name__}")

    # Record the email outcome on its own pipeline_runs marker row. This is
    # the ONLY place the email result lands in BigQuery, so a swallowed
    # SMTP/auth/render failure becomes visible to bq-snapshot / morning-check
    # instead of dying silently in stdout. The marker uses status='email_audit'
    # (not 'success') so the idempotency guard in check_already_ran_today.py
    # (WHERE status='success') ignores it and cron skip behaviour is unchanged.
    # No ingestion table is touched, so velocity baselines are never skewed.
    _log_email_status(run_id, env, started_at, email_status)

    if email_status not in _EMAIL_STATUS_HEALTHY:
        diagnostics.record(
            "digest",
            email_status if email_status in _EMAIL_STATUS_VALUES else "failed",
            source="email",
            event_type="digest_failed",
            error=digest_exc,
            fatal=True,
            reason_code=email_status
            if email_status in _EMAIL_STATUS_VALUES
            else "unknown_email_status",
        )
    else:
        diagnostics.record(
            "digest",
            "skipped" if email_status.startswith("skipped_") else "ok",
            source="email",
            event_type="digest_complete",
            reason_code=email_status,
        )

    print()
    print("Refresh Looker Studio to see all four views populated.")


@batch_events()
def run():
    global _ACTIVE_WAVE1_EXECUTION

    diagnostics = _RunDiagnostics()
    diagnostics.record("cron_run", "started", event_type="cron_run_started")
    try:
        result = _run_impl(diagnostics=diagnostics)
    except Exception as error:
        if execution_approval._is_unresolved_result(error):
            diagnostics.record(
                "cron_run",
                "unavailable",
                event_type="cron_run",
                reason_code="execution_result_unresolved",
                error=error,
            )
            _ACTIVE_WAVE1_EXECUTION = None
            raise
        diagnostics.record(
            "cron_run",
            "failed",
            event_type="cron_run",
            reason_code="unhandled_exception",
            error=error,
            fatal=True,
        )
        if _ACTIVE_WAVE1_EXECUTION is not None:
            authority, consumption, run_id = _ACTIVE_WAVE1_EXECUTION
            failure_payload = {
                "error_code": "wave1_pilot_failed",
                "run_id": run_id,
                "status": "failed",
            }
            failure_json = canonical_bytes(failure_payload).decode("utf-8")
            failure_digest = hashlib.sha256(failure_json.encode()).hexdigest()
            execution_approval._record_execution_result(
                authority,
                consumption,
                authority.execution_name + "#wave1-pilot-failed",
                failure_json,
                failure_digest,
                "failed",
            )
            _ACTIVE_WAVE1_EXECUTION = None
        raise
    else:
        diagnostics.record(
            "cron_run",
            "degraded" if diagnostics.failed_stages else "ok",
            event_type="cron_run",
            reason_code="stage_failures" if diagnostics.failed_stages else "run_completed",
        )
        return result


if __name__ == "__main__":
    run()
