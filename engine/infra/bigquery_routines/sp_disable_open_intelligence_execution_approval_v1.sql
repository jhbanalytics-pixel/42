CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_disable_open_intelligence_execution_approval_v1`(
  approval_phrase STRING
)
BEGIN
  DECLARE v_actor STRING;
  DECLARE v_disabled_at TIMESTAMP;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  SET v_actor = CONCAT('usr_', LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:', SESSION_USER())))));
  ASSERT v_actor = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab' AS 'execution_approval_identity_invalid';
  ASSERT approval_phrase = 'I disable new 42 staging execution approvals. Production remains unchanged.' AS 'execution_approval_manifest_mismatch';
  SET v_disabled_at = CURRENT_TIMESTAMP();
  SET v_lock_version = (SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1` WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'ready');
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET lock_version = lock_version + 1, state = 'disabled', updated_at = v_disabled_at
  WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1` WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'disabled' AND lock_version = v_lock_version + 1) = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_disable_receipt_v1' AS contract_version,
         'disabled' AS state, v_disabled_at AS disabled_at, v_actor AS disabled_by,
         v_lock_version + 1 AS lock_version;
END;
