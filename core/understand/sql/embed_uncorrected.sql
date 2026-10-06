-- Today's embed ceilings that no correction followed, in USD. Read-only. Parameter: @day, DATE, today in
-- Africa/Johannesburg, the day embed.book_spend books every ceiling and correction on.
-- post_enrichment holds no run stamp, so what a window that raised really spent cannot be read back, and its
-- ceiling stays booked. Each retry books a ceiling of its own, so run_embed reads this before every window and
-- sends none once it reaches one window's ceiling (USD 15.36).
-- A spend row's run_id is the run's own id, "-spend-" and eight hex characters, so the last 15 characters are
-- cut to find the run. A run stops at its first window that raises or is killed, so a run holding more ceilings
-- than corrections left its last ceiling uncorrected. A window whose true spend equals its ceiling books no
-- correction, and counts here as uncorrected, which errs on the side of spending less.

WITH spend AS (
  SELECT
    LEFT(r.run_id, LENGTH(r.run_id) - 15) AS run,
    JSON_VALUE(r.counts, '$.what') AS what,
    CAST(JSON_VALUE(r.counts, '$.model_usd') AS FLOAT64) AS usd,
    r.started_at
  FROM `ogilvy-trends-v2.intelligence_42_agent.runs` AS r
  WHERE r.stage = 'understand_spend'
    AND r.run_date = @day
    AND r.run_id LIKE '%-spend-%'
    AND JSON_VALUE(r.counts, '$.what') IN ('embed_ceiling', 'embed_correction')
),
per_run AS (
  SELECT
    s.run,
    COUNTIF(s.what = 'embed_ceiling') AS ceilings,
    COUNTIF(s.what = 'embed_correction') AS corrections
  FROM spend AS s
  GROUP BY s.run
),
last_ceiling AS (
  SELECT s.run, s.usd
  FROM spend AS s
  WHERE s.what = 'embed_ceiling'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY s.run ORDER BY s.started_at DESC) = 1
)
SELECT COALESCE(SUM(l.usd), 0) AS usd
FROM per_run AS p
JOIN last_ceiling AS l
  ON l.run = p.run
WHERE p.ceilings > p.corrections
