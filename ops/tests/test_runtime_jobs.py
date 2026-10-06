import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path

import pytest

from ops.deploy import runtime_jobs
from ops.runners.managed_runtime import (
    CONFIGURATION_ANNOTATION,
    DAILY_JOB_ID,
    PRICE_POLICY_JOB_ID,
    canonical_bytes,
    canonical_sha256,
)

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILE = ROOT / "infra" / "runtime" / "daily-staging.json"
MANIFEST_FILE = ROOT / "ops" / "deploy" / "resource_manifest.json"
DELTA_FILE = ROOT / "ops" / "deploy" / "iam_delta_v1.json"
REPOSITORY = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine"
DIGEST = "sha256:" + "4" * 64
GCLOUD = "gcloud.cmd"
JOB_PREFIX = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/"
)
DAILY_RESOURCE = JOB_PREFIX + DAILY_JOB_ID
DENIED = "ERROR: PERMISSION_DENIED: Permission 'run.jobs.get' denied on resource."
DENIED_GENERIC = (
    "ERROR: (gcloud.run.jobs.describe) [x@y.iam.gserviceaccount.com] does not have "
    "permission to access job [intelligence-42-daily-staging] (or it may not exist)"
)
DAILY_JOB_ENV = [
    {"name": "TRENDS_ENV", "value": "staging"},
    {"name": "BIGQUERY_DATASET", "value": "intelligence_42_sources_staging"},
]
APPROVED = {
    "applied": False,
    "approved_at": "2026-09-13T08:00:00Z",
    "approved_by": "reviewer",
    "state": "approved",
}


def _not_found(job_id, form="sdk"):
    if form == "sdk":
        return f"ERROR: (gcloud.run.jobs.describe) Cannot find job [{job_id}]."
    return f"ERROR: (gcloud.run.jobs.describe) Job [{job_id}] not found"


def _build_record(status="SUCCESS", images=None):
    if images is None:
        images = [{"name": f"{REPOSITORY}:{'a' * 40}", "digest": DIGEST}]
    return {"id": "b0a1", "status": status, "results": {"images": images}}


def _tree(tmp_path, *, manifest=None, engine_manifest=None, build=None, approval=None):
    root = tmp_path / "tree"
    root.mkdir()
    manifest_bytes = (
        MANIFEST_FILE.read_bytes() if manifest is None else canonical_bytes(manifest)
    )
    engine_bytes = (
        manifest_bytes if engine_manifest is None else canonical_bytes(engine_manifest)
    )
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    config = json.loads(RUNTIME_FILE.read_bytes())
    config["resource_manifest_sha256"] = manifest_sha256
    delta = json.loads(DELTA_FILE.read_bytes())
    delta["resource_manifest_sha256"] = manifest_sha256
    if approval != "tree":
        delta["approval"] = dict(APPROVED if approval is None else approval)
    paths = {
        "runtime": root / "daily-staging.json",
        "manifest": root / "resource_manifest.json",
        "engine": root / "resource_manifest_v1.json",
        "delta": root / "iam_delta_v1.json",
        "build": root / "build.json",
        "output": root / "out" / "plan.json",
    }
    paths["runtime"].write_bytes(canonical_bytes(config))
    paths["manifest"].write_bytes(manifest_bytes)
    paths["engine"].write_bytes(engine_bytes)
    paths["delta"].write_bytes(canonical_bytes(delta))
    paths["build"].write_text(
        json.dumps(_build_record() if build is None else build), encoding="utf-8"
    )
    return paths


def _render(paths, image_digest=DIGEST):
    return runtime_jobs.render_plan(
        runtime_path=paths["runtime"],
        image_digest=image_digest,
        build_record_path=paths["build"],
        output_path=paths["output"],
        resource_manifest_path=paths["manifest"],
        engine_manifest_path=paths["engine"],
        iam_delta_path=paths["delta"],
    )


def _plan(tmp_path, **tree):
    paths = _tree(tmp_path, **tree)
    plan = _render(paths)
    runtime_jobs.write_plan(plan, paths["output"])
    return plan, paths


def _manifest_dict(paths):
    return json.loads(paths["manifest"].read_bytes())


def _job(plan, job_id):
    return next(job for job in plan["jobs"] if job["job_id"] == job_id)


def _job_of(plan, argv):
    if argv[3] == "replace":
        name = Path(argv[4]).name
        return next(job for job in plan["jobs"] if job["specification_file"] == name)
    return _job(plan, argv[4])


def _v1_job(job, *, timeout_form="string", omit_max_retries=False, **drift):
    expected = {**job["expected"], **drift}
    annotations = {
        **expected["annotations"],
        "run.googleapis.com/client-name": "gcloud",
    }
    timeout = expected["timeout_seconds"]
    task = {
        "containers": [
            {
                "args": expected["args"],
                "command": expected["command"],
                "env": expected["env"],
                "image": expected["image"],
            }
        ],
        "maxRetries": expected["max_retries"],
        "serviceAccountName": expected["service_account"],
        "timeoutSeconds": str(timeout) if timeout_form == "string" else timeout,
    }
    if omit_max_retries:
        del task["maxRetries"]
    return {
        "apiVersion": "run.googleapis.com/v1",
        "kind": "Job",
        "metadata": {"name": job["job_id"]},
        "spec": {
            "template": {
                "metadata": {"annotations": annotations},
                "spec": {
                    "parallelism": expected["parallelism"],
                    "taskCount": expected["task_count"],
                    "template": {"spec": task},
                },
            }
        },
    }


def _v2_job(job):
    expected = job["expected"]
    return {
        "name": f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{job['job_id']}",
        "template": {
            "annotations": dict(expected["annotations"]),
            "parallelism": expected["parallelism"],
            "taskCount": expected["task_count"],
            "template": {
                "containers": [
                    {
                        "args": expected["args"],
                        "command": expected["command"],
                        "env": expected["env"],
                        "image": expected["image"],
                    }
                ],
                "serviceAccount": expected["service_account"],
                "timeout": f"{expected['timeout_seconds']}s",
            },
        },
    }


def _policy(job, *, drop=None, extra=None):
    roles = {}
    for binding in job["bindings"]:
        if binding != drop:
            roles.setdefault(binding["role"], []).append(binding["member"])
    for binding in extra or []:
        roles.setdefault(binding["role"], []).append(binding["member"])
    return {
        "bindings": [
            {"members": members, "role": role} for role, members in roles.items()
        ],
        "etag": "BwX",
        "version": 1,
    }


class FakeRunner:
    def __init__(self, script):
        self.calls = []
        self.cwds = []
        self._script = script

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        self.cwds.append(cwd)
        return self._script(argv)


def _absent_script(plan, overrides=None, not_found_form="sdk"):
    overrides = overrides or {}

    def script(argv):
        verb, job = argv[3], _job_of(plan, argv)
        key = (verb, job["job_id"])
        if key in overrides:
            return overrides[key]
        if verb == "describe":
            return 1, "", _not_found(job["job_id"], not_found_form)
        if verb == "replace":
            return 0, json.dumps(_v1_job(job)), ""
        if verb == "add-iam-policy-binding":
            return 0, json.dumps(_policy(job)), ""
        if verb == "get-iam-policy":
            return 0, json.dumps(_policy(job)), ""
        raise AssertionError(argv)

    return script


