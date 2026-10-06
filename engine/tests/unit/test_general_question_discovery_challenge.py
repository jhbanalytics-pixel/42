"""Discovery questions plan a challenge search under a new, separately versioned adapter."""

import copy
import hashlib
from datetime import timedelta

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_control import QuestionStoreError
from src.analysis.open_intelligence.general_question_policy import build_intake_context

from tests.unit import test_general_question_challenge_and_quotation as challenge
from tests.unit import test_general_question_runtime as runtime_tests
from tests.unit import test_general_question_store as store_fixture

ADAPTER = "discovery_challenge_v6"
PLAN_VERSION = "general_question_plan_v4"
# challenge_v5 bytes as released; the new adapter never edits them in place.
CHALLENGE_V5_INSTRUCTION = "1b6313c65fbd51b6a757e19b94fb7b15d20ecf6cc278829ecd391b5dae6b29c8"
CHALLENGE_V5_SCHEMA = "f79748a8f7b83da93e6f017f8e1c1bd2fd5d8639e26fa4de06c5d20cfcf5add0"
DISCOVERY = "What were South Africans talking about in the first week of September 2026?"

planning = challenge.planning
answering = challenge.answering
requirement = challenge.requirement
draft = challenge.draft


def discovery_plan(request, intake, **overrides):
    return planning().validate_discovery_challenge_plan(
        draft(intent="discovery", **overrides), request=request, intake=intake
    )


def discovery_pair():
    return [
        requirement("themes", "support"),
        requirement("themes_against", "challenge"),
    ]


