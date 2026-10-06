CREATE TABLE IF NOT EXISTS `{project}.{dataset}.signal_membership_v2` (
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  brand_config_id STRING NOT NULL OPTIONS(description = 'Resolved brand configuration.'),
  audience_lens_ids ARRAY<STRING> OPTIONS(description = 'Resolved audience lenses, possibly empty.'),
  theme_id STRING NOT NULL OPTIONS(description = 'Resolved theme.'),
  run_id STRING NOT NULL OPTIONS(description = 'Membership snapshot run.'),
  contract_version STRING NOT NULL OPTIONS(description = 'Contract used by the writer.'),
  signal_date DATE NOT NULL OPTIONS(description = 'Signal snapshot date and partition key.'),
  market STRING NOT NULL OPTIONS(description = 'Lower case market code in market_scope.'),
  signal_id STRING NOT NULL OPTIONS(description = 'Parent signal identity.'),
  member_id STRING NOT NULL OPTIONS(description = 'Stable mem_ identifier derived from the complete member facts.'),
  member_identity STRING NOT NULL OPTIONS(description = 'Stable observation identity used by component membership.'),
  candidate_type STRING NOT NULL OPTIONS(description = 'Accepted candidate observation type.'),
  canonical_value STRING NOT NULL OPTIONS(description = 'Canonical candidate value used for identity.'),
  source_families ARRAY<STRING> OPTIONS(description = 'Complete sorted independent source families for this member.'),
  platforms ARRAY<STRING> OPTIONS(description = 'Complete sorted platforms for this member.'),
  row_id STRING NOT NULL OPTIONS(description = 'Real aggregate or row level source identifier.'),
  qualifies_evidence BOOL NOT NULL OPTIONS(description = 'Whether this member has an accepted row level evidence receipt.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'Persist time in UTC.'),
  vendor_families ARRAY<STRING> OPTIONS(description = 'Complete sorted data supplier families for this member.'),
  channel_families ARRAY<STRING> OPTIONS(description = 'Complete sorted channel families. Equals source_families compatibility aliases.'),
  source_provenance_json STRING OPTIONS(description = 'Canonical sampled source provenance envelope for hybrid_graph_v3. Null for legacy rows.')
)
PARTITION BY signal_date
CLUSTER BY market, signal_id, candidate_type
OPTIONS (
  description = 'One immutable member of one V3 signal snapshot and run, retaining the complete stable membership facts needed for identity and lineage replay.'
);
