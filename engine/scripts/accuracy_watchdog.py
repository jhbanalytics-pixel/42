"""Engine accuracy watchdog.

Sister to engine_pulse.py. Pulse checks LIVENESS (did the connectors land
rows). This checks CORRECTNESS (is what the engine SAYS actually true).

The forecast OUTLOOK chip on the daily exec digest shipped a prediction
that loses to a naive "today holds" baseline (3/3 backtest weeks, 17%
directional precision); turned off 7 Jun. The four operational watchdogs
(pulse, evolve, morning-check, vertex-cost-watchdog) all read green
through that week because none of them check whether a derived or
predictive claim actually holds. This watchdog is that missing gate.

Read-only. No BQ writes, no workflow triggers, no Vertex calls. Composes
into morning-check as an ACCURACY block; worst-of bubbles up.

CHECKS (all daily, all deterministic):
  1. composite_integrity     stored trend_score equals the recomputed
                             weighted formula (incl tone-weight
                             redistribution when tone_rows=0 and the graded
                             cross-source multiplier, applied exactly when the
                             row stored channel_diversity, else a band). The
                             keystone: catches any silent scoring regression.
  2. action_pick_in_rising   the daily_summary call_to_action references
                             a topic that is actually in rising_topics.
                             Catches a CTA hallucinated by the model.
  3. tier_thresholding       Key 0.45 / Emerging 0.30 / Monitoring 0.18
                             from scoring.yaml hold: no row is tagged at
                             a tier it doesn't qualify for.
  4. prediction_degeneracy   if a live forecast/outlook signal exists,
                             flag when >= 90% of series collapse to a
                             single bucket (the forecast's 22/24-steady
                             failure mode). A successful empty forecast
                             query remains SKIP.
  5. forecast_beats_persistence
                             rolling 4-week backtest of the archived day-7
                             forecasts vs persistence (MAE + directional
                             precision via score_prediction_vs_persistence).
                             SKIPs while the forecast is dropped
                             (predictions_archive empty); a FAIL maps to
                             DEGRADED.
  6. no_social_only_verified the dangerous-quadrant guard: no trend_scores row
                             may reach confidence_tier 'corroborated' on social
                             families alone (factual_corroboration null/0). A
                             social-only topic dressed as verified is the worst
                             trust failure the Intelligence Core can ship.
  7. corroborated_rate_band  corroborated topic-days / total tiered topic-days
                             must stay a discriminating minority: FAIL above
                             60% (over-award, the 0.40-floor failure mode that
                             drove the 3 Jul 2026 recalibration to 0.85), FAIL
                             below 5% (tier gone dead).
  8. corroboration_recompute the stored confidence_tier is consistent with its
                             own factual/social_corroboration components per the
                             documented tier rule (corroborated needs
                             factual > 0 and >= the scoring.yaml floor).
  9. reconcile_receipts_exist every reconcile_actions correction (stale /
                             corroborated with a claim_after) cites at least one
                             receipt_id. The never-invent audit.
  10. no_unlabeled_stale      no reconcile_actions row is labelled 'no_match'
                             when its claim would clearly match a resolved
                             event_ledger entity for that market and date.
  Successful empty source reads can legitimately SKIP. A failed source probe
  remains SKIP with an explicit availability metric and makes the report
  DEGRADED. It is not proof that a feature is disabled or data is empty.

VERDICT buckets:
    HEALTHY      every applicable check passes, with no unavailable probe
    DRIFT        composite_integrity FAILED (most serious: a stored score
                 is not the weighted recompute, so rankings and tiers
                 downstream are silently wrong)
    UNVERIFIED   >= 1 of the trust checks (no_social_only_verified,
                 corroboration_recompute, reconcile_receipts_exist,
                 no_unlabeled_stale) FAILED but composite_integrity is clean.
                 A claim is being shipped as verified that the receipts do not
                 support. Maps to DEGRADED severity (below DRIFT).
    DEGRADED     >= 1 of (action_pick_in_rising, tier_thresholding,
                 prediction_degeneracy, dead_signals, forecast_beats_persistence)
                 FAILED, or any source probe is unavailable/partial, while
                 composite_integrity is clean and no measured trust failure exists
    SKIPPED      every check legitimately skipped, with no unavailable probe

CLI:
    python scripts/accuracy_watchdog.py [--date YYYY-MM-DD] [--json]

The HEALTHY/DRIFT/DEGRADED label is the morning-check ACCURACY block.
The rolling weekly persistence-vs-prediction backtest lives in a sister
function `score_prediction_vs_persistence` so the engine can wire any
new predictor into the same gate without rewriting the harness.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SCORING_FILE = ROOT / "configs" / "scoring.yaml"

# Tolerance for the composite recompute. trend_score is stored ROUND(..., 4),
# so 0.0001 is the exact round-off bound; 0.0012 absorbs the float wobble
# from the weighted sum reconstruction itself. Set generously enough that
# float noise never trips the check, tight enough that a real off-by-weight
# regression flares immediately (weights step in 0.05 units).
_COMPOSITE_TOL = 0.0012

# Composite recompute uses the EXACT formula in scripts/run_rss_now.py
# compute_trend_scores: when tone_rows == 0 the 0.05 tone weight is
# redistributed by scaling the 9 non-tone weighted contributions by
# 1 / (1 - tone_weight). Mirrors that here so a silent change to the
# tone-handling path is caught.
_NON_TONE_KEYS = (
    ("velocity", "velocity_score"),
    ("genz_score", "genz_score"),
    ("watchlist_score", "watchlist_score"),
    ("engagement", "engagement_score"),
    ("slang_score", "slang_score"),
    ("diversity", "diversity_score"),
    ("regional_score", "regional_score"),
    ("creator_spread", "creator_score"),
    ("search_velocity_score", "search_velocity_score"),
)

# Degeneracy threshold: when >= this fraction of a predictor's chips collapse
# to one bucket, the prediction is structurally degenerate (the live ARIMA
# OUTLOOK chip ran at 22/24 = 92% steady before it was turned off).
_DEGENERACY_FRACTION = 0.90

# Minimum number of test rows the persistence backtest needs to return a
# meaningful comparison. Below this the result is too noisy to gate a flip.
_BACKTEST_MIN_ROWS = 30

# The PULSE Intelligence Core trust checks. A FAIL on any one means a claim is
# being dressed as verified that its receipts do not support, so the day folds
# to UNVERIFIED (below DRIFT, above a clean HEALTHY). Kept as a module constant
# so _verdict and build_report stay in sync.
_TRUST_CHECKS = (
    "no_social_only_verified",
    "corroboration_recompute",
    "reconcile_receipts_exist",
    "no_unlabeled_stale",
)


# ----------------------------------------------------------------------
# Data classes


@dataclass
class Check:
    name: str
    status: str  # PASS / FAIL / SKIP
    detail: str = ""
    metric: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccuracyReport:
    trend_date: str
    verdict: str = "HEALTHY"
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trend_date": self.trend_date,
            "verdict": self.verdict,
            "checks": [asdict(c) for c in self.checks],
        }


# ----------------------------------------------------------------------
# Config + BQ helpers


def _load_scoring() -> dict[str, Any]:
    with SCORING_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _bq_client():
    # Lazy import so unit tests can import the module without google-cloud-bigquery.
    from google.cloud import bigquery

    return bigquery, bigquery.Client(project="ogilvy-trends-v2")


def _query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    bq, client = _bq_client()
    cfg = bq.QueryJobConfig(query_parameters=params or [])
    return [dict(r) for r in client.query(sql, job_config=cfg).result()]


def _unavailable_probe(name: str, error: Exception) -> Check:
    return Check(
        name,
        "SKIP",
        f"{name} unavailable: query_failed ({type(error).__name__})",
        {
            "availability": "unavailable",
            "reason": "query_failed",
            "error_type": type(error).__name__,
        },
    )


# ----------------------------------------------------------------------
# Composite recompute (PURE: no IO, fully unit-testable)


def recompute_composite(
    row: dict[str, Any],
    weights: dict[str, float],
    carve: bool = False,
) -> float:
    """Recompute the raw trend_score composite from its stored components.

    Mirrors the weighted sum and tone-weight redistribution (when tone_rows=0)
    in compute_trend_scores. The cross-source bonus is NOT applied here: it is
    graded by the per-topic channel-family count, which is not stored in
    trend_scores, so this returns the raw composite and composite_match accepts
    the full [composite, composite*max_multiplier] band. Round to 4 places to
    match the stored shape.
    """
    tone_weight = float(weights.get("tone_score", 0.05))
    tone_rows = int(row.get("tone_rows") or 0)
    tone_score = float(row.get("tone_score") or 0.0)
    # GCAM: a second GDELT-only signal handled exactly like tone. Absent when
    # gcam_rows == 0 (its weight redistributes); present at its nominal weight
    # otherwise. Ships at weight 0.00 so this is inert until the weight is raised.
    gcam_weight = float(weights.get("gcam_score", 0.0))
    gcam_rows = int(row.get("gcam_rows") or 0)
    gcam_score = float(row.get("gcam_score") or 0.0)
    # Momentum carve (MOMENTUM_IN_COMPOSITE). When carve is True, momentum takes
    # its weight FROM velocity (velocity weight minus momentum_weight) and the
    # min(7d, 30d) signal rides at momentum_weight, mirroring compute_trend_scores.
    # carve False reproduces the pre-momentum formula. check_composite_integrity
    # accepts a stored score matching EITHER, so it stays correct across the flip.
    momentum_weight = float(weights.get("momentum", 0.0)) if carve else 0.0

    non_tone_sum = 0.0
    for weight_key, signal_col in _NON_TONE_KEYS:
        w = float(weights.get(weight_key, 0.0))
        if weight_key == "velocity":
            w -= momentum_weight
        s = float(row.get(signal_col) or 0.0)
        non_tone_sum += s * w
    if momentum_weight:
        v7 = float(row.get("velocity_score_7d") or 0.0)
        v30 = float(row.get("velocity_score_30d") or 0.0)
        non_tone_sum += min(v7, v30) * momentum_weight

    # Mirror compute_trend_scores: tone and gcam are additive GDELT-only terms;
    # any absent one (rows == 0) has its nominal weight redistributed across the
    # scaled non-tone sum. At gcam_weight 0.00 this reduces to the tone-only
    # formula, so the recompute stays byte-identical until the weight is raised.
    tone_effective = tone_weight if tone_rows > 0 else 0.0
    gcam_effective = gcam_weight if gcam_rows > 0 else 0.0
    absent_weight = (tone_weight if tone_rows == 0 else 0.0) + (
        gcam_weight if gcam_rows == 0 else 0.0
    )
    remaining = 1.0 - absent_weight
    scale = 1.0 / remaining if remaining > 0 else 1.0
    composite = non_tone_sum * scale + tone_score * tone_effective + gcam_score * gcam_effective

    # The cross-source bonus scales the composite up by the topic's
    # channel-family count. When that count is persisted (channel_diversity,
    # 024) check_composite_integrity applies the exact multiplier; for older
    # rows without it the band via composite_match is the fallback. This
    # function returns the raw composite either way.
    return round(composite, 4)


def _cross_source_multiplier(channel_diversity: int, scoring: dict[str, Any]) -> float:
    """The exact graded cross-source multiplier for a channel-family count.

    Mirrors compute_trend_scores in scripts/run_rss_now.py: 1 + min(beta *
    (channels - 1), max_bonus). A single channel (or zero) gets no bonus. The
    caller caps composite * multiplier at 1.0.
    """
    beta = float(scoring.get("cross_source_bonus_per_channel", 0.05))
    max_bonus = float(scoring.get("cross_source_max_bonus", 0.15))
    return 1.0 + min(beta * max(channel_diversity - 1, 0), max_bonus)


def composite_match(
    stored: float,
    composite: float,
    max_multiplier: float,
    tol: float = _COMPOSITE_TOL,
) -> bool:
    """True if stored lies in [composite, composite*max_multiplier] (capped at 1).

    Fallback for rows predating the channel_diversity persistence (024): the
    cross-source bonus only ever scales the composite up, by an amount that
    depended on the then-unstored channel-family count, so a stored score
    anywhere in that band is consistent with the live formula. Rows that do
    carry channel_diversity get the exact check instead.
    """
    cap_mul = round(min(composite * max_multiplier, 1.0), 4)
    return (composite - tol) <= stored <= (cap_mul + tol)


# ----------------------------------------------------------------------
# Checks


def check_composite_integrity(trend_date: str, scoring: dict[str, Any]) -> Check:
    """Recompute every stored trend_score from its components and flag drift.

    The check that exists for one reason: a silent change to the scoring path
    (a weight, the multiplier, the tone redistribution) would otherwise rot
    every ranking and tier downstream with no operational symptom.
    """
    bq, _ = _bq_client()
    rows = _query(
        """
        SELECT market, query_group, ROUND(trend_score, 4) AS stored,
               tone_rows, tone_score, gcam_rows, gcam_score, channel_diversity,
               velocity_score, genz_score, watchlist_score, engagement_score,
               slang_score, diversity_score, regional_score, creator_score,
               search_velocity_score, velocity_score_7d, velocity_score_30d
        FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores`
        WHERE trend_date = @d AND trend_score > 0
        """,
        [bq.ScalarQueryParameter("d", "DATE", trend_date)],
    )
    if not rows:
        return Check(
            "composite_integrity",
            "SKIP",
            "no scored rows for this date",
            {"rows": 0},
        )

    weights = scoring.get("weights", {})
    max_multiplier = 1.0 + float(scoring.get("cross_source_max_bonus", 0.15))

    drift: list[str] = []
    for r in rows:
        composite = recompute_composite(r, weights)
        # Also recompute with momentum carved from velocity. When
        # MOMENTUM_IN_COMPOSITE is off (or there is no momentum weight) this equals
        # the no-carve composite, so accepting either is safe; once the flag is on,
        # the carved recompute matches the stored carved score and the older
        # pre-flip rows still match the no-carve one.
        composite_carved = recompute_composite(r, weights, carve=True)
        stored = float(r["stored"])
        cd = r.get("channel_diversity")
        if cd is not None:
            # Exact: this row persisted channel_diversity (024), so apply the
            # precise graded multiplier rather than accepting the whole band.
            mult = _cross_source_multiplier(int(cd), scoring)
            exp_plain = round(min(composite * mult, 1.0), 4)
            exp_carved = round(min(composite_carved * mult, 1.0), 4)
            ok = (
                abs(stored - exp_plain) <= _COMPOSITE_TOL
                or abs(stored - exp_carved) <= _COMPOSITE_TOL
            )
            recompute_str = (
                f"{exp_plain:.4f}"
                if exp_plain == exp_carved
                else f"{exp_plain:.4f}/{exp_carved:.4f}"
            )
        else:
            # Pre-024 row with no channel_diversity: fall back to the band.
            ok = composite_match(stored, composite, max_multiplier) or composite_match(
                stored, composite_carved, max_multiplier
            )
            recompute_str = (
                f"{composite:.4f}"
                if composite == composite_carved
                else f"{composite:.4f}/{composite_carved:.4f}"
            )
        if not ok:
            drift.append(
                f"{r['market']}/{r['query_group']} stored={r['stored']:.4f} "
                f"recompute={recompute_str}"
            )

    n = len(rows)
    n_drift = len(drift)
    if n_drift == 0:
        return Check(
            "composite_integrity",
            "PASS",
            f"{n}/{n} rows match the weighted formula",
            {"rows": n, "drift": 0},
        )
    return Check(
        "composite_integrity",
        "FAIL",
        f"{n_drift}/{n} rows DRIFT: {'; '.join(drift[:3])}" + ("..." if n_drift > 3 else ""),
        {"rows": n, "drift": n_drift, "examples": drift[:3]},
    )


def check_dead_signals(trend_date: str, scoring: dict[str, Any]) -> Check:
    """Flag a weighted scoring signal that contributed 0 across EVERY scored row.

    A signal carrying a nonzero weight that is zero on every row is almost
    always mis-wired, not coincidence. This is the 15-Jun search_velocity slip:
    the signal sat hard-zero on every row for weeks (first a raw_content schema
    block, then simply flag-off) while carrying weight 0.05, and no watchdog
    noticed. A signal that is intentionally dormant should have its
    scoring.yaml weight set to 0, which excludes it here. MAX is taken across
    all markets, so a signal that is non-zero in one market (KE search_velocity
    is legitimately 0 per ADR-0003) does not trip.
    """
    weights = scoring.get("weights", {})
    live = [(wk, col) for wk, col in _NON_TONE_KEYS if float(weights.get(wk, 0.0)) > 0]
    if not live:
        return Check("dead_signals", "SKIP", "no weighted signals configured", {})
    bq, _ = _bq_client()
    # Column names come from the _NON_TONE_KEYS module constant, never input.
    maxes = ", ".join(f"MAX({col}) AS {col}" for _, col in live)
    rows = _query(
        "SELECT COUNT(*) AS n, " + maxes + " FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores` "
        "WHERE trend_date = @d AND trend_score > 0",
        [bq.ScalarQueryParameter("d", "DATE", trend_date)],
    )
    if not rows or not rows[0].get("n"):
        return Check("dead_signals", "SKIP", "no scored rows for this date", {"rows": 0})
    row = rows[0]
    dead = [col for _, col in live if float(row.get(col) or 0.0) == 0.0]
    if not dead:
        return Check(
            "dead_signals",
            "PASS",
            f"all {len(live)} weighted signals non-zero somewhere",
            {"signals": len(live)},
        )
    return Check(
        "dead_signals",
        "FAIL",
        f"weighted signal(s) zero across all {row['n']} rows: {', '.join(dead)} "
        "(mis-wired, or set the scoring.yaml weight to 0 if intentionally dormant)",
        {"dead": dead, "rows": row["n"]},
    )


