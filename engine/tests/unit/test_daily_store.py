"""DailyStore over the evidence bucket: one fenced control record, create only stage records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import Mock

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cycle import (
    execute_daily_cycle,
    resume_incomplete_units,
)
from src.analysis.open_intelligence.daily_store import (
    CONTROL_CONTRACT,
    EVIDENCE_BUCKET,
    STAGE_RECORD_CONTRACT,
    DailyStore,
    PreconditionFailed,
    StoreRefusal,
    slot_operation_id,
)

CUTOFF = datetime(2026, 9, 20, tzinfo=UTC)
NOW = datetime(2026, 9, 20, 4, 30, tzinfo=UTC)
OPERATION = "a" * 64


class FakeObjectClient:
    """Generation counting object store with the two preconditions the store relies on."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.writes: list[tuple[str, int]] = []
        self.failing_reads: set[str] = set()

    def read(self, name):
        if name in self.failing_reads:
            raise OSError(f"read failed: {name}")
        return self.objects.get(name)

    def write(self, name, payload, *, if_generation_match):
        current = self.objects.get(name)
        generation = current[1] if current else 0
        if generation != if_generation_match:
            raise PreconditionFailed(name)
        self.objects[name] = (bytes(payload), generation + 1)
        self.writes.append((name, if_generation_match))
        return generation + 1

    def control(self, operation_id=OPERATION):
        raw, _generation = self.objects[f"42/daily/slots/{operation_id}/control.json"]
        return json.loads(raw)


def store(client, *, owner="run-1", now=NOW, lease_seconds=3600):
    clock = now if callable(now) else (lambda: now)
    return DailyStore(client, owner=owner, now=clock, lease_seconds=lease_seconds)


def result(state, digest, reference="ref", output="", retry_safe=False):
    return {
        "state": state,
        "input_digest": digest,
        "output_digest": output,
        "result_reference": reference,
        "retry_safe": retry_safe,
    }


def test_bucket_and_slot_identity_are_deterministic():
    assert EVIDENCE_BUCKET == "ogilvy-trends-v2-execution-approvals-staging"
    first = slot_operation_id(
        environment="staging", source_policy_digest="c" * 64, cutoff_utc=CUTOFF
    )
    again = slot_operation_id(
        environment="staging", source_policy_digest="c" * 64, cutoff_utc=CUTOFF
    )
    assert first == again
    assert len(first) == 64
    assert first != slot_operation_id(
        environment="staging",
        source_policy_digest="c" * 64,
        cutoff_utc=CUTOFF + timedelta(days=1),
    )
    with pytest.raises(StoreRefusal, match=r"^slot_cutoff_not_timezone_aware$"):
        slot_operation_id(
            environment="staging", source_policy_digest="c" * 64, cutoff_utc=datetime(2026, 9, 20)
        )


def test_claim_creates_the_control_record_with_lease_and_epoch():
    client = FakeObjectClient()
    assert store(client).claim(OPERATION, CUTOFF) is True
    control = client.control()
    assert control["contract_version"] == CONTROL_CONTRACT
    assert control["operation_id"] == OPERATION
    assert control["cutoff_utc"] == CUTOFF.isoformat()
    assert control["lease"] == {
        "owner": "run-1",
        "epoch": 1,
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }
    assert control["epoch"] == 1
    assert control["dispatch_intent"] is None
    assert control["stages"] == {}
    assert client.writes == [(f"42/daily/slots/{OPERATION}/control.json", 0)]


def test_simultaneous_trigger_is_refused_while_the_lease_is_live():
    client = FakeObjectClient()
    assert store(client, owner="run-1").claim(OPERATION, CUTOFF) is True
    assert (
        store(client, owner="run-2", now=NOW + timedelta(minutes=5)).claim(OPERATION, CUTOFF)
        is False
    )
    assert client.control()["lease"]["owner"] == "run-1"


def test_lost_compare_and_swap_reads_as_not_claimed():
    class RacingClient(FakeObjectClient):
        def write(self, name, payload, *, if_generation_match):
            raise PreconditionFailed(name)

    assert store(RacingClient()).claim(OPERATION, CUTOFF) is False


def test_cutoff_conflict_for_the_same_slot_is_refused():
    client = FakeObjectClient()
    store(client).claim(OPERATION, CUTOFF)
    with pytest.raises(StoreRefusal, match=r"^slot_cutoff_conflict$"):
        store(client, owner="run-2", now=NOW + timedelta(hours=2)).claim(
            OPERATION, CUTOFF + timedelta(days=1)
        )


