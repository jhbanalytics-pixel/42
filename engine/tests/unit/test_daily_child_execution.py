import copy
import inspect
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from src.analysis.open_intelligence import daily_child_execution as subject
from src.analysis.open_intelligence import daily_cycle
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_execution_authority import (
    DailyAuthorityIntegrityError,
    consume_daily_derivation,
)

from tests.unit.test_daily_derivation_authority import (
    CUTOFF,
    DAILY_JOB,
    IMAGE,
    JOB_POLICY,
    LEASE,
    NOW,
    ORCHESTRATION,
    authority,
    frame,
    grant,
)

GRANT_DIGEST = sha256(canonical_bytes(grant())).hexdigest()
EXECUTION_ID = "intelligence-42-daily-staging-child1"
EXECUTION = DAILY_JOB + "/executions/" + EXECUTION_ID
OPERATION = "projects/ogilvy-trends-v2/locations/us-central1/operations/op-1"
CREATED = NOW + timedelta(minutes=1)
STARTED = CREATED + timedelta(seconds=20)


def job_resource(**changes):
    value = {
        "name": DAILY_JOB,
        "template": {
            "annotations": {
                "42.ogilvy/runtime-configuration-sha256": JOB_POLICY,
                "42.ogilvy/recurring-grant-sha256": GRANT_DIGEST,
            },
            "taskCount": 1,
            "parallelism": 1,
            "template": {
                "serviceAccount": ORCHESTRATION,
                "maxRetries": 0,
                "timeout": "3600s",
                "containers": [
                    {
                        "image": IMAGE,
                        "command": ["python"],
                        "args": ["-m", "ops.runners.managed_runtime"],
                        "env": [
                            {"name": "TRENDS_ENV", "value": "staging"},
                            {
                                "name": "BIGQUERY_DATASET",
                                "value": "intelligence_42_sources_staging",
                            },
                        ],
                    }
                ],
            },
        },
    }
    value.update(changes)
    return value


def execution_resource(step="6", *, job=None, created=CREATED, started=STARTED, **changes):
    job = job or job_resource()
    task = copy.deepcopy(job["template"]["template"])
    task["containers"][0]["env"].append({"name": "DAILY_STEP_NUMBER", "value": step})
    value = {
        "name": EXECUTION,
        "job": "intelligence-42-daily-staging",
        "annotations": dict(job["template"]["annotations"]),
        "taskCount": 1,
        "parallelism": 1,
        "template": task,
        "createTime": created.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z",
        "startTime": started.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
    }
    value.update(changes)
    return value


RELEASE_STEP = "6"


def env(step=RELEASE_STEP, **changes):
    value = {"DAILY_STEP_NUMBER": step, "CLOUD_RUN_EXECUTION": EXECUTION_ID}
    value.update(changes)
    return value


# the run request changes the step number and nothing else (Albert, 23 September 08:44)


def test_the_run_request_overrides_only_the_step_number():
    assert subject.child_run_request("release") == {
        "overrides": {
            "containerOverrides": [{"env": [{"name": "DAILY_STEP_NUMBER", "value": "6"}]}]
        }
    }


def test_each_stage_has_its_position_in_the_daily_order_as_its_step_number():
    assert [subject.step_number(stage) for stage in daily_cycle.STAGE_ORDER_V2] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
    ]
    assert [subject.stage_for_step(str(n)) for n in range(1, 7)] == list(daily_cycle.STAGE_ORDER_V2)


@pytest.mark.parametrize("stage", ["collect", "", None, 6])
def test_a_run_request_for_an_unknown_stage_refuses(stage):
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_step_invalid$"):
        subject.child_run_request(stage)


# child admission: the execution equals the job template but for that one variable


def test_a_child_execution_admits_its_step():
    assert (
        subject.admit_child_execution(
            execution=execution_resource(), job=job_resource(), environment=env()
        )
        == "release"
    )


def test_an_execution_without_the_override_is_a_parent_not_a_child():
    execution = execution_resource()
    execution["template"] = job_resource()["template"]["template"]
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_derivation_missing$"):
        subject.child_override_of(execution=execution, job=job_resource())


def mutate(path, value):
    def apply(execution):
        target = execution
        for key in path[:-1]:
            target = target[key]
        if value is KeyError:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return execution

    return apply


