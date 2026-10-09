-- Near-duplicate input (N21, core/detect/neardup.py): the posts sighted in the 7 days to @d with a caption, one
-- row per post, and the largest near_dup_size post_enrichment already holds for each. Lanes as in tvf_item_window:
-- legacy, placebo and agent_live sightings are left out. Read only.
SELECT ps.post_id, ps.text, pe.near_dup_size
FROM (SELECT DISTINCT po.post_id
      FROM {core}.post_observations po
      WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
        AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')) o
JOIN {core}.posts ps ON ps.post_id = o.post_id
LEFT JOIN (SELECT post_id, MAX(near_dup_size) near_dup_size FROM {core}.post_enrichment GROUP BY post_id) pe
  ON pe.post_id = o.post_id
WHERE ps.text IS NOT NULL
ORDER BY ps.post_id