def test_expired_lease_takeover_fences_the_stale_owner():
    client = FakeObjectClient()
    stale = store(client, owner="run-1")
    assert stale.claim(OPERATION, CUTOFF) is True
    later = store(client, owner="run-2", now=NOW + timedelta(hours=2))
    assert later.claim(OPERATION, CUTOFF) is True
    assert client.control()["lease"] == {
        "owner": "run-2",
        "epoch": 2,
        "expires_at": (NOW + timedelta(hours=3)).isoformat(),
    }
    with pytest.raises(StoreRefusal, match=r"^stale_owner$"):
        stale.record_stage(OPERATION, "collect", result("pending", "1" * 64, "bat_1"))
    assert client.control()["stages"] == {}
    stale.release_claim(OPERATION)
    assert client.control()["lease"]["owner"] == "run-2"
    assert stale.current_lease(OPERATION) is None
    assert later.current_lease(OPERATION) == {"owner": "run-2", "epoch": 2}
    later.release_claim(OPERATION)
    assert client.control()["lease"] is None
    assert store(client, owner="run-3", now=NOW + timedelta(hours=2)).claim(OPERATION, CUTOFF)


def test_stale_owner_orphan_record_cannot_wedge_the_slot():
    class InterleavingClient(FakeObjectClient):
        def __init__(self):
            super().__init__()
            self.before_stage_write = None

        def write(self, name, payload, *, if_generation_match):
            hook, self.before_stage_write = self.before_stage_write, None
            if hook is not None and "/stages/" in name:
                hook()
            return super().write(name, payload, if_generation_match=if_generation_match)

    client = InterleavingClient()
    clock = {"now": NOW}
    stale = store(client, owner="run-1", now=lambda: clock["now"], lease_seconds=60)
    assert stale.claim(OPERATION, CUTOFF) is True
    later = store(client, owner="run-2", now=lambda: clock["now"], lease_seconds=60)

    def takeover():
        clock["now"] = NOW + timedelta(minutes=2)
        assert later.claim(OPERATION, CUTOFF) is True

    client.before_stage_write = takeover
    with pytest.raises(StoreRefusal, match=r"^stale_owner$"):
        stale.record_stage(OPERATION, "collect", result("pending", "1" * 64, "bat_stale"))
    orphan = f"42/daily/slots/{OPERATION}/stages/collect/1-1.json"
    assert orphan in client.objects
    assert client.control()["stages"] == {}
    later.record_stage(OPERATION, "collect", result("pending", "1" * 64, "bat_live"))
    published = f"42/daily/slots/{OPERATION}/stages/collect/2-1.json"
    assert client.control()["stages"]["collect"]["reference"] == published
    assert client.control()["dispatch_intent"]["business_attempt_id"] == "bat_live"
    assert later.read_stage(OPERATION, "collect") == result("pending", "1" * 64, "bat_live")
    assert orphan in client.objects


def test_timeline_persists_in_the_control_record_on_release():
    client = FakeObjectClient()
    current = store(client)
    with pytest.raises(StoreRefusal, match=r"^lease_not_held$"):
        current.note_timeline(OPERATION, {"observation_window_end": CUTOFF.isoformat()})
    current.claim(OPERATION, CUTOFF)
    with pytest.raises(StoreRefusal, match=r"^timeline_invalid$"):
        current.note_timeline(OPERATION, {"unknown_instant": "x"})
    current.note_timeline(
        OPERATION,
        {
            "observation_window_end": CUTOFF.isoformat(),
            "collection_started_at": NOW.isoformat(),
            "collection_completed_at": (NOW + timedelta(minutes=1)).isoformat(),
            "snapshot_as_of": None,
            "capture_available_at": None,
        },
    )
    assert client.control()["timeline"] == {}
    current.release_claim(OPERATION)
    assert client.control()["timeline"] == {
        "observation_window_end": CUTOFF.isoformat(),
        "collection_started_at": NOW.isoformat(),
        "collection_completed_at": (NOW + timedelta(minutes=1)).isoformat(),
        "snapshot_as_of": None,
        "capture_available_at": None,
    }
    again = store(client, owner="run-2", now=NOW + timedelta(minutes=9))
    again.claim(OPERATION, CUTOFF)
    again.note_timeline(
        OPERATION,
        {
            "observation_window_end": CUTOFF.isoformat(),
            "collection_started_at": None,
            "snapshot_as_of": (NOW + timedelta(minutes=10)).isoformat(),
        },
    )
    again.release_claim(OPERATION)
    timeline = client.control()["timeline"]
    assert timeline["collection_started_at"] == NOW.isoformat()
    assert timeline["snapshot_as_of"] == (NOW + timedelta(minutes=10)).isoformat()
    assert timeline["capture_available_at"] is None
    assert again.read_timeline(OPERATION) == timeline
    assert store(FakeObjectClient()).read_timeline(OPERATION) is None


