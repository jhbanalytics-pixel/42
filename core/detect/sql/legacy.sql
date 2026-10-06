-- The legacy backfill, BUILD.md 1.7 and DATA.md section 2, in BigQuery Standard SQL. {legacy} is the old
-- dataset (trends_v2_dev), read by SELECT only; {core} and {agent} are filled in by core/detect/sqlrun.py.
-- Each statement follows a "name:" comment that core/detect/legacy.py reads. No statement reads a genz or
-- search velocity column or a Google Trends source; platforms are an allow-list, so the Google search lane
-- (platform search or google_search) never enters. Platform x is read as twitter.
-- A legacy day is the UTC day of collection: seed_graph trend_date, DATE(collected_at) for enriched_content.

-- name: dates
-- Legacy days with rows the seed and handles statements read, less any day 42 itself collected or
-- aggregated: a collection_health row, or an ok aggregate run that holds no legacy item_daily rows that day.
SELECT DISTINCT x.metric_date FROM (
  SELECT sg.trend_date metric_date FROM {legacy}.seed_graph sg
  WHERE sg.trend_date BETWEEN @since AND @until AND UPPER(sg.market) IN ('ZA', 'NG', 'KE')
    AND sg.term_type IN ('hashtag', 'slang', 'handle', 'music')
    AND LOWER(sg.platform) IN ('tiktok', 'instagram', 'youtube', 'reddit', 'twitter', 'x', 'threads', 'facebook', 'news', 'music', 'web')
  UNION ALL
  SELECT DATE(ec.collected_at) FROM {legacy}.enriched_content ec
  WHERE DATE(ec.collected_at) BETWEEN @since AND @until AND UPPER(ec.market) IN ('ZA', 'NG', 'KE')
    AND LOWER(ec.platform) IN ('tiktok', 'instagram', 'twitter', 'x', 'youtube', 'reddit', 'threads', 'facebook')
    AND LOWER(ec.source) NOT IN ('google trends', 'google_trends', 'bigquery_trends')
    AND NOT (LOWER(ec.source) = 'brand24' AND LOWER(IFNULL(ec.content_type, '')) IN ('link', 'author', 'aggregate'))
    AND IFNULL(ec.author_handle_norm, '') != '') x
WHERE NOT EXISTS (SELECT 1 FROM {core}.collection_health h WHERE h.day = x.metric_date)
  AND NOT EXISTS (
    SELECT 1 FROM {agent}.runs r
    WHERE r.stage = 'aggregate' AND r.status = 'ok' AND r.run_date = x.metric_date
      AND NOT EXISTS (SELECT 1 FROM {core}.item_daily i
                      WHERE i.run_id = r.run_id AND i.metric_date = r.run_date AND i.lane_class = 'legacy'))
ORDER BY x.metric_date;

-- name: first_collect
-- The first day 42 collected; legacy.py refuses an until on or after it.
SELECT MIN(h.day) first_day FROM {core}.collection_health h;

-- name: done
-- Legacy dates already written: an ok aggregate runs row whose run_id holds legacy item_daily rows that day.
SELECT DISTINCT r.run_date FROM {agent}.runs r
JOIN {core}.item_daily i ON i.run_id = r.run_id AND i.metric_date = r.run_date
WHERE r.stage = 'aggregate' AND r.status = 'ok' AND i.lane_class = 'legacy'
  AND r.run_date BETWEEN @since AND @until;

-- name: seed
-- seed_graph terms for day @d: row_count is the day's posts. MAX, not SUM, so a day the old job wrote twice
-- counts once. Tokens and channels are skipped.
SELECT UPPER(sg.market) market, IF(LOWER(sg.platform) = 'x', 'twitter', LOWER(sg.platform)) platform,
  sg.term, sg.term_type,
  MAX(sg.row_count) posts, MAX(sg.generated_at) available_at
