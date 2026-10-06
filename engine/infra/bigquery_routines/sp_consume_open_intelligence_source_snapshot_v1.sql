CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_consume_open_intelligence_source_snapshot_v1`(
  manifest_sha256 STRING,
  execution_name STRING,
  job_resource STRING,
  source_sha STRING,
  image_uri STRING,
  capture_plan_json STRING,
  recovery_context_json STRING,
  storage_policy_json STRING
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
  DECLARE v_capture_plan_json STRING DEFAULT capture_plan_json;
  DECLARE v_recovery_context_json STRING DEFAULT recovery_context_json;
  DECLARE v_storage_policy_json STRING DEFAULT storage_policy_json;
  DECLARE v_plan JSON;
  DECLARE v_storage JSON;
  DECLARE v_recovery JSON;
  DECLARE v_cutoff STRING;
  DECLARE v_mode STRING;
  DECLARE v_initial_count INT64;
  DECLARE v_recovery_count INT64;
  DECLARE v_reserved_micro_usd INT64;
  DECLARE v_initial_result_count INT64;
  DECLARE v_ancestor_context_digest STRING;
  DECLARE v_inner_ancestor_context_digest STRING;
  DECLARE v_metadata_ancestor_context_digest STRING;
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
  ASSERT v_approval.operation = 'source_snapshot_capture' AS 'source_snapshot_operation_invalid';
  SET v_expected_identity = 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com';
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
  ASSERT JSON_VALUE(v_approval.canonical_manifest_json, '$.contract_sha256') = '5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f'
    AND v_job_resource = 'projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging'
    AND JSON_QUERY(v_approval.canonical_manifest_json, '$.datasets') = '["trends_v2_dev","trends_v2_staging"]'
    AND JSON_QUERY(v_approval.canonical_manifest_json, '$.command') = '["python"]'
    AND JSON_QUERY(v_approval.canonical_manifest_json, '$.secrets') = '[]'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.max_retries') = '0'
    AND SAFE_CAST(JSON_VALUE(v_approval.canonical_manifest_json, '$.timeout_seconds') AS INT64) BETWEEN 1 AND 600
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.limits.max_credits') = '0'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.limits.max_model_calls') = '0'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.limits.max_rows_written') = '0'
    AND SAFE_CAST(JSON_VALUE(v_approval.canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) BETWEEN 0 AND 1000000000
    AS 'source_snapshot_contract_invalid';
  SET v_cutoff = JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[2]');
  SET v_mode = JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[4]');
  ASSERT ARRAY_LENGTH(JSON_VALUE_ARRAY(v_approval.canonical_manifest_json, '$.arguments')) = 5
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[0]') = 'scripts/staging/capture_protected_production_snapshot.py'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[1]') = '--cutoff-date'
    AND v_cutoff IN ('2026-09-07', '2026-09-08')
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[3]') = '--mode'
    AND v_mode IN ('initial', 'recover')
    AND v_consumed_at >= TIMESTAMP(DATE_ADD(DATE(v_cutoff), INTERVAL 1 DAY))
    AS 'source_snapshot_arguments_invalid';
  ASSERT (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_approval.canonical_manifest_json, '$.input_artifacts')) AS item
    WHERE JSON_VALUE(item, '$.name') = 'capture_contract'
      AND JSON_VALUE(item, '$.sha256') = '5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f') = 1
    AS 'source_snapshot_contract_invalid';
  ASSERT NOT EXISTS (
    SELECT 1 FROM UNNEST([
      STRUCT('capture_plan' AS name, v_capture_plan_json AS body),
      STRUCT('recovery_context' AS name, v_recovery_context_json AS body),
      STRUCT('storage_policy' AS name, v_storage_policy_json AS body)
    ]) AS supplied
    WHERE supplied.body IS NULL OR (
      SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_approval.canonical_manifest_json, '$.input_artifacts')) AS item
      WHERE JSON_VALUE(item, '$.name') = supplied.name
        AND JSON_VALUE(item, '$.sha256') = LOWER(TO_HEX(SHA256(supplied.body)))
    ) != 1
  ) AS 'source_snapshot_artifact_mismatch';
  SET v_plan = SAFE.PARSE_JSON(v_capture_plan_json, wide_number_mode => 'exact');
  SET v_storage = SAFE.PARSE_JSON(v_storage_policy_json, wide_number_mode => 'exact');
  SET v_recovery = SAFE.PARSE_JSON(v_recovery_context_json, wide_number_mode => 'exact');
  ASSERT JSON_TYPE(v_plan) = 'object'
    AND ARRAY_TO_STRING(JSON_KEYS(v_plan, 1), ',') = 'client_scope_id,contract_version,creation_statements,cutoff_date,market_scope,snapshot_plan'
    AND JSON_VALUE(v_plan, '$.contract_version') = 'open_intelligence_protected_capture_plan_v1'
    AND JSON_VALUE(v_plan, '$.cutoff_date') = v_cutoff
    AND NULLIF(TRIM(JSON_VALUE(v_plan, '$.client_scope_id')), '') IS NOT NULL
    AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_plan, '$.market_scope')) BETWEEN 1 AND 3
    AND TO_JSON_STRING(JSON_VALUE_ARRAY(v_plan, '$.market_scope')) = TO_JSON_STRING(ARRAY(
      SELECT DISTINCT market FROM UNNEST(JSON_VALUE_ARRAY(v_plan, '$.market_scope')) market
      WHERE market IN ('ke', 'ng', 'za') ORDER BY market))
    AND JSON_VALUE(v_plan, '$.snapshot_plan.cutoff_date') = v_cutoff
    AND JSON_VALUE(v_plan, '$.snapshot_plan.profile_id') = 'trends_v2_dev_table_snapshot_v1'
    AND TIMESTAMP(JSON_VALUE(v_plan, '$.snapshot_plan.source_as_of')) = TIMESTAMP(DATE_ADD(DATE(v_cutoff), INTERVAL 1 DAY))
    AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_plan, '$.creation_statements')) = 5
    AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_plan, '$.snapshot_plan.statements')) = 5
    AS 'source_snapshot_plan_invalid';
  ASSERT JSON_VALUE(v_plan, '$.snapshot_plan.source_metadata_digest') = (
    SELECT JSON_VALUE(item, '$.sha256') FROM UNNEST(JSON_QUERY_ARRAY(v_approval.canonical_manifest_json, '$.input_artifacts')) item
    WHERE JSON_VALUE(item, '$.name') = 'source_metadata'
  ) AS 'source_snapshot_artifact_mismatch';
  ASSERT NOT EXISTS (
    SELECT 1 FROM UNNEST(['event_ledger','seed_graph','seed_candidates','enriched_content','raw_content']) lane WITH OFFSET position
    WHERE JSON_VALUE(JSON_QUERY_ARRAY(v_plan, '$.creation_statements')[OFFSET(position)], '$.lane') IS DISTINCT FROM lane
      OR JSON_VALUE(JSON_QUERY_ARRAY(v_plan, '$.snapshot_plan.statements')[OFFSET(position)], '$.lane') IS DISTINCT FROM lane
      OR JSON_VALUE(JSON_QUERY_ARRAY(v_plan, '$.snapshot_plan.statements')[OFFSET(position)], '$.source_table') IS DISTINCT FROM CONCAT('ogilvy-trends-v2.trends_v2_dev.', lane)
      OR JSON_VALUE(JSON_QUERY_ARRAY(v_plan, '$.snapshot_plan.statements')[OFFSET(position)], '$.destination_table') IS DISTINCT FROM CONCAT('ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_', REPLACE(v_cutoff, '-', ''), '_', lane)
  ) AS 'source_snapshot_plan_invalid';
  ASSERT JSON_TYPE(v_storage) = 'object'
    AND ARRAY_TO_STRING(JSON_KEYS(v_storage, 1), ',') = 'allowance_id,bucket,contract_version,input_prefix,max_artifact_bytes,max_source_logical_bytes,object_retention_days,output_prefix,price_review,reserved_micro_usd_per_cutoff,snapshot_retention_days'
    AND JSON_VALUE(v_storage, '$.contract_version') = 'open_intelligence_source_capture_storage_v1'
    AND JSON_VALUE(v_storage, '$.allowance_id') = 'source_capture_20260907_20260908_v1'
    AND JSON_VALUE(v_storage, '$.bucket') = 'ogilvy-trends-v2-oi-source-artifacts-staging'
    AND JSON_VALUE(v_storage, '$.input_prefix') = 'inputs/'
    AND JSON_VALUE(v_storage, '$.output_prefix') = 'captures/'
    AND JSON_TYPE(JSON_QUERY(v_storage, '$.snapshot_retention_days')) = 'number'
    AND JSON_VALUE(v_storage, '$.snapshot_retention_days') = '90'
    AND JSON_TYPE(JSON_QUERY(v_storage, '$.object_retention_days')) = 'number'
    AND JSON_VALUE(v_storage, '$.object_retention_days') = '90'
    AND JSON_TYPE(JSON_QUERY(v_storage, '$.max_source_logical_bytes')) = 'number'
    AND JSON_VALUE(v_storage, '$.max_source_logical_bytes') = '5368709120'
    AND JSON_TYPE(JSON_QUERY(v_storage, '$.max_artifact_bytes')) = 'number'
    AND JSON_VALUE(v_storage, '$.max_artifact_bytes') = '536870912'
    AND JSON_TYPE(JSON_QUERY(v_storage, '$.reserved_micro_usd_per_cutoff')) = 'number'
    AND JSON_VALUE(v_storage, '$.reserved_micro_usd_per_cutoff') = '500000'
    AS 'source_snapshot_storage_policy_invalid';
  ASSERT ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_storage, '$.price_review'), 1), ',') = 'assumptions,expires_at,maximum_cycle_cost_micro_usd,pricing_sources,reviewed_at'
    AND TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.reviewed_at')) <= v_consumed_at
    AND TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.expires_at')) > v_consumed_at
    AND TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.expires_at')) <= TIMESTAMP_ADD(TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.reviewed_at')), INTERVAL 24 HOUR)
    AND JSON_TYPE(JSON_QUERY(v_storage, '$.price_review.maximum_cycle_cost_micro_usd')) = 'number'
    AND SAFE_CAST(JSON_VALUE(v_storage, '$.price_review.maximum_cycle_cost_micro_usd') AS INT64) BETWEEN 0 AND 500000
    AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_storage, '$.price_review.pricing_sources')) > 0
    AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_storage, '$.price_review.assumptions')) > 0
    AS 'source_snapshot_price_review_invalid';
  ASSERT TO_JSON_STRING(JSON_VALUE_ARRAY(v_storage, '$.price_review.pricing_sources')) = TO_JSON_STRING(ARRAY(
    SELECT DISTINCT value FROM UNNEST(JSON_VALUE_ARRAY(v_storage, '$.price_review.pricing_sources')) value
    WHERE REGEXP_CONTAINS(value, r'^https://[^\s]+$') ORDER BY value))
    AND TO_JSON_STRING(JSON_VALUE_ARRAY(v_storage, '$.price_review.assumptions')) = TO_JSON_STRING(ARRAY(
    SELECT DISTINCT value FROM UNNEST(JSON_VALUE_ARRAY(v_storage, '$.price_review.assumptions')) value
    WHERE NULLIF(TRIM(value), '') IS NOT NULL ORDER BY value))
    AS 'source_snapshot_price_review_invalid';
  ASSERT NOT EXISTS (
    SELECT 1 FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
    LEFT JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a
      ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
    WHERE c.operation = 'source_snapshot_capture' AND (
      a.approval_id IS NULL OR a.operation IS DISTINCT FROM c.operation
      OR a.manifest_sha256 IS DISTINCT FROM LOWER(TO_HEX(SHA256(a.canonical_manifest_json)))
      OR JSON_VALUE(a.canonical_manifest_json, '$.contract_sha256') IS DISTINCT FROM '5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f'
      OR COALESCE(JSON_VALUE(a.canonical_manifest_json, '$.arguments[2]'), '') NOT IN ('2026-09-07', '2026-09-08')
      OR COALESCE(JSON_VALUE(a.canonical_manifest_json, '$.arguments[4]'), '') NOT IN ('initial', 'recover')
    )
  ) AS 'source_snapshot_allowance_state_invalid';
  SET v_initial_count = (
    SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
    JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
    WHERE c.operation = 'source_snapshot_capture' AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[2]') = v_cutoff
      AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[4]') = 'initial'
  );
  SET v_recovery_count = (
    SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
    JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
    WHERE c.operation = 'source_snapshot_capture' AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[2]') = v_cutoff
      AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[4]') = 'recover'
  );
  SET v_reserved_micro_usd = 500000 * (
    SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
    JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
    WHERE c.operation = 'source_snapshot_capture' AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[4]') = 'initial'
  );
  IF v_mode = 'initial' THEN
    ASSERT v_recovery_context_json = 'null' AS 'source_snapshot_recovery_invalid';
    ASSERT v_initial_count = 0 AND v_recovery_count = 0 AND v_reserved_micro_usd + 500000 <= 1000000
      AS 'source_snapshot_allowance_consumed';
  ELSE
    IF JSON_VALUE(v_recovery, '$.contract_version') NOT IN ('open_intelligence_source_capture_recovery_v3', 'open_intelligence_source_capture_recovery_v4') THEN
      ASSERT v_initial_count = 1 AND v_recovery_count = 0 AND v_reserved_micro_usd <= 1000000
        AS 'source_snapshot_allowance_consumed';
    ELSEIF JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v3' THEN
      ASSERT v_initial_count = 1 AND v_recovery_count = 1 AND v_reserved_micro_usd <= 1000000
        AS 'source_snapshot_allowance_consumed';
    ELSE
      ASSERT v_initial_count = 1 AND v_recovery_count = 2 AND v_reserved_micro_usd <= 1000000
        AS 'source_snapshot_allowance_consumed';
    END IF;
    SET v_ancestor_context_digest = LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.contract_version') AS contract_version,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.failed_creation_job_digest') AS failed_creation_job_digest,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_consumption_id') AS initial_consumption_id,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_execution_name') AS initial_execution_name,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_manifest_sha256') AS initial_manifest_sha256,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_result_digest') AS initial_result_digest,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_result_id') AS initial_result_id
    )))));
    SET v_inner_ancestor_context_digest = LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.contract_version') AS contract_version,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.failed_creation_job_digest') AS failed_creation_job_digest,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_consumption_id') AS initial_consumption_id,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_execution_name') AS initial_execution_name,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_manifest_sha256') AS initial_manifest_sha256,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_result_digest') AS initial_result_digest,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_result_id') AS initial_result_id
      )))));
    SET v_metadata_ancestor_context_digest = LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(
      STRUCT(
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.contract_version') AS contract_version,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.failed_creation_job_digest') AS failed_creation_job_digest,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_consumption_id') AS initial_consumption_id,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_execution_name') AS initial_execution_name,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_manifest_sha256') AS initial_manifest_sha256,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_result_digest') AS initial_result_digest,
        JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.initial_result_id') AS initial_result_id
      ) AS ancestor_recovery_context,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.contract_version') AS contract_version,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_consumption_id') AS initial_consumption_id,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_execution_name') AS initial_execution_name,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_manifest_sha256') AS initial_manifest_sha256,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_result_digest') AS initial_result_digest,
      JSON_VALUE(v_recovery, '$.ancestor_recovery_context.initial_result_id') AS initial_result_id
    )))));
    ASSERT JSON_TYPE(v_recovery) = 'object'
      AND (
        (ARRAY_TO_STRING(JSON_KEYS(v_recovery, 1), ',') = 'contract_version,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v1')
        OR
        (ARRAY_TO_STRING(JSON_KEYS(v_recovery, 1), ',') = 'contract_version,failed_creation_job_digest,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v2'
          AND JSON_TYPE(JSON_QUERY(v_recovery, '$.failed_creation_job_digest')) = 'string'
          AND REGEXP_CONTAINS(JSON_VALUE(v_recovery, '$.failed_creation_job_digest'), r'^[0-9a-f]{64}$'))
        OR
        (ARRAY_TO_STRING(JSON_KEYS(v_recovery, 1), ',') = 'ancestor_recovery_context,contract_version,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v3'
          AND JSON_TYPE(JSON_QUERY(v_recovery, '$.ancestor_recovery_context')) = 'object'
          AND ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_recovery, '$.ancestor_recovery_context'), 1), ',') = 'contract_version,failed_creation_job_digest,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.ancestor_recovery_context.contract_version') = 'open_intelligence_source_capture_recovery_v2'
          AND v_ancestor_context_digest = 'ade29835ba6ca2192ad2ae7ae3d87d97567ce7ea4796390ce7f5c934fd1a1d95')
        OR
        (ARRAY_TO_STRING(JSON_KEYS(v_recovery, 1), ',') = 'ancestor_recovery_context,contract_version,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v4'
          AND JSON_TYPE(JSON_QUERY(v_recovery, '$.ancestor_recovery_context')) = 'object'
          AND ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_recovery, '$.ancestor_recovery_context'), 1), ',') = 'ancestor_recovery_context,contract_version,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.ancestor_recovery_context.contract_version') = 'open_intelligence_source_capture_recovery_v3'
          AND JSON_TYPE(JSON_QUERY(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context')) = 'object'
          AND ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context'), 1), ',') = 'contract_version,failed_creation_job_digest,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.contract_version') = 'open_intelligence_source_capture_recovery_v2'
          AND v_metadata_ancestor_context_digest = '656e88826f4a5c9178d754b5a3ce213c9e7d52127be3ef6427a56feed4bd9b7c'
          AND v_inner_ancestor_context_digest = 'ade29835ba6ca2192ad2ae7ae3d87d97567ce7ea4796390ce7f5c934fd1a1d95')
      )
      AS 'source_snapshot_recovery_invalid';
    SET v_initial_result_count = (
      SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
      JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
      JOIN `{project}.{dataset}.open_intelligence_execution_results_v1` r ON r.consumption_id = c.consumption_id AND r.approval_id = a.approval_id AND r.manifest_sha256 = a.manifest_sha256 AND r.operation = c.operation AND r.execution_name = c.execution_name
      WHERE c.operation = 'source_snapshot_capture' AND a.operation = c.operation
        AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[2]') = v_cutoff
        AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[4]') = IF(
          JSON_VALUE(v_recovery, '$.contract_version') IN ('open_intelligence_source_capture_recovery_v3', 'open_intelligence_source_capture_recovery_v4'), 'recover', 'initial')
        AND (
          JSON_VALUE(v_recovery, '$.contract_version') NOT IN ('open_intelligence_source_capture_recovery_v3', 'open_intelligence_source_capture_recovery_v4')
          OR (
            ((JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v3' AND v_ancestor_context_digest = 'ade29835ba6ca2192ad2ae7ae3d87d97567ce7ea4796390ce7f5c934fd1a1d95')
              OR (JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v4'
                AND v_metadata_ancestor_context_digest = '656e88826f4a5c9178d754b5a3ce213c9e7d52127be3ef6427a56feed4bd9b7c' AND v_inner_ancestor_context_digest = 'ade29835ba6ca2192ad2ae7ae3d87d97567ce7ea4796390ce7f5c934fd1a1d95'
                AND c.manifest_sha256 = '1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d16974'
                AND c.consumption_id = 'exc_2aa600d87b7f29678cac363231de9d50e8feb2865ab033d9b593d8b670a66292'
                AND r.result_id = 'exr_1e53fdb9a6876cbc557efd6db93ff221883eb6e8b844276cc67960c3630a077d'
                AND r.result_digest = '72fe27188cd1ac022c4acc22ac096f91a56dc0ad601cd02020671980a144712c'))
            AND (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(a.canonical_manifest_json, '$.input_artifacts')) artifact
              WHERE JSON_VALUE(artifact, '$.name') = 'recovery_context'
                AND JSON_VALUE(artifact, '$.sha256') = IF(JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v4', v_metadata_ancestor_context_digest, v_ancestor_context_digest)) = 1
          )
        )
        AND c.consumption_id = JSON_VALUE(v_recovery, '$.initial_consumption_id')
        AND c.execution_name = JSON_VALUE(v_recovery, '$.initial_execution_name')
        AND c.manifest_sha256 = JSON_VALUE(v_recovery, '$.initial_manifest_sha256')
        AND r.result_id = JSON_VALUE(v_recovery, '$.initial_result_id')
        AND r.result_digest = JSON_VALUE(v_recovery, '$.initial_result_digest')
        AND r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))
        AND r.status IN ('succeeded', 'failed') AND r.completed_at >= c.consumed_at AND r.completed_at <= v_consumed_at
        AND JSON_VALUE(r.canonical_result_json, '$.contract_version') = 'open_intelligence_protected_source_snapshot_v1'
        AND JSON_VALUE(r.canonical_result_json, '$.cutoff_date') = v_cutoff
        AND (
          JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v1'
          OR (
            r.status = 'failed'
            AND JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(r.canonical_result_json), '$.query_count')) = 'number'
            AND JSON_VALUE(r.canonical_result_json, '$.query_count') = '0'
            AND JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(r.canonical_result_json), '$.captured_at')) = 'null'
            AND JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(r.canonical_result_json), '$.snapshot_digest')) = 'null'
            AND JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(r.canonical_result_json), '$.capture_receipt_digest')) = 'null'
            AND JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(r.canonical_result_json), '$.artifact_attempt')) = 'null'
            AND JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(r.canonical_result_json), '$.stored_artifact')) = 'null'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM UNNEST(['capture_contract', 'capture_plan', 'source_metadata']) name
          WHERE (SELECT JSON_VALUE(item, '$.sha256') FROM UNNEST(JSON_QUERY_ARRAY(a.canonical_manifest_json, '$.input_artifacts')) item WHERE JSON_VALUE(item, '$.name') = name)
            IS DISTINCT FROM
            (SELECT JSON_VALUE(item, '$.sha256') FROM UNNEST(JSON_QUERY_ARRAY(v_approval.canonical_manifest_json, '$.input_artifacts')) item WHERE JSON_VALUE(item, '$.name') = name)
        )
    );
    ASSERT v_initial_result_count = 1 AS 'source_snapshot_recovery_unavailable';
  END IF;
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