def test_record_stage_is_create_only_and_published_by_control_cas():
    client = FakeObjectClient()
    current = store(client)
    current.claim(OPERATION, CUTOFF)
    current.record_stage(OPERATION, "collect", result("pending", "1" * 64, "bat_collect"))
    control = client.control()
    first_name = f"42/daily/slots/{OPERATION}/stages/collect/1-1.json"
    assert control["stages"]["collect"]["reference"] == first_name
    assert control["stages"]["collect"]["state"] == "pending"
    assert control["stages"]["collect"]["epoch"] == 1
    assert control["dispatch_intent"] == {
        "stage": "collect",
        "business_attempt_id": "bat_collect",
        "input_digest": "1" * 64,
        "epoch": 1,
    }
    raw, generation = client.objects[first_name]
    assert generation == 1
    assert control["stages"]["collect"]["record_digest"] == sha256(raw).hexdigest()
    record = json.loads(raw)
    assert record["contract_version"] == STAGE_RECORD_CONTRACT
    assert record["attempt"] == 1
    assert record["recorded_at"] == NOW.isoformat()
    assert record["result"] == result("pending", "1" * 64, "bat_collect")
    current.record_stage(OPERATION, "collect", result("succeeded", "1" * 64, "out", "2" * 64))
    second_name = f"42/daily/slots/{OPERATION}/stages/collect/1-2.json"
    assert client.objects[first_name] == (raw, 1)
    assert client.control()["stages"]["collect"]["reference"] == second_name
    assert [write for write in client.writes if "stages" in write[0]] == [
        (first_name, 0),
        (second_name, 0),
    ]
    assert current.read_stage(OPERATION, "collect") == result(
        "succeeded", "1" * 64, "out", "2" * 64
    )


def test_record_stage_requires_the_lease_and_a_valid_result():
    client = FakeObjectClient()
    unclaimed = store(client)
    with pytest.raises(StoreRefusal, match=r"^lease_not_held$"):
        unclaimed.record_stage(OPERATION, "collect", result("pending", "1" * 64))
    unclaimed.claim(OPERATION, CUTOFF)
    with pytest.raises(StoreRefusal, match=r"^stage_key_invalid$"):
        unclaimed.record_stage(OPERATION, "Bad Key", result("pending", "1" * 64))
    with pytest.raises(ValueError, match=r"^stage_record_invalid$"):
        unclaimed.record_stage(OPERATION, "collect", {"state": "succeeded"})


def test_read_stage_preserves_unknown_when_the_reference_cannot_be_read():
    client = FakeObjectClient()
    current = store(client)
    current.claim(OPERATION, CUTOFF)
    assert current.read_stage(OPERATION, "collect") is None
    current.record_stage(OPERATION, "collect", result("unknown", "1" * 64, "bat_collect"))
    name = f"42/daily/slots/{OPERATION}/stages/collect/1-1.json"
    client.failing_reads.add(name)
    with pytest.raises(OSError, match="read failed"):
        current.read_stage(OPERATION, "collect")
    client.failing_reads.clear()
    client.objects[name] = (b'{"tampered": true}', 1)
    with pytest.raises(StoreRefusal, match=r"^stage_record_conflict$"):
        current.read_stage(OPERATION, "collect")
    del client.objects[name]
    with pytest.raises(StoreRefusal, match=r"^stage_record_missing$"):
        current.read_stage(OPERATION, "collect")
    assert store(FakeObjectClient()).read_stage(OPERATION, "collect") is None


