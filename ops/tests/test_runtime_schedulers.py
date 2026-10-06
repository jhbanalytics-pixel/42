"""R05 steps 5 and 7: the two Cloud Scheduler jobs are planned, created and read
back paused, over the injectable REST transport. The plan binds the reviewed
digest chain, the approved scheduler identity, the twelve hourly price policy
cadence and the approved run.invoker rows; apply creates then pauses and refuses
anything it cannot confirm paused; readback diffs the live job against the plan.
"""

import base64
import copy
import hashlib
import json
import socketserver
import threading
from pathlib import Path

import pytest

from ops.deploy import runtime_schedulers as schedulers
from ops.runners.managed_runtime import (
    DAILY_JOB_ID,
    PRICE_POLICY_JOB_ID,
    load_runtime_configuration,
    load_scheduler_configuration,
)

ROOT = Path(schedulers.__file__).resolve().parents[2]
RUNTIME_FILE = ROOT / "infra" / "runtime" / "daily-staging.json"
SCHEDULER_FILE = ROOT / "infra" / "runtime" / "scheduler-staging.json"
MANIFEST_FILE = ROOT / "ops" / "deploy" / "resource_manifest.json"
TREE_DELTA_FILE = ROOT / "ops" / "deploy" / "iam_delta_v1.json"
# These tests plan over the tree bindings under a test local stamp, so they hold
# the bindings whatever the tree's stamp; the tree file itself, under amendment e's
# stamp, is shown rendering below.
DELTA_FILE = TREE_DELTA_FILE
TEST_STAMP = {
    "applied": False,
    "approved_at": "2026-09-13T08:00:00Z",
    "approved_by": "reviewer",
    "state": "approved",
}
SCHEDULER_ACCOUNT = "intelligence-42-scheduler@ogilvy-trends-v2.iam.gserviceaccount.com"
PARENT = "projects/ogilvy-trends-v2/locations/us-central1"


@pytest.fixture(scope="session")
def stamped_tree_delta(tmp_path_factory):
    value = json.loads(TREE_DELTA_FILE.read_bytes())
    value["approval"] = dict(TEST_STAMP)
    path = tmp_path_factory.mktemp("stamped-delta") / "iam_delta_v1.json"
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture(autouse=True)
def _tree_bindings_under_a_test_stamp(monkeypatch, stamped_tree_delta):
    monkeypatch.setitem(globals(), "DELTA_FILE", stamped_tree_delta)
    monkeypatch.setitem(
        schedulers.render_plan.__kwdefaults__, "delta_path", stamped_tree_delta
    )


def test_the_tree_delta_is_stamped_so_the_scheduler_plan_renders_it():
    tree = json.loads(TREE_DELTA_FILE.read_bytes())
    assert tree["approval"] == {
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
    plan = _plan(delta_path=TREE_DELTA_FILE)
    assert plan["iam_delta"] == {
        "approved_at": tree["approval"]["approved_at"],
        "approved_by": tree["approval"]["approved_by"],
        "sha256": hashlib.sha256(TREE_DELTA_FILE.read_bytes()).hexdigest(),
        "state": "approved",
    }
    assert schedulers.verify_plan(plan) == plan["plan_sha256"]


def test_the_test_stamp_changes_nothing_but_the_approval(stamped_tree_delta):
    tree = json.loads(TREE_DELTA_FILE.read_bytes())
    stamped = json.loads(stamped_tree_delta.read_bytes())
    assert stamped["approval"] == TEST_STAMP
    assert {**stamped, "approval": tree["approval"]} == tree


def _files(**overrides):
    paths = {
        "runtime_path": RUNTIME_FILE,
        "scheduler_path": SCHEDULER_FILE,
        "manifest_path": MANIFEST_FILE,
        "delta_path": DELTA_FILE,
    }
    paths.update(overrides)
    return paths


def _plan(**overrides):
    return schedulers.render_plan(
        **_files(**overrides), now=lambda: "2026-09-18T00:00:00+00:00"
    )


def _rewrite(tmp_path, path, mutate):
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    target = tmp_path / path.name
    target.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    return target


def _rebound(tmp_path, mutate):
    """A scheduler file edited for one case, with the runtime file re-bound to it."""
    scheduler_path = _rewrite(tmp_path, SCHEDULER_FILE, mutate)
    digest = schedulers.file_sha256(scheduler_path)
    runtime_path = _rewrite(
        tmp_path,
        RUNTIME_FILE,
        lambda value: value.__setitem__("scheduler_configuration_sha256", digest),
    )
    return {"runtime_path": runtime_path, "scheduler_path": scheduler_path}


class FakeCloud:
    """The scheduler surface: jobs that exist, what was created and what was paused."""

    def __init__(self, existing=(), *, fail=None, paused_state="PAUSED"):
        self.jobs = {name: dict(job) for name, job in dict(existing).items()}
        self.calls = []
        self.fail = fail or {}
        self.paused_state = paused_state

    def __call__(self, request):
        url, method = request["url"], request["method"]
        body = json.loads(request["body"].decode("utf-8")) if request["body"] else None
        call = self._classify(method, url)
        self.calls.append(call)
        if call in self.fail:
            return self._json(*self.fail[call])
        if call == "get":
            name = url.split("/v1/", 1)[1]
            job = self.jobs.get(name)
            return self._json(404 if job is None else 200, job or {"error": "absent"})
        if call == "create":
            self.jobs[body["name"]] = {**body, "state": "ENABLED"}
            return self._json(200, self.jobs[body["name"]])
        name = url.split("/v1/", 1)[1].rsplit(":pause", 1)[0]
        self.jobs[name] = {**self.jobs[name], "state": self.paused_state}
        return self._json(200, self.jobs[name])

    def _classify(self, method, url):
        if method == "GET":
            return "get"
        return "pause" if url.endswith(":pause") else "create"

    @staticmethod
    def _json(status, value):
        return {
            "status": status,
            "headers": {},
            "body": json.dumps(value).encode("utf-8"),
        }


def _adapter(cloud):
    from ops.deploy import runtime_native_adapter as native

    return native.NativeAdapter(
        "adc", transport=cloud, token_source=lambda: "fake-token"
    )


# Plan


def test_plan_binds_the_reviewed_digest_chain_of_the_three_files():
    plan = _plan()
    config = load_runtime_configuration(RUNTIME_FILE)
    assert plan["contract_version"] == schedulers.PLAN_CONTRACT
    assert (
        plan["scheduler_configuration_sha256"]
        == (config["scheduler_configuration_sha256"])
    )
    assert plan["resource_manifest_sha256"] == config["resource_manifest_sha256"]
    assert plan["runtime_configuration_sha256"] == schedulers.file_sha256(RUNTIME_FILE)
    assert plan["plan_sha256"] == schedulers.verify_plan(plan)


def test_plan_covers_both_approved_schedulers_and_nothing_else():
    plan = _plan()
    assert set(plan["schedulers"]) == {DAILY_JOB_ID, PRICE_POLICY_JOB_ID}
    assert [operation["kind"] for operation in plan["operations"]] == [
        "describe",
        "create",
        "pause",
        "confirm",
    ] * 2


def test_the_create_body_carries_the_empty_run_request_and_no_output_only_state():
    plan = _plan()
    create = next(
        operation
        for operation in plan["operations"]
        if operation["kind"] == "create" and operation["scheduler"] == DAILY_JOB_ID
    )
    job = create["request"]["body"]
    assert "state" not in job
    assert job["httpTarget"]["body"] == "e30="
    assert job["httpTarget"]["headers"] == {"Content-Type": "application/json"}
    assert job["httpTarget"]["oauthToken"]["serviceAccountEmail"] == SCHEDULER_ACCOUNT
    assert create["request"]["url"].endswith(PARENT + "/jobs")


def test_the_plan_expects_every_scheduler_paused():
    plan = _plan()
    assert [entry["expected_state"] for entry in plan["schedulers"].values()] == [
        "PAUSED",
        "PAUSED",
    ]


def test_the_price_policy_scheduler_keeps_its_approved_twelve_hourly_cadence():
    plan = _plan()
    assert plan["schedulers"][PRICE_POLICY_JOB_ID]["job"]["schedule"] == "0 */12 * * *"
    assert plan["schedulers"][PRICE_POLICY_JOB_ID]["job"]["timeZone"] == "Etc/UTC"


def test_a_changed_price_policy_cadence_refuses(tmp_path):
    def mutate(value):
        value["schedulers"][PRICE_POLICY_JOB_ID]["job"]["schedule"] = "0 */6 * * *"

    with pytest.raises(ValueError, match="^price_policy_schedule_unapproved$"):
        _plan(**_rebound(tmp_path, mutate))


def test_a_scheduler_identity_outside_the_manifest_refuses(tmp_path):
    def mutate(value):
        target = value["schedulers"][DAILY_JOB_ID]["job"]["httpTarget"]
        target["oauthToken"]["serviceAccountEmail"] = (
            "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
        )

    with pytest.raises(ValueError, match="^scheduler_identity_mismatch$"):
        _plan(**_rebound(tmp_path, mutate))


def _manifest_without(tmp_path, prefix, action):
    """The manifest with one action struck from one resource row, and nothing else."""

    def mutate(value):
        for row in value["resources"]:
            if row["name"].startswith(prefix):
                row["actions"] = [name for name in row["actions"] if name != action]

    manifest_path = _rewrite(tmp_path, MANIFEST_FILE, mutate)
    digest = schedulers.file_sha256(manifest_path)
    runtime_path = _rewrite(
        tmp_path,
        RUNTIME_FILE,
        lambda value: value.__setitem__("resource_manifest_sha256", digest),
    )
    return {"manifest_path": manifest_path, "runtime_path": runtime_path}


SCHEDULER_PREFIX = (
    "//cloudscheduler.googleapis.com/projects/ogilvy-trends-v2/"
    "locations/us-central1/jobs/intelligence-42-daily-staging"
)
RUN_JOB_PREFIX = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/"
    "locations/us-central1/jobs/intelligence-42-daily-staging"
)


def test_a_scheduler_the_manifest_does_not_allow_deploying_refuses(tmp_path):
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        _plan(**_manifest_without(tmp_path, SCHEDULER_PREFIX, "deploy"))


def test_a_scheduler_the_manifest_does_not_allow_reading_refuses(tmp_path):
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        _plan(**_manifest_without(tmp_path, SCHEDULER_PREFIX, "read"))


