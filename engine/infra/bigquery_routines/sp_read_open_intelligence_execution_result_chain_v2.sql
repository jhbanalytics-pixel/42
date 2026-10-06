CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_execution_result_chain_v2`(
  p_source_operation STRING,
  p_run_id STRING
)
BEGIN
  DECLARE v_source_operation STRING DEFAULT p_source_operation;
  DECLARE v_run_id STRING DEFAULT p_run_id;
  DECLARE v_chain_count INT64;
  DECLARE v_chain STRUCT<approval_id STRING, manifest_sha256 STRING, contract_sha256 STRING, job_resource STRING, origin_registry_sha256 STRING, resource_manifest_sha256 STRING>;
  DECLARE v_origin_registry_sha256 STRING;
  DECLARE v_resource_manifest_sha256 STRING;
  DECLARE v_contract_sha256 STRING;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  DECLARE v_policy_json STRING;
  DECLARE v_origin_row STRING;
  DECLARE v_binding STRING;
  DECLARE v_apply_identity STRING;
  DECLARE v_proof_identity STRING;
  DECLARE v_release_identity STRING;

  ASSERT v_source_operation IN ('collection_exposure_issue', 'r3_apply', 'r3_proof_issue')
    AND NULLIF(TRIM(v_run_id), '') IS NOT NULL AS 'execution_result_chain_unavailable';

  SET v_chain_count = (
    SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS a
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` AS c
      ON c.approval_id = a.approval_id
      AND c.manifest_sha256 = a.manifest_sha256
      AND c.operation = a.operation
      AND c.origin_registry_sha256 = a.origin_registry_sha256
      AND c.resource_manifest_sha256 = a.resource_manifest_sha256
    JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` AS r
      ON r.consumption_id = c.consumption_id
      AND r.approval_id = a.approval_id
      AND r.manifest_sha256 = a.manifest_sha256
      AND r.operation = a.operation
      AND r.execution_name = c.execution_name
      AND r.origin_registry_sha256 = a.origin_registry_sha256
      AND r.resource_manifest_sha256 = a.resource_manifest_sha256
    WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND c.consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND r.result_contract_version = 'open_intelligence_execution_result_v2'
      AND a.operation = v_source_operation
      AND c.operation = v_source_operation
      AND r.operation = v_source_operation
      AND r.status = 'succeeded'
      AND a.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
      AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(a.canonical_manifest_json)
      AND JSON_TYPE(SAFE.PARSE_JSON(a.canonical_manifest_json, wide_number_mode => 'exact')) = 'object'
      AND a.manifest_sha256 = LOWER(TO_HEX(SHA256(a.canonical_manifest_json)))
      AND JSON_VALUE(a.canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(a.canonical_manifest_json, '$.operation') = a.operation
      AND JSON_VALUE(a.canonical_manifest_json, '$.contract_sha256') = a.contract_sha256
      AND JSON_VALUE(a.canonical_manifest_json, '$.job_resource') = c.job_resource
      AND JSON_VALUE(a.canonical_manifest_json, '$.source_sha') = c.source_sha
      AND JSON_VALUE(a.canonical_manifest_json, '$.image_uri') = c.image_uri
      AND REGEXP_CONTAINS(c.execution_name, r'^projects/ogilvy-trends-v2/locations/us-central1/jobs/[^/]+/executions/[^/]+$')
      AND STARTS_WITH(c.execution_name, CONCAT(c.job_resource, '/executions/'))
      AND LENGTH(c.execution_name) > LENGTH(CONCAT(c.job_resource, '/executions/'))
      AND REGEXP_CONTAINS(c.job_resource, r'^projects/ogilvy-trends-v2/locations/us-central1/jobs/[^/]+$')
      AND REGEXP_CONTAINS(c.source_sha, r'^[0-9a-f]{40}$')
      AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(r.canonical_result_json)
      AND JSON_TYPE(SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact')) = 'object'
      AND r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))
      AND JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id
      AND (SELECT COUNT(*) FROM (
             SELECT approval_id, manifest_sha256 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1`
             UNION ALL SELECT approval_id, manifest_sha256 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
           ) AS linked_a
           WHERE linked_a.approval_id = a.approval_id
              OR linked_a.manifest_sha256 = a.manifest_sha256) = 1
      AND (SELECT COUNT(*) FROM (
             SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1`
             UNION ALL SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
           ) AS linked_c
           WHERE linked_c.approval_id = a.approval_id
              OR linked_c.manifest_sha256 = a.manifest_sha256
              OR linked_c.consumption_id = c.consumption_id
              OR linked_c.execution_name = c.execution_name) = 1
      AND (SELECT COUNT(*) FROM (
             SELECT consumption_id, approval_id, manifest_sha256, result_id, result_reference FROM `{project}.{dataset}.open_intelligence_execution_results_v1`
             UNION ALL SELECT consumption_id, approval_id, manifest_sha256, result_id, result_reference FROM `{project}.{dataset}.open_intelligence_execution_results_v2`
           ) AS linked_r
           WHERE linked_r.consumption_id = c.consumption_id
              OR linked_r.approval_id = a.approval_id
              OR linked_r.manifest_sha256 = a.manifest_sha256
              OR linked_r.result_id = r.result_id
              OR linked_r.result_reference = r.result_reference) = 1
  );
  ASSERT v_chain_count = 1 AS 'execution_result_chain_unavailable';

  ASSERT (
    SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS a
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` AS c
      ON c.approval_id = a.approval_id
      AND c.manifest_sha256 = a.manifest_sha256
      AND c.operation = a.operation
    JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` AS r
      ON r.consumption_id = c.consumption_id
      AND r.approval_id = a.approval_id
      AND r.manifest_sha256 = a.manifest_sha256
      AND r.operation = a.operation
      AND r.execution_name = c.execution_name
    WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND c.consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND r.result_contract_version = 'open_intelligence_execution_result_v2'
      AND a.operation = v_source_operation
      AND r.status = 'succeeded'
      AND JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id
  ) = 1 AS 'execution_result_chain_ambiguous';
  SET v_chain = (
    SELECT AS STRUCT a.approval_id, a.manifest_sha256, a.contract_sha256, c.job_resource, a.origin_registry_sha256, a.resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS a
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` AS c
      ON c.approval_id = a.approval_id
      AND c.manifest_sha256 = a.manifest_sha256
      AND c.operation = a.operation
    JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` AS r
      ON r.consumption_id = c.consumption_id
      AND r.approval_id = a.approval_id
      AND r.manifest_sha256 = a.manifest_sha256
      AND r.operation = a.operation
      AND r.execution_name = c.execution_name
    WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND c.consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND r.result_contract_version = 'open_intelligence_execution_result_v2'
      AND a.operation = v_source_operation
      AND r.status = 'succeeded'
      AND JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id
  );
  SET v_origin_registry_sha256 = v_chain.origin_registry_sha256;
  SET v_resource_manifest_sha256 = v_chain.resource_manifest_sha256;
  SET v_contract_sha256 = v_chain.contract_sha256;
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
  SET v_binding = CASE v_source_operation
    WHEN 'migration_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.migration_apply')
    WHEN 'collection_exposure_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.collection_exposure_issue')
    WHEN 'r3_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_apply')
    WHEN 'r3_proof_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_proof_issue')
    WHEN 'r3_release' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_release')
    WHEN 'brain_read' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.brain_read')
    WHEN 'wave1_pilot' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.wave1_pilot')
    ELSE NULL
  END;
  ASSERT v_binding IS NOT NULL AND JSON_VALUE(v_binding, '$.job_resource') = v_chain.job_resource AS 'execution_origin_pair_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256) = 1 AS 'execution_origin_policy_invalid';
  SET v_policy_json = (SELECT canonical_policy_json FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_policy_json))) = v_contract_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_policy_json)
    AND SAFE.PARSE_JSON(v_policy_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_policy_json, '$.contract_version') = 'open_intelligence_execution_origin_policy_v2'
    AND v_source_operation IN UNNEST(JSON_VALUE_ARRAY(v_policy_json, '$.successor.operations')) AS 'execution_origin_policy_invalid';
  ASSERT (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_apply.service_identity') IS NOT NULL) <= 1
    AND (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_proof_issue.service_identity') IS NOT NULL) <= 1
    AND (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_release.service_identity') IS NOT NULL) <= 1
    AS 'execution_origin_binding_ambiguous';
  SET v_apply_identity = (SELECT JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_apply.service_identity')
    FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_apply.service_identity') IS NOT NULL);
  SET v_proof_identity = (SELECT JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_proof_issue.service_identity')
    FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_proof_issue.service_identity') IS NOT NULL);
  SET v_release_identity = (SELECT JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_release.service_identity')
    FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.exact_operation_bindings.r3_release.service_identity') IS NOT NULL);
  ASSERT v_apply_identity IS NOT NULL AND v_proof_identity IS NOT NULL AND v_release_identity IS NOT NULL
    AND v_apply_identity != v_proof_identity AND v_apply_identity != v_release_identity AND v_proof_identity != v_release_identity
    AS 'execution_origin_pair_invalid';
  ASSERT CASE SESSION_USER()
    WHEN v_apply_identity
      THEN v_source_operation = 'collection_exposure_issue'
    WHEN v_proof_identity
      THEN v_source_operation = 'r3_apply'
    WHEN v_release_identity
      THEN v_source_operation IN ('r3_apply', 'r3_proof_issue')
    ELSE FALSE
  END AS 'execution_result_chain_identity_invalid';

  SELECT a.approval_id AS approval_id,
         a.manifest_sha256 AS manifest_sha256,
         a.canonical_manifest_json AS canonical_manifest_json,
         c.consumption_id AS consumption_id,
         c.execution_name AS execution_name,
         c.job_resource AS job_resource,
         c.source_sha AS source_sha,
         c.image_uri AS image_uri,
         r.result_id AS result_id,
         r.result_reference AS result_reference,
         r.canonical_result_json AS canonical_result_json,
         r.result_digest AS result_digest,
         r.status AS status,
         r.completed_at AS completed_at,
         a.origin_registry_sha256 AS origin_registry_sha256,
         a.resource_manifest_sha256 AS resource_manifest_sha256
  FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS a
  JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` AS c
    ON c.approval_id = a.approval_id
    AND c.manifest_sha256 = a.manifest_sha256
    AND c.operation = a.operation
    AND c.origin_registry_sha256 = a.origin_registry_sha256
    AND c.resource_manifest_sha256 = a.resource_manifest_sha256
  JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` AS r
    ON r.consumption_id = c.consumption_id
    AND r.approval_id = a.approval_id
    AND r.manifest_sha256 = a.manifest_sha256
    AND r.operation = a.operation
    AND r.execution_name = c.execution_name
    AND r.origin_registry_sha256 = a.origin_registry_sha256
    AND r.resource_manifest_sha256 = a.resource_manifest_sha256
  WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND c.consumption_contract_version = 'open_intelligence_execution_consumption_v2'
    AND r.result_contract_version = 'open_intelligence_execution_result_v2'
    AND a.approval_id = v_chain.approval_id
    AND a.manifest_sha256 = v_chain.manifest_sha256
    AND a.origin_registry_sha256 = v_origin_registry_sha256
    AND a.resource_manifest_sha256 = v_resource_manifest_sha256
    AND a.operation = v_source_operation
    AND c.operation = v_source_operation
    AND r.operation = v_source_operation
    AND r.status = 'succeeded'
    AND JSON_VALUE(a.canonical_manifest_json, '$.job_resource') = JSON_VALUE(v_binding, '$.job_resource')
    AND JSON_VALUE(a.canonical_manifest_json, '$.service_identity') = JSON_VALUE(v_binding, '$.service_identity')
    AND REGEXP_CONTAINS(c.image_uri, JSON_VALUE(v_origin_row, '$.image_uri_regex'))
    AND JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id;
END;
