import copy
import importlib
import json
import socket
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from google.auth.credentials import AnonymousCredentials
from google.genai import types
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_store import QuestionStoreError

from tests.unit import test_general_question_deployment as deployment_fixture
from tests.unit import test_general_question_store as store_fixture

PREPARED = store_fixture.prepared


def source_window_prepared(index=1):
    from src.analysis.open_intelligence.general_question_policy import build_intake_context

    policy, request, _ = PREPARED(index)
    return policy, request, build_intake_context(request, selected_market="za", source_window=True)


def answer_module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_answer")


def planning_module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_planning")


def recorded_adapters(store, invocation):
    context = store.read_request(invocation["request_id"], scope=store_fixture.scope())
    calls = context["admission"]["execution"]["calls"]
    planned = planning_module().planning_adapter_id(
        calls["planning"]["system_instruction_digest"], calls["planning"]["response_schema_digest"]
    )
    answered = (
        None
        if calls.get("answering") is None
        else answer_module().answer_adapter_id(
            calls["answering"]["response_schema_digest"],
            calls["answering"]["system_instruction_digest"],
        )
    )
    plan = store._objects.read(f"requests/{invocation['request_id']}/plan.json").value
    return planned, answered, plan["contract_version"]


async def answering_fixture(monkeypatch, *, source_window=False):
    from tests.unit import test_general_question_answer as answer_fixture

    store, bucket, invocation, identity, credentials = setup_runtime(
        monkeypatch, source_window=source_window
    )
    planning_wire = intercept(monkeypatch, challenge_draft() if source_window else plan_draft())
    usage = []
    plan = await runtime_module().execute_question_planning(
        invocation,
        store=store,
        scope=store_fixture.scope(),
        runtime_identity=identity,
        credentials=credentials,
        persist_usage=lambda event: usage.append(event) or event,
        now=store_fixture.NOW,
    )
    context = store.read_request(invocation["request_id"], scope=store_fixture.scope())
    values = answer_fixture.fixture()
    snapshot = copy.deepcopy(values[3])
    snapshot.update(
        request_id=invocation["request_id"],
        request_digest=invocation["request_digest"],
        intake_digest=invocation["intake_digest"],
        policy_digest=invocation["policy_digest"],
        deployment_digest=invocation["deployment_digest"],
        plan_digest=plan["plan_digest"],
        window=plan["window"],
        as_of=context["request"]["as_of"],
        fulfilled_requirement_ids=[item["requirement_id"] for item in plan["requirements"]],
    )
    for reading in snapshot["readings"]:
        reading["window"] = plan["window"]
    snapshot["snapshot_digest"] = canonical_digest(
        {k: v for k, v in snapshot.items() if k != "snapshot_digest"}
    )
    bucket.seed(f"requests/{invocation['request_id']}/snapshot.json", snapshot)
    draft = answer_fixture.typed_fixture(snapshot, boundary_codes=True)
    if source_window:
        for field in ("quote_observations", "reading_observations"):
            for item in draft[field]:
                item["evidence_purpose"] = "support"
    return store, bucket, invocation, identity, credentials, draft, usage, planning_wire


@pytest.mark.asyncio
async def test_new_source_window_request_routes_discovery_challenge_v6_and_typed_v5_and_replays(
    monkeypatch,
):
    values = await answering_fixture(monkeypatch, source_window=True)
    store, _, invocation, _, _, draft, _, _ = values
    assert recorded_adapters(store, invocation) == (
        "discovery_challenge_v6",
        None,
        "general_question_plan_v4",
    )
    observed = intercept(monkeypatch, draft)
    result = await answer(values)
    assert recorded_adapters(store, invocation) == (
        "discovery_challenge_v6",
        "typed_v5",
        "general_question_plan_v4",
    )
    intelligence = result["response"]["intelligence"]
    texts = answer_module()._CHALLENGE_TEXT
    assert texts["challenge_completed_without_contradiction"] in intelligence["limitations"]
    assert "Challenge requirements: synthetic_mobility_challenge." in intelligence["limitations"]
    assert intelligence["receipts"]
    assert all("evidence_purposes" in row for row in intelligence["receipts"])
    recovered = await answer(values, now=store_fixture.NOW + timedelta(seconds=181))
    assert recovered == result
    assert len(observed) == 2


