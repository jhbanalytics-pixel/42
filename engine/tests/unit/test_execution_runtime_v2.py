import copy
import dataclasses
import hashlib
import importlib
import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import execution_approval as runtime
from src.analysis.open_intelligence import execution_origins

from tests.unit.test_execution_manifest_origins import CASES, manifest
from tests.unit.test_execution_records_v2 import (
    APPROVED_BY,
    RESULT_DIGEST,
    RESULT_JSON,
    RESULT_REFERENCE,
)
from tests.unit.test_open_intelligence_execution_approval_contract import valid_manifest

# The active pair: the bridge v3 generation.
# Amendment g added the route A capture's artifact bucket to the bridge manifest, so its
# digest moved from 1643e4ce to ab7802f4; the registry stays 573deeea.
# The daily contract revision then registered three daily operations
# and bounded exposure, so the pair moved again: registry 573deeea to e6b95e35, manifest
# ab7802f4 to b7553041.
# Tightening the date regex to ASCII digits then moved them to f23001ed and 592ed45f.
REGISTRY_SHA256 = "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2"
RESOURCE_SHA256 = "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe"
PAIR = (REGISTRY_SHA256, RESOURCE_SHA256)
# Amendment e's registry, shared by the retained amendment d and amendment e pairs, and
# the daily origin row's policy under it.
AMENDMENT_E_REGISTRY_SHA256 = "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
AMENDMENT_E_DAILY_CONTRACT = "66e1a11f30c6dd8a7ca9db2110ef0af91d9a3aeda6b48d0b6323797839a06546"
AMENDMENT_E_RESOURCE_SHA256 = "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
# The v2 capture plan's inputs under the amendment e capture policy.
AMENDMENT_E_CAPTURE_ARTIFACTS = (
    "capture_contract",
    "capture_plan",
    "recovery_context",
    "source_metadata",
    "storage_policy",
)
OTHER_SHA256 = hashlib.sha256(b"another generation").hexdigest()
RETAINED_V1_CONTRACT = "250367b37ec1c086082c0863e31ac938870aae0b63a5d5def9e75127cc29d528"
APPROVAL_V1 = "open_intelligence_execution_approval_v1"
APPROVAL_V2 = "open_intelligence_execution_approval_v2"
CONSUMPTION_V2 = "open_intelligence_execution_consumption_v2"
RESULT_V1 = "open_intelligence_execution_result_v1"
RESULT_V2 = "open_intelligence_execution_result_v2"
ANNOTATION_REGISTRY = "42.ogilvy/origin-registry-sha256"
ANNOTATION_RESOURCE = "42.ogilvy/resource-manifest-sha256"
NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
APPROVED_AT = datetime(2026, 9, 13, 11, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 9, 14, 11, tzinfo=UTC)
CONSUMED_AT = NOW + timedelta(seconds=1)
COMPLETED_AT = NOW + timedelta(minutes=5)
# The generic loader issues authority for the operations the generic consume routine
# reads. Protected capture is bound to its own consume routine by its vector kind and
# stays excluded here even though the registry now binds it under v2.
SELECTED_OPERATIONS = tuple(
    operation for operation in CASES if operation != "source_snapshot_capture"
)
MODES = ("historical_read", "historical_replay", "new_approval", "new_consume")
PUBLIC_SIGNATURES = {
    "approval_id": "(manifest_sha256: str, approved_by: str, approved_at: datetime.datetime) -> str",
    "approval_id_v2": "(manifest_sha256, approved_by, approved_at, *, origin_registry_sha256, resource_manifest_sha256)",
    "build_provenance_from_response": "(payload: object, operation_contract_sha256: str, *, manifest_version, mode, registry) -> src.analysis.open_intelligence.execution_approval.BuildProvenanceReceipt",
    "canonical_approval_v2_bytes": "(record, *, mode, registry, expected_resource_manifest_sha256)",
    "canonical_build_provenance_bytes": "(receipt: src.analysis.open_intelligence.execution_approval.BuildProvenanceReceipt, *, origin) -> bytes",
    "canonical_consumption_v2_bytes": "(record, *, mode, registry, expected_resource_manifest_sha256)",
    "canonical_execution_job": "(value: object, job_resource: str) -> str",
    "canonical_manifest_bytes": "(payload: object, *, mode, registry) -> bytes",
    "canonical_result_v2_bytes": "(record, *, mode, registry, expected_resource_manifest_sha256)",
    "consumption_id": "(approval_id: str, execution_name: str, consumed_at: datetime.datetime) -> str",
    "consumption_id_v2": "(approval_id, execution_name, consumed_at, *, origin_registry_sha256, resource_manifest_sha256)",
    "manifest_sha256": "(payload: object, *, mode, registry) -> str",
    "normalize_cloud_build_response": "(payload: object, *, origin) -> dict[str, object]",
    "result_id": "(consumption_id: str, result_reference: str, result_digest: str, status: str, completed_at: datetime.datetime) -> str",
    "result_id_v2": "(consumption_id, result_reference, result_digest, status, completed_at, *, origin_registry_sha256, resource_manifest_sha256)",
    "validate_execution_chain_v2": "(approval, consumption, result, *, mode, registry, expected_resource_manifest_sha256) -> None",
    "validate_execution_manifest": "(payload: object, *, mode, registry) -> src.analysis.open_intelligence.execution_approval.ExecutionManifest",
}
V1_ID_VECTORS = (
    "exa_95d1cc4ab11695e19de1fba68132bf1aaa12be26adfde594dcaf9f1ea824f269",
    "exc_51b46b24477643e4149a70c70766a66cc29d9d9fd8cce83c3ecf0993e6416273",
    "exr_90551a83f29097dc899be047afc95a069b995ab9b728ac7f61ea365e444b25a4",
)


def generations():
    return importlib.import_module("src.analysis.open_intelligence.execution_generations")


def registry():
    return generations().active_generation().registry


def refusal(code):
    return pytest.raises(
        (runtime.ApprovalRefusal, execution_origins.OriginRefusal), match=f"^{code}$"
    )


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def phrase_sha(operation, manifest_sha, registry_sha=REGISTRY_SHA256, resource_sha=RESOURCE_SHA256):
    phrase = (
        "I approve one 42 staging execution of "
        f"{operation} for manifest SHA256 {manifest_sha}, "
        f"origin registry SHA256 {registry_sha} and "
        f"resource manifest SHA256 {resource_sha}. Production remains unchanged."
    )
    return sha256(phrase.encode())


def context(mode="new_consume", resource_sha=RESOURCE_SHA256):
    return {
        "mode": mode,
        "registry": registry(),
        "expected_resource_manifest_sha256": resource_sha,
    }


def record_fields(record):
    return {field.name: getattr(record, field.name) for field in dataclasses.fields(record)}


