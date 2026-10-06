"""One v2 daily stage run as a child execution of the daily job.

The parent execution derives the stage (``daily_derivation_authority``) and then runs the
daily job again with exactly one container environment override, the step number:
``DAILY_STEP_NUMBER`` is the stage's position in ``STAGE_ORDER_V2``, "1" for collection to
"6" for release. That is the override Albert approved on 23 September at 08:44: the daily
job may start a copy of itself per step, changing only a step number. Nothing else may
differ between the child execution and the job template: not the image, command,
arguments, identity, timeout, retries, task shape, annotations or any other variable.

The child admits itself on that rule, from nothing its caller names: the execution its
process environment names (``CLOUD_RUN_EXECUTION``), the daily job and its own
``DAILY_STEP_NUMBER``. It then observes its own native execution, whose view carries the
step number the consume path compares with the context's stage. It selects its derivation through ``sp_select_open_intelligence_daily_derivation_v1`` by its
own job, image, grant digest and job policy digest, under its own identity, and refuses
unless the selected derivation's stage is its step. It reads the write-once artifact set
the derivation's operation context names, consumes the derivation, runs the stage
executor and records the protected result.

The native job carries two annotations the consume routine compares against the
operation context:

* ``42.ogilvy/runtime-configuration-sha256``, already set by ``runtime_jobs``, read as the
  job policy digest the grant binds;
* ``42.ogilvy/recurring-grant-sha256``, the authorising grant digest. The runtime plan does
  not yet set it; it is drafted as an unapproved amendment, and until it is applied the
  native job view refuses ``daily_native_job_unbound``.

The execution's ``template`` is assumed to carry the override as applied by Cloud Run.
That provider behaviour is unverified here and is checked in the native brief before any
child run.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256

from .brain_contract import canonical_bytes
from .daily_cycle import STEP_NUMBERS_V2
from .daily_derivation_authority import ARTIFACT_SET_PREFIX
from .daily_execution_authority import (
    consume_daily_derivation,
    record_daily_result,
    select_daily_derivation,
)
from .daily_execution_contracts import (
    DERIVATION_ID,
    MAX_ARTIFACT_SET_BYTES,
    DailyContractError,
    validate_operation_context,
    validate_result_metering,
)
from .daily_operation_map import DAILY_JOB_RESOURCE
from .daily_store import child_job_resource_v2

CHILD_VARIABLE = "DAILY_STEP_NUMBER"
STEP_NUMBERS = STEP_NUMBERS_V2
_STEP_STAGES = {step: stage for stage, step in STEP_NUMBERS.items()}
POLICY_ANNOTATION = "42.ogilvy/runtime-configuration-sha256"
GRANT_ANNOTATION = "42.ogilvy/recurring-grant-sha256"
OBSERVATION_CONTRACT = "daily_native_execution_observation_v1"
FAILURE_CONTRACT = "daily_child_failure_v1"
DAILY_JOB_ID = DAILY_JOB_RESOURCE.rsplit("/", 1)[1]
EXECUTION_PREFIX = DAILY_JOB_RESOURCE + "/executions/"
# The funded Wave 1 pilot job, whose executions the bridge's approval ledger route binds its
# collection receipts to. Where a daily child carries its step number, a funded execution
# carries the credential lane and funded stage its job definition sets, each with its one
# admitted value; the funded job names no digest annotation.
FUNDED_JOB_RESOURCE = DAILY_JOB_RESOURCE.rsplit("/", 1)[0] + "/intelligence-42-funded-pilot-staging"
FUNDED_EXECUTION_PREFIX = FUNDED_JOB_RESOURCE + "/executions/"
FUNDED_VARIABLES = {
    "credential_lane": ("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded"),
    "funded_stage": ("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1"),
}
OPERATION_PREFIX = DAILY_JOB_RESOURCE.split("/jobs/", 1)[0] + "/operations/"
OUTCOME_FIELDS = frozenset(
    {
        "payload",
        "stage_metering",
        "terminal_state",
        "effect_state",
        "spend_state",
        "result_reference",
    }
)
# A Cloud Run execution ID: an RFC 1035 label, so nothing that changes the request URL.
EXECUTION_ID = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_PROVIDER_INSTANT = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})"
)
_TASK_COUNTS = ("succeededCount", "failedCount", "cancelledCount")
_CANCEL_REASONS = frozenset({"CANCELLED", "CANCELLING"})
_UNKNOWN_METERING = {
    "complete": False,
    "model_calls": None,
    "query_count": None,
    "storage_write_bytes": None,
    "storage_write_count": None,
    "total_bytes_billed": None,
    "vendor_credits": None,
}


class DailyChildRefusal(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise DailyChildRefusal(code)


def _derivation_id(value: object) -> str:
    if not isinstance(value, str) or DERIVATION_ID.fullmatch(value) is None:
        _refuse("daily_child_derivation_invalid")
    return value


def step_number(stage: object) -> str:
    """The step number a stage runs under: its position in the daily order."""
    if not isinstance(stage, str) or stage not in STEP_NUMBERS:
        _refuse("daily_child_step_invalid")
    return STEP_NUMBERS[stage]


def stage_for_step(value: object) -> str:
    if not isinstance(value, str) or value not in _STEP_STAGES:
        _refuse("daily_child_step_invalid")
    return _STEP_STAGES[value]


def child_run_request(stage: object) -> dict:
    """The Jobs API ``:run`` body: the step number and nothing else."""
    return {
        "overrides": {
            "containerOverrides": [{"env": [{"name": CHILD_VARIABLE, "value": step_number(stage)}]}]
        }
    }


def _dict(value: object, code: str) -> dict:
    if type(value) is not dict:
        _refuse(code)
    return value


def _only_container(task: dict, code: str) -> dict:
    containers = task.get("containers")
    if type(containers) is not list or len(containers) != 1:
        _refuse(code)
    return _dict(containers[0], code)


def child_override_of(*, execution: object, job: object) -> str:
    """The stage an execution was started for, if its step number is its only difference."""
    forbidden = "daily_child_override_forbidden"
    execution = _dict(execution, forbidden)
    job = _dict(job, forbidden)
    job_template = _dict(job.get("template"), forbidden)
    task = _dict(job_template.get("template"), forbidden)
    ran = _dict(execution.get("template"), forbidden)
    annotations = job_template.get("annotations")
    name = execution.get("name")
    if (
        job.get("name") != DAILY_JOB_RESOURCE
        or execution.get("job") not in (DAILY_JOB_ID, DAILY_JOB_RESOURCE)
        or not isinstance(name, str)
        or not name.startswith(EXECUTION_PREFIX)
        or type(annotations) is not dict
        or execution.get("annotations") != annotations
        or execution.get("taskCount") != job_template.get("taskCount")
        or execution.get("parallelism") != job_template.get("parallelism")
        or set(ran) != set(task)
        or any(ran[key] != task[key] for key in task if key != "containers")
    ):
        _refuse(forbidden)
    expected, actual = _only_container(task, forbidden), _only_container(ran, forbidden)
    if set(actual) | {"env"} != set(expected) | {"env"} or any(
        actual[key] != expected[key] for key in expected if key != "env"
    ):
        _refuse(forbidden)
    template_env = expected.get("env", [])
    ran_env = actual.get("env")
    if type(template_env) is not list or type(ran_env) is not list:
        _refuse(forbidden)
    if any(
        type(entry) is not dict or entry.get("name") == CHILD_VARIABLE for entry in template_env
    ):
        _refuse(forbidden)
    overrides = [
        entry for entry in ran_env if type(entry) is dict and entry.get("name") == CHILD_VARIABLE
    ]
    remainder = [entry for entry in ran_env if entry not in overrides]
    if not overrides:
        if ran_env == template_env:
            _refuse("daily_child_derivation_missing")
        _refuse(forbidden)
    if len(overrides) != 1 or set(overrides[0]) != {"name", "value"} or remainder != template_env:
        _refuse(forbidden)
    return stage_for_step(overrides[0]["value"])


def admit_child_execution(*, execution: object, job: object, environment: Mapping[str, str]) -> str:
    """The child's own admission: its execution and process both name one step."""
    stage = child_override_of(execution=execution, job=job)
    execution_id = environment.get("CLOUD_RUN_EXECUTION")
    if (
        environment.get(CHILD_VARIABLE) != STEP_NUMBERS[stage]
        or not isinstance(execution_id, str)
        or EXECUTION_ID.fullmatch(execution_id) is None
        or execution["name"] != EXECUTION_PREFIX + execution_id
    ):
        _refuse("daily_child_identity_mismatch")
    return stage


