WITH dates AS ({link_dates}),
raw AS (
  SELECT ds.metric_date, pi.post_id, pi.item_id, pi.via, pi.link_market, pi.linked_on
  FROM dates ds
  JOIN {core}.post_items pi ON pi.linked_on IS NULL OR pi.linked_on <= ds.metric_date
  UNION ALL
  SELECT ds.metric_date, l.post_id, l.item_id, 'lineage', l.link_market, l.linked_on
  FROM dates ds
  JOIN {core}.post_item_lineage l ON l.linked_on <= ds.metric_date),
live AS (
  SELECT r.* FROM raw r
  WHERE NOT EXISTS (SELECT 1 FROM {core}.post_item_end e
                    WHERE e.post_id = r.post_id AND e.item_id = r.item_id AND e.ended_on <= r.metric_date)),
seen AS (
  SELECT DISTINCT ds.metric_date, o.post_id, o.market
  FROM dates ds
  JOIN {core}.post_observations o
    ON o.observed_date BETWEEN DATE_SUB(ds.metric_date, INTERVAL 27 DAY) AND ds.metric_date),
dated AS (
  SELECT l.metric_date, l.post_id, l.item_id, s.market,
    CASE l.via WHEN 'lineage' THEN 0 WHEN 'cluster' THEN 1 ELSE 2 END prec
  FROM live l
  JOIN seen s ON s.post_id = l.post_id AND s.metric_date = l.metric_date
  WHERE l.linked_on IS NOT NULL AND l.via IN ('cluster', 'cluster_pan', 'lineage')
    AND (l.link_market IS NULL OR l.link_market = s.market)),
winner AS (
  SELECT t.metric_date, t.post_id, t.item_id, t.market FROM dated t
  QUALIFY ROW_NUMBER() OVER (PARTITION BY t.metric_date, t.post_id, t.market ORDER BY t.prec, t.item_id) = 1)
SELECT l.metric_date, l.post_id, l.item_id, l.via, CAST(NULL AS STRING) market
FROM live l WHERE l.linked_on IS NULL OR l.via NOT IN ('cluster', 'cluster_pan', 'lineage')
UNION ALL
SELECT w.metric_date, w.post_id, w.item_id, 'topic', w.market FROM winner w
