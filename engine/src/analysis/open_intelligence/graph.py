"""Deterministic hybrid graph identity for canonical observations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date

CLUSTER_BUILD_VERSION = "hybrid_graph_v1"
ANCHORED_CLUSTER_BUILD_VERSION = "hybrid_graph_v2"
PROVENANCE_CLUSTER_BUILD_VERSION = "hybrid_graph_v3"


@dataclass(frozen=True)
class GraphRules:
    semantic_vote_floor: float
    component_similarity_floor: float
    temporal_overlap_days: int

    def __post_init__(self):
        for value in (self.semantic_vote_floor, self.component_similarity_floor):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 1
            ):
                raise ValueError("similarity floor must be within zero and one")
        if (
            isinstance(self.temporal_overlap_days, bool)
            or not isinstance(self.temporal_overlap_days, int)
            or self.temporal_overlap_days < 0
        ):
            raise ValueError("temporal overlap days must be a nonnegative integer")


@dataclass(frozen=True)
class AnchoredGraphRules:
    semantic_anchor_floor: float
    temporal_overlap_days: int
    pair_ceiling: int
    component_member_ceiling: int

    def __post_init__(self):
        if (
            isinstance(self.semantic_anchor_floor, bool)
            or not isinstance(self.semantic_anchor_floor, (int, float))
            or not math.isfinite(float(self.semantic_anchor_floor))
            or not 0 <= float(self.semantic_anchor_floor) <= 1
        ):
            raise ValueError("semantic anchor floor must be within zero and one")
        for value, label in (
            (self.temporal_overlap_days, "temporal overlap days"),
            (self.pair_ceiling, "pair ceiling"),
            (self.component_member_ceiling, "component member ceiling"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")


@dataclass(frozen=True)
class RelationshipVotes:
    co_occurrence: bool = False
    embedding_similarity: bool = False
    shared_creator: bool = False
    temporal_overlap: bool = False
    independent_source_family: bool = False

    def __post_init__(self):
        if any(type(getattr(self, f)) is not bool for f in self.__dataclass_fields__):
            raise ValueError("relationship votes must be booleans")

    @property
    def qualifying_count(self):
        return sum(getattr(self, f) for f in self.__dataclass_fields__)


@dataclass(frozen=True, order=True)
class GraphEdge:
    market: str
    left_identity: str
    right_identity: str
    votes: RelationshipVotes
    semantic_similarity: float


@dataclass(frozen=True, order=True)
class SignalComponent:
    market: str
    member_identities: tuple[str, ...]
    terms: tuple[str, ...]
    row_receipts: tuple[str, ...]
    source_families: tuple[str, ...]
    platforms: tuple[str, ...]
    creator_ids: tuple[str, ...]
    label: str
    label_member_identity: str | None = None
    build_version: str = CLUSTER_BUILD_VERSION

    def __post_init__(self) -> None:
        identity = self.label_member_identity
        if identity is None and self.build_version == CLUSTER_BUILD_VERSION:
            return
        if not isinstance(identity, str) or not identity:
            raise ValueError("label member identity is unavailable")
        if identity not in self.member_identities or not identity.startswith(f"{self.market}|"):
            raise ValueError("label member identity is not a component member")


def adapt_legacy_v1_component(
    *,
    market: str,
    member_identities: tuple[str, ...],
    terms: tuple[str, ...],
    row_receipts: tuple[str, ...],
    source_families: tuple[str, ...],
    platforms: tuple[str, ...],
    creator_ids: tuple[str, ...],
    label: str,
) -> SignalComponent:
    matches = tuple(
        member
        for member in member_identities
        if len(parts := member.split("|", 2)) == 3 and parts[0] == market and parts[2] == label
    )
    if len(matches) != 1:
        raise ValueError("label member identity is unavailable or ambiguous")
    return SignalComponent(
        market=market,
        member_identities=member_identities,
        terms=terms,
        row_receipts=row_receipts,
        source_families=source_families,
        platforms=platforms,
        creator_ids=creator_ids,
        label=label,
        label_member_identity=matches[0],
        build_version=CLUSTER_BUILD_VERSION,
    )


def _identity(o):
    return f"{o.market}|{o.candidate_type}|{o.term}"


def wave1_pair_is_independent(
    left_vendor: str,
    left_channel: str,
    right_vendor: str,
    right_channel: str,
) -> bool:
    values = (left_vendor, left_channel, right_vendor, right_channel)
    if any(not isinstance(value, str) or not value for value in values):
        return False
    return left_vendor != right_vendor and left_channel != right_channel


def _source_pair_is_independent(left, right) -> bool:
    values = (
        left.vendor_family,
        left.channel_family,
        right.vendor_family,
        right.channel_family,
    )
    if any(value is not None for value in values):
        if not all(value is not None for value in values):
            return False
        return wave1_pair_is_independent(*values)
    return bool(
        left.source_families
        and right.source_families
        and len(set(left.source_families) | set(right.source_families)) >= 2
    )


def wave1_independence_count(rows) -> int:
    values = []
    for row in rows:
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 3
            or any(not isinstance(value, str) or not value for value in row)
        ):
            raise ValueError("Wave 1 source identity row is invalid")
        values.append(tuple(row))
    ordered = tuple(sorted(values, key=lambda item: item[0]))
    channel_owner: dict[str, str] = {}

    def assign(vendor: str, seen: set[str]) -> bool:
        for _, row_vendor, channel in ordered:
            if row_vendor != vendor or channel in seen:
                continue
            seen.add(channel)
            owner = channel_owner.get(channel)
            if owner is None or assign(owner, seen):
                channel_owner[channel] = vendor
                return True
        return False

    return sum(assign(vendor, set()) for vendor in sorted({row[1] for row in ordered}))


def _ordered_observations(observations):
    items = tuple(sorted(observations, key=_identity))
    identities = tuple(_identity(item) for item in items)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate observation identity")
    return items


def _score(v):
    if (
        isinstance(v, bool)
        or not isinstance(v, (int, float))
        or not math.isfinite(float(v))
        or not 0 <= float(v) <= 1
    ):
        raise ValueError("semantic similarity must be finite within zero and one")
    return float(v)


def _validate_semantic_provider(provider):
    if not callable(provider) and not isinstance(provider, Mapping):
        raise ValueError("semantic similarity provider is invalid")


def _semantic(provider, a, b):
    _validate_semantic_provider(provider)
    if callable(provider):
        return _score(provider(a, b))
    keys = (tuple(sorted((_identity(a), _identity(b)))), tuple(sorted((a.term, b.term))))
    for key in keys:
        if key in provider:
            return _score(provider[key])
    return 0.0


def _temporal(a, b, days):
    return any(
        abs((date.fromisoformat(x) - date.fromisoformat(y)).days) <= days
        for x in a.observed_dates
        for y in b.observed_dates
    )


def _direct_co_occurrence(left, right):
    left_names = {left.term, *left.aliases}
    right_names = {right.term, *right.aliases}
    return bool(
        left_names.intersection(right.co_occurring_terms)
        or right_names.intersection(left.co_occurring_terms)
    )


def build_relationship_votes(left, right, *, semantic_similarity, rules):
    if left.market != right.market:
        raise ValueError("relationship cannot cross market")
    score = _score(semantic_similarity)
    co = _direct_co_occurrence(left, right)
    families = _source_pair_is_independent(left, right)
    return RelationshipVotes(
        co,
        score >= rules.semantic_vote_floor,
        bool(set(left.creator_ids) & set(right.creator_ids)),
        _temporal(left, right, rules.temporal_overlap_days),
        families,
    )


def build_edges(observations, semantic_similarity, rules):
    _validate_semantic_provider(semantic_similarity)
    items = _ordered_observations(observations)
    out = []
    for i, left in enumerate(items):
        for right in items[i + 1 :]:
            if left.market != right.market:
                continue
            score = _semantic(semantic_similarity, left, right)
            votes = build_relationship_votes(left, right, semantic_similarity=score, rules=rules)
            if votes.qualifying_count >= 2:
                out.append(GraphEdge(left.market, _identity(left), _identity(right), votes, score))
    return tuple(sorted(out))


def _materialize_semantic_scores(items, semantic_similarity):
    scores = {}
    for i, left in enumerate(items):
        for right in items[i + 1 :]:
            if left.market == right.market:
                key = tuple(sorted((_identity(left), _identity(right))))
                scores[key] = _semantic(semantic_similarity, left, right)
    return scores


def build_components(observations, semantic_similarity, rules):
    _validate_semantic_provider(semantic_similarity)
    items = _ordered_observations(observations)
    by_id = {_identity(o): o for o in items}
    parent = {k: k for k in by_id}
    members = {k: {k} for k in by_id}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    semantic_scores = _materialize_semantic_scores(items, semantic_similarity)
    edges = build_edges(items, semantic_scores, rules)
    for edge in edges:
        a, b = find(edge.left_identity), find(edge.right_identity)
        if a == b:
            continue
        if any(
            _semantic(semantic_scores, by_id[x], by_id[y]) < rules.component_similarity_floor
            for x in members[a]
            for y in members[b]
        ):
            continue
        keep, drop = sorted((a, b))
        parent[drop] = keep
        members[keep] |= members.pop(drop)
    output = []
    for ids in sorted(tuple(sorted(v)) for v in members.values()):
        obs = [by_id[x] for x in ids]
        families = tuple(sorted({f for o in obs for f in o.source_families}))
        if len(families) < 2 and not any(o.verified_search_spike for o in obs):
            continue
        degrees = dict.fromkeys(ids, 0)
        for e in edges:
            if e.left_identity in degrees and e.right_identity in degrees:
                degrees[e.left_identity] += e.votes.qualifying_count
                degrees[e.right_identity] += e.votes.qualifying_count
        label_observation = min(obs, key=lambda o: (-degrees[_identity(o)], -o.weight, o.term))
        output.append(
            SignalComponent(
                obs[0].market,
                ids,
                tuple(sorted({o.term for o in obs})),
                tuple(sorted({r for o in obs for r in o.row_ids})),
                families,
                tuple(sorted({p for o in obs for p in o.platforms})),
                tuple(sorted({c for o in obs for c in o.creator_ids})),
                label_observation.term,
                _identity(label_observation),
            )
        )
    return tuple(sorted(output))


def _anchored_pairs(items, candidate_pairs, pair_ceiling):
    by_id = {_identity(item): item for item in items}
    try:
        supplied = tuple(candidate_pairs)
    except TypeError as error:
        raise ValueError("candidate pairs must be iterable") from error
    pairs = []
    seen = set()
    counts: dict[str, int] = {}
    for pair in supplied:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or any(not isinstance(identity, str) for identity in pair)
        ):
            raise ValueError("candidate pair is invalid")
        left_id, right_id = pair
        if left_id >= right_id:
            raise ValueError("ordered distinct candidate pair is required")
        if left_id not in by_id or right_id not in by_id:
            raise ValueError("candidate pair identity is unknown")
        market = by_id[left_id].market
        if market != by_id[right_id].market:
            raise ValueError("candidate pair cannot cross market")
        if pair in seen:
            raise ValueError("duplicate candidate pair")
        seen.add(pair)
        counts[market] = counts.get(market, 0) + 1
        if counts[market] > pair_ceiling:
            raise ValueError("composition_pair_ceiling_exceeded")
        pairs.append(pair)
    return tuple(sorted(pairs))


def _anchored_scores(candidate_pairs, semantic_scores):
    if not isinstance(semantic_scores, Mapping):
        raise ValueError("semantic similarities must be a mapping")
    expected = set(candidate_pairs)
    actual = set(semantic_scores)
    if missing := expected - actual:
        raise ValueError(f"missing semantic pair: {min(missing)}")
    if extra := actual - expected:
        raise ValueError(f"extra semantic pair: {min(extra)}")
    return {pair: _score(semantic_scores[pair]) for pair in candidate_pairs}


def build_anchored_edges(observations, candidate_pairs, semantic_scores, rules):
    if not isinstance(rules, AnchoredGraphRules):
        raise ValueError("anchored graph rules are invalid")
    items = _ordered_observations(observations)
    pairs = _anchored_pairs(items, candidate_pairs, rules.pair_ceiling)
    scores = _anchored_scores(pairs, semantic_scores)
    return _anchored_edges(items, pairs, scores, rules)


def _anchored_edges(items, pairs, scores, rules, source_pairs_by_member=None):
    by_id = {_identity(item): item for item in items}
    edges = []
    for left_id, right_id in pairs:
        left = by_id[left_id]
        right = by_id[right_id]
        score = scores[(left_id, right_id)]
        votes = RelationshipVotes(
            co_occurrence=_direct_co_occurrence(left, right),
            embedding_similarity=score >= rules.semantic_anchor_floor,
            shared_creator=bool(set(left.creator_ids) & set(right.creator_ids)),
            temporal_overlap=_temporal(left, right, rules.temporal_overlap_days),
            independent_source_family=(
                _source_pair_is_independent(left, right)
                if source_pairs_by_member is None
                else any(
                    wave1_pair_is_independent(*left_pair, *right_pair)
                    for left_pair in source_pairs_by_member[left_id]
                    for right_pair in source_pairs_by_member[right_id]
                )
            ),
        )
        anchored = votes.co_occurrence or votes.embedding_similarity or votes.shared_creator
        supported = votes.temporal_overlap or votes.independent_source_family
        if anchored and supported:
            edges.append(GraphEdge(left.market, left_id, right_id, votes, score))
    return tuple(sorted(edges))


def build_anchored_components(observations, candidate_pairs, semantic_scores, rules):
    if not isinstance(rules, AnchoredGraphRules):
        raise ValueError("anchored graph rules are invalid")
    items = _ordered_observations(observations)
    pairs = _anchored_pairs(items, candidate_pairs, rules.pair_ceiling)
    scores = _anchored_scores(pairs, semantic_scores)
    edges = build_anchored_edges(items, pairs, scores, rules)
    return _anchored_components(items, edges, rules)


def _anchored_components(
    items,
    edges,
    rules,
    *,
    source_pairs_by_member=None,
    build_version=ANCHORED_CLUSTER_BUILD_VERSION,
):
    by_id = {_identity(item): item for item in items}
    parent = {identity: identity for identity in by_id}
    members = {identity: {identity} for identity in by_id}

    def find(identity):
        while parent[identity] != identity:
            identity = parent[identity]
        return identity

    qualified_pairs = {(edge.left_identity, edge.right_identity) for edge in edges}
    for edge in edges:
        left_root = find(edge.left_identity)
        right_root = find(edge.right_identity)
        if left_root == right_root:
            continue
        if any(
            tuple(sorted((left_identity, right_identity))) not in qualified_pairs
            for left_identity in members[left_root]
            for right_identity in members[right_root]
        ):
            continue
        combined = members[left_root] | members[right_root]
        if len(combined) > rules.component_member_ceiling:
            sample = ",".join(by_id[identity].term for identity in sorted(combined)[:8])
            raise ValueError(
                "composition_component_ceiling_exceeded:"
                f"market={edge.market}:members={len(combined)}:sample={sample}"
            )
        keep, drop = sorted((left_root, right_root))
        parent[drop] = keep
        members[keep] = combined
        members.pop(drop)

    output = []
    for identities in sorted(tuple(sorted(group)) for group in members.values()):
        component_edges = tuple(
            edge
            for edge in edges
            if edge.left_identity in identities and edge.right_identity in identities
        )
        if not component_edges:
            continue
        observations_in_component = [by_id[identity] for identity in identities]
        families = tuple(
            sorted(
                {
                    family
                    for observation in observations_in_component
                    for family in observation.source_families
                }
            )
        )
        independent_count = (
            len(families)
            if source_pairs_by_member is None
            else wave1_independence_count(
                (f"{identity}:{index}", vendor, channel)
                for identity in identities
                for index, (vendor, channel) in enumerate(source_pairs_by_member[identity])
            )
        )
        if independent_count < 2 and not any(
            observation.verified_search_spike
            and (
                source_pairs_by_member is None
                or ("google_trends", "search") in source_pairs_by_member[_identity(observation)]
            )
            for observation in observations_in_component
        ):
            continue
        degrees = dict.fromkeys(identities, 0)
        for edge in component_edges:
            degrees[edge.left_identity] += 1
            degrees[edge.right_identity] += 1
        label_observation = min(
            observations_in_component,
            key=lambda observation: (
                -degrees[_identity(observation)],
                -observation.weight,
                observation.term,
            ),
        )
        output.append(
            SignalComponent(
                observations_in_component[0].market,
                identities,
                tuple(sorted({item.term for item in observations_in_component})),
                tuple(sorted({row for item in observations_in_component for row in item.row_ids})),
                families,
                tuple(
                    sorted(
                        {
                            platform
                            for item in observations_in_component
                            for platform in item.platforms
                        }
                    )
                ),
                tuple(
                    sorted(
                        {
                            creator
                            for item in observations_in_component
                            for creator in item.creator_ids
                        }
                    )
                ),
                label_observation.term,
                _identity(label_observation),
                build_version,
            )
        )
    return tuple(sorted(output))


def _provenance_observations(observations, source_provenance_by_member):
    from src.analysis.open_intelligence.source_provenance import encode_source_provenance

    items = _ordered_observations(observations)
    identities = {_identity(item) for item in items}
    if (
        not isinstance(source_provenance_by_member, Mapping)
        or set(source_provenance_by_member) != identities
    ):
        raise ValueError("source_provenance_missing")
    snapshots = set()
    records = {}
    pairs_by_member = {}
    projected = []
    for item in items:
        identity = _identity(item)
        envelope = source_provenance_by_member[identity]
        encode_source_provenance(envelope)
        snapshots.add(envelope["source_snapshot_digest"])
        pairs = set()
        for pair in envelope["pairs"]:
            source_pair = (pair["vendor_family"], pair["channel_family"])
            pairs.add(source_pair)
            for ref in pair["sample_refs"]:
                key = (item.market, ref["row_id"])
                binding = (*source_pair, pair["source_table"], ref["row_digest"])
                if key in records and records[key] != binding:
                    raise ValueError("source_provenance_conflict")
                records[key] = binding
        pairs_by_member[identity] = tuple(sorted(pairs))
        single = next(iter(pairs)) if len(pairs) == 1 else (None, None)
        projected.append(
            replace(
                item,
                source_families=tuple(sorted({channel for _, channel in pairs})),
                vendor_family=single[0],
                channel_family=single[1],
            )
        )
    if len(snapshots) > 1:
        raise ValueError("source_provenance_snapshot_mismatch")
    return tuple(projected), pairs_by_member


def build_provenance_edges(
    observations, candidate_pairs, semantic_scores, rules, *, source_provenance_by_member
):
    if not isinstance(rules, AnchoredGraphRules):
        raise ValueError("anchored graph rules are invalid")
    items, source_pairs = _provenance_observations(observations, source_provenance_by_member)
    pairs = _anchored_pairs(items, candidate_pairs, rules.pair_ceiling)
    scores = _anchored_scores(pairs, semantic_scores)
    return _anchored_edges(items, pairs, scores, rules, source_pairs)


def build_provenance_components(
    observations, candidate_pairs, semantic_scores, rules, *, source_provenance_by_member
):
    if not isinstance(rules, AnchoredGraphRules):
        raise ValueError("anchored graph rules are invalid")
    items, source_pairs = _provenance_observations(observations, source_provenance_by_member)
    pairs = _anchored_pairs(items, candidate_pairs, rules.pair_ceiling)
    scores = _anchored_scores(pairs, semantic_scores)
    edges = _anchored_edges(items, pairs, scores, rules, source_pairs)
    return _anchored_components(
        items,
        edges,
        rules,
        source_pairs_by_member=source_pairs,
        build_version=PROVENANCE_CLUSTER_BUILD_VERSION,
    )
