-- Detection views and table functions, DATA.md sections 3.1, 3.4 and 3.6, in BigQuery Standard SQL.
-- {core} and {agent} are the dataset names; core/detect/sqlrun.py renders them and applies each statement in order.

-- 3.1 Runs and current views

CREATE OR REPLACE VIEW {core}.v_good_runs AS
SELECT r.stage, r.run_date, ARRAY_AGG(r.run_id ORDER BY r.finished_at DESC LIMIT 1)[OFFSET(0)] run_id
FROM {agent}.runs r WHERE r.status = 'ok' GROUP BY r.stage, r.run_date;

CREATE OR REPLACE VIEW {core}.v_collection_health_current AS
SELECT s.* FROM {core}.collection_health s
JOIN {core}.v_good_runs g
  ON g.stage = 'collect' AND g.run_date = s.day AND g.run_id = s.run_id;

CREATE OR REPLACE VIEW {core}.v_item_daily_current AS
SELECT s.* FROM {core}.item_daily s
JOIN {core}.v_good_runs g
  ON g.stage = 'aggregate' AND g.run_date = s.metric_date AND g.run_id = s.run_id;

CREATE OR REPLACE VIEW {core}.v_series_test_current AS
SELECT s.* FROM {core}.series_test s
JOIN {core}.v_good_runs g
  ON g.stage = 'stats' AND g.run_date = s.metric_date AND g.run_id = s.run_id;

CREATE OR REPLACE VIEW {core}.v_coord_signals_current AS
SELECT s.* FROM {core}.coord_signals s
JOIN {core}.v_good_runs g
  ON g.stage = 'coaction' AND g.run_date = s.metric_date AND g.run_id = s.run_id;

CREATE OR REPLACE VIEW {core}.v_item_state_current AS
SELECT s.* FROM {core}.item_state s
JOIN {core}.v_good_runs g
  ON g.stage = 'detect' AND g.run_date = s.metric_date AND g.run_id = s.run_id;

CREATE OR REPLACE VIEW {agent}.v_briefs_current AS
SELECT s.* FROM {agent}.briefs s
JOIN {core}.v_good_runs g
  ON g.stage = 'brief' AND g.run_date = s.brief_date AND g.run_id = s.run_id;

-- Counter and rank reads: the newest read of each value from any good collect run (vendor curves restate past days).
CREATE OR REPLACE VIEW {core}.v_item_counter_daily_current AS
SELECT c.* FROM {core}.item_counter_daily c
WHERE c.run_id IN (SELECT r.run_id FROM {agent}.runs r WHERE r.stage = 'collect' AND r.status = 'ok')
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.obs_date, c.market, c.item_id, c.series, c.protocol, c.unit, c.pull_seq
                           ORDER BY c.available_at DESC) = 1;

-- 3.4 Series and their features

CREATE OR REPLACE VIEW {core}.v_series_daily AS
WITH h AS (SELECT hc.* FROM {core}.v_collection_health_current hc),
cd AS (SELECT cc.* FROM {core}.v_item_counter_daily_current cc),
rank_items AS (             -- every item ever seen on a list; the list watched it from the protocol's first day
  SELECT DISTINCT cd.item_id, cd.market, cd.platform, cd.series, cd.protocol
  FROM cd WHERE cd.lane_class = 'unbiased_rank' AND cd.unit = 'appearances' AND cd.series != 'x_trends'),
panel_items AS (            -- one row per panel series; item_daily has a row per platform, and a panel whose route
  SELECT i.item_id, i.market, i.platform, i.series, i.protocol    -- names none (the culture desk) spans several,
  FROM {core}.v_item_daily_current i WHERE i.lane_class = 'panel'  -- so it keeps the platform it first saw the item on
  QUALIFY ROW_NUMBER() OVER (PARTITION BY i.item_id, i.market, i.series, i.protocol
                             ORDER BY i.metric_date, i.platform IS NULL, i.platform) = 1),
panel_posts AS (            -- each post once: item_daily counts it on its own platform's row
  SELECT pp.item_id, pp.market, pp.series, pp.protocol, pp.metric_date, SUM(pp.posts) posts
  FROM {core}.v_item_daily_current pp WHERE pp.lane_class = 'panel'
  GROUP BY pp.item_id, pp.market, pp.series, pp.protocol, pp.metric_date),
