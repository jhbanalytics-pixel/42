/* The trend test in shadow (contract TREND-TEST-v1, core/brief/trend_test.py). One row per brief run, market and item
   for every candidate and published card: the label TREND, NEWS, MIXED or UNKNOWN, the reason, and every input figure,
   so a later cut-off can be replayed on the stored figures without reading the posts again. A future additive table:
   this file is NOT in core/schema/apply.py SQL_FILES and is applied only by the release that names it in its durable
   effects manifest. Append only and never updated; a change of rule appends rows under a new rule_version. The name
   ends in _private so the Ask SQL tool refuses it. Observation only: no gate, ranking, payload or display reads it. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.trend_test_private` (
  run_date DATE NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL, brief_run_id STRING NOT NULL,
  rule_version STRING NOT NULL, schema_version INT64 NOT NULL, computed_at TIMESTAMP NOT NULL,
  population_cutoff TIMESTAMP NOT NULL, window_start DATE NOT NULL, window_end DATE NOT NULL,
  placement STRING NOT NULL, held_rule STRING, label STRING NOT NULL, reason STRING NOT NULL,
  news_axis BOOL, trend_axis BOOL, registry_status STRING, registry_keys INT64, outlet_hosts INT64,
  posts INT64, keyless_posts INT64, accounts INT64, located_posts INT64,
  feed_posts INT64, registry_posts INT64, outlet_posts INT64, outlet_accounts INT64, outlet_share FLOAT64,
  share_posts INT64, rt_posts INT64, outlet_link_posts INT64, link_posts INT64,
  own_accounts INT64, own_share FLOAT64,
  active_days INT64, growth_days INT64, peak_day_share FLOAT64, first_post_date DATE, last_post_date DATE,
  top_sound_accounts INT64, challenge_accounts INT64, caption_accounts INT64, format_signal BOOL,
  cause_dated_event BOOL, cause_cue STRING, news_driven BOOL,
  verified_accounts INT64, large_accounts INT64)
PARTITION BY run_date CLUSTER BY market, item_id;
