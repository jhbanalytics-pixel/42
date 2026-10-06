import copy
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from scripts.staging import replay_open_intelligence as replay
from src.analysis.open_intelligence import pipeline
from src.analysis.open_intelligence.source_provenance import provenance_member_id
from src.contracts.open_intelligence import ResolvedScope

DAY = date(2026, 9, 6)
CONFIG = Path(__file__).resolve().parents[2] / "configs/open_intelligence_composition_rules_v3.yaml"


def scope():
    return ResolvedScope(
        "ogilvy_internal",
        ("ke", "ng", "za"),
        "ogilvy",
        (),
        "open_intelligence",
        "synthetic_v3",
        "2.0.0",
    )


def snapshot():
    tables = dict.fromkeys(
        ("event_ledger", "seed_graph", "seed_candidates", "enriched_content", "raw_content")
    )
    tables = {key: [] for key in tables}
    for term, source, platform in [
        ("alpha rhythm", "rss", "news"),
        ("beta rhythm", "youtube", "youtube"),
    ]:
        graph = dict.fromkeys(pipeline.SEED_GRAPH_COLUMNS)
        graph.update(
            market="za",
            term=term,
            term_type="token",
            platform=platform,
            trend_date=DAY,
            event_date=DAY,
            row_count=4,
            topic_groups=["music"],
            near_topics=[],
            co_occur_terms=["alpha rhythm", "beta rhythm"],
            sample_row_ids=[term],
        )
        tables["seed_graph"].append(graph)
        row = dict.fromkeys(pipeline.EVIDENCE_COLUMNS_BY_TABLE["enriched_content"])
        row.update(
            id=term,
            source=source,
            platform=platform,
            market="za",
            content_type="article" if source == "rss" else "video",
            collected_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
            published_at=datetime(2026, 9, 6, 10, tzinfo=UTC),
            url="https://example.invalid/" + term.replace(" ", "-"),
        )
        tables["enriched_content"].append(row)
    value = {
        "fixture_scope": "synthetic_test_only",
        "snapshot_id": "source_provenance_snapshot_v3",
        "captured_at": "2026-09-06T13:00:00+00:00",
        "rows_by_table": tables,
        "row_counts_by_table": {},
        "table_digests": {},
        "section_counts": {},
        "section_identity_sets": {},
    }
    return seal(value)


def seal(value, *, rebuild_sections=True):
    value = replay.normalize_replay_v3_json(value)
    value["row_counts_by_table"] = {k: len(v) for k, v in value["rows_by_table"].items()}
    value["table_digests"] = {k: replay._digest(v) for k, v in value["rows_by_table"].items()}
    if rebuild_sections:
        value["section_identity_sets"] = {
            table: [f"{index:08d}:{replay._digest(row)}" for index, row in enumerate(rows)]
            for table, rows in value["rows_by_table"].items()
        }
        value["section_counts"] = {
            table: len(rows) for table, rows in value["rows_by_table"].items()
        }
    value.pop("source_digest", None)
    value["source_digest"] = replay._digest(value)
    return value


class Provider:
    version = "synthetic_frozen_v3"

    def candidate_pairs(self, observations):
        return ()

    def similarities(self, observations, pairs):
        return dict.fromkeys(pairs, 0.95)


def run(value=None, client=None, **kwargs):
    value = snapshot() if value is None else value
    rules = replay.load_composition_rules_v3(CONFIG, require_certified=False)
    return pipeline.run_dynamic_signal_identity_v3(
        DAY,
        scope(),
        replay.ProvenanceSnapshotClient(value) if client is None else client,
        "trends_v2_staging",
        False,
        source_snapshot=value,
        rule_bundle=rules,
        semantic_provider=Provider(),
        **kwargs,
    )


def test_runner_binds_actual_sources_and_members_before_graph():
    value = snapshot()
    result = run(value)
    assert result.run.component_count == 1
    assert result.run.components[0].build_version == "hybrid_graph_v3"
    assert result.source_snapshot_digest == value["source_digest"]
    assert result.run.observations[0].source_families == ("news",)
    assert result.run.observations[1].source_families == ("youtube",)
    assert "incomplete_current_window" in result.run.missing_work
    assert result.run.persistable_count == 0
    for members in result.run.evidence_projection.memberships_by_component.values():
        for member in members:
            observation = next(
                o
                for o in result.run.observations
                if pipeline._observation_identity(o) == member.member_identity
            )
            legacy_id = pipeline._member_id(scope(), DAY, observation, member.row_id)
            assert member.member_id == provenance_member_id(
                legacy_id, result.source_provenance_by_member[member.member_identity]
            )


