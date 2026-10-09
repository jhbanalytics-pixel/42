-- Watch matches, detect side (BUILD.md 2.9, core/api/contract.md sections 10.5 and 10.7), in BigQuery Standard
-- SQL. {core} and {agent} are the dataset names; core/detect/watches.py runs each named statement. Reads are
-- read-only; the one write appends to watch_matches.

-- name: watches
-- The active watches as they stand: L1's v_watches_current holds the latest row per watch_id.
SELECT w.watch_id, w.target, w.market, w.rule
FROM {agent}.v_watches_current w
WHERE w.status = 'active'
ORDER BY w.watch_id;

-- name: items
-- Today's item_state rows of this detect run that the gate would not hold on item_state alone, with the open
-- cultural_map row: eligible (state.sql already needs authenticity, geo_status and map status for that),
-- authenticity other than likely_coordinated, geo_status other than not_local, sponsored_share under 0.5 (G5)
-- and map status active (so never generic). Each is checked here as well, and a NULL never lets an item through.
-- breakout_creators is the item's largest creator count in breakout_signals for the day in that market, over
-- any run that day (core/detect/breakout.py writes it before this step), and NULL when it has none.
SELECT s.item_id, s.market, s.state, s.main_ratio, s.creators3, cm.kind, cm.canonical_key, cm.label, cm.aliases,
  b.breakout_creators
FROM {core}.item_state s
JOIN {core}.cultural_map cm ON cm.item_id = s.item_id AND cm.valid_to IS NULL
LEFT JOIN (
  SELECT bs.item_id, bs.market, MAX(bs.creators) breakout_creators
  FROM {core}.breakout_signals bs
  WHERE bs.metric_date = @d
  GROUP BY bs.item_id, bs.market) b ON b.item_id = s.item_id AND b.market = s.market
WHERE s.metric_date = @d AND s.run_id = @run_id
  AND IFNULL(s.eligible, FALSE)
  AND IFNULL(s.authenticity, '') != 'likely_coordinated'
  AND IFNULL(s.locality_status, '') NOT IN ('unreadable', 'missing')
  AND IF(s.locality_basis = 'locality_v2.1', TRUE, IFNULL(s.geo_status, '') != 'not_local')
  AND IFNULL(s.sponsored_share, 0) < 0.5
  AND IFNULL(cm.status, '') = 'active'
ORDER BY s.market, s.item_id;

-- name: tones
-- Each item's tone today and the day before in each market, from v_item_tone_daily (agent_views.sql, which the
-- detect job applies before this step): the mean enrichment tone sign of its enriched posts that day, NULL
-- under 5 of them. An item with no tone on either day has no row. core/detect/watches.py runs this only when an
-- active watch has tone_flip, and reads a failure (the view not there) as no tones.
SELECT t.item_id, t.market,
  MAX(IF(t.metric_date = @d, t.tone, NULL)) tone_today,
  MAX(IF(t.metric_date = DATE_SUB(@d, INTERVAL 1 DAY), t.tone, NULL)) tone_before
FROM {agent}.v_item_tone_daily t
WHERE t.metric_date BETWEEN DATE_SUB(@d, INTERVAL 1 DAY) AND @d
GROUP BY t.item_id, t.market
HAVING MAX(IF(t.metric_date = @d, t.tone, NULL)) IS NOT NULL
  OR MAX(IF(t.metric_date = DATE_SUB(@d, INTERVAL 1 DAY), t.tone, NULL)) IS NOT NULL;

-- name: append
-- Append today's matches. A row whose watch, day, item and market the table already holds is left out, so a
-- rerun on the same day adds nothing.
INSERT INTO {agent}.watch_matches (watch_id, match_date, item_id, market, method, run_id)
SELECT n.watch_id, n.match_date, n.item_id, n.market, n.method, n.run_id
FROM UNNEST(@rows) n
WHERE NOT EXISTS (
  SELECT 1 FROM {agent}.watch_matches m
  WHERE m.match_date = n.match_date AND m.watch_id = n.watch_id AND m.item_id = n.item_id
    AND m.market = n.market);

-- name: appended
-- The rows this step's run_id appended for the day.
SELECT COUNT(*) n FROM {agent}.watch_matches m WHERE m.match_date = @d AND m.run_id = @run_id;
