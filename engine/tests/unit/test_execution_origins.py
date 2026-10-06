import hashlib
import importlib
import inspect
import json
import os
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ENGINE_ROOT / "configs/open_intelligence/execution_origins_v1.json"
CONTRACT_ROOT = ENGINE_ROOT
REGISTRY_SHA256 = "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
HISTORICAL_PROJECTION_SHA256 = "e5461b8ad97535a3328352f9c12651051bc4bcc2a6dfb9aada5d9436e2ca7d3c"
MODES = ("historical_read", "historical_replay", "new_approval", "new_consume")


def origins():
    return importlib.import_module("src.analysis.open_intelligence.execution_origins")


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path=REGISTRY, *, expected=REGISTRY_SHA256, root=CONTRACT_ROOT):
    return origins().load_origin_registry(
        path,
        expected_sha256=expected,
        contract_root=root,
    )


def copy_bundle(tmp_path):
    root = tmp_path / "engine"
    target = root / "configs/open_intelligence/origin_contracts"
    target.mkdir(parents=True)
    shutil.copyfile(REGISTRY, root / "configs/open_intelligence/execution_origins_v1.json")
    for source in (CONTRACT_ROOT / "configs/open_intelligence/origin_contracts").glob("*.json"):
        shutil.copyfile(source, target / source.name)
    return root, root / "configs/open_intelligence/execution_origins_v1.json"


def rewrite_registry(path, mutate):
    value = json.loads(path.read_bytes())
    mutate(value)
    path.write_bytes(canonical(value))
    return digest(path)


def replace_policy(root, registry, row_index, raw):
    value = json.loads(registry.read_bytes())
    row = value["rows"][row_index]
    path = root / row["contract_file"]
    path.write_bytes(raw)
    policy_digest = hashlib.sha256(raw).hexdigest()
    row["contract_sha256"] = policy_digest
    row["contract_digests"] = [policy_digest]
    registry.write_bytes(canonical(value))
    return digest(registry), path


def refusal(code):
    return pytest.raises(origins().OriginRefusal, match=f"^{code}$")


def test_public_api_exists_with_exact_signatures():
    module = origins()
    assert str(inspect.signature(module.load_origin_registry)) == (
        "(path, *, expected_sha256: str, contract_root) -> "
        "src.analysis.open_intelligence.execution_origins.OriginRegistry"
    )
    assert str(inspect.signature(module.select_origin)) == (
        "(*, manifest_version: str, contract_sha256: str, mode: "
        "Literal['historical_read', 'historical_replay', 'new_approval', "
        "'new_consume'], registry: "
        "src.analysis.open_intelligence.execution_origins.OriginRegistry) -> "
        "src.analysis.open_intelligence.execution_origins.ExecutionOrigin"
    )


def test_loader_admits_exact_reviewed_bytes_and_freezes_every_layer():
    registry = load()
    assert registry.sha256 == REGISTRY_SHA256
    assert len(registry) == 9
    assert len(registry.verified_successor_policy_digests) == 4
    first = next(iter(registry.values()))
    with pytest.raises(TypeError):
        registry[(first.manifest_version, first.contract_sha256)] = first
    with pytest.raises(TypeError):
        first.operation_bindings["other"] = first.operation_bindings[
            next(iter(first.operation_bindings))
        ]
    with pytest.raises((AttributeError, TypeError)):
        first.job_resources += ("other",)


def test_loaded_registry_state_cannot_be_replaced_or_fabricate_a_selector():
    module = origins()
    registry = load()
    original_key = next(iter(registry))
    original = registry[original_key]
    fabricated_key = ("open_intelligence_execution_manifest_v2", "f" * 64)
    replacements = {
        "_rows": MappingProxyType({fabricated_key: original}),
        "_sha256": "f" * 64,
        "_policy_digests": MappingProxyType({"fabricated.json": "f" * 64}),
        "_policy_bytes": MappingProxyType({fabricated_key: b"{}"}),
    }
    for attribute, replacement in replacements.items():
        with pytest.raises((FrozenInstanceError, AttributeError)):
            setattr(registry, attribute, replacement)
    assert registry[original_key] is original
    assert registry.sha256 == REGISTRY_SHA256
    assert len(registry.verified_successor_policy_digests) == 4
    with refusal("execution_origin_pair_invalid"):
        module.select_origin(
            manifest_version=fabricated_key[0],
            contract_sha256=fabricated_key[1],
            mode="new_consume",
            registry=registry,
        )


