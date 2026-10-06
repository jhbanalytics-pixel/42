"""Contract proof for the versioned (v2) execution routines and their installation maps."""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import fields
from pathlib import Path

import pytest
from scripts import setup_bigquery as setup
from scripts.migrations import create_open_intelligence_execution_approval_store as migration
from src.analysis.open_intelligence import execution_approval, execution_origins

from tests.unit.test_execution_manifest_origins import (
    CASES,
    MANIFEST_V1,
    MANIFEST_V2,
    load_registry,
    manifest,
)
from tests.unit.test_execution_records_v2 import RESOURCE_SHA, chain, timestamp

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / "infra" / "bigquery_routines"
SCHEMAS = ROOT / "infra" / "bigquery_schemas"
PROPOSAL_SKIP_REASON = "reviewed proposal packet is external to the source checkout"
APPROVED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
UDF = "fn_is_canonical_execution_json_v1"
CANONICAL_CALL = f"`{{project}}.{{dataset}}.{UDF}`("
EXACT_PARSE = "wide_number_mode => 'exact'"
PAIR = (("origin_registry_sha256", "STRING"), ("resource_manifest_sha256", "STRING"))

V2_ROUTINE_PARAMETERS = {
    "sp_approve_open_intelligence_execution_v2": (
        ("canonical_manifest_json", "STRING"),
        ("manifest_sha256", "STRING"),
        ("approval_phrase", "STRING"),
        *PAIR,
    ),
    "sp_approve_open_intelligence_execution_v3": (
        ("canonical_manifest_json", "STRING"),
        ("manifest_sha256", "STRING"),
        ("approval_phrase", "STRING"),
        *PAIR,
    ),
    "sp_read_open_intelligence_execution_approval_v2": (("manifest_sha256", "STRING"),),
    "sp_read_open_intelligence_execution_approval_v3": (("manifest_sha256", "STRING"),),
    "sp_consume_open_intelligence_execution_v2": (
        ("manifest_sha256", "STRING"),
        ("execution_name", "STRING"),
        ("job_resource", "STRING"),
        ("source_sha", "STRING"),
        ("image_uri", "STRING"),
        *PAIR,
    ),
    "sp_record_open_intelligence_execution_result_v2": (
        ("consumption_id", "STRING"),
        ("result_reference", "STRING"),
        ("canonical_result_json", "STRING"),
        ("result_digest", "STRING"),
        ("status", "STRING"),
        *PAIR,
    ),
    "sp_read_open_intelligence_execution_result_v2": (("p_consumption_id", "STRING"),),
    "sp_read_open_intelligence_execution_result_chain_v2": (
        ("p_source_operation", "STRING"),
        ("p_run_id", "STRING"),
    ),
    "sp_disable_open_intelligence_execution_approval_v2": (("approval_phrase", "STRING"),),
    "sp_consume_open_intelligence_source_snapshot_v2": (
        ("manifest_sha256", "STRING"),
        ("execution_name", "STRING"),
        ("job_resource", "STRING"),
        ("source_sha", "STRING"),
        ("image_uri", "STRING"),
        ("capture_plan_json", "STRING"),
        ("recovery_context_json", "STRING"),
        ("storage_policy_json", "STRING"),
        *PAIR,
    ),
}
# Amendment d: the three recurring grant v2 routines and the seven daily routines join
# the deployer in this order. They are a separate family from the execution approval
# routines above and carry their own contracts (test_daily_execution_sql_contracts.py).
AMENDMENT_D_ROUTINE_PARAMETERS = {
    "sp_approve_open_intelligence_recurring_grant_v2": (
        ("canonical_grant_json", "STRING"),
        ("grant_sha256", "STRING"),
        ("approval_phrase", "STRING"),
        *PAIR,
    ),
    "sp_read_open_intelligence_recurring_grant_v2": (("grant_sha256", "STRING"),),
    "sp_disable_open_intelligence_recurring_grant_v2": (
        ("grant_sha256", "STRING"),
        ("disable_phrase", "STRING"),
        ("reason_code", "STRING"),
        *PAIR,
    ),
    "sp_derive_open_intelligence_daily_execution_v1": (
        ("canonical_manifest_json", "STRING"),
        ("manifest_sha256", "STRING"),
        ("canonical_operation_context_json", "STRING"),
        ("operation_context_sha256", "STRING"),
        ("canonical_operation_artifact_set_json", "STRING"),
        ("authorizing_grant_digest", "STRING"),
    ),
    "sp_select_open_intelligence_daily_derivation_v1": (
        ("child_job_resource", "STRING"),
        ("child_image_uri", "STRING"),
        ("authorizing_grant_digest", "STRING"),
        ("child_job_policy_digest", "STRING"),
    ),
    "sp_consume_open_intelligence_daily_derivation_v1": (
        ("derivation_id", "STRING"),
        ("execution_name", "STRING"),
        ("canonical_operation_context_json", "STRING"),
        ("canonical_operation_artifact_set_json", "STRING"),
        ("canonical_execution_observation_json", "STRING"),
        ("execution_observation_sha256", "STRING"),
    ),
    "sp_cancel_open_intelligence_daily_derivation_v1": (
        ("derivation_id", "STRING"),
        ("reason_code", "STRING"),
        ("canonical_reconciliation_json", "STRING"),
        ("reconciliation_digest", "STRING"),
    ),
    "sp_read_open_intelligence_daily_derivation_v1": (("derivation_id", "STRING"),),
    "sp_read_open_intelligence_daily_chain_v1": (
        ("derivation_id", "STRING"),
        ("canonical_execution_observation_json", "STRING"),
        ("execution_observation_sha256", "STRING"),
    ),
    "sp_record_open_intelligence_daily_result_v1": (
        ("derivation_id", "STRING"),
        ("consumption_id", "STRING"),
        ("canonical_payload_json", "STRING"),
        ("payload_digest", "STRING"),
        ("execution_observation_sha256", "STRING"),
        ("canonical_stage_metering_json", "STRING"),
        ("result_reference", "STRING"),
        ("terminal_state", "STRING"),
        ("effect_state", "STRING"),
        ("spend_state", "STRING"),
    ),
}
EARLY_RETURN_ROUTINES = (
    "sp_approve_open_intelligence_recurring_grant_v2",
    "sp_disable_open_intelligence_recurring_grant_v2",
    "sp_derive_open_intelligence_daily_execution_v1",
    "sp_consume_open_intelligence_daily_derivation_v1",
    "sp_cancel_open_intelligence_daily_derivation_v1",
    "sp_record_open_intelligence_daily_result_v1",
    "sp_reconcile_open_intelligence_daily_consumption_v1",
)
CALLER_UNGATED_READERS = (
    "sp_read_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_chain_v1",
)
RECURRING_GRANT_ROUTINES = (
    "sp_approve_open_intelligence_recurring_grant_v2",
    "sp_read_open_intelligence_recurring_grant_v2",
    "sp_disable_open_intelligence_recurring_grant_v2",
)
DAILY_ROUTINES = (
    "sp_derive_open_intelligence_daily_execution_v1",
    "sp_select_open_intelligence_daily_derivation_v1",
    "sp_consume_open_intelligence_daily_derivation_v1",
    "sp_cancel_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_chain_v1",
    "sp_record_open_intelligence_daily_result_v1",
)
CUTOVER_SCRIPT = "sp_activate_open_intelligence_execution_generation_v2_cutover_script"
CUTOVER_PARAMETERS = (
    "origin_registry_sha256",
    "resource_manifest_sha256",
    "residual_consumption_ids_json",
    "residual_set_sha256",
    "activation_phrase",
)
MUTATING = (
    "sp_approve_open_intelligence_execution_v2",
    "sp_approve_open_intelligence_execution_v3",
    "sp_consume_open_intelligence_execution_v2",
    "sp_record_open_intelligence_execution_result_v2",
)
READERS = (
    "sp_read_open_intelligence_execution_approval_v2",
    "sp_read_open_intelligence_execution_approval_v3",
    "sp_read_open_intelligence_execution_result_v2",
    "sp_read_open_intelligence_execution_result_chain_v2",
)
AMENDMENT_D_READERS = (
    "sp_read_open_intelligence_recurring_grant_v2",
    "sp_select_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_derivation_v1",
    "sp_read_open_intelligence_daily_chain_v1",
)
# Amendment e: the owner reconciliation routine and the v3 source snapshot routine join
# the deployer after the amendment d family (the reconciliation contract is in
# test_daily_consumption_reconciliation.py).
AMENDMENT_E_ROUTINE_PARAMETERS = {
    "sp_reconcile_open_intelligence_daily_consumption_v1": (
        ("derivation_id", "STRING"),
        ("consumption_id", "STRING"),
        ("execution_name", "STRING"),
        ("execution_terminal_state", "STRING"),
        ("canonical_reconciliation_json", "STRING"),
        ("reconciliation_digest", "STRING"),
    ),
    # The bridge stack's v3 source snapshot routine; its contract is in
    # test_source_bridge_approval_preparation.py.
    "sp_consume_open_intelligence_source_snapshot_v3": (
        ("manifest_sha256", "STRING"),
        ("execution_name", "STRING"),
        ("job_resource", "STRING"),
        ("source_sha", "STRING"),
        ("image_uri", "STRING"),
        ("capture_plan_json", "STRING"),
        ("recovery_context_json", "STRING"),
        ("storage_policy_json", "STRING"),
        ("origin_registry_sha256", "STRING"),
        ("resource_manifest_sha256", "STRING"),
    ),
}
DEPLOYED_ROUTINES = (
    UDF,
    *V2_ROUTINE_PARAMETERS,
    *AMENDMENT_D_ROUTINE_PARAMETERS,
    *AMENDMENT_E_ROUTINE_PARAMETERS,
)
DEPLOYED_PARAMETERS = {
    **V2_ROUTINE_PARAMETERS,
    **AMENDMENT_D_ROUTINE_PARAMETERS,
    **AMENDMENT_E_ROUTINE_PARAMETERS,
}
OPERATOR_ONLY = ("sp_disable_open_intelligence_execution_approval_v2",)
SELECTED_OPERATIONS = (
    "migration_apply",
    "collection_exposure_issue",
    "r3_apply",
    "r3_proof_issue",
    "r3_release",
    "brain_read",
    "wave1_pilot",
)
EXCLUDED_OPERATIONS = ("source_snapshot_capture", "bootstrap_migration_apply")
SOURCE_SNAPSHOT_ROUTINE = "sp_consume_open_intelligence_source_snapshot_v2"

