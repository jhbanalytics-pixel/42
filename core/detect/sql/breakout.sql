-- Creator-breakout input (BUILD.md 2.13), two read-only queries that core/detect/breakout.py runs in order.
-- Query 1: every post in the 180 days to @d by each platform and creator with a post published in the 7 days
-- to @d, with two first readings taken on or before @d, never from legacy, placebo, agent_live (as in
-- tvf_item_window, DATA.md 3.6) or counter_post_views sightings
-- (counter_post_views re-reads posts 42 chose, so it is Peaking and Fading context only, DATA.md 3.2):
-- first_views, the post's first reading with views in any other lane, used when the post is judged; and
-- base_views, its first reading with views in a measured lane (unbiased_rank, panel) or on the creator-profile
-- route of SOURCES.md row 10, used when the post is a baseline for a later one. The creators table gives the
-- co-action flag (any row for the creator_id) and the handle and home market on the post's platform.
WITH w AS (
  SELECT DISTINCT ps.platform, ps.creator_id
  FROM {core}.posts ps
  WHERE ps.post_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND ps.creator_id IS NOT NULL AND ps.published_at IS NOT NULL),
p AS (
  SELECT ps.post_id, ps.platform, ps.creator_id, ps.creator_tier_at_post tier, ps.published_at,
    ps.post_date >= DATE_SUB(@d, INTERVAL 6 DAY) in_window, ps.post_date, ps.geo_market, ps.geo_confidence
  FROM {core}.posts ps
  JOIN w ON w.platform = ps.platform AND w.creator_id = ps.creator_id
  WHERE ps.post_date BETWEEN DATE_SUB(@d, INTERVAL 180 DAY) AND @d AND ps.published_at IS NOT NULL),
o AS (
  SELECT po.post_id, po.observed_at, po.views,
    po.lane_class IN ('unbiased_rank', 'panel') OR IFNULL(po.route, '') = 'tiktok/profile/videos' measured
  FROM {core}.post_observations po
  WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 180 DAY) AND @d
    AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
    AND IFNULL(po.series, '') != 'counter_post_views' AND po.views IS NOT NULL
    AND po.post_id IN (SELECT p.post_id FROM p)),
r AS (
  SELECT o.post_id, o.views, o.observed_at FROM o
  QUALIFY ROW_NUMBER() OVER (PARTITION BY o.post_id ORDER BY o.observed_at, o.views) = 1),
b AS (
  SELECT o.post_id, o.views, o.observed_at FROM o WHERE o.measured
  QUALIFY ROW_NUMBER() OVER (PARTITION BY o.post_id ORDER BY o.observed_at, o.views) = 1),
f AS (
  SELECT cr.creator_id, MAX(IFNULL(cr.coord_score, 0)) coord_score
  FROM {core}.creators cr GROUP BY cr.creator_id),
h AS (
  SELECT cr.creator_id, cr.platform, MIN(cr.handle) handle, MIN(cr.home_market) home_market
  FROM {core}.creators cr GROUP BY cr.creator_id, cr.platform)
SELECT p.post_id, p.platform, p.creator_id, p.tier, p.published_at, p.in_window, p.post_date, p.geo_market,
  p.geo_confidence,
  r.views first_views, r.observed_at first_read_at, b.views base_views, b.observed_at base_read_at,
  h.handle, h.home_market, IFNULL(f.coord_score, 0) coord_score
FROM p
LEFT JOIN r ON r.post_id = p.post_id
LEFT JOIN b ON b.post_id = p.post_id
LEFT JOIN f ON f.creator_id = p.creator_id
LEFT JOIN h ON h.creator_id = p.creator_id AND h.platform = p.platform;

-- Query 2: the markets each post published in the 7 days to @d was sighted in by @d, with its sound and format
-- items. Lanes as in tvf_item_window: legacy, placebo and agent_live sightings place no post in a market.
-- Items with status generic are left out here; breakout.py also checks the stoplist.
SELECT DISTINCT po.post_id, po.market, pi.item_id, cm.kind, cm.canonical_key
FROM {core}.post_observations po
JOIN {core}.posts ps ON ps.post_id = po.post_id
JOIN {core}.post_items pi ON pi.post_id = po.post_id
JOIN {core}.cultural_map cm ON cm.item_id = pi.item_id AND cm.valid_to IS NULL
WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  AND po.market IN ('ZA', 'NG', 'KE')
  AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  AND IFNULL(po.series, '') != 'counter_post_views'
  AND ps.post_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  AND cm.kind IN ('sound', 'format') AND IFNULL(cm.status, '') != 'generic'
