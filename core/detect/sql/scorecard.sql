-- The weekly detection scorecard, ENGINE.md section 6 (BUILD.md 2.7). Read-only: every statement is a SELECT.
-- Params: @market (ZA, NG or KE), @week_start (a Monday), @week_end (the Sunday after it) and @iso_week
-- ("2026-W41"). {core} and {agent} are the dataset names; lead_time reads the held-out reference list from
-- core/detect/reference/ground_truth.yaml as an array literal, filled in by core/detect/scorecard.py.
-- Each query returns one row per item, entry, card, moment or bucket (cost returns one row), so the rows that
-- core/detect/scorecard.py hashes are the rows its number is counted from. time_to_detect, lead_time, recall
-- and breadth_platforms share one test of a trend signal, the fragment _trend_rows below. The scorecard's own
-- windows are placeholders filled from constants in scorecard.py, so the SQL and the unit text cannot drift:
-- lead_window (LEAD_WINDOW), recall_days (RECALL_DAYS), min_term (MIN_TERM) and data_start (DATA_START, the
-- earliest date 42 holds data for). The 2, 3, 5 and 27 day windows below are state.sql's and tvf_item_window's.

-- name: _trend_rows
-- Shared CTEs, filled in where a query has the placeholder _trend_rows|K in braces. K is week (rows from
-- @week_start), lead (from lead_window days before it) or all (from data_start), and every scan below carries
-- that literal lower bound (series tests 2 days earlier, posts 27 days earlier, never before data_start) so
-- BigQuery prunes by date. sg holds the market's trend-signal rows from there to recall_days after the week:
-- eligible (an active map row, neither likely coordinated nor not_local) and Emerging, Rising, Peaking or
-- Mainstream, or Seasonal or Recurring when the row also qualifies as Rising or Emerging. state.sql (DATA.md 3.7)
-- gives Seasonal and Recurring to anything that qualifies for Rising, Emerging, Spike or New to 42, and writes
-- the state the row had before that override to base_state. A Seasonal or Recurring row counts when its
-- base_state is rising or emerging (sb) and never when it is spike or new_to_42. Rows written before the column
-- existed hold NULL there, and only those are rechecked (sd, the fallback); a later row with NULL base_state
-- goes the same way, and as state.sql found neither Rising nor Emerging for it, a recheck that can only
-- undercount does not count it. The recheck rebuilds the inputs state.sql used that day: its floors and novelty from the row, and
-- sig_today, sig_ratio_today, sig_platforms_today, other_market, other_platform_global and obs_days from
-- v_series_test_current, posts3_prev from the posts as tvf_item_window counts them. It reads the tables as they
-- are now, so it is only trusted where that cannot overcount. On both paths a Seasonal or Recurring row counts
-- only when its day's good detect run shows state.sql read the stats runs that are good now: no ok stats run
-- for the 2 days before finished after that detect run started, at most one ok stats run for the day itself
-- finished after it started (the detect job's own stats step), and none after it finished; an unknown time
-- counts as late, and a day with no good detect run never counts. posts3_prev counts every post, whatever the
-- creator's flag and whether or not it has a posts row, so a creator flagged after the detect run can only
-- raise it. Both can only undercount. On both paths a row held at Seasonal or Recurring only by the two-day
-- hysteresis never counts.
s0 AS (
  SELECT s.* FROM {core}.v_item_state_current s
  WHERE s.market = @market AND s.metric_date BETWEEN {since}
    AND DATE_ADD(@week_end, INTERVAL {recall_days} DAY) AND s.eligible),
gr AS (                     -- ok detect and stats runs with their start and finish times
  SELECT r.stage, r.run_date, r.started_at, r.finished_at FROM {agent}.runs r
  WHERE r.status = 'ok' AND r.stage IN ('detect', 'stats')
    AND r.run_date BETWEEN {since2} AND DATE_ADD(@week_end, INTERVAL {recall_days} DAY)),
dt AS (                     -- each day's good detect run: the newest ok one, as v_good_runs picks it
  SELECT g.run_date d, MAX(g.finished_at) finished_at FROM gr g WHERE g.stage = 'detect' GROUP BY g.run_date),
dr AS (
  SELECT dt.d, dt.finished_at, MIN(g.started_at) started_at
  FROM dt JOIN gr g ON g.stage = 'detect' AND g.run_date = dt.d AND g.finished_at = dt.finished_at
  GROUP BY dt.d, dt.finished_at),
ok AS (                     -- days whose detect run read the stats runs that are good now
  SELECT dr.d FROM dr
  LEFT JOIN gr sr ON sr.stage = 'stats' AND sr.run_date BETWEEN DATE_SUB(dr.d, INTERVAL 2 DAY) AND dr.d
  GROUP BY dr.d, dr.started_at, dr.finished_at
  HAVING NOT IFNULL(LOGICAL_OR(sr.run_date < dr.d AND IFNULL(sr.finished_at > dr.started_at, TRUE)), FALSE)
    AND COUNTIF(sr.run_date = dr.d AND IFNULL(sr.finished_at > dr.started_at, TRUE)) <= 1
    AND NOT IFNULL(LOGICAL_OR(sr.run_date = dr.d AND IFNULL(sr.finished_at > dr.finished_at, TRUE)), FALSE)),
sb AS (                     -- Seasonal and Recurring rows classified that day, resting on Rising or Emerging
  SELECT s0.item_id, s0.metric_date FROM s0 JOIN ok ON ok.d = s0.metric_date
  WHERE s0.state IN ('seasonal', 'recurring') AND s0.state_raw = s0.state
    AND s0.base_state IN ('rising', 'emerging')),
sd AS (                     -- the same rows with no base_state (written before it) and the floors met
  SELECT s0.item_id, s0.metric_date, s0.untested FROM s0 JOIN ok ON ok.d = s0.metric_date
  WHERE s0.state IN ('seasonal', 'recurring') AND s0.state_raw = s0.state AND s0.base_state IS NULL
    AND IFNULL(s0.creators3, 0) >= 5 AND IFNULL(s0.posts3, 0) >= 8 AND IFNULL(s0.top_creator_share3, 1) <= .4),
tt AS (                     -- their series tests that day and the 2 days before, in every market
  SELECT sd.item_id, sd.metric_date d, st.market, st.metric_date, st.platform, st.significant, st.ratio,
    st.obs_prior, st.y
  FROM sd JOIN {core}.v_series_test_current st
    ON st.item_id = sd.item_id AND st.metric_date BETWEEN DATE_SUB(sd.metric_date, INTERVAL 2 DAY) AND sd.metric_date
  WHERE st.metric_date BETWEEN {since2} AND DATE_ADD(@week_end, INTERVAL {recall_days} DAY)),
td AS (                     -- state.sql's agg (the market's series that day) and other (3 days, other markets)
  SELECT tt.item_id, tt.d,
    LOGICAL_OR(tt.market = @market AND tt.metric_date = tt.d AND tt.significant) sig_today,
    LOGICAL_OR(tt.market = @market AND tt.metric_date = tt.d AND tt.significant AND tt.ratio >= 2) sig_ratio_today,
    COUNT(DISTINCT IF(tt.market = @market AND tt.metric_date = tt.d AND tt.significant, tt.platform, NULL))
      sig_platforms_today,
    LOGICAL_OR(tt.significant AND tt.market NOT IN (@market, 'GLOBAL')) other_market,
    MAX(IF(tt.market = @market AND tt.metric_date = tt.d, tt.obs_prior + IF(tt.y IS NULL, 0, 1), NULL)) obs_days
  FROM tt GROUP BY tt.item_id, tt.d),