u AS (
  SELECT ri.item_id, ri.market, ri.platform, ri.series, ri.protocol, 'unbiased_rank' lane_class, h.day,
    IF(h.valid, IFNULL(ra.value, 0), NULL) value, h.units_ok trials       -- zero only on a valid day
  FROM rank_items ri
  JOIN h ON h.market = ri.market AND h.series = ri.series AND h.protocol = ri.protocol
  LEFT JOIN cd ra ON ra.item_id = ri.item_id AND ra.market = ri.market AND ra.series = ri.series
    AND ra.protocol = ri.protocol AND ra.unit = 'appearances' AND ra.obs_date = h.day
  UNION ALL
  SELECT pi.item_id, pi.market, pi.platform, pi.series, pi.protocol, 'panel', h.day,
    IF(h.valid, IFNULL(pd.posts, 0) * h.k, NULL), NULL
  FROM panel_items pi
  JOIN h ON h.market = pi.market AND h.series = pi.series AND h.protocol = pi.protocol
  LEFT JOIN panel_posts pd ON pd.item_id = pi.item_id AND pd.market = pi.market
    AND pd.series = pi.series AND pd.protocol = pi.protocol AND pd.metric_date = h.day
  UNION ALL
  SELECT cv.item_id, cv.market, cv.platform, cv.series, cv.protocol, 'unbiased_counter', cv.obs_date,
    IF(cv.source = 'vendor_history' OR hv.valid, cv.value, NULL), NULL      -- no row before the first read
  FROM cd cv
  LEFT JOIN h hv ON hv.market = cv.market AND hv.series = cv.series AND hv.protocol = cv.protocol AND hv.day = cv.obs_date
  WHERE cv.lane_class = 'unbiased_counter' AND cv.unit = 'delta' AND cv.series != 'counter_post_views')
SELECT CONCAT(u.item_id, '|', u.market, '|', u.series, '|', u.protocol) series_id, u.*
FROM u;

CREATE OR REPLACE TABLE FUNCTION {core}.tvf_series_signal(d DATE) AS
WITH prior AS (             -- observed days before d since the series' protocol started
  SELECT sd.series_id, COUNTIF(sd.value IS NOT NULL) obs_prior
  FROM {core}.v_series_daily sd WHERE sd.day < d GROUP BY sd.series_id),
first_seen AS (             -- first day the item was present on any measured series in the market
  SELECT sd.item_id, sd.market, MIN(sd.day) first_measured
  FROM {core}.v_series_daily sd WHERE sd.day <= d AND sd.value > 0
  GROUP BY sd.item_id, sd.market),
w AS (
  SELECT sd.*,
    ARRAY_AGG(STRUCT(sd.day AS day, sd.value AS y, sd.trials AS n))       -- BigQuery allows no IGNORE NULLS here;
      OVER (s RANGE BETWEEN 28 PRECEDING AND 1 PRECEDING) hist_all,         -- wh keeps the valid days
    AVG(sd.value) OVER (s RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) v3,
    AVG(sd.value) OVER (s RANGE BETWEEN 5 PRECEDING AND 3 PRECEDING) v3_prev,
    AVG(sd.value) OVER (s RANGE BETWEEN 8 PRECEDING AND 6 PRECEDING) v3_prev2,
    SUM(sd.value) OVER (s RANGE BETWEEN 6 PRECEDING AND CURRENT ROW) v7,
    MAX(sd.value) OVER (s RANGE BETWEEN 27 PRECEDING AND CURRENT ROW) peak28
  FROM {core}.v_series_daily sd
  WHERE sd.day BETWEEN DATE_SUB(d, INTERVAL 36 DAY) AND d
  WINDOW s AS (PARTITION BY sd.series_id ORDER BY UNIX_DATE(sd.day))),
wh AS (                     -- valid days of the previous 28
  SELECT w.* EXCEPT (hist_all), ARRAY(SELECT h FROM UNNEST(w.hist_all) h WHERE h.y IS NOT NULL) hist
  FROM w WHERE w.day = d),
