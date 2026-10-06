CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_record_open_intelligence_execution_result_v2`(
  consumption_id STRING,
  result_reference STRING,
  canonical_result_json STRING,
  result_digest STRING,
  status STRING,
  origin_registry_sha256 STRING,
  resource_manifest_sha256 STRING
)
BEGIN
  DECLARE v_consumption_id STRING DEFAULT consumption_id;
  DECLARE v_result_reference STRING DEFAULT result_reference;
  DECLARE v_canonical_result_json STRING DEFAULT canonical_result_json;
  DECLARE v_result_digest STRING DEFAULT result_digest;
  DECLARE v_status STRING DEFAULT status;
  DECLARE v_origin_registry_sha256 STRING DEFAULT origin_registry_sha256;
  DECLARE v_resource_manifest_sha256 STRING DEFAULT resource_manifest_sha256;
  DECLARE v_consumption STRUCT<approval_id STRING, manifest_sha256 STRING, operation STRING, execution_name STRING, job_resource STRING, consumed_at TIMESTAMP, origin_registry_sha256 STRING, resource_manifest_sha256 STRING>;
  DECLARE v_contract_sha256 STRING;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  DECLARE v_policy_json STRING;
  DECLARE v_origin_row STRING;
  DECLARE v_binding STRING;
  DECLARE v_completed_at TIMESTAMP;
  DECLARE v_result_id STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  SET v_completed_at = CURRENT_TIMESTAMP();
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1` WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'disabled') = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`) = 1 AS 'execution_approval_v1_lock_not_disabled';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name = 'open_intelligence_execution_approval_v2' AND approval_contract_version = 'open_intelligence_execution_approval_v2' AND state = 'ready') = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`) = 1 AS 'execution_approval_lock_not_ready';
  SET v_lock_version = (
    SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    WHERE lock_name = 'open_intelligence_execution_approval_v2'
      AND approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND state = 'ready'
  );
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
  SET lock_version = lock_version + 1, state = 'recording_result', updated_at = v_completed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v2'
    AND approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT REGEXP_CONTAINS(v_origin_registry_sha256, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_resource_manifest_sha256, r'^[0-9a-f]{64}$') AS 'execution_approval_generation_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1`) = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1` WHERE origin_registry_sha256 = v_origin_registry_sha256 AND resource_manifest_sha256 = v_resource_manifest_sha256) = 1 AS 'execution_approval_generation_inactive';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_registries_v1` WHERE origin_registry_sha256 = v_origin_registry_sha256) = 1 AS 'execution_approval_generation_invalid';
  SET v_registry_json = (SELECT canonical_registry_json FROM `{project}.{dataset}.open_intelligence_execution_origin_registries_v1` WHERE origin_registry_sha256 = v_origin_registry_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_registry_json))) = v_origin_registry_sha256 AS 'execution_origin_registry_digest_mismatch';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_registry_json)
    AND SAFE.PARSE_JSON(v_registry_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_registry_json, '$.contract_version') = 'open_intelligence_execution_origin_registry_v1' AS 'execution_origin_registry_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_resource_manifests_v1` WHERE resource_manifest_sha256 = v_resource_manifest_sha256 AND origin_registry_sha256 = v_origin_registry_sha256) = 1 AS 'execution_approval_generation_invalid';
  SET v_resource_json = (SELECT canonical_resource_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_resource_manifests_v1` WHERE resource_manifest_sha256 = v_resource_manifest_sha256 AND origin_registry_sha256 = v_origin_registry_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_resource_json))) = v_resource_manifest_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_resource_json)
    AND SAFE.PARSE_JSON(v_resource_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_resource_json, '$.origin_registry_sha256') = v_origin_registry_sha256 AS 'execution_approval_generation_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` AS open_intelligence_execution_consumptions_v2
    WHERE open_intelligence_execution_consumptions_v2.consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND open_intelligence_execution_consumptions_v2.consumption_id = v_consumption_id) = 1 AS 'execution_approval_unavailable';
  SET v_consumption = (
    SELECT AS STRUCT approval_id, manifest_sha256, operation, execution_name, job_resource, consumed_at, origin_registry_sha256, resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` AS open_intelligence_execution_consumptions_v2
    WHERE open_intelligence_execution_consumptions_v2.consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND open_intelligence_execution_consumptions_v2.consumption_id = v_consumption_id
  );
  ASSERT v_consumption.origin_registry_sha256 = v_origin_registry_sha256
    AND v_consumption.resource_manifest_sha256 = v_resource_manifest_sha256 AS 'execution_approval_generation_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS a
    WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND a.approval_id = v_consumption.approval_id
      AND a.manifest_sha256 = v_consumption.manifest_sha256
      AND a.operation = v_consumption.operation
      AND a.origin_registry_sha256 = v_origin_registry_sha256
      AND a.resource_manifest_sha256 = v_resource_manifest_sha256
      AND a.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
      AND a.manifest_sha256 = LOWER(TO_HEX(SHA256(a.canonical_manifest_json)))
      AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(a.canonical_manifest_json)
      AND JSON_VALUE(a.canonical_manifest_json, '$.operation') = a.operation
      AND JSON_VALUE(a.canonical_manifest_json, '$.job_resource') = v_consumption.job_resource
      AND a.approved_at <= v_consumption.consumed_at
      AND v_consumption.consumed_at < a.expires_at) = 1 AS 'execution_approval_manifest_mismatch';
  SET v_contract_sha256 = (SELECT a.contract_sha256 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS a
    WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND a.approval_id = v_consumption.approval_id
      AND a.manifest_sha256 = v_consumption.manifest_sha256);
  ASSERT (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256) = 1 AS 'execution_origin_pair_invalid';
  SET v_origin_row = (SELECT origin_row FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256);
  SET v_binding = CASE v_consumption.operation
    WHEN 'migration_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.migration_apply')
    WHEN 'collection_exposure_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.collection_exposure_issue')
    WHEN 'r3_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_apply')
    WHEN 'r3_proof_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_proof_issue')
    WHEN 'r3_release' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_release')
    WHEN 'brain_read' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.brain_read')
    WHEN 'wave1_pilot' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.wave1_pilot')
    ELSE NULL
  END;
  ASSERT v_binding IS NOT NULL AND JSON_VALUE(v_binding, '$.job_resource') = v_consumption.job_resource AS 'execution_origin_pair_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256) = 1 AS 'execution_origin_policy_invalid';
  SET v_policy_json = (SELECT canonical_policy_json FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_policy_json))) = v_contract_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_policy_json)
    AND SAFE.PARSE_JSON(v_policy_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_policy_json, '$.contract_version') = 'open_intelligence_execution_origin_policy_v2'
    AND JSON_VALUE(v_policy_json, '$.successor.manifest_version') = 'open_intelligence_execution_manifest_v2'
    AND v_consumption.operation IN UNNEST(JSON_VALUE_ARRAY(v_policy_json, '$.successor.operations'))
    AND JSON_VALUE(v_policy_json, '$.origin.image_uri_regex') = JSON_VALUE(v_origin_row, '$.image_uri_regex')
    AND JSON_VALUE(v_policy_json, '$.origin.connected_repository') = JSON_VALUE(v_origin_row, '$.connected_repo') AS 'execution_origin_policy_invalid';
  ASSERT SESSION_USER() = JSON_VALUE(v_binding, '$.service_identity') AS 'execution_approval_identity_invalid';
  ASSERT NULLIF(TRIM(v_result_reference), '') IS NOT NULL
    AND NORMALIZE(v_result_reference, NFC) = v_result_reference AS 'execution_approval_manifest_invalid';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_canonical_result_json) AS 'execution_approval_manifest_invalid';
  ASSERT JSON_TYPE(SAFE.PARSE_JSON(v_canonical_result_json, wide_number_mode => 'exact')) = 'object'
    AS 'execution_approval_manifest_invalid';
  ASSERT v_status IN ('succeeded', 'failed') AS 'execution_approval_manifest_invalid';
  ASSERT v_result_digest = LOWER(TO_HEX(SHA256(v_canonical_result_json))) AS 'execution_approval_manifest_mismatch';
  ASSERT v_completed_at >= v_consumption.consumed_at AS 'execution_approval_manifest_mismatch';
  SET v_result_id = CONCAT('exr_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"completed_at":"%s","consumption_id":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s","result_contract_version":"open_intelligence_execution_result_v2","result_digest":"%s","result_reference":"%s","status":"%s"}',
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_completed_at), v_consumption_id,
    v_origin_registry_sha256, v_resource_manifest_sha256,
    v_result_digest, v_result_reference, v_status
  )))));
  ASSERT (SELECT COUNT(*) FROM (
      SELECT consumption_id, result_id, result_reference FROM `{project}.{dataset}.open_intelligence_execution_results_v1`
      UNION ALL SELECT consumption_id, result_id, result_reference FROM `{project}.{dataset}.open_intelligence_execution_results_v2`
    ) AS results WHERE results.consumption_id = v_consumption_id OR results.result_id = v_result_id OR results.result_reference = v_result_reference) = 0 AS 'execution_approval_conflict';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_results_v2` VALUES (
    'open_intelligence_execution_result_v2', v_result_id, v_consumption_id,
    v_consumption.approval_id, v_consumption.manifest_sha256, v_consumption.operation,
    v_consumption.execution_name, v_result_reference, v_canonical_result_json, v_result_digest,
    v_status, v_completed_at, v_origin_registry_sha256, v_resource_manifest_sha256
  );
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v2` AS open_intelligence_execution_results_v2
    WHERE open_intelligence_execution_results_v2.result_contract_version = 'open_intelligence_execution_result_v2'
      AND open_intelligence_execution_results_v2.result_id = v_result_id
      AND open_intelligence_execution_results_v2.consumption_id = v_consumption_id
      AND open_intelligence_execution_results_v2.approval_id = v_consumption.approval_id
      AND open_intelligence_execution_results_v2.manifest_sha256 = v_consumption.manifest_sha256
      AND open_intelligence_execution_results_v2.operation = v_consumption.operation
      AND open_intelligence_execution_results_v2.execution_name = v_consumption.execution_name
      AND open_intelligence_execution_results_v2.result_reference = v_result_reference
      AND open_intelligence_execution_results_v2.canonical_result_json = v_canonical_result_json
      AND open_intelligence_execution_results_v2.result_digest = v_result_digest
      AND open_intelligence_execution_results_v2.status = v_status
      AND open_intelligence_execution_results_v2.completed_at = v_completed_at
      AND open_intelligence_execution_results_v2.origin_registry_sha256 = v_origin_registry_sha256
      AND open_intelligence_execution_results_v2.resource_manifest_sha256 = v_resource_manifest_sha256) = 1 AS 'execution_approval_schema_mismatch';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
  SET state = 'ready', updated_at = v_completed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v2'
    AND approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND state = 'recording_result' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_result_v2' AS result_contract_version,
         v_result_id AS result_id, v_consumption_id AS consumption_id,
         v_consumption.approval_id AS approval_id, v_consumption.manifest_sha256 AS manifest_sha256,
         v_completed_at AS completed_at,
         v_origin_registry_sha256 AS origin_registry_sha256, v_resource_manifest_sha256 AS resource_manifest_sha256;
END;
