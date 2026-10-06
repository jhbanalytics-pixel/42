"""Gemini-assisted taxonomy proposals (V3 Track C4, dark stub).

Gated by PROPOSE_TAXONOMY_CANDIDATES_ENABLED. Returns empty until RECONCILE
watch closes and C4 is earned.
"""

from __future__ import annotations

import os
from datetime import date


def is_enabled() -> bool:
    return os.environ.get("PROPOSE_TAXONOMY_CANDIDATES_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def propose_taxonomy_candidates(trend_date: str | date, market: str) -> list[dict]:
    """One batched Vertex call per market per week (not implemented).

    When enabled, reads unclassified residual + top new seed_graph terms and
    returns candidate dicts with source=coverage for the same seed_candidates
    table and human review gate. Dark until C4 flip criteria are met.
    """
    if not is_enabled():
        return []
    _ = trend_date, market
    return []
