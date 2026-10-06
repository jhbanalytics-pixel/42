"""DailyAuthority consumes the recurring grant through the durable reservation path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from src.analysis.open_intelligence import daily_authority
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_authority import (
    AUTHORITY_CONTRACT,
    AuthorityRecordExists,
    AuthorityRefusal,
    DailyAuthority,
    business_attempt_id,
)
from src.analysis.open_intelligence import recurring_grant
from src.analysis.open_intelligence.daily_cycle import execute_daily_cycle
from src.analysis.open_intelligence.recurring_grant import (
    ALLOWED_OPERATIONS,
    GRANT_CONTRACT,
    RELEASE_MANIFEST_CONTRACT,
    STAGE_OPERATIONS,
    GrantRefusal,
    grant_digest,
)

DOMAIN = "ogilvy-trends-v2.iam.gserviceaccount.com"
ORCHESTRATION = f"intelligence-42-orchestration@{DOMAIN}"
IMAGE = "sha256:" + "4" * 64
VALID_FROM = datetime(2026, 9, 15, tzinfo=UTC)
NOW = datetime(2026, 9, 20, 4, 30, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 20, tzinfo=UTC)
CUTOFFS = [(VALID_FROM + timedelta(days=offset)).date().isoformat() for offset in range(30)]
DELEGATED_RELEASE = {
    "contract_version": RELEASE_MANIFEST_CONTRACT,
    "delegation": "recurring",
    "human_release_contract": "42_release_authority_v1",
}


def grant(**overrides):
    base = {
        "contract_version": GRANT_CONTRACT,
        "grant_id": "recurring_execution_20260915_v1",
        "environment": "staging",
        "issuing_principal": "Albert Meintjes",
        "executing_principals": [ORCHESTRATION],
        "resource_manifest_digest": "b" * 64,
        "allowed_operations": list(ALLOWED_OPERATIONS),
        "source_policy_digest": "c" * 64,
        "schema_version": "42_daily_v1",
        "approved_release_manifest_digest": sha256(canonical_bytes(DELEGATED_RELEASE)).hexdigest(),
        "permitted_image_digests": [IMAGE],
        "run_allowance_micro_usd": 450000,
        "monthly_allowance_micro_usd": 15000000,
        "reserved_micro_usd_per_capture": 500000,
        "cumulative_ceiling_micro_usd": 15000000,
        "permitted_cutoffs": list(CUTOFFS),
        "valid_from": "2026-09-15T00:00:00+00:00",
        "valid_until": "2026-10-15T00:00:00+00:00",
        "retry_rules": {
            "failed_stage": "retry_safe_only",
            "unknown_or_partial": "hold_for_reconciliation",
            "max_attempt_versions": 3,
        },
        "revocation_state": "active",
        "revocation_phrase_sha256": "f" * 64,
    }
    base.update(overrides)
    return base


def profile(value, **overrides):
    base = {
        "schema_version": "42_daily_v1",
        "resource_manifest_digest": "b" * 64,
        "source_policy_digest": "c" * 64,
        "recurring_grant_digest": grant_digest(value),
        "freshness_target_hours": 30,
        "max_publish_lag_hours": 48,
        "max_catchup_cutoffs": 2,
    }
    base.update(overrides)
    return base


def manifest(stage, value, predecessor="", **profile_overrides):
    return {
        "operation_id": "slot-a",
        "stage": stage,
        "cutoff_utc": CUTOFF.isoformat(),
        "profile": profile(value, **profile_overrides),
        "predecessor_digest": predecessor,
    }


class FakeReservation:
    """The engine authority adapter shape: reserve then consume, once per manifest."""

    def __init__(self, *, conflict=False):
        self.calls = []
        self.conflict = conflict
        self.attempts = 0

    def reserve(self, invocation, run_manifest):
        self.calls.append(("reserve", run_manifest["manifest_sha256"]))
        return {"authority": run_manifest["business_attempt_id"], "run_manifest": run_manifest}

    def consume(self, authority):
        self.calls.append(("consume", authority["authority"]))
        self.attempts += 1
        if self.conflict:
            raise ValueError("execution_approval_concurrent_conflict")
        return {
            "consumption_id": "exc_" + sha256(authority["authority"].encode()).hexdigest(),
            "manifest_sha256": authority["run_manifest"]["manifest_sha256"],
            "operation": authority["run_manifest"]["operation"],
            "consumed_at": "2026-09-20T04:30:00.000000Z",
        }

    @property
    def consumed(self):
        return 0 if self.conflict else self.attempts


class FakeLedger:
    def __init__(self):
        self.records = {}
        self.creates = 0

    def read(self, attempt_id):
        return self.records.get(attempt_id)

    def create(self, attempt_id, record):
        self.creates += 1
        if attempt_id in self.records:
            raise AuthorityRecordExists(attempt_id)
        self.records[attempt_id] = dict(record)
        return f"42/daily/authority/{attempt_id}.json"


class FakeStore:
    def __init__(self, *, die_on_record=0):
        self.records = {}
        self.record_calls = 0
        self.die_on_record = die_on_record

    def claim(self, operation_id, cutoff_utc):
        return True

    def read_stage(self, operation_id, stage):
        return self.records.get((operation_id, stage))

    def record_stage(self, operation_id, stage, result):
        self.record_calls += 1
        if self.record_calls == self.die_on_record:
            raise RuntimeError("worker died before pending state persisted")
        self.records[(operation_id, stage)] = dict(result)

    def release_claim(self, operation_id):
        return None


def authority(value=None, *, reservation=None, ledger=None, fence=None, **overrides):
    value = value or grant()
    arguments = {
        "grant": value,
        "profile": profile(value),
        "principal": ORCHESTRATION,
        "image_digest": IMAGE,
        "resource_manifest_digest": "b" * 64,
        "reservation": reservation if reservation is not None else FakeReservation(),
        "ledger": ledger if ledger is not None else FakeLedger(),
        "fence": fence or (lambda: {"owner": "run-1", "epoch": 1}),
        "now": lambda: NOW,
    }
    arguments.update(overrides)
    return DailyAuthority(**arguments)


def test_issue_returns_preassigned_attempt_and_reservation_without_execution_name():
    reservation, ledger = FakeReservation(), FakeLedger()
    issuer = authority(reservation=reservation, ledger=ledger)
    receipt = issuer.issue("collect", manifest("collect", grant()))
    expected_attempt = business_attempt_id("slot-a", "collect")
    assert receipt["business_attempt_id"] == expected_attempt
    assert receipt["authority_reference"] == f"42/daily/authority/{expected_attempt}.json"
    assert receipt["operation"] == "collection"
    assert receipt["lease_epoch"] == 1
    assert receipt["reservation"]["consumption_id"].startswith("exc_")
    assert "execution_name" not in receipt
    assert reservation.consumed == 1
    assert reservation.calls[0] == ("reserve", receipt["manifest_digest"])
    record = ledger.records[expected_attempt]
    assert record["contract_version"] == AUTHORITY_CONTRACT
    assert (
        record["manifest_digest"]
        == sha256(canonical_bytes(manifest("collect", grant()))).hexdigest()
    )
    assert record["grant_digest"] == grant_digest(grant())


class ObjectReservation(FakeReservation):
    """A reservation path answering the engine's shape: an authority object carrying its generation."""

    class Authority:
        def __init__(self, attempt, run_manifest):
            self.attempt = attempt
            self.run_manifest = run_manifest
            self.generation = f"generation:{attempt}"

        def __getitem__(self, key):
            return {"authority": self.attempt, "run_manifest": self.run_manifest}[key]

    def reserve(self, invocation, run_manifest):
        self.calls.append(("reserve", run_manifest["manifest_sha256"]))
        return self.Authority(run_manifest["business_attempt_id"], run_manifest)


