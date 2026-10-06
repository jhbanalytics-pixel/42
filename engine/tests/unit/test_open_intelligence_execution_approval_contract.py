import hashlib
import json
import re
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_approval, execution_origins

ENGINE_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ENGINE_ROOT / "configs/open_intelligence/execution_origins_v1.json"
REGISTRY_SHA256 = "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
HISTORICAL_READ = "historical_read"
HISTORICAL_REPLAY = "historical_replay"
PROJECT = "ogilvy-trends-v2"
BUILD_ID = "11111111-1111-4111-8111-111111111111"
SOURCE_SHA = "d" * 40
IMAGE_DIGEST = "b" * 64
CONTRACT_SHA = "a" * 64
ARTIFACT_SHA = "c" * 64
BUILD_ARTIFACT_SHA = "e" * 64
IMAGE_REPOSITORY = "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine"
IMAGE_URI = f"{IMAGE_REPOSITORY}@sha256:{IMAGE_DIGEST}"
BUILD_RESOURCE = (
    "projects/ogilvy-trends-v2/locations/us-central1/builds/11111111-1111-4111-8111-111111111111"
)
APPROVED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
RETAINED_CONTRACTS = {
    "bootstrap_migration_apply": "527798d33d83382f77586fe9869f4456371fce5370a21ad4316d2e4821969545",
    "migration_apply": "527798d33d83382f77586fe9869f4456371fce5370a21ad4316d2e4821969545",
    "collection_exposure_issue": "527798d33d83382f77586fe9869f4456371fce5370a21ad4316d2e4821969545",
    "r3_apply": "5eb924575097ffc893b6d93fcd54ac8bca4ae766adb1cd31d4c03ffa92350054",
    "r3_proof_issue": "5eb924575097ffc893b6d93fcd54ac8bca4ae766adb1cd31d4c03ffa92350054",
    "r3_release": "5eb924575097ffc893b6d93fcd54ac8bca4ae766adb1cd31d4c03ffa92350054",
    "brain_read": "250367b37ec1c086082c0863e31ac938870aae0b63a5d5def9e75127cc29d528",
    "wave1_pilot": "ddd41d395b5ab804048502289b4516f58f3103c1d6443662247461e90518b56b",
    "source_snapshot_capture": "5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f",
}
RETAINED_CONTRACT = RETAINED_CONTRACTS["r3_proof_issue"]
ORIGIN_GATED_FIELDS = {"operation", "datasets", "job_resource", "service_identity", "image_uri"}


def load_registry(path=REGISTRY_PATH, *, root=ENGINE_ROOT):
    return execution_origins.load_origin_registry(
        path,
        expected_sha256=REGISTRY_SHA256,
        contract_root=root,
    )


REGISTRY = load_registry()


def retained_origin(contract_sha256=RETAINED_CONTRACT):
    return REGISTRY[(MANIFEST_V1, contract_sha256)]


def origin_refusal(code):
    return pytest.raises(execution_origins.OriginRefusal, match=f"^{code}$")


OPERATION_CASES = {
    "bootstrap_migration_apply": {
        "job": "trends-engine-oi-approval-bootstrap-staging",
        "identity": "trends-engine-oi-bootstrap@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging_approvals"],
        "arguments": [
            "scripts/migrations/create_open_intelligence_execution_approval_store.py",
            "apply",
        ],
        "environment": {
            "BIGQUERY_DATASET": "trends_v2_staging_approvals",
            "GCP_PROJECT": PROJECT,
            "TRENDS_ENV": "staging",
        },
        "artifacts": {
            "bootstrap_contract",
            "build_provenance",
            "iam_plan",
            "kms_public_key",
            "migration_dry_run",
            "migration_plan",
        },
        "secrets": [],
        "limits": {
            "max_bytes_billed": 0,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 4,
        },
    },
    "migration_apply": {
        "job": "trends-engine-oi-migration-staging",
        "identity": "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": ["scripts/migrations/create_open_intelligence_v2.py", "apply"],
        "artifacts": {
            "build_provenance",
            "cloud_build",
            "migration_contract",
            "migration_dry_run",
            "migration_plan",
        },
        "secrets": [],
        "limits": {
            "max_bytes_billed": 0,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 1,
        },
    },
    "collection_exposure_issue": {
        "job": "trends-engine-oi-exposure-issuer-staging",
        "identity": "trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": ["scripts/staging/issue_collection_exposure_receipts.py"],
        "artifacts": {
            "build_provenance",
            "config",
            "issuer_contract",
            "source_copy_receipt_set",
            "vendor_quota_receipt",
        },
        "secrets": [],
        "limits": {
            "max_bytes_billed": 100,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 7,
        },
    },
    "r3_apply": {
        "job": "trends-engine-oi-apply-staging",
        "identity": "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": ["scripts/staging/replay_open_intelligence.py"],
        "artifacts": {
            "build_provenance",
            "config",
            "exposure_execution_proof",
            "exposure_receipt_readback",
            "inserted_natural_key_set",
            "quality_review_receipt",
            "r3_contract",
            "source_window_receipt_set",
        },
        "secrets": [],
        "limits": {
            "max_bytes_billed": 100,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 7,
        },
    },
    "r3_proof_issue": {
        "job": "trends-engine-oi-r3-proof-staging",
        "identity": "trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": [
            "scripts/staging/issue_r3_execution_proof.py",
            "--r3-execution-name",
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
            "trends-engine-oi-apply-staging/executions/example",
        ],
        "artifacts": {
            "blocked_run_receipt",
            "build_provenance",
            "proof_issuer_contract",
        },
        "secrets": [],
        "limits": {
            "max_bytes_billed": 0,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 0,
        },
    },
    "r3_release": {
        "job": "trends-engine-oi-release-staging",
        "identity": "trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": ["scripts/staging/release_open_intelligence_run.py"],
        "artifacts": {
            "blocked_run_receipt",
            "build_provenance",
            "execution_proof",
            "quality_review_receipt",
            "release_contract",
        },
        "secrets": [],
        "limits": {
            "max_bytes_billed": 0,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 2,
        },
    },
    "brain_read": {
        "job": "trends-engine-oi-brain-staging",
        "identity": "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": [
            "scripts/staging/run_live_intelligence_brain.py",
            "--target",
            "staging",
            "--run-id",
            "run_20260831_brain_manifest",
            "--signal-id",
            "sig_" + "f" * 64,
            "--research-depth",
            "briefing",
        ],
        "artifacts": {
            "brain_contract",
            "build_provenance",
            "run_receipt",
            "source_window_receipt_set",
        },
        "secrets": [],
        "limits": {
            "max_bytes_billed": 0,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 0,
        },
    },
    "wave1_pilot": {
        "job": "trends-engine-open-intelligence-staging",
        "identity": "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging", "trends_v2_staging_funded"],
        "arguments": ["scripts/run_rss_now.py"],
        "environment": {
            "BIGQUERY_DATASET": "trends_v2_staging",
            "GCP_PROJECT": PROJECT,
            "SOCIALCRAWL_CREDENTIAL_LANE": "ogilvy_funded",
            "SOCIALCRAWL_FUNDED_STAGE_NAME": "stage_1_wave_1",
            "TRENDS_ENV": "staging",
        },
        "artifacts": {
            "build_provenance",
            "funded_preflight",
            "gdelt_dry_run_set",
            "r3_seed_manifest",
            "source_lab_snapshot",
            "wave1_contract",
        },
        "secrets": ["SOCIALCRAWL_OGILVY_API_KEY"],
        "limits": {
            "max_bytes_billed": 50_000_000_000,
            "max_credits": 63,
            "max_model_calls": 0,
            "max_rows_written": 100,
        },
    },
}


def valid_manifest(operation="r3_proof_issue"):
    case = OPERATION_CASES[operation]
    environment = case.get(
        "environment",
        {
            "BIGQUERY_DATASET": "trends_v2_staging",
            "GCP_PROJECT": PROJECT,
            "TRENDS_ENV": "staging",
        },
    )
    return {
        "manifest_version": "open_intelligence_execution_manifest_v1",
        "operation": operation,
        "contract_sha256": RETAINED_CONTRACTS[operation],
        "project": PROJECT,
        "datasets": list(case["datasets"]),
        "location": "US",
        "job_resource": (f"projects/{PROJECT}/locations/us-central1/jobs/{case['job']}"),
        "service_identity": case["identity"],
        "source_sha": SOURCE_SHA,
        "image_uri": IMAGE_URI,
        "build_resource": BUILD_RESOURCE,
        "command": ["python"],
        "arguments": list(case["arguments"]),
        "environment": [
            {"name": name, "value": value} for name, value in sorted(environment.items())
        ],
        "secrets": list(case["secrets"]),
        "max_retries": 0,
        "timeout_seconds": 900,
        "input_artifacts": [
            {
                "name": name,
                "sha256": BUILD_ARTIFACT_SHA if name == "build_provenance" else ARTIFACT_SHA,
            }
            for name in sorted(case["artifacts"])
        ],
        "limits": dict(case["limits"]),
        "expires_at": "2026-08-31T23:59:59.000000Z",
    }


