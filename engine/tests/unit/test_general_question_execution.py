import importlib
import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import httpx
import pytest

from tests.unit import test_general_question_result as result_fixture
from tests.unit import test_general_question_runtime as runtime_fixture
from tests.unit import test_general_question_store as fixture


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_execution")


def test_answer_invalid_is_a_specific_bounded_reason():
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    assert module()._reason(QuestionStoreError("answer_invalid"), "answering") == "answer_invalid"
    assert module()._reason(RuntimeError("unexpected private text"), "answering") == "worker_failed"


def test_parent_source_failure_retains_only_fixed_public_code():
    assert module()._retrieval_checks(
        {
            "reason": "parent_source_unavailable",
            "missing_work": ["parent_source_unavailable", "PRIVATE_HASH_OR_CONTENT"],
        }
    ) == ("parent_source_unavailable",)


def test_all_foreign_local_refusal_retains_only_allowlisted_public_checks():
    checks = module()._retrieval_checks(
        {
            "status": "coverage_gap",
            "reason": "coverage_incomplete",
            "missing_work": [
                "corroborated_foreign_local_only",
                "RAW_PRIVATE_DETAIL",
                {"count": 24},
            ],
        }
    )
    assert checks == ("coverage_incomplete", "corroborated_foreign_local_only")


async def execute(values, *, now=fixture.NOW, diagnostics=None, stage_events=None):
    store, _bucket, invocation, identity, credentials = values
    return await module().execute_general_question(
        invocation,
        store=store,
        runtime_identity=identity,
        credentials=credentials,
        now=now,
        diagnostics=diagnostics,
        **({} if stage_events is None else {"stage_events": stage_events}),
    )


@pytest.mark.asyncio
async def test_terminal_redelivery_reuses_exact_result_without_starting_work(monkeypatch):
    values = runtime_fixture.setup_runtime()
    store, bucket, invocation, _, _ = values
    expected = result_fixture.publish(store, invocation["request_id"])
    monkeypatch.setattr(
        module(), "execute_question_planning", lambda *a, **k: pytest.fail("new work")
    )
    uploads = len(bucket.uploads)
    assert await execute(values) == expected
    assert len(bucket.uploads) == uploads


@pytest.mark.asyncio
async def test_expired_admission_gets_durable_unavailable_without_model(monkeypatch):
    values = runtime_fixture.setup_runtime()
    monkeypatch.setattr(
        module(), "execute_question_planning", lambda *a, **k: pytest.fail("new work")
    )
    result = await execute(values, now=fixture.NOW + timedelta(seconds=241))
    assert result["state"] == "unavailable"
    assert result["result_digest"]
    assert result["result_generation"]


@pytest.mark.asyncio
async def test_actual_planning_clarification_is_persisted_without_source_or_answer_call(
    monkeypatch,
):
    values = runtime_fixture.setup_runtime()
    draft = runtime_fixture.plan_draft()
    draft.update(
        status="needs_clarification",
        requirements=[],
        clarification="Which mobility choice should I examine?",
    )
    observed = runtime_fixture.intercept(monkeypatch, draft)
    monkeypatch.setattr(
        module(), "build_general_question_snapshot", lambda *a, **k: pytest.fail("source call")
    )
    result = await execute(values)
    assert result["state"] == "needs_clarification"
    assert len(observed) == 2
    store = values[0]
    record = store._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    assert record.value["response"]["answer"] == draft["clarification"]
    assert record.value["response"]["intelligence"]["usage"]["model_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary,failing_check",
    [
        ("copy_schema", "copy_schema_invalid"),
        ("release", "release_admission_invalid"),
        ("copy_binding", "snapshot_source_copy_binding_invalid"),
    ],
)
async def test_source_refusal_retains_safe_failing_check_without_raw_detail(
    monkeypatch, boundary, failing_check
):
    from tests.unit import test_general_question_snapshot as snapshot_fixture

    snapshot_result, _snapshot_store = snapshot_fixture.actual_validation_failure(
        monkeypatch, boundary
    )
    assert snapshot_result["reason"] == failing_check
    values = runtime_fixture.setup_runtime()
    observed = runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    diagnostics = []

    def refused_snapshot(*args, **kwargs):
        assert kwargs["source_bytes"] == 300_000_000
        assert kwargs["discovery_bytes"] == 100_000_000
        assert kwargs["release_bytes"] == 50_000_000
        return {
            **snapshot_result,
            "missing_work": [
                *snapshot_result["missing_work"],
                "RAW_SENTINEL provider failure text",
                {"raw": "RAW_SENTINEL"},
            ],
        }

    monkeypatch.setattr(module(), "build_general_question_snapshot", refused_snapshot)
    result = await execute(values, diagnostics=diagnostics.append)
    assert result["state"] == "unavailable"
    assert len(observed) == 2
    record = values[0]._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    response = record.value["response"]
    assert response["reason"] == "retrieval_incomplete"
    assert response["intelligence"]["missing_work"] == [
        "retrieval_incomplete",
        failing_check,
    ]
    assert diagnostics[-2]["phase"] == "retrieval"
    assert diagnostics[-2]["state"] == "failed"
    assert diagnostics[-2]["code"] == "retrieval_incomplete"
    assert diagnostics[-2]["request_id"] == result["request_id"]
    assert "RAW_SENTINEL" not in json.dumps(response)
    assert "RAW_SENTINEL" not in json.dumps(diagnostics)


