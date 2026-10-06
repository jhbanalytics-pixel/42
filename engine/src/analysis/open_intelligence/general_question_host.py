"""Measure shipped engine files before runtime-context loading is permitted."""

import hashlib
import json
import os
import re
import sys
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from time import monotonic
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import requests
from google.auth.compute_engine import Credentials, _metadata
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    require_digest,
)
from src.analysis.open_intelligence.general_question_deployment import (
    _AUDIENCE,
    _IDENTITY,
    _RUNTIME_FIELDS,
    _runtime_fields,
    validate_question_deployment,
)
from src.analysis.open_intelligence.general_question_policy import validate_question_policy
from src.analysis.open_intelligence.general_question_store import (
    _BUCKET,
    GeneralQuestionStore,
    _QuestionObjects,
)

_PROJECT = "ogilvy-trends-v2"
_METADATA_ROOT = "http://metadata.google.internal/computeMetadata/v1/"
_STORAGE_ROOT = "https://storage.googleapis.com"
_FORBIDDEN_ENVIRONMENT = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GOOGLE_CLOUD_UNIVERSE_DOMAIN",
    "STORAGE_EMULATOR_HOST",
    "BIGQUERY_EMULATOR_HOST",
    "GCE_METADATA_HOST",
    "GCE_METADATA_ROOT",
    "GCE_METADATA_IP",
    "GCE_METADATA_TIMEOUT",
    "GCE_METADATA_DETECT_RETRIES",
    "GCE_METADATA_MTLS_MODE",
)


def _safe_name(value):
    if type(value) is not str or not value or "\\" in value or ":" in value:
        raise ValueError("runtime_build_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(
            part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in path.parts
        )
    ):
        raise ValueError("runtime_build_invalid")
    for part in path.parts:
        name = part.casefold()
        if (
            name.startswith(".")
            or name == "__pycache__"
            or name
            in {
                "credentials.json",
                "application_default_credentials.json",
                "service-account.json",
                "service_account.json",
            }
            or name.endswith((".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx", ".env"))
        ):
            raise ValueError("runtime_build_invalid")
    return path


def _linked(path):
    return path.is_symlink() or path.is_junction()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("runtime_build_invalid")
        result[key] = value
    return result


