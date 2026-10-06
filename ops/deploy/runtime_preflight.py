"""Attended native preflight for the R05 managed runtime.

trigger reads the immutable Cloud Run job, refuses unless its bound mode is
verify-runtime, and sends the actual empty RunJobRequest. confirm reads that
operation to a terminal state through R04's bounded readback, then reads the
execution itself and checks the attached principal, the bound configuration
digest, the pinned image and an untouched template before it will call the
runtime verified; a deadline is unproven and a failure is a failure, never a
verified runtime. Every rule is re-applied wherever the value is read again
rather than carried over: the pin rule at each reading of the image, the digest
chain at each document, and the resource grammars before any name reaches a URL.
handoff exports the D04 package: the approved runtime resource manifest, the
attached principal, the invoker permission observed on the job's own policy and
the verify-runtime receipt, and only while both schedulers read back paused on a
readback document bound to this chain, this receipt and its own recorded reads.
It is given the scheduler plan as well, because a readback naming a plan digest
is a claim until the plan it names is loaded, sealed and found to carry that
digest; a readback with an invented digest is refused there rather than shape
checked, and the plan itself is bound to the reviewed files, so a plan that
could never have applied is not evidence either. A readback typed by hand with
the real digests and the two real reads is still accepted: no check over a
document can tell it from one a command wrote, and closing it needs the handoff
to perform the two describes itself over the adapter, or the readback to carry
a signature this command verifies.

Before the package is written the producer reads it back through its own
canonical encoding and recomputes its digest the way a reader would. That is an
encoding check, not evidence for anyone downstream: the only thing that proves
the package was not edited after it was written is a reader recomputing the
digest over the bytes it read.

A refusal that follows a request that already went out is written as a receipt
naming every request sent, because trigger sends the actual empty RunJobRequest
and a refusal after it with nothing written leaves an execution nobody can name.

Nothing here grants a schedule, a release or a recurring authority. The daily
mode is a reviewed edit of the runtime configuration, never a preflight flag,
so this module cannot start a cycle by sending a different request.
"""

import argparse
import json
import sys

from ops.deploy.readback import wait_operation
from ops.deploy.resource_guard import assert_allowed, load_resource_manifest
from ops.deploy.runtime_native_adapter import (
    SCHEDULER_ENDPOINT,
    NativeAdapter,
    NativeError,
    pinned_operation,
)
from ops.deploy.runtime_schedulers import (
    IAM_DELTA,
    PAUSED,
    RESOURCE_MANIFEST,
    RUNTIME_FILE,
    _attached,
    _bound,
    _invoker_bindings,
    _load_json,
    _output_available,
    _scheduler_identity,
    _validated_plan,
    _written,
    file_sha256,
)
from ops.deploy.runtime_schedulers import READBACK_CONTRACT as SCHEDULER_READBACK
from ops.runners.managed_runtime import (
    CONFIGURATION_ANNOTATION,
    DAILY_JOB_ID,
    DAILY_JOB_NAME,
    DAILY_JOB_RESOURCE,
    PRICE_POLICY_JOB_ID,
    PROJECT,
    REGION,
    canonical_bytes,
    canonical_sha256,
    load_runtime_configuration,
)

TRIGGER_CONTRACT = "42_runtime_preflight_trigger_v1"
VERIFY_CONTRACT = "42_runtime_preflight_verify_v1"
HANDOFF_CONTRACT = "42_r05_runtime_handoff_v1"
REFUSAL_CONTRACT = "42_runtime_preflight_refusal_v1"
VERIFY_MODE = "verify-runtime"
CREDENTIAL_SOURCE = "attached_service_account"
INVOKER_ROLE = "roles/run.invoker"
RUN_JOB_PREFIX = f"//run.googleapis.com/projects/{PROJECT}/locations/{REGION}/jobs/"
JOB_PATH = f"projects/{PROJECT}/locations/{REGION}/jobs/"
ACTIVATION = {
    "daily_schedule": "paused, D04 activation gate under the C03 recurring grant",
    "price_policy_schedule": "paused, separate C04 renewal grant",
}
_ADAPTER_STATES = ("owner-gcloud", "adc")
_HEX = "0123456789abcdef"
_READBACK_KEYS = {
    "contract_version",
    "plan_sha256",
    "record",
    "resource_manifest_sha256",
    "runtime_configuration_sha256",
    "schedulers",
    "scheduler_configuration_sha256",
    "state",
}


