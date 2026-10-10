-- The seed loop, detect side (BUILD.md 2.4, DATA.md section 5), in BigQuery Standard SQL. core/detect/seeds.py
-- reads each statement after its "name:" comment. {core} and {agent} are filled in by core/detect/sqlrun.py.
-- Every statement reads, except append, which only inserts rows the queue does not hold yet.

-- name: spend
-- Credits spent in the seed day's month up to @d, over every job (MONTHLY covers everything). A row with no
-- charge yet counts at its quote.
SELECT IFNULL(SUM(IFNULL(l.credits_charged, l.credits_quoted)), 0) spent
FROM {core}.credit_ledger l
WHERE l.trend_date BETWEEN @month_start AND @d;

-- name: cost
-- Credits per call by route over the 28 days to @d.
SELECT IFNULL(l.route, l.endpoint) route,
  SUM(IFNULL(l.credits_charged, l.credits_quoted)) / SUM(IFNULL(l.calls, 1)) cost_per_call
FROM {core}.credit_ledger l
WHERE l.trend_date BETWEEN DATE_SUB(@d, INTERVAL 27 DAY) AND @d AND IFNULL(l.route, l.endpoint) IS NOT NULL
GROUP BY 1
HAVING SUM(IFNULL(l.calls, 1)) > 0;

