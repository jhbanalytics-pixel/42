"""The daily capture operation admits a v3 bridge plan only with its four bound artifacts.

All records are synthetic and unissued. None of them grants execution authority.
"""

import base64
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from src.analysis.open_intelligence import daily_execution_authority as authority
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_execution_contracts import validate_capture_semantics
from src.analysis.open_intelligence.source_estate_bridge_plan import build_bridge_plan

from tests.unit import test_daily_execution_authority as chain_fixture
from tests.unit import test_source_estate_bridge_plan as plan_fixture
from tests.unit.test_source_estate_bridge_contract import NOW, A

BRIDGE_ARTIFACTS = (
    "bridge_policy",
    "collection_receipt_set",
    "history_completion_set",
    "temporal_rules",
)
BASE_ARTIFACTS = ("build_provenance", "capture_contract", "cost_policy", "daily_profile")
CUTOFF_UTC = "2026-09-21T00:00:00+00:00"


def context(**changes):
    value = {
        "cutoff_utc": CUTOFF_UTC,
        "mode": "initial",
        "predecessor_result_digest": None,
        "predecessor_result_id": None,
    }
    value.update(changes)
    return value


def v3_case():
    inputs = plan_fixture.inputs()
    plan = build_bridge_plan(**inputs)
    artifacts = {
        name: canonical_bytes({"contract_version": name + "_v1"}) for name in BASE_ARTIFACTS
    }
    artifacts.update(
        capture_plan=canonical_bytes(plan),
        storage_policy=canonical_bytes(inputs["storage_policy"]),
        source_metadata=canonical_bytes(inputs["source_metadata"]),
        recovery_context=b"null",
    )
    for name in BRIDGE_ARTIFACTS:
        artifacts[name] = canonical_bytes(inputs["artifacts"][name])
    return artifacts, plan, inputs


def now():
    return datetime.fromisoformat(NOW)


def test_v3_plan_is_admitted_with_its_four_bridge_artifacts_and_a_clock():
    artifacts, _plan, _inputs = v3_case()
    assert validate_capture_semantics(context(), artifacts, now=now()) is None


@pytest.mark.parametrize("missing", BRIDGE_ARTIFACTS)
def test_v3_plan_without_any_bridge_artifact_refuses(missing):
    artifacts, _plan, _inputs = v3_case()
    del artifacts[missing]
    with pytest.raises(ValueError, match=r"^daily_capture_artifact_set_invalid$"):
        validate_capture_semantics(context(), artifacts, now=now())


def test_v3_artifact_set_is_exact():
    artifacts, _plan, _inputs = v3_case()
    artifacts["extra"] = b"{}"
    with pytest.raises(ValueError, match=r"^daily_capture_artifact_set_invalid$"):
        validate_capture_semantics(context(), artifacts, now=now())


def test_v2_plan_cannot_carry_bridge_artifacts():
    artifacts, _plan, _inputs = v3_case()
    v2 = {
        "contract_version": "open_intelligence_protected_capture_plan_v2",
        "cutoff_date": "2026-09-20",
        "snapshot_plan": {
            "observation_window_end": CUTOFF_UTC,
            "profile_version": "42_staging_source_v2",
            "projection_version": "native_id_bound_v1",
            "source_dataset": "intelligence_42_sources_staging",
            "source_estate_digest": json.loads(artifacts["storage_policy"])["grant"][
                "source_estate_digest"
            ],
        },
    }
    artifacts["capture_plan"] = canonical_bytes(v2)
    with pytest.raises(ValueError, match=r"^daily_capture_artifact_set_invalid$"):
        validate_capture_semantics(context(), artifacts)
    for name in BRIDGE_ARTIFACTS:
        del artifacts[name]
    assert validate_capture_semantics(context(), artifacts) is None


def test_v3_plan_needs_the_reader_clock():
    artifacts, _plan, _inputs = v3_case()
    with pytest.raises(ValueError, match=r"^daily_capture_clock_invalid$"):
        validate_capture_semantics(context(), artifacts)
    with pytest.raises(ValueError, match=r"^daily_capture_clock_invalid$"):
        validate_capture_semantics(context(), artifacts, now=NOW)
    with pytest.raises(ValueError, match=r"^daily_capture_clock_invalid$"):
        validate_capture_semantics(context(), artifacts, now=now().replace(tzinfo=None))