def _refuse(code):
    raise ValueError(code)


def _digest_pinned(image, repository):
    prefix = repository + "@sha256:"
    if not isinstance(image, str) or not image.startswith(prefix):
        return False
    tail = image[len(prefix) :]
    return len(tail) == 64 and all(character in _HEX for character in tail)


def _loaded(runtime_path, manifest_path):
    config = load_runtime_configuration(runtime_path)
    resources = load_resource_manifest(
        manifest_path, expected_sha256=config["resource_manifest_sha256"]
    )
    return config, resources, config["jobs"][config["runtime_job"]]


def _container(task, code):
    containers = task.get("containers") if type(task) is dict else None
    if type(containers) is not list or len(containers) != 1:
        _refuse(code)
    if type(containers[0]) is not dict:
        _refuse(code)
    return containers[0]


def _template_drift(entry, task, counts, code):
    """Every field that must not change between the configuration and what ran.

    The comparison names the fields rather than demanding an equal document, so
    a field the API populates on its own answer is not read as an override while
    a changed command, argument, environment, retry, timeout or count still is.
    """
    container = _container(task, code)
    return (
        task.get("maxRetries") != entry["max_retries"]
        or task.get("timeout") != f"{entry['timeout_seconds']}s"
        or counts.get("taskCount") != entry["task_count"]
        or counts.get("parallelism") != entry["parallelism"]
        or container.get("command") != entry["command"]
        or container.get("args") != entry["args"]
        or container.get("env", []) != entry["env"]
    )


def trigger_verify_runtime(
    *, adapter, runtime_path=RUNTIME_FILE, manifest_path=RESOURCE_MANIFEST
) -> dict:
    """Read the immutable job, then send the empty RunJobRequest it is bound to."""
    config, resources, entry = _loaded(runtime_path, manifest_path)
    if entry["mode"] != VERIFY_MODE:
        _refuse("preflight_mode_forbidden")
    orchestration = resources["identities"]["orchestration"].rsplit("/", 1)[1]
    if entry["service_account"] != orchestration:
        _refuse("principal_mismatch")
    assert_allowed(DAILY_JOB_RESOURCE, "invoke", resources)
    assert_allowed(DAILY_JOB_RESOURCE, "read", resources)
    configuration_sha256 = canonical_sha256(config)
    job = adapter.get("get_job", name=DAILY_JOB_NAME)
    if job is None:
        _refuse("runtime_job_absent")
    template = job.get("template")
    if type(template) is not dict or type(template.get("template")) is not dict:
        _refuse("runtime_configuration_mismatch")
    annotations = template.get("annotations")
    if (
        type(annotations) is not dict
        or annotations.get(CONFIGURATION_ANNOTATION) != configuration_sha256
    ):
        _refuse("runtime_configuration_unbound")
    task = template["template"]
    if task.get("serviceAccount") != entry["service_account"]:
        _refuse("principal_mismatch")
    if _template_drift(entry, task, template, "runtime_configuration_mismatch"):
        _refuse("runtime_configuration_mismatch")
    image = _container(task, "runtime_configuration_mismatch").get("image")
    if not _digest_pinned(image, entry["image_repository"]):
        _refuse("image_unbound")
    status, operation = adapter.perform("run_job", name=DAILY_JOB_NAME)
    adapter.require_ok("run_job", status, operation)
    name = operation.get("name")
    if not isinstance(name, str) or not name.strip():
        _refuse("operation_name_missing")
    return {
        "contract_version": TRIGGER_CONTRACT,
        "status": "triggered",
        "mode": VERIFY_MODE,
        "operation": name,
        "job_name": DAILY_JOB_NAME,
        "image": image,
        "principal": entry["service_account"],
        "configuration_sha256": configuration_sha256,
        "resource_manifest_sha256": config["resource_manifest_sha256"],
        "scheduler_configuration_sha256": config["scheduler_configuration_sha256"],
        "record": list(adapter.record),
    }


