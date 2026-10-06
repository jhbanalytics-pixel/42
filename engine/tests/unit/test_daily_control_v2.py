import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import daily_cycle, daily_store
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit.test_daily_store import FakeObjectClient

CUTOFF = datetime(2026, 9, 14, tzinfo=UTC)
NOW = CUTOFF + timedelta(hours=1)
ESTATE = "intelligence-42-core"
OWNER = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging/executions/test-parent"
CHILD = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
PROFILE = {
    "schema_version": "42_daily_v2",
    "source_estate_id": ESTATE,
    "resource_manifest_digest": "1" * 64,
    "source_policy_digest": "2" * 64,
    "recurring_grant_digest": "3" * 64,
    "freshness_target_hours": 30,
    "max_publish_lag_hours": 48,
    "max_catchup_cutoffs": 2,
}


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def slot():
    return daily_store.slot_operation_id_v2(
        environment="staging", source_estate_id=ESTATE, cutoff_utc=CUTOFF
    )


def instance(client=None, *, owner=OWNER, clock=None):
    return daily_store.DailyStore(
        client or FakeObjectClient(), owner=owner, now=clock or (lambda: NOW), lease_seconds=60
    )


def claim(store):
    return store.claim_v2(slot(), "staging", ESTATE, CUTOFF)


def intent(*, input_digest="4" * 64):
    return {
        "intent_id": "dsi_"
        + digest(
            {
                "control_contract_version": "42_daily_control_v2",
                "slot_id": slot(),
                "stage": "collection",
                "attempt_version": 1,
            }
        ),
        "business_attempt_id": "bat_" + digest({"operation_id": slot(), "attempt": "collection@1"}),
        "stage": "collection",
        "attempt_version": 1,
        "input_digest": input_digest,
        "operation_context_sha256": "5" * 64,
        "authorizing_approval_id": "exa_" + "6" * 64,
        "authorizing_grant_digest": PROFILE["recurring_grant_digest"],
        "child_job_resource": CHILD,
        "lease_owner": OWNER,
        "lease_epoch": 1,
        "phase": "prepared",
        "derivation_id": None,
        "dispatch_observation_reference": None,
        "resolution_reference": None,
    }


def control(client):
    return json.loads(client.objects[f"42/daily/slots/{slot()}/control.json"][0])


def test_slot_normalizes_real_offset_and_refuses_other_namespace():
    offset = CUTOFF.astimezone(timezone(timedelta(hours=2)))
    assert offset.hour == 2
    assert (
        daily_store.slot_operation_id_v2(
            environment="staging", source_estate_id=ESTATE, cutoff_utc=offset
        )
        == slot()
    )
    for environment, estate in [("prod", ESTATE), ("staging", "another-estate")]:
        with pytest.raises(daily_store.StoreRefusal):
            daily_store.slot_operation_id_v2(
                environment=environment, source_estate_id=estate, cutoff_utc=CUTOFF
            )


@pytest.mark.parametrize(
    "key,value",
    [
        ("input_digest", "x"),
        ("operation_context_sha256", "x"),
        ("intent_id", "dsi_" + "0" * 64),
        ("business_attempt_id", "bat_" + "0" * 64),
        ("authorizing_approval_id", "not-an-approval"),
        ("authorizing_grant_digest", "x"),
        ("child_job_resource", "job"),
        ("lease_epoch", True),
        ("derivation_id", "exd_" + "1" * 64),
        ("phase", "derived"),
    ],
)
def test_bad_intent_refuses_before_any_write(key, value):
    client = FakeObjectClient()
    store = instance(client)
    assert claim(store)
    before = copy.deepcopy(client.objects)
    changed = intent()
    changed[key] = value
    with pytest.raises(daily_store.StoreRefusal):
        store.prepare_intent_v2(slot(), changed)
    assert client.objects == before


def test_expired_owner_and_wrong_slot_cannot_publish():
    client = FakeObjectClient()
    clock = [NOW]
    store = instance(client, clock=lambda: clock[0])
    with pytest.raises(daily_store.StoreRefusal):
        store.claim_v2("a" * 64, "staging", ESTATE, CUTOFF)
    assert not client.objects
    assert claim(store)
    clock[0] += timedelta(seconds=61)
    before = copy.deepcopy(client.objects)
    with pytest.raises(daily_store.StoreRefusal):
        store.prepare_intent_v2(slot(), intent())
    assert client.objects == before


