/*
  Native window totals for open_intelligence_observation_dispositions_v1.
  Every total is computed here, in SQL, over one explicit window and one
  explicit set of market strata, with an explicit row limit on the output.
  Distinct observations are counted over the whole window; nothing here adds
  per day distinct counts together. Reviewed as text; never executed by the
  unit suite. Unavailable boundaries are sink notes, not rows of this relation,
  so no total here counts an unknown outcome; the units report carries them.

  Parameters:
  @window_start   TIMESTAMP     inclusive start of the observation window
  @window_end     TIMESTAMP     exclusive end of the observation window
  @market_strata  ARRAY<STRING> the markets reported as separate strata
  @query_limit    INT64         maximum rows returned (one per stratum and boundary)
*/

ASSERT @window_start < @window_end AS 'window_is_ordered';
ASSERT ARRAY_LENGTH(@market_strata) > 0 AS 'market_strata_are_declared';
ASSERT @query_limit > 0 AS 'query_limit_is_positive';

ASSERT (
  SELECT COUNT(*)
  FROM `ogilvy-trends-v2.trends_v2_staging.open_intelligence_observation_dispositions_v1`
  WHERE observed_at >= @window_start
    AND observed_at < @window_end
    AND market NOT IN UNNEST(@market_strata)
) = 0 AS 'every_market_is_a_declared_stratum';

ASSERT (
  SELECT COUNT(*)
  FROM `ogilvy-trends-v2.trends_v2_staging.open_intelligence_observation_dispositions_v1`
  WHERE observed_at >= @window_start
    AND observed_at < @window_end
    AND outcome = 'rejected'
    AND reason_code IS NULL
) = 0 AS 'rejections_carry_a_reason';

WITH scoped AS (
  SELECT
    market,
    boundary,
    outcome,
    reason_code,
    identity_kind,
    native_namespace,
    native_id,
    observation_key,
    collection_event_id
  FROM `ogilvy-trends-v2.trends_v2_staging.open_intelligence_observation_dispositions_v1`
  WHERE observed_at >= @window_start
    AND observed_at < @window_end
    AND market IN UNNEST(@market_strata)
)
SELECT
  market,
  boundary,
  COUNT(*) AS dispositions,
  COUNT(DISTINCT collection_event_id) AS collection_events,
  COUNT(DISTINCT IF(identity_kind = 'native' AND outcome != 'unknown',
    CONCAT(native_namespace, ':', native_id), NULL)) AS distinct_native_observations,
  COUNT(DISTINCT IF(identity_kind = 'inferred' AND outcome != 'unknown',
    observation_key, NULL)) AS distinct_inferred_identity_observations,
  COUNT(DISTINCT IF(outcome = 'admitted', observation_key, NULL)) AS admitted_observations,
  COUNT(DISTINCT IF(outcome = 'rejected', observation_key, NULL)) AS rejected_observations
FROM scoped
GROUP BY market, boundary
ORDER BY market, boundary
LIMIT @query_limit;