def test_verified_policy_bytes_are_exact_immutable_snapshots(tmp_path):
    registry = load()
    expected = {
        "0fd42bbc35e9db6e935f6fb353f7956fb6d8c433999c563285a35d96c96a9a65",
        "596920b6dd5d3d431120349af7b108ab3182337e008c449ebee1557ea69f2de4",
        "66e1a11f30c6dd8a7ca9db2110ef0af91d9a3aeda6b48d0b6323797839a06546",
        "edc805068e0297085926004b86fb1d7d30cc2158e07761439cdb6a6ed074c2d3",
    }
    assert len(registry._policy_bytes) == 4
    assert {hashlib.sha256(raw).hexdigest() for raw in registry._policy_bytes.values()} == expected
    assert all(type(raw) is bytes for raw in registry._policy_bytes.values())
    with pytest.raises(TypeError):
        registry._policy_bytes[next(iter(registry._policy_bytes))] = b"{}"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        registry._policy_bytes = MappingProxyType({})

    root, path = copy_bundle(tmp_path)
    loaded = load(path, root=root)
    snapshots = dict(loaded._policy_bytes)
    for policy in (root / "configs/open_intelligence/origin_contracts").glob("*.json"):
        policy.write_bytes(b"{}")
    assert dict(loaded._policy_bytes) == snapshots


def test_registry_cannot_be_constructed_without_verified_loader_bytes():
    module = origins()
    with refusal("execution_origin_registry_invalid"):
        module.OriginRegistry({}, "a" * 64, {})


def test_every_historical_pair_admits_only_historical_modes():
    module = origins()
    registry = load()
    historical = [row for row in registry.values() if row.manifest_version.endswith("_v1")]
    assert len(historical) == 5
    projection = [json.loads(REGISTRY.read_bytes())["rows"][index] for index in range(5)]
    assert hashlib.sha256(canonical(projection)).hexdigest() == HISTORICAL_PROJECTION_SHA256
    for row in historical:
        for mode in MODES[:2]:
            assert (
                module.select_origin(
                    manifest_version=row.manifest_version,
                    contract_sha256=row.contract_sha256,
                    mode=mode,
                    registry=registry,
                )
                is row
            )
        for mode in MODES[2:]:
            with refusal("execution_origin_mode_forbidden"):
                module.select_origin(
                    manifest_version=row.manifest_version,
                    contract_sha256=row.contract_sha256,
                    mode=mode,
                    registry=registry,
                )


def test_every_successor_operation_resolves_in_all_four_modes():
    module = origins()
    registry = load()
    seen = set()
    for row in registry.values():
        if row.manifest_version.endswith("_v1"):
            continue
        for operation, binding in row.operation_bindings.items():
            seen.add(operation)
            manifest = {
                "manifest_version": row.manifest_version,
                "contract_sha256": row.contract_sha256,
                "operation": operation,
                "job_resource": binding.job_resource,
                "service_identity": binding.service_identity,
                "datasets": list(binding.datasets),
                "image_uri": row.image_repository + "@sha256:" + "a" * 64,
            }
            for mode in MODES:
                assert module.resolve_origin(manifest, mode, registry) is row
    assert seen == {
        "migration_apply",
        "collection_exposure_issue",
        "source_snapshot_capture",
        "r3_apply",
        "r3_proof_issue",
        "r3_release",
        "brain_read",
        "wave1_pilot",
    }


@pytest.mark.parametrize("field", ["job_resource", "service_identity", "datasets", "image_uri"])
def test_resolve_refuses_each_target_mutation(field):
    module = origins()
    registry = load()
    row = next(item for item in registry.values() if item.manifest_version.endswith("_v2"))
    operation, binding = next(iter(row.operation_bindings.items()))
    manifest = {
        "manifest_version": row.manifest_version,
        "contract_sha256": row.contract_sha256,
        "operation": operation,
        "job_resource": binding.job_resource,
        "service_identity": binding.service_identity,
        "datasets": list(binding.datasets),
        "image_uri": row.image_repository + "@sha256:" + "a" * 64,
    }
    manifest[field] = ["wrong"] if field == "datasets" else "wrong"
    with refusal("execution_origin_target_invalid"):
        module.resolve_origin(manifest, "new_consume", registry)