def test_a_target_job_the_manifest_does_not_allow_invoking_refuses(tmp_path):
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        _plan(**_manifest_without(tmp_path, RUN_JOB_PREFIX, "invoke"))


def test_an_unapproved_iam_delta_refuses(tmp_path):
    def mutate(value):
        value["approval"]["state"] = "proposed"

    delta_path = _rewrite(tmp_path, DELTA_FILE, mutate)
    with pytest.raises(ValueError, match="^iam_delta_unapproved$"):
        _plan(delta_path=delta_path)


def test_a_missing_invoker_row_for_a_target_job_refuses(tmp_path):
    def mutate(value):
        value["bindings"] = [
            row
            for row in value["bindings"]
            if not (
                row["role"] == "roles/run.invoker"
                and row["member"] == "serviceAccount:" + SCHEDULER_ACCOUNT
                and row["resource"].endswith("/jobs/" + PRICE_POLICY_JOB_ID)
            )
        ]

    delta_path = _rewrite(tmp_path, DELTA_FILE, mutate)
    with pytest.raises(ValueError, match="^invoker_binding_missing$"):
        _plan(delta_path=delta_path)


def test_the_plan_records_the_invoker_rows_as_the_permission_proof_obligation():
    plan = _plan()
    rows = plan["schedulers"][DAILY_JOB_ID]["invoker_bindings"]
    assert rows == [
        {"member": "serviceAccount:" + SCHEDULER_ACCOUNT, "role": "roles/run.invoker"}
    ]


def test_a_scheduler_file_that_left_its_bound_digest_refuses(tmp_path):
    scheduler_path = _rewrite(
        tmp_path,
        SCHEDULER_FILE,
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"].__setitem__(
            "description", "changed"
        ),
    )
    with pytest.raises(ValueError, match="^scheduler_configuration_digest_mismatch$"):
        _plan(scheduler_path=scheduler_path)


# Apply


def test_apply_creates_then_pauses_each_scheduler_and_confirms_it_paused():
    plan = _plan()
    cloud = FakeCloud()
    receipt = schedulers.apply_plan(plan, adapter=_adapter(cloud))
    assert cloud.calls == ["get", "create", "pause", "get"] * 2
    assert receipt["state"] == "applied"
    assert [entry["state"] for entry in receipt["schedulers"].values()] == [
        "PAUSED",
        "PAUSED",
    ]
    assert all(job["state"] == "PAUSED" for job in cloud.jobs.values()), (
        "every created scheduler is left paused"
    )


def test_apply_refuses_a_plan_whose_digest_moved():
    plan = _plan()
    moved = copy.deepcopy(plan)
    moved["schedulers"][DAILY_JOB_ID]["job"]["schedule"] = "*/5 * * * *"
    with pytest.raises(ValueError, match="^plan_digest_mismatch$"):
        schedulers.apply_plan(moved, adapter=_adapter(FakeCloud()))


def test_an_existing_scheduler_is_recorded_and_never_created_again():
    plan = _plan()
    name = plan["schedulers"][DAILY_JOB_ID]["name"]
    cloud = FakeCloud({name: {"name": name, "state": "ENABLED"}})
    receipt = schedulers.apply_plan(plan, adapter=_adapter(cloud))
    assert receipt["schedulers"][DAILY_JOB_ID]["result"] == "paused"
    assert receipt["schedulers"][DAILY_JOB_ID]["state"] == "PAUSED"
    assert cloud.calls == ["get", "pause", "get", "get", "create", "pause", "get"]
    assert cloud.jobs[name]["state"] == "PAUSED", "an existing job is left paused"


def test_an_existing_paused_scheduler_is_recorded_and_never_touched():
    plan = _plan()
    name = plan["schedulers"][DAILY_JOB_ID]["name"]
    cloud = FakeCloud({name: {"name": name, "state": "PAUSED"}})
    receipt = schedulers.apply_plan(plan, adapter=_adapter(cloud))
    assert receipt["schedulers"][DAILY_JOB_ID]["result"] == "already_exists"
    assert receipt["schedulers"][DAILY_JOB_ID]["state"] == "PAUSED"
    assert cloud.calls == ["get", "get", "create", "pause", "get"]


def test_a_scheduler_that_does_not_read_back_paused_refuses():
    plan = _plan()
    cloud = FakeCloud(paused_state="ENABLED")
    with pytest.raises(ValueError, match="^scheduler_not_paused$"):
        schedulers.apply_plan(plan, adapter=_adapter(cloud))


def test_apply_stops_at_the_first_failed_operation():
    plan = _plan()
    cloud = FakeCloud(fail={"create": (403, {"error": {"message": "denied"}})})
    with pytest.raises(schedulers.NativeError) as raised:
        schedulers.apply_plan(plan, adapter=_adapter(cloud))
    assert raised.value.status == 403
    assert cloud.calls == ["get", "create"]


def test_the_dry_run_renders_every_request_and_sends_none():
    plan = _plan()
    cloud = FakeCloud()
    rendered = schedulers.apply_plan(plan, adapter=_adapter(cloud), dry_run=True)
    assert cloud.calls == []
    assert rendered["state"] == "rendered"
    assert [entry["call"] for entry in rendered["requests"]] == [
        "get_scheduler",
        "create_scheduler",
        "pause_scheduler",
        "get_scheduler",
    ] * 2


def test_no_planned_or_applied_request_can_resume_or_run_a_scheduler():
    plan = _plan()
    rendered = schedulers.apply_plan(plan, adapter=_adapter(FakeCloud()), dry_run=True)
    calls = {entry["call"] for entry in rendered["requests"]}
    assert calls == {"get_scheduler", "create_scheduler", "pause_scheduler"}
    urls = [operation["request"]["url"] for operation in plan["operations"]]
    assert not any(url.endswith((":resume", ":run")) for url in urls)
    assert ":resume" not in json.dumps([plan, rendered])


# Readback


def _applied(cloud=None):
    plan = _plan()
    cloud = cloud or FakeCloud()
    schedulers.apply_plan(plan, adapter=_adapter(cloud))
    cloud.calls.clear()
    return plan, cloud


def test_readback_reports_every_scheduler_paused_and_matching():
    plan, cloud = _applied()
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "matched"
    assert report["schedulers"][DAILY_JOB_ID]["differences"] == {}
    assert report["schedulers"][PRICE_POLICY_JOB_ID]["state"] == "PAUSED"


def test_readback_names_a_field_that_drifted_from_the_plan():
    plan, cloud = _applied()
    name = plan["schedulers"][DAILY_JOB_ID]["name"]
    cloud.jobs[name]["schedule"] = "*/5 * * * *"
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "drifted"
    difference = report["schedulers"][DAILY_JOB_ID]["differences"]["schedule"]
    assert difference == {"expected": "0 3 * * *", "observed": "*/5 * * * *"}


def test_readback_reports_a_running_schedule_as_drift_rather_than_a_match():
    plan, cloud = _applied()
    name = plan["schedulers"][PRICE_POLICY_JOB_ID]["name"]
    cloud.jobs[name]["state"] = "ENABLED"
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "drifted"
    assert report["schedulers"][PRICE_POLICY_JOB_ID]["differences"]["state"] == {
        "expected": "PAUSED",
        "observed": "ENABLED",
    }


def test_readback_reports_an_absent_scheduler_rather_than_creating_it():
    plan = _plan()
    cloud = FakeCloud()
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "drifted"
    assert report["schedulers"][DAILY_JOB_ID]["result"] == "absent"
    assert cloud.calls == ["get", "get"]


def test_readback_records_a_refused_read_without_claiming_a_match():
    plan, cloud = _applied()
    cloud.fail = {"get": (403, {"error": {"message": "permission denied"}})}
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "drifted"
    assert report["schedulers"][DAILY_JOB_ID]["result"] == "refused"


# CLI


def test_the_cli_writes_a_plan_and_refuses_to_overwrite_it(tmp_path):
    output = tmp_path / "scheduler-plan.json"
    assert schedulers.main(["plan", "--output", str(output)]) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["plan_sha256"] == schedulers.verify_plan(plan)
    assert schedulers.main(["plan", "--output", str(output)]) == 1


def test_the_cli_exposes_no_command_that_activates_a_schedule():
    parser = schedulers._parser()
    actions = [
        action
        for action in parser._subparsers._actions
        if hasattr(action, "choices") and action.choices
    ]
    assert set(actions[0].choices) == {"plan", "apply", "readback"}


# A plan document is never trusted for what it puts on the wire


def _resealed(plan, mutate):
    """A plan edited by hand and resealed with the module's own digest function."""
    moved = copy.deepcopy(plan)
    mutate(moved)
    moved["plan_sha256"] = schedulers.verify_plan(moved)
    return moved


VICTIM = "projects/other-project/locations/us-central1/jobs/victim:resume#"


