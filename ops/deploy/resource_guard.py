import hashlib
import json
import os
import re
import stat
from pathlib import Path

_MAX_MANIFEST_BYTES = 1024 * 1024
_ACTIONS = {"deploy", "invoke", "read", "write"}
_ROOT_KEYS = {
    "bigquery_location",
    "contract_version",
    "identities",
    "origin_registry_sha256",
    "project",
    "project_number",
    "region",
    "resources",
    "review_auth",
}
_REQUIRED_IDENTITIES = {
    "app",
    "build",
    "deploy",
    "freshness",
    "ingestion",
    "orchestration",
    "price_policy",
    "qa",
    "scheduler",
}
_OPTIONAL_IDENTITIES = {
    "execution_bootstrap_migration_apply",
    "execution_brain_read",
    "execution_collection_exposure_issue",
    "execution_daily_composition_apply",
    "execution_legacy_chain_replay",
    "execution_migration_apply",
    "execution_r3_apply",
    "execution_r3_proof_issue",
    "execution_r3_release",
    "execution_source_collection",
    "execution_source_snapshot_capture",
    "execution_wave1_pilot",
}
# The five operations the daily job runs under the orchestration account since
# amendment d, and the three the daily contract revision added (collection, the daily
# compose and the legacy chain replay). Each key, when present, must equal the
# orchestration identity; every other key stays unique across the block.
DAILY_OPERATION_IDENTITIES = (
    "execution_collection_exposure_issue",
    "execution_source_snapshot_capture",
    "execution_r3_apply",
    "execution_r3_proof_issue",
    "execution_r3_release",
    "execution_source_collection",
    "execution_daily_composition_apply",
    "execution_legacy_chain_replay",
)
_DATASET = r"[A-Za-z_][A-Za-z0-9_]{0,1023}"
_BIGQUERY_ROUTINE = r"[A-Za-z_][A-Za-z0-9_]{0,255}"
_CLOUD_RUN_SERVICE = r"[a-z](?:[a-z0-9-]{0,47}[a-z0-9])?"
_LOWER_HYPHEN_63 = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ARTIFACT_REPOSITORY = r"[a-z][a-z0-9-]{2,61}[a-z0-9]"
_SECRET = r"[A-Za-z0-9_-]{1,255}"
_CUSTOM_ROLE = r"[A-Za-z][A-Za-z0-9_.]{2,63}"
# The only BigQuery tables the manifest may name: the three v2 ledger tables
# the serving identity reads (amendment e). Every other table stays unclassified.
LEDGER_TABLES = (
    "open_intelligence_execution_approvals_v2",
    "open_intelligence_execution_consumptions_v2",
    "open_intelligence_execution_results_v2",
)


def _manifest_invalid():
    raise ValueError("resource_manifest_invalid")


def _linked(path):
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _closed_mapping(value, keys):
    if type(value) is not dict or set(value) != keys:
        _manifest_invalid()


def _safe_text(value, *, allow_space=False):
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return allow_space or not any(char.isspace() for char in value)


