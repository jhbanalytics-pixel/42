CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_record_open_intelligence_execution_result_v1`(
  consumption_id STRING,
  result_reference STRING,
  canonical_result_json STRING,
  result_digest STRING,
  status STRING
)
BEGIN
  DECLARE v_consumption_id STRING DEFAULT consumption_id;
  DECLARE v_result_reference STRING DEFAULT result_reference;
  DECLARE v_canonical_result_json STRING DEFAULT canonical_result_json;
  DECLARE v_result_digest STRING DEFAULT result_digest;
  DECLARE v_status STRING DEFAULT status;
  DECLARE v_consumption STRUCT<approval_id STRING, manifest_sha256 STRING, operation STRING, execution_name STRING>;
  DECLARE v_expected_identity STRING;
  DECLARE v_completed_at TIMESTAMP;
  DECLARE v_result_id STRING;
  DECLARE v_lock_version INT64;
  DECLARE v_source_manifest STRING;
  DECLARE v_source_result JSON;
  DECLARE v_source_cutoff STRING;
  DECLARE v_source_initial_manifest STRING;
  DECLARE v_source_previous JSON;
  DECLARE v_source_previous_status STRING;
  DECLARE v_source_previous_manifest STRING;
  DECLARE v_source_continuation_allowed BOOL DEFAULT FALSE;
  DECLARE v_source_metadata_continuation_allowed BOOL DEFAULT FALSE;
  DECLARE v_source_replacement_allowed BOOL DEFAULT FALSE;
  BEGIN TRANSACTION;
  SET v_completed_at = CURRENT_TIMESTAMP();
  SET v_lock_version = (
    SELECT lock_version FROM `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
    WHERE lock_name = 'open_intelligence_execution_approval_v1'
      AND approval_contract_version = 'open_intelligence_execution_approval_v1'
      AND state = 'ready'
  );
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET lock_version = lock_version + 1, state = 'recording_result', updated_at = v_completed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v1'
    AND approval_contract_version = 'open_intelligence_execution_approval_v1'
    AND state = 'ready' AND lock_version = v_lock_version;
  ASSERT @@row_count = 1 AS 'execution_approval_concurrent_conflict';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS open_intelligence_execution_consumptions_v1
    WHERE open_intelligence_execution_consumptions_v1.consumption_contract_version = 'open_intelligence_execution_consumption_v1'
      AND open_intelligence_execution_consumptions_v1.consumption_id = v_consumption_id) = 1 AS 'execution_approval_unavailable';
  SET v_consumption = (
    SELECT AS STRUCT approval_id, manifest_sha256, operation, execution_name
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` AS open_intelligence_execution_consumptions_v1
    WHERE open_intelligence_execution_consumptions_v1.consumption_contract_version = 'open_intelligence_execution_consumption_v1'
      AND open_intelligence_execution_consumptions_v1.consumption_id = v_consumption_id
  );
  SET v_expected_identity = CASE v_consumption.operation
    WHEN 'source_snapshot_capture' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'migration_apply' THEN 'trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'collection_exposure_issue' THEN 'trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_apply' THEN 'trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_proof_issue' THEN 'trends-engine-oi-r3-proof@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'r3_release' THEN 'trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'brain_read' THEN 'trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com'
    WHEN 'wave1_pilot' THEN 'trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com'
  END;
  ASSERT SESSION_USER() = v_expected_identity AS 'execution_approval_identity_invalid';
  ASSERT NULLIF(TRIM(v_result_reference), '') IS NOT NULL AS 'execution_approval_manifest_invalid';
  ASSERT JSON_TYPE(SAFE.PARSE_JSON(v_canonical_result_json, wide_number_mode => 'exact')) = 'object'
    AS 'execution_approval_manifest_invalid';
  ASSERT v_status IN ('succeeded', 'failed') AS 'execution_approval_manifest_invalid';
  ASSERT v_result_digest = LOWER(TO_HEX(SHA256(v_canonical_result_json))) AS 'execution_approval_manifest_mismatch';
  IF v_consumption.operation = 'source_snapshot_capture' THEN
    SET v_source_manifest = (
      SELECT a.canonical_manifest_json FROM `{project}.{dataset}.open_intelligence_execution_approvals_v1` a
      WHERE a.approval_id = v_consumption.approval_id AND a.manifest_sha256 = v_consumption.manifest_sha256 AND a.operation = 'source_snapshot_capture'
    );
    SET v_source_cutoff = JSON_VALUE(v_source_manifest, '$.arguments[2]');
    SET v_source_result = SAFE.PARSE_JSON(v_canonical_result_json, wide_number_mode => 'exact');
    ASSERT v_source_cutoff IN ('2026-09-07', '2026-09-08')
      AND LOWER(TO_HEX(SHA256(v_source_manifest))) = v_consumption.manifest_sha256
      AND JSON_VALUE(v_source_manifest, '$.contract_sha256') = '5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f'
      AND ARRAY_TO_STRING(JSON_KEYS(v_source_result, 1), ',') = 'artifact_attempt,capture_receipt_digest,captured_at,client_scope_id,contract_version,creation_records,cutoff_date,limitations,market_scope,missing_checks,query_count,snapshot_digest,snapshot_plan_digest,source_as_of,stored_artifact,total_bytes_billed'
      AND JSON_VALUE(v_source_result, '$.contract_version') = 'open_intelligence_protected_source_snapshot_v1'
      AND JSON_VALUE(v_source_result, '$.cutoff_date') = v_source_cutoff
      AND NULLIF(TRIM(JSON_VALUE(v_source_result, '$.client_scope_id')), '') IS NOT NULL
      AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_source_result, '$.market_scope')) BETWEEN 1 AND 3
      AND TO_JSON_STRING(JSON_VALUE_ARRAY(v_source_result, '$.market_scope')) = TO_JSON_STRING(ARRAY(
        SELECT DISTINCT market FROM UNNEST(JSON_VALUE_ARRAY(v_source_result, '$.market_scope')) market WHERE market IN ('ke', 'ng', 'za') ORDER BY market))
      AND TIMESTAMP(JSON_VALUE(v_source_result, '$.source_as_of')) = TIMESTAMP(DATE_ADD(DATE(v_source_cutoff), INTERVAL 1 DAY))
      AND REGEXP_CONTAINS(JSON_VALUE(v_source_result, '$.snapshot_plan_digest'), r'^[0-9a-f]{64}$')
      AND JSON_TYPE(JSON_QUERY(v_source_result, '$.query_count')) = 'number'
      AND REGEXP_CONTAINS(JSON_VALUE(v_source_result, '$.query_count'), r'^[0-5]$')
      AND (JSON_TYPE(JSON_QUERY(v_source_result, '$.total_bytes_billed')) = 'null' OR (
        JSON_TYPE(JSON_QUERY(v_source_result, '$.total_bytes_billed')) = 'number'
        AND SAFE_CAST(JSON_VALUE(v_source_result, '$.total_bytes_billed') AS INT64) BETWEEN 0 AND 1000000000))
      AND JSON_TYPE(JSON_QUERY(v_source_result, '$.creation_records')) = 'array'
      AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) <= 5
      AND JSON_TYPE(JSON_QUERY(v_source_result, '$.limitations')) = 'array'
      AND JSON_TYPE(JSON_QUERY(v_source_result, '$.missing_checks')) = 'array'
      AS 'source_snapshot_result_invalid';
    ASSERT NOT EXISTS (
      SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) item
      WHERE NOT COALESCE(
        ARRAY_TO_STRING(JSON_KEYS(item, 1), ',') = 'destination,job_id,lane,native_job_digest,state'
        AND JSON_VALUE(item, '$.lane') IN ('event_ledger','seed_graph','seed_candidates','enriched_content','raw_content')
        AND JSON_VALUE(item, '$.destination') = CONCAT('ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_', REPLACE(v_source_cutoff, '-', ''), '_', JSON_VALUE(item, '$.lane'))
        AND REGEXP_CONTAINS(JSON_VALUE(item, '$.job_id'), r'^oi_v3_snapshot_[0-9a-f]{64}_[a-z_]+$')
        AND JSON_VALUE(item, '$.state') IN ('succeeded','failed','unresolved')
        AND (JSON_TYPE(JSON_QUERY(item, '$.native_job_digest')) = 'null' OR REGEXP_CONTAINS(JSON_VALUE(item, '$.native_job_digest'), r'^[0-9a-f]{64}$')),
        FALSE)
    ) AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) = (
      SELECT COUNT(DISTINCT JSON_VALUE(item, '$.lane')) FROM UNNEST(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) item
    ) AS 'source_snapshot_result_invalid';
    ASSERT TO_JSON_STRING(ARRAY(
      SELECT JSON_VALUE(item, '$.lane') FROM UNNEST(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) item WITH OFFSET position ORDER BY position
    )) = TO_JSON_STRING(ARRAY(
      SELECT lane FROM UNNEST(['event_ledger','seed_graph','seed_candidates','enriched_content','raw_content']) lane WITH OFFSET position
      WHERE EXISTS (SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) item WHERE JSON_VALUE(item, '$.lane') = lane)
      ORDER BY position
    )) AS 'source_snapshot_result_invalid';
    SET v_source_initial_manifest = (
      SELECT c.manifest_sha256 FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c
      JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v1` a ON a.approval_id=c.approval_id AND a.manifest_sha256=c.manifest_sha256
      WHERE c.operation='source_snapshot_capture' AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[2]')=v_source_cutoff
        AND JSON_VALUE(a.canonical_manifest_json, '$.arguments[4]')='initial'
    );
    ASSERT v_source_initial_manifest IS NOT NULL AS 'source_snapshot_result_invalid';
    IF JSON_VALUE(v_source_manifest, '$.arguments[4]') = 'recover' THEN
      SET v_source_metadata_continuation_allowed = COALESCE(
        v_source_initial_manifest = '8064a2d1a641ba1e61edca95b8db2a98846d12936baf51efdfdedcdeeda48de9'
        AND (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_source_manifest, '$.input_artifacts')) artifact
          WHERE JSON_VALUE(artifact, '$.name') = 'recovery_context'
            AND JSON_VALUE(artifact, '$.sha256') = 'cc8f820cdcc8603e5c1da881c16648a86a5c89132b67f78b84614587672e9e3a') = 1, FALSE);
      SET v_source_continuation_allowed = v_source_metadata_continuation_allowed OR COALESCE(
        v_source_initial_manifest = '8064a2d1a641ba1e61edca95b8db2a98846d12936baf51efdfdedcdeeda48de9'
        AND (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_source_manifest, '$.input_artifacts')) artifact
          WHERE JSON_VALUE(artifact, '$.name') = 'recovery_context'
            AND JSON_VALUE(artifact, '$.sha256') = '656e88826f4a5c9178d754b5a3ce213c9e7d52127be3ef6427a56feed4bd9b7c') = 1, FALSE);
      SET v_source_previous_manifest = IF(v_source_metadata_continuation_allowed,
        '1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d16974', IF(v_source_continuation_allowed,
        '109ea182ba6f1b7042c76ecbea7d62cab753b3867cca57c909b422878b0a5433', v_source_initial_manifest));
      SET (v_source_previous, v_source_previous_status) = (
        SELECT AS STRUCT SAFE.PARSE_JSON(r.canonical_result_json, wide_number_mode => 'exact'), r.status
        FROM `{project}.{dataset}.open_intelligence_execution_results_v1` r
        JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v1` c ON c.consumption_id=r.consumption_id AND c.manifest_sha256=r.manifest_sha256 AND c.approval_id=r.approval_id AND c.execution_name=r.execution_name
        WHERE r.operation='source_snapshot_capture' AND c.operation=r.operation AND r.manifest_sha256=v_source_previous_manifest
          AND r.result_digest=LOWER(TO_HEX(SHA256(r.canonical_result_json)))
          AND (NOT v_source_metadata_continuation_allowed OR (
            c.consumption_id = 'exc_2aa600d87b7f29678cac363231de9d50e8feb2865ab033d9b593d8b670a66292'
            AND r.result_id = 'exr_1e53fdb9a6876cbc557efd6db93ff221883eb6e8b844276cc67960c3630a077d'
            AND r.result_digest = '72fe27188cd1ac022c4acc22ac096f91a56dc0ad601cd02020671980a144712c'))
      );
      ASSERT v_source_previous IS NOT NULL AS 'source_snapshot_recovery_result_mismatch';
      IF v_source_continuation_allowed THEN
        ASSERT v_source_previous_status = 'failed'
          AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.query_count')) = 'number'
          AND JSON_VALUE(v_source_previous, '$.query_count') = '0'
          AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.captured_at')) = 'null'
          AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.snapshot_digest')) = 'null'
          AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.capture_receipt_digest')) = 'null'
          AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.artifact_attempt')) = 'null'
          AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.stored_artifact')) = 'null' AS 'source_snapshot_continuation_invalid';
      END IF;
      ASSERT JSON_VALUE(v_source_previous, '$.cutoff_date')=v_source_cutoff
        AND JSON_VALUE(v_source_previous, '$.client_scope_id')=JSON_VALUE(v_source_result, '$.client_scope_id')
        AND TO_JSON_STRING(JSON_QUERY(v_source_previous, '$.market_scope'))=TO_JSON_STRING(JSON_QUERY(v_source_result, '$.market_scope'))
        AND JSON_VALUE(v_source_previous, '$.snapshot_plan_digest')=JSON_VALUE(v_source_result, '$.snapshot_plan_digest')
        AS 'source_snapshot_recovery_result_mismatch';
      IF JSON_VALUE(v_source_previous, '$.query_count') != '0'
        OR JSON_TYPE(JSON_QUERY(v_source_previous, '$.artifact_attempt')) != 'null'
        OR JSON_TYPE(JSON_QUERY(v_source_previous, '$.captured_at')) != 'null' THEN
        ASSERT TO_JSON_STRING(JSON_QUERY(v_source_previous, '$.query_count')) IS NOT DISTINCT FROM TO_JSON_STRING(JSON_QUERY(v_source_result, '$.query_count'))
          AND TO_JSON_STRING(JSON_QUERY(v_source_previous, '$.total_bytes_billed')) IS NOT DISTINCT FROM TO_JSON_STRING(JSON_QUERY(v_source_result, '$.total_bytes_billed'))
          AND TO_JSON_STRING(JSON_QUERY(v_source_previous, '$.captured_at')) IS NOT DISTINCT FROM TO_JSON_STRING(JSON_QUERY(v_source_result, '$.captured_at'))
          AND TO_JSON_STRING(JSON_QUERY(v_source_previous, '$.snapshot_digest')) IS NOT DISTINCT FROM TO_JSON_STRING(JSON_QUERY(v_source_result, '$.snapshot_digest'))
          AND TO_JSON_STRING(JSON_QUERY(v_source_previous, '$.capture_receipt_digest')) IS NOT DISTINCT FROM TO_JSON_STRING(JSON_QUERY(v_source_result, '$.capture_receipt_digest'))
          AND TO_JSON_STRING(JSON_QUERY(v_source_previous, '$.artifact_attempt')) IS NOT DISTINCT FROM TO_JSON_STRING(JSON_QUERY(v_source_result, '$.artifact_attempt')) AS 'source_snapshot_recovery_result_mismatch';
      END IF;
      IF JSON_TYPE(JSON_QUERY(v_source_previous, '$.stored_artifact')) != 'null' THEN
        ASSERT TO_JSON_STRING(JSON_QUERY(v_source_previous, '$.stored_artifact'))=TO_JSON_STRING(JSON_QUERY(v_source_result, '$.stored_artifact'))
          AS 'source_snapshot_recovery_result_mismatch';
      END IF;
      SET v_source_replacement_allowed = COALESCE(
        v_source_initial_manifest = '8064a2d1a641ba1e61edca95b8db2a98846d12936baf51efdfdedcdeeda48de9'
        AND v_source_previous_status = 'failed'
        AND (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_source_manifest, '$.input_artifacts')) artifact
          WHERE JSON_VALUE(artifact, '$.name') = 'recovery_context'
            AND JSON_VALUE(artifact, '$.sha256') = 'ade29835ba6ca2192ad2ae7ae3d87d97567ce7ea4796390ce7f5c934fd1a1d95') = 1
        AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.query_count')) = 'number'
        AND JSON_VALUE(v_source_previous, '$.query_count') = '0'
        AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.captured_at')) = 'null'
        AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.snapshot_digest')) = 'null'
        AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.capture_receipt_digest')) = 'null'
        AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.artifact_attempt')) = 'null'
        AND JSON_TYPE(JSON_QUERY(v_source_previous, '$.stored_artifact')) = 'null', FALSE);
      ASSERT NOT EXISTS (
        SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_source_previous, '$.creation_records')) prior WITH OFFSET prior_position
        WHERE (SELECT COUNT(*) FROM UNNEST(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) current_item
          WHERE JSON_VALUE(current_item, '$.lane')=JSON_VALUE(prior, '$.lane')
            AND JSON_VALUE(current_item, '$.destination')=JSON_VALUE(prior, '$.destination')
            AND (
              (JSON_VALUE(current_item, '$.job_id')=JSON_VALUE(prior, '$.job_id')
                AND (NOT (v_source_replacement_allowed OR v_source_continuation_allowed) OR JSON_VALUE(prior, '$.state') != 'succeeded'
                  OR (JSON_VALUE(current_item, '$.state') = 'succeeded'
                    AND JSON_VALUE(current_item, '$.native_job_digest') IS NOT DISTINCT FROM JSON_VALUE(prior, '$.native_job_digest'))))
              OR (
                v_source_replacement_allowed
                AND prior_position = ARRAY_LENGTH(JSON_QUERY_ARRAY(v_source_previous, '$.creation_records')) - 1
                AND JSON_VALUE(prior, '$.state') IN ('failed', 'unresolved')
                AND JSON_VALUE(current_item, '$.job_id') = CONCAT('oi_v3_snapshot_', v_consumption.manifest_sha256, '_', JSON_VALUE(prior, '$.lane'))
                AND NOT EXISTS (
                  SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_source_previous, '$.creation_records')) earlier WITH OFFSET earlier_position
                  WHERE earlier_position < prior_position AND JSON_VALUE(earlier, '$.state') IS DISTINCT FROM 'succeeded'
                )
              )
            )) != 1
      ) AS 'source_snapshot_creation_replaced';
    END IF;
    ASSERT NOT EXISTS (
      SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) item
      WHERE NOT EXISTS (SELECT 1 FROM UNNEST(IFNULL(JSON_QUERY_ARRAY(v_source_previous, '$.creation_records'), ARRAY<JSON>[])) prior WHERE JSON_VALUE(prior, '$.lane')=JSON_VALUE(item, '$.lane'))
        AND (JSON_VALUE(item, '$.job_id') IS DISTINCT FROM CONCAT('oi_v3_snapshot_', v_consumption.manifest_sha256, '_', JSON_VALUE(item, '$.lane'))
          OR (v_source_metadata_continuation_allowed AND JSON_VALUE(item, '$.lane') IS DISTINCT FROM 'raw_content'))
    ) AS 'source_snapshot_creation_replaced';
    IF JSON_TYPE(JSON_QUERY(v_source_result, '$.artifact_attempt')) != 'null' THEN
      ASSERT ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_source_result, '$.artifact_attempt'),1), ',')='captured_at,sha256,size_bytes,uri'
        AND REGEXP_CONTAINS(JSON_VALUE(v_source_result, '$.artifact_attempt.sha256'), r'^[0-9a-f]{64}$')
        AND JSON_VALUE(v_source_result, '$.artifact_attempt.uri')=CONCAT('gs://ogilvy-trends-v2-oi-source-artifacts-staging/captures/',v_source_initial_manifest,'/',JSON_VALUE(v_source_result, '$.artifact_attempt.sha256'),'/capture.json')
        AND JSON_TYPE(JSON_QUERY(v_source_result, '$.artifact_attempt.size_bytes'))='number'
        AND SAFE_CAST(JSON_VALUE(v_source_result, '$.artifact_attempt.size_bytes') AS INT64) BETWEEN 1 AND 536870912
        AND TIMESTAMP(JSON_VALUE(v_source_result, '$.artifact_attempt.captured_at'))=TIMESTAMP(JSON_VALUE(v_source_result, '$.captured_at'))
        AS 'source_snapshot_result_invalid';
    END IF;
    IF JSON_TYPE(JSON_QUERY(v_source_result, '$.stored_artifact')) != 'null' THEN
      ASSERT ARRAY_TO_STRING(JSON_KEYS(JSON_QUERY(v_source_result, '$.stored_artifact'),1), ',')='created_at,generation,sha256,size_bytes,uri'
        AND JSON_TYPE(JSON_QUERY(v_source_result, '$.stored_artifact.generation'))='number'
        AND SAFE_CAST(JSON_VALUE(v_source_result, '$.stored_artifact.generation') AS INT64)>0
        AND JSON_TYPE(JSON_QUERY(v_source_result, '$.stored_artifact.size_bytes'))='number'
        AND JSON_VALUE(v_source_result, '$.stored_artifact.uri')=JSON_VALUE(v_source_result, '$.artifact_attempt.uri')
        AND JSON_VALUE(v_source_result, '$.stored_artifact.sha256')=JSON_VALUE(v_source_result, '$.artifact_attempt.sha256')
        AND JSON_VALUE(v_source_result, '$.stored_artifact.size_bytes')=JSON_VALUE(v_source_result, '$.artifact_attempt.size_bytes')
        AND TIMESTAMP(JSON_VALUE(v_source_result, '$.stored_artifact.created_at')) BETWEEN TIMESTAMP(JSON_VALUE(v_source_result, '$.captured_at')) AND v_completed_at
        AS 'source_snapshot_result_invalid';
    END IF;
    IF v_status = 'succeeded' THEN
      ASSERT ARRAY_LENGTH(JSON_QUERY_ARRAY(v_source_result, '$.creation_records'))=5
        AND NOT EXISTS (SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(v_source_result, '$.creation_records')) item WHERE JSON_VALUE(item, '$.state') IS DISTINCT FROM 'succeeded' OR JSON_TYPE(JSON_QUERY(item, '$.native_job_digest')) IS DISTINCT FROM 'string')
        AND ARRAY_LENGTH(JSON_QUERY_ARRAY(v_source_result, '$.missing_checks'))=0
        AND JSON_TYPE(JSON_QUERY(v_source_result, '$.stored_artifact'))='object'
        AND JSON_TYPE(JSON_QUERY(v_source_result, '$.total_bytes_billed'))='number'
        AND SAFE_CAST(JSON_VALUE(v_source_result, '$.query_count') AS INT64) BETWEEN 3 AND 5
        AND REGEXP_CONTAINS(JSON_VALUE(v_source_result, '$.snapshot_digest'),r'^[0-9a-f]{64}$')
        AND REGEXP_CONTAINS(JSON_VALUE(v_source_result, '$.capture_receipt_digest'),r'^[0-9a-f]{64}$')
        AND TIMESTAMP(JSON_VALUE(v_source_result, '$.captured_at')) BETWEEN TIMESTAMP(JSON_VALUE(v_source_result, '$.source_as_of')) AND v_completed_at
        AS 'source_snapshot_result_invalid';
    END IF;
  END IF;
  SET v_result_id = CONCAT('exr_', LOWER(TO_HEX(SHA256(FORMAT(
    '{"completed_at":"%s","consumption_id":"%s","result_digest":"%s","result_reference":"%s","status":"%s"}',
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_completed_at), v_consumption_id,
    v_result_digest, v_result_reference, v_status
  )))));
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS open_intelligence_execution_results_v1 WHERE open_intelligence_execution_results_v1.consumption_id = v_consumption_id OR open_intelligence_execution_results_v1.result_id = v_result_id OR open_intelligence_execution_results_v1.result_reference = v_result_reference) = 0 AS 'execution_approval_conflict';
  INSERT INTO `{project}.{dataset}.open_intelligence_execution_results_v1` VALUES (
    'open_intelligence_execution_result_v1', v_result_id, v_consumption_id,
    v_consumption.approval_id, v_consumption.manifest_sha256, v_consumption.operation,
    v_consumption.execution_name, v_result_reference, v_canonical_result_json, v_result_digest,
    v_status, v_completed_at
  );
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v1` AS open_intelligence_execution_results_v1
    WHERE open_intelligence_execution_results_v1.result_contract_version = 'open_intelligence_execution_result_v1'
      AND open_intelligence_execution_results_v1.result_id = v_result_id
      AND open_intelligence_execution_results_v1.consumption_id = v_consumption_id
      AND open_intelligence_execution_results_v1.approval_id = v_consumption.approval_id
      AND open_intelligence_execution_results_v1.manifest_sha256 = v_consumption.manifest_sha256
      AND open_intelligence_execution_results_v1.operation = v_consumption.operation
      AND open_intelligence_execution_results_v1.execution_name = v_consumption.execution_name
      AND open_intelligence_execution_results_v1.result_reference = v_result_reference
      AND open_intelligence_execution_results_v1.canonical_result_json = v_canonical_result_json
      AND open_intelligence_execution_results_v1.result_digest = v_result_digest
      AND open_intelligence_execution_results_v1.status = v_status
      AND open_intelligence_execution_results_v1.completed_at = v_completed_at) = 1 AS 'execution_approval_schema_mismatch';
  UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v1`
  SET state = 'ready', updated_at = v_completed_at
  WHERE lock_name = 'open_intelligence_execution_approval_v1'
    AND approval_contract_version = 'open_intelligence_execution_approval_v1'
    AND state = 'recording_result' AND lock_version = v_lock_version + 1;
  ASSERT @@row_count = 1 AS 'execution_approval_lock_invalid';
  COMMIT TRANSACTION;
  SELECT 'open_intelligence_execution_result_v1' AS result_contract_version,
         v_result_id AS result_id, v_consumption_id AS consumption_id,
         v_consumption.approval_id AS approval_id, v_consumption.manifest_sha256 AS manifest_sha256,
         v_completed_at AS completed_at;
END;
