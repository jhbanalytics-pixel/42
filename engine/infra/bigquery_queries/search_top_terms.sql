-- Steady-state top search terms per market from the Google Trends public dataset.
-- Source: bigquery-public-data.google_trends.international_top_terms
-- Complements search_velocity_terms.sql (which surfaces only rising/spiking terms).
-- Captures cultural staples (amapiano, jollof, mpesa) that sit in the top chart
-- consistently and would never trip a "rising" threshold.
--
-- Schema note: the public table is DAY-partitioned on `refresh_date`. We filter
-- on `refresh_date` (not `week`) so partition pruning fires and scan cost stays
-- bounded (~2 GB per market per call at days=7, vs ~9 GB if filtered on `week`).
--
-- Dedup note: a term can appear across multiple weeks. We collapse each term
-- to its most-recent-week occurrence (best rank as the tie-breaker) so the
-- caller sees one row per distinct term, biased toward the latest snapshot.
--
-- Parameters: @country_code (STRING), @days (INT64), @lim (INT64)

WITH ranked AS (
  SELECT
    country_code,
    country_name,
    refresh_date,
    week,
    rank,
    term,
    score,
    ROW_NUMBER() OVER (PARTITION BY term ORDER BY week DESC, rank ASC) AS row_num
  FROM `bigquery-public-data.google_trends.international_top_terms`
  WHERE country_code = @country_code
    AND refresh_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
)
SELECT
  country_code,
  country_name,
  refresh_date,
  week,
  rank,
  term,
  score
FROM ranked
WHERE row_num = 1
ORDER BY rank ASC, week DESC
LIMIT @lim;
