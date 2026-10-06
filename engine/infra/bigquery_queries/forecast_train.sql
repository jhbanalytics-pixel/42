-- Boosted-tree 7-day forecast: build the supervised feature table, then train.
--
-- Replaces the ARIMA_PLUS model (turned off 7 Jun 2026 after a 3-week backtest
-- proved it loses to a naive "today holds" baseline every week). Reframed as
-- supervised learning: every (market, topic, day t) becomes one leak-safe
-- labelled row, features as-of t, label = the realised trend_score at t+7. A
-- BOOSTED_TREE_REGRESSOR on those features BEATS persistence on MAE 3/3
-- walk-forward weeks (0.0552/0.0611/0.0362 vs 0.0659/0.0653/0.0401) with
-- 58/55/67% directional precision (vs ARIMA's 17%).
--
-- Two statements (a BigQuery script): (1) materialise forecast_supervised, the
-- leak-safe feature table; (2) train forecast_btree on the rows whose t+7 label
-- is realised. Idempotent (CREATE OR REPLACE). Same 2026-04-21 floor + rolling
-- 180-day window as the old model so create-model bytes stay bounded. Lags are
-- date-based self-joins (robust to any gap in a series' daily axis), not row
-- offsets. Non-fatal + gated by FORECAST_ENABLED upstream.

CREATE OR REPLACE TABLE `{project}.{dataset}.forecast_supervised` AS
WITH base AS (
  SELECT
    market,
    query_group,
    trend_date AS t,
    CAST(trend_score AS FLOAT64) AS score_t,
    item_count,
    velocity_score,
    engagement_score,
    genz_score,
    slang_score,
    source_diversity,
    search_velocity_score,
    tone_score,
    EXTRACT(DAYOFWEEK FROM trend_date) AS dow
  FROM `{project}.{dataset}.trend_scores`
  WHERE trend_date >= GREATEST(DATE '2026-04-21', DATE_SUB(@trend_date, INTERVAL 180 DAY))
    AND trend_date <= @trend_date
    AND market IN ('za', 'ng', 'ke')
)
SELECT
  b.market,
  b.query_group,
  b.t,
  DATE_ADD(b.t, INTERVAL 7 DAY) AS label_date,
  b.score_t,
  l1.score_t AS score_t1,
  l3.score_t AS score_t3,
  l7.score_t AS score_t7,
  b.score_t - l3.score_t AS mom3,
  b.score_t - l7.score_t AS mom7,
  b.item_count,
  b.velocity_score,
  b.engagement_score,
  b.genz_score,
  b.slang_score,
  b.source_diversity,
  b.search_velocity_score,
  b.tone_score,
  b.dow,
  lab.score_t AS label_score_t7
FROM base b
LEFT JOIN base l1
  ON l1.market = b.market AND l1.query_group = b.query_group
  AND l1.t = DATE_SUB(b.t, INTERVAL 1 DAY)
LEFT JOIN base l3
  ON l3.market = b.market AND l3.query_group = b.query_group
  AND l3.t = DATE_SUB(b.t, INTERVAL 3 DAY)
LEFT JOIN base l7
  ON l7.market = b.market AND l7.query_group = b.query_group
  AND l7.t = DATE_SUB(b.t, INTERVAL 7 DAY)
LEFT JOIN base lab
  ON lab.market = b.market AND lab.query_group = b.query_group
  AND lab.t = DATE_ADD(b.t, INTERVAL 7 DAY);

CREATE OR REPLACE MODEL `{project}.{dataset}.forecast_btree`
OPTIONS (
  model_type = 'BOOSTED_TREE_REGRESSOR',
  input_label_cols = ['label_score_t7'],
  l1_reg = 0,
  l2_reg = 1,
  max_tree_depth = 4,
  num_parallel_tree = 1,
  subsample = 0.85,
  data_split_method = 'NO_SPLIT'
) AS
SELECT
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
  dow,
  market,
  query_group,
  label_score_t7
FROM `{project}.{dataset}.forecast_supervised`
WHERE label_score_t7 IS NOT NULL
  AND score_t1 IS NOT NULL
  AND score_t3 IS NOT NULL
  AND score_t7 IS NOT NULL;
