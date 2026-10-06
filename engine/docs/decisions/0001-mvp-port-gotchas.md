# ADR 0001: MVP Port Gotchas and Per-Connector Fix List

**Date:** 2026-04-14
**Author:** Albert Meintjes
**Status:** Active

## Context

Trends Engine V2 ports its five data connectors from Thapelo Masebe's MVP implementation
at `trends-mvp/trends-free-mvp/`. The MVP ran as a single-market (ZA) prototype and was
never intended for production. This document records every fix applied during porting so
the rationale is traceable and future contributors understand what changed and why.

The MVP source files are local-only reference material. They are not committed to the V2
repo. When specific line numbers are cited below, they refer to the state of the MVP files
as audited on 14 April 2026.

## Fixes by connector

### RSS (`src/ingestion/connectors/rss.py`)

Full port, no rewrite required. Source: `src/connectors/rss_connector.py` (24 lines).

**Fix 1: Market field was empty.** The MVP never populated the `market` column. V2 assigns
`market = self.market` from the connector instance so that enrichment and downstream scoring
can filter by market without guessing.

**Fix 2: News24, Premium Times Nigeria, Daily Nation Kenya removed.** These three feeds are
blocked as of 2026. News24 returns HTTP 403, Premium Times Nigeria uses bot protection,
Daily Nation Kenya is paywalled. They were removed from `configs/sources.yaml` before
porting began. Replacements: SABC News and Daily Maverick (ZA), Punch and Vanguard (NG),
The Standard and Tuko (KE), AllAfrica for cross-market coverage.

**Fix 3: Feed scope extended to all_markets.** The MVP fetched only the feeds listed for a
single market. V2 combines market-specific feeds with `all_markets` feeds so that
cross-regional sources (AllAfrica) appear in every run without duplication.

**Fix 4: URL deduplication added.** `drop_duplicates(subset=["url"])` prevents the same
article appearing twice when multiple feeds carry the same story.

### YouTube (`src/ingestion/connectors/youtube.py`)

Rich stub pending Phase 1.3. Source: `src/connectors/youtube_connector.py` (~51 lines).

**Fix 1: regionCode must be per-market.** The MVP hardcodes no region code, so YouTube
returns global results regardless of market. V2 maps `za -> "ZA"`, `ng -> "NG"`,
`ke -> "KE"` via `REGION_CODE_MAP` and passes it to the search API.

**Correction:** The MVP already calls `videos.list()` for statistics (youtube_connector.py
lines 27-30). Views, likes, and comments are correctly fetched. No fix needed here.

**Fix 2: API key via secrets module.** The MVP reads the key directly from `os.getenv`.
V2 uses `src/utils/secrets.py` so the key routes through Secret Manager in staging/prod
and `.env` in dev, consistent with the other connectors.

**Fix 4: Market field was empty.** Same as RSS Fix 1.

### GDELT (`src/ingestion/connectors/gdelt.py`)

Rich stub pending Phase 1.4. Source: `src/connectors/gdelt_connector.py` (~37 lines).

**Fix 1: FIPS country codes missing.** GDELT DOC 2.0 uses FIPS codes, not ISO 3166.
South Africa is `SF` (not `ZA`), Nigeria is `NI` (not `NG`), Kenya is `KE` (same).
The MVP omits `&sourcecountry=` entirely, which is why it returned zero rows in testing.
V2 adds `FIPS_CODES = {"za": "SF", "ng": "NI", "ke": "KE"}` and includes the parameter
on every query.

**Fix 2: Rate limit not respected.** GDELT enforces one request per five seconds.
The MVP fires queries without delay. V2 adds `RATE_LIMIT_DELAY = 5.0` and sleeps
between requests.

**Fix 3: Market field was empty.** Same as RSS Fix 1.

### EnsembleData (`src/ingestion/connectors/ensemble.py`)

Rich stub pending Phase 1.5. Source: `src/connectors/ensemble_connector.py` (602 lines).

**What to keep as-is:** The extraction helpers `_safe_get` (nested dict access with
fallback) and `_normalize_posts` (platform-specific JSON to 16-column schema) handle
EnsembleData's inconsistent response shapes correctly. The cursor-based pagination loop
for hashtag endpoints is correct. The 0.5 second sleep between paginated requests must
be preserved. Budget caps per query type are read from `sources.yaml` and must stay.

**Fix 1: Token via secrets module.** The MVP reads `ENSEMBLEDATA_API_TOKEN` via
`os.getenv` directly and logs the raw value in error URLs (MVP line 61). V2 reads the
token through `src/utils/secrets.py` and logs pass through `src/utils/log_redactor.py`
to strip the token from any URL that appears in structured logs.

**Fix 2: Market field was empty.** Same as RSS Fix 1.

**Fix 3: Wrap in BaseConnector.** The MVP implementation is a standalone class.
V2 wraps it in a `BaseConnector` subclass to inherit retry-on-429, structured logging,
and the `safe_fetch()` exception wrapper.

**Note on EnsembleData SDK:** The official `ensembledata` Python SDK (`pip install
ensembledata`) provides typed methods with unit tracking per call. The MVP predates
it and uses raw HTTP. V2 Phase 1.5 ports the raw HTTP logic as-is for May 1 stability.
SDK migration is Phase 2.

### BigQuery Trends (`src/ingestion/connectors/bigquery_trends.py`)

No MVP source. Designed from scratch for Phase 1.6.

This connector replaces the abandoned `pytrends` library (archived April 2025) as the
source of search intent data. It queries
`bigquery-public-data.google_trends.international_top_rising_terms` via the BigQuery
client, which requires no additional API key (uses Application Default Credentials).

The `percent_gained` column in the public dataset maps to `search_velocity_score` via
`LEAST(percent_gained / 1000.0, 1.0)` to normalise it to a 0-1 range. This is the
9th scoring signal added in V2.

Country codes in the public dataset are ISO 3166 alpha-2 (ZA, NG, KE) rather than
FIPS, unlike GDELT.

## Cross-connector fixes

**Reddit dropped.** The MVP included `src/connectors/reddit_connector.py` (69 lines).
Reddit is not in the V2 connector set. SSA market penetration on Reddit is minimal
compared to TikTok and YouTube, and Reddit's API pricing changed in 2023 to block
most third-party access. See ADR 0002.

**Scoring expanding from 6 to 9 signals.** The MVP's `pipeline.py` scoring function
(lines 348-356) uses six signals. V2 uses nine. Weights are in `configs/scoring.yaml`.
The MVP scoring code is not ported. The V2 equivalent is a BigQuery scheduled query
(Phase 1.8).

**Single-market hardcoding removed.** The MVP's configs and connectors are ZA-only.
V2 uses per-market config files at `configs/keywords/{za,ng,ke}.yaml` and
`configs/creators/{za,ng,ke}.yaml`, and each connector instance receives its market at
instantiation time.

## Phase 1 port tracking

This section is updated as each connector port completes in Phase 1.

| Connector | Phase | Status | Notes |
|---|---|---|---|
| RSS | 1.2 | Complete | Full port done in Phase 0.5 |
| YouTube | 1.3 | Stub | |
| GDELT | 1.4 | Stub | |
| EnsembleData | 1.5 | Stub | |
| BigQuery Trends | 1.6 | Stub | New design |
