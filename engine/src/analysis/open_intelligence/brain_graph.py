"""Mandatory evidence graph derivation for the dark Brain kernel."""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.open_intelligence.brain_authority import _is_live_snapshot
from src.analysis.open_intelligence.brain_contract import (
    BrainAdmission,
    canonical_digest,
)


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    node_type: str
    admission: BrainAdmission


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_id: str
    from_node_id: str
    to_node_id: str
    edge_type: str
    scope_digest: str
    evidence_ids: tuple[str, ...]
    method_id: str
    admission: BrainAdmission
    relationship_decision_id: str | None
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntelligenceEvidenceGraph:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


def _edge(source, target, kind, snapshot, evidence=()):
    admission = BrainAdmission("available", "validated", "not_required", True, (), None)
    return GraphEdge(
        "edge_" + canonical_digest((source, target, kind)),
        source,
        target,
        kind,
        snapshot.scope_digest,
        tuple(evidence),
        "graph_derivation_v1",
        admission,
        None,
        (),
    )


def _expected(snapshot, roles):
    observer, _historian, analyst, skeptic, strategist, editor = roles
    admission = BrainAdmission("available", "validated", "not_required", True, (), None)
    nodes = [GraphNode(snapshot.signal_id, "signal", admission)]
    nodes.extend(
        GraphNode(f"role_{name}", "role_result", admission)
        for name in ("observer", "historian", "analyst", "skeptic", "strategist", "editor")
    )
    nodes.extend(GraphNode(item, "receipt", admission) for item in snapshot.evidence_ids)
    nodes.extend(
        GraphNode(item.observation_id, "observation", admission) for item in observer.observations
    )
    claim_ids = (
        observer.what_changed_claim_id,
        analyst.why_now.claim_id,
        analyst.leading_hypothesis["hypothesis_id"],
        *(item.hypothesis_id for item in skeptic.rival_hypotheses),
        *(item.change_id for item in skeptic.what_would_change),
        *(item.clause_id for item in editor.citation_map),
    )
    nodes.extend(GraphNode(item, "claim", admission) for item in claim_ids)
    nodes.append(
        GraphNode(
            skeptic.hypothesis_comparison.comparison_id,
            "hypothesis_comparison",
            admission,
        )
    )
    nodes.extend(GraphNode(item, "source", admission) for item in snapshot.source_family_ids)
    nodes.extend(GraphNode(item, "market", admission) for item in snapshot.market_ids)
    nodes.extend(
        GraphNode(item, "historical_object", admission) for item in snapshot.historical_object_ids
    )
    recurrence_id = f"recurrence_{snapshot.signal_id}"
    diffusion_id = f"diffusion_{snapshot.signal_id}"
    nodes.extend(
        (
            GraphNode(recurrence_id, "historical_object", admission),
            GraphNode(diffusion_id, "historical_object", admission),
        )
    )
    nodes.extend(
        (
            GraphNode(strategist.cultural_tension.tension_id, "tension", admission),
            GraphNode(strategist.opportunity.opportunity_id, "opportunity", admission),
            GraphNode(analyst.prediction_learning.prediction_id, "prediction", admission),
        )
    )
    nodes.extend(GraphNode(item, "outcome", admission) for item in snapshot.outcome_ids)
    edges = []
    edges.extend(
        _edge(item.observation_id, snapshot.signal_id, "derived_from", snapshot, item.evidence_ids)
        for item in observer.observations
    )
    edges.extend(
        _edge(item.observation_id, item.evidence_ids[0], "observed_in", snapshot, item.evidence_ids)
        for item in observer.observations
        if item.evidence_ids
    )
    edges.extend(
        _edge(evidence, observer.what_changed_claim_id, "supports", snapshot, (evidence,))
        for evidence in sorted(
            {evidence for item in observer.observations for evidence in item.evidence_ids}
        )
    )
    edges.extend(
        _edge(
            analyst.why_now.claim_id,
            item.observation_id,
            "derived_from",
            snapshot,
            item.evidence_ids,
        )
        for item in observer.observations
        if item.evidence_ids
    )
    edges.extend(
        _edge(
            evidence,
            analyst.why_now.claim_id,
            "supports",
            snapshot,
            (evidence,),
        )
        for evidence in analyst.why_now.evidence_ids
    )
    edges.extend(
        _edge(
            evidence,
            analyst.leading_hypothesis["hypothesis_id"],
            "supports",
            snapshot,
            (evidence,),
        )
        for evidence in analyst.leading_hypothesis["evidence_ids"]
    )
    for rival in skeptic.rival_hypotheses:
        edges.extend(
            _edge(evidence, rival.hypothesis_id, "supports", snapshot, (evidence,))
            for evidence in rival.supporting_evidence_ids
        )
        edges.extend(
            _edge(evidence, rival.hypothesis_id, "challenges", snapshot, (evidence,))
            for evidence in rival.challenging_evidence_ids
        )
    edges.extend(
        _edge(
            evidence,
            strategist.cultural_tension.tension_id,
            "supports",
            snapshot,
            (evidence,),
        )
        for evidence in strategist.cultural_tension.supporting_evidence_ids
    )
    edges.extend(
        _edge(
            evidence,
            strategist.cultural_tension.tension_id,
            "challenges",
            snapshot,
            (evidence,),
        )
        for evidence in strategist.cultural_tension.challenging_evidence_ids
    )
    edges.extend(
        _edge(
            evidence,
            strategist.opportunity.opportunity_id,
            "supports",
            snapshot,
            (evidence,),
        )
        for evidence in strategist.opportunity.supporting_evidence_ids
    )
    edges.extend(
        _edge(
            evidence,
            strategist.opportunity.opportunity_id,
            "challenges",
            snapshot,
            (evidence,),
        )
        for evidence in strategist.opportunity.challenging_evidence_ids
    )
    edges.extend(
        _edge(item.clause_id, item.claim_id, "depends_on", snapshot, item.required_evidence_ids)
        for item in editor.citation_map
    )
    edges.append(
        _edge(
            analyst.leading_hypothesis["hypothesis_id"],
            skeptic.rival_hypotheses[0].hypothesis_id,
            "compares_with",
            snapshot,
            snapshot.evidence_ids,
        )
    )
    edges.extend(
        _edge(item, snapshot.signal_id, "historical_of", snapshot, snapshot.evidence_ids)
        for item in snapshot.historical_object_ids
    )
    edges.append(_edge(recurrence_id, snapshot.signal_id, "recurs_as", snapshot))
    edges.append(_edge(diffusion_id, snapshot.signal_id, "diffusion_shadow_of", snapshot))
    edges.append(
        _edge(
            strategist.cultural_tension.tension_id,
            analyst.why_now.claim_id,
            "depends_on",
            snapshot,
            snapshot.evidence_ids,
        )
    )
    edges.append(
        _edge(
            strategist.opportunity.opportunity_id,
            strategist.cultural_tension.tension_id,
            "depends_on",
            snapshot,
            snapshot.evidence_ids,
        )
    )
    edges.extend(
        _edge(item.change_id, skeptic.hypothesis_comparison.comparison_id, "would_change", snapshot)
        for item in skeptic.what_would_change
    )
    edges.append(
        _edge(
            snapshot.signal_id,
            analyst.prediction_learning.prediction_id,
            "predicts",
            snapshot,
            snapshot.evidence_ids,
        )
    )
    edges.extend(
        _edge(item, analyst.prediction_learning.prediction_id, "resolved_by", snapshot)
        for item in analyst.prediction_learning.observed_outcome_ids
    )
    return tuple(sorted(nodes, key=lambda item: (item.node_type, item.node_id))), tuple(
        sorted(edges, key=lambda item: item.edge_id)
    )