def fixture(
    operation="r3_apply",
    *,
    resource_sha=RESOURCE_SHA256,
    registry_sha=REGISTRY_SHA256,
    reg=None,
    contract_sha=None,
    artifacts=None,
):
    reg = registry() if reg is None else reg
    payload = manifest(operation)
    if contract_sha is not None:
        payload["contract_sha256"] = contract_sha
    if artifacts is not None:
        payload["input_artifacts"] = [{"name": name, "sha256": "d" * 64} for name in artifacts]
    payload["expires_at"] = stamp(EXPIRES_AT)
    origin = execution_origins.select_origin(
        manifest_version=payload["manifest_version"],
        contract_sha256=payload["contract_sha256"],
        mode="new_consume",
        registry=reg,
    )
    image_digest = payload["image_uri"].rsplit("@", 1)[1]
    build = {
        "name": payload["build_resource"],
        "id": payload["build_resource"].rsplit("/", 1)[1],
        "projectId": "ogilvy-trends-v2",
        "status": "SUCCESS",
        "results": {
            "images": [
                {
                    "name": f"{origin.image_repository}:{payload['source_sha']}",
                    "digest": image_digest,
                }
            ]
        },
        "finishTime": "2026-09-13T10:00:00.000000Z",
        "source": {
            "connectedRepository": {
                "repository": origin.connected_repo,
                "revision": payload["source_sha"],
            }
        },
    }
    receipt = runtime.build_provenance_from_response(
        build,
        payload["contract_sha256"],
        manifest_version=payload["manifest_version"],
        mode="new_consume",
        registry=reg,
    )
    contents = {
        item["name"]: json.dumps({"artifact": item["name"]}).encode()
        for item in payload["input_artifacts"]
    }
    contents["build_provenance"] = runtime.canonical_build_provenance_bytes(receipt, origin=origin)
    for item in payload["input_artifacts"]:
        item["sha256"] = sha256(contents[item["name"]])
    canonical = runtime.canonical_manifest_bytes(payload, mode="new_consume", registry=reg)
    digest = sha256(canonical)
    approval = runtime.ExecutionApprovalV2(
        approval_contract_version=APPROVAL_V2,
        approval_id=runtime.approval_id_v2(
            digest,
            APPROVED_BY,
            APPROVED_AT,
            origin_registry_sha256=registry_sha,
            resource_manifest_sha256=resource_sha,
        ),
        manifest_version=payload["manifest_version"],
        operation=operation,
        contract_sha256=payload["contract_sha256"],
        manifest_sha256=digest,
        canonical_manifest_json=canonical.decode(),
        approved_by=APPROVED_BY,
        approved_at=APPROVED_AT,
        expires_at=EXPIRES_AT,
        approval_phrase_sha256=phrase_sha(
            operation, digest, registry_sha=registry_sha, resource_sha=resource_sha
        ),
        origin_registry_sha256=registry_sha,
        resource_manifest_sha256=resource_sha,
        **{**context(resource_sha=resource_sha), "registry": reg},
    )
    env = list(payload["environment"]) + [
        {"name": name, "valueSource": {"secretKeyRef": {"secret": name, "version": "1"}}}
        for name in payload["secrets"]
    ]
    task = {
        "serviceAccount": payload["service_identity"],
        "maxRetries": 0,
        "timeout": "900s",
        "containers": [
            {
                "image": payload["image_uri"],
                "command": payload["command"],
                "args": payload["arguments"],
                "env": env,
            }
        ],
    }
    names = (
        runtime._BRAIN_DURABLE_ANNOTATIONS
        if operation == "brain_read"
        else runtime._DURABLE_ANNOTATIONS
    )
    annotations = dict.fromkeys(names, "c" * 64)
    annotations["42.ogilvy/execution-approval-sha256"] = digest
    annotations["42.ogilvy/source-sha"] = payload["source_sha"]
    annotations[ANNOTATION_REGISTRY] = registry_sha
    annotations[ANNOTATION_RESOURCE] = RESOURCE_SHA256
    execution = {
        "name": payload["job_resource"] + "/executions/example-1",
        "job": payload["job_resource"],
        "annotations": dict(annotations),
        "template": copy.deepcopy(task),
    }
    job = {
        "name": payload["job_resource"],
        "template": {"annotations": dict(annotations), "template": copy.deepcopy(task)},
    }
    return SimpleNamespace(
        operation=operation,
        payload=payload,
        approval=approval,
        execution=execution,
        job=job,
        build=build,
        contents=contents,
        digest=digest,
        registry=reg,
        origin=origin,
    )


def amendment_e_capture_fixture(monkeypatch):
    """A v2 plan capture under the amendment e generation, made the active pair for the
    test as it was before the bridge generation: its capture policy takes the v2 plan and
    the v2 dedicated consume routine."""
    pair = (AMENDMENT_E_REGISTRY_SHA256, AMENDMENT_E_RESOURCE_SHA256)
    monkeypatch.setattr(generations(), "ACTIVE_GENERATION_PAIR", pair)
    fx = fixture(
        "source_snapshot_capture",
        resource_sha=AMENDMENT_E_RESOURCE_SHA256,
        registry_sha=AMENDMENT_E_REGISTRY_SHA256,
        reg=generations().active_generation().registry,
        contract_sha=AMENDMENT_E_DAILY_CONTRACT,
        artifacts=AMENDMENT_E_CAPTURE_ARTIFACTS,
    )
    for annotations in (fx.execution["annotations"], fx.job["template"]["annotations"]):
        annotations[ANNOTATION_RESOURCE] = AMENDMENT_E_RESOURCE_SHA256
    return fx


def load(fx, **overrides):
    kwargs = {
        "mode": "new_consume",
        "execution_reader": lambda: {"execution": fx.execution, "job": fx.job},
        "approval_reader": lambda digest: fx.approval,
        "build_reader": lambda name: fx.build,
        "artifact_reader": lambda name: fx.contents[name],
        "now": lambda: NOW,
    }
    kwargs.update(overrides)
    return runtime._load_execution_authority(fx.operation, **kwargs)


def consumption_row(fx, request, *, resource_sha=RESOURCE_SHA256):
    return {
        **request,
        "consumption_contract_version": CONSUMPTION_V2,
        "consumption_id": runtime.consumption_id_v2(
            request["approval_id"],
            request["execution_name"],
            CONSUMED_AT,
            origin_registry_sha256=request["origin_registry_sha256"],
            resource_manifest_sha256=resource_sha,
        ),
        "consumed_at": CONSUMED_AT,
        "resource_manifest_sha256": resource_sha,
    }


def consume(fx, authority):
    return runtime._consume_execution_authority(
        authority, consumption_writer=lambda request: consumption_row(fx, request)
    )