@pytest.mark.parametrize("mutation", ["digest", "row", "market", "window", "fields"])
def test_runner_rejects_invalid_snapshot(mutation):
    value = snapshot()
    row = value["rows_by_table"]["enriched_content"][0]
    if mutation == "digest":
        value["source_digest"] = "0" * 64
    elif mutation == "row":
        row["url"] = "https://example.invalid/changed"
    else:
        if mutation == "market":
            row["market"] = "us"
        elif mutation == "window":
            row["collected_at"] = "2026-08-01T12:00:00+00:00"
        else:
            row["unexpected"] = None
        value = seal(value)
    with pytest.raises(ValueError):
        run(value)


def test_changed_query_rows_cannot_borrow_snapshot_authority():
    value = snapshot()
    changed = copy.deepcopy(value)
    changed["rows_by_table"]["enriched_content"][0]["url"] = "https://example.invalid/changed"
    with pytest.raises(ValueError, match="snapshot"):
        run(value, replay.ProvenanceSnapshotClient(seal(changed)))


@pytest.mark.parametrize("mode", ["missing", "ambiguous"])
def test_missing_and_ambiguous_samples_are_unavailable(mode):
    value = snapshot()
    if mode == "missing":
        value["rows_by_table"]["enriched_content"].pop(0)
    else:
        value["rows_by_table"]["enriched_content"].append(
            copy.deepcopy(value["rows_by_table"]["enriched_content"][0])
        )
    result = run(seal(value))
    envelope = result.source_provenance_by_member["za|keyword|alpha rhythm"]
    assert envelope["resolution_state"] == "unavailable"
    assert envelope["unresolved_sample_refs"][0]["reason"] == mode
    assert result.run.component_count == 0


def test_persistence_is_refused_before_querying():
    class NoQueries:
        project = "ogilvy-trends-v2"

        def query(self, *args, **kwargs):
            pytest.fail("query before persistence refusal")

    with pytest.raises(ValueError, match="persistence"):
        pipeline.run_dynamic_signal_identity_v3(
            DAY, scope(), NoQueries(), "trends_v2_staging", True, source_snapshot={}
        )


def artifact():
    value = snapshot()
    return replay.adapt_provenance_result_to_replay_input(
        run(value), scope=scope(), source_snapshot=value, semantic_provider=Provider()
    )


def test_v3_roundtrip_preserves_snapshot_and_mapping_digests():
    original = artifact()
    encoded = replay.serialize_replay_input_v3(original)
    decoded = replay.validate_replay_input_v3(json.loads(encoded), semantic_provider=Provider())
    assert replay.serialize_replay_input_v3(decoded) == encoded
    assert replay._digest(decoded) == replay._digest(original)
    assert set(decoded) == set(replay.REPLAY_INPUT_FIELDS) | {"source_provenance_by_member"}


@pytest.mark.parametrize(
    "field,value",
    [("artifact_version", "open_intelligence_replay_v4"), ("contract_version", "3.0.0")],
)
def test_unsupported_replay_versions_fail(field, value):
    payload = json.loads(replay.serialize_replay_input_v3(artifact()))
    payload[field] = value
    with pytest.raises(ValueError, match="version"):
        replay.validate_replay_input_v3(payload, semantic_provider=Provider())


def test_changed_pair_record_binding_is_rejected():
    payload = json.loads(replay.serialize_replay_input_v3(artifact()))
    envelope = payload["source_provenance_by_member"]["za|keyword|alpha rhythm"]
    envelope["pairs"][0]["sample_refs"][0]["row_id"] = "beta rhythm"
    with pytest.raises(ValueError, match="provenance"):
        replay.validate_replay_input_v3(payload, semantic_provider=Provider())


def test_uncertified_rules_cannot_be_loaded_for_live_use():
    with pytest.raises(ValueError, match="certified"):
        replay.load_composition_rules_v3(CONFIG, require_certified=True)