def _provider_instant(value: object, code: str) -> str:
    match = _PROVIDER_INSTANT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        _refuse(code)
    whole, fraction, zone = match.groups()
    micros = (fraction or "").ljust(6, "0")[:6]
    try:
        parsed = datetime.fromisoformat(
            f"{whole}.{micros}{'+00:00' if zone == 'Z' else zone}"
        ).astimezone(UTC)
    except ValueError as error:
        raise DailyChildRefusal(code) from error
    return parsed.isoformat(timespec="microseconds")


def _digest_annotation(annotations: object, name: str, code: str) -> str:
    value = annotations.get(name) if type(annotations) is dict else None
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        _refuse(code)
    return value


def native_job_view(job: object) -> dict:
    """The job facts ``consume_daily_derivation`` compares against the context."""
    code = "daily_native_job_unbound"
    job = _dict(job, code)
    template = _dict(job.get("template"), code)
    task = _dict(template.get("template"), code)
    container = _only_container(task, code)
    view = {
        "job_resource": job.get("name"),
        "image_uri": container.get("image"),
        "service_identity": task.get("serviceAccount"),
        "job_policy_digest": _digest_annotation(
            template.get("annotations"), POLICY_ANNOTATION, code
        ),
        "grant_digest": _digest_annotation(template.get("annotations"), GRANT_ANNOTATION, code),
    }
    if view["job_resource"] != DAILY_JOB_RESOURCE or not all(
        isinstance(value, str) and value for value in view.values()
    ):
        _refuse(code)
    return view


