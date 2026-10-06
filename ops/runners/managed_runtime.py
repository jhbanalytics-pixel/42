import base64
import functools
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from ops.deploy.resource_guard import assert_allowed, load_resource_manifest

PROJECT = "ogilvy-trends-v2"
REGION = "us-central1"
DAILY_JOB_ID = "intelligence-42-daily-staging"
PRICE_POLICY_JOB_ID = "intelligence-42-price-policy-staging"
DAILY_JOB_NAME = f"projects/{PROJECT}/locations/{REGION}/jobs/{DAILY_JOB_ID}"
DAILY_JOB_RESOURCE = f"//run.googleapis.com/{DAILY_JOB_NAME}"
RUN_JOB_URI = f"https://run.googleapis.com/v2/{DAILY_JOB_NAME}:run"
PRICE_POLICY_RUN_URI = RUN_JOB_URI.replace(DAILY_JOB_ID, PRICE_POLICY_JOB_ID)
EMPTY_RUN_REQUEST_BODY = b"{}"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
CONFIGURATION_ANNOTATION = "42.ogilvy/runtime-configuration-sha256"
INVOCATION_FIELDS = ("job_resource", "resource_manifest_digest", "mode", "request_id")
MODES = ("verify-runtime", "daily")
JOBS_CONTRACT = "42_managed_runtime_jobs_v1"
SCHEDULERS_CONTRACT = "42_managed_runtime_schedulers_v1"
DAILY_PROFILE_FIELDS = (
    "schema_version",
    "resource_manifest_digest",
    "source_policy_digest",
    "recurring_grant_digest",
    "freshness_target_hours",
    "max_publish_lag_hours",
    "max_catchup_cutoffs",
    "cutoffs_per_cycle",
    "approval_phrase_sha256",
)
KERNEL_PROFILE_FIELDS = DAILY_PROFILE_FIELDS[:7]
# The two profile digests the owner's grant pointer carries. The image cannot carry
# them: the grant digest covers permitted_image_digests and the cycle refuses an image
# the grant does not list, so an image pinning its own grant cannot be built. The image
# carries the rest of the profile; the cycle joins the two at start.
GRANT_POINTER_FIELDS = ("recurring_grant_digest", "approval_phrase_sha256")
IMAGE_PROFILE_FIELDS = tuple(
    name for name in DAILY_PROFILE_FIELDS if name not in GRANT_POINTER_FIELDS
)
DAILY_PROFILE_SCHEMA = "42_daily_v1"
DAILY_ENVIRONMENT = "staging"
# The only environment the daily job may carry: the producer resolves its warehouse from
# these two, and the engine's collect stage refuses any resolution outside the staging
# source dataset. Plain values, no secrets; the exact execution authority carries the
# receipt identity in process, so no COLLECTION_* variable is set here.
DAILY_JOB_ENVIRONMENT = [
    {"name": "TRENDS_ENV", "value": DAILY_ENVIRONMENT},
    {"name": "BIGQUERY_DATASET", "value": "intelligence_42_sources_staging"},
]
GRANT_PREFIX = "42/daily/grants"
# The owner's create once pointers, one numbered sequence per image digest. The daily
# account reads this prefix and cannot create, replace or delete anything under it.
GRANT_POINTER_PREFIX = f"{GRANT_PREFIX}/pointers"
GRANT_POINTER_CONTRACT = "42_daily_grant_pointer_v1"
MAX_GRANT_POINTERS = 64
RUNTIME_COMMAND = ["python"]
RUNTIME_ARGS = ["-m", "ops.runners.managed_runtime"]
PRICE_POLICY_ARGS = ["-m", "ops.deploy.refresh_question_policy"]

_MAX_CONFIGURATION_BYTES = 256 * 1024
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_BUILD_RESOURCE = re.compile(r"projects/[^/]+/locations/[^/]+/builds/[^/]+")
BUILD_LIST_URI = f"https://cloudbuild.googleapis.com/v1/projects/{PROJECT}/builds"
# The reviewed manifest row the build provenance read is guarded against: the builds
# collection of the Cloud Build location, read only.
BUILDS_RESOURCE = (
    f"//cloudbuild.googleapis.com/projects/{PROJECT}/locations/{REGION}/builds"
)
# Where a Cloud Build record carries the commit it built: the resolved provenance the
# engine's build provenance receipt reads, and the requested source it resolves from.
_BUILD_SOURCE_REVISIONS = (
    ("sourceProvenance", "resolvedRepoSource", "commitSha"),
    ("sourceProvenance", "resolvedGitSource", "revision"),
    ("sourceProvenance", "resolvedConnectedRepository", "revision"),
    ("source", "repoSource", "commitSha"),
    ("source", "gitSource", "revision"),
    ("source", "connectedRepository", "revision"),
)
_EXECUTION_ID = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SERVICE_ACCOUNT = re.compile(
    rf"[a-z][a-z0-9-]{{4,28}}[a-z0-9]@{re.escape(PROJECT)}\.iam\.gserviceaccount\.com"
)
_IMAGE_REPOSITORY = re.compile(
    rf"{REGION}-docker\.pkg\.dev/{re.escape(PROJECT)}/intelligence-42/[a-z][a-z0-9-]*"
)
_CRON = re.compile(r"[0-9*/,-]+(?: [0-9*/,-]+){4}")
_DURATION = re.compile(r"[1-9][0-9]*s")
_JOBS_KEYS = {
    "contract_version",
    "credential_source",
    "daily_profile",
    "jobs",
    "location",
    "project",
    "resource_manifest_sha256",
    "retention",
    "runtime_job",
    "scheduler_configuration_sha256",
}
_JOB_ENTRY_KEYS = {
    "args",
    "command",
    "env",
    "image_repository",
    "job_resource",
    "max_retries",
    "mode",
    "parallelism",
    "service_account",
    "task_count",
    "timeout_seconds",
}
_RETENTION_KEYS = {"execution_history_limit", "operation_ledger_days"}
_SCHEDULERS_KEYS = {"contract_version", "location", "project", "schedulers"}
_SCHEDULER_JOB_KEYS = {
    "attemptDeadline",
    "description",
    "httpTarget",
    "name",
    "retryConfig",
    "schedule",
    "state",
    "timeZone",
}
_HTTP_TARGET_KEYS = {"body", "headers", "httpMethod", "oauthToken", "uri"}
_INVOCATION_KEYS = set(INVOCATION_FIELDS) | {
    "configuration_sha256",
    "credential_source",
    "execution_name",
    "image_uri",
    "job_name",
    "principal",
    "scheduler_configuration_sha256",
    "service_account",
}
_OVERRIDE_VARIABLES = (
    "MANAGED_RUNTIME_MODE",
    "MANAGED_RUNTIME_JOB_RESOURCE",
    "MANAGED_RUNTIME_REQUEST_ID",
    "MANAGED_RUNTIME_RESOURCE_MANIFEST_DIGEST",
    "MANAGED_RUNTIME_CONFIGURATION",
)
_CREDENTIAL_VARIABLES = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_CONFIG",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
)
_CREDENTIAL_KEYS = {"kind", "principal", "source"}
_CREDENTIAL_REFUSALS = {
    "user_oauth": "credential_source_user_oauth",
    "refresh_token_file": "credential_source_user_oauth",
    "service_account_key_file": "credential_source_file",
    "host_scheduler": "credential_source_host_dependency",
}
_TERMINAL_STATES = {"completed", "failed"}
_DAILY_WIRING_KEYS = {
    "engine",
    "profile",
    "grant_loader",
    "object_client",
    "stage_handlers",
    "now",
    "environment",
}
_DAILY_MODULES = (
    "daily_cycle",
    "daily_authority",
    "daily_store",
    "recurring_grant",
    "daily_native_clients",
    "execution_generations",
)


