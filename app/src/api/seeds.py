"""Seed intelligence payload for the tool: the day's hidden seedable behaviours.

Reads seed_insights (the cross-topic Gemini pass output) and shapes each
behaviour for the seeding journey, parsing the evidence lines into linkable
topic refs so the user can click a proof point straight through to its topic
story.
"""

from __future__ import annotations

import re

from . import bq

# Evidence lines come as "market/topic_group - what it reveals". Split the ref
# from the note so the frontend can link the ref to a topic story.
_EVIDENCE_RE = re.compile(r"^\s*(za|ng|ke)\s*/\s*([a-z0-9_]+)\s*[-–:]\s*(.*)$", re.IGNORECASE)


def _parse_evidence(items) -> list:
    out = []
    for raw in items or []:
        s = str(raw).strip()
        m = _EVIDENCE_RE.match(s)
        if m:
            market, topic, note = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
            out.append(
                {
                    "ref": f"{market}/{topic}",
                    "market": market,
                    "topic": topic,
                    "topic_label": bq.topic_label(topic),
                    "note": note,
                }
            )
        else:
            # Keep an unparseable line as a plain note so nothing is dropped.
            out.append({"ref": None, "market": None, "topic": None, "topic_label": None, "note": s})
    return out


def _seed(row: dict) -> dict:
    return {
        "rank": int(row.get("rank") or 0),
        "behaviour": row.get("behaviour") or "",
        "the_shift": row.get("the_shift") or "",
        "evidence": _parse_evidence(row.get("evidence")),
        "why_hidden": row.get("why_hidden") or "",
        "timing": row.get("timing") or "",
        "markets": [str(m).lower() for m in (row.get("markets") or [])],
        "brand_opportunity": row.get("brand_opportunity") or "",
        "activation": {
            "tool": row.get("activation_tool") or "",
            "angle": row.get("activation_angle") or "",
            "prompt": row.get("activation_prompt") or "",
        },
        "signal_strength": row.get("signal_strength") or "",
    }


def build_seeds_payload() -> dict:
    """The seeding-journey payload: the latest day's behaviours, ranked, each with
    linkable evidence and a ready activation. Empty list when none yet."""
    rows = bq.fetch_seed_insights()
    date = str(rows[0].get("trend_date")) if rows else None
    return {"date": date, "seeds": [_seed(r) for r in rows]}


__all__ = ["build_seeds_payload"]
