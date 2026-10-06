import copy
import importlib
import json
from datetime import timedelta
from uuid import UUID

import pytest
from src.analysis.open_intelligence.general_question_request import normalize_question_request

from tests.unit import test_general_question_deployment as deployment_fixture
from tests.unit import test_general_question_store as store_fixture


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_admission")


def value(invocation, *, request_id=None, transport=None, scope=None, selected_market="za"):
    return {
        "contract_version": "general_question_admission_v1",
        "request_id": request_id or invocation["request_id"],
        "transport": transport or {"message": "Explain mobility question 1"},
        "scope": copy.deepcopy(scope or store_fixture.scope()),
        "selected_market": selected_market,
        "policy_digest": invocation["policy_digest"],
        "deployment_digest": invocation["deployment_digest"],
    }


def admit(store, invocation, runtime, **overrides):
    return module().admit_question_transport(
        value(invocation, **overrides),
        store=store,
        runtime_identity=runtime,
        now=store_fixture.NOW,
    )


def test_new_transport_admission_returns_only_execution_binding_and_preserves_stored_context():
    store, bucket, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=22))
    history = [{"role": "user", "content": str(index)} for index in range(10)]
    result = admit(
        store,
        invocation,
        runtime,
        request_id=request_id,
        transport={"message": "A new unseen question", "history": history},
        scope={**store_fixture.scope(), "brand_config_id": None, "theme_id": None},
        selected_market="ng",
    )

    assert set(result) == {
        "contract_version",
        "job_id",
        "invocation",
        "request_generation",
        "intake_generation",
        "deadline_at",
    }
    assert result["contract_version"] == "general_question_admission_result_v1"
    assert result["job_id"] == f"chat_{UUID(request_id).hex}"
    assert set(result["invocation"]) == {
        "contract_version",
        "request_id",
        "request_digest",
        "intake_digest",
        "policy_digest",
        "deployment_digest",
    }
    context = store.read_request(request_id, scope=store_fixture.scope())
    assert context["request"]["as_of"] == "2026-09-06T20:00:00.000000Z"
    assert context["request"]["history_omitted_turns"] == 2
    assert context["request"]["brand_config_id"] is None
    assert context["request"]["theme_id"] is None
    assert context["intake"]["selected_market"] == "ng"
    assert "question" not in result
    assert "history" not in json.dumps(result)
    assert result["request_generation"] == context["admission"]["request_generation"]
    assert result["intake_generation"] == context["admission"]["intake_generation"]
    assert len(bucket.objects) > 0


def test_exact_retry_reuses_as_of_deadline_generations_and_reservation_after_lost_ack():
    store, bucket, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=25))
    bucket.lose_ack.add(store_fixture.PREFIX + store_fixture.LEDGER)
    first = admit(
        store,
        invocation,
        runtime,
        request_id=request_id,
        transport={"message": "Lost acknowledgement question"},
    )
    second = admit(
        store,
        invocation,
        runtime,
        request_id=request_id,
        transport={"message": "Lost acknowledgement question"},
    )

    assert first == second
    assert first["deadline_at"] == "2026-09-06T20:04:00.000000Z"
    control = json.loads(bucket.objects[store_fixture.PREFIX + store_fixture.LEDGER][1])
    assert len(control["requests"]) == 2
    assert control["reserved_microusd"] == 200000


def test_fresh_immutable_orphan_reuses_original_timestamp_and_generation():
    store, bucket, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=26))
    orphan = normalize_question_request(
        {"message": "Fresh orphan"},
        scope=store_fixture.scope(),
        request_id=request_id,
        admitted_at=store_fixture.NOW - timedelta(seconds=30),
        policy_digest=invocation["policy_digest"],
    )
    bucket.seed(f"requests/{request_id}/request.json", orphan)
    generation = bucket.objects[store_fixture.PREFIX + f"requests/{request_id}/request.json"][0]

    result = admit(
        store,
        invocation,
        runtime,
        request_id=request_id,
        transport={"message": "Fresh orphan"},
    )

    assert result["request_generation"] == str(generation)
    assert result["deadline_at"] == "2026-09-06T20:03:30.000000Z"
    context = store.read_request(request_id, scope=store_fixture.scope())
    assert context["request"] == orphan


@pytest.mark.parametrize("change", ["text", "selection", "scope"])
def test_changed_retry_cannot_reuse_request_identity(change):
    store, _, invocation, runtime, _ = deployment_fixture.fixture()
    overrides = {}
    if change == "text":
        overrides["transport"] = {"message": "Different question"}
    elif change == "selection":
        overrides["selected_market"] = "ng"
    else:
        overrides["scope"] = {**store_fixture.scope(), "client_scope_id": "other_scope"}
    with pytest.raises(store_fixture.module().QuestionStoreError):
        admit(store, invocation, runtime, **overrides)