def valid_cloud_build_response():
    return {
        "name": BUILD_RESOURCE,
        "id": BUILD_ID,
        "projectId": PROJECT,
        "status": "SUCCESS",
        "sourceProvenance": {"resolvedRepoSource": {"commitSha": SOURCE_SHA}},
        "results": {"images": [{"name": IMAGE_REPOSITORY, "digest": f"sha256:{IMAGE_DIGEST}"}]},
        "finishTime": "2026-08-31T12:00:00.000000Z",
    }


def source_snapshot_manifest(*, cutoff="2026-09-07", mode="initial"):
    payload = valid_manifest("migration_apply")
    payload.update(
        operation="source_snapshot_capture",
        contract_sha256="5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f",
        datasets=["trends_v2_dev", "trends_v2_staging"],
        job_resource=f"projects/{PROJECT}/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging",
        arguments=[
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            cutoff,
            "--mode",
            mode,
        ],
        timeout_seconds=600,
        limits={
            "max_bytes_billed": 1_000_000_000,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 0,
        },
        expires_at="2026-09-09T23:59:59.000000Z",
    )
    payload["input_artifacts"] = [
        {
            "name": name,
            "sha256": payload["contract_sha256"] if name == "capture_contract" else ARTIFACT_SHA,
        }
        for name in (
            "build_provenance",
            "capture_contract",
            "capture_plan",
            "recovery_context",
            "source_metadata",
            "storage_policy",
        )
    ]
    return payload


@pytest.mark.parametrize("cutoff", ["2026-09-07"])
@pytest.mark.parametrize("mode", ["initial", "recover"])
def test_source_snapshot_manifest_is_closed_to_approved_cutoffs_modes_and_limits(cutoff, mode):
    origin_mode = HISTORICAL_REPLAY if mode == "recover" else HISTORICAL_READ
    with pytest.raises(execution_approval.ApprovalRefusal):
        # The unpinned registry cutoff names no completed capture and is not approvable.
        execution_approval.validate_execution_manifest(
            source_snapshot_manifest(cutoff="2026-09-08", mode=mode),
            mode=origin_mode,
            registry=REGISTRY,
        )
    manifest = execution_approval.validate_execution_manifest(
        source_snapshot_manifest(cutoff=cutoff, mode=mode),
        mode=origin_mode,
        registry=REGISTRY,
    )
    assert manifest.operation == "source_snapshot_capture"
    assert manifest.arguments[-1] == mode
    assert dict(manifest.limits) == {
        "max_bytes_billed": 1_000_000_000,
        "max_credits": 0,
        "max_model_calls": 0,
        "max_rows_written": 0,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "date",
        "mode",
        "order",
        "contract",
        "capture_contract",
        "credits",
        "model",
        "rows",
        "bytes",
        "timeout",
        "secrets",
    ],
)
def test_source_snapshot_manifest_refuses_unapproved_authority(mutation):
    payload = source_snapshot_manifest()
    if mutation == "date":
        payload["arguments"][2] = "2026-09-09"
    if mutation == "mode":
        payload["arguments"][4] = "retry"
    if mutation == "order":
        payload["arguments"][1:3], payload["arguments"][3:5] = (
            payload["arguments"][3:5],
            payload["arguments"][1:3],
        )
    if mutation == "contract":
        payload["contract_sha256"] = "0" * 64
    if mutation == "capture_contract":
        payload["input_artifacts"][1]["sha256"] = "0" * 64
    if mutation == "credits":
        payload["limits"]["max_credits"] = 1
    if mutation == "model":
        payload["limits"]["max_model_calls"] = 1
    if mutation == "rows":
        payload["limits"]["max_rows_written"] = 1
    if mutation == "bytes":
        payload["limits"]["max_bytes_billed"] += 1
    if mutation == "timeout":
        payload["timeout_seconds"] = 601
    if mutation == "secrets":
        payload["secrets"] = ["PROVIDER_TOKEN"]
    if mutation == "contract":
        with origin_refusal("execution_origin_pair_invalid"):
            execution_approval.validate_execution_manifest(
                payload, mode=HISTORICAL_READ, registry=REGISTRY
            )
    else:
        with pytest.raises(execution_approval.ApprovalRefusal):
            execution_approval.validate_execution_manifest(
                payload, mode=HISTORICAL_READ, registry=REGISTRY
            )
    with pytest.raises(execution_approval.ApprovalRefusal):
        execution_approval._validate_execution_manifest_v1(payload)


def test_wave1_manifest_uses_the_approved_gdelt_byte_ceiling() -> None:
    assert execution_approval._OPERATION_CONTRACTS["wave1_pilot"]["limits"] == (
        50_000_000_000,
        63,
        0,
        100,
    )

    approval_sql = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "bigquery_routines"
        / "sp_approve_open_intelligence_execution_v1.sql"
    ).read_text(encoding="utf-8")
    assert "limits.max_bytes_billed') AS INT64) = 50000000000" in approval_sql
    assert "limits.max_rows_written') AS INT64) = 100" in approval_sql


def test_manifest_canonicalization_is_exact_and_stable():
    payload = valid_manifest(operation="r3_proof_issue")
    first = execution_approval.canonical_manifest_bytes(
        payload, mode=HISTORICAL_READ, registry=REGISTRY
    )
    second = execution_approval.canonical_manifest_bytes(
        dict(reversed(tuple(payload.items()))), mode=HISTORICAL_READ, registry=REGISTRY
    )

    assert first == second
    assert first.endswith(b"\n") is False
    assert (
        execution_approval.manifest_sha256(payload, mode=HISTORICAL_READ, registry=REGISTRY)
        == hashlib.sha256(first).hexdigest()
    )
    assert b" " not in first


def test_manifest_rejects_extra_field_and_wrong_target():
    payload = valid_manifest(operation="r3_apply")
    payload["extra"] = "forbidden"
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_manifest_invalid",
    ):
        execution_approval.validate_execution_manifest(
            payload, mode=HISTORICAL_READ, registry=REGISTRY
        )

    payload = valid_manifest(operation="r3_apply")
    payload["project"] = "other-project"
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_target_invalid",
    ):
        execution_approval.validate_execution_manifest(
            payload, mode=HISTORICAL_READ, registry=REGISTRY
        )


def test_build_provenance_has_exact_fields_and_digest():
    with origin_refusal("execution_origin_pair_invalid"):
        execution_approval.build_provenance_from_response(
            valid_cloud_build_response(),
            operation_contract_sha256=CONTRACT_SHA,
            manifest_version=MANIFEST_V1,
            mode=HISTORICAL_READ,
            registry=REGISTRY,
        )
    receipt = execution_approval.build_provenance_from_response(
        valid_cloud_build_response(),
        operation_contract_sha256=RETAINED_CONTRACT,
        manifest_version=MANIFEST_V1,
        mode=HISTORICAL_READ,
        registry=REGISTRY,
    )

    assert tuple(asdict(receipt)) == (
        "build_provenance_contract_version",
        "build_resource",
        "project_id",
        "location",
        "build_id",
        "status",
        "resolved_source_sha",
        "image_uri",
        "operation_contract_sha256",
        "finished_at",
    )
    assert (
        hashlib.sha256(
            execution_approval.canonical_build_provenance_bytes(receipt, origin=retained_origin())
        ).hexdigest()
        == "320c3ee2eaa694e8a9495e3d19e02ba8b5ff41df07650e10700e3b193018f448"
    )
    with pytest.raises(FrozenInstanceError):
        receipt.status = "FAILURE"


@pytest.mark.parametrize("operation", tuple(OPERATION_CASES))
def test_each_operation_resolves_to_its_exact_job_identity_and_authority(operation):
    payload = valid_manifest(operation)

    manifest = execution_approval.validate_execution_manifest(
        payload, mode=HISTORICAL_READ, registry=REGISTRY
    )

    case = OPERATION_CASES[operation]
    assert manifest.operation == operation
    assert manifest.job_resource.endswith(f"/jobs/{case['job']}")
    assert manifest.service_identity == case["identity"]
    assert manifest.datasets == tuple(case["datasets"])
    assert manifest.command == ("python",)
    assert manifest.arguments == tuple(case["arguments"])
    assert manifest.environment == tuple(
        sorted(
            case.get(
                "environment",
                {
                    "BIGQUERY_DATASET": "trends_v2_staging",
                    "GCP_PROJECT": PROJECT,
                    "TRENDS_ENV": "staging",
                },
            ).items()
        )
    )
    assert manifest.secrets == tuple(case["secrets"])
    assert tuple(name for name, _digest in manifest.input_artifacts) == tuple(
        sorted(case["artifacts"])
    )
    assert manifest.max_retries == 0
    assert manifest.timeout_seconds == 900
    assert dict(manifest.limits) == case["limits"]


