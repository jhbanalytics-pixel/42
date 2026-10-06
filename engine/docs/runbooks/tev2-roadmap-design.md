# TEV2 roadmap, 16 features onto Pulse and the emailer

Date: 2026-06-19
Author: Albert Meintjes
Status: design, Wave 0 detailed and locked. Waves 1 and 2 sketched, each gets its own spec.

## Goal

Add the 16 features in `TEv2 roadmap for features.xlsx` to the Trends Engine so each one shows on both surfaces: the daily emailer (mailer v2, live in production) and the Pulse (Listening Post web app, a separate repo). Every feature tested, vetted, and proven on a live cron before its flag goes on.

## What the codebase audit changed about the roadmap

Four read-only explorers mapped every roadmap item to the real code. Two items are already shipped, five are flag flips on code that already exists, one needs a single column, and the rest are genuine builds. The roadmap's "16 new features" is really 14 to build or flip, two already done.

Already shipped, no work:

- Hard brand-exclusion on image prompts. `src/analysis/prompts/trend_brief.py:40` and `:50` already emit "Exclude, as hard rules the model must not render" for both the Nano Banana and Lyria clauses. The UEFA-style leak is closed on the default path.
- YouTube comment threads. `comment_threads_enabled: true` for all three markets in `configs/sources.yaml`.

## Constraints and principles

These hold for every wave.

1. Dark on ship. New code lands behind a flag set off, so the day it merges the cron behaves exactly as before. Flip only after a clean cron has run the dark path.
2. Branch plus flag for any cron-path change. Each wave is its own branch, its own flag set, its own PR.
3. Full discipline vetting. Unit tests for new code, full CI green, dark ship, then one live 00:30 UTC cron prove-out, then flip. Morning-check must stay CLEAN across the flip.
4. Both surfaces. A feature is not done until it shows in the emailer and the Pulse. Most build features are three or four commits: engine compute, then a BigQuery column or table, then the email render, then the Listening Post API and React render.
5. No same-day full pipeline rerun. The cron owns the daily fire. Validation reads BigQuery, it does not re-fire the pipeline.
6. Staggered flips. When a wave flips more than one flag, stagger them across crons so a regression is attributable to one change.

## Surfacing model

The emailer runs mailer v2 in production (`MAILER_V2_ENABLED` true via the shared cron secret). Editorial features target the v2 render path under `src/alerts/email_render/`, not the v1 `email_digest.py` path.

The Pulse is imported under `app/`, FastAPI plus Vite and React, with a separate service and CI boundary. The seam for a new engine signal is clean and repeatable:

1. New column on `trend_scores` or `trend_analysis`, or a new derived table, written by the engine.
2. Add the column to the SELECT in `src/api/bq.py` `fetch_desk_rows` (around line 1165).
3. The field flows through `src/api/desk.py` `_build_topic` (around line 250) into the `/api/desk` payload with no new endpoint.
4. Render the field in `frontend/src/today.jsx` (board tile) or `frontend/src/views.jsx` (brief panel).

A new derived table needs a new `bq.fetch_*` function, a new `/api/...` endpoint in `src/api/main.py`, and a new React view plus route.

## The full wave map

| Wave | Feature | Real effort | Email surface | Pulse surface |
|---|---|---|---|---|
| done | Hard brand-exclusion | shipped | live | n/a |
| done | YouTube comment threads | shipped | n/a, ingest | n/a |
| 0 | GDELT Events, civil unrest | flip plus validate | new scoring signal | civil-unrest tag, later |
| 0 | GDELT GCAM emotion | one column, persist, flip | feeds sentiment | feeds sentiment |
| 0 | BQ Trends top_terms | flip | feeds scoring | feeds desk velocity |
| 0 | Search-velocity signal | env flip, careful | scoring weight 0.05 | velocity word |
| 0 | Forecast OUTLOOK chip | flip predictor | v2 card chip, exists | new outlook chip |
| 1 | Richer briefs | small, prompt only | sharper brief copy | sharper brief copy |
| 1 | Momentum 7d vs 30d | small, two cols plus lookup | momentum pill | momentum pill |
| 1 | Lifecycle phase | small, one col plus classifier | phase badge | phase badge |
| 1 | Classification instrumentation | small, col plus counts | invisible, ops | invisible, ops |
| 1 | Continuity badges | small, prior-day lookup | day2/day3/rebounding | day badge |
| 1 | Platform heat-map | small, one email section | new section | new desk panel |
| 2 | Social sentiment lexicon | medium, scorer plus lexicons | unlocks tone | unlocks tone |
| 2 | Tone/sentiment split | medium, depends on lexicon | pos/neu/neg split | sentiment bar |
| 2 | Pan-African story detection | medium, derived table plus stage | new section | new Stories view |
| 2 | Wikipedia pageviews | medium, new connector | new signal | new signal |
| 2 | Bluesky feed | medium, new connector | new signal | new signal |

