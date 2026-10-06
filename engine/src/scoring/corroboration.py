"""Deterministic corroboration scorer (PULSE Intelligence Core, Phase 0).

Produces TWO honest, separate corroboration numbers per (market, topic): one
for factual channels (news, search, GDELT entities, YouTube, music) and one for
social channels (TikTok/IG/Threads via Ensemble, Reddit, Brand24). The rule is
two numbers, never one combined score: a topic confirmed only on social
channels is never the same thing as one confirmed by independent news and
search, and collapsing them into a single figure hides exactly the distinction
a strategist needs.

Everything here is pure: no BigQuery, no Vertex, no genai. It reads only data
already aggregated in run_rss_now._aggregate_by_topic plus a config block, so it
unit-tests without any external dependency. The output is inert (shadow) by
design: the caller writes the four returned values into new trend_scores columns
and changes nothing about trend_score itself.

confidence_tier deliberately avoids the words "verified" and "confirmed". Those
read as a guarantee we cannot make from breadth-of-coverage alone. The tiers are
"corroborated", "single-source-factual", "social-only", and "thin". A
social-only topic can never reach "corroborated", however many social channels
back it, because social breadth is not factual corroboration.
"""

from __future__ import annotations

import math

# Channel families that count as FACTUAL corroboration. These are the same
# family names _channel_family emits in run_rss_now. Anything not in this set is
# treated as social, so a new family defaults to social (the conservative side:
# it can never manufacture a "corroborated" tier on its own).
FACTUAL_FAMILIES = frozenset({"news", "search", "youtube", "music"})

# A large sentinel returned for corroboration_recency_hours when no row carried a
# usable published_at. Keeps the column an honest "we do not know how fresh this
# is" rather than a misleading 0.
RECENCY_UNKNOWN_HOURS = 999_999

_DEFAULTS = {
    # Breadth: each present family adds this, capped at 1.0 before the other
    # terms. One factual family is already a real (if thin) corroboration, so the
    # first family alone should not read as near-zero.
    "factual_family_weight": 0.34,
    "social_family_weight": 0.25,
    # Factual-only bonuses, added on top of breadth (pre-recency, pre-clamp).
    # GDELT entity presence (tone_rows > 0) is an independent factual signal.
    "factual_entity_bonus": 0.15,
    # A real search spike (search_velocity above the floor) is a second
    # independent factual signal: people are actively looking this up.
    "factual_search_bonus": 0.15,
    "factual_search_spike_floor": 0.30,
    # Recency: the breadth+bonus score is multiplied by a recency factor that
    # decays with an exponential half-life. A topic whose freshest row is one
    # half-life old keeps half its corroboration weight. None published_at maps
    # to a neutral 0.5 factor (we neither reward nor punish unknown freshness).
    "recency_half_life_hours": 48.0,
    "recency_neutral_factor": 0.5,
    # Tier gate: "corroborated" needs factual_corroboration at or above this AND
    # at least one factual family. Social channels alone never clear it.
    "corroborated_floor": 0.40,
}


def _load_cfg(config: dict | None) -> dict:
    """Merge the caller's scoring['corroboration'] block over the defaults so a
    partial or absent config still yields every constant."""
    cfg = dict(_DEFAULTS)
    if config:
        block = config.get("corroboration") if "corroboration" in config else config
        if isinstance(block, dict):
            for key in _DEFAULTS:
                if block.get(key) is not None:
                    cfg[key] = block[key]
    return cfg


def _recency_hours(freshest_published_at, now) -> int:
    """Whole hours between the freshest published_at and now. None / unparseable
    -> the unknown sentinel. Negative (a future-stamped row) is clamped to 0."""
    if freshest_published_at is None or now is None:
        return RECENCY_UNKNOWN_HOURS
    try:
        delta = now - freshest_published_at
        hours = delta.total_seconds() / 3600.0
    except (TypeError, AttributeError, ValueError):
        return RECENCY_UNKNOWN_HOURS
    if math.isnan(hours):
        return RECENCY_UNKNOWN_HOURS
    return int(max(hours, 0.0))