@pytest.mark.asyncio
async def test_legacy_intake_keeps_its_retained_planning_and_answer_adapters(monkeypatch):
    values = await answering_fixture(monkeypatch)
    store, _, invocation, _, _, draft, _, _ = values
    assert recorded_adapters(store, invocation) == (
        "continuity_v2",
        None,
        "general_question_plan_v1",
    )
    intercept(monkeypatch, draft)
    result = await answer(values)
    assert recorded_adapters(store, invocation) == (
        "continuity_v2",
        "typed_v4",
        "general_question_plan_v1",
    )
    intelligence = result["response"]["intelligence"]
    assert not any("Challenge" in text for text in intelligence["limitations"])
    assert all(
        "evidence_purposes" not in row and "quote_bindings" not in row
        for row in intelligence["receipts"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_window, adapter, code",
    [
        (True, "typed_v4", "answer_challenge_adapter_required"),
        (False, "typed_v5", "answer_challenge_plan_required"),
    ],
)
async def test_plan_version_and_answer_adapter_refuse_each_other_before_any_count(
    monkeypatch, source_window, adapter, code
):
    values = await answering_fixture(monkeypatch, source_window=source_window)
    observed = intercept(monkeypatch, values[5])
    runtime = runtime_module()
    original = runtime.build_question_answering_request
    monkeypatch.setattr(
        runtime,
        "build_question_answering_request",
        lambda *args, **kwargs: original(*args, **{**kwargs, "_adapter": adapter}),
    )
    with pytest.raises(QuestionStoreError, match=code):
        await answer(values)
    assert observed == []


def test_runtime_adapter_policy_is_one_named_versioned_place():
    runtime = runtime_module()
    policy = runtime.RUNTIME_ADAPTER_POLICY
    assert policy == {
        "contract_version": "general_question_runtime_adapter_policy_v2",
        "planning_adapter": "discovery_challenge_v6",
        "answer_adapter": "typed_v5",
        "retained_answer_adapter": "typed_v4",
    }
    assert canonical_digest(policy) == runtime.RUNTIME_ADAPTER_POLICY_DIGEST
    current = {"contract_version": "general_question_intake_context_v3"}
    assert runtime._planning_adapter(current) == "discovery_challenge_v6"
    for version in ("general_question_intake_context_v1", "general_question_intake_context_v2"):
        assert runtime._planning_adapter({"contract_version": version}) is None
    assert runtime._new_answer_adapter({"contract_version": "general_question_plan_v3"}) == (
        "typed_v5"
    )
    for version in ("general_question_plan_v1", "general_question_plan_v2"):
        assert runtime._new_answer_adapter({"contract_version": version}) == "typed_v4"


async def answer(
    values, *, validator=lambda snapshot, **_: snapshot, now=store_fixture.NOW, persist=None
):
    store, _, invocation, identity, credentials, _, usage, _ = values
    return await runtime_module().execute_question_answering(
        invocation,
        store=store,
        scope=store_fixture.scope(),
        runtime_identity=identity,
        credentials=credentials,
        persist_usage=persist or (lambda event: usage.append(event) or event),
        now=now,
        validate_snapshot=validator,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["compact", "span_v1", "span_v2", "span_v3", "typed_v1"])
async def test_stored_historical_adapter_recovery_uses_exact_pair_without_resubmission(
    monkeypatch, adapter
):
    from tests.unit import test_general_question_answer as answer_fixture

    values = await answering_fixture(monkeypatch)
    store, _, invocation, _, _, _, _, _ = values
    snapshot = store._objects.read(f"requests/{invocation['request_id']}/snapshot.json").value
    if adapter == "typed_v1":
        draft = answer_fixture.typed_fixture(snapshot)
        for field in ("quote_observations", "reading_observations"):
            for item in draft[field]:
                item["limitations"] = ["Historical provider limitation."]
    else:
        draft = copy.deepcopy(answer_fixture.fixture()[5])
        sections = {cid: s["kind"] for s in draft["sections"] for cid in s["claim_ids"]}
        for claim in draft["claims"]:
            claim["receipt_ids"] = ["r1" for _ in claim["receipt_ids"]]
            for segment in claim["segments"]:
                if segment["kind"] == "quote":
                    segment["receipt_id"] = "r1"
                    if adapter != "compact":
                        option = answer_fixture.module()._quote_spans(snapshot)["receipt_tools"][0]
                        segment.clear()
                        segment.update(
                            kind="quote_span", receipt_id="r1", span_id=option["span_id"]
                        )
            if adapter != "compact":
                claim["section"] = sections[claim["claim_id"]]
            if adapter in ("span_v2", "span_v3"):
                claim.pop("receipt_ids")
                claim.pop("reading_ids")
        if adapter != "compact":
            draft.pop("sections")
    observed = intercept(monkeypatch, draft)
    original = runtime_module().build_question_answering_request

    def historical(*args, **kwargs):
        return original(*args, **{**kwargs, "_adapter": adapter})

    monkeypatch.setattr(runtime_module(), "build_question_answering_request", historical)
    result = await answer(values)
    monkeypatch.setattr(runtime_module(), "build_question_answering_request", original)
    recovered = await answer(values, now=store_fixture.NOW + timedelta(seconds=181))
    assert recovered == result
    assert len(observed) == 2
    claim = next(
        c for c in result["response"]["intelligence"]["claims"] if c["claim_id"] == "interpret"
    )
    assert claim["text"].startswith("Hypothesis:") == (adapter in ("span_v3", "typed_v1"))
    if adapter == "typed_v1":
        observed_claim = next(
            c for c in result["response"]["intelligence"]["claims"] if c["claim_id"] == "observed"
        )
        assert observed_claim["limitations"] == ["Historical provider limitation."]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["input_digest", "crossed_pair", "unknown_pair"])
