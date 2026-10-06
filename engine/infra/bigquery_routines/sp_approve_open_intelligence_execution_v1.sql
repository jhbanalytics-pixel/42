CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_approve_open_intelligence_execution_v1`(
  canonical_manifest_json STRING,
  manifest_sha256 STRING,
  approval_phrase STRING
)
BEGIN
  DECLARE v_canonical_manifest_json STRING DEFAULT canonical_manifest_json;
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_approval_phrase STRING DEFAULT approval_phrase;
  DECLARE v_actor STRING;
  DECLARE v_approved_at TIMESTAMP;
  DECLARE v_expires_at TIMESTAMP;
  DECLARE v_operation STRING;
  DECLARE v_expected_identity STRING;
  DECLARE v_expected_job STRING;
  DECLARE v_top_level_keys ARRAY<STRING>;
  DECLARE v_manifest_json JSON;
  DECLARE v_artifact_names ARRAY<STRING>;
  DECLARE v_expected_artifact_names ARRAY<STRING>;
  DECLARE v_approval_id STRING;
  DECLARE v_lock_version INT64;
  BEGIN TRANSACTION;
  SET v_actor = CONCAT('usr_', LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:', SESSION_USER())))));
  ASSERT v_actor = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab' AS 'execution_approval_identity_invalid';
  SET v_approved_at = CURRENT_TIMESTAMP();
  SET v_operation = JSON_VALUE(v_canonical_manifest_json, '$.operation');
  SET v_expected_identity = CASE v_operation
    WHEN 'source_snapshot_capture' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'migration_apply' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'collection_exposure_issue' THEN 'trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_apply' THEN 'trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_proof_issue' THEN 'trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_release' THEN 'trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'brain_read' THEN 'trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'wave1_pilot' THEN 'trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com'
  END;
  SET v_expected_job = CASE v_operation
    WHEN 'source_snapshot_capture' THEN 'trends-engine-oi-source-snapshot-staging'
    WHEN 'migration_apply' THEN 'trends-engine-oi-migration-staging'
    WHEN 'collection_exposure_issue' THEN 'trends-engine-oi-exposure-issuer-staging'
    WHEN 'r3_apply' THEN 'trends-engine-oi-apply-staging'
    WHEN 'r3_proof_issue' THEN 'trends-engine-oi-r3-proof-staging'
    WHEN 'r3_release' THEN 'trends-engine-oi-release-staging'
    WHEN 'brain_read' THEN 'trends-engine-oi-brain-staging'
    WHEN 'wave1_pilot' THEN 'trends-engine-open-intelligence-staging'
  END;
  SET v_expires_at = PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', JSON_VALUE(v_canonical_manifest_json, '$.expires_at'));
  SET v_manifest_json = SAFE.PARSE_JSON(v_canonical_manifest_json, wide_number_mode => 'exact');
  ASSERT v_manifest_json IS NOT NULL AS 'execution_approval_manifest_invalid';
  SET v_top_level_keys = JSON_KEYS(v_manifest_json, 1, mode => 'strict');
  ASSERT ARRAY_LENGTH(v_top_level_keys) = 20
    AND NOT EXISTS (
      SELECT 1 FROM UNNEST(v_top_level_keys) AS key
      WHERE key NOT IN (
        'manifest_version', 'operation', 'contract_sha256', 'project', 'datasets',
        'location', 'job_resource', 'service_identity', 'source_sha', 'image_uri',
        'build_resource', 'command', 'arguments', 'environment', 'secrets',
        'max_retries', 'timeout_seconds', 'input_artifacts', 'limits', 'expires_at'
      )
    ) AS 'execution_approval_manifest_invalid';
  ASSERT v_manifest_sha256 = LOWER(TO_HEX(SHA256(v_canonical_manifest_json))) AS 'execution_approval_manifest_mismatch';
  ASSERT JSON_VALUE(v_canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v1' AS 'execution_approval_manifest_invalid';
  ASSERT v_operation IN ('source_snapshot_capture', 'migration_apply', 'collection_exposure_issue', 'r3_apply', 'r3_proof_issue', 'r3_release', 'brain_read', 'wave1_pilot') AS 'execution_approval_manifest_invalid';
  ASSERT REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.contract_sha256'), r'^[0-9a-f]{64}$')
    AND JSON_VALUE(v_canonical_manifest_json, '$.project') = 'ogilvy-trends-v2'
    AND JSON_VALUE(v_canonical_manifest_json, '$.location') = 'US'
    AND JSON_VALUE(v_canonical_manifest_json, '$.service_identity') = v_expected_identity
    AND JSON_VALUE(v_canonical_manifest_json, '$.job_resource') = FORMAT('projects/ogilvy-trends-v2/locations/us-central1/jobs/%s', v_expected_job)
    AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.source_sha'), r'^[0-9a-f]{40}$')
    AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.image_uri'), r'^us-central1-docker[.]pkg[.]dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.build_resource'), r'^projects/ogilvy-trends-v2/locations/us-central1/builds/[^/]+$')
    AS 'execution_approval_target_invalid';
  ASSERT JSON_QUERY(v_canonical_manifest_json, '$.command') = '["python"]'
    AND JSON_VALUE(v_canonical_manifest_json, '$.max_retries') = '0'
    AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.timeout_seconds') AS INT64) BETWEEN 1 AND 10800
    AS 'execution_approval_manifest_invalid';
  ASSERT CASE v_operation
      WHEN 'source_snapshot_capture' THEN ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.arguments')) = 5
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[0]') = 'scripts/staging/capture_protected_production_snapshot.py'
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[1]') = '--cutoff-date'
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[2]') IN ('2026-09-07', '2026-09-08')
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[3]') = '--mode'
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[4]') IN ('initial', 'recover')
        AND JSON_VALUE(v_canonical_manifest_json, '$.contract_sha256') = '5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f'
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.timeout_seconds') AS INT64) BETWEEN 1 AND 600
      WHEN 'migration_apply' THEN JSON_QUERY(v_canonical_manifest_json, '$.arguments') = '["scripts/migrations/create_open_intelligence_v2.py","apply"]'
      WHEN 'collection_exposure_issue' THEN JSON_QUERY(v_canonical_manifest_json, '$.arguments') = '["scripts/staging/issue_collection_exposure_receipts.py"]'
      WHEN 'r3_apply' THEN JSON_QUERY(v_canonical_manifest_json, '$.arguments') = '["scripts/staging/replay_open_intelligence.py"]'
      WHEN 'r3_proof_issue' THEN ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.arguments')) = 3
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[0]') = 'scripts/staging/issue_r3_execution_proof.py'
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[1]') = '--r3-execution-name'
        AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.arguments[2]'), r'^projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-apply-staging/executions/[^/]+$')
      WHEN 'r3_release' THEN JSON_QUERY(v_canonical_manifest_json, '$.arguments') = '["scripts/staging/release_open_intelligence_run.py"]'
      WHEN 'brain_read' THEN ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.arguments')) IN (9, 11)
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[0]') = 'scripts/staging/run_live_intelligence_brain.py'
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[1]') = '--target'
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[2]') = 'staging'
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[3]') = '--run-id'
        AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.arguments[4]'), r'^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$')
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[5]') = '--signal-id'
        AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.arguments[6]'), r'^sig_[0-9a-f]{64}$')
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[7]') = '--research-depth'
        AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[8]') IN ('briefing', 'scan', 'investigation')
        AND (
          JSON_VALUE(v_canonical_manifest_json, '$.arguments[8]') = 'briefing'
          OR ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.arguments')) = 11
        )
        AND (
          ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.arguments')) = 9
          OR (
            JSON_VALUE(v_canonical_manifest_json, '$.arguments[9]') = '--decision-question'
            AND JSON_VALUE(v_canonical_manifest_json, '$.arguments[10]') != ''
            AND NORMALIZE(JSON_VALUE(v_canonical_manifest_json, '$.arguments[10]'), NFC) = JSON_VALUE(v_canonical_manifest_json, '$.arguments[10]')
            AND LENGTH(JSON_VALUE(v_canonical_manifest_json, '$.arguments[10]')) <= 2000
            AND NOT REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.arguments[10]'), r'\p{Cc}')
          )
        )
      WHEN 'wave1_pilot' THEN JSON_QUERY(v_canonical_manifest_json, '$.arguments') = '["scripts/run_rss_now.py"]'
      ELSE FALSE
    END AS 'execution_approval_manifest_invalid';
  ASSERT JSON_QUERY(v_canonical_manifest_json, '$.environment') = IF(
      v_operation = 'wave1_pilot',
      '[{"name":"BIGQUERY_DATASET","value":"trends_v2_staging"},{"name":"GCP_PROJECT","value":"ogilvy-trends-v2"},{"name":"SOCIALCRAWL_CREDENTIAL_LANE","value":"ogilvy_funded"},{"name":"SOCIALCRAWL_FUNDED_STAGE_NAME","value":"stage_1_wave_1"},{"name":"TRENDS_ENV","value":"staging"}]',
      '[{"name":"BIGQUERY_DATASET","value":"trends_v2_staging"},{"name":"GCP_PROJECT","value":"ogilvy-trends-v2"},{"name":"TRENDS_ENV","value":"staging"}]'
    ) AS 'execution_approval_manifest_invalid';
  ASSERT JSON_QUERY(v_canonical_manifest_json, '$.datasets') = IF(
      v_operation = 'wave1_pilot',
      '["trends_v2_staging","trends_v2_staging_funded"]',
      IF(v_operation = 'source_snapshot_capture', '["trends_v2_dev","trends_v2_staging"]', '["trends_v2_staging"]')
    ) AS 'execution_approval_target_invalid';
  ASSERT JSON_QUERY(v_canonical_manifest_json, '$.secrets') = IF(
      v_operation = 'wave1_pilot',
      '["SOCIALCRAWL_OGILVY_API_KEY"]',
      '[]'
    ) AS 'execution_approval_manifest_invalid';
  ASSERT ARRAY_LENGTH(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.input_artifacts')) > 0
    AND NOT EXISTS (
      SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.input_artifacts')) AS artifact
      WHERE JSON_VALUE(artifact, '$.name') IS NULL
        OR NOT REGEXP_CONTAINS(JSON_VALUE(artifact, '$.sha256'), r'^[0-9a-f]{64}$')
        OR SAFE.PARSE_JSON(artifact, wide_number_mode => 'exact') IS NULL
        OR ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(artifact, wide_number_mode => 'exact'), 1, mode => 'strict'), ',') != 'name,sha256'
    ) AS 'execution_approval_artifact_mismatch';
  SET v_artifact_names = ARRAY(
    SELECT JSON_VALUE(artifact, '$.name')
    FROM UNNEST(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.input_artifacts')) AS artifact
    ORDER BY 1
  );
  SET v_expected_artifact_names = CASE v_operation
    WHEN 'source_snapshot_capture' THEN ['build_provenance', 'capture_contract', 'capture_plan', 'recovery_context', 'source_metadata', 'storage_policy']
    WHEN 'migration_apply' THEN ['build_provenance', 'cloud_build', 'migration_contract', 'migration_dry_run', 'migration_plan']
    WHEN 'collection_exposure_issue' THEN ['build_provenance', 'config', 'issuer_contract', 'source_copy_receipt_set', 'vendor_quota_receipt']
    WHEN 'r3_apply' THEN ['build_provenance', 'config', 'exposure_execution_proof', 'exposure_receipt_readback', 'inserted_natural_key_set', 'quality_review_receipt', 'r3_contract', 'source_window_receipt_set']
    WHEN 'r3_proof_issue' THEN ['blocked_run_receipt', 'build_provenance', 'proof_issuer_contract']
    WHEN 'r3_release' THEN ['blocked_run_receipt', 'build_provenance', 'execution_proof', 'quality_review_receipt', 'release_contract']
    WHEN 'brain_read' THEN ['brain_contract', 'build_provenance', 'run_receipt', 'source_window_receipt_set']
    WHEN 'wave1_pilot' THEN ['build_provenance', 'funded_preflight', 'gdelt_dry_run_set', 'r3_seed_manifest', 'source_lab_snapshot', 'wave1_contract']
    ELSE []
  END;
  ASSERT ARRAY_TO_STRING(v_artifact_names, ',') = ARRAY_TO_STRING(v_expected_artifact_names, ',') AS 'execution_approval_artifact_mismatch';
  ASSERT v_operation != 'source_snapshot_capture' OR (
    SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.input_artifacts')) item
    WHERE JSON_VALUE(item, '$.name') = 'capture_contract'
      AND JSON_VALUE(item, '$.sha256') = '5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f'
  ) = 1 AS 'execution_approval_artifact_mismatch';
  ASSERT JSON_QUERY(v_canonical_manifest_json, '$.limits') IS NOT NULL
    AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) >= 0
    AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) >= 0
    AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_model_calls') AS INT64) = 0
    AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) >= 0
    AND CASE v_operation
      WHEN 'source_snapshot_capture' THEN SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) BETWEEN 0 AND 1000000000
        AND JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') = '0'
        AND JSON_VALUE(v_canonical_manifest_json, '$.limits.max_model_calls') = '0'
        AND JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') = '0'
      WHEN 'migration_apply' THEN SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) <= 1
      WHEN 'collection_exposure_issue' THEN SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) = 0
      WHEN 'r3_apply' THEN SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) = 7
      WHEN 'r3_proof_issue' THEN SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) = 0
      WHEN 'r3_release' THEN SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) <= 2
      WHEN 'brain_read' THEN SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) = 0
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) = 0
      WHEN 'wave1_pilot' THEN SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) = 50000000000
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) <= 63
        AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) = 100
      ELSE FALSE
    END
    AS 'execution_approval_manifest_invalid';
  ASSERT v_expires_at > v_approved_at AND v_expires_at <= TIMESTAMP_ADD(v_approved_at, INTERVAL 24 HOUR) AS 'execution_approval_expired';
  ASSERT v_approval_phrase = FORMAT('I approve one staging execution of %s for manifest SHA256 %s. Production remains unchanged.', v_operation, v_manifest_sha256) AS 'execution_approval_manifest_mismatch';
  SET v_approval_id = CONCAT('exa_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"approval_contract_version":"open_intelligence_execution_approval_v1","approved_at":"%s","approved_by":"%s","manifest_sha256":"%s"}',
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_approved_at), v_actor, v_manifest_sha256
  )))));
  SET v_lock_version = (
    SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
    WHERE lock_name = 'open_intelligence_execution_approval_v1'
      AND approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND state = 'ready'
  );
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET lock_version = lock_version + 1, state = 'approving', updated_at = v_approved_at
  WHERE lock_name = 'open_intelligence_execution_approval_v1'
    AND approval_contract_version = 'open_intelligence_execution_approval_v1'
    AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1 WHERE open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256 OR open_intelligence_execution_approvals_v1.approval_id = v_approval_id) = 0 AS 'execution_approval_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS open_intelligence_execution_consumptions_v1 WHERE open_intelligence_execution_consumptions_v1.manifest_sha256 = v_manifest_sha256) = 0 AS 'execution_approval_consumed';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_approvals_v1` VALUES (
    'open_intelligence_execution_approval_v1', v_approval_id,
    JSON_VALUE(v_canonical_manifest_json, '$.manifest_version'), v_operation,
    JSON_VALUE(v_canonical_manifest_json, '$.contract_sha256'), v_manifest_sha256,
    v_canonical_manifest_json, v_actor, v_approved_at, v_expires_at,
    LOWER(TO_HEX(SHA256(v_approval_phrase)))
  );
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` AS open_intelligence_execution_approvals_v1
    WHERE open_intelligence_execution_approvals_v1.approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND open_intelligence_execution_approvals_v1.approval_id = v_approval_id
      AND open_intelligence_execution_approvals_v1.manifest_version = JSON_VALUE(v_canonical_manifest_json, '$.manifest_version')
      AND open_intelligence_execution_approvals_v1.operation = v_operation
      AND open_intelligence_execution_approvals_v1.contract_sha256 = JSON_VALUE(v_canonical_manifest_json, '$.contract_sha256')
      AND open_intelligence_execution_approvals_v1.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_approvals_v1.canonical_manifest_json = v_canonical_manifest_json
      AND open_intelligence_execution_approvals_v1.approved_by = v_actor
      AND open_intelligence_execution_approvals_v1.approved_at = v_approved_at
      AND open_intelligence_execution_approvals_v1.expires_at = v_expires_at
      AND open_intelligence_execution_approvals_v1.approval_phrase_sha256 = LOWER(TO_HEX(SHA256(v_approval_phrase)))) = 1 AS 'execution_approval_schema_mismatch';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET state = 'ready', last_approval_id = v_approval_id, updated_at = v_approved_at
  WHERE lock_name = 'open_intelligence_execution_approval_v1'
    AND approval_contract_version = 'open_intelligence_execution_approval_v1'
    AND state = 'approving' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_approval_receipt_v1' AS contract_version,
         v_approval_id AS approval_id, v_approved_at AS approved_at, v_actor AS approved_by,
         v_expires_at AS expires_at, v_manifest_sha256 AS manifest_sha256, v_operation AS operation;
END;