def test_stale_orphan_keeps_original_as_of_and_never_reserves():
    store, bucket, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=23))
    stale = normalize_question_request(
        {"message": "Stale orphan"},
        scope=store_fixture.scope(),
        request_id=request_id,
        admitted_at=store_fixture.NOW - timedelta(seconds=241),
        policy_digest=invocation["policy_digest"],
    )
    bucket.seed(f"requests/{request_id}/request.json", stale)
    before = json.loads(bucket.objects[store_fixture.PREFIX + store_fixture.LEDGER][1])

    with pytest.raises(store_fixture.module().QuestionStoreError, match="request_expired"):
        admit(
            store,
            invocation,
            runtime,
            request_id=request_id,
            transport={"message": "Stale orphan"},
        )
    after = json.loads(bucket.objects[store_fixture.PREFIX + store_fixture.LEDGER][1])
    assert after == before


@pytest.mark.parametrize("change", ["runtime", "policy", "deployment"])
def test_wrong_binding_refuses_before_request_generation(change):
    store, bucket, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=24))
    incoming = value(invocation, request_id=request_id, transport={"message": "Never stored"})
    if change == "runtime":
        runtime = {**runtime, "revision_name": "wrong"}
    elif change == "policy":
        incoming["policy_digest"] = "d" * 64
    else:
        incoming["deployment_digest"] = "e" * 64
    before = copy.deepcopy(bucket.objects)

    with pytest.raises(store_fixture.module().QuestionStoreError):
        module().admit_question_transport(
            incoming,
            store=store,
            runtime_identity=runtime,
            now=store_fixture.NOW,
        )
    assert bucket.objects == before


def test_handler_has_no_model_or_generation_dependency(monkeypatch):
    store, _, invocation, runtime, _ = deployment_fixture.fixture()
    runtime_module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_runtime"
    )
    monkeypatch.setattr(
        runtime_module,
        "create_question_model_client",
        lambda **_kwargs: pytest.fail("generation client used"),
    )
    assert admit(store, invocation, runtime)["invocation"] == invocation


def test_admission_input_has_exact_fields():
    store, _, invocation, runtime, _ = deployment_fixture.fixture()
    incoming = {**value(invocation), "as_of": "caller-controlled"}
    with pytest.raises(store_fixture.module().QuestionStoreError, match="request_invalid"):
        module().admit_question_transport(
            incoming,
            store=store,
            runtime_identity=runtime,
            now=store_fixture.NOW,
        )


PROHIBITED_WORDING = "Which party messaging reached undecided voters before the election?"


def bsa_scope():
    return {**store_fixture.scope(), "client_scope_id": "bsa_pulse", "brand_config_id": "bsa"}


def test_bsa_scope_refuses_a_prohibited_client_purpose_before_any_reservation():
    store, bucket, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=41))
    before = {name: value[1] for name, value in bucket.objects.items()}

    with pytest.raises(store_fixture.module().QuestionStoreError) as caught:
        admit(
            store,
            invocation,
            runtime,
            request_id=request_id,
            transport={"message": PROHIBITED_WORDING},
            scope=bsa_scope(),
        )

    assert caught.value.code == "client_purpose_refused_at_admission"
    assert {name: value[1] for name, value in bucket.objects.items()} == before
    assert store.read_request(request_id, scope=bsa_scope()) is None


def test_the_same_wording_under_general_42_follows_the_general_policy():
    store, _, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=42))
    result = admit(
        store,
        invocation,
        runtime,
        request_id=request_id,
        transport={"message": PROHIBITED_WORDING},
        scope=store_fixture.scope(),
    )
    assert result["contract_version"] == "general_question_admission_result_v1"
    context = store.read_request(request_id, scope=store_fixture.scope())
    assert context["request"]["question"] == PROHIBITED_WORDING
    assert context["request"]["brand_config_id"] is None


def test_bsa_scope_admits_a_permitted_question_with_its_brand_bound():
    store, _, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=43))
    result = admit(
        store,
        invocation,
        runtime,
        request_id=request_id,
        transport={"message": "What are Global South Africans saying about load shedding?"},
        scope=bsa_scope(),
    )
    assert result["job_id"] == f"chat_{UUID(request_id).hex}"
    context = store.read_request(request_id, scope=bsa_scope())
    assert context["request"]["brand_config_id"] == "bsa"


BSA_SCOPE = {
    "client_scope_id": "bsa_pulse",
    "market_scope": ["ke", "ng", "za"],
    "brand_config_id": "bsa",
    "audience_lens_ids": [],
    "theme_id": None,
}
LENS_ENVELOPE = {
    "lens_binding_version": "client_lens_binding_v1",
    "client_lens_id": "bsa_pulse_lens",
    "configuration_digest": "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf",
}


