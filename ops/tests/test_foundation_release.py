import base64
import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ops.deploy import readback, refresh_question_policy, release
from ops.deploy.resource_guard import assert_allowed, load_resource_manifest

ROOT = Path(__file__).resolve().parents[2]
RELEASE_SCRIPT = ROOT / "ops" / "deploy" / "release.py"
ADAPTER_PATH = ROOT / "ops" / "tests" / "fixtures" / "release_fake_adapter.py"
MANIFEST_PATH = ROOT / "ops" / "deploy" / "resource_manifest.json"
MANIFEST_SHA256 = "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
DOC_PATH = ROOT / "docs" / "operations" / "release.md"

PROJECT = "ogilvy-trends-v2"
SERVICE = "listening-post-staging"
IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
AUDIENCE = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
SERVICE_API = f"projects/{PROJECT}/locations/us-central1/services/{SERVICE}"
QUEUE_API = (
    f"projects/{PROJECT}/locations/us-central1/queues/oi-general-question-staging"
)
SERVICE_RESOURCE = "//run.googleapis.com/" + SERVICE_API
QUEUE_RESOURCE = "//cloudtasks.googleapis.com/" + QUEUE_API
BUCKET_RESOURCE = (
    "//storage.googleapis.com/projects/_/buckets/listening-post-staging-cache"
)
IMAGE_NAME = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/listening-post-question"
PREFIX = "open-intelligence/v2/staging/general-questions/"
LEDGER_NAME = (
    PREFIX + "allowances/general_cultural_question_staging_eval_v1/ledger.json"
)
NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
VERIFIED_AT = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)
ENGINE_COMMIT = "e565290588e3720a79b6e1728c184650412a6f50"
LP_COMMIT = "0a4464a1dc4eb94a6775b4ba5d71780f2ab75319"
OLD_LP_COMMIT = "1111111111111111111111111111111111111111"
APP_MANIFEST = b"app image manifest"
NEW_IMAGE = hashlib.sha256(APP_MANIFEST).hexdigest()
OLD_IMAGE = "b" * 64
BUNDLE = "c" * 64
GATE_SHA = "d" * 64
RID = "8f2c3b4a-5d6e-4f70-8a9b-0c1d2e3f4a5b"
PARENT = "7e1b2a39-4c5d-4e6f-8a7b-9c0d1e2f3a4b"
ANCHOR = "6d0a1928-3b4c-4d5e-8f6a-7b8c9d0e1f2a"
APP_BUILD = (
    "projects/590353929363/locations/us-central1/builds/"
    "5f6df121-b82c-4d3c-8935-8a78d1f9b499"
)
ENGINE_BUILD = (
    "projects/590353929363/locations/us-central1/builds/"
    "889fd1ff-0f4e-4ea1-8c11-9fb1f5992308"
)

_LOADER = importlib.util.spec_from_file_location("release_fake_adapter", ADAPTER_PATH)
fake = importlib.util.module_from_spec(_LOADER)
_LOADER.loader.exec_module(fake)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return release.canonical_bytes(value)


def stamp(value):
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def make_policy(verified_at=VERIFIED_AT, **rates):
    return release.build_question_policy(pricing_verified_at=verified_at, **rates)


def make_binding(policy, *, revision, image_digest, lp_commit=LP_COMMIT):
    binding = {
        "contract_version": "general_question_deployment_v1",
        "project": PROJECT,
        "region": "us-central1",
        "service_name": SERVICE,
        "revision_name": revision,
        "service_account_email": IDENTITY,
        "lp_commit": lp_commit,
        "engine_bundle_digest": BUNDLE,
        "canonical_service_audience": AUDIENCE,
        "sdk_version": "2.20.0",
        "image_digest": image_digest,
        "worker_path": "/internal/general-question/execute",
        "policy_digest": policy["policy_digest"],
    }
    binding["deployment_digest"] = release.canonical_digest(binding)
    return binding


def make_ledger(binding, policy, *, requests=None, extra_bindings=()):
    requests = requests or {}
    bindings = {binding["deployment_digest"]: policy["policy_digest"]}
    for other_binding, other_policy in extra_bindings:
        bindings[other_binding["deployment_digest"]] = other_policy["policy_digest"]
    return {
        "contract_version": "general_question_allowance_v1",
        "allowance_id": "general_cultural_question_staging_eval_v1",
        "active_deployment_digest": binding["deployment_digest"],
        "bindings": bindings,
        "requests": requests,
        "reserved_microusd": len(requests) * 100_000,
    }


def make_activation(binding, policy, ledger):
    return {
        "state": "binding_created_ledger_refreshed_preserved_queue_paused",
        "binding": binding,
        "policy": policy,
        "reviewed_inputs": {
            "lp_commit": binding["lp_commit"],
            "engine_commit": ENGINE_COMMIT,
            "engine_bundle_digest": BUNDLE,
            "gate_sha256": GATE_SHA,
        },
        "queue_after": {"state": "PAUSED"},
        "build": {
            "status": "SUCCESS",
            "results": {
                "images": [
                    {
                        "name": IMAGE_NAME + ":" + binding["lp_commit"],
                        "digest": "sha256:" + binding["image_digest"],
                    }
                ]
            },
            "steps": [
                {"id": "build_question_image", "status": "SUCCESS", "args": []},
                {
                    "id": "verify_linux_boundary_tests",
                    "status": "SUCCESS",
                    "args": ["bash", "-c", "python /boundary/run_gate.py " + GATE_SHA],
                },
            ],
        },
        "control_after": ledger,
    }


def tag_for(engine_commit=ENGINE_COMMIT):
    return "question-" + engine_commit[:7]


def make_service(template_binding, tag, *, traffic_binding=None, traffic_tag=None):
    traffic_binding = traffic_binding or template_binding
    traffic_tag = traffic_tag or tag
    template = release.expected_template(template_binding, tag)
    targets = [
        {
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
            "revision": traffic_binding["revision_name"],
            "percent": 100,
            "tag": traffic_tag,
        }
    ]
    return {
        "name": SERVICE_API,
        "etag": "etag-before",
        "terminalCondition": {"type": "Ready", "state": "CONDITION_SUCCEEDED"},
        "template": template,
        "traffic": copy.deepcopy(targets),
        "trafficStatuses": copy.deepcopy(targets),
    }


def make_revision(binding, tag):
    template = release.expected_template(binding, tag)
    return {
        "name": SERVICE_API + "/revisions/" + binding["revision_name"],
        "serviceAccount": template["serviceAccount"],
        "containers": template["containers"],
        "timeout": template["timeout"],
        "scaling": template["scaling"],
        "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}],
    }


def make_request_row(policy, binding, *, admitted_at, execution=None, result=None):
    row = {
        "request_digest": sha(b"request"),
        "intake_digest": sha(b"intake"),
        "request_generation": "11",
        "intake_generation": "12",
        "client_scope_id": "client-scope",
        "policy_digest": policy["policy_digest"],
        "deployment_digest": binding["deployment_digest"],
        "admitted_at": stamp(admitted_at),
        "deadline_at": stamp(
            admitted_at + timedelta(seconds=policy["limits"]["deadline_seconds"])
        ),
        "reserved_microusd": 100_000,
    }
    if execution is not None:
        row["execution"] = execution
    if result is not None:
        row["result"] = result
    return row


def object_entry(raw, generation):
    return {"generation": str(generation), "raw_b64": fake.encode_raw(raw)}


def base_state(*, ledger, service, policies, bindings, tasks=(), queue_state="PAUSED"):
    objects = {LEDGER_NAME: object_entry(canonical(ledger), 40)}
    for policy in policies:
        name = PREFIX + f"policies/{policy['policy_digest']}/policy.json"
        objects[name] = object_entry(canonical(policy), 41)
    for binding in bindings:
        name = PREFIX + f"deployments/{binding['deployment_digest']}/binding.json"
        objects[name] = object_entry(canonical(binding), 42)
    return {
        "service": service,
        "revisions": {},
        "queue": {"name": QUEUE_API, "state": queue_state},
        "tasks": list(tasks),
        "objects": objects,
        "operations": {},
        "failures": {},
        "bytes": {},
        "journal": [],
        "reads": [],
        "next_generation": 100,
        "clock": {"now": NOW.isoformat(), "monotonic": 0.0},
    }


def write_json(path, value):
    path.write_text(json.dumps(value, indent=1), encoding="utf-8")
    return path


def authority(
    purpose, *, expires_at=NOW + timedelta(hours=2), manifest_sha256=MANIFEST_SHA256
):
    return {
        "contract_version": "42_release_authority_v1",
        "purpose": purpose,
        "resource_manifest_sha256": manifest_sha256,
        "granted_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": expires_at.isoformat(),
        "grantor": "operator",
    }


