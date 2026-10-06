import copy
import json
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence import general_question_context_admission as profiles
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_plan import (
    build_question_planning_context,
    validate_question_plan,
)
from src.analysis.open_intelligence.general_question_planning import build_question_planning_request
from src.analysis.open_intelligence.general_question_policy import (
    build_intake_context,
    build_question_policy,
    validate_intake_context,
)

from tests.unit.test_general_question_plan import draft, request


def current_request(**kwargs):
    value = request(admitted_at=datetime(2026, 9, 9, 17, 8, 3, 639484, tzinfo=UTC), **kwargs)
    value["policy_digest"] = policy()["policy_digest"]
    value["request_digest"] = canonical_digest(
        {k: v for k, v in value.items() if k != "request_digest"}
    )
    return value


def policy():
    return build_question_policy(pricing_verified_at=datetime(2026, 9, 9, tzinfo=UTC))


def sdk_for(value, intake, **kwargs):
    return build_question_planning_request(
        value, intake, policy=policy(), remaining_seconds=30, **kwargs
    )


def intake_for(value):
    return build_intake_context(value, selected_market="za", source_window=True)


def test_bound_closed_source_window_preserves_truthful_as_of_and_old_default():
    value = current_request()
    legacy = build_intake_context(value, selected_market="za")
    assert (
        build_question_planning_context(value, legacy)["default_observation_window"]["end"]
        == "2026-09-08"
    )
    intake = intake_for(value)
    assert intake["contract_version"] == "general_question_intake_context_v3"
    context = build_question_planning_context(value, intake)
    assert context["default_observation_window"] == {
        "start": "2026-08-25",
        "end": "2026-09-07",
        "closed": True,
    }
    assert context["request"] == value
    assert context["source_window_ceiling"] == "2026-09-07"


def test_null_hint_never_invents_yesterday(monkeypatch):
    monkeypatch.setattr(profiles, "_PROTECTED_CONTEXT_PROFILES", ())
    value = current_request()
    intake = intake_for(value)
    assert intake["source_window_hint"] is None
    assert build_question_planning_context(value, intake)["default_observation_window"] is None


def test_old_hint_remains_exact_after_registry_evolution(monkeypatch):
    value = current_request()
    intake = intake_for(value)
    before = build_question_planning_context(value, intake)
    sdk = sdk_for(value, intake)
    newer = {
        **profiles._PROTECTED_CONTEXT_PROFILES[0],
        "profile_id": "protected_context_20260908_v1",
        "cutoff_date": "2026-09-08",
    }
    monkeypatch.setattr(
        profiles, "_PROTECTED_CONTEXT_PROFILES", (*profiles._PROTECTED_CONTEXT_PROFILES, newer)
    )
    assert intake_for(value)["source_window_hint"]["cutoff_date"] == "2026-09-08"
    assert build_question_planning_context(value, intake) == before
    reconstructed = sdk_for(value, intake)
    assert (
        sdk.contents,
        sdk.input_digest,
        sdk.system_instruction_digest,
        sdk.response_schema_digest,
    ) == (
        reconstructed.contents,
        reconstructed.input_digest,
        reconstructed.system_instruction_digest,
        reconstructed.response_schema_digest,
    )
    broken = copy.deepcopy(intake)
    broken["source_window_hint"]["result_digest"] = "f" * 64
    broken["intake_digest"] = canonical_digest(
        {k: v for k, v in broken.items() if k != "intake_digest"}
    )
    with pytest.raises(ValueError):
        validate_intake_context(broken, request=value)


@pytest.mark.parametrize("empty", [False, True])
def test_explicit_uncovered_window_is_never_clamped(monkeypatch, empty):
    if empty:
        monkeypatch.setattr(profiles, "_PROTECTED_CONTEXT_PROFILES", ())
    value = current_request(requested_window={"start": "2026-09-01", "end": "2026-09-08"})
    intake = intake_for(value)
    expected = {"start": "2026-09-01", "end": "2026-09-08", "closed": True}
    assert json.loads(sdk_for(value, intake).contents)["default_observation_window"] == expected
    proposed = draft()
    proposed["window"] = expected
    assert validate_question_plan(proposed, request=value, intake=intake)["window"] == expected


