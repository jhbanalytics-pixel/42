CREATE OR REPLACE VIEW `{project}.{dataset}.v_signal_board_v2` AS
WITH ranked_candidates AS (
  SELECT
    contract_version,
    run_id,
    client_scope_id,
    market_scope,
    brand_config_id,
    audience_lens_ids,
    theme_id,
    signal_id,
    signal_date,
    market,
    label,
    cluster_build_version,
    discovery_mode,
    topic_tags,
    novelty_score,
    velocity_score,
    breadth_score,
    independence_score,
    historical_similarity,
    geo_confidence,
    evidence_state,
    created_at,
    ROW_NUMBER() OVER (
      PARTITION BY client_scope_id, market, signal_id
      ORDER BY signal_date DESC, created_at DESC, run_id DESC
    ) AS candidate_rank
  FROM `{project}.{dataset}.signal_candidates_v2`
  WHERE client_scope_id != 'qa_canary'
),
selected_candidates AS (
  SELECT
    contract_version,
    run_id,
    client_scope_id,
    market_scope,
    brand_config_id,
    audience_lens_ids,
    theme_id,
    signal_id,
    signal_date,
    market,
    label,
    cluster_build_version,
    discovery_mode,
    topic_tags,
    novelty_score,
    velocity_score,
    breadth_score,
    independence_score,
    historical_similarity,
    geo_confidence,
    evidence_state,
    created_at
  FROM ranked_candidates
  WHERE candidate_rank = 1
),
analysis_matches AS (
  SELECT
    client_scope_id,
    signal_date,
    market,
    signal_id,
    run_id,
    COUNT(*) AS analysis_match_count,
    ANY_VALUE(summary) AS summary,
    ANY_VALUE(why_now) AS why_now,
    ANY_VALUE(possible_response) AS possible_response,
    ANY_VALUE(limitations) AS limitations,
    ANY_VALUE(contradictions) AS contradictions,
    ANY_VALUE(evidence_ids) AS evidence_ids,
    ANY_VALUE(human_review_required) AS human_review_required,
    ANY_VALUE(analyzed_at) AS analyzed_at
  FROM `{project}.{dataset}.signal_analysis_v2`
  WHERE client_scope_id != 'qa_canary'
  GROUP BY
    client_scope_id,
    signal_date,
    market,
    signal_id,
    run_id
)
SELECT
  c.contract_version,
  c.run_id,
  c.client_scope_id,
  c.market_scope,
  c.brand_config_id,
  c.audience_lens_ids,
  c.theme_id,
  c.signal_id,
  c.signal_date,
  c.market,
  c.label,
  c.cluster_build_version,
  c.discovery_mode,
  c.topic_tags,
  c.novelty_score,
  c.velocity_score,
  c.breadth_score,
  c.independence_score,
  c.historical_similarity,
  c.geo_confidence,
  c.evidence_state,
  a.summary,
  a.why_now,
  a.possible_response,
  a.limitations,
  a.contradictions,
  a.evidence_ids,
  a.human_review_required,
  COALESCE(a.analyzed_at, c.created_at) AS created_at
FROM selected_candidates c
LEFT JOIN analysis_matches a
  ON a.client_scope_id = c.client_scope_id
  AND a.signal_date = c.signal_date
  AND a.market = c.market
  AND a.signal_id = c.signal_id
  AND a.run_id = c.run_id
WHERE IF(
  COALESCE(a.analysis_match_count, 0) <= 1,
  TRUE,
  ERROR('Duplicate same-run signal analysis rows')
);
