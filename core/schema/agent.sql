CREATE SCHEMA IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent`
OPTIONS (location = "US");

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.runs` (
  run_id STRING NOT NULL, stage STRING, run_date DATE, status STRING,
  started_at TIMESTAMP, finished_at TIMESTAMP, counts JSON, error STRING,
  question STRING, tier STRING, plan JSON, calls INT64, credits FLOAT64, tokens INT64,
  seconds FLOAT64, outcome STRING, answer JSON, record JSON, model_usd FLOAT64)
PARTITION BY run_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.findings` (
  finding_id STRING NOT NULL, question STRING, answer STRING, as_of TIMESTAMP,
  claims ARRAY<STRUCT<text STRING, label STRING, item_ids ARRAY<STRING>, evidence_post_ids ARRAY<STRING>, query_ids ARRAY<STRING>, run_ids ARRAY<STRING>, result_hashes ARRAY<STRING>>>,
  valid_from TIMESTAMP, valid_to TIMESTAMP, status STRING);

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.forecasts` (
  forecast_id STRING NOT NULL, item_id STRING, market STRING, target STRING, issue_date DATE,
  horizon INT64, rule STRING, prob FLOAT64, predicted_arrival BOOL, persistence_arrival BOOL,
  resolve_date DATE, observed_arrival BOOL)
PARTITION BY issue_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.feedback` (
  who STRING, what STRING, reason STRING, `at` TIMESTAMP);

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.claim_checks` (
  answer_or_brief_id STRING, claim_id STRING, rule STRING, verdict STRING, checker STRING,
  run_id STRING, reason STRING, span_sha256 STRING, reason_code STRING);

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.briefs` (
  brief_date DATE NOT NULL, market STRING NOT NULL, run_id STRING NOT NULL,
  published_at TIMESTAMP, status STRING, payload JSON, rule_version STRING)
PARTITION BY brief_date;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.watches` (
  watch_id STRING NOT NULL, created_at TIMESTAMP, status_at TIMESTAMP, who STRING, target JSON,
  market STRING, rule JSON, label STRING, status STRING);

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.watch_matches` (
  watch_id STRING NOT NULL, match_date DATE NOT NULL, item_id STRING, market STRING,
  method STRING, run_id STRING)
PARTITION BY match_date;

/* The weekly detection scorecard (task 2.7, core/detect/scorecard.py), L2's DDL as given: one row per
   market per learn run, each metric a JSON Figure {value, unit, query_id, run_id, result_hash, n, reason}.
   Append only; the latest run_id per week and market wins. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.engine_scorecard` (
  week_start DATE NOT NULL, week_end DATE NOT NULL, market STRING NOT NULL, run_id STRING NOT NULL,
  rule_version STRING, time_to_detect JSON, lead_time JSON, precision JSON, recall JSON,
  breadth_platforms JSON, expansion_cluster_share JSON, expansion_platform_share JSON,
  expansion_language_share JSON, cost_per_confirmed JSON)
PARTITION BY week_start CLUSTER BY market;