def _apply(plan, paths, runner, dry_run=False, out=None, manifest=None):
    return runtime_jobs.apply_plan(
        plan,
        runner=runner,
        gcloud=GCLOUD,
        plan_dir=paths["output"].parent,
        plan_name=paths["output"].name,
        dry_run=dry_run,
        manifest=_manifest_dict(paths) if manifest is None else manifest,
        out=out or io.StringIO(),
    )


def _reseal(plan):
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    plan["plan_sha256"] = canonical_sha256(body)
    return plan


def _apply_argv(paths, receipt, extra=()):
    return [
        "apply",
        "--plan",
        str(paths["output"]),
        "--output",
        str(receipt),
        "--gcloud",
        GCLOUD,
        "--resource-manifest",
        str(paths["manifest"]),
        *extra,
    ]


def _plan_argv(paths):
    return [
        "plan",
        "--runtime",
        str(paths["runtime"]),
        "--image-digest",
        DIGEST,
        "--build-record",
        str(paths["build"]),
        "--output",
        str(paths["output"]),
        "--resource-manifest",
        str(paths["manifest"]),
        "--engine-manifest",
        str(paths["engine"]),
        "--iam-delta",
        str(paths["delta"]),
    ]


def test_plan_happy_path_renders_jobs_bindings_and_digest(tmp_path):
    plan, paths = _plan(tmp_path)
    config = json.loads(paths["runtime"].read_bytes())
    manifest_sha256 = hashlib.sha256(paths["manifest"].read_bytes()).hexdigest()

    assert plan["contract_version"] == "42_runtime_jobs_plan_v1"
    assert [job["job_id"] for job in plan["jobs"]] == [
        DAILY_JOB_ID,
        PRICE_POLICY_JOB_ID,
    ]
    assert plan["runtime_configuration_sha256"] == canonical_sha256(config)
    assert plan["resource_manifest_sha256"] == manifest_sha256
    assert plan["inputs"]["build_record"]["build_id"] == "b0a1"
    assert plan["inputs"]["build_record"]["image_digest"] == DIGEST
    assert plan["inputs"]["iam_delta"]["approval"] == APPROVED
    # Amendment e adds the daily account's run with overrides row on the daily job,
    # and the serving identity's viewer row on the funded pilot job, which is deferred.
    assert plan["inputs"]["iam_delta"]["job_bindings"] == {
        "declared": 6,
        "deferred_other_jobs": 22,
    }

    daily = _job(plan, DAILY_JOB_ID)
    expected = daily["expected"]
    entry = config["jobs"][DAILY_JOB_ID]
    assert expected["image"] == f"{REPOSITORY}@{DIGEST}"
    assert expected["service_account"] == entry["service_account"]
    assert expected["command"] == entry["command"]
    assert expected["args"] == entry["args"]
    assert expected["env"] == DAILY_JOB_ENV
    assert _job(plan, PRICE_POLICY_JOB_ID)["expected"]["env"] == []
    assert expected["timeout_seconds"] == entry["timeout_seconds"]
    assert expected["max_retries"] == 0
    assert expected["task_count"] == 1
    assert expected["parallelism"] == 1
    assert expected["annotations"] == {
        CONFIGURATION_ANNOTATION: canonical_sha256(config),
        runtime_jobs.MANIFEST_ANNOTATION: manifest_sha256,
    }
    assert daily["guard"] == {"resource": DAILY_RESOURCE, "actions": ["deploy", "read"]}

    task = daily["specification"]["spec"]["template"]["spec"]["template"]["spec"]
    assert daily["specification"]["kind"] == "Job"
    assert daily["specification"]["metadata"] == {"name": DAILY_JOB_ID}
    assert daily["specification"]["spec"]["template"]["metadata"] == {
        "annotations": expected["annotations"]
    }
    assert task["containers"][0]["image"] == expected["image"]
    assert task["maxRetries"] == 0
    assert task["timeoutSeconds"] == entry["timeout_seconds"]
    assert task["serviceAccountName"] == entry["service_account"]
    sidecar = paths["output"].parent / daily["specification_file"]
    assert daily["specification_file"] == f"plan-{DAILY_JOB_ID}.job.yaml"
    assert (
        hashlib.sha256(sidecar.read_bytes()).hexdigest()
        == daily["specification_sha256"]
    )
    assert json.loads(sidecar.read_bytes()) == daily["specification"]

    delta = json.loads(paths["delta"].read_bytes())
    job_rows = [
        row for row in delta["bindings"] if row["resource"].startswith(JOB_PREFIX)
    ]
    # Amendment e adds two job rows: run with overrides on the daily job and the
    # serving identity's viewer role on the funded pilot job.
    assert len(job_rows) == 28
    declared = {job["resource"] for job in plan["jobs"]}
    declared_rows = [row for row in job_rows if row["resource"] in declared]
    assert len(declared_rows) == 6
    assert sum(len(job["bindings"]) for job in plan["jobs"]) == 6
    assert {(b["member"], b["role"]) for b in daily["bindings"]} == {
        (row["member"], row["role"])
        for row in declared_rows
        if row["resource"] == DAILY_RESOURCE
    }
    assert all(op["resource"] in declared for op in plan["operations"])

    kinds = [(op["kind"], op["job_id"]) for op in plan["operations"]]
    assert kinds == [
        ("precheck", DAILY_JOB_ID),
        ("create_job", DAILY_JOB_ID),
        ("add_binding", DAILY_JOB_ID),
        ("add_binding", DAILY_JOB_ID),
        ("add_binding", DAILY_JOB_ID),
        ("add_binding", DAILY_JOB_ID),
        ("precheck", PRICE_POLICY_JOB_ID),
        ("create_job", PRICE_POLICY_JOB_ID),
        ("add_binding", PRICE_POLICY_JOB_ID),
        ("add_binding", PRICE_POLICY_JOB_ID),
    ]
    assert [op["index"] for op in plan["operations"]] == list(range(10))
    create = plan["operations"][1]["argv"]
    assert create == [
        "gcloud",
        "run",
        "jobs",
        "replace",
        daily["specification_file"],
        "--project=ogilvy-trends-v2",
        "--region=us-central1",
        "--format=json",
        "--quiet",
    ]
    binding = plan["operations"][2]
    assert binding["argv"][:5] == [
        "gcloud",
        "run",
        "jobs",
        "add-iam-policy-binding",
        DAILY_JOB_ID,
    ]
    assert f"--member={binding['member']}" in binding["argv"]
    assert f"--role={binding['role']}" in binding["argv"]
    assert plan["operations"][0]["argv"][:5] == [
        "gcloud",
        "run",
        "jobs",
        "describe",
        DAILY_JOB_ID,
    ]

    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    assert plan["plan_sha256"] == canonical_sha256(body)
    assert (
        runtime_jobs.verify_plan(json.loads(paths["output"].read_bytes()))
        == plan["plan_sha256"]
    )
    assert "tree" not in json.dumps(plan)


def test_runtime_manifest_mismatch_refused(tmp_path):
    paths = _tree(tmp_path, engine_manifest={"different": True})
    with pytest.raises(ValueError, match="runtime_manifest_mismatch"):
        _render(paths)