def _refuse(code):
    raise ValueError(code)


def _text(value):
    if not isinstance(value, str) or not value or not value.isascii():
        return False
    return not any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
    )


def _string_list(value, *, nonempty):
    if type(value) is not list or (nonempty and not value):
        return False
    return all(_text(item) for item in value)


def canonical_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unique_object(pairs, code):
    value = {}
    for key, item in pairs:
        if key in value:
            _refuse(code)
        value[key] = item
    return value


def _read_bytes(path, code):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_CONFIGURATION_BYTES + 1)
    except (OSError, TypeError, ValueError):
        _refuse(code)
    if len(raw) > _MAX_CONFIGURATION_BYTES:
        _refuse(code)
    return raw


def _parse_canonical(raw, code):
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_object(pairs, code),
            parse_constant=lambda _value: _refuse(code),
        )
    except (UnicodeError, json.JSONDecodeError):
        _refuse(code)
    if type(value) is not dict or canonical_bytes(value) != raw:
        _refuse(code)
    return value


def _validate_job_entry(job_id, entry):
    code = "runtime_configuration_invalid"
    if type(entry) is not dict or set(entry) != _JOB_ENTRY_KEYS:
        _refuse(code)
    if (
        entry["job_resource"]
        != f"//run.googleapis.com/projects/{PROJECT}/locations/{REGION}/jobs/{job_id}"
    ):
        _refuse(code)
    if not isinstance(entry["service_account"], str) or not _SERVICE_ACCOUNT.fullmatch(
        entry["service_account"]
    ):
        _refuse(code)
    if not isinstance(
        entry["image_repository"], str
    ) or not _IMAGE_REPOSITORY.fullmatch(entry["image_repository"]):
        _refuse(code)
    if not _string_list(entry["command"], nonempty=True) or not _string_list(
        entry["args"], nonempty=True
    ):
        _refuse(code)
    if type(entry["env"]) is not list or any(
        type(item) is not dict
        or set(item) != {"name", "value"}
        or not _text(item["name"])
        for item in entry["env"]
    ):
        _refuse(code)
    for name in ("max_retries", "parallelism", "task_count", "timeout_seconds"):
        if type(entry[name]) is not int:
            _refuse(code)
    if (
        entry["max_retries"] != 0
        or entry["parallelism"] != 1
        or entry["task_count"] != 1
        or not 0 < entry["timeout_seconds"] <= 86400
    ):
        _refuse(code)
    if job_id == DAILY_JOB_ID:
        if (
            entry["mode"] not in MODES
            or entry["command"] != RUNTIME_COMMAND
            or entry["args"] != RUNTIME_ARGS
            or entry["env"] != DAILY_JOB_ENVIRONMENT
        ):
            _refuse(code)
    elif (
        entry["mode"] is not None
        or entry["command"] != RUNTIME_COMMAND
        or entry["args"][:2] != PRICE_POLICY_ARGS
    ):
        _refuse(code)


def _validated_profile_fields(profile, fields):
    code = "runtime_configuration_invalid"
    if type(profile) is not dict or set(profile) != set(fields):
        _refuse(code)
    if profile["schema_version"] != DAILY_PROFILE_SCHEMA:
        _refuse(code)
    for name in (
        "resource_manifest_digest",
        "source_policy_digest",
        *GRANT_POINTER_FIELDS,
    ):
        if name not in fields:
            continue
        if not isinstance(profile[name], str) or not _HEX_64.fullmatch(profile[name]):
            _refuse(code)
    for name in (
        "freshness_target_hours",
        "max_publish_lag_hours",
        "max_catchup_cutoffs",
        "cutoffs_per_cycle",
    ):
        if type(profile[name]) is not int or profile[name] < 0:
            _refuse(code)
    if (
        profile["cutoffs_per_cycle"] != 1
        or profile["freshness_target_hours"] == 0
        or profile["max_publish_lag_hours"] < profile["freshness_target_hours"]
    ):
        _refuse(code)
    return dict(profile)


def _validated_image_profile(profile):
    """The profile the image carries: every daily field except the two grant pins."""
    return _validated_profile_fields(profile, IMAGE_PROFILE_FIELDS)


def _validated_daily_profile(profile):
    """The profile the cycle runs: the image profile joined with the pointer's pins."""
    return _validated_profile_fields(profile, DAILY_PROFILE_FIELDS)


def _validated_jobs_configuration(config):
    code = "runtime_configuration_invalid"
    if type(config) is not dict or set(config) != _JOBS_KEYS:
        _refuse(code)
    if (
        config["contract_version"] != JOBS_CONTRACT
        or config["project"] != PROJECT
        or config["location"] != REGION
        or config["credential_source"] != "attached_service_account"
        or config["runtime_job"] != DAILY_JOB_ID
    ):
        _refuse(code)
    for name in ("resource_manifest_sha256", "scheduler_configuration_sha256"):
        if not isinstance(config[name], str) or not _HEX_64.fullmatch(config[name]):
            _refuse(code)
    retention = config["retention"]
    if type(retention) is not dict or set(retention) != _RETENTION_KEYS:
        _refuse(code)
    if any(type(value) is not int or value <= 0 for value in retention.values()):
        _refuse(code)
    jobs = config["jobs"]
    if type(jobs) is not dict or set(jobs) != {DAILY_JOB_ID, PRICE_POLICY_JOB_ID}:
        _refuse(code)
    for job_id, entry in jobs.items():
        _validate_job_entry(job_id, entry)
    _validated_image_profile(config["daily_profile"])
    return config


def load_runtime_configuration(path) -> dict:
    raw = _read_bytes(path, "runtime_configuration_invalid")
    return _validated_jobs_configuration(
        _parse_canonical(raw, "runtime_configuration_invalid")
    )