def lens_value(invocation, *, client_lens, request_id, scope=None):
    envelope = value(
        invocation,
        request_id=request_id,
        transport={"message": "What is being said about Play Your Part?"},
        scope=copy.deepcopy(scope if scope is not None else BSA_SCOPE),
    )
    if client_lens is not None:
        envelope["client_lens"] = copy.deepcopy(client_lens)
    return envelope


def admit_lens(store, invocation, runtime, **kwargs):
    return module().admit_question_transport(
        lens_value(invocation, **kwargs),
        store=store,
        runtime_identity=runtime,
        now=store_fixture.NOW,
    )


def test_an_admission_naming_no_lens_is_unchanged_and_stays_general():
    store, _bucket, invocation, runtime, _ = deployment_fixture.fixture()
    result = admit_lens(store, invocation, runtime, client_lens=None, request_id=str(UUID(int=51)))
    assert result["contract_version"] == "general_question_admission_result_v1"


def test_an_authorized_lens_is_admitted_after_the_engine_re_resolves_its_bytes():
    store, _bucket, invocation, runtime, _ = deployment_fixture.fixture()
    result = admit_lens(
        store,
        invocation,
        runtime,
        client_lens=LENS_ENVELOPE,
        request_id=str(UUID(int=52)),
    )
    assert result["job_id"] == "chat_" + UUID(int=52).hex


@pytest.mark.parametrize(
    "client_lens",
    [
        {**LENS_ENVELOPE, "configuration_digest": "f" * 64},
        {**LENS_ENVELOPE, "client_lens_id": "unregistered_lens"},
        {**LENS_ENVELOPE, "lens_binding_version": "client_lens_binding_v0"},
        {"client_lens_id": "bsa_pulse_lens", "configuration_digest": "a" * 64},
        {**LENS_ENVELOPE, "extra": "x"},
        "bsa_pulse_lens",
        [],
    ],
)
def test_a_forged_or_malformed_lens_envelope_is_refused(client_lens):
    """The envelope asserts a lens; the engine's own resolution decides it."""
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    store, _bucket, invocation, runtime, _ = deployment_fixture.fixture()
    with pytest.raises(QuestionStoreError) as caught:
        admit_lens(
            store,
            invocation,
            runtime,
            client_lens=client_lens,
            request_id=str(UUID(int=53)),
        )
    assert str(caught.value) in ("scope_invalid", "request_invalid")


def test_a_lens_from_another_client_scope_is_refused_at_admission():
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    store, _bucket, invocation, runtime, _ = deployment_fixture.fixture()
    with pytest.raises(QuestionStoreError) as caught:
        admit_lens(
            store,
            invocation,
            runtime,
            client_lens=LENS_ENVELOPE,
            request_id=str(UUID(int=54)),
            scope=store_fixture.scope(),
        )
    assert str(caught.value) == "scope_invalid"


def test_the_resolved_lens_is_carried_into_the_stored_request_not_discarded():
    """The same wording with and without a lens is two different durable records."""
    store, _bucket, invocation, runtime, _ = deployment_fixture.fixture()
    general = admit_lens(store, invocation, runtime, client_lens=None, request_id=str(UUID(int=61)))
    bound = admit_lens(
        store,
        invocation,
        runtime,
        client_lens=LENS_ENVELOPE,
        request_id=str(UUID(int=62)),
    )

    assert general["invocation"]["request_digest"] != bound["invocation"]["request_digest"]
    assert general["job_id"] != bound["job_id"]
    stored_general = store._objects.read(f"requests/{UUID(int=61)}/request.json").value
    stored_bound = store._objects.read(f"requests/{UUID(int=62)}/request.json").value
    assert "client_lens" not in stored_general
    assert stored_bound["client_lens"] == LENS_ENVELOPE
    # Only the lens and the identity differ; the wording and scope are the same.
    assert {
        key: item
        for key, item in stored_bound.items()
        if key not in ("client_lens", "request_id", "run_id", "request_digest")
    } == {
        key: item
        for key, item in stored_general.items()
        if key not in ("request_id", "run_id", "request_digest")
    }


def test_the_same_request_id_cannot_be_readmitted_under_a_different_lens():
    """A retry that changes the lens is a conflict, never a silent reuse."""
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    store, _bucket, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=63))
    admit_lens(store, invocation, runtime, client_lens=LENS_ENVELOPE, request_id=request_id)
    with pytest.raises(QuestionStoreError) as caught:
        admit_lens(store, invocation, runtime, client_lens=None, request_id=request_id)
    assert str(caught.value) in ("run_id_conflict", "request_invalid")


