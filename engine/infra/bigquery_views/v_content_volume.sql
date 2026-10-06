-- Content volume by connector view.
-- Powers the Content Volume dashboard page.
-- Row counts and engagement totals per day, market and connector (platform).
-- Collapsed to one row per (day, market, connector) per Thapelo's 17 Apr call.
-- source_count shows how many distinct feeds or endpoints contributed.

CREATE OR REPLACE VIEW `{project}.{dataset}.v_content_volume` AS
SELECT
  DATE(collected_at) AS collection_date,
  market,
  platform,
  COUNT(DISTINCT source) AS source_count,
  COUNT(*) AS row_count,
  COUNT(DISTINCT query_group) AS topics_covered,
  SUM(engagement_total) AS total_engagement,
  SUM(views) AS total_views,
  SUM(likes) AS total_likes,
  SUM(comments) AS total_comments,
  SUM(shares) AS total_shares,
  MAX(collected_at) AS latest_item_at
FROM `{project}.{dataset}.raw_content`
WHERE DATE(collected_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
GROUP BY 1, 2, 3
ORDER BY collection_date DESC, market, row_count DESC;
