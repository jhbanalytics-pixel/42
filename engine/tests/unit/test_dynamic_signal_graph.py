"""Boundary tests for deterministic dynamic signal graph identity."""

from __future__ import annotations

from math import nan

import pytest
from src.analysis.open_intelligence import graph as graph_module
from src.analysis.open_intelligence.candidates import Observation
from src.analysis.open_intelligence.graph import (
    CLUSTER_BUILD_VERSION,
    AnchoredGraphRules,
    GraphRules,
    RelationshipVotes,
    SignalComponent,
    build_anchored_components,
    build_anchored_edges,
    build_components,
    build_edges,
    build_relationship_votes,
)


def obs(
    term,
    *,
    market="za",
    family="news",
    platform="web",
    row=None,
    co=(),
    creators=(),
    dates=(),
    weight=1.0,
    spike=False,
    candidate_type="keyword",
):
    return Observation(
        market=market,
        term=term,
        candidate_type=candidate_type,
        row_ids=(row or f"row_{term}",),
        source_families=(() if family is None else (family,)),
        platforms=(platform,),
        co_occurring_terms=co,
        creator_ids=creators,
        observed_dates=dates,
        weight=weight,
        verified_search_spike=spike,
    )


def rules():
    return GraphRules(0.7, 0.55, 2)


def scores(values):
    return {tuple(sorted(k)): v for k, v in values.items()}


def test_graph_rules_require_explicit_strict_values():
    with pytest.raises(TypeError):
        GraphRules()
    for args in (
        (-0.1, 0.5, 2),
        (1.1, 0.5, 2),
        (0.5, -0.1, 2),
        (0.5, 1.1, 2),
        (0.5, 0.5, -1),
        (0.5, 0.5, True),
    ):
        with pytest.raises(ValueError):
            GraphRules(*args)


@pytest.mark.parametrize(
    "field",
    [
        "co_occurrence",
        "embedding_similarity",
        "shared_creator",
        "temporal_overlap",
        "independent_source_family",
    ],
)
def test_each_vote_alone_never_qualifies_edge(field):
    kwargs = dict.fromkeys(RelationshipVotes.__dataclass_fields__, False)
    kwargs[field] = True
    votes = RelationshipVotes(**kwargs)
    assert votes.qualifying_count == 1


def test_relationship_votes_reject_non_boolean_values():
    with pytest.raises(ValueError, match="booleans"):
        RelationshipVotes(co_occurrence=1)


def test_semantic_vote_floor_is_inclusive():
    votes = build_relationship_votes(
        obs("alpha"), obs("beta"), semantic_similarity=0.7, rules=rules()
    )
    assert votes.embedding_similarity is True


def test_two_vote_floor_creates_edge():
    a = obs("alpha", co=("beta",))
    b = obs("beta", family="search")
    edges = build_edges([a, b], scores({("alpha", "beta"): 0.2}), rules())
    assert len(edges) == 1
    assert edges[0].votes.co_occurrence is True
    assert edges[0].votes.independent_source_family is True


def test_one_vote_never_creates_an_edge():
    a = obs("alpha", co=("beta",), family="news")
    b = obs("beta", family="news")
    assert build_edges([a, b], scores({("alpha", "beta"): 0.1}), rules()) == ()


def test_shared_neighbour_is_not_direct_co_occurrence():
    a = obs("alpha", co=("shared",), family="news")
    b = obs("beta", co=("shared",), family="search")
    votes = build_relationship_votes(a, b, semantic_similarity=0.1, rules=rules())
    assert votes.co_occurrence is False
    assert build_edges([a, b], scores({("alpha", "beta"): 0.1}), rules()) == ()


def test_direct_alias_mention_counts_as_co_occurrence():
    a = Observation(
        market="za",
        term="cyril ramaphosa",
        candidate_type="entity",
        row_ids=("row_a",),
        source_families=("news",),
        platforms=("web",),
        aliases=("ramaphosa",),
    )
    b = obs("cabinet reshuffle", co=("ramaphosa",), family="search")
    votes = build_relationship_votes(a, b, semantic_similarity=0.1, rules=rules())
    assert votes.co_occurrence is True


def test_same_vendor_social_platforms_do_not_vote_as_two_families():
    a = obs("alpha", family="ensemble", platform="tiktok")
    b = obs("beta", family="ensemble", platform="instagram")
    votes = build_relationship_votes(a, b, semantic_similarity=0.8, rules=rules())
    assert votes.embedding_similarity is True
    assert votes.independent_source_family is False


