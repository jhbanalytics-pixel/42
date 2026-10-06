-- Engine forecasts (DATA.md section 6), in BigQuery Standard SQL. core/detect/forecasts.py reads each statement
-- after its "name:" comment. {core} and {agent} are filled in by core/detect/sqlrun.py.
-- intelligence_42_agent.forecasts is append-only. A forecast is one issue row; its resolution is a second row
-- with the same forecast_id and observed_arrival set. Nothing here updates, replaces or removes a row.

-- name: current_view
-- The current row of each forecast: its resolution row once one exists, else its issue row. Engine rows
-- (rule logistic_v1) and the agent's log_forecast rows (rule ask_v1) share the table and the view.
CREATE OR REPLACE VIEW {core}.v_forecasts_current AS
SELECT f.* FROM {agent}.forecasts f
WHERE f.forecast_id IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY f.forecast_id ORDER BY f.observed_arrival IS NULL, f.issue_date) = 1;

-- name: candidates
-- Items with a state on @d in ZA, NG and KE, pinned to the detect run that wrote them (@run_id is not ok yet
-- while detect runs), with the main series' velocity from the current stats run. GLOBAL never gets a forecast.
-- base_state is the state before the Seasonal or Recurring override (DATA.md 3.7), NULL on older rows.
SELECT s.item_id, s.market, s.state, s.base_state, s.markets_hot, s.sig_days3, s.creators3, s.worth_pct,
  t.vel main_vel
FROM {core}.item_state s
LEFT JOIN {core}.v_series_test_current t ON t.series_id = s.main_series_id AND t.metric_date = s.metric_date
WHERE s.metric_date = @d AND s.run_id = @run_id AND s.market IN ('ZA', 'NG', 'KE');

-- name: issued
-- Forecasts already issued on @d, so a rerun issues only what is missing.
SELECT DISTINCT f.forecast_id FROM {agent}.forecasts f WHERE f.issue_date = @d;

-- name: resolve
-- Open forecasts whose window (issue_date + 1 to resolve_date) closed before @d, with observed_arrival read
-- through the latest good runs. Validity is per market, platform, route and day (TRUST.md A4), so a forecast stays
-- open until every window day is good for every market and platform it reads: every route of that platform in the
-- measured lanes (unbiased_rank, unbiased_counter, panel) valid in the day's latest good collect run, and a good
-- detect and stats run for the day. Search, placebo and watchlist routes never hold a forecast. What a forecast
-- reads: in its own market, for every target, the main platform (G1) and every platform the item has a series on
-- there from the issue day to resolve_date, since state.sql derives Rising from all of them; for cross_market also,
-- in each other market, every platform the item has a series on there and the main platform where it has measured
-- health rows in the window. persist_50 also needs a value of its main series on every day from issue_date - 6 to
-- resolve_date, so a NULL or missing day never lowers v7. The main series is the one on the item's latest state row
-- up to the issue day; a forecast without one stays open. Only rows whose outcome can be read are returned.
-- Known gaps: significance in another market or GLOBAL that feeds Rising in state.sql (other, 3 days) is not
-- checked for validity, and aggregate and coaction runs are not required.
WITH cur AS (
  SELECT f.* FROM {agent}.forecasts f
  WHERE f.issue_date BETWEEN @since AND @d
  QUALIFY ROW_NUMBER() OVER (PARTITION BY f.forecast_id ORDER BY f.observed_arrival IS NULL, f.issue_date) = 1),
o AS (
  SELECT cur.* FROM cur
  WHERE cur.observed_arrival IS NULL AND cur.resolve_date < @d AND cur.market IN ('ZA', 'NG', 'KE')),
st AS (
  SELECT s.item_id, s.market, s.metric_date, s.state, s.authenticity, s.main_series_id
  FROM {core}.v_item_state_current s
  WHERE s.metric_date BETWEEN @since AND @d),
ml AS (                     -- the item's latest state row with a main series, up to the issue day
  SELECT o.forecast_id, o.item_id, o.market, MAX(st.metric_date) at_date
  FROM o JOIN st ON st.item_id = o.item_id AND st.market = o.market AND st.metric_date <= o.issue_date
  WHERE st.main_series_id IS NOT NULL
  GROUP BY o.forecast_id, o.item_id, o.market),
mp AS (                     -- that row's main series and the series' platform
  SELECT ml.forecast_id, st.main_series_id, t.platform
  FROM ml
  JOIN st ON st.item_id = ml.item_id AND st.market = ml.market AND st.metric_date = ml.at_date
  JOIN {core}.v_series_test_current t ON t.series_id = st.main_series_id AND t.metric_date = st.metric_date
  WHERE t.platform IS NOT NULL AND t.metric_date BETWEEN @since AND @d),
