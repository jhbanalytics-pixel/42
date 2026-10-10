-- The publish gate's context for one item and market (TRUST.md section 2), in BigQuery Standard SQL. Read-only.
-- {core} and {agent} are the dataset names, filled in by core/detect/sqlrun.py. core/brief/gatectx.py runs each
-- statement by the name on its own name line.

-- name: series_platform
-- The platform of the item's main series, from the latest good stats run up to @d.
SELECT st.platform FROM {core}.v_series_test_current st
WHERE st.series_id = @series_id AND st.metric_date <= @d AND st.platform IS NOT NULL
ORDER BY st.metric_date DESC
LIMIT 1;

-- name: sightings
-- The item's sightings in the market over the 14 days to @d, counted per platform and lane class. A
-- counter_post_views re-read (prism/post-stats, lane watchlist) re-reads a post 42 chose, so it counts as
-- watchlist, never as the measured unbiased_counter lane its rows carry (DATA.md 3.2).
SELECT s.platform, s.lane_class, COUNT(*) n
FROM (SELECT po.platform, IF(IFNULL(po.series, '') = 'counter_post_views', 'watchlist', po.lane_class) lane_class
      FROM {core}.post_observations po
      JOIN {core}.post_items pi ON pi.post_id = po.post_id
      WHERE pi.item_id = @item_id AND po.market = @market
        AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d) s
GROUP BY s.platform, s.lane_class;

-- name: board
-- Rank-list and board reads of the item in the market over the same 14 days. A board-only item has no post
-- sightings but is measured in unbiased_rank. The X trends archive is a seed only and never counts.
SELECT COUNT(*) n FROM {core}.v_item_counter_daily_current c
WHERE c.item_id = @item_id AND c.market = @market AND c.unit IN ('rank', 'appearances')
  AND c.series != 'x_trends' AND c.obs_date BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d;

-- name: health
-- One row per day from @d minus 2 to @d that has health rows for the platform in the market: ok is TRUE when
-- every baseline series (unbiased_rank, panel, unbiased_counter) was valid that day. Search, placebo and watchlist
-- rows are never baseline (DATA.md 3.2), so they do not decide the day. The main series' own route counts too when
-- it names no platform (the culture desk panel, whose health rows carry none); @series_id is '' without one.
SELECT h.day, LOGICAL_AND(h.valid) ok
FROM {core}.v_collection_health_current h
WHERE h.market = @market AND h.day BETWEEN DATE_SUB(@d, INTERVAL 2 DAY) AND @d
  AND (h.platform = @platform
    OR (h.platform IS NULL AND CONCAT(@item_id, '|', h.market, '|', h.series, '|', h.protocol) = @series_id))
  AND h.lane_class IN ('unbiased_rank', 'panel', 'unbiased_counter')
GROUP BY h.day;

-- name: post_lanes
-- Which of the cited posts (@post_ids, comma separated) were sighted in the market in a measured post lane
-- (unbiased_rank or panel) over the 14 days to @d. Counters carry no posts.
SELECT DISTINCT po.post_id FROM {core}.post_observations po
WHERE po.post_id IN UNNEST(SPLIT(@post_ids, ',')) AND po.market = @market
  AND po.lane_class IN ('unbiased_rank', 'panel')
  AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d;

-- name: names
-- The item's current label and canonical key from the cultural map.
SELECT cm.label, cm.canonical_key FROM {core}.cultural_map cm
WHERE cm.item_id = @item_id AND cm.valid_to IS NULL;

-- name: post_tags
-- The stored hashtags of the cited posts (@post_ids, comma separated), read for paid-post markers.
SELECT ps.post_id, ps.hashtags FROM {core}.posts ps WHERE ps.post_id IN UNNEST(SPLIT(@post_ids, ','));

-- name: post_authors
-- The creator's display name of each cited post (@post_ids, comma separated), read to count one person who posts
-- under several handles once. creators holds a row per creator and platform; MAX gives one name per post.
SELECT ps.post_id, MAX(cr.display_name) display_name
FROM {core}.posts ps
JOIN {core}.creators cr ON cr.creator_id = ps.creator_id AND cr.platform = ps.platform
WHERE ps.post_id IN UNNEST(SPLIT(@post_ids, ','))
GROUP BY ps.post_id;
