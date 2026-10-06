CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_disable_open_intelligence_recurring_grant_v2`(
  grant_sha256 STRING, disable_phrase STRING, reason_code STRING,
  origin_registry_sha256 STRING, resource_manifest_sha256 STRING
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_actor STRING DEFAULT SESSION_USER();
  DECLARE v_grant_sha256 STRING DEFAULT grant_sha256;
  DECLARE v_origin_registry_sha256 STRING DEFAULT origin_registry_sha256;
  DECLARE v_resource_manifest_sha256 STRING DEFAULT resource_manifest_sha256;
  DECLARE v_grant STRUCT<approval_id STRING, approved_by STRING>;
  DECLARE v_payload STRING;
  DECLARE v_id STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  ASSERT CONCAT('usr_',LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:',v_actor)))))='usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
    AS 'recurring_grant_actor_mismatch';
  SET v_grant = (SELECT AS STRUCT approval_id, approved_by FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    WHERE operation = 'recurring_grant_v2' AND manifest_sha256 = v_grant_sha256
      AND origin_registry_sha256 = v_origin_registry_sha256 AND resource_manifest_sha256 = v_resource_manifest_sha256);
  ASSERT v_grant IS NOT NULL AND v_actor = v_grant.approved_by AS 'recurring_grant_actor_mismatch';
  ASSERT disable_phrase = FORMAT('Disable recurring execution grant v2 %s', v_grant_sha256) AS 'recurring_grant_phrase_mismatch';
  ASSERT reason_code IN ('owner_withdrawn','compromised_execution','policy_superseded') AS 'recurring_grant_invalid';
  SET v_payload = TO_JSON_STRING(STRUCT('42_recurring_execution_grant_revocation_v2' AS contract_version,
    v_grant_sha256 AS grant_sha256, v_grant.approval_id AS grant_approval_id, reason_code AS reason_code));
  IF (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
      WHERE operation = 'recurring_grant_revocation_v2' AND manifest_sha256 = v_grant_sha256) > 0 THEN
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
      WHERE operation = 'recurring_grant_revocation_v2' AND manifest_sha256 = v_grant_sha256
        AND canonical_manifest_json = v_payload AND approved_by = v_actor) = 1 AS 'recurring_grant_conflict';
    COMMIT TRANSACTION;
    SELECT approval_id, approved_at FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
      WHERE operation = 'recurring_grant_revocation_v2' AND manifest_sha256 = v_grant_sha256;
    RETURN;
  END IF;
  SET v_lock_version = (SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    WHERE lock_name = 'open_intelligence_execution_approval_v2' AND state = 'ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    SET state = 'approving', lock_version = lock_version + 1, updated_at = v_now
    WHERE lock_name = 'open_intelligence_execution_approval_v2' AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'recurring_grant_conflict';
  SET v_id = CONCAT('exa_', LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(v_now, v_actor, v_grant_sha256, reason_code))))));
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    (approval_contract_version, approval_id, manifest_version, operation, contract_sha256,
     manifest_sha256, canonical_manifest_json, approved_by, approved_at, expires_at,
     approval_phrase_sha256, origin_registry_sha256, resource_manifest_sha256)
  VALUES ('open_intelligence_execution_approval_v2', v_id, '42_recurring_execution_grant_revocation_v2',
    'recurring_grant_revocation_v2', v_grant_sha256, v_grant_sha256, v_payload, v_actor, v_now, v_now,
    LOWER(TO_HEX(SHA256(disable_phrase))), v_origin_registry_sha256, v_resource_manifest_sha256);
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    SET state = 'ready', last_approval_id = v_id, updated_at = v_now
    WHERE lock_name = 'open_intelligence_execution_approval_v2' AND state = 'approving' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'recurring_grant_conflict';
  COMMIT TRANSACTION;
  SELECT v_id AS approval_id, v_now AS approved_at;
END;