def _validate_scheduler_entry(job_id, entry):
    code = "scheduler_configuration_invalid"
    if type(entry) is not dict or set(entry) != {"job", "proposal"}:
        _refuse(code)
    job = entry["job"]
    if type(job) is not dict or set(job) != _SCHEDULER_JOB_KEYS:
        _refuse(code)
    if (
        job["name"] != f"projects/{PROJECT}/locations/{REGION}/jobs/{job_id}"
        or job["state"] != "PAUSED"
        or job["timeZone"] != "Etc/UTC"
        or job["retryConfig"] != {"retryCount": 0, "maxRetryDuration": "0s"}
        or not isinstance(job["schedule"], str)
        or not _CRON.fullmatch(job["schedule"])
        or not isinstance(job["attemptDeadline"], str)
        or not _DURATION.fullmatch(job["attemptDeadline"])
        or not isinstance(job["description"], str)
        or type(entry["proposal"]) is not dict
    ):
        _refuse(code)
    target = job["httpTarget"]
    if type(target) is not dict or set(target) != _HTTP_TARGET_KEYS:
        _refuse(code)
    token = target["oauthToken"]
    if (
        target["uri"]
        != f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{job_id}:run"
        or target["httpMethod"] != "POST"
        or target["headers"] != {"Content-Type": "application/json"}
        or type(token) is not dict
        or set(token) != {"serviceAccountEmail", "scope"}
        or not isinstance(token["serviceAccountEmail"], str)
        or not _SERVICE_ACCOUNT.fullmatch(token["serviceAccountEmail"])
        or token["scope"] != CLOUD_PLATFORM_SCOPE
        or not isinstance(target["body"], str)
    ):
        _refuse(code)
    try:
        body = base64.b64decode(target["body"], validate=True)
    except (ValueError, TypeError):
        _refuse(code)
    if body != EMPTY_RUN_REQUEST_BODY:
        _refuse(code)


def load_scheduler_configuration(path, *, expected_sha256: str) -> dict:
    code = "scheduler_configuration_invalid"
    if not isinstance(expected_sha256, str) or not _HEX_64.fullmatch(expected_sha256):
        _refuse(code)
    raw = _read_bytes(path, code)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        _refuse("scheduler_configuration_digest_mismatch")
    config = _parse_canonical(raw, code)
    if set(config) != _SCHEDULERS_KEYS:
        _refuse(code)
    if (
        config["contract_version"] != SCHEDULERS_CONTRACT
        or config["project"] != PROJECT
        or config["location"] != REGION
    ):
        _refuse(code)
    schedulers = config["schedulers"]
    if type(schedulers) is not dict or set(schedulers) != {
        DAILY_JOB_ID,
        PRICE_POLICY_JOB_ID,
    }:
        _refuse(code)
    for job_id, entry in schedulers.items():
        _validate_scheduler_entry(job_id, entry)
    return config


def validate_invocation(payload: dict, resources: dict) -> dict:
    if type(payload) is not dict or set(payload) != set(INVOCATION_FIELDS):
        _refuse("invocation_fields_invalid")
    if any(not _text(payload[field]) for field in INVOCATION_FIELDS):
        _refuse("invocation_fields_invalid")
    if not _EXECUTION_ID.fullmatch(payload["request_id"]):
        _refuse("invocation_fields_invalid")
    if payload["mode"] not in MODES:
        _refuse("invocation_mode_invalid")
    if payload["job_resource"] != DAILY_JOB_RESOURCE:
        _refuse("invocation_target_forbidden")
    assert_allowed(DAILY_JOB_RESOURCE, "invoke", resources)
    assert_allowed(DAILY_JOB_RESOURCE, "read", resources)
    digest = payload["resource_manifest_digest"]
    if not _HEX_64.fullmatch(digest) or digest != canonical_sha256(resources):
        _refuse("resource_manifest_digest_mismatch")
    return {field: payload[field] for field in INVOCATION_FIELDS}


def _attached_principal(credential_discovery, expected):
    try:
        descriptor = credential_discovery()
    except Exception as exc:
        raise ValueError("credential_source_unavailable") from exc
    if type(descriptor) is not dict or set(descriptor) != _CREDENTIAL_KEYS:
        _refuse("credential_source_invalid")
    if any(not isinstance(value, str) for value in descriptor.values()):
        _refuse("credential_source_invalid")
    kind = descriptor["kind"]
    if kind in _CREDENTIAL_REFUSALS:
        _refuse(_CREDENTIAL_REFUSALS[kind])
    if kind != "attached_service_account" or descriptor["source"] != "metadata_server":
        _refuse("credential_source_invalid")
    if descriptor["principal"] != expected:
        _refuse("principal_mismatch")
    return descriptor["principal"]


def _confirm_parent_job(execution_reader, execution_name, entry, configuration_sha256):
    try:
        readback = execution_reader(execution_name)
    except PermissionError as exc:
        raise ValueError("execution_readback_forbidden") from exc
    except Exception as exc:
        raise ValueError("execution_readback_unavailable") from exc
    if type(readback) is not dict or set(readback) != {"execution", "job"}:
        _refuse("execution_readback_invalid")
    execution, job = readback["execution"], readback["job"]
    if type(execution) is not dict or type(job) is not dict:
        _refuse("execution_readback_invalid")
    if execution.get("name") != execution_name:
        _refuse("execution_identity_mismatch")
    if (
        execution.get("job") not in (DAILY_JOB_NAME, DAILY_JOB_ID)
        or job.get("name") != DAILY_JOB_NAME
    ):
        _refuse("execution_job_mismatch")
    job_template = job.get("template")
    if type(job_template) is not dict or type(job_template.get("template")) is not dict:
        _refuse("execution_readback_invalid")
    annotations = job_template.get("annotations")
    if (
        type(annotations) is not dict
        or execution.get("annotations") != annotations
        or annotations.get(CONFIGURATION_ANNOTATION) != configuration_sha256
    ):
        _refuse("runtime_configuration_unbound")
    task = job_template["template"]
    if (
        execution.get("template") != task
        or execution.get("taskCount") != job_template.get("taskCount")
        or execution.get("parallelism") != job_template.get("parallelism")
    ):
        _refuse("runtime_override_forbidden")
    if task.get("serviceAccount") != entry["service_account"]:
        _refuse("principal_mismatch")
    containers = task.get("containers")
    if (
        type(containers) is not list
        or len(containers) != 1
        or type(containers[0]) is not dict
    ):
        _refuse("runtime_configuration_mismatch")
    container = containers[0]
    if (
        task.get("maxRetries") != entry["max_retries"]
        or task.get("timeout") != f"{entry['timeout_seconds']}s"
        or job_template.get("taskCount") != entry["task_count"]
        or job_template.get("parallelism") != entry["parallelism"]
        or container.get("command") != entry["command"]
        or container.get("args") != entry["args"]
        or container.get("env", []) != entry["env"]
    ):
        _refuse("runtime_configuration_mismatch")
    image = container.get("image")
    prefix = entry["image_repository"] + "@sha256:"
    if (
        not isinstance(image, str)
        or not image.startswith(prefix)
        or not _HEX_64.fullmatch(image[len(prefix) :])
    ):
        _refuse("image_unbound")
    return image


