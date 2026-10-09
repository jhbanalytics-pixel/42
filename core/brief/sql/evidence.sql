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
-- Posts a card may cite (C4 v2 section 19, core/brief/pack_order.py): published in the market's last 7 local days
-- (@start to @end, market-local midnights), sighted in the market in that span in any lane but placebo, agent_live
-- and legacy, by a creator not on the suppression list. Members first: a post in a cluster of this item from the
-- market's own run, dated in the 7 days, ranks before every other post (tier 0; a member only of the pooled run
-- or of a cluster outside the window is context). Within a tier, measured lanes (unbiased_rank, panel) first, then
-- engagement, then the post id, which is unique, so no tie is left to the engine. At most 2 per creator, chosen
-- tier first, and at most @outlet_cap outlet posts (the news platform, or a platform:handle key in @outlet_keys;
-- a post whose creator has no creators row is not an outlet). 12 posts in all. The counts after each stage ride on
-- every row, and on one row with no post id when the caps leave nothing, so a hold can name its cause. A post seen
-- only by the brief's confirm search enters only when local by the market scope rule (market_scope.sql
-- creator_ranked): located in the market or sighted in its feeds in the window. sponsored is the enrich model's
-- paid marker or the vendor's paid label; sponsor_checked is true when either gave a reading.
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
members AS (
  SELECT mb.post_id, MAX(mb.probability) membership_probability
  FROM {core}.cluster_members mb
  JOIN {core}.clusters k ON k.cluster_id = mb.cluster_id
  WHERE k.item_id = @item_id AND UPPER(k.market) = @market
    AND k.cluster_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  GROUP BY mb.post_id),
pan_members AS (
  SELECT mb.post_id
  FROM {core}.cluster_members mb
  JOIN {core}.clusters k ON k.cluster_id = mb.cluster_id
  WHERE k.item_id = @item_id AND LOWER(k.market) = 'pan'
    AND k.cluster_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
  GROUP BY mb.post_id),
r AS (
  SELECT ps.post_id, ps.platform, ps.url, ps.published_at, ps.creator_tier_at_post creator_tier,
    ps.geo_market, ps.geo_confidence, ps.geo_source,
    source_market_by_post.source_market,
    IFNULL(NULLIF(ps.text, ''), ps.transcript) quote_text, ps.hashtags,
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
    mm.post_id IS NOT NULL market_member, pm.post_id IS NOT NULL pan_member, mm.membership_probability,
    -- An outlet is the news platform or a platform:handle key in the registry, written as core/api/store.py
    -- creator_key writes it: x for twitter, the handle trimmed, without a leading @ or u/, lower case. A handle the
    -- creators table does not hold gives a NULL key, which reads as not an outlet (never as NULL, which the cap
    -- would remove).
    IFNULL(ps.platform = 'news' OR CONCAT(IF(LOWER(TRIM(ps.platform)) = 'twitter', 'x', LOWER(TRIM(ps.platform))), ':',
        LOWER(REGEXP_REPLACE(TRIM(cr.handle), r'^@*(u/)?', ''))) IN UNNEST(@outlet_keys), FALSE) is_outlet,
    (ps.geo_market = @market AND IFNULL(ps.geo_confidence, 0) >= 0.7
       AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'))
      OR (NOT IFNULL(NULLIF(ps.geo_market, '') != @market AND ps.geo_confidence >= 0.7
                     AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)
          AND source_market_by_post.source_market = @market) local_flag,
    NOT IFNULL(NULLIF(ps.geo_market, '') != @market AND ps.geo_confidence >= 0.7
               AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE) showable_flag,
    ROW_NUMBER() OVER (PARTITION BY IFNULL(ps.creator_id, ps.post_id)
                       ORDER BY mm.post_id IS NULL, s.measured DESC, IFNULL(ps.engagement, 0) DESC, ps.post_id) creator_rank
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
  LEFT JOIN members mm ON mm.post_id = s.post_id
  LEFT JOIN pan_members pm ON pm.post_id = s.post_id
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
      )))),
c AS (
  SELECT r.*,
    ROW_NUMBER() OVER (PARTITION BY r.is_outlet
                       ORDER BY r.market_member DESC, r.measured DESC, r.eng DESC, r.post_id) class_rank,
    ROW_NUMBER() OVER (ORDER BY r.market_member DESC, r.measured DESC, r.eng DESC, r.post_id) pack_rank
  FROM r WHERE r.creator_rank <= 2),
f AS (
  SELECT c.* FROM c WHERE NOT c.is_outlet OR c.class_rank <= @outlet_cap),
-- What the outlet cap left for the stage counts: an outlet it removed still counts when 12 or more posts the cap kept
-- rank ahead of it, since it was never going to be in the pack. An outlet it removed from inside the first 12 places
-- does not count: that is the cap's doing. A cap of 12 therefore changes neither the pack nor any stage count.
g AS (
  SELECT c.* FROM c
  WHERE NOT c.is_outlet OR c.class_rank <= @outlet_cap
    OR (SELECT COUNT(*) FROM f WHERE f.pack_rank < c.pack_rank) >= 12)
SELECT f.* EXCEPT (enrich_sponsored, enrich_read, vendor_paid),
  f.enrich_sponsored OR IFNULL(f.vendor_paid, FALSE) sponsored,
  f.enrich_read OR f.vendor_paid IS NOT NULL sponsor_checked,
  (SELECT COUNT(*) FROM r) available_posts,
  (SELECT COUNTIF(r.market_member) FROM r) available_members,
  (SELECT COUNTIF(r.showable_flag) FROM r) available_showable,
  (SELECT COUNTIF(r.local_flag) FROM r) available_local,
  (SELECT COUNT(*) FROM c) after_creator_cap,
  (SELECT COUNTIF(c.showable_flag) FROM c) after_creator_cap_showable,
  (SELECT COUNTIF(c.local_flag) FROM c) after_creator_cap_local,
  (SELECT COUNT(*) FROM g) after_outlet_cap,
  (SELECT COUNTIF(g.showable_flag) FROM g) after_outlet_cap_showable,
  (SELECT COUNTIF(g.local_flag) FROM g) after_outlet_cap_local
FROM (SELECT 1 one) base
LEFT JOIN f ON TRUE
ORDER BY f.market_member DESC, f.measured DESC, f.eng DESC, f.post_id
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

-- name: sparkline
-- The main series' last 14 days; days with no row (before the series started) are filled in by evidence.py.
SELECT sd.day, sd.value, sd.lane_class FROM {core}.v_series_daily sd
WHERE sd.series_id = @series_id AND sd.day BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d
ORDER BY sd.day;

-- name: first_seen
-- First day 42 saw the item in the market, legacy memory included; agent_live never enters item_daily.
SELECT MIN(i.metric_date) first_seen FROM {core}.v_item_daily_current i
WHERE i.item_id = @item_id AND i.market = @market AND i.metric_date <= @d;
