# What carries over from the old 42

Audited at commit f8101d90 (latest approved core) on 28 September 2026. Paths are relative to the repo root. "Lift" means copy the named logic into core/ and adapt it; the old file stays where it is. Nothing is deleted from the repo or from BigQuery.

## Data that already exists (project ogilvy-trends-v2, BigQuery location US)

| Asset | Where | Use in the new core |
|---|---|---|
| Enriched post history, no expiry | trends_v2_dev.enriched_content (live cron), trends_v2_staging.enriched_content | Backfill posts for first_seen and Recurring only, never baselines (DATA.md section 2); embed for semantic search. Drop the genz_score column on read. |
| Daily topic scores history | trends_v2_dev.trend_scores | Memory only: first_seen and Recurring, never baselines (DATA.md section 2). Recompute without the search_velocity (Google Trends) term. |
| Rising-term series | seed_graph (365-day expiry), seed_candidates | Seed the cultural map with terms, first seen and last seen. |
| Run and credit logs | pipeline_runs, trends_v2_staging_funded.socialcrawl_credit_ledger_v1 | Shape of the new credit ledger and run log. |
| Citation schema | trends_v2_staging.signal_evidence_v2 (url, excerpt, claim_role) | Shape of findings evidence. |
| Health views | v_latest_trend_scores, v_market_comparison, v_content_volume, v_pipeline_health, v_system_status | Keep for the Coverage screen. |
| Untrusted archive | trend_analysis, daily_summary, seed_insights, creator_briefs (Gemini outputs) | Never used as evidence. |

Warning: raw_content has a 90-day partition expiry, so raw text older than about three months is gone. enriched_content is the durable history. Collection stopped on 8 September; Ask currently serves 25 August to 7 September only.

## Engine code

| Lift | Why it matters |
|---|---|
| engine/src/ingestion/connectors/socialcrawl.py | Credit price table, charge-the-max rule, halt on INSUFFICIENT_CREDITS, per-platform parsers, and live-probed quirks (tiktok/search dead with filters, Twitter date format, unsorted tweets, dead ids). Strip the Wave 1 and funded code (about lines 228 to 400 and 674 to 1000). |
| engine/configs/sources.yaml (socialcrawl block), engine/configs/vendor_endpoint_catalog.yaml | Curated terms, subreddits, handles and Facebook page ids per market; 244 catalogued routes including transcript routes. |
| engine/src/analysis/seed_graph.py, seed_candidates.py | Rising-term detection against a 28-day baseline with novelty and rejection memory. Remove avg_genz_score. |
| engine/src/enrichment/embedding_classifier.py and configs/topic_anchors | Embedding with a content-hash cache and anchor matching. |
| engine/src/analysis/open_intelligence/graph.py, scoring.py | Multi-vote clustering (co-occurrence, embedding similarity, shared creator, time overlap, independent source) and audience-neutral percentile scoring. |
| open_intelligence/general_question_quote_span.py, general_question_claims.py | Exact quote offsets with a hash, and a claim-to-evidence validator. |
| engine/src/scoring/velocity.py, corroboration.py; analysis/pan_african.py | Baseline ratio, independent corroboration (drop "search" as a family), multi-market rising. |
| engine/src/analysis/reconcile.py, claim_gathering.py | Claim-versus-evidence checking pattern for the critic. |
| open_intelligence/geographic_scope.py, history_normalizer.py | Local, contextual, foreign or unknown per post; keep old and new vendor data comparable. |
| engine/src/ingestion/observation_id.py, utils/geo_blocklist.py, language_guard.py | Deterministic post identity; blocks Sapa-style name collisions and foreign-language leakage. |
| engine/src/enrichment/sentiment_lexicon.py, configs/sentiment_lexicons, topic_groups, creators, entity_aliases.yaml, seed_graph_stoplist.yaml | Local slang, vocabularies, tiered creator lists. Remove genz_lifestyle (ZA) and genz_sheng (KE) groups and the youth/Gen Z query terms. |
| engine/src/ingestion/connectors/gdelt.py, youtube.py, youtube_scrape.py, rss.py, wikipedia.py, app_charts.py, apple_music.py | Secondary feeds for news, video transcripts, charts and interest signals. |
| engine/src/alerts/detector.py, email_render/ | Movers detection and a tested HTML email kit (remove Nano Banana, Lyria and "Powered by Gemini"). |
| engine/scripts/socialcrawl_rollout.py, leadtime_eval.py with watchlist_ground_truth.yaml, engine_pulse.py, watchdog_function/ | Credit runway governor; the only external accuracy yardstick; health checks. |

Retire: open_intelligence approval, authority, admission, funded, canary, certification, manifests and receipts (about 91k lines); every generate_* brief module and prompts/ (Gemini and Nano Banana); google_trends_rss.py, bigquery_trends.py and search_*_terms.sql; ensemble.py and reddit.py (EnsembleData cancelled); brand24, semrush, spotify connectors; scripts/staging and approval migrations.

## App

| Keep or lift | Why |
|---|---|
| app/frontend/vendor/ogilvy-intelligence-design-system-2.0.22.tgz | Shell, instruments, state frames, Newsreader and Recursive fonts. Its source is not in the repo; find it. |
| frontend/src/today.jsx, explore.jsx, compare.jsx, releasedSignal.jsx, instrumentAdapters.js, briefingContract.js | Finished Briefing, Discover, Compare and signal screens with every state designed. |
| frontend/src/chat.jsx, chatTransport.js, generalIntelligence.js, ui/CitedAnswer.jsx | Ask with follow-ups, retries and validated citations. The validators are strict (17 or 19 citation keys, SHA-256 quote checks): the new API must emit those shapes or the validators are loosened in one place. |
| app/src/api/pdf_exporter.py, pdf_renderer_worker.py, configs/pdf_runtime.json, ask_export.py, intelligence_reply.py | Hardened HTML to PDF and the cited answer export. |
| app/src/api/intelligence_dossier.py, dossier_resolver.py, dossier_store.py (storage only), dossier_review_store.py (log only) | Claims, evidence, contradictions and exclusions; write-once versioned storage; decision log. Drop the approval roles. |
| app/src/api/source_lab.py, fieldwork.py; frontend sourceLab.jsx, fieldwork.jsx | Source inventory and credit view; investigation status. |
| frontend check_contrast.mjs, Playwright journeys | Accessibility and journey checks. |

