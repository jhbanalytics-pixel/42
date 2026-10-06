-- The morning brief job's reads (BUILD.md 1.12, core/api/contract.md sections 4 and 8), in BigQuery Standard SQL.
-- Read-only. {core} and {agent} are the dataset names, filled in by core/detect/sqlrun.py. core/brief/job.py runs
-- each statement by the name on its own name line.

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
  GROUP BY pi.item_id)
SELECT s.*, cm.kind map_kind, cm.status map_status, cm.label, cm.canonical_key, fs.first_seen,
  seen.platforms seen_platforms
FROM {core}.v_item_state_current s
LEFT JOIN {core}.cultural_map cm ON cm.item_id = s.item_id AND cm.valid_to IS NULL
LEFT JOIN fs ON fs.item_id = s.item_id
LEFT JOIN seen ON seen.item_id = s.item_id
LEFT JOIN local_first lf ON lf.item_id = s.item_id
LEFT JOIN {core}.v_item_market_scope ms
  ON ms.metric_date = s.metric_date AND ms.item_id = s.item_id AND ms.market = s.market
WHERE s.metric_date = @d AND s.market = @market
ORDER BY s.eligible IS NOT TRUE,
  CASE WHEN ms.market_scope = 'market' AND ms.total_posts7 >= 3 AND ms.market_posts7 >= 2 THEN 0
       WHEN ms.market_scope = 'market' THEN 1
       ELSE 2 END,
  IFNULL(lf.creators, 0) < 2,
  IFNULL(s.creators3, 0) < 2 OR IFNULL(s.posts3, 0) < 3,
  s.worth_raw IS NULL, s.worth_raw DESC, s.item_id
LIMIT 90;

-- name: post_set
-- Every post the card for one item could rest on, not only the 12 of its evidence pack: linked to the item and
-- sighted in the market in the pack's days and lanes (core/brief/sql/evidence.sql, any lane but placebo,
-- agent_live and legacy), published from @start to @end, by a creator not on the suppression list. job.py merges
-- a card whose posts are nearly all on a higher card into that card.
SELECT DISTINCT po.post_id
FROM {core}.post_observations po
JOIN {core}.post_items pi ON pi.post_id = po.post_id
JOIN {core}.posts ps ON ps.post_id = po.post_id
WHERE pi.item_id = @item_id AND po.market = @market
  AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  AND ps.published_at >= @start AND ps.published_at < @end
  AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = ps.creator_id)
ORDER BY po.post_id;

-- name: moments
-- Calendar moments for the market from @d to 14 days on, the same span f42-api reads.
SELECT c.moment_date, c.name, c.kind, c.item_ids FROM {core}.calendar c
WHERE c.market = @market AND c.moment_date BETWEEN @d AND DATE_ADD(@d, INTERVAL 14 DAY)
ORDER BY c.moment_date, c.name;

-- name: boards
-- Today's board and chart entries in the market from good collect runs; the X trends archive is a seed only and
-- never shows. The best rank of the day wins; a board read without a rank row counts by appearances. job.py makes
-- the title from kind, label and canonical_key as a card's (card_title) and leaves out entries with no readable name.
SELECT b.platform, b.series, b.item_id, b.kind, b.label, b.canonical_key, b.best_rank, b.appearances FROM (
  SELECT c.platform, c.series, c.item_id, cm.kind, cm.label, cm.canonical_key,
    MIN(IF(c.unit = 'rank', c.value, NULL)) best_rank, MAX(IF(c.unit = 'appearances', c.value, NULL)) appearances
  FROM {core}.v_item_counter_daily_current c
  LEFT JOIN {core}.cultural_map cm ON cm.item_id = c.item_id AND cm.valid_to IS NULL
  WHERE c.obs_date = @d AND c.market = @market AND c.is_board AND c.series != 'x_trends'
  GROUP BY c.platform, c.series, c.item_id, cm.kind, cm.label, cm.canonical_key) b
WHERE b.best_rank IS NOT NULL OR b.appearances > 0
ORDER BY b.platform, b.series, b.best_rank IS NULL, b.best_rank, b.appearances DESC, b.item_id;

-- name: first_collect
-- The first ok collect run on or before @d; the warm-up day counts from it.
SELECT MIN(r.run_date) first_day FROM {agent}.runs r
WHERE r.stage = 'collect' AND r.status = 'ok' AND r.run_date <= @d;

-- name: spent_today
-- Model spend for @d across every stage, once per run_id. Ask writes run.model_usd in record; other stages write
-- model_usd in counts. Understand spend bookings have their own run_ids, including signed corrections.
SELECT IFNULL(SUM(u.usd), 0) usd FROM (
  SELECT r.run_id, MAX(CAST(COALESCE(JSON_VALUE(r.record, '$.run.model_usd'),
                                     JSON_VALUE(r.counts, '$.model_usd')) AS FLOAT64)) usd
  FROM {agent}.runs r
  WHERE r.run_date = @d
  GROUP BY r.run_id) u;

-- name: creator_names
-- The name of each creator item whose label is only the id its key was built from (job.py creator_names), keyed as
-- cultural_map folds a creator key: "platform:creator id" in lower case. The row with a display name and then the
-- most followers wins, as core/api/store.py creator_names reads it. A suppressed creator is never returned.
SELECT CONCAT(LOWER(TRIM(c.platform)), ':', LOWER(TRIM(c.creator_id))) AS key, c.creator_id, c.platform, c.handle,
  c.display_name
FROM {core}.creators c
WHERE CONCAT(LOWER(TRIM(c.platform)), ':', LOWER(TRIM(c.creator_id))) IN (SELECT k.key FROM UNNEST(@keys) k)
  AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = c.creator_id)
QUALIFY ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(c.platform)), LOWER(TRIM(c.creator_id))
                           ORDER BY c.display_name IS NULL, c.followers DESC) = 1;