def build_invocation(
    job_config: dict,
    resources: dict,
    execution_name: str,
    *,
    execution_reader,
    credential_discovery,
    environment=None,
) -> dict:
    config = _validated_jobs_configuration(job_config)
    entry = config["jobs"][config["runtime_job"]]
    if not isinstance(execution_name, str) or not _EXECUTION_ID.fullmatch(
        execution_name
    ):
        _refuse("execution_identity_missing")
    variables = os.environ if environment is None else environment
    if any(name in variables for name in _OVERRIDE_VARIABLES):
        _refuse("runtime_override_forbidden")
    if "CLOUD_RUN_JOB" in variables and variables["CLOUD_RUN_JOB"] != DAILY_JOB_ID:
        _refuse("runtime_override_forbidden")
    if any(name in variables for name in _CREDENTIAL_VARIABLES):
        _refuse("credential_file_forbidden")
    invocation = validate_invocation(
        {
            "job_resource": entry["job_resource"],
            "resource_manifest_digest": config["resource_manifest_sha256"],
            "mode": entry["mode"],
            "request_id": execution_name,
        },
        resources,
    )
    approved_identity = resources["identities"]["orchestration"].rsplit("/", 1)[1]
    if entry["service_account"] != approved_identity:
        _refuse("principal_mismatch")
    principal = _attached_principal(credential_discovery, entry["service_account"])
    configuration_sha256 = canonical_sha256(config)
    full_name = f"{DAILY_JOB_NAME}/executions/{execution_name}"
    image_uri = _confirm_parent_job(
        execution_reader, full_name, entry, configuration_sha256
    )
    return {
        **invocation,
        "execution_name": full_name,
        "job_name": DAILY_JOB_NAME,
        "principal": principal,
        "credential_source": "attached_service_account",
        "configuration_sha256": configuration_sha256,
        "scheduler_configuration_sha256": config["scheduler_configuration_sha256"],
        "image_uri": image_uri,
        "service_account": entry["service_account"],
    }


def _validated_invocation(invocation):
    if type(invocation) is not dict or set(invocation) != _INVOCATION_KEYS:
        _refuse("invocation_fields_invalid")
    if any(not _text(value) for value in invocation.values()):
        _refuse("invocation_fields_invalid")
    if not _EXECUTION_ID.fullmatch(invocation["request_id"]):
        _refuse("invocation_fields_invalid")
    if invocation["mode"] not in MODES:
        _refuse("invocation_mode_invalid")
    if (
        invocation["job_resource"] != DAILY_JOB_RESOURCE
        or invocation["job_name"] != DAILY_JOB_NAME
    ):
        _refuse("invocation_target_forbidden")
    if (
        invocation["execution_name"]
        != f"{DAILY_JOB_NAME}/executions/{invocation['request_id']}"
        or invocation["credential_source"] != "attached_service_account"
        or invocation["principal"] != invocation["service_account"]
    ):
        _refuse("invocation_fields_invalid")
    return invocation


def _run_manifest_from(run_manifest_loader, invocation):
    run_manifest = run_manifest_loader(invocation)
    if (
        type(run_manifest) is not dict
        or not _text(run_manifest.get("operation"))
        or not isinstance(run_manifest.get("manifest_sha256"), str)
        or not _HEX_64.fullmatch(run_manifest["manifest_sha256"])
    ):
        _refuse("run_manifest_invalid")
    return run_manifest


def run_managed_mode(
    invocation: dict,
    *,
    authority,
    metadata_reader=None,
    run_manifest_loader=None,
    handoff=None,
    daily=None,
) -> dict:
    invocation = _validated_invocation(invocation)
    if invocation["mode"] == "verify-runtime":
        if not callable(metadata_reader):
            _refuse("metadata_reader_unavailable")
        metadata = metadata_reader(invocation)
        if type(metadata) is not dict:
            _refuse("metadata_invalid")
        return {
            **invocation,
            "status": "runtime_verified",
            "launched": False,
            "metadata": metadata,
        }
    if not callable(run_manifest_loader) or not callable(handoff):
        return _run_daily_cycle(invocation, authority=authority, daily=daily)
    existing = authority.read_operation(invocation["request_id"])
    if existing is not None:
        if (
            type(existing) is not dict
            or existing.get("usage") != "known"
            or existing.get("state") not in _TERMINAL_STATES
        ):
            _refuse("operation_usage_unknown")
        return {
            "mode": "daily",
            "status": "duplicate_trigger",
            "launched": False,
            "duplicate": True,
            "request_id": invocation["request_id"],
            "existing_operation": existing,
        }
    unresolved = authority.unresolved_operations(invocation["job_resource"])
    if type(unresolved) is not list or unresolved:
        _refuse("operation_unresolved")
    run_manifest = _run_manifest_from(run_manifest_loader, invocation)
    issued = authority.reserve(invocation, run_manifest)
    consumption = authority.consume(issued)
    result = handoff(invocation, run_manifest, consumption)
    return {
        "mode": "daily",
        "status": "handed_off",
        "launched": True,
        "request_id": invocation["request_id"],
        "operation": run_manifest["operation"],
        "run_manifest_sha256": run_manifest["manifest_sha256"],
        "consumption": consumption,
        "handoff": result,
    }


def grant_pointer_name(image_digest, sequence) -> str:
    """The object name of one owner pointer: per image digest, numbered from 1."""
    if not isinstance(image_digest, str) or not _IMAGE_DIGEST.fullmatch(image_digest):
        _refuse("grant_pointer_invalid")
    if type(sequence) is not int or not 1 <= sequence <= MAX_GRANT_POINTERS:
        _refuse("grant_pointer_invalid")
    return (
        f"{GRANT_POINTER_PREFIX}/{image_digest[len('sha256:') :]}/{sequence:06d}.json"
    )


def grant_pointer_bytes(
    *, image_digest, sequence, recurring_grant_digest, approval_phrase_sha256
) -> bytes:
    """The exact canonical bytes of one pointer; the only bytes a reader accepts."""
    grant_pointer_name(image_digest, sequence)
    for value in (recurring_grant_digest, approval_phrase_sha256):
        if not isinstance(value, str) or not _HEX_64.fullmatch(value):
            _refuse("grant_pointer_invalid")
    return canonical_bytes(
        {
            "approval_phrase_sha256": approval_phrase_sha256,
            "contract_version": GRANT_POINTER_CONTRACT,
            "image_digest": image_digest,
            "job_resource": DAILY_JOB_RESOURCE,
            "recurring_grant_digest": recurring_grant_digest,
            "sequence": sequence,
        }
    )


