-- Weekly forecast scores (core/eval/forecast_score.py), one row per rule, target and horizon per run. Append
-- only: a rerun adds rows with a new run_id, and the newest scored_at per week, rule, target and horizon is current.
-- skill is NULL, never zero, while fewer than the minimum forecasts have resolved.
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.forecast_score` (
  week_start DATE NOT NULL, week_end DATE NOT NULL, run_id STRING NOT NULL, scored_at TIMESTAMP,
  rule STRING, target STRING, horizon INT64, n INT64, unresolved INT64, no_baseline INT64, no_prob INT64,
  minimum INT64, brier FLOAT64, persistence_brier FLOAT64, skill FLOAT64, promotion_eligible BOOL,
  query_id STRING, result_hash STRING, row_count INT64, reason STRING)
PARTITION BY week_start
