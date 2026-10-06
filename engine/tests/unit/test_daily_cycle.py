"""Pure orchestration tests for the durable daily cycle kernel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import Mock

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cycle import (
    STAGE_ORDER,
    execute_daily_cycle,
    resume_incomplete_units,
    run_validated_daily_cycle,
    validate_closed_cutoff,
    validate_daily_profile,
)

CUTOFF = datetime(2026, 9, 12, tzinfo=UTC)
NOW = datetime(2026, 9, 13, 6, tzinfo=UTC)


def profile(**overrides):
    base = {
        "schema_version": "42_daily_v1",
        "resource_manifest_digest": "b" * 64,
        "source_policy_digest": "c" * 64,
        "recurring_grant_digest": "d" * 64,
        "freshness_target_hours": 30,
        "max_publish_lag_hours": 48,
        "max_catchup_cutoffs": 2,
    }
    base.update(overrides)
    return base


def digest_of(manifest) -> str:
    return sha256(canonical_bytes(manifest)).hexdigest()


class FakeStore:
    """Dictionary backed durable store with an explicit claim flag."""

    def __init__(self, *, claimed: bool = False, die_on_record: int = 0):
        self.records: dict[tuple[str, str], dict] = {}
        self.claimed = claimed
        self.claims: list[tuple[str, datetime]] = []
        self.releases: list[str] = []
        self.record_calls = 0
        self.die_on_record = die_on_record

    def claim(self, operation_id, cutoff_utc):
        self.claims.append((operation_id, cutoff_utc))
        if self.claimed:
            return False
        self.claimed = True
        return True

    def read_stage(self, operation_id, stage):
        record = self.records.get((operation_id, stage))
        return None if record is None else dict(record)

    def record_stage(self, operation_id, stage, result):
        self.record_calls += 1
        if self.record_calls == self.die_on_record:
            raise RuntimeError("worker died before pending persistence")
        self.records[(operation_id, stage)] = dict(result)

    def release_claim(self, operation_id):
        self.claimed = False
        self.releases.append(operation_id)


class FakeAuthority:
    """Idempotent issuer keyed by stage and canonical manifest digest."""

    def __init__(self, *, deny=(), revoked: bool = False, resume_digest: str | None = None):
        self.deny = set(deny)
        self.revoked = revoked
        self.resume_digest = resume_digest
        self.issued: dict[tuple[str, str], dict] = {}
        self.reservations = 0
        self.calls: list[str] = []

    def issue(self, stage, manifest):
        self.calls.append(stage)
        if self.revoked or stage in self.deny:
            return None
        key = (stage, digest_of(manifest))
        if key not in self.issued:
            self.reservations += 1
            receipt = {
                "business_attempt_id": f"attempt:{stage}:{key[1][:12]}",
                "authority_reference": f"authority:{len(self.issued) + 1}",
            }
            if stage == "resume":
                receipt["resume_authority_digest"] = self.resume_digest
            self.issued[key] = receipt
        return self.issued[key]


class Handler:
    """Stage handler returning a chosen state, recording every invocation."""

    def __init__(self, state: str = "succeeded", *, explode: bool = False):
        self.state = state
        self.explode = explode
        self.calls: list[dict] = []

    def __call__(self, *, manifest, authority_receipt):
        self.calls.append({"manifest": dict(manifest), "receipt": dict(authority_receipt)})
        if self.explode:
            raise RuntimeError("worker died after dispatch")
        output = (
            digest_of({"stage": manifest["stage"], "attempt": manifest})
            if (self.state == "succeeded")
            else ""
        )
        return {
            "state": self.state,
            "input_digest": digest_of(manifest),
            "output_digest": output,
            "result_reference": f"native:{manifest['stage']}",
            "retry_safe": False,
        }


def handlers(**overrides):
    stages = {stage: Handler() for stage in STAGE_ORDER}
    stages.update(overrides)
    return stages


def run(store, authority, stages, *, profile_value=None, operation_id="run-a", cutoff=CUTOFF):
    return execute_daily_cycle(
        operation_id=operation_id,
        cutoff_utc=cutoff,
        profile=profile_value or profile(),
        authority=authority,
        store=store,
        stages=stages,
    )


def stored(state: str, **overrides):
    record = {
        "state": state,
        "input_digest": "a" * 64,
        "output_digest": "",
        "result_reference": "operation:held",
        "retry_safe": False,
    }
    record.update(overrides)
    return record


def test_unknown_paid_collection_never_repeats():
    store = Mock()
    store.claim.return_value = True
    store.read_stage.return_value = {
        "state": "unknown",
        "input_digest": "a" * 64,
        "output_digest": "",
        "result_reference": "operation:held",
        "retry_safe": False,
    }
    collect = Mock()
    result = execute_daily_cycle(
        operation_id="run-a",
        cutoff_utc=datetime(2026, 9, 12, tzinfo=UTC),
        profile={
            "schema_version": "42_daily_v1",
            "resource_manifest_digest": "b" * 64,
            "source_policy_digest": "c" * 64,
            "recurring_grant_digest": "d" * 64,
            "freshness_target_hours": 30,
            "max_publish_lag_hours": 48,
            "max_catchup_cutoffs": 2,
        },
        authority=Mock(),
        store=store,
        stages={"collect": collect},
    )
    assert result["state"] == "unavailable"
    collect.assert_not_called()


@pytest.mark.parametrize("state", ["partial", "cancelled", "pending"])
def test_held_paid_collection_states_run_no_handler(state):
    store = Mock()
    store.claim.return_value = True
    store.read_stage.return_value = stored(state)
    collect = Mock()
    authority = Mock()
    result = execute_daily_cycle(
        operation_id="run-a",
        cutoff_utc=CUTOFF,
        profile=profile(),
        authority=authority,
        store=store,
        stages={"collect": collect},
    )
    assert result == {"state": "unavailable", "operation_id": "run-a", "stage": "collect"}
    collect.assert_not_called()
    authority.issue.assert_not_called()
    store.release_claim.assert_called_once_with("run-a")


def test_unrecognized_provider_state_is_refused_before_any_handler():
    store = Mock()
    store.claim.return_value = True
    store.read_stage.return_value = stored("provider_running")
    collect = Mock()
    with pytest.raises(ValueError, match="invalid_stage_state"):
        execute_daily_cycle(
            operation_id="run-a",
            cutoff_utc=CUTOFF,
            profile=profile(),
            authority=Mock(),
            store=store,
            stages={"collect": collect},
        )
    collect.assert_not_called()
    store.release_claim.assert_called_once_with("run-a")


def test_full_cycle_chains_predecessor_digests_and_records_every_stage():
    store, authority, stages = FakeStore(), FakeAuthority(), handlers()
    assert run(store, authority, stages) == {
        "state": "succeeded",
        "operation_id": "run-a",
        "stage": "release",
    }
    assert [store.records[("run-a", stage)]["state"] for stage in STAGE_ORDER] == ["succeeded"] * 5
    previous = ""
    for stage in STAGE_ORDER:
        manifest = stages[stage].calls[0]["manifest"]
        assert manifest["predecessor_digest"] == previous
        assert manifest["cutoff_utc"] == "2026-09-12T00:00:00+00:00"
        assert store.records[("run-a", stage)]["input_digest"] == digest_of(manifest)
        previous = store.records[("run-a", stage)]["output_digest"]
    assert authority.reservations == 5
    assert store.releases == ["run-a"]


def test_simultaneous_trigger_loses_the_claim_and_touches_nothing():
    store, authority, stages = FakeStore(claimed=True), FakeAuthority(), handlers()
    assert run(store, authority, stages) == {
        "state": "already_claimed",
        "operation_id": "run-a",
        "stage": "",
    }
    assert store.records == {}
    assert authority.calls == []
    assert all(not handler.calls for handler in stages.values())
    assert store.releases == []


def test_worker_death_after_dispatch_leaves_pending_and_reruns_stop():
    store, authority = FakeStore(), FakeAuthority()
    stages = handlers(collect=Handler(explode=True))
    with pytest.raises(RuntimeError, match="worker died after dispatch"):
        run(store, authority, stages)
    pending = store.records[("run-a", "collect")]
    assert pending["state"] == "pending"
    assert pending["result_reference"].startswith("attempt:collect:")
    assert store.releases == ["run-a"]
    assert run(store, authority, stages) == {
        "state": "unavailable",
        "operation_id": "run-a",
        "stage": "collect",
    }
    assert len(stages["collect"].calls) == 1
    assert authority.reservations == 1
    assert store.records[("run-a", "collect")] == pending


def test_succeeded_collection_with_failed_capture_stops_and_stays_held():
    store, authority = FakeStore(), FakeAuthority()
    stages = handlers(capture=Handler("failed"))
    assert run(store, authority, stages) == {
        "state": "failed",
        "operation_id": "run-a",
        "stage": "capture",
    }
    assert store.records[("run-a", "collect")]["state"] == "succeeded"
    assert store.records[("run-a", "capture")]["state"] == "failed"
    assert not stages["compose"].calls
    assert run(store, authority, stages) == {
        "state": "unavailable",
        "operation_id": "run-a",
        "stage": "capture",
    }
    assert len(stages["collect"].calls) == 1
    assert len(stages["capture"].calls) == 1


def test_retry_safe_failure_is_retried_only_with_matching_input_digest():
    store, authority, stages = FakeStore(), FakeAuthority(), handlers()
    first = run(store, authority, handlers(capture=Handler("failed")))
    assert first["state"] == "failed"
    store.records[("run-a", "capture")]["retry_safe"] = True
    assert run(store, authority, stages)["state"] == "succeeded"
    assert not stages["collect"].calls
    assert len(stages["capture"].calls) == 1


def test_completed_capture_with_held_release_is_release_pending():
    store, authority, stages = FakeStore(), FakeAuthority(deny={"release"}), handlers()
    assert run(store, authority, stages) == {
        "state": "release_pending",
        "operation_id": "run-a",
        "stage": "release",
    }
    assert [store.records[("run-a", stage)]["state"] for stage in STAGE_ORDER[:4]] == (
        ["succeeded"] * 4
    )
    assert ("run-a", "release") not in store.records
    assert not stages["release"].calls
    reservations = authority.reservations
    assert run(store, authority, stages)["state"] == "release_pending"
    assert authority.reservations == reservations
    assert all(len(stages[stage].calls) == 1 for stage in STAGE_ORDER[:4])


@pytest.mark.parametrize(
    "authority", [FakeAuthority(deny={"collect"}), FakeAuthority(revoked=True)]
)
def test_expired_grant_or_revoked_principal_stops_before_dispatch(authority):
    store, stages = FakeStore(), handlers()
    assert run(store, authority, stages) == {
        "state": "authority_missing",
        "operation_id": "run-a",
        "stage": "collect",
    }
    assert store.records == {}
    assert all(not handler.calls for handler in stages.values())
    assert store.releases == ["run-a"]


def test_changed_source_policy_conflicts_with_recorded_stage_input():
    store, authority, stages = FakeStore(), FakeAuthority(), handlers()
    assert run(store, authority, stages)["state"] == "succeeded"
    changed = profile(source_policy_digest="e" * 64)
    with pytest.raises(ValueError, match="stage_input_conflict"):
        run(store, authority, handlers(), profile_value=changed)
    assert len(stages["collect"].calls) == 1
    assert store.releases == ["run-a", "run-a"]


def test_recorded_input_digest_conflict_is_refused():
    store, authority, stages = FakeStore(), FakeAuthority(), handlers()
    store.records[("run-a", "collect")] = stored("succeeded", output_digest="f" * 64)
    with pytest.raises(ValueError, match="stage_input_conflict"):
        run(store, authority, stages)
    assert all(not handler.calls for handler in stages.values())


@pytest.mark.parametrize(
    "changed",
    [profile(recurring_grant_digest="e" * 64), profile(freshness_target_hours=24)],
)
def test_grant_renewal_or_freshness_change_cannot_repeat_a_pending_paid_attempt(changed):
    store, authority, stages = FakeStore(), FakeAuthority(), handlers()
    store.records[("run-a", "collect")] = stored("pending", result_reference="attempt:collect:1")
    assert run(store, authority, stages, profile_value=changed) == {
        "state": "unavailable",
        "operation_id": "run-a",
        "stage": "collect",
    }
    assert authority.calls == []
    assert all(not handler.calls for handler in stages.values())


def test_authority_issue_is_idempotent_across_death_before_pending_persistence():
    store, authority, stages = FakeStore(die_on_record=1), FakeAuthority(), handlers()
    with pytest.raises(RuntimeError, match="before pending persistence"):
        run(store, authority, stages)
    assert store.records == {}
    assert authority.reservations == 1
    assert run(store, authority, stages)["state"] == "succeeded"
    assert authority.reservations == 5
    assert len([key for key in authority.issued if key[0] == "collect"]) == 1
    assert len(stages["collect"].calls) == 1
    first_manifest = stages["collect"].calls[0]["manifest"]
    assert (
        stages["collect"].calls[0]["receipt"]
        == (authority.issued[("collect", digest_of(first_manifest))])
    )


class BadResult(Handler):
    def __init__(self, patch):
        super().__init__()
        self.patch = patch

    def __call__(self, *, manifest, authority_receipt):
        result = super().__call__(manifest=manifest, authority_receipt=authority_receipt)
        result.update(self.patch)
        return result


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"state": "running"}, "invalid_stage_state"),
        ({"input_digest": "0" * 64}, "result_input_conflict"),
        ({"output_digest": ""}, "result_output_missing"),
    ],
)
def test_unadmitted_stage_results_leave_the_pending_record(patch, code):
    store, authority = FakeStore(), FakeAuthority()
    with pytest.raises(ValueError, match=code):
        run(store, authority, handlers(collect=BadResult(patch)))
    assert store.records[("run-a", "collect")]["state"] == "pending"
    assert store.releases == ["run-a"]


def test_validated_cycle_requires_closed_aware_cutoff_and_exact_profile():
    store, authority, stages = FakeStore(), FakeAuthority(), handlers()
    kwargs = {
        "operation_id": "run-a",
        "now": NOW,
        "authority": authority,
        "store": store,
        "stages": stages,
    }
    with pytest.raises(ValueError, match="cutoff_utc_not_timezone_aware"):
        run_validated_daily_cycle(cutoff_utc=datetime(2026, 9, 12), profile=profile(), **kwargs)
    with pytest.raises(ValueError, match="cutoff_window_open"):
        run_validated_daily_cycle(
            cutoff_utc=NOW + timedelta(seconds=1), profile=profile(), **kwargs
        )
    with pytest.raises(ValueError, match="profile_fields_inexact"):
        run_validated_daily_cycle(cutoff_utc=CUTOFF, profile=profile(extra=1), **kwargs)
    with pytest.raises(ValueError, match="stage_handlers_inexact"):
        run_validated_daily_cycle(
            cutoff_utc=CUTOFF, profile=profile(), **{**kwargs, "stages": {"collect": Handler()}}
        )
    assert store.claims == []
    assert run_validated_daily_cycle(cutoff_utc=CUTOFF, profile=profile(), **kwargs)["state"] == (
        "succeeded"
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"schema_version": "42_daily_v0"}, "profile_schema_unsupported"),
        ({"source_policy_digest": "C" * 64}, "profile_field_invalid:source_policy_digest"),
        ({"freshness_target_hours": 0}, "profile_field_invalid:freshness_target_hours"),
        ({"freshness_target_hours": True}, "profile_field_invalid:freshness_target_hours"),
        ({"max_publish_lag_hours": 12}, "profile_field_invalid:max_publish_lag_hours"),
        ({"max_catchup_cutoffs": -1}, "profile_field_invalid:max_catchup_cutoffs"),
    ],
)
def test_profile_fields_are_typed_and_bounded(overrides, code):
    with pytest.raises(ValueError, match=code):
        validate_daily_profile(profile(**overrides))
    with pytest.raises(ValueError, match="profile_fields_inexact"):
        validate_daily_profile({k: v for k, v in profile().items() if k != "max_catchup_cutoffs"})
    assert validate_daily_profile(profile()) == profile()
    assert validate_closed_cutoff(CUTOFF, now=NOW) == CUTOFF


def unit(state: str, stage: str = "collect", **overrides):
    return {
        "stage": stage,
        "permit_digest": "7" * 64,
        "result": stored(state, result_reference=f"permit:{state}", **overrides),
    }


def resume_manifest(**overrides):
    base = {
        "slot_id": "slot-1",
        "cutoff_utc": CUTOFF,
        "resume_authority_digest": "8" * 64,
        "attempt_version": 2,
        "unit_ledger": {
            "collect:socialcrawl:za": unit("succeeded", output_digest="9" * 64),
            "collect:socialcrawl:ng": unit("partial"),
            "collect:socialcrawl:ke": unit("pending"),
        },
        "resume_units": ["collect:socialcrawl:ng"],
        "amended_permits": {},
    }
    base.update(overrides)
    return base


def seeded_store(manifest):
    store = FakeStore()
    for unit_id, item in manifest["unit_ledger"].items():
        store.records[("slot-1", unit_id)] = dict(item["result"])
    return store


def resume(manifest, store=None, authority=None, stages=None):
    return resume_incomplete_units(
        slot_id="slot-1",
        approved_unit_manifest=manifest,
        store=seeded_store(manifest) if store is None else store,
        authority=FakeAuthority(resume_digest="8" * 64) if authority is None else authority,
        stages=handlers() if stages is None else stages,
    )


def test_resume_dispatches_only_named_units_and_publishes_a_new_attempt_version():
    manifest = resume_manifest()
    store, authority, stages = (
        seeded_store(manifest),
        FakeAuthority(resume_digest="8" * 64),
        handlers(),
    )
    assert resume(manifest, store, authority, stages) == {
        "state": "succeeded",
        "operation_id": "slot-1",
        "stage": "resume",
    }
    assert len(stages["collect"].calls) == 1
    dispatched = stages["collect"].calls[0]["manifest"]
    assert dispatched["unit_id"] == "collect:socialcrawl:ng"
    assert dispatched["permit_digest"] == "7" * 64
    assert dispatched["original_result"]["state"] == "partial"
    assert store.records[("slot-1", "collect:socialcrawl:ng")]["state"] == "partial"
    assert store.records[("slot-1", "collect:socialcrawl:ng@2")]["state"] == "succeeded"
    assert ("slot-1", "collect:socialcrawl:ke@2") not in store.records
    assert ("slot-1", "collect:socialcrawl:za@2") not in store.records
    assert authority.calls == ["resume", "collect:socialcrawl:ng"]
    assert store.releases == ["slot-1"]


def test_resume_uses_explicitly_amended_permit():
    manifest = resume_manifest(amended_permits={"collect:socialcrawl:ng": "6" * 64})
    stages = handlers()
    assert resume(manifest, stages=stages)["state"] == "succeeded"
    assert stages["collect"].calls[0]["manifest"]["permit_digest"] == "6" * 64


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"resume_units": ["collect:socialcrawl:us"]}, "unknown_unit"),
        ({"amended_permits": {"collect:brand24:za": "6" * 64}}, "unknown_unit"),
        ({"resume_units": ["collect:socialcrawl:za"]}, "unit_already_succeeded"),
        ({"resume_units": []}, "resume_units_invalid"),
        (
            {"resume_units": ["collect:socialcrawl:ng", "collect:socialcrawl:ng"]},
            "resume_units_invalid",
        ),
        ({"attempt_version": 1}, "attempt_version_invalid"),
        ({"slot_id": "slot-2"}, "resume_slot_mismatch"),
        ({"extra": True}, "resume_manifest_inexact"),
        ({"cutoff_utc": datetime(2026, 9, 12)}, "cutoff_utc_not_timezone_aware"),
        ({"resume_authority_digest": "short"}, "profile_field_invalid:resume_authority_digest"),
    ],
)
def test_resume_refuses_bad_manifests_before_claiming(overrides, code):
    manifest = resume_manifest(**overrides)
    store, stages = seeded_store(resume_manifest()), handlers()
    with pytest.raises(ValueError, match=code):
        resume(manifest, store, stages=stages)
    assert store.claims == []
    assert all(not handler.calls for handler in stages.values())


def test_resume_refuses_unknown_state_unit():
    manifest = resume_manifest(
        unit_ledger={**resume_manifest()["unit_ledger"], "collect:socialcrawl:ng": unit("unknown")}
    )
    stages = handlers()
    with pytest.raises(ValueError, match="unit_state_unknown"):
        resume(manifest, stages=stages)
    assert not stages["collect"].calls


def test_resume_requires_the_full_original_ledger_to_match_the_store():
    manifest = resume_manifest()
    missing = seeded_store(manifest)
    del missing.records[("slot-1", "collect:socialcrawl:ke")]
    stages = handlers()
    with pytest.raises(ValueError, match="unit_ledger_incomplete"):
        resume(manifest, missing, stages=stages)
    conflicting = seeded_store(manifest)
    conflicting.records[("slot-1", "collect:socialcrawl:za")]["output_digest"] = "0" * 64
    with pytest.raises(ValueError, match="unit_ledger_conflict"):
        resume(manifest, conflicting, stages=stages)
    assert not stages["collect"].calls
    assert missing.releases == ["slot-1"]
    assert conflicting.releases == ["slot-1"]


def test_resume_requires_exact_admitted_resume_authority():
    manifest = resume_manifest()
    stages = handlers()
    with pytest.raises(ValueError, match="resume_authority_mismatch"):
        resume(manifest, authority=FakeAuthority(resume_digest="1" * 64), stages=stages)
    assert resume(manifest, authority=FakeAuthority(revoked=True), stages=stages) == {
        "state": "authority_missing",
        "operation_id": "slot-1",
        "stage": "resume",
    }
    assert not stages["collect"].calls


def test_resume_never_overwrites_an_existing_attempt_version():
    manifest = resume_manifest()
    store = seeded_store(manifest)
    store.records[("slot-1", "collect:socialcrawl:ng@2")] = stored("unknown")
    stages = handlers()
    with pytest.raises(ValueError, match="attempt_version_exists"):
        resume(manifest, store, stages=stages)
    assert not stages["collect"].calls
    assert store.records[("slot-1", "collect:socialcrawl:ng@2")]["state"] == "unknown"


def test_resume_loses_claim_when_slot_is_held():
    manifest = resume_manifest()
    store = seeded_store(manifest)
    store.claimed = True
    assert resume(manifest, store) == {
        "state": "already_claimed",
        "operation_id": "slot-1",
        "stage": "",
    }


def test_resume_records_non_succeeded_unit_result_under_the_new_version():
    manifest = resume_manifest()
    store = seeded_store(manifest)
    stages = handlers(collect=Handler("partial"))
    assert resume(manifest, store, stages=stages) == {
        "state": "partial",
        "operation_id": "slot-1",
        "stage": "collect:socialcrawl:ng@2",
    }
    assert store.records[("slot-1", "collect:socialcrawl:ng@2")]["state"] == "partial"
    assert store.records[("slot-1", "collect:socialcrawl:ng")]["state"] == "partial"
