from __future__ import annotations

import json

import pytest


def _fixture_with(tmp_path, monkeypatch, mutate):
    from src.analysis.open_intelligence import brain_authority

    payload = json.loads(brain_authority._FIXTURE_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    fixture = tmp_path / "outcome_time_snapshot.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(brain_authority, "_FIXTURE_PATH", fixture)


def test_prediction_window_after_snapshot_does_not_resolve(tmp_path, monkeypatch):
    from src.analysis.open_intelligence.brain_analyst import _run_analyst

    from tests.unit.test_intelligence_brain_analyst import context

    def move_window_after_snapshot(payload):
        payload["prediction"]["expected_window_start"] = "2026-08-29"
        payload["prediction"]["expected_window_end"] = "2026-09-12"

    _fixture_with(tmp_path, monkeypatch, move_window_after_snapshot)
    runtime, snapshot, observer, historian = context()
    result = _run_analyst(runtime, snapshot, observer, historian)

    assert result.prediction_learning.expected_window_start > snapshot.as_of.date().isoformat()
    assert result.prediction_learning.outcome_state == "unresolved"
    assert result.prediction_learning.observed_outcome_ids == ()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload["outcomes"][0].pop("observed_at"), "time is invalid"),
        (
            lambda payload: payload["outcomes"][0].__setitem__(
                "observed_at", "2026-08-28T10:00:00.000001Z"
            ),
            "exceeds snapshot as_of",
        ),
        (
            lambda payload: payload["outcomes"][0].__setitem__(
                "observed_at", "2026-08-20T02:00:00+02:00"
            ),
            "time is invalid",
        ),
    ],
)
def test_snapshot_rejects_missing_or_future_outcome_time(tmp_path, monkeypatch, mutate, message):
    from tests.unit.test_intelligence_brain_observer import context

    _fixture_with(tmp_path, monkeypatch, mutate)
    with pytest.raises(ValueError, match=message):
        context()


def test_prediction_resolves_only_with_closed_window_and_in_window_outcome(tmp_path, monkeypatch):
    from src.analysis.open_intelligence.brain_analyst import _run_analyst

    from tests.unit.test_intelligence_brain_analyst import context

    def move_outcome_outside_window(payload):
        payload["outcomes"][0]["observed_at"] = "2026-08-21T00:00:00.000000Z"

    _fixture_with(tmp_path, monkeypatch, move_outcome_outside_window)
    runtime, snapshot, observer, historian = context()
    result = _run_analyst(runtime, snapshot, observer, historian)

    assert result.prediction_learning.expected_window_end < snapshot.as_of.date().isoformat()
    assert result.prediction_learning.outcome_state == "unresolved"
    assert result.prediction_learning.observed_outcome_ids == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["outcomes"][0].__setitem__(
            "observed_at", "2026-08-21T00:00:00.000000Z"
        ),
        lambda payload: payload["outcomes"][0].__setitem__("prediction_id", "pred_" + "f" * 64),
    ],
)
def test_unresolved_outcome_never_creates_resolved_by_edge(tmp_path, monkeypatch, mutate):
    from src.analysis.open_intelligence.brain import run_intelligence_brain

    from tests.unit.test_intelligence_brain_runner import intent

    _fixture_with(tmp_path, monkeypatch, mutate)
    result = run_intelligence_brain(intent())

    assert result.analyst.prediction_learning.outcome_state == "unresolved"
    assert result.analyst.prediction_learning.observed_outcome_ids == ()
    assert not any(edge.edge_type == "resolved_by" for edge in result.evidence_graph.edges)


def _context_with_missing_consumer_time(monkeypatch):
    from src.analysis.open_intelligence import brain_authority
    from src.analysis.open_intelligence.brain_historian import _run_historian

    from tests.unit.test_intelligence_brain_historian import context

    runtime, snapshot, observer = context()
    historian = _run_historian(runtime, snapshot, observer)
    payload = brain_authority._snapshot_payload(snapshot)
    payload["outcomes"][0].pop("observed_at")
    monkeypatch.setattr(brain_authority, "_snapshot_payload", lambda _snapshot: payload)
    return runtime, snapshot, observer, historian


def test_historian_rejects_outcome_without_observation_time(monkeypatch):
    from src.analysis.open_intelligence.brain_historian import _run_historian

    runtime, snapshot, observer, _ = _context_with_missing_consumer_time(monkeypatch)

    with pytest.raises(ValueError, match="outcome observation time is invalid"):
        _run_historian(runtime, snapshot, observer)


def test_analyst_rejects_outcome_without_observation_time(monkeypatch):
    from src.analysis.open_intelligence.brain_analyst import _run_analyst

    runtime, snapshot, observer, historian = _context_with_missing_consumer_time(monkeypatch)

    with pytest.raises(ValueError, match="outcome observation time is invalid"):
        _run_analyst(runtime, snapshot, observer, historian)
