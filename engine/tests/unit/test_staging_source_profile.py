"""Boundary tests for the versioned staging source profile and the capture policy closure."""

from __future__ import annotations

import copy
import inspect
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from src.analysis.open_intelligence import production_snapshot_capture
from src.analysis.open_intelligence import staging_source_profile as subject
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.capture_registry import (
    select_latest_capture,
    validate_capture_entry,
)

ROOT = Path(__file__).resolve().parents[2]
CUTOFF = date(2026, 9, 12)
WINDOW_END = datetime(2026, 9, 13, tzinfo=UTC)
STARTED = WINDOW_END + timedelta(minutes=30)
COMPLETED = WINDOW_END + timedelta(hours=1)
SNAPSHOT = COMPLETED + timedelta(minutes=5)
NOW = SNAPSHOT + timedelta(minutes=10)
POLICY_DIGEST = "3" * 64
GRANT_VALID_FROM = datetime(2026, 9, 1, tzinfo=UTC)
GRANT_VALID_UNTIL = datetime(2026, 10, 1, tzinfo=UTC)


def receipt(**overrides):
    base = {
        "contract_version": "collection_receipt_v1",
        "run_id": "run-2026-09-12-a",
        "execution_id": "exec-1",
        "source_sha": "a" * 40,
        "image_uri": "region-docker.pkg.dev/project/repo/image@sha256:" + "b" * 64,
        "policy_sha256": POLICY_DIGEST,
        "profile_sha256": "6" * 64,
        "cutoff": CUTOFF.isoformat(),
        "market_states": {"za": "collected", "ng": "collected", "ke": "collected"},
        "raw_rows_persisted": 10,
        "enriched_rows_persisted": 5,
        "funded_close": None,
        "complete": True,
    }
    base.update(overrides)
    return base


def run(**overrides):
    base = {
        "receipt": receipt(),
        "collection_started_at": STARTED,
        "collection_completed_at": COMPLETED,
    }
    base.update(overrides)
    return base


def profile(**overrides):
    arguments = {
        "snapshot_as_of": SNAPSHOT,
        "scope": "scope-42",
        "schema_digest": "2" * 64,
        "source_digest": "4" * 64,
        "now": NOW,
    }
    arguments.update(overrides)
    return subject.build_staging_source_profile(run(), **arguments)


def measured(**overrides):
    base = {
        "observed_at": NOW - timedelta(hours=1),
        "pricing_sources": [
            "https://cloud.google.com/storage/pricing",
            "https://cloud.google.com/bigquery/pricing",
        ],
        "storage_micro_usd_per_gib_month": 20000,
        "query_micro_usd_per_tib": 6250000,
        "operations_micro_usd_per_10k": 50000,
        "source_logical_bytes": 5 * 2**30,
        "max_bytes_billed_per_query": 5 * 2**30,
        "queries_per_cycle": 5,
        "jobs_per_cycle": 5,
        "retention_days": 90,
        "operations_per_cycle": 40,
        "permitted_retries": 1,
        "cadence_cycles_per_day": 1,
    }
    base.update(overrides)
    return base


def grant(**overrides):
    base = {
        "grant_id": "source_capture_grant_2026_09_v2",
        "environment": "staging",
        "source_estate_digest": "7" * 64,
        "contract_sha256": "8" * 64,
        "valid_from": GRANT_VALID_FROM,
        "valid_until": GRANT_VALID_UNTIL,
        "allowed_cutoffs": ["2026-09-12", "2026-09-13", "2026-09-14"],
        "reserved_micro_usd_per_capture": 750000,
        "cumulative_ceiling_micro_usd": 2250000,
        "revocation_state": "active",
    }
    base.update(overrides)
    return base


# Profile shape