def test_takeover_between_read_and_cas_fences_original_owner():
    class Racing(FakeObjectClient):
        callback = None

        def write(self, name, payload, *, if_generation_match):
            callback, self.callback = self.callback, None
            if callback:
                callback()
            return super().write(name, payload, if_generation_match=if_generation_match)

    client = Racing()
    first = instance(client)
    assert claim(first)
    second = instance(client, owner=OWNER + "-second", clock=lambda: NOW + timedelta(seconds=61))
    client.callback = lambda: claim(second)
    with pytest.raises(daily_store.StoreRefusal, match="stale_owner"):
        first.prepare_intent_v2(slot(), intent())
    assert control(client)["lease"]["owner"] == OWNER + "-second"
    assert control(client)["dispatch_intent"] is None


def test_unknown_intent_survives_release_and_blocks_expired_takeover():
    client = FakeObjectClient()
    first = instance(client)
    assert claim(first)
    first.prepare_intent_v2(slot(), intent())
    first.release_claim_v2(slot())
    second = instance(client, owner=OWNER + "-second", clock=lambda: NOW + timedelta(hours=2))
    assert not claim(second)
    assert control(client)["dispatch_intent"] == intent()
    assert list(client.objects) == [f"42/daily/slots/{slot()}/control.json"]


def test_live_renewal_keeps_epoch_but_expired_owner_cannot_resurrect():
    client = FakeObjectClient()
    clock = [NOW]
    first = instance(client, clock=lambda: clock[0])
    assert claim(first)
    clock[0] += timedelta(seconds=30)
    first.renew_claim_v2(slot())
    assert control(client)["epoch"] == 1
    later = instance(client, owner=OWNER + "-second", clock=lambda: NOW + timedelta(seconds=61))
    assert not claim(later)
    clock[0] = NOW + timedelta(seconds=91)
    before = copy.deepcopy(client.objects)
    with pytest.raises(daily_store.StoreRefusal, match="stale_owner"):
        first.renew_claim_v2(slot())
    assert client.objects == before


def test_v1_record_is_not_overwritten_or_relabelled():
    client = FakeObjectClient()
    old = instance(client)
    assert old.claim(slot(), CUTOFF)
    before = copy.deepcopy(client.objects)
    with pytest.raises(daily_store.StoreRefusal):
        claim(instance(client))
    assert client.objects == before


def test_v2_rejects_an_alternate_control_prefix():
    client = FakeObjectClient()
    store = daily_store.DailyStore(client, owner=OWNER, now=lambda: NOW, prefix="another-prefix")
    with pytest.raises(daily_store.StoreRefusal):
        claim(store)
    assert not client.objects


def test_profile_dispatch_accepts_exact_v2_and_preserves_v1_shape():
    assert daily_cycle.validate_daily_profile(PROFILE) == PROFILE
    changed = dict(PROFILE, max_catchup_cutoffs=3)
    with pytest.raises(ValueError):
        daily_cycle.validate_daily_profile(changed)
    old = dict(PROFILE, schema_version="42_daily_v1")
    old.pop("source_estate_id")
    assert daily_cycle.validate_daily_profile(old) == old


def test_new_profile_does_not_widen_the_historical_grant_validator():
    from src.analysis.open_intelligence.recurring_grant import (
        GrantRefusal,
        validate_recurring_grant,
    )

    from tests.unit.test_recurring_grant import grant

    value = grant(schema_version="42_daily_v2")
    with pytest.raises(GrantRefusal):
        validate_recurring_grant(value)


def test_unit_identity_ignores_grant_quote_and_consumption_but_binds_request():
    request = {
        "route": "twitter/profile",
        "market": "za",
        "subject": "corporate-test",
        "window_start": (CUTOFF - timedelta(days=1)).isoformat(),
        "window_end": CUTOFF.isoformat(),
        "request_sha256": "7" * 64,
    }
    unit = daily_store.unit_request_id_v2(request)
    assert unit == daily_store.unit_request_id_v2(
        dict(request, quoted_credits="2", consumption_id="new", grant_id="new")
    )
    assert unit != daily_store.unit_request_id_v2(dict(request, request_sha256="8" * 64))
    with pytest.raises(daily_store.StoreRefusal):
        daily_store.unit_request_id_v2(dict(request, window_end=request["window_start"]))