async def test_stored_adapter_binding_is_rejected_before_hydration(monkeypatch, tamper):
    from tests.unit import test_general_question_answer as answer_fixture

    values = await answering_fixture(monkeypatch)
    observed = intercept(monkeypatch, values[5])
    await answer(values)
    store, _, invocation, _, _, _, _, _ = values
    context = store.read_request(invocation["request_id"], scope=store_fixture.scope())
    plan = store._objects.read(f"requests/{invocation['request_id']}/plan.json").value
    snapshot = store._objects.read(f"requests/{invocation['request_id']}/snapshot.json").value
    calls = runtime_module().GeneralQuestionCalls(store)
    sdk = runtime_module()._answer_request(calls, store, context, plan, snapshot, 60)
    raw = calls.read_response(
        invocation["request_id"], stage="answering", scope=store_fixture.scope()
    )
    changed = copy.deepcopy(context)
    call = changed["admission"]["execution"]["calls"]["answering"]
    if tamper == "input_digest":
        call["input_digest"] = "0" * 64
    elif tamper == "crossed_pair":
        call["system_instruction_digest"] = answer_fixture.HISTORICAL_ADAPTERS[-1][3]
    else:
        call["response_schema_digest"] = "0" * 64
    sdk = replace(
        sdk,
        input_digest=call["input_digest"],
        response_schema_digest=call["response_schema_digest"],
        system_instruction_digest=call["system_instruction_digest"],
    )
    original = runtime_module().GeneralQuestionCalls._context

    def altered_context(self, *args, **kwargs):
        _, plan_record, snapshot_record = original(self, *args, **kwargs)
        return changed, plan_record, snapshot_record

    monkeypatch.setattr(runtime_module().GeneralQuestionCalls, "_context", altered_context)
    monkeypatch.setattr(
        runtime_module(),
        "_hydrate_typed_answer",
        lambda *a, **kw: pytest.fail("Hydrated an unbound response"),
    )
    with pytest.raises(QuestionStoreError, match="answer_binding_invalid"):
        runtime_module()._project_stored_answer(
            store, store_fixture.scope(), changed, plan, snapshot, raw, sdk
        )
    assert len(observed) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile", ["current", "legacy_low_high", "legacy_medium_high", "legacy_low_medium"]
)
async def test_actual_answer_call_shares_planning_budget_and_recovery_never_resubmits(
    monkeypatch, profile
):
    if profile != "current":
        original = store_fixture.build_question_policy

        def historical(**kwargs):
            policy = original(**kwargs)
            policy["approval_contract_digest"] = (
                "99c597f7a083ea14d4c87c34bbccdb6d5cea5958ba466352891b8d06731c4389"
                if profile == "legacy_low_high"
                else "a0410e47911ff343ef77726e4ffefbfd57f275e9cf341633a0a9fd1b01e506e0"
            )
            policy["stages"]["planning"]["thinking_level"] = (
                "LOW" if profile == "legacy_low_high" else "MEDIUM"
            )
            policy["stages"]["answering"]["thinking_level"] = "HIGH"
            if profile == "legacy_low_medium":
                policy["approval_contract_digest"] = (
                    "1306582d25499311bd70c59e1e25804b5219f7494a30a18a59b03c9ad3d474c5"
                )
                policy["stages"]["planning"]["thinking_level"] = "LOW"
                policy["stages"]["answering"]["thinking_level"] = "MEDIUM"
            policy["policy_digest"] = canonical_digest(
                {k: v for k, v in policy.items() if k != "policy_digest"}
            )
            return policy

        monkeypatch.setattr(store_fixture, "build_question_policy", historical)
    values = await answering_fixture(monkeypatch)
    store, bucket, invocation, _, _, draft, usage, planning_wire = values
    observed = intercept(monkeypatch, draft)
    result = await answer(values)
    persisted = (
        runtime_module()
        .GeneralQuestionCalls(store)
        .read_response(invocation["request_id"], stage="answering", scope=store_fixture.scope())
    )
    assert runtime_module()._draft_from_snapshot(persisted) == draft
    assert "claims" not in draft
    assert set(draft) == {
        "quote_observations",
        "reading_observations",
        "interpretations",
        "inferences",
        "answer",
        "proposals",
    }
    assert set(result) == {"response", "state", "plan_digest", "snapshot_digest"}
    assert result["state"] == "complete"
    assert result["response"]["intelligence"]["usage"]["model_calls"] == 2
    assert [event.stage for event in usage] == ["planning", "answering"]
    assert len(planning_wire) == len(observed) == 2
    assert observed[0]["body"]["contents"] == observed[1]["body"]["contents"]
    assert observed[0]["body"]["generationConfig"] == observed[1]["body"]["generationConfig"]
    prompt = json.loads(observed[1]["body"]["contents"][0]["parts"][0]["text"])
    assert prompt["remaining_input_tokens"] == 31950
    assert prompt["remaining_output_tokens"] == 3985
    prefix = store_fixture.PREFIX + f"requests/{invocation['request_id']}/"
    uploaded = [key for key, _ in bucket.uploads]
    assert uploaded.index(prefix + "calls/answering.json") < uploaded.index(
        prefix + "usage/answering.json"
    )
    recovered = await answer(
        values,
        now=store_fixture.NOW + timedelta(seconds=181),
        persist=lambda event: pytest.fail("usage acknowledged twice"),
    )
    assert recovered == result
    assert len(observed) == 2
    assert (
        store.read_request(invocation["request_id"], scope=store_fixture.scope())["admission"][
            "execution"
        ]["calls"]["answering"]["max_output_tokens"]
        == 3985
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["source_missing", "source_changed", "expired", "terminal"])
async def test_answer_authority_failures_prevent_even_count_request(monkeypatch, failure):
    values = await answering_fixture(monkeypatch)
    store, _, invocation, _, _, draft, _, _ = values
    observed = intercept(monkeypatch, draft)

    def validator(snapshot, **_):
        return snapshot

    now = store_fixture.NOW
    if failure == "source_missing":
        validator = None
    elif failure == "source_changed":

        def validator(snapshot, **_):
            return {**snapshot, "snapshot_id": "changed"}
    elif failure == "expired":
        now += timedelta(seconds=241)
    else:
        from src.analysis.open_intelligence.general_question_result import (
            observed_result_usage,
            unavailable_response,
        )

        request = store.read_request(invocation["request_id"], scope=store_fixture.scope())[
            "request"
        ]
        reply = unavailable_response(
            request,
            observed_result_usage(
                store, request_id=invocation["request_id"], scope=store_fixture.scope()
            ),
            reason="source_unavailable",
        )
        store.publish_result(
            invocation["request_id"],
            scope=store_fixture.scope(),
            response=reply,
            state="unavailable",
            now=now,
        )
    with pytest.raises(QuestionStoreError):
        await answer(values, validator=validator, now=now)
    assert observed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_json", "unknown_usage", "safety"])
