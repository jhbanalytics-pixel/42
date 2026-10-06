-- Pipeline health view.
-- Powers the Pipeline Health dashboard page.
-- Shows every pipeline run with duration, per-source row counts, and a freshness flag.
-- The freshness flag turns red in Looker Studio when no successful run has completed
-- for a given market in the last 12 hours (3x daily schedule means max gap is 8 hours).
--
-- Per-source row columns added by migration scripts (oldest first):
--   reddit_rows                       -- 27 May 2026 (add_reddit_rows_column.py)
--   apple_music_rows                  -- 28 May 2026 (add_wave1_wave2_rows_columns.py)
--   top_terms_rows                    -- 28 May 2026 (add_wave1_wave2_rows_columns.py)
--   brand24_mention_sentiment_rows    -- 28 May 2026 (add_wave1_wave2_rows_columns.py)
--   brand24_mention_reach_rows        -- 28 May 2026 (add_wave1_wave2_rows_columns.py)
--   brand24_daily_metric_rows         -- 28 May 2026 (add_wave1_wave2_rows_columns.py)
--   youtube_playlist_items_rows       -- 28 May 2026 (add_wave1_wave2_rows_columns.py)
--   wikipedia_rows                    -- 19 Jun 2026 (add_wave2_connector_rows.py)
--   bluesky_rows                      -- 19 Jun 2026 (add_wave2_connector_rows.py)
--   google_trends_rss_rows            -- 3 Jul 2026 (add_google_trends_rss_rows_column.py)
--   app_charts_rows                   -- 4 Jul 2026 (add_app_charts_rows_column.py)
--   audiomack_rows                    -- 4 Jul 2026 (add_audiomack_radar_rows_columns.py)
--   cloudflare_radar_rows             -- 4 Jul 2026 (add_audiomack_radar_rows_columns.py)
--   youtube_scrape_rows               -- 5 Jul 2026 (add_youtube_scrape_socialcrawl_rows_columns.py)
--   socialcrawl_rows                  -- 5 Jul 2026 (add_youtube_scrape_socialcrawl_rows_columns.py)
-- Each new column is wrapped in COALESCE so pre-migration rows surface as 0 not NULL.

CREATE OR REPLACE VIEW `{project}.{dataset}.v_pipeline_health` AS
WITH runs AS (
  SELECT
    run_id,
    environment,
    market,
    status,
    started_at,
    finished_at,
    TIMESTAMP_DIFF(
      COALESCE(finished_at, CURRENT_TIMESTAMP()),
      started_at,
      SECOND
    ) AS duration_seconds,
    rss_rows,
    youtube_rows,
    gdelt_rows,
    ensemble_rows,
    brand24_rows,
    bigquery_trends_rows,
    COALESCE(reddit_rows, 0) AS reddit_rows,
    COALESCE(apple_music_rows, 0) AS apple_music_rows,
    COALESCE(top_terms_rows, 0) AS top_terms_rows,
    COALESCE(brand24_mention_sentiment_rows, 0) AS brand24_mention_sentiment_rows,
    COALESCE(brand24_mention_reach_rows, 0) AS brand24_mention_reach_rows,
    COALESCE(brand24_daily_metric_rows, 0) AS brand24_daily_metric_rows,
    COALESCE(youtube_playlist_items_rows, 0) AS youtube_playlist_items_rows,
    COALESCE(wikipedia_rows, 0) AS wikipedia_rows,
    COALESCE(bluesky_rows, 0) AS bluesky_rows,
    COALESCE(google_trends_rss_rows, 0) AS google_trends_rss_rows,
    COALESCE(app_charts_rows, 0) AS app_charts_rows,
    COALESCE(audiomack_rows, 0) AS audiomack_rows,
    COALESCE(cloudflare_radar_rows, 0) AS cloudflare_radar_rows,
    COALESCE(youtube_scrape_rows, 0) AS youtube_scrape_rows,
    COALESCE(socialcrawl_rows, 0) AS socialcrawl_rows,
    total_rows,
    trends_scored,
    briefs_generated,
    -- Surfaced raw (no COALESCE): NULL on per-market status rows, the marker
    -- row's outcome reads through. Added live via add_email_status_column.py;
    -- kept here so the create_looker_views.py deploy path and that migration
    -- converge on one view definition.
    email_status,
    ARRAY_TO_STRING(errors, ' | ') AS error_summary,
    notes
  FROM `{project}.{dataset}.pipeline_runs`
  WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
),
last_success AS (
  SELECT
    market,
    MAX(finished_at) AS last_successful_run
  FROM `{project}.{dataset}.pipeline_runs`
  WHERE status = 'success'
    AND finished_at IS NOT NULL
  GROUP BY market
)
SELECT
  r.*,
  ls.last_successful_run,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), ls.last_successful_run, HOUR) AS hours_since_success,
  CASE
    WHEN ls.last_successful_run IS NULL THEN 'Never run'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), ls.last_successful_run, HOUR) > 12 THEN 'Stale'
    WHEN TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), ls.last_successful_run, HOUR) > 8 THEN 'Late'
    ELSE 'Fresh'
  END AS freshness_status
FROM runs r
LEFT JOIN last_success ls ON r.market = ls.market
ORDER BY r.started_at DESC;
