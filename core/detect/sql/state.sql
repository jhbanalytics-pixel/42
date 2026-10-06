-- The item_state INSERT of DATA.md section 3.7, verbatim, in BigQuery Standard SQL: a script of a temp
-- function and one INSERT. {core} is the dataset name, filled in by core/detect/sqlrun.py; params @d,
-- @run_id and @rule_version. Reads v_item_waves (sql/waves.sql) and the views in sql/views.sql.

CREATE TEMP FUNCTION state_level(s STRING) AS (
  CASE s WHEN 'rising' THEN 6 WHEN 'emerging' THEN 5 WHEN 'recurring' THEN 5 WHEN 'seasonal' THEN 5
    WHEN 'spike' THEN 4 WHEN 'peaking' THEN 4 WHEN 'mainstream' THEN 4 WHEN 'new_to_42' THEN 3
    WHEN 'on_the_boards' THEN 2 WHEN 'fading' THEN 1 ELSE 0 END);

INSERT INTO {core}.item_state
WITH t AS (SELECT st.* FROM {core}.v_series_test_current st WHERE st.metric_date = @d),
agg AS (                    -- the item's series in this market today
  SELECT t.item_id, t.market,
    COUNTIF(t.test != 'none') = 0 untested,
    MAX(t.obs_prior + IF(t.y IS NULL, 0, 1)) obs_days,
    MIN(t.first_measured) first_measured,
    MIN(t.p_mid) p_min, MIN(t.q) q_min,
    LOGICAL_OR(t.significant) sig_today,
    LOGICAL_OR(t.significant AND t.ratio >= 2) sig_ratio_today,
    COUNT(DISTINCT IF(t.significant, t.platform, NULL)) sig_platforms_today,
    ARRAY_AGG(DISTINCT IF(t.significant, t.platform, NULL) IGNORE NULLS) sig_platform_list,
    LOGICAL_OR(t.lane_class != 'unbiased_rank' AND t.obs_prior >= 5 AND t.y >= 8 AND t.y >= 3 * t.med) jump_today,
    ARRAY_AGG(t ORDER BY t.test != 'none' DESC, t.lane_class = 'unbiased_rank', IFNULL(t.mu, t.v3) DESC, t.series_id LIMIT 1)[OFFSET(0)] main
  FROM t WHERE t.market != 'GLOBAL'
  GROUP BY t.item_id, t.market
  HAVING MAX(IFNULL(t.peak28, 0)) > 0),
t3 AS (
  SELECT st.item_id, st.market, COUNT(DISTINCT st.metric_date) sig_days3
  FROM {core}.v_series_test_current st
  WHERE st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 2 DAY) AND @d AND st.significant AND st.ratio >= 2
  GROUP BY st.item_id, st.market),
other AS (                  -- independent evidence: other markets (3 days), GLOBAL counters (today)
  SELECT st.item_id, ARRAY_AGG(DISTINCT st.market) sig_markets3,
    ARRAY_AGG(DISTINCT IF(st.market = 'GLOBAL' AND st.metric_date = @d, st.platform, NULL) IGNORE NULLS) global_platforms
  FROM {core}.v_series_test_current st
  WHERE st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 2 DAY) AND @d AND st.significant
  GROUP BY st.item_id),
xm AS (                     -- markets significant in 14 days, not held as coordinated
  SELECT st.item_id, COUNT(DISTINCT st.market) markets_hot,
    ARRAY_AGG(st.market ORDER BY st.metric_date, st.market LIMIT 1)[OFFSET(0)] lead_market
  FROM {core}.v_series_test_current st
  LEFT JOIN {core}.v_item_state_current ps
    ON ps.item_id = st.item_id AND ps.market = st.market AND ps.metric_date = st.metric_date
  WHERE st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d AND st.market != 'GLOBAL'
    AND st.significant AND IFNULL(ps.authenticity, '') != 'likely_coordinated'
  GROUP BY st.item_id),
sp AS (
  SELECT st.item_id, st.market, COUNT(DISTINCT st.platform) rising_platforms
  FROM {core}.v_series_test_current st
  WHERE st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d AND st.significant
  GROUP BY st.item_id, st.market),
