from __future__ import annotations

from dataclasses import replace

import pytest

from tests.unit.test_intelligence_brain_graph import graph_context


def test_graph_represents_comparison_and_closes_every_edge_endpoint():
    from src.analysis.open_intelligence.brain_graph import (
        build_intelligence_evidence_graph,
        validate_intelligence_evidence_graph,
    )

    snapshot, roles = graph_context()
    graph = build_intelligence_evidence_graph(snapshot, roles)
    comparison_id = roles[3].hypothesis_comparison.comparison_id
    node_pairs = {(node.node_id, node.node_type) for node in graph.nodes}
    node_ids = {node_id for node_id, _node_type in node_pairs}
    edge_endpoints = {
        endpoint for edge in graph.edges for endpoint in (edge.from_node_id, edge.to_node_id)
    }
    would_change_targets = {
        edge.to_node_id for edge in graph.edges if edge.edge_type == "would_change"
    }

    assert would_change_targets <= node_ids
    assert (comparison_id, "hypothesis_comparison") in node_pairs
    assert edge_endpoints <= node_ids
    assert validate_intelligence_evidence_graph(graph, snapshot, roles) is graph


def test_validator_rejects_graph_when_comparison_endpoint_is_dangling():
    from src.analysis.open_intelligence.brain_graph import (
        build_intelligence_evidence_graph,
        validate_intelligence_evidence_graph,
    )

    snapshot, roles = graph_context()
    graph = build_intelligence_evidence_graph(snapshot, roles)
    comparison_id = roles[3].hypothesis_comparison.comparison_id
    nodes_without_comparison = tuple(node for node in graph.nodes if node.node_id != comparison_id)

    with pytest.raises(ValueError, match="graph incomplete"):
        validate_intelligence_evidence_graph(
            replace(graph, nodes=nodes_without_comparison), snapshot, roles
        )