Waves 1 and 2 are listed for context only. Each gets its own brainstorm, spec, and plan when Wave 0 is banked. This document specs Wave 0 in full.

## Wave 0, detailed spec

Five flips. No new scoring logic. One column migration. Lowest blast radius in the roadmap, so it ships first and proves the branch-flag-prove-out loop end to end.

### 0.1 GDELT Events, civil unrest

Change: flip `gdelt_events_enabled: false -> true` at `configs/sources.yaml:228`.

Code state: fully wired. `src/ingestion/connectors/gdelt.py:275` `fetch_events`, CAMEO country codes SAF/NGA/KEN, event root filter `^(14|15|17|18|19)$` for protest, force, coerce, assault, fight. Rows normalise to `content_type=gdelt_event` and concatenate onto the GKG rows.

Migration: none. Rows fit the existing `raw_content` schema.

Surface: the events flow into the existing scoring path as additional signal rows. Email needs no change for Wave 0. A dedicated civil-unrest tag on the Pulse is a Wave 1 or later follow-up, not in scope here.

Flag and prove-out: flip on, watch one cron. Check the new row volume on `raw_content` where `content_type='gdelt_event'`, and run a geo-collision sweep so SSA protest terms do not pull foreign noise.

Risk: row-volume spike and geo noise. Rollback: set the flag back to false, no schema to undo.

### 0.2 GDELT GCAM emotion

Change: add a `v2gcam STRING` column to `raw_content` and `enriched_content`, persist the parser output that today only logs, then flip `gcam_enabled: false -> true` at `configs/sources.yaml:223`.

Code state: `src/ingestion/connectors/gdelt.py:442` `_parse_gcam` already extracts the five dimensions (Hedonometer happiness, LIWC positive, LIWC negative, SentiWordNet polarity, and the secondary normalisation). The parser logs the parse rate and discards the value. A code comment records that the column was deferred until the signal was validated.

