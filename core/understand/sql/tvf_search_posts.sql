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
  WITH scored AS (
    SELECT
      e.post_id AS post_id,
      ML.DISTANCE(e.embedding, qe.embedding, 'COSINE') AS distance,
      w.platform AS platform,
      w.url AS url,
      w.creator_id AS creator_id,
      w.text AS text,
      w.published_at AS published_at,
      w.geo_market AS geo_market,
      w.engagement AS engagement
    FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
    JOIN `ogilvy-trends-v2.intelligence_42_core.posts` AS w
      ON w.post_id = e.post_id
    CROSS JOIN (
      SELECT g.ml_generate_embedding_result AS embedding
      FROM ML.GENERATE_EMBEDDING(
        MODEL `ogilvy-trends-v2.intelligence_42_core.embed_gemini`,
        (SELECT q AS content),
        STRUCT(768 AS output_dimensionality, 'RETRIEVAL_QUERY' AS task_type, TRUE AS flatten_json_output)
      ) AS g
    ) AS qe
    WHERE ARRAY_LENGTH(e.embedding) = 768
      AND w.post_date BETWEEN since AND until
      AND (
        market IS NULL
        OR w.geo_market = market
        OR (
          (w.geo_market IS NULL OR w.geo_source = 'home_market')
          AND (
            EXISTS (
              SELECT 1
              FROM `ogilvy-trends-v2.intelligence_42_core.creators` AS c
              WHERE c.platform = w.platform
                AND c.creator_id = w.creator_id
                AND c.home_market = market
            )
            OR EXISTS (
              SELECT 1
              FROM `ogilvy-trends-v2.intelligence_42_core.v_post_source_markets` AS sm
              CROSS JOIN UNNEST(sm.source_sightings) AS ss
              WHERE sm.post_id = w.post_id
                AND ss.obs_date BETWEEN since AND until
                AND ss.source_market = market
            )
          )
        )
      )
  ),
  nearest AS (
    SELECT
      post_id,
      MIN(distance) AS distance,
      ANY_VALUE(platform) AS platform,
      ANY_VALUE(url) AS url,
      ANY_VALUE(creator_id) AS creator_id,
      ANY_VALUE(text) AS text,
      ANY_VALUE(published_at) AS published_at,
      ANY_VALUE(geo_market) AS geo_market,
      ANY_VALUE(engagement) AS engagement
    FROM scored
    GROUP BY post_id
  ),
  ranked AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY distance, post_id) AS rn
    FROM nearest
  )
  SELECT
    r.post_id AS post_id,
    r.distance AS distance,
    r.platform AS platform,
    r.url AS url,
    r.creator_id AS creator_id,
    r.text AS text,
    r.published_at AS published_at,
    r.geo_market AS geo_market,
    r.engagement AS engagement
  FROM ranked AS r
  WHERE r.rn <= k
);
