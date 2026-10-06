import copy
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_policy import (
    build_question_policy,
    require_question_admission_policy,
    validate_question_policy,
)
from src.analysis.open_intelligence.general_question_store import (
    GeneralQuestionStore,
    QuestionStoreError,
)

from tests.unit import test_general_question_calls as calls_fixture
from tests.unit import test_general_question_runtime as runtime_fixture
from tests.unit import test_general_question_store as f

CURRENT_CONTRACT = "2885012a99bfc352c9ad92ced7b5bddd0b022f88e8ddc9d7688f237179062a8b"
HISTORICAL_CONTRACTS = (
    ("a0410e47911ff343ef77726e4ffefbfd57f275e9cf341633a0a9fd1b01e506e0", "MEDIUM", "HIGH"),
    ("99c597f7a083ea14d4c87c34bbccdb6d5cea5958ba466352891b8d06731c4389", "LOW", "HIGH"),
    ("1306582d25499311bd70c59e1e25804b5219f7494a30a18a59b03c9ad3d474c5", "LOW", "MEDIUM"),
    ("519a658ad91b3b4d447298611d259421bbac5b7765d7c0f0049dc8bdd08c5857", "LOW", "LOW"),
)


def reseal(policy):
    policy["policy_digest"] = canonical_digest(
        {key: value for key, value in policy.items() if key != "policy_digest"}
    )
    return policy


def activate_current(store, bucket):
    policy = build_question_policy(pricing_verified_at=f.NOW)
    _, control = store._control()
    control["bindings"]["e" * 64] = policy["policy_digest"]
    control["active_deployment_digest"] = "e" * 64
    bucket.seed(f"policies/{policy['policy_digest']}/policy.json", policy)
    bucket.seed(f.LEDGER, control)
    return GeneralQuestionStore(bucket, policy=policy, deployment_digest="e" * 64)


def test_new_question_policy_selects_38_with_fixed_limits_and_pricing():
    policy = build_question_policy(pricing_verified_at=f.NOW)
    assert policy["model"] == "gemini-3.8-flash"
    assert policy["approval_contract_digest"] == CURRENT_CONTRACT
    assert policy["pricing"]["input_usd_per_million"] == "0.750000"
    assert policy["pricing"]["output_usd_per_million"] == "3.750000"
    assert [stage["thinking_level"] for stage in policy["stages"].values()] == ["LOW", "LOW"]
    assert require_question_admission_policy(policy, now=f.NOW) == policy


@pytest.mark.parametrize("contract,planning,answering", HISTORICAL_CONTRACTS)
@pytest.mark.parametrize("deadline", [180, 240])
def test_historical_policy_model_stages_and_deadlines_remain_exact(
    contract, planning, answering, deadline
):
    policy = build_question_policy(pricing_verified_at=f.NOW, approval_contract_digest=contract)
    policy["limits"]["deadline_seconds"] = deadline
    reseal(policy)
    before = copy.deepcopy(policy)
    assert validate_question_policy(policy) == before
    assert policy["model"] == "gemini-3.5-flash"
    assert policy["pricing"]["output_usd_per_million"] == "9.000000"
    assert policy["stages"]["planning"]["thinking_level"] == planning
    assert policy["stages"]["answering"]["thinking_level"] == answering
    policy["model"] = "gemini-3.8-flash"
    with pytest.raises(ValueError):
        validate_question_policy(reseal(policy))


@pytest.mark.parametrize("model", ["gemini-3.5-flash", "gemini-3.7-flash", "unknown"])
def test_new_contract_refuses_another_model(model):
    policy = build_question_policy(pricing_verified_at=f.NOW)
    policy["model"] = model
    with pytest.raises(ValueError):
        validate_question_policy(reseal(policy))


def test_new_model_contract_cannot_reuse_historical_180_second_deadline():
    policy = build_question_policy(pricing_verified_at=f.NOW)
    policy["limits"]["deadline_seconds"] = 180
    with pytest.raises(ValueError, match="limits"):
        validate_question_policy(reseal(policy))


def test_new_call_claim_uses_38_and_refuses_crossed_legacy_model(monkeypatch):
    monkeypatch.setattr(f, "build_question_policy", build_question_policy)
    _, calls, bucket, rid = calls_fixture.setup()
    permit = calls_fixture.claim(calls, rid)
    assert permit.model == "gemini-3.8-flash"
    _, control = calls.store._control()
    control["requests"][rid]["execution"]["calls"]["planning"]["model"] = "gemini-3.5-flash"
    bucket.seed(f.LEDGER, control)
    with pytest.raises(QuestionStoreError, match="control_invalid"):
        calls.store._control()


def test_mixed_ledger_keeps_legacy_unknown_call_and_allowance(monkeypatch):
    _, calls, bucket, rid = calls_fixture.setup()
    calls_fixture.claim(calls, rid)
    _, old = calls.store._control()
    current = activate_current(calls.store, bucket)
    monkeypatch.setattr(f, "build_question_policy", build_question_policy)
    _, request, intake = f.prepared(2)
    current.admit(request, intake, scope=f.scope(), now=f.NOW)
    _, after = current._control()
    assert after["requests"][rid] == old["requests"][rid]
    assert after["requests"][rid]["execution"]["calls"]["planning"]["usage_status"] == "unknown"
    assert after["reserved_microusd"] == 200000
    with pytest.raises(QuestionStoreError, match="call_already_started"):
        calls_fixture.claim(type(calls)(current), rid)


