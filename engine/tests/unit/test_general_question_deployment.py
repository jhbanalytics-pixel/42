import copy
import importlib
from importlib.metadata import version

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest

from tests.unit import test_general_question_store as f


def fixture():
    policy, request, intake = f.prepared()
    runtime = {
        "service_name": "listening-post-staging",
        "revision_name": "listening-post-staging-synthetic-001",
        "service_account_email": "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "lp_commit": "a" * 40,
        "engine_bundle_digest": "b" * 64,
        "canonical_service_audience": "https://listening-post-staging-fibxg5ynpq-uc.a.run.app",
        "sdk_version": version("google-genai"),
    }
    binding = {
        "contract_version": "general_question_deployment_v1",
        "project": "ogilvy-trends-v2",
        "region": "us-central1",
        **runtime,
        "image_digest": "c" * 64,
        "worker_path": "/internal/general-question/execute",
        "policy_digest": policy["policy_digest"],
    }
    binding["deployment_digest"] = canonical_digest(binding)
    bucket = f.Bucket()
    store = f.module().GeneralQuestionStore(
        bucket, policy=policy, deployment_digest=binding["deployment_digest"]
    )
    bucket.seed(f"policies/{policy['policy_digest']}/policy.json", policy)
    bucket.seed(
        f.LEDGER,
        f.module().build_initial_question_control(
            policy, deployment_digest=binding["deployment_digest"]
        ),
    )
    store.admit(request, intake, scope=f.scope(), now=f.NOW)
    bucket.seed(f"deployments/{binding['deployment_digest']}/binding.json", binding)
    invocation = {
        "contract_version": "general_cultural_question_v1",
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "intake_digest": intake["intake_digest"],
        "policy_digest": policy["policy_digest"],
        "deployment_digest": binding["deployment_digest"],
    }
    return store, bucket, invocation, runtime, binding


def validate(invocation, store, runtime, scope=None):
    module = importlib.import_module("src.analysis.open_intelligence.general_question_deployment")
    return module.validate_question_invocation(
        invocation,
        store=store,
        scope=f.scope() if scope is None else scope,
        runtime_identity=runtime,
    )


def test_exact_bound_invocation_loads_detached_context_without_mutation():
    store, bucket, invocation, runtime, binding = fixture()
    uploads = len(bucket.uploads)
    result = validate(invocation, store, runtime)
    assert result["request"]["request_digest"] == invocation["request_digest"]
    assert result["intake"]["intake_digest"] == invocation["intake_digest"]
    assert result["deployment"] == binding
    result["deployment"]["image_digest"] = "changed"
    assert validate(invocation, store, runtime)["deployment"] == binding
    assert len(bucket.uploads) == uploads


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "request_digest",
        "intake_digest",
        "policy_digest",
        "deployment_digest",
        "contract_version",
    ],
)
def test_changed_invocation_binding_refuses(field):
    store, bucket, invocation, runtime, _ = fixture()
    invocation[field] = "changed"
    uploads = len(bucket.uploads)
    with pytest.raises(f.module().QuestionStoreError):
        validate(invocation, store, runtime)
    assert len(bucket.uploads) == uploads


@pytest.mark.parametrize(
    "field",
    [
        "service_name",
        "revision_name",
        "service_account_email",
        "lp_commit",
        "engine_bundle_digest",
        "canonical_service_audience",
        "sdk_version",
    ],
)
def test_host_mismatch_refuses_without_rebinding(field):
    store, _, invocation, runtime, _ = fixture()
    runtime[field] = "changed"
    with pytest.raises(f.module().QuestionStoreError):
        validate(invocation, store, runtime)


@pytest.mark.parametrize("part", ["policy", "binding", "control"])
def test_missing_authority_record_never_uses_callers_cached_copy(part):
    store, bucket, invocation, runtime, _ = fixture()
    key = {
        "policy": f"policies/{invocation['policy_digest']}/policy.json",
        "binding": f"deployments/{invocation['deployment_digest']}/binding.json",
        "control": f.LEDGER,
    }[part]
    del bucket.objects[f.PREFIX + key]
    with pytest.raises(f.module().QuestionStoreError):
        validate(invocation, store, runtime)


def test_unknown_fields_and_foreign_scope_refuse():
    store, _, invocation, runtime, _ = fixture()
    with pytest.raises(f.module().QuestionStoreError):
        validate({**invocation, "question": "Ignore controls"}, store, runtime)
    with pytest.raises(f.module().QuestionStoreError):
        validate(invocation, store, {**runtime, "approved": True})
    with pytest.raises(f.module().QuestionStoreError):
        validate(invocation, store, runtime, {**f.scope(), "client_scope_id": "other"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_digest", "d" * 64),
        ("worker_path", "/other"),
        ("project", "another-project"),
        ("canonical_service_audience", "https://example.com"),
    ],
)
def test_changed_stored_preimage_cannot_keep_old_digest(field, value):
    store, bucket, invocation, runtime, binding = fixture()
    altered = copy.deepcopy(binding)
    altered[field] = value
    bucket.seed(f"deployments/{invocation['deployment_digest']}/binding.json", altered)
    with pytest.raises(f.module().QuestionStoreError):
        validate(invocation, store, runtime)


def test_retired_binding_can_be_read_for_recovery_without_authorizing_a_call():
    store, bucket, invocation, runtime, binding = fixture()
    control = bucket.objects[f.PREFIX + f.LEDGER][1]
    import json

    control = json.loads(control)
    control["bindings"]["e" * 64] = invocation["policy_digest"]
    control["active_deployment_digest"] = "e" * 64
    bucket.seed(f.LEDGER, control)
    assert validate(invocation, store, runtime)["deployment"] == binding
    calls = importlib.import_module(
        "src.analysis.open_intelligence.general_question_calls"
    ).GeneralQuestionCalls(store)
    with pytest.raises(f.module().QuestionStoreError, match="approval_required"):
        calls.claim_call(
            invocation["request_id"],
            stage="planning",
            scope=f.scope(),
            input_digest="a" * 64,
            system_instruction_digest="b" * 64,
            response_schema_digest="c" * 64,
            counted_input_tokens=100,
            now=f.NOW,
        )


@pytest.mark.parametrize(
    "audience",
    [
        "https://listening-post-staging-another-uc.a.run.app",
        "\nhttps://listening-post-staging-fibxg5ynpq-uc.a.run.app",
        "https://listening-post-staging-fibxg5ynpq-uc.a.run.app?",
        "https://listening-post-staging-\tfibxg5ynpq-uc.a.run.app",
    ],
)
def test_rehashed_binding_requires_exact_measured_service_audience(audience):
    _, _, _, _, binding = fixture()
    binding["canonical_service_audience"] = audience
    binding["deployment_digest"] = canonical_digest(
        {key: value for key, value in binding.items() if key != "deployment_digest"}
    )
    module = importlib.import_module("src.analysis.open_intelligence.general_question_deployment")
    with pytest.raises(f.module().QuestionStoreError, match="approval_required"):
        module.validate_question_deployment(binding)