def test_profile_reads_the_completed_run_at_its_exact_snapshot_instant():
    built = profile()
    assert built.profile_version == subject.NATIVE_IDENTITY_PROFILE_VERSION
    assert built.projection_version == "native_id_bound_v1"
    assert built.source_kind == "staging_source_run"
    assert built.source_dataset == "intelligence_42_sources_staging"
    assert built.snapshot_as_of == SNAPSHOT
    assert built.observation_window_end == WINDOW_END
    assert built.collection_started_at == STARTED
    assert built.collection_completed_at == COMPLETED
    assert built.markets == ("ke", "ng", "za")
    assert built.source_tables == (
        "enriched_content",
        "event_ledger",
        "raw_content",
        "seed_candidates",
        "seed_graph",
    )
    # The tables the capture that executes creates, as the plan module names them, so the
    # entry a capture registers declares tables that exist rather than tables of a plan
    # that cannot run today.
    assert built.snapshot_tables == tuple(
        f"open_intelligence_v3_source_20260912_{lane}" for lane in built.source_tables
    )
    assert built.policy_digest == POLICY_DIGEST
    assert built.run_id == "run-2026-09-12-a"
    assert built.instants() == {
        "observation_window_end": WINDOW_END,
        "collection_started_at": STARTED,
        "collection_completed_at": COMPLETED,
        "snapshot_as_of": SNAPSHOT,
    }


def test_snapshot_instant_must_contain_the_runs_writes_and_may_not_be_future():
    with pytest.raises(ValueError, match="source_run_snapshot_precedes_writes"):
        profile(snapshot_as_of=COMPLETED - timedelta(seconds=1))
    with pytest.raises(ValueError, match="snapshot_instant_future"):
        profile(snapshot_as_of=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="snapshot_instant_invalid"):
        profile(snapshot_as_of=SNAPSHOT.replace(tzinfo=None))
    assert profile(snapshot_as_of=COMPLETED).snapshot_as_of == COMPLETED


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"receipt": receipt(complete=False)}, "source_run_incomplete"),
        (
            {
                "receipt": receipt(
                    market_states={"za": "collected", "ng": "partial", "ke": "collected"}
                )
            },
            "source_run_incomplete",
        ),
        ({"receipt": receipt(contract_version="collection_receipt_v0")}, "source_run_invalid"),
        ({"receipt": receipt(cutoff="2026-9-12")}, "source_run_invalid"),
        ({"receipt": receipt(policy_sha256="xyz")}, "source_run_invalid"),
        ({"receipt": "not a receipt"}, "source_run_invalid"),
        ({"collection_started_at": WINDOW_END - timedelta(seconds=1)}, "source_run_window_open"),
        ({"collection_completed_at": STARTED - timedelta(seconds=1)}, "source_run_invalid"),
        ({"collection_started_at": STARTED.replace(tzinfo=None)}, "source_run_invalid"),
        ({"extra": 1}, "source_run_invalid"),
    ],
)
def test_incomplete_open_or_foreign_runs_are_refused(mutation, code):
    with pytest.raises(ValueError, match=code):
        subject.validate_completed_source_run(run(**mutation))


def test_same_cutoff_with_a_different_policy_digest_is_a_different_profile():
    first = profile()
    same = profile()
    other = subject.build_staging_source_profile(
        run(receipt=receipt(policy_sha256="9" * 64)),
        snapshot_as_of=SNAPSHOT,
        scope="scope-42",
        schema_digest="2" * 64,
        source_digest="4" * 64,
        now=NOW,
    )
    assert first.profile_id == same.profile_id
    assert first.profile_id != other.profile_id
    assert first.observation_window_end == other.observation_window_end
    assert first.profile_id.startswith("staging-2026-09-12-")


def test_the_profile_id_names_the_cutoff_and_the_policy_prefix():
    assert (
        subject.staging_profile_id(date(2026, 9, 11), "ab" * 32) == "staging-2026-09-11-" + "ab" * 8
    )


