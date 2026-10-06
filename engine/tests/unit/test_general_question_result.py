import copy
import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from src.analysis.open_intelligence.general_question_control import QuestionStoreError
from src.analysis.open_intelligence.general_question_plan import validate_question_plan

from tests.unit import test_general_question_calls as calls_fixture
from tests.unit import test_general_question_store as fixture
from tests.unit.test_general_question_runtime import plan_draft


def setup():
    _, calls, bucket, request_id = calls_fixture.setup()
    return calls.store, calls, bucket, request_id


def response(store, request_id):
    module = importlib.import_module("src.analysis.open_intelligence.general_question_result")
    context = store.read_request(request_id, scope=fixture.scope())
    return module.unavailable_response(
        context["request"],
        module.observed_result_usage(store, request_id=request_id, scope=fixture.scope()),
        reason="request_expired",
    )


def publish(store, request_id, **changes):
    values = {
        "scope": fixture.scope(),
        "response": response(store, request_id),
        "state": "unavailable",
        "now": fixture.NOW,
    }
    values.update(changes)
    return store.publish_result(request_id, **values)


def test_publication_selects_exact_generation_and_retry_reuses_record():
    store, _, bucket, rid = setup()
    first = publish(store, rid)
    second = publish(store, rid, now=fixture.NOW + timedelta(seconds=1))
    assert first == second
    status = store.status(rid, scope=fixture.scope(), now=fixture.NOW)
    assert status["result_digest"] == first["result_digest"]
    raw = bucket.versions[
        (
            fixture.PREFIX + f"requests/{rid}/results/{first['result_digest']}.json",
            int(first["result_generation"]),
        )
    ]
    assert hashlib.sha256(raw).hexdigest() == first["result_digest"]
    assert json.loads(raw)["recorded_at"] == fixture.NOW.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    assert (
        json.loads(bucket.objects[fixture.PREFIX + fixture.LEDGER][1])["reserved_microusd"]
        == 100000
    )


def test_lost_control_ack_and_racing_identical_publications_select_one():
    store, _, bucket, rid = setup()
    bucket.lose_ack.add(fixture.PREFIX + fixture.LEDGER)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(store, rid), range(2)))
    assert results[0] == results[1]


def test_result_selected_between_the_two_usage_reads_acknowledges_the_selected_result(
    monkeypatch,
):
    """The usage projection behind a publication reads the admission twice. The winner's
    control write landing between them moves only the result pointer, which the projection
    never reads: the loser must re-read and end in the lost control acknowledgement."""
    store, _, _, rid = setup()
    values = {
        "scope": fixture.scope(),
        "response": response(store, rid),
        "state": "unavailable",
        "now": fixture.NOW,
    }
    usage = importlib.import_module("src.analysis.open_intelligence.general_question_usage")
    state = {"reads": 0, "armed": True, "winner": None}

    class Interleaved(usage.GeneralQuestionCalls):
        def _context(self, request_id, scope):
            if state["armed"]:
                state["reads"] += 1
                if state["reads"] == 2:
                    state["armed"] = False
                    state["winner"] = publish(store, rid)
            return super()._context(request_id, scope)

    monkeypatch.setattr(usage, "GeneralQuestionCalls", Interleaved)
    loser = store.publish_result(rid, **values)
    assert state["winner"] is not None
    assert loser == state["winner"]
    status = store.status(rid, scope=fixture.scope(), now=fixture.NOW)
    assert status["result_digest"] == loser["result_digest"]


def test_control_written_between_the_two_context_reads_acknowledges_the_selected_result(
    monkeypatch,
):
    """A publication reads the request and then the control in two round trips. The
    winner's control write landing between them is a torn snapshot, not a conflict: the
    loser must re-read and end in the lost control acknowledgement, never raise."""
    store, _, bucket, rid = setup()
    bucket.lose_ack.add(fixture.PREFIX + fixture.LEDGER)
    values = {
        "scope": fixture.scope(),
        "response": response(store, rid),
        "state": "unavailable",
        "now": fixture.NOW,
    }
    original = store._control
    state = {"reads": 0, "armed": True, "winner": None}

    def torn_control():
        if state["armed"]:
            state["reads"] += 1
            if state["reads"] == 2:
                state["armed"] = False
                state["winner"] = publish(store, rid)
        return original()

    monkeypatch.setattr(store, "_control", torn_control)
    loser = store.publish_result(rid, **values)
    assert state["winner"] is not None
    assert loser == state["winner"]
    status = store.status(rid, scope=fixture.scope(), now=fixture.NOW)
    assert status["result_digest"] == loser["result_digest"]


