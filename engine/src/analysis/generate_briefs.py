"""Trend brief orchestrator: BQ sample -> Gemini call -> persist.

The cross-cut between scoring and delivery. Reads the day's
``trend_scores``, samples representative ``enriched_content`` rows and
top creators per (market, topic_group), runs each through the Gemini
trend brief prompt, and writes the structured result to
``trend_analysis``. The same dict is returned in-memory so the email
digest renderer can pull it without a second BQ round trip.

Brief shape returned by Gemini (see prompts/trend_brief.py):
- ``description_rationale`` -> persisted as ``trend_synthesis``
- ``activation_idea`` -> persisted as ``cultural_context``
- ``key_metrics`` (list[3]) -> persisted as ``campaign_angles``
- ``platforms`` (list) -> persisted as ``platforms`` column
- ``sentiment_summary`` -> persisted as ``sentiment_summary``
- ``status_tag`` -> persisted as ``status_tag``

Schema dependency
=================

The trend_analysis table carries the current column set this module
persists. The columns were added incrementally by the additive
migrations in ``scripts/migrations/``; see those scripts for the exact
ALTER statements and the order they were applied.

Cost
====

One Gemini call per topic, ~$0.0065. Default is to brief the top 8
topics per market per run, so 24 calls per day = ~$5/month.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from google.cloud import bigquery as bq

try:
    from langdetect import DetectorFactory, LangDetectException, detect_langs

    DetectorFactory.seed = 0  # deterministic results across runs
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False

from src.analysis.display_layer import (
    act_window,
    channel_weights,
    confidence_label,
    in_market_pct,
    search_read,
    trend_phase,
    trend_state,
)
from src.analysis.gemini_client import BriefResponse, GeminiClient
from src.analysis.prompts.trend_brief import (
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    _humanise_count,
    build_brief_prompt,
)
from src.utils.bigquery import get_client, get_dataset, insert_dataframe
from src.utils.config_loader import load_scoring
from src.utils.gemini_usage import (
    UsageSink,
    persist_gemini_usage,
    record_usage,
    usage_rows,
)

# Back-compat aliases for the geo-collision blocklist + helpers. The data
# and functions moved to `src/utils/geo_blocklist.py` on 28 May 2026 so
# the brief generator and the topic classifier share a single source of
# truth. These aliases preserve the private names existing tests and
# downstream callers reach for. F401 noqa is intentional: each alias is
# imported as a public-by-convention re-export even when this module does
# not call it directly.
from src.utils.geo_blocklist import (  # noqa: F401
    FOREIGN_LATIN_CHARSETS as _FOREIGN_LATIN_CHARSETS,
)
from src.utils.geo_blocklist import (  # noqa: F401
    NON_SSA_SCRIPT_RANGES as _NON_SSA_SCRIPT_RANGES,
)
from src.utils.geo_blocklist import (
    TOPIC_GEO_BLOCKLIST as _TOPIC_GEO_BLOCKLIST,
)
from src.utils.geo_blocklist import (  # noqa: F401
    has_foreign_latin_density as _has_foreign_latin_density,
)
from src.utils.geo_blocklist import (  # noqa: F401
    has_non_ssa_script as _has_non_ssa_script,
)
from src.utils.geo_blocklist import (  # noqa: F401
    normalize_haystack_for_geo_match as _normalize_haystack_for_geo_match,
)
from src.utils.geo_blocklist import (
    row_matches_geo_blocklist as _row_matches_geo_blocklist,
)
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

DEFAULT_TOP_N_PER_MARKET = 8
DEFAULT_SAMPLE_ROWS = 10
DEFAULT_TOP_CREATORS = 5
# One market's brief cap = DEFAULT_TOP_N_PER_MARKET; flush per market so a mid-run kill preserves completed briefs.
PERSIST_CHUNK_SIZE = 8
_ALLOWED_MARKETS: frozenset[str] = frozenset({"za", "ng", "ke"})


def _validate_market(market: str) -> str:
    if market not in _ALLOWED_MARKETS:
        raise ValueError(f"market {market!r} not in {_ALLOWED_MARKETS}")
    return market


@dataclass
class TopicBrief:
    """A single completed brief ready for persistence + email rendering.

    The Gemini-output narrative fields (description_rationale, activation_idea,
    nano_banana_prompt, lyria_prompt, key_metrics, platforms, sentiment_summary,
    status_tag) sit alongside two orchestrator-computed fields drawn directly
    from BigQuery (top_creators, social_refs). Splitting the work this way
    keeps the Gemini prompt small (no need to ask the model to echo back data
    it already has) and lets us surface paste-ready creator handles + social
    URLs in the email without trusting the model to render them faithfully.

    top_creators / social_refs are stored as ARRAY<STRING> using a '|'
    delimiter so they slot into existing trend_analysis schema patterns
    without a full STRUCT migration. Format:
      top_creators: '@handle | platform | mentions'
      social_refs:  'url | platform | title'
    """

    market: str
    topic_group: str
    trend_score: float
    description_rationale: str
    activation_idea: str
    visual_anchor: str
    nano_banana_prompt: str
    lyria_prompt: str
    key_metrics: list[str]
    platforms: list[str]
    sentiment_summary: str
    status_tag: str
    top_creators: list[str]
    social_refs: list[str]
    platform_counts: list[str]
    prompt_tokens: int
    completion_tokens: int
    model: str
    # Per-project Brand24 sentiment trajectory label (Workstream E). Same
    # value for every brief in a market; computed once per run in
    # generate_briefs from the brand24_mention_sentiment aggregate rows.
    # Empty string when the Wave-2 sentiment surface is dark or has no rows
    # for this market today. Defaulted so the _to_topic_brief constructor
    # (which does not pass it) stays unchanged; populated post-construction.
    b24_sentiment_trajectory: str = ""
    # One-line editorial headline from Gemini (PULSE v2 mailer). Read off
    # the parsed response in _to_topic_brief. Empty string when the model
    # did not return one; the v2 renderer falls back to a humanised slug,
    # so an empty headline is safe. Persisted to trend_analysis.headline
    # (column added by add_headline_column_to_trend_analysis.py).
    headline: str = ""
    # Brand-safety / sensitivity flags from Gemini (PULSE v2 mailer).
    # Persisted to trend_analysis.risk_flags (was hardcoded to [] before).
    # Defaulted so existing constructors that do not pass it keep working.
    risk_flags: list[str] = field(default_factory=list)
    # PULSE v2 display bundle: state badge, phase, act window, in-market
    # geo share, channel weights, confidence, search read. Computed once
    # per brief in generate_briefs from src/analysis/display_layer.py off
    # data the engine already has (score, velocity, prior-day score,
    # platform counts, sample rows). The v2 email renderer reads this; the
    # v1 email ignores it. Empty dict default so non-pipeline callers and
    # existing tests stay unchanged.
    display: dict[str, Any] = field(default_factory=dict)
    # V3 behaviour path (A6a), gated by SEED_PATH_RENDER_ENABLED at call site.
    seed_path: dict[str, Any] = field(default_factory=dict)

    def to_bq_row(self, trend_date: date, cycle_id: str | None = None) -> dict[str, Any]:
        """Map to a row dict matching the trend_analysis BQ schema."""
        return {
            "analysis_id": str(uuid.uuid4()),
            "trend_date": trend_date,
            "market": self.market,
            "query_group": self.topic_group,
            "cycle_id": cycle_id,
            "trend_score": float(self.trend_score),
            "headline": self.headline,
            "trend_synthesis": self.description_rationale,
            "cultural_context": self.activation_idea,
            "campaign_angles": list(self.key_metrics),
            "risk_flags": list(self.risk_flags),
            "gemini_model": self.model,
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "analyzed_at": datetime.now(UTC),
            "platforms": list(self.platforms),
            "sentiment_summary": self.sentiment_summary,
            "status_tag": self.status_tag,
            "visual_anchor": self.visual_anchor,
            "nano_banana_prompt": self.nano_banana_prompt,
            "lyria_prompt": self.lyria_prompt,
            "top_creators": list(self.top_creators),
            "social_refs": list(self.social_refs),
            "platform_counts": list(self.platform_counts),
            "b24_sentiment_trajectory": self.b24_sentiment_trajectory,
            # The PULSE v2 render bundle. Persisted so a send that re-reads
            # briefs from BigQuery (resend workflow, preview, recovery day)
            # carries the same Seen-on channels, state badge, and chips the
            # live cron rendered. The conversation fields are merged into
            # this JSON later by the pipeline, after the producers run.
            "render_payload": json.dumps({"display": self.display or {}}, default=str),
        }


@dataclass
class GenerateBriefsReport:
    """Per-run summary that the digest stats + observability code can read."""

    briefs: dict[tuple[str, str], TopicBrief] = field(default_factory=dict)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    skipped_empty: list[tuple[str, str]] = field(default_factory=list)
    skipped_existing: list[tuple[str, str]] = field(default_factory=list)
    # Topics skipped before Vertex was even called because the surviving
    # sample (after geo filter) was too thin to support a useful brief.
    # See _sample_is_too_thin() for the rule. Added 25 May 2026 as the
    # real fix for the noise-floor brief-failure pattern that path A
    # (Monitoring floor raise, commit 346ddd3) tried and failed to fix.
    skipped_thin_sample: list[tuple[str, str, int, int]] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    # Surface BQ persistence outcome so the orchestrator + email layer can
    # tell when in-memory briefs were emailed without ever landing in
    # trend_analysis. Open audit finding (5 May 2026): the previous behavior
    # was a silent log.error on insert_dataframe failure, which meant 24
    # briefs could vanish from the dashboard while the email still shipped.
    persist_attempted: bool = False
    persist_succeeded: bool = False
    persist_error: str | None = None
    persist_row_count: int = 0

    @property
    def estimated_cost_usd(self) -> float:
        from src.analysis.gemini_client import _estimate_cost_usd, _resolve_model

        # All briefs in a run share one model; price at it. Fall back to the
        # env-resolved model when no briefs landed (all failed or skipped).
        model = next((b.model for b in self.briefs.values() if b.model), None) or _resolve_model(
            None
        )
        return _estimate_cost_usd(model, self.total_prompt_tokens, self.total_completion_tokens)


def _is_empty_brief(brief: TopicBrief) -> bool:
    """Return True when any narrative field on the brief is empty.

    A "valid" brief must have a description rationale, an activation idea,
    and at least one key metric. Missing any of those means the Gemini
    response failed schema validation or hit a safety filter and returned
    a stub. Such rows are useless to stakeholders and should not persist.
    """
    return (
        not (brief.description_rationale or "").strip()
        or not (brief.activation_idea or "").strip()
        or not brief.key_metrics
        or not (brief.visual_anchor or "").strip()
        or not (brief.nano_banana_prompt or "").strip()
        or not (brief.lyria_prompt or "").strip()
    )


_RETRY_PROMPT_ADDENDUM = (
    "\n\nIMPORTANT: Your previous response was empty or did not match the "
    "schema. Return a complete JSON object filling EVERY field defined in "
    "the schema. The description_rationale must be at least 80 words across "
    "two short paragraphs. The activation_idea must be at least 60 words and "
    "name a specific creator type, format, and cultural hook. key_metrics "
    "must be exactly 3 short sentences. platforms must be a non-empty list "
    "of platform names. sentiment_summary and status_tag must both be set. "
    "Do not return null, empty strings, or empty arrays for any field."
)


def _should_retry_empty(response: BriefResponse) -> bool:
    """Decide whether retrying an empty Gemini response is worth the spend.

    Retries when raw_text is empty OR JSON-shaped. Skips retry only when
    raw_text contains prose (the safety-filter refusal stub case like
    "I cannot generate this content...").

    Why retry on empty raw_text: 27 May 2026 ng/politics_tinubu came back
    with an empty response on the first attempt (Vertex 200 with empty body,
    likely a transient classifier flicker on the political-figure name).
    A backfill 60 minutes later landed cleanly on first try, confirming the
    failure was transient not deterministic. The old behavior returned False
    on empty raw_text and skipped the retry entirely, costing one brief per
    day on average. A second attempt costs ~$0.0065 and recovers this class
    of transient cleanly. Prose-stub refusals still short-circuit because
    they are deterministic safety-filter decisions that a retry will not
    flip.
    """
    raw = (response.raw_text or "").lstrip()
    # Empty raw_text: transient Vertex 200-with-empty-body. Retry is cheap
    # and recovers the politics_tinubu-class case.
    if not raw:
        return True
    # JSON-shaped: model attempted structure, retry has real chance.
    # Prose stub (e.g. "I cannot generate this...") is a deterministic
    # safety-filter refusal; retry just wastes spend.
    return raw[0] in ("{", "[")


# Optional score floor for brief generation. Defaults to 0.0 (no filter)
# so the brief generator always tries the top N per market regardless
# of score. Path A from 22 May 2026 (commit 346ddd3) set this to 0.30
# on the theory that noise-floor topics were failing brief generation;
# the 23-25 May audits showed the floor stripped 10-13 briefs per day
# (~half the digest) without solving the root cause. Reverted on 25
# May 2026: the real failure mode is sample-row thinness after geo
# filtering, not low score. Plan: gate brief generation on the
# surviving-row count after the geo filter runs, not on the trend
# score itself. Tracked as the next iteration; until then this gate
# stays at 0.0 (no filter) so we get the full top-N.
_BRIEF_GEN_SCORE_FLOOR: float = 0.0


def _query_top_topics(
    client: bq.Client,
    dataset: str,
    trend_date: date,
    top_n_per_market: int,
    min_score: float = _BRIEF_GEN_SCORE_FLOOR,
) -> pd.DataFrame:
    """Pull the top N (market, topic_group) rows from today's trend_scores.

    The ``min_score`` parameter defaults to 0.0 so all top-N topics get
    briefed regardless of score. Pass a positive value to filter out
    sub-floor topics. See ``_BRIEF_GEN_SCORE_FLOOR`` for the history of
    why this gate is currently dormant.

    Uses bound parameters; no caller-supplied identifier is interpolated
    so this stays safe under the project's SQL hardening policy.
    """
    sql = f"""
    SELECT market, query_group AS topic_group, trend_score, item_count,
           source_diversity, creator_spread, velocity_score, tone_score,
           tone_rows, search_velocity_score
    FROM `{client.project}.{dataset}.trend_scores`
    WHERE trend_date = @trend_date
      AND trend_score >= @min_score
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY market
        ORDER BY trend_score DESC, item_count DESC, query_group ASC
    ) <= @top_n
    ORDER BY market, trend_score DESC, query_group ASC
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("top_n", "INT64", int(top_n_per_market)),
            bq.ScalarQueryParameter("min_score", "FLOAT64", float(min_score)),
        ]
    )
    return client.query(sql, job_config=job_config).to_dataframe()


