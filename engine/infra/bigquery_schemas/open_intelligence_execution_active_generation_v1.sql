CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_active_generation_v1` (
  origin_registry_sha256 STRING NOT NULL OPTIONS(description = 'Active origin registry digest.'),
  resource_manifest_sha256 STRING NOT NULL OPTIONS(description = 'Active resource manifest digest.')
)
OPTIONS (
  description = 'Current execution origin registry and resource manifest generation.'
);