def _pointer_pins(found, image_digest, sequence):
    if type(found) is not tuple or len(found) != 2 or type(found[0]) is not bytes:
        _refuse("grant_pointer_invalid")
    raw = found[0]
    document = _parse_canonical(raw, "grant_pointer_invalid")
    pins = {name: document.get(name) for name in GRANT_POINTER_FIELDS}
    expected = grant_pointer_bytes(image_digest=image_digest, sequence=sequence, **pins)
    if raw != expected:
        _refuse("grant_pointer_invalid")
    return pins


def read_grant_pointer(object_client, image_digest) -> dict:
    """The grant pins the owner's latest pointer names for this image.

    Pointers are created once under a generation match of zero and never replaced, so
    a new grant for the same image is a new pointer at the next number. Numbers are
    read from 1 until the first absent one, at most ``MAX_GRANT_POINTERS``, and every
    pointer read must be the exact canonical document for this job, this image and its
    own number, or the read refuses ``grant_pointer_invalid``. No pointer refuses
    ``grant_pointer_missing``. The pins select a grant; they approve nothing. The
    grant loader still reads the approval through the durable reader.
    """
    grant_pointer_name(image_digest, 1)
    pins = None
    for sequence in range(1, MAX_GRANT_POINTERS + 1):
        found = object_client.read(grant_pointer_name(image_digest, sequence))
        if found is None:
            break
        pins = _pointer_pins(found, image_digest, sequence)
    if pins is None:
        _refuse("grant_pointer_missing")
    return pins


def _image_digest(image_uri):
    return image_uri.rsplit("@", 1)[-1] if isinstance(image_uri, str) else None


