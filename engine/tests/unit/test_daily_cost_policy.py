import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence import daily_cost_policy as subject
from src.analysis.open_intelligence import daily_operation_map, execution_generations
from src.analysis.open_intelligence.brain_contract import canonical_bytes

ENGINE = Path(__file__).resolve().parents[2]
OBSERVED = datetime(2026, 9, 23, 6, tzinfo=UTC)
NOW = OBSERVED + timedelta(hours=2)
STORED = OBSERVED + timedelta(minutes=5)
JOB_POLICY = "a" * 64
TIB = 2**40


def rates(**changes):
    record = {
        "contract_version": "daily_workload_rates_v1",
        "observed_at": OBSERVED.isoformat(),
        "pricing_sources": [
            "https://cloud.google.com/bigquery/pricing",
            "https://cloud.google.com/vertex-ai/generative-ai/pricing",
        ],
        "query_micro_usd_per_tib": 6250000,
        "vendor_credit_micro_usd": 1000,
        "model_call_micro_usd": 20000,
    }
    record.update(changes)
    return record


def bindings(*operations):
    registry = execution_generations.active_generation().registry
    return {
        operation: daily_operation_map.child_binding(operation, registry=registry)
        for operation in operations
    }


def build(**changes):
    arguments = {
        "rates": rates(),
        "bindings": bindings("daily_source_snapshot_capture", "daily_staging_release"),
        "administrative_bytes_billed": 10 * 2**30,
        "job_policy_digest": JOB_POLICY,
        "now": NOW,
        "stored_at": STORED,
    }
    arguments.update(changes)
    return subject.build_daily_cost_policy(**arguments)


def routine_keys():
    sql = (
        ENGINE / "infra/bigquery_routines/sp_derive_open_intelligence_daily_execution_v1.sql"
    ).read_text()
    return re.search(r"mode=>'strict'\),','\)='([a-z_,]+)'", sql).group(1).split(",")


def test_policy_carries_exactly_the_fields_the_derive_routine_requires():
    policy = build()
    assert sorted(policy) == routine_keys()
    assert policy["contract_version"] == "daily_workload_cost_policy_v1"
    assert policy["job_policy_digest"] == JOB_POLICY
    canonical_bytes(policy)