def test_resolve_refuses_unhashable_dataset_without_leaking_python_error():
    module = origins()
    registry = load()
    row = next(item for item in registry.values() if item.manifest_version.endswith("_v2"))
    operation, binding = next(iter(row.operation_bindings.items()))
    manifest = {
        "manifest_version": row.manifest_version,
        "contract_sha256": row.contract_sha256,
        "operation": operation,
        "job_resource": binding.job_resource,
        "service_identity": binding.service_identity,
        "datasets": [{}],
        "image_uri": row.image_repository + "@sha256:" + "a" * 64,
    }
    with refusal("execution_origin_target_invalid"):
        module.resolve_origin(manifest, "new_consume", registry)


def test_shared_historical_row_never_forms_a_mixed_tuple():
    module = origins()
    registry = load()
    row = next(item for item in registry.values() if len(item.operation_bindings) == 3)
    operation, binding = next(iter(row.operation_bindings.items()))
    foreign = next(value for name, value in row.operation_bindings.items() if name != operation)
    manifest = {
        "manifest_version": row.manifest_version,
        "contract_sha256": row.contract_sha256,
        "operation": operation,
        "job_resource": binding.job_resource,
        "service_identity": foreign.service_identity,
        "datasets": list(binding.datasets),
        "image_uri": row.image_repository + "@sha256:" + "a" * 64,
    }
    with refusal("execution_origin_target_invalid"):
        module.resolve_origin(manifest, "historical_read", registry)


def test_unknown_selector_and_mode_refuse_without_echo():
    module = origins()
    registry = load()
    with refusal("execution_origin_pair_invalid"):
        module.select_origin(
            manifest_version="unknown",
            contract_sha256="a" * 64,
            mode="historical_read",
            registry=registry,
        )
    with refusal("execution_origin_pair_invalid"):
        module.select_origin(
            manifest_version="open_intelligence_execution_manifest_v1",
            contract_sha256="bad",
            mode="historical_read",
            registry=registry,
        )
    with refusal("execution_origin_mode_forbidden"):
        module.select_origin(
            manifest_version="open_intelligence_execution_manifest_v1",
            contract_sha256=next(iter(registry))[1],
            mode="unknown",
            registry=registry,
        )


def test_registry_digest_drift_refuses_before_parse(tmp_path):
    root, path = copy_bundle(tmp_path)
    path.write_bytes(path.read_bytes() + b" ")
    with refusal("execution_origin_registry_digest_mismatch"):
        load(path, root=root)


def test_missing_and_altered_policy_refuse(tmp_path):
    root, path = copy_bundle(tmp_path)
    value = json.loads(path.read_bytes())
    policy = root / next(row["contract_file"] for row in value["rows"] if row["contract_file"])
    policy.unlink()
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=digest(path), root=root)
    root, path = copy_bundle(tmp_path / "altered")
    value = json.loads(path.read_bytes())
    policy = root / next(row["contract_file"] for row in value["rows"] if row["contract_file"])
    policy.write_bytes(policy.read_bytes() + b" ")
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=digest(path), root=root)


def test_policy_and_row_origin_mismatch_refuses_with_coherent_registry_hash(tmp_path):
    root, path = copy_bundle(tmp_path)
    expected = rewrite_registry(
        path,
        lambda value: value["rows"][5].__setitem__("connected_repo", "wrong"),
    )
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


def test_approved_positional_and_enumeration_order_loads_unchanged():
    registry = load()
    assert len(registry) == 9
    migration = json.loads(
        (
            CONTRACT_ROOT
            / "configs/open_intelligence/origin_contracts/successor-migration-exposure.json"
        ).read_bytes()
    )
    assert migration["operation_validation"]["migration_apply"]["arguments"]["value"] == [
        "scripts/migrations/create_open_intelligence_v2.py",
        "apply",
    ]
    brain = json.loads(
        (
            CONTRACT_ROOT / "configs/open_intelligence/origin_contracts/successor-brain-read.json"
        ).read_bytes()
    )["operation_validation"]["brain_read"]["arguments"]
    assert brain["depths"] == ["briefing", "scan", "investigation"]
    assert brain["decision_required_for"] == ["scan", "investigation"]