def _recency_factor(recency_hours: int, freshest_published_at, cfg: dict) -> float:
    """Exponential half-life decay on the corroboration weight. Unknown
    freshness (no published_at) returns the configured neutral factor."""
    if freshest_published_at is None or recency_hours >= RECENCY_UNKNOWN_HOURS:
        return float(cfg["recency_neutral_factor"])
    half_life = float(cfg["recency_half_life_hours"])
    if half_life <= 0:
        return 1.0
    return float(0.5 ** (recency_hours / half_life))


def compute_corroboration(
    channel_family_weights: dict | None,
    freshest_published_at,
    now,
    search_velocity,
    tone_rows,
    config: dict | None,
) -> dict:
    """Compute the two corroboration numbers, a confidence tier, and a recency.

    Args:
        channel_family_weights: dict[family -> weight] from _aggregate_by_topic.
            A family counts as present only when its weight > 0.
        freshest_published_at: the max published_at across the topic's rows, or
            None when no row carried a usable timestamp.
        now: the current time (tz-aware), used for the recency delta.
        search_velocity: the topic's search_velocity_avg (0..1), a real spike
            when above the configured floor.
        tone_rows: count of rows that contributed a GDELT V2Tone read. >0 means
            GDELT entities are present, an independent factual signal.
        config: the scoring config; reads its optional 'corroboration' block.

    Returns:
        {factual_corroboration, social_corroboration, confidence_tier,
         corroboration_recency_hours}. The two scores are bounded 0..1 and
        computed SEPARATELY (never combined).
    """
    cfg = _load_cfg(config)
    weights = channel_family_weights or {}

    factual_present = [
        fam for fam, wt in weights.items() if fam in FACTUAL_FAMILIES and (wt or 0.0) > 0
    ]
    social_present = [
        fam for fam, wt in weights.items() if fam not in FACTUAL_FAMILIES and (wt or 0.0) > 0
    ]
    n_factual = len(factual_present)
    n_social = len(social_present)

    recency_hours = _recency_hours(freshest_published_at, now)
    recency_factor = _recency_factor(recency_hours, freshest_published_at, cfg)

    # FACTUAL: breadth across factual families, plus a GDELT-entity bonus and a
    # real-search-spike bonus, all modulated by recency. Bonuses only apply when
    # at least one factual family is present (a bonus with no factual breadth
    # behind it would be corroboration from nothing).
    factual_raw = n_factual * float(cfg["factual_family_weight"])
    if n_factual > 0:
        if (tone_rows or 0) > 0:
            factual_raw += float(cfg["factual_entity_bonus"])
        if float(search_velocity or 0.0) >= float(cfg["factual_search_spike_floor"]):
            factual_raw += float(cfg["factual_search_bonus"])
    factual_corroboration = _clamp01(factual_raw * recency_factor)

    # SOCIAL: breadth across social families, modulated by the same recency.
    # Computed independently of the factual score (no shared term).
    social_raw = n_social * float(cfg["social_family_weight"])
    social_corroboration = _clamp01(social_raw * recency_factor)

    confidence_tier = _confidence_tier(
        n_factual=n_factual,
        n_social=n_social,
        factual_corroboration=factual_corroboration,
        cfg=cfg,
    )

    return {
        "factual_corroboration": round(factual_corroboration, 4),
        "social_corroboration": round(social_corroboration, 4),
        "confidence_tier": confidence_tier,
        "corroboration_recency_hours": recency_hours,
        "n_factual": n_factual,
        "n_social": n_social,
    }


def _confidence_tier(
    n_factual: int,
    n_social: int,
    factual_corroboration: float,
    cfg: dict,
) -> str:
    """Pick the confidence tier. Social channels alone never reach
    "corroborated": the gate requires at least one factual family."""
    floor = float(cfg["corroborated_floor"])
    if n_factual >= 1 and factual_corroboration >= floor:
        return "corroborated"
    if n_factual == 1:
        return "single-source-factual"
    if n_factual == 0 and n_social > 0:
        return "social-only"
    return "thin"


def _clamp01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))
