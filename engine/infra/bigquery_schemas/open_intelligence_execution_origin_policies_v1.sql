CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_origin_policies_v1` (
  contract_sha256 STRING NOT NULL OPTIONS(description = 'Immutable origin policy digest.'),
  canonical_policy_json STRING NOT NULL OPTIONS(description = 'Complete canonical origin policy JSON.'),
  registered_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery server-owned registration time.'),
  registered_by STRING NOT NULL OPTIONS(description = 'Registering control-plane identity.')
)
OPTIONS (
  description = 'Immutable canonical execution origin policies admitted by the control plane.'
);