def _terminal_facts(execution: dict, started_at: str) -> dict:
    """The terminal state and completion time of a completed execution; none while it runs.

    Cloud Run v2 sets ``completionTime`` once an execution completes and reports its end
    in the ``Completed`` condition and the task counts, which proto3 JSON omits when zero.
    A cancelled task or a cancel reason makes it cancelled. It succeeded only if the
    condition succeeded with no reason and every task succeeded, and failed only if the
    condition failed and not every task succeeded. A completion before the start, or
    anything else, refuses.
    """
    if "completionTime" not in execution:
        return {}
    code = "daily_native_execution_terminal_ambiguous"
    completed_at = execution["completionTime"]
    finished = datetime.fromisoformat(_provider_instant(completed_at, code))
    if not completed_at.endswith(("Z", "+00:00")) or finished < datetime.fromisoformat(started_at):
        _refuse(code)
    task_count = execution.get("taskCount")
    counts = {name: execution.get(name, 0) for name in _TASK_COUNTS}
    if (
        type(task_count) is not int
        or task_count < 1
        or any(type(count) is not int or count < 0 for count in counts.values())
        or sum(counts.values()) > task_count
    ):
        _refuse(code)
    conditions = execution.get("conditions")
    # The v2 reference names no condition types. "Completed" is the terminal condition in
    # the v1 status shape and the Cloud Run jobs guidance; that a real v2 GET carries it,
    # with these states and reasons, is still a native check for Albert's session.
    completed = [
        condition
        for condition in (conditions if type(conditions) is list else ())
        if type(condition) is dict and condition.get("type") == "Completed"
    ]
    if len(completed) != 1:
        _refuse(code)
    state = completed[0].get("state")
    reason = completed[0].get("executionReason")
    if counts["cancelledCount"] or reason in _CANCEL_REASONS:
        terminal = "cancelled"
    elif (
        state == "CONDITION_SUCCEEDED" and reason is None and counts["succeededCount"] == task_count
    ):
        terminal = "succeeded"
    elif state == "CONDITION_FAILED" and counts["succeededCount"] < task_count:
        terminal = "failed"
    else:
        _refuse(code)
    return {"terminal_state": terminal, "completed_at": completed_at}