@pytest.mark.parametrize(
    "change",
    [
        mutate(("template", "containers", 0, "args"), ["-m", "ops.runners.other"]),
        mutate(("template", "containers", 0, "command"), ["sh"]),
        mutate(("template", "containers", 0, "image"), IMAGE[:-1] + "0"),
        mutate(("template", "serviceAccount"), "other@ogilvy-trends-v2.iam.gserviceaccount.com"),
        mutate(("template", "timeout"), "7200s"),
        mutate(("template", "maxRetries"), 3),
        mutate(("taskCount",), 2),
        mutate(("parallelism",), 2),
        mutate(("annotations",), {"42.ogilvy/runtime-configuration-sha256": JOB_POLICY}),
        mutate(("template", "containers", 0, "env", 0), {"name": "TRENDS_ENV", "value": "prod"}),
        mutate(("template", "containers", 0, "env", 1), KeyError),
        mutate(("template", "containers", 0, "resources"), {"limits": {"cpu": "8"}}),
        mutate(("template", "volumes"), [{"name": "extra"}]),
        mutate(("job",), "intelligence-42-price-policy-staging"),
    ],
)
def test_any_other_difference_from_the_job_template_refuses(change):
    execution = change(execution_resource())
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_override_forbidden$"):
        subject.child_override_of(execution=execution, job=job_resource())


def test_a_second_override_variable_refuses():
    execution = execution_resource()
    execution["template"]["containers"][0]["env"].append(
        {"name": "COLLECTION_POLICY_SHA256", "value": "d" * 64}
    )
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_override_forbidden$"):
        subject.child_override_of(execution=execution, job=job_resource())


def test_a_repeated_or_secret_sourced_derivation_variable_refuses():
    execution = execution_resource()
    execution["template"]["containers"][0]["env"].append(
        {"name": "DAILY_STEP_NUMBER", "value": RELEASE_STEP}
    )
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_override_forbidden$"):
        subject.child_override_of(execution=execution, job=job_resource())
    execution = execution_resource()
    execution["template"]["containers"][0]["env"][-1] = {
        "name": "DAILY_STEP_NUMBER",
        "valueSource": {"secretKeyRef": {"secret": "s", "version": "1"}},
    }
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_override_forbidden$"):
        subject.child_override_of(execution=execution, job=job_resource())


def test_a_job_template_that_already_carries_the_variable_refuses():
    job = job_resource()
    job["template"]["template"]["containers"][0]["env"].append(
        {"name": "DAILY_STEP_NUMBER", "value": RELEASE_STEP}
    )
    execution = execution_resource(job=job)
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_override_forbidden$"):
        subject.child_override_of(execution=execution, job=job)


@pytest.mark.parametrize("step", ["0", "7", "06", " 6", "release", "exd_" + "c" * 64])
def test_a_malformed_step_value_refuses(step):
    execution = execution_resource(step)
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_step_invalid$"):
        subject.child_override_of(execution=execution, job=job_resource())


@pytest.mark.parametrize(
    "environment",
    [
        env(DAILY_STEP_NUMBER="5"),
        env(CLOUD_RUN_EXECUTION="intelligence-42-daily-staging-other"),
        {"CLOUD_RUN_EXECUTION": EXECUTION_ID},
    ],
)
def test_the_process_environment_must_name_the_same_execution_and_step(environment):
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_identity_mismatch$"):
        subject.admit_child_execution(
            execution=execution_resource(), job=job_resource(), environment=environment
        )


# native views the consume path compares against the operation context


def test_the_native_job_view_names_the_bindings_the_context_carries():
    assert subject.native_job_view(job_resource()) == {
        "job_resource": DAILY_JOB,
        "image_uri": IMAGE,
        "service_identity": ORCHESTRATION,
        "job_policy_digest": JOB_POLICY,
        "grant_digest": GRANT_DIGEST,
    }


def test_the_native_execution_view_normalises_provider_instants():
    view = subject.native_execution_view(execution_resource())
    assert view == {
        "execution_name": EXECUTION,
        "job_resource": DAILY_JOB,
        "image_uri": IMAGE,
        "service_identity": ORCHESTRATION,
        "job_policy_digest": JOB_POLICY,
        "grant_digest": GRANT_DIGEST,
        "execution_created_at": CREATED.isoformat(timespec="microseconds"),
        "execution_started_at": STARTED.isoformat(timespec="microseconds"),
        "step_number": RELEASE_STEP,
    }


def _without_step(execution):
    env_list = execution["template"]["containers"][0]["env"]
    env_list[:] = [item for item in env_list if item.get("name") != "DAILY_STEP_NUMBER"]
    return execution


def _second_step(execution):
    execution["template"]["containers"][0]["env"].append(
        {"name": "DAILY_STEP_NUMBER", "value": "5"}
    )
    return execution


def _secret_step(execution):
    execution["template"]["containers"][0]["env"][-1] = {
        "name": "DAILY_STEP_NUMBER",
        "valueSource": {"secretKeyRef": {"secret": "s", "version": "1"}},
    }
    return execution


