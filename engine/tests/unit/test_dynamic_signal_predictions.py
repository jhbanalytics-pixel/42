"""Boundary tests for deterministic immutable signal predictions."""

from __future__ import annotations

import inspect
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.predictions import (
    PREDICTION_ROW_FIELDS,
    PredictionRules,
    PromotedSignal,
    build_signal_prediction_rows,
)

ROOT = Path(__file__).resolve().parents[2]
SIGNAL_A = "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SIGNAL_B = "sig_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
PREDICTED_AT = datetime(2026, 8, 25, 6, 50, tzinfo=UTC)
FIRST_SEEN_AT = datetime(2026, 8, 22, 11, 0, tzinfo=UTC)


def candidate(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "client_scope_id": "fixture_scope",
        "market_scope": ["za"],
        "brand_config_id": "fixture_brand",
        "audience_lens_ids": [],
        "theme_id": "fixture_theme",
        "run_id": "run_fixture_001",
        "contract_version": "2.0.0",
        "signal_id": SIGNAL_A,
        "signal_date": date(2026, 8, 25),
        "market": "za",
        "cluster_build_version": "hybrid_graph_v1",
        "discovery_mode": "dynamic",
        "velocity_score": 0.68,
        "breadth_score": 0.62,
        "evidence_state": "ready",
    }
    row.update(overrides)
    return row


def promoted(**overrides: object) -> PromotedSignal:
    values: dict[str, object] = {
        "candidate": candidate(),
        "source_families": ("youtube", "reddit"),
        "first_seen_at": FIRST_SEEN_AT,
    }
    values.update(overrides)
    return PromotedSignal(**values)


def build(*signals: PromotedSignal, rules: PredictionRules | None = None):
    return build_signal_prediction_rows(
        promoted_signals=signals,
        predicted_at=PREDICTED_AT,
        rules=rules or PredictionRules(),
    )


def schema_fields(filename: str) -> tuple[str, ...]:
    schema = (ROOT / "infra" / "bigquery_schemas" / filename).read_text(encoding="utf-8")
    column_block = schema.split(")\nPARTITION BY", maxsplit=1)[0]
    return tuple(re.findall(r"^  ([a-z_]+) ", column_block, re.M))


def test_prediction_fields_match_approved_schema() -> None:
    assert schema_fields("signal_predictions_v2.sql") == PREDICTION_ROW_FIELDS


def test_sustained_prediction_has_literal_immutable_id_dates_observed_baseline_and_target() -> None:
    rows = build(promoted())
    assert rows == (
        {
            "client_scope_id": "fixture_scope",
            "market_scope": ["za"],
            "brand_config_id": "fixture_brand",
            "audience_lens_ids": [],
            "theme_id": "fixture_theme",
            "run_id": "run_fixture_001",
            "contract_version": "2.0.0",
            "prediction_id": "pred_4a4e07a1a2128946e0c0ed2c7aeebe299222f17e01a6e08e9a08d2f4dcc5c994",
            "signal_id": "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "signal_date": date(2026, 8, 25),
            "market": "za",
            "discovery_mode": "dynamic",
            "source_families": ["reddit", "youtube"],
            "evidence_state": "ready",
            "first_seen_at": datetime(2026, 8, 22, 11, 0, tzinfo=UTC),
            "predicted_at": datetime(2026, 8, 25, 6, 50, tzinfo=UTC),
            "expected_trajectory": "sustained",
            "evaluation_date": date(2026, 9, 1),
            "baseline": {"velocity": 0.68, "breadth": 0.62, "evidence_family_count": 2},
            "promotion_target": {"velocity": 0.6, "breadth": 0.6, "evidence_family_count": 2},
            "invalidation_condition": "Breadth falls below 0.60 before 2026-09-01.",
            "cluster_build_version": "hybrid_graph_v1",
            "source_family_map_version": "channel_family_v2",
            "rule_version": "prediction_rules_v1",
            "display_eligible": False,
        },
    )


@pytest.mark.parametrize(
    ("velocity", "breadth", "trajectory", "target"),
    [
        (0.8, 0.6, "growing", {"velocity": 0.7, "breadth": 0.6, "evidence_family_count": 2}),
        (0.68, 0.62, "sustained", {"velocity": 0.6, "breadth": 0.6, "evidence_family_count": 2}),
        (0.68, 0.5, "peaked", {"velocity": 0.4, "breadth": 0.45, "evidence_family_count": 2}),
        (0.5, 0.5, "fading", {"velocity": 0.35, "breadth": 0.35, "evidence_family_count": 2}),
    ],
)
def test_rule_based_trajectory_and_target_depend_only_on_observed_baseline(
    velocity: float, breadth: float, trajectory: str, target: dict[str, object]
) -> None:
    row = build(promoted(candidate=candidate(velocity_score=velocity, breadth_score=breadth)))[0]
    assert row["expected_trajectory"] == trajectory
    assert row["baseline"] == {
        "velocity": velocity,
        "breadth": breadth,
        "evidence_family_count": 2,
    }
    assert row["promotion_target"] == target


def test_prediction_is_rerun_immutable_and_id_uses_exact_ordered_inputs() -> None:
    first = build(promoted())
    second = build(promoted())
    changed_run = build(promoted(candidate=candidate(run_id="run_fixture_002")))[0]
    changed_signal = build(promoted(candidate=candidate(signal_id=SIGNAL_B)))[0]
    assert first == second
    assert (
        first[0]["prediction_id"]
        == "pred_4a4e07a1a2128946e0c0ed2c7aeebe299222f17e01a6e08e9a08d2f4dcc5c994"
    )
    assert changed_run["prediction_id"] != first[0]["prediction_id"]
    assert changed_signal["prediction_id"] != first[0]["prediction_id"]
    assert all(len(row["prediction_id"]) == 69 for row in (first[0], changed_run, changed_signal))


def test_prediction_order_and_source_families_are_deterministic() -> None:
    first = promoted()
    second = promoted(candidate=candidate(signal_id=SIGNAL_B), source_families=("search", "news"))
    forward = build(first, second)
    reverse = build(second, first)
    assert forward == reverse
    assert forward[0]["source_families"] == ["reddit", "youtube"]
    assert forward[1]["source_families"] == ["news", "search"]


def test_callers_cannot_override_prediction_display_eligibility() -> None:
    assert "outcome_loop_proven" not in inspect.signature(PredictionRules).parameters
    assert "display_eligible" not in inspect.signature(build_signal_prediction_rows).parameters
    with pytest.raises(TypeError, match="outcome_loop_proven"):
        PredictionRules(outcome_loop_proven=True)


@pytest.mark.parametrize(
    ("velocity", "breadth"), [(0.8, 0.6), (0.68, 0.62), (0.68, 0.5), (0.5, 0.5)]
)
def test_every_new_prediction_is_display_ineligible(velocity: float, breadth: float) -> None:
    row = build(promoted(candidate=candidate(velocity_score=velocity, breadth_score=breadth)))[0]
    assert row["display_eligible"] is False


def test_prediction_rejects_nonready_scope_leak_and_invalid_first_seen_timestamp() -> None:
    with pytest.raises(ValueError, match="ready"):
        build(promoted(candidate=candidate(evidence_state="thin")))
    with pytest.raises(ValueError, match="market scope"):
        build(promoted(candidate=candidate(market="ng")))
    with pytest.raises(ValueError, match="first seen"):
        build(promoted(first_seen_at=datetime(2026, 8, 22, 11, 0)))
    with pytest.raises(ValueError, match="first seen"):
        build(promoted(first_seen_at=datetime(2026, 8, 26, 6, 50, tzinfo=UTC)))
