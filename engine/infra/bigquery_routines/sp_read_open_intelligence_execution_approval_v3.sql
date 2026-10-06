CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_execution_approval_v3`(
  manifest_sha256 STRING
)
BEGIN
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_approval STRUCT<approval_id STRING, operation STRING, contract_sha256 STRING, canonical_manifest_json STRING, approved_by STRING, approved_at TIMESTAMP, expires_at TIMESTAMP, approval_phrase_sha256 STRING, origin_registry_sha256 STRING, resource_manifest_sha256 STRING>;
  DECLARE v_origin_registry_sha256 STRING;
  DECLARE v_resource_manifest_sha256 STRING;
  DECLARE v_contract_sha256 STRING;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  DECLARE v_policy_json STRING;
  DECLARE v_origin_row STRING;
  DECLARE v_binding STRING;
  DECLARE v_rule STRING;
  DECLARE v_grant_rows INT64;
  DECLARE v_revocations INT64;
  DECLARE v_revocation STRUCT<approval_id STRING, contract_sha256 STRING, canonical_manifest_json STRING, approved_by STRING, approved_at TIMESTAMP, approval_phrase_sha256 STRING, origin_registry_sha256 STRING, resource_manifest_sha256 STRING>;
  SET v_grant_rows = (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
    WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_approvals_v2.operation = 'recurring_grant_v1');
  IF v_grant_rows = 0 THEN
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
      WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
        AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256) = 1 AS 'execution_approval_unavailable';
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
      WHERE open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256) = 1 AS 'execution_approval_unavailable';
    SET v_approval = (
      SELECT AS STRUCT approval_id, operation, contract_sha256, canonical_manifest_json, approved_by, approved_at, expires_at, approval_phrase_sha256, origin_registry_sha256, resource_manifest_sha256
      FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
      WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
        AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
    );
    SET v_origin_registry_sha256 = v_approval.origin_registry_sha256;
    SET v_resource_manifest_sha256 = v_approval.resource_manifest_sha256;
    SET v_contract_sha256 = v_approval.contract_sha256;
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
    ASSERT (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
      WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
        AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256) = 1 AS 'execution_origin_pair_invalid';
    SET v_origin_row = (SELECT origin_row FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
      WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
        AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256);
    SET v_binding = CASE v_approval.operation
      WHEN 'migration_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.migration_apply')
      WHEN 'collection_exposure_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.collection_exposure_issue')
      WHEN 'r3_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_apply')
      WHEN 'r3_proof_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_proof_issue')
      WHEN 'r3_release' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_release')
      WHEN 'brain_read' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.brain_read')
      WHEN 'wave1_pilot' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.wave1_pilot')
      WHEN 'source_snapshot_capture' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.source_snapshot_capture')
      WHEN 'source_collection' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.source_collection')
      WHEN 'daily_composition_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.daily_composition_apply')
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
    SET v_rule = CASE v_approval.operation
      WHEN 'migration_apply' THEN JSON_QUERY(v_policy_json, '$.operation_validation.migration_apply')
      WHEN 'collection_exposure_issue' THEN JSON_QUERY(v_policy_json, '$.operation_validation.collection_exposure_issue')
      WHEN 'r3_apply' THEN JSON_QUERY(v_policy_json, '$.operation_validation.r3_apply')
      WHEN 'r3_proof_issue' THEN JSON_QUERY(v_policy_json, '$.operation_validation.r3_proof_issue')
      WHEN 'r3_release' THEN JSON_QUERY(v_policy_json, '$.operation_validation.r3_release')
      WHEN 'brain_read' THEN JSON_QUERY(v_policy_json, '$.operation_validation.brain_read')
      WHEN 'wave1_pilot' THEN JSON_QUERY(v_policy_json, '$.operation_validation.wave1_pilot')
      WHEN 'source_snapshot_capture' THEN JSON_QUERY(v_policy_json, '$.operation_validation.source_snapshot_capture')
      WHEN 'source_collection' THEN JSON_QUERY(v_policy_json, '$.operation_validation.source_collection')
      WHEN 'daily_composition_apply' THEN JSON_QUERY(v_policy_json, '$.operation_validation.daily_composition_apply')
      ELSE NULL
    END;
    ASSERT v_rule IS NOT NULL
      AND JSON_VALUE(v_rule, '$.job_resource') = JSON_VALUE(v_binding, '$.job_resource')
      AND JSON_VALUE(v_rule, '$.service_identity') = JSON_VALUE(v_binding, '$.service_identity') AS 'execution_origin_policy_invalid';
    ASSERT SESSION_USER() = JSON_VALUE(v_binding, '$.service_identity') AS 'execution_approval_identity_invalid';
    ASSERT v_approval.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
      AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_approval.canonical_manifest_json)
      AND SAFE.PARSE_JSON(v_approval.canonical_manifest_json, wide_number_mode => 'exact') IS NOT NULL
      AND v_manifest_sha256 = LOWER(TO_HEX(SHA256(v_approval.canonical_manifest_json)))
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.operation') = v_approval.operation
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.contract_sha256') = v_contract_sha256
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.job_resource') = JSON_VALUE(v_binding, '$.job_resource')
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.service_identity') = JSON_VALUE(v_binding, '$.service_identity')
      AND REGEXP_CONTAINS(JSON_VALUE(v_approval.canonical_manifest_json, '$.image_uri'), JSON_VALUE(v_origin_row, '$.image_uri_regex'))
      AND v_approval.expires_at > v_approval.approved_at
      AND v_approval.expires_at <= TIMESTAMP_ADD(v_approval.approved_at, INTERVAL 24 HOUR)
      AND v_approval.expires_at = PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', JSON_VALUE(v_approval.canonical_manifest_json, '$.expires_at'))
      AND v_approval.approval_phrase_sha256 = LOWER(TO_HEX(SHA256(FORMAT('I approve one 42 staging execution of %s for manifest SHA256 %s, origin registry SHA256 %s and resource manifest SHA256 %s. Production remains unchanged.', v_approval.operation, v_manifest_sha256, v_origin_registry_sha256, v_resource_manifest_sha256))))
      AND CONCAT('exa_', LOWER(TO_HEX(SHA256(FORMAT(
        '{"approval_contract_version":"open_intelligence_execution_approval_v2","approved_at":"%s","approved_by":"%s","manifest_sha256":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}',
        FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_approval.approved_at), v_approval.approved_by, v_manifest_sha256,
        v_origin_registry_sha256, v_resource_manifest_sha256
      ))))) = v_approval.approval_id AS 'execution_approval_manifest_invalid';
    SELECT * FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
    WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256;
  ELSE
    ASSERT v_grant_rows = 1
      AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1
        WHERE open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256) = 0
      AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
        WHERE open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
          AND open_intelligence_execution_approvals_v2.operation NOT IN ('recurring_grant_v1', 'recurring_grant_revocation_v1')) = 0 AS 'execution_approval_unavailable';
    SET v_approval = (
      SELECT AS STRUCT approval_id, operation, contract_sha256, canonical_manifest_json, approved_by, approved_at, expires_at, approval_phrase_sha256, origin_registry_sha256, resource_manifest_sha256
      FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
      WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
        AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
        AND open_intelligence_execution_approvals_v2.operation = 'recurring_grant_v1'
    );
    SET v_origin_registry_sha256 = v_approval.origin_registry_sha256;
    SET v_resource_manifest_sha256 = v_approval.resource_manifest_sha256;
    SET v_contract_sha256 = v_approval.contract_sha256;
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
    ASSERT v_approval.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
      AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_approval.canonical_manifest_json)
      AND SAFE.PARSE_JSON(v_approval.canonical_manifest_json, wide_number_mode => 'exact') IS NOT NULL
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.contract_version') = '42_recurring_execution_grant_v1'
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.revocation_state') = 'active'
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.issuing_principal') = v_approval.approved_by
      AND JSON_VALUE(v_approval.canonical_manifest_json, '$.resource_manifest_digest') = v_resource_manifest_sha256
      AND LENGTH(v_approval.canonical_manifest_json) - LENGTH(REPLACE(v_approval.canonical_manifest_json, ',"revocation_state":"active"', '')) = LENGTH(',"revocation_state":"active"')
      AND v_manifest_sha256 = LOWER(TO_HEX(SHA256(REPLACE(v_approval.canonical_manifest_json, ',"revocation_state":"active"', ''))))
      AND REGEXP_CONTAINS(v_contract_sha256, r'^[0-9a-f]{64}$')
      AND v_approval.approval_phrase_sha256 = LOWER(TO_HEX(SHA256(CONCAT('Approve recurring execution grant ', v_contract_sha256))))
      AND v_approval.expires_at = TIMESTAMP(JSON_VALUE(v_approval.canonical_manifest_json, '$.valid_until'))
      AND v_approval.expires_at > v_approval.approved_at
    AND CONCAT('exa_', LOWER(TO_HEX(SHA256(FORMAT(
      '{"approval_contract_version":"open_intelligence_execution_approval_v2","approved_at":"%s","approved_by":"%s","manifest_sha256":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}',
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_approval.approved_at), v_approval.approved_by, v_manifest_sha256,
      v_origin_registry_sha256, v_resource_manifest_sha256
    ))))) = v_approval.approval_id AS 'execution_approval_manifest_invalid';
    ASSERT SESSION_USER() IN UNNEST(JSON_VALUE_ARRAY(v_approval.canonical_manifest_json, '$.executing_principals')) AS 'execution_approval_identity_invalid';
    SET v_revocations = (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
      WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
        AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
        AND open_intelligence_execution_approvals_v2.operation = 'recurring_grant_revocation_v1');
    ASSERT v_revocations <= 1 AS 'execution_approval_schema_mismatch';
    IF v_revocations = 1 THEN
      SET v_revocation = (
        SELECT AS STRUCT approval_id, contract_sha256, canonical_manifest_json, approved_by, approved_at, approval_phrase_sha256, origin_registry_sha256, resource_manifest_sha256
        FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
        WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
          AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
          AND open_intelligence_execution_approvals_v2.operation = 'recurring_grant_revocation_v1'
      );
      ASSERT v_revocation.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
        AND v_revocation.canonical_manifest_json = v_approval.canonical_manifest_json
        AND v_revocation.contract_sha256 = v_contract_sha256
        AND v_revocation.approval_phrase_sha256 = JSON_VALUE(v_approval.canonical_manifest_json, '$.revocation_phrase_sha256')
        AND v_revocation.origin_registry_sha256 = v_origin_registry_sha256
        AND v_revocation.resource_manifest_sha256 = v_resource_manifest_sha256
        AND v_revocation.approved_at > v_approval.approved_at
        AND CONCAT('exa_', LOWER(TO_HEX(SHA256(FORMAT(
          '{"approval_contract_version":"open_intelligence_execution_approval_v2","approved_at":"%s","approved_by":"%s","manifest_sha256":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}',
          FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_revocation.approved_at), v_revocation.approved_by, v_manifest_sha256,
          v_origin_registry_sha256, v_resource_manifest_sha256
        ))))) = v_revocation.approval_id AS 'execution_approval_manifest_invalid';
    END IF;
    SELECT open_intelligence_execution_approvals_v2.*,
           IF(v_revocations = 1, 'revoked', 'active') AS revocation_state,
           v_revocation.approved_at AS revoked_at
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
    WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_approvals_v2.operation = 'recurring_grant_v1';
  END IF;
END;