def test_delta_bound_to_another_manifest_refused(tmp_path):
    paths = _tree(tmp_path)
    delta = json.loads(paths["delta"].read_bytes())
    delta["resource_manifest_sha256"] = "0" * 64
    paths["delta"].write_bytes(canonical_bytes(delta))
    with pytest.raises(ValueError, match="iam_delta_manifest_mismatch"):
        _render(paths)


def test_proposed_delta_refused_as_unapproved(tmp_path):
    proposed = {
        "applied": False,
        "approved_at": None,
        "approved_by": None,
        "state": "proposed",
    }
    paths = _tree(tmp_path, approval=proposed)
    delta = json.loads(paths["delta"].read_bytes())
    assert delta["approval"]["state"] == "proposed"
    with pytest.raises(ValueError, match="iam_delta_unapproved"):
        _render(paths)


@pytest.mark.parametrize(
    "approval",
    [
        {**APPROVED, "approved_by": None},
        {**APPROVED, "approved_at": None},
        {**APPROVED, "applied": True},
        {"state": "approved"},
    ],
    ids=["no_approver", "no_timestamp", "applied", "open_block"],
)
def test_malformed_approval_refused(tmp_path, approval):
    paths = _tree(tmp_path, approval=approval)
    with pytest.raises(ValueError, match="iam_delta_invalid"):
        _render(paths)


@pytest.mark.parametrize(
    "build",
    [
        _build_record(
            images=[{"name": f"{REPOSITORY}:tag", "digest": "sha256:" + "5" * 64}]
        ),
        _build_record(images=[{"name": f"{REPOSITORY}-other:tag", "digest": DIGEST}]),
        _build_record(
            images=[
                {"name": f"{REPOSITORY}:one", "digest": DIGEST},
                {"name": f"{REPOSITORY}:two", "digest": DIGEST},
            ]
        ),
        _build_record(status="FAILURE"),
        _build_record(images=[]),
    ],
    ids=["wrong_digest", "wrong_name", "two_images", "not_success", "no_image"],
)
def test_image_unbound_refused(tmp_path, build):
    paths = _tree(tmp_path, build=build)
    with pytest.raises(ValueError, match="image_unbound"):
        _render(paths)


def test_malformed_image_digest_refused(tmp_path):
    paths = _tree(tmp_path)
    with pytest.raises(ValueError, match="image_digest_invalid"):
        _render(paths, image_digest="latest")


def test_guard_refusal_stops_plan(tmp_path):
    manifest = json.loads(MANIFEST_FILE.read_bytes())
    for row in manifest["resources"]:
        if row["name"] == DAILY_RESOURCE:
            row["actions"] = ["invoke", "read"]
    paths = _tree(tmp_path, manifest=manifest)
    with pytest.raises(ValueError, match="resource_action_forbidden"):
        _render(paths)
    assert not paths["output"].parent.exists()


def test_apply_refuses_tampered_plan_and_writes_no_receipt(tmp_path):
    plan, paths = _plan(tmp_path)
    tampered = deepcopy(plan)
    tampered["operations"][2]["argv"][-3] = "--member=user:someone@example.com"
    runner = FakeRunner(_absent_script(plan))
    with pytest.raises(ValueError, match="plan_mismatch"):
        _apply(tampered, paths, runner)
    assert runner.calls == []

    paths["output"].write_text(json.dumps(tampered), encoding="utf-8")
    receipt = paths["output"].parent / "receipt.json"
    code = runtime_jobs.main(
        _apply_argv(paths, receipt), runner=runner, out=io.StringIO()
    )
    assert code == 1
    assert not receipt.exists()
    assert runner.calls == []


def test_apply_refuses_reshaped_operations_with_matching_digest(tmp_path):
    plan, paths = _plan(tmp_path)
    forged = deepcopy(plan)
    forged["operations"][1]["argv"] = ["gcloud", "run", "jobs", "delete", DAILY_JOB_ID]
    runner = FakeRunner(_absent_script(plan))
    with pytest.raises(ValueError, match="plan_invalid"):
        _apply(_reseal(forged), paths, runner)
    assert runner.calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan["jobs"][0]["expected"].__setitem__("args", 5),
        lambda plan: plan["jobs"][0]["expected"].__setitem__("annotations", 3),
        lambda plan: plan["jobs"][0]["expected"].__setitem__("command", None),
        lambda plan: plan["jobs"][0].__setitem__("bindings", [{}]),
        lambda plan: plan["jobs"][0].__setitem__(
            "bindings", [{"member": 1, "role": 2}]
        ),
        lambda plan: plan["jobs"][0].__setitem__("expected", "python"),
        lambda plan: plan.__setitem__("jobs", [None]),
        lambda plan: plan.__setitem__("operations", None),
    ],
    ids=[
        "args_int",
        "annotations_int",
        "command_none",
        "binding_empty",
        "binding_typed",
        "expected_str",
        "job_none",
        "operations_none",
    ],
)
def test_resealed_plan_with_wrong_types_refuses_plan_invalid(tmp_path, mutate):
    plan, paths = _plan(tmp_path)
    forged = deepcopy(plan)
    mutate(forged)
    _reseal(forged)
    runner = FakeRunner(_absent_script(plan))
    with pytest.raises(ValueError, match="plan_invalid"):
        _apply(forged, paths, runner)
    with pytest.raises(ValueError, match="plan_invalid"):
        runtime_jobs.read_back(forged, runner=runner, gcloud=GCLOUD)
    assert runner.calls == []
    paths["output"].write_text(json.dumps(forged), encoding="utf-8")
    out = io.StringIO()
    code = runtime_jobs.main(
        _apply_argv(paths, paths["output"].parent / "receipt.json"),
        runner=runner,
        out=out,
    )
    assert code == 1
    assert json.loads(out.getvalue()) == {"error": "plan_invalid", "status": "refused"}


def test_apply_refuses_specification_sidecar_drift(tmp_path):
    plan, paths = _plan(tmp_path)
    sidecar = paths["output"].parent / _job(plan, DAILY_JOB_ID)["specification_file"]
    drifted = json.loads(sidecar.read_bytes())
    drifted["spec"]["template"]["spec"]["template"]["spec"]["maxRetries"] = 3
    sidecar.write_text(json.dumps(drifted, indent=2, sort_keys=True) + "\n")
    runner = FakeRunner(_absent_script(plan))
    with pytest.raises(ValueError, match="specification_mismatch"):
        _apply(plan, paths, runner)
    assert runner.calls == []


@pytest.mark.parametrize("not_found_form", ["sdk", "generic"])
def test_apply_happy_path_records_argv_exit_and_tails(tmp_path, not_found_form):
    plan, paths = _plan(tmp_path)
    runner = FakeRunner(_absent_script(plan, not_found_form=not_found_form))
    receipt = _apply(plan, paths, runner)

    assert receipt["contract_version"] == "42_runtime_jobs_receipt_v1"
    assert receipt["status"] == "applied"
    assert receipt["plan_sha256"] == plan["plan_sha256"]
    assert receipt["plan_file"] == "plan.json"
    assert receipt["dry_run"] is False
    assert len(receipt["operations"]) == 10
    assert len(runner.calls) == 10
    states = [op["state"] for op in receipt["operations"]]
    assert states == [
        "absent",
        "applied",
        "applied",
        "applied",
        "applied",
        "applied",
    ] + [
        "absent",
        "applied",
        "applied",
        "applied",
    ]
    assert receipt["operations"][0]["stderr_tail"] == _not_found(
        DAILY_JOB_ID, not_found_form
    )
    create = receipt["operations"][1]
    assert create["argv"] == [GCLOUD] + plan["operations"][1]["argv"][1:]
    assert create["argv"][4] == _job(plan, DAILY_JOB_ID)["specification_file"]
    assert all(cwd == paths["output"].parent for cwd in runner.cwds)
    assert create["exit_code"] == 0
    assert len(create["stdout_tail"]) <= 500
    assert create["stdout_tail"] == json.dumps(_v1_job(_job(plan, DAILY_JOB_ID)))[-500:]
    assert create["stderr_tail"] == ""
    assert receipt["counts"] == {"absent": 2, "applied": 8}
    serialised = json.dumps(receipt)
    assert str(paths["output"].parent) not in serialised
    assert str(paths["output"].parent.resolve()) not in serialised
    assert "tree" not in serialised