def test_legacy_runner_bytes_match_before_task_snapshot():
    result = pipeline.run_dynamic_signal_identity(
        DAY, scope(), replay.ProvenanceSnapshotClient(snapshot()), "trends_v2_staging", False
    )
    assert (
        replay._digest(result) == "3df6a6b27a0520fd2a71fbdfd91ac968cc9ed39bfe5e007e5d982175e8774ab5"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_version", "invented"),
        ("matrix_digest", "a" * 64),
        ("pair_count", 0),
        ("provider_kind", "embedding"),
        ("estimated_cost", 0.0),
    ],
)
def test_replay_refuses_provider_claims_not_bound_to_reconstructed_output(field, value):
    payload = json.loads(replay.serialize_replay_input_v3(artifact()))
    payload["provider_outputs"][0][field] = value
    with pytest.raises(ValueError, match="provider"):
        replay.validate_replay_input_v3(payload, semantic_provider=Provider())


def test_default_lexical_replay_needs_no_injected_provider():
    value = snapshot()
    rules = replay.load_composition_rules_v3(CONFIG, require_certified=False)
    result = pipeline.run_dynamic_signal_identity_v3(
        DAY,
        scope(),
        replay.ProvenanceSnapshotClient(value),
        "trends_v2_staging",
        False,
        source_snapshot=value,
        rule_bundle=rules,
    )
    bundle = replay.adapt_provenance_result_to_replay_input(
        result, scope=scope(), source_snapshot=value
    )
    encoded = replay.serialize_replay_input_v3(bundle)
    assert (
        replay.serialize_replay_input_v3(replay.validate_replay_input_v3(json.loads(encoded)))
        == encoded
    )
    assert bundle["provider_outputs"][0]["provider_version"] == "lexical_similarity_v2"


def test_replay_does_not_remeasure_captured_runtime():
    payload = json.loads(replay.serialize_replay_input_v3(artifact()))
    payload["provider_outputs"][0]["runtime_ms"] = 1234.5
    assert (
        replay.validate_replay_input_v3(payload, semantic_provider=Provider())["provider_outputs"][
            0
        ]["runtime_ms"]
        == 1234.5
    )


def test_boolean_cannot_replace_numeric_source_fact_or_membership_flag():
    value = snapshot()
    value["rows_by_table"]["enriched_content"][0]["views"] = True
    with pytest.raises(ValueError, match="type"):
        run(seal(value))
    payload = json.loads(replay.serialize_replay_input_v3(artifact()))
    next(iter(payload["memberships_by_candidate"].values()))[0]["qualifies_evidence"] = 0
    with pytest.raises(ValueError, match="conflict"):
        replay.validate_replay_input_v3(payload, semantic_provider=Provider())


def test_actual_capture_labels_are_admitted_without_changing_authority():
    value = snapshot()
    value["fixture_scope"] = "captured_staging_source"
    value["snapshot_id"] = "captured_source_20260906"
    result = run(seal(value))
    assert result.run.dry_run is True
    assert result.run.evidence_projection.source_window.complete_partitions is False


def test_unsupported_enriched_record_does_not_fall_back_to_raw_authority():
    value = snapshot()
    enriched = value["rows_by_table"]["enriched_content"][0]
    raw = {field: enriched[field] for field in pipeline.EVIDENCE_COLUMNS_BY_TABLE["raw_content"]}
    value["rows_by_table"]["raw_content"].append(raw)
    enriched.update(source="unknown_vendor", platform="unknown_channel")
    result = run(seal(value))
    envelope = result.source_provenance_by_member["za|keyword|alpha rhythm"]
    assert envelope["resolution_state"] == "unavailable"
    assert envelope["unresolved_sample_refs"][0]["reason"] == "unsupported_identity"


def test_dates_normalize_before_hashing_and_row_preimage_stays_exact():
    value = snapshot()
    value["rows_by_table"]["enriched_content"][0]["collected_at"] = "2026-09-06T14:00:00+02:00"
    normalized = seal(value)
    assert normalized == snapshot()
    result = run(normalized)
    ref = result.source_provenance_by_member["za|keyword|alpha rhythm"]["pairs"][0]["sample_refs"][
        0
    ]
    assert ref["row_digest"] == replay._digest(normalized["rows_by_table"]["enriched_content"][0])