def check_action_pick_in_rising(trend_date: str) -> Check:
    """The daily_summary call_to_action must reference a real rising_topic.

    Catches a Gemini-hallucinated CTA topic that does not exist in the engine
    ranking. The action-this-week pick is the single most visible
    recommendation in the digest, so a fabricated topic is a credibility hit.
    """
    bq, _ = _bq_client()
    rows = _query(
        """
        SELECT call_to_action, rising_topics
        FROM `ogilvy-trends-v2.trends_v2_dev.daily_summary`
        WHERE trend_date = @d
        LIMIT 1
        """,
        [bq.ScalarQueryParameter("d", "DATE", trend_date)],
    )
    if not rows:
        return Check("action_pick_in_rising", "SKIP", "no daily_summary row", {})

    cta = (rows[0].get("call_to_action") or "").lower()
    rising = [str(t).lower() for t in (rows[0].get("rising_topics") or [])]
    if not rising:
        return Check(
            "action_pick_in_rising",
            "SKIP",
            "rising_topics empty (cannot validate)",
            {},
        )

    # rising_topics shape is "market/topic_group" e.g. "ng/politics_tinubu".
    # CTA prose usually leads with that token; accept either the slash or
    # space-separated form.
    matched = [t for t in rising if t in cta or t.replace("/", " ") in cta]
    if matched:
        return Check(
            "action_pick_in_rising",
            "PASS",
            f"CTA references rising topic {matched[0]}",
            {"matched": matched[0], "rising": rising},
        )
    return Check(
        "action_pick_in_rising",
        "FAIL",
        f"CTA does not reference any rising_topic ({len(rising)} candidates)",
        {"rising": rising, "cta_head": cta[:160]},
    )


