import hashlib
import inspect
import json
import os
from copy import deepcopy
from itertools import product
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_approval, execution_origins

ENGINE_ROOT = Path(__file__).resolve().parents[2]
# The active generation's registry: the bridge v3 registry, whose daily origin row links the
# v3 capture policy. The amendment e registry is retained for reading its records.
REGISTRY_PATH = ENGINE_ROOT / "configs/open_intelligence/execution_origins_bridge_v3.json"
# Tightening the date regex moved the registry from e6b95e35 and the policy from 95dcb79c.
REGISTRY_SHA256 = "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2"
AMENDMENT_E_REGISTRY_PATH = ENGINE_ROOT / "configs/open_intelligence/execution_origins_v1.json"
AMENDMENT_E_DAILY_CONTRACT = "66e1a11f30c6dd8a7ca9db2110ef0af91d9a3aeda6b48d0b6323797839a06546"
# The daily origin row's policy under the active registry.
DAILY_CONTRACT = "b007a53067e2e4e841421c146eb96bedc3f269b413b253db0d21c0ebd16ce226"
MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
SOURCE_SHA = "a" * 40
IMAGE_URI = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:" + "b" * 64
BUILD_RESOURCE = (
    "projects/ogilvy-trends-v2/locations/us-central1/builds/11111111-1111-4111-8111-111111111111"
)
COMMON_ENVIRONMENT = [
    {"name": "BIGQUERY_DATASET", "value": "trends_v2_staging"},
    {"name": "GCP_PROJECT", "value": "ogilvy-trends-v2"},
    {"name": "TRENDS_ENV", "value": "staging"},
]
MODES = ("historical_read", "historical_replay", "new_approval", "new_consume")
INT64_MAX = 9_223_372_036_854_775_807