def test_cycle_publishes_intent_before_issue_and_dispatch_then_holds_unknown():
    client = FakeObjectClient()
    store = instance(client)
    events = []

    class ProtocolAuthority:
        read_clients = None

        def prepare(self, stage, frame, *, lease):
            pending = intent(input_digest=digest(frame))
            context = {"slot_id": slot(), "stage": stage, "input_digest": digest(frame)}
            pending["operation_context_sha256"] = digest(context)
            return {"intent": pending, "operation_context": context}

        def issue(self, stage, prepared):
            assert control(client)["dispatch_intent"] == prepared["intent"]
            events.append("issue")
            return {
                "derivation_id": "exd_" + "9" * 64,
                "operation_context_sha256": prepared["intent"]["operation_context_sha256"],
            }

    def handler(**kwargs):
        assert control(client)["dispatch_intent"]["phase"] == "dispatch_started"
        events.append("dispatch")
        return {"state": "unknown"}

    stages = dict.fromkeys(
        ("collection", "exposure", "capture", "compose", "certify", "release"), handler
    )
    result = daily_cycle.execute_daily_cycle(
        operation_id=slot(),
        cutoff_utc=CUTOFF,
        profile=PROFILE,
        authority=ProtocolAuthority(),
        store=store,
        stages=stages,
    )
    assert result["state"] == "unknown"
    assert events == ["issue", "dispatch"]
    assert control(client)["dispatch_intent"]["phase"] == "dispatch_started"
    assert control(client)["lease"] is None


def unit_permit():
    value = {
        "contract_version": "daily_collection_unit_permit_v1",
        "operation_id": slot(),
        "consumption_id": "exc_" + "8" * 64,
        "route": "twitter/profile",
        "market": "za",
        "subject": "corporate-test",
        "window_start": (CUTOFF - timedelta(days=1)).isoformat(),
        "window_end": CUTOFF.isoformat(),
        "request_sha256": "7" * 64,
        "quoted_credits": "1",
        "max_calls": 1,
        "permit_sequence": 1,
        "created_at": NOW.isoformat(),
    }
    value["unit_id"] = daily_store.unit_request_id_v2(value)
    return value


def unit_state():
    client = FakeObjectClient()
    store = instance(client)
    assert claim(store)
    store.prepare_intent_v2(slot(), intent())
    store.bind_derivation_v2(
        slot(), {"derivation_id": "exd_" + "9" * 64, "operation_context_sha256": "5" * 64}
    )
    store.mark_dispatch_started_v2(slot())
    context = {
        "slot_id": slot(),
        "stage": "collection",
        "lease_owner": OWNER,
        "lease_epoch": 1,
        "authorizing_grant_digest": PROFILE["recurring_grant_digest"],
        "child_job_resource": CHILD,
    }
    return control(client), context


def test_pure_unit_transition_reserves_before_effect_and_never_reissues_unknown():
    current, context = unit_state()
    before = copy.deepcopy(current)
    updated, record = daily_store._unit_permit_state_v2(
        current,
        unit_permit(),
        context=context,
        derivation_id="exd_" + "9" * 64,
        consumption_id="exc_" + "8" * 64,
        max_credits=620,
    )
    assert current == before
    assert updated["units"][record["unit_id"]]["state"] == "attempt_started"
    with pytest.raises(daily_store.StoreRefusal, match="unit_already_started"):
        daily_store._unit_permit_state_v2(
            updated,
            unit_permit(),
            context=context,
            derivation_id="exd_" + "9" * 64,
            consumption_id="exc_" + "8" * 64,
            max_credits=620,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"max_calls": 2},
        {"quoted_credits": "621"},
        {"unit_id": "0" * 64},
        {"consumption_id": "exc_" + "0" * 64},
    ],
)
def test_pure_unit_transition_rejects_caps_or_unbound_identity(change):
    current, context = unit_state()
    before = copy.deepcopy(current)
    with pytest.raises(daily_store.StoreRefusal):
        daily_store._unit_permit_state_v2(
            current,
            dict(unit_permit(), **change),
            context=context,
            derivation_id="exd_" + "9" * 64,
            consumption_id="exc_" + "8" * 64,
            max_credits=620,
        )
    assert current == before


