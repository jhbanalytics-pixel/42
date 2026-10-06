CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_consume_open_intelligence_execution_v2`(
  manifest_sha256 STRING,
  execution_name STRING,
  job_resource STRING,
  source_sha STRING,
  image_uri STRING,
  origin_registry_sha256 STRING,
  resource_manifest_sha256 STRING
)
BEGIN
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_execution_name STRING DEFAULT execution_name;
  DECLARE v_job_resource STRING DEFAULT job_resource;
  DECLARE v_source_sha STRING DEFAULT source_sha;
  DECLARE v_image_uri STRING DEFAULT image_uri;
  DECLARE v_origin_registry_sha256 STRING DEFAULT origin_registry_sha256;
  DECLARE v_resource_manifest_sha256 STRING DEFAULT resource_manifest_sha256;
  DECLARE v_approval STRUCT<approval_id STRING, operation STRING, contract_sha256 STRING, canonical_manifest_json STRING, approved_by STRING, approved_at TIMESTAMP, expires_at TIMESTAMP, approval_phrase_sha256 STRING, origin_registry_sha256 STRING, resource_manifest_sha256 STRING>;
  DECLARE v_contract_sha256 STRING;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  DECLARE v_policy_json STRING;
  DECLARE v_origin_row STRING;
  DECLARE v_binding STRING;
  DECLARE v_consumed_at TIMESTAMP;
  DECLARE v_consumption_id STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
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
  SET lock_version = lock_version + 1, state = 'consuming', updated_at = CURRENT_TIMESTAMP()
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
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
    WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256) = 1 AS 'execution_approval_unavailable';
  SET v_approval = (
    SELECT AS STRUCT approval_id, operation, contract_sha256, canonical_manifest_json, approved_by, approved_at, expires_at, approval_phrase_sha256, origin_registry_sha256, resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
    WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
  );
  SET v_consumed_at = CURRENT_TIMESTAMP();
  ASSERT v_approval.origin_registry_sha256 = v_origin_registry_sha256
    AND v_approval.resource_manifest_sha256 = v_resource_manifest_sha256 AS 'execution_approval_generation_invalid';
  SET v_contract_sha256 = v_approval.contract_sha256;
  ASSERT (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256) = 1 AS 'execution_origin_pair_invalid';
  SET v_origin_row = (SELECT origin_row FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256);
  ASSERT 'new_consume' IN UNNEST(JSON_VALUE_ARRAY(v_origin_row, '$.allowed_execution_modes')) AS 'execution_origin_pair_invalid';
  SET v_binding = CASE v_approval.operation
    WHEN 'migration_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.migration_apply')
    WHEN 'collection_exposure_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.collection_exposure_issue')
    WHEN 'r3_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_apply')
    WHEN 'r3_proof_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_proof_issue')
    WHEN 'r3_release' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_release')
    WHEN 'brain_read' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.brain_read')
    WHEN 'wave1_pilot' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.wave1_pilot')
    ELSE NULL
  END;
  ASSERT v_binding IS NOT NULL AS 'execution_origin_pair_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256) = 1 AS 'execution_origin_policy_invalid';
  SET v_policy_json = (SELECT canonical_policy_json FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_policy_json))) = v_contract_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_policy_json)
    AND SAFE.PARSE_JSON(v_policy_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_policy_json, '$.contract_version') = 'open_intelligence_execution_origin_policy_v2'
    AND JSON_VALUE(v_policy_json, '$.successor.manifest_version') = 'open_intelligence_execution_manifest_v2'
    AND v_approval.operation IN UNNEST(JSON_VALUE_ARRAY(v_policy_json, '$.successor.operations'))
    AND JSON_VALUE(v_policy_json, '$.origin.image_uri_regex') = JSON_VALUE(v_origin_row, '$.image_uri_regex')
    AND JSON_VALUE(v_policy_json, '$.origin.connected_repository') = JSON_VALUE(v_origin_row, '$.connected_repo') AS 'execution_origin_policy_invalid';
  ASSERT SESSION_USER() = JSON_VALUE(v_binding, '$.service_identity') AS 'execution_approval_identity_invalid';
  ASSERT v_approval.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_approval.canonical_manifest_json)
    AND SAFE.PARSE_JSON(v_approval.canonical_manifest_json, wide_number_mode => 'exact') IS NOT NULL
    AND v_manifest_sha256 = LOWER(TO_HEX(SHA256(v_approval.canonical_manifest_json)))
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.operation') = v_approval.operation
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.contract_sha256') = v_contract_sha256
    AND v_approval.approval_phrase_sha256 = LOWER(TO_HEX(SHA256(FORMAT('I approve one 42 staging execution of %s for manifest SHA256 %s, origin registry SHA256 %s and resource manifest SHA256 %s. Production remains unchanged.', v_approval.operation, v_manifest_sha256, v_origin_registry_sha256, v_resource_manifest_sha256))))
    AND CONCAT('exa_', LOWER(TO_HEX(SHA256(FORMAT(
      '{"approval_contract_version":"open_intelligence_execution_approval_v2","approved_at":"%s","approved_by":"%s","manifest_sha256":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}',
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_approval.approved_at), v_approval.approved_by, v_manifest_sha256,
      v_origin_registry_sha256, v_resource_manifest_sha256
    ))))) = v_approval.approval_id
    AS 'execution_approval_manifest_mismatch';
  ASSERT JSON_VALUE(v_approval.canonical_manifest_json, '$.job_resource') = v_job_resource
    AND JSON_VALUE(v_binding, '$.job_resource') = v_job_resource
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.service_identity') = JSON_VALUE(v_binding, '$.service_identity')
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.source_sha') = v_source_sha
    AND REGEXP_CONTAINS(v_source_sha, JSON_VALUE(v_policy_json, '$.common_manifest_validation.source_sha_regex'))
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.image_uri') = v_image_uri
    AND REGEXP_CONTAINS(v_image_uri, JSON_VALUE(v_origin_row, '$.image_uri_regex'))
    AND STARTS_WITH(v_execution_name, CONCAT(v_job_resource, '/executions/'))
    AND LENGTH(v_execution_name) > LENGTH(CONCAT(v_job_resource, '/executions/'))
    AND NOT REGEXP_CONTAINS(SUBSTR(v_execution_name, LENGTH(CONCAT(v_job_resource, '/executions/')) + 1), '/')
    AS 'execution_approval_execution_mismatch';
  ASSERT v_consumed_at < v_approval.expires_at AS 'execution_approval_expired';
  SET v_consumption_id = CONCAT('exc_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"approval_id":"%s","consumed_at":"%s","consumption_contract_version":"open_intelligence_execution_consumption_v2","execution_name":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}',
    v_approval.approval_id, FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_consumed_at),
    v_execution_name, v_origin_registry_sha256, v_resource_manifest_sha256
  )))));
  ASSERT (SELECT COUNT(*) FROM (
      SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1`
      UNION ALL SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
    ) AS consumptions WHERE consumptions.approval_id = v_approval.approval_id OR consumptions.manifest_sha256 = v_manifest_sha256 OR consumptions.consumption_id = v_consumption_id OR consumptions.execution_name = v_execution_name) = 0 AS 'execution_approval_consumed';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v2` VALUES (
    'open_intelligence_execution_consumption_v2', v_consumption_id, v_approval.approval_id,
    v_manifest_sha256, v_approval.operation, v_execution_name, v_job_resource, v_source_sha, v_image_uri,
    v_consumed_at, v_origin_registry_sha256, v_resource_manifest_sha256
  );
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` AS open_intelligence_execution_consumptions_v2
    WHERE open_intelligence_execution_consumptions_v2.consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND open_intelligence_execution_consumptions_v2.consumption_id = v_consumption_id
      AND open_intelligence_execution_consumptions_v2.approval_id = v_approval.approval_id
      AND open_intelligence_execution_consumptions_v2.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_consumptions_v2.operation = v_approval.operation
      AND open_intelligence_execution_consumptions_v2.execution_name = v_execution_name
      AND open_intelligence_execution_consumptions_v2.job_resource = v_job_resource
      AND open_intelligence_execution_consumptions_v2.source_sha = v_source_sha
      AND open_intelligence_execution_consumptions_v2.image_uri = v_image_uri
      AND open_intelligence_execution_consumptions_v2.consumed_at = v_consumed_at
      AND open_intelligence_execution_consumptions_v2.origin_registry_sha256 = v_origin_registry_sha256
      AND open_intelligence_execution_consumptions_v2.resource_manifest_sha256 = v_resource_manifest_sha256) = 1 AS 'execution_approval_schema_mismatch';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
  SET state = 'ready', updated_at = v_consumed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v2'
    AND approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND state = 'consuming' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_consumption_v2' AS consumption_contract_version,
         v_consumption_id AS consumption_id, v_approval.approval_id AS approval_id,
         v_manifest_sha256 AS manifest_sha256, v_approval.operation AS operation,
         v_execution_name AS execution_name, v_consumed_at AS consumed_at,
         v_origin_registry_sha256 AS origin_registry_sha256, v_resource_manifest_sha256 AS resource_manifest_sha256;
END;
