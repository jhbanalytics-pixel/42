-- The clips video reading may pick from (BUILD.md 2.6, video.py): every member of a cluster written for @run_date
-- (any market, the pooled pan run too) that is a TikTok, YouTube or Instagram post, with its engagement and velocity.
-- Parameter: @run_date DATE. video.pick_clips keeps the clips (an Instagram post only with a duration) and orders
-- them; this query only narrows the read.
-- velocity is engagement per hour from publication to the post's latest sighting on @run_date, at least one hour;
-- NULL when the post has no publication time. A post that already has a video_notes row is left out, so a rerun
-- reads nothing twice, and so is a post by a suppressed creator (v_suppressed_creators), which is never sent on, and
-- a post with no creator_id, whose creator cannot be checked against the list.
-- The post_date bound keeps the scan to Stage 1's 400 days, as cluster_posts.sql does.
WITH members AS (
  SELECT k.cluster_id, k.market, m.post_id
  FROM `ogilvy-trends-v2.intelligence_42_core.clusters` AS k
  JOIN `ogilvy-trends-v2.intelligence_42_core.cluster_members` AS m
    ON m.cluster_id = k.cluster_id
  WHERE k.cluster_date = @run_date
),
seen AS (
  SELECT o.post_id, MAX(o.observed_at) AS observed_at
  FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
  WHERE o.observed_date = @run_date
    AND o.post_id IN (SELECT mb.post_id FROM members AS mb)
  GROUP BY o.post_id
),
already_read AS (
  SELECT DISTINCT e.post_id
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  WHERE e.video_notes IS NOT NULL
)
SELECT
  mb.cluster_id,
  mb.market,
  p.post_id,
  LOWER(p.platform) AS platform,
  p.url,
  p.thumbnail_url,
  p.duration_s,
  p.text,
  p.transcript,
  p.hashtags,
  p.sound_id,
  COALESCE(p.engagement, 0) AS engagement,
  SAFE_DIVIDE(COALESCE(p.engagement, 0),
              GREATEST(1, TIMESTAMP_DIFF(COALESCE(s.observed_at, CURRENT_TIMESTAMP()), p.published_at, HOUR)))
    AS velocity
FROM members AS mb
JOIN `ogilvy-trends-v2.intelligence_42_core.posts` AS p
  ON p.post_id = mb.post_id AND p.post_date >= DATE_SUB(@run_date, INTERVAL 402 DAY)
LEFT JOIN seen AS s
  ON s.post_id = p.post_id
WHERE LOWER(p.platform) IN ('tiktok', 'youtube', 'instagram')
  AND p.post_id NOT IN (SELECT r.post_id FROM already_read AS r)
  AND p.creator_id IS NOT NULL
  AND p.creator_id NOT IN (
    SELECT v.creator_id FROM `ogilvy-trends-v2.intelligence_42_core.v_suppressed_creators` AS v
    WHERE v.creator_id IS NOT NULL)
ORDER BY mb.cluster_id, p.post_id
