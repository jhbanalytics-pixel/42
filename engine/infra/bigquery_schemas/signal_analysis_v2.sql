CREATE TABLE IF NOT EXISTS `{project}.{dataset}.signal_analysis_v2` (
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  brand_config_id STRING NOT NULL OPTIONS(description = 'Resolved brand configuration.'),
  audience_lens_ids ARRAY<STRING> OPTIONS(description = 'Resolved audience lenses, possibly empty.'),
  theme_id STRING NOT NULL OPTIONS(description = 'Resolved theme.'),
  run_id STRING NOT NULL OPTIONS(description = 'Analysis run.'),
  contract_version STRING NOT NULL OPTIONS(description = 'Contract used by the writer.'),
  analysis_id STRING NOT NULL OPTIONS(description = 'Deterministic analysis identifier.'),
  signal_id STRING NOT NULL OPTIONS(description = 'Parent signal.'),
  signal_date DATE NOT NULL OPTIONS(description = 'Signal snapshot date.'),
  market STRING NOT NULL OPTIONS(description = 'Lower case market code.'),
  evidence_state STRING NOT NULL OPTIONS(description = 'Ready, thin, contradictory, or unchecked.'),
  summary STRING NOT NULL OPTIONS(description = 'Plain language signal read.'),
  why_now STRING NOT NULL OPTIONS(description = 'Dated explanation limited to supported evidence.'),
  possible_response STRING OPTIONS(description = 'One bounded strategist response. Null when evidence cannot support one.'),
  limitations ARRAY<STRING> OPTIONS(description = 'Named missing evidence and scope limits.'),
  contradictions ARRAY<STRING> OPTIONS(description = 'Named disagreements. Empty when none qualify.'),
  evidence_ids ARRAY<STRING> OPTIONS(description = 'Evidence used by the analysis. At least one for a nonempty claim.'),
  human_review_required BOOL NOT NULL OPTIONS(description = 'True for election scoped output before export.'),
  model_version STRING OPTIONS(description = 'Exact model identifier. Null for deterministic fallback.'),
  analyzed_at TIMESTAMP NOT NULL OPTIONS(description = 'Analysis time in UTC.')
)
PARTITION BY signal_date
CLUSTER BY market, evidence_state, human_review_required, signal_id
OPTIONS (
  description = 'One evidence bounded analysis for one signal snapshot and run. Natural key: (client_scope_id, signal_date, market, signal_id, analysis_id).'
);
