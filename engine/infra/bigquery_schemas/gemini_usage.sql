-- Vertex Gemini token-usage ledger: the single basis the cost watchdog reads.
--
-- The six existing consumers write aggregate rows by
-- (trend_date, consumer, market, gemini_model). dynamic_signal_summary and
-- open_question_answer write immutable per-call deltas with run, stage, and
-- zero-based call metadata. Nullable event columns preserve old row behavior.
--
-- Three consumers (trend_analysis, daily_summary, seed_insights) also carry
-- prompt_tokens / completion_tokens / gemini_model on the rows they write, but
-- those columns are each stage's record of what its own output cost, not the
-- cost report. scripts/vertex_cost_watchdog.py stopped reading them on
-- 2026-08-24, because summing three tables plus this ledger plus a hardcoded
-- reconcile estimate hid two faults at once: comment_sentiment wrote no row
-- here at all, and seed_insights stamps ONE call's tokens onto EVERY seed row,
-- so a SUM over that table priced a single call three times over (measured
-- 533,373 prompt tokens against a true 177,791 for 2026-07-25..08-24).
--
-- Writers live in src/utils/gemini_usage.py, and the declared consumer set is
-- USAGE_CONSUMERS in that module. The watchdog imports that same tuple rather
-- than keeping a second list, so a stage cannot bill and go uncounted.
--
-- Not everything Vertex bills fits a token ledger.
-- src/enrichment/embedding_classifier.py calls embed_content, priced per
-- billable CHARACTER, so it stays out and is named in the watchdog's
-- discrepancy note against the billing export instead.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.gemini_usage` (
  usage_id STRING NOT NULL,
  trend_date DATE NOT NULL,
  run_id STRING,
  stage STRING,
  call_index INT64,
  -- Stage that fired the calls; see USAGE_CONSUMERS in src/utils/gemini_usage.py.
  consumer STRING NOT NULL,
  -- Market the calls ran for; NULL when the stage is not per-market.
  market STRING,
  -- Exact model id the calls ran against, so a window spanning a model
  -- cutover prices each side correctly.
  gemini_model STRING,
  -- Number of Gemini calls folded into this row.
  calls INT64,
  prompt_tokens INT64,
  completion_tokens INT64,
  recorded_at TIMESTAMP
)
PARTITION BY trend_date
CLUSTER BY consumer
OPTIONS (
  description = 'Vertex Gemini token usage. Existing consumers retain aggregate rows; dynamic signal summaries and open question answers use immutable per-call deltas.'
);
