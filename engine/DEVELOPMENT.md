# Trends Engine V2 ,  Contributor Guide

> Engineering conventions and project context for contributors. Read before making changes.

## Overview
Automated cultural trend detection pipeline for Google's Gemini campaign (Nano Banana + Lyria) across Sub-Saharan Africa (Nigeria 40%, South Africa 40%, Kenya 20%). Detects emerging trends, generates zero-edit Gemini prompts, and packages creator briefs for 250 influencers per trend cycle.

## Quick Start
```bash
# Run from an activated Python 3.13 environment.
python -m pip install -e ".[dev]"                                    # deps (matches CI)
python -m pytest tests/unit/ -W error -p no:cacheprovider --tb=short  # tests
```
The daily pipeline (`scripts/run_rss_now.py`) and any BigQuery / Vertex work run in the CLOUD, not locally: the BigQuery + Vertex SDK RPC path segfaults on this Windows + Python 3.13 box (see Known Gotchas). Locally, use the `bq` CLI for data reads; for a full run execute the Cloud Run job (`gcloud run jobs execute trends-engine-pipeline --project=ogilvy-trends-v2 --region=us-central1`). The run is GCP-native; GitHub holds no execution path.

## Tech Stack
- **Language:** Python 3.13 (CI matrix is 3.11 + 3.13 on ubuntu).
- **Cloud:** GCP. BigQuery (live dataset `trends_v2_dev`), Vertex AI, Secret Manager, Cloud Scheduler (00:30 primary + 02:30 fallback, both to the Cloud Run jobs `:run` API), Cloud Run Jobs (the sole runtime: cron + ops), Cloud Build (the sole deploy), Artifact Registry. GitHub has no execution path.
- **AI:** Vertex Gemini 3.5 Flash (Phase 2 trend briefs + daily_summary, on the global endpoint via `GEMINI_LOCATION`) + `text-multilingual-embedding-002` (M4 topic-classifier rescue on us-central1, live at cosine 0.65 / margin 0.05).
- **Dashboard:** Looker Studio (reads the `v_trend_briefs` + `v_pipeline_health` views).
- **Data Sources:** see the README.md connector table for the authoritative live list. Core set: RSS, Google Trends RSS, BigQuery Trends, YouTube Data API, GDELT (BQ GKG), SocialCrawl (TikTok / Instagram / Threads / Reddit / X / Google News), Brand24 (Business), Apple Music charts, plus App charts, Cloudflare Radar, Wikipedia. EnsembleData and its Reddit connector were retired 23 Jul 2026 when the vendor account was cancelled; both stay in the registry with their flags false, and `customer_units.py` (the EnsembleData quota watchdog) is dormant with them. `spotify.py`, `semrush.py`, `audiomack.py` and `pulsar.py` are wired but held dark.
- **Email:** PULSE v2 editorial mailer in `src/alerts/email_render/` (behind `MAILER_V2_ENABLED`, LIVE) via Gmail SMTP; the v1 ops digest in `src/alerts/email_digest.py` is the fallback.
- **Testing:** pytest + responses (mocked HTTP). **CI:** GitHub Actions, `.github/workflows/ci.yml` only (lint, test, scan; ubuntu-only since 30 May). **Deploy:** Cloud Build on master push, not GitHub. **Quality:** ruff + bandit + qlty, via pre-commit + CI.

## Project Structure
```
src/
├── ingestion/
│   ├── connectors/     # live set (see README table): rss, bigquery_trends, youtube, gdelt, socialcrawl,
│   │                   #   brand24, apple_music (+ ensemble/reddit/customer_units retired 23 Jul 2026)
│   ├── enrichment.py   # GDELT V2Tone parse, market assignment, full_text build
│   └── validation.py   # data quality checks
├── enrichment/         # topic classification (top-level, separate from ingestion/enrichment.py)
│   ├── topic_classifier.py    # regex + GDELT-theme + slang taxonomy match
│   └── embedding_classifier.py # M4 multilingual-embedding rescue (Vertex)
├── scoring/            # weighted 10-signal composite (see Scoring Algorithm)
├── analysis/           # Phase 2 Gemini brief layer
│   ├── gemini_client.py     # google-genai (Vertex) structured-JSON client
│   ├── display_layer.py     # PULSE display bundle (state/phase/window/confidence/search)
│   ├── generate_briefs.py   # orchestrator: BQ sample -> Gemini -> trend_analysis
│   └── prompts/             # trend_brief prompt + RESPONSE_SCHEMA
├── alerts/
│   ├── email_render/   # PULSE v2 editorial mailer (masthead/lede/hero/card/kit/radar/footer)
│   ├── email_digest.py # v1 ops digest + the PULSE flag-gated send path
│   └── detector.py     # tier-upgrade spike detection
├── reporting/  tracking/   # reports + KPI tracking (lighter use)
└── utils/              # bigquery wrapper, secrets, log_redactor, geo_blocklist, config loader
```

## Data Flow (live; the original Pub/Sub + Cloud-Run-Functions design was never built)
```
Trigger (GCP-native; the run_rss_now per-market idempotency guard skips a market already done):
  Cloud Scheduler 00:30 UTC -> Cloud Run Job  <- the primary daily runner (~02:30 SAST)
  Cloud Scheduler 02:30 UTC -> Cloud Run Job  <- fallback; guard-skips once the primary wrote today's success row
    -> python scripts/run_rss_now.py (single sequential script, not a Pub/Sub fan-out):
       1. ingest   all connectors          -> raw_content
       2. enrich   topic classify + tone   -> enriched_content (topic_groups ARRAY<STRING>)
       3. score    10-signal composite     -> trend_scores  (per market, topic_group)
       4. brief    Gemini top-8/market     -> trend_analysis (Phase 2, behind PHASE_2_ENABLED)
       5. email    PULSE v2 digest         -> Jo + Thapelo + Albert (behind MAILER_V2_ENABLED)
```

## Scoring Algorithm (26 May 2026 retune, 10 signals, sums to 1.00)
```
trend_score = 0.20 velocity          # rising momentum vs 14-day baseline
            + 0.17 genz_score         # Gen Z language / context
            + 0.12 watchlist_score    # tracked Gen Z creator mentions
            + 0.10 engagement         # views+likes+comments+shares
            + 0.10 slang_score        # local slang (pidgin / Sheng / Mzansi)
            + 0.08 diversity          # platform / content-type spread
            + 0.08 regional_score     # market-specific markers
            + 0.05 creator_spread     # distinct creators
            + 0.05 search_velocity    # rising search intent (BigQuery Trends); dark, see note
            + 0.05 tone_score         # GDELT V2Tone
```
Source of truth: `configs/scoring.yaml`. A topic with zero GDELT rows redistributes the 0.05 tone weight across the other 9 signals (see the tone-fallback gotcha), it does NOT apply a neutral 0.5. `search_velocity` carries its 0.05 weight but its value is 0.0 unless `SEARCH_VELOCITY_ENABLED=true`. The signal is now plumbed end-to-end (`bigquery_trends._normalise_row`, `run_rss_now.build_raw_row`, `enrichment.py`) behind that flag, default off, so the cron is unchanged until it is flipped.

