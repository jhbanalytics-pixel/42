-- Inputs of the card and hold outcome measure (core/eval/card_outcome.py, METHOD-GAPS section 7). SELECT only.
-- Three named queries, each introduced by a '-- @query name' line and run on their own by ops/card_outcome_report.py
-- with the parameters @start (first run date), @end (last run date) and @last (@end plus 14 days, the last day a
-- later state can be read). {core} and {agent} are the dataset names, filled in by core/eval/card_outcome_read.py
-- from the caller, so no query names a production dataset. Every table read is filtered on its partition column. Only item ids, ranks, states,
-- hold reasons, rules and lane classes leave the warehouse: no title, post text, creator or URL.

-- @query briefs
-- One row per current brief of each market and run date, with the three lists the module reads.
SELECT b.brief_date, b.market, b.run_id, b.published_at, b.status,
  TO_JSON_STRING(ARRAY(SELECT AS STRUCT JSON_VALUE(c, '$.item_id') item_id,
    SAFE_CAST(JSON_VALUE(c, '$.rank') AS INT64) rank, JSON_VALUE(c, '$.state') state
    FROM UNNEST(IFNULL(JSON_QUERY_ARRAY(b.payload, '$.cards'), []) ) c)) cards_json,
  TO_JSON_STRING(ARRAY(SELECT AS STRUCT JSON_VALUE(c, '$.item_id') item_id,
    SAFE_CAST(JSON_VALUE(c, '$.rank') AS INT64) rank, JSON_VALUE(c, '$.state') state
    FROM UNNEST(IFNULL(JSON_QUERY_ARRAY(b.payload, '$.more'), []) ) c)) more_json,
  TO_JSON_STRING(ARRAY(SELECT AS STRUCT JSON_VALUE(h, '$.item_id') item_id,
    JSON_VALUE(h, '$.reason') reason, JSON_VALUE(h, '$.rule') rule
    FROM UNNEST(IFNULL(JSON_QUERY_ARRAY(b.payload, '$.held_back.items'), []) ) h)) held_json
FROM `{agent}.v_briefs_current` b
WHERE b.brief_date BETWEEN @start AND @end
ORDER BY b.brief_date, b.market;

-- @query states
-- item_state of the briefed items from the first run date to @last, each row with the lane class of its main
-- series read from series_test (a different table from the one being labelled). A series with two lane classes
-- gets NULL, which never measures. signal_lanes is the set of lane classes of every series of the item and day that
-- is significant or jumping (the jump_today predicate of core/detect/sql/state.sql), a missing lane class read as
-- 'unknown'; the module refuses a measured label when any of them is not a measured lane. base_state is the state
-- underneath a Recurring or Seasonal overlay.
WITH items AS (
  SELECT DISTINCT b.market, JSON_VALUE(x, '$.item_id') item_id
  FROM `{agent}.v_briefs_current` b,
    UNNEST(ARRAY_CONCAT(IFNULL(JSON_QUERY_ARRAY(b.payload, '$.cards'), []),
                        IFNULL(JSON_QUERY_ARRAY(b.payload, '$.more'), []),
                        IFNULL(JSON_QUERY_ARRAY(b.payload, '$.held_back.items'), []))) x
  WHERE b.brief_date BETWEEN @start AND @end),
sig AS (
  SELECT st.market, st.item_id, st.metric_date, ARRAY_AGG(DISTINCT IFNULL(st.lane_class, 'unknown')) signal_lanes
  FROM `{core}.v_series_test_current` st
  JOIN items i ON i.market = st.market AND i.item_id = st.item_id
  WHERE st.metric_date BETWEEN @start AND @last
    AND (st.significant OR (st.lane_class != 'unbiased_rank' AND st.obs_prior >= 5 AND st.y >= 8 AND st.y >= 3 * st.med))
  GROUP BY st.market, st.item_id, st.metric_date),
lane AS (
  SELECT st.series_id, st.metric_date, MIN(st.lane_class) lane_class, COUNT(DISTINCT st.lane_class) lanes
  FROM `{core}.v_series_test_current` st
  WHERE st.metric_date BETWEEN @start AND @last
  GROUP BY st.series_id, st.metric_date)
SELECT s.metric_date, s.market, s.item_id, s.state, s.base_state, s.untested,
  IF(l.lanes = 1, l.lane_class, NULL) main_lane_class, IFNULL(sg.signal_lanes, []) signal_lanes
FROM `{core}.v_item_state_current` s
JOIN items i ON i.market = s.market AND i.item_id = s.item_id
LEFT JOIN lane l ON l.series_id = s.main_series_id AND l.metric_date = s.metric_date
LEFT JOIN sig sg ON sg.market = s.market AND sg.item_id = s.item_id AND sg.metric_date = s.metric_date
WHERE s.metric_date BETWEEN @start AND @last
QUALIFY ROW_NUMBER() OVER (PARTITION BY s.metric_date, s.market, s.item_id ORDER BY s.run_id DESC) = 1
ORDER BY s.metric_date, s.market, s.item_id;

-- @query detect_days
-- Dates with a good detect run: only on these does a missing item_state row mean the item is absent.
SELECT DISTINCT g.run_date
FROM `{core}.v_good_runs` g
WHERE g.stage = 'detect' AND g.run_date BETWEEN @start AND @last
ORDER BY g.run_date;