def test_apply_treats_existing_job_and_binding_as_idempotent_states(tmp_path):
    plan, paths = _plan(tmp_path)
    found = json.dumps(_v1_job(_job(plan, DAILY_JOB_ID)))
    exists = "ERROR: (gcloud.run.jobs.replace) ALREADY_EXISTS: job already exists"
    overrides = {
        ("describe", DAILY_JOB_ID): (0, found, ""),
        ("replace", PRICE_POLICY_JOB_ID): (1, "", exists),
        ("add-iam-policy-binding", PRICE_POLICY_JOB_ID): (
            1,
            "",
            "Binding already exists.",
        ),
    }
    runner = FakeRunner(_absent_script(plan, overrides))
    receipt = _apply(plan, paths, runner)

    states = [op["state"] for op in receipt["operations"]]
    assert states == [
        "already_exists",
        "skipped_existing",
        "applied",
        "applied",
        "applied",
        "applied",
        "absent",
        "already_exists",
        "already_exists",
        "already_exists",
    ]
    assert receipt["status"] == "applied"
    assert [call[3] for call in runner.calls] == [
        "describe",
        "add-iam-policy-binding",
        "add-iam-policy-binding",
        "add-iam-policy-binding",
        "add-iam-policy-binding",
        "describe",
        "replace",
        "add-iam-policy-binding",
        "add-iam-policy-binding",
    ]
    assert "exit_code" not in receipt["operations"][1]


def test_apply_stops_on_first_failure(tmp_path):
    plan, paths = _plan(tmp_path)
    overrides = {("add-iam-policy-binding", DAILY_JOB_ID): (1, "", DENIED)}
    runner = FakeRunner(_absent_script(plan, overrides))
    receipt = _apply(plan, paths, runner)

    states = [op["state"] for op in receipt["operations"]]
    assert states == ["absent", "applied", "refused"] + ["not_run"] * 7
    assert receipt["status"] == "failed"
    assert receipt["stopped_at"] == 2
    assert len(runner.calls) == 3
    assert receipt["operations"][2]["exit_code"] == 1
    assert receipt["operations"][2]["stderr_tail"] == DENIED
    assert all("exit_code" not in op for op in receipt["operations"][3:])


@pytest.mark.parametrize("denied", [DENIED, DENIED_GENERIC])
def test_apply_refused_precheck_stops_before_create(tmp_path, denied):
    plan, paths = _plan(tmp_path)
    runner = FakeRunner(
        _absent_script(plan, {("describe", DAILY_JOB_ID): (1, "", denied)})
    )
    receipt = _apply(plan, paths, runner)
    assert [op["state"] for op in receipt["operations"]] == ["refused"] + [
        "not_run"
    ] * 9
    assert len(runner.calls) == 1


def test_apply_unclassified_precheck_failure_stops(tmp_path):
    plan, paths = _plan(tmp_path)
    error = "ERROR: gcloud crashed (SSLError): connection reset"
    runner = FakeRunner(
        _absent_script(plan, {("describe", DAILY_JOB_ID): (1, "", error)})
    )
    receipt = _apply(plan, paths, runner)
    assert [op["state"] for op in receipt["operations"]] == ["failed"] + ["not_run"] * 9
    assert receipt["stopped_at"] == 0


def test_apply_dry_run_prints_argv_and_writes_nothing(tmp_path):
    plan, paths = _plan(tmp_path)
    before = sorted(path.name for path in paths["output"].parent.iterdir())
    runner = FakeRunner(_absent_script(plan))
    receipt = paths["output"].parent / "receipt.json"
    out = io.StringIO()
    code = runtime_jobs.main(
        _apply_argv(paths, receipt, ["--dry-run"]), runner=runner, out=out
    )
    assert code == 0
    assert runner.calls == []
    assert not receipt.exists()
    assert sorted(path.name for path in paths["output"].parent.iterdir()) == before
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    printed = [line for line in lines if isinstance(line, list)]
    assert len(printed) == 10
    assert all(line[0] == GCLOUD for line in printed)
    assert printed[1][3] == "replace"
    assert printed[1][4] == _job(plan, DAILY_JOB_ID)["specification_file"]
    assert lines[-1]["status"] == "dry_run"


def test_apply_command_writes_receipt(tmp_path):
    plan, paths = _plan(tmp_path)
    runner = FakeRunner(_absent_script(plan))
    receipt = paths["output"].parent / "receipt.json"
    out = io.StringIO()
    code = runtime_jobs.main(_apply_argv(paths, receipt), runner=runner, out=out)
    assert code == 0
    written = json.loads(receipt.read_bytes())
    assert written["status"] == "applied"
    assert written["plan_sha256"] == plan["plan_sha256"]
    assert written["plan_file"] == "plan.json"
    assert len(written["operations"]) == 10
    assert str(paths["output"].parent.resolve()) not in receipt.read_text(
        encoding="utf-8"
    )
    assert json.loads(out.getvalue().splitlines()[-1])["status"] == "applied"


@pytest.mark.parametrize("keep", [["invoke", "read"], ["deploy", "invoke"]])
def test_apply_guard_refusal_at_apply_time(tmp_path, keep):
    plan, paths = _plan(tmp_path)
    manifest = _manifest_dict(paths)
    for row in manifest["resources"]:
        if row["name"] == DAILY_RESOURCE:
            row["actions"] = keep
    runner = FakeRunner(_absent_script(plan))
    with pytest.raises(ValueError, match="resource_action_forbidden"):
        _apply(plan, paths, runner, manifest=manifest)
    assert runner.calls == []


def test_apply_and_readback_use_runner_result_shape(tmp_path):
    plan, paths = _plan(tmp_path)
    runner = FakeRunner(lambda argv: "not a tuple")
    with pytest.raises(ValueError, match="runner_result_invalid"):
        _apply(plan, paths, runner)
    with pytest.raises(ValueError, match="runner_result_invalid"):
        runtime_jobs.read_back(plan, runner=runner, gcloud=GCLOUD)


def test_subprocess_runner_reports_missing_executable(tmp_path):
    exit_code, stdout, stderr = runtime_jobs.subprocess_runner(
        [str(tmp_path / "no-such-gcloud.cmd"), "run"], cwd=tmp_path
    )
    assert exit_code == -1
    assert stdout == ""
    assert stderr.startswith("runner error:")