hs AS (                     -- recent shown states
  SELECT s.item_id, s.market,
    LOGICAL_OR(s.state IN ('emerging', 'rising') AND s.metric_date >= DATE_SUB(@d, INTERVAL 14 DAY)) grew14,
    LOGICAL_OR(s.state IN ('rising', 'peaking', 'mainstream')) big28,
    LOGICAL_OR(s.state IN ('spike', 'emerging', 'rising', 'peaking', 'mainstream', 'recurring', 'seasonal')) active28,
    MAX(IF(s.metric_date = DATE_SUB(@d, INTERVAL 1 DAY), s.state, NULL)) state_yesterday,
    MAX(IF(s.metric_date = DATE_SUB(@d, INTERVAL 1 DAY), s.state_raw, NULL)) raw_yesterday
  FROM {core}.v_item_state_current s
  WHERE s.metric_date BETWEEN DATE_SUB(@d, INTERVAL 28 DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
  GROUP BY s.item_id, s.market),
fade AS (
  SELECT a.item_id, a.market, COUNTIF(st.y <= .6 * st.peak28) low_days
  FROM agg a JOIN {core}.v_series_test_current st
    ON st.series_id = a.main.series_id AND st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 2 DAY) AND @d
  GROUP BY a.item_id, a.market),
wave AS (SELECT wv0.* FROM {core}.v_item_waves wv0 WHERE wv0.wave_start <= @d),
cur AS (
  SELECT wave.item_id, wave.market, MAX(wave.wave_start) cur_start
  FROM wave WHERE wave.wave_end >= DATE_SUB(@d, INTERVAL 2 DAY) GROUP BY wave.item_id, wave.market),
wv AS (                     -- earlier waves (before the current one) with a peak of 8 or more
  SELECT e.item_id, e.market,
    LOGICAL_OR(e.peak_date >= DATE_SUB(@d, INTERVAL 365 DAY)) peak_365,
    LOGICAL_OR(ABS(DATE_DIFF(e.peak_date, DATE_SUB(@d, INTERVAL 1 YEAR), DAY)) <= 7) last_year,
    ARRAY_AGG(STRUCT(e.peak_date AS peak_date, e.peak_posts AS peak_posts) ORDER BY e.peak_date DESC LIMIT 1)[OFFSET(0)] last_wave
  FROM wave e LEFT JOIN cur ON cur.item_id = e.item_id AND cur.market = e.market
  WHERE e.peak_posts >= 8 AND e.wave_start < IFNULL(cur.cur_start, DATE_ADD(@d, INTERVAL 1 DAY))
  GROUP BY e.item_id, e.market),
cal AS (
  SELECT c.market, it AS item_id, MIN(c.name) moment
  FROM {core}.calendar c, UNNEST(c.item_ids) it
  WHERE c.moment_date BETWEEN DATE_SUB(@d, INTERVAL 3 DAY) AND DATE_ADD(@d, INTERVAL 14 DAY)
  GROUP BY c.market, it),
co AS (
  SELECT cs.item_id, cs.market,
    MAX(IF(cs.signal = 'coaction', cs.item_posts_share, 0)) network_share,
    COUNT(DISTINCT CONCAT(cs.signal, ':', IFNULL(cs.component_id, ''))) network_signals
  FROM {core}.v_coord_signals_current cs WHERE cs.metric_date = @d
  GROUP BY cs.item_id, cs.market),
cl AS (
  SELECT k.item_id, UPPER(k.market) market, ANY_VALUE(k.match_kind) match_kind
  FROM {core}.clusters k WHERE k.cluster_date = @d GROUP BY k.item_id, UPPER(k.market)),
f AS (
  SELECT a.*, cm.kind, cm.status map_status, w.* EXCEPT (item_id, market),
    IFNULL(t3.sig_days3, 0) sig_days3,
    EXISTS (SELECT 1 FROM UNNEST(IFNULL(o.sig_markets3, [])) mk WHERE mk NOT IN (a.market, 'GLOBAL')) other_market,
    EXISTS (SELECT 1 FROM UNNEST(IFNULL(o.global_platforms, [])) gp
            WHERE gp NOT IN UNNEST(IFNULL(a.sig_platform_list, []))) other_platform_global,
    IFNULL(xm.markets_hot, 0) markets_hot, xm.lead_market,
    IF(pb.placebo_items >= 20 AND sp.rising_platforms > pb.rising_p95, sp.rising_platforms, NULL) spread_platforms,
    IF(pb.placebo_items >= 20 AND w.found_platforms14 > pb.found_p95, w.found_platforms14, NULL) found_platforms,
    IFNULL(hs.grew14, FALSE) grew14, IFNULL(hs.big28, FALSE) big28, IFNULL(hs.active28, FALSE) active28,
    hs.state_yesterday, hs.raw_yesterday, IFNULL(fd.low_days, 0) = 3 low3,
    wv.item_id IS NOT NULL had_earlier_wave, IFNULL(cur.cur_start > DATE_SUB(@d, INTERVAL 28 DAY), FALSE) new_wave,
    IFNULL(wv.peak_365, FALSE) peak_365, IFNULL(wv.last_year, FALSE) last_year, wv.last_wave,
    cal.moment, cl.match_kind, co.network_share, co.network_signals,
    (SELECT COUNT(*) > 0 FROM {core}.v_good_runs g WHERE g.stage = 'coaction' AND g.run_date = @d) coaction_ran
  FROM agg a
  JOIN {core}.cultural_map cm ON cm.item_id = a.item_id AND cm.valid_to IS NULL
  LEFT JOIN {core}.tvf_item_window(@d) w ON w.item_id = a.item_id AND w.market = a.market
  LEFT JOIN t3 ON t3.item_id = a.item_id AND t3.market = a.market
  LEFT JOIN other o ON o.item_id = a.item_id
  LEFT JOIN xm ON xm.item_id = a.item_id
  LEFT JOIN sp ON sp.item_id = a.item_id AND sp.market = a.market
  LEFT JOIN {core}.tvf_placebo_base(@d) pb ON pb.market = a.market
  LEFT JOIN hs ON hs.item_id = a.item_id AND hs.market = a.market
  LEFT JOIN fade fd ON fd.item_id = a.item_id AND fd.market = a.market
  LEFT JOIN cur ON cur.item_id = a.item_id AND cur.market = a.market
  LEFT JOIN wv ON wv.item_id = a.item_id AND wv.market = a.market
  LEFT JOIN cal ON cal.item_id = a.item_id AND cal.market = a.market
  LEFT JOIN cl ON cl.item_id = a.item_id AND cl.market = a.market
  LEFT JOIN co ON co.item_id = a.item_id AND co.market = a.market),