@pytest.mark.asyncio
async def test_actual_uncovered_plan_failure_preserves_plan_market_window_and_coverage_reason(
    monkeypatch,
):
    now = datetime(2026, 9, 9, 17, 19, tzinfo=UTC)
    monkeypatch.setattr(fixture, "NOW", now)
    values = runtime_fixture.setup_runtime()
    draft = runtime_fixture.plan_draft()
    draft["window"] = {"start": "2026-08-26", "end": "2026-09-08", "closed": True}
    observed = runtime_fixture.intercept(monkeypatch, draft)
    result = await execute(values, now=now)
    store = values[0]
    _, control = store._control()
    assert control["requests"][result["request_id"]]["execution"].get("queries", {}) == {}
    record = store._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json"
    )
    response = record.value["response"]
    assert response["reason"] == "retrieval_incomplete"
    assert response["intelligence"]["window"] == draft["window"]
    assert response["intelligence"]["resolved_scope"]["market_scope"] == ["za"]
    assert response["intelligence"]["missing_work"] == [
        "retrieval_incomplete",
        "coverage_incomplete",
    ]
    assert len(observed) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_outcome", [None, "status", "snapshot", "missing_work"])
async def test_successful_empty_retrieval_publishes_insufficiency_without_answer_or_snapshot(
    monkeypatch, invalid_outcome
):
    values = runtime_fixture.setup_runtime()
    draft = runtime_fixture.plan_draft()
    observed = runtime_fixture.intercept(monkeypatch, draft)
    outcome = {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "coverage_gap",
        "snapshot": None,
        "reason": "evidence_insufficient",
        "missing_work": ["no_matching_evidence"],
    }
    if invalid_outcome:
        outcome[invalid_outcome] = {
            "status": "refused",
            "snapshot": {},
            "missing_work": ["unverified"],
        }[invalid_outcome]
    monkeypatch.setattr(module(), "build_general_question_snapshot", lambda *a, **k: outcome)
    monkeypatch.setattr(
        module(), "execute_question_answering", lambda *a, **k: pytest.fail("answer call")
    )
    diagnostics = []
    result = await execute(values, diagnostics=diagnostics.append)
    store = values[0]
    record = store._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json"
    )
    response = record.value["response"]
    if invalid_outcome:
        assert response["reason"] == "retrieval_incomplete"
        assert "no_matching_evidence" not in response["intelligence"]["missing_work"]
        return
    assert response["reason"] == "evidence_insufficient"
    assert (
        response["answer"]
        == "No admissible evidence matched this request. The requested answer cannot be established from this read."
    )
    assert response["intelligence"]["missing_work"] == [
        "evidence_insufficient",
        "no_matching_evidence",
    ]
    assert response["intelligence"]["window"] == draft["window"]
    assert response["intelligence"]["resolved_scope"]["market_scope"] == ["za"]
    assert store._objects.read(f"requests/{result['request_id']}/snapshot.json") is None
    _, control = store._control()
    assert control["reserved_microusd"] == 100000
    assert len(observed) == 2
    assert any(
        item["phase"] == "retrieval" and item["state"] == "succeeded" for item in diagnostics
    )


@pytest.mark.asyncio
async def test_unknown_usage_is_held_and_redelivery_never_generates_again(monkeypatch):
    values = runtime_fixture.setup_runtime()
    observed = runtime_fixture.intercept(
        monkeypatch, runtime_fixture.plan_draft(), include_usage=False
    )
    first = await execute(values)
    assert first["state"] == "held"
    assert await execute(values, now=fixture.NOW + timedelta(seconds=30)) == first
    assert len(observed) == 2
    store = values[0]
    record = store._objects.read(
        f"requests/{first['request_id']}/results/{first['result_digest']}.json",
        generation=int(first["result_generation"]),
    )
    usage = record.value["response"]["intelligence"]["usage"]
    assert usage["status"] == "unresolved"
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None


@pytest.mark.asyncio
async def test_input_budget_refusal_preserves_reason_without_paid_generation(monkeypatch):
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    observed = []

    def handler(request):
        observed.append(request.url.path)
        if not request.url.path.endswith(":countTokens"):
            pytest.fail("generation after excessive token count")
        return httpx.Response(200, json={"totalTokens": 8001})

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handler))
    result = await execute(values)
    assert result["state"] == "unavailable"
    assert len(observed) == 1
    record = values[0]._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    assert record.value["response"]["reason"] == "budget_exhausted_no_grounded_result"
    assert record.value["response"]["intelligence"]["usage"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_joined_sources_answer_publication_and_status_use_real_validators(monkeypatch):
    from tests.unit import test_general_question_source_integration as source_fixture

    source = source_fixture.setup_fixture(monkeypatch, seed_planning=False)
    observed = []

    def handler(request):
        body = json.loads(request.content)
        observed.append(body)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 50})
        content = json.loads(body["contents"][0]["parts"][0]["text"])
        if "snapshot" not in content:
            draft = json.loads(
                source.planning_response["candidates"][0]["content"]["parts"][0]["text"]
            )
        else:
            assert "provenance" not in content["snapshot"]
            reading = content["snapshot"]["readings"][0]
            draft = {
                "quote_observations": [],
                "reading_observations": [
                    {
                        "claim_id": "observed_count",
                        "reading_id": reading["reading_id"],
                    }
                ],
                "interpretations": [],
                "inferences": [],
                "proposals": [],
                "answer": ["observed_count"],
            }
        return httpx.Response(
            200,
            json={
                "modelVersion": "gemini-3.5-flash",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": json.dumps(draft)}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 50,
                    "candidatesTokenCount": 10,
                    "thoughtsTokenCount": 5,
                    "totalTokenCount": 65,
                },
            },
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handler))
    values = source.store, None, source.invocation, source.runtime_identity, source.credentials
    result = await execute(values, now=fixture.NOW + timedelta(seconds=2))
    assert result["state"] == "partial"
    assert len(observed) == 4
    assert len(source.http.jobs) == 4
    record = source.store._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    response = record.value["response"]
    assert response["intelligence"]["usage"]["model_calls"] == 2
    assert response["intelligence"]["claims"]
    assert response["sources"]
    assert response["intelligence"]["missing_work"]
    requests_before = len(source.http.calls)
    status = module().read_general_question_status(
        result["request_id"],
        store=source.store,
        scope=source.scope,
        now=fixture.NOW + timedelta(seconds=30),
    )
    assert status["result_digest"] == result["result_digest"]
    assert await execute(values, now=fixture.NOW + timedelta(seconds=30)) == result
    assert len(observed) == 4
    assert len(source.http.calls) == requests_before