gl AS (                     -- other_platform_global: a GLOBAL series significant that day on another platform
  SELECT g.item_id, g.d FROM tt g
  LEFT JOIN tt h ON h.item_id = g.item_id AND h.d = g.d AND h.market = @market AND h.metric_date = h.d
    AND h.significant AND h.platform = g.platform
  WHERE g.market = 'GLOBAL' AND g.metric_date = g.d AND g.significant AND h.item_id IS NULL
  GROUP BY g.item_id, g.d),
pw AS (                     -- untested rows: measured posts in the 28 days to that day, dated by first sighting
  SELECT sd.item_id, sd.metric_date d, po.post_id, MIN(po.observed_date) first_day
  FROM sd
  JOIN {core}.post_items pi ON pi.item_id = sd.item_id
  JOIN {core}.post_observations po ON po.post_id = pi.post_id AND po.market = @market
   AND po.observed_date BETWEEN DATE_SUB(sd.metric_date, INTERVAL 27 DAY) AND sd.metric_date
  WHERE sd.untested AND po.lane_class IN ('unbiased_rank', 'panel')
    AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
    AND po.observed_date BETWEEN {since27} AND DATE_ADD(@week_end, INTERVAL {recall_days} DAY)
  GROUP BY sd.item_id, sd.metric_date, po.post_id),
