# Data model and detection

Dataset intelligence_42_core holds everything collected and derived; intelligence_42_agent holds runs, findings, forecasts, feedback, claim_checks, briefs, watches, watch_matches, engine_scorecard, forecast_score, weekly_quality, investigations, dossier_versions, dossier_reviews, schedules, skins and the agent's authorised views. Each table lives in exactly one dataset. None of the SQL below has been run yet: every statement must pass a dry run and fixture tests before use. Every threshold is a starting value, recalibrated by the TRUST.md section 4 backtest after about four weeks of SocialCrawl data. There are no demographic fields anywhere. Tables are created with CREATE TABLE IF NOT EXISTS, a column added later with ALTER TABLE ADD COLUMN IF NOT EXISTS (and in the CREATE TABLE text too, so a fresh project gets it), and a view with CREATE VIEW IF NOT EXISTS; posts, creators, cultural_map and calendar are written by MERGE, everything else by append; nothing is dropped, replaced, deleted or expired.

Binding corrections from the plan review, applied in the DDL and SQL below:
- Raw first: every SocialCrawl response is appended to raw_responses, one MERGE fills posts, and every sighting is appended to post_observations. dlt is not used.
- Lanes: every sighting and series has a lane class (section 3.2). Only unbiased_rank, unbiased_counter and panel series form baselines, and only unbiased_rank and panel posts meet floors. watchlist, search_presence and legacy never do.
- No invented zeros: days before a series' protocol start and days that collection_health marks invalid are NULL. A zero is written only for a valid day on which the item was absent.
- Re-runs: derived tables (collection_health, item_daily, series_test, item_state, coord_signals, briefs) are append-only with run_id and rule_version, never MERGE, and are read only through v_<table>_current views of the latest good run (section 3.1). Every number carries its run_id and a hash of its query result, not time travel.
- SQL hygiene: joins to cultural_map filter valid_to IS NULL; cohort counts use windows without ORDER BY; every column is qualified.
- Statistics (negative binomial, beta-binomial, Benjamini-Hochberg) run in Python; SQL prepares their inputs and applies the state rules.
- Build tasks: tables in BUILD.md 1.2; warm-up detection (sections 3.1 to 3.4, 3.6 and 3.7, with the test step as a pass-through) in 1.6; legacy memory in 1.7; the full statistics (section 3.5) in 1.19; spread, novelty variants and the full worth-attention score in 2.3; co-action authenticity in 2.11.

Reused from the old engine: observation_id (post identity), geographic_scope, the rejection memory from seed_candidates, the geometric mean and midrank cohort percentile from open_intelligence/scoring.py (a cohort needs at least 20 items), the two-vote match rule from open_intelligence/graph.py, and ops/evaluation/forecast_cohort.py.

## 1. Tables

```sql
CREATE TABLE IF NOT EXISTS intelligence_42_core.raw_responses (       -- append-only, every SocialCrawl response
  run_id STRING NOT NULL, job STRING, market STRING, route STRING, params_hash STRING,
  lane STRING, seed_key STRING, fetched_at TIMESTAMP NOT NULL, http_status INT64,
  credits_quoted FLOAT64, credits_charged FLOAT64, cache_hit BOOL, body JSON)
PARTITION BY DATE(fetched_at) CLUSTER BY route, market;

CREATE TABLE IF NOT EXISTS intelligence_42_core.posts (      -- one MERGE from raw_responses: stable fields plus latest metrics
  post_id STRING NOT NULL,              -- observation_id() with market='': one row per post across markets
  platform STRING, native_id STRING, url STRING, creator_id STRING,
  creator_tier_at_post STRING,          -- nano <10k, micro <100k, mid <500k, macro <1M, mega; frozen at first sighting
  text STRING, transcript STRING, hashtags ARRAY<STRING>, sound_id STRING,
  thumbnail_url STRING, duration_s FLOAT64,
  published_at TIMESTAMP,
  post_date DATE NOT NULL,              -- published_at in the first sighting's market-local time; that observed_date when published_at is NULL
  views INT64, likes INT64, comments INT64, shares INT64, engagement INT64,   -- latest reading; history in post_observations
  geo_market STRING, geo_confidence FLOAT64,
  geo_source STRING,                    -- ext_region | home_market | place_mention | language (language alone is never a known location)
  geo_scope STRING, vendor STRING, endpoint STRING, source_regime STRING, vendor_labels JSON, run_id STRING)
PARTITION BY post_date CLUSTER BY platform, geo_market, creator_id;

CREATE TABLE IF NOT EXISTS intelligence_42_core.post_observations (   -- append-only, one row per sighting
  post_id STRING NOT NULL, observed_at TIMESTAMP NOT NULL,
  observed_date DATE NOT NULL,          -- market-local day (SAST, WAT, EAT)
  market STRING NOT NULL, source_market STRING, source_region STRING,
  platform STRING, route STRING, series STRING, protocol STRING,
  lane STRING,                          -- sweep | panel | watchlist | expansion | confirm | exploration | placebo | agent_live | legacy
  lane_class STRING NOT NULL,           -- section 3.2
  seed_key STRING, pull_seq INT64, rank INT64,
  views INT64, likes INT64, comments INT64, shares INT64, run_id STRING)
PARTITION BY observed_date CLUSTER BY market, lane_class, post_id;

CREATE TABLE IF NOT EXISTS intelligence_42_core.item_counter_daily (  -- append-only reads of rank lists and counters
  obs_date DATE NOT NULL,               -- market-local day the value describes
  market STRING NOT NULL,               -- ZA | NG | KE | GLOBAL (platform-wide counters)
  platform STRING NOT NULL, item_id STRING NOT NULL,
  series STRING NOT NULL, route STRING, protocol STRING NOT NULL,
  is_board BOOL,                        -- a platform's own board or chart (On the boards)
  lane_class STRING NOT NULL,           -- unbiased_rank | unbiased_counter
  unit STRING NOT NULL,                 -- appearances (per day) | rank (per pull) | total | delta
  pull_seq INT64,                       -- running pull number of that list and protocol (rank rows)
  value FLOAT64,
  source STRING,                        -- live | vendor_history (past days carried by a vendor curve)
  observed_at TIMESTAMP, available_at TIMESTAMP, run_id STRING)
PARTITION BY obs_date CLUSTER BY market, item_id, series;

CREATE TABLE IF NOT EXISTS intelligence_42_core.collection_health (   -- append-only, one row per market, series, protocol, day
  day DATE NOT NULL, market STRING NOT NULL, platform STRING, route STRING,
  series STRING NOT NULL, protocol STRING NOT NULL, lane_class STRING,
  calls INT64, calls_ok INT64,
  units_planned INT64, units_ok INT64,  -- pulls for rank lists, accounts for panels, items for counters
  items INT64,                          -- rows returned: list entries, posts or counter reads
  ref_items FLOAT64, ref_days INT64,    -- median items over the prior valid days (up to 28) and how many there were
  k FLOAT64,                            -- panels only: units_planned / units_ok
  valid BOOL, invalid_reason STRING,    -- calls | items | effort | drift; a calls failure names its failed
                                        -- calls' classes, e.g. "calls: http_5xx" or "calls: robots" (writers.py)
  located_share FLOAT64,                -- share of the day's posts with a known location (section 3.6)
  run_id STRING)
PARTITION BY day CLUSTER BY market, series;

CREATE TABLE IF NOT EXISTS intelligence_42_core.item_daily (   -- append-only, one aggregate run per metric_date
  metric_date DATE NOT NULL,            -- market-local day of the post's first sighting in this lane class
  market STRING NOT NULL,
  platform STRING NOT NULL,             -- a platform, or '_all' on '_any' rows
  item_id STRING NOT NULL,
  lane_class STRING NOT NULL,           -- a lane class, or '_any': each post once, every lane except legacy and agent_live
  series STRING, protocol STRING,       -- set on panel rows
  posts INT64, creators INT64, unflagged_creators INT64, engagement INT64,
  tier_posts STRUCT<nano INT64, micro INT64, mid INT64, macro INT64, mega INT64>,
  first_post_at TIMESTAMP,
  geo_known_posts INT64,                -- geo_confidence >= 0.7 from ext.region, creator home market or place mentions
  local_posts INT64,                    -- of those, located in this market
  source_regime STRING, available_at TIMESTAMP, run_id STRING, rule_version STRING)
PARTITION BY metric_date CLUSTER BY market, item_id, platform;

CREATE TABLE IF NOT EXISTS intelligence_42_core.item_hourly (   -- append-only hourly counts for the intraday Breaking rule (ENGINE.md), to be written by the pulse (not wired yet)
  market STRING NOT NULL, item_id STRING NOT NULL, platform STRING NOT NULL,
  hour TIMESTAMP NOT NULL,              -- SAST hour start
  posts INT64, creators INT64,
  lane_class STRING NOT NULL,
  run_id STRING NOT NULL)
PARTITION BY DATE(hour) CLUSTER BY market, item_id;

CREATE TABLE IF NOT EXISTS intelligence_42_core.breaking_signals (   -- append-only, written by the hourly Breaking rule (core/detect/breaking.py); read through v_breaking_signals_current, rows of ok breaking runs only
  hour TIMESTAMP NOT NULL,              -- SAST hour start of the last hour judged
  market STRING NOT NULL, item_id STRING NOT NULL,
  posts6 INT64, creators6 INT64, expected6 FLOAT64,
  ratio FLOAT64,                        -- NULL when expected6 is 0: shown as "new, no prior posts", never as a ratio
  platforms INT64,
  run_id STRING NOT NULL, rule_version STRING)
PARTITION BY DATE(hour) CLUSTER BY market, item_id;

CREATE TABLE IF NOT EXISTS intelligence_42_core.series_test (  -- append-only, written by core/detect/stats.py (section 3.5)
  metric_date DATE NOT NULL, series_id STRING NOT NULL, item_id STRING, market STRING, platform STRING,
  series STRING, protocol STRING, lane_class STRING, kind STRING,
  y FLOAT64, trials INT64, obs_prior INT64, obs28 INT64, first_measured DATE,
  baseline_state STRING,                -- warmup | thin | short | ok
  hist_mean FLOAT64, med FLOAT64, v3 FLOAT64, v7 FLOAT64, peak28 FLOAT64, vel FLOAT64, accel FLOAT64,
  z_display FLOAT64,                    -- robust z, display only
  test STRING,                          -- nb | betabinom | none
  mu FLOAT64, alpha FLOAT64, weekday_factor FLOAT64, mu_prior FLOAT64,
  ratio FLOAT64, p_mid FLOAT64, q FLOAT64, significant BOOL,
  run_id STRING, rule_version STRING)
PARTITION BY metric_date CLUSTER BY market, item_id;

CREATE TABLE IF NOT EXISTS intelligence_42_core.cultural_map (
  item_id STRING NOT NULL,              -- sha256(kind|canonical_key)
  kind STRING NOT NULL,                 -- topic | hashtag | sound | format | meme | creator | brand | event
  canonical_key STRING, label STRING, aliases ARRAY<STRING>, parent_item_id STRING,
  centroid ARRAY<FLOAT64>,
  first_seen DATE, first_seen_market STRING, first_seen_platform STRING, last_seen DATE,
  recurrences INT64, lifecycle STRING, status STRING, rejected_until DATE,
  valid_from TIMESTAMP, valid_to TIMESTAMP)
CLUSTER BY kind, item_id;
-- CREATE VECTOR INDEX cm_c ON intelligence_42_core.cultural_map(centroid)
--   OPTIONS(index_type='IVF', distance_type='COSINE');  (once the table has enough rows)
```