_NATIVE_TRUNCATED_PLANNING = {
    "modelVersion": "gemini-3.5-flash",
    "usageMetadata": {
        "promptTokenCount": 1806,
        "candidatesTokenCount": 19,
        "thoughtsTokenCount": 765,
        "totalTokenCount": 2590,
    },
    "candidates": [
        {
            "finishReason": "MAX_TOKENS",
            "content": {
                "role": "model",
                "parts": [{"text": '{\n  "status": "ready",\n  "intent": "history",\n  '}],
            },
        }
    ],
}


@pytest.mark.asyncio
async def test_native_truncated_planning_is_metered_semantic_refusal_without_retry(monkeypatch):
    import socket

    values = runtime_fixture.setup_runtime()
    observed = []

    def handler(request):
        observed.append(request.url.path)
        return httpx.Response(
            200,
            json={"totalTokens": 1806}
            if request.url.path.endswith(":countTokens")
            else _NATIVE_TRUNCATED_PLANNING,
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network"))
    monkeypatch.setattr(
        module(), "build_general_question_snapshot", lambda *a, **k: pytest.fail("retrieval")
    )
    monkeypatch.setattr(
        module(), "execute_question_answering", lambda *a, **k: pytest.fail("answer")
    )
    diagnostics = []
    result = await execute(values, diagnostics=diagnostics.append)
    store = values[0]
    record = store._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json"
    )
    assert record.value["response"]["reason"] == "semantic_output_invalid"
    usage = record.value["response"]["intelligence"]["usage"]
    assert usage["model_calls"] == 1
    assert usage["input_tokens"] == 1806
    assert usage["output_tokens"] == 784
    assert store._objects.read(f"requests/{result['request_id']}/usage/planning.json") is not None
    assert store._objects.read(f"requests/{result['request_id']}/plan.json") is None
    assert await execute(values, now=fixture.NOW + timedelta(seconds=30)) == result
    assert len(observed) == 2
    assert diagnostics[-2]["code"] == "semantic_output_invalid"


def test_legacy_failed_status_survives_new_active_policy_without_relabelling(monkeypatch):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.general_question_policy import build_question_policy
    from src.analysis.open_intelligence.general_question_store import GeneralQuestionStore

    current = build_question_policy(pricing_verified_at=fixture.NOW)
    legacy = build_question_policy(
        pricing_verified_at=fixture.NOW,
        approval_contract_digest="a0410e47911ff343ef77726e4ffefbfd57f275e9cf341633a0a9fd1b01e506e0",
    )
    legacy["stages"]["planning"]["thinking_level"] = "MEDIUM"
    legacy["stages"]["answering"]["thinking_level"] = "HIGH"
    legacy["policy_digest"] = canonical_digest(
        {k: v for k, v in legacy.items() if k != "policy_digest"}
    )
    monkeypatch.setattr(fixture, "build_question_policy", lambda **kwargs: legacy)
    store, bucket, invocation, _, _ = runtime_fixture.setup_runtime()
    response = result_fixture.response(store, invocation["request_id"])
    response["reason"] = "worker_failed"
    response["intelligence"]["missing_work"] = ["worker_failed"]
    original = result_fixture.publish(store, invocation["request_id"], response=response)
    stored, control = store._control()
    bucket.seed(f"policies/{current['policy_digest']}/policy.json", current)
    control["bindings"]["d" * 64] = current["policy_digest"]
    control["active_deployment_digest"] = "d" * 64
    store._objects.compare_control(stored.generation, control)
    refreshed = GeneralQuestionStore(bucket, policy=current, deployment_digest="d" * 64)
    status = module().read_general_question_status(
        invocation["request_id"], store=refreshed, scope=fixture.scope(), now=fixture.NOW
    )
    assert status["result_digest"] == original["result_digest"]
    record = refreshed._objects.read(
        f"requests/{invocation['request_id']}/results/{original['result_digest']}.json"
    )
    assert record.value["response"]["reason"] == "worker_failed"
    assert record.value["policy_digest"] == legacy["policy_digest"]


def stage_record(**overrides):
    record = {
        "event_version": "question_stage_event_v1",
        "request_id": "00000000-0000-0000-0000-000000000001",
        "invocation_id": "00000000-0000-0000-0000-000000000002",
        "policy_digest": "a" * 64,
        "deployment_digest": "b" * 64,
        "stage": "planning",
        "event": "failed",
        "occurred_at": "2026-09-06T20:00:01.000000Z",
        "elapsed_ms": 1000,
        "reason_code": "model_timeout",
        "usage_state": "unknown",
    }
    record.update(overrides)
    return record


