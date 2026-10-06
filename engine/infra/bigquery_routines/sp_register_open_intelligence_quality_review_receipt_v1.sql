CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_register_open_intelligence_quality_review_receipt_v1`(
  p_canonical_review_receipt_json STRING,
  p_artifact_sha256 STRING
)
BEGIN
  DECLARE v_review_receipt_json JSON;
  DECLARE v_review_receipt_keys ARRAY<STRING>;
  DECLARE v_run_id STRING;
  DECLARE v_registered_by STRING;
  DECLARE v_registered_at TIMESTAMP;
  DECLARE v_reviewed_at TIMESTAMP;
  DECLARE v_reconstructed_review_receipt_json STRING;
  DECLARE v_review_receipt_preimage_json STRING;
  DECLARE v_review_receipt_digest STRING;
  DECLARE v_stored_review_receipt_digest STRING;
  DECLARE v_artifact_sha256 STRING;
  DECLARE v_source_window_digest STRING;
  DECLARE v_copy_run_id STRING;
  DECLARE v_completeness ARRAY<STRUCT<source_table STRING, manifest_rows INT64, matched_rows INT64>>;
  DECLARE v_stored_source_window_digest STRING;
  DECLARE v_candidate_projection_digest STRING;
  DECLARE v_stored_candidate_projection_digest STRING;
  DECLARE v_packet_digest STRING;
  DECLARE v_stored_packet_digest STRING;
  DECLARE v_existing_count INT64 DEFAULT 0;
  DECLARE v_exact_count INT64 DEFAULT 0;

  ASSERT SESSION_USER() = 'albert.meintjes@ogilvy.co.za' AS 'quality_review_receipt_identity_invalid';
  SET v_registered_by = CONCAT('usr_', LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:', SESSION_USER())))));
  ASSERT v_registered_by = 'usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab' AS 'quality_review_receipt_identity_invalid';
  ASSERT REGEXP_CONTAINS(p_artifact_sha256, r'^[0-9a-f]{64}$') AS 'quality_review_receipt_digest_invalid';

  BEGIN TRANSACTION;
  SET v_review_receipt_json = SAFE.PARSE_JSON(p_canonical_review_receipt_json, wide_number_mode => 'exact');
  ASSERT JSON_TYPE(v_review_receipt_json) = 'object' AS 'quality_review_receipt_invalid';
  SET v_review_receipt_keys = JSON_KEYS(v_review_receipt_json, 1, mode => 'strict');
  ASSERT ARRAY_TO_STRING(v_review_receipt_keys, ',') = ARRAY_TO_STRING([
    'candidate_projection_digest', 'decision', 'factual_conflict_evidence_ids',
    'foreign_market_evidence_ids', 'packet_digest', 'receipt_digest',
    'review_contract_version', 'reviewed_at', 'reviewed_by', 'reviewed_evidence_ids',
    'run_id', 'source_window_digest', 'uncertain_evidence_ids'
  ], ',') AS 'quality_review_receipt_fields_invalid';
  SET v_run_id = JSON_VALUE(v_review_receipt_json, '$.run_id');
  ASSERT REGEXP_CONTAINS(v_run_id, r'^[a-z0-9_][a-z0-9_-]{0,127}$') AS 'quality_review_receipt_invalid';
  ASSERT JSON_VALUE(v_review_receipt_json, '$.review_contract_version') = 'dynamic_quality_review_v1'
    AND JSON_VALUE(v_review_receipt_json, '$.reviewed_by') = 'Albert'
    AND JSON_VALUE(v_review_receipt_json, '$.decision') = 'approved'
    AS 'quality_review_receipt_invalid';
  ASSERT REGEXP_CONTAINS(JSON_VALUE(v_review_receipt_json, '$.reviewed_at'), r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]{6}Z$')
    AS 'quality_review_receipt_invalid';
  SET v_reviewed_at = SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', JSON_VALUE(v_review_receipt_json, '$.reviewed_at'));
  ASSERT v_reviewed_at IS NOT NULL AS 'quality_review_receipt_invalid';
  ASSERT ARRAY_LENGTH(JSON_VALUE_ARRAY(v_review_receipt_json, '$.foreign_market_evidence_ids')) = 0
    AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_review_receipt_json, '$.factual_conflict_evidence_ids')) = 0
    AND ARRAY_LENGTH(JSON_VALUE_ARRAY(v_review_receipt_json, '$.uncertain_evidence_ids')) = 0
    AS 'quality_review_receipt_review_failed';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_run_receipts_v1`
    WHERE run_id = v_run_id AND client_scope_id != 'qa_canary' AND status = 'completed'
      AND complete_partitions IS TRUE) = 1 AS 'quality_review_receipt_authority_unavailable';
  ASSERT (SELECT COUNT(DISTINCT m.copy_run_id)
    FROM `{project}.{dataset}.open_intelligence_source_copy_manifest_v1` m
    JOIN `{project}.{dataset}.open_intelligence_run_receipts_v1` r
      ON r.run_id = v_run_id
    WHERE m.window_start = r.observation_start
      AND m.window_end = r.observation_end
      AND m.target_dataset = '{dataset}'
      AND m.target_table IN ('enriched_content', 'event_ledger', 'seed_candidates', 'seed_graph')
  ) = 1 AS 'quality_review_receipt_source_window_unavailable';
  SET v_copy_run_id = (
    SELECT ANY_VALUE(m.copy_run_id)
    FROM `{project}.{dataset}.open_intelligence_source_copy_manifest_v1` m
    JOIN `{project}.{dataset}.open_intelligence_run_receipts_v1` r
      ON r.run_id = v_run_id
    WHERE m.window_start = r.observation_start
      AND m.window_end = r.observation_end
      AND m.target_dataset = '{dataset}'
      AND m.target_table IN ('enriched_content', 'event_ledger', 'seed_candidates', 'seed_graph')
  );
  SET v_completeness = ARRAY(
    SELECT AS STRUCT * FROM (
    SELECT 'enriched_content' AS source_table, COUNT(*) AS manifest_rows, COUNTIF(target_match.copy_row_id IS NOT NULL) AS matched_rows FROM `{project}.{dataset}.open_intelligence_source_copy_manifest_v1` manifest LEFT JOIN (SELECT LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST('enriched_content' AS STRING) AS source_table,CAST(target.`market` AS STRING) AS market, CAST(target.`id` AS STRING) AS id))))) AS copy_row_id, LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST(target.`id` AS STRING) AS id, CAST(target.`source` AS STRING) AS source, CAST(target.`platform` AS STRING) AS platform, CAST(target.`market` AS STRING) AS market, CAST(target.`content_type` AS STRING) AS content_type, CAST(target.`query_group` AS STRING) AS query_group, CAST(target.`query_term` AS STRING) AS query_term, CAST(target.`author_name` AS STRING) AS author_name, CAST(target.`author_handle` AS STRING) AS author_handle, CAST(target.`title` AS STRING) AS title, CAST(target.`text` AS STRING) AS text, CAST(target.`url` AS STRING) AS url, CAST(target.`published_at` AS TIMESTAMP) AS published_at, CAST(target.`collected_at` AS TIMESTAMP) AS collected_at, CAST(target.`hashtags` AS STRING) AS hashtags, CAST(target.`views` AS FLOAT64) AS views, CAST(target.`likes` AS FLOAT64) AS likes, CAST(target.`comments` AS FLOAT64) AS comments, CAST(target.`shares` AS FLOAT64) AS shares, CAST(target.`engagement_total` AS FLOAT64) AS engagement_total, CAST(target.`pipeline_run_id` AS STRING) AS pipeline_run_id, CAST(target.`v2tone` AS STRING) AS v2tone, CAST(target.`v2persons` AS STRING) AS v2persons, CAST(target.`v2orgs` AS STRING) AS v2orgs, CAST(target.`v2locations` AS STRING) AS v2locations, CAST(target.`v2gcam` AS STRING) AS v2gcam, CAST(target.`author_handle_norm` AS STRING) AS author_handle_norm, CAST(target.`regional_score` AS FLOAT64) AS regional_score, CAST(target.`slang_terms` AS STRING) AS slang_terms, CAST(target.`search_velocity_score` AS FLOAT64) AS search_velocity_score, CAST(target.`tone_avg` AS FLOAT64) AS tone_avg, CAST(target.`tone_polarity` AS FLOAT64) AS tone_polarity, CAST(target.`topic_groups` AS ARRAY<STRING>) AS topic_groups, CAST(target.`classification_layer` AS STRING) AS classification_layer, CAST(target.`sentiment_lexicon_score` AS FLOAT64) AS sentiment_lexicon_score))))) AS content_hash FROM `{project}.{dataset}.enriched_content` target) target_match ON target_match.copy_row_id = manifest.copy_row_id AND target_match.content_hash = manifest.source_content_sha256 WHERE manifest.copy_run_id = v_copy_run_id AND manifest.target_table = 'enriched_content'
        UNION ALL
        SELECT 'event_ledger' AS source_table, COUNT(*) AS manifest_rows, COUNTIF(target_match.copy_row_id IS NOT NULL) AS matched_rows FROM `{project}.{dataset}.open_intelligence_source_copy_manifest_v1` manifest LEFT JOIN (SELECT LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST('event_ledger' AS STRING) AS source_table,CAST(target.`trend_date` AS DATE) AS trend_date, CAST(target.`market` AS STRING) AS market, CAST(target.`entity_key` AS STRING) AS entity_key))))) AS copy_row_id, LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST(target.`ledger_id` AS STRING) AS ledger_id, CAST(target.`trend_date` AS DATE) AS trend_date, CAST(target.`market` AS STRING) AS market, CAST(target.`entity_key` AS STRING) AS entity_key, CAST(target.`entity_aliases` AS ARRAY<STRING>) AS entity_aliases, CAST(target.`event_kind` AS STRING) AS event_kind, CAST(target.`state_label` AS STRING) AS state_label, CAST(target.`as_of` AS TIMESTAMP) AS as_of, CAST(target.`corroborating_sources` AS ARRAY<STRING>) AS corroborating_sources, CAST(target.`source_count` AS INT64) AS source_count, CAST(target.`confidence` AS FLOAT64) AS confidence))))) AS content_hash FROM `{project}.{dataset}.event_ledger` target) target_match ON target_match.copy_row_id = manifest.copy_row_id AND target_match.content_hash = manifest.source_content_sha256 WHERE manifest.copy_run_id = v_copy_run_id AND manifest.target_table = 'event_ledger'
        UNION ALL
        SELECT 'seed_candidates' AS source_table, COUNT(*) AS manifest_rows, COUNTIF(target_match.copy_row_id IS NOT NULL) AS matched_rows FROM `{project}.{dataset}.open_intelligence_source_copy_manifest_v1` manifest LEFT JOIN (SELECT LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST('seed_candidates' AS STRING) AS source_table,CAST(target.`candidate_id` AS STRING) AS candidate_id))))) AS copy_row_id, LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST(target.`candidate_id` AS STRING) AS candidate_id, CAST(target.`proposed_date` AS DATE) AS proposed_date, CAST(target.`market` AS STRING) AS market, CAST(target.`candidate_type` AS STRING) AS candidate_type, CAST(target.`candidate_value` AS STRING) AS candidate_value, CAST(target.`source` AS STRING) AS source, CAST(target.`lane` AS STRING) AS lane, CAST(target.`score` AS FLOAT64) AS score, CAST(target.`safety_flags` AS ARRAY<STRING>) AS safety_flags, CAST(target.`evidence_topics` AS ARRAY<STRING>) AS evidence_topics, CAST(target.`sample_row_ids` AS ARRAY<STRING>) AS sample_row_ids, CAST(target.`status` AS STRING) AS status))))) AS content_hash FROM `{project}.{dataset}.seed_candidates` target) target_match ON target_match.copy_row_id = manifest.copy_row_id AND target_match.content_hash = manifest.source_content_sha256 WHERE manifest.copy_run_id = v_copy_run_id AND manifest.target_table = 'seed_candidates'
        UNION ALL
        SELECT 'seed_graph' AS source_table, COUNT(*) AS manifest_rows, COUNTIF(target_match.copy_row_id IS NOT NULL) AS matched_rows FROM `{project}.{dataset}.open_intelligence_source_copy_manifest_v1` manifest LEFT JOIN (SELECT LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST('seed_graph' AS STRING) AS source_table,CAST(target.`market` AS STRING) AS market, CAST(target.`term` AS STRING) AS term, CAST(target.`term_type` AS STRING) AS term_type, CAST(target.`platform` AS STRING) AS platform, CAST(target.`trend_date` AS DATE) AS trend_date))))) AS copy_row_id, LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST(target.`market` AS STRING) AS market, CAST(target.`term` AS STRING) AS term, CAST(target.`term_type` AS STRING) AS term_type, CAST(target.`platform` AS STRING) AS platform, CAST(target.`trend_date` AS DATE) AS trend_date, CAST(target.`event_date` AS DATE) AS event_date, CAST(target.`row_count` AS INT64) AS row_count, CAST(target.`topic_groups` AS ARRAY<STRING>) AS topic_groups, CAST(target.`near_topics` AS ARRAY<STRING>) AS near_topics, CAST(target.`co_occur_terms` AS ARRAY<STRING>) AS co_occur_terms, CAST(target.`sample_row_ids` AS ARRAY<STRING>) AS sample_row_ids))))) AS content_hash FROM `{project}.{dataset}.seed_graph` target) target_match ON target_match.copy_row_id = manifest.copy_row_id AND target_match.content_hash = manifest.source_content_sha256 WHERE manifest.copy_run_id = v_copy_run_id AND manifest.target_table = 'seed_graph'
    )
  );
  ASSERT ARRAY_LENGTH(v_completeness) = 4
    AND (SELECT SUM(manifest_rows) FROM UNNEST(v_completeness)) > 0
    AND NOT EXISTS (
      SELECT 1 FROM UNNEST(v_completeness) WHERE manifest_rows != matched_rows
    ) AS 'quality_review_receipt_source_window_unavailable';
  SET v_source_window_digest = LOWER(TO_HEX(SHA256(CONCAT(
    '{"completeness": [',
    ARRAY_TO_STRING(ARRAY(
      SELECT CONCAT(
        '["', source_table, '", ', CAST(manifest_rows AS STRING), ', ',
        CAST(matched_rows AS STRING), ']'
      )
      FROM UNNEST(v_completeness)
      ORDER BY source_table
    ), ', '),
    '], "copy_run_id": ', TO_JSON_STRING(v_copy_run_id), '}'
  ))));
  SET v_stored_source_window_digest = JSON_VALUE(v_review_receipt_json, '$.source_window_digest');
  SET v_stored_candidate_projection_digest = JSON_VALUE(v_review_receipt_json, '$.candidate_projection_digest');
  SET v_stored_packet_digest = JSON_VALUE(v_review_receipt_json, '$.packet_digest');
  SET v_stored_review_receipt_digest = JSON_VALUE(v_review_receipt_json, '$.receipt_digest');
  ASSERT REGEXP_CONTAINS(v_stored_source_window_digest, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_stored_candidate_projection_digest, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_stored_packet_digest, r'^[0-9a-f]{64}$')
    AND REGEXP_CONTAINS(v_stored_review_receipt_digest, r'^[0-9a-f]{64}$')
    AS 'quality_review_receipt_digest_invalid';

  SET v_candidate_projection_digest = (
    WITH projection_candidates AS (
      SELECT TO_JSON_STRING(STRUCT(
        STRUCT('string' AS type, c.client_scope_id AS value) AS client_scope_id,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(c.market_scope) item ORDER BY item) AS value) AS market_scope,
        STRUCT('string' AS type, c.brand_config_id AS value) AS brand_config_id,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(c.audience_lens_ids) item ORDER BY item) AS value) AS audience_lens_ids,
        STRUCT('string' AS type, c.theme_id AS value) AS theme_id,
        STRUCT('string' AS type, c.run_id AS value) AS run_id,
        STRUCT('string' AS type, c.contract_version AS value) AS contract_version,
        STRUCT('string' AS type, c.signal_id AS value) AS signal_id,
        STRUCT('date' AS type, FORMAT_DATE('%Y-%m-%d', c.signal_date) AS value) AS signal_date,
        STRUCT('string' AS type, c.market AS value) AS market,
        STRUCT('string' AS type, c.label AS value) AS label,
        STRUCT('string' AS type, c.label_member_identity AS value) AS label_member_identity,
        STRUCT('string' AS type, c.cluster_signature AS value) AS cluster_signature,
        STRUCT('string' AS type, c.cluster_build_version AS value) AS cluster_build_version,
        IF(c.model_version IS NULL, STRUCT('null' AS type, CAST(NULL AS STRING) AS value), STRUCT('string' AS type, c.model_version AS value)) AS model_version,
        STRUCT('string' AS type, c.discovery_mode AS value) AS discovery_mode,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(c.topic_tags) item ORDER BY item) AS value) AS topic_tags,
        STRUCT('float' AS type, c.novelty_score AS value) AS novelty_score,
        STRUCT('float' AS type, c.velocity_score AS value) AS velocity_score,
        STRUCT('float' AS type, c.breadth_score AS value) AS breadth_score,
        STRUCT('float' AS type, c.independence_score AS value) AS independence_score,
        IF(c.historical_similarity IS NULL, STRUCT('null' AS type, CAST(NULL AS FLOAT64) AS value), STRUCT('float' AS type, c.historical_similarity AS value)) AS historical_similarity,
        STRUCT('float' AS type, c.geo_confidence AS value) AS geo_confidence,
        STRUCT('string' AS type, c.evidence_state AS value) AS evidence_state,
        STRUCT('timestamp' AS type, CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', c.created_at, 'UTC'), 'Z') AS value) AS created_at
      )) AS row_json, c.client_scope_id, c.signal_date, c.market, c.signal_id, c.run_id
      FROM `{project}.{dataset}.signal_candidates_v2` c
      WHERE c.run_id = v_run_id AND c.client_scope_id != 'qa_canary'
    ), projection_evidence AS (
      SELECT TO_JSON_STRING(STRUCT(
        STRUCT('string' AS type, e.client_scope_id AS value) AS client_scope_id,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(e.market_scope) item ORDER BY item) AS value) AS market_scope,
        STRUCT('string' AS type, e.brand_config_id AS value) AS brand_config_id,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(e.audience_lens_ids) item ORDER BY item) AS value) AS audience_lens_ids,
        STRUCT('string' AS type, e.theme_id AS value) AS theme_id,
        STRUCT('string' AS type, e.run_id AS value) AS run_id,
        STRUCT('string' AS type, e.contract_version AS value) AS contract_version,
        STRUCT('date' AS type, FORMAT_DATE('%Y-%m-%d', e.signal_date) AS value) AS signal_date,
        STRUCT('string' AS type, e.market AS value) AS market,
        STRUCT('string' AS type, e.signal_id AS value) AS signal_id,
        STRUCT('string' AS type, e.evidence_id AS value) AS evidence_id,
        STRUCT('string' AS type, e.row_id AS value) AS row_id,
        STRUCT('string' AS type, e.source_family AS value) AS source_family,
        STRUCT('string' AS type, e.vendor_family AS value) AS vendor_family,
        STRUCT('string' AS type, e.channel_family AS value) AS channel_family,
        STRUCT('string' AS type, e.platform AS value) AS platform,
        IF(e.url IS NULL, STRUCT('null' AS type, CAST(NULL AS STRING) AS value), STRUCT('string' AS type, e.url AS value)) AS url,
        IF(e.published_at IS NULL, STRUCT('null' AS type, CAST(NULL AS STRING) AS value), STRUCT('timestamp' AS type, CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', e.published_at, 'UTC'), 'Z') AS value)) AS published_at,
        STRUCT('string' AS type, e.claim_role AS value) AS claim_role,
        STRUCT('string' AS type, e.direction AS value) AS direction,
        STRUCT('float' AS type, e.geo_confidence AS value) AS geo_confidence,
        IF(e.source_label IS NULL, STRUCT('null' AS type, CAST(NULL AS STRING) AS value), STRUCT('string' AS type, e.source_label AS value)) AS source_label,
        IF(e.author_label IS NULL, STRUCT('null' AS type, CAST(NULL AS STRING) AS value), STRUCT('string' AS type, e.author_label AS value)) AS author_label,
        IF(e.excerpt IS NULL, STRUCT('null' AS type, CAST(NULL AS STRING) AS value), STRUCT('string' AS type, e.excerpt AS value)) AS excerpt,
        IF(e.metric_label IS NULL, STRUCT('null' AS type, CAST(NULL AS STRING) AS value), STRUCT('string' AS type, e.metric_label AS value)) AS metric_label,
        STRUCT('string' AS type, e.availability AS value) AS availability,
        STRUCT('string' AS type, e.evidence_state AS value) AS evidence_state,
        STRUCT('timestamp' AS type, CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', e.created_at, 'UTC'), 'Z') AS value) AS created_at
      )) AS row_json, e.client_scope_id, e.signal_date, e.market, e.signal_id, e.evidence_id, e.run_id
      FROM `{project}.{dataset}.signal_evidence_v2` e
      WHERE e.run_id = v_run_id AND e.client_scope_id != 'qa_canary'
    ), projection_membership AS (
      SELECT TO_JSON_STRING(STRUCT(
        STRUCT('string' AS type, m.client_scope_id AS value) AS client_scope_id,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(m.market_scope) item ORDER BY item) AS value) AS market_scope,
        STRUCT('string' AS type, m.brand_config_id AS value) AS brand_config_id,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(m.audience_lens_ids) item ORDER BY item) AS value) AS audience_lens_ids,
        STRUCT('string' AS type, m.theme_id AS value) AS theme_id,
        STRUCT('string' AS type, m.run_id AS value) AS run_id,
        STRUCT('string' AS type, m.contract_version AS value) AS contract_version,
        STRUCT('date' AS type, FORMAT_DATE('%Y-%m-%d', m.signal_date) AS value) AS signal_date,
        STRUCT('string' AS type, m.market AS value) AS market,
        STRUCT('string' AS type, m.signal_id AS value) AS signal_id,
        STRUCT('string' AS type, m.member_id AS value) AS member_id,
        STRUCT('string' AS type, m.member_identity AS value) AS member_identity,
        STRUCT('string' AS type, m.candidate_type AS value) AS candidate_type,
        STRUCT('string' AS type, m.canonical_value AS value) AS canonical_value,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(m.source_families) item ORDER BY item) AS value) AS source_families,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(m.vendor_families) item ORDER BY item) AS value) AS vendor_families,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(m.channel_families) item ORDER BY item) AS value) AS channel_families,
        STRUCT('repeated' AS type, ARRAY(SELECT AS STRUCT 'string' AS type, item AS value FROM UNNEST(m.platforms) item ORDER BY item) AS value) AS platforms,
        STRUCT('string' AS type, m.row_id AS value) AS row_id,
        STRUCT('boolean' AS type, m.qualifies_evidence AS value) AS qualifies_evidence,
        STRUCT('timestamp' AS type, CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', m.created_at, 'UTC'), 'Z') AS value) AS created_at
      )) AS row_json, m.client_scope_id, m.signal_date, m.market, m.signal_id, m.member_id, m.run_id
      FROM `{project}.{dataset}.signal_membership_v2` m
      WHERE m.run_id = v_run_id AND m.client_scope_id != 'qa_canary'
    )
    SELECT LOWER(TO_HEX(SHA256(CONCAT(
      '{"candidates":', IFNULL((SELECT TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, run_id, row_json)) FROM projection_candidates), '[]'),
      ',"evidence":', IFNULL((SELECT TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, evidence_id, run_id, row_json)) FROM projection_evidence), '[]'),
      ',"membership":', IFNULL((SELECT TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, member_id, run_id, row_json)) FROM projection_membership), '[]'),
      ',"projection_contract_version":"dynamic_quality_projection_v2","run_id":', TO_JSON_STRING(v_run_id), '}'
    ))))
  );
  SET v_packet_digest = LOWER(TO_HEX(SHA256(CONCAT(
    '{"review_contract_version":"dynamic_quality_review_v1","review_items":',
    IFNULL((SELECT CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT TO_JSON_STRING(STRUCT(
      e.author_label AS author_label, e.evidence_id AS evidence_id, e.excerpt AS excerpt,
      e.market AS market, e.platform AS platform,
      IF(e.published_at IS NULL, NULL, CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', e.published_at, 'UTC'), 'Z')) AS published_at,
      e.row_id AS row_id, e.run_id AS run_id, e.signal_id AS signal_id,
      e.source_family AS source_family, e.source_label AS source_label, e.url AS url
    )) FROM `{project}.{dataset}.signal_evidence_v2` e
      WHERE e.run_id = v_run_id AND e.client_scope_id != 'qa_canary'
      ORDER BY e.market, e.signal_id, e.evidence_id), ','), ']')), '[]'),
    ',"run_id":', TO_JSON_STRING(v_run_id), '}'
  ))));
  ASSERT ARRAY_TO_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.reviewed_evidence_ids'), ',') = IFNULL((
    SELECT STRING_AGG(evidence_id, ',' ORDER BY market, signal_id, evidence_id)
    FROM `{project}.{dataset}.signal_evidence_v2`
    WHERE run_id = v_run_id AND client_scope_id != 'qa_canary'
  ), '') AS 'quality_review_receipt_review_incomplete';

  SET v_review_receipt_preimage_json = CONCAT(
    '{"candidate_projection_digest":', TO_JSON_STRING(v_stored_candidate_projection_digest),
    ',"decision":', TO_JSON_STRING(JSON_VALUE(v_review_receipt_json, '$.decision')),
    ',"factual_conflict_evidence_ids":', TO_JSON_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.factual_conflict_evidence_ids')),
    ',"foreign_market_evidence_ids":', TO_JSON_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.foreign_market_evidence_ids')),
    ',"packet_digest":', TO_JSON_STRING(v_stored_packet_digest),
    ',"review_contract_version":', TO_JSON_STRING(JSON_VALUE(v_review_receipt_json, '$.review_contract_version')),
    ',"reviewed_at":', TO_JSON_STRING(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_reviewed_at, 'UTC')),
    ',"reviewed_by":', TO_JSON_STRING(JSON_VALUE(v_review_receipt_json, '$.reviewed_by')),
    ',"reviewed_evidence_ids":', TO_JSON_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.reviewed_evidence_ids')),
    ',"run_id":', TO_JSON_STRING(v_run_id),
    ',"source_window_digest":', TO_JSON_STRING(v_stored_source_window_digest),
    ',"uncertain_evidence_ids":', TO_JSON_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.uncertain_evidence_ids')), '}'
  );
  SET v_review_receipt_digest = LOWER(TO_HEX(SHA256(v_review_receipt_preimage_json)));
  SET v_reconstructed_review_receipt_json = CONCAT(
    '{"candidate_projection_digest":', TO_JSON_STRING(v_stored_candidate_projection_digest),
    ',"decision":', TO_JSON_STRING(JSON_VALUE(v_review_receipt_json, '$.decision')),
    ',"factual_conflict_evidence_ids":', TO_JSON_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.factual_conflict_evidence_ids')),
    ',"foreign_market_evidence_ids":', TO_JSON_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.foreign_market_evidence_ids')),
    ',"packet_digest":', TO_JSON_STRING(v_stored_packet_digest),
    ',"receipt_digest":', TO_JSON_STRING(v_stored_review_receipt_digest),
    ',"review_contract_version":', TO_JSON_STRING(JSON_VALUE(v_review_receipt_json, '$.review_contract_version')),
    ',"reviewed_at":', TO_JSON_STRING(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', v_reviewed_at, 'UTC')),
    ',"reviewed_by":', TO_JSON_STRING(JSON_VALUE(v_review_receipt_json, '$.reviewed_by')),
    ',"reviewed_evidence_ids":', TO_JSON_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.reviewed_evidence_ids')),
    ',"run_id":', TO_JSON_STRING(v_run_id),
    ',"source_window_digest":', TO_JSON_STRING(v_stored_source_window_digest),
    ',"uncertain_evidence_ids":', TO_JSON_STRING(JSON_VALUE_ARRAY(v_review_receipt_json, '$.uncertain_evidence_ids')), '}'
  );
  SET v_artifact_sha256 = LOWER(TO_HEX(SHA256(p_canonical_review_receipt_json)));
  ASSERT v_reconstructed_review_receipt_json = p_canonical_review_receipt_json AS 'quality_review_receipt_noncanonical';
  ASSERT v_source_window_digest = v_stored_source_window_digest
    AND v_candidate_projection_digest = v_stored_candidate_projection_digest
    AND v_packet_digest = v_stored_packet_digest
    AND v_review_receipt_digest = v_stored_review_receipt_digest
    AND v_artifact_sha256 = p_artifact_sha256
    AS 'quality_review_receipt_stale';
  SET v_existing_count = (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_quality_review_receipts_v1`
    WHERE run_id = v_run_id OR review_receipt_digest = v_review_receipt_digest
      OR artifact_sha256 = v_artifact_sha256
  );
  SET v_exact_count = (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_quality_review_receipts_v1`
    WHERE (run_id = v_run_id OR review_receipt_digest = v_review_receipt_digest
      OR artifact_sha256 = v_artifact_sha256)
      AND review_store_contract_version = 'open_intelligence_quality_review_store_v1'
      AND run_id = v_run_id AND canonical_review_receipt_json = p_canonical_review_receipt_json
      AND review_receipt_digest = v_review_receipt_digest AND artifact_sha256 = v_artifact_sha256
      AND source_window_digest = v_source_window_digest
      AND candidate_projection_digest = v_candidate_projection_digest
      AND packet_digest = v_packet_digest AND registered_by = v_registered_by
  );
  ASSERT v_existing_count = v_exact_count AS 'quality_review_receipt_conflict';
  ASSERT v_exact_count <= 1 AS 'quality_review_receipt_conflict';
  IF v_exact_count = 0 THEN
    SET v_registered_at = CURRENT_TIMESTAMP();
    INSERT INTO `{project}.{dataset}.open_intelligence_quality_review_receipts_v1` (
      review_store_contract_version, run_id, canonical_review_receipt_json,
      review_receipt_digest, artifact_sha256, source_window_digest,
      candidate_projection_digest, packet_digest, registered_by, registered_at
    ) VALUES (
      'open_intelligence_quality_review_store_v1', v_run_id, p_canonical_review_receipt_json,
      v_review_receipt_digest, v_artifact_sha256, v_source_window_digest,
      v_candidate_projection_digest, v_packet_digest, v_registered_by, v_registered_at
    );
    ASSERT @@row_count = 1 AS 'quality_review_receipt_conflict';
  ELSE
    SET v_registered_at = (
      SELECT registered_at
      FROM `{project}.{dataset}.open_intelligence_quality_review_receipts_v1`
      WHERE run_id = v_run_id AND canonical_review_receipt_json = p_canonical_review_receipt_json
        AND review_receipt_digest = v_review_receipt_digest AND artifact_sha256 = v_artifact_sha256
    );
  END IF;
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_quality_review_receipts_v1`
    WHERE review_store_contract_version = 'open_intelligence_quality_review_store_v1'
      AND run_id = v_run_id AND canonical_review_receipt_json = p_canonical_review_receipt_json
      AND review_receipt_digest = v_review_receipt_digest AND artifact_sha256 = v_artifact_sha256
      AND source_window_digest = v_source_window_digest
      AND candidate_projection_digest = v_candidate_projection_digest
      AND packet_digest = v_packet_digest AND registered_by = v_registered_by
      AND registered_at = v_registered_at) = 1 AS 'quality_review_receipt_readback_invalid';
  COMMIT TRANSACTION;
  SELECT review_store_contract_version, run_id, canonical_review_receipt_json,
    review_receipt_digest, artifact_sha256, source_window_digest,
    candidate_projection_digest, packet_digest, registered_by, registered_at
  FROM `{project}.{dataset}.open_intelligence_quality_review_receipts_v1`
  WHERE run_id = v_run_id;
END;