def result_row(request, *, resource_sha=RESOURCE_SHA256, status=None):
    status = request["status"] if status is None else status
    return {
        **request,
        "result_contract_version": RESULT_V2,
        "result_id": runtime.result_id_v2(
            request["consumption_id"],
            request["result_reference"],
            request["result_digest"],
            status,
            COMPLETED_AT,
            origin_registry_sha256=request["origin_registry_sha256"],
            resource_manifest_sha256=resource_sha,
        ),
        "status": status,
        "completed_at": COMPLETED_AT,
        "resource_manifest_sha256": resource_sha,
    }


def parameters(items):
    return [(item.name, item.type_, item.value) for item in items]


def v1_approval():
    reg = registry()
    payload = valid_manifest("brain_read")
    payload["contract_sha256"] = RETAINED_V1_CONTRACT
    canonical = runtime.canonical_manifest_bytes(payload, mode="historical_read", registry=reg)
    digest = sha256(canonical)
    approved_at = datetime(2026, 8, 31, 11, tzinfo=UTC)
    expires_at = datetime.strptime(payload["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=UTC
    )
    phrase = (
        "I approve one staging execution of brain_read for manifest SHA256 "
        f"{digest}. Production remains unchanged."
    )
    return runtime.ExecutionApproval(
        approval_contract_version=APPROVAL_V1,
        approval_id=runtime.approval_id(digest, APPROVED_BY, approved_at),
        manifest_version="open_intelligence_execution_manifest_v1",
        operation="brain_read",
        contract_sha256=RETAINED_V1_CONTRACT,
        manifest_sha256=digest,
        canonical_manifest_json=canonical.decode(),
        approved_by=APPROVED_BY,
        approved_at=approved_at,
        expires_at=expires_at,
        approval_phrase_sha256=sha256(phrase.encode()),
    )


def test_public_signatures_and_v1_identifiers_are_frozen():
    """Supplement: 'Existing v1 classes and ID functions remain behaviorally unchanged.'
    Successor 1: 'Preserve ... every historical approval, consumption and result byte.'"""
    for name, expected in PUBLIC_SIGNATURES.items():
        assert str(inspect.signature(getattr(runtime, name))) == expected, name
    assert sorted(runtime.__all__) == sorted(
        [
            *PUBLIC_SIGNATURES,
            "ApprovalRefusal",
            "BuildProvenanceReceipt",
            "ExecutionApproval",
            "ExecutionApprovalV2",
            "ExecutionConsumption",
            "ExecutionConsumptionV2",
            "ExecutionManifest",
            "ExecutionResult",
            "ExecutionResultV2",
        ]
    )
    digest = "a" * 64
    approval = runtime.approval_id(digest, APPROVED_BY, NOW)
    execution = "projects/ogilvy-trends-v2/locations/us-central1/jobs/x/executions/y"
    consumption = runtime.consumption_id(approval, execution, NOW)
    result = runtime.result_id(consumption, "gs://x/y", digest, "succeeded", NOW)
    assert (approval, consumption, result) == V1_ID_VECTORS
    for name in ("_load_execution_authority", "_consume_execution_authority"):
        assert name not in runtime.__all__


def test_loader_requires_explicit_new_consume_mode():
    """Original contract: 'Fresh runtime load/consume uses explicit new_consume with an
    admitted v2 row.' Caller closure: 'No caller may infer new_consume from an operation
    name, environment variable, current registry, or presence of a v2 row.'"""
    fx = fixture()
    calls = []
    with pytest.raises(TypeError):
        runtime._load_execution_authority(
            fx.operation, execution_reader=lambda: calls.append("execution")
        )
    for mode in ("historical_read", "historical_replay", "new_approval", "", None, "NEW_CONSUME"):
        with refusal("execution_origin_mode_forbidden"):
            load(
                fx,
                mode=mode,
                execution_reader=lambda: calls.append("execution"),
                approval_reader=lambda digest: calls.append("approval"),
            )
    assert calls == []


@pytest.mark.parametrize("name", [ANNOTATION_REGISTRY, ANNOTATION_RESOURCE])
def test_loader_refuses_missing_generation_annotation(name):
    """Successor 1: 'A v2 deployed job and its execution template carry the existing
    operation-specific annotations plus exactly two new required annotations ... An unknown
    pair, missing annotation, changed job template or wrong record generation refuses.'"""
    fx = fixture()
    reads = []
    del fx.execution["annotations"][name]
    del fx.job["template"]["annotations"][name]
    with refusal("execution_approval_execution_mismatch"):
        load(fx, approval_reader=lambda digest: reads.append(digest))
    assert reads == []


@pytest.mark.parametrize(
    "mutation",
    ["job_differs", "execution_differs", "uppercase", "short", "not_string", "extra"],
)
def test_loader_requires_exact_annotation_equality_before_reading_approval(mutation):
    """Successor 1: 'Both are lowercase 64-hex. Runtime requires exact annotation equality
    between the observed execution and job template, before reading its approval.'"""
    fx = fixture()
    reads = []
    execution = fx.execution["annotations"]
    job = fx.job["template"]["annotations"]
    if mutation == "job_differs":
        job[ANNOTATION_RESOURCE] = OTHER_SHA256
    elif mutation == "execution_differs":
        execution[ANNOTATION_REGISTRY] = OTHER_SHA256
    elif mutation == "uppercase":
        execution[ANNOTATION_RESOURCE] = job[ANNOTATION_RESOURCE] = RESOURCE_SHA256.upper()
    elif mutation == "short":
        execution[ANNOTATION_REGISTRY] = job[ANNOTATION_REGISTRY] = REGISTRY_SHA256[:63]
    elif mutation == "not_string":
        execution[ANNOTATION_REGISTRY] = job[ANNOTATION_REGISTRY] = 7
    elif mutation == "extra":
        execution["42.ogilvy/extra"] = job["42.ogilvy/extra"] = "x"
    with refusal("execution_approval_execution_mismatch"):
        load(fx, approval_reader=lambda digest: reads.append(digest))
    assert reads == []


def test_loader_refuses_unknown_pair_before_reading_approval():
    """Successor 2: 'Unknown catalogue pair refuses even if a native table accepted it.'"""
    fx = fixture()
    reads = []
    for name in (ANNOTATION_REGISTRY, ANNOTATION_RESOURCE):
        execution = dict(fx.execution["annotations"])
        job = dict(fx.job["template"]["annotations"])
        execution[name] = job[name] = OTHER_SHA256
        fx.execution["annotations"] = execution
        fx.job["template"]["annotations"] = job
        with refusal("execution_generation_unknown"):
            load(fx, approval_reader=lambda digest: reads.append(digest))
        fx.execution["annotations"][name] = fx.job["template"]["annotations"][name] = (
            REGISTRY_SHA256 if name == ANNOTATION_REGISTRY else RESOURCE_SHA256
        )
    assert reads == []


def test_loader_refuses_a_retained_but_inactive_pair(monkeypatch):
    """Successor 1: 'New approval and consumption require the active pair; old v2 result
    reconciliation can use its still-trusted consumed pair.'"""
    fx = fixture()
    reads = []
    monkeypatch.setattr(generations(), "ACTIVE_GENERATION_PAIR", (REGISTRY_SHA256, OTHER_SHA256))
    with refusal("execution_generation_inactive"):
        load(fx, approval_reader=lambda digest: reads.append(digest))
    assert reads == []


def test_loader_refuses_wrong_record_generation_before_manifest_or_artifact_use():
    """Successor 1: 'It verifies the returned record carries the same pair before any
    manifest, artifact or authority use.'"""
    fx = fixture()
    other = fixture(resource_sha=OTHER_SHA256)
    artifacts = []
    builds = []
    with refusal("execution_approval_schema_mismatch"):
        load(
            fx,
            approval_reader=lambda digest: other.approval,
            build_reader=lambda name: builds.append(name),
            artifact_reader=lambda name: artifacts.append(name),
        )
    assert builds == []
    assert artifacts == []


@pytest.mark.parametrize("family", ["v1_record", "v1_shaped", "mapping", "none"])
def test_loader_refuses_wrong_family_records(family):
    """Successor 1: 'Tests include wrong-family records, old jobs under v2, new jobs under
    v1, wrong upstream apply identity, direct-table ambiguity and attempted cross-generation
    reuse.'"""
    fx = fixture()
    value = {
        "v1_record": lambda: v1_approval(),
        "v1_shaped": lambda: SimpleNamespace(**record_fields(v1_approval())),
        "mapping": lambda: record_fields(fx.approval),
        "none": lambda: None,
    }[family]()
    with refusal("execution_approval_unavailable"):
        load(fx, approval_reader=lambda digest: value)


def test_old_job_without_generation_annotations_refuses_under_v2():
    fx = fixture()
    for name in (ANNOTATION_REGISTRY, ANNOTATION_RESOURCE):
        del fx.execution["annotations"][name]
        del fx.job["template"]["annotations"][name]
    with refusal("execution_approval_execution_mismatch"):
        load(fx)


@pytest.mark.parametrize("operation", SELECTED_OPERATIONS)
def test_loader_issues_authority_for_each_selected_operation(operation):
    """Successor 1: 'Seven fresh operations are selected by the current approved registry.
    Bootstrap and protected capture remain excluded.'"""
    fx = fixture(operation)
    authority = load(fx)
    assert authority.operation == operation
    assert authority.manifest.operation == operation
    assert authority.approval is not fx.approval
    assert record_fields(authority.approval) == record_fields(fx.approval)
    assert authority.execution_name == fx.execution["name"]
    assert authority.job_resource == fx.payload["job_resource"]
    assert authority.source_sha == fx.payload["source_sha"]
    assert authority.image_uri == fx.payload["image_uri"]
    generation = authority.generation
    assert type(generation) is generations().TrustedGeneration
    assert (generation.origin_registry_sha256, generation.resource_manifest_sha256) == PAIR


@pytest.mark.parametrize("operation", ["source_snapshot_capture", "bootstrap_migration_apply"])
def test_loader_refuses_excluded_operations_before_reading_approval(operation):
    fx = fixture()
    fx.operation = operation
    reads = []
    with refusal("execution_approval_manifest_invalid"):
        load(fx, approval_reader=lambda digest: reads.append(digest))
    assert reads == []


def test_loader_refuses_approval_for_another_operation_or_digest():
    fx = fixture()
    other = fixture("r3_release")
    with refusal("execution_approval_execution_mismatch"):
        load(fx, approval_reader=lambda digest: other.approval)
    fx.execution["annotations"]["42.ogilvy/execution-approval-sha256"] = other.digest
    fx.job["template"]["annotations"]["42.ogilvy/execution-approval-sha256"] = other.digest
    with refusal("execution_approval_execution_mismatch"):
        load(fx)


@pytest.mark.parametrize("mutation", ["template", "job_name", "execution_job", "expired"])
def test_loader_preserves_runtime_execution_checks(mutation):
    """Original contract: 'Preserve per-artifact regeneration and digest checks, runtime
    principal/job/image/source comparisons, task/environment bounds ...'"""
    fx = fixture()
    if mutation == "template":
        fx.job["template"]["template"]["timeout"] = "901s"
    elif mutation == "job_name":
        fx.job["name"] = fx.payload["job_resource"] + "-other"
    elif mutation == "execution_job":
        fx.execution["job"] = "intelligence-42-other-staging"
    with refusal("execution_approval_execution_mismatch"):
        load(fx, now=lambda: EXPIRES_AT if mutation == "expired" else NOW)


def test_loader_preserves_artifact_regeneration_checks():
    fx = fixture()
    contents = dict(fx.contents)
    contents["config"] = b"{}"
    with refusal("execution_approval_artifact_mismatch"):
        load(fx, artifact_reader=lambda name: contents[name])
    build = copy.deepcopy(fx.build)
    build["results"]["images"][0]["digest"] = "sha256:" + "e" * 64
    with refusal("execution_approval_artifact_mismatch"):
        load(fx, build_reader=lambda name: build)


def test_runtime_task_binds_the_exact_numeric_secret_version():
    """Successor 1: 'The Cloud Run task binds the approved secret name and exact numeric
    version string. Job and execution readback must match this exact tuple. ... An absent
    secret or latest refuses.'"""
    fx = fixture("wave1_pilot")
    authority = load(fx)
    assert authority.manifest.secrets == ("SOCIALCRAWL_OGILVY_API_KEY",)
    for version in ("latest", "2", 1, "01", ""):
        mutated = fixture("wave1_pilot")
        for template in (
            mutated.execution["template"],
            mutated.job["template"]["template"],
        ):
            template["containers"][0]["env"][-1]["valueSource"]["secretKeyRef"]["version"] = version
        with refusal("execution_approval_execution_mismatch"):
            load(mutated)


def thaw(value):
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def fabricated_generation(change):
    module = generations()
    generation = module.active_generation()
    payload = thaw(generation.resource_manifest)
    change(payload)
    return dataclasses.replace(generation, resource_manifest=module._freeze(payload))


SECRET_VERSION = (
    "//secretmanager.googleapis.com/projects/ogilvy-trends-v2/secrets/"
    "SOCIALCRAWL_OGILVY_API_KEY/versions/"
)


@pytest.mark.parametrize(
    ("operation", "change", "code"),
    [
        (
            "wave1_pilot",
            lambda payload: payload["resources"].append(
                {"name": SECRET_VERSION + "2", "actions": ["read"]}
            ),
            "execution_approval_target_invalid",
        ),
        (
            "wave1_pilot",
            lambda payload: payload["resources"].remove(
                {"name": SECRET_VERSION + "1", "actions": ["read"]}
            ),
            "execution_approval_target_invalid",
        ),
        (
            "wave1_pilot",
            lambda payload: payload["resources"].append(
                {"name": SECRET_VERSION + "latest", "actions": ["read"]}
            ),
            "execution_approval_target_invalid",
        ),
        (
            "r3_apply",
            lambda payload: payload["resources"].remove(
                {
                    "name": "//run.googleapis.com/projects/ogilvy-trends-v2/locations/"
                    "us-central1/jobs/intelligence-42-daily-staging",
                    "actions": ["deploy", "invoke", "read"],
                }
            ),
            "execution_approval_target_invalid",
        ),
        (
            "r3_apply",
            lambda payload: payload["resources"].remove(
                {
                    "name": "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/"
                    "trends_v2_staging",
                    "actions": ["read", "write"],
                }
            ),
            "execution_approval_target_invalid",
        ),
        (
            "r3_apply",
            lambda payload: payload["identities"].__setitem__(
                "execution_r3_apply", payload["identities"]["execution_migration_apply"]
            ),
            "execution_approval_identity_invalid",
        ),
        (
            "r3_apply",
            lambda payload: payload["identities"].pop("execution_r3_apply"),
            "execution_approval_identity_invalid",
        ),
    ],
)
def test_runtime_refuses_resources_outside_the_selected_generation(
    monkeypatch, operation, change, code
):
    """Successor 1: 'Multiple approved numeric versions for the same required secret are
    ambiguous and refuse this execution' and 'Deployment/runtime use the existing
    exact-membership guard for static resources it supports, with explicit
    provider-qualified resource names.'"""
    fx = fixture(operation)
    assert load(fx)
    fabricated = fabricated_generation(change)
    monkeypatch.setattr(
        runtime.execution_generations,
        "require_active_generation",
        lambda registry_sha, resource_sha: fabricated,
    )
    with refusal(code):
        load(fx)


def test_consume_calls_the_v2_procedure_with_both_digests_and_binds_one_use():
    """Successor 1: 'consume takes manifest digest, execution, job, source, image then both'
    and caller closure: 'Preserve one-use capability binding and registry equality.'"""
    fx = fixture()
    authority = load(fx)
    calls = []

    def query(procedure, params, *, allow_zero=False):
        calls.append((procedure, parameters(params), allow_zero))
        request = {item.name: item.value for item in params}
        row = consumption_row(
            fx,
            {
                **request,
                "approval_id": fx.approval.approval_id,
                "operation": fx.operation,
            },
        )
        return SimpleNamespace(**row)

    consumption = runtime._consume_execution_authority(
        authority,
        consumption_writer=partial(
            runtime._default_consumption_writer,
            version=CONSUMPTION_V2,
            mode="new_consume",
            query=query,
        ),
    )
    assert type(consumption) is runtime.ExecutionConsumptionV2
    assert calls == [
        (
            "sp_consume_open_intelligence_execution_v2",
            [
                ("manifest_sha256", "STRING", fx.digest),
                ("execution_name", "STRING", fx.execution["name"]),
                ("job_resource", "STRING", fx.payload["job_resource"]),
                ("source_sha", "STRING", fx.payload["source_sha"]),
                ("image_uri", "STRING", fx.payload["image_uri"]),
                ("origin_registry_sha256", "STRING", REGISTRY_SHA256),
                ("resource_manifest_sha256", "STRING", RESOURCE_SHA256),
            ],
            False,
        )
    ]
    assert (consumption.origin_registry_sha256, consumption.resource_manifest_sha256) == PAIR
    assert consumption.consumed_at == CONSUMED_AT
    with refusal("execution_approval_consumed"):
        consume(fx, authority)
    assert runtime._require_consumed_execution(authority, consumption, fx.operation) is consumption
    with refusal("execution_approval_identity_invalid"):
        runtime._require_consumed_execution(authority, consumption, "r3_release")


def test_consume_refuses_a_row_from_another_generation_or_family():
    """Successor 1: 'All linked records must agree on both' digests. Successor 2: 'No
    authority object or accepted result is created before this admission.'"""
    fx = fixture()
    authority = load(fx)
    with refusal("execution_approval_schema_mismatch"):
        runtime._consume_execution_authority(
            authority,
            consumption_writer=lambda request: consumption_row(
                fx, request, resource_sha=OTHER_SHA256
            ),
        )
    with refusal("execution_approval_consumed"):
        consume(fx, authority)
    second = load(fixture())

    def v1_row(request):
        row = consumption_row(fx, request)
        row["consumption_contract_version"] = "open_intelligence_execution_consumption_v1"
        return row

    with refusal("execution_approval_schema_mismatch"):
        runtime._consume_execution_authority(second, consumption_writer=v1_row)


def test_consumption_writer_refuses_v1_and_non_consume_modes():
    """Original contract: 'A fresh v1 request refuses before dispatch or mutation.'"""
    fx = fixture()
    authority = load(fx)
    calls = []
    request = {"manifest_sha256": fx.digest}
    for version, mode in (
        ("open_intelligence_execution_consumption_v1", "new_consume"),
        ("open_intelligence_execution_consumption_v1", "historical_replay"),
        (CONSUMPTION_V2, "historical_replay"),
        (CONSUMPTION_V2, "new_approval"),
    ):
        with refusal("execution_origin_mode_forbidden"):
            runtime._default_consumption_writer(
                request, version=version, mode=mode, query=lambda *args, **kwargs: calls.append(1)
            )
    with pytest.raises(TypeError):
        runtime._default_consumption_writer(request)
    with refusal("execution_approval_schema_mismatch"):
        runtime._default_consumption_writer(
            request, version="other", mode="new_consume", query=lambda *a, **k: calls.append(1)
        )
    assert calls == []
    assert authority.operation == fx.operation


def test_record_result_calls_the_v2_procedure_and_reconciles_by_the_same_pair():
    """Successor 1: 'result write takes consumption, reference, result JSON, result digest,
    status then both' and 'Existing consumed-result reconciliation verifies the original
    consumption and exact recorded generation.'"""
    fx = fixture()
    authority = load(fx)
    consumption = consume(fx, authority)
    calls = []
    durable = []

    def query(procedure, params, *, allow_zero=False):
        calls.append((procedure, parameters(params), allow_zero))
        if allow_zero:
            return [SimpleNamespace(**row) for row in durable]
        request = {item.name: item.value for item in params}
        row = result_row(
            {
                **request,
                "approval_id": consumption.approval_id,
                "manifest_sha256": consumption.manifest_sha256,
                "operation": consumption.operation,
                "execution_name": consumption.execution_name,
            }
        )
        durable.append(row)
        return SimpleNamespace(**_procedure_reply(procedure, row))

    result = runtime._record_execution_result(
        authority,
        consumption,
        RESULT_REFERENCE,
        RESULT_JSON,
        RESULT_DIGEST,
        "succeeded",
        result_writer=partial(
            runtime._default_result_writer, version=RESULT_V2, mode="new_consume", query=query
        ),
        result_reader=partial(
            runtime._default_result_reader, version=RESULT_V2, mode="new_consume", query=query
        ),
    )
    assert type(result) is runtime.ExecutionResultV2
    assert (result.origin_registry_sha256, result.resource_manifest_sha256) == PAIR
    assert calls == [
        (
            "sp_record_open_intelligence_execution_result_v2",
            [
                ("consumption_id", "STRING", consumption.consumption_id),
                ("result_reference", "STRING", RESULT_REFERENCE),
                ("canonical_result_json", "STRING", RESULT_JSON),
                ("result_digest", "STRING", RESULT_DIGEST),
                ("status", "STRING", "succeeded"),
                ("origin_registry_sha256", "STRING", REGISTRY_SHA256),
                ("resource_manifest_sha256", "STRING", RESOURCE_SHA256),
            ],
            False,
        ),
        (
            "sp_read_open_intelligence_execution_result_v2",
            [("p_consumption_id", "STRING", consumption.consumption_id)],
            True,
        ),
    ]


def test_record_result_reconciliation_requires_the_recorded_generation():
    fx = fixture()
    authority = load(fx)
    consumption = consume(fx, authority)

    def failing(request):
        raise RuntimeError("write lost")

    reads = []

    def reader(consumption_id):
        reads.append(consumption_id)
        return [
            result_row(
                {
                    "consumption_id": consumption.consumption_id,
                    "approval_id": consumption.approval_id,
                    "manifest_sha256": consumption.manifest_sha256,
                    "operation": consumption.operation,
                    "execution_name": consumption.execution_name,
                    "result_reference": RESULT_REFERENCE,
                    "canonical_result_json": RESULT_JSON,
                    "result_digest": RESULT_DIGEST,
                    "status": "succeeded",
                    "origin_registry_sha256": REGISTRY_SHA256,
                },
                resource_sha=OTHER_SHA256,
            )
        ]

    with refusal("execution_result_conflict"):
        runtime._record_execution_result(
            authority,
            consumption,
            RESULT_REFERENCE,
            RESULT_JSON,
            RESULT_DIGEST,
            "succeeded",
            result_writer=failing,
            result_reader=reader,
        )
    assert reads == [consumption.consumption_id]


def test_record_result_recovers_the_same_pair_row_after_a_lost_write():
    fx = fixture()
    authority = load(fx)
    consumption = consume(fx, authority)
    attempts = []

    def flaky(request):
        attempts.append(dict(request))
        raise RuntimeError("write lost")

    def reader(consumption_id):
        return [SimpleNamespace(**result_row(attempts[-1]))]

    result = runtime._record_execution_result(
        authority,
        consumption,
        RESULT_REFERENCE,
        RESULT_JSON,
        RESULT_DIGEST,
        "failed",
        result_writer=flaky,
        result_reader=reader,
    )
    assert result.status == "failed"
    assert result.consumption_id == consumption.consumption_id
    assert attempts[-1]["origin_registry_sha256"] == REGISTRY_SHA256
    assert attempts[-1]["resource_manifest_sha256"] == RESOURCE_SHA256


def test_default_readers_require_version_and_explicit_mode():
    """Caller closure: 'record reconstruction and default SQL read or write adapters |
    Exact version plus explicit mode'."""
    calls = []

    def query(*args, **kwargs):
        calls.append(args)

    with pytest.raises(TypeError):
        runtime._default_approval_reader("a" * 64, query=query)
    with pytest.raises(TypeError):
        runtime._default_result_reader("exc_" + "a" * 64, query=query)
    with pytest.raises(TypeError):
        runtime._default_result_writer({}, query=query)
    for version in (APPROVAL_V1, APPROVAL_V2):
        for mode in ("", None, "consume", "NEW_CONSUME"):
            with refusal("execution_origin_mode_forbidden"):
                runtime._default_approval_reader("a" * 64, version=version, mode=mode, query=query)
    for mode in ("new_approval", "new_consume"):
        with refusal("execution_origin_mode_forbidden"):
            runtime._default_approval_reader("a" * 64, version=APPROVAL_V1, mode=mode, query=query)
        with refusal("execution_origin_mode_forbidden"):
            runtime._default_result_reader(
                "exc_" + "a" * 64, version=RESULT_V1, mode=mode, query=query
            )
    for version in ("", None):
        with refusal("execution_approval_schema_mismatch"):
            runtime._default_approval_reader(
                "a" * 64, version=version, mode="historical_read", query=query
            )
    for version in ("open_intelligence_execution_approval_v3", "", None):
        with refusal("execution_approval_schema_mismatch"):
            runtime._default_result_reader(
                "exc_" + "a" * 64, version=version, mode="historical_read", query=query
            )
    assert calls == []
    # The v3 reader version is the one selector past v2: it names the v3 read routine and
    # hands an unreadable row to the same schema refusal.
    with refusal("execution_approval_schema_mismatch"):
        runtime._default_approval_reader(
            "a" * 64,
            version="open_intelligence_execution_approval_v3",
            mode="historical_read",
            query=query,
        )
    assert [call[0] for call in calls] == ["sp_read_open_intelligence_execution_approval_v3"]
    calls.clear()
    for version, mode in ((RESULT_V1, "new_consume"), (RESULT_V2, "historical_read")):
        with refusal("execution_origin_mode_forbidden"):
            runtime._default_result_writer({}, version=version, mode=mode, query=query)
    assert calls == []


@pytest.mark.parametrize("mode", MODES)
def test_default_approval_reader_v2_reads_by_manifest_digest_and_admits_by_catalogue(mode):
    """Successor 2: 'sp_read_open_intelligence_execution_approval_v2 | manifest_sha256' and
    'The Python adapter receives the whole native row as untrusted data. It first requires
    its exact schema and two digest grammars, finds that ordered pair in the fixed trusted
    packaged catalogue, rehashes and validates both referenced files, then fully admits the
    same returned row'."""
    fx = fixture()
    calls = []

    def query(procedure, params, *, allow_zero=False):
        calls.append((procedure, parameters(params), allow_zero))
        return SimpleNamespace(**record_fields(fx.approval), extra="ignored")

    approval = runtime._default_approval_reader(
        fx.digest, version=APPROVAL_V2, mode=mode, query=query
    )
    assert type(approval) is runtime.ExecutionApprovalV2
    assert record_fields(approval) == record_fields(fx.approval)
    assert calls == [
        (
            "sp_read_open_intelligence_execution_approval_v2",
            [("manifest_sha256", "STRING", fx.digest)],
            False,
        )
    ]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            lambda row: row.__setitem__("resource_manifest_sha256", OTHER_SHA256),
            "execution_generation_unknown",
        ),
        (
            lambda row: row.__setitem__("origin_registry_sha256", OTHER_SHA256),
            "execution_generation_unknown",
        ),
        (
            lambda row: row.__setitem__("origin_registry_sha256", REGISTRY_SHA256.upper()),
            "execution_approval_schema_mismatch",
        ),
        (
            lambda row: row.__setitem__("resource_manifest_sha256", None),
            "execution_approval_schema_mismatch",
        ),
        (lambda row: row.pop("resource_manifest_sha256"), "execution_approval_schema_mismatch"),
        (
            lambda row: row.__setitem__("approval_contract_version", APPROVAL_V1),
            "execution_approval_schema_mismatch",
        ),
        (
            lambda row: row.__setitem__("approved_by", "usr_" + "0" * 64),
            "execution_approval_schema_mismatch",
        ),
    ],
)
def test_default_approval_reader_v2_refuses_untrusted_rows(change, code):
    fx = fixture()
    row = record_fields(fx.approval)
    change(row)
    with refusal(code):
        runtime._default_approval_reader(
            fx.digest, version=APPROVAL_V2, mode="historical_read", query=lambda *a, **k: row
        )


