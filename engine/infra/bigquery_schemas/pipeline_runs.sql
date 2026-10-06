-- Pipeline execution audit log
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.pipeline_runs` (
  run_id STRING NOT NULL,
  environment STRING NOT NULL,
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  status STRING NOT NULL,
  market STRING,
  brand24_rows INT64 DEFAULT 0,
  bigquery_trends_rows INT64 DEFAULT 0,
  ensemble_rows INT64 DEFAULT 0,
  youtube_rows INT64 DEFAULT 0,
  gdelt_rows INT64 DEFAULT 0,
  rss_rows INT64 DEFAULT 0,
  total_rows INT64 DEFAULT 0,
  trends_scored INT64 DEFAULT 0,
  briefs_generated INT64 DEFAULT 0,
  errors ARRAY<STRING>,
  notes STRING,
  -- Topic clustering refactor: count of rows with empty topic_groups per run.
  unclassified_rows INT64,
  -- Email digest outcome for the day. Written on a separate marker row
  -- (market='ALL', status='email_audit') after the send resolves, so a
  -- swallowed SMTP/auth/render failure is visible in BigQuery instead of
  -- only stdout. NULL on the per-market status rows. Added live via
  -- scripts/migrations/add_email_status_column.py.
  email_status STRING,
  -- Per-source row counts for connectors added after the original DDL.
  -- All were applied to the live table via idempotent ALTER ADD COLUMN
  -- migrations, which cannot set a DEFAULT, so unlike the columns above
  -- these carry no DEFAULT and read NULL until the next run writes them.
  -- Consumers wrap them in COALESCE/IFNULL (engine_pulse, engine_evolve,
  -- v_pipeline_health). Listed in live append order.
  -- reddit_rows: scripts/migrations/add_reddit_rows_column.py (27 May).
  -- the rest: scripts/migrations/add_wave1_wave2_rows_columns.py (28 May).
  reddit_rows INT64,
  apple_music_rows INT64,
  top_terms_rows INT64,
  brand24_mention_sentiment_rows INT64,
  brand24_mention_reach_rows INT64,
  brand24_daily_metric_rows INT64,
  youtube_playlist_items_rows INT64,
  -- Wave 1 (add_wave1_columns.py): per-layer classification row counts and the
  -- overall labelling rate, written by run_rss_now's classification instrumentation.
  classification_brand24_rows INT64,
  classification_regex_rows INT64,
  classification_gdelt_rows INT64,
  classification_slang_rows INT64,
  classification_embedding_rows INT64,
  labelling_rate_percent FLOAT64,
  -- Wave 2 connector row counts (add_wave2_connector_rows.py, 19 Jun). Written
  -- every run by log_pipeline_run; no DEFAULT, same as the migration-added
  -- columns above. Listed here so a fresh setup_bigquery creates them and the
  -- end-of-run load does not fail on an unknown column.
  wikipedia_rows INT64,
  bluesky_rows INT64,
  -- Google Trends RSS row count (add_google_trends_rss_rows_column.py, 3 Jul).
  google_trends_rss_rows INT64,
  -- App Store top-charts row count (add_app_charts_rows_column.py, 4 Jul).
  app_charts_rows INT64,
  -- Audiomack + Cloudflare Radar row counts
  -- (add_audiomack_radar_rows_columns.py, 4 Jul).
  audiomack_rows INT64,
  cloudflare_radar_rows INT64,
  -- YouTube yt-dlp scrape + SocialCrawl eval channel row counts
  -- (add_youtube_scrape_socialcrawl_rows_columns.py, 5 Jul 2026).
  youtube_scrape_rows INT64,
  socialcrawl_rows INT64,
  -- Quoted SocialCrawl debit for the run (add_socialcrawl_credits_column.py); the
  -- pipeline writes it on every row, so the table needs it even before the
  -- funded lane runs (staging refused the load until it existed, 4 Sep 2026).
  socialcrawl_credits INT64
)
OPTIONS (
  description = 'Pipeline execution audit trail with per-source row counts and error tracking'
);