def test_cross_market_relationship_is_rejected_and_edges_never_cross():
    a = obs("alpha", market="za")
    b = obs("beta", market="ng")
    with pytest.raises(ValueError, match="market"):
        build_relationship_votes(a, b, semantic_similarity=1, rules=rules())
    assert build_edges([a, b], scores({}), rules()) == ()


@pytest.mark.parametrize(("right_date", "expected"), [("2026-08-23", True), ("2026-08-22", False)])
def test_temporal_vote_uses_explicit_inclusive_day_boundary(right_date, expected):
    a = obs("a", dates=("2026-08-25",))
    b = obs("b", dates=(right_date,))
    assert (
        build_relationship_votes(a, b, semantic_similarity=0, rules=rules()).temporal_overlap
        is expected
    )


def test_shared_creator_uses_canonical_creator_ids():
    a = obs("a", creators=("dj_max",))
    b = obs("b", creators=("dj_max",))
    assert build_relationship_votes(a, b, semantic_similarity=0, rules=rules()).shared_creator


def test_edge_order_is_deterministic_under_input_reversal():
    items = [obs("c", co=("b",), family="search"), obs("a", co=("b",)), obs("b", family="reddit")]
    sim = scores({("a", "b"): 0.8, ("b", "c"): 0.8, ("a", "c"): 0.1})
    assert build_edges(items, sim, rules()) == build_edges(reversed(items), sim, rules())


def test_chain_merge_is_rejected_when_component_diameter_breaks_floor():
    items = [
        obs("a", family="news"),
        obs("b", family="search"),
        obs("c", family="reddit", spike=True),
    ]
    sim = scores({("a", "b"): 0.8, ("b", "c"): 0.8, ("a", "c"): 0.31})
    components = build_components(items, sim, rules())
    assert tuple(c.terms for c in components) == (("a", "b"), ("c",))


def test_component_retention_requires_two_families_or_verified_spike():
    multi = build_components(
        [obs("a", family="news"), obs("b", family="search")], scores({("a", "b"): 0.9}), rules()
    )
    spike = build_components([obs("spike", family="search", spike=True)], scores({}), rules())
    thin = build_components([obs("thin", family="news")], scores({}), rules())
    assert len(multi) == 1
    assert len(spike) == 1
    assert thin == ()


def test_platform_diversity_never_substitutes_for_source_family_retention():
    same_vendor = [
        obs("alpha", family="ensemble", platform="tiktok", co=("beta",)),
        obs("beta", family="ensemble", platform="instagram"),
    ]
    no_families = [
        obs("gamma", family=None, platform="tiktok", co=("delta",)),
        obs("delta", family=None, platform="instagram"),
    ]
    assert build_components(same_vendor, scores({("alpha", "beta"): 0.9}), rules()) == ()
    assert build_components(no_families, scores({("delta", "gamma"): 0.9}), rules()) == ()


def test_component_exposes_receipts_families_platforms_creators_and_version():
    items = [
        obs("a", family="news", platform="web", row="row_b", creators=("maker_b",)),
        obs("b", family="search", platform="search", row="row_a", creators=("maker_a",)),
    ]
    component = build_components(items, scores({("a", "b"): 0.9}), rules())[0]
    assert component.row_receipts == ("row_a", "row_b")
    assert component.source_families == ("news", "search")
    assert component.platforms == ("search", "web")
    assert component.creator_ids == ("maker_a", "maker_b")
    assert component.build_version == CLUSTER_BUILD_VERSION == "hybrid_graph_v1"


def test_label_uses_edge_degree_then_weight_then_lexical_tie_break():
    items = [
        obs("alpha", family="news", weight=2),
        obs("beta", family="search", weight=5),
        obs("gamma", family="reddit", weight=5),
    ]
    sim = scores({("alpha", "beta"): 0.9, ("alpha", "gamma"): 0.9, ("beta", "gamma"): 0.9})
    component = build_components(items, sim, rules())[0]
    assert component.label == "beta"
    assert component.label_member_identity == "za|keyword|beta"
    tied = [obs("alpha", family="news"), obs("beta", family="search")]
    component = build_components(tied, scores({("alpha", "beta"): 0.9}), rules())[0]
    assert component.label == "alpha"
    assert component.label_member_identity == "za|keyword|alpha"


