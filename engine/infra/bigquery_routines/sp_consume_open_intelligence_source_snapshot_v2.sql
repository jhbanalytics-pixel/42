CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_consume_open_intelligence_source_snapshot_v2`(
  manifest_sha256 STRING,
  execution_name STRING,
  job_resource STRING,
  source_sha STRING,
  image_uri STRING,
  capture_plan_json STRING,
  recovery_context_json STRING,
  storage_policy_json STRING,
  origin_registry_sha256 STRING,
  resource_manifest_sha256 STRING
)
BEGIN
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_execution_name STRING DEFAULT execution_name;
  DECLARE v_job_resource STRING DEFAULT job_resource;
  DECLARE v_source_sha STRING DEFAULT source_sha;
  DECLARE v_image_uri STRING DEFAULT image_uri;
  DECLARE v_origin_registry_sha256 STRING DEFAULT origin_registry_sha256;
  DECLARE v_resource_manifest_sha256 STRING DEFAULT resource_manifest_sha256;
  DECLARE v_approval STRUCT<approval_id STRING, operation STRING, contract_sha256 STRING, canonical_manifest_json STRING, approved_by STRING, approved_at TIMESTAMP, expires_at TIMESTAMP, approval_phrase_sha256 STRING, origin_registry_sha256 STRING, resource_manifest_sha256 STRING>;
  DECLARE v_contract_sha256 STRING;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  DECLARE v_policy_json STRING;
  DECLARE v_origin_row STRING;
  DECLARE v_binding STRING;
  DECLARE v_consumed_at TIMESTAMP;
  DECLARE v_consumption_id STRING;
  DECLARE v_lock_version INT64;
  DECLARE v_capture_plan_json STRING DEFAULT capture_plan_json;
  DECLARE v_recovery_context_json STRING DEFAULT recovery_context_json;
  DECLARE v_storage_policy_json STRING DEFAULT storage_policy_json;
  DECLARE v_plan JSON;
  DECLARE v_storage JSON;
  DECLARE v_grant JSON;
  DECLARE v_recovery JSON;
  DECLARE v_cutoff STRING;
  DECLARE v_mode STRING;
  DECLARE v_grant_id STRING;
  DECLARE v_reserved_per_capture INT64;
  DECLARE v_ceiling INT64;
  DECLARE v_slot_initial_count INT64;
  DECLARE v_slot_recovery_count INT64;
  DECLARE v_grant_initial_count INT64;
  DECLARE v_grant_reserved_micro_usd INT64;
  DECLARE v_initial_result_count INT64;
  DECLARE v_ancestor_context_digest STRING;
  DECLARE v_inner_ancestor_context_digest STRING;
  DECLARE v_metadata_ancestor_context_digest STRING;
  BEGIN TRANSACTION;
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1` WHERE lock_name = 'open_intelligence_execution_approval_v1' AND approval_contract_version = 'open_intelligence_execution_approval_v1' AND state = 'disabled') = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`) = 1 AS 'execution_approval_v1_lock_not_disabled';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2` WHERE lock_name = 'open_intelligence_execution_approval_v2' AND approval_contract_version = 'open_intelligence_execution_approval_v2' AND state = 'ready') = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`) = 1 AS 'execution_approval_lock_not_ready';
  SET v_lock_version = (
    SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
    WHERE lock_name = 'open_intelligence_execution_approval_v2'
      AND approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND state = 'ready'
  );
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
  SET lock_version = lock_version + 1, state = 'consuming', updated_at = CURRENT_TIMESTAMP()
  WHERE lock_name = 'open_intelligence_execution_approval_v2'
    AND approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT REGEXP_CONTAINS(v_origin_registry_sha256, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_resource_manifest_sha256, r'^[0-9a-f]{64}$') AS 'execution_approval_generation_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1`) = 1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_active_generation_v1` WHERE origin_registry_sha256 = v_origin_registry_sha256 AND resource_manifest_sha256 = v_resource_manifest_sha256) = 1 AS 'execution_approval_generation_inactive';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_registries_v1` WHERE origin_registry_sha256 = v_origin_registry_sha256) = 1 AS 'execution_approval_generation_invalid';
  SET v_registry_json = (SELECT canonical_registry_json FROM `{project}.{dataset}.open_intelligence_execution_origin_registries_v1` WHERE origin_registry_sha256 = v_origin_registry_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_registry_json))) = v_origin_registry_sha256 AS 'execution_origin_registry_digest_mismatch';
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_registry_json)
    AND SAFE.PARSE_JSON(v_registry_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_registry_json, '$.contract_version') = 'open_intelligence_execution_origin_registry_v1' AS 'execution_origin_registry_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_resource_manifests_v1` WHERE resource_manifest_sha256 = v_resource_manifest_sha256 AND origin_registry_sha256 = v_origin_registry_sha256) = 1 AS 'execution_approval_generation_invalid';
  SET v_resource_json = (SELECT canonical_resource_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_resource_manifests_v1` WHERE resource_manifest_sha256 = v_resource_manifest_sha256 AND origin_registry_sha256 = v_origin_registry_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_resource_json))) = v_resource_manifest_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_resource_json)
    AND SAFE.PARSE_JSON(v_resource_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_resource_json, '$.origin_registry_sha256') = v_origin_registry_sha256 AS 'execution_approval_generation_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
    WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256) = 1 AS 'execution_approval_unavailable';
  SET v_approval = (
    SELECT AS STRUCT approval_id, operation, contract_sha256, canonical_manifest_json, approved_by, approved_at, expires_at, approval_phrase_sha256, origin_registry_sha256, resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
    WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
  );
  SET v_consumed_at = CURRENT_TIMESTAMP();
  ASSERT v_approval.operation = 'source_snapshot_capture' AS 'source_snapshot_operation_invalid';
  ASSERT v_approval.origin_registry_sha256 = v_origin_registry_sha256
    AND v_approval.resource_manifest_sha256 = v_resource_manifest_sha256 AS 'execution_approval_generation_invalid';
  SET v_contract_sha256 = v_approval.contract_sha256;
  ASSERT (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256) = 1 AS 'execution_origin_pair_invalid';
  SET v_origin_row = (SELECT origin_row FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
    WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256);
  ASSERT 'new_consume' IN UNNEST(JSON_VALUE_ARRAY(v_origin_row, '$.allowed_execution_modes')) AS 'execution_origin_pair_invalid';
  SET v_binding = JSON_QUERY(v_origin_row, '$.exact_operation_bindings.source_snapshot_capture');
  ASSERT v_binding IS NOT NULL AS 'execution_origin_pair_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256) = 1 AS 'execution_origin_policy_invalid';
  SET v_policy_json = (SELECT canonical_policy_json FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256);
  ASSERT LOWER(TO_HEX(SHA256(v_policy_json))) = v_contract_sha256
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_policy_json)
    AND SAFE.PARSE_JSON(v_policy_json, wide_number_mode => 'exact') IS NOT NULL
    AND JSON_VALUE(v_policy_json, '$.contract_version') = 'open_intelligence_execution_origin_policy_v2'
    AND JSON_VALUE(v_policy_json, '$.successor.manifest_version') = 'open_intelligence_execution_manifest_v2'
    AND v_approval.operation IN UNNEST(JSON_VALUE_ARRAY(v_policy_json, '$.successor.operations'))
    AND JSON_VALUE(v_policy_json, '$.origin.image_uri_regex') = JSON_VALUE(v_origin_row, '$.image_uri_regex')
    AND JSON_VALUE(v_policy_json, '$.origin.connected_repository') = JSON_VALUE(v_origin_row, '$.connected_repo') AS 'execution_origin_policy_invalid';
  ASSERT SESSION_USER() = JSON_VALUE(v_binding, '$.service_identity') AS 'execution_approval_identity_invalid';
  ASSERT v_approval.approved_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab'
    AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_approval.canonical_manifest_json)
    AND SAFE.PARSE_JSON(v_approval.canonical_manifest_json, wide_number_mode => 'exact') IS NOT NULL
    AND v_manifest_sha256 = LOWER(TO_HEX(SHA256(v_approval.canonical_manifest_json)))
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.operation') = v_approval.operation
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.contract_sha256') = v_contract_sha256
    AND v_approval.approval_phrase_sha256 = LOWER(TO_HEX(SHA256(FORMAT('I approve one 42 staging execution of %s for manifest SHA256 %s, origin registry SHA256 %s and resource manifest SHA256 %s. Production remains unchanged.', v_approval.operation, v_manifest_sha256, v_origin_registry_sha256, v_resource_manifest_sha256))))
    AND CONCAT('exa_', LOWER(TO_HEX(SHA256(FORMAT(
      '{"approval_contract_version":"open_intelligence_execution_approval_v2","approved_at":"%s","approved_by":"%s","manifest_sha256":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}',
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_approval.approved_at), v_approval.approved_by, v_manifest_sha256,
      v_origin_registry_sha256, v_resource_manifest_sha256
    ))))) = v_approval.approval_id
    AS 'execution_approval_manifest_mismatch';
  ASSERT JSON_VALUE(v_approval.canonical_manifest_json, '$.job_resource') = v_job_resource
    AND JSON_VALUE(v_binding, '$.job_resource') = v_job_resource
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.service_identity') = JSON_VALUE(v_binding, '$.service_identity')
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.source_sha') = v_source_sha
    AND REGEXP_CONTAINS(v_source_sha, JSON_VALUE(v_policy_json, '$.common_manifest_validation.source_sha_regex'))
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.image_uri') = v_image_uri
    AND REGEXP_CONTAINS(v_image_uri, JSON_VALUE(v_origin_row, '$.image_uri_regex'))
    AND STARTS_WITH(v_execution_name, CONCAT(v_job_resource, '/executions/'))
    AND LENGTH(v_execution_name) > LENGTH(CONCAT(v_job_resource, '/executions/'))
    AND NOT REGEXP_CONTAINS(SUBSTR(v_execution_name, LENGTH(CONCAT(v_job_resource, '/executions/')) + 1), '/')
    AS 'execution_approval_execution_mismatch';
  ASSERT v_consumed_at < v_approval.expires_at AS 'execution_approval_expired';
  ASSERT JSON_VALUE(v_approval.canonical_manifest_json, '$.max_retries') = '0'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.limits.max_credits') = '0'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.limits.max_model_calls') = '0'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.limits.max_rows_written') = '0'
    AND SAFE_CAST(JSON_VALUE(v_approval.canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) BETWEEN 0 AND 1000000000
    AS 'source_snapshot_contract_invalid';
  ASSERT (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_approval.canonical_manifest_json, '$.input_artifacts')) AS item
    WHERE JSON_VALUE(item, '$.name') IN ('capture_contract', 'capture_plan', 'recovery_context', 'source_metadata', 'storage_policy')) = 5
    AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_approval.canonical_manifest_json, '$.input_artifacts')) = 5
    AS 'source_snapshot_contract_invalid';
  ASSERT NOT EXISTS (
    SELECT 1 FROM UNNEST([
      STRUCT('capture_plan' AS name, v_capture_plan_json AS body),
      STRUCT('recovery_context' AS name, v_recovery_context_json AS body),
      STRUCT('storage_policy' AS name, v_storage_policy_json AS body)
    ]) AS artifact
    WHERE (SELECT JSON_VALUE(item, '$.sha256') FROM UNNEST(JSON_QUERY_ARRAY(v_approval.canonical_manifest_json, '$.input_artifacts')) AS item WHERE JSON_VALUE(item, '$.name') = artifact.name)
      IS DISTINCT FROM LOWER(TO_HEX(SHA256(artifact.body)))
  ) AS 'source_snapshot_artifact_mismatch';
  SET v_plan = SAFE.PARSE_JSON(v_capture_plan_json, wide_number_mode => 'exact');
  SET v_storage = SAFE.PARSE_JSON(v_storage_policy_json, wide_number_mode => 'exact');
  SET v_recovery = SAFE.PARSE_JSON(v_recovery_context_json, wide_number_mode => 'exact');
  ASSERT JSON_TYPE(v_storage) = 'object'
    AND ARRAY_TO_STRING(JSON_KEYS(v_storage, 1), ',') = 'allowance_id,bucket,contract_version,grant,input_prefix,max_artifact_bytes,max_source_logical_bytes,object_retention_days,output_prefix,price_review,reserved_micro_usd_per_cutoff,snapshot_retention_days'
    AND JSON_VALUE(v_storage, '$.contract_version') = 'open_intelligence_source_capture_storage_v2'
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
    AS 'source_snapshot_storage_policy_invalid';
  SET v_grant = JSON_QUERY(v_storage, '$.grant');
  ASSERT JSON_TYPE(v_grant) = 'object'
    AND ARRAY_TO_STRING(JSON_KEYS(v_grant, 1), ',') = 'allowed_cutoffs,contract_sha256,cumulative_ceiling_micro_usd,environment,grant_id,reserved_micro_usd_per_capture,revocation_state,source_estate_digest,valid_from,valid_until'
    AND JSON_VALUE(v_grant, '$.environment') = 'staging'
    AND REGEXP_CONTAINS(JSON_VALUE(v_grant, '$.grant_id'), r'^[a-z0-9_]+$')
    AND REGEXP_CONTAINS(JSON_VALUE(v_grant, '$.contract_sha256'), r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(JSON_VALUE(v_grant, '$.source_estate_digest'), r'^[0-9a-f]{64}$')
    AND JSON_TYPE(JSON_QUERY(v_grant, '$.reserved_micro_usd_per_capture')) = 'number'
    AND JSON_TYPE(JSON_QUERY(v_grant, '$.cumulative_ceiling_micro_usd')) = 'number'
    AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_grant, '$.allowed_cutoffs')) > 0
    AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_grant, '$.allowed_cutoffs')) = (SELECT COUNT(DISTINCT value) FROM UNNEST(JSON_VALUE_ARRAY(v_grant, '$.allowed_cutoffs')) AS value WHERE SAFE.PARSE_DATE('%Y-%m-%d', value) IS NOT NULL)
    AS 'source_snapshot_grant_invalid';
  SET v_grant_id = JSON_VALUE(v_grant, '$.grant_id');
  SET v_reserved_per_capture = SAFE_CAST(JSON_VALUE(v_grant, '$.reserved_micro_usd_per_capture') AS INT64);
  SET v_ceiling = SAFE_CAST(JSON_VALUE(v_grant, '$.cumulative_ceiling_micro_usd') AS INT64);
  ASSERT v_reserved_per_capture > 0
    AND v_ceiling >= v_reserved_per_capture * ARRAY_LENGTH(JSON_VALUE_ARRAY(v_grant, '$.allowed_cutoffs'))
    AND JSON_VALUE(v_storage, '$.allowance_id') = v_grant_id
    AND SAFE_CAST(JSON_VALUE(v_storage, '$.reserved_micro_usd_per_cutoff') AS INT64) = v_reserved_per_capture
    AS 'source_snapshot_grant_invalid';
  ASSERT JSON_VALUE(v_grant, '$.revocation_state') = 'active' AS 'source_snapshot_grant_revoked';
  ASSERT TIMESTAMP(JSON_VALUE(v_grant, '$.valid_from')) <= v_consumed_at
    AND v_consumed_at < TIMESTAMP(JSON_VALUE(v_grant, '$.valid_until')) AS 'source_snapshot_grant_expired';
  ASSERT v_contract_sha256 = JSON_VALUE(v_grant, '$.contract_sha256') AS 'source_snapshot_contract_invalid';
  SET v_cutoff = JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[2]');
  SET v_mode = JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[4]');
  ASSERT ARRAY_LENGTH(JSON_VALUE_ARRAY(v_approval.canonical_manifest_json, '$.arguments')) = 7
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[0]') = 'scripts/staging/capture_protected_production_snapshot.py'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[1]') = '--cutoff-date'
    AND SAFE.PARSE_DATE('%Y-%m-%d', v_cutoff) IS NOT NULL
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[3]') = '--mode'
    AND v_mode IN ('initial', 'recover')
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[5]') = '--grant'
    AND JSON_VALUE(v_approval.canonical_manifest_json, '$.arguments[6]') = v_grant_id
    AND v_consumed_at >= TIMESTAMP(DATE_ADD(DATE(v_cutoff), INTERVAL 1 DAY))
    AS 'source_snapshot_arguments_invalid';
  ASSERT v_cutoff IN UNNEST(JSON_VALUE_ARRAY(v_grant, '$.allowed_cutoffs')) AS 'source_snapshot_cutoff_not_permitted';
  ASSERT JSON_TYPE(v_plan) = 'object'
    AND ARRAY_TO_STRING(JSON_KEYS(v_plan, 1), ',') = 'client_scope_id,contract_version,creation_statements,cutoff_date,market_scope,snapshot_plan'
    AND JSON_VALUE(v_plan, '$.contract_version') = 'open_intelligence_protected_capture_plan_v2'
    AND JSON_VALUE(v_plan, '$.cutoff_date') = v_cutoff
    AND JSON_VALUE(v_plan, '$.snapshot_plan.profile_version') = '42_staging_source_v2'
    AND JSON_VALUE(v_plan, '$.snapshot_plan.projection_version') = 'native_id_bound_v1'
    AND JSON_VALUE(v_plan, '$.snapshot_plan.source_dataset') = 'intelligence_42_sources_staging'
    AND TIMESTAMP(JSON_VALUE(v_plan, '$.snapshot_plan.observation_window_end')) = TIMESTAMP(DATE_ADD(DATE(v_cutoff), INTERVAL 1 DAY))
    AND TIMESTAMP(JSON_VALUE(v_plan, '$.snapshot_plan.snapshot_as_of')) >= TIMESTAMP(JSON_VALUE(v_plan, '$.snapshot_plan.observation_window_end'))
    AND TIMESTAMP(JSON_VALUE(v_plan, '$.snapshot_plan.snapshot_as_of')) <= v_consumed_at
    AS 'source_snapshot_plan_invalid';
  ASSERT JSON_VALUE(v_plan, '$.snapshot_plan.source_estate_digest') = JSON_VALUE(v_grant, '$.source_estate_digest')
    AND JSON_VALUE(v_plan, '$.snapshot_plan.grant_id') = v_grant_id
    AS 'source_snapshot_estate_mismatch';
  ASSERT ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_storage, '$.price_review'), 1), ',') = 'assumptions,expires_at,maximum_cycle_cost_micro_usd,pricing_sources,reviewed_at'
    AND TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.reviewed_at')) <= v_consumed_at
    AND TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.expires_at')) > v_consumed_at
    AND TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.expires_at')) <= TIMESTAMP_ADD(TIMESTAMP(JSON_VALUE(v_storage, '$.price_review.reviewed_at')), INTERVAL 24 HOUR)
    AND JSON_TYPE(JSON_QUERY(v_storage, '$.price_review.maximum_cycle_cost_micro_usd')) = 'number'
    AND SAFE_CAST(JSON_VALUE(v_storage, '$.price_review.maximum_cycle_cost_micro_usd') AS INT64) BETWEEN 0 AND v_reserved_per_capture - CAST(CEIL(v_reserved_per_capture * 0.10) AS INT64)
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
  SET v_slot_initial_count = (
    SELECT COUNT(*) FROM (
      SELECT c.operation, a.canonical_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
      JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
      UNION ALL
      SELECT c.operation, a.canonical_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c
      JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
    ) AS slot
    WHERE slot.operation = 'source_snapshot_capture' AND JSON_VALUE(slot.canonical_manifest_json, '$.arguments[2]') = v_cutoff
      AND JSON_VALUE(slot.canonical_manifest_json, '$.arguments[4]') = 'initial'
  );
  SET v_slot_recovery_count = (
    SELECT COUNT(*) FROM (
      SELECT c.operation, a.canonical_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
      JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
      UNION ALL
      SELECT c.operation, a.canonical_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c
      JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
    ) AS slot
    WHERE slot.operation = 'source_snapshot_capture' AND JSON_VALUE(slot.canonical_manifest_json, '$.arguments[2]') = v_cutoff
      AND JSON_VALUE(slot.canonical_manifest_json, '$.arguments[4]') = 'recover'
  );
  SET v_grant_initial_count = (
    SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c
    JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
    WHERE c.operation = 'source_snapshot_capture'
      AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[4]') = 'initial'
      AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[6]') = v_grant_id
  );
  SET v_grant_reserved_micro_usd = v_reserved_per_capture * v_grant_initial_count;
  IF v_mode = 'initial' THEN
    ASSERT v_recovery_context_json = 'null' AS 'source_snapshot_recovery_invalid';
    ASSERT v_slot_initial_count = 0 AND v_slot_recovery_count = 0 AS 'source_snapshot_slot_consumed';
    ASSERT v_grant_reserved_micro_usd + v_reserved_per_capture <= v_ceiling AS 'source_snapshot_grant_exhausted';
  ELSE
    ASSERT JSON_TYPE(v_recovery) = 'object'
      AND JSON_VALUE(v_recovery, '$.contract_version') IN ('open_intelligence_source_capture_recovery_v1', 'open_intelligence_source_capture_recovery_v2', 'open_intelligence_source_capture_recovery_v3', 'open_intelligence_source_capture_recovery_v4')
      AS 'source_snapshot_recovery_invalid';
    ASSERT v_slot_initial_count = 1 AS 'source_snapshot_recovery_unavailable';
    ASSERT v_grant_reserved_micro_usd <= v_ceiling AS 'source_snapshot_grant_exhausted';
    IF JSON_VALUE(v_recovery, '$.contract_version') NOT IN ('open_intelligence_source_capture_recovery_v3', 'open_intelligence_source_capture_recovery_v4') THEN
      ASSERT v_slot_recovery_count = 0 AS 'source_snapshot_allowance_consumed';
    ELSEIF JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v3' THEN
      ASSERT v_slot_recovery_count = 1 AS 'source_snapshot_allowance_consumed';
    ELSE
      ASSERT v_slot_recovery_count = 2 AS 'source_snapshot_allowance_consumed';
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
          AND JSON_VALUE(v_recovery, '$.ancestor_recovery_context.contract_version') = 'open_intelligence_source_capture_recovery_v2')
        OR
        (ARRAY_TO_STRING(JSON_KEYS(v_recovery, 1), ',') = 'ancestor_recovery_context,contract_version,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v4'
          AND JSON_TYPE(JSON_QUERY(v_recovery, '$.ancestor_recovery_context')) = 'object'
          AND ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_recovery, '$.ancestor_recovery_context'), 1), ',') = 'ancestor_recovery_context,contract_version,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.ancestor_recovery_context.contract_version') = 'open_intelligence_source_capture_recovery_v3'
          AND JSON_TYPE(JSON_QUERY(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context')) = 'object'
          AND ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context'), 1), ',') = 'contract_version,failed_creation_job_digest,initial_consumption_id,initial_execution_name,initial_manifest_sha256,initial_result_digest,initial_result_id'
          AND JSON_VALUE(v_recovery, '$.ancestor_recovery_context.ancestor_recovery_context.contract_version') = 'open_intelligence_source_capture_recovery_v2')
      )
      AS 'source_snapshot_recovery_invalid';
    SET v_initial_result_count = (
      SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c
      JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` a ON a.approval_id = c.approval_id AND a.manifest_sha256 = c.manifest_sha256
      JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` r ON r.consumption_id = c.consumption_id AND r.approval_id = a.approval_id AND r.manifest_sha256 = a.manifest_sha256 AND r.operation = c.operation AND r.execution_name = c.execution_name
      WHERE c.operation = 'source_snapshot_capture' AND a.operation = c.operation
        AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[2]') = v_cutoff
        AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[4]') = IF(
          JSON_VALUE(v_recovery, '$.contract_version') IN ('open_intelligence_source_capture_recovery_v3', 'open_intelligence_source_capture_recovery_v4'), 'recover', 'initial')
        AND (
          JSON_VALUE(v_recovery, '$.contract_version') NOT IN ('open_intelligence_source_capture_recovery_v3', 'open_intelligence_source_capture_recovery_v4')
          OR (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(a.canonical_manifest_json, '$.input_artifacts')) artifact
            WHERE JSON_VALUE(artifact, '$.name') = 'recovery_context'
              AND JSON_VALUE(artifact, '$.sha256') = IF(JSON_VALUE(v_recovery, '$.contract_version') = 'open_intelligence_source_capture_recovery_v4', v_metadata_ancestor_context_digest, v_ancestor_context_digest)) = 1
        )
        AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[6]') = v_grant_id
        AND c.consumption_id = JSON_VALUE(v_recovery, '$.initial_consumption_id')
        AND c.execution_name = JSON_VALUE(v_recovery, '$.initial_execution_name')
        AND c.manifest_sha256 = JSON_VALUE(v_recovery, '$.initial_manifest_sha256')
        AND r.result_id = JSON_VALUE(v_recovery, '$.initial_result_id')
        AND r.result_digest = JSON_VALUE(v_recovery, '$.initial_result_digest')
        AND r.result_digest = LOWER(TO_HEX(SHA256(r.canonical_result_json)))
        AND r.status IN ('succeeded', 'failed') AND r.completed_at >= c.consumed_at AND r.completed_at <= v_consumed_at
        AND JSON_VALUE(r.canonical_result_json, '$.contract_version') = 'open_intelligence_protected_source_snapshot_v2'
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
    '{"approval_id":"%s","consumed_at":"%s","consumption_contract_version":"open_intelligence_execution_consumption_v2","execution_name":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}',
    v_approval.approval_id, FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_consumed_at),
    v_execution_name, v_origin_registry_sha256, v_resource_manifest_sha256
  )))));
  ASSERT (SELECT COUNT(*) FROM (
      SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1`
      UNION ALL SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
    ) AS consumptions WHERE consumptions.approval_id = v_approval.approval_id OR consumptions.manifest_sha256 = v_manifest_sha256 OR consumptions.consumption_id = v_consumption_id OR consumptions.execution_name = v_execution_name) = 0 AS 'execution_approval_consumed';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v2` VALUES (
    'open_intelligence_execution_consumption_v2', v_consumption_id, v_approval.approval_id,
    v_manifest_sha256, v_approval.operation, v_execution_name, v_job_resource, v_source_sha, v_image_uri,
    v_consumed_at, v_origin_registry_sha256, v_resource_manifest_sha256
  );
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` AS open_intelligence_execution_consumptions_v2
    WHERE open_intelligence_execution_consumptions_v2.consumption_contract_version = 'open_intelligence_execution_consumption_v2'
      AND open_intelligence_execution_consumptions_v2.consumption_id = v_consumption_id
      AND open_intelligence_execution_consumptions_v2.approval_id = v_approval.approval_id
      AND open_intelligence_execution_consumptions_v2.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_consumptions_v2.operation = v_approval.operation
      AND open_intelligence_execution_consumptions_v2.execution_name = v_execution_name
      AND open_intelligence_execution_consumptions_v2.job_resource = v_job_resource
      AND open_intelligence_execution_consumptions_v2.source_sha = v_source_sha
      AND open_intelligence_execution_consumptions_v2.image_uri = v_image_uri
      AND open_intelligence_execution_consumptions_v2.consumed_at = v_consumed_at
      AND open_intelligence_execution_consumptions_v2.origin_registry_sha256 = v_origin_registry_sha256
      AND open_intelligence_execution_consumptions_v2.resource_manifest_sha256 = v_resource_manifest_sha256) = 1 AS 'execution_approval_schema_mismatch';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
  SET state = 'ready', updated_at = v_consumed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v2'
    AND approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND state = 'consuming' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_consumption_v2' AS consumption_contract_version,
         v_consumption_id AS consumption_id, v_approval.approval_id AS approval_id,
         v_manifest_sha256 AS manifest_sha256, v_approval.operation AS operation,
         v_execution_name AS execution_name, v_consumed_at AS consumed_at,
         v_origin_registry_sha256 AS origin_registry_sha256, v_resource_manifest_sha256 AS resource_manifest_sha256,
         v_grant_id AS grant_id, v_cutoff AS cutoff, v_mode AS mode,
         v_grant_reserved_micro_usd + IF(v_mode = 'initial', v_reserved_per_capture, 0) AS grant_reserved_micro_usd,
         v_ceiling AS grant_ceiling_micro_usd;
END;
