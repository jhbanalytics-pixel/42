-- Read-only inputs to the backtest, TRUST.md section 4 and DATA.md section 3.5 (task 1.19), in BigQuery Standard SQL.
-- core/detect/backtest.py runs each statement with @as_of (the last replayed day) and @start (the first day read);
-- {core} and {agent} are the dataset names. Nothing here writes.
-- A row is available when its run's ok row landed, or at its own available_at when that is later; only rows
-- available on or before the UTC date @as_of are read. The replay then narrows each day t to the rows available
-- before the good stats run of t started, or before the end of t when there is none.

-- Good runs of the stages whose rows the replay reads, and the stats runs whose start sets each day's cutoff.
SELECT r.run_id, r.stage, r.run_date, r.started_at, r.finished_at
FROM {agent}.runs r
WHERE r.status = 'ok' AND r.stage IN ('collect', 'aggregate', 'stats', 'detect')
  AND r.run_date BETWEEN @start AND @as_of AND DATE(r.finished_at) <= @as_of;

-- Collection health from collect runs of the same day finished by @as_of.
SELECT h.day, h.market, h.platform, h.series, h.protocol, h.lane_class, h.valid, h.units_ok, h.k, h.items, h.run_id
FROM {core}.collection_health h
JOIN {agent}.runs r ON r.run_id = h.run_id AND r.stage = 'collect' AND r.status = 'ok' AND r.run_date = h.day
WHERE h.day BETWEEN @start AND @as_of AND DATE(r.finished_at) <= @as_of;

-- Rank appearances and counter deltas, the reads v_series_daily uses, each with the time it became available.
SELECT c.obs_date day, c.market, c.platform, c.item_id, c.series, c.protocol, c.lane_class, c.unit,
  c.value, c.source, GREATEST(c.available_at, r.finished_at) available_at
FROM {core}.item_counter_daily c
JOIN {agent}.runs r ON r.run_id = c.run_id AND r.stage = 'collect' AND r.status = 'ok'
WHERE c.obs_date BETWEEN @start AND @as_of
  AND ((c.lane_class = 'unbiased_rank' AND c.unit = 'appearances' AND c.series != 'x_trends')
    OR (c.lane_class = 'unbiased_counter' AND c.unit = 'delta' AND c.series != 'counter_post_views'))
  AND DATE(GREATEST(c.available_at, r.finished_at)) <= @as_of;

-- Every item a rank list or panel has watched, from any day up to @as_of, with the time it was first available.
SELECT c.item_id, c.market, c.platform, c.series, c.protocol, 'unbiased_rank' lane_class,
  MIN(GREATEST(c.available_at, r.finished_at)) available_at
FROM {core}.item_counter_daily c
JOIN {agent}.runs r ON r.run_id = c.run_id AND r.stage = 'collect' AND r.status = 'ok'
WHERE c.lane_class = 'unbiased_rank' AND c.unit = 'appearances' AND c.series != 'x_trends'
  AND c.obs_date <= @as_of AND DATE(GREATEST(c.available_at, r.finished_at)) <= @as_of
GROUP BY c.item_id, c.market, c.platform, c.series, c.protocol
UNION ALL
SELECT i.item_id, i.market, i.platform, i.series, i.protocol, 'panel' lane_class, MIN(r.finished_at) available_at
FROM {core}.item_daily i
JOIN {agent}.runs r ON r.run_id = i.run_id AND r.stage = 'aggregate' AND r.status = 'ok' AND r.run_date = i.metric_date
WHERE i.lane_class = 'panel' AND i.metric_date <= @as_of AND DATE(r.finished_at) <= @as_of
GROUP BY i.item_id, i.market, i.platform, i.series, i.protocol;

-- Panel posts per day from aggregate runs finished by @as_of. item_daily.available_at is stamped inside the run,
-- so the run's ok row is the later of the two and the one that makes the rows readable.
SELECT i.metric_date day, i.market, i.platform, i.item_id, i.series, i.protocol, i.posts, i.run_id
FROM {core}.item_daily i
JOIN {agent}.runs r ON r.run_id = i.run_id AND r.stage = 'aggregate' AND r.status = 'ok' AND r.run_date = i.metric_date
WHERE i.lane_class = 'panel' AND i.metric_date BETWEEN @start AND @as_of AND DATE(r.finished_at) <= @as_of;

-- Every cultural map version that began by @as_of; the replay keeps an item from the day its valid_from falls
-- before the cutoff. first_seen has no history: the aggregate and legacy steps rewrite it in place, so each
-- version carries the value as it stands today, which may be earlier than what was known on the replayed day.
SELECT cm.item_id, cm.kind, cm.first_seen, cm.valid_from, cm.valid_to
FROM {core}.cultural_map cm
WHERE DATE(cm.valid_from) <= @as_of;

-- Item states from detect runs finished by @as_of, for top-10 precision.
SELECT s.metric_date, s.market, s.item_id, s.state, s.eligible, s.worth_raw, s.run_id
FROM {core}.item_state s
JOIN {agent}.runs r ON r.run_id = s.run_id AND r.stage = 'detect' AND r.status = 'ok' AND r.run_date = s.metric_date
WHERE s.metric_date BETWEEN @start AND @as_of AND DATE(r.finished_at) <= @as_of;

-- test_switch rows already written: the keys in force on each replayed day join the family of the key measured,
-- and a key is switched on once per rule_version.
SELECT ts.market, ts.platform, ts.lane_class, ts.switched_on, ts.backtest_run_id, ts.rule_version
FROM {core}.test_switch ts;