def test_a_resealed_plan_whose_scheduler_name_left_this_project_sends_nothing():
    plan = _plan()
    moved = _resealed(
        plan,
        lambda value: value["schedulers"][DAILY_JOB_ID].__setitem__("name", VICTIM),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_name_unpinned$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_resealed_plan_whose_create_body_names_another_project_sends_nothing():
    plan = _plan()
    moved = _resealed(
        plan,
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"].__setitem__(
            "name", VICTIM
        ),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_name_unpinned$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_resealed_plan_whose_scheduler_target_moved_sends_nothing():
    plan = _plan()
    moved = _resealed(
        plan,
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"][
            "httpTarget"
        ].__setitem__(
            "uri", "https://run.googleapis.com/v2/projects/other/jobs/victim:run"
        ),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_target_unpinned$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_resealed_plan_whose_oauth_identity_moved_sends_nothing():
    plan = _plan()
    moved = _resealed(
        plan,
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"]["httpTarget"][
            "oauthToken"
        ].__setitem__("serviceAccountEmail", "attacker@example.com"),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_identity_mismatch$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_resealed_plan_whose_declared_resource_moved_sends_nothing():
    plan = _plan()
    moved = _resealed(
        plan,
        lambda value: value["schedulers"][DAILY_JOB_ID].__setitem__(
            "run_job_resource", "//run.googleapis.com/projects/other/jobs/victim"
        ),
    )
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.apply_plan(moved, adapter=_adapter(FakeCloud()))


# The digest chain and the resource guard are re-checked wherever a request goes


def test_apply_refuses_a_resealed_plan_whose_manifest_digest_moved():
    plan = _plan()
    moved = _resealed(
        plan, lambda value: value.__setitem__("resource_manifest_sha256", "b" * 64)
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^resource_manifest_digest_mismatch$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_apply_refuses_a_resealed_plan_whose_scheduler_file_digest_moved():
    plan = _plan()
    moved = _resealed(
        plan,
        lambda value: value.__setitem__("scheduler_configuration_sha256", "c" * 64),
    )
    with pytest.raises(ValueError, match="^scheduler_configuration_digest_mismatch$"):
        schedulers.apply_plan(moved, adapter=_adapter(FakeCloud()))


def test_apply_refuses_a_resealed_plan_whose_runtime_file_digest_moved():
    plan = _plan()
    moved = _resealed(
        plan,
        lambda value: value.__setitem__("runtime_configuration_sha256", "d" * 64),
    )
    with pytest.raises(ValueError, match="^runtime_configuration_digest_mismatch$"):
        schedulers.apply_plan(moved, adapter=_adapter(FakeCloud()))


def test_readback_refuses_a_resealed_plan_whose_manifest_digest_moved():
    plan = _plan()
    moved = _resealed(
        plan, lambda value: value.__setitem__("resource_manifest_sha256", "b" * 64)
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^resource_manifest_digest_mismatch$"):
        schedulers.read_back(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def _rebound_to(tmp_path, prefix, action):
    """The manifest with one action struck, and a plan resealed onto those files."""
    paths = _manifest_without(tmp_path, prefix, action)
    plan = _resealed(
        _plan(),
        lambda value: value.update(
            {
                "runtime_configuration_sha256": schedulers.file_sha256(
                    paths["runtime_path"]
                ),
                "resource_manifest_sha256": schedulers.file_sha256(
                    paths["manifest_path"]
                ),
            }
        ),
    )
    return plan, paths


def test_apply_runs_the_resource_guard_before_it_sends_anything(tmp_path):
    plan, paths = _rebound_to(tmp_path, SCHEDULER_PREFIX, "deploy")
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        schedulers.apply_plan(
            plan,
            adapter=_adapter(cloud),
            runtime_path=paths["runtime_path"],
            manifest_path=paths["manifest_path"],
        )
    assert cloud.calls == []


def test_readback_runs_the_resource_guard_before_it_reads(tmp_path):
    plan, paths = _rebound_to(tmp_path, SCHEDULER_PREFIX, "read")
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        schedulers.read_back(
            plan,
            adapter=_adapter(cloud),
            runtime_path=paths["runtime_path"],
            manifest_path=paths["manifest_path"],
        )
    assert cloud.calls == []


def test_apply_runs_the_invoke_guard_on_every_target_job(tmp_path):
    plan, paths = _rebound_to(tmp_path, RUN_JOB_PREFIX, "invoke")
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        schedulers.apply_plan(
            plan,
            adapter=_adapter(FakeCloud()),
            runtime_path=paths["runtime_path"],
            manifest_path=paths["manifest_path"],
        )


# A malformed plan refuses by name rather than raising


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        (
            "entry_is_a_string",
            lambda value: value["schedulers"].__setitem__(DAILY_JOB_ID, "enabled"),
        ),
        (
            "entry_without_a_name",
            lambda value: value["schedulers"][DAILY_JOB_ID].pop("name"),
        ),
        ("operations_is_not_a_list", lambda value: value.__setitem__("operations", {})),
        (
            "operation_is_a_string",
            lambda value: value["operations"].__setitem__(0, "describe"),
        ),
        (
            "another_contract_version",
            lambda value: value.__setitem__("contract_version", "42_other_plan_v1"),
        ),
        (
            "another_project",
            lambda value: value.__setitem__("project", "other-project"),
        ),
        (
            "another_parent",
            lambda value: value.__setitem__(
                "parent", "projects/other-project/locations/us-central1"
            ),
        ),
        (
            "a_scheduler_dropped",
            lambda value: value["schedulers"].pop(PRICE_POLICY_JOB_ID),
        ),
        (
            "an_extra_scheduler",
            lambda value: value["schedulers"].__setitem__("intelligence-42-other", {}),
        ),
        (
            "an_operation_sequence_that_creates_twice",
            lambda value: value["operations"][2].__setitem__("kind", "create"),
        ),
        (
            "an_operation_sequence_that_confirms_before_it_pauses",
            lambda value: [
                value["operations"][2].__setitem__("kind", "confirm"),
                value["operations"][3].__setitem__("kind", "pause"),
            ],
        ),
        (
            "an_unexpected_state",
            lambda value: value["schedulers"][DAILY_JOB_ID].__setitem__(
                "expected_state", "ENABLED"
            ),
        ),
        (
            "an_invoker_row_of_another_role",
            lambda value: value["schedulers"][DAILY_JOB_ID]["invoker_bindings"][
                0
            ].__setitem__("role", "roles/owner"),
        ),
    ],
)
def test_a_malformed_plan_refuses_by_name_rather_than_raising(case, mutate):
    plan = _resealed(_plan(), mutate)
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.apply_plan(plan, adapter=_adapter(cloud))
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.read_back(plan, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_plan_whose_scheduler_name_is_not_a_string_refuses():
    plan = _resealed(
        _plan(),
        lambda value: value["schedulers"][DAILY_JOB_ID].__setitem__("name", 42),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_name_unpinned$"):
        schedulers.apply_plan(plan, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_the_cli_reports_plan_invalid_rather_than_a_traceback(tmp_path):
    plan = _resealed(
        _plan(), lambda value: value["schedulers"].__setitem__(DAILY_JOB_ID, "enabled")
    )
    source = tmp_path / "plan.json"
    source.write_text(json.dumps(plan), encoding="utf-8")
    written = []
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(source),
            "--output",
            str(tmp_path / "receipt.json"),
        ],
        adapter_factory=lambda state: _adapter(FakeCloud()),
        out=type("Stream", (), {"write": lambda self, text: written.append(text)})(),
    )
    assert code == 1
    assert json.loads(written[0]) == {"status": "refused", "error": "plan_invalid"}


# An applied scheduler is never left running


def _enabled(plan):
    return {
        entry["name"]: {"name": entry["name"], "state": "ENABLED"}
        for entry in plan["schedulers"].values()
    }


def test_an_existing_enabled_scheduler_is_paused_before_apply_reports_applied():
    plan = _plan()
    cloud = FakeCloud(_enabled(plan))
    receipt = schedulers.apply_plan(plan, adapter=_adapter(cloud))
    assert receipt["state"] == "applied"
    assert [entry["result"] for entry in receipt["schedulers"].values()] == [
        "paused",
        "paused",
    ]
    assert all(job["state"] == "PAUSED" for job in cloud.jobs.values())
    assert "create" not in cloud.calls, "an existing scheduler is never created again"


def test_apply_refuses_when_an_existing_scheduler_will_not_pause():
    plan = _plan()
    cloud = FakeCloud(_enabled(plan), paused_state="ENABLED")
    with pytest.raises(ValueError, match="^scheduler_not_paused$"):
        schedulers.apply_plan(plan, adapter=_adapter(cloud))


def test_a_scheduler_without_an_observable_state_refuses():
    plan = _plan()
    name = plan["schedulers"][DAILY_JOB_ID]["name"]
    cloud = FakeCloud({name: {"name": name}})
    with pytest.raises(ValueError, match="^scheduler_readback_invalid$"):
        schedulers.apply_plan(plan, adapter=_adapter(cloud))


class VanishingCloud(FakeCloud):
    """The scheduler is gone by the time the confirming read asks for it."""

    def __call__(self, request):
        response = super().__call__(request)
        if self.calls[-1] == "pause":
            self.jobs.clear()
        return response


def test_a_scheduler_absent_after_its_create_refuses():
    plan = _plan()
    with pytest.raises(ValueError, match="^scheduler_absent_after_create$"):
        schedulers.apply_plan(plan, adapter=_adapter(VanishingCloud()))


def test_a_scheduler_absent_after_its_pause_refuses():
    plan = _plan()
    with pytest.raises(ValueError, match="^scheduler_absent_after_pause$"):
        schedulers.apply_plan(plan, adapter=_adapter(VanishingCloud(_enabled(plan))))


def test_a_conditioned_invoker_binding_refuses(tmp_path):
    def mutate(value):
        for row in value["bindings"]:
            if row["role"] == "roles/run.invoker" and row["member"] == (
                "serviceAccount:" + SCHEDULER_ACCOUNT
            ):
                row["condition"] = {
                    "title": "daylight",
                    "expression": "request.time.getHours() < 6",
                }

    delta_path = _rewrite(tmp_path, DELTA_FILE, mutate)
    with pytest.raises(ValueError, match="^binding_condition_unsupported$"):
        _plan(delta_path=delta_path)


def test_the_readback_binds_the_digest_chain_it_was_read_against():
    plan, cloud = _applied()
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["resource_manifest_sha256"] == plan["resource_manifest_sha256"]
    assert (
        report["scheduler_configuration_sha256"]
        == (plan["scheduler_configuration_sha256"])
    )
    assert report["runtime_configuration_sha256"] == schedulers.file_sha256(
        RUNTIME_FILE
    )


def test_a_resealed_plan_whose_http_target_is_not_an_object_sends_nothing():
    plan = _plan()
    moved = _resealed(
        plan,
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"].__setitem__(
            "httpTarget", "https://run.googleapis.com/v2/jobs/victim:run"
        ),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


# The content of what apply sends comes from the pinned scheduler file


OVERRIDE_BODY = base64.b64encode(
    json.dumps(
        {
            "overrides": {
                "containerOverrides": [
                    {
                        "args": ["--publish", "--all-clients"],
                        "env": [{"name": "OPENAI_API_KEY", "value": "sk-attacker"}],
                    }
                ],
                "taskCount": 50,
                "timeout": "3600s",
            }
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).decode("ascii")


def _job_edit(mutate):
    """A resealed plan whose copy of the daily job carries one changed field."""
    return _resealed(
        _plan(), lambda value: mutate(value["schedulers"][DAILY_JOB_ID]["job"])
    )


CONTENT_FIELDS = [
    ("cron", lambda job: job.__setitem__("schedule", "* * * * *")),
    (
        "retry_configuration",
        lambda job: job.__setitem__(
            "retryConfig", {"maxRetryDuration": "3600s", "retryCount": 5}
        ),
    ),
    ("attempt_deadline", lambda job: job.__setitem__("attemptDeadline", "1800s")),
    ("time_zone", lambda job: job.__setitem__("timeZone", "Africa/Johannesburg")),
    ("description", lambda job: job.__setitem__("description", "changed by hand")),
    (
        "headers",
        lambda job: job["httpTarget"].__setitem__(
            "headers",
            {
                "Content-Type": "application/json",
                "X-Goog-User-Project": "other-project",
            },
        ),
    ),
    (
        "target_body",
        lambda job: job["httpTarget"].__setitem__("body", OVERRIDE_BODY),
    ),
    (
        "http_method",
        lambda job: job["httpTarget"].__setitem__("httpMethod", "PUT"),
    ),
    (
        "token_scope",
        lambda job: job["httpTarget"]["oauthToken"].__setitem__(
            "scope", "https://www.googleapis.com/auth/devstorage.read_write"
        ),
    ),
]


@pytest.mark.parametrize(
    ("field", "mutate"), CONTENT_FIELDS, ids=[case[0] for case in CONTENT_FIELDS]
)
def test_a_resealed_plan_whose_scheduler_content_moved_sends_nothing(field, mutate):
    """The plan's copy of a job is a document; the bytes come from the file."""
    moved = _job_edit(mutate)
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_entry_unsealed$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


@pytest.mark.parametrize(
    ("field", "mutate"), CONTENT_FIELDS, ids=[case[0] for case in CONTENT_FIELDS]
)
def test_readback_refuses_a_resealed_plan_whose_scheduler_content_moved(field, mutate):
    moved = _job_edit(mutate)
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_entry_unsealed$"):
        schedulers.read_back(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_resealed_plan_carrying_the_full_override_payload_sends_nothing():
    """The whole shape the review demonstrated, refused before a byte goes out."""

    def mutate(job):
        job["schedule"] = "* * * * *"
        job["attemptDeadline"] = "1800s"
        job["retryConfig"] = {"maxRetryDuration": "3600s", "retryCount": 5}
        job["httpTarget"]["headers"] = {
            "Content-Type": "application/json",
            "X-Goog-User-Project": "other-project",
        }
        job["httpTarget"]["body"] = OVERRIDE_BODY

    moved = _job_edit(mutate)
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_entry_unsealed$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []
    assert cloud.jobs == {}


def test_the_create_body_apply_sends_is_the_one_in_the_pinned_scheduler_file():
    plan = _plan()
    cloud = FakeCloud()
    adapter = _adapter(cloud)
    schedulers.apply_plan(plan, adapter=adapter)
    sealed = load_scheduler_configuration(
        SCHEDULER_FILE,
        expected_sha256=load_runtime_configuration(RUNTIME_FILE)[
            "scheduler_configuration_sha256"
        ],
    )
    sent = {
        row["request"]["name"]: row["request"]
        for row in adapter.record
        if row["call"] == "create_scheduler"
    }
    assert len(sent) == 2
    for entry in sealed["schedulers"].values():
        expected = {key: value for key, value in entry["job"].items() if key != "state"}
        assert sent[expected["name"]] == expected, "the file is what went on the wire"
        assert cloud.jobs[expected["name"]] == {**expected, "state": "PAUSED"}


def test_an_extra_key_on_the_planned_create_body_refuses_and_sends_nothing():
    moved = _resealed(
        _plan(),
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"].__setitem__(
            "state", "ENABLED"
        ),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_missing_key_on_the_planned_create_body_refuses_and_sends_nothing():
    moved = _resealed(
        _plan(),
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"].pop("attemptDeadline"),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


# The dry run is evidence of what apply would send, not an echo of the plan


def _sent_requests(adapter):
    """Every request that actually went out, in the shape the dry run renders."""
    sent = []
    for row in adapter.record:
        rendered = {key: row[key] for key in ("call", "method", "url")}
        if "request" in row:
            rendered["body"] = row["request"]
        sent.append(rendered)
    return sent


def test_the_dry_run_renders_what_apply_would_send_rather_than_the_plan_document():
    def mutate(value):
        request = value["operations"][1]["request"]
        request["url"] = "https://example.invalid/anything"
        request["body"]["schedule"] = "* * * * *"
        value["operations"][0]["request"]["url"] = "https://example.invalid/anything"

    moved = _resealed(_plan(), mutate)
    cloud = FakeCloud()
    rendered = schedulers.apply_plan(moved, adapter=_adapter(cloud), dry_run=True)
    assert cloud.calls == []
    urls = [request["url"] for request in rendered["requests"]]
    assert "https://example.invalid/anything" not in urls
    assert all(
        url.startswith("https://cloudscheduler.googleapis.com/v1/") for url in urls
    )
    created = [
        request
        for request in rendered["requests"]
        if request["call"] == "create_scheduler"
    ]
    assert [body["body"]["schedule"] for body in created] == [
        "0 3 * * *",
        "0 */12 * * *",
    ]


def test_the_dry_run_renders_exactly_the_requests_apply_sends():
    plan = _plan()
    rendered = schedulers.apply_plan(plan, adapter=_adapter(FakeCloud()), dry_run=True)
    adapter = _adapter(FakeCloud())
    schedulers.apply_plan(plan, adapter=adapter)
    assert rendered["requests"] == _sent_requests(adapter)


# A refusal after a mutation still owes an artifact


def _stream():
    written = []
    return written, type(
        "Stream", (), {"write": lambda self, text: written.append(text)}
    )()


def _plan_file(tmp_path, plan):
    source = tmp_path / "plan.json"
    source.write_text(json.dumps(plan), encoding="utf-8")
    return source


def test_apply_attaches_the_record_to_a_refusal_raised_after_a_mutation():
    plan = _plan()
    cloud = FakeCloud(paused_state="ENABLED")
    adapter = _adapter(cloud)
    with pytest.raises(ValueError, match="^scheduler_not_paused$") as raised:
        schedulers.apply_plan(plan, adapter=adapter)
    receipt = raised.value.receipt
    assert receipt["contract_version"] == schedulers.RECEIPT_CONTRACT
    assert receipt["state"] == "refused"
    assert receipt["error"] == "scheduler_not_paused"
    assert receipt["plan_sha256"] == plan["plan_sha256"]
    assert receipt["record"] == list(adapter.record)
    assert [row["call"] for row in receipt["record"]].count("create_scheduler") == 2


def test_a_create_refused_by_the_endpoint_still_names_the_request_that_went_out():
    plan = _plan()
    cloud = FakeCloud(fail={"create": (403, {"error": {"message": "denied"}})})
    adapter = _adapter(cloud)
    with pytest.raises(schedulers.NativeError) as raised:
        schedulers.apply_plan(plan, adapter=adapter)
    receipt = raised.value.receipt
    assert receipt["state"] == "refused"
    assert receipt["record"][-1]["call"] == "create_scheduler"
    assert receipt["record"][-1]["status"] == 403


def test_a_refusal_before_any_mutation_carries_no_receipt():
    plan = _resealed(
        _plan(), lambda value: value["schedulers"].__setitem__(DAILY_JOB_ID, "enabled")
    )
    with pytest.raises(ValueError) as raised:
        schedulers.apply_plan(plan, adapter=_adapter(FakeCloud()))
    assert not hasattr(raised.value, "receipt")


def _stateless(plan):
    """One existing scheduler whose answer carries no observable state."""
    name = plan["schedulers"][DAILY_JOB_ID]["name"]
    return FakeCloud({name: {"name": name}})


def test_a_refusal_after_a_read_but_before_any_mutation_carries_no_receipt():
    plan = _plan()
    cloud = _stateless(plan)
    with pytest.raises(ValueError, match="^scheduler_readback_invalid$") as raised:
        schedulers.apply_plan(plan, adapter=_adapter(cloud))
    assert cloud.calls == ["get"], "only a read went out"
    assert not hasattr(raised.value, "receipt")


def test_the_cli_writes_no_document_when_only_reads_went_out(tmp_path):
    plan = _plan()
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(_stateless(plan)),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {
        "status": "refused",
        "error": "scheduler_readback_invalid",
    }
    assert not output.exists()


def test_the_cli_writes_a_refusal_document_when_a_mutation_already_went_out(tmp_path):
    plan = _plan()
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(FakeCloud(paused_state="ENABLED")),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0])["error"] == "scheduler_not_paused"
    assert json.loads(written[0])["receipt"] == str(output)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["state"] == "refused"
    assert document["error"] == "scheduler_not_paused"
    assert document["schedulers"][DAILY_JOB_ID]["state"] == "ENABLED"
    assert [row["call"] for row in document["record"]].count("create_scheduler") == 2


def test_the_cli_writes_a_refusal_document_when_only_a_pause_went_out(tmp_path):
    """An existing scheduler that will not pause is still a mutation that was sent."""
    plan = _plan()
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    cloud = FakeCloud(_enabled(plan), paused_state="ENABLED")
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    assert code == 1
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["state"] == "refused"
    assert document["error"] == "scheduler_not_paused"
    calls = [row["call"] for row in document["record"]]
    assert "create_scheduler" not in calls
    assert calls.count("pause_scheduler") == 2


def test_the_cli_writes_no_document_when_a_refusal_precedes_every_mutation(tmp_path):
    plan = _resealed(
        _plan(), lambda value: value["schedulers"].__setitem__(DAILY_JOB_ID, "enabled")
    )
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(FakeCloud()),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {"status": "refused", "error": "plan_invalid"}
    assert not output.exists()


# A plan value of the wrong JSON type refuses by name rather than raising


def test_a_plan_whose_operation_names_a_scheduler_array_refuses_by_name():
    moved = _resealed(
        _plan(),
        lambda value: value["operations"][0].__setitem__("scheduler", [DAILY_JOB_ID]),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.read_back(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_plan_whose_operation_names_a_scheduler_it_does_not_carry_refuses():
    moved = _resealed(
        _plan(),
        lambda value: value["operations"][0].__setitem__(
            "scheduler", "intelligence-42-other"
        ),
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.apply_plan(moved, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_a_plan_whose_operation_names_a_scheduler_object_refuses_by_name():
    moved = _resealed(
        _plan(),
        lambda value: value["operations"][0].__setitem__(
            "scheduler", {DAILY_JOB_ID: 1}
        ),
    )
    with pytest.raises(ValueError, match="^plan_invalid$"):
        schedulers.apply_plan(moved, adapter=_adapter(FakeCloud()))


def test_the_cli_reports_an_unhashable_scheduler_key_rather_than_a_traceback(tmp_path):
    plan = _resealed(
        _plan(),
        lambda value: value["operations"][0].__setitem__("scheduler", [DAILY_JOB_ID]),
    )
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(tmp_path / "receipt.json"),
        ],
        adapter_factory=lambda state: _adapter(FakeCloud()),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {"status": "refused", "error": "plan_invalid"}


# The confirming read is checked for the name it answered with


VICTIM_NAME = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-other"
)


class Renaming(FakeCloud):
    """A surface whose confirming read answers for a scheduler nobody asked for."""

    def __call__(self, request):
        response = super().__call__(request)
        body = json.loads(response["body"].decode("utf-8"))
        if request["method"] == "GET" and body.get("state") == "PAUSED":
            body["name"] = VICTIM_NAME
            response["body"] = json.dumps(body).encode("utf-8")
        return response


def test_a_confirming_read_that_answers_for_another_scheduler_refuses():
    cloud = Renaming()
    with pytest.raises(ValueError, match="^scheduler_name_unconfirmed$"):
        schedulers.apply_plan(_plan(), adapter=_adapter(cloud))
    assert cloud.calls == ["get", "create", "pause", "get"]


def test_apply_refuses_a_scheduler_file_that_left_the_digest_the_runtime_binds(
    tmp_path, monkeypatch
):
    """The content anchor is the digest the runtime file binds, not the path."""
    scheduler_path = _rewrite(
        tmp_path,
        SCHEDULER_FILE,
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"].__setitem__(
            "schedule", "*/5 * * * *"
        ),
    )
    monkeypatch.setattr(schedulers, "SCHEDULER_FILE", scheduler_path)
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_configuration_digest_mismatch$"):
        schedulers.apply_plan(_plan(), adapter=_adapter(cloud))
    assert cloud.calls == []


def test_readback_refuses_a_read_that_answered_for_another_scheduler():
    plan, cloud = _applied()
    with pytest.raises(ValueError, match="^scheduler_name_unconfirmed$"):
        schedulers.read_back(plan, adapter=_adapter(Renaming(cloud.jobs)))


def test_apply_validates_the_scheduler_file_where_the_request_is_sent(
    tmp_path, monkeypatch
):
    """The loader that owns the cron grammar runs on the sending path, not only
    where the plan was rendered."""
    paths = _rebound(
        tmp_path,
        lambda value: value["schedulers"][DAILY_JOB_ID]["job"].__setitem__(
            "schedule", "every minute"
        ),
    )
    plan = _resealed(
        _plan(),
        lambda value: value.update(
            {
                "runtime_configuration_sha256": schedulers.file_sha256(
                    paths["runtime_path"]
                ),
                "scheduler_configuration_sha256": schedulers.file_sha256(
                    paths["scheduler_path"]
                ),
            }
        ),
    )
    monkeypatch.setattr(schedulers, "SCHEDULER_FILE", paths["scheduler_path"])
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^scheduler_configuration_invalid$"):
        schedulers.apply_plan(
            plan, adapter=_adapter(cloud), runtime_path=paths["runtime_path"]
        )
    assert cloud.calls == []


# A transport that does not answer, an artifact that has somewhere to land, and
# a record that names every request that left the client


def _read_request(connection):
    """One HTTP request off a loopback socket, headers and body."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            return None, None
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        body += connection.recv(65536)
    return head.decode("utf-8"), body


def _answer(status, value):
    body = json.dumps(value).encode("utf-8")
    head = (
        f"HTTP/1.1 {status} ANSWER\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("utf-8") + body


class DyingLoopback:
    """A loopback endpoint that answers a fixed list and then stops answering.

    Nothing leaves 127.0.0.1. Once the list is spent the socket is closed with
    no response at all, which is the read timeout, the reset connection and the
    dropped name resolution a transport that always answers can never show.
    """

    def __init__(self, answers, *, path="/v1/"):
        self.answers = list(answers)
        self.requests = []
        self.path = path
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                head, body = _read_request(self.request)
                if head is None:
                    return
                outer.requests.append({"head": head, "body": body})
                if outer.answers:
                    self.request.sendall(_answer(*outer.answers.pop(0)))
                self.request.close()

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *details):
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.thread.join(timeout=10)

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}{self.path}"


def _live_adapter_factory():
    from ops.deploy import runtime_native_adapter as native

    return lambda state: native.NativeAdapter(state, token_source=lambda: "fake-token")


def test_a_transport_that_dies_after_a_create_still_writes_a_refusal_receipt(
    tmp_path, monkeypatch
):
    """A create that went out and an answer that never came still owe an artifact."""
    from ops.deploy import runtime_native_adapter as native

    plan = _plan()
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    with DyingLoopback([(404, {"error": "absent"}), (200, {})]) as server:
        monkeypatch.setattr(native, "SCHEDULER_ENDPOINT", server.endpoint)
        code = schedulers.main(
            [
                "apply",
                "--plan",
                str(_plan_file(tmp_path, plan)),
                "--output",
                str(output),
            ],
            adapter_factory=_live_adapter_factory(),
            out=stream,
        )
        assert len(server.requests) == 3, "the create and the pause both left"
    assert code == 1
    refusal = json.loads(written[0])
    assert refusal["status"] == "refused"
    assert refusal["receipt"] == str(output)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["state"] == "refused"
    assert document["sent"] == ["create_scheduler", "pause_scheduler"]
    assert [row["call"] for row in document["record"]] == [
        "get_scheduler",
        "create_scheduler",
        "pause_scheduler",
    ]
    assert "status" not in document["record"][-1], "the pause was never answered"


def test_apply_attaches_the_record_when_the_transport_never_answers(tmp_path):
    """apply_plan owns the refusal, so a transport level failure is not a traceback."""
    plan = _plan()
    cloud = FakeCloud()
    dying = _dying_after(cloud, "create")
    adapter = _adapter(dying)
    with pytest.raises(OSError) as raised:
        schedulers.apply_plan(plan, adapter=adapter)
    receipt = raised.value.receipt
    assert receipt["state"] == "refused"
    assert receipt["sent"] == ["create_scheduler", "pause_scheduler"]
    assert [row["call"] for row in receipt["record"]][-1] == "pause_scheduler"


def _dying_after(cloud, call):
    """A transport that answers up to and including one call and then raises."""
    import urllib.error

    dead = []

    def transport(request):
        if dead:
            raise urllib.error.URLError(
                ConnectionResetError(104, "Connection reset by peer")
            )
        response = cloud(request)
        if cloud.calls[-1] == call:
            dead.append(True)
        return response

    return transport


# The artifact has somewhere to land before the first request goes out


def test_the_cli_refuses_an_output_that_already_exists_before_it_sends_anything(
    tmp_path,
):
    plan = _plan()
    output = tmp_path / "receipt.json"
    output.write_text("operator notes\n", encoding="utf-8")
    cloud = FakeCloud()
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {"status": "refused", "error": "output_exists"}
    assert cloud.calls == [], "nothing is sent while the receipt has nowhere to go"
    assert output.read_text(encoding="utf-8") == "operator notes\n"


def test_the_cli_refuses_an_output_directory_that_is_absent_before_it_sends_anything(
    tmp_path,
):
    plan = _plan()
    output = tmp_path / "absent" / "receipt.json"
    cloud = FakeCloud()
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {
        "status": "refused",
        "error": "output_directory_absent",
    }
    assert cloud.calls == []


class Blocking(FakeCloud):
    """A surface that fills the output path while the requests are in flight."""

    def __init__(self, output, **keywords):
        super().__init__(**keywords)
        self.output = output

    def __call__(self, request):
        response = super().__call__(request)
        self.output.mkdir(exist_ok=True)
        return response


def test_a_receipt_that_cannot_be_written_lands_beside_the_output(tmp_path):
    """A document that cannot reach its path is named elsewhere, never discarded."""
    plan = _plan()
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(Blocking(output)),
        out=stream,
    )
    assert code == 0
    line = json.loads(written[0])
    assert line["status"] == "applied"
    assert line["output"] != str(output)
    document = json.loads(Path(line["output"]).read_text(encoding="utf-8"))
    assert document["state"] == "applied"
    assert document["plan_sha256"] == plan["plan_sha256"]


def test_a_refusal_receipt_that_cannot_be_written_lands_beside_the_output(tmp_path):
    plan = _plan()
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(
            Blocking(output, paused_state="ENABLED")
        ),
        out=stream,
    )
    assert code == 1
    line = json.loads(written[0])
    assert line["error"] == "scheduler_not_paused"
    assert line["receipt"] != str(output)
    assert "receipt_unwritten" not in line
    document = json.loads(Path(line["receipt"]).read_text(encoding="utf-8"))
    assert document["state"] == "refused"
    assert [row["call"] for row in document["record"]].count("create_scheduler") == 2


# The receipt names what went out, and says which field to read


def test_a_request_refused_after_it_was_sent_is_still_named_in_the_receipt(tmp_path):
    """A 302 answer to a create is a request that left with no readable answer."""
    plan = _plan()
    cloud = FakeCloud(fail={"create": (302, {"error": "moved"})})
    adapter = _adapter(cloud)
    with pytest.raises(schedulers.NativeError, match="redirect_refused") as raised:
        schedulers.apply_plan(plan, adapter=adapter)
    receipt = raised.value.receipt
    assert receipt["sent"] == ["create_scheduler"]
    assert [row["call"] for row in receipt["record"]] == [
        "get_scheduler",
        "create_scheduler",
    ]
    assert "status" not in receipt["record"][-1]


def test_a_refusal_receipt_says_that_schedulers_names_only_finished_jobs():
    """A create that went out is in record and sent, never in schedulers."""
    plan = _plan()
    cloud = FakeCloud(fail={"pause": (403, {"error": {"message": "denied"}})})
    with pytest.raises(schedulers.NativeError) as raised:
        schedulers.apply_plan(plan, adapter=_adapter(cloud))
    receipt = raised.value.receipt
    assert receipt["schedulers"] == {}
    assert receipt["sent"] == ["create_scheduler", "pause_scheduler"]
    assert [row["call"] for row in receipt["record"]].count("create_scheduler") == 1
    assert "record" in receipt["read_this_first"]
    assert "schedulers" in receipt["read_this_first"]


# The dry run renders the whole run; apply against a live project sends no more


def test_the_dry_run_never_understates_what_apply_sends():
    plan = _plan()
    rendered = schedulers.apply_plan(plan, adapter=_adapter(FakeCloud()), dry_run=True)
    entry = plan["schedulers"][DAILY_JOB_ID]
    cloud = FakeCloud({entry["name"]: {**entry["job"], "state": "PAUSED"}})
    schedulers.apply_plan(plan, adapter=_adapter(cloud))
    assert cloud.calls == ["get", "get", "create", "pause", "get"]
    assert len(cloud.calls) < len(rendered["requests"]) == 8


# The plan carries every field of a scheduler job but its output only state


def test_the_compared_fields_and_the_state_cover_every_scheduler_job_key():
    """Nothing else pins this, and the send from the file rests on it."""
    from ops.runners.managed_runtime import _SCHEDULER_JOB_KEYS

    assert set(schedulers.COMPARED_FIELDS) | {"state"} == _SCHEDULER_JOB_KEYS


def test_a_field_the_plan_does_not_carry_still_reaches_the_wire_from_the_file(
    monkeypatch,
):
    """The create body is the reviewed file's job, not the plan's copy of it."""
    monkeypatch.setattr(
        schedulers,
        "COMPARED_FIELDS",
        tuple(field for field in schedulers.COMPARED_FIELDS if field != "description"),
    )
    plan = _plan()
    assert "description" not in plan["schedulers"][DAILY_JOB_ID]["job"]
    cloud = FakeCloud()
    schedulers.apply_plan(plan, adapter=_adapter(cloud))
    reviewed = load_scheduler_configuration(
        SCHEDULER_FILE,
        expected_sha256=load_runtime_configuration(RUNTIME_FILE)[
            "scheduler_configuration_sha256"
        ],
    )["schedulers"][DAILY_JOB_ID]["job"]
    sent = cloud.jobs[plan["schedulers"][DAILY_JOB_ID]["name"]]
    assert set(sent) == set(reviewed)
    assert sent["description"] == reviewed["description"]


def test_the_cli_refuses_an_output_directory_it_cannot_write_before_it_sends(
    tmp_path, monkeypatch
):
    """The process runs as root in this harness, so the denial is stood in for."""
    plan = _plan()
    cloud = FakeCloud()
    written, stream = _stream()
    monkeypatch.setattr(schedulers.os, "access", lambda path, mode: False)
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(tmp_path / "receipt.json"),
        ],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {
        "status": "refused",
        "error": "output_directory_unwritable",
    }
    assert cloud.calls == []


# A receipt with nowhere to land is printed, never lost


class Vanishing(FakeCloud):
    """A surface that removes the output directory after every answer it gives."""

    def __init__(self, directory, **keywords):
        super().__init__(**keywords)
        self.directory = directory

    def __call__(self, request):
        response = super().__call__(request)
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)
        return response


def _apply_into_vanishing(tmp_path, **keywords):
    plan = _plan()
    directory = tmp_path / "out"
    directory.mkdir()
    output = directory / "receipt.json"
    cloud = Vanishing(directory, **keywords)
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    return plan, directory, cloud, code, written


def test_an_applied_receipt_neither_path_can_hold_is_printed_whole(tmp_path):
    """Both the output and its fallback fail, so the receipt goes to stdout."""
    plan, directory, cloud, code, written = _apply_into_vanishing(tmp_path)
    assert cloud.calls.count("create") == 2, "both schedulers were created"
    assert code == 1
    assert len(written) == 1
    line = json.loads(written[0])
    assert line["status"] == "refused"
    assert "receipt" not in line
    assert line["receipt_unwritten"]
    expected = schedulers.apply_plan(_plan(), adapter=_adapter(FakeCloud()))
    assert expected["state"] == "applied"
    assert line["receipt_document"] == expected
    assert not directory.exists()


def test_a_refusal_receipt_neither_path_can_hold_is_printed_whole(tmp_path):
    plan, directory, cloud, code, written = _apply_into_vanishing(
        tmp_path, paused_state="ENABLED"
    )
    assert code == 1
    assert len(written) == 1
    line = json.loads(written[0])
    assert line["status"] == "refused"
    assert line["error"] == "scheduler_not_paused"
    assert "receipt" not in line
    assert line["receipt_unwritten"]
    with pytest.raises(ValueError) as raised:
        schedulers.apply_plan(
            _plan(), adapter=_adapter(FakeCloud(paused_state="ENABLED"))
        )
    assert line["receipt_document"] == raised.value.receipt
    assert line["receipt_document"]["sent"] == [
        "create_scheduler",
        "pause_scheduler",
        "create_scheduler",
        "pause_scheduler",
    ]


class Dangling(FakeCloud):
    """A surface that turns the output path into a link to nowhere mid run."""

    def __init__(self, output, **keywords):
        super().__init__(**keywords)
        self.output = output

    def __call__(self, request):
        response = super().__call__(request)
        if not self.output.is_symlink():
            self.output.symlink_to(self.output.parent / "absent" / "receipt.json")
        return response


def test_a_receipt_whose_path_fails_to_write_lands_beside_it(tmp_path):
    """The requested write raises, not refuses, and the fallback still holds it."""
    plan = _plan()
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(Dangling(output)),
        out=stream,
    )
    assert code == 0
    line = json.loads(written[0])
    assert line["status"] == "applied"
    expected = schedulers.apply_plan(_plan(), adapter=_adapter(FakeCloud()))
    digest = schedulers.canonical_sha256(expected)
    assert line["output"] == str(tmp_path / f"receipt.unwritten-{digest[:16]}.json")
    assert Path(line["output"]).read_bytes() == (
        schedulers.canonical_bytes(expected) + b"\n"
    )
    assert not output.exists()


class Occupying(FakeCloud):
    """A surface during whose run an operator file appears at the output path."""

    def __init__(self, output, **keywords):
        super().__init__(**keywords)
        self.output = output

    def __call__(self, request):
        response = super().__call__(request)
        if not self.output.exists():
            self.output.write_text("operator notes\n", encoding="utf-8")
        return response


def test_a_file_that_appears_at_the_output_is_never_overwritten(tmp_path):
    plan = _plan()
    output = tmp_path / "receipt.json"
    written, stream = _stream()
    code = schedulers.main(
        [
            "apply",
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(Occupying(output)),
        out=stream,
    )
    assert code == 0
    line = json.loads(written[0])
    assert line["output"] != str(output)
    assert output.read_text(encoding="utf-8") == "operator notes\n"
    expected = schedulers.apply_plan(_plan(), adapter=_adapter(FakeCloud()))
    assert Path(line["output"]).read_bytes() == (
        schedulers.canonical_bytes(expected) + b"\n"
    )


def test_the_fallback_beside_an_output_with_no_suffix_is_named_json(tmp_path):
    value = {"state": "applied"}
    digest = schedulers.canonical_sha256(value)
    assert schedulers._sibling(tmp_path / "receipt", value) == (
        tmp_path / f"receipt.unwritten-{digest[:16]}.json"
    )


def test_a_receipt_at_its_requested_path_is_its_canonical_bytes(tmp_path):
    value = {"state": "applied"}
    output = tmp_path / "receipt.json"
    assert schedulers._written(output, value) == str(output)
    assert output.read_bytes() == schedulers.canonical_bytes(value) + b"\n"


# Resume: the price policy scheduler only, and only from a reviewed paused state


RUN_JOB_NAME = PARENT + "/jobs/" + PRICE_POLICY_JOB_ID


class ResumeCloud(FakeCloud):
    """The scheduler surface plus the Cloud Run job read and the resume verb."""

    def __init__(self, existing=(), *, run_jobs=(), resumed_state="ENABLED", **kw):
        super().__init__(existing, **kw)
        self.run_jobs = {name: dict(job) for name, job in dict(run_jobs).items()}
        self.resumed_state = resumed_state

    def __call__(self, request):
        url = request["url"]
        if url.startswith("https://run.googleapis.com/v2/"):
            self.calls.append("get_job")
            if "get_job" in self.fail:
                return self._json(*self.fail["get_job"])
            name = url.split("/v2/", 1)[1]
            job = self.run_jobs.get(name)
            return self._json(404 if job is None else 200, job or {"error": "absent"})
        if url.endswith(":resume"):
            self.calls.append("resume")
            if "resume" in self.fail:
                return self._json(*self.fail["resume"])
            name = url.split("/v1/", 1)[1].rsplit(":resume", 1)[0]
            self.jobs[name] = {**self.jobs[name], "state": self.resumed_state}
            return self._json(200, self.jobs[name])
        return super().__call__(request)


def _paused_cloud(*, run_job=True, **keywords):
    """Both schedulers as apply leaves them, and the price policy job present."""
    plan, seeded = _applied()
    run_jobs = {RUN_JOB_NAME: {"name": RUN_JOB_NAME}} if run_job else {}
    return plan, ResumeCloud(seeded.jobs, run_jobs=run_jobs, **keywords)


def _price_policy_name(plan):
    return plan["schedulers"][PRICE_POLICY_JOB_ID]["name"]


def test_resume_sends_describe_job_read_resume_and_confirm_for_the_price_policy():
    plan, cloud = _paused_cloud()
    receipt = schedulers.resume_scheduler(
        plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud)
    )
    assert cloud.calls == ["get", "get_job", "resume", "get"]
    assert receipt["contract_version"] == schedulers.RESUME_RECEIPT_CONTRACT
    assert receipt["state"] == "resumed"
    assert receipt["scheduler"] == {
        "job": PRICE_POLICY_JOB_ID,
        "result": "resumed",
        "state": "ENABLED",
    }
    assert receipt["sent"] == ["resume_price_policy_scheduler"]
    assert receipt["plan_sha256"] == plan["plan_sha256"]
    resume = receipt["record"][2]
    assert resume["method"] == "POST"
    assert resume["url"] == (
        "https://cloudscheduler.googleapis.com/v1/"
        + _price_policy_name(plan)
        + ":resume"
    )
    assert resume["request"] == {}
    daily = plan["schedulers"][DAILY_JOB_ID]["name"]
    assert cloud.jobs[daily]["state"] == "PAUSED", "the daily scheduler is untouched"


def test_resume_refuses_the_daily_scheduler_before_any_request():
    plan, cloud = _paused_cloud()
    for job_id in (DAILY_JOB_ID, "intelligence-42-other-staging", ""):
        with pytest.raises(ValueError, match="^scheduler_resume_unapproved$"):
            schedulers.resume_scheduler(plan, job_id, adapter=_adapter(cloud))
    assert cloud.calls == []


def test_the_adapter_resume_call_addresses_only_the_price_policy_scheduler():
    from ops.deploy import runtime_native_adapter as native

    with pytest.raises(ValueError, match="^scheduler_resume_unapproved$"):
        native.build_request(
            "resume_price_policy_scheduler", name=PARENT + "/jobs/" + DAILY_JOB_ID
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schedule", "*/5 * * * *"),
        ("description", "changed"),
        ("timeZone", "Europe/London"),
        ("attemptDeadline", "1800s"),
        ("retryConfig", {"retryCount": 5}),
        ("httpTarget", {"uri": "https://example.invalid/run"}),
    ],
)
def test_resume_refuses_a_scheduler_whose_compared_field_drifted(field, value):
    plan, cloud = _paused_cloud()
    cloud.jobs[_price_policy_name(plan)][field] = value
    with pytest.raises(ValueError, match="^price_policy_scheduler_drifted$"):
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert cloud.calls == ["get"]


def test_resume_refuses_when_the_target_job_is_missing():
    plan, cloud = _paused_cloud(run_job=False)
    with pytest.raises(ValueError, match="^price_policy_job_absent$"):
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert cloud.calls == ["get", "get_job"]
    assert cloud.jobs[_price_policy_name(plan)]["state"] == "PAUSED"


def test_resume_refuses_a_job_read_that_answers_for_another_job():
    plan, cloud = _paused_cloud()
    cloud.run_jobs[RUN_JOB_NAME] = {"name": PARENT + "/jobs/" + DAILY_JOB_ID}
    with pytest.raises(ValueError, match="^price_policy_job_unconfirmed$"):
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert "resume" not in cloud.calls


def test_resume_reports_an_enabled_scheduler_as_already_enabled_and_sends_nothing():
    plan, cloud = _paused_cloud()
    cloud.jobs[_price_policy_name(plan)]["state"] = "ENABLED"
    receipt = schedulers.resume_scheduler(
        plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud)
    )
    assert cloud.calls == ["get"]
    assert receipt["state"] == "already_enabled"
    assert receipt["scheduler"]["result"] == "already_enabled"
    assert receipt["sent"] == []


@pytest.mark.parametrize("state", ["UPDATE_FAILED", "STATE_UNSPECIFIED", "DISABLED"])
def test_resume_refuses_any_state_but_paused_or_enabled(state):
    plan, cloud = _paused_cloud()
    cloud.jobs[_price_policy_name(plan)]["state"] = state
    with pytest.raises(ValueError, match="^price_policy_scheduler_not_paused$"):
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert cloud.calls == ["get"]


def test_resume_refuses_an_absent_scheduler_rather_than_creating_it():
    plan = _plan()
    cloud = ResumeCloud(run_jobs={RUN_JOB_NAME: {"name": RUN_JOB_NAME}})
    with pytest.raises(ValueError, match="^price_policy_scheduler_absent$"):
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert cloud.calls == ["get"]


def test_a_resume_that_does_not_confirm_enabled_refuses_with_its_record():
    plan, cloud = _paused_cloud(resumed_state="PAUSED")
    with pytest.raises(
        ValueError, match="^price_policy_scheduler_not_enabled$"
    ) as raised:
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    receipt = raised.value.receipt
    assert receipt["state"] == "refused"
    assert receipt["sent"] == ["resume_price_policy_scheduler"]
    assert [entry["call"] for entry in receipt["record"]] == [
        "get_scheduler",
        "get_job",
        "resume_price_policy_scheduler",
        "get_scheduler",
    ]


def test_a_confirming_read_whose_fields_moved_refuses():
    class Moving(ResumeCloud):
        def __call__(self, request):
            response = super().__call__(request)
            if request["url"].endswith(":resume"):
                name = request["url"].split("/v1/", 1)[1].rsplit(":resume", 1)[0]
                self.jobs[name]["schedule"] = "*/5 * * * *"
            return response

    plan, seeded = _paused_cloud()
    cloud = Moving(seeded.jobs, run_jobs=seeded.run_jobs)
    with pytest.raises(ValueError, match="^price_policy_scheduler_drifted$") as raised:
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert raised.value.receipt["sent"] == ["resume_price_policy_scheduler"]


def test_a_refusal_before_the_resume_carries_no_receipt():
    plan, cloud = _paused_cloud(run_job=False)
    with pytest.raises(ValueError) as raised:
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert not hasattr(raised.value, "receipt")


def test_resume_refuses_a_resealed_plan_whose_scheduler_content_moved():
    plan, cloud = _paused_cloud()
    moved = _resealed(
        plan,
        lambda value: value["schedulers"][PRICE_POLICY_JOB_ID]["job"].__setitem__(
            "schedule", "*/5 * * * *"
        ),
    )
    with pytest.raises(ValueError, match="^scheduler_entry_unsealed$"):
        schedulers.resume_scheduler(moved, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert cloud.calls == []


@pytest.mark.parametrize(
    ("prefix", "action"),
    [("scheduler", "deploy"), ("scheduler", "read"), ("run_job", "invoke")],
)
def test_resume_runs_the_resource_guard_before_it_sends_anything(
    tmp_path, prefix, action
):
    _plan_unused, seeded = _paused_cloud()
    chosen = SCHEDULER_PREFIX if prefix == "scheduler" else RUN_JOB_PREFIX
    plan, paths = _rebound_to(tmp_path, chosen, action)
    cloud = ResumeCloud(seeded.jobs, run_jobs=seeded.run_jobs)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        schedulers.resume_scheduler(
            plan,
            PRICE_POLICY_JOB_ID,
            adapter=_adapter(cloud),
            runtime_path=paths["runtime_path"],
            manifest_path=paths["manifest_path"],
        )
    assert cloud.calls == []


def test_the_resume_dry_run_renders_exactly_the_requests_a_resume_sends():
    plan, cloud = _paused_cloud()
    rendered = schedulers.resume_scheduler(
        plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud), dry_run=True
    )
    assert cloud.calls == []
    assert rendered["state"] == "rendered"
    receipt = schedulers.resume_scheduler(
        plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud)
    )
    sent = [
        {key: entry[key] for key in ("call", "method", "url")}
        | ({"body": entry["request"]} if "request" in entry else {})
        for entry in receipt["record"]
    ]
    assert rendered["requests"] == sent
    assert [entry["call"] for entry in sent] == [
        "get_scheduler",
        "get_job",
        "resume_price_policy_scheduler",
        "get_scheduler",
    ]


def test_the_resume_dry_run_refuses_the_daily_scheduler_too():
    plan, cloud = _paused_cloud()
    with pytest.raises(ValueError, match="^scheduler_resume_unapproved$"):
        schedulers.resume_scheduler(
            plan, DAILY_JOB_ID, adapter=_adapter(cloud), dry_run=True
        )


def _resume_cli(tmp_path, plan, cloud, output, *extra, job=PRICE_POLICY_JOB_ID):
    written, stream = _stream()
    code = schedulers.resume_main(
        [
            "--job",
            job,
            "--plan",
            str(_plan_file(tmp_path, plan)),
            "--output",
            str(output),
            *extra,
        ],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    return code, [json.loads(line) for line in written]


def test_the_resume_cli_writes_a_write_once_receipt(tmp_path):
    plan, cloud = _paused_cloud()
    output = tmp_path / "resume-receipt.json"
    code, lines = _resume_cli(tmp_path, plan, cloud, output)
    assert code == 0
    assert lines == [{"status": "resumed", "output": str(output)}]
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["scheduler"]["state"] == "ENABLED"
    code, lines = _resume_cli(tmp_path, plan, cloud, output)
    assert code == 1
    assert lines == [{"status": "refused", "error": "output_exists"}]


def test_the_resume_cli_refuses_an_existing_output_before_any_request(tmp_path):
    plan, cloud = _paused_cloud()
    output = tmp_path / "resume-receipt.json"
    output.write_text("operator notes\n", encoding="utf-8")
    code, lines = _resume_cli(tmp_path, plan, cloud, output)
    assert code == 1
    assert lines == [{"status": "refused", "error": "output_exists"}]
    assert cloud.calls == []
    assert output.read_text(encoding="utf-8") == "operator notes\n"


def test_the_resume_cli_refuses_the_daily_scheduler_and_writes_nothing(tmp_path):
    plan, cloud = _paused_cloud()
    output = tmp_path / "resume-receipt.json"
    code, lines = _resume_cli(tmp_path, plan, cloud, output, job=DAILY_JOB_ID)
    assert code == 1
    assert lines == [{"status": "refused", "error": "scheduler_resume_unapproved"}]
    assert cloud.calls == []
    assert not output.exists()


def test_the_resume_cli_dry_run_sends_nothing(tmp_path):
    plan, cloud = _paused_cloud()
    output = tmp_path / "resume-rendered.json"
    code, lines = _resume_cli(tmp_path, plan, cloud, output, "--dry-run")
    assert code == 0
    assert lines[0]["status"] == "rendered"
    assert cloud.calls == []


def test_the_resume_cli_writes_a_refusal_receipt_once_the_resume_went_out(tmp_path):
    plan, cloud = _paused_cloud(resumed_state="PAUSED")
    output = tmp_path / "resume-receipt.json"
    code, lines = _resume_cli(tmp_path, plan, cloud, output)
    assert code == 1
    assert lines[0]["error"] == "price_policy_scheduler_not_enabled"
    assert lines[0]["receipt"] == str(output)
    assert json.loads(output.read_text(encoding="utf-8"))["state"] == "refused"


def test_the_resume_entry_point_is_its_own_module_beside_the_creation_commands():
    from ops.deploy import runtime_scheduler_resume

    assert runtime_scheduler_resume.main is schedulers.resume_main


# The readback compares the job as the service stores it


def _as_the_service_stores_it(job):
    """The job a describe returns: proto3 drops retryCount 0, the service stores
    its documented retry defaults and its User-Agent header, and adds output only
    fields the comparison never reads."""
    stored = copy.deepcopy(job)
    stored["retryConfig"] = {
        "maxBackoffDuration": "3600s",
        "maxDoublings": 5,
        "maxRetryDuration": "0s",
        "minBackoffDuration": "5s",
    }
    stored["httpTarget"]["headers"]["User-Agent"] = "Google-Cloud-Scheduler"
    stored["userUpdateTime"] = "2026-09-26T06:00:00Z"
    stored["scheduleTime"] = "2026-09-26T12:00:00Z"
    stored["status"] = {}
    return stored


def _reviewed_job(job_id):
    return load_scheduler_configuration(
        SCHEDULER_FILE,
        expected_sha256=load_runtime_configuration(RUNTIME_FILE)[
            "scheduler_configuration_sha256"
        ],
    )["schedulers"][job_id]["job"]


def _set(path, value):
    def mutate(job):
        *parents, leaf = path
        for key in parents:
            job = job[key]
        if value is _REMOVE:
            del job[leaf]
        else:
            job[leaf] = value

    return mutate


_REMOVE = object()
def _stored_cloud(cloud_type=FakeCloud, **keywords):
    plan, seeded = _applied()
    jobs = {name: _as_the_service_stores_it(job) for name, job in seeded.jobs.items()}
    return plan, cloud_type(jobs, **keywords)


def test_the_comparison_of_the_reviewed_job_with_itself_finds_nothing():
    job = schedulers._compared(_reviewed_job(PRICE_POLICY_JOB_ID))
    assert schedulers._compare(job, copy.deepcopy(job)) == ({}, {})


def test_readback_matches_a_job_carrying_only_the_service_defaults():
    plan, cloud = _stored_cloud()
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "matched"
    entry = report["schedulers"][PRICE_POLICY_JOB_ID]
    assert entry["differences"] == {}
    assert entry["accepted_service_values"] == {
        "httpTarget.headers.User-Agent": {
            "expected": None,
            "observed": "Google-Cloud-Scheduler",
        },
        "retryConfig.maxBackoffDuration": {"expected": None, "observed": "3600s"},
        "retryConfig.maxDoublings": {"expected": None, "observed": 5},
        "retryConfig.minBackoffDuration": {"expected": None, "observed": "5s"},
        "retryConfig.retryCount": {"expected": 0, "observed": None},
    }


def test_readback_of_the_exact_reviewed_job_accepts_nothing():
    plan, cloud = _applied()
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "matched"
    for job_id in (DAILY_JOB_ID, PRICE_POLICY_JOB_ID):
        assert report["schedulers"][job_id]["accepted_service_values"] == {}


def test_readback_matches_the_live_price_policy_shape_read_at_r9_10():
    """The describe of intelligence-42-price-policy-staging on 2026-09-26 differed
    from the reviewed file only by the service's User-Agent header, the three
    retry backoff defaults and retryCount 0 dropped; that exact job matches."""
    reviewed = _reviewed_job(PRICE_POLICY_JOB_ID)
    live = copy.deepcopy(reviewed)
    live["httpTarget"]["headers"]["User-Agent"] = "Google-Cloud-Scheduler"
    live["retryConfig"] = {
        "maxBackoffDuration": "3600s",
        "maxDoublings": 5,
        "maxRetryDuration": "0s",
        "minBackoffDuration": "5s",
    }
    differences, accepted = schedulers._compare(schedulers._compared(reviewed), live)
    assert differences == {}
    assert set(accepted) == {
        "httpTarget.headers.User-Agent",
        "retryConfig.maxBackoffDuration",
        "retryConfig.maxDoublings",
        "retryConfig.minBackoffDuration",
        "retryConfig.retryCount",
    }


@pytest.mark.parametrize(
    ("path", "value", "named", "expected", "observed"),
    [
        (("attemptDeadline",), "180.000s", "attemptDeadline", "180s", "180.000s"),
        (("attemptDeadline",), _REMOVE, "attemptDeadline", "180s", None),
        (
            ("retryConfig", "maxRetryDuration"),
            _REMOVE,
            "retryConfig.maxRetryDuration",
            "0s",
            None,
        ),
        (
            ("httpTarget", "oauthToken", "scope"),
            _REMOVE,
            "httpTarget.oauthToken.scope",
            "https://www.googleapis.com/auth/cloud-platform",
            None,
        ),
        (
            ("httpTarget", "headers", "user-agent"),
            "Google-Cloud-Scheduler",
            "httpTarget.headers.user-agent",
            None,
            "Google-Cloud-Scheduler",
        ),
        (
            ("httpTarget", "headers", "X-CloudScheduler"),
            "true",
            "httpTarget.headers.X-CloudScheduler",
            None,
            "true",
        ),
    ],
)
def test_readback_reconciles_nothing_beyond_the_named_service_values(
    path, value, named, expected, observed
):
    plan, cloud = _stored_cloud()
    _set(path, value)(cloud.jobs[plan["schedulers"][PRICE_POLICY_JOB_ID]["name"]])
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "drifted"
    assert report["schedulers"][PRICE_POLICY_JOB_ID]["differences"] == {
        named: {"expected": expected, "observed": observed}
    }


def test_resume_goes_ahead_on_a_job_carrying_only_the_service_defaults():
    plan, cloud = _stored_cloud(
        ResumeCloud, run_jobs={RUN_JOB_NAME: {"name": RUN_JOB_NAME}}
    )
    receipt = schedulers.resume_scheduler(
        plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud)
    )
    assert receipt["state"] == "resumed"
    assert cloud.calls == ["get", "get_job", "resume", "get"]


_WRONG_URI = f"https://run.googleapis.com/v2/{PARENT}/jobs/{DAILY_JOB_ID}:run"
_WRONG_ACCOUNT = "someone-else@ogilvy-trends-v2.iam.gserviceaccount.com"


@pytest.mark.parametrize(
    ("path", "value", "named", "expected"),
    [
        (("httpTarget", "uri"), _WRONG_URI, "httpTarget.uri", None),
        (("httpTarget", "body"), "e30K", "httpTarget.body", "e30="),
        (("httpTarget", "httpMethod"), "GET", "httpTarget.httpMethod", "POST"),
        (
            ("httpTarget", "oauthToken", "serviceAccountEmail"),
            _WRONG_ACCOUNT,
            "httpTarget.oauthToken.serviceAccountEmail",
            SCHEDULER_ACCOUNT,
        ),
        (
            ("httpTarget", "oauthToken", "scope"),
            "https://www.googleapis.com/auth/run",
            "httpTarget.oauthToken.scope",
            "https://www.googleapis.com/auth/cloud-platform",
        ),
        (
            ("httpTarget", "headers", "Content-Type"),
            "application/octet-stream",
            "httpTarget.headers.Content-Type",
            "application/json",
        ),
        (
            ("httpTarget", "headers", "User-Agent"),
            "curl/8",
            "httpTarget.headers.User-Agent",
            None,
        ),
        (
            ("httpTarget", "headers", "X-Extra"),
            "1",
            "httpTarget.headers.X-Extra",
            None,
        ),
        (("schedule",), "*/5 * * * *", "schedule", "0 */12 * * *"),
        (("timeZone",), "Europe/London", "timeZone", "Etc/UTC"),
        (("attemptDeadline",), "1800s", "attemptDeadline", "180s"),
        (("retryConfig", "retryCount"), 1, "retryConfig.retryCount", 0),
        (
            ("retryConfig", "maxRetryDuration"),
            "600s",
            "retryConfig.maxRetryDuration",
            "0s",
        ),
        (
            ("retryConfig", "minBackoffDuration"),
            "10s",
            "retryConfig.minBackoffDuration",
            "5s",
        ),
        (
            ("retryConfig", "maxBackoffDuration"),
            "60s",
            "retryConfig.maxBackoffDuration",
            "3600s",
        ),
        (("retryConfig", "maxDoublings"), 3, "retryConfig.maxDoublings", 5),
        (("retryConfig", "retryCount"), True, "retryConfig.retryCount", 0),
        (("httpTarget", "headers"), _REMOVE, "httpTarget.headers", None),
    ],
)
def test_readback_names_a_real_difference_beside_the_service_defaults(
    path, value, named, expected
):
    plan, cloud = _stored_cloud()
    name = plan["schedulers"][PRICE_POLICY_JOB_ID]["name"]
    _set(path, value)(cloud.jobs[name])
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "drifted"
    differences = report["schedulers"][PRICE_POLICY_JOB_ID]["differences"]
    assert set(differences) == {named}
    observed = None if value is _REMOVE else value
    if named == "httpTarget.headers":
        expected = {"Content-Type": "application/json"}
    if named == "httpTarget.uri":
        expected = _reviewed_job(PRICE_POLICY_JOB_ID)["httpTarget"]["uri"]
    assert differences[named] == {"expected": expected, "observed": observed}
    assert report["schedulers"][DAILY_JOB_ID]["differences"] == {}


def test_readback_names_an_oidc_token_in_place_of_the_reviewed_oauth_token():
    plan, cloud = _stored_cloud()
    target = cloud.jobs[plan["schedulers"][PRICE_POLICY_JOB_ID]["name"]]["httpTarget"]
    token = target.pop("oauthToken")
    target["oidcToken"] = {"serviceAccountEmail": token["serviceAccountEmail"]}
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    assert report["state"] == "drifted"
    assert set(report["schedulers"][PRICE_POLICY_JOB_ID]["differences"]) == {
        "httpTarget.oauthToken",
        "httpTarget.oidcToken",
    }


def test_readback_keeps_an_absent_retry_configuration_a_difference():
    plan, cloud = _stored_cloud()
    del cloud.jobs[plan["schedulers"][PRICE_POLICY_JOB_ID]["name"]]["retryConfig"]
    report = schedulers.read_back(plan, adapter=_adapter(cloud))
    differences = report["schedulers"][PRICE_POLICY_JOB_ID]["differences"]
    assert differences == {
        "retryConfig": {
            "expected": {
                "maxBackoffDuration": "3600s",
                "maxDoublings": 5,
                "maxRetryDuration": "0s",
                "minBackoffDuration": "5s",
                "retryCount": 0,
            },
            "observed": None,
        }
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("httpTarget", "uri"), _WRONG_URI),
        (("httpTarget", "body"), "e30K"),
        (("httpTarget", "oauthToken", "serviceAccountEmail"), _WRONG_ACCOUNT),
        (("schedule",), "*/5 * * * *"),
        (("retryConfig", "maxDoublings"), 3),
        (("httpTarget", "headers", "X-Extra"), "1"),
    ],
)
def test_resume_refuses_a_real_difference_beside_the_service_defaults(path, value):
    plan, cloud = _stored_cloud(
        ResumeCloud, run_jobs={RUN_JOB_NAME: {"name": RUN_JOB_NAME}}
    )
    _set(path, value)(cloud.jobs[_price_policy_name(plan)])
    with pytest.raises(ValueError, match="^price_policy_scheduler_drifted$"):
        schedulers.resume_scheduler(plan, PRICE_POLICY_JOB_ID, adapter=_adapter(cloud))
    assert cloud.calls == ["get"]
