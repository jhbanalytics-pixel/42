"""The independence rule on the live chain, versioned by run profile policy.

Under explicit_origin_v2 receipts hand readiness only an explicit validated identity, so a
placeholder is unknown origin and never pairs. Under wave1_family_v1, the default, the same
inputs give the retained legacy outputs byte for byte.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence import rows as rows_module
from src.analysis.open_intelligence.readiness import EvidenceRecord, evaluate_readiness

from tests.unit.test_dynamic_signal_rows import build, readiness_rules, receipt

PUBLISHED = datetime(2026, 8, 24, 19, 20, tzinfo=UTC)
V1 = "wave1_family_v1"
V2 = "explicit_origin_v2"
V2_TAG = "independence_policy:explicit_origin_v2"


def origin_receipt(row_id: str, family: str, vendor: str | None) -> object:
    item = receipt(row_id, family, family, "rising", url=None, published_at=PUBLISHED)
    if vendor is None:
        return item
    return replace(item, vendor_family=vendor, channel_family=family)


def placeholder_pair() -> MappingProxyType:
    return MappingProxyType(
        {
            "fixture_row_001": origin_receipt("fixture_row_001", "reddit", None),
            "fixture_row_002": origin_receipt("fixture_row_002", "youtube", None),
        }
    )


def mixed_pair() -> MappingProxyType:
    return MappingProxyType(
        {
            "fixture_row_001": origin_receipt("fixture_row_001", "reddit", "socialcrawl"),
            "fixture_row_002": origin_receipt("fixture_row_002", "youtube", None),
        }
    )


def explicit_pair() -> MappingProxyType:
    return MappingProxyType(
        {
            "fixture_row_001": origin_receipt("fixture_row_001", "reddit", "socialcrawl"),
            "fixture_row_002": origin_receipt("fixture_row_002", "youtube", "google_youtube"),
        }
    )


def one_vendor_pair() -> MappingProxyType:
    return MappingProxyType(
        {
            "fixture_row_001": origin_receipt("fixture_row_001", "reddit", "socialcrawl"),
            "fixture_row_002": origin_receipt("fixture_row_002", "youtube", "socialcrawl"),
        }
    )


def chain(receipt_mapping: MappingProxyType, policy: str = V1):
    records = rows_module._readiness_records(tuple(receipt_mapping.values()), policy)
    result = evaluate_readiness(
        records, readiness_rules(), quality_evaluated=True, independence_policy=policy
    )
    candidate, evidence_rows = build(
        receipts=receipt_mapping, readiness=result, independence_policy=policy
    )
    return records, result, candidate, evidence_rows


def test_placeholder_receipts_are_marked_unresolved_and_explicit_ones_resolved():
    for item in placeholder_pair().values():
        assert item.origin_resolved is False
        assert item.source_origin is None
    explicit = explicit_pair()
    assert explicit["fixture_row_001"].origin_resolved is True
    assert explicit["fixture_row_001"].source_origin == ("socialcrawl", "reddit")
    assert [(item.vendor_family, item.channel_family) for item in placeholder_pair().values()] == [
        ("reddit", "reddit"),
        ("google_youtube", "youtube"),
    ]


def test_readiness_records_hand_the_origin_the_policy_admits():
    v2 = rows_module._readiness_records(tuple(mixed_pair().values()), V2)
    assert [(item.vendor_family, item.channel_family) for item in v2] == [
        ("socialcrawl", "reddit"),
        (None, None),
    ]
    v1 = rows_module._readiness_records(tuple(mixed_pair().values()), V1)
    assert [(item.vendor_family, item.channel_family) for item in v1] == [
        (None, None),
        (None, None),
    ]
    default = rows_module._readiness_records(tuple(mixed_pair().values()))
    assert default == v1
    assert all(isinstance(item, EvidenceRecord) for item in v1)
    with pytest.raises(ValueError, match="independence policy"):
        rows_module._readiness_records(tuple(mixed_pair().values()), "explicit_origin_v3")


def test_two_placeholder_rows_stay_thin_under_the_explicit_origin_rule():
    _records, result, candidate, evidence_rows = chain(placeholder_pair(), V2)
    assert result.state == "thin"
    assert result.reasons == (V2_TAG, "insufficient_independent_support")
    assert result.independence_policy == V2
    assert result.independent_pairs == ()
    assert result.qualifying_row_ids == ("fixture_row_001", "fixture_row_002")
    assert candidate["evidence_state"] == "thin"
    assert {row["evidence_state"] for row in evidence_rows} == {"thin"}
    # The persisted row still carries the placeholder the receipt resolved.
    assert {(row["vendor_family"], row["channel_family"]) for row in evidence_rows} == {
        ("reddit", "reddit"),
        ("google_youtube", "youtube"),
    }


def test_an_explicit_row_beside_a_placeholder_row_stays_thin_under_the_explicit_origin_rule():
    _records, result, candidate, evidence_rows = chain(mixed_pair(), V2)
    assert result.state == "thin"
    assert result.reasons == (V2_TAG, "insufficient_independent_support")
    assert candidate["evidence_state"] == "thin"
    assert {row["evidence_state"] for row in evidence_rows} == {"thin"}


def test_two_explicit_independent_members_are_ready_under_the_explicit_origin_rule():
    _records, result, candidate, evidence_rows = chain(explicit_pair(), V2)
    assert result.state == "ready"
    assert result.reasons == (V2_TAG,)
    assert result.independent_pairs == (("fixture_row_001", "fixture_row_002"),)
    assert candidate["evidence_state"] == "ready"
    assert {row["evidence_state"] for row in evidence_rows} == {"ready"}


def test_one_vendor_under_two_family_labels_stays_thin_under_the_explicit_origin_rule():
    _records, result, candidate, _rows = chain(one_vendor_pair(), V2)
    assert result.state == "thin"
    assert result.reasons == (V2_TAG, "insufficient_independent_support")
    assert candidate["evidence_state"] == "thin"


def test_the_legacy_policy_gives_the_retained_outputs_for_the_same_inputs():
    # wave1_family_v1 is the default and reproduces the pre D07 outputs: placeholder
    # identities are admitted as before, and a legacy result carries no policy reason.
    for mapping in (placeholder_pair(), mixed_pair(), explicit_pair()):
        _records, result, candidate, evidence_rows = chain(mapping, V1)
        assert result.state == "ready"
        assert result.reasons == ()
        assert result.independence_policy == V1
        assert result.independent_pairs == (("fixture_row_001", "fixture_row_002"),)
        assert candidate["evidence_state"] == "ready"
        assert {row["evidence_state"] for row in evidence_rows} == {"ready"}
    _records, default_result, _candidate, _rows = chain(placeholder_pair())
    assert default_result.independence_policy == V1
    assert default_result.state == "ready"


def test_the_legacy_policy_pairs_by_family_regardless_of_identity():
    # 613dea2 never saw an identity: one vendor across two families was ready there
    # and stays ready under wave1_family_v1; only explicit_origin_v2 refuses it.
    _records, result, candidate, _rows = chain(one_vendor_pair(), V1)
    assert result.state == "ready"
    assert result.reasons == ()
    assert result.independent_pairs == (("fixture_row_001", "fixture_row_002"),)
    assert candidate["evidence_state"] == "ready"


def test_the_row_builder_refuses_a_readiness_evaluated_under_another_policy():
    records = rows_module._readiness_records(tuple(placeholder_pair().values()), V1)
    legacy = evaluate_readiness(
        records, readiness_rules(), quality_evaluated=True, independence_policy=V1
    )
    with pytest.raises(ValueError, match="readiness result does not match retained receipts"):
        build(receipts=placeholder_pair(), readiness=legacy, independence_policy=V2)
    with pytest.raises(ValueError, match="independence policy"):
        build(receipts=placeholder_pair(), readiness=legacy, independence_policy="v9")


def test_records_without_origin_never_pair_under_the_explicit_origin_rule():
    def record(row_id: str, family: str) -> EvidenceRecord:
        return EvidenceRecord(
            row_id=row_id,
            source_family=family,
            direction="rising",
            published_at=PUBLISHED,
            availability="available",
            geo_confidence=0.91,
        )

    records = [record("row_1", "reddit"), record("row_2", "youtube")]
    result = evaluate_readiness(
        records, readiness_rules(), quality_evaluated=True, independence_policy=V2
    )
    assert result.state == "thin"
    assert result.reasons == (V2_TAG, "insufficient_independent_support")
    legacy = evaluate_readiness(records, readiness_rules(), quality_evaluated=True)
    assert legacy.state == "ready"
    assert legacy.reasons == ()
    assert legacy.independence_policy == V1
