-- Creator briefs with Nano Banana + Lyria prompts
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.creator_briefs` (
  brief_id STRING NOT NULL,
  trend_date DATE NOT NULL,
  cycle_id STRING,
  market STRING NOT NULL,
  trend_name STRING NOT NULL,
  trend_score FLOAT64,
  nano_banana_prompt STRING,
  nano_banana_trend_trigger STRING,
  lyria_prompt STRING,
  campaign_angles ARRAY<STRING>,
  endorsement_disclosure STRING,
  created_with_gemini_tag STRING DEFAULT 'Created with Gemini',
  creator_tier STRING,
  status STRING DEFAULT 'generated',
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  reviewed_at TIMESTAMP,
  dispatched_at TIMESTAMP,
  exported_to_sheet BOOL DEFAULT FALSE
)
PARTITION BY trend_date
CLUSTER BY market, status
OPTIONS (
  description = 'Creator brief packages with zero-edit Nano Banana and Lyria prompts for influencer dispatch'
);
