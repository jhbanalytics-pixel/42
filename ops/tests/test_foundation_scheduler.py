import base64
import functools
import hashlib
import importlib
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.runners import managed_runtime
from ops.runners.managed_runtime import (
    CONFIGURATION_ANNOTATION,
    DAILY_JOB_ID,
    DAILY_JOB_NAME,
    DAILY_JOB_RESOURCE,
    EMPTY_RUN_REQUEST_BODY,
    INVOCATION_FIELDS,
    KERNEL_PROFILE_FIELDS,
    PRICE_POLICY_JOB_ID,
    PRICE_POLICY_RUN_URI,
    RUN_JOB_URI,
    _daily_wiring,
    _native_grant_loader,
    build_invocation,
    canonical_sha256,
    daily_plan,
    default_daily_wiring,
    engine_authority_adapter,
    engine_daily_adapter,
    load_runtime_configuration,
    load_scheduler_configuration,
    native_stage_handlers,
    observation_cutoff,
    run_managed_mode,
    unavailable_stage_handlers,
    validate_invocation,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "managed_runtime"
REVIEWED_MANIFEST = FIXTURES / "resource_manifest.json"
REVIEWED_MANIFEST_SHA256 = (
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
)
# The engine's active generation manifest: the approve routine and the durable approval
# reader admit only a grant naming it, while the invocation and the daily profile stay
# bound to the ops deploy manifest above.
# Amendment g added the route A capture's artifact bucket to the bridge manifest, so its
# digest moved from 1643e4ce to ab7802f4; the registry stays 573deeea.
# The daily contract revision then registered three daily operations
# and bounded exposure, so the pair moved again: registry 573deeea to e6b95e35, manifest
# ab7802f4 to b7553041.
# Tightening the date regex to ASCII digits then moved them to f23001ed and 592ed45f.
ACTIVE_RESOURCE_MANIFEST_SHA256 = (
    "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe"
)
JOBS_FILE = ROOT / "infra" / "runtime" / "daily-staging.json"
SCHEDULER_FILE = ROOT / "infra" / "runtime" / "scheduler-staging.json"
ORCHESTRATION = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
SCHEDULER_ACCOUNT = "intelligence-42-scheduler@ogilvy-trends-v2.iam.gserviceaccount.com"
PRICE_POLICY_ACCOUNT = (
    "intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"
)
EXECUTION = "intelligence-42-daily-staging-k7q2x"
EXECUTION_NAME = f"{DAILY_JOB_NAME}/executions/{EXECUTION}"
IMAGE_DIGEST = "sha256:" + "4" * 64
PRODUCTION_JOB = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
    "jobs/trends-v2-daily"
)
ATTACHED = {
    "kind": "attached_service_account",
    "source": "metadata_server",
    "principal": ORCHESTRATION,
}


def _manifest():
    raw = REVIEWED_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == REVIEWED_MANIFEST_SHA256
    return json.loads(raw)


def _payload(**overrides):
    payload = {
        "job_resource": DAILY_JOB_RESOURCE,
        "resource_manifest_digest": REVIEWED_MANIFEST_SHA256,
        "mode": "verify-runtime",
        "request_id": EXECUTION,
    }
    payload.update(overrides)
    return payload


def _config():
    return load_runtime_configuration(JOBS_FILE)


def _readback(config, *, execution=EXECUTION, execution_job=None, image=None):
    entry = config["jobs"][DAILY_JOB_ID]
    task = {
        "serviceAccount": entry["service_account"],
        "maxRetries": entry["max_retries"],
        "timeout": f"{entry['timeout_seconds']}s",
        "containers": [
            {
                "image": image or f"{entry['image_repository']}@{IMAGE_DIGEST}",
                "command": list(entry["command"]),
                "args": list(entry["args"]),
                "env": list(entry["env"]),
            }
        ],
    }
    annotations = {CONFIGURATION_ANNOTATION: canonical_sha256(config)}
    return {
        "execution": {
            "name": f"{DAILY_JOB_NAME}/executions/{execution}",
            "job": execution_job or DAILY_JOB_NAME,
            "annotations": dict(annotations),
            "taskCount": entry["task_count"],
            "parallelism": entry["parallelism"],
            "template": deepcopy(task),
        },
        "job": {
            "name": DAILY_JOB_NAME,
            "template": {
                "annotations": dict(annotations),
                "taskCount": entry["task_count"],
                "parallelism": entry["parallelism"],
                "template": deepcopy(task),
            },
        },
    }


def _build(config=None, resources=None, execution=EXECUTION, **overrides):
    config = config or _config()
    readback = overrides.pop("readback", None) or _readback(config)
    reader = overrides.pop("execution_reader", None) or (lambda _name: readback)
    return build_invocation(
        config,
        resources if resources is not None else _manifest(),
        execution,
        execution_reader=reader,
        credential_discovery=overrides.pop(
            "credential_discovery", lambda: dict(ATTACHED)
        ),
        environment=overrides.pop("environment", {}),
    )


class FakeAuthority:
    def __init__(self, *, existing=None, unresolved=()):
        self.existing = existing
        self.unresolved = list(unresolved)
        self.calls = []

    def read_operation(self, request_id):
        self.calls.append(("read_operation", request_id))
        return self.existing

    def unresolved_operations(self, job_resource):
        self.calls.append(("unresolved_operations", job_resource))
        return list(self.unresolved)

    def reserve(self, invocation, run_manifest):
        self.calls.append(
            ("reserve", invocation["request_id"], run_manifest["operation"])
        )
        return {"authority": invocation["request_id"], "manifest": run_manifest}

    def consume(self, authority):
        self.calls.append(("consume", authority["authority"]))
        return {
            "consumption_id": "cons_" + authority["authority"],
            "status": "consumed",
        }


def _run_manifest():
    return {
        "contract_version": "42_daily_run_manifest_v1",
        "operation": "daily_source_capture",
        "manifest_sha256": "b" * 64,
        "cycle_id": "2026-09-12T00:00:00Z",
    }


def test_missing_invocation_is_not_a_successful_empty_run():
    with pytest.raises(ValueError, match="invocation_fields_invalid"):
        validate_invocation({}, {})


def test_reviewed_manifest_positive_control_returns_exact_fields():
    validated = validate_invocation(_payload(), _manifest())
    assert tuple(validated) == INVOCATION_FIELDS
    assert validated == _payload()
    assert validated is not _payload()


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in _payload().items() if key != "request_id"},
        _payload(overrides={"containerOverrides": [{"args": ["--mode", "daily"]}]}),
        _payload(credentials_file="C:/keys/application_default_credentials.json"),
        _payload(request_id=""),
        _payload(request_id=7),
        _payload(mode="verify-runtime "),
        [("job_resource", DAILY_JOB_RESOURCE)],
    ],
)
def test_missing_extra_or_malformed_fields_refuse(payload):
    with pytest.raises(ValueError, match="^invocation_fields_invalid$"):
        validate_invocation(payload, _manifest())


@pytest.mark.parametrize(
    "job_resource",
    [
        PRODUCTION_JOB,
        DAILY_JOB_RESOURCE.replace("daily-staging", "ingest-staging"),
        DAILY_JOB_NAME,
        DAILY_JOB_RESOURCE.replace("ogilvy-trends-v2", "590353929363"),
        DAILY_JOB_RESOURCE + "/executions/" + EXECUTION,
    ],
)
def test_production_or_other_target_refused(job_resource):
    with pytest.raises(ValueError, match="^invocation_target_forbidden$"):
        validate_invocation(_payload(job_resource=job_resource), _manifest())


def test_unknown_mode_refused():
    with pytest.raises(ValueError, match="^invocation_mode_invalid$"):
        validate_invocation(_payload(mode="ingest"), _manifest())


def test_manifest_digest_mismatch_refused():
    with pytest.raises(ValueError, match="^resource_manifest_digest_mismatch$"):
        validate_invocation(_payload(resource_manifest_digest="0" * 64), _manifest())


def test_manifest_without_invoke_permission_refused():
    manifest = _manifest()
    for row in manifest["resources"]:
        if row["name"] == DAILY_JOB_RESOURCE:
            row["actions"] = ["read"]
    payload = _payload(resource_manifest_digest=canonical_sha256(manifest))
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        validate_invocation(payload, manifest)


def test_invalid_manifest_refused_before_digest_claim():
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        validate_invocation(_payload(), {"project": "ogilvy-trends-v2"})


def test_build_invocation_binds_configuration_identity_and_principal():
    config = _config()
    invocation = _build(config)
    assert {key: invocation[key] for key in INVOCATION_FIELDS} == _payload()
    assert invocation["request_id"] == EXECUTION
    assert invocation["execution_name"] == EXECUTION_NAME
    assert invocation["principal"] == ORCHESTRATION
    assert invocation["credential_source"] == "attached_service_account"
    assert invocation["configuration_sha256"] == canonical_sha256(config)
    assert (
        invocation["configuration_sha256"]
        == hashlib.sha256(JOBS_FILE.read_bytes()).hexdigest()
    )
    assert (
        invocation["scheduler_configuration_sha256"]
        == hashlib.sha256(SCHEDULER_FILE.read_bytes()).hexdigest()
    )
    assert invocation["image_uri"].endswith("@" + IMAGE_DIGEST)
    assert validate_invocation(
        {key: invocation[key] for key in INVOCATION_FIELDS}, _manifest()
    )


def test_build_invocation_takes_mode_from_immutable_configuration():
    config = _config()
    config["jobs"][DAILY_JOB_ID]["mode"] = "daily"
    invocation = _build(config)
    assert invocation["mode"] == "daily"
    assert invocation["configuration_sha256"] != canonical_sha256(_config())


@pytest.mark.parametrize(
    "execution", [None, "", "jobs/x/executions/y", "Upper", "a" * 64]
)
def test_missing_or_invalid_execution_identity_refused(execution):
    with pytest.raises(ValueError, match="^execution_identity_missing$"):
        _build(execution=execution)


@pytest.mark.parametrize(
    "environment",
    [
        {"MANAGED_RUNTIME_MODE": "daily"},
        {"MANAGED_RUNTIME_JOB_RESOURCE": PRODUCTION_JOB},
        {"MANAGED_RUNTIME_REQUEST_ID": "custom"},
        {"CLOUD_RUN_JOB": PRICE_POLICY_JOB_ID},
    ],
)
def test_environment_override_refused(environment):
    with pytest.raises(ValueError, match="^runtime_override_forbidden$"):
        _build(environment=environment)


def test_execution_override_in_native_readback_refused():
    config = _config()
    readback = _readback(config)
    readback["execution"]["template"]["containers"][0]["args"] = [
        "-m",
        "ops.runners.managed_runtime",
        "--mode",
        "daily",
    ]
    with pytest.raises(ValueError, match="^runtime_override_forbidden$"):
        _build(config, readback=readback)


def test_job_configuration_drift_in_native_readback_refused():
    config = _config()
    readback = _readback(config)
    for template in (
        readback["execution"]["template"],
        readback["job"]["template"]["template"],
    ):
        template["maxRetries"] = 3
    with pytest.raises(ValueError, match="^runtime_configuration_mismatch$"):
        _build(config, readback=readback)


def test_wrong_parent_job_refused():
    config = _config()
    readback = _readback(
        config, execution_job=DAILY_JOB_NAME.replace(DAILY_JOB_ID, PRICE_POLICY_JOB_ID)
    )
    with pytest.raises(ValueError, match="^execution_job_mismatch$"):
        _build(config, readback=readback)


def test_readback_for_other_execution_refused():
    config = _config()
    with pytest.raises(ValueError, match="^execution_identity_mismatch$"):
        _build(
            config,
            readback=_readback(config, execution="intelligence-42-daily-staging-other"),
        )


def test_absent_readback_permission_refused():
    def forbidden(_name):
        raise PermissionError("403 run.executions.get")

    with pytest.raises(ValueError, match="^execution_readback_forbidden$"):
        _build(execution_reader=forbidden)


def test_unavailable_readback_refused():
    def unavailable(_name):
        raise TimeoutError("metadata")

    with pytest.raises(ValueError, match="^execution_readback_unavailable$"):
        _build(execution_reader=unavailable)


