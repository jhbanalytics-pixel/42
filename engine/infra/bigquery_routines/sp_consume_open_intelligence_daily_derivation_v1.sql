CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_consume_open_intelligence_daily_derivation_v1`(
  derivation_id STRING, execution_name STRING, canonical_operation_context_json STRING,
  canonical_operation_artifact_set_json STRING, canonical_execution_observation_json STRING,
  execution_observation_sha256 STRING
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_derivation_id STRING DEFAULT derivation_id;
  DECLARE v_execution_name STRING DEFAULT execution_name;
  DECLARE v_observation_sha256 STRING DEFAULT execution_observation_sha256;
  DECLARE v_derivation STRUCT<authorizing_grant_digest STRING,operation STRING,manifest_sha256 STRING,canonical_manifest_json STRING,operation_context_sha256 STRING,canonical_operation_context_json STRING,child_job_resource STRING,child_service_identity STRING,child_image_uri STRING,child_job_policy_digest STRING,derived_at TIMESTAMP,expires_at TIMESTAMP,origin_registry_sha256 STRING,resource_manifest_sha256 STRING,reserved_micro_usd INT64>;
  DECLARE v_consumed_at TIMESTAMP;
  DECLARE v_consumption_id STRING;
  DECLARE v_lock_version INT64;
  DECLARE v_capture_plan STRING;
  DECLARE v_capture_storage STRING;
  DECLARE v_capture_recovery STRING;
  DECLARE v_grant_json STRING;
  DECLARE v_cutoff_date DATE;
  DECLARE v_mode STRING;
  BEGIN TRANSACTION;
  SET v_derivation=(SELECT AS STRUCT authorizing_grant_digest,operation,manifest_sha256,canonical_manifest_json,operation_context_sha256,canonical_operation_context_json,child_job_resource,child_service_identity,child_image_uri,child_job_policy_digest,derived_at,expires_at,origin_registry_sha256,resource_manifest_sha256,reserved_micro_usd
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` WHERE derivation_id=v_derivation_id);
  ASSERT v_derivation IS NOT NULL AND v_derivation.expires_at>v_now AS 'daily_derivation_unavailable';
  ASSERT v_derivation.canonical_operation_context_json=canonical_operation_context_json
    AND LOWER(TO_HEX(SHA256(canonical_operation_context_json)))=v_derivation.operation_context_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_operation_artifact_set_json)
    AND LOWER(TO_HEX(SHA256(canonical_operation_artifact_set_json)))=JSON_VALUE(canonical_operation_context_json,'$.operation_artifact_set_sha256')
    AS 'daily_context_or_artifact_mismatch';
  ASSERT NOT EXISTS(SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item
    WHERE LOWER(TO_HEX(SHA256(FROM_BASE64(JSON_VALUE(item,'$.data')))))!=(SELECT JSON_VALUE(manifest_item,'$.sha256') FROM UNNEST(JSON_QUERY_ARRAY(v_derivation.canonical_manifest_json,'$.input_artifacts')) manifest_item WHERE JSON_VALUE(manifest_item,'$.name')=JSON_VALUE(item,'$.name')))
    AND TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(item,'$.name') FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item ORDER BY JSON_VALUE(item,'$.name')))=TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(item,'$.name') FROM UNNEST(JSON_QUERY_ARRAY(v_derivation.canonical_manifest_json,'$.input_artifacts')) item WHERE JSON_VALUE(item,'$.name')!='operation_context' ORDER BY JSON_VALUE(item,'$.name')))
    AS 'daily_artifact_manifest_mismatch';
  ASSERT v_derivation.operation!='daily_source_snapshot_capture' OR (
    TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(item,'$.name') FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item ORDER BY JSON_VALUE(item,'$.name')))='["build_provenance","capture_contract","capture_plan","cost_policy","daily_profile","recovery_context","source_metadata","storage_policy"]'
    AND (SELECT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(CAST(FROM_BASE64(JSON_VALUE(item,'$.data')) AS STRING)) FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item WHERE JSON_VALUE(item,'$.name')='capture_plan')
    AND (SELECT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(CAST(FROM_BASE64(JSON_VALUE(item,'$.data')) AS STRING)) FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item WHERE JSON_VALUE(item,'$.name')='storage_policy')
    AND (SELECT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(CAST(FROM_BASE64(JSON_VALUE(item,'$.data')) AS STRING)) FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item WHERE JSON_VALUE(item,'$.name')='recovery_context'))
    AS 'daily_capture_artifact_invalid';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_execution_observation_json)
    AND LOWER(TO_HEX(SHA256(canonical_execution_observation_json)))=v_observation_sha256
    AND JSON_VALUE(canonical_execution_observation_json,'$.contract_version')='daily_native_execution_observation_v1'
    AND JSON_VALUE(canonical_execution_observation_json,'$.derivation_id')=v_derivation_id
    AND JSON_VALUE(canonical_execution_observation_json,'$.execution_name')=v_execution_name
    AND JSON_VALUE(canonical_execution_observation_json,'$.authorizing_grant_digest')=v_derivation.authorizing_grant_digest
    AND JSON_VALUE(canonical_execution_observation_json,'$.child_job_resource')=v_derivation.child_job_resource
    AND JSON_VALUE(canonical_execution_observation_json,'$.child_service_identity')=SESSION_USER()
    AND JSON_VALUE(canonical_execution_observation_json,'$.child_image_uri')=v_derivation.child_image_uri
    AND JSON_VALUE(canonical_execution_observation_json,'$.child_job_policy_digest')=v_derivation.child_job_policy_digest
    AND v_derivation.derived_at<=TIMESTAMP(JSON_VALUE(canonical_execution_observation_json,'$.execution_created_at'))
    AND TIMESTAMP(JSON_VALUE(canonical_execution_observation_json,'$.execution_created_at'))<=TIMESTAMP(JSON_VALUE(canonical_execution_observation_json,'$.execution_started_at'))
    AND TIMESTAMP(JSON_VALUE(canonical_execution_observation_json,'$.execution_started_at'))<=TIMESTAMP(JSON_VALUE(canonical_execution_observation_json,'$.observed_at'))
    AND TIMESTAMP(JSON_VALUE(canonical_execution_observation_json,'$.observed_at'))<=v_now
    AS 'daily_execution_observation_invalid';
  ASSERT NOT EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_derivation_id)
    AND NOT EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` WHERE operation='recurring_grant_revocation_v2' AND manifest_sha256=v_derivation.authorizing_grant_digest)
    AS 'daily_derivation_cancelled';
  IF (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE approval_id=v_derivation_id)>0 THEN
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
      WHERE approval_id=v_derivation_id AND execution_name=v_execution_name)=1 AS 'daily_consumption_conflict';
    SET v_consumed_at=(SELECT consumed_at FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE approval_id=v_derivation_id);
    SET v_consumption_id=CONCAT('exc_',LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(v_consumed_at AS consumed_at,'open_intelligence_execution_consumption_v3' AS consumption_contract_version,v_derivation_id AS derivation_id,v_execution_name AS execution_name,v_observation_sha256 AS execution_observation_sha256,v_derivation.origin_registry_sha256 AS origin_registry_sha256,v_derivation.resource_manifest_sha256 AS resource_manifest_sha256))))));
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE approval_id=v_derivation_id AND consumption_id=v_consumption_id)=1 AS 'daily_consumption_conflict';
    COMMIT TRANSACTION;
    SELECT TO_JSON(d) AS derivation, TO_JSON(a) AS authorizing_approval,
      SAFE.PARSE_JSON(a.canonical_manifest_json,wide_number_mode=>'exact') AS grant,
      SAFE.PARSE_JSON(d.canonical_manifest_json,wide_number_mode=>'exact') AS manifest,
      SAFE.PARSE_JSON(d.canonical_operation_context_json,wide_number_mode=>'exact') AS operation_context,
      TO_JSON(c) AS consumption
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` a
      ON a.approval_id=d.authorizing_approval_id AND a.manifest_sha256=d.authorizing_grant_digest
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    WHERE c.consumption_id=v_consumption_id;
    RETURN;
  END IF;
  SET v_lock_version=(SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name='open_intelligence_execution_approval_v2' AND state='ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='consuming',lock_version=lock_version+1,updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='ready' AND lock_version=v_lock_version;
  ASSERT @@row_count=1 AS 'daily_consumption_concurrent_conflict';
  ASSERT NOT EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE execution_name=v_execution_name)
    AS 'daily_execution_already_consumed';
  IF v_derivation.operation='daily_source_snapshot_capture' THEN
    SET v_capture_plan=(SELECT CAST(FROM_BASE64(JSON_VALUE(item,'$.data')) AS STRING) FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item WHERE JSON_VALUE(item,'$.name')='capture_plan');
    SET v_capture_storage=(SELECT CAST(FROM_BASE64(JSON_VALUE(item,'$.data')) AS STRING) FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item WHERE JSON_VALUE(item,'$.name')='storage_policy');
    SET v_capture_recovery=(SELECT CAST(FROM_BASE64(JSON_VALUE(item,'$.data')) AS STRING) FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item WHERE JSON_VALUE(item,'$.name')='recovery_context');
    SET v_grant_json=(SELECT canonical_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` WHERE operation='recurring_grant_v2' AND manifest_sha256=v_derivation.authorizing_grant_digest);
    SET v_mode=JSON_VALUE(canonical_operation_context_json,'$.mode');
    SET v_cutoff_date=DATE_SUB(DATE(TIMESTAMP(JSON_VALUE(canonical_operation_context_json,'$.cutoff_utc'))),INTERVAL 1 DAY);
    ASSERT JSON_VALUE(canonical_operation_context_json,'$.source_estate_id')=JSON_VALUE(v_grant_json,'$.source_estate_id')
      AND JSON_VALUE(v_grant_json,'$.source_estate_id')='intelligence-42-core'
      AND FORMAT_DATE('%Y-%m-%d',v_cutoff_date) IN UNNEST(JSON_VALUE_ARRAY(v_grant_json,'$.permitted_cutoffs'))
      AND JSON_VALUE(v_capture_plan,'$.contract_version')='open_intelligence_protected_capture_plan_v2'
      AND JSON_VALUE(v_capture_plan,'$.cutoff_date')=FORMAT_DATE('%Y-%m-%d',v_cutoff_date)
      AND JSON_VALUE(v_capture_plan,'$.snapshot_plan.profile_version')='42_staging_source_v2'
      AND JSON_VALUE(v_capture_plan,'$.snapshot_plan.projection_version')='native_id_bound_v1'
      AND JSON_VALUE(v_capture_plan,'$.snapshot_plan.source_dataset')='intelligence_42_sources_staging'
      AND TIMESTAMP(JSON_VALUE(v_capture_plan,'$.snapshot_plan.observation_window_end'))=TIMESTAMP(JSON_VALUE(canonical_operation_context_json,'$.cutoff_utc'))
      AND TIMESTAMP(JSON_VALUE(v_capture_plan,'$.snapshot_plan.snapshot_as_of'))>=TIMESTAMP(JSON_VALUE(v_capture_plan,'$.snapshot_plan.observation_window_end'))
      AND TIMESTAMP(JSON_VALUE(v_capture_plan,'$.snapshot_plan.snapshot_as_of'))<=v_now
      AND JSON_VALUE(v_capture_plan,'$.snapshot_plan.source_estate_digest')=JSON_VALUE(v_capture_storage,'$.grant.source_estate_digest')
      AND JSON_VALUE(v_capture_storage,'$.contract_version')='open_intelligence_source_capture_storage_v2'
      AND JSON_VALUE(v_capture_storage,'$.grant.grant_id')=JSON_VALUE(v_grant_json,'$.grant_id')
      AND SAFE_CAST(JSON_VALUE(v_capture_storage,'$.grant.reserved_micro_usd_per_capture') AS INT64)=SAFE_CAST(JSON_VALUE(v_grant_json,'$.reserved_micro_usd_per_capture') AS INT64)
      AND SAFE_CAST(JSON_VALUE(v_capture_storage,'$.grant.cumulative_ceiling_micro_usd') AS INT64)=SAFE_CAST(JSON_VALUE(v_grant_json,'$.cumulative_ceiling_micro_usd') AS INT64)
      AND FORMAT_DATE('%Y-%m-%d',v_cutoff_date) IN UNNEST(JSON_VALUE_ARRAY(v_capture_storage,'$.grant.allowed_cutoffs'))
      AS 'daily_capture_semantic_mismatch';
    ASSERT (v_mode='initial' AND v_capture_recovery='null'
        AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v2` r
          JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c USING(consumption_id)
          JOIN `{project}.{dataset}.open_intelligence_execution_derivations_v1` d ON d.derivation_id=c.approval_id
          WHERE r.result_id=JSON_VALUE(v_derivation.canonical_operation_context_json,'$.predecessor_result_id')
            AND r.result_digest=JSON_VALUE(v_derivation.canonical_operation_context_json,'$.predecessor_result_digest')
            AND d.operation='daily_collection_exposure_issue'
            AND JSON_VALUE(d.canonical_operation_context_json,'$.slot_id')=JSON_VALUE(v_derivation.canonical_operation_context_json,'$.slot_id')
            AND JSON_VALUE(r.canonical_result_json,'$.terminal_state')='succeeded')=1)
      OR (v_mode='recover' AND JSON_VALUE(v_capture_recovery,'$.contract_version')='open_intelligence_daily_source_capture_recovery_v1'
        AND JSON_VALUE(v_capture_recovery,'$.initial_operation')='daily_source_snapshot_capture'
        AND JSON_VALUE(v_capture_recovery,'$.initial_result_id')=JSON_VALUE(canonical_operation_context_json,'$.predecessor_result_id')
        AND JSON_VALUE(v_capture_recovery,'$.initial_result_digest')=JSON_VALUE(canonical_operation_context_json,'$.predecessor_result_digest')
        AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v2` r
          JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c USING(consumption_id)
          JOIN `{project}.{dataset}.open_intelligence_execution_derivations_v1` d ON d.derivation_id=c.approval_id
          WHERE r.result_id=JSON_VALUE(v_capture_recovery,'$.initial_result_id')
            AND r.result_digest=JSON_VALUE(v_capture_recovery,'$.initial_result_digest')
            AND d.operation='daily_source_snapshot_capture'
            AND d.authorizing_grant_digest=v_derivation.authorizing_grant_digest
            AND JSON_VALUE(d.canonical_operation_context_json,'$.cutoff_utc')=JSON_VALUE(v_derivation.canonical_operation_context_json,'$.cutoff_utc')
            AND JSON_VALUE(r.canonical_result_json,'$.terminal_state')='failed')=1)
      AS 'daily_capture_recovery_invalid';
    IF v_mode='initial' THEN
      ASSERT (
        SELECT COUNT(*) FROM (
          SELECT a.canonical_manifest_json AS manifest_json,NULL AS context_json
          FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
          JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a ON a.approval_id=c.approval_id AND a.manifest_sha256=c.manifest_sha256
          WHERE c.operation='source_snapshot_capture'
          UNION ALL
          SELECT a.canonical_manifest_json,NULL FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c
          JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` a ON a.approval_id=c.approval_id AND a.manifest_sha256=c.manifest_sha256
          WHERE c.operation='source_snapshot_capture'
          UNION ALL
          SELECT NULL,d.canonical_operation_context_json FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c
          JOIN `{project}.{dataset}.open_intelligence_execution_derivations_v1` d ON d.derivation_id=c.approval_id
          WHERE c.operation='daily_source_snapshot_capture'
        ) WHERE (manifest_json IS NOT NULL AND JSON_VALUE(manifest_json,'$.arguments[2]')=FORMAT_DATE('%Y-%m-%d',v_cutoff_date) AND JSON_VALUE(manifest_json,'$.arguments[4]')='initial')
          OR (context_json IS NOT NULL AND JSON_VALUE(context_json,'$.cutoff_utc')=JSON_VALUE(canonical_operation_context_json,'$.cutoff_utc') AND JSON_VALUE(context_json,'$.mode')='initial')
      )=0 AS 'daily_capture_slot_consumed';
    ELSE
      ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c
        JOIN `{project}.{dataset}.open_intelligence_execution_derivations_v1` d ON d.derivation_id=c.approval_id
        WHERE c.operation='daily_source_snapshot_capture' AND d.authorizing_grant_digest=v_derivation.authorizing_grant_digest
          AND JSON_VALUE(d.canonical_operation_context_json,'$.cutoff_utc')=JSON_VALUE(canonical_operation_context_json,'$.cutoff_utc')
          AND JSON_VALUE(d.canonical_operation_context_json,'$.mode')='recover')=0
        AS 'daily_capture_recovery_consumed';
    END IF;
  END IF;
  ASSERT v_derivation.operation!='daily_source_collection' OR (
    SELECT SAFE_CAST(JSON_VALUE(v_derivation.canonical_manifest_json,'$.limits.max_credits') AS INT64)
      + IFNULL(SUM(SAFE_CAST(JSON_VALUE(d.canonical_manifest_json,'$.limits.max_credits') AS INT64)),0)
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` r ON r.consumption_id=c.consumption_id
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` t
      ON t.derivation_id=d.derivation_id AND t.reason_code='owner_reconciliation_hold'
    WHERE d.operation='daily_source_collection' AND r.consumption_id IS NULL AND t.derivation_id IS NULL
  )<=620 AS 'daily_collection_credit_exhausted';
  ASSERT v_derivation.operation!='daily_source_collection' OR (
    SELECT SAFE_CAST(JSON_VALUE(v_derivation.canonical_manifest_json,'$.limits.max_credits') AS INT64)
      + IFNULL(SUM(/* daily_collection_credit_charge_begin */CASE
        WHEN JSON_VALUE(r.canonical_result_json,'$.spend_state') IN ('measured','no_spend')
          AND JSON_VALUE(r.canonical_result_json,'$.stage_metering.complete')='true'
          AND SAFE_CAST(JSON_VALUE(r.canonical_result_json,'$.stage_metering.vendor_credits') AS NUMERIC) IS NOT NULL
        THEN CAST(CEIL(SAFE_CAST(JSON_VALUE(r.canonical_result_json,'$.stage_metering.vendor_credits') AS NUMERIC)) AS INT64)
        ELSE SAFE_CAST(JSON_VALUE(d.canonical_manifest_json,'$.limits.max_credits') AS INT64)
      END/* daily_collection_credit_charge_end */),0)
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` r ON r.consumption_id=c.consumption_id
    WHERE d.operation='daily_source_collection' AND d.authorizing_grant_digest=v_derivation.authorizing_grant_digest
      AND TIMESTAMP_TRUNC(c.consumed_at,MONTH)=TIMESTAMP_TRUNC(v_now,MONTH)
  )<=(SELECT SAFE_CAST(JSON_VALUE(a.canonical_manifest_json,'$.monthly_credit_cap') AS INT64)
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` a
    WHERE a.operation='recurring_grant_v2' AND a.manifest_sha256=v_derivation.authorizing_grant_digest)
    AS 'daily_collection_monthly_credit_exhausted';
  ASSERT v_derivation.operation!='daily_source_snapshot_capture' OR v_mode='recover' OR (
    SELECT SAFE_CAST(JSON_VALUE(v_grant_json,'$.reserved_micro_usd_per_capture') AS INT64)
      + IFNULL(SUM(SAFE_CAST(JSON_VALUE(v_grant_json,'$.reserved_micro_usd_per_capture') AS INT64)),0)
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    WHERE d.operation='daily_source_snapshot_capture' AND d.authorizing_grant_digest=v_derivation.authorizing_grant_digest
      AND JSON_VALUE(d.canonical_operation_context_json,'$.mode')='initial'
  )<=SAFE_CAST(JSON_VALUE(v_grant_json,'$.cumulative_ceiling_micro_usd') AS INT64)
    AS 'daily_capture_allowance_exhausted';
  SET v_consumed_at=v_now;
  SET v_consumption_id=CONCAT('exc_',LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(v_consumed_at AS consumed_at,'open_intelligence_execution_consumption_v3' AS consumption_contract_version,v_derivation_id AS derivation_id,v_execution_name AS execution_name,v_observation_sha256 AS execution_observation_sha256,v_derivation.origin_registry_sha256 AS origin_registry_sha256,v_derivation.resource_manifest_sha256 AS resource_manifest_sha256))))));
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
    (consumption_contract_version,consumption_id,approval_id,manifest_sha256,operation,execution_name,job_resource,source_sha,image_uri,consumed_at,origin_registry_sha256,resource_manifest_sha256)
  VALUES('open_intelligence_execution_consumption_v3',v_consumption_id,v_derivation_id,v_derivation.manifest_sha256,v_derivation.operation,v_execution_name,v_derivation.child_job_resource,JSON_VALUE(v_derivation.canonical_manifest_json,'$.source_sha'),v_derivation.child_image_uri,v_consumed_at,v_derivation.origin_registry_sha256,v_derivation.resource_manifest_sha256);
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='ready',updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='consuming' AND lock_version=v_lock_version+1;
  ASSERT @@row_count=1 AS 'daily_consumption_concurrent_conflict';
  COMMIT TRANSACTION;
  SELECT TO_JSON(d) AS derivation, TO_JSON(a) AS authorizing_approval,
      SAFE.PARSE_JSON(a.canonical_manifest_json,wide_number_mode=>'exact') AS grant,
      SAFE.PARSE_JSON(d.canonical_manifest_json,wide_number_mode=>'exact') AS manifest,
      SAFE.PARSE_JSON(d.canonical_operation_context_json,wide_number_mode=>'exact') AS operation_context,
      TO_JSON(c) AS consumption
    FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` a
      ON a.approval_id=d.authorizing_approval_id AND a.manifest_sha256=d.authorizing_grant_digest
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    WHERE c.consumption_id=v_consumption_id;
END;