def test_default_approval_reader_v1_uses_the_retained_registry_in_historical_modes():
    """Successor 1: 'Historical v1 uses the approved retained registry and no fabricated
    resource binding.' Closure: 'v1 historical_read only if retained bytes are inspected'."""
    approval = v1_approval()
    calls = []

    def query(procedure, params, *, allow_zero=False):
        calls.append((procedure, parameters(params), allow_zero))
        return record_fields(approval)

    for mode in ("historical_read", "historical_replay"):
        read = runtime._default_approval_reader(
            approval.manifest_sha256, version=APPROVAL_V1, mode=mode, query=query
        )
        assert type(read) is runtime.ExecutionApproval
        assert read == approval
    assert (
        calls
        == [
            (
                "sp_read_open_intelligence_execution_approval_v1",
                [("manifest_sha256", "STRING", approval.manifest_sha256)],
                False,
            )
        ]
        * 2
    )
    foreign = record_fields(approval)
    foreign["canonical_manifest_json"] = foreign["canonical_manifest_json"].replace(
        RETAINED_V1_CONTRACT, "b" * 64
    )
    foreign["contract_sha256"] = "b" * 64
    foreign["manifest_sha256"] = sha256(foreign["canonical_manifest_json"].encode())
    foreign["approval_id"] = runtime.approval_id(
        foreign["manifest_sha256"], APPROVED_BY, approval.approved_at
    )
    phrase = (
        "I approve one staging execution of brain_read for manifest SHA256 "
        f"{foreign['manifest_sha256']}. Production remains unchanged."
    )
    foreign["approval_phrase_sha256"] = sha256(phrase.encode())
    assert runtime.ExecutionApproval(**foreign)
    with refusal("execution_origin_pair_invalid"):
        runtime._default_approval_reader(
            foreign["manifest_sha256"],
            version=APPROVAL_V1,
            mode="historical_read",
            query=lambda *a, **k: foreign,
        )
    fx = fixture()
    with refusal("execution_approval_schema_mismatch"):
        runtime._default_approval_reader(
            fx.digest,
            version=APPROVAL_V1,
            mode="historical_read",
            query=lambda *a, **k: record_fields(fx.approval),
        )


