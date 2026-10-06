import copy
import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from tests.unit import test_general_question_store as fixture


def observer():
    module = importlib.import_module("src.analysis.open_intelligence.general_question_observation")
    configured = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_ROOT")
    root = (
        Path(configured).resolve(strict=True)
        if configured is not None
        else Path(__file__).resolve().parents[2]
    )
    assert root.is_dir()
    assert Path(module.__file__).resolve(strict=True).is_relative_to(root / "src")
    return module


def test_observer_refuses_wrong_configured_engine_root(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERAL_QUESTION_ENGINE_TEST_ROOT", str(tmp_path))
    with pytest.raises(AssertionError):
        observer()


def setup(monkeypatch):
    store, bucket = fixture.store()
    fixture.activate_fixture(bucket)
    monkeypatch.setattr(fixture.Blob, "updated", property(lambda self: fixture.NOW), raising=False)
    return store, bucket


def observe(store, request_id=None):
    return observer().observe_question_requests(
        {
            "contract_version": "general_question_observe_request_v1",
            "scope": fixture.scope(),
            "request_id": request_id,
        },
        store=store,
    )


def forbid_writes(monkeypatch, store):
    def forbidden(*args, **kwargs):
        pytest.fail("Observer attempted a mutation or mutating status")

    monkeypatch.setattr(store._objects, "create", forbidden)
    monkeypatch.setattr(store, "status", forbidden)
    monkeypatch.setattr(store, "publish_result", forbidden)
    monkeypatch.setattr(store, "admit", forbidden)
    monkeypatch.setattr(store._objects.bucket, "blob", forbidden)


def test_complete_empty_inventory_requires_observed_ledger_authority(monkeypatch):
    store, bucket = setup(monkeypatch)
    before = copy.deepcopy(bucket.objects)
    forbid_writes(monkeypatch, store)
    result = observe(store)
    assert result["coverage"]["state"] == "complete"
    assert result["coverage"]["total_count"] == 0
    assert result["rows"] == []
    assert bucket.objects == before


@pytest.mark.parametrize("state", ["unavailable", "held"])
def test_selected_terminal_ack_is_exact_and_does_not_release_allowance(monkeypatch, state):
    from tests.unit import test_general_question_result as results

    store, _, bucket, identifier = results.setup()
    pointer = results.publish(store, identifier, state=state)
    before = copy.deepcopy(bucket.objects)
    forbid_writes(monkeypatch, store)
    result = observe(store, identifier)
    assert result["observed_state"] == state
    assert result["result_pointer"] == pointer
    assert result["reserved_microusd"] == 100000
    assert bucket.objects == before


@pytest.mark.parametrize("kind", ["request", "intake", "generation"])
def test_invalid_required_authority_refuses_detail_and_makes_inventory_partial(monkeypatch, kind):
    store, bucket = setup(monkeypatch)
    _, request, intake = fixture.prepared()
    admission = store.admit(request, intake, scope=fixture.scope(), now=fixture.NOW)
    name = "request" if kind == "generation" else kind
    key = fixture.PREFIX + f"requests/{request['request_id']}/{name}.json"
    generation = int(admission[name + "_generation"])
    if kind == "generation":
        bucket.versions.pop((key, generation))
    else:
        bucket.versions[(key, generation)] = b"{}"
    forbid_writes(monkeypatch, store)
    with pytest.raises(fixture.module().QuestionStoreError, match="observation_invalid"):
        observe(store, request["request_id"])
    inventory = observe(store)
    assert inventory["coverage"]["state"] == "partial"
    assert inventory["coverage"]["total_count"] is None
    assert inventory["coverage"]["invalid_count"] == (1 if kind == "intake" else None)


def test_absent_and_cross_scope_detail_share_one_safe_error(monkeypatch):
    store, _ = setup(monkeypatch)
    _, request, intake = fixture.prepared()
    store.admit(request, intake, scope=fixture.scope(), now=fixture.NOW)
    forbid_writes(monkeypatch, store)
    for identifier, scope in [
        ("00000000-0000-0000-0000-000000000099", fixture.scope()),
        (request["request_id"], {**fixture.scope(), "brand_config_id": "another_brand"}),
    ]:
        with pytest.raises(
            fixture.module().QuestionStoreError, match=r"^observation_request_unavailable$"
        ):
            observer().observe_question_requests(
                {
                    "contract_version": "general_question_observe_request_v1",
                    "scope": scope,
                    "request_id": identifier,
                },
                store=store,
            )


def test_invalid_selected_terminal_never_inherits_terminal_state(monkeypatch):
    from tests.unit import test_general_question_result as results

    store, _, bucket, identifier = results.setup()
    pointer = results.publish(store, identifier)
    key = fixture.PREFIX + f"requests/{identifier}/results/{pointer['result_digest']}.json"
    bucket.versions[(key, int(pointer["result_generation"]))] = b"{}"
    forbid_writes(monkeypatch, store)
    with pytest.raises(fixture.module().QuestionStoreError, match="observation_invalid"):
        observe(store, identifier)
    monkeypatch.setattr(fixture.Blob, "updated", property(lambda self: fixture.NOW), raising=False)
    inventory = observe(store)
    assert inventory["coverage"]["state"] == "partial"
    assert inventory["coverage"]["total_count"] is None
    assert inventory["rows"][0]["status"] == "unconfirmed"
    assert "The selected terminal record could not be validated." in inventory["rows"][0]["gaps"]


def test_read_ceiling_returns_partial_known_count_without_writes(monkeypatch):
    store, bucket = setup(monkeypatch)
    for number in range(1, 61):
        _, request, intake = fixture.prepared(number)
        store.admit(request, intake, scope=fixture.scope(), now=fixture.NOW)
    before = copy.deepcopy(bucket.objects)
    forbid_writes(monkeypatch, store)
    inventory = observe(store)
    coverage = inventory["coverage"]
    assert coverage["state"] == "partial"
    assert coverage["total_count"] is None
    assert 0 < coverage["known_count"] < 60
    assert sum(coverage["known_by_status"].values()) == coverage["known_count"]
    assert "read_limit_reached" in coverage["reasons"]
    assert bucket.objects == before


def test_missing_metadata_and_expired_read_are_not_complete_zero(monkeypatch):
    store, _ = setup(monkeypatch)
    monkeypatch.setattr(fixture.Blob, "updated", property(lambda self: None))
    result = observe(store)
    assert result["coverage"]["total_count"] is None
    assert result["coverage"]["observed_at"] is None
    monkeypatch.setattr(observer(), "_READ_SECONDS", 0)
    result = observe(store)
    assert result["coverage"]["state"] == "unavailable"
    assert result["ledger_generation"] is None
    assert result["coverage"]["reasons"] == ["read_deadline_reached"]


def test_inventory_counts_precede_display_and_byte_cuts():
    rows = [
        {
            "operation_id": f"gq_{number:032d}",
            "status": "partial",
            "updated_at": None,
            "title": "x" * 2000,
        }
        for number in range(73)
    ]
    result = observer()._inventory_reply(
        rows,
        generation="1",
        observed_at=None,
        boundary="2026-09-10T00:00:00Z",
        reasons=[],
        invalid_count=0,
    )
    assert result["coverage"]["known_count"] == result["coverage"]["total_count"] == 73
    assert result["coverage"]["known_by_status"]["partial"] == 73
    assert result["coverage"]["known_by_status"]["complete"] == 0
    assert result["coverage"]["returned_count"] == len(result["rows"]) < 50
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 64 * 1024


def test_inventory_orders_real_instants_preserving_timestamp_precision():
    rows = [
        {"operation_id": "a", "status": "unconfirmed", "updated_at": "2026-09-09T12:00:00Z"},
        {"operation_id": "b", "status": "unconfirmed", "updated_at": "2026-09-09T12:00:00.5Z"},
        {"operation_id": "c", "status": "unconfirmed", "updated_at": "2026-09-09T12:00:00.500000Z"},
        {"operation_id": "d", "status": "unconfirmed", "updated_at": None},
    ]
    result = observer()._inventory_reply(
        rows,
        generation="1",
        observed_at=None,
        boundary="2026-09-10T00:00:00Z",
        reasons=[],
        invalid_count=0,
    )
    assert [row["operation_id"] for row in result["rows"]] == ["b", "c", "a", "d"]
    assert result["rows"][0]["updated_at"] == "2026-09-09T12:00:00.5Z"
    assert result["rows"][1]["updated_at"] == "2026-09-09T12:00:00.500000Z"
    assert result["coverage"]["window"]["start"] == "2026-09-09T12:00:00Z"


def test_narrow_market_request_and_terminal_detail_use_original_scope(monkeypatch):
    from src.analysis.open_intelligence.general_question_policy import build_intake_context
    from src.analysis.open_intelligence.general_question_request import normalize_question_request
    from src.analysis.open_intelligence.general_question_result import (
        observed_result_usage,
        unavailable_response,
    )

    store, bucket = setup(monkeypatch)
    own_scope = {**fixture.scope(), "market_scope": ["za"]}
    request = normalize_question_request(
        {"message": "A South African question"},
        scope=own_scope,
        request_id="00000000-0000-0000-0000-000000000081",
        admitted_at=fixture.NOW,
        policy_digest=store.policy["policy_digest"],
    )
    intake = build_intake_context(request, selected_market="za")
    store.admit(request, intake, scope=own_scope, now=fixture.NOW)
    response = unavailable_response(
        request,
        observed_result_usage(store, request_id=request["request_id"], scope=own_scope),
        reason="request_expired",
    )
    pointer = store.publish_result(
        request["request_id"],
        scope=own_scope,
        response=response,
        state="unavailable",
        now=fixture.NOW,
    )
    before = copy.deepcopy(bucket.objects)
    forbid_writes(monkeypatch, store)
    result = observe(store, request["request_id"])
    assert result["result_pointer"] == pointer
    inventory = observe(store)
    assert inventory["coverage"]["total_count"] == 1
    assert inventory["rows"][0]["market_scope"] == ["za"]
    for scope in [
        {**fixture.scope(), "brand_config_id": "other"},
        {**fixture.scope(), "audience_lens_ids": ["other"]},
        {**fixture.scope(), "market_scope": ["ke", "ng"]},
    ]:
        with pytest.raises(
            fixture.module().QuestionStoreError, match="observation_request_unavailable"
        ):
            observer().observe_question_requests(
                {
                    "contract_version": "general_question_observe_request_v1",
                    "scope": scope,
                    "request_id": request["request_id"],
                },
                store=store,
            )
    assert bucket.objects == before


@pytest.mark.parametrize("source_window", [False, True])
def test_expired_admission_remains_unconfirmed_and_allowance_unchanged(monkeypatch, source_window):
    store, bucket = setup(monkeypatch)
    _, request, intake = fixture.prepared()
    if source_window:
        from src.analysis.open_intelligence.general_question_policy import build_intake_context

        intake = build_intake_context(request, selected_market="za", source_window=True)
    store.admit(request, intake, scope=fixture.scope(), now=fixture.NOW)
    before = copy.deepcopy(bucket.objects)
    forbid_writes(monkeypatch, store)
    result = observe(store, request["request_id"])
    assert result["observed_state"] == "unconfirmed"
    assert result["result_pointer"] is None
    assert result["reserved_microusd"] == 100000
    assert result["request_generation"]
    assert result["intake_generation"]
    assert bucket.objects == before


def test_ledger_metadata_generation_must_match_selected_immutable_ledger(monkeypatch):
    store, bucket = setup(monkeypatch)
    read = bucket.get_blob

    def changed(name, **kwargs):
        blob = read(name, **kwargs)
        if name == fixture.PREFIX + fixture.LEDGER and kwargs.get("generation") is not None:
            blob.generation += 1
        return blob

    monkeypatch.setattr(bucket, "get_blob", changed)
    result = observe(store)
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["observed_at"] is None
    assert result["coverage"]["total_count"] is None
    assert result["rows"] == []


def test_terminal_record_with_unresolvable_planning_binding_is_not_exposed(monkeypatch):
    from src.analysis.open_intelligence import general_question_observation as subject
    from src.analysis.open_intelligence.general_question_planning import (
        build_question_planning_request,
    )

    from tests.unit import test_general_question_result as results

    store, _, _, identifier = results.setup()
    context = copy.deepcopy(store.read_request(identifier, scope=fixture.scope()))
    policy = store.policy
    sdk = build_question_planning_request(
        context["request"], context["intake"], policy=policy, remaining_seconds=60
    )
    context["admission"]["execution"]["calls"] = {
        "planning": {
            "stage": "planning",
            "model": sdk.model,
            "policy_digest": policy["policy_digest"],
            "system_instruction_digest": sdk.system_instruction_digest,
            "response_schema_digest": sdk.response_schema_digest,
            "response": {"generation": "1", "digest": "d" * 64},
        }
    }
    context["admission"]["result"] = {"generation": "1", "digest": "e" * 64}
    record = {
        "state": "complete",
        "recorded_at": "2026-09-06T20:00:01Z",
        "response": {"intelligence": {"resolved_scope": {"market_scope": ["za"]}}},
    }
    monkeypatch.setattr(subject, "question_answer_validators", lambda store, scope: (None, None))
    monkeypatch.setattr(
        subject, "_selected", lambda store, context, validator: copy.deepcopy(record)
    )
    forbid_writes(monkeypatch, store)
    row, selected, missing, reasons = subject._observation(store, context, fixture.scope())
    assert (selected, row["status"], missing, reasons) == (record, "complete", [], [])
    tampered = copy.deepcopy(context)
    tampered["admission"]["execution"]["calls"]["planning"]["system_instruction_digest"] = (
        hashlib.sha256(b"prompt.txt").hexdigest()
    )
    row, selected, missing, reasons = subject._observation(store, tampered, fixture.scope())
    assert selected is None
    assert row["status"] == "unconfirmed"
    assert missing == ["terminal_record_unavailable", "execution_unconfirmed", "record_invalid"]
    assert reasons == ["record_invalid"]
    assert "The selected terminal record could not be validated." in row["gaps"]
    assert row["updated_at"] == context["admission"]["admitted_at"]
