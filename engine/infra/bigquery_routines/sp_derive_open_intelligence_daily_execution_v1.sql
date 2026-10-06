CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_derive_open_intelligence_daily_execution_v1`(
  canonical_manifest_json STRING, manifest_sha256 STRING,
  canonical_operation_context_json STRING, operation_context_sha256 STRING,
  canonical_operation_artifact_set_json STRING, authorizing_grant_digest STRING
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_actor STRING DEFAULT SESSION_USER();
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_operation_context_sha256 STRING DEFAULT operation_context_sha256;
  DECLARE v_authorizing_grant_digest STRING DEFAULT authorizing_grant_digest;
  DECLARE v_grant STRUCT<approval_id STRING, canonical_manifest_json STRING, expires_at TIMESTAMP, origin_registry_sha256 STRING, resource_manifest_sha256 STRING>;
  DECLARE v_operation STRING DEFAULT JSON_VALUE(canonical_operation_context_json, '$.operation');
  DECLARE v_business_attempt_id STRING DEFAULT JSON_VALUE(canonical_operation_context_json, '$.business_attempt_id');
  DECLARE v_child_job STRING DEFAULT JSON_VALUE(canonical_operation_context_json, '$.child_job_resource');
  DECLARE v_id STRING;
  DECLARE v_cost_policy STRING;
  DECLARE v_reserved_micro_usd INT64;
  DECLARE v_lock_version INT64;
  DECLARE v_derivation STRUCT<canonical_operation_context_json STRING, authorizing_grant_digest STRING>;
  DECLARE v_mode STRING;
  DECLARE v_capture_recovery STRING;
  BEGIN TRANSACTION;
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_manifest_json)
    AND LOWER(TO_HEX(SHA256(canonical_manifest_json))) = v_manifest_sha256 AS 'daily_manifest_invalid';
  ASSERT SAFE_CAST(JSON_VALUE(canonical_manifest_json,'$.limits.max_bytes_billed') AS INT64) BETWEEN 0 AND 9223372036854775806
    AND SAFE_CAST(JSON_VALUE(canonical_manifest_json,'$.limits.max_credits') AS INT64) BETWEEN 0 AND 9223372036854775806
    AND SAFE_CAST(JSON_VALUE(canonical_manifest_json,'$.limits.max_model_calls') AS INT64) BETWEEN 0 AND 9223372036854775806
    AND SAFE_CAST(JSON_VALUE(canonical_manifest_json,'$.limits.max_rows_written') AS INT64) BETWEEN 0 AND 9223372036854775806
    AS 'daily_limit_unbounded';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_operation_context_json)
    AND LOWER(TO_HEX(SHA256(canonical_operation_context_json))) = v_operation_context_sha256
    AND JSON_VALUE(canonical_operation_context_json, '$.contract_version') = 'daily_operation_context_v1'
    AND JSON_VALUE(canonical_operation_context_json, '$.source_estate_id') = 'intelligence-42-core'
    AND JSON_VALUE(canonical_operation_context_json, '$.parent_principal') = v_actor
    AND JSON_VALUE(canonical_operation_context_json, '$.authorizing_grant_digest') = v_authorizing_grant_digest
    AS 'daily_context_invalid';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_operation_artifact_set_json)
    AND JSON_VALUE(canonical_operation_artifact_set_json, '$.contract_version') = 'daily_operation_artifact_set_v1'
    AND LOWER(TO_HEX(SHA256(canonical_operation_artifact_set_json))) = JSON_VALUE(canonical_operation_context_json, '$.operation_artifact_set_sha256')
    AS 'daily_artifact_set_invalid';
  SET v_cost_policy=(SELECT CAST(FROM_BASE64(JSON_VALUE(item,'$.data')) AS STRING)
    FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item WHERE JSON_VALUE(item,'$.name')='cost_policy');
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_cost_policy)
    AND ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(v_cost_policy,wide_number_mode=>'exact'),1,mode=>'strict'),',')='administrative_reservation_micro_usd,assumptions,contract_version,expires_at,job_policy_digest,operation_reservations_micro_usd,pricing_sources,reviewed_at'
    AND JSON_VALUE(v_cost_policy,'$.contract_version')='daily_workload_cost_policy_v1'
    AND JSON_VALUE(v_cost_policy,'$.job_policy_digest')=JSON_VALUE(canonical_operation_context_json,'$.child_job_policy_digest')
    AND TIMESTAMP(JSON_VALUE(v_cost_policy,'$.reviewed_at'))<=v_now
    AND TIMESTAMP(JSON_VALUE(v_cost_policy,'$.expires_at'))>v_now
    AND TIMESTAMP_DIFF(TIMESTAMP(JSON_VALUE(v_cost_policy,'$.expires_at')),TIMESTAMP(JSON_VALUE(v_cost_policy,'$.reviewed_at')),HOUR)<=24
    AS 'daily_cost_policy_invalid';
  SET v_reserved_micro_usd=SAFE_CAST(JSON_VALUE(v_cost_policy,CONCAT('$.operation_reservations_micro_usd.',v_operation)) AS INT64);
  ASSERT v_reserved_micro_usd IS NOT NULL AND v_reserved_micro_usd>=0 AS 'daily_cost_policy_invalid';
  ASSERT TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(item,'$.name') FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item ORDER BY JSON_VALUE(item,'$.name')))=
    TO_JSON_STRING(ARRAY(SELECT JSON_VALUE(item,'$.name') FROM UNNEST(JSON_QUERY_ARRAY(canonical_manifest_json,'$.input_artifacts')) item WHERE JSON_VALUE(item,'$.name')!='operation_context' ORDER BY JSON_VALUE(item,'$.name')))
    AND NOT EXISTS(SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item
      WHERE ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(item,wide_number_mode=>'exact'),1,mode=>'strict'),',')!='data,encoding,name'
        OR JSON_VALUE(item,'$.encoding')!='base64' OR FROM_BASE64(JSON_VALUE(item,'$.data')) IS NULL
        OR BYTE_LENGTH(FROM_BASE64(JSON_VALUE(item,'$.data')))>524288
        OR LOWER(TO_HEX(SHA256(FROM_BASE64(JSON_VALUE(item,'$.data')))))!=(SELECT JSON_VALUE(manifest_item,'$.sha256') FROM UNNEST(JSON_QUERY_ARRAY(canonical_manifest_json,'$.input_artifacts')) manifest_item WHERE JSON_VALUE(manifest_item,'$.name')=JSON_VALUE(item,'$.name')))
    AND (SELECT IFNULL(SUM(BYTE_LENGTH(FROM_BASE64(JSON_VALUE(item,'$.data')))),0) FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item)<=4194304
    AS 'daily_artifact_set_invalid';
  SET v_grant = (SELECT AS STRUCT approval_id, canonical_manifest_json, expires_at, origin_registry_sha256, resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    WHERE operation = 'recurring_grant_v2' AND manifest_sha256 = v_authorizing_grant_digest);
  ASSERT v_grant IS NOT NULL
    AND LOWER(TO_HEX(SHA256(v_grant.canonical_manifest_json)))=v_authorizing_grant_digest
    AND v_now >= TIMESTAMP(JSON_VALUE(v_grant.canonical_manifest_json, '$.valid_from'))
    AND v_now < v_grant.expires_at
    AND v_operation IN UNNEST(JSON_VALUE_ARRAY(v_grant.canonical_manifest_json, '$.allowed_operations'))
    AND JSON_VALUE(canonical_operation_context_json,'$.child_service_identity') IN UNNEST(JSON_VALUE_ARRAY(v_grant.canonical_manifest_json,'$.executing_principals'))
    AND (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_grant.canonical_manifest_json,'$.job_policy_bindings')) item
      WHERE JSON_VALUE(item,'$.job_resource')=v_child_job
        AND JSON_VALUE(item,'$.job_policy_sha256')=JSON_VALUE(canonical_operation_context_json,'$.child_job_policy_digest'))=1
    AND (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_grant.canonical_manifest_json,'$.build_provenance_bindings')) item
      WHERE JSON_VALUE(item,'$.image_uri')=JSON_VALUE(canonical_operation_context_json,'$.child_image_uri'))=1
    AND JSON_VALUE(canonical_manifest_json,'$.job_resource')=v_child_job
    AND JSON_VALUE(canonical_manifest_json,'$.service_identity')=JSON_VALUE(canonical_operation_context_json,'$.child_service_identity')
    AND JSON_VALUE(canonical_manifest_json,'$.image_uri')=JSON_VALUE(canonical_operation_context_json,'$.child_image_uri')
    AND NOT EXISTS (SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
      WHERE operation = 'recurring_grant_revocation_v2' AND manifest_sha256 = v_authorizing_grant_digest)
    AS 'daily_grant_inactive';
  -- A capture consume would refuse is never derived: this is the consume routine's
  -- predecessor predicate word for word (D04 builds each stage from its predecessor's
  -- output; C03 snapshots the admitted completed run).
  IF v_operation='daily_source_snapshot_capture' THEN
    SET v_derivation=STRUCT(canonical_operation_context_json, v_authorizing_grant_digest);
    SET v_mode=JSON_VALUE(canonical_operation_context_json,'$.mode');
    SET v_capture_recovery=(SELECT CAST(FROM_BASE64(JSON_VALUE(item,'$.data')) AS STRING) FROM UNNEST(JSON_QUERY_ARRAY(canonical_operation_artifact_set_json,'$.artifacts')) item WHERE JSON_VALUE(item,'$.name')='recovery_context');
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
  END IF;
  IF (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1`
      WHERE business_attempt_id = v_business_attempt_id) > 0 THEN
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1`
      WHERE business_attempt_id = v_business_attempt_id AND manifest_sha256 = v_manifest_sha256
        AND operation_context_sha256 = v_operation_context_sha256 AND authorizing_grant_digest = v_authorizing_grant_digest) = 1
      AS 'daily_derivation_conflict';
    COMMIT TRANSACTION;
    SELECT * FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1`
      WHERE business_attempt_id = v_business_attempt_id;
    RETURN;
  END IF;
  ASSERT NOT EXISTS (
    SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` t USING (derivation_id)
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id = d.derivation_id
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` r ON r.consumption_id = c.consumption_id
    WHERE d.child_job_resource = v_child_job AND ( /* daily_unresolved_predicate_begin */
      (c.consumption_id IS NULL AND t.derivation_id IS NULL)
      OR (c.consumption_id IS NOT NULL AND r.result_id IS NULL AND COALESCE(t.reason_code,'none')!='owner_reconciliation_hold')
        OR (r.result_id IS NOT NULL AND (COALESCE(JSON_VALUE(r.canonical_result_json,'$.effect_state'),'unknown')='unknown'
          OR COALESCE(JSON_VALUE(r.canonical_result_json,'$.spend_state'),'unknown')='unknown'
          OR COALESCE(JSON_VALUE(r.canonical_result_json,'$.stage_metering.complete'),'false')!='true'))
    ) /* daily_unresolved_predicate_end */
  ) AS 'daily_derivation_unresolved';
  SET v_lock_version = (SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    WHERE lock_name = 'open_intelligence_execution_approval_v2' AND state = 'ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state = 'approving', lock_version = lock_version + 1, updated_at = v_now
    WHERE lock_name = 'open_intelligence_execution_approval_v2' AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'daily_derivation_concurrent_conflict';
  ASSERT v_reserved_micro_usd<=SAFE_CAST(JSON_VALUE(v_grant.canonical_manifest_json,'$.run_allowance_micro_usd') AS INT64)
    AND v_reserved_micro_usd+(SELECT IFNULL(SUM(d.reserved_micro_usd),0) FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
      LEFT JOIN `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` t USING(derivation_id)
      LEFT JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
      LEFT JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` r ON r.consumption_id=c.consumption_id
      WHERE d.authorizing_grant_digest=v_authorizing_grant_digest AND (t.reason_code IS NULL OR t.reason_code='owner_reconciliation_hold')
        AND (r.result_id IS NULL OR JSON_VALUE(r.canonical_result_json,'$.spend_state')!='no_spend'))
      <=SAFE_CAST(JSON_VALUE(v_grant.canonical_manifest_json,'$.monthly_allowance_micro_usd') AS INT64)
    AS 'daily_monetary_allowance_exhausted';
  SET v_id = CONCAT('exd_', LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(
    'open_intelligence_execution_derivation_v1' AS derivation_contract_version, v_grant.approval_id AS authorizing_approval_id,
    v_authorizing_grant_digest AS authorizing_grant_digest, v_manifest_sha256 AS manifest_sha256,
    v_operation_context_sha256 AS operation_context_sha256, v_business_attempt_id AS business_attempt_id,
    v_child_job AS child_job_resource, v_grant.origin_registry_sha256 AS origin_registry_sha256,
    v_grant.resource_manifest_sha256 AS resource_manifest_sha256))))));
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_derivations_v1`
    (derivation_contract_version,derivation_id,authorizing_approval_id,authorizing_grant_digest,manifest_version,operation,contract_sha256,manifest_sha256,canonical_manifest_json,operation_context_sha256,canonical_operation_context_json,business_attempt_id,child_job_resource,child_service_identity,child_image_uri,child_job_policy_digest,derived_by,origin_registry_sha256,resource_manifest_sha256,cost_policy_sha256,derived_at,expires_at,reserved_micro_usd)
  VALUES ('open_intelligence_execution_derivation_v1',v_id,v_grant.approval_id,v_authorizing_grant_digest,
    JSON_VALUE(canonical_manifest_json,'$.manifest_version'),v_operation,JSON_VALUE(canonical_manifest_json,'$.contract_sha256'),v_manifest_sha256,canonical_manifest_json,v_operation_context_sha256,canonical_operation_context_json,v_business_attempt_id,v_child_job,JSON_VALUE(canonical_operation_context_json,'$.child_service_identity'),JSON_VALUE(canonical_operation_context_json,'$.child_image_uri'),JSON_VALUE(canonical_operation_context_json,'$.child_job_policy_digest'),v_actor,v_grant.origin_registry_sha256,v_grant.resource_manifest_sha256,(SELECT JSON_VALUE(item,'$.sha256') FROM UNNEST(JSON_QUERY_ARRAY(canonical_manifest_json,'$.input_artifacts')) item WHERE JSON_VALUE(item,'$.name')='cost_policy'),v_now,LEAST(v_grant.expires_at,TIMESTAMP_ADD(v_now,INTERVAL 1 HOUR)),v_reserved_micro_usd);
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` SET state='ready',updated_at=v_now
    WHERE lock_name='open_intelligence_execution_approval_v2' AND state='approving' AND lock_version=v_lock_version+1;
  ASSERT @@row_count = 1 AS 'daily_derivation_concurrent_conflict';
  COMMIT TRANSACTION;
  SELECT * FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` WHERE derivation_id=v_id;
END;