@pytest.mark.parametrize(
    ("cutoff", "policy"),
    [
        (datetime(2026, 9, 11, tzinfo=UTC), "a" * 64),
        ("2026-09-11", "a" * 64),
        (date(2026, 9, 11), "A" * 64),
        (date(2026, 9, 11), "a" * 63),
        (date(2026, 9, 11), None),
    ],
    ids=["datetime", "text", "uppercase", "short", "none"],
)
def test_the_profile_id_refuses_anything_but_a_date_and_a_digest(cutoff, policy):
    with pytest.raises(ValueError, match="profile_input_invalid"):
        subject.staging_profile_id(cutoff, policy)


def test_capture_entry_binds_the_profile_and_is_selectable_only_when_succeeded():
    built = profile()
    available = SNAPSHOT + timedelta(minutes=2)
    entry = built.capture_entry(
        operation_id="op-1",
        result_id="result-1",
        content_digest="1" * 64,
        image_digest="5" * 64,
        generation="10",
        completion_state="succeeded",
        capture_available_at=available,
    )
    assert entry == validate_capture_entry(entry)
    assert entry["profile_id"] == built.profile_id
    assert entry["profile_version"] == subject.NATIVE_IDENTITY_PROFILE_VERSION
    assert entry["policy_digest"] == POLICY_DIGEST
    assert entry["observation_window_end"] == WINDOW_END
    assert entry["snapshot_as_of"] == SNAPSHOT
    assert entry["capture_available_at"] == available
    assert entry["source_tables"] == built.source_tables
    assert entry["snapshot_tables"] == built.snapshot_tables
    assert select_latest_capture([entry], now=NOW, scope="scope-42") == entry
    partial = built.capture_entry(
        operation_id="op-2",
        result_id="result-2",
        content_digest="1" * 64,
        image_digest="5" * 64,
        generation="11",
        completion_state="partial",
        capture_available_at=available + timedelta(minutes=1),
    )
    assert select_latest_capture([entry, partial], now=NOW, scope="scope-42") == entry
    with pytest.raises(ValueError, match="capture_instants_unordered"):
        built.capture_entry(
            operation_id="op-3",
            result_id="result-3",
            content_digest="1" * 64,
            image_digest="5" * 64,
            generation="12",
            completion_state="succeeded",
            capture_available_at=SNAPSHOT - timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ({"status": "access_denied"}, "source_run_read_access_denied"),
        ({"status": "metadata_unavailable"}, "source_run_read_metadata_unavailable"),
        ({"status": "invalid_authority"}, "source_run_read_authority_invalid"),
        ({"status": "ok"}, "source_run_read_invalid"),
        ({"status": "ok", "run": None}, "source_run_read_invalid"),
        ("ok", "source_run_read_invalid"),
        ([], "source_run_read_invalid"),
    ],
)
def test_run_reader_outcomes_are_distinct_refusals_never_an_empty_run(outcome, code):
    with pytest.raises(ValueError, match=code):
        subject.read_completed_source_run(lambda run_id: outcome, "run-2026-09-12-a")


def test_run_reader_returns_the_validated_run_and_must_be_a_callable_reader():
    seen = []

    def reader(run_id):
        seen.append(run_id)
        return {"status": "ok", "run": run()}

    value = subject.read_completed_source_run(reader, "run-2026-09-12-a")
    assert seen == ["run-2026-09-12-a"]
    assert value["observation_window_end"] == WINDOW_END
    assert value["receipt"]["run_id"] == "run-2026-09-12-a"
    with pytest.raises(ValueError, match="source_run_reader_invalid"):
        subject.read_completed_source_run(object(), "run-2026-09-12-a")
    with pytest.raises(ValueError, match="source_run_invalid"):
        subject.read_completed_source_run(reader, "run-other")


# Legacy dispatch preserved


