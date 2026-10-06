CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_approve_open_intelligence_execution_v3`(
  canonical_manifest_json STRING,
  manifest_sha256 STRING,
  approval_phrase STRING,
  origin_registry_sha256 STRING,
  resource_manifest_sha256 STRING
)
BEGIN
  DECLARE v_canonical_manifest_json STRING DEFAULT canonical_manifest_json;
  DECLARE v_manifest_sha256 STRING DEFAULT manifest_sha256;
  DECLARE v_approval_phrase STRING DEFAULT approval_phrase;
  DECLARE v_origin_registry_sha256 STRING DEFAULT origin_registry_sha256;
  DECLARE v_resource_manifest_sha256 STRING DEFAULT resource_manifest_sha256;
  DECLARE v_actor STRING;
  DECLARE v_approved_at TIMESTAMP;
  DECLARE v_expires_at TIMESTAMP;
  DECLARE v_operation STRING;
  DECLARE v_contract_sha256 STRING;
  DECLARE v_registry_json STRING;
  DECLARE v_resource_json STRING;
  DECLARE v_policy_json STRING;
  DECLARE v_origin_row STRING;
  DECLARE v_binding STRING;
  DECLARE v_rule STRING;
  DECLARE v_top_level_keys ARRAY<STRING>;
  DECLARE v_manifest_json JSON;
  DECLARE v_arguments ARRAY<STRING>;
  DECLARE v_rule_prefix ARRAY<STRING>;
  DECLARE v_artifact_names ARRAY<STRING>;
  DECLARE v_approval_id STRING;
  DECLARE v_lock_version INT64;
  DECLARE v_kind STRING;
  DECLARE v_manifest_version STRING;
  DECLARE v_valid_from TIMESTAMP;
  DECLARE v_valid_until TIMESTAMP;
  DECLARE v_cutoffs ARRAY<STRING>;
  DECLARE v_grant_row STRUCT<contract_sha256 STRING, canonical_manifest_json STRING, approved_by STRING, expires_at TIMESTAMP>;
  BEGIN TRANSACTION;
  SET v_actor = CONCAT('usr_', LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:', SESSION_USER())))));
  ASSERT v_actor = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab' AS 'execution_approval_identity_invalid';
  SET v_approved_at = CURRENT_TIMESTAMP();
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
  SET lock_version = lock_version + 1, state = 'approving', updated_at = v_approved_at
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
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_canonical_manifest_json) AS 'execution_approval_manifest_invalid';
  SET v_manifest_json = SAFE.PARSE_JSON(v_canonical_manifest_json, wide_number_mode => 'exact');
  ASSERT v_manifest_json IS NOT NULL AND JSON_TYPE(v_manifest_json) = 'object' AS 'execution_approval_manifest_invalid';
  SET v_kind = CASE
    WHEN JSON_VALUE(v_canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v2' THEN 'execution_manifest_v2'
    WHEN JSON_VALUE(v_canonical_manifest_json, '$.contract_version') = '42_recurring_execution_grant_v1'
      AND STARTS_WITH(v_approval_phrase, 'Approve recurring execution grant ') THEN 'recurring_grant_v1'
    WHEN JSON_VALUE(v_canonical_manifest_json, '$.contract_version') = '42_recurring_execution_grant_v1'
      AND STARTS_WITH(v_approval_phrase, 'Revoke recurring execution grant ') THEN 'recurring_grant_revocation_v1'
    ELSE NULL
  END;
  ASSERT v_kind IS NOT NULL AS 'execution_approval_manifest_invalid';
  IF v_kind = 'execution_manifest_v2' THEN
    SET v_manifest_version = JSON_VALUE(v_canonical_manifest_json, '$.manifest_version');
    ASSERT v_manifest_sha256 = LOWER(TO_HEX(SHA256(v_canonical_manifest_json))) AS 'execution_approval_manifest_mismatch';
    SET v_operation = JSON_VALUE(v_canonical_manifest_json, '$.operation');
    SET v_contract_sha256 = JSON_VALUE(v_canonical_manifest_json, '$.contract_sha256');
    ASSERT JSON_VALUE(v_canonical_manifest_json, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND REGEXP_CONTAINS(v_contract_sha256, r'^[0-9a-f]{64}$')
      AND v_operation IS NOT NULL AS 'execution_approval_manifest_invalid';
    ASSERT (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
      WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
        AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256) = 1 AS 'execution_origin_pair_invalid';
    SET v_origin_row = (SELECT origin_row FROM UNNEST(JSON_QUERY_ARRAY(v_registry_json, '$.rows')) AS origin_row
      WHERE JSON_VALUE(origin_row, '$.manifest_version') = 'open_intelligence_execution_manifest_v2'
        AND JSON_VALUE(origin_row, '$.contract_sha256') = v_contract_sha256);
    ASSERT 'new_approval' IN UNNEST(JSON_VALUE_ARRAY(v_origin_row, '$.allowed_execution_modes')) AS 'execution_origin_pair_invalid';
    SET v_binding = CASE v_operation
      WHEN 'migration_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.migration_apply')
      WHEN 'collection_exposure_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.collection_exposure_issue')
      WHEN 'r3_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_apply')
      WHEN 'r3_proof_issue' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_proof_issue')
      WHEN 'r3_release' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.r3_release')
      WHEN 'brain_read' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.brain_read')
      WHEN 'wave1_pilot' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.wave1_pilot')
      WHEN 'source_snapshot_capture' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.source_snapshot_capture')
      WHEN 'source_collection' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.source_collection')
      WHEN 'daily_composition_apply' THEN JSON_QUERY(v_origin_row, '$.exact_operation_bindings.daily_composition_apply')
      ELSE NULL
    END;
    ASSERT v_binding IS NOT NULL
      AND JSON_VALUE(v_binding, '$.job_resource') IN UNNEST(JSON_VALUE_ARRAY(v_origin_row, '$.job_resources'))
      AND JSON_VALUE(v_binding, '$.service_identity') IN UNNEST(JSON_VALUE_ARRAY(v_origin_row, '$.service_identities')) AS 'execution_origin_pair_invalid';
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256) = 1 AS 'execution_origin_policy_invalid';
    SET v_policy_json = (SELECT canonical_policy_json FROM `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` WHERE contract_sha256 = v_contract_sha256);
    ASSERT LOWER(TO_HEX(SHA256(v_policy_json))) = v_contract_sha256
      AND `{project}.{dataset}.fn_is_canonical_execution_json_v1`(v_policy_json)
      AND SAFE.PARSE_JSON(v_policy_json, wide_number_mode => 'exact') IS NOT NULL
      AND JSON_VALUE(v_policy_json, '$.contract_version') = 'open_intelligence_execution_origin_policy_v2'
      AND JSON_VALUE(v_policy_json, '$.successor.manifest_version') = 'open_intelligence_execution_manifest_v2'
      AND v_operation IN UNNEST(JSON_VALUE_ARRAY(v_policy_json, '$.successor.operations'))
      AND JSON_VALUE(v_policy_json, '$.origin.image_uri_regex') = JSON_VALUE(v_origin_row, '$.image_uri_regex')
      AND JSON_VALUE(v_policy_json, '$.origin.connected_repository') = JSON_VALUE(v_origin_row, '$.connected_repo') AS 'execution_origin_policy_invalid';
    SET v_rule = CASE v_operation
      WHEN 'migration_apply' THEN JSON_QUERY(v_policy_json, '$.operation_validation.migration_apply')
      WHEN 'collection_exposure_issue' THEN JSON_QUERY(v_policy_json, '$.operation_validation.collection_exposure_issue')
      WHEN 'r3_apply' THEN JSON_QUERY(v_policy_json, '$.operation_validation.r3_apply')
      WHEN 'r3_proof_issue' THEN JSON_QUERY(v_policy_json, '$.operation_validation.r3_proof_issue')
      WHEN 'r3_release' THEN JSON_QUERY(v_policy_json, '$.operation_validation.r3_release')
      WHEN 'brain_read' THEN JSON_QUERY(v_policy_json, '$.operation_validation.brain_read')
      WHEN 'wave1_pilot' THEN JSON_QUERY(v_policy_json, '$.operation_validation.wave1_pilot')
      WHEN 'source_snapshot_capture' THEN JSON_QUERY(v_policy_json, '$.operation_validation.source_snapshot_capture')
      WHEN 'source_collection' THEN JSON_QUERY(v_policy_json, '$.operation_validation.source_collection')
      WHEN 'daily_composition_apply' THEN JSON_QUERY(v_policy_json, '$.operation_validation.daily_composition_apply')
      ELSE NULL
    END;
    ASSERT v_rule IS NOT NULL
      AND JSON_VALUE(v_rule, '$.job_resource') = JSON_VALUE(v_binding, '$.job_resource')
      AND JSON_VALUE(v_rule, '$.service_identity') = JSON_VALUE(v_binding, '$.service_identity')
      AND JSON_QUERY(v_rule, '$.datasets') = JSON_QUERY(v_binding, '$.datasets') AS 'execution_origin_policy_invalid';
    SET v_top_level_keys = JSON_KEYS(v_manifest_json, 1, mode => 'strict');
    ASSERT ARRAY_LENGTH(v_top_level_keys) = 20
      AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_policy_json, '$.common_manifest_validation.top_level_fields')) = 20
      AND NOT EXISTS (
        SELECT 1 FROM UNNEST(v_top_level_keys) AS key
        WHERE key NOT IN UNNEST(JSON_VALUE_ARRAY(v_policy_json, '$.common_manifest_validation.top_level_fields'))
      ) AS 'execution_approval_manifest_invalid';
    ASSERT JSON_VALUE(v_canonical_manifest_json, '$.project') = JSON_VALUE(v_policy_json, '$.common_manifest_validation.project')
      AND JSON_VALUE(v_canonical_manifest_json, '$.location') = JSON_VALUE(v_policy_json, '$.common_manifest_validation.location')
      AS 'execution_approval_target_invalid';
    ASSERT ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.datasets')) > 0
      AND TO_JSON_STRING(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.datasets')) = TO_JSON_STRING(ARRAY(
        SELECT DISTINCT dataset FROM UNNEST(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.datasets')) dataset ORDER BY dataset))
      AS 'execution_approval_manifest_invalid';
    ASSERT JSON_QUERY(v_canonical_manifest_json, '$.datasets') = JSON_QUERY(v_rule, '$.datasets')
      AND JSON_VALUE(v_canonical_manifest_json, '$.job_resource') = JSON_VALUE(v_rule, '$.job_resource')
      AS 'execution_approval_target_invalid';
    ASSERT JSON_VALUE(v_canonical_manifest_json, '$.service_identity') = JSON_VALUE(v_rule, '$.service_identity')
      AS 'execution_approval_identity_invalid';
    ASSERT REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.source_sha'), JSON_VALUE(v_policy_json, '$.common_manifest_validation.source_sha_regex'))
      AS 'execution_approval_manifest_invalid';
    ASSERT REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.image_uri'), JSON_VALUE(v_origin_row, '$.image_uri_regex'))
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.build_resource'), JSON_VALUE(v_policy_json, '$.common_manifest_validation.build_resource_regex'))
      AS 'execution_approval_target_invalid';
    SET v_arguments = JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.arguments');
    SET v_rule_prefix = IFNULL(JSON_VALUE_ARRAY(v_rule, '$.arguments.prefix'), ARRAY<STRING>[]);
    ASSERT JSON_QUERY(v_canonical_manifest_json, '$.command') = JSON_QUERY(v_rule, '$.command')
      AND v_arguments IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM UNNEST(v_arguments) AS argument WHERE argument = '')
      AND CASE JSON_VALUE(v_rule, '$.arguments.kind')
        WHEN 'exact' THEN JSON_QUERY(v_canonical_manifest_json, '$.arguments') = JSON_QUERY(v_rule, '$.arguments.value')
        WHEN 'r3_execution_reference' THEN ARRAY_LENGTH(v_arguments) = ARRAY_LENGTH(v_rule_prefix) + 1
          AND TO_JSON_STRING(ARRAY(SELECT argument FROM UNNEST(v_arguments) argument WITH OFFSET position WHERE position < ARRAY_LENGTH(v_rule_prefix) ORDER BY position)) = TO_JSON_STRING(v_rule_prefix)
          AND REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(ARRAY_LENGTH(v_arguments) - 1)], JSON_VALUE(v_rule, '$.arguments.execution_resource_regex'))
        WHEN 'brain_read_v1' THEN ARRAY_LENGTH(v_arguments) IN UNNEST(ARRAY(SELECT SAFE_CAST(rule_length AS INT64) FROM UNNEST(JSON_VALUE_ARRAY(v_rule, '$.arguments.lengths')) rule_length))
          AND TO_JSON_STRING(ARRAY(SELECT argument FROM UNNEST(v_arguments) argument WITH OFFSET position WHERE position < ARRAY_LENGTH(v_rule_prefix) ORDER BY position)) = TO_JSON_STRING(v_rule_prefix)
          AND REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(4)], JSON_VALUE(v_rule, '$.arguments.run_id_regex'))
          AND v_arguments[SAFE_OFFSET(5)] = JSON_VALUE(v_rule, '$.arguments.signal_switch')
          AND REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(6)], JSON_VALUE(v_rule, '$.arguments.signal_id_regex'))
          AND v_arguments[SAFE_OFFSET(7)] = JSON_VALUE(v_rule, '$.arguments.depth_switch')
          AND v_arguments[SAFE_OFFSET(8)] IN UNNEST(JSON_VALUE_ARRAY(v_rule, '$.arguments.depths'))
          AND (
            ARRAY_LENGTH(v_arguments) = 11
            OR v_arguments[SAFE_OFFSET(8)] NOT IN UNNEST(JSON_VALUE_ARRAY(v_rule, '$.arguments.decision_required_for'))
          )
          AND (
            ARRAY_LENGTH(v_arguments) = 9
            OR (
              v_arguments[SAFE_OFFSET(9)] = JSON_VALUE(v_rule, '$.arguments.decision_switch')
              AND LENGTH(v_arguments[SAFE_OFFSET(10)]) <= SAFE_CAST(JSON_VALUE(v_rule, '$.arguments.decision_max_characters') AS INT64)
              AND (
                JSON_VALUE(v_rule, '$.arguments.decision_control_characters_forbidden') != 'true'
                OR NOT REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(10)], r'\p{Cc}')
              )
            )
          )
        WHEN 'source_snapshot_capture_v2' THEN ARRAY_LENGTH(v_arguments) = 7
          AND ARRAY_LENGTH(v_arguments) = SAFE_CAST(JSON_VALUE(v_rule, '$.arguments.length') AS INT64)
          AND TO_JSON_STRING(ARRAY(SELECT argument FROM UNNEST(v_arguments) argument WITH OFFSET position WHERE position < ARRAY_LENGTH(v_rule_prefix) ORDER BY position)) = TO_JSON_STRING(v_rule_prefix)
          AND REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(2)], JSON_VALUE(v_rule, '$.arguments.cutoff_date_regex'))
          AND SAFE.PARSE_DATE('%Y-%m-%d', v_arguments[SAFE_OFFSET(2)]) IS NOT NULL
          AND v_arguments[SAFE_OFFSET(3)] = JSON_VALUE(v_rule, '$.arguments.mode_switch')
          AND v_arguments[SAFE_OFFSET(4)] IN UNNEST(JSON_VALUE_ARRAY(v_rule, '$.arguments.modes'))
          AND v_arguments[SAFE_OFFSET(5)] = JSON_VALUE(v_rule, '$.arguments.grant_switch')
          AND REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(6)], JSON_VALUE(v_rule, '$.arguments.grant_id_regex'))
        WHEN 'prefix_pattern_v1' THEN ARRAY_LENGTH(v_rule_prefix) > 0
          AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_rule, '$.arguments.pattern')) > 0
          AND ARRAY_LENGTH(v_arguments) = ARRAY_LENGTH(v_rule_prefix) + ARRAY_LENGTH(JSON_QUERY_ARRAY(v_rule, '$.arguments.pattern'))
          AND TO_JSON_STRING(ARRAY(SELECT argument FROM UNNEST(v_arguments) argument WITH OFFSET position WHERE position < ARRAY_LENGTH(v_rule_prefix) ORDER BY position)) = TO_JSON_STRING(v_rule_prefix)
          AND NOT EXISTS (
            SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_rule, '$.arguments.pattern')) AS item WITH OFFSET position
            WHERE NOT IFNULL(CASE ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(item, wide_number_mode => 'exact'), 1, mode => 'strict'), ',')
              WHEN 'literal' THEN JSON_TYPE(JSON_QUERY(SAFE.PARSE_JSON(item, wide_number_mode => 'exact'), '$.literal')) = 'string'
                AND v_arguments[SAFE_OFFSET(ARRAY_LENGTH(v_rule_prefix) + position)] = JSON_VALUE(item, '$.literal')
              WHEN 'regex' THEN STARTS_WITH(JSON_VALUE(item, '$.regex'), '^')
                AND ENDS_WITH(JSON_VALUE(item, '$.regex'), '$')
                AND REGEXP_CONTAINS(v_arguments[SAFE_OFFSET(ARRAY_LENGTH(v_rule_prefix) + position)], CONCAT('^(?:', JSON_VALUE(item, '$.regex'), ')$'))
              ELSE FALSE
            END, FALSE)
          )
        ELSE FALSE
      END AS 'execution_approval_manifest_invalid';
    ASSERT JSON_QUERY(v_canonical_manifest_json, '$.environment') = JSON_QUERY(v_rule, '$.environment')
      AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.environment')) > 0
      AND NOT EXISTS (
        SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.environment')) AS item
        WHERE ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(item, wide_number_mode => 'exact'), 1, mode => 'strict'), ',') != 'name,value'
          OR JSON_VALUE(item, '$.name') IS NULL OR JSON_VALUE(item, '$.name') = ''
          OR JSON_VALUE(item, '$.value') IS NULL OR JSON_VALUE(item, '$.value') = ''
      ) AS 'execution_approval_manifest_invalid';
    ASSERT JSON_QUERY(v_canonical_manifest_json, '$.secrets') = JSON_QUERY(v_rule, '$.secrets')
      AND TO_JSON_STRING(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.secrets')) = TO_JSON_STRING(ARRAY(
        SELECT secret FROM UNNEST(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.secrets')) secret ORDER BY secret))
      AS 'execution_approval_manifest_invalid';
    ASSERT JSON_TYPE(JSON_QUERY(v_manifest_json, '$.max_retries')) = 'number'
      AND JSON_VALUE(v_canonical_manifest_json, '$.max_retries') = JSON_VALUE(v_policy_json, '$.common_manifest_validation.max_retries.exact_integer')
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.timeout_seconds')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.timeout_seconds'), r'^(0|-?[1-9][0-9]*)$')
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.timeout_seconds') AS INT64)
        BETWEEN SAFE_CAST(JSON_VALUE(v_policy_json, '$.common_manifest_validation.timeout_seconds.integer_minimum') AS INT64)
        AND SAFE_CAST(JSON_VALUE(v_policy_json, '$.common_manifest_validation.timeout_seconds.integer_maximum') AS INT64)
      AS 'execution_approval_manifest_invalid';
    ASSERT ARRAY_LENGTH(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.input_artifacts')) > 0
      AND NOT EXISTS (
        SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.input_artifacts')) AS artifact
        WHERE JSON_VALUE(artifact, '$.name') IS NULL
          OR JSON_VALUE(artifact, '$.name') = ''
          OR NOT REGEXP_CONTAINS(JSON_VALUE(artifact, '$.sha256'), JSON_VALUE(v_rule, '$.input_artifact_digest_regex'))
          OR SAFE.PARSE_JSON(artifact, wide_number_mode => 'exact') IS NULL
          OR ARRAY_TO_STRING(JSON_KEYS(SAFE.PARSE_JSON(artifact, wide_number_mode => 'exact'), 1, mode => 'strict'), ',') != 'name,sha256'
      ) AS 'execution_approval_artifact_mismatch';
    SET v_artifact_names = ARRAY(
      SELECT JSON_VALUE(artifact, '$.name')
      FROM UNNEST(JSON_QUERY_ARRAY(v_canonical_manifest_json, '$.input_artifacts')) AS artifact WITH OFFSET position
      ORDER BY position
    );
    ASSERT TO_JSON_STRING(v_artifact_names) = TO_JSON_STRING(JSON_VALUE_ARRAY(v_rule, '$.input_artifact_names'))
      AND TO_JSON_STRING(v_artifact_names) = TO_JSON_STRING(ARRAY(SELECT DISTINCT name FROM UNNEST(v_artifact_names) name ORDER BY name))
      AS 'execution_approval_artifact_mismatch';
    ASSERT JSON_TYPE(JSON_QUERY(v_manifest_json, '$.limits')) = 'object'
      AND ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_manifest_json, '$.limits'), 1, mode => 'strict'), ',') = 'max_bytes_billed,max_credits,max_model_calls,max_rows_written'
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.limits.max_bytes_billed')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed'), r'^(0|-?[1-9][0-9]*)$')
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) IS NOT NULL
      AND (JSON_VALUE(v_rule, '$.limits.max_bytes_billed.exact') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) = SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_bytes_billed.exact') AS INT64))
      AND (JSON_VALUE(v_rule, '$.limits.max_bytes_billed.minimum') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) >= SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_bytes_billed.minimum') AS INT64))
      AND (JSON_VALUE(v_rule, '$.limits.max_bytes_billed.maximum') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_bytes_billed') AS INT64) <= SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_bytes_billed.maximum') AS INT64))
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.limits.max_credits')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits'), r'^(0|-?[1-9][0-9]*)$')
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) IS NOT NULL
      AND (JSON_VALUE(v_rule, '$.limits.max_credits.exact') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) = SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_credits.exact') AS INT64))
      AND (JSON_VALUE(v_rule, '$.limits.max_credits.minimum') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) >= SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_credits.minimum') AS INT64))
      AND (JSON_VALUE(v_rule, '$.limits.max_credits.maximum') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_credits') AS INT64) <= SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_credits.maximum') AS INT64))
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.limits.max_model_calls')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_model_calls'), r'^(0|-?[1-9][0-9]*)$')
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_model_calls') AS INT64) IS NOT NULL
      AND (JSON_VALUE(v_rule, '$.limits.max_model_calls.exact') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_model_calls') AS INT64) = SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_model_calls.exact') AS INT64))
      AND (JSON_VALUE(v_rule, '$.limits.max_model_calls.minimum') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_model_calls') AS INT64) >= SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_model_calls.minimum') AS INT64))
      AND (JSON_VALUE(v_rule, '$.limits.max_model_calls.maximum') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_model_calls') AS INT64) <= SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_model_calls.maximum') AS INT64))
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.limits.max_rows_written')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written'), r'^(0|-?[1-9][0-9]*)$')
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) IS NOT NULL
      AND (JSON_VALUE(v_rule, '$.limits.max_rows_written.exact') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) = SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_rows_written.exact') AS INT64))
      AND (JSON_VALUE(v_rule, '$.limits.max_rows_written.minimum') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) >= SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_rows_written.minimum') AS INT64))
      AND (JSON_VALUE(v_rule, '$.limits.max_rows_written.maximum') IS NULL OR SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.limits.max_rows_written') AS INT64) <= SAFE_CAST(JSON_VALUE(v_rule, '$.limits.max_rows_written.maximum') AS INT64))
      AS 'execution_approval_manifest_invalid';
    ASSERT REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.expires_at'), JSON_VALUE(v_policy_json, '$.common_manifest_validation.expires_at_regex'))
      AND SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', JSON_VALUE(v_canonical_manifest_json, '$.expires_at')) IS NOT NULL
      AS 'execution_approval_manifest_invalid';
    SET v_expires_at = PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', JSON_VALUE(v_canonical_manifest_json, '$.expires_at'));
    ASSERT v_expires_at > v_approved_at AND v_expires_at <= TIMESTAMP_ADD(v_approved_at, INTERVAL 24 HOUR) AS 'execution_approval_expired';
    ASSERT v_approval_phrase = FORMAT('I approve one 42 staging execution of %s for manifest SHA256 %s, origin registry SHA256 %s and resource manifest SHA256 %s. Production remains unchanged.', v_operation, v_manifest_sha256, v_origin_registry_sha256, v_resource_manifest_sha256) AS 'execution_approval_manifest_mismatch';
  ELSEIF v_kind = 'recurring_grant_v1' THEN
    SET v_manifest_version = '42_recurring_execution_grant_v1';
    SET v_operation = 'recurring_grant_v1';
    ASSERT ARRAY_TO_STRING(JSON_KEYS(v_manifest_json, 1, mode => 'strict'), ',') = 'allowed_operations,approved_release_manifest_digest,contract_version,cumulative_ceiling_micro_usd,environment,executing_principals,grant_id,issuing_principal,monthly_allowance_micro_usd,permitted_cutoffs,permitted_image_digests,reserved_micro_usd_per_capture,resource_manifest_digest,retry_rules,revocation_phrase_sha256,revocation_state,run_allowance_micro_usd,schema_version,source_policy_digest,valid_from,valid_until'
      AS 'execution_approval_manifest_invalid';
    ASSERT JSON_VALUE(v_canonical_manifest_json, '$.contract_version') = '42_recurring_execution_grant_v1'
      AND JSON_VALUE(v_canonical_manifest_json, '$.environment') = 'staging'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.grant_id'), r'^[a-z0-9_]+$')
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.schema_version'), r'^[a-z0-9_]+$')
      AND JSON_VALUE(v_canonical_manifest_json, '$.revocation_state') = 'active'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.resource_manifest_digest'), r'^[0-9a-f]{64}$')
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.source_policy_digest'), r'^[0-9a-f]{64}$')
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.approved_release_manifest_digest'), r'^[0-9a-f]{64}$')
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.revocation_phrase_sha256'), r'^[0-9a-f]{64}$')
      AS 'execution_approval_manifest_invalid';
    ASSERT JSON_VALUE(v_canonical_manifest_json, '$.resource_manifest_digest') = v_resource_manifest_sha256 AS 'execution_approval_generation_invalid';
    ASSERT JSON_VALUE(v_canonical_manifest_json, '$.issuing_principal') = v_actor AS 'execution_approval_identity_invalid';
    ASSERT ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.executing_principals')) > 0
      AND NOT EXISTS (SELECT 1 FROM UNNEST(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.executing_principals')) AS principal
        WHERE NOT REGEXP_CONTAINS(principal, r'^[a-z][a-z0-9-]*@[a-z0-9-]+\.iam\.gserviceaccount\.com$'))
      AND TO_JSON_STRING(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.executing_principals')) = TO_JSON_STRING(ARRAY(
        SELECT DISTINCT principal FROM UNNEST(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.executing_principals')) principal ORDER BY principal))
      AS 'execution_approval_manifest_invalid';
    ASSERT ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.allowed_operations')) > 0
      AND NOT EXISTS (SELECT 1 FROM UNNEST(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.allowed_operations')) AS operation
        WHERE operation NOT IN ('collection', 'immutable_capture', 'composition', 'quality_proof_issuance', 'release_evidence_qualified_staging_results'))
      AND (SELECT COUNT(DISTINCT operation) FROM UNNEST(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.allowed_operations')) operation)
        = ARRAY_LENGTH(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.allowed_operations'))
      AS 'execution_approval_manifest_invalid';
    ASSERT NOT EXISTS (SELECT 1 FROM UNNEST(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.permitted_image_digests')) AS image
        WHERE NOT REGEXP_CONTAINS(image, r'^sha256:[0-9a-f]{64}$'))
      AND TO_JSON_STRING(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.permitted_image_digests')) = TO_JSON_STRING(ARRAY(
        SELECT DISTINCT image FROM UNNEST(JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.permitted_image_digests')) image ORDER BY image))
      AS 'execution_approval_manifest_invalid';
    ASSERT JSON_TYPE(JSON_QUERY(v_manifest_json, '$.retry_rules')) = 'object'
      AND ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_manifest_json, '$.retry_rules'), 1, mode => 'strict'), ',') = 'failed_stage,max_attempt_versions,unknown_or_partial'
      AND JSON_VALUE(v_canonical_manifest_json, '$.retry_rules.failed_stage') = 'retry_safe_only'
      AND JSON_VALUE(v_canonical_manifest_json, '$.retry_rules.unknown_or_partial') = 'hold_for_reconciliation'
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.retry_rules.max_attempt_versions')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.retry_rules.max_attempt_versions'), r'^[1-9][0-9]*$')
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.retry_rules.max_attempt_versions') AS INT64) >= 2
      AS 'execution_approval_manifest_invalid';
    ASSERT JSON_TYPE(JSON_QUERY(v_manifest_json, '$.run_allowance_micro_usd')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.run_allowance_micro_usd'), r'^(0|[1-9][0-9]*)$')
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.monthly_allowance_micro_usd')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.monthly_allowance_micro_usd'), r'^(0|[1-9][0-9]*)$')
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.reserved_micro_usd_per_capture')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.reserved_micro_usd_per_capture'), r'^[1-9][0-9]*$')
      AND JSON_TYPE(JSON_QUERY(v_manifest_json, '$.cumulative_ceiling_micro_usd')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_canonical_manifest_json, '$.cumulative_ceiling_micro_usd'), r'^(0|[1-9][0-9]*)$')
      AND (SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.reserved_micro_usd_per_capture') AS INT64) - SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.run_allowance_micro_usd') AS INT64)) * 10
        >= SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.reserved_micro_usd_per_capture') AS INT64)
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.monthly_allowance_micro_usd') AS INT64) >= SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.cumulative_ceiling_micro_usd') AS INT64)
      AS 'execution_approval_manifest_invalid';
    ASSERT SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.valid_from') AS TIMESTAMP) IS NOT NULL
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.valid_until') AS TIMESTAMP) IS NOT NULL
      AS 'execution_approval_manifest_invalid';
    SET v_valid_from = TIMESTAMP(JSON_VALUE(v_canonical_manifest_json, '$.valid_from'));
    SET v_valid_until = TIMESTAMP(JSON_VALUE(v_canonical_manifest_json, '$.valid_until'));
    ASSERT v_valid_until > v_valid_from AS 'execution_approval_manifest_invalid';
    SET v_cutoffs = JSON_VALUE_ARRAY(v_canonical_manifest_json, '$.permitted_cutoffs');
    ASSERT ARRAY_LENGTH(v_cutoffs) > 0
      AND NOT EXISTS (SELECT 1 FROM UNNEST(v_cutoffs) AS cutoff
        WHERE SAFE.PARSE_DATE('%Y-%m-%d', cutoff) IS NULL
          OR TIMESTAMP(SAFE.PARSE_DATE('%Y-%m-%d', cutoff)) < v_valid_from
          OR TIMESTAMP(SAFE.PARSE_DATE('%Y-%m-%d', cutoff)) >= v_valid_until)
      AND TO_JSON_STRING(v_cutoffs) = TO_JSON_STRING(ARRAY(SELECT DISTINCT cutoff FROM UNNEST(v_cutoffs) cutoff ORDER BY cutoff))
      AND SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.cumulative_ceiling_micro_usd') AS INT64)
        >= SAFE_CAST(JSON_VALUE(v_canonical_manifest_json, '$.reserved_micro_usd_per_capture') AS INT64) * ARRAY_LENGTH(v_cutoffs)
      AS 'execution_approval_manifest_invalid';
    ASSERT LENGTH(v_canonical_manifest_json) - LENGTH(REPLACE(v_canonical_manifest_json, ',"revocation_state":"active"', '')) = LENGTH(',"revocation_state":"active"')
      AND v_manifest_sha256 = LOWER(TO_HEX(SHA256(REPLACE(v_canonical_manifest_json, ',"revocation_state":"active"', '')))) AS 'execution_approval_manifest_mismatch';
    ASSERT REGEXP_CONTAINS(v_approval_phrase, r'^Approve recurring execution grant [0-9a-f]{64}$') AS 'execution_approval_manifest_mismatch';
    SET v_contract_sha256 = REGEXP_EXTRACT(v_approval_phrase, r'^Approve recurring execution grant ([0-9a-f]{64})$');
    ASSERT v_approval_phrase = CONCAT('Approve recurring execution grant ', v_contract_sha256) AS 'execution_approval_manifest_mismatch';
    SET v_expires_at = v_valid_until;
    ASSERT v_approved_at < v_expires_at AS 'execution_approval_expired';
  ELSE
    SET v_manifest_version = '42_recurring_execution_grant_v1';
    SET v_operation = 'recurring_grant_revocation_v1';
    ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
      WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
        AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
        AND open_intelligence_execution_approvals_v2.operation = 'recurring_grant_v1') = 1 AS 'execution_approval_unavailable';
    SET v_grant_row = (
      SELECT AS STRUCT contract_sha256, canonical_manifest_json, approved_by, expires_at
      FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
      WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
        AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
        AND open_intelligence_execution_approvals_v2.operation = 'recurring_grant_v1'
    );
    ASSERT v_grant_row.canonical_manifest_json = v_canonical_manifest_json
      AND v_grant_row.approved_by = v_actor
      AND LENGTH(v_canonical_manifest_json) - LENGTH(REPLACE(v_canonical_manifest_json, ',"revocation_state":"active"', '')) = LENGTH(',"revocation_state":"active"')
      AND v_manifest_sha256 = LOWER(TO_HEX(SHA256(REPLACE(v_canonical_manifest_json, ',"revocation_state":"active"', '')))) AS 'execution_approval_manifest_mismatch';
    ASSERT STARTS_WITH(v_approval_phrase, 'Revoke recurring execution grant ')
      AND LOWER(TO_HEX(SHA256(v_approval_phrase))) = JSON_VALUE(v_canonical_manifest_json, '$.revocation_phrase_sha256') AS 'execution_approval_manifest_mismatch';
    SET v_contract_sha256 = v_grant_row.contract_sha256;
    SET v_expires_at = v_grant_row.expires_at;
  END IF;
  SET v_approval_id = CONCAT('exa_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"approval_contract_version":"open_intelligence_execution_approval_v2","approved_at":"%s","approved_by":"%s","manifest_sha256":"%s","origin_registry_sha256":"%s","resource_manifest_sha256":"%s"}',
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_approved_at), v_actor, v_manifest_sha256,
    v_origin_registry_sha256, v_resource_manifest_sha256
  )))));
  IF v_kind = 'recurring_grant_revocation_v1' THEN
    ASSERT (SELECT COUNT(*) FROM (
        SELECT approval_id, manifest_sha256, operation FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1`
        UNION ALL SELECT approval_id, manifest_sha256, operation FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
      ) AS approvals WHERE approvals.approval_id = v_approval_id
        OR (approvals.manifest_sha256 = v_manifest_sha256 AND approvals.operation != 'recurring_grant_v1')) = 0 AS 'execution_approval_conflict';
  ELSE
    ASSERT (SELECT COUNT(*) FROM (
        SELECT approval_id, manifest_sha256 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1`
        UNION ALL SELECT approval_id, manifest_sha256 FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2`
      ) AS approvals WHERE approvals.manifest_sha256 = v_manifest_sha256 OR approvals.approval_id = v_approval_id) = 0 AS 'execution_approval_conflict';
  END IF;
  ASSERT (SELECT COUNT(*) FROM (
      SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1`
      UNION ALL SELECT approval_id, manifest_sha256, consumption_id, execution_name FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2`
    ) AS consumptions WHERE consumptions.manifest_sha256 = v_manifest_sha256 OR consumptions.approval_id = v_approval_id) = 0 AS 'execution_approval_consumed';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_approvals_v2` VALUES (
    'open_intelligence_execution_approval_v2', v_approval_id,
    v_manifest_version, v_operation,
    v_contract_sha256, v_manifest_sha256,
    v_canonical_manifest_json, v_actor, v_approved_at, v_expires_at,
    LOWER(TO_HEX(SHA256(v_approval_phrase))),
    v_origin_registry_sha256, v_resource_manifest_sha256
  );
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_approvals_v2` AS open_intelligence_execution_approvals_v2
    WHERE open_intelligence_execution_approvals_v2.approval_contract_version = 'open_intelligence_execution_approval_v2'
      AND open_intelligence_execution_approvals_v2.approval_id = v_approval_id
      AND open_intelligence_execution_approvals_v2.manifest_version = v_manifest_version
      AND open_intelligence_execution_approvals_v2.operation = v_operation
      AND open_intelligence_execution_approvals_v2.contract_sha256 = v_contract_sha256
      AND open_intelligence_execution_approvals_v2.manifest_sha256 = v_manifest_sha256
      AND open_intelligence_execution_approvals_v2.canonical_manifest_json = v_canonical_manifest_json
      AND open_intelligence_execution_approvals_v2.approved_by = v_actor
      AND open_intelligence_execution_approvals_v2.approved_at = v_approved_at
      AND open_intelligence_execution_approvals_v2.expires_at = v_expires_at
      AND open_intelligence_execution_approvals_v2.approval_phrase_sha256 = LOWER(TO_HEX(SHA256(v_approval_phrase)))
      AND open_intelligence_execution_approvals_v2.origin_registry_sha256 = v_origin_registry_sha256
      AND open_intelligence_execution_approvals_v2.resource_manifest_sha256 = v_resource_manifest_sha256) = 1 AS 'execution_approval_schema_mismatch';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`
  SET state = 'ready', last_approval_id = v_approval_id, updated_at = v_approved_at
  WHERE lock_name = 'open_intelligence_execution_approval_v2'
    AND approval_contract_version = 'open_intelligence_execution_approval_v2'
    AND state = 'approving' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_approval_receipt_v3' AS contract_version,
         v_approval_id AS approval_id, v_approved_at AS approved_at, v_actor AS approved_by,
         v_expires_at AS expires_at, v_manifest_sha256 AS manifest_sha256, v_operation AS operation,
         v_origin_registry_sha256 AS origin_registry_sha256, v_resource_manifest_sha256 AS resource_manifest_sha256;
END;
