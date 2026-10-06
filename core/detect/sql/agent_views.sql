-- Read-only views lane L4's API reads from L2 (core/api/contract.md sections 10.7 and 12.1, DATA.md section 7),
-- in BigQuery Standard SQL. {core} and {agent} are the dataset names; in v_sensitive_items the
-- sensitive_terms, never_sensitive and list_version placeholders become the term list of
-- core/brief/political.yaml and core/detect/sensitive.yaml, its never list, and a hash of both. sqlrun.py
-- renders them and applies each statement after views.sql. Nothing here writes.

-- The gate result per item from the latest good brief per market and date. place: cards and more are 'today',
-- held_back.items carry their rule and reason, and seasonal items the gate moved to moments are 'moments'.
-- Calendar moments are left out: their item_ids come from the calendar, not from the gate.
CREATE OR REPLACE VIEW {agent}.v_item_gate_current AS
WITH b AS (SELECT bc.brief_date, bc.market, bc.payload FROM {agent}.v_briefs_current bc)
SELECT JSON_VALUE(c, '$.item_id') item_id, b.market, b.brief_date,
  CAST(NULL AS STRING) rule, CAST(NULL AS STRING) reason, CAST(NULL AS STRING) reason_text, 'today' place
FROM b CROSS JOIN UNNEST(JSON_QUERY_ARRAY(b.payload, '$.cards')) c
UNION ALL
SELECT JSON_VALUE(c, '$.item_id'), b.market, b.brief_date,
  CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), 'today'
FROM b CROSS JOIN UNNEST(JSON_QUERY_ARRAY(b.payload, '$.more')) c
UNION ALL
SELECT JSON_VALUE(h, '$.item_id'), b.market, b.brief_date,
  JSON_VALUE(h, '$.rule'), JSON_VALUE(h, '$.reason'), JSON_VALUE(h, '$.reason_text'), 'held_back'
FROM b CROSS JOIN UNNEST(JSON_QUERY_ARRAY(b.payload, '$.held_back.items')) h
UNION ALL
SELECT mi, b.market, b.brief_date,
  CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), 'moments'
FROM b CROSS JOIN UNNEST(JSON_QUERY_ARRAY(b.payload, '$.moments')) m
CROSS JOIN UNNEST(JSON_VALUE_ARRAY(m, '$.item_ids')) mi
WHERE JSON_VALUE(m, '$.kind') = 'seasonal_item';

-- One row per post, item and market the post was sighted in, leaving out the legacy, placebo and agent_live
-- sightings. The excerpt is the caption, else the transcript, cut to 280 characters; engagement is the posts
-- row's latest reading; clip_uri is a stored clip, where one was kept. creators.coord_score is never read.
CREATE OR REPLACE VIEW {agent}.v_item_evidence AS
WITH seen AS (
  SELECT po.post_id, po.market, MIN(po.observed_at) first_seen_at
  FROM {core}.post_observations po
  WHERE po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
  GROUP BY po.post_id, po.market),
clip AS (
  SELECT md.post_id, MIN(md.gcs_uri) clip_uri FROM {core}.media md WHERE md.kind = 'clip' GROUP BY md.post_id),
linked AS (SELECT DISTINCT pi.post_id, pi.item_id FROM {core}.post_items pi)
SELECT ps.post_id, l.item_id, s.market, ps.platform, ps.url, cr.handle, ps.creator_tier_at_post creator_tier,
  SUBSTR(COALESCE(NULLIF(ps.text, ''), ps.transcript), 1, 280) excerpt,
  ps.views, ps.likes, ps.comments, ps.shares, ps.engagement, ps.thumbnail_url, ps.duration_s,
  cl.clip_uri, ps.published_at, s.first_seen_at
FROM linked l
JOIN seen s ON s.post_id = l.post_id
JOIN {core}.posts ps ON ps.post_id = l.post_id
LEFT JOIN (SELECT c.creator_id, c.handle FROM {core}.creators c) cr ON cr.creator_id = ps.creator_id
LEFT JOIN clip cl ON cl.post_id = l.post_id;