@pytest.mark.parametrize(
    "question,start,end",
    [
        ("Investigate 1 to 8 September 2026.", "2026-09-01", "2026-09-08"),
        ("Investigate the past seven closed calendar days.", "2026-09-02", "2026-09-08"),
        ("Investigate the latest available week.", "2026-09-01", "2026-09-07"),
    ],
)
def test_controlled_explicit_and_relative_plans_remain_exact(question, start, end):
    value = current_request(question=question)
    intake = intake_for(value)
    proposed = draft()
    proposed["window"] = {"start": start, "end": end, "closed": True}
    assert (
        validate_question_plan(proposed, request=value, intake=intake)["window"]
        == proposed["window"]
    )
    instruction = sdk_for(value, intake).generation_config.system_instruction
    assert "never clamp or intersect" in instruction
    assert "truthful request.as_of" in instruction


def test_null_hint_controlled_clarification_and_adapter_binding(monkeypatch):
    monkeypatch.setattr(profiles, "_PROTECTED_CONTEXT_PROFILES", ())
    value = current_request()
    intake = intake_for(value)
    sdk = sdk_for(value, intake)
    assert json.loads(sdk.contents)["default_observation_window"] is None
    proposed = draft()
    proposed.update(
        status="needs_clarification",
        window=None,
        requirements=[],
        clarification="No supported default observation window is available. Please specify the dates to investigate.",
    )
    assert validate_question_plan(proposed, request=value, intake=intake)["window"] is None
    for adapter in ("v1", "continuity_v2", "parent_context_v3"):
        with pytest.raises(ValueError, match="planning binding"):
            sdk_for(value, intake, _adapter=adapter)
    legacy = build_intake_context(value, selected_market="za")
    with pytest.raises(ValueError, match="planning binding"):
        sdk_for(value, legacy, _adapter="source_window_v4")


@pytest.mark.parametrize("cutoff", ["2026-09-09", "2026-09-10"])
def test_nonclosed_profile_cannot_supply_default(monkeypatch, cutoff):
    profile = {**profiles._PROTECTED_CONTEXT_PROFILES[0], "cutoff_date": cutoff}
    monkeypatch.setattr(profiles, "_PROTECTED_CONTEXT_PROFILES", (profile,))
    assert intake_for(current_request())["source_window_hint"] is None


def test_parent_frame_wins_and_dynamic_alias_schema_is_retained():
    from src.analysis.open_intelligence.general_question_parent_capsule import (
        REF_VERSION,
        build_capsule,
    )

    from tests.unit.test_general_question_parent_context import capsule_bundle

    value = current_request()
    parent = capsule_bundle()
    parent["plan"]["window"] = {"start": "2026-09-01", "end": "2026-09-07", "closed": True}
    capsule = build_capsule(value, parent)
    reference = {
        "contract_version": REF_VERSION,
        "parent": capsule["parent"],
        "anchor": capsule["anchor"],
        "context_digest": canonical_digest(capsule),
        "context_generation": "1",
    }
    intake = build_intake_context(
        value, selected_market="za", parent_context_ref=reference, source_window=True
    )
    sdk = sdk_for(value, intake, parent_context=capsule)
    assert json.loads(sdk.contents)["default_observation_window"] == parent["plan"]["window"]
    assert "parent_receipt_aliases" in sdk.generation_config.response_schema["required"]


@pytest.mark.parametrize("orphan", [False, True])
def test_admission_retry_preserves_null_hint_after_registry_evolution(monkeypatch, orphan):
    from uuid import UUID

    from tests.unit import test_general_question_admission as admission
    from tests.unit import test_general_question_store as store_fixture

    store, bucket, invocation, runtime, _ = admission.deployment_fixture.fixture()
    request_id = str(UUID(int=71))
    first = admission.admit(store, invocation, runtime, request_id=request_id)
    current = store.read_request(request_id, scope=store_fixture.scope())
    assert current["intake"]["source_window_hint"] is None
    if orphan:
        control = json.loads(bucket.objects[store_fixture.PREFIX + store_fixture.LEDGER][1])
        control["requests"].pop(request_id)
        control["reserved_microusd"] -= 100000
        bucket.seed(store_fixture.LEDGER, control)
    profile = {
        **profiles._PROTECTED_CONTEXT_PROFILES[0],
        "cutoff_date": "2026-09-05",
        "profile_id": "protected_context_20260905_v1",
    }
    monkeypatch.setattr(
        profiles, "_PROTECTED_CONTEXT_PROFILES", (*profiles._PROTECTED_CONTEXT_PROFILES, profile)
    )
    second = admission.admit(store, invocation, runtime, request_id=request_id)
    assert first == second
    assert (
        store.read_request(request_id, scope=store_fixture.scope())["intake"] == current["intake"]
    )
    assert (
        json.loads(bucket.objects[store_fixture.PREFIX + store_fixture.LEDGER][1])[
            "reserved_microusd"
        ]
        == 200000
    )


