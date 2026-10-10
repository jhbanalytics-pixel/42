CREATE OR REPLACE VIEW {core}.v_item_market_scope AS
WITH state_keys AS (
  SELECT DISTINCT metric_date, item_id, market
  FROM {core}.v_item_state_current
  WHERE market IN ('ZA', 'NG', 'KE')
), seen AS (
  SELECT s.metric_date, s.item_id, s.market, po.post_id,
    LOGICAL_OR(po.lane_class IN ('unbiased_rank', 'panel')) measured,
    LOGICAL_OR(IFNULL(po.lane, '') != 'confirm') beyond_confirm
  FROM state_keys s
  JOIN {core}.post_observations po ON po.market = s.market
  JOIN {core}.post_items pi ON pi.post_id = po.post_id AND pi.item_id = s.item_id
  WHERE po.observed_date BETWEEN DATE_SUB(s.metric_date, INTERVAL 6 DAY) AND s.metric_date
    AND po.lane_class != 'legacy'
    AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY s.metric_date, s.item_id, s.market, po.post_id
), creator_ranked AS (
  SELECT seen.metric_date, seen.item_id, seen.market, ps.post_id, ps.geo_market, ps.geo_confidence,
    ps.geo_source, seen.measured, IFNULL(ps.engagement, 0) eng,
    ROW_NUMBER() OVER (PARTITION BY seen.metric_date, seen.item_id, seen.market,
                                    IFNULL(ps.creator_id, ps.post_id)
                       ORDER BY seen.measured DESC, IFNULL(ps.engagement, 0) DESC, ps.post_id) creator_rank
  FROM seen
  JOIN {core}.posts ps ON ps.post_id = seen.post_id
  WHERE DATE(ps.published_at, CASE seen.market
      WHEN 'ZA' THEN 'Africa/Johannesburg'
      WHEN 'NG' THEN 'Africa/Lagos'
      WHEN 'KE' THEN 'Africa/Nairobi'
    END) BETWEEN DATE_SUB(seen.metric_date, INTERVAL 6 DAY) AND seen.metric_date
    -- As evidence.sql: a suppressed creator's posts leave before ranking, so the count describes the pack.
    AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = ps.creator_id)
    -- A post seen only by the brief's confirm search counts only when local by the scored_posts rule below,
    -- as before confirm finds were linked to their item.
    AND (seen.beyond_confirm
      OR (ps.geo_market = seen.market AND IFNULL(ps.geo_confidence, 0) >= 0.7
          AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'))
      OR (NOT IFNULL(NULLIF(ps.geo_market, '') != seen.market AND ps.geo_confidence >= 0.7
                         AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
      AND EXISTS (
        SELECT 1
        FROM {core}.v_post_source_markets v
        CROSS JOIN UNNEST(v.source_sightings) sight
        WHERE v.post_id = ps.post_id
          AND sight.source_market = seen.market
          AND sight.obs_date BETWEEN DATE_SUB(seen.metric_date, INTERVAL 6 DAY) AND seen.metric_date
      )))
), ranked_pack AS (
  SELECT creator_ranked.*,
    ROW_NUMBER() OVER (PARTITION BY metric_date, item_id, market
                       ORDER BY measured DESC, eng DESC, post_id) pack_rank
  FROM creator_ranked
  WHERE creator_rank <= 2
), eligible_posts AS (
  SELECT * FROM ranked_pack WHERE pack_rank <= 12
), scored_posts AS (
  SELECT e.metric_date, e.item_id, e.market, e.post_id,
    ((e.geo_market = e.market AND IFNULL(e.geo_confidence, 0) >= 0.7
      AND e.geo_source IN ('ext_region', 'home_market', 'place_mention'))
    OR (NOT IFNULL(NULLIF(e.geo_market, '') != e.market AND e.geo_confidence >= 0.7
                       AND e.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
    AND EXISTS (
      SELECT 1
      FROM {core}.v_post_source_markets v
      CROSS JOIN UNNEST(v.source_sightings) sight
      WHERE v.post_id = e.post_id
        AND sight.source_market = e.market
        AND sight.obs_date BETWEEN DATE_SUB(e.metric_date, INTERVAL 6 DAY) AND e.metric_date
    ))) in_market
  FROM eligible_posts e
), post_counts AS (
  SELECT metric_date, item_id, market,
    COUNT(DISTINCT IF(in_market, post_id, NULL)) market_posts7,
    COUNT(DISTINCT post_id) total_posts7
  FROM scored_posts
  GROUP BY metric_date, item_id, market
), scoped AS (
  SELECT s.metric_date, s.item_id, s.market,
    IFNULL(c.market_posts7, 0) market_posts7,
    IFNULL(c.total_posts7, 0) total_posts7,
    SAFE_DIVIDE(IFNULL(c.market_posts7, 0), IFNULL(c.total_posts7, 0)) market_share7
  FROM state_keys s
  LEFT JOIN post_counts c USING (metric_date, item_id, market)
), scope_values AS (
  SELECT scoped.*, IF(market_share7 > 0.5, 'market', 'global') market_scope
  FROM scoped
)
SELECT metric_date, item_id, market, market_scope, market_posts7, total_posts7, market_share7
FROM scope_values