def test_bootstrap_requires_the_approved_contract_sha():
    payload = valid_manifest("bootstrap_migration_apply")
    payload["contract_sha256"] = "0" * 64
    with origin_refusal("execution_origin_pair_invalid"):
        execution_approval.validate_execution_manifest(
            payload, mode=HISTORICAL_READ, registry=REGISTRY
        )
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_manifest_invalid",
    ):
        execution_approval._validate_execution_manifest_v1(payload)


def test_bootstrap_requires_exact_900_second_timeout():
    payload = valid_manifest("bootstrap_migration_apply")
    payload["timeout_seconds"] = 901
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_manifest_invalid",
    ):
        execution_approval.validate_execution_manifest(
            payload, mode=HISTORICAL_READ, registry=REGISTRY
        )


def test_r3_apply_requires_exact_seven_row_limit():
    payload = valid_manifest("r3_apply")
    payload["limits"]["max_rows_written"] = 8
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_manifest_invalid",
    ):
        execution_approval.validate_execution_manifest(
            payload, mode=HISTORICAL_READ, registry=REGISTRY
        )


@pytest.mark.parametrize("operation", tuple(OPERATION_CASES))
def test_each_operation_rejects_structurally_valid_wrong_command_and_arguments(operation):
    for field, replacement in (("command", ["other"]), ("arguments", ["other.py"])):
        payload = valid_manifest(operation)
        payload[field] = replacement
        with pytest.raises(
            execution_approval.ApprovalRefusal,
            match="execution_approval_manifest_invalid",
        ):
            execution_approval.validate_execution_manifest(
                payload, mode=HISTORICAL_READ, registry=REGISTRY
            )


def test_limits_accept_semantically_identical_key_order():
    payload = valid_manifest("r3_apply")
    payload["limits"] = dict(reversed(tuple(payload["limits"].items())))
    manifest = execution_approval.validate_execution_manifest(
        payload, mode=HISTORICAL_READ, registry=REGISTRY
    )
    assert tuple(manifest.limits) == (
        "max_bytes_billed",
        "max_credits",
        "max_model_calls",
        "max_rows_written",
    )


def test_bounded_limits_accept_safer_values_within_contract_caps():
    wave1 = valid_manifest("wave1_pilot")
    wave1["limits"]["max_credits"] = 62
    assert (
        execution_approval.validate_execution_manifest(
            wave1, mode=HISTORICAL_READ, registry=REGISTRY
        ).limits["max_credits"]
        == 62
    )

    migration = valid_manifest("migration_apply")
    migration["limits"]["max_rows_written"] = 0
    assert (
        execution_approval.validate_execution_manifest(
            migration, mode=HISTORICAL_READ, registry=REGISTRY
        ).limits["max_rows_written"]
        == 0
    )

    release = valid_manifest("r3_release")
    release["limits"]["max_rows_written"] = 1
    assert (
        execution_approval.validate_execution_manifest(
            release, mode=HISTORICAL_READ, registry=REGISTRY
        ).limits["max_rows_written"]
        == 1
    )


@pytest.mark.parametrize("job", ["trends-engine-production", "trends-engine-qa", "example"])
def test_r3_proof_rejects_every_non_r3_staging_execution_parent(job: str):
    payload = valid_manifest("r3_proof_issue")
    payload["arguments"][2] = (
        f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{job}/executions/x"
    )
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_manifest_invalid",
    ):
        execution_approval.validate_execution_manifest(
            payload, mode=HISTORICAL_READ, registry=REGISTRY
        )


@pytest.mark.parametrize(
    ("field", "replacement", "error"),
    [
        ("operation", "unknown", "execution_approval_manifest_invalid"),
        ("project", "other-project", "execution_approval_target_invalid"),
        ("datasets", ["trends_v2"], "execution_approval_target_invalid"),
        ("location", "EU", "execution_approval_target_invalid"),
        (
            "job_resource",
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/other",
            "execution_approval_target_invalid",
        ),
        (
            "service_identity",
            "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
            "execution_approval_identity_invalid",
        ),
        ("source_sha", "D" * 40, "execution_approval_manifest_invalid"),
        (
            "image_uri",
            f"{IMAGE_REPOSITORY}:latest",
            "execution_approval_target_invalid",
        ),
        (
            "build_resource",
            "projects/other/locations/us-central1/builds/" + BUILD_ID,
            "execution_approval_target_invalid",
        ),
        ("command", [], "execution_approval_manifest_invalid"),
        ("arguments", [1], "execution_approval_manifest_invalid"),
        ("secrets", ["UNAPPROVED_SECRET"], "execution_approval_manifest_invalid"),
        ("max_retries", 1, "execution_approval_manifest_invalid"),
        ("timeout_seconds", 10801, "execution_approval_manifest_invalid"),
        ("expires_at", "2026-08-31T23:59:59Z", "execution_approval_manifest_invalid"),
    ],
)
def test_manifest_authority_mutations_refuse(field, replacement, error):
    payload = valid_manifest("r3_apply")
    assert payload[field] != replacement
    payload[field] = replacement

    if field in ORIGIN_GATED_FIELDS:
        with origin_refusal("execution_origin_target_invalid"):
            execution_approval.validate_execution_manifest(
                payload, mode=HISTORICAL_READ, registry=REGISTRY
            )
    else:
        with pytest.raises(execution_approval.ApprovalRefusal, match=error):
            execution_approval.validate_execution_manifest(
                payload, mode=HISTORICAL_READ, registry=REGISTRY
            )
    with pytest.raises(execution_approval.ApprovalRefusal, match=error):
        execution_approval._validate_execution_manifest_v1(payload)


def test_manifest_rejects_environment_artifact_limit_and_unicode_mutations():
    mutations = []

    payload = valid_manifest()
    payload["environment"][0]["value"] = "production"
    mutations.append(payload)

    payload = valid_manifest()
    payload["input_artifacts"][0]["sha256"] = "F" * 64
    mutations.append(payload)

    payload = valid_manifest()
    payload["limits"]["max_model_calls"] = 1
    mutations.append(payload)

    payload = valid_manifest()
    payload["limits"]["max_rows_written"] = -1
    mutations.append(payload)

    payload = valid_manifest()
    payload["limits"]["extra"] = 0
    mutations.append(payload)

    payload = valid_manifest()
    payload["arguments"][0] = "cafe\u0301"
    assert payload["arguments"][0] != "caf\u00e9"
    mutations.append(payload)

    payload = valid_manifest()
    payload["limits"]["max_rows_written"] = float("nan")
    mutations.append(payload)

    for mutated in mutations:
        assert mutated != valid_manifest()
        with pytest.raises(
            execution_approval.ApprovalRefusal,
            match="execution_approval_manifest_invalid",
        ):
            execution_approval.validate_execution_manifest(
                mutated, mode=HISTORICAL_READ, registry=REGISTRY
            )


def test_manifest_rejects_unsorted_arrays_and_duplicate_names():
    mutations = []

    payload = valid_manifest("wave1_pilot")
    payload["datasets"].reverse()
    mutations.append(payload)

    payload = valid_manifest("wave1_pilot")
    payload["secrets"] = ["Z_SECRET", "A_SECRET"]
    mutations.append(payload)

    payload = valid_manifest()
    payload["environment"].reverse()
    mutations.append(payload)

    payload = valid_manifest()
    payload["environment"].append(dict(payload["environment"][0]))
    mutations.append(payload)

    payload = valid_manifest()
    payload["input_artifacts"].reverse()
    mutations.append(payload)

    payload = valid_manifest()
    payload["input_artifacts"].append(dict(payload["input_artifacts"][0]))
    mutations.append(payload)

    for mutated in mutations:
        assert mutated != valid_manifest(mutated["operation"])
        if mutated["datasets"] != sorted(mutated["datasets"]):
            with origin_refusal("execution_origin_target_invalid"):
                execution_approval.validate_execution_manifest(
                    mutated, mode=HISTORICAL_READ, registry=REGISTRY
                )
        else:
            with pytest.raises(
                execution_approval.ApprovalRefusal,
                match="execution_approval_manifest_invalid",
            ):
                execution_approval.validate_execution_manifest(
                    mutated, mode=HISTORICAL_READ, registry=REGISTRY
                )
        with pytest.raises(
            execution_approval.ApprovalRefusal,
            match="execution_approval_manifest_invalid",
        ):
            execution_approval._validate_execution_manifest_v1(mutated)