def test_stage_transition_table_is_frozen_and_closed():
    subject = module()
    assert subject.STAGES == (
        "admission",
        "queue",
        "planning",
        "retrieval",
        "answering",
        "validation",
        "publication",
    )
    assert subject.STAGE_EVENTS == ("entered", "completed", "failed", "held")
    with pytest.raises(TypeError):
        subject.STAGE_TRANSITIONS[("planning", "entered")] = ()
    assert set(subject.STAGE_TRANSITIONS) <= {
        (stage, event) for stage in subject.STAGES for event in subject.STAGE_EVENTS
    }
    for successors in subject.STAGE_TRANSITIONS.values():
        assert type(successors) is tuple
        assert all(pair in subject.STAGE_TRANSITIONS for pair in successors)
    assert subject.STAGE_TRANSITIONS[("planning", "completed")] == (
        ("retrieval", "entered"),
        ("publication", "entered"),
    )
    assert subject.STAGE_TRANSITIONS[("publication", "completed")] == ()
    assert ("queue", "held") not in subject.STAGE_TRANSITIONS
    assert subject.validate_stage_event(stage_record()) == stage_record()


@pytest.mark.parametrize(
    "change",
    [
        {"question": "What is changing for Gen Z?"},
        {"prompt": "system instruction text"},
        {"token": "ya29.private"},
        {"source_body": "private post body"},
        {"contents": []},
        {"reason_code": "What is changing for Gen Z?"},
        {"reason_code": "Bearer ya29.private"},
        {"reason_code": None},
        {"stage": "prompt"},
        {"event": "started"},
        {"usage_state": "free text"},
        {"occurred_at": "yesterday"},
        {"elapsed_ms": -1},
        {"elapsed_ms": "1000"},
        {"request_id": "not-a-uuid"},
        {"invocation_id": "ya29.private"},
        {"policy_digest": "x" * 64},
        {"event_version": "question_stage_event_v2"},
    ],
)
def test_stage_event_record_refuses_extra_keys_and_raw_material(change):
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    with pytest.raises(QuestionStoreError, match="stage_event_invalid"):
        module().validate_stage_event(stage_record(**change))
    dropped = stage_record()
    dropped.pop("usage_state")
    with pytest.raises(QuestionStoreError, match="stage_event_invalid"):
        module().validate_stage_event(dropped)


@pytest.mark.parametrize("event", ["entered", "completed"])
def test_only_failed_and_held_stage_events_carry_exactly_one_reason_code(event):
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    assert module().validate_stage_event(
        stage_record(event=event, reason_code=None, usage_state="reserved")
    )
    with pytest.raises(QuestionStoreError, match="stage_event_invalid"):
        module().validate_stage_event(stage_record(event=event, reason_code="model_timeout"))
    for outcome in ("failed", "held"):
        with pytest.raises(QuestionStoreError, match="stage_event_invalid"):
            module().validate_stage_event(stage_record(event=outcome, reason_code=None))


@pytest.mark.asyncio
async def test_planning_failure_emits_one_stage_failure_with_a_safe_reason(monkeypatch):
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, "PRIVATE unparsable plan text")
    events = []
    result = await execute(values, stage_events=events.append)
    assert result["state"] == "unavailable"
    for previous, current in pairwise(events):
        assert (current["stage"], current["event"]) in module().STAGE_TRANSITIONS[
            (previous["stage"], previous["event"])
        ]
    assert [(e["stage"], e["event"]) for e in events] == [
        ("planning", "entered"),
        ("planning", "failed"),
        ("publication", "entered"),
        ("publication", "completed"),
    ]
    failures = [e for e in events if e["event"] == "failed"]
    assert len(failures) == 1
    assert failures[0]["reason_code"] == "semantic_output_invalid"
    assert len({e["invocation_id"] for e in events}) == 1
    assert all(e["request_id"] == result["request_id"] for e in events)
    assert "PRIVATE" not in json.dumps(events)
    assert events[-1]["usage_state"] == "settled"
    assert all(module().validate_stage_event(e) == e for e in events)


@pytest.mark.asyncio
async def test_stage_events_follow_the_table_through_clarification(monkeypatch):
    values = runtime_fixture.setup_runtime()
    draft = runtime_fixture.plan_draft()
    draft.update(
        status="needs_clarification",
        requirements=[],
        clarification="Which mobility choice should I examine?",
    )
    runtime_fixture.intercept(monkeypatch, draft)
    events = []
    result = await execute(values, stage_events=events.append)
    assert result["state"] == "needs_clarification"
    assert [(e["stage"], e["event"], e["usage_state"]) for e in events] == [
        ("planning", "entered", "reserved"),
        ("planning", "completed", "settled"),
        ("publication", "entered", "settled"),
        ("publication", "completed", "settled"),
    ]
    assert all(e["reason_code"] is None for e in events)
    assert all(e["policy_digest"] == values[2]["policy_digest"] for e in events)
    assert all(e["deployment_digest"] == values[2]["deployment_digest"] for e in events)