m AS (
  SELECT w.*, IFNULL(ARRAY_LENGTH(w.hist), 0) obs28,
    (SELECT APPROX_QUANTILES(hh.y, 2)[SAFE_OFFSET(1)] FROM UNNEST(w.hist) hh) med,
    (SELECT AVG(hh.y) FROM UNNEST(w.hist) hh) hist_mean
  FROM wh w WHERE w.day = d)
SELECT m.series_id, m.item_id, m.market, m.platform, m.series, m.protocol, m.lane_class, cm.kind,
  m.value y, m.trials, m.hist, IFNULL(pr.obs_prior, 0) obs_prior, m.obs28, fs.first_measured,
  m.hist_mean, m.med, m.v3, m.v7, m.peak28,
  SAFE.LN(1 + m.v3) - SAFE.LN(1 + m.v3_prev) vel,            -- SAFE: a negative counter delta gives NULL, not an error
  (SAFE.LN(1 + m.v3) - SAFE.LN(1 + m.v3_prev)) - (SAFE.LN(1 + m.v3_prev) - SAFE.LN(1 + m.v3_prev2)) accel,
  (m.value - m.med) / GREATEST(1.4826 * (SELECT APPROX_QUANTILES(ABS(hh.y - m.med), 2)[SAFE_OFFSET(1)]
                                         FROM UNNEST(m.hist) hh), SAFE.SQRT(GREATEST(m.med, 0) + 1)) z_display,
  CASE WHEN IFNULL(pr.obs_prior, 0) < 14 THEN 'warmup'     -- no test before 14 observed days
       WHEN m.obs28 < 14 THEN 'thin'                       -- too many invalid days in the window
       WHEN pr.obs_prior < 28 THEN 'short'                 -- test with the cold-start prior
       ELSE 'ok' END baseline_state
FROM m
LEFT JOIN prior pr ON pr.series_id = m.series_id
LEFT JOIN first_seen fs ON fs.item_id = m.item_id AND fs.market = m.market
JOIN {core}.cultural_map cm ON cm.item_id = m.item_id AND cm.valid_to IS NULL;

-- 3.6 The item's window

CREATE OR REPLACE TABLE FUNCTION {core}.tvf_item_window(d DATE) AS
WITH o AS (                 -- each post once per item, market and lane group, dated by its first sighting
  SELECT pi.item_id, po.market, po.post_id,
    po.lane_class IN ('unbiased_rank', 'panel') AS measured, MIN(po.observed_date) first_day
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  WHERE po.observed_date BETWEEN DATE_SUB(d, INTERVAL 27 DAY) AND d
    AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY pi.item_id, po.market, po.post_id, measured),
p AS (
  SELECT o.*, o.first_day > DATE_SUB(d, INTERVAL 7 DAY) in7,
    ps.platform, ps.creator_id, ps.creator_tier_at_post tier, ps.published_at,
    IFNULL(cr_flags.coord_score, 0) >= 1 flagged,
    DATE_DIFF(o.first_day, DATE(cr.account_created_at), DAY) < 30 young,   -- account age, a bot signal; never person age
    IFNULL(pe.near_dup_size, 1) >= 3 near_dup, IFNULL(pe.sponsored, FALSE) sponsored
  FROM o
  JOIN {core}.posts ps ON ps.post_id = o.post_id
  LEFT JOIN {core}.creators cr ON cr.creator_id = ps.creator_id AND cr.platform = ps.platform
  LEFT JOIN (
    SELECT creator_id, MAX(IFNULL(coord_score, 0)) coord_score
    FROM {core}.creators GROUP BY creator_id
  ) cr_flags ON cr_flags.creator_id = ps.creator_id
  LEFT JOIN (               -- append-only, so one row per post with the cautious value of each field
    SELECT pe0.post_id, MAX(pe0.near_dup_size) near_dup_size, LOGICAL_OR(pe0.sponsored) sponsored
    FROM {core}.post_enrichment pe0 GROUP BY pe0.post_id
  ) pe ON pe.post_id = o.post_id),
cr3 AS (                    -- measured lanes: this 3-day window and the one before
  SELECT p.item_id, p.market, p.creator_id, LOGICAL_OR(p.flagged) flagged,
    COUNTIF(p.first_day > DATE_SUB(d, INTERVAL 3 DAY)) n3,
    COUNTIF(p.first_day BETWEEN DATE_SUB(d, INTERVAL 5 DAY) AND DATE_SUB(d, INTERVAL 3 DAY)) n3_prev
  FROM p WHERE p.measured GROUP BY p.item_id, p.market, p.creator_id),
