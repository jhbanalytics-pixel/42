-- Tone by language per item (FEATURES 27), read only, for Ask and the app. Parameters: @since DATE, @as_of DATE.
-- One row per item, market and language: n is the item's posts dated @since to @as_of in that market that use
-- the language and carry an enrichment tone, and each tone column counts those posts with that label, so the
-- columns sum to n. A post counts under every language in its langs, as the enrichment hand check scores it, so
-- a code-switched post shows in each of its languages; the language rows of one item are not added together.
-- Counts only, never a share: a row with n under 5 is marked 'n < 5 insufficient' and reads as too few posts
-- to describe a tone. tone_cap is single_source on every NG or KE row except en rows whose posts are English
-- only: an und row is capped, and so is an en row holding a post that also carries another code (code-switched
-- Pidgin, Sheng or und). Until Albert names a Lagos or Nairobi reviewer those tone claims are capped at Single
-- source (docs/full-42/progress/L3.md, Decisions from Albert). A language under 80% native-speaker accuracy is
-- capped by core/eval/review.score_native, not here.
-- Only rows with tone set are enrichment rows (embedding rows have none), and a post written twice by a race
-- keeps one row on the same tie-break enrich_check_rows.sql uses. A post linked to an item by more than one
-- route (post_items.via) counts once.
WITH enriched AS (
  SELECT e.post_id, e.langs, e.tone
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  WHERE e.tone IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.post_id
    ORDER BY e.tone, e.stance, ARRAY_TO_STRING(e.langs, '|'), ARRAY_TO_STRING(e.entities, '|')
  ) = 1
),
item_posts AS (
  SELECT DISTINCT pi.item_id, p.post_id, p.geo_market AS market
  FROM `ogilvy-trends-v2.intelligence_42_core.post_items` AS pi
  JOIN `ogilvy-trends-v2.intelligence_42_core.posts` AS p
    ON p.post_id = pi.post_id
  WHERE p.post_date BETWEEN @since AND @as_of
),
not_english_only AS (
  SELECT DISTINCT en.post_id
  FROM enriched AS en
  CROSS JOIN UNNEST(en.langs) AS code
  WHERE code != 'en'
),
labelled AS (
  SELECT DISTINCT ip.item_id, ip.market, lang, ip.post_id, en.tone, ne.post_id IS NOT NULL AS mixed
  FROM item_posts AS ip
  JOIN enriched AS en
    ON en.post_id = ip.post_id
  CROSS JOIN UNNEST(en.langs) AS lang
  LEFT JOIN not_english_only AS ne
    ON ne.post_id = ip.post_id
)
SELECT
  l.item_id,
  l.market,
  l.lang,
  COUNT(*) AS n,
  COUNTIF(l.tone = 'celebratory') AS celebratory,
  COUNTIF(l.tone = 'humorous') AS humorous,
  COUNTIF(l.tone = 'sarcastic') AS sarcastic,
  COUNTIF(l.tone = 'angry') AS angry,
  COUNTIF(l.tone = 'sad') AS sad,
  COUNTIF(l.tone = 'hopeful') AS hopeful,
  COUNTIF(l.tone = 'informative') AS informative,
  COUNTIF(l.tone = 'neutral') AS neutral,
  COUNTIF(l.tone = 'mixed') AS mixed,
  IF(COUNT(*) < 5, 'n < 5 insufficient', 'counted') AS sample,
  IF(l.market IN ('NG', 'KE') AND (l.lang != 'en' OR LOGICAL_OR(l.mixed)), 'single_source', NULL) AS tone_cap
FROM labelled AS l
GROUP BY l.item_id, l.market, l.lang
ORDER BY l.item_id, l.market, l.lang
