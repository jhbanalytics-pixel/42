-- Daily aggregated trend scores per market and topic
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.trend_scores` (
  trend_date DATE NOT NULL,
  market STRING NOT NULL,
  query_group STRING NOT NULL,
  cycle_id STRING,
  item_count INT64,
  engagement_sum FLOAT64,
  source_diversity INT64,
  creator_spread INT64,
  velocity_score FLOAT64,
  engagement_score FLOAT64,
  diversity_score FLOAT64,
  creator_score FLOAT64,
  regional_score FLOAT64,
  genz_score FLOAT64,
  watchlist_score FLOAT64,
  slang_score FLOAT64,
  search_velocity_score FLOAT64 DEFAULT 0,
  -- Cross-source corroboration: count of distinct channel families backing the
  -- topic, feeds the cross-source multiplier. Added via an out-of-band ALTER on
  -- the live table (see scripts/migrations/add_channel_diversity_column.py).
  channel_diversity INT64,
  -- Wave 1 (add_wave1_columns.py): rolling velocity, momentum/lifecycle labels
  -- and day-over-day continuity. Kept DEFAULT-less to match the live ALTER shape.
  velocity_score_7d FLOAT64,
  velocity_score_30d FLOAT64,
  momentum_label STRING,
  lifecycle_phase STRING,
  continuity_day INT64,
  continuity_state STRING,
  -- Step 4: tone signal parsed from GDELT V2Tone. tone_rows counts the
  -- rows that contributed; when 0 the tone weight is redistributed.
  tone_score FLOAT64,
  tone_rows INT64,
  trend_score FLOAT64 NOT NULL,
  -- Seed score (add_seed_score_column.py): worth-seeding-for-Nanobanana/Lyria
  -- signal, audience + creative-format fit, independent of trend_score.
  seed_score FLOAT64,
  -- Seed components (add_seed_components_columns.py): the decomposition the tool
  -- shows in the clickable breakdown, so it reads the same numbers the score was
  -- built from. seed = audience_fit * format_fit * safety_gate.
  seed_audience_fit FLOAT64,
  seed_format_fit FLOAT64,
  seed_safety_gate FLOAT64,
  visual_audio_share FLOAT64,
  -- GCAM emotional-arousal score (add_gcam_score_column.py): per-topic intensity
  -- 0..1 and the count of GDELT rows that fed it. DEFAULT-less to match the live
  -- ALTER shape; ships at weight 0.00 so the value is shadow until promoted.
  gcam_score FLOAT64,
  gcam_rows INT64,
  -- Corroboration scorer (add_corroboration_columns.py, PULSE Intelligence Core
  -- Phase 0): two honest numbers (factual vs social, never combined), a
  -- confidence tier, and a recency in hours. Shadow columns: nothing reads them
  -- yet and trend_score is unchanged. DEFAULT-less to match the live ALTER shape.
  factual_corroboration FLOAT64,
  social_corroboration FLOAT64,
  confidence_tier STRING,
  corroboration_recency_hours INT64,
  n_factual INT64,
  n_social INT64,
  -- Semantic corroboration (add_semantic_corroboration_columns.py, forward
  -- Phase 2). families: the most channel families any single shared entity
  -- (hashtag / GDELT person / org) reached in the topic. entities: how many
  -- entities spanned >= 2 families. Shadow: measures same-entity-across-families
  -- vs the structural channel_diversity count; nothing reads them and
  -- trend_score is unchanged. DEFAULT-less to match the live ALTER shape.
  semantic_corroboration_families INT64,
  semantic_corroboration_entities INT64,
  scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY trend_date
CLUSTER BY market, query_group
OPTIONS (
  description = 'Daily composite trend scores with all scoring components'
);
