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
