"""Release lifecycle controller: plan, apply, verify, resume, rollback-plan, rollback-apply.

Every native touchpoint is an injected client behind the adapter boundary. The
controller performs no Git, gcloud, HTTP or SDK call itself. The dated 20260909
prepare, resume and activation scripts were ported here first; their field
invariants, queue audience and timeout policy are kept as module constants and
covered by tests before any behaviour was extended.
"""

import argparse
import base64
import copy
import hashlib
import importlib
import importlib.util
import json
import re
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for _entry in (str(_ROOT / "engine"), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from ops.deploy import readback, resource_guard  # noqa: E402
from src.analysis.open_intelligence.brain_contract import (  # noqa: E402
    canonical_bytes,
    canonical_digest,
)
from src.analysis.open_intelligence.general_question_control import (  # noqa: E402
    QuestionStoreError,
)
from src.analysis.open_intelligence.general_question_deployment import (  # noqa: E402
    validate_question_deployment,
)
from src.analysis.open_intelligence.general_question_policy import (  # noqa: E402
    QUESTION_DEADLINE_SECONDS,
    build_question_policy,
    validate_question_policy,
)

__all__ = [
    "QUESTION_DEADLINE_SECONDS",
    "build_question_policy",
    "canonical_bytes",
    "canonical_digest",
    "execute",
    "validate_release_index",
]

PROJECT = "ogilvy-trends-v2"
# Cloud Build names the project by number; the manifest carries both.
PROJECT_NUMBER = "590353929363"
REGION = "us-central1"
SERVICE_NAME = "listening-post-staging"
IDENTITY = f"{SERVICE_NAME}@{PROJECT}.iam.gserviceaccount.com"
AUDIENCE = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
WORKER_PATH = "/internal/general-question/execute"
QUEUE_ID = "oi-general-question-staging"
BUCKET = "listening-post-staging-cache"
SERVICE_API_NAME = f"projects/{PROJECT}/locations/{REGION}/services/{SERVICE_NAME}"
QUEUE_API_NAME = f"projects/{PROJECT}/locations/{REGION}/queues/{QUEUE_ID}"
SERVICE_RESOURCE = "//run.googleapis.com/" + SERVICE_API_NAME
QUEUE_RESOURCE = "//cloudtasks.googleapis.com/" + QUEUE_API_NAME
BUCKET_RESOURCE = f"//storage.googleapis.com/projects/_/buckets/{BUCKET}"
# The three further services a release index check reads. Each is guarded like
# every other read, so a run that the approved manifest does not cover stops
# before it spends the owner credential rather than after.
BUILD_RESOURCE = f"//cloudbuild.googleapis.com/projects/{PROJECT}/locations/{REGION}"
# The collection a build record is read from; read only, and not the location.
BUILDS_RESOURCE = BUILD_RESOURCE + "/builds"
REGISTRY_RESOURCE = (
    "//artifactregistry.googleapis.com/projects/"
    f"{PROJECT}/locations/{REGION}/repositories/intelligence-42"
)
IMAGE_REGISTRY = f"{REGION}-docker.pkg.dev/{PROJECT}/intelligence-42/"
APP_IMAGE_NAME = IMAGE_REGISTRY + "listening-post-question"
OBJECT_PREFIX = "open-intelligence/v2/staging/general-questions/"
LEDGER_OBJECT = (
    OBJECT_PREFIX + "allowances/general_cultural_question_staging_eval_v1/ledger.json"
)
SERVICE_TIMEOUT = "300s"
DISPATCH_DEADLINE = "270s"
DRAIN_SECONDS = 125
RECOVERY_REMAINING_SECONDS = 190
RESUME_REMAINING_SECONDS = 180
TAG_PATTERN = r"question-[a-z0-9-]{1,35}"
RESOURCE_LIMITS = {"cpu": "2", "memory": "4Gi"}
MAX_INSTANCES = 1
TIMEOUT_SCOPE = {
    "engine_deadline_seconds": QUESTION_DEADLINE_SECONDS,
    "browser_wait_seconds": 240,
    "cloud_tasks_dispatch_seconds": 270,
    "cloud_run_timeout_seconds": 300,
}
BUILD_STEPS = [
    "build_question_image",
    "verify_isolated_engine",
    "prepare_linux_boundary_test_wheels",
    "verify_linux_boundary_tests",
    "push_question_image",
]
INTAKE_CONTRACT = "general_question_intake_context_v2"
INVOCATION_CONTRACT = "general_cultural_question_v1"
ACTIVATION_STATE = "binding_created_ledger_refreshed_preserved_queue_paused"
ROUTE_VERIFIED_STATE = "traffic_verified_100_queue_paused"
ROLLBACK_VERIFIED_STATE = "rollback_verified_queue_paused"
APPROVED_RESOURCE_MANIFEST_SHA256 = (
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
)
PLAN_CONTRACT = "42_release_plan_v1"
RECEIPT_CONTRACT = "42_release_receipt_v1"
AUTHORITY_CONTRACT = "42_release_authority_v1"
RELEASE_INDEX_SCHEMA = "42_staging_release_v3"
REPOSITORY = "https://github.com/jhbanalytics-pixel/42-Ogilvy-Intelligence"
# Evidence the index producer computes from the checkout and uploads once, under
# the release commit, so the reader can resolve those references natively.
RELEASE_PREFIX = "open-intelligence/v2/staging/releases/"
EVIDENCE_REFERENCES = ("engine_tree", "app_tree", "ops_tree", "resource_manifest")
POLICY_OBJECT_NAME = OBJECT_PREFIX + "policies/{policy_digest}/policy.json"
BINDING_OBJECT_NAME = OBJECT_PREFIX + "deployments/{deployment_digest}/binding.json"
RESUME_MODES = ("empty", "recovery", "drain-expired")
ROLLBACK_CHANGES = ("pause_queue", "activate_binding", "route_traffic")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
# The project is pinned in the pattern, not left to a check the reader runs after
# the request has already gone out with the owner credential on it.
_BUILD_NAME = re.compile(
    r"projects/(?:"
    + re.escape(PROJECT)
    + "|"
    + re.escape(PROJECT_NUMBER)
    + r")/locations/"
    + re.escape(REGION)
    + r"/builds/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_CONTRACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REVISION = re.compile(r"listening-post-staging-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
_GENERATION = re.compile(r"[1-9][0-9]{0,19}\Z")
_STATIC_ENVIRONMENT = {
    "WEB_CONCURRENCY": "1",
    "GENERATION_MAX_CONCURRENT": "1",
    "DEPLOYMENT_PROFILE": "open-intelligence-staging",
    "GCP_PROJECT": PROJECT,
    "BQ_DATASET": "trends_v2_staging",
    "CACHE_BUCKET": BUCKET,
    "CACHE_PREFIX": "open-intelligence/v2/staging/",
    "APPLICATION_SOURCE": "open-intelligence-staging",
}
# The worker reads pinned bridge captures only while this is exactly "enabled"
# (general_question_context_admission.BRIDGE_READS_SWITCH). Every revision this
# lifecycle deploys carries it as a fixed value, never an operator input; with no
# bridge entry pinned it changes nothing.
BRIDGE_READS_SWITCH = "GENERAL_QUESTION_BRIDGE_READS"
# Carried by the serving revision beyond the ported map: pinned plain values and a
# secret reference by name and version alias, never a secret value.
_CARRIED_ENVIRONMENT = {"CACHE_TTL": "86400", BRIDGE_READS_SWITCH: "enabled"}
_SECRET_ENVIRONMENT = {
    "UI_PASSCODE": {"secret": "ui-passcode-staging", "version": "latest"},
}
PLAIN_ENVIRONMENT_KEYS = (
    frozenset(_STATIC_ENVIRONMENT)
    | frozenset(_CARRIED_ENVIRONMENT)
    | {
        "GENERAL_QUESTION_DEPLOYMENT_DIGEST",
        "GENERAL_QUESTION_WORKER_URL",
        "SOURCE_SHA",
    }
)
_LEDGER_FIELDS = {
    "contract_version",
    "allowance_id",
    "active_deployment_digest",
    "bindings",
    "requests",
    "reserved_microusd",
}
_ADMISSIONS_PAUSE_GAP = {
    "state": "unsupported_by_ledger_control",
    "reason": (
        "the ledger control record is the closed field set the app validates and "
        "carries no admissions pause marker, so rollback cannot refuse new "
        "admissions ahead of the activation write; the write itself swaps "
        "active_deployment_digest, after which the outgoing revision refuses new "
        "admissions with approval_required"
    ),
    "ledger_control_fields": sorted(_LEDGER_FIELDS),
}
_AUTHORITY_FIELDS = {"name", "uri", "generation", "sha256"}
_INDEX_FIELDS = {
    "schema_version",
    "repository",
    "commit",
    "engine_tree_sha256",
    "app_tree_sha256",
    "ops_tree_sha256",
    "engine_image",
    "app_image",
    "resource_manifest_sha256",
    "policy_digest",
    "deployment_digest",
    "contract_digests",
    "raw_authority_objects",
    "evidence_mode",
    "source_binding",
    "served_assets",
    "app_build",
    "engine_build",
    "evidence_objects",
}


class Refusal(ValueError):
    pass


def _refuse(code):
    raise Refusal(code)


def _require(condition, code):
    if not condition:
        _refuse(code)


# Ported from prepare_question_staging_deploy_20260909.py


def _activation_digest(value):
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def verify_activation(activation):
    try:
        _require(activation["state"] == ACTIVATION_STATE, "activation_invalid")
        binding = activation["binding"]
        policy = activation["policy"]
        for value, field in ((binding, "deployment_digest"), (policy, "policy_digest")):
            rest = {key: item for key, item in value.items() if key != field}
            _require(
                _activation_digest(rest) == value[field], "activation_digest_mismatch"
            )
        _require(
            binding["project"] == PROJECT and binding["region"] == REGION,
            "activation_invalid",
        )
        _require(binding["service_name"] == SERVICE_NAME, "activation_invalid")
        _require(binding["service_account_email"] == IDENTITY, "activation_invalid")
        _require(
            binding["revision_name"].startswith(SERVICE_NAME + "-q-"),
            "activation_invalid",
        )
        _require(
            re.fullmatch(r"[a-z][a-z0-9-]{1,62}", binding["revision_name"]),
            "activation_invalid",
        )
        _require(_HEX40.fullmatch(binding["lp_commit"]), "activation_invalid")
        _require(_HEX64.fullmatch(binding["image_digest"]), "activation_invalid")
        reviewed = activation["reviewed_inputs"]
        _require(reviewed["lp_commit"] == binding["lp_commit"], "activation_invalid")
        _require(_HEX40.fullmatch(reviewed["engine_commit"]), "activation_invalid")
        _require(
            reviewed["engine_bundle_digest"] == binding["engine_bundle_digest"],
            "activation_invalid",
        )
        _require(
            policy["limits"]["deadline_seconds"] == QUESTION_DEADLINE_SECONDS,
            "activation_invalid",
        )
        _require(
            policy["policy_digest"] == binding["policy_digest"], "activation_invalid"
        )
        _require(activation["queue_after"]["state"] == "PAUSED", "activation_invalid")
        build = activation["build"]
        _require(build["status"] == "SUCCESS", "activation_invalid")
        images = build["results"]["images"]
        _require(len(images) == 1, "activation_invalid")
        _require(
            images[0]["digest"] == "sha256:" + binding["image_digest"],
            "activation_invalid",
        )
        _require(
            images[0]["name"] == APP_IMAGE_NAME + ":" + reviewed["lp_commit"],
            "activation_image_name_mismatch",
        )
        gates = [
            step
            for step in build["steps"]
            if step["id"] == "verify_linux_boundary_tests"
            and step["status"] == "SUCCESS"
        ]
        _require(len(gates) == 1, "activation_invalid")
        _require(reviewed["gate_sha256"] in gates[0]["args"][-1], "activation_invalid")
        validate_question_deployment(binding)
        validate_question_policy(policy)
    except Refusal:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, QuestionStoreError):
        _refuse("activation_invalid")
    return copy.deepcopy(binding)


def activation_tag(activation):
    tag = "question-" + activation["reviewed_inputs"]["engine_commit"][:7]
    _require(re.fullmatch(TAG_PATTERN, tag), "tag_invalid")
    return tag


def asset_origin(audience, tag):
    """The tagged revision origin: the worker URL and every served asset URL are
    built from this one construction."""
    _require(isinstance(tag, str) and re.fullmatch(TAG_PATTERN, tag), "tag_invalid")
    _require(
        isinstance(audience, str) and audience.startswith("https://"),
        "binding_invalid",
    )
    return "https://" + tag + "---" + audience.removeprefix("https://").rstrip("/")


def worker_url(binding, tag):
    return (
        asset_origin(binding["canonical_service_audience"], tag)
        + binding["worker_path"]
    )


def image_reference(binding):
    return APP_IMAGE_NAME + "@sha256:" + binding["image_digest"]


def expected_environment(binding, tag):
    return {
        "GENERAL_QUESTION_DEPLOYMENT_DIGEST": binding["deployment_digest"],
        "GENERAL_QUESTION_WORKER_URL": worker_url(binding, tag),
        "SOURCE_SHA": binding["lp_commit"],
        **_STATIC_ENVIRONMENT,
    }


def expected_container_env(binding, tag):
    """Cloud Run v2 env entries: the ported map, the carried plain value, the secret reference."""
    plain = {**expected_environment(binding, tag), **_CARRIED_ENVIRONMENT}
    return [
        *({"name": key, "value": value} for key, value in plain.items()),
        *(
            {"name": key, "valueSource": {"secretKeyRef": dict(reference)}}
            for key, reference in _SECRET_ENVIRONMENT.items()
        ),
    ]


def expected_template(binding, tag):
    return {
        "revision": binding["revision_name"],
        "serviceAccount": IDENTITY,
        "timeout": SERVICE_TIMEOUT,
        "scaling": {"maxInstanceCount": MAX_INSTANCES},
        "containers": [
            {
                "image": image_reference(binding),
                "resources": {"limits": dict(RESOURCE_LIMITS)},
                "env": expected_container_env(binding, tag),
            }
        ],
    }


def traffic_targets(binding, tag):
    return [
        {
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
            "revision": binding["revision_name"],
            "percent": 100,
            "tag": tag,
        }
    ]


def _verify_container(containers, binding, tag, code, *, earlier=False):
    _require(len(containers) == 1, code)
    container = containers[0]
    _require(container["image"] == image_reference(binding), code)
    _require(container["resources"]["limits"] == RESOURCE_LIMITS, code)
    entries = container["env"]
    _require(len({item["name"] for item in entries}) == len(entries), code)
    env = {item["name"]: item.get("value") for item in entries}
    expected = {**expected_environment(binding, tag), **_CARRIED_ENVIRONMENT}
    if earlier and BRIDGE_READS_SWITCH not in {item["name"] for item in entries}:
        # A revision deployed before the lifecycle carried the switch has bridge
        # reads off. It stays a valid rollback target; any value it does carry
        # must still be exactly the expected one.
        del expected[BRIDGE_READS_SWITCH]
    _require(all(env.get(key) == value for key, value in expected.items()), code)
    references = {
        item["name"]: (item.get("valueSource") or {}).get("secretKeyRef")
        for item in entries
    }
    _require(
        all(
            references.get(key) == reference
            for key, reference in _SECRET_ENVIRONMENT.items()
        ),
        code,
    )


def verify_service(service, binding, tag):
    try:
        _require(service["name"] == SERVICE_API_NAME, "service_mismatch")
        _require(
            service["terminalCondition"]["state"] == "CONDITION_SUCCEEDED",
            "service_mismatch",
        )
        template = service["template"]
        _require(template["revision"] == binding["revision_name"], "service_mismatch")
        _require(template["serviceAccount"] == IDENTITY, "service_mismatch")
        _require(template["timeout"] == SERVICE_TIMEOUT, "service_mismatch")
        _require(
            template["scaling"]["maxInstanceCount"] == MAX_INSTANCES, "service_mismatch"
        )
        _verify_container(template["containers"], binding, tag, "service_mismatch")
    except (KeyError, TypeError, AttributeError):
        _refuse("service_mismatch")


def verify_revision(revision, binding, tag, *, earlier=False):
    """``earlier`` marks a rollback target, which may predate the bridge reads switch."""
    try:
        _require(
            revision["name"]
            == SERVICE_API_NAME + "/revisions/" + binding["revision_name"],
            "revision_mismatch",
        )
        _require(revision["serviceAccount"] == IDENTITY, "revision_mismatch")
        _require(revision["timeout"] == SERVICE_TIMEOUT, "revision_mismatch")
        _require(
            revision["scaling"]["maxInstanceCount"] == MAX_INSTANCES,
            "revision_mismatch",
        )
        ready = [
            item
            for item in revision["conditions"]
            if item["type"] == "Ready" and item["state"] == "CONDITION_SUCCEEDED"
        ]
        _require(len(ready) == 1, "revision_not_ready")
        _verify_container(
            revision["containers"], binding, tag, "revision_mismatch", earlier=earlier
        )
    except (KeyError, TypeError, AttributeError):
        _refuse("revision_mismatch")


def _private_value_hash(name, value):
    return hashlib.sha256(f"42_env_value_v1:{name}:{value}".encode()).hexdigest()


def redacted_service(service):
    """Copy only allowlisted plain values; every other entry is a name plus a value hash."""
    redacted = copy.deepcopy(service)
    template = redacted.get("template") or {}
    for container in template.get("containers") or []:
        entries = container.pop("env", None) or []
        container["env_names"] = sorted(item.get("name", "") for item in entries)
        env = []
        for item in entries:
            name = item.get("name", "")
            if "value" not in item:
                env.append({"name": name, "source": "secret"})
            elif name in PLAIN_ENVIRONMENT_KEYS:
                env.append({"name": name, "value": item["value"]})
            else:
                env.append(
                    {
                        "name": name,
                        "value_sha256": _private_value_hash(name, str(item["value"])),
                    }
                )
        container["env"] = env
    return redacted


# Shared helpers


def _parse_time(value, code):
    if not isinstance(value, str):
        _refuse(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _refuse(code)
    if parsed.tzinfo is None:
        _refuse(code)
    return parsed.astimezone(UTC)


def _stamp(value):
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _read_json(path, code):
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _refuse(code)
    return value, raw


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _payload(value):
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")


def _require_absent(path):
    if Path(path).exists():
        _refuse("output_exists")


def _write_once(path, value):
    try:
        with open(path, "xb") as stream:
            stream.write(_payload(value))
    except FileExistsError:
        _refuse("output_exists")


def _rewrite(path, value):
    Path(path).write_bytes(_payload(value))


def _uuid(value, code):
    try:
        _require(isinstance(value, str) and str(UUID(value)) == value, code)
    except (ValueError, TypeError, AttributeError):
        _refuse(code)
    return value


def _validate_ledger(value):
    try:
        _require(type(value) is dict and set(value) == _LEDGER_FIELDS, "ledger_invalid")
        _require(
            value["contract_version"] == "general_question_allowance_v1",
            "ledger_invalid",
        )
        bindings = value["bindings"]
        requests = value["requests"]
        _require(
            type(bindings) is dict and bindings and type(requests) is dict,
            "ledger_invalid",
        )
        for deployment, policy in bindings.items():
            _require(
                _HEX64.fullmatch(deployment) and _HEX64.fullmatch(policy),
                "ledger_invalid",
            )
        _require(value["active_deployment_digest"] in bindings, "ledger_invalid")
        _require(type(value["reserved_microusd"]) is int, "ledger_invalid")
        _require(
            value["reserved_microusd"] == len(requests) * 100_000, "ledger_invalid"
        )
        for request_id, row in requests.items():
            _uuid(request_id, "ledger_invalid")
            _require(type(row) is dict, "ledger_invalid")
            _require(
                bindings.get(row["deployment_digest"]) == row["policy_digest"],
                "ledger_invalid",
            )
            _require(row["reserved_microusd"] == 100_000, "ledger_invalid")
            _parse_time(row["deadline_at"], "ledger_invalid")
    except (KeyError, TypeError, AttributeError):
        _refuse("ledger_invalid")
    return value


def _read_ledger(clients):
    stored = clients["objects"].read(LEDGER_OBJECT)
    _require(stored is not None, "ledger_unavailable")
    try:
        value = json.loads(stored["raw"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _refuse("ledger_invalid")
    _validate_ledger(value)
    return {
        "generation": str(stored["generation"]),
        "sha256": _sha256(stored["raw"]),
        "value": value,
    }


def _read_object_json(clients, name, *, generation=None, code):
    stored = clients["objects"].read(name, generation=generation)
    _require(stored is not None, code)
    try:
        return (
            json.loads(stored["raw"].decode("utf-8")),
            stored["raw"],
            str(stored["generation"]),
        )
    except (UnicodeError, json.JSONDecodeError):
        _refuse(code)


def _listed_tasks(clients):
    items = []
    token = None
    for _ in range(20):
        page = clients["tasks"].list_tasks(QUEUE_API_NAME, page_token=token)
        items.extend(page.get("tasks", []))
        _require(len(items) <= 1000, "task_enumeration_incomplete")
        token = page.get("nextPageToken")
        if not token:
            return items
    _refuse("task_enumeration_incomplete")


def _guard(manifest, resource, action):
    resource_guard.assert_allowed(resource, action, manifest)


def _capture_before(clients, manifest):
    """Read the current service, queue, tasks and ledger through guarded reads."""
    _guard(manifest, SERVICE_RESOURCE, "read")
    _guard(manifest, QUEUE_RESOURCE, "read")
    _guard(manifest, BUCKET_RESOURCE, "read")
    service = clients["run"].get_service(SERVICE_API_NAME)
    _require(service is not None, "service_unavailable")
    queue = clients["tasks"].get_queue(QUEUE_API_NAME)
    _require(queue is not None, "queue_unavailable")
    tasks = _listed_tasks(clients)
    ledger = _read_ledger(clients)
    before = {
        "service": redacted_service(service),
        "queue": queue,
        "tasks": tasks,
        "ledger": {"generation": ledger["generation"], "sha256": ledger["sha256"]},
    }
    native = {"service": service, "queue": queue, "tasks": tasks, "ledger": ledger}
    return before, native


def _require_paused_empty(clients):
    queue = clients["tasks"].get_queue(QUEUE_API_NAME)
    _require(
        queue is not None and queue.get("state") == "PAUSED", "queue_not_paused_empty"
    )
    _require(not _listed_tasks(clients), "queue_not_paused_empty")
    return queue


def _load_authority(path, purpose, manifest_sha256, now):
    authority, raw = _read_json(path, "authority_invalid")
    keys = {
        "contract_version",
        "purpose",
        "resource_manifest_sha256",
        "granted_at",
        "expires_at",
        "grantor",
    }
    _require(type(authority) is dict and set(authority) == keys, "authority_invalid")
    _require(authority["contract_version"] == AUTHORITY_CONTRACT, "authority_invalid")
    _require(authority["purpose"] == purpose, "authority_invalid")
    _require(
        isinstance(authority["grantor"], str) and authority["grantor"].strip(),
        "authority_invalid",
    )
    _require(
        authority["resource_manifest_sha256"] == manifest_sha256,
        "authority_manifest_mismatch",
    )
    _check_authority_window(authority, now)
    return authority, _sha256(raw)


def _check_authority_window(authority, now):
    granted = _parse_time(authority["granted_at"], "authority_invalid")
    expires = _parse_time(authority["expires_at"], "authority_invalid")
    _require(granted <= now, "authority_not_yet_valid")
    _require(now < expires, "authority_expired")


def _load_plan(path, kind):
    plan, raw = _read_json(path, "plan_invalid")
    _require(
        type(plan) is dict and plan.get("contract_version") == PLAN_CONTRACT,
        "plan_invalid",
    )
    _require(plan.get("kind") == kind, "plan_kind_mismatch")
    _require(plan.get("state", "planned") == "planned", "plan_invalid")
    key = plan.get("idempotency_key")
    rest = {name: value for name, value in plan.items() if name != "idempotency_key"}
    _require(key == canonical_digest(rest), "idempotency_key_mismatch")
    return plan, _sha256(raw)


def _seal(plan):
    plan["idempotency_key"] = canonical_digest(plan)
    return plan


def _base_plan(kind, *, now, manifest_sha256, authority, authority_sha256):
    return {
        "contract_version": PLAN_CONTRACT,
        "kind": kind,
        "state": "planned",
        "created_at": _stamp(now),
        "resource_manifest_sha256": manifest_sha256,
        "authority": authority,
        "authority_sha256": authority_sha256,
        "resources": {
            "service": SERVICE_RESOURCE,
            "queue": QUEUE_RESOURCE,
            "bucket": BUCKET_RESOURCE,
        },
    }


def _dropped_write(error, *, kind, revision_name):
    """The write went out and the response to it did not come back.

    There is no operation name to save, because the name only ever arrives in the
    reply that was lost, while the server may well have applied the write. The
    record therefore names the effect verify has to reconcile against instead, so
    a lost reply still leaves something to settle rather than nothing.
    """
    return {
        "operation": None,
        "state": "unproven",
        "reads": 0,
        "resubmitted": False,
        "last_state": "unknown",
        "last_error": f"{type(error).__name__}: {error}",
        "reconcile_by": {"kind": kind, "revision_name": revision_name},
    }


def _reconcile_dropped_write(clients, operation, binding, tag):
    """Settle a write whose reply was lost by reading its effect back."""
    reconcile = operation.get("reconcile_by")
    named = type(reconcile) is dict
    record = {
        "operation": None,
        "state": "unproven",
        "reconciled_by": reconcile.get("kind") if named else None,
        "last_error": operation.get("last_error"),
    }
    # Each branch settles its own record, so a kind this code does not read
    # cannot reach a success it never proved by falling past the branches.
    try:
        _require(named or reconcile is None, "reconcile_target_malformed")
        if named and reconcile.get("kind") == "revision":
            _require(
                type(reconcile.get("revision_name")) is str,
                "reconcile_target_malformed",
            )
            revision = clients["run"].get_revision(
                SERVICE_API_NAME + "/revisions/" + reconcile["revision_name"]
            )
            _require(revision is not None, "revision_unavailable")
            verify_revision(revision, binding, tag)
            record["state"] = "succeeded"
        elif named and reconcile.get("kind") == "traffic":
            service = clients["run"].get_service(SERVICE_API_NAME)
            _require(service is not None, "service_unavailable")
            _verify_routed_revision(clients, service, binding, tag)
            record["state"] = "succeeded"
        else:
            _refuse("reconcile_target_unknown")
    except Refusal as error:
        record["error"] = str(error)
        return record
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        # A lost reply reaches this code as a native, URL, HTTP or socket error,
        # never as one of these. The adapter spells its credential and
        # configuration refusals as plain value errors, and the rest of this set
        # is what a programming error or an unshaped input raises, so both stay
        # loud here rather than arriving on the receipt as a failed read.
        raise
    except Exception as error:
        # The read that settles a lost reply travels the same transport that lost
        # one, so it can lose its own. Recording the failed read keeps the record
        # unproven and leaves a receipt, rather than ending verify with nothing
        # written for the very operation that needed settling.
        record["error"] = f"reconcile_read_failed: {type(error).__name__}: {error}"
        return record
    return record


def _wait(clients, args, operation):
    clock = clients["clock"]
    return readback.wait_operation(
        clients["run"].read_operation,
        operation["operation"],
        deadline_seconds=args.deadline_seconds,
        poll_seconds=args.poll_seconds,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )


def _classify_requests(ledger, now):
    groups = {
        "completed": [],
        "held": [],
        "in_flight": [],
        "queued": [],
        "expired_unexecuted": [],
    }
    for request_id, row in ledger["requests"].items():
        if "result" in row:
            groups["completed" if "execution" in row else "held"].append(request_id)
        elif "execution" in row:
            groups["in_flight"].append(request_id)
        elif _parse_time(row["deadline_at"], "ledger_invalid") <= now:
            groups["expired_unexecuted"].append(request_id)
        else:
            groups["queued"].append(request_id)
    return groups


def _forward_recovery_plan(ledger, *, route=None):
    plan = {
        "queue": "paused",
        "preserved_requests": sorted(ledger["requests"]),
        "reserved_microusd": ledger["reserved_microusd"],
        "active_deployment_digest": ledger["active_deployment_digest"],
        "next": (
            "keep the queue paused, prepare a compatible forward activation, then run "
            "release.py plan, apply and verify; resume only after verify passes"
        ),
    }
    if route is not None:
        plan["route"] = route
        reconcile = (
            "reconcile the named route operation natively"
            if route.get("operation")
            else "read the service traffic back natively, since no route operation "
            "name survived the write"
        )
        plan["next"] = (
            "keep the queue paused; the ledger already activates the target revision, "
            f"so {reconcile} and route the target revision again; a repeated "
            "rollback-apply refuses before_state_changed and a new rollback-plan "
            "refuses rollback_target_already_active"
        )
    return plan


# Release index


def _hex64(value):
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def approved_object_name(uri):
    """Split ``gs://<approved bucket>/<name>`` and return the name, or None.

    The bucket is compared for equality against the one the resource manifest
    covers, not matched inside a pattern: a pattern that lost its separator would
    accept a bucket whose name merely starts with the approved one, and a pattern
    matched by search would accept a foreign bucket carrying the approved name
    somewhere in the string. Both of those reach a reader holding the owner
    credential, so neither the separator nor the anchoring may be load bearing.
    """
    prefix = "gs://"
    if not isinstance(uri, str) or not uri.startswith(prefix):
        return None
    bucket, slash, name = uri[len(prefix) :].partition("/")
    if not slash or bucket != BUCKET or not name:
        return None
    if any(character.isspace() or character == "#" for character in name):
        return None
    return name


def _object_binding(value):
    _require(
        type(value) is dict and set(value) == {"uri", "generation", "sha256"},
        "release_index_invalid",
    )
    _require(approved_object_name(value["uri"]) is not None, "release_index_invalid")
    _require(
        isinstance(value["generation"], str)
        and _GENERATION.fullmatch(value["generation"]),
        "release_index_invalid",
    )
    _require(_hex64(value["sha256"]), "release_index_invalid")


def _image_digest(value):
    _require(
        isinstance(value, str) and value.startswith(IMAGE_REGISTRY),
        "release_index_invalid",
    )
    match = re.fullmatch(r"[a-z0-9][a-z0-9./_-]*@sha256:([0-9a-f]{64})", value)
    _require(match is not None, "release_index_invalid")
    return match.group(1)


def evidence_object_name(commit, reference):
    """The one object name an uploaded evidence payload may live under."""
    return RELEASE_PREFIX + commit + "/" + reference.replace(":", "/", 1)


def evidence_references(index):
    """Every reference whose bytes the producer pins under the release prefix.

    The three tree inventories, the resource manifest and one entry per reviewed
    contract are computed from the checkout. The raw authority objects and the
    source binding are pinned as well, because each of those lives at a fixed
    object name that another writer supersedes: the ledger is rewritten by the
    next activation or pricing renewal, and the superseded generation is then
    unreadable, since the bucket keeps no versions. The index still records
    where those bytes came from, but the validator reads the pinned copy.
    """
    return (
        [*EVIDENCE_REFERENCES]
        + ["contract:" + name for name in index["contract_digests"]]
        + ["authority:" + item["name"] for item in index["raw_authority_objects"]]
        + ["source_binding"]
    )


def evidence_digest(index, reference):
    """The digest the index already records for a pinned reference, which the
    pinned object's own binding must repeat."""
    if reference.startswith("contract:"):
        return index["contract_digests"][reference[len("contract:") :]]
    if reference.startswith("authority:"):
        name = reference[len("authority:") :]
        for item in index["raw_authority_objects"]:
            if item["name"] == name:
                return item["sha256"]
        _refuse("release_index_invalid")
    if reference == "source_binding":
        return index["source_binding"]["sha256"]
    return index[reference + "_sha256"]


def release_index_references(index):
    """Every reference ``validate_release_index`` reads, in the order it reads
    them. The receipt records what was read against this list."""
    return (
        ["commit", "engine_image", "app_image", "policy", "deployment"]
        + evidence_references(index)
        + ["asset:" + name for name in index["served_assets"]]
    )


def validate_release_index_shape(index: dict) -> dict:
    """Check the closed ReleaseIndex grammar only; bytes are checked separately.

    Only ``42_staging_release_v3`` is accepted. No index of an earlier version
    was ever produced, and neither can be resolved natively: v1 names no build,
    so nothing outside the index attests the commit or either image, and neither
    v1 nor v2 pins the raw authority objects or the source binding, so both
    expect the validator to read a ledger generation that the next write to that
    object destroys.
    """
    _require(
        type(index) is dict and set(index) == _INDEX_FIELDS, "release_index_invalid"
    )
    _require(index["schema_version"] == RELEASE_INDEX_SCHEMA, "release_index_invalid")
    _require(index["repository"] == REPOSITORY, "release_index_invalid")
    _require(
        isinstance(index["commit"], str) and _HEX40.fullmatch(index["commit"]),
        "release_index_invalid",
    )
    for field in ("app_build", "engine_build"):
        _require(
            isinstance(index[field], str) and _BUILD_NAME.fullmatch(index[field]),
            "release_index_invalid",
        )
    for field in (
        "engine_tree_sha256",
        "app_tree_sha256",
        "ops_tree_sha256",
        "resource_manifest_sha256",
        "policy_digest",
        "deployment_digest",
    ):
        _require(_hex64(index[field]), "release_index_invalid")
    images = {
        field: _image_digest(index[field]) for field in ("engine_image", "app_image")
    }
    for field in ("contract_digests", "served_assets"):
        mapping = index[field]
        _require(type(mapping) is dict, "release_index_invalid")
        for name, digest in mapping.items():
            _require(
                isinstance(name, str) and name.strip() and _hex64(digest),
                "release_index_invalid",
            )
    for name in index["contract_digests"]:
        _require(_CONTRACT_NAME.fullmatch(name), "release_index_invalid")
    _require(type(index["raw_authority_objects"]) is list, "release_index_invalid")
    names = []
    for item in index["raw_authority_objects"]:
        _require(
            type(item) is dict and set(item) == _AUTHORITY_FIELDS,
            "release_index_invalid",
        )
        name = item["name"]
        _require(
            isinstance(name, str) and _CONTRACT_NAME.fullmatch(name),
            "release_index_invalid",
        )
        names.append(name)
        _object_binding({key: item[key] for key in ("uri", "generation", "sha256")})
    _require(len(set(names)) == len(names), "release_index_invalid")
    _require(
        index["evidence_mode"] in {"retained_baseline", "daily_capture"},
        "release_index_invalid",
    )
    _object_binding(index["source_binding"])
    evidence = index["evidence_objects"]
    _require(
        type(evidence) is dict and set(evidence) == set(evidence_references(index)),
        "release_index_invalid",
    )
    for reference, item in evidence.items():
        _object_binding(item)
        expected = (
            "gs://" + BUCKET + "/" + evidence_object_name(index["commit"], reference)
        )
        _require(item["uri"] == expected, "release_index_invalid")
        _require(
            item["sha256"] == evidence_digest(index, reference), "release_index_invalid"
        )
    return images


def validate_release_index(index: dict, *, byte_reader) -> dict:
    images = validate_release_index_shape(index)

    def expect(reference, digest, *, exact=None):
        raw = byte_reader(reference)
        _require(isinstance(raw, bytes | bytearray), "release_index_bytes_unavailable")
        if exact is not None:
            _require(bytes(raw) == exact, "release_index_digest_mismatch")
        else:
            _require(_sha256(bytes(raw)) == digest, "release_index_digest_mismatch")

    # The order here is the order release_index_references reports, which is what
    # the verify receipt records as read.
    expect("commit", None, exact=index["commit"].encode("ascii"))
    for field, digest in (
        ("engine_image", images["engine_image"]),
        ("app_image", images["app_image"]),
        ("policy", index["policy_digest"]),
        ("deployment", index["deployment_digest"]),
    ):
        expect(field, digest)
    # The raw authority objects and the source binding are read from the pinned
    # copies, never from the bucket locator they record, which names a
    # generation the next write to that object destroys.
    for reference in evidence_references(index):
        expect(reference, evidence_digest(index, reference))
    for name, digest in index["served_assets"].items():
        expect("asset:" + name, digest)
    return copy.deepcopy(index)


# Release plan, apply, verify


def _expected_block(binding, tag):
    return {
        "revision_name": binding["revision_name"],
        "image_digest": binding["image_digest"],
        "image": image_reference(binding),
        "lp_commit": binding["lp_commit"],
        "engine_bundle_digest": binding["engine_bundle_digest"],
        "policy_digest": binding["policy_digest"],
        "deployment_digest": binding["deployment_digest"],
        "tag": tag,
        "worker_url": worker_url(binding, tag),
        "audience": binding["canonical_service_audience"],
        "service_account": binding["service_account_email"],
    }


def _plan_release(args, clients, manifest, manifest_sha256, now):
    _require(args.activation is not None, "activation_required")
    activation, activation_raw = _read_json(args.activation, "activation_invalid")
    binding = verify_activation(activation)
    tag = activation_tag(activation)
    authority, authority_sha256 = _load_authority(
        args.authority, "release", manifest_sha256, now
    )
    _require_absent(args.output)
    before, native = _capture_before(clients, manifest)
    _require(
        native["queue"]["state"] == "PAUSED" and not native["tasks"],
        "queue_not_paused_empty",
    )
    _require(
        native["ledger"]["value"] == activation["control_after"], "ledger_incompatible"
    )
    _require(
        native["ledger"]["value"]["active_deployment_digest"]
        == binding["deployment_digest"],
        "ledger_incompatible",
    )
    changes = []
    existing = clients["run"].get_revision(
        SERVICE_API_NAME + "/revisions/" + binding["revision_name"]
    )
    if existing is None:
        changes.append(
            {
                "operation": "deploy_revision",
                "resource": SERVICE_RESOURCE,
                "action": "deploy",
                "revision": binding["revision_name"],
                "template": expected_template(binding, tag),
                "traffic": "unchanged",
            }
        )
    else:
        verify_revision(existing, binding, tag)
    changes.append(
        {
            "operation": "route_traffic",
            "resource": SERVICE_RESOURCE,
            "action": "deploy",
            "traffic": traffic_targets(binding, tag),
        }
    )
    plan = _base_plan(
        "release",
        now=now,
        manifest_sha256=manifest_sha256,
        authority=authority,
        authority_sha256=authority_sha256,
    )
    plan.update(
        {
            "activation_sha256": _sha256(activation_raw),
            "binding": binding,
            "policy_digest": activation["policy"]["policy_digest"],
            "expected": _expected_block(binding, tag),
            "before": before,
            "before_sha256": canonical_digest(before),
            "changes": changes,
        }
    )
    _write_once(args.output, _seal(plan))
    return 0, {"state": "planned", "kind": "release", "output": str(args.output)}


def _resolve_policy(clients, binding, activation=None):
    name = OBJECT_PREFIX + f"policies/{binding['policy_digest']}/policy.json"
    stored, _, _ = _read_object_json(clients, name, code="policy_unavailable")
    try:
        policy = validate_question_policy(stored)
    except ValueError:
        _refuse("policy_unavailable")
    _require(policy["policy_digest"] == binding["policy_digest"], "policy_unavailable")
    if activation is not None:
        _require(policy == activation["policy"], "policy_unavailable")
    return policy


def _route_receipt(args):
    """Return the binding, tag and ledger expectation carried by a verified route receipt."""
    _require(args.route is not None, "route_required")
    route, route_raw = _read_json(args.route, "route_invalid")
    _require(type(route) is dict, "route_invalid")
    state = route.get("state")
    if state == ROUTE_VERIFIED_STATE:
        _require(args.activation is not None, "activation_required")
        activation, activation_raw = _read_json(args.activation, "activation_invalid")
        binding = verify_activation(activation)
        tag = activation_tag(activation)
        _require(
            route.get("binding", {}).get("deployment_digest")
            == binding["deployment_digest"],
            "route_invalid",
        )
        expectation = {"kind": "release", "control_after": activation["control_after"]}
        activation_sha256 = _sha256(activation_raw)
    elif state == ROLLBACK_VERIFIED_STATE:
        _require(args.activation is None, "activation_forbidden")
        _require(route.get("kind") == "rollback", "route_invalid")
        try:
            binding = validate_question_deployment(route["binding"])
            tag = route["target"]["tag"]
            ledger = route["after"]["ledger"]
        except (KeyError, TypeError, QuestionStoreError):
            _refuse("route_invalid")
        _require(
            isinstance(tag, str) and re.fullmatch(TAG_PATTERN, tag), "route_invalid"
        )
        _require(
            type(ledger) is dict
            and isinstance(ledger.get("generation"), str)
            and _hex64(ledger.get("sha256")),
            "route_invalid",
        )
        expectation = {
            "kind": "rollback",
            "ledger": {"generation": ledger["generation"], "sha256": ledger["sha256"]},
        }
        activation = None
        activation_sha256 = None
    else:
        _refuse("route_invalid")
    finished_at = _parse_time(route.get("finished_at"), "route_invalid")
    return {
        "binding": binding,
        "tag": tag,
        "expectation": expectation,
        "activation": activation,
        "activation_sha256": activation_sha256,
        "route_sha256": _sha256(route_raw),
        "state": state,
        "finished_at": finished_at,
    }


def _plan_resume(args, clients, manifest, manifest_sha256, now):
    _require(args.mode in RESUME_MODES, "resume_mode_invalid")
    ids = {
        "pending_request": args.pending_request,
        "parent_request": args.parent_request,
        "anchor_request": args.anchor_request,
    }
    if args.mode == "empty":
        _require(
            all(value is None for value in ids.values()), "pending_flags_forbidden"
        )
    else:
        _require(
            all(value is not None for value in ids.values()), "pending_request_required"
        )
        for value in ids.values():
            _uuid(value, "request_id_invalid")
    route = _route_receipt(args)
    binding = route["binding"]
    tag = route["tag"]
    authority, authority_sha256 = _load_authority(
        args.authority, "release", manifest_sha256, now
    )
    _require_absent(args.output)
    resume = {"mode": args.mode, **ids}
    policy = None
    if args.mode != "empty":
        policy = _resolve_policy(clients, binding, route["activation"])
    preflight = _resume_preflight(
        clients,
        manifest,
        binding,
        tag,
        resume,
        route["finished_at"],
        now,
        expectation=route["expectation"],
        policy=policy,
    )
    plan = _base_plan(
        "resume",
        now=now,
        manifest_sha256=manifest_sha256,
        authority=authority,
        authority_sha256=authority_sha256,
    )
    plan.update(
        {
            "activation_sha256": route["activation_sha256"],
            "route": {
                "sha256": route["route_sha256"],
                "state": route["state"],
                "kind": route["expectation"]["kind"],
                "finished_at": _stamp(route["finished_at"]),
            },
            "binding": binding,
            "expectation": route["expectation"],
            "expected": _expected_block(binding, tag),
            "resume": resume,
            "before": preflight["before"],
            "before_sha256": canonical_digest(preflight["before"]),
            "changes": [
                {
                    "operation": "resume_queue",
                    "resource": QUEUE_RESOURCE,
                    "action": "write",
                    "queue": QUEUE_API_NAME,
                }
            ],
        }
    )
    _write_once(args.output, _seal(plan))
    return 0, {"state": "planned", "kind": "resume", "output": str(args.output)}


def _plan(args, clients, manifest, manifest_sha256, now):
    if args.kind == "resume":
        return _plan_resume(args, clients, manifest, manifest_sha256, now)
    _require(args.mode is None and args.route is None, "resume_flags_forbidden")
    _require(
        args.pending_request is None
        and args.parent_request is None
        and args.anchor_request is None,
        "resume_flags_forbidden",
    )
    return _plan_release(args, clients, manifest, manifest_sha256, now)


def _after_state(clients, binding, tag, *, expected_traffic):
    service = clients["run"].get_service(SERVICE_API_NAME)
    _require(service is not None, "service_unavailable")
    verify_service(service, binding, tag)
    observed = [
        {key: item.get(key) for key in expected_traffic[0]}
        for item in service.get("trafficStatuses", [])
    ]
    _require(
        service.get("traffic") == expected_traffic and observed == expected_traffic,
        "traffic_unverified",
    )
    queue = clients["tasks"].get_queue(QUEUE_API_NAME)
    ledger = _read_ledger(clients)
    return {
        "service": redacted_service(service),
        "queue": queue,
        "ledger": {"generation": ledger["generation"], "sha256": ledger["sha256"]},
    }, ledger


def _apply(args, clients, manifest, manifest_sha256, now):
    plan, plan_sha256 = _load_plan(args.plan, "release")
    _require(
        plan["resource_manifest_sha256"] == manifest_sha256,
        "resource_manifest_digest_mismatch",
    )
    _check_authority_window(plan["authority"], now)
    binding = plan["binding"]
    tag = plan["expected"]["tag"]
    receipt = {
        "contract_version": RECEIPT_CONTRACT,
        "kind": "release",
        "state": "started",
        "plan_sha256": plan_sha256,
        "idempotency_key": plan["idempotency_key"],
        "started_at": _stamp(now),
        "binding": binding,
        "expected": plan["expected"],
        "operations": {},
    }
    _write_once(args.output, receipt)

    def finish(state, code, error=None):
        receipt["state"] = state
        if error is not None:
            receipt["error"] = error
        receipt["finished_at"] = _stamp(clients["clock"].now())
        _rewrite(args.output, receipt)
        return code, {
            "state": state,
            "output": str(args.output),
            **({"error": error} if error else {}),
        }

    try:
        before, _native = _capture_before(clients, manifest)
        if canonical_digest(before) != plan["before_sha256"]:
            receipt["observed_before"] = before
            return finish("refused", 1, "before_state_changed")
        for change in plan["changes"]:
            if change["operation"] == "deploy_revision":
                _guard(manifest, change["resource"], change["action"])
                try:
                    operation = clients["run"].deploy_revision(
                        SERVICE_API_NAME,
                        revision=change["revision"],
                        template=change["template"],
                        idempotency_key=plan["idempotency_key"],
                    )
                except ValueError:
                    raise
                except Exception as error:
                    receipt["operations"]["deploy_revision"] = _dropped_write(
                        error, kind="revision", revision_name=change["revision"]
                    )
                    return finish("unproven_requires_reconciliation", 1)
                result = _wait(clients, args, operation)
                receipt["operations"]["deploy_revision"] = result
                if result["state"] == "unproven":
                    return finish("unproven_requires_reconciliation", 1)
                if result["state"] != "succeeded":
                    return finish(
                        "failed_or_unproven_requires_native_reconciliation",
                        1,
                        "deployment_failed",
                    )
                revision = clients["run"].get_revision(
                    SERVICE_API_NAME + "/revisions/" + change["revision"]
                )
                _require(revision is not None, "revision_unavailable")
                verify_revision(revision, binding, tag)
            elif change["operation"] == "route_traffic":
                _require_paused_empty(clients)
                service = clients["run"].get_service(SERVICE_API_NAME)
                _require(service is not None, "service_unavailable")
                verify_service(service, binding, tag)
                _guard(manifest, change["resource"], change["action"])
                try:
                    operation = clients["run"].update_traffic(
                        SERVICE_API_NAME,
                        etag=service["etag"],
                        traffic=change["traffic"],
                        idempotency_key=plan["idempotency_key"],
                    )
                except ValueError:
                    raise
                except Exception as error:
                    receipt["operations"]["route_traffic"] = _dropped_write(
                        error, kind="traffic", revision_name=binding["revision_name"]
                    )
                    return finish("unproven_requires_reconciliation", 1)
                result = _wait(clients, args, operation)
                receipt["operations"]["route_traffic"] = result
                if result["state"] == "unproven":
                    return finish("unproven_requires_reconciliation", 1)
                if result["state"] != "succeeded":
                    return finish(
                        "failed_or_unproven_requires_native_reconciliation",
                        1,
                        "route_failed",
                    )
            else:
                _refuse("plan_invalid")
        after, ledger = _after_state(
            clients, binding, tag, expected_traffic=traffic_targets(binding, tag)
        )
        _require(
            after["queue"] is not None and after["queue"]["state"] == "PAUSED",
            "queue_not_paused_empty",
        )
        _require(
            ledger["generation"] == plan["before"]["ledger"]["generation"],
            "ledger_changed",
        )
        _require(
            ledger["value"]["active_deployment_digest"] == binding["deployment_digest"],
            "ledger_incompatible",
        )
        receipt["after"] = after
        receipt["desired_traffic"] = traffic_targets(binding, tag)
        return finish(ROUTE_VERIFIED_STATE, 0)
    except Refusal as error:
        return finish(
            "failed_or_unproven_requires_native_reconciliation", 1, str(error)
        )
    except Exception as error:
        return finish(
            "failed_or_unproven_requires_native_reconciliation",
            1,
            f"{type(error).__name__}: {error}",
        )


def _verify(args, clients, manifest, manifest_sha256, now):
    plan, plan_sha256 = _load_plan(args.plan, "release")
    _require(
        plan["resource_manifest_sha256"] == manifest_sha256,
        "resource_manifest_digest_mismatch",
    )
    _require_absent(args.output)
    result, result_raw = _read_json(args.result, "result_invalid")
    _require(
        type(result) is dict and result.get("plan_sha256") == plan_sha256,
        "result_plan_mismatch",
    )
    binding = plan["binding"]
    tag = plan["expected"]["tag"]
    receipt = {
        "contract_version": RECEIPT_CONTRACT,
        "kind": "verify",
        "plan_sha256": plan_sha256,
        "result_sha256": _sha256(result_raw),
        "checked_at": _stamp(now),
        "reconciled_operations": {},
        "drift": [],
    }
    _guard(manifest, SERVICE_RESOURCE, "read")
    _guard(manifest, QUEUE_RESOURCE, "read")
    _guard(manifest, BUCKET_RESOURCE, "read")
    for name, operation in (result.get("operations") or {}).items():
        if operation.get("state") != "unproven":
            continue
        receipt["reconciled_operations"][name] = (
            _wait(clients, args, operation)
            if operation.get("operation")
            else _reconcile_dropped_write(clients, operation, binding, tag)
        )
    drift = receipt["drift"]
    service = clients["run"].get_service(SERVICE_API_NAME)
    revision = clients["run"].get_revision(
        SERVICE_API_NAME + "/revisions/" + binding["revision_name"]
    )
    queue = clients["tasks"].get_queue(QUEUE_API_NAME)
    ledger = _read_ledger(clients)
    for label, check in (
        ("service", lambda: verify_service(service, binding, tag)),
        ("revision", lambda: verify_revision(revision, binding, tag)),
    ):
        try:
            check()
        except Refusal as error:
            drift.append({"item": label, "error": str(error)})
    expected_traffic = traffic_targets(binding, tag)
    observed = [
        {key: item.get(key) for key in expected_traffic[0]}
        for item in (service or {}).get("trafficStatuses", [])
    ]
    if (service or {}).get(
        "traffic"
    ) != expected_traffic or observed != expected_traffic:
        drift.append({"item": "traffic", "error": "traffic_unverified"})
    if queue is None or queue.get("state") != "PAUSED":
        drift.append({"item": "queue", "error": "queue_not_paused"})
    if ledger["value"]["active_deployment_digest"] != binding["deployment_digest"]:
        drift.append({"item": "ledger", "error": "ledger_incompatible"})
    if (
        ledger["value"]["bindings"].get(binding["deployment_digest"])
        != binding["policy_digest"]
    ):
        drift.append({"item": "ledger", "error": "ledger_policy_mismatch"})
    for name, operation in (result.get("operations") or {}).items():
        final = receipt["reconciled_operations"].get(name, operation)
        if final.get("state") != "succeeded":
            drift.append({"item": "operation:" + name, "error": "operation_unproven"})
    if args.release_index is not None:
        index, index_raw = _read_json(args.release_index, "release_index_invalid")
        # Guarded before the first request: the index check reads Cloud Build,
        # Artifact Registry and the tagged revision on top of the service, the
        # queue and the bucket already asked for above.
        _guard(manifest, BUILDS_RESOURCE, "read")
        _guard(manifest, REGISTRY_RESOURCE, "read")
        _guard(manifest, SERVICE_RESOURCE, "invoke")
        # The reader is bound to this index and to the tagged revision the plan
        # routed, so served assets are read from the revision under test.
        origin = asset_origin(plan["expected"]["audience"], tag)
        reader = clients["bytes"].reader(index, asset_origin=origin)
        read = []

        def counting_reader(reference, reader=reader, read=read):
            read.append(reference)
            return reader(reference)

        record = {
            "sha256": _sha256(index_raw),
            "schema_version": index.get("schema_version")
            if type(index) is dict
            else None,
            "state": "verified",
            "references_read": read,
            "counts": {},
        }
        receipt["release_index"] = record
        receipt["release_index_sha256"] = record["sha256"]
        # An index that does not resolve is drift like any other failed check.
        # Escaping here would throw away the record of everything verify did
        # prove about the service, the queue and the ledger.
        try:
            validated = validate_release_index(index, byte_reader=counting_reader)
            bound = (
                validated["app_image"] == plan["expected"]["image"]
                and validated["policy_digest"] == binding["policy_digest"]
                and validated["deployment_digest"] == binding["deployment_digest"]
                and validated["resource_manifest_sha256"] == manifest_sha256
                and validated["commit"] == binding["lp_commit"]
            )
            _require(bound, "release_index_binding_mismatch")
            record["counts"] = {
                "contract_digests": len(validated["contract_digests"]),
                "evidence_objects": len(validated["evidence_objects"]),
                "raw_authority_objects": len(validated["raw_authority_objects"]),
                "served_assets": len(validated["served_assets"]),
            }
        except Refusal as error:
            record["state"] = "unproven"
            drift.append({"item": "release_index", "error": str(error)})
    receipt["native"] = {
        "service": redacted_service(service) if service else None,
        "revision": None
        if revision is None
        else {
            "name": revision.get("name"),
            "image": (revision.get("containers") or [{}])[0].get("image"),
            "service_account": revision.get("serviceAccount"),
        },
        "queue": queue,
        "ledger": {
            "generation": ledger["generation"],
            "sha256": ledger["sha256"],
            "active_deployment_digest": ledger["value"]["active_deployment_digest"],
            "reserved_microusd": ledger["value"]["reserved_microusd"],
            "request_count": len(ledger["value"]["requests"]),
        },
    }
    receipt["state"] = "verified" if not drift else "unproven"
    _write_once(args.output, receipt)
    return (0 if not drift else 1), {
        "state": receipt["state"],
        "output": str(args.output),
    }


# Queue resume, ported from resume_verified_question_queue_20260909.py


def _verify_task_target(task, binding, tag):
    try:
        http = task["httpRequest"]
        _require(http["httpMethod"] == "POST", "task_audience_mismatch")
        _require(http["url"] == worker_url(binding, tag), "task_audience_mismatch")
        _require(
            http["oidcToken"]["serviceAccountEmail"]
            == binding["service_account_email"],
            "task_audience_mismatch",
        )
        _require(
            http["oidcToken"]["audience"] == binding["canonical_service_audience"],
            "task_audience_mismatch",
        )
    except (KeyError, TypeError):
        _refuse("task_audience_mismatch")


def _verify_routed_revision(clients, service, binding, tag, *, earlier=False):
    """Terminal traffic readback plus the routed revision itself; the template may differ."""
    terminal = (service.get("terminalCondition") or {}).get(
        "state"
    ) == "CONDITION_SUCCEEDED"
    _require(terminal, "route_not_terminal")
    statuses = service.get("trafficStatuses") or []
    _require(len(statuses) == 1, "route_not_terminal")
    _require(
        statuses[0].get("revision") == binding["revision_name"], "route_not_terminal"
    )
    _require(
        statuses[0].get("percent") == 100 and statuses[0].get("tag") == tag,
        "route_not_terminal",
    )
    _require(
        service.get("traffic") == traffic_targets(binding, tag), "route_not_terminal"
    )
    revision = clients["run"].get_revision(
        SERVICE_API_NAME + "/revisions/" + binding["revision_name"]
    )
    _require(revision is not None, "route_not_terminal")
    verify_revision(revision, binding, tag, earlier=earlier)


def _resume_preflight(
    clients, manifest, binding, tag, resume, finished_at, now, *, expectation, policy
):
    drain = (now - finished_at).total_seconds()
    _require(drain >= DRAIN_SECONDS, "drain_not_elapsed")
    before, native = _capture_before(clients, manifest)
    _verify_routed_revision(
        clients,
        native["service"],
        binding,
        tag,
        earlier=expectation.get("kind") == "rollback",
    )
    _require(native["queue"]["state"] == "PAUSED", "queue_not_paused")
    tasks = native["tasks"]
    for task in tasks:
        _verify_task_target(task, binding, tag)
    ledger = native["ledger"]
    control = ledger["value"]
    _require(
        control["active_deployment_digest"] == binding["deployment_digest"],
        "ledger_incompatible",
    )
    _require(
        control["bindings"].get(binding["deployment_digest"])
        == binding["policy_digest"],
        "ledger_incompatible",
    )
    if expectation["kind"] == "release":
        expected = copy.deepcopy(expectation["control_after"])
    else:
        recorded = expectation["ledger"]
        _require(
            ledger["generation"] == recorded["generation"]
            and ledger["sha256"] == recorded["sha256"],
            "ledger_changed",
        )
        expected = copy.deepcopy(control)
        if resume["mode"] != "empty":
            pending = expected["requests"].pop(resume["pending_request"], None)
            _require(pending is not None, "pending_request_missing")
            expected["reserved_microusd"] -= pending["reserved_microusd"]
    details = {
        "before": before,
        "drain_seconds": drain,
        "ledger_generation": ledger["generation"],
        "reserved_microusd": control["reserved_microusd"],
        "tasks": tasks,
        "queue": native["queue"],
    }
    if resume["mode"] == "empty":
        _require(not tasks, "tasks_present")
    else:
        rid = resume["pending_request"]
        _require(policy is not None, "policy_unavailable")
        _require(
            set(control["requests"]) - set(expected["requests"]) == {rid},
            "pending_request_missing",
        )
        row = control["requests"][rid]
        _require(
            row["deployment_digest"] == binding["deployment_digest"],
            "pending_request_mismatch",
        )
        _require(
            row["policy_digest"] == policy["policy_digest"], "pending_request_mismatch"
        )
        _require(
            row["reserved_microusd"] == policy["limits"]["request_microusd"],
            "pending_request_mismatch",
        )
        execution = row.get("execution") or {}
        _require(
            not execution.get("calls") and not execution.get("queries"),
            "pending_request_executed",
        )
        remaining = (
            _parse_time(row["deadline_at"], "ledger_invalid") - now
        ).total_seconds()
        if resume["mode"] == "drain-expired":
            _require(remaining <= 0, "request_not_expired")
            if row.get("result"):
                pointer = row["result"]
                name = (
                    OBJECT_PREFIX + f"requests/{rid}/results/{pointer['digest']}.json"
                )
                terminal, _terminal_raw, _ = _read_object_json(
                    clients,
                    name,
                    generation=pointer["generation"],
                    code="result_mismatch",
                )
                _require(
                    _sha256(canonical_bytes(terminal)) == pointer["digest"],
                    "result_mismatch",
                )
                _require(terminal.get("request_id") == rid, "result_mismatch")
                _require(
                    terminal.get("state") in ("unavailable", "held"), "result_mismatch"
                )
                for key in (
                    "request_digest",
                    "intake_digest",
                    "policy_digest",
                    "deployment_digest",
                ):
                    _require(terminal.get(key) == row[key], "result_mismatch")
        else:
            _require(not row.get("result"), "pending_request_mismatch")
            _require(
                remaining >= RECOVERY_REMAINING_SECONDS, "insufficient_remaining_time"
            )
        expected["requests"][rid] = row
        expected["reserved_microusd"] += row["reserved_microusd"]
        _require(len(tasks) == 1, "task_mismatch")
        task = tasks[0]
        _require(
            task.get("name", "").endswith("/tasks/question_" + rid.replace("-", "")),
            "task_mismatch",
        )
        _require(task.get("dispatchCount", 0) == 0, "task_mismatch")
        _require(task.get("dispatchDeadline") == DISPATCH_DEADLINE, "task_mismatch")
        try:
            invocation = json.loads(
                base64.b64decode(task["httpRequest"]["body"], validate=True)
            )
        except (KeyError, TypeError, ValueError):
            _refuse("task_mismatch")
        _require(
            invocation
            == {
                "contract_version": INVOCATION_CONTRACT,
                "request_id": rid,
                **{
                    key: row[key]
                    for key in (
                        "request_digest",
                        "intake_digest",
                        "policy_digest",
                        "deployment_digest",
                    )
                },
            },
            "task_mismatch",
        )
        intake_name = OBJECT_PREFIX + f"requests/{rid}/intake.json"
        intake, _intake_raw, intake_generation = _read_object_json(
            clients,
            intake_name,
            generation=row["intake_generation"],
            code="intake_mismatch",
        )
        _require(intake_generation == row["intake_generation"], "intake_mismatch")
        _require(intake.get("contract_version") == INTAKE_CONTRACT, "intake_mismatch")
        intake_rest = {
            key: value for key, value in intake.items() if key != "intake_digest"
        }
        _require(
            _sha256(canonical_bytes(intake_rest))
            == intake.get("intake_digest")
            == row["intake_digest"],
            "intake_mismatch",
        )
        ref = intake.get("parent_context_ref") or {}
        _require(
            ref.get("parent", {}).get("request_id") == resume["parent_request"],
            "intake_mismatch",
        )
        _require(
            ref.get("anchor", {}).get("request_id") == resume["anchor_request"],
            "intake_mismatch",
        )
        capsule_name = OBJECT_PREFIX + f"requests/{rid}/parent_context.json"
        capsule, _capsule_raw, _ = _read_object_json(
            clients,
            capsule_name,
            generation=ref.get("context_generation"),
            code="capsule_mismatch",
        )
        _require(
            _sha256(canonical_bytes(capsule)) == ref.get("context_digest"),
            "capsule_mismatch",
        )
        _require(capsule.get("child_request_id") == rid, "capsule_mismatch")
        _require(
            capsule.get("child_request_digest") == row["request_digest"],
            "capsule_mismatch",
        )
        _require(
            capsule.get("parent") == ref.get("parent")
            and capsule.get("anchor") == ref.get("anchor"),
            "capsule_mismatch",
        )
        details.update(
            {
                "pending_row": row,
                "remaining_seconds": remaining,
                "intake_generation": intake_generation,
                "intake": intake,
                "capsule": capsule,
            }
        )
    _require(control == expected, "ledger_changed")
    return details


def _resume(args, clients, manifest, manifest_sha256, now):
    plan, plan_sha256 = _load_plan(args.plan, "resume")
    _require(
        plan["resource_manifest_sha256"] == manifest_sha256,
        "resource_manifest_digest_mismatch",
    )
    _check_authority_window(plan["authority"], now)
    _require_absent(args.output)
    binding = plan["binding"]
    tag = plan["expected"]["tag"]
    resume = plan["resume"]
    expectation = plan["expectation"]
    _require(expectation.get("kind") in {"release", "rollback"}, "plan_invalid")
    policy = None
    if resume["mode"] != "empty":
        policy = _resolve_policy(clients, binding)
    finished_at = _parse_time(plan["route"]["finished_at"], "plan_invalid")
    preflight = _resume_preflight(
        clients,
        manifest,
        binding,
        tag,
        resume,
        finished_at,
        now,
        expectation=expectation,
        policy=policy,
    )
    _require(
        canonical_digest(preflight["before"]) == plan["before_sha256"],
        "before_state_changed",
    )
    if resume["mode"] == "recovery":
        _require(
            preflight["remaining_seconds"] >= RESUME_REMAINING_SECONDS,
            "insufficient_remaining_time",
        )
    record = {
        "contract_version": RECEIPT_CONTRACT,
        "kind": "resume",
        "state": "resume_attempt_started",
        "plan_sha256": plan_sha256,
        "started_at": _stamp(now),
        "drain_seconds": preflight["drain_seconds"],
        "binding": binding,
        "queue_before": preflight["queue"],
        "tasks_before": preflight["tasks"],
        "ledger_generation": preflight["ledger_generation"],
        "pending_request": resume["pending_request"],
        "drain_expired": resume["mode"] == "drain-expired",
        "prior_requests_preserved": True,
        "reserved_microusd": preflight["reserved_microusd"],
        "pre_resume_checked_at": _stamp(clients["clock"].now()),
        "limitation": (
            "Final queue, task and ledger checks narrow concurrency exposure; queue resume "
            "is not atomic with ledger admission."
        ),
    }
    for key in ("intake_generation", "intake", "capsule"):
        if key in preflight:
            record[key] = preflight[key]
    _write_once(args.output, record)
    try:
        _guard(manifest, QUEUE_RESOURCE, "write")
        clients["tasks"].resume(QUEUE_API_NAME)
        record["queue_after"] = clients["tasks"].get_queue(QUEUE_API_NAME)
        running = (record["queue_after"] or {}).get("state") == "RUNNING"
        record["state"] = "running_verified" if running else "resume_unproven"
    except Exception as error:
        record["state"] = "resume_unproven"
        record["error"] = f"{type(error).__name__}: {error}"
    record["finished_at"] = _stamp(clients["clock"].now())
    _rewrite(args.output, record)
    code = 0 if record["state"] == "running_verified" else 1
    return code, {
        "state": record["state"],
        "pending_request": resume["pending_request"],
        "output": str(args.output),
    }


# Rollback


def _tag_from_environment(entries):
    env = {item.get("name"): item.get("value") for item in entries}
    url = env.get("GENERAL_QUESTION_WORKER_URL") or ""
    match = re.fullmatch(rf"https://({TAG_PATTERN})---.+", url)
    _require(match is not None, "rollback_target_unbound")
    return match.group(1), env


def _inspect_target(clients, revision, ledger, active_binding):
    try:
        containers = revision["containers"]
        _require(len(containers) == 1, "rollback_target_unbound")
        image = containers[0]["image"]
        tag, env = _tag_from_environment(containers[0]["env"])
    except (KeyError, TypeError):
        _refuse("rollback_target_unbound")
    deployment_digest = env.get("GENERAL_QUESTION_DEPLOYMENT_DIGEST")
    _require(_hex64(deployment_digest), "rollback_target_unbound")
    _require(deployment_digest in ledger["bindings"], "rollback_target_unbound")
    binding_name = OBJECT_PREFIX + f"deployments/{deployment_digest}/binding.json"
    binding, _, _ = _read_object_json(
        clients, binding_name, code="rollback_target_unbound"
    )
    try:
        binding = validate_question_deployment(binding)
    except QuestionStoreError:
        _refuse("rollback_incompatible_schema")
    _require(
        binding["deployment_digest"] == deployment_digest,
        "rollback_incompatible_schema",
    )
    _require(image == image_reference(binding), "rollback_incompatible_schema")
    _require(
        env.get("SOURCE_SHA") == binding["lp_commit"], "rollback_incompatible_schema"
    )
    _require(
        binding["revision_name"] == revision["name"].rsplit("/", 1)[-1],
        "rollback_incompatible_schema",
    )
    _require(
        binding["policy_digest"] == ledger["bindings"][deployment_digest],
        "rollback_incompatible_schema",
    )
    policy_name = OBJECT_PREFIX + f"policies/{binding['policy_digest']}/policy.json"
    policy, _, _ = _read_object_json(
        clients, policy_name, code="rollback_incompatible_schema"
    )
    try:
        policy = validate_question_policy(policy)
    except ValueError:
        _refuse("rollback_incompatible_schema")
    _require(
        policy["policy_digest"] == binding["policy_digest"],
        "rollback_incompatible_schema",
    )
    for field in (
        "sdk_version",
        "worker_path",
        "canonical_service_audience",
        "service_account_email",
        "service_name",
    ):
        _require(
            binding[field] == active_binding[field], "rollback_incompatible_runtime"
        )
    verify_revision(revision, binding, tag, earlier=True)
    return binding, policy, tag


def _active_binding(clients, ledger):
    name = (
        OBJECT_PREFIX + f"deployments/{ledger['active_deployment_digest']}/binding.json"
    )
    binding, _, _ = _read_object_json(clients, name, code="active_binding_unavailable")
    try:
        return validate_question_deployment(binding)
    except QuestionStoreError:
        _refuse("active_binding_unavailable")


def _rollback_plan(args, clients, manifest, manifest_sha256, now):
    _require(
        isinstance(args.target_revision, str)
        and _REVISION.fullmatch(args.target_revision),
        "rollback_target_invalid",
    )
    authority, authority_sha256 = _load_authority(
        args.authority, "rollback", manifest_sha256, now
    )
    _require_absent(args.output)
    before, native = _capture_before(clients, manifest)
    ledger = native["ledger"]["value"]
    classification = _classify_requests(ledger, now)
    try:
        _require(not classification["in_flight"], "rollback_in_flight_request")
        revision = clients["run"].get_revision(
            SERVICE_API_NAME + "/revisions/" + args.target_revision
        )
        _require(revision is not None, "rollback_target_missing")
        active = _active_binding(clients, ledger)
        binding, policy, tag = _inspect_target(clients, revision, ledger, active)
        _require(
            binding["deployment_digest"] != ledger["active_deployment_digest"],
            "rollback_target_already_active",
        )
    except Refusal as error:
        refusal = {
            "contract_version": PLAN_CONTRACT,
            "kind": "rollback",
            "state": "refused",
            "error": str(error),
            "created_at": _stamp(now),
            "target_revision": args.target_revision,
            "classification": classification,
            "before": before,
            "forward_recovery_plan": _forward_recovery_plan(ledger),
        }
        _write_once(args.output, refusal)
        raise
    plan = _base_plan(
        "rollback",
        now=now,
        manifest_sha256=manifest_sha256,
        authority=authority,
        authority_sha256=authority_sha256,
    )
    plan.update(
        {
            "target": _expected_block(binding, tag),
            "binding": binding,
            "policy_digest": policy["policy_digest"],
            "current": {
                "deployment_digest": ledger["active_deployment_digest"],
                "policy_digest": ledger["bindings"][ledger["active_deployment_digest"]],
                "revision_name": active["revision_name"],
            },
            "classification": classification,
            "admissions_pause": _ADMISSIONS_PAUSE_GAP,
            "before": before,
            "before_sha256": canonical_digest(before),
            "changes": [
                *(
                    []
                    if native["queue"]["state"] == "PAUSED"
                    else [
                        {
                            "operation": "pause_queue",
                            "resource": QUEUE_RESOURCE,
                            "action": "write",
                            "queue": QUEUE_API_NAME,
                            "queue_state_before": native["queue"]["state"],
                        }
                    ]
                ),
                {
                    "operation": "activate_binding",
                    "resource": BUCKET_RESOURCE,
                    "action": "write",
                    "object": LEDGER_OBJECT,
                    "if_generation_match": int(native["ledger"]["generation"]),
                    "active_deployment_digest": binding["deployment_digest"],
                },
                {
                    "operation": "route_traffic",
                    "resource": SERVICE_RESOURCE,
                    "action": "deploy",
                    "traffic": traffic_targets(binding, tag),
                },
            ],
        }
    )
    _write_once(args.output, _seal(plan))
    return 0, {"state": "planned", "kind": "rollback", "output": str(args.output)}


def _resume_eligibility(tasks, binding):
    blocked = []
    for task in tasks:
        try:
            invocation = json.loads(
                base64.b64decode(task["httpRequest"]["body"], validate=True)
            )
            digest = invocation.get("deployment_digest")
        except (KeyError, TypeError, ValueError):
            digest = None
        if digest != binding["deployment_digest"]:
            blocked.append(task.get("name"))
    return {
        "eligible": not blocked,
        "blocked_tasks": blocked,
        "reason": "all queued tasks target the routed binding"
        if not blocked
        else "queued tasks target another binding; keep the queue paused",
    }


def _rollback_apply(args, clients, manifest, manifest_sha256, now):
    plan, plan_sha256 = _load_plan(args.plan, "rollback")
    _require(
        plan["resource_manifest_sha256"] == manifest_sha256,
        "resource_manifest_digest_mismatch",
    )
    _check_authority_window(plan["authority"], now)
    _require(type(plan.get("admissions_pause")) is dict, "plan_invalid")
    binding = plan["binding"]
    tag = plan["target"]["tag"]
    receipt = {
        "contract_version": RECEIPT_CONTRACT,
        "kind": "rollback",
        "state": "started",
        "plan_sha256": plan_sha256,
        "idempotency_key": plan["idempotency_key"],
        "started_at": _stamp(now),
        "binding": binding,
        "target": plan["target"],
        "admissions_pause": plan["admissions_pause"],
        "operations": {},
    }
    _write_once(args.output, receipt)

    def finish(state, code, error=None, ledger=None, route=None):
        receipt["state"] = state
        if error is not None:
            receipt["error"] = error
        if ledger is not None and code != 0:
            receipt["forward_recovery_plan"] = _forward_recovery_plan(
                ledger, route=route
            )
        receipt["finished_at"] = _stamp(clients["clock"].now())
        _rewrite(args.output, receipt)
        return code, {
            "state": state,
            "output": str(args.output),
            **({"error": error} if error else {}),
        }

    ledger_value = None
    route = None
    try:
        before, native = _capture_before(clients, manifest)
        ledger_value = native["ledger"]["value"]
        if canonical_digest(before) != plan["before_sha256"]:
            receipt["observed_before"] = before
            return finish("refused", 1, "before_state_changed", ledger_value)
        classification = _classify_requests(ledger_value, now)
        if classification["in_flight"]:
            return finish("refused", 1, "rollback_in_flight_request", ledger_value)
        receipt["classification"] = classification
        operations = [
            change.get("operation") if type(change) is dict else None
            for change in plan["changes"]
        ]
        if operations not in (list(ROLLBACK_CHANGES), list(ROLLBACK_CHANGES[1:])):
            return finish("refused", 1, "rollback_change_order_invalid", ledger_value)
        if operations[0] != "pause_queue" and native["queue"]["state"] != "PAUSED":
            return finish("refused", 1, "queue_not_paused", ledger_value)
        requests_before = canonical_bytes(ledger_value["requests"])
        for change in plan["changes"]:
            if change["operation"] == "pause_queue":
                _guard(manifest, change["resource"], change["action"])
                clients["tasks"].pause(QUEUE_API_NAME)
                paused = clients["tasks"].get_queue(QUEUE_API_NAME)
                receipt["queue_paused"] = paused
                _require(
                    paused is not None and paused["state"] == "PAUSED",
                    "queue_not_paused",
                )
                paused_ledger = _read_ledger(clients)
                _require(
                    paused_ledger["generation"] == native["ledger"]["generation"],
                    "before_state_changed",
                )
                classification = _classify_requests(paused_ledger["value"], now)
                if classification["in_flight"]:
                    return finish(
                        "refused", 1, "rollback_in_flight_request", ledger_value
                    )
                receipt["classification"] = classification
            elif change["operation"] == "activate_binding":
                _require(
                    change["if_generation_match"]
                    == int(native["ledger"]["generation"]),
                    "before_state_changed",
                )
                activated = copy.deepcopy(ledger_value)
                activated["bindings"].setdefault(
                    binding["deployment_digest"], binding["policy_digest"]
                )
                _require(
                    activated["bindings"][binding["deployment_digest"]]
                    == binding["policy_digest"],
                    "rollback_incompatible_schema",
                )
                activated["active_deployment_digest"] = binding["deployment_digest"]
                _validate_ledger(activated)
                _guard(manifest, change["resource"], change["action"])
                try:
                    written = clients["objects"].create(
                        LEDGER_OBJECT,
                        canonical_bytes(activated),
                        if_generation_match=change["if_generation_match"],
                    )
                except Exception as error:
                    receipt["activation"] = {
                        "state": "failed",
                        "error": f"{type(error).__name__}: {error}",
                    }
                    return finish("refused", 1, "stale_ledger_generation", ledger_value)
                receipt["activation"] = {
                    "state": "written",
                    "if_generation_match": change["if_generation_match"],
                    "generation": str(written["generation"]),
                    "sha256": _sha256(canonical_bytes(activated)),
                    "active_deployment_digest": binding["deployment_digest"],
                }
                ledger_value = activated
            elif change["operation"] == "route_traffic":
                activation = receipt.get("activation") or {}
                confirmed = _read_ledger(clients)
                # Each clause holds on its own. A create answered by a replayed
                # success carrying the generation the object already had leaves
                # the activation written and the generations equal while the
                # bytes never landed, and only the digest clause sees that the
                # ledger never moved. A writer that appends to the ledger after
                # the activation leaves the active digest right and only the
                # generation wrong. Both route production traffic against a
                # ledger this run never wrote if their clause is removed.
                _require(
                    activation.get("state") == "written"
                    and confirmed["generation"] == activation.get("generation")
                    and confirmed["value"]["active_deployment_digest"]
                    == binding["deployment_digest"],
                    "rollback_activation_unconfirmed",
                )
                service = clients["run"].get_service(SERVICE_API_NAME)
                _require(service is not None, "service_unavailable")
                _guard(manifest, change["resource"], change["action"])
                route = {
                    "operation": None,
                    "state": "unknown",
                    "target_revision": plan["target"]["revision_name"],
                    "activation_generation": activation.get("generation"),
                }
                operation = clients["run"].update_traffic(
                    SERVICE_API_NAME,
                    etag=service["etag"],
                    traffic=change["traffic"],
                    idempotency_key=plan["idempotency_key"],
                )
                result = _wait(clients, args, operation)
                receipt["operations"]["route_traffic"] = result
                route = {
                    **route,
                    "operation": result["operation"],
                    "state": result["state"],
                }
                if result["state"] == "unproven":
                    return finish(
                        "unproven_requires_reconciliation",
                        1,
                        None,
                        ledger_value,
                        route=route,
                    )
                if result["state"] != "succeeded":
                    return finish(
                        "failed_or_unproven_requires_native_reconciliation",
                        1,
                        "route_failed",
                        ledger_value,
                        route=route,
                    )
            else:
                _refuse("plan_invalid")
        service = clients["run"].get_service(SERVICE_API_NAME)
        expected_traffic = traffic_targets(binding, tag)
        observed = [
            {key: item.get(key) for key in expected_traffic[0]}
            for item in (service or {}).get("trafficStatuses", [])
        ]
        _require(
            (service or {}).get("traffic") == expected_traffic
            and observed == expected_traffic,
            "traffic_unverified",
        )
        ledger_after = _read_ledger(clients)
        _require(
            ledger_after["generation"] == receipt["activation"]["generation"],
            "ledger_changed",
        )
        _require(
            ledger_after["value"]["active_deployment_digest"]
            == binding["deployment_digest"],
            "ledger_incompatible",
        )
        _require(
            canonical_bytes(ledger_after["value"]["requests"]) == requests_before,
            "requests_changed",
        )
        _require(
            ledger_after["value"]["reserved_microusd"]
            == ledger_value["reserved_microusd"],
            "requests_changed",
        )
        queue = clients["tasks"].get_queue(QUEUE_API_NAME)
        _require(queue is not None and queue["state"] == "PAUSED", "queue_not_paused")
        receipt["after"] = {
            "service": redacted_service(service),
            "queue": queue,
            "ledger": {
                "generation": ledger_after["generation"],
                "sha256": ledger_after["sha256"],
            },
        }
        receipt["resume_eligibility"] = _resume_eligibility(
            _listed_tasks(clients), binding
        )
        receipt["requests_preserved"] = sorted(ledger_after["value"]["requests"])
        return finish("rollback_verified_queue_paused", 0)
    except Refusal as error:
        return finish(
            "failed_or_unproven_requires_native_reconciliation",
            1,
            str(error),
            ledger_value,
            route=route,
        )
    except Exception as error:
        return finish(
            "failed_or_unproven_requires_native_reconciliation",
            1,
            f"{type(error).__name__}: {error}",
            ledger_value,
            route=route,
        )


# CLI


COMMANDS = {
    "plan": _plan,
    "apply": _apply,
    "verify": _verify,
    "resume": _resume,
    "rollback-plan": _rollback_plan,
    "rollback-apply": _rollback_apply,
}


def _add_common(parser, *, needs_plan):
    parser.add_argument("--output", required=True, help="write-once receipt path")
    parser.add_argument(
        "--adapter", required=True, help="module path and factory, PATH.py:factory"
    )
    parser.add_argument(
        "--adapter-state", default=None, help="opaque value handed to the factory"
    )
    parser.add_argument(
        "--resources", default=None, help="approved resource manifest path"
    )
    parser.add_argument(
        "--resources-sha256",
        default=None,
        help="independently approved manifest digest",
    )
    if needs_plan:
        parser.add_argument(
            "--plan", required=True, help="plan produced by the read-only command"
        )
    parser.add_argument("--deadline-seconds", type=float, default=600.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="release.py", description=__doc__.splitlines()[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="read-only release or resume plan")
    plan.add_argument(
        "--activation",
        default=None,
        help="activation receipt; required for a release plan",
    )
    plan.add_argument("--authority", required=True)
    plan.add_argument("--kind", choices=("release", "resume"), default="release")
    plan.add_argument(
        "--route", default=None, help="verified apply receipt, resume kind only"
    )
    plan.add_argument("--mode", choices=RESUME_MODES, default=None)
    plan.add_argument("--pending-request", default=None)
    plan.add_argument("--parent-request", default=None)
    plan.add_argument("--anchor-request", default=None)
    _add_common(plan, needs_plan=False)
    _add_common(
        commands.add_parser("apply", help="deploy and route the planned revision"),
        needs_plan=True,
    )
    verify = commands.add_parser("verify", help="certify native state against the plan")
    verify.add_argument("--result", required=True, help="apply receipt to reconcile")
    verify.add_argument(
        "--release-index", default=None, help="shared ReleaseIndex to bind"
    )
    _add_common(verify, needs_plan=True)
    _add_common(
        commands.add_parser(
            "resume", help="resume the paused queue from a resume plan"
        ),
        needs_plan=True,
    )
    rollback_plan = commands.add_parser(
        "rollback-plan", help="read-only compatible rollback plan"
    )
    rollback_plan.add_argument("--target-revision", required=True)
    rollback_plan.add_argument("--authority", required=True)
    _add_common(rollback_plan, needs_plan=False)
    _add_common(
        commands.add_parser(
            "rollback-apply", help="activate and route the rollback plan"
        ),
        needs_plan=True,
    )
    return parser


def _load_adapter(spec, state):
    _require(isinstance(spec, str) and ":" in spec, "adapter_invalid")
    location, factory = spec.rsplit(":", 1)
    path = Path(location)
    if path.suffix == ".py" and path.is_file():
        loader = importlib.util.spec_from_file_location("release_adapter", path)
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
    else:
        module = importlib.import_module(location)
    clients = getattr(module, factory)(state)
    for name in ("run", "tasks", "objects", "clock"):
        _require(name in clients, "adapter_invalid")
    return clients


def execute(argv, *, clients=None) -> int:
    args = build_parser().parse_args(argv)
    output = getattr(args, "output", None)
    try:
        manifest_path = (
            Path(args.resources)
            if args.resources
            else _HERE.parent / "resource_manifest.json"
        )
        manifest_sha256 = args.resources_sha256 or APPROVED_RESOURCE_MANIFEST_SHA256
        manifest = resource_guard.load_resource_manifest(
            manifest_path, expected_sha256=manifest_sha256
        )
        if clients is None:
            clients = _load_adapter(args.adapter, args.adapter_state)
        now = clients["clock"].now()
        _require(isinstance(now, datetime) and now.tzinfo is not None, "clock_invalid")
        code, summary = COMMANDS[args.command](
            args, clients, manifest, manifest_sha256, now
        )
    except ValueError as error:
        summary = {"state": "refused", "error": str(error), "output": str(output)}
        code = 1
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        summary = {
            "state": "refused",
            "error": f"{type(error).__name__}: {error}",
            "output": str(output),
        }
        code = 1
    print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(execute(sys.argv[1:]))