async def test_paid_answer_failure_is_captured_and_never_repaired_with_generation(
    monkeypatch, failure
):
    values = await answering_fixture(monkeypatch)
    draft = "bad json" if failure == "invalid_json" else values[5]
    observed = intercept(
        monkeypatch,
        draft,
        include_usage=failure != "unknown_usage",
        finish_reason="SAFETY" if failure == "safety" else "STOP",
    )
    for _ in range(2):
        with pytest.raises(QuestionStoreError):
            await answer(values)
    assert len(observed) == 2
    key = store_fixture.PREFIX + f"requests/{values[2]['request_id']}/calls/answering.json"
    assert key in values[1].objects
    assert len(values[6]) == (1 if failure == "unknown_usage" else 2)


@pytest.mark.asyncio
async def test_answer_count_cannot_reuse_the_full_input_allowance(monkeypatch):
    values = await answering_fixture(monkeypatch)
    observed = intercept(monkeypatch, values[5], count_tokens=31951)
    with pytest.raises(QuestionStoreError, match="budget_exhausted"):
        await answer(values)
    assert len(observed) == 1
    assert (
        "answering"
        not in values[0].read_request(values[2]["request_id"], scope=store_fixture.scope())[
            "admission"
        ]["execution"]["calls"]
    )


@pytest.mark.asyncio
async def test_answer_timeout_after_claim_never_allows_generation_retry(monkeypatch):
    values = await answering_fixture(monkeypatch)
    observed = intercept(monkeypatch, values[5])
    module = runtime_module()
    elapsed = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])
    original = module.GeneralQuestionCalls.claim_call

    def slow(self, *args, **kwargs):
        permit = original(self, *args, **kwargs)
        elapsed[0] = 61.0
        return permit

    monkeypatch.setattr(module.GeneralQuestionCalls, "claim_call", slow)
    with pytest.raises(QuestionStoreError, match="model_timeout"):
        await answer(values)
    with pytest.raises(QuestionStoreError, match="response_unknown"):
        await answer(values)
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_persisted_answer_validator_reprojects_exact_raw_response(monkeypatch):
    values = await answering_fixture(monkeypatch)
    observed = intercept(monkeypatch, values[5])
    result = await answer(values)
    store, _, invocation, _, _, _, _, _ = values
    context = store.read_request(invocation["request_id"], scope=store_fixture.scope())
    plan = store._objects.read(f"requests/{invocation['request_id']}/plan.json").value
    snapshot = store._objects.read(f"requests/{invocation['request_id']}/snapshot.json").value
    kwargs = {
        "store": store,
        "scope": store_fixture.scope(),
        "request": context["request"],
        "plan": plan,
        "snapshot": snapshot,
        "validate_snapshot": lambda value, **_: value,
    }
    assert (
        runtime_module().validate_persisted_question_answer(result["response"], **kwargs)
        == result["response"]
    )
    altered = copy.deepcopy(result["response"])
    altered["answer"] = "Unrelated synthetic answer."
    with pytest.raises(QuestionStoreError, match="answer_binding_invalid"):
        runtime_module().validate_persisted_question_answer(altered, **kwargs)

    def validate_public(value, *, request, plan, snapshot):
        return runtime_module().validate_persisted_question_answer(
            value,
            store=store,
            scope=store_fixture.scope(),
            request=request,
            plan=plan,
            snapshot=snapshot,
            validate_snapshot=lambda value, **_: value,
        )

    ack = store.publish_result(
        invocation["request_id"],
        scope=store_fixture.scope(),
        response=result["response"],
        state=result["state"],
        plan_digest=result["plan_digest"],
        snapshot_digest=result["snapshot_digest"],
        now=store_fixture.NOW + timedelta(seconds=30),
        answer_validator=validate_public,
    )
    status = store.status(
        invocation["request_id"],
        scope=store_fixture.scope(),
        now=store_fixture.NOW + timedelta(seconds=31),
        answer_validator=validate_public,
    )
    assert status["state"] == "complete"
    assert status["result_digest"] == ack["result_digest"]
    assert len(observed) == 2


@pytest.mark.asyncio
async def test_snapshot_changed_during_metering_cannot_rebind_paid_answer(monkeypatch):
    values = await answering_fixture(monkeypatch)
    observed = intercept(monkeypatch, values[5])
    store, bucket, invocation, _, _, _, _, _ = values

    def persist(event):
        key = f"requests/{invocation['request_id']}/snapshot.json"
        snapshot = store._objects.read(key).value
        snapshot["limitations"].append("Changed synthetic source context.")
        snapshot["snapshot_digest"] = canonical_digest(
            {k: v for k, v in snapshot.items() if k != "snapshot_digest"}
        )
        bucket.seed(key, snapshot)
        return event

    with pytest.raises(QuestionStoreError, match="answer_binding_invalid"):
        await answer(values, persist=persist)
    assert len(observed) == 2
    assert (
        store.read_request(invocation["request_id"], scope=store_fixture.scope())["admission"][
            "execution"
        ]["calls"]["answering"]["usage_status"]
        == "acknowledged"
    )


@pytest.mark.asyncio
async def test_failed_answer_metering_never_projects_or_resubmits(monkeypatch):
    values = await answering_fixture(monkeypatch)
    observed = intercept(monkeypatch, values[5])
    projected = []
    original = runtime_module().project_question_answer

    def project(*args, **kwargs):
        projected.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime_module(), "project_question_answer", project)

    def failed(_event):
        raise RuntimeError("synthetic meter failure")

    with pytest.raises(QuestionStoreError, match="metering_failed"):
        await answer(values, persist=failed)
    with pytest.raises(QuestionStoreError, match="usage_unresolved"):
        await answer(values)
    assert projected == []
    assert len(observed) == 2


