"""Native creation step for the R05 managed runtime jobs.

plan renders one create operation per Cloud Run job declared in the runtime
file, from a pinned engine image digest bound by a successful build record,
plus the job bindings the provisioning delta defers to job creation. apply runs
a plan through an injectable runner and writes a receipt. readback describes
each job and its IAM policy and diffs both against the plan. None of the three
touches a scheduler or runs a job.

replace is the one path for a job that already exists. It describes the live
job, refuses unless the live job differs from the plan only in image, args,
env and the two configuration annotations, and then sends the live job back
with only those fields changed, so identity, command, limits, secrets,
resources and volumes are carried as they are and IAM is never touched. The
replacement carries the live resourceVersion, so Cloud Run refuses it if the
job changed after it was read; the job is also described again just before the
replace and refused if the version moved. It never creates or deletes a job,
and it reads the result back against the plan.

The create operation is `gcloud run jobs replace` over a specification file,
because the create verb of the SDK carries labels only and the runtime binds
its configuration digest through an execution template annotation. A describe
precheck precedes it so an existing job is reported, never replaced. Every
operation runs with the plan file's directory as its working directory, so the
specification is named relative to the plan and no receipt carries a directory.

The runtime file is either reviewed job file: infra/runtime/daily-staging.json for
the daily and price policy jobs, or infra/runtime/ingest-staging.json for the
ingest job alone, so planning the ingest file creates that one job and nothing
else. For the ingest job the plan also binds, from the build it deploys, the
source commit and the pinned image the collection receipt stamps.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from ops.deploy.ingest_runtime import ingest_build_environment, load_job_configuration
from ops.deploy.resource_guard import assert_allowed, load_resource_manifest
from ops.runners.managed_runtime import CONFIGURATION_ANNOTATION, canonical_sha256

ROOT = Path(__file__).resolve().parents[2]
RESOURCE_MANIFEST = ROOT / "ops" / "deploy" / "resource_manifest.json"
ENGINE_MANIFEST = (
    ROOT / "engine" / "configs" / "open_intelligence" / "resource_manifest_v1.json"
)
IAM_DELTA = ROOT / "ops" / "deploy" / "iam_delta_v1.json"
PLAN_CONTRACT = "42_runtime_jobs_plan_v1"
REPLACE_RECEIPT_CONTRACT = "42_runtime_jobs_replace_receipt_v1"
# What replace may change on a live job; every other compared field must
# already equal the plan.
REPLACEABLE_FIELDS = ("image", "args", "env", "annotations")
RECEIPT_CONTRACT = "42_runtime_jobs_receipt_v1"
READBACK_CONTRACT = "42_runtime_jobs_readback_v1"
DELTA_CONTRACT = "42_iam_delta_v1"
MANIFEST_ANNOTATION = "42.ogilvy/resource-manifest-sha256"
GCLOUD_PLACEHOLDER = "gcloud"
TAIL_CHARACTERS = 500
EXPECTED_FIELDS = (
    "image",
    "service_account",
    "command",
    "args",
    "env",
    "timeout_seconds",
    "max_retries",
    "task_count",
    "parallelism",
    "annotations",
)
_MAX_INPUT_BYTES = 4 * 1024 * 1024
_RUNNER_TIMEOUT_SECONDS = 900
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_BINDING_KEYS = {"condition", "member", "purpose", "resource", "role"}
_ALREADY_EXISTS = re.compile(r"already\s*exists|ALREADY_EXISTS", re.IGNORECASE)
_NOT_FOUND = re.compile(
    r"cannot find|NOT_FOUND|could not be found|does not exist|not found",
    re.IGNORECASE,
)
_APPROVAL_KEYS = {"applied", "approved_at", "approved_by", "state"}
_JOB_RESOURCE = re.compile(
    r"//run\.googleapis\.com/projects/[^/]+/locations/[^/]+/jobs/[^/]+"
)
_REFUSED = re.compile(
    r"PERMISSION_DENIED|permission denied|does not have permission|forbidden|\b403\b",
    re.IGNORECASE,
)


# Case sensitive: ABORTED as a status token, not a transport error's "aborted".
_CONFLICT = re.compile(
    r"\bABORTED\b|\(409\)|HTTP(?:Error)? 409\b|\b409 Conflict\b|"
    r"Conflict for resource|[Tt]he object has been modified"
)
# Job metadata annotations the server sets; the replacement leaves them out.
_OUTPUT_ONLY_ANNOTATIONS = (
    "run.googleapis.com/creator",
    "run.googleapis.com/lastModifier",
    "run.googleapis.com/operation-id",
)


def _refuse(code):
    raise ValueError(code)


def _now():
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")


def _tail(text):
    return text[-TAIL_CHARACTERS:]


def _read_bytes(path, code):
    try:
        raw = Path(path).read_bytes()
    except (OSError, TypeError, ValueError):
        _refuse(code)
    if len(raw) > _MAX_INPUT_BYTES:
        _refuse(code)
    return raw


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


def _bound_image(build, entry, image_digest):
    if build.get("status") != "SUCCESS":
        _refuse("image_unbound")
    results = build.get("results")
    images = results.get("images") if type(results) is dict else None
    if type(images) is not list or len(images) != 1 or type(images[0]) is not dict:
        _refuse("image_unbound")
    name = images[0].get("name")
    repository = entry["image_repository"]
    if not isinstance(name, str) or not (
        name == repository or name.startswith((repository + ":", repository + "@"))
    ):
        _refuse("image_unbound")
    if images[0].get("digest") != image_digest:
        _refuse("image_unbound")
    return f"{repository}@{image_digest}"


def _approval(delta):
    approval = delta.get("approval")
    if type(approval) is not dict or set(approval) != _APPROVAL_KEYS:
        _refuse("iam_delta_invalid")
    if approval["state"] != "approved":
        _refuse("iam_delta_unapproved")
    if approval["applied"] is not False or any(
        not isinstance(approval[key], str) or not approval[key]
        for key in ("approved_at", "approved_by")
    ):
        _refuse("iam_delta_invalid")
    return dict(approval)


def _job_bindings(delta, resources):
    if delta.get("contract_version") != DELTA_CONTRACT:
        _refuse("iam_delta_invalid")
    rows = delta.get("bindings")
    if type(rows) is not list:
        _refuse("iam_delta_invalid")
    selected = {resource: [] for resource in resources}
    deferred = 0
    for row in rows:
        if type(row) is not dict or set(row) != _BINDING_KEYS:
            _refuse("iam_delta_invalid")
        if row["resource"] not in selected:
            if isinstance(row["resource"], str) and _JOB_RESOURCE.fullmatch(
                row["resource"]
            ):
                deferred += 1
            continue
        if row["condition"] is not None:
            _refuse("binding_condition_unsupported")
        member, role = row["member"], row["role"]
        if (
            not isinstance(member, str)
            or ":" not in member
            or not isinstance(role, str)
            or not role.startswith("roles/")
        ):
            _refuse("iam_delta_invalid")
        selected[row["resource"]].append({"member": member, "role": role})
    for bindings in selected.values():
        bindings.sort(key=lambda binding: (binding["role"], binding["member"]))
    return selected, deferred


def _specification(job_id, expected):
    return {
        "apiVersion": "run.googleapis.com/v1",
        "kind": "Job",
        "metadata": {"name": job_id},
        "spec": {
            "template": {
                "metadata": {"annotations": dict(expected["annotations"])},
                "spec": {
                    "parallelism": expected["parallelism"],
                    "taskCount": expected["task_count"],
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "args": list(expected["args"]),
                                    "command": list(expected["command"]),
                                    "env": list(expected["env"]),
                                    "image": expected["image"],
                                }
                            ],
                            "maxRetries": expected["max_retries"],
                            "serviceAccountName": expected["service_account"],
                            "timeoutSeconds": expected["timeout_seconds"],
                        }
                    },
                },
            }
        },
    }


def _specification_bytes(specification):
    return (json.dumps(specification, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _scope(project, region):
    return [f"--project={project}", f"--region={region}"]


def _describe_argv(job_id, project, region):
    return (
        [GCLOUD_PLACEHOLDER, "run", "jobs", "describe", job_id]
        + _scope(project, region)
        + ["--format=json"]
    )


def _policy_argv(job_id, project, region):
    return (
        [GCLOUD_PLACEHOLDER, "run", "jobs", "get-iam-policy", job_id]
        + _scope(project, region)
        + ["--format=json"]
    )


def _create_argv(file_name, project, region):
    return (
        [GCLOUD_PLACEHOLDER, "run", "jobs", "replace", file_name]
        + _scope(project, region)
        + ["--format=json", "--quiet"]
    )


def _binding_argv(job_id, member, role, project, region):
    return (
        [GCLOUD_PLACEHOLDER, "run", "jobs", "add-iam-policy-binding", job_id]
        + _scope(project, region)
        + [f"--member={member}", f"--role={role}", "--format=json", "--quiet"]
    )


def _render_operations(project, region, jobs):
    operations = []
    for job in jobs:
        job_id, resource = job["job_id"], job["resource"]
        operations.append(
            {
                "kind": "precheck",
                "job_id": job_id,
                "resource": resource,
                "argv": _describe_argv(job_id, project, region),
            }
        )
        operations.append(
            {
                "kind": "create_job",
                "job_id": job_id,
                "resource": resource,
                "argv": _create_argv(job["specification_file"], project, region),
            }
        )
        for binding in job["bindings"]:
            operations.append(
                {
                    "kind": "add_binding",
                    "job_id": job_id,
                    "resource": resource,
                    "member": binding["member"],
                    "role": binding["role"],
                    "argv": _binding_argv(
                        job_id, binding["member"], binding["role"], project, region
                    ),
                }
            )
    for index, operation in enumerate(operations):
        operation["index"] = index
    return operations


def render_plan(
    *,
    runtime_path,
    image_digest,
    build_record_path,
    output_path,
    resource_manifest_path=RESOURCE_MANIFEST,
    engine_manifest_path=ENGINE_MANIFEST,
    iam_delta_path=IAM_DELTA,
) -> dict:
    if not isinstance(image_digest, str) or not _IMAGE_DIGEST.fullmatch(image_digest):
        _refuse("image_digest_invalid")
    config = load_job_configuration(runtime_path)
    runtime_raw = _read_bytes(runtime_path, "runtime_configuration_invalid")
    manifest_sha256 = config["resource_manifest_sha256"]
    engine_raw = _read_bytes(engine_manifest_path, "engine_manifest_unreadable")
    if hashlib.sha256(engine_raw).hexdigest() != manifest_sha256:
        _refuse("runtime_manifest_mismatch")
    manifest = load_resource_manifest(
        resource_manifest_path, expected_sha256=manifest_sha256
    )
    build, build_sha256 = _load_json(build_record_path, "build_record_invalid")
    delta, delta_sha256 = _load_json(iam_delta_path, "iam_delta_invalid")
    if delta.get("resource_manifest_sha256") != manifest_sha256:
        _refuse("iam_delta_manifest_mismatch")
    approval = _approval(delta)
    configuration_sha256 = canonical_sha256(config)
    project, region = config["project"], config["location"]
    job_ids = sorted(config["jobs"])
    bindings, deferred = _job_bindings(
        delta, [config["jobs"][job_id]["job_resource"] for job_id in job_ids]
    )
    stem = Path(output_path).stem
    jobs = []
    for job_id in job_ids:
        entry = config["jobs"][job_id]
        resource = entry["job_resource"]
        for action in ("deploy", "read"):
            assert_allowed(resource, action, manifest)
        image = _bound_image(build, entry, image_digest)
        expected = {
            "image": image,
            "service_account": entry["service_account"],
            "command": list(entry["command"]),
            "args": list(entry["args"]),
            "env": list(entry["env"])
            + ingest_build_environment(config, job_id, image=image, build=build),
            "timeout_seconds": entry["timeout_seconds"],
            "max_retries": entry["max_retries"],
            "task_count": entry["task_count"],
            "parallelism": entry["parallelism"],
            "annotations": {
                CONFIGURATION_ANNOTATION: configuration_sha256,
                MANIFEST_ANNOTATION: manifest_sha256,
            },
        }
        specification = _specification(job_id, expected)
        jobs.append(
            {
                "job_id": job_id,
                "resource": resource,
                "mode": entry["mode"],
                "guard": {"resource": resource, "actions": ["deploy", "read"]},
                "expected": expected,
                "specification": specification,
                "specification_file": f"{stem}-{job_id}.job.yaml",
                "specification_sha256": hashlib.sha256(
                    _specification_bytes(specification)
                ).hexdigest(),
                "bindings": bindings[resource],
            }
        )
    plan = {
        "contract_version": PLAN_CONTRACT,
        "name": stem,
        "project": project,
        "region": region,
        "runtime_configuration_sha256": configuration_sha256,
        "resource_manifest_sha256": manifest_sha256,
        "inputs": {
            "runtime_file": {
                "name": Path(runtime_path).name,
                "sha256": hashlib.sha256(runtime_raw).hexdigest(),
            },
            "build_record": {
                "name": Path(build_record_path).name,
                "sha256": build_sha256,
                "build_id": build.get("id"),
                "image_digest": image_digest,
            },
            "iam_delta": {
                "name": Path(iam_delta_path).name,
                "sha256": delta_sha256,
                "approval": approval,
                "job_bindings": {
                    "declared": sum(len(rows) for rows in bindings.values()),
                    "deferred_other_jobs": deferred,
                },
            },
        },
        "jobs": jobs,
        "operations": _render_operations(project, region, jobs),
    }
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


def write_plan(plan, output_path):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for job in plan["jobs"]:
        path = output.parent / job["specification_file"]
        path.write_bytes(_specification_bytes(job["specification"]))
        written.append(path)
    output.write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    written.append(output)
    return written


def verify_plan(plan) -> str:
    if type(plan) is not dict or plan.get("contract_version") != PLAN_CONTRACT:
        _refuse("plan_invalid")
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    try:
        computed = canonical_sha256(body)
    except (TypeError, ValueError):
        _refuse("plan_invalid")
    digest = plan.get("plan_sha256")
    if not isinstance(digest, str) or digest != computed:
        _refuse("plan_mismatch")
    return digest


def _validated_plan(plan):
    digest = verify_plan(plan)
    jobs = plan.get("jobs")
    if type(jobs) is not list or not jobs or type(plan.get("operations")) is not list:
        _refuse("plan_invalid")
    project, region, stem = plan.get("project"), plan.get("region"), plan.get("name")
    if any(not isinstance(value, str) for value in (project, region, stem)):
        _refuse("plan_invalid")
    prefix = f"//run.googleapis.com/projects/{project}/locations/{region}/jobs/"
    for job in jobs:
        if type(job) is not dict or type(job.get("expected")) is not dict:
            _refuse("plan_invalid")
        job_id = job.get("job_id")
        if (
            not isinstance(job_id, str)
            or job.get("resource") != prefix + job_id
            or job.get("specification_file") != f"{stem}-{job_id}.job.yaml"
            or set(job["expected"]) != set(EXPECTED_FIELDS)
            or type(job.get("bindings")) is not list
        ):
            _refuse("plan_invalid")
        for binding in job["bindings"]:
            if (
                type(binding) is not dict
                or set(binding) != {"member", "role"}
                or not all(isinstance(value, str) for value in binding.values())
            ):
                _refuse("plan_invalid")
        try:
            rendered = _specification(job_id, job["expected"])
            _specification_bytes(rendered)
        except (TypeError, ValueError):
            _refuse("plan_invalid")
        if job.get("specification") != rendered:
            _refuse("plan_invalid")
    if plan["operations"] != _render_operations(project, region, jobs):
        _refuse("plan_invalid")
    return digest


def _run(runner, argv, cwd):
    result = runner(argv, cwd)
    if (
        type(result) is not tuple
        or len(result) != 3
        or type(result[0]) is not int
        or not isinstance(result[1], str)
        or not isinstance(result[2], str)
    ):
        _refuse("runner_result_invalid")
    return result


def subprocess_runner(argv, cwd=None):
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            shell=False,
            check=False,
            cwd=cwd,
            timeout=_RUNNER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"runner timeout after {_RUNNER_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return -1, "", f"runner error: {exc}"
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def _materialise(operation, *, plan, gcloud, plan_dir):
    argv = list(operation["argv"])
    argv[0] = gcloud
    if operation["kind"] == "create_job":
        job = next(job for job in plan["jobs"] if job["job_id"] == operation["job_id"])
        path = Path(plan_dir) / job["specification_file"]
        raw = _read_bytes(path, "specification_mismatch")
        if raw != _specification_bytes(job["specification"]):
            _refuse("specification_mismatch")
    return argv


def _state(kind, exit_code, output):
    if exit_code == 0:
        return "already_exists" if kind == "precheck" else "applied"
    if kind == "precheck":
        if _NOT_FOUND.search(output):
            return "absent"
    elif _ALREADY_EXISTS.search(output):
        return "already_exists"
    if _REFUSED.search(output):
        return "refused"
    return "failed"


def apply_plan(
    plan,
    *,
    runner,
    gcloud,
    plan_dir,
    plan_name,
    manifest,
    dry_run=False,
    out=None,
) -> dict:
    plan_sha256 = _validated_plan(plan)
    for job in plan["jobs"]:
        for action in ("deploy", "read"):
            assert_allowed(job["resource"], action, manifest)
    stream = sys.stdout if out is None else out
    started = _now()
    materialised = [
        _materialise(operation, plan=plan, gcloud=gcloud, plan_dir=plan_dir)
        for operation in plan["operations"]
    ]
    records, existing, stopped_at = [], set(), None
    for operation, argv in zip(plan["operations"], materialised):
        record = {
            "index": operation["index"],
            "kind": operation["kind"],
            "job_id": operation["job_id"],
            "resource": operation["resource"],
            "argv": argv,
        }
        records.append(record)
        if dry_run:
            stream.write(json.dumps(argv) + "\n")
            record["state"] = "dry_run"
            continue
        if stopped_at is not None:
            record["state"] = "not_run"
            continue
        if operation["kind"] == "create_job" and operation["job_id"] in existing:
            record["state"] = "skipped_existing"
            continue
        record["started_at"] = _now()
        exit_code, stdout, stderr = _run(runner, argv, plan_dir)
        record["finished_at"] = _now()
        record["exit_code"] = exit_code
        record["stdout_tail"] = _tail(stdout)
        record["stderr_tail"] = _tail(stderr)
        state = _state(operation["kind"], exit_code, stdout + "\n" + stderr)
        record["state"] = state
        if operation["kind"] == "precheck" and state == "already_exists":
            existing.add(operation["job_id"])
        if state in ("refused", "failed"):
            stopped_at = operation["index"]
    counts = {}
    for record in records:
        counts[record["state"]] = counts.get(record["state"], 0) + 1
    return {
        "contract_version": RECEIPT_CONTRACT,
        "plan_sha256": plan_sha256,
        "plan_file": Path(plan_name).name,
        "project": plan["project"],
        "region": plan["region"],
        "gcloud": gcloud,
        "dry_run": dry_run,
        "started_at": started,
        "finished_at": _now(),
        "status": "dry_run"
        if dry_run
        else ("failed" if stopped_at is not None else "applied"),
        "stopped_at": stopped_at,
        "counts": counts,
        "operations": records,
    }


def _read(runner, argv):
    exit_code, stdout, stderr = _run(runner, argv, None)
    record = {
        "argv": argv,
        "exit_code": exit_code,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }
    value = None
    if exit_code == 0:
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError:
            value = None
        record["state"] = "read" if type(value) is dict else "invalid"
    elif _REFUSED.search(stdout + "\n" + stderr):
        record["state"] = "refused"
    elif _NOT_FOUND.search(stdout + "\n" + stderr):
        record["state"] = "absent"
    else:
        record["state"] = "error"
    return record, value


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return value


def _observed_job(value):
    if "spec" in value:
        spec = value.get("spec") if type(value.get("spec")) is dict else {}
        template = spec.get("template") if type(spec.get("template")) is dict else {}
        metadata = template.get("metadata") or {}
        execution = template.get("spec") or {}
        task_template = execution.get("template") or {}
        task = task_template.get("spec") or {}
        service_account = task.get("serviceAccountName")
        timeout = task.get("timeoutSeconds")
        max_retries = task.get("maxRetries", 0)
        annotations = metadata.get("annotations") or {}
    else:
        execution = value.get("template") or {}
        task = execution.get("template") or {}
        service_account = task.get("serviceAccount")
        timeout = task.get("timeout")
        if isinstance(timeout, str) and re.fullmatch(r"[0-9]+s", timeout):
            timeout = timeout[:-1]
        max_retries = task.get("maxRetries", 0)
        annotations = execution.get("annotations") or {}
    containers = task.get("containers")
    count = len(containers) if type(containers) is list else 0
    container = containers[0] if count == 1 and type(containers[0]) is dict else {}
    return {
        "image": container.get("image"),
        "service_account": service_account,
        "command": container.get("command"),
        "args": container.get("args"),
        "env": container.get("env", []),
        "timeout_seconds": _integer(timeout),
        "max_retries": _integer(max_retries),
        "task_count": _integer(execution.get("taskCount")),
        "parallelism": _integer(execution.get("parallelism")),
        "annotations": annotations if type(annotations) is dict else {},
        "container_count": count,
    }


def _diff_fields(expected, observed):
    fields = {}
    for name in EXPECTED_FIELDS:
        wanted = expected[name]
        seen = observed[name]
        if name == "annotations":
            seen = {key: seen.get(key) for key in wanted}
        fields[name] = {"expected": wanted, "observed": seen, "match": wanted == seen}
    fields["container_count"] = {
        "expected": 1,
        "observed": observed["container_count"],
        "match": observed["container_count"] == 1,
    }
    return fields


def _diff_bindings(expected, policy):
    pairs, conditional = [], []
    for binding in policy.get("bindings") or []:
        if type(binding) is not dict:
            continue
        role = binding.get("role")
        for member in binding.get("members") or []:
            pair = {"member": member, "role": role}
            if binding.get("condition"):
                conditional.append({**pair, "condition": binding["condition"]})
            else:
                pairs.append(pair)
    missing = [binding for binding in expected if binding not in pairs]
    extra = [pair for pair in pairs if pair not in expected] + conditional
    return {
        "expected": expected,
        "missing": missing,
        "extra": extra,
        "match": not missing,
    }


def read_back(plan, *, runner, gcloud) -> dict:
    plan_sha256 = _validated_plan(plan)
    project, region = plan["project"], plan["region"]
    reports = []
    summary = {
        "jobs": 0,
        "match": 0,
        "mismatch": 0,
        "refused_reads": 0,
        "absent": 0,
        "extra_bindings": 0,
    }
    for job in plan["jobs"]:
        job_id = job["job_id"]
        describe_argv = _describe_argv(job_id, project, region)
        policy_argv = _policy_argv(job_id, project, region)
        describe_argv[0] = policy_argv[0] = gcloud
        describe, described = _read(runner, describe_argv)
        policy, policy_value = _read(runner, policy_argv)
        report = {
            "job_id": job_id,
            "resource": job["resource"],
            "describe": describe,
            "policy": policy,
            "fields": None,
            "bindings": None,
            "reasons": [],
            "match": False,
        }
        if describe["state"] == "read":
            report["fields"] = _diff_fields(job["expected"], _observed_job(described))
            if not all(field["match"] for field in report["fields"].values()):
                report["reasons"].append("fields_mismatch")
        else:
            report["reasons"].append(f"describe_{describe['state']}")
        if policy["state"] == "read":
            report["bindings"] = _diff_bindings(job["bindings"], policy_value)
            if not report["bindings"]["match"]:
                report["reasons"].append("bindings_missing")
            summary["extra_bindings"] += len(report["bindings"]["extra"])
        else:
            report["reasons"].append(f"policy_{policy['state']}")
        report["match"] = not report["reasons"]
        summary["jobs"] += 1
        summary["match" if report["match"] else "mismatch"] += 1
        for record in (describe, policy):
            if record["state"] == "refused":
                summary["refused_reads"] += 1
        if describe["state"] == "absent":
            summary["absent"] += 1
        reports.append(report)
    return {
        "contract_version": READBACK_CONTRACT,
        "plan_sha256": plan_sha256,
        "project": project,
        "region": region,
        "gcloud": gcloud,
        "read_at": _now(),
        "status": "match" if summary["mismatch"] == 0 else "mismatch",
        "summary": summary,
        "jobs": reports,
    }


def _replacement(live, job_id, expected):
    """The live job with only image, args, env and the two annotations changed."""
    if (
        type(live) is not dict
        or live.get("apiVersion") != "run.googleapis.com/v1"
        or live.get("kind") != "Job"
        or type(live.get("metadata")) is not dict
        or type(live.get("spec")) is not dict
    ):
        _refuse("job_shape_unsupported")
    metadata = live["metadata"]
    if metadata.get("name") != job_id:
        _refuse("job_shape_unsupported")
    version = metadata.get("resourceVersion")
    if not isinstance(version, str) or not version:
        _refuse("job_version_unavailable")
    spec = json.loads(json.dumps(live["spec"]))
    for token in _EXECUTION_TOKENS:
        spec.pop(token, None)
    try:
        template = spec["template"]
        containers = template["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError):
        _refuse("job_shape_unsupported")
    if type(containers) is not list or len(containers) != 1:
        _refuse("job_shape_unsupported")
    container = containers[0]
    env = container.get("env", [])
    if type(env) is not list or any(
        type(entry) is not dict or set(entry) != {"name", "value"} for entry in env
    ):
        # A secret or other reference would be dropped by the plan's env.
        _refuse("job_env_not_plain")
    container["image"] = expected["image"]
    container["args"] = list(expected["args"])
    container["env"] = list(expected["env"])
    template_metadata = template.setdefault("metadata", {})
    if type(template_metadata) is not dict:
        _refuse("job_shape_unsupported")
    annotations = template_metadata.setdefault("annotations", {})
    if type(annotations) is not dict:
        _refuse("job_shape_unsupported")
    annotations.update(expected["annotations"])
    kept = {"name": job_id, "resourceVersion": version}
    if isinstance(metadata.get("namespace"), str):
        kept["namespace"] = metadata["namespace"]
    if type(metadata.get("labels")) is dict:
        kept["labels"] = dict(metadata["labels"])
    if type(metadata.get("annotations")) is dict:
        kept["annotations"] = {
            key: value
            for key, value in metadata["annotations"].items()
            if key not in _OUTPUT_ONLY_ANNOTATIONS
        }
    return {
        "apiVersion": "run.googleapis.com/v1",
        "kind": "Job",
        "metadata": kept,
        "spec": spec,
    }


_EXECUTION_TOKENS = ("runExecutionToken", "startExecutionToken")
_CLIENT_ANNOTATIONS = (
    "run.googleapis.com/client-name",
    "run.googleapis.com/client-version",
)


def _carried(value, expected):
    """Everything replace must leave as it was: the job less the fields it sets.

    Labels under run.googleapis.com/ and the job annotations the server or
    gcloud stamps change on every update, so they are left out of the compare.
    """
    if type(value) is not dict:
        return None
    spec = json.loads(json.dumps(value.get("spec")))
    try:
        template = spec["template"]
        container = template["spec"]["template"]["spec"]["containers"][0]
        for name in ("image", "args", "env"):
            container.pop(name, None)
        template_metadata = template.get("metadata", {})
        annotations = template_metadata.get("annotations", {})
        # gcloud stamps its own name and version on the template it sends.
        for key in (*expected["annotations"], *_CLIENT_ANNOTATIONS):
            annotations.pop(key, None)
        if not annotations:
            template_metadata.pop("annotations", None)
        if not template_metadata:
            template.pop("metadata", None)
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    for token in _EXECUTION_TOKENS:
        spec.pop(token, None)
    metadata = value.get("metadata")
    metadata = metadata if type(metadata) is dict else {}
    labels = metadata.get("labels")
    job_annotations = metadata.get("annotations")
    return {
        "spec": spec,
        "labels": {
            key: item
            for key, item in labels.items()
            if not key.startswith("run.googleapis.com/")
        }
        if type(labels) is dict
        else {},
        "annotations": {
            key: item
            for key, item in job_annotations.items()
            if key not in (*_OUTPUT_ONLY_ANNOTATIONS, *_CLIENT_ANNOTATIONS)
        }
        if type(job_annotations) is dict
        else {},
    }


def _version(value):
    metadata = value.get("metadata") if type(value) is dict else None
    return metadata.get("resourceVersion") if type(metadata) is dict else None


def replace_job(
    plan,
    job_id,
    *,
    runner,
    gcloud,
    specification_path,
    manifest,
    dry_run=False,
    out=None,
) -> dict:
    """Replace one existing job so it matches the plan, changing nothing else.

    Refusals before the replace request raise, with the reads made recorded on
    the error as ``receipt``. After it, the receipt carries the outcome.
    """
    plan_sha256 = _validated_plan(plan)
    job = next((item for item in plan["jobs"] if item["job_id"] == job_id), None)
    if job is None:
        _refuse("job_not_in_plan")
    for action in ("deploy", "read"):
        assert_allowed(job["resource"], action, manifest)
    stream = sys.stdout if out is None else out
    project, region, expected = plan["project"], plan["region"], job["expected"]
    describe_argv = _describe_argv(job_id, project, region)
    describe_argv[0] = gcloud
    records = []
    receipt = {
        "contract_version": REPLACE_RECEIPT_CONTRACT,
        "plan_sha256": plan_sha256,
        "job_id": job_id,
        "resource": job["resource"],
        "gcloud": gcloud,
        "dry_run": dry_run,
        "started_at": _now(),
        "operations": records,
    }

    def read(step):
        record, value = _read(runner, list(describe_argv))
        records.append({"step": step, **record})
        return record, value

    def refuse(code, **detail):
        error = ValueError(code)
        error.receipt = {
            **receipt,
            **detail,
            "status": "refused",
            "error": code,
            "finished_at": _now(),
        }
        raise error

    record, live = read("describe")
    if record["state"] != "read":
        refuse(
            "job_absent"
            if record["state"] == "absent"
            else f"describe_{record['state']}"
        )
    before = _diff_fields(expected, _observed_job(live))
    receipt["before"] = {"resource_version": _version(live), "fields": before}
    fixed = sorted(
        name
        for name, field in before.items()
        if not field["match"] and name not in REPLACEABLE_FIELDS
    )
    if fixed:
        refuse("replace_field_not_allowed", fields_not_allowed=fixed)
    try:
        specification = _replacement(live, job_id, expected)
    except ValueError as error:
        refuse(str(error))
    changed = [name for name, field in before.items() if not field["match"]]
    receipt["changed_fields"] = changed
    raw = _specification_bytes(specification)
    receipt["specification_sha256"] = hashlib.sha256(raw).hexdigest()
    replace_argv = _create_argv(Path(specification_path).name, project, region)
    replace_argv[0] = gcloud
    receipt["replace_argv"] = replace_argv
    if not changed:
        return {**receipt, "status": "unchanged", "finished_at": _now()}
    if dry_run:
        stream.write(
            json.dumps({"changed": {name: before[name] for name in changed}}) + "\n"
        )
        stream.write(raw.decode("utf-8"))
        stream.write(json.dumps(replace_argv) + "\n")
        return {
            **receipt,
            "status": "dry_run",
            "specification": specification,
            "finished_at": _now(),
        }
    target = Path(specification_path)
    try:
        with target.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        refuse("specification_exists")
    record, again = read("describe_before_replace")
    if record["state"] != "read" or _version(again) != _version(live):
        refuse("job_changed_since_read")
    exit_code, stdout, stderr = _run(runner, replace_argv, target.parent)
    output = stdout + "\n" + stderr
    records.append(
        {
            "step": "replace",
            "argv": replace_argv,
            "exit_code": exit_code,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
        }
    )
    if exit_code != 0:
        if _REFUSED.search(output):
            error = "replace_refused"
        elif _CONFLICT.search(output):
            error = "job_changed_since_read"
        else:
            # A timeout or an unclassified failure may still have landed, so
            # what is live afterwards is read and recorded, not assumed.
            error = "replace_failed"
            record, after = read("readback")
            if record["state"] == "read":
                receipt["after"] = {
                    "resource_version": _version(after),
                    "fields": _diff_fields(expected, _observed_job(after)),
                }
        return {**receipt, "status": "failed", "error": error, "finished_at": _now()}
    record, after = read("readback")
    if record["state"] != "read":
        return {
            **receipt,
            "status": "replace_unverified",
            "error": f"readback_{record['state']}",
            "finished_at": _now(),
        }
    fields = _diff_fields(expected, _observed_job(after))
    receipt["after"] = {"resource_version": _version(after), "fields": fields}
    matched = all(field["match"] for field in fields.values())
    carried = _carried(after, expected) == _carried(live, expected)
    receipt["after"]["carried_unchanged"] = carried
    error = (
        None
        if matched and carried
        else ("readback_mismatch" if not matched else "carried_fields_changed")
    )
    return {
        **receipt,
        "status": "replaced" if error is None else "replace_unverified",
        **({} if error is None else {"error": error}),
        "finished_at": _now(),
    }


def _write_json(path, value):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _parser():
    parser = argparse.ArgumentParser(prog="runtime_jobs")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--runtime", required=True)
    plan.add_argument("--image-digest", required=True)
    plan.add_argument("--build-record", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--resource-manifest", default=str(RESOURCE_MANIFEST))
    plan.add_argument("--engine-manifest", default=str(ENGINE_MANIFEST))
    plan.add_argument("--iam-delta", default=str(IAM_DELTA))

    apply = commands.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--output", required=True)
    apply.add_argument("--gcloud", required=True)
    apply.add_argument("--resource-manifest", default=str(RESOURCE_MANIFEST))
    apply.add_argument("--dry-run", action="store_true")

    readback = commands.add_parser("readback")
    readback.add_argument("--plan", required=True)
    readback.add_argument("--output", required=True)
    readback.add_argument("--gcloud", required=True)

    replace = commands.add_parser("replace")
    replace.add_argument("--plan", required=True)
    replace.add_argument("--job", required=True)
    replace.add_argument("--output", required=True)
    replace.add_argument("--gcloud", required=True)
    replace.add_argument("--resource-manifest", default=str(RESOURCE_MANIFEST))
    replace.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None, *, runner=None, out=None) -> int:
    arguments = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    stream = sys.stdout if out is None else out
    runner = subprocess_runner if runner is None else runner
    try:
        if arguments.command == "plan":
            plan = render_plan(
                runtime_path=arguments.runtime,
                image_digest=arguments.image_digest,
                build_record_path=arguments.build_record,
                output_path=arguments.output,
                resource_manifest_path=arguments.resource_manifest,
                engine_manifest_path=arguments.engine_manifest,
                iam_delta_path=arguments.iam_delta,
            )
            written = write_plan(plan, arguments.output)
            result = {
                "status": "planned",
                "plan_sha256": plan["plan_sha256"],
                "jobs": [job["job_id"] for job in plan["jobs"]],
                "operations": len(plan["operations"]),
                "written": [path.name for path in written],
            }
            code = 0
        elif arguments.command == "apply":
            plan, _digest = _load_json(arguments.plan, "plan_invalid")
            verify_plan(plan)
            manifest = load_resource_manifest(
                arguments.resource_manifest,
                expected_sha256=plan.get("resource_manifest_sha256"),
            )
            receipt = apply_plan(
                plan,
                runner=runner,
                gcloud=arguments.gcloud,
                plan_dir=Path(arguments.plan).resolve().parent,
                plan_name=Path(arguments.plan).name,
                manifest=manifest,
                dry_run=arguments.dry_run,
                out=stream,
            )
            if not arguments.dry_run:
                _write_json(arguments.output, receipt)
            result = {
                "status": receipt["status"],
                "plan_sha256": receipt["plan_sha256"],
                "counts": receipt["counts"],
                "stopped_at": receipt["stopped_at"],
            }
            code = 0 if receipt["status"] in ("applied", "dry_run") else 1
        elif arguments.command == "replace":
            output = Path(arguments.output)
            if output.exists():
                _refuse("output_exists")
            plan, _digest = _load_json(arguments.plan, "plan_invalid")
            verify_plan(plan)
            manifest = load_resource_manifest(
                arguments.resource_manifest,
                expected_sha256=plan.get("resource_manifest_sha256"),
            )
            try:
                receipt = replace_job(
                    plan,
                    arguments.job,
                    runner=runner,
                    gcloud=arguments.gcloud,
                    specification_path=output.with_name(output.stem + ".job.yaml"),
                    manifest=manifest,
                    dry_run=arguments.dry_run,
                    out=stream,
                )
            except ValueError as exc:
                if getattr(exc, "receipt", None) is not None and not arguments.dry_run:
                    _write_json(output, exc.receipt)
                raise
            if not arguments.dry_run:
                _write_json(output, receipt)
            result = {
                "status": receipt["status"],
                "plan_sha256": receipt["plan_sha256"],
                "job_id": receipt["job_id"],
                "changed_fields": receipt.get("changed_fields"),
                "error": receipt.get("error"),
            }
            code = 0 if receipt["status"] in ("replaced", "unchanged", "dry_run") else 1
        else:
            plan, _digest = _load_json(arguments.plan, "plan_invalid")
            report = read_back(plan, runner=runner, gcloud=arguments.gcloud)
            _write_json(arguments.output, report)
            result = {
                "status": report["status"],
                "plan_sha256": report["plan_sha256"],
                "summary": report["summary"],
            }
            code = 0 if report["status"] == "match" else 1
    except ValueError as exc:
        stream.write(
            json.dumps({"status": "refused", "error": str(exc)}, sort_keys=True) + "\n"
        )
        return 1
    stream.write(json.dumps(result, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