## Configuration
- `configs/keywords/{za,ng,ke}.yaml` - Per-market topics, markers, slang terms
- `configs/creators/{za,ng,ke}.yaml` - Per-market creator watchlists (3 tiers)
- `configs/sources.yaml` - API endpoints, RSS feeds, query budgets
- `configs/scoring.yaml` - Composite weights, thresholds, baseline period
- `configs/trend_cycles.yaml` - Planned + ad-hoc cycles with dates/markets (brief says both 11 and 12; deliverables section says 12)
- `configs/alerts.yaml` - Spike thresholds, notification channels

## Environment Variables (GCP Secret Manager in prod, .env locally, mounted as env on the Cloud Run Job)
```
# secret names, with values supplied by Secret Manager or an ignored local environment
YOUTUBE_API_KEY
SOCIALCRAWL_API_KEY
BRAND24_API_KEY
SEMRUSH_API_KEY
GMAIL_USER
GMAIL_APP_PASSWORD
EMAIL_RECIPIENTS
# config
GCP_PROJECT=ogilvy-trends-v2   BIGQUERY_DATASET=trends_v2   TRENDS_ENV=dev   VERTEX_LOCATION=us-central1
# feature flags (all true in prod as of 31 May)
EMAIL_ALERTS_ENABLED=true   PHASE_2_ENABLED=true   EMBEDDING_CLASSIFIER_ENABLED=true   MAILER_V2_ENABLED=true
# dark flags (default false; plumbed end-to-end, not yet flipped live)
SEARCH_VELOCITY_ENABLED=false   LANGUAGE_GUARD_ENABLED=false
# optional
MAILER_ARCHIVE_BUCKET=   # GCS bucket for the hosted full-read link; inert when unset
```
`get_dataset()` returns `{BIGQUERY_DATASET}_{TRENDS_ENV}` unless `TRENDS_ENV=prod`, so dev + `trends_v2` resolves to the live `trends_v2_dev`. No `SLACK_WEBHOOK_URL`: Slack is not used for TEV2 (stakeholder comms are MS Teams).

Prerequisite before setting `MAILER_ARCHIVE_BUCKET`: the bucket must already grant `allUsers:objectViewer` (public read). The mailer returns a `https://storage.googleapis.com/<bucket>/...` link and never makes the object public itself, so without that grant every recipient's full-read link 403s. Provision the grant first, then smoke-test one uploaded object URL in an incognito window before you set the env var.

## Key Conventions
- **Markets:** Always use lowercase ISO codes: `za`, `ng`, `ke`
- **Dates:** All timestamps in UTC; display in SAST/WAT/EAT per market
- **Secrets:** NEVER hardcode API keys. Use `src/utils/secrets.py` to GCP Secret Manager
- **Logging:** Structured JSON via `google-cloud-logging`. No `print()` statements
- **Log redaction:** All logs pass through `src/utils/log_redactor.py` to strip API tokens
- **Configs:** All YAML validated against JSON Schema at startup via `config_loader.py`
- **Connectors:** All extend `src/ingestion/connectors/base.py` (retry, rate limit, logging)
- **Error handling:** Transient errors (429, 5xx) to retry with backoff. Config errors to halt pipeline. Data quality to warn + continue
- **Testing:** Target 80% coverage. Mocked HTTP responses for connectors. Known-input/output for scoring

## Writing Style

No em dashes or en dashes in prose. Short direct sentences. No filler openers, no trailing summaries. Commit messages are declarative and written in plain developer voice. Code comments explain why, not what. Docstrings are one line where possible, tight parameter docs where needed.

## Verified Costs (April 2026)
- SocialCrawl prepaid credits: 20,000 bought 23 Jul 2026, ~290/day steady state, ~69 days runway. Balance check is `GET /v1/credits/balance` and costs nothing.
- EnsembleData Bronze (5,000 units/day, ~R3,700/mo): CANCELLED 23 Jul 2026, replaced by SocialCrawl above
- YouTube Data API v3: Free (10,000 quota units/day, no charge)
- GDELT: Free
- RSS feeds: Free
- BigQuery Google Trends public dataset: Free (daily rising terms per country, covers international markets including ZA/NG/KE, needs schema verification)
- SerpAPI Google Trends (if needed for related_queries): $75/mo (~R1,400/mo) for 5,000 searches (Developer tier)
- GCP managed services (BigQuery, Cloud Run, Pub/Sub, Scheduler): ~R500/mo at our volume (budget buffer; actual likely ~R200)
- Vertex AI Gemini 3.5 Flash: ~$16/mo for Phase 2 briefs + daily_summary (output includes billed thinking tokens; on gemini-2.5-flash this was ~$5/mo)
- Looker Studio Pro (for auto-refresh): R170/mo ($9/user). Free tier needs Chrome extension for TV display
- GCP g2-standard-8 with L4 GPU (Phase 2): ~R11,500/mo on-demand, ~R7,250/mo with 1-year commitment
- Phase 1 total: R4,470/mo (15% of R30K ceiling)
- Phase 2 total: R11,720/mo committed, R15,970/mo on-demand (39-53% of R30K ceiling)
- Brand24 Business (ACTIVE 24 Apr 2026): $798/mo (verified on the subscription page 24 Jul 2026; renews 24 Aug), 25 keyword slots (8 + 9 + 8 = 25/25 used), 100K mentions/month. Projects rebuilt with conversation-area keywords + per-keyword noise exclusions on 24 Apr PM. ZA ships with: amapiano, mzansi, braai, matric, kasi, stokvel, nsfas, eskom. NG: naija, afrobeats, nollywood, japa, owambe, sapa, jollof, lagos, burna boy. KE: maandamano, nairobi, mpesa, gengetone, matatu, 254, sheng, nyama choma. Pulls via `src/ingestion/connectors/brand24.py`, projects declared per market in `configs/sources.yaml`.

## Project State

This guide stays stable: architecture, conventions, commands, gotchas. The engine is Phase 2 live (Gemini briefs on every cron), the PULSE v2 mailer is live behind `MAILER_V2_ENABLED`, the multilingual-embedding classifier is live, the full connector set runs (see the README table), and the daily cron runs on a Cloud Run job (Cloud Scheduler 00:30 primary + 02:30 fallback, both to the `:run` API; GitHub holds no execution path). The dated log below is historical build context, kept for reference; do not read it as current state.

## Build History (Apr to May 2026, historical)

Phase 1 closed. 6 connectors live on GitHub Actions cloud cron, writing to `trends_v2_dev`: RSS, BigQuery Trends public dataset, YouTube (fresh key on `ogilvy-trends-yt` project for independent quota), GDELT on BQ GKG, EnsembleData (Bronze, 5000 units/day), Brand24 (Business, 25 keywords, 100K mentions/month, 3 projects with rebuilt conversation-area keyword sets). Daily 06:30 UTC / 08:30 SAST via `.github/workflows/daily-trends.yml` with 11 GitHub secrets + `trends-v2-github` GCP service account. Local Windows task still fires as backup pending confirmation of 2 consecutive clean cloud runs.

