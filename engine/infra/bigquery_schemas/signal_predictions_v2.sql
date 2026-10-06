CREATE TABLE IF NOT EXISTS `{project}.{dataset}.signal_predictions_v2` (
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  brand_config_id STRING NOT NULL OPTIONS(description = 'Resolved brand configuration.'),
  audience_lens_ids ARRAY<STRING> OPTIONS(description = 'Resolved audience lenses, possibly empty.'),
  theme_id STRING NOT NULL OPTIONS(description = 'Resolved theme.'),
  run_id STRING NOT NULL OPTIONS(description = 'Prediction run.'),
  contract_version STRING NOT NULL OPTIONS(description = 'Contract used by the writer.'),
  prediction_id STRING NOT NULL OPTIONS(description = 'Deterministic immutable prediction identifier.'),
  signal_id STRING NOT NULL OPTIONS(description = 'Promoted signal.'),
  signal_date DATE NOT NULL OPTIONS(description = 'Promotion snapshot date.'),
  market STRING NOT NULL OPTIONS(description = 'Lower case market code.'),
  discovery_mode STRING NOT NULL OPTIONS(description = 'Same vocabulary as candidate discovery mode.'),
  source_families ARRAY<STRING> OPTIONS(description = 'Qualifying independent source families at prediction time.'),
  evidence_state STRING NOT NULL OPTIONS(description = 'Evidence state at prediction time.'),
  first_seen_at TIMESTAMP NOT NULL OPTIONS(description = 'First observed qualifying evidence time.'),
  predicted_at TIMESTAMP NOT NULL OPTIONS(description = 'Prediction creation time.'),
  expected_trajectory STRING NOT NULL OPTIONS(description = 'Peaked, sustained, growing, or fading.'),
  evaluation_date DATE NOT NULL OPTIONS(description = 'Date after which the outcome job may score this row.'),
  baseline STRUCT<
    velocity FLOAT64 NOT NULL,
    breadth FLOAT64 NOT NULL,
    evidence_family_count INT64 NOT NULL
  > NOT NULL OPTIONS(description = 'Observed baseline at prediction time. Every child mode is listed below.'),
  promotion_target STRUCT<
    velocity FLOAT64 NOT NULL,
    breadth FLOAT64 NOT NULL,
    evidence_family_count INT64 NOT NULL
  > NOT NULL OPTIONS(description = 'Declared target. Every child mode is listed below.'),
  invalidation_condition STRING NOT NULL OPTIONS(description = 'One measurable condition that would invalidate the prediction.'),
  cluster_build_version STRING NOT NULL OPTIONS(description = 'Identity build version used at prediction time.'),
  source_family_map_version STRING NOT NULL OPTIONS(description = 'Channel family mapping version used for attribution.'),
  rule_version STRING NOT NULL OPTIONS(description = 'Deterministic prediction rule version.'),
  display_eligible BOOL NOT NULL OPTIONS(description = 'System readiness gate. False until the weekly outcome loop is deployed and proven, then true for new eligible predictions without waiting for their own outcomes.')
)
PARTITION BY evaluation_date
CLUSTER BY market, discovery_mode, expected_trajectory, evidence_state
OPTIONS (
  description = 'One immutable prediction made when a signal is promoted. Natural key: prediction_id.'
);