FROM {legacy}.seed_graph sg
WHERE sg.trend_date = @d AND UPPER(sg.market) IN ('ZA', 'NG', 'KE')
  AND sg.term_type IN ('hashtag', 'slang', 'handle', 'music')
  AND LOWER(sg.platform) IN ('tiktok', 'instagram', 'youtube', 'reddit', 'twitter', 'x', 'threads', 'facebook', 'news', 'music', 'web')
GROUP BY UPPER(sg.market), IF(LOWER(sg.platform) = 'x', 'twitter', LOWER(sg.platform)), sg.term, sg.term_type;

-- name: handles
-- enriched_content authors on social platforms for day @d. A post counts once per market, on the day it was
-- first collected there. The hashtags column is empty on every row, so no hashtag items come from here.
WITH ec AS (
  SELECT e.id, UPPER(e.market) market, IF(LOWER(e.platform) = 'x', 'twitter', LOWER(e.platform)) platform,
    e.author_handle_norm handle,
    e.engagement_total, e.published_at, e.collected_at
  FROM {legacy}.enriched_content e
  WHERE DATE(e.collected_at) <= @d AND UPPER(e.market) IN ('ZA', 'NG', 'KE')
    AND LOWER(e.platform) IN ('tiktok', 'instagram', 'twitter', 'x', 'youtube', 'reddit', 'threads', 'facebook')
    AND LOWER(e.source) NOT IN ('google trends', 'google_trends', 'bigquery_trends')
    AND NOT (LOWER(e.source) = 'brand24' AND LOWER(IFNULL(e.content_type, '')) IN ('link', 'author', 'aggregate'))
    AND IFNULL(e.author_handle_norm, '') != ''),
firsts AS (
  SELECT ec.id, ec.market, ec.platform, ec.handle, ec.engagement_total, ec.published_at, ec.collected_at
  FROM ec
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ec.id, ec.market ORDER BY ec.collected_at, ec.platform) = 1)
SELECT f.market, f.platform, f.handle, COUNT(DISTINCT f.id) posts,
  CAST(ROUND(SUM(IFNULL(f.engagement_total, 0))) AS INT64) engagement,
  MIN(f.published_at) first_post_at, MAX(f.collected_at) available_at
FROM firsts f
WHERE DATE(f.collected_at) = @d
GROUP BY f.market, f.platform, f.handle;

-- name: merge
-- Items into cultural_map, by the same status rule as core/detect/aggregate.py: new items are inserted with
-- their status ('generic' when stoplisted, else 'active'); on an open row first_seen only moves earlier, with
-- its market and platform, last_seen only later, and an 'active' or 'generic' status follows the stoplist both
-- ways; any other status such as 'rejected' never changes; closed rows are left alone.
MERGE {core}.cultural_map t
USING (SELECT n.item_id, n.kind, n.canonical_key, n.label, n.first_seen, n.first_seen_market,
         n.first_seen_platform, n.last_seen, n.status FROM UNNEST(@items) n) s
ON t.item_id = s.item_id
WHEN MATCHED AND t.valid_to IS NULL THEN
  UPDATE SET
    first_seen = IF(t.first_seen IS NULL OR s.first_seen < t.first_seen, s.first_seen, t.first_seen),
    first_seen_market = IF(t.first_seen IS NULL OR s.first_seen < t.first_seen, s.first_seen_market,
                           t.first_seen_market),
    first_seen_platform = IF(t.first_seen IS NULL OR s.first_seen < t.first_seen, s.first_seen_platform,
                             t.first_seen_platform),
    last_seen = GREATEST(IFNULL(t.last_seen, s.last_seen), s.last_seen),
    status = IF(t.status IN ('active', 'generic'), s.status, t.status)
WHEN NOT MATCHED THEN
  INSERT (item_id, kind, canonical_key, label, first_seen, first_seen_market, first_seen_platform, last_seen,
          status, valid_from)
  VALUES (s.item_id, s.kind, s.canonical_key, s.label, s.first_seen, s.first_seen_market, s.first_seen_platform,
          s.last_seen, s.status, CURRENT_TIMESTAMP());