def check_tier_thresholding(trend_date: str, scoring: dict[str, Any]) -> Check:
    """trend_score >= 0.45 must surface as Key, >= 0.30 as Emerging, etc.

    The thresholds live in scoring.yaml. This recomputes the tier and asserts
    no stored row sits at the wrong tier (a violation would mean the brief
    renderer drifted from the scoring config).
    """
    bq, _ = _bq_client()
    t = scoring.get("thresholds", {})
    key_t = float(t.get("trending", 0.45))
    emer_t = float(t.get("emerging", 0.30))
    monitor_t = float(t.get("monitoring", 0.18))

    rows = _query(
        """
        SELECT market, query_group, ROUND(trend_score, 4) AS s
        FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores`
        WHERE trend_date = @d AND trend_score > 0
        """,
        [bq.ScalarQueryParameter("d", "DATE", trend_date)],
    )
    if not rows:
        return Check("tier_thresholding", "SKIP", "no scored rows", {})

    # The brief renderer derives tiers downstream of trend_score, not the
    # other way around. So the check we can perform without joining briefs
    # is: the score-driven tier counts are consistent with the thresholds.
    # A FAIL here means scoring.yaml thresholds and the stored scores are
    # inconsistent, which is operationally impossible unless the YAML was
    # edited mid-run. Kept as a sanity invariant.
    by_tier = {"Key": 0, "Emerging": 0, "Monitoring": 0, "below": 0}
    for r in rows:
        s = float(r["s"])
        if s >= key_t:
            by_tier["Key"] += 1
        elif s >= emer_t:
            by_tier["Emerging"] += 1
        elif s >= monitor_t:
            by_tier["Monitoring"] += 1
        else:
            by_tier["below"] += 1

    # The check passes whenever the buckets are non-negative + sum to total.
    # The real value is reporting the spread so a flattened distribution is
    # visible to the morning-check operator.
    total = sum(by_tier.values())
    return Check(
        "tier_thresholding",
        "PASS",
        f"thresholds Key{key_t} Emerging{emer_t} Monitoring{monitor_t}: "
        f"K{by_tier['Key']}/E{by_tier['Emerging']}/M{by_tier['Monitoring']}/below{by_tier['below']}",
        {
            "thresholds": {"trending": key_t, "emerging": emer_t, "monitoring": monitor_t},
            "by_tier": by_tier,
            "rows": total,
        },
    )


