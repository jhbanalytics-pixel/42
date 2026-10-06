CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_consumptions_v1` (
  consumption_contract_version STRING NOT NULL OPTIONS(description = 'Consumption row contract version.'),
  consumption_id STRING NOT NULL OPTIONS(description = 'Deterministic immutable consumption identifier.'),
  approval_id STRING NOT NULL OPTIONS(description = 'Consumed approval identifier.'),
  manifest_sha256 STRING NOT NULL OPTIONS(description = 'Consumed manifest digest.'),
  operation STRING NOT NULL OPTIONS(description = 'Consumed staging operation.'),
  execution_name STRING NOT NULL OPTIONS(description = 'Immutable Cloud Run Execution resource.'),
  job_resource STRING NOT NULL OPTIONS(description = 'Exact parent Cloud Run Job resource.'),
  source_sha STRING NOT NULL OPTIONS(description = 'Build-proven source Git SHA.'),
  image_uri STRING NOT NULL OPTIONS(description = 'Complete immutable Artifact Registry image URI.'),
  consumed_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery transaction-owned consumption time.')
)
OPTIONS (
  description = 'Immutable one-use reservations binding an approval to one Cloud Run Execution.'
);
