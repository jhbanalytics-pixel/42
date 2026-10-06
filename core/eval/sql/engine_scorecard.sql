-- L2's weekly detection scorecard (core/detect/scorecard.py), read only: one run per market for the week starting
-- @week_start, run @run_id where it wrote the market (learn scores the run it has just written), else the latest.
-- Each metric is a JSON Figure {value, unit, query_id, run_id, result_hash, n, reason}.
SELECT s.market, s.run_id, s.precision, s.time_to_detect
FROM `ogilvy-trends-v2.intelligence_42_agent.engine_scorecard` AS s
WHERE s.week_start = @week_start
QUALIFY ROW_NUMBER() OVER (PARTITION BY s.market ORDER BY IFNULL(s.run_id = @run_id, FALSE) DESC, s.run_id DESC) = 1
