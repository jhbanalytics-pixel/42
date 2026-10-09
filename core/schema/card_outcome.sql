/* Card and hold outcomes (core/eval/card_outcome.py, METHOD-GAPS section 7): one row per run date, market, item and
   kind (published card or held item), with the state at t and at t + 3, t + 7 and t + 14 and the class of each
   later day. A future additive table: this file is not applied anywhere yet. Append only; the newest scored_at per
   run_date, market, item_id, kind, hold_reason and definition is current, because t + 7 and t + 14 are pending
   until their days have a good detect run. definition names what held means (active28_by_base_v3: the product's
   active28 set, a Recurring or Seasonal overlay held and split confirmed or not by base_state, on a day when
   every significant or jumping series is on a measured lane), so a change of meaning never rewrites old rows.
   held_any is the earlier definition, held plus On the boards and New to 42. Observation only: no gate,
   threshold, card or hold reads it. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.card_outcome` (
  run_date DATE NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL, kind STRING NOT NULL,
  definition STRING NOT NULL,
  surface STRING, hold_reason STRING, rule STRING, rank INT64, stratum STRING,
  state_t STRING, state_t3 STRING, state_t7 STRING, state_t14 STRING,
  class_t3 STRING, class_t7 STRING, class_t14 STRING,
  eff_state_t3 STRING, eff_state_t7 STRING, eff_state_t14 STRING,
  outcome_t3 STRING, outcome_t7 STRING, outcome_t14 STRING,
  trust_t3 BOOL, trust_t7 BOOL, trust_t14 BOOL, backtest_t3 BOOL, backtest_t7 BOOL, backtest_t14 BOOL,
  any_t3 BOOL, any_t7 BOOL, any_t14 BOOL,
  measured BOOL, held BOOL, held_confirmed BOOL, held_trust BOOL, held_backtest BOOL, held_any BOOL, collapsed BOOL,
  run_id STRING, scored_at TIMESTAMP NOT NULL)
PARTITION BY run_date CLUSTER BY market, kind;
