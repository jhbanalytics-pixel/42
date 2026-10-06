import copy
import hashlib
import importlib
import json
from datetime import datetime

import pytest

from ops.deploy import release
from ops.deploy import refresh_question_policy
from ops.tests.test_foundation_release import (
    LEDGER_NAME,
    PRICING_SOURCE,
    fake,
    make_revision,
    observation,
    object_entry,
    renewal_grant,
    write_json,
)
from ops.tests.test_refresh_question_policy_cli import Lane, recording_clients


OLD_POLICY = {
    "api_version": "v1",
    "approval_contract_digest": "2885012a99bfc352c9ad92ced7b5bddd0b022f88e8ddc9d7688f237179062a8b",
    "contract_version": "general_question_policy_v1",
    "expires_at": "2026-12-31T23:59:59Z",
    "limits": {
        "billed_bytes": 1000000000,
        "calls_per_request": 2,
        "candidate_records": 1000,
        "deadline_seconds": 240,
        "evidence_records": 200,
        "input_tokens": 32000,
        "model_timeout_seconds": 60,
        "output_tokens": 4000,
        "query_jobs": 8,
        "request_microusd": 100000,
        "requests": 100,
        "reserved_microusd": 10000000,
    },
    "location": "global",
    "model": "gemini-3.8-flash",
    "policy_digest": "e9f597b5a5f6c30393667016617dae67b4aa25119b10eb4b382e3365d67e23bf",
    "policy_id": "general_cultural_question_staging_eval_v1",
    "pricing": {
        "input_usd_per_million": "0.750000",
        "output_usd_per_million": "3.750000",
        "source_url": "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing",
        "verified_at": "2026-09-27T03:02:33.272982Z",
    },
    "project": "ogilvy-trends-v2",
    "service_tier": "standard",
    "stages": {
        "answering": {
            "input_tokens": 32000,
            "output_tokens": 4000,
            "thinking_level": "LOW",
        },
        "planning": {
            "input_tokens": 8000,
            "output_tokens": 800,
            "thinking_level": "LOW",
        },
    },
}
CANDIDATE_POLICY = {
    **OLD_POLICY,
    "policy_digest": "3d0caed4175761b8864a60ef0f9435256f5e9d74d919f0272c01ed699a8783c0",
    "pricing": {
        **OLD_POLICY["pricing"],
        "verified_at": "2026-09-28T06:46:46.721138Z",
    },
}
OLD_BINDING = {
    "canonical_service_audience": "https://listening-post-staging-fibxg5ynpq-uc.a.run.app",
    "contract_version": "general_question_deployment_v1",
    "deployment_digest": "31d1ccc956937401c72e9daca4b7a101f4fca21cfdbc0eea8e15af282d8806cb",
    "engine_bundle_digest": "e7d3783b2b5a3d3af9304c17923fc004346cb19ab4e9d5f2f264da444a971271",
    "image_digest": "1249a1f8898b6374626a1159be1349123007219dfe9603a57628979fbd319db4",
    "lp_commit": "2f9d43afc6f52e0026f25dbba0c1639d1e84602a",
    "policy_digest": "e9f597b5a5f6c30393667016617dae67b4aa25119b10eb4b382e3365d67e23bf",
    "project": "ogilvy-trends-v2",
    "region": "us-central1",
    "revision_name": "listening-post-staging-p-e9f597b5a5",
    "sdk_version": "2.20.0",
    "service_account_email": "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    "service_name": "listening-post-staging",
    "worker_path": "/internal/general-question/execute",
}
CANDIDATE_BINDING = {
    **OLD_BINDING,
    "deployment_digest": "60b912f41bd71dbde5c9ac64c68332662bc4d5a3f07e70c93c9aa116803e6877",
    "policy_digest": CANDIDATE_POLICY["policy_digest"],
    "revision_name": "listening-post-staging-p-3d0caed417",
}
OPERATION_NAME = (
    "projects/ogilvy-trends-v2/locations/us-central1/operations/"
    "745ad970-693f-490c-bd21-f8b60f3884bb"
)
LEDGER_GENERATION = "1790478288140651"
TAG = "question-2f9d43a"
SOURCE_BYTES = b"<html>Input tokens $0.75. Output tokens $3.75.</html>"
SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()
SOURCE_NAME = release.OBJECT_PREFIX + f"renewal/sources/{SOURCE_SHA256}.html"


