-- The news to social bridge, BUILD.md task 2.5 (SOURCES.md "News to social bridge"), in BigQuery Standard SQL.
-- {core} and {agent} are the dataset names and {news_outlets} the lower-cased hub handles of kind news in
-- core/config/hubs.yaml (sqlrun.news_outlet_handles). core/detect/job.py applies the four views, in file order,
-- after sql/views.sql through sqlrun.apply_news, as a step whose failure never stops detect. They only read.
--
-- GDELT and local headline rows share this seed_queue shape: search/multi, ttl_days 3, lane expansion or
-- exploration, credits_estimate set, both yields NULL and an item_id. seed_queue has no source marker or source
-- date. seed_date is the queue date only; do not infer source, news_day or origin from it.
-- Detect's own seeds carry ttl_days 1, anchors lane anchor, placebo lane placebo and collect's yield rows ttl_days
-- 0 with a yield set, so those rows are excluded from these candidate news seeds.
--
-- Fold: lower case, every character that is not a letter or digit removed. A seed's query is its canonical key
-- (items.canonical_key: NFKC, casefolded, whitespace collapsed) and a hashtag key is NFKC and casefolded too, so
-- "load shedding", #LoadShedding and #load_shedding all fold to loadshedding. Labels are folded the same way
-- but are not casefolded upstream, so a label whose casefold differs from its lower case (the German sharp s)
-- does not match; the match only misses, it never joins two different words. Only exact folded matches count.