# Recorded at commit 819905d before any S1 change. The v1 family must stay byte identical.
V1_ROUTINE_SHA256 = {
    "fn_is_canonical_execution_json_v1.sql": (
        "5a677bbd10a2edb29dbb3a80c118a5c858d73a764b34798bece71b5692de5eea"
    ),
    "sp_approve_open_intelligence_execution_v1.sql": (
        "b20c99ac6964130119aba7c06bc86cf759ef72a21d1e24840775ba9e7404ab11"
    ),
    "sp_consume_open_intelligence_execution_v1.sql": (
        "26faa1fbbe9c8004423cd95cc955bb970c068fe9a5f2d8f0195de9bc686fca23"
    ),
    "sp_consume_open_intelligence_source_snapshot_v1.sql": (
        "9ba164f372941a73fcf9a5788c2a46a41619e116ba53aa41c95949c2836901bc"
    ),
    "sp_disable_open_intelligence_execution_approval_v1.sql": (
        "26d5080ab106b6e2be29cc2fbaef113901baf7cd1c49b7f5eab3935fbdd22ef5"
    ),
    "sp_import_open_intelligence_execution_bootstrap_v1.sql": (
        "93a507b8e1f442440bdacae9a9374fbe29e04c0efea444edb55bb4e1f58b1c37"
    ),
    "sp_read_open_intelligence_execution_approval_v1.sql": (
        "2d138a93c972569f653419db888c41379534136bc7e50590bd62ea09dc3fbbab"
    ),
    "sp_read_open_intelligence_execution_result_chain_v1.sql": (
        "f5570c9b25c635b7262b94188a3de6701aa476f9b3311f574331f107a4f3a7b6"
    ),
    "sp_read_open_intelligence_execution_result_v1.sql": (
        "022d70fb3bfc5e76febe729dc1df15603a464d0cb7a316b9db8fd8b6a12ebec0"
    ),
    "sp_read_open_intelligence_quality_review_receipt_v1.sql": (
        "a58d6a17ce36370c6bc3ce9ee8c7b595f63b4faad2b781078c4d3f782fe82e50"
    ),
    "sp_record_open_intelligence_execution_result_v1.sql": (
        "cf7e8a554e3204f94fb7bb12c939459e897d934830a2a08e48dea11d24dfd91a"
    ),
    "sp_register_open_intelligence_quality_review_receipt_v1.sql": (
        "c5323fd739fe554a3710fc3ddf3d73390f2f9b464d6d843454a3010d336b771d"
    ),
}
V1_ROUTINE_SPEC_NAMES = (
    "sp_approve_open_intelligence_execution_v1",
    "sp_read_open_intelligence_execution_approval_v1",
    "sp_read_open_intelligence_execution_result_v1",
    "sp_consume_open_intelligence_execution_v1",
    "sp_record_open_intelligence_execution_result_v1",
    "sp_read_open_intelligence_execution_result_chain_v1",
    "sp_import_open_intelligence_execution_bootstrap_v1",
    "sp_disable_open_intelligence_execution_approval_v1",
    "sp_consume_open_intelligence_source_snapshot_v1",
)
REGISTRATION_HASHES = {
    "execution_origins_v1.json": "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
    "resource_manifest.json": "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09",
    "successor-brain-read.json": "596920b6dd5d3d431120349af7b108ab3182337e008c449ebee1557ea69f2de4",
    "successor-migration-exposure.json": (
        "0fd42bbc35e9db6e935f6fb353f7956fb6d8c433999c563285a35d96c96a9a65"
    ),
    "successor-source-snapshot-capture.json": (
        "66e1a11f30c6dd8a7ca9db2110ef0af91d9a3aeda6b48d0b6323797839a06546"
    ),
    "successor-wave1-pilot.json": "edc805068e0297085926004b86fb1d7d30cc2158e07761439cdb6a6ed074c2d3",
}
# The active bridge generation and the one policy it names that amendment e does not.
# Amendment g added the route A capture's artifact bucket to the bridge manifest, so its
# digest moved from 1643e4ce to ab7802f4; the registry stays 573deeea.
# The daily contract revision then registered three daily operations
# and bounded exposure, so the pair moved again: registry 573deeea to e6b95e35, manifest
# ab7802f4 to b7553041.
# Tightening the date regex to ASCII digits then moved them to f23001ed and 592ed45f.
BRIDGE_REGISTRATION_HASHES = {
    "execution_origins_bridge_v3.json": (
        "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2"
    ),
    "resource_manifest_bridge_v3.json": (
        "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe"
    ),
    "source-bridge-capture-v3.json": (
        "b007a53067e2e4e841421c146eb96bedc3f269b413b253db0d21c0ebd16ce226"
    ),
}
REGISTRATION_ORDER = (
    "execution_origins_v1.json",
    "resource_manifest.json",
    "execution_origins_bridge_v3.json",
    "resource_manifest_bridge_v3.json",
    "successor-brain-read.json",
    "successor-migration-exposure.json",
    "successor-source-snapshot-capture.json",
    "successor-wave1-pilot.json",
    "source-bridge-capture-v3.json",
)
V2_TABLES = (
    "open_intelligence_execution_approvals_v2",
    "open_intelligence_execution_consumptions_v2",
    "open_intelligence_execution_results_v2",
    "open_intelligence_execution_approval_lock_v2",
    "open_intelligence_execution_origin_registries_v1",
    "open_intelligence_execution_resource_manifests_v1",
    "open_intelligence_execution_origin_policies_v1",
    "open_intelligence_execution_active_generation_v1",
    "open_intelligence_execution_derivations_v1",
    "open_intelligence_execution_derivation_tombstones_v1",
)

V1_LOCK_DISABLED = (
    "`{project}.{dataset}.open_intelligence_execution_approval_lock_v1` "
    "WHERE lock_name = 'open_intelligence_execution_approval_v1' "
    "AND approval_contract_version = 'open_intelligence_execution_approval_v1' "
    "AND state = 'disabled') = 1"
)
V2_LOCK_READY = (
    "`{project}.{dataset}.open_intelligence_execution_approval_lock_v2` "
    "WHERE lock_name = 'open_intelligence_execution_approval_v2' "
    "AND approval_contract_version = 'open_intelligence_execution_approval_v2' "
    "AND state = 'ready') = 1"
)
V2_LOCK_PREPARED = V2_LOCK_READY.replace("'ready'", "'prepared'")
ACTIVE_PAIR = (
    "`{project}.{dataset}.open_intelligence_execution_active_generation_v1` "
    "WHERE origin_registry_sha256 = v_origin_registry_sha256 "
    "AND resource_manifest_sha256 = v_resource_manifest_sha256) = 1"
)
POLICY_ROW = (
    "`{project}.{dataset}.open_intelligence_execution_origin_policies_v1` "
    "WHERE contract_sha256 = v_contract_sha256) = 1"
)
APPROVAL_UNION = (
    "SELECT approval_id, manifest_sha256 FROM "
    "`{project}.{dataset}.open_intelligence_execution_approvals_v1`\n"
    "      UNION ALL SELECT approval_id, manifest_sha256 FROM "
    "`{project}.{dataset}.open_intelligence_execution_approvals_v2`"
)
CONSUMPTION_UNION = (
    "SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM "
    "`{project}.{dataset}.open_intelligence_execution_consumptions_v1`\n"
    "      UNION ALL SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM "
    "`{project}.{dataset}.open_intelligence_execution_consumptions_v2`"
)
RESULT_UNION = (
    "SELECT consumption_id, result_id, result_reference FROM "
    "`{project}.{dataset}.open_intelligence_execution_results_v1`\n"
    "      UNION ALL SELECT consumption_id, result_id, result_reference FROM "
    "`{project}.{dataset}.open_intelligence_execution_results_v2`"
)
V2_APPROVAL_PHRASE = (
    "I approve one 42 staging execution of %s for manifest SHA256 %s, "
    "origin registry SHA256 %s and resource manifest SHA256 %s. Production remains unchanged."
)
V2_DISABLE_PHRASE = "I disable new 42 v2 staging execution approvals. Production remains unchanged."
APPROVAL_PREIMAGE = (
    '{"approval_contract_version":"open_intelligence_execution_approval_v2",'
    '"approved_at":"%s","approved_by":"%s","manifest_sha256":"%s",'
    '"origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}'
)
CONSUMPTION_PREIMAGE = (
    '{"approval_id":"%s","consumed_at":"%s",'
    '"consumption_contract_version":"open_intelligence_execution_consumption_v2",'
    '"execution_name":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}'
)
RESULT_PREIMAGE = (
    '{"completed_at":"%s","consumption_id":"%s","origin_registry_sha256":"%s",'
    '"resource_manifest_sha256":"%s",'
    '"result_contract_version":"open_intelligence_execution_result_v2",'
    '"result_digest":"%s","result_reference":"%s","status":"%s"}'
)
PREIMAGE_ARGUMENTS = {
    "sp_approve_open_intelligence_execution_v2": (
        "FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_approved_at), v_actor, v_manifest_sha256,\n"
        "    v_origin_registry_sha256, v_resource_manifest_sha256"
    ),
    "sp_consume_open_intelligence_execution_v2": (
        "v_approval.approval_id, FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_consumed_at),\n"
        "    v_execution_name, v_origin_registry_sha256, v_resource_manifest_sha256"
    ),
    "sp_record_open_intelligence_execution_result_v2": (
        "FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_completed_at), v_consumption_id,\n"
        "    v_origin_registry_sha256, v_resource_manifest_sha256,\n"
        "    v_result_digest, v_result_reference, v_status"
    ),
}

# Every refusal vector names the SQL branch that must refuse it and the code it raises.
REFUSE_VECTORS = (
    (
        "wrong pair",
        "sp_approve_open_intelligence_execution_v2",
        ACTIVE_PAIR,
        "execution_approval_generation_inactive",
    ),
    (
        "wrong pair",
        "sp_consume_open_intelligence_execution_v2",
        ACTIVE_PAIR,
        "execution_approval_generation_inactive",
    ),
    (
        "wrong pair",
        "sp_record_open_intelligence_execution_result_v2",
        ACTIVE_PAIR,
        "execution_approval_generation_inactive",
    ),
    (
        "unknown policy",
        "sp_approve_open_intelligence_execution_v2",
        POLICY_ROW,
        "execution_origin_policy_invalid",
    ),
    (
        "unknown policy",
        "sp_consume_open_intelligence_execution_v2",
        POLICY_ROW,
        "execution_origin_policy_invalid",
    ),
    (
        "unknown policy",
        "sp_record_open_intelligence_execution_result_v2",
        POLICY_ROW,
        "execution_origin_policy_invalid",
    ),
    (
        "unknown policy",
        "sp_read_open_intelligence_execution_approval_v2",
        POLICY_ROW,
        "execution_origin_policy_invalid",
    ),
    (
        "v1 lock not disabled",
        "sp_approve_open_intelligence_execution_v2",
        V1_LOCK_DISABLED,
        "execution_approval_v1_lock_not_disabled",
    ),
    (
        "v1 lock not disabled",
        "sp_consume_open_intelligence_execution_v2",
        V1_LOCK_DISABLED,
        "execution_approval_v1_lock_not_disabled",
    ),
    (
        "v1 lock not disabled",
        "sp_record_open_intelligence_execution_result_v2",
        V1_LOCK_DISABLED,
        "execution_approval_v1_lock_not_disabled",
    ),
    (
        "v1 lock not disabled",
        CUTOVER_SCRIPT,
        V1_LOCK_DISABLED,
        "execution_approval_v1_lock_not_disabled",
    ),
    (
        "v2 lock prepared",
        "sp_approve_open_intelligence_execution_v2",
        V2_LOCK_READY,
        "execution_approval_lock_not_ready",
    ),
    (
        "v2 lock prepared",
        "sp_consume_open_intelligence_execution_v2",
        V2_LOCK_READY,
        "execution_approval_lock_not_ready",
    ),
    (
        "v2 lock prepared",
        "sp_record_open_intelligence_execution_result_v2",
        V2_LOCK_READY,
        "execution_approval_lock_not_ready",
    ),
    (
        "v2 lock already ready",
        CUTOVER_SCRIPT,
        V2_LOCK_PREPARED,
        "execution_approval_lock_not_prepared",
    ),
    (
        "active row already exists",
        CUTOVER_SCRIPT,
        "`{project}.{dataset}.open_intelligence_execution_active_generation_v1`) = 0",
        "execution_approval_generation_already_active",
    ),
    (
        "duplicate approval id",
        "sp_approve_open_intelligence_execution_v2",
        APPROVAL_UNION,
        "execution_approval_conflict",
    ),
    (
        "duplicate consumption or execution",
        "sp_consume_open_intelligence_execution_v2",
        CONSUMPTION_UNION,
        "execution_approval_consumed",
    ),
    (
        "duplicate result id or reference",
        "sp_record_open_intelligence_execution_result_v2",
        RESULT_UNION,
        "execution_approval_conflict",
    ),
    (
        "cross-generation reuse",
        "sp_consume_open_intelligence_execution_v2",
        "v_approval.origin_registry_sha256 = v_origin_registry_sha256\n    AND v_approval.resource_manifest_sha256 = v_resource_manifest_sha256",
        "execution_approval_generation_invalid",
    ),
    (
        "cross-generation reuse",
        "sp_record_open_intelligence_execution_result_v2",
        "v_consumption.origin_registry_sha256 = v_origin_registry_sha256\n    AND v_consumption.resource_manifest_sha256 = v_resource_manifest_sha256",
        "execution_approval_generation_invalid",
    ),
    (
        "wrong actor",
        "sp_approve_open_intelligence_execution_v2",
        f"ASSERT v_actor = '{APPROVED_BY}'",
        "execution_approval_identity_invalid",
    ),
    (
        "wrong actor",
        "sp_consume_open_intelligence_execution_v2",
        "ASSERT SESSION_USER() = JSON_VALUE(v_binding, '$.service_identity')",
        "execution_approval_identity_invalid",
    ),
    (
        "wrong actor",
        "sp_record_open_intelligence_execution_result_v2",
        "ASSERT SESSION_USER() = JSON_VALUE(v_binding, '$.service_identity')",
        "execution_approval_identity_invalid",
    ),
    (
        "wrong actor",
        "sp_read_open_intelligence_execution_approval_v2",
        "ASSERT SESSION_USER() = JSON_VALUE(v_binding, '$.service_identity')",
        "execution_approval_identity_invalid",
    ),
    (
        "wrong actor",
        "sp_disable_open_intelligence_execution_approval_v2",
        f"ASSERT v_actor = '{APPROVED_BY}'",
        "execution_approval_identity_invalid",
    ),
    (
        "wrong actor",
        CUTOVER_SCRIPT,
        f"ASSERT v_actor = '{APPROVED_BY}'",
        "execution_approval_identity_invalid",
    ),
    (
        "expired horizon",
        "sp_approve_open_intelligence_execution_v2",
        "v_expires_at <= TIMESTAMP_ADD(v_approved_at, INTERVAL 24 HOUR)",
        "execution_approval_expired",
    ),
    (
        "expired horizon",
        "sp_consume_open_intelligence_execution_v2",
        "ASSERT v_consumed_at < v_approval.expires_at",
        "execution_approval_expired",
    ),
    (
        "malformed JSON",
        "sp_approve_open_intelligence_execution_v2",
        f"{CANONICAL_CALL}v_canonical_manifest_json)",
        "execution_approval_manifest_invalid",
    ),
    (
        "malformed JSON",
        "sp_record_open_intelligence_execution_result_v2",
        f"{CANONICAL_CALL}v_canonical_result_json)",
        "execution_approval_manifest_invalid",
    ),
    (
        "non NFC key",
        "sp_approve_open_intelligence_execution_v2",
        f"{CANONICAL_CALL}v_canonical_manifest_json)",
        "execution_approval_manifest_invalid",
    ),
    (
        "wrong family record under v2",
        "sp_read_open_intelligence_execution_approval_v2",
        "approval_contract_version = 'open_intelligence_execution_approval_v2'",
        "execution_approval_unavailable",
    ),
    (
        "wrong family record under v2",
        "sp_approve_open_intelligence_execution_v2",
        "JSON_VALUE(v_canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'",
        "execution_approval_manifest_invalid",
    ),
    (
        "wrong phrase",
        "sp_approve_open_intelligence_execution_v2",
        "ASSERT v_approval_phrase = FORMAT(",
        "execution_approval_manifest_mismatch",
    ),
    (
        "wrong phrase",
        "sp_disable_open_intelligence_execution_approval_v2",
        f"ASSERT approval_phrase = '{V2_DISABLE_PHRASE}'",
        "execution_approval_manifest_mismatch",
    ),
    (
        "residual mismatch",
        CUTOVER_SCRIPT,
        "LOWER(TO_HEX(SHA256(v_residual_json))) = v_residual_set_sha256",
        "execution_approval_residual_invalid",
    ),
    (
        "unconsumed unexpired v1 approval",
        CUTOVER_SCRIPT,
        "a.expires_at > v_activated_at",
        "execution_approval_residual_invalid",
    ),
    (
        "duplicate registry binding",
        "sp_read_open_intelligence_execution_result_chain_v2",
        "'$.exact_operation_bindings.r3_release.service_identity') IS NOT NULL) <= 1",
        "execution_origin_binding_ambiguous",
    ),
    (
        "second invalid chain for the run id",
        "sp_read_open_intelligence_execution_result_chain_v2",
        "AND JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id\n"
        "  ) = 1 AS 'execution_result_chain_ambiguous'",
        "execution_result_chain_ambiguous",
    ),
)