def test_default_result_reader_dispatches_by_exact_version():
    """Successor 2: 'sp_read_open_intelligence_execution_result_v2 | p_consumption_id' and
    'Existing result reader empty-result semantics remain unchanged; an empty result is
    absence, never success.'"""
    calls = []

    def query(procedure, params, *, allow_zero=False):
        calls.append((procedure, parameters(params), allow_zero))
        return ()

    consumption_id = "exc_" + "a" * 64
    assert (
        runtime._default_result_reader(
            consumption_id, version=RESULT_V1, mode="historical_replay", query=query
        )
        == ()
    )
    assert (
        runtime._default_result_reader(
            consumption_id, version=RESULT_V2, mode="new_consume", query=query
        )
        == ()
    )
    assert calls == [
        (
            "sp_read_open_intelligence_execution_result_v1",
            [("p_consumption_id", "STRING", consumption_id)],
            True,
        ),
        (
            "sp_read_open_intelligence_execution_result_v2",
            [("p_consumption_id", "STRING", consumption_id)],
            True,
        ),
    ]


def test_result_from_value_v2_admits_rows_by_their_own_trusted_pair():
    """Successor 2: 'A standalone historical reader uses only the immutable lookup key and
    explicit historical mode.'"""
    fx = fixture()
    authority = load(fx)
    consumption = consume(fx, authority)
    request = {
        "consumption_id": consumption.consumption_id,
        "approval_id": consumption.approval_id,
        "manifest_sha256": consumption.manifest_sha256,
        "operation": consumption.operation,
        "execution_name": consumption.execution_name,
        "result_reference": RESULT_REFERENCE,
        "canonical_result_json": RESULT_JSON,
        "result_digest": RESULT_DIGEST,
        "status": "succeeded",
        "origin_registry_sha256": REGISTRY_SHA256,
    }
    row = result_row(request)
    result = runtime._result_from_value_v2(
        SimpleNamespace(**row), "protected_context_invalid", mode="historical_read"
    )
    assert type(result) is runtime.ExecutionResultV2
    assert record_fields(result) == row
    with refusal("execution_generation_unknown"):
        runtime._result_from_value_v2(
            result_row(request, resource_sha=OTHER_SHA256),
            "protected_context_invalid",
            mode="historical_read",
        )
    for change in (
        lambda value: value.__setitem__("origin_registry_sha256", REGISTRY_SHA256.upper()),
        lambda value: value.pop("resource_manifest_sha256"),
        lambda value: value.__setitem__("result_contract_version", RESULT_V1),
        lambda value: value.__setitem__(
            "status", "succeeded" if row["status"] != "succeeded" else "failed"
        ),
    ):
        mutated = dict(row)
        change(mutated)
        with refusal("protected_context_invalid"):
            runtime._result_from_value_v2(
                mutated, "protected_context_invalid", mode="historical_read"
            )
    with pytest.raises(TypeError):
        runtime._result_from_value_v2(row, "protected_context_invalid")


