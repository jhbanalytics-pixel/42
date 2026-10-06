CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_execution_result_chain_v1`(
  p_source_operation STRING,
  p_run_id STRING
)
BEGIN
  DECLARE v_source_operation STRING DEFAULT p_source_operation;
  DECLARE v_run_id STRING DEFAULT p_run_id;
  DECLARE v_chain_count INT64;

  ASSERT CASE SESSION_USER()
    WHEN 'trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com'
      THEN v_source_operation = 'collection_exposure_issue'
    WHEN 'trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com'
      THEN v_source_operation = 'r3_apply'
    WHEN 'trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com'
      THEN v_source_operation IN ('r3_apply', 'r3_proof_issue')
    ELSE FALSE
  END AS 'execution_result_chain_identity_invalid';

  SET v_chain_count = (
    SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS a
    JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS c
      ON c.approval_id = a.approval_id
      AND c.manifest_sha256 = a.manifest_sha256
      AND c.operation = a.operation
    JOIN `{project}.{dataset}.open_intelligence_execution_results_v1` AS r
      ON r.consumption_id = c.consumption_id
      AND r.approval_id = a.approval_id
      AND r.manifest_sha256 = a.manifest_sha256
      AND r.operation = a.operation
      AND r.execution_name = c.execution_name
    WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND c.consumption_contract_version = 'open_intelligence_execution_consumption_v1'
      AND r.result_contract_version = 'open_intelligence_execution_result_v1'
      AND a.operation = v_source_operation
      AND c.operation = v_source_operation
      AND r.operation = v_source_operation
      AND r.status = 'succeeded'
      AND JSON_TYPE(SAFE.PARSE_JSON(a.canonical_manifest_json, wide_number_mode => 'exact')) = 'object'
      AND a.manifest_sha256 = LOWER(TO_HEX(SHA256(a.canonical_manifest_json)))
      AND JSON_VALUE(a.canonical_manifest_json, '$.operation') = a.operation
      AND JSON_VALUE(a.canonical_manifest_json, '$.job_resource') = c.job_resource
      AND JSON_VALUE(a.canonical_manifest_json, '$.source_sha') = c.source_sha
      AND JSON_VALUE(a.canonical_manifest_json, '$.image_uri') = c.image_uri
      AND REGEXP_CONTAINS(c.execution_name, r'^projects/ogilvy-trends-v2/locations/us-central1/jobs/[^/]+/executions/[^/]+$')
      AND STARTS_WITH(c.execution_name, CONCAT(c.job_resource, '/executions/'))
      AND LENGTH(c.execution_name) > LENGTH(CONCAT(c.job_resource, '/executions/'))
      AND REGEXP_CONTAINS(c.job_resource, r'^projects/ogilvy-trends-v2/locations/us-central1/jobs/[^/]+$')
      AND REGEXP_CONTAINS(c.source_sha, r'^[0-9a-f]{40}$')
      AND REGEXP_CONTAINS(c.image_uri, r'^us-central1-docker[.]pkg[.]dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:[0-9a-f]{64}$')
      AND JSON_TYPE(SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact')) = 'object'
      AND r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))
      AND JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id
      AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS linked_a
           WHERE linked_a.approval_id = a.approval_id
             AND linked_a.manifest_sha256 = a.manifest_sha256) = 1
      AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS linked_c
           WHERE linked_c.approval_id = a.approval_id
              OR linked_c.manifest_sha256 = a.manifest_sha256) = 1
      AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS linked_r
           WHERE linked_r.consumption_id = c.consumption_id
              OR linked_r.approval_id = a.approval_id
              OR linked_r.manifest_sha256 = a.manifest_sha256) = 1
  );
  ASSERT v_chain_count = 1 AS 'execution_result_chain_unavailable';

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
         r.completed_at AS completed_at
  FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS a
  JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS c
    ON c.approval_id = a.approval_id
    AND c.manifest_sha256 = a.manifest_sha256
    AND c.operation = a.operation
  JOIN `{project}.{dataset}.open_intelligence_execution_results_v1` AS r
    ON r.consumption_id = c.consumption_id
    AND r.approval_id = a.approval_id
    AND r.manifest_sha256 = a.manifest_sha256
    AND r.operation = a.operation
    AND r.execution_name = c.execution_name
  WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v1'
    AND c.consumption_contract_version = 'open_intelligence_execution_consumption_v1'
    AND r.result_contract_version = 'open_intelligence_execution_result_v1'
    AND a.operation = v_source_operation
    AND c.operation = v_source_operation
    AND r.operation = v_source_operation
    AND r.status = 'succeeded'
    AND JSON_TYPE(SAFE.PARSE_JSON(a.canonical_manifest_json, wide_number_mode => 'exact')) = 'object'
    AND a.manifest_sha256 = LOWER(TO_HEX(SHA256(a.canonical_manifest_json)))
    AND JSON_VALUE(a.canonical_manifest_json, '$.operation') = a.operation
    AND JSON_VALUE(a.canonical_manifest_json, '$.job_resource') = c.job_resource
    AND JSON_VALUE(a.canonical_manifest_json, '$.source_sha') = c.source_sha
    AND JSON_VALUE(a.canonical_manifest_json, '$.image_uri') = c.image_uri
    AND REGEXP_CONTAINS(c.execution_name, r'^projects/ogilvy-trends-v2/locations/us-central1/jobs/[^/]+/executions/[^/]+$')
    AND STARTS_WITH(c.execution_name, CONCAT(c.job_resource, '/executions/'))
    AND LENGTH(c.execution_name) > LENGTH(CONCAT(c.job_resource, '/executions/'))
    AND REGEXP_CONTAINS(c.job_resource, r'^projects/ogilvy-trends-v2/locations/us-central1/jobs/[^/]+$')
    AND REGEXP_CONTAINS(c.source_sha, r'^[0-9a-f]{40}$')
    AND REGEXP_CONTAINS(c.image_uri, r'^us-central1-docker[.]pkg[.]dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:[0-9a-f]{64}$')
    AND JSON_TYPE(SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact')) = 'object'
    AND r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))
    AND JSON_VALUE(r.canonical_result_json, '$.run_id') = v_run_id
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS linked_a
         WHERE linked_a.approval_id = a.approval_id
           AND linked_a.manifest_sha256 = a.manifest_sha256) = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS linked_c
         WHERE linked_c.approval_id = a.approval_id
            OR linked_c.manifest_sha256 = a.manifest_sha256) = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS linked_r
         WHERE linked_r.consumption_id = c.consumption_id
            OR linked_r.approval_id = a.approval_id
            OR linked_r.manifest_sha256 = a.manifest_sha256) = 1;
END;
