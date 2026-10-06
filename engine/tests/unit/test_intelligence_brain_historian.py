from __future__ import annotations

import json

import pytest


def context():
    from src.analysis.open_intelligence.brain_observer import _run_observer

    from tests.unit.test_intelligence_brain_observer import context as observer_context

    runtime, snapshot = observer_context()
    return runtime, snapshot, _run_observer(runtime, snapshot)


def test_historian_keeps_diffusion_shadow_and_transfer_limits():
    from src.analysis.open_intelligence.brain_historian import _run_historian

    runtime, snapshot, observer = context()
    result = _run_historian(runtime, snapshot, observer)
    assert result.selected_analogue.relationship_state == "analogue"
    assert result.selected_analogue.transferable_lessons
    assert result.selected_analogue.nontransferable_lessons
    assert result.diffusion["relationship_state"] == "diffusion_shadow"
    assert result.diffusion["display_eligible"] is False


def test_historian_rejects_same_time_source(tmp_path, monkeypatch):
    from src.analysis.open_intelligence import brain_authority
    from src.analysis.open_intelligence.brain_historian import _run_historian

    payload = json.loads(brain_authority._FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["historical"]["source_created_at"] = "2026-08-28T10:00:00.000000Z"
    fixture = tmp_path / "same_time_snapshot.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(brain_authority, "_FIXTURE_PATH", fixture)
    runtime, snapshot, observer = context()
    with pytest.raises(ValueError, match="future leak"):
        _run_historian(runtime, snapshot, observer)


def history_plan():
    return {
        "requirements": [
            {
                "requirement_id": "req_history",
                "question": "What happened the last time this pattern appeared?",
                "kind": "history",
                "mandatory": True,
                "search_terms": [],
            },
            {
                "requirement_id": "req_content",
                "question": "What is being said now?",
                "kind": "content",
                "mandatory": True,
                "search_terms": ["pattern"],
            },
        ]
    }


def rewritten_context(tmp_path, monkeypatch, **changes):
    from src.analysis.open_intelligence import brain_authority

    payload = json.loads(brain_authority._FIXTURE_PATH.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if key == "source_created_at":
            payload["historical"][key] = value
        else:
            payload[key] = value
    fixture = tmp_path / "rewritten_snapshot.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(brain_authority, "_FIXTURE_PATH", fixture)
    return context()


def test_history_requirement_yields_an_analogue_with_exact_provenance():
    from src.analysis.open_intelligence.brain_historian import (
        HistorianResult,
        HistoryRequirementAnswer,
    )
    from src.analysis.open_intelligence.general_question_plan import (
        resolve_history_requirements,
    )

    runtime, snapshot, observer = context()
    results = resolve_history_requirements(
        history_plan(), runtime=runtime, snapshot=snapshot, observer=observer
    )

    assert tuple(results) == ("req_history",)
    answer = results["req_history"]
    assert isinstance(answer, HistoryRequirementAnswer)
    assert answer.state == "analogue"
    assert answer.requirement_id == "req_history"
    assert isinstance(answer.historian, HistorianResult)
    assert answer.analogue is answer.historian.selected_analogue
    assert answer.provenance == {
        "historical_object_id": "hist_fixture_1",
        "source_created_at": "2026-08-01T00:00:00+00:00",
        "source_first_observed_at": "2026-08-02T00:00:00+00:00",
        "source_last_observed_at": "2026-08-20T00:00:00+00:00",
        "selection_cutoff": "2026-08-27T00:00:00+00:00",
        "evidence_ids": tuple(snapshot.evidence_ids),
        "signal_id": snapshot.signal_id,
        "snapshot_id": snapshot.snapshot_id,
        "result_digest": answer.historian.result_digest,
    }
    assert answer.as_of == snapshot.as_of
    with pytest.raises(AttributeError):
        answer.state = "changed"


def test_history_requirement_refuses_a_future_source(tmp_path, monkeypatch):
    from src.analysis.open_intelligence.general_question_plan import (
        resolve_history_requirements,
    )

    runtime, snapshot, observer = rewritten_context(
        tmp_path, monkeypatch, source_created_at="2026-08-28T10:00:00.000000Z"
    )
    with pytest.raises(ValueError, match="future leak"):
        resolve_history_requirements(
            history_plan(), runtime=runtime, snapshot=snapshot, observer=observer
        )


def test_history_requirement_without_comparable_history_is_explicit(tmp_path, monkeypatch):
    from src.analysis.open_intelligence.brain_historian import InsufficientHistory
    from src.analysis.open_intelligence.general_question_plan import (
        resolve_history_requirements,
    )

    runtime, snapshot, observer = rewritten_context(tmp_path, monkeypatch, historical_object_ids=[])
    results = resolve_history_requirements(
        history_plan(), runtime=runtime, snapshot=snapshot, observer=observer
    )

    assert tuple(results) == ("req_history",)
    answer = results["req_history"]
    assert isinstance(answer, InsufficientHistory)
    assert answer.state == "insufficient_history"
    assert answer.reason == "no_comparable_history"
    assert answer.requirement_id == "req_history"
    assert answer.signal_id == snapshot.signal_id
    assert answer.as_of == snapshot.as_of


def test_history_requirement_path_ignores_other_kinds_and_rejects_malformed_history():
    from src.analysis.open_intelligence.general_question_plan import (
        resolve_history_requirements,
    )

    runtime, snapshot, observer = context()
    plan = history_plan()
    plan["requirements"] = plan["requirements"][1:]
    assert (
        resolve_history_requirements(plan, runtime=runtime, snapshot=snapshot, observer=observer)
        == {}
    )
    plan = history_plan()
    plan["requirements"][0]["requirement_id"] = ""
    with pytest.raises(ValueError, match="history_requirement_invalid"):
        resolve_history_requirements(plan, runtime=runtime, snapshot=snapshot, observer=observer)
