CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_origin_registries_v1` (
  origin_registry_sha256 STRING NOT NULL OPTIONS(description = 'Immutable origin registry digest.'),
  canonical_registry_json STRING NOT NULL OPTIONS(description = 'Complete canonical origin registry JSON.'),
  registered_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery server-owned registration time.'),
  registered_by STRING NOT NULL OPTIONS(description = 'Registering control-plane identity.')
)
OPTIONS (
  description = 'Immutable canonical origin registry generations admitted by the control plane.'
);
