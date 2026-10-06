"""Operator and deployment callers under versioned execution authority.

Every native client stays behind the existing injection seams. The trusted
generation is a fake built from the packaged registry and an inline resource
manifest, plus one parity test against the reviewed resource manifest bytes.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from scripts.staging import approve_open_intelligence_execution as approval_cli
from scripts.staging import build_open_intelligence_execution_manifest as builder
from scripts.staging import deploy_open_intelligence_job as deployer
from scripts.staging import render_open_intelligence_execution_package as renderer
from src.analysis.open_intelligence import execution_approval, execution_origins

from tests.unit.test_execution_manifest_origins import CASES as V2_CASES
from tests.unit.test_execution_manifest_origins import manifest as v2_manifest
from tests.unit.test_open_intelligence_execution_approval_contract import (
    APPROVED_BY,
    valid_manifest,
)

ENGINE_ROOT = Path(__file__).resolve().parents[2]
# The packaged active registry is the bridge registry. The reviewed resource manifest
# bytes stay bound to the amendment e registry they were reviewed under.
REGISTRY_PATH = ENGINE_ROOT / "configs/open_intelligence/execution_origins_bridge_v3.json"
# Tightening the date regex moved the registry from e6b95e35.
REGISTRY_SHA256 = "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2"
AMENDMENT_E_REGISTRY_PATH = ENGINE_ROOT / "configs/open_intelligence/execution_origins_v1.json"
AMENDMENT_E_REGISTRY_SHA256 = "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
REVIEWED_RESOURCE_MANIFEST_SHA256 = (
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
)
# The packaged manifest carries the reviewed bytes; R03_RESOURCE_MANIFEST may point
# at the reviewed proposal copy instead, and the digest assertion holds for either.
REVIEWED_RESOURCE_MANIFEST = Path(
    os.environ.get(
        "R03_RESOURCE_MANIFEST",
        str(ENGINE_ROOT / "configs/open_intelligence/resource_manifest_v1.json"),
    )
)
V1 = "open_intelligence_execution_manifest_v1"
V2 = "open_intelligence_execution_manifest_v2"
PROJECT = "ogilvy-trends-v2"
SECRET_RESOURCE = (
    "//secretmanager.googleapis.com/projects/ogilvy-trends-v2/secrets/{name}/versions/{version}"
)
SESSION_USER = "albert.meintjes@ogilvy.co.za"
DEPENDENCY_SHA = "c" * 64
SBOM_SHA = "d" * 64
BUILD_ID = "11111111-1111-4111-8111-111111111111"
BUILD_RESOURCE = f"projects/ogilvy-trends-v2/locations/us-central1/builds/{BUILD_ID}"
GENERATION_MODULE = "src.analysis.open_intelligence.execution_generations"
ORIGIN_REGISTRY_ANNOTATION = "42.ogilvy/origin-registry-sha256"
RESOURCE_MANIFEST_ANNOTATION = "42.ogilvy/resource-manifest-sha256"


@dataclass(frozen=True)
class FakeTrustedGeneration:
    origin_registry_sha256: str
    resource_manifest_sha256: str
    registry: object
    resource_manifest: object
    registry_path: Path
    resource_manifest_path: Path


@functools.lru_cache(maxsize=1)
def registry():
    return execution_origins.load_origin_registry(
        REGISTRY_PATH,
        expected_sha256=REGISTRY_SHA256,
        contract_root=ENGINE_ROOT,
    )


@functools.lru_cache(maxsize=1)
def amendment_e_registry():
    return execution_origins.load_origin_registry(
        AMENDMENT_E_REGISTRY_PATH,
        expected_sha256=AMENDMENT_E_REGISTRY_SHA256,
        contract_root=ENGINE_ROOT,
    )


def resource_manifest_payload(
    secrets=(("SOCIALCRAWL_OGILVY_API_KEY", "1"), ("ui-passcode-staging", "2")),
    extra=(),
):
    resources = [
        {
            "actions": ["read", "write"],
            "name": "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2_staging",
        },
        *(
            {"actions": ["read"], "name": SECRET_RESOURCE.format(name=name, version=version)}
            for name, version in secrets
        ),
        *extra,
    ]
    return {
        "bigquery_location": "US",
        "contract_version": "42_resource_manifest_v1",
        "origin_registry_sha256": REGISTRY_SHA256,
        "project": PROJECT,
        "project_number": "590353929363",
        "region": "us-central1",
        "resources": sorted(resources, key=lambda item: item["name"]),
    }


def generation(resource_manifest=None):
    payload = resource_manifest_payload() if resource_manifest is None else resource_manifest
    raw = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return FakeTrustedGeneration(
        origin_registry_sha256=REGISTRY_SHA256,
        resource_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        registry=registry(),
        resource_manifest=payload,
        registry_path=REGISTRY_PATH,
        resource_manifest_path=Path("resource_manifest.json"),
    )


def reviewed_generation():
    if not REVIEWED_RESOURCE_MANIFEST.is_file():
        pytest.skip("reviewed resource manifest bytes are not installed")
    raw = REVIEWED_RESOURCE_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == REVIEWED_RESOURCE_MANIFEST_SHA256
    return FakeTrustedGeneration(
        origin_registry_sha256=AMENDMENT_E_REGISTRY_SHA256,
        resource_manifest_sha256=REVIEWED_RESOURCE_MANIFEST_SHA256,
        registry=amendment_e_registry(),
        resource_manifest=json.loads(raw),
        registry_path=AMENDMENT_E_REGISTRY_PATH,
        resource_manifest_path=REVIEWED_RESOURCE_MANIFEST,
    )


def retained_contract(operation):
    rows = [
        origin
        for origin in registry().values()
        if origin.manifest_version == V1 and operation in origin.operation_bindings
    ]
    assert len(rows) == 1
    return rows[0].contract_sha256


def v1_manifest(operation="brain_read"):
    payload = valid_manifest(operation)
    payload["contract_sha256"] = retained_contract(operation)
    return payload


def canonical(payload, mode):
    return execution_approval.canonical_manifest_bytes(payload, mode=mode, registry=registry())


def write_manifest(tmp_path, payload, mode, name="manifest.json"):
    raw = canonical(payload, mode)
    path = tmp_path / name
    path.write_bytes(raw)
    return path.resolve(), hashlib.sha256(raw).hexdigest()


def v2_origin(operation):
    return execution_origins.resolve_origin(v2_manifest(operation), "new_approval", registry())


def v2_build_payload(origin, *, source_sha, image_digest):
    return {
        "name": BUILD_RESOURCE,
        "id": BUILD_ID,
        "projectId": PROJECT,
        "status": "SUCCESS",
        "results": {
            "images": [{"name": f"{origin.image_repository}:{source_sha}", "digest": image_digest}]
        },
        "finishTime": "2026-08-31T12:00:00.000000Z",
        "source": {
            "connectedRepository": {"repository": origin.connected_repo, "revision": source_sha}
        },
    }


def v2_desired(tmp_path, operation="wave1_pilot", gen=None):
    gen = generation() if gen is None else gen
    path, digest = write_manifest(tmp_path, v2_manifest(operation), "new_approval")
    manifest = deployer.load_operation_execution_manifest(path, digest, operation, generation=gen)
    origin = deployer.origin_for_manifest(manifest, generation=gen)
    desired = deployer.desired_operation_job_from_execution_manifest(
        manifest,
        operation=operation,
        execution_approval_sha256=digest,
        dependency_lock_sha256=DEPENDENCY_SHA,
        runtime_sbom_sha256=SBOM_SHA,
        origin=origin,
        generation=gen,
    )
    return desired, origin, gen, digest, manifest


def describe_payload(desired):
    payload = deepcopy(deployer.brain_job_resource(desired))
    payload["metadata"]["selfLink"] = (
        f"/apis/run.googleapis.com/v1/namespaces/{deployer.PROJECT_NUMBER}/jobs/{desired.job}"
    )
    payload["metadata"]["labels"] = {"cloud.googleapis.com/location": deployer.REGION}
    return payload


def _error_line(code):
    return json.dumps({"error": code}, separators=(",", ":"), sort_keys=True) + "\n"


def _run(code, *, stdin=b""):
    return subprocess.run(
        [sys.executable, "-c", code],
        input=stdin,
        capture_output=True,
        check=False,
        cwd=str(ENGINE_ROOT),
    )


# shared fixtures for the helpers themselves


def test_fake_generation_matches_the_packaged_registry_and_reviewed_bytes():
    gen = generation()
    assert gen.registry.sha256 == REGISTRY_SHA256 == gen.origin_registry_sha256
    assert gen.resource_manifest["origin_registry_sha256"] == REGISTRY_SHA256
    reviewed = reviewed_generation()
    assert reviewed.resource_manifest["origin_registry_sha256"] == AMENDMENT_E_REGISTRY_SHA256
    assert reviewed.registry.sha256 == AMENDMENT_E_REGISTRY_SHA256
    secrets = [
        item["name"]
        for item in reviewed.resource_manifest["resources"]
        if item["name"].startswith("//secretmanager.googleapis.com/")
    ]
    assert secrets == [
        SECRET_RESOURCE.format(name="SOCIALCRAWL_OGILVY_API_KEY", version="1"),
        SECRET_RESOURCE.format(name="ui-passcode-staging", version="2"),
    ]


def test_v1_helper_manifests_validate_under_historical_read_only():
    payload = v1_manifest("brain_read")
    admitted = execution_approval.validate_execution_manifest(
        payload, mode="historical_read", registry=registry()
    )
    assert admitted.manifest_version == V1
    with pytest.raises(execution_origins.OriginRefusal, match="execution_origin_mode_forbidden"):
        execution_approval.validate_execution_manifest(
            payload, mode="new_approval", registry=registry()
        )


# manifest builder


def test_builder_refuses_a_v2_manifest_under_historical_read():
    with pytest.raises(builder.ManifestBuilderRefusal, match=r"^execution_origin_mode_forbidden$"):
        builder._admit_manifest(
            v2_manifest("brain_read"), mode="historical_read", registry=registry()
        )


def test_builder_refuses_a_v1_manifest_under_new_approval():
    with pytest.raises(builder.ManifestBuilderRefusal, match=r"^execution_origin_mode_forbidden$"):
        builder._admit_manifest(v1_manifest("brain_read"), mode="new_approval", registry=registry())


def test_builder_admits_each_version_only_in_its_own_mode():
    manifest, raw, digest = builder._admit_manifest(
        v2_manifest("r3_release"), mode="new_approval", registry=registry()
    )
    assert manifest.manifest_version == V2
    assert hashlib.sha256(raw).hexdigest() == digest
    manifest, raw, digest = builder._admit_manifest(
        v1_manifest("brain_read"), mode="historical_read", registry=registry()
    )
    assert manifest.manifest_version == V1
    assert hashlib.sha256(raw).hexdigest() == digest


@pytest.mark.parametrize(
    ("factory", "mode"),
    [
        (lambda: v1_manifest("brain_read"), "historical_read"),
        (lambda: v2_manifest("brain_read"), "new_approval"),
    ],
)
def test_builder_cli_selects_the_mode_from_the_exact_version(
    factory, mode, tmp_path, capsys, monkeypatch
):
    seen = []
    real = builder.validate_execution_manifest

    def spy(payload, **kwargs):
        seen.append((kwargs["mode"], kwargs["registry"]))
        return real(payload, **kwargs)

    monkeypatch.setattr(builder, "validate_execution_manifest", spy)
    gen = generation()
    monkeypatch.setattr(builder, "_active_generation", lambda: gen)
    payload = factory()
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    request.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    assert (
        builder.main(
            ["--request-file", str(request.resolve()), "--output-file", str(output.resolve())]
        )
        == 0
    )
    expected = canonical(payload, mode)
    assert output.read_bytes() == expected
    assert seen
    assert {mode_seen for mode_seen, _ in seen} == {mode}
    assert all(registry_seen is gen.registry for _, registry_seen in seen)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {
        "contract_version": "open_intelligence_execution_manifest_build_v1",
        "manifest_file": str(output.resolve()),
        "manifest_sha256": hashlib.sha256(expected).hexdigest(),
        "operation": "brain_read",
    }


def test_builder_refuses_an_unknown_version_before_loading_a_generation(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(
        builder, "_active_generation", lambda: pytest.fail("generation loaded for a bad version")
    )
    payload = v2_manifest("brain_read")
    payload["manifest_version"] = "open_intelligence_execution_manifest_v3"
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        builder.main(
            ["--request-file", str(request.resolve()), "--output-file", str(output.resolve())]
        )
        == 1
    )
    assert capsys.readouterr().err == _error_line("execution_approval_manifest_invalid")
    assert not output.exists()


def test_builder_generation_seam_refuses_clearly_when_the_module_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, GENERATION_MODULE, None)
    with pytest.raises(builder.ManifestBuilderRefusal, match=r"^execution_generation_unavailable$"):
        builder._active_generation()


def test_builder_process_documents_its_two_flags_and_uses_the_injected_generation(tmp_path):
    payload = v2_manifest("migration_apply")
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    argv = ["--request-file", str(request.resolve()), "--output-file", str(output.resolve())]
    code = (
        "import tests.unit.test_operator_callers_v2 as t;"
        "import scripts.staging.build_open_intelligence_execution_manifest as m;"
        "m._active_generation=t.generation;"
        f"raise SystemExit(m.main({argv!r}))"
    )
    process = _run(code)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["operation"] == "migration_apply"
    assert output.read_bytes() == canonical(payload, "new_approval")
    grammar = subprocess.run(
        [sys.executable, str(Path(builder.__file__)), "--request-file", str(request.resolve())],
        capture_output=True,
        check=False,
        cwd=str(ENGINE_ROOT),
    )
    assert grammar.returncode == 2
    assert json.loads(grammar.stderr) == {"error": "execution_approval_cli_invalid"}


# package renderer


def _render(tmp_path, payload, mode, monkeypatch, gen=None):
    gen = generation() if gen is None else gen
    monkeypatch.setattr(renderer, "_active_generation", lambda: gen)
    path, digest = write_manifest(tmp_path, payload, mode)
    output = tmp_path / "job.yaml"
    code = renderer.main(
        [
            "--manifest-file",
            str(path),
            "--manifest-sha256",
            digest,
            "--output-file",
            str(output.resolve()),
        ]
    )
    return code, output, digest, gen


def test_renderer_v2_carries_the_generation_pair_and_exact_secret_versions(tmp_path, monkeypatch):
    code, output, digest, gen = _render(
        tmp_path, v2_manifest("wave1_pilot"), "new_approval", monkeypatch
    )
    assert code == 0
    resource = yaml.safe_load(output.read_text(encoding="utf-8"))
    annotations = resource["spec"]["template"]["metadata"]["annotations"]
    assert annotations == {
        "42.ogilvy/execution-approval-sha256": digest,
        "42.ogilvy/source-sha": "a" * 40,
        ORIGIN_REGISTRY_ANNOTATION: gen.origin_registry_sha256,
        RESOURCE_MANIFEST_ANNOTATION: gen.resource_manifest_sha256,
    }
    env = resource["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
    secret_refs = [item["valueFrom"]["secretKeyRef"] for item in env if "valueFrom" in item]
    assert secret_refs == [{"name": "SOCIALCRAWL_OGILVY_API_KEY", "key": "1"}]
    assert resource["metadata"]["name"] == "intelligence-42-funded-pilot-staging"


def test_renderer_v2_refuses_latest_ambiguous_prefix_or_unapproved_secret_versions(
    tmp_path, monkeypatch, capsys
):
    cases = {
        "latest": resource_manifest_payload(secrets=(("SOCIALCRAWL_OGILVY_API_KEY", "latest"),)),
        "ambiguous": resource_manifest_payload(
            secrets=(("SOCIALCRAWL_OGILVY_API_KEY", "1"), ("SOCIALCRAWL_OGILVY_API_KEY", "2"))
        ),
        "prefix": resource_manifest_payload(secrets=(("SOCIALCRAWL_OGILVY_API_KEY_OLD", "3"),)),
        "absent": resource_manifest_payload(secrets=()),
        "no_read": resource_manifest_payload(
            secrets=(),
            extra=(
                {
                    "actions": ["write"],
                    "name": SECRET_RESOURCE.format(name="SOCIALCRAWL_OGILVY_API_KEY", version="1"),
                },
            ),
        ),
    }
    for name, manifest in cases.items():
        (tmp_path / name).mkdir()
        code, output, _digest, _gen = _render(
            tmp_path / name,
            v2_manifest("wave1_pilot"),
            "new_approval",
            monkeypatch,
            gen=generation(manifest),
        )
        assert code == 1, name
        assert not output.exists(), name
        assert capsys.readouterr().err == _error_line("execution_approval_secret_invalid"), name


def test_renderer_keeps_v1_latest_bindings_and_bootstrap_annotations_v1_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_MANIFEST_GENERATION", "11")
    monkeypatch.setenv("OPEN_INTELLIGENCE_BOOTSTRAP_SIGNATURE_GENERATION", "12")
    code, output, _digest, _gen = _render(
        tmp_path, v1_manifest("bootstrap_migration_apply"), "historical_read", monkeypatch
    )
    assert code == 0
    resource = yaml.safe_load(output.read_text(encoding="utf-8"))
    annotations = resource["spec"]["template"]["metadata"]["annotations"]
    assert set(annotations) == {
        "42.ogilvy/execution-approval-sha256",
        "42.ogilvy/source-sha",
        "42.ogilvy/bootstrap-manifest-sha256",
        "42.ogilvy/bootstrap-manifest-generation",
        "42.ogilvy/bootstrap-signature-generation",
    }
    (tmp_path / "v2").mkdir()
    code, output, _digest, _gen = _render(
        tmp_path / "v2", v2_manifest("migration_apply"), "new_approval", monkeypatch
    )
    assert code == 0
    resource = yaml.safe_load(output.read_text(encoding="utf-8"))
    annotations = resource["spec"]["template"]["metadata"]["annotations"]
    assert set(annotations) == {
        "42.ogilvy/execution-approval-sha256",
        "42.ogilvy/source-sha",
        ORIGIN_REGISTRY_ANNOTATION,
        RESOURCE_MANIFEST_ANNOTATION,
    }
    (tmp_path / "wave1").mkdir()
    code, output, _digest, _gen = _render(
        tmp_path / "wave1", v1_manifest("wave1_pilot"), "historical_read", monkeypatch
    )
    assert code == 0
    env = yaml.safe_load(output.read_text(encoding="utf-8"))["spec"]["template"]["spec"][
        "template"
    ]["spec"]["containers"][0]["env"]
    assert [item["valueFrom"]["secretKeyRef"]["key"] for item in env if "valueFrom" in item] == [
        "latest"
    ]


def test_renderer_selects_the_mode_from_the_exact_version(tmp_path, monkeypatch):
    seen = []
    real = renderer.execution_approval.canonical_manifest_bytes

    def spy(payload, **kwargs):
        seen.append(kwargs["mode"])
        return real(payload, **kwargs)

    monkeypatch.setattr(renderer.execution_approval, "canonical_manifest_bytes", spy)
    code, _output, _digest, _gen = _render(
        tmp_path, v1_manifest("r3_release"), "historical_read", monkeypatch
    )
    assert code == 0
    assert seen
    assert set(seen) == {"historical_read"}
    seen.clear()
    (tmp_path / "v2").mkdir()
    code, _output, _digest, _gen = _render(
        tmp_path / "v2", v2_manifest("r3_release"), "new_approval", monkeypatch
    )
    assert code == 0
    assert seen
    assert set(seen) == {"new_approval"}


def test_renderer_process_documents_its_three_flags(tmp_path):
    payload = v2_manifest("r3_release")
    path, digest = write_manifest(tmp_path, payload, "new_approval")
    output = tmp_path / "job.yaml"
    argv = [
        "--manifest-file",
        str(path),
        "--manifest-sha256",
        digest,
        "--output-file",
        str(output.resolve()),
    ]
    code = (
        "import tests.unit.test_operator_callers_v2 as t;"
        "import scripts.staging.render_open_intelligence_execution_package as m;"
        "m._active_generation=t.generation;"
        f"raise SystemExit(m.main({argv!r}))"
    )
    process = _run(code)
    assert process.returncode == 0, process.stderr
    receipt = json.loads(process.stdout)
    assert receipt["contract_version"] == "open_intelligence_execution_package_v1"
    assert receipt["resource_digest"] == hashlib.sha256(output.read_bytes()).hexdigest()
    grammar = subprocess.run(
        [sys.executable, str(Path(renderer.__file__)), *argv[:4]],
        capture_output=True,
        check=False,
        cwd=str(ENGINE_ROOT),
    )
    assert grammar.returncode == 2


# approval CLI


class _Job:
    def __init__(self, rows):
        self._rows = rows

    def result(self, **_kwargs):
        return self._rows


class _Client:
    def __init__(self, rows, *, session_user=SESSION_USER, error=None):
        self.rows = rows
        self.session_user = session_user
        self.error = error
        self.calls = []

    def query(self, sql, job_config=None, **kwargs):
        self.calls.append((sql, job_config, kwargs))
        if sql == "SELECT SESSION_USER() AS session_user":
            return _Job([{"session_user": self.session_user}])
        if self.error is not None:
            raise self.error
        return _Job(self.rows)


def _v2_receipt_row(payload, digest, gen, approved_at):
    approval_id = execution_approval.approval_id_v2(
        digest,
        APPROVED_BY,
        approved_at,
        origin_registry_sha256=gen.origin_registry_sha256,
        resource_manifest_sha256=gen.resource_manifest_sha256,
    )
    return {
        "contract_version": "open_intelligence_execution_approval_receipt_v2",
        "approval_id": approval_id,
        "approved_at": approved_at,
        "approved_by": APPROVED_BY,
        "expires_at": payload["expires_at"],
        "manifest_sha256": digest,
        "operation": payload["operation"],
        "origin_registry_sha256": gen.origin_registry_sha256,
        "resource_manifest_sha256": gen.resource_manifest_sha256,
    }


def test_review_without_an_explicit_version_refuses_before_any_generation_or_credential(
    tmp_path,
):
    payload = v2_manifest("brain_read")
    del payload["manifest_version"]
    path = tmp_path / "manifest.json"
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    argv = [
        "review",
        "--manifest-file",
        str(path.resolve()),
        "--manifest-sha256",
        hashlib.sha256(raw).hexdigest(),
    ]
    code = (
        "import scripts.staging.approve_open_intelligence_execution as m;"
        "m._active_generation=lambda:(_ for _ in ()).throw(AssertionError('generation'));"
        "m._load_credentials=lambda:(_ for _ in ()).throw(AssertionError('credential'));"
        f"raise SystemExit(m.main({argv!r}))"
    )
    process = _run(code)
    assert process.returncode == 1
    assert process.stdout == b""
    assert json.loads(process.stderr) == {"error": "execution_approval_manifest_invalid"}


def test_old_disable_form_without_a_version_refuses_before_reading_stdin_or_native_io():
    code = (
        "import scripts.staging.approve_open_intelligence_execution as m;"
        "m._read_approval_phrase=lambda:(_ for _ in ()).throw(AssertionError('stdin'));"
        "m._load_credentials=lambda:(_ for _ in ()).throw(AssertionError('credential'));"
        "raise SystemExit(m.main(['disable','--approval-phrase-stdin']))"
    )
    process = _run(code, stdin=b"phrase\n")
    assert process.returncode == 2
    assert process.stdout == b""
    assert json.loads(process.stderr) == {"error": "execution_approval_manifest_invalid"}


def test_review_v1_is_historical_read_with_the_unchanged_receipt(tmp_path, monkeypatch):
    payload = v1_manifest("brain_read")
    path, digest = write_manifest(tmp_path, payload, "historical_read")
    gen = generation()
    monkeypatch.setattr(approval_cli, "_active_generation", lambda: gen)
    monkeypatch.setattr(
        approval_cli, "_load_credentials", lambda: pytest.fail("review crossed credentials")
    )
    seen = []
    real = approval_cli.execution_approval.validate_execution_manifest

    def spy(value, **kwargs):
        seen.append(kwargs["mode"])
        return real(value, **kwargs)

    monkeypatch.setattr(approval_cli.execution_approval, "validate_execution_manifest", spy)
    receipt = approval_cli.review_manifest(path, digest)
    assert receipt == {
        "contract_version": "open_intelligence_execution_review_v1",
        "manifest": payload,
        "manifest_sha256": digest,
        "operation": "brain_read",
    }
    assert set(seen) == {"historical_read"}


def test_review_v2_is_new_approval_and_appends_both_digests_then_origin_mode(tmp_path, monkeypatch):
    payload = v2_manifest("r3_apply")
    path, digest = write_manifest(tmp_path, payload, "new_approval")
    gen = generation()
    monkeypatch.setattr(approval_cli, "_active_generation", lambda: gen)
    monkeypatch.setattr(
        approval_cli, "_load_credentials", lambda: pytest.fail("review crossed credentials")
    )
    receipt = approval_cli.review_manifest(path, digest)
    assert list(receipt) == [
        "contract_version",
        "manifest",
        "manifest_sha256",
        "operation",
        "origin_registry_sha256",
        "resource_manifest_sha256",
        "origin_mode",
    ]
    assert receipt["contract_version"] == "open_intelligence_execution_review_v2"
    assert receipt["manifest"] == payload
    assert receipt["origin_registry_sha256"] == gen.origin_registry_sha256
    assert receipt["resource_manifest_sha256"] == gen.resource_manifest_sha256
    assert receipt["origin_mode"] == "new_approval"


def test_approve_refuses_v1_before_the_phrase_the_generation_or_any_native_io(
    tmp_path, monkeypatch
):
    path, digest = write_manifest(tmp_path, v1_manifest("brain_read"), "historical_read")
    monkeypatch.setattr(
        approval_cli, "_read_approval_phrase", lambda: pytest.fail("phrase read for v1")
    )
    monkeypatch.setattr(
        approval_cli, "_load_credentials", lambda: pytest.fail("credentials for v1")
    )
    monkeypatch.setattr(
        approval_cli, "_active_generation", lambda: pytest.fail("generation loaded for v1")
    )
    with pytest.raises(approval_cli.ApprovalCliRefusal, match=r"^execution_origin_mode_forbidden$"):
        approval_cli.approve_manifest(path, digest)


def test_approve_v2_derives_the_actor_from_session_user_before_the_v2_procedure(
    tmp_path, monkeypatch
):
    payload = v2_manifest("brain_read")
    path, digest = write_manifest(tmp_path, payload, "new_approval")
    gen = generation()
    approved_at = datetime(2026, 9, 13, 12, tzinfo=UTC)
    row = _v2_receipt_row(payload, digest, gen, approved_at)
    phrase = (
        f"I approve one 42 staging execution of brain_read for manifest SHA256 {digest}, "
        f"origin registry SHA256 {gen.origin_registry_sha256} and resource manifest SHA256 "
        f"{gen.resource_manifest_sha256}. Production remains unchanged."
    )
    client = _Client([row])
    monkeypatch.setattr(approval_cli, "_active_generation", lambda: gen)
    monkeypatch.setattr(approval_cli, "_read_approval_phrase", lambda: phrase)
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: object())
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: client)
    receipt = approval_cli.approve_manifest(path, digest)
    assert client.calls[0][0] == "SELECT SESSION_USER() AS session_user"
    assert len(client.calls) == 2
    sql, config, options = client.calls[1]
    assert sql.startswith(
        "CALL `ogilvy-trends-v2.trends_v2_staging_approvals."
        "sp_approve_open_intelligence_execution_v2`"
    )
    assert tuple(item.name for item in config.query_parameters) == (
        "canonical_manifest_json",
        "manifest_sha256",
        "approval_phrase",
        "origin_registry_sha256",
        "resource_manifest_sha256",
    )
    values = {item.name: item.value for item in config.query_parameters}
    assert values["canonical_manifest_json"] == canonical(payload, "new_approval").decode("utf-8")
    assert values["manifest_sha256"] == digest
    assert values["approval_phrase"] == phrase
    assert values["origin_registry_sha256"] == gen.origin_registry_sha256
    assert values["resource_manifest_sha256"] == gen.resource_manifest_sha256
    assert options == {"retry": None, "job_retry": None}
    assert receipt == {
        "contract_version": "open_intelligence_execution_approval_receipt_v2",
        "approval_id": row["approval_id"],
        "approved_at": "2026-09-13T12:00:00.000000Z",
        "approved_by": APPROVED_BY,
        "expires_at": payload["expires_at"],
        "manifest_sha256": digest,
        "operation": "brain_read",
        "origin_registry_sha256": gen.origin_registry_sha256,
        "resource_manifest_sha256": gen.resource_manifest_sha256,
        "origin_mode": "new_approval",
    }
    assert list(receipt)[-3:] == [
        "origin_registry_sha256",
        "resource_manifest_sha256",
        "origin_mode",
    ]


@pytest.mark.parametrize(
    "session_user",
    ["someone.else@ogilvy.co.za", "other.person@example.com", "", None],
)
def test_approve_v2_refuses_a_session_user_that_is_not_the_pinned_actor(
    tmp_path, monkeypatch, session_user
):
    payload = v2_manifest("brain_read")
    path, digest = write_manifest(tmp_path, payload, "new_approval")
    gen = generation()
    client = _Client(
        [_v2_receipt_row(payload, digest, gen, datetime(2026, 9, 13, 12, tzinfo=UTC))],
        session_user=session_user,
    )
    monkeypatch.setattr(approval_cli, "_active_generation", lambda: gen)
    monkeypatch.setattr(
        approval_cli,
        "_read_approval_phrase",
        lambda: "I approve one 42 staging execution of phrase",
    )
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: object())
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: client)
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match=r"^execution_approval_identity_invalid$"
    ):
        approval_cli.approve_manifest(path, digest)
    assert [call[0] for call in client.calls] == ["SELECT SESSION_USER() AS session_user"]


def test_approve_v2_refuses_a_receipt_carrying_another_generation(tmp_path, monkeypatch):
    payload = v2_manifest("brain_read")
    path, digest = write_manifest(tmp_path, payload, "new_approval")
    gen = generation()
    row = _v2_receipt_row(payload, digest, gen, datetime(2026, 9, 13, 12, tzinfo=UTC))
    row["resource_manifest_sha256"] = "0" * 64
    client = _Client([row])
    monkeypatch.setattr(approval_cli, "_active_generation", lambda: gen)
    monkeypatch.setattr(
        approval_cli,
        "_read_approval_phrase",
        lambda: "I approve one 42 staging execution of phrase",
    )
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: object())
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: client)
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match=r"^execution_approval_schema_mismatch$"
    ):
        approval_cli.approve_manifest(path, digest)


def test_actor_derivation_is_the_v1_routine_formula_byte_for_byte():
    assert approval_cli._derive_actor(SESSION_USER) == APPROVED_BY
    prefix = "open-intelligence-execution-approver-v1:"
    routine = approval_cli._routine_source("sp_approve_open_intelligence_execution_v1")
    assert f"CONCAT('usr_', LOWER(TO_HEX(SHA256(CONCAT('{prefix}', SESSION_USER())))))" in routine
    assert approval_cli._derive_actor(SESSION_USER.upper()) != APPROVED_BY


def test_routine_allowlist_covers_exactly_the_four_operator_procedures(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_cli, "_ROUTINE_DIR", tmp_path)
    for name in (
        "sp_approve_open_intelligence_execution_v1",
        "sp_approve_open_intelligence_execution_v2",
        "sp_disable_open_intelligence_execution_approval_v1",
        "sp_disable_open_intelligence_execution_approval_v2",
    ):
        (tmp_path / f"{name}.sql").write_text(name, encoding="utf-8")
        assert approval_cli._routine_source(name) == name
    with pytest.raises(approval_cli.ApprovalCliRefusal, match="target_invalid"):
        approval_cli._routine_source("sp_consume_open_intelligence_execution_v2")


@pytest.mark.parametrize(
    ("version", "routine", "receipt_version"),
    [
        (
            V1,
            "sp_disable_open_intelligence_execution_approval_v1",
            "open_intelligence_execution_disable_receipt_v1",
        ),
        (
            V2,
            "sp_disable_open_intelligence_execution_approval_v2",
            "open_intelligence_execution_disable_receipt_v2",
        ),
    ],
)
def test_disable_selects_the_exact_version_and_keeps_five_fields(
    monkeypatch, version, routine, receipt_version
):
    client = _Client(
        [
            {
                "contract_version": receipt_version,
                "state": "disabled",
                "disabled_at": datetime(2026, 9, 13, 12, tzinfo=UTC),
                "disabled_by": APPROVED_BY,
                "lock_version": 4,
            }
        ]
    )
    monkeypatch.setattr(
        approval_cli,
        "_read_approval_phrase",
        lambda: "I approve one 42 staging execution of phrase",
    )
    monkeypatch.setattr(approval_cli, "_load_credentials", lambda: object())
    monkeypatch.setattr(approval_cli, "_bigquery_client", lambda _credentials: client)
    monkeypatch.setattr(
        approval_cli, "_active_generation", lambda: pytest.fail("disable selected a generation")
    )
    receipt = approval_cli._disable_approvals(version)
    assert list(receipt) == [
        "contract_version",
        "state",
        "disabled_at",
        "disabled_by",
        "lock_version",
    ]
    assert receipt["contract_version"] == receipt_version
    assert client.calls[0][0].startswith(
        f"CALL `ogilvy-trends-v2.trends_v2_staging_approvals.{routine}`("
    )
    assert tuple(item.name for item in client.calls[0][1].query_parameters) == ("approval_phrase",)
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match=r"^execution_approval_schema_mismatch$"
    ):
        approval_cli._disable_approvals(V2 if version == V1 else V1)


@pytest.mark.parametrize(
    "argv",
    [
        ["disable", "--approval-phrase-stdin"],
        ["disable", "--manifest-version", V2],
        [
            "disable",
            "--manifest-version",
            "open_intelligence_execution_manifest_v3",
            "--approval-phrase-stdin",
        ],
        ["disable", "--approval-phrase-stdin", "--manifest-version", V2],
        ["disable", "--manifest-version", V2, "--approval-phrase-stdin", "--manifest-version", V2],
        ["review", "--manifest-file", "x", "--manifest-sha256", "a" * 64, "--registry-path", "y"],
        [
            "approve",
            "--manifest-file",
            "x",
            "--manifest-sha256",
            "a" * 64,
            "--approval-phrase-stdin",
            "--origin-registry-sha256",
            "b" * 64,
        ],
    ],
)
def test_cli_grammar_refuses_versionless_disable_and_every_override(argv):
    assert approval_cli.main(argv) == 2


def test_cli_process_accepts_the_documented_forms(tmp_path):
    payload = v2_manifest("brain_read")
    path, digest = write_manifest(tmp_path, payload, "new_approval")
    review = [
        "review",
        "--manifest-file",
        str(path),
        "--manifest-sha256",
        digest,
    ]
    code = (
        "import tests.unit.test_operator_callers_v2 as t;"
        "import scripts.staging.approve_open_intelligence_execution as m;"
        "m._active_generation=t.generation;"
        f"raise SystemExit(m.main({review!r}))"
    )
    process = _run(code)
    assert process.returncode == 0, process.stderr
    receipt = json.loads(process.stdout)
    assert receipt["contract_version"] == "open_intelligence_execution_review_v2"
    assert receipt["origin_mode"] == "new_approval"
    for version in (V1, V2):
        disable = ["disable", "--manifest-version", version, "--approval-phrase-stdin"]
        code = (
            "import scripts.staging.approve_open_intelligence_execution as m;"
            "m._load_credentials=lambda:(_ for _ in ()).throw("
            "m.ApprovalCliRefusal('execution_approval_identity_invalid'));"
            f"raise SystemExit(m.main({disable!r}))"
        )
        process = _run(code, stdin=b"phrase\n")
        assert process.returncode == 1
        assert json.loads(process.stderr) == {"error": "execution_approval_identity_invalid"}
    approve = [
        "approve",
        "--manifest-file",
        str(path),
        "--manifest-sha256",
        digest,
        "--approval-phrase-stdin",
    ]
    code = (
        "import tests.unit.test_operator_callers_v2 as t;"
        "import scripts.staging.approve_open_intelligence_execution as m;"
        "m._active_generation=t.generation;"
        "m._load_credentials=lambda:(_ for _ in ()).throw("
        "m.ApprovalCliRefusal('execution_approval_identity_invalid'));"
        f"raise SystemExit(m.main({approve!r}))"
    )
    process = _run(code, stdin=b"I approve one 42 staging execution of phrase\n")
    assert process.returncode == 1
    assert json.loads(process.stderr) == {"error": "execution_approval_identity_invalid"}


# deployment helper


def test_deploy_v2_job_carries_the_active_pair_and_exact_secret_versions(tmp_path):
    desired, _origin, gen, digest, manifest = v2_desired(tmp_path)
    assert desired.manifest_version == V2
    assert desired.job == "intelligence-42-funded-pilot-staging"
    assert (
        desired.service_account == "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    annotations = dict(desired.annotations)
    assert annotations == {
        "42.ogilvy/execution-approval-sha256": digest,
        "42.ogilvy/source-sha": manifest.source_sha,
        ORIGIN_REGISTRY_ANNOTATION: gen.origin_registry_sha256,
        RESOURCE_MANIFEST_ANNOTATION: gen.resource_manifest_sha256,
    }
    assert desired.secret_names == ("SOCIALCRAWL_OGILVY_API_KEY",)
    assert desired.secret_versions == (("SOCIALCRAWL_OGILVY_API_KEY", "1"),)
    env = deployer.brain_job_resource(desired)["spec"]["template"]["spec"]["template"]["spec"][
        "containers"
    ][0]["env"]
    assert [item["valueFrom"]["secretKeyRef"] for item in env if "valueFrom" in item] == [
        {"name": "SOCIALCRAWL_OGILVY_API_KEY", "key": "1"}
    ]
    assert deployer.operation_annotation_keys("wave1_pilot", manifest_version=V2) == (
        "42.ogilvy/execution-approval-sha256",
        "42.ogilvy/source-sha",
        ORIGIN_REGISTRY_ANNOTATION,
        RESOURCE_MANIFEST_ANNOTATION,
    )
    assert deployer.operation_annotation_keys("wave1_pilot") == (
        "42.ogilvy/execution-approval-sha256",
        "42.ogilvy/source-sha",
    )
    record = desired.to_record()
    assert record["manifest_version"] == V2
    assert record["secret_versions"] == {"SOCIALCRAWL_OGILVY_API_KEY": "1"}


def test_deploy_v2_brain_job_keeps_its_six_annotations_and_adds_the_pair(tmp_path):
    desired, _origin, gen, digest, _manifest = v2_desired(tmp_path, "brain_read")
    keys = tuple(key for key, _ in desired.annotations)
    assert keys == (
        *deployer.BRAIN_ANNOTATION_KEYS,
        ORIGIN_REGISTRY_ANNOTATION,
        RESOURCE_MANIFEST_ANNOTATION,
    )
    values = dict(desired.annotations)
    assert values["42.ogilvy/execution-approval-sha256"] == digest
    assert values[ORIGIN_REGISTRY_ANNOTATION] == gen.origin_registry_sha256
    assert values["42.ogilvy/deployment-manifest-sha256"] == deployer.canonical_digest(
        deployer.brain_deployment_manifest(desired)
    )
    assert desired.job == "intelligence-42-brain-staging"


@pytest.mark.parametrize(
    ("name", "resources"),
    [
        ("latest", (("SOCIALCRAWL_OGILVY_API_KEY", "latest"),)),
        ("ambiguous", (("SOCIALCRAWL_OGILVY_API_KEY", "1"), ("SOCIALCRAWL_OGILVY_API_KEY", "2"))),
        ("prefix", (("SOCIALCRAWL_OGILVY_API_KEY_OLD", "1"),)),
        ("suffix", (("OLD_SOCIALCRAWL_OGILVY_API_KEY", "1"),)),
        ("absent", ()),
        ("zero_padded", (("SOCIALCRAWL_OGILVY_API_KEY", "01"),)),
    ],
)
def test_deploy_v2_secret_resolution_refuses_every_inexact_binding(name, resources):
    manifest = resource_manifest_payload(secrets=resources)
    with pytest.raises(deployer.DeploymentError, match="secret"):
        deployer.resolve_secret_versions(
            ("SOCIALCRAWL_OGILVY_API_KEY",), resource_manifest=manifest
        )
    assert name


def test_deploy_v2_secret_resolution_requires_read_and_the_exact_project():
    foreign = SECRET_RESOURCE.format(name="SOCIALCRAWL_OGILVY_API_KEY", version="1").replace(
        "projects/ogilvy-trends-v2/", "projects/other-project/"
    )
    for manifest in (
        resource_manifest_payload(
            secrets=(),
            extra=(
                {
                    "actions": ["write"],
                    "name": SECRET_RESOURCE.format(name="SOCIALCRAWL_OGILVY_API_KEY", version="1"),
                },
            ),
        ),
        resource_manifest_payload(secrets=(), extra=({"actions": ["read"], "name": foreign},)),
    ):
        with pytest.raises(deployer.DeploymentError, match="secret"):
            deployer.resolve_secret_versions(
                ("SOCIALCRAWL_OGILVY_API_KEY",), resource_manifest=manifest
            )
    assert deployer.resolve_secret_versions((), resource_manifest=resource_manifest_payload()) == ()
    assert deployer.resolve_secret_versions(
        ("ui-passcode-staging", "SOCIALCRAWL_OGILVY_API_KEY"),
        resource_manifest=resource_manifest_payload(),
    ) == (("SOCIALCRAWL_OGILVY_API_KEY", "1"), ("ui-passcode-staging", "2"))
    with pytest.raises(deployer.DeploymentError, match="secret"):
        deployer.resolve_secret_versions(
            ("SOCIALCRAWL_OGILVY_API_KEY", "SOCIALCRAWL_OGILVY_API_KEY"),
            resource_manifest=resource_manifest_payload(),
        )


def test_deploy_v2_uses_the_reviewed_resource_manifest_bytes(tmp_path):
    gen = reviewed_generation()
    desired, _origin, _gen, _digest, _manifest = v2_desired(tmp_path, gen=gen)
    assert desired.secret_versions == (("SOCIALCRAWL_OGILVY_API_KEY", "1"),)
    assert (
        dict(desired.annotations)[RESOURCE_MANIFEST_ANNOTATION] == REVIEWED_RESOURCE_MANIFEST_SHA256
    )


def test_deploy_v2_readback_requires_exact_versions_and_the_pair(tmp_path):
    desired, origin, _gen, _digest, _manifest = v2_desired(tmp_path)
    snapshot = deployer.parse_brain_job_snapshot(describe_payload(desired), origin=origin)
    assert deployer.brain_snapshot_mismatches(snapshot, desired) == ()
    assert snapshot.manifest["secret_versions"] == (("SOCIALCRAWL_OGILVY_API_KEY", "1"),)
    assert snapshot.manifest["manifest_version"] == V2

    drifted = describe_payload(desired)
    container = drifted["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    container["env"][-1]["valueFrom"]["secretKeyRef"]["key"] = "latest"
    with pytest.raises(deployer.DeploymentError, match="secret"):
        deployer.parse_brain_job_snapshot(drifted, origin=origin)

    rotated = describe_payload(desired)
    rotated["spec"]["template"]["metadata"]["annotations"][RESOURCE_MANIFEST_ANNOTATION] = "0" * 64
    snapshot = deployer.parse_brain_job_snapshot(rotated, origin=origin)
    assert "resource_manifest_sha256" in deployer.brain_snapshot_mismatches(snapshot, desired)

    missing = describe_payload(desired)
    del missing["spec"]["template"]["metadata"]["annotations"][ORIGIN_REGISTRY_ANNOTATION]
    with pytest.raises(deployer.DeploymentError, match="annotation"):
        deployer.parse_brain_job_snapshot(missing, origin=origin)

    with pytest.raises(deployer.DeploymentError, match="execution matrix"):
        deployer.parse_brain_job_snapshot(describe_payload(desired))


def test_deploy_v2_build_authority_threads_one_origin_through_both_shared_calls(
    tmp_path, monkeypatch
):
    desired, origin, gen, _digest, manifest = v2_desired(tmp_path)
    seen = {}
    real_normalize = deployer.execution_approval.normalize_cloud_build_response
    real_factory = deployer.execution_approval.build_provenance_from_response

    def normalize(payload, **kwargs):
        seen.setdefault("normalize", []).append(kwargs)
        return real_normalize(payload, **kwargs)

    def factory(payload, contract, **kwargs):
        seen["factory"] = (contract, kwargs)
        return real_factory(payload, contract, **kwargs)

    monkeypatch.setattr(deployer.execution_approval, "normalize_cloud_build_response", normalize)
    monkeypatch.setattr(deployer.execution_approval, "build_provenance_from_response", factory)
    payload = v2_build_payload(
        origin, source_sha=manifest.source_sha, image_digest=desired.image.rsplit("@", 1)[1]
    )
    authority = deployer.validate_cloud_build_authority_v2(
        payload, build_resource=BUILD_RESOURCE, desired=desired, origin=origin, generation=gen
    )
    assert seen["factory"][0] == origin.contract_sha256
    assert seen["factory"][1] == {
        "manifest_version": V2,
        "mode": "new_approval",
        "registry": gen.registry,
    }
    assert all(call == {"origin": origin} for call in seen["normalize"])
    assert authority.resolved_source_kind == "resolvedConnectedRepository"
    assert authority.resolved_source_sha == manifest.source_sha
    assert authority.result_image_digest == desired.image.rsplit("@", 1)[1]
    identity = deployer.cloud_build_identity_payload(authority)
    assert identity["origin_registry_sha256"] == gen.origin_registry_sha256
    assert identity["resource_manifest_sha256"] == gen.resource_manifest_sha256
    assert identity["origin_mode"] == "new_approval"
    receipt = real_factory(
        payload,
        origin.contract_sha256,
        manifest_version=V2,
        mode="new_approval",
        registry=gen.registry,
    )
    expected = hashlib.sha256(
        deployer.execution_approval.canonical_build_provenance_bytes(receipt, origin=origin)
    ).hexdigest()
    assert identity["build_provenance_sha256"] == expected


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("legacy_only", lambda p, o: p.pop("source")),
        (
            "wrong_repository",
            lambda p, o: p["source"]["connectedRepository"].update(repository="projects/x/y"),
        ),
        (
            "source_differs",
            lambda p, o: p["source"]["connectedRepository"].update(revision="e" * 40),
        ),
        (
            "v1_image",
            lambda p, o: p["results"]["images"][0].update(
                name="us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine:"
                + "a" * 40
            ),
        ),
        (
            "digest_differs",
            lambda p, o: p["results"]["images"][0].update(digest="sha256:" + "f" * 64),
        ),
        ("not_success", lambda p, o: p.update(status="FAILURE")),
    ],
)
def test_deploy_v2_build_authority_refuses_every_named_attack(tmp_path, name, mutate):
    desired, origin, gen, _digest, manifest = v2_desired(tmp_path)
    payload = v2_build_payload(
        origin, source_sha=manifest.source_sha, image_digest=desired.image.rsplit("@", 1)[1]
    )
    mutate(payload, origin)
    with pytest.raises(deployer.DeploymentError):
        deployer.validate_cloud_build_authority_v2(
            payload, build_resource=BUILD_RESOURCE, desired=desired, origin=origin, generation=gen
        )
    assert name


def test_deploy_v1_build_inspection_takes_an_explicit_historical_origin(tmp_path, monkeypatch):
    origin = deployer.retained_v1_origin("brain_read", registry=registry())
    assert origin.manifest_version == V1
    assert "new_approval" not in origin.allowed_execution_modes
    brain = deployer.desired_brain_job(
        "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:" + "a" * 64,
        "b" * 40,
        DEPENDENCY_SHA,
        SBOM_SHA,
        arguments=(
            "--target",
            "staging",
            "--run-id",
            "run_1",
            "--signal-id",
            "sig_" + "e" * 64,
            "--research-depth",
            "briefing",
        ),
        execution_approval_sha256="f" * 64,
    )
    assert brain.manifest_version == V1
    payload = {
        "name": BUILD_RESOURCE,
        "id": BUILD_ID,
        "projectId": PROJECT,
        "status": "SUCCESS",
        "finishTime": "2026-08-30T10:00:00Z",
        "sourceProvenance": {"resolvedConnectedRepository": {"revision": "b" * 40}},
        "results": {
            "images": [
                {
                    "name": "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine:67ac000",
                    "digest": "sha256:" + "a" * 64,
                }
            ]
        },
    }
    seen = []
    real = deployer.execution_approval.normalize_cloud_build_response

    def normalize(value, **kwargs):
        seen.append(kwargs)
        return real(value, **kwargs)

    monkeypatch.setattr(deployer.execution_approval, "normalize_cloud_build_response", normalize)
    authority = deployer.validate_cloud_build_authority(
        payload, build_resource=BUILD_RESOURCE, desired=brain, origin=origin
    )
    assert authority.resolved_source_sha == "b" * 40
    assert seen == [{"origin": origin}]
    assert "origin_registry_sha256" not in deployer.cloud_build_identity_payload(authority)
    with pytest.raises(deployer.DeploymentError, match="origin"):
        deployer.validate_cloud_build_authority(
            payload, build_resource=BUILD_RESOURCE, desired=brain, origin=v2_origin("brain_read")
        )
    with pytest.raises(deployer.DeploymentError, match="origin"):
        deployer.retained_v1_origin("brain_read", registry=object())


def test_deploy_v1_apply_refuses_before_any_native_call(monkeypatch):
    brain = deployer.desired_brain_job(
        "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:" + "a" * 64,
        "b" * 40,
        DEPENDENCY_SHA,
        SBOM_SHA,
        arguments=("--target", "staging"),
        execution_approval_sha256="f" * 64,
    )
    monkeypatch.setattr(
        deployer, "_check_local_git", lambda *_args: pytest.fail("git reached for v1 apply")
    )
    with pytest.raises(deployer.DeploymentError, match="inspection"):
        deployer.apply_brain_job(
            brain,
            runner=lambda _command: pytest.fail("runner reached"),
            resolver=lambda _name: pytest.fail("resolver reached"),
            environment={"OI_BRAIN_CLOUD_BUILD_RESOURCE": BUILD_RESOURCE},
            build_reader=lambda _resource: pytest.fail("build reader reached"),
        )


def test_deploy_v2_apply_reads_one_build_then_reads_back_the_pair(tmp_path, monkeypatch):
    desired, origin, gen, _digest, manifest = v2_desired(tmp_path)
    lock_authority = type(
        "LockAuthority",
        (),
        {"dependency_lock_sha256": DEPENDENCY_SHA, "runtime_sbom_sha256": SBOM_SHA},
    )()
    payload = v2_build_payload(
        origin, source_sha=manifest.source_sha, image_digest=desired.image.rsplit("@", 1)[1]
    )
    events = []
    monkeypatch.setattr(
        deployer, "_check_local_git", lambda source_sha, runner: events.append(("git", source_sha))
    )
    monkeypatch.setattr(
        deployer,
        "derive_brain_dependency_authority",
        lambda **_kwargs: events.append(("digests",)) or lock_authority,
    )
    monkeypatch.setattr(
        deployer,
        "resolve_gcloud_executable",
        lambda **_kwargs: events.append(("gcloud",)) or "gcloud",
    )
    snapshot = deployer.parse_brain_job_snapshot(describe_payload(desired), origin=origin)
    monkeypatch.setattr(
        deployer,
        "_describe_brain",
        lambda *_args, **_kwargs: events.append(("job_read",)) or snapshot,
    )
    reads = []
    receipt = deployer.apply_brain_job(
        desired,
        runner=lambda _command: pytest.fail("mutation command reached"),
        resolver=lambda _name: "gcloud",
        environment={"OI_BRAIN_CLOUD_BUILD_RESOURCE": BUILD_RESOURCE},
        build_reader=lambda resource: reads.append(resource) or payload,
        origin=origin,
        generation=gen,
    )
    assert reads == [BUILD_RESOURCE]
    assert [event[0] for event in events] == ["git", "digests", "gcloud", "job_read"]
    assert receipt.receipt_contract_version == "brain_deployment_receipt_v1"
    assert receipt.build_identity["origin_registry_sha256"] == gen.origin_registry_sha256
    assert receipt.build_identity["resource_manifest_sha256"] == gen.resource_manifest_sha256
    assert receipt.build_identity["origin_mode"] == "new_approval"
    assert receipt.service_identity == desired.service_account
    with pytest.raises(deployer.DeploymentError, match="origin"):
        deployer.apply_brain_job(
            desired,
            runner=lambda _command: pytest.fail("runner reached"),
            resolver=lambda _name: "gcloud",
            environment={"OI_BRAIN_CLOUD_BUILD_RESOURCE": BUILD_RESOURCE},
            build_reader=lambda _resource: payload,
        )


def test_deploy_manifest_loader_selects_the_mode_from_the_exact_version(tmp_path, monkeypatch):
    seen = []
    real = deployer.execution_approval.validate_execution_manifest

    def spy(value, **kwargs):
        seen.append(kwargs["mode"])
        return real(value, **kwargs)

    monkeypatch.setattr(deployer.execution_approval, "validate_execution_manifest", spy)
    gen = generation()
    path, digest = write_manifest(tmp_path, v1_manifest("brain_read"), "historical_read", "v1.json")
    manifest = deployer.load_brain_execution_manifest(path, digest, generation=gen)
    assert manifest.manifest_version == V1
    assert seen
    assert set(seen) == {"historical_read"}
    seen.clear()
    path, digest = write_manifest(tmp_path, v2_manifest("brain_read"), "new_approval", "v2.json")
    manifest = deployer.load_brain_execution_manifest(path, digest, generation=gen)
    assert manifest.manifest_version == V2
    assert seen
    assert set(seen) == {"new_approval"}
    with pytest.raises(deployer.DeploymentError, match="operation is invalid"):
        deployer.load_operation_execution_manifest(path, digest, "r3_release", generation=gen)
    unknown = v2_manifest("brain_read")
    unknown["manifest_version"] = "open_intelligence_execution_manifest_v9"
    raw = json.dumps(unknown, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (tmp_path / "v9.json").write_bytes(raw)
    with pytest.raises(deployer.DeploymentError, match="manifest is invalid"):
        deployer.load_operation_execution_manifest(
            (tmp_path / "v9.json").resolve(),
            hashlib.sha256(raw).hexdigest(),
            "brain_read",
            generation=gen,
        )


def test_deploy_v2_route_refuses_a_v1_origin_a_bootstrap_manifest_and_a_missing_generation(
    tmp_path,
):
    _desired, origin, gen, digest, manifest = v2_desired(tmp_path)
    with pytest.raises(deployer.DeploymentError, match="origin"):
        deployer.desired_operation_job_from_execution_manifest(
            manifest,
            operation="wave1_pilot",
            execution_approval_sha256=digest,
            dependency_lock_sha256=DEPENDENCY_SHA,
            runtime_sbom_sha256=SBOM_SHA,
            origin=deployer.retained_v1_origin("wave1_pilot", registry=registry()),
            generation=gen,
        )
    with pytest.raises(deployer.DeploymentError, match="generation"):
        deployer.desired_operation_job_from_execution_manifest(
            manifest,
            operation="wave1_pilot",
            execution_approval_sha256=digest,
            dependency_lock_sha256=DEPENDENCY_SHA,
            runtime_sbom_sha256=SBOM_SHA,
            origin=origin,
        )
    with pytest.raises(deployer.DeploymentError, match="bootstrap"):
        deployer.desired_operation_job_from_execution_manifest(
            manifest,
            operation="wave1_pilot",
            execution_approval_sha256=digest,
            dependency_lock_sha256=DEPENDENCY_SHA,
            runtime_sbom_sha256=SBOM_SHA,
            origin=origin,
            generation=gen,
            bootstrap_generations=("1", "2"),
        )
    foreign = FakeTrustedGeneration(
        origin_registry_sha256="0" * 64,
        resource_manifest_sha256=gen.resource_manifest_sha256,
        registry=gen.registry,
        resource_manifest=gen.resource_manifest,
        registry_path=gen.registry_path,
        resource_manifest_path=gen.resource_manifest_path,
    )
    with pytest.raises(deployer.DeploymentError, match="generation"):
        deployer.desired_operation_job_from_execution_manifest(
            manifest,
            operation="wave1_pilot",
            execution_approval_sha256=digest,
            dependency_lock_sha256=DEPENDENCY_SHA,
            runtime_sbom_sha256=SBOM_SHA,
            origin=origin,
            generation=foreign,
        )


def test_deploy_generation_seam_refuses_clearly_when_the_module_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, GENERATION_MODULE, None)
    with pytest.raises(deployer.DeploymentError, match="generation"):
        deployer._active_generation()
    monkeypatch.setitem(sys.modules, GENERATION_MODULE, None)
    with pytest.raises(
        renderer.ExecutionPackageRefusal, match=r"^execution_generation_unavailable$"
    ):
        renderer._active_generation()
    with pytest.raises(
        approval_cli.ApprovalCliRefusal, match=r"^execution_generation_unavailable$"
    ):
        approval_cli._active_generation()


def test_deploy_process_turns_a_refusing_or_unavailable_generation_into_the_error_line(
    tmp_path,
):
    path, digest = write_manifest(tmp_path, v2_manifest("r3_release"), "new_approval")
    script = str(Path(deployer.__file__))
    argv = [
        "--operation",
        "r3_release",
        "--execution-manifest-file",
        str(path),
        "--execution-manifest-sha256",
        digest,
        "--dependency-lock-sha256",
        DEPENDENCY_SHA,
        "--runtime-sbom-sha256",
        SBOM_SHA,
    ]
    module = "src.analysis.open_intelligence.execution_generations"
    # The authority adapter imports the generation module at load time and names
    # TrustedGeneration in its signatures, so the refusing double keeps the real module
    # shape and only replaces active_generation with a real refusal class and code.
    refusing = (
        "import runpy, sys;"
        f"import {module} as g;"
        "from src.analysis.open_intelligence.execution_origins import OriginRefusal;"
        "g.active_generation=lambda: (_ for _ in ()).throw("
        "OriginRefusal('execution_generation_digest_mismatch'));"
        f"sys.argv=[{script!r}, *{argv!r}];"
        f"runpy.run_path({script!r}, run_name='__main__')"
    )
    # The absent branch removes the module only after the authority adapter has loaded,
    # so the script's own import succeeds and the seam's ImportError path is what runs.
    unavailable = (
        "import runpy, sys;"
        "import src.analysis.open_intelligence.execution_approval;"
        f"sys.modules[{module!r}]=None;"
        f"sys.argv=[{script!r}, *{argv!r}];"
        f"runpy.run_path({script!r}, run_name='__main__')"
    )
    for code, line in (
        (refusing, b"error: execution_generation_digest_mismatch"),
        (unavailable, b"error: trusted generation module is unavailable"),
    ):
        process = _run(code)
        assert process.returncode == 1
        assert process.stdout == b""
        assert process.stderr.strip() == line
        assert b"Traceback" not in process.stderr


def test_deploy_process_documents_the_operation_flags_and_dry_runs_a_v2_job(tmp_path):
    path, digest = write_manifest(tmp_path, v2_manifest("r3_release"), "new_approval")
    argv = [
        "--operation",
        "r3_release",
        "--execution-manifest-file",
        str(path),
        "--execution-manifest-sha256",
        digest,
        "--dependency-lock-sha256",
        DEPENDENCY_SHA,
        "--runtime-sbom-sha256",
        SBOM_SHA,
    ]
    code = (
        "import tests.unit.test_operator_callers_v2 as t;"
        "import scripts.staging.deploy_open_intelligence_job as d;"
        "d._active_generation=t.generation;"
        f"raise SystemExit(d.main({argv!r}))"
    )
    process = _run(code)
    assert process.returncode == 0, process.stderr
    rendered = json.loads(process.stdout)
    assert rendered["mode"] == "dry-run"
    assert rendered["desired_snapshot"]["job"] == "intelligence-42-daily-staging"
    assert rendered["desired_snapshot"]["manifest_version"] == V2
    annotations = rendered["desired_resource"]["spec"]["template"]["metadata"]["annotations"]
    assert annotations[ORIGIN_REGISTRY_ANNOTATION] == REGISTRY_SHA256
    assert annotations[RESOURCE_MANIFEST_ANNOTATION] == generation().resource_manifest_sha256
    grammar = subprocess.run(
        [sys.executable, str(Path(deployer.__file__)), "--operation", "r3_release", *argv[2:4]],
        capture_output=True,
        check=False,
        cwd=str(ENGINE_ROOT),
    )
    assert grammar.returncode == 1
    assert b"CLI is invalid" in grammar.stderr
    apply_code = (
        "import tests.unit.test_operator_callers_v2 as t;"
        "import scripts.staging.deploy_open_intelligence_job as d;"
        "d._active_generation=t.generation;"
        "d._check_local_git=lambda *a, **k: (_ for _ in ()).throw(d.DeploymentError('git gate'));"
        f"raise SystemExit(d.main({[*argv, '--apply']!r}))"
    )
    process = _run(apply_code)
    assert process.returncode == 1
    assert b"git gate" in process.stderr
