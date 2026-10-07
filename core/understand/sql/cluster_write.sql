-- Write one clustering run, or one batch of it (BUILD.md 2.2, DATA.md section 4), as one transaction.
-- Parameters: @run_date DATE, @market STRING, and @map_rows, @cluster_rows and @member_rows, STRING, JSON arrays
-- written by cluster.plan and split by cluster.write_batches, so the job passes scalar parameters only. A run whose
-- parameters would pass 8 MB is sent as several batches of whole clusters, each through this script.
-- cultural_map is versioned, never overwritten. A map row with change 'update' closes the item's open row
-- (valid_to set to now, nothing else on it changes) and opens a new version (valid_from now); a row with change
-- 'insert' opens a new item, and only when no row of that item_id exists yet. Closing and opening happen in the
-- one MERGE, so the closed row's valid_to equals the new row's valid_from. Only the source row with no close_key
-- opens a version: a close row that finds no open row, because another run's MERGE closed it first, opens nothing,
-- so the item keeps one open version. A new version's last_seen is GREATEST(the item's latest last_seen, the run's), so
-- a write for an earlier day never moves last_seen back; the job clusters only today, and this is a second guard.
-- clusters and cluster_members are appended.
-- Each batch is written at most once: when clusters already holds a row for @run_date and @market with one of the
-- batch's cluster ids, the staged rows are empty and every statement below adds and closes nothing. The guard is
-- read in the transaction's snapshot before this batch writes. A pending update whose original valid_from is no
-- longer current holds the whole batch; it cannot close a newer map version. A whole rerun is
-- stopped only when cluster_done.sql establishes that every id in the saved plan is present. When a batch fails,
-- cluster.run_cluster raises with the batch index and unwritten cluster ids; a retry replays the saved batches.
-- The last statement returns the counts written: map_rows, cluster_rows and member_rows, and the cluster rows by
-- match kind (matched, recurrences, variants, new_items), plus stale_updates for a held batch.

BEGIN TRANSACTION;

CREATE TEMP TABLE IF NOT EXISTS batch_written AS
SELECT COUNT(*) AS n
FROM `ogilvy-trends-v2.intelligence_42_core.clusters` AS k
WHERE k.cluster_date = @run_date AND k.market = @market
  AND k.cluster_id IN (SELECT JSON_VALUE(b, '$.cluster_id') FROM UNNEST(JSON_QUERY_ARRAY(@cluster_rows)) AS b);

CREATE TEMP TABLE IF NOT EXISTS map_conflicts AS
SELECT COUNT(*) AS n
FROM UNNEST(JSON_QUERY_ARRAY(@map_rows)) AS j
LEFT JOIN (
  SELECT cm.item_id, MAX(cm.valid_from) AS valid_from
  FROM `ogilvy-trends-v2.intelligence_42_core.cultural_map` AS cm
  WHERE cm.valid_to IS NULL
  GROUP BY cm.item_id
) AS current_map ON current_map.item_id = JSON_VALUE(j, '$.item_id')
WHERE (SELECT w.n FROM batch_written AS w) = 0
  AND JSON_VALUE(j, '$.change') = 'update'
  AND (current_map.valid_from IS NULL
    OR JSON_VALUE(j, '$.expected_valid_from') IS NULL
    OR current_map.valid_from != CAST(JSON_VALUE(j, '$.expected_valid_from') AS TIMESTAMP));

CREATE TEMP TABLE IF NOT EXISTS map_changes AS
SELECT
  JSON_VALUE(j, '$.change') AS change,
  JSON_VALUE(j, '$.item_id') AS item_id,
  CAST(JSON_VALUE(j, '$.expected_valid_from') AS TIMESTAMP) AS expected_valid_from,
  JSON_VALUE(j, '$.kind') AS kind,
  JSON_VALUE(j, '$.canonical_key') AS canonical_key,
  JSON_VALUE(j, '$.label') AS label,
  JSON_VALUE_ARRAY(j, '$.aliases') AS aliases,
  JSON_VALUE(j, '$.parent_item_id') AS parent_item_id,
  ARRAY(
    SELECT CAST(x AS FLOAT64) FROM UNNEST(JSON_VALUE_ARRAY(j, '$.centroid')) AS x WITH OFFSET AS o ORDER BY o
  ) AS centroid,
  CAST(JSON_VALUE(j, '$.first_seen') AS DATE) AS first_seen,
  JSON_VALUE(j, '$.first_seen_market') AS first_seen_market,
  JSON_VALUE(j, '$.first_seen_platform') AS first_seen_platform,
  CAST(JSON_VALUE(j, '$.last_seen') AS DATE) AS last_seen,
  CAST(JSON_VALUE(j, '$.recurrences') AS INT64) AS recurrences,
  JSON_VALUE(j, '$.lifecycle') AS lifecycle,
  JSON_VALUE(j, '$.status') AS status,
  CAST(JSON_VALUE(j, '$.rejected_until') AS DATE) AS rejected_until
