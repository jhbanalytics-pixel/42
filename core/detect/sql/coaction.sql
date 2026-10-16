-- Co-action input (TRUST.md section 5, BUILD.md 2.11): posts first sighted in each market in the 30 days to @d,
-- one row per post, market and item, with the fields core/detect/coaction.py compares. Lanes as in
-- tvf_item_window: legacy, placebo and agent_live sightings are left out. Read only.
WITH o AS (
  SELECT po.post_id, po.market, MIN(po.observed_date) first_day
  FROM {core}.post_observations po
  WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 29 DAY) AND @d
    AND po.lane_class != 'legacy' AND po.lane NOT IN ('placebo', 'agent_live')
  GROUP BY po.post_id, po.market)
SELECT o.post_id, o.market, o.first_day, ps.creator_id, ps.published_at, ps.text, ps.hashtags,
  pi.item_id, cm.kind, cm.canonical_key
FROM o
JOIN {core}.posts ps ON ps.post_id = o.post_id
JOIN {core}.post_items pi ON pi.post_id = o.post_id
JOIN {core}.cultural_map cm ON cm.item_id = pi.item_id AND cm.valid_to IS NULL
WHERE ps.creator_id IS NOT NULL
