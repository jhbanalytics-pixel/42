from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest
from scripts.staging.replay_open_intelligence import _digest
from src.analysis.open_intelligence.candidates import CandidateInputs, Observation
from src.analysis.open_intelligence.geographic_scope import (
    GeographicScope,
    local_observation_refs,
    resolve_geographic_scope,
)
from src.analysis.open_intelligence.graph import (
    AnchoredGraphRules,
    build_provenance_components,
    build_provenance_edges,
)
from src.analysis.open_intelligence.pipeline import (
    EVIDENCE_COLUMNS_BY_TABLE,
    EvidenceProjection,
    SourceWindow,
    _projected_evidence_row,
)
from src.analysis.open_intelligence.source_provenance import (
    bind_source_provenance,
    validate_source_provenance,
)

RULES = AnchoredGraphRules(0.7, 3, 100, 10)
LEFT, RIGHT = "za|keyword|commuting", "za|keyword|saving"
PAIR = (LEFT, RIGHT)


def observations():
    return (
        Observation(
            "za",
            "commuting",
            "keyword",
            ("left",),
            source_families=("news", "youtube"),
            platforms=("news",),
            observed_dates=("2026-09-06",),
            co_occurring_terms=("saving",),
        ),
        Observation(
            "za",
            "saving",
            "keyword",
            ("right",),
            source_families=("news", "youtube"),
            platforms=("youtube",),
            observed_dates=("2026-09-06",),
            co_occurring_terms=("commuting",),
        ),
    )


def envelope(label, pairs):
    return {
        "contract_version": "candidate_source_provenance_v1",
        "coverage_basis": "sampled_record_support",
        "source_snapshot_digest": "a" * 64,
        "resolution_state": "resolved" if pairs else "unavailable",
        "pairs": tuple(
            {
                "vendor_family": vendor,
                "channel_family": channel,
                "source_table": "enriched_content",
                "sample_refs": ({"row_id": f"{label}{index}", "row_digest": "b" * 64},),
            }
            for index, (vendor, channel) in enumerate(sorted(pairs))
        ),
        "unresolved_sample_refs": (),
    }


@pytest.mark.parametrize(
    "left_pairs,right_pairs,independent",
    [
        ((("rss", "news"),), (("google_youtube", "youtube"),), True),
        ((("rss", "news"),), (("gdelt", "news"),), False),
        ((("socialcrawl", "short_video"),), (("socialcrawl", "youtube"),), False),
        (
            (("socialcrawl", "short_video"), ("socialcrawl", "youtube")),
            (("socialcrawl", "news"),),
            False,
        ),
        ((), (), False),
    ],
)
def test_component_support_uses_actual_vendor_channel_pairs(left_pairs, right_pairs, independent):
    provenance = {LEFT: envelope("left", left_pairs), RIGHT: envelope("right", right_pairs)}
    edges = build_provenance_edges(
        observations(), (PAIR,), {PAIR: 0.9}, RULES, source_provenance_by_member=provenance
    )
    assert len(edges) == 1
    assert edges[0].votes.independent_source_family is independent
    components = build_provenance_components(
        observations(), (PAIR,), {PAIR: 0.9}, RULES, source_provenance_by_member=provenance
    )
    assert bool(components) is independent
    if components:
        assert components[0].build_version == "hybrid_graph_v3"
        assert components[0].source_families == ("news", "youtube")


@pytest.mark.parametrize("mutation", ["missing_member", "different_snapshot", "conflicting_record"])
def test_inconsistent_provenance_cannot_be_combined(mutation):
    provenance = {
        LEFT: envelope("left", (("rss", "news"),)),
        RIGHT: envelope("right", (("google_youtube", "youtube"),)),
    }
    if mutation == "missing_member":
        provenance.pop(RIGHT)
    elif mutation == "different_snapshot":
        provenance[RIGHT]["source_snapshot_digest"] = "c" * 64
    else:
        provenance[RIGHT]["pairs"][0]["sample_refs"][0]["row_id"] = "left0"
    with pytest.raises(ValueError, match="source_provenance"):
        build_provenance_components(
            observations(), (PAIR,), {PAIR: 0.9}, RULES, source_provenance_by_member=provenance
        )


