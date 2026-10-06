"""R05 steps 5 and 6: the attended preflight sends the actual empty RunJobRequest
to the immutable verify-runtime job, reads the operation to a terminal state
through R04's bounded readback, confirms the attached principal and the bound
configuration on the execution itself, and exports the D04 handoff package only
when the runtime is verified, the schedulers read back paused and the invoker
permission is observed on the job policy.
"""

import copy
import json
import socketserver
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ops.deploy import runtime_native_adapter as native
from ops.deploy import runtime_preflight as preflight
from ops.deploy import runtime_schedulers as schedulers
from ops.deploy.readback import wait_operation
from ops.runners.managed_runtime import (
    CONFIGURATION_ANNOTATION,
    DAILY_JOB_ID,
    PRICE_POLICY_JOB_ID,
    canonical_sha256,
    load_runtime_configuration,
)

ROOT = Path(preflight.__file__).resolve().parents[2]
RUNTIME_FILE = ROOT / "infra" / "runtime" / "daily-staging.json"
JOB_NAME = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
)
EXECUTION = JOB_NAME + "/executions/intelligence-42-daily-staging-2xk4z"
OPERATION = "projects/ogilvy-trends-v2/locations/us-central1/operations/op-1"
ORCHESTRATION = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
SCHEDULER_ACCOUNT = "intelligence-42-scheduler@ogilvy-trends-v2.iam.gserviceaccount.com"
IMAGE = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:"
    + "30e02be6"
    + "0" * 56
)
TREE_DELTA_FILE = ROOT / "ops" / "deploy" / "iam_delta_v1.json"
# The scheduler plans here are rendered over the tree bindings under a test local
# stamp, so they hold the bindings whatever the tree's stamp, and the handoff reads
# the same copy.
TEST_STAMP = {
    "applied": False,
    "approved_at": "2026-09-13T08:00:00Z",
    "approved_by": "reviewer",
    "state": "approved",
}
CONFIG = load_runtime_configuration(RUNTIME_FILE)
CONFIG_SHA = canonical_sha256(CONFIG)
ENTRY = CONFIG["jobs"][DAILY_JOB_ID]


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
    monkeypatch.setitem(
        schedulers.render_plan.__kwdefaults__, "delta_path", stamped_tree_delta
    )
    monkeypatch.setitem(
        preflight.build_handoff.__kwdefaults__, "delta_path", stamped_tree_delta
    )


def _task():
    return {
        "serviceAccount": ORCHESTRATION,
        "maxRetries": ENTRY["max_retries"],
        "timeout": f"{ENTRY['timeout_seconds']}s",
        "containers": [
            {
                "image": IMAGE,
                "command": ENTRY["command"],
                "args": ENTRY["args"],
                "env": ENTRY["env"],
            }
        ],
    }


def _job(**overrides):
    job = {
        "name": JOB_NAME,
        "template": {
            "annotations": {CONFIGURATION_ANNOTATION: CONFIG_SHA},
            "taskCount": ENTRY["task_count"],
            "parallelism": ENTRY["parallelism"],
            "template": _task(),
        },
    }
    job.update(overrides)
    return job


def _execution(**overrides):
    execution = {
        "name": EXECUTION,
        "job": JOB_NAME,
        "annotations": {CONFIGURATION_ANNOTATION: CONFIG_SHA},
        "taskCount": ENTRY["task_count"],
        "parallelism": ENTRY["parallelism"],
        "template": _task(),
        "succeededCount": 1,
        "failedCount": 0,
        "cancelledCount": 0,
        "completionTime": "2026-09-18T04:05:06Z",
    }
    execution.update(overrides)
    return execution


def _policy(member=SCHEDULER_ACCOUNT):
    return {
        "bindings": [
            {"role": "roles/run.invoker", "members": ["serviceAccount:" + member]}
        ]
    }


class FakeCloud:
    """The Cloud Run jobs surface: the job, one run, its operation and execution."""

    def __init__(
        self, *, job=None, execution=None, policy=None, pending=0, failed=None
    ):
        self.job = _job() if job is None else job
        self.execution = _execution() if execution is None else execution
        self.policy = _policy() if policy is None else policy
        self.pending = pending
        self.failed = failed
        self.calls = []

    def __call__(self, request):
        url, method = request["url"], request["method"]
        name = url.split("/v2/", 1)[1]
        if method == "POST" and name.endswith(":run"):
            self.calls.append("run")
            return self._json(200, {"name": OPERATION})
        if method == "POST" and name.endswith(":wait"):
            self.calls.append("wait")
            if self.pending > 0:
                self.pending -= 1
                return self._json(200, {"name": OPERATION, "done": False})
            if self.failed is not None:
                return self._json(
                    200, {"name": OPERATION, "done": True, "error": self.failed}
                )
            return self._json(
                200,
                {"name": OPERATION, "done": True, "response": {"name": EXECUTION}},
            )
        if name.endswith(":getIamPolicy"):
            self.calls.append("policy")
            return self._json(200, self.policy)
        if "/executions/" in name:
            self.calls.append("execution")
            return self._json(200, self.execution)
        self.calls.append("job")
        return self._json(200, self.job)

    @staticmethod
    def _json(status, value):
        return {
            "status": status,
            "headers": {},
            "body": json.dumps(value).encode("utf-8"),
        }


def _adapter(cloud):
    return native.NativeAdapter(
        "adc", transport=cloud, token_source=lambda: "fake-token"
    )


def _clock():
    ticks = iter(range(0, 10000, 5))
    return lambda: float(next(ticks))


