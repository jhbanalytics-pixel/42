import hashlib
import inspect
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration
from src.analysis.open_intelligence import execution_approval, execution_origins

from tests.unit.test_execution_manifest_origins import CASES, load_registry, manifest

APPROVED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
RESOURCE_SHA = "e" * 64
RESULT_JSON = '{"ok":true}'
RESULT_DIGEST = hashlib.sha256(RESULT_JSON.encode()).hexdigest()
RESULT_REFERENCE = "gs://ogilvy-trends-v2-execution-approvals-staging/results/example.json"
APPROVAL_FIELDS = (
    "approval_contract_version",
    "approval_id",
    "manifest_version",
    "operation",
    "contract_sha256",
    "manifest_sha256",
    "canonical_manifest_json",
    "approved_by",
    "approved_at",
    "expires_at",
    "approval_phrase_sha256",
    "origin_registry_sha256",
    "resource_manifest_sha256",
)
CONSUMPTION_FIELDS = (
    "consumption_contract_version",
    "consumption_id",
    "approval_id",
    "manifest_sha256",
    "operation",
    "execution_name",
    "job_resource",
    "source_sha",
    "image_uri",
    "consumed_at",
    "origin_registry_sha256",
    "resource_manifest_sha256",
)
RESULT_FIELDS = (
    "result_contract_version",
    "result_id",
    "consumption_id",
    "approval_id",
    "manifest_sha256",
    "operation",
    "execution_name",
    "result_reference",
    "canonical_result_json",
    "result_digest",
    "status",
    "completed_at",
    "origin_registry_sha256",
    "resource_manifest_sha256",
)


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def timestamp(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def phrase(operation, manifest_sha, registry_sha):
    return (
        "I approve one 42 staging execution of "
        f"{operation} for manifest SHA256 {manifest_sha}, "
        f"origin registry SHA256 {registry_sha} and "
        f"resource manifest SHA256 {RESOURCE_SHA}. Production remains unchanged."
    )


def context(registry=None, mode="new_consume", expected=RESOURCE_SHA):
    return {
        "mode": mode,
        "registry": registry or load_registry(),
        "expected_resource_manifest_sha256": expected,
    }


def chain(
    operation="r3_apply",
    *,
    approved_at=None,
    consumed_at=None,
    completed_at=None,
    registry=None,
    payload=None,
):
    registry = load_registry() if registry is None else registry
    payload = manifest(operation) if payload is None else payload
    canonical_manifest = execution_approval.canonical_manifest_bytes(
        payload,
        mode="new_consume",
        registry=registry,
    )
    manifest_sha = hashlib.sha256(canonical_manifest).hexdigest()
    approved_at = approved_at or datetime(2026, 9, 13, 12, tzinfo=UTC)
    consumed_at = consumed_at or approved_at
    completed_at = completed_at or consumed_at
    expires_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
    phrase_sha = hashlib.sha256(
        phrase(operation, manifest_sha, registry.sha256).encode()
    ).hexdigest()
    approval_id = execution_approval.approval_id_v2(
        manifest_sha,
        APPROVED_BY,
        approved_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    approval = execution_approval.ExecutionApprovalV2(
        approval_contract_version="open_intelligence_execution_approval_v2",
        approval_id=approval_id,
        manifest_version="open_intelligence_execution_manifest_v2",
        operation=operation,
        contract_sha256=payload["contract_sha256"],
        manifest_sha256=manifest_sha,
        canonical_manifest_json=canonical_manifest.decode(),
        approved_by=APPROVED_BY,
        approved_at=approved_at,
        expires_at=expires_at,
        approval_phrase_sha256=phrase_sha,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
        **context(registry),
    )
    execution_name = payload["job_resource"] + "/executions/example"
    consumption_id = execution_approval.consumption_id_v2(
        approval_id,
        execution_name,
        consumed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    consumption = execution_approval.ExecutionConsumptionV2(
        consumption_contract_version="open_intelligence_execution_consumption_v2",
        consumption_id=consumption_id,
        approval_id=approval_id,
        manifest_sha256=manifest_sha,
        operation=operation,
        execution_name=execution_name,
        job_resource=payload["job_resource"],
        source_sha=payload["source_sha"],
        image_uri=payload["image_uri"],
        consumed_at=consumed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
        **context(registry),
    )
    result_id = execution_approval.result_id_v2(
        consumption_id,
        RESULT_REFERENCE,
        RESULT_DIGEST,
        "succeeded",
        completed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    result = execution_approval.ExecutionResultV2(
        result_contract_version="open_intelligence_execution_result_v2",
        result_id=result_id,
        consumption_id=consumption_id,
        approval_id=approval_id,
        manifest_sha256=manifest_sha,
        operation=operation,
        execution_name=execution_name,
        result_reference=RESULT_REFERENCE,
        canonical_result_json=RESULT_JSON,
        result_digest=RESULT_DIGEST,
        status="succeeded",
        completed_at=completed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
        **context(registry),
    )
    return registry, approval, consumption, result


def rebuild(record, registry, **changes):
    values = {field.name: getattr(record, field.name) for field in fields(record)}
    values.update(changes)
    return type(record)(**values, **context(registry))


def nested_object(depth):
    return '{"v":' + "[" * (depth - 1) + "0" + "]" * (depth - 1) + "}"


def rebuild_result_json(depth, raw=None):
    registry, _approval, _consumption, result = chain()
    value = raw if raw is not None else nested_object(depth)
    digest = hashlib.sha256(value.encode()).hexdigest()
    result_id = execution_approval.result_id_v2(
        result.consumption_id,
        result.result_reference,
        digest,
        result.status,
        result.completed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    return (
        registry,
        rebuild(
            result,
            registry,
            canonical_result_json=value,
            result_digest=digest,
            result_id=result_id,
        ),
        value,
    )


def refusal(code="execution_approval_manifest_invalid"):
    return pytest.raises(
        (execution_approval.ApprovalRefusal, execution_origins.OriginRefusal),
        match=f"^{code}$",
    )


def test_v2_types_have_schema_order_and_required_nonserialized_context():
    schemas = __import__("pathlib").Path(__file__).resolve().parents[2] / "infra/bigquery_schemas"
    cases = (
        (
            execution_approval.ExecutionApprovalV2,
            APPROVAL_FIELDS,
            migration._schema_from_sql(
                (schemas / "open_intelligence_execution_approvals_v2.sql").read_text()
            ),
        ),
        (
            execution_approval.ExecutionConsumptionV2,
            CONSUMPTION_FIELDS,
            migration._schema_from_sql(
                (schemas / "open_intelligence_execution_consumptions_v2.sql").read_text()
            ),
        ),
        (
            execution_approval.ExecutionResultV2,
            RESULT_FIELDS,
            migration._schema_from_sql(
                (schemas / "open_intelligence_execution_results_v2.sql").read_text()
            ),
        ),
    )
    for record_type, expected, schema in cases:
        assert tuple(field.name for field in fields(record_type)) == expected
        assert tuple(field[0] for field in schema) == expected
        timestamp_fields = {"approved_at", "expires_at", "consumed_at", "completed_at"}
        assert tuple((field[1], field[2]) for field in schema) == tuple(
            ("TIMESTAMP" if name in timestamp_fields else "STRING", "REQUIRED") for name in expected
        )
        signature = inspect.signature(record_type)
        for name in ("mode", "registry", "expected_resource_manifest_sha256"):
            assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert signature.parameters[name].default is inspect.Parameter.empty
            assert name not in expected
            assert name not in record_type.__slots__


def test_v2_id_preimages_are_independently_exact():
    registry = load_registry()
    approved_at = datetime(2026, 9, 13, 12, tzinfo=UTC)
    consumed_at = approved_at + timedelta(minutes=1)
    completed_at = consumed_at + timedelta(minutes=1)
    manifest_sha = "a" * 64
    approval_preimage = {
        "approval_contract_version": "open_intelligence_execution_approval_v2",
        "approved_at": timestamp(approved_at),
        "approved_by": APPROVED_BY,
        "manifest_sha256": manifest_sha,
        "origin_registry_sha256": registry.sha256,
        "resource_manifest_sha256": RESOURCE_SHA,
    }
    approval = "exa_" + hashlib.sha256(canonical(approval_preimage)).hexdigest()
    assert approval == execution_approval.approval_id_v2(
        manifest_sha,
        APPROVED_BY,
        approved_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    execution = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "intelligence-42-apply-staging/executions/example"
    )
    consumption_preimage = {
        "approval_id": approval,
        "consumed_at": timestamp(consumed_at),
        "consumption_contract_version": "open_intelligence_execution_consumption_v2",
        "execution_name": execution,
        "origin_registry_sha256": registry.sha256,
        "resource_manifest_sha256": RESOURCE_SHA,
    }
    consumption = "exc_" + hashlib.sha256(canonical(consumption_preimage)).hexdigest()
    assert consumption == execution_approval.consumption_id_v2(
        approval,
        execution,
        consumed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    result_preimage = {
        "completed_at": timestamp(completed_at),
        "consumption_id": consumption,
        "origin_registry_sha256": registry.sha256,
        "resource_manifest_sha256": RESOURCE_SHA,
        "result_contract_version": "open_intelligence_execution_result_v2",
        "result_digest": RESULT_DIGEST,
        "result_reference": RESULT_REFERENCE,
        "status": "succeeded",
    }
    expected = "exr_" + hashlib.sha256(canonical(result_preimage)).hexdigest()
    assert expected == execution_approval.result_id_v2(
        consumption,
        RESULT_REFERENCE,
        RESULT_DIGEST,
        "succeeded",
        completed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )


@pytest.mark.parametrize("operation", CASES)
def test_every_v2_operation_constructs_exact_records_bytes_and_chain(operation):
    registry, approval, consumption, result = chain(operation)
    approval_bytes = execution_approval.canonical_approval_v2_bytes(approval, **context(registry))
    consumption_bytes = execution_approval.canonical_consumption_v2_bytes(
        consumption, **context(registry)
    )
    result_bytes = execution_approval.canonical_result_v2_bytes(result, **context(registry))
    assert tuple(json.loads(approval_bytes)) == tuple(sorted(APPROVAL_FIELDS))
    assert tuple(json.loads(consumption_bytes)) == tuple(sorted(CONSUMPTION_FIELDS))
    assert tuple(json.loads(result_bytes)) == tuple(sorted(RESULT_FIELDS))
    approval_mapping = json.loads(approval_bytes)
    for name in ("mode", "registry", "expected_resource_manifest_sha256"):
        assert name not in approval_mapping
    assert approval_bytes == canonical(
        {
            "approval_contract_version": approval.approval_contract_version,
            "approval_id": approval.approval_id,
            "manifest_version": approval.manifest_version,
            "operation": approval.operation,
            "contract_sha256": approval.contract_sha256,
            "manifest_sha256": approval.manifest_sha256,
            "canonical_manifest_json": approval.canonical_manifest_json,
            "approved_by": approval.approved_by,
            "approved_at": timestamp(approval.approved_at),
            "expires_at": timestamp(approval.expires_at),
            "approval_phrase_sha256": approval.approval_phrase_sha256,
            "origin_registry_sha256": approval.origin_registry_sha256,
            "resource_manifest_sha256": approval.resource_manifest_sha256,
        }
    )
    assert (
        execution_approval.validate_execution_chain_v2(
            approval,
            consumption,
            result,
            **context(registry),
        )
        is None
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "actor",
        "phrase",
        "zero_horizon",
        "long_horizon",
        "manifest_hash",
        "resource_generation",
        "origin_generation",
    ],
)
def test_approval_actor_phrase_horizon_hash_and_generation_mutations_refuse(mutation):
    registry, approval, _consumption, _result = chain()
    changes = {}
    if mutation == "actor":
        changes["approved_by"] = "other"
    elif mutation == "phrase":
        changes["approval_phrase_sha256"] = "f" * 64
    elif mutation == "zero_horizon":
        changes["expires_at"] = approval.approved_at
    elif mutation == "long_horizon":
        changes["expires_at"] = approval.approved_at + timedelta(hours=24, microseconds=1)
    elif mutation == "manifest_hash":
        changes["manifest_sha256"] = "f" * 64
    elif mutation == "resource_generation":
        changes["resource_manifest_sha256"] = "f" * 64
    else:
        changes["origin_registry_sha256"] = "f" * 64
    with refusal(
        "execution_approval_expired"
        if mutation in {"zero_horizon", "long_horizon"}
        else "execution_approval_manifest_invalid"
    ):
        rebuild(approval, registry, **changes)


@pytest.mark.parametrize(
    "field",
    ["job_resource", "execution_name", "source_sha", "image_uri", "operation", "consumption_id"],
)
def test_consumption_origin_identity_and_identifier_mutations_refuse(field):
    registry, _approval, consumption, _result = chain()
    replacements = {
        "job_resource": "wrong",
        "execution_name": consumption.execution_name.replace("daily-staging", "brain-staging"),
        "source_sha": "F" * 40,
        "image_uri": "us-central1-docker.pkg.dev/other/image@sha256:" + "a" * 64,
        "operation": "bootstrap_migration_apply",
        "consumption_id": "exc_" + "f" * 64,
    }
    with refusal():
        rebuild(consumption, registry, **{field: replacements[field]})


@pytest.mark.parametrize(
    "field",
    ["result_id", "result_digest", "canonical_result_json", "status", "execution_name"],
)
def test_result_identifier_digest_status_and_execution_mutations_refuse(field):
    registry, _approval, _consumption, result = chain()
    replacements = {
        "result_id": "exr_" + "f" * 64,
        "result_digest": "f" * 64,
        "canonical_result_json": '{"ok":false}',
        "status": "unknown",
        "execution_name": result.execution_name.replace("daily-staging", "brain-staging"),
    }
    with refusal():
        rebuild(result, registry, **{field: replacements[field]})


@pytest.mark.parametrize(
    ("record_name", "field", "replacement"),
    [
        ("consumption", "consumption_id", 1),
        ("consumption", "approval_id", []),
        ("consumption", "manifest_sha256", None),
        ("consumption", "execution_name", 1),
        ("result", "result_id", 1),
        ("result", "consumption_id", []),
        ("result", "approval_id", None),
        ("result", "status", []),
    ],
)
def test_record_primitive_type_faults_use_stable_refusal(record_name, field, replacement):
    registry, _approval, consumption, result = chain()
    record = consumption if record_name == "consumption" else result
    with refusal():
        rebuild(record, registry, **{field: replacement})


def test_altered_standalone_link_key_validates_but_complete_chain_refuses():
    registry, approval, consumption, result = chain()
    other_approval = "exa_" + "f" * 64
    consumed_at = consumption.consumed_at
    altered_consumption_id = execution_approval.consumption_id_v2(
        other_approval,
        consumption.execution_name,
        consumed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    altered = rebuild(
        consumption,
        registry,
        approval_id=other_approval,
        consumption_id=altered_consumption_id,
    )
    assert altered.approval_id == other_approval
    with refusal():
        execution_approval.validate_execution_chain_v2(
            approval,
            altered,
            result,
            **context(registry),
        )


@pytest.mark.parametrize(
    "link",
    [
        "consumption_manifest",
        "consumption_source",
        "result_consumption",
        "result_approval",
        "result_manifest",
        "result_execution",
        "result_operation",
    ],
)
def test_each_cross_row_link_substitution_refuses_only_at_complete_chain(link):
    registry, approval, consumption, result = chain()
    if link == "consumption_manifest":
        consumption = rebuild(consumption, registry, manifest_sha256="f" * 64)
    elif link == "consumption_source":
        consumption = rebuild(consumption, registry, source_sha="f" * 40)
    elif link == "result_consumption":
        foreign = "exc_" + "f" * 64
        result = rebuild(
            result,
            registry,
            consumption_id=foreign,
            result_id=execution_approval.result_id_v2(
                foreign,
                result.result_reference,
                result.result_digest,
                result.status,
                result.completed_at,
                origin_registry_sha256=registry.sha256,
                resource_manifest_sha256=RESOURCE_SHA,
            ),
        )
    elif link == "result_approval":
        result = rebuild(result, registry, approval_id="exa_" + "f" * 64)
    elif link == "result_manifest":
        result = rebuild(result, registry, manifest_sha256="f" * 64)
    elif link == "result_execution":
        result = rebuild(
            result,
            registry,
            execution_name=result.execution_name.rsplit("/", 1)[0] + "/other",
        )
    else:
        _other_registry, _other_approval, _other_consumption, result = chain("brain_read")
    with refusal():
        execution_approval.validate_execution_chain_v2(
            approval,
            consumption,
            result,
            **context(registry),
        )


@pytest.mark.parametrize(
    ("consumed_delta", "completed_delta", "accepted"),
    [
        (timedelta(0), timedelta(0), True),
        (timedelta(hours=23, minutes=59), timedelta(0), True),
        (timedelta(hours=24), timedelta(0), False),
        (timedelta(seconds=-1), timedelta(0), False),
        (timedelta(0), timedelta(seconds=-1), False),
    ],
)
def test_chain_time_boundaries(consumed_delta, completed_delta, accepted):
    approved = datetime(2026, 9, 13, 12, tzinfo=UTC)
    registry, approval, consumption, result = chain(
        approved_at=approved,
        consumed_at=approved + consumed_delta,
        completed_at=approved + consumed_delta + completed_delta,
    )

    def call():
        return execution_approval.validate_execution_chain_v2(
            approval,
            consumption,
            result,
            **context(registry),
        )

    if accepted:
        assert call() is None
    else:
        with refusal():
            call()


def test_chain_refuses_mixed_v1_family_and_generation_context():
    registry, approval, consumption, result = chain()
    with refusal():
        execution_approval.validate_execution_chain_v2(
            execution_approval.ExecutionApproval,
            consumption,
            result,
            **context(registry),
        )
    with refusal():
        execution_approval.validate_execution_chain_v2(
            approval,
            consumption,
            result,
            **context(registry, expected="f" * 64),
        )
    for missing in (None,):
        with refusal():
            execution_approval.validate_execution_chain_v2(
                missing,
                consumption,
                result,
                **context(registry),
            )


def test_existing_v1_identifier_vectors_remain_exact():
    approved_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    consumed_at = datetime(2026, 8, 31, 12, 1, tzinfo=UTC)
    completed_at = datetime(2026, 8, 31, 12, 2, tzinfo=UTC)
    execution = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-r3-proof-staging/executions/example"
    )
    approval = execution_approval.approval_id("a" * 64, APPROVED_BY, approved_at)
    assert approval == "exa_d1a0a7295487bb867802032943bf64405a3a686741d9b34235d0605cf8db28c0"
    consumption = execution_approval.consumption_id(approval, execution, consumed_at)
    assert consumption == "exc_a57a6431f845c0a2ed0e4ee1d3d9d7a34b17b2ed78972d70f9ddb9b3e0913329"
    result = execution_approval.result_id(
        consumption,
        RESULT_REFERENCE,
        "b" * 64,
        "succeeded",
        completed_at,
    )
    assert result == "exr_cf00cbae7c9b4c6b563d4ba409ca836202ebf0c8a402b0e28a51a2019126565a"


@pytest.mark.parametrize("depth", [499, 500])
def test_complete_v2_result_accepts_depth_boundary_and_serializes_exact_json(depth):
    registry, result, value = rebuild_result_json(depth)
    serialized = execution_approval.canonical_result_v2_bytes(
        result,
        **context(registry),
    )
    assert json.loads(serialized)["canonical_result_json"] == value


def test_complete_v2_result_refuses_depth_501_with_bounded_error():
    with refusal():
        rebuild_result_json(501)


@pytest.mark.parametrize(
    "value",
    [
        '{"v":' + "[" * 498 + '"\\u0065\\u0301"' + "]" * 498 + "}",
        '{"v":' + "[" * 498 + "0" + "]" * 497 + "}",
        '{"v":NaN}',
        '{"v":0,"v":0}',
        "[]",
    ],
)
def test_v2_canonical_json_deep_unicode_parse_number_duplicate_and_root_faults_refuse(value):
    with refusal():
        rebuild_result_json(500, value)


@pytest.mark.parametrize("depth", [500, 501])
def test_deep_invalid_approval_manifest_returns_bounded_refusal(depth):
    registry, approval, _consumption, _result = chain()
    value = nested_object(depth)
    digest = hashlib.sha256(value.encode()).hexdigest()
    phrase_sha = hashlib.sha256(
        phrase(approval.operation, digest, registry.sha256).encode()
    ).hexdigest()
    approval_id = execution_approval.approval_id_v2(
        digest,
        approval.approved_by,
        approval.approved_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    with pytest.raises((execution_approval.ApprovalRefusal, execution_origins.OriginRefusal)):
        rebuild(
            approval,
            registry,
            canonical_manifest_json=value,
            manifest_sha256=digest,
            approval_phrase_sha256=phrase_sha,
            approval_id=approval_id,
        )


def test_v1_recursive_canonical_helpers_keep_exact_source():
    assert hashlib.sha256(inspect.getsource(execution_approval._is_nfc).encode()).hexdigest() == (
        "576709b6472ca1f05358fc3d1188e9a23b2ef8ce9088f146aec5fb5a9debc99d"
    )
    assert (
        hashlib.sha256(
            inspect.getsource(execution_approval._canonical_json_object).encode()
        ).hexdigest()
        == "8e668abe5ccade6642d3683fbc5be841ba8cd35603ff4c3b0f8181e5b629e59f"
    )
