import hashlib
import json

import pytest
import yaml
from scripts.staging import render_open_intelligence_execution_package as renderer

from tests.unit.test_open_intelligence_execution_approval_contract import OPERATION_CASES
from tests.unit.test_operator_callers_v2 import canonical, generation, v1_manifest


@pytest.fixture(autouse=True)
def _packaged_generation(monkeypatch):
    monkeypatch.setattr(renderer, "_active_generation", generation)


def valid_manifest(operation):
    return v1_manifest(operation)


def _canonical(payload) -> bytes:
    return canonical(payload, "historical_read")


def _digest(payload) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


@pytest.mark.parametrize(
    "operation", [operation for operation in OPERATION_CASES if operation != "brain_read"]
)
def test_renderer_emits_exact_job_authority_for_every_operation(
    operation,
    tmp_path,
    monkeypatch,
):
    payload = valid_manifest(operation)
    request = tmp_path / f"{operation}.json"
    output = tmp_path / f"{operation}.yaml"
    request.write_bytes(_canonical(payload))
    digest = _digest(payload)
    if operation == "bootstrap_migration_apply":
        monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_GENERATION", "11")
        monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_SIGNATURE_GENERATION", "12")

    assert (
        renderer.main(
            [
                "--manifest-file",
                str(request.resolve()),
                "--manifest-sha256",
                digest,
                "--output-file",
                str(output.resolve()),
            ]
        )
        == 0
    )

    resource = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert resource["apiVersion"] == "run.googleapis.com/v1"
    assert resource["kind"] == "Job"
    assert resource["metadata"]["name"] == payload["job_resource"].rsplit("/", 1)[1]
    template = resource["spec"]["template"]
    annotations = template["metadata"]["annotations"]
    expected_annotations = {
        "42.ogilvy/execution-approval-sha256": digest,
        "42.ogilvy/source-sha": payload["source_sha"],
    }
    if operation == "bootstrap_migration_apply":
        expected_annotations.update(
            {
                "42.ogilvy/bootstrap-manifest-sha256": digest,
                "42.ogilvy/bootstrap-manifest-generation": "11",
                "42.ogilvy/bootstrap-signature-generation": "12",
            }
        )
    assert annotations == expected_annotations
    task = template["spec"]["template"]["spec"]
    assert task["serviceAccountName"] == payload["service_identity"]
    assert task["maxRetries"] == 0
    assert task["timeoutSeconds"] == payload["timeout_seconds"]
    container = task["containers"][0]
    assert container["image"] == payload["image_uri"]
    assert container["command"] == payload["command"]
    assert container["args"] == payload["arguments"]
    assert [row["name"] for row in container["env"]] == [
        row["name"] for row in payload["environment"]
    ] + payload["secrets"]


def test_renderer_is_exclusive_and_refuses_grammar_or_digest_drift(tmp_path, capsys):
    payload = valid_manifest("r3_release")
    request = tmp_path / "manifest.json"
    output = tmp_path / "job.yaml"
    request.write_bytes(_canonical(payload))
    digest = _digest(payload)
    assert renderer.main([]) == 2
    assert (
        renderer.main(
            [
                "--manifest-file",
                str(request.resolve()),
                "--manifest-sha256",
                "0" * 64,
                "--output-file",
                str(output.resolve()),
            ]
        )
        == 1
    )
    output.write_text("occupied", encoding="utf-8")
    assert (
        renderer.main(
            [
                "--manifest-file",
                str(request.resolve()),
                "--manifest-sha256",
                digest,
                "--output-file",
                str(output.resolve()),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert all(set(json.loads(line)) == {"error"} for line in captured.err.splitlines())


def test_success_receipt_digest_is_exact_yaml_sha(tmp_path, capsys):
    payload = valid_manifest("r3_release")
    request = tmp_path / "manifest.json"
    output = tmp_path / "job.yaml"
    request.write_bytes(_canonical(payload))
    digest = _digest(payload)
    assert (
        renderer.main(
            [
                "--manifest-file",
                str(request.resolve()),
                "--manifest-sha256",
                digest,
                "--output-file",
                str(output.resolve()),
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {
        "contract_version": "open_intelligence_execution_package_v1",
        "manifest_sha256": digest,
        "operation": "r3_release",
        "output_file": str(output.resolve()),
        "resource_digest": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
