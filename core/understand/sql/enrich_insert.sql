-- Append validated model enrichment to post_enrichment (BUILD.md 2.1). Parameter: @rows, STRING, a JSON array
-- of objects written by enrich.enrichment_row, one per post, so the job passes one scalar parameter.
-- post_enrichment is append-only: this adds rows and changes none. embedding is left out, so these rows hold
-- no vector, and the embed step's own rows are untouched. A post that already has an enrichment row (tone
-- set) is not written again, which keeps a rerun from adding a second copy.

INSERT INTO `ogilvy-trends-v2.intelligence_42_core.post_enrichment` (post_id, langs, code_switched, entities, sounds, formats, tone, stance, sponsored)
SELECT r.post_id, r.langs, r.code_switched, r.entities, r.sounds, r.formats, r.tone, r.stance, r.sponsored
FROM (
  SELECT
    JSON_VALUE(j, '$.post_id') AS post_id,
    JSON_VALUE_ARRAY(j, '$.langs') AS langs,
    CAST(JSON_VALUE(j, '$.code_switched') AS BOOL) AS code_switched,
    JSON_VALUE_ARRAY(j, '$.entities') AS entities,
    JSON_VALUE_ARRAY(j, '$.sounds') AS sounds,
    JSON_VALUE_ARRAY(j, '$.formats') AS formats,
    JSON_VALUE(j, '$.tone') AS tone,
    JSON_VALUE(j, '$.stance') AS stance,
    CAST(JSON_VALUE(j, '$.sponsored') AS BOOL) AS sponsored
  FROM UNNEST(JSON_QUERY_ARRAY(@rows)) AS j
) AS r
WHERE NOT EXISTS (
  SELECT 1
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  WHERE e.post_id = r.post_id AND e.tone IS NOT NULL
)