CASES = {
    "migration_apply": {
        "contract": "0fd42bbc35e9db6e935f6fb353f7956fb6d8c433999c563285a35d96c96a9a65",
        "job": "intelligence-42-migration-staging",
        "identity": "intelligence-42-migration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": ["scripts/migrations/create_open_intelligence_v2.py", "apply"],
        "artifacts": [
            "build_provenance",
            "cloud_build",
            "migration_contract",
            "migration_dry_run",
            "migration_plan",
        ],
        "limits": (0, 0, 0, 1),
    },
    "collection_exposure_issue": {
        "contract": DAILY_CONTRACT,
        "job": "intelligence-42-daily-staging",
        "identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": ["scripts/staging/issue_collection_exposure_receipts.py"],
        "artifacts": [
            "build_provenance",
            "config",
            "issuer_contract",
            "source_copy_receipt_set",
            "vendor_quota_receipt",
        ],
        # The daily contract revision bounded exposure: 5e9 bytes, 1e6 rows.
        "limits": (5_000_000_000, 0, 0, 1_000_000),
    },
    "source_collection": {
        "contract": DAILY_CONTRACT,
        "job": "intelligence-42-daily-staging",
        "identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["intelligence_42_sources_staging"],
        "environment": [
            {"name": "BIGQUERY_DATASET", "value": "intelligence_42_sources_staging"},
            {"name": "GCP_PROJECT", "value": "ogilvy-trends-v2"},
            {"name": "TRENDS_ENV", "value": "staging"},
        ],
        "arguments": ["scripts/staging/collect_42_sources.py"],
        "artifacts": ["build_provenance", "cost_policy", "daily_profile"],
        "limits": (1_000_000_000, 620, 0, 0),
    },
    "daily_composition_apply": {
        "contract": DAILY_CONTRACT,
        "job": "intelligence-42-daily-staging",
        "identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": [
            "scripts/staging/compose_daily_open_intelligence.py",
            "--cutoff-date",
            "2026-09-25",
        ],
        "artifacts": [
            "build_provenance",
            "config",
            "exposure_execution_proof",
            "exposure_receipt_readback",
            "inserted_natural_key_set",
            "quality_review_receipt",
            "r3_contract",
            "source_window_receipt_set",
        ],
        "limits": (10_000_000_000, 0, 100, 1_000_000),
    },
    "legacy_chain_replay": {
        "contract": DAILY_CONTRACT,
        "job": "intelligence-42-daily-staging",
        "identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": [
            "scripts/staging/replay_open_intelligence.py",
            "--apply-run",
            "--trend-date",
            "2026-09-25",
            "--run-id",
            "run_20260925_legacy",
            "--source-sha",
            "a" * 40,
        ],
        "artifacts": [
            "build_provenance",
            "config",
            "exposure_execution_proof",
            "exposure_receipt_readback",
            "inserted_natural_key_set",
            "quality_review_receipt",
            "r3_contract",
            "source_window_receipt_set",
        ],
        "limits": (10_000_000_000, 0, 0, 1_000_000),
    },
    "r3_apply": {
        "contract": DAILY_CONTRACT,
        "job": "intelligence-42-daily-staging",
        "identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": ["scripts/staging/replay_open_intelligence.py"],
        "artifacts": [
            "build_provenance",
            "config",
            "exposure_execution_proof",
            "exposure_receipt_readback",
            "inserted_natural_key_set",
            "quality_review_receipt",
            "r3_contract",
            "source_window_receipt_set",
        ],
        "limits": (INT64_MAX, 0, 0, 7),
    },
    "r3_proof_issue": {
        "contract": DAILY_CONTRACT,
        "job": "intelligence-42-daily-staging",
        "identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": [
            "scripts/staging/issue_r3_execution_proof.py",
            "--r3-execution-name",
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
            "intelligence-42-daily-staging/executions/example",
        ],
        "artifacts": ["blocked_run_receipt", "build_provenance", "proof_issuer_contract"],
        "limits": (0, 0, 0, 0),
    },
    "r3_release": {
        "contract": DAILY_CONTRACT,
        "job": "intelligence-42-daily-staging",
        "identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": ["scripts/staging/release_open_intelligence_run.py"],
        "artifacts": [
            "blocked_run_receipt",
            "build_provenance",
            "execution_proof",
            "quality_review_receipt",
            "release_contract",
        ],
        "limits": (0, 0, 0, 2),
    },
    "brain_read": {
        "contract": "596920b6dd5d3d431120349af7b108ab3182337e008c449ebee1557ea69f2de4",
        "job": "intelligence-42-brain-staging",
        "identity": "intelligence-42-brain@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": [
            "scripts/staging/run_live_intelligence_brain.py",
            "--target",
            "staging",
            "--run-id",
            "run_20260913_brain",
            "--signal-id",
            "sig_" + "c" * 64,
            "--research-depth",
            "briefing",
        ],
        "artifacts": [
            "brain_contract",
            "build_provenance",
            "run_receipt",
            "source_window_receipt_set",
        ],
        "limits": (0, 0, 0, 0),
    },
    "source_snapshot_capture": {
        "contract": DAILY_CONTRACT,
        "job": "intelligence-42-daily-staging",
        "identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging"],
        "arguments": [
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            "2026-09-13",
            "--mode",
            "initial",
            "--grant",
            "source_capture_grant_2026_09_v2",
        ],
        "artifacts": [
            "bridge_policy",
            "capture_contract",
            "capture_plan",
            "collection_receipt_set",
            "history_completion_set",
            "recovery_context",
            "source_metadata",
            "storage_policy",
            "temporal_rules",
        ],
        "limits": (1_000_000_000, 0, 0, 0),
    },
    "wave1_pilot": {
        "contract": "edc805068e0297085926004b86fb1d7d30cc2158e07761439cdb6a6ed074c2d3",
        "job": "intelligence-42-funded-pilot-staging",
        "identity": "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com",
        "datasets": ["trends_v2_staging", "trends_v2_staging_funded"],
        "arguments": ["scripts/run_rss_now.py"],
        "artifacts": [
            "build_provenance",
            "funded_preflight",
            "gdelt_dry_run_set",
            "r3_seed_manifest",
            "source_lab_snapshot",
            "wave1_contract",
        ],
        "limits": (50_000_000_000, 63, 0, 100),
        "environment": [
            {"name": "BIGQUERY_DATASET", "value": "trends_v2_staging"},
            {"name": "GCP_PROJECT", "value": "ogilvy-trends-v2"},
            {"name": "SOCIALCRAWL_CREDENTIAL_LANE", "value": "ogilvy_funded"},
            {"name": "SOCIALCRAWL_FUNDED_STAGE_NAME", "value": "stage_1_wave_1"},
            {"name": "TRENDS_ENV", "value": "staging"},
        ],
        "secrets": ["SOCIALCRAWL_OGILVY_API_KEY"],
    },
}


