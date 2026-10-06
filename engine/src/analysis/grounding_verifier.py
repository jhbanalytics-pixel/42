"""Grounding verifier shadow stub (V3 Track B2, dark).

Full implementation waits on B1 live + cost approval. Staging exposes the
module and flag gate so wiring can be tested without Vertex spend.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_grounding_verifier_shadow(
    trend_date: str,
    claims: list[str],
    *,
    market: str | None = None,
) -> list[dict[str, Any]]:
    """Shadow pass: returns empty receipts until B2 is promoted."""
    _ = trend_date, claims, market
    logger.info("grounding_verifier shadow stub: 0 claims verified (B2 dark)")
    return []