def test_legacy_profile_keeps_its_projection_dataset_and_reader_bytes(monkeypatch):
    from tests.unit import test_protected_snapshot_replay_integration as replay_fixture

    legacy = subject.LEGACY_PROFILE
    assert legacy.profile_version == "42_capture_v1"
    assert legacy.projection_version == "v3_absent_nullable_v1"
    from src.analysis.open_intelligence import production_snapshot_tables as tables

    assert legacy.source_dataset == tables.SOURCE_DATASET == "trends_v2_dev"
    assert legacy.snapshot_dataset == tables.DESTINATION_DATASET == "trends_v2_staging"
    assert "trends_v2_staging" not in inspect.getsource(subject)
    assert "trends_v2_dev" not in inspect.getsource(subject)
    assert subject.LEGACY_PROFILE is subject.profile_for_version("42_capture_v1")
    assert profile().source_dataset != legacy.source_dataset
    source = inspect.getsource(production_snapshot_capture._query)
    assert "trends_v2_staging" in source
    module_source = inspect.getsource(subject)
    assert "_query(" not in module_source
    assert "capture_production_snapshot" not in module_source

    _http, _storage, _ledger, inputs = replay_fixture.completed_capture(monkeypatch)
    monkeypatch.setattr(
        production_snapshot_capture,
        "_utc_now",
        lambda: replay_fixture.capture_fixture.OPERATION_NOW + timedelta(days=365),
    )

    def legacy_read():
        value = production_snapshot_capture._read_protected_capture(
            **{
                name: inputs[name]
                for name in (
                    "manifest_sha256",
                    "consumption_id",
                    "result_id",
                    "result_digest",
                    "objects",
                )
            },
            client_scope_id="ogilvy_default",
            market_scope=("ke", "ng", "za"),
        )
        return canonical_bytes(value)

    before = legacy_read()
    built = profile()
    built.capture_entry(
        operation_id="op-1",
        result_id="result-1",
        content_digest="1" * 64,
        image_digest="5" * 64,
        generation="10",
        completion_state="succeeded",
        capture_available_at=SNAPSHOT + timedelta(minutes=1),
    )
    assert legacy_read() == before
    assert json.loads(before)["capture"]["assembly"]["snapshot"]["source_as_of"].endswith("+00:00")


# Capture policy closure


def test_accepted_legacy_policy_matches_the_operator_and_the_v1_routine_literals():
    from scripts.staging import capture_protected_production_snapshot as protected

    accepted = subject.accepted_capture_policy(subject.LEGACY_CAPTURE_POLICY_VERSION)
    assert accepted["storage_policy_constants"] == protected._POLICY_CONSTANTS
    assert accepted["contract_sha256"] == protected._CONTRACT_SHA256
    assert not hasattr(protected, "_ALLOWED_CUTOFFS")
    from tests.unit import test_protected_production_snapshot as input_fixture

    # The Python gate admits only pinned registry cutoffs; the retained v1 routine keeps
    # its own literal allowance, and the gate never admits a cutoff the routine refuses.
    assert protected._policy_bindings(input_fixture.artifacts()) == (
        accepted["contract_sha256"],
        ("2026-09-07",),
    )
    assert accepted["allowed_cutoffs"] == ("2026-09-07",)
    assert accepted["maximum_cycle_cost_bound_micro_usd"] == 500000
    assert accepted["reservation"] == {
        "kind": "lifetime",
        "reserved_micro_usd_per_initial": 500000,
        "ceiling_micro_usd": 1000000,
    }
    assert accepted["routine"] == "sp_consume_open_intelligence_source_snapshot_v1"
    sql = (ROOT / "infra" / "bigquery_routines" / f"{accepted['routine']}.sql").read_text(
        encoding="utf-8"
    )
    constants = accepted["storage_policy_constants"]
    assert f"= '{constants['allowance_id']}'" in sql
    assert f"= '{constants['contract_version']}'" in sql
    assert f"= '{constants['reserved_micro_usd_per_cutoff']}'" in sql
    assert f"= '{accepted['contract_sha256']}'" in sql
    routine_cutoffs = ("2026-09-07", "2026-09-08")
    cutoffs = ", ".join(f"'{cutoff}'" for cutoff in routine_cutoffs)
    assert f"v_cutoff IN ({cutoffs})" in sql
    assert set(accepted["allowed_cutoffs"]) <= set(routine_cutoffs)
    assert "SET v_reserved_micro_usd = 500000 * (" in sql
    assert "v_reserved_micro_usd + 500000 <= 1000000" in sql
    assert accepted["assertions"]["initial"] == {
        "initial_count": 0,
        "recovery_count": 0,
        "recovery_context": "null",
    }
    assert accepted["assertions"]["recover"]["open_intelligence_source_capture_recovery_v3"] == {
        "initial_count": 1,
        "recovery_count": 1,
    }
    accepted["storage_policy_constants"]["bucket"] = "other"
    assert subject.accepted_capture_policy(subject.LEGACY_CAPTURE_POLICY_VERSION) != accepted
    with pytest.raises(ValueError, match="capture_policy_version_unknown"):
        subject.accepted_capture_policy("open_intelligence_source_capture_storage_v0")