AMENDMENT_D_RESOURCE_SHA256 = "ee809a4e81dec5242ea71ddd703990c5e0a9e237613e11135e069be0ea51ad96"


def amendment_d_fixture():
    """A record written under the amendment d pair, whose registry is amendment e's."""
    reg = (
        generations()
        .load_trusted_generation(AMENDMENT_E_REGISTRY_SHA256, AMENDMENT_D_RESOURCE_SHA256)
        .registry
    )
    return fixture(
        resource_sha=AMENDMENT_D_RESOURCE_SHA256,
        registry_sha=AMENDMENT_E_REGISTRY_SHA256,
        reg=reg,
        contract_sha=AMENDMENT_E_DAILY_CONTRACT,
    )


def test_records_written_under_the_amendment_d_pair_still_read():
    """Amendment d is live and the ledger ran under its pair: amendment e and the bridge
    generation keep that pair trusted, so a historical read of its approval and result
    rows admits them by their own recorded pair, with no monkeypatched catalogue."""
    fx = amendment_d_fixture()
    row = record_fields(fx.approval)
    for name in ("mode", "registry", "expected_resource_manifest_sha256"):
        row.pop(name, None)
    approval = runtime._approval_from_row_v2(
        SimpleNamespace(**row), "protected_context_invalid", mode="historical_read"
    )
    assert approval.resource_manifest_sha256 == AMENDMENT_D_RESOURCE_SHA256
    request = {
        "consumption_id": runtime.consumption_id_v2(
            fx.approval.approval_id,
            fx.execution["name"],
            CONSUMED_AT,
            origin_registry_sha256=AMENDMENT_E_REGISTRY_SHA256,
            resource_manifest_sha256=AMENDMENT_D_RESOURCE_SHA256,
        ),
        "approval_id": fx.approval.approval_id,
        "manifest_sha256": fx.digest,
        "operation": fx.operation,
        "execution_name": fx.execution["name"],
        "result_reference": RESULT_REFERENCE,
        "canonical_result_json": RESULT_JSON,
        "result_digest": RESULT_DIGEST,
        "status": "succeeded",
        "origin_registry_sha256": AMENDMENT_E_REGISTRY_SHA256,
    }
    result = runtime._result_from_value_v2(
        SimpleNamespace(**result_row(request, resource_sha=AMENDMENT_D_RESOURCE_SHA256)),
        "protected_context_invalid",
        mode="historical_read",
    )
    assert (result.origin_registry_sha256, result.resource_manifest_sha256) == (
        AMENDMENT_E_REGISTRY_SHA256,
        AMENDMENT_D_RESOURCE_SHA256,
    )


