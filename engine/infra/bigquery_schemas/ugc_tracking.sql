-- UGC asset tracking for 1% conversion KPI
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.ugc_tracking` (
  tracking_id STRING NOT NULL,
  cycle_id STRING,
  market STRING NOT NULL,
  platform STRING NOT NULL,
  hashtag STRING,
  ugc_count INT64 DEFAULT 0,
  estimated_reach INT64 DEFAULT 0,
  conversion_rate FLOAT64 DEFAULT 0,
  tracked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP() NOT NULL
)
PARTITION BY DATE(tracked_at)
CLUSTER BY market, platform
OPTIONS (
  description = 'UGC asset creation tracking for #CreatedWithGemini conversion measurement'
);
