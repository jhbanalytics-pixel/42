-- name: plan
WITH ready AS (
  SELECT r.run_id, r.counts
  FROM {agent}.runs r
  WHERE r.run_date = @run_date AND r.stage = 'understand_cluster_plan' AND r.status = 'ready'
    AND JSON_VALUE(r.counts, '$.market') = @market
  QUALIFY ROW_NUMBER() OVER (ORDER BY r.started_at, r.run_id) = 1
), batches AS (
  SELECT r.run_id, r.counts, CAST(JSON_VALUE(r.counts, '$.batch_index') AS INT64) batch_index
  FROM {agent}.runs r JOIN ready p ON p.run_id = r.run_id
  WHERE r.run_date = @run_date AND r.stage = 'understand_cluster_plan' AND r.status = 'batch'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY r.run_id, batch_index ORDER BY r.started_at) = 1
), written AS (
  SELECT COUNT(*) n, COALESCE(ARRAY_AGG(k.cluster_id IGNORE NULLS), []) written_ids
  FROM {core}.clusters k WHERE k.cluster_date = @run_date AND k.market = @market
)
SELECT w.n, w.written_ids, p.run_id plan_id, p.counts checkpoint, b.batch_index, b.counts batch
FROM written w LEFT JOIN ready p ON TRUE LEFT JOIN batches b ON b.run_id = p.run_id
ORDER BY b.batch_index;

-- name: members
SELECT k.cluster_id, k.cluster_date, k.market, k.item_id, m.post_id
FROM {core}.clusters k
LEFT JOIN {core}.cluster_members m ON m.cluster_id = k.cluster_id
WHERE k.cluster_id = @cluster_id
ORDER BY m.post_id;

-- name: content
SELECT ids post_id, p.post_id IS NOT NULL record_exists,
  EXISTS(SELECT 1 FROM {core}.v_suppressed_creators s WHERE s.creator_id = p.creator_id) suppressed,
  p.platform, p.creator_id, p.url, p.published_at, p.geo_market, p.geo_confidence, p.geo_source,
  IF(EXISTS(SELECT 1 FROM {core}.v_suppressed_creators s WHERE s.creator_id = p.creator_id),
     NULL, COALESCE(NULLIF(p.text, ''), p.transcript)) quote_text,
  CURRENT_TIMESTAMP() content_read_at
FROM UNNEST(JSON_VALUE_ARRAY(@post_ids)) ids
LEFT JOIN {core}.posts p ON p.post_id = ids AND p.post_date >= DATE_SUB(@run_date, INTERVAL 402 DAY)
ORDER BY ids;
