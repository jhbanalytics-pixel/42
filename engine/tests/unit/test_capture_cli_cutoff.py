"""The capture command's cutoff is an argument the approved capture policy admits."""

import io
from datetime import UTC, date, datetime, timedelta

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit import test_protected_production_snapshot as input_fixture

GRANT_ID = "source_capture_grant_2026_09_v2"


def module():
    from scripts.staging import capture_protected_production_snapshot

    return capture_protected_production_snapshot


def legacy(cutoff):
    return ("--cutoff-date", cutoff, "--mode", "initial")


def granted(cutoff, grant=GRANT_ID, mode="initial"):
    return ("--cutoff-date", cutoff, "--mode", mode, "--grant", grant)


def test_legacy_form_admits_only_the_accepted_policy_cutoffs():
    capture = module()
    assert capture._ACCEPTED_POLICY["allowed_cutoffs"] == ("2026-09-07",)
    assert capture._parse_arguments(legacy("2026-09-07")) == (date(2026, 9, 7), "initial", None)
    assert capture._parse_cli(legacy("2026-09-07")) == (date(2026, 9, 7), "initial")
    for cutoff in ("2026-09-08", "2026-09-21", "2026-9-07", "2026-02-30"):
        with pytest.raises(ValueError, match=r"^snapshot_cli_invalid$"):
            capture._parse_arguments(legacy(cutoff))


def test_legacy_form_follows_the_policy_record_without_widening_it(monkeypatch):
    capture = module()
    monkeypatch.setitem(capture._ACCEPTED_POLICY, "allowed_cutoffs", ("2026-09-07", "2026-09-14"))
    assert capture._parse_arguments(legacy("2026-09-14"))[0] == date(2026, 9, 14)
    with pytest.raises(ValueError, match=r"^snapshot_cli_invalid$"):
        capture._parse_arguments(legacy("2026-09-21"))


def test_grant_form_parses_any_calendar_cutoff_for_the_grant_to_decide():
    capture = module()
    assert capture._parse_arguments(granted("2026-09-21")) == (
        date(2026, 9, 21),
        "initial",
        GRANT_ID,
    )
    assert capture._parse_arguments(granted("2026-09-21", mode="recover"))[1] == "recover"
    assert capture._parse_cli(granted("2026-09-21")) == (date(2026, 9, 21), "initial")


@pytest.mark.parametrize(
    "argv",
    [
        granted("2026-02-30"),
        granted("20260921"),
        granted("2026-09-21", grant="Grant-X"),
        granted("2026-09-21", grant=""),
        granted("2026-09-21", mode="retry"),
        ("--cutoff-date", "2026-09-21", "--grant", GRANT_ID, "--mode", "initial"),
        (*granted("2026-09-21"), "extra"),
        granted("2026-09-21")[:5],
        ("--cutoff-date", "2026-09-21", "--mode", "initial", "--policy", GRANT_ID),
    ],
)
def test_malformed_grant_form_refuses(argv):
    with pytest.raises(ValueError, match=r"^snapshot_cli_invalid$"):
        module()._parse_arguments(argv)


def test_manifest_vector_of_the_active_origin_parses():
    from tests.unit.test_execution_manifest_origins import CASES

    vector = tuple(CASES["source_snapshot_capture"]["arguments"])
    assert module()._parse_arguments(vector[1:]) == (
        date(2026, 9, 13),
        "initial",
        "source_capture_grant_2026_09_v2",
    )


