-- Posts sighted on one day, for model enrichment (BUILD.md 2.1). Parameter: @run_date, DATE.
-- Same sighting rule as embed.sql: a post is in the day when any post_observations row for it has
-- observed_date = @run_date, and its post_date is at most 400 days before that day.
-- post_enrichment is append-only and holds embedding rows too, so a post counts as enriched only when one of
-- its rows has tone set. Every enrichment row has a tone, and no embedding row does.
-- content is the post text plus its transcript, cut to 4000 characters, and it is left empty for a post
-- enriched already, since that post is not sent again. run_enrich skips enriched posts and posts with no
-- content, and sends the rest in this order, so a day past its limit leaves the least useful posts unsent.
-- Posts linked (post_items) to an item that detect marked eligible come first: eligible in a market the post
-- was sighted in on @run_date, in that market's newest good detect run (v_item_state_current, the view the
-- brief's candidates read) dated within the 7 days up to @run_date, so the posts the brief's candidates rest on
-- are enriched before the rest. Within each part, lowest post_id first.

WITH latest_state AS (
  SELECT s.market, s.item_id, s.eligible
  FROM `ogilvy-trends-v2.intelligence_42_core.v_item_state_current` AS s
  WHERE s.metric_date BETWEEN DATE_SUB(@run_date, INTERVAL 7 DAY) AND @run_date
  QUALIFY s.metric_date = MAX(s.metric_date) OVER (PARTITION BY s.market)
),
eligible_posts AS (
  SELECT DISTINCT o.post_id
  FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
  JOIN `ogilvy-trends-v2.intelligence_42_core.post_items` AS pi
    ON pi.post_id = o.post_id
  JOIN latest_state AS ls
    ON ls.item_id = pi.item_id AND ls.market = o.market
  WHERE o.observed_date = @run_date AND ls.eligible IS TRUE
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
  done.post_id IS NOT NULL AS enriched
FROM `ogilvy-trends-v2.intelligence_42_core.posts` AS p
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
WHERE p.post_date >= DATE_SUB(@run_date, INTERVAL 400 DAY)
  AND EXISTS (
    SELECT 1
    FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
    WHERE o.post_id = p.post_id AND o.observed_date = @run_date
  )
ORDER BY ep.post_id IS NULL, p.post_id
