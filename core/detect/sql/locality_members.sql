INSERT INTO {core}.item_locality_post
  (run_date, market, item_id, detect_run_id, population_cutoff, metric_version, post_id, creator_key, platform,
   locality_class, geo_market, geo_confidence, geo_source, feed_sighted, feed_obs_date)
WITH zones AS (
  SELECT 'ZA' market, 'Africa/Johannesburg' zone
  UNION ALL SELECT 'NG', 'Africa/Lagos'
  UNION ALL SELECT 'KE', 'Africa/Nairobi'),
seen AS (
  SELECT pi.item_id, po.market, po.post_id,
    MIN(IF(IFNULL(po.source_market = po.market, FALSE) AND IFNULL(po.route, '') != 'youtube/videos/trending',
           po.observed_date, NULL)) feed_obs_date
  FROM {core}.post_observations po
  JOIN {core}.tvf_post_items(@d) pi ON pi.post_id = po.post_id AND (pi.market IS NULL OR pi.market = po.market)
  WHERE po.market IN ('ZA', 'NG', 'KE')
    AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.observed_at <= @cutoff
    AND po.lane_class IN ('unbiased_rank', 'panel')
  GROUP BY pi.item_id, po.market, po.post_id),
p AS (
  SELECT s.item_id, s.market, s.post_id, s.feed_obs_date, ps.platform,
    IF(ps.creator_id IS NULL, NULL, CONCAT(IFNULL(ps.platform, ''), ':', ps.creator_id)) creator_key,
    UPPER(TRIM(ps.geo_market)) geo_market, ps.geo_confidence, ps.geo_source
  FROM seen s
  JOIN {core}.posts ps ON ps.post_id = s.post_id
  JOIN zones z ON z.market = s.market
  WHERE ps.post_date BETWEEN DATE_SUB(@d, INTERVAL 8 DAY) AND DATE_ADD(@d, INTERVAL 1 DAY)
    AND DATE(ps.published_at, z.zone) BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = ps.creator_id))
SELECT @d, p.market, p.item_id, @detect_run_id, @cutoff, @metric_version, p.post_id, p.creator_key, p.platform,
  CASE WHEN IFNULL(p.geo_confidence, 0) >= 0.7 AND p.geo_source IN ('ext_region', 'home_market', 'place_mention')
            AND IFNULL(p.geo_market, '') != '' THEN IF(p.geo_market = p.market, 'local', 'foreign')
       ELSE 'unknown' END,
  p.geo_market, p.geo_confidence, p.geo_source, p.feed_obs_date IS NOT NULL, p.feed_obs_date
FROM p
WHERE NOT EXISTS (SELECT 1 FROM {core}.item_locality done
                  WHERE done.run_date = @d AND done.detect_run_id = @detect_run_id
                    AND done.metric_version = @metric_version)