# Accept vectors: every selected operation resolves its binding and policy rule by literal path,
# and every rule kind present in the four reviewed policies has a named SQL branch.
ACCEPT_VECTORS = tuple(
    (
        operation,
        f"'$.exact_operation_bindings.{operation}'",
        f"'$.operation_validation.{operation}'",
    )
    for operation in SELECTED_OPERATIONS
)
RULE_KINDS = ("exact", "r3_execution_reference", "brain_read_v1")
CAPTURE_RULE_KIND = "source_snapshot_capture_v2"


def _sql(name: str) -> str:
    return (ROUTINES / f"{name}.sql").read_text(encoding="utf-8")


def _header(sql: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    match = re.match(
        r"CREATE OR REPLACE PROCEDURE `\{project\}\.\{dataset\}\.(?P<name>[a-z0-9_]+)`\("
        r"(?P<parameters>[^)]*)\)\n(?:[a-z_]+: )?BEGIN\n",
        sql,
    )
    assert match is not None, "routine header is not the v1 procedure shape"
    parameters = tuple(
        tuple(item.split()) for item in match.group("parameters").replace("\n", " ").split(",")
    )
    return match.group("name"), tuple((name, kind) for name, kind in parameters)


def _preimage(sql: str, prefix: str) -> str:
    match = re.search(
        r"CONCAT\('"
        + prefix
        + r"', LOWER\(TO_HEX\(SHA256\(FORMAT\(\n\s*'(?P<template>\{[^']*\})',",
        sql,
    )
    assert match is not None, f"missing {prefix} preimage FORMAT template"
    return match.group("template")


def _projection(sql: str, marker: str) -> str:
    start = sql.rindex(marker)
    return sql[start : sql.index("\n  FROM ", start)]


def _top_level_commas(text: str) -> int:
    depth = 0
    count = 0
    for character in text:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            count += 1
    return count


def _proposal_resource_manifest() -> Path:
    """The reviewed resource manifest lives outside the checkout; skip without it."""

    proposal_dir_value = os.environ.get("R03_CANONICAL_PROPOSAL_DIR")
    if proposal_dir_value is None:
        pytest.skip(PROPOSAL_SKIP_REASON)
    return Path(proposal_dir_value) / "resource_manifest.json"


def _routine_bodies() -> dict[str, str]:
    bodies = {name: _sql(name) for name in V2_ROUTINE_PARAMETERS}
    bodies[CUTOVER_SCRIPT] = _sql(CUTOVER_SCRIPT)
    return bodies


def _policy(name: str) -> dict:
    return json.loads(
        (ROOT / "configs" / "open_intelligence" / "origin_contracts" / name).read_text(
            encoding="utf-8"
        )
    )


# (a) routine files, headers and parameter lists


@pytest.mark.parametrize("name,parameters", tuple(DEPLOYED_PARAMETERS.items()))
def test_v2_routine_files_declare_exact_headers_and_parameters(name, parameters):
    path = ROUTINES / f"{name}.sql"
    assert path.is_file(), f"missing routine file {path.name}"
    sql = path.read_text(encoding="utf-8")
    header_name, header_parameters = _header(sql)
    assert header_name == name
    assert header_parameters == parameters
    assert sql.rstrip().endswith("END;")
    body = migration._routine_body(sql)
    # BigQuery rejects a labelled block as a procedure body (apply-v2 failed on
    # "main: BEGIN" with Unexpected identifier "main"), so every body is a plain
    # BEGIN and the seven early-exit routines leave it with RETURN.
    assert body.startswith("BEGIN\n")
    assert "main:" not in body
    assert "LEAVE" not in body
    assert ("RETURN;" in body) == (name in EARLY_RETURN_ROUTINES)
    if name in EARLY_RETURN_ROUTINES:
        # One early exit, and it sits as the last statement of its IF block: a RETURN
        # moved after END IF would leave the procedure on every call.
        lines = body.splitlines()
        assert body.count("RETURN;") == 1
        assert lines[lines.index("    RETURN;") + 1] == "  END IF;"
    # The two daily readers gate nothing on the caller; every other routine does.
    assert ("SESSION_USER()" in sql) == (name not in CALLER_UNGATED_READERS)


_ASSERT_STATEMENT = re.compile(r"^\s*ASSERT\b(?P<body>.*?);[ \t]*$", re.MULTILINE | re.DOTALL)
_ASSERT_LITERAL = re.compile(r"\sAS '[a-z0-9_]+'$")


def test_no_routine_file_builds_an_assert_message_with_an_expression():
    # BigQuery takes only a string literal after AS in an ASSERT; the apply-v2 dry run
    # at c6f42e6 refused "AS IF(...)" with Expected string literal but got keyword IF.
    for path in sorted(ROUTINES.glob("*.sql")):
        assert "AS IF(" not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("name", DEPLOYED_ROUTINES)
def test_every_assert_in_the_deployed_routines_ends_in_a_quoted_literal(name):
    sql = (ROUTINES / f"{name}.sql").read_text(encoding="utf-8")
    statements = _ASSERT_STATEMENT.findall(sql)
    assert len(statements) == len(re.findall(r"^\s*ASSERT\b", sql, re.MULTILINE))
    assert (len(statements) == 0) == (name == UDF)
    for body in statements:
        assert _ASSERT_LITERAL.search(body), (name, body.strip()[-80:])


def test_routine_body_keeps_an_optional_label_and_parses_a_plain_body_as_before():
    head = "CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_x`(a STRING)\n"
    plain = head + "BEGIN\n  SELECT 1;\nEND;\n"
    assert migration._routine_body(plain) == "BEGIN\n  SELECT 1;\nEND"
    labelled = head + "main: BEGIN\n  IF a IS NULL THEN LEAVE main; END IF;\n  SELECT 1;\nEND;\n"
    assert migration._routine_body(labelled) == (
        "main: BEGIN\n  IF a IS NULL THEN LEAVE main; END IF;\n  SELECT 1;\nEND"
    )
    # A label inside the body never becomes the marker, and no marker still refuses.
    nested = head + "BEGIN\n  loop_a: BEGIN\n  END;\nEND;\n"
    assert migration._routine_body(nested) == "BEGIN\n  loop_a: BEGIN\n  END;\nEND"
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_approval_schema_mismatch$"):
        migration._routine_body(head.rstrip("\n") + " SELECT 1;")
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_approval_schema_mismatch$"):
        migration._routine_body(head + "Main: BEGIN\n  SELECT 1;\nEND;\n")


def test_v2_iam_map_is_deduplicated_and_gives_the_daily_principal_the_daily_routines():
    routines = tuple(
        migration.RoutinePlan(
            name=name,
            path=ROUTINES / f"{name}.sql",
            sha256="0" * 64,
            sql=_sql(name),
            authorized_role=role,
            parameters=parameters,
        )
        for name, role, parameters in migration.V2_ROUTINE_SPECS
    )
    iam = migration._build_v2_iam_plan(routines)
    for group in (
        iam.principal_routine_bindings,
        iam.required_existing_job_user_bindings,
        iam.run_viewer_bindings,
        iam.cloudbuild_viewer_bindings,
    ):
        keys = [(item.principal, item.resource, item.role) for item in group]
        assert len(keys) == len(set(keys)), keys
    daily = f"serviceAccount:{CASES['source_snapshot_capture']['identity']}"
    for operation in ("collection_exposure_issue", "r3_apply", "r3_proof_issue", "r3_release"):
        assert f"serviceAccount:{CASES[operation]['identity']}" == daily
    by_principal: dict[str, set[str]] = {}
    for binding in iam.principal_routine_bindings:
        by_principal.setdefault(binding.principal, set()).add(binding.resource.rsplit("/", 1)[1])
    assert set(DAILY_ROUTINES) <= by_principal[daily]
    assert "sp_consume_open_intelligence_execution_v2" in by_principal[daily]
    assert SOURCE_SNAPSHOT_ROUTINE in by_principal[daily]
    assert "sp_read_open_intelligence_execution_result_chain_v2" in by_principal[daily]
    human = "user:albert.meintjes@ogilvy.co.za"
    for principal, names in by_principal.items():
        if principal != daily:
            assert not set(DAILY_ROUTINES) & names, principal
        if principal != human:
            assert not set(RECURRING_GRANT_ROUTINES) & names, principal
    assert set(RECURRING_GRANT_ROUTINES) <= by_principal[human]
    assert tuple(item.role for item in iam.routine_authorizations) == tuple(
        "roles/bigquery.routineDataViewer"
        if name in (UDF, *READERS, *AMENDMENT_D_READERS)
        else "roles/bigquery.routineDataEditor"
        for name in DEPLOYED_ROUTINES
    )


def test_distinct_bindings_keys_on_the_role_as_well_as_principal_and_resource():
    # Two rows equal in principal and resource but differing in role are two grants,
    # so both survive; only a row equal in all three collapses, and the first stays.
    routine = "projects/p/datasets/d/routines/sp_x"
    viewer = migration.IamBinding(
        "serviceAccount:a@p.iam", routine, "roles/bigquery.dataViewer", "read"
    )
    editor = migration.IamBinding(
        "serviceAccount:a@p.iam", routine, "roles/bigquery.dataEditor", "write"
    )
    repeat = migration.IamBinding(
        "serviceAccount:a@p.iam", routine, "roles/bigquery.dataViewer", "again"
    )
    other = migration.IamBinding(
        "serviceAccount:b@p.iam", routine, "roles/bigquery.dataViewer", "read"
    )
    kept = migration._distinct_bindings((viewer, editor, repeat, other))
    assert kept == (viewer, editor, other)
    assert kept[0].purpose == "read"
    assert migration._distinct_bindings(()) == ()


def test_v1_routine_files_are_byte_identical_to_head():
    daily_routines = {
        "sp_cancel_open_intelligence_daily_derivation_v1.sql",
        "sp_consume_open_intelligence_daily_derivation_v1.sql",
        "sp_derive_open_intelligence_daily_execution_v1.sql",
        "sp_read_open_intelligence_daily_chain_v1.sql",
        "sp_read_open_intelligence_daily_derivation_v1.sql",
        "sp_reconcile_open_intelligence_daily_consumption_v1.sql",
        "sp_record_open_intelligence_daily_result_v1.sql",
        "sp_select_open_intelligence_daily_derivation_v1.sql",
    }
    assert {path.name for path in ROUTINES.glob("*_v1.sql")} == (
        set(V1_ROUTINE_SHA256) | daily_routines
    )
    actual = {
        name: hashlib.sha256((ROUTINES / name).read_bytes()).hexdigest()
        for name in V1_ROUTINE_SHA256
    }
    assert actual == V1_ROUTINE_SHA256
    assert tuple(name for name, *_ in migration.ROUTINE_SPECS) == V1_ROUTINE_SPEC_NAMES
    assert tuple(item.name for item in migration.build_plan().routines) == V1_ROUTINE_SPEC_NAMES
    assert migration.build_plan().lock_seed["state"] == "ready"


def test_no_v2_routine_hardcodes_legacy_jobs_or_excluded_operations():
    # The grant bound capture routine is dedicated to its own operation and names it
    # in its operation gate, and the v3 approve and read successors carry it in their
    # operation CASEs; no other v2 routine may carry an excluded operation.
    dedicated = {
        SOURCE_SNAPSHOT_ROUTINE: {"source_snapshot_capture"},
        "sp_approve_open_intelligence_execution_v3": {"source_snapshot_capture"},
        "sp_read_open_intelligence_execution_approval_v3": {"source_snapshot_capture"},
    }
    for name, sql in _routine_bodies().items():
        assert "trends-engine-oi-" not in sql, name
        assert "trends-engine-staging@" not in sql, name
        for operation in EXCLUDED_OPERATIONS:
            if operation in dedicated.get(name, ()):
                continue
            assert operation not in sql, (name, operation)
    v1_approve = _sql("sp_approve_open_intelligence_execution_v1")
    assert "intelligence-42" not in v1_approve
    assert "trends-engine-oi-apply-staging" in v1_approve


def test_daily_consume_routine_references_only_the_store_it_lives_in():
    # The daily job runs under an identity that is authorized on the approvals
    # dataset alone, and the delta validator refuses a routine authorization on
    # any other dataset, so every qualified name the consume routine reads or
    # writes must be a store table or a store routine. The funded terminal
    # events table lives elsewhere and the daily job never writes it.
    rendered = migration._render_sql(_sql("sp_consume_open_intelligence_daily_derivation_v1"))
    assert "socialcrawl_funded_terminal_events_v1" not in rendered
    assert re.findall(r"\{[a-z_]+\}", rendered) == []
    plan = migration.build_v2_plan()
    store = (
        set(migration.TABLE_NAMES)
        | {item.name for item in plan.tables}
        | {item.name for item in plan.routines}
    )
    assert {item.name for item in plan.tables} == set(migration.V2_TABLE_NAMES)
    references = re.findall(r"`([^`]+)`", rendered)
    assert references
    prefix = f"{migration.PROJECT}.{migration.DATASET}."
    for reference in references:
        assert reference.startswith(prefix), reference
        assert reference.removeprefix(prefix) in store, reference


# (b) gates in every mutating routine


@pytest.mark.parametrize("name", MUTATING)
def test_mutating_routines_gate_on_v1_disabled_v2_ready_active_pair_and_registration(name):
    sql = _sql(name)
    transaction = sql.index("BEGIN TRANSACTION")
    assert transaction < sql.index(V1_LOCK_DISABLED)
    assert sql.index(V1_LOCK_DISABLED) < sql.index(V2_LOCK_READY)
    assert sql.index(V2_LOCK_READY) < sql.index("state = 'ready' AND lock_version = v_lock_version")
    assert "ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict'" in sql
    assert sql.count("approval_contract_version = 'open_intelligence_execution_approval_v2'") >= 3
    assert ACTIVE_PAIR in sql
    assert (
        "`{project}.{dataset}.open_intelligence_execution_origin_registries_v1` "
        "WHERE origin_registry_sha256 = v_origin_registry_sha256) = 1"
    ) in sql
    assert (
        "`{project}.{dataset}.open_intelligence_execution_resource_manifests_v1` "
        "WHERE resource_manifest_sha256 = v_resource_manifest_sha256 "
        "AND origin_registry_sha256 = v_origin_registry_sha256) = 1"
    ) in sql
    assert "LOWER(TO_HEX(SHA256(v_registry_json))) = v_origin_registry_sha256" in sql
    assert "LOWER(TO_HEX(SHA256(v_resource_json))) = v_resource_manifest_sha256" in sql
    assert (
        "JSON_VALUE(v_resource_json, '$.origin_registry_sha256') = v_origin_registry_sha256" in sql
    )
    assert POLICY_ROW in sql
    assert "LOWER(TO_HEX(SHA256(v_policy_json))) = v_contract_sha256" in sql
    for variable in ("v_registry_json", "v_resource_json", "v_policy_json"):
        assert f"{CANONICAL_CALL}{variable})" in sql, (name, variable)
        assert f"SAFE.PARSE_JSON({variable}, {EXACT_PARSE}) IS NOT NULL" in sql, (name, variable)
    assert "COMMIT TRANSACTION" in sql
    assert "open_intelligence_execution_approval_lock_v1`\n  SET" not in sql
    assert "UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`" not in sql


@pytest.mark.parametrize("name", ["sp_approve_open_intelligence_execution_v2"])
def test_approve_v2_requires_canonical_predicate_and_exact_parse_on_manifest(name):
    sql = _sql(name)
    assert f"{CANONICAL_CALL}v_canonical_manifest_json)" in sql
    assert f"SAFE.PARSE_JSON(v_canonical_manifest_json, {EXACT_PARSE})" in sql
    assert "ARRAY_LENGTH(v_top_level_keys) = 20" in sql
    assert "'$.common_manifest_validation.top_level_fields'" in sql
    for kind in RULE_KINDS:
        assert f"WHEN '{kind}' THEN" in sql, kind
    assert "ELSE FALSE" in sql
    for path in (
        "'$.common_manifest_validation.project'",
        "'$.common_manifest_validation.location'",
        "'$.common_manifest_validation.source_sha_regex'",
        "'$.common_manifest_validation.build_resource_regex'",
        "'$.common_manifest_validation.expires_at_regex'",
        "'$.common_manifest_validation.max_retries.exact_integer'",
        "'$.common_manifest_validation.timeout_seconds.integer_minimum'",
        "'$.common_manifest_validation.timeout_seconds.integer_maximum'",
        "'$.input_artifact_names'",
        "'$.input_artifact_digest_regex'",
        "'$.arguments.kind'",
        "'$.arguments.value'",
        "'$.arguments.prefix'",
        "'$.arguments.execution_resource_regex'",
        "'$.arguments.lengths'",
        "'$.arguments.run_id_regex'",
        "'$.arguments.signal_switch'",
        "'$.arguments.signal_id_regex'",
        "'$.arguments.depth_switch'",
        "'$.arguments.depths'",
        "'$.arguments.decision_required_for'",
        "'$.arguments.decision_switch'",
        "'$.arguments.decision_max_characters'",
        "'$.arguments.decision_control_characters_forbidden'",
        "'$.image_uri_regex'",
        "'$.origin.image_uri_regex'",
        "'$.origin.connected_repository'",
        "'$.successor.operations'",
        "'$.successor.manifest_version'",
    ):
        assert path in sql, path
    for field in ("max_bytes_billed", "max_credits", "max_model_calls", "max_rows_written"):
        for constraint in ("exact", "minimum", "maximum"):
            assert f"'$.limits.{field}.{constraint}'" in sql, (field, constraint)


def test_write_result_v2_requires_canonical_predicate_and_exact_parse_on_result():
    sql = _sql("sp_record_open_intelligence_execution_result_v2")
    assert f"{CANONICAL_CALL}v_canonical_result_json)" in sql
    assert f"JSON_TYPE(SAFE.PARSE_JSON(v_canonical_result_json, {EXACT_PARSE})) = 'object'" in sql
    assert "ASSERT v_status IN ('succeeded', 'failed')" in sql
    assert "ASSERT v_result_digest = LOWER(TO_HEX(SHA256(v_canonical_result_json)))" in sql


@pytest.mark.parametrize("operation,binding_path,rule_path", ACCEPT_VECTORS)
def test_accept_vectors_resolve_binding_and_rule_by_literal_path(
    operation, binding_path, rule_path
):
    registry = load_registry()
    execution_approval.validate_execution_manifest(
        manifest(operation), mode="new_approval", registry=registry
    )
    for name in (*MUTATING, *READERS):
        assert binding_path in _sql(name), (name, operation)
    assert rule_path in _sql("sp_approve_open_intelligence_execution_v2")
    assert rule_path in _sql("sp_read_open_intelligence_execution_approval_v2")


def test_rule_kinds_in_reviewed_policies_are_exactly_the_named_sql_branches():
    """Three kinds are branches of the generic approve routine. The fourth, the capture
    vector, is read positionally by the dedicated capture consume routine, and the generic
    reviewed v2 approve routine has no branch for it and stays byte identical; the v3
    successor carries the operation in both CASEs and the kind branch, so the gap is closed
    there and recorded here. Since amendment d the daily contract carries the folded
    exposure and r3 rules beside the capture vector, so it holds three kinds and only its
    capture rule is the positional one."""
    kinds = set()
    for name in (
        "successor-brain-read.json",
        "successor-migration-exposure.json",
        "successor-wave1-pilot.json",
    ):
        for rule in _policy(name)["operation_validation"].values():
            kinds.add(rule["arguments"]["kind"])
    daily = _policy("successor-source-snapshot-capture.json")["operation_validation"]
    assert daily["source_snapshot_capture"]["arguments"]["kind"] == CAPTURE_RULE_KIND
    for operation, rule in daily.items():
        if operation != "source_snapshot_capture":
            kinds.add(rule["arguments"]["kind"])
    assert kinds == set(RULE_KINDS)
    assert {rule["arguments"]["kind"] for rule in daily.values()} == {
        "exact",
        "r3_execution_reference",
        CAPTURE_RULE_KIND,
    }
    approve = _sql("sp_approve_open_intelligence_execution_v2")
    assert f"WHEN '{CAPTURE_RULE_KIND}' THEN" not in approve
    assert "WHEN 'source_snapshot_capture' THEN" not in approve
    successor = _sql("sp_approve_open_intelligence_execution_v3")
    assert successor.count(f"WHEN '{CAPTURE_RULE_KIND}' THEN") == 1
    assert successor.count("WHEN 'source_snapshot_capture' THEN") == 2
    assert "WHEN 'source_snapshot_capture' THEN" in _sql(
        "sp_read_open_intelligence_execution_approval_v3"
    )
    consume = _sql("sp_consume_open_intelligence_source_snapshot_v2")
    assert (
        "ARRAY_LENGTH(JSON_VALUE_ARRAY(v_approval.canonical_manifest_json, '$.arguments')) = 7"
        in consume
    )
    for position in (2, 4, 6):
        assert f"'$.arguments[{position}]'" in consume


@pytest.mark.parametrize("vector,name,branch,code", REFUSE_VECTORS)
def test_refuse_vectors_name_an_existing_sql_branch_and_code(vector, name, branch, code):
    sql = _sql(name)
    assert branch in sql, (vector, name)
    assert f"AS '{code}'" in sql, (vector, name, code)


# Python to SQL accept and refuse vector parity for the v2 origin selection.
#
# The approve routine selects one origin row by the authenticated pair, then binds the
# operation, the target and the image to that row. Each gate below names the routine
# predicate that carries one rule and the code that predicate raises. The SQL side of a
# vector is replayed from the routine text and from the same registry and policy bytes the
# Python side loads, so a routine that stopped asserting a rule would stop agreeing here.
PARITY_MODE = "new_approval"
PARITY_OPERATION = "migration_apply"
PARITY_ROUTINE = "sp_approve_open_intelligence_execution_v2"
SUCCESSOR_ROUTINE = "sp_approve_open_intelligence_execution_v3"
REGISTRY_PATH = ROOT / "configs" / "open_intelligence" / "execution_origins_v1.json"
# The active generation's registry, whose daily row links the bridge capture policy.
ACTIVE_REGISTRY_PATH = ROOT / "configs" / "open_intelligence" / "execution_origins_bridge_v3.json"
ORIGIN_GATES = (
    (
        "pair_shape",
        "JSON_VALUE(v_canonical_manifest_json, '$.manifest_version') = "
        "'open_intelligence_execution_manifest_v2'",
        "execution_approval_manifest_invalid",
    ),
    (
        "registered_pair",
        "JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256) = 1",
        "execution_origin_pair_invalid",
    ),
    (
        "allowed_mode",
        "'new_approval' IN UNNEST(JSON_VALUE_ARRAY(v_origin_row, '$.allowed_execution_modes'))",
        "execution_origin_pair_invalid",
    ),
    (
        "row_binding",
        "JSON_VALUE(v_binding, '$.job_resource') IN "
        "UNNEST(JSON_VALUE_ARRAY(v_origin_row, '$.job_resources'))",
        "execution_origin_pair_invalid",
    ),
    (
        "policy_origin",
        "JSON_VALUE(v_policy_json, '$.origin.connected_repository') = "
        "JSON_VALUE(v_origin_row, '$.connected_repo')",
        "execution_origin_policy_invalid",
    ),
    (
        # The same assertion carries the dataset equality, so both are modeled under this gate.
        "target_binding",
        "JSON_VALUE(v_canonical_manifest_json, '$.job_resource') = "
        "JSON_VALUE(v_rule, '$.job_resource')",
        "execution_approval_target_invalid",
    ),
    (
        "identity_binding",
        "JSON_VALUE(v_canonical_manifest_json, '$.service_identity') = "
        "JSON_VALUE(v_rule, '$.service_identity')",
        "execution_approval_identity_invalid",
    ),
    (
        "image_origin",
        "REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.image_uri'), "
        "JSON_VALUE(v_origin_row, '$.image_uri_regex'))",
        "execution_approval_target_invalid",
    ),
)
SELECTOR_REFUSALS = ("execution_origin_pair_invalid", "execution_origin_mode_forbidden")
# Without the uniqueness assertion the routine still refuses, but the scalar row selection
# carries no contract code, so the refusal is recorded here as unnamed.
UNNAMED_REFUSAL = "origin_row_not_selected"
IMAGE_DIGEST = "b" * 64
FOREIGN_REPOSITORY_IMAGE = (
    "us-central1-docker.pkg.dev/ogilvy-other/intelligence-42/engine@sha256:" + IMAGE_DIGEST
)
HISTORICAL_REPOSITORY_IMAGE = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:" + IMAGE_DIGEST
)
HISTORICAL_NAME_IN_NEW_REPOSITORY = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/trends-engine@sha256:"
    + IMAGE_DIGEST
)
NEW_NAME_IN_HISTORICAL_REPOSITORY = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/engine@sha256:" + IMAGE_DIGEST
)
PARITY_CONTRACT_SHA256 = CASES[PARITY_OPERATION]["contract"]
ALTERED_CONTRACT_SHA256 = PARITY_CONTRACT_SHA256[:-1] + (
    "1" if PARITY_CONTRACT_SHA256.endswith("0") else "0"
)
HISTORICAL_CONTRACT_SHA256 = next(
    row["contract_sha256"]
    for row in json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))["rows"]
    if row["manifest_version"] == MANIFEST_V1
)
SIBLING_OPERATION = "collection_exposure_issue"
SIBLING_JOB = (
    f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{CASES[SIBLING_OPERATION]['job']}"
)

