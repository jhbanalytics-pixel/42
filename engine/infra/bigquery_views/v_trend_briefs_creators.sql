-- Trend briefs creators view, one row per (trend, creator).
--
-- Splits trend_analysis.top_creators (ARRAY<STRING>, pipe-delimited
-- "@handle | platform | N mentions") into discrete rows so the Looker
-- "Top Creators" panel can bind directly without UNNESTing in a calc
-- field. Sibling to v_trend_briefs_social_refs.
--
-- Why split into its own view: binding both top_creators AND social_refs
-- as unnested arrays in a single Looker table panel creates a Cartesian
-- product (N creators x M refs rows per topic). Each panel must consume
-- exactly one unnested view for clean rendering.
--
-- Row shape: one row per creator entry. Keyed on
-- (trend_date, market, topic_group, creator_rank). Joined back to the
-- topic via (trend_date, market, topic_group) for cross-panel filters.
--
-- 30-day window matches v_trend_briefs.

CREATE OR REPLACE VIEW `{project}.{dataset}.v_trend_briefs_creators` AS
WITH unnested AS (
  SELECT
    trend_date,
    LOWER(market) AS market,
    query_group AS topic_group,
    trend_score,
    status_tag,
    creator_entry,
    creator_offset
  FROM `{project}.{dataset}.trend_analysis`,
  UNNEST(IFNULL(top_creators, [])) AS creator_entry WITH OFFSET creator_offset
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
  -- 1-based ordinal so the dashboard can show "Top 5" cleanly.
  creator_offset + 1 AS creator_rank,
  creator_entry,
  -- Split the pipe-delimited entry into discrete columns. SAFE_OFFSET
  -- returns NULL on out-of-range, so a malformed entry surfaces NULL
  -- columns rather than crashing the view.
  TRIM(SPLIT(creator_entry, '|')[SAFE_OFFSET(0)]) AS creator_handle,
  TRIM(SPLIT(creator_entry, '|')[SAFE_OFFSET(1)]) AS creator_platform,
  -- Mentions field arrives as "N mentions"; strip the suffix and cast.
  SAFE_CAST(
    REGEXP_EXTRACT(
      TRIM(SPLIT(creator_entry, '|')[SAFE_OFFSET(2)]),
      r'^([0-9]+)'
    ) AS INT64
  ) AS creator_mentions
FROM unnested
ORDER BY trend_date DESC, market, topic_group, creator_rank;