Migration: idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS v2gcam STRING` on both tables, applied before the persist code path goes live. Storage format is the GDELT `code:value` semicolon string, parsed downstream.

Surface: GCAM feeds the sentiment signal. It is plumbing for the Wave 2 tone split, not a standalone email or Pulse element in Wave 0.

Flag and prove-out: this is the textbook dark ship. Land the migration and the persist code with `gcam_enabled` still false, let one cron write the column on real rows, verify the column populates and parses, then flip the flag on a later cron.

Risk: low. The column is additive and nullable. Rollback: flag off, the column stays empty and harmless.

### 0.3 BigQuery Trends top_terms

Change: flip `top_terms.enabled: false -> true` at `configs/sources.yaml:102`.

Code state: fully wired. `src/ingestion/connectors/bigquery_trends.py:201` `_fetch_top_terms`, SQL at `infra/bigquery_queries/search_top_terms.sql`. Adds steady-state top search terms for ZA and NG, KE is off by config.

Migration: none.

Surface: feeds scoring and the desk velocity read. No dedicated section.

Flag and prove-out: flip on, watch one cron for the added rows and confirm no quota or scoring regression. Rollback: flag off.

### 0.4 Search-velocity signal

Change: set the `SEARCH_VELOCITY_ENABLED` cron secret to true.

Code state: fully wired. `src/ingestion/enrichment.py:45` gate, `src/scoring/velocity.py` consumes it, scoring weight 0.05 reserved at `configs/scoring.yaml:36`. When off, enrichment zeros the incoming `search_velocity_score`.

Landmine: `scripts/run_rss_now.py:813` warns the `search_velocity_score` WRITE_APPEND load can fail for ZA and NG the day the flag flips, because the load schema must already carry the column. The prove-out must confirm the column is present in the load before the env flips.

Migration: confirm the `search_velocity_score` column exists on the load target. If absent, add it dark first.

Surface: lifts the velocity word on the Pulse and the velocity component of the composite. No new section.

Flag and prove-out: confirm the column loads on one cron with the env still off, then set the env true on a later cron and confirm the WRITE_APPEND succeeds for all three markets. Rollback: env back to false.

Risk: the load failure at run_rss_now.py:813. Treated as the second-riskiest Wave 0 flip after forecast.

### 0.5 Forecast OUTLOOK chip

Change: set the `FORECAST_ENABLED` cron secret to true.

Code state: built and recent. BQML models `score_forecast_model` (ARIMA_PLUS, 7 Jun) and `forecast_btree` (boosted tree, 9 Jun) exist. Tables `score_forecast_7d`, `predictions_archive`, `forecast_supervised` exist. `src/scoring/forecast.py:121` `compute_forecast_outlook`, classification heating, steady, cooling at `:48`. The v2 email card renders the chip at `src/alerts/email_render/card.py:73` with glyphs for heating, steady, cooling. The gate is `scripts/run_rss_now.py:1100`, `FORECAST_ENABLED` default false, so the predictor does not run on the cron today and the forecast tables are empty for current dates.

Migration: none, the tables and models exist.

Surface: the v2 card chip already exists, it lights up the moment briefs carry `forecast_outlook`. The Pulse needs a small change: add `forecast_outlook` to the `fetch_desk_rows` SELECT and render it in `views.jsx`. This is the one Pulse change in Wave 0.

Flag and prove-out: flip the env, watch one cron populate `score_forecast_7d`, confirm the chip renders in the digest, and confirm the accuracy-watchdog `forecast_beats_persistence` check stops skipping. Rollback: env back to false, chip goes dark, no data harm.

Risk: highest in Wave 0. It adds a forecast compute step to the daily run and depends on the model staying valid. Flipped last in the cadence so the other four are already banked when it goes on.

### Wave 0 cadence

Staggered across roughly five crons, each gated by morning-check staying CLEAN before the next flip.

1. Dark ship the GCAM column migration and persist code, flag off.
2. Next cron, flip GDELT Events.
3. Next cron, flip top_terms and search-velocity together, both low-coupling once the load column is confirmed.
4. Next cron, flip GCAM on now that the column has banked a clean day.
5. Next cron, flip forecast OUTLOOK last.

If any flip turns morning-check non-CLEAN, halt, diagnose, roll that one flag back, do not proceed to the next.

### Wave 0 testing and vetting

- Unit tests for the GCAM persist path, since it is the only code change. The four pure flips are config and secret changes covered by existing connector and scoring tests.
- Full CI green on the branch before merge.
- For each flip, a morning-check run on the next cron, plus the targeted BigQuery checks named per feature above.
- Geo-collision sweep after the GDELT Events flip.

### Wave 0 Listening Post changes

Only the forecast OUTLOOK chip touches the Pulse in Wave 0. Add `forecast_outlook` to `src/api/bq.py` `fetch_desk_rows`, carry it through `desk.py` `_build_topic`, render it in `frontend/src/views.jsx` as an outlook chip on the brief panel. Its own branch and PR on the listening-post repo, gated by that repo's CI. Everything else in Wave 0 feeds scoring, which the desk already reads, so no Pulse change.

## Branch and flag strategy

Wave 0 engine and email work on a new branch off master, for example `feat/roadmap-wave0-flips`. The Listening Post change on a separate branch in that repo. Flags already exist for all five items, so the branch carries the GCAM column migration, the GCAM persist code, and the Pulse forecast render. The five flag and secret flips happen on the cron after merge, staggered per the cadence.

## Waves 1 and 2, sketch only

Detailed in their own specs when Wave 0 is banked.

Wave 1, small compute builds, each a new column plus logic plus both renders: richer briefs (feed the existing platform and creator breakdown into the brief prompt, same Gemini call), momentum 7d vs 30d (two new velocity windows beside the current 14-day baseline), lifecycle phase (trajectory classifier into a new column, distinct from the forward-looking forecast), classification instrumentation (record the winning classifier layer per row plus per-layer counts and a labelling-rate metric), continuity badges (revive the dead day counter via a prior-day trend_scores lookup), platform heat-map (aggregate the per-brief platform counts into one email section and one desk panel).

Wave 2, medium builds: social sentiment lexicon (a per-market lexicon scorer for the social rows that carry no tone today, which is 95 percent of the feed), tone/sentiment split (positive, neutral, negative, depends on the lexicon), pan-African story detection (a post-scoring stage and a derived cross-market table, plus a new Pulse Stories view), Wikipedia pageviews connector (new, free, no key), Bluesky connector (new, free, no key). New connectors follow the dark-ship connector pattern: connector module, sources.yaml block, a `pipeline_runs` row-count column, tests, ship dark, prove-out with a geo-collision sweep, flip.

## Assumptions

- Mailer v2 is the live email path in production. Confirmed by today's sent digest passing the v2 structure audit.
- The forecast BQML models stay valid when the predictor flips on. If the daily run shows model drift, forecast moves to its own remediation before flip.
- The Listening Post repo is available to branch and PR.

## Success criteria

Wave 0 is done when all five flags are on in production, five consecutive morning-checks read CLEAN, the GCAM column populates, the forecast chip renders in the digest and on the Pulse, the accuracy-watchdog forecast check is no longer skipping, and no geo leak appears after the GDELT Events flip.
