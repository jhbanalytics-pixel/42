-- Read-only inputs to the series test, DATA.md section 3.5 (task 1.19), in BigQuery Standard SQL.
-- core/detect/stats.py runs each statement with @d; {core} is the dataset name. Nothing here writes.

-- test_switch rows in force on @d: the market, platform and lane class pairs the test is switched on for.
SELECT ts.market, ts.platform, ts.lane_class, ts.switched_on, ts.rule_version
FROM {core}.test_switch ts
WHERE ts.switched_on <= @d;

-- Route totals per market, platform, lane class and day over the 8 weeks before @d, for the weekday factor.
-- A day counts only when every route of that market, platform and lane class was valid on it.
WITH h AS (
  SELECT hc.* FROM {core}.v_collection_health_current hc
  WHERE hc.day BETWEEN DATE_SUB(@d, INTERVAL 56 DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
), totals AS (
SELECT h.market, h.platform, h.lane_class, h.day, SUM(h.items) total,
  CAST(NULL AS STRING) series, CAST(NULL AS STRING) protocol
FROM h
GROUP BY h.market, h.platform, h.lane_class, h.day
HAVING LOGICAL_AND(h.valid)
UNION ALL
-- Candidate exposure for platformless panels, separate from the unchanged platform totals above.
SELECT h.market, h.platform, h.lane_class, h.day, SUM(h.items) total, h.series, h.protocol
FROM h
WHERE h.platform IS NULL AND h.lane_class = 'panel'
GROUP BY h.market, h.platform, h.lane_class, h.day, h.series, h.protocol
HAVING LOGICAL_AND(h.valid)
)
SELECT * FROM totals;

-- First-week means for the cold-start prior: panel and counter series of items first seen (cultural_map.first_seen)
-- in the 90 days before @d, with the mean over the series' first 7 observed days from its first value above 0.
-- Only those 90 days are read. Series with fewer than 7 observed days are left out.
WITH sd AS (
  SELECT s.series_id, s.item_id, s.market, s.platform, s.series, s.lane_class, s.day, s.value, cm.kind
  FROM {core}.v_series_daily s
  JOIN {core}.cultural_map cm ON cm.item_id = s.item_id AND cm.valid_to IS NULL
  WHERE s.day BETWEEN DATE_SUB(@d, INTERVAL 90 DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
    AND cm.first_seen BETWEEN DATE_SUB(@d, INTERVAL 90 DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
    AND s.lane_class IN ('panel', 'unbiased_counter')),
first_seen AS (
  SELECT sd.series_id, MIN(sd.day) first_seen FROM sd WHERE sd.value > 0 GROUP BY sd.series_id),
wk AS (
  SELECT sd.series_id, sd.item_id, sd.market, sd.platform, sd.series, sd.lane_class, sd.kind,
    GREATEST(sd.value, 0) value,                            -- a negative counter delta counts as 0
    ROW_NUMBER() OVER (PARTITION BY sd.series_id ORDER BY sd.day) n
  FROM sd
  JOIN first_seen fs ON fs.series_id = sd.series_id
  WHERE sd.day >= fs.first_seen AND sd.value IS NOT NULL)
SELECT wk.series_id, wk.item_id, wk.market, wk.platform, wk.series, wk.lane_class, wk.kind,
  AVG(wk.value) first_week_mean
FROM wk
WHERE wk.n <= 7
GROUP BY wk.series_id, wk.item_id, wk.market, wk.platform, wk.series, wk.lane_class, wk.kind
HAVING COUNT(*) = 7;
