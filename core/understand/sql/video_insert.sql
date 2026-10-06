-- Append video reading's rows to post_enrichment (BUILD.md 2.6, DATA.md). Parameter: @rows, STRING, a JSON array of
-- objects written by video.read_clip, one per clip: post_id, screen_text and video_notes (a JSON string).
-- post_enrichment is append-only: this adds rows and changes none. Every other column is left NULL, so the embed
-- and enrich rows of the same post are untouched and no reader of tone or embedding sees this row. A post that
-- already has a video_notes row is not written again, which keeps a rerun from adding a second copy.

INSERT INTO `ogilvy-trends-v2.intelligence_42_core.post_enrichment` (post_id, screen_text, video_notes)
SELECT r.post_id, r.screen_text, r.video_notes
FROM (
  SELECT
    JSON_VALUE(j, '$.post_id') AS post_id,
    JSON_VALUE(j, '$.screen_text') AS screen_text,
    JSON_VALUE(j, '$.video_notes') AS video_notes
  FROM UNNEST(JSON_QUERY_ARRAY(@rows)) AS j
) AS r
WHERE r.video_notes IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
    WHERE e.post_id = r.post_id AND e.video_notes IS NOT NULL
  )