def test_the_status_reply_names_the_lens_the_request_was_admitted_under():
    """A caller holding only a job id can still record the right configuration.

    A job id carries no lens, so the app can only record the one the request was
    admitted under if the engine says it. General 42 keeps the reply it had.
    """
    store, _bucket, invocation, runtime, _ = deployment_fixture.fixture()
    general_id, bound_id = str(UUID(int=71)), str(UUID(int=72))
    admit_lens(store, invocation, runtime, client_lens=None, request_id=general_id)
    admit_lens(store, invocation, runtime, client_lens=LENS_ENVELOPE, request_id=bound_id)

    general = store.status(general_id, scope=BSA_SCOPE, now=store_fixture.NOW)
    bound = store.status(bound_id, scope=BSA_SCOPE, now=store_fixture.NOW)

    assert general["contract_version"] == "general_question_status_v1"
    assert "client_lens" not in general
    assert bound["client_lens"] == LENS_ENVELOPE
    assert set(bound) - set(general) == {"client_lens"}


def test_admission_hands_the_purpose_gate_the_resolved_lens_not_the_brand_name(monkeypatch):
    """The configuration the policy runs under is selected by the lens, not the name.

    A registry may authorize more than one configuration for one client scope and
    brand, and then only the lens says which one this request asked for. The gate
    is therefore handed the resolved lens rather than left to re-derive one.
    """
    seen = []
    admission = module()
    original = admission.refuse_prohibited_client_purpose

    def record(scope, texts, *, stage, root=None, lens=None):
        seen.append(lens)
        return original(scope, texts, stage=stage, root=root, lens=lens)

    monkeypatch.setattr(admission, "refuse_prohibited_client_purpose", record)
    store, _bucket, invocation, runtime, _ = deployment_fixture.fixture()
    admit_lens(store, invocation, runtime, client_lens=None, request_id=str(UUID(int=81)))
    admit_lens(store, invocation, runtime, client_lens=LENS_ENVELOPE, request_id=str(UUID(int=82)))

    assert seen[0] is None
    assert seen[1] is not None
    assert seen[1].client_lens_id == LENS_ENVELOPE["client_lens_id"]
    assert seen[1].configuration_digest == LENS_ENVELOPE["configuration_digest"]


# Staging served policy f1f53ebb (pricing verified at the 20 Sep 20:29 renewal) on
# 23 Sep 06:06, 57 hours 37 minutes later, and every new question failed in the
# reservation stage as request_expired although only a few seconds had elapsed.
STALE_PRICING_GAP = timedelta(hours=57, minutes=37)


def admit_with_stages(store, invocation, runtime, *, now, request_id, message):
    events = []
    try:
        result = module().admit_question_transport(
            value(invocation, request_id=request_id, transport={"message": message}),
            store=store,
            runtime_identity=runtime,
            now=now,
            diagnostics=events.append,
        )
    except store_fixture.module().QuestionStoreError as error:
        return error, events
    return result, events


def test_a_lapsed_pricing_review_refuses_a_new_question_as_approval_not_as_expiry():
    store, bucket, invocation, runtime, _ = deployment_fixture.fixture()
    before = copy.deepcopy(bucket.objects)

    outcome, events = admit_with_stages(
        store,
        invocation,
        runtime,
        now=store_fixture.NOW + STALE_PRICING_GAP,
        request_id=str(UUID(int=31)),
        message="Asked after the pricing review lapsed",
    )

    assert isinstance(outcome, store_fixture.module().QuestionStoreError)
    assert outcome.code == "approval_required"
    assert bucket.objects == before
    stages = [(event["stage"], event["state"], event["error_code"]) for event in events]
    assert ("policy_freshness", "failed", "approval_required") in stages
    assert not any(stage in {"intake_context", "reservation"} for stage, _, _ in stages)
    assert stages[-1] == ("admission", "failed", "approval_required")


def test_a_question_admitted_before_the_review_lapsed_is_still_returned_on_retry():
    store, _, invocation, runtime, _ = deployment_fixture.fixture()
    request_id = str(UUID(int=32))
    first, _ = admit_with_stages(
        store,
        invocation,
        runtime,
        now=store_fixture.NOW,
        request_id=request_id,
        message="Admitted while the review was current",
    )
    retry, _ = admit_with_stages(
        store,
        invocation,
        runtime,
        now=store_fixture.NOW + STALE_PRICING_GAP,
        request_id=request_id,
        message="Admitted while the review was current",
    )

    assert retry == first


def test_admission_logs_the_intake_context_stage_between_lookup_and_reservation():
    store, _, invocation, runtime, _ = deployment_fixture.fixture()

    _, events = admit_with_stages(
        store,
        invocation,
        runtime,
        now=store_fixture.NOW,
        request_id=str(UUID(int=33)),
        message="Which stages run before the reservation?",
    )

    started = [event["stage"] for event in events if event["state"] == "started"]
    assert started == [
        "admission",
        "authority",
        "child_lookup",
        "policy_freshness",
        "intake_context",
        "reservation",
    ]
    assert all(event["error_code"] is None for event in events)