def test_measured_price_inputs_model_the_cycle_and_rewrite_the_assumptions():
    record = subject.validate_measured_price_inputs(measured(), now=NOW)
    cost = subject.model_capture_cycle_cost(record)
    attempts = 2
    assert cost["query_micro_usd"] == 5 * attempts * 5 * 2**30 * 6250000 // 2**40
    assert cost["storage_micro_usd"] == -(-(5 * 2**30 * 90 * 20000) // (2**30 * 30))
    assert cost["operations_micro_usd"] == 40 * attempts * 50000 // 10000
    assert cost["maximum_cycle_cost_micro_usd"] == sum(
        cost[name] for name in ("query_micro_usd", "storage_micro_usd", "operations_micro_usd")
    )
    review = subject.price_review_from_measured(record)
    assert set(review) == {
        "reviewed_at",
        "expires_at",
        "pricing_sources",
        "assumptions",
        "maximum_cycle_cost_micro_usd",
    }
    assert review["reviewed_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert review["expires_at"] == (NOW + timedelta(hours=23)).isoformat()
    assert review["pricing_sources"] == sorted(measured()["pricing_sources"])
    assert review["assumptions"] == sorted(set(review["assumptions"]))
    for field in (
        "source_logical_bytes=5368709120",
        "max_bytes_billed_per_query=5368709120",
        "queries_per_cycle=5",
        "jobs_per_cycle=5",
        "retention_days=90",
        "operations_per_cycle=40",
        "permitted_retries=1",
        "cadence_cycles_per_day=1",
    ):
        assert any(item.startswith(field) for item in review["assumptions"]), field
    assert not any(
        "two invocations" in item or "one-time" in item for item in review["assumptions"]
    )
    assert review["maximum_cycle_cost_micro_usd"] == cost["maximum_cycle_cost_micro_usd"]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"observed_at": NOW - timedelta(hours=24, seconds=1)}, "price_observation_stale"),
        ({"observed_at": NOW + timedelta(seconds=1)}, "price_observation_future"),
        ({"observed_at": (NOW - timedelta(hours=1)).replace(tzinfo=None)}, "price_inputs_invalid"),
        ({"pricing_sources": ["http://cloud.google.com/pricing"]}, "price_inputs_invalid"),
        ({"pricing_sources": []}, "price_inputs_invalid"),
        ({"source_logical_bytes": -1}, "price_inputs_invalid"),
        ({"source_logical_bytes": 1.5}, "price_inputs_invalid"),
        ({"permitted_retries": True}, "price_inputs_invalid"),
        ({"cadence_cycles_per_day": 0}, "price_inputs_invalid"),
        ({"queries_per_cycle": 0}, "price_inputs_invalid"),
        ({"unexpected": 1}, "price_inputs_invalid"),
    ],
)
def test_stale_future_and_malformed_price_inputs_are_refused(mutation, code):
    record = measured()
    record.update(mutation)
    with pytest.raises(ValueError, match=code):
        subject.validate_measured_price_inputs(record, now=NOW)


