-- Inputs of the card and hold outcome measure (core/eval/card_outcome.py, METHOD-GAPS section 7). SELECT only.
-- Three named queries, each introduced by a '-- @query name' line and run on their own by ops/card_outcome_report.py
-- with the parameters @start (first run date), @end (last run date) and @last (@end plus 14 days, the last day a
-- later state can be read). Every table read is filtered on its partition column. Only item ids, ranks, states,
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
FROM `ogilvy-trends-v2.intelligence_42_agent.v_briefs_current` b
WHERE b.brief_date BETWEEN @start AND @end
ORDER BY b.brief_date, b.market;

-- @query states
-- item_state of the briefed items from the first run date to @last, each row with the lane class of its main
-- series read from series_test (a different table from the one being labelled). A series with two lane classes
-- gets NULL, which never measures.
WITH items AS (
  SELECT DISTINCT b.market, JSON_VALUE(x, '$.item_id') item_id
  FROM `ogilvy-trends-v2.intelligence_42_agent.v_briefs_current` b,
    UNNEST(ARRAY_CONCAT(IFNULL(JSON_QUERY_ARRAY(b.payload, '$.cards'), []),
                        IFNULL(JSON_QUERY_ARRAY(b.payload, '$.more'), []),
                        IFNULL(JSON_QUERY_ARRAY(b.payload, '$.held_back.items'), []))) x
  WHERE b.brief_date BETWEEN @start AND @end),
lane AS (
  SELECT st.series_id, st.metric_date, MIN(st.lane_class) lane_class, COUNT(DISTINCT st.lane_class) lanes
  FROM `ogilvy-trends-v2.intelligence_42_core.v_series_test_current` st
  WHERE st.metric_date BETWEEN @start AND @last
  GROUP BY st.series_id, st.metric_date)
SELECT s.metric_date, s.market, s.item_id, s.state, s.untested,
  IF(l.lanes = 1, l.lane_class, NULL) main_lane_class
FROM `ogilvy-trends-v2.intelligence_42_core.v_item_state_current` s
JOIN items i ON i.market = s.market AND i.item_id = s.item_id
LEFT JOIN lane l ON l.series_id = s.main_series_id AND l.metric_date = s.metric_date
WHERE s.metric_date BETWEEN @start AND @last
QUALIFY ROW_NUMBER() OVER (PARTITION BY s.metric_date, s.market, s.item_id ORDER BY s.run_id DESC) = 1
ORDER BY s.metric_date, s.market, s.item_id;

-- @query detect_days
-- Dates with a good detect run: only on these does a missing item_state row mean the item is absent.
SELECT DISTINCT g.run_date
FROM `ogilvy-trends-v2.intelligence_42_core.v_good_runs` g
WHERE g.stage = 'detect' AND g.run_date BETWEEN @start AND @last
ORDER BY g.run_date;