# Per-topic geo-collision blocklist + foreign-language detectors live in
# `src/utils/geo_blocklist.py` so the topic classifier and the brief
# generator both reference the same data. The aliases below preserve the
# private names existing tests + downstream callers reach for.
# Hard-foreign language codes. SSA content (English / NG Pidgin / Sheng /
# Swahili / Afrikaans / Zulu / Xhosa) never classifies as any of these,
# so high-confidence detection of one of these languages is a reliable
# signal the row is foreign noise that the keyword blocklist missed.
#
# Explicitly NOT in the list: tl / id / ms / so. NG Pidgin and Sheng
# routinely mis-classify into these (probed 26 May 2026 with 10 real
# pidgin samples: 5x en, 2x tl, 2x so, 1x id). Blocklisting tl/id/so
# would strip legitimate pidgin content.
#
# Captured here so the script-block + Latin-density layers do not need
# to grow further for each new keyword-blocklist round. Catches
# Vietnamese-with-Latin (no diacritics in some posts), Hindi
# transliterated to Latin, Turkish without distinctive diacritics, etc.
_HARD_FOREIGN_LANGS: frozenset[str] = frozenset(
    {
        "vi",  # Vietnamese
        "th",  # Thai
        "ar",  # Arabic
        "hi",  # Hindi
        "de",  # German
        "ru",  # Russian
        "ja",  # Japanese
        "zh-cn",  # Simplified Chinese
        "zh-tw",  # Traditional Chinese
        "ko",  # Korean
        "tr",  # Turkish
        "fa",  # Persian
        "ur",  # Urdu
    }
)

# Minimum text length before running langdetect. Below this the detector
# returns noisy results that are not safe to act on (NG pidgin samples
# at 20-30 chars frequently mis-classify into hard-foreign languages).
_LANGDETECT_MIN_CHARS: int = 40

# Probability threshold for dropping. Conservative so we never strip
# legitimate SSA content. langdetect returns multiple candidates with
# probabilities; we only act when the top candidate is BOTH hard-foreign
# AND highly confident.
_LANGDETECT_DROP_PROB: float = 0.90


def _row_is_hard_foreign_language(row: dict[str, Any]) -> bool:
    """Return True when langdetect identifies the row as hard-foreign.

    Only acts on text longer than ``_LANGDETECT_MIN_CHARS`` (40), and
    only when langdetect's top candidate language is in
    ``_HARD_FOREIGN_LANGS`` with probability above
    ``_LANGDETECT_DROP_PROB`` (0.90). Both thresholds are deliberately
    conservative to avoid pidgin / Sheng false positives.

    Returns False if langdetect is not available (graceful degradation
    during pyproject install hiccups), if text is too short to detect,
    or if the detection itself errors.
    """
    if not _LANGDETECT_AVAILABLE:
        return False
    text = (str(row.get("title") or "") + " " + str(row.get("text") or "")).strip()
    if len(text) < _LANGDETECT_MIN_CHARS:
        return False
    try:
        results = detect_langs(text)
    except LangDetectException:
        return False
    if not results:
        return False
    top = results[0]
    return top.lang in _HARD_FOREIGN_LANGS and top.prob >= _LANGDETECT_DROP_PROB


# Sample-thinness thresholds for the brief-generation gate. A sample is
# "too thin" if either:
#   1. Fewer than _MIN_SAMPLE_ROWS rows survive the geo-collision filter, OR
#   2. The total title + text length across all surviving rows is less
#      than _MIN_SAMPLE_TEXT_CHARS.
#
# The two thresholds together catch both failure shapes observed during
# the 14-25 May 2026 cron observation window:
#   - Low row count (typical for noise-floor topics where the geo filter
#     stripped most rows)
#   - Adequate row count BUT mostly Brand24 trending-hashtag metadata
#     with thin text bodies (the 22 May ng/economy_sapa_hustle case had
#     15 rows but produced empty Vertex responses on both attempts)
#
# Set 25 May 2026 as the real fix for the noise-floor brief-failure
# pattern that path A (raising the Monitoring tier floor to 0.30,
# commit 346ddd3) tried and failed to fix. Path A stripped 10-13 briefs
# per day; this gate skips only the ~1-3 per day that would have failed
# anyway.
_MIN_SAMPLE_ROWS: int = 5
# Lowered 2000 to 1000 on 26 May 2026 after the gate stripped 13 of 24
# briefs. 2000 over-penalised TikTok and YouTube heavy topics where each
# row's text is short by platform shape (50 to 100 chars). Top-10 char
# totals for music_afrobeats, music_amapiano, politics_maandamano on
# 26 May were 1318, 1689, 1589, all valid samples that were being killed.
# 1000 still catches the original 22 May ng/economy_sapa_hustle thin
# metadata case which had under 500 chars across the surviving sample.
_MIN_SAMPLE_TEXT_CHARS: int = 1000