# name, manifest overrides, routine code, origin selection code, approval code.
PARITY_VECTORS = (
    ("admitted origin", {}, None, None, None),
    (
        "unknown manifest version",
        {"manifest_version": "open_intelligence_execution_manifest_v3"},
        "execution_approval_manifest_invalid",
        "execution_origin_pair_invalid",
        "execution_origin_pair_invalid",
    ),
    (
        "fresh v1 approval",
        {
            "manifest_version": MANIFEST_V1,
            "contract_sha256": HISTORICAL_CONTRACT_SHA256,
        },
        "execution_approval_manifest_invalid",
        "execution_origin_mode_forbidden",
        "execution_origin_mode_forbidden",
    ),
    (
        "mixed tuple",
        {"contract_sha256": HISTORICAL_CONTRACT_SHA256},
        "execution_origin_pair_invalid",
        "execution_origin_pair_invalid",
        "execution_origin_pair_invalid",
    ),
    (
        "altered contract digest",
        {"contract_sha256": ALTERED_CONTRACT_SHA256},
        "execution_origin_pair_invalid",
        "execution_origin_pair_invalid",
        "execution_origin_pair_invalid",
    ),
    (
        "wrong repository",
        {"image_uri": FOREIGN_REPOSITORY_IMAGE},
        "execution_approval_target_invalid",
        "execution_origin_target_invalid",
        "execution_approval_target_invalid",
    ),
    (
        "right source sha in wrong repository",
        {"image_uri": HISTORICAL_REPOSITORY_IMAGE},
        "execution_approval_target_invalid",
        "execution_origin_target_invalid",
        "execution_approval_target_invalid",
    ),
    (
        "old image under new repository",
        {"image_uri": HISTORICAL_NAME_IN_NEW_REPOSITORY},
        "execution_approval_target_invalid",
        "execution_origin_target_invalid",
        "execution_approval_target_invalid",
    ),
    (
        "new image under old repository",
        {"image_uri": NEW_NAME_IN_HISTORICAL_REPOSITORY},
        "execution_approval_target_invalid",
        "execution_origin_target_invalid",
        "execution_approval_target_invalid",
    ),
    (
        "wrong job resource",
        {"job_resource": SIBLING_JOB},
        "execution_approval_target_invalid",
        "execution_origin_target_invalid",
        "execution_approval_target_invalid",
    ),
    (
        "wrong service identity",
        {"service_identity": CASES[SIBLING_OPERATION]["identity"]},
        "execution_approval_identity_invalid",
        "execution_origin_target_invalid",
        "execution_approval_identity_invalid",
    ),
    (
        "wrong datasets",
        {"datasets": ["trends_v2_staging_funded"]},
        "execution_approval_target_invalid",
        "execution_origin_target_invalid",
        "execution_approval_target_invalid",
    ),
)


