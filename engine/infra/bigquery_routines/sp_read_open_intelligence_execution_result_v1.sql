CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_execution_result_v1`(
  p_consumption_id STRING
)
BEGIN
  DECLARE v_consumption_id STRING DEFAULT p_consumption_id;
  DECLARE v_consumption STRUCT<approval_id STRING, manifest_sha256 STRING, operation STRING, execution_name STRING>;
  DECLARE v_expected_identity STRING;
  DECLARE v_result_count INT64;

  ASSERT REGEXP_CONTAINS(v_consumption_id, r'^exc_[0-9a-f]{64}$')
    AS 'execution_approval_unavailable';
  ASSERT (SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1`
    WHERE consumption_contract_version = 'open_intelligence_execution_consumption_v1'
      AND consumption_id = v_consumption_id) = 1 AS 'execution_approval_unavailable';

  SET v_consumption = (
    SELECT AS STRUCT approval_id, manifest_sha256, operation, execution_name
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1`
    WHERE consumption_contract_version = 'open_intelligence_execution_consumption_v1'
      AND consumption_id = v_consumption_id
  );
  SET v_expected_identity = CASE v_consumption.operation
    WHEN 'bootstrap_migration_apply' THEN 'trends-engine-oi-bootstrap@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'source_snapshot_capture' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'migration_apply' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'collection_exposure_issue' THEN 'trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_apply' THEN 'trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_proof_issue' THEN 'trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_release' THEN 'trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'brain_read' THEN 'trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'wave1_pilot' THEN 'trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com'
    ELSE NULL
  END;
  ASSERT SESSION_USER() = v_expected_identity AS 'execution_approval_identity_invalid';

  ASSERT (SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS a
    WHERE a.approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND a.approval_id = v_consumption.approval_id
      AND a.manifest_sha256 = v_consumption.manifest_sha256
      AND a.operation = v_consumption.operation
      AND a.manifest_sha256 = LOWER(TO_HEX(SHA256(a.canonical_manifest_json)))
      AND JSON_VALUE(a.canonical_manifest_json, '$.operation') = a.operation) = 1
    AS 'execution_result_conflict';

  SET v_result_count = (
    SELECT COUNT(*)
    FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS r
    WHERE r.consumption_id = v_consumption_id
       OR r.approval_id = v_consumption.approval_id
       OR r.manifest_sha256 = v_consumption.manifest_sha256
  );
  ASSERT v_result_count <= 1 AS 'execution_result_conflict';
  ASSERT NOT EXISTS (
    SELECT 1
    FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS r
    WHERE (r.consumption_id = v_consumption_id
        OR r.approval_id = v_consumption.approval_id
        OR r.manifest_sha256 = v_consumption.manifest_sha256)
      AND NOT (
        r.result_contract_version = 'open_intelligence_execution_result_v1'
        AND r.consumption_id = v_consumption_id
        AND r.approval_id = v_consumption.approval_id
        AND r.manifest_sha256 = v_consumption.manifest_sha256
        AND r.operation = v_consumption.operation
        AND r.execution_name = v_consumption.execution_name
        AND JSON_TYPE(SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact')) = 'object'
        AND r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))
        AND r.status IN ('succeeded', 'failed')
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
         r.completed_at AS completed_at
  FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS r
  WHERE r.consumption_id = v_consumption_id
    AND r.approval_id = v_consumption.approval_id
    AND r.manifest_sha256 = v_consumption.manifest_sha256
    AND r.operation = v_consumption.operation
    AND r.execution_name = v_consumption.execution_name;
END;