**Scope B (topic clustering refactor) SHIPPED 24 Apr PM.** Hard cutover from source-level `query_group` aggregation (tiktok_hashtag, brand24_topics) to topic-level aggregation. Each enriched row carries a `topic_groups ARRAY<STRING>` column populated by `src/enrichment/topic_classifier.py` against per-market YAML taxonomies in `configs/topic_groups/{za,ng,ke}.yaml` (24 topic groups total, 8 per market). Scoring aggregates per `(market, topic_group)` with 1/N fractional weighting on multi-assigned rows. `pipeline_runs.unclassified_rows` tracks coverage drift per market; email digest footer surfaces the total.
**Post-ship hardening shipped 27 Apr 2026:**

1. **YouTube videos.list batch fix** (`src/ingestion/connectors/youtube.py`): chunked id list to 50 (was sending the whole batch URL-encoded which truncated the request mid-id-list and 400'd on stats enrichment). YouTube views populate 91-96% per market in cloud runs, up from 2-7%.
2. **Brand24 KE hashtag flag** (`configs/sources.yaml` + `src/ingestion/connectors/brand24.py`): per-project `include_hashtags: false` skips the call for KE only after Brand24's endpoint started returning HTML login pages on every request (even with retry). Email out to support; ZA + NG hashtags untouched.
3. **Threads connector unwrap** (`src/ingestion/connectors/ensemble.py`): `_unwrap_threads_post()` walks `node.thread.thread_items[0].post` so the existing extractors see consistent shape. Pre-fix every Threads row landed empty; post-fix 20/20 sampled posts return populated text + url + author_handle. Threads-specific URL builder (`https://www.threads.net/@{username}/post/{code}`) added.
4. **Velocity baseline backfill repair**: 24 Apr 2026 had 4 pipeline test-runs (22,770 enriched rows vs normal ~3K) which produced 24 inflated topic-level backfill rows in `trend_scores`. Surgical `DELETE FROM trend_scores WHERE trend_date='2026-04-24' AND DATE(scored_at)>='2026-04-25'` restored real per-topic baselines (sample post-delete: ng/music_afrobeats today=103 vs 14d baseline 61 → velocity 0.17, ke/fintech_mpesa today=57 vs 27 → velocity 0.27).
5. **Looker `v_market_comparison` rewritten** as per-market top-10 leaderboard (the wide pivot was meaningless on disjoint topic taxonomies). Cross-market comparison deferred to Phase 2 meta-cluster mapping (internal design notes). Deploy via `python scripts/create_looker_views.py`.
6. **SQL hardening pass** (`src/alerts/detector.py` + `src/utils/bigquery.py` + `scripts/setup_bigquery.py`): bandit B608 false positives now structurally avoided via bound `QueryJobConfig` parameters for caller-supplied values + `_validate_identifier()` / `_ALLOWED_*` whitelists for table / column / market identifiers. SHA1 in `bigquery_trends.build_row_id` marked `usedforsecurity=False`.
7. **Quality gate wired**: pre-commit hook + qlty + CI Quality job (see Known Gotchas).
8. **CI/CD hardening** (`.github/workflows/{ci,daily-trends}.yml`): top-level `permissions: contents: read` + `persist-credentials: false` on every checkout. Closes 13 zizmor findings (excessive-permissions, artipacked).

237 tests green under `-W error -p no:cacheprovider` as of 27 Apr; 251 in scope after Phase 2 wiring shipped 5 May. Bandit clean under `.bandit`. qlty `check --all` reports 2 informational TODO/NOTE markers, 0 actionable issues. Cloud cron fires daily 06:30 UTC; running cleanly through 4 May.

**Phase 2 (Vertex Gemini brief layer) prep + wiring shipped 5 May 2026 (master d2df0b8), gated behind `PHASE_2_ENABLED` env flag.** Until the flag flips on the cron the daily run produces the same v1 email it does today. Components live in the repo:

- `src/analysis/gemini_client.py`: thin wrapper around the new `google-genai` SDK (the unified client supersedes `vertexai.generative_models` retiring June 2026). Forces structured-JSON output via `response_mime_type` + `response_schema`. Logs token counts + estimated cost per call.
- `src/analysis/prompts/trend_brief.py`: single prompt template producing the per-topic brief in Jo's PDF mock shape: `description_rationale` (2 paragraphs), `activation_idea` (1 paragraph), `key_metrics` (3 strings), `platforms`, `sentiment_summary`, `status_tag` ("Key" or "Rising").
- `src/analysis/generate_briefs.py`: orchestrator. Queries top N (market, topic_group) per market from today's `trend_scores`, samples 10 enriched rows + 5 top creators per topic via bound BQ parameters, calls Gemini per topic, persists to `trend_analysis`. `TopicBrief` dataclass owns BQ-row mapping. Failures isolate per topic; one bad call does not kill the rest.
- `scripts/migrations/add_brief_columns_to_trend_analysis.py`: adds 3 columns (`platforms ARRAY<STRING>`, `sentiment_summary STRING`, `status_tag STRING`) to `trend_analysis`. Idempotent ADD COLUMN IF NOT EXISTS, no DEFAULT. Other 3 Brief fields map onto existing columns. **Apply pending user go before flipping `PHASE_2_ENABLED=true`**.
- `scripts/run_rss_now.py`: Phase 2 block sits between scoring and email digest, reads `PHASE_2_ENABLED` env var, default off. Briefs returned in-memory as `dict[(market, topic_group)] -> dict` and threaded into `send_daily_digest(briefs_by_topic=...)`. Phase 2 failures swallowed + logged; never block the email path.
- `src/alerts/email_digest.py`: `send_email_digest`, `render_html`, `render_text` all gain optional `briefs_by_topic` kwarg with `None` default. New `_render_brief_block_html` + `_render_brief_block_text` produce a "Daily briefs" section grouped per market with status badge (Key red, Rising blue), description, activation idea, key metrics, platforms, sentiment summary. HTML escapes user content.
- Cost envelope: ~$5/month at 24 briefs per day across 3 markets × 8 topics. Inside the existing GCP billing account. No new vendor or contract.

**Outstanding before flipping `PHASE_2_ENABLED=true`**:

1. Run `python scripts/migrations/add_brief_columns_to_trend_analysis.py --apply` to extend `trend_analysis` schema.
2. Confirm the GCP card on file is current. The billing owner holds `roles/billing.admin` on the billing account.
3. Set `PHASE_2_ENABLED=true` on the GitHub Actions secret + on local `.env` for testing.

**Outstanding but not blocking Phase 2 itself**:
- Thapelo Looker dashboard rebind on the new `v_market_comparison` shape. Confirmed actioning in Teams 4 May.

Post-ship improvements shipped 23 Apr:
- GKG V2Tone, V2Persons, V2Organizations, V2Locations persisted on raw_content + enriched_content.
- GDELT synthetic `text` from themes + persons + orgs + source so enrichment's full_text is non-empty; regional / genz / slang scoring now fires on news rows.
- Ensemble Threads endpoint (`/threads/keyword/search`) reinstated. Yaml had `threads_keywords` terms since the MVP port but endpoint was missing from DEFAULT_ENDPOINTS.
- Tone as 10th scoring signal. Weight 0.05 carved from velocity 0.20 -> 0.17 and diversity 0.15 -> 0.13. Groups with zero GDELT rows redistribute the tone weight across remaining 9 signals rather than applying a neutral 0.5 bias.
- Telegram alerts were live briefly on 23 Apr AM (commits 477f775, ad5143b, 8672b4e). Telegram anti-spam froze the bot mid-rollout after burst test sends across 2 chat_ids. Ripped out in commit 7f40bf4 and replaced with Gmail SMTP in `src/alerts/email_digest.py` + `src/alerts/detector.py` (commit b73df51). Single HTML + plain-text multipart email per pipeline run from `jhb.analytics@gmail.com`, one section per market sorted by upgrade count then by trend_score desc, plain-English reasons replace the old signal math dump, key people / orgs / article links per item. Recipients via `EMAIL_RECIPIENTS` (currently Jo, Thapelo, Albert). Gated by `EMAIL_ALERTS_ENABLED=true`. No email fires when no tier upgrades in the run.

223 unit + integration tests green under `-W error -p no:cacheprovider`. Pipeline elapsed 425-900s (varies by Ensemble budget spend). Cloud cron via GitHub Actions at 06:30 UTC + local Windows task `trends-engine-v2-daily-0830` as backup. `scripts/run_rss_now.py` is still the interim runner; Phase 1.9 promotion into `src/ingestion/orchestrator.py` precedes Phase 2 (Gemini analysis + creator briefs). Scope B topic clustering refactor is sequenced ahead of Phase 2 per Jo's feedback.

## Known Gotchas

**Every gotcha below that starts with "Ensemble" is HISTORICAL from 23 Jul 2026.** The EnsembleData account was cancelled; those endpoints, the 495 / 493 codes, and the unit-budget mechanics no longer apply to any live code path. They are kept because the connector is still in the registry and the notes explain why it looks the way it does.

- **SocialCrawl surfaces must be probed, never trusted from the spec.** Four spec-following configurations return HTTP 200, `success: true`, and zero rows: `tiktok/search` with any `date_posted` or `region` value, `youtube/search` with `uploadDate=week` (the enum is `this_week`), `instagram/profile/posts` with `trim=true`, and `reddit/omni-search` bills 5 credits against a documented 1. Full evidence in `docs/socialcrawl-probe-2026-07-23.md`. Re-probe after any vendor release.
- **SocialCrawl returns three different timestamp formats.** ISO on most surfaces, `2026-07-23 10:06:18 +00:00` on `google_news` (DataForSEO underneath), and `Wed Oct 30 05:45:03 +0000 2019` on `twitter/user/tweets`. `_parse_published` handles all three and returns `""` rather than a garbage value. An unparseable date is KEPT, not dropped: several surfaces legitimately omit it and dropping them guts the feed.
- **`twitter/user/tweets` is unsorted and has no recency filter.** It returns up to 99 tweets with the oldest first, `author.username` null, and no way to ask for recent ones. `max_age_days` plus the requested-handle fallback are the only things stopping six-year-old posts landing in `enriched_content`.
- **SocialCrawl billing is forgiving in three useful ways.** Cache hits bill 0, so a same-day re-run is close to free (unlike the EnsembleData recovery double-spend). `RESOURCE_NOT_FOUND` on a dead handle is auto-refunded. Upstream timeouts are refunded. The ledger charges `max(quoted, reported)` so an omitted `credits_used` still advances the breaker.
- **The SocialCrawl credit leak is stale-only responses, not dead handles.** Because 404s and empty responses are refunded (bullet above), pruning them saves nothing. The group that actually bills is a handle that resolves, returns real posts, and has every post older than `socialcrawl.max_age_days` (21): the vendor charges 1 credit and `_too_old()` then discards every row. Invisible in `pipeline_runs.errors` because the call was a clean HTTP 200. Probed 28 Jul 2026: of 83 watchlist handles with zero rows across four consecutive crons, 29 were `RESOURCE_NOT_FOUND`, 19 returned empty, and only the remaining 35 cost anything. Audit by probing, not by reading the error array.
- **`raw_content.source` is the publisher or author name, not the connector.** `source='SocialCrawl'` matches zero rows; the column holds values like `ewn.co.za` and `Davido`. Filter on `platform` (tiktok / instagram / threads / reddit / facebook / twitter / google_news) instead.
- **`raw_content.query_term` holds the handle the connector ASKED for; `author_handle` holds the post's actual author.** They diverge on reposts and tagged posts, so an NG instagram watchlist pull legitimately yields `author_handle` values like `shakira` or `billboard`. To audit watchlist coverage, diff the configs against `query_term`. Diffing against `author_handle` produces phantom handles that are not in any config.
- **`pipeline_runs.errors` does not capture every connector failure.** On 29 Jul 2026 apple_music timed out on both retries for NG and wrote 0 rows, while that run's error array held only socialcrawl entries. `scripts/engine_pulse.py` CROSS-MARKET-ZERO caught it; the run record did not. Never read an empty error array as "all connectors healthy".
- **An empty SocialCrawl account looks exactly like an empty day.** This is how the eval channel died unnoticed on 8 Jul 2026: the free 100 credits ran out and the connector logged nothing distinctive. `INSUFFICIENT_CREDITS` now sets a global halt and writes to `pipeline_runs.errors`.
- **Cloud Build and Cloud Run for this project are REGIONAL (`us-central1`).** A bare `gcloud builds list` queries the GLOBAL location and returns builds last seen 3 Jul 2026, which reads exactly like "the deploy trigger is dead" when it is healthy. Always pass `--region=us-central1` to `gcloud builds list`, `gcloud builds describe`, and `gcloud run jobs describe`.
- **`rows` is a BigQuery reserved word.** `SELECT COUNT(*) rows` fails with `Syntax error: Expected end of input but got keyword ROWS`. Alias to `n`.
- **`trend_analysis` timestamp column is `analyzed_at`, not `created_at`.** Vertex token-spend queries fail with `Unrecognized name: created_at` if you guess. `event_ledger` uses a third name, `generated_at`; same guess, same error (9 Aug 2026).
- **BigQuery rejects a subquery over a table inside a MERGE `ON` clause:** `400 Unsupported subquery with table in join predicate`. `persist_render_payloads` correlated a `SELECT MAX(analyzed_at)` against the source alias in `ON` and therefore raised on EVERY run from at least 6 to 9 Aug 2026, 16 log lines and zero successes, while its caller logged the failure as non-fatal so nothing ever surfaced. The enrichment silently never reached BigQuery and `render_payload` kept only its `display` key. Resolve the latest-row lookup inside `USING` and join it in as a source column, which keeps the semantics and leaves `ON` subquery-free. Fixed in PR #295.
- **DML is invisible to unit tests, and to `dry_run_sql.py` unless registered.** Unit tests assert on the SQL string and never execute it, and a non-fatal caller swallows the runtime error, so a broken MERGE or UPDATE can fail every day for a week with nothing going red. Add every new live query to `REGISTRY` in `scripts/dry_run_sql.py`, DML included. `generate_briefs.persist_render_payloads` was added there 9 Aug 2026 after exactly this.
- **`market` is stored lowercase, so `market='NG'` returns zero rows and no error.** Valid query, filter never matches, reads identically to a dead feed. Use lowercase literals or `LOWER(market)`. VERIFIED 10 Aug 2026 on `enriched_content` for 2026-08-10: grouping by market returns `ke` 2,659 / `ng` 4,664 / `za` 4,153, and the same query with `market='NG'` returns 0.
- **`enriched_content.topic_groups` is `ARRAY<STRING>`, not a string.** `topic_groups LIKE '%economy_sapa_hustle%'` fails with `No matching signature for operator LIKE for argument types: ARRAY<STRING>, STRING`. Filter with `, UNNEST(topic_groups) AS tg ... WHERE tg IN (...)`. This one errors loudly rather than returning empty, so it is the friendly member of the family.
- **When the `bq` CLI times out but the pipeline scripts still work, use the Python SDK, not the other way round.** On 10 Aug 2026 every `bq` call died with `TimeoutError(10060)` while `dry_run_sql.py`, `accuracy_watchdog.py` and `engine_pulse.py` all reached BigQuery fine in the same session through `src.utils.bigquery.get_client`. `curl` hits `bigquery.googleapis.com` in 0.35s and DNS answers IPv6-first, so the fault is the CLI's own socket path, not the network or credentials. This INVERTS the standing "use the bq CLI, never the SDK" gotcha below on days when the CLI is the broken one; a short standalone SDK script is the fallback. Note the SDK segfault warning still applies under pytest capture.
- **Cloud-synced checkouts leak `.env`:** in a folder that syncs to a cloud tenant, gitignored files are mirrored too, so a `.env` there is no longer local. Keep the checkout in a directory that does not sync, and never put real API keys in a `.env` that lives in a shared or synced folder.
- **Python interpreter:** Activate the repository's Python 3.13 environment before running tests or scripts. Confirm it with `python --version`; an unrelated interpreter may not contain the locked project dependencies.
- **BQ ALTER TABLE rejects DEFAULT on existing tables:** `ALTER TABLE ... ADD COLUMN x INT64 DEFAULT 0` errors on a table that already has rows. Three-statement workaround: `ADD COLUMN` without default, `ALTER COLUMN ... SET DEFAULT 0`, `UPDATE ... SET x = 0 WHERE x IS NULL`. Schema file can keep the inline DEFAULT since `CREATE TABLE IF NOT EXISTS` only runs on fresh creates.
- **BQ Trends public dataset has no KE data:** `bigquery-public-data.google_trends.international_top_rising_terms` returns 0 rows for country_code='KE'. Connector logs WARN and returns empty DataFrame. Not a bug. ZA + NG have daily data.
- **YOUTUBE_API_KEY empty in .env:** Connector behaves correctly (WARN + empty DataFrame). Needs a dev key before YouTube rows land. Tracked in pipeline_runs.youtube_rows.
- **Ensemble HTTP 495 vs 493:** 495 = quota exhausted (expected when budget blows). 493 = subscription expired or billing issue (requires email to hello@ensembledata.com or Bronze upgrade, not a quota reset). Both trip the same circuit breaker but 493 logs an ERROR asking operator to check billing. Current tier: Bronze paid, 5000 units/day, $200/mo, started 24 Apr 2026 (replaces extended trial).
- **Ensemble per-run budget is tunable, with a per-market slice:** `ensemble_budget.budget_units_per_run` in `configs/sources.yaml` caps the shared per-run unit spend across all markets (one class-level ledger consumed in `za`, `ng`, `ke` order). It ships at 800 (we use about 16% of the 5000/day Bronze cap) and can be raised toward that cap to scale ingest volume. `per_market_units` is the per-market slice of that cap, enforced by `_budget_reached` against a per-instance `_market_units_spent` counter so a raised budget cannot let `za` eat the whole ledger and starve `ke`. Keep `per_market_units` equal to the budget for no per-market limit, or set it lower (for example budget 2400 / per_market 800). Default is byte-identical: the fallback config path reads both keys from `ensemble_budget`, `per_market_units` defaults to the full budget, and an explicit `0` is honoured rather than coalesced away.
- **Ensemble `/tt/keyword/search` requires `period` query param:** EnsembleData validates the `period` field on this endpoint and returns 422 with `{"loc":["query","period"],"msg":"field required"}` when missing. DEFAULT_ENDPOINTS for `tiktok_keyword` carries `extra_params={"period":"7"}` so every call sends a 7-day window. Any new endpoint that needs API-required fields follows the same `extra_params` pattern merged into request params inside `_fetch_endpoint`.
- **Ensemble Threads endpoint:** `/threads/keyword/search` with `name` query param, same response shape as TikTok / Instagram. In `DEFAULT_ENDPOINTS`. `threads_keywords` block in `configs/sources.yaml` drives per-market terms. MVP had it, V2 dropped it until 23 Apr 2026.
- **GDELT is now BigQuery GKG, not DOC 2.0 HTTP:** `infra/bigquery_queries/gdelt_gkg.sql` queries `gdelt-bq.gdeltv2.gkg_partitioned` filtered by FIPS substring (ZA -> SF, NG -> NI, KE -> KE). No rate limit, richer payload (themes, entities, tone, locations). FIPS codes still required for filtering V2Locations column.
- **GKG column name gotcha:** `SourceCommonName` (not `V2SourceCommonName`). Most V2-prefixed columns exist (V2Themes, V2Locations, V2Tone, V2Persons, V2Organizations) but SourceCommonName was never renamed. BigQuery errors `Unrecognized name: V2SourceCommonName; Did you mean SourceCommonName?` if you get it wrong.
- **V2Tone string format:** comma-delimited 7 fields. Field 0 = avg_tone (-100 to +100). Field 3 = polarity (0 to +100). Field counts: positive_score, negative_score, polarity, activity_ref_density, self_group_ref_density, word_count. Parse via `src.ingestion.enrichment._parse_v2tone`, normalises (tone + 100) / 200 to 0..1.- **Tone fallback redistribution:** `compute_trend_scores` tracks `tone_rows` per group. When zero GDELT rows contributed tone, the 0.05 tone weight is redistributed across the other 9 signals via `weight_scale = 1.0 / (1.0 - tone_weight)` rather than applying a neutral 0.5 bias (which would add +0.025 to every no-GDELT group).
- **RAW_COLUMNS growth + connector projection:** Ensemble `_finalise` uses `df.reindex(columns=..., fill_value="")` instead of column project `df[...]` so non-GDELT connectors don't KeyError when the schema grows (e.g. v2tone / v2persons added). Any new connector that emits a subset of RAW_COLUMNS should use the same pattern.
- **Ensemble comment dedup keys on (url, text, author_handle):** `_finalise` dedups on `(url, text, author_handle)`, not `url` alone. Comment and reply enrichment rows (threads_reply, tiktok_comment_reply, the youtube / instagram comment rows) carry their parent post's url, so a url-only dedup collapsed N distinct comments on one post into a single row and discarded enrichment the connector had already spent units to fetch. Distinct comments now survive because their text and author differ; a genuine duplicate post (same post fetched via two hashtags) still collapses because its text and author match too, so the live post path is unchanged. `dedup_keys` intersects with the columns present, so it falls back to url-only if a column is absent. This is the gate for flipping any of the dark comment surfaces.
- **merge_dataframe staging schema:** `src/utils/bigquery.py` passes the target table's schema to `LoadJobConfig.schema` when staging. Without this, pandas infers INT64 for all-None columns and MERGE fails with "Value of type INT64 cannot be assigned to STRING" on columns like `cycle_id`. Stage name uses `uuid.uuid4().hex[:16]` (64 bits) to avoid concurrent-run collision.
- **Test command:** From the activated Python 3.13 environment, run `python -m pytest tests/ -W error -p no:cacheprovider --tb=short`. `-W error` catches deprecations as failures. `-p no:cacheprovider` avoids cache writes in restricted checkouts.
- **Quality gate (qlty + bandit + pre-commit):** Three layers are wired. (1) `.pre-commit-config.yaml` runs ruff and bandit against `.bandit`. (2) `.qlty/qlty.toml` configures qlty with the repository's reviewed suppressions; run `qlty check --all` with qlty available on PATH. (3) GitHub Actions `.github/workflows/ci.yml` runs the shared quality gate with read-only repository permission and credentials disabled on checkout.
- **Topic classifier:** `src/enrichment/topic_classifier.py` is a pure function. `classify_topics(title, text, hashtags, market)` returns a sorted list of topic_group names whose word-boundary regex patterns match. Per-market taxonomy lives in `configs/topic_groups/{za,ng,ke}.yaml` (29 groups total: ZA 9, NG 10, KE 10). LRU cache compiles patterns once per market. Multi-word keywords tolerate `\s+` whitespace drift. Empty list means unclassified, counted in `pipeline_runs.unclassified_rows`, skipped during scoring. Adding new keywords is a YAML edit + commit, no code change.
- **Topic taxonomy `da` over-matches:** ZA `politics_crises` includes the keyword `da` (Democratic Alliance shorthand). Word-boundary regex catches `da` in Italian "da sogno", Spanish "cada día", and other foreign-language haystacks. Real-world false positive surfaced during Task 3 testing. Future taxonomy work should narrow `da` to phrasal anchors like `da leader` or `democratic alliance`. Other generic-token risks: `eff`, `pap`, `groove` (ZA); `apc`, `pdp` (NG); `genge`, `bolt` (KE).
- **`item_count` is float in aggregator, INT64 in BQ:** `_aggregate_by_topic` accumulates fractional weights via 1/N multi-assign, so `item_count` is float (`176.5` for two rows tagged to two topics each). `trend_scores.item_count` is INT64. Cast at the BQ write boundary in `compute_trend_scores` via `int(round(float(item_count)))`. PyArrow refuses silent truncation: `Float value 176.500000 was truncated converting to int64`.
- **NaN tone_avg poisons trend_score:** pandas stores missing tone on non-GDELT rows as NaN, not None. `_aggregate_by_topic` guards with `math.isnan(float(tone_val))` before adding to `tone_sum`. Without the guard, NaN propagates into `tone_avg_mean` → `tone_score` → `trend_score`, and BQ rejects with `Required field trend_score cannot be null`.
- **Threads rows now extract correctly (fixed 2026-04-27):** The Ensemble `/threads/keyword/search` endpoint wraps each post under `node.thread.thread_items[0].post`. The connector's flat-iteration loop missed it and every Threads row landed with empty title/text/hashtags. Fixed via `_unwrap_threads_post(item)` in `src/ingestion/connectors/ensemble.py` plus a Threads-specific URL builder (`https://www.threads.net/@{username}/post/{code}`). `_extract_text` path order also reordered so nested `["caption","text"]` matches before bare `"caption"` (which on Threads/IG returns a dict and short-circuited the extractor). Live verification: 20/20 sampled posts now return populated text, url, author_handle.- **Velocity baseline cutover backfill:** `scripts/migrations/backfill_topic_trend_scores.py` re-classifies historical `enriched_content` over a 14-day window and merges topic-level rows into `trend_scores` so `compute_velocity_scores_for_today` finds real per-topic baselines. Without it, post-refactor topic_groups hit the empty-baseline branch in `velocity.py` and saturate `velocity_score = NEW_TOPIC_SCORE = 1.0` for 14 days. Backfilled rows carry `velocity_score = 0` (seed history, not a measurement); engagement-derived columns are 0 because `enriched_content` does not store `engagement_weighted` (only the runtime DataFrame does). Acceptable: digest reads only today's `trend_scores`, and `velocity.py` reads only `item_count` from the baseline window. Idempotent: `--dry-run` default, `--apply` to write. **Watch for inflated days**: 24 Apr 2026 had four pipeline runs during the refactor (22,770 enriched rows vs normal ~3K) which produced backfilled trend_scores 7-8x normal volume (politics_tinubu=1062, music_afrobeats=391). These rows tilt the 14-day baseline up far enough that today's 50-150 items can never beat `today/baseline > 1.0` and velocity stays 0. Workaround: surgical `DELETE FROM trend_scores WHERE trend_date='2026-04-24' AND DATE(scored_at) >= '2026-04-25'` (24 rows) restored real velocity. If a future test-storm day repeats the problem, repeat the surgical delete or harden the backfill script to dedup by run_id.
- **Brand24 KE trending-hashtags returns HTML login page:** Project `1397483539` returns `<!DOCTYPE html><title>Logging to Brand24.com</title>` on every call to `/api-data/v1/project/1397483539/trending-hashtags`. Other three KE endpoints (topics, most-followers, trending-links) work cleanly with the same `X-Api-Key`. ZA + NG hashtags also work. Endpoint- and project-specific bug on Brand24's side (or KE project config setting). Per-project `include_hashtags: false` flag in `configs/sources.yaml` skips the call (logged at INFO when skipped). Connector reads `project.get("include_hashtags", True)` per iteration. Flip back to `true` (or remove) once Brand24 support confirms. The endpoint requires a Business-tier API add-on that is not yet activated.
- **Phase 2 (Vertex Gemini brief layer) gated behind PHASE_2_ENABLED:** `scripts/run_rss_now.py` reads `os.environ.get("PHASE_2_ENABLED", "false").lower() == "true"`. LIVE in prod since May (flag true on the Cloud Run job); the steps here are the original enable record. To enable elsewhere: (a) run `python scripts/migrations/add_brief_columns_to_trend_analysis.py --apply` to extend `trend_analysis` with platforms / sentiment_summary / status_tag columns, (b) confirm the GCP card on file is current, (c) set the env var on the Cloud Run job + local `.env`. Each run makes one Vertex Gemini call per topic (~$0.0065 each, ~$5/month at full top-8-per-market scale).
- **Phase 2 brief shape comes from Jo's PDF mock:** `src/analysis/prompts/trend_brief.py` produces JSON with `description_rationale` (2 paragraphs), `activation_idea` (1 paragraph), `key_metrics` (3 strings), `platforms` (list), `sentiment_summary` (string), `status_tag` ("Key" or "Rising"). Three of these map onto existing `trend_analysis` columns (description→trend_synthesis, activation→cultural_context, key_metrics→campaign_angles); the other three need the schema migration above before persistence works.
- **`google-genai` SDK over `vertexai.generative_models`:** Vertex's Python SDK is migrating; the legacy `vertexai.generative_models` path retires June 2026. `src/analysis/gemini_client.py` uses the new unified `google.genai.Client(vertexai=True, project=..., location=...)` so we skip the migration. The legacy SDK still works but emits a deprecation warning every import.
- **Python 3.13.13 on Windows hits a native pytest crash:** `Windows fatal exception: access violation` thrown during stdlib logging makeRecord on certain test paths under pytest log capture. Pre-existing environmental issue; CI on Ubuntu unaffected. Workaround for tests that hit logger.warning: patch the module logger to a MagicMock for that test only. See `tests/unit/test_generate_briefs.py::test_generate_briefs_isolates_per_topic_failure` for the pattern.
- **Looker `v_market_comparison` view restated as per-market top-10 leaderboard:** Pre-refactor pivot was keyed on `(trend_date, query_group)` across markets, which worked when `query_group` held source labels (`tiktok_hashtag`, etc.) shared across markets. Post-refactor topic taxonomies are disjoint by design (`music_amapiano` ZA-only, `music_afrobeats` NG-only), so the pivot returned NULL for two columns and `markets_active = 1` for every row. View now returns per-market ranked leaderboard with tier label. Cross-market comparison ("music up in NG and ZA") deferred to Phase 2 meta-cluster mapping (internal design notes). Deploy via `python scripts/create_looker_views.py`.
- **Email digest alerting (replaces Telegram):** `src/alerts/email_digest.py` + `src/alerts/detector.py`. Four env vars: `GMAIL_USER`, `GMAIL_APP_PASSWORD` (16-char Google app password, spaces stripped internally), `EMAIL_RECIPIENTS` (comma-separated), `EMAIL_ALERTS_ENABLED=true` to actually send. App password requires 2FA on the sender account; create at `myaccount.google.com/apppasswords`. Sends via `smtp.gmail.com:587` with STARTTLS. One HTML + plain-text multipart email per run covering every tier upgrade across ZA / NG / KE; no email when nothing upgrades. Alerts fire only on tier upgrades vs the most recent prior score (trend_date < CURRENT_DATE); first-time-scored groups fire only at Emerging or above.
- **Telegram retired:** The short-lived Telegram bot integration was removed in commit 7f40bf4. Telegram anti-spam froze the bot after a burst of test sends across multiple chat_ids. If considering Telegram again, warm the bot by sending low-volume traffic to a single chat_id over several days before scaling or switching destinations; do not burst-test.
- **CI pip-audit ignores:** `.github/workflows/ci.yml` passes `--ignore-vuln CVE-2026-1703 --ignore-vuln CVE-2026-40192` (pip itself, pillow transitive via python-pptx in dev envs, neither is a direct app dep).
- **GPU machine type:** L4 GPUs require g2-standard machines on GCP, not n1-standard. n1 does not support L4
- **Looker Studio has no kiosk mode:** There is no built-in kiosk feature. TV display uses a Chrome auto-refresh extension (free) or Looker Studio Pro auto-refresh (paid)
- **On Gemini 3.5 Flash since 31 May 2026 (global endpoint):** model set by `GEMINI_MODEL` + `GEMINI_LOCATION` env. To fall back to gemini-2.5-flash, update `GEMINI_MODEL` + `GEMINI_LOCATION` on the Cloud Run job (via `gcloud run jobs update` or the cloudbuild deploy env). GitHub has no execution path, so there is no workflow to revert. No code change. It was NOT a one-line swap: 3.x is global-only on Vertex (us-central1 returns 404) and is a thinking model, so it needs a bounded `thinking_budget` + widened `max_output_tokens` or the structured JSON starves to empty.
- **Qwen model:** Use Qwen 3 14B (current gen, April 2026). Qwen 2.5 is two generations behind
- **AfriE5 model name:** The correct name is AfriE5-Large-Instruct (not AfriE5-Large). Weights may need to be obtained from the GitHub repo, not HuggingFace
- **Brief discrepancy:** Influencer section says 11 trends, deliverables section says 12 (8 planned + 4 ad-hoc). Budget math uses 11. Needs clarification with Google- **Brand24 Business API (api-data.brand24.com):** `src/ingestion/connectors/brand24.py` pulls per-project analytics from `https://api-data.brand24.com/api-data/v1/project/{pid}/...`. Auth is the `X-Api-Key` HTTP header populated from `BRAND24_API_KEY` (not Authorization: Bearer; `api.brand24.com` with Bearer returns 404 HTML). Successful responses wrap data under either `data` or `message` depending on endpoint; `_extract_payload` probes both. Account id lives on the Brand24 integrations-api-data page. List projects via `GET /api-data/v1/account/{account_id}/projects_list/` -> dict of `{project_id: project_name}`. Connector pulls four endpoints per project: `topics` (AI-clustered groups with description + sentiment + mentions + reach), `trending-hashtags` (hashtag + mentions_count + social_media_reach), `most-followers` (top authors with followers_count + reach + mention count; rows tagged as `top_author` and platform inferred from author URL so Facebook / Twitter / Instagram / TikTok voices slot into creator-watchlist scoring), and `trending-links` (shared URLs with platform inferred from domain so Facebook / YouTube / TikTok links land on the right platform). `_platform_from_url` maps hostnames to the same canonical labels used elsewhere. Sentiment breakdowns from `topics` are mapped to 0..1 tone via `_sentiment_breakdown_to_tone` and written back as a synthetic V2Tone string so enrichment's `_parse_v2tone` path picks them up on par with GDELT rows. Projects declared per market in `configs/sources.yaml` under `brand24.{market}.projects` as `[{id, name, query_group}]`. Empty list or missing key -> empty DataFrame, pipeline keeps running.
- **Brand24 project IDs are volatile:** Heavy keyword edits via the UI can trigger project delete + recreate, which mints a fresh project id. Always re-verify ids after manual UI work by calling `/api-data/v1/account/{account_id}/projects_list/` and updating `configs/sources.yaml` before the next pipeline run. Current live ids as of 24 Apr PM: ZA 1397483532, NG 1397483537, KE 1397483539.
- **EnsembleData 495 errors:** HTTP 495 is EnsembleData's custom quota-exhaustion code, not a service failure. Bronze tier (5,000 units/day, active from 24 Apr 2026) is sufficient with the budget caps in sources.yaml. Circuit breaker prevents over-spend.
- **YouTube trending is category-limited since July 2025:** The mostPopular/trending chart now only covers Music, Movies, and Gaming categories. It still runs as a supplementary feed behind the per-market `trending_enabled` flag (see the live `mostPopular` fetch in `src/ingestion/connectors/youtube.py`); keyword search remains the primary path.
- **Broken RSS feeds:** News24 (HTTP 403), Premium Times Nigeria (bot protection), and Daily Nation Kenya (paywalled) are all blocked as of 2026. Removed from sources.yaml. Working replacements: SABC News + Daily Maverick (ZA), Punch + Vanguard (NG), The Standard + Tuko (KE), AllAfrica cross-market
- **pytrends is dead:** Archived April 17, 2025. Maintainer stepped out Feb 2025. His advice: "stop using Pytrends." Do NOT use pytrends or build on it. Alternatives: BigQuery Trends public dataset (free), SerpAPI ($75/mo), Google Trends API alpha (limited access, apply at developers.google.com/search/apis/trends)
- **GDELT FIPS codes:** GDELT DOC 2.0 API uses FIPS country codes, not ISO. South Africa = SF (not ZA), Nigeria = NI (not NG), Kenya = KE. Rate limit: 1 request per 5 seconds. The MVP connector does not have these codes or rate limiting, which is why it returns 0 rows
- **EnsembleData has official SDK:** `pip install ensembledata` provides EDClient with typed methods and unit tracking per call. Better than raw HTTP long term, but MVP extraction logic should be ported as-is for May 1 to avoid regression
- **Python file writes emit CRLF on Windows:** the `mixed-line-ending` pre-commit hook auto-fixes the file and ABORTS that commit. Re-`git add` the same files and commit again; it passes the second time. Editing `configs/sources.yaml` (hook-protected, so edit it via Bash+Python) hits this every time.
- **PostToolUse ruff `--fix` strips a just-added import:** it removes the import (F401) when its first usage has not landed yet. Add the import and a usage in the SAME edit, or add the usage first.
- **Classifier Safety Gate, the classifier is NOT dark-shippable:** `topic_classifier` runs on every cron, so before merging any change to `src/enrichment/topic_classifier.py` or `configs/topic_groups/*.yaml`, run `<py3.13> scripts/verify_live.py reclass-diff <completed-date>` (local-safe, keyword-only, no Vertex). Gate: classified rate rises in every market, no topic-share swing over 10 points, 0 geo leaks. Distinct from the cloud `embedding-gate.yml` (Vertex threshold sweep, runs via workflow_dispatch).
- **Local BigQuery + Vertex SDK RPC segfaults on Win+Py3.13 (exit 0xC0000005 / -1073741819, no traceback):** any `bigquery.Client().query(...)` or Vertex `genai.Client` RPC crashes in the native gRPC layer on this box. IMPORT is safe (`python -c "import ..."` exits 0); only the RPC crashes, so a pure script that imports the render code but creates no client runs fine. For data use the `bq` CLI (gcloud's bundled Python): `bq query --use_legacy_sql=false --format=prettyjson '<sql>'`. For a PULSE preview, `scripts/_preview_pulse_local.py` (gitignored) renders from bq-CLI JSON with no google.cloud import. Schema ALTERs run via `bq query 'ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...'`. Full-pipeline / Vertex validation is cloud-only (workflow_dispatch or the Cloud Run Job). The bq MCP is not always connected; the bq CLI is the reliable fallback.
- **Connector-zero watchdog (engine-pulse):** `check_connector_zero` in `scripts/engine_pulse.py` hard-FAILs an enabled connector that declares a nonzero capacity floor (`expected_per_day > 0` and `critical_pct > 0` in `configs/engine_capacity.yaml`) but returns exactly 0 rows on a run `pipeline_runs` marked `success`. That is the silent bad-key signature: a normally high-volume connector flat-lining while the run still reads green. A may-be-zero allow-list (`connector_zero_alert.may_be_zero`, defaulting to `{gdelt}` in code when the config block is absent) exempts connectors whose zero is legitimate, so a genuine GDELT-quiet day stays WARN rather than tripping the FAIL. The flagged names surface as `connector_zero_alerts` in `to_dict` for the morning-check. `_connector_enabled` mirrors each connector's real master switch in `sources.yaml`, so a disabled connector at zero is never flagged.
- **No-email watchdog (`scripts/watchdog_function/main.py`):** the daily watchdog now alerts when the pipeline ran but the digest never went out, not only when no run happened at all. `_decide_alert` distinguishes `no_run` (no `success` rows in `pipeline_runs` for today) from `no_email` (success rows exist but `_count_healthy_email_today` finds no healthy `email_audit` marker, where healthy means `email_status` in `sent`, `skipped_nothing_notable`, or `skipped_already_sent`). A legitimately quiet day stays healthy because the two skip statuses count as healthy. Each case sends its own alert message. The watchdog fires at 06:30 UTC after both pipeline triggers have had their window (00:30 UTC Cloud Run primary, 02:30 UTC Cloud Run fallback; GitHub has no execution path).

## Document Generation
- Word documents generated with `docx` npm package (`npm install docx`)
- Generator scripts live in `scripts/generate-*.js`
- Run with `node scripts/generate-proposal-doc.js`
- All documents must follow the Writing Style rules above (no dashes, no bullets in prose, no bold inline)

## Brief Requirements (Non-Negotiable)
- Nano Banana prompts must be "zero-edit" ready with per-market Trend-Triggers
- Every prompt includes "Created with Gemini" tag
- Endorsement disclosure per market (APCON Nigeria, ASA South Africa, CAK Kenya)
- Primary KPI: 1% reach-to-prompter conversion (100K UGC assets from 10M reach)
- 250 creators per trend: 100 Nano (1K-10K) + 150 Micro (10K-100K)
- Geographic split: 40% NG, 40% ZA, 20% KE
- 48-hour trend-to-market workflow SLA

## Verification Checklist
```bash
python -m pytest tests/unit/ -W error -p no:cacheprovider --tb=short   # tests green
python -m ruff check .                                                  # lint clean
gh run list --workflow ci.yml --limit 3                             # CI green on master
bq query --use_legacy_sql=false 'SELECT COUNT(DISTINCT run_id) FROM `ogilvy-trends-v2.trends_v2_dev.pipeline_runs` WHERE DATE(started_at)=CURRENT_DATE()'  # today's cron ran
```
For a classifier / taxonomy change, also run the reclass-diff Safety Gate (see Known Gotchas). The full morning health check is the `morning-check` skill.