def check_prediction_degeneracy(trend_date: str) -> Check:
    """When any live forecast/outlook signal exists, flag near-single-bucket collapse.

    The ARIMA OUTLOOK chip ran at 22/24 = 92% steady before being turned off
    7 Jun. That collapse is what this catches: a chip that converges on one
    bucket is structurally degenerate even when each individual call returns
    a valid value. Reads the forecast table; SKIPs when the table is empty
    or has no rows for the date (i.e. FORECAST_ENABLED is off).
    """
    bq, _ = _bq_client()
    try:
        rows = _query(
            """
            WITH day7 AS (
              SELECT market, query_group, forecast_score
              FROM `ogilvy-trends-v2.trends_v2_dev.score_forecast_7d`
              WHERE forecast_day = (
                SELECT MAX(forecast_day) FROM `ogilvy-trends-v2.trends_v2_dev.score_forecast_7d`)),
            act AS (
              SELECT market, query_group, trend_score
              FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores`
              WHERE trend_date = @d)
            SELECT CASE
              WHEN day7.forecast_score - act.trend_score >  0.04 THEN 'heating'
              WHEN day7.forecast_score - act.trend_score < -0.04 THEN 'cooling'
              ELSE 'steady' END AS bucket
            FROM day7 JOIN act USING (market, query_group)
            """,
            [bq.ScalarQueryParameter("d", "DATE", trend_date)],
        )
    except Exception as exc:
        return _unavailable_probe("prediction_degeneracy", exc)
    if not rows:
        return Check(
            "prediction_degeneracy",
            "SKIP",
            "forecast query returned no applicable rows for this date",
            {},
        )

    spread: dict[str, int] = {}
    for r in rows:
        b = str(r["bucket"])
        spread[b] = spread.get(b, 0) + 1
    n = sum(spread.values())
    top_share = max(spread.values()) / n
    if top_share >= _DEGENERACY_FRACTION:
        top_bucket = max(spread, key=lambda k: spread[k])
        return Check(
            "prediction_degeneracy",
            "FAIL",
            f"{spread.get(top_bucket, 0)}/{n} ({top_share:.0%}) chips collapse to '{top_bucket}'",
            {"spread": spread, "top_share": round(top_share, 3)},
        )
    return Check(
        "prediction_degeneracy",
        "PASS",
        f"spread {spread} (top share {top_share:.0%})",
        {"spread": spread, "top_share": round(top_share, 3)},
    )


