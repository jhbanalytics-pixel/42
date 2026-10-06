"""Bridge Wave 1 continuity + lifecycle reads from trend_scores onto briefs.

The continuity counter (new / day2 / day3plus / rebounding) and the lifecycle
phase (birth / growth / maturity / decline) are both computed in
``scripts/run_rss_now.py:compute_trend_scores`` and stored on the day's
``trend_scores`` rows (columns ``continuity_state`` / ``continuity_day`` /
``lifecycle_phase``), gated by ``CONTINUITY_BADGES_ENABLED`` /
``LIFECYCLE_ENABLED``. The PULSE mailer card reads those values off the brief
dict (``brief["continuity_state"]`` etc), but nothing carried them from
trend_scores onto the briefs, so the badges never lit even with the flags on.

This module closes that gap with the same compute-then-tag shape the forecast
and comment-sentiment producers use: ``fetch_continuity_lifecycle`` reads the
stored columns for one date, ``tag_briefs_with_continuity_lifecycle`` stamps the
fields onto each brief in place. The render (card.py / __init__.py) is gated by
its own flag and drops cleanly when a field is absent, so this stage is purely
additive: with both flags off it tags nothing and the email is byte-identical.

Non-fatal by contract: any BigQuery failure logs and yields an empty lookup, so
the daily pipeline and the email are never blocked by it.
"""

from __future__ import annotations

import datetime

from google.cloud import bigquery

from src.utils.bigquery import get_client, get_dataset
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

# The continuity states the card render knows how to badge. A value outside this
# set is dropped here so a future producer-side rename cannot ship an unknown
# badge to stakeholders; the render also allow-lists, this is belt-and-braces.
_VALID_CONTINUITY_STATES = frozenset({"new", "day2", "day3plus", "rebounding"})
# The lifecycle phases the card render knows how to badge.
_VALID_LIFECYCLE_PHASES = frozenset({"birth", "growth", "maturity", "decline"})


def fetch_continuity_lifecycle(
    trend_date: datetime.date,
) -> dict[tuple[str, str], dict]:
    """Read the stored continuity + lifecycle columns for one date.

    Returns ``{(market, query_group): {"continuity_state", "continuity_day",
    "lifecycle_phase"}}`` for every today row that carries at least one of the
    three values. A row whose columns are all NULL (the flags were off when it
    was scored) is omitted, so the tagger leaves those briefs untouched.

    Non-fatal: any failure logs and returns ``{}`` so the caller carries on and
    the badges simply do not light.
    """
    try:
        client = get_client()
        dataset = get_dataset()
        sql = f"""
        SELECT market, query_group, continuity_state, continuity_day, lifecycle_phase
        FROM `{client.project}.{dataset}.trend_scores`
        WHERE trend_date = @trend_date
          AND (continuity_state IS NOT NULL OR lifecycle_phase IS NOT NULL)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date)]
        )
        out: dict[tuple[str, str], dict] = {}
        for r in client.query(sql, job_config=job_config).result():
            state = str(r.continuity_state or "").strip().lower()
            phase = str(r.lifecycle_phase or "").strip().lower()
            day = None
            if r.continuity_day is not None:
                try:
                    day = int(r.continuity_day)
                except (TypeError, ValueError):
                    day = None
            out[(str(r.market), str(r.query_group))] = {
                "continuity_state": state if state in _VALID_CONTINUITY_STATES else "",
                "continuity_day": day,
                "lifecycle_phase": phase if phase in _VALID_LIFECYCLE_PHASES else "",
            }
        logger.info("Continuity/lifecycle read for %d series on %s", len(out), trend_date)
        return out
    except Exception as exc:  # non-fatal by contract
        logger.error("Continuity/lifecycle read failed (non-fatal): %s", exc, exc_info=True)
        return {}


def tag_briefs_with_continuity_lifecycle(
    briefs_by_topic: dict[tuple[str, str], dict],
    lookup: dict[tuple[str, str], dict],
) -> int:
    """Tag each brief in place with its continuity + lifecycle fields.

    Both sides key on (market, query_group). A brief whose key is absent from
    ``lookup`` (or carries only empty values) is left untouched, so a key
    mismatch or a flag-off day degrades to no-badge rather than an error.
    Returns the count of briefs that received at least one field.

    The fields are set as top-level brief keys (``continuity_state`` /
    ``continuity_day`` / ``lifecycle_phase``) to match what the card render
    reads, mirroring how ``forecast_outlook`` is carried.
    """
    tagged = 0
    for key, brief in briefs_by_topic.items():
        entry = lookup.get(key)
        if not entry:
            continue
        touched = False
        state = entry.get("continuity_state") or ""
        if state:
            brief["continuity_state"] = state
            day = entry.get("continuity_day")
            if isinstance(day, int) and not isinstance(day, bool):
                brief["continuity_day"] = day
            touched = True
        phase = entry.get("lifecycle_phase") or ""
        if phase:
            brief["lifecycle_phase"] = phase
            touched = True
        if touched:
            tagged += 1
    return tagged


def fetch_seed_scores(
    trend_date: datetime.date,
) -> dict[tuple[str, str], float]:
    """Read the stored seed_score for one date.

    Returns ``{(market, query_group): seed_score}`` for every today row that
    carries a seed_score in the valid 0..1 range. A row with NULL or an
    out-of-range value is omitted, so the tagger leaves those briefs untouched.

    Non-fatal: any failure logs and returns ``{}`` so the caller carries on and
    the SEED chip simply does not light. Same shape as
    ``fetch_continuity_lifecycle``.
    """
    try:
        client = get_client()
        dataset = get_dataset()
        sql = f"""
        SELECT market, query_group, seed_score
        FROM `{client.project}.{dataset}.trend_scores`
        WHERE trend_date = @trend_date
          AND seed_score IS NOT NULL
          AND seed_score > 0
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date)]
        )
        out: dict[tuple[str, str], float] = {}
        for r in client.query(sql, job_config=job_config).result():
            try:
                score = float(r.seed_score)
            except (TypeError, ValueError):
                continue
            if 0.0 <= score <= 1.0:
                out[(str(r.market), str(r.query_group))] = score
        logger.info("Seed scores read for %d series on %s", len(out), trend_date)
        return out
    except Exception as exc:  # non-fatal by contract
        logger.error("Seed score read failed (non-fatal): %s", exc, exc_info=True)
        return {}


def tag_briefs_with_seed_score(
    briefs_by_topic: dict[tuple[str, str], dict],
    lookup: dict[tuple[str, str], float],
) -> int:
    """Tag each brief in place with its seed_score.

    Both sides key on (market, query_group). A brief whose key is absent from
    ``lookup`` is left untouched, so a key mismatch or a disabled day degrades to
    no-chip rather than an error. Returns the count of briefs tagged.

    The value is set as the top-level ``seed_score`` brief key to match what the
    card render reads, mirroring how ``forecast_outlook`` is carried.
    """
    tagged = 0
    for key, brief in briefs_by_topic.items():
        score = lookup.get(key)
        if score is None:
            continue
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            score_f = float(score)
            if 0.0 <= score_f <= 1.0:
                brief["seed_score"] = score_f
                tagged += 1
    return tagged


__all__ = [
    "fetch_continuity_lifecycle",
    "fetch_seed_scores",
    "tag_briefs_with_continuity_lifecycle",
    "tag_briefs_with_seed_score",
]