measured AS (               -- health rows of the measured lanes the series read (not search, placebo or watchlist)
  SELECT h.market, h.platform, h.day, h.valid
  FROM {core}.v_collection_health_current h
  WHERE h.day BETWEEN @since AND @d AND h.lane_class IN ('unbiased_rank', 'unbiased_counter', 'panel')),
mplat AS (                  -- a measured route with no platform (the culture desk panel, prism/profiles) feeds series on
  SELECT m.market, m.platform, m.day, m.valid       -- several platforms, so it counts for every platform with a series
  FROM measured m WHERE m.platform IS NOT NULL      -- in that market
  UNION ALL
  SELECT m.market, p.platform, m.day, m.valid
  FROM measured m
  JOIN (SELECT DISTINCT t.market, t.platform FROM {core}.v_series_test_current t
        WHERE t.platform IS NOT NULL AND t.metric_date BETWEEN @since AND @d) p ON p.market = m.market
  WHERE m.platform IS NULL),
good AS (                   -- market, platform and day with every measured route valid, and good detect and stats runs
  SELECT m.market, m.platform, m.day
  FROM mplat m
  WHERE m.day IN (SELECT g.run_date FROM {core}.v_good_runs g WHERE g.stage = 'detect')
    AND m.day IN (SELECT g.run_date FROM {core}.v_good_runs g WHERE g.stage = 'stats')
  GROUP BY m.market, m.platform, m.day
  HAVING LOGICAL_AND(IFNULL(m.valid, FALSE))),
reads AS (                  -- every market and platform a forecast reads:
  SELECT o.forecast_id, o.market, mp.platform                       -- the main platform in its own market;
  FROM o JOIN mp ON mp.forecast_id = o.forecast_id
  UNION DISTINCT
  SELECT o.forecast_id, t.market, t.platform                        -- every platform the item has a series on
  FROM o                                                            -- from the issue day to resolve_date, in its
  JOIN {core}.v_series_test_current t ON t.item_id = o.item_id      -- own market and, for cross_market, in ZA,
    AND t.metric_date >= o.issue_date AND t.metric_date <= o.resolve_date   -- NG and KE;
  WHERE t.platform IS NOT NULL AND t.metric_date BETWEEN @since AND @d
    AND (t.market = o.market OR (o.target = 'cross_market' AND t.market IN ('ZA', 'NG', 'KE')))
  UNION DISTINCT
  SELECT DISTINCT o.forecast_id, m.market, mp.platform              -- and, for cross_market, the main platform in
  FROM o                                                            -- each other market where it has measured
  JOIN mp ON mp.forecast_id = o.forecast_id                         -- health rows in the window
  JOIN mplat m ON m.platform = mp.platform AND m.market IN ('ZA', 'NG', 'KE') AND m.market != o.market
    AND m.day > o.issue_date AND m.day <= o.resolve_date
  WHERE o.target = 'cross_market'),
pairs AS (
  SELECT r.forecast_id, COUNT(g.day) = DATE_DIFF(o.resolve_date, o.issue_date, DAY) complete
  FROM reads r
  JOIN o ON o.forecast_id = r.forecast_id
  LEFT JOIN good g ON g.market = r.market AND g.platform = r.platform
    AND g.day > o.issue_date AND g.day <= o.resolve_date
  GROUP BY r.forecast_id, r.market, r.platform, o.issue_date, o.resolve_date),
whole AS (
  SELECT pairs.forecast_id FROM pairs GROUP BY pairs.forecast_id HAVING LOGICAL_AND(pairs.complete)),
reach AS (                  -- reach_rising: Rising or Peaking on a window day
  SELECT o.forecast_id, LOGICAL_OR(st.state IN ('rising', 'peaking')) hit
  FROM o JOIN st ON st.item_id = o.item_id AND st.market = o.market
    AND st.metric_date > o.issue_date AND st.metric_date <= o.resolve_date
  GROUP BY o.forecast_id),
cross_mk AS (               -- cross_market: another market, not GLOBAL, significant on a window day and not held
  SELECT DISTINCT o.forecast_id                                     -- as Likely coordinated there that day
  FROM o
  JOIN {core}.v_series_test_current t ON t.item_id = o.item_id AND t.market NOT IN (o.market, 'GLOBAL')
    AND t.metric_date > o.issue_date AND t.metric_date <= o.resolve_date AND t.significant
  LEFT JOIN st ON st.item_id = t.item_id AND st.market = t.market AND st.metric_date = t.metric_date
  WHERE t.metric_date BETWEEN @since AND @d AND IFNULL(st.authenticity, '') != 'likely_coordinated'),
