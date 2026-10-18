-- name: market_scope
-- The location share of the exact evidence set in evidence.sql: seven local days, no creator on the suppression list,
-- members of the item's cluster in the market's own run first (C4 v2 section 19), two posts per creator chosen
-- members first, twelve total, less the confirm search finds that are not local (see creator_ranked). The ordering
-- is copied from evidence.sql and v_item_market_scope; tests/test_pack_scope.py checks all three against each other.
-- Since 18 Oct 2026 each tier ranks local creator posts first (local, not news, not sighted on the YouTube trending
-- board alone), then posts sharing the dominant sound or specific format (the one most different creators among
-- the candidate posts share, 2 or more), then measured lanes, engagement and post id, as evidence.sql r1 to rc.
WITH seen AS (
  SELECT po.post_id, LOGICAL_OR(po.lane_class IN ('unbiased_rank', 'panel')) measured,
    LOGICAL_OR(IFNULL(po.lane, '') != 'confirm') beyond_confirm,
    LOGICAL_AND(IFNULL(po.series, '') = 'board_youtube') board_only
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  WHERE pi.item_id = @item_id AND po.market = @market
    AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.lane_class != 'legacy'
    AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY po.post_id
), source_market_by_post AS (
  -- As evidence.sql: the post's one source market in the window, for the local_creator key.
  SELECT v.post_id,
    COALESCE(MAX(IF(sight.source_market = @market, sight.source_market, NULL)),
             IF(COUNT(DISTINCT sight.source_market) = 1, MAX(sight.source_market), NULL)) source_market
  FROM {core}.v_post_source_markets v
  CROSS JOIN UNNEST(v.source_sightings) sight
  WHERE sight.obs_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND sight.source_market IN ('ZA', 'NG', 'KE')
  GROUP BY v.post_id
), enriched AS (
  SELECT post_id, ARRAY_CONCAT_AGG(formats) formats FROM {core}.post_enrichment GROUP BY post_id
), members AS (
  SELECT DISTINCT mb.post_id
  FROM {core}.cluster_members mb
  JOIN {core}.clusters k ON k.cluster_id = mb.cluster_id
  WHERE k.item_id = @item_id AND UPPER(k.market) = @market
    AND k.cluster_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
), candidates AS (
  SELECT ps.post_id, ps.platform, ps.creator_id, ps.geo_market, ps.geo_confidence, ps.geo_source, seen.measured,
    IFNULL(ps.engagement, 0) eng, mm.post_id IS NOT NULL market_member, ps.sound_id, pe.formats,
    IFNULL((ps.geo_market = @market AND IFNULL(ps.geo_confidence, 0) >= 0.7
         AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'))
      OR (NOT IFNULL(NULLIF(ps.geo_market, '') != @market AND ps.geo_confidence >= 0.7
                     AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
          AND smbp.source_market = @market), FALSE)
      AND NOT IFNULL(seen.board_only, FALSE) AND IFNULL(LOWER(TRIM(ps.platform)), '') != 'news' local_creator
  FROM seen
  JOIN {core}.posts ps ON ps.post_id = seen.post_id
  LEFT JOIN members mm ON mm.post_id = seen.post_id
  LEFT JOIN source_market_by_post smbp ON smbp.post_id = seen.post_id
  LEFT JOIN enriched pe ON pe.post_id = seen.post_id
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
), pat_sound AS (
  SELECT c.platform, c.sound_id, COUNT(DISTINCT IFNULL(c.creator_id, c.post_id)) n
  FROM candidates c WHERE c.sound_id IS NOT NULL GROUP BY c.platform, c.sound_id
), pat_format AS (
  SELECT fmt, COUNT(DISTINCT IFNULL(c.creator_id, c.post_id)) n
  FROM candidates c CROSS JOIN UNNEST(c.formats) fmt
  WHERE fmt NOT IN ('talking_head', 'other', 'news_clip') GROUP BY fmt
), patterned AS (
  SELECT c.*,
    GREATEST(IFNULL(pat_sound.n, 0),
             IFNULL((SELECT MAX(pf.n) FROM pat_format pf WHERE pf.fmt IN UNNEST(c.formats)), 0)) pattern_creators
  FROM candidates c
  LEFT JOIN pat_sound ON pat_sound.platform = c.platform AND pat_sound.sound_id = c.sound_id
), dominant AS (
  SELECT p.*, p.pattern_creators >= 2 AND p.pattern_creators = MAX(p.pattern_creators) OVER () shares_dominant
  FROM patterned p
), creator_ranked AS (
  SELECT d.*,
    ROW_NUMBER() OVER (PARTITION BY IFNULL(d.creator_id, d.post_id)
                       ORDER BY NOT d.market_member, NOT d.local_creator, NOT d.shares_dominant, d.measured DESC,
                                d.eng DESC, d.post_id) creator_rank
  FROM dominant d
), eligible_posts AS (
  SELECT * FROM creator_ranked
  WHERE creator_rank <= 2
  ORDER BY market_member DESC, local_creator DESC, shares_dominant DESC, measured DESC, eng DESC, post_id
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
