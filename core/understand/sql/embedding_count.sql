-- How many post_enrichment rows hold an embedding. CREATE VECTOR INDEX fails while every embedding is NULL
-- ("Failed to calculate array_min_len"), so run_embed creates the index only when this is above zero.
SELECT COUNT(*) AS n
FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment`
WHERE ARRAY_LENGTH(embedding) > 0
