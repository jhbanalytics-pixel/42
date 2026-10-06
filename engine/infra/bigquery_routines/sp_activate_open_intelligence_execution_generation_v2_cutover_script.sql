BEGIN
  DECLARE v_origin_registry_sha256 STRING DEFAULT @origin_registry_sha256;
  DECLARE v_resource_manifest_sha256 STRING DEFAULT @resource_manifest_sha256;
  DECLARE v_residual_json STRING DEFAULT @residual_consumption_ids_json;
  DECLARE v_residual_set_sha256 STRING DEFAULT @residual_set_sha256;
  DECLARE v_activation_phrase STRING DEFAULT @activation_phrase;
  DECLARE v_actor STRING;
  DECLARE v_activated_at TIMESTAMP;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  DECLARE v_residual_ids ARRAY<STRING>;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  SET v_actor = CONCAT('usr_', LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:', SESSION_USER())))));
  ASSERT v_actor = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab' AS 'execution_approval_identity_invalid';
  SET v_activated_at = CURRENT_TIMESTAMP();
  ASSERT REGEXP_CONTAINS(v_origin_registry_sha256, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_resource_manifest_sha256, r'^[0-9a-f]{64}$') AS 'execution_approval_generation_invalid';
  ASSERT v_activation_phrase = FORMAT('I activate one 42 staging execution generation for origin registry SHA256 %s and resource manifest SHA256 %s. Production remains unchanged.', v_origin_registry_sha256, v_resource_manifest_sha256) AS 'execution_approval_manifest_mismatch';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1` WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'disabled') = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`) = 1 AS 'execution_approval_v1_lock_not_disabled';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name = 'open_intelligence_execution_approval_v2' AND approval_contract_version = 'open_intelligence_execution_approval_v2' AND state = 'prepared') = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`) = 1 AS 'execution_approval_lock_not_prepared';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1`) = 0 AS 'execution_approval_generation_already_active';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`) = 0
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`) = 0
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v2`) = 0 AS 'execution_approval_generation_invalid';
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
  ASSERT NOT EXISTS (
    SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` AS policies
           WHERE policies.contract_sha256 = JSON_VALUE(origin_row, '$.contract_sha256')
             AND policies.contract_sha256 = LOWER(TO_HEX(SHA256(policies.canonical_policy_json)))
             AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(policies.canonical_policy_json)
             AND SAFE.PARSE_JSON(policies.canonical_policy_json, wide_number_mode => 'exact') IS NOT NULL
             AND JSON_VALUE(policies.canonical_policy_json, '$.contract_version') = 'open_intelligence_execution_origin_policy_v2'
             AND JSON_VALUE(policies.canonical_policy_json, '$.origin.image_uri_regex') = JSON_VALUE(origin_row, '$.image_uri_regex')
             AND JSON_VALUE(policies.canonical_policy_json, '$.origin.connected_repository') = JSON_VALUE(origin_row, '$.connected_repo')) != 1
  ) AS 'execution_origin_policy_invalid';
  SET v_residual_ids = JSON_VALUE_ARRAY(v_residual_json);
  ASSERT v_residual_ids IS NOT NULL
    AND REGEXP_CONTAINS(v_residual_set_sha256, r'^[0-9a-f]{64}$')
    AND TO_JSON_STRING(ARRAY(SELECT residual_id FROM UNNEST(v_residual_ids) residual_id ORDER BY residual_id)) = v_residual_json
    AND ARRAY_LENGTH(v_residual_ids) = (SELECT COUNT(DISTINCT residual_id) FROM UNNEST(v_residual_ids) residual_id)
    AND NOT EXISTS (SELECT 1 FROM UNNEST(v_residual_ids) residual_id WHERE NOT REGEXP_CONTAINS(residual_id, r'^exc_[0-9a-f]{64}$'))
    AND LOWER(TO_HEX(SHA256(v_residual_json))) = v_residual_set_sha256 AS 'execution_approval_residual_invalid';
  ASSERT NOT EXISTS (
    SELECT 1 FROM UNNEST(v_residual_ids) residual_id
    WHERE (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS c WHERE c.consumption_id = residual_id) != 1
  ) AS 'execution_approval_residual_invalid';
  ASSERT NOT EXISTS (
    SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS a
    WHERE a.expires_at > v_activated_at
      AND NOT EXISTS (SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS c WHERE c.approval_id = a.approval_id AND c.manifest_sha256 = a.manifest_sha256)
  ) AS 'execution_approval_residual_invalid';
  ASSERT NOT EXISTS (
    SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS c
    WHERE c.consumption_id NOT IN UNNEST(v_residual_ids)
      AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS r
           WHERE r.consumption_id = c.consumption_id
             AND r.approval_id = c.approval_id
             AND r.manifest_sha256 = c.manifest_sha256
             AND r.operation = c.operation
             AND r.execution_name = c.execution_name
             AND r.status IN ('succeeded', 'failed')
             AND JSON_TYPE(SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact')) = 'object'
             AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(r.canonical_result_json)
             AND r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))) != 1
  ) AS 'execution_approval_residual_invalid';
  ASSERT NOT EXISTS (
    SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS r
    WHERE (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS c WHERE c.consumption_id = r.consumption_id) != 1
       OR (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS other WHERE other.consumption_id = r.consumption_id OR other.result_id = r.result_id OR other.result_reference = r.result_reference) != 1
  ) AS 'execution_approval_residual_invalid';
  SET v_lock_version = (
    SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    WHERE lock_name = 'open_intelligence_execution_approval_v2'
      AND approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND state = 'prepared'
  );
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_active_generation_v1` (origin_registry_sha256, resource_manifest_sha256)
  VALUES (v_origin_registry_sha256, v_resource_manifest_sha256);
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1`) = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1` WHERE origin_registry_sha256 = v_origin_registry_sha256 AND resource_manifest_sha256 = v_resource_manifest_sha256) = 1 AS 'execution_approval_generation_invalid';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
  SET lock_version = lock_version + 1, state = 'ready', updated_at = v_activated_at
  WHERE lock_name = 'open_intelligence_execution_approval_v2'
    AND approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND state = 'prepared' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name = 'open_intelligence_execution_approval_v2' AND approval_contract_version = 'open_intelligence_execution_approval_v2' AND state = 'ready' AND lock_version = v_lock_version + 1) = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_activation_receipt_v2' AS contract_version,
         'ready' AS state, v_activated_at AS activated_at, v_actor AS activated_by,
         v_lock_version + 1 AS lock_version,
         v_origin_registry_sha256 AS origin_registry_sha256, v_resource_manifest_sha256 AS resource_manifest_sha256;
END;
