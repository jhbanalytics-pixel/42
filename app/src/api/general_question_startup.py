"""Attach the verified general-question worker to the existing 42 application."""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import requests
from google.auth.compute_engine import Credentials, _metadata
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from src.api import deployment_contract
from src.api.general_question_queue import GeneralQuestionQueue, _TARGET
from src.api.general_question_routes import GeneralQuestionRoutes
from src.api.question_worker_bundle import _json_file, verify_bundle
from src.api.question_worker_controller import QuestionWorker
from src.api.question_worker_protocol import _read_reply
from src.api.question_worker_store import _read_object

_ENGINE_ROOT = Path("/app/engine")
_STAMP_PATH = Path("/app/runtime-build.json")
_INTERPRETER = Path("/opt/42-engine-venv/bin/python")
_PROJECT = "ogilvy-trends-v2"
_BUCKET = "listening-post-staging-cache"
_AUDIENCE = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
_IDENTITY = deployment_contract.REQUIRED_SERVICE_ACCOUNT
_PREFIX = "open-intelligence/v2/staging/general-questions/"
_BINDING_FIELDS = {
    "contract_version",
    "project",
    "region",
    "service_name",
    "revision_name",
    "service_account_email",
    "lp_commit",
    "engine_bundle_digest",
    "canonical_service_audience",
    "sdk_version",
    "image_digest",
    "worker_path",
    "policy_digest",
    "deployment_digest",
}
_FORBIDDEN = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "STORAGE_EMULATOR_HOST",
    "BIGQUERY_EMULATOR_HOST",
    "GOOGLE_CLOUD_UNIVERSE_DOMAIN",
    "GCE_METADATA_HOST",
    "GCE_METADATA_ROOT",
    "GCE_METADATA_IP",
    "GCE_METADATA_TIMEOUT",
    "GCE_METADATA_DETECT_RETRIES",
    "GCE_METADATA_MTLS_MODE",
)
logger = logging.getLogger("listening_post.question_startup")


class QuestionStartupError(ValueError):
    pass


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _remaining(budget):
    remaining = (
        5.0
        if budget["deadline"] is None
        else min(5.0, budget["deadline"] - time.monotonic())
    )
    if remaining <= 0:
        raise QuestionStartupError("question_startup_deadline")
    return remaining


class _MetadataRequest:
    def __init__(self, budget):
        self.budget = budget
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))

    def __call__(self, url, method="GET", headers=None, **kwargs):
        parsed = urlparse(url)
        paths = {
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
            or unquote(parsed.path) not in paths
        ):
            raise QuestionStartupError("question_metadata_invalid")
        try:
            response = self.session.get(
                url,
                headers={**(headers or {}), "Metadata-Flavor": "Google"},
                timeout=_remaining(self.budget),
                allow_redirects=False,
            )
            _remaining(self.budget)
            if (
                response.status_code != 200
                or response.headers.get("Metadata-Flavor") != "Google"
                or len(response.content) > 16384
            ):
                raise QuestionStartupError("question_metadata_unavailable")
            if parsed.path.endswith("/") and response.json().get("email") != _IDENTITY:
                raise QuestionStartupError("question_principal_invalid")
            return SimpleNamespace(
                status=response.status_code,
                data=response.content,
                headers=response.headers,
            )
        except QuestionStartupError:
            raise
        except Exception:
            raise QuestionStartupError("question_metadata_unavailable") from None


class _AttachedCredentials(Credentials):
    def __init__(self, metadata_request):
        super().__init__(
            quota_project_id=_PROJECT,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            universe_domain="googleapis.com",
        )
        self.metadata_request = metadata_request

    def refresh(self, request):
        super().refresh(self.metadata_request)
        if self.service_account_email != _IDENTITY:
            raise QuestionStartupError("question_principal_invalid")


class _StorageSession(AuthorizedSession):
    def __init__(self, credentials, metadata_request, budget):
        super().__init__(
            credentials,
            auth_request=metadata_request,
            max_refresh_attempts=0,
            refresh_timeout=5,
        )
        self.budget = budget
        self.trust_env = False
        self.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        paths = (f"/storage/v1/b/{_BUCKET}/o/", f"/download/storage/v1/b/{_BUCKET}/o/")
        if (
            method != "GET"
            or parsed.scheme != "https"
            or parsed.netloc != "storage.googleapis.com"
            or parsed.fragment
            or not any(parsed.path.startswith(path) for path in paths)
        ):
            raise QuestionStartupError("question_storage_target_invalid")
        seconds = _remaining(self.budget)
        kwargs.update(timeout=seconds, max_allowed_time=seconds, allow_redirects=False)
        result = super().request(method, url, **kwargs)
        _remaining(self.budget)
        if 300 <= result.status_code < 400:
            raise QuestionStartupError("question_storage_unavailable")
        return result


def _binding(raw, expected_digest, stamp):
    value = _read_reply(raw, max_bytes=65536)
    if (
        type(value) is not dict
        or set(value) != _BINDING_FIELDS
        or _canonical(value) != raw
    ):
        raise QuestionStartupError("question_binding_invalid")
    if (
        value["deployment_digest"] != expected_digest
        or hashlib.sha256(
            _canonical({k: v for k, v in value.items() if k != "deployment_digest"})
        ).hexdigest()
        != expected_digest
    ):
        raise QuestionStartupError("question_binding_invalid")
    expected = {
        "contract_version": "general_question_deployment_v1",
        "project": _PROJECT,
        "region": "us-central1",
        "service_name": "listening-post-staging",
        "revision_name": os.environ["K_REVISION"],
        "service_account_email": _IDENTITY,
        "lp_commit": os.environ["SOURCE_SHA"],
        "engine_bundle_digest": stamp["engine_bundle_digest"],
        "canonical_service_audience": _AUDIENCE,
        "worker_path": "/internal/general-question/execute",
    }
    if any(value[key] != item for key, item in expected.items()):
        raise QuestionStartupError("question_binding_mismatch")
    for field in ("policy_digest", "image_digest", "engine_bundle_digest"):
        if (
            type(value[field]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None
        ):
            raise QuestionStartupError("question_binding_invalid")
    if (
        type(value["sdk_version"]) is not str
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value["sdk_version"]) is None
    ):
        raise QuestionStartupError("question_binding_invalid")
    return value