def _registry_rows(path: Path = REGISTRY_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["rows"]


def _policies_by_file(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    return {
        row["contract_file"]: json.loads((ROOT / row["contract_file"]).read_text(encoding="utf-8"))
        for row in _registry_rows(path)
        if row["contract_file"] is not None
    }


def _parity_row(rows: list[dict]) -> dict:
    return next(
        row
        for row in rows
        if row["manifest_version"] == MANIFEST_V2
        and PARITY_OPERATION in row["exact_operation_bindings"]
    )


def _swap_connected_repository(rows: list[dict]) -> None:
    _parity_row(rows)["connected_repo"] = next(
        row["connected_repo"] for row in rows if row["manifest_version"] == MANIFEST_V1
    )


def _duplicate_registered_pair(rows: list[dict]) -> None:
    rows.append(deepcopy(_parity_row(rows)))


def _withdraw_new_approval_mode(rows: list[dict]) -> None:
    row = _parity_row(rows)
    row["allowed_execution_modes"] = [
        mode for mode in row["allowed_execution_modes"] if mode != PARITY_MODE
    ]


def _unlisted_binding_job(rows: list[dict]) -> None:
    row = _parity_row(rows)
    row["exact_operation_bindings"][PARITY_OPERATION]["job_resource"] = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-unlisted-staging"
    )


# name, registry mutation, routine code. Python refuses every one of these when the registry
# is loaded, so the vector never reaches a selection call.
REGISTRY_DEFECTS = (
    (
        "connected repository swapped to the historical connection",
        _swap_connected_repository,
        "execution_origin_policy_invalid",
    ),
    (
        "duplicate registered pair",
        _duplicate_registered_pair,
        "execution_origin_pair_invalid",
    ),
    (
        "new approval withdrawn from the registered row",
        _withdraw_new_approval_mode,
        "execution_origin_pair_invalid",
    ),
    (
        "binding job outside the row job list",
        _unlisted_binding_job,
        "execution_origin_pair_invalid",
    ),
)


def _assertions(sql: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (match.group("text"), match.group("code"))
        for match in re.finditer(r"ASSERT (?P<text>.*?) AS '(?P<code>[a-z0-9_]+)';", sql, re.DOTALL)
    )


def _gate_codes(sql: str) -> dict[str, str]:
    """Map each modeled gate to the code of the one assertion carrying its predicate.

    A gate whose predicate is absent from the routine is absent from the result, and the
    replay below then applies no rule for it.
    """

    assertions = _assertions(sql)
    codes = {}
    for gate, fragment, _ in ORIGIN_GATES:
        carriers = [code for text, code in assertions if fragment in text]
        if len(carriers) == 1:
            codes[gate] = carriers[0]
    return codes


def _without_assertion(sql: str, fragment: str) -> str:
    for match in re.finditer(r"^  ASSERT .*? AS '[a-z0-9_]+';\n", sql, re.DOTALL | re.MULTILINE):
        if fragment in match.group(0):
            return sql[: match.start()] + sql[match.end() :]
    raise AssertionError(fragment)


def _regexp_contains(pattern: str, value: str) -> bool:
    """Replay REGEXP_CONTAINS over an anchored pattern.

    Anchors bind to the whole text in the routine's regular expression dialect, so an
    anchored pattern admits exactly the whole value and refuses a trailing line break. A
    full match over the pattern body is the same decision.
    """

    assert pattern.startswith("^"), pattern
    assert pattern.endswith("$"), pattern
    return re.fullmatch(pattern[1:-1], value) is not None


def _sql_origin_decision(payload, *, rows, policies, codes) -> str | None:
    operation = payload.get("operation")
    contract = payload.get("contract_sha256")
    if "pair_shape" in codes and (
        payload.get("manifest_version") != MANIFEST_V2
        or not isinstance(contract, str)
        or re.fullmatch(r"[0-9a-f]{64}", contract) is None
        or operation is None
    ):
        return codes["pair_shape"]
    selected = [
        row
        for row in rows
        if row["manifest_version"] == MANIFEST_V2 and row["contract_sha256"] == contract
    ]
    if len(selected) != 1:
        return codes.get("registered_pair", UNNAMED_REFUSAL)
    row = selected[0]
    if "allowed_mode" in codes and PARITY_MODE not in row["allowed_execution_modes"]:
        return codes["allowed_mode"]
    binding = (
        row["exact_operation_bindings"].get(operation) if operation in SELECTED_OPERATIONS else None
    )
    if "row_binding" in codes and (
        binding is None
        or binding["job_resource"] not in row["job_resources"]
        or binding["service_identity"] not in row["service_identities"]
    ):
        return codes["row_binding"]
    policy = policies[row["contract_file"]]
    if "policy_origin" in codes and (
        policy["origin"]["connected_repository"] != row["connected_repo"]
        or policy["origin"]["image_uri_regex"] != row["image_uri_regex"]
    ):
        return codes["policy_origin"]
    rule = (
        policy["operation_validation"].get(operation) if operation in SELECTED_OPERATIONS else None
    )
    if rule is None:
        return UNNAMED_REFUSAL
    if "target_binding" in codes and (
        payload.get("datasets") != rule["datasets"]
        or payload.get("job_resource") != rule["job_resource"]
    ):
        return codes["target_binding"]
    if "identity_binding" in codes and payload.get("service_identity") != rule["service_identity"]:
        return codes["identity_binding"]
    if "image_origin" in codes and not _regexp_contains(
        row["image_uri_regex"], payload["image_uri"]
    ):
        return codes["image_origin"]
    return None


def _parity_payload(overrides: dict) -> dict:
    payload = manifest(PARITY_OPERATION)
    payload.update(overrides)
    return payload


def _sql_outcomes(codes: dict[str, str]) -> dict[str, str | None]:
    policies = _policies_by_file()
    rows = _registry_rows()
    outcomes = {
        name: _sql_origin_decision(
            _parity_payload(overrides), rows=rows, policies=policies, codes=codes
        )
        for name, overrides, _, _, _ in PARITY_VECTORS
    }
    for name, mutate, _ in REGISTRY_DEFECTS:
        defective = _registry_rows()
        mutate(defective)
        outcomes[name] = _sql_origin_decision(
            _parity_payload({}), rows=defective, policies=policies, codes=codes
        )
    return outcomes


def _python_refusal(call) -> str | None:
    try:
        call()
    except (execution_approval.ApprovalRefusal, execution_origins.OriginRefusal) as error:
        return str(error)
    return None


def _defective_registry(tmp_path, mutate):
    root = tmp_path / "engine"
    contracts = root / "configs" / "open_intelligence" / "origin_contracts"
    contracts.mkdir(parents=True)
    for policy in (ROOT / "configs" / "open_intelligence" / "origin_contracts").glob("*.json"):
        (contracts / policy.name).write_bytes(policy.read_bytes())
    path = root / "configs" / "open_intelligence" / "execution_origins_v1.json"
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    mutate(payload["rows"])
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, root


def test_modeled_origin_gates_are_unique_ordered_and_cover_the_origin_row_assertions():
    sql = _sql(PARITY_ROUTINE)
    assertions = _assertions(sql)
    modeled = []
    for gate, fragment, code in ORIGIN_GATES:
        carriers = [index for index, (text, _) in enumerate(assertions) if fragment in text]
        assert len(carriers) == 1, gate
        assert assertions[carriers[0]][1] == code, gate
        modeled.append(carriers[0])
    assert modeled == sorted(set(modeled))
    on_the_row = {index for index, (text, _) in enumerate(assertions) if "v_origin_row" in text}
    assert on_the_row <= set(modeled)
    names = [name for name, *_ in PARITY_VECTORS] + [name for name, *_ in REGISTRY_DEFECTS]
    assert len(names) == len(set(names))
    for row in _registry_rows():
        assert row["image_uri_regex"].startswith("^")
        assert row["image_uri_regex"].endswith("$")


@pytest.mark.parametrize("name,overrides,routine_code,origin_code,approval_code", PARITY_VECTORS)
def test_python_and_v2_sql_share_the_same_origin_accept_and_refuse_vectors(
    name, overrides, routine_code, origin_code, approval_code
):
    payload = _parity_payload(overrides)
    registry = load_registry()
    assert _sql_outcomes(_gate_codes(_sql(PARITY_ROUTINE)))[name] == routine_code
    assert (
        _python_refusal(lambda: execution_origins.resolve_origin(payload, PARITY_MODE, registry))
        == origin_code
    )
    assert (
        _python_refusal(
            lambda: execution_approval.validate_execution_manifest(
                payload, mode=PARITY_MODE, registry=registry
            )
        )
        == approval_code
    )
    selector = _python_refusal(
        lambda: execution_origins.select_origin(
            manifest_version=payload.get("manifest_version"),
            contract_sha256=payload.get("contract_sha256"),
            mode=PARITY_MODE,
            registry=registry,
        )
    )
    assert (selector is not None) is (origin_code in SELECTOR_REFUSALS)
    assert selector in (None, origin_code)
    assert len({routine_code is None, origin_code is None, approval_code is None}) == 1


@pytest.mark.parametrize("operation", SELECTED_OPERATIONS)
def test_every_selected_operation_is_an_accept_vector_on_both_sides(operation):
    payload = manifest(operation)
    registry = load_registry()
    assert registry.sha256 == hashlib.sha256(ACTIVE_REGISTRY_PATH.read_bytes()).hexdigest()
    assert (
        _sql_origin_decision(
            payload,
            rows=_registry_rows(ACTIVE_REGISTRY_PATH),
            policies=_policies_by_file(ACTIVE_REGISTRY_PATH),
            codes=_gate_codes(_sql(PARITY_ROUTINE)),
        )
        is None
    )
    origin = execution_origins.resolve_origin(payload, PARITY_MODE, registry)
    assert origin.manifest_version == MANIFEST_V2
    assert origin.operation_bindings[operation].job_resource == payload["job_resource"]
    admitted = execution_approval.validate_execution_manifest(
        payload, mode=PARITY_MODE, registry=registry
    )
    assert admitted.operation == operation


@pytest.mark.parametrize("name,mutate,routine_code", REGISTRY_DEFECTS)
def test_registry_origin_defects_refuse_in_python_and_in_the_v2_routine(
    tmp_path, name, mutate, routine_code
):
    path, root = _defective_registry(tmp_path, mutate)
    with pytest.raises(
        execution_origins.OriginRefusal, match=r"^execution_origin_registry_invalid$"
    ):
        load_registry(path, root=root)
    assert _sql_outcomes(_gate_codes(_sql(PARITY_ROUTINE)))[name] == routine_code


def test_each_modeled_origin_assertion_changes_a_parity_vector_when_it_is_removed():
    """Every modeled gate carries at least one vector on its own.

    Removing the assertion from the routine text has to change the replayed outcome of some
    vector, otherwise the vector set does not actually exercise that part of the routine.
    """

    sql = _sql(PARITY_ROUTINE)
    baseline = _sql_outcomes(_gate_codes(sql))
    for gate, fragment, _ in ORIGIN_GATES:
        weakened = _sql_outcomes(_gate_codes(_without_assertion(sql, fragment)))
        assert weakened != baseline, gate


def test_the_capture_operation_is_the_one_recorded_gap_against_the_generic_v2_routine():
    """Python admits the capture operation under every mode, and the generic v2 approve
    routine has no binding branch for it, so it refuses there. The dedicated capture consume
    routine and the v3 successor carry that operation instead."""

    payload = manifest("source_snapshot_capture")
    registry = load_registry()
    admitted = execution_approval.validate_execution_manifest(
        payload, mode=PARITY_MODE, registry=registry
    )
    assert admitted.operation == "source_snapshot_capture"
    assert execution_origins.resolve_origin(payload, PARITY_MODE, registry).manifest_version == (
        MANIFEST_V2
    )
    assert (
        _sql_origin_decision(
            payload,
            rows=_registry_rows(),
            policies=_policies_by_file(),
            codes=_gate_codes(_sql(PARITY_ROUTINE)),
        )
        == "execution_origin_pair_invalid"
    )
    assert "source_snapshot_capture" in EXCLUDED_OPERATIONS
    assert "WHEN 'source_snapshot_capture' THEN" not in _sql(PARITY_ROUTINE)
    assert "WHEN 'source_snapshot_capture' THEN" in _sql(SUCCESSOR_ROUTINE)


def test_new_error_codes_are_exactly_the_documented_set():
    v1_codes = set()
    for path in ROUTINES.glob("sp_*_v1.sql"):
        v1_codes.update(re.findall(r"AS '([a-z0-9_]+)'", path.read_text(encoding="utf-8")))
    from tests.unit import test_source_snapshot_routine_v2 as capture

    v2_codes = set()
    for sql in _routine_bodies().values():
        v2_codes.update(re.findall(r"AS '([a-z0-9_]+)'", sql))
    assert v2_codes - v1_codes - capture.NEW_CODES == {
        "execution_approval_v1_lock_not_disabled",
        "execution_approval_lock_not_ready",
        "execution_approval_lock_not_prepared",
        "execution_approval_generation_invalid",
        "execution_approval_generation_inactive",
        "execution_approval_generation_already_active",
        "execution_approval_residual_invalid",
        "execution_origin_registry_invalid",
        "execution_origin_registry_digest_mismatch",
        "execution_origin_pair_invalid",
        "execution_origin_policy_invalid",
        "execution_origin_binding_ambiguous",
        "execution_result_chain_ambiguous",
    }


# (c) readers


@pytest.mark.parametrize("name", READERS)
def test_readers_span_generations_without_lock_or_active_pair(name):
    sql = _sql(name)
    assert "BEGIN TRANSACTION" not in sql
    assert "open_intelligence_execution_approval_lock_v" not in sql
    assert "open_intelligence_execution_active_generation_v1" not in sql
    assert "UPDATE " not in sql
    assert "INSERT " not in sql
    assert "origin_registry_sha256" in sql
    assert "resource_manifest_sha256" in sql
    assert "`{project}.{dataset}.open_intelligence_execution_origin_registries_v1`" in sql
    assert "`{project}.{dataset}.open_intelligence_execution_resource_manifests_v1`" in sql
    assert POLICY_ROW in sql


def test_approval_reader_selects_unique_row_and_returns_v1_projection_plus_digests():
    sql = _sql("sp_read_open_intelligence_execution_approval_v2")
    assert "AS 'execution_approval_unavailable'" in sql
    assert "manifest_sha256 = v_manifest_sha256) = 1" in sql
    assert sql.rstrip().endswith(
        "SELECT * FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` "
        "AS open_intelligence_execution_approvals_v2\n"
        "  WHERE open_intelligence_execution_approvals_v2.approval_contract_version = "
        "'open_intelligence_execution_approval_v2'\n"
        "    AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256;\nEND;"
    )
    schema = migration._schema_from_sql(
        (SCHEMAS / "open_intelligence_execution_approvals_v2.sql").read_text(encoding="utf-8")
    )
    assert [field for field, *_ in schema][-2:] == [
        "origin_registry_sha256",
        "resource_manifest_sha256",
    ]
    assert "= v_approval.approval_id AS 'execution_approval_manifest_invalid'" in sql
    assert "approval_phrase_sha256 = LOWER(TO_HEX(SHA256(FORMAT(" in sql


def test_result_reader_keeps_allow_zero_reconciliation_and_appends_digests():
    sql = _sql("sp_read_open_intelligence_execution_result_v2")
    assert "ASSERT v_result_count <= 1 AS 'execution_result_conflict'" in sql
    v1 = _sql("sp_read_open_intelligence_execution_result_v1")
    v1_projection = _projection(v1, "SELECT r.result_contract_version")
    v2_projection = _projection(sql, "SELECT r.result_contract_version")
    assert v2_projection.startswith(
        v1_projection.rstrip().removesuffix("r.completed_at AS completed_at")
    )
    assert v2_projection.rstrip().endswith(
        "r.completed_at AS completed_at,\n"
        "         r.origin_registry_sha256 AS origin_registry_sha256,\n"
        "         r.resource_manifest_sha256 AS resource_manifest_sha256"
    )
    assert "r.origin_registry_sha256 = v_consumption.origin_registry_sha256" in sql
    assert "r.resource_manifest_sha256 = v_consumption.resource_manifest_sha256" in sql


def test_chain_reader_unions_v1_and_v2_linkage_and_appends_digests():
    sql = _sql("sp_read_open_intelligence_execution_result_chain_v2")
    assert "ASSERT v_chain_count = 1 AS 'execution_result_chain_unavailable'" in sql
    assert "AS 'execution_result_chain_identity_invalid'" in sql
    v1 = _sql("sp_read_open_intelligence_execution_result_chain_v1")
    v1_projection = _projection(v1, "SELECT a.approval_id AS approval_id")
    v2_projection = _projection(sql, "SELECT a.approval_id AS approval_id")
    assert v2_projection.startswith(
        v1_projection.rstrip().removesuffix("r.completed_at AS completed_at")
    )
    assert v2_projection.rstrip().endswith(
        "r.completed_at AS completed_at,\n"
        "         a.origin_registry_sha256 AS origin_registry_sha256,\n"
        "         a.resource_manifest_sha256 AS resource_manifest_sha256"
    )
    for table in ("approvals", "consumptions", "results"):
        assert f"`{{project}}.{{dataset}}.open_intelligence_execution_{table}_v1`" in sql
    assert "c.origin_registry_sha256 = a.origin_registry_sha256" in sql
    assert "r.resource_manifest_sha256 = a.resource_manifest_sha256" in sql
    assert "JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id" in sql


# Python parity for identifiers, canonical bytes and the phrase


@pytest.mark.parametrize("operation", SELECTED_OPERATIONS)
def test_sql_preimages_hash_to_the_python_v2_identifiers(operation):
    registry, approval, consumption, result = chain(operation)
    approve = _sql("sp_approve_open_intelligence_execution_v2")
    consume = _sql("sp_consume_open_intelligence_execution_v2")
    write = _sql("sp_record_open_intelligence_execution_result_v2")

    assert _preimage(approve, "exa_") == APPROVAL_PREIMAGE
    assert _preimage(consume, "exc_") == CONSUMPTION_PREIMAGE
    assert _preimage(write, "exr_") == RESULT_PREIMAGE
    for name, arguments in PREIMAGE_ARGUMENTS.items():
        assert arguments in _sql(name), name

    approval_preimage = APPROVAL_PREIMAGE % (
        timestamp(approval.approved_at),
        approval.approved_by,
        approval.manifest_sha256,
        registry.sha256,
        RESOURCE_SHA,
    )
    consumption_preimage = CONSUMPTION_PREIMAGE % (
        approval.approval_id,
        timestamp(consumption.consumed_at),
        consumption.execution_name,
        registry.sha256,
        RESOURCE_SHA,
    )
    result_preimage = RESULT_PREIMAGE % (
        timestamp(result.completed_at),
        consumption.consumption_id,
        registry.sha256,
        RESOURCE_SHA,
        result.result_digest,
        result.result_reference,
        result.status,
    )
    assert "exa_" + hashlib.sha256(approval_preimage.encode()).hexdigest() == approval.approval_id
    assert (
        "exc_" + hashlib.sha256(consumption_preimage.encode()).hexdigest()
        == consumption.consumption_id
    )
    assert "exr_" + hashlib.sha256(result_preimage.encode()).hexdigest() == result.result_id
    assert approval.approval_id == execution_approval.approval_id_v2(
        approval.manifest_sha256,
        approval.approved_by,
        approval.approved_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )


@pytest.mark.parametrize("operation", SELECTED_OPERATIONS)
def test_sql_row_order_matches_python_canonical_v2_bytes(operation):
    registry, approval, consumption, result = chain(operation)
    context = {
        "mode": "new_consume",
        "registry": registry,
        "expected_resource_manifest_sha256": RESOURCE_SHA,
    }
    expectations = (
        (
            "sp_approve_open_intelligence_execution_v2",
            "open_intelligence_execution_approvals_v2",
            approval,
            execution_approval.canonical_approval_v2_bytes(approval, **context),
        ),
        (
            "sp_consume_open_intelligence_execution_v2",
            "open_intelligence_execution_consumptions_v2",
            consumption,
            execution_approval.canonical_consumption_v2_bytes(consumption, **context),
        ),
        (
            "sp_record_open_intelligence_execution_result_v2",
            "open_intelligence_execution_results_v2",
            result,
            execution_approval.canonical_result_v2_bytes(result, **context),
        ),
    )
    for routine, table, record, canonical in expectations:
        sql = _sql(routine)
        record_fields = tuple(field.name for field in fields(record) if field.name != "_")
        schema_fields = tuple(
            name
            for name, *_ in migration._schema_from_sql(
                (SCHEMAS / f"{table}.sql").read_text(encoding="utf-8")
            )
        )
        assert record_fields == schema_fields
        assert set(json.loads(canonical)) == set(schema_fields)
        insert = sql[sql.index(f"INSERT INTO `{{project}}.{{dataset}}.{table}` VALUES (") :]
        insert = insert[: insert.index(");")]
        assert _top_level_commas(insert[insert.index("VALUES (") + len("VALUES (") :]) == (
            len(schema_fields) - 1
        ), routine
        assert insert.rstrip().endswith("v_origin_registry_sha256, v_resource_manifest_sha256")
        marker = "AS 'execution_approval_schema_mismatch'"
        readback = sql[sql.rindex("ASSERT", 0, sql.rindex(marker)) : sql.rindex(marker)]
        for field_name in schema_fields:
            assert f"{table}.{field_name} =" in readback, (routine, field_name)


def test_v2_approval_phrase_text_equals_successor1():
    sql = _sql("sp_approve_open_intelligence_execution_v2")
    assert (
        "ASSERT v_approval_phrase = FORMAT('" + V2_APPROVAL_PHRASE + "', v_operation, "
        "v_manifest_sha256, v_origin_registry_sha256, v_resource_manifest_sha256) "
        "AS 'execution_approval_manifest_mismatch'"
    ) in sql
    registry, approval, *_ = chain("brain_read")
    phrase = V2_APPROVAL_PHRASE % (
        "brain_read",
        approval.manifest_sha256,
        registry.sha256,
        RESOURCE_SHA,
    )
    assert hashlib.sha256(phrase.encode("utf-8")).hexdigest() == approval.approval_phrase_sha256
    assert approval.approval_phrase_sha256 == execution_approval._approval_phrase_sha256_v2(
        "brain_read", approval.manifest_sha256, registry.sha256, RESOURCE_SHA
    )
    assert "LOWER(TO_HEX(SHA256(v_approval_phrase)))" in sql


def test_disable_and_activation_routines_only_move_the_v2_lock():
    disable = _sql("sp_disable_open_intelligence_execution_approval_v2")
    assert "open_intelligence_execution_approval_lock_v1" not in disable
    assert "state = 'ready' AND lock_version = v_lock_version" in disable
    assert "state = 'disabled'" in disable
    assert "'open_intelligence_execution_disable_receipt_v2' AS contract_version" in disable
    assert "origin_registry_sha256" not in disable
    activate = _sql(CUTOVER_SCRIPT)
    assert activate == migration.V2_CUTOVER_SCRIPT
    assert activate.startswith("BEGIN\n")
    assert activate.rstrip().endswith("END;")
    assert "CREATE" not in activate
    for parameter in CUTOVER_PARAMETERS:
        assert f" DEFAULT @{parameter};" in activate, parameter
    assert tuple(migration.V2_CUTOVER_PARAMETERS) == CUTOVER_PARAMETERS
    installed = {name for name, *_ in (*migration.ROUTINE_SPECS, *migration.V2_ROUTINE_SPECS)}
    assert not any("activate" in name or "cutover" in name for name in installed)
    assert not any("activate" in name or "cutover" in name for name in setup.APPROVAL_SCHEMA_ORDER)
    assert (
        "UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`" not in activate
    )
    assert "state = 'prepared' AND lock_version = v_lock_version" in activate
    assert "SET lock_version = lock_version + 1, state = 'ready'" in activate
    assert (
        "INSERT INTO `{project}.{dataset}.open_intelligence_execution_active_generation_v1`"
        in activate
    )
    assert "'open_intelligence_execution_activation_receipt_v2' AS contract_version" in activate
    for table in ("approvals_v2", "consumptions_v2", "results_v2"):
        assert f"`{{project}}.{{dataset}}.open_intelligence_execution_{table}`) = 0" in activate


# (d) additive installation and registration


def test_setup_bigquery_approval_order_is_additive():
    order = tuple(setup.APPROVAL_SCHEMA_ORDER)
    assert order[:4] == (
        "open_intelligence_execution_approvals_v1.sql",
        "open_intelligence_execution_consumptions_v1.sql",
        "open_intelligence_execution_results_v1.sql",
        "open_intelligence_execution_approval_lock_v1.sql",
    )
    # The v2 migration creates every table after the four v1 ledgers, including the
    # two daily derivation tables the v1 daily routines reference.
    assert order[4:] == tuple(f"{table}.sql" for table in V2_TABLES)
    assert len(order) == 14
    assert tuple(setup.schema_order_for_dataset("trends_v2_staging_approvals")) == order
    for name in order:
        assert (SCHEMAS / name).is_file()


def test_v2_plan_is_additive_and_hashes_exact_files(monkeypatch):
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", _proposal_resource_manifest())
    plan = migration.build_v2_plan()
    assert tuple(item.name for item in plan.tables) == V2_TABLES
    assert tuple(item.name for item in plan.routines) == DEPLOYED_ROUTINES
    for item in (*plan.tables, *plan.routines):
        assert item.sha256 == hashlib.sha256(item.path.read_bytes()).hexdigest()
    for item in plan.routines:
        if item.name == UDF:
            assert item.parameters == (("raw", "STRING"),)
            continue
        assert item.parameters == DEPLOYED_PARAMETERS[item.name]
    assert plan.lock_seed == {
        "lock_name": "open_intelligence_execution_approval_v2",
        "approval_contract_version": "open_intelligence_execution_approval_v2",
        "lock_version": 0,
        "state": "prepared",
        "last_approval_id": None,
    }
    assert {item.name: item.sha256 for item in plan.registrations} == {
        "execution_origins_v1.json": REGISTRATION_HASHES["execution_origins_v1.json"],
        "resource_manifest.json": REGISTRATION_HASHES["resource_manifest.json"],
        "successor-brain-read.json": REGISTRATION_HASHES["successor-brain-read.json"],
        "successor-migration-exposure.json": REGISTRATION_HASHES[
            "successor-migration-exposure.json"
        ],
        "successor-source-snapshot-capture.json": REGISTRATION_HASHES[
            "successor-source-snapshot-capture.json"
        ],
        "successor-wave1-pilot.json": REGISTRATION_HASHES["successor-wave1-pilot.json"],
        **BRIDGE_REGISTRATION_HASHES,
    }
    # The active pair is the bridge generation the code runs under.
    assert plan.active_pair == (
        BRIDGE_REGISTRATION_HASHES["execution_origins_bridge_v3.json"],
        BRIDGE_REGISTRATION_HASHES["resource_manifest_bridge_v3.json"],
    )
    # The v1 plan and its seed are untouched by the v2 plan.
    assert migration.build_plan().lock_seed["state"] == "ready"
    assert tuple(item.name for item in migration.build_plan().tables) == migration.TABLE_NAMES


def test_v2_plan_refuses_a_conflicting_or_missing_registration_input(monkeypatch, tmp_path):
    reviewed = _proposal_resource_manifest()
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", tmp_path / "absent.json")
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_resource_manifest_missing$"):
        migration.build_v2_plan()
    drifted = tmp_path / "resource_manifest.json"
    drifted.write_bytes(reviewed.read_bytes() + b"\n")
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", drifted)
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_origin_registry_digest_mismatch$"
    ):
        migration.build_v2_plan()


