-- Vector index on post embeddings (BUILD.md 1.8): IVF, cosine, the metric tvf_search_posts uses.
-- BigQuery refuses this statement on fewer than 5,000 rows ("Total rows 2931 is smaller than min allowed 5000"
-- for the IVF index type), so run_embed sends it only once post_enrichment holds
-- embed.INDEX_MIN_ROWS embeddings, and records a refusal without failing the run. Rows without an embedding hold
-- an empty array, which is not skipped: BigQuery refuses the build while the column mixes lengths (8 and 9 Oct 2026),
-- and embedding_count.sql counts those rows.
CREATE VECTOR INDEX IF NOT EXISTS post_enrichment_embedding
ON `ogilvy-trends-v2.intelligence_42_core.post_enrichment`(embedding)
OPTIONS (index_type = 'IVF', distance_type = 'COSINE');