CAPTURE_POLICY = "configs/open_intelligence/origin_contracts/successor-source-snapshot-capture.json"
CAPTURE_ROUTINE = "infra/bigquery_routines/sp_consume_open_intelligence_source_snapshot_v2.sql"
CAPTURE_ROW = 8
DAILY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
DAILY_IDENTITY = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
DAILY_OPERATIONS = (
    "collection_exposure_issue",
    "r3_apply",
    "r3_proof_issue",
    "r3_release",
    "source_snapshot_capture",
)


def test_amendment_d_binds_the_five_daily_operations_on_one_row_of_nine():
    """Amendment d: the daily row carries every daily operation and the r3 row is gone."""
    registry = load()
    assert len(registry) == 9
    assert len(registry.verified_successor_policy_digests) == 4
    rows = json.loads(REGISTRY.read_bytes())["rows"]
    assert len(rows) == 9
    daily = rows[CAPTURE_ROW]
    assert daily["contract_file"] == CAPTURE_POLICY
    assert tuple(daily["exact_operation_bindings"]) == DAILY_OPERATIONS
    for binding in daily["exact_operation_bindings"].values():
        assert binding == {
            "datasets": ["trends_v2_staging"],
            "job_resource": DAILY_JOB,
            "service_identity": DAILY_IDENTITY,
        }
    assert daily["job_resources"] == [DAILY_JOB]
    assert daily["service_identities"] == [DAILY_IDENTITY]
    assert daily["datasets"] == ["trends_v2_staging"]
    origin = registry[("open_intelligence_execution_manifest_v2", daily["contract_sha256"])]
    assert tuple(origin.operation_bindings) == DAILY_OPERATIONS
    policy = json.loads((CONTRACT_ROOT / CAPTURE_POLICY).read_bytes())
    assert tuple(policy["successor"]["operations"]) == DAILY_OPERATIONS
    assert tuple(policy["operation_validation"]) == DAILY_OPERATIONS
    for rule in policy["operation_validation"].values():
        assert rule["job_resource"] == DAILY_JOB
        assert rule["service_identity"] == DAILY_IDENTITY
    proof = policy["operation_validation"]["r3_proof_issue"]["arguments"]
    assert proof["execution_resource_regex"] == f"^{DAILY_JOB}/executions/[^/]+$"
    migration = json.loads(
        (
            CONTRACT_ROOT
            / "configs/open_intelligence/origin_contracts/successor-migration-exposure.json"
        ).read_bytes()
    )
    assert migration["successor"]["operations"] == ["migration_apply"]
    assert list(migration["operation_validation"]) == ["migration_apply"]
    assert [row["contract_file"] for row in rows if row["contract_file"]] == [
        "configs/open_intelligence/origin_contracts/successor-migration-exposure.json",
        "configs/open_intelligence/origin_contracts/successor-brain-read.json",
        "configs/open_intelligence/origin_contracts/successor-wave1-pilot.json",
        CAPTURE_POLICY,
    ]
    assert not (
        CONTRACT_ROOT / "configs/open_intelligence/origin_contracts/successor-r3-flow.json"
    ).exists()