def _operation_reader(adapter):
    def read(name, timeout_seconds):
        status, payload = adapter.perform(
            "read_operation", name=name, timeout_seconds=timeout_seconds
        )
        adapter.require_ok("read_operation", status, payload)
        identity = payload.get("name")
        if not payload.get("done"):
            return {"operation": identity, "state": "pending", "error": None}
        error = payload.get("error")
        if error is not None:
            return {
                "operation": identity,
                "state": "failed",
                "error": f"{error.get('code')}: {error.get('message')}",
            }
        response = payload.get("response")
        execution = response.get("name") if type(response) is dict else None
        return {
            "operation": identity,
            "state": "succeeded",
            "error": None,
            "execution": execution,
        }

    return read


def confirm_execution(
    triggered,
    *,
    adapter,
    clock,
    sleep,
    deadline_seconds=1800,
    poll_seconds=10,
    runtime_path=RUNTIME_FILE,
    manifest_path=RESOURCE_MANIFEST,
) -> dict:
    """Read the operation to terminal, then prove the execution that actually ran."""
    if type(triggered) is not dict or triggered.get("status") != "triggered":
        _refuse("trigger_receipt_unusable")
    if triggered.get("contract_version") != TRIGGER_CONTRACT:
        _refuse("trigger_receipt_unusable")
    if triggered.get("mode") != VERIFY_MODE:
        _refuse("trigger_receipt_unusable")
    if triggered.get("job_name") != DAILY_JOB_NAME:
        _refuse("trigger_receipt_unusable")
    config, _resources, entry = _loaded(runtime_path, manifest_path)
    if triggered.get("configuration_sha256") != canonical_sha256(config):
        _refuse("trigger_receipt_unusable")
    # The pin rule is re-applied wherever the image is read, never carried over.
    if not _digest_pinned(triggered.get("image"), entry["image_repository"]):
        _refuse("image_unbound")
    # The operation is addressed by its own grammar, never by concatenation.
    pinned_operation(triggered.get("operation"))
    waited = wait_operation(
        _operation_reader(adapter),
        triggered["operation"],
        deadline_seconds=deadline_seconds,
        poll_seconds=poll_seconds,
        clock=clock,
        sleep=sleep,
    )
    if waited["state"] == "unproven":
        _refuse("execution_unproven")
    if waited["state"] != "succeeded":
        _refuse("execution_not_verified")
    execution_name = (waited.get("last_response") or {}).get("execution")
    if not isinstance(execution_name, str) or not execution_name.strip():
        _refuse("execution_identity_missing")
    execution = adapter.get("get_execution", name=execution_name)
    if execution is None:
        _refuse("execution_absent")
    if execution.get("name") != execution_name:
        _refuse("execution_identity_mismatch")
    if execution.get("job") != DAILY_JOB_NAME:
        _refuse("execution_job_mismatch")
    annotations = execution.get("annotations")
    if (
        type(annotations) is not dict
        or annotations.get(CONFIGURATION_ANNOTATION)
        != triggered["configuration_sha256"]
    ):
        _refuse("runtime_configuration_unbound")
    task = execution.get("template")
    if type(task) is not dict:
        _refuse("runtime_override_forbidden")
    if task.get("serviceAccount") != entry["service_account"]:
        _refuse("principal_mismatch")
    if _template_drift(entry, task, execution, "runtime_override_forbidden"):
        _refuse("runtime_override_forbidden")
    ran = _container(task, "runtime_override_forbidden").get("image")
    if ran != triggered["image"]:
        _refuse("runtime_override_forbidden")
    completion = execution.get("completionTime")
    if (
        execution.get("succeededCount") != entry["task_count"]
        or execution.get("failedCount") not in (0, None)
        or execution.get("cancelledCount") not in (0, None)
        or not isinstance(completion, str)
        or not completion.strip()
    ):
        _refuse("execution_not_verified")
    return {
        "contract_version": VERIFY_CONTRACT,
        "status": "runtime_verified",
        "mode": VERIFY_MODE,
        "operation": triggered["operation"],
        "operation_state": waited["state"],
        "execution": execution_name,
        "job_name": DAILY_JOB_NAME,
        "principal": entry["service_account"],
        "credential_source": CREDENTIAL_SOURCE,
        "image": triggered["image"],
        "task_count": entry["task_count"],
        "completion_time": completion,
        "configuration_sha256": triggered["configuration_sha256"],
        "resource_manifest_sha256": config["resource_manifest_sha256"],
        "scheduler_configuration_sha256": config["scheduler_configuration_sha256"],
    }