fl AS (
  SELECT cr3.item_id, cr3.market,
    SUM(IF(cr3.flagged, 0, cr3.n3)) posts3, SUM(IF(cr3.flagged, 0, cr3.n3_prev)) posts3_prev,
    COUNTIF(cr3.creator_id IS NOT NULL AND cr3.n3 > 0 AND NOT cr3.flagged) creators3,
    SAFE_DIVIDE(MAX(cr3.n3), SUM(cr3.n3)) top_creator_share3
  FROM cr3 GROUP BY cr3.item_id, cr3.market),
a7 AS (
  SELECT p.item_id, p.market,
    COUNT(DISTINCT IF(p.in7, p.post_id, NULL)) posts7,
    SAFE_DIVIDE(COUNT(DISTINCT IF(p.in7 AND p.near_dup, p.post_id, NULL)), COUNT(DISTINCT IF(p.in7, p.post_id, NULL))) near_dup_share,
    SAFE_DIVIDE(COUNT(DISTINCT IF(p.in7 AND p.young, p.post_id, NULL)), COUNT(DISTINCT IF(p.in7, p.post_id, NULL))) young_share,
    SAFE_DIVIDE(COUNT(DISTINCT IF(p.in7 AND p.sponsored, p.post_id, NULL)), COUNT(DISTINCT IF(p.in7, p.post_id, NULL))) sponsored_share,
    COUNT(DISTINCT IF(p.in7 AND p.measured AND p.tier IN ('macro', 'mega'), p.post_id, NULL)) large_posts7,
    COUNT(DISTINCT IF(p.in7 AND p.platform = 'news', p.post_id, NULL)) news_posts7,
    MIN(IF(p.measured AND p.tier IN ('nano', 'micro'), p.published_at, NULL)) small_at,
    MIN(IF(p.measured AND p.tier IN ('macro', 'mega'), p.published_at, NULL)) large_at
  FROM p GROUP BY p.item_id, p.market),
t3 AS (                     -- top-3 creator share, 7 days, all evidence lanes
  SELECT c.item_id, c.market,       -- ranked in a subquery: BigQuery allows no aggregate inside UNNEST
    SAFE_DIVIDE(SUM(IF(c.rn <= 3, c.n, 0)), SUM(c.n)) top3_share
  FROM (SELECT cn.item_id, cn.market, cn.creator_id, cn.n,
          ROW_NUMBER() OVER (PARTITION BY cn.item_id, cn.market ORDER BY cn.n DESC, cn.creator_id) rn
        FROM (SELECT p.item_id, p.market, p.creator_id, COUNT(DISTINCT p.post_id) n
              FROM p WHERE p.in7 GROUP BY p.item_id, p.market, p.creator_id) cn) c
  GROUP BY c.item_id, c.market),
bu AS (                     -- share of 7-day posts in the busiest 10 minutes
  SELECT b.item_id, b.market, SAFE_DIVIDE(MAX(b.n), SUM(b.n)) burst_share
  FROM (SELECT p.item_id, p.market, DIV(UNIX_SECONDS(p.published_at), 600) bucket, COUNT(DISTINCT p.post_id) n
        FROM p WHERE p.in7 AND p.published_at IS NOT NULL GROUP BY p.item_id, p.market, bucket) b
  GROUP BY b.item_id, b.market),
sn AS (SELECT p.item_id, COUNT(DISTINCT p.post_id) seen7_all FROM p WHERE p.in7 GROUP BY p.item_id),
cv AS (                     -- largest 7-day counter delta, for the Not assessed coverage test
  SELECT x.item_id, MAX(x.delta7) delta7
  FROM (SELECT sd.item_id, sd.series_id, SUM(sd.value) delta7 FROM {core}.v_series_daily sd
        WHERE sd.lane_class = 'unbiased_counter' AND sd.day BETWEEN DATE_SUB(d, INTERVAL 6 DAY) AND d
        GROUP BY sd.item_id, sd.series_id) x
  GROUP BY x.item_id),
