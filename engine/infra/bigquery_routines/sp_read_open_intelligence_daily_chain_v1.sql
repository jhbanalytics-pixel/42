CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_daily_chain_v1`(
  derivation_id STRING, canonical_execution_observation_json STRING, execution_observation_sha256 STRING
)
BEGIN
  DECLARE v_id STRING DEFAULT derivation_id;
  DECLARE v_observation_digest STRING DEFAULT execution_observation_sha256;
  DECLARE v_consumption STRUCT<consumption_id STRING,execution_name STRING,consumed_at TIMESTAMP,origin_registry_sha256 STRING,resource_manifest_sha256 STRING>;
  ASSERT `{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_execution_observation_json)
    AND LOWER(TO_HEX(SHA256(canonical_execution_observation_json)))=v_observation_digest
    AND JSON_VALUE(canonical_execution_observation_json,'$.derivation_id')=v_id AS 'daily_execution_observation_invalid';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` WHERE derivation_id=v_id)=1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE approval_id=v_id)=1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_results_v2` r JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c USING(consumption_id) WHERE c.approval_id=v_id)=1
    AS 'daily_chain_unavailable';
  SET v_consumption=(SELECT AS STRUCT consumption_id,execution_name,consumed_at,origin_registry_sha256,resource_manifest_sha256
    FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE approval_id=v_id);
  ASSERT v_consumption.consumption_id=CONCAT('exc_',LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(v_consumption.consumed_at AS consumed_at,'open_intelligence_execution_consumption_v3' AS consumption_contract_version,v_id AS derivation_id,v_consumption.execution_name AS execution_name,v_observation_digest AS execution_observation_sha256,v_consumption.origin_registry_sha256 AS origin_registry_sha256,v_consumption.resource_manifest_sha256 AS resource_manifest_sha256))))))
    AS 'daily_chain_integrity_invalid';
  SELECT TO_JSON(d) AS derivation,TO_JSON(a) AS authorizing_approval,SAFE.PARSE_JSON(a.canonical_manifest_json,wide_number_mode=>'exact') AS grant,
    SAFE.PARSE_JSON(d.canonical_manifest_json,wide_number_mode=>'exact') AS manifest,
    SAFE.PARSE_JSON(d.canonical_operation_context_json,wide_number_mode=>'exact') AS operation_context,
    TO_JSON(c) AS consumption,TO_JSON(r) AS result,
    JSON_QUERY(r.canonical_result_json,'$.operation_payload') AS canonical_operation_payload_json
  FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
  JOIN `{project}.{dataset}.open_intelligence_execution_approvals_v2` a ON a.approval_id=d.authorizing_approval_id AND a.manifest_sha256=d.authorizing_grant_digest
  JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
  JOIN `{project}.{dataset}.open_intelligence_execution_results_v2` r ON r.consumption_id=c.consumption_id
  WHERE d.derivation_id=v_id;
END;