def test_pure_unit_transition_rejects_changed_parent_intent():
    current, context = unit_state()
    context["lease_epoch"] = 2
    with pytest.raises(daily_store.StoreRefusal, match="unit_intent_mismatch"):
        daily_store._unit_permit_state_v2(
            current,
            unit_permit(),
            context=context,
            derivation_id="exd_" + "9" * 64,
            consumption_id="exc_" + "8" * 64,
            max_credits=620,
        )


def test_unit_result_preserves_overage_and_holds_it():
    permit = unit_permit()
    result = {
        "contract_version": "daily_collection_unit_result_v1",
        "operation_id": slot(),
        "consumption_id": permit["consumption_id"],
        "unit_id": permit["unit_id"],
        "permit_sha256": digest(permit),
        "state": "complete",
        "calls": 1,
        "charged_credits": "2",
        "response_sha256": "a" * 64,
        "completed_at": NOW.isoformat(),
        "reason": None,
    }
    checked = daily_store._unit_result_v2(result, permit)
    assert checked["charged_credits"] == "2"
    assert checked["state"] == "unknown"
    assert checked["reason"] == "unit_quote_exceeded"


def protocol_chain(*, unknown=False):
    context = {
        "slot_id": slot(),
        "stage": "collection",
        "input_digest": "4" * 64,
        "lease_owner": OWNER,
        "lease_epoch": 1,
        "authorizing_grant_digest": PROFILE["recurring_grant_digest"],
    }
    envelope = {
        "operation_context_sha256": "5" * 64,
        "terminal_state": "succeeded",
        "effect_state": "effects_recorded",
        "spend_state": "unknown" if unknown else "measured",
        "stage_metering": {"complete": not unknown},
    }
    raw = canonical_bytes(envelope).decode("utf-8")
    return SimpleNamespace(
        operation_context=context,
        derivation={"operation_context_sha256": "5" * 64},
        result={
            "canonical_result_json": raw,
            "result_digest": digest(envelope),
            "result_id": "exr_" + digest(envelope),
        },
    )


def dispatched_store():
    client = FakeObjectClient()
    store = instance(client)
    assert claim(store)
    store.prepare_intent_v2(slot(), intent())
    store.bind_derivation_v2(
        slot(), {"derivation_id": "exd_" + "9" * 64, "operation_context_sha256": "5" * 64}
    )
    store.mark_dispatch_started_v2(slot())
    return store, client


def test_terminal_control_publication_is_one_cas_protocol_only(monkeypatch):
    store, client = dispatched_store()
    monkeypatch.setattr(store, "_read_chain_v2", lambda *args: protocol_chain())
    previous = len(client.writes)
    result = store.publish_terminal_v2(slot(), "collection", "exd_" + "9" * 64, clients=object())
    writes = client.writes[previous:]
    assert len([name for name, _ in writes if name.endswith("control.json")]) == 1
    assert result["state"] == "succeeded"
    assert control(client)["dispatch_intent"] is None
    assert store.read_stage_v2(slot(), "collection", clients=object()) == result


def test_unknown_spend_keeps_intent_even_with_terminal_protocol_record(monkeypatch):
    store, client = dispatched_store()
    monkeypatch.setattr(store, "_read_chain_v2", lambda *args: protocol_chain(unknown=True))
    before = copy.deepcopy(client.objects)
    result = store.publish_terminal_v2(slot(), "collection", "exd_" + "9" * 64, clients=object())
    assert result["state"] == "unknown"
    assert client.objects == before


def test_terminal_publication_rejects_context_substitution_protocol_only(monkeypatch):
    store, client = dispatched_store()
    chain = protocol_chain()
    chain.operation_context["lease_epoch"] = 2
    monkeypatch.setattr(store, "_read_chain_v2", lambda *args: chain)
    before = copy.deepcopy(client.objects)
    with pytest.raises(daily_store.StoreRefusal, match="context_mismatch"):
        store.publish_terminal_v2(slot(), "collection", "exd_" + "9" * 64, clients=object())
    assert client.objects == before