@pytest.mark.asyncio
async def test_completed_35_answer_reconstructs_with_original_policy_after_38_activation(
    monkeypatch,
):
    values = await runtime_fixture.answering_fixture(monkeypatch)
    store, bucket, invocation, _, _, draft, _, _ = values
    wire = runtime_fixture.intercept(monkeypatch, draft)
    result = await runtime_fixture.answer(values)
    before = copy.deepcopy(bucket.objects)
    current = activate_current(store, bucket)
    context = store.read_request(invocation["request_id"], scope=f.scope())
    prefix = f"requests/{invocation['request_id']}/"
    plan = store._objects.read(prefix + "plan.json").value
    snapshot = store._objects.read(prefix + "snapshot.json").value
    uploads = list(bucket.uploads)
    monkeypatch.setattr(
        socket.socket, "connect", lambda *_: pytest.fail("network during historical read")
    )
    actual = runtime_fixture.runtime_module().validate_persisted_question_answer(
        result["response"],
        store=current,
        scope=f.scope(),
        request=context["request"],
        plan=plan,
        snapshot=snapshot,
        validate_snapshot=lambda value, **_: value,
    )
    assert actual == result["response"]
    assert len(wire) == 2
    assert bucket.uploads == uploads
    assert all(
        bucket.objects[key] == value for key, value in before.items() if key != f.PREFIX + f.LEDGER
    )


@pytest.mark.asyncio
async def test_new_38_planning_and_answering_preserve_usage_and_structured_projection(monkeypatch):
    monkeypatch.setattr(f, "build_question_policy", build_question_policy)
    original_intercept = runtime_fixture.intercept
    monkeypatch.setattr(
        runtime_fixture,
        "intercept",
        lambda mp, draft, **kwargs: original_intercept(
            mp, draft, model="gemini-3.8-flash", **kwargs
        ),
    )
    values = await runtime_fixture.answering_fixture(monkeypatch)
    store, _, invocation, _, _, draft, usage, planning_wire = values
    answering_wire = runtime_fixture.intercept(monkeypatch, draft)
    result = await runtime_fixture.answer(values)
    assert result["response"]["intelligence"]["claims"]
    assert len(usage) == 2
    assert all(event.gemini_model == "gemini-3.8-flash" for event in usage)
    assert all("gemini-3.8-flash:" in item["path"] for item in planning_wire + answering_wire)
    _, control = store._control()
    calls = control["requests"][invocation["request_id"]]["execution"]["calls"]
    assert set(calls) == {"planning", "answering"}
    assert all(call["usage_status"] == "acknowledged" for call in calls.values())


def test_retained_06011_answer_reads_exactly_under_active_38_without_network_or_writes(monkeypatch):
    from src.analysis.open_intelligence.general_question_execution import (
        question_answer_validators,
        read_general_question_status,
    )

    ops = Path(
        os.environ.get(
            "OPEN_INTELLIGENCE_RETAINED_FIXTURES",
            Path(__file__).resolve().parents[1] / "fixtures/retained-questions",
        )
    )
    rid = "06011d23-6fd8-4942-96fa-c461bf09b325"
    directory = ops / ("question-request-" + rid)
    if not (directory / "listing.json").is_file():
        pytest.skip("Retained request artifacts are not installed on this host")
    listing = json.loads((directory / "listing.json").read_bytes())
    bucket = f.Bucket()
    for item in listing["items"]:
        name, generation = item["name"], int(item["generation"])
        raw = (directory / name.split("/requests/" + rid + "/", 1)[1]).read_bytes()
        bucket.objects[name] = (generation, raw)
        bucket.versions[(name, generation)] = raw
    ledger = json.loads((directory / "ledger-1788966407246392.json").read_bytes())
    oldrow = copy.deepcopy(ledger["requests"][rid])
    ledger["requests"] = {rid: oldrow}
    ledger["reserved_microusd"] = 100000
    oldpolicy = json.loads((ops / "QUESTION_REFRESH_ACTIVATION_353ca51.json").read_bytes())[
        "policy"
    ]
    assert oldpolicy["policy_digest"] == oldrow["policy_digest"]
    newpolicy = build_question_policy(pricing_verified_at=datetime.now(UTC))
    assert newpolicy["model"] == "gemini-3.8-flash"
    ledger["bindings"] = {
        oldrow["deployment_digest"]: oldrow["policy_digest"],
        "e" * 64: newpolicy["policy_digest"],
    }
    ledger["active_deployment_digest"] = "e" * 64
    for policy in (oldpolicy, newpolicy):
        bucket.seed(f"policies/{policy['policy_digest']}/policy.json", policy)
    bucket.seed(f.LEDGER, ledger)
    store = GeneralQuestionStore(bucket, policy=newpolicy, deployment_digest="e" * 64)
    request = json.loads((directory / "request.json").read_bytes())
    scope = {key: request[key] for key in f.scope()}
    original = json.loads(next((directory / "results").glob("*.json")).read_bytes())
    plan = json.loads((directory / "plan.json").read_bytes())
    snapshot = json.loads((directory / "snapshot.json").read_bytes())
    before = copy.deepcopy(bucket.objects)
    monkeypatch.setattr(
        socket.socket, "connect", lambda *_: pytest.fail("historical replay network")
    )
    monkeypatch.setattr(bucket, "blob", lambda *_: pytest.fail("historical replay write"))
    status = read_general_question_status(rid, store=store, scope=scope, now=datetime.now(UTC))
    assert status["state"] == "complete"
    _, validator = question_answer_validators(store, scope)
    assert (
        validator(original["response"], request=request, plan=plan, snapshot=snapshot)
        == original["response"]
    )
    assert bucket.objects == before
    assert bucket.uploads == []