def test_issue_keeps_the_issued_authority_beside_the_consumption_and_reissues_it():
    reservation, ledger = ObjectReservation(), FakeLedger()
    issuer = authority(reservation=reservation, ledger=ledger)
    receipt = issuer.issue("capture", manifest("capture", grant()))
    attempt = business_attempt_id("slot-a", "capture")
    execution = receipt["execution"]
    assert set(execution) == daily_authority.EXECUTION_FIELDS
    issued = execution["authority"]
    assert isinstance(issued, ObjectReservation.Authority)
    assert issued.attempt == attempt
    # The reserved manifest carries the registry name; the record and receipt below
    # keep the grant's own name.
    assert issued.run_manifest["operation"] == "source_snapshot_capture"
    assert execution["consumption"]["consumption_id"] == receipt["reservation"]["consumption_id"]
    assert execution["reservation"] == receipt["reservation"]
    assert execution["authority_reference"] == f"42/daily/authority/{attempt}.json"
    assert execution["stage"] == "capture"
    assert execution["operation"] == "immutable_capture"
    assert execution["mode"] == "new_consume"
    assert execution["generation"] == f"generation:{attempt}"
    # The durable record keeps the plain consumption only; the objects stay in process.
    record = ledger.records[attempt]
    assert "execution" not in record
    assert record["reservation"] == receipt["reservation"]
    canonical_bytes(record)
    # Idempotent: the same objects on a second issue, from this issuer and from another
    # over the same ledger, with no second paid consumption.
    second = issuer.issue("capture", manifest("capture", grant()))
    assert second["execution"]["authority"] is issued
    assert second["execution"]["consumption"] is execution["consumption"]
    other = authority(reservation=reservation, ledger=ledger).issue(
        "capture", manifest("capture", grant())
    )
    assert other["execution"]["authority"] is issued
    assert other["execution"]["consumption"] is execution["consumption"]
    assert reservation.consumed == 1
    assert len(ledger.records) == 1
    # A record issued by an earlier process carries the durable reference alone.
    daily_authority._ISSUED_AUTHORITIES.pop(attempt)
    later = authority(reservation=reservation, ledger=ledger).issue(
        "capture", manifest("capture", grant())
    )
    assert later["execution"]["authority"] is None
    assert later["execution"]["consumption"] is None
    assert later["execution"]["generation"] is None
    assert later["execution"]["reservation"] == receipt["reservation"]
    assert later["execution"]["authority_reference"] == receipt["authority_reference"]
    assert {key: value for key, value in later.items() if key != "execution"} == {
        key: value for key, value in receipt.items() if key != "execution"
    }
    assert reservation.consumed == 1


