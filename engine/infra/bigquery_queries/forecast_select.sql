-- Materialise the 7-day-ahead forecast from the boosted-tree, one row per
-- series. The regressor predicts the single t+7 trend_score directly (not a
-- 7-step path), so this writes one row per (market, query_group) at
-- forecast_day = run_date + 7, which is exactly the day-7 point the outlook
-- chip reads. The point forecast is clamped to [0, 1] (trend_score is a [0, 1]
-- quantity). lo_bound / hi_bound / confidence_level are NULL: a boosted-tree
-- has no native prediction interval, and the outlook chip does not use them
-- (classify_outlook compares only the day-7 point to today's actual). The
-- score_forecast_7d schema is preserved so forecast_outlook.sql and any
-- downstream reader are unchanged.
--
-- Features for the prediction row come from forecast_supervised at t = run_date
-- (built by forecast_train.sql in the same cron step). Rows missing a lag
-- (a series under 8 days old) are skipped, so they simply show no chip.
CREATE OR REPLACE TABLE `{project}.{dataset}.score_forecast_7d` AS
SELECT
  market,
  query_group,
  DATE_ADD(@trend_date, INTERVAL 7 DAY) AS forecast_day,
  LEAST(GREATEST(predicted_label_score_t7, 0.0), 1.0) AS forecast_score,
  CAST(NULL AS FLOAT64) AS lo_bound,
  CAST(NULL AS FLOAT64) AS hi_bound,
  CAST(NULL AS FLOAT64) AS confidence_level,
  @trend_date AS run_date
FROM ML.PREDICT(
  MODEL `{project}.{dataset}.forecast_btree`,
  (
    SELECT
      market,
      query_group,
      score_t,
      score_t1,
      score_t3,
      score_t7,
      mom3,
      mom7,
      item_count,
      velocity_score,
      engagement_score,
      genz_score,
      slang_score,
      source_diversity,
      search_velocity_score,
      tone_score,
      dow
    FROM `{project}.{dataset}.forecast_supervised`
    WHERE t = @trend_date
      AND score_t1 IS NOT NULL
      AND score_t3 IS NOT NULL
      AND score_t7 IS NOT NULL
  )
)