def test_conflicting_terminal_response_never_overwrites_pointer():
    store, _, _, rid = setup()
    first = publish(store, rid)
    changed = response(store, rid)
    changed["answer"] = "A different terminal response."
    with pytest.raises(QuestionStoreError):
        publish(store, rid, response=changed)
    assert (
        store.status(rid, scope=fixture.scope(), now=fixture.NOW)["result_digest"]
        == first["result_digest"]
    )


@pytest.mark.parametrize(
    "mutation", ["unknown", "orphan_request", "scope", "missing_result", "tampered_result"]
)
def test_status_refuses_missing_authority(mutation):
    store, _, bucket, rid = setup()
    if mutation in {"missing_result", "tampered_result"}:
        ack = publish(store, rid)
        key = fixture.PREFIX + f"requests/{rid}/results/{ack['result_digest']}.json"
        if mutation == "missing_result":
            del bucket.versions[(key, int(ack["result_generation"]))]
        else:
            bucket.versions[(key, int(ack["result_generation"]))] = b"{}"
    elif mutation in {"unknown", "orphan_request"}:
        control = json.loads(bucket.objects[fixture.PREFIX + fixture.LEDGER][1])
        control["requests"] = {}
        control["reserved_microusd"] = 0
        bucket.seed(fixture.LEDGER, control)
    scope = fixture.scope()
    if mutation == "scope":
        scope["market_scope"] = ["ng"]
    with pytest.raises(QuestionStoreError):
        store.status(rid, scope=scope, now=fixture.NOW)


def test_expired_admitted_becomes_durable_unavailable():
    store, _, bucket, rid = setup()
    assert store.status(rid, scope=fixture.scope(), now=fixture.NOW)["state"] == "admitted"
    result = store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))
    assert result["state"] == "unavailable"
    assert result["result_generation"] is not None
    record = json.loads(
        bucket.objects[fixture.PREFIX + f"requests/{rid}/results/{result['result_digest']}.json"][1]
    )
    assert record["response"]["intelligence"]["resolved_scope"]["market_scope"] == fixture.scope()[
        "market_scope"
    ]


def test_expired_request_preserves_open_stored_plan_window_and_digest():
    store, _, bucket, rid = setup()
    context = store.read_request(rid, scope=fixture.scope())
    draft = plan_draft()
    draft["window"] = {"start": "2026-09-01", "end": "2026-09-05", "closed": False}
    plan = validate_question_plan(draft, request=context["request"], intake=context["intake"])
    bucket.seed(f"requests/{rid}/plan.json", plan)

    status = store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))
    record = json.loads(
        bucket.objects[fixture.PREFIX + f"requests/{rid}/results/{status['result_digest']}.json"][1]
    )

    assert record["plan_digest"] == plan["plan_digest"]
    assert record["response"]["intelligence"]["window"] == plan["window"]
    assert record["response"]["intelligence"]["resolved_scope"]["market_scope"] == plan["markets"]


@pytest.mark.parametrize("field", ["window", "markets"])
def test_expired_request_refuses_invalid_stored_plan_before_publication(field):
    store, _, bucket, rid = setup()
    context = store.read_request(rid, scope=fixture.scope())
    plan = validate_question_plan(
        plan_draft(), request=context["request"], intake=context["intake"]
    )
    if field == "window":
        plan["window"]["start"] = "2026-09-02"
    else:
        plan["markets"] = ["ng"]
    bucket.seed(f"requests/{rid}/plan.json", plan)

    with pytest.raises(QuestionStoreError):
        store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))
    assert "result" not in store.read_request(rid, scope=fixture.scope())["admission"]


def test_expiry_does_not_rewrite_previously_published_scope():
    store, _, bucket, rid = setup()
    context = store.read_request(rid, scope=fixture.scope())
    plan = validate_question_plan(
        plan_draft(), request=context["request"], intake=context["intake"]
    )
    bucket.seed(f"requests/{rid}/plan.json", plan)
    prior = publish(store, rid, plan_digest=plan["plan_digest"])
    key = fixture.PREFIX + f"requests/{rid}/results/{prior['result_digest']}.json"
    before = bucket.objects[key]
    status = store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))
    assert status["result_digest"] == prior["result_digest"]
    assert bucket.objects[key] == before
    record = json.loads(bucket.objects[key][1])
    assert record["response"]["intelligence"]["resolved_scope"]["market_scope"] == fixture.scope()[
        "market_scope"
    ]