class RawOperationReader:
    def __init__(self, operation):
        self.operation = operation
        self.reads = []

    def perform(self, call, **kwargs):
        self.reads.append((call, kwargs))
        assert call == "read_operation"
        assert kwargs["name"] == self.operation["name"]
        return 200, copy.deepcopy(self.operation)

    def require_ok(self, call, status, value):
        assert status == 200
        return value


class FinishCase:
    def __init__(self, tmp_path):
        self.lane = Lane(tmp_path)
        state = self.lane.state()
        requests = {
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": {
                "deadline_at": "2026-09-28T07:00:00Z",
                "deployment_digest": OLD_BINDING["deployment_digest"],
                "policy_digest": OLD_POLICY["policy_digest"],
                "reserved_microusd": 100000,
            },
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": {
                "deadline_at": "2026-09-28T07:01:00Z",
                "deployment_digest": OLD_BINDING["deployment_digest"],
                "policy_digest": OLD_POLICY["policy_digest"],
                "reserved_microusd": 100000,
            },
        }
        self.ledger = {
            "contract_version": "general_question_allowance_v1",
            "allowance_id": "general_cultural_question_staging_eval_v1",
            "active_deployment_digest": OLD_BINDING["deployment_digest"],
            "bindings": {
                OLD_BINDING["deployment_digest"]: OLD_POLICY["policy_digest"]
            },
            "requests": requests,
            "reserved_microusd": 200000,
        }
        objects = state["objects"]
        objects[LEDGER_NAME] = object_entry(
            release.canonical_bytes(self.ledger), int(LEDGER_GENERATION)
        )
        objects[
            release.OBJECT_PREFIX
            + f"policies/{OLD_POLICY['policy_digest']}/policy.json"
        ] = object_entry(release.canonical_bytes(OLD_POLICY), 1790478288140601)
        objects[
            release.OBJECT_PREFIX
            + f"deployments/{OLD_BINDING['deployment_digest']}/binding.json"
        ] = object_entry(release.canonical_bytes(OLD_BINDING), 1790478288140602)
        objects[
            release.OBJECT_PREFIX
            + f"policies/{CANDIDATE_POLICY['policy_digest']}/policy.json"
        ] = object_entry(release.canonical_bytes(CANDIDATE_POLICY), 1790578009994863)
        objects[
            release.OBJECT_PREFIX
            + f"deployments/{CANDIDATE_BINDING['deployment_digest']}/binding.json"
        ] = object_entry(release.canonical_bytes(CANDIDATE_BINDING), 1790578010228413)
        objects[SOURCE_NAME] = object_entry(SOURCE_BYTES, 1790578009091094)
        self.before_service = copy.deepcopy(state["service"])
        self.before_service["template"] = release.expected_template(OLD_BINDING, TAG)
        self.before_service["traffic"] = release.traffic_targets(OLD_BINDING, TAG)
        self.before_service["trafficStatuses"] = copy.deepcopy(
            self.before_service["traffic"]
        )
        self.before_service.update(
            etag='"CPmi5dUGEMiaw-wB/cHJvamVjdHMvb2dpbHZ5LXRyZW5kcy12Mi9sb2NhdGlvbnMvdXMtY2VudHJhbDEvam9icy9pbnRlbGxpZ2VuY2UtNDItcHJpY2UtcG9saWN5LXN0YWdpbmci',
            generation="262",
            observedGeneration="262",
            latestCreatedRevision=release.SERVICE_API_NAME
            + "/revisions/"
            + OLD_BINDING["revision_name"],
            latestReadyRevision=release.SERVICE_API_NAME
            + "/revisions/"
            + OLD_BINDING["revision_name"],
            reconciling=False,
        )
        self.service = copy.deepcopy(self.before_service)
        self.service["template"] = release.expected_template(CANDIDATE_BINDING, TAG)
        self.service["traffic"] = release.traffic_targets(OLD_BINDING, TAG)
        self.service["trafficStatuses"] = copy.deepcopy(self.service["traffic"])
        self.service.update(
            etag='"CNqa6NUGEODNsM0C/cHJvamVjdHMvb2dpbHZ5LXRyZW5kcy12Mi9zZXJ2aWNlcy9saXN0ZW5pbmctcG9zdC1zdGFnaW5nL3JldmlzaW9ucy9saXN0ZW5pbmctcG9zdC1zdGFnaW5nLXAtM2QwY2FlZDQxNyI',
            generation="263",
            observedGeneration="263",
            latestCreatedRevision=release.SERVICE_API_NAME
            + "/revisions/"
            + CANDIDATE_BINDING["revision_name"],
            latestReadyRevision=release.SERVICE_API_NAME
            + "/revisions/"
            + OLD_BINDING["revision_name"],
            reconciling=False,
        )
        state["service"] = self.service
        state["revisions"][
            release.SERVICE_API_NAME + "/revisions/" + OLD_BINDING["revision_name"]
        ] = make_revision(OLD_BINDING, TAG)
        candidate_revision = make_revision(CANDIDATE_BINDING, TAG)
        candidate_revision["conditions"].append(
            {
                "type": "Active",
                "state": "CONDITION_FAILED",
                "revisionReason": "RETIRED",
            }
        )
        state["revisions"][
            release.SERVICE_API_NAME
            + "/revisions/"
            + CANDIDATE_BINDING["revision_name"]
        ] = candidate_revision
        state["clock"]["now"] = "2026-09-28T06:49:18.634863+00:00"
        state["next_generation"] = 1790579000000000
        write_json(self.lane.state_path, state)
        self.clients = recording_clients(self.lane.state_path)
        self.clients["manifest"] = {
            "value": self.lane.renewal.manifest,
            "sha256": "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09",
        }
        self.operation = {
            "name": OPERATION_NAME,
            "done": True,
            "metadata": {"@type": "type.googleapis.com/google.cloud.run.v2.Service"},
            "response": {
                "@type": "type.googleapis.com/google.cloud.run.v2.Service",
                **copy.deepcopy(self.service),
            },
        }
        self.raw_operation_reader = RawOperationReader(self.operation)
        self.clients["run"].adapter = self.raw_operation_reader
        self.receipt = {
            "command": "renew-unattended",
            "contract_version": "42_renewal_receipt_v1",
            "state": "failed",
            "error": (
                "NativeError: read_operation: http 403: Permission denied on "
                f"resource '{OPERATION_NAME}'"
            ),
            "deployed_revision": None,
            "ledger_moved": False,
            "rate_changed": False,
            "observation_source_sha256": SOURCE_SHA256,
            "operations": {},
            "tag": TAG,
            "binding": copy.deepcopy(CANDIDATE_BINDING),
            "policy": copy.deepcopy(CANDIDATE_POLICY),
            "observation": {
                "contract_version": "42_pricing_observation_v2",
                "observed_at": "2026-09-28T06:46:46.721138Z",
                "model": "gemini-3.8-flash",
                "input_usd_per_million": "0.750000",
                "output_usd_per_million": "3.750000",
                "source_sha256": SOURCE_SHA256,
                "source_object": f"object:gs://{release.BUCKET}/{SOURCE_NAME}#1790578009091094",
            },
            "mutations": [
                {
                    "action": "create_immutable_artifact",
                    "object": SOURCE_NAME,
                    "generation": "1790578009091094",
                    "sha256": SOURCE_SHA256,
                },
                {
                    "action": "create_immutable_artifact",
                    "object": "open-intelligence/v2/staging/general-questions/policies/3d0caed4175761b8864a60ef0f9435256f5e9d74d919f0272c01ed699a8783c0/policy.json",
                    "generation": "1790578009994863",
                    "sha256": "1d64a99c57575fa3260f0ceb714c6d5a45be5c8f8c571a68c8b4a27223d53305",
                },
                {
                    "action": "create_immutable_artifact",
                    "object": "open-intelligence/v2/staging/general-questions/deployments/60b912f41bd71dbde5c9ac64c68332662bc4d5a3f07e70c93c9aa116803e6877/binding.json",
                    "generation": "1790578010228413",
                    "sha256": "1247d80c954be762cd8eaf0704cc3c5e6e02eaf14a3e7b28b168257fd78277af",
                },
            ],
        }
        self.before_state = {
            "at": "2026-09-28T06:42:27.511684Z",
            "ledger_generation": LEDGER_GENERATION,
            "ledger": copy.deepcopy(self.ledger),
            "binding": copy.deepcopy(OLD_BINDING),
            "policy": copy.deepcopy(OLD_POLICY),
            "service": copy.deepcopy(self.before_service),
        }
        self.journal_start = len(self.clients["_state"].data["journal"])

    def finish(self, *, execute=True, **overrides):
        module = importlib.import_module("ops.deploy.finish_pricing_activation")
        values = {
            "receipt": copy.deepcopy(self.receipt),
            "before_state": copy.deepcopy(self.before_state),
            "operation_name": OPERATION_NAME,
            "clients": self.clients,
            "execute": execute,
        }
        values.update(overrides)
        return module.finish_pricing_activation(**values)

    def mutations(self):
        return [
            row
            for row in self.clients["_state"].data["journal"][self.journal_start :]
            if row["call"] in ("create_object", "deploy_revision", "update_traffic")
        ]