-- name: candidates
-- Today's eligible, ranked items per market with the map row, the main series' acceleration, the item's
-- language (the most common first language of its posts sighted in the market in 7 days, known languages
-- before unknown) and the c-TF-IDF terms of its latest cluster in 7 days (the market's before 'pan').
-- Items with status generic are left out here; seeds.py also checks the stoplist. The states are pinned to
-- the detect run that wrote them (@run_id is not ok yet while seeds runs inside it, so v_item_state_current
-- would not show them).
WITH s AS (
  SELECT x.market, x.item_id, x.state, x.worth_pct, x.novelty, x.main_series_id
  FROM {core}.item_state x
  WHERE x.metric_date = @d AND x.run_id = @run_id AND x.market IN ('ZA', 'NG', 'KE') AND x.eligible
    AND IFNULL(x.locality_status, '') NOT IN ('unreadable', 'missing')
    AND x.worth_pct IS NOT NULL),
pl AS (
  SELECT DISTINCT po.market, po.post_id
  FROM {core}.post_observations po
  WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')),
ln AS (
  SELECT pl.market, pi.item_id, pe.langs[SAFE_OFFSET(0)] lang, COUNT(*) n
  FROM pl
  JOIN {core}.post_items pi ON pi.post_id = pl.post_id
  LEFT JOIN {core}.post_enrichment pe ON pe.post_id = pl.post_id
  GROUP BY pl.market, pi.item_id, lang),
lang AS (
  SELECT ln.market, ln.item_id, ARRAY_AGG(ln.lang ORDER BY ln.lang IS NULL, ln.n DESC, ln.lang LIMIT 1)[OFFSET(0)] lang
  FROM ln GROUP BY ln.market, ln.item_id),
cl AS (
  SELECT s.market, s.item_id, k.keywords, k.local_terms
  FROM s JOIN {core}.clusters k ON k.item_id = s.item_id AND (UPPER(k.market) = s.market OR k.market = 'pan')
  WHERE k.cluster_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  QUALIFY ROW_NUMBER() OVER (PARTITION BY s.market, s.item_id
                             ORDER BY k.market = 'pan', k.cluster_date DESC, k.cluster_id) = 1)
SELECT s.market, s.item_id, cm.kind, cm.canonical_key, cm.label, s.state, s.worth_pct, s.novelty,
  st.accel, lang.lang, cl.keywords, cl.local_terms
FROM s
JOIN {core}.cultural_map cm ON cm.item_id = s.item_id AND cm.valid_to IS NULL
LEFT JOIN {core}.v_series_test_current st ON st.series_id = s.main_series_id AND st.metric_date = @d
LEFT JOIN lang ON lang.market = s.market AND lang.item_id = s.item_id
LEFT JOIN cl ON cl.market = s.market AND cl.item_id = s.item_id
WHERE IFNULL(cm.status, '') != 'generic';

-- name: queue
-- Queue rows from 90 days before @d to the seed day: recent expansions, rows already queued for tomorrow,
-- and the anchors' last runs.
SELECT q.seed_date, q.market, q.item_id, q.query, q.kind, q.lane, q.priority, q.template, q.ttl_days,
  q.credits_estimate, q.yield_posts, q.yield_new_creators
FROM {core}.seed_queue q
WHERE q.seed_date BETWEEN DATE_SUB(@d, INTERVAL 89 DAY) AND DATE_ADD(@d, INTERVAL 1 DAY);

-- name: trials
-- Exploration seeds in the 90 days to @d whose 7-day window has closed and whose yield collect has recorded.
-- Success: the seed found posts or new creators and the item was Rising in that market within 7 days of it.
-- A seed that was not a success counts as a failure only when every day of its window had a good detect run;
-- otherwise it is left out until the missing day's detect run is in, and leaves the 90 days like any seed.
WITH q AS (
  SELECT q0.seed_date, q0.market, q0.item_id, q0.kind,
    MAX(q0.yield_posts) yield_posts, MAX(q0.yield_new_creators) yield_new_creators
  FROM {core}.seed_queue q0
  WHERE q0.lane = 'exploration' AND q0.item_id IS NOT NULL
    AND q0.seed_date BETWEEN DATE_SUB(@d, INTERVAL 89 DAY) AND DATE_SUB(@d, INTERVAL 6 DAY)
  GROUP BY q0.seed_date, q0.market, q0.item_id, q0.kind
  HAVING MAX(q0.yield_posts) IS NOT NULL OR MAX(q0.yield_new_creators) IS NOT NULL),
r AS (
  SELECT q.seed_date, q.market, q.item_id, IFNULL(LOGICAL_OR(s.state = 'rising'), FALSE) rose
  FROM q LEFT JOIN {core}.v_item_state_current s
    ON s.item_id = q.item_id AND s.market = q.market
   AND s.metric_date BETWEEN q.seed_date AND DATE_ADD(q.seed_date, INTERVAL 6 DAY)
  GROUP BY q.seed_date, q.market, q.item_id),
g AS (
  SELECT q.seed_date, q.market, q.item_id, COUNT(*) good_days
  FROM q JOIN {core}.v_good_runs gr
    ON gr.stage = 'detect' AND gr.run_date BETWEEN q.seed_date AND DATE_ADD(q.seed_date, INTERVAL 6 DAY)
  GROUP BY q.seed_date, q.market, q.item_id),
t AS (
  SELECT q.seed_date, q.market, q.item_id, q.kind,
    (IFNULL(q.yield_posts, 0) > 0 OR IFNULL(q.yield_new_creators, 0) > 0) AND r.rose success,
    IFNULL(g.good_days, 0) good_days
  FROM q
  JOIN r ON r.seed_date = q.seed_date AND r.market = q.market AND r.item_id = q.item_id
  LEFT JOIN g ON g.seed_date = q.seed_date AND g.market = q.market AND g.item_id = q.item_id)
SELECT t.seed_date, t.market, t.item_id, t.kind, t.success
FROM t
WHERE t.success OR t.good_days = 7;

-- name: drift_seeds
-- The week's earned seeds (seed days @d minus 6 to @d; placebo left out) with the item's language as in
-- candidates.
WITH pl AS (
  SELECT DISTINCT po.market, po.post_id
  FROM {core}.post_observations po
  WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')),
ln AS (
  SELECT pl.market, pi.item_id, pe.langs[SAFE_OFFSET(0)] lang, COUNT(*) n
  FROM pl
  JOIN {core}.post_items pi ON pi.post_id = pl.post_id
  LEFT JOIN {core}.post_enrichment pe ON pe.post_id = pl.post_id
  GROUP BY pl.market, pi.item_id, lang),
lang AS (
  SELECT ln.market, ln.item_id, ARRAY_AGG(ln.lang ORDER BY ln.lang IS NULL, ln.n DESC, ln.lang LIMIT 1)[OFFSET(0)] lang
  FROM ln GROUP BY ln.market, ln.item_id)
SELECT q.market, q.kind, q.template, q.credits_estimate, lang.lang
FROM {core}.seed_queue q
LEFT JOIN lang ON lang.market = q.market AND lang.item_id = q.item_id
WHERE q.seed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  AND q.lane IN ('expansion', 'exploration', 'anchor') AND IFNULL(q.credits_estimate, 0) > 0;

-- name: drift_feeds
-- The week's unseeded feeds: posts sighted in the sweep lane on rank lists (unbiased_rank), counted by platform,
-- by the first language of the post, and by the kind of each item on the post.
WITH p AS (
  SELECT DISTINCT po.post_id, po.platform
  FROM {core}.post_observations po
  WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.lane = 'sweep' AND po.lane_class = 'unbiased_rank')
SELECT 'platform' dim, p.platform cat, COUNT(*) n FROM p GROUP BY p.platform
UNION ALL
SELECT 'language' dim, pe.langs[SAFE_OFFSET(0)] cat, COUNT(*) n
FROM p LEFT JOIN {core}.post_enrichment pe ON pe.post_id = p.post_id
GROUP BY cat
UNION ALL
SELECT 'kind' dim, cm.kind cat, COUNT(*) n
FROM p
JOIN {core}.post_items pi ON pi.post_id = p.post_id
JOIN {core}.cultural_map cm ON cm.item_id = pi.item_id AND cm.valid_to IS NULL
GROUP BY cm.kind;

-- name: append
-- Append tomorrow's seeds. A row whose seed day, market, lane and item (or query) the queue already holds is
-- left out, so a rerun adds nothing.
INSERT INTO {core}.seed_queue (seed_date, market, item_id, query, kind, lane, priority, template, ttl_days,
  credits_estimate)
SELECT n.seed_date, n.market, n.item_id, n.query, n.kind, n.lane, n.priority, n.template, n.ttl_days,
  n.credits_estimate
FROM UNNEST(@rows) n
WHERE NOT EXISTS (
  SELECT 1 FROM {core}.seed_queue q
  WHERE q.seed_date = n.seed_date AND q.market = n.market AND q.lane = n.lane
    AND IFNULL(q.item_id, '') = IFNULL(n.item_id, '') AND IFNULL(q.query, '') = IFNULL(n.query, ''))
