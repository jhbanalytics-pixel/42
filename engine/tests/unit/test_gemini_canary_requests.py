from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from google.genai import types

FORBIDDEN = {
    "temperature",
    "topP",
    "topK",
    "candidateCount",
    "frequencyPenalty",
    "presencePenalty",
}


def allocated_slots(task_id: str, family: str, prefix: str, count: int) -> list[str]:
    slots = []
    for ordinal in range(1, count + 1):
        payload = {"task_id": task_id, "slot": ordinal}
        if family != "claim":
            payload["family"] = family
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        slots.append(prefix + hashlib.sha256(canonical.encode()).hexdigest())
    return slots


def test_canonical_manifest_reproduces_all_twenty_three_stage_inputs() -> None:
    from src.analysis.open_intelligence.canary_manifests import load_canonical_manifest

    manifest = load_canonical_manifest()

    assert manifest["task_count"] == len(manifest["tasks"]) == 11
    assert manifest["stage_count"] == len(manifest["stages"]) == 23
    for stage in manifest["stages"]:
        canonical = json.dumps(
            stage["envelope"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert stage["canonical_envelope_json"] == canonical
        assert stage["envelope_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
        assert stage["input_sha256"] == hashlib.sha256(stage["contents"].encode()).hexdigest()


def test_all_id_slots_are_bounded_reproducible_and_cross_task_unique() -> None:
    from src.analysis.open_intelligence.canary_manifests import load_canonical_manifest

    manifest = load_canonical_manifest()
    seen = {"claim": set(), "requirement": set(), "recommendation": set(), "section": set()}
    for stage in manifest["stages"]:
        envelope = stage["envelope"]
        task_id = stage["task_id"]
        assert envelope["claim_slots"] == allocated_slots(task_id, "claim", "clm_", 12)
        assert len(envelope["claim_slots"]) == len(set(envelope["claim_slots"])) == 12
        seen["claim"].update(envelope["claim_slots"])
        if stage["stage"] == "planning":
            assert envelope["requirement_slots"] == allocated_slots(
                task_id, "requirement", "req_", 20
            )
            assert (
                len(envelope["requirement_slots"]) == len(set(envelope["requirement_slots"])) == 20
            )
            seen["requirement"].update(envelope["requirement_slots"])
        if stage["stage"] == "answering":
            assert envelope["recommendation_slots"] == allocated_slots(
                task_id, "recommendation", "rec_", 6
            )
            assert envelope["section_slots"] == allocated_slots(task_id, "section", "sec_", 12)
            assert len(envelope["recommendation_slots"]) == 6
            assert len(envelope["section_slots"]) == 12
            seen["recommendation"].update(envelope["recommendation_slots"])
            seen["section"].update(envelope["section_slots"])
    assert {key: len(value) for key, value in seen.items()} == {
        "claim": 132,
        "requirement": 220,
        "recommendation": 66,
        "section": 132,
    }


@pytest.mark.parametrize(
    ("stage_id", "expected_thinking"),
    [
        (
            "dynamic_signal_summary:summary:golden_01_emerging_without_keyword",
            types.ThinkingLevel.MEDIUM,
        ),
        (
            "open_question_answer:planning:golden_01_emerging_without_keyword",
            types.ThinkingLevel.MEDIUM,
        ),
        (
            "open_question_answer:answering:golden_01_emerging_without_keyword",
            types.ThinkingLevel.HIGH,
        ),
    ],
)
def test_request_and_full_token_preflight_share_exact_controls(stage_id, expected_thinking) -> None:
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_request import build_canary_request

    stage = stage_manifest(stage_id)
    policy = resolve_canary_policy(stage["consumer"], stage["stage"], "canary")
    request = build_canary_request(stage, policy=policy)

    generation = request.generation_config.model_dump(exclude_none=True, by_alias=True)
    counted = request.count_tokens_config.model_dump(exclude_none=True, by_alias=True)
    assert request.model == "gemini-3.7-flash"
    assert request.contents == stage["contents"]
    assert "httpOptions" not in generation
    assert generation["thinkingConfig"]["thinkingLevel"] == expected_thinking
    assert generation["responseMimeType"] == "application/json"
    assert generation["maxOutputTokens"] == policy.output_ceiling
    assert FORBIDDEN.isdisjoint(generation)
    assert "httpOptions" not in counted
    assert counted["systemInstruction"] == generation["systemInstruction"]
    count_generation = counted["generationConfig"]
    assert count_generation["thinkingConfig"] == generation["thinkingConfig"]
    assert count_generation["responseMimeType"] == generation["responseMimeType"]
    assert count_generation["maxOutputTokens"] == generation["maxOutputTokens"]
    assert FORBIDDEN.isdisjoint(count_generation)


def test_manifest_loader_fails_closed_on_digest_drift(tmp_path) -> None:
    from src.analysis.open_intelligence.canary_manifests import load_canonical_manifest

    source = (
        Path(__file__).resolve().parents[2]
        / "configs/open_intelligence/gemini_3_7_caller_manifests_v1.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["stages"][0]["contents"] += " drift"
    changed = tmp_path / "manifest.json"
    changed.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="digest"):
        load_canonical_manifest(changed)