def load_registry(path=REGISTRY_PATH, *, root=ENGINE_ROOT):
    return execution_origins.load_origin_registry(
        path,
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        contract_root=root,
    )


def manifest(operation):
    case = CASES[operation]
    max_bytes, max_credits, max_model_calls, max_rows = case["limits"]
    return {
        "manifest_version": MANIFEST_V2,
        "operation": operation,
        "contract_sha256": case["contract"],
        "project": "ogilvy-trends-v2",
        "datasets": list(case["datasets"]),
        "location": "US",
        "job_resource": (f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{case['job']}"),
        "service_identity": case["identity"],
        "source_sha": SOURCE_SHA,
        "image_uri": IMAGE_URI,
        "build_resource": BUILD_RESOURCE,
        "command": ["python"],
        "arguments": list(case["arguments"]),
        "environment": deepcopy(case.get("environment", COMMON_ENVIRONMENT)),
        "secrets": list(case.get("secrets", [])),
        "max_retries": 0,
        "timeout_seconds": 900,
        "input_artifacts": [{"name": name, "sha256": "d" * 64} for name in case["artifacts"]],
        "limits": {
            "max_bytes_billed": max_bytes,
            "max_credits": max_credits,
            "max_model_calls": max_model_calls,
            "max_rows_written": max_rows,
        },
        "expires_at": "2026-09-14T12:00:00.000000Z",
    }


def validate(payload, mode="new_consume", registry=None):
    return execution_approval.validate_execution_manifest(
        payload,
        mode=mode,
        registry=registry or load_registry(),
    )


def refusal(code):
    return pytest.raises(
        (execution_approval.ApprovalRefusal, execution_origins.OriginRefusal),
        match=f"^{code}$",
    )


def test_manifest_interfaces_require_explicit_mode_and_registry():
    module = execution_approval
    assert str(inspect.signature(module.validate_execution_manifest)) == (
        "(payload: object, *, mode, registry) -> "
        "src.analysis.open_intelligence.execution_approval.ExecutionManifest"
    )
    assert str(inspect.signature(module.canonical_manifest_bytes)) == (
        "(payload: object, *, mode, registry) -> bytes"
    )
    assert str(inspect.signature(module.manifest_sha256)) == (
        "(payload: object, *, mode, registry) -> str"
    )


@pytest.mark.parametrize("operation", CASES)
@pytest.mark.parametrize("mode", MODES)
def test_every_v2_operation_is_admitted_in_each_explicit_mode(operation, mode):
    payload = manifest(operation)
    admitted = validate(payload, mode)
    assert admitted.manifest_version == MANIFEST_V2
    assert admitted.operation == operation
    canonical = json.loads(
        execution_approval.canonical_manifest_bytes(
            payload,
            mode=mode,
            registry=load_registry(),
        )
    )
    assert len(canonical) == 20
    assert set(canonical) == set(execution_approval._MANIFEST_FIELDS)


def test_all_183_retained_manifests_keep_exact_bytes_and_digests_in_both_modes():
    metadata_path = os.getenv("R03_HISTORICAL_AUTHORITY_METADATA")
    page_path = os.getenv("R03_HISTORICAL_AUTHORITY_PAGE")
    if not metadata_path or not page_path:
        pytest.skip("private historical authority inputs are not installed")
    metadata = json.loads(Path(metadata_path).read_bytes())
    fields = [item["name"] for item in metadata["schema"]["fields"]]
    page = json.loads(Path(page_path).read_bytes())
    registry = load_registry()
    count = 0
    for row in page["rows"]:
        values = {name: cell["v"] for name, cell in zip(fields, row["f"], strict=True)}
        payload = json.loads(values["canonical_manifest_json"])
        expected = values["canonical_manifest_json"].encode("utf-8")
        for mode in ("historical_read", "historical_replay"):
            assert (
                execution_approval.canonical_manifest_bytes(payload, mode=mode, registry=registry)
                == expected
            )
            assert (
                execution_approval.manifest_sha256(payload, mode=mode, registry=registry)
                == values["manifest_sha256"]
            )
        count += 1
    assert count == int(metadata["numRows"]) == 183


def retained_manifests_by_pair():
    metadata_path = os.getenv("R03_HISTORICAL_AUTHORITY_METADATA")
    page_path = os.getenv("R03_HISTORICAL_AUTHORITY_PAGE")
    if not metadata_path or not page_path:
        pytest.skip("private historical authority inputs are not installed")
    metadata = json.loads(Path(metadata_path).read_bytes())
    fields = [item["name"] for item in metadata["schema"]["fields"]]
    page = json.loads(Path(page_path).read_bytes())
    result = {}
    for row in page["rows"]:
        values = {name: cell["v"] for name, cell in zip(fields, row["f"], strict=True)}
        payload = json.loads(values["canonical_manifest_json"])
        key = (payload["manifest_version"], payload["contract_sha256"])
        result.setdefault(key, {}).setdefault(payload["operation"], payload)
    return result


def test_every_historical_operation_refuses_each_other_known_v1_pair_before_output():
    registry = load_registry()
    pairs = retained_manifests_by_pair()
    assert len(pairs) == 5
    assert sum(len(operations) for operations in pairs.values()) == 9
    for selector, operations in pairs.items():
        for payload in operations.values():
            for foreign in pairs:
                if foreign == selector:
                    continue
                mutated = deepcopy(payload)
                mutated["contract_sha256"] = foreign[1]
                for mode in ("historical_read", "historical_replay"):
                    with refusal("execution_origin_target_invalid"):
                        execution_approval.validate_execution_manifest(
                            mutated,
                            mode=mode,
                            registry=registry,
                        )
                    with refusal("execution_origin_target_invalid"):
                        execution_approval.canonical_manifest_bytes(
                            mutated,
                            mode=mode,
                            registry=registry,
                        )


def test_shared_historical_rows_refuse_cross_operation_job_and_principal_swaps():
    registry = load_registry()
    shared = [
        operations for operations in retained_manifests_by_pair().values() if len(operations) > 1
    ]
    assert sorted(len(operations) for operations in shared) == [3, 3]
    for operations in shared:
        for operation, payload in operations.items():
            for foreign_operation, foreign in operations.items():
                if operation == foreign_operation:
                    continue
                for field in ("job_resource", "service_identity"):
                    mutated = deepcopy(payload)
                    mutated[field] = foreign[field]
                    for mode in ("historical_read", "historical_replay"):
                        with pytest.raises(
                            (
                                execution_approval.ApprovalRefusal,
                                execution_origins.OriginRefusal,
                            ),
                            match=(
                                r"^(execution_origin_target_invalid|"
                                "execution_approval_target_invalid|"
                                "execution_approval_identity_invalid)$"
                            ),
                        ):
                            execution_approval.validate_execution_manifest(
                                mutated,
                                mode=mode,
                                registry=registry,
                            )


def test_private_v1_record_shape_validation_remains_origin_neutral():
    pairs = retained_manifests_by_pair()
    r3_payload = next(
        payload
        for operations in pairs.values()
        for operation, payload in operations.items()
        if operation == "r3_apply"
    )
    brain_contract = next(
        selector[1] for selector, operations in pairs.items() if "brain_read" in operations
    )
    r3_payload["contract_sha256"] = brain_contract
    assert execution_approval._validate_execution_manifest_v1(r3_payload).operation == "r3_apply"


def test_fresh_v1_unknown_and_mixed_selectors_refuse_before_shape_validation():
    registry = load_registry()
    historical = next(row for row in registry.values() if row.manifest_version == MANIFEST_V1)
    for mode in ("new_approval", "new_consume"):
        with refusal("execution_origin_mode_forbidden"):
            execution_approval.validate_execution_manifest(
                {
                    "manifest_version": historical.manifest_version,
                    "contract_sha256": historical.contract_sha256,
                },
                mode=mode,
                registry=registry,
            )
    with refusal("execution_origin_pair_invalid"):
        execution_approval.validate_execution_manifest(
            {"manifest_version": MANIFEST_V2, "contract_sha256": "f" * 64},
            mode="new_consume",
            registry=registry,
        )
    with refusal("execution_origin_pair_invalid"):
        execution_approval.validate_execution_manifest(
            {
                "manifest_version": MANIFEST_V2,
                "contract_sha256": historical.contract_sha256,
            },
            mode="new_consume",
            registry=registry,
        )


@pytest.mark.parametrize(
    ("field", "replacement", "code"),
    [
        ("project", "other-project", "execution_approval_target_invalid"),
        ("location", "us-central1", "execution_approval_target_invalid"),
        ("job_resource", "wrong", "execution_approval_target_invalid"),
        ("service_identity", "wrong", "execution_approval_identity_invalid"),
        ("datasets", ["other"], "execution_approval_target_invalid"),
        ("source_sha", "A" * 40, "execution_approval_manifest_invalid"),
        (
            "image_uri",
            "us-central1-docker.pkg.dev/other/image@sha256:" + "b" * 64,
            "execution_approval_target_invalid",
        ),
        (
            "build_resource",
            "projects/other/locations/us-central1/builds/example",
            "execution_approval_target_invalid",
        ),
    ],
)
def test_each_v2_target_binding_refuses(field, replacement, code):
    payload = manifest("migration_apply")
    payload[field] = replacement
    with refusal(code):
        validate(payload)


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("migration_apply", ["scripts/migrations/create_open_intelligence_v2.py"]),
        ("collection_exposure_issue", []),
        ("r3_apply", ["scripts/staging/replay_open_intelligence.py", "extra"]),
        ("r3_release", ["wrong"]),
        (
            "r3_proof_issue",
            [
                "scripts/staging/issue_r3_execution_proof.py",
                "--r3-execution-name",
                "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
                "trends-engine-oi-apply-staging/executions/example",
            ],
        ),
    ],
)
def test_ordered_operation_arguments_refuse_wrong_variants(operation, arguments):
    payload = manifest(operation)
    payload["arguments"] = arguments
    with refusal("execution_approval_manifest_invalid"):
        validate(payload)


