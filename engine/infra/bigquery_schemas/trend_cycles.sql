-- Trend cycle definitions (8 planned + 4 ad-hoc)
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.trend_cycles` (
  cycle_id STRING NOT NULL,
  cycle_name STRING NOT NULL,
  cycle_type STRING NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  markets ARRAY<STRING>,
  focus_topics ARRAY<STRING>,
  status STRING DEFAULT 'upcoming',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  activated_at TIMESTAMP,
  completed_at TIMESTAMP
)
OPTIONS (
  description = 'Campaign trend cycles: 8 planned and up to 4 ad-hoc reactive cycles'
);
