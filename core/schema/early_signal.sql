CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.early_signal` (
  metric_date DATE NOT NULL, series_id STRING NOT NULL, item_id STRING, market STRING, platform STRING,
  series STRING, protocol STRING, lane_class STRING, kind STRING,
  y FLOAT64, mu FLOAT64, alpha FLOAT64, cusum FLOAT64, early BOOL,
  run_days INT64, days_used INT64, kappa FLOAT64, h FLOAT64, window_days INT64,
  baseline_mode STRING, baseline_source STRING, baseline_date DATE,
  run_id STRING, rule_version STRING)
PARTITION BY metric_date
CLUSTER BY market, item_id;