def grant_inputs(**overrides):
    from src.analysis.open_intelligence import production_snapshot_tables as tables
    from src.analysis.open_intelligence import staging_source_profile as policy

    from tests.unit import test_staging_source_profile as profiles

    now = profiles.NOW
    grant = {
        "grant_id": GRANT_ID,
        "environment": "staging",
        "source_estate_digest": "7" * 64,
        "contract_sha256": module()._CONTRACT_SHA256,
        "valid_from": datetime(2026, 9, 1, tzinfo=UTC),
        "valid_until": datetime(2026, 10, 1, tzinfo=UTC),
        "allowed_cutoffs": ["2026-09-12", "2026-09-13"],
        "reserved_micro_usd_per_capture": 750000,
        "cumulative_ceiling_micro_usd": 1500000,
        "revocation_state": "active",
    }
    grant.update(overrides)
    price = {
        "observed_at": now - timedelta(minutes=1),
        "pricing_sources": ["https://cloud.google.com/bigquery/pricing"],
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
    artifact = policy.build_capture_policy_artifact(
        policy.GRANT_CAPTURE_POLICY_VERSION, grant=grant, price_inputs=price, now=now
    )
    plan = tables.build_capture_plan_v2(
        profiles.profile(),
        grant=grant,
        client_scope_id="ogilvy_default",
        market_scope=["ke", "ng", "za"],
        now=now,
    )
    values = {
        **input_fixture.artifacts(),
        "capture_plan": canonical_bytes(plan),
        "storage_policy": canonical_bytes(artifact),
    }
    return values, profiles.CUTOFF, now


def test_grant_form_binds_the_grant_its_policy_and_plan_name():
    capture = module()
    values, cutoff, now = grant_inputs()
    inputs = capture._validate_cli_inputs(values, granted(cutoff.isoformat()), now)
    assert inputs["policy"]["grant"]["grant_id"] == GRANT_ID
    assert inputs["plan"]["cutoff_date"] == cutoff.isoformat()
    with pytest.raises(ValueError, match=r"^snapshot_inputs_invalid$"):
        capture._validate_cli_inputs(values, granted(cutoff.isoformat(), "another_grant"), now)
    with pytest.raises(ValueError, match=r"^snapshot_inputs_invalid$"):
        capture._validate_cli_inputs(values, granted("2026-09-14"), now)
    with pytest.raises(ValueError, match=r"^snapshot_cli_invalid$"):
        capture._validate_cli_inputs(values, legacy(cutoff.isoformat()), now)


def test_command_cutoff_must_be_the_plan_s_even_inside_the_grant():
    capture = module()
    values, cutoff, now = grant_inputs()
    other = (cutoff + timedelta(days=1)).isoformat()
    assert other in ("2026-09-12", "2026-09-13")
    with pytest.raises(ValueError):
        capture._validate_cli_inputs(values, granted(other), now)


def test_legacy_form_binds_the_legacy_policy():
    capture = module()
    values = input_fixture.artifacts()
    inputs = capture._validate_cli_inputs(
        values, legacy(input_fixture.CUTOFF.isoformat()), input_fixture.NOW
    )
    assert inputs["policy"]["contract_version"] == "open_intelligence_source_capture_storage_v1"
    with pytest.raises(ValueError, match=r"^snapshot_inputs_invalid$"):
        capture._validate_cli_inputs(
            values, granted(input_fixture.CUTOFF.isoformat()), input_fixture.NOW
        )


@pytest.mark.parametrize("mode", ["initial", "recover"])
def test_grant_form_still_meets_the_closed_fresh_route_before_any_runtime_read(monkeypatch, mode):
    for name in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    touched = []
    with pytest.raises(ValueError, match=r"^snapshot_fresh_route_unavailable$"):
        module()._main_impl(
            granted("2026-09-21", mode=mode),
            now=lambda: touched.append("clock"),
            execution_reader=lambda: touched.append("execution"),
            approval_reader=lambda _value: touched.append("approval"),
            build_reader=lambda _value: touched.append("build"),
            runtime_clients=lambda: touched.append("runtime"),
            operation_runner=lambda *a, **k: touched.append("operation"),
            source_preflight=lambda *a: touched.append("preflight"),
            stdout=io.StringIO(),
            diagnostics=lambda *a: touched.append("diagnostic"),
        )
    assert touched == []