def test_capture_successor_binds_the_routine_vector_kind():
    registry = load()
    policy = json.loads((CONTRACT_ROOT / CAPTURE_POLICY).read_bytes())
    rule = policy["operation_validation"]["source_snapshot_capture"]
    arguments = rule["arguments"]
    assert arguments["kind"] == "source_snapshot_capture_v2"
    assert arguments["length"] == 7
    assert arguments["prefix"] == [
        "scripts/staging/capture_protected_production_snapshot.py",
        "--cutoff-date",
    ]
    assert (arguments["mode_switch"], arguments["modes"]) == ("--mode", ["initial", "recover"])
    assert arguments["grant_switch"] == "--grant"
    assert arguments["grant_id_regex"] == "^[a-z0-9_]+$"
    assert arguments["plan_contract_version"] == "open_intelligence_protected_capture_plan_v2"
    assert arguments["consume_routine"] == "sp_consume_open_intelligence_source_snapshot_v2"
    assert arguments["consume_routine_sha256"] == digest(CONTRACT_ROOT / CAPTURE_ROUTINE)
    assert rule["input_artifact_names"] == [
        "capture_contract",
        "capture_plan",
        "recovery_context",
        "source_metadata",
        "storage_policy",
    ]
    assert rule["limits"] == {
        "max_bytes_billed": {"maximum": 1000000000, "minimum": 0},
        "max_credits": {"exact": 0},
        "max_model_calls": {"exact": 0},
        "max_rows_written": {"exact": 0},
    }
    assert policy["predecessor"] == {
        "contract_sha256": "5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f",
        "manifest_version": "open_intelligence_execution_manifest_v1",
        "operations": ["source_snapshot_capture"],
    }
    origin = registry[
        ("open_intelligence_execution_manifest_v2", digest(CONTRACT_ROOT / CAPTURE_POLICY))
    ]
    assert origin.contract_file == CAPTURE_POLICY
    assert origin.allowed_execution_modes == frozenset(MODES)
    assert set(origin.operation_bindings) == set(DAILY_OPERATIONS)
    binding = origin.operation_bindings["source_snapshot_capture"]
    assert binding.job_resource == rule["job_resource"]
    assert binding.service_identity == rule["service_identity"]
    assert binding.datasets == ("trends_v2_staging",)
    rows = json.loads(REGISTRY.read_bytes())["rows"]
    assert rows[CAPTURE_ROW]["contract_file"] == CAPTURE_POLICY
    assert rows[2]["contract_file"] is None
    assert rows[2]["contract_sha256"] == policy["predecessor"]["contract_sha256"]


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("length", 6),
        ("prefix", ["scripts/staging/capture_protected_production_snapshot.py"]),
        ("modes", ["initial"]),
        ("modes", ["initial", "recover", "initial"]),
        ("modes", ["initial", "replay"]),
        ("mode_switch", ""),
        ("grant_switch", 1),
        ("cutoff_date_regex", "\\d{4}-\\d{2}-\\d{2}"),
        ("grant_id_regex", "^[a-z0-9_]+"),
        ("consume_routine", None),
        ("consume_routine_sha256", "4432d55a"),
        ("plan_contract_version", 2),
        ("kind", "source_snapshot_capture_v3"),
        ("extra", True),
    ],
)
def test_capture_kind_malformed_fields_refuse(field, replacement, tmp_path):
    root, registry = copy_bundle(tmp_path)
    policy_path = root / CAPTURE_POLICY
    policy = json.loads(policy_path.read_bytes())
    policy["operation_validation"]["source_snapshot_capture"]["arguments"][field] = replacement
    expected, _path = replace_policy(root, registry, CAPTURE_ROW, canonical(policy))
    with refusal("execution_origin_registry_invalid"):
        load(registry, expected=expected, root=root)


@pytest.mark.parametrize("replacement", ["", 1, None])
def test_malformed_positional_argument_refuses(replacement, tmp_path):
    root, path = copy_bundle(tmp_path)
    value = json.loads(path.read_bytes())
    row_index = next(
        index
        for index, row in enumerate(value["rows"])
        if row["contract_file"] and "migration-exposure" in row["contract_file"]
    )
    policy = root / value["rows"][row_index]["contract_file"]
    parsed = json.loads(policy.read_bytes())
    parsed["operation_validation"]["migration_apply"]["arguments"]["value"][0] = replacement
    expected, _ = replace_policy(root, path, row_index, canonical(parsed))
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("depths", ["briefing", "scan", "scan"]),
        ("depths", ["briefing", "scan", "other"]),
        ("decision_required_for", ["scan", "scan"]),
        ("decision_required_for", ["scan", "other"]),
    ],
)
def test_brain_enumeration_membership_and_uniqueness_refuse(field, replacement, tmp_path):
    root, path = copy_bundle(tmp_path)
    value = json.loads(path.read_bytes())
    row_index = next(
        index
        for index, row in enumerate(value["rows"])
        if row["contract_file"] and "brain-read" in row["contract_file"]
    )
    policy = root / value["rows"][row_index]["contract_file"]
    parsed = json.loads(policy.read_bytes())
    parsed["operation_validation"]["brain_read"]["arguments"][field] = replacement
    expected, _ = replace_policy(root, path, row_index, canonical(parsed))
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


