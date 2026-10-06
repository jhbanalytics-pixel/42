"""Test-only funded generation, loaded through the real authority validators."""

import json
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

from src.analysis.open_intelligence import execution_approval as runtime
from src.analysis.open_intelligence import execution_generations, execution_origins
from src.analysis.open_intelligence.daily_products import POLICY_PATH

from tests.unit import test_execution_runtime_v2 as existing
from tests.unit.test_execution_generations import (
    REGISTRY_RELATIVE,
    RESOURCE_RELATIVE,
    point_catalogue_at,
    temp_root,
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


# A vector the daily compose rule admits: its prefix and a cutoff date.
COMPOSE_ARGUMENTS = [
    "scripts/staging/compose_daily_open_intelligence.py",
    "--cutoff-date",
    "2026-09-22",
]


def funded_product_authority(
    tmp_path, monkeypatch, *, artifacts=None, limits=None, operation="r3_apply"
):
    """A consumed authority for ``operation`` under a test generation.

    For ``r3_apply`` the rule's limits are rewritten to exact values, as before the daily
    compose had its own operation. For ``daily_composition_apply`` the committed rule
    stands and the manifest asks for its maxima, or for ``limits`` where given, so a
    request past the committed bounds refuses at the real validators.
    """
    fx = existing.fixture()
    root = temp_root(tmp_path, monkeypatch)
    registry_path = root / REGISTRY_RELATIVE
    registry = json.loads(registry_path.read_bytes())
    [row] = [
        row
        for row in registry["rows"]
        if fx.payload["contract_sha256"] in row["contract_digests"]
        and "new_consume" in row["allowed_execution_modes"]
        and operation in row["exact_operation_bindings"]
    ]
    policy_path = root / row["contract_file"]
    policy = json.loads(policy_path.read_bytes())
    rule = policy["operation_validation"][operation]
    if operation == "r3_apply":
        limits = {
            "max_bytes_billed": 1_000_000_000,
            "max_credits": 0,
            "max_model_calls": 100,
            "max_rows_written": 10_000,
            **(limits or {}),
        }
        rule["limits"] = {name: {"exact": value} for name, value in limits.items()}
    else:
        limits = {
            name: item.get("exact", item.get("maximum")) for name, item in rule["limits"].items()
        } | (limits or {})
        fx.payload["operation"] = operation
        fx.payload["arguments"] = list(COMPOSE_ARGUMENTS)
        fx.operation = operation
        for task in (fx.execution["template"], fx.job["template"]["template"]):
            task["containers"][0]["args"] = list(COMPOSE_ARGUMENTS)
    contents = dict(fx.contents)
    contents["config"] = POLICY_PATH.read_bytes()
    contents.update(artifacts or {})
    rule["input_artifact_names"] = sorted(contents)
    policy_path.write_bytes(_canonical(policy))
    policy_digest = sha256(policy_path.read_bytes()).hexdigest()
    row["contract_sha256"] = policy_digest
    row["contract_digests"] = [policy_digest]
    registry_path.write_bytes(_canonical(registry))
    registry_digest = sha256(registry_path.read_bytes()).hexdigest()
    resource_path = root / RESOURCE_RELATIVE
    resources = json.loads(resource_path.read_bytes())
    resources["origin_registry_sha256"] = registry_digest
    resource_path.write_bytes(_canonical(resources))
    resource_digest = sha256(resource_path.read_bytes()).hexdigest()
    point_catalogue_at(monkeypatch, registry_digest, resource_digest)
    generation = execution_generations.load_trusted_generation(registry_digest, resource_digest)
    payload = fx.payload
    payload["contract_sha256"] = policy_digest
    payload["limits"] = limits
    origin = execution_origins.select_origin(
        manifest_version=payload["manifest_version"],
        contract_sha256=policy_digest,
        mode="new_consume",
        registry=generation.registry,
    )
    provenance = runtime.build_provenance_from_response(
        fx.build,
        policy_digest,
        manifest_version=payload["manifest_version"],
        mode="new_consume",
        registry=generation.registry,
    )
    contents["build_provenance"] = runtime.canonical_build_provenance_bytes(
        provenance, origin=origin
    )
    payload["input_artifacts"] = [
        {"name": name, "sha256": sha256(raw).hexdigest()} for name, raw in sorted(contents.items())
    ]
    canonical = runtime.canonical_manifest_bytes(
        payload, mode="new_consume", registry=generation.registry
    )
    digest = sha256(canonical).hexdigest()
    approval = replace(
        fx.approval,
        operation=operation,
        approval_id=runtime.approval_id_v2(
            digest,
            fx.approval.approved_by,
            fx.approval.approved_at,
            origin_registry_sha256=registry_digest,
            resource_manifest_sha256=resource_digest,
        ),
        contract_sha256=policy_digest,
        manifest_sha256=digest,
        canonical_manifest_json=canonical.decode(),
        approval_phrase_sha256=existing.phrase_sha(
            operation, digest, registry_digest, resource_digest
        ),
        origin_registry_sha256=registry_digest,
        resource_manifest_sha256=resource_digest,
        mode="new_consume",
        registry=generation.registry,
        expected_resource_manifest_sha256=resource_digest,
    )
    for annotations in (fx.execution["annotations"], fx.job["template"]["annotations"]):
        annotations["42.ogilvy/execution-approval-sha256"] = digest
        annotations[existing.ANNOTATION_REGISTRY] = registry_digest
        annotations[existing.ANNOTATION_RESOURCE] = resource_digest
    fx.approval, fx.contents = approval, contents
    authority = existing.load(fx)
    consumption = runtime._consume_execution_authority(
        authority,
        consumption_writer=lambda request: existing.consumption_row(
            fx,
            request,
            resource_sha=resource_digest,
        ),
    )
    receipt = {
        "business_attempt_id": "test-funded-attempt-1",
        "execution": {
            "authority": authority,
            "consumption": consumption,
            "stage": "compose",
            "mode": "new_consume",
        },
    }
    return SimpleNamespace(
        authority=authority,
        consumption=consumption,
        receipt=receipt,
        generation=generation,
        artifacts=contents,
        fixture=fx,
    )