-- An item's series in one market, one row per series and day over the last days days, ending on the item's
-- latest day in v_series_daily. A day with no value is a NULL row, a gap, never a zero: an invalid
-- collection day, or a day before the series' first read. Columns are always qualified, because the
-- parameters share names with columns; arg reads the parameters where no column can shadow them.
CREATE OR REPLACE TABLE FUNCTION {agent}.tvf_item_timeseries(item_id STRING, market STRING, days INT64) AS
WITH arg AS (SELECT item_id AS a_item, market AS a_market, days AS a_days),
s AS (
  SELECT sd.series_id, sd.item_id, sd.market, sd.platform, sd.series, sd.protocol, sd.lane_class,
    sd.day, sd.value, sd.trials
  FROM {core}.v_series_daily sd
  JOIN arg ON sd.item_id = arg.a_item AND sd.market = arg.a_market),
e AS (SELECT MAX(s.day) end_day FROM s),
ser AS (
  SELECT DISTINCT s.series_id, s.item_id, s.market, s.platform, s.series, s.protocol, s.lane_class FROM s)
SELECT ser.series_id, ser.item_id, ser.market, ser.platform, ser.series, ser.protocol, ser.lane_class,
  cal_day AS day, s.value, s.trials
FROM ser
CROSS JOIN e
CROSS JOIN arg
CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(e.end_day, INTERVAL arg.a_days - 1 DAY), e.end_day)) cal_day
LEFT JOIN s ON s.series_id = ser.series_id AND s.day = cal_day;

-- Items in the sensitive set (contract section 12.1, rule 2): the current cultural_map row's label, an alias
-- or its canonical key matches a term. Labels and aliases match as whole words, as the G4b rules of
-- core/brief/gatectx.py do. A key is a lowercase compound, so it matches when it is the term or holds it
-- among its words; a term's key_rule says how much further it reaches. 'label' never reads the key (PrEP,
-- not #prep). 'keys' never reads a label or alias, only the key and its words (tik, not TikTok). 'words' goes
-- no further (sti, not stickers). 'starts' also takes the term at the start of the key
-- (eidmubarak, not apartheid). 'g4b' also takes a term of five letters or more anywhere. 'edges' is g4b plus
-- a term of three or four letters at the start or end of the key. 'inside' takes the term anywhere.
-- sensitive.yaml says which terms get which rule. Its never list is checked first: an item whose key, letters
-- only, is on it is never in the set. A term gives way, losing its reason for that item, only to a longer
-- term that sensitive.yaml's gives_way_to names for it, that also matched the item, and inside which every
-- occurrence of the term sits (counted over the key and each name, letters only), so TB Joshua reads
-- religion and not health but #ProtestantChurchProtest keeps political. A term also gives way, the same way,
-- to any phrase of sensitive.yaml's ordinary list that holds it (tik inside TikTok, killing inside killing
-- it); ordinary is never a reason. Otherwise an item keeps every reason its terms carry. A term with no
-- letters (419) is matched on its digits. One row per item and reason (political, religion, health,
-- sex_life, race_ethnicity or crime), each stamped with list_version, the hash of the lists
-- it was built from.
-- A floor, not a complete list: it hides obvious cases; anything it misses is not safe to show next to a
-- named person.
CREATE OR REPLACE VIEW {core}.v_sensitive_items AS
WITH terms AS (
  SELECT t.reason, t.term, t.key_rule, t.pattern, t.squashed, t.gives_way FROM UNNEST({sensitive_terms}) t),
named AS (
  SELECT c.item_id, c.canonical_key, ARRAY_CONCAT([IFNULL(c.label, '')], IFNULL(c.aliases, [])) raw_names
  FROM {core}.cultural_map c WHERE c.valid_to IS NULL),
