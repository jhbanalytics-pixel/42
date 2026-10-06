from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest


def intent(depth="investigation", investigation_id="inv_fixture_01"):
    from src.analysis.open_intelligence.brain_contract import IntelligenceBrainIntent

    return IntelligenceBrainIntent(
        "intelligence_brain_v1",
        investigation_id,
        "sig_" + "a" * 64,
        depth,
        "What changed?" if depth != "briefing" else None,
        None,
    )


def test_weak_authority_free_result_validator_is_not_exposed():
    from src.analysis.open_intelligence import brain_contract

    assert not hasattr(brain_contract, "validate_intelligence_brain_result")


def test_nonbriefing_intent_requires_stored_investigation():
    with pytest.raises(ValueError, match="investigation"):
        intent("scan", None)


def test_evaluation_rejects_manifest_task_pointing_at_another_task_fixture():
    from src.analysis.open_intelligence.brain_evaluation import (
        load_evaluation_manifest,
        validate_evaluation_manifest,
    )

    manifest = deepcopy(load_evaluation_manifest())
    manifest["tasks"][0]["fixture_id"] = manifest["tasks"][1]["fixture_id"]
    with pytest.raises(ValueError, match="task fixture"):
        validate_evaluation_manifest(manifest)


def test_graph_contains_every_mandatory_derived_type_with_admission():
    from src.analysis.open_intelligence.brain import run_intelligence_brain
    from src.analysis.open_intelligence.brain_contract import BrainAdmission

    graph = run_intelligence_brain(intent()).evidence_graph
    assert {item.node_type for item in graph.nodes} >= {
        "signal",
        "role_result",
        "observation",
        "claim",
        "receipt",
        "source",
        "market",
        "historical_object",
        "prediction",
        "outcome",
        "tension",
        "opportunity",
    }
    assert {item.edge_type for item in graph.edges} >= {
        "observed_in",
        "derived_from",
        "supports",
        "challenges",
        "compares_with",
        "historical_of",
        "depends_on",
        "would_change",
        "predicts",
        "resolved_by",
    }
    assert all(isinstance(item.admission, BrainAdmission) for item in graph.nodes)
    assert all(isinstance(item.admission, BrainAdmission) for item in graph.edges)


def test_every_observer_measurement_id_resolves_inside_snapshot():
    from src.analysis.open_intelligence.brain_authority import (
        _issue_brain_evidence_snapshot,
        _issue_brain_runtime_request,
        _snapshot_measurements,
    )
    from src.analysis.open_intelligence.brain_observer import _run_observer

    runtime = _issue_brain_runtime_request(intent())
    snapshot = _issue_brain_evidence_snapshot(runtime)
    observer = _run_observer(runtime, snapshot)
    issued = {item.measurement_id for item in _snapshot_measurements(snapshot)}
    assert {item.measurement_id for item in observer.observations} == issued


@pytest.mark.parametrize(
    "mutation",
    ["naive_window", "foreign_market", "malformed_evidence"],
)
def test_measured_record_rejects_unsafe_time_market_and_evidence(mutation):
    from src.analysis.open_intelligence.brain_contract import BrainMeasurement

    values = {
        "measurement_id": "bm_safe",
        "metric_name": "velocity",
        "availability": "measured",
        "value": 0.5,
        "unit": "index",
        "method_id": "velocity_v1",
        "window_start": datetime(2026, 8, 1, tzinfo=UTC),
        "window_end": datetime(2026, 8, 20, tzinfo=UTC),
        "market": "za",
        "evidence_ids": ("ev_" + "a" * 64,),
        "limitations": (),
    }
    if mutation == "naive_window":
        values["window_start"] = datetime(2026, 8, 1)
    elif mutation == "foreign_market":
        values["market"] = "us"
    else:
        values["evidence_ids"] = ("ev_bad",)
    with pytest.raises(ValueError):
        BrainMeasurement(**values)
