-- Trend briefs social references view, one row per (trend, ref).
--
-- Splits trend_analysis.social_refs (ARRAY<STRING>, pipe-delimited
-- "url | platform | title") into discrete rows so the Looker "Social
-- References" panel can bind directly without UNNESTing in a calc
-- field. Sibling to v_trend_briefs_creators.
--
-- Why split into its own view: binding both top_creators AND social_refs
-- as unnested arrays in a single Looker table panel creates a Cartesian
-- product (N creators x M refs rows per topic). Each panel must consume
-- exactly one unnested view for clean rendering.
--
-- Row shape: one row per ref entry. Keyed on
-- (trend_date, market, topic_group, ref_rank). Joined back to the topic
-- via (trend_date, market, topic_group) for cross-panel filters.
--
-- 30-day window matches v_trend_briefs.

CREATE OR REPLACE VIEW `{project}.{dataset}.v_trend_briefs_social_refs` AS
WITH unnested AS (
  SELECT
    trend_date,
    LOWER(market) AS market,
    query_group AS topic_group,
    trend_score,
    status_tag,
    ref_entry,
    ref_offset
  FROM `{project}.{dataset}.trend_analysis`,
  UNNEST(IFNULL(social_refs, [])) AS ref_entry WITH OFFSET ref_offset
  WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
)
SELECT
  trend_date,
  market,
  CASE
    WHEN market = 'za' THEN 'South Africa'
    WHEN market = 'ng' THEN 'Nigeria'
    WHEN market = 'ke' THEN 'Kenya'
    ELSE UPPER(market)
  END AS market_label,
  topic_group,
  trend_score,
  status_tag,
  CASE
    WHEN trend_score >= 0.45 THEN 'Trending'
    WHEN trend_score >= 0.30 THEN 'Emerging'
    WHEN trend_score >= 0.18 THEN 'Monitoring'
    ELSE 'Below threshold'
  END AS tier,
  ref_offset + 1 AS ref_rank,
  ref_entry,
  -- url | platform | title  -> three columns. SAFE_OFFSET so malformed
  -- entries surface NULL rather than crashing the view.
  TRIM(SPLIT(ref_entry, '|')[SAFE_OFFSET(0)]) AS ref_url,
  TRIM(SPLIT(ref_entry, '|')[SAFE_OFFSET(1)]) AS ref_platform,
  TRIM(SPLIT(ref_entry, '|')[SAFE_OFFSET(2)]) AS ref_title
FROM unnested
ORDER BY trend_date DESC, market, topic_group, ref_rank;