@pytest.mark.parametrize(
    "change",
    [
        _without_step,
        _second_step,
        _secret_step,
        lambda execution: execution_resource("7"),
        lambda execution: execution_resource("exd_" + "c" * 64),
    ],
)
def test_the_native_execution_view_names_exactly_one_step(change):
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_native_execution_invalid$"):
        subject.native_execution_view(change(execution_resource()))


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"startTime": None}, "daily_native_execution_not_started"),
        ({"createTime": "2026-09-23 06:00:00"}, "daily_native_execution_invalid"),
        ({"annotations": {}}, "daily_native_execution_invalid"),
        ({"name": DAILY_JOB + "/executions/"}, "daily_native_execution_invalid"),
    ],
)
def test_an_incomplete_native_execution_refuses(change, code):
    execution = execution_resource()
    for key, value in change.items():
        if value is None:
            del execution[key]
        else:
            execution[key] = value
    with pytest.raises(subject.DailyChildRefusal, match=rf"^{code}$"):
        subject.native_execution_view(execution)


# terminal facts: read from the Cloud Run v2 Execution fields the transport returns

COMPLETION = (STARTED + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "789Z"


def completed_resource(state="succeeded", **changes):
    """An execution as the Cloud Run v2 API reports it once it completed."""
    condition = {"type": "Completed", "lastTransitionTime": COMPLETION}
    counts = {}
    if state == "succeeded":
        condition.update(state="CONDITION_SUCCEEDED", message="Execution completed successfully.")
        counts["succeededCount"] = 1
    elif state == "failed":
        condition.update(state="CONDITION_FAILED", executionReason="NON_ZERO_EXIT_CODE")
        counts["failedCount"] = 1
    else:
        condition.update(state="CONDITION_FAILED", executionReason="CANCELLED")
        counts["cancelledCount"] = 1
    value = execution_resource(
        completionTime=COMPLETION,
        conditions=[
            {"type": "ResourcesAvailable", "state": "CONDITION_SUCCEEDED"},
            {"type": "Started", "state": "CONDITION_SUCCEEDED"},
            condition,
        ],
        **counts,
    )
    value.update(changes)
    return value


def test_a_running_execution_view_carries_no_terminal_state():
    view = subject.native_execution_view(execution_resource(runningCount=1, reconciling=True))
    assert "terminal_state" not in view
    assert "completed_at" not in view


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_a_completed_execution_view_names_its_terminal_state_and_completion(state):
    view = subject.native_execution_view(completed_resource(state))
    assert view["terminal_state"] == state
    assert view["completed_at"] == COMPLETION
    assert view["step_number"] == RELEASE_STEP


def _completed_condition(**fields):
    execution = completed_resource()
    execution["conditions"][-1].update(fields)
    return execution


def test_a_cancel_reason_is_cancelled_even_when_no_task_counts_as_cancelled():
    execution = completed_resource("failed", failedCount=0)
    execution["conditions"][-1]["executionReason"] = "CANCELLING"
    assert subject.native_execution_view(execution)["terminal_state"] == "cancelled"


def test_a_cancelled_task_is_cancelled_even_without_a_cancel_reason():
    execution = completed_resource("cancelled")
    del execution["conditions"][-1]["executionReason"]
    assert subject.native_execution_view(execution)["terminal_state"] == "cancelled"


def _three_task_job():
    job = job_resource()
    job["template"]["taskCount"] = 3
    return job


@pytest.mark.parametrize(("succeeded", "state"), [(3, "succeeded"), (2, None), (1, None)])
def test_a_succeeded_condition_is_success_only_when_every_task_of_the_job_succeeded(
    succeeded, state
):
    """The execution's task count is the job's: admission refuses any other count."""
    job = _three_task_job()
    execution = completed_resource(taskCount=3, succeededCount=succeeded)
    execution["template"] = execution_resource(job=job)["template"]
    assert subject.admit_child_execution(execution=execution, job=job, environment=env()) == (
        "release"
    )
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_override_forbidden$"):
        subject.child_override_of(execution=dict(execution, taskCount=1), job=job)
    if state is None:
        with pytest.raises(
            subject.DailyChildRefusal, match=r"^daily_native_execution_terminal_ambiguous$"
        ):
            subject.native_execution_view(execution)
    else:
        assert subject.native_execution_view(execution)["terminal_state"] == state


@pytest.mark.parametrize("before", [timedelta(seconds=1), timedelta(microseconds=1)])
def test_a_completion_before_the_start_refuses(before):
    completion = (STARTED - before).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    execution = completed_resource(completionTime=completion)
    with pytest.raises(
        subject.DailyChildRefusal, match=r"^daily_native_execution_terminal_ambiguous$"
    ):
        subject.native_execution_view(execution)


def test_a_completion_at_the_start_is_accepted():
    completion = STARTED.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    view = subject.native_execution_view(completed_resource(completionTime=completion))
    assert view["completed_at"] == completion


def test_completed_at_is_the_provider_completion_time_not_a_condition_transition():
    execution = completed_resource()
    for condition in execution["conditions"]:
        condition["lastTransitionTime"] = "2026-09-23T06:30:00.000001Z"
    view = subject.native_execution_view(execution)
    assert view["completed_at"] == COMPLETION
    assert view["completed_at"] == execution["completionTime"]


@pytest.mark.parametrize(
    "execution",
    [
        completed_resource(conditions=[]),
        completed_resource(conditions=None),
        completed_resource(conditions=["Completed"]),
        completed_resource(conditions=[["Completed", "CONDITION_SUCCEEDED"]]),
        completed_resource(conditions=1),
        completed_resource(
            conditions=[
                {"type": "Completed", "state": "CONDITION_SUCCEEDED"},
                {"type": "Completed", "state": "CONDITION_SUCCEEDED"},
            ]
        ),
        _completed_condition(state="CONDITION_PENDING"),
        _completed_condition(state="CONDITION_RECONCILING"),
        _completed_condition(executionReason="NON_ZERO_EXIT_CODE"),
        completed_resource(succeededCount=0),
        completed_resource(failedCount=1),
        completed_resource(succeededCount="1"),
        completed_resource(succeededCount=True),
        completed_resource(succeededCount=-1),
        completed_resource(failedCount=-1),
        completed_resource(taskCount=0, succeededCount=0),
        completed_resource(taskCount=None),
        completed_resource(taskCount=0),
        completed_resource("failed", succeededCount=1),
        completed_resource("failed", succeededCount=1, failedCount=0),
        completed_resource(completionTime=None),
        completed_resource(completionTime=""),
        completed_resource(completionTime="2026-09-23 06:05:00"),
        completed_resource(completionTime="2026-09-23T08:05:00+02:00"),
    ],
)
def test_a_completed_execution_whose_terminal_facts_disagree_refuses(execution):
    with pytest.raises(
        subject.DailyChildRefusal, match=r"^daily_native_execution_terminal_ambiguous$"
    ):
        subject.native_execution_view(execution)


def test_a_job_without_the_grant_annotation_is_unbound():
    job = job_resource()
    del job["template"]["annotations"]["42.ogilvy/recurring-grant-sha256"]
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_native_job_unbound$"):
        subject.native_job_view(job)


# parent dispatch


class Runs:
    def __init__(self, *, executions, operation=None, run_error=None):
        self.executions = list(executions)
        self.operation = operation or {"name": OPERATION, "metadata": {"name": EXECUTION}}
        self.run_error = run_error
        self.requests = []
        self.reads = 0

    def run_job(self, job, body):
        self.requests.append((job, body))
        if self.run_error:
            raise self.run_error
        return self.operation

    def read_execution(self, name):
        assert name == EXECUTION
        self.reads += 1
        return self.executions[min(self.reads, len(self.executions)) - 1]

    def read_raw_job(self, job):
        assert job == DAILY_JOB
        return job_resource()


class Store:
    def __init__(self):
        self.bound = []

    def bind_dispatch_response_v2(self, operation_id, reference):
        self.bound.append((operation_id, reference))


def receipt(derivation_id="exd_" + "c" * 64, job=DAILY_JOB):
    return {"derivation_id": derivation_id, "child_job_resource": job}


def finished(state):
    succeeded = 1 if state == "succeeded" else 0
    return execution_resource(
        completionTime=(STARTED + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        succeededCount=succeeded,
        failedCount=1 - succeeded,
    )


def dispatcher(runs, store=None, polls=3):
    return subject.ChildDispatcher(
        store=store or Store(), runs=runs, polls=polls, sleep=lambda seconds: None
    )


@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_the_parent_runs_the_child_with_the_override_and_waits_for_its_terminal(state):
    runs = Runs(executions=[execution_resource(), finished(state)])
    store = Store()
    observed = dispatcher(runs, store)(manifest=frame("release"), authority_receipt=receipt())
    assert observed == {"state": state, "execution_name": EXECUTION}
    assert runs.requests == [(DAILY_JOB, subject.child_run_request("release"))]
    assert store.bound == [(frame("release")["operation_id"], OPERATION)]
    assert runs.reads == 2


def test_an_unanswered_run_request_is_unknown_and_binds_nothing():
    store = Store()
    runs = Runs(executions=[], run_error=ConnectionError("reset"))
    assert dispatcher(runs, store)(manifest=frame("release"), authority_receipt=receipt()) == {
        "state": "unknown"
    }
    assert store.bound == []


def test_a_child_still_running_after_the_poll_budget_is_pending():
    runs = Runs(executions=[execution_resource()])
    observed = dispatcher(runs, polls=2)(manifest=frame("release"), authority_receipt=receipt())
    assert observed == {"state": "pending", "execution_name": EXECUTION}


def test_an_execution_carrying_another_step_refuses():
    runs = Runs(executions=[execution_resource("5")])
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_execution_mismatch$"):
        dispatcher(runs)(manifest=frame("release"), authority_receipt=receipt())


@pytest.mark.parametrize(
    "operation",
    [
        {"name": OPERATION, "metadata": {"name": "projects/x/jobs/other/executions/e"}},
        {"name": "operations/op-1", "metadata": {"name": EXECUTION}},
        {"name": OPERATION},
    ],
)
def test_a_malformed_provider_operation_is_unknown(operation):
    store = Store()
    runs = Runs(executions=[], operation=operation)
    assert dispatcher(runs, store)(manifest=frame("release"), authority_receipt=receipt()) == {
        "state": "unknown"
    }
    assert store.bound == []


def test_the_receipt_must_name_the_stage_job():
    runs = Runs(executions=[])
    retired = DAILY_JOB.replace("daily-staging", "daily-release-staging")
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_job_mismatch$"):
        dispatcher(runs)(manifest=frame("release"), authority_receipt=receipt(job=retired))
    assert runs.requests == []


def test_the_parent_binds_the_dispatch_response_through_the_real_store():
    subject_authority, _derive, store, _objects = authority()
    slot = frame("release")["operation_id"]
    assert store.claim_v2(slot, "staging", "intelligence-42-core", CUTOFF)
    prepared = subject_authority.prepare(
        "release", frame("release"), lease=store.current_lease_v2(slot)
    )
    store.prepare_intent_v2(slot, prepared["intent"])
    derived = subject_authority.issue("release", prepared)
    store.bind_derivation_v2(slot, derived)
    store.mark_dispatch_started_v2(slot)
    runs = Runs(executions=[execution_resource()])
    observed = dispatcher(runs, store, polls=1)(
        manifest=frame("release"), authority_receipt=derived
    )
    assert observed["state"] == "pending"
    intent = store.read_intent_v2(slot)
    assert intent["phase"] == "dispatch_observed"
    assert intent["dispatch_observation_reference"] == OPERATION


# the child consumes its own derivation, runs its stage and records the result


class Natives:
    """The child's raw Cloud Run reads: its own execution and the daily job."""

    def __init__(self, execution, job=None):
        self.execution = execution
        self.job = job or job_resource()
        self.reads = []

    def read_execution(self, name):
        self.reads.append(name)
        assert name == self.execution["name"]
        return copy.deepcopy(self.execution)

    def read_raw_job(self, name):
        self.reads.append(name)
        assert name == DAILY_JOB
        return copy.deepcopy(self.job)


class Writes:
    """The child's write clients: its own native execution and the select routine."""

    def __init__(self, execution, row):
        self.execution = execution
        self.row = row
        self.selections = []

    def read_native_execution(self, name):
        assert name == EXECUTION
        return subject.native_execution_view(self.execution)

    def select_derivation(self, *parameters):
        self.selections.append(parameters)
        return self.row


def derivation_row(prepared, derived, **changes):
    row = dict(
        derived,
        canonical_operation_context_json=prepared["canonical_operation_context_json"],
        operation_context_sha256=prepared["intent"]["operation_context_sha256"],
    )
    row.update(changes)
    return row


def issued():
    subject_authority, _derive, _store, objects = authority()
    prepared = subject_authority.prepare("release", frame("release"), lease=LEASE)
    derived = subject_authority.issue("release", prepared)
    return prepared, dict(derived), objects


class Consumed:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return FakeConsumption(kwargs["derivation_id"], kwargs["execution_name"])


class FakeConsumption:
    def __init__(self, derivation_id, execution_name):
        self.derivation_id = derivation_id
        self.execution_name = execution_name
        self.consumption_id = "exc_" + "f" * 64
        self.operation = "daily_staging_release"


class Recorded:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"result": {"terminal_state": kwargs["terminal_state"]}}


def child(
    prepared,
    derived,
    objects,
    *,
    executors=None,
    row=None,
    principal=ORCHESTRATION,
    stage="release",
    environment=None,
    execution=None,
):
    consumed, recorded = Consumed(), Recorded()
    execution = execution or execution_resource(subject.step_number(stage))
    writes = Writes(execution, row or derivation_row(prepared, derived))
    natives = Natives(execution)
    child.writes = writes
    child.natives = natives
    child.consumed = consumed
    outcome = subject.run_child_execution(
        environment=env(subject.step_number(stage)) if environment is None else environment,
        principal=principal,
        runs=natives,
        write_clients=writes,
        objects=objects,
        executors={"daily_staging_release": release_executor} if executors is None else executors,
        now=lambda: STARTED + timedelta(seconds=5),
        consume=consumed,
        record=recorded,
    )
    return outcome, consumed, recorded


METERING = {
    "complete": True,
    "model_calls": 0,
    "query_count": 1,
    "storage_write_bytes": 0,
    "storage_write_count": 0,
    "total_bytes_billed": 10485760,
    "vendor_credits": "0",
}


def release_executor(consumption):
    return {
        "payload": {"contract_version": "daily_release_v1", "released": consumption.derivation_id},
        "stage_metering": METERING,
        "terminal_state": "succeeded",
        "effect_state": "effects_recorded",
        "spend_state": "measured",
        "result_reference": "42/daily/releases/r1.json",
    }


def test_the_child_consumes_then_records_its_stage_result():
    prepared, derived, objects = issued()
    outcome, consumed, recorded = child(prepared, derived, objects)
    assert outcome["state"] == "succeeded"
    (call,) = consumed.calls
    assert call["canonical_operation_context_json"] == prepared["canonical_operation_context_json"]
    assert (
        call["canonical_operation_artifact_set_json"]
        == prepared["canonical_operation_artifact_set_json"]
    )
    observation = json.loads(call["canonical_execution_observation_json"])
    assert observation == {
        "authorizing_grant_digest": GRANT_DIGEST,
        "child_image_uri": IMAGE,
        "child_job_policy_digest": JOB_POLICY,
        "child_job_resource": DAILY_JOB,
        "child_service_identity": ORCHESTRATION,
        "contract_version": "daily_native_execution_observation_v1",
        "derivation_id": derived["derivation_id"],
        "execution_created_at": CREATED.isoformat(timespec="microseconds"),
        "execution_name": EXECUTION,
        "execution_started_at": STARTED.isoformat(timespec="microseconds"),
        "observed_at": (STARTED + timedelta(seconds=5)).isoformat(timespec="microseconds"),
        "observer_principal": ORCHESTRATION,
    }
    assert (
        sha256(call["canonical_execution_observation_json"].encode()).hexdigest()
        == call["execution_observation_sha256"]
    )
    (record,) = recorded.calls
    assert record["consumption_id"] == "exc_" + "f" * 64
    assert record["canonical_payload_json"] == canonical_bytes(
        {"contract_version": "daily_release_v1", "released": derived["derivation_id"]}
    ).decode("utf-8")
    assert sha256(record["canonical_payload_json"].encode()).hexdigest() == record["payload_digest"]
    assert record["canonical_stage_metering_json"] == canonical_bytes(METERING).decode("utf-8")
    assert record["execution_observation_sha256"] == call["execution_observation_sha256"]
    assert record["result_reference"] == "42/daily/releases/r1.json"


def test_an_executor_that_raises_records_a_failure_of_unknown_effect_and_spend():
    prepared, derived, objects = issued()

    def broken(consumption):
        raise RuntimeError("boom")

    outcome, _consumed, recorded = child(
        prepared, derived, objects, executors={"daily_staging_release": broken}
    )
    assert outcome["state"] == "failed"
    (record,) = recorded.calls
    assert record["terminal_state"] == "failed"
    assert record["effect_state"] == "unknown"
    assert record["spend_state"] == "unknown"
    assert json.loads(record["canonical_stage_metering_json"])["complete"] is False
    assert json.loads(record["canonical_payload_json"]) == {
        "contract_version": "daily_child_failure_v1",
        "error": "daily_child_executor_failed",
    }
    assert record["result_reference"] == EXECUTION


def test_a_missing_executor_refuses_before_consuming():
    prepared, derived, objects = issued()
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_executor_missing$"):
        child(prepared, derived, objects, executors={})


def test_the_child_selects_its_derivation_by_its_own_native_bindings():
    prepared, derived, objects = issued()
    outcome, consumed, _recorded = child(prepared, derived, objects)
    assert child.writes.selections == [(DAILY_JOB, IMAGE, GRANT_DIGEST, JOB_POLICY)]
    assert outcome["derivation_id"] == derived["derivation_id"]
    assert consumed.calls[0]["derivation_id"] == derived["derivation_id"]


@pytest.mark.parametrize("stage", ["certify", "capture", "collection"])
def test_a_selected_derivation_for_another_step_refuses_before_consuming(stage):
    prepared, derived, objects = issued()
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_step_mismatch$"):
        child(prepared, derived, objects, stage=stage)
    assert child.consumed.calls == []


# the child admits itself: the step comes from its own execution, job and process,
# never from its caller (review of dd9fc49, must fix 1)


def test_the_caller_names_no_step_execution_or_derivation():
    parameters = set(inspect.signature(subject.run_child_execution).parameters)
    assert not parameters & {"stage", "step", "execution_name", "derivation_id"}
    assert {"environment", "runs"} <= parameters


def test_the_child_reads_the_execution_its_process_names_and_the_daily_job():
    prepared, derived, objects = issued()
    outcome, _consumed, _recorded = child(prepared, derived, objects)
    assert child.natives.reads == [EXECUTION, DAILY_JOB]
    assert outcome["execution_name"] == EXECUTION


def test_an_execution_started_for_another_step_refuses_before_consuming():
    """The reviewer's probe: the execution ran step 5 while release was derived."""
    prepared, derived, objects = issued()
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_step_mismatch$"):
        child(
            prepared,
            derived,
            objects,
            environment=env("5"),
            execution=execution_resource("5"),
        )
    assert child.consumed.calls == []


@pytest.mark.parametrize(
    "environment",
    [
        env("5"),
        env(CLOUD_RUN_EXECUTION="child1:run"),
        env(CLOUD_RUN_EXECUTION="child1?alt=media"),
        env(CLOUD_RUN_EXECUTION="Child1"),
        {"DAILY_STEP_NUMBER": RELEASE_STEP},
        {"CLOUD_RUN_EXECUTION": EXECUTION_ID},
    ],
)
def test_a_process_that_does_not_name_its_own_execution_and_step_refuses(environment):
    prepared, derived, objects = issued()
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_identity_mismatch$"):
        child(prepared, derived, objects, environment=environment)
    assert child.writes.selections == []
    assert child.consumed.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"step_number": "5"},
        {"step_number": None},
        {"execution_name": DAILY_JOB + "/executions/intelligence-42-daily-staging-child2"},
        {"execution_name": None},
    ],
)
def test_a_native_view_naming_another_step_or_execution_refuses_before_selecting(
    change, monkeypatch
):
    """The child checks its own native view before select, not only consume later."""
    view = subject.native_execution_view

    def changed(execution):
        return dict(view(execution), **change)

    monkeypatch.setattr(subject, "native_execution_view", changed)
    prepared, derived, objects = issued()
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_identity_mismatch$"):
        child(prepared, derived, objects)
    assert child.writes.selections == []
    assert child.consumed.calls == []


