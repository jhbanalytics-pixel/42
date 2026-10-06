-- Clusters for the discovery review (BUILD.md 2.2 check): every cluster dated @from_date to @to_date, both
-- inclusive, with its five most probable member posts cut to 280 characters, lowest post_id first on a tie.
-- discovery_review.review_sample draws the reviewer's sample from these rows.
WITH ranked AS (
  SELECT
    m.cluster_id,
    LEFT(COALESCE(p.text, ''), 280) AS excerpt,
    ROW_NUMBER() OVER (PARTITION BY m.cluster_id ORDER BY m.probability DESC, m.post_id) AS rn
  FROM `ogilvy-trends-v2.intelligence_42_core.clusters` AS k
  JOIN `ogilvy-trends-v2.intelligence_42_core.cluster_members` AS m
    ON m.cluster_id = k.cluster_id
  JOIN `ogilvy-trends-v2.intelligence_42_core.posts` AS p
    ON p.post_id = m.post_id AND p.post_date >= DATE_SUB(@from_date, INTERVAL 402 DAY)
  WHERE k.cluster_date BETWEEN @from_date AND @to_date
),
-- Aggregated here and joined back on cluster_id, because BigQuery refuses a correlated subquery over another table.
excerpts AS (
  SELECT r.cluster_id, ARRAY_AGG(r.excerpt ORDER BY r.rn) AS posts
  FROM ranked AS r
  WHERE r.rn <= 5
  GROUP BY r.cluster_id
)
SELECT
  k.cluster_id, k.cluster_date, k.market, k.item_id, k.match_kind, k.label, k.keywords, e.posts
FROM `ogilvy-trends-v2.intelligence_42_core.clusters` AS k
LEFT JOIN excerpts AS e
  ON e.cluster_id = k.cluster_id
WHERE k.cluster_date BETWEEN @from_date AND @to_date
ORDER BY k.market, k.cluster_id