@pytest.mark.parametrize(
    "field",
    ["name", "id", "projectId", "status", "sourceProvenance", "results", "finishTime"],
)
def test_build_provenance_rejects_missing_response_fields(field):
    payload = valid_cloud_build_response()
    removed = payload.pop(field)
    assert removed is not None

    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_build_provenance_invalid",
    ):
        execution_approval.build_provenance_from_response(
            payload,
            RETAINED_CONTRACT,
            manifest_version=MANIFEST_V1,
            mode=HISTORICAL_READ,
            registry=REGISTRY,
        )


def test_build_provenance_projects_away_unrelated_build_metadata():
    payload = valid_cloud_build_response()
    payload["createTime"] = "2026-08-31T11:00:00.000000Z"
    payload["steps"] = [{"name": "gcr.io/cloud-builders/docker"}]
    payload["results"]["buildStepImages"] = [""]
    payload["results"]["images"][0]["pushTiming"] = {"startTime": "2026-08-31T11:59:00Z"}
    assert set(payload) != set(valid_cloud_build_response())

    canonical = execution_approval.normalize_cloud_build_response(payload, origin=retained_origin())
    assert canonical == valid_cloud_build_response()

    receipt = execution_approval.build_provenance_from_response(
        payload,
        RETAINED_CONTRACT,
        manifest_version=MANIFEST_V1,
        mode=HISTORICAL_READ,
        registry=REGISTRY,
    )
    assert receipt == execution_approval.build_provenance_from_response(
        valid_cloud_build_response(),
        RETAINED_CONTRACT,
        manifest_version=MANIFEST_V1,
        mode=HISTORICAL_READ,
        registry=REGISTRY,
    )


def test_build_provenance_rejects_field_mutations():
    mutations = []

    payload = valid_cloud_build_response()
    payload["name"] = payload["name"].replace(PROJECT, "other-project")
    mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["id"] = "22222222-2222-4222-8222-222222222222"
    mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["projectId"] = "other-project"
    mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["status"] = "FAILURE"
    mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["sourceProvenance"]["resolvedRepoSource"]["commitSha"] = "D" * 40
    mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["results"]["images"][0]["name"] = "gcr.io/other/image"
    mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["results"]["images"][0]["digest"] = "sha256:" + "B" * 64
    mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["finishTime"] = "2026-08-31T12:00:00"
    mutations.append(payload)

    for mutated in mutations:
        assert mutated != valid_cloud_build_response()
        with pytest.raises(
            execution_approval.ApprovalRefusal,
            match="execution_approval_build_provenance_invalid",
        ):
            execution_approval.build_provenance_from_response(
                mutated,
                RETAINED_CONTRACT,
                manifest_version=MANIFEST_V1,
                mode=HISTORICAL_READ,
                registry=REGISTRY,
            )

    with origin_refusal("execution_origin_pair_invalid"):
        execution_approval.build_provenance_from_response(
            valid_cloud_build_response(),
            "A" * 64,
            manifest_version=MANIFEST_V1,
            mode=HISTORICAL_READ,
            registry=REGISTRY,
        )
    receipt = execution_approval.build_provenance_from_response(
        valid_cloud_build_response(),
        RETAINED_CONTRACT,
        manifest_version=MANIFEST_V1,
        mode=HISTORICAL_READ,
        registry=REGISTRY,
    )
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_build_provenance_invalid",
    ):
        execution_approval.canonical_build_provenance_bytes(
            replace(receipt, operation_contract_sha256="A" * 64), origin=retained_origin()
        )


def test_build_provenance_requires_one_resolved_git_source_and_one_image():
    source_mutations = []
    payload = valid_cloud_build_response()
    payload["sourceProvenance"] = {}
    source_mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["sourceProvenance"]["resolvedGitSource"] = {"revision": SOURCE_SHA}
    source_mutations.append(payload)

    image_mutations = []
    payload = valid_cloud_build_response()
    payload["results"]["images"] = []
    image_mutations.append(payload)

    payload = valid_cloud_build_response()
    payload["results"]["images"].append(deepcopy(payload["results"]["images"][0]))
    image_mutations.append(payload)

    for mutated in (*source_mutations, *image_mutations):
        assert mutated != valid_cloud_build_response()
        with pytest.raises(
            execution_approval.ApprovalRefusal,
            match="execution_approval_build_provenance_invalid",
        ):
            execution_approval.build_provenance_from_response(
                mutated,
                RETAINED_CONTRACT,
                manifest_version=MANIFEST_V1,
                mode=HISTORICAL_READ,
                registry=REGISTRY,
            )


def test_contract_types_are_frozen_and_have_exact_ordered_fields():
    expected = {
        execution_approval.ExecutionApproval: (
            "approval_contract_version",
            "approval_id",
            "manifest_version",
            "operation",
            "contract_sha256",
            "manifest_sha256",
            "canonical_manifest_json",
            "approved_by",
            "approved_at",
            "expires_at",
            "approval_phrase_sha256",
        ),
        execution_approval.ExecutionConsumption: (
            "consumption_contract_version",
            "consumption_id",
            "approval_id",
            "manifest_sha256",
            "operation",
            "execution_name",
            "job_resource",
            "source_sha",
            "image_uri",
            "consumed_at",
        ),
        execution_approval.ExecutionResult: (
            "result_contract_version",
            "result_id",
            "consumption_id",
            "approval_id",
            "manifest_sha256",
            "operation",
            "execution_name",
            "result_reference",
            "canonical_result_json",
            "result_digest",
            "status",
            "completed_at",
        ),
    }

    for contract_type, fields in expected.items():
        assert tuple(contract_type.__dataclass_fields__) == fields
        assert contract_type.__dataclass_params__.frozen is True


def test_approval_horizon_is_positive_and_at_most_24_hours():
    approved_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    manifest_payload = valid_manifest("brain_read")
    manifest_payload["expires_at"] = "2026-09-01T12:00:00.000000Z"
    with origin_refusal("execution_origin_mode_forbidden"):
        execution_approval.validate_execution_manifest(
            manifest_payload, mode="new_approval", registry=REGISTRY
        )
    canonical_manifest_json = execution_approval.canonical_manifest_bytes(
        manifest_payload, mode=HISTORICAL_READ, registry=REGISTRY
    ).decode("utf-8")
    manifest_digest = execution_approval.manifest_sha256(
        manifest_payload, mode=HISTORICAL_READ, registry=REGISTRY
    )
    common = {
        "approval_contract_version": "open_intelligence_execution_approval_v1",
        "approval_id": execution_approval.approval_id(manifest_digest, APPROVED_BY, approved_at),
        "manifest_version": "open_intelligence_execution_manifest_v1",
        "operation": "brain_read",
        "contract_sha256": manifest_payload["contract_sha256"],
        "manifest_sha256": manifest_digest,
        "canonical_manifest_json": canonical_manifest_json,
        "approved_by": APPROVED_BY,
        "approved_at": approved_at,
        "approval_phrase_sha256": hashlib.sha256(
            (
                f"I approve one staging execution of brain_read for manifest SHA256 "
                f"{manifest_digest}. Production remains unchanged."
            ).encode()
        ).hexdigest(),
    }

    valid = execution_approval.ExecutionApproval(
        **common,
        expires_at=approved_at + timedelta(hours=24),
    )
    assert valid.expires_at - valid.approved_at == timedelta(hours=24)

    for expires_at in (approved_at, approved_at + timedelta(hours=24, microseconds=1)):
        with pytest.raises(
            execution_approval.ApprovalRefusal,
            match="execution_approval_expired",
        ):
            execution_approval.ExecutionApproval(**common, expires_at=expires_at)


def test_public_id_and_provenance_boundaries_refuse_wrong_types_deterministically():
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_manifest_invalid",
    ):
        execution_approval.result_id(
            "exc_" + "1" * 64,
            "receipt",
            "2" * 64,
            [],
            datetime(2026, 8, 31, 12, tzinfo=UTC),
        )

    receipt = execution_approval.BuildProvenanceReceipt(
        build_provenance_contract_version="open_intelligence_build_provenance_v1",
        build_resource=1,
        project_id=PROJECT,
        location="us-central1",
        build_id=BUILD_ID,
        status="SUCCESS",
        resolved_source_sha=SOURCE_SHA,
        image_uri=IMAGE_URI,
        operation_contract_sha256=RETAINED_CONTRACT,
        finished_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
    )
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_build_provenance_invalid",
    ):
        execution_approval.canonical_build_provenance_bytes(receipt, origin=retained_origin())


