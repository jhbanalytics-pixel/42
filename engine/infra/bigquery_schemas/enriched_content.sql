-- Enriched content with scoring signals
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.enriched_content` (
  id STRING NOT NULL,
  source STRING NOT NULL,
  platform STRING NOT NULL,
  market STRING NOT NULL,
  content_type STRING,
  query_group STRING,
  query_term STRING,
  author_name STRING,
  author_handle STRING,
  author_handle_norm STRING,
  title STRING,
  text STRING,
  url STRING,
  published_at TIMESTAMP,
  collected_at TIMESTAMP NOT NULL,
  hashtags STRING,
  views FLOAT64 DEFAULT 0,
  likes FLOAT64 DEFAULT 0,
  comments FLOAT64 DEFAULT 0,
  shares FLOAT64 DEFAULT 0,
  engagement_total FLOAT64 DEFAULT 0,
  regional_score FLOAT64 DEFAULT 0,
  genz_score FLOAT64 DEFAULT 0,
  creator_watchlist_tier STRING,
  creator_watchlist_score FLOAT64 DEFAULT 0,
  slang_terms STRING,
  slang_score FLOAT64 DEFAULT 0,
  search_velocity_score FLOAT64 DEFAULT 0,
  pipeline_run_id STRING,
  -- GDELT GKG 2.0 metadata fields (kept through enrichment for Step 4 tone scoring).
  v2tone STRING,
  v2persons STRING,
  v2orgs STRING,
  v2locations STRING,
  -- GCAM emotional-cognitive dimensions (kept through enrichment for downstream emotion scoring).
  v2gcam STRING,
  -- Step 4: parsed tone metrics. NaN / NULL for non-GDELT rows.
  tone_avg FLOAT64,
  tone_polarity FLOAT64,
  -- Topic clustering refactor: list of topic_group names matched by classifier.
  topic_groups ARRAY<STRING>,
  -- Wave 1 (add_wave1_columns.py): which classifier layer labelled the row
  -- (brand24 / regex / gdelt / slang / embedding), NULL unless instrumentation on.
  classification_layer STRING,
  -- Near-miss capture (NEAR_MISS_CAPTURE_ENABLED): nearest anchor topic and its
  -- cosine for rows the classifier did not place. The pipeline writes both on
  -- every row, so the table needs them even with the flag off (staging refused
  -- every load until they existed, 4 Sep 2026). On staging they sit after the
  -- authority block because ALTER TABLE appends; the readback compares by name.
  near_topic STRING,
  near_cosine FLOAT64,
  -- Per-row social sentiment lexicon score (add_sentiment_lexicon_column.py),
  -- -1.0..1.0. DEFAULT-less to match the live ALTER shape.
  sentiment_lexicon_score FLOAT64,
  endpoint STRING OPTIONS(description = 'exact retained vendor route identifier'),
  vendor_family STRING OPTIONS(description = 'independent vendor collection authority'),
  channel_family STRING OPTIONS(description = 'evidence channel authority'),
  source_family STRING OPTIONS(description = 'compatibility alias equal to channel_family for Wave 1 rows'),
  geo_method_id STRING OPTIONS(description = 'retained row-level geo method identifier'),
  geo_receipt_id STRING OPTIONS(description = 'retained row-level geo authority receipt identifier'),
  native_id STRING OPTIONS(description = 'platform-native content identity used before URL deduplication'),
  source_family_map_version STRING OPTIONS(description = 'exact source identity map version')
)
PARTITION BY DATE(collected_at)
CLUSTER BY market, platform, query_group
OPTIONS (
  description = 'Content enriched with topic detection, slang scoring, market assignment, and watchlist matching'
);
