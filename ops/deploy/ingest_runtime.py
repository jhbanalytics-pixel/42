"""The ingest job definition: infra/runtime/ingest-staging.json.

The ingest job intelligence-42-ingest-staging runs the collection entry point
(python -m scripts.staging.collect_42_sources) as the ingest identity, the only
identity with write on the staging source dataset. It has its own file and its
own contract, so the daily job's configuration digest, which its execution
annotation and the recurring grant bind, does not move when it is defined.

load_ingest_configuration validates that file in full. load_job_configuration
chooses between it and the daily file by the contract the file declares, and is
what ops/deploy/runtime_jobs.py plans from. ingest_build_environment gives the
two variables the job creation step binds from the build it deploys. The daily
loader in ops/runners/managed_runtime.py is imported and not changed.
"""

from ops.runners.managed_runtime import (
    _HEX_64,
    _IMAGE_REPOSITORY,
    _JOB_ENTRY_KEYS,
    DAILY_ENVIRONMENT,
    PROJECT,
    REGION,
    RUNTIME_COMMAND,
    _build_source_sha,
    _parse_canonical,
    _read_bytes,
    _refuse,
    _validated_jobs_configuration,
    load_runtime_configuration,
)

INGEST_JOB_ID = "intelligence-42-ingest-staging"
INGEST_CONTRACT = "42_managed_runtime_ingest_v1"
INGEST_SERVICE_ACCOUNT = f"intelligence-42-ingest@{PROJECT}.iam.gserviceaccount.com"
INGEST_ARGS = ["-m", "scripts.staging.collect_42_sources"]
# Each declared variable with its one admitted value, or None for a sha256 digest.
INGEST_ENVIRONMENT = (
    ("TRENDS_ENV", DAILY_ENVIRONMENT),
    ("BIGQUERY_DATASET", "intelligence_42_sources_staging"),
    ("GCP_PROJECT", PROJECT),
    ("COLLECTION_POLICY_SHA256", None),
    ("COLLECTION_PROFILE_SHA256", None),
)
# The collect dispatch waits for the ingest receipt inside the daily execution's hour.
INGEST_MAX_TIMEOUT_SECONDS = 3000
_INGEST_KEYS = {
    "contract_version",
    "credential_source",
    "jobs",
    "location",
    "project",
    "resource_manifest_sha256",
}
_CODE = "runtime_configuration_invalid"


def _validate_ingest_entry(entry):
    if type(entry) is not dict or set(entry) != _JOB_ENTRY_KEYS:
        _refuse(_CODE)
    if (
        entry["job_resource"]
        != f"//run.googleapis.com/projects/{PROJECT}/locations/{REGION}/jobs/{INGEST_JOB_ID}"
        or entry["service_account"] != INGEST_SERVICE_ACCOUNT
        or not isinstance(entry["image_repository"], str)
        or not _IMAGE_REPOSITORY.fullmatch(entry["image_repository"])
        or entry["mode"] is not None
        or entry["command"] != RUNTIME_COMMAND
        or entry["args"] != INGEST_ARGS
    ):
        _refuse(_CODE)
    env = entry["env"]
    if (
        type(env) is not list
        or any(type(item) is not dict or set(item) != {"name", "value"} for item in env)
        or [item["name"] for item in env]
        != [name for name, _rule in INGEST_ENVIRONMENT]
    ):
        _refuse(_CODE)
    for item, (_name, rule) in zip(env, INGEST_ENVIRONMENT):
        value = item["value"]
        if not isinstance(value, str) or not (
            _HEX_64.fullmatch(value) if rule is None else value == rule
        ):
            _refuse(_CODE)
    for name in ("max_retries", "parallelism", "task_count", "timeout_seconds"):
        if type(entry[name]) is not int:
            _refuse(_CODE)
    if (
        entry["max_retries"] != 0
        or entry["parallelism"] != 1
        or entry["task_count"] != 1
        or not 0 < entry["timeout_seconds"] <= INGEST_MAX_TIMEOUT_SECONDS
    ):
        _refuse(_CODE)


def _validated_ingest_configuration(config):
    if type(config) is not dict or set(config) != _INGEST_KEYS:
        _refuse(_CODE)
    if (
        config["contract_version"] != INGEST_CONTRACT
        or config["project"] != PROJECT
        or config["location"] != REGION
        or config["credential_source"] != "attached_service_account"
        or not isinstance(config["resource_manifest_sha256"], str)
        or not _HEX_64.fullmatch(config["resource_manifest_sha256"])
    ):
        _refuse(_CODE)
    jobs = config["jobs"]
    if type(jobs) is not dict or set(jobs) != {INGEST_JOB_ID}:
        _refuse(_CODE)
    _validate_ingest_entry(jobs[INGEST_JOB_ID])
    return config


def load_ingest_configuration(path) -> dict:
    """The ingest job file, canonical and closed, or runtime_configuration_invalid.

    Its environment is closed: the staging target, the project, and the policy and
    profile digests the entry point checks against what it reconciles. The source
    commit and the image the collection receipt stamps are not declared here; the job
    creation step binds them from the build it deploys.
    """
    raw = _read_bytes(path, _CODE)
    return _validated_ingest_configuration(_parse_canonical(raw, _CODE))


def load_job_configuration(path) -> dict:
    """Either reviewed job file, chosen by the contract it declares, validated in full."""
    raw = _read_bytes(path, _CODE)
    config = _parse_canonical(raw, _CODE)
    if config.get("contract_version") == INGEST_CONTRACT:
        return _validated_ingest_configuration(config)
    return _validated_jobs_configuration(config)


def ingest_build_environment(config, job_id, *, image, build) -> list:
    """The variables the job creation step binds from the build, for the ingest job only.

    The collection receipt stamps the source commit and the image the run executed,
    and the dispatch compares both with the caller's build fact; so the commit is the
    one the build record resolves to, read the way the runtime reads its own build,
    and the image is the pinned image the plan deploys. Every other job binds nothing.
    A build record that resolves to no single commit refuses build_source_unbound.
    """
    if config.get("contract_version") != INGEST_CONTRACT or job_id != INGEST_JOB_ID:
        return []
    source_sha = _build_source_sha(build) if type(build) is dict else None
    if source_sha is None:
        _refuse("build_source_unbound")
    return [
        {"name": "COLLECTION_SOURCE_SHA", "value": source_sha},
        {"name": "COLLECTION_IMAGE_URI", "value": image},
    ]


__all__ = [
    "INGEST_CONTRACT",
    "INGEST_JOB_ID",
    "ingest_build_environment",
    "load_ingest_configuration",
    "load_job_configuration",
    "load_runtime_configuration",
]
