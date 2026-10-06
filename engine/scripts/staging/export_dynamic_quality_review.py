"""Read-only canonical review packet exporter for deterministic fixture batches."""

from __future__ import annotations

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.live_quality import build_review_packet
from src.analysis.open_intelligence.persistence import OpenIntelligenceRowBatch


def render_review_packet(run_id: str, batch: OpenIntelligenceRowBatch) -> str:
    """Render one complete packet without a cloud reader or write surface."""
    return canonical_bytes(build_review_packet(run_id, batch)).decode("utf-8")


__all__ = ["render_review_packet"]
