"""Boundary tests for strict semantic matrix composition."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from src.analysis.open_intelligence.candidates import Observation
from src.analysis.open_intelligence.composition import (
    build_components_with_anchored_semantics,
    build_components_with_validated_semantics,
)
from src.analysis.open_intelligence.graph import AnchoredGraphRules, GraphRules


def observation(
    term: str,
    market: str = "za",
    *,
    family: str = "news",
    dates: tuple[str, ...] = (),
    co: tuple[str, ...] = (),
    creators: tuple[str, ...] = (),
) -> Observation:
    return Observation(
        market=market,
        term=term,
        candidate_type="keyword",
        row_ids=(f"row_{term}",),
        source_families=(family,),
        platforms=("web",),
        observed_dates=dates,
        co_occurring_terms=co,
        creator_ids=creators,
    )


class SemanticProvider:
    version = "test_semantics_v1"

    def __init__(self, similarities):
        self._similarities = similarities
        self.calls = []

    def similarities(self, observations):
        self.calls.append(observations)
        return self._similarities


def test_missing_same_market_semantic_pair_is_rejected_before_graph(monkeypatch):
    graph_builder = Mock()
    monkeypatch.setattr(
        "src.analysis.open_intelligence.composition.build_components", graph_builder
    )
    provider = SemanticProvider({})

    with pytest.raises(ValueError, match="missing semantic pair"):
        build_components_with_validated_semantics(
            [observation("beta"), observation("alpha")],
            provider,
            GraphRules(0.7, 0.55, 2),
        )

    assert tuple(item.term for item in provider.calls[0]) == ("alpha", "beta")
    graph_builder.assert_not_called()


def identity(term: str, market: str = "za") -> str:
    return f"{market}|keyword|{term}"


def test_complete_semantic_matrix_reaches_graph_once(monkeypatch):
    rules = GraphRules(0.7, 0.55, 2)
    matrix = {(identity("alpha"), identity("beta")): 0.8}
    provider = SemanticProvider(matrix)
    graph_builder = Mock(return_value=("component",))
    monkeypatch.setattr(
        "src.analysis.open_intelligence.composition.build_components", graph_builder
    )

    result = build_components_with_validated_semantics(
        [observation("gamma", "ng"), observation("beta"), observation("alpha")],
        provider,
        rules,
    )

    assert result == ("component",)
    assert len(provider.calls) == 1
    graph_builder.assert_called_once()
    ordered, semantic_scores, passed_rules = graph_builder.call_args.args
    assert tuple((item.market, item.term) for item in ordered) == (
        ("ng", "gamma"),
        ("za", "alpha"),
        ("za", "beta"),
    )
    assert semantic_scores == matrix
    assert passed_rules is rules


def test_extra_semantic_pair_is_rejected_before_graph(monkeypatch):
    graph_builder = Mock()
    monkeypatch.setattr(
        "src.analysis.open_intelligence.composition.build_components", graph_builder
    )
    provider = SemanticProvider(
        {
            (identity("alpha"), identity("beta")): 0.8,
            (identity("alpha"), identity("unknown")): 0.7,
        }
    )

    with pytest.raises(ValueError, match="extra semantic pair"):
        build_components_with_validated_semantics(
            [observation("alpha"), observation("beta")],
            provider,
            GraphRules(0.7, 0.55, 2),
        )

    graph_builder.assert_not_called()


@pytest.mark.parametrize(
    "provider",
    [
        object(),
        type("Provider", (), {"version": "", "similarities": lambda self, items: {}})(),
        type("Provider", (), {"version": "v1", "similarities": None})(),
    ],
)
def test_invalid_semantic_provider_is_rejected_before_graph(monkeypatch, provider):
    graph_builder = Mock()
    monkeypatch.setattr(
        "src.analysis.open_intelligence.composition.build_components", graph_builder
    )

    with pytest.raises(ValueError, match="semantic provider"):
        build_components_with_validated_semantics(
            [observation("alpha")],
            provider,
            GraphRules(0.7, 0.55, 2),
        )

    graph_builder.assert_not_called()


def test_non_mapping_semantic_result_is_rejected_before_graph(monkeypatch):
    graph_builder = Mock()
    monkeypatch.setattr(
        "src.analysis.open_intelligence.composition.build_components", graph_builder
    )
    provider = SemanticProvider([])

    with pytest.raises(ValueError, match="must be a mapping"):
        build_components_with_validated_semantics(
            [observation("alpha")],
            provider,
            GraphRules(0.7, 0.55, 2),
        )

    graph_builder.assert_not_called()


def test_duplicate_observation_identity_is_rejected_before_provider():
    provider = SemanticProvider({})

    with pytest.raises(ValueError, match="duplicate observation identity"):
        build_components_with_validated_semantics(
            [observation("alpha"), observation("alpha")],
            provider,
            GraphRules(0.7, 0.55, 2),
        )

    assert provider.calls == []


@pytest.mark.parametrize(
    ("pair", "message"),
    [
        ((identity("alpha"), identity("alpha")), "self semantic pair"),
        ((identity("alpha"), identity("gamma", "ng")), "cross-market semantic pair"),
        ((identity("beta"), identity("alpha")), "ordered semantic pair"),
        ((identity("alpha"),), "semantic pair key"),
    ],
)
def test_malformed_semantic_pairs_are_rejected_before_graph(monkeypatch, pair, message):
    graph_builder = Mock()
    monkeypatch.setattr(
        "src.analysis.open_intelligence.composition.build_components", graph_builder
    )
    provider = SemanticProvider({pair: 0.8})

    with pytest.raises(ValueError, match=message):
        build_components_with_validated_semantics(
            [observation("alpha"), observation("beta"), observation("gamma", "ng")],
            provider,
            GraphRules(0.7, 0.55, 2),
        )

    graph_builder.assert_not_called()


@pytest.mark.parametrize("score", [True, "0.8", float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_semantic_scores_are_rejected_before_graph(monkeypatch, score):
    graph_builder = Mock()
    monkeypatch.setattr(
        "src.analysis.open_intelligence.composition.build_components", graph_builder
    )
    provider = SemanticProvider({(identity("alpha"), identity("beta")): score})

    with pytest.raises(ValueError, match="semantic similarity"):
        build_components_with_validated_semantics(
            [observation("alpha"), observation("beta")],
            provider,
            GraphRules(0.7, 0.55, 2),
        )

    graph_builder.assert_not_called()


def test_input_order_does_not_change_validated_graph_call(monkeypatch):
    calls = []

    def graph_builder(observations, semantic_scores, rules):
        calls.append(
            (
                tuple((item.market, item.term) for item in observations),
                dict(semantic_scores),
                rules,
            )
        )
        return tuple(item.term for item in observations)

    monkeypatch.setattr(
        "src.analysis.open_intelligence.composition.build_components", graph_builder
    )
    rules = GraphRules(0.7, 0.55, 2)
    matrix = {(identity("alpha"), identity("beta")): 0.8}

    forward = build_components_with_validated_semantics(
        [observation("alpha"), observation("beta")], SemanticProvider(matrix), rules
    )
    reverse = build_components_with_validated_semantics(
        [observation("beta"), observation("alpha")], SemanticProvider(matrix), rules
    )

    assert forward == reverse == ("alpha", "beta")
    assert calls[0] == calls[1]


class AnchoredProvider:
    version = "test_semantics_v2"

    def __init__(self, pairs, similarities):
        self._pairs = tuple(pairs)
        self._similarities = dict(similarities)
        self.pair_calls = []
        self.score_calls = []

    def candidate_pairs(self, observations):
        self.pair_calls.append(observations)
        return self._pairs

    def similarities(self, observations, pairs):
        self.score_calls.append((observations, pairs))
        return self._similarities


def anchored_rules(pair_ceiling=10, component_member_ceiling=50):
    return AnchoredGraphRules(0.5, 2, pair_ceiling, component_member_ceiling)


def test_anchored_composition_scores_only_candidate_pairs():
    alpha = observation("repair alpha", family="news", dates=("2026-08-27",))
    beta = observation("repair beta", family="search", dates=("2026-08-27",))
    gamma = observation("unrelated", family="reddit", dates=("2026-08-27",))
    pair = (identity("repair alpha"), identity("repair beta"))
    provider = AnchoredProvider((pair,), {pair: 0.5})

    components = build_components_with_anchored_semantics(
        (gamma, beta, alpha), provider, anchored_rules()
    )

    assert tuple(component.terms for component in components) == (("repair alpha", "repair beta"),)
    assert provider.score_calls[0][1] == (pair,)


def test_anchored_composition_refuses_a_missing_candidate_score():
    pair = (identity("alpha"), identity("beta"))
    provider = AnchoredProvider((pair,), {})

    with pytest.raises(ValueError, match="missing semantic pair"):
        build_components_with_anchored_semantics(
            (observation("alpha"), observation("beta", family="search")),
            provider,
            anchored_rules(),
        )


def test_anchored_composition_refuses_before_scoring_above_the_pair_ceiling():
    pairs = (
        (identity("alpha"), identity("beta")),
        (identity("alpha"), identity("gamma")),
    )
    provider = AnchoredProvider(pairs, dict.fromkeys(pairs, 0.8))

    with pytest.raises(ValueError, match="composition_pair_ceiling_exceeded"):
        build_components_with_anchored_semantics(
            (
                observation("alpha"),
                observation("beta", family="search"),
                observation("gamma", family="reddit"),
            ),
            provider,
            anchored_rules(pair_ceiling=1),
        )

    assert provider.score_calls == []


def test_anchored_composition_is_input_order_independent():
    pair = (identity("alpha"), identity("beta"))
    items = (
        observation("alpha", family="news", dates=("2026-08-27",)),
        observation("beta", family="search", dates=("2026-08-27",)),
    )

    forward = build_components_with_anchored_semantics(
        items, AnchoredProvider((pair,), {pair: 0.8}), anchored_rules()
    )
    reverse = build_components_with_anchored_semantics(
        tuple(reversed(items)), AnchoredProvider((pair,), {pair: 0.8}), anchored_rules()
    )

    assert forward == reverse
