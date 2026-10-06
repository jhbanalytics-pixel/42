CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_import_open_intelligence_execution_bootstrap_v1`(
  canonical_manifest_json STRING,
  manifest_sha256 STRING,
  manifest_object STRING,
  manifest_generation INT64,
  manifest_created_at TIMESTAMP,
  signature_object STRING,
  signature_generation INT64,
  signature_sha256 STRING,
  kms_key_version STRING,
  signature_created_at TIMESTAMP,
  bootstrap_lock_object STRING,
  bootstrap_lock_generation INT64,
  bootstrap_lock_created_at TIMESTAMP,
  execution_name STRING,
  job_resource STRING,
  source_sha STRING,
  image_uri STRING,
  migration_result_reference STRING,
  migration_result_digest STRING
)
BEGIN
  DECLARE v_canonical_manifest_json STRING DEFAULT canonical_manifest_json;
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_execution_name STRING DEFAULT execution_name;
  DECLARE v_job_resource STRING DEFAULT job_resource;
  DECLARE v_source_sha STRING DEFAULT source_sha;
  DECLARE v_image_uri STRING DEFAULT image_uri;
  DECLARE v_migration_result_reference STRING DEFAULT migration_result_reference;
  DECLARE v_migration_result_digest STRING DEFAULT migration_result_digest;
  DECLARE v_approval_id STRING;
  DECLARE v_consumption_id STRING;
  DECLARE v_result_id STRING;
  DECLARE v_completed_at TIMESTAMP;
  DECLARE v_expires_at TIMESTAMP;
  DECLARE v_proof STRING;
  DECLARE v_proof_digest STRING;
  DECLARE v_lock_version INT64;
  DECLARE v_approver STRING DEFAULT 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab';
  BEGIN TRANSACTION;
  ASSERT SESSION_USER() = 'trends-engine-oi-bootstrap@ogilvy-trends-v2.iam.gserviceaccount.com' AS 'execution_approval_identity_invalid';
  ASSERT kms_key_version = 'projects/ogilvy-trends-v2/locations/global/keyRings/open-intelligence-staging/cryptoKeys/execution-bootstrap/cryptoKeyVersions/1' AS 'execution_approval_bootstrap_unapproved';
  ASSERT manifest_generation > 0 AND signature_generation > 0 AND bootstrap_lock_generation > 0 AS 'execution_approval_bootstrap_unapproved';
  ASSERT STARTS_WITH(manifest_object, 'gs://ogilvy-trends-v2-execution-approvals-staging/bootstrap/manifests/')
    AND STARTS_WITH(signature_object, 'gs://ogilvy-trends-v2-execution-approvals-staging/bootstrap/signatures/')
    AND bootstrap_lock_object = 'gs://ogilvy-trends-v2-execution-approvals-staging/bootstrap/locks/open-intelligence-execution-approval-v1.lock'
    AS 'execution_approval_bootstrap_unapproved';
  ASSERT v_manifest_sha256 = LOWER(TO_HEX(SHA256(v_canonical_manifest_json))) AS 'execution_approval_manifest_mismatch';
  ASSERT JSON_VALUE(v_canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v1'
    AND JSON_VALUE(v_canonical_manifest_json, '$.contract_sha256') = '527798d33d83382f77586fe9869f4456371fce5370a21ad4316d2e4821969545'
    AND JSON_VALUE(v_canonical_manifest_json, '$.project') = 'ogilvy-trends-v2'
    AND JSON_VALUE(v_canonical_manifest_json, '$.location') = 'US'
    AND JSON_VALUE(v_canonical_manifest_json, '$.service_identity') = 'trends-engine-oi-bootstrap@ogilvy-trends-v2.iam.gserviceaccount.com'
    AS 'execution_approval_manifest_invalid';
  ASSERT JSON_VALUE(v_canonical_manifest_json, '$.operation') = 'bootstrap_migration_apply' AS 'execution_approval_manifest_invalid';
  ASSERT JSON_VALUE(v_canonical_manifest_json, '$.job_resource') = v_job_resource
    AND JSON_VALUE(v_canonical_manifest_json, '$.source_sha') = v_source_sha
    AND JSON_VALUE(v_canonical_manifest_json, '$.image_uri') = v_image_uri
    AND v_job_resource = 'projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-approval-bootstrap-staging'
    AND STARTS_WITH(v_execution_name, CONCAT(v_job_resource, '/executions/'))
    AS 'execution_approval_execution_mismatch';
  ASSERT manifest_created_at <= signature_created_at
    AND REGEXP_CONTAINS(signature_sha256, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_migration_result_digest, r'^[0-9a-f]{64}$')
    AND NULLIF(TRIM(v_migration_result_reference), '') IS NOT NULL
    AS 'execution_approval_bootstrap_unapproved';
  SET v_expires_at = PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', JSON_VALUE(v_canonical_manifest_json, '$.expires_at'));
  ASSERT signature_created_at <= bootstrap_lock_created_at AND bootstrap_lock_created_at < v_expires_at AS 'execution_approval_expired';
  SET v_completed_at = CURRENT_TIMESTAMP();
  ASSERT v_completed_at < v_expires_at AS 'execution_approval_expired';
  SET v_approval_id = CONCAT('exa_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"approval_contract_version":"open_intelligence_execution_approval_v1","approved_at":"%s","approved_by":"%s","manifest_sha256":"%s"}',
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', signature_created_at), v_approver,
    v_manifest_sha256
  )))));
  SET v_consumption_id = CONCAT('exc_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"approval_id":"%s","consumed_at":"%s","execution_name":"%s"}',
    v_approval_id, FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', bootstrap_lock_created_at),
    v_execution_name
  )))));
  SET v_proof = TO_JSON_STRING(STRUCT(
    'open_intelligence_bootstrap_proof_v1' AS bootstrap_proof_contract_version,
    manifest_object AS manifest_object, manifest_generation AS manifest_generation,
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', manifest_created_at) AS manifest_created_at,
    v_manifest_sha256 AS manifest_sha256, signature_object AS signature_object,
    signature_generation AS signature_generation,
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', signature_created_at) AS signature_created_at,
    signature_sha256 AS signature_sha256, kms_key_version AS kms_key_version,
    bootstrap_lock_object AS lock_object, bootstrap_lock_generation AS lock_generation,
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', bootstrap_lock_created_at) AS lock_created_at,
    v_execution_name AS execution_name, v_job_resource AS job_resource, v_source_sha AS source_sha,
    v_image_uri AS image_uri, v_migration_result_reference AS migration_result_reference,
    v_migration_result_digest AS migration_result_digest,
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_completed_at) AS completed_at
  ));
  SET v_proof_digest = LOWER(TO_HEX(SHA256(v_proof)));
  SET v_result_id = CONCAT('exr_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"completed_at":"%s","consumption_id":"%s","result_digest":"%s","result_reference":"%s","status":"succeeded"}',
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_completed_at), v_consumption_id,
    v_proof_digest, v_migration_result_reference
  )))));
  SET v_lock_version = (SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1` WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET lock_version = lock_version + 1, state = 'recording_result', updated_at = v_completed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1 WHERE open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256 OR open_intelligence_execution_approvals_v1.approval_id = v_approval_id) = 0 AS 'execution_approval_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS open_intelligence_execution_consumptions_v1 WHERE open_intelligence_execution_consumptions_v1.approval_id = v_approval_id OR open_intelligence_execution_consumptions_v1.manifest_sha256 = v_manifest_sha256 OR open_intelligence_execution_consumptions_v1.consumption_id = v_consumption_id OR open_intelligence_execution_consumptions_v1.execution_name = v_execution_name) = 0 AS 'execution_approval_consumed';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS open_intelligence_execution_results_v1 WHERE open_intelligence_execution_results_v1.consumption_id = v_consumption_id OR open_intelligence_execution_results_v1.result_id = v_result_id OR open_intelligence_execution_results_v1.result_reference = v_migration_result_reference) = 0 AS 'execution_approval_conflict';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_approvals_v1` VALUES (
    'open_intelligence_execution_approval_v1', v_approval_id,
    'open_intelligence_execution_manifest_v1', 'bootstrap_migration_apply',
    JSON_VALUE(v_canonical_manifest_json, '$.contract_sha256'), v_manifest_sha256,
    v_canonical_manifest_json, v_approver, signature_created_at, v_expires_at,
    LOWER(TO_HEX(SHA256(FORMAT('I approve one staging bootstrap migration for manifest SHA256 %s. Production remains unchanged.', v_manifest_sha256))))
  );
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v1` VALUES (
    'open_intelligence_execution_consumption_v1', v_consumption_id, v_approval_id,
    v_manifest_sha256, 'bootstrap_migration_apply', v_execution_name, v_job_resource, v_source_sha,
    v_image_uri, bootstrap_lock_created_at
  );
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_results_v1` VALUES (
    'open_intelligence_execution_result_v1', v_result_id, v_consumption_id, v_approval_id,
    v_manifest_sha256, 'bootstrap_migration_apply', v_execution_name, v_migration_result_reference,
    v_proof, v_proof_digest, 'succeeded', v_completed_at
  );
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1
    WHERE open_intelligence_execution_approvals_v1.approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND open_intelligence_execution_approvals_v1.approval_id = v_approval_id
      AND open_intelligence_execution_approvals_v1.manifest_version = 'open_intelligence_execution_manifest_v1'
      AND open_intelligence_execution_approvals_v1.operation = 'bootstrap_migration_apply'
      AND open_intelligence_execution_approvals_v1.contract_sha256 = JSON_VALUE(v_canonical_manifest_json, '$.contract_sha256')
      AND open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_approvals_v1.canonical_manifest_json = v_canonical_manifest_json
      AND open_intelligence_execution_approvals_v1.approved_by = v_approver
      AND open_intelligence_execution_approvals_v1.approved_at = signature_created_at
      AND open_intelligence_execution_approvals_v1.expires_at = v_expires_at
      AND open_intelligence_execution_approvals_v1.approval_phrase_sha256 = LOWER(TO_HEX(SHA256(FORMAT('I approve one staging bootstrap migration for manifest SHA256 %s. Production remains unchanged.', v_manifest_sha256))))) = 1 AS 'execution_approval_schema_mismatch';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS open_intelligence_execution_consumptions_v1
    WHERE open_intelligence_execution_consumptions_v1.consumption_contract_version = 'open_intelligence_execution_consumption_v1'
      AND open_intelligence_execution_consumptions_v1.consumption_id = v_consumption_id
      AND open_intelligence_execution_consumptions_v1.approval_id = v_approval_id
      AND open_intelligence_execution_consumptions_v1.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_consumptions_v1.operation = 'bootstrap_migration_apply'
      AND open_intelligence_execution_consumptions_v1.execution_name = v_execution_name
      AND open_intelligence_execution_consumptions_v1.job_resource = v_job_resource
      AND open_intelligence_execution_consumptions_v1.source_sha = v_source_sha
      AND open_intelligence_execution_consumptions_v1.image_uri = v_image_uri
      AND open_intelligence_execution_consumptions_v1.consumed_at = bootstrap_lock_created_at) = 1 AS 'execution_approval_schema_mismatch';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS open_intelligence_execution_results_v1
    WHERE open_intelligence_execution_results_v1.result_contract_version = 'open_intelligence_execution_result_v1'
      AND open_intelligence_execution_results_v1.result_id = v_result_id
      AND open_intelligence_execution_results_v1.consumption_id = v_consumption_id
      AND open_intelligence_execution_results_v1.approval_id = v_approval_id
      AND open_intelligence_execution_results_v1.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_results_v1.operation = 'bootstrap_migration_apply'
      AND open_intelligence_execution_results_v1.execution_name = v_execution_name
      AND open_intelligence_execution_results_v1.result_reference = v_migration_result_reference
      AND open_intelligence_execution_results_v1.canonical_result_json = v_proof
      AND open_intelligence_execution_results_v1.result_digest = v_proof_digest
      AND open_intelligence_execution_results_v1.status = 'succeeded'
      AND open_intelligence_execution_results_v1.completed_at = v_completed_at) = 1 AS 'execution_approval_schema_mismatch';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET state = 'ready', last_approval_id = v_approval_id, updated_at = v_completed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'recording_result' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT v_manifest_sha256 AS manifest_sha256, v_approval_id AS approval_id,
         v_consumption_id AS consumption_id, v_result_id AS result_id,
         v_migration_result_reference AS migration_result_reference,
         v_migration_result_digest AS migration_result_digest,
         v_proof_digest AS bootstrap_proof_digest;
END;
