CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` (
  tombstone_contract_version STRING NOT NULL OPTIONS(description = 'Tombstone row contract version.'),
  tombstone_id STRING NOT NULL OPTIONS(description = 'Deterministic immutable tombstone identifier, ext_ plus a digest of derivation, reason and reconciliation.'),
  derivation_id STRING NOT NULL OPTIONS(description = 'derivation_id of the cancelled derivation.'),
  authorizing_approval_id STRING NOT NULL OPTIONS(description = 'authorizing_approval_id copied from the cancelled derivation.'),
  authorizing_grant_digest STRING NOT NULL OPTIONS(description = 'authorizing_grant_digest copied from the cancelled derivation.'),
  manifest_sha256 STRING NOT NULL OPTIONS(description = 'manifest_sha256 copied from the cancelled derivation.'),
  operation_context_sha256 STRING NOT NULL OPTIONS(description = 'operation_context_sha256 copied from the cancelled derivation.'),
  business_attempt_id STRING NOT NULL OPTIONS(description = 'business_attempt_id copied from the cancelled derivation.'),
  child_job_resource STRING NOT NULL OPTIONS(description = 'child_job_resource copied from the cancelled derivation.'),
  reason_code STRING NOT NULL OPTIONS(description = 'Cancellation reason, dispatch_not_attempted or provider_terminal_no_execution.'),
  reconciliation_digest STRING NOT NULL OPTIONS(description = 'Digest of exact canonical daily_dispatch_reconciliation_v1 bytes.'),
  cancelled_by STRING NOT NULL OPTIONS(description = 'SESSION_USER of the cancelling principal, which must equal derived_by.'),
  origin_registry_sha256 STRING NOT NULL OPTIONS(description = 'Immutable origin registry digest, copied from the cancelled derivation.'),
  resource_manifest_sha256 STRING NOT NULL OPTIONS(description = 'Immutable resource manifest digest, copied from the cancelled derivation.'),
  cancelled_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery transaction-owned cancellation time.')
)
CLUSTER BY child_job_resource, authorizing_grant_digest, derivation_id
OPTIONS(description = 'Immutable cancellation records for unconsumed daily derivations.');