/* Weekly forecast scores and the weekly quality score (FEATURES.md 26 and 7), written by the weekly learn job
   through core/eval/forecast_score.py and quality_score.py, each exactly as its writer's CREATE TABLE in
   core/eval/sql. forecast_score has one row per rule, target and horizon per run, skill NULL (never zero) below
   the minimum, and promotion_eligible; weekly_quality has one row per market (ALL, ZA, NG, KE) per run. Append
   only: the newest scored_at per week and cohort, or week and market, is current. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.forecast_score` (
  week_start DATE NOT NULL, week_end DATE NOT NULL, run_id STRING NOT NULL, scored_at TIMESTAMP,
  rule STRING, target STRING, horizon INT64, n INT64, unresolved INT64, no_baseline INT64, no_prob INT64,
  minimum INT64, brier FLOAT64, persistence_brier FLOAT64, skill FLOAT64, promotion_eligible BOOL,
  query_id STRING, result_hash STRING, row_count INT64, reason STRING)
PARTITION BY week_start;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.weekly_quality` (
  week_start DATE NOT NULL, week_end DATE NOT NULL, market STRING NOT NULL, run_id STRING NOT NULL,
  scored_at TIMESTAMP, score FLOAT64, counted STRING, change FLOAT64, previous_run_id STRING, questions INT64,
  question_set_hash STRING, parts JSON, context JSON, notes STRING)
PARTITION BY week_start;

/* Investigations and dossiers (core/api/contract.md section 13), written by f42-agent, append only.
   An investigation gains a row per draft, edit, start and finish, and its highest version is current.
   A dossier gains a version per draft and freeze; a tick belongs to its claim and the newest `at` counts. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.investigations` (
  investigation_id STRING NOT NULL, version INT64 NOT NULL, created_at TIMESTAMP, who STRING,
  status STRING, question STRING, market STRING, plan JSON, estimate JSON, ask_id STRING, run_id STRING)
CLUSTER BY investigation_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.dossier_versions` (
  dossier_id STRING NOT NULL, version INT64 NOT NULL, created_at TIMESTAMP, who STRING, state STRING,
  body JSON, source_ask_id STRING, content_hash STRING)
CLUSTER BY dossier_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.dossier_reviews` (
  dossier_id STRING NOT NULL, claim_id STRING NOT NULL, ticked BOOL, note STRING, who STRING, `at` TIMESTAMP)
CLUSTER BY dossier_id, claim_id;

/* Scheduled questions and client skins (contract.md 14.2 and 15.1), written by f42-agent, append only:
   a pause, resume, edit or archive appends a row, and the current one is the newest per id in the order
   v_watches_current uses. */
CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.schedules` (
  schedule_id STRING NOT NULL, created_at TIMESTAMP, status_at TIMESTAMP, who STRING, question STRING,
  market STRING, tier STRING, cadence STRING, deliver JSON, status STRING)
CLUSTER BY schedule_id;

CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.skins` (
  skin_id STRING NOT NULL, skin_key STRING, created_at TIMESTAMP, status_at TIMESTAMP, who STRING,
  name STRING, markets JSON, terms JSON, hashtags JSON, accounts JSON, watch_ids JSON, template STRING,
  status STRING)
CLUSTER BY skin_id;

/* Tables made before these columns existed gain them here; a fresh project gets them from the
   CREATE TABLE text above and these statements change nothing. */
ALTER TABLE `ogilvy-trends-v2.intelligence_42_agent.runs`
ADD COLUMN IF NOT EXISTS model_usd FLOAT64;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_agent.feedback`
ADD COLUMN IF NOT EXISTS `at` TIMESTAMP;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_agent.claim_checks`
ADD COLUMN IF NOT EXISTS reason STRING;

/* W8-DEC-14: a failed support or sentence check keeps the SHA-256 of the rejected span (after NFKC and whitespace
   normalisation) and a reason code from a fixed list; never post text or model words. */
ALTER TABLE `ogilvy-trends-v2.intelligence_42_agent.claim_checks`
ADD COLUMN IF NOT EXISTS span_sha256 STRING;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_agent.claim_checks`
ADD COLUMN IF NOT EXISTS reason_code STRING;

ALTER TABLE `ogilvy-trends-v2.intelligence_42_agent.watches`
ADD COLUMN IF NOT EXISTS status_at TIMESTAMP;

/* Watches are append-only: a pause or resume appends a row that keeps the watch's first created_at
   and sets status_at to when its status was set, and the newest row per watch_id by
   COALESCE(status_at, created_at) is the watch as it stands (rows written before status_at fall
   back to created_at). Rows with neither sort last; a tie goes to paused, then to the row's own
   JSON text, so the view picks the same row on every read. */
CREATE VIEW IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.v_watches_current` AS
SELECT w.* FROM `ogilvy-trends-v2.intelligence_42_agent.watches` w
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (PARTITION BY w.watch_id
  ORDER BY COALESCE(w.status_at, w.created_at) DESC NULLS LAST, w.status DESC, TO_JSON_STRING(w)) = 1;
