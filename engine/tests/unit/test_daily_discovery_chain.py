"""Admission regressions for the repeatable daily discovery chain."""

from __future__ import annotations

import pytest
from src.analysis.open_intelligence.readiness import require_independent_release


def test_copies_cannot_release():
    with pytest.raises(ValueError, match="insufficient_independent_support"):
        require_independent_release({"evidence_state": "ready"}, independent_pairs=())


@pytest.mark.parametrize("state", ["thin", "contradictory", "unchecked"])
def test_unready_candidate_is_refused_before_support_is_considered(state):
    with pytest.raises(ValueError, match="candidate_not_ready"):
        require_independent_release(
            {"evidence_state": state}, independent_pairs=(("row-1", "row-2"),)
        )


def test_ready_candidate_with_an_admitted_independent_pair_passes():
    assert (
        require_independent_release(
            {"evidence_state": "ready"}, independent_pairs=(("row-1", "row-2"),)
        )
        is None
    )


def test_candidate_must_carry_an_evidence_state():
    with pytest.raises(KeyError):
        require_independent_release({}, independent_pairs=(("row-1", "row-2"),))
