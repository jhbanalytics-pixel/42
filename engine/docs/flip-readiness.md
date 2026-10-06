# Dark-flag flip-readiness

Standing reference for every dark flag in the engine: when it is safe to flip, the proof to confirm after, and the blind risk if flipped without that proof. Validate before flip; never flip blind.

## 2026-07-23 state, vendor switch

EnsembleData was cancelled on 23 Jul 2026 and SocialCrawl became the primary social vendor. Every EnsembleData row in the roadmap below is now dead: those flags cannot be flipped, because there is no account behind them. They are kept as a record of what the surfaces were, so the equivalent SocialCrawl surface can be sized against them.

Flip discipline, holds for every flag in this doc: never flip an ingestion flag without BOTH a live vendor-shape probe AND a grep-confirmed downstream reader. No reader means dead ingestion.

The EnsembleData-era budget rule (one charging surface per cron day, recovery re-ingest double-spends against a hard 5000/day cap) no longer applies in the same shape. SocialCrawl is prepaid per call with no daily cap, cache hits and failed lookups bill nothing, so a recovery re-run is close to free. The constraint is now the balance, not the day: 20,000 credits at roughly 290/day is about 69 days. Check it with `GET /v1/credits/balance`, which costs nothing, and treat anything under 5,000 as a reorder trigger.

Live SocialCrawl surfaces, all probed 23 Jul 2026 (`docs/socialcrawl-probe-2026-07-23.md`): tiktok/trending, youtube/videos/trending, tiktok/search/top, youtube/search, threads/search, tiktok/profile/videos, instagram/profile/posts, threads/user/posts, reddit/subreddit, reddit/search, twitter/user/tweets, google_news/search, tiktok/profile/region.

Probed and deliberately NOT shipped: `google_trends/rising` (upstream timeout on probe, retry later), `reddit/omni-search` (bills 5 credits against a documented 1), `instagram/search/hashtag` (5 credits and no geo scoping, the London result on a ZA query).

Next SocialCrawl surfaces, in priority order: pagination on tiktok/search/top and youtube/search (biggest row lever, 1 credit per extra page), tiktok/post/comments and instagram/post/comments (restores comment sentiment), creator geo verification across the full watchlist, then bluesky and pinterest for breadth.

Free connectors flipped live since the 1 Jul snapshot, each three-market probed with a wired consumer:

| Connector | Flag | Live | PR |
|---|---|---|---|
| App Store top-charts | app_charts.enabled | 5 Jul 2026 | #239 |
| Cloudflare Radar top domains | cloudflare_radar.enabled | 4 Jul 2026 | #240 |
| Wikipedia per-country pageviews | wikipedia.enabled | 3 Jul 2026 | #232 |
| Google Trends RSS | google_trends_rss.enabled | 4 Jul 2026 | #234 |
| SocialCrawl multi-platform (eval) | socialcrawl.enabled | 3 Jul 2026 | #226 |

Twitter is live in all three markets now. `twitter_handles_enabled: true` for za, ng, ke with five probed handles each (ZA 2 Jul, NG and KE 3 Jul). The "NG and KE stay dark" notes further down are superseded.

