-- Save every original batch before publishing its ready marker. No cluster write precedes that marker.
INSERT INTO `ogilvy-trends-v2.intelligence_42_agent.runs`
  (run_id, stage, run_date, status, started_at, finished_at, counts)
SELECT @plan_id, 'understand_cluster_plan', @run_date, IF(@batch_index < 0, 'ready', 'batch'),
  CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(),
  JSON_OBJECT('market', @market, 'batch_index', @batch_index, 'batch_count', @batch_count,
    'summary', PARSE_JSON(@summary), 'map_rows', PARSE_JSON(@map_rows),
    'cluster_rows', PARSE_JSON(@cluster_rows), 'member_rows', PARSE_JSON(@member_rows));
