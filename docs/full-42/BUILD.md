# Build order

One task at a time, in order. Every task has a check that must pass before it is marked done in PROGRESS.md. "Albert" marks the few steps only he can do; everything else is the builder's. Targets are in working days for one builder working steadily; they are targets, not promises, and every stage ends with something Albert can use. Stage 1 is split in two: operational is Stage 1A.

Branch: full-42, created from the pack branch (which sits on the last approved core, f8101d90). Code in core/. Credits: the caps table in SETUP.md is the only source of numbers (build and probes up to 600 a day before the morning schedule starts; evaluations run in replay mode from cached responses).

## Stage 0. Ready to build (target: days 1 and 2)

| # | Task | Who | Check |
|---|---|---|---|
| 0.1 | Close the earlier build branches at their save point; copy GENERAL_INTELLIGENCE_EVALUATION.md and development-bank-36.json from the PC into core/eval/legacy/ | Albert | Files present in the repo |
| 0.2 | Create branch full-42; set git identity per RULES.md rule 10; Albert gives the builder a fine-grained GitHub token for this repository with contents and pull requests only, so workflow runs and dispatches stay Albert's | Builder | git config shows Albert; `git push` works; `gh workflow run` is refused for the builder's token |
| 0.3 | Enable Gemini on Vertex AI for ogilvy-trends-v2: gemini-3.8-flash on location global for every model role (understand, brief, Ask) and gemini-embedding-001 for embeddings; model ids live in core/llm/provider.py (GEMINI_MODEL, GEMINI_FAST_MODEL); quota is requested only if the first test calls show it is too low | Albert | After 0.4: one smoke model call to gemini-3.8-flash as f42-builder succeeds |
| 0.4 | Write core/setup/bootstrap.py from SETUP.md (add-only, idempotent, dry run by default, readback table, creates every identity, the WIF pool and the Vertex connection); the reviewer checks it; Albert runs it dry, then with --apply, in his own terminal; then Albert runs `gcloud auth application-default login --impersonate-service-account f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com` so the builder's Python clients act as f42-builder too. Until this step the builder has no cloud access (settings.json impersonates f42-builder) | Builder, then Albert | Readback table shows every binding present, nothing removed; each identity's smoke test passes |
| 0.5 | SocialCrawl probe: balance, status, credits/transactions, and the probes in research/13-socialcrawl-full-map.md section 5 plus probe 14 (web/scrape of the public X trends archive for ZA, NG, KE), in that order (about 60 credits); save redacted samples to core/collect/samples/ | Builder | Probe report lists route, status, charged credits, rows; feed=local share of in-country videos per market and NG to KE overlap. If the local share is under 40% or the feeds overlap, move that budget to the X trends seed, hub panels and country-filtered search before 1.3 |
| 0.6 | Inventory existing BigQuery data read-only: rows and date range per market in trends_v2_dev.enriched_content, trend_scores, seed_graph, intelligence_42_sources_staging | Builder | Inventory in docs/full-42/reference/data-inventory.md |
| 0.7 | Strip Gen Z and Google Trends from the market configs being lifted (topic groups genz_lifestyle and genz_sheng, youth and Gen Z queries, google_trends sources) into core/config/markets.yaml | Builder | grep for genz, gen z, google_trends in core/ returns nothing |
| 0.8 | Albert gives one standing go for the capped morning collection, to start the day task 1.3 passes | Albert | Go recorded in PROGRESS.md |
| 0.9 | The builder drafts the hub panels (8 X accounts and 12 culture-desk accounts per market) from the curated handles in engine/configs; colleagues in Johannesburg, Lagos and Nairobi confirm them; the same three people become the weekly reviewers (TRUST.md section 6) | Builder, then Albert names the people | Confirmed lists in core/config/hubs.yaml; three reviewers named in PROGRESS.md |
| 0.10 | Albert asks Ogilvy legal to check the terms of the public X trends archive and of platform collection through a vendor | Albert | Answer recorded; until it is yes, 1.3 runs without the archive and still passes |

## Stage 1A. 42 is operational (target: working days 3 to 13)

Operational means: three mornings in a row, by 06:30 SAST, Today shows the key things per market with posts you can open, nothing unchecked is published, and Ask answers questions with cited claims. Collection starts on about day 4 so that history builds while the rest is built. Build Today before Ask's polish: tasks 1.12 to 1.15 come before any work on the research log, and embeddings (1.8) never block the operational check (Ask at T0 and T1 works with keyword search until 1.8 lands).