@pytest.mark.parametrize("shape", ["v1", "v1_int", "v1_no_max_retries", "v2"])
def test_readback_match(tmp_path, shape):
    plan, _paths = _plan(tmp_path)

    def script(argv):
        job = _job_of(plan, argv)
        if argv[3] == "describe":
            if shape == "v1":
                body = _v1_job(job)
            elif shape == "v1_int":
                body = _v1_job(job, timeout_form="int")
            elif shape == "v1_no_max_retries":
                body = _v1_job(job, omit_max_retries=True)
            else:
                body = _v2_job(job)
            return 0, json.dumps(body), ""
        if argv[3] == "get-iam-policy":
            return 0, json.dumps(_policy(job)), ""
        raise AssertionError(argv)

    runner = FakeRunner(script)
    report = runtime_jobs.read_back(plan, runner=runner, gcloud=GCLOUD)

    assert report["contract_version"] == "42_runtime_jobs_readback_v1"
    assert report["plan_sha256"] == plan["plan_sha256"]
    assert report["status"] == "match"
    assert report["summary"] == {
        "jobs": 2,
        "match": 2,
        "mismatch": 0,
        "refused_reads": 0,
        "absent": 0,
        "extra_bindings": 0,
    }
    assert [call[3] for call in runner.calls] == ["describe", "get-iam-policy"] * 2
    assert all(call[0] == GCLOUD for call in runner.calls)
    daily = report["jobs"][0]
    assert daily["job_id"] == DAILY_JOB_ID
    assert daily["match"] is True
    assert daily["reasons"] == []
    assert daily["describe"]["state"] == "read"
    assert daily["policy"]["state"] == "read"
    fields = daily["fields"]
    assert set(fields) == {
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
        "container_count",
    }
    assert all(field["match"] for field in fields.values())
    assert fields["image"]["observed"] == f"{REPOSITORY}@{DIGEST}"
    assert fields["max_retries"] == {"expected": 0, "observed": 0, "match": True}
    assert fields["timeout_seconds"] == {
        "expected": 3600,
        "observed": 3600,
        "match": True,
    }
    assert fields["task_count"]["observed"] == 1
    assert fields["parallelism"]["observed"] == 1
    assert daily["bindings"]["missing"] == []
    assert daily["bindings"]["extra"] == []
    assert daily["bindings"]["match"] is True


def test_readback_v1_string_integers_are_compared_as_integers(tmp_path):
    plan, _paths = _plan(tmp_path)
    daily = _job(plan, DAILY_JOB_ID)
    body = _v1_job(daily)
    execution = body["spec"]["template"]["spec"]
    execution["taskCount"] = "1"
    execution["parallelism"] = "1"
    execution["template"]["spec"]["maxRetries"] = "0"
    assert execution["template"]["spec"]["timeoutSeconds"] == "3600"
    observed = runtime_jobs._observed_job(body)
    assert observed["timeout_seconds"] == 3600
    assert observed["max_retries"] == 0
    assert observed["task_count"] == 1
    assert observed["parallelism"] == 1
    fields = runtime_jobs._diff_fields(daily["expected"], observed)
    assert all(field["match"] for field in fields.values())

    drifted = _v1_job(daily)
    drifted["spec"]["template"]["spec"]["template"]["spec"]["timeoutSeconds"] = "7200"
    fields = runtime_jobs._diff_fields(
        daily["expected"], runtime_jobs._observed_job(drifted)
    )
    assert fields["timeout_seconds"] == {
        "expected": 3600,
        "observed": 7200,
        "match": False,
    }
    odd = _v1_job(daily)
    odd["spec"]["template"]["spec"]["template"]["spec"]["timeoutSeconds"] = "1h"
    fields = runtime_jobs._diff_fields(
        daily["expected"], runtime_jobs._observed_job(odd)
    )
    assert fields["timeout_seconds"] == {
        "expected": 3600,
        "observed": "1h",
        "match": False,
    }


def test_readback_mismatch_reports_fields_and_bindings(tmp_path):
    plan, _paths = _plan(tmp_path)
    daily = _job(plan, DAILY_JOB_ID)
    dropped = daily["bindings"][0]
    extra = {"member": "user:someone@example.com", "role": "roles/run.invoker"}

    def script(argv):
        job = _job_of(plan, argv)
        if argv[3] == "describe":
            if job["job_id"] == DAILY_JOB_ID:
                drift = _v1_job(
                    job,
                    image=f"{REPOSITORY}:latest",
                    max_retries=3,
                    annotations={CONFIGURATION_ANNOTATION: "0" * 64},
                )
                return 0, json.dumps(drift), ""
            return 0, json.dumps(_v1_job(job)), ""
        if job["job_id"] == DAILY_JOB_ID:
            return 0, json.dumps(_policy(job, drop=dropped, extra=[extra])), ""
        return 0, json.dumps(_policy(job)), ""

    report = runtime_jobs.read_back(plan, runner=FakeRunner(script), gcloud=GCLOUD)

    assert report["status"] == "mismatch"
    assert report["summary"]["match"] == 1
    assert report["summary"]["mismatch"] == 1
    assert report["summary"]["extra_bindings"] == 1
    first = report["jobs"][0]
    assert first["match"] is False
    assert first["reasons"] == ["fields_mismatch", "bindings_missing"]
    fields = first["fields"]
    assert fields["image"] == {
        "expected": f"{REPOSITORY}@{DIGEST}",
        "observed": f"{REPOSITORY}:latest",
        "match": False,
    }
    assert fields["max_retries"]["observed"] == 3
    assert fields["max_retries"]["match"] is False
    assert fields["annotations"]["match"] is False
    assert fields["annotations"]["observed"][CONFIGURATION_ANNOTATION] == "0" * 64
    assert fields["annotations"]["observed"][runtime_jobs.MANIFEST_ANNOTATION] is None
    assert fields["service_account"]["match"] is True
    assert first["bindings"]["missing"] == [dropped]
    assert first["bindings"]["extra"] == [extra]
    assert first["bindings"]["match"] is False
    assert report["jobs"][1]["match"] is True


def test_readback_against_the_prior_empty_environment_shows_only_the_env_delta(
    tmp_path,
):
    plan, _paths = _plan(tmp_path)

    def script(argv):
        job = _job_of(plan, argv)
        if argv[3] == "describe":
            if job["job_id"] == DAILY_JOB_ID:
                return 0, json.dumps(_v1_job(job, env=[])), ""
            return 0, json.dumps(_v1_job(job)), ""
        if argv[3] == "get-iam-policy":
            return 0, json.dumps(_policy(job)), ""
        raise AssertionError(argv)

    report = runtime_jobs.read_back(plan, runner=FakeRunner(script), gcloud=GCLOUD)

    assert report["status"] == "mismatch"
    assert report["summary"]["match"] == 1
    assert report["summary"]["mismatch"] == 1
    daily = report["jobs"][0]
    assert daily["job_id"] == DAILY_JOB_ID
    assert daily["reasons"] == ["fields_mismatch"]
    assert daily["fields"]["env"] == {
        "expected": DAILY_JOB_ENV,
        "observed": [],
        "match": False,
    }
    assert [name for name, field in daily["fields"].items() if not field["match"]] == [
        "env"
    ]
    assert daily["bindings"]["match"] is True
    assert report["jobs"][1]["match"] is True


