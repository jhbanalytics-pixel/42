-- Enrichment batch jobs still open (BUILD.md 2.1). Parameter: @run_date, DATE.
-- A job is keyed by its suffix, which names its enrich_batch_in_ and enrich_batch_out_ tables and its display name
-- f42-enrich-<suffix>. Before submitting, a run books the estimate in an understand_spend row (what is
-- enrich_batch_estimate:<suffix>) and appends a runs row of its own (stage understand_batch, status submitted,
-- enrich.BATCH_ROW_SQL) with the suffix and a NULL enrich_batch_job_id. After the submit it appends a second such
-- row with the job id, and at the end writes both into its understand row's counts. A run killed between the submit
-- and the second row leaves the job id NULL here, and the collector finds the job by its display name. A suffix whose
-- submit raised, or that Vertex has no job for, gets a row with status failed (enrich.BATCH_FAILED_SQL) and is never
-- open again. The estimate is read only where it was booked: the spend row, or the understand row (which carries it
-- in model_usd when its booking failed, and in rows written before the spend ledger). The collecting run adds actual
-- minus it. A job whose estimate was booked nowhere has none, and its whole actual spend lands on the collecting run.
-- run_date is the submitting run's day, which tells the collector whether it books on the same day.
-- A run that collects, fails or expires a job appends an understand_batch row with status closed right after it
-- books the job's correction (enrich.BATCH_CLOSED_SQL), and lists its id in enrich_batch_closed in its understand
-- row. Either closes it: the closed row does so at once, so a run killed before its understand row never collects
-- the job again. A job is open when a run in the last 14 days recorded it and neither row has closed it. A job
-- expires 48 hours after submission and is flagged stale at 7 days, so 14 days leaves room to see a stale job end
-- and close it.

WITH submitted AS (
  SELECT
    JSON_VALUE(r.counts, '$.enrich_batch_job_id') AS job_id,
    JSON_VALUE(r.counts, '$.enrich_batch_suffix') AS suffix,
    IF(r.stage = 'understand', SAFE_CAST(JSON_VALUE(r.counts, '$.enrich_batch_estimate_usd') AS FLOAT64), NULL)
      AS estimate_usd,
    r.stage = 'understand_batch' AND r.status = 'failed' AS failed,
    r.stage = 'understand_batch' AND r.status = 'closed' AS closed,
    r.run_date,
    r.started_at
  FROM `ogilvy-trends-v2.intelligence_42_agent.runs` AS r
  WHERE r.stage IN ('understand', 'understand_batch')
    AND r.run_date >= DATE_SUB(@run_date, INTERVAL 14 DAY)
),
booked AS (
  SELECT
    JSON_VALUE(r.counts, '$.what') AS what,
    MAX(SAFE_CAST(JSON_VALUE(r.counts, '$.model_usd') AS FLOAT64)) AS estimate_usd
  FROM `ogilvy-trends-v2.intelligence_42_agent.runs` AS r
  WHERE r.stage = 'understand_spend'
    AND r.run_date >= DATE_SUB(@run_date, INTERVAL 14 DAY)
    AND STARTS_WITH(JSON_VALUE(r.counts, '$.what'), 'enrich_batch_estimate:')
  GROUP BY what
),
jobs AS (
  SELECT
    MAX(s.job_id) AS job_id,
    s.suffix,
    COALESCE(MAX(s.estimate_usd), MAX(b.estimate_usd)) AS estimate_usd,
    MIN(s.run_date) AS run_date,
    MIN(s.started_at) AS submitted_at,
    LOGICAL_OR(s.failed) AS failed,
    LOGICAL_OR(s.closed) AS closed
  FROM submitted AS s
  LEFT JOIN booked AS b
    ON b.what = CONCAT('enrich_batch_estimate:', s.suffix)
  WHERE s.suffix IS NOT NULL
  GROUP BY s.suffix
)
SELECT
  j.job_id,
  j.suffix,
  j.estimate_usd,
  j.run_date,
  j.submitted_at
FROM jobs AS j
WHERE NOT j.failed
  AND NOT j.closed
  AND NOT EXISTS (
    SELECT 1
    FROM `ogilvy-trends-v2.intelligence_42_agent.runs` AS d
    CROSS JOIN UNNEST(JSON_VALUE_ARRAY(d.counts, '$.enrich_batch_closed')) AS closed
    WHERE d.stage = 'understand' AND closed = j.job_id
  )
ORDER BY j.submitted_at, j.suffix
