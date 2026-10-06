CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_quality_release_records_v2` (
  run_id STRING NOT NULL OPTIONS(description = 'Exact immutable released run identity.'),
  run_receipt_digest STRING NOT NULL OPTIONS(description = 'Canonical post-release run receipt digest.'),
  source_window_digest STRING NOT NULL OPTIONS(description = 'Exact source window digest.'),
  candidate_projection_digest STRING NOT NULL OPTIONS(description = 'Canonical quality projection digest.'),
  packet_digest STRING NOT NULL OPTIONS(description = 'Canonical complete review packet digest.'),
  review_receipt_digest STRING NOT NULL OPTIONS(description = 'Approved complete human review receipt digest.'),
  approval_addendum_sha256 STRING NOT NULL OPTIONS(description = 'Approved r3-release-addendum-v1 SHA256.'),
  released_at TIMESTAMP NOT NULL OPTIONS(description = 'Release transaction time in UTC.'),
  release_contract_version STRING NOT NULL OPTIONS(description = 'Exactly open_intelligence_quality_release_v2.')
)
PARTITION BY DATE(released_at)
CLUSTER BY run_id
OPTIONS (
  description = 'Append-only quality-owned display authority. One immutable row per run.'
);
