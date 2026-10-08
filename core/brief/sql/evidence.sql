-- The morning brief's evidence pack for one item and market (BUILD.md 1.12, TRUST.md section 3 step 4), in
-- BigQuery Standard SQL. Read-only. {core} and {agent} are the dataset names, filled in by core/detect/sqlrun.py.
-- core/brief/evidence.py runs each statement by the name on its own name line. The number queries read
-- item_state itself pinned to the detect @run_id (DATA.md 3.1 allows this for a re-run pinned to a run_id), so
-- a newer detect run never hides the pinned row; each returns one column, value, so a re-run gives the same
-- rows to hash.

-- name: suppressed
-- The suppression list (SETUP.md data protection) as f42-api reads it: each creator id in v_suppressed_creators
-- with the platform and handle of its creators rows, which evidence.py turns into creator keys.
SELECT DISTINCT s.creator_id, c.platform, c.handle
FROM {core}.v_suppressed_creators s
LEFT JOIN {core}.creators c ON c.creator_id = s.creator_id;

-- name: evidence
-- Posts a card may cite: published in the market's last 7 local days (@start to @end, market-local midnights),
-- sighted in the market in that span in any lane but placebo, agent_live and legacy, by a creator not on the
-- suppression list; at most 2 per creator and 12 in all; measured lanes (unbiased_rank, panel) first, then
-- engagement. A post seen only by the brief's confirm search enters only when local by the market scope rule
-- (market_scope.sql creator_ranked): located in the market or sighted in its feeds in the window. sponsored is the
-- enrich model's paid marker or the vendor's paid label; sponsor_checked is true when either gave a reading.
WITH source_market_by_post AS (
  SELECT v.post_id,
    COALESCE(MAX(IF(sight.source_market = @market, sight.source_market, NULL)),
             IF(COUNT(DISTINCT sight.source_market) = 1, MAX(sight.source_market), NULL)) source_market
  FROM {core}.v_post_source_markets v
  CROSS JOIN UNNEST(v.source_sightings) sight
  WHERE sight.obs_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND sight.source_market IN ('ZA', 'NG', 'KE')
  GROUP BY v.post_id
),
s AS (
  SELECT po.post_id, LOGICAL_OR(po.lane_class IN ('unbiased_rank', 'panel')) measured,
    LOGICAL_OR(IFNULL(po.lane, '') != 'confirm') beyond_confirm
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  WHERE pi.item_id = @item_id AND po.market = @market
    AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY po.post_id),
