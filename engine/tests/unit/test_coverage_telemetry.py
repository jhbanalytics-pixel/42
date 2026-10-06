"""Boundary tests for identity based observation flow telemetry."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence.coverage_telemetry import (
    classified_observation_keys,
    count_identities,
    disposition_record,
    summarize_observation_flow,
)

OBSERVED_AT = datetime(2026, 9, 12, 3, tzinfo=UTC)


def disposition(**overrides):
    fields = {
        "operation_id": "run-a",
        "observation_key": "tiktok:post:1",
        "identity_kind": "native",
        "native_namespace": "tiktok",
        "native_id": "post:1",
        "collection_event_id": "event-1",
        "source_row_id": "row-1",
        "boundary": "producer",
        "outcome": "admitted",
        "reason_code": None,
        "market": "za",
        "route": "socialcrawl:fetch",
        "observed_at": OBSERVED_AT,
        "source_binding_digest": "a" * 64,
    }
    fields.update(overrides)
    return disposition_record(**fields)


def test_repeated_collection_is_one_observation():
    result = summarize_observation_flow(
        {"post:1", "post:2"}, {"post:1", "post:2"}, {"post:1"}, set()
    )
    assert result == {
        "emitted_observations": 2,
        "persisted_observations": 2,
        "qualified_observations": 1,
        "classified_observations": 0,
        "persisted_not_qualified": 1,
    }


@pytest.mark.parametrize(
    ("emitted", "persisted", "qualified", "classified"),
    [
        ({"post:1"}, {"post:1", "post:2"}, set(), set()),
        ({"post:1", "post:2"}, {"post:1"}, {"post:2"}, set()),
        ({"post:1", "post:2"}, {"post:1", "post:2"}, {"post:1"}, {"post:2"}),
    ],
)
def test_non_subset_populations_are_refused(emitted, persisted, qualified, classified):
    with pytest.raises(ValueError, match="observation_population_mismatch"):
        summarize_observation_flow(emitted, persisted, qualified, classified)


def test_flow_counts_identities_not_rows():
    emitted = {"post:1", "post:2", "post:3"}
    result = summarize_observation_flow(emitted, {"post:1", "post:3"}, {"post:3"}, {"post:3"})
    assert result["persisted_not_qualified"] == 1
    assert result["classified_observations"] == 1


def test_disposition_record_round_trips_validated_fields():
    record = disposition()
    assert record["identity_kind"] == "native"
    assert record["native_id"] == "post:1"
    assert record["reason_code"] is None
    assert record["observed_at"] == OBSERVED_AT
    rejected = disposition(
        boundary="classification", outcome="rejected", reason_code="no_topic_match"
    )
    assert rejected["reason_code"] == "no_topic_match"


def test_inferred_identity_is_validated_and_counted_apart_from_native():
    inferred = disposition(
        observation_key="inferred:7f3a",
        identity_kind="inferred",
        native_namespace="rss",
        native_id=None,
        source_row_id=None,
    )
    assert inferred["identity_kind"] == "inferred"
    records = [
        disposition(collection_event_id="event-1", route="socialcrawl:fetch"),
        disposition(collection_event_id="event-2", route="socialcrawl:comments"),
        inferred,
        disposition(outcome="unknown", reason_code="telemetry_unavailable"),
    ]
    assert count_identities(records) == {
        "native_observations": 1,
        "inferred_identity_observations": 1,
        "unknown_dispositions": 1,
    }


def test_inferred_identity_cannot_claim_a_native_id_and_native_needs_both_parts():
    with pytest.raises(ValueError, match="inferred_identity_claims_native_id"):
        disposition(identity_kind="inferred")
    with pytest.raises(ValueError, match="native_identity_incomplete"):
        disposition(native_namespace=None)
    with pytest.raises(ValueError, match="native_identity_incomplete"):
        disposition(native_id=None)


def test_unavailable_telemetry_is_recorded_as_unknown_not_zero():
    unknown = disposition(outcome="unknown", reason_code="telemetry_unavailable")
    assert unknown["outcome"] == "unknown"
    with pytest.raises(ValueError, match="unknown_outcome_reason_invalid"):
        disposition(outcome="unknown", reason_code=None)
    with pytest.raises(ValueError, match="unknown_outcome_reason_invalid"):
        disposition(outcome="unknown", reason_code="guessed")
    with pytest.raises(ValueError, match="rejection_reason_missing"):
        disposition(outcome="rejected", reason_code=None)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"operation_id": ""}, "disposition_field_invalid:operation_id"),
        ({"observation_key": " key"}, "disposition_field_invalid:observation_key"),
        ({"identity_kind": "guessed"}, "disposition_field_invalid:identity_kind"),
        ({"collection_event_id": None}, "disposition_field_invalid:collection_event_id"),
        ({"boundary": "scoring"}, "disposition_field_invalid:boundary"),
        ({"outcome": "dropped"}, "disposition_field_invalid:outcome"),
        ({"reason_code": "No Match"}, "disposition_field_invalid:reason_code"),
        ({"market": "us"}, "disposition_field_invalid:market"),
        ({"route": ""}, "disposition_field_invalid:route"),
        ({"observed_at": datetime(2026, 9, 12, 3)}, "disposition_field_invalid:observed_at"),
        ({"observed_at": "2026-09-12T03:00:00Z"}, "disposition_field_invalid:observed_at"),
        ({"source_binding_digest": "A" * 64}, "disposition_field_invalid:source_binding_digest"),
    ],
)
def test_invalid_disposition_fields_are_refused(overrides, code):
    with pytest.raises(ValueError, match=code):
        disposition(**overrides)


def test_other_empty_and_null_labels_are_excluded_from_classified_counts():
    rows = [
        {"observation_key": "post:1", "topic_groups": ["other"]},
        {"observation_key": "post:2", "topic_groups": []},
        {"observation_key": "post:3", "topic_groups": None},
        {"observation_key": "post:4", "topic_groups": ""},
        {"observation_key": "post:5", "topic_groups": ["  ", "OTHER"]},
        {"observation_key": "post:6", "topic_groups": ["other", "music_amapiano"]},
        {"observation_key": "post:7", "topic_groups": "sport_football"},
        {"observation_key": "post:8"},
    ]
    classified = classified_observation_keys(rows)
    assert classified == {"post:6", "post:7"}
    keys = {row["observation_key"] for row in rows}
    assert summarize_observation_flow(keys, keys, keys, classified)["classified_observations"] == 2


# Boundary sink, distinct named units and the two controls

import re
from datetime import timedelta
from pathlib import Path

from src.analysis.open_intelligence.coverage_telemetry import (
    NATIVE_WINDOW_ASSERTS,
    NATIVE_WINDOW_PARAMETERS,
    NATIVE_WINDOW_QUERY_PATH,
    coverage_units_report,
)
from src.analysis.open_intelligence.coverage_telemetry_sink import (
    BoundaryTelemetry,
    DisabledDispositionSink,
    MemoryDispositionSink,
    observation_identity,
)

WINDOW = (datetime(2026, 9, 12, tzinfo=UTC), datetime(2026, 9, 14, tzinfo=UTC))


def bound_telemetry(sink=None, *, observed_at=OBSERVED_AT, operation_id="run-a"):
    telemetry = BoundaryTelemetry(sink if sink is not None else MemoryDispositionSink())
    telemetry.bind(
        operation_id=operation_id,
        source_binding_digest="a" * 64,
        observed_at=observed_at,
    )
    return telemetry


def fetched(**overrides):
    row = {
        "platform": "tiktok",
        "native_id": "post:1",
        "source": "socialcrawl",
        "url": "https://t.example/1",
        "market": "za",
    }
    row.update(overrides)
    return row


def raw(row_id, **overrides):
    return {"id": row_id, **fetched(**overrides)}


def test_observation_identity_is_native_only_when_the_row_carries_a_native_id():
    native = observation_identity({"platform": "tiktok", "native_id": "post:1", "url": "u"}, "ev-1")
    assert native == ("tiktok:post:1", "native", "tiktok", "post:1")
    key, kind, namespace, native_id = observation_identity(
        {"platform": "web", "url": "https://News.example/a?x=1"}, "ev-2"
    )
    assert (kind, namespace, native_id) == ("inferred", "web", None)
    assert key.startswith("url:news.example:")
    assert observation_identity({"platform": "web"}, "ev-3") == (
        "row:ev-3",
        "inferred",
        "web",
        None,
    )
    # A native id without its namespace is not a native identity.
    assert observation_identity({"native_id": "post:9"}, "ev-4")[1] == "inferred"
    # Identical text alone never merges two records.
    assert (
        observation_identity({"text": "same"}, "ev-5")[0]
        != (observation_identity({"text": "same"}, "ev-6")[0])
    )


def test_two_routes_returning_one_post_are_two_collection_events_and_one_observation():
    telemetry = bound_telemetry()
    telemetry.emit_collection(
        "za",
        [[fetched()], [fetched(source="brand24")]],
        [],
        [raw("row-1"), raw("row-2", source="brand24")],
    )
    producer = [record for record in telemetry.records if record["boundary"] == "producer"]
    assert [record["collection_event_id"] for record in producer] == [
        "run-a:za:socialcrawl:0:0",
        "run-a:za:brand24:1:0",
    ]
    assert [record["source_row_id"] for record in producer] == ["row-1", "row-2"]
    assert {record["observation_key"] for record in producer} == {"tiktok:post:1"}
    assert all(record["source_binding_digest"] == "a" * 64 for record in producer)
    report = coverage_units_report(WINDOW, telemetry)
    assert report["units"]["emissions"] == 2
    assert report["units"]["unique_native_observations"] == 1
    assert report["units"]["inferred_identity_observations"] == 0
    assert report["concentration"]["vendor"] == {"brand24": 0.5, "socialcrawl": 0.5}
    assert report["concentration"]["platform"] == {"tiktok": 1.0}
    assert report["concentration"]["common_origin"] == {"tiktok": 1.0}


def test_dedup_rejection_reason_comes_from_the_decision_record():
    telemetry = bound_telemetry()
    url = "https://www.youtube.com/watch?v=abc"
    api = fetched(platform="youtube", native_id=None, source="youtube", url=url)
    scrape = fetched(platform="youtube", native_id=None, source="youtube_scrape", url=url)
    decisions = [{"frame_index": 1, "row_index": 0, "reason_code": "youtube_scrape_duplicate"}]
    telemetry.emit_collection("za", [[api], [scrape]], decisions, [raw("row-1", **api)])
    dedup = {
        (record["collection_event_id"], record["outcome"], record["reason_code"])
        for record in telemetry.records
        if record["boundary"] == "dedup"
    }
    assert dedup == {
        ("run-a:za:youtube:0:0", "admitted", None),
        ("run-a:za:youtube_scrape:1:0", "rejected", "youtube_scrape_duplicate"),
    }
    rejected = next(record for record in telemetry.records if record["outcome"] == "rejected")
    assert rejected["source_row_id"] is None
    report = coverage_units_report(WINDOW, telemetry)
    assert report["units"]["emissions"] == 2
    assert report["units"]["inferred_identity_observations"] == 1
    assert report["transitions"]["dedup"] == {
        "admitted": 1,
        "rejected": {"youtube_scrape_duplicate": 1},
        "unknown": 0,
        "unrecorded": 0,
    }


def test_enrichment_and_classification_dispositions_come_from_the_enriched_rows():
    telemetry = bound_telemetry()
    raw_rows = [
        raw("row-1"),
        raw("row-2", native_id="post:2"),
        raw("row-3", native_id="post:3"),
        raw("row-4", native_id="post:4"),
    ]
    telemetry.emit_collection("za", [[dict(row) for row in raw_rows]], [], raw_rows)
    enriched = [
        {**raw_rows[0], "topic_groups": ["music_amapiano"]},
        {**raw_rows[1], "topic_groups": ["other"]},
        {**raw_rows[2], "topic_groups": []},
        {**raw_rows[3], "topic_groups": None},
    ]
    telemetry.emit_enrichment("za", raw_rows, enriched)
    classification = {
        record["source_row_id"]: (record["outcome"], record["reason_code"])
        for record in telemetry.records
        if record["boundary"] == "classification"
    }
    assert classification == {
        "row-1": ("admitted", None),
        "row-2": ("rejected", "unclassified"),
        "row-3": ("rejected", "unclassified"),
        "row-4": ("rejected", "unclassified"),
    }
    enrichment = [record for record in telemetry.records if record["boundary"] == "enrichment"]
    assert [record["outcome"] for record in enrichment] == ["admitted"] * 4
    report = coverage_units_report(WINDOW, telemetry)
    assert report["units"]["qualified_observations"] == 4
    assert report["units"]["classified_observations"] == 1
    keys = {f"tiktok:post:{n}" for n in (1, 2, 3, 4)}
    assert report["flow"] == summarize_observation_flow(keys, keys, keys, {"tiktok:post:1"})
    assert report["transitions"]["classification"]["rejected"] == {"unclassified": 3}


def test_whole_window_unique_observations_come_from_sets_not_per_day_sums():
    sink = MemoryDispositionSink()
    for day, operation_id in ((12, "run-1"), (13, "run-2")):
        telemetry = bound_telemetry(
            sink,
            observed_at=datetime(2026, 9, day, 3, tzinfo=UTC),
            operation_id=operation_id,
        )
        telemetry.emit_collection("za", [[fetched()]], [], [raw(f"row-{day}")])
    whole = coverage_units_report(WINDOW, sink)
    assert whole["units"]["emissions"] == 2
    assert whole["units"]["unique_native_observations"] == 1
    per_day = sum(
        coverage_units_report((start, start + timedelta(days=1)), sink)["units"][
            "unique_native_observations"
        ]
        for start in (WINDOW[0], WINDOW[0] + timedelta(days=1))
    )
    assert per_day == 2
    later = (WINDOW[1], WINDOW[1] + timedelta(days=1))
    assert coverage_units_report(later, sink)["units"]["emissions"] == 0
    with pytest.raises(ValueError, match="window_invalid"):
        coverage_units_report((WINDOW[1], WINDOW[0]), sink)


def test_units_without_telemetry_are_unknown_never_zero_and_losses_stay_same_identity():
    telemetry = bound_telemetry()
    telemetry.emit_collection("za", [[fetched()]], [], [raw("row-1")])
    report = coverage_units_report(WINDOW, telemetry)
    unknown_units = (
        "physical_persisted_rows",
        "topic_day_scores",
        "membership_edges",
        "candidates",
        "released_signals",
        "retrieved_observations",
        "admitted_receipts",
        "cited_receipts",
    )
    assert all(report["units"][unit] is None for unit in unknown_units)
    assert set(report["units"]) == {
        "emissions",
        "unique_native_observations",
        "inferred_identity_observations",
        "qualified_observations",
        "classified_observations",
        *unknown_units,
    }
    telemetry.count("physical_persisted_rows", 7, market="za")
    telemetry.count("topic_day_scores", 3, market="za")
    report = coverage_units_report(WINDOW, telemetry)
    assert report["units"]["physical_persisted_rows"] == 7
    assert report["units"]["topic_day_scores"] == 3
    assert set(report["transitions"]) == {
        "dedup",
        "enrichment",
        "classification",
        "membership",
    }
    # No enrichment record exists: the key is unrecorded there, never a subtracted loss.
    assert report["transitions"]["enrichment"] == {
        "admitted": 0,
        "rejected": {},
        "unknown": 0,
        "unrecorded": 1,
    }
    with pytest.raises(ValueError, match="unit_unknown"):
        telemetry.count("rows", 1, market="za")
    with pytest.raises(ValueError, match="count_invalid"):
        telemetry.count("candidates", -1, market="za")


def test_failed_source_and_known_quiet_controls_keep_a_zero_from_hiding_a_broken_reader():
    telemetry = bound_telemetry()
    news = fetched(platform="web", native_id=None, source="rss", url="https://news.example/a")
    telemetry.emit_collection("za", [[news]], [], [raw("row-1", **news)])
    counts = {"za": {"rss": 1, "gdelt": 0, "brand24": 0, "apple_music": 0}}
    errors = [
        {"market": "za", "source": "gdelt", "error": "HTTP 503", "fatal": True},
        {"market": "za", "source": "rss", "error": "one dead feed", "fatal": False},
    ]
    telemetry.route_outcomes(counts, errors)
    report = coverage_units_report(WINDOW, telemetry, known_quiet={("za", "apple_music")})
    assert report["routes"]["za"] == {
        "rss": {"state": "emitting", "emissions": 1, "reason_code": "endpoint_failure"},
        "gdelt": {"state": "failed", "emissions": 0, "reason_code": "fetch_failed"},
        "apple_music": {
            "state": "known_quiet",
            "emissions": 0,
            "reason_code": "declared_quiet",
        },
        "brand24": {
            "state": "unexplained_zero",
            "emissions": 0,
            "reason_code": "zero_without_decision_record",
        },
    }
    assert report["controls"] == {
        "failed_routes": 1,
        "known_quiet_routes": 1,
        "unexplained_zero_routes": 1,
    }
    assert report["units"]["qualified_observations"] == 0
    telemetry.route_outcomes(
        {"ng": {"rss": 2}},
        [
            {"market": "ng", "source": "pipeline", "error": "enrich crashed", "fatal": True},
            {"market": "ke", "source": "pipeline", "error": "insert failed", "fatal": True},
        ],
    )
    report = coverage_units_report(WINDOW, telemetry, known_quiet={("za", "apple_music")})
    assert [(m["boundary"], m["market"], m["reason_code"]) for m in report["unavailable"]] == [
        ("enrichment", "ng", "stage_failed"),
        ("classification", "ng", "stage_failed"),
        ("enrichment", "ke", "stage_failed"),
        ("classification", "ke", "stage_failed"),
    ]
    assert report["units"]["qualified_observations"] is None
    assert report["units"]["classified_observations"] is None
    assert report["units"]["emissions"] == 1


def test_unavailable_boundaries_are_reported_unknown_and_a_disabled_sink_drops_everything():
    telemetry = bound_telemetry()
    telemetry.boundary_unavailable(
        "capture",
        operation_id="attempt-1",
        reason_code="capture_result_without_observation_identity",
    )
    report = coverage_units_report(WINDOW, telemetry)
    assert report["unavailable"] == [
        {
            "boundary": "capture",
            "operation_id": "attempt-1",
            "reason_code": "capture_result_without_observation_identity",
            "market": None,
        }
    ]
    disabled = BoundaryTelemetry(DisabledDispositionSink())
    disabled.bind(operation_id="run-a", source_binding_digest="a" * 64, observed_at=OBSERVED_AT)
    disabled.emit_collection("za", [[fetched()]], [], [raw("row-1")])
    disabled.count("physical_persisted_rows", 1, market="za")
    disabled.boundary_unavailable("release", operation_id="run-a", reason_code="x_y")
    assert disabled.records == []
    assert disabled.notes == []
    off = coverage_units_report(WINDOW, disabled)
    assert off["telemetry"] == "disabled"
    assert all(value is None for value in off["units"].values())


def test_a_faulty_disposition_never_stops_the_producer_it_is_recorded_unavailable():
    telemetry = BoundaryTelemetry(MemoryDispositionSink())
    telemetry.bind(
        operation_id="run-a",
        source_binding_digest="not-a-digest",
        observed_at=OBSERVED_AT,
    )
    telemetry.emit_collection("za", [[fetched()]], [], [raw("row-1")])
    assert telemetry.records == []
    unavailable = [note for note in telemetry.notes if note["kind"] == "unavailable"]
    assert {note["boundary"] for note in unavailable} == {"producer", "dedup"}
    assert {note["reason_code"] for note in unavailable} == {"disposition_invalid"}
    report = coverage_units_report(WINDOW, telemetry)
    assert report["units"]["emissions"] is None
    assert report["units"]["unique_native_observations"] is None
    assert report["units"]["inferred_identity_observations"] is None
    assert len(report["unavailable"]) == 2
    assert all(entry["market"] == "za" for entry in report["unavailable"])
    with pytest.raises(ValueError, match="telemetry_unbound"):
        BoundaryTelemetry(MemoryDispositionSink()).emit_collection("za", [], [], [])


def test_a_malformed_link_never_reaches_the_producer_it_is_recorded_unavailable():
    telemetry = bound_telemetry()
    bad = fetched(platform="web", native_id=None, source="rss", url="http://[bad/x")
    good = fetched(platform="web", native_id=None, source="rss", url="https://news.example/a")
    telemetry.emit_collection("za", [[bad, good]], [], [raw("row-1", **bad), raw("row-2", **good)])
    assert [record["source_row_id"] for record in telemetry.records] == ["row-2", "row-2"]
    unavailable = [note for note in telemetry.notes if note["kind"] == "unavailable"]
    assert [(note["boundary"], note["market"], note["reason_code"]) for note in unavailable] == [
        ("producer", "za", "emission_failed"),
        ("dedup", "za", "emission_failed"),
    ]
    assert "Invalid IPv6 URL" in unavailable[0]["detail"]
    enriched = [{**raw("row-2", **good), "topic_groups": ["news_politics"]}]
    telemetry.emit_enrichment("za", [raw("row-2", **good)], enriched)
    assert [record["boundary"] for record in telemetry.records[2:]] == [
        "enrichment",
        "classification",
    ]

    class BrokenSink(MemoryDispositionSink):
        def emit(self, record):
            raise RuntimeError("warehouse write refused")

    broken = bound_telemetry(BrokenSink())
    broken.emit_collection("za", [[good]], [], [raw("row-1", **good)])
    broken.emit_membership(
        [
            {
                "run_id": "run-m",
                "signal_id": "sig-1",
                "member_identity": "za|term|x",
                "candidate_type": "term",
                "row_id": "ev-1",
                "qualifies_evidence": True,
                "market": "za",
            }
        ],
        created_at=OBSERVED_AT,
        source_binding_digest="b" * 64,
        availability_by_member={},
    )
    assert broken.records == []
    assert [(note["boundary"], note["reason_code"]) for note in broken.notes] == [
        ("producer", "emission_failed"),
        ("dedup", "emission_failed"),
        ("membership", "emission_failed"),
    ]

    class LateBrokenSink(MemoryDispositionSink):
        def emit(self, record):
            if record["boundary"] in {"enrichment", "classification"}:
                raise RuntimeError("warehouse write refused")
            super().emit(record)

    late = bound_telemetry(LateBrokenSink())
    late.emit_collection("za", [[good]], [], [raw("row-1", **good)])
    late.emit_enrichment(
        "za", [raw("row-1", **good)], [{**raw("row-1", **good), "topic_groups": ["x"]}]
    )
    assert [record["boundary"] for record in late.records] == ["producer", "dedup"]
    assert [(note["boundary"], note["reason_code"]) for note in late.notes] == [
        ("enrichment", "emission_failed"),
        ("classification", "emission_failed"),
    ]

    class DeadSink(BrokenSink):
        def note(self, note):
            raise RuntimeError("note refused")

    dead = bound_telemetry(DeadSink())
    dead.emit_collection("za", [[good]], [], [raw("row-1", **good)])
    dead.count("physical_persisted_rows", 1, market="za")
    dead.route_outcomes({"za": {"rss": 1}}, [])
    dead.boundary_unavailable("release", operation_id="run-a", reason_code="x_y")
    assert dead.records == []
    assert dead.notes == []


def test_syndicated_copies_stay_distinct_but_never_multiply_independent_support():
    telemetry = bound_telemetry()
    copies = [
        fetched(platform="web", native_id=None, source="rss", url="https://a.example/story"),
        fetched(platform="web", native_id=None, source="rss", url="https://b.example/story"),
    ]
    telemetry.emit_collection(
        "za", [copies], [], [raw("row-1", **copies[0]), raw("row-2", **copies[1])]
    )
    report = coverage_units_report(WINDOW, telemetry)
    assert report["units"]["inferred_identity_observations"] == 2
    assert report["units"]["unique_native_observations"] == 0
    assert report["concentration"]["common_origin"] == {
        "a.example": 0.5,
        "b.example": 0.5,
    }
    assert report["independent_support"] == {
        "basis": "membership_edges_qualifying",
        "value": None,
    }


def test_membership_edges_are_inferred_identities_with_reasons_from_the_availability_record():
    telemetry = BoundaryTelemetry(MemoryDispositionSink())
    rows = [
        {
            "run_id": "run-m",
            "signal_id": "sig-1",
            "member_identity": "za|term|amapiano",
            "candidate_type": "term",
            "row_id": "ev-1",
            "qualifies_evidence": True,
            "market": "za",
        },
        {
            "run_id": "run-m",
            "signal_id": "sig-1",
            "member_identity": "za|term|gqom",
            "candidate_type": "term",
            "row_id": "ev-2",
            "qualifies_evidence": False,
            "market": "za",
        },
        {
            "run_id": "run-m",
            "signal_id": "sig-1",
            "member_identity": "za|term|kwaito",
            "candidate_type": "term",
            "row_id": "ev-3",
            "qualifies_evidence": False,
            "market": "za",
        },
    ]
    telemetry.emit_membership(
        rows,
        created_at=OBSERVED_AT,
        source_binding_digest="b" * 64,
        availability_by_member={
            "za|term|amapiano": "available",
            "za|term|gqom": "unavailable",
        },
    )
    assert [
        (
            record["operation_id"],
            record["observation_key"],
            record["identity_kind"],
            record["native_id"],
            record["outcome"],
            record["reason_code"],
            record["route"],
            record["collection_event_id"],
            record["source_row_id"],
        )
        for record in telemetry.records
    ] == [
        (
            "run-m",
            "za|term|amapiano",
            "inferred",
            None,
            "admitted",
            None,
            "term",
            "sig-1",
            "ev-1",
        ),
        (
            "run-m",
            "za|term|gqom",
            "inferred",
            None,
            "rejected",
            "member_unavailable",
            "term",
            "sig-1",
            "ev-2",
        ),
        (
            "run-m",
            "za|term|kwaito",
            "inferred",
            None,
            "rejected",
            "member_receipt_missing",
            "term",
            "sig-1",
            "ev-3",
        ),
    ]
    # The bridge measures its own units; the dispositions never stand in for them.
    telemetry.count("candidates", 1, operation_id="run-m", observed_at=OBSERVED_AT)
    telemetry.count("membership_edges", 3, operation_id="run-m", observed_at=OBSERVED_AT)
    report = coverage_units_report(WINDOW, telemetry)
    assert report["units"]["membership_edges"] == 3
    assert report["units"]["candidates"] == 1
    assert report["independent_support"] == {
        "basis": "membership_edges_qualifying",
        "value": 1,
    }
    assert report["identity_counts"] == {
        "native_observations": 0,
        "inferred_identity_observations": 0,
        "unknown_dispositions": 0,
    }
    assert report["units"]["inferred_identity_observations"] == 0
    assert report["transitions"]["membership"]["admitted"] == 1
    assert report["transitions"]["membership"]["rejected"] == {
        "member_receipt_missing": 1,
        "member_unavailable": 1,
    }


def test_native_window_query_text_declares_its_asserts_and_parameters_without_running():
    text = Path(NATIVE_WINDOW_QUERY_PATH).read_text(encoding="utf-8")
    declared_asserts = re.findall(r"ASSERT\s.+?\sAS\s+'([a-z_]+)'", text, re.S)
    assert declared_asserts == list(NATIVE_WINDOW_ASSERTS)
    assert len(set(declared_asserts)) == len(declared_asserts)
    declared_parameters = set(re.findall(r"@([a-z_]+)", text))
    assert declared_parameters == set(NATIVE_WINDOW_PARAMETERS)
    assert {
        "window_start",
        "window_end",
        "market_strata",
        "query_limit",
    } <= declared_parameters
    assert "LIMIT @query_limit" in text
    assert "COUNT(DISTINCT" in text
    assert "SUM(distinct" not in text.lower()
    assert "GROUP BY market" in text
    assert "UNNEST(@market_strata)" in text
