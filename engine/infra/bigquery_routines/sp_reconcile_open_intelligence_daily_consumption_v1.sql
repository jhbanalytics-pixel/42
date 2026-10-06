CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_reconcile_open_intelligence_daily_consumption_v1`(
  derivation_id STRING, consumption_id STRING, execution_name STRING, execution_terminal_state STRING,
  canonical_reconciliation_json STRING, reconciliation_digest STRING
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_actor STRING DEFAULT SESSION_USER();
  DECLARE v_derivation_id STRING DEFAULT derivation_id;
  DECLARE v_consumption_id STRING DEFAULT consumption_id;
  DECLARE v_execution_name STRING DEFAULT execution_name;
  DECLARE v_terminal_state STRING DEFAULT execution_terminal_state;
  DECLARE v_digest STRING DEFAULT reconciliation_digest;
  DECLARE v_derivation STRUCT<authorizing_approval_id STRING,authorizing_grant_digest STRING,manifest_sha256 STRING,operation_context_sha256 STRING,business_attempt_id STRING,child_job_resource STRING,origin_registry_sha256 STRING,resource_manifest_sha256 STRING>;
  DECLARE v_consumption STRUCT<execution_name STRING,consumed_at TIMESTAMP>;
  DECLARE v_id STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  ASSERT CONCAT('usr_',LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:',v_actor)))))='usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
    AS 'daily_consumption_reconciliation_actor_invalid';
  ASSERT v_terminal_state IN ('cancelled','failed','succeeded') AS 'daily_consumption_reconciliation_state_invalid';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_reconciliation_json)
    AND LOWER(TO_HEX(SHA256(canonical_reconciliation_json)))=v_digest
    AND ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(canonical_reconciliation_json,wide_number_mode=>'exact'),1,mode=>'strict'),',')='child_job_resource,consumption_id,contract_version,derivation_id,execution_completed_at,execution_name,execution_readback_sha256,execution_terminal_state,observed_at,observer_principal'
    AND JSON_VALUE(canonical_reconciliation_json,'$.contract_version')='daily_consumption_reconciliation_v1'
    AND JSON_VALUE(canonical_reconciliation_json,'$.derivation_id')=v_derivation_id
    AND JSON_VALUE(canonical_reconciliation_json,'$.consumption_id')=v_consumption_id
    AND JSON_VALUE(canonical_reconciliation_json,'$.execution_name')=v_execution_name
    AND JSON_VALUE(canonical_reconciliation_json,'$.execution_terminal_state')=v_terminal_state
    AND JSON_VALUE(canonical_reconciliation_json,'$.observer_principal')=v_actor
    AND REGEXP_CONTAINS(JSON_VALUE(canonical_reconciliation_json,'$.execution_readback_sha256'),r'^[0-9a-f]{64}$')
    AND TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.execution_completed_at'))<=TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.observed_at'))
    AND TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.observed_at'))<=v_now
    AS 'daily_consumption_reconciliation_invalid';
  SET v_derivation=(SELECT AS STRUCT authorizing_approval_id,authorizing_grant_digest,manifest_sha256,operation_context_sha256,business_attempt_id,child_job_resource,origin_registry_sha256,resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` WHERE derivation_id=v_derivation_id);
  SET v_consumption=(SELECT AS STRUCT execution_name,consumed_at FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
    WHERE consumption_id=v_consumption_id AND approval_id=v_derivation_id);
  ASSERT v_derivation IS NOT NULL AND v_consumption IS NOT NULL
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE approval_id=v_derivation_id)=1
    AS 'daily_consumption_reconciliation_authority_invalid';
  ASSERT v_consumption.execution_name=v_execution_name
    AND JSON_VALUE(canonical_reconciliation_json,'$.child_job_resource')=v_derivation.child_job_resource
    AND v_consumption.consumed_at<=TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.execution_completed_at'))
    AS 'daily_consumption_reconciliation_execution_mismatch';
  ASSERT NOT EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_results_v2` WHERE consumption_id=v_consumption_id)
    AS 'daily_consumption_reconciliation_resolved';
  SET v_id=CONCAT('ext_',LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT('open_intelligence_execution_derivation_tombstone_v1' AS tombstone_contract_version,v_derivation_id AS derivation_id,'owner_reconciliation_hold' AS reason_code,v_digest AS reconciliation_digest))))));
  IF EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_derivation_id) THEN
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_derivation_id AND tombstone_id=v_id)=1 AS 'daily_consumption_reconciliation_conflict';
    COMMIT TRANSACTION;
    SELECT * FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_derivation_id;
    RETURN;
  END IF;
  SET v_lock_version=(SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name='open_intelligence_execution_approval_v2' AND state='ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='consuming',lock_version=lock_version+1,updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='ready' AND lock_version=v_lock_version;
  ASSERT @@row_count=1 AS 'daily_consumption_reconciliation_concurrent_conflict';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1`
    (tombstone_contract_version,tombstone_id,derivation_id,authorizing_approval_id,authorizing_grant_digest,manifest_sha256,operation_context_sha256,business_attempt_id,child_job_resource,reason_code,reconciliation_digest,cancelled_by,origin_registry_sha256,resource_manifest_sha256,cancelled_at)
  VALUES('open_intelligence_execution_derivation_tombstone_v1',v_id,v_derivation_id,v_derivation.authorizing_approval_id,v_derivation.authorizing_grant_digest,v_derivation.manifest_sha256,v_derivation.operation_context_sha256,v_derivation.business_attempt_id,v_derivation.child_job_resource,'owner_reconciliation_hold',v_digest,v_actor,v_derivation.origin_registry_sha256,v_derivation.resource_manifest_sha256,v_now);
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='ready',updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='consuming' AND lock_version=v_lock_version+1;
  ASSERT @@row_count=1 AS 'daily_consumption_reconciliation_concurrent_conflict';
  COMMIT TRANSACTION;
  SELECT * FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE tombstone_id=v_id;
END;
