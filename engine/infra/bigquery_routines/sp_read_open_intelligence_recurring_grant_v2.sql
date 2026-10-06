CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_recurring_grant_v2`(grant_sha256 STRING)
BEGIN
  DECLARE v_count INT64;
  DECLARE v_grant_sha256 STRING DEFAULT grant_sha256;
  SET v_count = (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    WHERE operation = 'recurring_grant_v2' AND manifest_sha256 = v_grant_sha256);
  ASSERT v_count >= 1 AS 'recurring_grant_unavailable';
  ASSERT v_count <= 1 AS 'recurring_grant_ambiguous';
  ASSERT CONCAT('usr_',LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:',SESSION_USER())))))='usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
    OR SESSION_USER() IN UNNEST(JSON_VALUE_ARRAY((SELECT canonical_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` WHERE operation='recurring_grant_v2' AND manifest_sha256=v_grant_sha256),'$.executing_principals'))
    AS 'recurring_grant_actor_mismatch';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    WHERE operation = 'recurring_grant_revocation_v2' AND manifest_sha256 = v_grant_sha256) <= 1
    AS 'recurring_grant_ambiguous';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    WHERE operation='recurring_grant_v2' AND manifest_sha256=v_grant_sha256
      AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_manifest_json)
      AND LOWER(TO_HEX(SHA256(canonical_manifest_json)))=manifest_sha256)=1 AS 'recurring_grant_invalid';
  ASSERT NOT EXISTS(SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
    WHERE operation='recurring_grant_revocation_v2' AND manifest_sha256=v_grant_sha256
      AND (NOT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_manifest_json)
        OR JSON_VALUE(canonical_manifest_json,'$.contract_version')!='42_recurring_execution_grant_revocation_v2'
        OR JSON_VALUE(canonical_manifest_json,'$.grant_sha256')!=v_grant_sha256)) AS 'recurring_grant_invalid';
  SELECT SAFE.PARSE_JSON(grant.canonical_manifest_json, wide_number_mode => 'exact') AS grant,
    grant.manifest_sha256 AS grant_sha256, grant.approval_id, grant.approved_by, grant.approved_at,
    grant.expires_at, grant.approval_phrase_sha256, grant.origin_registry_sha256,
    grant.resource_manifest_sha256,
    CASE WHEN revocation.approval_id IS NOT NULL THEN 'revoked'
      WHEN CURRENT_TIMESTAMP() < TIMESTAMP(JSON_VALUE(grant.canonical_manifest_json, '$.valid_from')) THEN 'not_yet_valid'
      WHEN CURRENT_TIMESTAMP() >= grant.expires_at THEN 'expired' ELSE 'active' END AS state,
    IF(revocation.approval_id IS NULL, NULL, SAFE.PARSE_JSON(revocation.canonical_manifest_json, wide_number_mode => 'exact')) AS revocation
  FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` grant
  LEFT JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` revocation
    ON revocation.operation = 'recurring_grant_revocation_v2' AND revocation.manifest_sha256 = grant.manifest_sha256
  WHERE grant.operation = 'recurring_grant_v2' AND grant.manifest_sha256 = v_grant_sha256
    AND LOWER(TO_HEX(SHA256(grant.canonical_manifest_json))) = grant.manifest_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(grant.canonical_manifest_json);
END;
