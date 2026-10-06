-- One-time DDL for the predictions archive. Append-only ledger of every
-- day-7 forecast the boosted-tree emits, so the accuracy watchdog can score
-- the realised forecast against persistence weeks later. IF NOT EXISTS so a
-- re-run never wipes history (this is NOT a CREATE OR REPLACE). One row per
-- (run_date, market, query_group): run_date is the day the forecast was made,
-- forecast_day = run_date + 7 is the day it predicts, latest_actual is the
-- persistence baseline (today's score) captured alongside the forecast.
--
-- Partitioned by run_date so the watchdog's trailing-28-day window prunes to a
-- handful of partitions. Dormant while FORECAST_ENABLED is off: nothing writes
-- here until a predictor runs, and the watchdog SKIPs on the empty table.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.predictions_archive` (
  run_date DATE NOT NULL,
  forecast_day DATE NOT NULL,
  market STRING NOT NULL,
  query_group STRING NOT NULL,
  forecast_score FLOAT64,
  latest_actual FLOAT64,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY run_date
OPTIONS (
  description = 'Append-only archive of day-7 forecasts for the persistence backtest'
);