def test_expired_unknown_call_is_held_and_can_reconcile_late_known_usage():
    store, calls, bucket, rid = setup()
    permit = calls_fixture.claim(calls, rid)
    first = store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))
    assert first["state"] == "held"
    record = json.loads(
        bucket.objects[fixture.PREFIX + f"requests/{rid}/results/{first['result_digest']}.json"][1]
    )
    assert record["response"]["intelligence"]["usage"]["model_calls"] is None
    calls.record_response(
        permit,
        scope=fixture.scope(),
        response=calls_fixture.native(),
        received_at=fixture.NOW + timedelta(seconds=1),
    )
    calls.persist_usage(rid, stage="planning", scope=fixture.scope(), persist=lambda event: event)
    second = store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=242))
    assert second["state"] == "unavailable"
    assert second["result_digest"] != first["result_digest"]
    assert (
        fixture.PREFIX + f"requests/{rid}/results/{first['result_digest']}.json" in bucket.objects
    )


@pytest.mark.parametrize("state", ["complete", "partial", "held"])
def test_grounded_reply_requires_actual_bound_plan_snapshot_and_validator(state):
    store, _, _, rid = setup()
    reply = response(store, rid)
    reply["intelligence"]["status"] = "partial"
    reply["intelligence"]["snapshot_id"] = "snapshot"
    with pytest.raises(QuestionStoreError):
        publish(
            store, rid, response=reply, state=state, plan_digest="a" * 64, snapshot_digest="b" * 64
        )


def test_object_size_limit_refuses_without_result_pointer():
    store, _, _, rid = setup()
    reply = response(store, rid)
    reply["answer"] = "x" * (4 * 1024 * 1024)
    with pytest.raises(QuestionStoreError):
        publish(store, rid, response=reply)
    assert "result" not in store.read_request(rid, scope=fixture.scope())["admission"]


def answered_fixture(store, bucket, rid):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.general_question_plan import validate_question_plan

    from tests.unit.test_general_question_runtime import plan_draft

    context = store.read_request(rid, scope=fixture.scope())
    plan = validate_question_plan(
        plan_draft(), request=context["request"], intake=context["intake"]
    )
    bucket.seed(f"requests/{rid}/plan.json", plan)
    snapshot = {
        "contract_version": "general_question_snapshot_v1",
        "snapshot_id": "synthetic_snapshot",
        "request_id": rid,
        "plan_digest": plan["plan_digest"],
        **{
            key: context["admission"][key]
            for key in ("request_digest", "intake_digest", "policy_digest", "deployment_digest")
        },
    }
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    bucket.seed(f"requests/{rid}/snapshot.json", snapshot)
    reply = response(store, rid)
    reply.pop("error")
    reply.pop("reason")
    reply["intelligence"].update(
        status="partial",
        snapshot_id=snapshot["snapshot_id"],
        claims=[{"claim_id": "synthetic"}],
        receipts=[{"receipt_id": "synthetic"}],
        sections=[{"kind": "answer", "claim_ids": ["synthetic"]}],
    )
    return reply, plan["plan_digest"], snapshot["snapshot_digest"]


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "missing_plan",
        "missing_snapshot",
        "changed_snapshot",
        "missing_validator",
        "validator_rejects",
    ],
)
def test_answered_bindings_and_trusted_validation_path_are_required(mutation):
    store, _, bucket, rid = setup()
    reply, plan, snapshot = answered_fixture(store, bucket, rid)
    invocations = []

    def trusted(value, **context):
        invocations.append(context)
        if mutation == "validator_rejects":
            raise ValueError("synthetic support rejection")
        return value

    if mutation in {"missing_plan", "missing_snapshot"}:
        key = "plan" if mutation == "missing_plan" else "snapshot"
        del bucket.objects[fixture.PREFIX + f"requests/{rid}/{key}.json"]
    elif mutation == "changed_snapshot":
        bucket.seed(f"requests/{rid}/snapshot.json", {"snapshot_digest": snapshot})
    kwargs = {
        "response": reply,
        "state": "partial",
        "plan_digest": plan,
        "snapshot_digest": snapshot,
        "answer_validator": None if mutation == "missing_validator" else trusted,
    }
    if mutation:
        with pytest.raises(QuestionStoreError):
            publish(store, rid, **kwargs)
    else:
        publish(store, rid, **kwargs)
        assert (
            store.status(rid, scope=fixture.scope(), now=fixture.NOW, answer_validator=trusted)[
                "state"
            ]
            == "partial"
        )
        assert len(invocations) >= 2
        with pytest.raises(QuestionStoreError, match="result_support_unverified"):
            store.status(rid, scope=fixture.scope(), now=fixture.NOW)


