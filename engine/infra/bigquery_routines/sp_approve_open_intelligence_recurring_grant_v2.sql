CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_approve_open_intelligence_recurring_grant_v2`(
  canonical_grant_json STRING, grant_sha256 STRING, approval_phrase STRING,
  origin_registry_sha256 STRING, resource_manifest_sha256 STRING
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_actor STRING DEFAULT SESSION_USER();
  DECLARE v_grant_sha256 STRING DEFAULT grant_sha256;
  DECLARE v_origin_registry_sha256 STRING DEFAULT origin_registry_sha256;
  DECLARE v_resource_manifest_sha256 STRING DEFAULT resource_manifest_sha256;
  DECLARE v_existing STRUCT<approval_id STRING, approved_at TIMESTAMP>;
  DECLARE v_approval_id STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  ASSERT CONCAT('usr_',LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:',v_actor)))))='usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
    AS 'recurring_grant_actor_mismatch';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_grant_json)
    AND LOWER(TO_HEX(SHA256(canonical_grant_json))) = v_grant_sha256
    AND REGEXP_CONTAINS(v_grant_sha256, r'^[0-9a-f]{64}$') AS 'recurring_grant_invalid';
  ASSERT ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(canonical_grant_json, wide_number_mode => 'exact'), 1, mode => 'strict'), ',') =
    'allowed_operations,build_provenance_bindings,contract_version,cumulative_ceiling_micro_usd,environment,executing_principals,grant_id,issuing_principal,job_policy_bindings,monthly_allowance_micro_usd,monthly_credit_cap,permitted_cutoffs,release_policy_digest,reserved_micro_usd_per_capture,resource_manifest_digest,retry_rules,run_allowance_micro_usd,run_credit_cap,schema_version,source_estate_id,source_policy_digest,valid_from,valid_until'
    AS 'recurring_grant_invalid';
  ASSERT JSON_VALUE(canonical_grant_json, '$.contract_version') = '42_recurring_execution_grant_v2'
    AND JSON_VALUE(canonical_grant_json, '$.environment') = 'staging'
    AND JSON_VALUE(canonical_grant_json, '$.schema_version') = '42_daily_v2'
    AND JSON_VALUE(canonical_grant_json, '$.source_estate_id') = 'intelligence-42-core'
    AND JSON_VALUE(canonical_grant_json, '$.issuing_principal') = v_actor
    AS 'recurring_grant_actor_mismatch';
  ASSERT approval_phrase = FORMAT('Approve recurring execution grant v2 %s', v_grant_sha256)
    AS 'recurring_grant_phrase_mismatch';
  ASSERT SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.run_credit_cap') AS INT64) >= 0
    AND SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.monthly_credit_cap') AS INT64) >= SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.run_credit_cap') AS INT64)
    AND SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.run_allowance_micro_usd') AS INT64) >= 0
    AND SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.monthly_allowance_micro_usd') AS INT64) >= SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.run_allowance_micro_usd') AS INT64)
    AND SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.reserved_micro_usd_per_capture') AS INT64) >= 0
    AND SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.cumulative_ceiling_micro_usd') AS INT64) >= SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.reserved_micro_usd_per_capture') AS INT64)
    AS 'recurring_grant_invalid';
  ASSERT NOT EXISTS (SELECT 1 FROM UNNEST(JSON_VALUE_ARRAY(canonical_grant_json, '$.allowed_operations')) value
    WHERE value NOT IN ('daily_source_collection','daily_collection_exposure_issue','daily_source_snapshot_capture','daily_composition_apply','daily_quality_proof_issue','daily_staging_release'))
    AND TO_JSON_STRING(JSON_VALUE_ARRAY(canonical_grant_json, '$.allowed_operations')) = TO_JSON_STRING(ARRAY(SELECT DISTINCT value FROM UNNEST(JSON_VALUE_ARRAY(canonical_grant_json, '$.allowed_operations')) value ORDER BY value))
    AS 'recurring_grant_invalid';
  ASSERT ARRAY_LENGTH(JSON_QUERY_ARRAY(canonical_grant_json,'$.job_policy_bindings'))>0
    AND NOT EXISTS(SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(canonical_grant_json,'$.job_policy_bindings')) item
      WHERE ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(item,wide_number_mode=>'exact'),1,mode=>'strict'),',')!='job_policy_sha256,job_resource'
        OR NOT REGEXP_CONTAINS(JSON_VALUE(item,'$.job_policy_sha256'),r'^[0-9a-f]{64}$'))
    AND TO_JSON_STRING(JSON_QUERY_ARRAY(canonical_grant_json,'$.job_policy_bindings'))=TO_JSON_STRING(ARRAY(SELECT DISTINCT item FROM UNNEST(JSON_QUERY_ARRAY(canonical_grant_json,'$.job_policy_bindings')) item ORDER BY item))
    AND ARRAY_LENGTH(JSON_QUERY_ARRAY(canonical_grant_json,'$.build_provenance_bindings'))>0
    AND NOT EXISTS(SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(canonical_grant_json,'$.build_provenance_bindings')) item
      WHERE ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(item,wide_number_mode=>'exact'),1,mode=>'strict'),',')!='build_id,image_digest,image_uri,provenance_record_sha256,receipt_sha256,source_sha'
        OR NOT REGEXP_CONTAINS(JSON_VALUE(item,'$.image_digest'),r'^[0-9a-f]{64}$')
        OR NOT ENDS_WITH(JSON_VALUE(item,'$.image_uri'),CONCAT('@sha256:',JSON_VALUE(item,'$.image_digest')))
        OR NOT REGEXP_CONTAINS(JSON_VALUE(item,'$.source_sha'),r'^[0-9a-f]{40}$')
        OR NOT REGEXP_CONTAINS(JSON_VALUE(item,'$.receipt_sha256'),r'^[0-9a-f]{64}$')
        OR NOT REGEXP_CONTAINS(JSON_VALUE(item,'$.provenance_record_sha256'),r'^[0-9a-f]{64}$'))
    AND (SELECT COUNT(DISTINCT JSON_VALUE(item,'$.image_digest')) FROM UNNEST(JSON_QUERY_ARRAY(canonical_grant_json,'$.build_provenance_bindings')) item)=ARRAY_LENGTH(JSON_QUERY_ARRAY(canonical_grant_json,'$.build_provenance_bindings'))
    AND (SELECT COUNT(DISTINCT JSON_VALUE(item,'$.provenance_record_sha256')) FROM UNNEST(JSON_QUERY_ARRAY(canonical_grant_json,'$.build_provenance_bindings')) item)=ARRAY_LENGTH(JSON_QUERY_ARRAY(canonical_grant_json,'$.build_provenance_bindings'))
    AS 'recurring_grant_invalid';
  ASSERT ('daily_source_collection' NOT IN UNNEST(JSON_VALUE_ARRAY(canonical_grant_json, '$.allowed_operations'))
    OR SAFE_CAST(JSON_VALUE(canonical_grant_json, '$.run_credit_cap') AS INT64) = 620) AS 'recurring_grant_invalid';
  ASSERT TIMESTAMP(JSON_VALUE(canonical_grant_json, '$.valid_from')) < TIMESTAMP(JSON_VALUE(canonical_grant_json, '$.valid_until'))
    AND TIMESTAMP(JSON_VALUE(canonical_grant_json, '$.valid_until')) > v_now AS 'recurring_grant_expired';
  ASSERT JSON_VALUE(canonical_grant_json, '$.resource_manifest_digest') = v_resource_manifest_sha256
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1`
      WHERE origin_registry_sha256 = v_origin_registry_sha256 AND resource_manifest_sha256 = v_resource_manifest_sha256) = 1
    AS 'recurring_grant_generation_mismatch';
  SET v_existing = (SELECT AS STRUCT approval_id, approved_at
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    WHERE operation = 'recurring_grant_v2' AND manifest_sha256 = v_grant_sha256 LIMIT 1);
  IF v_existing IS NOT NULL THEN
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
      WHERE operation = 'recurring_grant_v2' AND manifest_sha256 = v_grant_sha256
        AND canonical_manifest_json = canonical_grant_json AND approved_by = v_actor
        AND origin_registry_sha256 = v_origin_registry_sha256 AND resource_manifest_sha256 = v_resource_manifest_sha256) = 1
      AS 'recurring_grant_conflict';
    COMMIT TRANSACTION;
    SELECT v_existing.approval_id AS approval_id, v_existing.approved_at AS approved_at, v_grant_sha256 AS grant_sha256;
    RETURN;
  END IF;
  SET v_lock_version = (SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    WHERE lock_name = 'open_intelligence_execution_approval_v2' AND state = 'ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    SET state = 'approving', lock_version = lock_version + 1, updated_at = v_now
    WHERE lock_name = 'open_intelligence_execution_approval_v2' AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'recurring_grant_conflict';
  SET v_approval_id = CONCAT('exa_', LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(
    'open_intelligence_execution_approval_v2' AS approval_contract_version, v_now AS approved_at,
    v_actor AS approved_by, v_grant_sha256 AS manifest_sha256, v_origin_registry_sha256 AS origin_registry_sha256,
    v_resource_manifest_sha256 AS resource_manifest_sha256))))));
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    (approval_contract_version, approval_id, manifest_version, operation, contract_sha256,
     manifest_sha256, canonical_manifest_json, approved_by, approved_at, expires_at,
     approval_phrase_sha256, origin_registry_sha256, resource_manifest_sha256)
  VALUES ('open_intelligence_execution_approval_v2', v_approval_id, '42_recurring_execution_grant_v2',
    'recurring_grant_v2', JSON_VALUE(canonical_grant_json, '$.release_policy_digest'), v_grant_sha256,
    canonical_grant_json, v_actor, v_now, TIMESTAMP(JSON_VALUE(canonical_grant_json, '$.valid_until')),
    LOWER(TO_HEX(SHA256(approval_phrase))), v_origin_registry_sha256, v_resource_manifest_sha256);
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    SET state = 'ready', last_approval_id = v_approval_id, updated_at = v_now
    WHERE lock_name = 'open_intelligence_execution_approval_v2' AND state = 'approving' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'recurring_grant_conflict';
  COMMIT TRANSACTION;
  SELECT v_approval_id AS approval_id, v_now AS approved_at, v_grant_sha256 AS grant_sha256;
END;