def test_label_identity_preserves_the_winning_observation_type():
    items = [
        obs("repair routine", family="news", candidate_type="hashtag", weight=2),
        obs("repair routine", family="search", candidate_type="keyword", weight=1),
    ]
    component = build_components(
        items,
        scores({("za|hashtag|repair routine", "za|keyword|repair routine"): 0.9}),
        rules(),
    )[0]
    assert component.label == "repair routine"
    assert component.label_member_identity == "za|hashtag|repair routine"


def test_legacy_component_infers_only_through_the_explicit_v1_adapter():
    adapter = getattr(graph_module, "adapt_legacy_v1_component", None)
    assert callable(adapter)
    component = adapter(
        market="za",
        member_identities=("za|keyword|alpha", "za|keyword|beta"),
        terms=("alpha", "beta"),
        row_receipts=("row_a", "row_b"),
        source_families=("news", "search"),
        platforms=("search", "web"),
        creator_ids=(),
        label="alpha",
    )
    assert component.label_member_identity == "za|keyword|alpha"
    with pytest.raises(ValueError, match="label member identity"):
        adapter(
            market="za",
            member_identities=("za|hashtag|alpha", "za|keyword|alpha"),
            terms=("alpha",),
            row_receipts=("row_a", "row_b"),
            source_families=("news", "search"),
            platforms=("search", "web"),
            creator_ids=(),
            label="alpha",
        )


@pytest.mark.parametrize("identity", [None, ""])
def test_hybrid_graph_v2_refuses_missing_label_member_authority(identity):
    with pytest.raises(ValueError, match="label member identity"):
        SignalComponent(
            market="za",
            member_identities=("za|keyword|alpha",),
            terms=("alpha",),
            row_receipts=("row_a",),
            source_families=("news", "search"),
            platforms=("search", "web"),
            creator_ids=(),
            label="alpha",
            label_member_identity=identity,
            build_version="hybrid_graph_v2",
        )


def test_component_and_edges_are_input_order_independent():
    items = [obs("a", family="news", row="z"), obs("b", family="search", row="a")]
    sim = scores({("a", "b"): 0.9})
    assert build_components(items, sim, rules()) == build_components(reversed(items), sim, rules())


def test_duplicate_identity_is_rejected_before_evidence_is_overwritten():
    duplicate = [
        obs("alpha", family="news", row="news_row"),
        obs("alpha", family="search", row="search_row"),
    ]
    with pytest.raises(ValueError, match="duplicate observation identity"):
        build_edges(duplicate, scores({}), rules())
    with pytest.raises(ValueError, match="duplicate observation identity"):
        build_components(duplicate, scores({}), rules())


@pytest.mark.parametrize("bad", [None, True, -0.1, 1.1, nan, "0.8"])
def test_malformed_semantic_scores_fail_closed(bad):
    a = obs("a")
    b = obs("b")
    with pytest.raises(ValueError, match="similarity"):
        build_relationship_votes(a, b, semantic_similarity=bad, rules=rules())


def test_semantic_callback_is_pure_injected_and_supported():
    a = obs("a", family="news")
    b = obs("b", family="search")
    calls = []

    def similarity(left, right):
        calls.append((left.term, right.term))
        return 0.9

    assert len(build_edges([b, a], similarity, rules())) == 1
    assert calls == [("a", "b")]


def test_invalid_semantic_provider_fails_closed_without_a_pair_to_score():
    with pytest.raises(ValueError, match="provider"):
        build_edges([], object(), rules())
    with pytest.raises(ValueError, match="provider"):
        build_components([obs("spike", spike=True)], object(), rules())


def test_component_materializes_each_semantic_pair_once_in_stable_order():
    items = [
        obs("c", family="reddit", spike=True),
        obs("a", family="news"),
        obs("b", family="search"),
    ]
    calls = []

    def similarity(left, right):
        calls.append((left.term, right.term))
        return 0.9

    assert build_components(items, similarity, rules())
    assert calls == [("a", "b"), ("a", "c"), ("b", "c")]


def test_label_prefers_graph_centrality_over_observation_weight():
    items = [
        obs("centre", family="news", weight=1),
        obs("heavy leaf", family="search", weight=100),
        obs("light leaf", family="reddit", weight=1),
    ]
    sim = scores(
        {
            ("centre", "heavy leaf"): 0.9,
            ("centre", "light leaf"): 0.9,
            ("heavy leaf", "light leaf"): 0.6,
        }
    )
    component = build_components(items, sim, rules())[0]
    assert component.label == "centre"


def anchored_rules(pair_ceiling=10, component_member_ceiling=50):
    return AnchoredGraphRules(0.5, 2, pair_ceiling, component_member_ceiling)


