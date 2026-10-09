CREATE SCHEMA IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core`
OPTIONS (location = "US");

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.raw_responses` (
  run_id STRING NOT NULL, job STRING, market STRING, route STRING, params_hash STRING,
  lane STRING, seed_key STRING, fetched_at TIMESTAMP NOT NULL, http_status INT64,
  credits_quoted FLOAT64, credits_charged FLOAT64, cache_hit BOOL, body JSON)
PARTITION BY DATE(fetched_at) CLUSTER BY route, market;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.posts` (
  post_id STRING NOT NULL,
  platform STRING, native_id STRING, url STRING, creator_id STRING,
  creator_tier_at_post STRING,
  text STRING, transcript STRING, hashtags ARRAY<STRING>, sound_id STRING,
  thumbnail_url STRING, duration_s FLOAT64,
  published_at TIMESTAMP,
  post_date DATE NOT NULL,
  views INT64, likes INT64, comments INT64, shares INT64, engagement INT64,
  geo_market STRING, geo_confidence FLOAT64,
  geo_source STRING,
  geo_scope STRING, vendor STRING, endpoint STRING, source_regime STRING, vendor_labels JSON, run_id STRING)
PARTITION BY post_date CLUSTER BY platform, geo_market, creator_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.post_observations` (
  post_id STRING NOT NULL, observed_at TIMESTAMP NOT NULL,
  observed_date DATE NOT NULL,
  market STRING NOT NULL, source_market STRING, source_region STRING,
  platform STRING, route STRING, series STRING, protocol STRING,
  lane STRING,
  lane_class STRING NOT NULL,
  seed_key STRING, pull_seq INT64, rank INT64,
  views INT64, likes INT64, comments INT64, shares INT64, run_id STRING)
PARTITION BY observed_date CLUSTER BY market, lane_class, post_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.item_counter_daily` (
  obs_date DATE NOT NULL,
  market STRING NOT NULL,
  platform STRING NOT NULL, item_id STRING NOT NULL,
  series STRING NOT NULL, route STRING, protocol STRING NOT NULL,
  is_board BOOL,
  lane_class STRING NOT NULL,
  unit STRING NOT NULL,
  pull_seq INT64,
  value FLOAT64,
  source STRING,
  observed_at TIMESTAMP, available_at TIMESTAMP, run_id STRING)
PARTITION BY obs_date CLUSTER BY market, item_id, series;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.collection_health` (
  day DATE NOT NULL, market STRING NOT NULL, platform STRING, route STRING,
  series STRING NOT NULL, protocol STRING NOT NULL, lane_class STRING,
  calls INT64, calls_ok INT64,
  units_planned INT64, units_ok INT64,
  items INT64,
  ref_items FLOAT64, ref_days INT64,
  k FLOAT64,
  valid BOOL, invalid_reason STRING,
  located_share FLOAT64,
  run_id STRING)
PARTITION BY day CLUSTER BY market, series;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.item_daily` (
  metric_date DATE NOT NULL,
  market STRING NOT NULL,
  platform STRING NOT NULL,
  item_id STRING NOT NULL,
  lane_class STRING NOT NULL,
  series STRING, protocol STRING,
  posts INT64, creators INT64, unflagged_creators INT64, engagement INT64,
  tier_posts STRUCT<nano INT64, micro INT64, mid INT64, macro INT64, mega INT64>,
  first_post_at TIMESTAMP,
  geo_known_posts INT64,
  local_posts INT64,
  source_regime STRING, available_at TIMESTAMP, run_id STRING, rule_version STRING)
PARTITION BY metric_date CLUSTER BY market, item_id, platform;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.series_test` (
  metric_date DATE NOT NULL, series_id STRING NOT NULL, item_id STRING, market STRING, platform STRING,
  series STRING, protocol STRING, lane_class STRING, kind STRING,
  y FLOAT64, trials INT64, obs_prior INT64, obs28 INT64, first_measured DATE,
  baseline_state STRING,
  hist_mean FLOAT64, med FLOAT64, v3 FLOAT64, v7 FLOAT64, peak28 FLOAT64, vel FLOAT64, accel FLOAT64,
  z_display FLOAT64,
  test STRING,
  mu FLOAT64, alpha FLOAT64, weekday_factor FLOAT64, mu_prior FLOAT64,
  ratio FLOAT64, p_mid FLOAT64, q FLOAT64, significant BOOL,
  run_id STRING, rule_version STRING)