def _live_expected(snapshot, roles):
    observer = roles[0]
    admission = BrainAdmission("available", "validated", "not_required", True, (), None)
    nodes = [GraphNode(snapshot.signal_id, "signal", admission)]
    nodes.extend(GraphNode(item, "source", admission) for item in snapshot.source_family_ids)
    nodes.extend(GraphNode(item, "market", admission) for item in snapshot.market_ids)
    nodes.extend(GraphNode(item, "receipt", admission) for item in snapshot.evidence_ids)
    nodes.extend(
        GraphNode(item.observation_id, "observation", admission) for item in observer.observations
    )
    edges = []
    for observation in observer.observations:
        edges.append(
            _edge(
                observation.observation_id,
                snapshot.signal_id,
                "derived_from",
                snapshot,
                observation.evidence_ids,
            )
        )
        if observation.evidence_ids:
            edges.append(
                _edge(
                    observation.observation_id,
                    observation.evidence_ids[0],
                    "observed_in",
                    snapshot,
                    observation.evidence_ids,
                )
            )
    return tuple(sorted(nodes, key=lambda item: (item.node_type, item.node_id))), tuple(
        sorted(edges, key=lambda item: item.edge_id)
    )


def _mandatory_node_pairs(snapshot, roles):
    observer, _historian, analyst, skeptic, strategist, editor = roles
    pairs = {(snapshot.signal_id, "signal")}
    pairs.update(
        (f"role_{name}", "role_result")
        for name in ("observer", "historian", "analyst", "skeptic", "strategist", "editor")
    )
    pairs.update((item, "receipt") for item in snapshot.evidence_ids)
    pairs.update((item.observation_id, "observation") for item in observer.observations)
    pairs.update(
        (item, "claim")
        for item in (
            observer.what_changed_claim_id,
            analyst.why_now.claim_id,
            analyst.leading_hypothesis["hypothesis_id"],
            *(rival.hypothesis_id for rival in skeptic.rival_hypotheses),
            *(change.change_id for change in skeptic.what_would_change),
            *(citation.clause_id for citation in editor.citation_map),
        )
    )
    pairs.add((skeptic.hypothesis_comparison.comparison_id, "hypothesis_comparison"))
    pairs.update((item, "source") for item in snapshot.source_family_ids)
    pairs.update((item, "market") for item in snapshot.market_ids)
    pairs.update((item, "historical_object") for item in snapshot.historical_object_ids)
    pairs.update(
        (
            (f"recurrence_{snapshot.signal_id}", "historical_object"),
            (f"diffusion_{snapshot.signal_id}", "historical_object"),
            (strategist.cultural_tension.tension_id, "tension"),
            (strategist.opportunity.opportunity_id, "opportunity"),
            (analyst.prediction_learning.prediction_id, "prediction"),
        )
    )
    pairs.update((item, "outcome") for item in snapshot.outcome_ids)
    return pairs


