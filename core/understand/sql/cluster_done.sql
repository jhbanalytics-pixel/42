-- Read the first fully saved plan and existing cluster ids for one market-day.
WITH ready AS (
  SELECT r.run_id, r.counts
  FROM `ogilvy-trends-v2.intelligence_42_agent.runs` AS r
  WHERE r.run_date = @run_date AND r.stage = 'understand_cluster_plan' AND r.status = 'ready'
    AND JSON_VALUE(r.counts, '$.market') = @market
  QUALIFY ROW_NUMBER() OVER (ORDER BY r.started_at, r.run_id) = 1
), batches AS (
  SELECT r.run_id, r.counts, CAST(JSON_VALUE(r.counts, '$.batch_index') AS INT64) AS batch_index
  FROM `ogilvy-trends-v2.intelligence_42_agent.runs` AS r
  JOIN ready AS p ON p.run_id = r.run_id
  WHERE r.run_date = @run_date AND r.stage = 'understand_cluster_plan' AND r.status = 'batch'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY r.run_id, batch_index ORDER BY r.started_at) = 1
), written AS (
  SELECT COUNT(*) AS n, COALESCE(ARRAY_AGG(k.cluster_id IGNORE NULLS), []) AS written_ids
  FROM `ogilvy-trends-v2.intelligence_42_core.clusters` AS k
  WHERE k.cluster_date = @run_date AND k.market = @market
)
SELECT w.n, w.written_ids, p.counts AS checkpoint, b.batch_index, b.counts AS batch
FROM written AS w
LEFT JOIN ready AS p ON TRUE
LEFT JOIN batches AS b ON b.run_id = p.run_id
ORDER BY b.batch_index;
