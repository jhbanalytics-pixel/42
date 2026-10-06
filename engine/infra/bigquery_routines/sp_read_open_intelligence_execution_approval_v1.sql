CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_execution_approval_v1`(
  manifest_sha256 STRING
)
BEGIN
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_expected_identity STRING;
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1
    WHERE open_intelligence_execution_approvals_v1.approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256) = 1 AS 'execution_approval_unavailable';
  SET v_expected_identity = (
    SELECT CASE operation
      WHEN 'source_snapshot_capture' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'migration_apply' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
      WHEN 'collection_exposure_issue' THEN 'trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com'
      WHEN 'r3_apply' THEN 'trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com'
      WHEN 'r3_proof_issue' THEN 'trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com'
      WHEN 'r3_release' THEN 'trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com'
      WHEN 'brain_read' THEN 'trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com'
      WHEN 'wave1_pilot' THEN 'trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com'
    END
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1
    WHERE open_intelligence_execution_approvals_v1.approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256
  );
  ASSERT SESSION_USER() = v_expected_identity AS 'execution_approval_identity_invalid';
  SELECT * FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1
  WHERE open_intelligence_execution_approvals_v1.approval_contract_version = 'open_intelligence_execution_approval_v1'
    AND open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256;
END;