@pytest.mark.asyncio
async def test_durable_cancel_marker_stops_new_work_and_keeps_the_reservation(monkeypatch):
    values = runtime_fixture.setup_runtime()
    store, bucket, invocation, _, _ = values
    status = store.cancel(invocation["request_id"], scope=fixture.scope(), now=fixture.NOW)
    assert status["state"] == "unavailable"
    ledger = json.loads(bucket.objects[fixture.PREFIX + fixture.LEDGER][1])
    assert ledger["reserved_microusd"] == 100000
    assert (
        ledger["requests"][invocation["request_id"]]["deadline_at"] == "2026-09-06T20:04:00.000000Z"
    )
    monkeypatch.setattr(
        module(), "execute_question_planning", lambda *a, **k: pytest.fail("new work")
    )
    events = []
    result = await execute(values, stage_events=events.append)
    assert result["state"] == "unavailable"
    assert result["result_digest"] == status["result_digest"]
    record = store._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    ).value
    assert record["response"]["reason"] == "request_cancelled"
    assert [(e["stage"], e["event"]) for e in events] == [
        ("publication", "entered"),
        ("publication", "completed"),
    ]
    again = store.cancel(invocation["request_id"], scope=fixture.scope(), now=fixture.NOW)
    assert again["result_digest"] == status["result_digest"]
    with pytest.raises(Exception, match="scope_invalid"):
        store.cancel(
            invocation["request_id"],
            scope=fixture.scope() | {"client_scope_id": "other_scope"},
            now=fixture.NOW,
        )


def test_reason_maps_validation_failures_to_claim_support_failed():
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    subject = module()
    assert (
        subject._reason(QuestionStoreError("answer_binding_invalid"), "validation")
        == "claim_support_failed"
    )
    assert subject._reason(ValueError("private detail"), "validation") == "claim_support_failed"
    assert subject._reason(QuestionStoreError("storage_unavailable"), "validation") == (
        "storage_unavailable"
    )


@pytest.mark.parametrize(
    ("stage", "state", "expected"),
    [
        ("planning", "held", "held"),
        ("answering", "held", "held"),
        ("publication", "held", "held"),
        ("retrieval", "held", "failed"),
        ("validation", "held", "failed"),
        ("queue", "held", "failed"),
        ("retrieval", "unavailable", "failed"),
        ("planning", "unavailable", "failed"),
    ],
)
def test_failure_event_holds_only_where_the_table_allows(stage, state, expected):
    subject = module()
    assert subject._failure_event(stage, state) == expected
    assert (stage, expected) in subject.STAGE_TRANSITIONS


@pytest.mark.asyncio
async def test_validation_failure_emits_the_validation_stage_with_a_safe_reason(monkeypatch):
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    monkeypatch.setattr(
        module(),
        "build_general_question_snapshot",
        lambda *a, **k: {
            "contract_version": "general_question_snapshot_build_v1",
            "status": "admitted",
        },
    )

    async def stubbed_answer(*args, **kwargs):
        return {
            "response": {"answer": "PRIVATE draft", "sources": [], "intelligence": {}},
            "state": "complete",
            "plan_digest": "0" * 64,
            "snapshot_digest": "1" * 64,
        }

    monkeypatch.setattr(module(), "execute_question_answering", stubbed_answer)

    def refused(*args, **kwargs):
        raise QuestionStoreError("answer_binding_invalid")

    monkeypatch.setattr(module(), "_validation_material", refused)
    events, diagnostics = [], []
    result = await execute(values, diagnostics=diagnostics.append, stage_events=events.append)
    assert result["state"] == "unavailable"
    record = (
        values[0]
        ._objects.read(
            f"requests/{result['request_id']}/results/{result['result_digest']}.json",
            generation=int(result["result_generation"]),
        )
        .value
    )
    assert record["response"]["reason"] == "claim_support_failed"
    assert [(e["stage"], e["event"], e["reason_code"]) for e in events] == [
        ("planning", "entered", None),
        ("planning", "completed", None),
        ("retrieval", "entered", None),
        ("retrieval", "completed", None),
        ("answering", "entered", None),
        ("answering", "completed", None),
        ("validation", "entered", None),
        ("validation", "failed", "claim_support_failed"),
        ("publication", "entered", None),
        ("publication", "completed", None),
    ]
    assert {d["phase"] for d in diagnostics} <= {
        "execution",
        "planning",
        "retrieval",
        "answering",
        "result",
    }
    assert ("result", "failed", "claim_support_failed") in {
        (d["phase"], d["state"], d["code"]) for d in diagnostics
    }
    assert "PRIVATE" not in json.dumps(events) + json.dumps(diagnostics)


@pytest.mark.asyncio
async def test_retrieval_failure_with_unresolved_usage_emits_failed_not_held(monkeypatch):
    values = runtime_fixture.setup_runtime()
    store, _, invocation, _, _ = values
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    monkeypatch.setattr(
        module(),
        "build_general_question_snapshot",
        lambda *a, **k: {
            "contract_version": "general_question_snapshot_build_v1",
            "status": "coverage_gap",
            "reason": "coverage_incomplete",
            "missing_work": ["coverage_incomplete"],
        },
    )
    real_usage = module().observed_result_usage

    def unresolved(*args, **kwargs):
        usage = real_usage(*args, **kwargs)
        usage.update(status="unresolved", reason="response_usage_unavailable", model_calls=None)
        return usage

    monkeypatch.setattr(module(), "observed_result_usage", unresolved)
    published = []

    def fake_publish(request_id, *, scope, response, state, now, **kwargs):
        published.append((state, response["reason"]))
        return {
            "request_id": request_id,
            "request_digest": invocation["request_digest"],
            "intake_digest": invocation["intake_digest"],
            "result_digest": "2" * 64,
            "result_generation": "1",
            "state": state,
        }

    monkeypatch.setattr(store, "publish_result", fake_publish)
    events = []
    result = await execute(values, stage_events=events.append)
    assert result["state"] == "held"
    assert published == [("held", "retrieval_incomplete")]
    assert [(e["stage"], e["event"], e["reason_code"]) for e in events][2:] == [
        ("retrieval", "entered", None),
        ("retrieval", "failed", "retrieval_incomplete"),
        ("publication", "entered", None),
        ("publication", "held", "retrieval_incomplete"),
    ]


