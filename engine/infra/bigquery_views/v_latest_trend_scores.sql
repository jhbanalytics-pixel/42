-- Trend leaderboard view.
-- Powers the Trend Leaderboard and Market Comparison dashboard pages.
-- Shows composite scores for each topic per market over the last 30 days,
-- with a human-readable tier label for Looker Studio filtering.

CREATE OR REPLACE VIEW `{project}.{dataset}.v_latest_trend_scores` AS
SELECT
  trend_date,
  market,
  query_group,
  trend_score,
  CASE
    WHEN trend_score >= 0.45 THEN 'Trending'
    WHEN trend_score >= 0.30 THEN 'Emerging'
    WHEN trend_score >= 0.18 THEN 'Monitoring'
    ELSE 'Below threshold'
  END AS tier,
  item_count,
  engagement_sum,
  source_diversity,
  creator_spread,
  velocity_score,
  engagement_score,
  diversity_score,
  creator_score,
  regional_score,
  genz_score,
  watchlist_score,
  slang_score,
  search_velocity_score,
  scored_at
FROM `{project}.{dataset}.trend_scores`
WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
ORDER BY trend_date DESC, market, trend_score DESC;