@pytest.mark.asyncio
async def test_answer_generate_transport_timeout_preserves_unknown_marker(monkeypatch):
    values = await answering_fixture(monkeypatch)
    observed = []

    def handler(request):
        observed.append(request.url.path)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 50})
        raise httpx.ReadTimeout("synthetic response timeout")

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handler))
    with pytest.raises(QuestionStoreError, match="model_unavailable"):
        await answer(values)
    with pytest.raises(QuestionStoreError, match="response_unknown"):
        await answer(values)
    assert len(observed) == 2


def runtime_module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_runtime")


@pytest.mark.asyncio
async def test_legacy_planning_redelivery_and_existing_plan_refuse_tampered_bindings(monkeypatch):
    store, bucket, invocation, identity, credentials = setup_runtime()
    runtime = runtime_module()
    original = runtime.build_question_planning_request
    monkeypatch.setattr(
        runtime,
        "build_question_planning_request",
        lambda *args, **kwargs: original(*args, **kwargs, _adapter="v1"),
    )
    observed = intercept(monkeypatch, plan_draft())
    kwargs = {
        "store": store,
        "scope": store_fixture.scope(),
        "runtime_identity": identity,
        "credentials": credentials,
        "persist_usage": lambda event: event,
        "now": store_fixture.NOW,
    }
    expected = await runtime.execute_question_planning(invocation, **kwargs)
    monkeypatch.setattr(runtime, "build_question_planning_request", original)
    uploads = list(bucket.uploads)
    assert await runtime.execute_question_planning(invocation, **kwargs) == expected
    assert len(observed) == 2
    assert bucket.uploads == uploads
    context = store.read_request(invocation["request_id"], scope=store_fixture.scope())
    snapshot = store._objects.read(f"requests/{invocation['request_id']}/calls/planning.json").value
    for field in ("input_digest", "response_schema_digest", "system_instruction_digest"):
        changed = copy.deepcopy(snapshot)
        changed["binding"][field] = "0" * 64
        with pytest.raises(QuestionStoreError, match="plan_invalid"):
            runtime._persist_plan(store, context, changed)
    assert bucket.uploads == uploads


def plan_draft():
    return {
        "status": "ready",
        "intent": "explanation",
        "markets": ["za"],
        "window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True},
        "decision": None,
        "requirements": [
            {
                "requirement_id": "synthetic_mobility",
                "question": "Which admitted mobility observations matter?",
                "kind": "content",
                "mandatory": True,
                "search_terms": ["mobility"],
            }
        ],
        "clarification": None,
        "limitations": [],
    }


def challenge_draft():
    value = plan_draft()
    support = value["requirements"][0]
    value["requirements"] = [
        {**support, "evidence_purpose": "support"},
        {
            **support,
            "requirement_id": "synthetic_mobility_challenge",
            "question": "Which admitted observations bear against the mobility explanation?",
            "search_terms": ["mobility", "against"],
            "evidence_purpose": "challenge",
        },
    ]
    return value


def setup_runtime(monkeypatch=None, *, source_window=False):
    if source_window:
        monkeypatch.setattr(store_fixture, "prepared", source_window_prepared)
    try:
        store, bucket, invocation, runtime_identity, _binding = deployment_fixture.fixture()
    finally:
        if source_window:
            monkeypatch.setattr(store_fixture, "prepared", PREPARED)
    credentials = AnonymousCredentials()
    credentials.token = "synthetic"
    credentials.service_account_email = runtime_identity["service_account_email"]
    return store, bucket, invocation, runtime_identity, credentials


def intercept(
    monkeypatch,
    draft,
    *,
    include_usage=True,
    finish_reason="STOP",
    count_tokens=50,
    model="gemini-3.5-flash",
):
    observed = []

    def handler(request):
        observed.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content),
            }
        )
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": count_tokens})
        response = {
            "modelVersion": model,
            "candidates": [
                {
                    "finishReason": finish_reason,
                    "content": {
                        "role": "model",
                        "parts": [{"text": draft if isinstance(draft, str) else json.dumps(draft)}],
                    },
                }
            ],
        }
        if include_usage:
            response["usageMetadata"] = {
                "promptTokenCount": 50,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 65,
            }
        return httpx.Response(200, json=response)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(handler))
    monkeypatch.setattr(
        socket.socket, "connect", lambda *_args: pytest.fail("socket connection attempted")
    )
    return observed


