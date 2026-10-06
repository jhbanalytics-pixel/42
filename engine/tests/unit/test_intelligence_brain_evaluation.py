from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest


def test_fifteen_task_inventory_and_automated_gates():
    from src.analysis.open_intelligence.brain_evaluation import (
        TASK_IDS,
        evaluate_frozen_tasks,
        load_evaluation_manifest,
        validate_evaluation_manifest,
    )

    manifest = load_evaluation_manifest()
    assert len(TASK_IDS) == 15
    assert tuple(item["task_id"] for item in manifest["tasks"]) == TASK_IDS
    validate_evaluation_manifest(manifest)
    assert all(set(item) == {"task_id", "fixture_id"} for item in manifest["tasks"])
    result = evaluate_frozen_tasks()
    assert result["task_count"] == 15
    assert result["candidate_scores"]["factual_support"] == 4
    assert result["baseline_scores"]["factual_support"] == 3


def test_missing_task_and_violation_fail():
    from src.analysis.open_intelligence.brain_evaluation import (
        load_evaluation_manifest,
        validate_evaluation_manifest,
    )

    manifest = load_evaluation_manifest()
    with pytest.raises(ValueError, match="15-task"):
        validate_evaluation_manifest({**manifest, "tasks": manifest["tasks"][:-1]})


def test_paired_score_vectors_and_exact_human_arithmetic():
    from src.analysis.open_intelligence.brain_evaluation import (
        BrainScoreVector,
        EvaluationReviewRecord,
        evaluate_review_pair,
        load_evaluation_manifest,
        pairing_key,
    )

    baseline = BrainScoreVector(*(3 for _ in range(10)))
    candidate = BrainScoreVector(*(4 for _ in range(10)))
    task = load_evaluation_manifest()["tasks"][0]
    payload = json.loads(
        (Path("tests/fixtures/open_intelligence/brain_v1") / task["fixture_id"]).read_text(
            encoding="utf-8"
        )
    )
    key = pairing_key(
        task["task_id"],
        task["fixture_id"],
        payload["baseline_result_digest"],
        payload["candidate_result_digest"],
    )
    first = EvaluationReviewRecord(
        task["task_id"],
        task["fixture_id"],
        key,
        payload["baseline_result_digest"],
        payload["candidate_result_digest"],
        baseline,
        candidate,
        100,
        90,
        0,
        0,
        0,
        0,
        0,
        "review_1",
        (),
        "pending",
    )
    second = replace(first, review_record_id="review_2")
    scores = evaluate_review_pair(first, second)
    assert scores["candidate"]["factual_support"] == 4
    assert scores["baseline"]["factual_support"] == 3
    low = replace(second, candidate_scores=BrainScoreVector(*(1 for _ in range(10))))
    with pytest.raises(ValueError, match="below 3"):
        evaluate_review_pair(first, low)
