-- The cultural_map topics a clustering run can match (BUILD.md 2.2, DATA.md section 4), with what the match votes
-- read. Parameter: @run_date DATE.
-- Every current topic row with a centroid is a candidate, whatever its status, so a rejected topic that comes
-- back is matched and stays rejected instead of reappearing as new. If two rows of one item are still open, the
-- newest valid_from is read.
-- The profile comes from the item's clusters in the 90 days to @run_date: keywords from its latest cluster, and
-- its ten commonest hashtags (lower case, no #), ten commonest sounds and fifty commonest creators across those
-- clusters' member posts. An item with no cluster in that window has NULL lists (BigQuery returns them to the
-- client as empty arrays) and is matched on its centroid, its last_seen and nothing else.
WITH cur AS (
  SELECT
    cm.item_id, cm.kind, cm.canonical_key, cm.label, cm.aliases, cm.parent_item_id, cm.centroid, cm.first_seen,
    cm.first_seen_market, cm.first_seen_platform, cm.last_seen, cm.recurrences, cm.lifecycle, cm.status,
    cm.rejected_until
  FROM `ogilvy-trends-v2.intelligence_42_core.cultural_map` AS cm
  WHERE cm.valid_to IS NULL AND cm.kind = 'topic' AND ARRAY_LENGTH(cm.centroid) > 0
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cm.item_id ORDER BY cm.valid_from DESC) = 1
),
recent AS (
  SELECT k.item_id, k.cluster_id, k.cluster_date, k.keywords
  FROM `ogilvy-trends-v2.intelligence_42_core.clusters` AS k
  WHERE k.cluster_date BETWEEN DATE_SUB(@run_date, INTERVAL 90 DAY) AND @run_date
    AND k.item_id IN (SELECT c.item_id FROM cur AS c)
),
latest AS (
  SELECT r.item_id, r.keywords
  FROM recent AS r
  QUALIFY ROW_NUMBER() OVER (PARTITION BY r.item_id ORDER BY r.cluster_date DESC, r.cluster_id DESC) = 1
),
member_posts AS (
  SELECT r.item_id, p.creator_id, p.hashtags, p.sound_id
  FROM recent AS r
  JOIN `ogilvy-trends-v2.intelligence_42_core.cluster_members` AS m
    ON m.cluster_id = r.cluster_id
  JOIN `ogilvy-trends-v2.intelligence_42_core.posts` AS p
    ON p.post_id = m.post_id AND p.post_date >= DATE_SUB(@run_date, INTERVAL 492 DAY)
),
facets AS (
  SELECT mp.item_id, 'hashtag' AS facet, LOWER(LTRIM(h, '#')) AS value
  FROM member_posts AS mp
  CROSS JOIN UNNEST(mp.hashtags) AS h
  UNION ALL
  SELECT mp.item_id, 'sound' AS facet, mp.sound_id AS value
  FROM member_posts AS mp
  WHERE mp.sound_id IS NOT NULL
  UNION ALL
  SELECT mp.item_id, 'creator' AS facet, mp.creator_id AS value
  FROM member_posts AS mp
  WHERE mp.creator_id IS NOT NULL
),
ranked AS (
  SELECT
    f.item_id, f.facet, f.value,
    ROW_NUMBER() OVER (PARTITION BY f.item_id, f.facet ORDER BY COUNT(*) DESC, f.value) AS rn
  FROM facets AS f
  GROUP BY f.item_id, f.facet, f.value
),
-- One list per item and facet, commonest first: the top 50 creators and the top 10 of the others. The lists are
-- aggregated here and joined back on item_id, because BigQuery refuses a correlated subquery over another table.
lists AS (
  SELECT x.item_id, x.facet, ARRAY_AGG(x.value ORDER BY x.rn) AS vals
  FROM ranked AS x
  WHERE x.rn <= IF(x.facet = 'creator', 50, 10)
  GROUP BY x.item_id, x.facet
)
SELECT
  c.item_id, c.kind, c.canonical_key, c.label, c.aliases, c.parent_item_id, c.centroid, c.first_seen,
  c.first_seen_market, c.first_seen_platform, c.last_seen, c.recurrences, c.lifecycle, c.status, c.rejected_until,
  l.keywords,
  ht.vals AS hashtags,
  sd.vals AS sounds,
  cr.vals AS creators
FROM cur AS c
LEFT JOIN latest AS l
  ON l.item_id = c.item_id
LEFT JOIN lists AS ht
  ON ht.item_id = c.item_id AND ht.facet = 'hashtag'
LEFT JOIN lists AS sd
  ON sd.item_id = c.item_id AND sd.facet = 'sound'
LEFT JOIN lists AS cr
  ON cr.item_id = c.item_id AND cr.facet = 'creator'
ORDER BY c.item_id
