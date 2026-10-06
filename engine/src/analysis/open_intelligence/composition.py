"""Strict semantic admission before dynamic signal graph composition."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from itertools import combinations
from typing import Protocol

from src.analysis.open_intelligence.candidates import Observation
from src.analysis.open_intelligence.graph import (
    AnchoredGraphRules,
    GraphRules,
    SignalComponent,
    _provenance_observations,
    build_anchored_components,
    build_components,
    build_provenance_components,
)


class SemanticSimilarityProvider(Protocol):
    version: str

    def similarities(
        self,
        observations: tuple[Observation, ...],
    ) -> Mapping[tuple[str, str], float]: ...


class AnchoredSemanticSimilarityProvider(Protocol):
    version: str

    def candidate_pairs(
        self,
        observations: tuple[Observation, ...],
    ) -> Iterable[tuple[str, str]]: ...

    def similarities(
        self,
        observations: tuple[Observation, ...],
        candidate_pairs: tuple[tuple[str, str], ...],
    ) -> Mapping[tuple[str, str], float]: ...


def _identity(observation: Observation) -> str:
    return f"{observation.market}|{observation.candidate_type}|{observation.term}"


def _ordered_observations(observations: Iterable[Observation]) -> tuple[Observation, ...]:
    try:
        items = tuple(observations)
    except TypeError as error:
        raise ValueError("observations must be iterable") from error
    if any(not isinstance(item, Observation) for item in items):
        raise ValueError("observations must use canonical observation rows")
    ordered = tuple(sorted(items, key=_identity))
    identities = tuple(_identity(item) for item in ordered)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate observation identity")
    return ordered


def _expected_pairs(observations: tuple[Observation, ...]) -> set[tuple[str, str]]:
    return {
        (_identity(left), _identity(right))
        for left, right in combinations(observations, 2)
        if left.market == right.market
    }


def _validated_scores(
    raw_scores: object,
    observations: tuple[Observation, ...],
) -> dict[tuple[str, str], float]:
    if not isinstance(raw_scores, Mapping):
        raise ValueError("semantic similarities must be a mapping")
    by_identity = {_identity(item): item for item in observations}
    scores: dict[tuple[str, str], float] = {}
    for key, value in raw_scores.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or any(not isinstance(item, str) for item in key)
        ):
            raise ValueError("semantic pair key is invalid")
        left, right = key
        if left == right:
            raise ValueError("self semantic pair is invalid")
        if left not in by_identity or right not in by_identity:
            raise ValueError("extra semantic pair")
        if by_identity[left].market != by_identity[right].market:
            raise ValueError("cross-market semantic pair is invalid")
        if left > right:
            raise ValueError("ordered semantic pair is required")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
        ):
            raise ValueError("semantic similarity must be finite within zero and one")
        scores[key] = float(value)

    expected = _expected_pairs(observations)
    actual = set(scores)
    if missing := expected - actual:
        raise ValueError(f"missing semantic pair: {min(missing)}")
    if extra := actual - expected:
        raise ValueError(f"extra semantic pair: {min(extra)}")
    return scores


def build_components_with_validated_semantics(
    observations: Iterable[Observation],
    semantic_provider: SemanticSimilarityProvider,
    rules: GraphRules,
) -> tuple[SignalComponent, ...]:
    ordered = _ordered_observations(observations)
    version = getattr(semantic_provider, "version", None)
    provider_method = getattr(semantic_provider, "similarities", None)
    if not isinstance(version, str) or not version.strip() or not callable(provider_method):
        raise ValueError("semantic provider is invalid")
    scores = _validated_scores(provider_method(ordered), ordered)
    return build_components(ordered, scores, rules)


def build_anchored_candidate_pairs(
    observations: tuple[Observation, ...],
    provider_pairs: object,
    pair_ceiling: int,
) -> tuple[tuple[str, str], ...]:
    by_identity = {_identity(item): item for item in observations}
    pairs: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}

    def add(left_id: str, right_id: str) -> None:
        pair = tuple(sorted((left_id, right_id)))
        if pair[0] == pair[1]:
            raise ValueError("self candidate pair is invalid")
        if pair[0] not in by_identity or pair[1] not in by_identity:
            raise ValueError("candidate pair identity is unknown")
        market = by_identity[pair[0]].market
        if market != by_identity[pair[1]].market:
            raise ValueError("candidate pair cannot cross market")
        if pair in pairs:
            return
        pairs.add(pair)
        counts[market] = counts.get(market, 0) + 1
        if counts[market] > pair_ceiling:
            raise ValueError("composition_pair_ceiling_exceeded")

    try:
        supplied = tuple(provider_pairs)
    except TypeError as error:
        raise ValueError("semantic candidate pairs must be iterable") from error
    if len(supplied) != len(set(supplied)):
        raise ValueError("duplicate semantic candidate pair")
    for pair in supplied:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or any(not isinstance(identity, str) for identity in pair)
        ):
            raise ValueError("semantic candidate pair is invalid")
        if pair[0] >= pair[1]:
            raise ValueError("ordered semantic candidate pair is required")
        add(*pair)

    names: dict[tuple[str, str], set[str]] = {}
    creators: dict[tuple[str, str], set[str]] = {}
    for observation in observations:
        identity = _identity(observation)
        for name in {observation.term, *observation.aliases}:
            names.setdefault((observation.market, name), set()).add(identity)
        for creator in observation.creator_ids:
            creators.setdefault((observation.market, creator), set()).add(identity)

    for observation in observations:
        identity = _identity(observation)
        for co_occurring_term in observation.co_occurring_terms:
            for other in names.get((observation.market, co_occurring_term), ()):
                if other != identity:
                    add(identity, other)
    for identities in creators.values():
        for left_id, right_id in combinations(sorted(identities), 2):
            add(left_id, right_id)
    return tuple(sorted(pairs))


def _validated_anchored_scores(
    raw_scores: object,
    candidate_pairs: tuple[tuple[str, str], ...],
) -> dict[tuple[str, str], float]:
    if not isinstance(raw_scores, Mapping):
        raise ValueError("semantic similarities must be a mapping")
    expected = set(candidate_pairs)
    actual = set(raw_scores)
    if missing := expected - actual:
        raise ValueError(f"missing semantic pair: {min(missing)}")
    if extra := actual - expected:
        raise ValueError(f"extra semantic pair: {min(extra)}")
    scores = {}
    for pair in candidate_pairs:
        value = raw_scores[pair]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
        ):
            raise ValueError("semantic similarity must be finite within zero and one")
        scores[pair] = float(value)
    return scores


def build_components_with_anchored_semantics(
    observations: Iterable[Observation],
    semantic_provider: AnchoredSemanticSimilarityProvider,
    rules: AnchoredGraphRules,
) -> tuple[SignalComponent, ...]:
    ordered, candidate_pairs, scores = _anchored_composition_inputs(
        observations, semantic_provider, rules
    )
    return build_anchored_components(ordered, candidate_pairs, scores, rules)


def _anchored_composition_inputs(observations, semantic_provider, rules):
    ordered = _ordered_observations(observations)
    version = getattr(semantic_provider, "version", None)
    pair_method = getattr(semantic_provider, "candidate_pairs", None)
    score_method = getattr(semantic_provider, "similarities", None)
    if (
        not isinstance(version, str)
        or not version.strip()
        or not callable(pair_method)
        or not callable(score_method)
    ):
        raise ValueError("anchored semantic provider is invalid")
    if not isinstance(rules, AnchoredGraphRules):
        raise ValueError("anchored graph rules are invalid")
    candidate_pairs = build_anchored_candidate_pairs(
        ordered,
        pair_method(ordered),
        rules.pair_ceiling,
    )
    scores = _validated_anchored_scores(
        score_method(ordered, candidate_pairs),
        candidate_pairs,
    )
    return ordered, candidate_pairs, scores


def build_components_with_provenance_semantics(
    observations: Iterable[Observation],
    semantic_provider: AnchoredSemanticSimilarityProvider,
    rules: AnchoredGraphRules,
    *,
    source_provenance_by_member: Mapping[str, object],
) -> tuple[SignalComponent, ...]:
    resolved, _ = _provenance_observations(observations, source_provenance_by_member)
    ordered, candidate_pairs, scores = _anchored_composition_inputs(
        resolved, semantic_provider, rules
    )
    return build_provenance_components(
        ordered,
        candidate_pairs,
        scores,
        rules,
        source_provenance_by_member=source_provenance_by_member,
    )