def _trigger(cloud, **kwargs):
    return preflight.trigger_verify_runtime(adapter=_adapter(cloud), **kwargs)


def _confirm(cloud, triggered, **kwargs):
    return preflight.confirm_execution(
        triggered,
        adapter=_adapter(cloud),
        clock=_clock(),
        sleep=lambda seconds: None,
        deadline_seconds=kwargs.pop("deadline_seconds", 600),
        poll_seconds=kwargs.pop("poll_seconds", 5),
        **kwargs,
    )


# Trigger


def test_the_trigger_sends_the_actual_empty_run_request_after_reading_the_job():
    cloud = FakeCloud()
    triggered = _trigger(cloud)
    assert cloud.calls == ["job", "run"]
    assert triggered["operation"] == OPERATION
    assert triggered["configuration_sha256"] == CONFIG_SHA
    assert triggered["image"] == IMAGE
    run = next(entry for entry in triggered["record"] if entry["call"] == "run_job")
    assert run["request"] == {}
    assert run["url"].endswith("/jobs/intelligence-42-daily-staging:run")


def test_a_job_whose_mode_is_not_verify_runtime_is_never_triggered(tmp_path):
    value = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    value["jobs"][DAILY_JOB_ID]["mode"] = "daily"
    path = tmp_path / "daily-staging.json"
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    cloud = FakeCloud()
    with pytest.raises(ValueError, match="^preflight_mode_forbidden$"):
        _trigger(cloud, runtime_path=path)
    assert cloud.calls == []


def test_a_job_template_that_drifted_from_the_configuration_is_never_triggered():
    job = _job()
    job["template"]["template"]["containers"][0]["args"] = ["-m", "other.module"]
    cloud = FakeCloud(job=job)
    with pytest.raises(ValueError, match="^runtime_configuration_mismatch$"):
        _trigger(cloud)
    assert cloud.calls == ["job"]


def test_a_job_whose_annotation_does_not_bind_this_configuration_is_never_triggered():
    job = _job()
    job["template"]["annotations"] = {CONFIGURATION_ANNOTATION: "0" * 64}
    cloud = FakeCloud(job=job)
    with pytest.raises(ValueError, match="^runtime_configuration_unbound$"):
        _trigger(cloud)
    assert cloud.calls == ["job"]


def test_a_job_running_as_another_principal_is_never_triggered():
    job = _job()
    job["template"]["template"]["serviceAccount"] = SCHEDULER_ACCOUNT
    cloud = FakeCloud(job=job)
    with pytest.raises(ValueError, match="^principal_mismatch$"):
        _trigger(cloud)
    assert cloud.calls == ["job"]


def test_a_floating_image_tag_is_never_triggered():
    job = _job()
    job["template"]["template"]["containers"][0]["image"] = (
        "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine:latest"
    )
    cloud = FakeCloud(job=job)
    with pytest.raises(ValueError, match="^image_unbound$"):
        _trigger(cloud)
    assert cloud.calls == ["job"]


def test_an_absent_job_refuses_rather_than_creating_one():
    class Missing(FakeCloud):
        def __call__(self, request):
            if request["url"].endswith("jobs/intelligence-42-daily-staging"):
                self.calls.append("job")
                return self._json(404, {"error": "absent"})
            return super().__call__(request)

    with pytest.raises(ValueError, match="^runtime_job_absent$"):
        _trigger(Missing())


# Confirm


def test_the_confirmed_execution_carries_the_principal_and_the_bound_configuration():
    cloud = FakeCloud()
    receipt = _confirm(cloud, _trigger(cloud))
    assert receipt["status"] == "runtime_verified"
    assert receipt["principal"] == ORCHESTRATION
    assert receipt["credential_source"] == "attached_service_account"
    assert receipt["execution"] == EXECUTION
    assert receipt["configuration_sha256"] == CONFIG_SHA
    assert receipt["image"] == IMAGE
    assert receipt["operation_state"] == "succeeded"


def test_a_pending_operation_is_polled_to_its_terminal_state():
    cloud = FakeCloud(pending=2)
    receipt = _confirm(cloud, _trigger(cloud))
    assert receipt["status"] == "runtime_verified"
    assert cloud.calls.count("wait") == 3


def test_a_failed_operation_is_not_polled_into_a_verified_runtime():
    cloud = FakeCloud(failed={"code": 9, "message": "container failed"})
    with pytest.raises(ValueError, match="^execution_not_verified$"):
        _confirm(cloud, _trigger(cloud))
    assert cloud.calls.count("wait") == 1


def test_a_deadline_reached_before_a_terminal_state_is_unproven_not_verified():
    cloud = FakeCloud(pending=1000)
    with pytest.raises(ValueError, match="^execution_unproven$"):
        _confirm(cloud, _trigger(cloud), deadline_seconds=12)


def test_an_execution_of_another_job_is_refused():
    cloud = FakeCloud(execution=_execution(job=JOB_NAME.replace("daily", "price")))
    with pytest.raises(ValueError, match="^execution_job_mismatch$"):
        _confirm(cloud, _trigger(cloud))


def test_an_execution_that_did_not_complete_is_refused():
    cloud = FakeCloud(execution=_execution(succeededCount=0, failedCount=1))
    with pytest.raises(ValueError, match="^execution_not_verified$"):
        _confirm(cloud, _trigger(cloud))


