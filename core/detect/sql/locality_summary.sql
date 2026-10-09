INSERT INTO {core}.item_locality
  (run_date, market, item_id, detect_run_id, population_cutoff, metric_version, schema_version, computed_at,
   population_posts, known_posts, local_posts, foreign_posts, unknown_posts, feed_only_posts, vetoed_feed_posts,
   local_creators, known_creators, feed_only_creators, breadth_creators, status, local_share, population_digest)
WITH keys AS (
  SELECT st.item_id, st.market FROM {core}.v_series_test_current st
  WHERE st.metric_date = @d AND st.market IN ('ZA', 'NG', 'KE')
  UNION DISTINCT
  SELECT m.item_id, m.market FROM {core}.item_locality_post m
  WHERE m.run_date = @d AND m.detect_run_id = @detect_run_id AND m.metric_version = @metric_version),
agg AS (
  SELECT m.item_id, m.market,
    COUNT(*) population_posts,
    COUNTIF(m.locality_class != 'unknown') known_posts,
    COUNTIF(m.locality_class = 'local') local_posts,
    COUNTIF(m.locality_class = 'foreign') foreign_posts,
    COUNTIF(m.locality_class = 'unknown') unknown_posts,
    COUNTIF(m.locality_class = 'unknown' AND m.feed_sighted) feed_only_posts,
    COUNTIF(m.locality_class = 'foreign' AND m.feed_sighted) vetoed_feed_posts,
    COUNT(DISTINCT IF(m.locality_class = 'local', IFNULL(m.creator_key, m.post_id), NULL)) local_creators,
    COUNT(DISTINCT IF(m.locality_class != 'unknown', IFNULL(m.creator_key, m.post_id), NULL)) known_creators,
    COUNT(DISTINCT IF(m.locality_class = 'unknown' AND m.feed_sighted, IFNULL(m.creator_key, m.post_id), NULL)) feed_only_creators,
    COUNT(DISTINCT IF(m.locality_class = 'local' OR (m.locality_class = 'unknown' AND m.feed_sighted),
                      IFNULL(m.creator_key, m.post_id), NULL)) breadth_creators,
    TO_HEX(SHA256(STRING_AGG(
      CONCAT(m.post_id, '|', m.locality_class, '|', IFNULL(m.creator_key, ''), '|', IF(m.feed_sighted, '1', '0')),
      CHR(10) ORDER BY m.post_id))) population_digest
  FROM {core}.item_locality_post m
  WHERE m.run_date = @d AND m.detect_run_id = @detect_run_id AND m.metric_version = @metric_version
  GROUP BY m.item_id, m.market)
SELECT @d, k.market, k.item_id, @detect_run_id, @cutoff, @metric_version, 1, CURRENT_TIMESTAMP(),
  IFNULL(a.population_posts, 0), IFNULL(a.known_posts, 0), IFNULL(a.local_posts, 0), IFNULL(a.foreign_posts, 0),
  IFNULL(a.unknown_posts, 0), IFNULL(a.feed_only_posts, 0), IFNULL(a.vetoed_feed_posts, 0),
  IFNULL(a.local_creators, 0), IFNULL(a.known_creators, 0), IFNULL(a.feed_only_creators, 0),
  IFNULL(a.breadth_creators, 0),
  CASE WHEN IFNULL(a.known_posts, 0) < 8 THEN 'market_unconfirmed'
       WHEN 5 * a.local_posts >= 3 * a.known_posts THEN 'local'
       ELSE 'not_local' END,
  SAFE_DIVIDE(a.local_posts, a.known_posts),
  IFNULL(a.population_digest, TO_HEX(SHA256('')))
FROM keys k
LEFT JOIN agg a ON a.item_id = k.item_id AND a.market = k.market
WHERE NOT EXISTS (SELECT 1 FROM {core}.item_locality done
                  WHERE done.run_date = @d AND done.detect_run_id = @detect_run_id
                    AND done.metric_version = @metric_version)