def test_expired_known_response_without_metering_remains_held():
    store, calls, bucket, rid = setup()
    permit = calls_fixture.claim(calls, rid)
    calls.record_response(
        permit,
        scope=fixture.scope(),
        response=calls_fixture.native(),
        received_at=fixture.NOW + timedelta(seconds=1),
    )
    status = store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))
    assert status["state"] == "held"
    raw = bucket.objects[fixture.PREFIX + f"requests/{rid}/results/{status['result_digest']}.json"][
        1
    ]
    assert json.loads(raw)["response"]["intelligence"]["usage"]["model_calls"] == 1


def test_permanent_metering_failure_never_unholds_after_acknowledgement():
    store, calls, _, rid = setup()
    permit = calls_fixture.claim(calls, rid)
    calls.record_response(
        permit,
        scope=fixture.scope(),
        response=calls_fixture.native(),
        received_at=fixture.NOW + timedelta(seconds=1),
    )

    def fail(_event):
        raise RuntimeError("synthetic metering failure")

    with pytest.raises(QuestionStoreError):
        calls.persist_usage(rid, stage="planning", scope=fixture.scope(), persist=fail)
    first = store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))
    calls.persist_usage(rid, stage="planning", scope=fixture.scope(), persist=lambda event: event)
    second = store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=242))
    assert first["state"] == second["state"] == "held"


def test_orphan_result_object_does_not_select_completion(monkeypatch):
    store, _, bucket, rid = setup()
    monkeypatch.setattr(store._objects, "compare_control", lambda *args: False)
    with pytest.raises(QuestionStoreError, match="control_conflict"):
        publish(store, rid)
    assert any("/results/" in key for key in bucket.objects)
    assert store.status(rid, scope=fixture.scope(), now=fixture.NOW)["state"] == "admitted"


def test_expired_pending_metering_holds_until_actual_acknowledgement():
    store, calls, _, rid = setup()
    permit = calls_fixture.claim(calls, rid)
    calls.record_response(
        permit,
        scope=fixture.scope(),
        response=calls_fixture.native(),
        received_at=fixture.NOW + timedelta(seconds=1),
    )
    observed = []

    def acknowledge(event):
        observed.append(
            store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))
        )
        assert (
            calls._context(rid, fixture.scope())[0]["admission"]["execution"]["calls"]["planning"][
                "usage_status"
            ]
            == "pending"
        )
        return event

    calls.persist_usage(rid, stage="planning", scope=fixture.scope(), persist=acknowledge)
    assert observed[0]["state"] == "held"
    assert (
        store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=242))["state"]
        == "unavailable"
    )


def test_status_recovers_only_bound_orphan_response(monkeypatch):
    store, calls, _, rid = setup()
    permit = calls_fixture.claim(calls, rid)
    compare = store._objects.compare_control
    monkeypatch.setattr(store._objects, "compare_control", lambda *args: False)
    with pytest.raises(QuestionStoreError, match="control_conflict"):
        calls.record_response(
            permit,
            scope=fixture.scope(),
            response=calls_fixture.native(),
            received_at=fixture.NOW + timedelta(seconds=1),
        )
    monkeypatch.setattr(store._objects, "compare_control", compare)
    assert (
        store.status(rid, scope=fixture.scope(), now=fixture.NOW + timedelta(seconds=241))["state"]
        == "held"
    )
    assert (
        calls._context(rid, fixture.scope())[0]["admission"]["execution"]["calls"]["planning"][
            "response"
        ]
        is not None
    )


@pytest.mark.parametrize(
    "pointer",
    [None, {"digest": "a" * 64, "generation": "0"}, {"digest": "a" * 64, "generation": True}],
)
def test_corrupt_terminal_pointer_refuses(pointer):
    store, _, bucket, rid = setup()
    control = json.loads(bucket.objects[fixture.PREFIX + fixture.LEDGER][1])
    control["requests"][rid]["result"] = pointer
    bucket.seed(fixture.LEDGER, control)
    with pytest.raises(QuestionStoreError, match="control_invalid"):
        store.status(rid, scope=fixture.scope(), now=fixture.NOW)