@pytest.mark.parametrize("depth", ["scan", "investigation"])
def test_brain_requires_a_bounded_decision_for_deep_modes(depth):
    payload = manifest("brain_read")
    payload["arguments"][-1] = depth
    with refusal("execution_approval_manifest_invalid"):
        validate(payload)
    payload["arguments"] += ["--decision-question", "What changed?"]
    assert validate(payload).arguments[-1] == "What changed?"


@pytest.mark.parametrize(
    "decision",
    ["x" * 2001, "line\nbreak"],
)
def test_brain_refuses_oversized_or_control_character_decisions(decision):
    payload = manifest("brain_read")
    payload["arguments"][-1] = "scan"
    payload["arguments"] += ["--decision-question", decision]
    with refusal("execution_approval_manifest_invalid"):
        validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("extra", True),
        lambda payload: payload["datasets"].append(payload["datasets"][0]),
        lambda payload: payload["environment"].reverse(),
        lambda payload: payload["secrets"].append("A_SECRET"),
        lambda payload: payload["input_artifacts"].reverse(),
        lambda payload: payload["input_artifacts"].append(deepcopy(payload["input_artifacts"][0])),
        lambda payload: payload["input_artifacts"][0].__setitem__("sha256", "D" * 64),
        lambda payload: payload["command"].__setitem__(0, "pythone\u0301"),
    ],
)
def test_manifest_type_order_duplicate_nfc_and_artifact_defects_refuse(mutate):
    payload = manifest("wave1_pilot")
    mutate(payload)
    with refusal("execution_approval_manifest_invalid"):
        validate(payload)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("max_retries", True),
        ("max_retries", 1),
        ("timeout_seconds", 0),
        ("timeout_seconds", 10801),
        ("timeout_seconds", 1.0),
        ("expires_at", "2026-09-14T12:00:00Z"),
    ],
)
def test_retry_timeout_and_timestamp_controls_refuse(field, replacement):
    payload = manifest("migration_apply")
    payload[field] = replacement
    with refusal("execution_approval_manifest_invalid"):
        validate(payload)