def _resource_kind(name, *, project, project_number, region):
    if not _safe_text(name) or len(name) > 2048:
        return None
    if any(token in name for token in ("%", "?", "#", "\\", "*")):
        return None
    if not name.startswith("//") or name.endswith("/"):
        return None
    tail = name[2:]
    if "//" in tail or any(segment in {"", ".", ".."} for segment in tail.split("/")):
        return None

    projects = f"(?:{re.escape(project)}|{re.escape(project_number)})"
    location = re.escape(region)
    patterns = (
        (
            "run",
            rf"//run\.googleapis\.com/projects/{projects}/locations/{location}/services/{_CLOUD_RUN_SERVICE}",
        ),
        (
            "run",
            rf"//run\.googleapis\.com/projects/{projects}/locations/{location}/jobs/{_LOWER_HYPHEN_63}",
        ),
        (
            "cloudtasks",
            rf"//cloudtasks\.googleapis\.com/projects/{projects}/locations/{location}/queues/{_LOWER_HYPHEN_63}",
        ),
        (
            "cloudscheduler",
            rf"//cloudscheduler\.googleapis\.com/projects/{projects}/locations/{location}/jobs/{_LOWER_HYPHEN_63}",
        ),
        (
            "artifactregistry",
            rf"//artifactregistry\.googleapis\.com/projects/{projects}/locations/{location}/repositories/{_ARTIFACT_REPOSITORY}",
        ),
        (
            "cloudbuild_parent",
            rf"//cloudbuild\.googleapis\.com/projects/{projects}/locations/{location}",
        ),
        (
            "cloudbuild_builds",
            rf"//cloudbuild\.googleapis\.com/projects/{projects}/locations/{location}/builds",
        ),
        (
            "cloudbuild_connection",
            rf"//cloudbuild\.googleapis\.com/projects/{projects}/locations/{location}/connections/{_LOWER_HYPHEN_63}",
        ),
        (
            "cloudbuild_repository",
            rf"//cloudbuild\.googleapis\.com/projects/{projects}/locations/{location}/connections/{_LOWER_HYPHEN_63}/repositories/{_LOWER_HYPHEN_63}",
        ),
        (
            "bigquery_dataset",
            rf"//bigquery\.googleapis\.com/projects/{projects}/datasets/{_DATASET}",
        ),
        (
            "bigquery_table",
            rf"//bigquery\.googleapis\.com/projects/{re.escape(project)}/datasets/trends_v2_staging_approvals/tables/(?:{'|'.join(LEDGER_TABLES)})",
        ),
        (
            "bigquery_routine",
            rf"//bigquery\.googleapis\.com/projects/{projects}/datasets/{_DATASET}/routines/{_BIGQUERY_ROUTINE}",
        ),
        (
            "secret_container",
            rf"//secretmanager\.googleapis\.com/projects/{projects}/secrets/{_SECRET}",
        ),
        (
            "secret_version",
            rf"//secretmanager\.googleapis\.com/projects/{projects}/secrets/{_SECRET}/versions/[1-9][0-9]*",
        ),
        ("project", rf"//cloudresourcemanager\.googleapis\.com/projects/{projects}"),
        (
            "custom_role",
            rf"//iam\.googleapis\.com/projects/{projects}/roles/{_CUSTOM_ROLE}",
        ),
    )
    for kind, pattern in patterns:
        if re.fullmatch(pattern, name):
            return kind

    email = rf"[a-z][a-z0-9-]{{4,28}}[a-z0-9]@{re.escape(project)}\.iam\.gserviceaccount\.com"
    if re.fullmatch(
        rf"//iam\.googleapis\.com/projects/{projects}/serviceAccounts/{email}", name
    ):
        return "service_account"
    if (
        re.fullmatch(
            r"//storage\.googleapis\.com/projects/_/buckets/"
            r"[a-z0-9](?:[a-z0-9._-]{1,61}[a-z0-9])",
            name,
        )
        and ".." not in name
    ):
        return "bucket"
    return None


def _validate_actions(kind, actions):
    if type(actions) is not list or not actions:
        _manifest_invalid()
    if any(type(action) is not str or action not in _ACTIONS for action in actions):
        _manifest_invalid()
    if actions != sorted(actions) or len(actions) != len(set(actions)):
        _manifest_invalid()
    if kind == "secret_version" and actions != ["read"]:
        _manifest_invalid()
    if kind == "secret_container" and not set(actions) <= {"read", "write"}:
        _manifest_invalid()
    if kind == "cloudbuild_parent" and actions != ["write"]:
        _manifest_invalid()
    # The builds collection is read only: release verify reads a build record
    # there, and a single build is never a manifest row.
    if kind == "cloudbuild_builds" and actions != ["read"]:
        _manifest_invalid()
    # A ledger table row is read only.
    if kind == "bigquery_table" and actions != ["read"]:
        _manifest_invalid()
    if kind == "project" and not set(actions) <= {"read", "write"}:
        _manifest_invalid()