def history_plan_draft():
    draft = runtime_fixture.plan_draft()
    draft["requirements"] = [
        *draft["requirements"],
        {
            "requirement_id": "history",
            "question": "What is the earlier history?",
            "kind": "history",
            "mandatory": True,
            "search_terms": [],
        },
    ]
    return draft


def capture_history_resolver(monkeypatch):
    captured = {}
    outcome = {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "coverage_gap",
        "snapshot": None,
        "reason": "evidence_insufficient",
        "missing_work": ["no_matching_evidence"],
    }

    def snapshot(*_args, **kwargs):
        captured["kwargs"] = kwargs
        return outcome

    monkeypatch.setattr(module(), "build_general_question_snapshot", snapshot)
    monkeypatch.setattr(
        module(), "execute_question_answering", lambda *a, **k: pytest.fail("answer call")
    )
    return captured


@pytest.mark.asyncio
async def test_worker_hands_the_snapshot_builder_a_history_resolver(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import UnresolvedHistory

    values = runtime_fixture.setup_runtime()
    draft = history_plan_draft()
    runtime_fixture.intercept(monkeypatch, draft)
    captured = capture_history_resolver(monkeypatch)
    await execute(values)

    resolver = captured["kwargs"]["history_resolver"]
    assert callable(resolver)
    answers = resolver(draft)
    assert set(answers) == {"history"}
    answer = answers["history"]
    assert isinstance(answer, UnresolvedHistory)
    assert answer.reason == "historical_sources_unavailable"


@pytest.mark.asyncio
async def test_worker_history_resolver_reads_the_retained_bridge_when_a_source_is_admitted(
    monkeypatch,
):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecordAnswer,
        RetainedHistorySource,
    )

    from tests.unit import test_historical_provenance_v2 as bridge_fixture

    current, candidate, rows = bridge_fixture.retained_analogue_material()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: bridge_fixture.FakeClient(rows))
    monkeypatch.setattr(
        module(),
        "retained_history_material",
        lambda context, scope, plan: {
            "frame": bridge_fixture.frame(),
            "sources": (RetainedHistorySource("history", current, (candidate,)),),
            "rules": bridge_fixture.analogue_rules(),
        },
    )
    values = runtime_fixture.setup_runtime()
    draft = history_plan_draft()
    runtime_fixture.intercept(monkeypatch, draft)
    captured = capture_history_resolver(monkeypatch)
    await execute(values)

    answer = captured["kwargs"]["history_resolver"](draft)["history"]
    assert isinstance(answer, HistoryRecordAnswer)
    assert answer.record.source_object_id == candidate.signal_id
    assert answer.record.reader_mode == "runtime_view"


def test_retained_history_material_is_absent_until_a_native_cutoff_is_authorized():
    assert module().retained_history_material({}, {}, {"requirements": []}) is None


def test_the_retained_history_seam_is_an_explicit_switch_that_is_off(monkeypatch):
    """The seam reads a named switch, so both of its sides run as written.

    Production runs the unset side, which is why every question with a history
    requirement resolves to historical_sources_unavailable and no history
    record is ever built outside a test. Nothing below this switch has run
    against a real retained source.
    """
    assert module().RETAINED_HISTORY_SOURCE_PROVIDER is None
    assert module().retained_history_material({}, {}, {"requirements": []}) is None

    calls = []

    def provider(context, scope, plan):
        calls.append((context, scope, plan))
        return {"frame": "frame", "sources": (), "rules": "rules"}

    monkeypatch.setattr(module(), "RETAINED_HISTORY_SOURCE_PROVIDER", provider)
    material = module().retained_history_material({"a": 1}, {"b": 2}, {"requirements": []})
    assert material == {"frame": "frame", "sources": (), "rules": "rules"}
    assert calls == [({"a": 1}, {"b": 2}, {"requirements": []})]


def test_retrieval_checks_carry_only_check_codes_and_never_raw_text():
    checks = module()._retrieval_checks
    # Internal builder reasons stay behind the generic public code in the published field.
    assert checks({"reason": "plan_invalid", "missing_work": ["plan_invalid"]}) == ()
    assert checks({"reason": "query_budget_invalid", "missing_work": []}) == ()
    assert checks(
        {"reason": "coverage_incomplete", "missing_work": ["No planned requirement matched."]}
    ) == ("coverage_incomplete",)
    assert checks({"reason": 403, "missing_work": ["plan_invalid"]}) == ()
    assert checks({"reason": "plan_invalid", "missing_work": ["coverage_incomplete"]}) == (
        "coverage_incomplete",
    )
    assert checks({"reason": "RAW provider text", "missing_work": []}) == ()
    assert checks({"reason": "x" * 65, "missing_work": []}) == ()
    assert checks({"reason": None, "missing_work": ["RAW_SENTINEL"]}) == ()


def _cause_events(diagnostics):
    return [
        event
        for event in diagnostics
        if event.get("contract_version") == "general_question_retrieval_cause_v1"
    ]