def test_v2_lock_seed_is_prepared_once_and_never_touches_the_v1_lock():
    seed = migration.v2_lock_seed_sql()
    assert "open_intelligence_execution_approval_lock_v2`" in seed
    assert (
        "'open_intelligence_execution_approval_v2', 'open_intelligence_execution_approval_v2', 0, 'prepared', NULL"
        in seed
    )
    assert "WHERE NOT EXISTS" in seed
    assert "lock_v1" not in seed
    assert "UPDATE" not in seed
    assert "DELETE" not in seed


def test_v2_registration_is_idempotent_for_identical_rows_and_refuses_conflicts(monkeypatch):
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", _proposal_resource_manifest())
    plan = migration.build_v2_plan()
    sql, parameters = migration.v2_registration_script(plan)
    assert sql.startswith("BEGIN TRANSACTION;")
    assert sql.rstrip().endswith("COMMIT TRANSACTION;")
    assert sql.count("WHERE NOT EXISTS") == 9
    assert sql.count("INSERT INTO") == 9
    assert "UPDATE " not in sql
    assert "DELETE " not in sql
    assert "lock_v" not in sql
    assert "active_generation" not in sql
    assert "AS 'execution_origin_registry_conflict'" in sql
    assert "AS 'execution_resource_manifest_conflict'" in sql
    assert "AS 'execution_origin_policy_conflict'" in sql
    assert "JSON_VALUE(@resource_json, '$.origin_registry_sha256') = @origin_registry_sha256" in sql
    assert f"{CANONICAL_CALL}@registry_json)" in sql
    assert f"{CANONICAL_CALL}@resource_json)" in sql
    assert f"{CANONICAL_CALL}@registry_json_1)" in sql
    assert f"{CANONICAL_CALL}@resource_json_1)" in sql
    assert (
        "JSON_VALUE(@resource_json_1, '$.origin_registry_sha256') = @origin_registry_sha256_1"
        in sql
    )
    for index in range(5):
        assert f"{CANONICAL_CALL}@policy_json_{index})" in sql
        assert f"@policy_sha256_{index} = LOWER(TO_HEX(SHA256(@policy_json_{index})))" in sql
    values = {name: value for name, kind, value in parameters}
    assert all(kind == "STRING" for _name, kind, _value in parameters)
    assert values["origin_registry_sha256"] == REGISTRATION_HASHES["execution_origins_v1.json"]
    assert values["resource_manifest_sha256"] == REGISTRATION_HASHES["resource_manifest.json"]
    assert (
        hashlib.sha256(values["registry_json"].encode("utf-8")).hexdigest()
        == values["origin_registry_sha256"]
    )
    assert (
        hashlib.sha256(values["resource_json"].encode("utf-8")).hexdigest()
        == values["resource_manifest_sha256"]
    )
    assert (
        values["origin_registry_sha256_1"]
        == (BRIDGE_REGISTRATION_HASHES["execution_origins_bridge_v3.json"])
    )
    assert (
        values["resource_manifest_sha256_1"]
        == (BRIDGE_REGISTRATION_HASHES["resource_manifest_bridge_v3.json"])
    )
    for suffix in ("", "_1"):
        assert (
            hashlib.sha256(values[f"registry_json{suffix}"].encode("utf-8")).hexdigest()
            == values[f"origin_registry_sha256{suffix}"]
        )
        assert (
            hashlib.sha256(values[f"resource_json{suffix}"].encode("utf-8")).hexdigest()
            == values[f"resource_manifest_sha256{suffix}"]
        )
    policy_digests = {values[f"policy_sha256_{index}"] for index in range(5)}
    assert policy_digests == {
        REGISTRATION_HASHES[name]
        for name in (
            "successor-brain-read.json",
            "successor-migration-exposure.json",
            "successor-source-snapshot-capture.json",
            "successor-wave1-pilot.json",
        )
    } | {BRIDGE_REGISTRATION_HASHES["source-bridge-capture-v3.json"]}
    for index in range(5):
        assert (
            hashlib.sha256(values[f"policy_json_{index}"].encode("utf-8")).hexdigest()
            == values[f"policy_sha256_{index}"]
        )
    assert "SESSION_USER()" in sql
    assert "CURRENT_TIMESTAMP()" in sql