def test_fresh_consumption_refuses_the_amendment_d_pair_before_reading_approval():
    """Fresh approval and consumption use only the active pair: an execution annotated
    with the amendment d pair refuses as inactive before any approval is read."""
    fx = amendment_d_fixture()
    for annotations in (fx.execution["annotations"], fx.job["template"]["annotations"]):
        annotations[ANNOTATION_RESOURCE] = AMENDMENT_D_RESOURCE_SHA256
    reads = []
    with refusal("execution_generation_inactive"):
        load(fx, approval_reader=lambda digest: reads.append(digest))
    assert reads == []
    assert generations().ACTIVE_GENERATION_PAIR == PAIR


def _procedure_reply(procedure, row):
    import re

    from scripts.migrations import create_open_intelligence_execution_approval_store as store

    routine = next(item for item in store.build_v2_plan().routines if item.name == procedure)
    projection = routine.sql.rsplit("COMMIT TRANSACTION;", 1)[1]
    columns = re.findall(r"\bAS\s+([a-z_][a-z0-9_]*)", projection, flags=re.IGNORECASE)
    assert columns
    assert len(columns) == len(set(columns))
    return {name: row[name] for name in columns}


@pytest.mark.parametrize("operation", SELECTED_OPERATIONS)
def test_consume_accepts_real_sql_reply_and_binds_asserted_request_fields(operation):
    fx = fixture(operation)
    authority = load(fx)
    writes = []

    def query(procedure, params, *, allow_zero=False):
        request = {item.name: item.value for item in params}
        row = consumption_row(
            fx, {**request, "approval_id": fx.approval.approval_id, "operation": fx.operation}
        )
        reply = _procedure_reply(procedure, row)
        assert set(row) - set(reply) == {"job_resource", "source_sha", "image_uri"}
        writes.append(reply)
        return SimpleNamespace(**reply)

    consumed = runtime._consume_execution_authority(
        authority,
        consumption_writer=partial(
            runtime._default_consumption_writer,
            version=CONSUMPTION_V2,
            mode="new_consume",
            query=query,
        ),
    )
    assert consumed.job_resource == fx.payload["job_resource"]
    assert consumed.source_sha == fx.payload["source_sha"]
    assert consumed.image_uri == fx.payload["image_uri"]
    assert consumed.consumption_id == writes[0]["consumption_id"]
    assert len(writes) == 1
    assert runtime._require_consumed_execution(authority, consumed, operation) is consumed


