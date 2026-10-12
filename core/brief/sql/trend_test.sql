-- The trend test (contract TREND-TEST-v1). Read only: core/brief/trend_test.py runs these statements after a brief run has
-- finished and computes every figure and the label in Python. Nothing here is read by a gate, the ranking or a display.

-- name: briefs
-- The stored brief of the day, as Today reads it: one row per market of the brief run that finished ok.
SELECT b.market, b.run_id, b.published_at, b.status, TO_JSON_STRING(b.payload) payload
FROM {agent}.v_briefs_current b
WHERE b.brief_date = @d
ORDER BY b.market;

-- name: population
-- The posts of the named items in one market for the seven days ending at the brief date: a post observed in the market
-- in the window on a lane that is not one of 42's own searches (@excluded_lanes), no later than the brief's publication
-- (@cutoff), linked to the item as the locality members statement links it (tvf_post_items), and not by a suppressed
-- creator. The creator's handle and verified flag are one creators row for the id. @item_ids and @excluded_lanes are
-- comma-joined strings: an item id never holds a comma (core/brief/trend_test.py refuses one).
WITH seen AS (
  SELECT DISTINCT po.post_id
  FROM {core}.post_observations po
  WHERE po.market = @market
    AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.observed_at <= @cutoff
    AND IFNULL(po.lane, '') NOT IN UNNEST(SPLIT(@excluded_lanes, ','))),
members AS (
  SELECT DISTINCT pi.item_id, s.post_id
  FROM seen s
  JOIN {core}.tvf_post_items(@d) pi ON pi.post_id = s.post_id AND (pi.market IS NULL OR pi.market = @market)
  WHERE pi.item_id IN UNNEST(SPLIT(@item_ids, ','))),
who AS (
  SELECT cr.creator_id, cr.handle, cr.verified
  FROM {core}.creators cr
  WHERE cr.creator_id IN (SELECT ps.creator_id FROM {core}.posts ps JOIN members m ON m.post_id = ps.post_id
                          WHERE ps.post_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cr.creator_id ORDER BY cr.handle) = 1)
SELECT m.item_id, ps.post_id, ps.platform, ps.creator_id, w.handle, w.verified, ps.creator_tier_at_post tier,
  ps.post_date, ps.text, ps.hashtags, ps.sound_id, ps.url, ps.geo_market
FROM members m
JOIN {core}.posts ps ON ps.post_id = m.post_id
LEFT JOIN who w ON w.creator_id = ps.creator_id
WHERE ps.post_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = ps.creator_id)
ORDER BY m.item_id, ps.post_id;

-- name: outlet_links
-- The addresses of the market's news feed posts in the same window and before the same cutoff. Their hosts are the
-- outlet hosts: a post that links to one shares an outlet's story rather than making its own version.
SELECT DISTINCT ps.url
FROM {core}.post_observations po
JOIN {core}.posts ps ON ps.post_id = po.post_id
WHERE po.market = @market
  AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  AND po.observed_at <= @cutoff
  AND ps.platform = 'news'
  AND ps.post_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  AND ps.url IS NOT NULL
ORDER BY ps.url;
