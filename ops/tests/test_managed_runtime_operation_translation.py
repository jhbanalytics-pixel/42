"""The daily runner binds a v1 grant's operations to the origin registry by translation.

A v1 recurring grant names its operations in the grant vocabulary
(``recurring_grant.ALLOWED_OPERATIONS``). The origin registry's daily row binds the
same five stages under registry names. One pinned map beside the grant vocabulary,
``recurring_grant.V1_REGISTRY_OPERATIONS``, is the only place the two meet: the
runner translates each grant name through it before asking the authority to describe
it, and the daily authority translates the stage's grant name through the same map
when it reserves, so describe and reserve ask the registry for the same name.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.runners import managed_runtime
from ops.runners.managed_runtime import run_managed_mode
from ops.tests.test_foundation_scheduler import (
    FakeObjectClient,
    FakeReservation,
    _build,
    _engine,
    _grant,
    _succeeding_handlers,
    _wiring,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "managed_runtime"
DAILY_ROW_REGISTRY = FIXTURES / "execution_origins_daily_row.json"
DAILY_JOB = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
)


class RegistryReservation(FakeReservation):
    """An authority whose describe and reserve know only the registry names of one row.

    The engine's reserve resolves the run manifest's operation by registry binding
    name, so a manifest carrying a name outside the row refuses before anything is
    paid; this fake refuses the same way instead of accepting any manifest.
    """

    def __init__(self, bound):
        super().__init__()
        self.bound = set(bound)

    def describe(self, operation):
        self.calls.append(("describe", operation))
        if operation not in self.bound:
            return None
        return {"operation": operation, "contract_sha256": "7" * 64}

    def reserve(self, invocation, run_manifest):
        if run_manifest["operation"] not in self.bound:
            raise ValueError("execution_approval_manifest_invalid")
        self.calls.append(("reserve", run_manifest["operation"]))
        return {"run_manifest": run_manifest}

    @property
    def reserved(self):
        return [name for kind, name in self.calls if kind == "reserve"]

    @property
    def described(self):
        return [name for kind, name in self.calls if kind == "describe"]


def _daily_row_operations():
    registry = json.loads(DAILY_ROW_REGISTRY.read_text(encoding="utf-8"))
    rows = [
        row
        for row in registry["rows"]
        if row["manifest_version"] == "open_intelligence_execution_manifest_v2"
        and row["job_resources"] == [DAILY_JOB]
    ]
    assert len(rows) == 1
    bindings = rows[0]["exact_operation_bindings"]
    assert all(binding["job_resource"] == DAILY_JOB for binding in bindings.values())
    return bindings


def test_v1_grant_binds_to_the_registry_names_the_daily_row_carries():
    engine = _engine()
    grant = _grant(engine)
    reservation = RegistryReservation(bound=_daily_row_operations())
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    checked = engine.recurring_grant.validate_recurring_grant(grant)
    assert checked["allowed_operations"] == list(
        engine.recurring_grant.ALLOWED_OPERATIONS
    )

    receipt = run_managed_mode(_build(config), authority=reservation, daily=wiring)

    assert receipt["status"] == "release_pending"
    assert receipt["stages_dispatched"] == ["collect", "capture", "compose", "certify"]
    assert reservation.described == [
        "collection_exposure_issue",
        "source_snapshot_capture",
        "r3_apply",
        "r3_proof_issue",
        "r3_release",
    ]
    assert not set(reservation.described) & set(engine.recurring_grant.ALLOWED_OPERATIONS)
    assert reservation.reserved == [
        "collection_exposure_issue",
        "source_snapshot_capture",
        "r3_apply",
        "r3_proof_issue",
    ]
    assert reservation.consumed == 4


def test_the_collect_stage_reserves_under_the_registry_name_describe_accepted():
    engine = _engine()
    grant = _grant(engine)
    reservation = RegistryReservation(bound=_daily_row_operations())
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )

    receipt = run_managed_mode(_build(config), authority=reservation, daily=wiring)

    assert receipt["stages_dispatched"][0] == "collect"
    assert reservation.reserved[0] == "collection_exposure_issue"
    assert reservation.reserved[0] in reservation.described
    assert reservation.reserved[0] not in engine.recurring_grant.ALLOWED_OPERATIONS
    assert seen[0][0] == "collect"


def test_the_describe_loop_and_the_reserve_read_the_same_map():
    engine = _engine()
    shared = engine.recurring_grant.V1_REGISTRY_OPERATIONS
    assert engine.daily_authority.V1_REGISTRY_OPERATIONS is shared
    assert not hasattr(managed_runtime, "V1_REGISTRY_OPERATIONS")
    grant = _grant(engine)
    reservation = RegistryReservation(bound=_daily_row_operations())
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    run_managed_mode(_build(config), authority=reservation, daily=wiring)
    stage_operations = engine.recurring_grant.STAGE_OPERATIONS
    assert reservation.described == [shared[name] for name in grant["allowed_operations"]]
    assert reservation.reserved == [
        shared[stage_operations[stage]] for stage, _attempt in seen
    ]
    assert set(reservation.reserved) <= set(reservation.described)


def test_the_describe_loop_follows_the_map_the_grant_module_carries():
    engine = _engine()
    grant = _grant(engine)
    real = engine.recurring_grant
    renamed = {name: "renamed_" + value for name, value in real.V1_REGISTRY_OPERATIONS.items()}
    renamed_engine = SimpleNamespace(
        daily_cycle=engine.daily_cycle,
        daily_authority=engine.daily_authority,
        daily_store=engine.daily_store,
        daily_native_clients=engine.daily_native_clients,
        execution_generations=engine.execution_generations,
        recurring_grant=SimpleNamespace(
            grant_digest=real.grant_digest,
            validate_recurring_grant=real.validate_recurring_grant,
            ALLOWED_OPERATIONS=real.ALLOWED_OPERATIONS,
            V1_REGISTRY_OPERATIONS=renamed,
        ),
    )
    reservation = RegistryReservation(bound=_daily_row_operations())
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        renamed_engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    with pytest.raises(ValueError, match="^operation_unbound$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.described == ["renamed_collection_exposure_issue"]
    assert reservation.paid_calls == []


def test_a_grant_name_outside_the_map_refuses_operation_unbound_before_any_claim():
    engine = _engine()
    grant = _grant(engine)
    real = engine.recurring_grant

    def validate_with_stray(payload):
        checked = real.validate_recurring_grant(payload)
        return {
            **checked,
            "allowed_operations": [*checked["allowed_operations"], "brain_read"],
        }

    stray_engine = SimpleNamespace(
        daily_cycle=engine.daily_cycle,
        daily_authority=engine.daily_authority,
        daily_store=engine.daily_store,
        daily_native_clients=engine.daily_native_clients,
        execution_generations=engine.execution_generations,
        recurring_grant=SimpleNamespace(
            grant_digest=real.grant_digest,
            validate_recurring_grant=validate_with_stray,
            ALLOWED_OPERATIONS=real.ALLOWED_OPERATIONS,
            V1_REGISTRY_OPERATIONS=real.V1_REGISTRY_OPERATIONS,
        ),
    )
    bound = {*_daily_row_operations(), "brain_read"}
    reservation = RegistryReservation(bound=bound)
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        stray_engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    with pytest.raises(ValueError, match="^operation_unbound$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    described = [name for kind, name in reservation.calls if kind == "describe"]
    assert described == [
        "collection_exposure_issue",
        "source_snapshot_capture",
        "r3_apply",
        "r3_proof_issue",
        "r3_release",
    ]
    assert reservation.paid_calls == []
    assert client.writes == []
    assert seen == []


def test_a_registry_name_the_authority_cannot_describe_refuses_operation_unbound():
    engine = _engine()
    grant = _grant(engine)
    bound = set(_daily_row_operations()) - {"r3_release"}
    reservation = RegistryReservation(bound=bound)
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    with pytest.raises(ValueError, match="^operation_unbound$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert ("describe", "r3_release") in reservation.calls
    assert reservation.paid_calls == []
    assert client.writes == []
    assert seen == []


def test_the_map_covers_exactly_the_grant_vocabulary():
    engine = _engine()
    allowed = engine.recurring_grant.ALLOWED_OPERATIONS
    registry_operations = engine.recurring_grant.V1_REGISTRY_OPERATIONS
    assert tuple(registry_operations) == allowed
    assert set(registry_operations) == set(allowed)
    assert len(registry_operations) == len(allowed)


def test_the_map_targets_are_the_operations_the_daily_row_binds():
    registry_operations = _engine().recurring_grant.V1_REGISTRY_OPERATIONS
    bindings = _daily_row_operations()
    assert set(registry_operations.values()) == set(bindings)
    assert len(set(registry_operations.values())) == len(registry_operations)
    assert dict(registry_operations) == {
        "collection": "collection_exposure_issue",
        "immutable_capture": "source_snapshot_capture",
        "composition": "r3_apply",
        "quality_proof_issuance": "r3_proof_issue",
        "release_evidence_qualified_staging_results": "r3_release",
    }
