-- The quote pool for the weekly native-speaker tone check (FEATURES 27, TRUST.md section 6), read only.
-- Parameters: @since DATE, @as_of DATE, @languages ARRAY<STRING>, the codes of the week's rotation slot
-- (core/eval/review.rotation). One row per tone-labelled post dated @since to @as_of and per slot language in
-- its langs, so a post in isiZulu and isiXhosa can go to each speaker; id is post_id:language, which keeps the
-- two apart in the feedback table. core/eval/review.native_sample draws the week's quotes from these rows and
-- native_sheet asks the speaker whether each tone label is right. Enrichment writes no gloss, so gloss is empty
-- and the speaker marks tone alone. Only rows with tone set are enrichment rows, and a post written twice keeps
-- one row on the tie-break enrich_check_rows.sql uses. A post held twice in posts keeps one row too, the one
-- with the fuller text, so an id is never listed twice, and a language listed twice in langs counts once.
WITH enriched AS (
  SELECT e.post_id, e.langs, e.tone
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  WHERE e.tone IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.post_id
    ORDER BY e.tone, e.stance, ARRAY_TO_STRING(e.langs, '|'), ARRAY_TO_STRING(e.entities, '|')
  ) = 1
),
window_posts AS (
  SELECT p.post_id, p.geo_market, p.url, p.text
  FROM `ogilvy-trends-v2.intelligence_42_core.posts` AS p
  WHERE p.post_date BETWEEN @since AND @as_of
  QUALIFY ROW_NUMBER() OVER (PARTITION BY p.post_id ORDER BY p.post_date DESC, p.geo_market, p.url, LENGTH(p.text) DESC, p.text) = 1
)
SELECT DISTINCT
  CONCAT(p.post_id, ':', lang) AS id,
  p.post_id,
  lang AS language,
  p.geo_market AS market,
  p.url,
  p.text,
  en.tone,
  '' AS gloss
FROM window_posts AS p
JOIN enriched AS en
  ON en.post_id = p.post_id
CROSS JOIN UNNEST(en.langs) AS lang
WHERE lang IN UNNEST(@languages)
ORDER BY id
