-- Nightly discovery graph: terms extracted from enriched_content per platform.
--
-- One row per (market, term, term_type, platform, trend_date). Built by
-- src/analysis/seed_graph.py with day delete+insert idempotency. Partition
-- expires after 365 days.
--
-- Created by scripts/migrations/create_seed_graph_table.py.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.seed_graph` (
  market STRING NOT NULL,
  term STRING NOT NULL,
  term_type STRING NOT NULL,
  platform STRING NOT NULL,
  trend_date DATE NOT NULL,
  event_date DATE,
  row_count INT64,
  avg_genz_score FLOAT64,
  slang_row_share FLOAT64,
  topic_groups ARRAY<STRING>,
  near_topics ARRAY<STRING>,
  co_occur_terms ARRAY<STRING>,
  sample_row_ids ARRAY<STRING>,
  generated_at TIMESTAMP
)
PARTITION BY trend_date
CLUSTER BY market, term
OPTIONS (
  partition_expiration_days = 365,
  description = 'Discovery loop term graph: slang, hashtags, tokens per platform per day.'
);

CREATE OR REPLACE VIEW `{project}.{dataset}.v_seed_first_seen` AS
SELECT
  market,
  term,
  platform,
  MIN(event_date) AS first_seen_event_date,
  MIN(trend_date) AS first_seen_ingest_date
FROM `{project}.{dataset}.seed_graph`
GROUP BY market, term, platform;