-- v_news_bridge: one row per candidate seed and each social item it names. matched_by: seed (the seed's own item,
-- always present), hashtag_key (a hashtag whose canonical key folds to the query), label (a cultural_map item
-- whose label folds to it, cluster topics and topics included) or cluster_keyword (the item of an L3 cluster in
-- the seed's market or 'pan' with a keyword that folds to it). An item matched more than one way takes the
-- first of hashtag_key, label, cluster_keyword.
CREATE OR REPLACE VIEW {agent}.v_news_bridge AS
WITH gs AS (
  SELECT q.seed_date, q.market, q.item_id seed_item_id, ANY_VALUE(q.query) query, ANY_VALUE(q.kind) kind,
    MIN(q.lane) lane, MAX(q.priority) rise_score
  FROM {core}.seed_queue q
  WHERE q.template = 'search/multi' AND q.ttl_days = 3 AND q.lane IN ('expansion', 'exploration')
    AND q.credits_estimate IS NOT NULL AND q.yield_posts IS NULL AND q.yield_new_creators IS NULL
    AND q.item_id IS NOT NULL
  GROUP BY q.seed_date, q.market, q.item_id),
gf AS (
  SELECT gs.*, REGEXP_REPLACE(LOWER(gs.query), r'[^\p{L}\p{N}]', '') folded FROM gs),
ky AS (                     -- every folded name a social item answers to; market NULL means any market
  SELECT c.item_id, CAST(NULL AS STRING) market, 1 rnk,
    REGEXP_REPLACE(LOWER(c.canonical_key), r'[^\p{L}\p{N}]', '') folded
  FROM {core}.cultural_map c WHERE c.valid_to IS NULL AND c.kind = 'hashtag'
  UNION ALL
  SELECT c.item_id, CAST(NULL AS STRING) market, 2 rnk, REGEXP_REPLACE(LOWER(c.label), r'[^\p{L}\p{N}]', '') folded
  FROM {core}.cultural_map c WHERE c.valid_to IS NULL
  UNION ALL
  SELECT k.item_id, UPPER(k.market) market, 3 rnk, REGEXP_REPLACE(LOWER(kw), r'[^\p{L}\p{N}]', '') folded
  FROM {core}.clusters k, UNNEST(k.keywords) kw WHERE k.item_id IS NOT NULL),
mt AS (
  SELECT gf.seed_date, gf.market, gf.seed_item_id, ky.item_id, MIN(ky.rnk) rnk
  FROM gf JOIN ky ON ky.folded = gf.folded AND gf.folded != '' AND IFNULL(ky.market, gf.market) IN (gf.market, 'PAN')
  WHERE ky.item_id != gf.seed_item_id
  GROUP BY gf.seed_date, gf.market, gf.seed_item_id, ky.item_id)
SELECT gf.seed_date, gf.market, gf.seed_item_id, gf.query, gf.kind, gf.lane, gf.rise_score,
  gf.seed_item_id item_id, 'seed' matched_by
FROM gf
UNION ALL
SELECT gf.seed_date, gf.market, gf.seed_item_id, gf.query, gf.kind, gf.lane, gf.rise_score, mt.item_id,
  CASE mt.rnk WHEN 1 THEN 'hashtag_key' WHEN 2 THEN 'label' ELSE 'cluster_keyword' END matched_by
FROM gf JOIN mt ON mt.seed_date = gf.seed_date AND mt.market = gf.market AND mt.seed_item_id = gf.seed_item_id;

-- v_item_first_sighting: each item's first measured social sighting per market. Measured: lane_class
-- unbiased_rank or panel, lane never placebo or agent_live (the v_item_spread rule); search_presence and
-- watchlist sightings date when 42 searched (DATA.md section 3.2). A news outlet's post is never the social
-- sighting: a post on platform news, or one whose creator_id, lower-cased, is a hubs.yaml handle of kind news
-- (collect writes the author's username as creator_id). A hub outlet read through an id rather than its
-- username is not caught.
-- after_collection_began: one of the series that saw the item on that first day had a collection_health row on
-- an earlier day in the market, so 42 was already measuring there and had not seen it. False when every series
-- began that day or has no collection record: the sighting may be older than 42's measurement of it.
CREATE OR REPLACE VIEW {agent}.v_item_first_sighting AS
WITH so AS (
  SELECT pi.item_id, po.market, po.observed_date, po.series
  FROM {core}.post_observations po
  JOIN {core}.post_items pi ON pi.post_id = po.post_id
  LEFT JOIN {core}.posts ps ON ps.post_id = po.post_id
  WHERE po.lane_class IN ('unbiased_rank', 'panel') AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
    AND IFNULL(ps.platform, '') != 'news' AND IFNULL(po.platform, '') != 'news'
    AND LOWER(IFNULL(ps.creator_id, '')) NOT IN UNNEST({news_outlets})),
fd AS (
  SELECT so.item_id, so.market, MIN(so.observed_date) first_measured FROM so GROUP BY so.item_id, so.market),
ss AS (
  SELECT h.market, h.series, MIN(h.day) since FROM {core}.collection_health h GROUP BY h.market, h.series)
SELECT fd.item_id, fd.market, fd.first_measured,
  IFNULL(LOGICAL_OR(ss.since < fd.first_measured), FALSE) after_collection_began
FROM fd
JOIN so ON so.item_id = fd.item_id AND so.market = fd.market AND so.observed_date = fd.first_measured
LEFT JOIN ss ON ss.market = so.market AND ss.series = so.series
GROUP BY fd.item_id, fd.market, fd.first_measured;

-- v_news_followthrough: one row per candidate seed (queue date, market, seed item), over the seed item and every item
-- v_news_bridge matches to it (matched_items).
-- collect_ran: collect appended a yield row for the seed's item in its market and lane on one of the seed's 3
-- live days (seed day to seed day + 2); collect's yield rows carry the run day as seed_date. yield_posts and
-- yield_new_creators are the largest over those runs, as detect's read_trials reads them.
-- states_7d: each distinct 42 state (item_state.state, good detect runs only) held in the market from seed_date
-- through six days after it, in the order first held; first_state_day is the first such metric date.
-- first_measured is the earliest actual first sighting (v_item_first_sighting) of a matched item in the market.
-- news_day, news_day_from and lag_days stay NULL until the producer supplies a source marker and source date.
CREATE OR REPLACE VIEW {agent}.v_news_followthrough AS
WITH gs AS (
  SELECT DISTINCT b.seed_date, b.market, b.seed_item_id, b.query, b.kind, b.lane, b.rise_score
  FROM {agent}.v_news_bridge b),
mi AS (
  SELECT b.seed_date, b.market, b.seed_item_id,
    ARRAY_AGG(STRUCT(b.item_id AS item_id, cm.label AS label, b.matched_by AS matched_by)
      ORDER BY b.matched_by, b.item_id) matched_items
  FROM {agent}.v_news_bridge b
  LEFT JOIN (SELECT c.item_id, ANY_VALUE(c.label) label FROM {core}.cultural_map c WHERE c.valid_to IS NULL
             GROUP BY c.item_id) cm ON cm.item_id = b.item_id
  GROUP BY b.seed_date, b.market, b.seed_item_id),
yl AS (
  SELECT gs.seed_date, gs.market, gs.seed_item_id, COUNT(DISTINCT y.seed_date) collect_runs,
    MAX(y.yield_posts) yield_posts, MAX(y.yield_new_creators) yield_new_creators
  FROM gs JOIN {core}.seed_queue y
    ON y.market = gs.market AND y.item_id = gs.seed_item_id AND y.lane = gs.lane
   AND y.seed_date BETWEEN gs.seed_date AND DATE_ADD(gs.seed_date, INTERVAL 2 DAY)
   AND (y.yield_posts IS NOT NULL OR y.yield_new_creators IS NOT NULL)
  GROUP BY gs.seed_date, gs.market, gs.seed_item_id),
sd AS (                     -- each state a matched item held in the window, dated by its first day
  SELECT b.seed_date, b.market, b.seed_item_id, s.state, MIN(s.metric_date) first_day
  FROM {agent}.v_news_bridge b JOIN {core}.v_item_state_current s
    ON s.item_id = b.item_id AND s.market = b.market AND s.state IS NOT NULL
   AND s.metric_date BETWEEN b.seed_date AND DATE_ADD(b.seed_date, INTERVAL 6 DAY)
  GROUP BY b.seed_date, b.market, b.seed_item_id, s.state),
st AS (
  SELECT sd.seed_date, sd.market, sd.seed_item_id, ARRAY_AGG(sd.state ORDER BY sd.first_day, sd.state) states,
    MIN(sd.first_day) first_state_day
  FROM sd GROUP BY sd.seed_date, sd.market, sd.seed_item_id),
fm AS (
  SELECT b.seed_date, b.market, b.seed_item_id, MIN(f.first_measured) first_measured
  FROM {agent}.v_news_bridge b
  JOIN {agent}.v_item_first_sighting f ON f.item_id = b.item_id AND f.market = b.market
  GROUP BY b.seed_date, b.market, b.seed_item_id),
cm AS (
  SELECT c.item_id, ANY_VALUE(c.label) label FROM {core}.cultural_map c WHERE c.valid_to IS NULL
  GROUP BY c.item_id)
SELECT gs.market, CAST(NULL AS DATE) news_day, CAST(NULL AS DATE) news_day_from,
  gs.seed_date, gs.seed_item_id item_id, COALESCE(cm.label, gs.query) label, gs.kind, gs.lane, gs.rise_score,
  mi.matched_items,
  IFNULL(yl.collect_runs, 0) > 0 collect_ran, IFNULL(yl.collect_runs, 0) collect_runs,
  yl.yield_posts, yl.yield_new_creators,
  st.first_state_day IS NOT NULL reached_state, IFNULL(st.states, []) states_7d, st.first_state_day,
  fm.first_measured,
  CAST(NULL AS INT64) lag_days
FROM gs
JOIN mi ON mi.seed_date = gs.seed_date AND mi.market = gs.market AND mi.seed_item_id = gs.seed_item_id
LEFT JOIN yl ON yl.seed_date = gs.seed_date AND yl.market = gs.market AND yl.seed_item_id = gs.seed_item_id
LEFT JOIN st ON st.seed_date = gs.seed_date AND st.market = gs.market AND st.seed_item_id = gs.seed_item_id
LEFT JOIN fm ON fm.seed_date = gs.seed_date AND fm.market = gs.market AND fm.seed_item_id = gs.seed_item_id
LEFT JOIN cm ON cm.item_id = gs.seed_item_id;

-- v_item_origin: one row per measured item and market. first_measured, after_collection_began and first_state_day
-- are measured facts. origin, lead_news_day and lag_days stay NULL until seed_queue records a source marker and
-- source date; coverage and nameability alone do not establish native origin.
CREATE OR REPLACE VIEW {agent}.v_item_origin AS
WITH fs AS (
  SELECT f.item_id, f.market, f.first_measured, f.after_collection_began FROM {agent}.v_item_first_sighting f),
fr AS (
  SELECT s.item_id, s.market, MIN(s.metric_date) first_state_day
  FROM {core}.v_item_state_current s WHERE s.state IS NOT NULL GROUP BY s.item_id, s.market),
im AS (
  SELECT fs.item_id, fs.market FROM fs
  UNION DISTINCT
  SELECT fr.item_id, fr.market FROM fr)
SELECT im.item_id, im.market,
  CAST(NULL AS STRING) origin,
  fs.first_measured, IFNULL(fs.after_collection_began, FALSE) after_collection_began, fr.first_state_day,
  CAST(NULL AS DATE) lead_news_day, CAST(NULL AS INT64) lag_days
FROM im
LEFT JOIN fs ON fs.item_id = im.item_id AND fs.market = im.market
LEFT JOIN fr ON fr.item_id = im.item_id AND fr.market = im.market;