def _validate_review_auth(review_auth):
    keys = {"audience", "enrollment_evidence_sha256", "issuer", "state", "subjects"}
    _closed_mapping(review_auth, keys)
    if review_auth["state"] != "prepared":
        _manifest_invalid()
    if review_auth["issuer"] != "https://accounts.google.com":
        _manifest_invalid()
    audience = review_auth["audience"]
    if audience is not None and not _safe_text(audience, allow_space=True):
        _manifest_invalid()
    if (
        review_auth["subjects"] != []
        or review_auth["enrollment_evidence_sha256"] is not None
    ):
        _manifest_invalid()


def _validate_manifest(manifest):
    _closed_mapping(manifest, _ROOT_KEYS)
    if manifest["contract_version"] != "42_resource_manifest_v1":
        _manifest_invalid()
    if manifest["project"] != "ogilvy-trends-v2":
        _manifest_invalid()
    if (
        type(manifest["project_number"]) is not str
        or manifest["project_number"] != "590353929363"
    ):
        _manifest_invalid()
    if manifest["region"] != "us-central1" or manifest["bigquery_location"] != "US":
        _manifest_invalid()
    registry_digest = manifest["origin_registry_sha256"]
    if type(registry_digest) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", registry_digest
    ):
        _manifest_invalid()

    resources = manifest["resources"]
    if type(resources) is not list or not resources:
        _manifest_invalid()
    names = []
    for row in resources:
        _closed_mapping(row, {"actions", "name"})
        name = row["name"]
        kind = _resource_kind(
            name,
            project=manifest["project"],
            project_number=manifest["project_number"],
            region=manifest["region"],
        )
        if kind is None:
            _manifest_invalid()
        _validate_actions(kind, row["actions"])
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        _manifest_invalid()

    identities = manifest["identities"]
    if type(identities) is not dict:
        _manifest_invalid()
    identity_keys = set(identities)
    if not _REQUIRED_IDENTITIES <= identity_keys:
        _manifest_invalid()
    if not identity_keys <= _REQUIRED_IDENTITIES | _OPTIONAL_IDENTITIES:
        _manifest_invalid()
    identity_values = list(identities.values())
    if any(type(value) is not str for value in identity_values):
        _manifest_invalid()
    for key in DAILY_OPERATION_IDENTITIES:
        if key in identities and identities[key] != identities["orchestration"]:
            _manifest_invalid()
    unique_values = [
        value
        for key, value in identities.items()
        if key not in DAILY_OPERATION_IDENTITIES
    ]
    if len(unique_values) != len(set(unique_values)):
        _manifest_invalid()
    for value in identity_values:
        if (
            _resource_kind(
                value,
                project=manifest["project"],
                project_number=manifest["project_number"],
                region=manifest["region"],
            )
            != "service_account"
        ):
            _manifest_invalid()
    _validate_review_auth(manifest["review_auth"])


def assert_allowed(resource: str, action: str, manifest: dict) -> None:
    _validate_manifest(manifest)
    if type(resource) is not str or type(action) is not str or action not in _ACTIONS:
        raise ValueError("resource_action_forbidden")
    for row in manifest["resources"]:
        if row["name"] == resource and action in row["actions"]:
            return
    raise ValueError("resource_action_forbidden")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _manifest_invalid()
        value[key] = item
    return value


def load_resource_manifest(path, *, expected_sha256: str) -> dict:
    if type(expected_sha256) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        _manifest_invalid()
    try:
        source = Path(path).absolute()
    except (TypeError, ValueError, OSError):
        _manifest_invalid()
    current = source
    while True:
        if _linked(current):
            _manifest_invalid()
        if current.parent == current:
            break
        current = current.parent
    try:
        before = source.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_MANIFEST_BYTES:
            _manifest_invalid()
        with source.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(_MAX_MANIFEST_BYTES + 1)
        after = source.stat()
    except OSError:
        _manifest_invalid()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        _manifest_invalid()
    if len(raw) > _MAX_MANIFEST_BYTES or len(raw) != opened.st_size:
        _manifest_invalid()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("resource_manifest_digest_mismatch")
    try:
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _manifest_invalid(),
        )
        _validate_manifest(manifest)
    except (UnicodeError, json.JSONDecodeError, TypeError):
        _manifest_invalid()
    return manifest