def anchored_pair(left, right):
    return tuple(
        sorted(
            (
                f"{left.market}|{left.candidate_type}|{left.term}",
                f"{right.market}|{right.candidate_type}|{right.term}",
            )
        )
    )


def test_anchored_graph_rejects_temporal_plus_independence_without_an_anchor():
    left = obs("alpha", family="news", dates=("2026-08-27",))
    right = obs("beta", family="search", dates=("2026-08-27",))
    pair = anchored_pair(left, right)

    assert build_anchored_edges((left, right), (pair,), {pair: 0.0}, anchored_rules()) == ()


def test_anchored_graph_accepts_each_anchor_with_one_support():
    semantic_left = obs("semantic alpha", family="news", dates=("2026-08-27",))
    semantic_right = obs("semantic beta", family="news", dates=("2026-08-27",))
    semantic_pair = anchored_pair(semantic_left, semantic_right)
    assert (
        len(
            build_anchored_edges(
                (semantic_left, semantic_right),
                (semantic_pair,),
                {semantic_pair: 0.5},
                anchored_rules(),
            )
        )
        == 1
    )

    co_left = obs("co alpha", family="news", co=("co beta",))
    co_right = obs("co beta", family="search")
    co_pair = anchored_pair(co_left, co_right)
    assert (
        len(build_anchored_edges((co_left, co_right), (co_pair,), {co_pair: 0.0}, anchored_rules()))
        == 1
    )

    creator_left = obs("creator alpha", creators=("maker",), dates=("2026-08-27",))
    creator_right = obs("creator beta", creators=("maker",), dates=("2026-08-27",))
    creator_pair = anchored_pair(creator_left, creator_right)
    assert (
        len(
            build_anchored_edges(
                (creator_left, creator_right),
                (creator_pair,),
                {creator_pair: 0.0},
                anchored_rules(),
            )
        )
        == 1
    )


def test_anchored_graph_refuses_a_component_above_the_member_ceiling():
    items = (
        obs("alpha", family="news", dates=("2026-08-27",)),
        obs("beta", family="search", dates=("2026-08-27",)),
        obs("gamma", family="reddit", dates=("2026-08-27",)),
    )
    pairs = tuple(
        anchored_pair(left, right)
        for left, right in ((items[0], items[1]), (items[0], items[2]), (items[1], items[2]))
    )
    semantic = dict.fromkeys(pairs, 1.0)

    with pytest.raises(ValueError, match="composition_component_ceiling_exceeded"):
        build_anchored_components(
            items,
            pairs,
            semantic,
            anchored_rules(component_member_ceiling=2),
        )


def test_anchored_graph_rejects_bridge_chaining_without_complete_link_coherence():
    items = (
        obs("alpha", family="news", dates=("2026-08-27",)),
        obs("beta", family="search", dates=("2026-08-27",)),
        obs("gamma", family="reddit", dates=("2026-08-27",)),
    )
    alpha_beta = anchored_pair(items[0], items[1])
    beta_gamma = anchored_pair(items[1], items[2])
    pairs = (alpha_beta, beta_gamma)
    semantic = {alpha_beta: 0.8, beta_gamma: 0.8}

    components = build_anchored_components(items, pairs, semantic, anchored_rules())

    assert tuple(component.terms for component in components) == (("alpha", "beta"),)


def test_anchored_complete_link_result_is_stable_under_input_reversal():
    items = (
        obs("alpha", family="news", dates=("2026-08-27",)),
        obs("beta", family="search", dates=("2026-08-27",)),
        obs("gamma", family="reddit", dates=("2026-08-27",)),
    )
    pairs = (anchored_pair(items[0], items[1]), anchored_pair(items[1], items[2]))
    semantic = dict.fromkeys(pairs, 0.8)

    assert build_anchored_components(
        items, pairs, semantic, anchored_rules()
    ) == build_anchored_components(tuple(reversed(items)), pairs, semantic, anchored_rules())


def test_anchored_graph_is_input_order_independent():
    left = obs("alpha", family="news", dates=("2026-08-27",))
    right = obs("beta", family="search", dates=("2026-08-27",))
    pair = anchored_pair(left, right)
    scores_by_pair = {pair: 0.8}

    assert build_anchored_components(
        (left, right), (pair,), scores_by_pair, anchored_rules()
    ) == build_anchored_components((right, left), (pair,), scores_by_pair, anchored_rules())