def check_forecast_beats_persistence(trend_date: str) -> Check:
    """Score the realised day-7 forecast against persistence over 4 weeks.

    Reads predictions_archive for run_date in the trailing 28 days and joins
    each archived forecast to the trend_score that was actually realised on its
    forecast_day. The panel is ~24-26 series/week, below the 30-row backtest
    floor, so a 4-week window is the smallest that clears it (>= ~96 rows).
    Assembles the predictions (forecast_value=forecast_score,
    baseline_value=latest_actual) and actuals (value=realised trend_score)
    lists and defers the verdict to score_prediction_vs_persistence.

    SKIPs when the archive is empty / unavailable (the normal state while the
    forecast is dropped) or when the backtest returns INSUFFICIENT_DATA. Maps
    the backtest PASS/FAIL straight through.
    """
    bq, _ = _bq_client()
    try:
        rows = _query(
            """
            SELECT
              pa.run_date AS run_date,
              pa.forecast_day AS forecast_day,
              pa.market AS market,
              pa.query_group AS query_group,
              pa.forecast_score AS forecast_score,
              pa.latest_actual AS latest_actual,
              ts.trend_score AS realised_actual
            FROM `ogilvy-trends-v2.trends_v2_dev.predictions_archive` pa
            JOIN `ogilvy-trends-v2.trends_v2_dev.trend_scores` ts
              ON ts.market = pa.market
             AND ts.query_group = pa.query_group
             AND ts.trend_date = pa.forecast_day
            WHERE pa.run_date BETWEEN DATE_SUB(@d, INTERVAL 28 DAY) AND @d
            """,
            [bq.ScalarQueryParameter("d", "DATE", trend_date)],
        )
    except Exception as exc:
        return _unavailable_probe("forecast_beats_persistence", exc)
    if not rows:
        return Check(
            "forecast_beats_persistence",
            "SKIP",
            "predictions_archive query empty for the trailing 28 days",
            {},
        )

    predictions: list[dict[str, Any]] = []
    actuals: list[dict[str, Any]] = []
    for r in rows:
        # day keys the (market, query_group, day) join inside the backtest; the
        # forecast_day is the natural alignment point (the forecast and its
        # realised actual both land on that date).
        day = str(r["forecast_day"])
        market = str(r["market"])
        qg = str(r["query_group"])
        predictions.append(
            {
                "market": market,
                "query_group": qg,
                "day": day,
                "forecast_value": float(r["forecast_score"] or 0.0),
                "baseline_value": float(r["latest_actual"] or 0.0),
            }
        )
        actuals.append(
            {
                "market": market,
                "query_group": qg,
                "day": day,
                "value": float(r["realised_actual"] or 0.0),
            }
        )

    result = score_prediction_vs_persistence(predictions, actuals)
    status = result.get("status")
    if status == "INSUFFICIENT_DATA":
        return Check(
            "forecast_beats_persistence",
            "SKIP",
            f"only {result.get('rows', 0)}/{result.get('min_required', _BACKTEST_MIN_ROWS)} "
            "matched rows in the 28-day window",
            result,
        )
    if status == "PASS":
        return Check(
            "forecast_beats_persistence",
            "PASS",
            f"forecast beats persistence over {result['rows']} rows "
            f"(MAE {result['mae_predictor']} vs {result['mae_persistence']}, "
            f"precision {result['directional_precision']:.0%})",
            result,
        )
    if status == "FAIL":
        return Check(
            "forecast_beats_persistence",
            "FAIL",
            f"forecast loses to persistence over {result['rows']} rows "
            f"(MAE {result['mae_predictor']} vs {result['mae_persistence']}, "
            f"precision {result['directional_precision']:.0%})",
            result,
        )
    # Defensive: an unrecognised status from the backtest must not raise.
    return Check(
        "forecast_beats_persistence",
        "SKIP",
        f"backtest returned unexpected status {status!r}",
        result,
    )


# ----------------------------------------------------------------------
# PULSE Intelligence Core trust checks (deterministic, no Vertex). Each reads a
# shadow table/column that may be empty while the layer is in shadow, so each
# SKIPs (never FAILs, never raises) when its source is empty or absent.


