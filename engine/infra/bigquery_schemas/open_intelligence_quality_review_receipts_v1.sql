CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_quality_review_receipts_v1` (
  review_store_contract_version STRING NOT NULL OPTIONS(description = 'Quality review store contract used by the registration routine.'),
  run_id STRING NOT NULL OPTIONS(description = 'Exact reviewed replacement run identity and natural key.'),
  canonical_review_receipt_json STRING NOT NULL OPTIONS(description = 'Complete canonical UTF-8 quality review receipt JSON without a trailing newline.'),
  review_receipt_digest STRING NOT NULL OPTIONS(description = 'Validated digest carried by the quality review receipt.'),
  artifact_sha256 STRING NOT NULL OPTIONS(description = 'SHA256 of the complete canonical receipt bytes including receipt_digest.'),
  source_window_digest STRING NOT NULL OPTIONS(description = 'Validated current source window authority digest.'),
  candidate_projection_digest STRING NOT NULL OPTIONS(description = 'Validated current canonical candidate projection digest.'),
  packet_digest STRING NOT NULL OPTIONS(description = 'Validated current canonical review packet digest.'),
  registered_by STRING NOT NULL OPTIONS(description = 'Approved human pseudonym derived from the routine caller.'),
  registered_at TIMESTAMP NOT NULL OPTIONS(description = 'Transaction-owned UTC registration time.')
)
OPTIONS (
  description = 'One immutable canonical human quality review receipt per replacement run. Natural key: run_id.'
);
