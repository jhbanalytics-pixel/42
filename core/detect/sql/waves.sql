-- v_item_waves, DATA.md end of section 3.6, in BigQuery Standard SQL: one row per item, market and wave.
-- A wave is an island of days with sightings in v_item_daily_current (every lane class except '_any',
-- legacy included), split by 28 or more quiet days; its peak is the busiest day, the earlier on a tie.
-- A day's posts is the largest lane class total that day, so a post seen in two lane classes counts once.
-- above_half_days counts the wave's days with posts at or above half its peak_posts.
-- item_daily has no lane column, so placebo sightings are dropped as the search_presence rows of an item
-- on a day it was seeded in the placebo lane in that market.
-- {core} and {agent} are the dataset names, filled in by core/detect/sqlrun.py.

CREATE OR REPLACE VIEW {core}.v_item_waves AS
WITH pl AS (
  SELECT DISTINCT q.item_id, q.market, q.seed_date FROM {core}.seed_queue q WHERE q.lane = 'placebo'),
lc AS (                     -- posts per item, market, day and lane class
  SELECT i.item_id, i.market, i.metric_date, i.lane_class, SUM(i.posts) posts
  FROM {core}.v_item_daily_current i
  LEFT JOIN pl ON pl.item_id = i.item_id AND pl.market = i.market AND pl.seed_date = i.metric_date
  WHERE i.lane_class != '_any' AND NOT (i.lane_class = 'search_presence' AND pl.item_id IS NOT NULL)
  GROUP BY i.item_id, i.market, i.metric_date, i.lane_class),
dd AS (
  SELECT lc.item_id, lc.market, lc.metric_date, MAX(lc.posts) posts
  FROM lc GROUP BY lc.item_id, lc.market, lc.metric_date
  HAVING MAX(lc.posts) > 0),
gap AS (
  SELECT dd.*,
    IF(IFNULL(DATE_DIFF(dd.metric_date, LAG(dd.metric_date) OVER (PARTITION BY dd.item_id, dd.market
                                                                  ORDER BY dd.metric_date), DAY), 99) > 28,
       1, 0) starts
  FROM dd),
num AS (
  SELECT gap.*, SUM(gap.starts) OVER (PARTITION BY gap.item_id, gap.market ORDER BY gap.metric_date) wave_no
  FROM gap),
pk AS (
  SELECT num.*, MAX(num.posts) OVER (PARTITION BY num.item_id, num.market, num.wave_no) wave_peak
  FROM num)
SELECT pk.item_id, pk.market, MIN(pk.metric_date) wave_start, MAX(pk.metric_date) wave_end,
  ARRAY_AGG(pk.metric_date ORDER BY pk.posts DESC, pk.metric_date LIMIT 1)[OFFSET(0)] peak_date,
  MAX(pk.posts) peak_posts, COUNTIF(2 * pk.posts >= pk.wave_peak) above_half_days
FROM pk
GROUP BY pk.item_id, pk.market, pk.wave_no;