def test_second_issue_for_same_attempt_and_digest_returns_same_receipt_without_new_reservation():
    reservation, ledger = FakeReservation(), FakeLedger()
    issuer = authority(reservation=reservation, ledger=ledger)
    first = issuer.issue("collect", manifest("collect", grant()))
    second = authority(reservation=reservation, ledger=ledger).issue(
        "collect", manifest("collect", grant())
    )
    assert second == first
    assert reservation.consumed == 1
    assert len(ledger.records) == 1


@pytest.mark.parametrize(
    ("current_lease", "code"),
    [
        ({"owner": "run-2", "epoch": 1}, "authority_takeover_reconciliation_required"),
        ({"owner": "run-1", "epoch": 2}, "authority_takeover_reconciliation_required"),
        (None, "authority_lease_lost"),
        ({"owner": "", "epoch": 1}, "authority_lease_lost"),
        ({"owner": "run-1", "epoch": True}, "authority_lease_lost"),
        ({"owner": "run-1", "epoch": 0}, "authority_lease_lost"),
        ({"owner": "run-1", "epoch": -1}, "authority_lease_lost"),
    ],
)
@pytest.mark.parametrize("clear_process_cache", [False, True])
def test_existing_record_refuses_a_lost_or_changed_lease_before_reissue(
    current_lease, code, clear_process_cache
):
    reservation, ledger = ObjectReservation(), FakeLedger()
    lease = {"value": {"owner": "run-1", "epoch": 1}}
    first = authority(
        reservation=reservation,
        ledger=ledger,
        fence=lambda: lease["value"],
    ).issue("capture", manifest("capture", grant()))
    lease["value"] = current_lease
    attempt = first["business_attempt_id"]
    initial_calls = list(reservation.calls)
    initial_record = dict(ledger.records[attempt])
    if clear_process_cache:
        daily_authority._ISSUED_AUTHORITIES.pop(attempt)

    with pytest.raises(AuthorityRefusal, match=rf"^{code}$"):
        authority(
            reservation=reservation,
            ledger=ledger,
            fence=lambda: lease["value"],
        ).issue("capture", manifest("capture", grant()))

    assert reservation.consumed == 1
    assert reservation.calls == initial_calls
    assert len(ledger.records) == 1
    assert ledger.records[attempt] == initial_record