def _mandatory_edge_evidence(snapshot, roles):
    observer, _historian, analyst, skeptic, strategist, editor = roles
    expected = {}

    def add(source, target, kind, evidence=()):
        key = (source, target, kind)
        value = tuple(evidence)
        if key in expected and expected[key] != value:
            raise ValueError("graph incomplete")
        expected[key] = value

    for observation in observer.observations:
        add(
            observation.observation_id,
            snapshot.signal_id,
            "derived_from",
            observation.evidence_ids,
        )
        if observation.evidence_ids:
            add(
                observation.observation_id,
                observation.evidence_ids[0],
                "observed_in",
                observation.evidence_ids,
            )
            add(
                analyst.why_now.claim_id,
                observation.observation_id,
                "derived_from",
                observation.evidence_ids,
            )
        for evidence in observation.evidence_ids:
            add(evidence, observer.what_changed_claim_id, "supports", (evidence,))
    for evidence in analyst.why_now.evidence_ids:
        add(evidence, analyst.why_now.claim_id, "supports", (evidence,))
    for evidence in analyst.leading_hypothesis["evidence_ids"]:
        add(evidence, analyst.leading_hypothesis["hypothesis_id"], "supports", (evidence,))
    for rival in skeptic.rival_hypotheses:
        for evidence in rival.supporting_evidence_ids:
            add(evidence, rival.hypothesis_id, "supports", (evidence,))
        for evidence in rival.challenging_evidence_ids:
            add(evidence, rival.hypothesis_id, "challenges", (evidence,))
    tension = strategist.cultural_tension
    for evidence in tension.supporting_evidence_ids:
        add(evidence, tension.tension_id, "supports", (evidence,))
    for evidence in tension.challenging_evidence_ids:
        add(evidence, tension.tension_id, "challenges", (evidence,))
    opportunity = strategist.opportunity
    for evidence in opportunity.supporting_evidence_ids:
        add(evidence, opportunity.opportunity_id, "supports", (evidence,))
    for evidence in opportunity.challenging_evidence_ids:
        add(evidence, opportunity.opportunity_id, "challenges", (evidence,))
    for citation in editor.citation_map:
        add(citation.clause_id, citation.claim_id, "depends_on", citation.required_evidence_ids)
    add(
        analyst.leading_hypothesis["hypothesis_id"],
        skeptic.rival_hypotheses[0].hypothesis_id,
        "compares_with",
        snapshot.evidence_ids,
    )
    for item in snapshot.historical_object_ids:
        add(item, snapshot.signal_id, "historical_of", snapshot.evidence_ids)
    add(f"recurrence_{snapshot.signal_id}", snapshot.signal_id, "recurs_as")
    add(f"diffusion_{snapshot.signal_id}", snapshot.signal_id, "diffusion_shadow_of")
    add(tension.tension_id, analyst.why_now.claim_id, "depends_on", snapshot.evidence_ids)
    add(opportunity.opportunity_id, tension.tension_id, "depends_on", snapshot.evidence_ids)
    for change in skeptic.what_would_change:
        add(change.change_id, skeptic.hypothesis_comparison.comparison_id, "would_change")
    add(
        snapshot.signal_id,
        analyst.prediction_learning.prediction_id,
        "predicts",
        snapshot.evidence_ids,
    )
    for item in analyst.prediction_learning.observed_outcome_ids:
        add(item, analyst.prediction_learning.prediction_id, "resolved_by")
    return expected


