"""Boundary tests for staging-only outcome job orchestration."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType, SimpleNamespace

import pytest
from src.analysis.open_intelligence import outcome_evaluator, outcome_reader, outcomes, persistence

EVALUATION_DATE = date(2026, 9, 1)
EVALUATED_AT = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
PREDICTION_ID = "pred_" + "a" * 64


def job_module():
    name = "src.analysis.open_intelligence.outcome_job"
    assert importlib.util.find_spec(name) is not None, "outcome job module is missing"
    return importlib.import_module(name)


def empty_read_result():
    return outcome_reader.OutcomeReadResult((), MappingProxyType({}), ())


def evaluation_result():
    from tests.unit.test_outcome_evaluator import evaluate, evaluator_module

    return evaluate(evaluator_module())


class Client:
    project = "ogilvy-trends-v2"


def rule_bundle():
    return SimpleNamespace(
        status="replay_certified",
        rule_version="outcome_rules_v1",
        replay_receipt_id="outcome_replay_receipt_001",
        approved_at=EVALUATED_AT,
        approved_by="Albert",
    )


def persistence_result(dry_run, outcome_count):
    counts = dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    counts["outcomes"] = outcome_count
    return persistence.PersistenceResult(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        dry_run=dry_run,
        validated_counts=counts,
        inserted_counts=dict.fromkeys(persistence.TABLE_BINDINGS, 0),
        unchanged_counts=dict.fromkeys(persistence.TABLE_BINDINGS, 0),
        conflict_counts=dict.fromkeys(persistence.TABLE_BINDINGS, 0),
        statement_digests=dict.fromkeys(persistence.TABLE_BINDINGS, "a" * 64),
        cleanup_state="not_started" if dry_run else "complete",
    )


def run(module, **overrides):
    values = {
        "client": Client(),
        "dataset": "trends_v2_staging",
        "evaluation_date": EVALUATION_DATE,
        "evaluated_at": EVALUATED_AT,
        "client_scope_id": "fixture_scope",
        "evaluation_run_id": "outcome_run_001",
        "outcome_rules": outcomes.OutcomeRules(),
        "persistence_rule_bundle": rule_bundle(),
        "dry_run": True,
        "quality_findings_by_prediction": {},
        "human_calibrations_by_prediction": {},
    }
    values.update(overrides)
    return module.run_outcome_evaluation(**values)


def test_no_due_predictions_skips_evaluation_and_persistence(monkeypatch):
    module = job_module()
    calls = []

    monkeypatch.setattr(module, "read_due_outcome_inputs", lambda **kwargs: empty_read_result())
    monkeypatch.setattr(
        module,
        "build_due_outcome_rows",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("evaluator must not run")),
    )
    monkeypatch.setattr(
        module.persistence,
        "persist_open_intelligence_rows",
        lambda **kwargs: calls.append(kwargs),
    )

    result = run(module)

    assert result.due_prediction_count == 0
    assert result.outcome_row_count == 0
    assert result.persistence_result is None
    assert result.state_counts == dict.fromkeys(outcomes.OUTCOME_STATES, 0)
    assert calls == []


@pytest.mark.parametrize("dry_run", [True, False])
def test_orchestrator_passes_exact_contracts_and_outcomes_only_batch(monkeypatch, dry_run):
    module = job_module()
    read_calls = []
    evaluation_calls = []
    persistence_calls = []
    evaluated = evaluation_result()
    marker = object()

    def fake_read(**kwargs):
        read_calls.append(kwargs)
        return SimpleNamespace(predictions=(marker,))

    def fake_evaluate(**kwargs):
        evaluation_calls.append(kwargs)
        return evaluated

    persisted = persistence_result(dry_run, 1)

    def fake_persist(**kwargs):
        persistence_calls.append(kwargs)
        return persisted

    monkeypatch.setattr(module, "read_due_outcome_inputs", fake_read)
    monkeypatch.setattr(module, "build_due_outcome_rows", fake_evaluate)
    monkeypatch.setattr(module.persistence, "persist_open_intelligence_rows", fake_persist)

    client = Client()
    bundle = rule_bundle()
    result = run(
        module,
        client=client,
        dry_run=dry_run,
        persistence_rule_bundle=bundle,
    )

    assert read_calls == [
        {
            "client": client,
            "dataset": "trends_v2_staging",
            "evaluation_date": EVALUATION_DATE,
            "client_scope_id": "fixture_scope",
            "evaluation_run_id": "outcome_run_001",
        }
    ]
    assert evaluation_calls[0]["read_result"].predictions == (marker,)
    assert evaluation_calls[0]["evaluated_at"] == EVALUATED_AT
    assert evaluation_calls[0]["evaluation_run_id"] == "outcome_run_001"
    assert isinstance(evaluation_calls[0]["rules"], outcomes.OutcomeRules)
    assert evaluation_calls[0]["quality_findings_by_prediction"] == {}
    assert evaluation_calls[0]["human_calibrations_by_prediction"] == {}
    call = persistence_calls[0]
    assert call["project"] == "ogilvy-trends-v2"
    assert call["dataset"] == "trends_v2_staging"
    assert call["dry_run"] is dry_run
    assert call["rule_bundle"] is bundle
    assert isinstance(call["batch"], persistence.OpenIntelligenceRowBatch)
    assert call["batch"].outcomes == evaluated.rows
    assert all(
        getattr(call["batch"], table) == ()
        for table in ("candidates", "evidence", "membership", "lineage", "predictions")
    )
    if dry_run:
        assert isinstance(call["client"], persistence.PersistenceTarget)
    else:
        assert call["client"] is client
    assert result.persistence_result is persisted
    assert result.due_prediction_count == 1
    assert result.outcome_row_count == 1


def test_invalid_boundary_values_fail_before_reader(monkeypatch):
    module = job_module()
    calls = []
    monkeypatch.setattr(module, "read_due_outcome_inputs", lambda **kwargs: calls.append(kwargs))

    with pytest.raises(ValueError, match="dry run"):
        run(module, dry_run=1)
    with pytest.raises(ValueError, match="evaluated at"):
        run(module, evaluated_at=datetime(2026, 9, 1, 18, 0))
    with pytest.raises(ValueError, match="evaluation date"):
        run(module, evaluation_date=datetime(2026, 9, 1))
    with pytest.raises(ValueError, match="outcome rules"):
        run(module, outcome_rules=object())
    with pytest.raises(ValueError, match="exact staging target"):
        run(module, client=SimpleNamespace(project="other-project"))
    with pytest.raises(ValueError, match="exact staging target"):
        run(module, dataset="trends_v2")
    with pytest.raises(ValueError, match="client scope"):
        run(module, client_scope_id="")
    with pytest.raises(ValueError, match="evaluation run"):
        run(module, evaluation_run_id="")
    with pytest.raises(ValueError, match="evaluated at cannot be before evaluation date"):
        run(module, evaluated_at=datetime(2026, 8, 31, 18, 0, tzinfo=UTC))
    with pytest.raises(persistence.RuleInvalid, match="rule bundle"):
        run(module, persistence_rule_bundle=SimpleNamespace(status="bad"))
    assert calls == []


def test_public_job_result_rejects_count_missing_and_persistence_contradictions(monkeypatch):
    module = job_module()
    monkeypatch.setattr(module, "read_due_outcome_inputs", lambda **kwargs: empty_read_result())
    empty = run(module)

    with pytest.raises(ValueError, match="state counts"):
        module.OutcomeJobResult(
            evaluation_run_id="outcome_run_001",
            dry_run=True,
            due_prediction_count=0,
            outcome_row_count=0,
            state_counts={**empty.state_counts, "unresolved": True},
            missing_prediction_ids=(),
            persistence_result=None,
        )
    with pytest.raises(ValueError, match="missing prediction"):
        module.OutcomeJobResult(
            evaluation_run_id="outcome_run_001",
            dry_run=True,
            due_prediction_count=1,
            outcome_row_count=1,
            state_counts={**empty.state_counts, "unresolved": 1},
            missing_prediction_ids=(PREDICTION_ID, PREDICTION_ID),
            persistence_result=persistence_result(True, 1),
        )
    with pytest.raises(ValueError, match="missing prediction"):
        module.OutcomeJobResult(
            evaluation_run_id="outcome_run_001",
            dry_run=True,
            due_prediction_count=1,
            outcome_row_count=1,
            state_counts={**empty.state_counts, "unresolved": 1},
            missing_prediction_ids=("bad-id",),
            persistence_result=persistence_result(True, 1),
        )
    with pytest.raises(ValueError, match="persistence result"):
        module.OutcomeJobResult(
            evaluation_run_id="outcome_run_001",
            dry_run=True,
            due_prediction_count=1,
            outcome_row_count=1,
            state_counts={**empty.state_counts, "unresolved": 1},
            missing_prediction_ids=(),
            persistence_result=persistence_result(False, 1),
        )
    with pytest.raises(ValueError, match="persistence result"):
        module.OutcomeJobResult(
            evaluation_run_id="outcome_run_001",
            dry_run=True,
            due_prediction_count=1,
            outcome_row_count=1,
            state_counts={**empty.state_counts, "unresolved": 1},
            missing_prediction_ids=(),
            persistence_result=persistence_result(True, 0),
        )
    for malformed_count in (True, 1.0):
        with pytest.raises(ValueError, match="persistence result"):
            module.OutcomeJobResult(
                evaluation_run_id="outcome_run_001",
                dry_run=True,
                due_prediction_count=1,
                outcome_row_count=1,
                state_counts={**empty.state_counts, "unresolved": 1},
                missing_prediction_ids=(),
                persistence_result=persistence_result(True, malformed_count),
            )


def test_evaluator_count_mismatch_stops_before_persistence(monkeypatch):
    module = job_module()
    persistence_calls = []
    monkeypatch.setattr(
        module,
        "read_due_outcome_inputs",
        lambda **kwargs: SimpleNamespace(predictions=(object(),)),
    )
    monkeypatch.setattr(
        module,
        "build_due_outcome_rows",
        lambda **kwargs: SimpleNamespace(
            rows=(),
            state_counts=dict.fromkeys(outcomes.OUTCOME_STATES, 0),
            missing_prediction_ids=(),
        ),
    )
    monkeypatch.setattr(
        module.persistence,
        "persist_open_intelligence_rows",
        lambda **kwargs: persistence_calls.append(kwargs),
    )

    with pytest.raises(ValueError, match="evaluator row count"):
        run(module)
    assert persistence_calls == []


def review_records_fixture():
    from src.analysis.open_intelligence import outcome_review_reader

    from tests.unit.test_outcome_review_reader import document, record

    return outcome_review_reader.parse_outcome_review_records(
        document(record(quality_findings=[], human_calibration=None))
    )


def test_v2_evaluation_reads_by_cutoff_and_keeps_unreviewed_predictions_unresolved(monkeypatch):
    module = importlib.import_module("src.analysis.open_intelligence.outcome_job")
    from tests.unit.test_outcome_evaluator import observation, prediction, read_result

    other_id = "pred_" + "c" * 64
    read_calls = []

    def fake_read(**kwargs):
        read_calls.append(kwargs)
        rows = (prediction(), prediction(prediction_id=other_id, signal_id="sig_" + "d" * 64))
        stamped = {
            row["prediction_id"]: tuple(
                observation(
                    offset,
                    available_at=datetime(2026, 8, 27, 0, 40, tzinfo=UTC) + timedelta(days=offset),
                    run_id=f"daily_run_{offset}",
                )
                for offset in range(7)
            )
            for row in rows
        }
        result = read_result(rows, stamped)
        return outcome_reader.OutcomeReadResult(
            predictions=result.predictions,
            observations_by_prediction=result.observations_by_prediction,
            missing_prediction_ids=result.missing_prediction_ids,
            observation_query_version=outcome_reader.OBSERVATION_QUERY_VERSION_V2,
        )

    persisted = []

    def fake_persist(**kwargs):
        persisted.append(kwargs)
        return persistence_result(True, 2)

    monkeypatch.setattr(module, "read_due_outcome_inputs", fake_read)
    monkeypatch.setattr(module.persistence, "persist_open_intelligence_rows", fake_persist)

    cutoff = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    result = run(
        module,
        evaluated_at=cutoff,
        evaluation_version=module.EVALUATION_VERSION_V2,
        review_records=review_records_fixture(),
    )

    assert read_calls[0]["observation_query_version"] == outcome_reader.OBSERVATION_QUERY_VERSION_V2
    assert read_calls[0]["evaluation_cutoff"] == cutoff
    assert result.evaluation_version == module.EVALUATION_VERSION_V2
    assert result.unreviewed_prediction_ids == (other_id,)
    assert result.state_counts["sustained"] == 1
    assert result.state_counts["unresolved"] == 1
    rows = {row["prediction_id"]: row for row in persisted[0]["batch"].outcomes}
    assert rows[other_id]["resolution_reason"] == "review_record_missing"


def test_v2_requires_review_records_and_v1_refuses_them(monkeypatch):
    module = importlib.import_module("src.analysis.open_intelligence.outcome_job")
    calls = []
    monkeypatch.setattr(module, "read_due_outcome_inputs", lambda **kwargs: calls.append(kwargs))

    with pytest.raises(ValueError, match="review records are required"):
        run(module, evaluation_version=module.EVALUATION_VERSION_V2)
    with pytest.raises(ValueError, match="review records require"):
        run(module, review_records=review_records_fixture())
    with pytest.raises(ValueError, match="evaluation version"):
        run(module, evaluation_version="outcome_evaluation_v9")
    with pytest.raises(ValueError, match="review records"):
        run(module, evaluation_version=module.EVALUATION_VERSION_V2, review_records={"records": []})
    assert calls == []


def test_same_immutable_run_never_rewrites_an_unresolved_result(monkeypatch):
    module = importlib.import_module("src.analysis.open_intelligence.outcome_job")
    from tests.unit.test_outcome_evaluator import prediction

    stored: dict[tuple[str, date, str], dict] = {}
    persist_calls = []

    def fake_read(**kwargs):
        row = prediction()
        key = (row["prediction_id"], kwargs["evaluation_date"], kwargs["evaluation_run_id"])
        rows = () if key in stored else (row,)
        return outcome_reader.OutcomeReadResult(
            predictions=rows,
            observations_by_prediction=MappingProxyType({row["prediction_id"]: () for row in rows}),
            missing_prediction_ids=tuple(row["prediction_id"] for row in rows),
        )

    def fake_persist(**kwargs):
        persist_calls.append(kwargs)
        for row in kwargs["batch"].outcomes:
            key = (row["prediction_id"], row["evaluation_date"], row["run_id"])
            if key in stored and stored[key] != dict(row):
                raise persistence.ImmutableConflict("immutable row conflicts with existing content")
            stored[key] = dict(row)
        return persistence_result(False, len(kwargs["batch"].outcomes))

    monkeypatch.setattr(module, "read_due_outcome_inputs", fake_read)
    monkeypatch.setattr(module.persistence, "persist_open_intelligence_rows", fake_persist)

    first = run(module, dry_run=False, evaluation_run_id="outcome_eval_v1_2026-09-01")
    assert first.state_counts["unresolved"] == 1
    assert next(iter(stored.values()))["resolution_reason"] == "daily_window_incomplete"
    second = run(module, dry_run=False, evaluation_run_id="outcome_eval_v1_2026-09-01")
    assert second.due_prediction_count == 0
    assert second.persistence_result is None
    assert len(persist_calls) == 1
    corrected = run(module, dry_run=False, evaluation_run_id="outcome_eval_v2_2026-09-01")
    assert corrected.due_prediction_count == 1
    assert len(stored) == 2
    assert {key[2] for key in stored} == {
        "outcome_eval_v1_2026-09-01",
        "outcome_eval_v2_2026-09-01",
    }


def test_carried_outcome_rows_must_match_the_declared_outcome_row_count():
    """No rows carried is an absence, and it never reads as a run that wrote none."""
    module = job_module()
    states = dict.fromkeys(module.OUTCOME_STATES, 0)
    row = {"prediction_id": PREDICTION_ID, "outcome": "unresolved"}

    carried = module.OutcomeJobResult(
        evaluation_run_id="outcome_run_001",
        dry_run=True,
        due_prediction_count=1,
        outcome_row_count=1,
        state_counts={**states, "unresolved": 1},
        missing_prediction_ids=(),
        persistence_result=persistence_result(True, 1),
        outcome_rows=(row,),
    )
    assert carried.outcome_rows == (row,)

    with pytest.raises(ValueError, match="outcome rows do not match"):
        module.OutcomeJobResult(
            evaluation_run_id="outcome_run_001",
            dry_run=True,
            due_prediction_count=2,
            outcome_row_count=2,
            state_counts={**states, "unresolved": 2},
            missing_prediction_ids=(),
            persistence_result=persistence_result(True, 2),
            outcome_rows=(row,),
        )
    with pytest.raises(ValueError, match="outcome rows are invalid"):
        module.OutcomeJobResult(
            evaluation_run_id="outcome_run_001",
            dry_run=True,
            due_prediction_count=1,
            outcome_row_count=1,
            state_counts={**states, "unresolved": 1},
            missing_prediction_ids=(),
            persistence_result=persistence_result(True, 1),
            outcome_rows=("not a row",),
        )


def test_rows_not_carried_are_absent_and_an_empty_tuple_means_none_were_written():
    module = job_module()
    states = dict.fromkeys(module.OUTCOME_STATES, 0)
    row = {"prediction_id": PREDICTION_ID, "outcome": "unresolved"}

    absent = module.OutcomeJobResult(
        evaluation_run_id="outcome_run_001",
        dry_run=True,
        due_prediction_count=1,
        outcome_row_count=1,
        state_counts={**states, "unresolved": 1},
        missing_prediction_ids=(),
        persistence_result=persistence_result(True, 1),
    )
    assert absent.outcome_rows is None

    none_written = module.OutcomeJobResult(
        evaluation_run_id="outcome_run_001",
        dry_run=True,
        due_prediction_count=0,
        outcome_row_count=0,
        state_counts=dict(states),
        missing_prediction_ids=(),
        persistence_result=None,
        outcome_rows=(),
    )
    assert none_written.outcome_rows == ()

    # An empty tuple beside a nonzero count is a contradiction, not an absence.
    with pytest.raises(ValueError, match="outcome rows do not match"):
        module.OutcomeJobResult(
            evaluation_run_id="outcome_run_001",
            dry_run=True,
            due_prediction_count=1,
            outcome_row_count=1,
            state_counts={**states, "unresolved": 1},
            missing_prediction_ids=(),
            persistence_result=persistence_result(True, 1),
            outcome_rows=(),
        )
    assert absent.outcome_rows is not ()  # noqa: F632
    assert row not in (absent.outcome_rows or ())


@pytest.mark.parametrize("carry", [True, False])
def test_the_evaluator_rows_reach_the_result_only_when_the_caller_asks_for_them(monkeypatch, carry):
    """The real evaluator's rows, through the real orchestrator, with no fake runner."""
    module = job_module()
    evaluated = evaluation_result()

    monkeypatch.setattr(
        module, "read_due_outcome_inputs", lambda **kwargs: SimpleNamespace(predictions=(object(),))
    )
    monkeypatch.setattr(module, "build_due_outcome_rows", lambda **kwargs: evaluated)
    monkeypatch.setattr(
        module.persistence,
        "persist_open_intelligence_rows",
        lambda **kwargs: persistence_result(True, 1),
    )

    result = run(module, carry_outcome_rows=carry)

    assert evaluated.rows
    if carry:
        assert result.outcome_rows == tuple(evaluated.rows)
        assert result.outcome_rows[0]["run_id"] == "outcome_run_001"
        assert result.outcome_rows[0]["prediction_id"] == PREDICTION_ID
    else:
        assert result.outcome_rows is None


