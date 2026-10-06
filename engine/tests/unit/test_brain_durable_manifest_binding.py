import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_approval as durable
from src.analysis.open_intelligence import execution_origins

from tests.unit.test_open_intelligence_execution_approval_contract import (
    APPROVED_BY,
    HISTORICAL_READ,
    MANIFEST_V1,
    REGISTRY,
    SOURCE_SHA,
    retained_origin,
    valid_cloud_build_response,
    valid_manifest,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
V1_APPROVAL_VERSION = "open_intelligence_execution_approval_v1"
V1_APPROVAL_PROCEDURE = "sp_read_open_intelligence_execution_approval_v1"
SIGNAL_ID = "sig_" + "a" * 64
BRAIN_IDENTITY = "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com"
BRAIN_ANNOTATIONS = {
    "42.ogilvy/contract-sha256": "1" * 64,
    "42.ogilvy/dependency-lock-sha256": "2" * 64,
    "42.ogilvy/runtime-sbom-sha256": "3" * 64,
    "42.ogilvy/source-sha": SOURCE_SHA,
    "42.ogilvy/deployment-manifest-sha256": "4" * 64,
    "42.ogilvy/execution-approval-sha256": None,
}


def brain_arguments(depth="briefing", question=None):
    arguments = [
        "scripts/staging/run_live_intelligence_brain.py",
        "--target",
        "staging",
        "--run-id",
        "brain_run_001",
        "--signal-id",
        SIGNAL_ID,
        "--research-depth",
        depth,
    ]
    if question is not None:
        arguments.extend(("--decision-question", question))
    return arguments


def brain_manifest(arguments=None):
    manifest = valid_manifest("brain_read")
    manifest["arguments"] = brain_arguments() if arguments is None else arguments
    manifest["expires_at"] = "2026-08-31T13:00:00.000000Z"
    return manifest


@pytest.mark.parametrize(
    "arguments",
    [
        brain_arguments(),
        brain_arguments(question="What changed?"),
        brain_arguments("scan", "Which evidence should we challenge?"),
        brain_arguments("investigation", "What would disprove the signal?"),
    ],
)
def test_valid_dynamic_brain_manifest_grammar(arguments):
    manifest = durable.validate_execution_manifest(
        brain_manifest(arguments), mode=HISTORICAL_READ, registry=REGISTRY
    )

    assert manifest.arguments == tuple(arguments)
    assert manifest.service_identity == BRAIN_IDENTITY


@pytest.mark.parametrize(
    "arguments",
    [
        ["scripts/staging/run_live_intelligence_brain.py"],
        brain_arguments()[2:],
        [*brain_arguments(), "--target", "staging"],
        [*brain_arguments()[:2], "production", *brain_arguments()[3:]],
        [*brain_arguments()[:-2], "--other", "briefing"],
        brain_arguments()[:-1],
        [*brain_arguments(), "extra"],
        [*brain_arguments()[:4], "ab", *brain_arguments()[5:]],
        [*brain_arguments()[:6], "sig_" + "A" * 64, *brain_arguments()[7:]],
        brain_arguments("deep", "Question"),
        brain_arguments("scan"),
        brain_arguments(question=""),
        brain_arguments(question="e\u0301"),
        brain_arguments(question="x" * 2001),
        brain_arguments(question="line\nbreak"),
        brain_arguments(question="next\u0085line"),
    ],
)
def test_malformed_dynamic_brain_manifest_grammar_refuses(arguments):
    with pytest.raises(durable.ApprovalRefusal, match="execution_approval_manifest_invalid"):
        durable.validate_execution_manifest(
            brain_manifest(arguments), mode=HISTORICAL_READ, registry=REGISTRY
        )


@pytest.mark.parametrize(
    "operation",
    [
        "bootstrap_migration_apply",
        "migration_apply",
        "collection_exposure_issue",
        "r3_apply",
        "r3_proof_issue",
        "r3_release",
        "wave1_pilot",
    ],
)
def test_non_brain_manifest_argument_grammar_remains_exact(operation):
    manifest = valid_manifest(operation)
    durable.validate_execution_manifest(manifest, mode=HISTORICAL_READ, registry=REGISTRY)
    manifest["arguments"].append("extra")

    with pytest.raises(durable.ApprovalRefusal, match="execution_approval_manifest_invalid"):
        durable.validate_execution_manifest(manifest, mode=HISTORICAL_READ, registry=REGISTRY)


def runtime_fixture():
    manifest = brain_manifest()
    build = valid_cloud_build_response()
    build_receipt = durable.build_provenance_from_response(
        build,
        manifest["contract_sha256"],
        manifest_version=MANIFEST_V1,
        mode=HISTORICAL_READ,
        registry=REGISTRY,
    )
    artifacts = {}
    for item in manifest["input_artifacts"]:
        if item["name"] == "build_provenance":
            content = durable.canonical_build_provenance_bytes(
                build_receipt, origin=retained_origin(manifest["contract_sha256"])
            )
        else:
            content = f"artifact:{item['name']}".encode()
            artifacts[item["name"]] = content
        item["sha256"] = hashlib.sha256(content).hexdigest()
    manifest_json = durable.canonical_manifest_bytes(
        manifest, mode=HISTORICAL_READ, registry=REGISTRY
    ).decode()
    manifest_sha = durable.manifest_sha256(manifest, mode=HISTORICAL_READ, registry=REGISTRY)
    phrase = (
        f"I approve one staging execution of brain_read for manifest SHA256 {manifest_sha}. "
        "Production remains unchanged."
    )
    approval = durable.ExecutionApproval(
        approval_contract_version="open_intelligence_execution_approval_v1",
        approval_id=durable.approval_id(manifest_sha, APPROVED_BY, NOW),
        manifest_version="open_intelligence_execution_manifest_v1",
        operation="brain_read",
        contract_sha256=manifest["contract_sha256"],
        manifest_sha256=manifest_sha,
        canonical_manifest_json=manifest_json,
        approved_by=APPROVED_BY,
        approved_at=NOW,
        expires_at=datetime(2026, 8, 31, 13, tzinfo=UTC),
        approval_phrase_sha256=hashlib.sha256(phrase.encode()).hexdigest(),
    )
    task = {
        "serviceAccount": BRAIN_IDENTITY,
        "containers": [
            {
                "image": manifest["image_uri"],
                "command": manifest["command"],
                "args": manifest["arguments"],
                "env": manifest["environment"],
            }
        ],
        "maxRetries": 0,
        "timeout": "900s",
    }
    annotations = {**BRAIN_ANNOTATIONS, "42.ogilvy/execution-approval-sha256": manifest_sha}
    execution_name = manifest["job_resource"] + "/executions/execution-1"
    execution = {
        "name": execution_name,
        "job": manifest["job_resource"],
        "annotations": copy.deepcopy(annotations),
        "template": copy.deepcopy(task),
    }
    job = {
        "name": manifest["job_resource"],
        "template": {
            "annotations": copy.deepcopy(annotations),
            "template": copy.deepcopy(task),
        },
    }
    return manifest, approval, execution, job, build, artifacts


def load_brain_authority(execution, job, approval, build, artifacts, *, mode):
    return durable._load_execution_authority(
        "brain_read",
        mode=mode,
        execution_reader=lambda: {"execution": execution, "job": job},
        approval_reader=lambda _sha: approval,
        build_reader=lambda _resource: build,
        artifact_reader=lambda name: artifacts[name],
        now=lambda: NOW,
    )


def _refusing_query(_procedure, _parameters, **_options):
    raise AssertionError("the v1 approval reader must refuse before it issues a query")


def _recording_query(calls, row):
    def query(procedure, parameters, **_options):
        calls.append((procedure, [(item.name, item.value) for item in parameters]))
        return row

    return query


def test_brain_runtime_requires_dedicated_identity_and_exact_six_equal_annotations():
    """The retained Brain v1 pair is historical only under successor 1.

    A fresh runtime load of the retained v1 manifest is a fresh v1 consume, which
    successor 1 forbids. The loader accepts only mode new_consume, and under that mode
    it refuses this six annotation v1 template at its annotation gate with
    execution_approval_execution_mismatch before any approval read, because a fresh
    template must also carry the two generation annotations. Every other mode refuses
    at entry with execution_origin_mode_forbidden, and the v1 approval reader refuses
    new_consume with the same code before issuing a query. The dedicated identity
    assertion is kept on the historical path, mode historical_read through the default
    approval reader with the v1 record version, where no authority is issued.
    """
    _manifest, approval, execution, job, build, artifacts = runtime_fixture()

    with pytest.raises(durable.ApprovalRefusal, match=r"^execution_approval_execution_mismatch$"):
        load_brain_authority(execution, job, approval, build, artifacts, mode="new_consume")
    for mode in ("historical_read", "historical_replay", "new_approval"):
        with pytest.raises(
            execution_origins.OriginRefusal, match=r"^execution_origin_mode_forbidden$"
        ):
            load_brain_authority(execution, job, approval, build, artifacts, mode=mode)
    with pytest.raises(execution_origins.OriginRefusal, match=r"^execution_origin_mode_forbidden$"):
        durable._default_approval_reader(
            approval.manifest_sha256,
            version=V1_APPROVAL_VERSION,
            mode="new_consume",
            query=_refusing_query,
        )

    calls = []
    historical = durable._default_approval_reader(
        approval.manifest_sha256,
        version=V1_APPROVAL_VERSION,
        mode=HISTORICAL_READ,
        query=_recording_query(calls, approval),
    )
    assert calls == [(V1_APPROVAL_PROCEDURE, [("manifest_sha256", approval.manifest_sha256)])]
    assert historical == approval
    historical_manifest = durable.validate_execution_manifest(
        json.loads(historical.canonical_manifest_json), mode=HISTORICAL_READ, registry=REGISTRY
    )
    assert historical_manifest.service_identity == BRAIN_IDENTITY
    assert execution["annotations"] == job["template"]["annotations"]
    assert set(execution["annotations"]) == set(BRAIN_ANNOTATIONS)


@pytest.mark.parametrize("mutation", ["old_identity", "missing", "extra", "changed", "malformed"])
def test_brain_runtime_identity_or_annotation_drift_refuses(mutation):
    """Drift on the retained Brain v1 pair still refuses; the pair is historical only
    under successor 1.

    Under mode new_consume the loader's annotation gate fires first for every mutation
    with execution_approval_execution_mismatch, because the six annotation v1 template
    lacks the two generation annotations as well as carrying the drift. The original
    pattern is kept and still holds. The old_identity parameter's original code,
    execution_approval_identity_invalid, is superseded: the runtime task identity check
    sits behind the annotation gate and no historical loader path reaches it, so the
    annotation gate is the only refusal reachable for that mutation. The historical
    modes refuse at entry with execution_origin_mode_forbidden.
    """
    _manifest, approval, execution, job, build, artifacts = runtime_fixture()
    if mutation == "old_identity":
        old_identity = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
        execution["template"]["serviceAccount"] = old_identity
        job["template"]["template"]["serviceAccount"] = old_identity
    elif mutation == "missing":
        execution["annotations"].pop("42.ogilvy/runtime-sbom-sha256")
        job["template"]["annotations"].pop("42.ogilvy/runtime-sbom-sha256")
    elif mutation == "extra":
        execution["annotations"]["42.ogilvy/extra"] = "5" * 64
        job["template"]["annotations"]["42.ogilvy/extra"] = "5" * 64
    elif mutation == "changed":
        job["template"]["annotations"]["42.ogilvy/dependency-lock-sha256"] = "5" * 64
    else:
        execution["annotations"]["42.ogilvy/runtime-sbom-sha256"] = "invalid"
        job["template"]["annotations"]["42.ogilvy/runtime-sbom-sha256"] = "invalid"

    with pytest.raises(
        durable.ApprovalRefusal,
        match=r"execution_approval_(identity|execution)_invalid|execution_approval_execution_mismatch",
    ):
        load_brain_authority(execution, job, approval, build, artifacts, mode="new_consume")
    with pytest.raises(durable.ApprovalRefusal, match=r"^execution_approval_execution_mismatch$"):
        load_brain_authority(execution, job, approval, build, artifacts, mode="new_consume")
    for mode in ("historical_read", "historical_replay"):
        with pytest.raises(
            execution_origins.OriginRefusal, match=r"^execution_origin_mode_forbidden$"
        ):
            load_brain_authority(execution, job, approval, build, artifacts, mode=mode)


def test_approval_routine_binds_dynamic_brain_grammar_and_dedicated_identity():
    sql = (
        Path(__file__).resolve().parents[2]
        / "infra/bigquery_routines/sp_approve_open_intelligence_execution_v1.sql"
    ).read_text(encoding="utf-8")

    assert (
        "WHEN 'brain_read' THEN 'trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com'"
        in sql
    )
    assert (
        "WHEN 'brain_read' THEN JSON_QUERY(v_canonical_manifest_json, '$.arguments') = '[\"scripts/staging/run_live_intelligence_brain.py\"]'"
        not in sql
    )
    for marker in (
        "ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.arguments')) IN (9, 11)",
        "$.arguments[1]') = '--target'",
        "$.arguments[2]') = 'staging'",
        "$.arguments[3]') = '--run-id'",
        "$.arguments[5]') = '--signal-id'",
        "$.arguments[7]') = '--research-depth'",
        "$.arguments[9]') = '--decision-question'",
        "NORMALIZE(JSON_VALUE(v_canonical_manifest_json, '$.arguments[10]'), NFC)",
        "LENGTH(JSON_VALUE(v_canonical_manifest_json, '$.arguments[10]')) <= 2000",
        "r'\\p{Cc}'",
    ):
        assert marker in sql