def test_composition_rejects_missing_provenance_before_provider_work():
    from src.analysis.open_intelligence.composition import (
        build_components_with_provenance_semantics,
    )

    class Provider:
        version = "synthetic_similarity_v1"
        called = False

        def candidate_pairs(self, rows):
            self.called = True
            return (PAIR,)

        def similarities(self, rows, pairs):
            return {PAIR: 0.9}

    provider = Provider()
    with pytest.raises(ValueError, match="source_provenance"):
        build_components_with_provenance_semantics(
            observations(), provider, RULES, source_provenance_by_member={}
        )
    assert provider.called is False
    provenance = {
        LEFT: envelope("left", (("rss", "news"),)),
        RIGHT: envelope("right", (("google_youtube", "youtube"),)),
    }
    components = build_components_with_provenance_semantics(
        observations(), provider, RULES, source_provenance_by_member=provenance
    )
    assert len(components) == 1
    assert components[0].build_version == "hybrid_graph_v3"


# Retained source fixtures. Each case starts from stored evidence rows, binds them
# through the real provenance binder, resolves every row through the real
# geographic kernel, and builds components through the real provenance graph.
# Source identity comes from the rows' own vendor and channel; geography from the
# records each row cites. Nothing reads a host, a collection order or a day as
# identity, and no count of hosts or people stands in for independence.
NG_LEFT, NG_RIGHT = "ng|keyword|cbn rate hold", "ng|keyword|sapa season"
NG_PAIR = (NG_LEFT, NG_RIGHT)
WIRE = "Central bank holds the policy rate at 27.5%, the third hold this year"
WIRE_RECEIPT = {
    "retained_geo_market": "ng",
    "geo_method_id": "geo_method_wire_origin_v1",
    "geo_receipt_id": "geo_receipt_nan_wire_7781",
}
EARLY = datetime(2026, 9, 6, 1, tzinfo=UTC)
LATE = datetime(2026, 9, 6, 23, tzinfo=UTC)


def stored_row(row_id, source, platform, content_type, *, title, published, collected=EARLY):
    row = dict.fromkeys(EVIDENCE_COLUMNS_BY_TABLE["enriched_content"])
    row.update(
        id=row_id,
        market="ng",
        source=source,
        platform=platform,
        content_type=content_type,
        title=title,
        collected_at=collected,
        published_at=published,
        url=f"https://example.invalid/{row_id}",
    )
    return row


def ng_observations(left_rows, right_rows):
    return (
        Observation(
            "ng",
            "cbn rate hold",
            "keyword",
            tuple(row["id"] for row in left_rows),
            platforms=tuple(row["platform"] for row in left_rows),
            observed_dates=("2026-09-06",),
            co_occurring_terms=("sapa season",),
        ),
        Observation(
            "ng",
            "sapa season",
            "keyword",
            tuple(row["id"] for row in right_rows),
            platforms=tuple(row["platform"] for row in right_rows),
            observed_dates=("2026-09-06",),
            co_occurring_terms=("cbn rate hold",),
        ),
    )


def retained(left_rows, right_rows):
    """Bind, then build: the envelopes, the edge and the components for one fixture."""
    rows = (*left_rows, *right_rows)
    snapshot = {
        "snapshot_id": "retained_ng_sources_v1",
        "captured_at": "2026-09-07T00:00:00Z",
        "rows_by_table": {"enriched_content": rows, "raw_content": ()},
    }
    snapshot["source_digest"] = _digest(snapshot)
    members = {NG_LEFT: left_rows, NG_RIGHT: right_rows}
    projection = EvidenceProjection(
        CandidateInputs(),
        MappingProxyType({}),
        MappingProxyType(
            {
                member: tuple(_projected_evidence_row(row, "enriched_content") for row in items)
                for member, items in members.items()
            }
        ),
        MappingProxyType(dict.fromkeys(members, ())),
        (),
        (),
        SourceWindow(
            date(2026, 8, 31),
            date(2026, 9, 6),
            ("ng",),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            False,
        ),
    )
    kwargs = {
        "observations": ng_observations(left_rows, right_rows),
        "member_samples": {
            member: tuple(row["id"] for row in items) for member, items in members.items()
        },
        "projection": projection,
        "source_snapshot": snapshot,
    }
    envelopes = bind_source_provenance(**kwargs)
    validate_source_provenance(envelopes, **kwargs)
    graph_inputs = (kwargs["observations"], (NG_PAIR,), {NG_PAIR: 0.9}, RULES)
    (edge,) = build_provenance_edges(*graph_inputs, source_provenance_by_member=envelopes)
    components = build_provenance_components(*graph_inputs, source_provenance_by_member=envelopes)
    return envelopes, edge, components


def pairs_of(envelope):
    return {(pair["vendor_family"], pair["channel_family"]) for pair in envelope["pairs"]}