def _pinned_execution_view(execution: object, pin: Mapping[str, object]) -> dict:
    """The facts of one execution of the pinned job ``pin`` names, under its own codes."""
    code = pin["code"]
    job_resource = pin["job_resource"]
    prefix = job_resource + "/executions/"
    execution = _dict(execution, code)
    name = execution.get("name")
    task = _dict(execution.get("template"), code)
    container = _only_container(task, code)
    annotations = execution.get("annotations")
    if (
        not isinstance(name, str)
        or not name.startswith(prefix)
        or EXECUTION_ID.fullmatch(name[len(prefix) :]) is None
        or execution.get("job") not in (job_resource.rsplit("/", 1)[1], job_resource)
    ):
        _refuse(code)
    view = {
        "execution_name": name,
        "job_resource": job_resource,
        "image_uri": container.get("image"),
        "service_identity": task.get("serviceAccount"),
    }
    for field, annotation in pin["annotations"]:
        view[field] = _digest_annotation(annotations, annotation, code)
    view["execution_created_at"] = _provider_instant(execution.get("createTime"), code)
    if "startTime" not in execution:
        _refuse(pin["not_started"])
    view["execution_started_at"] = _provider_instant(execution["startTime"], code)
    for field, variable, admitted in pin["variables"]:
        entries = [
            entry
            for entry in container.get("env", ())
            if type(entry) is dict and entry.get("name") == variable
        ]
        if (
            len(entries) != 1
            or set(entries[0]) != {"name", "value"}
            or not admitted(entries[0]["value"])
        ):
            _refuse(code)
        view[field] = entries[0]["value"]
    view.update(_terminal_facts(execution, view["execution_started_at"]))
    if not all(isinstance(value, str) and value for value in view.values()):
        _refuse(code)
    return view


_DAILY_EXECUTION = {
    "job_resource": DAILY_JOB_RESOURCE,
    "code": "daily_native_execution_invalid",
    "not_started": "daily_native_execution_not_started",
    "annotations": (("job_policy_digest", POLICY_ANNOTATION), ("grant_digest", GRANT_ANNOTATION)),
    "variables": (("step_number", CHILD_VARIABLE, lambda value: value in _STEP_STAGES),),
}
_FUNDED_EXECUTION = {
    "job_resource": FUNDED_JOB_RESOURCE,
    "code": "funded_native_execution_invalid",
    "not_started": "funded_native_execution_not_started",
    "annotations": (),
    "variables": tuple(
        (field, variable, lambda value, admitted=admitted: value == admitted)
        for field, (variable, admitted) in FUNDED_VARIABLES.items()
    ),
}


def native_execution_view(execution: object) -> dict:
    """The execution facts the consume routine and the observation must agree on.

    A completed execution also carries its ``terminal_state`` (succeeded, failed or
    cancelled) and ``completed_at``, the provider ``completionTime`` as returned, which
    the chain read compares with the protected result.
    """
    return _pinned_execution_view(execution, _DAILY_EXECUTION)


def native_funded_execution_view(execution: object) -> dict:
    """The facts of one execution of the funded pilot job, read by the daily rules.

    It names the funded job, its image and identity, its creation and start instants and
    the credential lane and funded stage its container sets, each exactly once as its one
    admitted plain value; a completed execution carries its terminal state and completion
    time from the same strict terminal read as a daily execution.
    """
    return _pinned_execution_view(execution, _FUNDED_EXECUTION)


def _terminal(execution: Mapping[str, object]) -> str | None:
    if not execution.get("completionTime"):
        return None
    succeeded = execution.get("succeededCount", 0)
    failed = execution.get("failedCount", 0)
    cancelled = execution.get("cancelledCount", 0)
    if succeeded == execution.get("taskCount") and not failed and not cancelled:
        return "succeeded"
    return "failed"


