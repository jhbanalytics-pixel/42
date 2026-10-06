CREATE TABLE IF NOT EXISTS `{project}.{dataset}.signal_outcomes_v2` (
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  brand_config_id STRING NOT NULL OPTIONS(description = 'Resolved brand configuration.'),
  audience_lens_ids ARRAY<STRING> OPTIONS(description = 'Resolved audience lenses, possibly empty.'),
  theme_id STRING NOT NULL OPTIONS(description = 'Resolved theme.'),
  run_id STRING NOT NULL OPTIONS(description = 'Outcome evaluation run.'),
  contract_version STRING NOT NULL OPTIONS(description = 'Contract used by the writer.'),
  outcome_id STRING NOT NULL OPTIONS(description = 'Deterministic outcome identifier.'),
  prediction_id STRING NOT NULL OPTIONS(description = 'Parent prediction.'),
  signal_id STRING NOT NULL OPTIONS(description = 'Evaluated signal.'),
  signal_date DATE NOT NULL OPTIONS(description = 'Original promotion date.'),
  market STRING NOT NULL OPTIONS(description = 'Lower case market code.'),
  discovery_mode STRING NOT NULL OPTIONS(description = 'Discovery mode at prediction time.'),
  source_families ARRAY<STRING> OPTIONS(description = 'Qualifying families copied from the prediction for attribution. No aggregate sentinel.'),
  source_family_map_version STRING NOT NULL OPTIONS(description = 'Channel family mapping version used for attribution.'),
  evaluation_date DATE NOT NULL OPTIONS(description = 'Contracted evaluation date.'),
  evaluated_at TIMESTAMP NOT NULL OPTIONS(description = 'Actual evaluation time.'),
  outcome STRING NOT NULL OPTIONS(description = 'Peaked, sustained, fizzled, noise, or unresolved.'),
  observed_velocity FLOAT64 OPTIONS(description = 'Metric at evaluation.'),
  observed_breadth FLOAT64 OPTIONS(description = 'Metric at evaluation.'),
  observed_evidence_family_count INT64 OPTIONS(description = 'Qualifying families at evaluation.'),
  human_calibration_label STRING OPTIONS(description = 'Human sample label when present. No model judgment.'),
  human_reviewed_at TIMESTAMP OPTIONS(description = 'Human calibration time.'),
  resolution_reason STRING NOT NULL OPTIONS(description = 'Named rule or insufficiency that produced the outcome.'),
  rule_version STRING NOT NULL OPTIONS(description = 'Outcome rule version.')
)
PARTITION BY evaluation_date
CLUSTER BY market, outcome, discovery_mode
OPTIONS (
  description = 'Exactly one immutable scored result per prediction and evaluation run. Natural key: (prediction_id, evaluation_date, run_id).'
);
