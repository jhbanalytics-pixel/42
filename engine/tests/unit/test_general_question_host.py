import hashlib
import importlib
import io
import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import requests
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_control import QuestionStoreError
from urllib3.response import HTTPResponse


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_host")


def bundle(tmp_path):
    root = tmp_path / "engine"
    files = {
        "src/entry.py": b"VALUE = 1\n",
        "configs/limits.json": b"{}\n",
        "infra/schema.sql": b"SELECT 1\n",
        "scripts/run.py": b"pass\n",
        "requirements.lock": b"synthetic==1\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "contract_version": "general_question_engine_bundle_v1",
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    (root / "bundle-manifest.json").write_text(canonical_bytes(manifest).decode(), encoding="utf-8")
    return root, manifest


def test_actual_bundle_bytes_and_canonical_manifest_are_measured(tmp_path):
    root, manifest = bundle(tmp_path)
    result = module().verify_question_engine_bundle(engine_root=root)
    assert result["engine_bundle_digest"] == canonical_digest(manifest)
    assert result["verified_file_count"] == 5
    assert result["manifest"] == manifest
    assert "credentials" not in result
    assert "store" not in result


@pytest.mark.parametrize(
    "mutation",
    [
        "tamper",
        "missing",
        "unlisted",
        "pyc",
        "symlink",
        "case_collision",
        "parent",
        "absolute",
        "backslash",
        "credential",
        "self_digest",
        "duplicate_json_key",
    ],
)
def test_invalid_runtime_tree_refuses(tmp_path, mutation):
    root, manifest = bundle(tmp_path)
    if mutation == "tamper":
        (root / "src/entry.py").write_bytes(b"VALUE = 2\n")
    elif mutation == "missing":
        (root / "src/entry.py").unlink()
    elif mutation == "unlisted":
        (root / "src/extra.py").write_bytes(b"pass\n")
    elif mutation == "pyc":
        (root / "src/entry.pyc").write_bytes(b"not executable")
    elif mutation == "symlink":
        (root / "src/entry.py").unlink()
        try:
            (root / "src/entry.py").symlink_to(root / "scripts/run.py")
        except OSError:
            pytest.skip("host does not permit synthetic symlink creation")
    elif mutation == "case_collision":
        manifest["files"]["SRC/entry.py"] = manifest["files"]["src/entry.py"]
    elif mutation in ("parent", "absolute", "backslash", "credential"):
        name = {
            "parent": "../outside.py",
            "absolute": "/tmp/outside.py",
            "backslash": "src\\entry.py",
            "credential": "configs/credentials.json",
        }[mutation]
        manifest["files"][name] = "a" * 64
    elif mutation == "self_digest":
        manifest["digest"] = "a" * 64
    else:
        raw = json.dumps(manifest)
        (root / "bundle-manifest.json").write_text('{"files":{},' + raw[1:], encoding="utf-8")
    if mutation != "duplicate_json_key":
        (root / "bundle-manifest.json").write_text(
            canonical_bytes(manifest).decode(), encoding="utf-8"
        )
    with pytest.raises(ValueError, match="runtime_build_invalid"):
        module().verify_question_engine_bundle(engine_root=root)


def test_listed_root_dependency_file_is_hashed(tmp_path):
    root, manifest = bundle(tmp_path)
    path = root / "pyproject.toml"
    path.write_bytes(b"synthetic==1\n")
    manifest["files"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "bundle-manifest.json").write_text(canonical_bytes(manifest).decode(), encoding="utf-8")
    assert module().verify_question_engine_bundle(engine_root=root)["verified_file_count"] == 6


def test_credential_json_refuses_even_under_innocent_filename(tmp_path):
    root, manifest = bundle(tmp_path)
    raw = canonical_bytes({"type": "service_account", "private_key": "synthetic_not_a_key"})
    (root / "configs/limits.json").write_bytes(raw)
    manifest["files"]["configs/limits.json"] = hashlib.sha256(raw).hexdigest()
    (root / "bundle-manifest.json").write_bytes(canonical_bytes(manifest))
    with pytest.raises(ValueError, match="runtime_build_invalid"):
        module().verify_question_engine_bundle(engine_root=root)


@pytest.mark.parametrize(
    "mutation", [None, "wrong_digest", "wrong_commit", "extra_field", "missing_lock"]
)
def test_confirmed_parent_build_stamp_binds_measured_engine(tmp_path, mutation):
    from importlib.metadata import version

    root, manifest = bundle(tmp_path)
    stamp = {
        "contract_version": "general_question_runtime_build_v1",
        "lp_commit": "a" * 40,
        "engine_bundle_digest": canonical_digest(manifest),
    }
    if mutation == "wrong_digest":
        stamp["engine_bundle_digest"] = "b" * 64
    elif mutation == "wrong_commit":
        stamp["lp_commit"] = "unknown"
    elif mutation == "extra_field":
        stamp["image_digest"] = "b" * 64
    elif mutation == "missing_lock":
        del manifest["files"]["requirements.lock"]
        (root / "bundle-manifest.json").write_bytes(canonical_bytes(manifest))
    (tmp_path / "runtime-build.json").write_bytes(canonical_bytes(stamp))
    if mutation:
        with pytest.raises(ValueError, match="runtime_build_invalid"):
            module().verify_question_runtime_build(engine_root=root)
    else:
        assert module().verify_question_runtime_build(engine_root=root) == {
            "lp_commit": "a" * 40,
            "engine_bundle_digest": canonical_digest(manifest),
            "sdk_version": version("google-genai"),
        }


def host_fixture(tmp_path, monkeypatch, mode=None):
    from importlib.metadata import version

    from tests.unit import test_general_question_store as fixture

    root, manifest = bundle(tmp_path)
    stamp = {
        "contract_version": "general_question_runtime_build_v1",
        "lp_commit": "a" * 40,
        "engine_bundle_digest": canonical_digest(manifest),
    }
    (tmp_path / "runtime-build.json").write_bytes(canonical_bytes(stamp))
    policy = fixture.prepared()[0]
    identity = {
        "service_name": "listening-post-staging",
        "revision_name": "listening-post-staging-test",
        "service_account_email": "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "lp_commit": stamp["lp_commit"],
        "engine_bundle_digest": stamp["engine_bundle_digest"],
        "canonical_service_audience": "https://listening-post-staging-fibxg5ynpq-uc.a.run.app",
        "sdk_version": version("google-genai"),
    }
    binding = {
        "contract_version": "general_question_deployment_v1",
        **identity,
        "project": "ogilvy-trends-v2",
        "region": "us-central1",
        "image_digest": "c" * 64,
        "worker_path": "/internal/general-question/execute",
        "policy_digest": policy["policy_digest"],
    }
    if mode == "wrong_revision":
        binding["revision_name"] = "listening-post-staging-other"
    binding["deployment_digest"] = canonical_digest(binding)
    objects = {
        f"deployments/{binding['deployment_digest']}/binding.json": canonical_bytes(binding),
        f"policies/{policy['policy_digest']}/policy.json": canonical_bytes(policy),
    }
    if mode == "policy_tamper":
        objects[f"policies/{policy['policy_digest']}/policy.json"] = canonical_bytes(
            {**policy, "policy_digest": "b" * 64}
        )
    if mode == "binding_tamper":
        objects[f"deployments/{binding['deployment_digest']}/binding.json"] = canonical_bytes(
            {**binding, "lp_commit": "b" * 40}
        )
    monkeypatch.setenv("K_SERVICE", identity["service_name"])
    monkeypatch.setenv("GCP_PROJECT", "ogilvy-trends-v2")
    monkeypatch.setenv("K_REVISION", identity["revision_name"])
    monkeypatch.setenv("GENERAL_QUESTION_DEPLOYMENT_DIGEST", binding["deployment_digest"])
    monkeypatch.setattr(sys, "executable", "/opt/42-engine-venv/bin/python")
    monkeypatch.setattr(sys, "prefix", "/opt/42-engine-venv")
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network attempted"))
    calls = []
    clock = [0.0]
    if mode == "deadline":
        monkeypatch.setattr(module(), "monotonic", lambda: clock[0])

    def send(session, prepared, **kwargs):
        parsed = urlparse(prepared.url)
        calls.append((prepared.method, parsed.hostname, parsed.path))
        assert session.trust_env is False
        assert kwargs["timeout"] <= 5
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response.request = prepared
        if parsed.hostname == "metadata.google.internal":
            assert prepared.headers.get("Metadata-Flavor") == "Google"
            response.headers["Metadata-Flavor"] = "Google"
            if mode == "deadline":
                clock[0] = 21.0
            if mode == "metadata_timeout":
                raise requests.Timeout("synthetic timeout")
            if mode == "missing_flavor":
                del response.headers["Metadata-Flavor"]
            if mode == "metadata_redirect":
                response.status_code = 302
                response.headers["Location"] = "http://example.invalid/"
                value = {}
            elif parsed.path.endswith("project-id"):
                response.headers["Content-Type"] = "text/plain"
                response._content = (
                    b"foreign-project" if mode == "foreign_project" else b"ogilvy-trends-v2"
                )
                return response
            elif parsed.path.endswith("/token"):
                value = {
                    "access_token": "synthetic-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            else:
                value = {
                    "email": "foreign@example.invalid"
                    if mode == "foreign_identity"
                    else identity["service_account_email"],
                    "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                    "aliases": ["default"],
                }
        else:
            assert parsed.hostname == "storage.googleapis.com"
            assert prepared.method == "GET"
            key = unquote(parsed.path.split("/o/", 1)[1])
            prefix = "open-intelligence/v2/staging/general-questions/"
            assert key.startswith(prefix)
            raw = objects.get(key[len(prefix) :])
            if mode == "storage_denied":
                response.status_code = 403
                value = {"error": {"code": 403, "message": "synthetic denial"}}
            elif mode == "storage_redirect":
                response.status_code = 302
                response.headers["Location"] = "https://example.invalid/"
                value = {}
            elif mode == "missing_binding" or raw is None:
                response.status_code = 404
                value = {"error": {"code": 404, "message": "synthetic missing"}}
            elif parse_qs(parsed.query).get("alt") == ["media"]:
                response._content = raw
                response.raw = HTTPResponse(
                    body=io.BytesIO(raw), preload_content=False, headers=response.headers
                )
                return response
            else:
                value = {
                    "name": key,
                    "bucket": "listening-post-staging-cache",
                    "generation": "1",
                    "size": str(len(raw)),
                }
        response._content = json.dumps(value).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return root, identity, policy, calls


def test_load_host_uses_actual_compute_and_storage_sdks_read_only(tmp_path, monkeypatch):
    from google.auth.compute_engine import Credentials
    from src.analysis.open_intelligence.general_question_store import GeneralQuestionStore

    root, expected, policy, calls = host_fixture(tmp_path, monkeypatch)
    host = module().load_question_host(engine_root=root)
    assert set(host) == {"store", "runtime_identity", "credentials"}
    store, identity, credentials = host["store"], host["runtime_identity"], host["credentials"]
    assert type(store) is GeneralQuestionStore
    assert type(credentials) is Credentials
    assert identity == expected
    assert store.policy == policy
    assert credentials.service_account_email == expected["service_account_email"]
    assert calls
    assert all(method == "GET" for method, _, _ in calls)


@pytest.mark.parametrize(
    "mode",
    [
        "foreign_identity",
        "foreign_project",
        "metadata_redirect",
        "storage_redirect",
        "storage_denied",
        "missing_binding",
        "wrong_revision",
        "deadline",
        "metadata_timeout",
        "missing_flavor",
        "policy_tamper",
        "binding_tamper",
    ],
)
def test_host_refuses_foreign_missing_and_redirected_authority(tmp_path, monkeypatch, mode):
    root, _, _, calls = host_fixture(tmp_path, monkeypatch, mode)
    with pytest.raises(QuestionStoreError):
        module().load_question_host(engine_root=root)
    assert calls
    assert all(host != "example.invalid" for _, host, _ in calls)
    if mode in {
        "deadline",
        "metadata_timeout",
        "missing_flavor",
        "metadata_redirect",
        "foreign_project",
    }:
        assert len(calls) == 1


@pytest.mark.parametrize(
    "variable",
    [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "STORAGE_EMULATOR_HOST",
        "GCE_METADATA_HOST",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
    ],
)
def test_host_environment_overrides_refuse_before_network(tmp_path, monkeypatch, variable):
    root, _, _, calls = host_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, "synthetic-override")
    with pytest.raises(QuestionStoreError):
        module().load_question_host(engine_root=root)
    assert calls == []


@pytest.mark.parametrize(
    "field",
    [
        "K_SERVICE",
        "K_REVISION",
        "GENERAL_QUESTION_DEPLOYMENT_DIGEST",
        "interpreter",
        "bytecode",
        "bundle",
    ],
)
def test_host_local_binding_failures_refuse_before_network(tmp_path, monkeypatch, field):
    root, _, _, calls = host_fixture(tmp_path, monkeypatch)
    if field == "interpreter":
        monkeypatch.setattr(sys, "prefix", "/wrong-runtime")
    elif field == "bytecode":
        monkeypatch.setattr(sys, "dont_write_bytecode", False)
    elif field == "bundle":
        (root / "src/entry.py").write_bytes(b"changed")
    else:
        monkeypatch.setenv(field, "invalid")
    with pytest.raises(QuestionStoreError):
        module().load_question_host(engine_root=root)
    assert calls == []
