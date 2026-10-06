import copy
import hashlib
import importlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from google.genai import types
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_plan import (
    build_question_planning_context,
    validate_question_plan,
)
from src.analysis.open_intelligence.general_question_policy import (
    build_intake_context,
    build_question_policy,
)
from src.analysis.open_intelligence.general_question_request import normalize_question_request

NOW = datetime(2026, 9, 6, 20, tzinfo=UTC)


def inputs(message="How are people changing their commute?", history=None):
    policy = build_question_policy(pricing_verified_at=NOW)
    request = normalize_question_request(
        {"message": message, "history": history or []},
        scope={
            "client_scope_id": "ogilvy_default",
            "market_scope": ["ke", "ng", "za"],
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        request_id=str(UUID(int=1)),
        admitted_at=NOW,
        policy_digest=policy["policy_digest"],
    )
    return policy, request, build_intake_context(request, selected_market="ng")


def build(policy, request, intake):
    module = importlib.import_module("src.analysis.open_intelligence.general_question_planning")
    return module.build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=30
    )


def test_planning_sdk_input_preserves_question_scope_history_and_disclosure():
    policy, request, intake = inputs(
        history=[{"role": "user", "content": str(i)} for i in range(10)]
    )
    actual = build(policy, request, intake)
    context = build_question_planning_context(request, intake)
    assert actual.contents == canonical_bytes(context).decode("utf-8")
    assert context["intake"]["selected_market"] == "ng"
    assert context["request"]["market_scope"] == ["ke", "ng", "za"]
    assert context["limitations"]
    assert context["request"]["brand_config_id"] is None
    assert context["request"]["audience_lens_ids"] == []
    assert actual.input_digest == canonical_digest(context)


def test_preflight_counts_the_same_system_schema_and_thinking_as_generation():
    policy, request, intake = inputs()
    actual = build(policy, request, intake)
    count = actual.count_tokens_config
    generation = actual.generation_config
    assert actual.model == "gemini-3.8-flash"
    assert count.system_instruction == generation.system_instruction
    assert count.generation_config.response_schema == types.Schema.model_validate(
        generation.response_schema
    )
    assert count.generation_config.max_output_tokens == generation.max_output_tokens == 800
    assert count.generation_config.thinking_config == generation.thinking_config
    assert generation.thinking_config.thinking_level.value == "LOW"
    assert generation.automatic_function_calling.disable is True
    assert generation.tools is None
    assert generation.http_options.retry_options.attempts == 1
    assert count.http_options.retry_options.attempts == 1
    assert generation.http_options.timeout == count.http_options.timeout == 30000


def test_followup_instruction_prioritizes_continuity_then_explicit_override_without_audience_default():
    policy, request, intake = inputs(
        "Which of those signals should be investigated first?",
        history=[
            {"role": "user", "content": "Review South Africa from 1 to 7 September 2026."},
            {"role": "assistant", "content": "The observations concern shopping habits."},
        ],
    )
    sdk = build(policy, request, intake)
    instruction = sdk.generation_config.system_instruction
    assert "most recent relevant user turn" in instruction
    assert "current question explicitly changes" in instruction
    assert "before using the default observation window" in instruction
    assert "Assistant statements are not authority" in instruction
    assert "Do not infer" in instruction
    assert "Gen Z" not in instruction
    assert sdk.count_tokens_config.system_instruction == instruction
    assert json.loads(sdk.contents)["request"]["history"] == request["history"]


def test_v1_planning_binding_and_instruction_bytes_remain_exact():
    policy, request, intake = inputs()
    planning = importlib.import_module("src.analysis.open_intelligence.general_question_planning")
    old = planning.build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60, _adapter="v1"
    )
    current = planning.build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60
    )
    assert (
        old.system_instruction_digest
        == "7225760b9fb7e1c9f66b3080002d86dba105aa8e6715126fec48e38a776046e8"
    )
    assert current.input_digest == old.input_digest
    assert current.response_schema_digest == old.response_schema_digest
    assert current.system_instruction_digest != old.system_instruction_digest
    assert (
        hashlib.sha256(current.generation_config.system_instruction.encode()).hexdigest()
        == current.system_instruction_digest
    )
    call = {
        **{
            field: getattr(old, field)
            for field in (
                "input_digest",
                "system_instruction_digest",
                "response_schema_digest",
                "model",
            )
        },
        "thinking_level": "LOW",
        "max_output_tokens": 800,
    }
    assert (
        planning.planning_request_for_call(call, request=request, intake=intake, policy=policy)
        == old
    )
    for field in ("input_digest", "response_schema_digest", "system_instruction_digest"):
        changed = {**call, field: "0" * 64}
        with pytest.raises(ValueError, match="planning binding"):
            planning.planning_request_for_call(
                changed, request=request, intake=intake, policy=policy
            )