@pytest.mark.asyncio
async def test_retrieval_refusal_cause_is_logged_before_the_generic_code(monkeypatch):
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    diagnostics = []
    refusal = {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "refused",
        "snapshot": None,
        "reason": "plan_invalid",
        "missing_work": ["plan_invalid", "RAW_SENTINEL provider failure text", {"raw": 1}],
    }
    monkeypatch.setattr(module(), "build_general_question_snapshot", lambda *a, **k: refusal)
    result = await execute(values, diagnostics=diagnostics.append)
    record = values[0]._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    response = record.value["response"]
    assert response["reason"] == "retrieval_incomplete"
    assert response["intelligence"]["missing_work"] == ["retrieval_incomplete"]
    causes = _cause_events(diagnostics)
    assert len(causes) == 1
    cause = causes[0]
    assert cause == {
        "contract_version": "general_question_retrieval_cause_v1",
        "request_id": result["request_id"],
        "phase": "retrieval",
        "public_code": "retrieval_incomplete",
        "exception_type": "_RetrievalIncomplete",
        "exception_code": "retrieval_incomplete",
        "builder_status": "refused",
        "builder_reason": "plan_invalid",
        "builder_missing_work": ["plan_invalid"],
        "builder_missing_work_omitted": 2,
        "elapsed_ms": cause["elapsed_ms"],
    }
    assert type(cause["elapsed_ms"]) is int
    failed = next(
        index
        for index, event in enumerate(diagnostics)
        if event.get("phase") == "retrieval" and event.get("state") == "failed"
    )
    assert diagnostics.index(cause) < failed
    assert "RAW_SENTINEL" not in json.dumps(diagnostics)
    assert "RAW_SENTINEL" not in json.dumps(response)


@pytest.mark.asyncio
async def test_retrieval_exception_cause_is_logged_without_its_message(monkeypatch):
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    diagnostics = []

    def exploding(*args, **kwargs):
        raise RuntimeError("PRIVATE_SENTINEL transport detail")

    monkeypatch.setattr(module(), "build_general_question_snapshot", exploding)
    result = await execute(values, diagnostics=diagnostics.append)
    record = values[0]._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    assert record.value["response"]["reason"] == "retrieval_incomplete"
    assert record.value["response"]["intelligence"]["missing_work"] == ["retrieval_incomplete"]
    causes = _cause_events(diagnostics)
    assert len(causes) == 1
    assert causes[0]["exception_type"] == "RuntimeError"
    assert causes[0]["exception_code"] is None
    assert causes[0]["builder_status"] is None
    assert causes[0]["builder_reason"] is None
    assert causes[0]["builder_missing_work"] is None
    assert causes[0]["builder_missing_work_omitted"] == 0
    assert "PRIVATE_SENTINEL" not in json.dumps(diagnostics)


@pytest.mark.asyncio
async def test_retrieval_cause_is_not_logged_for_other_phases(monkeypatch):
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, "not json at all")
    diagnostics = []
    await execute(values, diagnostics=diagnostics.append)
    assert diagnostics[-2]["phase"] == "planning"
    assert diagnostics[-2]["state"] == "failed"
    assert _cause_events(diagnostics) == []


_PINNED_BUILDER_REASONS = [
    "coverage_incomplete",
    "corroborated_foreign_local_only",
    "parent_source_unavailable",
    "protected_context_registry_invalid",
    "copy_schema_invalid",
    "current_source_copy_invalid",
    "physical_schema_invalid",
    "release_admission_invalid",
    "release_material_invalid",
    "snapshot_source_copy_binding_invalid",
    "source_validation_incomplete",
    "plan_invalid",
    "plan_unavailable",
    "plan_conflict",
    "query_budget_invalid",
    "history_resolution_invalid",
    "snapshot_readback_invalid",
    "snapshot_validation_failed",
]


def test_published_builder_reason_vocabulary_is_pinned_by_name():
    assert frozenset(_PINNED_BUILDER_REASONS) == module()._PUBLIC_BUILDER_REASONS
    assert len(_PINNED_BUILDER_REASONS) == len(set(_PINNED_BUILDER_REASONS))
    assert frozenset(_PINNED_RETRIEVAL_CHECK_CODES) == module()._RETRIEVAL_CHECK_CODES
    assert set(_PINNED_RETRIEVAL_CHECK_CODES) <= set(_PINNED_BUILDER_REASONS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["protected_context_registry_invalid", "unlisted_registry_refusal", "RAW registry text"],
)
async def test_registry_builder_refusal_reaches_published_missing_work_safely(monkeypatch, reason):
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    diagnostics = []
    refusal = {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "refused",
        "snapshot": None,
        "reason": reason,
        "missing_work": [reason, "PRIVATE_SENTINEL provider text", "unlisted_registry_refusal"],
    }
    monkeypatch.setattr(
        module(), "build_general_question_snapshot", lambda *args, **kwargs: refusal
    )
    result = await execute(values, diagnostics=diagnostics.append)
    record = values[0]._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    expected = (
        ["protected_context_registry_invalid"]
        if reason == "protected_context_registry_invalid"
        else []
    )
    response = record.value["response"]
    assert response["reason"] == "retrieval_incomplete"
    assert response["intelligence"]["missing_work"] == ["retrieval_incomplete", *expected]
    assert _cause_events(diagnostics)[0]["builder_missing_work"] == expected
    assert "PRIVATE_SENTINEL" not in json.dumps([response, diagnostics])
    assert "RAW registry text" not in json.dumps([response, diagnostics])
    assert "unlisted_registry_refusal" not in json.dumps(response)


def test_retrieval_checks_drop_a_reason_outside_the_pinned_vocabulary():
    checks = module()._retrieval_checks
    for reason in ("query_billing_unknown", "query_policy_overage", "identity_invalid"):
        assert checks({"reason": reason, "missing_work": [reason]}) == ()
    assert checks({"reason": "query_billing_unknown", "missing_work": ["coverage_incomplete"]}) == (
        "coverage_incomplete",
    )
    # The builder's own reasons are internal too and are not published.
    assert checks({"reason": "plan_invalid", "missing_work": ["plan_invalid"]}) == ()
    assert checks({"reason": "plan_conflict", "missing_work": ["plan_conflict"]}) == ()


