-- Save every original batch before publishing its ready marker. No cluster write precedes that marker.
-- The parameters are JSON text holding centroid components and membership probabilities. PARSE_JSON in its default
-- exact mode refuses a number it cannot reproduce from the double it parses to, and on 9 Oct 2026 that ended the
-- clustering of every market with "cannot round-trip through string representation". 'round' takes every number.
INSERT INTO `ogilvy-trends-v2.intelligence_42_agent.runs`
  (run_id, stage, run_date, status, started_at, finished_at, counts)
SELECT @plan_id, 'understand_cluster_plan', @run_date, IF(@batch_index < 0, 'ready', 'batch'),
  CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(),
  JSON_OBJECT('market', @market, 'batch_index', @batch_index, 'batch_count', @batch_count,
    'summary', PARSE_JSON(@summary, wide_number_mode => 'round'),
    'map_rows', PARSE_JSON(@map_rows, wide_number_mode => 'round'),
    'cluster_rows', PARSE_JSON(@cluster_rows, wide_number_mode => 'round'),
    'member_rows', PARSE_JSON(@member_rows, wide_number_mode => 'round'));