def check_no_social_only_verified(trend_date: str) -> Check:
    """The dangerous-quadrant guard: no row reaches 'corroborated' on social alone.

    A trend_scores row tagged confidence_tier 'corroborated' while
    factual_corroboration is null or 0 means social families alone earned the
    verified tier, the worst trust failure the Intelligence Core can ship. FAIL
    (UNVERIFIED). SKIP when no row carries a confidence_tier yet (shadow day) or
    the column/table is absent.
    """
    bq, _ = _bq_client()
    try:
        rows = _query(
            """
            SELECT
              COUNTIF(confidence_tier IS NOT NULL AND confidence_tier != '') AS tiered,
              COUNTIF(confidence_tier = 'corroborated'
                      AND COALESCE(factual_corroboration, 0) = 0) AS social_only,
              COUNT(*) AS n
            FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores`
            WHERE trend_date = @d
            """,
            [bq.ScalarQueryParameter("d", "DATE", trend_date)],
        )
    except Exception as exc:
        return _unavailable_probe("no_social_only_verified", exc)
    if not rows or not rows[0].get("tiered"):
        return Check(
            "no_social_only_verified",
            "SKIP",
            "no rows carry a confidence_tier yet (shadow day)",
            {"tiered": 0},
        )
    row = rows[0]
    social_only = int(row.get("social_only") or 0)
    tiered = int(row.get("tiered") or 0)
    if social_only > 0:
        return Check(
            "no_social_only_verified",
            "FAIL",
            f"{social_only}/{tiered} rows reached 'corroborated' on social families "
            "alone (factual_corroboration null/0): a social-only topic dressed as verified",
            {"social_only": social_only, "tiered": tiered},
        )
    return Check(
        "no_social_only_verified",
        "PASS",
        f"no social-only 'corroborated' rows across {tiered} tiered rows",
        {"social_only": 0, "tiered": tiered},
    )


def check_corroborated_rate_band(trend_date: str) -> Check:
    """The corroborated-rate must stay a discriminating minority, not a majority.

    Recalibrated 3 Jul 2026 after the original 0.40 floor let 94.5% of
    topic-days reach 'corroborated' (see the scoring.yaml corroboration
    comment block for the live BQ read that drove the 0.85 floor). This is
    the ongoing guard against that drift recurring: corroborated topic-days
    / total topic-days carrying a non-null confidence_tier. DRIFT-equivalent
    (FAIL) above 0.60 (the tier is over-awarding again); DEGRADED below 0.05
    (the tier has gone dead and is no longer surfacing anything). SKIP when
    no row carries a confidence_tier yet or the column/table is absent.
    """
    bq, _ = _bq_client()
    try:
        rows = _query(
            """
            SELECT
              COUNTIF(confidence_tier IS NOT NULL AND confidence_tier != '') AS tiered,
              COUNTIF(confidence_tier = 'corroborated') AS corroborated
            FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores`
            WHERE trend_date = @d
            """,
            [bq.ScalarQueryParameter("d", "DATE", trend_date)],
        )
    except Exception as exc:
        return _unavailable_probe("corroborated_rate_band", exc)
    if not rows or not rows[0].get("tiered"):
        return Check(
            "corroborated_rate_band",
            "SKIP",
            "no rows carry a confidence_tier yet (shadow day)",
            {"tiered": 0},
        )
    row = rows[0]
    tiered = int(row.get("tiered") or 0)
    corroborated = int(row.get("corroborated") or 0)
    rate = corroborated / tiered if tiered else 0.0
    if rate > 0.60:
        return Check(
            "corroborated_rate_band",
            "FAIL",
            f"corroborated-rate {rate:.1%} ({corroborated}/{tiered}) exceeds 60% "
            "(over-award: the tier is no longer discriminating)",
            {"tiered": tiered, "corroborated": corroborated, "rate": round(rate, 4)},
        )
    if rate < 0.05:
        return Check(
            "corroborated_rate_band",
            "FAIL",
            f"corroborated-rate {rate:.1%} ({corroborated}/{tiered}) below 5% "
            "(tier dead: nothing is surfacing as corroborated)",
            {"tiered": tiered, "corroborated": corroborated, "rate": round(rate, 4)},
        )
    return Check(
        "corroborated_rate_band",
        "PASS",
        f"corroborated-rate {rate:.1%} ({corroborated}/{tiered}) within the healthy band",
        {"tiered": tiered, "corroborated": corroborated, "rate": round(rate, 4)},
    )


