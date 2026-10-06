CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_approvals_v1` (
  approval_contract_version STRING NOT NULL OPTIONS(description = 'Approval row contract version.'),
  approval_id STRING NOT NULL OPTIONS(description = 'Deterministic immutable approval identifier.'),
  manifest_version STRING NOT NULL OPTIONS(description = 'Canonical execution manifest version.'),
  operation STRING NOT NULL OPTIONS(description = 'Approved staging operation.'),
  contract_sha256 STRING NOT NULL OPTIONS(description = 'Human-approved operation contract digest.'),
  manifest_sha256 STRING NOT NULL OPTIONS(description = 'Digest of exact canonical manifest bytes.'),
  canonical_manifest_json STRING NOT NULL OPTIONS(description = 'Complete canonical execution manifest JSON.'),
  approved_by STRING NOT NULL OPTIONS(description = 'Salted human SESSION_USER pseudonym.'),
  approved_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery transaction-owned approval time.'),
  expires_at TIMESTAMP NOT NULL OPTIONS(description = 'Latest UTC time at which consumption may commit.'),
  approval_phrase_sha256 STRING NOT NULL OPTIONS(description = 'Digest of the exact approval phrase bytes.')
)
OPTIONS (
  description = 'Immutable human approvals for one exact staging execution manifest.'
);