@pytest.mark.parametrize("field", ["job_resources", "service_identities", "datasets"])
def test_each_aggregate_mismatch_refuses(field, tmp_path):
    root, path = copy_bundle(tmp_path)
    expected = rewrite_registry(
        path,
        lambda value: value["rows"][5][field].append("wrong"),
    )
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


def test_duplicate_selector_and_duplicate_version_operation_refuse(tmp_path):
    root, path = copy_bundle(tmp_path)
    expected = rewrite_registry(path, lambda value: value["rows"].append(value["rows"][0]))
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)
    root, path = copy_bundle(tmp_path / "operation")

    def duplicate_operation(value):
        source = value["rows"][5]
        target = value["rows"][6]
        operation, binding = next(iter(source["exact_operation_bindings"].items()))
        target["exact_operation_bindings"][operation] = binding
        target["job_resources"] = sorted(
            {item["job_resource"] for item in target["exact_operation_bindings"].values()}
        )
        target["service_identities"] = sorted(
            {item["service_identity"] for item in target["exact_operation_bindings"].values()}
        )
        target["datasets"] = sorted(
            {
                dataset
                for item in target["exact_operation_bindings"].values()
                for dataset in item["datasets"]
            }
        )

    expected = rewrite_registry(path, duplicate_operation)
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


@pytest.mark.parametrize("kind", ["extra", "nonfinite", "duplicate"])
def test_registry_schema_and_json_faults_refuse(kind, tmp_path):
    root, path = copy_bundle(tmp_path)
    if kind == "extra":
        expected = rewrite_registry(path, lambda value: value.__setitem__("extra", True))
    elif kind == "nonfinite":
        path.write_bytes(path.read_bytes()[:-1] + b',"extra":NaN}')
        expected = digest(path)
    else:
        path.write_bytes(path.read_bytes()[:-1] + b',"rows":[]}')
        expected = digest(path)
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


def test_malformed_historical_row_uses_stable_registry_refusal(tmp_path):
    root, path = copy_bundle(tmp_path)

    def remove_selector(value):
        value["rows"][0].pop("contract_sha256")

    expected = rewrite_registry(path, remove_selector)
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


@pytest.mark.parametrize("kind", ["extra", "nonfinite", "duplicate"])
def test_policy_schema_and_json_faults_refuse(kind, tmp_path):
    root, path = copy_bundle(tmp_path)
    value = json.loads(path.read_bytes())
    row_index = next(index for index, row in enumerate(value["rows"]) if row["contract_file"])
    policy = root / value["rows"][row_index]["contract_file"]
    raw = policy.read_bytes()
    if kind == "extra":
        parsed = json.loads(raw)
        parsed["extra"] = True
        raw = canonical(parsed)
    elif kind == "nonfinite":
        raw = raw[:-1] + b',"extra":NaN}'
    else:
        raw = raw[:-1] + b',"origin":{}}'
    expected, _ = replace_policy(root, path, row_index, raw)
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    [
        (None, "purpose", 1),
        ("predecessor", "operations", 1),
        ("fresh_build_source", "repository", 1),
        ("max_retries", "exact_integer", True),
        ("canonical_json", "ensure_ascii", "false"),
        ("preservation", "new_v2_record_schemas", 1),
    ],
)
def test_policy_nested_invalid_types_refuse(section, field, replacement, tmp_path):
    root, path = copy_bundle(tmp_path)
    value = json.loads(path.read_bytes())
    row_index = next(index for index, row in enumerate(value["rows"]) if row["contract_file"])
    policy = root / value["rows"][row_index]["contract_file"]
    parsed = json.loads(policy.read_bytes())
    if section is None:
        target = parsed
    elif section == "fresh_build_source":
        target = parsed["origin"][section]
    elif section in {"max_retries", "canonical_json"}:
        target = parsed["common_manifest_validation"][section]
    else:
        target = parsed[section]
    target[field] = replacement
    expected, _ = replace_policy(root, path, row_index, canonical(parsed))
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