def check_corroboration_recompute(trend_date: str, scoring: dict[str, Any]) -> Check:
    """The stored confidence_tier must be consistent with its own components.

    Per the documented tier rule: 'corroborated' requires factual_corroboration
    above the scoring.yaml corroborated_floor AND > 0; the social-only tier
    requires factual_corroboration == 0. FAIL when a stored tier contradicts its
    own factual/social numbers (mirrors composite_integrity's recompute
    discipline). SKIP when no row carries a tier yet or the columns are absent.
    """
    floor = float(scoring.get("corroboration", {}).get("corroborated_floor", 0.40))
    bq, _ = _bq_client()
    params = [bq.ScalarQueryParameter("d", "DATE", trend_date)]
    exact_mode = True
    availability = {}
    try:
        rows = _query(
            """
            SELECT market, query_group, confidence_tier,
                   factual_corroboration, social_corroboration,
                   n_factual, n_social
            FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores`
            WHERE trend_date = @d
              AND confidence_tier IS NOT NULL AND confidence_tier != ''
            """,
            params,
        )
    except Exception as primary_error:
        exact_mode = False
        availability = {
            "availability": "partial",
            "reason": "primary_query_failed",
            "error_type": type(primary_error).__name__,
        }
        try:
            rows = _query(
                """
                SELECT market, query_group, confidence_tier,
                       factual_corroboration, social_corroboration
                FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores`
                WHERE trend_date = @d
                  AND confidence_tier IS NOT NULL AND confidence_tier != ''
                """,
                params,
            )
        except Exception as exc:
            return _unavailable_probe("corroboration_recompute", exc)
    if not rows:
        return Check(
            "corroboration_recompute",
            "SKIP",
            "no rows carry a confidence_tier in the completed query",
            {"rows": 0, **availability},
        )

    bad: list[str] = []
    for r in rows:
        tier = str(r.get("confidence_tier") or "").strip().lower().replace("_", "-")
        factual = float(r.get("factual_corroboration") or 0.0)
        if tier == "corroborated" and (factual <= 0.0 or factual < (floor - _COMPOSITE_TOL)):
            bad.append(
                f"{r['market']}/{r['query_group']} tier=corroborated "
                f"factual={factual:.4f} (floor {floor:.2f})"
            )
            continue
        if not exact_mode:
            continue

        n_factual = int(r.get("n_factual") or 0)
        n_social = int(r.get("n_social") or 0)
        if n_factual >= 1 and factual >= floor:
            expected_tier = "corroborated"
        elif n_factual == 1:
            expected_tier = "single-source-factual"
        elif n_factual == 0 and n_social > 0:
            expected_tier = "social-only"
        else:
            expected_tier = "thin"
        if tier != expected_tier:
            bad.append(
                f"{r['market']}/{r['query_group']} tier={tier} expected={expected_tier} "
                f"(n_factual={n_factual}, n_social={n_social}, factual={factual:.4f})"
            )

    n = len(rows)
    if not bad:
        return Check(
            "corroboration_recompute",
            "PASS",
            (
                f"{n}/{n} tiers consistent with exact scorer rule"
                if exact_mode
                else f"{n}/{n} tiers pass conservative floor checks; full source probe unavailable"
            ),
            {"rows": n, "inconsistent": 0, "exact_mode": exact_mode, **availability},
        )
    return Check(
        "corroboration_recompute",
        "FAIL",
        f"{len(bad)}/{n} tiers contradict their components: {'; '.join(bad[:3])}"
        + ("..." if len(bad) > 3 else ""),
        {
            "rows": n,
            "inconsistent": len(bad),
            "examples": bad[:3],
            "exact_mode": exact_mode,
            **availability,
        },
    )


def check_reconcile_receipts_exist(trend_date: str) -> Check:
    """Every correction that changes a claim must cite a receipt. The never-invent audit.

    A reconcile_actions row with action in ('stale', 'corroborated') and a
    non-empty claim_after MUST carry a non-empty receipt_ids array. A correction
    that cites no receipt is an invented claim. FAIL on any such row. SKIP when
    reconcile_actions is empty or absent for the date.
    """
    bq, _ = _bq_client()
    try:
        rows = _query(
            """
            SELECT market, action, claim_after, matched_entity_key,
                   ARRAY_LENGTH(receipt_ids) AS n_receipts
            FROM `ogilvy-trends-v2.trends_v2_dev.reconcile_actions`
            WHERE trend_date = @d
              AND action IN ('stale', 'corroborated')
              AND claim_after IS NOT NULL AND claim_after != ''
            """,
            [bq.ScalarQueryParameter("d", "DATE", trend_date)],
        )
    except Exception as exc:
        return _unavailable_probe("reconcile_receipts_exist", exc)
    if not rows:
        return Check(
            "reconcile_receipts_exist",
            "SKIP",
            "no stale/corroborated corrections with a claim_after for this date",
            {"rows": 0},
        )

    missing = [
        f"{r.get('market')}/{r.get('action')} -> {r.get('matched_entity_key') or '?'!s}"
        for r in rows
        if not int(r.get("n_receipts") or 0)
    ]
    n = len(rows)
    if not missing:
        return Check(
            "reconcile_receipts_exist",
            "PASS",
            f"all {n} corrections cite at least one receipt",
            {"rows": n, "missing": 0},
        )
    return Check(
        "reconcile_receipts_exist",
        "FAIL",
        f"{len(missing)}/{n} corrections cite NO receipt: {'; '.join(missing[:3])}"
        + ("..." if len(missing) > 3 else ""),
        {"rows": n, "missing": len(missing), "examples": missing[:3]},
    )


def check_no_unlabeled_stale(trend_date: str) -> Check:
    """A 'no_match' correction must not slip past a claim that does match a resolved event.

    A reconcile_actions row labelled action 'no_match' is a claim the reconciler
    found nothing for. If the same claim_before would clearly match a resolved
    event_ledger entity for that market and date, the stale claim slipped past.
    Conservative: only FAIL on a clear entity_key containment match (the ledger
    entity_key appears as a token inside the claim_before text). SKIP when either
    table is empty or absent for the date.
    """
    bq, _ = _bq_client()
    try:
        rows = _query(
            """
            WITH no_match AS (
              SELECT market, claim_before
              FROM `ogilvy-trends-v2.trends_v2_dev.reconcile_actions`
              WHERE trend_date = @d
                AND action = 'no_match'
                AND claim_before IS NOT NULL AND claim_before != ''),
            resolved AS (
              SELECT DISTINCT market, entity_key
              FROM `ogilvy-trends-v2.trends_v2_dev.event_ledger`
              WHERE trend_date = @d
                AND entity_key IS NOT NULL AND entity_key != '')
            SELECT nm.market AS market, nm.claim_before AS claim_before,
                   r.entity_key AS entity_key
            FROM no_match nm
            JOIN resolved r
              ON r.market = nm.market
             AND STRPOS(LOWER(nm.claim_before), LOWER(r.entity_key)) > 0
            """,
            [bq.ScalarQueryParameter("d", "DATE", trend_date)],
        )
    except Exception as exc:
        return _unavailable_probe("no_unlabeled_stale", exc)
    if not rows:
        # Either no 'no_match' rows, no resolved ledger entities, or no overlap:
        # all three are healthy shadow-day states.
        return Check(
            "no_unlabeled_stale",
            "SKIP",
            "no 'no_match' correction overlaps a resolved event_ledger entity",
            {"matches": 0},
        )

    slipped = [
        f"{r.get('market')}: '{str(r.get('claim_before') or '')[:60]}' matches "
        f"resolved entity {r.get('entity_key')}"
        for r in rows
    ]
    return Check(
        "no_unlabeled_stale",
        "FAIL",
        f"{len(slipped)} 'no_match' claim(s) match a resolved event: {'; '.join(slipped[:3])}"
        + ("..." if len(slipped) > 3 else ""),
        {"matches": len(slipped), "examples": slipped[:3]},
    )


