CREATE TABLE IF NOT EXISTS `{project}.{dataset}.collection_exposure_receipts_v1` (
  exposure_contract_version STRING NOT NULL OPTIONS(description = 'Exactly collection_exposure_receipt_v1.'),
  source_family STRING NOT NULL OPTIONS(description = 'Qualifying source family.'),
  exposure_date DATE NOT NULL OPTIONS(description = 'Closed exposure date.'),
  collection_policy_digest STRING NOT NULL OPTIONS(description = 'Canonical collection policy digest.'),
  source_sha STRING NOT NULL OPTIONS(description = 'Immutable issuer source SHA.'),
  image_digest STRING NOT NULL OPTIONS(description = 'Immutable issuer image digest.'),
  config_digest STRING NOT NULL OPTIONS(description = 'Canonical collection configuration digest.'),
  quota_authority_id STRING NOT NULL OPTIONS(description = 'Digest of the approved vendor quota receipt.'),
  quota_applicability STRING NOT NULL OPTIONS(description = 'Exactly metered or unmetered.'),
  quota_unit STRING OPTIONS(description = 'Vendor native quota unit when metered.'),
  quota_limit INT64 OPTIONS(description = 'Vendor quota limit when metered.'),
  quota_used INT64 OPTIONS(description = 'Vendor quota used when metered.'),
  quota_exhausted BOOL NOT NULL OPTIONS(description = 'Whether the approved quota was exhausted.'),
  capture_complete BOOL NOT NULL OPTIONS(description = 'Whether source capture was complete.'),
  source_copy_receipt_refs ARRAY<STRUCT<
    copy_run_id STRING NOT NULL,
    source_table STRING NOT NULL,
    source_set_digest STRING NOT NULL
  >> OPTIONS(description = 'Sorted exact source-copy receipt references.'),
  issued_at TIMESTAMP NOT NULL OPTIONS(description = 'Issuer completion time in UTC.'),
  issuer_identity STRING NOT NULL OPTIONS(description = 'Exact staging service identity.'),
  receipt_digest STRING NOT NULL OPTIONS(description = 'Canonical receipt digest excluding issuance metadata.')
)
PARTITION BY exposure_date
CLUSTER BY source_family
OPTIONS (
  description = 'Append-only collection exposure authority. Natural key: (source_family, exposure_date).'
);