def _damage_case(case, mutation):
    state = case.clients["_state"].data
    candidate_revision_name = (
        release.SERVICE_API_NAME + "/revisions/" + CANDIDATE_BINDING["revision_name"]
    )
    policy_name = (
        release.OBJECT_PREFIX
        + f"policies/{CANDIDATE_POLICY['policy_digest']}/policy.json"
    )
    binding_name = (
        release.OBJECT_PREFIX
        + f"deployments/{CANDIDATE_BINDING['deployment_digest']}/binding.json"
    )
    if mutation == "ledger_generation":
        state["objects"][LEDGER_NAME]["generation"] = str(
            int(LEDGER_GENERATION) + 1
        )
    elif mutation == "operation_pending":
        case.operation["done"] = False
    elif mutation == "operation_failed":
        case.operation["error"] = {"code": 10, "message": "operation failed"}
    elif mutation == "wrong_project":
        case.operation["name"] = case.operation["name"].replace(
            "ogilvy-trends-v2", "another-project"
        )
    elif mutation == "wrong_service":
        case.operation["response"]["name"] = (
            "projects/another-project/locations/us-central1/services/other"
        )
    elif mutation == "revision_unready":
        state["revisions"][candidate_revision_name]["conditions"][0][
            "state"
        ] = "CONDITION_FAILED"
    elif mutation == "revision_mismatch":
        state["service"]["latestCreatedRevision"] = (
            release.SERVICE_API_NAME + "/revisions/other"
        )
    elif mutation == "policy_forged":
        state["objects"][policy_name]["raw_b64"] = fake.encode_raw(b"forged")
    elif mutation == "policy_missing":
        del state["objects"][policy_name]
    elif mutation == "binding_forged":
        state["objects"][binding_name]["raw_b64"] = fake.encode_raw(b"forged")
    elif mutation == "binding_missing":
        del state["objects"][binding_name]
    elif mutation == "source_missing":
        del state["objects"][SOURCE_NAME]
    elif mutation == "source_hash_drift":
        state["objects"][SOURCE_NAME]["raw_b64"] = fake.encode_raw(b"forged")
    elif mutation == "source_generation_drift":
        state["objects"][SOURCE_NAME]["generation"] = "1790578009091095"
    elif mutation == "receipt_forged":
        case.receipt["policy"]["policy_digest"] = "0" * 64
    elif mutation == "receipt_missing":
        case.receipt = {}
    elif mutation == "image_changed":
        wrong = "us-central1-docker.pkg.dev/other/image@sha256:" + "0" * 64
        state["service"]["template"]["containers"][0]["image"] = wrong
        case.operation["response"]["template"]["containers"][0]["image"] = wrong
    elif mutation == "env_changed":
        for service in (state["service"], case.operation["response"]):
            env = service["template"]["containers"][0]["env"]
            next(item for item in env if item["name"] == "SOURCE_SHA")["value"] = (
                "0" * 40
            )
    elif mutation == "bridge_missing":
        for service in (state["service"], case.operation["response"]):
            env = service["template"]["containers"][0]["env"]
            next(
                item for item in env if item["name"] == "GENERAL_QUESTION_BRIDGE_READS"
            )["value"] = "disabled"
    elif mutation == "candidate_stale":
        state["clock"]["now"] = "2026-09-29T07:00:00+00:00"
    elif mutation == "service_etag_drift":
        state["service"]["etag"] = "changed-after-operation"
    elif mutation == "extra_runtime_env":
        entry = {"name": "UNEXPECTED_RUNTIME_FLAG", "value": "enabled"}
        for template in (
            state["service"]["template"],
            case.operation["response"]["template"],
            state["revisions"][candidate_revision_name],
        ):
            template["containers"][0]["env"].append(copy.deepcopy(entry))
    elif mutation == "before_not_utc":
        case.before_state["at"] = "2026-09-28T09:42:27.511684+03:00"
    elif mutation == "before_future":
        case.before_state["at"] = "2026-09-29T06:42:27.511684Z"
    elif mutation == "before_day_mismatch":
        case.before_state["at"] = "2026-09-27T06:42:27.511684Z"
    case.raw_operation_reader.operation = case.operation
    case.clients["_state"].save()


