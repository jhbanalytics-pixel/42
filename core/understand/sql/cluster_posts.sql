-- The posts one clustering run fits on (BUILD.md 2.2, DATA.md section 4): every post sighted in the three days
-- ending @run_date that has a stored embedding. Parameters: @run_date DATE, @market STRING. A market run reads
-- the posts sighted in that market; the pooled 'pan' run reads every market. @market comes lower case ('za', as
-- cluster.MARKETS and the clusters table hold it) and post_observations.market upper case ('ZA', as collect writes
-- it), so the two are compared upper cased; market below is returned as post_observations holds it.
-- today is true for a post sighted on @run_date: only those posts are assigned to clusters, the other two days
-- only shape the topics. market is the market of the post's latest sighting in the window.
-- post_enrichment holds an embed row and an enrich row per post; only a row with a vector is read, and one per
-- post. post_enrichment has no timestamp, so when a post has two vector rows the one with the lowest first element
-- is read: a deterministic tiebreak whatever order the rows landed in. Two embeddings of one text by one model are
-- near identical, so which of them is read barely moves the fit.
-- The post_date bound keeps the scan to Stage 1's 400 days before the window, as embed.sql does.
WITH seen AS (
  SELECT
    o.post_id,
    MAX(o.observed_date) AS seen_date,
    MAX_BY(o.market, o.observed_date) AS market
  FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
  WHERE o.observed_date BETWEEN DATE_SUB(@run_date, INTERVAL 2 DAY) AND @run_date
    AND (@market = 'pan' OR UPPER(o.market) = UPPER(@market))
  GROUP BY o.post_id
),
vectors AS (
  SELECT e.post_id, e.embedding
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  WHERE ARRAY_LENGTH(e.embedding) > 0
    AND e.post_id IN (SELECT s.post_id FROM seen AS s)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.post_id ORDER BY e.embedding[OFFSET(0)]) = 1
)
SELECT
  p.post_id,
  p.platform,
  p.creator_id,
  COALESCE(p.text, '') AS text,
  p.hashtags,
  p.sound_id,
  s.market,
  s.seen_date = @run_date AS today,
  v.embedding
FROM seen AS s
JOIN `ogilvy-trends-v2.intelligence_42_core.posts` AS p
  ON p.post_id = s.post_id AND p.post_date >= DATE_SUB(@run_date, INTERVAL 402 DAY)
JOIN vectors AS v
  ON v.post_id = s.post_id
ORDER BY p.post_id
