"""Brain-only extension tests for the existing staging job deploy owner."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.staging import deploy_open_intelligence_job as deployer

from tests.unit.test_operator_callers_v2 import generation, registry, retained_contract

V1 = "open_intelligence_execution_manifest_v1"
IMAGE = "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:" + "a" * 64
SOURCE_SHA = "b" * 40
DEPENDENCY_SHA = "c" * 64
SBOM_SHA = "d" * 64
CONTRACT_SHA = "f62be04fd31f6236855c05e5907d87f87b5d89f644512c2391420d46da6b4962"
ARGUMENTS = (
    "--target",
    "staging",
    "--run-id",
    "run_20260903_dynamic_apply_v2_r16",
    "--signal-id",
    "sig_" + "e" * 64,
    "--research-depth",
    "briefing",
)
BUILD_RESOURCE = (
    "projects/ogilvy-trends-v2/locations/us-central1/builds/12345678-1234-1234-1234-123456789abc"
)


def _build_payload(**overrides):
    payload = {
        "name": BUILD_RESOURCE,
        "id": "12345678-1234-1234-1234-123456789abc",
        "projectId": "ogilvy-trends-v2",
        "status": "SUCCESS",
        "finishTime": "2026-08-30T10:00:00Z",
        "sourceProvenance": {
            "resolvedConnectedRepository": {"revision": SOURCE_SHA},
        },
        "results": {
            "images": [
                {
                    "name": (
                        "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine:67ac000"
                    ),
                    "digest": "sha256:" + "a" * 64,
                }
            ]
        },
    }
    payload.update(overrides)
    return payload


def _v1_origin(operation="brain_read"):
    return deployer.retained_v1_origin(operation, registry=registry())


@pytest.fixture(autouse=True)
def _packaged_generation(monkeypatch):
    monkeypatch.setattr(deployer, "_active_generation", generation)


def _brain():
    return deployer.desired_brain_job(
        IMAGE,
        SOURCE_SHA,
        DEPENDENCY_SHA,
        SBOM_SHA,
        arguments=ARGUMENTS,
        execution_approval_sha256="f" * 64,
    )


def _brain_execution_manifest():
    return SimpleNamespace(
        manifest_version=V1,
        operation="brain_read",
        image_uri=IMAGE,
        source_sha=SOURCE_SHA,
        service_identity=deployer.BRAIN_SERVICE_ACCOUNT,
        job_resource=(
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-brain-staging"
        ),
        command=("python",),
        arguments=("scripts/staging/run_live_intelligence_brain.py", *ARGUMENTS),
        environment=deployer.BRAIN_ENVIRONMENT,
        secrets=(),
        max_retries=0,
        timeout_seconds=900,
    )


def _brain_manifest_cli(path, *, apply=False):
    values = [
        "--brain",
        "--execution-manifest-file",
        str(path.resolve()),
        "--execution-manifest-sha256",
        "f" * 64,
        "--dependency-lock-sha256",
        DEPENDENCY_SHA,
        "--runtime-sbom-sha256",
        SBOM_SHA,
    ]
    if apply:
        values.append("--apply")
    return values


def test_brain_job_and_manifest_are_exact_without_changing_existing_job() -> None:
    existing = deployer.desired_job(IMAGE, SOURCE_SHA)
    brain = _brain()
    assert existing.job == "trends-engine-open-intelligence-staging"
    assert existing.args == ("scripts/run_rss_now.py",)
    assert brain.job == "trends-engine-oi-brain-staging"
    assert brain.command == ("python",)
    assert brain.args == ("scripts/staging/run_live_intelligence_brain.py", *ARGUMENTS)
    assert brain.memory == "4Gi"
    assert brain.timeout_seconds == 900
    assert brain.max_retries == 0
    assert brain.environment == (
        ("BIGQUERY_DATASET", "trends_v2_staging"),
        ("GCP_PROJECT", "ogilvy-trends-v2"),
        ("TRENDS_ENV", "staging"),
    )
    assert brain.secret_names == ()
    assert brain.writable_code_mounts == ()

    manifest = deployer.brain_deployment_manifest(brain)
    assert tuple(manifest) == (
        "contract_sha256",
        "dependency_lock_sha256",
        "runtime_sbom_sha256",
        "source_sha",
        "job_resource",
        "region",
        "image_uri_with_digest",
        "service_identity",
        "command",
        "arguments",
        "environment_names",
        "secret_names",
        "memory",
        "timeout_seconds",
        "max_retries",
        "writable_code_mounts",
        "operation",
    )
    assert manifest["contract_sha256"] == CONTRACT_SHA
    assert manifest["operation"] == "brain_read"
    assert dict(brain.annotations) == {
        "42.ogilvy/contract-sha256": CONTRACT_SHA,
        "42.ogilvy/dependency-lock-sha256": DEPENDENCY_SHA,
        "42.ogilvy/runtime-sbom-sha256": SBOM_SHA,
        "42.ogilvy/source-sha": SOURCE_SHA,
        "42.ogilvy/deployment-manifest-sha256": deployer.canonical_digest(manifest),
        "42.ogilvy/execution-approval-sha256": "f" * 64,
    }


def test_brain_deploy_commands_target_only_the_isolated_job_and_read_back() -> None:
    dry_run = deployer.render_brain_dry_run(_brain())
    assert "trends-engine-oi-brain-staging" in dry_run
    assert "trends-engine-open-intelligence-staging" not in dry_run
    assert '"memory": "4Gi"' in dry_run
    assert '"timeoutSeconds": "900"' in dry_run
    assert '"maxRetries": 0' in dry_run
    assert '"replace"' in dry_run
    assert "--set-annotations=" not in dry_run
    assert "readback" in dry_run

    resource = deployer.brain_job_resource(_brain())
    assert resource["spec"]["template"]["metadata"]["annotations"] == dict(_brain().annotations)
    assert resource["spec"]["template"]["spec"]["template"]["spec"]["containers"][0][
        "args"
    ] == list(_brain().args)


def test_brain_deploy_cli_owns_the_exact_dry_run_without_mutation(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest_path = tmp_path / "brain-manifest.json"
    manifest_path.write_bytes(b"manifest")
    monkeypatch.setattr(
        deployer,
        "load_brain_execution_manifest",
        lambda _path, _digest, **_kwargs: _brain_execution_manifest(),
    )
    assert deployer.main(_brain_manifest_cli(manifest_path)) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "trends-engine-oi-brain-staging" in output.out
    assert '"mode": "dry-run"' in output.out


def test_brain_readback_requires_every_manifest_field_and_annotation() -> None:
    brain = _brain()
    snapshot = deployer.brain_snapshot_from_desired(brain)
    assert deployer.brain_snapshot_mismatches(snapshot, brain) == ()
    mutated = deployer.brain_snapshot_from_desired(brain, runtime_sbom_sha256="f" * 64)
    assert "runtime_sbom_sha256" in deployer.brain_snapshot_mismatches(mutated, brain)


def test_brain_reconcile_creates_then_requires_exact_readback() -> None:
    brain = _brain()
    states = iter((None, deployer.brain_snapshot_from_desired(brain)))
    mutations = []
    assert (
        deployer.reconcile_brain_job(
            brain,
            read_current=lambda: next(states),
            mutate=lambda operation, desired: mutations.append((operation, desired.job)),
        )
        == "created"
    )
    assert mutations == [("create", "trends-engine-oi-brain-staging")]

    bad_states = iter(
        (
            None,
            deployer.brain_snapshot_from_desired(brain, runtime_sbom_sha256="f" * 64),
        )
    )
    with pytest.raises(deployer.DeploymentError, match="readback mismatch"):
        deployer.reconcile_brain_job(
            brain,
            read_current=lambda: next(bad_states),
            mutate=lambda _operation, _desired: None,
        )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "projects/foreign/locations/us-central1/builds/123",
        "projects/ogilvy-trends-v2/builds/123",
        "projects/ogilvy-trends-v2/locations/us-central1/builds/../123",
    ],
)
def test_cloud_build_resource_lookup_is_exact(value) -> None:
    with pytest.raises(deployer.DeploymentError, match="Cloud Build resource"):
        deployer.validate_cloud_build_resource(value)
    assert deployer.validate_cloud_build_resource(BUILD_RESOURCE) == BUILD_RESOURCE


def test_cloud_build_authority_binds_resolved_source_and_result_image() -> None:
    authority = deployer.validate_cloud_build_authority(
        _build_payload(),
        build_resource=BUILD_RESOURCE,
        desired=_brain(),
        origin=_v1_origin(),
    )
    assert authority.build_resource == BUILD_RESOURCE
    assert authority.resolved_source_kind == "resolvedConnectedRepository"
    assert authority.resolved_source_sha == SOURCE_SHA
    assert authority.result_image_digest == "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("wrong_name", lambda p: p.update(name=BUILD_RESOURCE + "-other")),
        ("wrong_id", lambda p: p.update(id="other")),
        ("foreign_project", lambda p: p.update(projectId="foreign")),
        ("not_success", lambda p: p.update(status="FAILURE")),
        ("bad_finish", lambda p: p.update(finishTime="not-a-time")),
        ("missing_source", lambda p: p.update(sourceProvenance={})),
        (
            "ambiguous_source",
            lambda p: p["sourceProvenance"].update(resolvedGitSource={"revision": SOURCE_SHA}),
        ),
        (
            "branch_text",
            lambda p: p["sourceProvenance"]["resolvedConnectedRepository"].update(revision="main"),
        ),
        (
            "source_mismatch",
            lambda p: p["sourceProvenance"]["resolvedConnectedRepository"].update(
                revision="c" * 40
            ),
        ),
        ("missing_images", lambda p: p.update(results={"images": []})),
        (
            "duplicate_image",
            lambda p: p["results"]["images"].append(deepcopy(p["results"]["images"][0])),
        ),
        (
            "digest_mismatch",
            lambda p: p["results"]["images"][0].update(digest="sha256:" + "f" * 64),
        ),
        (
            "repository_mismatch",
            lambda p: p["results"]["images"][0].update(
                name="us-central1-docker.pkg.dev/ogilvy-trends-v2/other/image:tag"
            ),
        ),
    ],
)
def test_cloud_build_authority_rejects_every_named_attack(name, mutate) -> None:
    payload = _build_payload()
    mutate(payload)
    with pytest.raises(deployer.DeploymentError):
        deployer.validate_cloud_build_authority(
            payload,
            build_resource=BUILD_RESOURCE,
            desired=_brain(),
            origin=_v1_origin(),
        )


def test_retained_v1_apply_refuses_before_git_build_or_job_discovery(monkeypatch) -> None:
    # The retained v1 job is inspection only under historical read; a fresh apply needs
    # a v2 execution manifest and its resolved origin (covered in the v2 caller suite).
    events = []
    monkeypatch.setattr(deployer, "_check_local_git", lambda *_args: events.append("git"))
    monkeypatch.setattr(
        deployer,
        "derive_brain_dependency_authority",
        lambda **_kwargs: events.append("digests"),
    )
    monkeypatch.setattr(
        deployer,
        "_describe_brain",
        lambda *_args, **_kwargs: events.append("job") or pytest.fail("job discovery reached"),
    )
    with pytest.raises(deployer.DeploymentError, match="inspection only"):
        deployer.apply_brain_job(
            _brain(),
            runner=lambda _command: events.append("mutation"),
            resolver=lambda _name: "gcloud",
            environment={"OI_BRAIN_CLOUD_BUILD_RESOURCE": BUILD_RESOURCE},
            build_reader=lambda _resource: events.append("build") or _build_payload(),
        )
    assert events == []


def test_canonical_deployment_receipt_is_one_line_and_exact(tmp_path, capsys, monkeypatch) -> None:
    build_authority = deployer.validate_cloud_build_authority(
        _build_payload(), build_resource=BUILD_RESOURCE, desired=_brain(), origin=_v1_origin()
    )
    receipt = deployer.build_brain_deployment_receipt(
        desired=_brain(),
        snapshot=deployer.brain_snapshot_from_desired(_brain()),
        build_authority=build_authority,
        dependency_lock_sha256=DEPENDENCY_SHA,
        runtime_sbom_sha256=SBOM_SHA,
    )
    rendered = deployer.render_brain_deployment_receipt(receipt)
    assert rendered.endswith("\n")
    assert len(rendered.splitlines()) == 1
    assert json.loads(rendered) == {
        "arguments": list(ARGUMENTS),
        "build_identity": deployer.cloud_build_identity_payload(build_authority),
        "command": "python scripts/staging/run_live_intelligence_brain.py",
        "dependency_lock_sha256": DEPENDENCY_SHA,
        "deployment_manifest_sha256": dict(_brain().annotations)[
            "42.ogilvy/deployment-manifest-sha256"
        ],
        "image_digest": "sha256:" + "a" * 64,
        "receipt_contract_version": "brain_deployment_receipt_v1",
        "runtime_sbom_sha256": SBOM_SHA,
        "service_identity": deployer.BRAIN_SERVICE_ACCOUNT,
        "source_sha": SOURCE_SHA,
    }

    monkeypatch.setattr(deployer, "apply_brain_job", lambda *_args, **_kwargs: receipt)
    monkeypatch.setenv("OI_BRAIN_CLOUD_BUILD_RESOURCE", BUILD_RESOURCE)
    manifest_path = tmp_path / "brain-manifest.json"
    manifest_path.write_bytes(b"manifest")
    monkeypatch.setattr(
        deployer,
        "load_brain_execution_manifest",
        lambda _path, _digest, **_kwargs: _brain_execution_manifest(),
    )
    assert deployer.main(_brain_manifest_cli(manifest_path, apply=True)) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == rendered


def test_deployment_receipt_refuses_job_readback_drift() -> None:
    build_authority = deployer.validate_cloud_build_authority(
        _build_payload(), build_resource=BUILD_RESOURCE, desired=_brain(), origin=_v1_origin()
    )
    current = deployer.brain_snapshot_from_desired(_brain())
    drifted = deployer.BrainJobSnapshot(
        manifest={**current.manifest, "memory": "8Gi"},
        annotations=current.annotations,
    )
    with pytest.raises(deployer.DeploymentError, match="readback mismatch"):
        deployer.build_brain_deployment_receipt(
            desired=_brain(),
            snapshot=drifted,
            build_authority=build_authority,
            dependency_lock_sha256=DEPENDENCY_SHA,
            runtime_sbom_sha256=SBOM_SHA,
        )


LIVE_BUILD_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "cloud_build_connected_repository_v1.json"
)
LIVE_SOURCE_SHA = "dddafd6a7af284c39c42afe1b1eb387e2aafb2b6"
LIVE_IMAGE = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@"
    "sha256:8afb12031c74d85f557fb2f68c503a8e9b9f141cf588efd1c330b87c63cd249e"
)
LIVE_BUILD_RESOURCE = (
    "projects/ogilvy-trends-v2/locations/us-central1/builds/6b0e1e79-c819-4732-b189-df736dc09b16"
)


def _live_build_payload():
    return json.loads(LIVE_BUILD_FIXTURE.read_text(encoding="utf-8"))


def _live_brain():
    return deployer.desired_brain_job(
        LIVE_IMAGE,
        LIVE_SOURCE_SHA,
        DEPENDENCY_SHA,
        SBOM_SHA,
        arguments=ARGUMENTS,
        execution_approval_sha256="f" * 64,
    )


def test_live_connected_repository_build_binds_deployment_authority() -> None:
    authority = deployer.validate_cloud_build_authority(
        _live_build_payload(),
        build_resource=LIVE_BUILD_RESOURCE,
        desired=_live_brain(),
        origin=_v1_origin(),
    )
    assert authority.build_resource == LIVE_BUILD_RESOURCE
    assert authority.build_id == "6b0e1e79-c819-4732-b189-df736dc09b16"
    assert authority.resolved_source_kind == "resolvedConnectedRepository"
    assert authority.resolved_source_sha == LIVE_SOURCE_SHA
    assert authority.result_image_digest == LIVE_IMAGE.rsplit("@", 1)[1]
    assert authority.result_image_name.endswith(":" + LIVE_SOURCE_SHA)


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        (
            "dual_envelope_mismatch",
            lambda p: p.update(sourceProvenance={"resolvedRepoSource": {"commitSha": "c" * 40}}),
        ),
        (
            "wrong_repository",
            lambda p: p["source"]["connectedRepository"].update(
                repository="projects/ogilvy-trends-v2/locations/us-central1/connections/"
                "tev2-gh/repositories/other"
            ),
        ),
        ("branch_revision", lambda p: p["source"]["connectedRepository"].update(revision="master")),
        ("extra_connected_field", lambda p: p["source"]["connectedRepository"].update(dir=".")),
        ("unsupported_source", lambda p: p.update(source={"gitSource": {"revision": "c" * 40}})),
        ("missing_source", lambda p: p.pop("source")),
        ("foreign_project_number", lambda p: p.update(name=p["name"].replace("5903", "5904"))),
        (
            "second_unrelated_image",
            lambda p: p["results"]["images"].append(
                {
                    "name": "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/other:v1",
                    "digest": "sha256:" + "e" * 64,
                }
            ),
        ),
        (
            "source_differs_from_local",
            lambda p: p["source"]["connectedRepository"].update(revision="c" * 40),
        ),
    ],
)
def test_live_connected_repository_build_refuses_every_named_attack(name, mutate) -> None:
    payload = _live_build_payload()
    mutate(payload)
    with pytest.raises(deployer.DeploymentError):
        deployer.validate_cloud_build_authority(
            payload,
            build_resource=LIVE_BUILD_RESOURCE,
            desired=_live_brain(),
            origin=_v1_origin(),
        )


OPERATION_ARGUMENTS = {
    "source_snapshot_capture": (
        "scripts/staging/capture_protected_production_snapshot.py",
        "--cutoff-date",
        "2026-09-07",
        "--mode",
        "initial",
    ),
    "bootstrap_migration_apply": (
        "scripts/migrations/create_open_intelligence_execution_approval_store.py",
        "apply",
    ),
    "migration_apply": ("scripts/migrations/create_open_intelligence_v2.py", "apply"),
    "collection_exposure_issue": ("scripts/staging/issue_collection_exposure_receipts.py",),
    "r3_apply": ("scripts/staging/replay_open_intelligence.py",),
    "r3_proof_issue": (
        "scripts/staging/issue_r3_execution_proof.py",
        "--r3-execution-name",
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-apply-staging/executions/example",
    ),
    "r3_release": ("scripts/staging/release_open_intelligence_run.py",),
    "brain_read": ("scripts/staging/run_live_intelligence_brain.py", *ARGUMENTS),
    "wave1_pilot": ("scripts/run_rss_now.py",),
}
PRODUCTION_JOBS = (
    "trends-engine-pipeline",
    "trends-engine-phase2",
    "trends-engine-regen",
    "trends-engine-resend",
)


def _operation_manifest(operation, **overrides):
    contract = deployer.execution_approval._OPERATION_CONTRACTS[operation]
    environment = contract.get("environment") or deployer.BRAIN_ENVIRONMENT
    fields = {
        "manifest_version": V1,
        "operation": operation,
        "image_uri": IMAGE,
        "source_sha": SOURCE_SHA,
        "service_identity": contract["identity"],
        "job_resource": f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{contract['job']}",
        "command": ("python",),
        "arguments": OPERATION_ARGUMENTS[operation],
        "environment": tuple(environment),
        "secrets": tuple(contract.get("secrets") or ()),
        "max_retries": 0,
        "timeout_seconds": 600 if operation == "source_snapshot_capture" else 900,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _describe_payload(desired):
    payload = deepcopy(deployer.brain_job_resource(desired))
    payload["metadata"]["selfLink"] = (
        f"/apis/run.googleapis.com/v1/namespaces/{deployer.PROJECT_NUMBER}/jobs/{desired.job}"
    )
    payload["metadata"]["labels"] = {"cloud.googleapis.com/location": deployer.REGION}
    return payload


def test_source_snapshot_profile_has_exact_resources_without_changing_legacy_manifests():
    desired = _desired_for("source_snapshot_capture")
    resource = deployer.brain_job_resource(desired)
    task = resource["spec"]["template"]["spec"]["template"]["spec"]
    assert task["containers"][0]["resources"]["limits"] == {"cpu": "2", "memory": "8Gi"}
    assert task["timeoutSeconds"] == "600"
    assert task["maxRetries"] == 0
    assert resource["spec"]["template"]["spec"]["taskCount"] == 1
    assert resource["spec"]["template"]["spec"]["parallelism"] == 1
    assert deployer.brain_deployment_manifest(desired)["cpu"] == "2"
    assert desired.to_record()["cpu"] == "2"
    for operation in OPERATION_ARGUMENTS:
        if operation == "source_snapshot_capture":
            continue
        legacy = _desired_for(operation)
        assert legacy.memory == "4Gi"
        assert legacy.timeout_seconds == 900
        assert "cpu" not in deployer.brain_deployment_manifest(legacy)
        assert "cpu" not in legacy.to_record()


@pytest.mark.parametrize("cpu", ["2", "2000m", "1", "4"])
def test_source_snapshot_cpu_readback_is_exact_and_normalizes_native_millicpu(cpu):
    desired = _desired_for("source_snapshot_capture")
    resource = _describe_payload(desired)
    limits = resource["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["resources"][
        "limits"
    ]
    limits["cpu"] = cpu
    snapshot = deployer.parse_brain_job_snapshot(resource)
    mismatches = deployer.brain_snapshot_mismatches(snapshot, desired)
    if cpu in ("2", "2000m"):
        assert mismatches == ()
        assert snapshot.manifest["cpu"] == "2"
    else:
        assert "cpu" in mismatches


@pytest.mark.parametrize("timeout", [599, 601, 900])
def test_source_snapshot_requires_approved_600_second_profile(timeout):
    with pytest.raises(deployer.DeploymentError, match="snapshot"):
        _desired_for("source_snapshot_capture", timeout_seconds=timeout)


def _desired_for(operation, **overrides):
    generations = ("3", "4") if operation == "bootstrap_migration_apply" else None
    return deployer.desired_operation_job_from_execution_manifest(
        _operation_manifest(operation, **overrides),
        operation=operation,
        execution_approval_sha256="f" * 64,
        dependency_lock_sha256=DEPENDENCY_SHA,
        runtime_sbom_sha256=SBOM_SHA,
        bootstrap_generations=generations,
    )


def test_annotation_sets_match_the_runtime_authority_reader_per_operation() -> None:
    durable = {"42.ogilvy/execution-approval-sha256", "42.ogilvy/source-sha"}
    for operation in deployer.OPERATION_JOBS:
        desired = _desired_for(operation)
        keys = set(dict(desired.annotations))
        assert keys == set(deployer.operation_annotation_keys(operation))
        if operation == "brain_read":
            assert keys == set(deployer.execution_approval._BRAIN_DURABLE_ANNOTATIONS)
        elif operation == "bootstrap_migration_apply":
            assert keys == durable | {
                "42.ogilvy/bootstrap-manifest-sha256",
                "42.ogilvy/bootstrap-manifest-generation",
                "42.ogilvy/bootstrap-signature-generation",
            }
            values = dict(desired.annotations)
            assert values["42.ogilvy/bootstrap-manifest-sha256"] == "f" * 64
            assert values["42.ogilvy/bootstrap-manifest-generation"] == "3"
            assert values["42.ogilvy/bootstrap-signature-generation"] == "4"
        else:
            assert keys == set(deployer.execution_approval._DURABLE_ANNOTATIONS)

    with pytest.raises(deployer.DeploymentError, match="bootstrap object generations"):
        deployer.desired_operation_job_from_execution_manifest(
            _operation_manifest("bootstrap_migration_apply"),
            operation="bootstrap_migration_apply",
            execution_approval_sha256="f" * 64,
            dependency_lock_sha256=DEPENDENCY_SHA,
            runtime_sbom_sha256=SBOM_SHA,
        )
    with pytest.raises(deployer.DeploymentError, match="bootstrap object generations"):
        deployer.desired_operation_job_from_execution_manifest(
            _operation_manifest("r3_apply"),
            operation="r3_apply",
            execution_approval_sha256="f" * 64,
            dependency_lock_sha256=DEPENDENCY_SHA,
            runtime_sbom_sha256=SBOM_SHA,
            bootstrap_generations=("3", "4"),
        )
    for bad in (("0", "4"), ("3", "x"), ("3",)):
        with pytest.raises(deployer.DeploymentError, match="bootstrap"):
            deployer.desired_operation_job_from_execution_manifest(
                _operation_manifest("bootstrap_migration_apply"),
                operation="bootstrap_migration_apply",
                execution_approval_sha256="f" * 64,
                dependency_lock_sha256=DEPENDENCY_SHA,
                runtime_sbom_sha256=SBOM_SHA,
                bootstrap_generations=bad,
            )


def test_readback_accepts_a_template_without_the_empty_labels_map() -> None:
    # Cloud Run drops an empty labels map from the execution template on readback.
    desired = _desired_for("bootstrap_migration_apply")
    payload = _describe_payload(desired)
    del payload["spec"]["template"]["metadata"]["labels"]
    snapshot = deployer.parse_brain_job_snapshot(payload)
    assert deployer.brain_snapshot_mismatches(snapshot, desired) == ()

    payload["spec"]["template"]["metadata"]["labels"] = "not-a-map"
    with pytest.raises(deployer.DeploymentError, match=r"spec\.template\.metadata\.labels"):
        deployer.parse_brain_job_snapshot(payload)


def test_deployment_receipt_binds_ordinary_operations_without_digest_annotations() -> None:
    desired = _desired_for("r3_apply", image_uri=IMAGE, source_sha=SOURCE_SHA)
    build_authority = deployer.validate_cloud_build_authority(
        _build_payload(),
        build_resource=BUILD_RESOURCE,
        desired=desired,
        origin=_v1_origin("r3_apply"),
    )
    receipt = deployer.build_brain_deployment_receipt(
        desired=desired,
        snapshot=deployer.brain_snapshot_from_desired(desired),
        build_authority=build_authority,
        dependency_lock_sha256=DEPENDENCY_SHA,
        runtime_sbom_sha256=SBOM_SHA,
    )
    assert receipt.service_identity == deployer.OPERATION_JOBS["r3_apply"][1]
    assert receipt.dependency_lock_sha256 == DEPENDENCY_SHA
    assert receipt.runtime_sbom_sha256 == SBOM_SHA
    with pytest.raises(deployer.DeploymentError, match="authority differs"):
        deployer.build_brain_deployment_receipt(
            desired=desired,
            snapshot=deployer.brain_snapshot_from_desired(desired),
            build_authority=build_authority,
            dependency_lock_sha256="0" * 64,
            runtime_sbom_sha256=SBOM_SHA,
        )


def test_reconcile_replaces_an_unparseable_legacy_job_and_still_reads_back_strictly() -> None:
    # Live legacy jobs (2 September 2026) carry no 42.ogilvy annotations and omit
    # parallelism, so the pre-mutation read cannot parse. That is drift to replace,
    # never a reason to stop; the post-mutation readback stays strict.
    desired = _desired_for("r3_apply")
    reads = iter(
        (
            deployer.CurrentJobUnparseable("job readback malformed at parallelism"),
            deployer.brain_snapshot_from_desired(desired),
        )
    )

    def read_current():
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    mutations = []
    assert (
        deployer.reconcile_brain_job(
            desired,
            read_current=read_current,
            mutate=lambda operation, job: mutations.append((operation, job.job)),
        )
        == "updated"
    )
    assert mutations == [("update", desired.job)]

    still_bad = iter(
        (
            deployer.CurrentJobUnparseable("job readback malformed at parallelism"),
            deployer.CurrentJobUnparseable("job readback malformed at parallelism"),
        )
    )

    def read_bad():
        raise next(still_bad)

    with pytest.raises(deployer.CurrentJobUnparseable):
        deployer.reconcile_brain_job(
            desired, read_current=read_bad, mutate=lambda _operation, _job: None
        )

    with pytest.raises(deployer.DeploymentError, match="describe failed"):
        deployer.reconcile_brain_job(
            desired,
            read_current=lambda: (_ for _ in ()).throw(
                deployer.DeploymentError("gcloud Brain describe failed")
            ),
            mutate=lambda _operation, _job: pytest.fail("mutated after a failed read"),
        )


def test_describe_classifies_a_malformed_current_job_as_unparseable(monkeypatch) -> None:
    desired = _desired_for("r3_apply")
    payload = _describe_payload(desired)
    del payload["spec"]["template"]["spec"]["parallelism"]
    payload["spec"]["template"]["metadata"]["annotations"] = {}
    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(payload), stderr=""
    )
    with pytest.raises(deployer.CurrentJobUnparseable):
        deployer._describe_brain(desired, "gcloud", lambda _command: result)


def test_readback_with_a_foreign_annotation_refuses() -> None:
    desired = _desired_for("r3_apply")
    payload = _describe_payload(desired)
    payload["spec"]["template"]["metadata"]["annotations"]["42.ogilvy/contract-sha256"] = "f" * 64
    with pytest.raises(deployer.DeploymentError, match="annotation set differs"):
        deployer.parse_brain_job_snapshot(payload)


@pytest.mark.parametrize("operation", sorted(deployer.execution_approval._OPERATION_CONTRACTS))
def test_every_operation_resolves_to_its_exact_job_and_identity(operation) -> None:
    contract = deployer.execution_approval._OPERATION_CONTRACTS[operation]
    desired = _desired_for(operation)
    assert desired.job == contract["job"]
    assert desired.service_account == contract["identity"]
    assert desired.args == OPERATION_ARGUMENTS[operation]
    assert desired.secret_names == tuple(contract.get("secrets") or ())
    assert desired.operation == operation
    assert deployer.brain_deployment_manifest(desired)["operation"] == operation
    assert set(dict(desired.annotations)) == set(deployer.operation_annotation_keys(operation))
    assert deployer.OPERATION_JOBS[operation] == (contract["job"], contract["identity"])
    assert desired.job not in PRODUCTION_JOBS

    snapshot = deployer.parse_brain_job_snapshot(_describe_payload(desired))
    assert deployer.brain_snapshot_mismatches(snapshot, desired) == ()


def test_manifest_for_one_operation_refuses_under_another() -> None:
    with pytest.raises(deployer.DeploymentError, match="manifest authority"):
        deployer.desired_operation_job_from_execution_manifest(
            _operation_manifest("r3_apply"),
            operation="r3_release",
            execution_approval_sha256="f" * 64,
            dependency_lock_sha256=DEPENDENCY_SHA,
            runtime_sbom_sha256=SBOM_SHA,
        )
    with pytest.raises(deployer.DeploymentError, match="manifest authority"):
        _desired_for(
            "r3_apply",
            service_identity="trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com",
        )


@pytest.mark.parametrize("job", PRODUCTION_JOBS)
def test_production_job_names_refuse_even_if_the_matrix_named_one(job, monkeypatch) -> None:
    monkeypatch.setitem(
        deployer.OPERATION_JOBS,
        "r3_apply",
        (job, "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"),
    )
    with pytest.raises(deployer.DeploymentError, match="production job"):
        deployer.desired_operation_job(
            "r3_apply",
            IMAGE,
            SOURCE_SHA,
            DEPENDENCY_SHA,
            SBOM_SHA,
            arguments=OPERATION_ARGUMENTS["r3_apply"],
            environment=deployer.BRAIN_ENVIRONMENT,
            secrets=(),
            execution_approval_sha256="f" * 64,
        )


@pytest.mark.parametrize("job", PRODUCTION_JOBS)
def test_production_job_resources_refuse(job) -> None:
    with pytest.raises(deployer.DeploymentError, match="manifest authority"):
        _desired_for(
            "r3_apply",
            job_resource=f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{job}",
        )
    assert job not in {name for name, _identity in deployer.OPERATION_JOBS.values()}


def test_wave1_manifest_renders_exactly_one_secret_binding_and_extra_secret_refuses() -> None:
    desired = _desired_for("wave1_pilot")
    assert desired.secret_names == ("SOCIALCRAWL_OGILVY_API_KEY",)
    resource = _describe_payload(desired)
    env = resource["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
    bindings = [entry for entry in env if "valueFrom" in entry]
    assert bindings == [
        {
            "name": "SOCIALCRAWL_OGILVY_API_KEY",
            "valueFrom": {"secretKeyRef": {"name": "SOCIALCRAWL_OGILVY_API_KEY", "key": "latest"}},
        }
    ]
    assert (
        deployer.brain_snapshot_mismatches(deployer.parse_brain_job_snapshot(resource), desired)
        == ()
    )

    env.append(
        {
            "name": "SOCIALCRAWL_API_KEY",
            "valueFrom": {"secretKeyRef": {"name": "SOCIALCRAWL_API_KEY", "key": "latest"}},
        }
    )
    drifted = deployer.parse_brain_job_snapshot(resource)
    assert drifted.manifest["secret_names"] == (
        "SOCIALCRAWL_API_KEY",
        "SOCIALCRAWL_OGILVY_API_KEY",
    )
    assert "secret_names" in deployer.brain_snapshot_mismatches(drifted, desired)

    brain = _desired_for("brain_read")
    brain_env = deployer.brain_job_resource(brain)["spec"]["template"]["spec"]["template"]["spec"][
        "containers"
    ][0]["env"]
    assert all("valueFrom" not in entry for entry in brain_env)


def test_operation_cli_owns_the_exact_dry_run_without_mutation(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest_path = tmp_path / "r3-proof-manifest.json"
    manifest_path.write_bytes(b"manifest")
    loads = []
    monkeypatch.setattr(
        deployer,
        "load_operation_execution_manifest",
        lambda path, digest, operation, **_kwargs: (
            loads.append((path, digest, operation)) or _operation_manifest("r3_proof_issue")
        ),
    )
    values = [
        "--operation",
        "r3_proof_issue",
        "--execution-manifest-file",
        str(manifest_path.resolve()),
        "--execution-manifest-sha256",
        "f" * 64,
        "--dependency-lock-sha256",
        DEPENDENCY_SHA,
        "--runtime-sbom-sha256",
        SBOM_SHA,
    ]
    assert deployer.main(values) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "trends-engine-oi-r3-proof-staging" in output.out
    assert '"mode": "dry-run"' in output.out
    assert loads == [(manifest_path.resolve(), "f" * 64, "r3_proof_issue")]

    with pytest.raises(deployer.DeploymentError, match="CLI is invalid"):
        deployer.main(["--operation", "trends-engine-pipeline", *values[2:]])


@pytest.mark.parametrize("operation", ["r3_proof_issue", "bootstrap_migration_apply"])
def test_canonical_manifest_fixtures_load_for_their_operation_only(operation, tmp_path) -> None:
    import hashlib

    # The retained fixtures keep placeholder contract digests; the loader selects the
    # retained v1 row by its real digest, so the copy under test carries that digest.
    source = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "open_intelligence"
        / f"execution_manifest_{operation}_v1.json"
    )
    payload = json.loads(source.read_bytes())
    payload["contract_sha256"] = retained_contract(operation)
    fixture = tmp_path / source.name
    fixture.write_bytes(
        deployer.execution_approval.canonical_manifest_bytes(
            payload, mode="historical_read", registry=registry()
        )
    )
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    manifest = deployer.load_operation_execution_manifest(fixture, digest, operation)
    desired = deployer.desired_operation_job_from_execution_manifest(
        manifest,
        operation=operation,
        execution_approval_sha256=digest,
        dependency_lock_sha256=DEPENDENCY_SHA,
        runtime_sbom_sha256=SBOM_SHA,
        bootstrap_generations=("7", "8") if operation == "bootstrap_migration_apply" else None,
    )
    job, identity = deployer.OPERATION_JOBS[operation]
    assert desired.job == job
    assert desired.service_account == identity
    assert desired.args == tuple(manifest.arguments)
    assert desired.environment == tuple(manifest.environment)
    assert dict(desired.annotations)["42.ogilvy/execution-approval-sha256"] == digest

    other = "bootstrap_migration_apply" if operation == "r3_proof_issue" else "r3_proof_issue"
    with pytest.raises(deployer.DeploymentError, match="operation is invalid"):
        deployer.load_operation_execution_manifest(fixture, digest, other)
    with pytest.raises(deployer.DeploymentError, match="digest differs"):
        deployer.load_operation_execution_manifest(fixture, "0" * 64, operation)


def test_not_found_detection_uses_the_desired_job_name() -> None:
    desired = _desired_for("r3_proof_issue")
    result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="ERROR: (gcloud.run.jobs.describe) Cannot find job [trends-engine-oi-r3-proof-staging].\n",
    )
    assert deployer._is_exact_job_not_found(result, desired.job) is True
    assert deployer._is_exact_job_not_found(result, "trends-engine-oi-brain-staging") is False


def test_deployment_reader_uses_the_shared_normalizer(monkeypatch) -> None:
    calls = []

    def refuse(payload, **kwargs):
        calls.append((payload, kwargs))
        raise deployer.execution_approval.ApprovalRefusal(
            "execution_approval_build_provenance_invalid"
        )

    origin = _v1_origin()
    monkeypatch.setattr(deployer.execution_approval, "normalize_cloud_build_response", refuse)
    with pytest.raises(deployer.DeploymentError, match="provenance"):
        deployer.validate_cloud_build_authority(
            _live_build_payload(),
            build_resource=LIVE_BUILD_RESOURCE,
            desired=_live_brain(),
            origin=origin,
        )
    assert calls[0][1] == {"origin": origin}
    assert len(calls) == 1


def test_v2_route_accepts_the_packaged_active_generation_as_loaded() -> None:
    from src.analysis.open_intelligence import execution_generations, execution_origins

    from tests.unit.test_execution_manifest_origins import manifest as v2_manifest

    active = execution_generations.active_generation()
    # The loader freezes the manifest; the deployer must take it exactly as loaded.
    assert isinstance(active.resource_manifest["resources"], tuple)
    payload = v2_manifest("brain_read")
    origin = execution_origins.resolve_origin(payload, "new_approval", active.registry)
    desired = deployer.desired_operation_job(
        "brain_read",
        payload["image_uri"],
        payload["source_sha"],
        DEPENDENCY_SHA,
        SBOM_SHA,
        arguments=tuple(payload["arguments"]),
        environment=tuple((item["name"], item["value"]) for item in payload["environment"]),
        secrets=(),
        execution_approval_sha256="f" * 64,
        origin=origin,
        generation=active,
    )
    manifest = deployer.brain_deployment_manifest(desired)
    assert manifest["job_resource"] == (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-brain-staging"
    )
    assert (
        manifest["service_identity"]
        == "intelligence-42-brain@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert manifest["manifest_version"] == "open_intelligence_execution_manifest_v2"
    assert manifest["secret_versions"] == ()
    assert manifest["contract_sha256"] == deployer.BRAIN_CONTRACT_SHA256
    assert manifest["image_uri_with_digest"] == payload["image_uri"]
    assert manifest["source_sha"] == payload["source_sha"]
    assert manifest["command"] == "python scripts/staging/run_live_intelligence_brain.py"
    assert manifest["arguments"] == tuple(payload["arguments"][1:])
    assert tuple(key for key, _value in desired.annotations) == (
        *deployer.BRAIN_ANNOTATION_KEYS,
        *deployer.V2_ANNOTATION_KEYS,
    )
    annotations = dict(desired.annotations)
    assert (
        annotations["42.ogilvy/origin-registry-sha256"]
        == execution_generations.ACTIVE_GENERATION_PAIR[0]
        == active.origin_registry_sha256
    )
    assert (
        annotations["42.ogilvy/resource-manifest-sha256"]
        == execution_generations.ACTIVE_GENERATION_PAIR[1]
        == active.resource_manifest_sha256
    )
    assert annotations["42.ogilvy/execution-approval-sha256"] == "f" * 64
    assert annotations["42.ogilvy/deployment-manifest-sha256"] == deployer.canonical_digest(
        manifest
    )
    # A secret bearing operation resolves its exact version from the frozen resources too.
    wave1 = v2_manifest("wave1_pilot")
    funded = deployer.desired_operation_job(
        "wave1_pilot",
        wave1["image_uri"],
        wave1["source_sha"],
        DEPENDENCY_SHA,
        SBOM_SHA,
        arguments=tuple(wave1["arguments"]),
        environment=tuple((item["name"], item["value"]) for item in wave1["environment"]),
        secrets=tuple(wave1["secrets"]),
        execution_approval_sha256="f" * 64,
        origin=execution_origins.resolve_origin(wave1, "new_approval", active.registry),
        generation=active,
    )
    assert funded.secret_versions == (("SOCIALCRAWL_OGILVY_API_KEY", "1"),)
    with pytest.raises(deployer.DeploymentError, match="secret resources are unavailable"):
        deployer.resolve_secret_versions((), resource_manifest={"resources": "not a sequence"})
    with pytest.raises(deployer.DeploymentError, match="secret resources are unavailable"):
        deployer.resolve_secret_versions((), resource_manifest={"resources": None})
    with pytest.raises(deployer.DeploymentError, match="secret resources are unavailable"):
        deployer.resolve_secret_versions((), resource_manifest={"resources": {"name": "x"}})