@pytest.mark.parametrize(
    "lease",
    [
        None,
        {"owner": "", "epoch": 1},
        {"owner": "run-1", "epoch": True},
        {"owner": "run-1", "epoch": 0},
        {"owner": "run-1", "epoch": -1},
    ],
)
def test_invalid_lease_refuses_first_issue_before_reservation(lease):
    reservation, ledger = FakeReservation(), FakeLedger()

    with pytest.raises(AuthorityRefusal, match=r"^authority_lease_lost$"):
        authority(
            reservation=reservation,
            ledger=ledger,
            fence=lambda: lease,
        ).issue("collect", manifest("collect", grant()))

    assert reservation.calls == []
    assert reservation.consumed == 0
    assert ledger.records == {}


def test_death_after_issuance_before_pending_persistence_reruns_to_one_reservation():
    reservation, ledger = FakeReservation(), FakeLedger()
    handlers = {"collect": lambda **_kwargs: pytest.fail("collect must not run after death")}
    with pytest.raises(RuntimeError, match="worker died"):
        execute_daily_cycle(
            operation_id="slot-a",
            cutoff_utc=CUTOFF,
            profile=profile(grant()),
            authority=authority(reservation=reservation, ledger=ledger),
            store=FakeStore(die_on_record=1),
            stages=handlers,
        )
    assert reservation.consumed == 1
    assert len(ledger.records) == 1
    seen = []

    def collect(*, manifest, authority_receipt):
        seen.append(authority_receipt)
        digest = sha256(canonical_bytes(manifest)).hexdigest()
        return {
            "state": "failed",
            "input_digest": digest,
            "output_digest": "",
            "result_reference": "collect:failed",
            "retry_safe": False,
        }

    store = FakeStore()
    result = execute_daily_cycle(
        operation_id="slot-a",
        cutoff_utc=CUTOFF,
        profile=profile(grant()),
        authority=authority(reservation=reservation, ledger=ledger),
        store=store,
        stages={"collect": collect},
    )
    assert result["state"] == "failed"
    assert reservation.consumed == 1
    assert len(ledger.records) == 1
    assert seen[0]["business_attempt_id"] == business_attempt_id("slot-a", "collect")
    assert store.records[("slot-a", "collect")]["state"] == "failed"


def test_changed_profile_for_same_attempt_conflicts_instead_of_paying_again():
    reservation, ledger = FakeReservation(), FakeLedger()
    authority(reservation=reservation, ledger=ledger).issue("collect", manifest("collect", grant()))
    renewed = manifest("collect", grant(), freshness_target_hours=24)
    renewed_authority = authority(
        reservation=reservation,
        ledger=ledger,
        profile=profile(grant(), freshness_target_hours=24),
    )
    with pytest.raises(AuthorityRefusal, match=r"^authority_manifest_conflict$"):
        renewed_authority.issue("collect", renewed)
    assert reservation.consumed == 1