def test_approval_id_refuses_a_raw_human_principal():
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_manifest_invalid",
    ):
        execution_approval.approval_id(
            CONTRACT_SHA,
            "albert.meintjes@ogilvy.co.za",
            datetime(2026, 8, 31, 12, tzinfo=UTC),
        )


def test_approval_row_rejects_the_wrong_exact_phrase_digest():
    approved_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    payload = valid_manifest("brain_read")
    payload["expires_at"] = "2026-08-31T13:00:00.000000Z"
    canonical = execution_approval.canonical_manifest_bytes(
        payload, mode=HISTORICAL_READ, registry=REGISTRY
    ).decode("utf-8")
    digest = execution_approval.manifest_sha256(payload, mode=HISTORICAL_READ, registry=REGISTRY)
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_manifest_invalid",
    ):
        execution_approval.ExecutionApproval(
            approval_contract_version="open_intelligence_execution_approval_v1",
            approval_id=execution_approval.approval_id(digest, APPROVED_BY, approved_at),
            manifest_version="open_intelligence_execution_manifest_v1",
            operation="brain_read",
            contract_sha256=payload["contract_sha256"],
            manifest_sha256=digest,
            canonical_manifest_json=canonical,
            approved_by=APPROVED_BY,
            approved_at=approved_at,
            expires_at=datetime(2026, 8, 31, 13, tzinfo=UTC),
            approval_phrase_sha256="2" * 64,
        )


def test_approval_row_refuses_malformed_timestamp_types_deterministically():
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_expired",
    ):
        execution_approval.ExecutionApproval(
            approval_contract_version="open_intelligence_execution_approval_v1",
            approval_id="exa_" + "1" * 64,
            manifest_version="open_intelligence_execution_manifest_v1",
            operation="brain_read",
            contract_sha256=CONTRACT_SHA,
            manifest_sha256=CONTRACT_SHA,
            canonical_manifest_json="{}",
            approved_by=APPROVED_BY,
            approved_at=1,
            expires_at=2,
            approval_phrase_sha256="2" * 64,
        )


def test_bootstrap_approval_row_requires_the_distinct_bootstrap_phrase():
    approved_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    payload = valid_manifest("bootstrap_migration_apply")
    payload["expires_at"] = "2026-08-31T13:00:00.000000Z"
    with origin_refusal("execution_origin_mode_forbidden"):
        execution_approval.validate_execution_manifest(
            payload, mode="new_approval", registry=REGISTRY
        )
    canonical = execution_approval.canonical_manifest_bytes(
        payload, mode=HISTORICAL_READ, registry=REGISTRY
    ).decode("utf-8")
    digest = execution_approval.manifest_sha256(payload, mode=HISTORICAL_READ, registry=REGISTRY)
    exact_phrase = (
        f"I approve one staging bootstrap migration for manifest SHA256 {digest}. "
        "Production remains unchanged."
    )
    row = execution_approval.ExecutionApproval(
        approval_contract_version="open_intelligence_execution_approval_v1",
        approval_id=execution_approval.approval_id(digest, APPROVED_BY, approved_at),
        manifest_version="open_intelligence_execution_manifest_v1",
        operation="bootstrap_migration_apply",
        contract_sha256=payload["contract_sha256"],
        manifest_sha256=digest,
        canonical_manifest_json=canonical,
        approved_by=APPROVED_BY,
        approved_at=approved_at,
        expires_at=datetime(2026, 8, 31, 13, tzinfo=UTC),
        approval_phrase_sha256=hashlib.sha256(exact_phrase.encode()).hexdigest(),
    )
    assert row.operation == "bootstrap_migration_apply"


def test_frozen_rows_reject_invalid_direct_construction():
    approved_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    with pytest.raises(execution_approval.ApprovalRefusal):
        execution_approval.ExecutionApproval(
            approval_contract_version="wrong",
            approval_id="bad",
            manifest_version="wrong",
            operation="unknown",
            contract_sha256="bad",
            manifest_sha256="bad",
            canonical_manifest_json="{}",
            approved_by="raw@example.com",
            approved_at=approved_at,
            expires_at=approved_at + timedelta(hours=1),
            approval_phrase_sha256="bad",
        )


@pytest.mark.parametrize(
    "row_type",
    [execution_approval.ExecutionConsumption, execution_approval.ExecutionResult],
)
def test_direct_rows_refuse_unhashable_operation_types(row_type):
    approved_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    fields = (
        {
            "consumption_contract_version": "open_intelligence_execution_consumption_v1",
            "consumption_id": "exc_" + "1" * 64,
            "approval_id": "exa_" + "2" * 64,
            "manifest_sha256": CONTRACT_SHA,
            "operation": [],
            "execution_name": "bad",
            "job_resource": "bad",
            "source_sha": SOURCE_SHA,
            "image_uri": IMAGE_URI,
            "consumed_at": approved_at,
        }
        if row_type is execution_approval.ExecutionConsumption
        else {
            "result_contract_version": "open_intelligence_execution_result_v1",
            "result_id": "exr_" + "1" * 64,
            "consumption_id": "exc_" + "2" * 64,
            "approval_id": "exa_" + "3" * 64,
            "manifest_sha256": CONTRACT_SHA,
            "operation": [],
            "execution_name": "bad",
            "result_reference": "receipt",
            "canonical_result_json": "{}",
            "result_digest": hashlib.sha256(b"{}").hexdigest(),
            "status": "succeeded",
            "completed_at": approved_at,
        }
    )
    with pytest.raises(execution_approval.ApprovalRefusal):
        row_type(**fields)
    with pytest.raises(execution_approval.ApprovalRefusal):
        execution_approval.ExecutionConsumption(
            consumption_contract_version="wrong",
            consumption_id="bad",
            approval_id="bad",
            manifest_sha256="bad",
            operation="unknown",
            execution_name="bad",
            job_resource="bad",
            source_sha="bad",
            image_uri="bad",
            consumed_at=approved_at,
        )
    with pytest.raises(execution_approval.ApprovalRefusal):
        execution_approval.ExecutionResult(
            result_contract_version="wrong",
            result_id="bad",
            consumption_id="bad",
            approval_id="bad",
            manifest_sha256="bad",
            operation="unknown",
            execution_name="bad",
            result_reference="bad",
            canonical_result_json="{}",
            result_digest="bad",
            status="unknown",
            completed_at=approved_at,
        )


def test_valid_consumption_and_result_rows_bind_their_exact_preimages():
    consumed_at = datetime(2026, 8, 31, 12, 1, tzinfo=UTC)
    completed_at = datetime(2026, 8, 31, 12, 2, tzinfo=UTC)
    approval = "exa_" + "1" * 64
    execution_name = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-r3-proof-staging/executions/example"
    )
    consumption = execution_approval.consumption_id(approval, execution_name, consumed_at)
    consumption_row = execution_approval.ExecutionConsumption(
        consumption_contract_version="open_intelligence_execution_consumption_v1",
        consumption_id=consumption,
        approval_id=approval,
        manifest_sha256=CONTRACT_SHA,
        operation="r3_proof_issue",
        execution_name=execution_name,
        job_resource=(
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-r3-proof-staging"
        ),
        source_sha=SOURCE_SHA,
        image_uri=IMAGE_URI,
        consumed_at=consumed_at,
    )
    assert consumption_row.consumption_id == consumption

    canonical_result_json = '{"ok":true}'
    result_digest = hashlib.sha256(canonical_result_json.encode("utf-8")).hexdigest()
    result_reference = "gs://ogilvy-trends-v2-execution-approvals-staging/results/example.json"
    result = execution_approval.result_id(
        consumption,
        result_reference,
        result_digest,
        "succeeded",
        completed_at,
    )
    result_row = execution_approval.ExecutionResult(
        result_contract_version="open_intelligence_execution_result_v1",
        result_id=result,
        consumption_id=consumption,
        approval_id=approval,
        manifest_sha256=CONTRACT_SHA,
        operation="r3_proof_issue",
        execution_name=execution_name,
        result_reference=result_reference,
        canonical_result_json=canonical_result_json,
        result_digest=result_digest,
        status="succeeded",
        completed_at=completed_at,
    )
    assert result_row.result_id == result


def test_deterministic_ids_use_exact_canonical_preimages():
    approved_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    consumed_at = datetime(2026, 8, 31, 12, 1, tzinfo=UTC)
    completed_at = datetime(2026, 8, 31, 12, 2, tzinfo=UTC)
    execution_name = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-r3-proof-staging/executions/example"
    )
    result_reference = "gs://ogilvy-trends-v2-execution-approvals-staging/results/example.json"

    approval = execution_approval.approval_id(CONTRACT_SHA, APPROVED_BY, approved_at)
    assert approval == "exa_d1a0a7295487bb867802032943bf64405a3a686741d9b34235d0605cf8db28c0"

    consumption = execution_approval.consumption_id(approval, execution_name, consumed_at)
    assert consumption == "exc_a57a6431f845c0a2ed0e4ee1d3d9d7a34b17b2ed78972d70f9ddb9b3e0913329"

    result = execution_approval.result_id(
        consumption,
        result_reference,
        IMAGE_DIGEST,
        "succeeded",
        completed_at,
    )
    assert result == "exr_cf00cbae7c9b4c6b563d4ba409ca836202ebf0c8a402b0e28a51a2019126565a"


