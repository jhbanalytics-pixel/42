CREATE TABLE IF NOT EXISTS `{project}.{dataset}.signal_candidates_v2` (
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  brand_config_id STRING NOT NULL OPTIONS(description = 'Resolved brand configuration.'),
  audience_lens_ids ARRAY<STRING> OPTIONS(description = 'Resolved audience lenses, possibly empty.'),
  theme_id STRING NOT NULL OPTIONS(description = 'Resolved theme.'),
  run_id STRING NOT NULL OPTIONS(description = 'Logical build run.'),
  contract_version STRING NOT NULL OPTIONS(description = 'Contract used by the writer.'),
  signal_id STRING NOT NULL OPTIONS(description = 'Stable signal identity.'),
  signal_date DATE NOT NULL OPTIONS(description = 'Daily snapshot date and partition key.'),
  market STRING NOT NULL OPTIONS(description = 'Lower case market code in market_scope.'),
  label STRING NOT NULL OPTIONS(description = 'Most central observed phrase. Gemini does not choose membership.'),
  cluster_signature STRING NOT NULL OPTIONS(description = 'Deterministic digest of canonical membership inputs.'),
  cluster_build_version STRING NOT NULL OPTIONS(description = 'Deterministic clustering rule version.'),
  model_version STRING OPTIONS(description = 'Model used only for a promoted summary. Null when no model summary ran.'),
  discovery_mode STRING NOT NULL OPTIONS(description = 'Dynamic, replay, or canary. Canary is valid only in QA.'),
  topic_tags ARRAY<STRING> OPTIONS(description = 'Optional legacy taxonomy relationships. Tags never decide identity.'),
  novelty_score FLOAT64 NOT NULL OPTIONS(description = 'Bounded 0 to 1.'),
  velocity_score FLOAT64 NOT NULL OPTIONS(description = 'Bounded 0 to 1.'),
  breadth_score FLOAT64 NOT NULL OPTIONS(description = 'Bounded 0 to 1.'),
  independence_score FLOAT64 NOT NULL OPTIONS(description = 'Bounded 0 to 1.'),
  historical_similarity FLOAT64 OPTIONS(description = 'Bounded 0 to 1. Null when no valid historical comparison exists.'),
  geo_confidence FLOAT64 NOT NULL OPTIONS(description = 'Bounded 0 to 1.'),
  evidence_state STRING NOT NULL OPTIONS(description = 'Ready, thin, contradictory, or unchecked.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'Persist time in UTC.'),
  label_member_identity STRING OPTIONS(description = 'Exact winning observation identity that owns the candidate label.')
)
PARTITION BY signal_date
CLUSTER BY market, evidence_state
OPTIONS (
  description = 'One daily snapshot of one stable signal identity per market and run. Natural key: (client_scope_id, signal_date, market, signal_id, run_id).'
);
