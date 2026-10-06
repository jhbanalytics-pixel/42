-- One weekly_quality row. parts and context arrive as JSON text; nullable values are cast to their column type.
INSERT INTO `ogilvy-trends-v2.intelligence_42_agent.weekly_quality`
  (week_start, week_end, market, run_id, scored_at, score, counted, change, previous_run_id, questions,
   question_set_hash, parts, context, notes)
VALUES (@week_start, @week_end, @market, @run_id, @scored_at, CAST(@score AS FLOAT64), @counted,
  CAST(@change AS FLOAT64), CAST(@previous_run_id AS STRING), CAST(@questions AS INT64),
  CAST(@question_set_hash AS STRING), PARSE_JSON(@parts), PARSE_JSON(@context), @notes)