PARTITION BY metric_date CLUSTER BY market, item_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.cultural_map` (
  item_id STRING NOT NULL,
  kind STRING NOT NULL,
  canonical_key STRING, label STRING, aliases ARRAY<STRING>, parent_item_id STRING,
  centroid ARRAY<FLOAT64>,
  first_seen DATE, first_seen_market STRING, first_seen_platform STRING, last_seen DATE,
  recurrences INT64, lifecycle STRING, status STRING, rejected_until DATE,
  valid_from TIMESTAMP, valid_to TIMESTAMP)
CLUSTER BY kind, item_id;

/* Once cultural_map has enough rows:
   CREATE VECTOR INDEX cm_c ON `ogilvy-trends-v2.intelligence_42_core.cultural_map`(centroid)
   OPTIONS(index_type='IVF', distance_type='COSINE') */

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.post_enrichment` (
  post_id STRING NOT NULL,
  embedding ARRAY<FLOAT64>, langs ARRAY<STRING>, code_switched BOOL, entities ARRAY<STRING>,
  sounds ARRAY<STRING>, formats ARRAY<STRING>, tone STRING, stance STRING, sponsored BOOL,
  near_dup_size INT64, screen_text STRING, video_notes STRING, sensitive ARRAY<STRING>);

/* The understand job writes sensitive from its model reading of each post (core/understand/enrich.py SENSITIVE),
   only once enrich.SENSITIVE_ENRICH_READY is set. A post_enrichment table made before the column existed gains it
   here; apply this before that flag is turned on. */
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.post_enrichment`
ADD COLUMN IF NOT EXISTS sensitive ARRAY<STRING>;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.post_items` (
  post_id STRING NOT NULL, item_id STRING NOT NULL, via STRING, linked_on DATE, link_market STRING)
PARTITION BY linked_on;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.creators` (
  creator_id STRING NOT NULL,
  platform STRING, handle STRING, followers INT64, tier STRING, account_created_at TIMESTAMP,
  home_market STRING, verified_region STRING, coord_score INT64,
  display_name STRING, verified BOOL, profile_location STRING, first_seen TIMESTAMP, last_seen TIMESTAMP);

/* The collect job fills creators from the author block of every post it parses (core/collect/writers.py).
   A creators table made before these columns existed gains them here; a fresh project gets them from the
   CREATE TABLE text above and these statements change nothing. */
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.creators`
ADD COLUMN IF NOT EXISTS display_name STRING;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.creators`
ADD COLUMN IF NOT EXISTS verified BOOL;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.creators`
ADD COLUMN IF NOT EXISTS profile_location STRING;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.creators`
ADD COLUMN IF NOT EXISTS first_seen TIMESTAMP;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.creators`
ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.media` (
  sha256 STRING NOT NULL, post_id STRING, gcs_uri STRING, kind STRING);

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.entities` (
  entity_id STRING NOT NULL, kind STRING, name STRING, aliases ARRAY<STRING>);

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.clusters` (
  cluster_date DATE NOT NULL, cluster_id STRING NOT NULL,
  market STRING, item_id STRING, match_kind STRING, label STRING,
  keywords ARRAY<STRING>, local_terms ARRAY<STRING>, centroid ARRAY<FLOAT64>)
PARTITION BY cluster_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.cluster_members` (
  cluster_id STRING NOT NULL, post_id STRING NOT NULL, probability FLOAT64);

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.item_state` (
  metric_date DATE NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL, kind STRING,
  state_raw STRING, state STRING, untested BOOL,
  main_series_id STRING, main_y FLOAT64, main_mu FLOAT64, main_ratio FLOAT64,
  q_min FLOAT64, sig_days3 INT64, creators3 INT64, posts3 INT64, top_creator_share3 FLOAT64,
  authenticity STRING, share_flags ARRAY<STRING>, sponsored_share FLOAT64, geo_status STRING,
  local_share FLOAT64, geo_known_posts7 INT64,
  spread_platforms INT64, found_platforms INT64, markets_hot INT64, lead_market STRING,
  diffusion STRING, novelty STRING,
  last_wave STRUCT<peak_date DATE, peak_posts INT64>, moment STRING, eligible BOOL, worth_raw FLOAT64,
  worth_pct FLOAT64,
  run_id STRING NOT NULL, rule_version STRING, base_state STRING,
  eligible_v1 BOOL, locality_basis STRING, locality_status STRING)
PARTITION BY metric_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.coord_signals` (
  metric_date DATE NOT NULL, item_id STRING NOT NULL, market STRING NOT NULL, run_id STRING NOT NULL,
  signal STRING, component_id STRING, accounts INT64, item_posts_share FLOAT64, rule_version STRING)