r AS (
  SELECT ps.post_id, ps.platform, ps.url, ps.published_at, ps.creator_tier_at_post creator_tier,
    ps.geo_market, ps.geo_confidence, ps.geo_source,
    source_market_by_post.source_market,
    IFNULL(NULLIF(ps.text, ''), ps.transcript) quote_text,
    ps.views, ps.likes, ps.comments, ps.shares, ps.thumbnail_url, ps.duration_s, cr.handle,
    IFNULL(cr_flags.coord_score, 0) >= 1 flagged,
    IFNULL(pe.sponsored, FALSE) enrich_sponsored, IFNULL(pe.sponsor_read, FALSE) enrich_read,
    -- The vendor's paid label (posts.vendor_labels, core/collect/socialcrawl_client.py split_vendor_labels): a
    -- boolean at sponsored or labels.sponsored, or an object there whose disclosed or undisclosed boolean says the
    -- post is paid. TRUE when it says paid, FALSE when it says not paid, NULL when it gives no reading.
    (SELECT LOGICAL_OR(LOWER(label) = 'true')
     FROM UNNEST([
       JSON_VALUE(ps.vendor_labels, '$.sponsored'), JSON_VALUE(ps.vendor_labels, '$.sponsored.disclosed'),
       JSON_VALUE(ps.vendor_labels, '$.sponsored.undisclosed'), JSON_VALUE(ps.vendor_labels, '$.labels.sponsored'),
       JSON_VALUE(ps.vendor_labels, '$.labels.sponsored.disclosed'),
       JSON_VALUE(ps.vendor_labels, '$.labels.sponsored.undisclosed')]) label
     WHERE LOWER(label) IN ('true', 'false')) vendor_paid,
    IFNULL(pe.near_dup_size, 1) >= 3 near_dup,
    s.measured, IFNULL(ps.engagement, 0) eng,
    ROW_NUMBER() OVER (PARTITION BY IFNULL(ps.creator_id, ps.post_id)
                       ORDER BY s.measured DESC, IFNULL(ps.engagement, 0) DESC, ps.post_id) creator_rank
  FROM s
  JOIN {core}.posts ps ON ps.post_id = s.post_id
  LEFT JOIN source_market_by_post ON source_market_by_post.post_id = s.post_id
  LEFT JOIN {core}.creators cr ON cr.creator_id = ps.creator_id AND cr.platform = ps.platform
  LEFT JOIN (
    SELECT creator_id, MAX(IFNULL(coord_score, 0)) coord_score
    FROM {core}.creators GROUP BY creator_id
  ) cr_flags ON cr_flags.creator_id = ps.creator_id
  -- One row per post: post_enrichment can hold several rows for a post, which would repeat the evidence record.
  -- sponsor_read: a row the enrich model wrote, with its sponsored reading; embed and video rows leave it null.
  LEFT JOIN (SELECT post_id, MAX(near_dup_size) near_dup_size, LOGICAL_OR(sponsored) sponsored,
               LOGICAL_OR(sponsored IS NOT NULL) sponsor_read
             FROM {core}.post_enrichment GROUP BY post_id) pe ON pe.post_id = s.post_id
  WHERE ps.published_at >= @start AND ps.published_at < @end
    AND NOT EXISTS (SELECT 1 FROM {core}.v_suppressed_creators sc WHERE sc.creator_id = ps.creator_id)
    -- A post seen only by the brief's confirm search counts only when local by the market scope rule
    -- (market_scope.sql creator_ranked), as before confirm finds were linked to their item.
    AND (s.beyond_confirm
      OR (ps.geo_market = @market AND IFNULL(ps.geo_confidence, 0) >= 0.7
          AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'))
      OR (NOT IFNULL(NULLIF(ps.geo_market, '') != @market AND ps.geo_confidence >= 0.7
                         AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
      AND EXISTS (
        SELECT 1
        FROM {core}.v_post_source_markets v
        CROSS JOIN UNNEST(v.source_sightings) sight
        WHERE v.post_id = ps.post_id
          AND sight.source_market = @market
          AND sight.obs_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
      ))))
SELECT r.* EXCEPT (enrich_sponsored, enrich_read, vendor_paid),
  r.enrich_sponsored OR IFNULL(r.vendor_paid, FALSE) sponsored,
  r.enrich_read OR r.vendor_paid IS NOT NULL sponsor_checked
FROM r
WHERE r.creator_rank <= 2
ORDER BY r.measured DESC, r.eng DESC, r.post_id
LIMIT 12;

-- name: creators3
SELECT s.creators3 value FROM {core}.item_state s
WHERE s.item_id = @item_id AND s.market = @market AND s.metric_date = @d AND s.run_id = @run_id;

-- name: posts3
SELECT s.posts3 value FROM {core}.item_state s
WHERE s.item_id = @item_id AND s.market = @market AND s.metric_date = @d AND s.run_id = @run_id;

-- name: main_ratio
SELECT s.main_ratio value FROM {core}.item_state s
WHERE s.item_id = @item_id AND s.market = @market AND s.metric_date = @d AND s.run_id = @run_id;

-- name: rival_state
-- Detect's rival-explanation values for the item (METHOD-GAPS Gap 7): the columns of item_state that name a simpler
-- explanation, read pinned to the detect @run_id exactly as the number queries above read creators3. One row;
-- evidence.py takes one column per pinned value and a re-run takes the same column, so each re-runs to the value
-- it pinned.
SELECT st.sponsored_share, st.local_share, st.markets_hot, st.share_flags, st.diffusion, st.lead_market, st.novelty,
  st.moment
FROM {core}.item_state st
WHERE st.item_id = @item_id AND st.market = @market AND st.metric_date = @d AND st.run_id = @run_id;

-- name: rival_cutoff
-- The data cutoff of the detect run the pack is pinned to: the time that run started. The window numbers below read
-- observations up to it, so a post observed after the run cannot change what a K2 re-run returns. runs is
-- partitioned by run_date, and the detect run for a brief date started within a day before it or two after. The run
-- must be the one that wrote this item's state for the date (item_state is partitioned by metric_date).
SELECT r.started_at value
FROM {agent}.runs r
WHERE r.run_id = @run_id AND r.run_date BETWEEN DATE_SUB(@d, INTERVAL 1 DAY) AND DATE_ADD(@d, INTERVAL 2 DAY)
  AND EXISTS (SELECT 1 FROM {core}.item_state st
              WHERE st.run_id = r.run_id AND st.item_id = @item_id AND st.market = @market AND st.metric_date = @d);

-- name: rival_window
-- The 7 day window values item_state does not store, computed as tvf_item_window (views.sql) computes them for this
-- item and market, but only from observations made by @cutoff, the detect run's start (rival_cutoff), so the numbers
-- re-run to the same value for that run. A post observed before the cutoff but written late still counts. Partition
-- filter: post_observations by observed_date, the 28 days to @d, as the table function reads it. posts and
-- post_enrichment have no date filter (posts is partitioned by publish date, and a post seen this week can be
-- older); both are read only for the post ids that survive the joins.
WITH o AS (
  SELECT po.post_id, po.lane_class IN ('unbiased_rank', 'panel') AS measured, MIN(po.observed_date) first_day
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  WHERE pi.item_id = @item_id AND po.market = @market
    AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 27 DAY) AND @d
    AND po.observed_at <= @cutoff
    AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY po.post_id, measured),
p AS (
  SELECT o.post_id, o.measured, o.first_day > DATE_SUB(@d, INTERVAL 7 DAY) in7,
    ps.creator_id, ps.creator_tier_at_post tier, ps.published_at, IFNULL(pe.near_dup_size, 1) >= 3 near_dup
  FROM o
  JOIN {core}.posts ps ON ps.post_id = o.post_id
  LEFT JOIN (SELECT pe0.post_id, MAX(pe0.near_dup_size) near_dup_size
             FROM {core}.post_enrichment pe0 GROUP BY pe0.post_id) pe ON pe.post_id = o.post_id),
a7 AS (
  SELECT COUNT(DISTINCT IF(p.in7, p.post_id, NULL)) posts7,
    SAFE_DIVIDE(COUNT(DISTINCT IF(p.in7 AND p.near_dup, p.post_id, NULL)),
                COUNT(DISTINCT IF(p.in7, p.post_id, NULL))) near_dup_share,
    MIN(IF(p.measured AND p.tier IN ('nano', 'micro'), p.published_at, NULL)) small_at,
    MIN(IF(p.measured AND p.tier IN ('macro', 'mega'), p.published_at, NULL)) large_at
  FROM p),
t3 AS (
  SELECT SAFE_DIVIDE(SUM(IF(c.rn <= 3, c.n, 0)), SUM(c.n)) top3_share
  FROM (SELECT cn.creator_id, cn.n, ROW_NUMBER() OVER (ORDER BY cn.n DESC, cn.creator_id) rn
        FROM (SELECT p.creator_id, COUNT(DISTINCT p.post_id) n FROM p WHERE p.in7 GROUP BY p.creator_id) cn) c),
bu AS (
  SELECT SAFE_DIVIDE(MAX(b.n), SUM(b.n)) burst_share
  FROM (SELECT DIV(UNIX_SECONDS(p.published_at), 600) bucket, COUNT(DISTINCT p.post_id) n
        FROM p WHERE p.in7 AND p.published_at IS NOT NULL GROUP BY bucket) b)
SELECT a7.posts7, bu.burst_share, t3.top3_share, a7.near_dup_share, a7.small_at, a7.large_at
FROM a7 CROSS JOIN t3 CROSS JOIN bu
WHERE a7.posts7 > 0;

-- name: sparkline
-- The main series' last 14 days; days with no row (before the series started) are filled in by evidence.py.
SELECT sd.day, sd.value, sd.lane_class FROM {core}.v_series_daily sd
WHERE sd.series_id = @series_id AND sd.day BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d
ORDER BY sd.day;

-- name: first_seen
-- First day 42 saw the item in the market, legacy memory included; agent_live never enters item_daily.
SELECT MIN(i.metric_date) first_seen FROM {core}.v_item_daily_current i
WHERE i.item_id = @item_id AND i.market = @market AND i.metric_date <= @d;