def test_finish_activates_saved_ready_revision_with_cas_then_traffic(tmp_path):
    case = FinishCase(tmp_path)

    result = case.finish(execute=True)

    assert result["state"] == "activated", result
    assert [row["call"] for row in case.mutations()] == [
        "create_object",
        "update_traffic",
    ]
    assert case.raw_operation_reader.reads[0][1]["name"] == OPERATION_NAME
    ledger = release._read_ledger(case.clients)
    assert ledger["value"]["active_deployment_digest"] == CANDIDATE_BINDING[
        "deployment_digest"
    ]
    assert ledger["value"]["requests"] == case.ledger["requests"]
    assert ledger["value"]["reserved_microusd"] == case.ledger["reserved_microusd"]
    policy_name = (
        release.OBJECT_PREFIX
        + f"policies/{CANDIDATE_POLICY['policy_digest']}/policy.json"
    )
    stored_policy = case.clients["objects"].read(policy_name, generation="1790578009994863")
    assert stored_policy["raw"] == release.canonical_bytes(CANDIDATE_POLICY)
    service = case.clients["run"].get_service(release.SERVICE_API_NAME)
    assert service["traffic"] == release.traffic_targets(CANDIDATE_BINDING, TAG)
    expected_template = copy.deepcopy(case.before_service["template"])
    expected_template["revision"] = CANDIDATE_BINDING["revision_name"]
    deployment_entry = next(
        item
        for item in expected_template["containers"][0]["env"]
        if item["name"] == "GENERAL_QUESTION_DEPLOYMENT_DIGEST"
    )
    deployment_entry["value"] = CANDIDATE_BINDING["deployment_digest"]
    assert service["template"] == expected_template