def test_unbound_configuration_annotation_refused():
    config = _config()
    readback = _readback(config)
    config["jobs"][DAILY_JOB_ID]["mode"] = "daily"
    with pytest.raises(ValueError, match="^runtime_configuration_unbound$"):
        _build(config, readback=readback)


def test_mutable_image_tag_refused():
    config = _config()
    image = config["jobs"][DAILY_JOB_ID]["image_repository"] + ":latest"
    with pytest.raises(ValueError, match="^image_unbound$"):
        _build(config, readback=_readback(config, image=image))


def test_configuration_manifest_digest_mismatch_refused():
    config = _config()
    config["resource_manifest_sha256"] = "1" * 64
    with pytest.raises(ValueError, match="^resource_manifest_digest_mismatch$"):
        _build(config)


def test_malformed_configuration_refused():
    config = _config()
    config["jobs"][DAILY_JOB_ID]["args"] = [
        "-m",
        "ops.runners.managed_runtime",
        "--daily",
    ]
    with pytest.raises(ValueError, match="^runtime_configuration_invalid$"):
        _build(config)


def test_wrong_principal_refused():
    descriptor = dict(ATTACHED, principal=PRICE_POLICY_ACCOUNT)
    with pytest.raises(ValueError, match="^principal_mismatch$"):
        _build(credential_discovery=lambda: descriptor)


def test_readback_service_account_drift_refused():
    config = _config()
    readback = _readback(config)
    for template in (
        readback["execution"]["template"],
        readback["job"]["template"]["template"],
    ):
        template["serviceAccount"] = PRICE_POLICY_ACCOUNT
    with pytest.raises(ValueError, match="^principal_mismatch$"):
        _build(config, readback=readback)


