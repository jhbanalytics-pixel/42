CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_record_open_intelligence_daily_result_v1`(
  derivation_id STRING, consumption_id STRING, canonical_payload_json STRING, payload_digest STRING,
  execution_observation_sha256 STRING, canonical_stage_metering_json STRING, result_reference STRING,
  terminal_state STRING, effect_state STRING, spend_state STRING
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_derivation_id STRING DEFAULT derivation_id;
  DECLARE v_consumption_id STRING DEFAULT consumption_id;
  DECLARE v_payload_digest STRING DEFAULT payload_digest;
  DECLARE v_observation_digest STRING DEFAULT execution_observation_sha256;
  DECLARE v_result_reference STRING DEFAULT result_reference;
  DECLARE v_terminal_state STRING DEFAULT terminal_state;
  DECLARE v_effect_state STRING DEFAULT effect_state;
  DECLARE v_spend_state STRING DEFAULT spend_state;
  DECLARE v_consumption STRUCT<manifest_sha256 STRING,operation STRING,execution_name STRING,consumed_at TIMESTAMP,origin_registry_sha256 STRING,resource_manifest_sha256 STRING>;
  DECLARE v_derivation STRUCT<authorizing_approval_id STRING,authorizing_grant_digest STRING,operation_context_sha256 STRING,business_attempt_id STRING,child_job_resource STRING,child_service_identity STRING>;
  DECLARE v_metering JSON;
  DECLARE v_envelope STRING;
  DECLARE v_result_digest STRING;
  DECLARE v_result_id STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_payload_json)
    AND LOWER(TO_HEX(SHA256(canonical_payload_json)))=v_payload_digest AS 'daily_result_payload_invalid';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_stage_metering_json) AS 'daily_result_metering_invalid';
  SET v_metering=SAFE.PARSE_JSON(canonical_stage_metering_json,wide_number_mode=>'exact');
  ASSERT ARRAY_TO_STRING(JSON_KEYS(v_metering,1,mode=>'strict'),',')='complete,model_calls,query_count,storage_write_bytes,storage_write_count,total_bytes_billed,vendor_credits'
    AND JSON_TYPE(JSON_QUERY(v_metering,'$.complete'))='boolean'
    AND NOT EXISTS(SELECT 1 FROM UNNEST([
      JSON_QUERY(v_metering,'$.model_calls'),JSON_QUERY(v_metering,'$.query_count'),
      JSON_QUERY(v_metering,'$.storage_write_bytes'),JSON_QUERY(v_metering,'$.storage_write_count'),
      JSON_QUERY(v_metering,'$.total_bytes_billed')]) metric
      WHERE JSON_TYPE(metric) NOT IN ('number','null')
        OR (JSON_TYPE(metric)='number' AND NOT REGEXP_CONTAINS(JSON_VALUE(metric),r'^(0|[1-9][0-9]*)$')))
    AND (JSON_TYPE(JSON_QUERY(v_metering,'$.vendor_credits'))='null' OR (
      JSON_TYPE(JSON_QUERY(v_metering,'$.vendor_credits'))='string'
      AND REGEXP_CONTAINS(JSON_VALUE(canonical_stage_metering_json,'$.vendor_credits'),r'^(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$')))
    AS 'daily_result_metering_invalid';
  ASSERT terminal_state IN ('succeeded','failed') AND effect_state IN ('effects_recorded','no_effect','unknown')
    AND spend_state IN ('measured','no_spend','unknown') AS 'daily_result_state_invalid';
  ASSERT JSON_VALUE(canonical_stage_metering_json,'$.complete')!='true' OR NOT EXISTS(
    SELECT 1 FROM UNNEST([
      JSON_QUERY(v_metering,'$.model_calls'),JSON_QUERY(v_metering,'$.query_count'),
      JSON_QUERY(v_metering,'$.storage_write_bytes'),JSON_QUERY(v_metering,'$.storage_write_count'),
      JSON_QUERY(v_metering,'$.total_bytes_billed'),JSON_QUERY(v_metering,'$.vendor_credits')]) metric
    WHERE JSON_VALUE(metric) IS NULL) AS 'daily_result_metering_invalid';
  ASSERT /* daily_metering_state_predicate_begin */
    (JSON_VALUE(canonical_stage_metering_json,'$.complete')='true' OR spend_state='unknown')
    AND (terminal_state!='succeeded' OR effect_state='effects_recorded')
    AND (spend_state!='no_spend' OR (effect_state='no_effect'
    AND JSON_VALUE(canonical_stage_metering_json,'$.complete')='true'
    AND JSON_VALUE(canonical_stage_metering_json,'$.query_count')='0'
    AND JSON_VALUE(canonical_stage_metering_json,'$.total_bytes_billed')='0'
    AND JSON_VALUE(canonical_stage_metering_json,'$.storage_write_count')='0'
    AND JSON_VALUE(canonical_stage_metering_json,'$.storage_write_bytes')='0'
    AND JSON_VALUE(canonical_stage_metering_json,'$.model_calls')='0'
    AND JSON_VALUE(canonical_stage_metering_json,'$.vendor_credits')='0'))
    /* daily_metering_state_predicate_end */ AS 'daily_result_state_invalid';
  SET v_lock_version=(SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name='open_intelligence_execution_approval_v2' AND state='ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='consuming',lock_version=lock_version+1,updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='ready' AND lock_version=v_lock_version;
  ASSERT @@row_count=1 AS 'daily_result_concurrent_conflict';
  SET v_consumption=(SELECT AS STRUCT manifest_sha256,operation,execution_name,consumed_at,origin_registry_sha256,resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE consumption_id=v_consumption_id AND approval_id=v_derivation_id);
  SET v_derivation=(SELECT AS STRUCT authorizing_approval_id,authorizing_grant_digest,operation_context_sha256,business_attempt_id,child_job_resource,child_service_identity
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` WHERE derivation_id=v_derivation_id);
  ASSERT v_consumption IS NOT NULL AND v_derivation IS NOT NULL AS 'daily_result_authority_invalid';
  ASSERT SESSION_USER()=v_derivation.child_service_identity AS 'daily_result_actor_mismatch';
  ASSERT NOT EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_derivation_id)
    AS 'daily_result_reconciled';
  ASSERT v_consumption_id=CONCAT('exc_',LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(v_consumption.consumed_at AS consumed_at,'open_intelligence_execution_consumption_v3' AS consumption_contract_version,v_derivation_id AS derivation_id,v_consumption.execution_name AS execution_name,v_observation_digest AS execution_observation_sha256,v_consumption.origin_registry_sha256 AS origin_registry_sha256,v_consumption.resource_manifest_sha256 AS resource_manifest_sha256)))))) AS 'daily_result_observation_invalid';
  IF EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_results_v2` WHERE consumption_id=v_consumption_id) THEN
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v2`
      WHERE consumption_id=v_consumption_id AND result_reference=v_result_reference
        AND JSON_VALUE(canonical_result_json,'$.payload_digest')=v_payload_digest
        AND JSON_VALUE(canonical_result_json,'$.execution_observation_sha256')=v_observation_digest
        AND JSON_QUERY(canonical_result_json,'$.stage_metering')=JSON_QUERY(canonical_stage_metering_json,'$')
        AND JSON_VALUE(canonical_result_json,'$.terminal_state')=v_terminal_state
        AND JSON_VALUE(canonical_result_json,'$.effect_state')=v_effect_state
        AND JSON_VALUE(canonical_result_json,'$.spend_state')=v_spend_state)=1 AS 'daily_result_conflict';
    UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='ready',updated_at=v_now
      WHERE lock_name='open_intelligence_execution_approval_v2' AND state='consuming' AND lock_version=v_lock_version+1;
    ASSERT @@row_count=1 AS 'daily_result_concurrent_conflict';
    COMMIT TRANSACTION;
    SELECT TO_JSON(r) AS result FROM `{project}.{dataset}.open_intelligence_execution_results_v2` r WHERE r.consumption_id=v_consumption_id;
    RETURN;
  END IF;
  SET v_envelope=CONCAT(
    '{"authorizing_approval_id":', TO_JSON_STRING(v_derivation.authorizing_approval_id),
    ',"authorizing_grant_digest":', TO_JSON_STRING(v_derivation.authorizing_grant_digest),
    ',"business_attempt_id":', TO_JSON_STRING(v_derivation.business_attempt_id),
    ',"child_job_resource":', TO_JSON_STRING(v_derivation.child_job_resource),
    ',"completed_at":', TO_JSON_STRING(v_now),
    ',"consumption_id":', TO_JSON_STRING(v_consumption_id),
    ',"contract_version":', TO_JSON_STRING('daily_execution_result_v1'),
    ',"derivation_id":', TO_JSON_STRING(v_derivation_id),
    ',"effect_state":', TO_JSON_STRING(v_effect_state),
    ',"execution_name":', TO_JSON_STRING(v_consumption.execution_name),
    ',"execution_observation_sha256":', TO_JSON_STRING(v_observation_digest),
    ',"manifest_sha256":', TO_JSON_STRING(v_consumption.manifest_sha256),
    ',"operation":', TO_JSON_STRING(v_consumption.operation),
    ',"operation_context_sha256":', TO_JSON_STRING(v_derivation.operation_context_sha256),
    ',"operation_payload":', canonical_payload_json,
    ',"payload_contract_version":', TO_JSON_STRING(JSON_VALUE(canonical_payload_json,'$.contract_version')),
    ',"payload_digest":', TO_JSON_STRING(v_payload_digest),
    ',"result_reference":', TO_JSON_STRING(v_result_reference),
    ',"spend_state":', TO_JSON_STRING(v_spend_state),
    ',"stage_metering":', canonical_stage_metering_json,
    ',"terminal_state":', TO_JSON_STRING(v_terminal_state),
    '}');
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_envelope) AS 'daily_result_envelope_noncanonical';
  SET v_result_digest=LOWER(TO_HEX(SHA256(v_envelope)));
  SET v_result_id=CONCAT('exr_',v_result_digest);
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_results_v2`
    (result_contract_version,result_id,consumption_id,approval_id,manifest_sha256,operation,execution_name,result_reference,canonical_result_json,result_digest,status,completed_at,origin_registry_sha256,resource_manifest_sha256)
  VALUES('open_intelligence_execution_result_v3',v_result_id,v_consumption_id,v_derivation_id,v_consumption.manifest_sha256,v_consumption.operation,v_consumption.execution_name,result_reference,v_envelope,v_result_digest,terminal_state,v_now,v_consumption.origin_registry_sha256,v_consumption.resource_manifest_sha256);
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='ready',updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='consuming' AND lock_version=v_lock_version+1;
  ASSERT @@row_count=1 AS 'daily_result_concurrent_conflict';
  COMMIT TRANSACTION;
  SELECT TO_JSON(r) AS result FROM `{project}.{dataset}.open_intelligence_execution_results_v2` r WHERE r.result_id=v_result_id;
END;