PARTITION BY metric_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.test_switch` (
  market STRING NOT NULL, platform STRING NOT NULL, lane_class STRING NOT NULL,
  switched_on DATE, backtest_run_id STRING, rule_version STRING)
PARTITION BY switched_on;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.calendar` (
  moment_date DATE NOT NULL, market STRING NOT NULL, name STRING NOT NULL,
  kind STRING, source STRING, item_ids ARRAY<STRING>)
PARTITION BY moment_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.seed_queue` (
  seed_date DATE NOT NULL, market STRING NOT NULL, item_id STRING, query STRING,
  kind STRING, lane STRING, priority FLOAT64, template STRING, ttl_days INT64,
  credits_estimate FLOAT64, yield_posts INT64, yield_new_creators INT64)
PARTITION BY seed_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.credit_ledger` (
  trend_date DATE NOT NULL, logged_at TIMESTAMP,
  run_id STRING, job STRING, lane STRING, agent STRING, market STRING, platform STRING,
  route STRING, endpoint STRING, params_hash STRING, item_id STRING, calls INT64,
  credits_quoted FLOAT64, credits_charged FLOAT64, cache_hit BOOL, posts_new INT64,
  balance_after FLOAT64)
PARTITION BY trend_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.calendar_analogues` (
  moment_date DATE NOT NULL, market STRING NOT NULL, name STRING NOT NULL,
  analogue_date DATE, match_kind STRING, window_start DATE, window_end DATE,
  status STRING NOT NULL, reason STRING, evidence JSON, query_text STRING, query_params JSON,
  computed_at TIMESTAMP NOT NULL)
PARTITION BY moment_date CLUSTER BY market;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.gdelt_daily` (
  day DATE NOT NULL, market STRING NOT NULL, entity_kind STRING NOT NULL, entity STRING NOT NULL,
  mentions INT64, computed_at TIMESTAMP, market_rule STRING)
PARTITION BY day CLUSTER BY market, entity_kind;

/* Rows written before the market rule was recorded keep market_rule NULL; a fresh project gets the
   column from the CREATE TABLE text above and this statement changes nothing. */
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.gdelt_daily`
ADD COLUMN IF NOT EXISTS market_rule STRING;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.breakout_signals` (
  metric_date DATE NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL, run_id STRING NOT NULL,
  creators INT64, posts INT64, evidence_post_ids ARRAY<STRING>, top_ratio FLOAT64,
  held_flagged INT64, rule_version STRING)
