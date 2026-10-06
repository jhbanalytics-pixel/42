# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- SocialCrawl promoted to the primary social ingestion vendor (#285). Seven credit-bounded phases (discover, creators, search, reddit, accounts, news, facebook) carry every surface EnsembleData used to provide, plus per-country TikTok and YouTube trending, Google News, and a verified creator country stamped into `v2locations`. Bounded by a per-run credit cap shared across markets and a per-phase share of it; `INSUFFICIENT_CREDITS` halts the run and writes to `pipeline_runs.errors` instead of reading as an empty day.

### Removed

- EnsembleData retired, vendor account cancelled (#285). The Reddit connector went with it, having billed the same unit ledger. Both stay in the registry with their flags false; their capacity targets are zeroed and `engine_pulse` gained a `RETIRED` bucket so a retired vendor cannot fail the daily verdict.

### Added

- Pulsar read-only social-listening connector, dark by default (#281).
- App Store top-charts connector, flipped live (#235, #239).
- Cloudflare Radar connector, flipped live (#236, #240).
- Audiomack chart connector, dark (#236).
- Five live-probed KE RSS feeds, with a lifted KE capacity target (#279).
- Google Trends RSS `trending_search` rows read into the event ledger (#278).
- Intelligence Core reconcile: event ledger anchored on real enriched_content shapes, source-backed claim selection, state-label coverage, shadow digest, coverage-report tool and graduation pack (#237, #241, #276), resolver routed to gemini-2.5-flash (#269).
- Seed discovery: `seed_graph` live (#242), `seed_candidates` live (#256), seed-path headline reweight with a measured gate and live flip render (#264).
- Lead-time evaluation harness with a standing ground-truth watchlist (#238).
- Football and FIFA topic coverage across ZA, NG, KE with a guarded "X vs Y" fixture rule.
- `search_velocity_score` carried end to end behind `SEARCH_VELOCITY_ENABLED` (default off).
- Connector-zero and no-email watchdogs; forecast accuracy gate (`predictions_archive` + persistence backtest); tunable per-run EnsembleData unit budget with a per-market slice.

### Changed

- NG creator watchlist flipped to tier_2 (#273).
- README refreshed into a current, accurate engine guide (#249); flip-readiness synced to the live flag state (#266).
- Watchdog counts seed_insights + reconcile-ledger Gemini spend and uses corrected 3.5-flash pricing (#268).
- Mailer labels made honest ("POST-TOPIC READS", "MARKET 7-DAY"); EnsembleData `_finalise` dedups comments on `(url, text, author_handle)`.

### Fixed

- Ensemble tight timeouts plus a vendor-down circuit breaker (#280).
- Scoring: neutralise NaN velocity baselines, stop SEMrush inflating engagement (#254).
- Seed graph: foreign-community leaks, generic-word and token-quality gates, stoplist passes (#257 to #262).
- SocialCrawl per-call credit floor so the run budget always accrues (#251); YouTube scrape-vs-API row dedup per market (#252).
- Mailer: dark-mode tone classes, KE dot colour and collision, board glyphs, subject cap, teaser CTA and plain-text parity (#271, #267, #265, #247).

### Removed

- Internal agent handoff files removed from the shareable tree.
- The dead EnsembleData `/tt/trending` endpoint (a 404 absent from the vendor openapi).

## [0.1.0] - 2026-04-17

### Added

- GCP project bootstrap: BigQuery dataset `trends_v2_dev` in US multi-region, all 8 table schemas deployed
- 9-signal composite scoring configuration (velocity, diversity, engagement, creator spread, regional, gen-z, watchlist, slang, search velocity)
- `search_velocity_score` column added to `trend_scores` and `enriched_content` schemas
- BaseConnector framework: retry on 429/5xx, rate limiting, token redaction, 16-column output schema
- RSS connector: full port from MVP, multi-market, URL dedup, all_markets feed support
- YouTube, GDELT, EnsembleData, BigQuery Trends connector stubs with class structure and port notes
- Orchestrator and enrichment layer stubs with port source references
- Unit test suite: 10 tests passing (BaseConnector and RSSConnector)
- Pre-commit hooks, CI pipeline, type checking scaffold
- Architecture decision records: ADR 0001 (MVP port gotchas)
- Client reference materials moved to `docs/reference/`

### Security

- Repository history cleaned: no attribution markers, no embedded PAT
- Dead GitHub remote removed from `.git/config`
- `detect-secrets` baseline established