geo AS (
  SELECT i.item_id, i.market, SUM(i.geo_known_posts) geo_known_posts7, SUM(i.local_posts) local_posts7
  FROM {core}.v_item_daily_current i
  WHERE i.lane_class = '_any' AND i.metric_date BETWEEN DATE_SUB(d, INTERVAL 6 DAY) AND d
  GROUP BY i.item_id, i.market),
fp AS (                     -- presence, 14 days; shown only above the placebo base rate
  SELECT i.item_id, i.market, COUNT(DISTINCT i.platform) found_platforms14
  FROM {core}.v_item_daily_current i
  WHERE i.lane_class NOT IN ('_any', 'legacy') AND i.metric_date BETWEEN DATE_SUB(d, INTERVAL 13 DAY) AND d
  GROUP BY i.item_id, i.market),
bd AS (                     -- board or chart entry today (never the X trends archive)
  SELECT c.item_id, c.market, TRUE board_entry
  FROM {core}.v_item_counter_daily_current c
  WHERE c.obs_date = d AND c.is_board AND c.unit = 'appearances' AND c.value > 0 AND c.series != 'x_trends'
  GROUP BY c.item_id, c.market),
t10 AS (                    -- top 10 of a board or feed on 2 consecutive pulls, ending today
  SELECT DISTINCT a.item_id, a.market, TRUE top10_twice
  FROM {core}.v_item_counter_daily_current a
  JOIN {core}.v_item_counter_daily_current b
    ON b.item_id = a.item_id AND b.market = a.market AND b.series = a.series AND b.protocol = a.protocol
   AND b.unit = 'rank' AND b.pull_seq = a.pull_seq - 1 AND b.value <= 10
   AND b.obs_date BETWEEN DATE_SUB(a.obs_date, INTERVAL 35 DAY) AND a.obs_date   -- pull_seq restarts after 35 days
  WHERE a.obs_date = d AND a.unit = 'rank' AND a.value <= 10 AND a.series != 'x_trends'),
keys AS (
  SELECT p.item_id, p.market FROM p UNION DISTINCT
  SELECT bd.item_id, bd.market FROM bd UNION DISTINCT
  SELECT t10.item_id, t10.market FROM t10)
SELECT k.item_id, k.market, fl.posts3, fl.posts3_prev, fl.creators3, fl.top_creator_share3,
  a7.posts7, a7.near_dup_share, a7.young_share, a7.sponsored_share, a7.large_posts7, a7.news_posts7,
  a7.small_at, a7.large_at, t3.top3_share, bu.burst_share, sn.seen7_all, cv.delta7,
  geo.geo_known_posts7, geo.local_posts7, fp.found_platforms14,
  IFNULL(bd.board_entry, FALSE) board_entry, IFNULL(t10.top10_twice, FALSE) top10_twice
FROM keys k
LEFT JOIN fl ON fl.item_id = k.item_id AND fl.market = k.market
LEFT JOIN a7 ON a7.item_id = k.item_id AND a7.market = k.market
LEFT JOIN t3 ON t3.item_id = k.item_id AND t3.market = k.market
LEFT JOIN bu ON bu.item_id = k.item_id AND bu.market = k.market
LEFT JOIN sn ON sn.item_id = k.item_id
LEFT JOIN cv ON cv.item_id = k.item_id
LEFT JOIN geo ON geo.item_id = k.item_id AND geo.market = k.market
LEFT JOIN fp ON fp.item_id = k.item_id AND fp.market = k.market
LEFT JOIN bd ON bd.item_id = k.item_id AND bd.market = k.market
LEFT JOIN t10 ON t10.item_id = k.item_id AND t10.market = k.market;

CREATE OR REPLACE TABLE FUNCTION {core}.tvf_placebo_base(d DATE) AS
WITH pl AS (
  SELECT DISTINCT q.item_id, q.market FROM {core}.seed_queue q
  WHERE q.lane = 'placebo' AND q.seed_date BETWEEN DATE_SUB(d, INTERVAL 27 DAY) AND d),
found AS (
  SELECT pl.item_id, pl.market, COUNT(DISTINCT i.platform) n
  FROM pl LEFT JOIN {core}.v_item_daily_current i
    ON i.item_id = pl.item_id AND i.market = pl.market AND i.lane_class NOT IN ('_any', 'legacy')
   AND i.metric_date BETWEEN DATE_SUB(d, INTERVAL 13 DAY) AND d
  GROUP BY pl.item_id, pl.market),
