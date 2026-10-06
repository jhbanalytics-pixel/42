-- Rising search terms per market from the Google Trends public dataset.
-- Source: bigquery-public-data.google_trends.international_top_rising_terms
-- Maps percent_gain to search_velocity_score (9th scoring signal in configs/scoring.yaml).
--
-- Note: the table has one row per (country_code, week, rank, term). Multiple rows can
-- share the same term across different weeks. This query deduplicates to the top-ranked
-- occurrence of each distinct term from the most recent available week.
--
-- Parameters: @country_code (STRING), @days (INT64), @limit (INT64)

WITH ranked AS (
  SELECT
    country_code,
    country_name,
    refresh_date,
    week,
    rank,
    term,
    score,
    percent_gain,
    -- Log-scale, not a flat divisor. Rising-terms percent_gain runs roughly
    -- 250 to 20000+, so percent_gain / 1000 clips about 80 percent of terms
    -- to 1.0 and the signal loses all gradient. LN spreads the real range
    -- across about 0.55 to 1.0 so a genuine breakout outscores a merely-rising
    -- term instead of every term reading as maxed out.
    LEAST(LN(1.0 + percent_gain) / LN(1.0 + 25000.0), 1.0) AS search_velocity_score,
    ROW_NUMBER() OVER (PARTITION BY term ORDER BY week DESC, rank ASC) AS row_num
  FROM `bigquery-public-data.google_trends.international_top_rising_terms`
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
  score,
  percent_gain,
  search_velocity_score
FROM ranked
WHERE row_num = 1
ORDER BY week DESC, rank ASC
LIMIT @limit;
