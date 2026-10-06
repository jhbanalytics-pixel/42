CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_cancel_open_intelligence_daily_derivation_v1`(
  derivation_id STRING, reason_code STRING, canonical_reconciliation_json STRING, reconciliation_digest STRING
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_derivation_id STRING DEFAULT derivation_id;
  DECLARE v_reason STRING DEFAULT reason_code;
  DECLARE v_digest STRING DEFAULT reconciliation_digest;
  DECLARE v_row STRUCT<authorizing_approval_id STRING,authorizing_grant_digest STRING,manifest_sha256 STRING,operation_context_sha256 STRING,business_attempt_id STRING,child_job_resource STRING,derived_by STRING,origin_registry_sha256 STRING,resource_manifest_sha256 STRING>;
  DECLARE v_id STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  ASSERT v_reason IN ('dispatch_not_attempted','provider_terminal_no_execution') AS 'daily_tombstone_reason_invalid';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_reconciliation_json)
    AND LOWER(TO_HEX(SHA256(canonical_reconciliation_json)))=v_digest
    AND ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(canonical_reconciliation_json,wide_number_mode=>'exact'),1,mode=>'strict'),',')='contract_version,derivation_id,dispatch_attempted,dispatch_observation_reference,execution_created,observed_at,observer_principal,provider_operation_name,provider_terminal_state,reason_code'
    AND JSON_VALUE(canonical_reconciliation_json,'$.contract_version')='daily_dispatch_reconciliation_v1'
    AND JSON_VALUE(canonical_reconciliation_json,'$.derivation_id')=v_derivation_id
    AND JSON_VALUE(canonical_reconciliation_json,'$.reason_code')=v_reason
    AND JSON_VALUE(canonical_reconciliation_json,'$.observer_principal')=SESSION_USER()
    AND TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.observed_at'))<=v_now
    AND ((v_reason='dispatch_not_attempted'
      AND JSON_VALUE(canonical_reconciliation_json,'$.dispatch_attempted')='false'
      AND JSON_VALUE(canonical_reconciliation_json,'$.execution_created')='false'
      AND JSON_QUERY(canonical_reconciliation_json,'$.provider_operation_name')='null'
      AND JSON_QUERY(canonical_reconciliation_json,'$.provider_terminal_state')='null'
      AND JSON_QUERY(canonical_reconciliation_json,'$.dispatch_observation_reference')='null')
    OR (v_reason='provider_terminal_no_execution'
      AND JSON_VALUE(canonical_reconciliation_json,'$.dispatch_attempted')='true'
      AND JSON_VALUE(canonical_reconciliation_json,'$.execution_created')='false'
      AND NULLIF(JSON_VALUE(canonical_reconciliation_json,'$.provider_operation_name'),'') IS NOT NULL
      AND JSON_VALUE(canonical_reconciliation_json,'$.provider_terminal_state')='done_no_execution'
      AND NULLIF(JSON_VALUE(canonical_reconciliation_json,'$.dispatch_observation_reference'),'') IS NOT NULL))
    AS 'daily_reconciliation_invalid';
  SET v_row=(SELECT AS STRUCT authorizing_approval_id,authorizing_grant_digest,manifest_sha256,operation_context_sha256,business_attempt_id,child_job_resource,derived_by,origin_registry_sha256,resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` WHERE derivation_id=v_derivation_id);
  ASSERT v_row IS NOT NULL AND SESSION_USER()=v_row.derived_by AS 'daily_tombstone_actor_invalid';
  ASSERT NOT EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE approval_id=v_derivation_id) AS 'daily_tombstone_consumed';
  SET v_id=CONCAT('ext_',LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT('open_intelligence_execution_derivation_tombstone_v1' AS tombstone_contract_version,v_derivation_id AS derivation_id,v_reason AS reason_code,v_digest AS reconciliation_digest))))));
  IF EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_derivation_id) THEN
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_derivation_id AND tombstone_id=v_id)=1 AS 'daily_tombstone_conflict';
    COMMIT TRANSACTION;
    SELECT * FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_derivation_id;
    RETURN;
  END IF;
  SET v_lock_version=(SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name='open_intelligence_execution_approval_v2' AND state='ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='consuming',lock_version=lock_version+1,updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='ready' AND lock_version=v_lock_version;
  ASSERT @@row_count=1 AS 'daily_tombstone_concurrent_conflict';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1`
    (tombstone_contract_version,tombstone_id,derivation_id,authorizing_approval_id,authorizing_grant_digest,manifest_sha256,operation_context_sha256,business_attempt_id,child_job_resource,reason_code,reconciliation_digest,cancelled_by,origin_registry_sha256,resource_manifest_sha256,cancelled_at)
  VALUES('open_intelligence_execution_derivation_tombstone_v1',v_id,v_derivation_id,v_row.authorizing_approval_id,v_row.authorizing_grant_digest,v_row.manifest_sha256,v_row.operation_context_sha256,v_row.business_attempt_id,v_row.child_job_resource,v_reason,v_digest,SESSION_USER(),v_row.origin_registry_sha256,v_row.resource_manifest_sha256,v_now);
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='ready',updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='consuming' AND lock_version=v_lock_version+1;
  ASSERT @@row_count=1 AS 'daily_tombstone_concurrent_conflict';
  COMMIT TRANSACTION;
  SELECT * FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE tombstone_id=v_id;
END;
