-- The rows the 200-post enrichment hand check samples from (BUILD.md 2.1). Parameter: @day, DATE.
-- Every post sighted on @day (a post_observations row with observed_date = @day and a post_date at most 400 days
-- before it, the rule enrich_select.sql uses) that has an enrichment row, joined to one enrichment row per post. Only rows with tone set are
-- enrichment rows; embedding rows have none. post_enrichment records no write time, so "latest" cannot be read
-- from it. enrich_insert.sql writes a post once, and when a race leaves two copies the tie is broken on fixed
-- columns so the same row is kept every time. enrich.enrich_check_sample_from stratifies these rows by market and
-- platform.

SELECT
  p.post_id,
  p.geo_market AS market,
  p.platform,
  p.url,
  p.text,
  e.langs,
  e.code_switched,
  e.entities
FROM `ogilvy-trends-v2.intelligence_42_core.posts` AS p
JOIN (
  SELECT e.post_id, e.langs, e.code_switched, e.entities
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  WHERE e.tone IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.post_id
    ORDER BY e.tone, e.stance, ARRAY_TO_STRING(e.langs, '|'), ARRAY_TO_STRING(e.entities, '|')
  ) = 1
) AS e
  ON e.post_id = p.post_id
WHERE p.post_date >= DATE_SUB(@day, INTERVAL 400 DAY)
  AND EXISTS (
    SELECT 1
    FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
    WHERE o.post_id = p.post_id AND o.observed_date = @day
  )
ORDER BY p.post_id