PARTITION BY metric_date CLUSTER BY market, item_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.item_hourly` (
  market STRING NOT NULL, item_id STRING NOT NULL, platform STRING NOT NULL,
  hour TIMESTAMP NOT NULL,
  posts INT64, creators INT64,
  lane_class STRING NOT NULL,
  run_id STRING NOT NULL)
PARTITION BY DATE(hour) CLUSTER BY market, item_id;

/* The hourly Breaking rule's output (core/detect/breaking.py, L2 Needs 34): one row per hour, market and item an ok
   breaking run judged Breaking. Append only; ratio stays NULL for an item with no prior posts (expected6 0). */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.breaking_signals` (
  hour TIMESTAMP NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL,
  posts6 INT64, creators6 INT64, expected6 FLOAT64, ratio FLOAT64, platforms INT64,
  run_id STRING NOT NULL, rule_version STRING)
PARTITION BY DATE(hour) CLUSTER BY market, item_id;

/* The suppression list (SETUP.md data protection). An operator suppresses a creator by creator_id, or by
   platform and handle, with a reason and who; status is suppressed or lifted. Append only: lifting a
   suppression appends a lifted row with the same suppression_id and a later status_at. The reason says why
   in plain words and never holds contact details. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.suppressions` (
  suppression_id STRING NOT NULL, status_at TIMESTAMP NOT NULL, status STRING NOT NULL,
  creator_id STRING, platform STRING, handle STRING, reason STRING, who STRING)
CLUSTER BY suppression_id;

/* The creators f42-api must not name (core/api/store.py suppressed_creators reads creator_id here). The newest
   row per suppression_id by status_at is the one that holds; a tie goes to the row that is not lifted, then to
   the row's own JSON text, so the view picks the same row on every read. Any status but lifted suppresses, so a
   mistyped status errs toward hiding the creator. A handle is matched to creators the way store.creator_key
   matches it: platform in lower case with twitter read as x, handle trimmed, lower case, without a leading @
   or u/. */
CREATE VIEW IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.v_suppressed_creators` AS
WITH s AS (
  SELECT x.* FROM `ogilvy-trends-v2.intelligence_42_core.suppressions` x
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY x.suppression_id
    ORDER BY x.status_at DESC, x.status = 'lifted', TO_JSON_STRING(x)) = 1)
SELECT s.creator_id FROM s WHERE s.status != 'lifted' AND s.creator_id IS NOT NULL
UNION DISTINCT
SELECT c.creator_id FROM s
JOIN `ogilvy-trends-v2.intelligence_42_core.creators` c
  ON CONCAT(IF(LOWER(TRIM(c.platform)) = 'twitter', 'x', LOWER(TRIM(c.platform))), ':',
            LOWER(REGEXP_REPLACE(TRIM(c.handle), r'^@*(u/)?', '')))
   = CONCAT(IF(LOWER(TRIM(s.platform)) = 'twitter', 'x', LOWER(TRIM(s.platform))), ':',
            LOWER(REGEXP_REPLACE(TRIM(s.handle), r'^@*(u/)?', '')))
WHERE s.status != 'lifted';

ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.post_observations`
ADD COLUMN IF NOT EXISTS source_market STRING;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.post_observations`
ADD COLUMN IF NOT EXISTS source_region STRING;

CREATE VIEW IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.v_post_source_markets` AS
WITH valid_sightings AS (
  SELECT DISTINCT post_id, source_market, source_region, route, protocol,
    observed_at, observed_date AS obs_date
  FROM `ogilvy-trends-v2.intelligence_42_core.post_observations`
  WHERE post_id IS NOT NULL
    AND source_market IN ('ZA', 'NG', 'KE')
    AND observed_at IS NOT NULL
    AND observed_date IS NOT NULL
)
SELECT post_id,
  ARRAY_AGG(DISTINCT source_market ORDER BY source_market) AS source_markets,
  ARRAY_AGG(STRUCT(source_market, source_region, route, protocol, observed_at, obs_date)
    ORDER BY obs_date, observed_at, source_market, source_region, route, protocol) AS source_sightings
FROM valid_sightings
GROUP BY post_id;

/* What readers of breaking_signals read (breaking.py CURRENT_VIEW_SQL): rows of ok breaking runs only, so the rows
   of a run that failed after its INSERT are never read. It reads agent.runs, so agent.sql loads first. */