pm AS (                     -- persist_50: the main series
  SELECT o.forecast_id, o.issue_date, o.resolve_date, mp.main_series_id
  FROM o JOIN mp ON mp.forecast_id = o.forecast_id
  WHERE o.target = 'persist_50'),
sd AS (
  SELECT d.series_id, d.day, d.value
  FROM {core}.v_series_daily d
  WHERE d.series_id IN (SELECT pm.main_series_id FROM pm)
    AND d.day BETWEEN DATE_SUB(@since, INTERVAL 6 DAY) AND @d),
filled AS (                 -- a value on every day from issue_date - 6 to resolve_date
  SELECT pm.forecast_id
  FROM pm JOIN sd ON sd.series_id = pm.main_series_id
    AND sd.day BETWEEN DATE_SUB(pm.issue_date, INTERVAL 6 DAY) AND pm.resolve_date
  WHERE sd.value IS NOT NULL
  GROUP BY pm.forecast_id, pm.issue_date, pm.resolve_date
  HAVING COUNT(DISTINCT sd.day) = DATE_DIFF(pm.resolve_date, pm.issue_date, DAY) + 7),
v7 AS (
  SELECT sd.series_id, sd.day,
    SUM(sd.value) OVER (PARTITION BY sd.series_id ORDER BY UNIX_DATE(sd.day)
                        RANGE BETWEEN 6 PRECEDING AND CURRENT ROW) v7
  FROM sd),
persist AS (                -- v7 on every window day at half the issue day's v7 or more
  SELECT pm.forecast_id, LOGICAL_AND(w.v7 >= .5 * b.v7) held
  FROM pm
  JOIN filled ON filled.forecast_id = pm.forecast_id
  JOIN v7 b ON b.series_id = pm.main_series_id AND b.day = pm.issue_date
  JOIN v7 w ON w.series_id = pm.main_series_id AND w.day > pm.issue_date AND w.day <= pm.resolve_date
  GROUP BY pm.forecast_id),
res AS (
  SELECT o.forecast_id, o.item_id, o.market, o.target, o.issue_date, o.horizon, o.rule, o.prob,
    o.predicted_arrival, o.persistence_arrival, o.resolve_date,
    CASE o.target
      WHEN 'reach_rising' THEN IFNULL(r.hit, FALSE)
      WHEN 'cross_market' THEN c.forecast_id IS NOT NULL
      WHEN 'persist_50' THEN p.held END observed_arrival
  FROM o
  JOIN whole ON whole.forecast_id = o.forecast_id
  LEFT JOIN reach r ON r.forecast_id = o.forecast_id
  LEFT JOIN cross_mk c ON c.forecast_id = o.forecast_id
  LEFT JOIN persist p ON p.forecast_id = o.forecast_id)
SELECT res.* FROM res WHERE res.observed_arrival IS NOT NULL;

-- name: append
-- Append issue and resolution rows. An issue row is left out when its forecast_id is already in the table; a
-- resolution row when the forecast already has one. A rerun adds nothing.
INSERT INTO {agent}.forecasts (forecast_id, item_id, market, target, issue_date, horizon, rule, prob,
  predicted_arrival, persistence_arrival, resolve_date, observed_arrival)
SELECT n.forecast_id, n.item_id, n.market, n.target, n.issue_date, n.horizon, n.rule, n.prob,
  n.predicted_arrival, n.persistence_arrival, n.resolve_date, n.observed_arrival
FROM UNNEST(@rows) n
WHERE NOT EXISTS (
  SELECT 1 FROM {agent}.forecasts f
  WHERE f.forecast_id = n.forecast_id AND f.issue_date = n.issue_date
    AND (n.observed_arrival IS NULL OR f.observed_arrival IS NOT NULL));

-- name: scoring
-- The current row of every engine forecast whose window has closed, resolved or not, for the weekly score.
SELECT c.forecast_id, c.item_id, c.market, c.target, c.issue_date, c.horizon, c.rule, c.prob,
  c.predicted_arrival, c.persistence_arrival, c.resolve_date, c.observed_arrival
FROM {core}.v_forecasts_current c
WHERE c.rule = @rule AND c.resolve_date < @d;
