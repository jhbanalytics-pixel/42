-- The weekly quality score (core/eval/quality_score.py): one row per market (ALL, ZA, NG, KE) per run, the score
-- from 0 to 100 and its parts, each a Figure {value, unit, query_id, result_hash, row_count, n, minimum, points,
-- insufficient, reason}. Append only: a rerun adds rows with a new run_id, and the newest scored_at per week and
-- market is current. score is NULL when no part had enough data. change is NULL unless last week's current row,
-- previous_run_id, counted the same parts and its replay had the same questions and question_set_hash.
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.weekly_quality` (
  week_start DATE NOT NULL, week_end DATE NOT NULL, market STRING NOT NULL, run_id STRING NOT NULL,
  scored_at TIMESTAMP, score FLOAT64, counted STRING, change FLOAT64, previous_run_id STRING, questions INT64,
  question_set_hash STRING, parts JSON, context JSON, notes STRING)
PARTITION BY week_start
