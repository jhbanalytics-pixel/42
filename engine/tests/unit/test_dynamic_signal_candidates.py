"""Boundary tests for canonical dynamic discovery observations."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from src.analysis.open_intelligence.candidates import (
    SOURCE_FAMILY_MAP_VERSION,
    CandidateInputs,
    Observation,
    canonicalize_observations,
    canonicalize_observations_with_mapping,
    extract_event_observations,
    extract_seed_candidate_observations,
    extract_seed_graph_observations,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "open_intelligence" / "v2" / "dynamic_signal_ready.json"


def event_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ledger_id": "ledger_001",
        "market": "za",
        "entity_key": "cyril ramaphosa",
        "entity_aliases": ["Ramaphosa", "Cyril Ramaphosa"],
        "event_kind": "entity",
        "corroborating_sources": ["rss", "gdelt"],
    }
    row.update(overrides)
    return row


def seed_graph_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "market": "ng",
        "term": "sapa",
        "term_type": "slang",
        "platform": "reddit",
        "sample_row_ids": ["raw_002", "raw_001"],
    }
    row.update(overrides)
    return row


def seed_candidate_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "candidate_id": "candidate_001",
        "market": "ke",
        "candidate_value": "bei ya unga",
        "candidate_type": "keyword",
        "source": "seed_graph",
        "sample_row_ids": ["raw_004", "raw_003"],
    }
    row.update(overrides)
    return row


def observation(market, term, candidate_type, family, platform, row_id, **kwargs):
    kwargs.setdefault("row_ids", (row_id,))
    kwargs.setdefault("source_families", () if family == "seed_graph" else (family,))
    kwargs.setdefault("platforms", (platform,))
    return Observation(
        market=market,
        term=term,
        candidate_type=candidate_type,
        **kwargs,
    )


def test_observation_exposes_no_atomic_source_platform_or_row_pair() -> None:
    item = observation("za", "term", "keyword", "news", "web", "row")
    assert not hasattr(item, "source_family")
    assert not hasattr(item, "platform")
    assert not hasattr(item, "row_id")


def test_observation_and_candidate_inputs_are_frozen() -> None:
    item = observation("za", "Ramaphosa", "entity", "rss", "rss", "row_1")
    inputs = CandidateInputs(event_ledger=(item,))
    with pytest.raises(FrozenInstanceError):
        item.term = "changed"
    with pytest.raises(FrozenInstanceError):
        inputs.event_ledger = ()


def test_event_adapter_maps_current_types_and_channel_families() -> None:
    observations = extract_event_observations(
        [
            event_row(),
            event_row(
                ledger_id="ledger_002",
                entity_key="load shedding",
                entity_aliases=[],
                event_kind="search_term",
                corroborating_sources=["trends"],
            ),
            event_row(
                ledger_id="ledger_003",
                entity_key="springboks final",
                entity_aliases=[],
                event_kind="headline",
                corroborating_sources=["youtube", "music"],
            ),
        ]
    )
    assert [(item.term, item.candidate_type) for item in observations] == [
        ("cyril ramaphosa", "entity"),
        ("load shedding", "keyword"),
        ("springboks final", "headline"),
    ]
    assert SOURCE_FAMILY_MAP_VERSION == "channel_family_v2"
    assert observations[0].source_families == ("news",)
    assert observations[2].source_families == ("music", "youtube")


def test_event_family_aliases_cannot_manufacture_independence() -> None:
    observations = extract_event_observations(
        [
            event_row(corroborating_sources=["tiktok", "instagram"]),
            event_row(
                ledger_id="ledger_brand24",
                entity_key="brand signal",
                corroborating_sources=["brand24", "brand24_legacy"],
            ),
            event_row(
                ledger_id="ledger_search",
                entity_key="search signal",
                event_kind="search_term",
                corroborating_sources=["trends", "bigquery_trends"],
            ),
        ]
    )
    by_term = {item.term: item for item in observations}
    assert by_term["cyril ramaphosa"].source_families == ("short_video",)
    assert by_term["brand signal"].source_families == ("brand24",)
    assert by_term["search signal"].source_families == ("search",)


def test_unknown_and_other_event_families_fail_closed() -> None:
    for family in ("vendor_a", "other", "facebook"):
        with pytest.raises(ValueError, match="source family"):
            extract_event_observations([event_row(corroborating_sources=[family])])


@pytest.mark.parametrize(
    ("term_type", "candidate_type"),
    [
        ("token", "keyword"),
        ("slang", "slang"),
        ("hashtag", "hashtag"),
        ("handle", "creator"),
        ("channel", "creator"),
        ("music", "sound"),
        ("entity", "entity"),
    ],
)
def test_seed_graph_adapter_maps_current_term_types(term_type: str, candidate_type: str) -> None:
    observation = extract_seed_graph_observations([seed_graph_row(term_type=term_type)])[0]
    assert observation.candidate_type == candidate_type
    assert observation.source_families == ("reddit",)
    assert observation.platforms == ("reddit",)
    assert observation.row_ids == ("raw_001", "raw_002")


def test_seed_graph_adapter_omits_nonsemantic_open_input_terms() -> None:
    assert extract_seed_graph_observations([seed_graph_row(term="________")]) == ()


@pytest.mark.parametrize(
    ("platform", "family"),
    [
        ("news", "news"),
        ("search", "search"),
        ("music", "music"),
        ("youtube", "youtube"),
        ("tiktok", "tiktok"),
        ("facebook", "facebook"),
        ("web", "web"),
    ],
)
def test_seed_graph_adapter_uses_current_platform_normalization(platform: str, family: str) -> None:
    # Opened 4 Sep 2026 on Albert's word: a seed on a SocialCrawl surface carries
    # that vendor and channel identity, so it can qualify like a youtube seed.
    # Surfaces the seed graph cannot attribute to one vendor stay unattributed.
    expected = {
        "youtube": ("google_youtube", "youtube"),
        "tiktok": ("socialcrawl", "short_video"),
    }.get(platform)
    observation = extract_seed_graph_observations([seed_graph_row(platform=platform)])[0]
    if expected is None:
        assert observation.source_families == ()
        assert observation.vendor_family is None
        assert observation.channel_family is None
    else:
        assert observation.source_families == (expected[1],)
        assert (observation.vendor_family, observation.channel_family) == expected
    assert observation.platforms == (family,)


@pytest.mark.parametrize(
    ("platform", "channel"),
    [
        ("tiktok", "short_video"),
        ("instagram", "short_video"),
        ("reddit", "reddit"),
        ("twitter", "twitter"),
        ("threads", "threads"),
    ],
)
def test_seed_graph_socialcrawl_surfaces_carry_their_channel_family(platform, channel) -> None:
    observation = extract_seed_graph_observations([seed_graph_row(platform=platform)])[0]
    assert observation.vendor_family == "socialcrawl"
    assert observation.channel_family == channel
    assert observation.source_families == (channel,)


def test_producer_shaped_rows_preserve_every_task_two_input() -> None:
    graph = extract_seed_graph_observations(
        [
            seed_graph_row(
                term="@fixture_creator",
                term_type="handle",
                trend_date="2026-08-25",
                event_date="2026-08-24",
                row_count=7,
                topic_groups=["culture_creator_economy"],
                near_topics=["culture_genz_identity"],
                co_occur_terms=["repair routine", "creator swap"],
            )
        ]
    )[0]
    assert graph.candidate_type == "creator"
    assert graph.observed_dates == ("2026-08-24", "2026-08-25")
    assert graph.co_occurring_terms == ("creator swap", "repair routine")
    assert graph.topic_tags == ("culture_creator_economy",)
    assert graph.near_topics == ("culture_genz_identity",)
    assert graph.creator_ids == ("fixture_creator",)
    assert graph.weight == 7.0

    candidate = extract_seed_candidate_observations(
        [
            seed_candidate_row(
                proposed_date="2026-08-25",
                score=0.73,
                evidence_topics=["culture_creator_economy"],
            )
        ]
    )[0]
    assert candidate.observed_dates == ("2026-08-25",)
    assert candidate.topic_tags == ("culture_creator_economy",)
    assert candidate.candidate_score == 0.73

    event = extract_event_observations(
        [
            event_row(
                event_kind="search_term",
                corroborating_sources=["trends"],
                trend_date="2026-08-25",
                as_of="2026-08-25T06:30:00Z",
                source_count=1,
            )
        ]
    )[0]
    assert event.observed_dates == ("2026-08-25",)
    assert event.observed_timestamps == ("2026-08-25T06:30:00Z",)
    assert event.weight == 1.0
    assert event.verified_search_spike is True


def test_seed_candidate_adapter_maps_current_output_contract() -> None:
    item = extract_seed_candidate_observations([seed_candidate_row()])[0]
    assert item == observation(
        "ke",
        "bei ya unga",
        "keyword",
        "seed_graph",
        "seed_candidates",
        "candidate_001",
        row_ids=("candidate_001", "raw_003", "raw_004"),
        source_families=(),
        platforms=("seed_candidates",),
    )


@pytest.mark.parametrize(
    "extractor,row",
    [
        (extract_event_observations, event_row(market="ZA")),
        (extract_seed_graph_observations, seed_graph_row(market="NG")),
        (extract_seed_candidate_observations, seed_candidate_row(market="KE")),
    ],
)
def test_adapters_reject_market_case_drift(extractor, row) -> None:
    with pytest.raises(ValueError, match="approved lower-case market"):
        extractor([row])


@pytest.mark.parametrize("market", ["ZA", "ng ", "us", "", None])
def test_canonicalizer_rejects_nonapproved_markets(market) -> None:
    with pytest.raises(ValueError, match="approved lower-case market"):
        canonicalize_observations(
            [observation(market, "valid phrase", "keyword", "rss", "rss", "row")]
        )


def test_canonicalization_folds_aliases_but_preserves_receipts() -> None:
    observations = canonicalize_observations(
        [
            observation("za", "Ramaphosa", "entity", "rss", "rss", "row_b"),
            observation(
                "za",
                "Cyril Ramaphosa",
                "entity",
                "gdelt",
                "gdelt",
                "row_a",
            ),
        ],
        aliases={"ramaphosa": "cyril ramaphosa"},
    )
    assert [item.term for item in observations] == ["cyril ramaphosa"]
    assert observations[0].row_ids == ("row_a", "row_b")
    assert observations[0].source_families == ("news",)
    assert observations[0].platforms == ("gdelt", "rss")


def test_null_and_empty_aliases_do_not_create_terms() -> None:
    observations = canonicalize_observations(
        [
            observation(
                "za",
                "Cyril Ramaphosa",
                "entity",
                "rss",
                "rss",
                "row_a",
                aliases=("", "  "),
            )
        ],
        aliases={"": "", "   ": "cyril ramaphosa"},
    )
    assert len(observations) == 1
    assert observations[0].term == "cyril ramaphosa"
    assert observations[0].aliases == ()


def test_nonsemantic_optional_terms_are_omitted_before_aggregation() -> None:
    result = canonicalize_observations(
        [
            observation(
                "za",
                "valid signal",
                "keyword",
                "news",
                "web",
                "row",
                aliases=("______", "Signal Alias"),
                co_occurring_terms=("______", "valid context"),
            )
        ]
    )[0]
    assert result.aliases == ("signal alias",)
    assert result.co_occurring_terms == ("valid context",)


def test_nonsemantic_primary_term_is_rejected_at_the_observation_boundary() -> None:
    with pytest.raises(ValueError, match="term"):
        observation("za", "______", "keyword", "news", "web", "row")


@pytest.mark.parametrize("candidate_type", ["entity", "headline"])
@pytest.mark.parametrize("term", ["news", "government", "trending", "south africa"])
def test_generic_entity_anchors_are_rejected(term: str, candidate_type: str) -> None:
    assert (
        canonicalize_observations([observation("za", term, candidate_type, "news", "rss", "row")])
        == ()
    )


def test_generic_words_remain_valid_for_non_entity_candidates() -> None:
    observations = canonicalize_observations(
        [observation("za", "music", "sound", "music", "music", "row")]
    )
    assert tuple(item.term for item in observations) == ("music",)


@pytest.mark.parametrize(
    ("term", "topic"),
    [
        ("sapa vietnam tour", "economy_sapa_hustle"),
        ("ankara turkey venue", "fashion_ankara_asoebi"),
    ],
)
def test_topic_scoped_geo_collisions_are_rejected(term: str, topic: str) -> None:
    assert (
        canonicalize_observations(
            [
                observation(
                    "ng",
                    term,
                    "keyword",
                    "reddit",
                    "reddit",
                    "row",
                    topic_tags=(topic,),
                )
            ]
        )
        == ()
    )


@pytest.mark.parametrize(
    ("term", "topic"),
    [
        ("sapa vietnam diaspora homecoming", "diaspora_events"),
        ("ankara turkey diaspora designers", "diaspora_events"),
        ("sapa", "economy_sapa_hustle"),
        ("ankara style", "fashion_ankara_asoebi"),
    ],
)
def test_local_and_out_of_map_geo_context_is_retained(term: str, topic: str) -> None:
    observations = canonicalize_observations(
        [
            observation(
                "ng",
                term,
                "keyword",
                "reddit",
                "reddit",
                "row",
                topic_tags=(topic,),
            )
        ]
    )
    assert tuple(item.term for item in observations) == (term,)


def test_foreign_subreddit_requires_explicit_upstream_rejection_marker() -> None:
    assumed_filtered = observation("ng", "fliptop battle", "keyword", "reddit", "reddit", "row_a")
    explicitly_rejected = observation(
        "ng",
        "fliptop battle",
        "keyword",
        "reddit",
        "reddit",
        "row_b",
        geo_rejected=True,
    )
    assert canonicalize_observations([assumed_filtered]) != ()
    assert canonicalize_observations([explicitly_rejected]) == ()


@pytest.mark.parametrize("term", ["sapa", "ankara style", "mpesa", "amapiano"])
def test_local_terms_that_share_collision_tokens_are_retained(term: str) -> None:
    observations = canonicalize_observations(
        [observation("ng", term, "keyword", "reddit", "reddit", "row")]
    )
    assert tuple(item.term for item in observations) == (term,)


@pytest.mark.parametrize(
    "extractor,row",
    [
        (extract_event_observations, event_row(ledger_id="")),
        (extract_seed_graph_observations, seed_graph_row(sample_row_ids=[])),
        (
            extract_seed_candidate_observations,
            seed_candidate_row(candidate_id="", sample_row_ids=[]),
        ),
    ],
)
def test_every_adapter_requires_a_stable_receipt(extractor, row) -> None:
    with pytest.raises(ValueError, match="stable row receipt"):
        extractor([row])


@pytest.mark.parametrize("bad", [1, True, {}, [], ["ok", 1], [None]])
def test_receipt_collections_reject_nonstring_members(bad) -> None:
    with pytest.raises(ValueError, match="receipt"):
        extract_seed_graph_observations([seed_graph_row(sample_row_ids=bad)])


@pytest.mark.parametrize("bad", [1, True, {}, "rss", ["rss", 1], [None]])
def test_family_collections_reject_nonstring_or_wrong_shapes(bad) -> None:
    with pytest.raises(ValueError, match="source family"):
        extract_event_observations([event_row(corroborating_sources=bad)])


def test_alias_collections_ignore_null_but_reject_other_nonstrings() -> None:
    accepted = extract_event_observations([event_row(entity_aliases=[None, "Ramaphosa"])])
    assert accepted[0].aliases == ("ramaphosa",)
    for aliases in ([1], [True], [{}], [[]]):
        with pytest.raises(ValueError, match="alias"):
            extract_event_observations([event_row(entity_aliases=aliases)])


def test_observation_and_candidate_inputs_deeply_freeze_mutable_inputs() -> None:
    row_ids = ["row_b", "row_a"]
    families = ["rss", "gdelt"]
    topics = ["topic_b", "topic_a"]
    item = observation(
        "za",
        "term",
        "entity",
        "news",
        "rss",
        "row_a",
        row_ids=row_ids,
        source_families=families,
        topic_tags=topics,
    )
    inputs_list = [item]
    inputs = CandidateInputs(event_ledger=inputs_list)
    row_ids.append("row_c")
    families.append("search")
    topics.append("topic_c")
    inputs_list.clear()
    assert item.row_ids == ("row_a", "row_b")
    assert item.source_families == ("news",)
    assert item.topic_tags == ("topic_a", "topic_b")
    assert inputs.event_ledger == (item,)


def test_total_sort_key_orders_aggregate_ties_independent_of_input_order() -> None:
    first = observation(
        "za",
        "same",
        "keyword",
        "news",
        "web",
        "same_row",
        source_families=("news",),
        topic_tags=("topic_b",),
    )
    second = observation(
        "za",
        "same",
        "keyword",
        "news",
        "web",
        "same_row",
        source_families=("news", "search"),
        topic_tags=("topic_a",),
    )
    assert (
        CandidateInputs(event_ledger=[first, second]).observations
        == CandidateInputs(event_ledger=[second, first]).observations
    )


def test_total_sort_key_handles_none_and_float_scores() -> None:
    a = observation("za", "same", "keyword", "news", "web", "row", candidate_score=None)
    b = observation("za", "same", "keyword", "news", "web", "row", candidate_score=0.4)
    assert (
        CandidateInputs(event_ledger=[b, a]).observations
        == CandidateInputs(event_ledger=[a, b]).observations
    )


def test_creator_canonicalizer_collapses_current_handle_variants() -> None:
    items = [
        observation("za", value, "creator", "ensemble", "tiktok", f"row_{index}")
        for index, value in enumerate(("@DJ_Max", "dj_max", "Dj Max", "DJ-Max", "dj.max"))
    ]
    result = canonicalize_observations(reversed(items))
    assert len(result) == 1
    assert result[0].term == "dj_max"


def test_creator_canonicalizer_is_idempotent_for_dotted_handles() -> None:
    first = canonicalize_observations(
        [observation("za", "Creator.Name", "creator", "ensemble", "tiktok", "row")]
    )[0].term
    second = canonicalize_observations(
        [observation("za", first, "creator", "ensemble", "tiktok", "row")]
    )[0].term
    assert first == second == "creator_name"


@pytest.mark.parametrize("value", ["@", "---", "bad!creator", " "])
def test_creator_canonicalizer_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="creator"):
        observation("za", value, "creator", "ensemble", "tiktok", "row")


def test_alias_aggregation_preserves_every_task_two_field_deterministically() -> None:
    items = [
        observation(
            "za",
            "Ramaphosa",
            "entity",
            "news",
            "rss",
            "row_b",
            observed_dates=("2026-08-25",),
            observed_timestamps=("2026-08-25T06:30:00Z",),
            co_occurring_terms=("cabinet",),
            topic_tags=("politics_leadership",),
            near_topics=("politics_elections",),
            creator_ids=("creator_b",),
            weight=2,
            candidate_score=0.4,
        ),
        observation(
            "za",
            "Cyril Ramaphosa",
            "entity",
            "search",
            "google_search",
            "row_a",
            observed_dates=("2026-08-24",),
            co_occurring_terms=("speech",),
            topic_tags=("politics_leadership",),
            creator_ids=("creator_a",),
            weight=3,
            candidate_score=0.8,
            verified_search_spike=True,
        ),
    ]
    result = canonicalize_observations(reversed(items), aliases={"ramaphosa": "cyril ramaphosa"})[0]
    assert result.observed_dates == ("2026-08-24", "2026-08-25")
    assert result.observed_timestamps == ("2026-08-25T06:30:00Z",)
    assert result.co_occurring_terms == ("cabinet", "speech")
    assert result.topic_tags == ("politics_leadership",)
    assert result.near_topics == ("politics_elections",)
    assert result.creator_ids == ("creator_a", "creator_b")
    assert result.weight == 5.0
    assert result.candidate_score == 0.8
    assert result.verified_search_spike is True


def test_candidate_inputs_and_canonical_output_ignore_input_order() -> None:
    event = extract_event_observations([event_row()])
    graph = extract_seed_graph_observations([seed_graph_row()])
    candidates = extract_seed_candidate_observations([seed_candidate_row()])
    left = CandidateInputs(event, graph, candidates)
    right = CandidateInputs(
        tuple(reversed(event)),
        tuple(reversed(graph)),
        tuple(reversed(candidates)),
    )
    assert left.observations == right.observations
    assert canonicalize_observations(left.observations) == canonicalize_observations(
        tuple(reversed(right.observations))
    )


def test_canonicalization_returns_input_aligned_identity_mapping_with_shared_rows() -> None:
    inputs = (
        observation("za", "ramaphosa", "entity", "news", "rss", "shared"),
        observation("za", "cabinet", "entity", "news", "rss", "shared"),
        observation(
            "za",
            "foreign leader",
            "entity",
            "news",
            "rss",
            "other",
            geo_rejected=True,
        ),
    )

    canonical, identities = canonicalize_observations_with_mapping(
        inputs,
        aliases={"ramaphosa": "cyril ramaphosa"},
    )

    assert tuple(item.term for item in canonical) == ("cabinet", "cyril ramaphosa")
    assert identities == (
        "za|entity|cyril ramaphosa",
        "za|entity|cabinet",
        None,
    )


def test_adapter_output_is_sorted_independent_of_row_order() -> None:
    rows = [
        seed_graph_row(term="z term", sample_row_ids=["row_z"]),
        seed_graph_row(term="a term", sample_row_ids=["row_a"]),
    ]
    assert extract_seed_graph_observations(rows) == extract_seed_graph_observations(
        list(reversed(rows))
    )


def test_canonical_fixture_evidence_preserves_stable_receipts_and_families() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    evidence = fixture["payload"]["evidence"]
    observations = canonicalize_observations(
        [
            observation(
                fixture["payload"]["signal"]["market"],
                "fixture repair routine",
                "keyword",
                item["source_family"],
                item["platform"],
                item["row_id"],
            )
            for item in reversed(evidence)
        ]
    )
    assert len(observations) == 1
    assert observations[0].row_ids == ("fixture_row_001", "fixture_row_002")
    assert observations[0].source_families == ("reddit", "youtube")


@pytest.mark.parametrize(
    "extractor,row",
    [
        (extract_event_observations, event_row(event_kind="unknown")),
        (extract_seed_graph_observations, seed_graph_row(term_type="unknown")),
        (
            extract_seed_candidate_observations,
            seed_candidate_row(candidate_type="unknown"),
        ),
    ],
)
def test_unknown_candidate_types_fail_closed(extractor, row) -> None:
    with pytest.raises(ValueError, match="candidate type"):
        extractor([row])
