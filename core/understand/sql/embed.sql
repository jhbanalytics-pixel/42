-- Embed one window of posts (BUILD.md 1.8, DATA.md section 1). One script, run as one query job.
-- Parameters: @from_date and @to_date, DATE, both inclusive; @max_rows, INT64, the most posts one window
-- sends to the model.
-- The window is the sighting day, not the publish day: a post is in it when any post_observations row for
-- it has observed_date inside the window (that table is partitioned on observed_date, so only the window's
-- partitions are read). Its post_date can be any day up to 400 days before the window, so a post published
-- yesterday and first seen today is embedded today.
-- A post is sent when its text, plus its transcript when it has one, is not empty and no post_enrichment
-- row already holds an embedding for it. Content is cut to 4000 characters, which can still reach the model's
-- 2048-token input for isiZulu or emoji-heavy text. At most @max_rows posts go per window, lowest post_id first;
-- the rest stay unembedded.
-- post_enrichment is append-only: this adds one row per embedded post and changes no existing row.
-- A row whose ml_generate_embedding_status is not empty, or whose vector is not 768 long, failed at Vertex.
-- It is not written and it is counted as failed. It still has no embedding, so it is sent again when a
-- later sighting of the post falls in a later window, and a backfill run (EMBED_DAYS) retries every post
-- sighted in its days that is still unembedded, the failed ones and those past @max_rows alike.
-- BigQuery tables store a NULL array as an empty one, so "has an embedding" is ARRAY_LENGTH > 0.
-- The last statement returns the counts: embedded, failed, skipped (no text, or embedded already), chars_sent,
-- the characters of content sent to the model, and tokens, the input tokens the spend correction is priced from:
-- each row's token_count in ml_generate_embedding_statistics as Vertex reports it, or, where a row reports none,
-- its content's UTF-8 bytes at three a token (as enrich prices a prompt) and at most the 2048 a post can bill.

CREATE TEMP TABLE IF NOT EXISTS window_posts AS
SELECT
  p.post_id,
  LEFT(TRIM(CONCAT(
    COALESCE(p.text, ''),
    IF(TRIM(COALESCE(p.transcript, '')) = '', '', CONCAT('\n', p.transcript))
  )), 4000) AS content,
  done.post_id IS NOT NULL AS has_embedding
FROM `ogilvy-trends-v2.intelligence_42_core.posts` AS p
LEFT JOIN (
  SELECT DISTINCT e.post_id
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  WHERE ARRAY_LENGTH(e.embedding) > 0
) AS done
  ON done.post_id = p.post_id
-- The post_date bound keeps each window from scanning all of posts. Sightings of posts older than 400 days
-- are out of Stage 1's scope, so those posts are left out of the window and are not counted as skipped.
WHERE p.post_date >= DATE_SUB(@from_date, INTERVAL 400 DAY)
  AND EXISTS (
    SELECT 1
    FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
    WHERE o.post_id = p.post_id AND o.observed_date BETWEEN @from_date AND @to_date
  );

CREATE TEMP TABLE IF NOT EXISTS to_send AS
SELECT w.post_id, w.content
FROM window_posts AS w
WHERE w.content != '' AND NOT w.has_embedding
ORDER BY w.post_id
LIMIT @max_rows;

CREATE TEMP TABLE IF NOT EXISTS generated AS
SELECT
  g.post_id,
  g.ml_generate_embedding_result AS embedding,
  COALESCE(g.ml_generate_embedding_status, '') = '' AND ARRAY_LENGTH(g.ml_generate_embedding_result) = 768 AS ok,
  COALESCE(
    SAFE_CAST(JSON_VALUE(g.ml_generate_embedding_statistics, '$.token_count') AS INT64),
    LEAST(CAST(CEIL(BYTE_LENGTH(g.content) / 3) AS INT64), 2048)
  ) AS tokens
FROM ML.GENERATE_EMBEDDING(
  MODEL `ogilvy-trends-v2.intelligence_42_core.embed_gemini`,
  (SELECT s.post_id, s.content FROM to_send AS s),
  STRUCT(768 AS output_dimensionality, 'RETRIEVAL_DOCUMENT' AS task_type, TRUE AS flatten_json_output)
) AS g;

INSERT INTO `ogilvy-trends-v2.intelligence_42_core.post_enrichment` (post_id, embedding)
SELECT g.post_id, g.embedding
FROM generated AS g
WHERE g.ok;

SELECT
  (SELECT COUNTIF(g.ok) FROM generated AS g) AS embedded,
  (SELECT COUNTIF(NOT g.ok) FROM generated AS g) AS failed,
  (SELECT COUNTIF(w.content = '' OR w.has_embedding) FROM window_posts AS w) AS skipped,
  (SELECT COALESCE(SUM(LENGTH(s.content)), 0) FROM to_send AS s) AS chars_sent,
  (SELECT COALESCE(SUM(g.tokens), 0) FROM generated AS g) AS tokens;