def test_v3_plan_before_the_collection_clone_instant_refuses():
    artifacts, _plan, _inputs = v3_case()
    early = datetime.fromisoformat(A) - timedelta(seconds=1)
    with pytest.raises(ValueError, match=r"^daily_capture_plan_invalid$"):
        validate_capture_semantics(context(), artifacts, now=early)


def _plan_edit(plan, edit):
    changed = deepcopy(plan)
    edit(changed)
    return changed


@pytest.mark.parametrize(
    "edit",
    [
        lambda plan: plan["creation_statements"][0].update(sql="SELECT 1"),
        lambda plan: plan["creation_statements"].pop(),
        lambda plan: plan.update(market_scope=["za"]),
        lambda plan: plan.update(client_scope_id="another_scope"),
        lambda plan: plan.update(client_scope_id="fixture_scope"),
        lambda plan: plan["snapshot_plan"]["relation_bindings"][0].update(
            destination_table="ogilvy-trends-v2.trends_v2_staging.enriched_content"
        ),
        lambda plan: plan["snapshot_plan"].update(grant_id="another_grant"),
    ],
)
def test_v3_plan_is_regenerated_not_trusted(edit):
    artifacts, plan, _inputs = v3_case()
    artifacts["capture_plan"] = canonical_bytes(_plan_edit(plan, edit))
    with pytest.raises(ValueError, match=r"^daily_capture_plan_invalid$"):
        validate_capture_semantics(context(), artifacts, now=now())


def test_v3_plan_is_bound_to_the_context_cutoff():
    artifacts, _plan, _inputs = v3_case()
    with pytest.raises(ValueError, match=r"^daily_capture_plan_invalid$"):
        validate_capture_semantics(
            context(cutoff_utc="2026-09-22T00:00:00+00:00"), artifacts, now=now()
        )


@pytest.mark.parametrize("name", BRIDGE_ARTIFACTS)
def test_v3_plan_refuses_changed_bridge_artifact_bytes(name):
    artifacts, _plan, inputs = v3_case()
    value = deepcopy(inputs["artifacts"][name])
    if name == "bridge_policy":
        value["market_scope"] = ["za"]
    elif name == "temporal_rules":
        value["rules"] = value["rules"][:1]
    elif name == "collection_receipt_set":
        value["receipts"][0]["run_id"] = "run-other"
    else:
        value["entries"][0]["reason_code"] = "native_completion_unrecorded"
        value["entries"][0]["product_date"] = "2026-09-18"
    artifacts[name] = canonical_bytes(value)
    with pytest.raises(ValueError, match=r"^daily_capture_plan_invalid$"):
        validate_capture_semantics(context(), artifacts, now=now())


def test_v3_bridge_artifacts_must_be_canonical_json():
    artifacts, _plan, _inputs = v3_case()
    artifacts["bridge_policy"] = artifacts["bridge_policy"] + b" "
    with pytest.raises(ValueError, match=r"^daily_capture_plan_invalid$"):
        validate_capture_semantics(context(), artifacts, now=now())


def test_v3_source_metadata_is_the_artifact_the_plan_was_built_from():
    artifacts, _plan, inputs = v3_case()
    metadata = deepcopy(inputs["source_metadata"])
    table = next(iter(metadata["tables"]))
    metadata["tables"][table]["schema"]["fields"][1]["name"] = "other_payload"
    artifacts["source_metadata"] = canonical_bytes(metadata)
    with pytest.raises(ValueError, match=r"^daily_capture_plan_invalid$"):
        validate_capture_semantics(context(), artifacts, now=now())


