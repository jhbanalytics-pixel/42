-- The current weekly_quality row per market for the week starting @week_start: the newest scored_at. Its counted
-- parts, questions and question_set_hash are the key change is compared on.
SELECT q.market, q.run_id, q.score, q.counted, q.questions, q.question_set_hash
FROM `ogilvy-trends-v2.intelligence_42_agent.weekly_quality` AS q
WHERE q.week_start = @week_start
QUALIFY ROW_NUMBER() OVER (PARTITION BY q.market ORDER BY q.scored_at DESC, q.run_id DESC) = 1
