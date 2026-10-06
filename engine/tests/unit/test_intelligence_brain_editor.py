from __future__ import annotations

from dataclasses import replace

import pytest


def context():
    from src.analysis.open_intelligence.brain_strategist import _run_strategist

    from tests.unit.test_intelligence_brain_strategist import context as strategist_context

    values = strategist_context()
    return (*values, _run_strategist(*values))


def test_editor_citations_are_exact_and_client_read_stays_false():
    from src.analysis.open_intelligence.brain_editor import _run_editor

    result = _run_editor(*context())
    assert result.client_read_eligible is False
    assert result.citation_map
    assert all(item.citation_precision_state == "exact" for item in result.citation_map)
    assert all(item.citation_completeness_state == "complete" for item in result.citation_map)


def test_editor_rejects_incomplete_citation():
    from src.analysis.open_intelligence.brain_editor import _run_editor, _validate_citation

    item = _run_editor(*context()).citation_map[0]
    with pytest.raises(ValueError, match="citation incomplete"):
        _validate_citation(replace(item, cited_evidence_ids=()))


def test_editor_rejects_foreign_citation():
    from src.analysis.open_intelligence.brain_editor import _run_editor, _validate_citation

    item = _run_editor(*context()).citation_map[0]
    with pytest.raises(ValueError, match="citation invalid"):
        _validate_citation(replace(item, cited_evidence_ids=("ev_foreign",)))