@pytest.mark.parametrize(
    "descriptor, code",
    [
        (
            {
                "kind": "user_oauth",
                "source": "gcloud_auth_login",
                "principal": "albert@example.com",
            },
            "credential_source_user_oauth",
        ),
        (
            {
                "kind": "user_oauth",
                "source": "application_default_credentials",
                "principal": "albert@example.com",
            },
            "credential_source_user_oauth",
        ),
        (
            {
                "kind": "refresh_token_file",
                "source": "application_default_credentials",
                "principal": "albert@example.com",
            },
            "credential_source_user_oauth",
        ),
        (
            {
                "kind": "service_account_key_file",
                "source": "GOOGLE_APPLICATION_CREDENTIALS",
                "principal": ORCHESTRATION,
            },
            "credential_source_file",
        ),
        (
            {
                "kind": "host_scheduler",
                "source": "windows_task_scheduler",
                "principal": ORCHESTRATION,
            },
            "credential_source_host_dependency",
        ),
        (
            {"kind": "attached_service_account", "principal": ORCHESTRATION},
            "credential_source_invalid",
        ),
        (dict(ATTACHED, fallback_from="user_oauth"), "credential_source_invalid"),
        (dict(ATTACHED, source="gcloud_impersonation"), "credential_source_invalid"),
    ],
)
def test_non_attached_credentials_refused(descriptor, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        _build(credential_discovery=lambda: descriptor)


def test_silent_fallback_to_user_oauth_refused():
    attempts = []

    def discovery():
        attempts.append("metadata_server_unreachable")
        return {
            "kind": "user_oauth",
            "source": "application_default_credentials",
            "principal": ORCHESTRATION,
        }

    with pytest.raises(ValueError, match="^credential_source_user_oauth$"):
        _build(credential_discovery=discovery)
    assert attempts == ["metadata_server_unreachable"]


def test_credential_discovery_failure_refused():
    def discovery():
        raise RuntimeError("no credentials")

    with pytest.raises(ValueError, match="^credential_source_unavailable$"):
        _build(credential_discovery=discovery)


@pytest.mark.parametrize(
    "environment",
    [
        {"GOOGLE_APPLICATION_CREDENTIALS": "C:/keys/orchestration.json"},
        {"CLOUDSDK_CONFIG": "C:/gcloud-config"},
        {"CLOUDSDK_AUTH_ACCESS_TOKEN": "ya29.token"},
    ],
)
def test_credential_file_or_host_login_in_environment_refused(environment):
    with pytest.raises(ValueError, match="^credential_file_forbidden$"):
        _build(environment=environment)


def test_readback_precedes_authority_consumption():
    order = []
    config = _config()
    readback = _readback(config)

    def reader(name):
        order.append(("readback", name))
        return readback

    config["jobs"][DAILY_JOB_ID]["mode"] = "daily"
    readback = _readback(config)
    invocation = _build(config, execution_reader=reader)
    authority = FakeAuthority()
    run_managed_mode(
        invocation,
        authority=authority,
        run_manifest_loader=lambda _invocation: _run_manifest(),
        handoff=lambda *_args: order.append(("handoff",)),
    )
    order.extend(authority.calls)
    assert order[0] == ("readback", EXECUTION_NAME)
    assert order[1] == ("handoff",)
    assert [call[0] for call in authority.calls] == [
        "read_operation",
        "unresolved_operations",
        "reserve",
        "consume",
    ]


def test_verify_runtime_records_principal_without_vendor_or_authority_calls(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    invocation = _build()
    authority = FakeAuthority()
    seen = []

    def metadata_reader(current):
        seen.append(current)
        return {"approved_identities": ["orchestration"], "job": DAILY_JOB_NAME}

    receipt = run_managed_mode(
        invocation, authority=authority, metadata_reader=metadata_reader
    )
    assert receipt["mode"] == "verify-runtime"
    assert receipt["status"] == "runtime_verified"
    assert receipt["launched"] is False
    assert receipt["principal"] == ORCHESTRATION
    assert receipt["request_id"] == EXECUTION
    assert receipt["configuration_sha256"] == invocation["configuration_sha256"]
    assert receipt["metadata"] == {
        "approved_identities": ["orchestration"],
        "job": DAILY_JOB_NAME,
    }
    assert seen == [invocation]
    assert authority.calls == []
    assert list(tmp_path.iterdir()) == []


def test_verify_runtime_requires_injected_metadata_reader():
    with pytest.raises(ValueError, match="^metadata_reader_unavailable$"):
        run_managed_mode(_build(), authority=FakeAuthority())


def _daily_invocation():
    config = _config()
    config["jobs"][DAILY_JOB_ID]["mode"] = "daily"
    return _build(config)


def test_daily_without_wiring_refuses_daily_wiring_unavailable():
    authority = FakeAuthority()
    with pytest.raises(ValueError, match="^daily_wiring_unavailable$"):
        run_managed_mode(_daily_invocation(), authority=authority)
    assert authority.calls == []


def test_daily_with_loader_but_no_handoff_needs_the_wiring():
    authority = FakeAuthority()
    with pytest.raises(ValueError, match="^daily_wiring_unavailable$"):
        run_managed_mode(
            _daily_invocation(),
            authority=authority,
            run_manifest_loader=lambda _invocation: _run_manifest(),
        )
    assert authority.calls == []


def test_daily_consumes_existing_authority_and_hands_off_exact_request(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    invocation = _daily_invocation()
    authority = FakeAuthority()
    manifest = _run_manifest()
    handed = []

    def handoff(current, run_manifest, consumption):
        handed.append((current, run_manifest, consumption))
        return {"stage": "handed_off", "cycle_id": run_manifest["cycle_id"]}

    result = run_managed_mode(
        invocation,
        authority=authority,
        run_manifest_loader=lambda current: dict(manifest),
        handoff=handoff,
    )
    assert handed[0][0] == invocation
    assert handed[0][1] == manifest
    assert handed[0][2] == {"consumption_id": "cons_" + EXECUTION, "status": "consumed"}
    assert result["mode"] == "daily"
    assert result["launched"] is True
    assert result["operation"] == "daily_source_capture"
    assert result["consumption"] == handed[0][2]
    assert result["handoff"] == {
        "stage": "handed_off",
        "cycle_id": manifest["cycle_id"],
    }
    assert authority.calls == [
        ("read_operation", EXECUTION),
        ("unresolved_operations", DAILY_JOB_RESOURCE),
        ("reserve", EXECUTION, "daily_source_capture"),
        ("consume", EXECUTION),
    ]
    assert list(tmp_path.iterdir()) == []


def test_duplicate_trigger_reads_existing_operation_without_relaunch():
    existing = {"state": "completed", "usage": "known", "consumption_id": "cons_prior"}
    authority = FakeAuthority(existing=existing)
    result = run_managed_mode(
        _daily_invocation(),
        authority=authority,
        run_manifest_loader=lambda _invocation: _run_manifest(),
        handoff=lambda *_args: pytest.fail("duplicate must not hand off"),
    )
    assert result["launched"] is False
    assert result["duplicate"] is True
    assert result["existing_operation"] == existing
    assert [call[0] for call in authority.calls] == ["read_operation"]


def test_duplicate_trigger_with_unknown_usage_blocks_launch():
    authority = FakeAuthority(existing={"state": "in_progress", "usage": "unknown"})
    with pytest.raises(ValueError, match="^operation_usage_unknown$"):
        run_managed_mode(
            _daily_invocation(),
            authority=authority,
            run_manifest_loader=lambda _invocation: _run_manifest(),
            handoff=lambda *_args: pytest.fail("unknown usage must not hand off"),
        )
    assert [call[0] for call in authority.calls] == ["read_operation"]


def test_unresolved_previous_operation_blocks_launch():
    authority = FakeAuthority(
        unresolved=[{"request_id": "intelligence-42-daily-staging-prior"}]
    )
    with pytest.raises(ValueError, match="^operation_unresolved$"):
        run_managed_mode(
            _daily_invocation(),
            authority=authority,
            run_manifest_loader=lambda _invocation: _run_manifest(),
            handoff=lambda *_args: pytest.fail(
                "unresolved operation must not hand off"
            ),
        )
    assert [call[0] for call in authority.calls] == [
        "read_operation",
        "unresolved_operations",
    ]


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        {},
        {"operation": "", "manifest_sha256": "b" * 64},
        {"operation": "x", "manifest_sha256": "zz"},
    ],
)
def test_invalid_run_manifest_refused(manifest):
    authority = FakeAuthority()
    with pytest.raises(ValueError, match="^run_manifest_invalid$"):
        run_managed_mode(
            _daily_invocation(),
            authority=authority,
            run_manifest_loader=lambda _invocation: manifest,
            handoff=lambda *_args: pytest.fail("invalid manifest must not hand off"),
        )
    assert [call[0] for call in authority.calls] == [
        "read_operation",
        "unresolved_operations",
    ]


def test_run_mode_refuses_tampered_invocation():
    invocation = _build()
    invocation["job_resource"] = PRODUCTION_JOB
    with pytest.raises(ValueError, match="^invocation_target_forbidden$"):
        run_managed_mode(
            invocation, authority=FakeAuthority(), metadata_reader=lambda _i: {}
        )


def test_engine_adapter_is_unavailable_without_the_engine_package():
    def importer(name):
        raise ImportError(name)

    with pytest.raises(ValueError, match="^authority_adapter_unavailable$"):
        engine_authority_adapter(importer=importer)


def test_engine_adapter_delegates_reservation_and_consumption():
    calls = []

    class Module:
        @staticmethod
        def _load_execution_authority(operation, *, mode):
            calls.append(("load", operation, mode))
            return {"issued": operation}

        @staticmethod
        def _consume_execution_authority(authority):
            calls.append(("consume", authority["issued"]))
            return {"consumption_id": "cons_native"}

    adapter = engine_authority_adapter(importer=lambda _name: Module)
    issued = adapter.reserve({"request_id": EXECUTION}, _run_manifest())
    assert adapter.consume(issued) == {"consumption_id": "cons_native"}
    # The engine loader takes its execution mode as a required keyword: the v2 consume path.
    assert calls == [
        ("load", "daily_source_capture", "new_consume"),
        ("consume", "daily_source_capture"),
    ]
    with pytest.raises(ValueError, match="^operation_state_unavailable$"):
        adapter.read_operation(EXECUTION)
    with pytest.raises(ValueError, match="^operation_state_unavailable$"):
        adapter.unresolved_operations(DAILY_JOB_RESOURCE)


def test_daily_job_configuration_encodes_the_proposal():
    raw = JOBS_FILE.read_bytes()
    config = load_runtime_configuration(JOBS_FILE)
    assert (
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8") == raw
    )
    assert config["contract_version"] == "42_managed_runtime_jobs_v1"
    assert config["project"] == "ogilvy-trends-v2"
    assert config["location"] == "us-central1"
    assert config["credential_source"] == "attached_service_account"
    assert config["resource_manifest_sha256"] == REVIEWED_MANIFEST_SHA256
    assert config["runtime_job"] == DAILY_JOB_ID
    assert set(config["jobs"]) == {DAILY_JOB_ID, PRICE_POLICY_JOB_ID}
    daily = config["jobs"][DAILY_JOB_ID]
    assert daily["job_resource"] == DAILY_JOB_RESOURCE
    assert daily["mode"] == "verify-runtime"
    assert daily["service_account"] == ORCHESTRATION
    assert daily["command"] == ["python"]
    assert daily["args"] == ["-m", "ops.runners.managed_runtime"]
    assert daily["env"] == [
        {"name": "TRENDS_ENV", "value": "staging"},
        {"name": "BIGQUERY_DATASET", "value": "intelligence_42_sources_staging"},
    ]
    assert daily["max_retries"] == 0
    assert daily["task_count"] == 1
    assert daily["parallelism"] == 1
    assert 0 < daily["timeout_seconds"] <= 86400
    price = config["jobs"][PRICE_POLICY_JOB_ID]
    assert price["job_resource"] == DAILY_JOB_RESOURCE.replace(
        DAILY_JOB_ID, PRICE_POLICY_JOB_ID
    )
    assert price["mode"] is None
    assert price["service_account"] == PRICE_POLICY_ACCOUNT
    assert price["args"][:2] == ["-m", "ops.deploy.refresh_question_policy"]
    assert price["max_retries"] == 0
    assert config["retention"]["execution_history_limit"] > 0
    assert config["retention"]["operation_ledger_days"] > 0
    manifest = _manifest()
    assert canonical_sha256(manifest) == REVIEWED_MANIFEST_SHA256
    for entry in config["jobs"].values():
        assert entry["service_account"] in {
            value.rsplit("/", 1)[1] for value in manifest["identities"].values()
        }


def test_scheduler_configuration_encodes_the_empty_run_request():
    raw = SCHEDULER_FILE.read_bytes()
    config = _config()
    assert hashlib.sha256(raw).hexdigest() == config["scheduler_configuration_sha256"]
    schedulers = load_scheduler_configuration(
        SCHEDULER_FILE, expected_sha256=config["scheduler_configuration_sha256"]
    )
    assert (
        json.dumps(schedulers, sort_keys=True, separators=(",", ":")).encode("utf-8")
        == raw
    )
    assert schedulers["contract_version"] == "42_managed_runtime_schedulers_v1"
    assert set(schedulers["schedulers"]) == {DAILY_JOB_ID, PRICE_POLICY_JOB_ID}
    daily = schedulers["schedulers"][DAILY_JOB_ID]["job"]
    assert daily["name"] == (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
    )
    assert daily["state"] == "PAUSED"
    assert daily["timeZone"] == "Etc/UTC"
    assert daily["retryConfig"] == {"retryCount": 0, "maxRetryDuration": "0s"}
    target = daily["httpTarget"]
    assert target["uri"] == RUN_JOB_URI
    assert target["uri"] == (
        "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/"
        "jobs/intelligence-42-daily-staging:run"
    )
    assert target["httpMethod"] == "POST"
    assert target["headers"] == {"Content-Type": "application/json"}
    assert (
        base64.b64decode(target["body"], validate=True)
        == EMPTY_RUN_REQUEST_BODY
        == b"{}"
    )
    assert json.loads(base64.b64decode(target["body"])) == {}
    assert set(target) == {"uri", "httpMethod", "headers", "body", "oauthToken"}
    assert target["oauthToken"] == {
        "serviceAccountEmail": SCHEDULER_ACCOUNT,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
    }
    price = schedulers["schedulers"][PRICE_POLICY_JOB_ID]["job"]
    assert price["schedule"] == "0 */12 * * *"
    assert price["timeZone"] == "Etc/UTC"
    assert price["state"] == "PAUSED"
    assert price["retryConfig"] == {"retryCount": 0, "maxRetryDuration": "0s"}
    assert price["httpTarget"]["uri"] == PRICE_POLICY_RUN_URI
    assert base64.b64decode(price["httpTarget"]["body"], validate=True) == b"{}"
    assert price["httpTarget"]["oauthToken"]["serviceAccountEmail"] == SCHEDULER_ACCOUNT
    manifest = _manifest()
    scheduler_resource = (
        "//cloudscheduler.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
        "jobs/intelligence-42-daily-staging"
    )
    assert any(row["name"] == scheduler_resource for row in manifest["resources"])


def test_changed_scheduler_bytes_break_the_bound_digest(tmp_path):
    altered = tmp_path / "scheduler-staging.json"
    altered.write_bytes(SCHEDULER_FILE.read_bytes().replace(b"PAUSED", b"ENABLED", 1))
    with pytest.raises(ValueError, match="^scheduler_configuration_digest_mismatch$"):
        load_scheduler_configuration(
            altered, expected_sha256=_config()["scheduler_configuration_sha256"]
        )


def test_noncanonical_configuration_bytes_refused(tmp_path):
    altered = tmp_path / "daily-staging.json"
    altered.write_bytes(json.dumps(_config(), indent=2).encode("utf-8"))
    with pytest.raises(ValueError, match="^runtime_configuration_invalid$"):
        load_runtime_configuration(altered)


def test_configuration_digest_changes_with_the_job_file(tmp_path):
    config = _config()
    config["jobs"][DAILY_JOB_ID]["timeout_seconds"] = (
        config["jobs"][DAILY_JOB_ID]["timeout_seconds"] + 1
    )
    altered = tmp_path / "daily-staging.json"
    altered.write_bytes(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    reloaded = load_runtime_configuration(altered)
    assert (
        canonical_sha256(reloaded) == hashlib.sha256(altered.read_bytes()).hexdigest()
    )
    assert canonical_sha256(reloaded) != canonical_sha256(_config())
    assert _build(reloaded)["configuration_sha256"] == canonical_sha256(reloaded)
    with pytest.raises(ValueError, match="^runtime_configuration_unbound$"):
        _build(reloaded, readback=_readback(_config()))


NOW = datetime(2026, 9, 20, 4, 30, tzinfo=UTC)
SOURCE_POLICY = "c" * 64


class FakeObjectClient:
    def __init__(self):
        self.objects = {}
        self.writes = []

    def read(self, name):
        return self.objects.get(name)

    def write(self, name, payload, *, if_generation_match):
        from src.analysis.open_intelligence.daily_store import PreconditionFailed

        current = self.objects.get(name)
        generation = current[1] if current else 0
        if generation != if_generation_match:
            raise PreconditionFailed(name)
        self.objects[name] = (bytes(payload), generation + 1)
        self.writes.append(name)
        return generation + 1


class FakeReservation:
    def __init__(self, *, unbound=()):
        self.calls = []
        self.unbound = set(unbound)

    def describe(self, operation):
        self.calls.append(("describe", operation))
        if operation in self.unbound:
            return None
        return {"operation": operation, "contract_sha256": "7" * 64}

    def reserve(self, invocation, run_manifest):
        self.calls.append(("reserve", run_manifest["stage"]))
        return {"run_manifest": run_manifest}

    def consume(self, authority):
        self.calls.append(("consume", authority["run_manifest"]["stage"]))
        return {
            "consumption_id": "exc_"
            + authority["run_manifest"]["business_attempt_id"][4:],
            "manifest_sha256": authority["run_manifest"]["manifest_sha256"],
        }

    @property
    def consumed(self):
        return sum(1 for call in self.calls if call[0] == "consume")

    @property
    def paid_calls(self):
        return [call for call in self.calls if call[0] != "describe"]


# The actor hash the deployed v3 approve routine asserts as the grant's issuing principal.
APPROVER_HASH = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
APPROVAL_PHRASE = hashlib.sha256(
    ("Approve recurring execution grant " + "1" * 64).encode("ascii")
).hexdigest()


def _engine():
    return engine_daily_adapter()


def _grant(engine, *, image=IMAGE_DIGEST):
    # The grant names the engine's active generation manifest, the one the approve
    # routine binds it to.
    return engine.recurring_grant.proposed_grant(
        identities=_manifest()["identities"],
        issuing_principal=APPROVER_HASH,
        resource_manifest_digest=ACTIVE_RESOURCE_MANIFEST_SHA256,
        source_policy_digest=SOURCE_POLICY,
        valid_from=datetime(2026, 9, 15, tzinfo=UTC),
        permitted_image_digests=[image] if image else [],
    )


def _daily_config(engine, grant):
    # Changed with the grant pointer redesign: the image no longer pins the grant
    # digest or the approval phrase digest, because the grant binds the image digest
    # and an image naming its own grant cannot be built. Both now reach the cycle
    # through the owner's pointer object, which _wiring places beside the grant.
    config = _config()
    config["jobs"][DAILY_JOB_ID]["mode"] = "daily"
    config["daily_profile"]["source_policy_digest"] = SOURCE_POLICY
    return config


def _pointer_bytes(engine, grant, *, image=IMAGE_DIGEST, sequence=1, phrase=None):
    return managed_runtime.grant_pointer_bytes(
        image_digest=image,
        sequence=sequence,
        recurring_grant_digest=engine.recurring_grant.grant_digest(grant),
        approval_phrase_sha256=APPROVAL_PHRASE if phrase is None else phrase,
    )


def _place_pointer(client, engine, grant, *, image=IMAGE_DIGEST, sequence=1, raw=None):
    """Leave the owner's pointer where the owner's create once write leaves it.

    Placed straight into the double's objects, so the writes the cycle makes stay
    exactly the writes the tests count.
    """
    name = managed_runtime.grant_pointer_name(image, sequence)
    payload = _pointer_bytes(engine, grant, image=image, sequence=sequence)
    client.objects[name] = (payload if raw is None else raw, 1)
    return name


def _effective_profile(config, grant, engine):
    return {
        **config["daily_profile"],
        "recurring_grant_digest": engine.recurring_grant.grant_digest(grant),
        "approval_phrase_sha256": APPROVAL_PHRASE,
    }


def _succeeding_handlers(seen):
    def handlers(invocation, profile, timeline):
        def handler(*, manifest, authority_receipt):
            seen.append((manifest["stage"], authority_receipt["business_attempt_id"]))
            if manifest["stage"] == "capture":
                timeline["snapshot_as_of"] = "2026-09-20T04:31:00+00:00"
                timeline["capture_available_at"] = "2026-09-20T04:32:00+00:00"
            return {
                "state": "succeeded",
                "input_digest": hashlib.sha256(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "output_digest": "9" * 64,
                "result_reference": authority_receipt["business_attempt_id"],
                "retry_safe": False,
            }

        return dict.fromkeys(
            ("collect", "capture", "compose", "certify", "release"), handler
        )

    return handlers


def _wiring(
    engine,
    grant,
    *,
    client=None,
    handlers=None,
    grant_loader=None,
    pointer=True,
    image=IMAGE_DIGEST,
):
    config = _daily_config(engine, grant)
    clock = {"now": NOW}
    client = client if client is not None else FakeObjectClient()
    if pointer:
        _place_pointer(client, engine, grant, image=image)
    wiring = {
        "engine": engine,
        "profile": config["daily_profile"],
        "grant_loader": grant_loader or (lambda pins: grant),
        "object_client": client,
        "stage_handlers": handlers or (lambda *_args: None),
        "now": lambda: clock["now"],
        "environment": "staging",
    }
    return wiring, config, clock


def test_configuration_carries_the_proposed_daily_profile_and_refuses_drift():
    profile = _config()["daily_profile"]
    assert profile["schema_version"] == "42_daily_v1"
    assert profile["resource_manifest_digest"] == REVIEWED_MANIFEST_SHA256
    assert profile["freshness_target_hours"] == 30
    assert profile["max_publish_lag_hours"] == 48
    assert profile["max_catchup_cutoffs"] == 2
    assert profile["cutoffs_per_cycle"] == 1
    # Changed with the grant pointer redesign: the two grant pins left the image, so
    # the tree profile carries neither, and carrying either one (well formed or not)
    # is now the drift that refuses.
    assert "approval_phrase_sha256" not in profile
    assert "recurring_grant_digest" not in profile
    for mutate in (
        lambda c: c["daily_profile"].pop("cutoffs_per_cycle"),
        lambda c: c["daily_profile"].update(approval_phrase_sha256="a" * 64),
        lambda c: c["daily_profile"].update(approval_phrase_sha256="zz"),
        lambda c: c["daily_profile"].update(cutoffs_per_cycle=2),
        lambda c: c["daily_profile"].update(resource_manifest_digest="zz"),
        lambda c: c["daily_profile"].update(recurring_grant_digest="a" * 64),
        lambda c: c["daily_profile"].update(recurring_grant_digest="zz"),
        lambda c: c["daily_profile"].update(max_publish_lag_hours=24),
        lambda c: c.pop("daily_profile"),
    ):
        config = _config()
        mutate(config)
        with pytest.raises(ValueError, match="^runtime_configuration_invalid$"):
            _build(config)


def test_engine_daily_adapter_binds_the_kernel_and_its_companions():
    engine = _engine()
    assert callable(engine.daily_cycle.run_validated_daily_cycle)
    assert callable(engine.daily_authority.DailyAuthority)
    assert callable(engine.daily_store.DailyStore)
    assert callable(engine.recurring_grant.check_grant)

    def importer(name):
        raise ImportError(name)

    with pytest.raises(ValueError, match="^daily_adapter_unavailable$"):
        engine_daily_adapter(importer=importer)


def test_engine_adapter_describe_reports_unbound_operations_as_none():
    class Origin:
        contract_sha256 = "6" * 64

        def __init__(self):
            self.operation_bindings = {
                "r3_release": SimpleNamespace(
                    job_resource="projects/ogilvy-trends-v2/locations/"
                    "us-central1/jobs/intelligence-42-daily-staging"
                )
            }

    class Generation:
        registry = "registry"

    class Generations:
        @staticmethod
        def active_generation():
            return Generation()

    class Module:
        execution_generations = Generations

        @staticmethod
        def _v2_origin_for_operation(operation, *, mode, registry):
            if operation != "r3_release":
                raise ValueError("execution_origin_unknown")
            return Origin()

    adapter = engine_authority_adapter(importer=lambda _name: Module)
    assert adapter.describe("collection") is None
    assert adapter.describe("r3_release") == {
        "operation": "r3_release",
        "contract_sha256": "6" * 64,
    }

    class Bare:
        pass

    assert (
        engine_authority_adapter(importer=lambda _name: Bare).describe("r3_release")
        is None
    )


def test_observation_cutoff_is_the_latest_closed_utc_midnight():
    assert observation_cutoff(NOW) == datetime(2026, 9, 20, tzinfo=UTC)
    assert observation_cutoff(datetime(2026, 9, 20, tzinfo=UTC)) == datetime(
        2026, 9, 20, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="^daily_clock_invalid$"):
        observation_cutoff(datetime(2026, 9, 20))  # noqa: DTZ001


def test_daily_firing_without_grant_refuses_grant_missing():
    engine = _engine()
    grant = _grant(engine)
    reservation = FakeReservation()
    client = FakeObjectClient()
    wiring, config, _clock = _wiring(
        engine, grant, client=client, grant_loader=lambda d: None
    )
    with pytest.raises(ValueError, match="^grant_missing$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.calls == []
    assert client.writes == []


def test_daily_firing_with_revoked_grant_refuses_before_any_claim():
    engine = _engine()
    grant = _grant(engine)
    revoked = {**grant, "revocation_state": "revoked"}
    reservation = FakeReservation()
    client = FakeObjectClient()
    wiring, config, _clock = _wiring(
        engine, grant, client=client, grant_loader=lambda digest: revoked
    )
    with pytest.raises(ValueError, match="^grant_revoked$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.calls == []
    assert client.writes == []


def test_daily_firing_refuses_operation_unbound_before_any_claim():
    engine = _engine()
    grant = _grant(engine)
    reservation = FakeReservation(unbound={"collection_exposure_issue"})
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    with pytest.raises(ValueError, match="^operation_unbound$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.paid_calls == []
    assert ("describe", "collection_exposure_issue") in reservation.calls
    assert client.writes == []
    assert seen == []

    class NoDescribe:
        pass

    with pytest.raises(ValueError, match="^operation_unbound$"):
        run_managed_mode(_build(config), authority=NoDescribe(), daily=wiring)
    assert client.writes == []


def _grant_object(client, engine, grant, *, approval=None):
    digest = engine.recurring_grant.grant_digest(grant)
    client.write(
        f"42/daily/grants/{digest}.json",
        json.dumps({"grant": grant}, sort_keys=True, separators=(",", ":")).encode(),
        if_generation_match=0,
    )
    return digest


def test_native_grant_loader_binds_the_approval_to_the_pointer_phrase():
    # Changed with the grant pointer redesign: the loader no longer takes the phrase
    # digest from the image profile. It takes both pins from the owner's pointer and
    # still requires the durable approval to carry exactly that phrase digest, the
    # grant's issuing principal and the grant digest.
    engine = _engine()
    grant = _grant(engine)
    client = FakeObjectClient()
    digest = _grant_object(client, engine, grant)
    pins = {"recurring_grant_digest": digest, "approval_phrase_sha256": APPROVAL_PHRASE}
    approved = {
        "approved_by": grant["issuing_principal"],
        "approved_at": "2026-09-15T08:00:00.000000Z",
        "approval_phrase_sha256": APPROVAL_PHRASE,
        "manifest_sha256": digest,
    }
    reads = []

    def reader(record):
        def read(requested):
            reads.append(requested)
            return record

        return read

    loader = _native_grant_loader(engine, client, reader(approved))
    assert loader(pins) == engine.recurring_grant.validate_recurring_grant(grant)
    assert reads == [digest]
    assert _native_grant_loader(engine, client, reader(None))(pins) is None
    assert (
        _native_grant_loader(engine, FakeObjectClient(), reader(approved))(pins) is None
    )
    for wrong in (
        {**approved, "approval_phrase_sha256": "2" * 64},
        {**approved, "approved_by": "someone else"},
        {**approved, "manifest_sha256": "3" * 64},
        {"approved_by": grant["issuing_principal"]},
    ):
        with pytest.raises(ValueError, match="^grant_approval_mismatch$"):
            _native_grant_loader(engine, client, reader(wrong))(pins)
    # A pointer naming another phrase than the one the approver sent refuses too.
    with pytest.raises(ValueError, match="^grant_approval_mismatch$"):
        _native_grant_loader(engine, client, reader(approved))(
            {**pins, "approval_phrase_sha256": "4" * 64}
        )
    for bad in (
        {**pins, "recurring_grant_digest": "zz"},
        {**pins, "approval_phrase_sha256": "zz"},
        {"recurring_grant_digest": digest},
        {**pins, "extra": "x"},
        digest,
    ):
        with pytest.raises(ValueError, match="^grant_record_invalid$"):
            _native_grant_loader(engine, client, reader(approved))(bad)
    forged = FakeObjectClient()
    forged.write(
        f"42/daily/grants/{digest}.json",
        json.dumps({"grant": grant, "approval": approved}, sort_keys=True).encode(),
        if_generation_match=0,
    )
    with pytest.raises(ValueError, match="^grant_record_invalid$"):
        _native_grant_loader(engine, forged, reader(approved))(pins)
    client.write(
        f"42/daily/grants/{digest}/revocation.json", b"{}", if_generation_match=0
    )
    revoked = _native_grant_loader(engine, client, reader(approved))(pins)
    assert revoked["revocation_state"] == "revoked"
    assert engine.recurring_grant.grant_digest(revoked) == digest


# The grant pointer. The image cannot pin its grant: the grant binds the image digest,
# so an image carrying the digest of a grant that names the image's own digest cannot
# be built. The owner writes a create once pointer per image under
# 42/daily/grants/pointers/ naming the grant digest and the approval phrase digest, and
# the cycle reads it for the image it runs as, before the grant and before any claim.


def test_the_tree_configuration_is_independent_of_every_grant():
    engine = _engine()
    config = _config()
    assert set(config["daily_profile"]) == set(managed_runtime.IMAGE_PROFILE_FIELDS)
    assert set(managed_runtime.IMAGE_PROFILE_FIELDS) | set(
        managed_runtime.GRANT_POINTER_FIELDS
    ) == set(managed_runtime.DAILY_PROFILE_FIELDS)
    first = _grant(engine)
    second = _grant(engine, image="sha256:" + "6" * 64)
    assert engine.recurring_grant.grant_digest(first) != (
        engine.recurring_grant.grant_digest(second)
    )
    assert _daily_config(engine, first) == _daily_config(engine, second)


@pytest.mark.parametrize("image", [IMAGE_DIGEST, "sha256:" + "6" * 64])
def test_a_grant_rendered_after_the_build_runs_the_cycle_through_the_pointer(image):
    engine = _engine()
    grant = _grant(engine, image=image)
    reservation = FakeReservation()
    client = FakeObjectClient()
    seen = []
    asked = []

    def loader(pins):
        asked.append(dict(pins))
        return grant

    wiring, config, _clock = _wiring(
        engine,
        grant,
        client=client,
        handlers=_succeeding_handlers(seen),
        grant_loader=loader,
        image=image,
    )
    repository = config["jobs"][DAILY_JOB_ID]["image_repository"]
    invocation = _build(
        config, readback=_readback(config, image=f"{repository}@{image}")
    )
    receipt = run_managed_mode(invocation, authority=reservation, daily=wiring)
    digest = engine.recurring_grant.grant_digest(grant)
    assert receipt["status"] == "release_pending"
    assert receipt["grant_digest"] == digest
    assert asked == [
        {"recurring_grant_digest": digest, "approval_phrase_sha256": APPROVAL_PHRASE}
    ]
    assert reservation.consumed == 4


def test_daily_firing_without_a_pointer_refuses_before_the_grant_is_read():
    engine = _engine()
    grant = _grant(engine)
    reservation = FakeReservation()
    client = FakeObjectClient()
    reads = []

    def loader(pins):
        reads.append(pins)
        return grant

    wiring, config, _clock = _wiring(
        engine, grant, client=client, grant_loader=loader, pointer=False
    )
    # A pointer written for another image is no pointer for this one.
    _place_pointer(client, engine, grant, image="sha256:" + "6" * 64)
    with pytest.raises(ValueError, match="^grant_pointer_missing$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reads == []
    assert reservation.calls == []
    assert client.writes == []


def _pointer_document(engine, grant, **overrides):
    document = json.loads(_pointer_bytes(engine, grant))
    document.update(overrides)
    return document


def _canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize(
    "alter",
    [
        lambda engine, grant: _pointer_bytes(engine, grant) + b"\n",
        lambda engine, grant: json.dumps(
            json.loads(_pointer_bytes(engine, grant)), indent=2
        ).encode(),
        lambda engine, grant: b"not json",
        lambda engine, grant: _canonical([1]),
        lambda engine, grant: _canonical(_pointer_document(engine, grant, extra="x")),
        lambda engine, grant: _canonical(
            {
                key: value
                for key, value in _pointer_document(engine, grant).items()
                if key != "approval_phrase_sha256"
            }
        ),
        lambda engine, grant: _canonical(
            _pointer_document(
                engine, grant, contract_version="42_daily_grant_pointer_v0"
            )
        ),
        lambda engine, grant: _canonical(
            _pointer_document(engine, grant, image_digest="sha256:" + "6" * 64)
        ),
        lambda engine, grant: _canonical(
            _pointer_document(
                engine,
                grant,
                job_resource=DAILY_JOB_RESOURCE.replace("daily", "weekly"),
            )
        ),
        lambda engine, grant: _canonical(_pointer_document(engine, grant, sequence=2)),
        lambda engine, grant: _canonical(
            _pointer_document(engine, grant, sequence=True)
        ),
        lambda engine, grant: _canonical(
            _pointer_document(engine, grant, recurring_grant_digest="Z" * 64)
        ),
        lambda engine, grant: _canonical(
            _pointer_document(engine, grant, approval_phrase_sha256="1" * 63)
        ),
    ],
)
def test_an_altered_pointer_refuses_before_the_grant_is_read(alter):
    engine = _engine()
    grant = _grant(engine)
    reservation = FakeReservation()
    client = FakeObjectClient()
    reads = []

    def loader(pins):
        reads.append(pins)
        return grant

    wiring, config, _clock = _wiring(
        engine, grant, client=client, grant_loader=loader, pointer=False
    )
    _place_pointer(client, engine, grant, raw=alter(engine, grant))
    with pytest.raises(ValueError, match="^grant_pointer_invalid$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reads == []
    assert reservation.calls == []
    assert client.writes == []


def test_a_later_pointer_supersedes_and_a_gap_ends_the_search():
    engine = _engine()
    first = _grant(engine)
    second = engine.recurring_grant.proposed_grant(
        identities=_manifest()["identities"],
        issuing_principal=APPROVER_HASH,
        resource_manifest_digest=ACTIVE_RESOURCE_MANIFEST_SHA256,
        source_policy_digest=SOURCE_POLICY,
        valid_from=datetime(2026, 9, 16, tzinfo=UTC),
        permitted_image_digests=[IMAGE_DIGEST],
    )
    ignored = _grant(engine, image="sha256:" + "6" * 64)
    by_digest = {
        engine.recurring_grant.grant_digest(grant): grant
        for grant in (first, second, ignored)
    }
    asked = []

    def loader(pins):
        asked.append(pins["recurring_grant_digest"])
        return by_digest[pins["recurring_grant_digest"]]

    client = FakeObjectClient()
    wiring, config, _clock = _wiring(
        engine,
        first,
        client=client,
        handlers=_succeeding_handlers([]),
        grant_loader=loader,
    )
    _place_pointer(client, engine, second, sequence=2)
    # Sequence 3 is absent, so a pointer at 4 is never reached.
    _place_pointer(client, engine, ignored, sequence=4)
    receipt = run_managed_mode(
        _build(config), authority=FakeReservation(), daily=wiring
    )
    assert asked == [engine.recurring_grant.grant_digest(second)]
    assert receipt["grant_digest"] == engine.recurring_grant.grant_digest(second)
    assert managed_runtime.read_grant_pointer(client, IMAGE_DIGEST) == {
        "recurring_grant_digest": engine.recurring_grant.grant_digest(second),
        "approval_phrase_sha256": APPROVAL_PHRASE,
    }


def test_a_pointer_to_a_grant_without_this_image_refuses_before_any_claim():
    engine = _engine()
    grant = _grant(engine, image="sha256:" + "5" * 64)
    reservation = FakeReservation()
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    with pytest.raises(ValueError, match="^recurring_grant_image_forbidden$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.calls == []
    assert client.writes == []
    assert seen == []


def test_an_unapproved_pointer_stops_at_grant_missing_through_the_native_loader():
    engine = _engine()
    grant = _grant(engine)
    reservation = FakeReservation()
    client = FakeObjectClient()
    digest = _grant_object(client, engine, grant)
    writes_before = list(client.writes)
    approvals = {}
    loader = _native_grant_loader(engine, client, approvals.get)
    wiring, config, _clock = _wiring(
        engine,
        grant,
        client=client,
        handlers=_succeeding_handlers([]),
        grant_loader=loader,
    )
    with pytest.raises(ValueError, match="^grant_missing$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    approvals[digest] = {
        "approved_by": grant["issuing_principal"],
        "approved_at": "2026-09-15T08:00:00.000000Z",
        "approval_phrase_sha256": "4" * 64,
        "manifest_sha256": digest,
    }
    with pytest.raises(ValueError, match="^grant_approval_mismatch$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.calls == []
    assert client.writes == writes_before
    approvals[digest]["approval_phrase_sha256"] = APPROVAL_PHRASE
    receipt = run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert receipt["status"] == "release_pending"
    assert receipt["grant_digest"] == digest


def test_the_pointer_names_are_create_once_and_bounded():
    engine = _engine()
    grant = _grant(engine)
    hex_digest = IMAGE_DIGEST.removeprefix("sha256:")
    assert managed_runtime.GRANT_POINTER_PREFIX == "42/daily/grants/pointers"
    assert managed_runtime.grant_pointer_name(IMAGE_DIGEST, 1) == (
        f"42/daily/grants/pointers/{hex_digest}/000001.json"
    )
    document = json.loads(_pointer_bytes(engine, grant, sequence=3))
    assert document == {
        "approval_phrase_sha256": APPROVAL_PHRASE,
        "contract_version": "42_daily_grant_pointer_v1",
        "image_digest": IMAGE_DIGEST,
        "job_resource": DAILY_JOB_RESOURCE,
        "recurring_grant_digest": engine.recurring_grant.grant_digest(grant),
        "sequence": 3,
    }
    assert _pointer_bytes(engine, grant, sequence=3) == _canonical(document)
    for sequence in (0, -1, managed_runtime.MAX_GRANT_POINTERS + 1, True, "1"):
        with pytest.raises(ValueError, match="^grant_pointer_invalid$"):
            managed_runtime.grant_pointer_name(IMAGE_DIGEST, sequence)
    for image in ("4" * 64, "sha256:" + "4" * 63, "sha256:" + "A" * 64, None):
        with pytest.raises(ValueError, match="^grant_pointer_invalid$"):
            managed_runtime.grant_pointer_name(image, 1)
    with pytest.raises(ValueError, match="^grant_pointer_invalid$"):
        _pointer_bytes(engine, grant, phrase="zz")
    # Every pointer up to the bound is read, and nothing beyond it.
    client = FakeObjectClient()
    for sequence in range(1, managed_runtime.MAX_GRANT_POINTERS + 1):
        _place_pointer(client, engine, grant, sequence=sequence)
    reads = []
    original = client.read

    def counting(name):
        reads.append(name)
        return original(name)

    client.read = counting
    assert managed_runtime.read_grant_pointer(client, IMAGE_DIGEST) == {
        "recurring_grant_digest": engine.recurring_grant.grant_digest(grant),
        "approval_phrase_sha256": APPROVAL_PHRASE,
    }
    assert len(reads) == managed_runtime.MAX_GRANT_POINTERS


def test_daily_profile_bound_to_another_manifest_refuses_before_the_grant_is_read():
    engine = _engine()
    grant = _grant(engine)
    reservation = FakeReservation()
    reads = []

    def loader(digest):
        reads.append(digest)
        return grant

    wiring, config, _clock = _wiring(engine, grant, grant_loader=loader)
    wiring["profile"] = {**wiring["profile"], "resource_manifest_digest": "a" * 64}
    with pytest.raises(ValueError, match="^daily_profile_manifest_mismatch$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reads == []
    assert reservation.calls == []


def test_daily_firing_with_another_grant_refuses_digest_mismatch():
    engine = _engine()
    grant = _grant(engine)
    other = _grant(engine, image="sha256:" + "5" * 64)
    reservation = FakeReservation()
    wiring, config, _clock = _wiring(engine, grant, grant_loader=lambda digest: other)
    with pytest.raises(ValueError, match="^grant_digest_mismatch$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.calls == []


def test_daily_firing_with_unavailable_handlers_stops_before_any_paid_call():
    engine = _engine()
    grant = _grant(engine)
    reservation = FakeReservation()
    client = FakeObjectClient()
    wiring, config, _clock = _wiring(engine, grant, client=client)
    with pytest.raises(ValueError, match="^stage_adapter_unavailable$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.calls == []
    assert client.writes == []


def test_daily_cycle_runs_to_release_pending_and_duplicate_trigger_reuses_operation_id():
    engine = _engine()
    grant = _grant(engine)
    reservation = FakeReservation()
    client = FakeObjectClient()
    seen = []
    wiring, config, clock = _wiring(
        engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    receipt = run_managed_mode(_build(config), authority=reservation, daily=wiring)
    expected_operation = engine.daily_store.slot_operation_id(
        environment="staging",
        source_policy_digest=SOURCE_POLICY,
        cutoff_utc=datetime(2026, 9, 20, tzinfo=UTC),
    )
    assert receipt["mode"] == "daily"
    assert receipt["status"] == "release_pending"
    assert receipt["launched"] is True
    assert receipt["operation_id"] == expected_operation
    assert receipt["cycle"] == {
        "state": "release_pending",
        "operation_id": expected_operation,
        "stage": "release",
    }
    assert receipt["stages_dispatched"] == ["collect", "capture", "compose", "certify"]
    assert receipt["observation_window_end"] == "2026-09-20T00:00:00+00:00"
    assert receipt["collection_started_at"] == NOW.isoformat()
    assert receipt["collection_completed_at"] == NOW.isoformat()
    assert receipt["snapshot_as_of"] == "2026-09-20T04:31:00+00:00"
    assert receipt["capture_available_at"] == "2026-09-20T04:32:00+00:00"
    assert receipt["grant_digest"] == engine.recurring_grant.grant_digest(grant)
    assert reservation.consumed == 4
    assert [stage for stage, _attempt in seen] == [
        "collect",
        "capture",
        "compose",
        "certify",
    ]
    assert all(attempt.startswith("bat_") for _stage, attempt in seen)
    control_name = f"42/daily/slots/{expected_operation}/control.json"
    assert control_name in client.writes
    authority_records = [
        n for n in client.writes if n.startswith("42/daily/authority/")
    ]
    assert len(authority_records) == 4
    persisted = json.loads(client.objects[control_name][0])["timeline"]
    assert persisted == {
        "observation_window_end": "2026-09-20T00:00:00+00:00",
        "collection_started_at": NOW.isoformat(),
        "collection_completed_at": NOW.isoformat(),
        "snapshot_as_of": "2026-09-20T04:31:00+00:00",
        "capture_available_at": "2026-09-20T04:32:00+00:00",
    }
    assert receipt["timeline"] == persisted
    clock["now"] = NOW + timedelta(minutes=7)
    duplicate = run_managed_mode(
        _build(
            config,
            execution="intelligence-42-daily-staging-dup42",
            readback=_readback(config, execution="intelligence-42-daily-staging-dup42"),
        ),
        authority=reservation,
        daily=wiring,
    )
    assert duplicate["operation_id"] == expected_operation
    assert duplicate["status"] == "release_pending"
    assert duplicate["launched"] is False
    assert duplicate["stages_dispatched"] == []
    assert duplicate["collection_started_at"] is None
    assert duplicate["timeline"] == persisted
    assert json.loads(client.objects[control_name][0])["timeline"] == persisted
    assert reservation.consumed == 4
    assert len(seen) == 4


def _grant_approval(engine, grant):
    """The durable approval row the v3 read routine returns for the grant, typed under
    the engine's active generation the way the native approval reader types it."""
    execution_approval = importlib.import_module(
        "src.analysis.open_intelligence.execution_approval"
    )
    phrases = importlib.import_module(
        "src.analysis.open_intelligence.recurring_grant_phrases"
    )
    generation = execution_approval.execution_generations.active_generation()
    digest = engine.recurring_grant.grant_digest(grant)
    approved_at = datetime(2026, 9, 15, 8, 30, tzinfo=UTC)
    proposal_sha256 = "c" * 64
    row = {
        "approval_contract_version": "open_intelligence_execution_approval_v2",
        "approval_id": execution_approval.approval_id_v2(
            digest,
            APPROVER_HASH,
            approved_at,
            origin_registry_sha256=generation.origin_registry_sha256,
            resource_manifest_sha256=generation.resource_manifest_sha256,
        ),
        "manifest_version": "42_recurring_execution_grant_v1",
        "operation": "recurring_grant_v1",
        "contract_sha256": proposal_sha256,
        "manifest_sha256": digest,
        "canonical_manifest_json": phrases.canonical_grant_bytes(grant).decode("utf-8"),
        "approved_by": APPROVER_HASH,
        "approved_at": approved_at,
        "expires_at": datetime.fromisoformat(grant["valid_until"]),
        "approval_phrase_sha256": phrases.approval_phrase_sha256(proposal_sha256),
        "origin_registry_sha256": generation.origin_registry_sha256,
        "resource_manifest_sha256": generation.resource_manifest_sha256,
        "revocation_state": "active",
        "revoked_at": None,
    }
    return execution_approval.RecurringGrantApproval(
        **row,
        mode="new_consume",
        registry=generation.registry,
        expected_resource_manifest_sha256=generation.resource_manifest_sha256,
    )


def _grant_naming(engine, digest):
    return engine.recurring_grant.proposed_grant(
        identities=_manifest()["identities"],
        issuing_principal=APPROVER_HASH,
        resource_manifest_digest=digest,
        source_policy_digest=SOURCE_POLICY,
        valid_from=datetime(2026, 9, 15, tzinfo=UTC),
        permitted_image_digests=[IMAGE_DIGEST],
    )


def test_a_grant_naming_the_active_generation_manifest_passes_approval_and_the_cycle():
    """One grant must pass both sides. The approval side (the approve routine and the
    durable reader) binds the grant to the engine's active generation manifest; the
    daily cycle must check the grant against that same manifest, while the invocation
    and the profile keep the ops deploy manifest."""
    engine = _engine()
    generations = importlib.import_module(
        "src.analysis.open_intelligence.execution_generations"
    )
    assert generations.ACTIVE_GENERATION_PAIR[1] == ACTIVE_RESOURCE_MANIFEST_SHA256
    assert ACTIVE_RESOURCE_MANIFEST_SHA256 != REVIEWED_MANIFEST_SHA256
    execution_approval = importlib.import_module(
        "src.analysis.open_intelligence.execution_approval"
    )
    grant = _grant_naming(engine, ACTIVE_RESOURCE_MANIFEST_SHA256)
    approval = _grant_approval(engine, grant)
    assert (
        approval.grant()["resource_manifest_digest"] == ACTIVE_RESOURCE_MANIFEST_SHA256
    )
    stale = _grant_naming(engine, REVIEWED_MANIFEST_SHA256)
    with pytest.raises(
        execution_approval.ApprovalRefusal,
        match="^execution_approval_manifest_invalid$",
    ):
        _grant_approval(engine, stale)

    reservation = FakeReservation()
    client = FakeObjectClient()
    seen = []
    wiring, config, _clock = _wiring(
        engine, grant, client=client, handlers=_succeeding_handlers(seen)
    )
    assert (
        config["daily_profile"]["resource_manifest_digest"] == REVIEWED_MANIFEST_SHA256
    )
    invocation = _build(config)
    assert invocation["resource_manifest_digest"] == REVIEWED_MANIFEST_SHA256
    receipt = run_managed_mode(invocation, authority=reservation, daily=wiring)
    assert receipt["status"] == "release_pending"
    assert receipt["grant_digest"] == engine.recurring_grant.grant_digest(grant)
    assert reservation.consumed == 4


def test_a_grant_naming_the_ops_deploy_manifest_refuses_before_any_paid_call():
    engine = _engine()
    stale = _grant_naming(engine, REVIEWED_MANIFEST_SHA256)
    reservation = FakeReservation()
    seen = []
    wiring, config, _clock = _wiring(engine, stale, handlers=_succeeding_handlers(seen))
    with pytest.raises(ValueError, match="^recurring_grant_manifest_mismatch$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reservation.paid_calls == []
    assert seen == []


@pytest.mark.parametrize(
    "generations",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(ACTIVE_GENERATION_PAIR=["a" * 64, "b" * 64]),
        SimpleNamespace(ACTIVE_GENERATION_PAIR=("a" * 64,)),
        SimpleNamespace(ACTIVE_GENERATION_PAIR=("a" * 64, "B" * 64)),
    ],
)
def test_daily_cycle_without_an_active_generation_refuses_before_the_grant_is_read(
    generations,
):
    engine = _engine()
    grant = _grant(engine)
    modules = {name: getattr(engine, name) for name in managed_runtime._DAILY_MODULES}
    modules["execution_generations"] = generations
    reads = []

    def loader(digest):
        reads.append(digest)
        return grant

    reservation = FakeReservation()
    wiring, config, _clock = _wiring(
        SimpleNamespace(**modules), grant, grant_loader=loader
    )
    with pytest.raises(ValueError, match="^daily_wiring_unavailable$"):
        run_managed_mode(_build(config), authority=reservation, daily=wiring)
    assert reads == []
    assert reservation.calls == []


def test_daily_mode_override_in_environment_stays_forbidden():
    with pytest.raises(ValueError, match="^runtime_override_forbidden$"):
        _build(environment={"MANAGED_RUNTIME_MODE": "daily"})


# D04: the native stage clients behind the daily seam


class FakeWarehouseClient:
    """A BigQuery client double whose every query answers no rows."""

    def __init__(self):
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append(sql)
        return SimpleNamespace(result=lambda **_kwargs: iter(()))


class ProducerTerminal:
    """In process double of the producer's collection only terminal.

    It records the receipt through the ledger it is handed under the exact authority it
    receives, exactly where the real terminal commits, and never reaches a connector. It
    reads no identity from the environment.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        from scripts.staging.collect_42_sources import collection_receipt

        self.calls.append(dict(kwargs))
        ledger = kwargs["receipt_ledger"]
        authority = dict(kwargs["collection_authority"])
        prior = ledger.operation(authority["execution_id"])
        if prior is not None:
            return prior
        receipt = collection_receipt(
            run_id="collect-2026-09-19",
            **authority,
            cutoff="2026-09-19",
            market_states={"za": "collected", "ng": "collected", "ke": "collected"},
            raw_count=10,
            enriched_count=5,
        )
        ledger.record(receipt, trend_date="2026-09-20")
        return dict(receipt)


def _native_environment(monkeypatch):
    image = f"us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@{IMAGE_DIGEST}"
    for variable, value in (
        ("TRENDS_ENV", "staging"),
        ("BIGQUERY_DATASET", "intelligence_42_sources_staging"),
        ("CLOUD_RUN_EXECUTION", EXECUTION),
        # A decoy: the exact authority must carry the wiring's build, never this value.
        ("COLLECTION_SOURCE_SHA", "d" * 40),
        ("COLLECTION_IMAGE_URI", image),
        ("COLLECTION_POLICY_SHA256", SOURCE_POLICY),
        ("COLLECTION_PROFILE_SHA256", "6" * 64),
        ("GOOGLE_APPLICATION_CREDENTIALS", "secret-credential-path-never-reported"),
        ("CLOUDSDK_AUTH_ACCESS_TOKEN", "secret-token-never-reported"),
    ):
        monkeypatch.setenv(variable, value)


def _native_fixture(monkeypatch):
    engine = _engine()
    grant = _grant(engine)
    client = FakeObjectClient()
    warehouse = FakeWarehouseClient()
    terminal = ProducerTerminal()
    producer = importlib.import_module("scripts.run_rss_now")
    monkeypatch.setattr(producer, "_run_impl", terminal)
    _native_environment(monkeypatch)
    wiring, config, clock = _wiring(
        engine, grant, client=client, handlers=native_stage_handlers
    )
    wiring["warehouse"] = warehouse
    wiring["build"] = {
        "source_sha": "a" * 40,
        "image_uri": (
            "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@"
            + IMAGE_DIGEST
        ),
    }
    return SimpleNamespace(
        engine=engine,
        grant=grant,
        client=client,
        warehouse=warehouse,
        terminal=terminal,
        wiring=wiring,
        config=config,
        clock=clock,
    )


def _expected_slot(engine):
    return engine.daily_store.slot_operation_id(
        environment="staging",
        source_policy_digest=SOURCE_POLICY,
        cutoff_utc=datetime(2026, 9, 20, tzinfo=UTC),
    )


def test_daily_wiring_binds_the_native_stage_handlers(monkeypatch):
    fixture = _native_fixture(monkeypatch)
    bound = _daily_wiring(fixture.wiring)["stage_handlers"]
    assert isinstance(bound, functools.partial)
    assert bound.func is native_stage_handlers
    assert bound.keywords["engine"] is fixture.engine
    assert bound.keywords["object_client"] is fixture.client
    assert bound.keywords["warehouse"] is fixture.warehouse
    assert bound.keywords["now"] is fixture.wiring["now"]
    monkeypatch.setattr(
        managed_runtime, "_native_object_client", lambda engine: FakeObjectClient()
    )
    monkeypatch.setattr(
        managed_runtime, "_native_warehouse", lambda: FakeWarehouseClient()
    )
    default = default_daily_wiring(fixture.config)
    assert default["stage_handlers"] is native_stage_handlers
    assert isinstance(default["warehouse"], FakeWarehouseClient)
    assert unavailable_stage_handlers(None, None, None) is None
    seam = dict(fixture.wiring, stage_handlers=lambda *_args: None)
    assert _daily_wiring(seam)["stage_handlers"] is seam["stage_handlers"]
    unavailable = dict(fixture.wiring, stage_handlers=unavailable_stage_handlers)
    assert _daily_wiring(unavailable)["stage_handlers"] is unavailable_stage_handlers
    reservation = FakeReservation()
    with pytest.raises(ValueError, match=r"^stage_adapter_unavailable$"):
        run_managed_mode(
            _build(fixture.config), authority=reservation, daily=unavailable
        )
    assert reservation.calls == []
    assert fixture.client.writes == []
    assert fixture.terminal.calls == []


def test_daily_plan_reports_the_clients_without_any_dispatch(monkeypatch):
    fixture = _native_fixture(monkeypatch)
    # Changed with the grant pointer redesign: the kernel profile carries the grant
    # digest, which the image no longer holds, so the plan reads the pointer for the
    # image it is asked about and digests the profile the cycle would run.
    plan = daily_plan(
        daily=fixture.wiring, image_uri=fixture.wiring["build"]["image_uri"]
    )
    assert plan["mode"] == "daily-plan"
    assert plan["launched"] is False
    assert plan["operation_id"] == _expected_slot(fixture.engine)
    assert plan["cutoff_utc"] == "2026-09-20T00:00:00+00:00"
    effective = _effective_profile(fixture.config, fixture.grant, fixture.engine)
    assert plan["profile_digest"] == canonical_sha256(
        {name: effective[name] for name in KERNEL_PROFILE_FIELDS}
    )
    assert plan["release_profile"] == {
        "run_id": "run_20260919_staging_daily_v1",
        "cutoff": "2026-09-19",
        "is_replay": False,
        "independence_policy": "explicit_origin_v2",
    }
    assert list(plan["clients"]) == [
        "clock",
        "store",
        "collection_profile",
        "collector",
        "collection_ledger",
        "source_runs",
        "generation",
        "capture",
        "composer",
        "persist",
        "warehouse",
        "certifier",
        "release_profile",
        "products",
    ]
    assert plan["bound"] == [
        "clock",
        "store",
        "collection_profile",
        "collector",
        "collection_ledger",
        "source_runs",
        "generation",
        "capture",
        "warehouse",
        "certifier",
        "release_profile",
        "products",
    ]
    assert len(plan["bound"]) == 12
    assert plan["unbound"] == {
        "composer": "client_unbound:composer",
        "persist": "client_unbound:persist",
    }
    assert plan["clients"]["composer"] == {
        "bound": False,
        "binding": "unbound",
        "code": "client_unbound:composer",
    }
    # Bound over the native runner seam, so the plan carries no capture refusal.
    assert plan["clients"]["capture"] == {
        "bound": True,
        "binding": "src.analysis.open_intelligence.daily_native_clients.NativeCapture",
        "code": None,
    }
    # The products are bound over the reviewed history factory, so no bound client
    # carries a refusal; compose stays refused through its unbound composer.
    assert [
        name
        for name, entry in plan["clients"].items()
        if entry["bound"] and entry["code"]
    ] == []
    assert {
        name: entry["code"] for name, entry in plan["clients"].items() if entry["bound"]
    } == dict.fromkeys(plan["bound"])
    assert plan["clients"]["certifier"] == {
        "bound": True,
        "binding": "src.analysis.open_intelligence.daily_certification.NativeCertifier",
        "code": None,
    }
    assert plan["clients"]["store"]["bound"] is True
    serialized = json.dumps(plan, sort_keys=True)
    assert "secret-credential-path-never-reported" not in serialized
    assert "secret-token-never-reported" not in serialized
    assert fixture.terminal.calls == []
    assert fixture.warehouse.calls == []
    assert fixture.client.writes == []
    monkeypatch.setenv("COLLECTION_POLICY_SHA256", "1" * 64)
    with pytest.raises(ValueError, match="^collection_policy_mismatch$"):
        daily_plan(daily=fixture.wiring, image_uri=fixture.wiring["build"]["image_uri"])
    # Without a pointer for the image the plan refuses as the cycle does, before any
    # client is built.
    with pytest.raises(ValueError, match="^grant_pointer_missing$"):
        daily_plan(
            daily=fixture.wiring,
            image_uri=fixture.wiring["build"]["image_uri"].replace(
                IMAGE_DIGEST, "sha256:" + "6" * 64
            ),
        )


@pytest.mark.parametrize(
    "build",
    [
        {
            "source_sha": "a" * 40,
            "image_uri": (
                "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:"
                + "5" * 64
            ),
        },
        "not a mapping",
    ],
    ids=["other_image", "text"],
)
def test_native_handlers_refuse_a_build_whose_image_is_not_the_jobs_own(
    monkeypatch, build
):
    fixture = _native_fixture(monkeypatch)
    fixture.wiring["build"] = build
    reservation = FakeReservation()
    objects_before = dict(fixture.client.objects)
    with pytest.raises(ValueError, match=r"^build_image_mismatch$"):
        run_managed_mode(
            _build(fixture.config), authority=reservation, daily=fixture.wiring
        )
    assert fixture.terminal.calls == []
    assert fixture.client.objects == objects_before
    assert reservation.calls == []
    assert fixture.warehouse.calls == []


def test_daily_cycle_with_native_handlers_stops_at_capture_with_a_retry_safe_refusal(
    monkeypatch,
):
    """The active generation carries the v2 capture origin and the runner seam is bound,
    so the pre dispatch refusal left at capture is the one an attempt issued by another
    process meets: the durable authority record answers a receipt without the issued
    objects, and the handler refuses before the client, retry safe, never unknown."""
    fixture = _native_fixture(monkeypatch)
    reservation = FakeReservation()
    store_module = fixture.engine.daily_store
    record_stage = store_module.DailyStore.record_stage
    deaths = []

    class ProcessLost(Exception):
        pass

    def record_stage_or_die(self, operation_id, stage, result):
        # The process dies after the capture authority is issued and before the
        # pending record lands: the authority record is durable, the stage record is not.
        if stage == "capture" and result["state"] == "pending" and not deaths:
            deaths.append(operation_id)
            raise ProcessLost(operation_id)
        return record_stage(self, operation_id, stage, result)

    monkeypatch.setattr(store_module.DailyStore, "record_stage", record_stage_or_die)
    with pytest.raises(ProcessLost):
        run_managed_mode(
            _build(fixture.config), authority=reservation, daily=fixture.wiring
        )
    expected_operation = _expected_slot(fixture.engine)
    assert deaths == [expected_operation]
    reader = store_module.DailyStore(fixture.client, owner="reader", now=lambda: NOW)
    collect = reader.read_stage(expected_operation, "collect")
    assert collect["state"] == "succeeded"
    assert collect["result_reference"] == "source_run:collect-2026-09-19"
    assert reader.read_stage(expected_operation, "capture") is None
    assert reservation.consumed == 2
    attempt = fixture.engine.daily_authority.business_attempt_id(
        expected_operation, "capture"
    )
    prefix = fixture.engine.daily_authority.AUTHORITY_PREFIX
    assert f"{prefix}/{attempt}.json" in fixture.client.objects
    authority = fixture.terminal.calls[0]["collection_authority"]
    collect_attempt = authority["execution_id"]
    assert collect_attempt != EXECUTION
    assert authority["source_sha"] == "a" * 40
    assert authority["image_uri"] == fixture.wiring["build"]["image_uri"]
    assert f"{prefix}/{collect_attempt}.json" in fixture.client.objects
    assert (
        f"42/daily/collection/receipts/{collect_attempt}.json" in fixture.client.objects
    )
    assert (
        f"42/daily/collection/receipts/{EXECUTION}.json" not in fixture.client.objects
    )
    assert "42/daily/collection/days/2026-09-20.json" in fixture.client.objects
    assert "42/daily/source_runs/collect-2026-09-19.json" in fixture.client.objects
    # The next invocation runs in another process. The durable issued lease is a
    # reconciliation hold, so it cannot reissue capture or mutate the first attempt.
    monkeypatch.setattr(fixture.engine.daily_authority, "_ISSUED_AUTHORITIES", {})
    fixture.clock["now"] = NOW + timedelta(minutes=7)
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "intelligence-42-daily-staging-dup42")
    immutable_objects_before = {
        name: value
        for name, value in fixture.client.objects.items()
        if not name.endswith("/control.json")
    }
    paid_calls_before = list(reservation.paid_calls)
    terminal_calls_before = list(fixture.terminal.calls)
    with pytest.raises(
        ValueError, match=r"^authority_takeover_reconciliation_required$"
    ):
        run_managed_mode(
            _build(
                fixture.config,
                execution="intelligence-42-daily-staging-dup42",
                readback=_readback(
                    fixture.config, execution="intelligence-42-daily-staging-dup42"
                ),
            ),
            authority=reservation,
            daily=fixture.wiring,
        )
    assert reservation.consumed == 2
    assert reservation.paid_calls == paid_calls_before
    assert fixture.terminal.calls == terminal_calls_before
    assert fixture.warehouse.calls == []
    assert {
        name: value
        for name, value in fixture.client.objects.items()
        if not name.endswith("/control.json")
    } == immutable_objects_before
    assert reader.read_stage(expected_operation, "capture") is None
    assert reader.read_stage(expected_operation, "compose") is None
    assert reader.read_stage(expected_operation, "certify") is None


BUILD_REPOSITORY = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine"
BUILD_IMAGE_URI = BUILD_REPOSITORY + "@" + IMAGE_DIGEST


def _build_record(
    *,
    commit="e" * 40,
    digest=IMAGE_DIGEST,
    name=BUILD_REPOSITORY + ":" + "e" * 40,
    status="SUCCESS",
    build_id="b-1",
    source=None,
):
    return {
        "name": f"projects/590353929363/locations/us-central1/builds/{build_id}",
        "id": build_id,
        "projectId": "ogilvy-trends-v2",
        "status": status,
        "sourceProvenance": {"resolvedRepoSource": {"commitSha": commit}}
        if source is None
        else source,
        "results": {"images": [{"name": name, "digest": digest}]},
        "finishTime": "2026-09-19T10:00:00Z",
    }


def test_build_fact_binds_from_the_build_record_of_the_running_image(monkeypatch):
    fact = managed_runtime.build_fact_from_records(
        BUILD_IMAGE_URI, [_build_record()], repository=BUILD_REPOSITORY
    )
    assert fact == {"source_sha": "e" * 40, "image_uri": BUILD_IMAGE_URI}
    # A record of another digest, a failed build, a foreign repository, another project
    # or a malformed resource name binds nothing.
    for record in (
        _build_record(digest="sha256:" + "5" * 64),
        _build_record(status="FAILURE"),
        _build_record(name="us-central1-docker.pkg.dev/other/repo/engine:e"),
        {**_build_record(), "projectId": "other-project"},
        {**_build_record(), "name": "builds/b-1"},
        {**_build_record(), "results": {"images": []}},
        "not a record",
    ):
        assert (
            managed_runtime.build_fact_from_records(
                BUILD_IMAGE_URI, [record], repository=BUILD_REPOSITORY
            )
            is None
        )
    assert (
        managed_runtime.build_fact_from_records(
            BUILD_IMAGE_URI, [], repository=BUILD_REPOSITORY
        )
        is None
    )
    # The commit comes from the record's own provenance, never from the image tag.
    tagged = _build_record(name=BUILD_REPOSITORY + ":" + "f" * 40)
    assert managed_runtime.build_fact_from_records(
        BUILD_IMAGE_URI, [tagged], repository=BUILD_REPOSITORY
    ) == {"source_sha": "e" * 40, "image_uri": BUILD_IMAGE_URI}
    connected = _build_record(
        source={"resolvedConnectedRepository": {"revision": "d" * 40}}
    )
    assert (
        managed_runtime.build_fact_from_records(
            BUILD_IMAGE_URI, [connected], repository=BUILD_REPOSITORY
        )["source_sha"]
        == "d" * 40
    )
    with pytest.raises(ValueError, match=r"^build_provenance_invalid$"):
        managed_runtime.build_fact_from_records(
            BUILD_IMAGE_URI, [_build_record(source={})], repository=BUILD_REPOSITORY
        )
    with pytest.raises(ValueError, match=r"^build_provenance_invalid$"):
        managed_runtime.build_fact_from_records(
            BUILD_IMAGE_URI,
            [_build_record(source={"resolvedRepoSource": {"commitSha": "short"}})],
            repository=BUILD_REPOSITORY,
        )
    with pytest.raises(ValueError, match=r"^build_provenance_ambiguous$"):
        managed_runtime.build_fact_from_records(
            BUILD_IMAGE_URI,
            [_build_record(), _build_record(commit="f" * 40, build_id="b-2")],
            repository=BUILD_REPOSITORY,
        )
    twice = [_build_record(), _build_record(build_id="b-2")]
    assert managed_runtime.build_fact_from_records(
        BUILD_IMAGE_URI, twice, repository=BUILD_REPOSITORY
    ) == {"source_sha": "e" * 40, "image_uri": BUILD_IMAGE_URI}
    with pytest.raises(ValueError, match=r"^build_provenance_invalid$"):
        managed_runtime.build_fact_from_records(
            "no digest", [_build_record()], repository=BUILD_REPOSITORY
        )
    seen = []

    def reader(image_uri):
        seen.append(image_uri)
        return [_build_record()]

    assert managed_runtime.native_build_fact(
        BUILD_IMAGE_URI,
        repository=BUILD_REPOSITORY,
        resources=_manifest(),
        reader=reader,
    ) == {"source_sha": "e" * 40, "image_uri": BUILD_IMAGE_URI}
    assert seen == [BUILD_IMAGE_URI]

    def failing(image_uri):
        raise OSError("cloud build unreachable")

    with pytest.raises(ValueError, match=r"^build_provenance_unavailable$"):
        managed_runtime.native_build_fact(
            BUILD_IMAGE_URI,
            repository=BUILD_REPOSITORY,
            resources=_manifest(),
            reader=failing,
        )
    fixture = _native_fixture(monkeypatch)
    monkeypatch.setattr(
        managed_runtime, "_native_object_client", lambda engine: FakeObjectClient()
    )
    monkeypatch.setattr(
        managed_runtime, "_native_warehouse", lambda: FakeWarehouseClient()
    )
    assert "build" not in default_daily_wiring(fixture.config)
    wired = default_daily_wiring(
        fixture.config,
        image_uri=BUILD_IMAGE_URI,
        build_reader=reader,
        resources=_manifest(),
    )
    assert wired["build"] == {"source_sha": "e" * 40, "image_uri": BUILD_IMAGE_URI}
    unbound = default_daily_wiring(
        fixture.config,
        image_uri=BUILD_IMAGE_URI,
        build_reader=lambda image_uri: [],
        resources=_manifest(),
    )
    assert unbound["build"] is None
    # No build record for the running digest: the collect stage refuses before
    # dispatch, retry safe, and the producer is never called.
    fixture.wiring["build"] = None
    reservation = FakeReservation()
    receipt = run_managed_mode(
        _build(fixture.config), authority=reservation, daily=fixture.wiring
    )
    assert receipt["cycle"]["stage"] == "collect"
    assert receipt["cycle"]["state"] == "failed"
    reader_store = fixture.engine.daily_store.DailyStore(
        fixture.client, owner="reader", now=lambda: NOW
    )
    collect = reader_store.read_stage(_expected_slot(fixture.engine), "collect")
    assert collect["result_reference"] == "collect_refused:collection_build_unbound"
    assert collect["retry_safe"] is True
    assert fixture.terminal.calls == []


# The five source lanes one capture creates, and the estate and table naming the only
# capture producer that can execute writes into. A creation record naming anything else
# is not what a real capture of this cutoff would report.
CAPTURE_LANES = (
    "enriched_content",
    "event_ledger",
    "raw_content",
    "seed_candidates",
    "seed_graph",
)
CAPTURE_ESTATE = "ogilvy-trends-v2.trends_v2_staging"


def capture_creation_records(cutoff):
    """The creation records a succeeded capture of ``cutoff`` reports, one per lane.

    Each carries the identity of the native job that was read back: a job named for its
    own lane and the digest over that job's own resource. An empty list is what a capture
    that created nothing reports, which is not what this double stands for.
    """
    day = cutoff.strftime("%Y%m%d")
    return [
        {
            "lane": lane,
            "destination": f"{CAPTURE_ESTATE}.open_intelligence_v3_source_{day}_{lane}",
            "job_id": f"oi_v3_snapshot_{'a' * 64}_{lane}",
            "native_job_digest": "d" * 64,
            "state": "succeeded",
        }
        for lane in CAPTURE_LANES
    ]


class CaptureRunner:
    """The snapshot capture runner boundary double the daily fixture binds."""

    def __init__(self):
        self.calls = []

    def __call__(
        self, run, *, cutoff, snapshot_as_of, attempt_id, origin, client, authority
    ):
        self.calls.append(
            {
                "run": run,
                "cutoff": cutoff,
                "snapshot_as_of": snapshot_as_of,
                "attempt_id": attempt_id,
                "origin": origin,
                "client": client,
                "authority": authority,
            }
        )
        payload = {
            "contract_version": "source_snapshot_capture_v1",
            "cutoff_date": cutoff.isoformat(),
            "client_scope_id": "ogilvy_default",
            "market_scope": ["ke", "ng", "za"],
            "source_as_of": snapshot_as_of.isoformat(),
            "captured_at": (snapshot_as_of + timedelta(minutes=1)).isoformat(),
            "snapshot_plan_digest": "5" * 64,
            "snapshot_digest": "1" * 64,
            "capture_receipt_digest": "6" * 64,
            "creation_records": capture_creation_records(cutoff),
            "artifact_attempt": 1,
            "stored_artifact": {"generation": "7"},
            "query_count": 3,
            "total_bytes_billed": 1024,
            "limitations": [],
            "missing_checks": [],
        }
        verification = {
            "result_id": "res_" + "2" * 32,
            "image_digest": IMAGE_DIGEST.removeprefix("sha256:"),
            "generation": "7",
            "schema_digest": "2" * 64,
            "source_digest": "3" * 64,
        }
        return payload, "succeeded", verification


def test_daily_cycle_with_a_bound_capture_runner_stops_at_compose(monkeypatch):
    fixture = _native_fixture(monkeypatch)
    runner = CaptureRunner()
    native = fixture.engine.daily_native_clients
    monkeypatch.setattr(native, "DEFAULT_CAPTURE_RUNNER", runner)
    reservation = FakeReservation()
    receipt = run_managed_mode(
        _build(fixture.config), authority=reservation, daily=fixture.wiring
    )
    expected_operation = _expected_slot(fixture.engine)
    assert receipt["cycle"] == {
        "state": "failed",
        "operation_id": expected_operation,
        "stage": "compose",
    }
    assert receipt["stages_dispatched"] == ["collect", "capture", "compose"]
    assert reservation.consumed == 3
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["attempt_id"] == fixture.engine.daily_authority.business_attempt_id(
        expected_operation, "capture"
    )
    assert call["client"] is fixture.warehouse
    assert call["cutoff"].isoformat() == "2026-09-19"
    assert call["snapshot_as_of"] == NOW
    assert call["run"]["receipt"]["source_sha"] == "a" * 40
    # The runner receives the execution authority the daily authority issued for the
    # capture attempt: the reserved object and the consumption the reservation path
    # answered, beside the plain consumption the durable record keeps.
    issued = call["authority"]
    assert issued["stage"] == "capture"
    assert issued["mode"] == "new_consume"
    assert issued["operation"] == "immutable_capture"
    assert (
        issued["authority"]["run_manifest"]["business_attempt_id"] == call["attempt_id"]
    )
    assert issued["authority"]["run_manifest"]["stage"] == "capture"
    assert issued["consumption"] == {
        "consumption_id": "exc_" + call["attempt_id"][4:],
        "manifest_sha256": issued["authority"]["run_manifest"]["manifest_sha256"],
    }
    assert issued["reservation"] == issued["consumption"]
    assert (
        issued["authority_reference"] == f"42/daily/authority/{call['attempt_id']}.json"
    )
    # The origin is the v2 capture origin the rotated generation registers, resolved
    # from the active generation itself, not one the test substitutes.
    daily_stages = importlib.import_module(
        "src.analysis.open_intelligence.daily_stages"
    )
    assert call["origin"] == daily_stages._capture_origin(native.active_generation())
    assert call["origin"].manifest_version == "open_intelligence_execution_manifest_v2"
    assert "source_snapshot_capture" in call["origin"].operation_bindings
    assert "new_consume" in call["origin"].allowed_execution_modes
    assert fixture.warehouse.calls == []
    reader = fixture.engine.daily_store.DailyStore(
        fixture.client, owner="reader", now=lambda: NOW
    )
    capture = reader.read_stage(expected_operation, "capture")
    assert capture["state"] == "succeeded"
    assert capture["retry_safe"] is False
    key = capture["result_reference"].removeprefix("capture:")
    assert key.startswith("staging-2026-09-19-")
    assert f"42/daily/captures/registry/{key}.json" in fixture.client.objects
    assert (
        f"42/daily/captures/results/{call['attempt_id']}.json" in fixture.client.objects
    )
    registry = importlib.import_module(
        "src.analysis.open_intelligence.capture_registry"
    )
    entry = registry.validate_capture_entry(
        native.ObjectCaptureRegistry(fixture.client).read(key)
    )
    assert entry["operation_id"] == call["attempt_id"]
    assert entry["completion_state"] == "succeeded"
    assert entry["snapshot_as_of"] == NOW
    compose = reader.read_stage(expected_operation, "compose")
    assert compose["state"] == "failed"
    assert compose["retry_safe"] is True
    assert compose["output_digest"] == ""
    assert compose["result_reference"] == "compose_refused:client_unbound:composer"
    assert reader.read_stage(expected_operation, "certify") is None
    immutable_objects_before = {
        name: value
        for name, value in fixture.client.objects.items()
        if not name.endswith("/control.json")
    }
    paid_calls_before = list(reservation.paid_calls)
    terminal_calls_before = list(fixture.terminal.calls)
    fixture.clock["now"] = NOW + timedelta(minutes=7)
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "intelligence-42-daily-staging-dup42")
    with pytest.raises(
        ValueError, match=r"^authority_takeover_reconciliation_required$"
    ):
        run_managed_mode(
            _build(
                fixture.config,
                execution="intelligence-42-daily-staging-dup42",
                readback=_readback(
                    fixture.config, execution="intelligence-42-daily-staging-dup42"
                ),
            ),
            authority=reservation,
            daily=fixture.wiring,
        )
    assert len(runner.calls) == 1
    assert reservation.consumed == 3
    assert reservation.paid_calls == paid_calls_before
    assert fixture.terminal.calls == terminal_calls_before
    assert reader.read_stage(expected_operation, "capture") == capture
    assert reader.read_stage(expected_operation, "compose") == compose
    assert {
        name: value
        for name, value in fixture.client.objects.items()
        if not name.endswith("/control.json")
    } == immutable_objects_before
    assert fixture.warehouse.calls == []


def test_native_approval_reader_reads_grants_through_the_v3_routine(monkeypatch):
    calls = []

    def fake_reader(digest, *, version, mode):
        calls.append((digest, version, mode))
        return SimpleNamespace(
            approved_by="usr_" + "c" * 64,
            approved_at="2026-09-14T00:00:00+00:00",
            approval_phrase_sha256="a" * 64,
            manifest_sha256=digest,
        )

    fake = SimpleNamespace(
        _default_approval_reader=fake_reader,
        _APPROVAL_VERSION_V2="open_intelligence_execution_approval_v2",
        _APPROVAL_VERSION_V3="open_intelligence_execution_approval_v3",
    )
    monkeypatch.setitem(
        sys.modules, "src.analysis.open_intelligence.execution_approval", fake
    )
    read = managed_runtime._native_approval_reader()
    assert read("b" * 64)["manifest_sha256"] == "b" * 64
    assert calls == [
        ("b" * 64, "open_intelligence_execution_approval_v3", "new_consume")
    ]


BUILDS_COLLECTION = (
    "//cloudbuild.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/builds"
)


def _manifest_without_the_builds_row():
    manifest = _manifest()
    rows = [row for row in manifest["resources"] if row["name"] != BUILDS_COLLECTION]
    assert len(rows) == len(manifest["resources"]) - 1
    manifest["resources"] = rows
    return manifest


def test_the_build_provenance_read_is_guarded_against_the_builds_collection():
    """The runtime lists Cloud Build records with its own credential, so the read is
    asked of the reviewed guard first: the builds collection row, read only."""
    assert managed_runtime.BUILDS_RESOURCE == BUILDS_COLLECTION
    rows = {row["name"]: row["actions"] for row in _manifest()["resources"]}
    assert rows[BUILDS_COLLECTION] == ["read"]
    seen = []

    def reader(image_uri):
        seen.append(image_uri)
        return [_build_record()]

    assert managed_runtime.native_build_fact(
        BUILD_IMAGE_URI,
        repository=BUILD_REPOSITORY,
        resources=_manifest(),
        reader=reader,
    ) == {"source_sha": "e" * 40, "image_uri": BUILD_IMAGE_URI}
    assert seen == [BUILD_IMAGE_URI]


def test_the_build_provenance_read_refuses_when_the_builds_row_is_absent(monkeypatch):
    """Without the builds row the read refuses resource_action_forbidden before any
    Cloud Build request, through an injected reader and the native one alike."""
    seen = []

    def reader(image_uri):
        seen.append(image_uri)
        return [_build_record()]

    with pytest.raises(ValueError, match=r"^resource_action_forbidden$"):
        managed_runtime.native_build_fact(
            BUILD_IMAGE_URI,
            repository=BUILD_REPOSITORY,
            resources=_manifest_without_the_builds_row(),
            reader=reader,
        )
    monkeypatch.setattr(managed_runtime, "_native_build_provenance_reader", reader)
    with pytest.raises(ValueError, match=r"^resource_action_forbidden$"):
        managed_runtime.native_build_fact(
            BUILD_IMAGE_URI,
            repository=BUILD_REPOSITORY,
            resources=_manifest_without_the_builds_row(),
        )
    assert seen == []


def test_the_daily_wiring_refuses_the_build_read_without_the_builds_row(monkeypatch):
    fixture = _native_fixture(monkeypatch)
    monkeypatch.setattr(
        managed_runtime, "_native_object_client", lambda engine: FakeObjectClient()
    )
    monkeypatch.setattr(
        managed_runtime, "_native_warehouse", lambda: FakeWarehouseClient()
    )
    seen = []
    with pytest.raises(ValueError, match=r"^resource_action_forbidden$"):
        default_daily_wiring(
            fixture.config,
            image_uri=BUILD_IMAGE_URI,
            build_reader=lambda image_uri: seen.append(image_uri) or [],
            resources=_manifest_without_the_builds_row(),
        )
    assert seen == []