class Scenario:
    """A release scenario: old revision serving, new activation prepared, queue paused."""

    def __init__(self, tmp_path, *, ledger_requests=None):
        self.dir = tmp_path
        self.policy = make_policy()
        self.old_policy = make_policy(VERIFIED_AT - timedelta(hours=1))
        self.binding = make_binding(
            self.policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE
        )
        self.old_binding = make_binding(
            self.old_policy,
            revision=SERVICE + "-q-old",
            image_digest=OLD_IMAGE,
            lp_commit=OLD_LP_COMMIT,
        )
        self.tag = tag_for()
        self.old_tag = "question-old0000"
        self.ledger = make_ledger(
            self.binding,
            self.policy,
            requests=ledger_requests,
            extra_bindings=[(self.old_binding, self.old_policy)],
        )
        self.activation = make_activation(self.binding, self.policy, self.ledger)
        service = make_service(self.old_binding, self.old_tag)
        self.state = base_state(
            ledger=self.ledger,
            service=service,
            policies=[self.policy, self.old_policy],
            bindings=[self.binding, self.old_binding],
        )
        self.state["revisions"][SERVICE_API + "/revisions/" + SERVICE + "-q-old"] = (
            make_revision(self.old_binding, self.old_tag)
        )
        self.state_path = write_json(tmp_path / "state.json", self.state)
        self.activation_path = write_json(tmp_path / "activation.json", self.activation)
        self.authority_path = write_json(
            tmp_path / "authority.json", authority("release")
        )

    def clients(self):
        return fake.clients(self.state_path)

    def current(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def ledger_now(self):
        stored = self.current()["objects"][LEDGER_NAME]
        return json.loads(fake.decode_raw(stored["raw_b64"])), stored["generation"]

    def common(self):
        return [
            "--adapter",
            str(ADAPTER_PATH) + ":clients",
            "--adapter-state",
            str(self.state_path),
            "--resources",
            str(MANIFEST_PATH),
        ]


def run_cli(*args):
    completed = subprocess.run(
        [sys.executable, str(RELEASE_SCRIPT), *[str(item) for item in args]],
        capture_output=True,
        cwd=str(ROOT),
        timeout=180,
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    summary = None
    for line in stdout.splitlines():
        if line.startswith("{"):
            summary = json.loads(line)
    return completed.returncode, summary, stderr


def plan_release(scenario, output="release-plan.json"):
    path = scenario.dir / output
    code, summary, stderr = run_cli(
        "plan",
        "--activation",
        scenario.activation_path,
        "--authority",
        scenario.authority_path,
        "--output",
        path,
        *scenario.common(),
    )
    return code, summary, stderr, path


def apply_release(scenario, plan_path, output="release-result.json"):
    path = scenario.dir / output
    code, summary, stderr = run_cli(
        "apply", "--plan", plan_path, "--output", path, *scenario.common()
    )
    return code, summary, stderr, path


# Readback


def test_terminal_failure_is_not_polled_to_success():
    calls = []

    def reader(name, timeout_seconds):
        calls.append(name)
        return {"operation": name, "state": "failed", "error": "build_failed"}

    result = readback.wait_operation(
        reader,
        "build-42",
        deadline_seconds=30,
        poll_seconds=1,
        clock=lambda: 0,
        sleep=lambda seconds: None,
    )
    assert result["state"] == "failed"
    assert calls == ["build-42"]


def test_wrong_operation_identity_refuses_after_one_read():
    calls = []

    def reader(name, timeout_seconds):
        calls.append(name)
        return {"operation": "build-43", "state": "succeeded"}

    with pytest.raises(ValueError, match="operation_identity_mismatch"):
        readback.wait_operation(
            reader,
            "build-42",
            deadline_seconds=30,
            poll_seconds=1,
            clock=lambda: 0,
            sleep=lambda seconds: None,
        )
    assert calls == ["build-42"]


def test_delayed_success_is_reported_with_read_count_and_elapsed_time():
    ticks = iter(range(0, 100))
    states = iter(["pending", "pending", "unknown", "succeeded"])
    slept = []

    def reader(name, timeout_seconds):
        return {"operation": name, "state": next(states), "native": {"done": False}}

    result = readback.wait_operation(
        reader,
        "build-42",
        deadline_seconds=30,
        poll_seconds=2,
        clock=lambda: next(ticks),
        sleep=slept.append,
    )
    assert result["state"] == "succeeded"
    assert result["operation"] == "build-42"
    assert result["reads"] == 4
    assert slept == [2, 2, 2]
    assert result["elapsed_seconds"] > 0


def test_blocking_reader_consumes_budget_without_extra_reads():
    clock_value = [0.0]
    timeouts = []

    def clock():
        return clock_value[0]

    def reader(name, timeout_seconds):
        timeouts.append(timeout_seconds)
        clock_value[0] += 25.0
        return {"operation": name, "state": "pending"}

    result = readback.wait_operation(
        reader,
        "build-42",
        deadline_seconds=30,
        poll_seconds=1,
        clock=clock,
        sleep=lambda seconds: None,
    )
    assert result["state"] == "unproven"
    assert result["reads"] == 2
    assert timeouts[0] <= 30
    assert timeouts[1] <= 5
    assert all(timeout > 0 for timeout in timeouts)


def test_timeout_emits_unproven_never_success():
    ticks = iter(range(0, 1000, 5))
    calls = []

    def reader(name, timeout_seconds):
        calls.append(name)
        return {"operation": name, "state": "pending"}

    result = readback.wait_operation(
        reader,
        "build-42",
        deadline_seconds=30,
        poll_seconds=5,
        clock=lambda: next(ticks),
        sleep=lambda seconds: None,
    )
    assert result["state"] == "unproven"
    assert result["last_state"] == "pending"
    assert set(calls) == {"build-42"}
    assert result["resubmitted"] is False


def test_denied_read_stays_unknown_and_ends_unproven():
    ticks = iter(range(0, 1000, 10))
    calls = []

    def reader(name, timeout_seconds):
        calls.append(name)
        raise PermissionError("denied")

    result = readback.wait_operation(
        reader,
        "build-42",
        deadline_seconds=60,
        poll_seconds=10,
        clock=lambda: next(ticks),
        sleep=lambda seconds: None,
    )
    assert result["state"] == "unproven"
    assert result["last_state"] == "unknown"
    assert result["last_error"] == "PermissionError: denied"
    assert len(calls) >= 2


def test_a_reader_that_refuses_by_name_is_not_polled_as_an_unknown_state():
    """A refused request line is a refusal, never a state the loop can retry."""
    ticks = iter(range(0, 10000, 5))
    calls = []

    def reader(name, timeout_seconds):
        calls.append(name)
        raise ValueError("resource_name_invalid")

    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        readback.wait_operation(
            reader,
            "build-42",
            deadline_seconds=1800,
            poll_seconds=10,
            clock=lambda: next(ticks),
            sleep=lambda seconds: None,
        )
    assert calls == ["build-42"]


def test_retry_after_timeout_reconciles_same_operation_without_resubmission():
    ticks = iter(range(0, 1000, 10))
    calls = []

    def reader(name, timeout_seconds):
        calls.append(name)
        state = "succeeded" if len(calls) > 2 else "pending"
        return {"operation": name, "state": state}

    first = readback.wait_operation(
        reader,
        "build-42",
        deadline_seconds=30,
        poll_seconds=10,
        clock=lambda: next(ticks),
        sleep=lambda seconds: None,
    )
    assert first["state"] == "unproven"
    second = readback.wait_operation(
        reader,
        first["operation"],
        deadline_seconds=60,
        poll_seconds=10,
        clock=lambda: next(ticks),
        sleep=lambda seconds: None,
    )
    assert second["state"] == "succeeded"
    assert set(calls) == {"build-42"}
    assert second["resubmitted"] is False


def test_wait_operation_rejects_invalid_budgets():
    def reader(name, timeout_seconds):
        return {"operation": name, "state": "pending"}

    with pytest.raises(ValueError, match="deadline_invalid"):
        readback.wait_operation(
            reader,
            "x",
            deadline_seconds=0,
            poll_seconds=1,
            clock=lambda: 0,
            sleep=lambda s: None,
        )
    with pytest.raises(ValueError, match="operation_invalid"):
        readback.wait_operation(
            reader,
            "",
            deadline_seconds=1,
            poll_seconds=1,
            clock=lambda: 0,
            sleep=lambda s: None,
        )


# Ported invariants from the dated 20260909 scripts


def test_ported_constants_equal_dated_originals():
    assert release.SERVICE_NAME == "listening-post-staging"
    assert (
        release.IDENTITY
        == "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert release.AUDIENCE == "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
    assert release.WORKER_PATH == "/internal/general-question/execute"
    assert release.SERVICE_TIMEOUT == "300s"
    assert release.DISPATCH_DEADLINE == "270s"
    assert release.DRAIN_SECONDS == 125
    assert release.RECOVERY_REMAINING_SECONDS == 190
    assert release.RESUME_REMAINING_SECONDS == 180
    assert release.TAG_PATTERN == r"question-[a-z0-9-]{1,35}"
    assert release.TIMEOUT_SCOPE == {
        "engine_deadline_seconds": 240,
        "browser_wait_seconds": 240,
        "cloud_tasks_dispatch_seconds": 270,
        "cloud_run_timeout_seconds": 300,
    }
    assert release.QUESTION_DEADLINE_SECONDS == 240
    assert release.BUILD_STEPS == [
        "build_question_image",
        "verify_isolated_engine",
        "prepare_linux_boundary_test_wheels",
        "verify_linux_boundary_tests",
        "push_question_image",
    ]
    assert release.INTAKE_CONTRACT == "general_question_intake_context_v2"
    assert release.INVOCATION_CONTRACT == "general_cultural_question_v1"
    assert release.RESOURCE_LIMITS == {"cpu": "2", "memory": "4Gi"}
    assert release.MAX_INSTANCES == 1


def test_ported_environment_matches_dated_expected_map():
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    env = release.expected_environment(binding, tag_for())
    assert env == {
        "GENERAL_QUESTION_DEPLOYMENT_DIGEST": binding["deployment_digest"],
        "GENERAL_QUESTION_WORKER_URL": (
            "https://question-e565290---listening-post-staging-fibxg5ynpq-uc.a.run.app"
            "/internal/general-question/execute"
        ),
        "SOURCE_SHA": LP_COMMIT,
        "WEB_CONCURRENCY": "1",
        "GENERATION_MAX_CONCURRENT": "1",
        "DEPLOYMENT_PROFILE": "open-intelligence-staging",
        "GCP_PROJECT": "ogilvy-trends-v2",
        "BQ_DATASET": "trends_v2_staging",
        "CACHE_BUCKET": "listening-post-staging-cache",
        "CACHE_PREFIX": "open-intelligence/v2/staging/",
        "APPLICATION_SOURCE": "open-intelligence-staging",
    }
    with pytest.raises(ValueError, match="tag_invalid"):
        release.worker_url(binding, "Question-BAD")


def test_ported_verify_activation_and_verify_service_hold_dated_invariants(tmp_path):
    scenario = Scenario(tmp_path)
    binding = release.verify_activation(scenario.activation)
    assert binding == scenario.binding
    broken = copy.deepcopy(scenario.activation)
    broken["binding"]["image_digest"] = "e" * 64
    with pytest.raises(ValueError, match="activation_digest_mismatch"):
        release.verify_activation(broken)
    stale = copy.deepcopy(scenario.activation)
    stale["queue_after"]["state"] = "RUNNING"
    with pytest.raises(ValueError, match="activation_invalid"):
        release.verify_activation(stale)
    service = make_service(scenario.binding, scenario.tag)
    release.verify_service(service, scenario.binding, scenario.tag)
    wrong_timeout = copy.deepcopy(service)
    wrong_timeout["template"]["timeout"] = "600s"
    with pytest.raises(ValueError, match="service_mismatch"):
        release.verify_service(wrong_timeout, scenario.binding, scenario.tag)
    secret_env = copy.deepcopy(service)
    assert PASSCODE_ENTRY in secret_env["template"]["containers"][0]["env"]
    release.verify_service(secret_env, scenario.binding, scenario.tag)
    redacted = release.redacted_service(secret_env)
    assert "secretKeyRef" not in json.dumps(redacted)
    assert "UI_PASSCODE" in redacted["template"]["containers"][0]["env_names"]


# Carried environment: the serving revision's secret reference and cache TTL

PASSCODE_ENTRY = {
    "name": "UI_PASSCODE",
    "valueSource": {
        "secretKeyRef": {"secret": "ui-passcode-staging", "version": "latest"}
    },
}
CACHE_TTL_ENTRY = {"name": "CACHE_TTL", "value": "86400"}
BRIDGE_READS = "GENERAL_QUESTION_BRIDGE_READS"
BRIDGE_READS_ENTRY = {"name": BRIDGE_READS, "value": "enabled"}
BROKEN_CARRIED_ENTRIES = {
    "passcode_absent": ("UI_PASSCODE", None),
    "passcode_without_version": (
        "UI_PASSCODE",
        {"valueSource": {"secretKeyRef": {"secret": "ui-passcode-staging"}}},
    ),
    "passcode_other_secret": (
        "UI_PASSCODE",
        {
            "valueSource": {
                "secretKeyRef": {"secret": "ui-passcode-other", "version": "latest"}
            }
        },
    ),
    "passcode_plain_value": ("UI_PASSCODE", {"value": "SYNTHETIC-PASSCODE-VALUE"}),
    "cache_ttl_absent": ("CACHE_TTL", None),
    "cache_ttl_other_value": ("CACHE_TTL", {"value": "3600"}),
    "cache_ttl_as_secret": ("CACHE_TTL", dict(PASSCODE_ENTRY, name="CACHE_TTL")),
    "bridge_reads_absent": (BRIDGE_READS, None),
    "bridge_reads_other_value": (BRIDGE_READS, {"value": "disabled"}),
    "bridge_reads_empty": (BRIDGE_READS, {"value": ""}),
    "bridge_reads_as_secret": (BRIDGE_READS, dict(PASSCODE_ENTRY, name=BRIDGE_READS)),
}


def replace_entry(entries, name, entry):
    kept = [item for item in entries if item["name"] != name]
    return kept if entry is None else [*kept, {"name": name, **entry}]


def test_expected_template_carries_passcode_reference_and_cache_ttl():
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    tag = tag_for()
    entries = release.expected_template(binding, tag)["containers"][0]["env"]
    names = [item["name"] for item in entries]
    assert len(set(names)) == len(names) == 14
    plain = {item["name"]: item["value"] for item in entries if "value" in item}
    assert plain == {
        **release.expected_environment(binding, tag),
        "CACHE_TTL": "86400",
        BRIDGE_READS: "enabled",
    }
    assert [item for item in entries if "value" not in item] == [PASSCODE_ENTRY]
    assert "CACHE_TTL" in release.PLAIN_ENVIRONMENT_KEYS
    assert BRIDGE_READS in release.PLAIN_ENVIRONMENT_KEYS
    assert "UI_PASSCODE" not in release.PLAIN_ENVIRONMENT_KEYS
    release.verify_service(make_service(binding, tag), binding, tag)
    release.verify_revision(make_revision(binding, tag), binding, tag)


@pytest.mark.parametrize("case", sorted(BROKEN_CARRIED_ENTRIES))
def test_service_and_revision_refuse_a_template_missing_a_carried_entry(case):
    name, entry = BROKEN_CARRIED_ENTRIES[case]
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    tag = tag_for()
    service = make_service(binding, tag)
    container = service["template"]["containers"][0]
    container["env"] = replace_entry(container["env"], name, entry)
    with pytest.raises(ValueError, match="service_mismatch"):
        release.verify_service(service, binding, tag)
    revision = make_revision(binding, tag)
    container = revision["containers"][0]
    container["env"] = replace_entry(container["env"], name, entry)
    with pytest.raises(ValueError, match="revision_mismatch"):
        release.verify_revision(revision, binding, tag)


def test_redacted_service_records_the_passcode_reference_by_name_only():
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    redacted = release.redacted_service(make_service(binding, tag_for()))
    env = {item["name"]: item for item in redacted["template"]["containers"][0]["env"]}
    assert env["UI_PASSCODE"] == {"name": "UI_PASSCODE", "source": "secret"}
    assert env["CACHE_TTL"] == CACHE_TTL_ENTRY
    assert env[BRIDGE_READS] == BRIDGE_READS_ENTRY
    raw = json.dumps(redacted)
    assert "secretKeyRef" not in raw
    assert "ui-passcode-staging" not in raw
    assert "latest" not in raw


# Resource manifest


def test_reviewed_resource_manifest_bytes_are_present_and_guard_accepts_them():
    raw = MANIFEST_PATH.read_bytes()
    assert sha(raw) == MANIFEST_SHA256
    manifest = load_resource_manifest(MANIFEST_PATH, expected_sha256=MANIFEST_SHA256)
    assert_allowed(SERVICE_RESOURCE, "deploy", manifest)
    assert_allowed(QUEUE_RESOURCE, "write", manifest)
    assert_allowed(BUCKET_RESOURCE, "write", manifest)
    with pytest.raises(ValueError, match="resource_action_forbidden"):
        assert_allowed(SERVICE_RESOURCE, "write", manifest)


# Release plan, apply and verify through the CLI


def test_plan_is_read_only_and_records_before_state_and_idempotency_key(tmp_path):
    scenario = Scenario(tmp_path)
    code, summary, stderr, path = plan_release(scenario)
    assert code == 0, stderr
    assert summary["state"] == "planned"
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert plan["contract_version"] == "42_release_plan_v1"
    assert plan["kind"] == "release"
    assert plan["resource_manifest_sha256"] == MANIFEST_SHA256
    assert plan["authority"]["purpose"] == "release"
    assert plan["expected"]["revision_name"] == SERVICE + "-q-new"
    assert plan["expected"]["image"] == IMAGE_NAME + "@sha256:" + NEW_IMAGE
    assert plan["expected"]["lp_commit"] == LP_COMMIT
    assert plan["expected"]["policy_digest"] == scenario.policy["policy_digest"]
    assert (
        plan["expected"]["deployment_digest"] == scenario.binding["deployment_digest"]
    )
    assert plan["before"]["service"]["etag"] == "etag-before"
    assert plan["before"]["ledger"]["generation"] == "40"
    assert [change["operation"] for change in plan["changes"]] == [
        "deploy_revision",
        "route_traffic",
    ]
    assert all(change["resource"] == SERVICE_RESOURCE for change in plan["changes"])
    assert all(change["action"] == "deploy" for change in plan["changes"])
    without_key = {
        key: value for key, value in plan.items() if key != "idempotency_key"
    }
    assert plan["idempotency_key"] == sha(canonical(without_key))
    assert scenario.current()["journal"] == []
    assert scenario.current()["service"]["etag"] == "etag-before"


def test_output_is_write_once(tmp_path):
    scenario = Scenario(tmp_path)
    code, summary, stderr, path = plan_release(scenario)
    assert code == 0, stderr
    before = path.read_bytes()
    code, summary, stderr, path = plan_release(scenario)
    assert code != 0
    assert summary["error"] == "output_exists"
    assert path.read_bytes() == before


def test_plan_refuses_expired_authority(tmp_path):
    scenario = Scenario(tmp_path)
    write_json(
        scenario.authority_path,
        authority("release", expires_at=NOW - timedelta(minutes=1)),
    )
    code, summary, _stderr, path = plan_release(scenario)
    assert code != 0
    assert summary["error"] == "authority_expired"
    assert not path.exists()


@pytest.mark.parametrize(
    "name",
    [
        IMAGE_NAME,
        IMAGE_NAME + ":" + OLD_LP_COMMIT,
        IMAGE_NAME + ":" + LP_COMMIT[:7],
        IMAGE_NAME + ":latest",
        IMAGE_NAME + "@sha256:" + NEW_IMAGE,
    ],
    ids=["untagged", "other_revision", "short_revision", "floating_tag", "digest"],
)
def test_plan_refuses_activation_image_name_not_tagged_with_lp_commit(tmp_path, name):
    scenario = Scenario(tmp_path)
    activation = copy.deepcopy(scenario.activation)
    activation["build"]["results"]["images"][0]["name"] = name
    write_json(scenario.activation_path, activation)
    code, summary, _stderr, path = plan_release(scenario)
    assert code != 0
    assert summary["error"] == "activation_image_name_mismatch"
    assert not path.exists()


def test_plan_accepts_activation_image_name_tagged_with_lp_commit(tmp_path):
    scenario = Scenario(tmp_path)
    activation = copy.deepcopy(scenario.activation)
    activation["build"]["results"]["images"][0]["name"] = IMAGE_NAME + ":" + LP_COMMIT
    write_json(scenario.activation_path, activation)
    code, summary, stderr, path = plan_release(scenario)
    assert code == 0, stderr
    assert summary["state"] == "planned"
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert plan["expected"]["lp_commit"] == LP_COMMIT
    assert plan["expected"]["image"] == IMAGE_NAME + "@sha256:" + NEW_IMAGE


def test_apply_deploys_routes_and_verifies_with_guard_before_each_mutation(
    tmp_path, monkeypatch
):
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    clients = scenario.clients()
    state = clients["_state"]
    real_guard = release.resource_guard.assert_allowed

    def spy(resource, action, manifest):
        real_guard(resource, action, manifest)
        state.journal({"call": "guard", "resource": resource, "action": action})

    monkeypatch.setattr(release.resource_guard, "assert_allowed", spy)
    result_path = tmp_path / "release-result.json"
    code = release.execute(
        [
            "apply",
            "--plan",
            str(plan_path),
            "--output",
            str(result_path),
            "--resources",
            str(MANIFEST_PATH),
            "--adapter",
            "unused:unused",
            "--adapter-state",
            str(scenario.state_path),
        ],
        clients=clients,
    )
    assert code == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "traffic_verified_100_queue_paused"
    journal = [
        entry
        for entry in scenario.current()["journal"]
        if entry["call"] != "guard" or entry["action"] != "read"
    ]
    calls = [entry["call"] for entry in journal]
    assert calls == ["guard", "deploy_revision", "guard", "update_traffic"]
    assert (
        journal[0]["resource"] == SERVICE_RESOURCE and journal[0]["action"] == "deploy"
    )
    assert (
        journal[1]["idempotency_key"]
        == json.loads(plan_path.read_bytes())["idempotency_key"]
    )
    service = scenario.current()["service"]
    assert service["traffic"][0]["revision"] == SERVICE + "-q-new"
    assert service["trafficStatuses"][0]["tag"] == scenario.tag
    assert scenario.current()["queue"]["state"] == "PAUSED"
    assert result["operations"]["deploy_revision"]["state"] == "succeeded"
    assert result["operations"]["route_traffic"]["state"] == "succeeded"
    assert result["after"]["ledger"]["generation"] == "40"


def test_apply_refuses_changed_before_state_and_expired_authority(tmp_path):
    scenario = Scenario(tmp_path)
    code, summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    drifted = scenario.current()
    drifted["service"]["etag"] = "etag-drifted"
    write_json(scenario.state_path, drifted)
    code, summary, _stderr, _result_path = apply_release(scenario, plan_path)
    assert code != 0
    assert summary["error"] == "before_state_changed"
    assert scenario.current()["journal"] == []
    restored = scenario.current()
    restored["service"]["etag"] = "etag-before"
    restored["clock"]["now"] = (NOW + timedelta(hours=3)).isoformat()
    write_json(scenario.state_path, restored)
    code, summary, stderr, _result_path = apply_release(
        scenario, plan_path, "result-2.json"
    )
    assert code != 0
    assert summary["error"] == "authority_expired"
    assert scenario.current()["journal"] == []


def test_apply_refuses_tampered_plan(tmp_path):
    scenario = Scenario(tmp_path)
    code, summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    plan["expected"]["image_digest"] = "f" * 64
    write_json(plan_path, plan)
    code, summary, stderr, _result_path = apply_release(scenario, plan_path)
    assert code != 0
    assert summary["error"] == "idempotency_key_mismatch"
    assert scenario.current()["journal"] == []


def test_apply_unproven_operation_requires_reconciliation_not_resubmission(tmp_path):
    scenario = Scenario(tmp_path)
    code, summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    state = scenario.current()
    state["failures"]["deploy_pending_reads"] = 1000
    write_json(scenario.state_path, state)
    code, summary, stderr = run_cli(
        "apply",
        "--plan",
        plan_path,
        "--output",
        tmp_path / "result.json",
        "--deadline-seconds",
        "12",
        "--poll-seconds",
        "4",
        *scenario.common(),
    )
    assert code != 0
    assert summary["state"] == "unproven_requires_reconciliation"
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["operations"]["deploy_revision"]["state"] == "unproven"
    assert [entry["call"] for entry in scenario.current()["journal"]] == [
        "deploy_revision"
    ]
    code, summary, stderr = run_cli(
        "apply",
        "--plan",
        plan_path,
        "--output",
        tmp_path / "result.json",
        *scenario.common(),
    )
    assert code != 0 and summary["error"] == "output_exists"
    assert [entry["call"] for entry in scenario.current()["journal"]] == [
        "deploy_revision"
    ]


def test_verify_certifies_release_and_reports_unproven_drift(tmp_path):
    scenario = Scenario(tmp_path)
    code, summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    verified_path = tmp_path / "release-verified.json"
    code, summary, stderr = run_cli(
        "verify",
        "--plan",
        plan_path,
        "--result",
        result_path,
        "--output",
        verified_path,
        *scenario.common(),
    )
    assert code == 0, stderr
    verified = json.loads(verified_path.read_text(encoding="utf-8"))
    assert verified["state"] == "verified"
    assert (
        verified["native"]["revision"]["image"] == IMAGE_NAME + "@sha256:" + NEW_IMAGE
    )
    assert (
        verified["native"]["ledger"]["active_deployment_digest"]
        == (scenario.binding["deployment_digest"])
    )
    assert scenario.current()["journal"][-1]["call"] == "update_traffic"
    drifted = scenario.current()
    drifted["service"]["trafficStatuses"][0]["percent"] = 50
    write_json(scenario.state_path, drifted)
    code, summary, stderr = run_cli(
        "verify",
        "--plan",
        plan_path,
        "--result",
        result_path,
        "--output",
        tmp_path / "verified-2.json",
        *scenario.common(),
    )
    assert code != 0
    assert summary["state"] == "unproven"


def test_verify_binds_release_index_and_records_missing_bytes_as_drift(
    tmp_path, monkeypatch
):
    """Runs in process so the guard questions are recorded; since amendment e
    the approved manifest grants the three index reads, as the guard tests
    below spell out."""
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    index, references = release_index_fixture(scenario)
    install_index(scenario, index, references)
    index_path = write_json(tmp_path / "release-index.json", index)
    allowing_the_index_reads(monkeypatch)
    output = tmp_path / "verified.json"
    assert verify_with_index(scenario, plan_path, result_path, index_path, output) == 0
    verified = json.loads(output.read_text(encoding="utf-8"))
    assert verified["release_index_sha256"] == sha(index_path.read_bytes())
    assert verified["drift"] == []
    state = scenario.current()
    assert state["asset_origin"] == (
        "https://" + scenario.tag + "---" + AUDIENCE.removeprefix("https://")
    )
    engine_tree_name = index["evidence_objects"]["engine_tree"]["uri"].split("/", 3)[3]
    del state["objects"][engine_tree_name]
    write_json(scenario.state_path, state)
    second = tmp_path / "verified-2.json"
    assert verify_with_index(scenario, plan_path, result_path, index_path, second) == 1
    receipt = json.loads(second.read_text(encoding="utf-8"))
    assert receipt["state"] == "unproven"
    assert {
        "item": "release_index",
        "error": "release_index_bytes_unavailable",
    } in receipt["drift"]


INDEX_READS = None  # filled in below, once release is imported at call time


def index_read_guards():
    """The three reads a release index check adds on top of the service, the
    queue and the bucket."""
    return [
        (release.BUILDS_RESOURCE, "read"),
        (release.REGISTRY_RESOURCE, "read"),
        (SERVICE_RESOURCE, "invoke"),
    ]


def allowing_the_index_reads(monkeypatch):
    """Record every guard question and answer it with the real guard.

    Before amendment e this stood in for the missing manifest row by opening
    the gate for the three index reads. The approved manifest now grants all
    three, so nothing is opened: every read, the index reads included, goes
    through the real guard over the approved manifest.
    """
    asked = []
    real_guard = release.resource_guard.assert_allowed

    def spy(resource, action, manifest):
        asked.append((resource, action))
        real_guard(resource, action, manifest)

    monkeypatch.setattr(release.resource_guard, "assert_allowed", spy)
    return asked


def verify_with_index(scenario, plan_path, result_path, index_path, output):
    return release.execute(
        [
            "verify",
            "--plan",
            str(plan_path),
            "--result",
            str(result_path),
            "--release-index",
            str(index_path),
            "--output",
            str(output),
            "--resources",
            str(MANIFEST_PATH),
            "--adapter",
            "unused:unused",
            "--adapter-state",
            str(scenario.state_path),
        ],
        clients=scenario.clients(),
    )


def test_verify_guards_every_service_the_release_index_reads(tmp_path, monkeypatch):
    """Cloud Build, Artifact Registry and the tagged revision are three more
    services a verify run reads with the owner credential, so each is asked of
    the guard first. Since amendment e the approved manifest grants read on the
    builds collection, so the real guard lets the Cloud Build read through, and
    it is asked before any build record is read."""
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    index, references = release_index_fixture(scenario)
    install_index(scenario, index, references)
    index_path = write_json(tmp_path / "release-index.json", index)
    asked = []
    real_guard = release.resource_guard.assert_allowed

    def spy(resource, action, manifest):
        asked.append((resource, action))
        real_guard(resource, action, manifest)

    monkeypatch.setattr(release.resource_guard, "assert_allowed", spy)
    output = tmp_path / "verified.json"
    code = verify_with_index(scenario, plan_path, result_path, index_path, output)
    assert code == 0
    assert asked[-3:] == index_read_guards()
    assert index_read_guards()[0] == (release.BUILDS_RESOURCE, "read")
    # The reader was built after the guard, and the build record was read
    # through it: `commit` is the reference the Cloud Build read answers.
    assert scenario.current()["asset_origin"]
    verified = json.loads(output.read_text(encoding="utf-8"))
    assert verified["release_index"]["state"] == "verified"
    assert "commit" in verified["release_index"]["references_read"]


def test_verify_stops_before_any_build_read_when_the_manifest_lacks_the_builds_row(
    tmp_path, monkeypatch
):
    """The fail closed half of the gate, on a real guard over a real manifest:
    the approved manifest with its builds collection row removed refuses the
    Cloud Build read, and the run stops with no receipt rather than reading it
    anyway."""
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    index, references = release_index_fixture(scenario)
    install_index(scenario, index, references)
    index_path = write_json(tmp_path / "release-index.json", index)
    approved = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    without = copy.deepcopy(approved)
    without["resources"] = [
        row for row in without["resources"] if row["name"] != release.BUILDS_RESOURCE
    ]
    assert len(without["resources"]) == len(approved["resources"]) - 1
    asked = []
    real_guard = release.resource_guard.assert_allowed

    def spy(resource, action, manifest):
        asked.append((resource, action))
        real_guard(resource, action, without)

    monkeypatch.setattr(release.resource_guard, "assert_allowed", spy)
    output = tmp_path / "verified.json"
    code = verify_with_index(scenario, plan_path, result_path, index_path, output)
    assert code == 1
    assert not output.exists()
    assert asked[-1] == (release.BUILDS_RESOURCE, "read"), (
        "the refused read is the last thing attempted; nothing is read after it"
    )
    state = scenario.current()
    assert not [
        entry for entry in state["journal"] if entry["call"].startswith("get_build")
    ]
    # No byte reader was ever bound, so no build record could have been read.
    assert "asset_origin" not in state


def test_the_approved_manifest_expresses_the_cloud_build_read_on_the_builds_collection(
    tmp_path,
):
    """Amendment e names the Cloud Build read exactly. The location keeps
    `write` alone, the builds collection holds `read` alone, and the guard still
    refuses any other action set for the location and any single build row."""
    approved = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = {row["name"]: row["actions"] for row in approved["resources"]}
    assert release.BUILDS_RESOURCE == release.BUILD_RESOURCE + "/builds"
    assert rows[release.BUILD_RESOURCE] == ["write"]
    assert rows[release.BUILDS_RESOURCE] == ["read"]
    assert rows[release.REGISTRY_RESOURCE] == ["deploy", "read", "write"]
    assert "read" in rows[SERVICE_RESOURCE]
    assert "invoke" in rows[SERVICE_RESOURCE]
    amended = copy.deepcopy(approved)
    for row in amended["resources"]:
        if row["name"] == release.BUILD_RESOURCE:
            row["actions"] = ["read", "write"]
    path = tmp_path / "amended.json"
    raw = json.dumps(amended, indent=1, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="resource_manifest_invalid"):
        load_resource_manifest(path, expected_sha256=sha(raw))
    single_build = copy.deepcopy(approved)
    single_build["resources"].append(
        {"actions": ["read"], "name": release.BUILD_RESOURCE + "/builds/" + "a" * 8}
    )
    single_build["resources"].sort(key=lambda row: row["name"])
    other = tmp_path / "single-build.json"
    raw = json.dumps(single_build, indent=1, sort_keys=True).encode("utf-8")
    other.write_bytes(raw)
    with pytest.raises(ValueError, match="resource_manifest_invalid"):
        load_resource_manifest(other, expected_sha256=sha(raw))
    writable = copy.deepcopy(approved)
    for row in writable["resources"]:
        if row["name"] == release.BUILDS_RESOURCE:
            row["actions"] = ["read", "write"]
    third = tmp_path / "writable-builds.json"
    raw = json.dumps(writable, indent=1, sort_keys=True).encode("utf-8")
    third.write_bytes(raw)
    with pytest.raises(ValueError, match="resource_manifest_invalid"):
        load_resource_manifest(third, expected_sha256=sha(raw))


def test_verify_asks_for_the_three_extra_reads_only_when_an_index_is_given(
    tmp_path, monkeypatch
):
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    asked = allowing_the_index_reads(monkeypatch)
    code = release.execute(
        [
            "verify",
            "--plan",
            str(plan_path),
            "--result",
            str(result_path),
            "--output",
            str(tmp_path / "verified.json"),
            "--resources",
            str(MANIFEST_PATH),
            "--adapter",
            "unused:unused",
            "--adapter-state",
            str(scenario.state_path),
        ],
        clients=scenario.clients(),
    )
    assert code == 0
    assert asked == [
        (SERVICE_RESOURCE, "read"),
        (QUEUE_RESOURCE, "read"),
        (BUCKET_RESOURCE, "read"),
    ]
    index, references = release_index_fixture(scenario)
    install_index(scenario, index, references)
    index_path = write_json(tmp_path / "release-index.json", index)
    asked.clear()
    code = verify_with_index(
        scenario, plan_path, result_path, index_path, tmp_path / "verified-2.json"
    )
    assert code == 0
    assert asked == [
        (SERVICE_RESOURCE, "read"),
        (QUEUE_RESOURCE, "read"),
        (BUCKET_RESOURCE, "read"),
        *index_read_guards(),
    ]


def test_verify_records_index_failure_as_drift_and_still_writes_the_receipt(
    tmp_path, monkeypatch
):
    """Every other check records drift and the receipt still lands. An index
    that does not resolve must behave the same way, or one failed reference
    voids the record of everything verify did prove."""
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    index, references = release_index_fixture(scenario)
    install_index(scenario, index, references)
    state = scenario.current()
    del state["objects"][
        index["evidence_objects"]["authority:activation_ledger"]["uri"].split("/", 3)[3]
    ]
    write_json(scenario.state_path, state)
    index_path = write_json(tmp_path / "release-index.json", index)
    allowing_the_index_reads(monkeypatch)
    output = tmp_path / "verified.json"
    code = verify_with_index(scenario, plan_path, result_path, index_path, output)
    assert code == 1
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["state"] == "unproven"
    assert {
        "item": "release_index",
        "error": "release_index_bytes_unavailable",
    } in receipt["drift"]
    assert receipt["native"]["ledger"]["generation"] == "40"
    assert receipt["release_index"]["sha256"] == sha(index_path.read_bytes())
    assert receipt["release_index"]["state"] == "unproven"


def test_verify_receipt_records_which_references_the_index_check_read(
    tmp_path, monkeypatch
):
    """A twelve check run and a thirteen check run must not read alike."""
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    index, references = release_index_fixture(scenario)
    install_index(scenario, index, references)
    allowing_the_index_reads(monkeypatch)
    index_path = write_json(tmp_path / "release-index.json", index)
    output = tmp_path / "verified.json"
    code = verify_with_index(scenario, plan_path, result_path, index_path, output)
    assert code == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    record = receipt["release_index"]
    assert record["state"] == "verified"
    assert record["references_read"] == release.release_index_references(index)
    assert len(record["references_read"]) == 14
    assert record["counts"] == {
        "contract_digests": 1,
        "evidence_objects": 8,
        "raw_authority_objects": 2,
        "served_assets": 1,
    }
    assert record["schema_version"] == "42_staging_release_v3"
    thinner = copy.deepcopy(index)
    thinner["served_assets"] = {}
    thinner_path = write_json(tmp_path / "release-index-2.json", thinner)
    other = tmp_path / "verified-2.json"
    code = verify_with_index(scenario, plan_path, result_path, thinner_path, other)
    assert code == 0
    thin_record = json.loads(other.read_text(encoding="utf-8"))["release_index"]
    assert len(thin_record["references_read"]) == 13
    assert thin_record["counts"]["served_assets"] == 0
    assert thin_record["references_read"] != record["references_read"]


def test_documented_commands_only_name_existing_flags():
    text = DOC_PATH.read_text(encoding="utf-8")
    documented = {}
    for match in re.finditer(
        r"release\.py (plan|apply|verify|resume|rollback-plan|rollback-apply)((?:\s+--[a-z-]+(?:\s+[^\s`-][^\s`]*)?)+)",
        text,
    ):
        documented.setdefault(match.group(1), set()).update(
            re.findall(r"--[a-z-]+", match.group(2))
        )
    assert set(documented) == {
        "plan",
        "apply",
        "verify",
        "resume",
        "rollback-plan",
        "rollback-apply",
    }
    for command, flags in documented.items():
        completed = subprocess.run(
            [sys.executable, str(RELEASE_SCRIPT), command, "--help"],
            capture_output=True,
            cwd=str(ROOT),
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        help_text = completed.stdout.decode("utf-8", "replace")
        for flag in flags:
            assert flag in help_text, (command, flag)
        assert "--plan" in help_text or command in {"plan", "rollback-plan"}
        assert "--output" in help_text
    completed = subprocess.run(
        [
            sys.executable,
            str(RELEASE_SCRIPT),
            "apply",
            "--plan",
            "x",
            "--output",
            "y",
            "--bogus",
        ],
        capture_output=True,
        cwd=str(ROOT),
        timeout=120,
    )
    assert completed.returncode == 2


# Queue resume


def released_scenario(tmp_path, **kwargs):
    scenario = Scenario(tmp_path, **kwargs)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    state = scenario.current()
    state["clock"]["now"] = (NOW + timedelta(seconds=200)).isoformat()
    write_json(scenario.state_path, state)
    scenario.route_path = result_path
    return scenario


def plan_resume(
    scenario, mode, *extra, output="resume-plan.json", route=None, activation=True
):
    path = scenario.dir / output
    activation_flags = ["--activation", scenario.activation_path] if activation else []
    code, summary, stderr = run_cli(
        "plan",
        "--kind",
        "resume",
        "--mode",
        mode,
        "--route",
        route or scenario.route_path,
        *activation_flags,
        "--authority",
        scenario.authority_path,
        "--output",
        path,
        *extra,
        *scenario.common(),
    )
    return code, summary, stderr, path


def resume_queue(scenario, plan_path, output="resume-result.json"):
    path = scenario.dir / output
    code, summary, stderr = run_cli(
        "resume", "--plan", plan_path, "--output", path, *scenario.common()
    )
    return code, summary, stderr, path


def test_empty_resume_runs_queue_after_all_preconditions(tmp_path):
    scenario = released_scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_resume(scenario, "empty")
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    assert plan["kind"] == "resume"
    assert plan["resume"] == {
        "mode": "empty",
        "pending_request": None,
        "parent_request": None,
        "anchor_request": None,
    }
    code, _summary, stderr, result_path = resume_queue(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "running_verified"
    assert result["drain_seconds"] >= 125
    assert scenario.current()["queue"]["state"] == "RUNNING"
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert calls[-1] == "resume_queue"


def test_empty_resume_refuses_pending_flags_and_recovery_requires_all_ids(tmp_path):
    scenario = released_scenario(tmp_path)
    code, summary, _stderr, _plan_path = plan_resume(
        scenario, "empty", "--pending-request", RID
    )
    assert code != 0
    assert summary["error"] == "pending_flags_forbidden"
    code, summary, _stderr, _plan_path = plan_resume(
        scenario,
        "recovery",
        "--pending-request",
        RID,
        "--parent-request",
        PARENT,
        output="r.json",
    )
    assert code != 0
    assert summary["error"] == "pending_request_required"
    code, summary, _stderr, _plan_path = plan_resume(
        scenario, "drain-expired", output="d.json"
    )
    assert code != 0
    assert summary["error"] == "pending_request_required"


def test_resume_refuses_drain_route_enumeration_audience_and_ledger_faults(tmp_path):
    scenario = released_scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_resume(scenario, "empty")
    assert code == 0, stderr

    def attempt(mutate, output):
        state = scenario.current()
        mutate(state)
        write_json(scenario.state_path, state)
        code, summary, _stderr, _result_path = resume_queue(scenario, plan_path, output)
        assert code != 0
        assert scenario.current()["queue"]["state"] == "PAUSED"
        calls = [entry["call"] for entry in scenario.current()["journal"]]
        assert "resume_queue" not in calls
        return summary["error"]

    def early(state):
        state["clock"]["now"] = (NOW + timedelta(seconds=30)).isoformat()

    assert attempt(early, "a.json") == "drain_not_elapsed"

    def restore_clock(state):
        state["clock"]["now"] = (NOW + timedelta(seconds=200)).isoformat()
        state["service"]["trafficStatuses"][0]["percent"] = 99

    assert attempt(restore_clock, "b.json") == "route_not_terminal"

    def endless(state):
        state["service"]["trafficStatuses"][0]["percent"] = 100
        state["tasks_endless_pages"] = True

    assert attempt(endless, "c.json") == "task_enumeration_incomplete"

    def foreign_task(state):
        state["tasks_endless_pages"] = False
        state["tasks"] = [
            {
                "name": QUEUE_API + "/tasks/question_" + RID.replace("-", ""),
                "dispatchCount": 0,
                "dispatchDeadline": "270s",
                "httpRequest": {
                    "httpMethod": "POST",
                    "url": "https://elsewhere.example/execute",
                    "oidcToken": {
                        "serviceAccountEmail": IDENTITY,
                        "audience": "https://elsewhere.example",
                    },
                    "body": "",
                },
            }
        ]

    assert attempt(foreign_task, "d.json") == "task_audience_mismatch"

    def foreign_ledger(state):
        state["tasks"] = []
        ledger = json.loads(fake.decode_raw(state["objects"][LEDGER_NAME]["raw_b64"]))
        ledger["active_deployment_digest"] = scenario.old_binding["deployment_digest"]
        state["objects"][LEDGER_NAME] = object_entry(canonical(ledger), 77)

    assert attempt(foreign_ledger, "e.json") == "ledger_incompatible"


def pending_fixture(scenario, *, admitted_at, binding=None, policy=None, tag=None):
    """Return a ledger row, task and objects for one pending request bound to a revision."""
    binding = binding or scenario.binding
    policy = policy or scenario.policy
    tag = tag or scenario.tag
    row = make_request_row(policy, binding, admitted_at=admitted_at)
    capsule = {
        "child_request_id": RID,
        "child_request_digest": row["request_digest"],
        "parent": {"request_id": PARENT},
        "anchor": {"request_id": ANCHOR},
    }
    capsule_raw = canonical(capsule)
    intake = {
        "contract_version": "general_question_intake_context_v2",
        "request_id": RID,
        "request_digest": row["request_digest"],
        "parent_context_ref": {
            "parent": {"request_id": PARENT},
            "anchor": {"request_id": ANCHOR},
            "context_generation": "55",
            "context_digest": sha(capsule_raw),
        },
    }
    intake["intake_digest"] = sha(canonical(intake))
    row["intake_digest"] = intake["intake_digest"]
    invocation = {
        "contract_version": "general_cultural_question_v1",
        "request_id": RID,
        "request_digest": row["request_digest"],
        "intake_digest": row["intake_digest"],
        "policy_digest": row["policy_digest"],
        "deployment_digest": row["deployment_digest"],
    }
    task = {
        "name": QUEUE_API + "/tasks/question_" + RID.replace("-", ""),
        "dispatchCount": 0,
        "dispatchDeadline": "270s",
        "httpRequest": {
            "httpMethod": "POST",
            "url": release.worker_url(binding, tag),
            "oidcToken": {"serviceAccountEmail": IDENTITY, "audience": AUDIENCE},
            "body": base64.b64encode(canonical(invocation)).decode("ascii"),
        },
    }
    objects = {
        PREFIX + f"requests/{RID}/intake.json": object_entry(
            canonical(intake), row["intake_generation"]
        ),
        PREFIX + f"requests/{RID}/parent_context.json": object_entry(capsule_raw, 55),
    }
    return row, task, objects


def test_recovery_resume_requires_pending_request_with_parent_and_anchor(tmp_path):
    scenario = released_scenario(tmp_path)
    resume_now = NOW + timedelta(seconds=200)
    row, task, objects = pending_fixture(
        scenario, admitted_at=resume_now - timedelta(seconds=10)
    )
    state = scenario.current()
    ledger = json.loads(fake.decode_raw(state["objects"][LEDGER_NAME]["raw_b64"]))
    ledger["requests"][RID] = row
    ledger["reserved_microusd"] += 100_000
    state["objects"][LEDGER_NAME] = object_entry(canonical(ledger), 40)
    state["objects"].update(objects)
    state["tasks"] = [task]
    write_json(scenario.state_path, state)
    code, _summary, stderr, plan_path = plan_resume(
        scenario,
        "recovery",
        "--pending-request",
        RID,
        "--parent-request",
        PARENT,
        "--anchor-request",
        ANCHOR,
    )
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    assert (
        plan["resume"]["mode"] == "recovery"
        and plan["resume"]["pending_request"] == RID
    )
    code, _summary, stderr, result_path = resume_queue(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "running_verified"
    assert result["pending_request"] == RID
    assert result["reserved_microusd"] == 100_000
    assert scenario.current()["queue"]["state"] == "RUNNING"


def test_expired_drain_requires_unexecuted_expired_request(tmp_path):
    scenario = released_scenario(tmp_path)
    resume_now = NOW + timedelta(seconds=200)
    row, task, objects = pending_fixture(
        scenario, admitted_at=resume_now - timedelta(seconds=10)
    )
    state = scenario.current()
    ledger = json.loads(fake.decode_raw(state["objects"][LEDGER_NAME]["raw_b64"]))
    ledger["requests"][RID] = row
    ledger["reserved_microusd"] += 100_000
    state["objects"][LEDGER_NAME] = object_entry(canonical(ledger), 40)
    state["objects"].update(objects)
    state["tasks"] = [task]
    write_json(scenario.state_path, state)
    code, summary, stderr, plan_path = plan_resume(
        scenario,
        "drain-expired",
        "--pending-request",
        RID,
        "--parent-request",
        PARENT,
        "--anchor-request",
        ANCHOR,
    )
    assert code != 0
    assert summary["error"] == "request_not_expired"
    state = scenario.current()
    state["clock"]["now"] = (resume_now + timedelta(seconds=600)).isoformat()
    write_json(scenario.state_path, state)
    code, summary, stderr, plan_path = plan_resume(
        scenario,
        "drain-expired",
        "--pending-request",
        RID,
        "--parent-request",
        PARENT,
        "--anchor-request",
        ANCHOR,
        output="drain-plan.json",
    )
    assert code == 0, stderr
    code, summary, stderr, result_path = resume_queue(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "running_verified" and result["drain_expired"] is True


# Rollback


def rollback_scenario(tmp_path, *, requests=None):
    scenario = released_scenario(tmp_path, ledger_requests=requests)
    scenario.rollback_authority = write_json(
        tmp_path / "rollback-authority.json", authority("rollback")
    )
    return scenario


def plan_rollback(scenario, target=SERVICE + "-q-old", output="rollback-plan.json"):
    path = scenario.dir / output
    code, summary, stderr = run_cli(
        "rollback-plan",
        "--target-revision",
        target,
        "--authority",
        scenario.rollback_authority,
        "--output",
        path,
        *scenario.common(),
    )
    return code, summary, stderr, path


def apply_rollback(scenario, plan_path, output="rollback-result.json"):
    path = scenario.dir / output
    code, summary, stderr = run_cli(
        "rollback-apply", "--plan", plan_path, "--output", path, *scenario.common()
    )
    return code, summary, stderr, path


def terminal_requests(scenario_policy, scenario_binding):
    completed = make_request_row(
        scenario_policy,
        scenario_binding,
        admitted_at=NOW - timedelta(hours=1),
        execution={"calls": [], "queries": []},
        result={"digest": sha(b"completed"), "generation": "61"},
    )
    held = make_request_row(
        scenario_policy,
        scenario_binding,
        admitted_at=NOW - timedelta(minutes=30),
        result={"digest": sha(b"held"), "generation": "62"},
    )
    return {RID: completed, PARENT: held}


def test_rollback_preserves_completed_and_held_requests_and_swaps_only_active_binding(
    tmp_path,
):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    ledger_before, generation_before = scenario.ledger_now()
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    assert plan["kind"] == "rollback"
    assert plan["target"]["revision_name"] == SERVICE + "-q-old"
    assert plan["target"]["image_digest"] == OLD_IMAGE
    assert (
        plan["target"]["deployment_digest"] == scenario.old_binding["deployment_digest"]
    )
    assert plan["classification"] == {
        "completed": [RID],
        "held": [PARENT],
        "in_flight": [],
        "queued": [],
        "expired_unexecuted": [],
    }
    assert scenario.current()["journal"][-1]["call"] == "update_traffic"
    code, _summary, stderr, result_path = apply_rollback(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "rollback_verified_queue_paused"
    ledger_after, generation_after = scenario.ledger_now()
    assert generation_after != generation_before
    assert canonical(ledger_after["requests"]) == canonical(ledger_before["requests"])
    assert (
        ledger_after["reserved_microusd"]
        == ledger_before["reserved_microusd"]
        == 200_000
    )
    assert (
        ledger_after["active_deployment_digest"]
        == scenario.old_binding["deployment_digest"]
    )
    assert ledger_after["bindings"] == ledger_before["bindings"]
    service = scenario.current()["service"]
    assert service["trafficStatuses"][0]["revision"] == SERVICE + "-q-old"
    assert service["template"]["revision"] == SERVICE + "-q-new"
    assert scenario.current()["queue"]["state"] == "PAUSED"
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert calls[-2:] == ["create_object", "update_traffic"]
    assert result["activation"]["if_generation_match"] == int(generation_before)
    assert result["resume_eligibility"]["eligible"] is True


def test_rollback_refuses_in_flight_request_under_new_timeout(tmp_path):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    in_flight = make_request_row(
        policy,
        binding,
        admitted_at=NOW - timedelta(seconds=30),
        execution={"calls": [{"stage": "planning"}], "queries": []},
    )
    scenario = rollback_scenario(tmp_path, requests={RID: in_flight})
    ledger_before, generation_before = scenario.ledger_now()
    code, summary, _stderr, _plan_path = plan_rollback(scenario)
    assert code != 0
    assert summary["error"] == "rollback_in_flight_request"
    assert scenario.ledger_now() == (ledger_before, generation_before)
    assert scenario.current()["queue"]["state"] == "PAUSED"


def test_rollback_refuses_stale_ledger_generation_without_losing_requests(tmp_path):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    code, summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    state = scenario.current()
    ledger = json.loads(fake.decode_raw(state["objects"][LEDGER_NAME]["raw_b64"]))
    state["objects"][LEDGER_NAME] = object_entry(canonical(ledger), 90)
    write_json(scenario.state_path, state)
    code, summary, stderr, _result_path = apply_rollback(scenario, plan_path)
    assert code != 0
    assert summary["error"] == "before_state_changed"
    assert scenario.ledger_now() == (ledger, "90")
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert "create_object" not in calls
    assert (
        scenario.current()["service"]["trafficStatuses"][0]["revision"]
        == SERVICE + "-q-new"
    )


def test_rollback_refuses_incompatible_schema_and_keeps_forward_recovery_plan(tmp_path):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    state = scenario.current()
    name = (
        PREFIX + f"deployments/{scenario.old_binding['deployment_digest']}/binding.json"
    )
    stale_binding = copy.deepcopy(scenario.old_binding)
    stale_binding["contract_version"] = "general_question_deployment_v0"
    state["objects"][name] = object_entry(canonical(stale_binding), 42)
    write_json(scenario.state_path, state)
    ledger_before = scenario.ledger_now()
    code, summary, _stderr, plan_path = plan_rollback(scenario)
    assert code != 0
    assert summary["error"] == "rollback_incompatible_schema"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["state"] == "refused"
    assert plan["forward_recovery_plan"]["queue"] == "paused"
    assert set(plan["forward_recovery_plan"]["preserved_requests"]) == {RID, PARENT}
    assert scenario.ledger_now() == ledger_before
    assert scenario.current()["queue"]["state"] == "PAUSED"


def test_rollback_refuses_missing_old_revision(tmp_path):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    ledger_before, generation_before = scenario.ledger_now()
    code, summary, _stderr, plan_path = plan_rollback(
        scenario, target=SERVICE + "-q-gone"
    )
    assert code != 0
    assert summary["error"] == "rollback_target_missing"
    assert scenario.current()["journal"][-1]["call"] == "update_traffic"
    ledger_after, generation_after = scenario.ledger_now()
    assert generation_after == generation_before
    assert canonical(ledger_after["requests"]) == canonical(ledger_before["requests"])
    assert (
        ledger_after["reserved_microusd"]
        == ledger_before["reserved_microusd"]
        == 200_000
    )
    refusal = json.loads(plan_path.read_text(encoding="utf-8"))
    assert refusal["state"] == "refused"
    assert set(refusal["forward_recovery_plan"]["preserved_requests"]) == {RID, PARENT}
    assert scenario.current()["queue"]["state"] == "PAUSED"


def test_rollback_route_failure_keeps_queue_paused_and_requests_intact(tmp_path):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    ledger_before, _generation_before = scenario.ledger_now()
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    state = scenario.current()
    state["failures"]["update_traffic"] = "failed"
    write_json(scenario.state_path, state)
    code, _summary, stderr, result_path = apply_rollback(scenario, plan_path)
    assert code != 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "failed_or_unproven_requires_native_reconciliation"
    assert result["operations"]["route_traffic"]["state"] == "failed"
    ledger_after, _generation_after = scenario.ledger_now()
    assert canonical(ledger_after["requests"]) == canonical(ledger_before["requests"])
    assert ledger_after["reserved_microusd"] == ledger_before["reserved_microusd"]
    assert scenario.current()["queue"]["state"] == "PAUSED"
    assert result["forward_recovery_plan"]["queue"] == "paused"


# Release index


def release_index_fixture(scenario):
    engine_tree = b"engine tree bytes"
    app_tree = b"app tree bytes"
    ops_tree = b"ops tree bytes"
    engine_manifest = b"engine image manifest"
    app_manifest = APP_MANIFEST
    contract = b"question contract"
    binding_object = b"raw binding object"
    activation_ledger = b"raw activation ledger"
    ledger_before = b"raw ledger before the activation"
    asset = b"served asset"
    policy_raw = canonical(
        {key: value for key, value in scenario.policy.items() if key != "policy_digest"}
    )
    binding_raw = canonical(
        {
            key: value
            for key, value in scenario.binding.items()
            if key != "deployment_digest"
        }
    )
    payloads = {
        "engine_tree": engine_tree,
        "app_tree": app_tree,
        "ops_tree": ops_tree,
        "resource_manifest": MANIFEST_PATH.read_bytes(),
        "contract:question_timeout": contract,
        "authority:deployment_binding": binding_object,
        "authority:activation_ledger": activation_ledger,
        "source_binding": ledger_before,
    }
    index = {
        "schema_version": "42_staging_release_v3",
        "repository": "https://github.com/jhbanalytics-pixel/42-Ogilvy-Intelligence",
        "commit": LP_COMMIT,
        "app_build": APP_BUILD,
        "engine_build": ENGINE_BUILD,
        "engine_tree_sha256": sha(engine_tree),
        "app_tree_sha256": sha(app_tree),
        "ops_tree_sha256": sha(ops_tree),
        "engine_image": IMAGE_NAME.replace("listening-post-question", "engine")
        + "@sha256:"
        + sha(engine_manifest),
        "app_image": IMAGE_NAME + "@sha256:" + sha(app_manifest),
        "resource_manifest_sha256": MANIFEST_SHA256,
        "policy_digest": scenario.policy["policy_digest"],
        "deployment_digest": scenario.binding["deployment_digest"],
        "contract_digests": {"question_timeout": sha(contract)},
        "raw_authority_objects": [
            {
                "name": "deployment_binding",
                "uri": "gs://listening-post-staging-cache/"
                + PREFIX
                + f"deployments/{scenario.binding['deployment_digest']}/binding.json",
                "generation": "42",
                "sha256": sha(binding_object),
            },
            {
                "name": "activation_ledger",
                "uri": "gs://listening-post-staging-cache/" + LEDGER_NAME,
                "generation": "40",
                "sha256": sha(activation_ledger),
            },
        ],
        "evidence_mode": "retained_baseline",
        "evidence_objects": {
            reference: {
                "uri": "gs://listening-post-staging-cache/"
                + release.evidence_object_name(LP_COMMIT, reference),
                "generation": str(50 + number),
                "sha256": sha(raw),
            }
            for number, (reference, raw) in enumerate(sorted(payloads.items()))
        },
        "source_binding": {
            "uri": "gs://listening-post-staging-cache/" + LEDGER_NAME,
            "generation": "39",
            "sha256": sha(ledger_before),
        },
        "served_assets": {"app.js": sha(asset)},
    }
    references = {
        "commit": LP_COMMIT.encode(),
        "engine_image": engine_manifest,
        "app_image": app_manifest,
        "policy": policy_raw,
        "deployment": binding_raw,
        "asset:app.js": asset,
        **payloads,
    }
    return index, references


def install_index(scenario, index, references):
    """Serve the index's bytes through the fake adapter the way the native reader
    finds them: pinned objects in the objects store, the rest by reference. The
    ledger object is left exactly as the scenario has it, because no index
    reference may resolve through that mutable name."""
    state = scenario.current()
    for reference, item in index["evidence_objects"].items():
        name = item["uri"].split("/", 3)[3]
        state["objects"][name] = object_entry(references[reference], item["generation"])
    state["bytes"] = {
        key: fake.encode_raw(value)
        for key, value in references.items()
        if key in ("commit", "engine_image", "app_image") or key.startswith("asset:")
    }
    write_json(scenario.state_path, state)
    return state


def test_validate_release_index_checks_every_value_against_bytes(tmp_path):
    scenario = Scenario(tmp_path)
    index, references = release_index_fixture(scenario)
    validated = release.validate_release_index(index, byte_reader=references.get)
    assert validated == index
    assert validated is not index
    missing = dict(references)
    del missing["asset:app.js"]
    with pytest.raises(ValueError, match="release_index_bytes_unavailable"):
        release.validate_release_index(index, byte_reader=missing.get)
    wrong = dict(references)
    wrong["ops_tree"] = b"other bytes"
    with pytest.raises(ValueError, match="release_index_digest_mismatch"):
        release.validate_release_index(index, byte_reader=wrong.get)
    for field, value in (
        ("schema_version", "42_staging_release_v1"),
        ("evidence_mode", "hourly_capture"),
        ("commit", "not-hex"),
        ("repository", "https://github.com/other/repo"),
    ):
        broken = copy.deepcopy(index)
        broken[field] = value
        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index(broken, byte_reader=references.get)
    extra = copy.deepcopy(index)
    extra["revision"] = "guessed"
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index(extra, byte_reader=references.get)
    binding = copy.deepcopy(index)
    binding["source_binding"] = {"uri": "gs://x/y", "sha256": "0" * 64}
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index(binding, byte_reader=references.get)


def test_release_index_object_references_must_name_the_approved_bucket(tmp_path):
    """A raw authority object or source binding outside the approved evidence
    bucket is refused by the grammar, before any byte reader sees the URI."""
    scenario = Scenario(tmp_path)
    index, references = release_index_fixture(scenario)
    foreign = "another-projects-bucket"
    for field in ("raw_authority_objects", "source_binding"):
        broken = copy.deepcopy(index)
        entry = broken[field][0] if field == "raw_authority_objects" else broken[field]
        entry["uri"] = entry["uri"].replace(release.BUCKET, foreign)
        reads = []

        def reader(reference, reads=reads):
            reads.append(reference)
            return references.get(reference.replace(foreign, release.BUCKET))

        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index(broken, byte_reader=reader)
        assert reads == []
        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index_shape(broken)


# Pricing renewal controller


PRICING_SOURCE = refresh_question_policy.PRICING_SOURCE
PRICING_PAGE = (
    b"<html>approved flash model input 0.75 output 3.75 USD global standard</html>"
)


def observation(
    *,
    observed_at,
    input_rate="0.750000",
    output_rate="3.750000",
    page=PRICING_PAGE,
    model=None,
):
    if model is None:
        model = make_policy()["model"]
    return {
        "contract_version": "42_pricing_observation_v1",
        "observed_at": stamp(observed_at),
        "source_url": PRICING_SOURCE,
        "source_sha256": sha(page),
        "model": model,
        "location": "global",
        "service_tier": "standard",
        "currency": "USD",
        "input_usd_per_million": input_rate,
        "output_usd_per_million": output_rate,
    }


def renewal_grant(expires_at=NOW + timedelta(days=30)):
    return {
        "contract_version": "42_renewal_grant_v1",
        "purpose": "pricing_renewal",
        "resource_manifest_sha256": MANIFEST_SHA256,
        "granted_at": (NOW - timedelta(days=1)).isoformat(),
        "expires_at": expires_at.isoformat(),
        "grantor": "operator",
    }


class Renewal:
    def __init__(self, tmp_path):
        self.scenario = released_scenario(tmp_path)
        self.index, references = release_index_fixture(self.scenario)
        state = self.scenario.current()
        service = state["service"]
        current_revision = (
            service["name"] + "/revisions/" + service["template"]["revision"]
        )
        service["reconciling"] = False
        service["generation"] = "1"
        service["observedGeneration"] = "1"
        service["latestCreatedRevision"] = current_revision
        service["latestReadyRevision"] = current_revision
        state["clock"]["now"] = NOW.isoformat()
        state["bytes"] = {
            key: fake.encode_raw(value) for key, value in references.items()
        }
        state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(PRICING_PAGE)
        write_json(self.scenario.state_path, state)
        self.manifest = load_resource_manifest(
            MANIFEST_PATH, expected_sha256=MANIFEST_SHA256
        )

    def clients(self):
        clients = self.scenario.clients()
        clients["manifest"] = {"value": self.manifest, "sha256": MANIFEST_SHA256}
        return clients

    def set_now(self, value):
        state = self.scenario.current()
        state["clock"]["now"] = value.isoformat()
        write_json(self.scenario.state_path, state)

    def refresh(self, observed, *, grant=None, index=None):
        return refresh_question_policy.refresh_observed_policy(
            release_index=index or self.index,
            observation=observed,
            grant=grant or renewal_grant(),
            clients=self.clients(),
        )

    def calls(self):
        return [entry["call"] for entry in self.scenario.current()["journal"]]


def test_renewal_activates_fresh_observation_with_same_image(tmp_path):
    renewal = Renewal(tmp_path)
    observed = observation(observed_at=NOW - timedelta(hours=1))
    result = renewal.refresh(observed)
    assert result["state"] == "activated", result
    ledger, _generation = renewal.scenario.ledger_now()
    new_binding = result["binding"]
    assert ledger["active_deployment_digest"] == new_binding["deployment_digest"]
    assert new_binding["image_digest"] == NEW_IMAGE
    assert new_binding["lp_commit"] == LP_COMMIT
    assert result["policy"]["pricing"]["verified_at"] == observed["observed_at"]
    assert result["policy"]["limits"] == renewal.scenario.policy["limits"]
    assert result["policy"]["expires_at"] == "2026-09-30T23:59:59Z"
    assert result["policy"]["model"] == renewal.scenario.policy["model"]
    assert (
        ledger["bindings"][renewal.scenario.binding["deployment_digest"]]
        == (renewal.scenario.policy["policy_digest"])
    )
    assert ledger["requests"] == {} and ledger["reserved_microusd"] == 0
    assert result["activation"]["if_generation_match"] == 40
    service = renewal.scenario.current()["service"]
    assert service["trafficStatuses"][0]["revision"] == new_binding["revision_name"]
    assert (
        service["template"]["containers"][0]["image"]
        == IMAGE_NAME + "@sha256:" + NEW_IMAGE
    )
    calls = renewal.calls()
    assert calls.count("deploy_revision") == 2
    assert calls[-4:] == [
        "create_object",
        "deploy_revision",
        "create_object",
        "update_traffic",
    ]


def test_renewal_refuses_failed_observation_without_writes(tmp_path):
    renewal = Renewal(tmp_path)
    before = renewal.calls()
    observed = observation(
        observed_at=NOW - timedelta(hours=1), page=b"<html>different page</html>"
    )
    result = renewal.refresh(observed)
    assert (
        result["state"] == "refused"
        and result["error"] == "observation_source_mismatch"
    )
    assert renewal.calls() == before
    other_model = next(
        name
        for name in refresh_question_policy.PRICING_PROFILES
        if name != make_policy()["model"]
    )
    wrong_model = observation(observed_at=NOW - timedelta(hours=1), model=other_model)
    assert renewal.refresh(wrong_model)["error"] == "observation_model_mismatch"
    assert renewal.calls() == before


def test_renewal_refuses_rate_above_reservation_bound(tmp_path):
    renewal = Renewal(tmp_path)
    page = b"approved flash model input 2.5 output 6.0"
    observed = observation(
        observed_at=NOW - timedelta(hours=1),
        input_rate="2.500000",
        output_rate="6.000000",
        page=page,
    )
    state = renewal.scenario.current()
    state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(page)
    write_json(renewal.scenario.state_path, state)
    result = renewal.refresh(observed)
    assert (
        result["state"] == "refused"
        and result["error"] == "rate_above_reservation_bound"
    )
    assert "deploy_revision" not in renewal.calls()[2:]


def test_renewal_refuses_stale_and_future_dated_observations(tmp_path):
    renewal = Renewal(tmp_path)
    stale = observation(observed_at=NOW - timedelta(hours=25))
    assert renewal.refresh(stale)["error"] == "observation_stale"
    future = observation(observed_at=NOW + timedelta(minutes=5))
    assert renewal.refresh(future)["error"] == "observation_future_dated"
    renewal.set_now(datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    expired = observation(observed_at=datetime(2026, 9, 30, 23, 0, tzinfo=UTC))
    assert renewal.refresh(expired)["error"] == "policy_expired"
    assert renewal.calls().count("deploy_revision") == 1


def test_renewal_duplicate_trigger_returns_existing_activation(tmp_path):
    renewal = Renewal(tmp_path)
    observed = observation(observed_at=NOW - timedelta(hours=1))
    first = renewal.refresh(observed)
    assert first["state"] == "activated"
    calls_after_first = renewal.calls()
    second = renewal.refresh(observed)
    assert second["state"] == "already_activated"
    assert (
        second["binding"]["deployment_digest"] == first["binding"]["deployment_digest"]
    )
    assert renewal.calls() == calls_after_first


def test_renewal_refuses_concurrent_deployment_and_stale_ledger(tmp_path):
    renewal = Renewal(tmp_path)
    observed = observation(observed_at=NOW - timedelta(hours=1))
    clients = renewal.clients()
    original_create = clients["objects"].create
    state = clients["_state"]

    def racing_create(name, raw, *, if_generation_match):
        if name == LEDGER_NAME:
            stored = state.data["objects"][LEDGER_NAME]
            state.data["objects"][LEDGER_NAME] = object_entry(
                fake.decode_raw(stored["raw_b64"]), 95
            )
            state.save()
        return original_create(name, raw, if_generation_match=if_generation_match)

    clients["objects"].create = racing_create
    result = refresh_question_policy.refresh_observed_policy(
        release_index=renewal.index,
        observation=observed,
        grant=renewal_grant(),
        clients=clients,
    )
    assert result["state"] == "failed" and result["error"] == "stale_ledger_generation"
    assert result["deployed_revision"] is not None
    ledger, generation = renewal.scenario.ledger_now()
    assert generation == "95"
    assert (
        ledger["active_deployment_digest"]
        == renewal.scenario.binding["deployment_digest"]
    )
    assert "update_traffic" not in renewal.calls()[3:]
    service = renewal.scenario.current()["service"]
    assert service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"


def test_renewal_failure_after_no_traffic_deployment_before_activation(tmp_path):
    renewal = Renewal(tmp_path)
    observed = observation(observed_at=NOW - timedelta(hours=1))
    state = renewal.scenario.current()
    state["failures"]["deploy_revision"] = "failed"
    write_json(renewal.scenario.state_path, state)
    result = renewal.refresh(observed)
    assert result["state"] == "failed" and result["error"] == "deployment_failed"
    ledger, generation = renewal.scenario.ledger_now()
    assert generation == "40"
    assert (
        ledger["active_deployment_digest"]
        == renewal.scenario.binding["deployment_digest"]
    )
    assert "update_traffic" not in renewal.calls()[3:]


def test_two_renewals_then_duplicate_and_ordinary_deployment_between_observations(
    tmp_path,
):
    renewal = Renewal(tmp_path)
    first = renewal.refresh(observation(observed_at=NOW - timedelta(hours=1)))
    assert first["state"] == "activated"
    renewal.set_now(NOW + timedelta(hours=12))
    second = renewal.refresh(observation(observed_at=NOW + timedelta(hours=11)))
    assert second["state"] == "activated"
    assert second["policy"]["policy_digest"] != first["policy"]["policy_digest"]
    ledger, _generation = renewal.scenario.ledger_now()
    assert ledger["active_deployment_digest"] == second["binding"]["deployment_digest"]
    assert (
        ledger["bindings"][first["binding"]["deployment_digest"]]
        == first["policy"]["policy_digest"]
    )
    assert len(ledger["bindings"]) == 4
    third = renewal.refresh(observation(observed_at=NOW + timedelta(hours=11)))
    assert third["state"] == "already_activated"
    assert (
        third["binding"]["deployment_digest"] == second["binding"]["deployment_digest"]
    )
    ordinary_policy = make_policy(NOW + timedelta(hours=11, minutes=30))
    ordinary_binding = make_binding(
        ordinary_policy, revision=SERVICE + "-q-ord", image_digest="e" * 64
    )
    state = renewal.scenario.current()
    ledger["bindings"][ordinary_binding["deployment_digest"]] = ordinary_policy[
        "policy_digest"
    ]
    ledger["active_deployment_digest"] = ordinary_binding["deployment_digest"]
    state["objects"][LEDGER_NAME] = object_entry(canonical(ledger), 120)
    state["objects"][
        PREFIX + f"policies/{ordinary_policy['policy_digest']}/policy.json"
    ] = object_entry(canonical(ordinary_policy), 121)
    state["objects"][
        PREFIX + f"deployments/{ordinary_binding['deployment_digest']}/binding.json"
    ] = object_entry(canonical(ordinary_binding), 122)
    previous_generation = int(state["service"]["generation"])
    state["service"] = make_service(ordinary_binding, renewal.scenario.tag)
    state["service"]["etag"] = "etag-ordinary"
    ordinary_revision = SERVICE_API + "/revisions/" + ordinary_binding["revision_name"]
    state["service"]["reconciling"] = False
    state["service"]["generation"] = str(previous_generation + 1)
    state["service"]["observedGeneration"] = str(previous_generation + 1)
    state["service"]["latestCreatedRevision"] = ordinary_revision
    state["service"]["latestReadyRevision"] = ordinary_revision
    state["revisions"][ordinary_revision] = make_revision(
        ordinary_binding, renewal.scenario.tag
    )
    state["clock"]["now"] = (NOW + timedelta(hours=24)).isoformat()
    write_json(renewal.scenario.state_path, state)
    ordinary_index = copy.deepcopy(renewal.index)
    ordinary_index["app_image"] = IMAGE_NAME + "@sha256:" + "e" * 64
    ordinary_index["policy_digest"] = ordinary_policy["policy_digest"]
    ordinary_index["deployment_digest"] = ordinary_binding["deployment_digest"]
    fourth = renewal.refresh(
        observation(observed_at=NOW + timedelta(hours=23)), index=ordinary_index
    )
    assert fourth["state"] == "activated", fourth
    assert fourth["binding"]["image_digest"] == "e" * 64
    assert fourth["binding"]["policy_digest"] != first["policy"]["policy_digest"]
    ledger, _generation = renewal.scenario.ledger_now()
    assert ledger["active_deployment_digest"] == fourth["binding"]["deployment_digest"]
    assert (
        ledger["bindings"][ordinary_binding["deployment_digest"]]
        == ordinary_policy["policy_digest"]
    )
    stale_index = renewal.refresh(
        observation(observed_at=NOW + timedelta(hours=23, minutes=30)),
        index=renewal.index,
    )
    assert (
        stale_index["state"] == "refused"
        and stale_index["error"] == "release_index_image_mismatch"
    )


def test_renewal_refuses_expired_grant_and_wider_configuration_change(tmp_path):
    renewal = Renewal(tmp_path)
    observed = observation(observed_at=NOW - timedelta(hours=1))
    result = renewal.refresh(
        observed, grant=renewal_grant(expires_at=NOW - timedelta(minutes=1))
    )
    assert result["state"] == "refused" and result["error"] == "grant_expired"
    wider = dict(observed, location="us-central1")
    assert renewal.refresh(wider)["error"] == "observation_scope_mismatch"
    assert "deploy_revision" not in renewal.calls()[2:]


# Review findings F1, F2, F5, F6, F7


def rolled_back_scenario(tmp_path, *, requests=None, tasks=(), objects=None):
    """Release, then roll back to the old revision, then advance the clock past the drain."""
    scenario = rollback_scenario(tmp_path, requests=requests)
    state = scenario.current()
    state["tasks"] = list(tasks)
    state["objects"].update(objects or {})
    write_json(scenario.state_path, state)
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_rollback(scenario, plan_path)
    assert code == 0, stderr
    rollback_now = datetime.fromisoformat(scenario.current()["clock"]["now"])
    state = scenario.current()
    state["clock"]["now"] = (rollback_now + timedelta(seconds=200)).isoformat()
    write_json(scenario.state_path, state)
    scenario.rollback_result_path = result_path
    return scenario


def test_resume_after_rollback_accepts_rollback_receipt_despite_newer_template(
    tmp_path,
):
    scenario = rolled_back_scenario(tmp_path)
    service = scenario.current()["service"]
    assert service["template"]["revision"] == SERVICE + "-q-new"
    assert service["trafficStatuses"][0]["revision"] == SERVICE + "-q-old"
    code, summary, _stderr, _plan_path = plan_resume(
        scenario, "empty", route=scenario.rollback_result_path, output="wrong.json"
    )
    assert code != 0 and summary["error"] == "activation_forbidden"
    code, _summary, stderr, plan_path = plan_resume(
        scenario, "empty", route=scenario.rollback_result_path, activation=False
    )
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    assert plan["route"]["kind"] == "rollback"
    assert plan["route"]["state"] == "rollback_verified_queue_paused"
    assert (
        plan["binding"]["deployment_digest"]
        == scenario.old_binding["deployment_digest"]
    )
    assert plan["expected"]["revision_name"] == SERVICE + "-q-old"
    code, _summary, stderr, result_path = resume_queue(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "running_verified"
    assert scenario.current()["queue"]["state"] == "RUNNING"
    assert scenario.current()["service"]["template"]["revision"] == SERVICE + "-q-new"


def test_resume_after_rollback_drains_expired_request_bound_to_compatible_revision(
    tmp_path,
):
    (tmp_path / "seed").mkdir()
    seed = Scenario(tmp_path / "seed")
    rollback_now = NOW + timedelta(seconds=200)
    row, task, objects = pending_fixture(
        seed,
        admitted_at=rollback_now - timedelta(seconds=100),
        binding=seed.old_binding,
        policy=seed.old_policy,
        tag=seed.old_tag,
    )
    scenario = rolled_back_scenario(
        tmp_path, requests={RID: row}, tasks=[task], objects=objects
    )
    rollback = json.loads(scenario.rollback_result_path.read_text(encoding="utf-8"))
    assert rollback["classification"]["queued"] == [RID]
    assert rollback["resume_eligibility"]["eligible"] is True
    ledger_before, generation_before = scenario.ledger_now()
    ids = [
        "--pending-request",
        RID,
        "--parent-request",
        PARENT,
        "--anchor-request",
        ANCHOR,
    ]
    code, summary, _stderr, _plan_path = plan_resume(
        scenario,
        "recovery",
        *ids,
        route=scenario.rollback_result_path,
        activation=False,
    )
    assert code != 0 and summary["error"] == "insufficient_remaining_time"
    code, _summary, stderr, plan_path = plan_resume(
        scenario,
        "drain-expired",
        *ids,
        route=scenario.rollback_result_path,
        activation=False,
        output="drain.json",
    )
    assert code == 0, stderr
    code, _summary, stderr, result_path = resume_queue(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "running_verified" and result["drain_expired"] is True
    assert result["pending_request"] == RID
    ledger_after, generation_after = scenario.ledger_now()
    assert (ledger_after, generation_after) == (ledger_before, generation_before)
    assert scenario.current()["queue"]["state"] == "RUNNING"


def test_resume_after_rollback_refuses_requests_bound_to_the_newer_binding(tmp_path):
    (tmp_path / "seed").mkdir()
    seed = Scenario(tmp_path / "seed")
    row, task, objects = pending_fixture(seed, admitted_at=NOW - timedelta(seconds=10))
    scenario = rolled_back_scenario(
        tmp_path, requests={RID: row}, tasks=[task], objects=objects
    )
    rollback = json.loads(scenario.rollback_result_path.read_text(encoding="utf-8"))
    assert rollback["classification"]["queued"] == [RID]
    assert rollback["resume_eligibility"]["eligible"] is False
    ids = [
        "--pending-request",
        RID,
        "--parent-request",
        PARENT,
        "--anchor-request",
        ANCHOR,
    ]
    for mode, output in (("drain-expired", "a.json"), ("recovery", "b.json")):
        code, summary, _stderr, _plan_path = plan_resume(
            scenario,
            mode,
            *ids,
            route=scenario.rollback_result_path,
            activation=False,
            output=output,
        )
        assert code != 0
        assert summary["error"] == "task_audience_mismatch"
    code, summary, _stderr, _plan_path = plan_resume(
        scenario,
        "empty",
        route=scenario.rollback_result_path,
        activation=False,
        output="c.json",
    )
    assert code != 0 and summary["error"] == "task_audience_mismatch"
    assert scenario.current()["queue"]["state"] == "PAUSED"
    assert "resume_queue" not in [
        entry["call"] for entry in scenario.current()["journal"]
    ]


def test_rollback_plan_records_pause_step_for_running_queue_and_apply_pauses_first(
    tmp_path,
):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    state = scenario.current()
    state["queue"]["state"] = "RUNNING"
    write_json(scenario.state_path, state)
    ledger_before, _generation_before = scenario.ledger_now()
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    assert [change["operation"] for change in plan["changes"]] == [
        "pause_queue",
        "activate_binding",
        "route_traffic",
    ]
    assert plan["changes"][0]["resource"] == QUEUE_RESOURCE
    assert plan["changes"][0]["action"] == "write"
    assert scenario.current()["queue"]["state"] == "RUNNING"
    code, _summary, stderr, result_path = apply_rollback(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "rollback_verified_queue_paused"
    assert result["queue_paused"]["state"] == "PAUSED"
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert calls[-3:] == ["pause_queue", "create_object", "update_traffic"]
    assert scenario.current()["queue"]["state"] == "PAUSED"
    ledger_after, _generation_after = scenario.ledger_now()
    assert canonical(ledger_after["requests"]) == canonical(ledger_before["requests"])
    assert ledger_after["reserved_microusd"] == ledger_before["reserved_microusd"]


def test_rollback_apply_refuses_running_queue_without_a_planned_pause(tmp_path):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    assert [change["operation"] for change in plan["changes"]] == [
        "activate_binding",
        "route_traffic",
    ]
    state = scenario.current()
    state["queue"]["state"] = "RUNNING"
    write_json(scenario.state_path, state)
    ledger_before = scenario.ledger_now()
    code, summary, _stderr, _result_path = apply_rollback(scenario, plan_path)
    assert code != 0
    assert summary["error"] == "before_state_changed"
    assert scenario.ledger_now() == ledger_before
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert "pause_queue" not in calls and "create_object" not in calls


def test_rollback_plan_records_admissions_pause_as_unsupported_by_ledger_control(
    tmp_path,
):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    gap = plan["admissions_pause"]
    assert gap["state"] == "unsupported_by_ledger_control"
    assert gap["ledger_control_fields"] == [
        "active_deployment_digest",
        "allowance_id",
        "bindings",
        "contract_version",
        "requests",
        "reserved_microusd",
    ]
    assert "approval_required" in gap["reason"]
    assert [change["operation"] for change in plan["changes"]] == [
        "activate_binding",
        "route_traffic",
    ]
    code, _summary, stderr, result_path = apply_rollback(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "rollback_verified_queue_paused"
    assert result["admissions_pause"] == gap


def test_repeated_rollback_apply_after_route_failure_refuses_and_names_the_route(
    tmp_path,
):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    ledger_before, generation_before = scenario.ledger_now()
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    state = scenario.current()
    state["failures"]["update_traffic"] = "failed"
    write_json(scenario.state_path, state)
    code, summary, _stderr, result_path = apply_rollback(scenario, plan_path)
    assert code != 0
    assert summary["error"] == "route_failed"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "failed_or_unproven_requires_native_reconciliation"
    assert result["admissions_pause"]["state"] == "unsupported_by_ledger_control"
    ledger_after, generation_after = scenario.ledger_now()
    assert generation_after == result["activation"]["generation"]
    assert generation_after != generation_before
    target_digest = scenario.old_binding["deployment_digest"]
    assert ledger_after["active_deployment_digest"] == target_digest
    assert canonical(ledger_after["requests"]) == canonical(ledger_before["requests"])
    recovery = result["forward_recovery_plan"]
    assert recovery["route"] == {
        "operation": result["operations"]["route_traffic"]["operation"],
        "state": "failed",
        "target_revision": SERVICE + "-q-old",
        "activation_generation": generation_after,
    }
    assert recovery["route"]["operation"].startswith(SERVICE_API + "/operations/")
    assert recovery["active_deployment_digest"] == target_digest
    assert recovery["queue"] == "paused"
    assert set(recovery["preserved_requests"]) == {RID, PARENT}
    assert "before_state_changed" in recovery["next"]
    state = scenario.current()
    del state["failures"]["update_traffic"]
    write_json(scenario.state_path, state)
    code, summary, _stderr, retry_path = apply_rollback(
        scenario, plan_path, output="rollback-retry.json"
    )
    assert code != 0
    assert summary["error"] == "before_state_changed"
    retry = json.loads(retry_path.read_text(encoding="utf-8"))
    assert retry["state"] == "refused"
    assert retry["observed_before"]["ledger"]["generation"] == generation_after
    assert retry["forward_recovery_plan"]["active_deployment_digest"] == target_digest
    assert "route" not in retry["forward_recovery_plan"]
    assert scenario.ledger_now() == (ledger_after, generation_after)
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert calls[-2:] == ["create_object", "update_traffic"]
    assert scenario.current()["queue"]["state"] == "PAUSED"
    code, summary, _stderr, _replan_path = plan_rollback(
        scenario, output="rollback-replan.json"
    )
    assert code != 0
    assert summary["error"] == "rollback_target_already_active"


def test_rollback_ledger_compare_and_swap_refuses_a_concurrent_write(tmp_path):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    ledger_before, generation_before = scenario.ledger_now()
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    state = scenario.current()
    state["failures"]["create_object"] = "concurrent_write"
    write_json(scenario.state_path, state)
    code, summary, _stderr, result_path = apply_rollback(scenario, plan_path)
    assert code != 0
    assert summary["error"] == "stale_ledger_generation"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "refused"
    assert result["activation"]["state"] == "failed"
    assert result["activation"]["error"] == "PreconditionFailed: generation_mismatch"
    assert "route_traffic" not in result["operations"]
    ledger_after, generation_after = scenario.ledger_now()
    assert ledger_after == ledger_before
    assert generation_after != generation_before
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert calls[-2:] == ["concurrent_write", "create_object"]
    assert scenario.current()["journal"][-2]["generation"] == generation_after
    assert scenario.current()["queue"]["state"] == "PAUSED"
    assert (
        scenario.current()["service"]["trafficStatuses"][0]["revision"]
        == SERVICE + "-q-new"
    )
    recovery = result["forward_recovery_plan"]
    assert recovery["active_deployment_digest"] == binding["deployment_digest"]
    assert set(recovery["preserved_requests"]) == {RID, PARENT}
    assert "route" not in recovery


def test_rollback_apply_never_pauses_an_already_paused_queue(tmp_path):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    state = scenario.current()
    assert state["queue"]["state"] == "PAUSED"
    state["journal"].append({"call": "pause_queue", "queue": QUEUE_API})
    write_json(scenario.state_path, state)
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    assert "pause_queue" not in [change["operation"] for change in plan["changes"]]
    code, _summary, stderr, result_path = apply_rollback(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "rollback_verified_queue_paused"
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert calls.count("pause_queue") == 1
    assert calls[-2:] == ["create_object", "update_traffic"]
    assert scenario.current()["queue"]["state"] == "PAUSED"


def test_rollback_apply_refuses_before_any_ledger_write_when_pause_reads_back_running(
    tmp_path,
):
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = rollback_scenario(tmp_path, requests=terminal_requests(policy, binding))
    state = scenario.current()
    state["queue"]["state"] = "RUNNING"
    write_json(scenario.state_path, state)
    ledger_before = scenario.ledger_now()
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    assert plan["changes"][0]["operation"] == "pause_queue"
    state = scenario.current()
    state["failures"]["pause_queue"] = "ignored"
    write_json(scenario.state_path, state)
    code, summary, _stderr, result_path = apply_rollback(scenario, plan_path)
    assert code != 0
    assert summary["error"] == "queue_not_paused"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "failed_or_unproven_requires_native_reconciliation"
    assert result["queue_paused"]["state"] == "RUNNING"
    assert "activation" not in result
    assert scenario.ledger_now() == ledger_before
    calls = [entry["call"] for entry in scenario.current()["journal"]]
    assert calls[-1] == "pause_queue"
    assert "create_object" not in calls
    assert scenario.current()["queue"]["state"] == "RUNNING"


def test_renewal_route_failure_after_ledger_write_is_labelled_ledger_moved(tmp_path):
    renewal = Renewal(tmp_path)
    ledger_before, _generation_before = renewal.scenario.ledger_now()
    state = renewal.scenario.current()
    state["failures"]["update_traffic"] = "failed"
    write_json(renewal.scenario.state_path, state)
    result = renewal.refresh(observation(observed_at=NOW - timedelta(hours=1)))
    assert result["state"] == "ledger_moved_requires_native_reconciliation"
    assert result["error"] == "route_failed"
    assert result["ledger_moved"] is True
    ledger_after, generation_after = renewal.scenario.ledger_now()
    assert generation_after == result["activation"]["generation"]
    assert (
        ledger_after["active_deployment_digest"]
        == result["binding"]["deployment_digest"]
    )
    assert canonical(ledger_after["requests"]) == canonical(ledger_before["requests"])
    service = renewal.scenario.current()["service"]
    assert service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"


def test_renewal_adapter_exception_never_escapes(tmp_path):
    renewal = Renewal(tmp_path)
    state = renewal.scenario.current()
    state["failures"]["deploy_revision"] = "raise"
    write_json(renewal.scenario.state_path, state)
    result = renewal.refresh(observation(observed_at=NOW - timedelta(hours=1)))
    assert result["state"] == "failed"
    assert result["error"] == "RuntimeError: deploy_unavailable"
    assert result["ledger_moved"] is False
    assert renewal.scenario.ledger_now()[1] == "40"


def test_receipts_never_carry_plain_secret_values(tmp_path):
    secret = "SYNTHETIC-SECRET-VALUE-9f8e7d6c"
    scenario = Scenario(tmp_path)
    state = scenario.current()
    state["service"]["template"]["containers"][0]["env"].append(
        {"name": "OPERATOR_ADDED_TOKEN", "value": secret}
    )
    write_json(scenario.state_path, state)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    verified_path = tmp_path / "release-verified.json"
    code, _summary, stderr = run_cli(
        "verify",
        "--plan",
        plan_path,
        "--result",
        result_path,
        "--output",
        verified_path,
        *scenario.common(),
    )
    assert code == 0, stderr
    for path in (plan_path, result_path, verified_path):
        raw = path.read_bytes()
        assert secret.encode() not in raw, path
    for path in (result_path, verified_path):
        assert b"secretKeyRef" not in path.read_bytes(), path
    plan = json.loads(plan_path.read_bytes())
    assert "secretKeyRef" not in json.dumps(plan["before"])
    entries = {
        item["name"]: item
        for item in plan["before"]["service"]["template"]["containers"][0]["env"]
    }
    assert set(entries["OPERATOR_ADDED_TOKEN"]) == {"name", "value_sha256"}
    assert entries["OPERATOR_ADDED_TOKEN"]["value_sha256"] != sha(secret.encode())
    assert entries["UI_PASSCODE"] == {"name": "UI_PASSCODE", "source": "secret"}
    assert entries["SOURCE_SHA"]["value"] == OLD_LP_COMMIT
    assert (
        "UI_PASSCODE"
        in plan["before"]["service"]["template"]["containers"][0]["env_names"]
    )


def test_lifecycle_carries_the_passcode_reference_only_as_the_deploy_input(tmp_path):
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    verified_path = tmp_path / "release-verified.json"
    code, _summary, stderr = run_cli(
        "verify",
        "--plan",
        plan_path,
        "--result",
        result_path,
        "--output",
        verified_path,
        *scenario.common(),
    )
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    result = json.loads(result_path.read_bytes())
    verified = json.loads(verified_path.read_bytes())
    deploy = [
        item for item in plan["changes"] if item["operation"] == "deploy_revision"
    ]
    assert len(deploy) == 1
    template_entries = deploy[0]["template"]["containers"][0]["env"]
    assert PASSCODE_ENTRY in template_entries
    assert CACHE_TTL_ENTRY in template_entries
    for block in (plan["before"], result["after"], verified["native"]):
        env = {
            item["name"]: item
            for item in block["service"]["template"]["containers"][0]["env"]
        }
        assert env["UI_PASSCODE"] == {"name": "UI_PASSCODE", "source": "secret"}
        assert env["CACHE_TTL"] == CACHE_TTL_ENTRY
        raw = json.dumps(block)
        assert "secretKeyRef" not in raw
        assert "ui-passcode-staging" not in raw
    for path in (result_path, verified_path):
        raw = path.read_bytes()
        assert b"secretKeyRef" not in raw, path
        assert b"ui-passcode-staging" not in raw, path
    drifted = scenario.current()
    revision = drifted["revisions"][
        SERVICE_API + "/revisions/" + scenario.binding["revision_name"]
    ]
    for container in (
        drifted["service"]["template"]["containers"][0],
        revision["containers"][0],
    ):
        container["env"] = replace_entry(container["env"], "UI_PASSCODE", None)
    write_json(scenario.state_path, drifted)
    code, summary, _stderr = run_cli(
        "verify",
        "--plan",
        plan_path,
        "--result",
        result_path,
        "--output",
        tmp_path / "verified-2.json",
        *scenario.common(),
    )
    assert code != 0
    assert summary["state"] == "unproven"
    drift = json.loads((tmp_path / "verified-2.json").read_bytes())["drift"]
    assert {"item": "service", "error": "service_mismatch"} in drift
    assert {"item": "revision", "error": "revision_mismatch"} in drift


def test_plan_refuses_existing_output_before_any_native_read(tmp_path):
    scenario = Scenario(tmp_path)
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    before = plan_path.read_bytes()
    broken_state = write_json(tmp_path / "broken-state.json", {})
    code, summary, _stderr = run_cli(
        "plan",
        "--activation",
        scenario.activation_path,
        "--authority",
        scenario.authority_path,
        "--output",
        plan_path,
        "--adapter",
        str(ADAPTER_PATH) + ":clients",
        "--adapter-state",
        broken_state,
        "--resources",
        MANIFEST_PATH,
    )
    assert code != 0
    assert summary["error"] == "output_exists"
    assert plan_path.read_bytes() == before


# Bridge reads: the release lifecycle turns the worker's bridge reads on


def without_bridge_reads(scenario, *revision_names, service=True):
    """Model a revision deployed before the lifecycle carried the bridge reads switch."""
    state = scenario.current()
    containers = [
        state["revisions"][SERVICE_API + "/revisions/" + name]["containers"][0]
        for name in revision_names
    ]
    if service:
        containers.append(state["service"]["template"]["containers"][0])
    for container in containers:
        container["env"] = replace_entry(container["env"], BRIDGE_READS, None)
    write_json(scenario.state_path, state)


def verify_release(scenario, plan_path, result_path, output):
    options = {"plan": plan_path, "result": result_path, "output": output}
    flags = [
        part for name, value in options.items() for part in ("-" * 2 + name, value)
    ]
    return run_cli("verify", *flags, *scenario.common())


def test_bridge_reads_switch_is_the_name_the_engine_and_the_ask_route_read():
    from src.analysis.open_intelligence.general_question_context_admission import (
        BRIDGE_READS_SWITCH,
    )

    assert BRIDGE_READS == BRIDGE_READS_SWITCH
    routes = (ROOT / "app" / "src" / "api" / "general_question_routes.py").read_text(
        encoding="utf-8"
    )
    assert f'_COVERAGE_BRIDGE_SWITCH = "{BRIDGE_READS}"' in routes


def test_release_from_a_revision_without_bridge_reads_deploys_and_verifies_it(
    tmp_path,
):
    scenario = Scenario(tmp_path)
    without_bridge_reads(scenario, SERVICE + "-q-old")
    code, _summary, stderr, plan_path = plan_release(scenario)
    assert code == 0, stderr
    plan = json.loads(plan_path.read_bytes())
    (deploy,) = [
        item for item in plan["changes"] if item["operation"] == "deploy_revision"
    ]
    assert BRIDGE_READS_ENTRY in deploy["template"]["containers"][0]["env"]
    before = plan["before"]["service"]["template"]["containers"][0]
    assert BRIDGE_READS not in before["env_names"]
    code, _summary, stderr, result_path = apply_release(scenario, plan_path)
    assert code == 0, stderr
    revision = scenario.current()["revisions"][
        SERVICE_API + "/revisions/" + SERVICE + "-q-new"
    ]
    assert BRIDGE_READS_ENTRY in revision["containers"][0]["env"]
    verified_path = tmp_path / "release-verified.json"
    code, summary, stderr = verify_release(
        scenario, plan_path, result_path, verified_path
    )
    assert code == 0, stderr
    assert summary["state"] == "verified"
    verified = json.loads(verified_path.read_bytes())
    assert verified["drift"] == []
    env = {
        item["name"]: item
        for item in verified["native"]["service"]["template"]["containers"][0]["env"]
    }
    assert env[BRIDGE_READS] == BRIDGE_READS_ENTRY
    without_bridge_reads(scenario, SERVICE + "-q-new")
    code, summary, _stderr = verify_release(
        scenario, plan_path, result_path, tmp_path / "verified-2.json"
    )
    assert code != 0
    assert summary["state"] == "unproven"
    drift = json.loads((tmp_path / "verified-2.json").read_bytes())["drift"]
    assert {"item": "service", "error": "service_mismatch"} in drift
    assert {"item": "revision", "error": "revision_mismatch"} in drift


def test_renewal_refuses_legacy_revision_without_bridge_reads_before_deploy(tmp_path):
    renewal = Renewal(tmp_path)
    without_bridge_reads(renewal.scenario, SERVICE + "-q-new")
    before = renewal.calls()
    result = renewal.refresh(observation(observed_at=NOW - timedelta(hours=1)))
    assert (result["state"], result["error"]) == (
        "refused",
        "service_mismatch",
    ), result
    assert result["mutations"] == []
    assert result["operations"] == {}
    assert "deploy_revision" not in renewal.calls()[len(before) :]
    state = renewal.scenario.current()
    assert BRIDGE_READS_ENTRY not in state["service"]["template"]["containers"][0]["env"]
    revision = state["revisions"][SERVICE_API + "/revisions/" + SERVICE + "-q-new"]
    assert BRIDGE_READS_ENTRY not in revision["containers"][0]["env"]


def test_rollback_to_a_revision_without_bridge_reads_plans_applies_and_resumes(
    tmp_path,
):
    scenario = rollback_scenario(tmp_path)
    without_bridge_reads(scenario, SERVICE + "-q-old", service=False)
    code, _summary, stderr, plan_path = plan_rollback(scenario)
    assert code == 0, stderr
    code, _summary, stderr, result_path = apply_rollback(scenario, plan_path)
    assert code == 0, stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["state"] == "rollback_verified_queue_paused"
    rollback_now = datetime.fromisoformat(scenario.current()["clock"]["now"])
    state = scenario.current()
    state["clock"]["now"] = (rollback_now + timedelta(seconds=200)).isoformat()
    write_json(scenario.state_path, state)
    code, _summary, stderr, resume_path = plan_resume(
        scenario, "empty", route=result_path, activation=False
    )
    assert code == 0, stderr
    code, _summary, stderr, resumed_path = resume_queue(scenario, resume_path)
    assert code == 0, stderr
    resumed = json.loads(resumed_path.read_text(encoding="utf-8"))
    assert resumed["state"] == "running_verified"


@pytest.mark.parametrize("entry", [{"value": "disabled"}, {"value": ""}])
def test_rollback_refuses_a_target_whose_bridge_reads_switch_is_not_enabled(
    tmp_path, entry
):
    scenario = rollback_scenario(tmp_path)
    state = scenario.current()
    container = state["revisions"][SERVICE_API + "/revisions/" + SERVICE + "-q-old"][
        "containers"
    ][0]
    container["env"] = replace_entry(container["env"], BRIDGE_READS, entry)
    write_json(scenario.state_path, state)
    code, summary, _stderr, _plan_path = plan_rollback(scenario)
    assert code != 0
    assert summary["error"] == "revision_mismatch"


def test_resume_after_a_release_refuses_a_routed_revision_without_bridge_reads(
    tmp_path,
):
    scenario = released_scenario(tmp_path)
    code, _summary, stderr, _plan_path = plan_resume(
        scenario, "empty", output="resume-plan-enabled.json"
    )
    assert code == 0, stderr
    without_bridge_reads(scenario, SERVICE + "-q-new")
    code, summary, _stderr, _plan_path = plan_resume(scenario, "empty")
    assert code != 0
    assert summary["error"] == "revision_mismatch"