def _checked_receipt(verify_receipt, config, entry):
    if type(verify_receipt) is not dict:
        _refuse("verify_receipt_unusable")
    expected = {
        "contract_version": VERIFY_CONTRACT,
        "status": "runtime_verified",
        "mode": VERIFY_MODE,
        "credential_source": CREDENTIAL_SOURCE,
        "job_name": DAILY_JOB_NAME,
        "principal": entry["service_account"],
        "configuration_sha256": canonical_sha256(config),
        "resource_manifest_sha256": config["resource_manifest_sha256"],
        "scheduler_configuration_sha256": config["scheduler_configuration_sha256"],
    }
    if any(verify_receipt.get(key) != value for key, value in expected.items()):
        _refuse("verify_receipt_unusable")
    if not isinstance(verify_receipt.get("execution"), str):
        _refuse("verify_receipt_unusable")
    if not _digest_pinned(verify_receipt.get("image"), entry["image_repository"]):
        _refuse("image_unbound")
    return verify_receipt


def _recorded_reads(scheduler_readback):
    """The paused states the document claims, each a recorded read of its job."""
    record = scheduler_readback.get("record")
    if type(record) is not list or not record:
        _refuse("scheduler_readback_unbound")
    read = set()
    for entry in record:
        if type(entry) is not dict or entry.get("call") != "get_scheduler":
            _refuse("scheduler_readback_unbound")
        response = entry.get("response")
        if entry.get("status") != 200 or type(response) is not dict:
            _refuse("scheduler_readback_unbound")
        name = response.get("name")
        if not isinstance(name, str) or entry.get("url") != SCHEDULER_ENDPOINT + name:
            _refuse("scheduler_readback_unbound")
        if response.get("state") != PAUSED:
            _refuse("scheduler_readback_unbound")
        read.add(name)
    return read


def _bound_readback(observed, receipt, runtime_path, plan):
    """The readback is usable only as the document the readback command wrote.

    Its key set is the closed one that command produces and its plan digest is
    the digest of the plan handed in beside it, not merely a well shaped
    string. The three file digests it carries are checked against the verify
    receipt and the runtime file below, and build_handoff binds the plan those
    digests belong to against the reviewed files themselves, so a plan that
    could never apply cannot be the plan a readback names.

    What this closes is the accidental mismatch: a readback of one chain paired
    with a plan or a receipt of another. It does not close a readback typed by
    hand. Every value checked here is a value the document carries, and a
    document can carry any of them, so a hand written readback whose digests are
    the real ones and whose record names the two real reads is accepted. Closing
    that needs evidence this command does not hold: either the handoff performs
    the two describes itself over the adapter rather than reading a document, or
    the readback carries a signature over its own bytes that the producer made
    and this command verifies. Neither is done here.
    """
    if set(observed) != _READBACK_KEYS:
        _refuse("scheduler_readback_unbound")
    if observed.get("state") != "matched":
        _refuse("scheduler_readback_drifted")
    # The plan digest is checked against the plan it names, not for its shape:
    # a well formed string that belongs to no plan proves nothing.
    if observed["plan_sha256"] != plan["plan_sha256"]:
        _refuse("scheduler_readback_unbound")
    if observed.get("resource_manifest_sha256") != receipt["resource_manifest_sha256"]:
        _refuse("scheduler_readback_unbound")
    scheduler_digest = receipt["scheduler_configuration_sha256"]
    if observed.get("scheduler_configuration_sha256") != scheduler_digest:
        _refuse("scheduler_readback_unbound")
    if observed.get("runtime_configuration_sha256") != file_sha256(runtime_path):
        _refuse("scheduler_readback_unbound")
    if _recorded_reads(observed) != {
        JOB_PATH + DAILY_JOB_ID,
        JOB_PATH + PRICE_POLICY_JOB_ID,
    }:
        _refuse("scheduler_readback_unbound")