The V3 seed-graph flags are all live in `configs/cron_flags.env`, past the staging gate: `SEED_GRAPH_ENABLED`, `NEAR_MISS_CAPTURE_ENABLED`, `SEED_PATH_RENDER_ENABLED`, `SEED_CANDIDATES_ENABLED` all true (#256 flipped candidates, #264 flipped the seed-path render and measured gate). The V3 Discovery Loop table at the foot of this doc is now historical; treat these four as live.

### Roadmap

READY, EnsembleData charging surfaces. Gate first, one per cron day, re-read `fetch_units_history` (0-unit call, `src/ingestion/connectors/customer_units.py`) the next morning as proof, not row counts:

| Surface | Flag | Note |
|---|---|---|
| creator tier_2 boost | creator_tier_cap_boost tier_2 | NG first. Pools trimmed (NG 41 / ZA 32 / KE 37), `CREATOR_INGEST_BOOST` already true, needs 2-3 clean fetch_units_history days first |
| Threads post replies | threads_post_replies_enabled | ZA. Per-post enrichment, cost scales on post count |
| IG post comments | ig_post_comments_enabled | NG then KE. ZA pilot already live, bounded to 5 calls per run |

BLOCKED, a prerequisite is missing, not a timing call:

| Flag | Blocker |
|---|---|
| semrush.enabled | no `SEMRUSH_API_KEY` in Secret Manager |
| audiomack.enabled | no OAuth consumer keys, genre slugs still docs-derived and unvalidated |
| Wave-3 seed-list flags: yt_keyword_search, yt_channel_videos, yt_shorts, yt_video_comments, ig_user_reels, ig_user_tagged_posts, tt_music_posts, tt_post_comment_replies, tt_post_info | matching seed arrays empty, so flipping is a silent no-op or a 422 that burns units |
| LANGUAGE_GUARD_ENABLED | needs a clean shadow re-run first |
| CORROB_COUNTS_ENABLED | no reader consumes the counts |
| SEED_BEHAVIOUR_EMAIL_ENABLED | the email section is not built |

DO-NOT-FLIP, leave off indefinitely:

| Flag | Reason |
|---|---|
| FORECAST_ENABLED | loses to the persistence baseline on walk-forward backtest |
| spotify.enabled | permanent 403/404 on client_credentials, superseded by apple_music |

## 2026-07-01 state

RSS: 11 dead publisher feeds across za/ng/ke replaced with live-probed alternatives (PR #212), fixing the partial-pipeline nights caused by 403/404/301s under the old default UA.

NEW DARK CONNECTOR: Semrush v4 Keywords API (PR #213). `sources.yaml` `semrush.enabled: false`, 9 seed keywords across za/ng/ke. No `SEMRUSH_API_KEY` provisioned yet (not in Secret Manager, not local `.env`). WHEN: get the key from the Semrush account, run `python scripts/verify_live.py semrush za mpesa` to confirm the response shape, then flip.

Live env: `RECONCILE_ENABLED=true` (PR #218 merged 1 Jul 2026; Cloud Build deploy pushes `configs/cron_flags.env` on next master deploy). Re-ground-truth after deploy with `gcloud run jobs describe trends-engine-pipeline`. Also: `FORECAST_ENABLED=false`, `LANGUAGE_GUARD_ENABLED=false`, `MAILER_ARCHIVE_BUCKET=ogilvy-pulse-archive-k7m3xq`, `CREATOR_INGEST_BOOST=true` with `creator_tier_cap: tier_1` in all three markets.

## 2026-06-27 state

INCIDENT + FIX: the corroboration scorer (PR #175) crashed the cron in `_aggregate_by_topic` on a str-vs-Timestamp `published_at` compare. Fixed by coercing `published_at` to a UTC Timestamp before the compare (#180). The day's run and digest were recovered. A new `dry_run_sql` registry entry plus the watchdog checks now guard the whole class so a type mismatch surfaces before it can crash a live cron.

INTELLIGENCE CORE SHADOW: ACTIVATED (PR #184, 27 Jun) then PAUSED (PR #201, 28 Jun) then RESUMED (PR #218, 1 Jul 2026). Shadow renders nothing; audit rows land in `event_ledger` + `reconcile_actions`. Ledger Gemini cost is ~3 batched calls/day (one per market), not per watched entity. Typical ~$3-5/mo; watch via morning-check $30 Vertex RED. The 10-clean-day promotion clock to Phase 1 (labels) starts with the 1 Jul resume. See `docs/archive/trends-engine-v3-blueprint-v3.1.md` Phase 0 (archived; current blueprint is v3.8).

GCP DEPLOY: cut over to Cloud Build (`cloudbuild.yaml` + the `trends-engine-deploy` trigger, `cloudbuild-deployer` SA). The GitHub auto-deploy is disabled. A GCP Cloud Scheduler fallback was added. The GitHub Actions schedule fallback is removed in this PR. Deploy and run are now GCP-native.

THE READ: the Listening Post now leads with the fresh engine read, and the stale Brand24 weekly was removed from THE READ entirely.

## Live as of 2026-06-24 (PR #166)

Day-1 activation wave, all four live-probed against the real vendor contract before flip:

- `youtube.playlist_items_enabled` true (za/ng/ke). Free, native quota ~12 percent. Proof: youtube_playlist_items rows ingest next cron.
- `brand24.include_ai_insights` true (za/ng/ke). Free inside Business tier, one call per project per run. Consumer SHIPPED same PR: the rows were dead ingestion (nothing read brand24_ai_insight), now generate_daily_summary pulls them (_fetch_brand24_insights) and injects a DATA-ONLY <market_intelligence> grounding block into the summary Gemini prompt. Proof: 4 narrative fields populate, rows feed briefs not trend_scores (platform=aggregate), and the daily through_line reflects Brand24's read.
- NOTE the Pulse tool (Listening Post) ai-insights endpoint is INDEPENDENT of this flag: it calls the Brand24 live API directly, not the TEV2 BQ rows.
- `ensembledata.tt_comments_enabled` true (za/ng/ke). ~75 units per run. The only new charging query_group this wave, so the next morning-check isolates its real per-call rate. Proof: tiktok_comment rows ingest, ensemble cap stays under ~70 percent.
- `gdelt.gcam_enabled` true (global). The v2gcam column already existed and the persist path was wired 28 May, so this was a one-line flip, NOT a migration (the old note below was stale). Free, ~735MB extra BQ scan per market per day. PROOF MET 2026-06-25 (first cron after the flag landed): v2gcam populated on 4545/4545 gdelt_gkg rows, 100%, valid code:value target dims (v10.1/v10.2/v19.1/v19.9). The 19-24 Jun zeros are pre-flip dark default. GOTCHA for any future check: GKG rows store `source = <publisher>` (news24.com, nation.africa), NOT 'gdelt'; only Events rows use source='gdelt'. Query gcam by `content_type='gdelt_gkg'`, never by source, or you get a false GKG-zero. READER SHIPPED 25 Jun 2026 (PR #167): v2gcam now folds into trend_scores as gcam_score, path mirrors tone exactly (parsed per row, weight-summed per topic, additive GDELT-only term with absent-weight redistribution). Ships at scoring.yaml gcam_score weight 0.00, so it is computed and stored but does not move the composite yet. Next: after 3+ days of non-zero gcam_rows, recalibrate `_GCAM_INTENSITY_BOUNDS` and raise gcam_score to 0.03 carved from tone_score per `docs/gcam-reader-scope.md`.

Bugfix same PR: ensemble threads `chunk_size` 1 -> 10. Threads creator pull was capped at one post per handle (threads_user 9 rows vs tiktok 174, instagram 189); resolve/pk/unwrap were all correct. Confirm threads_user climbs to ~90-140 next cron.

Already live before this wave: gdelt_events_enabled, brand24 Wave-2 (mentions_sentiment / reach / daily_metrics), ensembledata creator tier_1.

### Still dark, ranked (the remaining roadmap, do not forget)

1. `creator_tier_cap_boost: tier_2` (NG first). tier_1 banked a working creator pull from 17-Jun and is still what's live in all three markets (confirmed in sources.yaml 1 Jul 2026). tier_2 WAS cumulative and huge in all three markets (mostly an 11-Jun client micro-influencer seeding blob, not curated anchors), so it 495s mid-run. DONE 2026-07-01: all three creator files trimmed, seeding blobs moved to tier_3 (not deleted): `configs/creators/ng.yaml` tier_2 41 handles (tiktok 17, instagram 18, threads 6), `configs/creators/za.yaml` tier_2 32 handles (tiktok 14, instagram 13, threads 5), `configs/creators/ke.yaml` tier_2 37 handles (tiktok 18, instagram 14, threads 5). The flag itself is still NOT flipped anywhere; that stays a separate step so the trim can be reviewed first. Note the budget framing below was also wrong: `creator_tier_cap_boost` still needs `CREATOR_INGEST_BOOST=true` first, and that boost budget (code default 1500, no override in sources.yaml) is one ledger SHARED across za+ng+ke in run order, not a per-market slice. Real vendor-truth via `fetch_units_history` (0-unit call, `src/ingestion/connectors/customer_units.py`) showed tiktok+instagram+threads combined running 1700-3700 units/day already (a 27-28 Jun ZA `ig_post_comments` pilot pushed it to ~5007/5000 before PR #202's fix), so confirm 2-3 clean days of that real reading before flipping NG to tier_2 first, then re-check it the next morning as proof, not row counts.
2. Ensemble Wave-3 enrichment, one charging surface per day, measure cost shape first: `ig_post_comments`, `threads_post_replies` (per-post enrichment, no seed list, cost scales on post count), then the seed-list ones below.
3. Seed-list-gated Wave-3 no-ops (populate the matching array FIRST or flipping is silent): `yt_keyword_search` (yt_keywords), `yt_channel_videos` + `yt_shorts` (yt_channel_browse_ids), `tt_music_posts` (tt_music_ids), `ig_user_reels` + `ig_user_tagged_posts` (ig_user_ids), `tt_post_comment_replies`, `tt_post_info`, `yt_video_comments`.
4. `twitter_handles_enabled`: DONE all three markets. ZA 2026-07-02 (probe docs/twitter-probe-2026-07-02.md, WPP legal cleared same date, GraphQL unwrap shipped), NG and KE flipped 2026-07-03, five probed handles each.

Budget guardrail: max ONE charging EnsembleData surface per day. Units are not logged to BQ; the only live guard is the 495 breaker + next-morning cap percent. Flip slow, re-read cap each morning-check.

Note (2026-06-27): the remaining dark flags above still need their pre-flip prep done first (creator tier_2 pool trim, Wave-3 term-list population or per-surface cost probe, twitter handle probe), so they get flipped one ready surface at a time, never as a blind batch.

## Flip protocol

1. Confirm the flag's current value in `configs/sources.yaml` and `.github/workflows/deploy-cron-image.yml` before flipping.
2. Flip ONE flag at a time, in a quiet non-cron window.
3. Set the flag in all three homes or the next deploy clobbers it: `daily-trends.yml` env, `deploy-cron-image.yml` `--update-env-vars`, and the live `gcloud` job.
4. Watch the next cron and confirm the named proof for that flag.

## Env flags (deploy-cron-image.yml `--update-env-vars`)

FORECAST_ENABLED=false. WHEN: effectively never. The data is mean-reverting and loses to persistence (both ARIMA and the boosted-tree replacement failed walk-forward). Flip only if a new predictor beats persistence on a trailing 4-week backtest (the dormant WS3 FC-GATE), and only after migration 021 creates predictions_archive. Blind risk: degenerate chips that hug persistence.

LANGUAGE_GUARD_ENABLED=false. WHEN: re-run the shadow on a recent clean day and confirm the drop set is still about 0.9 percent, all genuinely foreign, zero SSA-slang false positives (the denylist must still exclude tl/id/so to protect Pidgin and Sheng). Flip on a clean cron day. The GKG v2locations geo-tag guard is a later phase, not a blocker. Blind risk: dropping legitimate Pidgin and Sheng rows.

## Connector flags (configs/sources.yaml)

gdelt_events_enabled=LIVE (already true in sources.yaml). Events stream wired in #91. Keep confirming total GDELT volume stays near the #124 expected (za about 1182, ng about 1389, ke about 603).

gcam_enabled=LIVE 2026-06-24 (PR #166). The v2gcam column already existed and the persist path was wired 28 May, so the "migration must land first" note was stale; this was a one-line flip. Probed ZA/NG/KE GKG GCAM live, returns the expected code:value tokens. PROOF MET 2026-06-25: v2gcam populated 4545/4545 gdelt_gkg rows on the first post-flip cron (100%). Query by content_type='gdelt_gkg', NOT source (GKG source is the publisher name). Still write-only: no reader consumes v2gcam (see the GCAM-reader scope below).

brand24 include_ai_insights=LIVE 2026-06-24 (PR #166). Probed clean (4 narrative fields return). The #122 platform=aggregate fix keeps these rows out of scoring. Confirm next cron the fields populate and feed briefs not trend_scores.

Brand24 Wave-2 surfaces (include_mentions_sentiment, include_mentions_reach, include_daily_metrics). WHEN: confirm the current per-project state first (some may already be on); for any still off, flip per-project and confirm the per-day rows populate and land in the brief trajectory. The #122 _resolve fix stops one project's opt-in leaking to siblings. Blind risk: low, mention-budget only.

ensemble twitter_handles_enabled: LIVE all three markets. ZA flipped 2026-07-02 (audit row 084 probe DONE, docs/twitter-probe-2026-07-02.md, envelope is GraphQL, WPP legal approved 2 Jul 2026 by Albert, legacy.favorite_count / retweet_count / reply_count added to the metric candidates, timeline-entry unwrap content.itemContent.tweet_results.result shipped with a unit test on the real probe envelope). NG and KE flipped 2026-07-03 with five probed handles each.

Ensemble Wave-3 creator-enrichment flags (tt/yt/ig/threads post comments and replies). WHEN: each needs its seed list populated in sources.yaml first (the matching ig_user_ids, yt_channel_browse_ids, yt_keywords, tt_music_ids lists, currently empty) AND the per-call real-units rate confirmed under the 5000/day Bronze cap. The id-extraction guards already shipped (#122). Blind risk: guaranteed 422s burning units.

creator_tier_cap_boost: tier_2. All three watchlists are trimmed now (NG 41 / ZA 32 / KE 37 curated tier_2 handles, done 1 Jul 2026). WHEN: confirm 2-3 clean days of `fetch_units_history` real vendor-truth (tiktok+instagram+threads combined, currently 1700-3700 units/day against the shared 5000/day Bronze cap) before flipping, then re-check that same reading the next morning as proof. Stage NG first, then the others. A guard test holds all markets at tier_1 until then. Blind risk: 495 quota exhaustion mid-run.

## Infra (a provisioning step, not a boolean)

MAILER_ARCHIVE_BUCKET (hosted full-read digest). WHEN: provision a bucket with allUsers:objectViewer (public read) first, smoke-test one object URL, THEN add MAILER_ARCHIVE_BUCKET=<bucket> to the durable Cloud Run `--update-env-vars` list AND daily-trends.yml env, or the next deploy clobbers it. Prerequisite already documented in DEVELOPMENT.md (#119). Blind risk: every recipient's full-read link 403s.

## Recommended flip sequence when we start

DONE 2026-06-24 (PR #166): youtube playlist_items, brand24 ai_insights, ensemble tt_comments, gcam, plus the threads chunk_size fix. gdelt_events and Brand24 Wave-2 were already live.

Next, in order: creator_tier_cap_boost tier_2 (NG first; pool trim to 41 handles done 1 Jul 2026, flag flip still pending a clean fetch_units_history read), then the Wave-3 enrichment flags one charging surface per day (ig_post_comments and threads_post_replies first, then the seed-list-gated ones after their arrays are populated), then twitter NG/KE (ZA live 2 Jul 2026, one market per cron day), then MAILER_ARCHIVE_BUCKET (after the bucket is provisioned). Leave FORECAST_ENABLED off indefinitely.

## V3 Discovery Loop (Track A), HISTORICAL, now live

Kept for the flip criteria record. As of 2026-07-08 all four seed flags are true in configs/cron_flags.env (SEED_GRAPH, NEAR_MISS_CAPTURE, SEED_PATH_RENDER, SEED_CANDIDATES), past the staging gate. Only SEED_BEHAVIOUR_EMAIL_ENABLED stays false (email section not built). The table below is the original dark-ship gate, not current state.

| Flag | Flip when | Proof |
|---|---|---|
| SEED_GRAPH_ENABLED | After dark merge CI-green + migration applied | bq-snapshot seed_graph rows za/ng/ke; PII spot-check; dry-run bytes in PR |
| NEAR_MISS_CAPTURE_ENABLED | One clean cron post-A7 merge | classify_batch parity test green; near_topic sample |
| SEED_PATH_RENDER_ENABLED | Two clean seed_graph days + prompt spot-check plan | Three briefs eyeballed; email unchanged with flag off |

See docs/archive/v3-staging-audit.md and docs/trends-engine-v3-blueprint-v3.8.md flip queue slot 2+.