def test_discovery_challenge_adapter_is_new_bytes_and_challenge_v5_stays_pinned():
    policy, request, intake = challenge.inputs(DISCOVERY)
    module = planning()
    old = module.build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60, _adapter="challenge_v5"
    )
    assert old.system_instruction_digest == CHALLENGE_V5_INSTRUCTION
    assert old.response_schema_digest == CHALLENGE_V5_SCHEMA
    new = module.build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60, _adapter=ADAPTER
    )
    assert new.system_instruction_digest != old.system_instruction_digest
    assert new.response_schema_digest == old.response_schema_digest
    assert new.input_digest == old.input_digest
    assert new.generation_config.max_output_tokens == old.generation_config.max_output_tokens
    instruction = module._PLANNING_INSTRUCTIONS[ADAPTER]
    assert instruction.startswith(module._PLANNING_INSTRUCTIONS["challenge_v5"])
    assert "discovery" in instruction.removeprefix(module._PLANNING_INSTRUCTIONS["challenge_v5"])
    assert hashlib.sha256(instruction.encode()).hexdigest() == new.system_instruction_digest
    assert module.planning_adapter_id(new.system_instruction_digest) == ADAPTER
    assert module.planning_adapter_id(old.system_instruction_digest) == "challenge_v5"
    assert (
        module.planning_adapter_id(new.system_instruction_digest, new.response_schema_digest)
        == ADAPTER
    )
    assert module.planning_schema_digest(ADAPTER) == new.response_schema_digest
    current = module.build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60
    )
    with pytest.raises(ValueError, match="planning binding is invalid"):
        module.planning_adapter_id(new.system_instruction_digest, current.response_schema_digest)
    call = {
        **{
            field: getattr(new, field)
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
        module.planning_request_for_call(call, request=request, intake=intake, policy=policy) == new
    )
    older_intake = build_intake_context(request, selected_market="za")
    with pytest.raises(ValueError, match="planning binding"):
        module.build_question_planning_request(
            request, older_intake, policy=policy, remaining_seconds=60, _adapter=ADAPTER
        )


def test_discovery_plan_needs_a_support_and_challenge_pair_under_the_new_version_only():
    _, request, intake = challenge.inputs(DISCOVERY)
    plan = discovery_plan(request, intake, requirements=discovery_pair())
    assert plan["contract_version"] == PLAN_VERSION
    assert [item["evidence_purpose"] for item in plan["requirements"]] == ["support", "challenge"]
    assert plan["plan_digest"] == canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    for requirements in (
        [requirement("only", "context")],
        [requirement("only", "support")],
        [requirement("only", "challenge")],
        [requirement("first", "support"), requirement("second", "context")],
    ):
        with pytest.raises(ValueError, match="semantic_output_invalid"):
            discovery_plan(request, intake, requirements=requirements)
        if requirements[0]["evidence_purpose"] != "challenge":
            older = challenge.challenge_plan(
                request, intake, intent="discovery", requirements=requirements
            )
            assert older["contract_version"] == "general_question_plan_v3"
    for intent in ("explanation", "comparison"):
        with pytest.raises(ValueError, match="semantic_output_invalid"):
            planning().validate_discovery_challenge_plan(
                draft(intent=intent, requirements=[requirement("only", "support")]),
                request=request,
                intake=intake,
            )
    assert (
        planning().validate_discovery_challenge_plan(
            draft(intent="creators", requirements=[requirement("only", "context")]),
            request=request,
            intake=intake,
        )["contract_version"]
        == PLAN_VERSION
    )
    clarification = discovery_plan(
        request,
        intake,
        status="needs_clarification",
        requirements=[],
        window=None,
        clarification="Which dates should the discovery search cover?",
    )
    assert clarification["contract_version"] == PLAN_VERSION
    eight = [requirement(f"r{index}", "support" if index else "challenge") for index in range(8)]
    assert len(discovery_plan(request, intake, requirements=eight)["requirements"]) == 8
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        discovery_plan(request, intake, requirements=[*eight, requirement("r8", "support")])


def test_stored_discovery_plan_round_trips_and_rechecks_its_own_pairing_rule():
    _, request, intake = challenge.inputs(DISCOVERY)
    plan = discovery_plan(request, intake, requirements=discovery_pair())
    stored = planning().validate_stored_question_plan(plan, request=request, intake=intake)
    assert stored == plan
    unpaired = copy.deepcopy(plan)
    unpaired["requirements"][1]["evidence_purpose"] = "context"
    unpaired["plan_digest"] = canonical_digest(
        {key: value for key, value in unpaired.items() if key != "plan_digest"}
    )
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        planning().validate_stored_question_plan(unpaired, request=request, intake=intake)
    older = challenge.challenge_plan(
        request, intake, intent="discovery", requirements=[requirement("only", "context")]
    )
    assert planning().validate_stored_question_plan(older, request=request, intake=intake) == older
    promoted = copy.deepcopy(older)
    promoted["contract_version"] = PLAN_VERSION
    promoted["plan_digest"] = canonical_digest(
        {key: value for key, value in promoted.items() if key != "plan_digest"}
    )
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        planning().validate_stored_question_plan(promoted, request=request, intake=intake)


def test_discovery_plan_with_a_fulfilled_challenge_reads_complete_under_typed_v5_only():
    _, request, intake = challenge.inputs(DISCOVERY)
    plan = discovery_plan(request, intake, requirements=discovery_pair())
    context = challenge.material(
        plan, request, intake, {"r_a": "Commuters in Soweto now share taxis."}
    )
    result = challenge.replay(context, [("quoted", "r_a", 0, "support")])
    intelligence = result["intelligence"]
    texts = answering()._CHALLENGE_TEXT
    assert texts["challenge_completed_without_contradiction"] in intelligence["limitations"]
    assert texts["challenge_incomplete"] not in intelligence["limitations"]
    assert answering()._CHALLENGE_UNPLANNED not in intelligence["missing_work"]
    assert "Challenge requirements: themes_against." in intelligence["limitations"]
    assert intelligence["status"] == "complete"
    assert answering().challenge_state(plan, context["snapshot"], intelligence["claims"]) == {
        "state": "challenge_completed_without_contradiction",
        "challenge_requirement_ids": ["themes_against"],
        "unfulfilled_requirement_ids": [],
        "finding_claim_ids": [],
    }
    with pytest.raises(answering().PlanAdapterMismatch, match="answer_challenge_adapter_required"):
        answering().check_plan_adapter(plan, "typed_v4")
    answering().check_plan_adapter(plan, "typed_v5")
    unfulfilled = challenge.material(
        plan, request, intake, {"r_a": "Commuters in Soweto now share taxis."}, fulfilled=["themes"]
    )
    partial = challenge.replay(unfulfilled, [("quoted", "r_a", 0, "support")])["intelligence"]
    assert partial["status"] == "partial"
    assert texts["challenge_incomplete"] in partial["limitations"]


_CHALLENGE_DRAFT = runtime_tests.challenge_draft


def discovery_draft():
    value = _CHALLENGE_DRAFT()
    value["intent"] = "discovery"
    return value


V1_POLICY = {
    "contract_version": "general_question_runtime_adapter_policy_v1",
    "planning_adapter": "challenge_v5",
    "answer_adapter": "typed_v5",
    "retained_answer_adapter": "typed_v4",
}


def test_runtime_policy_v2_is_live_and_v1_is_retained_for_rollback():
    runtime = runtime_tests.runtime_module()
    assert runtime.RUNTIME_ADAPTER_POLICY is runtime.RUNTIME_ADAPTER_POLICY_V2
    assert runtime.RUNTIME_ADAPTER_POLICY_V2 == {
        "contract_version": "general_question_runtime_adapter_policy_v2",
        "planning_adapter": ADAPTER,
        "answer_adapter": "typed_v5",
        "retained_answer_adapter": "typed_v4",
    }
    assert (
        canonical_digest(runtime.RUNTIME_ADAPTER_POLICY_V2)
        == runtime.RUNTIME_ADAPTER_POLICY_V2_DIGEST
    )
    assert runtime.RUNTIME_ADAPTER_POLICY_V1 == V1_POLICY
    assert runtime._new_answer_adapter({"contract_version": PLAN_VERSION}) == "typed_v5"


@pytest.mark.asyncio
async def test_live_policy_plans_a_discovery_question_with_a_challenge_end_to_end(monkeypatch):
    monkeypatch.setattr(runtime_tests, "challenge_draft", discovery_draft)
    values = await runtime_tests.answering_fixture(monkeypatch, source_window=True)
    store, _, invocation, _, _, answer_draft, _, planning_wire = values
    instruction = planning()._PLANNING_INSTRUCTIONS[ADAPTER]
    sent = planning_wire[-1]["body"]["systemInstruction"]["parts"][0]["text"]
    assert sent == instruction
    assert runtime_tests.recorded_adapters(store, invocation) == (ADAPTER, None, PLAN_VERSION)
    plan = store._objects.read(f"requests/{invocation['request_id']}/plan.json").value
    assert plan["intent"] == "discovery"
    assert [item["evidence_purpose"] for item in plan["requirements"]] == ["support", "challenge"]
    observed = runtime_tests.intercept(monkeypatch, answer_draft)
    result = await runtime_tests.answer(values)
    assert runtime_tests.recorded_adapters(store, invocation) == (ADAPTER, "typed_v5", PLAN_VERSION)
    intelligence = result["response"]["intelligence"]
    texts = answering()._CHALLENGE_TEXT
    assert "Challenge requirements: synthetic_mobility_challenge." in intelligence["limitations"]
    assert texts["challenge_completed_without_contradiction"] in intelligence["limitations"]
    assert answering()._CHALLENGE_UNPLANNED not in intelligence["missing_work"]
    recovered = await runtime_tests.answer(values, now=store_fixture.NOW + timedelta(seconds=181))
    assert recovered == result
    assert len(observed) == 2


@pytest.mark.asyncio
async def test_live_policy_refuses_a_discovery_plan_without_a_challenge(monkeypatch):
    store, _, invocation, identity, credentials = runtime_tests.setup_runtime(
        monkeypatch, source_window=True
    )
    unpaired = discovery_draft()
    unpaired["requirements"] = unpaired["requirements"][:1]
    runtime_tests.intercept(monkeypatch, unpaired)
    with pytest.raises(QuestionStoreError, match="plan_invalid"):
        await runtime_tests.runtime_module().execute_question_planning(
            invocation,
            store=store,
            scope=store_fixture.scope(),
            runtime_identity=identity,
            credentials=credentials,
            persist_usage=lambda event: event,
            now=store_fixture.NOW,
        )
    assert store._objects.read(f"requests/{invocation['request_id']}/plan.json") is None


@pytest.mark.asyncio
async def test_discovery_request_replays_under_its_recorded_adapters_after_rollback_to_v1(
    monkeypatch,
):
    runtime = runtime_tests.runtime_module()
    monkeypatch.setattr(runtime_tests, "challenge_draft", discovery_draft)
    values = await runtime_tests.answering_fixture(monkeypatch, source_window=True)
    store, _, invocation, _, _, answer_draft, _, _ = values
    monkeypatch.setattr(runtime, "RUNTIME_ADAPTER_POLICY", runtime.RUNTIME_ADAPTER_POLICY_V1)
    assert runtime_tests.recorded_adapters(store, invocation) == (ADAPTER, None, PLAN_VERSION)
    observed = runtime_tests.intercept(monkeypatch, answer_draft)
    result = await runtime_tests.answer(values)
    assert runtime_tests.recorded_adapters(store, invocation) == (ADAPTER, "typed_v5", PLAN_VERSION)
    recovered = await runtime_tests.answer(values, now=store_fixture.NOW + timedelta(seconds=181))
    assert recovered == result
    assert len(observed) == 2