def test_wave1_effective_sql_limit_grid_has_exact_96_vectors():
    accepted = 0
    total = 0
    for max_bytes, max_rows, max_credits, model_calls in product(
        (0, 49_999_999_999, 50_000_000_000, 50_000_000_001),
        (0, 99, 100, 101),
        (0, 63, 64),
        (0, 1),
    ):
        total += 1
        payload = manifest("wave1_pilot")
        payload["limits"] = {
            "max_bytes_billed": max_bytes,
            "max_credits": max_credits,
            "max_model_calls": model_calls,
            "max_rows_written": max_rows,
        }
        expected = (
            max_bytes == 50_000_000_000
            and max_rows == 100
            and max_credits <= 63
            and model_calls == 0
        )
        try:
            validate(payload)
            actual = True
            accepted += 1
        except execution_approval.ApprovalRefusal:
            actual = False
        assert actual is expected
    assert (total, accepted) == (96, 2)


@pytest.mark.parametrize(
    "field", ["max_bytes_billed", "max_credits", "max_model_calls", "max_rows_written"]
)
@pytest.mark.parametrize("replacement", [True, 1.0, -1])
def test_wave1_limit_bool_float_and_negative_values_refuse(field, replacement):
    payload = manifest("wave1_pilot")
    payload["limits"][field] = replacement
    with refusal("execution_approval_manifest_invalid"):
        validate(payload)