def test_release_stage_returns_none_without_human_or_delegated_release_authority():
    reservation, ledger = FakeReservation(), FakeLedger()
    issuer = authority(reservation=reservation, ledger=ledger)
    assert issuer.issue("release", manifest("release", grant(), "a" * 64)) is None
    assert reservation.calls == []
    assert ledger.records == {}


def test_release_stage_issues_under_an_approved_recurring_delegation():
    reservation, ledger = FakeReservation(), FakeLedger()
    issuer = authority(
        reservation=reservation,
        ledger=ledger,
        release_delegation=lambda: dict(DELEGATED_RELEASE),
    )
    receipt = issuer.issue("release", manifest("release", grant(), "a" * 64))
    assert receipt["operation"] == "release_evidence_qualified_staging_results"
    assert reservation.consumed == 1


def test_release_delegation_outside_the_grant_digest_is_refused():
    other = {**DELEGATED_RELEASE, "human_release_contract": "42_release_authority_v0"}
    issuer = authority(release_delegation=lambda: other)
    with pytest.raises(AuthorityRefusal, match=r"^authority_release_manifest_mismatch$"):
        issuer.issue("release", manifest("release", grant(), "a" * 64))


def test_undelegated_release_manifest_holds_release():
    undelegated = {**DELEGATED_RELEASE, "delegation": "none"}
    value = grant(approved_release_manifest_digest=sha256(canonical_bytes(undelegated)).hexdigest())
    issuer = authority(value, release_delegation=lambda: undelegated)
    assert issuer.issue("release", manifest("release", value, "a" * 64)) is None


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"now": lambda: datetime(2026, 10, 16, tzinfo=UTC)}, "recurring_grant_expired"),
        (
            {"principal": f"intelligence-42-migration@{DOMAIN}"},
            "recurring_grant_principal_mismatch",
        ),
        ({"image_digest": "sha256:" + "5" * 64}, "recurring_grant_image_forbidden"),
        ({"resource_manifest_digest": "a" * 64}, "recurring_grant_manifest_mismatch"),
    ],
)
def test_grant_refusals_stop_before_any_reservation(overrides, code):
    reservation, ledger = FakeReservation(), FakeLedger()
    issuer = authority(reservation=reservation, ledger=ledger, **overrides)
    with pytest.raises(GrantRefusal, match=rf"^{code}$"):
        issuer.issue("collect", manifest("collect", grant()))
    assert reservation.calls == []
    assert ledger.records == {}


def test_revoked_grant_refuses_next_operation_but_returns_known_in_flight_record():
    reservation, ledger = FakeReservation(), FakeLedger()
    authority(reservation=reservation, ledger=ledger).issue("collect", manifest("collect", grant()))
    revoked = grant(revocation_state="revoked")
    issuer = authority(
        revoked,
        reservation=reservation,
        ledger=ledger,
        profile=profile(grant()),
    )
    receipt = issuer.issue("collect", manifest("collect", grant()))
    assert receipt["business_attempt_id"] == business_attempt_id("slot-a", "collect")
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_revoked$"):
        issuer.issue("capture", manifest("capture", grant(), "a" * 64))
    assert reservation.consumed == 1


def test_profile_that_references_another_grant_is_refused():
    other = grant(grant_id="recurring_execution_20260915_v2")
    with pytest.raises(AuthorityRefusal, match=r"^authority_profile_grant_mismatch$"):
        authority(grant(), profile=profile(other))


def test_manifest_profile_must_match_the_authority_profile():
    issuer = authority()
    with pytest.raises(AuthorityRefusal, match=r"^authority_manifest_invalid$"):
        issuer.issue("collect", manifest("collect", grant(), max_catchup_cutoffs=5))
    with pytest.raises(AuthorityRefusal, match=r"^authority_manifest_invalid$"):
        issuer.issue("collect", {"operation_id": "slot-a", "stage": "collect"})
    with pytest.raises(AuthorityRefusal, match=r"^authority_manifest_invalid$"):
        issuer.issue("capture", manifest("collect", grant()))


