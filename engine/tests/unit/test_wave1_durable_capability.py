from __future__ import annotations

from copy import copy
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence import execution_approval, funded_lane, funded_lane_runtime
from src.ingestion.connectors import gdelt, socialcrawl


def _consumption():
    approval_id = "exa_" + "a" * 64
    execution_name = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-open-intelligence-staging/executions/exe-1"
    )
    consumed_at = datetime(2026, 8, 31, 12, 1, tzinfo=UTC)
    return execution_approval.ExecutionConsumption(
        consumption_contract_version="open_intelligence_execution_consumption_v1",
        consumption_id=execution_approval.consumption_id(approval_id, execution_name, consumed_at),
        approval_id=approval_id,
        manifest_sha256="b" * 64,
        operation="wave1_pilot",
        execution_name=execution_name,
        job_resource=(
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
            "trends-engine-open-intelligence-staging"
        ),
        source_sha="c" * 40,
        image_uri=(
            "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:" + "d" * 64
        ),
        consumed_at=consumed_at,
    )


def _bound_consumption():
    # A real v2 authority for wave1_pilot, consumed through the real consumer: the job,
    # principal and image it carries are the packaged origin's funded pilot binding.
    from tests.unit.test_execution_runtime_v2 import consume, fixture, load

    fx = fixture("wave1_pilot")
    authority = load(fx)
    return authority, consume(fx, authority)


def test_retired_compiled_wave1_digest_authority_is_absent():
    source = __import__("inspect").getsource(funded_lane)
    assert "APPROVED_WAVE1_EXECUTION_ADDENDUM_SHA256" not in source
    assert "require_wave1_execution_authority" not in source


def test_capability_is_issued_only_from_one_exact_wave1_consumption():
    authority, consumption = _bound_consumption()
    capability = funded_lane._issue_wave1_execution_capability(authority, consumption)
    assert (
        funded_lane._require_wave1_execution_capability(capability, action="funded_preflight")
        is capability
    )
    assert capability.maximum_bytes_billed == 50_000_000_000
    assert len(capability.gdelt_dry_run_set_sha256) == 64
    with pytest.raises(funded_lane.Wave1ExecutionBlocked):
        funded_lane.Wave1ExecutionCapability()
    with pytest.raises(funded_lane.Wave1ExecutionBlocked):
        funded_lane._issue_wave1_execution_capability(object(), _consumption())


def test_forged_but_well_formed_consumption_cannot_issue_capability():
    with pytest.raises(funded_lane.Wave1ExecutionBlocked):
        funded_lane._issue_wave1_execution_capability(object(), _consumption())


def test_raw_copied_and_object_new_capabilities_refuse():
    authority, consumption = _bound_consumption()
    capability = funded_lane._issue_wave1_execution_capability(authority, consumption)
    forged = object.__new__(funded_lane.Wave1ExecutionCapability)
    for candidate in ("a" * 64, copy(capability), forged):
        with pytest.raises(funded_lane.Wave1ExecutionBlocked):
            funded_lane._require_wave1_execution_capability(candidate, action="vendor_call")


def test_capability_mutation_or_registry_injection_cannot_authorize():
    assert not hasattr(funded_lane, "_WAVE1_CAPABILITIES")
    for field in (
        "consumption_id",
        "manifest_sha256",
        "execution_name",
        "source_sha",
        "image_uri",
        "gdelt_dry_run_set_sha256",
        "maximum_bytes_billed",
    ):
        authority, consumption = _bound_consumption()
        capability = funded_lane._issue_wave1_execution_capability(authority, consumption)
        object.__setattr__(capability, field, "0" * 64)
        with pytest.raises(funded_lane.Wave1ExecutionBlocked):
            funded_lane._require_wave1_execution_capability(capability, action="funded_preflight")


def test_all_protected_wave1_leaves_remove_caller_digest_authority():
    inspect = __import__("inspect")
    for module in (funded_lane_runtime, socialcrawl, gdelt):
        source = inspect.getsource(module)
        assert "execution_addendum_sha256" not in source
        assert "WAVE1_EXECUTION_ADDENDUM_SHA256" not in source
        assert "require_wave1_execution_authority" not in source
        assert "_require_wave1_execution_capability" in source