cm AS (   -- each name as written (for a term like PrEP) and, when it has no spaces, split where its capitals
  SELECT n.item_id,       -- change: #VoteANC reads as Vote ANC, while KwaZulu-Natal floods stays whole
    ARRAY_CONCAT(n.raw_names, ARRAY(
      SELECT REGEXP_REPLACE(REGEXP_REPLACE(r, r'(\p{Ll})(\p{Lu})', r'\1 \2'), r'(\p{Lu})(\p{Lu}\p{Ll})', r'\1 \2')
      FROM UNNEST(n.raw_names) r WHERE NOT REGEXP_CONTAINS(r, r'\s'))) names,
    REGEXP_REPLACE(LOWER(IFNULL(n.canonical_key, '')), r'\P{L}', '') key_squashed,
    ARRAY_TO_STRING(REGEXP_EXTRACT_ALL(LOWER(IFNULL(n.canonical_key, '')), r'\p{L}+'), ' ') key_words,
    CONCAT(ARRAY_TO_STRING(ARRAY(          -- each name on its own, so two names never join into one word
             SELECT REGEXP_REPLACE(LOWER(x), r'\P{L}', '')
             FROM UNNEST(ARRAY_CONCAT([IFNULL(n.canonical_key, '')], n.raw_names)) x), '|'),
           ' ', REGEXP_REPLACE(ARRAY_TO_STRING(n.raw_names, ' '), r'[^0-9]', '')) letters
  FROM named n),
hits AS (       -- every rule needs the term's letters (or a digit term's digits) in a row, so pair on that first
  SELECT DISTINCT cm.item_id, t.reason, t.squashed, t.gives_way, cm.letters
  FROM cm JOIN terms t ON STRPOS(cm.letters, t.squashed) > 0
  WHERE cm.key_squashed NOT IN UNNEST({never_sensitive})
  AND ((t.key_rule != 'keys' AND EXISTS (SELECT 1 FROM UNNEST(cm.names) n WHERE REGEXP_CONTAINS(n, t.pattern)))
    OR (t.key_rule != 'label' AND cm.key_squashed = t.squashed)
    OR (t.key_rule != 'label' AND REGEXP_CONTAINS(cm.key_words, t.pattern))
    OR (t.key_rule = 'starts' AND CHAR_LENGTH(t.squashed) >= 3 AND STARTS_WITH(cm.key_squashed, t.squashed))
    OR (t.key_rule IN ('g4b', 'edges') AND CHAR_LENGTH(t.squashed) >= 5
        AND STRPOS(cm.key_squashed, t.squashed) > 0)
    OR (t.key_rule = 'inside' AND CHAR_LENGTH(t.squashed) >= 3 AND STRPOS(cm.key_squashed, t.squashed) > 0)
    OR (t.key_rule = 'edges' AND CHAR_LENGTH(t.squashed) >= 3
        AND (STARTS_WITH(cm.key_squashed, t.squashed) OR ENDS_WITH(cm.key_squashed, t.squashed)))))
SELECT DISTINCT h.* EXCEPT (squashed, gives_way, letters), {list_version} list_version
FROM hits h
LEFT JOIN hits l ON l.item_id = h.item_id
  AND (STRPOS(h.gives_way, CONCAT('|', l.squashed, '|')) > 0
       OR (l.reason = 'ordinary' AND CHAR_LENGTH(l.squashed) > CHAR_LENGTH(h.squashed)
           AND STRPOS(l.squashed, h.squashed) > 0))
  -- times h occurs in the text = times l occurs x times h occurs in l, both sides multiplied by both lengths
  AND (CHAR_LENGTH(h.letters) - CHAR_LENGTH(REPLACE(h.letters, h.squashed, ''))) * CHAR_LENGTH(l.squashed)
    = (CHAR_LENGTH(h.letters) - CHAR_LENGTH(REPLACE(h.letters, l.squashed, '')))
      * (CHAR_LENGTH(l.squashed) - CHAR_LENGTH(REPLACE(l.squashed, h.squashed, '')))
WHERE l.item_id IS NULL AND h.reason != 'ordinary';

-- An item's tone per market and day (FEATURES 16, the tone_flip watch rule of contract 10.5), from the
-- enrichment tone of task 2.1 (core/understand/enrich.py). Tone there is a label, not a score, so each label
-- reads as a sign: celebratory, hopeful and humorous +1; angry, sad and sarcastic -1 (sarcastic is mock praise,
-- enrich.py's prompt); informative, neutral and mixed 0. tone is the mean of those signs over the item's
-- enriched posts dated that day (posts.post_date) and sighted in that market outside the legacy, placebo and
-- agent_live lanes, so it runs from -1 to 1 and its sign says whether positive or negative posts outnumber the
-- other. A mean rather than a majority label, because two of the nine labels say nothing about sign and a
-- majority of them would hide a clear lean in the rest. enriched_posts counts those posts, each once however
-- many routes link it to the item; tone is NULL under 5 of them, the floor tone_by_language.sql (FEATURES 27)
-- already uses for a tone it will describe. A post enriched twice by a race keeps the first tone in label order,
-- the pick tone_by_language.sql makes. Posts not enriched yet do not count.
CREATE OR REPLACE VIEW {agent}.v_item_tone_daily AS
WITH enriched AS (
  SELECT e.post_id, e.tone
  FROM {core}.post_enrichment e
  WHERE e.tone IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.post_id ORDER BY e.tone) = 1),