pp AS (                     -- posts3_prev over every post: at or above what state.sql saw, never below
  SELECT pw.item_id, pw.d,
    COUNTIF(pw.first_day BETWEEN DATE_SUB(pw.d, INTERVAL 5 DAY) AND DATE_SUB(pw.d, INTERVAL 3 DAY)) posts3_prev
  FROM pw GROUP BY pw.item_id, pw.d),
sg AS (
  SELECT s0.item_id, s0.metric_date, s0.state, s0.found_platforms
  FROM s0
  LEFT JOIN sb ON sb.item_id = s0.item_id AND sb.metric_date = s0.metric_date
  LEFT JOIN sd ON sd.item_id = s0.item_id AND sd.metric_date = s0.metric_date
  LEFT JOIN td ON td.item_id = s0.item_id AND td.d = s0.metric_date
  LEFT JOIN gl ON gl.item_id = s0.item_id AND gl.d = s0.metric_date
  LEFT JOIN pp ON pp.item_id = s0.item_id AND pp.d = s0.metric_date
  WHERE s0.state IN ('emerging', 'rising', 'peaking', 'mainstream') OR sb.item_id IS NOT NULL
    OR (sd.item_id IS NOT NULL AND (
      (NOT s0.untested AND IFNULL(td.sig_ratio_today, FALSE)                              -- Rising
        AND (IFNULL(s0.sig_days3, 0) >= 2 OR IFNULL(td.sig_platforms_today, 0) >= 2
          OR gl.item_id IS NOT NULL OR IFNULL(td.other_market, FALSE)))
      OR (s0.novelty IN ('new', 'recurrence')                                            -- Emerging
        AND IF(s0.untested, IFNULL(td.obs_days, 0) >= 5 AND IFNULL(s0.posts3, 0) >= IFNULL(pp.posts3_prev, 0),
               IFNULL(td.sig_today, FALSE))))))
;

-- name: time_to_detect
-- Items whose first Mainstream state in the market falls in the week, reading Mainstream, flags and
-- sightings over the whole history from data_start. flagged_on: their first Emerging or Rising day on or
-- before that day (NULL if never), from _trend_rows: an eligible Emerging or Rising
-- row, or a Seasonal or Recurring row that qualifies as Rising or Emerging. first_seen: their first unbiased
-- sighting in the market on or before flagged_on (mainstream_on if never flagged), from unbiased_rank and
-- panel posts (item_daily) or live
-- unbiased_rank and unbiased_counter reads (item_counter_daily). The X trends archive is a seed only and
-- counter_post_views re-reads are 42's own choice of posts, so neither counts; vendor_history days were not
-- seen by 42 on that day. days is NULL when the item was never flagged or had no sighting before the flag;
-- scorecard.py ranks those items above every measured wait rather than leaving them out.
WITH {_trend_rows|all},
st AS (
  SELECT s.item_id, s.metric_date, s.state FROM {core}.v_item_state_current s
  WHERE s.market = @market AND s.metric_date BETWEEN {data_start} AND @week_end),
ms AS (
  SELECT st.item_id, MIN(st.metric_date) mainstream_on FROM st WHERE st.state = 'mainstream'
  GROUP BY st.item_id HAVING MIN(st.metric_date) >= @week_start),
fl AS (
  SELECT ms.item_id, ms.mainstream_on, MIN(sg.metric_date) flagged_on
  FROM ms LEFT JOIN sg ON sg.item_id = ms.item_id AND sg.state IN ('emerging', 'rising', 'seasonal', 'recurring')
    AND sg.metric_date <= ms.mainstream_on
  GROUP BY ms.item_id, ms.mainstream_on),
seen AS (
  SELECT i.item_id, i.metric_date seen_on FROM {core}.v_item_daily_current i
  WHERE i.market = @market AND i.lane_class IN ('unbiased_rank', 'panel') AND i.posts > 0
    AND i.metric_date BETWEEN {data_start} AND @week_end
  UNION ALL
  SELECT c.item_id, c.obs_date FROM {core}.v_item_counter_daily_current c
  WHERE c.market = @market AND c.lane_class IN ('unbiased_rank', 'unbiased_counter')
    AND c.series NOT IN ('x_trends', 'counter_post_views') AND IFNULL(c.source, 'live') != 'vendor_history'
    AND c.value > 0 AND c.obs_date BETWEEN {data_start} AND @week_end)
SELECT fl.item_id, fl.mainstream_on, fl.flagged_on, MIN(seen.seen_on) first_seen,
  DATE_DIFF(fl.flagged_on, MIN(seen.seen_on), DAY) days
FROM fl LEFT JOIN seen ON seen.item_id = fl.item_id AND seen.seen_on <= IFNULL(fl.flagged_on, fl.mainstream_on)
GROUP BY fl.item_id, fl.mainstream_on, fl.flagged_on
ORDER BY fl.item_id;