Retire: main.py routes (lift the passcode gate, rate limiter and static serving), bq.py, synth.py, chat.py (Gemini), research and persona registries, dossier_approval, dossier_artifacts, scope and authority modules, legacy screens (topic.jsx Prompt Pulse, board, seeds, seedpath, map, browse, method) and all Gen Z and Nano Banana copy.

Missing screens to build: History (real), Coverage, role-free dossier review.

## Ops and records

| Keep or lift | Why |
|---|---|
| ops/deploy/runtime_jobs.py, runtime_schedulers.py, release_native_adapter.py, release.py, readback.py, infra/runtime/*.json | Working Cloud Run job, paused scheduler and service deploys over REST with readback and rollback. Remove grant fields. |
| ops/monitoring/apply_monitoring.py, definitions.json | Alerts as code, including out-of-credits and missed-daily-run alerts. |
| ops/evaluation/score_questions.py, discovery_review.py, forecast_cohort.py, source_ablation.py, trials.py, run_questions.py (FAMILIES) | Claim-support scoring, cluster coherence review, forecast versus persistence, source ablation, strategist trials. |
| ops/deploy/resource_manifest.json | Inventory of the 76 staging resources and 17 identities that already exist. |
| cloudbuild.app.publish.yaml | App build pinned by digest with a least-privilege build identity. |
| On Albert's PC: GENERAL_INTELLIGENCE_EVALUATION.md and development-bank-36.json | The only real question bank (12 families x 3). Albert must copy these into the repo. |

Existing staging resources worth reusing: service listening-post-staging, Artifact Registry intelligence-42, Cloud Tasks queue oi-general-question-staging, secret SOCIALCRAWL_OGILVY_API_KEY (funded, about 250k credits on 23 Sept). The secret SOCIALCRAWL_API_KEY has a zero balance and must never be used. The app service account's custom roles QuestionVertexPredict and QuestionControlReplace expire on 30 September 2026 at 23:59:59Z, so the old Ask goes dark then; the new core does not depend on them.

## What the old build taught us

1. IAM churn: 134 bindings across seven amendments, each approved separately. The new core uses one small set of identities, one per job or service (SETUP.md), created by one add-only grant script, run once.
2. Pins and digests turned every change into a new approval. The new core pins only container images, automatically, in CI.
3. Approval machinery made the daily job unable to finish by design. The new core has a credit cap in code instead.
4. Hand renewals and expiring policies. The new core has nothing that expires by design.
5. Credentials only on one Windows PC. The new core deploys from GitHub Actions with Workload Identity Federation.
6. A zero-balance key stopped collection silently. The new core checks the balance daily and alerts.
7. Scope: 35 tasks and 84 requirements, 3 finished. The new core measures done by working features and a weekly score.

## Earlier build work since 19 September

The earlier build made 1,621 commits on 270 branches with 127 open draft pull requests, about 260,000 lines. Measured by file path and commit subject, about one line in six is product logic; about 55% is approval, authority, pins, grants, certification, pricing policy and their tests. Most of the valuable product code is already on f8101d90, so full-42 starts from there. What exists only on unmerged branches, and should be lifted from the branch named:

| Lift | From | Why |
|---|---|---|
| Daily reader fixes: late rows by event window, retained capture check, Ask window bound to the eligible capture, parent rows in snapshots | integration/42-core-20260928-daily-ask-repair (7e98eb50) | Correct time-window logic for daily data |
| Follow-up evidence carry-over (general_question_parent_capsule.py) | fix/42-ask-milestone-path (cfa34ffc) | Without it follow-ups lose their evidence |
| Window-half comparison by full match counts | task/42-s2-window-counts | Close to rising detection |
| Stable observation ids | task/42-s2-obs1 | Dedup across runs |
| Native id and route on every record (connectors/base.py and 10 connectors) | PR 116 | Dedup and provenance |
| TikTok comments and Instagram creator-country checks | PR 86 | SocialCrawl as main feed |
| Diffusion forecast fix (aged-out evidence is not an analogue) | PR 97 | Honest history and forecasts |
| Dossier from a stored answer (dossier_producer.py) | PR 96 | Dossier assembly |
| Deterministic claim checks (score_questions.py) | PRs 111 and 138 | Citation checker |
| Bytes-billed cap on every app BigQuery query | PR 98 | Cost guard |
| Historian and forecast states on topic pages | PR 54 | Honest UI states |
| Export response check (AnswerExport.jsx) | fix/42-source-export-milestone (8cb2f802) | Safe downloads |

Still live and to be removed in the new core: google_trends_rss in collection_exposure_policy_r3.json and collection_exposure_policy_daily_v1.json, the coverage window and exposure stages, source_inventory.py and candidates.py; the Gemini Flash answer model in general_question_answer.py (gemini-3.8-flash) and gemini-3.5-flash in brain_semantics.py, canary_policy.py and gemini_client.py; legacy genz fields in bq.py, desk.py, model.js, researchLib.jsx and the digital_architect_genz persona.

All 304 branches and 127 pull requests stay as they are. Closing or deleting them is Albert's call; branch deletion is done by Albert.