def build_intelligence_evidence_graph(snapshot, roles) -> IntelligenceEvidenceGraph:
    nodes, edges = (
        _live_expected(snapshot, roles)
        if _is_live_snapshot(snapshot)
        else _expected(snapshot, roles)
    )
    return IntelligenceEvidenceGraph(nodes, edges)


def validate_intelligence_evidence_graph(value, snapshot, roles):
    if _is_live_snapshot(snapshot):
        expected_nodes, expected_edges = _live_expected(snapshot, roles)
        if value != IntelligenceEvidenceGraph(expected_nodes, expected_edges):
            raise ValueError("graph incomplete")
        return value
    admission = BrainAdmission("available", "validated", "not_required", True, (), None)
    expected_nodes = _mandatory_node_pairs(snapshot, roles)
    actual_nodes = {(node.node_id, node.node_type) for node in value.nodes}
    actual_node_ids = {node.node_id for node in value.nodes}
    expected_edges = _mandatory_edge_evidence(snapshot, roles)
    actual_edges = {
        (edge.from_node_id, edge.to_node_id, edge.edge_type): edge for edge in value.edges
    }
    if (
        len(value.nodes) != len(expected_nodes)
        or actual_nodes != expected_nodes
        or any(node.admission != admission for node in value.nodes)
        or any(
            edge.from_node_id not in actual_node_ids or edge.to_node_id not in actual_node_ids
            for edge in value.edges
        )
        or len(value.edges) != len(expected_edges)
        or set(actual_edges) != set(expected_edges)
        or any(
            edge.edge_id != "edge_" + canonical_digest(key)
            or edge.scope_digest != snapshot.scope_digest
            or edge.evidence_ids != expected_edges[key]
            or edge.method_id != "graph_derivation_v1"
            or edge.admission != admission
            or edge.relationship_decision_id is not None
            or edge.limitations
            for key, edge in actual_edges.items()
        )
    ):
        raise ValueError("graph incomplete")
    return value


__all__ = []
