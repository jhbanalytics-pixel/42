CREATE OR REPLACE PROCEDURE `{project}.{dataset}.sp_read_open_intelligence_daily_derivation_v1`(derivation_id STRING)
BEGIN
  DECLARE v_id STRING DEFAULT derivation_id;
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` WHERE derivation_id=v_id)=1 AS 'daily_derivation_unavailable';
  ASSERT (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` WHERE derivation_id=v_id)<=1
    AND (SELECT COUNT(*) FROM `{project}.{dataset}.open_intelligence_execution_consumptions_v2` WHERE approval_id=v_id)<=1 AS 'daily_derivation_ambiguous';
  SELECT TO_JSON(d) AS derivation,
    CASE WHEN t.derivation_id IS NULL THEN NULL ELSE TO_JSON(t) END AS tombstone,
    CASE WHEN t.derivation_id IS NOT NULL THEN 'cancelled' WHEN c.consumption_id IS NOT NULL THEN 'consumed'
      WHEN d.expires_at<=CURRENT_TIMESTAMP() THEN 'expired' ELSE 'derived' END AS lifecycle_state,
    CASE WHEN c.consumption_id IS NULL THEN NULL ELSE TO_JSON(c) END AS consumption
  FROM `{project}.{dataset}.open_intelligence_execution_derivations_v1` d
  LEFT JOIN `{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1` t USING(derivation_id)
  LEFT JOIN `{project}.{dataset}.open_intelligence_execution_consumptions_v2` c ON c.approval_id=d.derivation_id
  WHERE d.derivation_id=v_id;
END;
