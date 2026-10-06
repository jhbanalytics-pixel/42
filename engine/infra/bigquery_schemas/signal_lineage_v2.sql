CREATE TABLE IF NOT EXISTS `{project}.{dataset}.signal_lineage_v2` (
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  brand_config_id STRING NOT NULL OPTIONS(description = 'Resolved brand configuration.'),
  audience_lens_ids ARRAY<STRING> OPTIONS(description = 'Resolved audience lenses, possibly empty.'),
  theme_id STRING NOT NULL OPTIONS(description = 'Resolved theme.'),
  run_id STRING NOT NULL OPTIONS(description = 'Lineage build run.'),
  contract_version STRING NOT NULL OPTIONS(description = 'Contract used by the writer.'),
  signal_date DATE NOT NULL OPTIONS(description = 'Date on which the relation was observed.'),
  market STRING NOT NULL OPTIONS(description = 'Lower case market code.'),
  from_signal_id STRING NOT NULL OPTIONS(description = 'Earlier or contributing identity.'),
  to_signal_id STRING NOT NULL OPTIONS(description = 'Continuing or resulting identity.'),
  relation STRING NOT NULL OPTIONS(description = 'Continues, merges_into, or splits_into.'),
  overlap_score FLOAT64 NOT NULL OPTIONS(description = 'Bounded 0 to 1.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'Persist time in UTC.')
)
PARTITION BY signal_date
CLUSTER BY market, relation, from_signal_id, to_signal_id
OPTIONS (
  description = 'One directed lineage edge created by one run. Natural key: (client_scope_id, signal_date, market, from_signal_id, to_signal_id, relation, run_id).'
);