def refs_of(envelope):
    return {ref["row_id"] for pair in envelope["pairs"] for ref in pair["sample_refs"]}


def geography(row, **receipt):
    return resolve_geographic_scope(
        market="ng", row_id=row["id"], title=row["title"], url=row["url"], **receipt
    )


def test_copied_wire_story_on_two_hosts_is_one_source_and_one_observation():
    # The same wire copy on two Nigerian hosts, one of which never dated it.
    punch = stored_row(
        "row_punch_11", "Punch", "web", "article", title=WIRE, published=EARLY, collected=EARLY
    )
    vanguard = stored_row(
        "row_vanguard_12", "Vanguard", "web", "article", title=WIRE, published=None, collected=LATE
    )
    street = stored_row(
        "row_nairaland_13", "Nairaland", "web", "article", title="sapa season", published=EARLY
    )
    envelopes, edge, components = retained((punch, vanguard), (street,))
    # Two hosts, one source pair: the copy adds no independent family.
    assert pairs_of(envelopes[NG_LEFT]) == {("rss", "news")}
    assert refs_of(envelopes[NG_LEFT]) == {"row_punch_11", "row_vanguard_12"}
    assert edge.votes.independent_source_family is False
    assert components == ()
    # Both copies cite the wire's own receipt, so geography counts one observation.
    scopes = [geography(row, **WIRE_RECEIPT) for row in (punch, vanguard)]
    assert scopes[0] == scopes[1]
    assert local_observation_refs(scopes) == ("geo_receipt_nan_wire_7781",)


def test_policy_different_hosts_as_automatic_independence_fails_on_retained_rows():
    hosts = ("Punch", "Vanguard", "The Nation", "Premium Times")
    left = tuple(
        stored_row(f"row_host_{index}", host, "web", "article", title=WIRE, published=EARLY)
        for index, host in enumerate(hosts[:2])
    )
    right = tuple(
        stored_row(f"row_host_{index + 2}", host, "web", "article", title=WIRE, published=EARLY)
        for index, host in enumerate(hosts[2:])
    )
    envelopes, edge, components = retained(left, right)
    assert pairs_of(envelopes[NG_LEFT]) == pairs_of(envelopes[NG_RIGHT]) == {("rss", "news")}
    assert edge.votes.independent_source_family is False
    assert components == ()
    # Four hosts with no geographic record resolve nothing, not four local observations.
    unresolved = [geography(row) for row in (*left, *right)]
    assert {record.scope for record in unresolved} == {"unknown"}
    assert local_observation_refs(unresolved) == ()


def test_policy_earliest_collection_as_proof_of_originator_fails_on_retained_rows():
    def build(first, second):
        left = (
            stored_row(
                "row_copy_a", "Punch", "web", "article", title=WIRE, published=None, collected=first
            ),
            stored_row(
                "row_copy_b",
                "Vanguard",
                "web",
                "article",
                title=WIRE,
                published=None,
                collected=second,
            ),
        )
        right = (
            stored_row(
                "row_video_c", "youtube", "youtube", "video", title="sapa season", published=EARLY
            ),
        )
        return retained(left, right)

    forward = build(EARLY, LATE)
    backward = build(LATE, EARLY)
    # Swapping which copy was collected first changes no source pair, no support
    # and no component: collection order names no originator.
    for member in (NG_LEFT, NG_RIGHT):
        assert pairs_of(forward[0][member]) == pairs_of(backward[0][member])
        assert refs_of(forward[0][member]) == refs_of(backward[0][member])
        assert not {"originator", "origin_row", "first_seen"} & set(forward[0][member])
    assert forward[1].votes == backward[1].votes
    assert [c.row_receipts for c in forward[2]] == [c.row_receipts for c in backward[2]]
    assert len(forward[2]) == 1


def test_absent_dates_keep_their_source_pair_and_stay_undated():
    undated = stored_row("row_undated_1", "Punch", "web", "article", title=WIRE, published=None)
    dated = stored_row(
        "row_dated_2", "youtube", "youtube", "video", title="sapa season", published=EARLY
    )
    envelopes, edge, components = retained((undated,), (dated,))
    projected = _projected_evidence_row(undated, "enriched_content")
    # The missing date is not filled from collection: it stays missing.
    assert projected.published_at is None
    assert projected.collected_at == EARLY
    assert pairs_of(envelopes[NG_LEFT]) == {("rss", "news")}
    assert edge.votes.independent_source_family is True
    assert len(components) == 1
    # Geography never reads a date, so the undated row is unknown, not foreign.
    assert geography(undated) == GeographicScope("none", None, "unknown", None)


