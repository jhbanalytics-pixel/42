"""Boundary tests for deterministic evidence readiness."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from src.analysis.open_intelligence.candidates import extract_event_observations
from src.analysis.open_intelligence.readiness import (
    EvidenceRecord,
    ReadinessResult,
    ReadinessRules,
    evaluate_readiness,
)

FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures" / "open_intelligence" / "v2"
FIXTURE_FAMILY_ADAPTER = {"forum": "reddit", "video": "youtube"}
CURRENT_CUTOFF = datetime(2026, 8, 24, tzinfo=UTC)


def record(
    row_id: str,
    family: str,
    direction: str,
    *,
    published_at: datetime = datetime(2026, 8, 24, 9, tzinfo=UTC),
    availability: str = "available",
    geo_confidence: float = 0.91,
    factual_conflict: bool = False,
) -> EvidenceRecord:
    return EvidenceRecord(
        row_id=row_id,
        source_family=family,
        direction=direction,
        published_at=published_at,
        availability=availability,
        geo_confidence=geo_confidence,
        factual_conflict=factual_conflict,
    )


def rules(
    *,
    current_cutoff: datetime = CURRENT_CUTOFF,
    minimum_geo_confidence: float = 0.8,
) -> ReadinessRules:
    return ReadinessRules(
        current_cutoff=current_cutoff,
        minimum_geo_confidence=minimum_geo_confidence,
    )


def fixture_case(name: str) -> tuple[list[EvidenceRecord], dict]:
    fixture = json.loads((FIXTURE_DIRECTORY / f"{name}.json").read_text(encoding="utf-8"))
    payload = fixture["payload"]
    records = [
        record(
            item["row_id"],
            FIXTURE_FAMILY_ADAPTER[item["source_family"]],
            item["direction"],
            published_at=datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")),
            availability=item["availability"],
            geo_confidence=item["geo_confidence"],
        )
        for item in payload["evidence"]
    ]
    return records, payload


@pytest.mark.parametrize(
    "fixture_name",
    ["evidence_ready", "evidence_thin", "evidence_contradictory", "evidence_unchecked"],
)
def test_frozen_readiness_fixtures_match_the_pure_evaluator(fixture_name):
    records, payload = fixture_case(fixture_name)
    result = evaluate_readiness(
        records,
        rules(),
        quality_evaluated=fixture_name != "evidence_unchecked",
    )
    expected_directions = {
        FIXTURE_FAMILY_ADAPTER[family]: direction
        for family, direction in payload["direction_by_family"].items()
    }
    assert result.state == payload["evidence_state"]
    assert len(result.qualifying_families) == payload["qualifying_family_count"]
    assert dict(result.direction_by_family) == expected_directions


def test_quality_state_has_first_precedence_and_failure_is_never_thin():
    coherent = [record("row_1", "reddit", "rising"), record("row_2", "youtube", "rising")]
    not_run = evaluate_readiness(
        coherent,
        rules(),
        quality_evaluated=False,
        factual_conflict=True,
    )
    failed = evaluate_readiness(
        coherent,
        rules(),
        quality_evaluated=True,
        quality_failed=True,
    )
    assert not_run.state == "unchecked"
    assert not_run.reasons == ("quality_not_evaluated",)
    assert failed.state == "unchecked"
    assert failed.reasons == ("quality_evaluation_failed",)


def test_explicit_factual_conflict_is_contradictory_before_family_count():
    result = evaluate_readiness(
        [],
        rules(),
        quality_evaluated=True,
        factual_conflict=True,
    )
    assert result.state == "contradictory"
    assert result.reasons == ("factual_conflict",)
    assert result.qualifying_families == ()


def test_retained_factual_conflict_is_contradictory_even_when_stale():
    stale_conflict = record(
        "row_0",
        "news",
        "declining",
        published_at=CURRENT_CUTOFF - timedelta(seconds=1),
        factual_conflict=True,
    )
    coherent = [record("row_1", "reddit", "rising"), record("row_2", "youtube", "rising")]
    result = evaluate_readiness(
        [stale_conflict, *coherent],
        rules(),
        quality_evaluated=True,
    )
    assert result.state == "contradictory"
    assert result.reasons == ("factual_conflict",)


def test_brand24_is_rejected_from_active_v2_readiness():
    with pytest.raises(ValueError, match="source family"):
        record("row_brand24", "brand24", "rising")


def test_missing_publish_time_is_retained_but_cannot_qualify():
    result = evaluate_readiness(
        [record("row_missing_time", "reddit", "rising", published_at=None)],
        rules(),
        quality_evaluated=True,
    )
    assert result.state == "thin"
    assert result.qualifying_families == ()
    assert result.reasons == ("insufficient_qualifying_families", "missing_published_at")


def test_upstream_platform_aliases_stay_one_family_through_readiness():
    observations = extract_event_observations(
        [
            {
                "ledger_id": "alias_row",
                "market": "za",
                "entity_key": "repair routine",
                "entity_aliases": [],
                "event_kind": "entity",
                "corroborating_sources": ["tiktok", "instagram"],
            }
        ]
    )
    records = [
        record("alias_row", family, "rising")
        for observation in observations
        for family in observation.source_families
    ]
    result = evaluate_readiness(records, rules(), quality_evaluated=True)
    assert result.state == "thin"
    assert result.qualifying_families == ("short_video",)


def test_only_available_current_geo_passing_records_qualify():
    records = [
        record("row_current", "reddit", "rising"),
        record(
            "row_stale",
            "youtube",
            "rising",
            published_at=CURRENT_CUTOFF - timedelta(microseconds=1),
        ),
        record("row_weak_geo", "news", "rising", geo_confidence=0.79),
        record("row_aged", "search", "rising", availability="aged_out"),
        record("row_unavailable", "wikipedia", "rising", availability="unavailable"),
    ]
    result = evaluate_readiness(records, rules(), quality_evaluated=True)
    assert result.state == "thin"
    assert result.qualifying_families == ("reddit",)
    assert result.reasons == (
        "aged_out_evidence",
        "insufficient_qualifying_families",
        "stale_evidence",
        "unavailable_evidence",
        "weak_geo_evidence",
    )


def test_freshness_and_geo_floors_are_inclusive():
    result = evaluate_readiness(
        [
            record(
                "row_1",
                "reddit",
                "stable",
                published_at=CURRENT_CUTOFF,
                geo_confidence=0.8,
            ),
            record(
                "row_2",
                "youtube",
                "stable",
                published_at=CURRENT_CUTOFF,
                geo_confidence=0.8,
            ),
        ],
        rules(),
        quality_evaluated=True,
    )
    assert result.state == "ready"


def test_multiple_rows_from_one_family_remain_thin():
    result = evaluate_readiness(
        [record("row_1", "reddit", "rising"), record("row_2", "reddit", "rising")],
        rules(),
        quality_evaluated=True,
    )
    assert result.state == "thin"
    assert result.qualifying_families == ("reddit",)
    assert result.direction_by_family == {"reddit": "rising"}


def test_opposed_family_directions_are_contradictory():
    result = evaluate_readiness(
        [record("row_1", "reddit", "rising"), record("row_2", "youtube", "declining")],
        rules(),
        quality_evaluated=True,
    )
    assert result.state == "contradictory"
    assert result.reasons == ("opposing_family_directions",)
    assert dict(result.direction_by_family) == {"reddit": "rising", "youtube": "declining"}


def test_explicit_or_in_family_conflicting_direction_is_contradictory():
    explicit = evaluate_readiness(
        [record("row_1", "reddit", "conflicting"), record("row_2", "youtube", "rising")],
        rules(),
        quality_evaluated=True,
    )
    aggregated = evaluate_readiness(
        [
            record("row_3", "reddit", "rising"),
            record("row_4", "reddit", "declining"),
            record("row_5", "youtube", "rising"),
        ],
        rules(),
        quality_evaluated=True,
    )
    assert explicit.state == "contradictory"
    assert explicit.reasons == ("conflicting_direction",)
    assert aggregated.state == "contradictory"
    assert aggregated.direction_by_family["reddit"] == "conflicting"


def test_not_applicable_cannot_create_directional_agreement():
    thin = evaluate_readiness(
        [
            record("row_1", "reddit", "rising"),
            record("row_2", "youtube", "not_applicable"),
        ],
        rules(),
        quality_evaluated=True,
    )
    ready = evaluate_readiness(
        [
            record("row_3", "reddit", "rising"),
            record("row_4", "youtube", "not_applicable"),
            record("row_5", "news", "rising"),
        ],
        rules(),
        quality_evaluated=True,
    )
    assert thin.state == "thin"
    assert thin.reasons == ("insufficient_directional_agreement",)
    assert ready.state == "ready"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_family", "forum"),
        ("source_family", "REDDIT"),
        ("direction", "up"),
        ("availability", "live"),
    ],
)
def test_unknown_or_noncanonical_enums_are_rejected(field, value):
    kwargs = {
        "row_id": "row_1",
        "source_family": "reddit",
        "direction": "rising",
        "published_at": CURRENT_CUTOFF,
        "availability": "available",
        "geo_confidence": 0.9,
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match=field.replace("_", " ")):
        EvidenceRecord(**kwargs)


@pytest.mark.parametrize("bad_geo", [None, True, -0.1, 1.1, float("nan"), "0.9"])
def test_malformed_geo_confidence_is_rejected(bad_geo):
    with pytest.raises(ValueError, match="geo confidence"):
        record("row_1", "reddit", "rising", geo_confidence=bad_geo)


def test_naive_timestamps_duplicate_rows_and_invalid_booleans_are_rejected():
    with pytest.raises(ValueError, match="timezone aware"):
        record("row_1", "reddit", "rising", published_at=datetime(2026, 8, 24, 9))
    with pytest.raises(ValueError, match="factual conflict"):
        record("row_1", "reddit", "rising", factual_conflict=1)
    duplicate = [record("same", "reddit", "rising"), record("same", "youtube", "rising")]
    with pytest.raises(ValueError, match="duplicate row id"):
        evaluate_readiness(duplicate, rules(), quality_evaluated=True)
    with pytest.raises(ValueError, match="quality evaluated"):
        evaluate_readiness([], rules(), quality_evaluated=1)
    with pytest.raises(ValueError, match="quality failed"):
        evaluate_readiness([], rules(), quality_evaluated=True, quality_failed=1)
    with pytest.raises(ValueError, match="factual conflict"):
        evaluate_readiness([], rules(), quality_evaluated=True, factual_conflict=1)


def test_rules_require_explicit_strict_values_and_normalize_to_utc():
    with pytest.raises(TypeError):
        ReadinessRules()
    with pytest.raises(ValueError, match="timezone aware"):
        rules(current_cutoff=datetime(2026, 8, 24))
    for bad in (None, True, -0.1, 1.1, float("nan"), "0.8"):
        with pytest.raises(ValueError, match="minimum geo confidence"):
            rules(minimum_geo_confidence=bad)
    offset = datetime.fromisoformat("2026-08-24T02:00:00+02:00")
    assert rules(current_cutoff=offset).current_cutoff == CURRENT_CUTOFF


def test_inputs_and_outputs_are_immutable_and_order_independent():
    first = record("row_2", "youtube", "rising")
    second = record("row_1", "reddit", "rising")
    forward = evaluate_readiness([first, second], rules(), quality_evaluated=True)
    reverse = evaluate_readiness([second, first], rules(), quality_evaluated=True)
    assert forward == reverse
    assert tuple(forward.direction_by_family) == ("reddit", "youtube")
    assert isinstance(forward, ReadinessResult)
    with pytest.raises(FrozenInstanceError):
        first.direction = "declining"
    with pytest.raises(FrozenInstanceError):
        forward.state = "thin"
    with pytest.raises(TypeError):
        forward.direction_by_family["reddit"] = "declining"


V2 = "explicit_origin_v2"
V2_TAG = "independence_policy:explicit_origin_v2"
# Explicit vendor identities per family for the explicit origin tests only: distinct
# families carry distinct vendors where a test expects independence.
EXPLICIT_VENDOR = {
    "news": "rss",
    "reddit": "socialcrawl",
    "youtube": "google_youtube",
    "search": "google_trends",
}


def explicit_origin(family: str) -> tuple[str, str]:
    return (EXPLICIT_VENDOR[family], family)


def origin_record(
    row_id: str,
    family: str,
    direction: str,
    origin: tuple[str, str] | None,
    **overrides,
) -> EvidenceRecord:
    vendor, channel = origin if origin is not None else (None, None)
    return EvidenceRecord(
        row_id=row_id,
        source_family=family,
        direction=direction,
        published_at=overrides.pop("published_at", datetime(2026, 8, 24, 9, tzinfo=UTC)),
        availability=overrides.pop("availability", "available"),
        geo_confidence=overrides.pop("geo_confidence", 0.91),
        factual_conflict=overrides.pop("factual_conflict", False),
        vendor_family=vendor,
        channel_family=channel,
    )


def evaluate_v2(records, **kwargs):
    kwargs.setdefault("quality_evaluated", True)
    return evaluate_readiness(records, rules(), independence_policy=V2, **kwargs)


def test_the_default_policy_is_the_legacy_rule_and_is_stored_on_the_result():
    result = evaluate_readiness(
        [record("row_1", "reddit", "rising"), record("row_2", "youtube", "rising")],
        rules(),
        quality_evaluated=True,
    )
    assert result.state == "ready"
    assert result.reasons == ()
    assert result.independence_policy == "wave1_family_v1"
    assert result.independent_pairs == (("row_1", "row_2"),)
    with pytest.raises(ValueError, match="independence policy"):
        evaluate_readiness(
            [record("row_1", "reddit", "rising")],
            rules(),
            quality_evaluated=True,
            independence_policy="explicit_origin_v3",
        )
    with pytest.raises(ValueError, match="independence policy"):
        ReadinessResult(
            state="thin",
            qualifying_families=(),
            direction_by_family={},
            reasons=(),
            independence_policy="v9",
        )


def test_the_legacy_rule_pairs_by_family_whatever_identity_the_records_carry():
    placeholders = [
        origin_record("row_1", "reddit", "rising", ("reddit", "reddit")),
        origin_record("row_2", "youtube", "rising", ("google_youtube", "youtube")),
    ]
    legacy = evaluate_readiness(placeholders, rules(), quality_evaluated=True)
    assert legacy.state == "ready"
    assert legacy.reasons == ()
    assert legacy.independent_pairs == (("row_1", "row_2"),)
    one_vendor = [
        origin_record("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
        origin_record("row_2", "youtube", "rising", ("socialcrawl", "youtube")),
    ]
    one_vendor_result = evaluate_readiness(one_vendor, rules(), quality_evaluated=True)
    assert one_vendor_result.state == "ready"
    assert one_vendor_result.reasons == ()
    mixed = [
        origin_record("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
        origin_record("row_2", "youtube", "rising", None),
    ]
    assert evaluate_readiness(mixed, rules(), quality_evaluated=True).state == "ready"
    same_family = [
        origin_record("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
        origin_record("row_2", "reddit", "rising", ("apify", "reddit")),
    ]
    assert evaluate_readiness(same_family, rules(), quality_evaluated=True).state == "thin"


def test_one_origin_under_two_family_labels_stays_thin():
    records = [
        origin_record("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
        origin_record("row_2", "youtube", "rising", ("socialcrawl", "reddit")),
    ]
    result = evaluate_v2(records)
    assert result.state == "thin"
    assert result.reasons == (V2_TAG, "insufficient_independent_support")
    assert result.independence_policy == V2
    assert result.independent_pairs == ()
    assert result.qualifying_row_ids == ("row_1", "row_2")
    assert result.context_row_ids == ("row_1", "row_2")


def test_same_vendor_different_channels_are_not_independent():
    records = [
        origin_record("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
        origin_record("row_2", "youtube", "rising", ("socialcrawl", "youtube")),
    ]
    result = evaluate_v2(records)
    assert result.state == "thin"
    assert result.reasons == (V2_TAG, "insufficient_independent_support")


def test_two_admitted_independent_members_become_ready():
    records = [
        origin_record("row_2", "news", "rising", explicit_origin("news")),
        origin_record("row_1", "reddit", "rising", explicit_origin("reddit")),
    ]
    result = evaluate_v2(records)
    assert result.state == "ready"
    assert result.reasons == (V2_TAG,)
    assert result.independent_pairs == (("row_1", "row_2"),)
    assert result.qualifying_row_ids == ("row_1", "row_2")
    reverse = evaluate_v2(list(reversed(records)))
    assert reverse == result


def test_unknown_origin_is_never_asserted_independent():
    records = [
        origin_record("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
        origin_record("row_2", "news", "rising", None),
    ]
    result = evaluate_v2(records)
    assert result.state == "thin"
    assert result.reasons == (V2_TAG, "insufficient_independent_support")
    assert result.independent_pairs == ()
    origin_free = [record("row_1", "reddit", "rising"), record("row_2", "youtube", "rising")]
    assert evaluate_v2(origin_free).state == "thin"


def test_pairs_need_both_members_supporting_the_candidate_direction():
    records = [
        origin_record("row_1", "reddit", "rising", ("socialcrawl", "reddit")),
        origin_record("row_2", "news", "rising", ("rss", "news")),
        origin_record("row_3", "youtube", "not_applicable", ("google_youtube", "youtube")),
    ]
    result = evaluate_v2(records)
    assert result.state == "ready"
    assert result.independent_pairs == (("row_1", "row_2"),)
    assert result.qualifying_row_ids == ("row_1", "row_2", "row_3")


def test_existing_freshness_geo_and_conflict_rules_still_apply_before_independence():
    stale = origin_record(
        "row_2",
        "news",
        "rising",
        ("rss", "news"),
        published_at=datetime(2026, 8, 23, 9, tzinfo=UTC),
    )
    fresh = origin_record("row_1", "reddit", "rising", ("socialcrawl", "reddit"))
    result = evaluate_v2([fresh, stale])
    assert result.state == "thin"
    assert result.reasons == (V2_TAG, "insufficient_qualifying_families", "stale_evidence")
    assert result.qualifying_row_ids == ("row_1",)
    assert result.context_row_ids == ("row_1", "row_2")
    opposed = evaluate_v2([fresh, origin_record("row_2", "news", "declining", ("rss", "news"))])
    assert opposed.state == "contradictory"
    assert opposed.independent_pairs == ()
    assert opposed.context_row_ids == ("row_1", "row_2")
    unchecked = evaluate_v2([fresh, stale], quality_evaluated=False)
    assert unchecked.state == "unchecked"
    assert unchecked.reasons == (V2_TAG, "quality_not_evaluated")
    assert unchecked.context_row_ids == ("row_1", "row_2")


def test_partial_origin_is_rejected():
    with pytest.raises(ValueError, match="source origin"):
        EvidenceRecord(
            row_id="row_1",
            source_family="reddit",
            direction="rising",
            published_at=datetime(2026, 8, 24, 9, tzinfo=UTC),
            availability="available",
            geo_confidence=0.9,
            vendor_family="socialcrawl",
        )
