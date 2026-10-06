import json
import re
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence import daily_operation_map as subject
from src.analysis.open_intelligence import execution_generations
from src.analysis.open_intelligence.daily_execution_contracts import OPERATIONS
from src.analysis.open_intelligence.execution_origins import OriginRefusal

ENGINE = Path(__file__).resolve().parents[2]
DAILY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
ORCHESTRATION = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
INT64_MAX = 9223372036854775807


def registry():
    return execution_generations.active_generation().registry


def committed_contract(name, directory="origin_contracts"):
    path = ENGINE / "configs/open_intelligence" / directory / name
    return json.loads(path.read_bytes())


def grant_routine_operations():
    sql = (
        ENGINE / "infra/bigquery_routines/sp_approve_open_intelligence_recurring_grant_v2.sql"
    ).read_text()
    listed = re.search(r"value NOT IN \(([^)]*)\)", sql).group(1)
    return {item.strip().strip("'") for item in listed.split(",")}


def test_the_map_covers_exactly_the_daily_vocabulary():
    assert set(subject.DAILY_REGISTRY_OPERATIONS) | set(subject.UNMAPPED_OPERATIONS) == set(
        OPERATIONS
    )
    assert not set(subject.DAILY_REGISTRY_OPERATIONS) & set(subject.UNMAPPED_OPERATIONS)
    assert set(OPERATIONS) == grant_routine_operations()


def test_the_pinned_map_is_the_reviewed_one():
    # The daily contract revision registered source_collection and daily_composition_apply,
    # so collection is mapped and compose no longer reads the R3 replay's r3_apply rule.
    assert subject.DAILY_REGISTRY_OPERATIONS == {
        "daily_source_collection": "source_collection",
        "daily_collection_exposure_issue": "collection_exposure_issue",
        "daily_source_snapshot_capture": "source_snapshot_capture",
        "daily_composition_apply": "daily_composition_apply",
        "daily_quality_proof_issue": "r3_proof_issue",
        "daily_staging_release": "r3_release",
    }
    assert subject.UNMAPPED_OPERATIONS == {}


@pytest.mark.parametrize("operation", sorted(subject.DAILY_REGISTRY_OPERATIONS))
def test_every_mapped_operation_binds_the_daily_job_in_the_committed_registry(operation):
    binding = subject.child_binding(operation, registry=registry())
    # The active registry's daily origin row links the bridge capture policy.
    contract = committed_contract("source-bridge-capture-v3.json", "candidate_contracts")
    rule = contract["operation_validation"][subject.DAILY_REGISTRY_OPERATIONS[operation]]
    assert binding.operation == operation
    assert binding.stage == OPERATIONS[operation]
    assert binding.registry_operation == subject.DAILY_REGISTRY_OPERATIONS[operation]
    assert binding.job_resource == DAILY_JOB
    assert binding.service_identity == ORCHESTRATION
    assert binding.manifest_version == "open_intelligence_execution_manifest_v2"
    # The bridge policy; tightening the date regex moved it from 95dcb79c.
    assert binding.contract_sha256 == (
        "b007a53067e2e4e841421c146eb96bedc3f269b413b253db0d21c0ebd16ce226"
    )
    assert binding.datasets == tuple(rule["datasets"])
    assert dict(binding.limit_rules) == rule["limits"]


def test_collection_maps_to_its_own_operation_under_the_grant_credit_cap():
    # The approve routine requires run_credit_cap 620 whenever collection is granted,
    # and collection spends vendor credits. collection_exposure_issue pins max_credits to
    # exactly zero, so the daily contract revision gave collection its own operation.
    sql = (
        ENGINE / "infra/bigquery_routines/sp_approve_open_intelligence_recurring_grant_v2.sql"
    ).read_text()
    assert "run_credit_cap') AS INT64) = 620" in sql
    rule = committed_contract("source-bridge-capture-v3.json", "candidate_contracts")[
        "operation_validation"
    ]
    assert rule["collection_exposure_issue"]["limits"]["max_credits"] == {"exact": 0}
    assert rule["source_collection"]["limits"]["max_credits"] == {"maximum": 620, "minimum": 0}
    binding = subject.child_binding("daily_source_collection", registry=registry())
    assert binding.registry_operation == "source_collection"
    assert binding.bounded_limits()["max_credits"] == 620


@pytest.mark.parametrize("operation", ["collection", "r3_apply", "", None, "daily_unknown"])
def test_unknown_operations_refuse(operation):
    with pytest.raises(subject.DailyOperationRefusal, match=r"^daily_operation_unmapped$"):
        subject.child_binding(operation, registry=registry())


def test_bounded_limits_come_from_the_contract_and_none_is_unbounded():
    capture = subject.child_binding("daily_source_snapshot_capture", registry=registry())
    assert capture.bounded_limits() == {
        "max_bytes_billed": 1000000000,
        "max_credits": 0,
        "max_model_calls": 0,
        "max_rows_written": 0,
    }
    # The daily contract revision bounded exposure (5e9 bytes, 1e6 rows), where the int64
    # sentinel used to stand, so it no longer refuses as unbounded.
    exposure = subject.child_binding("daily_collection_exposure_issue", registry=registry())
    assert exposure.unbounded_limits() == ()
    assert exposure.bounded_limits() == {
        "max_bytes_billed": 5000000000,
        "max_credits": 0,
        "max_model_calls": 0,
        "max_rows_written": 1000000,
    }
    release = subject.child_binding("daily_staging_release", registry=registry())
    assert release.bounded_limits()["max_rows_written"] == 2
    for operation in subject.DAILY_REGISTRY_OPERATIONS:
        binding = subject.child_binding(operation, registry=registry())
        assert INT64_MAX not in binding.bounded_limits().values()


def test_an_unbounded_rule_is_named_and_refuses():
    capture = subject.child_binding("daily_source_snapshot_capture", registry=registry())
    rules = dict(capture.limit_rules)
    rules["max_bytes_billed"] = MappingProxyType({"minimum": 0, "maximum": INT64_MAX})
    unbounded = replace(capture, limit_rules=MappingProxyType(rules))
    assert unbounded.unbounded_limits() == ("max_bytes_billed",)
    with pytest.raises(subject.DailyOperationRefusal, match=r"^daily_limit_unbounded$"):
        unbounded.bounded_limits()


def test_a_binding_moved_off_the_daily_job_refuses(monkeypatch):
    real = subject._origin_for

    def moved(operation, *, registry):
        origin = real(operation, registry=registry)
        binding = origin.operation_bindings[operation]
        bindings = dict(origin.operation_bindings)
        bindings[operation] = replace(
            binding, job_resource=DAILY_JOB.replace("daily-staging", "ingest-staging")
        )
        return replace(origin, operation_bindings=MappingProxyType(bindings))

    monkeypatch.setattr(subject, "_origin_for", moved)
    monkeypatch.setattr(subject, "_policy_bytes", lambda origin, registry: b"{}")
    with pytest.raises(subject.DailyOperationRefusal, match=r"^daily_child_job_unbound$"):
        subject.child_binding("daily_staging_release", registry=registry())


def test_a_registry_that_is_not_the_trusted_type_refuses():
    with pytest.raises((subject.DailyOperationRefusal, OriginRefusal, ValueError)):
        subject.child_binding("daily_staging_release", registry=object())


def test_stage_names_translate_to_daily_operations():
    assert {stage: subject.stage_operation(stage) for stage in OPERATIONS.values()} == {
        stage: operation for operation, stage in OPERATIONS.items()
    }
    with pytest.raises(subject.DailyOperationRefusal, match=r"^daily_stage_unknown$"):
        subject.stage_operation("collect")