def verify_question_engine_bundle(*, engine_root):
    try:
        root = Path(engine_root)
        if _linked(root) or not root.is_dir():
            raise ValueError()
        root = root.resolve(strict=True)
        manifest_path = root / "bundle-manifest.json"
        if _linked(manifest_path) or not manifest_path.is_file():
            raise ValueError()
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        if type(manifest) is not dict or set(manifest) != {"contract_version", "files"}:
            raise ValueError()
        if manifest["contract_version"] != "general_question_engine_bundle_v1":
            raise ValueError()
        files = manifest["files"]
        if type(files) is not dict or "requirements.lock" not in files:
            raise ValueError()
        if canonical_bytes(manifest) != raw:
            raise ValueError()
        names = set()
        for name, digest in files.items():
            _safe_name(name)
            if name.casefold() == "bundle-manifest.json" or name.casefold() in names:
                raise ValueError()
            names.add(name.casefold())
            if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError()
        actual = set()
        seen_paths = set()
        for directory, directories, filenames in os.walk(root, followlinks=False):
            for name in (*directories, *filenames):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if relative == "bundle-manifest.json":
                    continue
                _safe_name(relative)
                if _linked(path) or relative.casefold() in seen_paths:
                    raise ValueError()
                seen_paths.add(relative.casefold())
                if name in filenames:
                    if not path.is_file() or not path.resolve(strict=True).is_relative_to(root):
                        raise ValueError()
                    actual.add(relative)
        if actual != set(files):
            raise ValueError()
        for name, expected in files.items():
            path = root / name
            before = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            after = path.stat()
            if (
                digest.hexdigest() != expected
                or _linked(path)
                or (before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError()
            if path.suffix.casefold() == ".json":
                pending = [json.loads(path.read_bytes(), object_pairs_hook=_unique)]
                while pending:
                    value = pending.pop()
                    if isinstance(value, dict):
                        if value.get("type") in (
                            "service_account",
                            "authorized_user",
                            "external_account",
                        ):
                            raise ValueError()
                        if any(
                            key in {"private_key", "client_secret", "refresh_token", "access_token"}
                            and isinstance(item, str)
                            and item
                            for key, item in value.items()
                        ):
                            raise ValueError()
                        pending.extend(value.values())
                    elif isinstance(value, list):
                        pending.extend(value)
        if manifest_path.read_bytes() != raw:
            raise ValueError()
        return {
            "engine_bundle_digest": canonical_digest(manifest),
            "manifest": manifest,
            "verified_file_count": len(files),
        }
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise ValueError("runtime_build_invalid") from error


def verify_question_runtime_build(*, engine_root):
    measured = verify_question_engine_bundle(engine_root=engine_root)
    try:
        stamp_path = Path(engine_root).parent / "runtime-build.json"
        if _linked(stamp_path):
            raise ValueError()
        raw = stamp_path.read_bytes()
        stamp = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        if type(stamp) is not dict or set(stamp) != {
            "contract_version",
            "lp_commit",
            "engine_bundle_digest",
        }:
            raise ValueError()
        if (
            canonical_bytes(stamp) != raw
            or stamp["contract_version"] != "general_question_runtime_build_v1"
            or type(stamp["lp_commit"]) is not str
            or re.fullmatch(r"[0-9a-f]{40}", stamp["lp_commit"]) is None
            or stamp["engine_bundle_digest"] != measured["engine_bundle_digest"]
        ):
            raise ValueError()
        return {
            "lp_commit": stamp["lp_commit"],
            "engine_bundle_digest": measured["engine_bundle_digest"],
            "sdk_version": version("google-genai"),
        }
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise ValueError("runtime_build_invalid") from error


def _timeout(budget):
    seconds = 5.0 if budget["deadline"] is None else min(5.0, budget["deadline"] - monotonic())
    if seconds <= 0:
        raise QuestionStoreError("host_deadline_exceeded")
    return seconds


class _MetadataRequest:
    def __init__(self, budget):
        self.budget = budget
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))

    def __call__(self, url, method="GET", headers=None, **kwargs):
        parsed = urlparse(url)
        allowed_paths = {
            "/computeMetadata/v1/project/project-id",
            "/computeMetadata/v1/instance/service-accounts/default/",
            f"/computeMetadata/v1/instance/service-accounts/{_IDENTITY}/",
            "/computeMetadata/v1/instance/service-accounts/default/token",
        }
        if (
            method != "GET"
            or parsed.scheme != "http"
            or parsed.netloc != "metadata.google.internal"
            or parsed.fragment
            or unquote(parsed.path) not in allowed_paths
        ):
            raise QuestionStoreError("host_metadata_invalid")
        try:
            response = self.session.request(
                "GET",
                url,
                headers={**(headers or {}), "Metadata-Flavor": "Google"},
                timeout=_timeout(self.budget),
                allow_redirects=False,
            )
            _timeout(self.budget)
            if response.status_code != 200 or response.headers.get("Metadata-Flavor") != "Google":
                raise QuestionStoreError("host_metadata_unavailable")
            if parsed.path.endswith("/") and response.json().get("email") != _IDENTITY:
                raise QuestionStoreError("identity_invalid")
            return SimpleNamespace(
                status=response.status_code, data=response.content, headers=response.headers
            )
        except QuestionStoreError:
            raise
        except Exception as error:
            raise QuestionStoreError("host_metadata_unavailable") from error