@pytest.mark.asyncio
async def test_actual_sdk_planning_persists_raw_usage_and_plan_then_redelivery_is_read_only(
    monkeypatch,
):
    store, bucket, invocation, identity, credentials = setup_runtime()
    observed = intercept(monkeypatch, plan_draft())
    usage = []

    result = await runtime_module().execute_question_planning(
        invocation,
        store=store,
        scope=store_fixture.scope(),
        runtime_identity=identity,
        credentials=credentials,
        persist_usage=lambda event: usage.append(event) or event,
        now=store_fixture.NOW,
    )

    assert result["status"] == "ready"
    assert len(observed) == 2
    assert observed[0]["path"].endswith(":countTokens")
    assert observed[1]["path"].endswith(":generateContent")
    assert observed[0]["body"]["contents"] == observed[1]["body"]["contents"]
    count_config = copy.deepcopy(observed[0]["body"]["generationConfig"])
    generation_config = copy.deepcopy(observed[1]["body"]["generationConfig"])
    count_schema = count_config.pop("responseSchema")
    generation_schema = generation_config.pop("responseSchema")
    assert count_config == generation_config
    assert types.Schema.model_validate(count_schema) == types.Schema.model_validate(
        generation_schema
    )
    assert len(usage) == 1
    assert usage[0].prompt_tokens == 50
    assert usage[0].completion_tokens == 15

    prefix = store_fixture.PREFIX + f"requests/{invocation['request_id']}/"
    uploaded = [name for name, _generation in bucket.uploads if name.startswith(prefix)]
    response_name = prefix + "calls/planning.json"
    usage_name = prefix + "usage/planning.json"
    plan_name = prefix + "plan.json"
    assert uploaded.index(response_name) < uploaded.index(usage_name) < uploaded.index(plan_name)
    raw_response = json.loads(bucket.objects[response_name][1])
    assert (
        plan_draft()["requirements"][0]["requirement_id"]
        in raw_response["raw_sdk_response"]["sdk_http_response"]["body"]
    )
    control = json.loads(bucket.objects[store_fixture.PREFIX + store_fixture.LEDGER][1])
    call = control["requests"][invocation["request_id"]]["execution"]["calls"]["planning"]
    assert call["counted_input_tokens"] == 50
    assert call["usage_status"] == "acknowledged"

    recovered = await runtime_module().execute_question_planning(
        invocation,
        store=store,
        scope=store_fixture.scope(),
        runtime_identity=identity,
        credentials=credentials,
        persist_usage=lambda _event: pytest.fail("usage persisted twice"),
        now=store_fixture.NOW,
    )
    assert recovered == result
    assert len(observed) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft", "include_usage", "finish_reason", "code"),
    [
        ("not valid json", True, "STOP", "plan_invalid"),
        ('{"status":' + "9" * 5000 + "}", True, "STOP", "plan_invalid"),
        (plan_draft(), True, "SAFETY", "plan_invalid"),
        (plan_draft(), False, "STOP", "usage_unknown"),
    ],
)
async def test_known_response_failure_never_resubmits_generation(
    monkeypatch, draft, include_usage, finish_reason, code
):
    store, bucket, invocation, identity, credentials = setup_runtime()
    observed = intercept(
        monkeypatch,
        draft,
        include_usage=include_usage,
        finish_reason=finish_reason,
    )
    usage = []

    for _attempt in range(2):
        with pytest.raises(QuestionStoreError, match=code):
            await runtime_module().execute_question_planning(
                invocation,
                store=store,
                scope=store_fixture.scope(),
                runtime_identity=identity,
                credentials=credentials,
                persist_usage=lambda event: usage.append(event) or event,
                now=store_fixture.NOW,
            )
    assert len(observed) == 2
    assert (
        store_fixture.PREFIX + f"requests/{invocation['request_id']}/calls/planning.json"
        in bucket.objects
    )
    if include_usage:
        assert len(usage) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [{"model": "other-model"}, {"thinking_level": "MEDIUM"}, {"thinking_level": "UNKNOWN"}],
)
async def test_submission_permit_must_match_exact_sdk_configuration(monkeypatch, change):
    store, _bucket, invocation, identity, credentials = setup_runtime()
    observed = intercept(monkeypatch, plan_draft())
    module = runtime_module()
    original = module.GeneralQuestionCalls.claim_call

    def mismatched(self, *args, **kwargs):
        return replace(original(self, *args, **kwargs), **change)

    monkeypatch.setattr(module.GeneralQuestionCalls, "claim_call", mismatched)
    with pytest.raises(QuestionStoreError, match="call_invalid"):
        await module.execute_question_planning(
            invocation,
            store=store,
            scope=store_fixture.scope(),
            runtime_identity=identity,
            credentials=credentials,
            persist_usage=lambda event: event,
            now=store_fixture.NOW,
        )
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_sdk_credentials_must_match_measured_runtime_identity(monkeypatch):
    store, _bucket, invocation, identity, credentials = setup_runtime()
    observed = intercept(monkeypatch, plan_draft())
    credentials.service_account_email = "other@example.invalid"

    with pytest.raises(QuestionStoreError, match="identity_invalid"):
        await runtime_module().execute_question_planning(
            invocation,
            store=store,
            scope=store_fixture.scope(),
            runtime_identity=identity,
            credentials=credentials,
            persist_usage=lambda event: event,
            now=store_fixture.NOW,
        )
    assert observed == []


@pytest.mark.asyncio
async def test_slow_durable_claim_holds_without_starting_generation(monkeypatch):
    store, bucket, invocation, identity, credentials = setup_runtime()
    observed = intercept(monkeypatch, plan_draft())
    module = runtime_module()
    elapsed = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])
    original = module.GeneralQuestionCalls.claim_call

    def slow_claim(self, *args, **kwargs):
        permit = original(self, *args, **kwargs)
        elapsed[0] = 61.0
        return permit

    monkeypatch.setattr(module.GeneralQuestionCalls, "claim_call", slow_claim)
    with pytest.raises(QuestionStoreError, match="model_timeout"):
        await module.execute_question_planning(
            invocation,
            store=store,
            scope=store_fixture.scope(),
            runtime_identity=identity,
            credentials=credentials,
            persist_usage=lambda event: event,
            now=store_fixture.NOW,
        )
    assert [item["path"] for item in observed] == [
        next(item["path"] for item in observed if item["path"].endswith(":countTokens"))
    ]
    control = json.loads(bucket.objects[store_fixture.PREFIX + store_fixture.LEDGER][1])
    call = control["requests"][invocation["request_id"]]["execution"]["calls"]["planning"]
    assert call["response"] is None