@pytest.mark.parametrize("not_found_form", ["sdk", "generic"])
def test_readback_records_refused_and_absent_reads(tmp_path, not_found_form):
    plan, _paths = _plan(tmp_path)

    def script(argv):
        job = _job_of(plan, argv)
        if job["job_id"] == DAILY_JOB_ID:
            return 1, "", DENIED
        if argv[3] == "describe":
            return 1, "", _not_found(job["job_id"], not_found_form)
        return 0, json.dumps(_policy(job)), ""

    runner = FakeRunner(script)
    report = runtime_jobs.read_back(plan, runner=runner, gcloud=GCLOUD)

    assert report["status"] == "mismatch"
    assert all(cwd is None for cwd in runner.cwds)
    first, second = report["jobs"]
    assert first["describe"]["state"] == "refused"
    assert first["policy"]["state"] == "refused"
    assert first["describe"]["exit_code"] == 1
    assert first["describe"]["stderr_tail"] == DENIED
    assert first["fields"] is None
    assert first["bindings"] is None
    assert first["match"] is False
    assert first["reasons"] == ["describe_refused", "policy_refused"]
    assert second["describe"]["state"] == "absent"
    assert second["fields"] is None
    assert second["reasons"] == ["describe_absent"]
    assert second["bindings"]["match"] is True
    assert report["summary"]["refused_reads"] == 2
    assert report["summary"]["absent"] == 1


def test_readback_command_writes_report_and_refuses_tampered_plan(tmp_path):
    plan, paths = _plan(tmp_path)
    runner = FakeRunner(_absent_script(plan))
    report_path = paths["output"].parent / "readback.json"
    out = io.StringIO()
    argv = ["readback", "--plan", str(paths["output"]), "--output", str(report_path)]
    code = runtime_jobs.main(argv + ["--gcloud", GCLOUD], runner=runner, out=out)
    assert code == 1
    report = json.loads(report_path.read_bytes())
    assert report["status"] == "mismatch"
    assert [job["describe"]["state"] for job in report["jobs"]] == ["absent", "absent"]
    assert report["summary"]["absent"] == 2
    assert json.loads(out.getvalue().splitlines()[-1])["status"] == "mismatch"

    tampered = deepcopy(plan)
    tampered["jobs"][0]["expected"]["max_retries"] = 3
    paths["output"].write_text(json.dumps(tampered), encoding="utf-8")
    report_path.unlink()
    out = io.StringIO()
    code = runtime_jobs.main(argv + ["--gcloud", GCLOUD], runner=runner, out=out)
    assert code == 1
    assert not report_path.exists()
    assert json.loads(out.getvalue().splitlines()[-1]) == {
        "error": "plan_mismatch",
        "status": "refused",
    }


def test_plan_command_writes_plan_and_sidecars(tmp_path):
    paths = _tree(tmp_path)
    out = io.StringIO()
    code = runtime_jobs.main(_plan_argv(paths), out=out)
    assert code == 0
    plan = json.loads(paths["output"].read_bytes())
    assert runtime_jobs.verify_plan(plan) == plan["plan_sha256"]
    names = sorted(path.name for path in paths["output"].parent.iterdir())
    assert names == [
        f"plan-{DAILY_JOB_ID}.job.yaml",
        f"plan-{PRICE_POLICY_JOB_ID}.job.yaml",
        "plan.json",
    ]
    summary = json.loads(out.getvalue().splitlines()[-1])
    assert summary["status"] == "planned"
    assert summary["plan_sha256"] == plan["plan_sha256"]
    assert summary["operations"] == 10


def test_plan_command_refusal_writes_nothing(tmp_path):
    paths = _tree(tmp_path, build=_build_record(status="WORKING"))
    out = io.StringIO()
    code = runtime_jobs.main(_plan_argv(paths), out=out)
    assert code == 1
    assert not paths["output"].parent.exists()
    assert json.loads(out.getvalue()) == {"error": "image_unbound", "status": "refused"}


STAMPED = {
    "applied": False,
    "approved_at": "2026-09-24T06:53:16+00:00",
    "approved_by": (
        "Albert Meintjes, in the session chat, document 36b26fca392ed3267e67419ed03adcbd242fa4714008bc85e96ea6284533031c; "
        "amendment b PROVISIONING_DELTA_AMENDMENT_B_APPROVAL_20260914.json, document ea2579481ab074ce175f14e34db9e799d60ffc546ed3a8bd675e6f79bcf194df; "
        "amendment c PROVISIONING_DELTA_AMENDMENT_C_APPROVAL_20260914.json, document 3dce68ab20ecb238e10e15ea3e80bc962edb038d0d1f222e7e4ef72a7989e6ca; "
        "amendment d PROVISIONING_DELTA_AMENDMENT_D_APPROVAL_20260920.json, document 35023bcfc88eae60fe042e053dcf558a4bc375d2de881f8ff8a5543f61e9d13f; "
        "amendment e in the session chat, delta 33ad00c0254fd0e514c56bf17b98571f95608ab30604a2523499a7ddf37bda9f "
        "at commit b24e27cb01a89ad2631af0f3a0e9b076006b4cb9"
    ),
    "state": "approved",
}


def test_tree_delta_is_stamped_so_the_acting_path_renders_the_tree(tmp_path):
    # The live delta carries amendment e's stamp, written after Albert's phrase
    # over the proposed delta at b24e27cb. Equality, not a state check, so the
    # acting path opens only under exactly the stamp Albert gave and no other block.
    tree_delta = json.loads(DELTA_FILE.read_bytes())
    assert tree_delta["approval"] == STAMPED
    (tmp_path / "build.json").write_text(json.dumps(_build_record()), encoding="utf-8")
    plan = runtime_jobs.render_plan(
        runtime_path=RUNTIME_FILE,
        image_digest=DIGEST,
        build_record_path=tmp_path / "build.json",
        output_path=tmp_path / "plan.json",
    )
    assert plan["inputs"]["iam_delta"]["approval"] == STAMPED
    assert (
        plan["inputs"]["iam_delta"]["sha256"]
        == hashlib.sha256(DELTA_FILE.read_bytes()).hexdigest()
    )
    assert len(plan["operations"]) == 10
    assert runtime_jobs.verify_plan(plan) == plan["plan_sha256"]


def test_tree_delta_under_an_approved_stamp_renders_the_pinned_plan(tmp_path):
    # The same bindings under a test local approved stamp still render the plan the
    # tree pins, so only the stamp holds the acting path back.
    paths = _tree(tmp_path)
    delta = json.loads(paths["delta"].read_bytes())
    assert delta["approval"] == APPROVED
    assert delta["bindings"] == json.loads(DELTA_FILE.read_bytes())["bindings"]
    plan = _render(paths)
    assert plan["resource_manifest_sha256"] == (
        "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
    )
    assert plan["inputs"]["iam_delta"]["approval"] == APPROVED
    assert len(plan["operations"]) == 10
    daily = [
        op
        for op in plan["operations"]
        if op.get("job_id") == DAILY_JOB_ID and op.get("role")
    ]
    assert [op["role"] for op in daily] == [
        "roles/run.developer",
        "roles/run.invoker",
        "roles/run.jobsExecutorWithOverrides",
        "roles/run.viewer",
    ]
    assert runtime_jobs.verify_plan(plan) == plan["plan_sha256"]


# replace: one existing job brought to the plan, nothing else changed.