# For topics in _TOPIC_GEO_BLOCKLIST the geo filter strips a large share
# of rows, leaving fewer chars in the surviving sample. Apply a looser
# floor for these topics so a clean small sample still goes through to
# Vertex instead of being skipped. 500 chars across 5+ surviving rows is
# enough for the brief writer to produce a useful output; quality-judge
# (Phase 3 of the Carla roadmap) will catch any weak briefs downstream.
_MIN_SAMPLE_TEXT_CHARS_GEO_BLOCKLIST: int = 500


def _sample_is_too_thin(
    sample: list[dict[str, Any]], topic_group: str | None = None
) -> tuple[bool, int, int]:
    """Decide whether the surviving sample is too thin to brief on.

    Returns a 3-tuple ``(is_thin, row_count, total_text_chars)``. The
    caller logs the row + char counts on skip so future audits can see
    the threshold that fired.

    Counts title + text length, not URL, because URLs are pure metadata
    and don't help the brief writer produce useful content.

    Topics in ``_TOPIC_GEO_BLOCKLIST`` get a looser char floor because
    the geo filter strips a large share of their rows; see the constant
    docstring above.
    """
    row_count = len(sample)
    total_chars = sum(
        len(str(row.get("title") or "")) + len(str(row.get("text") or "")) for row in sample
    )
    char_floor = (
        _MIN_SAMPLE_TEXT_CHARS_GEO_BLOCKLIST
        if topic_group in _TOPIC_GEO_BLOCKLIST
        else _MIN_SAMPLE_TEXT_CHARS
    )
    is_thin = row_count < _MIN_SAMPLE_ROWS or total_chars < char_floor
    return is_thin, row_count, total_chars


def _sample_rows_for_topic(
    client: bq.Client,
    dataset: str,
    market: str,
    topic_group: str,
    trend_date: date,
    n: int,
) -> list[dict[str, Any]]:
    """Pull n representative rows from today's enriched_content for this topic.

    Applies a geo-collision blocklist for topics whose slang collides
    with a foreign place / brand (see ``_TOPIC_GEO_BLOCKLIST``). Pulls
    3x the requested limit so the post-filter sample still has enough
    rows after contamination is dropped.

    Additionally drops rows detected by langdetect as hard-foreign
    languages (Vietnamese, Thai, Arabic, Hindi, German, Russian, etc.)
    for topics in the geo blocklist. The language filter catches
    foreign-noise survivors that the keyword blocklist missed without
    risking pidgin / Sheng false positives. See
    ``_row_is_hard_foreign_language``.
    """
    market = _validate_market(market)
    fetch_n = int(n) * 3 if topic_group in _TOPIC_GEO_BLOCKLIST else int(n)
    # Pull the engagement magnitudes alongside the text. The ORDER BY is a
    # binary engagement>0 CASE (plus a title-length nudge) then RAND(), so the
    # sample is randomized among rows with any engagement rather than ranked by
    # reach; carrying the numbers through lets build_brief_prompt cite real
    # reach instead of leaving the model to invent a figure.
    sql = f"""
    SELECT title, text, platform, url,
           engagement_total, views, likes, comments, shares
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = @trend_date
      AND market = @market
      AND @topic_group IN UNNEST(topic_groups)
      AND (LENGTH(IFNULL(title,'')) > 0 OR LENGTH(IFNULL(text,'')) > 0)
    ORDER BY (
      CASE WHEN engagement_total > 0 THEN 1 ELSE 0 END
      + CASE WHEN LENGTH(IFNULL(title,'')) > 20 THEN 1 ELSE 0 END
    ) DESC, RAND()
    LIMIT @lim
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("market", "STRING", market),
            bq.ScalarQueryParameter("topic_group", "STRING", topic_group),
            bq.ScalarQueryParameter("lim", "INT64", fetch_n),
        ]
    )
    rows: list[dict[str, Any]] = []
    for row in client.query(sql, job_config=job_config).result():
        candidate = {
            "title": row.title or "",
            "text": row.text or "",
            "platform": row.platform or "",
            "url": row.url or "",
            "engagement_total": float(row.engagement_total or 0.0),
            "views": float(row.views or 0.0),
            "likes": float(row.likes or 0.0),
            "comments": float(row.comments or 0.0),
            "shares": float(row.shares or 0.0),
        }
        if _row_matches_geo_blocklist(candidate, topic_group):
            continue
        # Language filter only runs on geo-blocklist topics (cost control;
        # langdetect on every row of every topic adds ~30s pipeline time
        # while adding zero signal on topics without foreign-noise risk).
        if topic_group in _TOPIC_GEO_BLOCKLIST and _row_is_hard_foreign_language(candidate):
            continue
        rows.append(candidate)
        if len(rows) >= int(n):
            break
    return rows


def _platform_counts_for_topic(
    client: bq.Client,
    dataset: str,
    market: str,
    topic_group: str,
    trend_date: date,
) -> list[dict[str, Any]]:
    """Pull per-platform content counts for this topic on this date.

    Returns a list of dicts with keys ``platform`` and ``count``, sorted
    descending by count. Surfaces in the email card so stakeholders see
    'TikTok (124) | Instagram Reels (87)' rather than a bare platform
    list, per Thapelo's request 2026-05-05.

    Empty platform values are normalised to 'web' so the renderer never
    shows blank chips. Uses bound parameters; no caller-supplied
    identifier is interpolated.
    """
    market = _validate_market(market)
    sql = f"""
    SELECT
      IF(LENGTH(IFNULL(platform, '')) = 0, 'web', LOWER(platform)) AS platform,
      COUNT(*) AS n
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = @trend_date
      AND market = @market
      AND @topic_group IN UNNEST(topic_groups)
    GROUP BY platform
    ORDER BY n DESC, platform ASC
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("market", "STRING", market),
            bq.ScalarQueryParameter("topic_group", "STRING", topic_group),
        ]
    )
    return [
        {"platform": row.platform, "count": int(row.n or 0)}
        for row in client.query(sql, job_config=job_config).result()
    ]


