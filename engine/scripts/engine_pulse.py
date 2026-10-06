"""Engine performance watchdog.

Reads `configs/engine_capacity.yaml` (declared capacity per connector per
market) and `ogilvy-trends-v2.trends_v2_dev.pipeline_runs` + per-source
row counts in BigQuery, computes utilisation %, and emits a 4-block
report naming the highest-priority next move.

Read-only. No BQ writes, no workflow triggers, no Vertex calls.

CLI:
    python scripts/engine_pulse.py [--date YYYY-MM-DD] [--json]

When --date is omitted, defaults to today UTC. When --json is set, the
report is emitted as a single JSON object on stdout for downstream
parsing (morning-check skill consumes this shape).

Verdict bucket model:
    CLEAN     - every connector >= warn threshold AND no critical conditions
    MINOR     - 1 or 2 connectors between warn and critical
    DEGRADED  - 3+ connectors below warn OR any below critical
    FAIL      - any connector at zero unexpectedly OR the
                CONNECTOR-ZERO check fires (an enabled, normally-high-volume
                connector returned exactly 0 on a success run = bad-key signature)

The auto-suggest engine matches the current state against `auto_suggest`
rules in engine_capacity.yaml and surfaces the highest-priority matched
action in the RECOMMENDED ACTION block.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence.source_inventory import flag_reads_on

CAPACITY_FILE = ROOT / "configs" / "engine_capacity.yaml"
SOURCES_FILE = ROOT / "configs" / "sources.yaml"


# ----------------------------------------------------------------------
# Data classes


@dataclass
class ConnectorPulse:
    name: str
    expected_per_day: int
    actual_per_day: int
    per_market_expected: dict[str, int]
    per_market_actual: dict[str, int]
    warn_pct: int
    critical_pct: int
    utilisation_pct: float
    bucket: str  # CLEAN / WARN / CRITICAL / FAIL
    notes: str = ""

    @property
    def per_market_pct(self) -> dict[str, float]:
        return {
            m: (self.per_market_actual.get(m, 0) / e * 100.0) if e else 0.0
            for m, e in self.per_market_expected.items()
        }


@dataclass
class EnginePulse:
    trend_date: str
    connectors: list[ConnectorPulse] = field(default_factory=list)
    briefs_expected: int = 24
    briefs_actual: int = 0
    daily_summary_tokens: int = 0
    verdict: str = "CLEAN"
    recommended_action: str = "no action"
    ensembledata_quota: dict[str, Any] | None = None
    connector_zero_alerts: list[str] = field(default_factory=list)
    cross_market_zero_alerts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trend_date": self.trend_date,
            "verdict": self.verdict,
            "recommended_action": self.recommended_action,
            "briefs_expected": self.briefs_expected,
            "briefs_actual": self.briefs_actual,
            "daily_summary_tokens": self.daily_summary_tokens,
            "ensembledata_quota": self.ensembledata_quota,
            "connector_zero_alerts": list(self.connector_zero_alerts),
            "cross_market_zero_alerts": list(self.cross_market_zero_alerts),
            "connectors": [
                {
                    "name": c.name,
                    "expected": c.expected_per_day,
                    "actual": c.actual_per_day,
                    "utilisation_pct": round(c.utilisation_pct, 1),
                    "bucket": c.bucket,
                    "per_market_actual": c.per_market_actual,
                    "per_market_expected": c.per_market_expected,
                }
                for c in self.connectors
            ],
        }


# ----------------------------------------------------------------------
# Config + BQ helpers


def _load_capacity() -> dict[str, Any]:
    with CAPACITY_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_sources() -> dict[str, Any]:
    with SOURCES_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _bq_row_counts(
    trend_date: str,
) -> tuple[
    dict[str, dict[str, int]],
    dict[str, int],
    int,
]:
    """Pull per-connector + per-market row counts from pipeline_runs.

    Returns (per_source_per_market, briefs_per_market, daily_summary_tokens).
    """
    from google.cloud import bigquery as bq

    client = bq.Client(project="ogilvy-trends-v2")

    # Take the LATEST run per market for the date. A killed run plus a same-day
    # rerun both land in pipeline_runs; SUM-ing across all runs double-counts
    # ingestion. ROW_NUMBER over (market, started_at DESC) keeps only the most
    # recent run per market, which is the one whose row counts are authoritative.
    sql = """
    WITH latest AS (
        SELECT
            market,
            IFNULL(rss_rows, 0) AS rss,
            IFNULL(bigquery_trends_rows, 0) AS bigquery_trends,
            IFNULL(youtube_rows, 0) AS youtube,
            IFNULL(gdelt_rows, 0) AS gdelt,
            IFNULL(ensemble_rows, 0) AS ensembledata,
            IFNULL(reddit_rows, 0) AS reddit,
            IFNULL(brand24_rows, 0) AS brand24,
            IFNULL(socialcrawl_rows, 0) AS socialcrawl,
            IFNULL(apple_music_rows, 0) AS apple_music,
            IFNULL(briefs_generated, 0) AS briefs,
            ROW_NUMBER() OVER (
                PARTITION BY market, DATE(started_at)
                ORDER BY started_at DESC
            ) AS rn
        FROM `ogilvy-trends-v2.trends_v2_dev.pipeline_runs`
        WHERE DATE(started_at) = @d
    )
    SELECT
        market,
        rss,
        bigquery_trends,
        youtube,
        gdelt,
        ensembledata,
        reddit,
        brand24,
        socialcrawl,
        apple_music,
        briefs
    FROM latest
    WHERE rn = 1
    ORDER BY market
    """
    cfg = bq.QueryJobConfig(query_parameters=[bq.ScalarQueryParameter("d", "DATE", trend_date)])
    per_source: dict[str, dict[str, int]] = {}
    briefs: dict[str, int] = {}
    for row in client.query(sql, job_config=cfg).result():
        m = row.market
        per_source.setdefault("rss", {})[m] = int(row.rss or 0)
        per_source.setdefault("bigquery_trends", {})[m] = int(row.bigquery_trends or 0)
        per_source.setdefault("youtube", {})[m] = int(row.youtube or 0)
        per_source.setdefault("gdelt", {})[m] = int(row.gdelt or 0)
        per_source.setdefault("ensembledata", {})[m] = int(row.ensembledata or 0)
        per_source.setdefault("reddit", {})[m] = int(row.reddit or 0)
        per_source.setdefault("brand24", {})[m] = int(row.brand24 or 0)
        per_source.setdefault("socialcrawl", {})[m] = int(row.socialcrawl or 0)
        per_source.setdefault("apple_music", {})[m] = int(row.apple_music or 0)
        briefs[m] = int(row.briefs or 0)

    # Daily summary tokens
    sql2 = """
    SELECT COALESCE(MAX(completion_tokens), 0) AS tok
    FROM `ogilvy-trends-v2.trends_v2_dev.daily_summary`
    WHERE trend_date = @d
    """
    cfg2 = bq.QueryJobConfig(query_parameters=[bq.ScalarQueryParameter("d", "DATE", trend_date)])
    ds_rows = list(client.query(sql2, job_config=cfg2).result())
    ds_tokens = int(ds_rows[0].tok if ds_rows else 0)

    return per_source, briefs, ds_tokens


# ----------------------------------------------------------------------
# Bucketing + verdict


def _bucket_for(utilisation_pct: float, warn: int, critical: int, may_be_zero: bool = False) -> str:
    # Special case: critical=0 declares the connector as expected-zero
    # (e.g. Spotify pre-secrets, KE bigquery_trends). Never alert on it.
    if critical == 0:
        return "CLEAN"
    if utilisation_pct >= warn:
        return "CLEAN"
    if utilisation_pct >= critical:
        return "WARN"
    if utilisation_pct == 0:
        # An unexpected zero is the bad-key signature and FAILs, UNLESS the
        # connector is allow-listed as legitimately-zero-some-days (GDELT). For
        # those, a zero degrades to WARN so it surfaces without false-FAILing
        # the verdict; the explicit CONNECTOR-ZERO check owns the hard alert.
        return "WARN" if may_be_zero else "FAIL"
    return "CRITICAL"


# ----------------------------------------------------------------------
# Connector-zero alert (the bad-key signature)


# Code-level fallback when engine_capacity.yaml carries no
# connector_zero_alert.may_be_zero list. GDELT is the documented legitimate
# zero (V2Persons + slang sort returns 23-48 rows/day, sometimes 0 on a thin
# partition).
_DEFAULT_MAY_BE_ZERO = frozenset({"gdelt"})


def _load_may_be_zero(capacity: dict[str, Any]) -> set[str]:
    """Resolve the may-be-zero allow-list from engine_capacity.yaml.

    Connectors here are exempt from the CONNECTOR-ZERO check because a genuine
    zero is an expected state for them, not the bad-key signature. Falls back
    to `_DEFAULT_MAY_BE_ZERO` only when the key is absent so the check is never
    accidentally disarmed for GDELT; an explicit empty list means no exemptions
    (the 2026-06-16 GDELT de-list, where a GDELT zero is now a real bad-key
    signal).
    """
    block = capacity.get("connector_zero_alert") or {}
    listed = block.get("may_be_zero")
    if listed is None:
        return set(_DEFAULT_MAY_BE_ZERO)
    return {str(name) for name in listed}


def _connector_enabled(name: str, sources: dict[str, Any]) -> bool:
    """Mirror each connector's real master switch in sources.yaml.

    The enabled flag shape is bespoke per connector (verified against the
    connector code in src/ingestion/connectors/):
      - rss            : always on (feed presence; no master switch)
      - youtube        : always on (query presence; no master switch)
      - bigquery_trends: `active` defaults True (bigquery_trends.py:90)
      - gdelt          : `active` defaults True (gdelt.py:104)
      - apple_music    : `active` defaults True (apple_music.py:75)
      - reddit         : `enabled` defaults False (reddit.py:133)
      - spotify        : `enabled` defaults False (spotify.py:118)
      - ensembledata   : gated by ensemble_budget.enabled + query terms present
      - brand24        : off when `enabled: false` (retired 21 Aug 2026);
                         otherwise enabled when any market has a non-empty
                         projects list

    An unknown connector is treated as enabled (fail-loud: a newly declared
    capacity floor should not be silently exempt from the zero check).
    """
    if name in ("rss", "youtube"):
        return True
    if name in ("bigquery_trends", "gdelt", "apple_music"):
        return bool((sources.get(name) or {}).get("active", True))
    if name in ("reddit", "spotify"):
        return bool((sources.get(name) or {}).get("enabled", False))
    if name == "socialcrawl":
        sc = sources.get("socialcrawl") or {}
        return bool(sc.get("enabled", False)) and bool(sc.get("markets"))
    if name == "ensembledata":
        budget_on = bool((sources.get("ensemble_budget") or {}).get("enabled", True))
        has_terms = bool(sources.get("ensembledata"))
        return budget_on and has_terms
    if name == "brand24":
        b24 = sources.get("brand24") or {}
        if b24.get("enabled") is False:
            return False
        return any((b24.get(m, {}).get("projects") or []) for m in ("za", "ng", "ke"))
    return True


def check_connector_zero(
    connectors: list[ConnectorPulse],
    sources: dict[str, Any],
    may_be_zero: set[str],
) -> list[str]:
    """Return the names of connectors hard-flagged by the CONNECTOR-ZERO check.

    A connector is flagged when ALL of:
      - it is ENABLED in sources.yaml (real master switch), AND
      - it declares a nonzero capacity floor in engine_capacity.yaml
        (expected_per_day > 0 and critical_pct > 0), AND
      - it is NOT on the may-be-zero allow-list, AND
      - it produced exactly 0 rows.

    This is the 3-Jun bad-key signature: an enabled, normally-high-volume
    connector returning 0 rows. Run status is NOT inspected here; the row
    counts come from the latest run per market for the date (see
    _bq_row_counts), so a killed-then-rerun day does not double count. The
    utilisation buckets already WARN on under-volume; this is the harder,
    named signal that a connector silently flat-lined.
    """
    flagged: list[str] = []
    for c in connectors:
        if c.name in may_be_zero:
            continue
        if c.expected_per_day <= 0 or c.critical_pct <= 0:
            continue  # expected-zero (e.g. spotify) is not a flat-line
        if c.actual_per_day != 0:
            continue
        if not _connector_enabled(c.name, sources):
            continue  # a disabled connector is meant to be at zero
        flagged.append(c.name)
    return flagged


def check_cross_market_zero(
    connectors: list[ConnectorPulse],
    sources: dict[str, Any],
    may_be_zero: set[str],
) -> list[str]:
    """Flag a connector that flat-lined for SOME markets while others produced rows.

    The overall-utilisation and CONNECTOR-ZERO checks both miss this: a
    connector can look healthy in aggregate while silently producing nothing
    for one or two markets. This is the 15-Jun signature where Reddit charging
    the shared EnsembleData ledger zeroed NG and KE ensemble while ZA ensemble
    still produced rows, so ensemble's overall count looked fine and 2 of 3
    markets lost all TikTok / IG / Threads ingestion for weeks unnoticed.

    A connector is flagged when ALL of:
      - it is ENABLED in sources.yaml, AND
      - it is NOT on the may-be-zero allow-list, AND
      - it declares >= 2 markets expected non-zero
        (expected_rows_per_market_per_day > 0), AND
      - at least one of those expected-live markets produced 0 rows WHILE at
        least one sibling expected-live market produced rows.

    Data-driven off per_market_expected, so it automatically covers any current
    or future connector that declares per-market expectations.
    """
    flagged: list[str] = []
    for c in connectors:
        if c.name in may_be_zero:
            continue
        if not _connector_enabled(c.name, sources):
            continue
        live_markets = [m for m, e in c.per_market_expected.items() if e and e > 0]
        if len(live_markets) < 2:
            continue
        zero = sorted(m for m in live_markets if c.per_market_actual.get(m, 0) == 0)
        nonzero = [m for m in live_markets if c.per_market_actual.get(m, 0) > 0]
        if zero and nonzero:
            flagged.append(
                f"{c.name} (zero: {', '.join(zero)}; live: {', '.join(sorted(nonzero))})"
            )
    return flagged


def _verdict_for(
    connectors: list[ConnectorPulse],
    briefs_actual: int,
    briefs_expected: int,
    connector_zero_alerts: list[str] | None = None,
    cross_market_zero_alerts: list[str] | None = None,
    daily_summary_tokens: int | None = None,
    daily_summary_min_tokens: int | None = None,
) -> str:
    if connector_zero_alerts or cross_market_zero_alerts:
        return "FAIL"
    fails = [c for c in connectors if c.bucket == "FAIL"]
    critical = [c for c in connectors if c.bucket == "CRITICAL"]
    warn = [c for c in connectors if c.bucket == "WARN"]
    brief_critical = briefs_actual < briefs_expected * 0.75
    brief_warn = briefs_actual < briefs_expected * 0.90
    # Tokens below the configured floor mean the Gemini daily_summary fell back
    # to the canned template; the brief did not really generate. Treat it as a
    # degradation so a fallback day stops reading CLEAN. Only checked when both
    # the observed count and the floor are supplied.
    summary_fallback = (
        daily_summary_tokens is not None
        and daily_summary_min_tokens is not None
        and daily_summary_tokens < daily_summary_min_tokens
    )

    if fails or brief_critical:
        return "FAIL"
    if critical or summary_fallback:
        return "DEGRADED"
    if len(warn) >= 3 or brief_warn:
        return "DEGRADED"
    if warn:
        return "MINOR"
    return "CLEAN"


# ----------------------------------------------------------------------
# Auto-suggest matcher


def _flag_state_from_sources(sources: dict[str, Any]) -> dict[str, bool]:
    """Read the current flag state of known auto-suggest gates."""
    state: dict[str, bool] = {}

    # Brand24 include_mentions per project (any false counts as the gate firing).
    # A retired vendor (`enabled: false`) raises no gate, so nothing suggests a flip.
    b24 = sources.get("brand24") or {}
    if b24.get("enabled") is False:
        b24 = {}
    state["include_mentions_false"] = any(
        not p.get("include_mentions", False)
        for m in ("za", "ng", "ke")
        for p in (b24.get(m, {}).get("projects") or [])
    )
    state["include_ai_insights_false"] = any(
        not p.get("include_ai_insights", False)
        for m in ("za", "ng", "ke")
        for p in (b24.get(m, {}).get("projects") or [])
    )
    # Brand24 Wave 2 (shipped 8ad51c7). Each gate fires when ANY project on
    # ANY market has the include flag false.
    state["include_mentions_sentiment_false"] = any(
        not p.get("include_mentions_sentiment", False)
        for m in ("za", "ng", "ke")
        for p in (b24.get(m, {}).get("projects") or [])
    )
    state["include_mentions_reach_false"] = any(
        not p.get("include_mentions_reach", False)
        for m in ("za", "ng", "ke")
        for p in (b24.get(m, {}).get("projects") or [])
    )
    state["include_daily_metrics_false"] = any(
        not p.get("include_daily_metrics", False)
        for m in ("za", "ng", "ke")
        for p in (b24.get(m, {}).get("projects") or [])
    )

    # EnsembleData per-market twitter / tt_comments flags. Wave 1 patch (28 May
    # 2026): only twitter_handles is a real EnsembleData path. The legacy
    # twitter_keywords_enabled / twitter_hashtags_enabled markers were dropped
    # because the underlying vendor endpoints don't exist.
    ens = sources.get("ensembledata") or {}
    state["twitter_handles_enabled_false"] = any(
        not (ens.get(m, {}).get("twitter_handles_enabled", False)) for m in ("za", "ng", "ke")
    )
    state["tt_comments_enabled_false"] = any(
        not (ens.get(m, {}).get("tt_comments_enabled", False)) for m in ("za", "ng", "ke")
    )

    # Wave 3 EnsembleData dark surfaces. Each marker fires when ANY market has
    # the flag absent / false. Markers gate auto_suggest rules in
    # engine_capacity.yaml that recommend flipping the flag.
    _wave3_flags = (
        # YouTube via EnsembleData
        "yt_shorts_enabled",
        "yt_video_comments_enabled",
        "yt_channel_videos_enabled",
        "yt_keyword_search_enabled",
        # Instagram
        "ig_post_comments_enabled",
        "ig_user_reels_enabled",
        "ig_user_tagged_posts_enabled",
        # Threads
        "threads_post_replies_enabled",
        # TikTok
        "tt_music_posts_enabled",
        "tt_post_comment_replies_enabled",
        "tt_post_info_enabled",
    )
    for flag in _wave3_flags:
        # marker name is "<flag-name-minus-_enabled>_disabled" e.g.
        # yt_shorts_enabled -> yt_shorts_disabled.
        marker = flag[: -len("_enabled")] + "_disabled"
        state[marker] = any(not (ens.get(m, {}).get(flag, False)) for m in ("za", "ng", "ke"))

    # GDELT flags
    g = sources.get("gdelt") or {}
    state["gdelt_events_enabled_false"] = not g.get("gdelt_events_enabled", False)
    state["gcam_enabled_false"] = not g.get("gcam_enabled", False)

    # Spotify
    sp = sources.get("spotify") or {}
    state["spotify_disabled_no_secrets"] = not sp.get("enabled", False)

    # Wave 2 (shipped 8ad51c7).
    # BigQuery Trends top_terms.enabled false -> bq_top_terms_disabled
    bq = sources.get("bigquery_trends") or {}
    state["bq_top_terms_disabled"] = not (bq.get("top_terms") or {}).get("enabled", False)
    # YouTube playlist_items_enabled false on ANY market triggers the gate. Read
    # through the estate's own reader, because the estate and this watchdog gate
    # on one flag and two readers of one flag may not disagree: `false` in quotes
    # read for its truthiness answered the opposite of what it spells, and a
    # present null is the connector's own bool(None) rather than its default.
    yt = sources.get("youtube_queries") or {}
    state["playlist_items_disabled"] = any(
        not flag_reads_on(yt.get(m), "playlist_items_enabled") for m in ("za", "ng", "ke")
    )

    return state


def _eval_condition(condition: str, util: dict[str, float], flags: dict[str, bool]) -> bool:
    """Tiny expression evaluator for auto_suggest conditions.

    Supports:
    - `<connector>_utilisation_below_<N>` (compared against `util[<connector>]`)
    - flag names from `_flag_state_from_sources` (truthy bool)
    - `AND` between terms

    Anything unrecognised returns False so an unknown rule doesn't fire.
    """
    if not isinstance(condition, str):
        return False
    parts = [p.strip() for p in condition.split("AND")]
    for term in parts:
        if "_utilisation_below_" in term:
            try:
                connector, _, threshold = term.partition("_utilisation_below_")
                if connector not in util:
                    # Unknown connector means no reading, not zero spend.
                    # Defaulting to 0.0 made a misspelled connector always
                    # satisfy the term, which is the opposite of fail-closed.
                    return False
                pct = util[connector]
                if not (pct < float(threshold)):
                    return False
            except (ValueError, AttributeError):
                return False
        else:
            if not flags.get(term, False):
                return False
    return True


def _connector_from_condition(condition: str) -> str | None:
    """Extract the connector named in a `<connector>_utilisation_below_<N>` term.

    Returns the first connector found, or None if the condition has no
    utilisation term (flag-only conditions).
    """
    if not isinstance(condition, str):
        return None
    for term in (p.strip() for p in condition.split("AND")):
        if "_utilisation_below_" in term:
            connector, _, _ = term.partition("_utilisation_below_")
            return connector or None
    return None


def _format_action(
    action_template: str, util: dict[str, float], connector: str | None = None
) -> str:
    """Fill `{pct}` with the matched rule's connector utilisation.

    The placeholder must reflect the SPECIFIC connector the rule fired on,
    not an average across every connector. A blended average is meaningless
    (and misleading: a cratered GDELT at 13% rendered as 154% because high
    utilisers like brand24 dominated the mean). When the connector's util is
    unavailable the placeholder is dropped rather than faked.
    """
    if "{pct}" not in action_template:
        return action_template
    if connector and connector in util:
        return action_template.replace("{pct}", f"{util[connector]:.0f}")
    # No reliable per-connector figure: strip the " at {pct}% of expected"
    # style fragment cleanly rather than printing a wrong number.
    return action_template.replace(" at {pct}% of expected", "").replace("{pct}", "?")


ENSEMBLEDATA_DAILY_CAP = 5000


def _compute_ensembledata_quota_state(
    ensembledata_token: str | None,
    trend_date: str | None = None,
) -> dict[str, Any] | None:
    """Pull EnsembleData unit-spend snapshot from the free Customer endpoints.

    Returns a dict shape:
        {
            "today_used": {"tiktok": int, "instagram": int, ...},
            "today_total": int,
            "rolling_avg_per_day": float,
            "cap": int,
            "headroom_today": int,
        }
    or None when the token is missing or any underlying call fails. Defensive:
    this is an observability hook, never raises into the watchdog.
    """
    if not ensembledata_token:
        return None
    try:
        # Imported lazily so engine_pulse keeps loading even if customer_units.py
        # hasn't shipped yet on a given branch.
        from src.ingestion.connectors.customer_units import (  # type: ignore[import-not-found]
            fetch_units_history,
            fetch_used_units,
        )

        target_date = trend_date or datetime.now(UTC).date().isoformat()
        today = fetch_used_units(ensembledata_token, target_date) or {}
        history = fetch_units_history(ensembledata_token, 14) or []

        # Both empty means a vendor outage, not zero spend. Synthesising a
        # zero-spend snapshot here would read as full headroom and mask the
        # outage. Return None (unknown) so callers can tell the two apart.
        if not today and not history:
            return None

        per_platform: dict[str, int] = {}
        for platform, value in today.items():
            if platform in ("date", "total", "total_units"):
                continue
            try:
                per_platform[str(platform)] = int(value or 0)
            except (TypeError, ValueError):
                continue
        if isinstance(today, dict) and "total" in today:
            today_total = int(today.get("total") or 0)
        else:
            today_total = sum(per_platform.values())

        # Rolling average across the supplied history window.
        history_totals: list[int] = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if "total" in entry:
                try:
                    history_totals.append(int(entry.get("total") or 0))
                except (TypeError, ValueError):
                    continue
            else:
                running = 0
                for k, v in entry.items():
                    if k in ("date", "total", "total_units"):
                        continue
                    try:
                        running += int(v or 0)
                    except (TypeError, ValueError):
                        continue
                history_totals.append(running)
        rolling_avg = sum(history_totals) / len(history_totals) if history_totals else 0.0

        cap = ENSEMBLEDATA_DAILY_CAP
        headroom_today = max(cap - today_total, 0)

        return {
            "today_used": per_platform,
            "today_total": today_total,
            "rolling_avg_per_day": round(rolling_avg, 1),
            "cap": cap,
            "headroom_today": headroom_today,
        }
    except Exception as exc:
        logger.warning("ensembledata quota fetch failed: %s", exc)
        return None


def _quota_flags(quota: dict[str, Any] | None) -> dict[str, bool]:
    """Synthesise auto_suggest gates from the Customer-endpoint snapshot."""
    flags: dict[str, bool] = {
        "ensembledata_quota_headroom_below_500": False,
        "ensembledata_quota_headroom_below_1500": False,
    }
    if not quota:
        return flags
    headroom = int(quota.get("headroom_today", 0) or 0)
    flags["ensembledata_quota_headroom_below_1500"] = headroom < 1500
    flags["ensembledata_quota_headroom_below_500"] = headroom < 500
    return flags


def _select_action(capacity: dict[str, Any], util: dict[str, float], flags: dict[str, bool]) -> str:
    """Walk auto_suggest rules in priority order; return first match."""
    rules = capacity.get("auto_suggest") or []
    priorities = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(rules, key=lambda r: priorities.get(r.get("priority", "low"), 99))
    for rule in ranked:
        cond = rule.get("condition", "")
        if _eval_condition(cond, util, flags):
            connector = _connector_from_condition(cond)
            return _format_action(rule.get("action", ""), util, connector)
    return "no action - all known optimisation hooks already fired or no gap detected"


# ----------------------------------------------------------------------
# Main


def build_pulse(trend_date: str) -> EnginePulse:
    capacity = _load_capacity()
    sources = _load_sources()
    per_source, briefs, ds_tokens = _bq_row_counts(trend_date)

    pulse = EnginePulse(trend_date=trend_date)
    pulse.daily_summary_tokens = ds_tokens
    pulse.briefs_actual = sum(briefs.values())
    pulse.briefs_expected = capacity.get("phase2", {}).get("briefs", {}).get("expected_per_day", 24)

    may_be_zero = _load_may_be_zero(capacity)

    for name, decl in (capacity.get("connectors") or {}).items():
        expected = int(decl.get("expected_rows_per_day", 0))
        warn = int(decl.get("utilisation_warn_pct", 70))
        critical = int(decl.get("utilisation_critical_pct", 40))
        per_market_expected = decl.get("expected_rows_per_market_per_day") or {}
        per_market_actual = per_source.get(name, {})
        actual = sum(per_market_actual.values())
        util = (actual / expected * 100.0) if expected > 0 else 0.0
        if expected == 0:
            # A retired connector (EnsembleData and Reddit from 23 Jul 2026)
            # declares expected 0. Without this it lands on the zero-utilisation
            # branch and FAILs the whole verdict every single day for a vendor
            # we deliberately stopped buying.
            bucket = "RETIRED"
        else:
            bucket = _bucket_for(util, warn, critical, may_be_zero=name in may_be_zero)
        pulse.connectors.append(
            ConnectorPulse(
                name=name,
                expected_per_day=expected,
                actual_per_day=actual,
                per_market_expected={k: int(v) for k, v in per_market_expected.items()},
                per_market_actual={k: int(v) for k, v in per_market_actual.items()},
                warn_pct=warn,
                critical_pct=critical,
                utilisation_pct=util,
                bucket=bucket,
                notes=str(decl.get("notes", "")),
            )
        )

    # CONNECTOR-ZERO check: enabled, normally-high-volume connectors that
    # returned exactly 0 on a success run (the 3-Jun bad-key signature).
    pulse.connector_zero_alerts = check_connector_zero(pulse.connectors, sources, may_be_zero)
    # CROSS-MARKET-ZERO check: enabled connectors healthy in aggregate but
    # flat-lined for some markets (the 15-Jun Reddit->ensemble starvation).
    pulse.cross_market_zero_alerts = check_cross_market_zero(pulse.connectors, sources, may_be_zero)

    ds_min_tokens = capacity.get("phase2", {}).get("daily_summary", {}).get("min_completion_tokens")
    pulse.verdict = _verdict_for(
        pulse.connectors,
        pulse.briefs_actual,
        pulse.briefs_expected,
        pulse.connector_zero_alerts,
        pulse.cross_market_zero_alerts,
        daily_summary_tokens=pulse.daily_summary_tokens,
        daily_summary_min_tokens=int(ds_min_tokens) if ds_min_tokens is not None else None,
    )

    # EnsembleData Customer endpoint snapshot (free) — used for quota
    # dashboarding + headroom auto_suggest gates. Falls back to None when the
    # token isn't configured (CI / local without secrets) so the watchdog
    # keeps running unchanged.
    token = os.environ.get("ENSEMBLEDATA_API_TOKEN") or None
    pulse.ensembledata_quota = _compute_ensembledata_quota_state(token, trend_date)

    util_map = {c.name: c.utilisation_pct for c in pulse.connectors}
    flag_state = _flag_state_from_sources(sources)
    flag_state.update(_quota_flags(pulse.ensembledata_quota))
    pulse.recommended_action = _select_action(capacity, util_map, flag_state)

    return pulse


def render_text(pulse: EnginePulse) -> str:
    out: list[str] = []
    out.append(f"ENGINE PULSE {pulse.trend_date}")
    out.append("")
    out.append(f"VERDICT: {pulse.verdict}")
    out.append("")
    out.append("PER-CONNECTOR UTILISATION")
    out.append(f"  {'connector':16s} {'actual':>8s} {'expect':>8s} {'%':>6s} {'bucket':>10s}")
    for c in pulse.connectors:
        out.append(
            f"  {c.name:16s} {c.actual_per_day:>8d} {c.expected_per_day:>8d} "
            f"{c.utilisation_pct:>5.0f}% {c.bucket:>10s}"
        )
    out.append("")
    out.append("CONNECTOR-ZERO")
    if pulse.connector_zero_alerts:
        out.append(
            f"  FAIL: {', '.join(pulse.connector_zero_alerts)} enabled but returned "
            "0 rows on a success run (bad-key signature)"
        )
    else:
        out.append("  clean - no enabled connector flat-lined")
    out.append("")
    out.append("CROSS-MARKET-ZERO")
    if pulse.cross_market_zero_alerts:
        out.append(
            f"  FAIL: {', '.join(pulse.cross_market_zero_alerts)} - healthy in "
            "aggregate but flat-lined for some markets (per-market starvation)"
        )
    else:
        out.append("  clean - no connector flat-lined for a subset of markets")
    out.append("")
    out.append(
        f"PHASE 2  briefs={pulse.briefs_actual} / {pulse.briefs_expected}  "
        f"daily_summary_tokens={pulse.daily_summary_tokens}"
    )
    if pulse.ensembledata_quota:
        q = pulse.ensembledata_quota
        out.append(
            f"PIPELINE ensembledata_quota today_used={q.get('today_total', 0)} / "
            f"{q.get('cap', ENSEMBLEDATA_DAILY_CAP)}  "
            f"headroom={q.get('headroom_today', 0)}  "
            f"rolling_avg_14d={q.get('rolling_avg_per_day', 0.0)}"
        )
    out.append("")
    out.append("RECOMMENDED ACTION")
    out.append(f"  {pulse.recommended_action}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="", help="trend_date YYYY-MM-DD; default today UTC")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text report")
    args = parser.parse_args()

    trend_date = args.date or datetime.now(UTC).date().isoformat()
    # Validate the date format
    try:
        date.fromisoformat(trend_date)
    except ValueError:
        print(f"invalid date: {trend_date}", file=sys.stderr)
        return 2

    try:
        pulse = build_pulse(trend_date)
    except Exception as exc:  # pragma: no cover - top-level safety
        print(f"engine_pulse failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(pulse.to_dict(), indent=2))
    else:
        print(render_text(pulse))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