def test_an_execution_whose_template_was_overridden_is_refused():
    execution = _execution()
    execution["template"]["containers"][0]["env"] = [{"name": "X", "value": "y"}]
    cloud = FakeCloud(execution=execution)
    with pytest.raises(ValueError, match="^runtime_override_forbidden$"):
        _confirm(cloud, _trigger(cloud))


def test_an_execution_run_by_another_principal_is_refused():
    execution = _execution()
    execution["template"]["serviceAccount"] = SCHEDULER_ACCOUNT
    cloud = FakeCloud(execution=execution)
    with pytest.raises(ValueError, match="^principal_mismatch$"):
        _confirm(cloud, _trigger(cloud))


# Handoff


def _unbound_readback():
    """A hand written document that asserts two paused schedulers and proves none."""
    return {
        "contract_version": schedulers.READBACK_CONTRACT,
        "state": "matched",
        "plan_sha256": "a" * 64,
        "schedulers": {
            DAILY_JOB_ID: {"result": "read", "state": "PAUSED", "differences": {}},
            PRICE_POLICY_JOB_ID: {
                "result": "read",
                "state": "PAUSED",
                "differences": {},
            },
        },
    }


class SchedulerCloud:
    """The Cloud Scheduler surface the readback reads: both jobs, paused."""

    def __init__(self, jobs):
        self.jobs = jobs

    def __call__(self, request):
        name = request["url"].split("/v1/", 1)[1]
        job = self.jobs.get(name)
        return {
            "status": 200 if job is not None else 404,
            "headers": {},
            "body": json.dumps(job or {"error": "absent"}).encode("utf-8"),
        }


def _scheduler_plan():
    """The plan the readback names, rendered the same way every time."""
    return schedulers.render_plan(now=lambda: "2026-09-18T00:00:00+00:00")


def _readback(plan=None):
    """The readback document the scheduler step actually writes, over a transport."""
    plan = plan or _scheduler_plan()
    jobs = {
        entry["name"]: {**entry["job"], "state": "PAUSED"}
        for entry in plan["schedulers"].values()
    }
    return schedulers.read_back(plan, adapter=_adapter(SchedulerCloud(jobs)))