def test_an_execution_that_differs_from_its_job_refuses_before_selecting():
    prepared, derived, objects = issued()
    execution = execution_resource()
    execution["template"]["containers"][0]["image"] = IMAGE[:-1] + "0"
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_override_forbidden$"):
        child(prepared, derived, objects, execution=execution)
    assert child.writes.selections == []


def test_a_selection_readback_that_names_no_derivation_refuses():
    prepared, derived, objects = issued()
    row = derivation_row(prepared, derived, derivation_id="exd_short")
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_derivation_invalid$"):
        child(prepared, derived, objects, row=row)


def test_a_derivation_for_another_identity_refuses():
    prepared, derived, objects = issued()
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_identity_mismatch$"):
        child(
            prepared,
            derived,
            objects,
            principal="intelligence-42-release@ogilvy-trends-v2.iam.gserviceaccount.com",
        )


def test_a_tampered_context_or_artifact_set_refuses():
    prepared, derived, objects = issued()
    row = derivation_row(prepared, derived, operation_context_sha256="0" * 64)
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_context_mismatch$"):
        child(prepared, derived, objects, row=row)
    name = next(key for key in objects.objects if key.startswith("42/daily/artifact-sets/"))
    body, generation = objects.objects[name]
    objects.objects[name] = (body + b" ", generation)
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_artifact_set_mismatch$"):
        child(prepared, derived, objects)
    del objects.objects[name]
    with pytest.raises(subject.DailyChildRefusal, match=r"^daily_child_artifact_set_missing$"):
        child(prepared, derived, objects)


