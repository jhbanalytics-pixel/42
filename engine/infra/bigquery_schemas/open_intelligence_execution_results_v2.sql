CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_results_v2` (
  result_contract_version STRING NOT NULL OPTIONS(description = 'Result link contract version.'),
  result_id STRING NOT NULL OPTIONS(description = 'Deterministic immutable result identifier.'),
  consumption_id STRING NOT NULL OPTIONS(description = 'Consumed execution authority identifier.'),
  approval_id STRING NOT NULL OPTIONS(description = 'Human approval identifier.'),
  manifest_sha256 STRING NOT NULL OPTIONS(description = 'Approved manifest digest.'),
  operation STRING NOT NULL OPTIONS(description = 'Completed staging operation.'),
  execution_name STRING NOT NULL OPTIONS(description = 'Immutable Cloud Run Execution resource.'),
  result_reference STRING NOT NULL OPTIONS(description = 'Stable operation receipt or canonical output reference.'),
  canonical_result_json STRING NOT NULL OPTIONS(description = 'Complete canonical operation receipt or bootstrap proof JSON.'),
  result_digest STRING NOT NULL OPTIONS(description = 'Digest of the referenced operation result.'),
  status STRING NOT NULL OPTIONS(description = 'Exactly succeeded or failed.'),
  completed_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery transaction-owned result-link time.'),
  origin_registry_sha256 STRING NOT NULL OPTIONS(description = 'Immutable origin registry digest.'),
  resource_manifest_sha256 STRING NOT NULL OPTIONS(description = 'Immutable resource manifest digest.')
)
OPTIONS (
  description = 'Immutable terminal links bound to one consumed approval and resource generation.'
);