def test_legacy_policy_artifact_keeps_exact_constants_and_the_old_bound():
    record = measured(source_logical_bytes=2**30, max_bytes_billed_per_query=2**30)
    artifact = subject.build_capture_policy_artifact(
        subject.LEGACY_CAPTURE_POLICY_VERSION, price_inputs=record, now=NOW
    )
    accepted = subject.accepted_capture_policy(subject.LEGACY_CAPTURE_POLICY_VERSION)
    assert {name: artifact[name] for name in accepted["storage_policy_constants"]} == accepted[
        "storage_policy_constants"
    ]
    assert set(artifact) == set(accepted["storage_policy_constants"]) | {"price_review"}
    assert artifact["price_review"]["maximum_cycle_cost_micro_usd"] <= 500000
    canonical_bytes(artifact)
    subject.validate_capture_policy_artifact(artifact, now=NOW)
    with pytest.raises(ValueError, match="snapshot_price_review_invalid"):
        subject.build_capture_policy_artifact(
            subject.LEGACY_CAPTURE_POLICY_VERSION, price_inputs=measured(), now=NOW
        )


def test_grant_policy_accepts_a_reservation_above_the_old_bound_and_refuses_over_budget():
    from scripts.staging import capture_protected_production_snapshot as protected

    record = measured()
    assert (
        subject.model_capture_cycle_cost(subject.validate_measured_price_inputs(record, now=NOW))[
            "maximum_cycle_cost_micro_usd"
        ]
        > 500000
    )
    artifact = subject.build_capture_policy_artifact(
        subject.GRANT_CAPTURE_POLICY_VERSION, grant=grant(), price_inputs=record, now=NOW
    )
    assert artifact["contract_version"] == "open_intelligence_source_capture_storage_v2"
    assert artifact["allowance_id"] == "source_capture_grant_2026_09_v2"
    assert artifact["reserved_micro_usd_per_cutoff"] == 750000
    assert artifact["grant"]["allowed_cutoffs"] == ["2026-09-12", "2026-09-13", "2026-09-14"]
    assert artifact["grant"]["cumulative_ceiling_micro_usd"] == 2250000
    assert artifact["grant"]["contract_sha256"] == "8" * 64
    assert artifact["grant"]["valid_from"] == GRANT_VALID_FROM.isoformat()
    assert artifact["grant"]["revocation_state"] == "active"
    assert artifact["price_review"]["maximum_cycle_cost_micro_usd"] > 500000
    assert artifact["price_review"]["maximum_cycle_cost_micro_usd"] <= 750000 * 9 // 10
    canonical_bytes(artifact)
    subject.validate_capture_policy_artifact(artifact, now=NOW)
    protected._validate_policy(artifact, NOW)
    over = copy.deepcopy(artifact)
    over["price_review"]["maximum_cycle_cost_micro_usd"] = 750000 * 9 // 10 + 1
    with pytest.raises(ValueError, match="snapshot_price_review_invalid"):
        subject.validate_capture_policy_artifact(over, now=NOW)
    with pytest.raises(ValueError, match="snapshot_price_review_invalid"):
        protected._validate_policy(over, NOW)
    legacy_over = copy.deepcopy(artifact)
    legacy_over.pop("grant")
    legacy_over["contract_version"] = subject.LEGACY_CAPTURE_POLICY_VERSION
    legacy_over["allowance_id"] = "source_capture_20260907_20260908_v1"
    legacy_over["reserved_micro_usd_per_cutoff"] = 500000
    with pytest.raises(ValueError, match="snapshot_price_review_invalid"):
        protected._validate_policy(legacy_over, NOW)
    with pytest.raises(ValueError, match="snapshot_storage_policy_invalid"):
        protected._validate_policy({**artifact, "bucket": "other"}, NOW)
    with pytest.raises(ValueError, match="snapshot_storage_policy_invalid"):
        protected._validate_policy({**artifact, "contract_version": "v3"}, NOW)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"cumulative_ceiling_micro_usd": 2249999}, "capture_grant_ceiling_insufficient"),
        ({"valid_until": NOW}, "capture_grant_expired"),
        ({"valid_from": NOW + timedelta(seconds=1)}, "capture_grant_not_yet_valid"),
        ({"revocation_state": "revoked"}, "capture_grant_revoked"),
        ({"allowed_cutoffs": ["2026-09-12", "2026-09-12"]}, "capture_grant_invalid"),
        ({"allowed_cutoffs": []}, "capture_grant_invalid"),
        ({"reserved_micro_usd_per_capture": 0}, "capture_grant_invalid"),
        ({"contract_sha256": "short"}, "capture_grant_invalid"),
        ({"grant_id": ""}, "capture_grant_invalid"),
        ({"environment": "production"}, "capture_grant_invalid"),
        ({"extra": 1}, "capture_grant_invalid"),
    ],
)
def test_grants_are_checked_for_coverage_validity_and_revocation(mutation, code):
    with pytest.raises(ValueError, match=code):
        subject.build_capture_policy_artifact(
            subject.GRANT_CAPTURE_POLICY_VERSION,
            grant=grant(**mutation),
            price_inputs=measured(),
            now=NOW,
        )


