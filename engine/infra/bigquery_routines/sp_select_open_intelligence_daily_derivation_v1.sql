CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_select_open_intelligence_daily_derivation_v1`(
  child_job_resource STRING, child_image_uri STRING, authorizing_grant_digest STRING, child_job_policy_digest STRING
)
BEGIN
  DECLARE v_count INT64;
  DECLARE v_child_job_resource STRING DEFAULT child_job_resource;
  DECLARE v_child_image_uri STRING DEFAULT child_image_uri;
  DECLARE v_authorizing_grant_digest STRING DEFAULT authorizing_grant_digest;
  DECLARE v_child_job_policy_digest STRING DEFAULT child_job_policy_digest;
  SET v_count = (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` t USING (derivation_id)
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    WHERE d.child_job_resource=v_child_job_resource AND d.child_image_uri=v_child_image_uri
      AND d.authorizing_grant_digest=v_authorizing_grant_digest AND d.child_job_policy_digest=v_child_job_policy_digest
      AND d.child_service_identity=SESSION_USER() AND d.expires_at>CURRENT_TIMESTAMP()
      AND t.derivation_id IS NULL AND c.consumption_id IS NULL);
  ASSERT v_count>=1 AS 'daily_derivation_unavailable';
  ASSERT v_count<=1 AS 'daily_derivation_ambiguous';
  SELECT d.* FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` t USING (derivation_id)
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
    WHERE d.child_job_resource=v_child_job_resource AND d.child_image_uri=v_child_image_uri
      AND d.authorizing_grant_digest=v_authorizing_grant_digest AND d.child_job_policy_digest=v_child_job_policy_digest
      AND d.child_service_identity=SESSION_USER() AND d.expires_at>CURRENT_TIMESTAMP()
      AND t.derivation_id IS NULL AND c.consumption_id IS NULL;
END;