def test_retrieval_cause_keeps_only_pinned_codes_in_builder_missing_work():
    from src.analysis.open_intelligence.general_question_snapshot import _failure

    cause = module()._retrieval_cause
    error = module()._RetrievalIncomplete((), None)
    # The reviewer's proof: plan requirement ids are model output derived from the question.
    refusal = _failure(
        "coverage_incomplete", "sunday_times_readership_decline_za", "news24_migration_bsa_kenya"
    )
    line = cause(error, refusal)
    assert line["builder_reason"] == "coverage_incomplete"
    assert line["builder_missing_work"] == []
    assert line["builder_missing_work_omitted"] == 2
    assert "sunday_times" not in json.dumps(line)
    mixed = cause(
        error,
        _failure(
            "coverage_incomplete",
            "coverage_incomplete",
            "sunday_times_readership_decline_za",
            "market:za",
            "window:2026-09-01/2026-09-07",
            "corroborated_foreign_local_only",
        ),
    )
    assert mixed["builder_missing_work"] == [
        "coverage_incomplete",
        "corroborated_foreign_local_only",
    ]
    assert mixed["builder_missing_work_omitted"] == 3
    # A code shaped string that is not in the vocabulary never reaches the line either.
    shaped = cause(
        error, {"status": "refused", "reason": "x", "missing_work": ["query_billing_unknown"]}
    )
    assert shaped["builder_missing_work"] == []
    assert shaped["builder_missing_work_omitted"] == 1


@pytest.mark.asyncio
async def test_unlisted_builder_reason_falls_back_to_the_generic_public_code(monkeypatch):
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    diagnostics = []
    refusal = {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "unavailable",
        "snapshot": None,
        "reason": "query_billing_unknown",
        "missing_work": ["query_billing_unknown"],
    }
    monkeypatch.setattr(module(), "build_general_question_snapshot", lambda *a, **k: refusal)
    result = await execute(values, diagnostics=diagnostics.append)
    record = values[0]._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    response = record.value["response"]
    assert response["reason"] == "retrieval_incomplete"
    assert response["intelligence"]["missing_work"] == ["retrieval_incomplete"]
    assert "query_billing_unknown" not in json.dumps(response)
    # The diagnostic line still names the builder's reason for the operator.
    causes = _cause_events(diagnostics)
    assert len(causes) == 1
    assert causes[0]["builder_reason"] == "query_billing_unknown"
    assert causes[0]["builder_missing_work"] == []
    assert causes[0]["builder_missing_work_omitted"] == 1


@pytest.mark.asyncio
async def test_requirement_ids_in_builder_missing_work_never_reach_the_diagnostic_line(monkeypatch):
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    diagnostics = []
    refusal = {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "coverage_gap",
        "snapshot": None,
        "reason": "coverage_incomplete",
        "missing_work": ["sunday_times_readership_decline_za", "news24_migration_bsa_kenya"],
    }
    monkeypatch.setattr(module(), "build_general_question_snapshot", lambda *a, **k: refusal)
    result = await execute(values, diagnostics=diagnostics.append)
    record = values[0]._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    response = record.value["response"]
    assert response["intelligence"]["missing_work"] == [
        "retrieval_incomplete",
        "coverage_incomplete",
    ]
    causes = _cause_events(diagnostics)
    assert len(causes) == 1
    assert causes[0]["builder_reason"] == "coverage_incomplete"
    assert causes[0]["builder_missing_work"] == []
    assert causes[0]["builder_missing_work_omitted"] == 2
    assert "sunday_times" not in json.dumps(diagnostics)
    assert "news24" not in json.dumps(diagnostics)


_PINNED_RETRIEVAL_CHECK_CODES = [
    "coverage_incomplete",
    "corroborated_foreign_local_only",
    "parent_source_unavailable",
    "protected_context_registry_invalid",
    "copy_schema_invalid",
    "current_source_copy_invalid",
    "physical_schema_invalid",
    "release_admission_invalid",
    "release_material_invalid",
    "snapshot_source_copy_binding_invalid",
    "source_validation_incomplete",
]
_INTERNAL_BUILDER_REASONS = sorted(set(_PINNED_BUILDER_REASONS) - set(_PINNED_RETRIEVAL_CHECK_CODES))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", _INTERNAL_BUILDER_REASONS)
async def test_internal_builder_reasons_never_reach_the_published_missing_work(monkeypatch, reason):
    """The published field keeps the user facing vocabulary its contract version names.

    The builder's own refusal codes are internal; the frontend prints an unknown
    missing_work code raw, so they stay on the operator's diagnostic line and the
    published result carries the generic public code alone.
    """
    values = runtime_fixture.setup_runtime()
    runtime_fixture.intercept(monkeypatch, runtime_fixture.plan_draft())
    diagnostics = []
    refusal = {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "refused",
        "snapshot": None,
        "reason": reason,
        "missing_work": [reason],
    }
    monkeypatch.setattr(module(), "build_general_question_snapshot", lambda *a, **k: refusal)
    result = await execute(values, diagnostics=diagnostics.append)
    record = values[0]._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json",
        generation=int(result["result_generation"]),
    )
    response = record.value["response"]
    assert response["intelligence"]["missing_work"] == ["retrieval_incomplete"]
    assert reason not in json.dumps(response)
    assert module()._retrieval_checks(refusal) == ()
    (cause,) = _cause_events(diagnostics)
    assert cause["builder_reason"] == reason
    assert cause["builder_missing_work"] == [reason]