| # | Task | Check |
|---|---|---|
| 1.1 | core/collect/socialcrawl_client.py: prices from the OpenAPI spec, charge the higher of quoted and reported, ledger write per call, the caps in SETUP.md (per job, Ask, eval, month), balance floor, same-day cache, replay mode from cached responses, status states, forbidden routes (SOURCES.md), vendor labels kept apart from evidence | Unit tests with recorded samples: cap refusal, forbidden route refusal, ledger rows, cache hit costs 0, replay costs 0 |
| 1.2 | Datasets intelligence_42_core and intelligence_42_agent and the Stage 1 tables in DATA.md, including raw_responses and post_observations (append-only), collection_health, item_counter_daily, calendar, claim_checks (CREATE TABLE IF NOT EXISTS only) | Dry-run DDL, then create; INFORMATION_SCHEMA lists every table |
| 1.3 | core/collect/job.py: the Stage 1A rows of the costed table in SOURCES.md (rows 1 to 8, 11, 12, 14, 16 and 22, the hub panels and, once legal says yes, the X trends seed); responses appended to raw_responses, then one MERGE into posts and an append to post_observations; RUN_DATE override | Staging run with ENGINE_DAILY set to 150: rows for ZA, NG and KE dated today from at least 5 platforms; ledger total equals vendor-reported charges; second run adds no duplicate posts |
| 1.4 | Moments calendar, loaded before the first scheduled collect: the next 90 days per market from date.nager.at plus a hand list (NG Independence Day 1 October, KE Mashujaa Day 20 October, SA local elections 4 November, the 25th and SASSA paydays, fuel price days, BBNaija and BBMzansi, Felabration, matric and WAEC results, Black Friday, Detty December, fixtures) | Calendar table lists the next 90 days with a source per row |
| 1.5 | Scheduler for collect at 02:00 SAST under Albert's go from 0.8; the job runs every day from here on | Next morning: rows for all three markets, ledger within cap, collection_health rows written |
| 1.6 | Warm-up detection (TRUST.md section 4 and DATA.md section 3): geo_confidence per platform from the SOURCES.md geo recipe; items from raw fields; item_daily from each item's first unbiased sighting, with earlier days NULL for counters only (rank-list and panel series start at the protocol's first day, so their earlier days are real zeros, DATA.md section 3.2); vendor series that carry their own history (board curves, sound adoption curves, counter deltas, rank entry and climb); warm-up states New to 42, Spike, On the boards, Seasonal, Recurring, and Emerging after 5 valid days with the floors | SQL tests on fixture tables with known answers pass, including a newly watched item that must not read as a surge |
| 1.7 | Legacy history for memory only: enriched_content and seed_graph give first_seen and Recurring; no baselines from legacy rows | Three known recurring items show their earlier waves |
| 1.8 | Embeddings: BigQuery remote model on gemini-embedding-001 (768 dimensions) through connection f42-vertex; embed posts; vector index | VECTOR_SEARCH for test phrases in English, isiZulu, Pidgin and Sheng returns on-topic posts (recorded) |
| 1.9 | One answer contract: the schema in core/eval/rubric.md section 1 is the only answer shape; AGENT.md matches it; the app's citation validators are loosened in one place to accept it | Contract test: agent output, eval and app adapter all validate the same fixture answer |
| 1.10 | Agent tools as 42's own functions only (AGENT.md, core/agent/toolset.py); the model has no file system, shell or web access outside them | Unit tests; sql_query refuses writes and non-allowlisted datasets; socialcrawl_call refuses over budget |
| 1.11 | Ask at T0 and T1 (live calls spend from ASK_DAILY): orchestrator with one researcher; the writer returns strict JSON claims; code checks every quote word for word against stored post text, pins every number to its run_id and result hash, computes labels; deterministic checks K1, K2, K3, K5, K6, K8, K10; a claim passes the support check only if marked supported | Five questions from core/eval/questions.yaml: schema-valid, each with at least 5 cited posts inside the window from 2 or more platforms, zero unknown evidence ids, zero age claims, every number reproduced |
| 1.12 | core/brief/job.py, a fixed pipeline, not agent runs: confirm the top 10 candidates per market (since= on search/multi from the first sighting, and the seen id on the search routes that accept it, SOURCES.md), build an SQL evidence pack, one structured Gemini call per trend, code checks and one support check, 5 trends at a time; at 06:15 anything unfinished publishes as numbers and posts only; held-back items keep their reason | Staging run: a brief per market; every trend has a state, explanation, figure and at least 3 cited posts, or is listed as held back with its reason |
| 1.13 | Job chain: collect, then understand (embeddings and synchronous extraction of the day's evidence posts), then detect, then brief; each job's last step starts the next job through the Cloud Run Admin API as its own identity (granted run.invoker and run.jobsExecutorWithOverrides at project level), and each job refuses to run unless today's upstream run row is ok; each with an explicit task timeout and one retry; the 06:15 deadline publishes whatever passed | Two staging mornings complete by 06:30 SAST; a forced failure in detect still publishes a Today with a data-issue banner |
| 1.14 | core/api: FastAPI with /api/today, /api/trends, /api/ask (with wait=true, mode=replay and tier for the eval), /api/ask/{id}/events (server-sent events), /api/ask/{id}, passcode gate lifted from app/src/api/main.py; deploy f42-agent and f42-api to staging | Smoke test on staging: health, today, one T1 question end to end |
| 1.15 | App: Today as specified for Stage 1 in EXPERIENCE.md (five cards per market, day-over-day tags, held back, moments row, coverage strip with the share of posts with a confident location from collection_health, thin-coverage banner, warm-up notice); Ask with research log, evidence chips, confidence words, posts strip; answer export through the lifted ask_export; deploy to staging | Playwright journey on staging: open Today, open a trend's posts, ask about it, see the log, open a source, export the answer |
| 1.16 | Evaluation in replay mode. First a record pass: each of the 30 questions runs once live at T1 and its SocialCrawl responses are cached, spread over three days inside ASK_DAILY; a replay cache miss returns status not_in_replay, graded as a gap. Then promptfoo over the 30 questions from the cache, and a replay of three stored mornings with 20 cards labelled Real or Not real by a named reviewer | Score file in core/eval/results/ with per-family results, trust metrics and a note that questions over 30 to 90 days rest partly on legacy rows; zero live credits spent in the replay |

Operational check (Albert): three consecutive mornings with Today ready by 06:30 SAST for all three markets and Ask answering his own questions.

Try this (Stage 1A): open Today at 07:00 and read the key things for South Africa, Nigeria and Kenya; open the posts behind one and check they say what 42 says; then ask "What are people in South Africa talking about most on TikTok and X this week, and what is rising fastest?" and "How are Kenyans reacting online to the latest fuel price news, and does the tone differ between X and TikTok?". Good looks like: specific trends and claims, each with posts from this week you can open, numbers that match, and an honest gaps section. In the first two weeks, cards say "New to 42" or "Spike" rather than "Rising"; that is the warm-up working, not a fault.

## Stage 1B. Full brain and full statistics (target: working days 11 to 15)

| # | Task | Check |
|---|---|---|
| 1.17 | Ask at T2: researchers per platform in parallel, the critic in a fresh context (another model where the blind test allows), one gap round | Five T2 questions pass the Stage 1A checks plus critic verdicts logged |
| 1.18 | Blind test of candidate models for orchestrator, writer and critic on the replay set | Results table in core/eval/results/ with the chosen model per role |
| 1.19 | Full detection statistics (TRUST.md section 4): negative binomial test, pooled dispersion, weekday factor, Benjamini-Hochberg at q = 0.05; built and backtested now, switched on per market and platform when 14 observed days of the same protocol exist (about day 18); Rising switches on then | Code and injected-spike backtest ready; placebo windows run; switch-on recorded per series when the data allows |
| 1.20 | Human review routine with the reviewers named in 0.9; the weekly sample in TRUST.md section 6; calibrated support threshold after 300 labelled claims | First weekly review recorded; kappa on the double-reviewed 10% |

## Stage 2. Deeper understanding and the full trend picture (target: working days 16 to 25)

| # | Task | Check |
|---|---|---|
| 2.1 | Enrichment job: model extraction (language, entities, format, sound, hashtags, tone, stance, sponsored markers) with Gemini in batch | 200 sampled posts hand-checked: entity and language accuracy recorded |
| 2.2 | Clustering: nightly BERTopic on precomputed embeddings, merge into the cultural map with valid_from and valid_to; cluster labels; trends become clusters, not just hashtags | Discovery review sample (ops/evaluation/discovery_review.py): coherence at least 80%, duplicates under 10% |
| 2.3 | Full detection from DATA.md: velocity and acceleration, spread across platforms, tiers and markets, novelty and recurrence, authenticity flags, worth-attention score | SQL tests on fixture tables with known answers pass |
| 2.4 | Seed loop closes (the Stage 2 rows of the costed table in SOURCES.md join the collect job): detection writes seed_queue; next day's collector reads it; exploration share and diversity quotas; weekly drift report | Two consecutive days show earned seeds used; no cluster above 25% of expansion credits |
| 2.5 | GDELT seed generator (daily aggregates vs 28-day baseline) and news-to-social bridge | Top rising GDELT entities per market listed with their social follow-through |
| 2.6 | Video reading: keyframes, screen text and transcript for the clips that matter, read by the chosen vision model; clips and frames copied only under the SETUP.md data protection rule | 20 clips hand-checked for hook, format and sound accuracy |
| 2.7 | Detection scorecard (ENGINE.md section 6): time to detect, lead time against held-out reference lists only, precision from the weekly random review (never from taps), recall, breadth | Scorecard row written for the week; numbers traced to SQL |
| 2.8 | App: Discover (full trends feed with states, filters and Radar), topic pages with lifecycle and spread, Coverage screen with the scorecard | Playwright journeys on staging |
| 2.9 | Alerts: watchlists and threshold rules, shown in Today, email digest via the lifted email kit | A test watch fires on fixture data and on a real rising item |
| 2.10 | Monitoring as code: the alerts in SETUP.md | Alert policies listed; a forced zero-rows run triggers the alert |
| 2.11 | Authenticity (TRUST.md section 5) with the election-season signals, the campaign hashtag list and the thin-sample rule | Labelled library of past ZA, NG and KE campaigns: flag precision and misses recorded |
| 2.12 | Local sources: Nairaland, music charts (Boomplay, Audiomack, Shazam, TurnTable, kworb), an Instagram gossip and blog hub panel per market, and more news RSS through web/scrape and RSS (SOURCES.md), Google Play charts (App Store charts are labelled iPhone only until then) | Each source lands rows daily and shows on Coverage |
| 2.13 | Creator-breakout detector: a video at 3 or more times its creator's usual views, across several unrelated small creators on the same sound or format | Fixture tests; five real breakouts reviewed by eye |

Try this (Stage 2): open Discover and sort sounds by velocity; open a trend and read where it started and how it spread; set a watch on one rising topic.

## Stage 3. The rest of the app, access and deploys (target: working days 26 to 35)

| # | Task | Check |
|---|---|---|
| 3.1 | Compare (topics, brands, markets, platforms, creators) | Journey test; numbers match SQL |
| 3.2 | Creators and communities (interest and interaction based, no age) | Creator page shows recent posts, formats, reach, community |
| 3.3 | History: analogue search over the cultural map, recurrence, past findings | Three known recurring items find their earlier waves |
| 3.4 | Investigations: T3 with editable plan, credit estimate, background run, notice | One investigation completes within its budget |
| 3.5 | Dossiers: assemble, review ticks, freeze, HTML and PDF export (lifted exporter) | PDF opens with working citations |
| 3.6 | Moments calendar gains last year's analogues and what happened around each moment | Each calendar row links to last year's evidence where it exists |
| 3.7 | IAP on staging for Ogilvy accounts (after Albert answers the account question) | A named Ogilvy user signs in; passcode retired |
| 3.8 | GitHub Actions with Workload Identity Federation: build, deploy staging, smoke; production workflow only Albert can start | A merge deploys staging without any PC credentials |

Try this (Stage 3): build a dossier on one rising topic, review it, export the PDF you would show a client.

## Production (after Stage 3, only on Albert's written word)

Albert creates the production project and links billing; runs bootstrap for production; starts the production workflow with the image digest that passed staging. The release gate is a paired replay of a frozen snapshot with cached SocialCrawl responses (no live credits): the candidate must have no new hard fail and no question scoring lower than the last release on the same snapshot, and citation integrity and number reproducibility must be 100%.

## Stage 4 and 5

Stage 4: forecast scoring against persistence, cross-market lead-lag memory (hidden until it beats persistence), a sentinel creator panel per market, the optional intraday pulse (ENGINE.md section 3), local-language tone model, spike explanation, scheduled questions, Slack or Teams delivery. Stage 5: brand lens, creator brand-fit and safety, GenAI visibility, client skins (ENGINE.md section 8; BSA first), creative context pack. Each gets its own task table in this file when Stage 3 is done.