def test_a_malformed_executor_outcome_is_recorded_as_a_failure():
    prepared, derived, objects = issued()

    def malformed(consumption):
        return {"payload": {}, "terminal_state": "succeeded"}

    outcome, _consumed, recorded = child(
        prepared, derived, objects, executors={"daily_staging_release": malformed}
    )
    assert outcome["state"] == "failed"
    assert json.loads(recorded.calls[0]["canonical_payload_json"])["error"] == (
        "daily_child_outcome_invalid"
    )


def test_the_real_consume_path_is_the_default():
    assert subject.run_child_execution.__kwdefaults__["consume"].__name__ == (
        "consume_daily_derivation"
    )
    assert subject.run_child_execution.__kwdefaults__["record"].__name__ == "record_daily_result"
    assert subject.run_child_execution.__kwdefaults__["select"].__name__ == (
        "select_daily_derivation"
    )


def test_a_payload_without_its_contract_version_is_recorded_as_a_failure():
    prepared, derived, objects = issued()

    def unversioned(consumption):
        return dict(release_executor(consumption), payload={"released": True})

    outcome, _consumed, recorded = child(
        prepared, derived, objects, executors={"daily_staging_release": unversioned}
    )
    assert outcome["state"] == "failed"
    assert json.loads(recorded.calls[0]["canonical_payload_json"])["error"] == (
        "daily_child_outcome_invalid"
    )