def test_reservations_are_computed_from_contract_caps_and_observed_rates():
    policy = build()
    # capture: the contract bounds bytes billed at 1000000000 and pins credits and calls at 0
    capture = -(-1000000000 * 6250000 // TIB)
    assert policy["operation_reservations_micro_usd"] == {
        "daily_source_snapshot_capture": capture,
        "daily_staging_release": 0,
    }
    assert policy["administrative_reservation_micro_usd"] == -(-10 * 2**30 * 6250000 // TIB)
    assert all(type(value) is int for value in policy["operation_reservations_micro_usd"].values())


def test_review_window_is_the_observation_plus_twenty_four_hours():
    policy = build()
    assert policy["reviewed_at"] == OBSERVED.isoformat()
    assert policy["expires_at"] == (OBSERVED + timedelta(hours=24)).isoformat()
    assert policy["pricing_sources"] == sorted(rates()["pricing_sources"])


def test_assumptions_name_every_input_that_moved_a_reservation():
    assumptions = build()["assumptions"]
    assert assumptions == sorted(assumptions)
    joined = "\n".join(assumptions)
    for fragment in (
        "query_micro_usd_per_tib=6250000",
        "vendor_credit_micro_usd=1000",
        "model_call_micro_usd=20000",
        "daily_source_snapshot_capture max_bytes_billed=1000000000",
        # The bridge policy; tightening the date regex moved it from 95dcb79c.
        "contract_sha256=b007a53067e2e4e841421c146eb96bedc3f269b413b253db0d21c0ebd16ce226",
        "administrative_bytes_billed=10737418240",
    ):
        assert fragment in joined


@pytest.mark.parametrize(
    ("now", "code"),
    [
        (OBSERVED + timedelta(hours=24), "daily_cost_rates_stale"),
        (OBSERVED + timedelta(hours=30), "daily_cost_rates_stale"),
        (OBSERVED - timedelta(seconds=1), "daily_cost_rates_future"),
    ],
)
def test_observations_older_than_a_day_or_from_the_future_refuse(now, code):
    with pytest.raises(subject.DailyCostPolicyRefusal, match=rf"^{code}$"):
        build(now=now)


def test_an_unbounded_contract_cap_refuses_rather_than_guessing_a_bound():
    # The daily contract revision bounded exposure, so the unbounded cap is built here
    # from its binding with the int64 sentinel put back on one limit.
    exposure = bindings("daily_collection_exposure_issue")["daily_collection_exposure_issue"]
    rules = dict(exposure.limit_rules)
    rules["max_rows_written"] = MappingProxyType({"minimum": 0, "maximum": 2**63 - 1})
    unbounded = replace(exposure, limit_rules=MappingProxyType(rules))
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_limit_unbounded$"):
        build(bindings={"daily_collection_exposure_issue": unbounded})


@pytest.mark.parametrize(
    "changes",
    [
        {"contract_version": "daily_workload_rates_v0"},
        {"pricing_sources": []},
        {"pricing_sources": ["http://cloud.google.com/bigquery/pricing"]},
        {"pricing_sources": ["https://user@cloud.google.com/bigquery/pricing"]},
        {"query_micro_usd_per_tib": -1},
        {"query_micro_usd_per_tib": True},
        {"vendor_credit_micro_usd": 1.5},
        {"model_call_micro_usd": "20000"},
        {"observed_at": "2026-09-23T06:00:00"},
        {"extra": 1},
    ],
)
def test_malformed_rates_refuse(changes):
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_rates_invalid$"):
        build(rates=rates(**changes))


def test_self_asserted_reservations_are_not_an_input():
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_rates_invalid$"):
        build(rates=rates(operation_reservations_micro_usd={"daily_staging_release": 0}))
    with pytest.raises(TypeError):
        build(operation_reservations_micro_usd={})


@pytest.mark.parametrize(
    "changes",
    [
        {"administrative_bytes_billed": 0},
        {"administrative_bytes_billed": True},
        {"job_policy_digest": "A" * 64},
        {"bindings": {}},
        {"bindings": {"daily_staging_release": object()}},
    ],
)
def test_malformed_arguments_refuse(changes):
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_policy_invalid$"):
        build(**changes)


def test_a_binding_filed_under_another_operation_refuses():
    release = bindings("daily_staging_release")["daily_staging_release"]
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_policy_invalid$"):
        build(bindings={"daily_source_snapshot_capture": release})


def test_validation_mirrors_the_derive_routine_window():
    policy = build()
    assert subject.validate_daily_cost_policy(policy, now=NOW) == policy
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_policy_expired$"):
        subject.validate_daily_cost_policy(policy, now=OBSERVED + timedelta(hours=24))
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_policy_expired$"):
        subject.validate_daily_cost_policy(policy, now=OBSERVED - timedelta(seconds=1))
    stretched = dict(policy, expires_at=(OBSERVED + timedelta(hours=25)).isoformat())
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_policy_invalid$"):
        subject.validate_daily_cost_policy(stretched, now=NOW)
    negative = dict(policy, operation_reservations_micro_usd={"daily_staging_release": -1})
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_policy_invalid$"):
        subject.validate_daily_cost_policy(negative, now=NOW)


# review of dd9fc49, should fix: a zero rate and a caller-declared observation time


@pytest.mark.parametrize(
    "field", ["query_micro_usd_per_tib", "vendor_credit_micro_usd", "model_call_micro_usd"]
)
def test_a_zero_rate_refuses(field):
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_rates_invalid$"):
        build(rates=rates(**{field: 0}))


@pytest.mark.parametrize(
    "stored_at",
    [
        OBSERVED - timedelta(seconds=1),
        NOW + timedelta(seconds=1),
    ],
)
def test_an_observation_time_the_storage_time_does_not_witness_refuses(stored_at):
    """observed_at is declared by whoever wrote the record; the provider's storage time
    bounds it: the record cannot claim an observation after it was stored, and it cannot
    have been stored after now."""
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_rates_unwitnessed$"):
        build(stored_at=stored_at)


@pytest.mark.parametrize("stored_at", [None, "2026-09-23T06:05:00+00:00", datetime(2026, 9, 23, 6)])
def test_a_malformed_storage_time_refuses(stored_at):
    with pytest.raises(subject.DailyCostPolicyRefusal, match=r"^daily_cost_rates_invalid$"):
        build(stored_at=stored_at)


def test_the_storage_time_is_required():
    arguments = {
        "rates": rates(),
        "bindings": bindings("daily_staging_release"),
        "administrative_bytes_billed": 2**30,
        "job_policy_digest": JOB_POLICY,
        "now": NOW,
    }
    with pytest.raises(TypeError):
        subject.build_daily_cost_policy(**arguments)
