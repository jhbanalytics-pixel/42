-- The day-7 forecast for each series joined to today's actual trend_score, so
-- the caller can reduce each series to heating / steady / cooling. One row per
-- series. Only series present in BOTH the forecast table and today's scores
-- survive the join, which is exactly the set the renderer can tag.
WITH day7 AS (
  SELECT market, query_group, forecast_score AS day7_forecast
  FROM `{project}.{dataset}.score_forecast_7d`
  WHERE forecast_day = (
    SELECT MAX(forecast_day) FROM `{project}.{dataset}.score_forecast_7d`
  )
),
actual AS (
  SELECT market, query_group, trend_score AS latest_actual
  FROM `{project}.{dataset}.trend_scores`
  WHERE trend_date = @trend_date
)
SELECT
  d.market,
  d.query_group,
  a.latest_actual,
  d.day7_forecast
FROM day7 d
JOIN actual a USING (market, query_group)
