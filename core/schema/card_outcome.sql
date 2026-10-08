/* Card and hold outcomes (core/eval/card_outcome.py, METHOD-GAPS section 7): one row per run date, market, item and
   kind (published card or held item), with the state at t and at t + 3, t + 7 and t + 14 and the outcome of each
   later day. A future additive table: this file is not applied anywhere yet. Append only; the newest scored_at per
   run_date, market, item_id, kind and hold_reason is current, because t + 7 and t + 14 are pending until their
   days have a good detect run. Observation only: no gate, threshold, card or hold reads it. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.card_outcome` (
  run_date DATE NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL, kind STRING NOT NULL,
  surface STRING, hold_reason STRING, rule STRING, rank INT64, stratum STRING,
  state_t STRING, state_t3 STRING, state_t7 STRING, state_t14 STRING,
  outcome_t3 STRING, outcome_t7 STRING, outcome_t14 STRING,
  measured BOOL, held BOOL, collapsed BOOL,
  run_id STRING, scored_at TIMESTAMP NOT NULL)
PARTITION BY run_date CLUSTER BY market, kind;