def _checked_schedulers(scheduler_readback, receipt, runtime_path, plan):
    if (
        type(scheduler_readback) is not dict
        or scheduler_readback.get("contract_version") != SCHEDULER_READBACK
    ):
        _refuse("scheduler_readback_unusable")
    observed = scheduler_readback.get("schedulers")
    if type(observed) is not dict or set(observed) != {
        DAILY_JOB_ID,
        PRICE_POLICY_JOB_ID,
    }:
        _refuse("scheduler_readback_unusable")
    states = {}
    for job_id in sorted(observed):
        entry = observed[job_id]
        if type(entry) is not dict or entry.get("result") != "read":
            _refuse("scheduler_readback_unusable")
        if entry.get("state") != PAUSED:
            _refuse("scheduler_not_paused")
        if entry.get("differences"):
            _refuse("scheduler_readback_drifted")
        states[job_id] = PAUSED
    _bound_readback(scheduler_readback, receipt, runtime_path, plan)
    return states


def _observed_invoker(adapter, job_id, required):
    policy = adapter.get("get_job_iam_policy", name=JOB_PATH + job_id)
    bindings = policy.get("bindings") if type(policy) is dict else None
    granted = set()
    for binding in bindings if type(bindings) is list else ():
        if type(binding) is not dict or binding.get("role") != INVOKER_ROLE:
            continue
        members = binding.get("members")
        if type(members) is not list:
            continue
        granted.update(member for member in members if isinstance(member, str))
    for row in required:
        if row["member"] not in granted:
            _refuse("invoker_binding_unproven")
    return [dict(row) for row in required]


def build_handoff(
    *,
    verify_receipt,
    scheduler_plan,
    scheduler_readback,
    adapter,
    runtime_path=RUNTIME_FILE,
    manifest_path=RESOURCE_MANIFEST,
    delta_path=IAM_DELTA,
) -> dict:
    """The R05 export D04 consumes: manifest, principal, invoker proof, receipt."""
    config, resources, entry = _loaded(runtime_path, manifest_path)
    receipt = _checked_receipt(verify_receipt, config, entry)
    plan = _validated_plan(scheduler_plan)
    # A sealed plan is a document like any other: its own seal only proves it
    # was not edited after sealing. _validated_plan shape checks its three file
    # digests, which admits a plan carrying digests that belong to no file on
    # disk and so could never have applied. Binding it to the reviewed files
    # here is what makes the plan digest this package exports mean something.
    _bound(plan, runtime_path, manifest_path)
    states = _checked_schedulers(scheduler_readback, receipt, runtime_path, plan)
    delta, delta_sha256 = _load_json(delta_path, "iam_delta_invalid")
    member = "serviceAccount:" + _scheduler_identity(resources)
    proof = {
        job_id: _observed_invoker(
            adapter,
            job_id,
            _invoker_bindings(delta, RUN_JOB_PREFIX + job_id, member),
        )
        for job_id in sorted(states)
    }
    package = {
        "contract_version": HANDOFF_CONTRACT,
        "project": PROJECT,
        "region": REGION,
        "daily_job_resource": DAILY_JOB_RESOURCE,
        "daily_job_actions": ["invoke", "read"],
        "principal": receipt["principal"],
        "credential_source": CREDENTIAL_SOURCE,
        "image": receipt["image"],
        "execution": receipt["execution"],
        "configuration_sha256": receipt["configuration_sha256"],
        "resource_manifest_sha256": config["resource_manifest_sha256"],
        "runtime_configuration_sha256": canonical_sha256(config),
        "scheduler_configuration_sha256": config["scheduler_configuration_sha256"],
        "iam_delta_sha256": delta_sha256,
        "invoker_proof": proof,
        "schedulers": states,
        "scheduler_plan_sha256": scheduler_readback["plan_sha256"],
        "scheduler_readback_sha256": canonical_sha256(scheduler_readback),
        "activation": dict(ACTIVATION),
        "verify_runtime_receipt_sha256": canonical_sha256(receipt),
    }
    return {**package, "handoff_sha256": canonical_sha256(package)}


def verify_handoff(package) -> str:
    if type(package) is not dict or "handoff_sha256" not in package:
        _refuse("handoff_invalid")
    body = {key: value for key, value in package.items() if key != "handoff_sha256"}
    return canonical_sha256(body)