OLD_DIGEST = "sha256:" + "1" * 64


def _live_price_job(plan, *, version="7", **drift):
    """The price job as it stands live: the old image, the pending vector."""
    job = _job(plan, PRICE_POLICY_JOB_ID)
    old = {
        "image": f"{REPOSITORY}@{OLD_DIGEST}",
        "args": [
            item.replace("#current", "#pending") for item in job["expected"]["args"]
        ],
        "annotations": {
            CONFIGURATION_ANNOTATION: "0" * 64,
            runtime_jobs.MANIFEST_ANNOTATION: job["expected"]["annotations"][
                runtime_jobs.MANIFEST_ANNOTATION
            ],
        },
    }
    live = _v1_job(job, **{**old, **drift})
    live["metadata"].update(
        {
            "namespace": "123456789",
            "resourceVersion": version,
            "labels": {"cloud.googleapis.com/location": "us-central1"},
            "annotations": {
                "run.googleapis.com/creator": "owner@example.com",
                "run.googleapis.com/launch-stage": "GA",
            },
        }
    )
    task = live["spec"]["template"]["spec"]["template"]["spec"]
    task["containers"][0]["resources"] = {"limits": {"cpu": "1", "memory": "512Mi"}}
    live["status"] = {"observedGeneration": 3}
    return live


class _Replacing:
    """describe answers the live job; replace stores what was sent."""

    def __init__(self, plan, live, *, replace_result=None, moved=None):
        self.plan, self.live = plan, live
        self.replace_result, self.moved = replace_result, moved
        self.calls, self.sent = [], None

    def __call__(self, argv, cwd):
        verb = argv[3]
        self.calls.append(verb)
        if verb == "describe":
            describes = self.calls.count("describe")
            if self.moved is not None and describes == 2:
                return 0, json.dumps(self.moved), ""
            if self.sent is not None:
                return 0, json.dumps(self.sent), ""
            return 0, json.dumps(self.live), ""
        if verb == "replace":
            if self.replace_result is not None:
                return self.replace_result
            self.sent = json.loads((Path(cwd) / argv[4]).read_bytes())
            return 0, json.dumps(self.sent), ""
        raise AssertionError(argv)


def _replace_argv(paths, receipt, extra=()):
    return [
        "replace",
        "--plan",
        str(paths["output"]),
        "--job",
        PRICE_POLICY_JOB_ID,
        "--output",
        str(receipt),
        "--gcloud",
        GCLOUD,
        "--resource-manifest",
        str(paths["manifest"]),
        *extra,
    ]


def _replace(paths, runner, receipt, *extra):
    out = io.StringIO()
    code = runtime_jobs.main(
        _replace_argv(paths, receipt, extra), runner=runner, out=out
    )
    lines = out.getvalue().splitlines()
    return code, json.loads(lines[-1]), out.getvalue()


def test_replace_changes_only_image_args_env_and_annotations(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)
    runner = _Replacing(plan, live)
    receipt = tmp_path / "replace.json"
    code, result, _ = _replace(paths, runner, receipt)
    assert (code, result["status"]) == (0, "replaced"), result
    assert runner.calls == ["describe", "describe", "replace", "describe"]
    assert sorted(result["changed_fields"]) == ["annotations", "args", "image"]
    sent = runner.sent
    expected = _job(plan, PRICE_POLICY_JOB_ID)["expected"]
    # The live job, byte for byte, but for the four fields and the output only parts.
    carried = json.loads(json.dumps(live))
    del carried["status"]
    del carried["metadata"]["annotations"]["run.googleapis.com/creator"]
    container = carried["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    container.update(
        image=expected["image"], args=expected["args"], env=expected["env"]
    )
    carried["spec"]["template"]["metadata"]["annotations"].update(
        expected["annotations"]
    )
    assert sent == carried
    assert sent["metadata"]["resourceVersion"] == "7"
    assert container["resources"] == {"limits": {"cpu": "1", "memory": "512Mi"}}
    written = json.loads(receipt.read_bytes())
    assert written["status"] == "replaced"
    assert all(field["match"] for field in written["after"]["fields"].values())
    assert written["before"]["resource_version"] == "7"
    assert (tmp_path / "replace.job.yaml").exists()


@pytest.mark.parametrize(
    "drift, field",
    [
        (
            {"service_account": "someone@ogilvy-trends-v2.iam.gserviceaccount.com"},
            "service_account",
        ),
        ({"command": ["sh"]}, "command"),
        ({"timeout_seconds": 60}, "timeout_seconds"),
        ({"max_retries": 3}, "max_retries"),
        ({"task_count": 2}, "task_count"),
        ({"parallelism": 2}, "parallelism"),
    ],
)
def test_replace_refuses_a_live_job_that_differs_in_anything_else(
    tmp_path, drift, field
):
    plan, paths = _plan(tmp_path)
    runner = _Replacing(plan, _live_price_job(plan, **drift))
    receipt = tmp_path / "replace.json"
    code, result, _ = _replace(paths, runner, receipt)
    assert (code, result["error"]) == (1, "replace_field_not_allowed"), result
    assert runner.calls == ["describe"]
    assert json.loads(receipt.read_bytes())["fields_not_allowed"] == [field]


def test_replace_refuses_a_live_secret_it_would_drop(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)
    live["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "KEY", "valueFrom": {"secretKeyRef": {"name": "k", "key": "latest"}}}
    ]
    runner = _Replacing(plan, live)
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "job_env_not_plain"), result
    assert "replace" not in runner.calls


def test_replace_refuses_when_the_job_moved_after_it_was_read(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)
    runner = _Replacing(plan, live, moved=_live_price_job(plan, version="8"))
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "job_changed_since_read"), result
    assert runner.calls == ["describe", "describe"]


def test_a_stale_version_the_server_refuses_is_reported_and_not_retried(tmp_path):
    plan, paths = _plan(tmp_path)
    stale = (
        1,
        "",
        "ERROR: (gcloud.run.jobs.replace) Conflict for resource: the object has "
        "been modified; resourceVersion 7 is stale (409)",
    )
    runner = _Replacing(plan, _live_price_job(plan), replace_result=stale)
    receipt = tmp_path / "replace.json"
    code, result, _ = _replace(paths, runner, receipt)
    assert (code, result["status"], result["error"]) == (
        1,
        "failed",
        "job_changed_since_read",
    ), result
    assert runner.calls.count("replace") == 1
    assert runner.calls[-1] == "replace"
    assert json.loads(receipt.read_bytes())["error"] == "job_changed_since_read"


def test_replace_never_creates_a_missing_job(tmp_path):
    plan, paths = _plan(tmp_path)
    calls = []

    def runner(argv, cwd):
        calls.append(argv[3])
        return 1, "", _not_found(PRICE_POLICY_JOB_ID)

    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "job_absent"), result
    assert calls == ["describe"]


def test_replace_dry_run_prints_the_field_diff_and_sends_nothing(tmp_path):
    plan, paths = _plan(tmp_path)
    runner = _Replacing(plan, _live_price_job(plan))
    receipt = tmp_path / "replace.json"
    code, result, printed = _replace(paths, runner, receipt, "--dry-run")
    assert (code, result["status"]) == (0, "dry_run"), result
    assert runner.calls == ["describe"]
    changed = json.loads(printed.splitlines()[0])["changed"]
    assert sorted(changed) == ["annotations", "args", "image"]
    expected = _job(plan, PRICE_POLICY_JOB_ID)["expected"]
    assert changed["args"]["expected"] == expected["args"]
    assert changed["args"]["observed"][-1] == "/tmp/renewal-receipt.json"
    assert any(item.endswith("#pending") for item in changed["args"]["observed"])
    assert not receipt.exists()
    assert not (tmp_path / "replace.job.yaml").exists()


