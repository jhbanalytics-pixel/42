CREATE OR REPLACE VIEW `{project}.{dataset}.v_desk_dynamic_signals_v2` AS
WITH valid_provenance_runs AS (
WITH provenance_receipts AS (
  SELECT run_id, ANY_VALUE(client_scope_id) AS client_scope_id,
    ANY_VALUE(signal_date) AS signal_date, ANY_VALUE(cluster_build_version) AS cluster_build_version,
    ANY_VALUE(candidate_count) AS candidate_count, ANY_VALUE(membership_count) AS membership_count
  FROM `{project}.{dataset}.open_intelligence_run_receipts_v1` AS source_receipt
  WHERE client_scope_id != 'qa_canary'
  GROUP BY run_id
  HAVING COUNT(*) = 1 AND COUNTIF(status = 'completed' AND complete_partitions
    AND source_receipt.cluster_build_version IN ('hybrid_graph_v1', 'hybrid_graph_v2', 'hybrid_graph_v3')) = 1
), provenance_candidates AS (
  SELECT client_scope_id, run_id, signal_date, market, signal_id,
    COUNT(*) AS row_count, ANY_VALUE(cluster_build_version) AS cluster_build_version
  FROM `{project}.{dataset}.signal_candidates_v2`
  WHERE client_scope_id != 'qa_canary'
  GROUP BY client_scope_id, run_id, signal_date, market, signal_id
), provenance_candidate_checks AS (
  SELECT c.run_id, SUM(c.row_count) AS candidate_count,
    COUNTIF(c.row_count != 1 OR NOT IFNULL(c.cluster_build_version = r.cluster_build_version, FALSE)
      OR NOT IFNULL(c.client_scope_id = r.client_scope_id, FALSE)
      OR NOT IFNULL(c.signal_date = r.signal_date, FALSE)) AS invalid_count
  FROM provenance_candidates AS c JOIN provenance_receipts AS r ON r.run_id = c.run_id
  GROUP BY c.run_id
), provenance_membership_checks AS (
  SELECT m.run_id, COUNT(*) AS membership_count,
    COUNTIF(NOT IFNULL(c.row_count = 1, FALSE)
      OR NOT IFNULL(c.cluster_build_version = r.cluster_build_version, FALSE)
      OR NOT IFNULL(m.client_scope_id = r.client_scope_id, FALSE)
      OR NOT IFNULL(m.signal_date = r.signal_date, FALSE)
      OR (r.cluster_build_version = 'hybrid_graph_v3' AND m.source_provenance_json IS NULL)
      OR (r.cluster_build_version != 'hybrid_graph_v3' AND m.source_provenance_json IS NOT NULL)) AS invalid_count
  FROM `{project}.{dataset}.signal_membership_v2` AS m
  JOIN provenance_receipts AS r ON r.run_id = m.run_id
  LEFT JOIN provenance_candidates AS c ON c.client_scope_id = m.client_scope_id AND c.run_id = m.run_id
    AND c.signal_date = m.signal_date AND c.market = m.market AND c.signal_id = m.signal_id
  WHERE m.client_scope_id != 'qa_canary'
  GROUP BY m.run_id
)
SELECT r.run_id, r.cluster_build_version
FROM provenance_receipts AS r
LEFT JOIN provenance_candidate_checks AS c ON c.run_id = r.run_id
LEFT JOIN provenance_membership_checks AS m ON m.run_id = r.run_id
WHERE IFNULL(c.candidate_count, 0) = r.candidate_count AND IFNULL(c.invalid_count, 0) = 0
  AND IFNULL(m.membership_count, 0) = r.membership_count AND IFNULL(m.invalid_count, 0) = 0
),
projection_candidates AS (
  SELECT CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(candidate.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(candidate.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.contract_version), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.signal_id), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', candidate.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.market), '}'), ',', '"label":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.label), '}'), ',', '"label_member_identity":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.label_member_identity), '}'), ',', '"cluster_signature":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.cluster_signature), '}'), ',', '"cluster_build_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.cluster_build_version), '}'), ',', '"model_version":', IF(candidate.model_version IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.model_version), '}')), ',', '"discovery_mode":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.discovery_mode), '}'), ',', '"topic_tags":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(candidate.topic_tags) AS item ORDER BY item), ','), ']'), '}'), ',', '"novelty_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(candidate.novelty_score), '}'), ',', '"velocity_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(candidate.velocity_score), '}'), ',', '"breadth_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(candidate.breadth_score), '}'), ',', '"independence_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(candidate.independence_score), '}'), ',', '"historical_similarity":', IF(candidate.historical_similarity IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"float","value":', TO_JSON_STRING(candidate.historical_similarity), '}')), ',', '"geo_confidence":', CONCAT('{"type":"float","value":', TO_JSON_STRING(candidate.geo_confidence), '}'), ',', '"evidence_state":', CONCAT('{"type":"string","value":', TO_JSON_STRING(candidate.evidence_state), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', candidate.created_at, 'UTC'), 'Z')), '}'), '}') AS row_json, candidate.client_scope_id, candidate.signal_date, candidate.market, candidate.signal_id, candidate.run_id FROM `{project}.{dataset}.signal_candidates_v2` AS candidate WHERE candidate.client_scope_id != 'qa_canary'
),
projection_evidence AS (
  SELECT CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(evidence.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(evidence.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.contract_version), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', evidence.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.market), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.signal_id), '}'), ',', '"evidence_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.evidence_id), '}'), ',', '"row_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.row_id), '}'), ',', '"source_family":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.source_family), '}'), ',', '"vendor_family":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.vendor_family), '}'), ',', '"channel_family":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.channel_family), '}'), ',', '"platform":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.platform), '}'), ',', '"url":', IF(evidence.url IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.url), '}')), ',', '"published_at":', IF(evidence.published_at IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', evidence.published_at, 'UTC'), 'Z')), '}')), ',', '"claim_role":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.claim_role), '}'), ',', '"direction":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.direction), '}'), ',', '"geo_confidence":', CONCAT('{"type":"float","value":', TO_JSON_STRING(evidence.geo_confidence), '}'), ',', '"source_label":', IF(evidence.source_label IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.source_label), '}')), ',', '"author_label":', IF(evidence.author_label IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.author_label), '}')), ',', '"excerpt":', IF(evidence.excerpt IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.excerpt), '}')), ',', '"metric_label":', IF(evidence.metric_label IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.metric_label), '}')), ',', '"availability":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.availability), '}'), ',', '"evidence_state":', CONCAT('{"type":"string","value":', TO_JSON_STRING(evidence.evidence_state), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', evidence.created_at, 'UTC'), 'Z')), '}'), '}') AS row_json, evidence.client_scope_id, evidence.signal_date, evidence.market, evidence.signal_id, evidence.evidence_id, evidence.run_id FROM `{project}.{dataset}.signal_evidence_v2` AS evidence WHERE evidence.client_scope_id != 'qa_canary'
),
projection_membership AS (
  SELECT CASE WHEN membership_authority.cluster_build_version IN ('hybrid_graph_v1', 'hybrid_graph_v2') THEN CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.contract_version), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', membership.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.market), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.signal_id), '}'), ',', '"member_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.member_id), '}'), ',', '"member_identity":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.member_identity), '}'), ',', '"candidate_type":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.candidate_type), '}'), ',', '"canonical_value":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.canonical_value), '}'), ',', '"source_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.source_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"vendor_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.vendor_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"channel_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.channel_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"platforms":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.platforms) AS item ORDER BY item), ','), ']'), '}'), ',', '"row_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.row_id), '}'), ',', '"qualifies_evidence":', CONCAT('{"type":"boolean","value":', TO_JSON_STRING(membership.qualifies_evidence), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', membership.created_at, 'UTC'), 'Z')), '}'), '}') WHEN membership_authority.cluster_build_version = 'hybrid_graph_v3' THEN CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.contract_version), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', membership.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.market), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.signal_id), '}'), ',', '"member_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.member_id), '}'), ',', '"member_identity":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.member_identity), '}'), ',', '"candidate_type":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.candidate_type), '}'), ',', '"canonical_value":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.canonical_value), '}'), ',', '"source_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.source_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"vendor_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.vendor_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"channel_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.channel_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"platforms":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(membership.platforms) AS item ORDER BY item), ','), ']'), '}'), ',', '"row_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.row_id), '}'), ',', '"qualifies_evidence":', CONCAT('{"type":"boolean","value":', TO_JSON_STRING(membership.qualifies_evidence), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', membership.created_at, 'UTC'), 'Z')), '}'), ',', '"source_provenance_json":', CONCAT('{"type":"string","value":', TO_JSON_STRING(membership.source_provenance_json), '}'), '}') ELSE NULL END AS row_json, membership.client_scope_id, membership.signal_date, membership.market, membership.signal_id, membership.member_id, membership.run_id FROM `{project}.{dataset}.signal_membership_v2` AS membership JOIN valid_provenance_runs AS membership_authority ON membership_authority.run_id = membership.run_id WHERE membership.client_scope_id != 'qa_canary'
),
row_set_candidates AS (
  SELECT CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_candidates_row.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_candidates_row.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.contract_version), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.signal_id), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_candidates_row.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.market), '}'), ',', '"label":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.label), '}'), ',', '"label_member_identity":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.label_member_identity), '}'), ',', '"cluster_signature":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.cluster_signature), '}'), ',', '"cluster_build_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.cluster_build_version), '}'), ',', '"model_version":', IF(row_set_candidates_row.model_version IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.model_version), '}')), ',', '"discovery_mode":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.discovery_mode), '}'), ',', '"topic_tags":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_candidates_row.topic_tags) AS item ORDER BY item), ','), ']'), '}'), ',', '"novelty_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_candidates_row.novelty_score), '}'), ',', '"velocity_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_candidates_row.velocity_score), '}'), ',', '"breadth_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_candidates_row.breadth_score), '}'), ',', '"independence_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_candidates_row.independence_score), '}'), ',', '"historical_similarity":', IF(row_set_candidates_row.historical_similarity IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_candidates_row.historical_similarity), '}')), ',', '"geo_confidence":', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_candidates_row.geo_confidence), '}'), ',', '"evidence_state":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_candidates_row.evidence_state), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_candidates_row.created_at, 'UTC'), 'Z')), '}'), '}') AS row_json, row_set_candidates_row.client_scope_id, row_set_candidates_row.signal_date, row_set_candidates_row.market, row_set_candidates_row.signal_id, row_set_candidates_row.run_id FROM `{project}.{dataset}.signal_candidates_v2` AS row_set_candidates_row WHERE row_set_candidates_row.client_scope_id != 'qa_canary'
),
row_set_evidence AS (
  SELECT CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_evidence_row.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_evidence_row.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.contract_version), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_evidence_row.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.market), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.signal_id), '}'), ',', '"evidence_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.evidence_id), '}'), ',', '"row_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.row_id), '}'), ',', '"source_family":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.source_family), '}'), ',', '"vendor_family":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.vendor_family), '}'), ',', '"channel_family":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.channel_family), '}'), ',', '"platform":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.platform), '}'), ',', '"url":', IF(row_set_evidence_row.url IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.url), '}')), ',', '"published_at":', IF(row_set_evidence_row.published_at IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_evidence_row.published_at, 'UTC'), 'Z')), '}')), ',', '"claim_role":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.claim_role), '}'), ',', '"direction":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.direction), '}'), ',', '"geo_confidence":', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_evidence_row.geo_confidence), '}'), ',', '"source_label":', IF(row_set_evidence_row.source_label IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.source_label), '}')), ',', '"author_label":', IF(row_set_evidence_row.author_label IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.author_label), '}')), ',', '"excerpt":', IF(row_set_evidence_row.excerpt IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.excerpt), '}')), ',', '"metric_label":', IF(row_set_evidence_row.metric_label IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.metric_label), '}')), ',', '"availability":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.availability), '}'), ',', '"evidence_state":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_evidence_row.evidence_state), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_evidence_row.created_at, 'UTC'), 'Z')), '}'), '}') AS row_json, row_set_evidence_row.client_scope_id, row_set_evidence_row.signal_date, row_set_evidence_row.market, row_set_evidence_row.signal_id, row_set_evidence_row.evidence_id, row_set_evidence_row.run_id FROM `{project}.{dataset}.signal_evidence_v2` AS row_set_evidence_row WHERE row_set_evidence_row.client_scope_id != 'qa_canary'
),
row_set_membership AS (
  SELECT CASE WHEN row_set_membership_row_authority.cluster_build_version IN ('hybrid_graph_v1', 'hybrid_graph_v2') THEN CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.contract_version), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_membership_row.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.market), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.signal_id), '}'), ',', '"member_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.member_id), '}'), ',', '"member_identity":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.member_identity), '}'), ',', '"candidate_type":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.candidate_type), '}'), ',', '"canonical_value":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.canonical_value), '}'), ',', '"source_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.source_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"vendor_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.vendor_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"channel_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.channel_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"platforms":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.platforms) AS item ORDER BY item), ','), ']'), '}'), ',', '"row_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.row_id), '}'), ',', '"qualifies_evidence":', CONCAT('{"type":"boolean","value":', TO_JSON_STRING(row_set_membership_row.qualifies_evidence), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_membership_row.created_at, 'UTC'), 'Z')), '}'), '}') WHEN row_set_membership_row_authority.cluster_build_version = 'hybrid_graph_v3' THEN CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.contract_version), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_membership_row.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.market), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.signal_id), '}'), ',', '"member_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.member_id), '}'), ',', '"member_identity":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.member_identity), '}'), ',', '"candidate_type":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.candidate_type), '}'), ',', '"canonical_value":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.canonical_value), '}'), ',', '"source_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.source_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"vendor_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.vendor_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"channel_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.channel_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"platforms":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_membership_row.platforms) AS item ORDER BY item), ','), ']'), '}'), ',', '"row_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.row_id), '}'), ',', '"qualifies_evidence":', CONCAT('{"type":"boolean","value":', TO_JSON_STRING(row_set_membership_row.qualifies_evidence), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_membership_row.created_at, 'UTC'), 'Z')), '}'), ',', '"source_provenance_json":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_membership_row.source_provenance_json), '}'), '}') ELSE NULL END AS row_json, row_set_membership_row.client_scope_id, row_set_membership_row.signal_date, row_set_membership_row.market, row_set_membership_row.signal_id, row_set_membership_row.member_id, row_set_membership_row.run_id FROM `{project}.{dataset}.signal_membership_v2` AS row_set_membership_row JOIN valid_provenance_runs AS row_set_membership_row_authority ON row_set_membership_row_authority.run_id = row_set_membership_row.run_id WHERE row_set_membership_row.client_scope_id != 'qa_canary'
),
row_set_lineage AS (
  SELECT CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_lineage_row.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_lineage_row.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.contract_version), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_lineage_row.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.market), '}'), ',', '"from_signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.from_signal_id), '}'), ',', '"to_signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.to_signal_id), '}'), ',', '"relation":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_lineage_row.relation), '}'), ',', '"overlap_score":', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_lineage_row.overlap_score), '}'), ',', '"created_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_lineage_row.created_at, 'UTC'), 'Z')), '}'), '}') AS row_json, row_set_lineage_row.client_scope_id, row_set_lineage_row.signal_date, row_set_lineage_row.market, row_set_lineage_row.from_signal_id, row_set_lineage_row.to_signal_id, row_set_lineage_row.relation, row_set_lineage_row.run_id FROM `{project}.{dataset}.signal_lineage_v2` AS row_set_lineage_row WHERE row_set_lineage_row.client_scope_id != 'qa_canary'
),
row_set_predictions AS (
  SELECT CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_predictions_row.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_predictions_row.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.contract_version), '}'), ',', '"prediction_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.prediction_id), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.signal_id), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_predictions_row.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.market), '}'), ',', '"discovery_mode":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.discovery_mode), '}'), ',', '"source_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_predictions_row.source_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"evidence_state":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.evidence_state), '}'), ',', '"first_seen_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_predictions_row.first_seen_at, 'UTC'), 'Z')), '}'), ',', '"predicted_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_predictions_row.predicted_at, 'UTC'), 'Z')), '}'), ',', '"expected_trajectory":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.expected_trajectory), '}'), ',', '"evaluation_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_predictions_row.evaluation_date)), '}'), ',', '"baseline":', CONCAT('{"type":"struct","value":', CONCAT('{', '"velocity":{"type":"float","value":', TO_JSON_STRING(row_set_predictions_row.baseline.velocity), '}', ',', '"breadth":{"type":"float","value":', TO_JSON_STRING(row_set_predictions_row.baseline.breadth), '}', ',', '"evidence_family_count":{"type":"integer","value":', TO_JSON_STRING(row_set_predictions_row.baseline.evidence_family_count), '}', '}'), '}'), ',', '"promotion_target":', CONCAT('{"type":"struct","value":', CONCAT('{', '"velocity":{"type":"float","value":', TO_JSON_STRING(row_set_predictions_row.promotion_target.velocity), '}', ',', '"breadth":{"type":"float","value":', TO_JSON_STRING(row_set_predictions_row.promotion_target.breadth), '}', ',', '"evidence_family_count":{"type":"integer","value":', TO_JSON_STRING(row_set_predictions_row.promotion_target.evidence_family_count), '}', '}'), '}'), ',', '"invalidation_condition":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.invalidation_condition), '}'), ',', '"cluster_build_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.cluster_build_version), '}'), ',', '"source_family_map_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.source_family_map_version), '}'), ',', '"rule_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_predictions_row.rule_version), '}'), ',', '"display_eligible":', CONCAT('{"type":"boolean","value":', TO_JSON_STRING(row_set_predictions_row.display_eligible), '}'), '}') AS row_json, row_set_predictions_row.prediction_id, row_set_predictions_row.run_id FROM `{project}.{dataset}.signal_predictions_v2` AS row_set_predictions_row WHERE row_set_predictions_row.client_scope_id != 'qa_canary'
),
row_set_outcomes AS (
  SELECT CONCAT('{', '"client_scope_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.client_scope_id), '}'), ',', '"market_scope":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_outcomes_row.market_scope) AS item ORDER BY item), ','), ']'), '}'), ',', '"brand_config_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.brand_config_id), '}'), ',', '"audience_lens_ids":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_outcomes_row.audience_lens_ids) AS item ORDER BY item), ','), ']'), '}'), ',', '"theme_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.theme_id), '}'), ',', '"run_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.run_id), '}'), ',', '"contract_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.contract_version), '}'), ',', '"outcome_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.outcome_id), '}'), ',', '"prediction_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.prediction_id), '}'), ',', '"signal_id":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.signal_id), '}'), ',', '"signal_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_outcomes_row.signal_date)), '}'), ',', '"market":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.market), '}'), ',', '"discovery_mode":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.discovery_mode), '}'), ',', '"source_families":', CONCAT('{"type":"repeated","value":', CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT CONCAT('{"type":"string","value":', TO_JSON_STRING(item), '}') FROM UNNEST(row_set_outcomes_row.source_families) AS item ORDER BY item), ','), ']'), '}'), ',', '"source_family_map_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.source_family_map_version), '}'), ',', '"evaluation_date":', CONCAT('{"type":"date","value":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_outcomes_row.evaluation_date)), '}'), ',', '"evaluated_at":', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_outcomes_row.evaluated_at, 'UTC'), 'Z')), '}'), ',', '"outcome":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.outcome), '}'), ',', '"observed_velocity":', IF(row_set_outcomes_row.observed_velocity IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_outcomes_row.observed_velocity), '}')), ',', '"observed_breadth":', IF(row_set_outcomes_row.observed_breadth IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"float","value":', TO_JSON_STRING(row_set_outcomes_row.observed_breadth), '}')), ',', '"observed_evidence_family_count":', IF(row_set_outcomes_row.observed_evidence_family_count IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"integer","value":', TO_JSON_STRING(row_set_outcomes_row.observed_evidence_family_count), '}')), ',', '"human_calibration_label":', IF(row_set_outcomes_row.human_calibration_label IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.human_calibration_label), '}')), ',', '"human_reviewed_at":', IF(row_set_outcomes_row.human_reviewed_at IS NULL, '{"type":"null","value":null}', CONCAT('{"type":"timestamp","value":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', row_set_outcomes_row.human_reviewed_at, 'UTC'), 'Z')), '}')), ',', '"resolution_reason":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.resolution_reason), '}'), ',', '"rule_version":', CONCAT('{"type":"string","value":', TO_JSON_STRING(row_set_outcomes_row.rule_version), '}'), '}') AS row_json, row_set_outcomes_row.prediction_id, row_set_outcomes_row.evaluation_date, row_set_outcomes_row.run_id FROM `{project}.{dataset}.signal_outcomes_v2` AS row_set_outcomes_row WHERE row_set_outcomes_row.client_scope_id != 'qa_canary'
),
row_set_analysis AS (
  SELECT CONCAT('{', '"analysis_id":', TO_JSON_STRING(row_set_analysis_row.analysis_id), ',', '"analyzed_at":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S', row_set_analysis_row.analyzed_at, 'UTC'), 'Z')), ',', '"audience_lens_ids":', TO_JSON_STRING(row_set_analysis_row.audience_lens_ids), ',', '"brand_config_id":', TO_JSON_STRING(row_set_analysis_row.brand_config_id), ',', '"client_scope_id":', TO_JSON_STRING(row_set_analysis_row.client_scope_id), ',', '"contract_version":', TO_JSON_STRING(row_set_analysis_row.contract_version), ',', '"contradictions":', TO_JSON_STRING(row_set_analysis_row.contradictions), ',', '"evidence_ids":', TO_JSON_STRING(row_set_analysis_row.evidence_ids), ',', '"evidence_state":', TO_JSON_STRING(row_set_analysis_row.evidence_state), ',', '"human_review_required":', TO_JSON_STRING(row_set_analysis_row.human_review_required), ',', '"limitations":', TO_JSON_STRING(row_set_analysis_row.limitations), ',', '"market":', TO_JSON_STRING(row_set_analysis_row.market), ',', '"market_scope":', TO_JSON_STRING(row_set_analysis_row.market_scope), ',', '"model_version":', IF(row_set_analysis_row.model_version IS NULL, 'null', TO_JSON_STRING(row_set_analysis_row.model_version)), ',', '"possible_response":', IF(row_set_analysis_row.possible_response IS NULL, 'null', TO_JSON_STRING(row_set_analysis_row.possible_response)), ',', '"run_id":', TO_JSON_STRING(row_set_analysis_row.run_id), ',', '"signal_date":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', row_set_analysis_row.signal_date)), ',', '"signal_id":', TO_JSON_STRING(row_set_analysis_row.signal_id), ',', '"summary":', TO_JSON_STRING(row_set_analysis_row.summary), ',', '"theme_id":', TO_JSON_STRING(row_set_analysis_row.theme_id), ',', '"why_now":', TO_JSON_STRING(row_set_analysis_row.why_now), '}') AS row_json, row_set_analysis_row.run_id FROM `{project}.{dataset}.signal_analysis_v2` AS row_set_analysis_row WHERE row_set_analysis_row.client_scope_id != 'qa_canary'
),
projection_candidates_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, run_id, row_json)) AS payload
  FROM projection_candidates
  GROUP BY run_id
),
projection_evidence_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, evidence_id, run_id, row_json)) AS payload
  FROM projection_evidence
  GROUP BY run_id
),
projection_membership_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, member_id, run_id, row_json)) AS payload
  FROM projection_membership
  GROUP BY run_id
),
row_set_analysis_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY row_json)) AS payload
  FROM row_set_analysis
  GROUP BY run_id
),
row_set_candidates_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, run_id, row_json)) AS payload
  FROM row_set_candidates
  GROUP BY run_id
),
row_set_evidence_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, evidence_id, run_id, row_json)) AS payload
  FROM row_set_evidence
  GROUP BY run_id
),
row_set_lineage_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, from_signal_id, to_signal_id, relation, run_id, row_json)) AS payload
  FROM row_set_lineage
  GROUP BY run_id
),
row_set_membership_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY client_scope_id, signal_date, market, signal_id, member_id, run_id, row_json)) AS payload
  FROM row_set_membership
  GROUP BY run_id
),
row_set_outcomes_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY prediction_id, evaluation_date, run_id, row_json)) AS payload
  FROM row_set_outcomes
  GROUP BY run_id
),
row_set_predictions_agg AS (
  SELECT run_id, TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY prediction_id, row_json)) AS payload
  FROM row_set_predictions
  GROUP BY run_id
),
packet_items_agg AS (
  SELECT run_id, ARRAY_TO_STRING(ARRAY_AGG(item ORDER BY market, signal_id, evidence_id), ',') AS payload
  FROM (
    SELECT e.run_id, e.market, e.signal_id, e.evidence_id, CONCAT('{', '"author_label":', TO_JSON_STRING(e.author_label), ',', '"evidence_id":', TO_JSON_STRING(e.evidence_id), ',', '"excerpt":', TO_JSON_STRING(e.excerpt), ',', '"market":', TO_JSON_STRING(e.market), ',', '"platform":', TO_JSON_STRING(e.platform), ',', '"published_at":', IF(e.published_at IS NULL, 'null', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', e.published_at, 'UTC'), 'Z'))), ',', '"row_id":', TO_JSON_STRING(e.row_id), ',', '"run_id":', TO_JSON_STRING(e.run_id), ',', '"signal_id":', TO_JSON_STRING(e.signal_id), ',', '"source_family":', TO_JSON_STRING(e.source_family), ',', '"source_label":', TO_JSON_STRING(e.source_label), ',', '"url":', TO_JSON_STRING(e.url), '}') AS item
    FROM `{project}.{dataset}.signal_evidence_v2` e
    WHERE e.client_scope_id != 'qa_canary'
  )
  GROUP BY run_id
),
run_receipts AS (
  SELECT
    run_id,
    client_scope_id,
    market_scope,
    signal_date,
    observation_start,
    observation_end,
    observation_method,
    source_window_digest,
    cluster_build_version,
    source_family_map_version,
    rule_version,
    candidate_count,
    evidence_count,
    membership_count,
    lineage_count,
    analysis_count,
    prediction_count,
    row_set_digest,
    source_sha,
    completed_at,
    run_contract_version,
    status,
    complete_partitions,
    display_release_state
  FROM `{project}.{dataset}.open_intelligence_run_receipts_v1`
  WHERE client_scope_id != 'qa_canary'
    AND run_id IN (SELECT run_id FROM valid_provenance_runs)
),
quality_releases AS (
  SELECT
    run_id,
    run_receipt_digest,
    source_window_digest,
    candidate_projection_digest,
    packet_digest,
    review_receipt_digest,
    approval_addendum_sha256,
    released_at,
    release_contract_version
  FROM `{project}.{dataset}.open_intelligence_quality_release_records_v2`
  WHERE release_contract_version = 'open_intelligence_quality_release_v2'
),
quality_authority AS (
  SELECT
    q.run_id,
    q.run_receipt_digest,
    q.source_window_digest,
    q.candidate_projection_digest,
    q.packet_digest,
    q.review_receipt_digest,
    q.approval_addendum_sha256,
    q.released_at,
    q.release_contract_version
  FROM quality_releases q
  JOIN run_receipts r USING (run_id)
  LEFT JOIN packet_items_agg ON packet_items_agg.run_id = q.run_id
  LEFT JOIN projection_candidates_agg ON projection_candidates_agg.run_id = q.run_id
  LEFT JOIN projection_evidence_agg ON projection_evidence_agg.run_id = q.run_id
  LEFT JOIN projection_membership_agg ON projection_membership_agg.run_id = q.run_id
  LEFT JOIN row_set_analysis_agg ON row_set_analysis_agg.run_id = q.run_id
  LEFT JOIN row_set_candidates_agg ON row_set_candidates_agg.run_id = q.run_id
  LEFT JOIN row_set_evidence_agg ON row_set_evidence_agg.run_id = q.run_id
  LEFT JOIN row_set_lineage_agg ON row_set_lineage_agg.run_id = q.run_id
  LEFT JOIN row_set_membership_agg ON row_set_membership_agg.run_id = q.run_id
  LEFT JOIN row_set_outcomes_agg ON row_set_outcomes_agg.run_id = q.run_id
  LEFT JOIN row_set_predictions_agg ON row_set_predictions_agg.run_id = q.run_id
  WHERE q.run_receipt_digest = LOWER(TO_HEX(SHA256(CONCAT('{', '"analysis_count":', TO_JSON_STRING(r.analysis_count), ',', '"candidate_count":', TO_JSON_STRING(r.candidate_count), ',', '"client_scope_id":', TO_JSON_STRING(r.client_scope_id), ',', '"cluster_build_version":', TO_JSON_STRING(r.cluster_build_version), ',', '"complete_partitions":', TO_JSON_STRING(r.complete_partitions), ',', '"completed_at":', TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S', r.completed_at, 'UTC'), 'Z')), ',', '"display_release_state":', TO_JSON_STRING(r.display_release_state), ',', '"evidence_count":', TO_JSON_STRING(r.evidence_count), ',', '"lineage_count":', TO_JSON_STRING(r.lineage_count), ',', '"market_scope":', TO_JSON_STRING(r.market_scope), ',', '"membership_count":', TO_JSON_STRING(r.membership_count), ',', '"observation_end":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', r.observation_end)), ',', '"observation_method":', TO_JSON_STRING(r.observation_method), ',', '"observation_start":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', r.observation_start)), ',', '"prediction_count":', TO_JSON_STRING(r.prediction_count), ',', '"row_set_digest":', TO_JSON_STRING(r.row_set_digest), ',', '"rule_version":', TO_JSON_STRING(r.rule_version), ',', '"run_contract_version":', TO_JSON_STRING(r.run_contract_version), ',', '"run_id":', TO_JSON_STRING(r.run_id), ',', '"signal_date":', TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', r.signal_date)), ',', '"source_family_map_version":', TO_JSON_STRING(r.source_family_map_version), ',', '"source_sha":', TO_JSON_STRING(r.source_sha), ',', '"source_window_digest":', TO_JSON_STRING(r.source_window_digest), ',', '"status":', TO_JSON_STRING(r.status), '}'))))
    AND q.source_window_digest = r.source_window_digest
    AND q.candidate_projection_digest = (SELECT LOWER(TO_HEX(SHA256(CONCAT('{"candidates":', IFNULL(projection_candidates_agg.payload, 'null'), ',"evidence":', IFNULL(projection_evidence_agg.payload, 'null'), ',"membership":', IFNULL(projection_membership_agg.payload, 'null'), ',"projection_contract_version":"dynamic_quality_projection_v2"', ',"run_id":', TO_JSON_STRING(CAST(q.run_id AS STRING)), '}')))))
    AND r.row_set_digest = (SELECT LOWER(TO_HEX(SHA256(CONCAT('{"analysis":', IFNULL(row_set_analysis_agg.payload, 'null'), ',"candidates":', IFNULL(row_set_candidates_agg.payload, 'null'), ',"evidence":', IFNULL(row_set_evidence_agg.payload, 'null'), ',"lineage":', IFNULL(row_set_lineage_agg.payload, 'null'), ',"membership":', IFNULL(row_set_membership_agg.payload, 'null'), ',"outcomes":', IFNULL(row_set_outcomes_agg.payload, 'null'), ',"predictions":', IFNULL(row_set_predictions_agg.payload, 'null'), '}')))))
    AND q.packet_digest = LOWER(TO_HEX(SHA256(CONCAT('{"review_contract_version":"dynamic_quality_review_v1"', ',"review_items":', IFNULL((SELECT CONCAT('[', IFNULL(packet_items_agg.payload, ''), ']')), '[]'), ',"run_id":', TO_JSON_STRING(CAST(q.run_id AS STRING)), '}'))))
),
predictions AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    prediction_id,
    predicted_at,
    expected_trajectory,
    display_eligible
  FROM `{project}.{dataset}.signal_predictions_v2`
  WHERE client_scope_id != 'qa_canary'
    AND NOT display_eligible
),
candidates AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    label,
    label_member_identity,
    evidence_state,
    velocity_score,
    novelty_score,
    breadth_score,
    independence_score,
    historical_similarity,
    geo_confidence,
    topic_tags
  FROM `{project}.{dataset}.signal_candidates_v2`
  WHERE client_scope_id != 'qa_canary'
),
memberships AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    member_identity,
    canonical_value,
    candidate_type
  FROM `{project}.{dataset}.signal_membership_v2`
  WHERE client_scope_id != 'qa_canary'
),
evidence AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    evidence_id,
    row_id,
    url,
    platform,
    source_family,
    source_label,
    author_label,
    excerpt,
    metric_label,
    published_at,
    direction,
    evidence_state,
    availability
  FROM `{project}.{dataset}.signal_evidence_v2`
  WHERE client_scope_id != 'qa_canary'
),
analysis AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    why_now,
    possible_response
  FROM `{project}.{dataset}.signal_analysis_v2`
  WHERE client_scope_id != 'qa_canary'
),
released_runs AS (
  SELECT
    run_id,
    client_scope_id,
    market_scope,
    signal_date,
    observation_start,
    observation_end,
    observation_method,
    completed_at,
    q.released_at AS released_at
  FROM run_receipts r
  JOIN quality_authority q USING (run_id)
  WHERE run_contract_version = 'open_intelligence_run_receipt_v1'
    AND status = 'completed'
    AND complete_partitions
    AND display_release_state = 'enabled'
    AND observation_start <= observation_end
    AND signal_date = observation_end
    AND observation_end < CURRENT_DATE('Africa/Johannesburg')
    AND q.source_window_digest = r.source_window_digest
),
released_predictions AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    prediction_id,
    predicted_at,
    expected_trajectory
  FROM predictions
),
available_receipts AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    ARRAY_AGG(
      STRUCT(
        evidence_id AS evidence_id,
        url AS url,
        platform AS platform,
        source_family AS source_family,
        author_label AS author_label,
        excerpt AS excerpt,
        metric_label AS metric_label,
        published_at AS published_at
      )
      ORDER BY evidence_id
    ) AS receipts
  FROM evidence
  WHERE availability = 'available'
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id
),
same_run_analysis AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    COUNT(*) AS analysis_row_count,
    MIN(why_now) AS why_now,
    MIN(possible_response) AS possible_response
  FROM analysis
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id
),
instrument_candidates AS (
  SELECT
    e.client_scope_id,
    e.run_id,
    e.signal_date,
    e.market,
    e.signal_id,
    e.evidence_id,
    e.source_family,
    COALESCE(NULLIF(TRIM(e.source_label), ''), e.platform) AS source_name,
    e.url,
    IF(REGEXP_CONTAINS(e.url, r'^https?://'), NULL, COALESCE(e.row_id, e.evidence_id)) AS record_authority,
    e.published_at,
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', e.published_at) AS observed_at,
    e.excerpt,
    CASE
      WHEN e.direction = 'not_applicable' THEN 'neutral'
      WHEN e.direction IN ('rising', 'declining')
        AND p.expected_trajectory IN ('fading', 'declining')
        THEN IF(e.direction = 'declining', 'supporting', 'opposing')
      WHEN e.direction IN ('rising', 'declining')
        AND p.expected_trajectory IN ('rising', 'emerging', 'surging', 'growing')
        THEN IF(e.direction = 'rising', 'supporting', 'opposing')
      ELSE 'unknown'
    END AS receipt_direction,
    e.evidence_state = 'ready'
      AND e.published_at >= TIMESTAMP(r.observation_start)
      AND e.published_at < TIMESTAMP(DATE_ADD(r.observation_end, INTERVAL 1 DAY)) AS in_window_ready
  FROM evidence e
  JOIN released_predictions p
    ON p.client_scope_id = e.client_scope_id
    AND p.run_id = e.run_id
    AND p.signal_date = e.signal_date
    AND p.market = e.market
    AND p.signal_id = e.signal_id
  JOIN released_runs r
    ON r.client_scope_id = e.client_scope_id
    AND r.run_id = e.run_id
    AND r.signal_date = e.signal_date
  WHERE e.availability = 'available'
    AND e.published_at IS NOT NULL
    AND e.excerpt IS NOT NULL
    AND TRIM(e.excerpt) != ''
),
instrument_withheld AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    COUNTIF(published_at IS NULL) AS without_time,
    COUNTIF(published_at IS NOT NULL AND (excerpt IS NULL OR TRIM(excerpt) = '')) AS without_excerpt
  FROM evidence
  WHERE availability = 'available'
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id
),
instrument_stance AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    COUNTIF(in_window_ready AND receipt_direction = 'supporting') AS provisional_supporting,
    COUNTIF(in_window_ready AND receipt_direction = 'opposing') AS provisional_opposing
  FROM instrument_candidates
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id
),
instrument_receipts AS (
  SELECT
    x.client_scope_id,
    x.run_id,
    x.signal_date,
    x.market,
    x.signal_id,
    x.evidence_id,
    x.source_family,
    x.source_name,
    x.url,
    x.record_authority,
    x.published_at,
    x.observed_at,
    x.excerpt,
    x.receipt_direction,
    CASE
      WHEN x.in_window_ready
        AND x.receipt_direction IN ('supporting', 'opposing')
        AND st.provisional_supporting > 0
        THEN 'qualifying'
      WHEN x.in_window_ready
        AND x.receipt_direction = 'opposing'
        AND st.provisional_supporting = 0
        THEN 'unchecked'
      WHEN x.receipt_direction = 'neutral' THEN 'thin'
      ELSE 'unchecked'
    END AS quality,
    st.provisional_supporting = 0 AND st.provisional_opposing > 0 AS opposing_only
  FROM instrument_candidates x
  JOIN instrument_stance st
    ON st.client_scope_id = x.client_scope_id
    AND st.run_id = x.run_id
    AND st.signal_date = x.signal_date
    AND st.market = x.market
    AND st.signal_id = x.signal_id
),
instrument_summary AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    ARRAY_AGG(
      STRUCT(
        evidence_id AS id,
        source_name AS sourceName,
        source_family AS familyId,
        url AS url,
        record_authority AS recordAuthority,
        observed_at AS observedAt,
        receipt_direction AS direction,
        excerpt AS excerpt,
        quality AS quality,
        quality = 'qualifying' AS qualifying
      )
      ORDER BY published_at, evidence_id
    ) AS receipts,
    ARRAY_AGG(IF(receipt_direction = 'supporting', evidence_id, NULL) IGNORE NULLS ORDER BY published_at, evidence_id) AS supporting_ids,
    ARRAY_AGG(IF(receipt_direction = 'opposing', evidence_id, NULL) IGNORE NULLS ORDER BY published_at, evidence_id) AS opposing_ids,
    COUNTIF(quality = 'qualifying') AS qualifying_count,
    COUNTIF(quality = 'qualifying' AND receipt_direction = 'supporting') AS qualifying_supporting,
    COUNTIF(quality = 'qualifying' AND receipt_direction = 'opposing') AS qualifying_opposing,
    COUNT(DISTINCT IF(quality = 'qualifying', source_family, NULL)) AS family_count,
    LOGICAL_OR(opposing_only) AS opposing_only
  FROM instrument_receipts
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id
),
instrument_points AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    source_family,
    evidence_id,
    published_at,
    observed_at,
    receipt_direction,
    ROUND(
      SAFE_DIVIDE(
        COUNT(*) OVER (
          PARTITION BY client_scope_id, run_id, signal_date, market, signal_id, source_family
          ORDER BY UNIX_SECONDS(TIMESTAMP_TRUNC(published_at, SECOND))
          RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ),
        COUNT(*) OVER (
          PARTITION BY client_scope_id, run_id, signal_date, market, signal_id, source_family
        )
      ),
      6
    ) AS share
  FROM instrument_receipts
  WHERE quality = 'qualifying'
),
instrument_point_set AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    source_family,
    MIN(published_at) AS published_at,
    observed_at,
    share
  FROM instrument_points
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    source_family,
    observed_at,
    share
),
instrument_family_points AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    source_family,
    COUNT(*) AS distinct_times,
    ARRAY_AGG(STRUCT(observed_at AS `at`, share AS value) ORDER BY published_at) AS receipt_points
  FROM instrument_point_set
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    source_family
),
instrument_strands AS (
  SELECT
    i.client_scope_id,
    i.run_id,
    i.signal_date,
    i.market,
    i.signal_id,
    i.source_family,
    MIN(i.published_at) AS first_at,
    MIN(f.distinct_times) AS distinct_times,
    ARRAY_AGG(i.evidence_id ORDER BY i.published_at, i.evidence_id) AS receipt_ids,
    ARRAY_AGG(
      STRUCT(i.evidence_id AS receiptId, i.observed_at AS `at`, i.share AS value)
      ORDER BY i.published_at, i.evidence_id
    ) AS receipt_anchors,
    ANY_VALUE(f.receipt_points) AS receipt_points,
    LOGICAL_AND(i.receipt_direction = 'supporting') AS all_supporting,
    LOGICAL_AND(i.receipt_direction = 'opposing') AS all_opposing
  FROM instrument_points i
  JOIN instrument_family_points f
    ON f.client_scope_id = i.client_scope_id
    AND f.run_id = i.run_id
    AND f.signal_date = i.signal_date
    AND f.market = i.market
    AND f.signal_id = i.signal_id
    AND f.source_family = i.source_family
  GROUP BY
    i.client_scope_id,
    i.run_id,
    i.signal_date,
    i.market,
    i.signal_id,
    i.source_family
),
instrument_series AS (
  SELECT
    s.client_scope_id,
    s.run_id,
    s.signal_date,
    s.market,
    s.signal_id,
    LOGICAL_OR(NOT s.all_supporting AND NOT s.all_opposing) AS mixed_family,
    ARRAY_AGG(
      STRUCT(
        s.source_family AS familyId,
        INITCAP(REPLACE(s.source_family, '_', ' ')) AS familyLabel,
        'channel_family_v2' AS independenceAuthority,
        IF(s.all_supporting, 'supporting', IF(s.all_opposing, 'opposing', 'unknown')) AS direction,
        'qualifying' AS quality,
        STRUCT(
          FORMAT_DATE('%Y-%m-%d', r.observation_start) AS start,
          FORMAT_DATE('%Y-%m-%d', r.observation_end) AS `end`,
          'receipt' AS `interval`
        ) AS axis,
        'cumulative_receipt_share_0_1' AS normalization,
        STRUCT(0.0 AS min, 1.0 AS max) AS valueDomain,
        ARRAY_CONCAT(
          IF(
            TIMESTAMP_TRUNC(s.first_at, SECOND) > TIMESTAMP(r.observation_start),
            [STRUCT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', TIMESTAMP(r.observation_start)) AS `at`, 0.0 AS value)],
            []
          ),
          s.receipt_points,
          IF(
            TIMESTAMP_TRUNC(s.first_at, SECOND) <= TIMESTAMP(r.observation_start) AND s.distinct_times = 1,
            [STRUCT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', TIMESTAMP_SUB(TIMESTAMP(DATE_ADD(r.observation_end, INTERVAL 1 DAY)), INTERVAL 1 SECOND)) AS `at`, 1.0 AS value)],
            []
          )
        ) AS points,
        s.receipt_ids AS receiptIds,
        s.receipt_anchors AS receiptAnchors
      )
      ORDER BY s.source_family
    ) AS strands
  FROM instrument_strands s
  JOIN released_runs r
    ON r.client_scope_id = s.client_scope_id
    AND r.run_id = s.run_id
    AND r.signal_date = s.signal_date
  GROUP BY
    s.client_scope_id,
    s.run_id,
    s.signal_date,
    s.market,
    s.signal_id
),
instrument_signal AS (
  SELECT
    p.client_scope_id,
    p.run_id,
    p.signal_date,
    p.market,
    p.signal_id,
    STRUCT(
      '1.0.0' AS contractVersion,
      CASE
        WHEN IFNULL(ARRAY_LENGTH(u.receipts), 0) = 0 THEN 'unchecked'
        WHEN u.qualifying_supporting > 0 AND u.qualifying_opposing > 0 THEN 'contradictory'
        WHEN u.qualifying_count > 0 AND u.qualifying_opposing = 0 AND u.family_count >= 2 THEN 'ready'
        ELSE 'thin'
      END AS state,
      IFNULL(u.receipts, []) AS receipts,
      STRUCT(
        'validated' AS status,
        IFNULL(u.family_count, 0) AS familyCount,
        'channel_family_v2' AS groupingAuthority
      ) AS independence,
      STRUCT(
        CASE
          WHEN u.qualifying_supporting > 0 AND u.qualifying_opposing > 0 THEN 'mixed'
          WHEN u.qualifying_supporting > 0 AND u.qualifying_opposing = 0 THEN 'agree'
          ELSE 'unknown'
        END AS status,
        IFNULL(u.supporting_ids, []) AS supportingReceiptIds,
        IFNULL(u.opposing_ids, []) AS opposingReceiptIds
      ) AS direction,
      STRUCT(
        FORMAT_DATE('%Y-%m-%d', r.observation_start) AS start,
        FORMAT_DATE('%Y-%m-%d', r.observation_end) AS `end`,
        r.observation_method AS method,
        TRUE AS closed
      ) AS `window`,
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E3SZ', r.released_at) AS checkedAt,
      ARRAY_CONCAT(
        ['strand values are the cumulative share of one source family qualifying receipts observed by that time, not audience reach'],
        IF(IFNULL(w.without_time, 0) > 0, [FORMAT('%d available receipts withheld without a publish time', w.without_time)], []),
        IF(IFNULL(w.without_excerpt, 0) > 0, [FORMAT('%d available receipts withheld without an excerpt', w.without_excerpt)], []),
        IF(IFNULL(u.opposing_only, FALSE), ['every directed receipt opposes the predicted trajectory; none qualifies'], []),
        IF(IFNULL(v.mixed_family, FALSE), ['a source family carries both supporting and opposing receipts; the ribbon is empty'], [])
      ) AS limitations
    ) AS evidence_summary,
    CASE
      WHEN IFNULL(u.family_count, 0) = 0 THEN []
      WHEN v.mixed_family THEN []
      ELSE v.strands
    END AS ribbon_series
  FROM released_predictions p
  JOIN released_runs r
    ON r.client_scope_id = p.client_scope_id
    AND r.run_id = p.run_id
    AND r.signal_date = p.signal_date
  LEFT JOIN instrument_summary u
    ON u.client_scope_id = p.client_scope_id
    AND u.run_id = p.run_id
    AND u.signal_date = p.signal_date
    AND u.market = p.market
    AND u.signal_id = p.signal_id
  LEFT JOIN instrument_withheld w
    ON w.client_scope_id = p.client_scope_id
    AND w.run_id = p.run_id
    AND w.signal_date = p.signal_date
    AND w.market = p.market
    AND w.signal_id = p.signal_id
  LEFT JOIN instrument_series v
    ON v.client_scope_id = p.client_scope_id
    AND v.run_id = p.run_id
    AND v.signal_date = p.signal_date
    AND v.market = p.market
    AND v.signal_id = p.signal_id
)
SELECT
  'desk_dynamic_signal_v2' AS contract_version,
  r.run_id AS run_id,
  r.client_scope_id AS client_scope_id,
  r.market_scope AS market_scope,
  r.signal_date AS signal_date,
  r.observation_start AS observation_start,
  r.observation_end AS observation_end,
  r.observation_method AS observation_method,
  r.completed_at AS run_completed_at,
  c.signal_id AS signal_id,
  c.market AS market,
  c.label AS signal_name,
  CASE m.candidate_type
    WHEN 'keyword' THEN 'phrase'
    WHEN 'slang' THEN 'phrase'
    WHEN 'headline' THEN 'phrase'
    WHEN 'hashtag' THEN 'hashtag'
    WHEN 'sound' THEN 'sound'
    WHEN 'creator' THEN 'creator'
    WHEN 'entity' THEN 'entity'
  END AS discovery_mode,
  c.evidence_state AS evidence_state,
  IF(a.analysis_row_count = 1, a.why_now, NULL) AS why_now,
  IF(a.analysis_row_count = 1, a.possible_response, NULL) AS possible_response,
  p.prediction_id AS prediction_id,
  p.predicted_at AS predicted_at,
  IFNULL(e.receipts, []) AS receipts,
  c.velocity_score AS velocity_score,
  c.novelty_score AS novelty_score,
  c.breadth_score AS breadth_score,
  c.independence_score AS independence_score,
  c.historical_similarity AS historical_similarity,
  c.geo_confidence AS geo_confidence,
  c.topic_tags AS topic_tags,
  s.evidence_summary AS evidence_summary,
  s.ribbon_series AS ribbon_series
FROM released_runs r
JOIN candidates c
  ON c.client_scope_id = r.client_scope_id
  AND c.run_id = r.run_id
  AND c.signal_date = r.signal_date
JOIN released_predictions p
  ON p.client_scope_id = c.client_scope_id
  AND p.run_id = c.run_id
  AND p.signal_date = c.signal_date
  AND p.market = c.market
  AND p.signal_id = c.signal_id
JOIN memberships m
  ON m.client_scope_id = c.client_scope_id
  AND m.run_id = c.run_id
  AND m.signal_date = c.signal_date
  AND m.market = c.market
  AND m.signal_id = c.signal_id
  AND m.member_identity = c.label_member_identity
  AND m.canonical_value = c.label
LEFT JOIN available_receipts e
  ON e.client_scope_id = c.client_scope_id
  AND e.run_id = c.run_id
  AND e.signal_date = c.signal_date
  AND e.market = c.market
  AND e.signal_id = c.signal_id
LEFT JOIN same_run_analysis a
  ON a.client_scope_id = c.client_scope_id
  AND a.run_id = c.run_id
  AND a.signal_date = c.signal_date
  AND a.market = c.market
  AND a.signal_id = c.signal_id
LEFT JOIN instrument_signal s
  ON s.client_scope_id = c.client_scope_id
  AND s.run_id = c.run_id
  AND s.signal_date = c.signal_date
  AND s.market = c.market
  AND s.signal_id = c.signal_id
WHERE c.market IN UNNEST(r.market_scope);
