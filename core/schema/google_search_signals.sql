CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.google_search_signals` (
  market STRING NOT NULL, term STRING NOT NULL,
  source STRING NOT NULL, kind STRING NOT NULL,
  fetched_at TIMESTAMP NOT NULL, refreshed_at STRING NOT NULL,
  rank INT64, refresh_date DATE, raw_payload JSON NOT NULL)
PARTITION BY DATE(fetched_at)
CLUSTER BY market, source;