@pytest.mark.asyncio
async def test_null_hint_clarification_publishes_without_retrieval_or_answer(monkeypatch):
    from uuid import UUID

    from tests.unit import test_general_question_admission as admission
    from tests.unit import test_general_question_execution as execution
    from tests.unit import test_general_question_runtime as runtime
    from tests.unit import test_general_question_store as stored

    monkeypatch.setattr(profiles, "_PROTECTED_CONTEXT_PROFILES", ())
    store, bucket, invocation, identity, credentials = runtime.setup_runtime()
    reply = admission.admit(store, invocation, identity, request_id=str(UUID(int=73)))
    invocation = reply["invocation"]
    value = runtime.plan_draft()
    value.update(
        status="needs_clarification",
        window=None,
        requirements=[],
        clarification="No supported default observation window is available. Please specify the dates to investigate.",
    )
    observed = runtime.intercept(monkeypatch, value)
    monkeypatch.setattr(
        execution.module(),
        "build_general_question_snapshot",
        lambda *a, **k: pytest.fail("source call"),
    )
    monkeypatch.setattr(
        execution.module(), "execute_question_answering", lambda *a, **k: pytest.fail("answer call")
    )
    result = await execution.execute((store, bucket, invocation, identity, credentials))
    assert result["state"] == "needs_clarification"
    record = store._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    ).value
    assert record["response"]["answer"] == value["clarification"]
    assert record["response"]["intelligence"]["usage"]["model_calls"] == 1
    assert record["response"]["intelligence"]["resolved_scope"]["market_scope"] == value["markets"]
    assert len(observed) == 2
    ledger = json.loads(bucket.objects[stored.PREFIX + stored.LEDGER][1])
    assert not ledger["requests"][invocation["request_id"]]["execution"].get("queries")
    from src.analysis.open_intelligence import general_question_result as results
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    context = store.read_request(invocation["request_id"], scope=stored.scope())
    plan_key = f"requests/{invocation['request_id']}/plan.json"
    plan = store._objects.read(plan_key).value
    for mutation in (
        "missing_plan",
        "wrong_state",
        "nonnull_plan",
        "legacy_intake",
        "clarification",
        "scope",
    ):
        changed = copy.deepcopy(record)
        checked_context = copy.deepcopy(context)
        if mutation == "missing_plan":
            changed["plan_digest"] = None
        elif mutation == "wrong_state":
            changed["state"] = "unavailable"
            changed["response"]["intelligence"]["status"] = "unavailable"
        elif mutation == "legacy_intake":
            checked_context["intake"] = build_intake_context(
                context["request"], selected_market="za"
            )
        elif mutation == "clarification":
            changed["response"]["answer"] = "Unbound clarification"
        elif mutation == "scope":
            changed["response"]["intelligence"]["resolved_scope"]["market_scope"] = ["ng"]
        else:
            altered = copy.deepcopy(plan)
            altered["window"] = {"start": "2026-09-01", "end": "2026-09-05", "closed": True}
            altered["plan_digest"] = canonical_digest(
                {k: v for k, v in altered.items() if k != "plan_digest"}
            )
            bucket.seed(plan_key, altered)
            changed["plan_digest"] = altered["plan_digest"]
        with pytest.raises((ValueError, QuestionStoreError)):
            results._validate_record(store, checked_context, changed, None)
        bucket.seed(plan_key, plan)


@pytest.mark.parametrize("orphan", [False, True])
def test_intake_recovery_lookup_occurs_only_for_orphan_request(monkeypatch, orphan):
    from uuid import UUID

    from src.analysis.open_intelligence.general_question_request import normalize_question_request

    from tests.unit import test_general_question_admission as admission
    from tests.unit import test_general_question_store as stored

    store, bucket, invocation, identity, _ = admission.deployment_fixture.fixture()
    request_id = str(UUID(int=74))
    if orphan:
        value = normalize_question_request(
            {"message": "Explain mobility question 1"},
            scope=stored.scope(),
            request_id=request_id,
            admitted_at=stored.NOW,
            policy_digest=invocation["policy_digest"],
        )
        bucket.seed(f"requests/{request_id}/request.json", value)
    reads = []
    get = bucket.get_blob
    monkeypatch.setattr(
        bucket,
        "get_blob",
        lambda name, **kw: reads.append((name, kw.get("generation"))) or get(name, **kw),
    )
    admission.admit(store, invocation, identity, request_id=request_id)
    lookups = [
        name
        for name, generation in reads
        if name.endswith(f"{request_id}/intake.json") and generation is None
    ]
    assert len(lookups) == int(orphan)