def test_a_job_already_matching_the_plan_is_left_alone(tmp_path):
    plan, paths = _plan(tmp_path)
    job = _job(plan, PRICE_POLICY_JOB_ID)
    live = _live_price_job(plan, **job["expected"])
    runner = _Replacing(plan, live)
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["status"]) == (0, "unchanged"), result
    assert runner.calls == ["describe"]


def test_a_readback_that_does_not_match_the_plan_is_unverified(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)

    class Ignoring(_Replacing):
        def __call__(self, argv, cwd):
            result = super().__call__(argv, cwd)
            if argv[3] == "replace":
                self.sent = self.live
            return result

    code, result, _ = _replace(paths, Ignoring(plan, live), tmp_path / "replace.json")
    assert (code, result["status"], result["error"]) == (
        1,
        "replace_unverified",
        "readback_mismatch",
    ), result


def test_replace_refuses_an_existing_output_before_any_call(tmp_path):
    plan, paths = _plan(tmp_path)
    receipt = tmp_path / "replace.json"
    receipt.write_text("{}", encoding="utf-8")
    runner = _Replacing(plan, _live_price_job(plan))
    code, result, _ = _replace(paths, runner, receipt)
    assert (code, result["error"]) == (1, "output_exists"), result
    assert runner.calls == []


def test_replace_refuses_a_live_job_without_a_version(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)
    del live["metadata"]["resourceVersion"]
    runner = _Replacing(plan, live)
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "job_version_unavailable"), result
    assert "replace" not in runner.calls


def test_replace_refuses_a_tampered_plan_before_any_call(tmp_path):
    plan, paths = _plan(tmp_path)
    tampered = json.loads(paths["output"].read_bytes())
    _job(tampered, PRICE_POLICY_JOB_ID)["expected"]["service_account"] = "x@y"
    paths["output"].write_text(json.dumps(tampered), encoding="utf-8")
    runner = _Replacing(plan, _live_price_job(plan))
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "plan_mismatch"), result
    assert runner.calls == []


def test_a_readback_that_lost_a_carried_field_is_unverified(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)

    class Dropping(_Replacing):
        def __call__(self, argv, cwd):
            result = super().__call__(argv, cwd)
            if argv[3] == "replace":
                task = self.sent["spec"]["template"]["spec"]["template"]["spec"]
                del task["containers"][0]["resources"]
            return result

    code, result, _ = _replace(paths, Dropping(plan, live), tmp_path / "replace.json")
    assert (code, result["status"], result["error"]) == (
        1,
        "replace_unverified",
        "carried_fields_changed",
    ), result


def test_a_replace_that_timed_out_is_read_back(tmp_path):
    plan, paths = _plan(tmp_path)
    timeout = (-1, "", "runner timeout after 900s")
    runner = _Replacing(plan, _live_price_job(plan), replace_result=timeout)
    receipt = tmp_path / "replace.json"
    code, result, _ = _replace(paths, runner, receipt)
    assert (code, result["error"]) == (1, "replace_failed"), result
    assert runner.calls == ["describe", "describe", "replace", "describe"]
    assert "after" in json.loads(receipt.read_bytes())


def test_an_invalid_spec_is_not_reported_as_a_conflict(tmp_path):
    plan, paths = _plan(tmp_path)
    invalid = (
        1,
        "",
        "ERROR: (gcloud.run.jobs.replace) INVALID_ARGUMENT: Conflicting fields "
        "in metadata.resourceVersion; precondition not met",
    )
    runner = _Replacing(plan, _live_price_job(plan), replace_result=invalid)
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "replace_failed"), result


def test_execution_tokens_are_not_sent_back(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)
    live["spec"]["runExecutionToken"] = "token-1"
    runner = _Replacing(plan, live)
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["status"]) == (0, "replaced"), result
    assert "runExecutionToken" not in runner.sent["spec"]


def test_a_newer_gcloud_stamp_on_the_template_is_not_a_carried_change(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)

    class Stamping(_Replacing):
        def __call__(self, argv, cwd):
            result = super().__call__(argv, cwd)
            if argv[3] == "replace":
                self.sent["spec"]["template"]["metadata"]["annotations"][
                    "run.googleapis.com/client-version"
                ] = "999.0.0"
            return result

    code, result, _ = _replace(paths, Stamping(plan, live), tmp_path / "replace.json")
    assert (code, result["status"]) == (0, "replaced"), result


def test_a_dropped_connection_is_read_back_not_called_a_conflict(tmp_path):
    plan, paths = _plan(tmp_path)
    dropped = (
        1,
        "",
        "ERROR: gcloud crashed (ConnectionError): ('Connection aborted.', "
        "RemoteDisconnected('Remote end closed connection without response'))",
    )
    runner = _Replacing(plan, _live_price_job(plan), replace_result=dropped)
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "replace_failed"), result
    assert runner.calls[-1] == "describe"


def test_server_stamped_labels_and_annotations_are_not_carried_changes(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)
    live["metadata"]["labels"]["run.googleapis.com/lastUpdatedTime"] = "2026-09-20"
    del live["spec"]["template"]["metadata"]

    class Stamping(_Replacing):
        def __call__(self, argv, cwd):
            result = super().__call__(argv, cwd)
            if argv[3] == "replace":
                self.sent["metadata"]["labels"][
                    "run.googleapis.com/lastUpdatedTime"
                ] = "2026-09-25"
                self.sent["metadata"]["annotations"]["run.googleapis.com/creator"] = "x"
            return result

    code, result, _ = _replace(paths, Stamping(plan, live), tmp_path / "replace.json")
    assert (code, result["status"]) == (0, "replaced"), result


def test_a_dropped_job_annotation_is_a_carried_change(tmp_path):
    plan, paths = _plan(tmp_path)
    live = _live_price_job(plan)

    class Dropping(_Replacing):
        def __call__(self, argv, cwd):
            result = super().__call__(argv, cwd)
            if argv[3] == "replace":
                del self.sent["metadata"]["annotations"][
                    "run.googleapis.com/launch-stage"
                ]
            return result

    code, result, _ = _replace(paths, Dropping(plan, live), tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "carried_fields_changed"), result


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: (gcloud.run.jobs.replace) HTTPError 409: Conflict for resource "
        "'intelligence-42-price-policy-staging': version '7' was specified but "
        "current version is '8'.",
        "ERROR: (gcloud.run.jobs.replace) Conflict for resource "
        "'intelligence-42-price-policy-staging'",
    ],
)
def test_gcloud_version_conflicts_are_reported_as_a_moved_job(tmp_path, message):
    plan, paths = _plan(tmp_path)
    runner = _Replacing(plan, _live_price_job(plan), replace_result=(1, "", message))
    code, result, _ = _replace(paths, runner, tmp_path / "replace.json")
    assert (code, result["error"]) == (1, "job_changed_since_read"), result
    assert runner.calls[-1] == "replace"
