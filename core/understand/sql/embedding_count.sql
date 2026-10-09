-- How many post_enrichment rows hold an embedding, and how many rows a vector index cannot take. One scan of the
-- column answers both.
-- n: rows with an embedding. CREATE VECTOR INDEX fails while every embedding is NULL ("Failed to calculate
-- array_min_len"), so run_embed creates the index only when this is above zero.
-- unindexable: rows whose embedding is not 768 long, which is every row enrich.py wrote (it leaves the vector out and
-- BigQuery stores it as an empty array) and any short vector. The index build refuses a column whose arrays are not
-- all one length ("Column 'embedding' must have the same array length, while the minimum length is 0 and the maximum
-- length is 768", 8 and 9 Oct 2026), so while this is above zero a new build of the index fails.
SELECT
  COUNTIF(ARRAY_LENGTH(embedding) > 0) AS n,
  COUNTIF(COALESCE(ARRAY_LENGTH(embedding), 0) != 768) AS unindexable
FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment`