def test_a_day_that_wrote_no_row_still_says_so_when_the_rows_were_asked_for(monkeypatch):
    module = job_module()

    monkeypatch.setattr(module, "read_due_outcome_inputs", lambda **kwargs: empty_read_result())

    assert run(module, carry_outcome_rows=True).outcome_rows == ()
    assert run(module, carry_outcome_rows=False).outcome_rows is None


def test_carry_outcome_rows_must_be_boolean(monkeypatch):
    module = job_module()

    monkeypatch.setattr(module, "read_due_outcome_inputs", lambda **kwargs: empty_read_result())

    with pytest.raises(ValueError, match="carry outcome rows must be boolean"):
        run(module, carry_outcome_rows="yes")


def verified_dry_result(module, rows, *, run_id="outcome_run_001", **overrides):
    states = dict.fromkeys(module.OUTCOME_STATES, 0)
    for row in rows:
        states[row["outcome"]] += 1
    values = {
        "evaluation_run_id": run_id,
        "dry_run": True,
        "due_prediction_count": len(rows),
        "outcome_row_count": len(rows),
        "state_counts": states,
        "missing_prediction_ids": (),
        "persistence_result": persistence_result(True, len(rows)) if rows else None,
        "outcome_rows": tuple(rows),
    }
    values.update(overrides)
    return module.OutcomeJobResult(**values)


