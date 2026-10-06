-- Market comparison view (per-market topic leaderboard).
--
-- Powers the Market Comparison dashboard page. Replaces the prior
-- pivoted-by-topic shape, which was broken by the topic clustering
-- refactor (commit 3fe15ef). Pre-refactor, query_group held source-level
-- labels (tiktok_hashtag, news, etc.) that were shared across markets,
-- so MAX(CASE WHEN market='ZA' ...) GROUP BY query_group produced
-- side-by-side comparisons. Post-refactor, the topic taxonomies in
-- configs/topic_groups/{za,ng,ke}.yaml are disjoint by design
-- (music_amapiano lives only in ZA, music_afrobeats only in NG).
-- The pivot would always show NULL for two columns and markets_active
-- would always be 1.
--
-- This view restates the comparison as a per-market top-10 leaderboard.
-- Looker page becomes three stacked panels (ZA / NG / KE), each ranked.
-- Cross-market signal (e.g. "music is up in NG and ZA today") is
-- deferred to a Phase 2 meta-cluster mapping (see
-- internal design notes).

CREATE OR REPLACE VIEW `{project}.{dataset}.v_market_comparison` AS
WITH ranked AS (
  SELECT
    trend_date,
    LOWER(market) AS market,
    query_group AS topic_group,
    trend_score,
    item_count,
    source_diversity,
    creator_spread,
    velocity_score,
    engagement_score,
    regional_score,
    genz_score,
    tone_score,
    CASE
      WHEN trend_score >= 0.45 THEN 'Trending'
      WHEN trend_score >= 0.30 THEN 'Emerging'
      WHEN trend_score >= 0.18 THEN 'Monitoring'
      ELSE 'Below threshold'
    END AS tier,
    ROW_NUMBER() OVER (
      PARTITION BY trend_date, LOWER(market)
      ORDER BY trend_score DESC, item_count DESC
    ) AS rank_in_market
  FROM `{project}.{dataset}.trend_scores`
  WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
)
SELECT
  trend_date,
  market,
  topic_group,
  rank_in_market,
  trend_score,
  tier,
  item_count,
  source_diversity,
  creator_spread,
  velocity_score,
  engagement_score,
  regional_score,
  genz_score,
  tone_score
FROM ranked
WHERE rank_in_market <= 10
ORDER BY trend_date DESC, market, rank_in_market;