def _peak_engagement_for_topic(
    client: bq.Client,
    dataset: str,
    market: str,
    topic_group: str,
    trend_date: date,
) -> float:
    """Return the TRUE peak post engagement_total for this topic on this date.

    The global MAX over enriched_content, not the max of the random 10-row
    sample. The sample-max understates the topic peak roughly 91% of the
    time, so a brief that cites a sample-max "peak" is wrong by an order of
    magnitude. Bound parameters; no caller identifier interpolated.
    """
    market = _validate_market(market)
    sql = f"""
    SELECT MAX(CAST(engagement_total AS FLOAT64)) AS peak
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = @trend_date
      AND market = @market
      AND @topic_group IN UNNEST(topic_groups)
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("market", "STRING", market),
            bq.ScalarQueryParameter("topic_group", "STRING", topic_group),
        ]
    )
    for row in client.query(sql, job_config=job_config).result():
        return float(row.peak or 0.0)
    return 0.0


def _prefetch_brief_aggregates(
    client: bq.Client,
    dataset: str,
    trend_date: date,
    topics: list[tuple[str, str]],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[tuple[str, str], float]]:
    """Batch platform counts and peak engagement for all queued brief topics."""
    if not topics:
        return {}, {}
    markets = sorted({m for m, _ in topics if m})
    topic_groups = sorted({t for _, t in topics if t})
    if not markets or not topic_groups:
        return {}, {}

    counts_sql = f"""
    SELECT market, tg AS topic_group,
           IF(LENGTH(IFNULL(platform, '')) = 0, 'web', LOWER(platform)) AS platform,
           COUNT(*) AS n
    FROM `{client.project}.{dataset}.enriched_content`, UNNEST(topic_groups) AS tg
    WHERE DATE(collected_at) = @trend_date
      AND market IN UNNEST(@markets)
      AND tg IN UNNEST(@topic_groups)
    GROUP BY market, topic_group, platform
    ORDER BY n DESC, platform ASC
    """
    peaks_sql = f"""
    SELECT market, tg AS topic_group, MAX(CAST(engagement_total AS FLOAT64)) AS peak
    FROM `{client.project}.{dataset}.enriched_content`, UNNEST(topic_groups) AS tg
    WHERE DATE(collected_at) = @trend_date
      AND market IN UNNEST(@markets)
      AND tg IN UNNEST(@topic_groups)
    GROUP BY market, topic_group
    """
    params = [
        bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
        bq.ArrayQueryParameter("markets", "STRING", markets),
        bq.ArrayQueryParameter("topic_groups", "STRING", topic_groups),
    ]
    wanted = set(topics)
    counts_by_topic: dict[tuple[str, str], list[dict[str, Any]]] = {}
    peaks_by_topic: dict[tuple[str, str], float] = {}
    try:
        for row in client.query(
            counts_sql, job_config=bq.QueryJobConfig(query_parameters=params)
        ).result():
            key = (str(row.market or ""), str(row.topic_group or ""))
            if key not in wanted:
                continue
            counts_by_topic.setdefault(key, []).append(
                {"platform": row.platform, "count": int(row.n or 0)}
            )
        for row in client.query(
            peaks_sql, job_config=bq.QueryJobConfig(query_parameters=params)
        ).result():
            key = (str(row.market or ""), str(row.topic_group or ""))
            if key in wanted:
                peaks_by_topic[key] = float(row.peak or 0.0)
    except Exception as exc:
        logger.warning(
            "generate_briefs: aggregate prefetch failed, falling back per topic: %s", exc
        )
        return {}, {}
    return counts_by_topic, peaks_by_topic


def _build_key_metrics(
    peak_reach: float,
    item_count: int,
    source_diversity: int,
    creator_spread: int,
) -> list[str]:
    """Deterministic key_metrics from the engine row, not Gemini free-text.

    Guarantees the brief's headline numbers are real and correctly labelled.
    The composite engagement SUM is narrated as "engagement", never as
    "views" (the model relabels it inconsistently), and the peak is the true
    topic peak. Replaces the model's key_metrics so any card that renders
    them cannot drift from the data. Drops a zero metric so no "0 creators"
    line ever ships.
    """
    metrics: list[str] = []
    peak_label = _humanise_count(peak_reach)
    if peak_label:
        metrics.append(f"Peak post engagement {peak_label}")
    if item_count:
        if source_diversity:
            metrics.append(f"{int(item_count)} posts across {int(source_diversity)} sources")
        else:
            metrics.append(f"{int(item_count)} posts")
    if creator_spread:
        metrics.append(f"{int(creator_spread)} creators tracked")
    return metrics[:3]


# Cap on each named-entity / slang list surfaced in the signals digest. Five
# keeps the prompt block to a few hundred tokens while still naming the
# dominant people, orgs, places, and slang for the topic.
_SIGNALS_DIGEST_CAP: int = 5


def _parse_entity_names(aggregated: str, cap: int) -> list[str]:
    """Parse GDELT entity / slang aggregate text into a ranked name list.

    GDELT GKG entity columns arrive as 'Name,offset;Name,offset' and the
    slang_terms column as a comma-joined list. The pipeline aggregates the
    raw column values with STRING_AGG, so the input here is many such
    fragments joined by ';'. Split on ';' and ',', strip the trailing
    numeric offset GDELT appends, drop pure-number tokens, count by
    frequency, and return the top ``cap`` names. De-duplication is by the
    cleaned name so 'Bola Tinubu,12' and 'Bola Tinubu,90' collapse to one.
    """
    if not aggregated:
        return []
    counts: dict[str, int] = {}
    order: list[str] = []
    for raw_fragment in str(aggregated).split(";"):
        for piece in raw_fragment.split(","):
            name = piece.strip()
            # Drop the numeric offset tokens GDELT appends after each name,
            # and any empty token left by the split.
            if not name or name.isdigit():
                continue
            key = name.lower()
            if key not in counts:
                counts[key] = 0
                order.append(name)
            counts[key] += 1
    # Rank by frequency, stable on first-seen order for ties.
    ranked = sorted(order, key=lambda n: (-counts[n.lower()], order.index(n)))
    return ranked[:cap]


def _signals_digest_for_topic(
    client: bq.Client,
    dataset: str,
    market: str,
    topic_group: str,
    trend_date: date,
) -> dict[str, Any]:
    """Aggregate the derived signals the engine matched for this topic.

    Returns a dict with the top people and orgs (from the GDELT entity
    columns) and the distinct slang terms for the
    (market, topic_group, trend_date). For topics in _TOPIC_GEO_BLOCKLIST
    the named entities are withheld and only slang surfaces: the
    entity columns are exactly where a foreign name-collision leaks and the
    SQL aggregate cannot apply the row-level geo strip the sample path uses.
    v2locations is not surfaced at all; the GKG location column is a FIPS
    hash, not a clean name. Feeds build_brief_prompt so the model writes
    about the real local signal. Bound parameters only; no caller-supplied
    identifier is interpolated.
    """
    market = _validate_market(market)
    sql = f"""
    SELECT
      STRING_AGG(NULLIF(v2persons, ''), ';') AS persons,
      STRING_AGG(NULLIF(v2orgs, ''), ';') AS orgs,
      STRING_AGG(NULLIF(slang_terms, ''), ',') AS slang
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = @trend_date
      AND market = @market
      AND @topic_group IN UNNEST(topic_groups)
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("market", "STRING", market),
            bq.ScalarQueryParameter("topic_group", "STRING", topic_group),
        ]
    )
    empty: dict[str, Any] = {
        "persons": [],
        "orgs": [],
        "slang": [],
    }
    rows = list(client.query(sql, job_config=job_config).result())
    if not rows:
        return empty
    row = rows[0]
    # Geo-collision topics: withhold the named entities. The aggregate pulls
    # in the exact foreign names (Sapa -> Lao Cai, Fansipan) the sample path
    # strips row-by-row, and naming the foreign meaning is forbidden by the
    # SYSTEM_INSTRUCTION. Surface only the matched local slang (the real
    # disambiguator) for these topics.
    is_blocklist = topic_group in _TOPIC_GEO_BLOCKLIST
    return {
        "persons": []
        if is_blocklist
        else _parse_entity_names(row.persons or "", _SIGNALS_DIGEST_CAP),
        "orgs": [] if is_blocklist else _parse_entity_names(row.orgs or "", _SIGNALS_DIGEST_CAP),
        "slang": _parse_entity_names(row.slang or "", _SIGNALS_DIGEST_CAP),
    }


def _b24_sentiment_trajectory_for_market(
    client: bq.Client,
    dataset: str,
    market: str,
    trend_date: date,
    lookback_days: int = 7,
) -> str:
    """Derive a short sentiment-trajectory label from Brand24 Wave-2
    mention-sentiment aggregate rows for this market (Workstream E).

    Reads brand24_mention_sentiment rows (one per project per date) from
    enriched_content over the trailing lookback window. Each row carries
    views=total mentions, likes=positive, comments=negative (see
    _mention_sentiment_to_row in connectors/brand24.py). Compares the mean
    positive-share of the most recent half of the window against the older
    half and returns 'improving' / 'declining' / 'stable'. Returns '' when
    the surface is dark or has fewer than two days of data, so briefs render
    unchanged until the include_mentions_sentiment flag flips.
    """
    market = _validate_market(market)
    # The connector re-ingests the same multi-day window on every run with
    # fresh uuid4 ids, so DATE(collected_at) would bucket a whole lookback
    # window into one run-day and double-count each sample day across runs.
    # Bucket by DATE(published_at) (the sample day) instead, and dedupe per
    # query_term ({project_id}:{sample_date}) keeping the latest collected_at
    # row so each sample day contributes its freshest counts exactly once.
    sql = f"""
    WITH latest AS (
      SELECT
        DATE(published_at) AS d,
        likes AS positive,
        views AS total,
        ROW_NUMBER() OVER (
          PARTITION BY query_term ORDER BY collected_at DESC
        ) AS rn
      FROM `{client.project}.{dataset}.enriched_content`
      WHERE market = @market
        AND content_type = 'brand24_mention_sentiment'
        AND DATE(published_at)
            BETWEEN DATE_SUB(@trend_date, INTERVAL @lookback DAY) AND @trend_date
    )
    SELECT d,
           SUM(positive) AS positive,
           SUM(total) AS total
    FROM latest
    WHERE rn = 1
    GROUP BY d
    ORDER BY d
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("market", "STRING", market),
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("lookback", "INT64", int(lookback_days)),
        ]
    )
    shares: list[float] = []
    for row in client.query(sql, job_config=job_config).result():
        total = float(row.total or 0.0)
        if total <= 0:
            continue
        shares.append(float(row.positive or 0.0) / total)
    if len(shares) < 2:
        return ""
    mid = len(shares) // 2
    older = shares[:mid] or shares[:1]
    recent = shares[mid:]
    delta = (sum(recent) / len(recent)) - (sum(older) / len(older))
    if delta > 0.05:
        return f"improving (+{delta * 100:.0f}pp positive share)"
    if delta < -0.05:
        return f"declining ({delta * 100:.0f}pp positive share)"
    return "stable"


# Per-topic blocklist for creator handles whose username obviously
# references a foreign-collision geo. Mirrors _TOPIC_GEO_BLOCKLIST but
# applied at the creator-handle layer so contaminated handles never
# show up as "top creators to brief". Strings match against
# author_handle_norm (lowercased, leading @ stripped).
_TOPIC_CREATOR_BLOCKLIST: dict[str, list[str]] = {
    "economy_sapa_hustle": [
        "vietnam",
        "sapavietnam",
        "fansipan",
        "sailingsapa",
    ],
    "fashion_ankara_asoebi": [
        "turkey",
        "türkiye",
        "ankaraturkey",
        "ankaratürkiye",
        "istanbul",
        "ankaramekan",
    ],
}


def _creator_matches_geo_blocklist(handle: str, topic_group: str) -> bool:
    """Return True when a creator handle references a foreign-collision geo
    for this topic. Used to drop contaminated 'top creator' rows before
    they ship in the email card."""
    blockers = _TOPIC_CREATOR_BLOCKLIST.get(topic_group)
    if not blockers:
        return False
    h = (handle or "").lower().lstrip("@")
    return any(b in h for b in blockers)


def _top_creators_for_topic(
    client: bq.Client,
    dataset: str,
    market: str,
    topic_group: str,
    trend_date: date,
    n: int,
) -> list[dict[str, Any]]:
    """Pull top N creators on this topic ranked by mention count.

    Applies the geo-collision creator blocklist for topics whose slang
    collides with a foreign place / brand. Pulls 3x the requested limit
    when filtering applies so the post-filter list still has enough
    creators after contaminated handles are dropped.
    """
    market = _validate_market(market)
    fetch_n = int(n) * 3 if topic_group in _TOPIC_CREATOR_BLOCKLIST else int(n)
    sql = f"""
    SELECT author_handle_norm, ANY_VALUE(platform) AS platform,
           COUNT(*) AS mentions
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = @trend_date
      AND market = @market
      AND @topic_group IN UNNEST(topic_groups)
      AND LENGTH(IFNULL(author_handle_norm,'')) > 0
    GROUP BY author_handle_norm
    ORDER BY mentions DESC
    LIMIT @lim
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("market", "STRING", market),
            bq.ScalarQueryParameter("topic_group", "STRING", topic_group),
            bq.ScalarQueryParameter("lim", "INT64", fetch_n),
        ]
    )
    creators: list[dict[str, Any]] = []
    for row in client.query(sql, job_config=job_config).result():
        handle = row.author_handle_norm or ""
        if _creator_matches_geo_blocklist(handle, topic_group):
            continue
        creators.append(
            {
                "author_handle_norm": handle,
                "platform": row.platform or "",
                "mentions": int(row.mentions or 0),
            }
        )
        if len(creators) >= int(n):
            break
    return creators