def _self_verified(package):
    """The package as its reader will parse it, refused unless its digest holds.

    This is a producer side encoding check and nothing more. It does not make
    the export trustworthy to a reader: a reader that does not recompute the
    digest over the bytes it read has no proof at all, and a reader that does
    is the only thing that proves the package was not edited afterwards. What
    this rules out is narrower: a package whose in memory form does not survive
    its own canonical encoding, so that the digest a reader computes over the
    bytes on disk is not the digest the producer sealed.
    """
    reread = json.loads(canonical_bytes(package).decode("utf-8"))
    if reread != package or reread["handoff_sha256"] != verify_handoff(reread):
        _refuse("handoff_invalid")
    return package


def _mutations(record):
    """Every recorded request that was not a read."""
    return [entry["call"] for entry in record if entry.get("method") != "GET"]


def _refusal_receipt(command, adapter, error):
    """A refusal that follows a request owes an artifact naming what went out.

    trigger sends the actual empty RunJobRequest, so a refusal after it is a
    refusal with an execution possibly already running and, without this, no
    document naming it. Reads owe nothing, so a command that only read writes
    no receipt and the refusal stands alone.
    """
    sent = _mutations(adapter.record)
    if not sent:
        return None
    return {
        "contract_version": REFUSAL_CONTRACT,
        "state": "refused",
        "command": command,
        "error": str(error),
        "sent": sent,
        "record": list(adapter.record),
    }


def _parser():
    parser = argparse.ArgumentParser(prog="runtime_preflight")
    commands = parser.add_subparsers(dest="command", required=True)
    trigger = commands.add_parser("trigger")
    trigger.add_argument("--output", required=True)
    confirm = commands.add_parser("confirm")
    confirm.add_argument("--trigger", required=True)
    confirm.add_argument("--output", required=True)
    confirm.add_argument("--deadline-seconds", type=float, default=1800.0)
    confirm.add_argument("--poll-seconds", type=float, default=10.0)
    handoff = commands.add_parser("handoff")
    handoff.add_argument("--verify", required=True)
    handoff.add_argument("--scheduler-plan", required=True)
    handoff.add_argument("--scheduler-readback", required=True)
    handoff.add_argument("--output", required=True)
    for command in (trigger, confirm, handoff):
        command.add_argument("--adapter-state", choices=_ADAPTER_STATES, default="adc")
    return parser


def _document(path, code):
    value, _digest = _load_json(path, code)
    return value.get("receipt", value)


def main(argv=None, *, adapter_factory=None, out=None) -> int:
    arguments = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    stream = out or sys.stdout
    build = adapter_factory or (lambda state: NativeAdapter(state, transport=None))
    adapter = build(arguments.adapter_state)
    try:
        # Nothing is sent until the artifact has somewhere to land: a refusal
        # to write after the run request went out leaves an execution running
        # with no document naming it.
        _output_available(arguments.output)
        if arguments.command == "trigger":
            result = trigger_verify_runtime(adapter=adapter)
        elif arguments.command == "confirm":
            import time

            receipt = confirm_execution(
                _document(arguments.trigger, "trigger_receipt_unusable"),
                adapter=adapter,
                clock=time.monotonic,
                sleep=time.sleep,
                deadline_seconds=arguments.deadline_seconds,
                poll_seconds=arguments.poll_seconds,
            )
            result = {"receipt": receipt, "record": list(adapter.record)}
        else:
            package = build_handoff(
                verify_receipt=_document(arguments.verify, "verify_receipt_unusable"),
                scheduler_plan=_document(arguments.scheduler_plan, "plan_invalid"),
                scheduler_readback=_document(
                    arguments.scheduler_readback, "scheduler_readback_unusable"
                ),
                adapter=adapter,
            )
            result = _self_verified(package)
    except (OSError, ValueError, NativeError) as error:
        # OSError is the transport that never answered, and that refusal can
        # follow a request that already left the client, so it is caught here
        # rather than escaping as a traceback with nothing written.
        refusal = _attached(
            {"status": "refused", "error": str(error)},
            arguments.output,
            _refusal_receipt(arguments.command, adapter, error),
        )
        stream.write(json.dumps(refusal) + "\n")
        return 1
    try:
        written = _written(arguments.output, result)
    except OSError as failure:
        refusal = _attached(
            {"status": "refused", "error": str(failure)}, arguments.output, result
        )
        stream.write(json.dumps(refusal) + "\n")
        return 1
    stream.write(
        json.dumps(
            {"status": "written", "command": arguments.command, "output": written}
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
