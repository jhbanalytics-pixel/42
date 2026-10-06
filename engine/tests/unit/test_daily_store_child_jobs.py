import hashlib
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence import daily_operation_map, daily_store, execution_generations
from src.analysis.open_intelligence.brain_contract import canonical_bytes

CUTOFF = datetime(2026, 9, 14, tzinfo=UTC)
DAILY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
OWNER = DAILY_JOB + "/executions/parent-1"
RETIRED = {
    "collection": "intelligence-42-ingest-staging",
    "exposure": "intelligence-42-daily-exposure-staging",
    "capture": "intelligence-42-capture-staging",
    "compose": "intelligence-42-compose-staging",
    "certify": "intelligence-42-certify-staging",
    "release": "intelligence-42-daily-release-staging",
}


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def slot():
    return daily_store.slot_operation_id_v2(
        environment="staging", source_estate_id="intelligence-42-core", cutoff_utc=CUTOFF
    )


def intent(stage, child_job):
    return {
        "intent_id": "dsi_"
        + digest(
            {
                "control_contract_version": "42_daily_control_v2",
                "slot_id": slot(),
                "stage": stage,
                "attempt_version": 1,
            }
        ),
        "business_attempt_id": "bat_" + digest({"operation_id": slot(), "attempt": f"{stage}@1"}),
        "stage": stage,
        "attempt_version": 1,
        "input_digest": "4" * 64,
        "operation_context_sha256": "5" * 64,
        "authorizing_approval_id": "exa_" + "6" * 64,
        "authorizing_grant_digest": "7" * 64,
        "child_job_resource": child_job,
        "lease_owner": OWNER,
        "lease_epoch": 1,
        "phase": "prepared",
        "derivation_id": None,
        "dispatch_observation_reference": None,
        "resolution_reference": None,
    }


@pytest.mark.parametrize("stage", daily_store.STAGES_V2)
def test_every_stage_intent_names_the_daily_job_since_amendment_d(stage):
    assert daily_store._validate_intent_v2(intent(stage, DAILY_JOB), slot())["stage"] == stage


@pytest.mark.parametrize("stage", daily_store.STAGES_V2)
def test_the_retired_per_stage_jobs_refuse(stage):
    retired = "projects/ogilvy-trends-v2/locations/us-central1/jobs/" + RETIRED[stage]
    with pytest.raises(daily_store.StoreRefusal, match=r"^dispatch_intent_invalid$"):
        daily_store._validate_intent_v2(intent(stage, retired), slot())


def test_the_store_job_is_the_registry_binding_of_every_mapped_stage():
    registry = execution_generations.active_generation().registry
    for operation in daily_operation_map.DAILY_REGISTRY_OPERATIONS:
        binding = daily_operation_map.child_binding(operation, registry=registry)
        assert daily_store.child_job_resource_v2(binding.stage) == binding.job_resource
    assert daily_store.child_job_resource_v2("collection") == DAILY_JOB
    with pytest.raises(daily_store.StoreRefusal, match=r"^stage_key_invalid$"):
        daily_store.child_job_resource_v2("collect")
