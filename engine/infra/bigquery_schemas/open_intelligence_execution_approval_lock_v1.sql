CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_execution_approval_lock_v1` (
  lock_name STRING NOT NULL OPTIONS(description = 'Singleton execution approval lock name.'),
  approval_contract_version STRING NOT NULL OPTIONS(description = 'Approval contract version guarded by this lock.'),
  lock_version INT64 NOT NULL OPTIONS(description = 'Monotonic lock mutation version.'),
  state STRING NOT NULL OPTIONS(description = 'Exactly ready, approving, consuming, recording_result or disabled.'),
  last_approval_id STRING OPTIONS(description = 'Most recently committed approval identifier.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery server-owned lock creation time.'),
  updated_at TIMESTAMP NOT NULL OPTIONS(description = 'BigQuery transaction-owned lock update time.')
)
OPTIONS (
  description = 'Mutable singleton serialization and emergency-disable state for execution approvals.'
);
