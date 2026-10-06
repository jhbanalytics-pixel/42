CREATE TABLE IF NOT EXISTS `{project}.{dataset}.open_intelligence_observation_dispositions_v1` (
  operation_id STRING NOT NULL OPTIONS(description = 'Execution or run id of the operation that recorded the disposition.'),
  observation_key STRING NOT NULL OPTIONS(description = 'Native or inferred observation identity key.'),
  identity_kind STRING NOT NULL OPTIONS(description = 'Exactly native or inferred.'),
  native_namespace STRING OPTIONS(description = 'Namespace of the native id; the platform for a native identity.'),
  native_id STRING OPTIONS(description = 'Native source id the producer preserved; null for an inferred identity.'),
  collection_event_id STRING NOT NULL OPTIONS(description = 'The collection event that produced this record.'),
  source_row_id STRING OPTIONS(description = 'Persisted source row id when the record reached storage.'),
  boundary STRING NOT NULL OPTIONS(description = 'producer, dedup, enrichment, classification, capture, membership or release.'),
  outcome STRING NOT NULL OPTIONS(description = 'Exactly admitted, rejected or unknown.'),
  reason_code STRING OPTIONS(description = 'Reason from the boundary decision record; required for rejected and unknown.'),
  market STRING NOT NULL OPTIONS(description = 'Exactly za, ng or ke.'),
  route STRING NOT NULL OPTIONS(description = 'Collection route the record travelled.'),
  observed_at TIMESTAMP NOT NULL OPTIONS(description = 'Observation instant in UTC.'),
  source_binding_digest STRING NOT NULL OPTIONS(description = 'Digest of the source binding the operation ran under.')
)
PARTITION BY DATE(observed_at)
CLUSTER BY market, route, boundary
OPTIONS (
  description = 'Append-only observation disposition sidecar. Identities are counted through sets over a window, never through per day distinct sums.'
);
