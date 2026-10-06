# TEV2 roadmap, master flip runbook

Date: 2026-06-20. The one doc you act on. All 16 roadmap features are built, tested, CI-green, merged to master (engine) and main (Pulse), and the migrations are applied to production. Everything is dark. The daily digest, the trend_score, and the Pulse desk are byte-identical to before until you flip a flag below.

## Status

- Engine: Wave 0 (#138), Wave 1 (#140), Wave 2 (#141) merged to master. Deployed dark via the cron image rebuild.
- Pulse: Wave 0 (#1) and Wave 1+2 (#4) merged to main. Deployed, every new render guarded so it shows nothing until data exists.
- Migrations: all applied to ogilvy-trends-v2.trends_v2_dev and verified. raw_content +1, enriched_content +2, trend_scores +6, pipeline_runs +6, plus the pan_african_stories table. Nothing left to migrate.
- Composite: unchanged. Momentum is display-only. accuracy_watchdog composite-integrity stays HEALTHY.

So the only thing between here and live is flipping flags, which is yours.

## How to flip

Two kinds of flag, both flipped by a reviewed change to a committed file, NOT by a GitHub secret. (An earlier version of this doc said secrets; that was wrong.)

ENV flags (lifecycle, continuity, classification instrumentation, platform heat-map, sentiment lexicon, tone split, richer briefs, pan-African, momentum-in-composite, forecast) live in configs/cron_flags.env, the single source both cron paths read. Set the line to true (or add it), merge to master, and the deploy-cron-image workflow rebuilds the Cloud Run job env off the configs/ path filter, so the next 00:30 UTC cron picks it up. That file is not write-guarded, so these can be flipped via a PR.

INGESTION flags (gdelt_events, gcam, top_terms, wikipedia, bluesky) live in configs/sources.yaml. A PreToolUse hook blocks automated edits to that file, so you type these by hand, then commit and merge (the same configs/ path filter triggers the rebuild).

Flip in batches one cron apart, run the prove-out the next morning, keep morning-check CLEAN before the next batch. No code change or migration is needed first, those are done.

## Flipped so far (20 Jun 2026)

Live on the next cron, all additive (no trend_score or Gemini-prompt change):
- gdelt_events + top_terms (sources.yaml, PR #149); search-velocity was already live since 15 Jun.
- lifecycle badge, continuity badge, classification instrumentation, platform heat-map, sentiment lexicon stored-only (cron_flags.env, PR #150).

There is NO MOMENTUM_DISPLAY_ENABLED flag. The momentum pill in email_render/card.py renders unconditionally, so it has shown on the digest since momentum_label first populated (20 Jun). Add a code gate if you want it dark.

## Batch 1, the safe flips (Wave 0 ingestion)

| Flag | Where | Prove-out |
|---|---|---|
| gdelt_events_enabled | sources.yaml:228 false to true | raw_content rows with content_type='gdelt_event' for today |
| top_terms.enabled | sources.yaml:102 false to true | raw_content rows with content_type='top_term', ZA+NG, KE zero is correct |
| SEARCH_VELOCITY_ENABLED | secret = true | enriched_content.search_velocity_score nonzero for ZA/NG |

All three are SAFE per the Wave 0 audit, no prep. Watch the GDELT events volume and run a geo-collision spot check (NG economy_sapa_hustle, fashion_ankara_asoebi) after the events flip.

## Batch 2, GCAM (Wave 0)

| Flag | Where | Prove-out |
|---|---|---|
| gcam_enabled | sources.yaml:223 false to true | raw_content.v2gcam nonempty on fresh GKG rows |

The v2gcam migration is applied and the build_raw_row bridge is in the branch (conditional), so this is a clean flag flip now.

## Batch 3, Wave 1 display + instrumentation (configs/cron_flags.env)

Lifecycle, continuity, classification-instrumentation, and platform-heatmap are FLIPPED (PR #150, live on the next cron). RICHER_BRIEFS is HELD: it changes the Gemini brief prompt, so flip it alone to keep any brief-quality regression attributable. There is no MOMENTUM_DISPLAY flag; the pill already renders unconditionally (see "Flipped so far" above).

| Flag | Turns on | Prove-out |
|---|---|---|
| LIFECYCLE_ENABLED | lifecycle phase badge | trend_scores.lifecycle_phase populated |
| CLASSIFICATION_INSTRUMENTATION_ENABLED | per-layer counts + labelling rate | pipeline_runs.labelling_rate_percent populated; engine_pulse shows it |
| CONTINUITY_BADGES_ENABLED | new / day 2 / day 3+ / rebounding badge | trend_scores.continuity_state populated |
| RICHER_BRIEFS_ENABLED | platform breakdown into the brief prompt | briefs read sharper; same Gemini cost |
| PLATFORM_HEATMAP_ENABLED | platform heat-map section + Pulse panel | section renders |

MOMENTUM_IN_COMPOSITE is calibrated and a clean single env-flag flip (PR #145). When on, momentum (the min of the 7d and 30d windows, a sustained read) carves 0.07 from the velocity weight (velocity 0.20 to 0.13 plus momentum 0.07), so the bundle stays at 1.0, a one-day flash is demoted, and a sustained climb holds. scoring.yaml already carries momentum 0.07 dark. The accuracy_watchdog is carve-aware (PR #147), so composite_integrity stays HEALTHY across the flip, no precondition. The 20-Jun calibration ran on thin data (one live day plus backfill), so re-run scripts/calibration/momentum_calibration.py on a few more days of accumulating data, or run a short shadow period, before relying on it.

Note: velocity_score_7d and velocity_score_30d populate on the next cron with no flag (stored unconditionally), and the momentum pill renders from momentum_label directly (no flag gate).

## Batch 4, Wave 2 sentiment (env secrets)

| Flag | Turns on | Prove-out |
|---|---|---|
| SENTIMENT_LEXICON_ENABLED | social-row sentiment score | enriched_content.sentiment_lexicon_score nonzero on social rows |
| TONE_SPLIT_ENABLED | positive/neutral/negative split (email + Pulse) | split renders, reads the lexicon score |

Flip the lexicon first, confirm it scores, then the split. The lexicon term lists are seed-quality (about 50 terms per polarity per market); plan a tuning pass against real rows once it runs in shadow.

## Batch 5, Wave 2 connectors (sources.yaml, probed + validated)

Live-probed 20 Jun, both validated against the real APIs. Bluesky returned strong real content (ZA 96, NG 81, KE 45 posts). Wikipedia returned clean articles for ZA (8) and NG (6) after a skip-set fix (the wiki.phtml software artifact, PR #144); KE returns 0 because its per-country top-articles feed carries no Kenya-specific articles, so KE Wikipedia is expected-empty. Both are flip-ready. A re-probe is cheap if you want a fresh check right before the flag.

Paste these blocks into sources.yaml, then set enabled: true after the probe:

```
wikipedia:
  enabled: false
  days: 3
  limit: 40
  markets:
    za: {}
    ng: {}
    ke: {}
```

```
bluesky:
  enabled: false
  limit: 25
  sort: top
  markets:
    za:
      search_terms: [amapiano, kasi, mzansi, bafana]
    ng:
      search_terms: [afrobeats, naija, japa, nollywood]
    ke:
      search_terms: [gengetone, sheng, nairobi, mpesa]
```

Prove-out: pipeline_runs.wikipedia_rows and bluesky_rows nonzero after the flip. Until the block is present, both connectors return empty every run (cron-safe), so adding them disabled changes nothing.

## Batch 6, forecast and pan-African (need a touch more than a flag)

FORECAST_ENABLED (secret, Wave 0). The model, tables, persist line, and the v2 card chip are all in place. First run may show no chips for cold series (the lags 1/3/7 gate skips series under 8 days old). Confirm score_forecast_7d populates and the accuracy watchdog forecast_beats_persistence check stops skipping before relying on it.

PAN_AFRICAN_ENABLED (secret, Wave 2). The detection stage is built, tested, and now wired into the cron (PR #142): run_pan_african_stage(trend_date) runs after the per-market scoring and forecast block, env-gated and non-fatal. So this is a clean flag flip now. Prove-out: pan_african_stories rows for today, with markets arrays of length 2 or more. First days may be sparse until several markets carry rising families on the same day.

## Rollback

Every flag is runtime-gated and reverts on the next cron with no deploy. sources.yaml flags back to false, secrets back to false or deleted. The migrations are additive nullable columns and an empty table; they are inert while the flags are off and safe to leave.

## What is genuinely done vs needs a follow-up

FLIPPED 20 Jun (live on the next cron): gdelt_events, top_terms, search-velocity (already on), lifecycle, continuity, classification-instrumentation, platform-heatmap, sentiment-lexicon (stored only).

HELD for a deliberate later flip: RICHER_BRIEFS (Gemini prompt), TONE_SPLIT (after the lexicon banks real scores), PAN_AFRICAN (derived table, 2-market threshold unvalidated), MOMENTUM_IN_COMPOSITE (changes the live score, shadow first, PR #145 + watchdog carve-aware PR #147), FORECAST (predictor lost to a naive baseline 7 Jun).

YOURS (sources.yaml, hook-blocked): gcam_enabled (migration applied, ready), wikipedia and bluesky (paste the blocks above, both probed clean 20 Jun). No code follow-ups remain anywhere. The momentum pill renders ungated (no MOMENTUM_DISPLAY flag exists).
