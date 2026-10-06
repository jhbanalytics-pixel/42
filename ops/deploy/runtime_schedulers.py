"""Native creation step for the R05 managed schedulers.

plan renders, for each Cloud Scheduler job declared in the reviewed scheduler
file, a describe precheck, one create, one pause and one confirming describe.
Cloud Scheduler's job state is output only, so a job is born enabled and is
paused by its own request; the confirming read is what lets the receipt say
paused rather than assume it. apply runs a plan over the injectable REST
transport and writes a receipt; readback describes each scheduler and diffs it
against the plan. An existing job is never created again and never rewritten,
and one found running is paused with the same pause request and confirmed, so
no receipt reports applied while a scheduler this command touched is live.
None of the three resumes a scheduler, deletes one, rewrites one that exists,
or runs a Cloud Run job: the adapter has no call for any of the last three, and
its one resume call is used by none of them.

resume is a separate command, reached through runtime_scheduler_resume so that
the creation command line keeps no command that activates a schedule. It
resumes the price policy scheduler and nothing else: asked for the daily
scheduler, or any other identifier, it refuses scheduler_resume_unapproved
before anything is sent, because the daily scheduler's activation belongs to
another task and its own gate, and the adapter's resume call refuses every
name but the price policy one as well. Before the resume request it re-derives
the name, the resource and the target from the approved identifiers and re-runs
the digest chain and the resource guard exactly as apply does, describes the
scheduler and requires every compared field to equal the reviewed scheduler
file's entry, with only the documented retry defaults and the service's own
User-Agent header reconciled, and the state to be PAUSED, and reads the target
Cloud Run job,
refusing price_policy_job_absent when it is missing, because a scheduler that
points at a missing job must not go live. A scheduler found already ENABLED
with its fields intact is reported as already_enabled and nothing is sent. It
then sends exactly one resume request and a confirming describe that must read
ENABLED with the fields unchanged, and writes a write once receipt; a refusal
after the resume went out still writes one. resume never creates, pauses,
deletes or rewrites a scheduler, never runs a Cloud Run job and never touches
the daily scheduler. Its dry run renders the same four requests and sends none.

A plan document is evidence, not authority. apply and readback re-derive every
name, resource and target they address from the approved identifiers, re-check
the reviewed digest chain against the files themselves and re-run the resource
guard before any request goes out, because the plan's own digest is one the
module can recompute over an edited document.

That holds for the content of a scheduler as well as for its address. The
schedule, the time zone, the retry configuration, the attempt deadline, the
headers, the method, the token and the target body that reach the wire are
loaded from the reviewed scheduler file through the loader that validates them,
not read out of the plan, and a plan whose copy of a job differs from that file
refuses before anything is sent. The scheduler file is the module constant on
every sending path, so no caller supplied path can move the content anchor. The
runtime file and the manifest are still keyword paths, which the offline proofs
use and the shipped command line never sets; a caller able to pass them is
already running arbitrary code in this process, so that override is latent
rather than a channel a document can reach.

The dry run renders through the same request builder from the same loaded
entries, so the artifact an operator reads before letting apply touch a billing
project is built the way apply builds what it sends rather than echoing the
document. It renders the whole run, both creates and both pauses, and does not
perform the describe precheck, so against a project where a scheduler already
exists apply sends fewer requests than the dry run shows. It never shows fewer
than apply sends.

A refusal raised part way through apply writes a receipt, and the field to read
in that receipt is record, with sent beside it. schedulers names only the jobs
whose create, pause and confirming read all completed, so a scheduler this run
created a moment before the refusal is named in record and sent and is absent
from schedulers. An empty schedulers on a refused receipt does not mean nothing
was created; the receipt says so in its own words as well.

The plan binds the reviewed digest chain of the runtime file, the scheduler
file and the resource manifest, checks each scheduler resource for deploy and
read and each target job for invoke, requires the approved scheduler identity
and the approved twelve hourly price policy cadence, and carries the run.invoker
rows of the approved provisioning delta as the permission proof obligation the
R05 handoff has to observe.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from ops.deploy.resource_guard import assert_allowed, load_resource_manifest
from ops.deploy.runtime_native_adapter import NativeAdapter, NativeError, build_request
from ops.runners.managed_runtime import (
    DAILY_JOB_ID,
    PRICE_POLICY_JOB_ID,
    PROJECT,
    REGION,
    canonical_bytes,
    canonical_sha256,
    load_runtime_configuration,
    load_scheduler_configuration,
)

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILE = ROOT / "infra" / "runtime" / "daily-staging.json"
SCHEDULER_FILE = ROOT / "infra" / "runtime" / "scheduler-staging.json"
RESOURCE_MANIFEST = ROOT / "ops" / "deploy" / "resource_manifest.json"
IAM_DELTA = ROOT / "ops" / "deploy" / "iam_delta_v1.json"
PLAN_CONTRACT = "42_runtime_schedulers_plan_v1"
RECEIPT_CONTRACT = "42_runtime_schedulers_receipt_v1"
READBACK_CONTRACT = "42_runtime_schedulers_readback_v1"
RESUME_RECEIPT_CONTRACT = "42_runtime_schedulers_resume_receipt_v1"
DELTA_CONTRACT = "42_iam_delta_v1"
PARENT = f"projects/{PROJECT}/locations/{REGION}"
PAUSED = "PAUSED"
ENABLED = "ENABLED"
RESUMABLE_JOB_IDS = (PRICE_POLICY_JOB_ID,)
INVOKER_ROLE = "roles/run.invoker"
PRICE_POLICY_SCHEDULE = "0 */12 * * *"
OPERATOR_NOTE = (
    "Read record and sent, not schedulers. schedulers names only the jobs whose "
    "create, pause and confirming read all completed; a scheduler created a "
    "moment before this refusal is named in sent and record and is absent from "
    "schedulers, so an empty schedulers here does not mean nothing was created."
)
COMPARED_FIELDS = (
    "name",
    "description",
    "schedule",
    "timeZone",
    "attemptDeadline",
    "retryConfig",
    "httpTarget",
)
# What the Cloud Scheduler v1 Job resource fills in or leaves out on its own, and
# nothing else. A describe returns the job as the service stores it: the proto3
# JSON mapping drops a scalar at its zero value, so retryCount 0 comes back
# absent, and the service stores the documented default of three retry fields
# the create left out. Each value below is the default the v1 reference states
# for that field; the comparison fills it in on either side where it is absent,
# so an absent field equals its stated default and any other value is still a
# named difference. Every other field, maxRetryDuration, attemptDeadline and the
# token scope among them, is compared as it stands. See
# https://cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs
_RETRY_DEFAULTS = {
    # RetryConfig.retryCount: "The default value of retryCount is zero."
    "retryCount": 0,
    # RetryConfig.minBackoffDuration: "The default value of this field is 5 seconds."
    "minBackoffDuration": "5s",
    # RetryConfig.maxBackoffDuration: "The default value of this field is 1 hour."
    "maxBackoffDuration": "3600s",
    # RetryConfig.maxDoublings: "The default value of this field is 5."
    "maxDoublings": 5,
}
# HttpTarget.headers: "User-Agent: This will be set to "Google-Cloud-Scheduler"."
# That is the one header the service is documented to store on a job it did not
# receive, so it is the one allowed, and only with that exact name and value and
# only when the reviewed file sets no User-Agent of its own. Every other header on
# the live job that the file does not carry stays a difference.
_SERVER_HEADERS = {"User-Agent": "Google-Cloud-Scheduler"}
_ABSENT = object()
_ADAPTER_STATES = ("owner-gcloud", "adc")
_APPROVAL_KEYS = {"applied", "approved_at", "approved_by", "state"}
_BINDING_KEYS = {"condition", "member", "purpose", "resource", "role"}
_MAX_INPUT_BYTES = 4 * 1024 * 1024
_SCHEDULER_RESOURCE = "//cloudscheduler.googleapis.com/" + PARENT + "/jobs/"
_RUN_JOB_RESOURCE = "//run.googleapis.com/" + PARENT + "/jobs/"
_RUN_TARGET = "https://run.googleapis.com/v2/" + PARENT + "/jobs/"
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_PLAN_KEYS = {
    "contract_version",
    "created_at",
    "project",
    "location",
    "parent",
    "scheduler_identity",
    "runtime_configuration_sha256",
    "scheduler_configuration_sha256",
    "resource_manifest_sha256",
    "iam_delta",
    "schedulers",
    "operations",
    "plan_sha256",
}
_ENTRY_KEYS = {
    "name",
    "job",
    "expected_state",
    "scheduler_resource",
    "run_job_resource",
    "invoker_bindings",
}
_OPERATION_KEYS = {"kind", "scheduler", "request"}
_OPERATION_KINDS = ["describe", "create", "pause", "confirm"] * 2


def _refuse(code):
    raise ValueError(code)


def _read_bytes(path, code):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_INPUT_BYTES + 1)
    except (OSError, TypeError, ValueError):
        _refuse(code)
    if len(raw) > _MAX_INPUT_BYTES:
        _refuse(code)
    return raw


def file_sha256(path) -> str:
    return hashlib.sha256(_read_bytes(path, "input_unreadable")).hexdigest()


def _unique_object(pairs, code):
    value = {}
    for key, item in pairs:
        if key in value:
            _refuse(code)
        value[key] = item
    return value


def _load_json(path, code):
    raw = _read_bytes(path, code)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_object(pairs, code),
            parse_constant=lambda _value: _refuse(code),
        )
    except (UnicodeError, json.JSONDecodeError):
        _refuse(code)
    if type(value) is not dict:
        _refuse(code)
    return value, hashlib.sha256(raw).hexdigest()


def _approval(delta):
    approval = delta.get("approval")
    if type(approval) is not dict or set(approval) != _APPROVAL_KEYS:
        _refuse("iam_delta_invalid")
    if approval["state"] != "approved":
        _refuse("iam_delta_unapproved")
    if any(
        not isinstance(approval[key], str) or not approval[key]
        for key in ("approved_at", "approved_by")
    ):
        _refuse("iam_delta_invalid")
    return {key: approval[key] for key in ("approved_at", "approved_by", "state")}


def _invoker_bindings(delta, job_resource, member):
    """The approved run.invoker rows the scheduler identity needs on one job."""
    if delta.get("contract_version") != DELTA_CONTRACT:
        _refuse("iam_delta_invalid")
    rows = delta.get("bindings")
    if type(rows) is not list:
        _refuse("iam_delta_invalid")
    selected = []
    for row in rows:
        if type(row) is not dict or set(row) != _BINDING_KEYS:
            _refuse("iam_delta_invalid")
        if row["resource"] != job_resource or row["member"] != member:
            continue
        if row["role"] != INVOKER_ROLE:
            continue
        if row["condition"] is not None:
            _refuse("binding_condition_unsupported")
        selected.append({"member": row["member"], "role": row["role"]})
    if not selected:
        _refuse("invoker_binding_missing")
    return selected


def _scheduler_identity(resources):
    identity = resources["identities"]["scheduler"]
    return identity.rsplit("/", 1)[1]


def _create_body(job):
    """Cloud Scheduler reports state; it is never sent, so the create body drops it."""
    return {key: value for key, value in job.items() if key != "state"}


def _operations(job_id, job):
    name = job["name"]
    return [
        {
            "kind": "describe",
            "scheduler": job_id,
            "request": _rendered("get_scheduler", name=name),
        },
        {
            "kind": "create",
            "scheduler": job_id,
            "request": _rendered(
                "create_scheduler", parent=PARENT, job=_create_body(job)
            ),
        },
        {
            "kind": "pause",
            "scheduler": job_id,
            "request": _rendered("pause_scheduler", name=name),
        },
        {
            "kind": "confirm",
            "scheduler": job_id,
            "request": _rendered("get_scheduler", name=name),
        },
    ]


def _sealed_operations(sealed):
    """Every request apply would send, rendered from the loaded entries."""
    operations = []
    for job_id in sorted(sealed):
        operations.extend(_operations(job_id, sealed[job_id]))
    return operations


def _rendered(call, **kwargs):
    request = build_request(call, **kwargs)
    rendered = {key: request[key] for key in ("call", "method", "url")}
    if "json" in request:
        rendered["body"] = request["json"]
    return rendered


def render_plan(
    *,
    runtime_path=RUNTIME_FILE,
    scheduler_path=SCHEDULER_FILE,
    manifest_path=RESOURCE_MANIFEST,
    delta_path=IAM_DELTA,
    now=None,
) -> dict:
    config = load_runtime_configuration(runtime_path)
    schedulers = load_scheduler_configuration(
        scheduler_path, expected_sha256=config["scheduler_configuration_sha256"]
    )
    resources = load_resource_manifest(
        manifest_path, expected_sha256=config["resource_manifest_sha256"]
    )
    delta, delta_sha256 = _load_json(delta_path, "iam_delta_invalid")
    approval = _approval(delta)
    identity = _scheduler_identity(resources)
    member = "serviceAccount:" + identity
    planned = {}
    operations = []
    for job_id in sorted(schedulers["schedulers"]):
        job = schedulers["schedulers"][job_id]["job"]
        if job["httpTarget"]["oauthToken"]["serviceAccountEmail"] != identity:
            _refuse("scheduler_identity_mismatch")
        if job_id == PRICE_POLICY_JOB_ID and job["schedule"] != PRICE_POLICY_SCHEDULE:
            _refuse("price_policy_schedule_unapproved")
        scheduler_resource = _SCHEDULER_RESOURCE + job_id
        job_resource = _RUN_JOB_RESOURCE + job_id
        assert_allowed(scheduler_resource, "deploy", resources)
        assert_allowed(scheduler_resource, "read", resources)
        assert_allowed(job_resource, "invoke", resources)
        planned[job_id] = {
            "name": job["name"],
            "job": {key: job[key] for key in COMPARED_FIELDS},
            "expected_state": PAUSED,
            "scheduler_resource": scheduler_resource,
            "run_job_resource": job_resource,
            "invoker_bindings": _invoker_bindings(delta, job_resource, member),
        }
        operations.extend(_operations(job_id, job))
    plan = {
        "contract_version": PLAN_CONTRACT,
        "created_at": (now or (lambda: datetime.now(UTC).isoformat()))(),
        "project": PROJECT,
        "location": REGION,
        "parent": PARENT,
        "scheduler_identity": identity,
        "runtime_configuration_sha256": file_sha256(runtime_path),
        "scheduler_configuration_sha256": config["scheduler_configuration_sha256"],
        "resource_manifest_sha256": config["resource_manifest_sha256"],
        "iam_delta": {**approval, "sha256": delta_sha256},
        "schedulers": planned,
        "operations": operations,
    }
    return {**plan, "plan_sha256": canonical_sha256(plan)}


def verify_plan(plan) -> str:
    if type(plan) is not dict or "plan_sha256" not in plan:
        _refuse("plan_invalid")
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return canonical_sha256(body)


def _hex_64(value):
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _scheduler_name(job_id):
    """The one resource name this package will ever address for a scheduler."""
    return f"{PARENT}/jobs/{job_id}"


def _validated_entry(job_id, entry):
    """One planned scheduler, re-derived rather than read from the document."""
    if type(entry) is not dict or set(entry) != _ENTRY_KEYS:
        _refuse("plan_invalid")
    if entry["name"] != _scheduler_name(job_id):
        _refuse("scheduler_name_unpinned")
    if entry["expected_state"] != PAUSED:
        _refuse("plan_invalid")
    if entry["scheduler_resource"] != _SCHEDULER_RESOURCE + job_id:
        _refuse("plan_invalid")
    if entry["run_job_resource"] != _RUN_JOB_RESOURCE + job_id:
        _refuse("plan_invalid")
    job = entry["job"]
    if type(job) is not dict or set(job) != set(COMPARED_FIELDS):
        _refuse("plan_invalid")
    if job["name"] != entry["name"]:
        _refuse("scheduler_name_unpinned")
    rows = entry["invoker_bindings"]
    if type(rows) is not list or not rows:
        _refuse("plan_invalid")
    for row in rows:
        if type(row) is not dict or set(row) != {"member", "role"}:
            _refuse("plan_invalid")
        if not isinstance(row["member"], str) or row["role"] != INVOKER_ROLE:
            _refuse("plan_invalid")


def _validated_plan(plan):
    """Every field this module acts on, checked before anything is addressed."""
    if type(plan) is not dict or set(plan) != _PLAN_KEYS:
        _refuse("plan_invalid")
    if plan["contract_version"] != PLAN_CONTRACT:
        _refuse("plan_invalid")
    try:
        sealed = verify_plan(plan)
    except (TypeError, ValueError):
        _refuse("plan_invalid")
    if plan["plan_sha256"] != sealed:
        _refuse("plan_digest_mismatch")
    if plan["project"] != PROJECT or plan["location"] != REGION:
        _refuse("plan_invalid")
    if plan["parent"] != PARENT:
        _refuse("plan_invalid")
    if any(
        not _hex_64(plan[key])
        for key in (
            "runtime_configuration_sha256",
            "scheduler_configuration_sha256",
            "resource_manifest_sha256",
        )
    ):
        _refuse("plan_invalid")
    planned = plan["schedulers"]
    if type(planned) is not dict or set(planned) != {DAILY_JOB_ID, PRICE_POLICY_JOB_ID}:
        _refuse("plan_invalid")
    for job_id in sorted(planned):
        _validated_entry(job_id, planned[job_id])
    operations = plan["operations"]
    if type(operations) is not list or len(operations) != len(_OPERATION_KINDS):
        _refuse("plan_invalid")
    for operation in operations:
        if type(operation) is not dict or set(operation) != _OPERATION_KEYS:
            _refuse("plan_invalid")
        scheduler = operation["scheduler"]
        # A JSON array or object here is unhashable: name the refusal rather
        # than letting a TypeError escape the vocabulary as a traceback.
        if not isinstance(scheduler, str) or scheduler not in planned:
            _refuse("plan_invalid")
        if type(operation["request"]) is not dict:
            _refuse("plan_invalid")
    if [operation["kind"] for operation in operations] != _OPERATION_KINDS:
        _refuse("plan_invalid")
    return plan


def _sealed_jobs(scheduler_digest):
    """Every scheduler job as the reviewed file holds it, validated again here.

    load_scheduler_configuration runs _validate_scheduler_entry over each entry,
    which is what pins the time zone, the cron grammar, the retry configuration,
    the attempt deadline, the closed key set on the http target, the method, the
    headers, the token keys and scope and the empty target body. Running it on
    the sending path is what makes those rules bind what is created rather than
    only what was planned.
    """
    sealed = load_scheduler_configuration(
        SCHEDULER_FILE, expected_sha256=scheduler_digest
    )
    return {job_id: entry["job"] for job_id, entry in sealed["schedulers"].items()}


def _bound(plan, runtime_path, manifest_path):
    """The reviewed digest chain, the resource guard and the sealed entries.

    The plan records these digests; recording them is not proof, so every
    command that addresses a live scheduler re-derives them from the reviewed
    files and re-runs the resource guard over each resource it is about to touch.
    The scheduler entries themselves are re-derived the same way, and the plan's
    copy of a job is compared field by field against the file rather than
    trusted, so a resealed document cannot choose what is created or read back.
    """
    if file_sha256(runtime_path) != plan["runtime_configuration_sha256"]:
        _refuse("runtime_configuration_digest_mismatch")
    config = load_runtime_configuration(runtime_path)
    scheduler_digest = config["scheduler_configuration_sha256"]
    if scheduler_digest != plan["scheduler_configuration_sha256"]:
        _refuse("scheduler_configuration_digest_mismatch")
    if config["resource_manifest_sha256"] != plan["resource_manifest_sha256"]:
        _refuse("resource_manifest_digest_mismatch")
    resources = load_resource_manifest(
        manifest_path, expected_sha256=plan["resource_manifest_sha256"]
    )
    identity = _scheduler_identity(resources)
    # Both sets are closed to the two approved identifiers, the plan's by
    # _validated_plan and the file's by the scheduler loader, so the entry for
    # each planned job is present.
    sealed = _sealed_jobs(scheduler_digest)
    for job_id in sorted(plan["schedulers"]):
        assert_allowed(_SCHEDULER_RESOURCE + job_id, "deploy", resources)
        assert_allowed(_SCHEDULER_RESOURCE + job_id, "read", resources)
        assert_allowed(_RUN_JOB_RESOURCE + job_id, "invoke", resources)
        target = plan["schedulers"][job_id]["job"]["httpTarget"]
        if type(target) is not dict:
            _refuse("plan_invalid")
        if target.get("uri") != _RUN_TARGET + job_id + ":run":
            _refuse("scheduler_target_unpinned")
        token = target.get("oauthToken")
        if type(token) is not dict or token.get("serviceAccountEmail") != identity:
            _refuse("scheduler_identity_mismatch")
        if plan["schedulers"][job_id]["job"] != _compared(sealed[job_id]):
            _refuse("scheduler_entry_unsealed")
    return resources, sealed


def _compared(job):
    """The fields the plan carries and the readback diffs, taken from one job."""
    return {key: job[key] for key in COMPARED_FIELDS}


def _observed_state(job, name):
    """The state of the scheduler that answered, and only if it is the one asked."""
    if type(job) is not dict or not isinstance(job.get("state"), str):
        _refuse("scheduler_readback_invalid")
    if job.get("name") != name:
        _refuse("scheduler_name_unconfirmed")
    return job["state"]


def _paused(adapter, name, absent_code, sent):
    """Pause one scheduler and return the state its own confirming read shows."""
    sent.append("pause_scheduler")
    status, paused = adapter.perform("pause_scheduler", name=name)
    adapter.require_ok("pause_scheduler", status, paused)
    confirmed = adapter.get("get_scheduler", name=name)
    if confirmed is None:
        _refuse(absent_code)
    return _observed_state(confirmed, name)


def apply_plan(
    plan,
    *,
    adapter,
    dry_run=False,
    runtime_path=RUNTIME_FILE,
    manifest_path=RESOURCE_MANIFEST,
) -> dict:
    """Create each absent scheduler and pause it, and pause an existing one that
    is running; nothing is created twice, resumed, deleted or rewritten, and no
    receipt says applied while a scheduler this command touched is still live."""
    plan = _validated_plan(plan)
    _resources, sealed = _bound(plan, runtime_path, manifest_path)
    if dry_run:
        # Every request the run would make against a project where neither
        # scheduler exists. The describe precheck is not performed here, so a
        # project that already holds one of them takes fewer requests than this
        # renders, never more.
        return {
            "contract_version": RECEIPT_CONTRACT,
            "state": "rendered",
            "plan_sha256": plan["plan_sha256"],
            "requests": [
                operation["request"] for operation in _sealed_operations(sealed)
            ],
        }
    applied = {}
    sent = []
    try:
        for job_id in sorted(plan["schedulers"]):
            name = _scheduler_name(job_id)
            existing = adapter.get("get_scheduler", name=name)
            if existing is None:
                sent.append("create_scheduler")
                status, created = adapter.perform(
                    "create_scheduler", parent=PARENT, job=_create_body(sealed[job_id])
                )
                adapter.require_ok("create_scheduler", status, created)
                state = _paused(adapter, name, "scheduler_absent_after_create", sent)
                result = "created"
            else:
                state = _observed_state(existing, name)
                if state != PAUSED:
                    state = _paused(adapter, name, "scheduler_absent_after_pause", sent)
                    result = "paused"
                else:
                    result = "already_exists"
            applied[job_id] = {"result": result, "state": state}
        if any(entry["state"] != PAUSED for entry in applied.values()):
            _refuse("scheduler_not_paused")
    except (OSError, ValueError, NativeError) as error:
        # A refusal after a request went out still owes an artifact: without one
        # a created scheduler is left on a billing project with nothing naming
        # it. The record carries every request sent and every answer received.
        # A transport that never answered at all is one of those refusals, which
        # is why OSError is caught here beside the two refusal vocabularies: a
        # read timeout, a reset connection, a name resolution failure and a TLS
        # failure each arrive as one, and each can follow a create that landed.
        if sent:
            error.receipt = {
                "contract_version": RECEIPT_CONTRACT,
                "state": "refused",
                "error": str(error),
                "plan_sha256": plan["plan_sha256"],
                "schedulers": applied,
                "sent": list(sent),
                "read_this_first": OPERATOR_NOTE,
                "record": list(adapter.record),
            }
        raise
    return {
        "contract_version": RECEIPT_CONTRACT,
        "state": "applied",
        "plan_sha256": plan["plan_sha256"],
        "schedulers": applied,
        "sent": list(sent),
        "record": list(adapter.record),
    }


def _normalised(job, reviewed):
    """The compared fields of one job with the service's own values filled in.

    reviewed is the reviewed file's job; it decides whether a header the service
    adds is one the file left to the service. Only the retry defaults and the one
    header named above are reconciled, and a field whose shape is not the
    expected one is left as it came so that it differs rather than being coerced.
    """
    value = {field: job.get(field, _ABSENT) for field in COMPARED_FIELDS}
    retry = value["retryConfig"]
    if type(retry) is dict:
        value["retryConfig"] = {**_RETRY_DEFAULTS, **retry}
    target = value["httpTarget"]
    if type(target) is dict:
        target = dict(target)
        headers = target.get("headers")
        allowed = reviewed.get("httpTarget", {}).get("headers", {})
        if type(headers) is dict:
            target["headers"] = {
                name: text
                for name, text in headers.items()
                if name in allowed or _SERVER_HEADERS.get(name) != text
            }
        value["httpTarget"] = target
    return value


def _shown(value):
    return None if value is _ABSENT else value


def _walk(path, expected, observed, found):
    """Every leaf that differs, named by its dotted path, with both values."""
    if type(expected) is dict and type(observed) is dict:
        for key in sorted(set(expected) | set(observed)):
            _walk(
                path + "." + key,
                expected.get(key, _ABSENT),
                observed.get(key, _ABSENT),
                found,
            )
    elif type(expected) is not type(observed) or expected != observed:
        found[path] = {"expected": _shown(expected), "observed": _shown(observed)}


def _compare(expected, observed):
    """The differences that decide the verdict, and the service values accepted.

    Both come from the same walk over the compared fields. differences is taken
    after the documented retry defaults are filled in on both sides and the
    service's User-Agent header is set aside; accepted names every path whose
    raw value differed and that this reconciliation alone settled, with the raw
    values, so the readback shows an operator exactly what it took as the
    service's own.
    """
    found, raw = {}, {}
    normal_expected = _normalised(expected, expected)
    normal_observed = _normalised(observed, expected)
    for field in COMPARED_FIELDS:
        _walk(field, normal_expected[field], normal_observed[field], found)
        _walk(field, expected.get(field, _ABSENT), observed.get(field, _ABSENT), raw)
    accepted = {path: raw[path] for path in sorted(raw) if path not in found}
    return found, accepted


def _differences(expected, observed):
    return _compare(expected, observed)[0]


def read_back(
    plan, *, adapter, runtime_path=RUNTIME_FILE, manifest_path=RESOURCE_MANIFEST
) -> dict:
    plan = _validated_plan(plan)
    _resources, sealed = _bound(plan, runtime_path, manifest_path)
    report = {}
    matched = True
    for job_id in sorted(plan["schedulers"]):
        entry = plan["schedulers"][job_id]
        name = _scheduler_name(job_id)
        try:
            observed = adapter.get("get_scheduler", name=name)
        except NativeError as error:
            report[job_id] = {"result": "refused", "status": error.status}
            matched = False
            continue
        if observed is None:
            report[job_id] = {"result": "absent"}
            matched = False
            continue
        state = _observed_state(observed, name)
        differences, accepted = _compare(_compared(sealed[job_id]), observed)
        if state != entry["expected_state"]:
            differences["state"] = {
                "expected": entry["expected_state"],
                "observed": state,
            }
        report[job_id] = {
            "result": "read",
            "state": state,
            "differences": differences,
            "accepted_service_values": accepted,
        }
        matched = matched and not differences
    return {
        "contract_version": READBACK_CONTRACT,
        "state": "matched" if matched else "drifted",
        "plan_sha256": plan["plan_sha256"],
        "runtime_configuration_sha256": plan["runtime_configuration_sha256"],
        "scheduler_configuration_sha256": plan["scheduler_configuration_sha256"],
        "resource_manifest_sha256": plan["resource_manifest_sha256"],
        "schedulers": report,
        "record": list(adapter.record),
    }


def _resume_requests(name, run_job_name):
    """Every request a resume sends, in order; the dry run renders these."""
    return [
        _rendered("get_scheduler", name=name),
        _rendered("get_job", name=run_job_name),
        _rendered("resume_price_policy_scheduler", name=name),
        _rendered("get_scheduler", name=name),
    ]


def _resume_receipt(plan, job_id, state, result, sent, adapter):
    return {
        "contract_version": RESUME_RECEIPT_CONTRACT,
        "state": result,
        "plan_sha256": plan["plan_sha256"],
        "scheduler": {"job": job_id, "result": result, "state": state},
        "sent": list(sent),
        "record": list(adapter.record),
    }


def resume_scheduler(
    plan,
    job_id,
    *,
    adapter,
    dry_run=False,
    runtime_path=RUNTIME_FILE,
    manifest_path=RESOURCE_MANIFEST,
) -> dict:
    """Resume the price policy scheduler, and only from its reviewed paused state.

    The daily scheduler, or any other identifier, refuses before the plan is
    even read. The scheduler must read back with every compared field equal to
    the reviewed file's entry and PAUSED, and its target Cloud Run job must
    exist, before the one resume request goes out; the confirming read must show
    ENABLED with the fields unchanged. Nothing is created, paused, deleted or
    rewritten, and no Cloud Run job is run.
    """
    if job_id not in RESUMABLE_JOB_IDS:
        _refuse("scheduler_resume_unapproved")
    plan = _validated_plan(plan)
    _resources, sealed = _bound(plan, runtime_path, manifest_path)
    name = _scheduler_name(job_id)
    run_job_name = f"{PARENT}/jobs/{job_id}"
    expected = _compared(sealed[job_id])
    if dry_run:
        return {
            "contract_version": RESUME_RECEIPT_CONTRACT,
            "state": "rendered",
            "plan_sha256": plan["plan_sha256"],
            "requests": _resume_requests(name, run_job_name),
        }
    sent = []
    try:
        observed = adapter.get("get_scheduler", name=name)
        if observed is None:
            _refuse("price_policy_scheduler_absent")
        state = _observed_state(observed, name)
        if _differences(expected, observed):
            _refuse("price_policy_scheduler_drifted")
        if state == ENABLED:
            return _resume_receipt(
                plan, job_id, state, "already_enabled", sent, adapter
            )
        if state != PAUSED:
            _refuse("price_policy_scheduler_not_paused")
        target = adapter.get("get_job", name=run_job_name)
        if target is None:
            _refuse("price_policy_job_absent")
        if target.get("name") != run_job_name:
            _refuse("price_policy_job_unconfirmed")
        sent.append("resume_price_policy_scheduler")
        status, resumed = adapter.perform("resume_price_policy_scheduler", name=name)
        adapter.require_ok("resume_price_policy_scheduler", status, resumed)
        confirmed = adapter.get("get_scheduler", name=name)
        if confirmed is None:
            _refuse("price_policy_scheduler_absent_after_resume")
        state = _observed_state(confirmed, name)
        if _differences(expected, confirmed):
            _refuse("price_policy_scheduler_drifted")
        if state != ENABLED:
            _refuse("price_policy_scheduler_not_enabled")
    except (OSError, ValueError, NativeError) as error:
        # Once the resume has gone out the schedule may be live, so the refusal
        # carries the record of every request sent and every answer received.
        if sent:
            error.receipt = {
                **_resume_receipt(plan, job_id, None, "refused", sent, adapter),
                "error": str(error),
            }
        raise
    return _resume_receipt(plan, job_id, state, "resumed", sent, adapter)


def _write_once(path, value):
    target = Path(path)
    if target.exists():
        _refuse("output_exists")
    target.write_bytes(canonical_bytes(value) + b"\n")


def _output_available(path):
    """The output path is free and its directory writable, before any request.

    Checking this after apply has returned is too late: an apply that created
    two schedulers and then refused output_exists prints a refusal an operator
    reasonably reads as nothing happened, while two schedules exist. So the
    check runs at the top of the command, before the first request goes out.
    """
    target = Path(path)
    if target.exists():
        _refuse("output_exists")
    if not target.parent.is_dir():
        _refuse("output_directory_absent")
    if not os.access(target.parent, os.W_OK | os.X_OK):
        _refuse("output_directory_unwritable")


def _sibling(path, value):
    """A path beside the output, named by the document it is about to hold."""
    target = Path(path)
    suffix = target.suffix or ".json"
    stem = target.name[: len(target.name) - len(target.suffix)]
    return target.with_name(f"{stem}.unwritten-{canonical_sha256(value)[:16]}{suffix}")


def _written(path, value) -> str:
    """Write the document where it was asked for, or beside it, and say where.

    A document that cannot reach its path is not discarded. A receipt lost
    after a create leaves a live schedule on a billing project with nothing
    naming it, which is worse than an artifact under a name the operator did
    not choose, so the fallback path is written and returned to be printed.
    """
    try:
        _write_once(path, value)
        return str(path)
    except (OSError, ValueError):
        target = _sibling(path, value)
        target.write_bytes(canonical_bytes(value) + b"\n")
        return str(target)


def _attached(refusal, path, receipt):
    """Put a refusal receipt where an operator will find it, or in the refusal."""
    if receipt is None:
        return refusal
    try:
        refusal["receipt"] = _written(path, receipt)
    except OSError as failure:
        refusal["receipt_unwritten"] = str(failure)
        refusal["receipt_document"] = receipt
    return refusal


def _parser():
    parser = argparse.ArgumentParser(prog="runtime_schedulers")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--output", required=True)
    for name in ("apply", "readback"):
        command = commands.add_parser(name)
        command.add_argument("--plan", required=True)
        command.add_argument("--output", required=True)
        command.add_argument("--adapter-state", choices=_ADAPTER_STATES, default="adc")
        if name == "apply":
            command.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None, *, adapter_factory=None, out=None) -> int:
    arguments = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    stream = out or sys.stdout
    try:
        # Nothing is sent until the artifact has somewhere to land.
        _output_available(arguments.output)
        if arguments.command == "plan":
            result = render_plan()
        else:
            plan, _digest = _load_json(arguments.plan, "plan_invalid")
            build = adapter_factory or (
                lambda state: NativeAdapter(state, transport=None)
            )
            adapter = build(arguments.adapter_state)
            if arguments.command == "apply":
                result = apply_plan(plan, adapter=adapter, dry_run=arguments.dry_run)
            else:
                result = read_back(plan, adapter=adapter)
    except (OSError, ValueError, NativeError) as error:
        # OSError is the transport that never answered, and the refusal it
        # raises can follow a create that landed, so it carries a receipt too.
        refusal = _attached(
            {"status": "refused", "error": str(error)},
            arguments.output,
            getattr(error, "receipt", None),
        )
        stream.write(json.dumps(refusal) + "\n")
        return 1
    try:
        document = {"status": result.get("state", "planned")}
        document["output"] = _written(arguments.output, result)
    except OSError as failure:
        refusal = _attached(
            {"status": "refused", "error": str(failure)}, arguments.output, result
        )
        stream.write(json.dumps(refusal) + "\n")
        return 1
    stream.write(json.dumps(document) + "\n")
    return 0


def _resume_parser():
    parser = argparse.ArgumentParser(prog="runtime_scheduler_resume")
    parser.add_argument("--job", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--adapter-state", choices=_ADAPTER_STATES, default="adc")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def resume_main(argv=None, *, adapter_factory=None, out=None) -> int:
    """The resume command: kept off the creation command line on purpose."""
    arguments = _resume_parser().parse_args(
        sys.argv[1:] if argv is None else list(argv)
    )
    stream = out or sys.stdout
    try:
        # Nothing is sent until the artifact has somewhere to land.
        _output_available(arguments.output)
        if arguments.job not in RESUMABLE_JOB_IDS:
            _refuse("scheduler_resume_unapproved")
        plan, _digest = _load_json(arguments.plan, "plan_invalid")
        build = adapter_factory or (lambda state: NativeAdapter(state, transport=None))
        adapter = build(arguments.adapter_state)
        result = resume_scheduler(
            plan, arguments.job, adapter=adapter, dry_run=arguments.dry_run
        )
    except (OSError, ValueError, NativeError) as error:
        refusal = _attached(
            {"status": "refused", "error": str(error)},
            arguments.output,
            getattr(error, "receipt", None),
        )
        stream.write(json.dumps(refusal) + "\n")
        return 1
    try:
        document = {"status": result["state"]}
        document["output"] = _written(arguments.output, result)
    except OSError as failure:
        refusal = _attached(
            {"status": "refused", "error": str(failure)}, arguments.output, result
        )
        stream.write(json.dumps(refusal) + "\n")
        return 1
    stream.write(json.dumps(document) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