@pytest.mark.parametrize(
    ("operation", "field", "maximum"),
    [
        ("collection_exposure_issue", "max_bytes_billed", 5_000_000_000),
        ("collection_exposure_issue", "max_rows_written", 1_000_000),
        ("source_collection", "max_credits", 620),
        ("source_collection", "max_bytes_billed", 1_000_000_000),
        ("daily_composition_apply", "max_bytes_billed", 10_000_000_000),
        ("daily_composition_apply", "max_model_calls", 100),
        ("daily_composition_apply", "max_rows_written", 1_000_000),
        ("legacy_chain_replay", "max_rows_written", 1_000_000),
    ],
)
def test_the_revised_daily_bounds_accept_the_maximum_and_refuse_one_over(operation, field, maximum):
    payload = manifest(operation)
    payload["limits"][field] = maximum
    assert validate(payload).limits[field] == maximum
    payload["limits"][field] = maximum + 1
    with refusal("execution_approval_manifest_invalid"):
        validate(payload)


# The daily contract revision bounded exposure; r3_apply keeps the int64 byte bound.
@pytest.mark.parametrize(("operation", "field"), [("r3_apply", "max_bytes_billed")])
def test_int64_policy_limits_accept_maximum_and_refuse_one_over(operation, field):
    payload = manifest(operation)
    assert validate(payload).limits[field] == INT64_MAX
    payload["limits"][field] = INT64_MAX + 1
    with refusal("execution_approval_manifest_invalid"):
        validate(payload)


def test_loaded_policy_snapshot_survives_file_change_and_validation_performs_no_io(
    tmp_path, monkeypatch
):
    root = tmp_path / "engine"
    target = root / "configs/open_intelligence/origin_contracts"
    target.mkdir(parents=True)
    source = ENGINE_ROOT / "configs/open_intelligence"
    registry_path = root / "configs/open_intelligence/execution_origins_v1.json"
    registry_path.write_bytes(AMENDMENT_E_REGISTRY_PATH.read_bytes())
    for policy in (source / "origin_contracts").glob("*.json"):
        (target / policy.name).write_bytes(policy.read_bytes())
    registry = load_registry(registry_path, root=root)
    for policy in target.glob("*.json"):
        policy.unlink()

    monkeypatch.setattr(
        Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError(self))
    )
    payload = manifest("wave1_pilot")
    assert validate(payload, registry=registry).operation == "wave1_pilot"
    assert execution_approval.canonical_manifest_bytes(
        payload, mode="new_consume", registry=registry
    )
    assert execution_approval.manifest_sha256(payload, mode="new_consume", registry=registry)


@pytest.mark.parametrize("operation", ["collection_exposure_issue", "source_snapshot_capture"])
def test_the_daily_row_contract_selects_its_own_generation(operation):
    """The active registry admits the daily operations only under the bridge policy; the
    retained amendment e registry still admits its own daily policy, and neither admits
    the other's."""
    payload = manifest(operation)
    assert validate(payload).contract_sha256 == DAILY_CONTRACT
    amendment_e = load_registry(AMENDMENT_E_REGISTRY_PATH)
    with refusal("execution_origin_pair_invalid"):
        validate(payload, "historical_read", registry=amendment_e)
    payload["contract_sha256"] = AMENDMENT_E_DAILY_CONTRACT
    with refusal("execution_origin_pair_invalid"):
        validate(payload)
    if operation == "collection_exposure_issue":
        admitted = validate(payload, "historical_read", registry=amendment_e)
        assert admitted.contract_sha256 == AMENDMENT_E_DAILY_CONTRACT