rise AS (
  SELECT pl.item_id, pl.market, COUNT(DISTINCT st.platform) n
  FROM pl LEFT JOIN {core}.v_series_test_current st
    ON st.item_id = pl.item_id AND st.market = pl.market AND st.significant
   AND st.metric_date BETWEEN DATE_SUB(d, INTERVAL 13 DAY) AND d
  GROUP BY pl.item_id, pl.market)
SELECT f.market, COUNT(*) placebo_items,
  APPROX_QUANTILES(f.n, 20)[OFFSET(19)] found_p95, APPROX_QUANTILES(r.n, 20)[OFFSET(19)] rising_p95
FROM found f JOIN rise r ON r.item_id = f.item_id AND r.market = f.market
GROUP BY f.market;

CREATE OR REPLACE VIEW {core}.v_item_market_scope AS
WITH state_keys AS (
  SELECT DISTINCT metric_date, item_id, market
  FROM {core}.v_item_state_current
  WHERE market IN ('ZA', 'NG', 'KE')
), seen AS (
  SELECT s.metric_date, s.item_id, s.market, po.post_id,
    LOGICAL_OR(po.lane_class IN ('unbiased_rank', 'panel')) measured,
    LOGICAL_OR(IFNULL(po.lane, '') != 'confirm') beyond_confirm
  FROM state_keys s
  JOIN {core}.post_observations po ON po.market = s.market
  JOIN {core}.post_items pi ON pi.post_id = po.post_id AND pi.item_id = s.item_id
  WHERE po.observed_date BETWEEN DATE_SUB(s.metric_date, INTERVAL 6 DAY) AND s.metric_date
    AND po.lane_class != 'legacy'
    AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY s.metric_date, s.item_id, s.market, po.post_id
), creator_ranked AS (
  SELECT seen.metric_date, seen.item_id, seen.market, ps.post_id, ps.geo_market, ps.geo_confidence,
    ps.geo_source, seen.measured, IFNULL(ps.engagement, 0) eng,
    ROW_NUMBER() OVER (PARTITION BY seen.metric_date, seen.item_id, seen.market,
                                    IFNULL(ps.creator_id, ps.post_id)
                       ORDER BY seen.measured DESC, IFNULL(ps.engagement, 0) DESC, ps.post_id) creator_rank
  FROM seen
  JOIN {core}.posts ps ON ps.post_id = seen.post_id
  WHERE DATE(ps.published_at, CASE seen.market
      WHEN 'ZA' THEN 'Africa/Johannesburg'
      WHEN 'NG' THEN 'Africa/Lagos'
      WHEN 'KE' THEN 'Africa/Nairobi'
    END) BETWEEN DATE_SUB(seen.metric_date, INTERVAL 6 DAY) AND seen.metric_date
    -- As evidence.sql: a suppressed creator's posts leave before ranking, so the count describes the pack.
    AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = ps.creator_id)
    -- A post seen only by the brief's confirm search counts only when local by the scored_posts rule below,
    -- as before confirm finds were linked to their item.
    AND (seen.beyond_confirm
      OR (ps.geo_market = seen.market AND IFNULL(ps.geo_confidence, 0) >= 0.7
          AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'))
      OR (NOT IFNULL(NULLIF(ps.geo_market, '') != seen.market AND ps.geo_confidence >= 0.7
                         AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
      AND EXISTS (
        SELECT 1
        FROM {core}.v_post_source_markets v
        CROSS JOIN UNNEST(v.source_sightings) sight
        WHERE v.post_id = ps.post_id
          AND sight.source_market = seen.market
          AND sight.obs_date BETWEEN DATE_SUB(seen.metric_date, INTERVAL 6 DAY) AND seen.metric_date
      )))
), ranked_pack AS (
  SELECT creator_ranked.*,
    ROW_NUMBER() OVER (PARTITION BY metric_date, item_id, market
                       ORDER BY measured DESC, eng DESC, post_id) pack_rank
  FROM creator_ranked
  WHERE creator_rank <= 2
), eligible_posts AS (
  SELECT * FROM ranked_pack WHERE pack_rank <= 12
), scored_posts AS (
  SELECT e.metric_date, e.item_id, e.market, e.post_id,
    ((e.geo_market = e.market AND IFNULL(e.geo_confidence, 0) >= 0.7
      AND e.geo_source IN ('ext_region', 'home_market', 'place_mention'))
    OR (NOT IFNULL(NULLIF(e.geo_market, '') != e.market AND e.geo_confidence >= 0.7
                       AND e.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
    AND EXISTS (
      SELECT 1
      FROM {core}.v_post_source_markets v
      CROSS JOIN UNNEST(v.source_sightings) sight
      WHERE v.post_id = e.post_id
        AND sight.source_market = e.market
        AND sight.obs_date BETWEEN DATE_SUB(e.metric_date, INTERVAL 6 DAY) AND e.metric_date
    ))) in_market
  FROM eligible_posts e
), post_counts AS (
  SELECT metric_date, item_id, market,
    COUNT(DISTINCT IF(in_market, post_id, NULL)) market_posts7,
    COUNT(DISTINCT post_id) total_posts7
  FROM scored_posts
  GROUP BY metric_date, item_id, market
), scoped AS (
  SELECT s.metric_date, s.item_id, s.market,
    IFNULL(c.market_posts7, 0) market_posts7,
    IFNULL(c.total_posts7, 0) total_posts7,
    SAFE_DIVIDE(IFNULL(c.market_posts7, 0), IFNULL(c.total_posts7, 0)) market_share7
  FROM state_keys s
  LEFT JOIN post_counts c USING (metric_date, item_id, market)
), scope_values AS (
  SELECT scoped.*, IF(market_share7 > 0.5, 'market', 'global') market_scope
  FROM scoped
)
SELECT metric_date, item_id, market, market_scope, market_posts7, total_posts7, market_share7
FROM scope_values;

-- tvf_post_items(d): the counted post to item links for date d (C4 v3 section 16, I-1 and I-4). Dated topic
-- links (cluster, cluster_pan and lineage links that carry linked_on) are ranked per post and market, lineage
-- first, then the market run, then the pooled run, ties to the lower item id; a link counts from linked_on and
-- until its post_item_end. Every row written before linked_on existed, and every link that is not a topic link,
-- passes through unchanged with a NULL market, so no count of history moves. The scan of post_observations is
-- bounded to the 28 days ending at d. Read by the locality_v2 members statement.
CREATE OR REPLACE TABLE FUNCTION {core}.tvf_post_items(d DATE) AS
WITH raw AS (
  SELECT pi.post_id, pi.item_id, pi.via, pi.link_market, pi.linked_on
  FROM {core}.post_items pi
  WHERE pi.linked_on IS NULL OR pi.linked_on <= d
  UNION ALL
  SELECT l.post_id, l.item_id, 'lineage', l.link_market, l.linked_on
  FROM {core}.post_item_lineage l
  WHERE l.linked_on <= d),
live AS (
  SELECT r.* FROM raw r
  WHERE NOT EXISTS (SELECT 1 FROM {core}.post_item_end e
                    WHERE e.post_id = r.post_id AND e.item_id = r.item_id AND e.ended_on <= d)),
seen AS (
  SELECT DISTINCT o.post_id, o.market FROM {core}.post_observations o
  WHERE o.observed_date BETWEEN DATE_SUB(d, INTERVAL 27 DAY) AND d),
dated AS (
  SELECT l.post_id, l.item_id, s.market,
    CASE l.via WHEN 'lineage' THEN 0 WHEN 'cluster' THEN 1 ELSE 2 END prec
  FROM live l
  JOIN seen s ON s.post_id = l.post_id
  WHERE l.linked_on IS NOT NULL AND l.via IN ('cluster', 'cluster_pan', 'lineage')
    AND (l.link_market IS NULL OR l.link_market = s.market)),
winner AS (
  SELECT t.post_id, t.item_id, t.market FROM dated t
  QUALIFY ROW_NUMBER() OVER (PARTITION BY t.post_id, t.market ORDER BY t.prec, t.item_id) = 1)
SELECT l.post_id, l.item_id, l.via, CAST(NULL AS STRING) market
FROM live l WHERE l.linked_on IS NULL OR l.via NOT IN ('cluster', 'cluster_pan', 'lineage')
UNION ALL
SELECT w.post_id, w.item_id, 'topic', w.market FROM winner w;