class ConsumeRoutine(Writes):
    """The consume routine's protected readback, as the SQL returns it."""

    def __init__(self, execution, job, prepared, derived):
        super().__init__(execution, derivation_row(prepared, derived))
        self.job = job
        self.prepared = prepared
        self.derived = derived
        self.stored = {}

    def read_native_job(self, job):
        assert job == DAILY_JOB
        return subject.native_job_view(self.job)

    def admit_execution_observation(self, name, body, digest):
        stored_at = datetime.now(UTC)
        object_name = (
            "42/daily/execution-observations/" + sha256(name.encode()).hexdigest() + ".json"
        )
        self.stored[object_name] = body
        return body, {
            "content_sha256": digest,
            "created_at": stored_at.isoformat(),
            "generation": "1",
            "object_name": object_name,
            "size_bytes": len(body),
        }

    def consume_derivation(self, derivation_id, name, context_json, artifacts, obs, obs_sha):
        consumed_at = datetime.now(UTC).isoformat()
        consumption = {
            "approval_id": derivation_id,
            "consumed_at": consumed_at,
            "execution_name": name,
            "operation": self.derived["operation"],
            "origin_registry_sha256": "a" * 64,
            "resource_manifest_sha256": "b" * 64,
        }
        consumption["consumption_id"] = (
            "exc_"
            + sha256(
                canonical_bytes(
                    {
                        "consumed_at": consumed_at,
                        "consumption_contract_version": "open_intelligence_execution_consumption_v3",
                        "derivation_id": derivation_id,
                        "execution_name": name,
                        "execution_observation_sha256": obs_sha,
                        "origin_registry_sha256": "a" * 64,
                        "resource_manifest_sha256": "b" * 64,
                    }
                )
            ).hexdigest()
        )
        value = grant()
        return {
            "derivation": dict(
                self.derived,
                canonical_operation_context_json=context_json,
                operation_context_sha256=sha256(context_json.encode()).hexdigest(),
            ),
            "operation_context": json.loads(context_json),
            "manifest": json.loads(self.prepared["canonical_manifest_json"]),
            "consumption": consumption,
            "grant": value,
            "authorizing_approval": {
                "approval_id": self.derived["authorizing_approval_id"],
                "manifest_sha256": self.derived["authorizing_grant_digest"],
            },
        }