def _format_platform_counts_for_brief(rows: list[dict[str, Any]]) -> list[str]:
    """Format per-platform counts into 'platform | N items' entries.

    Caps at 8 platforms so the email row stays readable; usually 3-5 is
    typical. Empty platform names are normalised upstream in the SQL
    query so we don't need to defend here.
    """
    out: list[str] = []
    for row in rows[:8]:
        platform = str(row.get("platform") or "").strip() or "web"
        count = int(row.get("count") or 0)
        if count <= 0:
            continue
        out.append(f"{platform} | {count} items")
    return out


_YT_CHANNEL_ID_RE = re.compile(r"^uc[a-z0-9_-]{22}$", re.IGNORECASE)


def _creator_persistable(handle: str) -> bool:
    """Drop handles the mailer cast list would skip (raw YT channel ids, numeric ids)."""
    bare = handle.lstrip("@").strip()
    if not bare or _YT_CHANNEL_ID_RE.match(bare):
        return False
    return not (bare.isdigit() and len(bare) >= 5)


def _format_creators_for_brief(creators: list[dict[str, Any]]) -> list[str]:
    """Format up to 5 top creators into pipe-delimited '@handle | platform | mentions'.

    Pipes flag the field boundaries cleanly so Looker / downstream renderers
    can split on them without escaping. Empty handles are dropped.
    """
    out: list[str] = []
    for creator in creators[:5]:
        handle = str(creator.get("author_handle_norm") or "").strip()
        if not handle or not _creator_persistable(handle):
            continue
        platform = str(creator.get("platform") or "").strip() or "web"
        try:
            mentions = int(creator.get("mentions") or 0)
        except (TypeError, ValueError):
            mentions = 0
        out.append(f"@{handle} | {platform} | {mentions} mentions")
    return out