@pytest.mark.parametrize("orphan_retry", [False, True])
def test_child_unit_cas_preserves_parent_lease_protocol_only(monkeypatch, orphan_retry):
    parent, client = dispatched_store()
    child_name = CHILD + "/executions/test-child"
    child = instance(client, owner=child_name)
    context = dict(protocol_chain().operation_context, child_job_resource=CHILD)
    consumed = SimpleNamespace(
        consumption_id="exc_" + "8" * 64,
        derivation_id="exd_" + "9" * 64,
        operation_context=context,
        execution_name=child_name,
        manifest={"limits": {"max_credits": 620}},
    )

    def protocol_boundary(*args):
        current, generation = child._read_control_v2(slot())
        daily_store._unit_intent_v2(current, context, consumed.derivation_id)
        return consumed, current, generation

    monkeypatch.setattr(child, "_consumed_unit_control_v2", protocol_boundary)
    old_lease = copy.deepcopy(control(client)["lease"])
    supplied = dict(unit_permit(), created_at="2020-01-01T00:00:00+00:00")
    first_created = NOW - timedelta(seconds=10)
    if orphan_retry:
        orphan = dict(supplied, created_at=first_created.isoformat())
        name = f"42/daily/slots/{slot()}/units/{supplied['unit_id']}/permit.json"
        client.objects[name] = (canonical_bytes(orphan), 1)
    permit = child.publish_unit_permit_v2(slot(), supplied, consumed_authority=object())
    assert permit["created_at"] == (first_created if orphan_retry else NOW).isoformat()
    if orphan_retry:
        assert client.objects[name] == (canonical_bytes(orphan), 1)
    assert control(client)["lease"] == old_lease
    assert (
        child.read_unit_v2(slot(), permit["unit_id"], consumed_authority=object())["state"]
        == "attempt_started"
    )
    with pytest.raises(daily_store.StoreRefusal, match="unit_already_started"):
        child.publish_unit_permit_v2(slot(), unit_permit(), consumed_authority=object())
    parent.release_claim_v2(slot())
    assert control(client)["units"][permit["unit_id"]]["state"] == "attempt_started"


@pytest.mark.parametrize(
    "field,value",
    [
        ("stages", {"forged": {"anything": True}}),
        ("stages", {"collection": {"anything": True}}),
        ("units", {"0" * 64: {"anything": True}}),
        ("timeline", {"forged": None}),
        ("timeline", {"collection_started_at": "2026-09-14T00:00:00"}),
    ],
)
def test_nested_control_grammar_refuses_before_claim(field, value):
    client = FakeObjectClient()
    store = instance(client)
    assert claim(store)
    name = f"42/daily/slots/{slot()}/control.json"
    changed = control(client)
    changed[field] = value
    client.objects[name] = (canonical_bytes(changed), client.objects[name][1])
    before = copy.deepcopy(client.objects)
    with pytest.raises(daily_store.StoreRefusal):
        store._read_control_v2(slot())
    assert client.objects == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("reference", "another/path"),
        ("record_digest", "bad"),
        ("attempt", True),
        ("epoch", 2),
        ("state", []),
        ("protected_result_id", "exr_" + "0" * 64),
        ("protected_result_digest", "bad"),
    ],
)
def test_stage_pointer_mutations_refuse_on_control_read(monkeypatch, key, value):
    store, client = dispatched_store()
    monkeypatch.setattr(store, "_read_chain_v2", lambda *args: protocol_chain())
    store.publish_terminal_v2(slot(), "collection", "exd_" + "9" * 64, clients=object())
    changed = control(client)
    changed["stages"]["collection"][key] = value
    name = f"42/daily/slots/{slot()}/control.json"
    client.objects[name] = (canonical_bytes(changed), client.objects[name][1])
    with pytest.raises(daily_store.StoreRefusal):
        store._read_control_v2(slot())


@pytest.mark.parametrize(
    "key,value",
    [
        ("permit_reference", "another/path"),
        ("permit_digest", "bad"),
        ("consumption_id", "bad"),
        ("state", []),
        ("result_reference", "bad"),
        ("result_digest", "0" * 64),
        ("quoted_credits", "-1"),
        ("permit_sequence", True),
    ],
)
def test_unit_pointer_mutations_refuse_on_control_read(key, value):
    store, client = dispatched_store()
    changed = control(client)
    permit = unit_permit()
    changed, _ = daily_store._unit_permit_state_v2(
        changed,
        permit,
        context=dict(protocol_chain().operation_context, child_job_resource=CHILD),
        derivation_id="exd_" + "9" * 64,
        consumption_id="exc_" + "8" * 64,
        max_credits=620,
    )
    changed["units"][permit["unit_id"]][key] = value
    name = f"42/daily/slots/{slot()}/control.json"
    client.objects[name] = (canonical_bytes(changed), client.objects[name][1])
    with pytest.raises(daily_store.StoreRefusal):
        store._read_control_v2(slot())
