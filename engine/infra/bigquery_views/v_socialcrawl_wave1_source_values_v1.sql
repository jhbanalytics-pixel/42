CREATE OR REPLACE VIEW `{project}.{view_dataset}.v_socialcrawl_wave1_source_values_v1` AS
SELECT
  measured.values_contract_version,
  measured.source_value_id,
  measured.execution_id,
  measured.run_id,
  measured.recorded_at,
  measured.credential_lane,
  measured.close_verdict,
  measured.attribution_state,
  measured.source_sha,
  measured.catalog_digest,
  measured.metadata_digest,
  measured.route_path,
  measured.state,
  measured.reason,
  measured.unique_observations,
  measured.marginal_candidates,
  measured.marginal_evidence,
  measured.created_at
FROM `{project}.{source_dataset}.socialcrawl_wave1_source_values_v1` AS measured
WHERE measured.credential_lane = 'ogilvy_funded'
  AND measured.values_contract_version = 'socialcrawl_wave1_source_values_v1';