def _format_social_refs_for_brief(
    sample: list[dict[str, Any]],
    topic_group: str = "",
) -> list[str]:
    """Format up to 5 social references (URL | platform | title) from sample rows.

    Picks rows that carry a non-empty URL, in the order returned by the
    sample query (which is already engagement-ranked + recency tie-broken).
    Each entry is pipe-delimited the same way as top_creators so renderers
    can split consistently. Title is truncated to 90 chars so the array
    cells stay readable in BQ console + the email card.

    Belt-and-braces geo filter: re-apply the topic blocklist here. In the
    pipeline path this is redundant (row_matches_geo_blocklist checks the same
    title+text+url that _sample_rows_for_topic already filtered), so it cannot
    catch anything the sample filter missed. It exists only to protect any
    non-pipeline caller that passes unfiltered sample rows directly.
    """
    out: list[str] = []
    seen_urls: set[str] = set()
    for row in sample:
        # Defensive re-filter: when topic is geo-bound, drop rows that
        # would pollute the email card's social_refs even if they
        # slipped past the upstream sample filter.
        if topic_group and _row_matches_geo_blocklist(row, topic_group):
            continue
        url = str(row.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        platform = str(row.get("platform") or "").strip() or "web"
        title = str(row.get("title") or row.get("text") or "").strip()
        title = title[:90]
        out.append(f"{url} | {platform} | {title}")
        if len(out) >= 5:
            break
    return out


# Deterministic brand-safety backstop. The prompt asks Gemini to populate
# risk_flags, but the model defaults to a conservative empty list even on
# topics that obviously warrant a caution (the 30 May briefs returned [] for
# Maandamano, Tinubu, Eskom). This maps inherently-sensitive signals to a
# strategist-facing note so the per-Kit risk line (the UEFA safeguard) fires
# even when the model stays silent or returns only a weak single note. The
# model's flags are always kept; the backstop runs as a union on top and adds
# any note the model did not already cover (dedup is substring-aware), so a
# thin model response no longer suppresses the deterministic caution.
_RISK_SIGNALS: tuple[tuple[str, str], ...] = (
    ("politics", "Political content; vet brand alignment and partisan exposure before activating."),
    (
        "crisis",
        "Sits near a crisis or social-tension story; check tone and timing before briefing.",
    ),
    (
        "protest",
        "Protest and civic-unrest context; confirm the brand should be in this conversation.",
    ),
    ("religion", "Religious or cultural sensitivity; review for respectful framing."),
)
# Text markers that imply a sensitivity even when the topic slug does not.
_RISK_TEXT_MARKERS: tuple[tuple[str, str], ...] = (
    (
        "maandamano",
        "Protest context (maandamano); confirm the brand should be in this conversation.",
    ),
    ("xenophob", "Xenophobia and anti-immigrant tension in the samples; high reputational risk."),
    ("afrophob", "Afrophobia tension in the samples; high reputational risk."),
    ("fraud", "Alleged fraud or scandal in the samples; vet before any brand tie-in."),
    ("scandal", "Scandal context in the samples; vet before any brand tie-in."),
    ("died", "Death or tragedy referenced; check tone and timing before activating."),
    ("death", "Death or tragedy referenced; check tone and timing before activating."),
)
# Short markers that occur as substrings of innocuous words (died -> studied,
# embodied, parodied) must match on a word boundary, not a bare substring.
# The longer markers above cannot collide so they keep plain substring matching.
_RISK_SHORT_MARKERS = frozenset({"died", "death", "fraud"})


# Topic-slug prefixes where a strongly-negative tone turns the trend into a
# brand-safety question on its own, regardless of any keyword match.
_TONE_SENSITIVE_PREFIXES: tuple[str, ...] = ("politics", "protest", "crisis")
# GDELT tone runs roughly -10..+10. A topic at or below this on a sensitive
# prefix reads as a negative civic or political story, not a neutral one.
_NEGATIVE_TONE_FLOOR: float = -2.0
_TONE_NOTE = "Negative tone on a sensitive topic; vet before activating."


def _derive_risk_flags(
    model_flags: list[str],
    topic_group: str,
    brief_text: str,
    tone_avg: float | None = None,
) -> list[str]:
    """Brand-safety notes for a brief, model findings unioned with a backstop.

    The model's own flags are always kept. The deterministic backstop always
    runs on top and adds any note the model did not already cover, so a weak
    single-note model response no longer suppresses the backstop. Dedup is
    case-insensitive and substring-aware: a backstop note is skipped when an
    existing flag already says the same thing. The backstop never removes a
    model flag, it only fills gaps the model left.
    """
    flags = [f.strip() for f in model_flags if f and f.strip()]

    def _already_covered(note: str) -> bool:
        note_l = note.lower()
        for existing in flags:
            existing_l = existing.lower()
            if note_l in existing_l or existing_l in note_l:
                return True
        return False

    def _add(note: str) -> None:
        if not _already_covered(note):
            flags.append(note)

    hay = f"{topic_group} {brief_text}".lower()
    category = topic_group.split("_")[0] if topic_group else ""

    # Tone-weighted check: a strongly-negative tone on a sensitive-prefix topic
    # is a caution on its own, even when no keyword marker fired.
    if tone_avg is not None and float(tone_avg) <= _NEGATIVE_TONE_FLOOR:
        for prefix in _TONE_SENSITIVE_PREFIXES:
            if category == prefix or prefix in topic_group.lower():
                _add(_TONE_NOTE)
                break

    # Known-sensitive markers in the slug or brief text.
    for marker, note in _RISK_TEXT_MARKERS:
        if marker in _RISK_SHORT_MARKERS:
            if re.search(rf"\b{re.escape(marker)}\b", hay):
                _add(note)
        elif marker in hay:
            _add(note)

    # Inherently-sensitive topic categories.
    for signal, note in _RISK_SIGNALS:
        if signal == category or signal in topic_group.lower():
            _add(note)

    return flags


def _clamp_status_tag(trend_score: float) -> str:
    """Derive the Key/Rising pill from the real trend_score, not the model's
    self-reported status_tag.

    The brief generator previously trusted Gemini's ``status_tag`` verbatim, so
    a model mislabel could ship the wrong tier to stakeholders even though the
    code holds the score. The pill is 2-state and anchored to the scoring
    Trending floor (configs/scoring.yaml ``thresholds.trending``, default 0.45).
    """
    try:
        threshold = float(load_scoring().get("thresholds", {}).get("trending", 0.45))
    except Exception:
        threshold = 0.45
    return "Key" if float(trend_score) >= threshold else "Rising"


def _to_topic_brief(
    market: str,
    topic_group: str,
    trend_score: float,
    response: BriefResponse,
    top_creators: list[str] | None = None,
    social_refs: list[str] | None = None,
    platform_counts: list[str] | None = None,
    key_metrics_override: list[str] | None = None,
    tone_avg: float | None = None,
) -> TopicBrief:
    parsed = response.parsed
    return TopicBrief(
        market=market,
        topic_group=topic_group,
        trend_score=float(trend_score),
        description_rationale=str(parsed.get("description_rationale") or ""),
        activation_idea=str(parsed.get("activation_idea") or ""),
        visual_anchor=str(parsed.get("visual_anchor") or ""),
        nano_banana_prompt=str(parsed.get("nano_banana_prompt") or ""),
        lyria_prompt=str(parsed.get("lyria_prompt") or ""),
        key_metrics=(
            list(key_metrics_override)
            if key_metrics_override is not None
            else [str(x) for x in (parsed.get("key_metrics") or [])][:3]
        ),
        platforms=[str(x) for x in (parsed.get("platforms") or [])][:4],
        sentiment_summary=str(parsed.get("sentiment_summary") or ""),
        status_tag=_clamp_status_tag(trend_score),
        top_creators=list(top_creators or []),
        social_refs=list(social_refs or []),
        platform_counts=list(platform_counts or []),
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        model=response.model,
        headline=str(parsed.get("headline") or ""),
        risk_flags=_derive_risk_flags(
            [str(x) for x in (parsed.get("risk_flags") or [])],
            topic_group,
            " ".join(
                str(parsed.get(k) or "")
                for k in ("description_rationale", "activation_idea", "sentiment_summary")
            ),
            tone_avg=tone_avg,
        ),
    )


def _existing_brief_keys(
    client: bq.Client,
    dataset: str,
    trend_date: date,
) -> set[tuple[str, str]]:
    """Return set of (market, topic_group) already briefed for this date.

    Used to short-circuit the per-topic Gemini call when a brief already
    exists, so cron retries and ad-hoc reruns don't double-spend on
    Vertex. The natural key is (trend_date, market, query_group); any
    row with that key is treated as "already briefed" regardless of
    cycle_id, status, or content.
    """
    sql = f"""
    SELECT DISTINCT market, query_group AS topic_group
    FROM `{client.project}.{dataset}.trend_analysis`
    WHERE trend_date = @trend_date
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    return {
        (str(r.market), str(r.topic_group))
        for r in client.query(sql, job_config=job_config).result()
    }


def persist_render_payloads(
    trend_date: date,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
) -> int:
    """Write the full render bundle onto the stored briefs for one date.

    Runs in the pipeline after the conversation producers tag the in-memory
    briefs. Updates ``trend_analysis.render_payload`` with ``{display,
    comment_sentiment, comment_opening, comment_themes, driving_hashtags}`` per (market,
    topic), so a send that re-reads briefs from BigQuery (the resend
    workflow, a preview, a recovery day) renders exactly what the live email
    rendered. Returns the number of rows updated. Parameterized DML, one
    UPDATE per brief (the panel is ~24 rows). The caller treats any raised
    error as non-fatal.
    """
    from google.cloud import bigquery as _bq

    from src.utils.bigquery import get_client, get_dataset

    client = get_client()
    dataset = get_dataset()
    table = f"`{client.project}.{dataset}.trend_analysis`"
    if not briefs_by_topic:
        return 0

    merge_selects: list[str] = []
    query_parameters: list = [
        _bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
    ]
    for idx, ((market, topic_group), brief) in enumerate(briefs_by_topic.items()):
        payload = json.dumps(
            {
                "display": brief.get("display") or {},
                "comment_sentiment": str(brief.get("comment_sentiment") or ""),
                "comment_opening": str(brief.get("comment_opening") or ""),
                "comment_themes": list(brief.get("comment_themes") or []),
                "driving_hashtags": list(brief.get("driving_hashtags") or []),
                "forecast_outlook": str(brief.get("forecast_outlook") or ""),
                "continuity_state": str(brief.get("continuity_state") or ""),
                "continuity_day": brief.get("continuity_day"),
                "lifecycle_phase": str(brief.get("lifecycle_phase") or ""),
                "sentiment_lexicon_scores": list(brief.get("sentiment_lexicon_scores") or [])[:500],
                "seed_score": brief.get("seed_score"),
                "seed_path": brief.get("seed_path") or {},
            },
            default=str,
        )
        merge_selects.append(
            f"SELECT @market_{idx} AS market, @query_group_{idx} AS query_group, "
            f"@payload_{idx} AS payload"
        )
        query_parameters.extend(
            [
                _bq.ScalarQueryParameter(f"market_{idx}", "STRING", market),
                _bq.ScalarQueryParameter(f"query_group_{idx}", "STRING", topic_group),
                _bq.ScalarQueryParameter(f"payload_{idx}", "STRING", payload),
            ]
        )

    # The latest-row lookup is resolved inside USING, not in the ON clause.
    # BigQuery rejects a MERGE whose join predicate contains a subquery over a
    # table ("Unsupported subquery with table in join predicate"), so the
    # earlier form that correlated a SELECT MAX(analyzed_at) against S.market
    # raised on every run and this whole stage was a silent no-op: the enrichment
    # never reached trend_analysis and render_payload kept only its display key.
    # Joining the per-topic max in as a source column keeps the semantics
    # identical (update only the newest row for the date) with no subquery in ON.
    sql = (
        f"MERGE {table} T "
        "USING ("
        "SELECT s.market, s.query_group, s.payload, latest.analyzed_at "
        "FROM (" + " UNION ALL ".join(merge_selects) + ") s "
        "JOIN ("
        f"SELECT market, query_group, MAX(analyzed_at) AS analyzed_at FROM {table} "
        "WHERE trend_date = @trend_date GROUP BY market, query_group"
        ") latest "
        "ON latest.market = s.market AND latest.query_group = s.query_group"
        ") S "
        "ON T.trend_date = @trend_date "
        "AND T.market = S.market "
        "AND T.query_group = S.query_group "
        "AND T.analyzed_at = S.analyzed_at "
        "WHEN MATCHED THEN UPDATE SET render_payload = S.payload"
    )
    job_config = _bq.QueryJobConfig(query_parameters=query_parameters)
    job = client.query(sql, job_config=job_config)
    job.result()
    # Report what BigQuery actually changed, not the brief count. A topic with no
    # stored row for the date matches nothing, and a dry run affects nothing, so
    # returning len() would let the caller's "persisted N/M" line overstate.
    return int(job.num_dml_affected_rows or 0)


def _prior_day_scores(
    client: bq.Client,
    dataset: str,
    trend_date: date,
) -> dict[tuple[str, str], float]:
    """Map (market, query_group) -> trend_score from the prior cron day.

    Reads ``trend_analysis`` for ``trend_date - 1 day`` so the PULSE v2
    display layer can compute a day-over-day delta (the state badge:
    accelerating / holding / cooling). Returns ``{}`` on any failure or
    when yesterday has no rows, which the display layer reads as "no
    prior" and renders as "New on the board". Bound parameters only.
    """
    prior_date = trend_date - timedelta(days=1)
    sql = f"""
    SELECT market, query_group, trend_score
    FROM `{client.project}.{dataset}.trend_analysis`
    WHERE trend_date = @prior_date
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("prior_date", "DATE", prior_date)]
    )
    out: dict[tuple[str, str], float] = {}
    for r in client.query(sql, job_config=job_config).result():
        try:
            out[(str(r.market), str(r.query_group))] = float(r.trend_score)
        except (TypeError, ValueError):
            continue
    return out


def _build_display(
    *,
    market: str,
    topic_group: str,
    trend_score: float,
    velocity: float,
    prior_score: float | None,
    platform_count_rows: list[dict[str, Any]],
    sample_rows: list[dict[str, Any]],
    search_velocity: float | None,
) -> dict[str, Any]:
    """Compose the PULSE v2 display bundle for one brief.

    Pure mapping onto src/analysis/display_layer.py off data the engine
    already has. Channel weights derive from the per-platform counts
    (``platform_count_rows`` is the [{"platform", "count"}] list from
    ``_platform_counts_for_topic``). ``in_market_pct`` reuses the sample
    rows already pulled for the brief. The keys match the dict the v2
    renderers read (state / phase / window / in_market_pct / channels /
    confidence / search).
    """
    platform_counts = {
        str(r.get("platform") or "web"): int(r.get("count") or 0) for r in platform_count_rows
    }
    phase = trend_phase(trend_score, velocity)
    return {
        "state": trend_state(trend_score, prior_score),
        "phase": phase,
        "window": act_window(phase),
        "in_market_pct": in_market_pct(sample_rows, market, topic_group),
        "channels": channel_weights(platform_counts),
        "confidence": confidence_label(len(platform_counts)),
        "search": search_read(search_velocity),
    }


# This stage's name in the shared gemini_usage ledger, the single basis the
# cost watchdog reads. trend_analysis carries prompt_tokens on its own rows
# too, but those are the brief's record of what it cost, not the cost report.
USAGE_CONSUMER = "trend_analysis"


