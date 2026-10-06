-- The hourly Breaking rule (ENGINE.md section 3, "Later"), in BigQuery Standard SQL; core/detect/breaking.py runs it.
-- @hour is the start of the hour judged (a SAST hour start); the window is [@hour minus 5 hours, @hour plus 1 hour).
-- @day is @hour's SAST date, and the baseline is the 28 days before it.
-- The unit is item_daily's (DATA.md 3.4): each post once per market, item, lane class and panel series, at its
-- first sighting there, with the item from post_items. posts6 counts the posts whose first sighting falls in the
-- window, and creators6 their distinct creators. Measured lanes only (DATA.md 3.2): unbiased_rank and panel; a
-- placebo or agent_live sighting never counts, and GLOBAL is left out.
-- A component is one market, item, platform and lane class with new posts in the window, and its series are the
-- series those posts were first sighted through. Its baseline is the item's mean daily posts on that platform and
-- lane over the days every one of those series ran and was valid (DATA.md 3.2 to 3.4: a zero only on a valid day,
-- and no day before a series ran); with fewer than @min_days such days it has none. Health rows are matched by
-- series, so a panel route with no platform (prism/profiles) covers every platform its posts are on.
-- expected6 adds up the components' baselines times @share, and is NULL when any component has no baseline.
-- One row per market and item. breaking is TRUE when posts6 is at least @ratio times expected6, creators6 is at
-- least @min_creators, and the item was seen on an unbiased_rank list or on at least @min_platforms platforms.
WITH measured AS (        -- measured sightings up to the window's end
  SELECT po.post_id, po.market, po.lane_class, IF(po.lane_class = 'panel', po.series, NULL) pseries,
    po.series, po.observed_at
  FROM {core}.post_observations po
  WHERE po.observed_date <= DATE_ADD(@day, INTERVAL 1 DAY)
    AND po.observed_at < TIMESTAMP_ADD(@hour, INTERVAL 1 HOUR)
    AND po.market IN ('ZA', 'NG', 'KE') AND po.lane_class IN ('unbiased_rank', 'panel')
    AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')),
win AS (                  -- sightings inside the window
  SELECT m.* FROM measured m
  WHERE m.observed_at >= TIMESTAMP_SUB(@hour, INTERVAL 5 HOUR)),
firsts AS (               -- first sighting of each post seen in the window, per market, lane class and panel series
  SELECT m.post_id, m.market, m.lane_class, m.pseries, MIN(m.observed_at) first_at
  FROM measured m
  WHERE m.post_id IN (SELECT w.post_id FROM win w)
  GROUP BY m.post_id, m.market, m.lane_class, m.pseries),
fresh AS (                -- posts new in the window, with their item, platform and creator
  SELECT f.post_id, f.market, f.lane_class, f.pseries, f.first_at, pi.item_id, ps.platform, ps.creator_id
  FROM firsts f
  JOIN {core}.posts ps ON ps.post_id = f.post_id
  JOIN (SELECT DISTINCT pit.post_id, pit.item_id FROM {core}.post_items pit) pi ON pi.post_id = f.post_id
  WHERE f.first_at >= TIMESTAMP_SUB(@hour, INTERVAL 5 HOUR)),
comp AS (
  SELECT fr.market, fr.item_id, fr.platform, fr.lane_class, COUNT(*) posts
  FROM fresh fr GROUP BY fr.market, fr.item_id, fr.platform, fr.lane_class),
comp_series AS (          -- the series each component's new posts were first sighted through
  SELECT DISTINCT fr.market, fr.item_id, fr.platform, fr.lane_class, w.series
  FROM fresh fr
  JOIN win w ON w.post_id = fr.post_id AND w.market = fr.market AND w.lane_class = fr.lane_class
    AND w.observed_at = fr.first_at AND IFNULL(w.pseries, '') = IFNULL(fr.pseries, '')),
series_days AS (          -- a series' valid days: it ran, and every health row of it that day was valid
  SELECT hc.market, hc.series, hc.day
  FROM {core}.v_collection_health_current hc
  WHERE hc.market IN ('ZA', 'NG', 'KE')
    AND hc.day BETWEEN DATE_SUB(@day, INTERVAL 28 DAY) AND DATE_SUB(@day, INTERVAL 1 DAY)
  GROUP BY hc.market, hc.series, hc.day
  HAVING LOGICAL_AND(IFNULL(hc.valid, FALSE))),
comp_days AS (            -- days on which every series of the component was valid
  SELECT cs.market, cs.item_id, cs.platform, cs.lane_class, sd.day
  FROM (SELECT c.*, COUNT(*) OVER (PARTITION BY c.market, c.item_id, c.platform, c.lane_class) n_series
        FROM comp_series c) cs
  JOIN series_days sd ON sd.market = cs.market AND sd.series = cs.series
  GROUP BY cs.market, cs.item_id, cs.platform, cs.lane_class, sd.day
  HAVING COUNT(*) = MAX(cs.n_series)),
days AS (
  SELECT cd.market, cd.item_id, cd.platform, cd.lane_class, COUNT(*) baseline_days
  FROM comp_days cd GROUP BY cd.market, cd.item_id, cd.platform, cd.lane_class),
base AS (                 -- panel rows of item_daily carry their series; only the component's series count
  SELECT cd.market, cd.item_id, cd.platform, cd.lane_class, SUM(i.posts) posts
  FROM comp_days cd
  JOIN {core}.v_item_daily_current i ON i.market = cd.market AND i.item_id = cd.item_id
    AND i.platform = cd.platform AND i.lane_class = cd.lane_class AND i.metric_date = cd.day
  WHERE i.metric_date BETWEEN DATE_SUB(@day, INTERVAL 28 DAY) AND DATE_SUB(@day, INTERVAL 1 DAY)
    AND (i.lane_class != 'panel' OR EXISTS (
      SELECT 1 FROM comp_series cs WHERE cs.market = cd.market AND cs.item_id = cd.item_id
        AND cs.platform = cd.platform AND cs.lane_class = 'panel' AND cs.series = i.series))
  GROUP BY cd.market, cd.item_id, cd.platform, cd.lane_class),
comp_base AS (
  SELECT c.market, c.item_id, c.posts, IFNULL(d.baseline_days, 0) baseline_days,
    IF(IFNULL(d.baseline_days, 0) >= @min_days, IFNULL(b.posts, 0) / d.baseline_days * @share, NULL) expected
  FROM comp c
  LEFT JOIN days d ON d.market = c.market AND d.item_id = c.item_id AND d.platform = c.platform
    AND d.lane_class = c.lane_class
  LEFT JOIN base b ON b.market = c.market AND b.item_id = c.item_id AND b.platform = c.platform
    AND b.lane_class = c.lane_class),
x AS (
  SELECT cb.market, cb.item_id, SUM(cb.posts) posts6, MIN(cb.baseline_days) baseline_days,
    IF(COUNTIF(cb.expected IS NULL) = 0, SUM(cb.expected), NULL) expected6
  FROM comp_base cb GROUP BY cb.market, cb.item_id),
who AS (
  SELECT fr.market, fr.item_id, COUNT(DISTINCT fr.creator_id) creators6, COUNT(DISTINCT fr.platform) platforms,
    LOGICAL_OR(fr.lane_class = 'unbiased_rank') unbiased
  FROM fresh fr GROUP BY fr.market, fr.item_id)
SELECT x.market, x.item_id, x.posts6, w.creators6, x.expected6, SAFE_DIVIDE(x.posts6, x.expected6) ratio,
  w.platforms, w.unbiased, x.baseline_days,
  IFNULL(x.posts6 >= @ratio * x.expected6 AND w.creators6 >= @min_creators
         AND (w.unbiased OR w.platforms >= @min_platforms), FALSE) breaking
FROM x
JOIN who w ON w.market = x.market AND w.item_id = x.item_id
ORDER BY x.market, x.item_id
