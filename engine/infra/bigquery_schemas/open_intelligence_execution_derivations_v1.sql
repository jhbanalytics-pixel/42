CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_derivations_v1` (
  derivation_contract_version STRING NOT NULL OPTIONS(description = 'Derivation row contract version.'),
  derivation_id STRING NOT NULL OPTIONS(description = 'Deterministic immutable derivation identifier, exd_ plus a digest of the binding fields.'),
  authorizing_approval_id STRING NOT NULL OPTIONS(description = 'approval_id of the recurring grant row that authorised this derivation.'),
  authorizing_grant_digest STRING NOT NULL OPTIONS(description = 'manifest_sha256 of the authorising recurring grant.'),
  manifest_version STRING NOT NULL OPTIONS(description = 'Canonical execution manifest version of the child manifest.'),
  operation STRING NOT NULL OPTIONS(description = 'Daily operation named by the operation context and allowed by the grant.'),
  contract_sha256 STRING NOT NULL OPTIONS(description = 'Operation contract digest named by the child execution manifest.'),
  manifest_sha256 STRING NOT NULL OPTIONS(description = 'Digest of exact canonical child manifest bytes.'),
  canonical_manifest_json STRING NOT NULL OPTIONS(description = 'Complete canonical child execution manifest JSON.'),
  operation_context_sha256 STRING NOT NULL OPTIONS(description = 'Digest of exact canonical operation context bytes.'),
  canonical_operation_context_json STRING NOT NULL OPTIONS(description = 'Complete canonical daily operation context JSON.'),
  business_attempt_id STRING NOT NULL OPTIONS(description = 'Stable business attempt key from the operation context; a repeat returns the existing row.'),
  child_job_resource STRING NOT NULL OPTIONS(description = 'Child job resource named by the operation context and bound by the grant.'),
  child_service_identity STRING NOT NULL OPTIONS(description = 'Child service identity from the operation context, one of the grant executing principals.'),
  child_image_uri STRING NOT NULL OPTIONS(description = 'Child image URI from the operation context, bound by the grant build provenance.'),
  child_job_policy_digest STRING NOT NULL OPTIONS(description = 'Child job policy digest from the operation context, bound by the grant job policy.'),
  derived_by STRING NOT NULL OPTIONS(description = 'SESSION_USER of the parent principal that derived the row.'),
  origin_registry_sha256 STRING NOT NULL OPTIONS(description = 'Immutable origin registry digest, copied from the authorising grant.'),
  resource_manifest_sha256 STRING NOT NULL OPTIONS(description = 'Immutable resource manifest digest, copied from the authorising grant.'),
  cost_policy_sha256 STRING NOT NULL OPTIONS(description = 'Digest of the cost_policy input artifact named by the child manifest.'),
  derived_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery transaction-owned derivation time.'),
  expires_at TIMESTAMP NOT NULL OPTIONS(description = 'Latest UTC time at which consumption may proceed, the earlier of grant expiry and one hour after derivation.'),
  reserved_micro_usd INT64 NOT NULL OPTIONS(description = 'Operation reservation in micro USD from the cost policy, counted against the grant allowances.')
)
CLUSTER BY operation, child_job_resource, authorizing_grant_digest
OPTIONS(description = 'Immutable machine derivations linked to protected recurring grants.');