@pytest.mark.parametrize("retry_first_write", [False, True])
def test_result_uses_durable_readback_after_real_sql_reply(retry_first_write):
    fx = fixture("wave1_pilot")
    authority = load(fx)
    consumed = consume(fx, authority)
    writes, reads, durable = [], [], []

    def writer(request):
        writes.append(dict(request))
        if retry_first_write and len(writes) == 1:
            raise RuntimeError("write did not commit")
        row = result_row(request)
        durable.append(row)
        reply = _procedure_reply("sp_record_open_intelligence_execution_result_v2", row)
        assert set(row) - set(reply) == {
            "operation",
            "execution_name",
            "result_reference",
            "canonical_result_json",
            "result_digest",
            "status",
        }
        return reply

    def reader(consumption_id):
        reads.append(consumption_id)
        return list(durable)

    result = runtime._record_execution_result(
        authority,
        consumed,
        RESULT_REFERENCE,
        RESULT_JSON,
        RESULT_DIGEST,
        "succeeded",
        result_writer=writer,
        result_reader=reader,
    )
    assert record_fields(result) == durable[0]
    assert len(writes) == 1 + retry_first_write
    assert reads == [consumed.consumption_id] * (1 + retry_first_write)


@pytest.mark.parametrize("readback", ["absent", "failed", "duplicate", "foreign"])
def test_successful_result_reply_never_hides_missing_or_conflicting_readback(readback):
    fx = fixture("wave1_pilot")
    authority = load(fx)
    consumed = consume(fx, authority)
    writes, durable = [], []

    def writer(request):
        writes.append(dict(request))
        row = result_row(request)
        durable.append(row)
        return _procedure_reply("sp_record_open_intelligence_execution_result_v2", row)

    def reader(_consumption_id):
        if readback == "absent":
            return []
        if readback == "failed":
            raise RuntimeError("read unavailable")
        if readback == "duplicate":
            return [durable[0], durable[0]]
        return [result_row(writes[0], status="failed")]

    code = (
        "execution_result_unresolved"
        if readback in {"absent", "failed"}
        else "execution_result_conflict"
    )
    with refusal(code):
        runtime._record_execution_result(
            authority,
            consumed,
            RESULT_REFERENCE,
            RESULT_JSON,
            RESULT_DIGEST,
            "succeeded",
            result_writer=writer,
            result_reader=reader,
        )
    assert len(writes) == 1


@pytest.mark.parametrize("field", ["job_resource", "source_sha", "image_uri"])
def test_consumption_refuses_a_conflicting_binding_when_reply_supplies_it(field):
    fx = fixture("wave1_pilot")
    authority = load(fx)

    def writer(request):
        row = consumption_row(fx, request)
        row[field] = "conflicting"
        return row

    with refusal("execution_approval_schema_mismatch"):
        runtime._consume_execution_authority(authority, consumption_writer=writer)
