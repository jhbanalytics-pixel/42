-- name: candidates
-- The top 10 of one market's current detect run for @d. Detect-eligible rows rank first, then strict market
-- majorities with at least 3 scoped posts and 2 market posts, other strict majorities, and global rows.
-- Within each bucket, rows with local posts by at least 2 distinct creators beyond the YouTube trending board
-- (local_first below) rank before rows whose only local evidence is a board video's region or one creator's posts:
-- a foreign channel's video on the market's board counts as a market post for scope, so three such videos under one
-- tag make a strict market majority with 3 creators, yet no local creator stands behind them. The key sits inside
-- the scope bucket because a global-scope row never reaches Today (job.py _for_today) or the market's payload, so
-- local breadth must not lift it over a market-scope row. What counts as local for the gates, the critic and the
-- support check does not change. Then items with fewer than 2 creators or fewer than 3 posts in 3 days
-- (item_state's creators3 and posts3, NULL read as 0) rank after the rest: a single-video board tag can never reach
-- MIN_EVIDENCE, so it should not take a judged slot from an item with breadth. This only orders the pool; nothing
-- is dropped and no threshold changes. Then non-NULL worth_raw ranks first, followed by worth_raw descending and
-- item_id. Label, key, first seen and 28-day sighted platforms provide context only; they do not affect rank. The
-- top 90 come back so core/brief/job.py can rank rows without a readable title (platform ids) after named ones and
-- keep 10, judging the next ones in the place of any held for invalid data days (G1). The LIMIT must equal job.py's
-- POOL.
WITH seen AS (
  SELECT pi.item_id, ARRAY_AGG(DISTINCT po.platform ORDER BY po.platform) platforms
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  WHERE po.market = @market AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 27 DAY) AND @d
    AND po.platform IS NOT NULL AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY pi.item_id),
fs AS (
  SELECT i.item_id, MIN(i.metric_date) first_seen
  FROM {core}.v_item_daily_current i
  WHERE i.market = @market AND i.metric_date <= @d
  GROUP BY i.item_id),
-- Posts sighted in the market in the last 7 days whose market is @market by a source other than the YouTube
-- trending board: the market's own feeds and lists (TikTok, Instagram, Facebook, Reddit and the rest) in
-- v_post_source_markets. A video on the board carries the board's region as its source market whoever made it
-- (core/collect/parse.py _source_provenance), so the board alone never counts here.
sourced AS (
  SELECT DISTINCT v.post_id
  FROM {core}.v_post_source_markets v
  CROSS JOIN UNNEST(v.source_sightings) sight
  WHERE sight.source_market = @market AND IFNULL(sight.route, '') != 'youtube/videos/trending'
    AND sight.obs_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d),
-- Distinct creators of the item's local posts: located in the market (the market_scope.sql rule) or sourced
-- above and not located in another market by that rule, by a creator not on the suppression list. Rank only: it is
-- not selected, so nothing downstream reads it.
local_first AS (
  SELECT pi.item_id, COUNT(DISTINCT IFNULL(ps.creator_id, ps.post_id)) creators
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  JOIN {core}.posts ps ON ps.post_id = po.post_id
  LEFT JOIN sourced ON sourced.post_id = po.post_id
  WHERE po.market = @market AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
    AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = ps.creator_id)
    AND ((ps.geo_market = @market AND IFNULL(ps.geo_confidence, 0) >= 0.7
          AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'))
      OR (sourced.post_id IS NOT NULL
          AND NOT IFNULL(ps.geo_market != @market AND ps.geo_confidence >= 0.7
                         AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)))
  GROUP BY pi.item_id),
ordered AS (
SELECT s.*, cm.kind map_kind, cm.status map_status, cm.label, cm.canonical_key, fs.first_seen,
  seen.platforms seen_platforms, ms.market_scope _selection_market_scope,
  ROW_NUMBER() OVER (ORDER BY s.eligible IS NOT TRUE,
    CASE WHEN ms.market_scope = 'market' AND ms.total_posts7 >= 3 AND ms.market_posts7 >= 2 THEN 0
         WHEN ms.market_scope = 'market' THEN 1 ELSE 2 END,
    IFNULL(lf.creators, 0) < 2,
    IFNULL(s.creators3, 0) < 2 OR IFNULL(s.posts3, 0) < 3,
    s.worth_raw IS NULL, s.worth_raw DESC, s.item_id) _selection_sql_rank
FROM {core}.v_item_state_current s
LEFT JOIN {core}.cultural_map cm ON cm.item_id = s.item_id AND cm.valid_to IS NULL
LEFT JOIN fs ON fs.item_id = s.item_id
LEFT JOIN seen ON seen.item_id = s.item_id
LEFT JOIN local_first lf ON lf.item_id = s.item_id
LEFT JOIN {core}.v_item_market_scope ms
  ON ms.metric_date = s.metric_date AND ms.item_id = s.item_id AND ms.market = s.market
WHERE s.metric_date = @d AND s.market = @market),
selection_snapshot AS (
  SELECT COUNT(*) total_count, COUNTIF(eligible IS TRUE) eligible_count,
    COUNT(DISTINCT run_id) detect_run_count, MAX(run_id) detect_run_id,
    ARRAY_AGG(IF(eligible IS TRUE,
      STRUCT(item_id, label, canonical_key, map_kind, _selection_sql_rank AS sql_rank,
             _selection_market_scope AS market_scope), NULL)
      IGNORE NULLS ORDER BY _selection_sql_rank) items
  FROM ordered)
SELECT ordered.*,
  IF(_selection_sql_rank = 1, TO_JSON_STRING(STRUCT(
    selection_snapshot.total_count, selection_snapshot.eligible_count,
    selection_snapshot.detect_run_count, selection_snapshot.detect_run_id, selection_snapshot.items)), NULL)
    _selection_snapshot
FROM ordered CROSS JOIN selection_snapshot
ORDER BY _selection_sql_rank
LIMIT 90
