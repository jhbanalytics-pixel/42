-- Semantic search for Ask (DATA.md section 7): embed q as a retrieval query, then score every stored post
-- embedding inside the date window and market by exact cosine distance. market NULL searches every market.
-- Results are the k nearest posts; order by distance, then post_id, when reading them. Embedding q calls
-- Vertex, so the caller needs bigquery.connectionUser on f42-vertex (f42-agent has it, SETUP.md).
-- This is exact search, a brute-force scan with ML.DISTANCE, which is fine at Stage 1 volume. Staging
-- refuses VECTOR_SEARCH inside a table function whatever its base query. When post_enrichment grows past a
-- few hundred thousand rows, revisit the vector index plus VECTOR_SEARCH outside a TVF.
-- Ceiling: post_enrichment is unpartitioned at about 6.1 KB per embedded row, and every call reads all of
-- it, so Ask's 2 GB bytes-billed cap refuses the TVF at roughly 320,000 embedded rows. run_embed reports
-- embedded_total each day; watch it against that line.
-- posts is read once, inside the scoring CTE where post_date is filtered, and every output column is carried
-- from there, so the read is pruned to the window's partitions.
-- post_enrichment is append-only and can hold a post twice, so each post is kept once at its smallest
-- distance before the first k are taken.
-- The copy of this function already on staging was created before this fix and still joins posts a second
-- time without the date filter. This file's IF NOT EXISTS leaves it in place; swapping it for this
-- version is Albert's step.
CREATE TABLE FUNCTION IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.tvf_search_posts`(
  q STRING, market STRING, since DATE, until DATE, k INT64)
AS (
  SELECT * FROM `ogilvy-trends-v2.intelligence_42_agent.tvf_search_posts_scoped`(
    q, market, since, until, k, CAST(NULL AS ARRAY<STRING>), CAST(NULL AS STRING), CAST(NULL AS INT64))
);