def test_a_verified_result_is_persisted_exactly_without_a_new_read_or_evaluation(monkeypatch):
    module = job_module()
    row = evaluation_result().rows[0]
    assert row["run_id"] == "outcome_run_001"
    verified = verified_dry_result(module, (row,), missing_prediction_ids=(PREDICTION_ID,))
    persisted = []

    def refuse(**kwargs):
        raise AssertionError("a verified write must not read or evaluate again")

    def fake_persist(**kwargs):
        persisted.append(kwargs)
        return persistence_result(False, 1)

    monkeypatch.setattr(module, "read_due_outcome_inputs", refuse)
    monkeypatch.setattr(module, "build_due_outcome_rows", refuse)
    monkeypatch.setattr(module.persistence, "persist_open_intelligence_rows", fake_persist)

    result = run(module, dry_run=False, carry_outcome_rows=True, verified_result=verified)

    assert len(persisted) == 1
    assert persisted[0]["dry_run"] is False
    assert persisted[0]["client"] is not None
    assert tuple(persisted[0]["batch"].outcomes) == (row,)
    assert result.dry_run is False
    assert result.outcome_rows == (row,)
    assert result.outcome_rows[0] is row
    assert dict(result.state_counts) == dict(verified.state_counts)
    assert result.missing_prediction_ids == (PREDICTION_ID,)
    assert result.persistence_result.dry_run is False