@pytest.mark.parametrize(
    "pair",
    [("apple_music", "music", "chart_track"), ("google_trends_rss", "google_search", "trend")],
)
def test_existing_search_and_music_sources_are_valid_in_v3(pair):
    value = snapshot()
    value["rows_by_table"]["enriched_content"][0].update(
        source=pair[0], platform=pair[1], content_type=pair[2]
    )
    result = run(seal(value))
    assert (
        result.source_provenance_by_member["za|keyword|alpha rhythm"]["resolution_state"]
        == "resolved"
    )
    assert result.run.observations[0].verified_search_spike is False


@pytest.mark.parametrize("mutation", ["rule_version", "threshold", "rule_bytes"])
def test_runner_refuses_unbound_composition_rules(mutation):
    from dataclasses import replace

    rules = replay.load_composition_rules_v3(CONFIG, require_certified=False)
    if mutation == "rule_version":
        rules = replace(rules, rule_version="composition_rules_v2")
    elif mutation == "threshold":
        rules = replace(rules, graph_rules=replace(rules.graph_rules, semantic_anchor_floor=0.8))
    else:
        rules = replace(rules, rule_config_sha256="a" * 64)
    value = snapshot()
    with pytest.raises(ValueError, match="version"):
        pipeline.run_dynamic_signal_identity_v3(
            DAY,
            scope(),
            replay.ProvenanceSnapshotClient(value),
            "trends_v2_staging",
            False,
            source_snapshot=value,
            rule_bundle=rules,
        )


@pytest.mark.parametrize(
    "mutation", ["component_version", "member_id", "source_row", "missing_envelope"]
)
def test_replay_rebuilds_all_derived_bindings(mutation):
    payload = json.loads(replay.serialize_replay_input_v3(artifact()))
    if mutation == "component_version":
        next(iter(payload["components_by_candidate"].values()))["build_version"] = "hybrid_graph_v2"
    elif mutation == "member_id":
        next(iter(payload["memberships_by_candidate"].values()))[0]["member_id"] = "mem_" + "a" * 64
    elif mutation == "source_row":
        payload["source_snapshot"]["rows_by_table"]["enriched_content"][0]["url"] = (
            "https://example.invalid/changed"
        )
        payload["source_snapshot"] = seal(payload["source_snapshot"])
    else:
        payload["source_provenance_by_member"].pop("za|keyword|alpha rhythm")
    with pytest.raises(ValueError, match="provenance"):
        replay.validate_replay_input_v3(payload, semantic_provider=Provider())


def test_unused_metric_provider_does_not_claim_metric_projection():
    class NoQueries:
        project = "ogilvy-trends-v2"

        def query(self, *args, **kwargs):
            pytest.fail("unsupported metric provider queried source")

    with pytest.raises(ValueError, match="metric provider"):
        run(client=NoQueries(), metric_provider=object())


def source_sections(value):
    value = copy.deepcopy(value)
    value["section_identity_sets"] = {
        table: [f"{index:08d}:{replay._digest(row)}" for index, row in enumerate(rows)]
        for table, rows in value["rows_by_table"].items()
    }
    value["section_counts"] = {table: len(rows) for table, rows in value["rows_by_table"].items()}
    return seal(value)


@pytest.mark.parametrize("mutation", ["empty", "unknown", "false_identity", "false_count"])
def test_v3_source_section_claims_must_match_exact_source_rows(mutation):
    value = source_sections(snapshot())
    if mutation == "empty":
        value["section_counts"] = {}
        value["section_identity_sets"] = {}
    elif mutation == "unknown":
        value["section_counts"]["imaginary_section"] = 0
        value["section_identity_sets"]["imaginary_section"] = []
    elif mutation == "false_identity":
        value["section_identity_sets"]["seed_graph"][0] = "00000000:" + "a" * 64
    else:
        value["section_counts"]["seed_graph"] = 1
        value["section_identity_sets"]["seed_graph"] = value["section_identity_sets"]["seed_graph"][
            :1
        ]
    with pytest.raises(ValueError, match="section"):
        run(seal(value, rebuild_sections=False))