def build_general_question_routes(*, try_acquire, release):
    metadata = session = None
    try:
        if not callable(try_acquire) or not callable(release):
            raise QuestionStartupError("question_capacity_invalid")
        digest = os.environ.get("GENERAL_QUESTION_DEPLOYMENT_DIGEST", "")
        worker_url = os.environ.get("GENERAL_QUESTION_WORKER_URL", "")
        if (
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or _TARGET.fullmatch(worker_url) is None
        ):
            raise QuestionStartupError("question_activation_missing")
        if (
            os.environ.get("DEPLOYMENT_PROFILE") != deployment_contract.STAGING_PROFILE
            or os.environ.get("K_SERVICE") != "listening-post-staging"
            or os.environ.get("GCP_PROJECT") != _PROJECT
        ):
            raise QuestionStartupError("question_runtime_invalid")
        revision = os.environ.get("K_REVISION", "")
        if (
            len(revision) > 63
            or re.fullmatch(
                r"listening-post-staging-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", revision
            )
            is None
            or re.fullmatch(r"[0-9a-f]{40}", os.environ.get("SOURCE_SHA", "")) is None
        ):
            raise QuestionStartupError("question_runtime_invalid")
        if (
            any(os.environ.get(key) for key in _FORBIDDEN)
            or os.environ.get("WEB_CONCURRENCY", "1") != "1"
            or os.environ.get("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false") != "false"
            or os.environ.get("GOOGLE_API_USE_MTLS_ENDPOINT", "never")
            not in {"never", "auto"}
        ):
            raise QuestionStartupError("question_environment_invalid")
        stamp = _json_file(_STAMP_PATH, 4096)
        if (
            _canonical(stamp) != _STAMP_PATH.read_bytes()
            or stamp.get("lp_commit") != os.environ["SOURCE_SHA"]
        ):
            raise QuestionStartupError("question_build_invalid")
        verify_bundle(
            _ENGINE_ROOT,
            _STAMP_PATH,
            expected_lp_commit=os.environ["SOURCE_SHA"],
            expected_bundle_digest=stamp.get("engine_bundle_digest"),
        )
        if not _INTERPRETER.is_file() or _INTERPRETER.is_symlink():
            raise QuestionStartupError("question_interpreter_unavailable")
        budget = {"deadline": time.monotonic() + 20}
        metadata = _MetadataRequest(budget)
        if (
            _metadata.get(
                metadata,
                "project/project-id",
                root="http://metadata.google.internal/computeMetadata/v1/",
                retry_count=1,
            )
            != _PROJECT
        ):
            raise QuestionStartupError("question_project_invalid")
        credentials = _AttachedCredentials(metadata)
        credentials.refresh(metadata)
        deployment_contract.validate_runtime_contract(
            metadata_email_getter=lambda: credentials.service_account_email
        )
        session = _StorageSession(credentials, metadata, budget)
        client = storage.Client(
            project=_PROJECT,
            credentials=credentials,
            _http=session,
            client_options={"api_endpoint": "https://storage.googleapis.com"},
        )
        raw, _generation = _read_object(
            client,
            _PREFIX + f"deployments/{digest}/binding.json",
            generation=None,
            deadline=budget["deadline"],
        )
        binding = _binding(raw, digest, stamp)
        worker = QuestionWorker(
            bundle_root=_ENGINE_ROOT,
            interpreter=_INTERPRETER,
            stamp_path=_STAMP_PATH,
            lp_commit=binding["lp_commit"],
            bundle_digest=binding["engine_bundle_digest"],
            storage_client=client,
            try_acquire=try_acquire,
            release=release,
        )
        queue = GeneralQuestionQueue(
            credentials, worker_url=worker_url, deployment_digest=digest
        )
        _remaining(budget)
        budget["deadline"] = None
        return GeneralQuestionRoutes(
            worker,
            queue,
            policy_digest=binding["policy_digest"],
            deployment_digest=digest,
        )
    except Exception as error:
        if session is not None:
            session.close()
        if metadata is not None:
            metadata.session.close()
        if isinstance(error, QuestionStartupError):
            raise
        raise QuestionStartupError("question_startup_unavailable") from error


def configure_general_question_startup(app, *, try_acquire, release):
    app.state.general_question_routes = None
    try:
        app.state.general_question_routes = build_general_question_routes(
            try_acquire=try_acquire, release=release
        )
        code = "question_worker_configured"
    except QuestionStartupError as error:
        code = str(error)
    app.state.general_question_startup_code = code
    revision = os.environ.get("K_REVISION", "")
    source = os.environ.get("SOURCE_SHA", "")
    revision = (
        revision
        if re.fullmatch(r"listening-post-staging-[a-z0-9-]{1,40}", revision)
        else "unverified"
    )
    source = source if re.fullmatch(r"[0-9a-f]{40}", source) else "unverified"
    logger.warning(
        "code=%s component=general_question_startup revision=%s source_digest=%s",
        code,
        revision,
        source,
    )