-- name: lead_time
-- Reference entries for the market dated in the week. An entry matches an item when one of its terms, as
-- written or with its spaces removed (hashtag keys have none), is inside the item's label, canonical key or an
-- alias (current cultural_map rows). A term under min_term characters with its spaces removed never matches.
-- flagged_on: the first day any matched item was a trend signal (_trend_rows) in the market, from lead_window
-- days before the entry's date to the week's end. lead_days is positive when 42 flagged the item before the list had
-- it; NULL when 42 never flagged it.
WITH {_trend_rows|lead},
ref AS (
  SELECT r.ref_id, r.market, r.event_date, r.term FROM UNNEST({reference}) r
  WHERE r.market = @market AND r.event_date BETWEEN @week_start AND @week_end),
names AS (
  SELECT m.item_id, LOWER(m.label) name FROM {core}.cultural_map m WHERE m.valid_to IS NULL AND m.label IS NOT NULL
  UNION ALL
  SELECT m.item_id, LOWER(m.canonical_key) FROM {core}.cultural_map m
  WHERE m.valid_to IS NULL AND m.canonical_key IS NOT NULL
  UNION ALL
  SELECT m.item_id, LOWER(a) FROM {core}.cultural_map m, UNNEST(m.aliases) a WHERE m.valid_to IS NULL),
hit AS (
  SELECT DISTINCT ref.ref_id, ref.event_date, names.item_id
  FROM ref JOIN names
    ON LENGTH(REPLACE(ref.term, ' ', '')) >= {min_term}
   AND (STRPOS(names.name, ref.term) > 0 OR STRPOS(names.name, REPLACE(ref.term, ' ', '')) > 0)),
fl AS (
  SELECT hit.ref_id, MIN(sg.metric_date) flagged_on
  FROM hit JOIN sg
    ON sg.item_id = hit.item_id
   AND sg.metric_date BETWEEN DATE_SUB(hit.event_date, INTERVAL {lead_window} DAY) AND @week_end
  GROUP BY hit.ref_id),
e AS (SELECT DISTINCT ref.ref_id, ref.event_date FROM ref)
SELECT e.ref_id, e.event_date, fl.flagged_on, DATE_DIFF(e.event_date, fl.flagged_on, DAY) lead_days
FROM e LEFT JOIN fl ON fl.ref_id = e.ref_id
ORDER BY e.ref_id;