def test_the_child_observation_passes_the_real_consume_checks():
    prepared, derived, objects = issued()
    now = datetime.now(UTC)
    execution = execution_resource(
        created=now - timedelta(minutes=2),
        started=now - timedelta(minutes=1),
    )
    writes = ConsumeRoutine(execution, job_resource(), prepared, derived)
    recorded = Recorded()
    outcome = subject.run_child_execution(
        environment=env(),
        principal=ORCHESTRATION,
        runs=Natives(execution),
        write_clients=writes,
        objects=objects,
        executors={"daily_staging_release": release_executor},
        now=lambda: now - timedelta(seconds=30),
        record=recorded,
    )
    assert outcome["state"] == "succeeded"
    assert outcome["consumption_id"].startswith("exc_")
    assert recorded.calls[0]["consumption_id"] == outcome["consumption_id"]
    assert len(writes.stored) == 1


def test_consume_refuses_a_native_execution_started_for_another_step():
    """consume compares the native step with the context's stage, not only shared facts."""
    prepared, derived, objects = issued()
    now = datetime.now(UTC)
    execution = execution_resource(
        created=now - timedelta(minutes=2), started=now - timedelta(minutes=1)
    )
    consumed = Consumed()
    subject.run_child_execution(
        environment=env(),
        principal=ORCHESTRATION,
        runs=Natives(execution),
        write_clients=ConsumeRoutine(execution, job_resource(), prepared, derived),
        objects=objects,
        executors={"daily_staging_release": release_executor},
        now=lambda: now - timedelta(seconds=30),
        consume=consumed,
        record=Recorded(),
    )
    (call,) = consumed.calls
    other = execution_resource(
        "5", created=now - timedelta(minutes=2), started=now - timedelta(minutes=1)
    )
    writes = ConsumeRoutine(other, job_resource(), prepared, derived)
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_native_execution_mismatch$"):
        consume_daily_derivation(**dict(call, clients=writes))
    assert writes.stored == {}