@pytest.mark.asyncio
async def test_redelivery_rederives_plan_and_rejects_valid_rehashed_mutation(monkeypatch):
    store, bucket, invocation, identity, credentials = setup_runtime()
    observed = intercept(monkeypatch, plan_draft())
    result = await runtime_module().execute_question_planning(
        invocation,
        store=store,
        scope=store_fixture.scope(),
        runtime_identity=identity,
        credentials=credentials,
        persist_usage=lambda event: event,
        now=store_fixture.NOW,
    )
    mutated = copy.deepcopy(result)
    mutated["requirements"][0]["question"] = "Different but structurally valid question"
    mutated["plan_digest"] = canonical_digest(
        {key: item for key, item in mutated.items() if key != "plan_digest"}
    )
    bucket.seed(f"requests/{invocation['request_id']}/plan.json", mutated)

    with pytest.raises(QuestionStoreError, match="plan_invalid"):
        await runtime_module().execute_question_planning(
            invocation,
            store=store,
            scope=store_fixture.scope(),
            runtime_identity=identity,
            credentials=credentials,
            persist_usage=lambda _event: pytest.fail("usage persisted twice"),
            now=store_fixture.NOW,
        )
    assert len(observed) == 2


@pytest.mark.parametrize("code,label", [(400, "INVALID_ARGUMENT"), (429, "RESOURCE_EXHAUSTED")])
def test_provider_failure_diagnostic_is_bounded_and_redacted(capsys, code, label):
    from google.genai.errors import ClientError

    error = ClientError(
        code,
        {
            "error": {
                "code": code,
                "status": label,
                "message": "generation_config.response_schema invalid; Bearer private-token access_token=hidden api_key=secret contents=private-prompt",
                "details": [{"secret": "never-log-details"}],
            }
        },
    )
    runtime_module()._log_model_failure(
        error, request_id="00000000-0000-4000-8000-000000000001", stage="answering"
    )
    emitted = capsys.readouterr().err
    event = json.loads(emitted)
    assert event["provider_status"] == code
    assert event["provider_status_label"] == label
    assert "response_schema invalid" in event["provider_message"]
    for text in ("private-token", "hidden", "secret", "private-prompt", "never-log-details"):
        assert text not in emitted
    assert len(event["provider_message"]) <= 1000


@pytest.mark.parametrize(
    "error", [TimeoutError("private"), httpx.ReadTimeout("private"), RuntimeError("private")]
)
def test_nonprovider_diagnostic_never_formats_exception(capsys, error):
    runtime_module()._log_model_failure(
        error, request_id="00000000-0000-4000-8000-000000000001", stage="planning"
    )
    emitted = capsys.readouterr().err
    event = json.loads(emitted)
    assert event["exception_class"] == type(error).__name__
    assert event["provider_message"] is None
    assert "private" not in emitted


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["planning", "answering"])
@pytest.mark.parametrize("code", [400, 429])
async def test_provider_boundary_logs_once_and_never_retries(monkeypatch, capsys, stage, code):
    values = await answering_fixture(monkeypatch) if stage == "answering" else setup_runtime()
    store, _, invocation, identity, credentials = values[:5]
    sent = []

    def handler(request):
        sent.append(request.url.path)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 50})
        return httpx.Response(
            code,
            json={
                "error": {
                    "code": code,
                    "status": "INVALID_ARGUMENT" if code == 400 else "RESOURCE_EXHAUSTED",
                    "message": "response_schema invalid" if code == 400 else "quota exceeded",
                }
            },
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))
    capsys.readouterr()

    async def invoke():
        if stage == "answering":
            await answer(values)
        else:
            await runtime_module().execute_question_planning(
                invocation,
                store=store,
                scope=store_fixture.scope(),
                runtime_identity=identity,
                credentials=credentials,
                persist_usage=lambda event: event,
                now=store_fixture.NOW,
            )

    with pytest.raises(QuestionStoreError, match="model_unavailable"):
        await invoke()
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert len(events) == 1
    assert events[0]["stage"] == stage
    assert events[0]["provider_status"] == code
    assert sum(path.endswith(":generateContent") for path in sent) == 1
    call = store.read_request(invocation["request_id"], scope=store_fixture.scope())["admission"][
        "execution"
    ]["calls"][stage]
    assert call["response"] is None


@pytest.mark.parametrize(
    "code", ["call_invalid", "snapshot_cap_version_unknown", "protected_context_registry_invalid"]
)
def test_store_error_diagnostic_uses_only_known_code(capsys, code):
    runtime_module()._log_model_failure(
        QuestionStoreError(code),
        request_id="00000000-0000-4000-8000-000000000001",
        stage="answering",
    )
    assert json.loads(capsys.readouterr().err)["error_code"] == code
    runtime_module()._log_model_failure(
        QuestionStoreError("private-secret"),
        request_id="00000000-0000-4000-8000-000000000001",
        stage="answering",
    )
    emitted = capsys.readouterr().err
    assert json.loads(emitted)["error_code"] is None
    assert "private-secret" not in emitted


@pytest.mark.parametrize(
    "message",
    [
        '{"api_key":"private-secret"}',
        '{"headers":{"Cookie":"private-secret"}}',
        "{'access_token': 'private-secret'}",
        '{"contents":[{"text":"private-secret"}]}',
    ],
)
def test_provider_json_quoted_keys_are_redacted(capsys, message):
    from google.genai.errors import ClientError

    error = ClientError(400, {"error": {"status": "INVALID_ARGUMENT", "message": message}})
    runtime_module()._log_model_failure(
        error, request_id="00000000-0000-4000-8000-000000000001", stage="answering"
    )
    emitted = capsys.readouterr().err
    assert "private-secret" not in emitted
    assert json.loads(emitted)["provider_status"] == 400