LIVE_BUILD_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "cloud_build_connected_repository_v1.json"
)
LIVE_SOURCE_SHA = "dddafd6a7af284c39c42afe1b1eb387e2aafb2b6"
LIVE_IMAGE_DIGEST = "sha256:8afb12031c74d85f557fb2f68c503a8e9b9f141cf588efd1c330b87c63cd249e"
LIVE_BUILD_RESOURCE = (
    "projects/ogilvy-trends-v2/locations/us-central1/builds/6b0e1e79-c819-4732-b189-df736dc09b16"
)
CONNECTED_REPOSITORY = (
    "projects/ogilvy-trends-v2/locations/us-central1/connections/tev2-gh/"
    "repositories/jhbanalytics-pixel-trends-engine-v2"
)


def live_connected_repository_response():
    return json.loads(LIVE_BUILD_FIXTURE.read_text(encoding="utf-8"))


def test_live_connected_repository_fixture_is_the_captured_build():
    payload = live_connected_repository_response()
    assert payload["sourceProvenance"] == {}
    assert payload["source"] == {
        "connectedRepository": {"repository": CONNECTED_REPOSITORY, "revision": LIVE_SOURCE_SHA}
    }
    assert payload["name"].startswith("projects/590353929363/")


def test_normalizer_projects_live_connected_repository_build_to_seven_exact_fields():
    """Successor projection: the six base fields plus the actual source fields.

    Authority: r03-build-provenance-repair2-independent-review.md, integrated as 9638db8,
    which states that actual source fields are preserved and no sourceProvenance is
    manufactured, and successor 1, which requires the raw build's exact
    source.connectedRepository identity rather than a legacy SHA-only envelope. The
    projection matches tests/test_execution_build_origins.py; the previously expected
    synthesized envelope is kept below as a negative control.
    """
    payload = live_connected_repository_response()
    canonical = execution_approval.normalize_cloud_build_response(payload, origin=retained_origin())

    assert tuple(canonical) == (
        *execution_approval._BUILD_RESPONSE_FIELDS,
        "source",
        "sourceProvenance",
    )
    assert canonical["source"] == payload["source"]
    assert canonical["sourceProvenance"] == {}
    assert canonical["sourceProvenance"] != {
        "resolvedConnectedRepository": {"revision": LIVE_SOURCE_SHA}
    }
    assert canonical["name"] == LIVE_BUILD_RESOURCE
    assert canonical["id"] == "6b0e1e79-c819-4732-b189-df736dc09b16"
    assert canonical["projectId"] == PROJECT
    assert canonical["status"] == "SUCCESS"
    assert canonical["results"] == {
        "images": [{"name": f"{IMAGE_REPOSITORY}:{LIVE_SOURCE_SHA}", "digest": LIVE_IMAGE_DIGEST}]
    }
    assert canonical["finishTime"] == "2026-09-01T12:00:50.236089Z"


def test_live_connected_repository_build_yields_a_provenance_receipt_without_an_adapter():
    receipt = execution_approval.build_provenance_from_response(
        live_connected_repository_response(),
        RETAINED_CONTRACT,
        manifest_version=MANIFEST_V1,
        mode=HISTORICAL_READ,
        registry=REGISTRY,
    )

    assert receipt.build_resource == LIVE_BUILD_RESOURCE
    assert receipt.build_id == "6b0e1e79-c819-4732-b189-df736dc09b16"
    assert receipt.resolved_source_sha == LIVE_SOURCE_SHA
    assert receipt.image_uri == f"{IMAGE_REPOSITORY}@{LIVE_IMAGE_DIGEST}"
    assert receipt.finished_at == datetime(2026, 9, 1, 12, 0, 50, 236089, tzinfo=UTC)


def test_normalizer_keeps_legacy_source_provenance_and_tagless_image_exact():
    payload = valid_cloud_build_response()
    canonical = execution_approval.normalize_cloud_build_response(
        deepcopy(payload), origin=retained_origin()
    )
    assert canonical == payload

    tagged = valid_cloud_build_response()
    tagged["results"]["images"][0]["name"] = f"{IMAGE_REPOSITORY}:{SOURCE_SHA}"
    receipt = execution_approval.build_provenance_from_response(
        tagged,
        RETAINED_CONTRACT,
        manifest_version=MANIFEST_V1,
        mode=HISTORICAL_READ,
        registry=REGISTRY,
    )
    assert receipt.image_uri == IMAGE_URI


def test_normalizer_accepts_agreeing_dual_envelopes_and_empty_legacy_with_exact_connected():
    """Successor projection: an agreeing legacy envelope is kept verbatim next to the
    native source, and an absent legacy envelope stays absent.

    Authority: r03-build-provenance-repair2-independent-review.md, integrated as 9638db8,
    which states that actual source fields are preserved and no sourceProvenance is
    manufactured, and successor 1, which requires the raw build's exact
    source.connectedRepository identity rather than a legacy SHA-only envelope. The
    projection matches tests/test_execution_build_origins.py; the previously expected
    synthesized envelope is kept below as a negative control.
    """
    agreeing = live_connected_repository_response()
    agreeing["sourceProvenance"] = {"resolvedRepoSource": {"commitSha": LIVE_SOURCE_SHA}}
    canonical = execution_approval.normalize_cloud_build_response(
        agreeing, origin=retained_origin()
    )
    assert canonical["sourceProvenance"] == {"resolvedRepoSource": {"commitSha": LIVE_SOURCE_SHA}}
    assert canonical["source"] == agreeing["source"]

    absent_legacy = live_connected_repository_response()
    del absent_legacy["sourceProvenance"]
    canonical = execution_approval.normalize_cloud_build_response(
        absent_legacy, origin=retained_origin()
    )
    assert tuple(canonical) == (*execution_approval._BUILD_RESPONSE_FIELDS, "source")
    assert canonical["source"] == absent_legacy["source"]
    assert "sourceProvenance" not in canonical
    assert canonical.get("sourceProvenance") != {
        "resolvedConnectedRepository": {"revision": LIVE_SOURCE_SHA}
    }


def _wrong_repository(payload):
    payload["source"]["connectedRepository"]["repository"] = CONNECTED_REPOSITORY.replace(
        "jhbanalytics-pixel-trends-engine-v2", "other-repository"
    )


def _branch_revision(payload):
    payload["source"]["connectedRepository"]["revision"] = "feat/open-intelligence-phase0"


def _uppercase_revision(payload):
    payload["source"]["connectedRepository"]["revision"] = LIVE_SOURCE_SHA.upper()


def _short_revision(payload):
    payload["source"]["connectedRepository"]["revision"] = LIVE_SOURCE_SHA[:39]


def _unsupported_source(payload):
    payload["source"] = {"storageSource": {"bucket": "b", "object": "o.tgz"}}


def _ambiguous_native_source(payload):
    payload["source"]["repoSource"] = {"commitSha": LIVE_SOURCE_SHA}


def _ambiguous_legacy_source(payload):
    payload["sourceProvenance"] = {
        "resolvedRepoSource": {"commitSha": LIVE_SOURCE_SHA},
        "resolvedGitSource": {"revision": LIVE_SOURCE_SHA},
    }


def _missing_source(payload):
    payload["source"] = {}


def _absent_source(payload):
    del payload["source"]


def _extra_connected_field(payload):
    payload["source"]["connectedRepository"]["dir"] = "."


def _missing_connected_field(payload):
    del payload["source"]["connectedRepository"]["repository"]


def _mismatched_dual_envelopes(payload):
    payload["sourceProvenance"] = {"resolvedRepoSource": {"commitSha": "c" * 40}}


def _legacy_with_bad_shape(payload):
    payload["sourceProvenance"] = {"resolvedRepoSource": {"commitSha": LIVE_SOURCE_SHA, "dir": "."}}


def _foreign_project_number(payload):
    payload["name"] = payload["name"].replace("590353929363", "590353929364")


def _foreign_project_id(payload):
    payload["projectId"] = "other-project"


def _wrong_build_id(payload):
    payload["id"] = "22222222-2222-4222-8222-222222222222"


def _not_success(payload):
    payload["status"] = "WORKING"


def _no_images(payload):
    payload["results"]["images"] = []