b AS (
  SELECT f.*,
    IFNULL(f.creators3, 0) >= 5 AND IFNULL(f.posts3, 0) >= 8 AND IFNULL(f.top_creator_share3, 1) <= .4 floors,
    IFNULL(f.creators3, 0) >= 3 OR IFNULL(f.board_entry, FALSE) new_floor,
    f.first_measured > DATE_SUB(@d, INTERVAL 28 DAY) AND NOT f.had_earlier_wave is_new,
    ARRAY(SELECT x FROM UNNEST([
      IF(f.near_dup_share >= .30, 'near_duplicates', NULL), IF(f.young_share >= .40, 'young_accounts', NULL),
      IF(f.posts7 >= 10 AND f.burst_share >= .35, 'burst', NULL),
      IF(f.posts7 >= 10 AND f.top3_share >= .60, 'concentrated', NULL)]) x WHERE x IS NOT NULL) share_flags
  FROM f),
c AS (
  SELECT b.*,
    NOT b.untested AND b.floors AND b.sig_ratio_today
      AND (b.sig_days3 >= 2 OR b.sig_platforms_today >= 2 OR b.other_platform_global OR b.other_market) rising,
    b.floors AND (b.is_new OR (b.new_wave AND b.had_earlier_wave))
      AND IF(b.untested, b.obs_days >= 5 AND IFNULL(b.posts3, 0) >= IFNULL(b.posts3_prev, 0), b.sig_today) emerging,
    IF(b.untested, (b.jump_today AND IFNULL(b.creators3, 0) >= 3) OR IFNULL(b.top10_twice, FALSE),
       b.floors AND b.sig_today) spike,
    b.untested AND b.first_measured > DATE_SUB(@d, INTERVAL 14 DAY) AND b.new_floor fresh,
    NOT b.untested AND b.active28 AND b.low3 fading,
    NOT b.untested AND b.big28 AND ((IFNULL(b.large_posts7, 0) > 0 AND IFNULL(b.news_posts7, 0) > 0)
      OR IFNULL(b.spread_platforms, 0) >= 3) mainstream,
    NOT b.untested AND b.grew14 AND b.main.accel < 0 AND b.main.v3 >= .7 * b.main.peak28 peaking
  FROM b),
