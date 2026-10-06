from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.staging import deploy_open_intelligence_job as deployer
from scripts.staging import render_open_intelligence_execution_package as renderer

from tests.unit.test_operator_callers_v2 import generation

IMAGE = "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:" + "a" * 64
SOURCE_SHA = "b" * 40
MANIFEST_SHA = "c" * 64
LOCK_SHA = "d" * 64
SBOM_SHA = "e" * 64
BRAIN_IDENTITY = "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com"
BRAIN_ARGS = (
    "scripts/staging/run_live_intelligence_brain.py",
    "--target",
    "staging",
    "--run-id",
    "run_20260831_brain_manifest",
    "--signal-id",
    "sig_" + "f" * 64,
    "--research-depth",
    "investigation",
    "--decision-question",
    "What changed in the source evidence?",
)


@pytest.fixture(autouse=True)
def _packaged_generation(monkeypatch):
    monkeypatch.setattr(deployer, "_active_generation", generation)
    monkeypatch.setattr(renderer, "_active_generation", generation)


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(
        manifest_version="open_intelligence_execution_manifest_v1",
        operation="brain_read",
        image_uri=IMAGE,
        source_sha=SOURCE_SHA,
        service_identity=BRAIN_IDENTITY,
        job_resource=(
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-brain-staging"
        ),
        command=("python",),
        arguments=BRAIN_ARGS,
        environment=(
            ("BIGQUERY_DATASET", "trends_v2_staging"),
            ("GCP_PROJECT", "ogilvy-trends-v2"),
            ("TRENDS_ENV", "staging"),
        ),
        secrets=(),
        max_retries=0,
        timeout_seconds=1200,
    )


def _brain_cli(manifest_path: Path, *, apply: bool = False) -> list[str]:
    argv = [
        "--brain",
        "--execution-manifest-file",
        str(manifest_path.resolve()),
        "--execution-manifest-sha256",
        MANIFEST_SHA,
        "--dependency-lock-sha256",
        LOCK_SHA,
        "--runtime-sbom-sha256",
        SBOM_SHA,
    ]
    if apply:
        argv.append("--apply")
    return argv


def test_exact_brain_cli_derives_job_and_all_six_annotations(tmp_path, monkeypatch, capsys) -> None:
    manifest_path = tmp_path / "brain-manifest.json"
    manifest_path.write_text("authority bytes are loaded by the isolated loader", encoding="utf-8")
    loads = []
    monkeypatch.setattr(
        deployer,
        "load_brain_execution_manifest",
        lambda path, digest, **_kwargs: loads.append((path, digest)) or _manifest(),
        raising=False,
    )

    assert deployer.main(_brain_cli(manifest_path)) == 0

    assert loads == [(manifest_path.resolve(), MANIFEST_SHA)]
    output = capsys.readouterr()
    assert output.err == ""
    dry_run = json.loads(output.out)
    desired = dry_run["desired_snapshot"]
    assert desired["image"] == IMAGE
    assert desired["source_sha"] == SOURCE_SHA
    assert desired["service_account"] == BRAIN_IDENTITY
    assert desired["command"] == ["python"]
    assert desired["args"] == list(BRAIN_ARGS)
    assert desired["environment"] == dict(_manifest().environment)
    assert desired["secret_names"] == []
    assert desired["max_retries"] == 0
    assert desired["timeout_seconds"] == 1200
    annotations = desired["annotations"]
    assert set(annotations) == {
        "42.ogilvy/contract-sha256",
        "42.ogilvy/dependency-lock-sha256",
        "42.ogilvy/runtime-sbom-sha256",
        "42.ogilvy/source-sha",
        "42.ogilvy/deployment-manifest-sha256",
        "42.ogilvy/execution-approval-sha256",
    }
    assert annotations["42.ogilvy/execution-approval-sha256"] == MANIFEST_SHA


@pytest.mark.parametrize(
    "mutation",
    [
        ["--image-digest", IMAGE],
        ["--source-sha", SOURCE_SHA],
        ["--brain-argument=--target"],
        ["--execution-manifest-sha256", MANIFEST_SHA],
    ],
)
def test_brain_cli_rejects_free_or_duplicate_authority(tmp_path, monkeypatch, mutation) -> None:
    manifest_path = tmp_path / "brain-manifest.json"
    manifest_path.write_bytes(b"unused")
    monkeypatch.setattr(
        deployer,
        "load_brain_execution_manifest",
        lambda _path, _digest, **_kwargs: _manifest(),
        raising=False,
    )
    with pytest.raises(deployer.DeploymentError, match="Brain deployment CLI is invalid"):
        deployer.main([*_brain_cli(manifest_path), *mutation])


def test_brain_apply_uses_manifest_derived_job_without_cloud_call(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest_path = tmp_path / "brain-manifest.json"
    manifest_path.write_bytes(b"unused")
    monkeypatch.setattr(
        deployer,
        "load_brain_execution_manifest",
        lambda _path, _digest, **_kwargs: _manifest(),
        raising=False,
    )
    applied = []
    receipt = SimpleNamespace()
    monkeypatch.setattr(
        deployer,
        "apply_brain_job",
        lambda desired, **_kwargs: applied.append(desired) or receipt,
    )
    monkeypatch.setattr(
        deployer,
        "render_brain_deployment_receipt",
        lambda value: '{"receipt":"manifest-bound"}\n' if value is receipt else "",
    )

    assert deployer.main(_brain_cli(manifest_path, apply=True)) == 0

    assert len(applied) == 1
    assert applied[0].image == IMAGE
    assert applied[0].args == BRAIN_ARGS
    assert capsys.readouterr().out == '{"receipt":"manifest-bound"}\n'


def test_non_brain_cli_stdout_remains_byte_compatible(capsys) -> None:
    argv = ["--image-digest", IMAGE, "--source-sha", SOURCE_SHA]
    expected = deployer.render_dry_run(deployer.desired_job(IMAGE, SOURCE_SHA)) + "\n"

    assert deployer.main(argv) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == expected


def test_generic_renderer_refuses_brain_read_before_creating_output(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest_path = tmp_path / "brain-manifest.json"
    output_path = tmp_path / "brain-job.yaml"
    manifest_path.write_bytes(b"unused")
    monkeypatch.setattr(
        renderer,
        "_load_manifest",
        lambda _path, _digest, **_kwargs: (
            {"operation": "brain_read"},
            b"unused",
            "historical_read",
        ),
    )

    assert (
        renderer.main(
            [
                "--manifest-file",
                str(manifest_path.resolve()),
                "--manifest-sha256",
                MANIFEST_SHA,
                "--output-file",
                str(output_path.resolve()),
            ]
        )
        == 1
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == '{"error":"execution_approval_manifest_invalid"}\n'
    assert not output_path.exists()
