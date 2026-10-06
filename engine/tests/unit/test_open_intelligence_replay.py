"""Point-in-time replay metric and leakage-gate tests."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock

import pytest
from scripts.staging import replay_open_intelligence as replay
from scripts.staging.replay_open_intelligence import (
    EXPECTED_EVENT_LEDGER_ROWS,
    FutureLeakageDetected,
    IncompleteReplayInput,
    KnownEvent,
    ReplaySignal,
    evaluate_replay,
    render_dry_run,
)
from src.analysis.open_intelligence import execution_generations
from src.analysis.open_intelligence import pipeline as dynamic_pipeline
from src.analysis.open_intelligence.graph import SignalComponent
from src.analysis.open_intelligence.pipeline import run_dynamic_signal_identity

from tests.unit import test_dynamic_signal_pipeline as task5c

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay_known_events.json"
)
BUNDLE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay"
    / "replay_input_complete.json"
)
ADAPTER_SNAPSHOT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay"
    / "task5c_adapter_source_snapshot.json"
)
SEMANTIC_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay"
    / "semantic_candidates_v1.json"
)
METRIC_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay"
    / "metric_formulas_v1.json"
)
COVERAGE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay"
    / "geo_direction_coverage_v1.json"
)
GRID_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay"
    / "graph_readiness_grid_v1.json"
)
LINEAGE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay"
    / "lineage_reconstruction_v1.json"
)
TRAJECTORY_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "replay"
    / "trajectory_candidates_v1.json"
)
EXPECTED_BUNDLE_DIGEST = "6f506f9d0323018a8e9acf72a986cff883aadfd514aff7977f805f686397c4c9"
# Re-pinned when ReplayInputBundle gained the optional
# scoring_sources_by_candidate field. This digest covers the bundle object, so
# it moves when the bundle's shape does. EXPECTED_BUNDLE_DIGEST covers the raw
# payload and moves only when approved fixture bytes change.
EXPECTED_ADAPTER_DIGEST = "e574886650a9aeb5d96dbce804719349d4f16280b5a27f76aae04e97c181820c"
CUTOFF = date(2026, 8, 25)


def signal(
    signal_id: str,
    market: str,
    signature: str,
    *members: str,
    source_time: str = "2026-08-25T12:00:00Z",
    membership_complete: bool = True,
    evidence_ready: bool = True,
    geo_proven: bool = True,
) -> ReplaySignal:
    return ReplaySignal(
        signal_id=signal_id,
        market=market,
        cluster_signature=signature,
        member_identities=tuple(members),
        source_max_observed_at=datetime.fromisoformat(source_time.replace("Z", "+00:00")),
        membership_complete=membership_complete,
        evidence_ready=evidence_ready,
        geo_proven=geo_proven,
    )


def known_events() -> tuple[KnownEvent, ...]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert raw["fixture_scope"] == "synthetic_test_only"
    assert raw["expected_event_ledger_rows"] == EXPECTED_EVENT_LEDGER_ROWS
    return tuple(KnownEvent(**row) for row in raw["known_events"])


def bundle_payload() -> dict:
    return json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))


def semantic_payload() -> dict:
    return json.loads(SEMANTIC_FIXTURE.read_text(encoding="utf-8"))


def metric_payload() -> dict:
    return json.loads(METRIC_FIXTURE.read_text(encoding="utf-8"))


def coverage_payload() -> dict:
    return json.loads(COVERAGE_FIXTURE.read_text(encoding="utf-8"))


def grid_payload() -> dict:
    return json.loads(GRID_FIXTURE.read_text(encoding="utf-8"))


def lineage_payload() -> dict:
    return json.loads(LINEAGE_FIXTURE.read_text(encoding="utf-8"))


def trajectory_payload() -> dict:
    return json.loads(TRAJECTORY_FIXTURE.read_text(encoding="utf-8"))


def evaluation_artifact(**overrides):
    inputs = {
        "replay_input": bundle_payload(),
        "semantic_fixture": semantic_payload(),
        "metric_fixture": metric_payload(),
        "coverage_fixture": coverage_payload(),
        "grid_fixture": grid_payload(),
        "lineage_fixture": lineage_payload(),
        "trajectory_fixture": trajectory_payload(),
    }
    return replay.build_replay_evaluation_artifact(**{**inputs, **overrides})


def evaluation_payload(artifact) -> dict:
    return {field: getattr(artifact, field) for field in replay.REPLAY_EVALUATION_FIELDS}


def mutable_tree(value):
    if isinstance(value, (dict, MappingProxyType)):
        return {key: mutable_tree(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(mutable_tree(item) for item in value)
    return value


def test_replay_evaluation_artifact_is_exact_complete_and_fixture_only():
    artifact = evaluation_artifact()

    assert tuple(replay.ReplayEvaluationArtifact.__dataclass_fields__) == (
        "artifact_version",
        "contract_version",
        "mode",
        "certified",
        "cutoff",
        "source_snapshot",
        "known_event_set",
        "provider_comparisons",
        "metric_comparisons",
        "geo_comparisons",
        "graph_rule_comparisons",
        "readiness_rule_comparisons",
        "trajectory_rule_comparisons",
        "replay_metrics",
        "review_sample_ids",
        "human_coherence_status",
        "recommendation",
        "missing_work",
        "artifact_digest",
    )
    assert artifact.mode == "fixture_dry_run"
    assert artifact.certified is False
    assert artifact.human_coherence_status == "pending"
    assert artifact.recommendation is None
    assert artifact.artifact_digest == replay.replay_evaluation_artifact_digest(artifact)
    assert replay.validate_replay_evaluation_artifact(evaluation_payload(artifact)) == artifact


def test_replay_evaluation_artifact_is_byte_identical_under_input_reversal():
    semantic = semantic_payload()
    semantic["observations"].reverse()
    metric = metric_payload()
    metric["evidence_rows"].reverse()
    coverage = coverage_payload()
    coverage["rows"].reverse()
    grid = grid_payload()
    grid["observations"].reverse()
    grid["component_reviews"].reverse()
    grid["readiness_cases"].reverse()
    lineage = lineage_payload()
    lineage["candidate_rows"].reverse()
    lineage["membership_rows"].reverse()
    trajectory = trajectory_payload()
    trajectory["metric_rows"].reverse()

    forward = evaluation_artifact()
    reverse = evaluation_artifact(
        semantic_fixture=semantic,
        metric_fixture=metric,
        coverage_fixture=coverage,
        grid_fixture=grid,
        lineage_fixture=lineage,
        trajectory_fixture=trajectory,
    )
    assert replay.canonical_typed_json(forward) == replay.canonical_typed_json(reverse)
    assert forward.artifact_digest == reverse.artifact_digest


def test_replay_evaluation_artifact_rejects_field_family_and_state_mutations():
    artifact = evaluation_artifact()
    payload = evaluation_payload(artifact)
    reordered = {key: payload[key] for key in reversed(tuple(payload))}
    with pytest.raises(IncompleteReplayInput, match="artifact field order"):
        replay.validate_replay_evaluation_artifact(reordered)

    omitted = dict(payload)
    omitted.pop("metric_comparisons")
    with pytest.raises(IncompleteReplayInput, match="artifact field order"):
        replay.validate_replay_evaluation_artifact(omitted)

    for field, value in (("certified", True), ("recommendation", "winner")):
        mutated = dict(payload)
        mutated[field] = value
        mutated["artifact_digest"] = replay.replay_evaluation_artifact_digest(mutated)
        with pytest.raises(IncompleteReplayInput, match="fixture artifact state"):
            replay.validate_replay_evaluation_artifact(mutated)


def test_replay_evaluation_digest_excludes_only_itself_and_missing_work_is_exact():
    artifact = evaluation_artifact()
    payload = evaluation_payload(artifact)
    original = artifact.artifact_digest
    payload["artifact_digest"] = "0" * 64
    assert replay.replay_evaluation_artifact_digest(payload) == original
    payload["human_coherence_status"] = "changed"
    assert replay.replay_evaluation_artifact_digest(payload) != original

    payload = evaluation_payload(artifact)
    payload["missing_work"] = ()
    payload["artifact_digest"] = replay.replay_evaluation_artifact_digest(payload)
    with pytest.raises(IncompleteReplayInput, match="artifact missing work"):
        replay.validate_replay_evaluation_artifact(payload)


def test_replay_evaluation_keeps_full_denominators_separate_from_display_sample():
    artifact = evaluation_artifact()
    assert len(artifact.review_sample_ids) == 3
    for result in artifact.replay_metrics["grid_results"]:
        assert result.known_event_recall.denominator == 3
        assert result.duplicate_clusters.denominator == 5
        assert result.foreign_leakage.denominator == 5
        assert result.ready.denominator == 7

    first = artifact.replay_metrics["grid_results"][0]
    sampled = replace(
        first,
        duplicate_clusters=replay.Metric(1, 3, 1 / 3),
        foreign_leakage=replay.Metric(1, 3, 1 / 3),
    )
    mutated_metrics = {
        **artifact.replay_metrics,
        "grid_results": (sampled, *artifact.replay_metrics["grid_results"][1:]),
    }
    payload = evaluation_payload(artifact)
    payload["replay_metrics"] = mutated_metrics
    payload["artifact_digest"] = replay.replay_evaluation_artifact_digest(payload)
    with pytest.raises(IncompleteReplayInput, match="full replay denominator"):
        replay.validate_replay_evaluation_artifact(payload)


def test_replay_evaluation_rejects_future_evidence_and_unpinned_source_snapshot():
    coverage = coverage_payload()
    coverage["rows"][0]["current_observed_at"] = "2026-08-26T00:00:00Z"
    refresh_coverage_digest(coverage)
    with pytest.raises(FutureLeakageDetected, match="future coverage timestamp"):
        evaluation_artifact(coverage_fixture=coverage)

    replay_input = bundle_payload()
    replay_input["source_snapshot"]["captured_at"] = "2026-08-25T22:59:59Z"
    snapshot = replay_input["source_snapshot"]
    without_digest = dict(snapshot)
    without_digest.pop("source_digest")
    snapshot["source_digest"] = replay._digest(without_digest)
    with pytest.raises(IncompleteReplayInput, match="approved fixture source snapshot"):
        evaluation_artifact(replay_input=replay_input)


def test_replay_evaluation_direct_mutation_rejects_cutoff_drift():
    payload = evaluation_payload(evaluation_artifact())
    payload["cutoff"] = date(2026, 8, 24)
    payload["artifact_digest"] = replay.replay_evaluation_artifact_digest(payload)

    with pytest.raises(IncompleteReplayInput, match="approved artifact cutoff"):
        replay.validate_replay_evaluation_artifact(payload)


def test_replay_evaluation_direct_mutation_revalidates_nested_source_snapshot():
    payload = evaluation_payload(evaluation_artifact())
    source_root = mutable_tree(payload["source_snapshot"])
    source_root["replay_input_source_snapshot"]["row_counts_by_table"]["event_ledger"] += 1
    payload["source_snapshot"] = source_root
    payload["artifact_digest"] = replay.replay_evaluation_artifact_digest(payload)

    with pytest.raises(IncompleteReplayInput, match="nested source snapshot"):
        replay.validate_replay_evaluation_artifact(payload)


def test_replay_evaluation_direct_mutation_revalidates_known_event_content():
    payload = evaluation_payload(evaluation_artifact())
    known = mutable_tree(payload["known_event_set"])
    known["events"][0]["member_identities"] = ("ke|entity|mutated",)
    payload["known_event_set"] = known
    payload["artifact_digest"] = replay.replay_evaluation_artifact_digest(payload)

    with pytest.raises(IncompleteReplayInput, match="known event content"):
        replay.validate_replay_evaluation_artifact(payload)


def _mutate_provider_comparison(payload):
    comparison = payload["provider_comparisons"]
    candidate = replace(comparison.candidates[0], pair_count=99)
    payload["provider_comparisons"] = replace(
        comparison, candidates=(candidate, *comparison.candidates[1:])
    )


def _mutate_metric_comparison(payload):
    comparison = payload["metric_comparisons"]
    result = comparison.results[0]
    changed = replace(
        result, numerator=result.numerator - 1, value=(result.numerator - 1) / result.denominator
    )
    payload["metric_comparisons"] = replace(comparison, results=(changed, *comparison.results[1:]))


def _mutate_geo_comparison(payload):
    comparison = payload["geo_comparisons"]
    payload["geo_comparisons"] = replace(
        comparison, explicit_geo_evidence_rows=replay.CoverageRecord(0, ())
    )


def _changed_grid(comparison):
    result = comparison.results[0]
    changed = replace(result, foreign_leakage=replay.Metric(0, 5, 0.0))
    return replace(comparison, results=(changed, *comparison.results[1:]))


def _mutate_graph_and_replay_metrics(payload):
    changed = _changed_grid(payload["graph_rule_comparisons"])
    payload["graph_rule_comparisons"] = changed
    payload["readiness_rule_comparisons"] = changed
    metrics = dict(payload["replay_metrics"])
    metrics["grid_results"] = changed.results
    payload["replay_metrics"] = metrics


def _mutate_readiness_comparison(payload):
    payload["readiness_rule_comparisons"] = _changed_grid(payload["readiness_rule_comparisons"])


def _mutate_trajectory_comparison(payload):
    comparison = payload["trajectory_rule_comparisons"]
    result = comparison.results[0]
    changed = replace(result, matched=replay.Metric(3, 5, 0.6))
    payload["trajectory_rule_comparisons"] = replace(
        comparison, results=(changed, *comparison.results[1:])
    )


def _mutate_lineage_replay_metric(payload):
    metrics = dict(payload["replay_metrics"])
    metrics["lineage_evaluation"] = replace(
        metrics["lineage_evaluation"], missing_work=("lineage_membership_unavailable",)
    )
    payload["replay_metrics"] = metrics


@pytest.mark.parametrize(
    "mutation",
    [
        _mutate_provider_comparison,
        _mutate_metric_comparison,
        _mutate_geo_comparison,
        _mutate_graph_and_replay_metrics,
        _mutate_readiness_comparison,
        _mutate_trajectory_comparison,
        _mutate_lineage_replay_metric,
    ],
)
def test_replay_evaluation_direct_mutation_rejects_changed_constituent_output(mutation):
    payload = evaluation_payload(evaluation_artifact())
    mutation(payload)
    payload["artifact_digest"] = replay.replay_evaluation_artifact_digest(payload)

    with pytest.raises(IncompleteReplayInput, match="pinned constituent comparison"):
        replay.validate_replay_evaluation_artifact(payload)


def test_trajectory_candidates_are_exact_ordered_and_never_selected():
    payload = trajectory_payload()
    artifact = replay.compare_replay_trajectory_candidates(payload)

    assert tuple(replay.TrajectoryRule.__dataclass_fields__) == (
        "trajectory_name",
        "minimum_velocity",
        "minimum_breadth",
        "target_velocity",
        "target_breadth",
        "invalidation_metric",
        "invalidation_threshold",
    )
    assert tuple(replay.ReplayTrajectoryCandidateResult.__dataclass_fields__) == (
        "candidate_id",
        "version",
        "rules_digest",
        "evaluations",
        "matched",
        "missing",
        "missing_work",
        "status",
        "display_eligible",
    )
    assert len(artifact.results) == 2
    assert artifact.recommendation is None
    assert artifact.certified_rule is None
    assert artifact == replay.compare_replay_trajectory_candidates(payload)


def test_trajectory_baseline_matches_frozen_prediction_behavior_with_full_denominator():
    artifact = replay.compare_replay_trajectory_candidates(trajectory_payload())
    baseline = next(
        item for item in artifact.results if item.candidate_id == "baseline_predictions_v1"
    )
    matched = {item.metric_id: item.matched_trajectory for item in baseline.evaluations}
    assert matched == {
        "metric_fading": "fading",
        "metric_growing": "growing",
        "metric_missing": None,
        "metric_peaked": "peaked",
        "metric_sustained": "sustained",
    }
    assert baseline.matched == replay.Metric(4, 5, 0.8)
    assert baseline.missing == replay.Metric(1, 5, 0.2)
    assert baseline.missing_work == ("velocity_score_unavailable",)
    assert baseline.status == "provisional"
    assert baseline.display_eligible is False
    assert all(
        item.invalidation_at == "2026-09-08"
        for item in baseline.evaluations
        if item.matched_trajectory
    )


def test_trajectory_candidate_rejects_order_duplicate_hidden_and_leakage_fields():
    payload = trajectory_payload()
    payload["candidates"][1]["rules"][0], payload["candidates"][1]["rules"][1] = (
        payload["candidates"][1]["rules"][1],
        payload["candidates"][1]["rules"][0],
    )
    payload["candidates"][1]["rules_digest"] = replay._digest(
        tuple(payload["candidates"][1]["rules"])
    )
    payload["fixture_digest"] = replay.trajectory_fixture_digest(payload)
    with pytest.raises(IncompleteReplayInput, match="trajectory rule order"):
        replay.compare_replay_trajectory_candidates(payload)

    payload = trajectory_payload()
    payload["candidates"][1]["rules"][1]["trajectory_name"] = "growing"
    payload["candidates"][1]["rules_digest"] = replay._digest(
        tuple(payload["candidates"][1]["rules"])
    )
    payload["fixture_digest"] = replay.trajectory_fixture_digest(payload)
    with pytest.raises(IncompleteReplayInput, match="duplicate trajectory"):
        replay.compare_replay_trajectory_candidates(payload)

    payload = trajectory_payload()
    payload["candidates"][0]["rules"][0]["minimum_velocity"] = 0.76
    payload["candidates"][0]["rules_digest"] = replay._digest(
        tuple(payload["candidates"][0]["rules"])
    )
    payload["fixture_digest"] = replay.trajectory_fixture_digest(payload)
    with pytest.raises(IncompleteReplayInput, match="approved baseline metadata"):
        replay.compare_replay_trajectory_candidates(payload)

    payload = trajectory_payload()
    payload["candidates"][1]["recurrence_score"] = 0.9
    payload["fixture_digest"] = replay.trajectory_fixture_digest(payload)
    with pytest.raises(IncompleteReplayInput, match="trajectory candidate fields"):
        replay.compare_replay_trajectory_candidates(payload)


def test_trajectory_missing_metrics_never_become_zero_or_certified():
    artifact = replay.compare_replay_trajectory_candidates(trajectory_payload())
    for result in artifact.results:
        missing = next(item for item in result.evaluations if item.metric_id == "metric_missing")
        assert missing.matched_trajectory is None
        assert missing.target_velocity is None
        assert missing.target_breadth is None
        assert missing.invalidation_metric is None
        assert missing.invalidation_threshold is None
        assert missing.missing_work == ("velocity_score_unavailable",)
        assert result.display_eligible is False
    assert artifact.recommendation is None
    assert artifact.certified_rule is None


def test_predictions_baseline_replay_horizon_is_content_pinned():
    payload = trajectory_payload()
    baseline = next(
        item for item in payload["candidates"] if item["source"] == "predictions_py_baseline"
    )
    baseline["evaluation_days"] = 99
    payload["fixture_digest"] = replay.trajectory_fixture_digest(payload)

    with pytest.raises(IncompleteReplayInput, match="approved baseline metadata"):
        replay.compare_replay_trajectory_candidates(payload)


def test_predictions_module_is_byte_stable_for_fixture_comparison():
    """Pin the prediction module bytes that the replay fixtures were compared against.

    The pin moved once, when the learning kernels added first_evaluation_day to the
    module (integration commit a025c9a). The prediction rows the fixtures compare were
    captured before and after that change and hashed equal (l-kernels-prediction-rows
    before and after receipts, 8252 bytes, digest 264c3588), so the fixture comparison
    the pin protects still holds and the digest below is the module as integrated.

    The pin moved a second time when L02 added the v2 issue producer
    (build_signal_prediction_rows_v2) beside the untouched v1 producer. The same
    rows were captured again after that change and hashed equal (8252 bytes, digest
    264c3588, pinned by test_v2_rows_leave_the_retained_v1_fixture_bytes_untouched in
    test_prediction_time_v2.py), so the protected comparison still holds."""
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "analysis"
        / "open_intelligence"
        / "predictions.py"
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "c49c98ac002b22a30ddf79730d524c7da73a8bacf3f3c60bdc9df45cd078b219"
    )


def test_lineage_fixture_reconstructs_complete_snapshots_and_relations():
    payload = lineage_payload()
    artifact = replay.evaluate_replay_lineage_fixture(payload)

    assert tuple(replay.ReplayLineageArtifact.__dataclass_fields__) == (
        "prior_snapshots",
        "current_snapshots",
        "lineage_rows",
        "missing_work",
        "status",
        "recommendation",
    )
    assert len(artifact.prior_snapshots) == 4
    assert len(artifact.current_snapshots) == 4
    assert len(artifact.lineage_rows) == 5
    assert [row["relation"] for row in artifact.lineage_rows].count("continues") == 1
    assert [row["relation"] for row in artifact.lineage_rows].count("merges_into") == 2
    assert [row["relation"] for row in artifact.lineage_rows].count("splits_into") == 2
    assert artifact.missing_work == ()
    assert artifact.status == "provisional"
    assert artifact.recommendation is None
    prior_za = next(item for item in artifact.prior_snapshots if item.component.market == "za")
    assert prior_za.component.member_identities == (
        "za|keyword|alpha",
        "za|keyword|beta",
    )
    assert artifact == replay.evaluate_replay_lineage_fixture(
        {
            **payload,
            "candidate_rows": list(reversed(payload["candidate_rows"])),
            "membership_rows": list(reversed(payload["membership_rows"])),
        }
    )


def _mutate_lineage_rows(payload, signal_id, updates):
    for collection in ("candidate_rows", "membership_rows"):
        for row in payload[collection]:
            if row["signal_id"] == signal_id:
                row.update(updates)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["membership_rows"].pop(1),
        lambda payload: payload["membership_rows"].append(
            copy.deepcopy(payload["membership_rows"][0])
        ),
        lambda payload: payload["membership_rows"].append(
            {
                **copy.deepcopy(payload["membership_rows"][0]),
                "member_id": "conflict",
                "canonical_value": "changed",
            }
        ),
        lambda payload: _mutate_lineage_rows(
            payload,
            "sig_1111111111111111111111111111111111111111111111111111111111111111",
            {"signal_date": "2026-08-25"},
        ),
        lambda payload: _mutate_lineage_rows(
            payload,
            "sig_1111111111111111111111111111111111111111111111111111111111111111",
            {"client_scope_id": "foreign_scope"},
        ),
        lambda payload: payload["membership_rows"][0].__setitem__("retention_until", "2026-08-24"),
        lambda payload: _mutate_lineage_rows(
            payload,
            "sig_1111111111111111111111111111111111111111111111111111111111111111",
            {"contract_version": "3.0.0"},
        ),
        lambda payload: _mutate_lineage_rows(
            payload,
            "sig_1111111111111111111111111111111111111111111111111111111111111111",
            {"rule_version": "foreign_rules"},
        ),
        lambda payload: _mutate_lineage_rows(
            payload,
            "sig_1111111111111111111111111111111111111111111111111111111111111111",
            {"signal_date": "2025-08-24"},
        ),
        lambda payload: payload.__setitem__(
            "membership_rows",
            [
                row
                for row in payload["membership_rows"]
                if row["signal_id"]
                != "sig_1111111111111111111111111111111111111111111111111111111111111111"
            ],
        ),
    ],
)
def test_lineage_loader_fails_closed_for_invalid_prior_membership(mutation):
    payload = lineage_payload()
    mutation(payload)
    payload["fixture_digest"] = replay.lineage_fixture_digest(payload)

    artifact = replay.evaluate_replay_lineage_fixture(payload)
    assert artifact.prior_snapshots == ()
    assert artifact.current_snapshots == ()
    assert artifact.lineage_rows == ()
    assert artifact.missing_work == ("lineage_membership_unavailable",)
    assert artifact.status == "provisional"
    assert artifact.recommendation is None


def test_lineage_fixture_rejects_ambiguous_many_to_many():
    payload = lineage_payload()
    p2 = "sig_2222222222222222222222222222222222222222222222222222222222222222"
    p3 = "sig_3333333333333333333333333333333333333333333333333333333333333333"
    c1 = "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    for candidate in payload["candidate_rows"]:
        if candidate["signal_id"] in {p2, p3}:
            candidate["member_count"] = 2
        if candidate["signal_id"] == c1:
            candidate["market"] = "ng"
    for membership in payload["membership_rows"]:
        if membership["signal_id"] == c1:
            membership["market"] = "ng"
            value = "gamma" if membership["member_id"] == "c1a" else "delta"
            membership["canonical_value"] = value
            membership["member_identity"] = f"ng|keyword|{value}"
    p2_extra = copy.deepcopy(
        next(row for row in payload["membership_rows"] if row["signal_id"] == p2)
    )
    p2_extra.update(
        {
            "member_id": "p2b",
            "member_identity": "ng|keyword|delta",
            "canonical_value": "delta",
            "source_families": ["search"],
            "platforms": ["search"],
            "row_id": "row_p2b",
        }
    )
    p3_extra = copy.deepcopy(
        next(row for row in payload["membership_rows"] if row["signal_id"] == p3)
    )
    p3_extra.update(
        {
            "member_id": "p3b",
            "member_identity": "ng|keyword|gamma",
            "canonical_value": "gamma",
            "source_families": ["news"],
            "platforms": ["news"],
            "row_id": "row_p3b",
        }
    )
    payload["membership_rows"].extend((p2_extra, p3_extra))
    payload["fixture_digest"] = replay.lineage_fixture_digest(payload)

    artifact = replay.evaluate_replay_lineage_fixture(payload)
    assert artifact.lineage_rows == ()
    assert artifact.missing_work == ("lineage_membership_unavailable",)


def test_graph_readiness_grid_is_exact_deterministic_and_never_selects():
    payload = grid_payload()
    artifact = replay.evaluate_replay_graph_readiness_grid(payload)

    assert tuple(replay.ReplayGridCandidateResult.__dataclass_fields__) == (
        "candidate_id",
        "provider_candidate_id",
        "graph_rule_candidate_id",
        "readiness_rule_candidate_id",
        "known_event_recall",
        "duplicate_clusters",
        "foreign_leakage",
        "membership_completeness",
        "evidence_coverage",
        "geo_coverage",
        "ready",
        "thin",
        "contradictory",
        "unchecked",
        "missing_work",
        "runtime_ms",
        "status",
        "display_eligible",
    )
    assert len(artifact.results) == 8
    assert artifact.recommendation is None
    assert artifact.certified_rule is None
    assert artifact == replay.evaluate_replay_graph_readiness_grid(payload)
    assert artifact == replay.evaluate_replay_graph_readiness_grid(
        {
            **payload,
            "observations": list(reversed(payload["observations"])),
            "component_reviews": list(reversed(payload["component_reviews"])),
            "readiness_cases": list(reversed(payload["readiness_cases"])),
        }
    )


def test_graph_readiness_grid_reports_full_denominators_and_leakage():
    result = replay.evaluate_replay_graph_readiness_grid(grid_payload()).results[0]
    assert result.known_event_recall == replay.Metric(3, 3, 1.0)
    assert result.duplicate_clusters == replay.Metric(1, 5, 0.2)
    assert result.foreign_leakage == replay.Metric(1, 5, 0.2)
    assert result.membership_completeness == replay.Metric(4, 5, 0.8)
    assert result.evidence_coverage == replay.Metric(2, 5, 0.4)
    assert result.geo_coverage == replay.Metric(2, 5, 0.4)
    assert result.ready == replay.Metric(1, 7, 1 / 7)
    assert result.thin == replay.Metric(4, 7, 4 / 7)
    assert result.contradictory == replay.Metric(1, 7, 1 / 7)
    assert result.unchecked == replay.Metric(1, 7, 1 / 7)
    assert result.missing_work == (
        "incomplete_current_window",
        "incomplete_history_window",
        "task5c_nonqualifying_evidence",
    )
    assert result.status == "provisional"
    assert result.display_eligible is False


def test_grid_kills_missing_matrix_promotion_and_automatic_winner():
    payload = grid_payload()
    payload["semantic_providers"][0]["matrix"].pop()
    payload["fixture_digest"] = replay.grid_fixture_digest(payload)
    with pytest.raises(ValueError, match="missing semantic pair"):
        replay.evaluate_replay_graph_readiness_grid(payload)

    artifact = replay.evaluate_replay_graph_readiness_grid(grid_payload())
    assert all(result.foreign_leakage.numerator == 1 for result in artifact.results)
    assert all(result.ready.denominator == 7 for result in artifact.results)
    assert all(result.display_eligible is False for result in artifact.results)
    assert artifact.recommendation is None


@pytest.mark.parametrize(
    "missing_work",
    [
        ["incomplete_current_window", "incomplete_history_window"],
        ["incomplete_current_window", "incomplete_history_window", "replacement_reason"],
        [
            "incomplete_current_window",
            "incomplete_history_window",
            "task5c_nonqualifying_evidence",
            "task5c_nonqualifying_evidence",
        ],
        [
            "extra_reason",
            "incomplete_current_window",
            "incomplete_history_window",
            "task5c_nonqualifying_evidence",
        ],
    ],
)
def test_grid_requires_exact_approved_task5c_missing_work(missing_work):
    payload = grid_payload()
    payload["task5c_contract"]["missing_work"] = missing_work
    payload["fixture_digest"] = replay.grid_fixture_digest(payload)

    with pytest.raises(IncompleteReplayInput, match="exact approved Task 5C missing work"):
        replay.evaluate_replay_graph_readiness_grid(payload)


def refresh_coverage_digest(payload: dict) -> None:
    facts = dict(payload)
    facts["rows"] = tuple(sorted(facts["rows"], key=lambda row: row["row_id"]))
    facts.pop("fixture_digest", None)
    payload["fixture_digest"] = replay._digest(facts)


@pytest.mark.parametrize("field", ["prior_observed_at", "current_observed_at"])
def test_coverage_rejects_future_temporal_facts(field):
    payload = coverage_payload()
    payload["rows"][0][field] = "2026-08-26T00:00:00Z"
    refresh_coverage_digest(payload)

    with pytest.raises(FutureLeakageDetected, match="future coverage timestamp"):
        replay.probe_replay_geo_direction_coverage(payload)


def test_coverage_accepts_signed_sentiment_and_tone_without_inference():
    payload = coverage_payload()
    row = next(item for item in payload["rows"] if item["row_id"] == "row_regional_only")
    row["sentiment_lexicon_score"] = -0.5
    row["tone_polarity"] = -0.5
    refresh_coverage_digest(payload)

    artifact = replay.probe_replay_geo_direction_coverage(payload)
    assert "row_regional_only" in artifact.unresolved_geo_rows.row_ids
    assert "row_regional_only" not in artifact.direction_eligible_rows.row_ids


def test_geo_direction_coverage_artifact_is_exact_deterministic_and_output_free():
    payload = coverage_payload()
    artifact = replay.probe_replay_geo_direction_coverage(payload)

    assert tuple(replay.ReplayCoverageArtifact.__dataclass_fields__) == (
        "artifact_version",
        "total_projected_rows",
        "explicit_geo_evidence_rows",
        "unresolved_geo_rows",
        "factual_location_field_presence",
        "direction_eligible_rows",
        "direction_conflict_rows",
        "unavailable_producer_reasons",
        "required_upstream_evidence_fields",
    )
    assert artifact == replay.probe_replay_geo_direction_coverage(payload)
    assert artifact == replay.probe_replay_geo_direction_coverage(
        {**payload, "rows": list(reversed(payload["rows"]))}
    )
    assert not hasattr(artifact, "geo_confidence")
    assert not hasattr(artifact, "direction")
    assert not hasattr(artifact, "readiness")
    assert not hasattr(artifact, "score")
    assert not hasattr(artifact, "recommendation")


def test_geo_direction_coverage_counts_only_explicit_and_temporal_facts():
    artifact = replay.probe_replay_geo_direction_coverage(coverage_payload())

    assert artifact.total_projected_rows == replay.CoverageRecord(
        4, ("row_conflict", "row_explicit_geo", "row_regional_only", "row_unresolved")
    )
    assert artifact.explicit_geo_evidence_rows == replay.CoverageRecord(1, ("row_explicit_geo",))
    assert artifact.unresolved_geo_rows == replay.CoverageRecord(
        3, ("row_conflict", "row_regional_only", "row_unresolved")
    )
    assert artifact.factual_location_field_presence == replay.CoverageRecord(
        2, ("row_conflict", "row_explicit_geo")
    )
    assert artifact.direction_eligible_rows == replay.CoverageRecord(
        2, ("row_conflict", "row_explicit_geo")
    )
    assert artifact.direction_conflict_rows == replay.CoverageRecord(1, ("row_conflict",))
    assert artifact.unavailable_producer_reasons == (
        "direction_producer_unavailable",
        "geo_provenance_unavailable",
    )
    assert artifact.required_upstream_evidence_fields == (
        "enriched_content.direction_baseline_at",
        "enriched_content.direction_observed_at",
        "enriched_content.geo_evidence_id",
        "enriched_content.geo_provenance",
    )


def test_coverage_mutations_cannot_infer_or_hide_geo_and_direction():
    payload = coverage_payload()
    regional = next(row for row in payload["rows"] if row["row_id"] == "row_regional_only")
    assert regional["regional_score"] == 0.95
    artifact = replay.probe_replay_geo_direction_coverage(payload)
    assert "row_regional_only" in artifact.unresolved_geo_rows.row_ids
    assert "row_regional_only" not in artifact.explicit_geo_evidence_rows.row_ids
    assert "row_regional_only" not in artifact.direction_eligible_rows.row_ids
    assert "row_conflict" in artifact.direction_conflict_rows.row_ids
    assert not hasattr(artifact, "recommendation")


def refresh_universe_digest(universe: dict) -> None:
    universe["digest"] = replay._digest(
        {key: value for key, value in universe.items() if key != "digest"}
    )


def test_metric_formula_comparison_is_exact_deterministic_and_has_no_winner():
    payload = metric_payload()
    comparison = replay.compare_replay_metric_formulas(payload)

    assert tuple(replay.ReplayMetricFormulaResult.__dataclass_fields__) == (
        "formula_id",
        "formula_version",
        "metric_family",
        "current_window",
        "history_window",
        "source_row_ids",
        "numerator",
        "denominator",
        "value",
        "missing_measurement",
        "rival_formula_id",
    )
    assert len(comparison.results) == 10
    assert comparison.recommendation is None
    assert all(result.formula_id and result.formula_version for result in comparison.results)
    assert all(result.current_window and result.history_window for result in comparison.results)
    assert all(result.source_row_ids for result in comparison.results)
    assert comparison == replay.compare_replay_metric_formulas(payload)
    assert comparison == replay.compare_replay_metric_formulas(
        {
            **payload,
            "evidence_rows": list(reversed(payload["evidence_rows"])),
            "formulas": list(reversed(payload["formulas"])),
        }
    )


def test_metric_formulas_use_declared_saturation_and_family_independence():
    by_id = {
        result.formula_id: result
        for result in replay.compare_replay_metric_formulas(metric_payload()).results
    }
    breadth = by_id["breadth_combined_saturation"]
    assert (breadth.numerator, breadth.denominator, breadth.value) == (5.0, 9.0, 5 / 9)
    assert by_id["breadth_mean_saturation"].value == pytest.approx(0.55)
    assert by_id["independence_one_minus_hhi"].value == 0.5
    assert by_id["independence_one_minus_max_share"].value == 0.5
    assert by_id["geo_all_receipt_coverage"].value == 0.5
    assert by_id["geo_qualifying_receipt_coverage"].value == 2 / 3


def test_historical_formula_receipts_union_current_and_selected_history():
    by_id = {
        result.formula_id: result
        for result in replay.compare_replay_metric_formulas(metric_payload()).results
    }
    expected = ("hist_row_1", "hist_row_2", "row_news_youtube", "row_search")
    assert by_id["historical_similarity_max"].source_row_ids == expected
    assert by_id["novelty_one_minus_max"].source_row_ids == expected


@pytest.mark.parametrize(
    ("window", "start", "end"),
    [
        ("current_window", "2026-08-18", "2026-08-24"),
        ("history_window", "2026-07-21", "2026-08-17"),
    ],
)
def test_metric_windows_are_exactly_anchored(window, start, end):
    payload = metric_payload()
    payload[window]["start_date"] = start
    payload[window]["end_date"] = end

    with pytest.raises(IncompleteReplayInput, match="metric window anchor"):
        replay.compare_replay_metric_formulas(payload)


def test_breadth_requires_independent_digested_universe_reference():
    payload = metric_payload()
    universe = payload["saturation_universes"][0]
    universe["source"] = "observed_sample"
    refresh_universe_digest(universe)
    with pytest.raises(IncompleteReplayInput, match="independent saturation universe"):
        replay.compare_replay_metric_formulas(payload)

    payload = metric_payload()
    payload["formulas"][0]["universe_id"] = "unknown_universe"
    payload["formula_digest"] = replay._digest(
        tuple(sorted(payload["formulas"], key=lambda item: item["formula_id"]))
    )
    with pytest.raises(IncompleteReplayInput, match="universe reference"):
        replay.compare_replay_metric_formulas(payload)


def test_approved_universe_digest_cannot_be_replaced_by_sample_content():
    payload = metric_payload()
    universe = payload["saturation_universes"][0]
    universe["source_families"] = ["brand24", "news", "search"]
    universe["platforms"] = ["instagram", "search", "tiktok", "youtube"]
    refresh_universe_digest(universe)

    with pytest.raises(IncompleteReplayInput, match="approved saturation universe digest"):
        replay.compare_replay_metric_formulas(payload)


@pytest.mark.parametrize("field", ["universe_id", "version"])
def test_saturation_universe_registry_rejects_unknown_identity_or_version(field):
    payload = metric_payload()
    universe = payload["saturation_universes"][0]
    universe[field] = "unknown"
    if field == "universe_id":
        for formula in payload["formulas"]:
            if formula["metric_family"] == "breadth":
                formula["universe_id"] = "unknown"
        payload["formula_digest"] = replay._digest(
            tuple(sorted(payload["formulas"], key=lambda item: item["formula_id"]))
        )
    refresh_universe_digest(universe)

    with pytest.raises(IncompleteReplayInput, match="approved saturation universe"):
        replay.compare_replay_metric_formulas(payload)


def test_metric_formula_rivals_must_be_nonself_and_exactly_symmetric():
    payload = metric_payload()
    payload["formulas"][0]["rival_formula_id"] = "geo_all_receipt_coverage"
    payload["formula_digest"] = replay._digest(
        tuple(sorted(payload["formulas"], key=lambda item: item["formula_id"]))
    )

    with pytest.raises(IncompleteReplayInput, match="symmetric"):
        replay.compare_replay_metric_formulas(payload)


def test_metric_formulas_reject_future_window_leakage():
    payload = metric_payload()
    payload["current_window"]["end_date"] = "2026-08-26"

    with pytest.raises(FutureLeakageDetected, match="future metric window"):
        replay.compare_replay_metric_formulas(payload)


@pytest.mark.parametrize(
    ("mutation", "families", "reason"),
    [
        (
            lambda payload: payload["historical"].__setitem__("candidates", []),
            {"novelty", "historical_similarity"},
            "missing_history",
        ),
        (
            lambda payload: payload["current_window"].__setitem__("complete_partitions", False),
            None,
            "incomplete_current_window",
        ),
        (
            lambda payload: payload["history_window"].__setitem__("complete_partitions", False),
            None,
            "incomplete_history_window",
        ),
        (
            lambda payload: [
                row.__setitem__("qualifies_evidence", False) for row in payload["evidence_rows"]
            ],
            {"velocity", "breadth", "independence"},
            "no_qualifying_evidence",
        ),
        (
            lambda payload: [
                row.__setitem__("geo_provenance", None) for row in payload["evidence_rows"]
            ],
            {"geo_coverage"},
            "geo_provenance_unavailable",
        ),
        (
            lambda payload: [
                row.__setitem__("history_count", 0)
                for row in payload["evidence_rows"]
                if row["qualifies_evidence"]
            ],
            {"velocity"},
            "zero_denominator",
        ),
    ],
)
def test_metric_formulas_return_bounded_missing_measurements(mutation, families, reason):
    payload = metric_payload()
    mutation(payload)
    comparison = replay.compare_replay_metric_formulas(payload)
    selected = (
        comparison.results
        if families is None
        else tuple(result for result in comparison.results if result.metric_family in families)
    )

    assert selected
    assert all(result.value is None for result in selected)
    assert all(result.missing_measurement.reason == reason for result in selected)
    assert all(result.numerator is None and result.denominator is None for result in selected)
    assert comparison.recommendation is None


def test_replay_semantic_candidates_are_exact_deterministic_and_never_recommend():
    payload = semantic_payload()
    comparison = replay.compare_replay_semantic_candidates(payload)

    assert tuple(replay.ReplaySemanticCandidate.__dataclass_fields__) == (
        "candidate_id",
        "provider_kind",
        "provider_version",
        "metering_status",
        "matrix_digest",
        "pair_count",
        "runtime_ms",
        "input_units",
        "estimated_cost",
        "missing_work",
    )
    assert tuple(item.provider_kind for item in comparison.candidates) == (
        "frozen_embedding_matrix",
        "lexical_similarity",
    )
    assert comparison.recommendation is None
    assert comparison == replay.compare_replay_semantic_candidates(payload)
    assert comparison == replay.compare_replay_semantic_candidates(
        {**payload, "observations": list(reversed(payload["observations"]))}
    )


@pytest.mark.parametrize("mutation", ["unknown", "missing"])
def test_replay_semantic_candidate_rejects_nonexact_fields(mutation):
    candidate = copy.deepcopy(semantic_payload()["lexical"]["candidate"])
    if mutation == "unknown":
        candidate["generated_at"] = "2026-08-25T12:00:00Z"
    else:
        candidate.pop("pair_count")

    with pytest.raises(IncompleteReplayInput, match="semantic candidate fields"):
        replay.validate_replay_semantic_candidate(candidate)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda rows: rows.pop(), "missing semantic pair"),
        (
            lambda rows: rows.append(
                {"left": "ng|keyword|beta", "right": "za|keyword|alpha wave", "score": 0.2}
            ),
            "cross-market semantic pair",
        ),
        (
            lambda rows: rows.append(
                {"left": "za|keyword|alpha wave", "right": "za|keyword|alpha wave", "score": 0.2}
            ),
            "self semantic pair",
        ),
        (
            lambda rows: rows.__setitem__(
                0, {"left": rows[0]["right"], "right": rows[0]["left"], "score": 0.8}
            ),
            "ordered semantic pair",
        ),
        (lambda rows: rows[0].__setitem__("score", True), "finite within zero and one"),
        (lambda rows: rows[0].__setitem__("score", math.inf), "finite within zero and one"),
        (lambda rows: rows[0].__setitem__("score", 1.1), "finite within zero and one"),
        (
            lambda rows: rows.append(
                {"left": "za|keyword|alpha sound", "right": "za|keyword|unknown", "score": 0.2}
            ),
            "extra semantic pair",
        ),
    ],
)
def test_frozen_embedding_uses_strict_existing_matrix_admission(mutation, match):
    payload = semantic_payload()
    mutation(payload["frozen_embedding"]["matrix"])

    with pytest.raises(ValueError, match=match):
        replay.compare_replay_semantic_candidates(payload)


def test_semantic_comparison_kills_relabel_cost_claim_live_import_and_winner():
    payload = semantic_payload()
    payload["lexical"]["candidate"]["provider_kind"] = "embedding_similarity"
    with pytest.raises(IncompleteReplayInput, match="provider kind"):
        replay.compare_replay_semantic_candidates(payload)

    payload = semantic_payload()
    payload["frozen_embedding"]["candidate"]["estimated_cost"] = 0.01
    with pytest.raises(IncompleteReplayInput, match="estimated cost"):
        replay.compare_replay_semantic_candidates(payload)

    source = inspect.getsource(replay).lower()
    assert "vertexai" not in source
    assert "embedding_classifier" not in source
    assert replay.compare_replay_semantic_candidates(semantic_payload()).recommendation is None


def test_lexical_config_requires_at_least_one_strictly_positive_weight():
    payload = semantic_payload()
    weighting = payload["lexical"]["weighting"]
    for field in ("term_weight", "alias_weight", "topic_weight"):
        weighting[field] = 0.0
    payload["lexical"]["config_digest"] = replay._digest(
        {"tokenizer": payload["lexical"]["tokenizer"], "weighting": weighting}
    )

    with pytest.raises(IncompleteReplayInput, match="strictly positive"):
        replay.compare_replay_semantic_candidates(payload)


def test_lexical_matrix_rejects_zero_union_weight_without_dividing():
    payload = semantic_payload()
    observations = replay._semantic_observations(payload["observations"])
    weighting = {
        **payload["lexical"]["weighting"],
        "term_weight": 0.0,
        "alias_weight": 0.0,
        "topic_weight": 0.0,
    }

    with pytest.raises(IncompleteReplayInput, match="union weight"):
        replay._lexical_matrix(observations, payload["lexical"]["tokenizer"], weighting)


def fixture_timestamp(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat().replace("+00:00", "Z")


def matching_adapter_snapshot(result) -> dict:
    projection = result.evidence_projection
    assert projection is not None
    payload = json.loads(ADAPTER_SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
    rows = payload["rows_by_table"]
    rows["event_ledger"] = [
        {
            **task5c.event_row(item.term, item.market),
            "ledger_id": projection.candidate_only_receipts[
                f"{item.market}|{item.candidate_type}|{item.term}"
            ][0],
            "trend_date": item.observed_dates[0],
            "as_of": item.observed_timestamps[0],
        }
        for item in result.candidate_inputs.event_ledger
    ]
    rows["seed_graph"] = [
        {
            **task5c.graph_row(item.term, item.market),
            "trend_date": item.observed_dates[0],
            "event_date": item.observed_dates[-1],
            "sample_row_ids": list(item.row_ids),
        }
        for item in result.candidate_inputs.seed_graph
    ]
    rows["seed_candidates"] = []
    for item in result.candidate_inputs.seed_candidates:
        identity = f"{item.market}|{item.candidate_type}|{item.term}"
        candidate_id = projection.candidate_only_receipts[identity][0]
        rows["seed_candidates"].append(
            {
                **task5c.candidate_row(item.term),
                "candidate_id": candidate_id,
                "proposed_date": item.observed_dates[0],
                "sample_row_ids": [row_id for row_id in item.row_ids if row_id != candidate_id],
            }
        )
    receipts = {
        receipt.row_id: receipt
        for values in projection.receipts_by_member.values()
        for receipt in values
    }
    if "ambiguous_sample_id" in result.missing_work:
        rows["enriched_content"] = [
            {
                "id": row_id,
                "source": "rss",
                "platform": "news",
                "vendor_family": "rss",
                "channel_family": "news",
                "source_family": "news",
                "market": "za",
                "published_at": "2026-08-25T10:00:00Z",
                "collected_at": "2026-08-25T12:00:00Z",
            }
            for row_id in projection.unresolved_sample_ids
            for _ in range(2)
        ]
    else:
        rows["enriched_content"] = [
            {
                "id": receipt.row_id,
                "source": receipt.source,
                "platform": receipt.platform,
                "vendor_family": receipt.vendor_family,
                "channel_family": receipt.channel_family,
                "source_family": receipt.channel_family,
                "market": receipt.market,
                "published_at": fixture_timestamp(receipt.published_at),
                "collected_at": fixture_timestamp(receipt.collected_at),
            }
            for receipt in receipts.values()
        ]
    rows["raw_content"] = []
    rows["signal_candidates_v2"] = [
        {
            "signal_id": key,
            "signal_date": CUTOFF.isoformat(),
            "market": values[0].member_identity.split("|", 1)[0],
            "run_id": result.run_id,
        }
        for key, values in projection.memberships_by_component.items()
    ]
    rows["signal_membership_v2"] = [
        {
            "member_id": membership.member_id,
            "member_identity": membership.member_identity,
            "source_families": list(membership.source_families),
            "vendor_families": list(membership.vendor_families),
            "channel_families": list(membership.channel_families),
            "signal_id": key,
            "signal_date": CUTOFF.isoformat(),
            "run_id": result.run_id,
        }
        for key, values in projection.memberships_by_component.items()
        for membership in values
    ]
    payload["row_counts_by_table"] = {key: len(values) for key, values in rows.items()}
    payload["table_digests"] = {key: replay._digest(values) for key, values in rows.items()}
    event_ids = tuple(sorted(row["ledger_id"] for row in rows["event_ledger"]))
    graph_ids = tuple(
        sorted(
            f"{item.market}|{item.candidate_type}|{item.term}"
            for item in result.candidate_inputs.seed_graph
        )
    )
    seed_ids = tuple(sorted(row["candidate_id"] for row in rows["seed_candidates"]))
    membership_ids = tuple(sorted(row["member_id"] for row in rows["signal_membership_v2"]))
    projected_receipt_ids = tuple(
        sorted(
            f"{identity}|{receipt.row_id}"
            for identity, values in projection.receipts_by_member.items()
            for receipt in values
        )
    )
    section_ids = {
        "components": tuple(sorted(projection.memberships_by_component)),
        "event_ledger_candidates": event_ids,
        "known_events": ("known_ke", "known_ng", "known_za"),
        "memberships": membership_ids,
        "observations": tuple(
            sorted(
                f"{item.market}|{item.candidate_type}|{item.term}" for item in result.observations
            )
        ),
        "prior_snapshots": (),
        "provider_outputs": (),
        "receipts": projected_receipt_ids,
        "seed_candidates": seed_ids,
        "seed_graph_candidates": graph_ids,
    }
    payload["section_identity_sets"] = {key: list(values) for key, values in section_ids.items()}
    payload["section_counts"] = {key: len(values) for key, values in section_ids.items()}
    without_digest = dict(payload)
    without_digest.pop("source_digest", None)
    payload["source_digest"] = replay._digest(without_digest)
    return payload


def refresh_snapshot_digests(snapshot: dict) -> None:
    snapshot["row_counts_by_table"] = {
        key: len(values) for key, values in snapshot["rows_by_table"].items()
    }
    snapshot["table_digests"] = {
        key: replay._digest(values) for key, values in snapshot["rows_by_table"].items()
    }
    without_digest = dict(snapshot)
    without_digest.pop("source_digest", None)
    snapshot["source_digest"] = replay._digest(without_digest)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("vendor_family", "news", "source identity"),
        ("channel_family", "reddit", "source identity"),
        ("source_family", "reddit", "source identity"),
    ],
)
def test_replay_source_identity_mutations_refuse_after_digest_refresh(field, value, message):
    payload = bundle_payload()
    source = payload["source_snapshot"]
    source["rows_by_table"]["enriched_content"][0][field] = value
    refresh_snapshot_digests(source)
    with pytest.raises(IncompleteReplayInput, match=message):
        replay.validate_replay_input(payload)


def test_replay_source_identity_changes_move_fixture_digest() -> None:
    payload = bundle_payload()
    original = replay._digest(payload)
    source = payload["source_snapshot"]
    source["rows_by_table"]["enriched_content"][0]["vendor_family"] = "changed"
    refresh_snapshot_digests(source)
    assert replay._digest(payload) != original


def test_replay_source_field_reordering_refuses_even_when_canonical_digest_is_unchanged() -> None:
    payload = bundle_payload()
    source = payload["source_snapshot"]
    row = source["rows_by_table"]["enriched_content"][0]
    original_digest = replay._digest(row)
    row["vendor_family"] = row.pop("vendor_family")
    assert replay._digest(row) == original_digest
    refresh_snapshot_digests(source)
    with pytest.raises(IncompleteReplayInput, match="field order"):
        replay.validate_replay_input(payload)


def adapter_metadata(result=None) -> dict:
    payload = bundle_payload()
    if result is None:
        result = projected_result()
    history_window = copy.deepcopy(payload["history_window"])
    history_window["complete_partitions"] = False
    return {
        "scope": task5c.scope(),
        "source_snapshot": matching_adapter_snapshot(result),
        "known_event_set": payload["known_event_set"],
        "history_window": history_window,
        "prior_snapshots": (),
        "provider_outputs": (),
        "human_review_state": "pending",
    }


def projected_result(*, ambiguous: bool = False, missing: bool = False):
    alpha = task5c.event_row("alpha")
    alpha["trend_date"] = CUTOFF
    alpha["as_of"] = datetime(2026, 8, 25, 12, tzinfo=UTC)
    beta = task5c.graph_row("beta")
    gamma = task5c.candidate_row("gamma")
    beta["trend_date"] = CUTOFF
    beta["event_date"] = CUTOFF
    gamma["proposed_date"] = CUTOFF
    beta["sample_row_ids"] = ["shared"]
    gamma["sample_row_ids"] = ["shared"]
    enriched_rows = []
    if ambiguous:
        enriched_rows = [task5c.evidence_row("shared", match_count=2)]
    elif not missing:
        enriched_rows = [task5c.evidence_row("shared")]
    for row in enriched_rows:
        row["published_at"] = datetime(2026, 8, 25, 10, tzinfo=UTC)
        row["collected_at"] = datetime(2026, 8, 25, 12, tzinfo=UTC)
    return run_dynamic_signal_identity(
        CUTOFF,
        task5c.scope(),
        task5c.FakeClient(
            event_rows=[alpha],
            graph_rows=[beta],
            candidate_rows=[gamma],
            enriched_rows=enriched_rows,
        ),
        task5c.DATASET,
        False,
    )


def adapt(result):
    return replay.adapt_dynamic_result_to_replay_input(result, **adapter_metadata(result))


def test_task5c_adapter_preserves_zero_write_projection_without_fabrication():
    result = projected_result()
    bundle = adapt(result)
    projection = result.evidence_projection
    assert projection is not None

    assert tuple(replay.ReplayCandidateInputs.__dataclass_fields__) == (
        "event_ledger",
        "seed_graph",
        "seed_candidates",
        "candidate_only_receipts",
        "unresolved_sample_ids",
        "missing_work",
        "source_windows",
    )
    assert bundle.candidate_inputs.event_ledger == result.candidate_inputs.event_ledger
    assert bundle.candidate_inputs.seed_graph == result.candidate_inputs.seed_graph
    assert bundle.candidate_inputs.seed_candidates == result.candidate_inputs.seed_candidates
    assert all(item.candidate_score is None for item in bundle.candidate_inputs.seed_candidates)
    assert bundle.observations == result.observations
    assert bundle.components_by_candidate == {}
    assert bundle.memberships_by_candidate == {}
    assert bundle.receipts_by_member == projection.receipts_by_member
    assert bundle.current_window == projection.source_window
    assert bundle.current_window.complete_partitions is False
    assert bundle.history_window["complete_partitions"] is False
    assert set(bundle.source_snapshot) == set(replay._SNAPSHOT_FIELDS)
    assert bundle.candidate_inputs.candidate_only_receipts == (projection.candidate_only_receipts)
    assert bundle.candidate_inputs.unresolved_sample_ids == ()
    assert bundle.candidate_inputs.missing_work == result.missing_work
    assert bundle.candidate_inputs.source_windows == result.source_windows
    assert bundle.receipts_by_member["za|keyword|alpha"] == ()
    assert tuple(item.row_id for item in bundle.receipts_by_member["za|keyword|beta"]) == (
        "shared",
    )
    assert tuple(item.row_id for item in bundle.receipts_by_member["za|keyword|gamma"]) == (
        "shared",
    )
    source = bundle.source_snapshot["rows_by_table"]["enriched_content"][0]
    assert source["vendor_family"] == "rss"
    assert source["channel_family"] == "news"
    assert source["source_family"] == source["channel_family"]
    assert replay.replay_input_digest(bundle) == EXPECTED_ADAPTER_DIGEST
    assert replay.validate_replay_input_bundle(bundle) is bundle


@pytest.mark.parametrize(
    ("ambiguous", "missing", "expected_missing"),
    [
        (True, False, ("shared",)),
        (False, True, ("shared",)),
    ],
)
def test_task5c_adapter_retains_ambiguous_and_missing_exact_ids(
    ambiguous, missing, expected_missing
):
    result = projected_result(ambiguous=ambiguous, missing=missing)
    bundle = adapt(result)

    assert bundle.candidate_inputs.unresolved_sample_ids == expected_missing
    assert bundle.candidate_inputs.missing_work == result.missing_work
    assert bundle.receipts_by_member["za|keyword|beta"] == ()
    assert bundle.receipts_by_member["za|keyword|gamma"] == ()
    assert bundle.components_by_candidate == {}
    assert bundle.memberships_by_candidate == {}


def test_task5c_adapter_preserves_nonqualifying_membership_and_identity_digests(monkeypatch):
    monkeypatch.setattr(
        dynamic_pipeline,
        "compose_components",
        lambda observations, _provider, _rules: (
            task5c.component(*(item.term for item in observations)),
        ),
    )
    result = run_dynamic_signal_identity(
        CUTOFF,
        task5c.scope(),
        task5c.FakeClient(
            graph_rows=[task5c.graph_row("beta")],
            enriched_rows=[task5c.evidence_row("row_za_beta")],
        ),
        task5c.DATASET,
        False,
        rule_bundle=task5c.provisional_rules(),
        semantic_provider=task5c.semantic_provider(),
    )
    bundle = adapt(result)
    projection = result.evidence_projection
    assert projection is not None

    assert bundle.memberships_by_candidate == projection.memberships_by_component
    membership = next(iter(bundle.memberships_by_candidate.values()))[0]
    assert membership.qualifies_evidence is False
    assert (
        membership.member_id
        == next(iter(projection.memberships_by_component.values()))[0].member_id
    )
    assert len(membership.member_id) == len("mem_") + 64


def test_task5c_adapter_rejects_nonzero_or_nondry_run_results():
    result = projected_result()
    for changed in (
        replace(result, dry_run=False),
        replace(result, persistable_count=1),
        replace(result, row_counts=MappingProxyType({"candidates": 1})),
        replace(result, persistence_result=object()),
    ):
        with pytest.raises(IncompleteReplayInput, match="zero-write dry-run"):
            adapt(changed)


def test_task5c_adapter_rejects_foreign_scope_or_contract():
    result = projected_result()
    metadata = adapter_metadata(result)
    with pytest.raises(IncompleteReplayInput, match="scope run ID"):
        replay.adapt_dynamic_result_to_replay_input(
            result,
            **{**metadata, "scope": replace(metadata["scope"], run_id="foreign_run")},
        )
    with pytest.raises(IncompleteReplayInput, match="scope contract"):
        adapt(replace(result, contract_version="foreign_contract"))


def test_task5c_adapter_rejects_unrelated_or_mutated_source_snapshot():
    result = projected_result()
    metadata = adapter_metadata(result)
    with pytest.raises(IncompleteReplayInput, match="source snapshot"):
        replay.adapt_dynamic_result_to_replay_input(
            result,
            **{**metadata, "source_snapshot": bundle_payload()["source_snapshot"]},
        )

    mismatched = copy.deepcopy(metadata["source_snapshot"])
    mismatched["row_counts_by_table"]["event_ledger"] += 1
    with pytest.raises(IncompleteReplayInput, match="row count"):
        replay.adapt_dynamic_result_to_replay_input(
            result, **{**metadata, "source_snapshot": mismatched}
        )


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("event_ledger", "entity_key", "changed event"),
        ("seed_candidates", "candidate_value", "changed candidate"),
        ("seed_graph", "sample_row_ids", ["changed_sample"]),
        ("enriched_content", "market", "ng"),
        ("enriched_content", "collected_at", "2026-08-25T11:59:00Z"),
    ],
)
def test_task5c_adapter_rejects_full_source_fact_mismatch(table, field, value):
    result = projected_result()
    metadata = adapter_metadata(result)
    mismatched = copy.deepcopy(metadata["source_snapshot"])
    mismatched["rows_by_table"][table][0][field] = value
    refresh_snapshot_digests(mismatched)

    with pytest.raises(IncompleteReplayInput, match=r"source snapshot .* facts"):
        replay.adapt_dynamic_result_to_replay_input(
            result, **{**metadata, "source_snapshot": mismatched}
        )


def test_task5c_adapter_never_injects_source_snapshot_fields():
    bundle = adapt(projected_result())
    assert set(bundle.source_snapshot) == set(replay._SNAPSHOT_FIELDS)
    for forbidden in (
        "candidate_only_receipts",
        "unresolved_sample_ids",
        "missing_work",
        "source_windows",
    ):
        assert forbidden not in bundle.source_snapshot
    assert replay.validate_replay_input_bundle(bundle) is bundle


def test_task5c_adapter_always_calls_settled_bundle_validator(monkeypatch):
    result = projected_result()
    sentinel = object()
    validator = Mock(return_value=sentinel)
    monkeypatch.setattr(replay, "validate_replay_input_bundle", validator)

    assert adapt(result) is sentinel
    validator.assert_called_once()
    assert isinstance(validator.call_args.args[0], replay.ReplayInputBundle)


def test_task5c_adapter_is_byte_identical_under_input_reversal():
    result = projected_result()
    projection = result.evidence_projection
    assert projection is not None
    reversed_result = replace(
        result,
        observations=tuple(reversed(result.observations)),
        evidence_projection=replace(
            projection,
            receipts_by_member=MappingProxyType(
                dict(reversed(tuple(projection.receipts_by_member.items())))
            ),
            candidate_only_receipts=MappingProxyType(
                dict(reversed(tuple(projection.candidate_only_receipts.items())))
            ),
        ),
    )

    assert replay.replay_input_digest(adapt(result)) == replay.replay_input_digest(
        adapt(reversed_result)
    )


def test_replay_input_bundle_is_exact_immutable_and_literal_digest():
    payload = bundle_payload()
    bundle = replay.validate_replay_input(payload)

    assert tuple(replay.ReplayInputBundle.__dataclass_fields__) == (
        "artifact_version",
        "contract_version",
        "cutoff",
        "scope",
        "source_snapshot",
        "known_event_set",
        "candidate_inputs",
        "observations",
        "components_by_candidate",
        "memberships_by_candidate",
        "receipts_by_member",
        "current_window",
        "history_window",
        "prior_snapshots",
        "provider_outputs",
        "human_review_state",
        # Optional and last, so every existing replay consumer keeps working
        # and a bundle carrying no scoring source is simply unavailable.
        "scoring_sources_by_candidate",
    )
    assert bundle.scoring_sources_by_candidate is None
    assert bundle.artifact_version == "replay_input_v1"
    assert bundle.cutoff == CUTOFF
    assert bundle.source_snapshot["fixture_scope"] == "synthetic_test_only"
    assert "certified" not in bundle.source_snapshot
    assert bundle.human_review_state == "pending"
    assert replay.replay_input_digest(payload) == EXPECTED_BUNDLE_DIGEST
    with pytest.raises(TypeError):
        bundle.scope["client_scope_id"] = "changed"


@pytest.mark.parametrize("mutation", ["unknown", "missing"])
def test_replay_input_rejects_unknown_and_missing_top_level_fields(mutation):
    payload = bundle_payload()
    if mutation == "unknown":
        payload["unexpected"] = True
    else:
        payload.pop("provider_outputs")

    with pytest.raises(IncompleteReplayInput, match="bundle fields"):
        replay.validate_replay_input(payload)


def test_replay_input_rejects_duplicate_identities():
    payload = bundle_payload()
    payload["observations"].append(copy.deepcopy(payload["observations"][0]))

    with pytest.raises(IncompleteReplayInput, match="duplicate observation identity"):
        replay.validate_replay_input(payload)


@pytest.mark.parametrize("field", ["source_digest", "table_digest", "known_event_digest"])
def test_replay_input_rejects_missing_or_incomplete_digests(field):
    payload = bundle_payload()
    if field == "source_digest":
        payload["source_snapshot"]["source_digest"] = "0" * 63
    elif field == "table_digest":
        payload["source_snapshot"]["table_digests"]["event_ledger"] = ""
    else:
        payload["known_event_set"]["digest"] = None

    with pytest.raises(IncompleteReplayInput, match="digest"):
        replay.validate_replay_input(payload)


def test_replay_input_rejects_future_source_timestamp():
    payload = bundle_payload()
    payload["observations"][0]["source_max_observed_at"] = "2026-08-26T00:00:00Z"

    with pytest.raises(FutureLeakageDetected, match="future timestamp"):
        replay.validate_replay_input(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("source_snapshot", "captured_at"),
        ("observations", "source_max_observed_at"),
        ("receipts_by_member", "published_at"),
    ],
)
def test_replay_input_rejects_every_defined_future_timestamp(section, field):
    payload = bundle_payload()
    if section == "source_snapshot":
        target = payload[section]
    elif section == "receipts_by_member":
        target = payload[section]["za|keyword|fixture"][0]
    else:
        target = payload[section][0]
    target[field] = "2026-08-26T00:00:00Z"

    with pytest.raises(FutureLeakageDetected, match="future timestamp"):
        replay.validate_replay_input(payload)


@pytest.mark.parametrize("window", ["current_window", "history_window"])
def test_replay_input_rejects_unbounded_or_incomplete_windows(window):
    payload = bundle_payload()
    payload[window]["complete_partitions"] = False

    with pytest.raises(IncompleteReplayInput, match="complete"):
        replay.validate_replay_input(payload)


def test_replay_input_rejects_inconsistent_row_counts():
    payload = bundle_payload()
    payload["source_snapshot"]["row_counts_by_table"]["event_ledger"] += 1

    with pytest.raises(IncompleteReplayInput, match="row count"):
        replay.validate_replay_input(payload)


def test_replay_input_rejects_any_synthetic_certified_state():
    payload = bundle_payload()
    payload["source_snapshot"]["certified"] = False

    with pytest.raises(IncompleteReplayInput, match="forbidden fixture key"):
        replay.validate_replay_input(payload)


@pytest.mark.parametrize(
    "target",
    [
        lambda payload: payload["scope"],
        lambda payload: payload["source_snapshot"],
        lambda payload: payload["known_event_set"],
        lambda payload: payload["known_event_set"]["events"][0],
        lambda payload: payload["source_snapshot"]["rows_by_table"]["event_ledger"][0],
        lambda payload: payload["candidate_inputs"]["event_ledger"][0],
        lambda payload: payload["observations"][0],
        lambda payload: payload["components_by_candidate"]["candidate_fixture"],
        lambda payload: payload["memberships_by_candidate"]["candidate_fixture"][0],
        lambda payload: payload["receipts_by_member"]["za|keyword|fixture"][0],
        lambda payload: payload["current_window"],
        lambda payload: payload["prior_snapshots"][0],
        lambda payload: payload["provider_outputs"][0],
    ],
)
def test_replay_input_rejects_unknown_nested_fields(target):
    payload = bundle_payload()
    target(payload)["unexpected"] = True

    with pytest.raises(IncompleteReplayInput, match="fields"):
        replay.validate_replay_input(payload)


@pytest.mark.parametrize("forbidden", ["certified", "certification", "recommendation"])
def test_replay_input_rejects_forbidden_keys_at_any_depth(forbidden):
    payload = bundle_payload()
    payload["components_by_candidate"]["candidate_fixture"][forbidden] = False

    with pytest.raises(IncompleteReplayInput, match="forbidden fixture key"):
        replay.validate_replay_input(payload)


def test_replay_input_rejects_cross_section_count_mismatch():
    payload = bundle_payload()
    payload["source_snapshot"]["section_counts"]["observations"] += 1

    with pytest.raises(IncompleteReplayInput, match="section count"):
        replay.validate_replay_input(payload)


def test_replay_input_rejects_cross_section_identity_mismatch():
    payload = bundle_payload()
    payload["source_snapshot"]["section_identity_sets"]["memberships"] = ["wrong"]

    with pytest.raises(IncompleteReplayInput, match="section identities"):
        replay.validate_replay_input(payload)


def test_replay_reports_every_metric_with_full_numerator_and_denominator():
    signals = (
        signal("sig_za_1", "za", "cluster_a", "za|entity|known za"),
        signal(
            "sig_za_2",
            "za",
            "cluster_a",
            "za|entity|other",
            membership_complete=False,
            evidence_ready=False,
            geo_proven=False,
        ),
        signal("sig_ng_1", "ng", "cluster_b", "za|entity|foreign"),
        signal("sig_ke_1", "ke", "cluster_c", "ke|entity|known ke", evidence_ready=False),
    )

    metrics = evaluate_replay(
        cutoff=CUTOFF,
        input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
        signals=signals,
        known_events=known_events(),
        sample_per_market=1,
    )

    assert metrics.row_count == EXPECTED_EVENT_LEDGER_ROWS
    assert metrics.known_event_recall.as_tuple() == (2, 3, 2 / 3)
    assert {
        market: metric.as_tuple() for market, metric in metrics.known_event_recall_by_market.items()
    } == {
        "ke": (1, 1, 1.0),
        "ng": (0, 1, 0.0),
        "za": (1, 1, 1.0),
    }
    assert metrics.duplicate_rate.as_tuple() == (1, 4, 0.25)
    assert metrics.foreign_leakage.as_tuple() == (1, 4, 0.25)
    assert metrics.receipt_completeness.as_tuple() == (3, 4, 0.75)
    assert metrics.evidence_coverage.as_tuple() == (2, 4, 0.5)
    assert metrics.geo_coverage.as_tuple() == (3, 4, 0.75)
    assert tuple(item.market for item in metrics.review_sample) == ("ke", "ng", "za")
    assert metrics.human_coherence_status == "pending"


@pytest.mark.parametrize(
    "row_count", [0, EXPECTED_EVENT_LEDGER_ROWS - 1, EXPECTED_EVENT_LEDGER_ROWS + 1]
)
def test_certified_replay_rejects_empty_or_nonexact_input(row_count):
    with pytest.raises(IncompleteReplayInput, match="18693"):
        evaluate_replay(
            cutoff=CUTOFF,
            input_row_count=row_count,
            signals=(signal("sig", "za", "cluster", "za|entity|known za"),),
            known_events=known_events(),
        )


def test_future_source_timestamp_is_rejected_before_metrics():
    with pytest.raises(FutureLeakageDetected, match="sig_future"):
        evaluate_replay(
            cutoff=CUTOFF,
            input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
            signals=(
                signal(
                    "sig_future",
                    "za",
                    "cluster",
                    "za|entity|known za",
                    source_time="2026-08-26T00:00:00Z",
                ),
            ),
            known_events=known_events(),
        )


def test_replay_metrics_and_review_sample_ignore_input_order():
    signals = (
        signal("sig_za", "za", "a", "za|entity|known za"),
        signal("sig_ng", "ng", "b", "ng|entity|known ng"),
        signal("sig_ke", "ke", "c", "ke|entity|known ke"),
    )
    forward = evaluate_replay(
        cutoff=CUTOFF,
        input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
        signals=signals,
        known_events=known_events(),
        sample_per_market=1,
    )
    reverse = evaluate_replay(
        cutoff=CUTOFF,
        input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
        signals=tuple(reversed(signals)),
        known_events=tuple(reversed(known_events())),
        sample_per_market=1,
    )
    assert forward == reverse


def test_known_event_recall_never_crosses_market():
    metrics = evaluate_replay(
        cutoff=CUTOFF,
        input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
        signals=(
            signal("sig_za_other", "za", "a", "za|entity|other"),
            signal("sig_ng_foreign", "ng", "b", "za|entity|known za"),
            signal("sig_ke_other", "ke", "c", "ke|entity|other"),
        ),
        known_events=(
            KnownEvent("known_za", "za", ("za|entity|known za",)),
            KnownEvent("known_ng", "ng", ("ng|entity|known ng",)),
            KnownEvent("known_ke", "ke", ("ke|entity|known ke",)),
        ),
        sample_per_market=1,
    )
    assert metrics.known_event_recall.as_tuple() == (0, 3, 0.0)
    assert metrics.foreign_leakage.as_tuple() == (1, 3, 1 / 3)


def test_certified_replay_requires_a_signal_in_every_market():
    with pytest.raises(IncompleteReplayInput, match="every market"):
        evaluate_replay(
            cutoff=CUTOFF,
            input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
            signals=(
                signal("sig_za", "za", "a", "za|entity|known za"),
                signal("sig_ng", "ng", "b", "ng|entity|known ng"),
            ),
            known_events=known_events(),
        )


def test_certified_replay_requires_known_events_in_every_market():
    with pytest.raises(IncompleteReplayInput, match="known events in every market"):
        evaluate_replay(
            cutoff=CUTOFF,
            input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
            signals=(
                signal("sig_za", "za", "a", "za|entity|known za"),
                signal("sig_ng", "ng", "b", "ng|entity|known ng"),
                signal("sig_ke", "ke", "c", "ke|entity|known ke"),
            ),
            known_events=(KnownEvent("known_za", "za", ("za|entity|known za",)),),
            sample_per_market=1,
        )


def test_review_sample_requires_declared_count_in_every_market():
    with pytest.raises(IncompleteReplayInput, match="review sample requires 5"):
        evaluate_replay(
            cutoff=CUTOFF,
            input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
            signals=(
                signal("sig_za", "za", "a", "za|entity|known za"),
                signal("sig_ng", "ng", "b", "ng|entity|known ng"),
                signal("sig_ke", "ke", "c", "ke|entity|known ke"),
            ),
            known_events=known_events(),
        )


def test_duplicate_signal_id_is_rejected_before_sampling():
    with pytest.raises(IncompleteReplayInput, match="duplicate signal ID"):
        evaluate_replay(
            cutoff=CUTOFF,
            input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
            signals=(
                signal("duplicate", "za", "a", "za|entity|known za"),
                signal("duplicate", "ng", "b", "ng|entity|known ng"),
                signal("sig_ke", "ke", "c", "ke|entity|known ke"),
            ),
            known_events=known_events(),
            sample_per_market=1,
        )


def test_dry_run_names_bounds_and_never_claims_certification():
    payload = json.loads(render_dry_run())
    assert payload == {
        "mode": "dry_run",
        "cutoff": "2026-08-25",
        "expected_event_ledger_rows": 18693,
        "query_limit": 18694,
        "markets": ["ke", "ng", "za"],
        "human_coherence_status": "pending",
        "certified": False,
    }


# --- The apply mode -----------------------------------------------------------
#
# Approved by 42-noncanary-run-release-approval-request-2026-08-29.md sections
# 3.2 and 3.3. One producing run: pipeline with persist off, the bridge, a
# dry run, the apply, an exact readback, and the receipt last, always blocked.
# Partition completeness is proved by count-and-digest match against the copy
# manifest, computed in SQL; a mismatch writes a failed receipt and persists
# no rows.


def _apply_component():
    from dataclasses import replace

    from tests.unit import test_dynamic_signal_rows as fixtures

    return replace(fixtures.component(), label="repair routine")


class _ApplyRecorder:
    """Records every query and persist call in order."""

    def __init__(self, completeness_rows, existing_receipts=0, readback_counts=None):
        self.calls = []
        self._completeness_rows = completeness_rows
        self._existing_receipts = existing_receipts
        self._readback_counts = dict(readback_counts or {})

    def query_runner(self, sql, params=None):
        self.calls.append(("query", sql))
        if "AS row_set_digest" in sql and "SHA256" in sql:
            return [{"row_set_digest": "b" * 64}]
        if "open_intelligence_source_copy_manifest_v1" in sql:
            return list(self._completeness_rows)
        if "FROM receipt_probe" in sql or "run_receipts_v1` WHERE run_id" in sql:
            return [{"existing": self._existing_receipts}]
        if "INSERT INTO" in sql and "run_receipts_v1" in sql:
            return []
        for table, count in self._readback_counts.items():
            if table in sql:
                return [{"row_count": count}]
        return [{"row_count": 0}]

    def persist_runner(self, *, batch, dry_run):
        self.calls.append(("persist", dry_run))
        from types import MappingProxyType as _MP

        counts = {t: len(getattr(batch, t)) for t in replay.persistence.TABLE_BINDINGS}
        return SimpleNamespace(
            dry_run=dry_run,
            validated_counts=_MP(counts),
            inserted_counts=_MP(counts if not dry_run else dict.fromkeys(counts, 0)),
        )


def _retained_provider(client):
    # The retained v1 chain of the r16 run, read under a historical mode against the
    # packaged retained registry.
    return replay._R3ArtifactProvider(
        client,
        version=replay.execution_approval._RESULT_VERSION,
        mode="historical_read",
        generation=execution_generations.active_generation(),
    )


def _v2_apply_authority():
    generation = execution_generations.active_generation()
    origin = next(
        row
        for row in generation.registry.values()
        if row.manifest_version == "open_intelligence_execution_manifest_v2"
        and "r3_apply" in row.operation_bindings
    )
    binding = origin.operation_bindings["r3_apply"]
    image_uri = (
        "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:" + "2" * 64
    )
    return SimpleNamespace(
        operation="r3_apply",
        manifest=SimpleNamespace(
            job_resource=binding.job_resource,
            image_uri=image_uri,
            service_identity=binding.service_identity,
            command=("python",),
            arguments=("scripts/staging/replay_open_intelligence.py",),
            input_artifacts=(("config", "3" * 64),),
        ),
        approval=SimpleNamespace(
            manifest_sha256="d" * 64,
            approval_id="exa_" + "e" * 64,
            approved_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        ),
        execution_name=binding.job_resource + "/executions/r3-execution-fixture",
        job_resource=binding.job_resource,
        image_uri=image_uri,
        source_sha="a" * 40,
        generation=generation,
    )


def _complete_rows():
    return [
        {"source_table": "event_ledger", "manifest_rows": 10, "matched_rows": 10},
        {"source_table": "seed_graph", "manifest_rows": 20, "matched_rows": 20},
    ]


def test_apply_cli_resolves_the_default_persistence_scope(monkeypatch):
    from google.cloud import bigquery
    from scripts.migrations import create_open_intelligence_v2 as migration

    monkeypatch.setattr(migration, "_authorized_credentials", lambda *_args: object())
    client_kwargs = {}

    class FakeJob:
        def __init__(self, rows):
            self.rows = rows

        def result(self, **_kwargs):
            return self.rows

    class FakeClient:
        def query(self, sql, **_kwargs):
            return FakeJob(_complete_rows() if "source_copy_manifest" in sql else [])

    def fake_client(**kwargs):
        client_kwargs.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(bigquery, "Client", fake_client)
    rule_bundle = SimpleNamespace(
        rule_version="composition_rules_v2",
        graph_rules=SimpleNamespace(pair_ceiling=10),
    )
    monkeypatch.setattr(replay, "load_composition_rules_v2", lambda *_args, **_kwargs: rule_bundle)
    result = SimpleNamespace(components=(), missing_work=())
    monkeypatch.setattr(
        replay,
        "run_dynamic_signal_identity_v2",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        replay,
        "collect_component_factor_evidence",
        lambda **_kwargs: {},
    )
    assembled = SimpleNamespace(
        metrics_by_component={},
        receipts_by_component={},
        readiness_by_component={},
        readiness_rules=object(),
        missing_by_component={},
    )
    monkeypatch.setattr(
        replay,
        "assemble_component_inputs",
        lambda **_kwargs: assembled,
    )
    monkeypatch.setattr(
        replay,
        "_load_r3_exposure_authority",
        lambda _runner, **_kwargs: (),
    )
    monkeypatch.setattr(
        replay,
        "_load_r3_exposure_execution_proof",
        lambda _path, **_kwargs: {},
    )
    monkeypatch.setattr(replay, "_load_human_review_receipt", lambda _path: {})
    quality = SimpleNamespace(
        metrics_by_component={},
        receipts_by_component={},
        readiness_by_component={},
        readiness_rules=object(),
        missing_by_component={},
        promotion_results_by_signal_id={},
        quality_failed_by_component={},
        factual_conflict_by_component={},
    )
    quality_calls = []
    monkeypatch.setattr(
        replay,
        "evaluate_r3_quality_authority",
        lambda **kwargs: quality_calls.append(kwargs) or quality,
    )
    captured = {}

    def fake_apply(**kwargs):
        captured["scope"] = kwargs["scope"]
        captured["cluster_build_version"] = kwargs["cluster_build_version"]
        captured["quality_evaluated"] = kwargs["quality_evaluated"]
        captured["promotion_results"] = kwargs["promotion_results_by_signal_id"]
        return SimpleNamespace(
            run_id=kwargs["scope"].run_id,
            receipt=SimpleNamespace(
                status="completed",
                complete_partitions=True,
                display_release_state="blocked",
            ),
            admitted=(),
            skipped=(),
            persisted_counts={},
        )

    monkeypatch.setattr(replay, "apply_open_intelligence_run", fake_apply)
    monkeypatch.setattr(replay, "_read_r3_execution_proof", lambda **_kwargs: object())
    monkeypatch.setattr(
        replay,
        "render_r3_execution_proof",
        lambda _proof: '{"proof":"ok"}\n',
    )
    durable_authority = _v2_apply_authority()
    durable_consumption = SimpleNamespace(consumption_id="exc_" + "f" * 64)
    durable_result = SimpleNamespace(result_id="exr_" + "0" * 64)
    monkeypatch.setattr(
        replay.execution_approval,
        "_load_execution_authority",
        lambda _operation, **_kwargs: durable_authority,
    )
    monkeypatch.setattr(
        replay.execution_approval,
        "_consume_execution_authority",
        lambda _authority: durable_consumption,
    )
    monkeypatch.setattr(
        replay.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: durable_result,
    )

    replay.run_apply_cli(
        [
            "--apply-run",
            "--trend-date",
            "2026-09-03",
            "--run-id",
            "run_20260903_dynamic_apply_v2_r16",
            "--source-sha",
            "a" * 40,
        ]
    )

    assert captured["scope"].client_scope_id == "ogilvy_default"
    assert captured["scope"].market_scope == ("za", "ng", "ke")
    assert captured["scope"].brand_config_id == "ogilvy_default"
    assert captured["scope"].audience_lens_ids == ()
    assert captured["scope"].theme_id == "ogilvy_42"
    assert client_kwargs["project"] == "ogilvy-trends-v2"
    assert client_kwargs["location"] == "US"
    assert captured["cluster_build_version"] == "hybrid_graph_v2"
    assert captured["quality_evaluated"] is True
    assert captured["promotion_results"] == {}
    assert len(quality_calls) == 1


@pytest.mark.parametrize(
    ("run_id", "trend_date"),
    [
        ("run_20260827_dynamic_apply_v2", "2026-09-03"),
        ("run_20260903_dynamic_apply_v2_r16", "2026-09-02"),
    ],
)
def test_r3_apply_refuses_any_other_run_or_date_before_credentials(run_id, trend_date):
    with pytest.raises(replay.ApplyRefusal):
        replay.run_apply_cli(
            [
                "--apply-run",
                "--trend-date",
                trend_date,
                "--run-id",
                run_id,
                "--source-sha",
                "a" * 40,
            ]
        )


def test_r3_apply_consumes_before_full_source_reads_and_records_after_proof():
    source = inspect.getsource(replay._run_apply_cli_impl)
    load = source.index("_load_execution_authority")
    consume = source.index("_consume_execution_authority")
    protected = source.index("run_dynamic_signal_identity_v2")
    record = source.index("_record_execution_result")
    assert load < consume < protected < record
    assert "_require_r3_compiled_authority" not in source
    assert "_require_r3_execution_authority" not in source
    assert "retry=None" in source
    assert "job_retry=None" in source


def test_r3_artifact_context_has_every_manifest_required_name():
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    review = {"review": "ready"}
    exposure = {"proof": "ready"}
    receipts = (
        {
            "source_family": "news",
            "exposure_date": date(2026, 9, 3),
        },
    )
    completeness = [{"source_table": "seed_graph", "manifest_rows": 1, "matched_rows": 1}]
    artifacts = replay._build_r3_execution_artifacts(
        review_receipt=review,
        exposure_proof=exposure,
        exposure_receipts=receipts,
        completeness_rows=completeness,
    )
    assert set(artifacts) == {
        "r3_contract",
        "quality_review_receipt",
        "exposure_execution_proof",
        "exposure_receipt_readback",
        "inserted_natural_key_set",
        "config",
        "source_window_receipt_set",
    }
    assert artifacts["quality_review_receipt"] == canonical_bytes(review)


def test_r3_protected_artifact_provider_reads_only_exact_routines_and_binds_run_id():
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    review = {"run_id": replay.R3_RUN_ID, "review": "ready"}
    exposure = {"run_id": replay.R3_RUN_ID, "proof": "ready"}
    calls = []

    class Job:
        def __init__(self, rows):
            self.rows = rows

        def result(self, **kwargs):
            assert kwargs == {"max_results": 2, "retry": None, "job_retry": None}
            return self.rows

    class Client:
        def query(self, sql, *, location, job_config, retry, job_retry):
            calls.append(sql)
            assert location == "US"
            assert retry is None
            assert job_retry is None
            assert len(job_config.query_parameters) in {1, 2}
            if "quality_review_receipt" in sql:
                return Job(
                    [
                        {
                            "run_id": replay.R3_RUN_ID,
                            "canonical_review_receipt_json": canonical_bytes(review).decode(),
                        }
                    ]
                )
            return Job(
                [
                    {
                        "status": "succeeded",
                        "execution_name": (
                            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
                            "trends-engine-oi-exposure-issuer-staging/executions/exe-1"
                        ),
                        "canonical_result_json": canonical_bytes(exposure).decode(),
                        "result_digest": hashlib.sha256(canonical_bytes(exposure)).hexdigest(),
                    }
                ]
            )

    provider = _retained_provider(Client())
    assert provider._quality_review() == review
    assert provider._exposure_result() == exposure
    assert "sp_read_open_intelligence_quality_review_receipt_v1" in calls[0]
    assert "sp_read_open_intelligence_execution_result_chain_v1" in calls[1]
    assert all("SELECT * FROM" not in sql for sql in calls)


def test_r3_protected_path_is_script_only_and_consumes_before_producing_reads():
    source = inspect.getsource(replay._run_apply_cli_impl)
    provider_source = inspect.getsource(replay._R3ArtifactProvider._build)
    protected = source[
        source.index("if protected:") : source.index("else:", source.index("if protected:"))
    ]
    assert "_R3ArtifactProvider" in source
    assert "artifact_reader=provider.read" in source
    assert source.index("_load_execution_authority") < source.index("_consume_execution_authority")
    assert source.index("_consume_execution_authority") < source.index(
        "run_dynamic_signal_identity_v2"
    )
    assert "_load_human_review_receipt" not in protected
    assert "_load_r3_exposure_execution_proof" not in protected
    assert "build_partition_completeness_query" not in provider_source
    assert "open_intelligence_source_copy_receipts_v1" in provider_source
    assert source.index("_consume_execution_authority") < source.index(
        "build_partition_completeness_query"
    )
    assert source.index("_terminal_context.extend") < source.index(
        "build_partition_completeness_query"
    )


def test_r3_post_consumption_failure_records_one_sanitized_terminal_result(monkeypatch):
    generation = execution_generations.active_generation()
    authority = SimpleNamespace(
        approval=SimpleNamespace(
            manifest_sha256="a" * 64,
            approval_id="exa_" + "b" * 64,
        ),
        generation=generation,
    )
    consumption = SimpleNamespace(
        consumption_id="exc_" + "c" * 64,
        origin_registry_sha256=generation.origin_registry_sha256,
        resource_manifest_sha256=generation.resource_manifest_sha256,
    )
    recorded = []

    def fail_after_consumption(_argv, *, _terminal_context):
        _terminal_context.extend((authority, consumption))
        raise RuntimeError("private scoring detail")

    monkeypatch.setattr(replay, "_run_apply_cli_impl", fail_after_consumption)
    monkeypatch.setattr(
        replay.execution_approval,
        "_record_execution_result",
        lambda *args, **kwargs: (
            recorded.append((args, kwargs)) or SimpleNamespace(result_id="exr_" + "d" * 64)
        ),
    )

    with pytest.raises(RuntimeError, match="private scoring detail"):
        replay.run_apply_cli([])

    assert len(recorded) == 1
    args = recorded[0][0]
    payload = json.loads(args[3])
    assert args[5] == "failed"
    assert payload == {
        "error_code": "r3_apply_failed",
        "run_id": replay.R3_RUN_ID,
        "status": "failed",
    }
    assert "private scoring detail" not in args[3]


def test_r3_unresolved_result_write_never_substitutes_a_failed_payload(monkeypatch):
    authority = SimpleNamespace()
    consumption = SimpleNamespace()

    def unresolved(_argv, *, _terminal_context):
        _terminal_context.extend((authority, consumption))
        raise replay.execution_approval.ApprovalRefusal("execution_result_unresolved")

    monkeypatch.setattr(replay, "_run_apply_cli_impl", unresolved)
    monkeypatch.setattr(
        replay.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: pytest.fail("fallback result reached"),
    )
    with pytest.raises(replay.execution_approval.ApprovalRefusal, match="unresolved"):
        replay.run_apply_cli([])


def test_r3_apply_refuses_the_unchecked_quality_fallback_before_any_query():
    from tests.unit import test_dynamic_signal_rows as fixtures

    scope = replace(fixtures.scope(), run_id=replay.R3_RUN_ID)
    result = SimpleNamespace(
        components=(),
        error_state=None,
        run_id=scope.run_id,
        persistence_result=None,
        missing_work=(),
    )
    calls = []
    with pytest.raises(replay.ApplyRefusal, match="quality authority"):
        replay.apply_open_intelligence_run(
            result=result,
            scope=scope,
            signal_date=replay.R3_SIGNAL_DATE,
            observation_start=date(2026, 8, 21),
            created_at=datetime(2026, 8, 30, tzinfo=UTC),
            project="ogilvy-trends-v2",
            dataset="trends_v2_staging",
            rule_version="composition_rules_v2",
            cluster_build_version="hybrid_graph_v2",
            source_sha="a" * 40,
            query_runner=lambda sql: calls.append(sql),
            persist_runner=lambda **_kwargs: pytest.fail("unchecked R3 attempted persistence"),
        )
    assert calls == []


def test_r3_execution_proof_is_canonical_and_binds_exact_applied_readback():
    import io

    from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
    from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest

    receipt = build_run_receipt(
        run_contract_version="open_intelligence_run_receipt_v1",
        run_id=replay.R3_RUN_ID,
        client_scope_id="ogilvy_default",
        market_scope=("za", "ng", "ke"),
        signal_date=replay.R3_SIGNAL_DATE,
        observation_start=date(2026, 8, 21),
        observation_end=replay.R3_SIGNAL_DATE,
        observation_method="dynamic_source_copy_apply_v2",
        source_window_digest="1" * 64,
        cluster_build_version="hybrid_graph_v2",
        source_family_map_version="channel_family_v1",
        rule_version="composition_rules_v2",
        status="completed",
        complete_partitions=True,
        display_release_state="blocked",
        candidate_count=1,
        evidence_count=2,
        membership_count=1,
        lineage_count=0,
        analysis_count=0,
        prediction_count=1,
        row_set_digest="2" * 64,
        source_sha="a" * 40,
        completed_at=datetime(2026, 8, 30, 10, tzinfo=UTC),
    )
    counts = {
        "candidates": 1,
        "evidence": 2,
        "membership": 1,
        "lineage": 0,
        "predictions": 1,
        "outcomes": 0,
        "analysis": 0,
    }
    authority = {
        "execution_name": "projects/p/locations/r/jobs/r3/executions/exe-1",
        "job_resource": "projects/p/locations/r/jobs/r3",
        "image_digest": "sha256:" + "3" * 64,
        "source_sha": receipt.source_sha,
        "service_identity": "fixture@example.invalid",
        "command": "python scripts/staging/replay_open_intelligence.py",
        "args": ("--apply-run",),
        "config_digest": "4" * 64,
        "started_at": datetime(2026, 8, 30, 9, tzinfo=UTC),
        "completed_at": datetime(2026, 8, 30, 10, 5, tzinfo=UTC),
        "status": "succeeded",
    }
    proof = replay.build_r3_execution_proof(
        pre_execution_addendum_sha256="5" * 64,
        authority=authority,
        applied_receipt=receipt,
        readback_receipt=receipt,
        persisted_counts=counts,
        readback_counts=counts,
    )
    rendered = replay.render_r3_execution_proof(proof)

    assert proof.execution_proof_contract_version == "r3-execution-proof-v1"
    assert proof.run_receipt_digest == run_receipt_digest(receipt)
    assert proof.row_set_digest == receipt.row_set_digest
    assert proof.completed_at == authority["completed_at"]
    assert proof.status == "succeeded"
    assert rendered == canonical_bytes(proof).decode("utf-8") + "\n"
    assert canonical_digest(proof) == canonical_digest(__import__("json").loads(rendered))
    stdout = io.StringIO()
    replay.write_r3_execution_proof(proof, stdout=stdout)
    assert stdout.getvalue() == rendered
    assert len(stdout.getvalue().splitlines(keepends=True)) == 1

    with pytest.raises(replay.ApplyRefusal, match="readback"):
        replay.build_r3_execution_proof(
            pre_execution_addendum_sha256="5" * 64,
            authority=authority,
            applied_receipt=receipt,
            readback_receipt=replace(receipt, row_set_digest="9" * 64),
            persisted_counts=counts,
            readback_counts=counts,
        )
    with pytest.raises(replay.ApplyRefusal, match="counts"):
        replay.build_r3_execution_proof(
            pre_execution_addendum_sha256="5" * 64,
            authority=authority,
            applied_receipt=receipt,
            readback_receipt=receipt,
            persisted_counts=counts,
            readback_counts={**counts, "evidence": 1},
        )

    running_authority = {**authority, "completed_at": None, "status": "running"}
    with pytest.raises(replay.ApplyRefusal, match="immutable Execution completion"):
        replay.build_r3_execution_proof(
            pre_execution_addendum_sha256="5" * 64,
            authority=running_authority,
            applied_receipt=receipt,
            readback_receipt=receipt,
            persisted_counts=counts,
            readback_counts=counts,
        )


def test_producing_persistence_uses_a_descriptor_then_the_real_client(monkeypatch):
    calls = []

    def fake_persist(**kwargs):
        calls.append(kwargs)
        return kwargs["client"]

    monkeypatch.setattr(
        replay.persistence,
        "persist_open_intelligence_rows",
        fake_persist,
    )
    real_client = object()
    batch = object()
    rule_bundle = object()

    dry_target = replay.persist_producing_batch(
        client=real_client,
        batch=batch,
        rule_bundle=rule_bundle,
        dry_run=True,
    )
    applied_target = replay.persist_producing_batch(
        client=real_client,
        batch=batch,
        rule_bundle=rule_bundle,
        dry_run=False,
    )

    assert dry_target == replay.persistence.PersistenceTarget(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        location="US",
        writer_identity="trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    )
    assert applied_target is real_client
    assert [call["dry_run"] for call in calls] == [True, False]
    assert all(call["batch"] is batch for call in calls)
    assert all(call["rule_bundle"] is rule_bundle for call in calls)


def _apply(recorder=None, scope_run_id=None, **overrides):
    from dataclasses import replace as _replace

    from tests.unit import test_dynamic_signal_rows as fixtures

    recorder = recorder or _ApplyRecorder(_complete_rows())
    component = _apply_component()
    from src.analysis.open_intelligence.pipeline import MembershipReceipt

    membership = MembershipReceipt(
        member_id="mem_" + "1" * 64,
        member_identity="za|keyword|repair routine",
        candidate_type="keyword",
        canonical_value="repair routine",
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        row_id="fixture_row_001",
        qualifies_evidence=False,
    )
    scope = fixtures.scope()
    if scope_run_id is not None:
        scope = _replace(scope, run_id=scope_run_id)
    result = SimpleNamespace(
        components=(component,),
        error_state=None,
        run_id=scope.run_id,
        persistence_result=None,
        missing_work=("metric_provider_unavailable",),
        evidence_projection=SimpleNamespace(
            memberships_by_component={"projection_component": (membership,)}
        ),
    )
    values = {
        "result": result,
        "scope": scope,
        "signal_date": fixtures.SIGNAL_DATE,
        "observation_start": date(2026, 8, 21),
        "created_at": fixtures.CREATED_AT,
        "project": "ogilvy-trends-v2",
        "dataset": "trends_v2_staging",
        "rule_version": "rules_v1",
        "cluster_build_version": component.build_version,
        "source_sha": "a" * 40,
        "query_runner": recorder.query_runner,
        "persist_runner": recorder.persist_runner,
    }
    values.update(overrides)
    return recorder, replay.apply_open_intelligence_run(**values)


def test_apply_skips_every_component_with_the_registry_reasons():
    _recorder, report = _apply()
    assert report.admitted == ()
    assert len(report.skipped) == 1
    reasons = report.skipped[0][1]
    assert "velocity_formula_unapproved" in reasons
    assert "breadth_formula_unapproved" in reasons


def test_apply_writes_the_receipt_last_and_blocked():
    recorder, report = _apply()
    insert_positions = [
        i
        for i, (k, v) in enumerate(recorder.calls)
        if k == "query" and "INSERT INTO" in v and "run_receipts_v1" in v
    ]
    assert len(insert_positions) == 1
    # The receipt is the last act, after every readback.
    assert insert_positions[0] == len(recorder.calls) - 1
    assert report.receipt.display_release_state == "blocked"
    assert report.receipt.status == "completed"
    assert report.receipt.complete_partitions is True


def test_apply_receipt_uses_the_actual_component_build_version():
    from dataclasses import replace as _replace

    from src.analysis.open_intelligence.graph import ANCHORED_CLUSTER_BUILD_VERSION

    from tests.unit import test_dynamic_signal_rows as fixtures

    component = _replace(
        _apply_component(),
        build_version=ANCHORED_CLUSTER_BUILD_VERSION,
    )
    scope = fixtures.scope()
    result = SimpleNamespace(
        components=(component,),
        error_state=None,
        run_id=scope.run_id,
        persistence_result=None,
        missing_work=(),
    )

    _recorder, report = _apply(
        result=result,
        cluster_build_version=ANCHORED_CLUSTER_BUILD_VERSION,
    )

    assert report.receipt.cluster_build_version == ANCHORED_CLUSTER_BUILD_VERSION


def test_apply_refuses_a_component_build_version_mismatch():
    from src.analysis.open_intelligence.graph import ANCHORED_CLUSTER_BUILD_VERSION

    recorder = _ApplyRecorder(_complete_rows())
    with pytest.raises(replay.ApplyRefusal, match="cluster build version"):
        _apply(
            recorder,
            cluster_build_version=ANCHORED_CLUSTER_BUILD_VERSION,
        )
    assert recorder.calls == []


def test_apply_receipt_carries_the_full_closed_source_window():
    _recorder, report = _apply()
    assert report.receipt.observation_start == date(2026, 8, 21)
    assert report.receipt.observation_end == report.receipt.signal_date


def test_apply_runs_a_dry_run_before_the_write():
    """A nonempty batch is dry-run before the write. An empty batch has
    nothing to write, so it skips persist and proves the zero-row state by
    reading the target back instead; no certified rule bundle exists yet
    and a zero-row run does not need one."""
    from tests.unit import test_dynamic_signal_rows as fixtures

    component = _apply_component()
    key = replay.persistence.component_bridge_key(component)
    recorder, _unused = _apply(
        metrics_by_component={key: fixtures.metrics()},
        receipts_by_component={key: fixtures.receipts()},
        readiness_by_component={key: fixtures.readiness()},
        readiness_rules=fixtures.readiness_rules(),
        quality_evaluated=True,
    )
    persist_flags = [flag for kind, flag in recorder.calls if kind == "persist"]
    assert persist_flags == [True, False]


def test_an_empty_batch_skips_persist_and_reads_back_the_zero_row_state():
    recorder, report = _apply()
    assert all(kind != "persist" for kind, _ in recorder.calls)
    readbacks = [
        sql for kind, sql in recorder.calls if kind == "query" and "COUNT(*) AS row_count" in sql
    ]
    assert len(readbacks) == 6
    assert all(value == 0 for value in report.persisted_counts.values())


def test_a_dirty_target_for_the_run_refuses_the_zero_row_receipt():
    recorder = _ApplyRecorder(_complete_rows(), readback_counts={"signal_candidates_v2": 3})
    with pytest.raises(replay.ApplyRefusal):
        _apply(recorder)
    assert not any(
        kind == "query" and "INSERT INTO" in sql and "run_receipts_v1" in sql
        for kind, sql in recorder.calls
    )


def test_a_completeness_mismatch_writes_a_failed_receipt_and_persists_nothing():
    rows = _complete_rows()
    rows[0]["matched_rows"] = 9
    recorder = _ApplyRecorder(rows)
    recorder, report = _apply(recorder)
    assert report.receipt.status == "failed"
    assert report.receipt.complete_partitions is False
    assert all(kind != "persist" for kind, _ in recorder.calls)


def test_an_existing_receipt_for_the_run_refuses_the_apply():
    recorder = _ApplyRecorder(_complete_rows(), existing_receipts=1)
    with pytest.raises(replay.ApplyRefusal):
        _apply(recorder)


def test_a_result_with_a_persistence_result_refuses_the_apply():
    from tests.unit import test_dynamic_signal_rows as fixtures

    scope = fixtures.scope()
    result = SimpleNamespace(
        components=(),
        error_state=None,
        run_id=scope.run_id,
        persistence_result=object(),
        missing_work=(),
    )
    with pytest.raises(replay.ApplyRefusal):
        _apply(result=result)


def test_a_readback_mismatch_refuses_before_any_receipt():
    from tests.unit import test_dynamic_signal_rows as fixtures

    class _LyingRecorder(_ApplyRecorder):
        def persist_runner(self, *, batch, dry_run):
            self.calls.append(("persist", dry_run))
            from types import MappingProxyType as _MP

            counts = {t: len(getattr(batch, t)) for t in replay.persistence.TABLE_BINDINGS}
            # The write claims one more candidate than the batch holds.
            claimed = dict(counts)
            claimed["candidates"] = counts["candidates"] + 1
            return SimpleNamespace(
                dry_run=dry_run,
                validated_counts=_MP(counts),
                inserted_counts=_MP(claimed if not dry_run else dict.fromkeys(counts, 0)),
            )

    recorder = _LyingRecorder(_complete_rows())
    component = _apply_component()
    key = replay.persistence.component_bridge_key(component)
    with pytest.raises(replay.ApplyRefusal):
        _apply(
            recorder,
            metrics_by_component={key: fixtures.metrics()},
            receipts_by_component={key: fixtures.receipts()},
            readiness_by_component={key: fixtures.readiness()},
            readiness_rules=fixtures.readiness_rules(),
            quality_evaluated=True,
        )
    assert not any(
        kind == "query" and "INSERT INTO" in sql and "run_receipts_v1" in sql
        for kind, sql in recorder.calls
    )


def test_the_completeness_query_names_manifest_and_every_copied_table():
    sql = replay.build_partition_completeness_query(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        copy_run_id="dynamic_replay_source_copy_20260821_20260903_v1",
    )
    assert "open_intelligence_source_copy_manifest_v1" in sql
    for table in ("event_ledger", "seed_graph", "enriched_content"):
        assert table in sql


def test_completeness_is_proven_only_by_exact_match_on_nonzero_rows():
    assert replay.partition_completeness_proven(_complete_rows()) is True
    short = _complete_rows()
    short[1]["matched_rows"] = 19
    assert replay.partition_completeness_proven(short) is False
    empty = [
        {"source_table": "event_ledger", "manifest_rows": 0, "matched_rows": 0},
    ]
    # A manifest with no rows proves nothing; vacuous truth is not proof.
    assert replay.partition_completeness_proven(empty) is False


def test_a_run_identity_with_hostile_characters_refuses_the_apply():
    """Found by independent review: run_id was interpolated raw into the
    duplicate guard and readback queries while the insert escaped it, so the
    guard checked a different identity than the one stored. The identity is
    now validated against a strict charset before any query exists."""
    from tests.unit import test_dynamic_signal_rows as fixtures

    for hostile in ("a'--", "x' OR '1'='1", "run id", "run`id", ""):
        recorder = _ApplyRecorder(_complete_rows())
        with pytest.raises(replay.ApplyRefusal):
            _apply(recorder, scope_run_id=hostile)
        assert recorder.calls == [], f"a query ran for run_id {hostile!r}"


def test_a_failed_receipt_counts_zero_rows_because_zero_were_written():
    """Found by independent review: a failed run carried the in-memory batch
    counts while writing nothing, so the receipt claimed rows the tables do
    not hold. A failed receipt derives its counts from what was written."""
    from tests.unit import test_dynamic_signal_rows as fixtures

    rows = _complete_rows()
    rows[0]["matched_rows"] = 9
    component = _apply_component()
    key = replay.persistence.component_bridge_key(component)
    recorder = _ApplyRecorder(rows)
    recorder, report = _apply(
        recorder,
        metrics_by_component={key: fixtures.metrics()},
        receipts_by_component={key: fixtures.receipts()},
        readiness_by_component={key: fixtures.readiness()},
        readiness_rules=fixtures.readiness_rules(),
        quality_evaluated=True,
    )
    assert report.receipt.status == "failed"
    assert report.receipt.candidate_count == 0
    assert report.receipt.evidence_count == 0


# --- Composition authority ----------------------------------------------------
#
# Approved by 42-composition-authority-approval-request-2026-08-29.md:
# lexical_similarity_v1 as the deterministic semantic provider and
# composition_rules_v1 as a replay-certified rule bundle recorded in configs.


def test_the_certified_composition_rules_load_with_their_graph_values():
    from datetime import UTC, datetime

    bundle = replay.load_composition_rules(
        replay.COMPOSITION_RULES_PATH, datetime(2026, 8, 30, tzinfo=UTC)
    )
    assert bundle.status == "replay_certified"
    assert bundle.rule_version == "composition_rules_v1"
    assert bundle.approved_by == "Albert"
    assert bundle.graph_rules.semantic_vote_floor == 0.5
    assert bundle.graph_rules.component_similarity_floor == 0.5
    assert bundle.graph_rules.temporal_overlap_days == 2


def test_an_uncertified_or_expired_rule_document_refuses(tmp_path):
    from datetime import UTC, datetime

    import yaml as _yaml

    now = datetime(2026, 8, 30, tzinfo=UTC)
    base = _yaml.safe_load(Path(replay.COMPOSITION_RULES_PATH).read_text(encoding="utf-8"))
    for mutation in (
        {"status": "uncertified"},
        {"approved_by": ""},
        {"expires_at": "2026-08-29T00:00:00+00:00"},
        {"graph_rules": None},
    ):
        document = dict(base)
        document.update(mutation)
        path = tmp_path / "rules.yaml"
        path.write_text(_yaml.safe_dump(document), encoding="utf-8")
        with pytest.raises(replay.ApplyRefusal):
            replay.load_composition_rules(path, now)


def test_the_lexical_provider_is_deterministic_and_bounded():
    from src.analysis.open_intelligence.candidates import Observation

    provider = replay.LexicalSimilarityProvider()
    assert provider.version == "lexical_similarity_v1"
    a = Observation(
        market="za",
        term="repair cafe",
        candidate_type="keyword",
        row_ids=("r1",),
        platforms=("reddit",),
        source_families=("reddit",),
        aliases=("fix it",),
        topic_tags=("repair",),
    )
    b = Observation(
        market="za",
        term="repair meetup",
        candidate_type="keyword",
        row_ids=("r2",),
        platforms=("youtube",),
        source_families=("youtube",),
        aliases=(),
        topic_tags=("repair",),
    )
    first = provider.similarities((a, b))
    second = provider.similarities((a, b))
    assert first == second
    assert len(first) == 1
    score = next(iter(first.values()))
    assert 0.0 < score < 1.0


def test_the_v2_lexical_provider_scores_only_shared_token_candidates():
    from src.analysis.open_intelligence.candidates import Observation

    alpha = Observation(
        market="za",
        term="repair circle",
        candidate_type="keyword",
        row_ids=("r1",),
        platforms=("reddit",),
        source_families=("reddit",),
    )
    beta = Observation(
        market="za",
        term="repair club",
        candidate_type="keyword",
        row_ids=("r2",),
        platforms=("youtube",),
        source_families=("youtube",),
    )
    unrelated = Observation(
        market="za",
        term="night market",
        candidate_type="keyword",
        row_ids=("r3",),
        platforms=("news",),
        source_families=("news",),
    )
    provider = replay.LexicalSimilarityProviderV2(pair_ceiling=10)

    pairs = provider.candidate_pairs((unrelated, beta, alpha))
    scores = provider.similarities((unrelated, beta, alpha), pairs)

    expected_pair = (
        "za|keyword|repair circle",
        "za|keyword|repair club",
    )
    assert pairs == (expected_pair,)
    assert (
        scores[expected_pair]
        == replay.LexicalSimilarityProvider().similarities((alpha, beta))[expected_pair]
    )


def test_the_v2_lexical_provider_refuses_before_crossing_its_pair_ceiling():
    from src.analysis.open_intelligence.candidates import Observation

    observations = tuple(
        Observation(
            market="za",
            term=f"repair {suffix}",
            candidate_type="keyword",
            row_ids=(f"r{index}",),
            platforms=("reddit",),
            source_families=("reddit",),
        )
        for index, suffix in enumerate(("one", "two", "three"), 1)
    )

    with pytest.raises(ValueError, match="composition_pair_ceiling_exceeded"):
        replay.LexicalSimilarityProviderV2(pair_ceiling=2).candidate_pairs(observations)


def test_v2_candidate_pairs_never_use_taxonomy_tags_as_identity_anchors():
    from src.analysis.open_intelligence.candidates import Observation

    left = Observation(
        market="ke",
        term="repair circle",
        candidate_type="keyword",
        row_ids=("r1",),
        source_families=("reddit",),
        platforms=("reddit",),
        topic_tags=("food",),
    )
    right = Observation(
        market="ke",
        term="night market",
        candidate_type="keyword",
        row_ids=("r2",),
        source_families=("news",),
        platforms=("news",),
        topic_tags=("food",),
    )

    assert replay.LexicalSimilarityProviderV2().candidate_pairs((left, right)) == ()


def test_v2_composition_rules_are_bound_to_the_successful_live_probe():
    bundle = replay.load_composition_rules_v2(
        replay.COMPOSITION_RULES_V2_PATH,
        datetime(2026, 8, 30, 10, tzinfo=UTC),
        require_certified=True,
    )
    assert bundle.status == "replay_certified"
    assert bundle.rule_version == "composition_rules_v2"
    assert bundle.replay_receipt_id == (
        "composition_v2_probe_afaeb3ff6fc22668fdba754fc9224bd2bf055ca882bab08525093c473fa114b0"
    )
    assert bundle.graph_rules.semantic_anchor_floor == 0.5
    assert bundle.graph_rules.temporal_overlap_days == 2
    assert bundle.graph_rules.pair_ceiling == 250_000
    assert bundle.graph_rules.component_member_ceiling == 50


def test_v2_pipeline_runner_selects_anchored_composition_and_restores_it(monkeypatch):
    from src.analysis.open_intelligence import pipeline

    original = pipeline.compose_components
    original_rules = pipeline.GraphRules
    anchored = object()
    observed = []

    monkeypatch.setattr(replay, "build_components_with_anchored_semantics", anchored)

    def fake_run(*args, **kwargs):
        observed.append((args, kwargs, pipeline.compose_components, pipeline.GraphRules))
        return "result"

    monkeypatch.setattr(pipeline, "run_dynamic_signal_identity", fake_run)

    assert replay.run_dynamic_signal_identity_v2("arg", option="value") == "result"
    assert observed == [(("arg",), {"option": "value"}, anchored, replay.AnchoredGraphRules)]
    assert pipeline.compose_components is original
    assert pipeline.GraphRules is original_rules


def test_composition_probe_invariants_measure_live_component_and_edge_failures():
    from src.analysis.open_intelligence.graph import (
        GraphEdge,
        RelationshipVotes,
        SignalComponent,
    )

    valid = SignalComponent(
        market="za",
        member_identities=("za|keyword|alpha", "za|keyword|beta"),
        terms=("alpha", "beta"),
        row_receipts=("r1", "r2"),
        source_families=("news", "search"),
        platforms=("web", "search"),
        creator_ids=(),
        label="alpha",
        label_member_identity="za|keyword|alpha",
    )
    cross_market = SignalComponent(
        market="za",
        member_identities=("za|keyword|alpha", "ng|keyword|beta"),
        terms=("alpha", "beta"),
        row_receipts=("r1", "r2"),
        source_families=("news", "search"),
        platforms=("web", "search"),
        creator_ids=(),
        label="alpha",
        label_member_identity="za|keyword|alpha",
    )
    oversized = SignalComponent(
        market="ke",
        member_identities=tuple(f"ke|keyword|item_{index}" for index in range(51)),
        terms=tuple(f"item_{index}" for index in range(51)),
        row_receipts=tuple(f"r{index}" for index in range(51)),
        source_families=("news", "search"),
        platforms=("web", "search"),
        creator_ids=(),
        label="item_0",
        label_member_identity="ke|keyword|item_0",
    )
    unanchored = GraphEdge(
        market="za",
        left_identity="za|keyword|alpha",
        right_identity="za|keyword|beta",
        votes=RelationshipVotes(
            temporal_overlap=True,
            independent_source_family=True,
        ),
        semantic_similarity=0.0,
    )

    assert replay.composition_probe_invariants(
        (valid, cross_market, oversized),
        (unanchored,),
        member_ceiling=50,
    ) == {
        "max_component_member_count": 51,
        "oversized_component_count": 1,
        "cross_market_component_count": 1,
        "unanchored_edge_count": 1,
    }


# --- Factor evidence collection and component assembly ------------------------
#
# Approved by the raw-factor formula request: factors derive from citing
# evidence in the copied staging tables through one bounded query, and a
# component is admitted only when its metrics, receipts and readiness all
# assemble; every other component is skipped with the derivation's own named
# reasons, never a silent drop.


def _enriched_row(**over):
    row = {
        "term": "arsenal",
        "query_term": "arsenal",
        "title": "",
        "text": "",
        "market": "ke",
        "day": date(2026, 9, 3),
        "id": "enr_1",
        "platform": "youtube",
        "author_handle_norm": "@kearsenal",
        "url": "https://evidence.invalid/1",
        "published_at": datetime(2026, 9, 3, 9, tzinfo=UTC),
        "regional_score": 0.9,
    }
    row.update(over)
    return row


def _factor_component():
    return SignalComponent(
        market="ke",
        member_identities=("ke|keyword|arsenal",),
        terms=("arsenal",),
        row_receipts=("enr_1",),
        source_families=("youtube",),
        platforms=("youtube",),
        creator_ids=(),
        label="Arsenal watch parties",
        label_member_identity="ke|keyword|arsenal",
    )


def test_the_collector_builds_per_component_unit_sets():
    component = _factor_component()
    rows = [
        _enriched_row(),
        _enriched_row(id="enr_2", day=date(2026, 8, 20), author_handle_norm="@other"),
        _enriched_row(id="enr_x", term="unrelated", query_term="unrelated"),
    ]
    calls = []

    def query_runner(sql, params=None):
        calls.append(sql)
        if "GROUP BY market, platform" in sql:
            return [{"market": "ke", "platform": "youtube"}, {"market": "ke", "platform": "reddit"}]
        return [dict(r) for r in rows if r["term"] == "arsenal"]

    evidence = replay.collect_component_factor_evidence(
        query_runner=query_runner,
        components=(component,),
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        trend_date=date(2026, 9, 3),
        window_start=date(2026, 8, 21),
    )
    key = replay.persistence.component_bridge_key(component)
    item = evidence[key]
    assert item.current_rows == ("enr_1",)
    assert set(item.window_rows) == {"enr_1", "enr_2"}
    assert item.families_current == ("youtube",)
    assert set(item.family_universe) == {"reddit", "youtube"}
    assert item.creators_current == ("@kearsenal",)
    assert item.clean_current == ("enr_1",)
    assert item.geo_confirmed_current == ("enr_1",)
    # Ruled 4 Sep 2026: the best citing post over the window carries the geo score.
    assert item.geo_best_regional_score == 0.9
    citing_sql = next(sql for sql in calls if "GROUP BY market, platform" not in sql)
    assert "JOIN term_patterns" in citing_sql
    assert "patterns.term AS term" in citing_sql
    assert " AS title" not in citing_sql
    assert " AS text" not in citing_sql


def test_a_row_without_provenance_counts_against_geo_and_integrity():
    component = _factor_component()
    rows = [
        _enriched_row(),
        _enriched_row(id="enr_3", url=None, regional_score=None),
    ]

    def query_runner(sql, params=None):
        if "GROUP BY market, platform" in sql:
            return [{"market": "ke", "platform": "youtube"}]
        return [dict(r) for r in rows]

    evidence = replay.collect_component_factor_evidence(
        query_runner=query_runner,
        components=(component,),
        project="p",
        dataset="d",
        trend_date=date(2026, 9, 3),
        window_start=date(2026, 8, 21),
    )
    item = evidence[replay.persistence.component_bridge_key(component)]
    assert set(item.current_rows) == {"enr_1", "enr_3"}
    assert item.clean_current == ("enr_1",)
    assert item.geo_confirmed_current == ("enr_1",)


@pytest.mark.parametrize("duplicate_window", ["current", "baseline", "both"])
def test_factor_direction_counts_do_not_multiply_multi_term_rows(duplicate_window):
    component = replace(
        _factor_component(),
        member_identities=("ke|keyword|arsenal", "ke|keyword|football"),
        terms=("arsenal", "football"),
    )
    current = _enriched_row()
    baseline = _enriched_row(
        id="baseline_1",
        day=date(2026, 8, 22),
        published_at=datetime(2026, 8, 22, 9, tzinfo=UTC),
    )
    second_current = _enriched_row(id="enr_2")
    other_family = _enriched_row(id="reddit_1", platform="reddit")
    rows = [current, second_current, baseline, other_family]
    if duplicate_window in {"current", "both"}:
        rows.append({**current, "term": "football"})
    if duplicate_window in {"baseline", "both"}:
        rows.append({**baseline, "term": "football"})

    def query_runner(sql):
        if "GROUP BY market, platform" in sql:
            return [{"market": "ke", "platform": "youtube"}, {"market": "ke", "platform": "reddit"}]
        return rows

    evidence = replay.collect_component_factor_evidence(
        query_runner=query_runner,
        components=(component,),
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        trend_date=date(2026, 9, 3),
        window_start=date(2026, 8, 21),
    )
    item = evidence[replay.persistence.component_bridge_key(component)]
    assert item.family_direction_counts == (("reddit", 1, 0), ("youtube", 2, 1))
    assert item.current_rows == ("enr_1", "enr_2", "reddit_1")
    assert item.window_rows == ("baseline_1", "enr_1", "enr_2", "reddit_1")


def test_one_multi_term_post_cannot_create_a_rising_family_direction():
    from src.analysis.open_intelligence.live_quality import family_directions

    terms = ("arsenal", "football", "stadium", "supporters", "watchparty", "weekend")
    component = replace(
        _factor_component(),
        member_identities=tuple(f"ke|keyword|{term}" for term in terms),
        terms=terms,
    )
    rows = [_enriched_row(term=term) for term in terms]
    rows.append(
        _enriched_row(
            id="baseline_1",
            day=date(2026, 8, 22),
            published_at=datetime(2026, 8, 22, 9, tzinfo=UTC),
        )
    )

    def query_runner(sql):
        return (
            [{"market": "ke", "platform": "youtube"}]
            if "GROUP BY market, platform" in sql
            else rows
        )

    evidence = replay.collect_component_factor_evidence(
        query_runner=query_runner,
        components=(component,),
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        trend_date=date(2026, 9, 3),
        window_start=date(2026, 8, 21),
    )
    item = evidence[replay.persistence.component_bridge_key(component)]
    directions = family_directions(
        {family: (current, baseline) for family, current, baseline in item.family_direction_counts},
        exposure_complete={"youtube": True},
    )
    assert directions["youtube"].direction == "not_applicable"


def test_assembly_skips_a_component_with_derivation_reasons():
    """A component whose receipts cannot assemble skips with the assembly's
    exact reason rather than the generic registry list."""
    component = _factor_component()
    key = replay.persistence.component_bridge_key(component)
    evidence = {
        key: replay.scoring_qualification.ComponentFactorEvidence(
            current_rows=("enr_1",),
            window_rows=("enr_1", "enr_2"),
            families_current=("youtube",),
            family_universe=("reddit", "youtube"),
            creators_current=("@kearsenal",),
            clean_current=("enr_1",),
            geo_confirmed_current=("enr_1",),
            current_member_terms=("arsenal",),
            history_member_terms=(),
        )
    }
    projection = SimpleNamespace(receipts_by_member={})
    result = SimpleNamespace(
        components=(component,), evidence_projection=projection, observations=()
    )
    inputs = replay.assemble_component_inputs(
        result=result, evidence_by_component=evidence, trend_date=date(2026, 9, 3)
    )
    assert inputs.metrics_by_component == {}
    assert inputs.missing_by_component[key] == ("evidence_receipts_unavailable",)


def test_assembly_names_missing_receipts_when_metrics_would_measure(monkeypatch):
    component = _factor_component()
    key = replay.persistence.component_bridge_key(component)
    monkeypatch.setattr(
        replay.scoring_qualification, "NOVELTY_FORMULA_ID", "novelty_new_member_share_v1"
    )
    evidence = {
        key: replay.scoring_qualification.ComponentFactorEvidence(
            current_rows=("enr_1",),
            window_rows=("enr_1", "enr_2"),
            families_current=("youtube",),
            family_universe=("reddit", "youtube"),
            creators_current=("@kearsenal",),
            clean_current=("enr_1",),
            geo_confirmed_current=("enr_1",),
            current_member_terms=("arsenal",),
            history_member_terms=(),
        )
    }
    projection = SimpleNamespace(receipts_by_member={})
    result = SimpleNamespace(
        components=(component,), evidence_projection=projection, observations=()
    )
    inputs = replay.assemble_component_inputs(
        result=result, evidence_by_component=evidence, trend_date=date(2026, 9, 3)
    )
    assert inputs.metrics_by_component == {}
    assert "evidence_receipts_unavailable" in inputs.missing_by_component[key]


def test_assembly_admits_a_fully_evidenced_component(monkeypatch):
    from src.analysis.open_intelligence.pipeline import ProjectedEvidenceRow

    component = _factor_component()
    key = replay.persistence.component_bridge_key(component)
    monkeypatch.setattr(
        replay.scoring_qualification, "NOVELTY_FORMULA_ID", "novelty_new_member_share_v1"
    )
    evidence = {
        key: replay.scoring_qualification.ComponentFactorEvidence(
            current_rows=("enr_1",),
            window_rows=("enr_1", "enr_2"),
            families_current=("youtube",),
            family_universe=("reddit", "youtube"),
            creators_current=("@kearsenal",),
            clean_current=("enr_1",),
            geo_confirmed_current=("enr_1",),
            current_member_terms=("arsenal",),
            history_member_terms=(),
        )
    }
    projected = ProjectedEvidenceRow(
        row_id="enr_1",
        market="ke",
        source="youtube",
        platform="youtube",
        vendor_family="google_youtube",
        channel_family="youtube",
        content_type="video",
        query_group=None,
        query_term="arsenal",
        author_name="KE Arsenal",
        author_handle="@kearsenal",
        author_handle_norm="@kearsenal",
        title="Watch party",
        text="Arsenal watch parties keep growing.",
        url="https://evidence.invalid/1",
        published_at=datetime(2026, 9, 3, 9, tzinfo=UTC),
        collected_at=datetime(2026, 9, 3, 10, tzinfo=UTC),
        hashtags=None,
        views=None,
        likes=None,
        comments=None,
        shares=None,
        engagement_total=None,
        regional_score=0.9,
        search_velocity_score=None,
        slang_terms=None,
        pipeline_run_id=None,
        v2tone=None,
        v2persons=None,
        v2orgs=None,
        v2locations=None,
        v2gcam=None,
        tone_avg=None,
        tone_polarity=None,
        topic_groups=None,
        classification_layer=None,
        sentiment_lexicon_score=None,
        source_table="enriched_content",
    )
    projection = SimpleNamespace(receipts_by_member={"ke|keyword|arsenal": (projected,)})
    from src.analysis.open_intelligence.candidates import Observation

    observation = Observation(
        market="ke",
        term="arsenal",
        candidate_type="keyword",
        row_ids=("enr_1",),
        platforms=("youtube",),
        source_families=("youtube",),
    )
    result = SimpleNamespace(
        components=(component,),
        evidence_projection=projection,
        observations=(observation,),
    )
    inputs = replay.assemble_component_inputs(
        result=result, evidence_by_component=evidence, trend_date=date(2026, 9, 3)
    )
    assert key in inputs.metrics_by_component
    metrics = inputs.metrics_by_component[key]
    assert metrics.velocity_score == 0.5
    assert metrics.novelty_score == 1.0
    assert set(inputs.receipts_by_component[key]) == {"enr_1"}
    assert key in inputs.readiness_by_component
    assert inputs.missing_by_component == {}


def test_r3_quality_authority_recomputes_ready_scores_and_prediction_integration(monkeypatch):
    from src.analysis.open_intelligence import live_quality
    from src.analysis.open_intelligence.pipeline import MembershipReceipt
    from src.analysis.open_intelligence.rows import EvidenceReceipt

    from tests.unit import test_dynamic_signal_rows as rows_fixtures

    row_ids = tuple(f"quality_row_{index:02d}" for index in range(10))
    component = SignalComponent(
        market="za",
        member_identities=("za|keyword|repair routine",),
        terms=("repair routine",),
        row_receipts=row_ids,
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        creator_ids=(),
        label="repair routine",
        label_member_identity="za|keyword|repair routine",
        build_version="hybrid_graph_v2",
    )
    key = replay.persistence.component_bridge_key(component)
    receipts = {
        row_id: EvidenceReceipt(
            member_identity="za|keyword|repair routine",
            row_id=row_id,
            source_family="reddit" if index < 5 else "youtube",
            platform="reddit" if index < 5 else "youtube",
            url=f"https://evidence.invalid/{index}",
            published_at=datetime(2026, 9, 3, 23, 30, index, tzinfo=UTC),
            claim_role="direction",
            direction="not_applicable",
            geo_confidence=0.94,
            source_label="Fixture",
            author_label="@fixture",
            excerpt="Fixture evidence",
            metric_label=None,
            availability="available",
        )
        for index, row_id in enumerate(row_ids)
    }
    membership = MembershipReceipt(
        member_id="mem_" + "1" * 64,
        member_identity="za|keyword|repair routine",
        candidate_type="keyword",
        canonical_value="repair routine",
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        row_id=row_ids[0],
        qualifies_evidence=True,
    )
    result = SimpleNamespace(
        components=(component,),
        evidence_projection=SimpleNamespace(memberships_by_component={key: (membership,)}),
        error_state=None,
    )
    scope = replace(rows_fixtures.scope(), run_id=replay.R3_RUN_ID)
    rules = replay.ReadinessRules(
        current_cutoff=datetime(2026, 9, 1, tzinfo=UTC),
        minimum_geo_confidence=0.8,
    )
    assembled = replay.AssembledComponentInputs(
        metrics_by_component={key: rows_fixtures.metrics()},
        receipts_by_component={key: receipts},
        readiness_by_component={},
        readiness_rules=rules,
        missing_by_component={},
    )
    exposure_receipts = tuple(
        {
            "source_family": family,
            "exposure_date": date(2026, 8, 21) + timedelta(days=offset),
            "collection_policy_digest": "1" * 64,
            "quota_authority_id": "2" * 64,
            "quota_applicability": "metered",
            "quota_unit": "requests",
            "quota_limit": 1000,
            "quota_exhausted": False,
            "capture_complete": True,
        }
        for family in ("reddit", "youtube")
        for offset in range(14)
    )
    authority = live_quality.ReviewAuthority(
        run_id=replay.R3_RUN_ID,
        receipt_digest="8" * 64,
        reviewed_by="Albert",
        reviewed_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    reviewed_projection_digests = []

    def validate_review(*_args, **kwargs):
        reviewed_projection_digests.append(kwargs["candidate_projection_digest"])
        return authority

    monkeypatch.setattr(live_quality, "validate_review_receipt", validate_review)

    quality = replay.evaluate_r3_quality_authority(
        result=result,
        scope=scope,
        assembled=assembled,
        signal_date=date(2026, 9, 3),
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
        exposure_receipts=exposure_receipts,
        review_receipt={"candidate_projection_digest": "d" * 64},
        source_window_digest="a" * 64,
        evidence_by_component={
            key: SimpleNamespace(family_direction_counts=(("reddit", 5, 0), ("youtube", 5, 0)))
        },
    )

    assert (
        quality.readiness_by_component[key].state,
        quality.readiness_by_component[key].reasons,
    ) == ("ready", ())
    promotion = next(iter(quality.promotion_results_by_signal_id.values()))
    assert promotion.promotion_eligible is True
    # The direction test reads the component's full term match, not the sampled receipts:
    # the same ten receipts with a one row current count per family name no direction.
    thin = replay.evaluate_r3_quality_authority(
        result=result,
        scope=scope,
        assembled=assembled,
        signal_date=date(2026, 9, 3),
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
        exposure_receipts=exposure_receipts,
        review_receipt={"candidate_projection_digest": "d" * 64},
        source_window_digest="a" * 64,
        evidence_by_component={
            key: SimpleNamespace(family_direction_counts=(("reddit", 1, 0), ("youtube", 1, 0)))
        },
    )
    assert thin.readiness_by_component[key].state == "thin"
    assert "insufficient_directional_agreement" in thin.readiness_by_component[key].reasons
    del reviewed_projection_digests[2:]
    final = replay.persistence.build_producing_batch(
        result,
        scope=scope,
        signal_date=date(2026, 9, 3),
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
        metrics_by_component=quality.metrics_by_component,
        receipts_by_component=quality.receipts_by_component,
        readiness_by_component=quality.readiness_by_component,
        readiness_rules=quality.readiness_rules,
        missing_by_component=quality.missing_by_component,
        quality_evaluated=True,
        promotion_results_by_signal_id=quality.promotion_results_by_signal_id,
    )
    assert len(final.batch.predictions) == 1
    assert final.batch.predictions[0]["display_eligible"] is False
    assert len(reviewed_projection_digests) == 2
    assert len(set(reviewed_projection_digests)) == 1
    assert reviewed_projection_digests[0] == live_quality.candidate_projection_digest(
        replay.R3_RUN_ID, final.batch
    )


def test_assembly_skips_when_one_component_member_has_no_receipt(monkeypatch):
    from src.analysis.open_intelligence.candidates import Observation
    from src.analysis.open_intelligence.rows import EvidenceReceipt

    component = SignalComponent(
        market="ke",
        member_identities=("ke|keyword|arsenal", "ke|keyword|watch party"),
        terms=("arsenal", "watch party"),
        row_receipts=("enr_1",),
        source_families=("youtube",),
        platforms=("youtube",),
        creator_ids=(),
        label="arsenal",
        label_member_identity="ke|keyword|arsenal",
    )
    key = replay.persistence.component_bridge_key(component)
    monkeypatch.setattr(
        replay.scoring_qualification,
        "derive_component_factor_metrics",
        lambda _evidence: (
            {
                "novelty_score": 0.5,
                "velocity_score": 0.5,
                "breadth_score": 0.5,
                "independence_score": 0.5,
                "historical_similarity": None,
                "geo_confidence": 0.9,
            },
            (),
        ),
    )
    receipt = EvidenceReceipt(
        member_identity="ke|keyword|arsenal",
        row_id="enr_1",
        source_family="youtube",
        platform="youtube",
        url="https://evidence.invalid/1",
        published_at=datetime(2026, 9, 3, 9, tzinfo=UTC),
        claim_role="identity",
        direction="rising",
        geo_confidence=0.9,
        source_label="YouTube",
        author_label="@kearsenal",
        excerpt="Watch party",
        metric_label=None,
        availability="available",
    )
    monkeypatch.setattr(replay, "_receipt_for_row", lambda *_args: receipt)
    observation = Observation(
        market="ke",
        term="arsenal",
        candidate_type="keyword",
        row_ids=("enr_1",),
        platforms=("youtube",),
        source_families=("youtube",),
    )
    result = SimpleNamespace(
        components=(component,),
        evidence_projection=SimpleNamespace(receipts_by_member={}),
        observations=(observation,),
    )

    inputs = replay.assemble_component_inputs(
        result=result,
        evidence_by_component={key: object()},
        trend_date=date(2026, 9, 3),
    )

    assert inputs.metrics_by_component == {}
    assert inputs.missing_by_component[key] == ("evidence_receipts_unavailable",)


def test_assembly_skips_when_receipt_family_is_outside_component(monkeypatch):
    from src.analysis.open_intelligence.candidates import Observation
    from src.analysis.open_intelligence.rows import EvidenceReceipt

    component = SignalComponent(
        market="ke",
        member_identities=("ke|keyword|arsenal", "ke|keyword|watch party"),
        terms=("arsenal", "watch party"),
        row_receipts=("enr_1", "enr_2"),
        source_families=("youtube",),
        platforms=("youtube",),
        creator_ids=(),
        label="arsenal",
        label_member_identity="ke|keyword|arsenal",
    )
    key = replay.persistence.component_bridge_key(component)
    monkeypatch.setattr(
        replay.scoring_qualification,
        "derive_component_factor_metrics",
        lambda _evidence: (
            {
                "novelty_score": 0.5,
                "velocity_score": 0.5,
                "breadth_score": 0.5,
                "independence_score": 0.5,
                "historical_similarity": None,
                "geo_confidence": 0.9,
            },
            (),
        ),
    )
    receipts = {
        "enr_1": EvidenceReceipt(
            member_identity="ke|keyword|arsenal",
            row_id="enr_1",
            source_family="youtube",
            platform="youtube",
            url="https://evidence.invalid/1",
            published_at=datetime(2026, 9, 3, 9, tzinfo=UTC),
            claim_role="identity",
            direction="rising",
            geo_confidence=0.9,
            source_label="YouTube",
            author_label="@kearsenal",
            excerpt="Watch party",
            metric_label=None,
            availability="available",
        ),
        "enr_2": EvidenceReceipt(
            member_identity="ke|keyword|watch party",
            row_id="enr_2",
            source_family="reddit",
            platform="reddit",
            url="https://evidence.invalid/2",
            published_at=datetime(2026, 9, 3, 10, tzinfo=UTC),
            claim_role="identity",
            direction="rising",
            geo_confidence=0.9,
            source_label="Reddit",
            author_label="u/kearsenal",
            excerpt="Watch party",
            metric_label=None,
            availability="available",
        ),
    }
    monkeypatch.setattr(
        replay,
        "_receipt_for_row",
        lambda row_id, *_args: receipts[row_id],
    )
    monkeypatch.setattr(
        replay,
        "evaluate_readiness",
        lambda *_args, **_kwargs: pytest.fail("readiness ran before receipt family validation"),
    )
    observations = (
        Observation(
            market="ke",
            term="arsenal",
            candidate_type="keyword",
            row_ids=("enr_1",),
            platforms=("youtube",),
            source_families=("youtube",),
        ),
        Observation(
            market="ke",
            term="watch party",
            candidate_type="keyword",
            row_ids=("enr_2",),
            platforms=("youtube",),
            source_families=("youtube",),
        ),
    )
    result = SimpleNamespace(
        components=(component,),
        evidence_projection=SimpleNamespace(receipts_by_member={}),
        observations=observations,
    )

    inputs = replay.assemble_component_inputs(
        result=result,
        evidence_by_component={key: object()},
        trend_date=date(2026, 9, 3),
    )

    assert inputs.metrics_by_component == {}
    assert inputs.missing_by_component[key] == ("evidence_receipts_unavailable",)


def test_the_script_entry_is_the_final_statement_of_the_module():
    """Twice now an appended section landed after the __main__ block, so the
    job died on a NameError for a function defined further down. The entry
    guard must be the last top-level statement, permanently."""
    import ast as _ast
    from pathlib import Path as _Path

    source = _Path(replay.__file__).read_text(encoding="utf-8")
    last = _ast.parse(source).body[-1]
    assert isinstance(last, _ast.If)
    condition = _ast.unparse(last.test)
    assert "__main__" in condition


def test_protected_r3_requires_the_receipts_the_copier_actually_writes():
    # The copy runner and the review procedure both cover event_ledger, seed_graph,
    # seed_candidates and enriched_content; raw_content is never copied, so a receipt for it
    # can never exist and the protected path must not demand one.
    from scripts.staging import copy_open_intelligence_replay_sources as copier

    assert frozenset(copier.SOURCE_COPY_TABLES) == replay.R3_SOURCE_COPY_TABLES
    assert (
        frozenset({"event_ledger", "seed_graph", "seed_candidates", "enriched_content"})
        == replay.R3_SOURCE_COPY_TABLES
    )
    source = inspect.getsource(replay._R3ArtifactProvider._build)
    assert "R3_SOURCE_COPY_TABLES" in source
    assert '"raw_content"' not in source


def test_r3_quality_authority_with_a_pending_review_promotes_nothing_and_stays_blocked(monkeypatch):
    # The review packet is built from the run's own rows, so the human review can only follow
    # the run. Until it is registered the apply proceeds unreviewed: nothing is promotable and
    # every persisted row stays blocked for the release step to lift.

    from src.analysis.open_intelligence import live_quality
    from src.analysis.open_intelligence.pipeline import MembershipReceipt
    from src.analysis.open_intelligence.rows import EvidenceReceipt

    from tests.unit import test_dynamic_signal_rows as rows_fixtures

    row_ids = tuple(f"quality_row_{index:02d}" for index in range(10))
    component = SignalComponent(
        market="za",
        member_identities=("za|keyword|repair routine",),
        terms=("repair routine",),
        row_receipts=row_ids,
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        creator_ids=(),
        label="repair routine",
        label_member_identity="za|keyword|repair routine",
        build_version="hybrid_graph_v2",
    )
    key = replay.persistence.component_bridge_key(component)
    receipts = {
        row_id: EvidenceReceipt(
            member_identity="za|keyword|repair routine",
            row_id=row_id,
            source_family="reddit" if index < 5 else "youtube",
            platform="reddit" if index < 5 else "youtube",
            url=f"https://evidence.invalid/{index}",
            published_at=datetime(2026, 9, 3, 23, 30, index, tzinfo=UTC),
            claim_role="direction",
            direction="not_applicable",
            geo_confidence=0.94,
            source_label="Fixture",
            author_label="@fixture",
            excerpt="Fixture evidence",
            metric_label=None,
            availability="available",
        )
        for index, row_id in enumerate(row_ids)
    }
    membership = MembershipReceipt(
        member_id="mem_" + "1" * 64,
        member_identity="za|keyword|repair routine",
        candidate_type="keyword",
        canonical_value="repair routine",
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        row_id=row_ids[0],
        qualifies_evidence=True,
    )
    result = SimpleNamespace(
        components=(component,),
        evidence_projection=SimpleNamespace(memberships_by_component={key: (membership,)}),
        error_state=None,
    )
    scope = replace(rows_fixtures.scope(), run_id=replay.R3_RUN_ID)
    rules = replay.ReadinessRules(
        current_cutoff=datetime(2026, 8, 25, tzinfo=UTC),
        minimum_geo_confidence=0.8,
    )
    assembled = replay.AssembledComponentInputs(
        metrics_by_component={key: rows_fixtures.metrics()},
        receipts_by_component={key: receipts},
        readiness_by_component={},
        readiness_rules=rules,
        missing_by_component={},
    )
    exposure_receipts = tuple(
        {
            "source_family": family,
            "exposure_date": date(2026, 8, 21) + timedelta(days=offset),
            "collection_policy_digest": "1" * 64,
            "quota_authority_id": "2" * 64,
            "quota_applicability": "metered",
            "quota_unit": "requests",
            "quota_limit": 1000,
            "quota_exhausted": False,
            "capture_complete": True,
        }
        for family in ("reddit", "youtube")
        for offset in range(14)
    )
    authority = live_quality.ReviewAuthority(
        run_id=replay.R3_RUN_ID,
        receipt_digest="8" * 64,
        reviewed_by="Albert",
        reviewed_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    reviewed_projection_digests = []

    def validate_review(*_args, **kwargs):
        reviewed_projection_digests.append(kwargs["candidate_projection_digest"])
        return authority

    def refuse_review(*_args, **_kwargs):
        raise AssertionError("a pending review must not be validated against the packet")

    monkeypatch.setattr(live_quality, "validate_review_receipt", refuse_review)

    quality = replay.evaluate_r3_quality_authority(
        result=result,
        scope=scope,
        assembled=assembled,
        signal_date=date(2026, 9, 3),
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
        exposure_receipts=exposure_receipts,
        review_receipt=dict(replay.PENDING_REVIEW_RECEIPT),
        source_window_digest="a" * 64,
        evidence_by_component={
            key: SimpleNamespace(family_direction_counts=(("reddit", 5, 0), ("youtube", 5, 0)))
        },
    )

    promotion = next(iter(quality.promotion_results_by_signal_id.values()))
    assert promotion.promotion_eligible is False
    assert (
        quality.readiness_by_component[key].state,
        quality.readiness_by_component[key].reasons,
    ) == ("ready", ())


def test_r3_artifact_provider_reports_a_pending_review_when_none_is_registered():
    class Job:
        def __init__(self, rows):
            self._rows = rows

        def result(self, **_kwargs):
            return list(self._rows)

    class Client:
        def query(self, sql, *, location, job_config, retry, job_retry):
            assert "sp_read_open_intelligence_quality_review_receipt_v1" in sql
            return Job([])

    provider = _retained_provider(Client())
    review = provider._quality_review()
    assert review == dict(replay.PENDING_REVIEW_RECEIPT)
    assert replay._review_is_pending(review) is True
    assert review["run_id"] == replay.R3_RUN_ID
    assert review["review_state"] == "pending"


def test_unreviewed_candidates_are_never_promotion_eligible():
    from scripts.staging.scoring_qualification import approved_decision_strength_rules
    from src.analysis.open_intelligence import live_quality

    from tests.unit import test_dynamic_signal_rows as rows_fixtures

    candidate, _evidence = rows_fixtures.build()
    readiness = SimpleNamespace(qualifying_families=("reddit", "youtube"), state="ready")
    technical = live_quality.TechnicalQuality(passed=True, reasons=())
    scored = live_quality.score_quality_candidate(
        candidate=candidate,
        readiness=readiness,
        receipts={},
        memberships=(),
        technical_quality=technical,
        review_authority=None,
        rules=approved_decision_strength_rules(),
        directional_conflict=False,
    )
    assert scored.promotion_eligible is False
    assert any("foreign" in reason or "review" in reason for reason in scored.reasons)


def test_protected_r3_apply_runs_and_writes_as_its_own_operation_identity(monkeypatch):
    # The durable contract binds r3_apply to a dedicated identity. The protected path must
    # pin that identity for the credential check and hand it to persistence as the writer,
    # instead of the shared staging engine identity the argument path still uses.
    identity = replay.execution_approval._OPERATION_CONTRACTS["r3_apply"]["identity"]
    assert identity == "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert identity == replay.R3_APPLY_IDENTITY
    source = inspect.getsource(replay._run_apply_cli_impl)
    assert "service_account=apply_binding.service_identity if protected else SERVICE_ACCOUNT" in (
        source
    )
    assert (
        "apply_binding.service_identity if protected else persistence.TARGET_WRITER_IDENTITY"
        in (source)
    )
    assert "R3_APPLY_IDENTITY" not in source
    identity = replay._r3_apply_profile(mode="new_consume")[2].service_identity
    monkeypatch.setattr(
        replay.persistence,
        "persist_open_intelligence_rows",
        lambda **kwargs: (kwargs["client"], kwargs["writer_identity"]),
    )
    target, writer = replay.persist_producing_batch(
        client=object(),
        batch=object(),
        rule_bundle=object(),
        dry_run=True,
        writer_identity=identity,
    )
    assert writer == identity
    assert target == replay.persistence.PersistenceTarget(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        location="US",
        writer_identity=identity,
    )


def test_r3_artifact_provider_treats_the_routines_missing_receipt_assertion_as_pending():
    # The read routine asserts exactly one registered receipt, so before the human review
    # it raises quality_review_receipt_unavailable instead of returning no rows. Only that
    # named assertion means pending; any other failure of the read still refuses.
    from google.api_core import exceptions as api_exceptions

    class Client:
        def __init__(self, message):
            self.message = message

        def query(self, sql, *, location, job_config, retry, job_retry):
            assert "sp_read_open_intelligence_quality_review_receipt_v1" in sql
            raise api_exceptions.BadRequest(self.message)

    pending = _retained_provider(
        Client("400 Query error: quality_review_receipt_unavailable at [x:29:3]")
    )
    assert pending._quality_review() == dict(replay.PENDING_REVIEW_RECEIPT)
    identity = _retained_provider(
        Client("400 Query error: quality_review_receipt_identity_invalid at [x:23:3]")
    )
    with pytest.raises(api_exceptions.BadRequest, match="identity_invalid"):
        identity._quality_review()


def test_quality_authority_hands_every_row_build_the_flags_readiness_used():
    # Three row builds follow the readiness classification: the provisional batch, the scoring
    # candidates and the final batch. Each must carry the technical verdict and the conflict
    # flag, or a component that failed its technical check refuses the run on real data.
    source = inspect.getsource(replay.evaluate_r3_quality_authority)
    assert source.count("quality_failed_by_component=quality_failed_by_component") == 2
    assert source.count("factual_conflict_by_component=factual_conflict_by_component") == 2
    assert "quality_failed=quality_failed_by_component[key]" in source
    assert "factual_conflict=factual_conflict_by_component[key]" in source
    apply_source = inspect.getsource(replay.apply_open_intelligence_run)
    assert "quality_failed_by_component=quality_failed_by_component" in apply_source
    assert "factual_conflict_by_component=factual_conflict_by_component" in apply_source
    cli_source = inspect.getsource(replay._run_apply_cli_impl)
    assert "quality_failed_by_component=quality.quality_failed_by_component" in cli_source
    assert "factual_conflict_by_component=quality.factual_conflict_by_component" in cli_source


def test_apply_reads_the_row_set_digest_back_through_sql_after_the_write():
    from tests.unit import test_dynamic_signal_rows as fixtures

    component = _apply_component()
    key = replay.persistence.component_bridge_key(component)
    recorder, report = _apply(
        metrics_by_component={key: fixtures.metrics()},
        receipts_by_component={key: fixtures.receipts()},
        readiness_by_component={key: fixtures.readiness()},
        readiness_rules=fixtures.readiness_rules(),
        quality_evaluated=True,
    )
    kinds = [
        ("persist", flag)
        if kind == "persist"
        else (
            "digest"
            if "AS row_set_digest" in flag
            else ("insert" if "INSERT INTO" in flag and "run_receipts_v1" in flag else "other")
        )
        for kind, flag in recorder.calls
    ]
    assert ("persist", False) in kinds
    assert kinds.index("digest") > kinds.index(("persist", False))
    assert kinds.index("digest") < kinds.index("insert")
    assert report.receipt.row_set_digest == "b" * 64


def test_assembly_readiness_admits_evidence_regardless_of_geo_confidence(monkeypatch):
    # Approved by Albert on 2026-09-03 after the first run on the 21 August to 3 September
    # window: 49 of its 64 evidence rows carried no regional marker and 0 signals survived
    # any geo floor from 0.4 up, while 5 held fresh evidence in two families. Geo confidence
    # therefore weakens decision strength through APPROVED_GEO_FLOORS and no longer decides
    # whether evidence is admitted at all.
    from scripts.staging import scoring_qualification as qualification
    from src.analysis.open_intelligence import daily_native_clients

    assert replay.REPLAY_READINESS_GEO_FLOOR == 0.0
    assert min(qualification.APPROVED_GEO_FLOORS.values()) == 0.6
    # The daily chain's floor is a separate constant, named for its own chain so neither
    # can be read for the other, and it was raised off zero with the composer binding;
    # this pin does not follow it, because the replay decision above was approved over a
    # window whose rows carry no resolved geography at all.
    assert daily_native_clients.DAILY_READINESS_GEO_FLOOR == 0.6
    assert daily_native_clients.DAILY_READINESS_GEO_FLOOR != replay.REPLAY_READINESS_GEO_FLOOR
    assert not hasattr(replay, "READINESS_GEO_FLOOR")
    assert not hasattr(daily_native_clients, "READINESS_GEO_FLOOR")
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after rules")

    monkeypatch.setattr(replay, "ReadinessRules", capture)
    with pytest.raises(RuntimeError, match="stop after rules"):
        replay.assemble_component_inputs(
            result=SimpleNamespace(evidence_projection=None),
            evidence_by_component={},
            trend_date=date(2026, 9, 3),
        )
    assert captured["minimum_geo_confidence"] == 0.0
    assert captured["current_cutoff"] == datetime(2026, 9, 1, tzinfo=UTC)


def test_the_collector_counts_family_direction_rows_over_the_full_term_match():
    # Approved by Albert on 2026-09-03 ("do it"): the direction test reads every row the
    # window's term match cites for the component, by family and by published date, not the
    # handful of sampled receipts that carry the displayed evidence. Current is the signal
    # date and the two days before it; baseline is the window start through the day before.
    component = _factor_component()
    rows = [
        _enriched_row(id="enr_1", published_at=datetime(2026, 9, 3, 9, tzinfo=UTC)),
        _enriched_row(id="enr_2", published_at=datetime(2026, 9, 1, 9, tzinfo=UTC)),
        _enriched_row(
            id="enr_3", day=date(2026, 8, 25), published_at=datetime(2026, 8, 25, 9, tzinfo=UTC)
        ),
        _enriched_row(
            id="enr_4",
            day=date(2026, 8, 22),
            platform="reddit",
            published_at=datetime(2026, 8, 22, 9, tzinfo=UTC),
        ),
        _enriched_row(id="enr_5", day=date(2026, 8, 31), published_at=None),
        _enriched_row(id="enr_x", term="unrelated", query_term="unrelated"),
    ]

    def query_runner(sql, params=None):
        if "GROUP BY market, platform" in sql:
            return [{"market": "ke", "platform": "youtube"}, {"market": "ke", "platform": "reddit"}]
        return [dict(r) for r in rows if r["term"] == "arsenal"]

    evidence = replay.collect_component_factor_evidence(
        query_runner=query_runner,
        components=(component,),
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        trend_date=date(2026, 9, 3),
        window_start=date(2026, 8, 21),
    )
    item = evidence[replay.persistence.component_bridge_key(component)]
    assert item.family_direction_counts == (("reddit", 0, 1), ("youtube", 2, 1))


def test_receipt_for_row_falls_back_to_the_observed_time_when_no_publish_time_exists():
    """A youtube scrape row carries no upload date, so its projected
    published_at is None. The receipt reads the engine's own collected_at for
    that row instead of leaving the timestamp empty, which failed technical
    quality (available_receipt_timestamp_unavailable) on every component the
    row touched (3 Sep 2026, six ZA signals on r5)."""
    from src.analysis.open_intelligence.candidates import Observation
    from src.analysis.open_intelligence.pipeline import ProjectedEvidenceRow

    collected = datetime(2026, 9, 2, 6, 30, tzinfo=UTC)
    projected = ProjectedEvidenceRow(
        row_id="enr_scrape_1",
        market="za",
        source="youtube_scrape",
        platform="youtube",
        vendor_family="google_youtube",
        channel_family="youtube",
        content_type="video",
        query_group="youtube_scrape",
        query_term="johannesburg",
        author_name="A channel",
        author_handle="UC123",
        author_handle_norm=None,
        title="A title",
        text="A title",
        url="https://www.youtube.com/watch?v=abc",
        published_at=None,
        collected_at=collected,
        hashtags=None,
        views=None,
        likes=None,
        comments=None,
        shares=None,
        engagement_total=None,
        regional_score=0.4,
        search_velocity_score=None,
        slang_terms=None,
        pipeline_run_id=None,
        v2tone=None,
        v2persons=None,
        v2orgs=None,
        v2locations=None,
        v2gcam=None,
        tone_avg=None,
        tone_polarity=None,
        topic_groups=None,
        classification_layer=None,
        sentiment_lexicon_score=None,
        source_table="enriched_content",
    )
    observation = Observation(
        market="za",
        term="johannesburg",
        candidate_type="entity",
        row_ids=("enr_scrape_1",),
        platforms=("youtube",),
        source_families=("youtube",),
    )
    receipt = replay._receipt_for_row(
        "enr_scrape_1", "za|entity|johannesburg", observation, projected
    )
    assert receipt.published_at == collected
    assert receipt.availability == "available"
    dated = replace(projected, published_at=datetime(2026, 9, 1, 12, tzinfo=UTC))
    assert replay._receipt_for_row(
        "enr_scrape_1", "za|entity|johannesburg", observation, dated
    ).published_at == datetime(2026, 9, 1, 12, tzinfo=UTC)


def test_r3_quality_authority_promotes_with_a_prior_run_review_for_the_same_rows(monkeypatch):
    # Approved 3 Sep 2026: when the run's own review is pending, the review the
    # human registered on the prior run is authority for this run when the rows
    # are the same, so a ready candidate becomes promotable.
    from src.analysis.open_intelligence import live_quality
    from src.analysis.open_intelligence.pipeline import MembershipReceipt
    from src.analysis.open_intelligence.rows import EvidenceReceipt

    from tests.unit import test_dynamic_signal_rows as rows_fixtures

    row_ids = tuple(f"quality_row_{index:02d}" for index in range(10))
    component = SignalComponent(
        market="za",
        member_identities=("za|keyword|repair routine",),
        terms=("repair routine",),
        row_receipts=row_ids,
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        creator_ids=(),
        label="repair routine",
        label_member_identity="za|keyword|repair routine",
        build_version="hybrid_graph_v2",
    )
    key = replay.persistence.component_bridge_key(component)
    receipts = {
        row_id: EvidenceReceipt(
            member_identity="za|keyword|repair routine",
            row_id=row_id,
            source_family="reddit" if index < 5 else "youtube",
            platform="reddit" if index < 5 else "youtube",
            url=f"https://evidence.invalid/{index}",
            published_at=datetime(2026, 9, 3, 23, 30, index, tzinfo=UTC),
            claim_role="direction",
            direction="not_applicable",
            geo_confidence=0.94,
            source_label="Fixture",
            author_label="@fixture",
            excerpt="Fixture evidence",
            metric_label=None,
            availability="available",
        )
        for index, row_id in enumerate(row_ids)
    }
    membership = MembershipReceipt(
        member_id="mem_" + "1" * 64,
        member_identity="za|keyword|repair routine",
        candidate_type="keyword",
        canonical_value="repair routine",
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        row_id=row_ids[0],
        qualifies_evidence=True,
    )
    result = SimpleNamespace(
        components=(component,),
        evidence_projection=SimpleNamespace(memberships_by_component={key: (membership,)}),
        error_state=None,
    )
    scope = replace(rows_fixtures.scope(), run_id=replay.R3_RUN_ID)
    rules = replay.ReadinessRules(
        current_cutoff=datetime(2026, 8, 25, tzinfo=UTC),
        minimum_geo_confidence=0.8,
    )
    assembled = replay.AssembledComponentInputs(
        metrics_by_component={key: rows_fixtures.metrics()},
        receipts_by_component={key: receipts},
        readiness_by_component={},
        readiness_rules=rules,
        missing_by_component={},
    )
    exposure_receipts = tuple(
        {
            "source_family": family,
            "exposure_date": date(2026, 8, 21) + timedelta(days=offset),
            "collection_policy_digest": "1" * 64,
            "quota_authority_id": "2" * 64,
            "quota_applicability": "metered",
            "quota_unit": "requests",
            "quota_limit": 1000,
            "quota_exhausted": False,
            "capture_complete": True,
        }
        for family in ("reddit", "youtube")
        for offset in range(14)
    )
    authority = live_quality.ReviewAuthority(
        run_id=replay.R3_PRIOR_RUN_ID,
        receipt_digest="8" * 64,
        reviewed_by="Albert",
        reviewed_at=datetime(2026, 9, 3, 17, 35, tzinfo=UTC),
    )
    seen = []

    def content_authority(prior, *, source_window_digest, batch):
        seen.append((prior, source_window_digest, batch))
        return authority

    monkeypatch.setattr(live_quality, "content_review_authority", content_authority)
    prior = object()
    quality = replay.evaluate_r3_quality_authority(
        result=result,
        scope=scope,
        assembled=assembled,
        signal_date=date(2026, 9, 3),
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
        exposure_receipts=exposure_receipts,
        review_receipt=dict(replay.PENDING_REVIEW_RECEIPT),
        source_window_digest="a" * 64,
        evidence_by_component={
            key: SimpleNamespace(family_direction_counts=(("reddit", 5, 0), ("youtube", 5, 0)))
        },
        prior_review=prior,
    )
    promotion = next(iter(quality.promotion_results_by_signal_id.values()))
    assert promotion.promotion_eligible is True
    assert seen
    assert seen[0][0] is prior
    assert seen[0][1] == "a" * 64
    assert quality.readiness_by_component[key].state == "ready"
    assert replay.R3_PRIOR_RUN_ID == "run_20260903_dynamic_apply_v2_r14"
    assert replay.R3_RUN_ID == "run_20260903_dynamic_apply_v2_r16"


def test_prior_quality_review_reads_the_registered_receipt_in_contract_order(monkeypatch):
    # The store returns the receipt as canonical JSON with sorted keys, and the
    # validator demands the contract field order, so the prior review must be
    # reordered on the way in or it is refused silently (r8, 4 Sep 2026).
    from datetime import UTC, datetime

    from src.analysis.open_intelligence import live_quality
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    receipt = {
        "review_contract_version": live_quality.REVIEW_CONTRACT_VERSION,
        "run_id": replay.R3_PRIOR_RUN_ID,
        "source_window_digest": "6" * 64,
        "candidate_projection_digest": "7" * 64,
        "packet_digest": "8" * 64,
        "reviewed_evidence_ids": ("ev_" + "a" * 64,),
        "foreign_market_evidence_ids": (),
        "factual_conflict_evidence_ids": (),
        "uncertain_evidence_ids": (),
        "reviewed_by": "Albert",
        "reviewed_at": datetime(2026, 9, 3, 17, 35, 9, 461350, tzinfo=UTC),
        "decision": "approved",
    }
    receipt["receipt_digest"] = live_quality.review_receipt_digest(receipt)
    canonical_json = canonical_bytes(receipt).decode("utf-8")
    assert canonical_json.startswith('{"candidate_projection_digest"')

    class _Client:
        pass

    provider = _retained_provider(_Client())
    calls = []

    def query(sql, *, parameters=(), max_results=2):
        calls.append(sql)
        if "sp_read_open_intelligence_quality_review_receipt_v1" in sql:
            return (
                {"run_id": replay.R3_PRIOR_RUN_ID, "canonical_review_receipt_json": canonical_json},
            )
        if "AS content_packet" in sql:
            return (
                {
                    "projection": "7" * 64,
                    "packet": "8" * 64,
                    "content_projection": "1" * 64,
                    "content_packet": "2" * 64,
                },
            )
        return ({"evidence_id": "ev_" + "a" * 64},)

    monkeypatch.setattr(provider, "_query", query)
    prior = provider._prior_quality_review()
    assert isinstance(prior, live_quality.PriorRunReview)
    assert tuple(prior.receipt) == live_quality.REVIEW_RECEIPT_FIELDS
    assert prior.receipt["reviewed_at"] == receipt["reviewed_at"]
    assert live_quality.review_receipt_digest(prior.receipt) == receipt["receipt_digest"]
    assert prior.packet_evidence_ids == ("ev_" + "a" * 64,)
    assert prior.candidate_projection_content_digest == "1" * 64


MANIFEST_CUTOFF = date(2026, 9, 1)


def bytes_digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def second_retained_capture() -> dict:
    payload = bundle_payload()
    payload["cutoff"] = MANIFEST_CUTOFF.isoformat()
    payload["current_window"]["start_date"] = "2026-08-26"
    payload["current_window"]["end_date"] = MANIFEST_CUTOFF.isoformat()
    snapshot = payload["source_snapshot"]
    snapshot["snapshot_id"] = "snapshot_fixture_v2"
    snapshot["captured_at"] = "2026-09-01T23:00:00Z"
    snapshot["rows_by_table"]["event_ledger"].append(
        {
            "ledger_id": "ledger_second",
            "trend_date": "2026-09-01",
            "market": "za",
            "entity_key": "fixture",
        }
    )
    payload["candidate_inputs"]["event_ledger"].append(
        {"candidate_id": "ledger_second", "identity": "za|keyword|fixture"}
    )
    snapshot["section_counts"]["event_ledger_candidates"] = 2
    snapshot["section_identity_sets"]["event_ledger_candidates"] = [
        "ledger_fixture",
        "ledger_second",
    ]
    refresh_snapshot_digests(snapshot)
    return payload


def bundle_known_events(payload: dict) -> tuple[KnownEvent, ...]:
    return tuple(KnownEvent(**event) for event in payload["known_event_set"]["events"])


def manifest_signals(source_time: str = "2026-08-25T12:00:00Z") -> tuple[ReplaySignal, ...]:
    return (
        signal("sig_za", "za", "a", "za|keyword|fixture", source_time=source_time),
        signal("sig_ng", "ng", "b", "ng|entity|known ng", source_time=source_time),
        signal("sig_ke", "ke", "c", "ke|entity|known ke", source_time=source_time),
    )


def test_certified_replay_refuses_a_second_valid_retained_capture():
    payload = second_retained_capture()
    bundle = replay.validate_replay_input(payload)
    assert bundle.cutoff == MANIFEST_CUTOFF
    assert bundle.source_snapshot["row_counts_by_table"]["event_ledger"] == 2

    with pytest.raises(IncompleteReplayInput, match="certified cutoff must be 2026-08-25"):
        evaluate_replay(
            cutoff=bundle.cutoff,
            input_row_count=2,
            signals=manifest_signals(),
            known_events=known_events(),
            sample_per_market=1,
        )
    with pytest.raises(IncompleteReplayInput, match="18693"):
        evaluate_replay(
            cutoff=CUTOFF,
            input_row_count=2,
            signals=manifest_signals(),
            known_events=known_events(),
            sample_per_market=1,
        )


def test_replay_input_mutation_cannot_keep_its_old_binding():
    from scripts.staging.replay_manifest import require_input_digest

    payload = {"cutoff": "2026-09-07", "counts": {"event_ledger": 3}}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    require_input_digest(payload, digest)
    payload["counts"]["event_ledger"] = 4
    with pytest.raises(ValueError, match="replay_input_digest_mismatch"):
        require_input_digest(payload, digest)


def test_manifest_replay_evaluates_each_authorized_retained_input_under_its_own_digest():
    from scripts.staging.replay_manifest import evaluate_replay_manifest

    first = bundle_payload()
    second = second_retained_capture()
    assert bytes_digest(first) != bytes_digest(second)

    certified = evaluate_replay_manifest(
        input_payload=first,
        expected_input_sha256=bytes_digest(first),
        signals=manifest_signals(),
        known_events=bundle_known_events(first),
        sample_per_market=1,
    )
    later = evaluate_replay_manifest(
        input_payload=second,
        expected_input_sha256=bytes_digest(second),
        signals=manifest_signals(source_time="2026-08-30T12:00:00Z"),
        known_events=bundle_known_events(second),
        sample_per_market=1,
    )

    assert (certified.cutoff, certified.row_count) == (CUTOFF, 1)
    assert (later.cutoff, later.row_count) == (MANIFEST_CUTOFF, 2)
    for metrics in (certified, later):
        assert metrics.signal_count == 3
        assert metrics.known_event_recall.as_tuple() == (3, 3, 1.0)
        assert {
            market: metric.as_tuple()
            for market, metric in metrics.known_event_recall_by_market.items()
        } == {"ke": (1, 1, 1.0), "ng": (1, 1, 1.0), "za": (1, 1, 1.0)}
        assert metrics.duplicate_rate.as_tuple() == (0, 3, 0.0)
        assert metrics.human_coherence_status == "pending"
    with pytest.raises(IncompleteReplayInput, match="certified cutoff"):
        evaluate_replay(
            cutoff=MANIFEST_CUTOFF,
            input_row_count=2,
            signals=manifest_signals(),
            known_events=known_events(),
            sample_per_market=1,
        )


def manifest_metrics(payload, **overrides):
    from scripts.staging.replay_manifest import evaluate_replay_manifest

    values = {
        "input_payload": payload,
        "expected_input_sha256": bytes_digest(payload),
        "signals": manifest_signals(),
        "known_events": bundle_known_events(payload),
        "sample_per_market": 1,
    }
    values.update(overrides)
    return evaluate_replay_manifest(**values)


def test_manifest_replay_refuses_a_count_that_lost_its_binding():
    payload = second_retained_capture()
    digest = bytes_digest(payload)
    payload["source_snapshot"]["row_counts_by_table"]["event_ledger"] = 3

    with pytest.raises(ValueError, match="replay_input_digest_mismatch"):
        manifest_metrics(payload, expected_input_sha256=digest)


def test_manifest_replay_refuses_a_rebound_wrong_count():
    payload = second_retained_capture()
    payload["source_snapshot"]["row_counts_by_table"]["event_ledger"] = 3

    with pytest.raises(IncompleteReplayInput, match="event_ledger row count is inconsistent"):
        manifest_metrics(payload)


def test_manifest_replay_refuses_signals_missing_a_market():
    with pytest.raises(IncompleteReplayInput, match="signals in every market"):
        manifest_metrics(second_retained_capture(), signals=manifest_signals()[:2])


def test_manifest_replay_refuses_a_future_receipt():
    with pytest.raises(FutureLeakageDetected, match="future source timestamp for sig_ke"):
        manifest_metrics(
            second_retained_capture(),
            signals=manifest_signals(source_time="2026-09-02T00:00:00Z"),
        )


def test_manifest_replay_refuses_duplicate_signal_identity():
    with pytest.raises(IncompleteReplayInput, match="duplicate signal ID"):
        manifest_metrics(
            second_retained_capture(),
            signals=(
                signal("duplicate", "za", "a", "za|entity|known za"),
                signal("duplicate", "ng", "b", "ng|entity|known ng"),
                signal("sig_ke", "ke", "c", "ke|entity|known ke"),
            ),
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda events: (replace(events[0], member_identities=("ke|entity|other",)), *events[1:]),
        lambda events: events[1:],
        lambda events: (*events, KnownEvent("known_extra", "za", ("za|entity|extra",))),
        # The certified ledger's known event file names a different za member
        # than the retained bundle's own known event set, so it is a changed
        # membership for this input, not an alternative authority.
        lambda events: known_events(),
    ],
)
def test_manifest_replay_refuses_changed_known_event_membership(change):
    payload = second_retained_capture()
    with pytest.raises(IncompleteReplayInput, match="known event membership"):
        manifest_metrics(payload, known_events=change(bundle_known_events(payload)))


def test_manifest_replay_validates_the_input_before_any_metric(monkeypatch):
    sampler = Mock(side_effect=AssertionError("metrics ran before validation"))
    monkeypatch.setattr(replay, "_review_sample", sampler)
    monkeypatch.setattr(
        replay,
        "validate_replay_input",
        Mock(side_effect=IncompleteReplayInput("validator gate")),
    )

    with pytest.raises(IncompleteReplayInput, match="validator gate"):
        manifest_metrics(second_retained_capture())
    assert sampler.call_count == 0


def test_manifest_history_is_normalized_as_of_the_admitted_cutoff(monkeypatch):
    from scripts.staging import replay_manifest
    from src.analysis.open_intelligence import history_normalizer

    seen = []
    original = history_normalizer.normalize_vendor_regimes

    def spy(rows, rules, **kwargs):
        seen.append((tuple(rows), kwargs))
        return original(rows, rules, **kwargs)

    monkeypatch.setattr(replay_manifest, "normalize_vendor_regimes", spy)
    payload = second_retained_capture()
    normalized = replay_manifest.manifest_history_normalization(
        replay.validate_replay_input(payload)
    )

    assert tuple(normalized) == ("za",)
    assert len(seen) == 1
    assert seen[0][1] == {"as_of": datetime(2026, 9, 1, 23, 59, 59, 999999, tzinfo=UTC)}
    rows = seen[0][0]
    assert all(isinstance(row, history_normalizer.HistoricalObservation) for row in rows)
    assert {(row.collection_vendor, row.platform, row.seeded_or_discovered) for row in rows} == {
        ("rss", "news", "seeded")
    }
    assert all(row.available_at == datetime(2026, 8, 25, 12, tzinfo=UTC) for row in rows)
    assert normalized["za"].unavailable_rows == 0


def test_manifest_replay_matches_the_certified_evaluator_field_by_field():
    from dataclasses import fields

    from scripts.staging.replay_manifest import evaluate_replay_manifest

    payload = bundle_payload()
    events = bundle_known_events(payload)
    signals = manifest_signals()
    certified = evaluate_replay(
        cutoff=CUTOFF,
        input_row_count=EXPECTED_EVENT_LEDGER_ROWS,
        signals=signals,
        known_events=events,
        sample_per_market=1,
    )
    manifest = evaluate_replay_manifest(
        input_payload=payload,
        expected_input_sha256=bytes_digest(payload),
        signals=signals,
        known_events=events,
        sample_per_market=1,
    )

    assert isinstance(manifest, replay.ReplayMetrics)
    assert tuple(field.name for field in fields(replay.ReplayMetrics)) == (
        "cutoff",
        "row_count",
        "signal_count",
        "known_event_recall",
        "known_event_recall_by_market",
        "duplicate_rate",
        "foreign_leakage",
        "receipt_completeness",
        "evidence_coverage",
        "geo_coverage",
        "review_sample",
        "human_coherence_status",
    )
    assert certified.row_count == EXPECTED_EVENT_LEDGER_ROWS
    assert manifest.row_count == 1
    for field in fields(replay.ReplayMetrics):
        if field.name == "row_count":
            continue
        left = getattr(certified, field.name)
        right = getattr(manifest, field.name)
        if field.name == "known_event_recall_by_market":
            left, right = dict(left), dict(right)
        assert left == right, field.name


def test_manifest_replay_carries_normalization_state_beside_the_certified_fields():
    from dataclasses import fields

    from scripts.staging.replay_manifest import ManifestReplayMetrics

    metrics = manifest_metrics(second_retained_capture())

    assert isinstance(metrics, ManifestReplayMetrics)
    assert isinstance(metrics, replay.ReplayMetrics)
    assert len(fields(replay.ReplayMetrics)) == 12
    assert [field.name for field in fields(ManifestReplayMetrics)][-1] == "history_normalization"
    assert tuple(metrics.history_normalization) == ("za",)
    normalized = metrics.history_normalization["za"]
    assert normalized.state == "unbridgeable"
    assert normalized.gaps == ("platform_coverage_not_comparable",)
    assert normalized.points == ()
    assert normalized.unavailable_rows == 0
    with pytest.raises(TypeError):
        metrics.history_normalization["za"] = None
