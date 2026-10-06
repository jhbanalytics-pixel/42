CREATE TABLE IF NOT EXISTS `{project}.{dataset}.signal_evidence_v2` (
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  brand_config_id STRING NOT NULL OPTIONS(description = 'Resolved brand configuration.'),
  audience_lens_ids ARRAY<STRING> OPTIONS(description = 'Resolved audience lenses, possibly empty.'),
  theme_id STRING NOT NULL OPTIONS(description = 'Resolved theme.'),
  run_id STRING NOT NULL OPTIONS(description = 'Evidence extraction run.'),
  contract_version STRING NOT NULL OPTIONS(description = 'Contract used by the writer.'),
  signal_date DATE NOT NULL OPTIONS(description = 'Signal snapshot date.'),
  market STRING NOT NULL OPTIONS(description = 'Lower case market code.'),
  signal_id STRING NOT NULL OPTIONS(description = 'Parent signal identity.'),
  evidence_id STRING NOT NULL OPTIONS(description = 'Stable evidence identifier.'),
  row_id STRING NOT NULL OPTIONS(description = 'Source row identifier.'),
  source_family STRING NOT NULL OPTIONS(description = 'Independent channel family from the current engine family map.'),
  platform STRING NOT NULL OPTIONS(description = 'Normalized observed platform.'),
  url STRING OPTIONS(description = 'Direct evidence URL. Null when the source provides none.'),
  published_at TIMESTAMP OPTIONS(description = 'Source publish time. Null cannot qualify as current evidence.'),
  claim_role STRING NOT NULL OPTIONS(description = 'Identity, direction, context, contradiction, or geo.'),
  direction STRING NOT NULL OPTIONS(description = 'Rising, stable, declining, conflicting, or not_applicable.'),
  geo_confidence FLOAT64 NOT NULL OPTIONS(description = 'Bounded 0 to 1.'),
  source_label STRING OPTIONS(description = 'Human readable source or host retained for evidence display.'),
  author_label STRING OPTIONS(description = 'Public author label when policy permits retention.'),
  excerpt STRING OPTIONS(description = 'Sanitized excerpt. Null when retention or policy prevents display.'),
  metric_label STRING OPTIONS(description = 'Observed metric with unit. Never inferred.'),
  availability STRING NOT NULL OPTIONS(description = 'Available, aged_out, or unavailable.'),
  evidence_state STRING NOT NULL OPTIONS(description = 'State assigned to the parent evaluation.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'Persist time in UTC.'),
  vendor_family STRING OPTIONS(description = 'Canonical data supplier family. Distinct from channel family.'),
  channel_family STRING OPTIONS(description = 'Canonical evidence channel. Equals source_family compatibility alias.')
)
PARTITION BY signal_date
CLUSTER BY market, signal_id, source_family, evidence_state
OPTIONS (
  description = 'One observed evidence row attached to one signal snapshot. Natural key: (client_scope_id, signal_date, market, signal_id, evidence_id, run_id).'
);
