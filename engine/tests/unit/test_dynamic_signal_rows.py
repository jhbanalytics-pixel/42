"""Boundary tests for exact dynamic signal candidate and evidence rows."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence.graph import SignalComponent
from src.analysis.open_intelligence.readiness import (
    EvidenceRecord,
    ReadinessRules,
    evaluate_readiness,
)
from src.analysis.open_intelligence.rows import (
    EvidenceReceipt,
    ObservedSignalMetrics,
    build_dynamic_signal_rows,
)
from src.contracts.open_intelligence import ResolvedScope, encode_identifier_part

CANDIDATE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "signal_id",
    "signal_date",
    "market",
    "label",
    "cluster_signature",
    "cluster_build_version",
    "model_version",
    "discovery_mode",
    "topic_tags",
    "novelty_score",
    "velocity_score",
    "breadth_score",
    "independence_score",
    "historical_similarity",
    "geo_confidence",
    "evidence_state",
    "created_at",
    "label_member_identity",
)
EVIDENCE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "signal_date",
    "market",
    "signal_id",
    "evidence_id",
    "row_id",
    "source_family",
    "platform",
    "url",
    "published_at",
    "claim_role",
    "direction",
    "geo_confidence",
    "source_label",
    "author_label",
    "excerpt",
    "metric_label",
    "availability",
    "evidence_state",
    "created_at",
    "vendor_family",
    "channel_family",
)
SIGNAL_DATE = date(2026, 8, 25)
CREATED_AT = datetime(2026, 8, 25, 6, 45, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def schema_fields(filename: str) -> tuple[str, ...]:
    schema = (ROOT / "infra" / "bigquery_schemas" / filename).read_text(encoding="utf-8")
    column_block = schema.split(")\nPARTITION BY", maxsplit=1)[0]
    return tuple(re.findall(r"^  ([a-z_]+) ", column_block, re.M))


def identifier(prefix: str, *parts: str | None) -> str:
    canonical = "|".join(encode_identifier_part(value) for value in parts)
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cluster_signature(*members: tuple[str, str, str, str, str]) -> str:
    canonical_members = sorted(
        "|".join(encode_identifier_part(value) for value in member) for member in members
    )
    canonical = "|".join(encode_identifier_part(member) for member in canonical_members)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scope() -> ResolvedScope:
    return ResolvedScope(
        client_scope_id="fixture_scope",
        market_scope=("za",),
        brand_config_id="fixture_brand",
        audience_lens_ids=(),
        theme_id="fixture_theme",
        run_id="run_fixture_001",
        contract_version="2.0.0",
    )


def component() -> SignalComponent:
    return SignalComponent(
        market="za",
        member_identities=("za|keyword|repair routine",),
        terms=("repair routine",),
        row_receipts=("fixture_row_001", "fixture_row_002"),
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        creator_ids=(),
        label="Fixture repair routine",
        label_member_identity="za|keyword|repair routine",
    )


def receipt(
    row_id: str,
    source_family: str,
    platform: str,
    direction: str,
    *,
    url: str | None,
    published_at: datetime,
    factual_conflict: bool = False,
) -> EvidenceReceipt:
    return EvidenceReceipt(
        member_identity="za|keyword|repair routine",
        row_id=row_id,
        source_family=source_family,
        platform=platform,
        url=url,
        published_at=published_at,
        claim_role="direction",
        direction=direction,
        geo_confidence=0.94,
        source_label=f"Fixture {source_family}",
        author_label=f"@fixture_{source_family}",
        excerpt=f"Synthetic {source_family} evidence.",
        metric_label="12 synthetic interactions",
        availability="available",
        factual_conflict=factual_conflict,
    )


def receipts() -> MappingProxyType:
    return MappingProxyType(
        {
            "fixture_row_001": receipt(
                "fixture_row_001",
                "reddit",
                "reddit",
                "rising",
                url="https://evidence.invalid/items/fixture-1",
                published_at=datetime(2026, 8, 24, 19, 20, tzinfo=UTC),
            ),
            "fixture_row_002": receipt(
                "fixture_row_002",
                "youtube",
                "youtube",
                "rising",
                url=None,
                published_at=datetime(2026, 8, 24, 21, tzinfo=UTC),
            ),
        }
    )


def metrics(**overrides: object) -> ObservedSignalMetrics:
    values: dict[str, float | None] = {
        "novelty_score": 0.74,
        "velocity_score": 0.68,
        "breadth_score": 0.62,
        "independence_score": 0.71,
        "historical_similarity": 0.33,
        "geo_confidence": 0.94,
    }
    values.update(overrides)
    return ObservedSignalMetrics(**values)


def readiness_rules() -> ReadinessRules:
    return ReadinessRules(
        current_cutoff=datetime(2026, 8, 24, tzinfo=UTC), minimum_geo_confidence=0.8
    )


def readiness(
    receipt_mapping: MappingProxyType | None = None, *, quality_evaluated: bool = True
) -> object:
    evidence = [
        EvidenceRecord(
            row_id=item.row_id,
            source_family=item.source_family,
            direction=item.direction,
            published_at=item.published_at,
            availability=item.availability,
            geo_confidence=item.geo_confidence,
            factual_conflict=item.factual_conflict,
        )
        for item in (receipt_mapping or receipts()).values()
    ]
    return evaluate_readiness(
        evidence,
        readiness_rules(),
        quality_evaluated=quality_evaluated,
    )


def build(**overrides: object) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    values: dict[str, object] = {
        "component": component(),
        "scope": scope(),
        "signal_date": SIGNAL_DATE,
        "created_at": CREATED_AT,
        "metrics": metrics(),
        "receipts": receipts(),
        "readiness": readiness(),
        "readiness_rules": readiness_rules(),
        "quality_evaluated": True,
        "topic_tags": ("economy",),
    }
    values.update(overrides)
    return build_dynamic_signal_rows(**values)


def test_rows_have_exact_schema_field_order_full_ids_and_canonical_values() -> None:
    candidate, evidence_rows = build()
    expected_signature = cluster_signature(
        ("keyword", "repair routine", "reddit", "reddit", "fixture_row_001"),
        ("keyword", "repair routine", "youtube", "youtube", "fixture_row_002"),
    )
    expected_signal_id = identifier("sig_", "fixture_scope", "za", expected_signature)
    expected_evidence_ids = {
        "fixture_row_001": identifier(
            "ev_",
            "fixture_scope",
            "za",
            expected_signal_id,
            "reddit",
            "reddit",
            "fixture_row_001",
            "https://evidence.invalid/items/fixture-1",
        ),
        "fixture_row_002": identifier(
            "ev_",
            "fixture_scope",
            "za",
            expected_signal_id,
            "youtube",
            "youtube",
            "fixture_row_002",
            None,
        ),
    }

    assert tuple(candidate) == CANDIDATE_FIELDS
    assert [tuple(row) for row in evidence_rows] == [EVIDENCE_FIELDS, EVIDENCE_FIELDS]
    assert candidate["cluster_signature"] == expected_signature
    assert candidate["signal_id"] == expected_signal_id
    assert candidate["signal_id"].startswith("sig_")
    assert len(candidate["signal_id"]) == 68
    assert candidate["cluster_signature"].startswith("sha256:")
    assert len(candidate["cluster_signature"]) == 71
    assert candidate["market_scope"] == ["za"]
    assert candidate["audience_lens_ids"] == []
    assert candidate["topic_tags"] == ["economy"]
    assert candidate["model_version"] is None
    assert candidate["discovery_mode"] == "dynamic"
    assert candidate["evidence_state"] == "ready"
    assert candidate["created_at"] == CREATED_AT
    assert candidate["signal_date"] == SIGNAL_DATE
    assert [row["row_id"] for row in evidence_rows] == ["fixture_row_001", "fixture_row_002"]
    assert {row["row_id"]: row["evidence_id"] for row in evidence_rows} == expected_evidence_ids
    assert all(row["signal_id"] == expected_signal_id for row in evidence_rows)
    assert all(
        row["evidence_id"].startswith("ev_") and len(row["evidence_id"]) == 67
        for row in evidence_rows
    )
    assert evidence_rows[1]["url"] is None
    assert all(row["evidence_state"] == "ready" for row in evidence_rows)


def test_evidence_rows_persist_vendor_channel_and_compatibility_alias() -> None:
    _candidate, evidence_rows = build()
    assert {
        (row["vendor_family"], row["channel_family"], row["source_family"]) for row in evidence_rows
    } == {("reddit", "reddit", "reddit"), ("google_youtube", "youtube", "youtube")}


def test_row_field_lists_match_the_approved_bigquery_schemas() -> None:
    assert schema_fields("signal_candidates_v2.sql") == CANDIDATE_FIELDS
    assert schema_fields("signal_evidence_v2.sql") == EVIDENCE_FIELDS


def test_rows_are_deterministic_for_mapping_and_component_input_order() -> None:
    forward_candidate, forward_evidence = build()
    reverse_component = SignalComponent(
        market="za",
        member_identities=("za|keyword|repair routine",),
        terms=("repair routine",),
        row_receipts=("fixture_row_002", "fixture_row_001"),
        source_families=("youtube", "reddit"),
        platforms=("youtube", "reddit"),
        creator_ids=(),
        label="Fixture repair routine",
        label_member_identity="za|keyword|repair routine",
    )
    reverse_receipts = MappingProxyType(dict(reversed(tuple(receipts().items()))))
    reverse_candidate, reverse_evidence = build(
        component=reverse_component,
        receipts=reverse_receipts,
        topic_tags=("economy",),
    )
    assert (forward_candidate, forward_evidence) == (reverse_candidate, reverse_evidence)


def test_one_row_cited_by_two_signals_in_a_market_is_two_evidence_rows() -> None:
    # Seen on staging 4 Sep 2026 once SocialCrawl seed rows qualified as evidence:
    # one sampled post sat in two candidate clusters of the same market, and the
    # review packet refused the batch for a duplicate evidence identity. Evidence
    # belongs to the signal that cites it, so the identity carries the signal.
    _, first_evidence = build()
    other_component = SignalComponent(
        market="za",
        member_identities=("za|keyword|repair kit",),
        terms=("repair kit",),
        row_receipts=("fixture_row_001", "fixture_row_002"),
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        creator_ids=(),
        label="Fixture repair kit",
        label_member_identity="za|keyword|repair kit",
    )
    other_receipts = MappingProxyType(
        {
            row_id: replace(item, member_identity="za|keyword|repair kit")
            for row_id, item in receipts().items()
        }
    )
    other_candidate, other_evidence = build(component=other_component, receipts=other_receipts)
    assert other_candidate["signal_id"] != first_evidence[0]["signal_id"]
    assert [row["row_id"] for row in other_evidence] == [row["row_id"] for row in first_evidence]
    assert {row["evidence_id"] for row in other_evidence}.isdisjoint(
        {row["evidence_id"] for row in first_evidence}
    )
    assert all(row["signal_id"] == other_candidate["signal_id"] for row in other_evidence)


def test_candidate_binds_the_exact_label_member_identity():
    candidate, _evidence = build()
    assert candidate["label"] == "Fixture repair routine"
    assert candidate["label_member_identity"] == "za|keyword|repair routine"
    # Added to live tables by an additive ALTER, so the field closes the row like the schema file.
    assert tuple(candidate)[-1] == "label_member_identity"


def test_rows_reject_missing_receipts_and_positional_receipt_arrays() -> None:
    with pytest.raises(ValueError, match="receipt mapping"):
        build(receipts=tuple(receipts().values()))
    with pytest.raises(ValueError, match="missing receipt"):
        build(receipts=MappingProxyType({"fixture_row_001": receipts()["fixture_row_001"]}))
    mismatched = dict(receipts())
    mismatched["wrong_key"] = mismatched.pop("fixture_row_002")
    with pytest.raises(ValueError, match="receipt key"):
        build(receipts=MappingProxyType(mismatched))


def test_rows_recompute_exact_receipts_and_reject_forged_ready_state() -> None:
    stale = dict(receipts())
    stale["fixture_row_001"] = replace(
        stale["fixture_row_001"], published_at=datetime(2026, 8, 23, tzinfo=UTC)
    )
    stale["fixture_row_002"] = replace(
        stale["fixture_row_002"], published_at=datetime(2026, 8, 23, tzinfo=UTC)
    )
    forged = readiness()
    with pytest.raises(ValueError, match="readiness result does not match retained receipts"):
        build(receipts=MappingProxyType(stale), readiness=forged)


def test_rows_preserve_a_valid_signal_level_factual_conflict() -> None:
    contradictory = evaluate_readiness(
        [], readiness_rules(), quality_evaluated=True, factual_conflict=True
    )
    candidate, evidence_rows = build(readiness=contradictory, factual_conflict=True)
    assert candidate["evidence_state"] == "contradictory"
    assert {row["evidence_state"] for row in evidence_rows} == {"contradictory"}
    with pytest.raises(ValueError, match="factual conflict"):
        build(factual_conflict=1)


def test_equivalent_normalized_urls_produce_one_stable_evidence_id() -> None:
    canonical = build()[1][0]["evidence_id"]
    equivalent = dict(receipts())
    equivalent["fixture_row_001"] = replace(
        equivalent["fixture_row_001"],
        url="HTTPS://EVIDENCE.INVALID:443/items/fixture-1#ignored-fragment",
    )
    normalized_candidate, normalized_rows = build(receipts=MappingProxyType(equivalent))
    assert normalized_candidate["evidence_state"] == "ready"
    assert normalized_rows[0]["url"] == "https://evidence.invalid/items/fixture-1"
    assert normalized_rows[0]["evidence_id"] == canonical


@pytest.mark.parametrize(
    ("raw_url", "normalized_url"),
    [
        (
            "HTTPS://[2001:DB8::1]:443/items/fixture-1#ignored-fragment",
            "https://[2001:db8::1]/items/fixture-1",
        ),
        (
            "https://[2001:DB8::1]:8443/items/fixture-1#ignored-fragment",
            "https://[2001:db8::1]:8443/items/fixture-1",
        ),
    ],
)
def test_ipv6_urls_keep_brackets_and_hash_the_normalized_form(
    raw_url: str, normalized_url: str
) -> None:
    ipv6 = dict(receipts())
    ipv6["fixture_row_001"] = replace(ipv6["fixture_row_001"], url=raw_url)
    _, evidence_rows = build(receipts=MappingProxyType(ipv6))
    expected_id = identifier(
        "ev_",
        "fixture_scope",
        "za",
        evidence_rows[0]["signal_id"],
        "reddit",
        "reddit",
        "fixture_row_001",
        normalized_url,
    )
    assert evidence_rows[0]["url"] == normalized_url
    assert evidence_rows[0]["evidence_id"] == expected_id


@pytest.mark.parametrize(
    "field",
    [
        "novelty_score",
        "velocity_score",
        "breadth_score",
        "independence_score",
        "geo_confidence",
    ],
)
def test_rows_reject_observed_metric_score_overflow(field: str) -> None:
    with pytest.raises(ValueError, match=field.replace("_", " ")):
        build(metrics=metrics(**{field: 1.01}))


def test_rows_copy_only_observed_metrics_and_reject_scope_or_market_mismatch() -> None:
    candidate, _ = build(metrics=metrics(historical_similarity=None))
    assert candidate["novelty_score"] == 0.74
    assert candidate["velocity_score"] == 0.68
    assert candidate["breadth_score"] == 0.62
    assert candidate["independence_score"] == 0.71
    assert candidate["historical_similarity"] is None
    assert candidate["geo_confidence"] == 0.94

    bad_component = SignalComponent(
        market="ng",
        member_identities=("ng|keyword|repair routine",),
        terms=("repair routine",),
        row_receipts=("fixture_row_001", "fixture_row_002"),
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        creator_ids=(),
        label="Fixture repair routine",
        label_member_identity="ng|keyword|repair routine",
    )
    with pytest.raises(ValueError, match="market scope"):
        build(component=bad_component)


def test_rows_preserve_all_four_readiness_states_without_dropping_receipts() -> None:
    thin = dict(receipts())
    thin["fixture_row_002"] = replace(thin["fixture_row_002"], availability="unavailable")
    contradictory = dict(receipts())
    contradictory["fixture_row_002"] = replace(
        contradictory["fixture_row_002"], direction="declining"
    )
    cases = (
        ("ready", receipts(), True),
        ("thin", MappingProxyType(thin), True),
        ("contradictory", MappingProxyType(contradictory), True),
        ("unchecked", receipts(), False),
    )
    for state, receipt_mapping, quality_evaluated in cases:
        result = readiness(receipt_mapping, quality_evaluated=quality_evaluated)
        candidate, evidence_rows = build(
            receipts=receipt_mapping,
            readiness=result,
            quality_evaluated=quality_evaluated,
        )
        assert candidate["evidence_state"] == state
        assert {row["evidence_state"] for row in evidence_rows} == {state}


def test_rows_reject_receipt_that_is_not_a_component_member_or_direction_contract() -> None:
    bad_member = dict(receipts())
    bad_member["fixture_row_001"] = replace(
        bad_member["fixture_row_001"], member_identity="za|keyword|other"
    )
    with pytest.raises(ValueError, match="member identity"):
        build(receipts=MappingProxyType(bad_member))

    bad_direction = dict(receipts())
    bad_direction["fixture_row_001"] = replace(
        bad_direction["fixture_row_001"], direction="declining"
    )
    with pytest.raises(ValueError, match="readiness result"):
        build(receipts=MappingProxyType(bad_direction))