def test_stale_owner_cannot_consume_after_takeover():
    reservation, ledger = FakeReservation(), FakeLedger()
    issuer = authority(reservation=reservation, ledger=ledger, fence=lambda: None)
    with pytest.raises(AuthorityRefusal, match=r"^authority_lease_lost$"):
        issuer.issue("collect", manifest("collect", grant()))
    assert reservation.calls == []
    assert ledger.records == {}


def test_reservation_conflict_is_unresolved_never_a_second_paid_call():
    reservation, ledger = FakeReservation(conflict=True), FakeLedger()
    issuer = authority(reservation=reservation, ledger=ledger)
    with pytest.raises(AuthorityRefusal, match=r"^authority_unresolved$"):
        issuer.issue("collect", manifest("collect", grant()))
    assert reservation.consumed == 0
    assert ledger.records == {}
    with pytest.raises(AuthorityRefusal, match=r"^authority_unresolved$"):
        authority(reservation=reservation, ledger=ledger).issue(
            "collect", manifest("collect", grant())
        )
    assert reservation.attempts == 2
    assert reservation.consumed == 0
    assert ledger.records == {}


def test_resume_manifest_needs_explicit_resume_authority():
    resume = {
        "slot_id": "slot-a",
        "cutoff_utc": CUTOFF,
        "resume_authority_digest": "9" * 64,
        "attempt_version": 2,
        "unit_ledger": {},
        "resume_units": ["ng_rss"],
        "amended_permits": {},
    }
    assert authority().issue("resume", resume) is None
    approved = authority(resume_authority=lambda: {"resume_authority_digest": "9" * 64})
    receipt = approved.issue("resume", resume)
    assert receipt["resume_authority_digest"] == "9" * 64
    assert receipt["business_attempt_id"] == business_attempt_id("slot-a", "resume")


def test_release_hold_keys_on_the_operation_for_unit_manifests():
    reservation = FakeReservation()
    issuer = authority(reservation=reservation)
    unit_manifest = {
        "slot_id": "slot-a",
        "unit_id": "rel_unit",
        "stage": "release",
        "attempt_version": 2,
        "cutoff_utc": CUTOFF.isoformat(),
        "resume_authority_digest": "9" * 64,
        "permit_digest": "8" * 64,
        "original_result": {
            "state": "failed",
            "input_digest": "7" * 64,
            "output_digest": "",
            "result_reference": "rel:failed",
            "retry_safe": True,
        },
    }
    assert issuer.issue("rel_unit", unit_manifest) is None
    assert reservation.calls == []
    delegated = authority(
        reservation=reservation, release_delegation=lambda: dict(DELEGATED_RELEASE)
    )
    receipt = delegated.issue("rel_unit", unit_manifest)
    assert receipt["operation"] == "release_evidence_qualified_staging_results"
    assert reservation.consumed == 1


def test_unit_issue_uses_the_unit_stage_operation():
    reservation = FakeReservation()
    issuer = authority(reservation=reservation)
    unit_manifest = {
        "slot_id": "slot-a",
        "unit_id": "ng_rss",
        "stage": "collect",
        "attempt_version": 2,
        "cutoff_utc": CUTOFF.isoformat(),
        "resume_authority_digest": "9" * 64,
        "permit_digest": "8" * 64,
        "original_result": {
            "state": "failed",
            "input_digest": "7" * 64,
            "output_digest": "",
            "result_reference": "ng:failed",
            "retry_safe": True,
        },
    }
    receipt = issuer.issue("ng_rss", unit_manifest)
    assert receipt["operation"] == "collection"
    assert receipt["business_attempt_id"] == business_attempt_id("slot-a", "ng_rss@2")
    assert reservation.consumed == 1


