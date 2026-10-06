"""Load persisted trend_analysis briefs into the email dict shape.

Single source for turning the day's persisted briefs back into the
``briefs_by_topic`` mapping that ``send_daily_digest`` consumes, so two paths
share it:

  - the resend ops job (scripts/ops/resend_email.py), which rebuilds the whole
    digest from BigQuery, and
  - the daily cron (scripts/run_rss_now.py), which supplements its in-memory
    set with topics that generate_briefs skipped because a partial prior run
    had already briefed them (BriefReport.skipped_existing). Without this the
    inline email renders a partial set and can miss a whole market even though
    BigQuery holds every brief (the 28 Jun incident dropped ZA this way).

The render_payload bundle is parsed back so a loaded brief renders identically
to the live email (forecast chip, continuity + lifecycle badges, tone split,
seed chip, the Seen-on display dict, comment fields, driving hashtags).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from typing import Any

from google.cloud import bigquery as bq

from src.utils.bigquery import get_client, get_dataset

# The full persisted brief shape. Column names already match the keys the PULSE
# v2 renderer reads, so the dicts carry the v2 contract natively; the v1 alias
# keys are added alongside so the legacy fallback path still binds.
_BRIEFS_SQL = """
SELECT
    market,
    query_group,
    trend_score,
    headline,
    trend_synthesis,
    cultural_context,
    campaign_angles,
    risk_flags,
    platforms,
    sentiment_summary,
    status_tag,
    visual_anchor,
    nano_banana_prompt,
    lyria_prompt,
    top_creators,
    social_refs,
    platform_counts,
    b24_sentiment_trajectory,
    render_payload
FROM `{ds_path}.trend_analysis`
WHERE trend_date = @trend_date
-- A forced regen (FORCE_INPUT) can leave more than one row per
-- (market, query_group); keep only the most recent so a resend renders the
-- latest brief deterministically instead of an arbitrary duplicate.
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY market, query_group ORDER BY analyzed_at DESC
) = 1
"""


def _row_to_entry(r: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "market": str(r.market),
        "query_group": str(r.query_group),
        "topic": str(r.query_group),
        "trend_score": float(r.trend_score or 0.0),
        "headline": r.headline or "",
        "trend_synthesis": r.trend_synthesis or "",
        "cultural_context": r.cultural_context or "",
        "key_metrics": list(r.campaign_angles or []),
        "risk_flags": list(r.risk_flags or []),
        "platforms": list(r.platforms or []),
        "sentiment_summary": r.sentiment_summary or "",
        "status_tag": r.status_tag or "Rising",
        "visual_anchor": r.visual_anchor or "",
        "nano_banana_prompt": r.nano_banana_prompt or "",
        "lyria_prompt": r.lyria_prompt or "",
        "top_creators": list(r.top_creators or []),
        "social_refs": list(r.social_refs or []),
        "platform_counts": list(r.platform_counts or []),
        "b24_sentiment_trajectory": r.b24_sentiment_trajectory or "",
        # Legacy v1 aliases (the fallback render reads these).
        "description_rationale": r.trend_synthesis or "",
        "activation_idea": r.cultural_context or "",
    }
    # The persisted render bundle: the display dict (Seen-on channels, state
    # badge, chips) plus the conversation + badge fields the producers tagged on
    # the live run. Parsed back so a loaded brief renders exactly as it did live.
    payload_raw = getattr(r, "render_payload", None)
    if payload_raw:
        try:
            payload = json.loads(payload_raw)
            display = payload.get("display")
            if isinstance(display, dict) and display:
                entry["display"] = display
            if payload.get("comment_sentiment"):
                entry["comment_sentiment"] = str(payload["comment_sentiment"])
            if payload.get("comment_opening"):
                entry["comment_opening"] = str(payload["comment_opening"])
            if payload.get("comment_themes"):
                entry["comment_themes"] = list(payload["comment_themes"])
            if payload.get("driving_hashtags"):
                entry["driving_hashtags"] = list(payload["driving_hashtags"])
            if payload.get("forecast_outlook"):
                entry["forecast_outlook"] = str(payload["forecast_outlook"])
            if payload.get("continuity_state"):
                entry["continuity_state"] = str(payload["continuity_state"])
            if payload.get("continuity_day") is not None:
                entry["continuity_day"] = payload["continuity_day"]
            if payload.get("lifecycle_phase"):
                entry["lifecycle_phase"] = str(payload["lifecycle_phase"])
            if payload.get("sentiment_lexicon_scores"):
                entry["sentiment_lexicon_scores"] = list(payload["sentiment_lexicon_scores"])
            if payload.get("seed_score") is not None:
                entry["seed_score"] = payload["seed_score"]
            if payload.get("seed_path"):
                entry["seed_path"] = payload["seed_path"]
        except (ValueError, TypeError) as exc:
            print(f"render_payload parse skipped for {r.market}/{r.query_group}: {exc}")
    return entry


def load_briefs_by_topic_from_bq(
    trend_date: date,
    keys: Iterable[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load persisted trend_analysis briefs for trend_date into the email dict
    shape used by send_daily_digest.

    keys restricts the result to those (market, query_group) pairs; None returns
    every persisted brief for the date.
    """
    bq_client = get_client()
    dataset = get_dataset()
    ds_path = f"{bq_client.project}.{dataset}"
    job = bq_client.query(
        _BRIEFS_SQL.format(ds_path=ds_path),
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
        ),
    )
    wanted = {(str(m), str(g)) for m, g in keys} if keys is not None else None
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in job.result():
        key = (str(r.market), str(r.query_group))
        if wanted is not None and key not in wanted:
            continue
        out[key] = _row_to_entry(r)
    return out