def test_kernel_runs_to_release_pending_over_the_store_and_releases_the_lease():
    client = FakeObjectClient()
    current = store(client)
    profile = {
        "schema_version": "42_daily_v1",
        "resource_manifest_digest": "b" * 64,
        "source_policy_digest": "c" * 64,
        "recurring_grant_digest": "d" * 64,
        "freshness_target_hours": 30,
        "max_publish_lag_hours": 48,
        "max_catchup_cutoffs": 2,
    }
    issued = []

    def issue(stage, manifest):
        if stage == "release":
            return None
        issued.append(stage)
        return {"business_attempt_id": f"bat_{stage}"}

    def handler(*, manifest, authority_receipt):
        digest = sha256(canonical_bytes(manifest)).hexdigest()
        return result("succeeded", digest, authority_receipt["business_attempt_id"], "9" * 64)

    authority = Mock()
    authority.issue.side_effect = issue
    stages = dict.fromkeys(("collect", "capture", "compose", "certify", "release"), handler)
    outcome = execute_daily_cycle(
        operation_id=OPERATION,
        cutoff_utc=CUTOFF,
        profile=profile,
        authority=authority,
        store=current,
        stages=stages,
    )
    assert outcome == {"state": "release_pending", "operation_id": OPERATION, "stage": "release"}
    assert issued == ["collect", "capture", "compose", "certify"]
    control = client.control()
    assert sorted(control["stages"]) == ["capture", "certify", "collect", "compose"]
    assert all(entry["state"] == "succeeded" for entry in control["stages"].values())
    assert control["lease"] is None
    rerun = execute_daily_cycle(
        operation_id=OPERATION,
        cutoff_utc=CUTOFF,
        profile=profile,
        authority=authority,
        store=store(client, owner="run-2", now=NOW + timedelta(minutes=1)),
        stages=stages,
    )
    assert rerun["state"] == "release_pending"
    assert issued == ["collect", "capture", "compose", "certify"]


def test_resume_incomplete_units_works_over_the_store():
    client = FakeObjectClient()
    current = store(client)
    current.claim(OPERATION, CUTOFF)
    current.record_unit(
        OPERATION,
        "za_rss",
        stage="collect",
        permit_digest="5" * 64,
        result=result("succeeded", "1" * 64, "za:done", "2" * 64),
    )
    current.record_unit(
        OPERATION,
        "ng_rss",
        stage="collect",
        permit_digest="6" * 64,
        result=result("failed", "1" * 64, "ng:failed", retry_safe=True),
    )
    current.release_claim(OPERATION)
    ledger = current.read_unit_ledger(OPERATION)
    assert ledger == {
        "za_rss": {
            "stage": "collect",
            "permit_digest": "5" * 64,
            "result": result("succeeded", "1" * 64, "za:done", "2" * 64),
        },
        "ng_rss": {
            "stage": "collect",
            "permit_digest": "6" * 64,
            "result": result("failed", "1" * 64, "ng:failed", retry_safe=True),
        },
    }
    with pytest.raises(ValueError, match=r"^unit_ledger_invalid$"):
        store(client).read_unit_ledger("b" * 64)
    manifest = {
        "slot_id": OPERATION,
        "cutoff_utc": CUTOFF,
        "resume_authority_digest": "9" * 64,
        "attempt_version": 2,
        "unit_ledger": ledger,
        "resume_units": ["ng_rss"],
        "amended_permits": {},
    }
    authority = Mock()
    authority.issue.side_effect = lambda stage, manifest: {
        "business_attempt_id": f"bat_{stage}",
        "resume_authority_digest": "9" * 64,
    }

    def collect(*, manifest, authority_receipt):
        digest = sha256(canonical_bytes(manifest)).hexdigest()
        return result("succeeded", digest, authority_receipt["business_attempt_id"], "3" * 64)

    resumed = store(client, owner="run-2", now=NOW + timedelta(minutes=3))
    outcome = resume_incomplete_units(
        slot_id=OPERATION,
        approved_unit_manifest=manifest,
        store=resumed,
        authority=authority,
        stages={"collect": collect},
    )
    assert outcome == {"state": "succeeded", "operation_id": OPERATION, "stage": "resume"}
    control = client.control()
    assert control["stages"]["ng_rss@2"]["state"] == "succeeded"
    assert control["stages"]["ng_rss"]["state"] == "failed"
    assert (
        control["units"]["ng_rss"]["reference"]
        == f"42/daily/slots/{OPERATION}/stages/ng_rss/1-1.json"
    )
    assert resumed.read_stage(OPERATION, "ng_rss@2")["output_digest"] == "3" * 64
    assert resumed.read_stage(OPERATION, "ng_rss") == result(
        "failed", "1" * 64, "ng:failed", retry_safe=True
    )
