-- Posts sighted on one day, for model enrichment (BUILD.md 2.1). Parameter: @run_date, DATE.
-- Same sighting rule as embed.sql: a post is in the day when any post_observations row for it has
-- observed_date = @run_date, and its post_date is at most 400 days before that day.
-- A post sighted in the 7 days up to @run_date (the window the brief's evidence pack reads) that has no enrichment
-- row yet and has content is selected too, so a post missed on its sighting day (the time budget, the cap, a failed
-- output) is tried again on a later day instead of never (22 Oct 2026). Within each part of the order below, the
-- posts sighted on @run_date come first, so a retry never goes before a post of the same part sighted today.
-- post_enrichment is append-only and holds embedding rows too, so a post counts as enriched only when one of
-- its rows has tone set. Every enrichment row has a tone, and no embedding row does.
-- content is the post text plus its transcript, cut to 4000 characters, and it is left empty for a post
-- enriched already, since that post is not sent again. run_enrich skips enriched posts and posts with no
-- content, and sends the rest in this order, so a day past its limit leaves the least useful posts unsent.
-- Posts linked (post_items) to an item that detect marked eligible come first: eligible in a market the post
-- was sighted in on @run_date, in that market's newest good detect run (v_item_state_current, the view the
-- brief's candidates read) dated within the 7 days up to @run_date, so the posts the brief's candidates rest on
-- are enriched before the rest. Then come the posts that are members of a cluster dated within the 7 days up to
-- @run_date (clusters, cluster_members): the day's own clusters are written after this step, so these are the
-- newest clusters there are, and a post sighted again that one of them holds is a post the brief's topics rest
-- on (18 Oct 2026). The order changes which posts the budget reaches, not the budget.
-- Within each part, the posts sighted on @run_date first, then lowest post_id first.
-- A post by a creator on the suppression list (v_suppressed_creators) is never selected for enrichment; embed.sql
-- and video_clips.sql apply the same rule to their own reads. A post with no creator_id names nobody and stays.
-- This stops new model calls only: rows already written for a creator hidden later are not removed here.

WITH latest_state AS (
  SELECT s.market, s.item_id, s.eligible
  FROM `ogilvy-trends-v2.intelligence_42_core.v_item_state_current` AS s
  WHERE s.metric_date BETWEEN DATE_SUB(@run_date, INTERVAL 7 DAY) AND @run_date
  QUALIFY s.metric_date = MAX(s.metric_date) OVER (PARTITION BY s.market)
),
sightings AS (
  SELECT o.post_id, LOGICAL_OR(o.observed_date = @run_date) AS today
  FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
  WHERE o.observed_date BETWEEN DATE_SUB(@run_date, INTERVAL 6 DAY) AND @run_date
  GROUP BY o.post_id
),
eligible_posts AS (
  SELECT DISTINCT o.post_id
  FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
  JOIN `ogilvy-trends-v2.intelligence_42_core.post_items` AS pi
    ON pi.post_id = o.post_id
  JOIN latest_state AS ls
    ON ls.item_id = pi.item_id AND ls.market = o.market
  WHERE o.observed_date BETWEEN DATE_SUB(@run_date, INTERVAL 6 DAY) AND @run_date AND ls.eligible IS TRUE
),
clustered_posts AS (
  SELECT DISTINCT cm.post_id
  FROM `ogilvy-trends-v2.intelligence_42_core.clusters` AS k
  JOIN `ogilvy-trends-v2.intelligence_42_core.cluster_members` AS cm
    ON cm.cluster_id = k.cluster_id
  WHERE k.cluster_date BETWEEN DATE_SUB(@run_date, INTERVAL 7 DAY) AND @run_date
)
SELECT
  p.post_id,
  p.platform,
  p.geo_market AS market,
  c.handle,
  p.hashtags,
  p.sound_id,
  p.duration_s,
  IF(done.post_id IS NOT NULL, '', LEFT(TRIM(CONCAT(
    COALESCE(p.text, ''),
    IF(TRIM(COALESCE(p.transcript, '')) = '', '', CONCAT('\n', p.transcript))
  )), 4000)) AS content,
  done.post_id IS NOT NULL AS enriched,
  sg.today
FROM `ogilvy-trends-v2.intelligence_42_core.posts` AS p
JOIN sightings AS sg
  ON sg.post_id = p.post_id
LEFT JOIN `ogilvy-trends-v2.intelligence_42_core.creators` AS c
  ON c.creator_id = p.creator_id
LEFT JOIN (
  SELECT DISTINCT e.post_id
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  WHERE e.tone IS NOT NULL
) AS done
  ON done.post_id = p.post_id
LEFT JOIN eligible_posts AS ep
  ON ep.post_id = p.post_id
LEFT JOIN clustered_posts AS cp
  ON cp.post_id = p.post_id
WHERE p.post_date >= DATE_SUB(@run_date, INTERVAL 400 DAY)
  AND (sg.today
       OR (done.post_id IS NULL
           AND TRIM(CONCAT(COALESCE(p.text, ''), COALESCE(p.transcript, ''))) != ''))
  AND NOT EXISTS (
    SELECT 1
    FROM `ogilvy-trends-v2.intelligence_42_core.v_suppressed_creators` AS v
    WHERE v.creator_id = p.creator_id
  )
ORDER BY ep.post_id IS NULL, cp.post_id IS NULL, NOT sg.today, p.post_id
