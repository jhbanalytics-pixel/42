CREATE TABLE IF NOT EXISTS `{project}.{dataset}.socialcrawl_wave1_source_values_v1` (
  values_contract_version STRING NOT NULL OPTIONS(description = 'Wave 1 source value row contract version.'),
  source_value_id STRING NOT NULL OPTIONS(description = 'Content address over execution_id and route_path.'),
  execution_id STRING NOT NULL OPTIONS(description = 'Funded execution identifier of the pilot that measured the route.'),
  run_id STRING NOT NULL OPTIONS(description = 'Pipeline run identifier.'),
  recorded_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC funded run close time and partition key.'),
  credential_lane STRING NOT NULL OPTIONS(description = 'Approved funded credential lane.'),
  close_verdict STRING NOT NULL OPTIONS(description = 'Succeeded when the funded close attribution held with a measured balance and no terminal, otherwise failed. The Source Lab snapshot carries succeeded rows only.'),
  attribution_state STRING NOT NULL OPTIONS(description = 'Complete, conservative, or gap detected attribution state at close.'),
  source_sha STRING OPTIONS(description = 'Full source commit SHA admitted for the runtime, null when the lane ran without one.'),
  catalog_digest STRING NOT NULL OPTIONS(description = 'Wave 1 route identity digest the pilot ran against.'),
  metadata_digest STRING NOT NULL OPTIONS(description = 'Wave 1 route metadata digest the pilot ran against.'),
  route_path STRING NOT NULL OPTIONS(description = 'Canonical Wave 1 route path beginning with /v1/.'),
  state STRING NOT NULL OPTIONS(description = 'Passed or permanently_rejected source value state.'),
  reason STRING OPTIONS(description = 'Rejection reason, null when the value passed.'),
  unique_observations INT64 NOT NULL OPTIONS(description = 'Distinct evidence observations the route produced.'),
  marginal_candidates INT64 NOT NULL OPTIONS(description = 'Candidate keys only this route produced.'),
  marginal_evidence INT64 NOT NULL OPTIONS(description = 'Evidence keys only this route produced.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC immutable row creation time.')
)
PARTITION BY DATE(recorded_at)
CLUSTER BY credential_lane, close_verdict, execution_id
OPTIONS (
  description = 'Staging-only immutable Wave 1 measured source values per funded execution and route, carried into Source Lab by the daily snapshot.'
);
