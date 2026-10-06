from __future__ import annotations

from dataclasses import replace

import pytest


def context():
    from src.analysis.open_intelligence.brain_analyst import _run_analyst

    from tests.unit.test_intelligence_brain_analyst import context as analyst_context

    runtime, snapshot, observer, historian = analyst_context()
    analyst = _run_analyst(runtime, snapshot, observer, historian)
    return runtime, snapshot, observer, historian, analyst


def test_skeptic_requires_rival_and_symmetric_comparison():
    from src.analysis.open_intelligence.brain_skeptic import _run_skeptic

    values = context()
    result = _run_skeptic(*values)
    assert len(result.rival_hypotheses) == 1
    comparison = result.hypothesis_comparison
    assert comparison.ordered_evidence_universe_ids == tuple(values[1].evidence_ids)
    assert comparison.verdict == "leading_fits_better"
    assert {item.direction for item in result.what_would_change} == {
        "strengthen",
        "weaken",
        "reverse",
    }


@pytest.mark.parametrize("mutation", ["order", "omit", "method"])
def test_symmetric_comparison_mutations_fail(mutation):
    from src.analysis.open_intelligence.brain_skeptic import (
        _run_skeptic,
        _validate_hypothesis_comparison,
    )

    result = _run_skeptic(*context())
    value = result.hypothesis_comparison
    if mutation == "order":
        value = replace(
            value,
            ordered_evidence_universe_ids=tuple(reversed(value.ordered_evidence_universe_ids)),
        )
    elif mutation == "omit":
        value = replace(
            value, ordered_evidence_universe_ids=value.ordered_evidence_universe_ids[:-1]
        )
    else:
        value = replace(value, fit_method_id="different")
    with pytest.raises(ValueError, match="hypothesis comparison"):
        _validate_hypothesis_comparison(value, tuple(context()[1].evidence_ids))


def test_skeptic_rejects_missing_rival():
    from src.analysis.open_intelligence.brain_skeptic import _require_rivals

    with pytest.raises(ValueError, match="rival"):
        _require_rivals(())