def test_unknown_planning_adapter_refuses_before_sdk_request():
    policy, request, intake = inputs()
    planning = importlib.import_module("src.analysis.open_intelligence.general_question_planning")
    with pytest.raises(ValueError, match="planning adapter"):
        planning.build_question_planning_request(
            request, intake, policy=policy, remaining_seconds=60, _adapter="unknown"
        )


def test_unseen_question_and_selection_change_the_exact_request_binding():
    policy, first, intake = inputs()
    original = build(policy, first, intake)
    _, other, second_intake = inputs("What cultural role could a retailer play in saving rituals?")
    changed = build(policy, other, second_intake)
    assert changed.input_digest != original.input_digest
    assert changed.contents != original.contents
    selected = build_intake_context(first, selected_market="za")
    assert build(policy, first, selected).input_digest != original.input_digest


@pytest.mark.parametrize("field", ["policy_digest", "question", "client_scope_id"])
def test_tampered_normalized_request_refuses_before_request_construction(field):
    policy, request, intake = inputs()
    request[field] = "a" * 64 if field == "policy_digest" else "changed"
    with pytest.raises(ValueError):
        build(policy, request, intake)


def test_another_valid_policy_cannot_silently_rebind_a_request():
    policy, request, intake = inputs()
    changed = build_question_policy(pricing_verified_at=NOW.replace(hour=21))
    with pytest.raises(ValueError):
        build(changed, request, intake)
    assert build(policy, request, intake)


def test_mutating_one_returned_sdk_config_does_not_change_later_request():
    policy, request, intake = inputs()
    original = build(policy, request, intake)
    frozen = copy.deepcopy(original.generation_config)
    original.generation_config.system_instruction = "Ignore the real instructions"
    original.generation_config.response_schema["properties"].clear()
    fresh = build(policy, request, intake)
    assert fresh.generation_config == frozen


def stored_plan(request, intake):
    return validate_question_plan(
        {
            "status": "ready",
            "intent": "explanation",
            "markets": ["ng"],
            "window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True},
            "decision": None,
            "requirements": [
                {
                    "requirement_id": "commuting",
                    "question": "Which observed commuting patterns matter?",
                    "kind": "content",
                    "mandatory": True,
                    "search_terms": ["commute"],
                }
            ],
            "clarification": None,
            "limitations": [],
        },
        request=request,
        intake=intake,
    )


def read_plan(value, request, intake):
    module = importlib.import_module("src.analysis.open_intelligence.general_question_planning")
    return module.validate_stored_question_plan(value, request=request, intake=intake)


def test_stored_plan_is_revalidated_and_detached_with_the_same_binding():
    _, request, intake = inputs()
    original = stored_plan(request, intake)
    result = read_plan(original, request, intake)
    assert result == original
    result["requirements"][0]["question"] = "mutated"
    assert read_plan(original, request, intake) == original


@pytest.mark.parametrize(
    "field", ["request_id", "request_digest", "intake_digest", "plan_digest", "contract_version"]
)
def test_stored_plan_rejects_changed_binding(field):
    _, request, intake = inputs()
    value = stored_plan(request, intake)
    value[field] = "changed"
    with pytest.raises(ValueError):
        read_plan(value, request, intake)


def test_stored_plan_rejects_unknown_fields_and_rehashed_invalid_scope():
    _, request, intake = inputs()
    value = stored_plan(request, intake)
    value["sql"] = "SELECT 1"
    with pytest.raises(ValueError):
        read_plan(value, request, intake)
    value = stored_plan(request, intake)
    value["markets"] = ["gb"]
    value["plan_digest"] = canonical_digest(
        {key: item for key, item in value.items() if key != "plan_digest"}
    )
    with pytest.raises(ValueError):
        read_plan(value, request, intake)