class ChildDispatcher:
    """The kernel's stage callable in the parent: run the child, bind, wait for its end.

    The protected result the child records stays the authority on the stage outcome;
    the parent only reports that the child reached a terminal state, after which the
    kernel publishes the terminal from the protected chain.
    """

    def __init__(self, *, store, runs, polls: int, sleep: Callable[[float], None], interval=15):
        if type(polls) is not int or polls < 1:
            _refuse("daily_child_poll_budget_invalid")
        self._store = store
        self._runs = runs
        self._polls = polls
        self._sleep = sleep
        self._interval = interval

    def __call__(self, *, manifest, authority_receipt):
        stage = manifest.get("stage")
        request = child_run_request(stage)
        job = authority_receipt.get("child_job_resource")
        if job != child_job_resource_v2(stage):
            _refuse("daily_child_job_mismatch")
        try:
            operation = self._runs.run_job(job, request)
        except Exception:
            return {"state": "unknown"}
        operation_name = operation.get("name") if isinstance(operation, Mapping) else None
        metadata = operation.get("metadata") if isinstance(operation, Mapping) else None
        execution_name = metadata.get("name") if isinstance(metadata, Mapping) else None
        if (
            not isinstance(operation_name, str)
            or not operation_name.startswith(OPERATION_PREFIX)
            or len(operation_name) == len(OPERATION_PREFIX)
            or not isinstance(execution_name, str)
            or not execution_name.startswith(job + "/executions/")
        ):
            return {"state": "unknown"}
        self._store.bind_dispatch_response_v2(manifest["operation_id"], operation_name)
        for attempt in range(self._polls):
            if attempt:
                self._sleep(self._interval)
            try:
                execution = self._runs.read_execution(execution_name)
                template = self._runs.read_raw_job(job)
            except Exception:
                continue
            if child_override_of(execution=execution, job=template) != stage:
                _refuse("daily_child_execution_mismatch")
            state = _terminal(execution)
            if state is not None:
                return {"state": state, "execution_name": execution_name}
        return {"state": "pending", "execution_name": execution_name}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _outcome(executor, consumption, execution_name: str) -> dict:
    try:
        outcome = executor(consumption)
    except Exception as error:
        code = getattr(error, "code", None)
        return _failure(
            code if isinstance(code, str) and code else "daily_child_executor_failed",
            execution_name,
        )
    if (
        not isinstance(outcome, Mapping)
        or set(outcome) != OUTCOME_FIELDS
        or not isinstance(outcome["payload"], Mapping)
        or not isinstance(outcome["payload"].get("contract_version"), str)
        or not outcome["payload"]["contract_version"]
        or not isinstance(outcome["stage_metering"], Mapping)
        or not isinstance(outcome["result_reference"], str)
        or not outcome["result_reference"]
    ):
        return _failure("daily_child_outcome_invalid", execution_name)
    try:
        canonical_bytes(outcome["payload"])
        validate_result_metering(
            canonical_bytes(outcome["stage_metering"]).decode("utf-8"),
            terminal_state=outcome["terminal_state"],
            effect_state=outcome["effect_state"],
            spend_state=outcome["spend_state"],
        )
    except (DailyContractError, TypeError, ValueError):
        return _failure("daily_child_outcome_invalid", execution_name)
    return dict(outcome)


def _failure(code: str, execution_name: str) -> dict:
    return {
        "payload": {"contract_version": FAILURE_CONTRACT, "error": code},
        "stage_metering": dict(_UNKNOWN_METERING),
        "terminal_state": "failed",
        "effect_state": "unknown",
        "spend_state": "unknown",
        "result_reference": execution_name,
    }


