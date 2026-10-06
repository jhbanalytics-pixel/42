BEGIN
  DECLARE v_retire_origin_registry_sha256 STRING DEFAULT @retire_origin_registry_sha256;
  DECLARE v_retire_resource_manifest_sha256 STRING DEFAULT @retire_resource_manifest_sha256;
  DECLARE v_activate_origin_registry_sha256 STRING DEFAULT @activate_origin_registry_sha256;
  DECLARE v_activate_resource_manifest_sha256 STRING DEFAULT @activate_resource_manifest_sha256;
  DECLARE v_expected_lock_version_text STRING DEFAULT @expected_v2_lock_version;
  DECLARE v_activation_phrase STRING DEFAULT @activation_phrase;
  DECLARE v_actor STRING;
  DECLARE v_rotated_at TIMESTAMP;
  DECLARE v_lock_version INT64;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  BEGIN TRANSACTION;
  SET v_actor = CONCAT('usr_', LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:', SESSION_USER())))));
  ASSERT v_actor = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab' AS 'execution_approval_identity_invalid';
  SET v_rotated_at = CURRENT_TIMESTAMP();
  SET v_lock_version = SAFE_CAST(v_expected_lock_version_text AS INT64);
  ASSERT REGEXP_CONTAINS(v_retire_origin_registry_sha256, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_retire_resource_manifest_sha256, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_activate_origin_registry_sha256, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_activate_resource_manifest_sha256, r'^[0-9a-f]{64}$')
    AND (v_retire_origin_registry_sha256 != v_activate_origin_registry_sha256
      OR v_retire_resource_manifest_sha256 != v_activate_resource_manifest_sha256) AS 'execution_approval_generation_invalid';
  ASSERT v_activation_phrase = FORMAT('I rotate the 42 staging execution generation from origin registry SHA256 %s and resource manifest SHA256 %s to origin registry SHA256 %s and resource manifest SHA256 %s. Production remains unchanged.', v_retire_origin_registry_sha256, v_retire_resource_manifest_sha256, v_activate_origin_registry_sha256, v_activate_resource_manifest_sha256) AS 'execution_approval_manifest_mismatch';
  ASSERT REGEXP_CONTAINS(v_expected_lock_version_text, r'^(0|[1-9][0-9]*)$')
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name = 'open_intelligence_execution_approval_v2' AND approval_contract_version = 'open_intelligence_execution_approval_v2' AND state = 'ready' AND lock_version = v_lock_version) = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`) = 1 AS 'execution_rotation_lock_not_ready';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1`) = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1` WHERE origin_registry_sha256 = v_retire_origin_registry_sha256 AND resource_manifest_sha256 = v_retire_resource_manifest_sha256) = 1 AS 'execution_rotation_active_generation_mismatch';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` WHERE origin_registry_sha256 = v_retire_origin_registry_sha256 OR resource_manifest_sha256 = v_retire_resource_manifest_sha256) = 0
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE origin_registry_sha256 = v_retire_origin_registry_sha256 OR resource_manifest_sha256 = v_retire_resource_manifest_sha256) = 0
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v2` WHERE origin_registry_sha256 = v_retire_origin_registry_sha256 OR resource_manifest_sha256 = v_retire_resource_manifest_sha256) = 0 AS 'execution_rotation_live_records';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_registries_v1` WHERE origin_registry_sha256 = v_activate_origin_registry_sha256) = 1 AS 'execution_approval_generation_invalid';
  SET v_registry_json = (SELECT canonical_registry_json FROM `{project}.{dataset}.open_intelligence_execution_origin_registries_v1` WHERE origin_registry_sha256 = v_activate_origin_registry_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_registry_json))) = v_activate_origin_registry_sha256 AS 'execution_origin_registry_digest_mismatch';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_registry_json)
    AND SAFE.PARSE_JSON(v_registry_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_registry_json, '$.contract_version') = 'open_intelligence_execution_origin_registry_v1' AS 'execution_origin_registry_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_resource_manifests_v1` WHERE resource_manifest_sha256 = v_activate_resource_manifest_sha256 AND origin_registry_sha256 = v_activate_origin_registry_sha256) = 1 AS 'execution_approval_generation_invalid';
  SET v_resource_json = (SELECT canonical_resource_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_resource_manifests_v1` WHERE resource_manifest_sha256 = v_activate_resource_manifest_sha256 AND origin_registry_sha256 = v_activate_origin_registry_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_resource_json))) = v_activate_resource_manifest_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_resource_json)
    AND SAFE.PARSE_JSON(v_resource_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_resource_json, '$.origin_registry_sha256') = v_activate_origin_registry_sha256 AS 'execution_approval_generation_invalid';
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
  UPDATE `{project}.{dataset}.open_intelligence_execution_active_generation_v1`
  SET origin_registry_sha256 = v_activate_origin_registry_sha256, resource_manifest_sha256 = v_activate_resource_manifest_sha256
  WHERE origin_registry_sha256 = v_retire_origin_registry_sha256
    AND resource_manifest_sha256 = v_retire_resource_manifest_sha256;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
  SET lock_version = lock_version + 1, state = 'ready', updated_at = v_rotated_at
  WHERE lock_name = 'open_intelligence_execution_approval_v2'
    AND approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name = 'open_intelligence_execution_approval_v2' AND approval_contract_version = 'open_intelligence_execution_approval_v2' AND state = 'ready' AND lock_version = v_lock_version + 1) = 1 AS 'execution_approval_lock_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1`) = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1` WHERE origin_registry_sha256 = v_activate_origin_registry_sha256 AND resource_manifest_sha256 = v_activate_resource_manifest_sha256) = 1 AS 'execution_approval_generation_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_rotation_receipt_v2' AS contract_version,
         'ready' AS state, v_rotated_at AS rotated_at, v_actor AS rotated_by,
         v_lock_version + 1 AS lock_version,
         v_retire_origin_registry_sha256 AS retired_origin_registry_sha256, v_retire_resource_manifest_sha256 AS retired_resource_manifest_sha256,
         v_activate_origin_registry_sha256 AS origin_registry_sha256, v_activate_resource_manifest_sha256 AS resource_manifest_sha256;
END;