seen AS (
  SELECT DISTINCT po.post_id, po.market
  FROM {core}.post_observations po
  WHERE po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')),
linked AS (SELECT DISTINCT pi.post_id, pi.item_id FROM {core}.post_items pi),
signed AS (
  SELECT ps.post_date metric_date, s.market, l.item_id, l.post_id,
    CASE WHEN en.tone IN ('celebratory', 'hopeful', 'humorous') THEN 1
         WHEN en.tone IN ('angry', 'sad', 'sarcastic') THEN -1 ELSE 0 END sign
  FROM linked l
  JOIN seen s ON s.post_id = l.post_id
  JOIN enriched en ON en.post_id = l.post_id
  JOIN {core}.posts ps ON ps.post_id = l.post_id)
SELECT g.metric_date, g.market, g.item_id, COUNT(*) enriched_posts,
  IF(COUNT(*) >= 5, AVG(g.sign), NULL) tone
FROM signed g
GROUP BY g.metric_date, g.market, g.item_id;

-- The complete sensitive set (contract section 12.1 rule 2, L4 Needs 19): every row of v_sensitive_items, the
-- keyword floor, plus the items the understand job's model reading marks. post_enrichment.sensitive holds the
-- reasons the model gave a post, or 'none'; a row written before that field, an embedding row or an empty array
-- is a post the model has not read for this and counts nowhere. post_enrichment can hold several rows for a post
-- and records no write time, so each post is taken once with every reason any of its rows carries, and each
-- post_items link once. An item takes a category when at least {model_min_posts} of its read posts carry the
-- reason, or at least {model_share} of them do. model_categories maps each model reason to its category;
-- religious_holiday is in it only once Albert has said those holidays count as religion (sqlrun.py
-- RELIGIOUS_HOLIDAYS_ARE_RELIGION). The model can only add items: nothing here takes a floor row away. market is
-- NULL: a sensitive item is kept off named pages in every market. sqlrun.py builds this view only when the
-- detect job's SENSITIVE_COMPLETE_READY is set, because f42-api switches named item lists on once it exists.
CREATE OR REPLACE VIEW {core}.v_sensitive_items_complete AS
WITH cats AS (
  SELECT m.reason, m.category FROM UNNEST({model_categories}) m),
read_post AS (      -- each post the model read for the sensitive set, once, with every reason its rows carry
  SELECT DISTINCT pe.post_id, r reason
  FROM {core}.post_enrichment pe CROSS JOIN UNNEST(pe.sensitive) r
  WHERE r IS NOT NULL),
link AS (
  SELECT DISTINCT pi.item_id, pi.post_id FROM {core}.post_items pi
  WHERE pi.post_id IN (SELECT rp.post_id FROM read_post rp)),
item_read AS (
  SELECT l.item_id, COUNT(*) read_posts FROM link l GROUP BY l.item_id),
item_reason AS (
  SELECT l.item_id, c.category, COUNT(DISTINCT l.post_id) posts
  FROM link l
  JOIN read_post rp ON rp.post_id = l.post_id
  JOIN cats c ON c.reason = rp.reason
  GROUP BY l.item_id, c.category)
SELECT k.item_id, CAST(NULL AS STRING) market, k.reason category, 'keyword' source
FROM {core}.v_sensitive_items k
UNION DISTINCT
SELECT ir.item_id, CAST(NULL AS STRING) market, ir.category, 'model' source
FROM item_reason ir
JOIN item_read rd ON rd.item_id = ir.item_id
WHERE ir.posts >= {model_min_posts} OR ir.posts >= {model_share} * rd.read_posts;
