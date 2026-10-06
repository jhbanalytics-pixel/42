-- The aggregate step, DATA.md section 3.4: item_daily for metric_date @d, appended under @run_id and @rule_version.
-- Each post counts once per market, item, lane class and panel series, on the market-local day of its first
-- sighting there; plus one '_any' row per market and item (platform '_all') counting each post once across
-- every lane except legacy and agent_live. agent_live sightings never enter item_daily. Series and protocol
-- are set on panel rows only. Known location follows core/detect/geo.py: is_known is geo_confidence >= 0.7,
-- is_local is known and geo_market is the market.
INSERT INTO {core}.item_daily
  (metric_date, market, platform, item_id, lane_class, series, protocol, posts, creators, unflagged_creators,
   engagement, tier_posts, first_post_at, geo_known_posts, local_posts, source_regime, available_at,
   run_id, rule_version)
WITH s AS (
  SELECT po.post_id, po.market, po.lane_class,
    IF(po.lane_class = 'panel', po.series, NULL) series, IF(po.lane_class = 'panel', po.protocol, NULL) protocol,
    po.observed_date
  FROM {core}.post_observations po
  WHERE po.observed_date <= @d AND IFNULL(po.lane, '') != 'agent_live'),
firsts AS (                 -- first sighting per post, market, lane class and panel series, and across lanes
  SELECT s.post_id, s.market, s.lane_class, s.series, s.protocol, MIN(s.observed_date) first_day
  FROM s GROUP BY s.post_id, s.market, s.lane_class, s.series, s.protocol
  UNION ALL
  SELECT s.post_id, s.market, '_any', NULL, NULL, MIN(s.observed_date)
  FROM s WHERE s.lane_class != 'legacy' GROUP BY s.post_id, s.market),
f AS (
  SELECT fi.post_id, fi.market, fi.lane_class, fi.series, fi.protocol, pi.item_id,
    IF(fi.lane_class = '_any', '_all', ps.platform) platform,
    ps.creator_id, IFNULL(cr_flags.coord_score, 0) >= 1 flagged, ps.creator_tier_at_post tier, ps.engagement,
    ps.published_at, ps.source_regime,
    IFNULL(ps.geo_confidence >= 0.7, FALSE) geo_known,
    IFNULL(ps.geo_confidence >= 0.7 AND UPPER(ps.geo_market) = fi.market, FALSE) geo_local
  FROM firsts fi
  JOIN {core}.posts ps ON ps.post_id = fi.post_id
  JOIN (SELECT DISTINCT pit.post_id, pit.item_id FROM {core}.post_items pit) pi ON pi.post_id = fi.post_id
  LEFT JOIN (
    SELECT creator_id, MAX(IFNULL(coord_score, 0)) coord_score
    FROM {core}.creators GROUP BY creator_id
  ) cr_flags ON cr_flags.creator_id = ps.creator_id
  WHERE fi.first_day = @d)
SELECT @d, f.market, f.platform, f.item_id, f.lane_class, f.series, f.protocol,
  COUNT(DISTINCT f.post_id), COUNT(DISTINCT f.creator_id), COUNT(DISTINCT IF(f.flagged, NULL, f.creator_id)),
  SUM(f.engagement),
  STRUCT(COUNTIF(f.tier = 'nano') AS nano, COUNTIF(f.tier = 'micro') AS micro, COUNTIF(f.tier = 'mid') AS mid,
         COUNTIF(f.tier = 'macro') AS macro, COUNTIF(f.tier = 'mega') AS mega),
  MIN(f.published_at), COUNTIF(f.geo_known), COUNTIF(f.geo_local),
  MIN(f.source_regime), CURRENT_TIMESTAMP(), @run_id, @rule_version
FROM f
GROUP BY f.market, f.platform, f.item_id, f.lane_class, f.series, f.protocol
