import builtins
import copy
import hashlib
import inspect
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import execution_approval as runtime
from src.analysis.open_intelligence import execution_generations
from src.analysis.open_intelligence import production_snapshot_tables as tables
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

from tests.unit.test_execution_runtime_v2 import (
    ANNOTATION_REGISTRY,
    ANNOTATION_RESOURCE,
    COMPLETED_AT,
    CONSUMED_AT,
    REGISTRY_SHA256,
    RESOURCE_SHA256,
)
from tests.unit.test_execution_runtime_v2 import NOW as V2_NOW
from tests.unit.test_execution_runtime_v2 import fixture as v2_fixture
from tests.unit.test_open_intelligence_execution_approval_contract import (
    APPROVED_BY,
    HISTORICAL_READ,
    HISTORICAL_REPLAY,
    MANIFEST_V1,
    OPERATION_CASES,
    REGISTRY,
    SOURCE_SHA,
    retained_origin,
    source_snapshot_manifest,
    valid_cloud_build_response,
    valid_manifest,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
SOURCE_SNAPSHOT_NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
ORDINARY_OPERATIONS = tuple(
    operation for operation in OPERATION_CASES if operation != "bootstrap_migration_apply"
)
# Successor 1: "Bootstrap and protected capture remain excluded" from the seven fresh
# operations, so the retained v1 capture fixture below can only be read historically and
# refuses under new_consume, the only mode the loader accepts (P1 runtime packet, section 6).
V1_FRESH_REFUSAL = "execution_approval_execution_mismatch"
CONSUMPTION_V2 = "open_intelligence_execution_consumption_v2"
RESULT_V2 = "open_intelligence_execution_result_v2"


def source_snapshot_runtime_fixture(*, artifacts=None, cutoff_date="2026-09-07", mode="initial"):
    """Retained v1 capture record: an initial capture is read under historical_read and a
    recovery under historical_replay, against the packaged registry (successor 1)."""
    origin_mode = HISTORICAL_REPLAY if mode == "recover" else HISTORICAL_READ
    payload = source_snapshot_manifest(cutoff=cutoff_date, mode=mode)
    expires = SOURCE_SNAPSHOT_NOW + timedelta(hours=1)
    payload["expires_at"] = expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    build = valid_cloud_build_response()
    contents = {name["name"]: b"{}" for name in payload["input_artifacts"]}
    contents["recovery_context"] = b"null"
    contents["capture_contract"] = (
        Path(__file__).resolve().parents[1]
        / "fixtures/open_intelligence/protected_source_snapshot_contract.md"
    ).read_bytes()
    contents.update(artifacts or {})
    contents["build_provenance"] = runtime.canonical_build_provenance_bytes(
        runtime.build_provenance_from_response(
            build,
            payload["contract_sha256"],
            manifest_version=MANIFEST_V1,
            mode=origin_mode,
            registry=REGISTRY,
        ),
        origin=retained_origin(payload["contract_sha256"]),
    )
    for item in payload["input_artifacts"]:
        item["sha256"] = hashlib.sha256(contents[item["name"]]).hexdigest()
    manifest_json = runtime.canonical_manifest_bytes(
        payload, mode=origin_mode, registry=REGISTRY
    ).decode()
    digest = hashlib.sha256(manifest_json.encode()).hexdigest()
    phrase = f"I approve one staging execution of source_snapshot_capture for manifest SHA256 {digest}. Production remains unchanged."
    approval = runtime.ExecutionApproval(
        approval_contract_version="open_intelligence_execution_approval_v1",
        approval_id=runtime.approval_id(digest, APPROVED_BY, SOURCE_SNAPSHOT_NOW),
        manifest_version="open_intelligence_execution_manifest_v1",
        operation="source_snapshot_capture",
        contract_sha256=payload["contract_sha256"],
        manifest_sha256=digest,
        canonical_manifest_json=manifest_json,
        approved_by=APPROVED_BY,
        approved_at=SOURCE_SNAPSHOT_NOW,
        expires_at=expires,
        approval_phrase_sha256=hashlib.sha256(phrase.encode()).hexdigest(),
    )
    task = {
        "serviceAccount": payload["service_identity"],
        "maxRetries": 0,
        "timeout": "600s",
        "containers": [
            {
                "image": payload["image_uri"],
                "command": payload["command"],
                "args": payload["arguments"],
                "env": payload["environment"],
                "resources": {"limits": {"cpu": "2", "memory": "8Gi"}},
            }
        ],
    }
    annotations = {
        "42.ogilvy/execution-approval-sha256": digest,
        "42.ogilvy/source-sha": SOURCE_SHA,
    }
    execution = {
        "name": payload["job_resource"] + "/executions/source-test-1",
        "job": payload["job_resource"],
        "annotations": annotations,
        "template": copy.deepcopy(task),
        "taskCount": 1,
    }
    job = {
        "name": payload["job_resource"],
        "template": {
            "annotations": annotations,
            "taskCount": 1,
            "template": copy.deepcopy(task),
        },
    }
    return payload, approval, execution, job, build, contents


def consumed_source_snapshot_authority(*, artifacts=None, cutoff_date="2026-09-07", mode="initial"):
    """Fresh capture authority is a refusal under successor 1: protected capture is excluded
    from the seven fresh operations and historical replay cannot grant fresh execution
    authority, so the loader refuses the retained v1 job under new_consume before reading
    the approval. Callers that need a live consumed capture authority stay blocked until a
    v2 capture profile exists (caller closure row for source_snapshot_capture)."""
    _payload, approval, execution, job, build, contents = source_snapshot_runtime_fixture(
        artifacts=artifacts, cutoff_date=cutoff_date, mode=mode
    )
    authority = runtime._load_execution_authority(
        "source_snapshot_capture",
        mode="new_consume",
        execution_reader=lambda: {"execution": execution, "job": job},
        approval_reader=lambda digest: approval,
        build_reader=lambda name: build,
        artifact_reader=lambda name: contents[name],
        now=lambda: SOURCE_SNAPSHOT_NOW,
    )
    consumption = runtime._consume_execution_authority(
        authority,
        consumption_writer=lambda request: _consumption_row(request),
    )
    return authority, consumption


def retained_source_snapshot_records(*, artifacts=None, cutoff_date="2026-09-07", mode="initial"):
    """Retained v1 capture approval and consumption records, the row material the context
    readers consume under historical_read and snapshot recovery under historical_replay.
    Successor 1 preserves 'every historical approval, consumption and result byte' and
    excludes protected capture from fresh execution, so no authority is issued here."""
    payload, approval, execution, _job, _build, _contents = source_snapshot_runtime_fixture(
        artifacts=artifacts, cutoff_date=cutoff_date, mode=mode
    )
    consumed_at = SOURCE_SNAPSHOT_NOW + timedelta(seconds=1)
    consumption = runtime.ExecutionConsumption(
        consumption_contract_version="open_intelligence_execution_consumption_v1",
        consumption_id=runtime.consumption_id(approval.approval_id, execution["name"], consumed_at),
        approval_id=approval.approval_id,
        manifest_sha256=approval.manifest_sha256,
        operation="source_snapshot_capture",
        execution_name=execution["name"],
        job_resource=payload["job_resource"],
        source_sha=payload["source_sha"],
        image_uri=payload["image_uri"],
        consumed_at=consumed_at,
    )
    return approval, consumption


def retained_capture_view(*, artifacts=None, cutoff_date="2026-09-07", mode="initial"):
    """Retained v1 view of a historical capture for the validated creation body. The
    approval row material is typed back into the v1 record and re-read under
    historical_replay through the retained reader against the packaged registry, the
    consumption is the retained row material
    checked as the creation body checks it, and result_record builds the v1 ExecutionResult
    directly from a payload the body returned, which is how a historical capture left its
    result row (successor 1). The view can never reach a guarded public entry, so it grants
    no execution authority; that is the difference from a loader issued object."""
    approval, consumption = retained_source_snapshot_records(
        artifacts=artifacts, cutoff_date=cutoff_date, mode=mode
    )
    registry = tables.retained_origin_registry()
    approval, manifest = tables.retained_v1_approval(
        runtime.ExecutionApproval(**asdict(approval)),
        "snapshot_creation_context_invalid",
        mode=HISTORICAL_REPLAY,
        registry=registry,
    )
    tables._require_retained_consumption(
        consumption, "source_snapshot_capture", mode=HISTORICAL_REPLAY, registry=registry
    )

    def result_record(payload, *, status, completed_at, reference="synthetic:retained-result"):
        digest = canonical_digest(payload)
        return runtime.ExecutionResult(
            result_contract_version="open_intelligence_execution_result_v1",
            result_id=runtime.result_id(
                consumption.consumption_id, reference, digest, status, completed_at
            ),
            consumption_id=consumption.consumption_id,
            approval_id=consumption.approval_id,
            manifest_sha256=consumption.manifest_sha256,
            operation="source_snapshot_capture",
            execution_name=consumption.execution_name,
            result_reference=reference,
            canonical_result_json=canonical_bytes(payload).decode(),
            result_digest=digest,
            status=status,
            completed_at=completed_at,
        )

    return SimpleNamespace(
        approval=approval, manifest=manifest, consumption=consumption, result_record=result_record
    )


def test_active_registry_binds_exactly_one_v2_capture_origin():
    """D03 flipped the retained view sentinel: the active registry now binds
    source_snapshot_capture on exactly one v2 row, the successor capture policy, in all
    four modes, while the v1 row that bound it under the historical modes is unchanged.
    Fresh capture authority resolves through the live loader; the retained readers keep
    selecting the v1 row under historical_read and historical_replay only."""
    active = execution_generations.active_generation().registry
    origin = runtime._v2_origin_for_operation(
        "source_snapshot_capture", mode="new_consume", registry=active
    )
    assert origin.manifest_version == "open_intelligence_execution_manifest_v2"
    # The active registry is the bridge generation's, whose daily row links the v3 bridge
    # capture policy at its candidate_contracts path.
    assert origin.contract_file == (
        "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"
    )
    assert origin.allowed_execution_modes == frozenset(tables.HISTORICAL_MODES) | {
        "new_approval",
        "new_consume",
    }
    binding = origin.operation_bindings["source_snapshot_capture"]
    assert binding.job_resource == (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
    )
    assert (
        binding.service_identity
        == "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert binding.datasets == ("trends_v2_staging",)
    # The retained amendment e registry's v2 capture row keeps its own successor policy.
    for registry, v2_contract in (
        (active, origin.contract_sha256),
        (
            tables.retained_origin_registry(),
            "66e1a11f30c6dd8a7ca9db2110ef0af91d9a3aeda6b48d0b6323797839a06546",
        ),
    ):
        rows = sorted(
            (
                origin
                for origin in registry.values()
                if "source_snapshot_capture" in origin.operation_bindings
            ),
            key=lambda item: item.manifest_version,
        )
        assert [row.manifest_version for row in rows] == [MANIFEST_V1, origin.manifest_version]
        assert rows[0].contract_sha256 == (
            "5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f"
        )
        assert rows[0].allowed_execution_modes == frozenset(tables.HISTORICAL_MODES)
        assert rows[0].contract_file is None
        assert rows[1].contract_sha256 == v2_contract
        for mode in tables.HISTORICAL_MODES:
            retained = tables._retained_v1_origin(
                "source_snapshot_capture", mode=mode, registry=registry
            )
            assert retained.contract_sha256 == rows[0].contract_sha256


def test_source_snapshot_real_loader_and_consumption_preserve_runtime_gate():
    """Successor 1: 'Bootstrap and protected capture remain excluded' and 'Historical replay
    cannot issue a new consumption ... or grant fresh execution authority.' The retained v1
    capture job refuses under new_consume, the only loader mode (P1 runtime packet, section
    6), with the loader's own code after the execution read and before any approval, build
    or artifact read, so no consumption can follow."""
    _payload, approval, execution, job, build, contents = source_snapshot_runtime_fixture()
    reads = []
    with pytest.raises(runtime.ApprovalRefusal, match=f"^{V1_FRESH_REFUSAL}$"):
        runtime._load_execution_authority(
            "source_snapshot_capture",
            mode="new_consume",
            execution_reader=lambda: (
                reads.append("execution") or {"execution": execution, "job": job}
            ),
            approval_reader=lambda digest: reads.append("approval") or approval,
            build_reader=lambda name: reads.append("build") or build,
            artifact_reader=lambda name: reads.append(name) or contents[name],
            now=lambda: SOURCE_SNAPSHOT_NOW,
        )
    assert reads == ["execution"]
    assert approval.operation == "source_snapshot_capture"
    with pytest.raises(runtime.ApprovalRefusal, match=f"^{V1_FRESH_REFUSAL}$"):
        consumed_source_snapshot_authority()
    # The same capture job stamped with the active generation pair is refused as an
    # excluded operation, still before the approval read: no job shape yields capture authority.
    for annotations in (execution["annotations"], job["template"]["annotations"]):
        annotations[ANNOTATION_REGISTRY] = REGISTRY_SHA256
        annotations[ANNOTATION_RESOURCE] = RESOURCE_SHA256
    with pytest.raises(runtime.ApprovalRefusal, match=r"^execution_approval_manifest_invalid$"):
        runtime._load_execution_authority(
            "source_snapshot_capture",
            mode="new_consume",
            execution_reader=lambda: {"execution": execution, "job": job},
            approval_reader=lambda digest: reads.append("approval") or approval,
            build_reader=lambda name: reads.append("build") or build,
            artifact_reader=lambda name: reads.append(name) or contents[name],
            now=lambda: SOURCE_SNAPSHOT_NOW,
        )
    assert reads == ["execution"]


def test_source_snapshot_default_artifact_reader_uses_only_protected_cli(monkeypatch):
    modules = []

    def resolve(name):
        modules.append(name)
        return SimpleNamespace(_execution_approval_artifact_bytes=lambda name: name.encode())

    monkeypatch.setattr(runtime.importlib, "import_module", resolve)
    assert (
        runtime._default_artifact_reader("source_snapshot_capture", "capture_plan")
        == b"capture_plan"
    )
    assert modules == ["scripts.staging.capture_protected_production_snapshot"]


@pytest.mark.parametrize("mutation", ["cpu", "memory", "execution_tasks", "job_tasks"])
def test_source_snapshot_runtime_resource_ceilings_refuse(mutation):
    """Successor 1: the retained v1 capture job is refused under new_consume before its
    resource ceilings are compared, with the same code the ceilings raised, and the
    approval reader is never reached (P1 runtime packet, section 6)."""
    _payload, approval, execution, job, build, contents = source_snapshot_runtime_fixture()
    if mutation in {"cpu", "memory"}:
        value = "4" if mutation == "cpu" else "16Gi"
        execution["template"]["containers"][0]["resources"]["limits"][mutation] = value
        job["template"]["template"]["containers"][0]["resources"]["limits"][mutation] = value
    if mutation == "execution_tasks":
        execution["taskCount"] = 2
    if mutation == "job_tasks":
        job["template"]["taskCount"] = True
    reads = []
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_execution_mismatch"):
        runtime._load_execution_authority(
            "source_snapshot_capture",
            mode="new_consume",
            execution_reader=lambda: {"execution": execution, "job": job},
            approval_reader=lambda digest: reads.append("approval") or approval,
            build_reader=lambda name: build,
            artifact_reader=lambda name: contents[name],
            now=lambda: SOURCE_SNAPSHOT_NOW,
        )
    assert reads == []


def _fixture(operation="brain_read"):
    """Retained v1 execution record, read under historical_read against the packaged
    registry (successor 1); it is the old job that refuses under v2 in the tests below."""
    manifest_payload = valid_manifest(operation)
    manifest_payload["expires_at"] = "2026-08-31T13:00:00.000000Z"
    build = valid_cloud_build_response()
    build_receipt = runtime.build_provenance_from_response(
        build,
        manifest_payload["contract_sha256"],
        manifest_version=MANIFEST_V1,
        mode=HISTORICAL_READ,
        registry=REGISTRY,
    )
    artifacts = {}
    for item in manifest_payload["input_artifacts"]:
        if item["name"] == "build_provenance":
            content = runtime.canonical_build_provenance_bytes(
                build_receipt,
                origin=retained_origin(manifest_payload["contract_sha256"]),
            )
        else:
            content = f"artifact:{item['name']}".encode()
            artifacts[item["name"]] = content
        item["sha256"] = hashlib.sha256(content).hexdigest()
    manifest_json = runtime.canonical_manifest_bytes(
        manifest_payload, mode=HISTORICAL_READ, registry=REGISTRY
    ).decode()
    manifest_sha = runtime.manifest_sha256(
        manifest_payload, mode=HISTORICAL_READ, registry=REGISTRY
    )
    phrase = (
        f"I approve one staging execution of {operation} for manifest SHA256 {manifest_sha}. "
        "Production remains unchanged."
    )
    approval = runtime.ExecutionApproval(
        approval_contract_version="open_intelligence_execution_approval_v1",
        approval_id=runtime.approval_id(manifest_sha, APPROVED_BY, NOW),
        manifest_version="open_intelligence_execution_manifest_v1",
        operation=operation,
        contract_sha256=manifest_payload["contract_sha256"],
        manifest_sha256=manifest_sha,
        canonical_manifest_json=manifest_json,
        approved_by=APPROVED_BY,
        approved_at=NOW,
        expires_at=datetime(2026, 8, 31, 13, tzinfo=UTC),
        approval_phrase_sha256=hashlib.sha256(phrase.encode()).hexdigest(),
    )
    task = {
        "serviceAccount": manifest_payload["service_identity"],
        "containers": [
            {
                "image": manifest_payload["image_uri"],
                "command": manifest_payload["command"],
                "args": manifest_payload["arguments"],
                "env": manifest_payload["environment"],
            }
        ],
        "maxRetries": 0,
        "timeout": "900s",
    }
    task["containers"][0]["env"].extend(
        {
            "name": name,
            "valueSource": {
                "secretKeyRef": {
                    "secret": name,
                    "version": "latest",
                }
            },
        }
        for name in manifest_payload["secrets"]
    )
    annotations = {
        "42.ogilvy/execution-approval-sha256": manifest_sha,
        "42.ogilvy/source-sha": SOURCE_SHA,
    }
    if operation == "brain_read":
        annotations.update(
            {
                "42.ogilvy/contract-sha256": "f" * 64,
                "42.ogilvy/dependency-lock-sha256": "1" * 64,
                "42.ogilvy/runtime-sbom-sha256": "2" * 64,
                "42.ogilvy/deployment-manifest-sha256": "3" * 64,
            }
        )
    execution_name = manifest_payload["job_resource"] + "/executions/execution-1"
    execution = {
        "name": execution_name,
        "job": manifest_payload["job_resource"],
        "annotations": annotations,
        "template": copy.deepcopy(task),
    }
    job = {
        "name": manifest_payload["job_resource"],
        "template": {"annotations": annotations, "template": copy.deepcopy(task)},
    }
    return manifest_payload, approval, execution, job, build, artifacts


def _load_v1(operation, approval, execution, job, build, artifacts, reads=None, now=None):
    reads = [] if reads is None else reads
    return runtime._load_execution_authority(
        operation,
        mode="new_consume",
        execution_reader=lambda: reads.append("execution") or {"execution": execution, "job": job},
        approval_reader=lambda _sha: reads.append("approval") or approval,
        build_reader=lambda _resource: reads.append("build") or build,
        artifact_reader=lambda name: reads.append(f"artifact:{name}") or artifacts[name],
        now=now or (lambda: NOW),
    )


def _load_v2(fx, reads=None, now=None):
    reads = [] if reads is None else reads
    return runtime._load_execution_authority(
        fx.operation,
        mode="new_consume",
        execution_reader=lambda: (
            reads.append("execution") or {"execution": fx.execution, "job": fx.job}
        ),
        approval_reader=lambda _sha: reads.append("approval") or fx.approval,
        build_reader=lambda _resource: reads.append("build") or fx.build,
        artifact_reader=lambda name: reads.append(f"artifact:{name}") or fx.contents[name],
        now=now or (lambda: V2_NOW),
    )


@pytest.mark.parametrize("operation", ["r3_apply", "brain_read", "wave1_pilot"])
def test_execution_job_accepts_the_short_job_id_cloud_run_v2_returns(operation):
    """Live Cloud Run v2 readback (2 September 2026) returns execution.job as the short job
    id, not the full resource. Both forms must bind to the same manifest job. Successor 1
    tests 'old jobs under v2': the retained v1 job refuses under new_consume in either
    form before its approval is read, and the binding is preserved on the selected v2
    fixture (P1 runtime packet, section 6)."""
    manifest, approval, execution, job, build, artifacts = _fixture(operation)
    execution["job"] = manifest["job_resource"].rsplit("/", 1)[1]
    reads = []
    with pytest.raises(runtime.ApprovalRefusal, match=f"^{V1_FRESH_REFUSAL}$"):
        _load_v1(operation, approval, execution, job, build, artifacts, reads)
    assert reads == ["execution"]

    fx = v2_fixture(operation)
    fx.execution["job"] = fx.payload["job_resource"].rsplit("/", 1)[1]
    authority = _load_v2(fx)
    assert authority.manifest.job_resource == fx.payload["job_resource"]

    for wrong in ("other-job", manifest["job_resource"] + "-other", "", None):
        execution["job"] = wrong
        with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_execution_mismatch"):
            _load_v1(operation, approval, execution, job, build, artifacts)
        fx = v2_fixture(operation)
        fx.execution["job"] = wrong
        with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_execution_mismatch"):
            _load_v2(fx)


def _load(events, operation="brain_read"):
    """Live authority under the successor: the loader accepts only mode new_consume and
    issues authority for the seven selected v2 operations (P1 runtime packet, section 6);
    the retained v1 fixture of the same operation is the refusal exercised above."""
    fx = v2_fixture(operation)
    authority = runtime._load_execution_authority(
        operation,
        mode="new_consume",
        execution_reader=lambda: (
            events.append("execution") or {"execution": fx.execution, "job": fx.job}
        ),
        approval_reader=lambda sha: events.append("approval") or fx.approval,
        build_reader=lambda resource: events.append("build") or fx.build,
        artifact_reader=lambda name: events.append(f"artifact:{name}") or fx.contents[name],
        now=lambda: V2_NOW,
    )
    return fx.payload, fx.approval, fx.execution, authority


def _consumption_row(request, consumed_at=None):
    consumed_at = consumed_at or CONSUMED_AT
    return {
        **request,
        "consumption_contract_version": CONSUMPTION_V2,
        "consumption_id": runtime.consumption_id_v2(
            request["approval_id"],
            request["execution_name"],
            consumed_at,
            origin_registry_sha256=request["origin_registry_sha256"],
            resource_manifest_sha256=request["resource_manifest_sha256"],
        ),
        "consumed_at": consumed_at,
    }


def test_runtime_reloads_consumes_before_protected_work_and_records_result():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    events = []
    durable = []
    manifest, approval, execution, authority = _load(events)
    consumption = runtime._consume_execution_authority(
        authority,
        consumption_writer=lambda request: (
            events.append("consumption") or _consumption_row(request)
        ),
    )
    events.append("protected")
    result_json = '{"receipt":"fixture"}'
    result_digest = hashlib.sha256(result_json.encode()).hexdigest()
    result = runtime._record_execution_result(
        authority,
        consumption,
        "fixture-receipt",
        result_json,
        result_digest,
        "succeeded",
        result_writer=lambda request: (
            events.append("result") or durable.append(_result_row(request)) or durable[0]
        ),
        result_reader=lambda _consumption_id: events.append("result_readback") or list(durable),
    )
    assert result.approval_id == approval.approval_id
    assert result.consumption_id == runtime.consumption_id_v2(
        approval.approval_id,
        execution["name"],
        CONSUMED_AT,
        origin_registry_sha256=consumption.origin_registry_sha256,
        resource_manifest_sha256=consumption.resource_manifest_sha256,
    )
    assert events[:3] == ["execution", "approval", "build"]
    assert events[-4:] == ["consumption", "protected", "result", "result_readback"]
    assert manifest["operation"] == "brain_read"


MUTATIONS = [
    "annotation",
    "parent",
    "image",
    "source",
    "identity",
    "command",
    "arguments",
    "environment",
    "retries",
    "timeout",
    "build_source",
    "build_resource",
]


def _mutate(mutation, execution, job, build):
    if mutation == "annotation":
        execution["annotations"]["42.ogilvy/execution-approval-sha256"] = "0" * 64
    elif mutation == "parent":
        execution["job"] += "-other"
    elif mutation == "image":
        job["template"]["template"]["containers"][0]["image"] += "-other"
    elif mutation == "source":
        execution["annotations"]["42.ogilvy/source-sha"] = "0" * 40
    elif mutation == "identity":
        job["template"]["template"]["serviceAccount"] = "other@example.invalid"
    elif mutation == "command":
        job["template"]["template"]["containers"][0]["command"] = ["other"]
    elif mutation == "arguments":
        job["template"]["template"]["containers"][0]["args"] = ["other"]
    elif mutation == "environment":
        job["template"]["template"]["containers"][0]["env"][0]["value"] = "other"
    elif mutation == "retries":
        job["template"]["template"]["maxRetries"] = 1
    elif mutation == "build_source":
        if "sourceProvenance" in build:
            build["sourceProvenance"]["resolvedRepoSource"]["commitSha"] = "0" * 40
        else:
            build["source"]["connectedRepository"]["revision"] = "0" * 40
    elif mutation == "build_resource":
        build["name"] = build["name"] + "-other"
    else:
        job["template"]["template"]["timeout"] = "901s"


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_execution_job_and_build_authority_mutations_refuse(mutation):
    """Successor 1: the retained v1 job refuses under new_consume whatever else drifted
    (old jobs under v2), and every execution, job and build mutation still refuses on the
    selected v2 fixture (P1 runtime packet, section 6)."""
    _manifest, approval, execution, job, build, artifacts = _fixture()
    _mutate(mutation, execution, job, build)
    with pytest.raises(runtime.ApprovalRefusal):
        _load_v1("brain_read", approval, execution, job, build, artifacts)
    fx = v2_fixture("brain_read")
    _mutate(mutation, fx.execution, fx.job, fx.build)
    with pytest.raises(runtime.ApprovalRefusal):
        _load_v2(fx)


def test_consumption_failure_is_never_retried():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    _manifest, _approval, _execution, authority = _load([])
    calls = []

    def fail(_request):
        calls.append("attempt")
        raise RuntimeError("ambiguous")

    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_concurrent_conflict"):
        runtime._consume_execution_authority(authority, consumption_writer=fail)
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_consumed"):
        runtime._consume_execution_authority(authority, consumption_writer=fail)
    assert calls == ["attempt"]


def test_copied_or_forged_authority_refuses_before_writer():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    _manifest, _approval, _execution, authority = _load([])
    forged = copy.copy(authority)
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_identity_invalid"):
        runtime._consume_execution_authority(
            forged,
            consumption_writer=lambda _request: pytest.fail("forged authority reached writer"),
        )

    object_new_forgery = object.__new__(type(authority))
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_identity_invalid"):
        runtime._consume_execution_authority(
            object_new_forgery,
            consumption_writer=lambda _request: pytest.fail("object.__new__ reached writer"),
        )


def test_stale_integer_registry_slot_cannot_authorize_distinct_object(monkeypatch):
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    _manifest, _approval, _execution, live_authority = _load([])
    _manifest, _approval, _execution, stale_authority = _load([])

    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_concurrent_conflict"):
        runtime._consume_execution_authority(
            stale_authority,
            consumption_writer=lambda _request: (_ for _ in ()).throw(RuntimeError("ambiguous")),
        )

    forged = copy.copy(live_authority)
    real_id = builtins.id
    stale_id = real_id(stale_authority)
    monkeypatch.setattr(
        runtime,
        "id",
        lambda value: stale_id if value is forged else real_id(value),
        raising=False,
    )

    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_identity_invalid"):
        runtime._consume_execution_authority(
            forged,
            consumption_writer=lambda _request: pytest.fail("stale slot forgery reached writer"),
        )


def test_authority_registry_and_raw_issuer_are_not_module_globals():
    for name in (
        "_AUTHORITY_SECRET",
        "_AUTHORITY_REGISTRY",
        "_CONSUMPTION_REGISTRY",
        "_IssuedExecutionAuthority",
        "_issue_runtime_authority",
        "_transition_authority",
        "_validate_issued_authority",
    ):
        assert not hasattr(runtime, name)


def test_coherent_digest_mutation_refuses_before_writer():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    _manifest, _approval, _execution, authority = _load([])
    authority._digest = "0" * 64
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_identity_invalid"):
        runtime._consume_execution_authority(
            authority,
            consumption_writer=lambda _request: pytest.fail("mutated authority reached writer"),
        )


def test_artifact_digest_drift_refuses():
    """Successor 1: the retained v1 job refuses before any artifact is read (old jobs under
    v2); artifact regeneration is preserved on the selected v2 fixture (P1 runtime packet,
    section 6)."""
    _manifest, approval, execution, job, build, artifacts = _fixture()
    first = next(iter(artifacts))
    artifacts[first] += b"drift"
    reads = []
    with pytest.raises(runtime.ApprovalRefusal, match=f"^{V1_FRESH_REFUSAL}$"):
        _load_v1("brain_read", approval, execution, job, build, artifacts, reads)
    assert reads == ["execution"]
    fx = v2_fixture("brain_read")
    first = next(name for name in fx.contents if name != "build_provenance")
    fx.contents[first] += b"drift"
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_artifact_mismatch"):
        _load_v2(fx)


@pytest.mark.parametrize("field", ["approval_id", "approved_by", "expires_at"])
def test_approval_identity_approver_and_expiry_mutations_refuse(field):
    """Successor 1: the retained v1 job refuses before its approval is read (old jobs under
    v2), so the approval mutation is never consulted; the same mutation on the selected v2
    approval refuses as a schema mismatch (P1 runtime packet, section 6)."""
    _manifest, approval, execution, job, build, artifacts = _fixture()
    replacement = {
        "approval_id": "exa_" + "0" * 64,
        "approved_by": "usr_" + "0" * 64,
        "expires_at": datetime(2026, 8, 31, 11, tzinfo=UTC),
    }[field]
    object.__setattr__(approval, field, replacement)
    reads = []
    with pytest.raises(runtime.ApprovalRefusal, match=f"^{V1_FRESH_REFUSAL}$"):
        _load_v1("brain_read", approval, execution, job, build, artifacts, reads)
    assert reads == ["execution"]
    fx = v2_fixture("brain_read")
    object.__setattr__(fx.approval, field, replacement)
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_schema_mismatch"):
        _load_v2(fx)


def test_secret_binding_mutation_refuses():
    """Successor 1: the retained v1 job, which binds secrets at 'latest', refuses under
    new_consume before its task is compared; the selected v2 fixture binds the exact
    numeric version and refuses the same secret drift (P1 runtime packet, section 6)."""
    _manifest, approval, execution, job, build, artifacts = _fixture("wave1_pilot")
    job["template"]["template"]["containers"][0]["env"][-1]["valueSource"]["secretKeyRef"][
        "secret"
    ] = "OTHER_SECRET"
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_execution_mismatch"):
        _load_v1("wave1_pilot", approval, execution, job, build, artifacts)
    fx = v2_fixture("wave1_pilot")
    fx.job["template"]["template"]["containers"][0]["env"][-1]["valueSource"]["secretKeyRef"][
        "secret"
    ] = "OTHER_SECRET"
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_execution_mismatch"):
        _load_v2(fx)


def test_runtime_helpers_are_private():
    assert "_load_execution_authority" not in runtime.__all__
    assert "_consume_execution_authority" not in runtime.__all__
    assert "_record_execution_result" not in runtime.__all__
    assert not hasattr(runtime, "IssuedExecutionAuthority")


@pytest.mark.parametrize("operation", ORDINARY_OPERATIONS)
def test_every_ordinary_operation_round_trips_one_consumption_and_failed_result(
    operation,
):
    """Successor 1: 'Seven fresh operations are selected by the current approved registry.
    Bootstrap and protected capture remain excluded.' The seven round trip one consumption
    and one failed result on the v2 path; protected capture refuses as a fresh execution
    (P1 runtime packet, section 6)."""
    if operation == "source_snapshot_capture":
        with pytest.raises(runtime.ApprovalRefusal, match=f"^{V1_FRESH_REFUSAL}$"):
            consumed_source_snapshot_authority()
        return
    _manifest, approval, execution, authority = _load([], operation)
    consumption = runtime._consume_execution_authority(
        authority,
        consumption_writer=lambda request: _consumption_row(request),
    )
    assert consumption.approval_id == approval.approval_id
    assert consumption.execution_name == execution["name"]
    durable = []
    result_json = '{"error":"operation_failed"}'
    result_digest = hashlib.sha256(result_json.encode()).hexdigest()
    result = runtime._record_execution_result(
        authority,
        consumption,
        "fixture-failure-receipt",
        result_json,
        result_digest,
        "failed",
        result_writer=lambda request: durable.append(_result_row(request)) or durable[0],
        result_reader=lambda _consumption_id: list(durable),
    )
    assert result.operation == operation
    assert result.status == "failed"


def test_bootstrap_operation_has_no_ordinary_runtime_path():
    """Successor 1: 'Bootstrap and protected capture remain excluded.' The loader reads the
    execution pair to select the generation and then refuses bootstrap before the approval,
    build or artifact readers are reached, and an operation that is not an exact string
    refuses before any reader (P1 runtime packet, section 6)."""
    fx = v2_fixture()
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_manifest_invalid"):
        runtime._load_execution_authority(
            "bootstrap_migration_apply",
            mode="new_consume",
            execution_reader=lambda: {"execution": fx.execution, "job": fx.job},
            approval_reader=lambda _sha: pytest.fail("bootstrap reached approval reader"),
            build_reader=lambda _resource: pytest.fail("bootstrap reached build reader"),
            artifact_reader=lambda _name: pytest.fail("bootstrap reached artifact reader"),
            now=lambda: NOW,
        )
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_manifest_invalid"):
        runtime._load_execution_authority(
            "",
            mode="new_consume",
            execution_reader=lambda: pytest.fail("empty operation reached ordinary runtime"),
            approval_reader=lambda _sha: pytest.fail("empty operation reached approval reader"),
            build_reader=lambda _resource: pytest.fail("empty operation reached build reader"),
            artifact_reader=lambda _name: pytest.fail("empty operation reached artifact reader"),
            now=lambda: NOW,
        )


def test_runtime_bigquery_transactions_explicitly_disable_all_sdk_retries():
    source = inspect.getsource(runtime._runtime_query)
    assert "retry=None" in source
    assert "job_retry=None" in source
    assert ".result(retry=None, job_retry=None)" in source


def _consumed_authority(operation="brain_read"):
    _manifest, _approval, _execution, authority = _load([], operation)
    consumption = runtime._consume_execution_authority(
        authority,
        consumption_writer=lambda request: _consumption_row(request),
    )
    return authority, consumption


def _result_row(request, completed_at=None):
    completed_at = completed_at or COMPLETED_AT
    return {
        **request,
        "result_contract_version": RESULT_V2,
        "result_id": runtime.result_id_v2(
            request["consumption_id"],
            request["result_reference"],
            request["result_digest"],
            request["status"],
            completed_at,
            origin_registry_sha256=request["origin_registry_sha256"],
            resource_manifest_sha256=request["resource_manifest_sha256"],
        ),
        "completed_at": completed_at,
    }


def _record_fixture(authority, consumption, **kwargs):
    result_json = '{"receipt":"fixture"}'
    return runtime._record_execution_result(
        authority,
        consumption,
        "fixture-receipt",
        result_json,
        hashlib.sha256(result_json.encode()).hexdigest(),
        "succeeded",
        **kwargs,
    )


def test_result_writer_error_before_commit_reads_zero_then_retries_exact_request_once():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    authority, consumption = _consumed_authority()
    writes = []
    reads = []
    durable = []

    def writer(request):
        writes.append(dict(request))
        if len(writes) == 1:
            raise RuntimeError("not committed")
        durable.append(_result_row(request))
        return durable[0]

    result = _record_fixture(
        authority,
        consumption,
        result_writer=writer,
        result_reader=lambda consumption_id: reads.append(consumption_id) or list(durable),
    )

    assert result.status == "succeeded"
    assert writes == [writes[0], writes[0]]
    assert reads == [consumption.consumption_id, consumption.consumption_id]


def test_result_writer_error_after_commit_returns_the_exact_durable_row_without_retry():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    authority, consumption = _consumed_authority()
    writes = []
    committed = []

    def writer(request):
        writes.append(dict(request))
        committed.append(_result_row(request))
        raise RuntimeError("response lost")

    result = _record_fixture(
        authority,
        consumption,
        result_writer=writer,
        result_reader=lambda _consumption_id: committed,
    )

    assert result.result_id == committed[0]["result_id"]
    assert len(writes) == 1


def test_result_retry_commit_with_lost_response_is_reconciled_by_second_read():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    authority, consumption = _consumed_authority()
    writes = []
    committed = []

    def writer(request):
        writes.append(dict(request))
        if len(writes) == 2:
            committed.append(_result_row(request))
        raise RuntimeError("response lost")

    result = _record_fixture(
        authority,
        consumption,
        result_writer=writer,
        result_reader=lambda _consumption_id: list(committed),
    )

    assert result.result_id == committed[0]["result_id"]
    assert writes == [writes[0], writes[0]]


def test_unresolved_result_preserves_attempt_and_only_the_same_request_can_resume():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    authority, consumption = _consumed_authority()

    with pytest.raises(runtime.ApprovalRefusal, match="execution_result_unresolved"):
        _record_fixture(
            authority,
            consumption,
            result_writer=lambda _request: (_ for _ in ()).throw(RuntimeError("ambiguous")),
            result_reader=lambda _consumption_id: [],
        )

    with pytest.raises(runtime.ApprovalRefusal, match="execution_result_conflict"):
        runtime._record_execution_result(
            authority,
            consumption,
            "different-fallback",
            '{"error":"fallback"}',
            hashlib.sha256(b'{"error":"fallback"}').hexdigest(),
            "failed",
            result_writer=lambda _request: pytest.fail("fallback reached writer"),
            result_reader=lambda _consumption_id: pytest.fail("fallback reached reader"),
        )

    durable = []
    result = _record_fixture(
        authority,
        consumption,
        result_writer=lambda request: durable.append(_result_row(request)) or durable[0],
        result_reader=lambda _consumption_id: list(durable),
    )
    assert result.status == "succeeded"


def test_two_zero_row_reads_leave_the_result_attempt_explicitly_unresolved():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    authority, consumption = _consumed_authority()
    writes = []
    reads = []

    with pytest.raises(runtime.ApprovalRefusal, match="execution_result_unresolved"):
        _record_fixture(
            authority,
            consumption,
            result_writer=lambda request: (
                writes.append(dict(request)) or (_ for _ in ()).throw(RuntimeError("ambiguous"))
            ),
            result_reader=lambda consumption_id: reads.append(consumption_id) or [],
        )

    assert writes == [writes[0], writes[0]]
    assert reads == [consumption.consumption_id, consumption.consumption_id]


@pytest.mark.parametrize(
    "mutation",
    ["approval_id", "canonical_result_json", "operation", "status"],
)
def test_reconciliation_rejects_foreign_or_drifted_result_rows(mutation):
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    authority, consumption = _consumed_authority()
    durable = []

    def writer(request):
        row = _result_row(request)
        if mutation == "approval_id":
            row["approval_id"] = "exa_" + "0" * 64
        elif mutation == "canonical_result_json":
            row["canonical_result_json"] = '{"receipt":"foreign"}'
            row["result_digest"] = hashlib.sha256(row["canonical_result_json"].encode()).hexdigest()
        elif mutation == "operation":
            row["operation"] = "r3_release"
            row["execution_name"] = (
                "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
                "trends-engine-oi-r3-release-staging/executions/foreign"
            )
        else:
            row["status"] = "failed"
        row["result_id"] = runtime.result_id_v2(
            row["consumption_id"],
            row["result_reference"],
            row["result_digest"],
            row["status"],
            row["completed_at"],
            origin_registry_sha256=row["origin_registry_sha256"],
            resource_manifest_sha256=row["resource_manifest_sha256"],
        )
        durable.append(row)
        raise RuntimeError("response lost")

    with pytest.raises(runtime.ApprovalRefusal, match="execution_result_conflict"):
        _record_fixture(
            authority,
            consumption,
            result_writer=writer,
            result_reader=lambda _consumption_id: durable,
        )


def test_reconciliation_rejects_duplicate_result_rows():
    """Unchanged semantics on the v2 live path: authority is issued only under new_consume
    for a selected v2 operation, and the consumption and result rows are v2 records
    (P1 runtime packet, section 6)."""
    authority, consumption = _consumed_authority()
    durable = []

    def writer(request):
        durable.append(_result_row(request))
        raise RuntimeError("response lost")

    with pytest.raises(runtime.ApprovalRefusal, match="execution_result_conflict"):
        _record_fixture(
            authority,
            consumption,
            result_writer=writer,
            result_reader=lambda _consumption_id: [durable[0], durable[0]],
        )


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("unknown_submit", [False, True])
def test_source_control_native_sdk_config_and_legacy_config(monkeypatch, bounded, unknown_submit):
    """The retained v1 capture approval record stands in for the consumed authority the
    ledger used to be seeded from: successor 1 excludes protected capture from fresh
    execution authority, and the SDK configuration under test reads the v1 row by its
    manifest digest exactly as before."""
    from google.cloud import bigquery

    from tests.unit import test_protected_snapshot_replay_integration as native

    _payload, approval, *_ = source_snapshot_runtime_fixture()
    from dataclasses import asdict

    ledger = native.NativeLedgerHTTP({}, asdict(approval))
    closed = []
    ledger._auth_request = SimpleNamespace(
        session=SimpleNamespace(close=lambda: closed.append("auth"))
    )
    ledger.close = lambda: closed.append("http")
    rpc_timeouts = []
    original_request = ledger.request

    def request(*args, **kwargs):
        rpc_timeouts.append(kwargs.get("timeout"))
        result = original_request(*args, **kwargs)
        if unknown_submit and args[0] == "POST":
            raise TimeoutError("synthetic unknown submission")
        return result

    ledger.request = request
    real_client = bigquery.Client
    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: real_client(**kwargs, _http=ledger))
    monkeypatch.setattr(
        runtime,
        "_runtime_credentials",
        lambda: native.capture_fixture.creation_fixture.CreationCredentials(),
    )
    query = runtime._source_control_query() if bounded else runtime._runtime_query
    from contextlib import nullcontext

    expectation = pytest.raises(runtime.ApprovalRefusal) if unknown_submit else nullcontext()
    with expectation:
        query(
            "sp_read_open_intelligence_execution_approval_v1",
            [bigquery.ScalarQueryParameter("manifest_sha256", "STRING", approval.manifest_sha256)],
        )
    inserts = [data for method, _, data in ledger.calls if method == "POST"]
    assert len(inserts) == 1
    if unknown_submit:
        assert len(ledger.calls) == 1
    config = inserts[0]["configuration"]["query"]
    expected = {
        "query": config["query"],
        "queryParameters": [
            {
                "name": "manifest_sha256",
                "parameterType": {"type": "STRING"},
                "parameterValue": {"value": approval.manifest_sha256},
            }
        ],
        "parameterMode": "NAMED",
        "useLegacySql": False,
    }
    if bounded:
        expected["maximumBytesBilled"] = "125000000"
    assert config == expected
    if bounded:
        assert closed == ["auth", "http"]
        assert all(value is not None and value <= 30 for value in rpc_timeouts)
    else:
        assert closed == []


@pytest.mark.parametrize("recovery", [False, True])
def test_source_control_attempt_counts_all_initial_and_recovery_submissions(monkeypatch, recovery):
    observed = []

    def submit(procedure, parameters, **kwargs):
        observed.append((procedure, kwargs))
        if len(observed) == 2:
            raise runtime.ApprovalRefusal("execution_approval_internal_refusal")
        return ()

    monkeypatch.setattr(runtime, "_runtime_query", submit)
    query = runtime._source_control_query()
    operations = [
        "approval",
        "consume",
        *(["original_result"] if recovery else []),
        "result_write",
        "result_read",
    ]
    from contextlib import suppress

    for procedure in operations:
        with suppress(runtime.ApprovalRefusal):
            query(procedure, [])
    assert len(observed) == (5 if recovery else 4)
    for _ in range(10 - len(observed)):
        query("result_read", [])
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_internal_refusal"):
        query("eleventh", [])
    assert len(observed) == 10
    assert all(
        options
        == {"allow_zero": False, "_bounded_control": True, "_source_byte_limit": 125_000_000}
        for _, options in observed
    )


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize(
    "procedure, byte_cap",
    [
        ("sp_consume_open_intelligence_source_snapshot_v1", 300_000_000),
        ("sp_read_open_intelligence_execution_approval_v1", 125_000_000),
        ("sp_record_open_intelligence_execution_result_v1", 150_000_000),
    ],
)
@pytest.mark.parametrize("reason_only", [False, True])
def test_native_control_budget_error_and_consumer_sdk_cap(
    monkeypatch, bounded, procedure, byte_cap, reason_only
):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    from tests.unit.test_production_snapshot_storage import HTTP

    calls = []

    class Transport:
        is_mtls = False
        _auth_request = SimpleNamespace(session=SimpleNamespace(close=lambda: None))

        def close(self):
            pass

        def request(self, method, url, **kwargs):
            calls.append(json.loads(kwargs["data"]))
            error = {
                "code": 400,
                "message": "private native error"
                if reason_only
                else (
                    "Query exceeded limit for bytes billed: 20142400. "
                    "20971520 or higher required. private native error"
                ),
                "errors": [
                    {"reason": "bytesBilledLimitExceeded" if reason_only else "invalidQuery"}
                ],
            }
            return HTTP.response(method, url, 400, json.dumps({"error": error}).encode())

    real_client = bigquery.Client
    monkeypatch.setattr(
        bigquery, "Client", lambda **kwargs: real_client(**kwargs, _http=Transport())
    )
    monkeypatch.setattr(runtime, "_runtime_credentials", AnonymousCredentials)
    query = runtime._source_control_query() if bounded else runtime._runtime_query
    expected = (
        "execution_approval_query_budget_exceeded"
        if bounded
        else "execution_approval_internal_refusal"
    )
    with pytest.raises(runtime.ApprovalRefusal) as caught:
        query(procedure, [])
    assert str(caught.value) == expected
    assert len(calls) == 1
    config = calls[0]["configuration"]["query"]
    assert config.get("maximumBytesBilled") == (str(byte_cap) if bounded else None)


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize(
    "procedure, cap, attempts",
    [
        ("sp_consume_open_intelligence_source_snapshot_v1", 300_000_000, 5),
        ("sp_record_open_intelligence_execution_result_v1", 150_000_000, 9),
    ],
)
def test_source_control_reserves_aggregate_before_unknown_or_successful_submissions(
    monkeypatch, unknown, procedure, cap, attempts
):
    caps = []

    def submit(procedure, parameters, **kwargs):
        caps.append(kwargs["_source_byte_limit"])
        if unknown:
            raise runtime.ApprovalRefusal("execution_approval_internal_refusal")
        return ()

    monkeypatch.setattr(runtime, "_runtime_query", submit)
    query = runtime._source_control_query()
    from contextlib import suppress

    for _ in range(attempts):
        with suppress(runtime.ApprovalRefusal):
            query(procedure, [])
    assert caps == [cap] * (attempts - 1) + [50_000_000]
    with pytest.raises(runtime.ApprovalRefusal, match="execution_approval_query_budget_exceeded"):
        query("sp_read_open_intelligence_execution_approval_v1", [])
    assert len(caps) == attempts
    assert sum(caps) == 1_250_000_000


def test_default_reader_v3_serves_the_grant_row_shape_the_daily_loader_reads():
    """Caller closure for the daily grant loader: version v3, explicit mode, the v3 read
    routine keyed by the grant digest, and a row exposing approved_by, approved_at,
    approval_phrase_sha256 and manifest_sha256; a revoked grant refuses before return."""
    from tests.unit.test_execution_approval_routine_v3 import sample_row

    row = sample_row()
    calls = []

    def query(procedure, parameters):
        calls.append((procedure, [(item.name, item.type_, item.value) for item in parameters]))
        return row

    for mode in ("", None, "consume", "NEW_CONSUME"):
        with pytest.raises(Exception, match="execution_origin_mode_forbidden"):
            runtime._default_approval_reader(
                row["manifest_sha256"],
                version=runtime._APPROVAL_VERSION_V3,
                mode=mode,
                query=query,
            )
    assert calls == []
    approval = runtime._default_approval_reader(
        row["manifest_sha256"],
        version=runtime._APPROVAL_VERSION_V3,
        mode="new_consume",
        query=query,
    )
    assert calls == [
        (
            "sp_read_open_intelligence_execution_approval_v3",
            [("manifest_sha256", "STRING", row["manifest_sha256"])],
        )
    ]
    shape = {
        "approved_by": approval.approved_by,
        "approved_at": approval.approved_at,
        "approval_phrase_sha256": approval.approval_phrase_sha256,
        "manifest_sha256": approval.manifest_sha256,
    }
    assert shape == {name: row[name] for name in shape}
    assert approval.revocation_state == "active"
    assert approval.revoked_at is None
    revoked = sample_row(
        revocation_state="revoked", revoked_at=row["approved_at"] + timedelta(days=1)
    )
    with pytest.raises(runtime.ApprovalRefusal, match=r"^execution_approval_revoked$"):
        runtime._default_approval_reader(
            revoked["manifest_sha256"],
            version=runtime._APPROVAL_VERSION_V3,
            mode="new_consume",
            query=lambda *_: revoked,
        )
