-- v_item_spread, BUILD.md task 2.3, in BigQuery Standard SQL: one row per item, market and metric_date for the
-- latest good detect day and the 6 days before it, for each item with a measured post first seen in the market
-- in the 7 days ending that day. {core} and {agent} are the dataset names; core/detect/job.py applies this after
-- sql/views.sql through sqlrun.apply_spread, as a step whose failure never stops detect.
--
-- Spread reads measured lanes only, unbiased_rank and panel (tvf_item_window's measured set), with placebo and
-- agent_live also left out as a guard. DATA.md section 3.2 says search_presence and watchlist sightings are never
-- spread: their dates record when 42 searched, not when the item reached a platform. "First seen" here means the
-- first sighting in a measured lane.
-- tiers: distinct measured posts per creator tier (creator_tier_at_post), each dated by its first measured
-- sighting in the market and counted when that day falls in the 7 days ending metric_date (metric_date and the
-- 6 days before), the window large_posts7 and diffusion use in tvf_item_window. DATA.md does not fix this
-- window; 7 days is an inference.
-- platform_first_seen: each platform's first measured sighting of the item in the market on or before
-- metric_date.
-- spread_line: the card sentence of core/api/contract.md section 10.1, "First seen on TikTok 12 September,
-- YouTube 18 September", one name per platform word at its earliest day; the year is added when it is not
-- metric_date's year. NULL when no platform qualifies.

CREATE OR REPLACE VIEW {core}.v_item_spread AS
WITH ld AS (
  SELECT MAX(g.run_date) detect_day FROM {core}.v_good_runs g WHERE g.stage = 'detect'),
dd AS (
  SELECT metric_date
  FROM ld, UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(ld.detect_day, INTERVAL 6 DAY), ld.detect_day)) metric_date),
mp AS (                     -- each measured post once per item and market, dated by its first measured sighting
  SELECT pi.item_id, po.market, po.post_id, ANY_VALUE(ps.platform) platform,
    ANY_VALUE(ps.creator_tier_at_post) tier, MIN(po.observed_date) first_day
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  JOIN {core}.posts ps ON ps.post_id = po.post_id
  WHERE po.lane_class IN ('unbiased_rank', 'panel') AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY pi.item_id, po.market, po.post_id),
tw AS (                     -- measured posts per tier, first seen in the 7 days ending metric_date
  SELECT mp.item_id, mp.market, dd.metric_date,
    STRUCT(COUNT(DISTINCT IF(mp.tier = 'nano', mp.post_id, NULL)) AS nano,
           COUNT(DISTINCT IF(mp.tier = 'micro', mp.post_id, NULL)) AS micro,
           COUNT(DISTINCT IF(mp.tier = 'mid', mp.post_id, NULL)) AS mid,
           COUNT(DISTINCT IF(mp.tier = 'macro', mp.post_id, NULL)) AS macro,
           COUNT(DISTINCT IF(mp.tier = 'mega', mp.post_id, NULL)) AS mega) tiers
  FROM dd JOIN mp ON mp.first_day BETWEEN DATE_SUB(dd.metric_date, INTERVAL 6 DAY) AND dd.metric_date
  GROUP BY mp.item_id, mp.market, dd.metric_date),
fs AS (                     -- each platform's first measured sighting of the item in the market
  SELECT mp.item_id, mp.market, mp.platform, MIN(mp.first_day) first_seen
  FROM mp WHERE mp.platform IS NOT NULL
  GROUP BY mp.item_id, mp.market, mp.platform),
tf AS (
  SELECT tw.item_id, tw.market, tw.metric_date, fs.platform, fs.first_seen,
    CASE fs.platform WHEN 'tiktok' THEN 'TikTok' WHEN 'youtube' THEN 'YouTube' WHEN 'instagram' THEN 'Instagram'
      WHEN 'twitter' THEN 'X' WHEN 'x' THEN 'X' WHEN 'facebook' THEN 'Facebook' WHEN 'reddit' THEN 'Reddit'
      WHEN 'threads' THEN 'Threads' WHEN 'linkedin' THEN 'LinkedIn' WHEN 'telegram' THEN 'Telegram'
      WHEN 'apple_music' THEN 'Apple Music' ELSE REPLACE(fs.platform, '_', ' ') END word
  FROM tw JOIN fs ON fs.item_id = tw.item_id AND fs.market = tw.market AND fs.first_seen <= tw.metric_date),
pf AS (
  SELECT tf.item_id, tf.market, tf.metric_date,
    ARRAY_AGG(STRUCT(tf.platform AS platform, tf.first_seen AS first_seen) ORDER BY tf.first_seen, tf.platform)
      platform_first_seen
  FROM tf GROUP BY tf.item_id, tf.market, tf.metric_date),
wd AS (                     -- one name per platform word, at its earliest day
  SELECT tf.item_id, tf.market, tf.metric_date, tf.word, MIN(tf.first_seen) first_seen
  FROM tf GROUP BY tf.item_id, tf.market, tf.metric_date, tf.word),
sl AS (
  SELECT wd.item_id, wd.market, wd.metric_date,
    CONCAT('First seen on ', STRING_AGG(CONCAT(wd.word, ' ', CAST(EXTRACT(DAY FROM wd.first_seen) AS STRING), ' ',
      FORMAT_DATE('%B', wd.first_seen),
      IF(EXTRACT(YEAR FROM wd.first_seen) = EXTRACT(YEAR FROM wd.metric_date), '',
         CONCAT(' ', CAST(EXTRACT(YEAR FROM wd.first_seen) AS STRING)))), ', ' ORDER BY wd.first_seen, wd.word))
      spread_line
  FROM wd GROUP BY wd.item_id, wd.market, wd.metric_date)
SELECT tw.item_id, tw.market, tw.metric_date, tw.tiers,
  IFNULL(pf.platform_first_seen, []) platform_first_seen, sl.spread_line
FROM tw
LEFT JOIN pf ON pf.item_id = tw.item_id AND pf.market = tw.market AND pf.metric_date = tw.metric_date
LEFT JOIN sl ON sl.item_id = tw.item_id AND sl.market = tw.market AND sl.metric_date = tw.metric_date;