class _RecordingClient:
    def __init__(self):
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return _Job()


class _Job:
    def result(self):
        return ()


def test_apply_v2_installation_runs_ddl_seed_and_registration_in_order(monkeypatch):
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", _proposal_resource_manifest())
    client = _RecordingClient()
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)
    plan = migration.build_v2_plan()

    migration._apply_v2_installation(plan, object())

    statements = [sql for sql, _config in client.queries]
    assert len(statements) == len(plan.tables) + len(plan.routines) + 2
    for index, table in enumerate(plan.tables):
        assert statements[index].startswith(
            f"CREATE TABLE IF NOT EXISTS `{migration.PROJECT}.{migration.DATASET}.{table.name}`"
        )
    for index, routine in enumerate(plan.routines, start=len(plan.tables)):
        assert f"`{migration.PROJECT}.{migration.DATASET}.{routine.name}`" in statements[index]
        assert "{project}" not in statements[index]
        assert "{dataset}" not in statements[index]
    assert statements[-2] == migration.v2_lock_seed_sql()
    assert statements[-1] == migration.v2_registration_script(plan)[0].replace(
        "{project}", migration.PROJECT
    ).replace("{dataset}", migration.DATASET)
    assert client.queries[-1][1] is not None
    for sql in statements:
        assert "open_intelligence_execution_approval_lock_v1`\n  SET" not in sql
        assert (
            "CREATE OR REPLACE PROCEDURE `ogilvy-trends-v2.trends_v2_staging_approvals.sp_"
            not in sql
            or sql.rstrip().endswith("END;")
        )
        for name in V1_ROUTINE_SPEC_NAMES:
            assert (
                f"CREATE OR REPLACE PROCEDURE `{migration.PROJECT}.{migration.DATASET}.{name}`"
                not in sql
            )


