CREATE OR REPLACE VIEW `{project}.{dataset}.v_desk_dynamic_signals_v1` AS
WITH run_receipts AS (
  SELECT
    run_id,
    client_scope_id,
    market_scope,
    signal_date,
    observation_start,
    observation_end,
    observation_method,
    completed_at,
    run_contract_version,
    status,
    complete_partitions,
    display_release_state
  FROM `{project}.{dataset}.open_intelligence_run_receipts_v1`
  WHERE client_scope_id != 'qa_canary'
),
predictions AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    prediction_id,
    predicted_at,
    display_eligible
  FROM `{project}.{dataset}.signal_predictions_v2`
  WHERE client_scope_id != 'qa_canary'
),
candidates AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    label,
    evidence_state
  FROM `{project}.{dataset}.signal_candidates_v2`
  WHERE client_scope_id != 'qa_canary'
),
memberships AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    canonical_value,
    candidate_type
  FROM `{project}.{dataset}.signal_membership_v2`
  WHERE client_scope_id != 'qa_canary'
),
evidence AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    evidence_id,
    url,
    platform,
    source_family,
    author_label,
    excerpt,
    metric_label,
    published_at,
    availability
  FROM `{project}.{dataset}.signal_evidence_v2`
  WHERE client_scope_id != 'qa_canary'
),
analysis AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    why_now,
    possible_response
  FROM `{project}.{dataset}.signal_analysis_v2`
  WHERE client_scope_id != 'qa_canary'
),
released_runs AS (
  SELECT
    run_id,
    client_scope_id,
    market_scope,
    signal_date,
    observation_start,
    observation_end,
    observation_method,
    completed_at
  FROM run_receipts
  WHERE run_contract_version = 'open_intelligence_run_receipt_v1'
    AND status = 'completed'
    AND complete_partitions
    AND display_release_state = 'enabled'
    AND observation_start <= observation_end
    AND signal_date = observation_end
    AND observation_end < CURRENT_DATE('Africa/Johannesburg')
),
released_predictions AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    prediction_id,
    predicted_at
  FROM predictions
  WHERE display_eligible
),
member_types AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    canonical_value,
    COUNT(DISTINCT candidate_type) AS distinct_candidate_types,
    MIN(candidate_type) AS candidate_type
  FROM memberships
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    canonical_value
),
available_receipts AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    ARRAY_AGG(
      STRUCT(
        evidence_id AS evidence_id,
        url AS url,
        platform AS platform,
        source_family AS source_family,
        author_label AS author_label,
        excerpt AS excerpt,
        metric_label AS metric_label,
        published_at AS published_at
      )
      ORDER BY evidence_id
    ) AS receipts
  FROM evidence
  WHERE availability = 'available'
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id
),
same_run_analysis AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    COUNT(*) AS analysis_row_count,
    MIN(why_now) AS why_now,
    MIN(possible_response) AS possible_response
  FROM analysis
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id
)
SELECT
  'desk_dynamic_signal_v1' AS contract_version,
  r.run_id AS run_id,
  r.client_scope_id AS client_scope_id,
  r.market_scope AS market_scope,
  r.signal_date AS signal_date,
  r.observation_start AS observation_start,
  r.observation_end AS observation_end,
  r.observation_method AS observation_method,
  r.completed_at AS run_completed_at,
  c.signal_id AS signal_id,
  c.market AS market,
  c.label AS signal_name,
  CASE m.candidate_type
    WHEN 'keyword' THEN 'phrase'
    WHEN 'slang' THEN 'phrase'
    WHEN 'headline' THEN 'phrase'
    WHEN 'hashtag' THEN 'hashtag'
    WHEN 'sound' THEN 'sound'
    WHEN 'creator' THEN 'creator'
    WHEN 'entity' THEN 'entity'
  END AS discovery_mode,
  c.evidence_state AS evidence_state,
  IF(a.analysis_row_count = 1, a.why_now, NULL) AS why_now,
  IF(a.analysis_row_count = 1, a.possible_response, NULL) AS possible_response,
  p.prediction_id AS prediction_id,
  p.predicted_at AS predicted_at,
  IFNULL(e.receipts, []) AS receipts
FROM released_runs r
JOIN candidates c
  ON c.client_scope_id = r.client_scope_id
  AND c.run_id = r.run_id
  AND c.signal_date = r.signal_date
JOIN released_predictions p
  ON p.client_scope_id = c.client_scope_id
  AND p.run_id = c.run_id
  AND p.signal_date = c.signal_date
  AND p.market = c.market
  AND p.signal_id = c.signal_id
JOIN member_types m
  ON m.client_scope_id = c.client_scope_id
  AND m.run_id = c.run_id
  AND m.signal_date = c.signal_date
  AND m.market = c.market
  AND m.signal_id = c.signal_id
  AND m.canonical_value = c.label
LEFT JOIN available_receipts e
  ON e.client_scope_id = c.client_scope_id
  AND e.run_id = c.run_id
  AND e.signal_date = c.signal_date
  AND e.market = c.market
  AND e.signal_id = c.signal_id
LEFT JOIN same_run_analysis a
  ON a.client_scope_id = c.client_scope_id
  AND a.run_id = c.run_id
  AND a.signal_date = c.signal_date
  AND a.market = c.market
  AND a.signal_id = c.signal_id
WHERE m.distinct_candidate_types = 1
  AND c.market IN UNNEST(r.market_scope);
