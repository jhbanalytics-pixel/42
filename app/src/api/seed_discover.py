"""Discover desk: seed_candidates proposals for the current week."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from . import bq

logger = logging.getLogger(__name__)

_MARKETS = ("za", "ng", "ke")


def _empty_market_bucket() -> dict[str, Any]:
    return {
        "pending_count": 0,
        "weekly_count": 0,
        "fast_count": 0,
        "candidates": [],
    }


def _discover_why(row: dict, fit: dict[str, float]) -> str:
    parts: list[str] = []
    if fit.get("co_occur", 0) >= 0.5:
        parts.append("Co-occurs with hot topics")
    topics = row.get("evidence_topics") or []
    if topics:
        n = len(topics)
        parts.append(f"Touches {n} engine topic{'s' if n != 1 else ''}")
    if fit.get("visual_audio", 0) >= 0.5:
        parts.append("Strong on visual/audio platforms")
    if fit.get("slang", 0) >= 0.55:
        parts.append("Slang signal")
    lane = str(row.get("lane") or "")
    if lane == "weekly" and not parts:
        parts.append("Monday weekly batch")
    elif lane == "fast" and len(parts) < 2:
        parts.append("Daily fast lane")
    return " · ".join(parts) if parts else "Ranked from seed_graph this week."


def _shape_candidate(row: dict) -> dict[str, Any]:
    fit = bq._seed_fit_dict(row)
    topics_raw = row.get("evidence_topics") or []
    topics = [
        {"id": str(tg), "label": bq.topic_label(str(tg))}
        for tg in topics_raw
        if tg
    ]
    sample_ids = row.get("sample_row_ids") or []
    pd = row.get("proposed_date")
    proposed = pd.isoformat() if hasattr(pd, "isoformat") else str(pd or "")
    score = row.get("score")
    return {
        "id": str(row.get("candidate_id") or ""),
        "term": str(row.get("candidate_value") or ""),
        "term_type": str(row.get("candidate_type") or "keyword"),
        "market": str(row.get("market") or "").lower(),
        "lane": str(row.get("lane") or ""),
        "score": float(score) if score is not None else 0.0,
        "proposed_date": proposed,
        "status": str(row.get("status") or ""),
        "source": str(row.get("source") or ""),
        "fit": {
            "co_occur": fit.get("co_occur"),
            "visual_audio": fit.get("visual_audio"),
            "slang": fit.get("slang"),
        },
        "topics": topics[:8],
        "sample_post_count": len(sample_ids) if sample_ids else 0,
        "why": _discover_why(row, fit),
        "rationale": row.get("rationale"),
    }


def build_discover_payload(
    *,
    market: str | None = None,
    status: str = "pending",
    week_start: date | None = None,
) -> dict[str, Any]:
    today = date.today()
    start = week_start or bq._monday_on_or_before(today)
    end = start + timedelta(days=6)
    mk = market.strip().lower() if market else None
    if mk and mk not in _MARKETS:
        mk = None

    markets_out = {m: _empty_market_bucket() for m in _MARKETS}
    try:
        rows = bq.fetch_seed_candidates_week(
            week_start=start,
            week_end=end if status != "decided" else today,
            status_mode=status,
            market=mk,
        )
    except Exception as exc:
        logger.warning("seed_discover BQ read failed: %s", exc)
        return {
            "week_start": start.isoformat(),
            "week_end": end.isoformat(),
            "as_of": today.isoformat(),
            "dataset": bq._dataset(),
            "status": status,
            "error": "Discovery proposals not available yet.",
            "markets": markets_out,
        }

    for row in rows:
        m = str(row.get("market") or "").lower()
        if m not in markets_out:
            continue
        shaped = _shape_candidate(row)
        bucket = markets_out[m]
        bucket["candidates"].append(shaped)
        if shaped["status"] == "pending":
            bucket["pending_count"] += 1
        if shaped["lane"] == "weekly":
            bucket["weekly_count"] += 1
        elif shaped["lane"] == "fast":
            bucket["fast_count"] += 1

    if mk:
        filtered = {mk: markets_out[mk]}
    else:
        filtered = markets_out

    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "as_of": today.isoformat(),
        "dataset": bq._dataset(),
        "status": status,
        "markets": filtered,
    }


__all__ = ["build_discover_payload"]