def test_v2_iam_map_grants_runtime_principals_only_reader_consume_and_write_routines(monkeypatch):
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", _proposal_resource_manifest())
    plan = migration.build_v2_plan()
    iam = plan.iam_plan
    human = "user:albert.meintjes@ogilvy.co.za"
    by_principal: dict[str, set[str]] = {}
    for binding in iam.principal_routine_bindings:
        by_principal.setdefault(binding.principal, set()).add(binding.resource.rsplit("/", 1)[1])
    assert by_principal[human] == {
        "sp_approve_open_intelligence_execution_v2",
        "sp_approve_open_intelligence_execution_v3",
        "sp_disable_open_intelligence_execution_approval_v2",
        UDF,
        *RECURRING_GRANT_ROUTINES,
    }
    runtime = {principal: names for principal, names in by_principal.items() if principal != human}
    capture_principal = f"serviceAccount:{CASES['source_snapshot_capture']['identity']}"
    assert set(runtime) == {
        f"serviceAccount:{CASES[operation]['identity']}" for operation in SELECTED_OPERATIONS
    } | {capture_principal}
    for principal, names in runtime.items():
        assert "sp_approve_open_intelligence_execution_v2" not in names
        assert "sp_approve_open_intelligence_execution_v3" not in names
        assert "sp_disable_open_intelligence_execution_approval_v2" not in names
        # The capture binding swaps the generic consume routine for the grant bound one.
        consume = (
            SOURCE_SNAPSHOT_ROUTINE
            if principal == capture_principal
            else "sp_consume_open_intelligence_execution_v2"
        )
        assert {
            "sp_read_open_intelligence_execution_approval_v2",
            "sp_read_open_intelligence_execution_approval_v3",
            consume,
            "sp_record_open_intelligence_execution_result_v2",
            "sp_read_open_intelligence_execution_result_v2",
            UDF,
        } <= names
        assert (SOURCE_SNAPSHOT_ROUTINE in names) == (principal == capture_principal)
        assert ("sp_consume_open_intelligence_execution_v2" in names) == (
            principal != capture_principal
        )
        assert not any(name.startswith("sp_") and name.endswith("_v1") for name in names), principal
    chain_readers = {
        principal
        for principal, names in runtime.items()
        if "sp_read_open_intelligence_execution_result_chain_v2" in names
    }
    assert chain_readers == {
        f"serviceAccount:{CASES[operation]['identity']}"
        for operation in ("r3_apply", "r3_proof_issue", "r3_release")
    }
    assert tuple(item.role for item in iam.routine_authorizations) == tuple(
        "roles/bigquery.routineDataViewer"
        if name in (UDF, *READERS, *AMENDMENT_D_READERS)
        else "roles/bigquery.routineDataEditor"
        for name in DEPLOYED_ROUTINES
    )
    assert iam.proposed_job_user_grants == ()
    assert iam.temporary_bootstrap_bindings == ()


def test_v2_plan_render_is_canonical_and_never_lists_v1_sources(monkeypatch):
    monkeypatch.setattr(migration, "RESOURCE_MANIFEST_PATH", _proposal_resource_manifest())
    plan = migration.build_v2_plan()
    rendered = migration.render_v2_plan(plan)
    payload = json.loads(rendered)
    assert (
        rendered
        == json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    )
    assert payload["proof_kind"] == "code_only_structural_proof"
    assert payload["lock_seed"]["state"] == "prepared"
    assert [item["name"] for item in payload["tables"]] == list(V2_TABLES)
    assert [item["name"] for item in payload["routines"]] == list(DEPLOYED_ROUTINES)
    assert [item["name"] for item in payload["registrations"]] == list(REGISTRATION_ORDER)
    assert "_v1.sql" not in json.dumps(
        [item["path"] for item in payload["routines"] if item["name"] in V2_ROUTINE_PARAMETERS]
    )


def test_registration_digests_pin_the_packaged_files_and_only_the_named_policies():
    """Every registration digest is the sha256 of the packaged file it names, and the
    policy directory holds exactly the policy files the migration registers."""
    paths = {
        "execution_origins_v1.json": migration.REGISTRY_PATH,
        "resource_manifest.json": migration.RESOURCE_MANIFEST_PATH,
        "execution_origins_bridge_v3.json": migration.BRIDGE_REGISTRY_PATH,
        "resource_manifest_bridge_v3.json": migration.BRIDGE_RESOURCE_MANIFEST_PATH,
        "source-bridge-capture-v3.json": (
            migration.BRIDGE_POLICY_CONTRACT_DIR / "source-bridge-capture-v3.json"
        ),
    }
    for name, expected in migration.REGISTRATION_DIGESTS.items():
        path = paths.get(name, migration.POLICY_CONTRACT_DIR / name)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
    assert set(migration.POLICY_FILES) == {
        name for name in migration.REGISTRATION_DIGESTS if name.startswith("successor-")
    }
    assert sorted(path.name for path in migration.POLICY_CONTRACT_DIR.glob("*.json")) == list(
        migration.POLICY_FILES
    )
    assert migration.BRIDGE_POLICY_FILES == ("source-bridge-capture-v3.json",)
    assert migration.REGISTRATION_DIGESTS == {**REGISTRATION_HASHES, **BRIDGE_REGISTRATION_HASHES}


def test_packaged_resource_manifest_is_the_registration_input():
    """The registration reads the packaged manifest copies that the loader pins: the
    active bridge manifest, and the amendment e manifest the loader retains."""
    from src.analysis.open_intelligence import execution_generations

    assert migration.RESOURCE_MANIFEST_PATH.is_file()
    packaged = hashlib.sha256(migration.RESOURCE_MANIFEST_PATH.read_bytes()).hexdigest()
    assert packaged == migration.REGISTRATION_DIGESTS["resource_manifest.json"]
    retained = execution_generations.load_trusted_generation(
        REGISTRATION_HASHES["execution_origins_v1.json"], packaged
    )
    assert retained.resource_manifest_path == migration.RESOURCE_MANIFEST_PATH
    assert migration.BRIDGE_RESOURCE_MANIFEST_PATH.is_file()
    bridge = hashlib.sha256(migration.BRIDGE_RESOURCE_MANIFEST_PATH.read_bytes()).hexdigest()
    assert bridge == migration.REGISTRATION_DIGESTS["resource_manifest_bridge_v3.json"]
    active = execution_generations.active_generation()
    assert bridge == active.resource_manifest_sha256
    assert active.resource_manifest_path == migration.BRIDGE_RESOURCE_MANIFEST_PATH
    assert active.registry_path == migration.BRIDGE_REGISTRY_PATH


def test_v2_plan_registers_the_active_bridge_generation_beside_amendment_e():
    """apply-v2 registers the code's active pair and every policy its registry names, so
    the rotation to that pair finds each registration it asserts; the amendment e pair
    stays registered for the records written under it."""
    from src.analysis.open_intelligence import execution_generations

    plan = migration.build_v2_plan()
    assert tuple(item.name for item in plan.registrations) == REGISTRATION_ORDER
    assert {item.name: item.sha256 for item in plan.registrations} == {
        **REGISTRATION_HASHES,
        **BRIDGE_REGISTRATION_HASHES,
    }
    assert plan.active_pair == execution_generations.ACTIVE_GENERATION_PAIR
    assert plan.active_pair == (
        BRIDGE_REGISTRATION_HASHES["execution_origins_bridge_v3.json"],
        BRIDGE_REGISTRATION_HASHES["resource_manifest_bridge_v3.json"],
    )
    paths = {item.name: migration._registration_path(item) for item in plan.registrations}
    assert paths["execution_origins_bridge_v3.json"] == (
        "configs/open_intelligence/execution_origins_bridge_v3.json"
    )
    assert paths["resource_manifest_bridge_v3.json"] == (
        "configs/open_intelligence/resource_manifest_bridge_v3.json"
    )
    # The bridge registry names its policy at this path, so the policy is registered from it.
    assert paths["source-bridge-capture-v3.json"] == (
        "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"
    )
    for item in plan.registrations:
        assert hashlib.sha256(item.canonical_json.encode("utf-8")).hexdigest() == item.sha256
        assert item.canonical_json.encode("utf-8") == item.path.read_bytes()


def test_v2_registration_script_registers_both_generations_and_five_policies():
    plan = migration.build_v2_plan()
    sql, parameters = migration.v2_registration_script(plan)
    assert sql.startswith("BEGIN TRANSACTION;")
    assert sql.rstrip().endswith("COMMIT TRANSACTION;")
    assert sql.count("WHERE NOT EXISTS") == 9
    assert sql.count("INSERT INTO") == 9
    assert "UPDATE " not in sql
    assert "DELETE " not in sql
    assert "lock_v" not in sql
    assert "active_generation" not in sql
    assert (
        sql.count(
            "INSERT INTO `{project}.{dataset}.open_intelligence_execution_origin_registries_v1`"
        )
        == 2
    )
    assert (
        sql.count(
            "INSERT INTO `{project}.{dataset}.open_intelligence_execution_resource_manifests_v1`"
        )
        == 2
    )
    assert (
        sql.count(
            "INSERT INTO `{project}.{dataset}.open_intelligence_execution_origin_policies_v1`"
        )
        == 5
    )
    for suffix in ("", "_1"):
        assert (
            f"JSON_VALUE(@resource_json{suffix}, '$.origin_registry_sha256') = "
            f"@origin_registry_sha256{suffix}" in sql
        )
        assert f"SELECT @resource_manifest_sha256{suffix}, @origin_registry_sha256{suffix}, " in sql
    values = {name: value for name, kind, value in parameters}
    assert all(kind == "STRING" for _name, kind, _value in parameters)
    assert len(values) == len(parameters) == 18
    by_name = {item.name: item for item in plan.registrations}
    expected = {
        "origin_registry_sha256": by_name["execution_origins_v1.json"],
        "resource_manifest_sha256": by_name["resource_manifest.json"],
        "origin_registry_sha256_1": by_name["execution_origins_bridge_v3.json"],
        "resource_manifest_sha256_1": by_name["resource_manifest_bridge_v3.json"],
    }
    for name, item in expected.items():
        assert values[name] == item.sha256
    assert values["registry_json_1"] == by_name["execution_origins_bridge_v3.json"].canonical_json
    assert values["resource_json_1"] == by_name["resource_manifest_bridge_v3.json"].canonical_json
    assert [values[f"policy_sha256_{index}"] for index in range(5)] == [
        by_name[name].sha256 for name in REGISTRATION_ORDER[4:]
    ]


def test_rotation_to_the_active_pair_finds_every_registration_the_script_asserts():
    """The rotation script's registration asserts, evaluated over what apply-v2 registers:
    the activated registry and its manifest are registered once, the manifest names the
    registry, and every v2 origin row has exactly one matching registered policy."""
    from src.analysis.open_intelligence import execution_generations

    plan = migration.build_v2_plan()
    registries = {}
    manifests = {}
    policies = {}
    for item in plan.registrations:
        payload = json.loads(item.canonical_json)
        if payload.get("contract_version") == "open_intelligence_execution_origin_registry_v1":
            registries[item.sha256] = payload
        elif "resources" in payload:
            manifests[item.sha256] = payload
        else:
            policies[item.sha256] = payload
    assert len(registries) == 2
    assert len(manifests) == 2
    assert len(policies) == 5
    activate = execution_generations.ACTIVE_GENERATION_PAIR
    registry = registries[activate[0]]
    assert manifests[activate[1]]["origin_registry_sha256"] == activate[0]
    v2_rows = [
        row
        for row in registry["rows"]
        if row["manifest_version"] == "open_intelligence_execution_manifest_v2"
    ]
    assert v2_rows
    for row in v2_rows:
        matches = [
            policy
            for digest, policy in policies.items()
            if digest == row["contract_sha256"]
            and policy["contract_version"] == "open_intelligence_execution_origin_policy_v2"
            and policy["origin"]["image_uri_regex"] == row["image_uri_regex"]
            and policy["origin"]["connected_repository"] == row["connected_repo"]
        ]
        assert len(matches) == 1, row["contract_file"]
    # The retiring pair's registry is registered too, with its own policies.
    assert "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179" in registries


def test_v2_plan_refuses_a_bridge_registration_input_that_moved(monkeypatch, tmp_path):
    drifted = tmp_path / "resource_manifest_bridge_v3.json"
    drifted.write_bytes(migration.BRIDGE_RESOURCE_MANIFEST_PATH.read_bytes() + b"\n")
    monkeypatch.setattr(migration, "BRIDGE_RESOURCE_MANIFEST_PATH", drifted)
    with pytest.raises(
        migration.MigrationRefusal, match=r"^execution_origin_registry_digest_mismatch$"
    ):
        migration.build_v2_plan()
    monkeypatch.setattr(migration, "BRIDGE_RESOURCE_MANIFEST_PATH", tmp_path / "absent.json")
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_resource_manifest_missing$"):
        migration.build_v2_plan()


def test_v2_plan_refuses_when_a_policy_the_bridge_registry_names_is_not_registered(
    monkeypatch,
):
    monkeypatch.setattr(migration, "BRIDGE_POLICY_FILES", ())
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_origin_registry_invalid$"):
        migration.build_v2_plan()
