CREATE OR REPLACE VIEW `{project}.{dataset}.v_open_intelligence_health_v2` AS
WITH health_rows AS (
  SELECT
    contract_version,
    run_id,
    client_scope_id,
    market_scope,
    brand_config_id,
    audience_lens_ids,
    theme_id,
    signal_date,
    market,
    'candidate' AS row_kind,
    evidence_state,
    created_at AS activity_at
  FROM `{project}.{dataset}.signal_candidates_v2`
  WHERE client_scope_id != 'qa_canary'
  UNION ALL
  SELECT
    contract_version,
    run_id,
    client_scope_id,
    market_scope,
    brand_config_id,
    audience_lens_ids,
    theme_id,
    signal_date,
    market,
    'evidence' AS row_kind,
    NULL AS evidence_state,
    created_at AS activity_at
  FROM `{project}.{dataset}.signal_evidence_v2`
  WHERE client_scope_id != 'qa_canary'
  UNION ALL
  SELECT
    contract_version,
    run_id,
    client_scope_id,
    market_scope,
    brand_config_id,
    audience_lens_ids,
    theme_id,
    signal_date,
    market,
    'analysis' AS row_kind,
    NULL AS evidence_state,
    analyzed_at AS activity_at
  FROM `{project}.{dataset}.signal_analysis_v2`
  WHERE client_scope_id != 'qa_canary'
  UNION ALL
  SELECT
    contract_version,
    run_id,
    client_scope_id,
    market_scope,
    brand_config_id,
    audience_lens_ids,
    theme_id,
    signal_date,
    market,
    'prediction' AS row_kind,
    NULL AS evidence_state,
    predicted_at AS activity_at
  FROM `{project}.{dataset}.signal_predictions_v2`
  WHERE client_scope_id != 'qa_canary'
)
SELECT
  contract_version,
  run_id,
  client_scope_id,
  ANY_VALUE(market_scope) AS market_scope,
  ANY_VALUE(brand_config_id) AS brand_config_id,
  ANY_VALUE(audience_lens_ids) AS audience_lens_ids,
  ANY_VALUE(theme_id) AS theme_id,
  signal_date,
  market,
  COUNTIF(row_kind = 'candidate') AS candidate_count,
  COUNTIF(row_kind = 'candidate' AND evidence_state = 'ready') AS ready_count,
  COUNTIF(row_kind = 'candidate' AND evidence_state = 'thin') AS thin_count,
  COUNTIF(row_kind = 'candidate' AND evidence_state = 'contradictory') AS contradictory_count,
  COUNTIF(row_kind = 'candidate' AND evidence_state = 'unchecked') AS unchecked_count,
  COUNTIF(row_kind = 'evidence') AS evidence_count,
  COUNTIF(row_kind = 'analysis') AS analysis_count,
  COUNTIF(row_kind = 'prediction') AS prediction_count,
  MAX(activity_at) AS latest_created_at
FROM health_rows
GROUP BY
  contract_version,
  run_id,
  client_scope_id,
  signal_date,
  market;
