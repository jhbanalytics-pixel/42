-- name: market_scope
-- The location share of the exact evidence set in evidence.sql: seven local days, no creator on the suppression list,
-- two posts per creator, twelve total, less the confirm search finds that are not local (see creator_ranked).
WITH seen AS (
  SELECT po.post_id, LOGICAL_OR(po.lane_class IN ('unbiased_rank', 'panel')) measured,
    LOGICAL_OR(IFNULL(po.lane, '') != 'confirm') beyond_confirm
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  WHERE pi.item_id = @item_id AND po.market = @market
    AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.lane_class != 'legacy'
    AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY po.post_id
), creator_ranked AS (
  SELECT ps.post_id, ps.platform, ps.geo_market, ps.geo_confidence, ps.geo_source, seen.measured,
    IFNULL(ps.engagement, 0) eng,
    ROW_NUMBER() OVER (PARTITION BY IFNULL(ps.creator_id, ps.post_id)
                       ORDER BY seen.measured DESC, IFNULL(ps.engagement, 0) DESC, ps.post_id) creator_rank
  FROM seen
  JOIN {core}.posts ps ON ps.post_id = seen.post_id
  WHERE ps.published_at >= @start AND ps.published_at < @end
    -- As evidence.sql: a suppressed creator's posts leave before ranking, so the count describes the pack.
    AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = ps.creator_id)
    -- A post seen only by the brief's confirm search counts only when local by the market_posts rule below,
    -- as before confirm finds were linked to their item.
    AND (seen.beyond_confirm
      OR (ps.geo_market = @market AND IFNULL(ps.geo_confidence, 0) >= 0.7
          AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'))
      OR (NOT IFNULL(NULLIF(ps.geo_market, '') != @market AND ps.geo_confidence >= 0.7
                         AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
      AND EXISTS (
        SELECT 1
        FROM {core}.v_post_source_markets v
        CROSS JOIN UNNEST(v.source_sightings) sight
        WHERE v.post_id = ps.post_id
          AND sight.source_market = @market
          AND sight.obs_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
      )))
), eligible_posts AS (
  SELECT * FROM creator_ranked
  WHERE creator_rank <= 2
  ORDER BY measured DESC, eng DESC, post_id
  LIMIT 12
), market_posts AS (
  SELECT DISTINCT p.post_id
  FROM eligible_posts p
  WHERE (p.geo_market = @market AND IFNULL(p.geo_confidence, 0) >= 0.7
         AND p.geo_source IN ('ext_region', 'home_market', 'place_mention'))
    OR (NOT IFNULL(NULLIF(p.geo_market, '') != @market AND p.geo_confidence >= 0.7
                       AND p.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
    AND EXISTS (
      SELECT 1
      FROM {core}.v_post_source_markets v
      CROSS JOIN UNNEST(v.source_sightings) sight
      WHERE v.post_id = p.post_id
        AND sight.source_market = @market
        AND sight.obs_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    ))
)
-- market_news_posts7 counts the news public-feed posts among the market posts: they are feed evidence only
-- (W8-DEC-12), so market posts that are all news are read as Market unconfirmed by read_market_scope.
SELECT COUNT(DISTINCT p.post_id) total_posts7, COUNT(DISTINCT m.post_id) market_posts7,
  COUNT(DISTINCT IF(LOWER(TRIM(p.platform)) = 'news', m.post_id, NULL)) market_news_posts7
FROM eligible_posts p
LEFT JOIN market_posts m USING (post_id);
