-- The items detect holds eligible, for enrichment's order (enrich.py _eligible_first). Parameter: @run_date, DATE.
-- The same state enrich_select.sql's eligible_posts reads: each market's newest good detect run in
-- v_item_state_current (the view the brief's candidates read) dated within the 7 days up to @run_date.

WITH latest_state AS (
  SELECT s.market, s.item_id, s.eligible
  FROM `ogilvy-trends-v2.intelligence_42_core.v_item_state_current` AS s
  WHERE s.metric_date BETWEEN DATE_SUB(@run_date, INTERVAL 7 DAY) AND @run_date
  QUALIFY s.metric_date = MAX(s.metric_date) OVER (PARTITION BY s.market)
)
SELECT DISTINCT ls.market, ls.item_id
FROM latest_state AS ls
WHERE ls.eligible IS TRUE
