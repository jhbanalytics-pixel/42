# 42 QA log

One line per run from `scripts/lp_qa.py`. Watch for verdict drops, new findings, latency creep, and freshness lag. A recurring finding is a continuous-improvement candidate.

| date | verdict | freshest | slowest | findings |
|---|---|---|---|---|
| 2026-06-21 | HEALTHY | n/a | 585ms | none |
| 2026-06-21 | HEALTHY | 2026-06-21 | 15751ms | today slow: 8232ms (> 2500ms); rails slow: 2509ms (> 2500ms); desk slow: 12830ms (> 2500ms); voices slow: 6305ms (> 2500ms); intel_overview slow: 15751ms (> 2500ms) |
| 2026-06-22 | HEALTHY | 2026-06-22 | 12766ms | today slow: 7990ms (> 2500ms); desk slow: 12766ms (> 2500ms); voices slow: 5606ms (> 2500ms) |
| 2026-06-23 | HEALTHY | 2026-06-23 | 8755ms | today slow: 8755ms (> 2500ms); voices slow: 7383ms (> 2500ms) |
| 2026-06-23 | HEALTHY | 2026-06-23 | 7962ms | today slow: 7962ms (> 2500ms); rails slow: 2797ms (> 2500ms); voices slow: 7056ms (> 2500ms) |
| 2026-06-23 | HEALTHY | 2026-06-23 | 1001ms | none |
| 2026-06-30 | HEALTHY | 2026-06-30 | 15681ms | today slow: 2668ms (> 2500ms); rails slow: 2905ms (> 2500ms); desk slow: 15681ms (> 2500ms); intel_overview slow: 5387ms (> 2500ms) |
| 2026-07-01 | HEALTHY | n/a | 963ms | none |
| 2026-07-01 | HEALTHY | n/a | 2430ms | none |
| 2026-07-02 | HEALTHY | n/a | 596ms | none |
| 2026-07-02 | HEALTHY | n/a | 626ms | none |
| 2026-07-02 | DEGRADED | 2026-07-02 | 30249ms | today slow: 8385ms (> 2500ms); rails slow: 2504ms (> 2500ms); voices slow: 7659ms (> 2500ms); intel_overview: HTTP 0 |
| 2026-07-02 | HEALTHY | n/a | 532ms | none |
| 2026-07-02 | HEALTHY | 2026-07-02 | 5029ms | today slow: 2996ms (> 2500ms); intel_overview slow: 5029ms (> 2500ms) |
| 2026-07-03 | HEALTHY | n/a | 728ms | none |
| 2026-07-03 | DEGRADED | 2026-07-03 | 30223ms | today slow: 15230ms (> 2500ms); rails slow: 2585ms (> 2500ms); voices slow: 7657ms (> 2500ms); intel_overview: HTTP 0 |
| 2026-07-03 | HEALTHY | 2026-07-03 | 3694ms | today slow: 3397ms (> 2500ms); intel_overview slow: 3694ms (> 2500ms) |
| 2026-07-03 | STAGING LP-R | url=https://listening-post-staging-590353929363.us-central1.run.app | pass | API 200: health, seed-discover, seed-path amapiano, behaviours ZA; Discover pending ke 10 ng 1 (API cache; BQ ke 15 ng 8); Explorer cross-links live; UI checklist Albert |
| 2026-07-03 | STAGING LP-R | rev=00016-snt | pass | lp_qa HEALTHY: desk X in platform_heat, intel_overview 2.4s (parallel load), Discover/Explorer/seed-path green; deploy 26e2247+intel fix |
| 2026-07-03 | STAGING LP-R layout | rev=00018-h7f prod=00151-d4v | pass | page-shell/page-hero full width; workbench h1 11ch removed; map Discover node + auto-fill grid; bscan ZA duplicate fixed; vite build green |
| 2026-07-03 | HEALTHY | 2026-07-03 | 3694ms | today slow: 3397ms (> 2500ms); intel_overview slow: 3694ms (> 2500ms) |
| 2026-07-03 | HEALTHY | n/a | 3409ms | none |
| 2026-07-03 | HEALTHY | n/a | 521ms | none |
| 2026-07-03 | HEALTHY | n/a | 517ms | none |
| 2026-07-03 | HEALTHY | n/a | 2605ms | none |
| 2026-07-03 | DEGRADED | 2026-07-05 | 3680ms | today slow: 2840ms (> 2500ms); rails slow: 2738ms (> 2500ms); intel_overview slow: 2570ms (> 2500ms); seed_path slow: 3680ms (> 2500ms); metrics platforms=9 stale (desk has X; expect >=10 voice platforms) |
| 2026-07-03 | STAGING LP-R layout pass 2 | rev=00020-kp9 prod=00153-6pm | pass | topic.jsx hero why-paragraph 58ch cap widened to min(72ch,100%), full audit of all 26 .jsx files for the same narrow-cap pattern found only topic.jsx affected (creator.jsx shares the 1fr/auto hero grid but has no long-form paragraph so was never broken); lp_qa HEALTHY staging + prod; verified 1024/1280/1440px on two topics, dark and light theme |
| 2026-07-03 | STAGING V3 redesign | rev=00022-hhx branch=feat/v3-visual-redesign | pass | full v3 visual redesign live on staging: ui/ primitive library, per-route accent lanes (blue/green/yellow/violet), staged boot, skeleton desk loader, ProgressRail on brief generation verified live end to end (rail advanced 15/40/65/90/100 on real poll statuses, doc rendered), masthead+ticker verified 1440px and 1720px both themes scrolled and unscrolled, contrast gate green all four accent families, vite build green; transient mid-rollout render glitch observed by Albert during deploy window, not reproducible on settled revision |
| 2026-07-03 | STAGING V3 redesign parity | rev=00023-cr9 | pass | redeploy from PR #52 head (d37cac2); served index bundle hash byte-matches local build of the PR head, staging now exactly equals the review branch; verification pass from the 00022 row carries over unchanged |
| 2026-07-04 | STAGING V3 run 2 complete | rev=00028-4dk branch=feat/v3-visual-redesign | pass | full run-2 redesign: campaign progress contract (5/25/50/75/90/100) with forward-only clamp and focus-mode brief theatre proven live mid-generation; desk lead in fold at 1280x900, digest instrument panel with topic chips; topic dossier hero on PageHero+MetaRail, gap 0px at 1280, EmptyState for bad ids; console FlowBar pinned across landing/ask/brief with guarded CTAs, approval wall collapsed top-5 per market, active-thread answer detection, cancel during generation; SeedTrail thread across Seeds/Discover/Explorer; all "reading the signal" copy dead; 27-check DOM gate green on 00027, re-gated on 00028; contrast AA both themes all lanes; merge-gate review: ready, zero must-fix |
| 2026-07-04 | STAGING V3 critique fixes | rev=00029-jwl | pass | post-completion critique-ux fix wave: MarketChip renders code once (Windows flag-glyph fallback killed on Voices and market tiles), Explorer platform trail deduped render-side with matching platform count, junk id tokens filtered from adjacent terms, brand name unified to Nano Banana, seed explainer sentence fixed; spot probe green on all five |
| 2026-07-04 | STAGING V3 full handoff | rev=00037-t5n branch=feat/v3-surfaces-wave1 | pass | all 13 design references implemented and live: desk reveal, console workbench with behaviour proof strips, topic dossier instrument aside, intelligence loop with shared card grammar and trace path, voices/creator, graphs focus layer, lexicon decode panel, listen live sentiment feed, browse archive search on the previously unused ask endpoint, board honest last-look deltas, method rebuild, passcode v3 gate with loader vocabulary; 16-check final sweep green; build clean; contrast AA both themes all accents |
| 2026-07-05 | STAGING V3 round 2 | rev=00039-lms branch=feat/lp-r2-refinement PR #67 -> feat/lp-r-staging 90cf387 | pass | all 10 round-2 handoff items live: desk aside ask+digest tiers with DigestList and THE PLAY inset, console recents identity row (time spine, monochrome confidence ramp, artifact hash tiebreaker), theatre landed beat with v3develop doc entrance, dossier instrument readouts with honest axis, four loader empty homes (ScanSweep Discover, Orbit Network, SignalRings Board, LogDrum Listen) sharing the EmptyState primitive, ThemeDock in the masthead with compact popover below 1101px, ticker desk-only; 17-probe DOM suite 34/34 across both themes at 1280 plus 1101/1024 for the dock; full-route sweep 19 routes x 2 themes: zero horizontal overflow, zero console errors and failed requests except the pre-existing /api/__idle__ sentinel 404 (compare/lexicon/seedpath, predates round 2); bundle UI strings dash-free; 375 clean, one pre-existing 768 dossier sample-wall bleed fixed in the follow-up PR; contrast AA both themes; vite build green |
| 2026-07-06 | HEALTHY | n/a | 793ms | none |
| 2026-08-02 | HEALTHY | n/a | 677ms | none |
| 2026-08-02 | DEGRADED | 2026-08-02 | 4555ms | desk platform_heat missing X (twitter ingest or render_payload stale); seed_path slow: 4555ms (> 2500ms) |
| 2026-08-04 | HEALTHY | 2026-08-04 | 4224ms | desk platform_heat has X again - a known-gap platform is back, drop it from KNOWN_GAP_PLATFORMS; WARN seed_discover: future-dated value(s) 2026-08-09 in payload (excluded from the freshness stamp); seed_path slow: 4224ms (> 2500ms) |
| 2026-08-05 | HEALTHY | 2026-08-05 | 4607ms | today slow: 2777ms (> 2500ms); metrics slow: 3399ms (> 2500ms); WARN seed_discover: future-dated value(s) 2026-08-09 in payload (excluded from the freshness stamp); seed_path slow: 4607ms (> 2500ms) |
| 2026-08-06 | HEALTHY | 2026-08-06 | 3989ms | WARN seed_discover: future-dated value(s) 2026-08-09 in payload (excluded from the freshness stamp); seed_path slow: 3989ms (> 2500ms) |
| 2026-08-07 | HEALTHY | n/a | 169056ms | none |
| 2026-08-07 | HEALTHY | n/a | 168838ms | none |
| 2026-08-09 | HEALTHY | 2026-08-09 | 4112ms | today slow: 2574ms (> 2500ms); metrics slow: 3421ms (> 2500ms); seed_path slow: 4112ms (> 2500ms) |
| 2026-08-10 | HEALTHY | n/a | 169016ms | none |
| 2026-08-11 | HEALTHY | 2026-08-11 | 5022ms | WARN seed_discover: future-dated value(s) 2026-08-16 in payload (excluded from the freshness stamp); seed_path slow: 5022ms (> 2500ms) |
| 2026-08-14 | HEALTHY | 2026-08-14 | 4556ms | metrics slow: 3902ms (> 2500ms); intel_overview slow: 2851ms (> 2500ms); WARN seed_discover: future-dated value(s) 2026-08-16 in payload (excluded from the freshness stamp); seed_path slow: 4556ms (> 2500ms) |
| 2026-08-17 | HEALTHY | 2026-08-17 | 105713ms | today slow: 21982ms (> 2500ms); metrics slow: 43464ms (> 2500ms); desk slow: 22551ms (> 2500ms); voices slow: 105713ms (> 2500ms); intel_overview slow: 21861ms (> 2500ms); seed_discover slow: 64575ms (> 2500ms); seed_path slow: 4280ms (> 2500ms) |
| 2026-08-18 | HEALTHY | 2026-08-18 | 10505ms | intel_overview slow: 10505ms (> 2500ms) |
| 2026-09-04 | HEALTHY | 2026-09-04 | 2761ms | seed_path slow: 2761ms (> 2500ms) |
