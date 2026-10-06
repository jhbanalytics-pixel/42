from __future__ import annotations

from dataclasses import replace

import pytest

from tests.unit.test_intelligence_brain_editor import context


def graph_context():
    from src.analysis.open_intelligence.brain_editor import _run_editor

    values = context()
    editor = _run_editor(*values)
    return values[1], (*values[2:], editor)


def test_graph_derives_mandatory_nodes_and_edges():
    from src.analysis.open_intelligence.brain_graph import build_intelligence_evidence_graph

    snapshot, roles = graph_context()
    graph = build_intelligence_evidence_graph(snapshot, roles)
    assert {node.node_type for node in graph.nodes} >= {
        "signal",
        "role_result",
        "receipt",
        "hypothesis_comparison",
        "tension",
        "opportunity",
        "prediction",
        "outcome",
    }
    assert {edge.edge_type for edge in graph.edges} >= {
        "derived_from",
        "supports",
        "challenges",
        "depends_on",
        "would_change",
        "predicts",
        "resolved_by",
    }


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_graph_rejects_edge_set_mutation(mutation):
    from src.analysis.open_intelligence.brain_contract import BrainAdmission
    from src.analysis.open_intelligence.brain_graph import (
        GraphEdge,
        build_intelligence_evidence_graph,
        validate_intelligence_evidence_graph,
    )

    snapshot, roles = graph_context()
    graph = build_intelligence_evidence_graph(snapshot, roles)
    edges = (
        graph.edges[:-1]
        if mutation == "missing"
        else (
            *graph.edges,
            GraphEdge(
                "edge_extra",
                "foreign",
                "foreign",
                "depends_on",
                snapshot.scope_digest,
                (),
                "graph_v1",
                BrainAdmission("available", "validated", "not_required", True, (), None),
                None,
                (),
            ),
        )
    )
    with pytest.raises(ValueError, match="graph incomplete"):
        validate_intelligence_evidence_graph(replace(graph, edges=edges), snapshot, roles)