def test_separate_people_repeating_similar_language_on_one_channel():
    first = stored_row(
        "row_person_a",
        "socialcrawl",
        "tiktok",
        "video",
        title="sapa don hold me this month o, na wire I dey eat",
        published=EARLY,
    )
    second = stored_row(
        "row_person_b",
        "socialcrawl",
        "instagram",
        "video",
        title="sapa hold me this month, na wire we dey chop",
        published=EARLY,
    )
    envelopes, edge, components = retained((first,), (second,))
    # Two people are two records, each bound to its own row, on one source pair.
    assert refs_of(envelopes[NG_LEFT]) == {"row_person_a"}
    assert refs_of(envelopes[NG_RIGHT]) == {"row_person_b"}
    assert (
        pairs_of(envelopes[NG_LEFT])
        == pairs_of(envelopes[NG_RIGHT])
        == {("socialcrawl", "short_video")}
    )
    # Similar words from separate people do not make an independent source family.
    assert edge.votes.independent_source_family is False
    assert components == ()
    # Each cites their own receipt, so geography keeps them as two observations.
    scopes = [
        geography(
            first,
            retained_geo_market="ng",
            geo_method_id="geo_method_audience_v1",
            geo_receipt_id="geo_receipt_ng_a1",
        ),
        geography(
            second,
            retained_geo_market="ng",
            geo_method_id="geo_method_audience_v1",
            geo_receipt_id="geo_receipt_ng_b2",
        ),
    ]
    assert local_observation_refs(scopes) == ("geo_receipt_ng_a1", "geo_receipt_ng_b2")


def test_international_news_about_a_local_subject_is_context_beside_local_behaviour():
    abroad = stored_row(
        "row_wire_gb_1",
        "Reuters",
        "web",
        "article",
        title="Nigeria's central bank holds rates as inflation cools",
        published=EARLY,
    )
    local = stored_row(
        "row_video_ng_2",
        "youtube",
        "youtube",
        "video",
        title="sapa season don land for Lagos",
        published=EARLY,
    )
    envelopes, _edge, components = retained((abroad,), (local,))
    # The foreign story keeps its source pair as evidence of context.
    assert pairs_of(envelopes[NG_LEFT]) == {("rss", "news")}
    assert len(components) == 1
    context = geography(
        abroad,
        retained_geo_market="gb",
        geo_method_id="geo_method_publisher_v1",
        geo_receipt_id="geo_receipt_gb_004410",
    )
    behaviour = geography(
        local,
        retained_geo_market="ng",
        geo_method_id="geo_method_audience_v1",
        geo_receipt_id="geo_receipt_ng_c3",
    )
    assert context == GeographicScope("content_marker", "row_wire_gb_1", "contextual", 0.4)
    assert behaviour.scope == "local"
    # Only the local record counts as local behaviour; the global story never does.
    assert local_observation_refs([context, behaviour]) == ("geo_receipt_ng_c3",)
    # Every resolved record carries method, evidence reference, scope and confidence.
    for record in (context, behaviour):
        assert record.method != "none"
        assert record.evidence_ref
        assert record.confidence is not None


def test_nigerian_collision_and_creator_registration_on_retained_rows():
    portugal = stored_row(
        "row_lagos_pt_1",
        "socialcrawl",
        "tiktok",
        "video",
        title="Lagos beach weekend in the Algarve, best cliffs on the coast",
        published=EARLY,
    )
    creator = stored_row(
        "row_creator_2", "youtube", "youtube", "video", title="sapa season vlog", published=EARLY
    )
    envelopes, _edge, _components = retained((portugal,), (creator,))
    assert pairs_of(envelopes[NG_LEFT]) == {("socialcrawl", "short_video")}
    # A colliding city name resolves nothing on its own; a Portuguese receipt is foreign.
    assert geography(portugal).scope == "unknown"
    assert (
        geography(
            portugal,
            retained_geo_market="pt",
            geo_method_id="geo_method_audience_v1",
            geo_receipt_id="geo_receipt_pt_000009",
        ).scope
        == "foreign"
    )
    # A creator registered in Nigeria is not an audience in Nigeria.
    registered = resolve_geographic_scope(
        market="ng",
        row_id=creator["id"],
        title=creator["title"],
        creator_registration_market="ng",
    )
    assert registered == GeographicScope("none", None, "unknown", None)
