CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_consume_open_intelligence_execution_v1`(
  manifest_sha256 STRING,
  execution_name STRING,
  job_resource STRING,
  source_sha STRING,
  image_uri STRING
)
BEGIN
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_execution_name STRING DEFAULT execution_name;
  DECLARE v_job_resource STRING DEFAULT job_resource;
  DECLARE v_source_sha STRING DEFAULT source_sha;
  DECLARE v_image_uri STRING DEFAULT image_uri;
  DECLARE v_approval STRUCT<approval_id STRING, operation STRING, canonical_manifest_json STRING, approved_by STRING, expires_at TIMESTAMP>;
  DECLARE v_consumed_at TIMESTAMP;
  DECLARE v_consumption_id STRING;
  DECLARE v_expected_identity STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  SET v_lock_version = (
    SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
    WHERE lock_name = 'open_intelligence_execution_approval_v1'
      AND approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND state = 'ready'
  );
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET lock_version = lock_version + 1, state = 'consuming', updated_at = CURRENT_TIMESTAMP()
  WHERE lock_name = 'open_intelligence_execution_approval_v1'
    AND approval_contract_version = 'open_intelligence_execution_approval_v1'
    AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1
    WHERE open_intelligence_execution_approvals_v1.approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256) = 1 AS 'execution_approval_unavailable';
  SET v_approval = (
    SELECT AS STRUCT approval_id, operation, canonical_manifest_json, approved_by, expires_at
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1
    WHERE open_intelligence_execution_approvals_v1.approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256
  );
  SET v_consumed_at = CURRENT_TIMESTAMP();
  SET v_expected_identity = CASE v_approval.operation
    WHEN 'migration_apply' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'collection_exposure_issue' THEN 'trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_apply' THEN 'trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_proof_issue' THEN 'trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_release' THEN 'trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'brain_read' THEN 'trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'wave1_pilot' THEN 'trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com'
  END;
  ASSERT SESSION_USER() = v_expected_identity AS 'execution_approval_identity_invalid';
  ASSERT v_approval.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
    AND v_manifest_sha256 = LOWER(TO_HEX(SHA256(v_approval.canonical_manifest_json)))
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.operation') = v_approval.operation
    AS 'execution_approval_manifest_mismatch';
  ASSERT JSON_VALUE(v_approval.canonical_manifest_json, '$.job_resource') = v_job_resource
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.source_sha') = v_source_sha
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.image_uri') = v_image_uri
    AND STARTS_WITH(v_execution_name, CONCAT(v_job_resource, '/executions/'))
    AND LENGTH(v_execution_name) > LENGTH(CONCAT(v_job_resource, '/executions/'))
    AS 'execution_approval_execution_mismatch';
  ASSERT v_consumed_at < v_approval.expires_at AS 'execution_approval_expired';
  SET v_consumption_id = CONCAT('exc_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"approval_id":"%s","consumed_at":"%s","execution_name":"%s"}',
    v_approval.approval_id, FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_consumed_at),
    v_execution_name
  )))));
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS open_intelligence_execution_consumptions_v1 WHERE open_intelligence_execution_consumptions_v1.approval_id = v_approval.approval_id OR open_intelligence_execution_consumptions_v1.manifest_sha256 = v_manifest_sha256 OR open_intelligence_execution_consumptions_v1.consumption_id = v_consumption_id OR open_intelligence_execution_consumptions_v1.execution_name = v_execution_name) = 0 AS 'execution_approval_consumed';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v1` VALUES (
    'open_intelligence_execution_consumption_v1', v_consumption_id, v_approval.approval_id,
    v_manifest_sha256, v_approval.operation, v_execution_name, v_job_resource, v_source_sha, v_image_uri,
    v_consumed_at
  );
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS open_intelligence_execution_consumptions_v1
    WHERE open_intelligence_execution_consumptions_v1.consumption_contract_version = 'open_intelligence_execution_consumption_v1'
      AND open_intelligence_execution_consumptions_v1.consumption_id = v_consumption_id
      AND open_intelligence_execution_consumptions_v1.approval_id = v_approval.approval_id
      AND open_intelligence_execution_consumptions_v1.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_consumptions_v1.operation = v_approval.operation
      AND open_intelligence_execution_consumptions_v1.execution_name = v_execution_name
      AND open_intelligence_execution_consumptions_v1.job_resource = v_job_resource
      AND open_intelligence_execution_consumptions_v1.source_sha = v_source_sha
      AND open_intelligence_execution_consumptions_v1.image_uri = v_image_uri
      AND open_intelligence_execution_consumptions_v1.consumed_at = v_consumed_at) = 1 AS 'execution_approval_schema_mismatch';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET state = 'ready', updated_at = v_consumed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v1'
    AND approval_contract_version = 'open_intelligence_execution_approval_v1'
    AND state = 'consuming' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_consumption_v1' AS consumption_contract_version,
         v_consumption_id AS consumption_id, v_approval.approval_id AS approval_id,
         v_manifest_sha256 AS manifest_sha256, v_approval.operation AS operation,
         v_execution_name AS execution_name, v_consumed_at AS consumed_at;
END;