def test_escaped_provider_message_fits_parent_line_limit(capsys):
    from google.genai.errors import ClientError

    runtime_module()._log_model_failure(
        ClientError(400, {"error": {"status": "INVALID_ARGUMENT", "message": '"' * 1000}}),
        request_id="00000000-0000-4000-8000-000000000001",
        stage="answering",
    )
    wire = capsys.readouterr().err
    assert len(wire.encode("utf-8")) <= 2048
    assert json.loads(wire)["provider_status"] == 400


def test_queue_wait_never_resets_the_original_deadline():
    from datetime import UTC, datetime

    from src.analysis.open_intelligence.general_question_runtime import next_stage_deadline

    admitted = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    deadline = admitted + timedelta(seconds=240)
    assert next_stage_deadline(deadline, admitted + timedelta(seconds=230), 60) == deadline
    with pytest.raises(ValueError):
        next_stage_deadline(deadline, deadline, 60)


@pytest.mark.parametrize(
    ("now_offset", "stage_seconds", "expected_offset"),
    [(0, 60, 60), (100, 60, 160), (181, 60, 240), (239, 1, 240)],
)
def test_next_stage_deadline_is_the_earlier_of_stage_and_admitted_limits(
    now_offset, stage_seconds, expected_offset
):
    from datetime import UTC, datetime

    from src.analysis.open_intelligence.general_question_runtime import next_stage_deadline

    admitted = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    deadline = admitted + timedelta(seconds=240)
    assert next_stage_deadline(
        deadline, admitted + timedelta(seconds=now_offset), stage_seconds
    ) == admitted + timedelta(seconds=expected_offset)


@pytest.mark.parametrize(
    ("deadline_shift", "now_shift", "stage_seconds"),
    [
        (None, None, 0),
        (None, None, -60),
        (None, None, True),
        (None, None, 60.0),
        ("naive", None, 60),
        (None, "naive", 60),
        (None, "late", 60),
    ],
)
def test_next_stage_deadline_refuses_bad_limits_naive_clocks_and_expired_requests(
    deadline_shift, now_shift, stage_seconds
):
    from datetime import UTC, datetime

    from src.analysis.open_intelligence.general_question_runtime import next_stage_deadline

    admitted = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    deadline = admitted + timedelta(seconds=240)
    now = admitted + timedelta(seconds=10)
    if deadline_shift == "naive":
        deadline = deadline.replace(tzinfo=None)
    if now_shift == "naive":
        now = now.replace(tzinfo=None)
    if now_shift == "late":
        now = deadline + timedelta(seconds=1)
    with pytest.raises(ValueError):
        next_stage_deadline(deadline, now, stage_seconds)


@pytest.mark.asyncio
async def test_model_stage_deadline_is_bounded_by_the_admitted_deadline(monkeypatch):
    store, _bucket, invocation, identity, credentials = setup_runtime()
    runtime = runtime_module()
    observed = []
    original = runtime.next_stage_deadline

    def spy(admitted_deadline, now, stage_seconds):
        observed.append((admitted_deadline, now, stage_seconds))
        return original(admitted_deadline, now, stage_seconds)

    monkeypatch.setattr(runtime, "next_stage_deadline", spy)
    intercept(monkeypatch, plan_draft())
    late = store_fixture.NOW + timedelta(seconds=230)
    await runtime.execute_question_planning(
        invocation,
        store=store,
        scope=store_fixture.scope(),
        runtime_identity=identity,
        credentials=credentials,
        persist_usage=lambda event: event,
        now=late,
    )
    assert observed
    assert all(stage_seconds == 60 for _, _, stage_seconds in observed)
    assert all(
        admitted_deadline == store_fixture.NOW + timedelta(seconds=240)
        for admitted_deadline, _, _ in observed
    )
    assert all(now >= late for _, now, _ in observed)


def test_deadline_exhaustion_is_the_only_expiry_from_next_stage_deadline():
    from datetime import UTC, datetime

    runtime = runtime_module()
    admitted = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    deadline = admitted + timedelta(seconds=240)
    with pytest.raises(runtime.StageDeadlineReached):
        runtime.next_stage_deadline(deadline, deadline, 60)
    assert issubclass(runtime.StageDeadlineReached, ValueError)
    for bad in (
        (deadline, admitted, 0),
        (deadline.replace(tzinfo=None), admitted, 60),
        (deadline, admitted.replace(tzinfo=None), 60),
    ):
        with pytest.raises(ValueError) as raised:
            runtime.next_stage_deadline(*bad)
        assert not isinstance(raised.value, runtime.StageDeadlineReached)


def test_stage_deadline_reports_expiry_distinctly_from_configuration_faults(monkeypatch):
    store, _bucket, invocation, _identity, _credentials = setup_runtime()
    runtime = runtime_module()
    context = store.read_request(invocation["request_id"], scope=store_fixture.scope())
    started_monotonic = runtime.monotonic()
    with pytest.raises(QuestionStoreError, match="request_expired"):
        runtime._stage_deadline(
            store, context, store_fixture.NOW + timedelta(seconds=240), started_monotonic
        )
    bounded = runtime._stage_deadline(
        store, context, store_fixture.NOW + timedelta(seconds=230), started_monotonic
    )
    assert 9 < bounded - runtime.monotonic() <= 10
    with pytest.raises(QuestionStoreError, match="control_invalid"):
        runtime._stage_deadline(
            store, context, store_fixture.NOW.replace(tzinfo=None), started_monotonic
        )
    original = store._stored_policy

    def corrupt_limit(policy_digest):
        policy = original(policy_digest)
        policy["limits"]["model_timeout_seconds"] = 0
        return policy

    monkeypatch.setattr(store, "_stored_policy", corrupt_limit)
    with pytest.raises(QuestionStoreError, match="control_invalid"):
        runtime._stage_deadline(store, context, store_fixture.NOW, started_monotonic)