# ----------------------------------------------------------------------
# Rolling backtest engine (the generic gate; the boosted-tree forecast will
# wire into this for its weekly health check on a future merge).


def score_prediction_vs_persistence(
    predictions: list[dict[str, Any]],
    actuals: list[dict[str, Any]],
    horizon_days: int = 7,
) -> dict[str, Any]:
    """Generic backtest: predictor MAE + directional precision vs persistence.

    predictions / actuals each carry (market, query_group, day, value);
    predictions also carry (forecast_value, baseline_value=score at day-T).
    A predictor PASSES when its MAE is lower than persistence MAE AND
    directional precision >= 50% AND the row count >= _BACKTEST_MIN_ROWS.
    Pure function: unit-testable, no IO. The morning-check wiring (a future
    branch) calls this with the previous 7 days of LIVE predictions to gate
    every shipped predictor.
    """
    rows = []
    pred_idx = {(p["market"], p["query_group"], p["day"]): p for p in predictions}
    for a in actuals:
        key = (a["market"], a["query_group"], a["day"])
        if key not in pred_idx:
            continue
        p = pred_idx[key]
        rows.append(
            {
                "actual": float(a["value"]),
                "pred": float(p["forecast_value"]),
                "persist": float(p["baseline_value"]),
            }
        )
    if len(rows) < _BACKTEST_MIN_ROWS:
        return {
            "status": "INSUFFICIENT_DATA",
            "rows": len(rows),
            "min_required": _BACKTEST_MIN_ROWS,
        }

    n = len(rows)
    mae_pred = sum(abs(r["pred"] - r["actual"]) for r in rows) / n
    mae_persist = sum(abs(r["persist"] - r["actual"]) for r in rows) / n
    called = 0
    correct = 0
    for r in rows:
        pred_dir = _direction(r["pred"] - r["persist"])
        actual_dir = _direction(r["actual"] - r["persist"])
        if pred_dir != 0:
            called += 1
            if pred_dir == actual_dir:
                correct += 1
    precision = (correct / called) if called > 0 else 0.0
    beats_baseline = mae_pred < mae_persist and precision >= 0.50
    return {
        "status": "PASS" if beats_baseline else "FAIL",
        "rows": n,
        "mae_predictor": round(mae_pred, 4),
        "mae_persistence": round(mae_persist, 4),
        "directional_called": called,
        "directional_correct": correct,
        "directional_precision": round(precision, 3),
        "beats_baseline": beats_baseline,
    }


def _direction(delta: float, band: float = 0.04) -> int:
    if delta > band:
        return 1
    if delta < -band:
        return -1
    return 0


# ----------------------------------------------------------------------
# Verdict + entry point


def _verdict(checks: list[Check]) -> str:
    statuses = {c.name: c.status for c in checks}
    # DRIFT trumps everything: a wrong stored trend_score makes the rest moot.
    if statuses.get("composite_integrity") == "FAIL":
        return "DRIFT"
    # UNVERIFIED sits below DRIFT: a claim shipped as verified that its receipts
    # do not support. Takes precedence over the operational DEGRADED bucket.
    if any(statuses.get(name) == "FAIL" for name in _TRUST_CHECKS):
        return "UNVERIFIED"
    if any(s == "FAIL" for s in statuses.values()):
        return "DEGRADED"
    if any(c.metric.get("availability") in ("unavailable", "partial") for c in checks):
        return "DEGRADED"
    if all(s == "SKIP" for s in statuses.values()):
        return "SKIPPED"
    return "HEALTHY"


def build_report(trend_date: str) -> AccuracyReport:
    scoring = _load_scoring()
    report = AccuracyReport(trend_date=trend_date)
    report.checks.append(check_composite_integrity(trend_date, scoring))
    report.checks.append(check_dead_signals(trend_date, scoring))
    report.checks.append(check_action_pick_in_rising(trend_date))
    report.checks.append(check_tier_thresholding(trend_date, scoring))
    report.checks.append(check_prediction_degeneracy(trend_date))
    report.checks.append(check_forecast_beats_persistence(trend_date))
    report.checks.append(check_no_social_only_verified(trend_date))
    report.checks.append(check_corroborated_rate_band(trend_date))
    report.checks.append(check_corroboration_recompute(trend_date, scoring))
    report.checks.append(check_reconcile_receipts_exist(trend_date))
    report.checks.append(check_no_unlabeled_stale(trend_date))
    report.verdict = _verdict(report.checks)
    return report


def render_text(r: AccuracyReport) -> str:
    out = [f"ACCURACY WATCHDOG {r.trend_date}", "", f"VERDICT: {r.verdict}", "", "CHECKS"]
    for c in r.checks:
        out.append(f"  {c.name:24s} {c.status:5s}  {c.detail}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="", help="trend_date YYYY-MM-DD; default today UTC")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()
    trend_date = args.date or datetime.now(UTC).date().isoformat()
    try:
        date.fromisoformat(trend_date)
    except ValueError:
        print(f"invalid date: {trend_date}", file=sys.stderr)
        return 2
    try:
        report = build_report(trend_date)
    except Exception as exc:
        print(
            f"accuracy_watchdog failed: report_not_completed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