FROM UNNEST(JSON_QUERY_ARRAY(@map_rows)) AS j
WHERE (SELECT w.n FROM batch_written AS w) = 0 AND (SELECT c.n FROM map_conflicts AS c) = 0;

CREATE TEMP TABLE IF NOT EXISTS cluster_changes AS
SELECT
  JSON_VALUE(j, '$.cluster_id') AS cluster_id,
  JSON_VALUE(j, '$.item_id') AS item_id,
  JSON_VALUE(j, '$.match_kind') AS match_kind,
  JSON_VALUE(j, '$.label') AS label,
  JSON_VALUE_ARRAY(j, '$.keywords') AS keywords,
  JSON_VALUE_ARRAY(j, '$.local_terms') AS local_terms,
  ARRAY(
    SELECT CAST(x AS FLOAT64) FROM UNNEST(JSON_VALUE_ARRAY(j, '$.centroid')) AS x WITH OFFSET AS o ORDER BY o
  ) AS centroid
FROM UNNEST(JSON_QUERY_ARRAY(@cluster_rows)) AS j
WHERE (SELECT w.n FROM batch_written AS w) = 0 AND (SELECT c.n FROM map_conflicts AS c) = 0;

CREATE TEMP TABLE IF NOT EXISTS member_changes AS
SELECT
  JSON_VALUE(j, '$.cluster_id') AS cluster_id,
  JSON_VALUE(j, '$.post_id') AS post_id,
  CAST(JSON_VALUE(j, '$.probability') AS FLOAT64) AS probability
FROM UNNEST(JSON_QUERY_ARRAY(@member_rows)) AS j
WHERE (SELECT w.n FROM batch_written AS w) = 0 AND (SELECT c.n FROM map_conflicts AS c) = 0;

MERGE `ogilvy-trends-v2.intelligence_42_core.cultural_map` AS t
USING (
  SELECT c.item_id AS close_key, c.*, CAST(NULL AS DATE) AS known_last_seen
  FROM map_changes AS c
  WHERE c.change = 'update'
  UNION ALL
  SELECT CAST(NULL AS STRING) AS close_key, c.*, known.last_seen AS known_last_seen
  FROM map_changes AS c
  LEFT JOIN (
    SELECT x.item_id, MAX(x.last_seen) AS last_seen
    FROM `ogilvy-trends-v2.intelligence_42_core.cultural_map` AS x
    GROUP BY x.item_id
  ) AS known
    ON known.item_id = c.item_id
  WHERE c.change = 'update' OR known.item_id IS NULL
) AS s
ON t.item_id = s.close_key AND t.valid_to IS NULL AND t.valid_from = s.expected_valid_from
WHEN MATCHED THEN
  UPDATE SET valid_to = CURRENT_TIMESTAMP()
WHEN NOT MATCHED AND s.close_key IS NULL THEN
  INSERT (item_id, kind, canonical_key, label, aliases, parent_item_id, centroid, first_seen, first_seen_market,
          first_seen_platform, last_seen, recurrences, lifecycle, status, rejected_until, valid_from, valid_to)
  VALUES (s.item_id, s.kind, s.canonical_key, s.label, s.aliases, s.parent_item_id, s.centroid, s.first_seen,
          s.first_seen_market, s.first_seen_platform, GREATEST(s.last_seen, COALESCE(s.known_last_seen, s.last_seen)),
          s.recurrences, s.lifecycle, s.status,
          s.rejected_until, CURRENT_TIMESTAMP(), NULL);

INSERT INTO `ogilvy-trends-v2.intelligence_42_core.cluster_members` (cluster_id, post_id, probability)
SELECT m.cluster_id, m.post_id, m.probability
FROM member_changes AS m;

INSERT INTO `ogilvy-trends-v2.intelligence_42_core.clusters`
  (cluster_date, cluster_id, market, item_id, match_kind, label, keywords, local_terms, centroid)
SELECT @run_date, c.cluster_id, @market, c.item_id, c.match_kind, c.label, c.keywords, c.local_terms, c.centroid
FROM cluster_changes AS c;

COMMIT TRANSACTION;

SELECT
  (SELECT c.n FROM map_conflicts AS c) AS stale_updates,
  (SELECT COUNT(*) FROM map_changes) AS map_rows,
  (SELECT COUNT(*) FROM cluster_changes) AS cluster_rows,
  (SELECT COUNT(*) FROM member_changes) AS member_rows,
  (SELECT COUNTIF(c.match_kind = 'match') FROM cluster_changes AS c) AS matched,
  (SELECT COUNTIF(c.match_kind = 'recurrence') FROM cluster_changes AS c) AS recurrences,
  (SELECT COUNTIF(c.match_kind = 'variant') FROM cluster_changes AS c) AS variants,
  (SELECT COUNTIF(c.match_kind = 'new') FROM cluster_changes AS c) AS new_items;
