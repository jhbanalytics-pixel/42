CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_execution_result_v2`(
  p_consumption_id STRING
)
BEGIN
  DECLARE v_consumption_id STRING DEFAULT p_consumption_id;
  DECLARE v_consumption STRUCT<approval_id STRING, manifest_sha256 STRING, operation STRING, execution_name STRING, job_resource STRING, origin_registry_sha256 STRING, resource_manifest_sha256 STRING>;
  DECLARE v_origin_registry_sha256 STRING;
  DECLARE v_resource_manifest_sha256 STRING;
  DECLARE v_contract_sha256 STRING;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  DECLARE v_policy_json STRING;
  DECLARE v_origin_row STRING;
  DECLARE v_binding STRING;
  DECLARE v_result_count INT64;

  ASSERT REGEXP_CONTAINS(v_consumption_id, r'^exc_[0-9a-f]{64}$')
    AS 'execution_approval_unavailable';
  ASSERT (SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
    WHERE consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND consumption_id = v_consumption_id) = 1 AS 'execution_approval_unavailable';
  ASSERT (SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
    WHERE consumption_id = v_consumption_id) = 1 AS 'execution_approval_unavailable';

  SET v_consumption = (
    SELECT AS STRUCT approval_id, manifest_sha256, operation, execution_name, job_resource, origin_registry_sha256, resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
    WHERE consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND consumption_id = v_consumption_id
  );
  SET v_origin_registry_sha256 = v_consumption.origin_registry_sha256;
  SET v_resource_manifest_sha256 = v_consumption.resource_manifest_sha256;
  ASSERT REGEXP_CONTAINS(v_origin_registry_sha256, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_resource_manifest_sha256, r'^[0-9a-f]{64}$') AS 'execution_approval_generation_invalid';
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

  ASSERT (SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS a
    WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND a.approval_id = v_consumption.approval_id
      AND a.manifest_sha256 = v_consumption.manifest_sha256
      AND a.operation = v_consumption.operation
      AND a.origin_registry_sha256 = v_origin_registry_sha256
      AND a.resource_manifest_sha256 = v_resource_manifest_sha256
      AND a.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
      AND a.manifest_sha256 = LOWER(TO_HEX(SHA256(a.canonical_manifest_json)))
      AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(a.canonical_manifest_json)
      AND SAFE.PARSE_JSON(a.canonical_manifest_json, wide_number_mode => 'exact') IS NOT NULL
      AND JSON_VALUE(a.canonical_manifest_json, '$.operation') = a.operation
      AND JSON_VALUE(a.canonical_manifest_json, '$.job_resource') = v_consumption.job_resource) = 1
    AS 'execution_result_conflict';
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
    AND v_consumption.operation IN UNNEST(JSON_VALUE_ARRAY(v_policy_json, '$.successor.operations')) AS 'execution_origin_policy_invalid';
  ASSERT SESSION_USER() = JSON_VALUE(v_binding, '$.service_identity') AS 'execution_approval_identity_invalid';

  SET v_result_count = (
    SELECT COUNT(*)
    FROM (
      SELECT consumption_id, approval_id, manifest_sha256 FROM `{project}.{dataset}.open_intelligence_execution_results_v1`
      UNION ALL SELECT consumption_id, approval_id, manifest_sha256 FROM `{project}.{dataset}.open_intelligence_execution_results_v2`
    ) AS r
    WHERE r.consumption_id = v_consumption_id
       OR r.approval_id = v_consumption.approval_id
       OR r.manifest_sha256 = v_consumption.manifest_sha256
  );
  ASSERT v_result_count <= 1 AS 'execution_result_conflict';
  ASSERT NOT EXISTS (
    SELECT 1
    FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS r
    WHERE r.consumption_id = v_consumption_id
       OR r.approval_id = v_consumption.approval_id
       OR r.manifest_sha256 = v_consumption.manifest_sha256
  ) AS 'execution_result_conflict';
  ASSERT NOT EXISTS (
    SELECT 1
    FROM `{project}.{dataset}.open_intelligence_execution_results_v2` AS r
    WHERE (r.consumption_id = v_consumption_id
        OR r.approval_id = v_consumption.approval_id
        OR r.manifest_sha256 = v_consumption.manifest_sha256)
      AND NOT (
        r.result_contract_version = 'open_intelligence_execution_result_v2'
        AND r.consumption_id = v_consumption_id
        AND r.approval_id = v_consumption.approval_id
        AND r.manifest_sha256 = v_consumption.manifest_sha256
        AND r.operation = v_consumption.operation
        AND r.execution_name = v_consumption.execution_name
        AND r.origin_registry_sha256 = v_consumption.origin_registry_sha256
        AND r.resource_manifest_sha256 = v_consumption.resource_manifest_sha256
        AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(r.canonical_result_json)
        AND JSON_TYPE(SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact')) = 'object'
        AND r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))
        AND r.status IN ('succeeded', 'failed')
        AND r.result_id = CONCAT('exr_', LOWER(TO_HEX(SHA256(FORMAT(
          '{"completed_at":"%s","consumption_id":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s","result_contract_version":"open_intelligence_execution_result_v2","result_digest":"%s","result_reference":"%s","status":"%s"}',
          FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', r.completed_at), r.consumption_id,
          r.origin_registry_sha256, r.resource_manifest_sha256,
          r.result_digest, r.result_reference, r.status
        )))))
      )
  ) AS 'execution_result_conflict';

  SELECT r.result_contract_version AS result_contract_version,
         r.result_id AS result_id,
         r.consumption_id AS consumption_id,
         r.approval_id AS approval_id,
         r.manifest_sha256 AS manifest_sha256,
         r.operation AS operation,
         r.execution_name AS execution_name,
         r.result_reference AS result_reference,
         r.canonical_result_json AS canonical_result_json,
         r.result_digest AS result_digest,
         r.status AS status,
         r.completed_at AS completed_at,
         r.origin_registry_sha256 AS origin_registry_sha256,
         r.resource_manifest_sha256 AS resource_manifest_sha256
  FROM `{project}.{dataset}.open_intelligence_execution_results_v2` AS r
  WHERE r.consumption_id = v_consumption_id
    AND r.approval_id = v_consumption.approval_id
    AND r.manifest_sha256 = v_consumption.manifest_sha256
    AND r.operation = v_consumption.operation
    AND r.execution_name = v_consumption.execution_name
    AND r.origin_registry_sha256 = v_consumption.origin_registry_sha256
    AND r.resource_manifest_sha256 = v_consumption.resource_manifest_sha256;
END;
