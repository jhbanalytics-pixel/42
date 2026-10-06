# V3 staging full flip checklist

Staging validates the entire V3.8 build before any prod flag flip. Prod jobs keep flags default false via `configs/cron_flags.env`; staging jobs override all V3 flags true for manual runs.

## TEV2 staging job

Clone `trends-engine-phase2` as `trends-engine-staging` (same image, separate env). Manual test run:

```bash
gcloud run jobs execute trends-engine-staging \
  --region us-central1 \
  --project ogilvy-trends-v2 \
  --update-env-vars TREND_DATE_INPUT=2026-07-01,SEED_GRAPH_ENABLED=true,NEAR_MISS_CAPTURE_ENABLED=true,SEED_PATH_RENDER_ENABLED=true,SEED_CANDIDATES_ENABLED=true,PHASE_2_ENABLED=true
```

Never set `FORCE_REINGEST_MARKETS` on the job definition. Use per-execution `--update-env-vars` only for scoped recovery.

## LP staging service

See `Listening Post/docs/staging-deploy.md`. Service: `listening-post-staging`.

Required env: `BEHAVIOUR_SCAN_ENABLED=true`, `RESEARCH_CLIENT_METRICS_DEFAULT=true`.

## Feature verification matrix

| Ticket | Flag / surface | Staging verify |
|---|---|---|
| A0 | SCHEMA_ORDER test | `py -3.13 -m pytest tests/unit/test_schema_order_parity.py -q` |
| A1-A2 | seed_graph table + builder | BQ `SELECT COUNT(*) FROM seed_graph WHERE trend_date=@d` all markets |
| A3 | SEED_GRAPH_ENABLED cron stage | Job log shows seed_graph row counts |
| A4 | backfill_seed_graph.py | `--dry-run` bytes, then chunked apply |
| A5 | build_seed_path | Unit tests + LP `/api/seed-path` |
| A6a/A6b | SEED_PATH_RENDER_ENABLED | Brief prompt block + email card (three-brief spot check) |
| A7 | NEAR_MISS_CAPTURE_ENABLED | enriched_content near_topic populated; classify parity test |
| A8 | structured IDs | seed_graph term_type handle/music rows after ensemble ingest |
| C1-C2 | SEED_CANDIDATES_ENABLED | seed_candidates pending rows Monday + fast lane |
| C3 | review_seed_candidates.py | `--list` shows pending; approve emits YAML diff |
| C4 | PROPOSE_TAXONOMY_CANDIDATES_ENABLED | Stub returns empty (post-RECONCILE watch) |
| B0 | fetch_units_history pre-call | Cron log line before ingest |
| B0b | FORCE_REINGEST_MARKETS | Per-execution only; cleanup + no duplicate day rows |
| B1 | CORROBORATION_COUNTS_ENABLED | n_factual/n_social on trend_scores when on |
| B2 | grounding_verifier stub | Import + unit test green |
| D2-D5 | twitter dark | Probe documented; no cron spend |
| E | SEED_BEHAVIOUR_EMAIL_ENABLED | Email section hidden when false |
| LP-R1-R8 | LP staging env | `Listening Post/docs/staging-qa-checklist.md` |
| LP1-LP3 | Seed Explorer | `#/seedpath`, adjacency under 5s cold |

## Prod blockers (staging still testable)

| Blocker | Affects prod flip | Staging |
|---|---|---|
| X legal sign-off | D4-D5 Twitter | Code wired, flag off |
| RECONCILE 7-day watch | C4 Gemini assist | C4 stub only |
| C2 FLOOR calibration | SEED_CANDIDATES prod flip | Run after A4 backfill on staging BQ |
| B1 shadow days | Corroboration chip prod | Dark flag test on staging |
| Thapelo deck sign-off | LP-R7 prod | Staging QA first |