def _two_images(payload):
    payload["results"]["images"].append(deepcopy(payload["results"]["images"][0]))


def _wrong_image_repository(payload):
    payload["results"]["images"][0]["name"] = (
        "us-central1-docker.pkg.dev/ogilvy-trends-v2/other/trends-engine:" + LIVE_SOURCE_SHA
    )


def _digest_in_name(payload):
    payload["results"]["images"][0]["name"] = f"{IMAGE_REPOSITORY}@{LIVE_IMAGE_DIGEST}"


def _bad_digest(payload):
    payload["results"]["images"][0]["digest"] = "sha256:" + "G" * 64


def _image_without_digest(payload):
    del payload["results"]["images"][0]["digest"]


def _results_without_images(payload):
    payload["results"] = {"buildStepImages": []}


def _missing_finish_time(payload):
    del payload["finishTime"]


def _non_nfc_text(payload):
    payload["logUrl"] = "https://console.cloud.google.com/cloud-build/builds/e\u0301"


@pytest.mark.parametrize(
    "mutate",
    [
        _wrong_repository,
        _branch_revision,
        _uppercase_revision,
        _short_revision,
        _unsupported_source,
        _ambiguous_native_source,
        _ambiguous_legacy_source,
        _missing_source,
        _absent_source,
        _extra_connected_field,
        _missing_connected_field,
        _mismatched_dual_envelopes,
        _legacy_with_bad_shape,
        _foreign_project_number,
        _foreign_project_id,
        _wrong_build_id,
        _not_success,
        _no_images,
        _two_images,
        _wrong_image_repository,
        _digest_in_name,
        _bad_digest,
        _image_without_digest,
        _results_without_images,
        _missing_finish_time,
        _non_nfc_text,
    ],
    ids=lambda fn: fn.__name__.lstrip("_"),
)
def test_live_connected_repository_build_refuses_every_named_attack(mutate):
    payload = live_connected_repository_response()
    mutate(payload)
    assert payload != live_connected_repository_response()

    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_build_provenance_invalid",
    ):
        execution_approval.build_provenance_from_response(
            payload,
            RETAINED_CONTRACT,
            manifest_version=MANIFEST_V1,
            mode=HISTORICAL_READ,
            registry=REGISTRY,
        )


@pytest.mark.parametrize("payload", [None, [], "build", 7, {"name": LIVE_BUILD_RESOURCE}])
def test_normalizer_refuses_non_build_payloads(payload):
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="execution_approval_build_provenance_invalid",
    ):
        execution_approval.normalize_cloud_build_response(payload, origin=retained_origin())


_ARRAY_COMPARISON = re.compile(
    r"(?:!=|<>|(?<![<>!])=)\s*\[|\]\s*(?:!=|<>|=)"
    r"|(?:!=|<>|(?<![<>!])=)\s*ARRAY\("
    r"|JSON_VALUE_ARRAY\([^)]*\)\s*(?:!=|<>|=)"
    r"|(?:!=|<>|(?<![<>!])=)\s*JSON_VALUE_ARRAY\("
    r"|\bARRAY\((?:[^()]|\([^()]*\))*\)\s*(?:!=|<>|=)"
)


_JAVASCRIPT_UDF_BODY = re.compile(r'AS r"""[\s\S]*?"""')


def _strip_javascript_udf_bodies(text):
    # A persistent JavaScript UDF body is not BigQuery SQL, so its array literals and
    # comparisons are outside this lint. The body is blanked line for line so offender
    # line numbers stay exact. Narrowing authority: the reviewed Unicode UDF slice,
    # r03-sql-canonical-repair1-independent-review.md, integrated as e979bbf.
    return _JAVASCRIPT_UDF_BODY.sub(
        lambda match: 'AS r"""' + "\n" * match.group(0).count("\n") + '"""',
        text,
    )


# An element read such as v_arguments[SAFE_OFFSET(5)] yields a scalar, so comparing it is
# legal; the lint blanks those reads before it looks for the array operand patterns.
_ARRAY_ELEMENT_READ = re.compile(r"\w+\[(?:SAFE_)?(?:OFFSET|ORDINAL)\([^\]]*\)\]")


def _array_comparison_offenders(name, text):
    text = _strip_javascript_udf_bodies(text)
    offenders = []
    array_variables = re.findall(r"DECLARE\s+(\w+)\s+ARRAY<", text)
    for number, line in enumerate(text.splitlines(), start=1):
        if re.match(r"\s*SET\s+\w+\s*=", line):
            continue  # an assignment, not a comparison
        line = _ARRAY_ELEMENT_READ.sub("element", line)
        if _ARRAY_COMPARISON.search(line):
            offenders.append(f"{name}:{number}")
            continue
        for variable in array_variables:
            left = re.search(rf"\b{variable}\s*(?:!=|<>|=)\s*(?:\w|\[)", line)
            right = re.search(rf"(?:!=|<>|(?<![<>!])=)\s*{variable}\b", line)
            if left or right:
                offenders.append(f"{name}:{number}")
                break
    return offenders


def test_routine_sql_never_compares_arrays_with_equality_operators():
    """Lint SQL text only for ARRAY equality; JavaScript UDF bodies are stripped first.

    Authority: r03-sql-canonical-repair1-independent-review.md, integrated as e979bbf. The
    controls prove the stripped UDF keeps its CREATE FUNCTION header and line count, and
    that a SQL array equality still trips the lint, so the narrowing cannot hide a hit.
    """
    # BigQuery defines neither = nor != for ARRAY operands, so a procedure that reaches such a
    # statement fails at CALL time with "Inequality is not defined for arguments of type ARRAY".
    routine_dir = Path(__file__).resolve().parents[2] / "infra" / "bigquery_routines"
    udf_text = (routine_dir / "fn_is_canonical_execution_json_v1.sql").read_text(encoding="utf-8")
    stripped = _strip_javascript_udf_bodies(udf_text)
    assert "CREATE OR REPLACE FUNCTION" in stripped
    assert "LANGUAGE js" in stripped
    assert "function fail()" in udf_text
    assert "function fail()" not in stripped
    assert stripped.count("\n") == udf_text.count("\n")
    assert _array_comparison_offenders(
        "control.sql", "DECLARE v_ids ARRAY<STRING>;\nIF v_ids = ['a'] THEN\nEND IF;\n"
    ) == ["control.sql:2"]
    assert _array_comparison_offenders("udf.sql", 'AS r"""\nvar output = [];\n""";\n') == []
    # Element reads compare scalars, so they never trip; a literal on either side still does.
    assert (
        _array_comparison_offenders(
            "element.sql",
            "DECLARE v_ids ARRAY<STRING>;\nIF v_ids[SAFE_OFFSET(0)] = 'a' THEN\nEND IF;\n"
            "IF v_ids[OFFSET(1)] != v_ids[ORDINAL(1)] THEN\nEND IF;\n",
        )
        == []
    )
    assert _array_comparison_offenders(
        "literal_left.sql", "DECLARE v_ids ARRAY<STRING>;\nIF ['a'] = v_ids THEN\nEND IF;\n"
    ) == ["literal_left.sql:2"]
    assert _array_comparison_offenders(
        "variable.sql", "DECLARE v_ids ARRAY<STRING>;\nIF v_ids = v_other THEN\nEND IF;\n"
    ) == ["variable.sql:2"]
    # A blanked element read on the left must not hide an array operand on the right.
    adversarial = (
        "DECLARE v_ids ARRAY<STRING>;\nDECLARE v_other ARRAY<STRING>;\n"
        "IF v_ids[SAFE_OFFSET(0)] = v_ids THEN\nEND IF;\n"
        "IF v_other[ORDINAL(1)] != v_ids THEN\nEND IF;\n"
        'IF v_ids[OFFSET(0)] = JSON_VALUE_ARRAY(v_m, "$.x") THEN\nEND IF;\n'
        "IF ARRAY(SELECT x FROM UNNEST(v_ids) AS x) = v_ids[OFFSET(0)] THEN\nEND IF;\n"
        'SET v_ids = JSON_VALUE_ARRAY(v_m, "$.y");\n'
    )
    assert _array_comparison_offenders("adversarial.sql", adversarial) == [
        "adversarial.sql:3",
        "adversarial.sql:5",
        "adversarial.sql:7",
        "adversarial.sql:9",
    ]

    offenders = []
    for path in sorted(routine_dir.glob("*.sql")):
        offenders.extend(_array_comparison_offenders(path.name, path.read_text(encoding="utf-8")))
    assert offenders == []


