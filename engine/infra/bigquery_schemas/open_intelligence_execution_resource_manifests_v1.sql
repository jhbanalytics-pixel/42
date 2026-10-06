CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_resource_manifests_v1` (
  resource_manifest_sha256 STRING NOT NULL OPTIONS(description = 'Immutable resource manifest digest.'),
  origin_registry_sha256 STRING NOT NULL OPTIONS(description = 'Immutable origin registry digest.'),
  canonical_resource_manifest_json STRING NOT NULL OPTIONS(description = 'Complete canonical resource manifest JSON.'),
  registered_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery server-owned registration time.'),
  registered_by STRING NOT NULL OPTIONS(description = 'Registering control-plane identity.')
)
OPTIONS (
  description = 'Immutable canonical resource manifest generations bound to origin registries.'
);
