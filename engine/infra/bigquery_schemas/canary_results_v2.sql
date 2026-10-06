CREATE TABLE IF NOT EXISTS `{project}.{dataset}.canary_results_v2` (
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  brand_config_id STRING NOT NULL OPTIONS(description = 'Resolved brand configuration.'),
  audience_lens_ids ARRAY<STRING> OPTIONS(description = 'Resolved audience lenses, possibly empty.'),
  theme_id STRING NOT NULL OPTIONS(description = 'Resolved theme.'),
  run_id STRING NOT NULL OPTIONS(description = 'Logical canary run.'),
  contract_version STRING NOT NULL OPTIONS(description = 'Contract used by the writer.'),
  canary_run_id STRING NOT NULL OPTIONS(description = 'Immutable identifier for one canary execution.'),
  canary_id STRING NOT NULL OPTIONS(description = 'Approved canary case identifier.'),
  expected_result STRING NOT NULL OPTIONS(description = 'Expected bounded canary result.'),
  actual_result STRING OPTIONS(description = 'Observed bounded canary result.'),
  passed BOOL NOT NULL OPTIONS(description = 'Whether actual matched expected.'),
  started_at TIMESTAMP NOT NULL OPTIONS(description = 'Canary start time in UTC.'),
  finished_at TIMESTAMP OPTIONS(description = 'Canary finish time in UTC.'),
  error_code STRING OPTIONS(description = 'Bounded error code when failed.')
)
PARTITION BY DATE(started_at)
CLUSTER BY canary_id, passed
OPTIONS (
  partition_expiration_days = 90,
  description = 'One isolated QA result for one approved canary case and canary run.'
);