def test_routine_sql_builds_multi_column_arrays_only_as_structs():
    # BigQuery refuses an ARRAY subquery with more than one column unless it selects AS
    # STRUCT, and it refuses at call time, not at CREATE PROCEDURE, so the registration
    # routine compiled and then failed on its first real call.
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "infra" / "bigquery_routines"
    offenders = []
    for path in sorted(root.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"ARRAY\(\s*SELECT\s+(?!AS STRUCT)([\s\S]*?)\sFROM\s", text):
            columns = match.group(1)
            # A single expression may carry commas and AS inside its own parentheses; only
            # a top-level comma means more than one column.
            while re.search(r"\([^()]*\)", columns):
                columns = re.sub(r"\([^()]*\)", "", columns)
            if "," in columns:
                offenders.append((path.name, match.group(1)[:60]))
    assert offenders == []


CAPTURE_POLICY = (
    ENGINE_ROOT
    / "configs/open_intelligence/origin_contracts/successor-source-snapshot-capture.json"
)


def _capture_rule():
    return json.loads(CAPTURE_POLICY.read_bytes())["operation_validation"][
        "source_snapshot_capture"
    ]


def _capture_plan(**overrides):
    from tests.unit import test_production_snapshot_tables as tables_tests

    return tables_tests._v2_plan(**overrides), tables_tests._v2_grant()


def _capture_now():
    from tests.unit import test_staging_source_profile as profiles

    return profiles.NOW


def test_capture_vector_round_trips_from_a_v2_plan_through_the_policy_rule():
    plan, grant = _capture_plan()
    rule = _capture_rule()["arguments"]
    vector = execution_approval.build_source_snapshot_capture_arguments_v2(
        plan, grant=grant, mode="initial", now=_capture_now(), rule=rule
    )
    assert vector == (
        "scripts/staging/capture_protected_production_snapshot.py",
        "--cutoff-date",
        plan["cutoff_date"],
        "--mode",
        "initial",
        "--grant",
        grant["grant_id"],
    )
    assert execution_approval._validate_policy_arguments(vector, rule) is True
    read = execution_approval.read_source_snapshot_capture_arguments_v2(vector, rule=rule)
    assert (read.cutoff_date.isoformat(), read.mode, read.grant_id) == (
        plan["cutoff_date"],
        "initial",
        plan["snapshot_plan"]["grant_id"],
    )
    assert read.cutoff_date.isoformat() in grant["allowed_cutoffs"]
    assert plan["snapshot_plan"]["source_estate_digest"] == grant["source_estate_digest"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        ({"mode": "replay"}, "source_snapshot_arguments_invalid"),
        ({"mode": None}, "source_snapshot_arguments_invalid"),
        ({"mode": ["initial"]}, "source_snapshot_arguments_invalid"),
        ({"mode": {"initial": True}}, "source_snapshot_arguments_invalid"),
        ({"mode": 1}, "source_snapshot_arguments_invalid"),
        (
            {"plan": {"contract_version": "open_intelligence_protected_capture_plan_v1"}},
            "source_snapshot_plan_invalid",
        ),
        ({"plan": {"cutoff_date": "2026-09-31"}}, "source_snapshot_plan_invalid"),
        ({"grant": {"allowed_cutoffs": ["2026-09-01"]}}, "source_snapshot_cutoff_not_permitted"),
        ({"grant": {"source_estate_digest": "9" * 64}}, "source_snapshot_estate_mismatch"),
        ({"grant": {"grant_id": "another_grant"}}, "source_snapshot_estate_mismatch"),
    ],
)
def test_capture_vector_builder_refuses_with_the_routine_codes(mutation, code):
    plan, grant = _capture_plan()
    plan = dict(plan)
    plan.update(mutation.get("plan", {}))
    grant.update(mutation.get("grant", {}))
    with pytest.raises(execution_approval.ApprovalRefusal, match=f"^{code}$"):
        execution_approval.build_source_snapshot_capture_arguments_v2(
            plan,
            grant=grant,
            mode=mutation.get("mode", "recover"),
            now=_capture_now(),
            rule=_capture_rule()["arguments"],
        )


@pytest.mark.parametrize(
    "vector",
    [
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "2026-09-13",
            "--mode",
            "initial",
            "--grant",
        ),
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "2026-09-13",
            "--mode",
            "initial",
            "--grant",
            "g",
            "x",
        ),
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff",
            "2026-09-13",
            "--mode",
            "initial",
            "--grant",
            "g",
        ),
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "2026-09-31",
            "--mode",
            "initial",
            "--grant",
            "g",
        ),
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "20260913",
            "--mode",
            "initial",
            "--grant",
            "g",
        ),
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "2026-09-13",
            "--mode",
            "replay",
            "--grant",
            "g",
        ),
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "2026-09-13",
            "--mode",
            "initial",
            "--grant",
            "Grant",
        ),
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "2026-09-13",
            "--mode",
            "initial",
            "--grant",
            "",
        ),
        (
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "2026-09-13",
            "--grant",
            "g",
            "--mode",
            "initial",
        ),
    ],
)
def test_capture_vector_reader_refuses_each_shape_fault(vector):
    rule = _capture_rule()["arguments"]
    assert execution_approval._validate_policy_arguments(tuple(vector), rule) is False
    with pytest.raises(
        execution_approval.ApprovalRefusal, match=r"^source_snapshot_arguments_invalid$"
    ):
        execution_approval.read_source_snapshot_capture_arguments_v2(tuple(vector), rule=rule)


def test_capture_vector_reader_admits_the_routine_shape_only():
    rule = _capture_rule()["arguments"]
    vector = (
        "scripts/staging/capture_protected_production_snapshot.py",
        "--cutoff-date",
        "2026-09-13",
        "--mode",
        "recover",
        "--grant",
        "source_capture_grant_2026_09_v2",
    )
    read = execution_approval.read_source_snapshot_capture_arguments_v2(vector, rule=rule)
    assert read.cutoff_date == date(2026, 9, 13)
    assert (read.mode, read.grant_id) == ("recover", "source_capture_grant_2026_09_v2")
    assert (
        execution_approval._validate_policy_arguments(
            vector, {"kind": "exact", "value": list(vector)}
        )
        is True
    )


def test_recurring_grant_approval_kind_binds_the_c03_phrase_and_the_durable_identity():
    """The grant kind is keyed by the grant digest, carries the grant document, and its
    phrase digest is the C03 formula over the proposal sha256, not the v2 formula."""
    from src.analysis.open_intelligence import recurring_grant_phrases

    from tests.unit.test_execution_approval_routine_v3 import PROPOSAL_SHA256, sample_row

    row = sample_row()
    generation = execution_approval.execution_generations.active_generation()
    context = {
        "mode": "new_consume",
        "registry": generation.registry,
        "expected_resource_manifest_sha256": generation.resource_manifest_sha256,
    }
    approval = execution_approval.RecurringGrantApproval(**row, **context)
    assert approval.approved_by == execution_approval._APPROVED_BY
    assert approval.grant()["issuing_principal"] == execution_approval._APPROVED_BY
    assert (
        approval.approval_phrase_sha256
        == hashlib.sha256(
            recurring_grant_phrases.approval_phrase(PROPOSAL_SHA256).encode("utf-8")
        ).hexdigest()
    )
    assert approval.approval_phrase_sha256 != execution_approval._approval_phrase_sha256_v2(
        approval.operation,
        approval.manifest_sha256,
        approval.origin_registry_sha256,
        approval.resource_manifest_sha256,
    )
    assert approval.manifest_sha256 == recurring_grant_phrases.grant_digest(approval.grant())
    assert approval.expires_at == datetime.fromisoformat(approval.grant()["valid_until"])
    with pytest.raises(FrozenInstanceError):
        approval.revocation_state = "revoked"
    v2_formula = execution_approval._approval_phrase_sha256_v2(
        "recurring_grant_v1",
        row["manifest_sha256"],
        row["origin_registry_sha256"],
        row["resource_manifest_sha256"],
    )
    for broken in (
        {"operation": "recurring_grant_revocation_v1"},
        {"approved_by": "albert.meintjes@ogilvy.co.za"},
        {"approval_phrase_sha256": v2_formula},
        {"expires_at": row["approved_at"]},
        {"revocation_state": "revoked"},
        {"revoked_at": row["approved_at"] - timedelta(seconds=1), "revocation_state": "revoked"},
    ):
        with pytest.raises(
            execution_approval.ApprovalRefusal, match=r"^execution_approval_manifest_invalid$"
        ):
            execution_approval.RecurringGrantApproval(**{**row, **broken}, **context)
    # A row bound to another generation refuses the way every v2 record does.
    with pytest.raises(
        execution_approval.ApprovalRefusal, match=r"^execution_approval_manifest_invalid$"
    ):
        execution_approval.RecurringGrantApproval(
            **row, **{**context, "expected_resource_manifest_sha256": "4" * 64}
        )