s AS (
  SELECT c.*,
    CASE
      WHEN (c.rising OR c.emerging OR c.spike OR c.fresh) AND (c.moment IS NOT NULL OR c.last_year) THEN 'seasonal'
      WHEN (c.rising OR c.emerging OR c.spike OR c.fresh) AND c.new_wave AND c.peak_365 THEN 'recurring'
      WHEN c.rising THEN 'rising' WHEN c.emerging THEN 'emerging' WHEN c.spike THEN 'spike'
      WHEN c.fading THEN 'fading' WHEN c.mainstream THEN 'mainstream' WHEN c.peaking THEN 'peaking'
      WHEN IFNULL(c.board_entry, FALSE) THEN 'on_the_boards'
      WHEN c.fresh AND NOT c.had_earlier_wave THEN 'new_to_42' END state_raw,
    CASE                    -- the state without the Seasonal and Recurring override, kept when it is one of four
      WHEN c.rising THEN 'rising' WHEN c.emerging THEN 'emerging' WHEN c.spike THEN 'spike'
      WHEN c.fading OR c.mainstream OR c.peaking OR IFNULL(c.board_entry, FALSE) THEN NULL
      WHEN c.fresh AND NOT c.had_earlier_wave THEN 'new_to_42' END base_state,
    CASE
      WHEN IFNULL(c.network_share, 0) >= .2 OR IFNULL(c.network_signals, 0) >= 2 THEN 'likely_coordinated'
      WHEN IFNULL(c.posts7, 0) < 30 OR IFNULL(SAFE_DIVIDE(c.seen7_all, c.delta7), 1) < .1 THEN 'not_assessed'
      WHEN ARRAY_LENGTH(c.share_flags) > 0 OR IFNULL(c.network_signals, 0) = 1 THEN 'check_pattern'
      WHEN NOT c.coaction_ran THEN 'not_assessed'
      ELSE 'clear' END authenticity,
    CASE WHEN IFNULL(c.geo_known_posts7, 0) < 8 THEN 'market_unconfirmed'
         WHEN c.local_posts7 / c.geo_known_posts7 < .6 THEN 'not_local' ELSE 'local' END geo_status,
    SAFE_DIVIDE(c.local_posts7, c.geo_known_posts7) local_share,
    CASE WHEN c.large_at IS NULL THEN 'small_only' WHEN c.small_at < c.large_at THEN 'bottom_up'
         ELSE 'top_down' END diffusion,
    CASE WHEN c.is_new THEN 'new' WHEN c.new_wave AND c.had_earlier_wave THEN 'recurrence'
         WHEN c.match_kind = 'variant' THEN 'variant' ELSE 'ongoing' END novelty
  FROM c),
sc AS (
  SELECT s.*,
    IF(state_level(s.state_raw) < state_level(s.state_yesterday)
       AND state_level(s.raw_yesterday) >= state_level(s.state_yesterday), s.state_yesterday, s.state_raw) state,
    s.state_raw IS NOT NULL AND s.authenticity != 'likely_coordinated' AND s.geo_status != 'not_local'
      AND s.map_status = 'active' eligible,
    PERCENT_RANK() OVER (PARTITION BY s.market, s.kind
      ORDER BY IFNULL(-LOG10(GREATEST(s.p_min, 1e-12)), 0), (s.main.y + 1) / (IFNULL(s.main.mu, s.main.med) + 1)) surge,
    PERCENT_RANK() OVER (PARTITION BY s.market, s.kind ORDER BY IFNULL(s.main.vel, 0) + .5 * IFNULL(s.main.accel, 0)) momentum,
    PERCENT_RANK() OVER (PARTITION BY s.market, s.kind ORDER BY IFNULL(s.creators3, 0)) breadth
  FROM s),
wr AS (
  SELECT sc.*,
    CASE sc.authenticity WHEN 'clear' THEN 1 ELSE .8 END
    * EXP((LN(.05 + sc.surge) + LN(.05 + sc.momentum) + LN(.05 + sc.breadth)
      + LN(.05 + LEAST(1, .25 * IFNULL(sc.spread_platforms, 1) + .35 * GREATEST(sc.markets_hot - 1, 0)))
      + LN(CASE sc.novelty WHEN 'new' THEN 1 WHEN 'recurrence' THEN .8 WHEN 'variant' THEN .6 ELSE .35 END)
      + LN(CASE sc.diffusion WHEN 'bottom_up' THEN 1 WHEN 'small_only' THEN .7 ELSE .5 END)) / 6) worth_raw
  FROM sc)
SELECT @d metric_date, wr.market, wr.item_id, wr.kind, wr.state_raw, wr.state, wr.untested,
  wr.main.series_id main_series_id, wr.main.y main_y, wr.main.mu main_mu, wr.main.ratio main_ratio,
  wr.q_min, wr.sig_days3, wr.creators3, wr.posts3, wr.top_creator_share3,
  wr.authenticity, wr.share_flags, wr.sponsored_share, wr.geo_status, wr.local_share, wr.geo_known_posts7,
  wr.spread_platforms, wr.found_platforms, wr.markets_hot, wr.lead_market, wr.diffusion, wr.novelty,
  wr.last_wave, wr.moment, wr.eligible, wr.worth_raw,
  IF(wr.eligible AND COUNT(*) OVER co_n >= 20,
     (PERCENT_RANK() OVER co * (COUNT(*) OVER co_n - 1) + CUME_DIST() OVER co * COUNT(*) OVER co_n - 1)
       / 2 / (COUNT(*) OVER co_n - 1), NULL) worth_pct,              -- midrank percentile in the cohort
  @run_id run_id, @rule_version rule_version, wr.base_state
FROM wr
WHERE wr.state IS NOT NULL
WINDOW co AS (PARTITION BY wr.market, wr.eligible ORDER BY wr.worth_raw),
       co_n AS (PARTITION BY wr.market, wr.eligible);
