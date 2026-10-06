-- One forecast_score row. Nullable numbers arrive as untyped NULL parameters, so each is cast to its column type.
INSERT INTO `ogilvy-trends-v2.intelligence_42_agent.forecast_score`
  (week_start, week_end, run_id, scored_at, rule, target, horizon, n, unresolved, no_baseline, no_prob,
   minimum, brier, persistence_brier, skill, promotion_eligible, query_id, result_hash, row_count, reason)
VALUES (@week_start, @week_end, @run_id, @scored_at, @rule, @target, @horizon, @n, @unresolved, @no_baseline,
  @no_prob, @minimum, CAST(@brier AS FLOAT64), CAST(@persistence_brier AS FLOAT64), CAST(@skill AS FLOAT64),
  @promotion_eligible, @query_id, @result_hash, @row_count, CAST(@reason AS STRING))
