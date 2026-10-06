-- Remote embedding model for 42 (BUILD.md 1.8): gemini-embedding-001 on Vertex through connection f42-vertex.
-- The connection must sit in the datasets' location. SETUP.md puts BigQuery in the US multi-region, so the
-- connection id is ogilvy-trends-v2.us.f42-vertex. Albert's bootstrap creates the connection and grants its
-- service account aiplatform.user; until then this statement fails with "connection not found".
CREATE MODEL IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.embed_gemini`
  REMOTE WITH CONNECTION `ogilvy-trends-v2.us.f42-vertex`
  OPTIONS (ENDPOINT = 'gemini-embedding-001');