CREATE VIEW IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.v_breaking_signals_current` AS
SELECT s.* FROM `ogilvy-trends-v2.intelligence_42_core.breaking_signals` s
JOIN `ogilvy-trends-v2.intelligence_42_agent.runs` r ON r.run_id = s.run_id AND r.stage = 'breaking' AND r.status = 'ok';

/* locality_v2 (C4 v3 section 7.1). item_locality is the retained row of one item and market for one detect run,
   item_locality_post its member posts, item_locality_verified the row the detect step writes for a key only after
   Python recounted the members and agreed (core/detect/locality.py). Nothing here is ever updated or deleted: a
   change of rule appends rows under a new metric_version. The views that read them are
   core/detect/sql/locality_views.sql. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.item_locality` (
  run_date DATE NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL,
  detect_run_id STRING NOT NULL, population_cutoff TIMESTAMP NOT NULL, metric_version STRING NOT NULL,
  schema_version INT64 NOT NULL, computed_at TIMESTAMP NOT NULL,
  population_posts INT64 NOT NULL, known_posts INT64 NOT NULL, local_posts INT64 NOT NULL,
  foreign_posts INT64 NOT NULL, unknown_posts INT64 NOT NULL,
  feed_only_posts INT64 NOT NULL, vetoed_feed_posts INT64 NOT NULL,
  local_creators INT64 NOT NULL, known_creators INT64 NOT NULL, feed_only_creators INT64 NOT NULL,
  breadth_creators INT64 NOT NULL,
  status STRING NOT NULL, local_share FLOAT64, population_digest STRING NOT NULL)
PARTITION BY run_date CLUSTER BY market, item_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.item_locality_post` (
  run_date DATE NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL,
  detect_run_id STRING NOT NULL, population_cutoff TIMESTAMP NOT NULL, metric_version STRING NOT NULL,
  post_id STRING NOT NULL, creator_key STRING, platform STRING,
  locality_class STRING NOT NULL, geo_market STRING, geo_confidence FLOAT64, geo_source STRING,
  feed_sighted BOOL NOT NULL, feed_obs_date DATE)
PARTITION BY run_date CLUSTER BY market, item_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.item_locality_verified` (
  run_date DATE NOT NULL, market STRING NOT NULL, item_id STRING NOT NULL,
  detect_run_id STRING NOT NULL, metric_version STRING NOT NULL, verified_at TIMESTAMP NOT NULL,
  member_rows INT64 NOT NULL, population_digest STRING NOT NULL)
PARTITION BY run_date CLUSTER BY market, item_id;

/* The dated, market-aware links of C4 v3 sections 7.2 and 16 that tvf_post_items (locality_views.sql) reads. A row of
   post_items written before these columns existed keeps NULL in both and is read as always, in any market. */
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.post_items`
ADD COLUMN IF NOT EXISTS linked_on DATE;
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.post_items`
ADD COLUMN IF NOT EXISTS link_market STRING;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.post_item_lineage` (
  post_id STRING NOT NULL, item_id STRING NOT NULL, linked_on DATE NOT NULL, link_market STRING,
  lineage_id STRING NOT NULL)
PARTITION BY linked_on CLUSTER BY item_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.post_item_end` (
  post_id STRING NOT NULL, item_id STRING NOT NULL, ended_on DATE NOT NULL, reason STRING NOT NULL,
  lineage_id STRING NOT NULL, recorded_at TIMESTAMP NOT NULL)
PARTITION BY ended_on CLUSTER BY item_id;

/* The three columns the locality switch adds to item_state (C4 v3 section 7.2): the v1 eligibility kept as an
   observation, the rule that wrote eligible (v1 or locality_v2.1), and the checked v2 status carried to the brief
   (null on the v1 basis). Rows written before them keep NULL. */
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.item_state`
ADD COLUMN IF NOT EXISTS eligible_v1 BOOL;
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.item_state`
ADD COLUMN IF NOT EXISTS locality_basis STRING;
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.item_state`
ADD COLUMN IF NOT EXISTS locality_status STRING;