def test_v3_source_sections_preserve_duplicate_occurrences_and_validate_output():
    value = snapshot()
    value["rows_by_table"]["enriched_content"].append(
        copy.deepcopy(value["rows_by_table"]["enriched_content"][0])
    )
    value = source_sections(value)
    result = run(value)
    envelope = result.source_provenance_by_member["za|keyword|alpha rhythm"]
    assert envelope["unresolved_sample_refs"][0]["reason"] == "ambiguous"
    identities = value["section_identity_sets"]["enriched_content"]
    assert identities[0][9:] == identities[2][9:]
    assert identities[0] != identities[2]
    replay.adapt_provenance_result_to_replay_input(
        result, scope=scope(), source_snapshot=value, semantic_provider=Provider()
    )


def candidate_source(table):
    if table == "event_ledger":
        return {
            "ledger_id": "synthetic_event",
            "trend_date": DAY.isoformat(),
            "market": "za",
            "entity_key": "gamma rhythm",
            "entity_aliases": [],
            "event_kind": "search_term",
            "state_label": "observed",
            "as_of": "2026-09-06T12:00:00+00:00",
            "corroborating_sources": ["search"],
            "source_count": 1,
            "confidence": 0.5,
        }
    return {
        "candidate_id": "synthetic_candidate",
        "proposed_date": DAY.isoformat(),
        "market": "za",
        "candidate_type": "keyword",
        "candidate_value": "gamma rhythm",
        "source": "seed_graph",
        "lane": "token",
        "score": 0.5,
        "evidence_topics": [],
        "sample_row_ids": ["alpha rhythm"],
        "status": "approved",
    }


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("event_ledger", "state_label", {"invalid": "object"}),
        ("event_ledger", "confidence", {"invalid": "object"}),
        ("event_ledger", "confidence", True),
        ("event_ledger", "source_count", 1.0),
        ("event_ledger", "corroborating_sources", [None]),
        ("seed_graph", "row_count", 1.0),
        ("seed_graph", "term", None),
        ("seed_graph", "topic_groups", "music"),
        ("seed_candidates", "lane", 1),
        ("seed_candidates", "lane", None),
        ("seed_candidates", "score", True),
        ("seed_candidates", "evidence_topics", [2]),
        ("enriched_content", "classification_layer", 1),
        ("raw_content", "views", True),
    ],
)
def test_every_v3_source_field_uses_its_stored_type(table, field, value):
    snapshot_value = snapshot()
    if table in ("event_ledger", "seed_candidates"):
        snapshot_value["rows_by_table"][table] = [candidate_source(table)]
    if table == "raw_content":
        source = snapshot_value["rows_by_table"]["enriched_content"][0]
        snapshot_value["rows_by_table"][table] = [
            {name: source[name] for name in pipeline.EVIDENCE_COLUMNS_BY_TABLE[table]}
        ]
    snapshot_value["rows_by_table"][table][0][field] = value
    snapshot_value = source_sections(snapshot_value)
    with pytest.raises(ValueError, match=r"type|null"):
        replay.validate_source_snapshot_v3(snapshot_value, DAY, markets=scope().market_scope)


def test_v3_optional_source_nulls_are_preserved():
    value = snapshot()
    event = candidate_source("event_ledger")
    event.update(state_label=None, confidence=None, entity_aliases=None, as_of=None)
    value["rows_by_table"]["event_ledger"] = [event]
    value = source_sections(value)
    admitted = replay.validate_source_snapshot_v3(value, DAY, markets=scope().market_scope)
    row = admitted["rows_by_table"]["event_ledger"][0]
    assert row["state_label"] is None
    assert row["confidence"] is None
    assert row["entity_aliases"] is None
    assert row["as_of"] is None
    assert replay._digest(row) == replay._digest(event)


def known_event_artifact():
    value = source_sections(snapshot())
    events = [
        {
            "event_id": "synthetic_event",
            "market": "za",
            "member_identities": ["za|keyword|alpha rhythm"],
        }
    ]
    known = {
        "fixture_scope": value["fixture_scope"],
        "set_id": "synthetic_known_events",
        "events": events,
        "digest": replay._digest(events),
    }
    return replay.adapt_provenance_result_to_replay_input(
        run(value),
        scope=scope(),
        source_snapshot=value,
        semantic_provider=Provider(),
        known_event_set=known,
    )