def test_a_verified_result_with_no_rows_writes_nothing(monkeypatch):
    module = job_module()
    monkeypatch.setattr(
        module.persistence,
        "persist_open_intelligence_rows",
        lambda **kwargs: pytest.fail("nothing to persist"),
    )

    result = run(
        module,
        dry_run=False,
        carry_outcome_rows=True,
        verified_result=verified_dry_result(module, ()),
    )

    assert result.outcome_rows == ()
    assert result.persistence_result is None


def test_a_verified_result_is_refused_unless_it_is_this_runs_carried_dry_run(monkeypatch):
    module = job_module()
    row = {"prediction_id": PREDICTION_ID, "run_id": "outcome_run_001", "outcome": "unresolved"}
    monkeypatch.setattr(
        module.persistence,
        "persist_open_intelligence_rows",
        lambda **kwargs: pytest.fail("a refused verified result must not persist"),
    )
    good = verified_dry_result(module, (row,))
    cases = (
        ({"verified_result": good, "dry_run": True}, "verified result"),
        ({"verified_result": good, "carry_outcome_rows": False}, "verified result"),
        ({"verified_result": object()}, "verified result"),
        (
            {
                "verified_result": verified_dry_result(
                    module, (row,), dry_run=False, persistence_result=persistence_result(False, 1)
                )
            },
            "verified result",
        ),
        (
            {"verified_result": verified_dry_result(module, (row,), outcome_rows=None)},
            "verified result",
        ),
        (
            {"verified_result": verified_dry_result(module, (row,), run_id="outcome_run_002")},
            "verified result",
        ),
        (
            {
                "verified_result": verified_dry_result(
                    module, ({**row, "run_id": "outcome_run_002"},)
                )
            },
            "verified result",
        ),
        (
            {
                "verified_result": verified_dry_result(module, (row,)),
                "evaluation_version": module.EVALUATION_VERSION_V2,
                "review_records": review_records_fixture(),
            },
            "verified result",
        ),
    )
    for overrides, message in cases:
        values = {"dry_run": False, "carry_outcome_rows": True, **overrides}
        with pytest.raises(ValueError, match=message):
            run(module, **values)