class _HostStorageSession(AuthorizedSession):
    def __init__(self, credentials, metadata_request, budget):
        super().__init__(
            credentials, auth_request=metadata_request, max_refresh_attempts=0, refresh_timeout=5
        )
        self.trust_env = False
        self.budget = budget
        self.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        paths = (
            f"/storage/v1/b/{_BUCKET}/o",
            f"/download/storage/v1/b/{_BUCKET}/o",
            f"/upload/storage/v1/b/{_BUCKET}/o",
        )
        if (
            parsed.scheme != "https"
            or parsed.netloc != "storage.googleapis.com"
            or parsed.fragment
            or method not in {"GET", "POST"}
            or not any(parsed.path == path or parsed.path.startswith(path + "/") for path in paths)
            or self.credentials.service_account_email != _IDENTITY
        ):
            raise QuestionStoreError("storage_target_invalid")
        seconds = _timeout(self.budget)
        kwargs.update(timeout=seconds, max_allowed_time=seconds, allow_redirects=False)
        response = super().request(method, url, **kwargs)
        _timeout(self.budget)
        if 300 <= response.status_code < 400:
            raise QuestionStoreError("storage_unavailable")
        return response


def load_question_host(*, engine_root):
    metadata_request = session = None
    try:
        if (
            sys.version_info[:2] != (3, 13)
            or sys.prefix != "/opt/42-engine-venv"
            or sys.executable != "/opt/42-engine-venv/bin/python"
            or not sys.dont_write_bytecode
            or any(os.environ.get(name) for name in _FORBIDDEN_ENVIRONMENT)
            or os.environ.get("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false") != "false"
            or os.environ.get("GOOGLE_API_USE_MTLS_ENDPOINT", "never") not in {"never", "auto"}
            or any(
                os.environ.get(name, _PROJECT) != _PROJECT
                for name in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT")
            )
        ):
            raise QuestionStoreError("host_environment_invalid")
        deployment_digest = require_digest(
            os.environ.get("GENERAL_QUESTION_DEPLOYMENT_DIGEST"), "approval_required"
        )
        measured = verify_question_runtime_build(engine_root=engine_root)
        runtime = {
            "service_name": os.environ.get("K_SERVICE"),
            "revision_name": os.environ.get("K_REVISION"),
            "service_account_email": _IDENTITY,
            **measured,
            "canonical_service_audience": _AUDIENCE,
        }
        _runtime_fields(runtime)
        budget = {"deadline": monotonic() + 20.0}
        metadata_request = _MetadataRequest(budget)
        project = _metadata.get(
            metadata_request, "project/project-id", root=_METADATA_ROOT, retry_count=1
        )
        if project != _PROJECT:
            raise QuestionStoreError("identity_invalid")
        credentials = Credentials(
            quota_project_id=_PROJECT,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            universe_domain="googleapis.com",
        )
        credentials.refresh(metadata_request)
        if credentials.service_account_email != _IDENTITY:
            raise QuestionStoreError("identity_invalid")
        runtime["service_account_email"] = credentials.service_account_email
        session = _HostStorageSession(credentials, metadata_request, budget)
        client = storage.Client(
            project=_PROJECT,
            credentials=credentials,
            _http=session,
            client_options={"api_endpoint": _STORAGE_ROOT},
        )
        bucket = client.bucket(_BUCKET)
        objects = _QuestionObjects(bucket)
        stored_binding = objects.read(f"deployments/{deployment_digest}/binding.json")
        if stored_binding is None:
            raise QuestionStoreError("approval_required")
        binding = validate_question_deployment(stored_binding.value)
        if binding["deployment_digest"] != deployment_digest or any(
            binding[field] != runtime[field] for field in _RUNTIME_FIELDS
        ):
            raise QuestionStoreError("approval_required")
        stored_policy = objects.read(f"policies/{binding['policy_digest']}/policy.json")
        if stored_policy is None:
            raise QuestionStoreError("approval_required")
        policy = validate_question_policy(stored_policy.value)
        if policy["policy_digest"] != binding["policy_digest"]:
            raise QuestionStoreError("approval_required")
        _timeout(budget)
        store = GeneralQuestionStore(bucket, policy=policy, deployment_digest=deployment_digest)
        budget["deadline"] = None
        return {"store": store, "runtime_identity": runtime, "credentials": credentials}
    except Exception as error:
        if session is not None:
            session.close()
        if metadata_request is not None:
            metadata_request.session.close()
        if isinstance(error, QuestionStoreError):
            raise
        raise QuestionStoreError("host_unavailable") from error