Other tables (key: columns; partitioned on their date column where they have one):

- post_enrichment (post_id): embedding (768-d, gemini-embedding-001), langs, code_switched, entities, sounds, formats, tone, stance, sponsored, near_dup_size (written by detect, core/detect/neardup.py: a row only for a size of 2 or more, appended only when the size grows, never deleted), screen_text, video_notes, sensitive (the sensitive topics the post is about: political, religion, religious_holiday, health, sex_life, race_ethnicity, crime, or none; empty on rows written while core/understand/enrich.py SENSITIVE_ENRICH_READY was off).
- post_items (post_id, item_id, via, linked_on, link_market).
- creators (creator_id): platform, handle, followers, tier, account_created_at, home_market, verified_region, coord_score (candidate networks joined in 30 days; 1 or more marks the creator flagged), display_name, verified, profile_location, first_seen, last_seen (the collect job's creators MERGE on platform and creator_id; switched off until every reader joins creators on platform as well as creator_id).
- media (sha256): post_id, gcs_uri, kind (clip, keyframe, thumbnail). Clips are linked; a copy is kept only for macro-tier accounts, brands, or a clip a dossier cites (SETUP.md data protection).
- entities (entity_id): kind, name, aliases.
- clusters (cluster_date, cluster_id): market or 'pan', item_id, match_kind, label, keywords, local_terms, centroid.
- cluster_members (cluster_id, post_id, probability).
- item_locality (run_date, market, item_id, detect_run_id, population_cutoff, metric_version): schema_version, computed_at, population_posts, known_posts, local_posts, foreign_posts, unknown_posts, feed_only_posts, vetoed_feed_posts, local_creators, known_creators, feed_only_creators, breadth_creators, status, local_share, population_digest.
- item_locality_post (run_date, market, item_id, detect_run_id, population_cutoff, metric_version, post_id): creator_key, platform, locality_class, geo_market, geo_confidence, geo_source, feed_sighted, feed_obs_date.
- item_locality_verified (run_date, market, item_id, detect_run_id, metric_version): verified_at, member_rows, population_digest.
- post_item_lineage (post_id, item_id, linked_on): link_market, lineage_id.
- post_item_end (post_id, item_id, ended_on): reason, lineage_id, recorded_at.
- item_state (metric_date, market, item_id, run_id): the columns of the final SELECT in section 3.7, append-only. The table the schema apply creates has the first 36 of them; eligible_v1, locality_basis and locality_status are added by core/schema/locality_switch.sql, which only the locality switch release applies (a80's INSERT is positional, so a wider table would fail it), and under the v1 authority the INSERT names the 36 and writes none of the three.
- coord_signals (metric_date, item_id, market, run_id): signal (coaction | pool_reuse | same_evening_template), component_id, accounts, item_posts_share. Written by the co-action job (task 2.11), append-only.
- breakout_signals (metric_date, market, item_id, run_id): creators, posts, evidence_post_ids ARRAY<STRING>, top_ratio, held_flagged, rule_version. Creator breakouts on one sound or format (task 2.13, core/detect/breakout.py), with flagged accounts left out and counted in held_flagged; append-only, clustered by market, item_id.
- early_signal (metric_date, series_id): item_id, market, platform, series, protocol, lane_class, kind, y, mu, alpha, cusum, early, run_days, days_used, kappa, h, window_days, baseline_mode, baseline_source, baseline_date, run_id, rule_version. The 14 day CUSUM early flag recorded beside series_test by core/detect/early_signal.py; no gate, state, card payload or rank reads it. Append-only, created by core/schema/early_signal.sql through the setup runner (core/schema/apply.py --apply).
- test_switch (market, platform, lane_class): switched_on DATE, backtest_run_id, rule_version. Appended by task 1.19 when a market and platform pass the backtest. A row is in force only once the runs row of the backtest run it cites has status ok (core/detect/sql/stats.sql joins it), so a row added by hand must cite such a run. A replay without --apply leaves a runs row with status replayed carrying the sha256 of its result file, and --apply FILE needs that digest.
- calendar (moment_date, market, name): kind, source, item_ids ARRAY<STRING> (items matched to the moment by alias).
- calendar_analogues (moment_date, market, name): analogue_date, match_kind (rule | source_row | fixed_date | none), window_start, window_end, status (evidence | no_evidence | no_analogue), reason, evidence JSON (top hashtags per source with posts and first_seen; rule 1 labels held back and counted in reason), query_text, query_params JSON, computed_at. Last year's analogue for each calendar row and what 42 and the legacy history held 7 days either side (task 3.6, core/collect/calendar.py --analogues); append-only, readers take the newest computed_at per row.
- gdelt_daily (day, market, entity_kind, entity): mentions, computed_at, market_rule. Mentions of each GDELT person, organisation, theme and place in documents located in a market, for one UTC news day (one GKG partition, task 2.5, core/collect/gdelt_daily.py); the dropped theme families and entities holding a whole rule 1 word are left out, and readers run gdelt.blocked on every entity. market_rule names the rule that placed a document in a market (gdelt_daily.MARKET_RULE, dominant_or_outlet_v2: that country is most of its locations, or a local outlet ran it); rows written before the column existed used the earlier any-mention rule and have NULL. Every read filters on market_rule, so NULL rows are never counted and their days are written again under the rule. The 28-day baseline for rising entities and the news to social bridge; append-only, a day already present under the rule is skipped.
- seed_queue (seed_date, market, item_id or query): kind, lane (expansion | exploration | placebo | anchor), priority, template, ttl_days, credits_estimate, yield_posts, yield_new_creators.
- credit_ledger (trend_date partition): run_id, lane, agent, market, platform, endpoint, params_hash, item_id, calls, credits_quoted, credits_charged, cache_hit, posts_new, balance_after. Its lane takes the post_observations lane values plus local, the collect job's local sources phase (task 2.12), paid from the collect share.
- suppressions (suppression_id): status_at, status (suppressed | lifted), creator_id, platform, handle, reason, who. The suppression list of SETUP.md data protection: an operator suppresses a creator by creator_id, or by platform and handle, with a reason in plain words (never contact details) and who. Append-only, clustered by suppression_id: lifting a suppression appends a lifted row with the same suppression_id and a later status_at. The view v_suppressed_creators gives the creator_id of every creator whose newest row per suppression_id is not lifted (a tie goes to the row that is not lifted), matching a handle to creators the way core/api/store.py creator_key does; f42-api reads it on every route that names a person.
- In intelligence_42_agent: runs (run_id, stage: collect | understand | aggregate | stats | detect | coaction | breakout | watch | seeds | forecast | confirm | brief | learn | ask | investigation | scheduled | reconcile | watchdog | pulse | breaking | digest | backtest, run_date, status, started_at, finished_at, counts, error, model_usd: FLOAT64 model spend in USD for the run at every stage, summed against MODEL_DAILY_USD, stamp: JSON which code produced the run (core/setup/stamp.py: git_sha and image_digest as the deploy declared them, the sha256 of every prompt file recomputed from the tree that ran, and which of the two kinds each field is; built by the writer from its own environment and tree, never passed in, and a row that already carries one is refused; nullable and additive, added by core/schema/apply.py as ADD COLUMN IF NOT EXISTS), and for Ask question, tier, plan, calls, credits, tokens, seconds, outcome); findings (finding_id, question, answer, as_of, claims ARRAY<STRUCT<text, label, item_ids, evidence_post_ids, query_ids, run_ids, result_hashes>>, valid_from, valid_to, status); forecasts (forecast_id: hash of item, market, target, issue date, horizon, rule; prob, predicted_arrival, persistence_arrival, resolve_date, observed_arrival); feedback (who, what, reason, at: TIMESTAMP of the tap, a GoogleSQL reserved word written `at` with backticks in every query); claim_checks (answer or brief id, claim id, rule, verdict, checker, run_id, reason: STRING, why a claim was downgraded or cut, span_sha256: STRING and reason_code: STRING; W8-DEC-14: claim_checks retains, for each failed support or sentence check, the check verdict, the claim id, the SHA-256 of the rejected span after NFKC and whitespace normalisation, and a reason code from a fixed enum; no post text and no model free text; the brief keeps span_sha256 only when it is 64 lowercase hex characters, because the span is not kept and the digest cannot be recomputed, so the check is on shape only (core/brief/job.py, core/trust/retained.py)); briefs (brief_date, market, run_id, rule_version, append-only; rule_version is the detect version followed by one token per rule change that alters what a brief row says for the same inputs, in tree order: pack-member-first, w8-dec-02, w8-dec-06, w8-dec-11, w8-dec-12, w8-dec-14, w8-dec-15, w8-dec-16, w8-dec-17, w8-dec-03d, w8-dec-06b, k6-b7-decade, k6-names, k6-n13-n15, rule1-old-age and g1-gap, pinned in core/brief/tests/test_pack_order.py); watches (watch_id, created_at, status_at: TIMESTAMP, who, target: JSON, market, rule: JSON, label, status: active | paused; append-only, a pause or resume appends a row that keeps the watch's first created_at, status_at is when that row's status was set, and v_watches_current keeps the newest row per watch_id by COALESCE(status_at, created_at), NULL last, a tie going to paused); watch_matches (watch_id, match_date, item_id, market, method, run_id; append-only, partitioned by match_date); engine_scorecard (week_start, week_end, market, run_id, rule_version, time_to_detect: JSON, lead_time: JSON, precision: JSON, recall: JSON, breadth_platforms: JSON, expansion_cluster_share: JSON, expansion_platform_share: JSON, expansion_language_share: JSON, cost_per_confirmed: JSON; each metric is a Figure with value and unit and query_id and run_id and result_hash and n and reason, and regime (the rule that wrote item_state.eligible over the window the Figure reads and over the same window a week earlier, with their window, and whether the two may be compared; locality_basis is NULL, read as v1, until the switch release), and a null value carries its reason; one row per market per learn run (task 2.7), append-only, the latest run_id per week and market wins; partitioned by week_start and clustered by market); forecast_score (week_start, week_end, run_id, scored_at, rule, target, horizon, n, unresolved, no_baseline, no_prob, minimum, brier, persistence_brier, skill, promotion_eligible: BOOL, query_id, result_hash, row_count, reason; one row per rule, target and horizon per weekly learn run (core/eval/forecast_score.py, section 6), skill NULL below 200 resolved, append-only, the newest scored_at is current; partitioned by week_start); weekly_quality (week_start, week_end, market, run_id, scored_at, score, counted, change, previous_run_id, questions, question_set_hash, parts: JSON, context: JSON, notes; one row per market (ALL, ZA, NG, KE) per weekly learn run (core/eval/quality_score.py), score NULL when no part had enough data, append-only, the newest scored_at is current; partitioned by week_start); investigations (investigation_id, version, created_at, who, status: draft | running | complete | stopped | failed, question, market, plan: JSON, estimate: JSON, ask_id, run_id; a row for each draft or edit and at start and at finish, the highest version is current; append-only, clustered by investigation_id); dossier_versions (dossier_id, version, created_at, who, state: draft | frozen, body: JSON, source_ask_id, content_hash; a frozen version is never changed and editing again starts a new draft version; append-only, clustered by dossier_id); dossier_reviews (dossier_id, claim_id, ticked: BOOL, note, who, at: TIMESTAMP written `at` with backticks; a tick belongs to its claim and the newest row per claim_id counts; append-only, clustered by dossier_id and claim_id); schedules (schedule_id, created_at, status_at, who, question, market, tier: T0 | T1, cadence: weekly_monday | daily, deliver: JSON, status: active | paused; append-only, a pause or resume appends a row and the current schedule is the newest row per schedule_id in the order v_watches_current uses; clustered by schedule_id); skins (skin_id, skin_key, created_at, status_at, who, name, markets: JSON, terms: JSON, hashtags: JSON, accounts: JSON, watch_ids: JSON, template, status: active | archived; append-only in the same way as schedules, an edit or archive appends a row; clustered by skin_id).

## 2. Backfill

- enriched_content to posts, post_items and post_observations with lane_class='legacy': recompute ids, rerun geographic scope, leave tier NULL, drop genz_score and search velocity. Re-embed only the last 90 days.
- seed_graph to item_daily with lane_class='legacy': hashtag to hashtag, slang to meme, handle to creator, music to sound; tokens skipped. v_seed_first_seen gives first_seen; rejected seed_candidates stay blocked for 28 days.
- trend_scores: query groups become topic items. lifecycle_phase is kept only to check the new classifier; trend_score is not copied.
- Legacy rows are memory only (BUILD.md 1.7): first_seen, Recurring and the height of earlier waves in v_item_waves. They never form a baseline or shorten warm-up, because they are a different regime (TRUST.md section 4). history_normalizer may later put old wave heights on the SocialCrawl scale for display only; where it cannot bridge, heights read "posts 42 found then". Backtests filter on available_at <= as_of.

## 3. Detection

The detect step runs after collect and understand: aggregate (item_daily), stats (series_test), detect (item_state). Each writes a runs row in intelligence_42_agent.runs.

### 3.1 Runs and current views

The latest good run for a stage and date is the newest run_id whose runs row has status 'ok' for that run_date. Every derived table is read through a view of it; nothing reads a derived table directly except a re-run pinned to a run_id or a backtest filtering available_at.

```sql
CREATE OR REPLACE VIEW intelligence_42_core.v_good_runs AS
SELECT r.stage, r.run_date, ARRAY_AGG(r.run_id ORDER BY r.finished_at DESC LIMIT 1)[OFFSET(0)] run_id
FROM intelligence_42_agent.runs r WHERE r.status = 'ok' GROUP BY r.stage, r.run_date;

-- Same shape for v_collection_health_current (stage collect, day), v_item_daily_current (aggregate, metric_date),
-- v_series_test_current (stats), v_coord_signals_current (coaction) and intelligence_42_agent.v_briefs_current (brief).
CREATE OR REPLACE VIEW intelligence_42_core.v_item_state_current AS
SELECT s.* FROM intelligence_42_core.item_state s
JOIN intelligence_42_core.v_good_runs g
  ON g.stage = 'detect' AND g.run_date = s.metric_date AND g.run_id = s.run_id;

-- Counter and rank reads: the newest read of each value from any good collect run (vendor curves restate past days).
CREATE OR REPLACE VIEW intelligence_42_core.v_item_counter_daily_current AS
SELECT c.* FROM intelligence_42_core.item_counter_daily c
WHERE c.run_id IN (SELECT r.run_id FROM intelligence_42_agent.runs r WHERE r.stage = 'collect' AND r.status = 'ok')
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.obs_date, c.market, c.item_id, c.series, c.protocol, c.unit, c.pull_seq
                           ORDER BY c.available_at DESC) = 1;
```

### 3.2 Measurement series

A series is one item on one source in one market, measured in the unit that source supports, under one protocol (route, fixed parameters, list length, pulls a day, panel membership, planned search calls a market). A protocol change starts a new series. From the parser-fix release, the culture desk, the curated panels and the gossip panel read as panel:<12 hex>:v2, and tiktok/song reads as tiktok/song?proto=v2; those version tokens start a fresh health reference for their series. The X panel, the facebook pages panel, the hashtag routes (tiktok/hashtags/popular and tiktok/hashtag) and tiktok/song/videos keep their protocols unchanged, with no version token. Markets: ZA, NG, KE; GLOBAL for platform-wide counters. Row numbers are SOURCES.md's costed table.

| Series | Source route | Markets | Lane class | Unit | Test from task 1.19 |
|---|---|---|---|---|---|
| feed_tiktok | tiktok/trending feed=local, 3 pulls (1) | ZA, NG, KE | unbiased_rank | appearances in the day's pulls; days present | beta-binomial |
| board_tiktok_hashtag | tiktok/hashtags/popular, 7-day board (2) | ZA | unbiased_rank, board | entry, rank, rank climb, days present | beta-binomial |
| curve_tiktok_hashtag | the same call's daily curve per hashtag (2) | ZA | unbiased_counter | daily count from the vendor curve | negative binomial |
| board_youtube | youtube/videos/trending, 5 categories (3) | ZA, NG, KE | unbiased_rank, board | entry, rank, rank climb, days present | beta-binomial |
| board_global_music | youtube/shorts/trending, instagram/music/trending (4) | GLOBAL | unbiased_rank, board | entry, days present | none (context) |
| board_apple_music | apple_music/charts (6) | ZA; NG, KE if the probe passes | unbiased_rank, board | entry, rank, rank climb, days present | beta-binomial |
| list_reddit | reddit/subreddit rising and hot (7) | ZA, NG, KE | unbiased_rank | appearances, days present | beta-binomial |
| x_trends | web/scrape of the X trends archive (23b) | ZA, NG, KE | unbiased_rank, seed only | entry, days present | none: never evidence, floor, state or Today |
| panel_fb_hub | facebook/profile/posts since=, hub pages (8) | ZA, NG, KE | panel | posts per day times k | negative binomial |
| panel_culture_desk | prism/profiles include=posts since=, the hubs.yaml culture desk list (23) | ZA, NG, KE | panel | posts per day times k | negative binomial |
| panel_curated_creators | prism/profiles include=posts since=, the day's rotation of kept creators (23) | ZA, NG, KE | panel | posts per day times k | negative binomial |
| panel_x_hub | twitter/user/tweets since= (23a) | ZA, NG, KE | panel | posts per day times k | negative binomial |
| panel_telegram | telegram/profile/posts, curated channels (9, Stage 2) | ZA, NG, KE | panel | posts per day times k | negative binomial |
| counter_tiktok_hashtag, counter_tiktok_sound | tiktok/hashtag and tiktok/song totals (12) | GLOBAL | unbiased_counter | daily delta of the running total | negative binomial |
| curve_tiktok_sound | tiktok/song/videos adoption points (11) | none | not written | the vendor's by_day points total the videos on the page, a sample by publish day and not a daily total, so no series is written; the job counts them as song_curve_sample_skipped | none |
| counter_ig_audio | instagram/audio/reels (13, Stage 2) | GLOBAL | unbiased_counter if a total is returned (probe), else watchlist | daily delta | negative binomial |
| counter_post_views | prism/post-stats re-reads (22) | per post | unbiased_counter | daily view delta per post | none: Peaking and Fading context only, because 42 chooses the posts |
| watch | posts on sound and hashtag pages (11, 12), new feed authors' posts (10), Alerts watches, anchor terms | as read | watchlist | presence | none |
| ig_location | instagram/location/posts, rotating hub (5) | ZA, NG, KE | search_presence | presence, location | none |
| search | rows 14 to 18 and 24 to 27: expansion, confirmation, exploration searches | ZA, NG, KE | search_presence | presence | none |
| placebo | about 5% of expansion calls on random low-ranked items (23c) | ZA, NG, KE | search_presence, lane placebo | presence | none: sets the spread base rate |
| agent_live | Ask's live calls | as asked | search_presence, lane agent_live | presence | none; kept out of item_daily |
| legacy | enriched_content, seed_graph | ZA, NG, KE | legacy | first_seen, waves | none: memory only |
| local charts | Boomplay, Audiomack, Shazam, TurnTable, kworb, Google Play, Nairaland front page (2.12) | per source | unbiased_rank, board | entry, rank, rank climb, days present | beta-binomial |

Rules:
- Baselines come only from unbiased_rank, unbiased_counter and panel series. Floors count posts and creators seen in unbiased_rank and panel lanes (counters carry no creators). watchlist and search_presence rows are evidence, presence, authenticity and location only: never baseline, floor, test or spread. Items seen only in those lanes have no series and get no state (G3, "Found by search").
- Protocol start: rank lists and panels watch every item at once, so an item's series on them starts on the protocol's first day in that market and days before its first sighting are genuine zeros. Counters are read per item, so their series starts at the item's first read; earlier days are NULL. A vendor curve read on first sighting carries its own past days (source 'vendor_history', available_at the read time); those count as observed days of that series.
- The culture desk and the curated creator rotation read the same route in the same lane, and each has its own series, so one panel's zero day is never the day before of the other (W8-DEC-11, RB-C3). Rows written before the split hold both panels under panel_culture_desk and stay as written; the rotation's baselines under panel_curated_creators start with no history.
- GLOBAL counters are tested in their own family. They never make an item Rising in a market by themselves; they count only as the independent second platform.
- Effort factor k applies only to panel series: k = units_planned / units_ok (accounts scheduled over accounts read successfully), and the panel's day is valid only with k inside [0.5, 2]. Feeds, boards, charts and counters are fixed-length lists or platform totals and are never scaled.
- Rank and rank climb are stored per pull and shown on cards; the test on rank lists uses presence, which is less noisy with three pulls a day.

### 3.3 Collection validity

Appended by the collect job for day @d after its last call. v_route_day is a view giving that run's counts per market, series and protocol from raw_responses (calls_ok: HTTP 2xx with a parsable body), post_observations and item_counter_daily, plus drifted (the TRUST.md A1 feed overlap and concentration check). The reference is the median of prior valid days: 28 of them once they exist, otherwise whatever valid days exist, at least 3; with fewer than 3, a day is judged on call success alone.

```sql
INSERT INTO intelligence_42_core.collection_health
  (day, market, platform, route, series, protocol, lane_class, calls, calls_ok, units_planned, units_ok, items,
   ref_items, ref_days, k, valid, invalid_reason, located_share, run_id)
WITH ref AS (
  SELECT h.market, h.series, h.protocol,
    APPROX_QUANTILES(h.items, 2)[OFFSET(1)] ref_items, COUNT(*) ref_days
  FROM intelligence_42_core.v_collection_health_current h
  WHERE h.valid AND h.day BETWEEN DATE_SUB(@d, INTERVAL 28 DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
  GROUP BY h.market, h.series, h.protocol),
x AS (
  SELECT t.*, r.ref_items, IFNULL(r.ref_days, 0) ref_days,
    IF(t.lane_class = 'panel', SAFE_DIVIDE(t.units_planned, t.units_ok), NULL) k,
    CASE
      WHEN t.calls_ok < .8 * t.calls THEN 'calls'
      WHEN t.drifted THEN 'drift'                       -- feed drift check, TRUST.md A1
      WHEN IFNULL(r.ref_days, 0) >= 3 AND t.items NOT BETWEEN .5 * r.ref_items AND 2 * r.ref_items THEN 'items'
      WHEN t.lane_class = 'panel'
        AND IFNULL(SAFE_DIVIDE(t.units_planned, t.units_ok), 99) NOT BETWEEN .5 AND 2 THEN 'effort'
    END invalid_reason
  FROM intelligence_42_core.v_route_day t
  LEFT JOIN ref r ON r.market = t.market AND r.series = t.series AND r.protocol = t.protocol
  WHERE t.day = @d)
SELECT x.day, x.market, x.platform, x.route, x.series, x.protocol, x.lane_class, x.calls, x.calls_ok,
  x.units_planned, x.units_ok, x.items, x.ref_items, x.ref_days, x.k,
  x.invalid_reason IS NULL, x.invalid_reason, x.located_share, @run_id
FROM x;
```

Invalid reasons in `invalid_reason` are `calls`, `drift`, `items`, `effort` and `zero_yield`. A paid route whose calls succeed on two consecutive days and land zero posts and zero counters is recorded invalid for those days with reason `zero_yield`. G1's hold rule and the d, d-1, d-2 window are unchanged.

### 3.4 Series and their features

item_daily is appended by the aggregate step: each post counts once per market, item, lane class and series, on the market-local day of its first sighting there, plus one '_any' row (platform '_all') counting every post once. Rank and counter values go straight to item_counter_daily from the collect job, including 'delta' rows derived from successive totals (NULL when the previous day is missing).

```sql
CREATE OR REPLACE VIEW intelligence_42_core.v_series_daily AS
WITH h AS (SELECT hc.* FROM intelligence_42_core.v_collection_health_current hc),
cd AS (SELECT cc.* FROM intelligence_42_core.v_item_counter_daily_current cc),
rank_items AS (             -- every item ever seen on a list; the list watched it from the protocol's first day
  SELECT DISTINCT cd.item_id, cd.market, cd.platform, cd.series, cd.protocol
  FROM cd WHERE cd.lane_class = 'unbiased_rank' AND cd.unit = 'appearances' AND cd.series != 'x_trends'),
panel_items AS (            -- one row per panel series; item_daily has a row per platform, and a panel whose route
  SELECT i.item_id, i.market, i.platform, i.series, i.protocol    -- names none (the culture desk) spans several,
  FROM intelligence_42_core.v_item_daily_current i WHERE i.lane_class = 'panel'  -- so it keeps the platform it first saw the item on
  QUALIFY ROW_NUMBER() OVER (PARTITION BY i.item_id, i.market, i.series, i.protocol
                             ORDER BY i.metric_date, i.platform IS NULL, i.platform) = 1),
panel_posts AS (            -- each post once: item_daily counts it on its own platform's row
  SELECT pp.item_id, pp.market, pp.series, pp.protocol, pp.metric_date, SUM(pp.posts) posts
  FROM intelligence_42_core.v_item_daily_current pp WHERE pp.lane_class = 'panel'
  GROUP BY pp.item_id, pp.market, pp.series, pp.protocol, pp.metric_date),
u AS (
  SELECT ri.item_id, ri.market, ri.platform, ri.series, ri.protocol, 'unbiased_rank' lane_class, h.day,
    IF(h.valid, IFNULL(ra.value, 0), NULL) value, h.units_ok trials       -- zero only on a valid day
  FROM rank_items ri
  JOIN h ON h.market = ri.market AND h.series = ri.series AND h.protocol = ri.protocol
  LEFT JOIN cd ra ON ra.item_id = ri.item_id AND ra.market = ri.market AND ra.series = ri.series
    AND ra.protocol = ri.protocol AND ra.unit = 'appearances' AND ra.obs_date = h.day
  UNION ALL
  SELECT pi.item_id, pi.market, pi.platform, pi.series, pi.protocol, 'panel', h.day,
    IF(h.valid, IFNULL(pd.posts, 0) * h.k, NULL), NULL
  FROM panel_items pi
  JOIN h ON h.market = pi.market AND h.series = pi.series AND h.protocol = pi.protocol
  LEFT JOIN panel_posts pd ON pd.item_id = pi.item_id AND pd.market = pi.market
    AND pd.series = pi.series AND pd.protocol = pi.protocol AND pd.metric_date = h.day
  UNION ALL
  SELECT cv.item_id, cv.market, cv.platform, cv.series, cv.protocol, 'unbiased_counter', cv.obs_date,
    IF(cv.source = 'vendor_history' OR hv.valid, cv.value, NULL), NULL      -- no row before the first read
  FROM cd cv
  LEFT JOIN h hv ON hv.market = cv.market AND hv.series = cv.series AND hv.protocol = cv.protocol AND hv.day = cv.obs_date
  WHERE cv.lane_class = 'unbiased_counter' AND cv.unit = 'delta' AND cv.series != 'counter_post_views')
SELECT CONCAT(u.item_id, '|', u.market, '|', u.series, '|', u.protocol) series_id, u.*
FROM u;

CREATE OR REPLACE TABLE FUNCTION intelligence_42_core.tvf_series_signal(d DATE) AS
WITH prior AS (             -- observed days before d since the series' protocol started
  SELECT sd.series_id, COUNTIF(sd.value IS NOT NULL) obs_prior
  FROM intelligence_42_core.v_series_daily sd WHERE sd.day < d GROUP BY sd.series_id),
first_seen AS (             -- first day the item was present on any measured series in the market
  SELECT sd.item_id, sd.market, MIN(sd.day) first_measured
  FROM intelligence_42_core.v_series_daily sd WHERE sd.day <= d AND sd.value > 0
  GROUP BY sd.item_id, sd.market),
w AS (
  SELECT sd.*,
    ARRAY_AGG(IF(sd.value IS NULL, NULL, STRUCT(sd.day AS day, sd.value AS y, sd.trials AS n)) IGNORE NULLS)
      OVER (s RANGE BETWEEN 28 PRECEDING AND 1 PRECEDING) hist,             -- valid days of the previous 28
    AVG(sd.value) OVER (s RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) v3,
    AVG(sd.value) OVER (s RANGE BETWEEN 5 PRECEDING AND 3 PRECEDING) v3_prev,
    AVG(sd.value) OVER (s RANGE BETWEEN 8 PRECEDING AND 6 PRECEDING) v3_prev2,
    SUM(sd.value) OVER (s RANGE BETWEEN 6 PRECEDING AND CURRENT ROW) v7,
    MAX(sd.value) OVER (s RANGE BETWEEN 27 PRECEDING AND CURRENT ROW) peak28
  FROM intelligence_42_core.v_series_daily sd
  WHERE sd.day BETWEEN DATE_SUB(d, INTERVAL 36 DAY) AND d
  WINDOW s AS (PARTITION BY sd.series_id ORDER BY UNIX_DATE(sd.day))),
m AS (
  SELECT w.*, IFNULL(ARRAY_LENGTH(w.hist), 0) obs28,
    (SELECT APPROX_QUANTILES(hh.y, 2)[SAFE_OFFSET(1)] FROM UNNEST(w.hist) hh) med,
    (SELECT AVG(hh.y) FROM UNNEST(w.hist) hh) hist_mean
  FROM w WHERE w.day = d)
SELECT m.series_id, m.item_id, m.market, m.platform, m.series, m.protocol, m.lane_class, cm.kind,
  m.value y, m.trials, m.hist, IFNULL(pr.obs_prior, 0) obs_prior, m.obs28, fs.first_measured,
  m.hist_mean, m.med, m.v3, m.v7, m.peak28,
  LN(1 + m.v3) - LN(1 + m.v3_prev) vel,
  (LN(1 + m.v3) - LN(1 + m.v3_prev)) - (LN(1 + m.v3_prev) - LN(1 + m.v3_prev2)) accel,
  (m.value - m.med) / GREATEST(1.4826 * (SELECT APPROX_QUANTILES(ABS(hh.y - m.med), 2)[SAFE_OFFSET(1)]
                                         FROM UNNEST(m.hist) hh), SQRT(m.med + 1)) z_display,
  CASE WHEN IFNULL(pr.obs_prior, 0) < 14 THEN 'warmup'     -- no test before 14 observed days
       WHEN m.obs28 < 14 THEN 'thin'                       -- too many invalid days in the window
       WHEN pr.obs_prior < 28 THEN 'short'                 -- test with the cold-start prior
       ELSE 'ok' END baseline_state
FROM m
LEFT JOIN prior pr ON pr.series_id = m.series_id
LEFT JOIN first_seen fs ON fs.item_id = m.item_id AND fs.market = m.market
JOIN intelligence_42_core.cultural_map cm ON cm.item_id = m.item_id AND cm.valid_to IS NULL;
```

### 3.5 The test (core/detect/stats.py, Python, task 1.19)

Until task 1.19 switches a market and platform on, stats.py copies every tvf_series_signal row to series_test with test = 'none'. After that:

Input: `SELECT * FROM intelligence_42_core.tvf_series_signal(@d)`, v_test_switch (the test_switch rows in force), weekday totals from v_collection_health_current, and first-week values of recently new items from v_series_daily.

Per series:
1. test = 'none' when baseline_state is 'warmup' or 'thin', when the switch for its market, platform and lane class is off, or when y is 0 and the 28-day history has no activity (it cannot be significant). The row is still written.
2. test = 'nb' for unbiased_counter and panel series, with y rounded (panel values already carry k; a negative counter delta is a vendor correction, set to 0 and logged).
   - Weekday factor f per market, platform and lane class from 8 weeks of valid route totals, shrunk n/(n+4) towards 1; f = 1 until 4 weeks exist.
   - Prepared candidate stats-2-series uses each platformless panel's own market, series and collection protocol for weekday exposure, with the same closed 56-day window, validity rules, shrinkage and minimum observations. It requires an explicit in-force test_switch row naming stats-2-series. Existing stats-1 switches, missing or unknown versions, test eligibility and dispersion cohorts keep their existing behavior. Offline replay can select this candidate explicitly; the normal backtest command and apply path still default to stats-1. Accepted backtest evidence and separate activation approval are required before any candidate switch row is appended.
   - Replay uses its explicitly requested weekday model. The default stats-1 replay keeps legacy factors even when retained switch metadata names stats-2-series, while preserving which keys are eligible. Candidate replay requires rule_version="stats-2-series". Panel identity is derived only from each metric day's latest good aggregate available at the replay cutoff, matching the live view.
   - mu_item (rule_version stats-1, approved by Albert on 29 September): the Farrington baseline times max(1, the pooled NB2 intercept scale) times the weekday factor f for today, fitted in two stages. First, the baseline: first-pass median m of hist y/f; residual r = 1.5 (y^(2/3) - m^(2/3)) / (m^(1/6) sqrt(phi)) where phi = 1 + alpha m and alpha comes from a first pooled NB2 fit (the residual of Farrington flexible, Noufaily et al. 2013); days with r > 2 get weight r^-2; the baseline is the weighted median of y/f. Second, the scale: the pooled NB2 fit below is refitted on that down-weighted baseline and its intercept gives the scale; scales below 1 are taken as 1. A baseline of 0 always takes the cold-start prior. The plain weighted median made 12 to 15% of unchanged series significant at p 0.05 in a null simulation; this form holds the false-alarm share between 2% and 8%.
   - Cold-start prior, from observed day 14 to 27 (baseline_state 'short') and for any item first measured under 28 days ago: mu = max(mu_item, mu_prior). mu_prior is the 90th percentile of the mean daily value over the first 7 observed days of items of the same kind, market, platform and series first seen in the previous 90 days; with under 20 such items it pools across platforms, then markets. It never applies before day 14, because no test runs then.
   - Dispersion alpha pooled per market, platform, kind and volume band (floor(log2(1 + mu))): statsmodels NB2 GLM, intercept only, offset log(mu), over the pooled 28-day histories; under 30 series the group pools up (drop band, then kind, then platform). Method of moments if the fit fails.
   - p_mid = P(Y > y) + 0.5 P(Y = y), Y ~ NB(mean mu, alpha) via scipy.stats.nbinom (n = 1/alpha, p = n/(n + mu)); Poisson when alpha is 0. ratio = (y + 1)/(mu + 1).
3. test = 'betabinom' for unbiased_rank series: x = appearances over the last 3 observed days out of n pulls (trials); prior Beta(a0, b0) fitted by moments to the 28-day appearance rates of all items of that kind on that list and market; posterior a = a0 + appearances in hist, b = b0 + non-appearances in hist; p_mid under BetaBinom(n, a, b); ratio = (x/n) / (a/(a + b)).
4. Benjamini-Hochberg with scipy.stats.false_discovery_control(method='bh') per market and day (GLOBAL is its own family) over every series with test != 'none'; significant = q <= 0.05.

Output: one series_test row per input series (the tvf columns except hist, plus test, mu, alpha, weekday_factor, mu_prior, ratio, p_mid, q, significant, run_id, rule_version), then a runs row with stage 'stats'. Weekly, the learn job runs Efron's empirical-null check on z = Phi^-1(1 - p_mid); if the central spread exceeds 1.1, alpha is scaled up and rule_version bumped.

### 3.6 The item's window

Floors, authenticity shares, location, presence and board entries for each item and market, from sightings in the last 28 days (legacy, placebo and agent_live excluded). Known location means geo_confidence of 0.7 or more from ext.region, the creator's home market or place mentions; language alone never counts.

```sql
CREATE OR REPLACE TABLE FUNCTION intelligence_42_core.tvf_item_window(d DATE) AS
WITH o AS (                 -- each post once per item, market and lane group, dated by its first sighting
  SELECT pi.item_id, po.market, po.post_id,
    po.lane_class IN ('unbiased_rank', 'panel') AS measured, MIN(po.observed_date) first_day
  FROM intelligence_42_core.post_observations po
  JOIN intelligence_42_core.post_items pi ON pi.post_id = po.post_id
  WHERE po.observed_date BETWEEN DATE_SUB(d, INTERVAL 27 DAY) AND d
    AND po.lane_class != 'legacy' AND po.lane NOT IN ('placebo', 'agent_live')
  GROUP BY pi.item_id, po.market, po.post_id, measured),
p AS (
  SELECT o.*, o.first_day > DATE_SUB(d, INTERVAL 7 DAY) in7,
    ps.platform, ps.creator_id, ps.creator_tier_at_post tier, ps.published_at,
    IFNULL(cr.coord_score, 0) >= 1 flagged,
    DATE_DIFF(o.first_day, DATE(cr.account_created_at), DAY) < 30 young,   -- account age, a bot signal; never person age
    IFNULL(pe.near_dup_size, 1) >= 3 near_dup, IFNULL(pe.sponsored, FALSE) sponsored
  FROM o
  JOIN intelligence_42_core.posts ps ON ps.post_id = o.post_id
  LEFT JOIN intelligence_42_core.creators cr ON cr.creator_id = ps.creator_id
  LEFT JOIN intelligence_42_core.post_enrichment pe ON pe.post_id = o.post_id),
cr3 AS (                    -- measured lanes: this 3-day window and the one before
  SELECT p.item_id, p.market, p.creator_id, LOGICAL_OR(p.flagged) flagged,
    COUNTIF(p.first_day > DATE_SUB(d, INTERVAL 3 DAY)) n3,
    COUNTIF(p.first_day BETWEEN DATE_SUB(d, INTERVAL 5 DAY) AND DATE_SUB(d, INTERVAL 3 DAY)) n3_prev
  FROM p WHERE p.measured GROUP BY p.item_id, p.market, p.creator_id),
fl AS (
  SELECT cr3.item_id, cr3.market,
    SUM(IF(cr3.flagged, 0, cr3.n3)) posts3, SUM(IF(cr3.flagged, 0, cr3.n3_prev)) posts3_prev,
    COUNTIF(cr3.creator_id IS NOT NULL AND cr3.n3 > 0 AND NOT cr3.flagged) creators3,
    SAFE_DIVIDE(MAX(cr3.n3), SUM(cr3.n3)) top_creator_share3
  FROM cr3 GROUP BY cr3.item_id, cr3.market),
a7 AS (
  SELECT p.item_id, p.market,
    COUNT(DISTINCT IF(p.in7, p.post_id, NULL)) posts7,
    SAFE_DIVIDE(COUNT(DISTINCT IF(p.in7 AND p.near_dup, p.post_id, NULL)), COUNT(DISTINCT IF(p.in7, p.post_id, NULL))) near_dup_share,
    SAFE_DIVIDE(COUNT(DISTINCT IF(p.in7 AND p.young, p.post_id, NULL)), COUNT(DISTINCT IF(p.in7, p.post_id, NULL))) young_share,
    SAFE_DIVIDE(COUNT(DISTINCT IF(p.in7 AND p.sponsored, p.post_id, NULL)), COUNT(DISTINCT IF(p.in7, p.post_id, NULL))) sponsored_share,
    COUNT(DISTINCT IF(p.in7 AND p.measured AND p.tier IN ('macro', 'mega'), p.post_id, NULL)) large_posts7,
    COUNT(DISTINCT IF(p.in7 AND p.platform = 'news', p.post_id, NULL)) news_posts7,
    MIN(IF(p.measured AND p.tier IN ('nano', 'micro'), p.published_at, NULL)) small_at,
    MIN(IF(p.measured AND p.tier IN ('macro', 'mega'), p.published_at, NULL)) large_at
  FROM p GROUP BY p.item_id, p.market),
t3 AS (                     -- top-3 creator share, 7 days, all evidence lanes
  SELECT c.item_id, c.market,
    SAFE_DIVIDE((SELECT SUM(v) FROM UNNEST(ARRAY_AGG(c.n ORDER BY c.n DESC LIMIT 3)) v), SUM(c.n)) top3_share
  FROM (SELECT p.item_id, p.market, p.creator_id, COUNT(DISTINCT p.post_id) n
        FROM p WHERE p.in7 GROUP BY p.item_id, p.market, p.creator_id) c
  GROUP BY c.item_id, c.market),
bu AS (                     -- share of 7-day posts in the busiest 10 minutes
  SELECT b.item_id, b.market, SAFE_DIVIDE(MAX(b.n), SUM(b.n)) burst_share
  FROM (SELECT p.item_id, p.market, DIV(UNIX_SECONDS(p.published_at), 600) bucket, COUNT(DISTINCT p.post_id) n
        FROM p WHERE p.in7 AND p.published_at IS NOT NULL GROUP BY p.item_id, p.market, bucket) b
  GROUP BY b.item_id, b.market),
sn AS (SELECT p.item_id, COUNT(DISTINCT p.post_id) seen7_all FROM p WHERE p.in7 GROUP BY p.item_id),
cv AS (                     -- largest 7-day counter delta, for the Not assessed coverage test
  SELECT x.item_id, MAX(x.delta7) delta7
  FROM (SELECT sd.item_id, sd.series_id, SUM(sd.value) delta7 FROM intelligence_42_core.v_series_daily sd
        WHERE sd.lane_class = 'unbiased_counter' AND sd.day BETWEEN DATE_SUB(d, INTERVAL 6 DAY) AND d
        GROUP BY sd.item_id, sd.series_id) x
  GROUP BY x.item_id),
geo AS (
  SELECT i.item_id, i.market, SUM(i.geo_known_posts) geo_known_posts7, SUM(i.local_posts) local_posts7
  FROM intelligence_42_core.v_item_daily_current i
  WHERE i.lane_class = '_any' AND i.metric_date BETWEEN DATE_SUB(d, INTERVAL 6 DAY) AND d
  GROUP BY i.item_id, i.market),
fp AS (                     -- presence, 14 days; shown only above the placebo base rate
  SELECT i.item_id, i.market, COUNT(DISTINCT i.platform) found_platforms14
  FROM intelligence_42_core.v_item_daily_current i
  WHERE i.lane_class NOT IN ('_any', 'legacy') AND i.metric_date BETWEEN DATE_SUB(d, INTERVAL 13 DAY) AND d
  GROUP BY i.item_id, i.market),
bd AS (                     -- board or chart entry today (never the X trends archive)
  SELECT c.item_id, c.market, TRUE board_entry
  FROM intelligence_42_core.v_item_counter_daily_current c
  WHERE c.obs_date = d AND c.is_board AND c.unit = 'appearances' AND c.value > 0 AND c.series != 'x_trends'
  GROUP BY c.item_id, c.market),
t10 AS (                    -- top 10 of a board or feed on 2 consecutive pulls, ending today
  SELECT DISTINCT a.item_id, a.market, TRUE top10_twice
  FROM intelligence_42_core.v_item_counter_daily_current a
  JOIN intelligence_42_core.v_item_counter_daily_current b
    ON b.item_id = a.item_id AND b.market = a.market AND b.series = a.series AND b.protocol = a.protocol
   AND b.unit = 'rank' AND b.pull_seq = a.pull_seq - 1 AND b.value <= 10
  WHERE a.obs_date = d AND a.unit = 'rank' AND a.value <= 10 AND a.series != 'x_trends'),
keys AS (
  SELECT p.item_id, p.market FROM p UNION DISTINCT
  SELECT bd.item_id, bd.market FROM bd UNION DISTINCT
  SELECT t10.item_id, t10.market FROM t10)
SELECT k.item_id, k.market, fl.posts3, fl.posts3_prev, fl.creators3, fl.top_creator_share3,
  a7.posts7, a7.near_dup_share, a7.young_share, a7.sponsored_share, a7.large_posts7, a7.news_posts7,
  a7.small_at, a7.large_at, t3.top3_share, bu.burst_share, sn.seen7_all, cv.delta7,
  geo.geo_known_posts7, geo.local_posts7, fp.found_platforms14,
  IFNULL(bd.board_entry, FALSE) board_entry, IFNULL(t10.top10_twice, FALSE) top10_twice
FROM keys k
LEFT JOIN fl ON fl.item_id = k.item_id AND fl.market = k.market
LEFT JOIN a7 ON a7.item_id = k.item_id AND a7.market = k.market
LEFT JOIN t3 ON t3.item_id = k.item_id AND t3.market = k.market
LEFT JOIN bu ON bu.item_id = k.item_id AND bu.market = k.market
LEFT JOIN sn ON sn.item_id = k.item_id
LEFT JOIN cv ON cv.item_id = k.item_id
LEFT JOIN geo ON geo.item_id = k.item_id AND geo.market = k.market
LEFT JOIN fp ON fp.item_id = k.item_id AND fp.market = k.market
LEFT JOIN bd ON bd.item_id = k.item_id AND bd.market = k.market
LEFT JOIN t10 ON t10.item_id = k.item_id AND t10.market = k.market;

CREATE OR REPLACE TABLE FUNCTION intelligence_42_core.tvf_placebo_base(d DATE) AS
WITH pl AS (
  SELECT DISTINCT q.item_id, q.market FROM intelligence_42_core.seed_queue q
  WHERE q.lane = 'placebo' AND q.seed_date BETWEEN DATE_SUB(d, INTERVAL 27 DAY) AND d),
found AS (
  SELECT pl.item_id, pl.market, COUNT(DISTINCT i.platform) n
  FROM pl LEFT JOIN intelligence_42_core.v_item_daily_current i
    ON i.item_id = pl.item_id AND i.market = pl.market AND i.lane_class NOT IN ('_any', 'legacy')
   AND i.metric_date BETWEEN DATE_SUB(d, INTERVAL 13 DAY) AND d
  GROUP BY pl.item_id, pl.market),
rise AS (
  SELECT pl.item_id, pl.market, COUNT(DISTINCT st.platform) n
  FROM pl LEFT JOIN intelligence_42_core.v_series_test_current st
    ON st.item_id = pl.item_id AND st.market = pl.market AND st.significant
   AND st.metric_date BETWEEN DATE_SUB(d, INTERVAL 13 DAY) AND d
  GROUP BY pl.item_id, pl.market)
SELECT f.market, COUNT(*) placebo_items,
  APPROX_QUANTILES(f.n, 20)[OFFSET(19)] found_p95, APPROX_QUANTILES(r.n, 20)[OFFSET(19)] rising_p95
FROM found f JOIN rise r ON r.item_id = f.item_id AND r.market = f.market
GROUP BY f.market;
```

v_item_waves (task 1.7): one row per item, market and wave (wave_start, wave_end, peak_date, peak_posts), from v_item_daily_current rows of every lane class except '_any' (legacy included, placebo excluded): islands of days with sightings, split by 28 or more quiet days, with the busiest day as the peak. A wave counts for Recurring and Seasonal when peak_posts is 8 or more.

### 3.7 States

One state per item and market, the first that applies in this order. "Tested" means at least one of the item's series in that market ran the section 3.5 test today; "untested" means none did (warm-up, thin or not yet switched on). Floors: 5 distinct unflagged creators and 8 posts in 3 days in unbiased_rank and panel lanes, no creator above 40% of those posts.

| Order | State | Rule |
|---|---|---|
| 1 | Seasonal | Qualifies for Rising, Emerging, Spike or New to 42, and a calendar moment for the item falls from 3 days ago to 14 days ahead, or an earlier wave peaked within 7 days of this date last year (G8) |
| 2 | Recurring | Qualifies for Rising, Emerging, Spike or New to 42, its current wave began under 28 days ago, and an earlier wave peaked in the last 365 days (TRUST.md B5); the card shows that wave's date and height |
| 3 | Rising | Tested; floors met; significant today with ratio 2 or more, and either significant with ratio 2 or more on another of the last 3 days, or significant today on another platform (a GLOBAL counter counts only on a different platform), or significant in another market in the last 3 days |
| 4 | Emerging | Floors met; new (first measured sighting under 28 days ago, no earlier wave) or dormant (current wave began under 28 days ago after an earlier wave); if tested, significant today; if untested, 5 or more observed days and a 3-day count not below the previous 3 days' |
| 5 | Spike | Tested: floors met and significant today, without the persistence Rising needs. Untested: a panel or counter value at least 3 times its own median over 5 or more earlier observed days and at least 8, with 3 unflagged creators; or top 10 of a board or feed on 2 consecutive pulls. Shown as unconfirmed, with counts |
| 6 | Fading | Tested; was Spike, Emerging, Rising, Peaking, Mainstream, Recurring or Seasonal in the last 28 days; the main series at or below 60% of its 28-day peak on each of the last 3 days |
| 7 | Mainstream | Tested; was Rising, Peaking or Mainstream in the last 28 days; macro or mega creators in measured lanes and news coverage in 7 days, or rising on 3 or more platforms above the placebo base rate |
| 8 | Peaking | Tested; was Emerging or Rising in the last 14 days; acceleration below zero while the 3-day level is at least 70% of the 28-day peak |
| 9 | On the boards | On a platform's own board or chart today (never the X trends archive); labelled as the platform's list |
| 10 | New to 42 | Untested; first measured sighting in the last 14 days; 3 distinct unflagged creators in unbiased_rank or panel lanes in 3 days, or 1 board entry; no earlier wave; counts shown, no growth claim |

The main series is the item's tested series with the highest expected value (panels and counters before rank lists). There is no med = 0 shortcut: a zero baseline is handled by the cold-start prior and the floors. Step-downs have two-day hysteresis: an item shown at a higher state yesterday keeps it unless it also classified lower yesterday. base_state (approved 29 September 2026, the last column of the row) is the state the item would have without orders 1 and 2 (Seasonal and Recurring) when that is Rising, Emerging, Spike or New to 42, and NULL otherwise, so a Seasonal row resting on Rising can be told from one resting on New to 42. Rows written before the column existed hold NULL.

Authenticity (TRUST.md section 5), first match wins: Likely coordinated, only from coord_signals (a co-action network holding 20% or more of the item's posts, or two network signals); Not assessed, when 42 saw fewer than 30 of the item's posts in 7 days or fewer than 10% of its largest counter's 7-day delta; Check pattern, for any share flag or one network signal; Not assessed, when no co-action run exists for the day (so nothing reads Clear before task 2.11); Clear otherwise. Share flags never hold an item. Location (G6): local_share = local_posts / geo_known_posts over 7 days; under 8 known posts the item is market_unconfirmed, not dropped; under 0.6 it is not_local. Spread counts a platform only if one of the item's measured series there was significant in the last 14 days, and only when that count exceeds the 95th percentile for placebo items (at least 20 of them); presence ("found on N platforms") follows the same placebo rule.

```sql
CREATE TEMP FUNCTION state_level(s STRING) AS (
  CASE s WHEN 'rising' THEN 6 WHEN 'emerging' THEN 5 WHEN 'recurring' THEN 5 WHEN 'seasonal' THEN 5
    WHEN 'spike' THEN 4 WHEN 'peaking' THEN 4 WHEN 'mainstream' THEN 4 WHEN 'new_to_42' THEN 3
    WHEN 'on_the_boards' THEN 2 WHEN 'fading' THEN 1 ELSE 0 END);

INSERT INTO intelligence_42_core.item_state (
  metric_date, market, item_id, kind, state_raw, state, untested, main_series_id, main_y, main_mu, main_ratio,
  q_min, sig_days3, creators3, posts3, top_creator_share3, authenticity, share_flags, sponsored_share,
  geo_status, local_share, geo_known_posts7, spread_platforms, found_platforms, markets_hot, lead_market,
  diffusion, novelty, last_wave, moment, eligible, worth_raw, worth_pct, run_id, rule_version, base_state, eligible_v1, locality_basis, locality_status)
WITH t AS (SELECT st.* FROM intelligence_42_core.v_series_test_current st WHERE st.metric_date = @d),
agg AS (                    -- the item's series in this market today
  SELECT t.item_id, t.market,
    COUNTIF(t.test != 'none') = 0 untested,
    MAX(t.obs_prior + IF(t.y IS NULL, 0, 1)) obs_days,
    MIN(t.first_measured) first_measured,
    MIN(t.p_mid) p_min, MIN(t.q) q_min,
    LOGICAL_OR(t.significant) sig_today,
    LOGICAL_OR(t.significant AND t.ratio >= 2) sig_ratio_today,
    COUNT(DISTINCT IF(t.significant, t.platform, NULL)) sig_platforms_today,
    ARRAY_AGG(DISTINCT IF(t.significant, t.platform, NULL) IGNORE NULLS) sig_platform_list,
    LOGICAL_OR(t.lane_class != 'unbiased_rank' AND t.obs_prior >= 5 AND t.y >= 8 AND t.y >= 3 * t.med) jump_today,
    ARRAY_AGG(t ORDER BY t.test != 'none' DESC, t.lane_class = 'unbiased_rank', IFNULL(t.mu, t.v3) DESC, t.series_id LIMIT 1)[OFFSET(0)] main
  FROM t WHERE t.market != 'GLOBAL'
  GROUP BY t.item_id, t.market
  HAVING MAX(IFNULL(t.peak28, 0)) > 0),
t3 AS (
  SELECT st.item_id, st.market, COUNT(DISTINCT st.metric_date) sig_days3
  FROM intelligence_42_core.v_series_test_current st
  WHERE st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 2 DAY) AND @d AND st.significant AND st.ratio >= 2
  GROUP BY st.item_id, st.market),
other AS (                  -- independent evidence: other markets (3 days), GLOBAL counters (today)
  SELECT st.item_id, ARRAY_AGG(DISTINCT st.market) sig_markets3,
    ARRAY_AGG(DISTINCT IF(st.market = 'GLOBAL' AND st.metric_date = @d, st.platform, NULL) IGNORE NULLS) global_platforms
  FROM intelligence_42_core.v_series_test_current st
  WHERE st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 2 DAY) AND @d AND st.significant
  GROUP BY st.item_id),
xm AS (                     -- markets significant in 14 days, not held as coordinated
  SELECT st.item_id, COUNT(DISTINCT st.market) markets_hot,
    ARRAY_AGG(st.market ORDER BY st.metric_date, st.market LIMIT 1)[OFFSET(0)] lead_market
  FROM intelligence_42_core.v_series_test_current st
  LEFT JOIN intelligence_42_core.v_item_state_current ps
    ON ps.item_id = st.item_id AND ps.market = st.market AND ps.metric_date = st.metric_date
  WHERE st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d AND st.market != 'GLOBAL'
    AND st.significant AND IFNULL(ps.authenticity, '') != 'likely_coordinated'
  GROUP BY st.item_id),
sp AS (
  SELECT st.item_id, st.market, COUNT(DISTINCT st.platform) rising_platforms
  FROM intelligence_42_core.v_series_test_current st
  WHERE st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d AND st.significant
  GROUP BY st.item_id, st.market),
hs AS (                     -- recent shown states
  SELECT s.item_id, s.market,
    LOGICAL_OR(s.state IN ('emerging', 'rising') AND s.metric_date >= DATE_SUB(@d, INTERVAL 14 DAY)) grew14,
    LOGICAL_OR(s.state IN ('rising', 'peaking', 'mainstream')) big28,
    LOGICAL_OR(s.state IN ('spike', 'emerging', 'rising', 'peaking', 'mainstream', 'recurring', 'seasonal')) active28,
    MAX(IF(s.metric_date = DATE_SUB(@d, INTERVAL 1 DAY), s.state, NULL)) state_yesterday,
    MAX(IF(s.metric_date = DATE_SUB(@d, INTERVAL 1 DAY), s.state_raw, NULL)) raw_yesterday
  FROM intelligence_42_core.v_item_state_current s
  WHERE s.metric_date BETWEEN DATE_SUB(@d, INTERVAL 28 DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
  GROUP BY s.item_id, s.market),
fade AS (
  SELECT a.item_id, a.market, COUNTIF(st.y <= .6 * st.peak28) low_days
  FROM agg a JOIN intelligence_42_core.v_series_test_current st
    ON st.series_id = a.main.series_id AND st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 2 DAY) AND @d
  GROUP BY a.item_id, a.market),
wave AS (SELECT wv0.* FROM intelligence_42_core.v_item_waves wv0 WHERE wv0.wave_start <= @d),
cur AS (
  SELECT wave.item_id, wave.market, MAX(wave.wave_start) cur_start
  FROM wave WHERE wave.wave_end >= DATE_SUB(@d, INTERVAL 2 DAY) GROUP BY wave.item_id, wave.market),
wv AS (                     -- earlier waves (before the current one) with a peak of 8 or more
  SELECT e.item_id, e.market,
    LOGICAL_OR(e.peak_date >= DATE_SUB(@d, INTERVAL 365 DAY)) peak_365,
    LOGICAL_OR(ABS(DATE_DIFF(e.peak_date, DATE_SUB(@d, INTERVAL 1 YEAR), DAY)) <= 7) last_year,
    ARRAY_AGG(STRUCT(e.peak_date AS peak_date, e.peak_posts AS peak_posts) ORDER BY e.peak_date DESC LIMIT 1)[OFFSET(0)] last_wave
  FROM wave e LEFT JOIN cur ON cur.item_id = e.item_id AND cur.market = e.market
  WHERE e.peak_posts >= 8 AND e.wave_start < IFNULL(cur.cur_start, DATE_ADD(@d, INTERVAL 1 DAY))
  GROUP BY e.item_id, e.market),
cal AS (
  SELECT c.market, it AS item_id, MIN(c.name) moment
  FROM intelligence_42_core.calendar c, UNNEST(c.item_ids) it
  WHERE c.moment_date BETWEEN DATE_SUB(@d, INTERVAL 3 DAY) AND DATE_ADD(@d, INTERVAL 14 DAY)
  GROUP BY c.market, it),
co AS (
  SELECT cs.item_id, cs.market,
    MAX(IF(cs.signal = 'coaction', cs.item_posts_share, 0)) network_share,
    COUNT(DISTINCT CONCAT(cs.signal, ':', IFNULL(cs.component_id, ''))) network_signals
  FROM intelligence_42_core.v_coord_signals_current cs WHERE cs.metric_date = @d
  GROUP BY cs.item_id, cs.market),
cl AS (
  SELECT k.item_id, UPPER(k.market) market, ANY_VALUE(k.match_kind) match_kind
  FROM intelligence_42_core.clusters k WHERE k.cluster_date = @d GROUP BY k.item_id, UPPER(k.market)),
lo AS (SELECT k.item_id, k.market, k.checked_status FROM intelligence_42_core.v_item_locality_checked k
  WHERE k.run_date = @d AND k.detect_run_id = @run_id),
f AS (
  SELECT a.*, cm.kind, cm.status map_status, w.* EXCEPT (item_id, market),
    IFNULL(t3.sig_days3, 0) sig_days3,
    EXISTS (SELECT 1 FROM UNNEST(IFNULL(o.sig_markets3, [])) mk WHERE mk NOT IN (a.market, 'GLOBAL')) other_market,
    EXISTS (SELECT 1 FROM UNNEST(IFNULL(o.global_platforms, [])) gp
            WHERE gp NOT IN UNNEST(IFNULL(a.sig_platform_list, []))) other_platform_global,
    IFNULL(xm.markets_hot, 0) markets_hot, xm.lead_market,
    IF(pb.placebo_items >= 20 AND sp.rising_platforms > pb.rising_p95, sp.rising_platforms, NULL) spread_platforms,
    IF(pb.placebo_items >= 20 AND w.found_platforms14 > pb.found_p95, w.found_platforms14, NULL) found_platforms,
    IFNULL(hs.grew14, FALSE) grew14, IFNULL(hs.big28, FALSE) big28, IFNULL(hs.active28, FALSE) active28,
    hs.state_yesterday, hs.raw_yesterday, IFNULL(fd.low_days, 0) = 3 low3,
    wv.item_id IS NOT NULL had_earlier_wave, IFNULL(cur.cur_start > DATE_SUB(@d, INTERVAL 28 DAY), FALSE) new_wave,
    IFNULL(wv.peak_365, FALSE) peak_365, IFNULL(wv.last_year, FALSE) last_year, wv.last_wave,
    cal.moment, cl.match_kind, co.network_share, co.network_signals, lo.checked_status locality_checked,
    (SELECT COUNT(*) > 0 FROM intelligence_42_core.v_good_runs g WHERE g.stage = 'coaction' AND g.run_date = @d) coaction_ran
  FROM agg a
  JOIN intelligence_42_core.cultural_map cm ON cm.item_id = a.item_id AND cm.valid_to IS NULL
  LEFT JOIN intelligence_42_core.tvf_item_window(@d) w ON w.item_id = a.item_id AND w.market = a.market
  LEFT JOIN t3 ON t3.item_id = a.item_id AND t3.market = a.market
  LEFT JOIN other o ON o.item_id = a.item_id
  LEFT JOIN xm ON xm.item_id = a.item_id
  LEFT JOIN sp ON sp.item_id = a.item_id AND sp.market = a.market
  LEFT JOIN intelligence_42_core.tvf_placebo_base(@d) pb ON pb.market = a.market
  LEFT JOIN hs ON hs.item_id = a.item_id AND hs.market = a.market
  LEFT JOIN fade fd ON fd.item_id = a.item_id AND fd.market = a.market
  LEFT JOIN cur ON cur.item_id = a.item_id AND cur.market = a.market
  LEFT JOIN wv ON wv.item_id = a.item_id AND wv.market = a.market
  LEFT JOIN cal ON cal.item_id = a.item_id AND cal.market = a.market
  LEFT JOIN cl ON cl.item_id = a.item_id AND cl.market = a.market
  LEFT JOIN co ON co.item_id = a.item_id AND co.market = a.market
  LEFT JOIN lo ON lo.item_id = a.item_id AND lo.market = a.market),
b AS (
  SELECT f.*,
    IFNULL(f.creators3, 0) >= 5 AND IFNULL(f.posts3, 0) >= 8 AND IFNULL(f.top_creator_share3, 1) <= .4 floors,
    IFNULL(f.creators3, 0) >= 3 OR IFNULL(f.board_entry, FALSE) new_floor,
    f.first_measured > DATE_SUB(@d, INTERVAL 28 DAY) AND NOT f.had_earlier_wave is_new,
    ARRAY(SELECT x FROM UNNEST([
      IF(f.near_dup_share >= .30, 'near_duplicates', NULL), IF(f.young_share >= .40, 'young_accounts', NULL),
      IF(f.posts7 >= 10 AND f.burst_share >= .35, 'burst', NULL),
      IF(f.posts7 >= 10 AND f.top3_share >= .60, 'concentrated', NULL)]) x WHERE x IS NOT NULL) share_flags
  FROM f),
c AS (
  SELECT b.*,
    NOT b.untested AND b.floors AND b.sig_ratio_today
      AND (b.sig_days3 >= 2 OR b.sig_platforms_today >= 2 OR b.other_platform_global OR b.other_market) rising,
    b.floors AND (b.is_new OR (b.new_wave AND b.had_earlier_wave))
      AND IF(b.untested, b.obs_days >= 5 AND IFNULL(b.posts3, 0) >= IFNULL(b.posts3_prev, 0), b.sig_today) emerging,
    IF(b.untested, (b.jump_today AND IFNULL(b.creators3, 0) >= 3) OR IFNULL(b.top10_twice, FALSE),
       b.floors AND b.sig_today) spike,
    b.untested AND b.first_measured > DATE_SUB(@d, INTERVAL 14 DAY) AND b.new_floor fresh,
    NOT b.untested AND b.active28 AND b.low3 fading,
    NOT b.untested AND b.big28 AND ((IFNULL(b.large_posts7, 0) > 0 AND IFNULL(b.news_posts7, 0) > 0)
      OR IFNULL(b.spread_platforms, 0) >= 3) mainstream,
    NOT b.untested AND b.grew14 AND b.main.accel < 0 AND b.main.v3 >= .7 * b.main.peak28 peaking
  FROM b),
s AS (
  SELECT c.*,
    CASE
      WHEN (c.rising OR c.emerging OR c.spike OR c.fresh) AND (c.moment IS NOT NULL OR c.last_year) THEN 'seasonal'
      WHEN (c.rising OR c.emerging OR c.spike OR c.fresh) AND c.new_wave AND c.peak_365 THEN 'recurring'
      WHEN c.rising THEN 'rising' WHEN c.emerging THEN 'emerging' WHEN c.spike THEN 'spike'
      WHEN c.fading THEN 'fading' WHEN c.mainstream THEN 'mainstream' WHEN c.peaking THEN 'peaking'
      WHEN IFNULL(c.board_entry, FALSE) THEN 'on_the_boards'
      WHEN c.fresh AND NOT c.had_earlier_wave THEN 'new_to_42' END state_raw,
    CASE                    -- the state without the Seasonal and Recurring override, kept when it is one of four
      WHEN c.rising THEN 'rising' WHEN c.emerging THEN 'emerging' WHEN c.spike THEN 'spike'
      WHEN c.fading OR c.mainstream OR c.peaking OR IFNULL(c.board_entry, FALSE) THEN NULL
      WHEN c.fresh AND NOT c.had_earlier_wave THEN 'new_to_42' END base_state,
    CASE
      WHEN IFNULL(c.network_share, 0) >= .2 OR IFNULL(c.network_signals, 0) >= 2 THEN 'likely_coordinated'
      WHEN IFNULL(c.posts7, 0) < 30 OR IFNULL(SAFE_DIVIDE(c.seen7_all, c.delta7), 1) < .1 THEN 'not_assessed'
      WHEN ARRAY_LENGTH(c.share_flags) > 0 OR IFNULL(c.network_signals, 0) = 1 THEN 'check_pattern'
      WHEN NOT c.coaction_ran THEN 'not_assessed'
      ELSE 'clear' END authenticity,
    CASE WHEN IFNULL(c.geo_known_posts7, 0) < 8 THEN 'market_unconfirmed'
         WHEN c.local_posts7 / c.geo_known_posts7 < .6 THEN 'not_local' ELSE 'local' END geo_status,
    SAFE_DIVIDE(c.local_posts7, c.geo_known_posts7) local_share,
    CASE WHEN c.large_at IS NULL THEN 'small_only' WHEN c.small_at < c.large_at THEN 'bottom_up'
         ELSE 'top_down' END diffusion,
    CASE WHEN c.is_new THEN 'new' WHEN c.new_wave AND c.had_earlier_wave THEN 'recurrence'
         WHEN c.match_kind = 'variant' THEN 'variant' ELSE 'ongoing' END novelty
  FROM c),
sc AS (
  SELECT s.*,
    IF(state_level(s.state_raw) < state_level(s.state_yesterday)
       AND state_level(s.raw_yesterday) >= state_level(s.state_yesterday), s.state_yesterday, s.state_raw) state,
    s.state_raw IS NOT NULL AND s.authenticity != 'likely_coordinated'
      AND IF(@authority = 'v2', IFNULL(s.locality_checked, 'missing') != 'not_local', s.geo_status != 'not_local')
      AND s.map_status = 'active' eligible,
    s.state_raw IS NOT NULL AND s.authenticity != 'likely_coordinated' AND s.geo_status != 'not_local'
      AND s.map_status = 'active' eligible_v1,
    IF(@authority = 'v2', IFNULL(s.locality_checked, 'missing'), NULL) locality_status,
    PERCENT_RANK() OVER (PARTITION BY s.market, s.kind
      ORDER BY IFNULL(-LOG10(GREATEST(s.p_min, 1e-12)), 0), (s.main.y + 1) / (IFNULL(s.main.mu, s.main.med) + 1)) surge,
    PERCENT_RANK() OVER (PARTITION BY s.market, s.kind ORDER BY IFNULL(s.main.vel, 0) + .5 * IFNULL(s.main.accel, 0)) momentum,
    PERCENT_RANK() OVER (PARTITION BY s.market, s.kind ORDER BY IFNULL(s.creators3, 0)) breadth
  FROM s),
wr AS (
  SELECT sc.*,
    CASE sc.authenticity WHEN 'clear' THEN 1 ELSE .8 END
    * EXP((LN(.05 + sc.surge) + LN(.05 + sc.momentum) + LN(.05 + sc.breadth)
      + LN(.05 + LEAST(1, .25 * IFNULL(sc.spread_platforms, 1) + .35 * GREATEST(sc.markets_hot - 1, 0)))
      + LN(CASE sc.novelty WHEN 'new' THEN 1 WHEN 'recurrence' THEN .8 WHEN 'variant' THEN .6 ELSE .35 END)
      + LN(CASE sc.diffusion WHEN 'bottom_up' THEN 1 WHEN 'small_only' THEN .7 ELSE .5 END)) / 6) worth_raw
  FROM sc)
SELECT @d metric_date, wr.market, wr.item_id, wr.kind, wr.state_raw, wr.state, wr.untested,
  wr.main.series_id main_series_id, wr.main.y main_y, wr.main.mu main_mu, wr.main.ratio main_ratio,
  wr.q_min, wr.sig_days3, wr.creators3, wr.posts3, wr.top_creator_share3,
  wr.authenticity, wr.share_flags, wr.sponsored_share, wr.geo_status, wr.local_share, wr.geo_known_posts7,
  wr.spread_platforms, wr.found_platforms, wr.markets_hot, wr.lead_market, wr.diffusion, wr.novelty,
  wr.last_wave, wr.moment, wr.eligible, wr.worth_raw,
  IF(wr.eligible AND COUNT(*) OVER co_n >= 20,
     (PERCENT_RANK() OVER co * (COUNT(*) OVER co_n - 1) + CUME_DIST() OVER co * COUNT(*) OVER co_n - 1)
       / 2 / (COUNT(*) OVER co_n - 1), NULL) worth_pct,              -- midrank percentile in the cohort
  @run_id run_id, @rule_version rule_version, wr.base_state,
  wr.eligible_v1, IF(@authority = 'v2', 'locality_v2.1', 'v1') locality_basis, wr.locality_status
FROM wr
WHERE wr.state IS NOT NULL
WINDOW co AS (PARTITION BY wr.market, wr.eligible ORDER BY wr.worth_raw),
       co_n AS (PARTITION BY wr.market, wr.eligible);
```

The publish gate (TRUST.md section 2) reads v_item_state_current and v_collection_health_current; it is not part of this query. App words: new_to_42 New to 42, spike Spike, on_the_boards On the boards, emerging Emerging, rising Rising, peaking Peaking, mainstream Mainstream, fading Fading, recurring Recurring, seasonal Seasonal. Flags: likely_coordinated Likely coordinated, check_pattern Check pattern, not_assessed Not assessed (thin sample), market_unconfirmed Market unconfirmed.

Audience neutrality: the score uses no demographic input; every component is behaviour against the item's own baseline or a rank within its cohort. young_share is about account age (a bot signal), never person age.

## 4. Clustering (nightly, per market plus a pooled 'pan' run)

1. BERTopic fits three days of posts using stored embeddings (embedding_model=None), retaining the original HDBSCAN assignments and probabilities and the original-fit keywords. Only today's original inliers become daily cluster members, including assigned members with probability zero; original outliers stay unassigned. UMAP n_neighbors=15, n_components=5, metric='cosine', random_state=42. HDBSCAN min_cluster_size=max(10, n//500), min_samples=5. Unicode CountVectorizer ngram_range=(1,2), ClassTfidfTransformer(reduce_frequent_words=True). Run counts report fit_outliers across the actual fitted input and today_outliers across its posts sighted today.
2. Matching to cultural_map without merge_models (it renumbers topics; items need stable ids): VECTOR_SEARCH top 3 by centroid; a match needs at least two votes from cosine >= 0.82, keyword Jaccard >= 0.10, a shared hashtag or sound, a shared creator, the item seen in the last 7 days, with at least one vote from cosine, keywords or a shared hashtag or sound. A match to an item dormant 28 days or more is a recurrence. Cosine 0.70 to 0.82 creates a variant child item. Anything else is new. One-to-one assignment (Hungarian); centroids update by EMA (0.8 old, 0.2 new); pairs at 0.9 or above go to a weekly merge review. A matched item keeps its label. Under the v2 locality authority only, the label changes when the new cluster is at cosine 0.82 or more to the item, its keywords have left the item's (Jaccard under 0.10) and it shares at least two member posts with the item's cluster of the same market on the previous day (Q14); an item is renamed at most once a day, the old label goes to aliases unless it is the neutral Topic placeholder, and the run's counts list the label changes and the pairs that kept their label for want of shared posts, the first 50 of each with their totals.
3. Labels: a small, cheap model labels only new or drifted clusters from 12 representative posts (at most two per creator), returning validated JSON: label, kind, description, local_terms [{term, gloss}], languages. Labels are cached. The labeller is chosen by the blind test like every other role.
4. Multilingual and code-switched text: embeddings are multilingual, so clusters are never split by language. Span-level language ID sets code_switched. Language never marks a post foreign (Pidgin and Sheng misdetect as tl, id or ms); only geographic_scope does.

## 5. Seed expansion budget

Daily budget B = (monthly cap minus month spend minus reserve) / days left, from credit_ledger, never above collect's share of ENGINE_DAILY (2,000 credits, SETUP.md). Confirmation has its own 300-credit share and is not paid from B. B is spent in this order:

1. Measured lanes, about 40% of B: feeds, boards, charts, counters and the panels, including the hub panels (SOURCES.md rows 1 to 12, 22, 23, 23a). Their protocols stay fixed and are not retuned by yield; a change is a new protocol and a new series.
2. Exploration floor: at least 10% of the expansion share (about 27 credits, paid inside row 14 as SOURCES.md says), reserved next. Hub panels and placebo are never paid from it.
3. Expansion, about 30%, of which about 5% of calls are placebo expansions (row 23c, paid from expansion).
4. Depth and watchlist, about 20%.

Exploration above its floor, expansion and depth are retuned weekly by yield per credit. When B falls, depth shrinks first, then expansion; the measured lanes and the exploration floor are kept. If the measured lanes alone would pass 90% of B, the lowest-yield panel accounts are dropped as a new protocol version rather than cutting exploration.

- Expansion: items with worth_pct >= 0.8 in New to 42, Spike, Emerging, Rising or Peaking. Skip items expanded in the last two days unless the main series' acceleration is above 0. Priority = worth_pct x (1.3 New to 42 or Emerging, 1.0 Spike or Rising, 0.5 Peaking) x 1.2 if new. Templates by kind (expansion_templates): hashtag feed; posts using the sound; creator recent posts; topic c-TF-IDF terms plus local_terms; brand or event search on X, Threads, Reddit and news. Greedy selection by 28-day ledger cost per call until each market has used a third of expansion credits; leftovers to a global pass. Rising items at p90 or above are also probed in the other two markets at 0.6 priority. Expansion finds are search_presence: evidence and presence, never baseline, floor or spread.
- Placebo: the same templates on random items ranked below p40 in the same market, lane placebo, written to seed_queue so tvf_placebo_base can find them.
- Exploration: Thompson sampling over p40 to p80 items with a Beta prior on "Rising within 7 days"; unseeded pulls (trending feeds, one-hop creator walks, co-occurring hashtags not yet in the map, rotating vernacular queries such as "eish", "abeg", "sasa").

## 6. Forecasts

Each day, 7-day and 14-day forecasts for every Emerging and Rising item plus a random 10% control sample. Targets and their persistence baselines: reach_rising (the item is Rising or Peaking now); cross_market, another market with a significant day (q <= 0.05) not held as Likely coordinated (markets_hot >= 2 now); persist_50, v7 of the main series stays at half its level or more (always). A logistic rule first, BQML LOGISTIC_REG after 500 resolved forecasts. A daily append records observed_arrival once, read through the latest good run; a failed collection day in the window leaves the forecast unresolved. Weekly scoring through forecast_cohort.review_arrival_cohort(rows, minimum_comparable_sample=200) with Brier scores against persistence. Forecasts stay hidden from users and the agent until they beat persistence (promotion_eligible), because the old ARIMA forecast lost to "today holds".

## 7. What the agent can see

Authorised views in intelligence_42_agent with a 2 GB bytes-billed cap, all built on the v_*_current views: v_items_today (v_item_state_current plus label and as_of); tvf_item_timeseries(item_id, market, days) (from v_series_daily, NULL days shown as gaps); v_item_evidence (post_id, url, creator tier, excerpt up to 280 characters, engagement, thumbnail, clip); v_clusters_today; v_prior_findings; tvf_search_items(q) (ML.GENERATE_EMBEDDING then VECTOR_SEARCH); v_cross_market; v_platform_path; v_creator_diffusion; v_forecast_record and v_open_forecasts; v_collection_coverage (lanes, credits, invalid days, failed runs, so the agent can say "we did not look"). The agent writes only through save_finding (validates evidence post ids; every number carries a run_id and result hash; any claim about the future needs a logged forecast_id). Live calls use the agent_live lane (the AGENT.md tier budgets: T1 up to 60 credits, T2 up to 300) and never count toward baselines, floors or presence.
