-- Raw content items from all data sources
-- Partitioned by collection date, clustered by market and platform
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.raw_content` (
  id STRING NOT NULL,
  source STRING NOT NULL,
  platform STRING NOT NULL,
  market STRING,
  content_type STRING,
  query_group STRING,
  query_term STRING,
  author_name STRING,
  author_handle STRING,
  title STRING,
  text STRING,
  url STRING,
  published_at TIMESTAMP,
  collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP() NOT NULL,
  hashtags STRING,
  views FLOAT64 DEFAULT 0,
  likes FLOAT64 DEFAULT 0,
  comments FLOAT64 DEFAULT 0,
  shares FLOAT64 DEFAULT 0,
  engagement_total FLOAT64 DEFAULT 0,
  pipeline_run_id STRING,
  -- GDELT GKG 2.0 metadata fields. Populated on GDELT rows only; empty string elsewhere.
  v2tone STRING,
  v2persons STRING,
  v2orgs STRING,
  v2locations STRING,
  -- GCAM emotional-cognitive dimensions. Populated on GDELT rows only; empty string elsewhere.
  v2gcam STRING,
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
CLUSTER BY market, platform
OPTIONS (
  description = 'Raw social media posts, news articles, and video metadata from all ingestion sources',
  partition_expiration_days = 90
);