-- name: precision
-- Top-3 cards of the week's random review (TRUST.md section 6), written to feedback by core/eval/review.py
-- with source 42_review_v1. One-tap feedback carries no such source and never counts. A card is real only when
-- every reviewer who checked it said real (review.py's _real). The CASE keeps JSON_VALUE off free-text rows.
WITH rv AS (
  SELECT CASE WHEN REGEXP_CONTAINS(f.what, r'"source": *"42_review_v1"') THEN f.what END j
  FROM {agent}.feedback f),
cards AS (
  SELECT JSON_VALUE(rv.j, '$.id') card_id, JSON_VALUE(rv.j, '$.verdict') verdict
  FROM rv
  WHERE JSON_VALUE(rv.j, '$.source') = '42_review_v1' AND JSON_VALUE(rv.j, '$.kind') = 'card'
    AND JSON_VALUE(rv.j, '$.stratum') = 'top3' AND JSON_VALUE(rv.j, '$.market') = @market
    AND JSON_VALUE(rv.j, '$.week') = @iso_week)
SELECT cards.card_id, LOGICAL_AND(cards.verdict = 'real') is_real
FROM cards GROUP BY cards.card_id
ORDER BY cards.card_id;

-- name: recall
-- Calendar moments for the market in the week. matched: the calendar matched at least one item to the moment.
-- surfaced: one of those items was a trend signal (_trend_rows) in the market on the moment's day or the day
-- after (within 24 hours, ENGINE.md section 6). A moment with no matched item is a miss. The large GDELT events
-- of ENGINE.md section 6 are not included yet (task 2.5).
WITH {_trend_rows|week},
mo AS (
  SELECT c.moment_date, c.name, LOGICAL_OR(IFNULL(ARRAY_LENGTH(c.item_ids), 0) > 0) matched
  FROM {core}.calendar c
  WHERE c.market = @market AND c.moment_date BETWEEN @week_start AND @week_end
  GROUP BY c.moment_date, c.name),
ci AS (
  SELECT c.moment_date, c.name, it item_id
  FROM {core}.calendar c, UNNEST(c.item_ids) it
  WHERE c.market = @market AND c.moment_date BETWEEN @week_start AND @week_end),
hits AS (
  SELECT ci.moment_date, ci.name, COUNT(DISTINCT sg.item_id) items
  FROM ci JOIN sg
    ON sg.item_id = ci.item_id
   AND sg.metric_date BETWEEN ci.moment_date AND DATE_ADD(ci.moment_date, INTERVAL {recall_days} DAY)
  GROUP BY ci.moment_date, ci.name)
SELECT mo.moment_date, mo.name, mo.matched, IFNULL(hits.items, 0) > 0 surfaced
FROM mo LEFT JOIN hits ON hits.moment_date = mo.moment_date AND hits.name = mo.name
ORDER BY mo.moment_date, mo.name;

-- name: breadth_platforms
-- Trends of the week: items that were a trend signal (_trend_rows) on a day of the week. multi: found on
-- 2 or more platforms on one of those days. found_platforms is set only above the placebo base rate
-- (DATA.md 3.7), so placebo_items (at the week's end) says whether it could be.
WITH {_trend_rows|week},
tr AS (
  SELECT sg.item_id, MAX(IFNULL(sg.found_platforms, 0)) >= 2 multi
  FROM sg WHERE sg.metric_date BETWEEN @week_start AND @week_end
  GROUP BY sg.item_id),
pb AS (
  SELECT MAX(p.placebo_items) placebo_items FROM {core}.tvf_placebo_base(@week_end) p WHERE p.market = @market)
SELECT tr.item_id, tr.multi, pb.placebo_items
FROM tr CROSS JOIN pb
ORDER BY tr.item_id;

-- name: expansion_share
-- Credits charged on the expansion lane in the market in the week, by platform and by cluster. A cluster is an
-- item with its variant children (cultural_map parent_item_id); credits spent on no item have no cluster and
-- are left out of the cluster rows, so the cluster share's denominator is only credits spent on an item.
WITH x AS (
  SELECT cl.platform, cl.item_id, IFNULL(cl.credits_charged, 0) credits
  FROM {core}.credit_ledger cl
  WHERE cl.market = @market AND cl.lane = 'expansion' AND cl.trend_date BETWEEN @week_start AND @week_end),
fam AS (
  SELECT m.item_id, IFNULL(m.parent_item_id, m.item_id) family
  FROM {core}.cultural_map m WHERE m.valid_to IS NULL)
SELECT 'platform' dimension, x.platform bucket, SUM(x.credits) credits
FROM x GROUP BY x.platform
UNION ALL
SELECT 'cluster', IFNULL(fam.family, x.item_id), SUM(x.credits)
FROM x LEFT JOIN fam ON fam.item_id = x.item_id
WHERE x.item_id IS NOT NULL
GROUP BY IFNULL(fam.family, x.item_id)
ORDER BY dimension, bucket;

-- name: cost_per_confirmed
-- Engine credits (the collect, confirm and reserve shares, never Ask, eval or build) charged to the market in
-- the week, over its confirmed trends: distinct items on Today (cards and more) in the week's current briefs on
-- days cross-platform confirmation ran for the market (no thin_coverage banner).
WITH cr AS (
  SELECT SUM(IFNULL(cl.credits_charged, 0)) credits
  FROM {core}.credit_ledger cl
  WHERE cl.market = @market AND cl.job IN ('collect', 'confirm', 'reserve')
    AND cl.trend_date BETWEEN @week_start AND @week_end),
b AS (
  SELECT bc.brief_date, bc.payload FROM {agent}.v_briefs_current bc
  WHERE bc.market = @market AND bc.brief_date BETWEEN @week_start AND @week_end),
thin AS (
  SELECT DISTINCT b.brief_date FROM b, UNNEST(JSON_QUERY_ARRAY(b.payload, '$.banners')) bn
  WHERE JSON_VALUE(bn, '$.kind') = 'thin_coverage'),
shown AS (
  SELECT b.brief_date, JSON_VALUE(c, '$.item_id') item_id FROM b, UNNEST(JSON_QUERY_ARRAY(b.payload, '$.cards')) c
  UNION ALL
  SELECT b.brief_date, JSON_VALUE(c, '$.item_id') FROM b, UNNEST(JSON_QUERY_ARRAY(b.payload, '$.more')) c),
conf AS (
  SELECT COUNT(DISTINCT shown.item_id) trends
  FROM shown WHERE shown.brief_date NOT IN (SELECT thin.brief_date FROM thin))
SELECT cr.credits, conf.trends FROM cr CROSS JOIN conf;