def run_child_execution(
    *,
    environment: Mapping[str, str],
    principal: str,
    runs,
    write_clients,
    objects,
    executors: Mapping[str, Callable],
    now: Callable[[], datetime] = _utc_now,
    select=select_daily_derivation,
    consume=consume_daily_derivation,
    record=record_daily_result,
) -> dict:
    """Admit this execution, select its step's derivation, consume it, run and record.

    ``environment`` is the process environment and ``runs`` the raw Cloud Run reads
    (``CloudRunTransport``); the step and the execution come from them, never from a
    caller argument.
    """
    execution_id = (
        environment.get("CLOUD_RUN_EXECUTION") if isinstance(environment, Mapping) else None
    )
    if not isinstance(execution_id, str) or EXECUTION_ID.fullmatch(execution_id) is None:
        _refuse("daily_child_identity_mismatch")
    execution_name = EXECUTION_PREFIX + execution_id
    stage = admit_child_execution(
        execution=runs.read_execution(execution_name),
        job=runs.read_raw_job(DAILY_JOB_RESOURCE),
        environment=environment,
    )
    native = write_clients.read_native_execution(execution_name)
    if (
        native.get("execution_name") != execution_name
        or native.get("step_number") != (STEP_NUMBERS[stage])
    ):
        _refuse("daily_child_identity_mismatch")
    derivation = select(
        child_job_resource=native["job_resource"],
        child_image_uri=native["image_uri"],
        authorizing_grant_digest=native["grant_digest"],
        child_job_policy_digest=native["job_policy_digest"],
        clients=write_clients,
    )
    if not isinstance(derivation, Mapping):
        _refuse("daily_child_derivation_invalid")
    derivation_id = _derivation_id(derivation.get("derivation_id"))
    if (
        derivation.get("child_service_identity") != principal
        or derivation.get("child_job_resource") != DAILY_JOB_RESOURCE
    ):
        _refuse("daily_child_identity_mismatch")
    context_json = derivation.get("canonical_operation_context_json")
    if not isinstance(context_json, str) or sha256(
        context_json.encode("utf-8")
    ).hexdigest() != derivation.get("operation_context_sha256"):
        _refuse("daily_child_context_mismatch")
    try:
        context = validate_operation_context(context_json)
    except DailyContractError as error:
        raise DailyChildRefusal("daily_child_context_mismatch") from error
    operation = context["operation"]
    if operation != derivation.get("operation"):
        _refuse("daily_child_context_mismatch")
    if context["stage"] != stage:
        _refuse("daily_child_step_mismatch")
    executor = executors.get(operation) if isinstance(executors, Mapping) else None
    if not callable(executor):
        _refuse("daily_child_executor_missing")
    artifact_digest = context["operation_artifact_set_sha256"]
    found = objects.read(f"{ARTIFACT_SET_PREFIX}{artifact_digest}.json")
    if found is None:
        _refuse("daily_child_artifact_set_missing")
    body = found[0]
    if (
        not isinstance(body, bytes)
        or len(body) > MAX_ARTIFACT_SET_BYTES
        or sha256(body).hexdigest() != artifact_digest
    ):
        _refuse("daily_child_artifact_set_mismatch")
    observed_at = now()
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        _refuse("daily_child_clock_invalid")
    observation = {
        "authorizing_grant_digest": native["grant_digest"],
        "child_image_uri": native["image_uri"],
        "child_job_policy_digest": native["job_policy_digest"],
        "child_job_resource": native["job_resource"],
        "child_service_identity": native["service_identity"],
        "contract_version": OBSERVATION_CONTRACT,
        "derivation_id": derivation_id,
        "execution_created_at": native["execution_created_at"],
        "execution_name": execution_name,
        "execution_started_at": native["execution_started_at"],
        "observed_at": observed_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "observer_principal": principal,
    }
    observation_json = canonical_bytes(observation).decode("utf-8")
    observation_sha256 = sha256(observation_json.encode("utf-8")).hexdigest()
    consumption = consume(
        derivation_id=derivation_id,
        execution_name=execution_name,
        canonical_operation_context_json=context_json,
        canonical_operation_artifact_set_json=body.decode("utf-8"),
        canonical_execution_observation_json=observation_json,
        execution_observation_sha256=observation_sha256,
        clients=write_clients,
    )
    outcome = _outcome(executor, consumption, execution_name)
    payload_json = canonical_bytes(outcome["payload"]).decode("utf-8")
    recorded = record(
        derivation_id=derivation_id,
        consumption_id=consumption.consumption_id,
        canonical_payload_json=payload_json,
        payload_digest=sha256(payload_json.encode("utf-8")).hexdigest(),
        execution_observation_sha256=observation_sha256,
        canonical_stage_metering_json=canonical_bytes(outcome["stage_metering"]).decode("utf-8"),
        result_reference=outcome["result_reference"],
        terminal_state=outcome["terminal_state"],
        effect_state=outcome["effect_state"],
        spend_state=outcome["spend_state"],
        clients=write_clients,
    )
    return {
        "state": outcome["terminal_state"],
        "derivation_id": derivation_id,
        "consumption_id": consumption.consumption_id,
        "execution_name": execution_name,
        "result": recorded,
    }


__all__ = [
    "CHILD_VARIABLE",
    "EXECUTION_ID",
    "EXECUTION_PREFIX",
    "FUNDED_EXECUTION_PREFIX",
    "FUNDED_JOB_RESOURCE",
    "FUNDED_VARIABLES",
    "GRANT_ANNOTATION",
    "POLICY_ANNOTATION",
    "ChildDispatcher",
    "DailyChildRefusal",
    "admit_child_execution",
    "child_override_of",
    "child_run_request",
    "native_execution_view",
    "native_funded_execution_view",
    "native_job_view",
    "run_child_execution",
    "stage_for_step",
    "step_number",
]