@pytest.mark.parametrize(
    "contract_file", ["../escape.json", "origin_contracts/file.json", "C:/escape.json"]
)
def test_policy_path_escape_and_wrong_prefix_refuse(contract_file, tmp_path):
    root, path = copy_bundle(tmp_path)
    expected = rewrite_registry(
        path,
        lambda value: value["rows"][5].__setitem__("contract_file", contract_file),
    )
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


def test_linked_registry_policy_and_ancestor_refuse(tmp_path):
    root, path = copy_bundle(tmp_path)
    real_registry = path.with_name("real.json")
    path.replace(real_registry)
    path.symlink_to(real_registry)
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=digest(real_registry), root=root)

    root, path = copy_bundle(tmp_path / "policy")
    value = json.loads(path.read_bytes())
    policy = root / next(row["contract_file"] for row in value["rows"] if row["contract_file"])
    real_policy = policy.with_name("real.json")
    policy.replace(real_policy)
    policy.symlink_to(real_policy)
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=digest(path), root=root)

    root, path = copy_bundle(tmp_path / "ancestor")
    directory = root / "configs/open_intelligence/origin_contracts"
    real_directory = directory.with_name("real-origin-contracts")
    directory.replace(real_directory)
    directory.symlink_to(real_directory, target_is_directory=True)
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=digest(path), root=root)


def test_registry_and_policy_size_bounds_refuse(tmp_path):
    root, path = copy_bundle(tmp_path)
    path.write_bytes(b"{" + b" " * (1024 * 1024))
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=digest(path), root=root)
    root, path = copy_bundle(tmp_path / "policy")
    value = json.loads(path.read_bytes())
    row_index = next(index for index, row in enumerate(value["rows"]) if row["contract_file"])
    raw = b"{" + b" " * (1024 * 1024)
    expected, _ = replace_policy(root, path, row_index, raw)
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)


def test_loader_pins_exactly_nine_rows_and_four_policy_digests(tmp_path):
    # A tenth successor row that passes every other check (its own policy file, its
    # own digest, an operation no other row binds) is refused, so the counts are
    # exact and not lower bounds; the same bundle one row short is refused too.
    root, path = copy_bundle(tmp_path)
    value = json.loads(path.read_bytes())
    row = json.loads(json.dumps(value["rows"][7]))
    assert row["manifest_version"].endswith("_v2")
    (operation, binding), = row["exact_operation_bindings"].items()
    policy = json.loads((root / row["contract_file"]).read_bytes())
    tenth = operation + "_tenth"
    row["exact_operation_bindings"] = {tenth: binding}
    policy["successor"]["operations"] = [tenth]
    policy["operation_validation"] = {tenth: policy["operation_validation"][operation]}
    raw = canonical(policy)
    row["contract_file"] = "configs/open_intelligence/origin_contracts/successor-tenth.json"
    (root / row["contract_file"]).write_bytes(raw)
    row["contract_sha256"] = hashlib.sha256(raw).hexdigest()
    row["contract_digests"] = [row["contract_sha256"]]
    expected = rewrite_registry(path, lambda value: value["rows"].append(row))
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)
    root, path = copy_bundle(tmp_path / "short")
    expected = rewrite_registry(path, lambda value: value["rows"].pop(7))
    with refusal("execution_origin_registry_invalid"):
        load(path, expected=expected, root=root)
    assert len(load()) == 9
    assert len(load().verified_successor_policy_digests) == 4


def test_all_183_private_historical_manifests_resolve():
    metadata_path = os.getenv("R03_HISTORICAL_AUTHORITY_METADATA")
    page_path = os.getenv("R03_HISTORICAL_AUTHORITY_PAGE")
    if not metadata_path or not page_path:
        pytest.skip("private historical authority inputs are not installed")
    metadata = json.loads(Path(metadata_path).read_bytes())
    fields = [item["name"] for item in metadata["schema"]["fields"]]
    page = json.loads(Path(page_path).read_bytes())
    registry = load()
    count = 0
    for row in page["rows"]:
        values = {name: cell["v"] for name, cell in zip(fields, row["f"], strict=True)}
        manifest = json.loads(values["canonical_manifest_json"])
        origin = origins().resolve_origin(manifest, "historical_read", registry)
        assert origin.manifest_version == values["manifest_version"]
        assert origin.contract_sha256 == values["contract_sha256"]
        count += 1
    assert count == int(metadata["numRows"]) == 183