def observation_cutoff(now) -> datetime:
    """The latest closed UTC cutoff at or before ``now``: that day's 00:00:00 UTC."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _refuse("daily_clock_invalid")
    current = now.astimezone(UTC)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def unavailable_stage_handlers(invocation, profile, timeline):
    """The seam the stage adapters fill; on its own no handler exists and nothing is paid."""
    return


def _kernel_profile(profile):
    return {name: profile[name] for name in KERNEL_PROFILE_FIELDS}


def native_stage_handlers(
    invocation,
    profile,
    timeline,
    *,
    engine=None,
    object_client=None,
    warehouse=None,
    now=None,
    build=None,
):
    """The daily stage handlers over the engine's native clients.

    The wiring binds its own engine, object client, warehouse and clock through
    ``_daily_wiring`` so the kernel and the handlers share them; called bare, the
    runtime's own are constructed. The engine factory leaves capture, composer,
    persist and certifier unbound, and its guard refuses those stages before any
    native call, as a retry safe failure naming the code. ``build`` is the
    wiring's build fact for the exact collection authority; its image must be
    the job's own image, which the invocation already carries, and without it
    the collect stage refuses before dispatch.
    """
    engine = engine_daily_adapter() if engine is None else engine
    if build is not None and (
        type(build) is not dict or build.get("image_uri") != invocation["image_uri"]
    ):
        _refuse("build_image_mismatch")
    clients = engine.daily_native_clients.build_native_stage_clients(
        engine=engine,
        profile=_kernel_profile(_validated_daily_profile(profile)),
        environment=DAILY_ENVIRONMENT,
        object_client=_native_object_client(engine)
        if object_client is None
        else object_client,
        warehouse=_native_warehouse() if warehouse is None else warehouse,
        now=(lambda: datetime.now(UTC)) if now is None else now,
        build=build,
        compose_binding=engine.daily_native_clients.native_compose_binding(),
    )
    return engine.daily_native_clients.guarded_stage_handlers(
        clients, profile=_kernel_profile(profile)
    )


def _daily_wiring(daily):
    if type(daily) is not dict or not _DAILY_WIRING_KEYS <= set(daily):
        _refuse("daily_wiring_unavailable")
    if not all(
        callable(daily[name]) for name in ("grant_loader", "stage_handlers", "now")
    ):
        _refuse("daily_wiring_unavailable")
    engine = daily["engine"]
    if any(getattr(engine, name, None) is None for name in _DAILY_MODULES):
        _refuse("daily_wiring_unavailable")
    if daily["stage_handlers"] is native_stage_handlers:
        # The native seam binds over this wiring's own pieces; an explicit
        # unavailable seam stays unavailable and nothing is paid.
        return {
            **daily,
            "stage_handlers": functools.partial(
                native_stage_handlers,
                engine=engine,
                object_client=daily["object_client"],
                warehouse=daily.get("warehouse"),
                now=daily["now"],
                build=daily.get("build"),
            ),
        }
    return daily


def daily_plan(*, daily, image_uri) -> dict:
    """Report the daily binding for one image without dispatching anything.

    The daily profile digest, the slot operation id of the next due cutoff, the
    release profile the compose and release stages would bind, and each client as
    bound or unbound with its refusal code. Names only; no credential or value. The
    profile is the one the cycle would run for that image: the image profile joined
    with the pins of the owner's pointer, so a missing pointer refuses here too.
    """
    wiring = _daily_wiring(daily)
    engine = wiring["engine"]
    image_profile = _validated_image_profile(wiring["profile"])
    environment = wiring["environment"]
    if environment != DAILY_ENVIRONMENT:
        _refuse("daily_wiring_unavailable")
    pins = read_grant_pointer(wiring["object_client"], _image_digest(image_uri))
    profile = _kernel_profile(_validated_daily_profile({**image_profile, **pins}))
    cutoff = observation_cutoff(wiring["now"]())
    clients = engine.daily_native_clients.build_native_stage_clients(
        engine=engine,
        profile=profile,
        environment=environment,
        object_client=wiring["object_client"],
        warehouse=wiring["warehouse"] if "warehouse" in wiring else _native_warehouse(),
        now=wiring["now"],
        build=wiring.get("build"),
        compose_binding=engine.daily_native_clients.native_compose_binding(),
    )
    described = engine.daily_native_clients.describe_stage_clients(clients)
    release_profile = clients["release_profile"]
    return {
        "mode": "daily-plan",
        "launched": False,
        "environment": environment,
        "profile_digest": canonical_sha256(profile),
        "cutoff_utc": cutoff.isoformat(),
        "operation_id": engine.daily_store.slot_operation_id(
            environment=environment,
            source_policy_digest=profile["source_policy_digest"],
            cutoff_utc=cutoff,
        ),
        "release_profile": {
            "run_id": release_profile.run_id,
            "cutoff": release_profile.cutoff,
            "is_replay": release_profile.is_replay,
            "independence_policy": release_profile.independence_policy,
        },
        "clients": described,
        "bound": [name for name, entry in described.items() if entry["bound"]],
        "unbound": {
            name: entry["code"]
            for name, entry in described.items()
            if not entry["bound"]
        },
    }


def _instant(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        _refuse("daily_clock_invalid")
    return value.astimezone(UTC).isoformat()


def _active_generation_manifest_digest(engine):
    """The resource manifest digest of the engine's active execution generation.

    The approve routine and the durable approval reader admit only a grant naming the
    manifest of the active generation row, which the engine's active pair is. The daily
    cycle checks the grant against the same digest, so one grant passes both sides. The
    invocation and the daily profile stay bound to the ops deploy manifest, which guards
    the job's own resources.
    """
    pair = getattr(engine.execution_generations, "ACTIVE_GENERATION_PAIR", None)
    if (
        type(pair) is not tuple
        or len(pair) != 2
        or not isinstance(pair[1], str)
        or not _HEX_64.fullmatch(pair[1])
    ):
        _refuse("daily_wiring_unavailable")
    return pair[1]


def _run_daily_cycle(invocation, *, authority, daily) -> dict:
    wiring = _daily_wiring(daily)
    engine = wiring["engine"]
    image_profile = _validated_image_profile(wiring["profile"])
    if (
        image_profile["resource_manifest_digest"]
        != invocation["resource_manifest_digest"]
    ):
        _refuse("daily_profile_manifest_mismatch")
    grant_manifest_digest = _active_generation_manifest_digest(engine)
    now = wiring["now"]()
    cutoff = observation_cutoff(now)
    # The image digest comes from the execution readback the invocation was built
    # from, never from the environment; the pointer is read for exactly that image.
    image_digest = _image_digest(invocation["image_uri"])
    pins = read_grant_pointer(wiring["object_client"], image_digest)
    profile = _validated_daily_profile({**image_profile, **pins})
    grant = wiring["grant_loader"](dict(pins))
    if grant is None:
        _refuse("grant_missing")
    grant_digest = engine.recurring_grant.grant_digest(grant)
    if grant_digest != profile["recurring_grant_digest"]:
        _refuse("grant_digest_mismatch")
    checked_grant = engine.recurring_grant.validate_recurring_grant(grant)
    if checked_grant["revocation_state"] != "active":
        _refuse("grant_revoked")
    # The authority refuses this at its first reservation too; refusing here keeps a
    # pointer to a grant for another image from claiming the slot or writing anything.
    if image_digest not in checked_grant["permitted_image_digests"]:
        _refuse("recurring_grant_image_forbidden")
    timeline = {"snapshot_as_of": None, "capture_available_at": None}
    handlers = wiring["stage_handlers"](invocation, dict(profile), timeline)
    if handlers is None:
        _refuse("stage_adapter_unavailable")
    if type(handlers) is not dict or not all(
        callable(value) for value in handlers.values()
    ):
        _refuse("stage_adapter_unavailable")
    environment = wiring["environment"]
    if environment != DAILY_ENVIRONMENT:
        _refuse("daily_wiring_unavailable")
    describe = getattr(authority, "describe", None)
    if not callable(describe):
        _refuse("operation_unbound")
    # The grant names its operations in the C03 vocabulary; the registry's daily row
    # binds the same five stages under registry names. The grant module's one map is
    # the meeting point here and at the reserve, so describe and reserve ask the
    # registry for the same name.
    registry_operations = engine.recurring_grant.V1_REGISTRY_OPERATIONS
    for operation in checked_grant["allowed_operations"]:
        registry_operation = registry_operations.get(operation)
        if registry_operation is None or describe(registry_operation) is None:
            _refuse("operation_unbound")
    operation_id = engine.daily_store.slot_operation_id(
        environment=environment,
        source_policy_digest=profile["source_policy_digest"],
        cutoff_utc=cutoff,
    )
    kernel_profile = _kernel_profile(profile)
    store = engine.daily_store.DailyStore(
        wiring["object_client"], owner=invocation["request_id"], now=wiring["now"]
    )
    ledger = engine.daily_authority.ObjectAuthorityLedger(wiring["object_client"])
    daily_authority = engine.daily_authority.DailyAuthority(
        grant=grant,
        profile=kernel_profile,
        principal=invocation["principal"],
        image_digest=image_digest,
        resource_manifest_digest=grant_manifest_digest,
        reservation=authority,
        ledger=ledger,
        fence=lambda: store.current_lease(operation_id),
        now=wiring["now"],
        release_delegation=wiring.get("release_delegation"),
    )
    dispatched = []
    instants = {"collection_started_at": None, "collection_completed_at": None}

    def traced(stage, handler):
        def run(*, manifest, authority_receipt):
            dispatched.append(stage)
            noted = {"observation_window_end": cutoff.isoformat()}
            if stage == "collect":
                instants["collection_started_at"] = _instant(wiring["now"]())
                noted["collection_started_at"] = instants["collection_started_at"]
            store.note_timeline(operation_id, noted)
            result = handler(manifest=manifest, authority_receipt=authority_receipt)
            if (
                stage == "collect"
                and type(result) is dict
                and result.get("state") == "succeeded"
            ):
                instants["collection_completed_at"] = _instant(wiring["now"]())
                store.note_timeline(
                    operation_id,
                    {"collection_completed_at": instants["collection_completed_at"]},
                )
            if stage == "capture":
                store.note_timeline(
                    operation_id,
                    {
                        "snapshot_as_of": timeline["snapshot_as_of"],
                        "capture_available_at": timeline["capture_available_at"],
                    },
                )
            return result

        return run

    stages = {stage: traced(stage, handler) for stage, handler in handlers.items()}
    result = engine.daily_cycle.run_validated_daily_cycle(
        operation_id=operation_id,
        cutoff_utc=cutoff,
        now=now,
        profile=kernel_profile,
        authority=daily_authority,
        store=store,
        stages=stages,
    )
    return {
        "mode": "daily",
        "status": result["state"],
        "launched": bool(dispatched),
        "request_id": invocation["request_id"],
        "operation_id": operation_id,
        "cycle": dict(result),
        "stages_dispatched": list(dispatched),
        "grant_digest": grant_digest,
        "observation_window_end": cutoff.isoformat(),
        "collection_started_at": instants["collection_started_at"],
        "collection_completed_at": instants["collection_completed_at"],
        "snapshot_as_of": timeline["snapshot_as_of"],
        "capture_available_at": timeline["capture_available_at"],
        "timeline": store.read_timeline(operation_id),
    }


class _DailyEngine:
    def __init__(self, modules):
        for name, module in modules.items():
            setattr(self, name, module)


def engine_daily_adapter(*, importer=importlib.import_module):
    root = Path(__file__).resolve().parents[2]
    for entry in (str(root / "engine"), str(root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    try:
        modules = {
            name: importer(f"src.analysis.open_intelligence.{name}")
            for name in _DAILY_MODULES
        }
    except ImportError as exc:
        raise ValueError("daily_adapter_unavailable") from exc
    return _DailyEngine(modules)


def _native_object_client(engine):
    from google.cloud import storage

    client = storage.Client(project=PROJECT)
    return engine.daily_store.GcsObjectClient(
        client.bucket(engine.daily_store.EVIDENCE_BUCKET)
    )


def _native_warehouse():
    from google.cloud import bigquery

    return bigquery.Client(project=PROJECT)


def _native_approval_reader():
    """The existing durable approval reader, keyed by the grant digest as manifest digest."""

    def read(digest):
        module = importlib.import_module(
            "src.analysis.open_intelligence.execution_approval"
        )
        try:
            approval = module._default_approval_reader(
                digest, version=module._APPROVAL_VERSION_V3, mode="new_consume"
            )
        except ValueError:
            return None
        return {
            "approved_by": approval.approved_by,
            "approved_at": approval.approved_at,
            "approval_phrase_sha256": approval.approval_phrase_sha256,
            "manifest_sha256": approval.manifest_sha256,
        }

    return read


def _native_grant_loader(engine, object_client, approval_reader):
    """Grant terms from the evidence bucket; approval only through the durable reader.

    The loader is asked with the two pins of the owner's pointer. The executing
    principal writes other prefixes of the bucket but cannot create, replace or delete
    anything under the grants prefix, so the grant object, its revocation marker and
    the pointer are the owner's. The approval is read only through the durable reader
    and must carry exactly the pointer's phrase digest, the grant's issuing principal
    and the grant digest. A create only revocation marker beside the grant object
    revokes it.
    """
    required = {"approved_by", "approval_phrase_sha256", "manifest_sha256"}

    def load(pins):
        if (
            type(pins) is not dict
            or set(pins) != set(GRANT_POINTER_FIELDS)
            or any(
                not isinstance(pins[name], str) or not _HEX_64.fullmatch(pins[name])
                for name in GRANT_POINTER_FIELDS
            )
        ):
            _refuse("grant_record_invalid")
        digest = pins["recurring_grant_digest"]
        pinned = pins["approval_phrase_sha256"]
        found = object_client.read(f"{GRANT_PREFIX}/{digest}.json")
        if found is None:
            return None
        raw, _generation = found
        record = _parse_canonical(raw, "grant_record_invalid")
        if set(record) != {"grant"}:
            _refuse("grant_record_invalid")
        grant = engine.recurring_grant.validate_recurring_grant(record["grant"])
        if engine.recurring_grant.grant_digest(grant) != digest:
            _refuse("grant_record_invalid")
        approval = approval_reader(digest)
        if approval is None:
            return None
        if (
            type(approval) is not dict
            or not required <= set(approval)
            or approval["approval_phrase_sha256"] != pinned
            or approval["approved_by"] != grant["issuing_principal"]
            or approval["manifest_sha256"] != digest
        ):
            _refuse("grant_approval_mismatch")
        if object_client.read(f"{GRANT_PREFIX}/{digest}/revocation.json") is not None:
            grant["revocation_state"] = "revoked"
        return grant

    return load


def _build_source_sha(record):
    """The one commit a Cloud Build record resolves to, or None when it names none or several."""
    found = set()
    for path in _BUILD_SOURCE_REVISIONS:
        value = record
        for key in path:
            value = value.get(key) if type(value) is dict else None
        if isinstance(value, str) and _GIT_SHA.fullmatch(value):
            found.add(value)
    return found.pop() if len(found) == 1 else None


def build_fact_from_records(image_uri, records, *, repository) -> dict | None:
    """The build fact from the Cloud Build records that built the running image.

    A record counts only by the rule the deploy plan bound the image with: a successful
    build of this project whose single image is the running image in the job's own
    repository. The commit is the one the record resolves to; a counting record without a
    single commit refuses, no counting record leaves the build unbound (None, so the
    collect stage refuses ``collection_build_unbound`` before dispatch), and two counting
    records that resolve to different commits refuse rather than pick one.
    """
    if not isinstance(image_uri, str) or "@sha256:" not in image_uri:
        _refuse("build_provenance_invalid")
    if type(records) is not list or not isinstance(repository, str) or not repository:
        _refuse("build_provenance_invalid")
    digest = image_uri.rsplit("@", 1)[1]
    commits = set()
    for record in records:
        if (
            type(record) is not dict
            or record.get("status") != "SUCCESS"
            or record.get("projectId") != PROJECT
            or not isinstance(record.get("name"), str)
            or _BUILD_RESOURCE.fullmatch(record["name"]) is None
        ):
            continue
        results = record.get("results")
        images = results.get("images") if type(results) is dict else None
        if type(images) is not list or len(images) != 1 or type(images[0]) is not dict:
            continue
        name = images[0].get("name")
        if images[0].get("digest") != digest or not isinstance(name, str):
            continue
        if not (
            name == repository or name.startswith((repository + ":", repository + "@"))
        ):
            continue
        commit = _build_source_sha(record)
        if commit is None:
            _refuse("build_provenance_invalid")
        commits.add(commit)
    if not commits:
        return None
    if len(commits) != 1:
        _refuse("build_provenance_ambiguous")
    return {"source_sha": commits.pop(), "image_uri": f"{repository}@{digest}"}


def _native_build_provenance_reader(image_uri):
    """Every Cloud Build record of this project whose image digest is the running one.

    Read through the engine authority's own runtime credentials and session, the same
    transport its build provenance receipt is read with.
    """
    module = importlib.import_module(
        "src.analysis.open_intelligence.execution_approval"
    )
    session = module._runtime_session(module._runtime_credentials())
    digest = image_uri.rsplit("@", 1)[1]
    records, token = [], None
    while True:
        params = {"filter": f'results.images.digest="{digest}"', "pageSize": 100}
        if token:
            params["pageToken"] = token
        response = session.get(BUILD_LIST_URI, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        records.extend(payload.get("builds", []))
        token = payload.get("nextPageToken")
        if not token:
            return records


def native_build_fact(image_uri, *, repository, resources, reader=None) -> dict | None:
    """The runtime's build fact for the running image, or None while no build record binds it.

    The Cloud Build read is asked of the reviewed guard before any request, so a
    manifest without the read only builds collection row refuses and nothing is read.
    """
    assert_allowed(BUILDS_RESOURCE, "read", resources)
    read = _native_build_provenance_reader if reader is None else reader
    try:
        records = read(image_uri)
    except Exception as exc:
        raise ValueError("build_provenance_unavailable") from exc
    return build_fact_from_records(image_uri, records, repository=repository)


def default_daily_wiring(config, *, image_uri=None, build_reader=None, resources=None):
    """The runtime's own daily wiring; with the invocation's image, its build fact too."""
    engine = engine_daily_adapter()
    object_client = _native_object_client(engine)
    wiring = {
        "engine": engine,
        "profile": config["daily_profile"],
        "grant_loader": _native_grant_loader(
            engine, object_client, _native_approval_reader()
        ),
        "object_client": object_client,
        "warehouse": _native_warehouse(),
        "stage_handlers": native_stage_handlers,
        "now": lambda: datetime.now(UTC),
        "environment": DAILY_ENVIRONMENT,
    }
    if image_uri is not None:
        entry = config["jobs"][config["runtime_job"]]
        wiring["build"] = native_build_fact(
            image_uri,
            repository=entry["image_repository"],
            resources=resources,
            reader=build_reader,
        )
    return wiring


class _EngineAuthority:
    def __init__(self, module):
        self._module = module

    def read_operation(self, request_id):
        _refuse("operation_state_unavailable")

    def unresolved_operations(self, job_resource):
        _refuse("operation_state_unavailable")

    def describe(self, operation):
        generations = getattr(self._module, "execution_generations", None)
        origin_for = getattr(self._module, "_v2_origin_for_operation", None)
        if generations is None or not callable(origin_for):
            return None
        try:
            registry = generations.active_generation().registry
            origin = origin_for(operation, mode="new_consume", registry=registry)
            if origin.operation_bindings[operation].job_resource != DAILY_JOB_NAME:
                return None
        except (ValueError, LookupError, AttributeError, TypeError):
            return None
        return {
            "operation": operation,
            "contract_sha256": getattr(origin, "contract_sha256", None),
        }

    def reserve(self, invocation, run_manifest):
        if run_manifest["operation"] == "source_snapshot_capture":
            return self._module._load_source_snapshot_authority(mode="new_consume")
        return self._module._load_execution_authority(
            run_manifest["operation"], mode="new_consume"
        )

    def consume(self, authority):
        if getattr(authority, "operation", None) == "source_snapshot_capture":
            return self._module._consume_source_snapshot_authority(authority)
        return self._module._consume_execution_authority(authority)


def engine_authority_adapter(*, importer=importlib.import_module):
    root = Path(__file__).resolve().parents[2]
    for entry in (str(root / "engine"), str(root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    try:
        module = importer("src.analysis.open_intelligence.execution_approval")
    except ImportError as exc:
        raise ValueError("authority_adapter_unavailable") from exc
    return _EngineAuthority(module)


def _native_credential_discovery():
    import google.auth
    from google.auth import compute_engine
    from google.auth.transport.requests import Request
    from google.oauth2 import credentials as user_credentials
    from google.oauth2 import service_account

    credentials, _project = google.auth.default(scopes=(CLOUD_PLATFORM_SCOPE,))
    if isinstance(credentials, compute_engine.Credentials):
        credentials.refresh(Request())
        return {
            "kind": "attached_service_account",
            "source": "metadata_server",
            "principal": credentials.service_account_email,
        }
    if isinstance(credentials, user_credentials.Credentials):
        return {
            "kind": "user_oauth",
            "source": "application_default_credentials",
            "principal": "",
        }
    if isinstance(credentials, service_account.Credentials):
        return {
            "kind": "service_account_key_file",
            "source": "GOOGLE_APPLICATION_CREDENTIALS",
            "principal": credentials.service_account_email,
        }
    return {"kind": type(credentials).__name__, "source": "unknown", "principal": ""}


def _native_execution_reader(execution_name):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _project = google.auth.default(scopes=(CLOUD_PLATFORM_SCOPE,))
    session = AuthorizedSession(credentials)
    readback = {}
    for key, name in (("execution", execution_name), ("job", DAILY_JOB_NAME)):
        response = session.get(f"https://run.googleapis.com/v2/{name}", timeout=30)
        if response.status_code == 403:
            raise PermissionError(name)
        response.raise_for_status()
        readback[key] = response.json()
    return readback


def semantic_canary_script(root: Path) -> Path:
    """The QA semantic canary in the repository layout or the engine image layout."""
    for candidate in (
        root / "engine" / "scripts" / "staging" / "run_42_semantic_canaries.py",
        root / "scripts" / "staging" / "run_42_semantic_canaries.py",
    ):
        if candidate.is_file():
            return candidate
    raise ValueError("semantic_canary_unavailable")


def run_semantic_canary_check(
    root: Path, manifest, *, today=None, stderr=None, drill_break=None
) -> str:
    """Dry run the QA semantic canary; a failure writes one ERROR line for the alert.

    The dry run evaluates the seven cases through the engine path only, constructs
    no client and writes nothing, so it needs no identity beyond the job's own. A
    canary that cannot run at all writes the same alarm message with a named error.
    """
    out = sys.stderr if stderr is None else stderr
    try:
        script = semantic_canary_script(root)
        spec = importlib.util.spec_from_file_location(
            "run_42_semantic_canaries", script
        )
        if spec is None or spec.loader is None:
            raise ValueError("semantic_canary_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run_date = today if today is not None else module._utc_today()
        result = module.run_semantic_canaries(
            run_date, apply=False, manifest=manifest, drill_break=drill_break
        )
    except Exception as exc:  # noqa: BLE001 - any failure to run must still raise the alarm
        out.write(
            json.dumps(
                {
                    "severity": "ERROR",
                    "message": "qa_semantic_canary_failed",
                    "error": "qa_semantic_canary_unavailable",
                    "detail": type(exc).__name__,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        return "failed"
    if result["status"] != "passed":
        out.write(module.render_alarm(result) + "\n")
        return "failed"
    return "passed"


def main(argv=None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        if arguments:
            _refuse("runtime_override_forbidden")
        config = load_runtime_configuration(
            root / "infra" / "runtime" / "daily-staging.json"
        )
        load_scheduler_configuration(
            root / "infra" / "runtime" / "scheduler-staging.json",
            expected_sha256=config["scheduler_configuration_sha256"],
        )
        resources = load_resource_manifest(
            root / "ops" / "deploy" / "resource_manifest.json",
            expected_sha256=config["resource_manifest_sha256"],
        )
        invocation = build_invocation(
            config,
            resources,
            os.environ.get("CLOUD_RUN_EXECUTION"),
            execution_reader=_native_execution_reader,
            credential_discovery=_native_credential_discovery,
            environment=os.environ,
        )
        result = run_managed_mode(
            invocation,
            authority=engine_authority_adapter()
            if invocation["mode"] == "daily"
            else None,
            daily=default_daily_wiring(
                config, image_uri=invocation["image_uri"], resources=resources
            )
            if invocation["mode"] == "daily"
            else None,
            metadata_reader=lambda current: {
                "project": resources["project"],
                "region": resources["region"],
                "identities": resources["identities"],
                "resource_manifest_digest": current["resource_manifest_digest"],
            },
        )
    except ValueError as exc:
        sys.stdout.write(
            json.dumps({"status": "refused", "error": str(exc)}, sort_keys=True) + "\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    if run_semantic_canary_check(root, resources) != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