def generate_briefs(
    *,
    trend_date: date,
    gemini_client: GeminiClient | None = None,
    bq_client: bq.Client | None = None,
    dataset: str | None = None,
    top_n_per_market: int = DEFAULT_TOP_N_PER_MARKET,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    top_creators: int = DEFAULT_TOP_CREATORS,
    persist: bool = True,
    force: bool = False,
    usage_sink: UsageSink | None = None,
    row_sink: Callable[[list[dict[str, Any]]], int] | None = None,
) -> GenerateBriefsReport:
    """Orchestrate brief generation for the day's top topics per market.

    Pulls scoring stats + sample rows + creators, generates one Gemini
    brief per (market, topic_group), and persists each successful brief
    to ``trend_analysis``. Failures don't stop the run; they're collected
    in the returned report.

    Idempotent by default: if ``trend_analysis`` already has rows for
    ``trend_date`` and a given (market, topic_group), the Vertex call is
    skipped. Pass ``force=True`` to re-generate (e.g. after a prompt
    change). Skipped topics are tracked in ``report.skipped_existing``.
    """
    bq_client = bq_client or get_client()
    dataset = dataset or get_dataset()
    gemini_client = gemini_client or GeminiClient()

    topics_df = _query_top_topics(bq_client, dataset, trend_date, top_n_per_market)
    logger.info(
        "generate_briefs: %d topics queued for %s (top %d per market)",
        len(topics_df),
        trend_date,
        top_n_per_market,
    )

    if force:
        existing: set[tuple[str, str]] = set()
        if persist and row_sink is None:
            delete_sql = f"""
            DELETE FROM `{bq_client.project}.{dataset}.trend_analysis`
            WHERE trend_date = @trend_date
            """
            bq_client.query(
                delete_sql,
                job_config=bq.QueryJobConfig(
                    query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
                ),
            ).result()
            logger.info(
                "generate_briefs: force=True deleted existing trend_analysis rows for %s",
                trend_date,
            )
    else:
        existing = _existing_brief_keys(bq_client, dataset, trend_date)
        if existing:
            logger.info(
                "generate_briefs: %d (market, topic_group) keys already briefed for %s, will skip those",
                len(existing),
                trend_date,
            )

    report = GenerateBriefsReport()
    # Per-(market, model) token tally for the gemini_usage ledger, flushed once
    # at the end of the pass via ``usage_sink`` (default persist_gemini_usage).
    usage_tally: dict = {}
    sink = usage_sink or persist_gemini_usage
    rows_to_persist: list[dict[str, Any]] = []
    # persist_succeeded ends True only if every chunk flush succeeded.
    all_ok = True

    def _flush(rows: list[dict[str, Any]]) -> bool:
        """Persist one chunk of brief rows to trend_analysis. Returns whether
        the insert succeeded. On failure the report carries the error string
        and the run continues so earlier chunks already in BQ are not lost."""
        nonlocal all_ok
        if not (persist and rows):
            return True
        report.persist_attempted = True
        report.persist_row_count += len(rows)
        try:
            if row_sink is None:
                insert_dataframe(pd.DataFrame(rows), "trend_analysis")
            else:
                written = row_sink(rows)
                if type(written) is not int or written != len(rows):
                    raise ValueError("trend_analysis row sink incomplete")
            return True
        except Exception as exc:
            all_ok = False
            report.persist_error = str(exc)
            logger.error(
                "generate_briefs: persistence failed (%d rows would have "
                "been lost from trend_analysis); see report.persist_error "
                "for the orchestrator to surface this in the digest header: %s",
                len(rows),
                exc,
            )
            return False

    # Workstream E: per-market Brand24 sentiment trajectory, computed once
    # per market per run (3 queries max) and stamped onto every brief.
    b24_trajectory_by_market: dict[str, str] = {}
    # PULSE v2 display layer: yesterday's per-topic trend_score, read once
    # per run so each brief's display can show a day-over-day delta. A
    # lookup failure leaves the map empty; the display layer then renders
    # "New on the board" for every topic, which is safe.
    try:
        prior_scores = _prior_day_scores(bq_client, dataset, trend_date)
    except Exception as prior_exc:
        logger.warning("generate_briefs: prior-day score lookup failed: %s", prior_exc)
        prior_scores = {}

    queued_topics: list[tuple[str, str]] = []
    for row in topics_df.to_dict(orient="records"):
        market = str(row.get("market") or "")
        topic_group = str(row.get("topic_group") or "")
        if not market or not topic_group:
            continue
        if (market, topic_group) in existing:
            continue
        queued_topics.append((market, topic_group))
    prefetch_counts, prefetch_peaks = _prefetch_brief_aggregates(
        bq_client, dataset, trend_date, queued_topics
    )

    # Money left the account whether or not a brief shipped, so the ledger
    # flush is in a finally: a mid-pass failure still records what was spent.
    try:
        for row in topics_df.to_dict(orient="records"):
            market = str(row.get("market") or "")
            topic_group = str(row.get("topic_group") or "")
            if not market or not topic_group:
                continue
            if (market, topic_group) in existing:
                logger.info(
                    "generate_briefs: skipping %s/%s, brief already exists for %s",
                    market,
                    topic_group,
                    trend_date,
                )
                report.skipped_existing.append((market, topic_group))
                continue
            try:
                sample = _sample_rows_for_topic(
                    bq_client, dataset, market, topic_group, trend_date, sample_rows
                )
                # Sample-thinness gate. Skip the Vertex call entirely when the
                # surviving sample (after geo filter) is too thin to support a
                # useful brief. Catches the noise-floor failure pattern observed
                # 14-25 May 2026: empty Vertex responses on topics with <5
                # surviving rows OR <2000 total title+text chars. Path A
                # (raising the score floor to 0.30) tried to fix this on score
                # alone and over-corrected; this gate fixes the actual failure
                # shape. See _sample_is_too_thin for thresholds and rationale.
                is_thin, thin_rows, thin_chars = _sample_is_too_thin(sample, topic_group)
                if is_thin:
                    logger.warning(
                        "generate_briefs: skipping %s/%s due to thin sample "
                        "(surviving rows=%d threshold=%d, total title+text chars=%d "
                        "threshold=%d). Topic stays out of the brief block this "
                        "run; will appear in the movers section if velocity warrants.",
                        market,
                        topic_group,
                        thin_rows,
                        _MIN_SAMPLE_ROWS,
                        thin_chars,
                        _MIN_SAMPLE_TEXT_CHARS,
                    )
                    report.skipped_thin_sample.append((market, topic_group, thin_rows, thin_chars))
                    continue
                creators = _top_creators_for_topic(
                    bq_client, dataset, market, topic_group, trend_date, top_creators
                )
                # Pre-compute the data-only fields once per topic. They are
                # the same regardless of how many Gemini calls we make below
                # (first-pass + optional retry both attach the same creators
                # and refs).
                creators_for_brief = _format_creators_for_brief(creators)
                social_refs_for_brief = _format_social_refs_for_brief(
                    sample, topic_group=topic_group
                )
                topic_key = (market, topic_group)
                platform_counts_rows = prefetch_counts.get(topic_key)
                if platform_counts_rows is None:
                    platform_counts_rows = _platform_counts_for_topic(
                        bq_client, dataset, market, topic_group, trend_date
                    )
                platform_counts_for_brief = _format_platform_counts_for_brief(platform_counts_rows)
                # Topic-level peak reach: the TRUE global MAX over enriched_content,
                # not the random-sample max (which understates the real peak ~91%
                # of the time). Anchors the prompt and the deterministic metrics.
                # Non-fatal: a lookup failure falls back to the sample max.
                if topic_key in prefetch_peaks:
                    peak_reach = prefetch_peaks[topic_key]
                else:
                    try:
                        peak_reach = _peak_engagement_for_topic(
                            bq_client, dataset, market, topic_group, trend_date
                        )
                    except Exception as peak_exc:
                        logger.warning(
                            "generate_briefs: peak lookup failed for %s/%s: %s; falling back to sample max",
                            market,
                            topic_group,
                            peak_exc,
                        )
                        peak_reach = max(
                            (float(r.get("engagement_total") or 0.0) for r in sample),
                            default=0.0,
                        )
                # Deterministic key_metrics from the engine row override the model's
                # free-text so the headline numbers cannot drift from the data (the
                # audit found the model authoring divergent reach figures and
                # relabelling the engagement SUM as "views").
                deterministic_metrics = _build_key_metrics(
                    peak_reach,
                    int(row.get("item_count") or 0),
                    int(row.get("source_diversity") or 0),
                    int(row.get("creator_spread") or 0),
                )
                # Derived-signal digest (slang and named entities).
                # Non-fatal: a lookup failure leaves the digest empty and the
                # brief still ships, exactly as before this change.
                try:
                    signals_digest = _signals_digest_for_topic(
                        bq_client, dataset, market, topic_group, trend_date
                    )
                except Exception as digest_exc:
                    logger.warning(
                        "generate_briefs: signals digest lookup failed for %s/%s: %s",
                        market,
                        topic_group,
                        digest_exc,
                    )
                    signals_digest = None
                seed_path_block = ""
                seed_path_data: dict[str, Any] = {}
                if os.environ.get("SEED_PATH_RENDER_ENABLED", "false").lower() == "true":
                    try:
                        from src.analysis.seed_path import (
                            build_seed_path,
                            format_seed_path_prompt_block,
                        )

                        seed_path_data = build_seed_path(
                            market, topic_group, trend_date, client=bq_client, dataset=dataset
                        )
                        seed_path_block = format_seed_path_prompt_block(seed_path_data)
                    except Exception as sp_exc:
                        logger.warning(
                            "generate_briefs: seed_path lookup failed for %s/%s: %s",
                            market,
                            topic_group,
                            sp_exc,
                        )
                prompt = build_brief_prompt(
                    market=market,
                    topic_group=topic_group,
                    trend_score=float(row.get("trend_score") or 0.0),
                    velocity_score=float(row.get("velocity_score") or 0.0),
                    item_count=int(row.get("item_count") or 0),
                    source_diversity=int(row.get("source_diversity") or 0),
                    creator_spread=int(row.get("creator_spread") or 0),
                    tone_avg=row.get("tone_score"),
                    # Pass the GDELT tone-row count so the prompt can tell a
                    # no-signal topic (tone_rows == 0, where run_rss_now writes
                    # tone_score 0.0) apart from a genuinely-negative one. None
                    # for any legacy row whose tone_rows is NULL; the prompt then
                    # falls back to its own no-signal heuristic.
                    tone_rows=(int(row["tone_rows"]) if pd.notna(row.get("tone_rows")) else None),
                    sample_rows=sample,
                    top_creators=creators,
                    peak_reach=peak_reach,
                    signals_digest=signals_digest,
                    # Wave 1 richer briefs: the per-platform rollup already pulled
                    # for the email card. build_brief_prompt only folds it into the
                    # prompt when RICHER_BRIEFS_ENABLED is on, so passing it here is
                    # byte-identical to before while the flag is dark.
                    platform_counts=platform_counts_rows,
                    seed_path_block=seed_path_block,
                )
                # Transient-retry wrapper. Vertex 5xx, network blip, schema
                # validator hiccup. Without this, a single transient kills the
                # topic for the day. Observed twice in week 2 of the cron
                # observation window: 18 May za/politics_crises (0.205) and
                # 21 May ng/economy_sapa_hustle (0.333), both Monitoring-tier
                # topics just above the threshold floor. Empty-response retry
                # below this block continues to handle the schema-collapse
                # case separately.
                response = None
                for transient_attempt in range(2):
                    try:
                        response = gemini_client.generate_brief(
                            prompt=prompt,
                            response_schema=RESPONSE_SCHEMA,
                            system_instruction=SYSTEM_INSTRUCTION,
                        )
                        break
                    except Exception as transient_exc:
                        if transient_attempt == 0:
                            logger.warning(
                                "generate_briefs: transient Vertex failure for %s/%s "
                                "on attempt 1: %s; retrying in 5s",
                                market,
                                topic_group,
                                transient_exc,
                            )
                            time.sleep(5)
                            continue
                        raise
                record_usage(usage_tally, market, response)
                brief = _to_topic_brief(
                    market=market,
                    topic_group=topic_group,
                    trend_score=float(row.get("trend_score") or 0.0),
                    response=response,
                    top_creators=creators_for_brief,
                    social_refs=social_refs_for_brief,
                    platform_counts=platform_counts_for_brief,
                    key_metrics_override=deterministic_metrics,
                    tone_avg=(
                        float(row["tone_score"]) if pd.notna(row.get("tone_score")) else None
                    ),
                )
                # First-pass token counts always count toward spend even if
                # the response was unusable; the API was billed regardless.
                report.total_prompt_tokens += brief.prompt_tokens
                report.total_completion_tokens += brief.completion_tokens

                if _is_empty_brief(brief):
                    if not _should_retry_empty(response):
                        logger.warning(
                            "generate_briefs: empty response for %s/%s on first "
                            "attempt and raw_text is not JSON-shaped (likely a "
                            "safety-filter refusal); skipping retry to avoid wasted "
                            "spend",
                            market,
                            topic_group,
                        )
                        report.skipped_empty.append((market, topic_group))
                        continue
                    logger.warning(
                        "generate_briefs: empty response for %s/%s on first attempt "
                        "(prompt_tokens=%d completion_tokens=%d), retrying with "
                        "stricter prompt and higher temperature",
                        market,
                        topic_group,
                        brief.prompt_tokens,
                        brief.completion_tokens,
                    )
                    retry_response = gemini_client.generate_brief(
                        prompt=prompt + _RETRY_PROMPT_ADDENDUM,
                        response_schema=RESPONSE_SCHEMA,
                        temperature=0.6,
                        system_instruction=SYSTEM_INSTRUCTION,
                    )
                    # The first call billed too; both land in the ledger.
                    record_usage(usage_tally, market, retry_response)
                    retry_brief = _to_topic_brief(
                        market=market,
                        topic_group=topic_group,
                        trend_score=float(row.get("trend_score") or 0.0),
                        response=retry_response,
                        top_creators=creators_for_brief,
                        social_refs=social_refs_for_brief,
                        platform_counts=platform_counts_for_brief,
                        key_metrics_override=deterministic_metrics,
                        tone_avg=(
                            float(row["tone_score"]) if pd.notna(row.get("tone_score")) else None
                        ),
                    )
                    report.total_prompt_tokens += retry_brief.prompt_tokens
                    report.total_completion_tokens += retry_brief.completion_tokens

                    if _is_empty_brief(retry_brief):
                        logger.warning(
                            "generate_briefs: brief for %s/%s still empty after "
                            "retry; skipping persist",
                            market,
                            topic_group,
                        )
                        report.skipped_empty.append((market, topic_group))
                        continue
                    # Sum first-call tokens onto the retry_brief so the per-row
                    # spend ledger persisted to trend_analysis matches what the
                    # API was billed for. Without this, the row stores only the
                    # retry's tokens and undercounts. Mirrors the daily_summary
                    # retry path.
                    retry_brief.prompt_tokens += brief.prompt_tokens
                    retry_brief.completion_tokens += brief.completion_tokens
                    brief = retry_brief

                # Workstream E: stamp the per-market Brand24 sentiment trajectory
                # (computed once per market, cached). Non-fatal: a lookup failure
                # leaves the label empty and the brief still ships.
                if market not in b24_trajectory_by_market:
                    try:
                        b24_trajectory_by_market[market] = _b24_sentiment_trajectory_for_market(
                            bq_client, dataset, market, trend_date
                        )
                    except Exception as traj_exc:
                        logger.warning(
                            "generate_briefs: b24 sentiment trajectory lookup failed for %s: %s",
                            market,
                            traj_exc,
                        )
                        b24_trajectory_by_market[market] = ""
                brief.b24_sentiment_trajectory = b24_trajectory_by_market[market]

                # PULSE v2 display bundle. Computed off data already pulled for
                # this topic: the trend_score + velocity from the scoring row,
                # yesterday's score for the delta, the per-platform counts for
                # channel weights, and the sample rows for in-market share.
                # search_velocity is the row's dedicated search_velocity_score
                # (the BigQuery Trends search-interest signal, distinct from the
                # composite velocity_score), so the SEARCH read reflects real
                # search intent and shows only when there is genuine lift.
                brief.display = _build_display(
                    market=market,
                    topic_group=topic_group,
                    trend_score=float(row.get("trend_score") or 0.0),
                    velocity=float(row.get("velocity_score") or 0.0),
                    prior_score=prior_scores.get((market, topic_group)),
                    platform_count_rows=platform_counts_rows,
                    sample_rows=sample,
                    search_velocity=(
                        float(row["search_velocity_score"])
                        if row.get("search_velocity_score") is not None
                        else None
                    ),
                )
                # Only surface the seed-path card when the trail is measured. Thin
                # trails are where a topic without a distinctive term of its own
                # bleeds to a neighbouring cultural term, so gating on confidence
                # keeps every rendered card on-topic.
                if seed_path_data and seed_path_data.get("confidence") == "measured":
                    brief.seed_path = seed_path_data

                report.briefs[(market, topic_group)] = brief
                rows_to_persist.append(brief.to_bq_row(trend_date))
                if persist and len(rows_to_persist) >= PERSIST_CHUNK_SIZE:
                    _flush(rows_to_persist)
                    rows_to_persist = []
            except Exception as exc:
                logger.warning(
                    "generate_briefs: failed for %s/%s: %s",
                    market,
                    topic_group,
                    exc,
                )
                report.failures.append((market, topic_group, str(exc)))

        # Flush any remaining rows that did not fill a final chunk.
        if persist and rows_to_persist:
            _flush(rows_to_persist)
            rows_to_persist = []
        report.persist_succeeded = report.persist_attempted and all_ok

        logger.info(
            "generate_briefs: complete. successful=%d failed=%d total_tokens=%d "
            "estimated_cost_usd=%.5f",
            len(report.briefs),
            len(report.failures),
            report.total_prompt_tokens + report.total_completion_tokens,
            report.estimated_cost_usd,
        )
    finally:
        # persist=False is an explicit "write nothing to BigQuery" contract, and
        # the ledger is a BigQuery table. The Gemini calls still billed, and the
        # watchdog's billing reconciliation is what surfaces that spend; a write
        # that breaks the caller's flag is not.
        if persist:
            sink(usage_rows(trend_date, USAGE_CONSUMER, usage_tally))
    return report


__all__ = [
    "DEFAULT_SAMPLE_ROWS",
    "DEFAULT_TOP_CREATORS",
    "DEFAULT_TOP_N_PER_MARKET",
    "PERSIST_CHUNK_SIZE",
    "GenerateBriefsReport",
    "TopicBrief",
    "_is_empty_brief",
    "generate_briefs",
]