def test_grant_headroom_refuses_a_modeled_cost_inside_the_ten_percent_margin():
    record = measured(queries_per_cycle=7)
    cost = subject.model_capture_cycle_cost(
        subject.validate_measured_price_inputs(record, now=NOW)
    )["maximum_cycle_cost_micro_usd"]
    assert 750000 * 9 // 10 < cost <= 750000
    with pytest.raises(ValueError, match="capture_policy_headroom_insufficient"):
        subject.build_capture_policy_artifact(
            subject.GRANT_CAPTURE_POLICY_VERSION, grant=grant(), price_inputs=record, now=NOW
        )


# The receipt may name the kind of authority that produced it. A run under a
# verified manifest says so in the record; one that names none was produced
# under no manifest read at the collection boundary.
@pytest.mark.parametrize("kind", ["verified_manifest", "unverified"])
def test_a_completed_run_may_name_the_authority_kind_that_produced_it(kind):
    validated = subject.validate_completed_source_run(run(receipt=receipt(authority_kind=kind)))
    assert validated["receipt"]["authority_kind"] == kind
    assert "authority_kind" not in subject.validate_completed_source_run(run())["receipt"]


@pytest.mark.parametrize(
    "kind", ["", "verified", "VERIFIED_MANIFEST", "manifest", 7, True, None, "unverified "]
)
def test_a_completed_run_refuses_an_authority_kind_the_boundary_never_places(kind):
    with pytest.raises(ValueError, match="source_run_invalid"):
        subject.validate_completed_source_run(run(receipt=receipt(authority_kind=kind)))


def test_a_completed_run_still_refuses_a_receipt_carrying_a_field_of_its_own():
    with pytest.raises(ValueError, match="source_run_invalid"):
        subject.validate_completed_source_run(run(receipt=receipt(collected_by="hand")))


@pytest.mark.parametrize("value", [7, None, 3.5, True, ("contract_version",)])
def test_a_completed_run_refuses_a_receipt_that_is_no_mapping_at_all(value):
    """The mapping check is the guard, not the field set comparison behind it.

    A string receipt is refused by either, because its characters are no field
    set. A receipt that cannot be made into a set at all reaches the comparison
    as a type error, which is no refusal this reader named.
    """
    with pytest.raises(ValueError, match="source_run_invalid"):
        subject.validate_completed_source_run(run(receipt=value))
