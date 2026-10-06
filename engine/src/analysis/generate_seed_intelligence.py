"""Generate the day's seed intelligence: hidden seedable behaviours from the briefs.

A cross-topic Gemini pass (sibling to generate_daily_summary) that reads all of
the day's per-topic briefs and returns three to five cultural BEHAVIOURS worth
seeding a Nanobanana/Lyria activation around, each evidenced, timed, and with a
ready activation. Persists one row per behaviour to seed_insights.

Idempotent: skips when rows already exist for the date unless force=True.
Non-fatal by contract for the caller: any failure logs and returns []. Additive,
so it never blocks the briefs or the email.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from google.cloud import bigquery as bq

from src.analysis.gemini_client import GeminiClient
from src.analysis.prompts.seed_intelligence import (
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    build_seed_prompt,
)
from src.utils.bigquery import get_client, get_dataset, insert_dataframe
from src.utils.gemini_usage import (
    UsageSink,
    persist_gemini_usage,
    record_usage,
    usage_rows,
)
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

# Below this many briefs the cross-topic read is too thin to mine a behaviour.
_MIN_BRIEFS = 6


def _briefs_input(
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
    scores_by_topic: dict[tuple[str, str], dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    scores = scores_by_topic or {}
    out: list[dict[str, Any]] = []
    for (market, topic), brief in briefs_by_topic.items():
        s = scores.get((market, topic), {})
        out.append(
            {
                "market": market,
                "topic_group": topic,
                "status_tag": brief.get("status_tag") or "Rising",
                "trend_synthesis": brief.get("trend_synthesis")
                or brief.get("description_rationale")
                or "",
                "cultural_context": brief.get("cultural_context")
                or brief.get("activation_idea")
                or "",
                "headline": brief.get("headline") or "",
                "platforms": list(brief.get("platforms") or []),
                "seed_score": float(s.get("seed_score") or brief.get("seed_score") or 0.0),
                "velocity_score": float(s.get("velocity_score") or 0.0),
            }
        )
    # Lead with the strongest seed signals so the model meets the best candidates
    # first, but it still reads across the whole set.
    out.sort(key=lambda r: -float(r["seed_score"]))
    return out


def _rows(trend_date: date, seeds: list[dict], response) -> list[dict]:
    rows = []
    for i, s in enumerate(seeds, 1):
        act = s.get("activation") or {}
        rows.append(
            {
                "insight_id": str(uuid.uuid4()),
                "trend_date": trend_date,
                "rank": i,
                "behaviour": str(s.get("behaviour") or "")[:200],
                "the_shift": str(s.get("the_shift") or ""),
                "evidence": [str(e) for e in (s.get("evidence") or [])],
                "why_hidden": str(s.get("why_hidden") or ""),
                "timing": str(s.get("timing") or ""),
                "markets": [str(m) for m in (s.get("markets") or [])],
                "brand_opportunity": str(s.get("brand_opportunity") or ""),
                "activation_tool": str(act.get("tool") or ""),
                "activation_angle": str(act.get("angle") or ""),
                "activation_prompt": str(act.get("prompt") or ""),
                "signal_strength": str(s.get("signal_strength") or ""),
                "gemini_model": response.model,
                "prompt_tokens": int(response.prompt_tokens),
                "completion_tokens": int(response.completion_tokens),
                "generated_at": datetime.now(UTC),
            }
        )
    return rows


def _existing(client, dataset, trend_date) -> bool:
    sql = f"SELECT COUNT(*) AS n FROM `{client.project}.{dataset}.seed_insights` WHERE trend_date = @d"
    job = client.query(
        sql,
        job_config=bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("d", "DATE", trend_date)]
        ),
    )
    return bool(next(iter(job.result())).n)


# This stage's name in the shared gemini_usage ledger. market is NULL: the seed
# pass is one cross-market call. Note the seed_insights TABLE stamps that one
# call's tokens onto EVERY seed row it writes, so a SUM over that table prices
# the call once per seed (measured 3.0x over 2026-07-25..08-24). The ledger
# records the call, not the fan-out.
USAGE_CONSUMER = "seed_insights"


def generate_seed_intelligence(
    *,
    trend_date: date,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
    trend_scores_by_topic: dict[tuple[str, str], dict[str, Any]] | None = None,
    gemini_client: GeminiClient | None = None,
    persist: bool = True,
    force: bool = False,
    usage_sink: UsageSink | None = None,
    bq_client: bq.Client | None = None,
    dataset: str | None = None,
    row_sink: Callable[[list[dict[str, Any]]], int] | None = None,
) -> list[dict]:
    """Mine the day's briefs for seedable behaviours. Returns the seed list (and
    persists one row each). Non-fatal: logs and returns [] on any failure."""
    usage_tally: dict = {}
    sink = usage_sink or persist_gemini_usage
    try:
        if not briefs_by_topic or len(briefs_by_topic) < _MIN_BRIEFS:
            logger.info(
                "seed_intelligence: only %d briefs for %s, too thin, skipping",
                len(briefs_by_topic or {}),
                trend_date,
            )
            return []

        client = bq_client if bq_client is not None else get_client()
        dataset = dataset if dataset is not None else get_dataset()
        if persist and not force and _existing(client, dataset, trend_date):
            logger.info("seed_intelligence: rows already exist for %s, skipping", trend_date)
            return []

        gemini_client = gemini_client or GeminiClient()
        inputs = _briefs_input(briefs_by_topic, trend_scores_by_topic)
        prompt = build_seed_prompt(trend_date=trend_date.isoformat(), briefs=inputs)
        response = gemini_client.generate_brief(
            prompt=prompt,
            response_schema=RESPONSE_SCHEMA,
            temperature=0.55,
            max_output_tokens=8192,
            system_instruction=SYSTEM_INSTRUCTION,
        )
        record_usage(usage_tally, None, response)
        seeds = (response.parsed or {}).get("seeds") or []
        if not seeds:
            logger.warning("seed_intelligence: empty seeds for %s", trend_date)
            return []

        rows = _rows(trend_date, seeds, response)
        if persist:
            if row_sink is None:
                insert_dataframe(pd.DataFrame(rows), "seed_insights")
            else:
                written = row_sink(rows)
                if type(written) is not int or written != len(rows):
                    raise ValueError("seed_insights row sink incomplete")
            logger.info("seed_intelligence: wrote %d behaviours for %s", len(rows), trend_date)
        return seeds
    except Exception as exc:  # non-fatal by contract
        if row_sink is not None:
            raise
        logger.error("seed_intelligence failed (non-fatal): %s", exc, exc_info=True)
        return []
    finally:
        # The call bills whether or not a seed shipped, but persist=False is an
        # explicit "write nothing to BigQuery" contract and the ledger is a
        # BigQuery table. On a dry run the billing reconciliation is what
        # surfaces the spend, not a write that breaks the caller's flag.
        if persist:
            sink(usage_rows(trend_date, USAGE_CONSUMER, usage_tally))


__all__ = ["generate_seed_intelligence"]