def test_saved_ready_candidate_refuses_existing_renewal_before_mutation(tmp_path):
    case = FinishCase(tmp_path)
    page = b"Input tokens cost $0.75 per million. Output tokens cost $3.75 per million."
    case.clients["_state"].data["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(
        page
    )
    case.clients["_state"].save()
    index = copy.deepcopy(case.lane.renewal.index)
    index.update(
        commit=OLD_BINDING["lp_commit"],
        app_image=release.image_reference(OLD_BINDING),
        policy_digest=OLD_POLICY["policy_digest"],
        deployment_digest=OLD_BINDING["deployment_digest"],
    )
    for item in index["raw_authority_objects"]:
        if item["name"] == "deployment_binding":
            item["uri"] = (
                "gs://"
                + release.BUCKET
                + "/"
                + release.OBJECT_PREFIX
                + f"deployments/{OLD_BINDING['deployment_digest']}/binding.json"
            )
    for reference, item in index["evidence_objects"].items():
        item["uri"] = (
            "gs://"
            + release.BUCKET
            + "/"
            + release.evidence_object_name(index["commit"], reference)
        )
    before = len(case.mutations())
    receipt = refresh_question_policy.refresh_observed_policy(
        release_index=index,
        observation=observation(
            observed_at=datetime.fromisoformat(
                "2026-09-28T06:46:46.721138+00:00"
            ),
            input_rate="0.750000",
            output_rate="3.750000",
            page=page,
        ),
        grant=renewal_grant(),
        clients=case.clients,
    )

    assert (receipt["state"], receipt["error"]) == ("refused", "service_mismatch"), receipt
    assert receipt["mutations"] == []
    assert receipt["operations"] == {}
    assert len(case.mutations()) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "ledger_generation",
        "operation_pending",
        "operation_failed",
        "wrong_project",
        "wrong_service",
        "revision_unready",
        "revision_mismatch",
        "policy_forged",
        "policy_missing",
        "binding_forged",
        "binding_missing",
        "source_missing",
        "source_hash_drift",
        "source_generation_drift",
        "receipt_forged",
        "receipt_missing",
        "image_changed",
        "env_changed",
        "bridge_missing",
        "candidate_stale",
        "service_etag_drift",
        "extra_runtime_env",
        "before_not_utc",
        "before_future",
        "before_day_mismatch",
    ],
)
def test_invalid_candidate_refuses_before_first_write(tmp_path, mutation):
    case = FinishCase(tmp_path)
    _damage_case(case, mutation)

    result = case.finish(execute=True)

    assert result["state"] == "refused", (mutation, result)
    assert case.mutations() == [], (mutation, case.mutations())