def _written(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _handoff(cloud=None, *, verify=None, readback=None, plan=None):
    cloud = cloud or FakeCloud()
    verify = verify or _confirm(cloud, _trigger(cloud))
    return preflight.build_handoff(
        verify_receipt=verify,
        scheduler_plan=plan or _scheduler_plan(),
        scheduler_readback=_readback() if readback is None else readback,
        adapter=_adapter(cloud),
    )


def test_the_handoff_exports_the_manifest_principal_invoker_proof_and_receipt():
    package = _handoff()
    assert package["contract_version"] == preflight.HANDOFF_CONTRACT
    assert package["principal"] == ORCHESTRATION
    assert package["resource_manifest_sha256"] == CONFIG["resource_manifest_sha256"]
    assert package["daily_job_resource"].endswith("/jobs/" + DAILY_JOB_ID)
    assert package["verify_runtime_receipt_sha256"] == canonical_sha256(
        _confirm(FakeCloud(), _trigger(FakeCloud()))
    )
    assert package["schedulers"] == {
        DAILY_JOB_ID: "PAUSED",
        PRICE_POLICY_JOB_ID: "PAUSED",
    }
    assert package["invoker_proof"][DAILY_JOB_ID] == [
        {"member": "serviceAccount:" + SCHEDULER_ACCOUNT, "role": "roles/run.invoker"}
    ]
    assert package["handoff_sha256"] == preflight.verify_handoff(package)


def test_a_handoff_refuses_while_a_scheduler_is_not_paused():
    readback = _readback()
    readback["schedulers"][PRICE_POLICY_JOB_ID]["state"] = "ENABLED"
    readback["state"] = "drifted"
    with pytest.raises(ValueError, match="^scheduler_not_paused$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_scheduler_readback_that_drifted():
    readback = _readback()
    readback["schedulers"][DAILY_JOB_ID]["differences"] = {
        "schedule": {"expected": "0 3 * * *", "observed": "*/5 * * * *"}
    }
    readback["state"] = "drifted"
    with pytest.raises(ValueError, match="^scheduler_readback_drifted$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_receipt_that_is_not_a_verified_runtime():
    cloud = FakeCloud()
    verify = copy.deepcopy(_confirm(cloud, _trigger(cloud)))
    verify["status"] = "runtime_unproven"
    with pytest.raises(ValueError, match="^verify_receipt_unusable$"):
        _handoff(verify=verify)


def test_a_handoff_refuses_a_receipt_bound_to_another_manifest():
    cloud = FakeCloud()
    verify = copy.deepcopy(_confirm(cloud, _trigger(cloud)))
    verify["resource_manifest_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="^verify_receipt_unusable$"):
        _handoff(verify=verify)


def test_a_handoff_refuses_when_the_job_policy_does_not_carry_the_invoker_row():
    cloud = FakeCloud(policy={"bindings": []})
    with pytest.raises(ValueError, match="^invoker_binding_unproven$"):
        _handoff(cloud)


def test_a_handoff_refuses_when_the_job_policy_cannot_be_read():
    class NoPolicy(FakeCloud):
        def __call__(self, request):
            if request["url"].endswith(":getIamPolicy"):
                self.calls.append("policy")
                return self._json(404, {"error": "absent"})
            return super().__call__(request)

    with pytest.raises(ValueError, match="^invoker_binding_unproven$"):
        _handoff(NoPolicy())


def test_a_handoff_refuses_when_the_invoker_row_names_another_member():
    cloud = FakeCloud(policy=_policy(member=ORCHESTRATION))
    with pytest.raises(ValueError, match="^invoker_binding_unproven$"):
        _handoff(cloud)


def test_the_handoff_grants_no_release_or_activation_authority():
    package = _handoff()
    assert package["activation"] == {
        "daily_schedule": "paused, D04 activation gate under the C03 recurring grant",
        "price_policy_schedule": "paused, separate C04 renewal grant",
    }
    assert "grant" not in json.dumps(package["invoker_proof"])


# CLI


def test_the_cli_exposes_only_the_preflight_commands():
    parser = preflight._parser()
    actions = [
        action
        for action in parser._subparsers._actions
        if hasattr(action, "choices") and action.choices
    ]
    assert set(actions[0].choices) == {"trigger", "confirm", "handoff"}


# A caller supplied document never chooses a URL


FOREIGN_OPERATION = "projects/other-project/locations/us-central1/operations/op-1"


def test_a_trigger_receipt_whose_operation_left_this_project_is_never_read():
    cloud = FakeCloud()
    triggered = _trigger(cloud)
    triggered["operation"] = FOREIGN_OPERATION
    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        _confirm(cloud, triggered)
    assert "wait" not in cloud.calls


def test_a_trigger_receipt_whose_operation_carries_a_verb_is_never_read():
    cloud = FakeCloud()
    triggered = _trigger(cloud)
    triggered["operation"] = OPERATION + ":cancel#"
    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        _confirm(cloud, triggered)
    assert "wait" not in cloud.calls


def test_an_execution_name_outside_this_job_is_never_read():
    class Elsewhere(FakeCloud):
        def __call__(self, request):
            if request["url"].endswith(":wait"):
                self.calls.append("wait")
                return self._json(
                    200,
                    {
                        "name": OPERATION,
                        "done": True,
                        "response": {
                            "name": JOB_NAME.replace(
                                "ogilvy-trends-v2", "other-project"
                            )
                            + "/executions/victim"
                        },
                    },
                )
            return super().__call__(request)

    cloud = Elsewhere()
    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        _confirm(cloud, _trigger(cloud))
    assert "execution" not in cloud.calls


# The pinned image is re-proved wherever it is read


FLOATING = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine:latest"


def test_a_confirm_refuses_a_trigger_receipt_whose_image_is_not_digest_pinned():
    cloud = FakeCloud()
    triggered = _trigger(cloud)
    triggered["image"] = FLOATING
    with pytest.raises(ValueError, match="^image_unbound$"):
        _confirm(cloud, triggered)
    assert "wait" not in cloud.calls


@pytest.mark.parametrize(
    ("case", "image"),
    [
        ("short_digest", IMAGE[: IMAGE.index("@sha256:") + 8 + 8]),
        ("not_hexadecimal", IMAGE[: IMAGE.index("@sha256:") + 8] + "z" * 64),
        (
            "another_repository",
            "us-central1-docker.pkg.dev/other-project/intelligence-42/engine@sha256:"
            + "0" * 64,
        ),
    ],
)
def test_an_image_that_is_not_pinned_to_the_approved_repository_is_never_triggered(
    case, image
):
    job = _job()
    job["template"]["template"]["containers"][0]["image"] = image
    cloud = FakeCloud(job=job)
    with pytest.raises(ValueError, match="^image_unbound$"):
        _trigger(cloud)


def test_an_execution_that_ran_another_image_than_the_trigger_is_refused():
    execution = _execution()
    execution["template"]["containers"][0]["image"] = (
        IMAGE[: IMAGE.index("@sha256:") + 8] + "1" * 64
    )
    cloud = FakeCloud(execution=execution)
    with pytest.raises(ValueError, match="^runtime_override_forbidden$"):
        _confirm(cloud, _trigger(cloud))


def test_a_handoff_refuses_a_receipt_whose_image_is_not_digest_pinned():
    cloud = FakeCloud()
    verify = copy.deepcopy(_confirm(cloud, _trigger(cloud)))
    verify["image"] = FLOATING
    with pytest.raises(ValueError, match="^image_unbound$"):
        _handoff(verify=verify)


def test_a_handoff_refuses_a_receipt_whose_image_is_from_another_repository():
    cloud = FakeCloud()
    verify = copy.deepcopy(_confirm(cloud, _trigger(cloud)))
    verify["image"] = (
        "us-central1-docker.pkg.dev/other-project/intelligence-42/engine@sha256:"
        + "0" * 64
    )
    with pytest.raises(ValueError, match="^image_unbound$"):
        _handoff(verify=verify)


# The scheduler proof is bound to the plan and the receipt it claims


def test_a_handoff_refuses_a_hand_written_scheduler_readback():
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=_unbound_readback())


def test_a_handoff_refuses_a_scheduler_readback_bound_to_another_manifest():
    readback = _readback()
    readback["resource_manifest_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_scheduler_readback_bound_to_another_scheduler_file():
    readback = _readback()
    readback["scheduler_configuration_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_scheduler_readback_bound_to_another_runtime_file():
    readback = _readback()
    readback["runtime_configuration_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_scheduler_readback_whose_record_was_removed():
    readback = _readback()
    readback["record"] = []
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_scheduler_readback_whose_record_shows_a_running_job():
    readback = _readback()
    readback["record"][0]["response"]["state"] = "ENABLED"
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_scheduler_readback_whose_record_read_another_job():
    readback = _readback()
    entry = readback["record"][0]
    entry["url"] = entry["url"].replace("ogilvy-trends-v2", "other-project")
    entry["response"]["name"] = entry["response"]["name"].replace(
        "ogilvy-trends-v2", "other-project"
    )
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_scheduler_readback_that_covers_one_scheduler():
    readback = _readback()
    readback["schedulers"].pop(PRICE_POLICY_JOB_ID)
    with pytest.raises(ValueError, match="^scheduler_readback_unusable$"):
        _handoff(readback=readback)


def test_a_handoff_refuses_a_scheduler_entry_that_was_never_read():
    readback = _readback()
    readback["schedulers"][DAILY_JOB_ID] = {"result": "absent"}
    with pytest.raises(ValueError, match="^scheduler_readback_unusable$"):
        _handoff(readback=readback)


def test_the_handoff_names_the_scheduler_plan_it_was_proved_against():
    readback = _readback()
    package = _handoff(readback=readback)
    assert package["scheduler_plan_sha256"] == readback["plan_sha256"]
    assert package["scheduler_readback_sha256"] == canonical_sha256(readback)


# Guards the confirm step rests on


def test_a_document_that_is_not_a_trigger_receipt_is_never_confirmed():
    cloud = FakeCloud()
    triggered = _trigger(cloud)
    triggered["status"] = "refused"
    with pytest.raises(ValueError, match="^trigger_receipt_unusable$"):
        _confirm(cloud, triggered)
    assert "wait" not in cloud.calls


def test_a_trigger_receipt_bound_to_another_configuration_is_never_confirmed():
    cloud = FakeCloud()
    triggered = _trigger(cloud)
    triggered["configuration_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="^trigger_receipt_unusable$"):
        _confirm(cloud, triggered)
    assert "wait" not in cloud.calls


def test_an_execution_that_answers_with_another_identity_is_refused():
    cloud = FakeCloud(execution=_execution(name=EXECUTION + "-other"))
    with pytest.raises(ValueError, match="^execution_identity_mismatch$"):
        _confirm(cloud, _trigger(cloud))


def test_a_still_running_execution_is_not_verified():
    execution = _execution()
    execution.pop("completionTime")
    cloud = FakeCloud(execution=execution)
    with pytest.raises(ValueError, match="^execution_not_verified$"):
        _confirm(cloud, _trigger(cloud))


def test_an_execution_with_no_successful_task_is_not_verified():
    cloud = FakeCloud(execution=_execution(succeededCount=0))
    with pytest.raises(ValueError, match="^execution_not_verified$"):
        _confirm(cloud, _trigger(cloud))


# A server populated field is not an override


@pytest.mark.parametrize(
    ("case", "populate"),
    [
        (
            "container_name",
            lambda execution: execution["template"]["containers"][0].__setitem__(
                "name", "container-1"
            ),
        ),
        (
            "container_resources",
            lambda execution: execution["template"]["containers"][0].__setitem__(
                "resources", {"limits": {"cpu": "1000m", "memory": "2Gi"}}
            ),
        ),
        (
            "execution_environment",
            lambda execution: execution["template"].__setitem__(
                "executionEnvironment", "EXECUTION_ENVIRONMENT_GEN2"
            ),
        ),
        (
            "service_mesh",
            lambda execution: execution["template"].__setitem__("encryptionKey", ""),
        ),
    ],
)
def test_a_server_populated_execution_field_still_verifies_the_runtime(case, populate):
    execution = _execution()
    populate(execution)
    cloud = FakeCloud(execution=execution)
    receipt = _confirm(cloud, _trigger(cloud))
    assert receipt["status"] == "runtime_verified"


@pytest.mark.parametrize(
    ("case", "field", "value"),
    [
        ("another_contract", "contract_version", "42_other_trigger_v1"),
        ("another_mode", "mode", "daily"),
        ("another_job", "job_name", JOB_NAME.replace("daily", "price-policy")),
    ],
)
def test_a_trigger_receipt_of_another_shape_is_never_confirmed(case, field, value):
    cloud = FakeCloud()
    triggered = _trigger(cloud)
    triggered[field] = value
    with pytest.raises(ValueError, match="^trigger_receipt_unusable$"):
        _confirm(cloud, triggered)
    assert "wait" not in cloud.calls


# A native refusal ends the readback loop; it is never an unknown state


class Loopback:
    """One loopback endpoint, so the transport behaviour is proved on the wire."""

    def __init__(self, handler):
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                server.requests.append(
                    {
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                handler(self)

            do_GET = do_POST

            def log_message(self, *arguments):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *details):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.port}/v2/"


def _denied(handler):
    body = json.dumps({"error": {"code": 403, "message": "denied"}}).encode("utf-8")
    handler.send_response(403)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _live_adapter():
    return native.NativeAdapter("adc", token_source=lambda: "fake-token")


def _budget():
    """A clock and sleep that would allow a hundred and eighty reads of the deadline."""
    ticks = iter(range(0, 100000, 10))
    slept = []
    return (lambda: next(ticks)), slept.append, slept


def test_a_denied_operation_read_refuses_after_one_read_rather_than_polling(
    monkeypatch,
):
    with Loopback(_denied) as server:
        monkeypatch.setattr(native, "RUN_ENDPOINT", server.endpoint)
        adapter = _live_adapter()
        clock, sleep, slept = _budget()
        with pytest.raises(native.NativeError) as raised:
            wait_operation(
                preflight._operation_reader(adapter),
                OPERATION,
                deadline_seconds=1800,
                poll_seconds=10,
                clock=clock,
                sleep=sleep,
            )
        assert raised.value.status == 403
        assert len(server.requests) == 1
        assert slept == []


def _redirecting(target):
    def handler(request):
        request.send_response(302)
        request.send_header("Location", target)
        request.send_header("Content-Length", "0")
        request.end_headers()

    return handler


def test_a_redirected_operation_read_refuses_without_replaying_the_token(monkeypatch):
    with (
        Loopback(_denied) as elsewhere,
        Loopback(_redirecting(elsewhere.endpoint + "moved")) as server,
    ):
        monkeypatch.setattr(native, "RUN_ENDPOINT", server.endpoint)
        adapter = _live_adapter()
        clock, sleep, slept = _budget()
        with pytest.raises(native.NativeError) as raised:
            wait_operation(
                preflight._operation_reader(adapter),
                OPERATION,
                deadline_seconds=1800,
                poll_seconds=10,
                clock=clock,
                sleep=sleep,
            )
        assert raised.value.status == 302
        assert len(server.requests) == 1
        assert elsewhere.requests == []
        assert slept == []


def test_an_operation_read_answered_from_another_url_refuses_after_one_read():
    sent = []

    def transport(request):
        sent.append(request)
        return {
            "status": 200,
            "headers": {},
            "body": json.dumps({"name": OPERATION, "done": False}).encode("utf-8"),
            "url": request["url"] + "?moved",
        }

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    clock, sleep, slept = _budget()
    with pytest.raises(native.NativeError, match="redirect_refused"):
        wait_operation(
            preflight._operation_reader(adapter),
            OPERATION,
            deadline_seconds=1800,
            poll_seconds=10,
            clock=clock,
            sleep=sleep,
        )
    assert len(sent) == 1
    assert slept == []


def test_a_read_that_could_not_be_made_is_still_an_unknown_state():
    """A transport level failure is not an answer, so the loop may read again."""
    calls = []

    def reader(name, timeout_seconds):
        calls.append(name)
        raise ConnectionResetError("reset by peer")

    ticks = iter(range(0, 1000, 10))
    result = wait_operation(
        reader,
        "build-42",
        deadline_seconds=60,
        poll_seconds=10,
        clock=lambda: next(ticks),
        sleep=lambda seconds: None,
    )
    assert result["state"] == "unproven"
    assert result["last_state"] == "unknown"
    assert len(calls) >= 2


def test_an_answered_http_error_is_a_refusal_rather_than_an_unknown_state():
    calls = []

    def reader(name, timeout_seconds):
        calls.append(name)
        raise urllib.error.HTTPError(
            "https://run.googleapis.com/v2/x", 403, "Forbidden", {}, None
        )

    clock, sleep, slept = _budget()
    with pytest.raises(urllib.error.HTTPError):
        wait_operation(
            reader,
            "build-42",
            deadline_seconds=1800,
            poll_seconds=10,
            clock=clock,
            sleep=sleep,
        )
    assert calls == ["build-42"]
    assert slept == []


# The handoff verifies the plan the readback names, and its own digest


def test_a_handoff_refuses_a_readback_whose_plan_digest_names_another_plan():
    readback_document = _readback()
    readback_document["plan_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_readback_whose_plan_digest_is_not_a_digest():
    readback_document = _readback()
    readback_document["plan_sha256"] = "not-a-digest"
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_scheduler_plan_whose_own_seal_does_not_verify():
    plan = _scheduler_plan()
    plan["schedulers"][DAILY_JOB_ID]["job"]["schedule"] = "* * * * *"
    with pytest.raises(ValueError, match="^plan_digest_mismatch$"):
        _handoff(plan=plan)


def test_a_handoff_refuses_a_readback_document_of_another_shape():
    readback_document = _readback()
    readback_document["approved_by"] = "someone"
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_readback_that_dropped_a_bound_digest():
    readback_document = _readback()
    readback_document.pop("runtime_configuration_sha256")
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


@pytest.mark.parametrize(
    ("case", "record"),
    [
        ("an_object", {"call": "get_scheduler"}),
        ("a_string", "get_scheduler"),
        ("a_number", 42),
        ("a_null", None),
    ],
)
def test_a_handoff_refuses_a_readback_whose_record_is_not_a_list(case, record):
    readback_document = _readback()
    readback_document["record"] = record
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_readback_whose_record_entry_is_not_an_object():
    readback_document = _readback()
    readback_document["record"][0] = "get_scheduler"
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_readback_whose_record_holds_another_call():
    readback_document = _readback()
    readback_document["record"][0]["call"] = "pause_scheduler"
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_readback_whose_record_was_not_a_successful_read():
    readback_document = _readback()
    readback_document["record"][0]["status"] = 404
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_readback_whose_recorded_response_is_not_an_object():
    readback_document = _readback()
    readback_document["record"][0]["response"] = "PAUSED"
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_readback_whose_recorded_name_is_not_a_string():
    readback_document = _readback()
    readback_document["record"][0]["response"]["name"] = 42
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_recorded_read_whose_url_left_the_name_it_reports():
    """The url to name binding, with both pinned names still present in the set."""
    readback_document = _readback()
    entry = readback_document["record"][0]
    entry["url"] = entry["url"] + "?alt=media"
    with pytest.raises(ValueError, match="^scheduler_readback_unbound$"):
        _handoff(readback=readback_document)


def test_a_handoff_refuses_a_readback_that_did_not_claim_a_match():
    readback_document = _readback()
    readback_document["state"] = "drifted"
    with pytest.raises(ValueError, match="^scheduler_readback_drifted$"):
        _handoff(readback=readback_document)


def test_the_handoff_command_verifies_its_own_digest_before_it_writes(
    tmp_path, monkeypatch
):
    """verify_handoff is a production caller, not a test only helper."""
    package = _handoff()
    package["handoff_sha256"] = "f" * 64
    monkeypatch.setattr(preflight, "build_handoff", lambda **kwargs: package)
    output = tmp_path / "handoff.json"
    written = []
    code = preflight.main(
        [
            "handoff",
            "--verify",
            str(
                _written(
                    tmp_path / "verify.json",
                    _confirm(FakeCloud(), _trigger(FakeCloud())),
                )
            ),
            "--scheduler-plan",
            str(_written(tmp_path / "plan.json", _scheduler_plan())),
            "--scheduler-readback",
            str(_written(tmp_path / "readback.json", _readback())),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(FakeCloud()),
        out=type("Stream", (), {"write": lambda self, text: written.append(text)})(),
    )
    assert code == 1
    assert json.loads(written[0]) == {"status": "refused", "error": "handoff_invalid"}
    assert not output.exists()


def test_the_handoff_command_writes_a_package_that_verifies(tmp_path):
    output = tmp_path / "handoff.json"
    written = []
    code = preflight.main(
        [
            "handoff",
            "--verify",
            str(
                _written(
                    tmp_path / "verify.json",
                    _confirm(FakeCloud(), _trigger(FakeCloud())),
                )
            ),
            "--scheduler-plan",
            str(_written(tmp_path / "plan.json", _scheduler_plan())),
            "--scheduler-readback",
            str(_written(tmp_path / "readback.json", _readback())),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(FakeCloud()),
        out=type("Stream", (), {"write": lambda self, text: written.append(text)})(),
    )
    assert code == 0
    package = json.loads(output.read_text(encoding="utf-8"))
    assert package["handoff_sha256"] == preflight.verify_handoff(package)


# The handoff binds the plan it names to the reviewed files, not only its shape


def _resealed_plan(mutate):
    plan = _scheduler_plan()
    mutate(plan)
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return {**body, "plan_sha256": canonical_sha256(body)}


def test_a_handoff_refuses_a_scheduler_plan_whose_digests_name_no_file_on_disk():
    """A sealed plan carrying digests no reviewed file has could never apply, so a
    readback naming it proves nothing about this runtime."""
    plan = _resealed_plan(
        lambda value: value.__setitem__("runtime_configuration_sha256", "8" * 64)
    )
    readback = _readback()
    readback["plan_sha256"] = plan["plan_sha256"]
    with pytest.raises(ValueError, match="^runtime_configuration_digest_mismatch$"):
        _handoff(plan=plan, readback=readback)


def test_a_handoff_refuses_a_scheduler_plan_bound_to_another_scheduler_file():
    plan = _resealed_plan(
        lambda value: value.__setitem__("scheduler_configuration_sha256", "8" * 64)
    )
    readback = _readback()
    readback["plan_sha256"] = plan["plan_sha256"]
    with pytest.raises(ValueError, match="^scheduler_configuration_digest_mismatch$"):
        _handoff(plan=plan, readback=readback)


def test_the_handoff_command_verifies_the_digest_over_the_bytes_it_will_write(
    tmp_path, monkeypatch
):
    """The producer check reads the package back the way a consumer will, so a
    package that does not survive its own canonical encoding is refused."""
    package = _handoff()
    body = {
        **{key: value for key, value in package.items() if key != "handoff_sha256"},
        "daily_job_actions": ("invoke", "read"),
    }
    package = {**body, "handoff_sha256": canonical_sha256(body)}
    assert package["handoff_sha256"] == preflight.verify_handoff(package)
    monkeypatch.setattr(preflight, "build_handoff", lambda **kwargs: package)
    output = tmp_path / "handoff.json"
    written = []
    code = preflight.main(
        [
            "handoff",
            "--verify",
            str(
                _written(
                    tmp_path / "verify.json",
                    _confirm(FakeCloud(), _trigger(FakeCloud())),
                )
            ),
            "--scheduler-plan",
            str(_written(tmp_path / "plan.json", _scheduler_plan())),
            "--scheduler-readback",
            str(_written(tmp_path / "readback.json", _readback())),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(FakeCloud()),
        out=type("Stream", (), {"write": lambda self, text: written.append(text)})(),
    )
    assert code == 1
    assert json.loads(written[0]) == {"status": "refused", "error": "handoff_invalid"}
    assert not output.exists()


# The preflight owes an artifact once a request has gone out


def _read_request(connection):
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
    """A loopback endpoint that answers a fixed list and then stops answering."""

    def __init__(self, answers, *, path="/v2/"):
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


def _stream():
    written = []
    return written, type(
        "Stream", (), {"write": lambda self, text: written.append(text)}
    )()


def test_a_trigger_that_dies_after_the_run_request_still_writes_what_went_out(
    tmp_path, monkeypatch
):
    """The empty RunJobRequest left the client, so the refusal owes an artifact."""
    output = tmp_path / "trigger.json"
    written, stream = _stream()
    with DyingLoopback([(200, _job())]) as server:
        monkeypatch.setattr(native, "RUN_ENDPOINT", server.endpoint)
        code = preflight.main(
            ["trigger", "--output", str(output)],
            adapter_factory=lambda state: native.NativeAdapter(
                state, token_source=lambda: "fake-token"
            ),
            out=stream,
        )
        assert len(server.requests) == 2, "the run request left the client"
    assert code == 1
    line = json.loads(written[0])
    assert line["status"] == "refused"
    assert line["receipt"] == str(output)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["state"] == "refused"
    assert document["command"] == "trigger"
    assert document["sent"] == ["run_job"]
    assert [row["call"] for row in document["record"]] == ["get_job", "run_job"]


def test_the_trigger_refuses_an_output_that_already_exists_before_it_sends_anything(
    tmp_path,
):
    output = tmp_path / "trigger.json"
    output.write_text("operator notes\n", encoding="utf-8")
    cloud = FakeCloud()
    written, stream = _stream()
    code = preflight.main(
        ["trigger", "--output", str(output)],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {"status": "refused", "error": "output_exists"}
    assert cloud.calls == [], "nothing is sent while the receipt has nowhere to go"
    assert output.read_text(encoding="utf-8") == "operator notes\n"


def test_the_trigger_refuses_an_output_directory_that_is_absent_before_it_sends(
    tmp_path,
):
    cloud = FakeCloud()
    written, stream = _stream()
    code = preflight.main(
        ["trigger", "--output", str(tmp_path / "absent" / "trigger.json")],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {
        "status": "refused",
        "error": "output_directory_absent",
    }
    assert cloud.calls == []


def test_a_handoff_refused_after_only_reads_writes_no_receipt(tmp_path):
    """Reads owe nothing: only a request that changed something does."""
    output = tmp_path / "handoff.json"
    written, stream = _stream()
    readback = _readback()
    readback["state"] = "drifted"
    code = preflight.main(
        [
            "handoff",
            "--verify",
            str(
                _written(
                    tmp_path / "verify.json",
                    _confirm(FakeCloud(), _trigger(FakeCloud())),
                )
            ),
            "--scheduler-plan",
            str(_written(tmp_path / "plan.json", _scheduler_plan())),
            "--scheduler-readback",
            str(_written(tmp_path / "readback.json", readback)),
            "--output",
            str(output),
        ],
        adapter_factory=lambda state: _adapter(FakeCloud()),
        out=stream,
    )
    assert code == 1
    assert json.loads(written[0]) == {
        "status": "refused",
        "error": "scheduler_readback_drifted",
    }
    assert not output.exists()


# A trigger receipt is written, written beside the output, or printed whole


class Blocking(FakeCloud):
    """A surface that fills the output path while the requests are in flight."""

    def __init__(self, output, **keywords):
        super().__init__(**keywords)
        self.output = output

    def __call__(self, request):
        response = super().__call__(request)
        self.output.mkdir(exist_ok=True)
        return response


class Vanishing(FakeCloud):
    """A surface that removes the output directory after every answer it gives."""

    def __init__(self, directory, *, nameless_run=False, **keywords):
        super().__init__(**keywords)
        self.directory = directory
        self.nameless_run = nameless_run

    def __call__(self, request):
        response = super().__call__(request)
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)
        if self.nameless_run and self.calls[-1] == "run":
            return self._json(200, {})
        return response


def _trigger_main(output, cloud):
    written, stream = _stream()
    code = preflight.main(
        ["trigger", "--output", str(output)],
        adapter_factory=lambda state: _adapter(cloud),
        out=stream,
    )
    return code, written


def test_a_trigger_receipt_that_cannot_reach_its_path_lands_beside_it(tmp_path):
    """Only the requested path fails, so the fallback holds the receipt."""
    output = tmp_path / "trigger.json"
    cloud = Blocking(output)
    code, written = _trigger_main(output, cloud)
    assert "run" in cloud.calls
    assert code == 0
    line = json.loads(written[0])
    assert line["status"] == "written"
    assert line["output"] != str(output)
    fallback = Path(line["output"])
    assert fallback.parent == output.parent
    expected = _trigger(FakeCloud())
    digest = schedulers.canonical_sha256(expected)
    assert fallback.name == f"trigger.unwritten-{digest[:16]}.json"
    document = json.loads(fallback.read_text(encoding="utf-8"))
    assert document == expected
    assert output.is_dir(), "the blocked path was not overwritten"


def test_a_trigger_receipt_neither_path_can_hold_is_printed_whole(tmp_path):
    """The run request went out and both paths fail, so the receipt goes to stdout."""
    directory = tmp_path / "out"
    directory.mkdir()
    cloud = Vanishing(directory)
    code, written = _trigger_main(directory / "trigger.json", cloud)
    assert "run" in cloud.calls
    assert code == 1
    assert len(written) == 1
    line = json.loads(written[0])
    assert line["status"] == "refused"
    assert "receipt" not in line
    assert line["receipt_unwritten"]
    expected = _trigger(FakeCloud())
    assert expected["status"] == "triggered"
    assert line["receipt_document"] == expected
    assert not directory.exists()


def test_a_trigger_refusal_receipt_neither_path_can_hold_is_printed_whole(tmp_path):
    directory = tmp_path / "out"
    directory.mkdir()
    cloud = Vanishing(directory, nameless_run=True)
    code, written = _trigger_main(directory / "trigger.json", cloud)
    assert code == 1
    assert len(written) == 1
    line = json.loads(written[0])
    assert line["status"] == "refused"
    assert line["error"] == "operation_name_missing"
    assert "receipt" not in line
    assert line["receipt_unwritten"]
    document = line["receipt_document"]
    assert document["state"] == "refused"
    assert document["command"] == "trigger"
    assert document["sent"] == ["run_job"]
    assert [row["call"] for row in document["record"]] == ["get_job", "run_job"]
