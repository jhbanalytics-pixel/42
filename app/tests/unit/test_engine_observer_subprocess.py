import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest
from src.api.question_worker_bundle import BundleVerificationError, verify_bundle
from src.api.question_worker_process import invoke_engine

ENGINE_ROOT = Path("/app/engine")
ENGINE_PYTHON = Path("/opt/42-engine-venv/bin/python")
REQUEST_ID = "00000000-0000-0000-0000-000000000001"
RUNTIME_BUILD = Path("/app/runtime-build.json")
GATE_MANIFEST = Path("/opt/42-gates/linux-boundary/manifest.json")


def _runtime_binding():
    stamp = json.loads(RUNTIME_BUILD.read_bytes())
    gate = json.loads(GATE_MANIFEST.read_bytes())
    bundle = json.loads((ENGINE_ROOT / "bundle-manifest.json").read_bytes())
    assert set(stamp) == {
        "contract_version",
        "engine_bundle_digest",
        "lp_commit",
    }
    assert stamp["contract_version"] == "general_question_runtime_build_v1"
    assert (
        stamp["engine_bundle_digest"] == gate["engine_bundle"]["bundle_manifest_sha256"]
    )
    assert len(bundle["files"]) == gate["engine_bundle"]["file_count"]
    assert (
        hashlib.sha256(
            json.dumps(
                bundle["files"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        == gate["engine_bundle"]["files_map_sha256"]
    )
    entrypoint = ENGINE_ROOT / "scripts/staging/run_general_question_worker.py"
    assert (
        hashlib.sha256(entrypoint.read_bytes()).hexdigest()
        == gate["engine_bundle"]["entrypoint_sha256"]
    )
    return stamp


def _verify_runtime(root=ENGINE_ROOT):
    stamp = _runtime_binding()
    return verify_bundle(
        root,
        RUNTIME_BUILD,
        expected_lp_commit=stamp["lp_commit"],
        expected_bundle_digest=stamp["engine_bundle_digest"],
    )


def test_linux_gate_recipe_binds_current_bundle_and_test_environments():
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads(
        (root / "ops/tests/fixtures/linux_boundary/manifest.json").read_bytes()
    )
    docker = (root / "ops/tests/Dockerfile.linux-gates").read_text(encoding="utf-8")

    assert manifest["runtime"]["app_image_binding"] == "external_immutable_image"
    assert not {"app_image_tag", "app_image_id"} & set(manifest["runtime"])
    assert (
        "APP_RUNTIME_IMAGE=<approved-immutable-image>" in manifest["commands"]["build"]
    )
    assert "prepared_not_executed" not in manifest
    assert manifest["execution_evidence"]["storage"] == "external_receipt"
    assert {
        "input_manifest_sha256",
        "parent_image_id",
        "derived_image_id",
        "commands",
    } <= set(manifest["execution_evidence"]["required_fields"])

    spec = importlib.util.spec_from_file_location(
        "pin_linux_gate_manifest", root / "ops/build/pin_linux_gate_manifest.py"
    )
    pin_tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pin_tool)
    assert manifest["engine_bundle"] == {
        **pin_tool.compute_engine_bundle(root),
        "entrypoint": "engine/scripts/staging/run_general_question_worker.py",
    }
    native_nodes = [
        "tests/unit/test_engine_observer_subprocess.py::test_app_invokes_real_engine_observer_through_sanitized_store_adapter",
        "tests/unit/test_engine_observer_subprocess.py::test_changed_bundle_prevents_child_launch",
    ]
    assert manifest["combined_node_ids"] == native_nodes
    assert manifest["prebuild_node_id"] == (
        "tests/unit/test_engine_observer_subprocess.py::"
        "test_linux_gate_recipe_binds_current_bundle_and_test_environments"
    )
    assert manifest["prebuild_node_id"] in manifest["commands"]["run_prebuild"]
    command = manifest["commands"]["run_combined"]
    assert all(node in command for node in native_nodes)
    assert manifest["prebuild_node_id"] not in command
    assert command.count("tests/unit/test_engine_observer_subprocess.py::") == 2
    assert manifest["combined_image_static_paths"] == [
        "/app/engine/bundle-manifest.json",
        "/app/engine/requirements.lock",
        "/app/engine/scripts/staging/run_general_question_worker.py",
        "/app/runtime-build.json",
        "/opt/42-app-test-venv/bin/python",
        "/opt/42-engine-test-venv/bin/python",
        "/opt/42-engine-venv/bin/python",
        "/opt/42-gates/app/src/api/fieldwork.py",
        "/opt/42-gates/app/tests/unit/test_engine_observer_subprocess.py",
        "/opt/42-gates/linux-boundary/manifest.json",
        "/opt/42-gates/linux-boundary/observer_store.json",
    ]
    for path in manifest["combined_image_static_paths"]:
        if path.startswith("/opt/42-gates/"):
            assert path in docker
    assert manifest["test_dependency_closure"] == {
        "app/requirements-dev.lock": "8832214c7436bde9f0f826fd27f459fae9464da0a1290280374c7377fa7fb626",
        "engine/requirements-dev.lock": "e2afbc89d94de508377e638fe60380aa09d43e9631a2448bb00b5417bfc47e93",
    }
    for name, expected in manifest["test_dependency_closure"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    for name, expected in manifest["app_test_closure"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    assert len(manifest["engine_test_closure"]) == 14
    for name, expected in manifest["engine_test_closure"].items():
        source = root / "engine" / name
        destination = "/opt/42-gates/engine/" + name
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
        assert destination in docker
    assert len(manifest["engine_observer_node_ids"]) == 20
    assert len(set(manifest["engine_observer_node_ids"])) == 20
    engine_command = manifest["commands"]["run_engine_observer"]
    assert "--workdir /app/engine" in engine_command
    assert "PYTHONPATH=/opt/42-gates/engine:/app/engine" in engine_command
    assert "GENERAL_QUESTION_ENGINE_TEST_ROOT=/app/engine" in engine_command
    assert all(
        node.replace("tests/", "/opt/42-gates/engine/tests/", 1) in engine_command
        for node in manifest["engine_observer_node_ids"]
    )
    fixture_root = root / "ops/tests/fixtures/linux_boundary"
    for name, expected in manifest["adapter_hashes"].items():
        assert (
            hashlib.sha256((fixture_root / name).read_bytes()).hexdigest() == expected
        )
    assert len(manifest["linux_node_ids"]) == 35
    assert len(set(manifest["linux_node_ids"])) == 35
    assert "<linux_node_ids" not in manifest["commands"]["run_35"]
    assert all(
        node_id in manifest["commands"]["run_35"]
        for node_id in manifest["linux_node_ids"]
    )
    assert "/opt/42-app-test-venv/bin/python" in docker
    assert "/opt/42-engine-test-venv/bin/python" in docker
    assert "pip install --require-hashes" in docker
    assert "USER 10001:10001" in docker
    assert docker.index("find_spec('observer_host') is None") < docker.index(
        "COPY ops/tests/fixtures/linux_boundary/observer_host.py"
    )
    for name in ("run_35", "run_combined", "run_engine_observer"):
        command = manifest["commands"][name]
        assert "--network=none" in command
        assert "--user 10001:10001" in command
        assert "--security-opt no-new-privileges" in command

    assert manifest["pdf_runtime_closure"] == {
        "app/configs/pdf_runtime.json": "f2d8221e31bdb5f08b52b70d2aac763e0922dad53259cffc859f4a0db80ec30a",
        "app/src/api/pdf_exporter.py": "19b5d9786fc6e27f1abaa72dbb4390aa5366030b787acd619660bb15f5c6b894",
        "app/src/api/pdf_renderer_worker.py": "ce5f5640ff1d0c1471b66d5fc86bafa92e5330c04d9454f81ccb13b13b71f648",
    }
    assert manifest["pdf_test_closure"] == {
        "app/tests/fixtures/pdf/newsreader-chromium151.pdf": "9c665218a6a7f3d4aff65d47a1aa593120ffb3dece3d8912b51c632d21fbd577",
        "app/tests/unit/test_pdf_renderer_linux.py": "fb532bc0b95f8decc53f741064c139cf84048a9f18c2a78a05b46b4105ef9629",
    }
    for name, expected in {
        **manifest["pdf_runtime_closure"],
        **manifest["pdf_test_closure"],
    }.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    assert "COPY app/src/api/pdf_exporter.py" not in docker
    assert "COPY app/src/api/pdf_renderer_worker.py" not in docker
    assert (
        "COPY app/tests/unit/test_pdf_renderer_linux.py "
        "/opt/42-gates/pdf/tests/unit/test_pdf_renderer_linux.py"
    ) in docker
    assert (
        "COPY app/tests/fixtures/pdf/newsreader-chromium151.pdf "
        "/opt/42-gates/pdf/tests/fixtures/pdf/newsreader-chromium151.pdf"
    ) in docker
    assert (
        "Path(pdf_exporter.__file__).resolve() == Path('/app/src/api/pdf_exporter.py')"
        in docker
    )
    assert (
        "Path(pdf_renderer_worker.__file__).resolve() == Path('/app/src/api/pdf_renderer_worker.py')"
        in docker
    )
    assert len(manifest["pdf_native_node_ids"]) == 4
    assert len(set(manifest["pdf_native_node_ids"])) == 4
    assert manifest["pdf_security_profile"] == {
        "host_path": "<reviewed-seccomp-profile-host-path>",
        "sha256": "e53c15d248f57bceec0a3a16fad1b4ab0d05a7bddb455f86a60e2dba5612cdf7",
    }
    for name, mode in (("run_pdf_no_init", "no-init"), ("run_pdf_init", "init")):
        command = manifest["commands"][name]
        assert "<immutable-derived-image>" in command
        assert "--workdir /app" in command
        assert "PDF_RENDERER_NATIVE=1" in command
        assert f"PDF_RENDERER_PID1_MODE={mode}" in command
        assert "--network=none" in command
        assert "--read-only" in command
        assert "--tmpfs /tmp:rw,size=268435456" in command
        assert "--cap-drop ALL" in command
        assert "--security-opt no-new-privileges" in command
        assert "--security-opt seccomp=<reviewed-seccomp-profile-host-path>" in command
        assert "--user 10001:10001" in command
        assert all(
            "/opt/42-gates/pdf/" + node in command
            for node in manifest["pdf_native_node_ids"]
        )
    assert "--init" not in manifest["commands"]["run_pdf_no_init"]
    assert "--init" in manifest["commands"]["run_pdf_init"]


@pytest.mark.skipif(os.name != "posix", reason="requires the Linux runtime boundary")
def test_app_invokes_real_engine_observer_through_sanitized_store_adapter():
    fixture_path = Path(
        os.environ.get(
            "R02_OBSERVER_STORE_PATH",
            "/opt/42-gates/linux-boundary/observer_store.json",
        )
    )
    assert ENGINE_ROOT.is_dir()
    assert ENGINE_PYTHON.is_file()
    assert fixture_path.is_file()
    assert not Path("/workspace/source").exists()
    assert not Path("/workspace/ops").exists()
    assert os.getuid() == 10001
    assert os.getgid() == 10001
    status = Path("/proc/self/status").read_text(encoding="utf-8")
    assert "NoNewPrivs:\t1" in status
    assert os.access(ENGINE_PYTHON, os.X_OK)
    assert os.access(Path("/opt/42-app-test-venv/bin/python"), os.X_OK)
    assert os.access(Path("/opt/42-engine-test-venv/bin/python"), os.X_OK)
    with pytest.raises(PermissionError):
        (ENGINE_ROOT / "requirements.lock").open("ab")
    with pytest.raises(PermissionError):
        RUNTIME_BUILD.open("ab")

    payload = {
        "contract_version": "general_question_observe_request_v1",
        "scope": {
            "client_scope_id": "ogilvy_default",
            "market_scope": ["ke", "ng", "za"],
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        "request_id": REQUEST_ID,
    }
    result = asyncio.run(
        invoke_engine(
            ENGINE_ROOT,
            ENGINE_PYTHON,
            "observe",
            payload,
            deadline=time.monotonic() + 15,
            verify_runtime=_verify_runtime,
            environment={
                "PATH": "/opt/42-engine-venv/bin:/usr/local/bin:/usr/bin:/bin",
                "R02_OBSERVER_STORE_PATH": str(fixture_path),
            },
        )
    )

    assert result.stderr_observed_bytes == 0
    assert result.reply["contract_version"] == "general_question_observe_reply_v1"
    assert result.reply["mode"] == "detail"
    assert result.reply["observed_state"] == "unconfirmed"
    assert result.reply["invocation"]["request_id"] == REQUEST_ID
    assert result.reply["result_pointer"] is None
    assert result.reply["missing_work"] == ["execution_unconfirmed"]


@pytest.mark.skipif(os.name != "posix", reason="requires the Linux runtime boundary")
def test_changed_bundle_prevents_child_launch(tmp_path):
    copied = tmp_path / "engine"
    shutil.copytree(ENGINE_ROOT, copied)
    changed = copied / "requirements.lock"
    changed.chmod(stat.S_IRUSR | stat.S_IWUSR)
    changed.write_bytes(changed.read_bytes() + b"changed")
    marker = tmp_path / "launched"
    interpreter = tmp_path / "engine-python"
    interpreter.write_text(
        f"#!/bin/sh\nprintf launched > '{marker}'\nexec '{ENGINE_PYTHON}' \"$@\"\n",
        encoding="utf-8",
    )
    interpreter.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    payload = {
        "contract_version": "general_question_observe_request_v1",
        "scope": {
            "client_scope_id": "ogilvy_default",
            "market_scope": ["ke", "ng", "za"],
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        "request_id": REQUEST_ID,
    }

    with pytest.raises(BundleVerificationError, match="bundle_file_mismatch"):
        asyncio.run(
            invoke_engine(
                copied,
                interpreter,
                "observe",
                payload,
                deadline=time.monotonic() + 15,
                verify_runtime=lambda: _verify_runtime(copied),
            )
        )
    assert not marker.exists()
