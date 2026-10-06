CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_run_receipts_v1` (
  run_contract_version STRING NOT NULL OPTIONS(description = 'Run receipt contract used by the writer.'),
  run_id STRING NOT NULL OPTIONS(description = 'Engine issued immutable run identity.'),
  client_scope_id STRING NOT NULL OPTIONS(description = 'Resolved client scope.'),
  market_scope ARRAY<STRING> OPTIONS(description = 'Markets resolved for the run.'),
  signal_date DATE NOT NULL OPTIONS(description = 'Candidate snapshot date. Equals observation_end.'),
  observation_start DATE NOT NULL OPTIONS(description = 'Inclusive closed source window start.'),
  observation_end DATE NOT NULL OPTIONS(description = 'Inclusive closed source window end.'),
  observation_method STRING NOT NULL OPTIONS(description = 'Exact engine observation method identifier.'),
  source_window_digest STRING NOT NULL OPTIONS(description = 'Digest of the exact closed source window.'),
  cluster_build_version STRING NOT NULL OPTIONS(description = 'Identity build version used by the run.'),
  source_family_map_version STRING NOT NULL OPTIONS(description = 'Channel family mapping version used for attribution.'),
  rule_version STRING NOT NULL OPTIONS(description = 'Deterministic rule version used by the run.'),
  status STRING NOT NULL OPTIONS(description = 'Exactly completed or failed.'),
  complete_partitions BOOL NOT NULL OPTIONS(description = 'True only when every required partition was written and read back.'),
  display_release_state STRING NOT NULL OPTIONS(description = 'Exactly blocked or enabled. Blocked by default.'),
  candidate_count INT64 NOT NULL OPTIONS(description = 'Candidate rows written by the run.'),
  evidence_count INT64 NOT NULL OPTIONS(description = 'Evidence rows written by the run.'),
  membership_count INT64 NOT NULL OPTIONS(description = 'Membership rows written by the run.'),
  lineage_count INT64 NOT NULL OPTIONS(description = 'Lineage rows written by the run.'),
  analysis_count INT64 NOT NULL OPTIONS(description = 'Analysis rows written by the run.'),
  prediction_count INT64 NOT NULL OPTIONS(description = 'Prediction rows written by the run.'),
  row_set_digest STRING NOT NULL OPTIONS(description = 'Digest of the natural keys of every row family written by the run.'),
  source_sha STRING NOT NULL OPTIONS(description = 'Full forty character lower case commit SHA of the writing source.'),
  completed_at TIMESTAMP NOT NULL OPTIONS(description = 'Engine completion time.')
)
PARTITION BY signal_date
CLUSTER BY client_scope_id, status, display_release_state
OPTIONS (
  description = 'One immutable completion marker for one exact engine run, written last after readback of every row family. Natural key: run_id.'
);