def test_v3_storage_policy_estate_is_bound_to_the_plan():
    artifacts, _plan, _inputs = v3_case()
    storage = deepcopy(json.loads(artifacts["storage_policy"]))
    storage["grant"]["source_estate_digest"] = "f" * 64
    artifacts["storage_policy"] = canonical_bytes(storage)
    with pytest.raises(ValueError, match=r"^daily_capture_storage_invalid$"):
        validate_capture_semantics(context(), artifacts, now=now())


def test_v3_recovery_keeps_the_initial_result_binding():
    artifacts, _plan, _inputs = v3_case()
    recover = context(
        mode="recover",
        predecessor_result_id="exr_" + "c" * 64,
        predecessor_result_digest="c" * 64,
    )
    with pytest.raises(ValueError, match=r"^daily_capture_recovery_invalid$"):
        validate_capture_semantics(recover, artifacts, now=now())
    artifacts["recovery_context"] = canonical_bytes(
        {
            "contract_version": "open_intelligence_daily_source_capture_recovery_v1",
            "initial_operation": "daily_source_snapshot_capture",
            "initial_result_id": "exr_" + "c" * 64,
            "initial_result_digest": "c" * 64,
        }
    )
    assert validate_capture_semantics(recover, artifacts, now=now()) is None


class DeriveClients:
    def __init__(self):
        self.calls = []

    def derive_execution(self, *parameters):
        self.calls.append(parameters)
        return {
            "operation": "daily_source_snapshot_capture",
            "operation_context_sha256": parameters[3],
        }


def derive_inputs():
    artifacts, _plan, _inputs = v3_case()
    envelope = {
        "artifacts": [
            {
                "data": base64.b64encode(artifacts[name]).decode("ascii"),
                "encoding": "base64",
                "name": name,
            }
            for name in sorted(artifacts)
        ],
        "contract_version": "daily_operation_artifact_set_v1",
    }
    artifact_json = chain_fixture._canonical(envelope)
    context_json, _artifact_json, _observation, _parts = chain_fixture._fixture()
    value = json.loads(context_json)
    value.update(
        operation="daily_source_snapshot_capture",
        stage="capture",
        mode="initial",
        cutoff_utc=CUTOFF_UTC,
        predecessor_result_id="exr_" + chain_fixture.HEX,
        predecessor_result_digest=chain_fixture.HEX,
        operation_artifact_set_sha256=hashlib.sha256(artifact_json.encode()).hexdigest(),
    )
    context_json = chain_fixture._canonical(value)
    context_digest = hashlib.sha256(context_json.encode()).hexdigest()
    manifest = {
        "input_artifacts": [
            {"name": "operation_context", "sha256": context_digest},
            *(
                {"name": name, "sha256": hashlib.sha256(artifacts[name]).hexdigest()}
                for name in sorted(artifacts)
            ),
        ],
        # Derive refuses a daily manifest without four bounded limits before any plan check.
        "limits": {
            "max_bytes_billed": 1_000_000_000,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 0,
        },
    }
    manifest_json = chain_fixture._canonical(manifest)
    return {
        "canonical_manifest_json": manifest_json,
        "manifest_sha256": hashlib.sha256(manifest_json.encode()).hexdigest(),
        "canonical_operation_context_json": context_json,
        "operation_context_sha256": context_digest,
        "canonical_operation_artifact_set_json": artifact_json,
        "authorizing_grant_digest": value["authorizing_grant_digest"],
    }


def test_derivation_checks_a_v3_capture_plan_on_its_own_clock(monkeypatch):
    monkeypatch.setattr(authority, "_utc_now", now)
    clients = DeriveClients()
    row = authority.derive_daily_execution(**derive_inputs(), clients=clients)
    assert row["operation"] == "daily_source_snapshot_capture"
    assert len(clients.calls) == 1


def test_derivation_refuses_a_v3_capture_plan_its_clock_cannot_admit(monkeypatch):
    early = datetime.fromisoformat(A) - timedelta(seconds=1)
    monkeypatch.setattr(authority, "_utc_now", lambda: early)
    clients = DeriveClients()
    with pytest.raises(ValueError, match=r"^daily_capture_plan_invalid$"):
        authority.derive_daily_execution(**derive_inputs(), clients=clients)
    assert clients.calls == []
