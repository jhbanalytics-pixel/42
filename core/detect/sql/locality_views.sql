-- The SQL side of the locality_v2 reader (C4 v3 section 7.1, appendix F2). Applied by core/detect/job.py after
-- views.sql; it reads v_good_runs, so views.sql must come first.
--
-- v_item_locality_checked: the rows of item_locality that passed write-time verification (a verification row for the
-- same key, written by core/detect/locality.py only after Python recounted the key's members), at the pinned metric
-- version, with checked_status recomputed from the counts using the literals of core/trust/locality.py (test L-12)
-- and checked_label, the W8-DEC-17 label from the same counts. It does not filter on run status, so detect's own
-- state step can read the run's rows while the run is still running. Consumers read checked_status and
-- checked_label, never status.
--
-- v_item_locality_current: the same, for the good detect run of the date. For readers that run after detect: the
-- brief, the API, Ask.

-- tvf_post_items(d): the counted post to item links for date d (C4 v3 section 16, I-1 and I-4). Dated topic
-- links (cluster, cluster_pan and lineage links that carry linked_on) are ranked per post and market, lineage
-- first, then the market run, then the pooled run, ties to the lower item id; a link counts from linked_on and
-- until its post_item_end. Every row written before linked_on existed, and every link that is not a topic link,
-- passes through unchanged with a NULL market, so no count of history moves. The scan of post_observations is
-- bounded to the 28 days ending at d. Read by the locality_v2 members statement.
-- It is applied here and not with views.sql because it reads the tables the locality DDL adds (post_items.linked_on,
-- post_item_lineage, post_item_end), and views.sql is applied inside detect's main try: a release that lands the image before
-- that DDL then fails the locality step only, never detect (C4 v3 section 10).
CREATE OR REPLACE TABLE FUNCTION {core}.tvf_post_items(d DATE) AS
WITH raw AS (
  SELECT pi.post_id, pi.item_id, pi.via, pi.link_market, pi.linked_on
  FROM {core}.post_items pi
  WHERE pi.linked_on IS NULL OR pi.linked_on <= d
  UNION ALL
  SELECT l.post_id, l.item_id, 'lineage', l.link_market, l.linked_on
  FROM {core}.post_item_lineage l
  WHERE l.linked_on <= d),
live AS (
  SELECT r.* FROM raw r
  WHERE NOT EXISTS (SELECT 1 FROM {core}.post_item_end e
                    WHERE e.post_id = r.post_id AND e.item_id = r.item_id AND e.ended_on <= d)),
seen AS (
  SELECT DISTINCT o.post_id, o.market FROM {core}.post_observations o
  WHERE o.observed_date BETWEEN DATE_SUB(d, INTERVAL 27 DAY) AND d),
dated AS (
  SELECT l.post_id, l.item_id, s.market,
    CASE l.via WHEN 'lineage' THEN 0 WHEN 'cluster' THEN 1 ELSE 2 END prec
  FROM live l
  JOIN seen s ON s.post_id = l.post_id
  WHERE l.linked_on IS NOT NULL AND l.via IN ('cluster', 'cluster_pan', 'lineage')
    AND (l.link_market IS NULL OR l.link_market = s.market)),
winner AS (
  SELECT t.post_id, t.item_id, t.market FROM dated t
  QUALIFY ROW_NUMBER() OVER (PARTITION BY t.post_id, t.market ORDER BY t.prec, t.item_id) = 1)
SELECT l.post_id, l.item_id, l.via, CAST(NULL AS STRING) market
FROM live l WHERE l.linked_on IS NULL OR l.via NOT IN ('cluster', 'cluster_pan', 'lineage')
UNION ALL
SELECT w.post_id, w.item_id, 'topic', w.market FROM winner w;

CREATE OR REPLACE VIEW {core}.v_item_locality_checked AS
WITH c AS (
  SELECT l.*,
    CASE
      WHEN LEAST(l.population_posts, l.known_posts, l.local_posts, l.foreign_posts, l.unknown_posts) < 0 THEN 'unreadable'
      WHEN l.local_posts + l.foreign_posts != l.known_posts OR l.known_posts + l.unknown_posts != l.population_posts
        THEN 'unreadable'
      WHEN (CASE WHEN l.known_posts < 8 THEN 'market_unconfirmed'
                 WHEN 5 * l.local_posts >= 3 * l.known_posts THEN 'local'
                 ELSE 'not_local' END) != l.status THEN 'unreadable'
      WHEN NOT IFNULL((l.known_posts = 0 AND l.local_share IS NULL)
                      OR (l.known_posts > 0 AND ABS(l.local_share - SAFE_DIVIDE(l.local_posts, l.known_posts)) <= 0.000000001),
                      FALSE) THEN 'unreadable'
      ELSE l.status
    END checked_status
  FROM {core}.item_locality l
  JOIN (SELECT DISTINCT run_date, market, item_id, detect_run_id, metric_version
        FROM {core}.item_locality_verified) v
    ON v.run_date = l.run_date AND v.market = l.market AND v.item_id = l.item_id
       AND v.detect_run_id = l.detect_run_id AND v.metric_version = l.metric_version
  WHERE l.metric_version = 'locality_v2.1')
SELECT c.*,
  CASE c.checked_status
    WHEN 'market_unconfirmed' THEN 'market_unconfirmed'
    WHEN 'not_local' THEN 'not_local'
    WHEN 'local' THEN IF(
      (SAFE_DIVIDE(c.local_posts, c.known_posts) + 1.96 * 1.96 / (2 * c.known_posts)
        - 1.96 * SQRT(SAFE_DIVIDE(c.local_posts, c.known_posts) * (1 - SAFE_DIVIDE(c.local_posts, c.known_posts)) / c.known_posts
                      + 1.96 * 1.96 / (4 * c.known_posts * c.known_posts)))
      / (1 + 1.96 * 1.96 / c.known_posts) >= 0.5, 'local', 'market_unconfirmed')
  END checked_label
FROM c;

CREATE OR REPLACE VIEW {core}.v_item_locality_current AS
SELECT c.* FROM {core}.v_item_locality_checked c
JOIN {core}.v_good_runs g ON g.stage = 'detect' AND g.run_date = c.run_date AND g.run_id = c.detect_run_id;