REGISTRY_OPERATIONS = (
    "collection_exposure_issue",
    "source_snapshot_capture",
    "r3_apply",
    "r3_proof_issue",
    "r3_release",
)


class RegistryReservation(FakeReservation):
    """A reservation path that, like the engine's, reserves only under a registry name."""

    def reserve(self, invocation, run_manifest):
        if run_manifest["operation"] not in REGISTRY_OPERATIONS:
            raise ValueError("execution_approval_manifest_invalid")
        self.calls.append(("reserve", run_manifest["operation"]))
        return {"authority": run_manifest["business_attempt_id"], "run_manifest": run_manifest}


@pytest.mark.parametrize(
    ("stage", "registry_operation"),
    [
        ("collect", "collection_exposure_issue"),
        ("capture", "source_snapshot_capture"),
        ("compose", "r3_apply"),
        ("certify", "r3_proof_issue"),
    ],
)
def test_kernel_stage_reserves_under_the_registry_name_and_records_the_grant_name(
    stage, registry_operation
):
    reservation, ledger = RegistryReservation(), FakeLedger()
    issuer = authority(reservation=reservation, ledger=ledger)
    receipt = issuer.issue(stage, manifest(stage, grant()))
    assert reservation.calls[0] == ("reserve", registry_operation)
    assert reservation.consumed == 1
    assert receipt["operation"] == STAGE_OPERATIONS[stage]
    assert receipt["execution"]["operation"] == STAGE_OPERATIONS[stage]
    assert ledger.records[receipt["business_attempt_id"]]["operation"] == STAGE_OPERATIONS[stage]
    assert receipt["operation"] in ALLOWED_OPERATIONS
    assert registry_operation not in ALLOWED_OPERATIONS


def test_delegated_release_reserves_under_the_registry_name():
    reservation, ledger = RegistryReservation(), FakeLedger()
    issuer = authority(
        reservation=reservation,
        ledger=ledger,
        release_delegation=lambda: dict(DELEGATED_RELEASE),
    )
    receipt = issuer.issue("release", manifest("release", grant(), "a" * 64))
    assert reservation.calls[0] == ("reserve", "r3_release")
    assert receipt["operation"] == "release_evidence_qualified_staging_results"


def test_unit_attempt_reserves_under_the_registry_name_of_its_stage():
    reservation = RegistryReservation()
    issuer = authority(reservation=reservation)
    unit_manifest = {
        "slot_id": "slot-a",
        "unit_id": "ng_rss",
        "stage": "collect",
        "attempt_version": 2,
        "cutoff_utc": CUTOFF.isoformat(),
        "resume_authority_digest": "9" * 64,
        "permit_digest": "8" * 64,
        "original_result": {
            "state": "failed",
            "input_digest": "7" * 64,
            "output_digest": "",
            "result_reference": "ng:failed",
            "retry_safe": True,
        },
    }
    receipt = issuer.issue("ng_rss", unit_manifest)
    assert reservation.calls[0] == ("reserve", "collection_exposure_issue")
    assert receipt["operation"] == "collection"


def test_the_grant_check_sees_the_grant_name_before_any_reservation():
    reservation = RegistryReservation()
    narrowed = grant(allowed_operations=["immutable_capture"])
    issuer = authority(grant=narrowed, profile=profile(narrowed), reservation=reservation)
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_operation_forbidden$"):
        issuer.issue("collect", manifest("collect", narrowed))
    assert reservation.calls == []


def test_the_reserve_reads_the_one_map_the_grant_module_carries():
    assert daily_authority.V1_REGISTRY_OPERATIONS is recurring_grant.V1_REGISTRY_OPERATIONS
    assert tuple(daily_authority.V1_REGISTRY_OPERATIONS) == ALLOWED_OPERATIONS
    assert tuple(daily_authority.V1_REGISTRY_OPERATIONS.values()) == REGISTRY_OPERATIONS
