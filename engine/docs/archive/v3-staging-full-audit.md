# V3 staging full build audit

Generated for feat/v3-staging. Maps every v3.8 ticket to repo state. Staging goal: all code present, flags default false on prod deploy path, staging overrides true for manual verification.

See also: `docs/staging-full-flip-checklist.md`, `docs/v3-execution-log.md`, `docs/trends-engine-v3-blueprint-v3.8.md`.

## Track A

| ID | Status | Paths |
|---|---|---|
| A0 SCHEMA_ORDER | DONE | `scripts/setup_bigquery.py`, `tests/unit/test_schema_order_parity.py` |
| A1 seed_graph.sql | DONE | `infra/bigquery_schemas/seed_graph.sql`, `scripts/migrations/create_seed_graph_table.py` |
| A2 seed_graph.py + stoplist | DONE | `src/analysis/seed_graph.py`, `configs/seed_graph_stoplist.yaml` |
| A3 cron wiring | DONE | `scripts/run_rss_now.py`, `configs/cron_flags.env` |
| A4 backfill | DONE | `scripts/backfill_seed_graph.py` |
| A5 seed_path.py | DONE | `src/analysis/seed_path.py`, `tests/unit/test_seed_path.py` |
| A6a brief prompt | DONE | `src/analysis/generate_briefs.py`, `src/analysis/prompts/trend_brief.py` |
| A6b card render | DONE | `src/alerts/email_render/card.py`, `src/alerts/brief_loader.py` |
| A7 near-miss | DONE | `src/enrichment/embedding_classifier.py`, `src/ingestion/enrichment.py`, `scripts/migrations/add_near_miss_columns.py` |
| A8 structured IDs | PARTIAL | Handle via `author_handle` in seed_graph; ensemble music side-channel minimal |

## Track C

| ID | Status | Paths |
|---|---|---|
| C1 tables | DONE | `infra/bigquery_schemas/seed_candidates.sql`, `seed_outcomes.sql`, migrations |
| C2 ranker | DONE | `src/analysis/seed_candidates.py`, `tests/unit/test_seed_candidates.py` |
| C3 review CLI | DONE | `scripts/review_seed_candidates.py` |
| C4 Gemini assist | STUB | `scripts/propose_taxonomy_candidates.py` (dark, post-RECONCILE) |

## Track B

| ID | Status | Paths |
|---|---|---|
| B0 substrate | PARTIAL | Vendor pre-call in `run_rss_now.py`; get_dataset routing not fully swept |
| B0b FORCE_REINGEST | DONE | `run_rss_now.py` `_parse_force_reingest_markets`, `_cleanup_market_day_rows` |
| B1 corroboration counts | STUB | Scores persist; n_factual/n_social count columns dark pending migration |
| B2 grounding | STUB | `src/analysis/grounding_verifier.py` |
| B3/B4 | STUB | Documented in blueprint; no live reconcile promotion |

## Track D

| ID | Status | Paths |
|---|---|---|
| D2-D5 Twitter | STUB | Dark in `ensemble.py`; probe via `scripts/verify_live.py ensemble-probe twitter_user` |

## Phase E/F

| ID | Status | Paths |
|---|---|---|
| E email behaviour | STUB | `SEED_BEHAVIOUR_EMAIL_ENABLED` in cron_flags.env |
| F seed_outcomes | DONE table | `seed_outcomes.sql`; monthly read not wired |

## Infra / docs

| Item | Status | Paths |
|---|---|---|
| cron_flags + _DURABLE_FLAGS | DONE | `configs/cron_flags.env`, `tests/unit/test_workflow_env_parity.py` |
| flip-readiness rows | PARTIAL | `docs/flip-readiness.md` (extend per flag) |
| v3-execution-log | DONE template | `docs/v3-execution-log.md` |
| staging job doc | DONE | `docs/staging-full-flip-checklist.md` |