def test_v3_source_bound_known_events_can_roundtrip_with_pending_review():
    bundle = known_event_artifact()
    encoded = replay.serialize_replay_input_v3(bundle)
    rebuilt = replay.validate_replay_input_v3(json.loads(encoded), semantic_provider=Provider())
    assert replay.serialize_replay_input_v3(rebuilt) == encoded
    assert rebuilt["human_review_state"] == "pending"
    assert rebuilt["known_event_set"]["events"][0]["event_id"] == "synthetic_event"


@pytest.mark.parametrize(
    "table", ["event_ledger", "seed_graph", "seed_candidates", "enriched_content", "raw_content"]
)
def test_v3_source_field_schema_matches_projected_ddl(table):
    fields_by_table = {
        "event_ledger": pipeline.EVENT_COLUMNS,
        "seed_graph": pipeline.SEED_GRAPH_COLUMNS,
        "seed_candidates": pipeline.SEED_CANDIDATE_COLUMNS,
        **pipeline.EVIDENCE_COLUMNS_BY_TABLE,
    }
    ddl = (CONFIG.parent.parent / "infra" / "bigquery_schemas" / f"{table}.sql").read_text(
        encoding="utf-8"
    )
    stored = {
        field: (kind, "NOT NULL" not in suffix)
        for field, kind, suffix in re.findall(
            r"^\s+(\w+)\s+(ARRAY<STRING>|STRING|INT64|FLOAT64|DATE|TIMESTAMP)([^\n,]*)",
            ddl,
            re.MULTILINE,
        )
    }
    expected = {field: stored[field] for field in fields_by_table[table]}
    assert replay._v3_source_field_schema(table, fields_by_table[table]) == expected


def test_v3_float_source_refuses_integer_outside_float_range():
    value = snapshot()
    value["rows_by_table"]["enriched_content"][0]["views"] = 10**400
    value = seal(value)
    with pytest.raises(ValueError, match="type"):
        replay.validate_source_snapshot_v3(value, DAY, markets=scope().market_scope)


@pytest.mark.parametrize(
    "mutation",
    [
        "digest",
        "scope",
        "market",
        "unknown_member",
        "duplicate_member",
        "duplicate_event",
        "unavailable",
    ],
)
def test_v3_known_event_authority_rejects_unbound_claims(mutation):
    payload = json.loads(replay.serialize_replay_input_v3(known_event_artifact()))
    known = payload["known_event_set"]
    event = known["events"][0]
    if mutation == "digest":
        known["digest"] = "a" * 64
    else:
        if mutation == "scope":
            known["fixture_scope"] = "unrelated_capture"
        elif mutation == "market":
            event["market"] = "ke"
        elif mutation == "unknown_member":
            event["member_identities"] = ["za|keyword|invented"]
        elif mutation == "duplicate_member":
            event["member_identities"] *= 2
        elif mutation == "duplicate_event":
            known["events"] *= 2
        else:
            known["set_id"] = "unavailable"
        known["digest"] = replay._digest(known["events"])
    with pytest.raises(ValueError, match="known event"):
        replay.validate_replay_input_v3(payload, semantic_provider=Provider())


@pytest.mark.parametrize("mutation", ["review", "prior", "complete_history", "overlap_history"])
def test_v3_preserves_existing_review_and_historical_source_requirements(mutation):
    payload = json.loads(replay.serialize_replay_input_v3(known_event_artifact()))
    if mutation == "review":
        payload["human_review_state"] = "approved"
        reason = "review must remain pending"
    elif mutation == "prior":
        payload["prior_snapshots"] = [
            {
                "signal_id": "synthetic_prior",
                "signal_date": "2026-08-30",
                "market": "za",
                "run_id": "synthetic_prior_run",
                "member_identities": ["za|keyword|alpha rhythm"],
            }
        ]
        reason = "prior snapshot source relation"
    else:
        if mutation == "complete_history":
            payload["history_window"]["complete_partitions"] = True
        else:
            payload["history_window"]["end_date"] = "2026-09-06"
        reason = "history window"
    with pytest.raises(ValueError, match=reason):
        replay.validate_replay_input_v3(payload, semantic_provider=Provider())