def test_dry_run_plans_cas_then_traffic_without_mutation(tmp_path):
    case = FinishCase(tmp_path)
    ledger_before = release._read_ledger(case.clients)
    service_before = copy.deepcopy(case.clients["run"].get_service(release.SERVICE_API_NAME))
    object_names_before = set(case.clients["_state"].data["objects"])

    result = case.finish(execute=False)

    assert result["state"] == "dry_run", result
    assert [request["call"] for request in result["requests"]] == [
        "create_object",
        "update_traffic",
    ]
    assert case.mutations() == []
    assert release._read_ledger(case.clients) == ledger_before
    assert case.clients["run"].get_service(release.SERVICE_API_NAME) == service_before
    assert set(case.clients["_state"].data["objects"]) == object_names_before


def test_operator_cli_defaults_to_attended_dry_run(tmp_path, capsys, monkeypatch):
    case = FinishCase(tmp_path)
    module = importlib.import_module("ops.deploy.finish_pricing_activation")
    receipt_path = write_json(tmp_path / "failed-receipt.json", case.receipt)
    before_path = write_json(tmp_path / "before-state.json", case.before_state)
    output_path = tmp_path / "finish-receipt.json"
    adapter_states = []

    def local_clients(args, clients):
        adapter_states.append(args.adapter_state)
        return case.clients

    monkeypatch.setattr(refresh_question_policy, "_clients_for", local_clients)

    code = module.main(
        [
            "--receipt",
            str(receipt_path),
            "--before-state",
            str(before_path),
            "--operation",
            OPERATION_NAME,
            "--adapter",
            "release_native_adapter.py:factory",
            "--output",
            str(output_path),
        ]
    )

    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert code == 0
    assert result["state"] == "dry_run"
    assert adapter_states == ["owner-gcloud"]
    assert case.mutations() == []


def test_exact_twenty_utc_cutoff_refuses_before_ledger_cas(tmp_path):
    case = FinishCase(tmp_path)
    case.clients["_state"].data["clock"]["now"] = "2026-09-28T20:00:00+00:00"
    case.clients["_state"].save()

    result = case.finish(execute=True)

    assert result["state"] == "refused", result
    assert case.mutations() == []


def test_cutoff_after_cas_reports_partial_state_without_traffic(tmp_path):
    case = FinishCase(tmp_path)
    objects = case.clients["objects"]
    create = objects.create

    def create_then_cross_cutoff(name, raw, *, if_generation_match):
        result = create(name, raw, if_generation_match=if_generation_match)
        case.clients["_state"].data["clock"]["now"] = "2026-09-28T20:00:00+00:00"
        case.clients["_state"].save()
        return result

    objects.create = create_then_cross_cutoff

    result = case.finish(execute=True)

    assert result["state"] == "partial", result
    assert result["ledger_moved"] is True
    assert [item["action"] for item in result["mutations"]] == [
        "compare_and_swap_ledger"
    ]
    assert [row["call"] for row in case.mutations()] == ["create_object"]


def test_traffic_failure_after_cas_reports_partial_state_without_retry(tmp_path):
    case = FinishCase(tmp_path)
    case.clients["_state"].data["failures"]["update_traffic"] = "failed"
    case.clients["_state"].save()

    result = case.finish(execute=True)

    assert result["state"] == "partial", result
    assert result["ledger_moved"] is True
    assert [row["call"] for row in case.mutations()] == [
        "create_object",
        "update_traffic",
    ]
