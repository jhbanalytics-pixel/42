# V3 staging audit (2 Jul 2026)

Branch: `feat/v3-staging` (TEV2). LP: `feat/lp-r-staging`.

Score: **7/10** staging-complete for Google pitch. Track A dark stack ships with tests green; LP-R1 through LP-R5 code is on the LP branch with unit tests passing. Prod flips blocked until staging deploy + manual visual QA.

## TEV2: what is on the branch

| Ticket | Status | Notes |
|---|---|---|
| A0 SCHEMA_ORDER | Done | seed_insights.sql, system_events.sql, seed_graph.sql in SCHEMA_ORDER; test_schema_order_parity green |
| A1 seed_graph table | Done | infra/bigquery_schemas/seed_graph.sql + create_seed_graph_table.py migration |
| A2 seed_graph builder | Done | src/analysis/seed_graph.py (extract, platform axis, PII guard, stoplist) |
| A3 cron wiring | Done | SEED_GRAPH_ENABLED stage in run_rss_now.py; default false |
| A4 backfill | Done | scripts/backfill_seed_graph.py with --dry-run |
| A5 seed_path | Done | src/analysis/seed_path.py dark |
| A7 near-miss | Done | classify_batch_scored additive; add_near_miss_columns.py; NEAR_MISS_CAPTURE_ENABLED default false |
| B0 vendor pre-call | Done | fetch_units_history logged at cron start |
| Flags | Done | cron_flags.env + _DURABLE_FLAGS entries; all default false |

## TEV2: what stays dark (not flipped)

- SEED_GRAPH_ENABLED=false
- NEAR_MISS_CAPTURE_ENABLED=false
- SEED_PATH_RENDER_ENABLED=false
- No prod migration apply in this audit (run create_seed_graph_table.py --apply before first flip)

## TEV2 test results

```
py -3.13 -m pytest tests/unit/test_seed_graph.py tests/unit/test_schema_order_parity.py tests/unit/test_seed_path.py tests/unit/test_workflow_env_parity.py -q
28 passed

py -3.13 -m pytest tests/unit -q --tb=no -p no:cacheprovider
1643 passed, 1 xfailed
```

## TEV2 dataset audit

grep for hardcoded `trends_v2_dev` in new modules: **clean** (seed_graph.py and seed_path.py use get_dataset()).

## TEV2 staging deploy (manual)

Clone the cron job for staging dataset validation:

```bash
# One-time: clone pipeline job for staging (do not touch prod cron)
gcloud run jobs create trends-engine-staging \
  --project ogilvy-trends-v2 \
  --region us-central1 \
  --image $(gcloud run jobs describe trends-engine-pipeline --project ogilvy-trends-v2 --region us-central1 --format='value(spec.template.spec.containers[0].image)') \
  --set-env-vars TRENDS_ENV=dev,SEED_GRAPH_ENABLED=false \
  --set-secrets ... # mirror pipeline job secrets

# Dry-run backfill bytes estimate (local ADC)
py -3.13 scripts/backfill_seed_graph.py --start 2026-06-25 --end 2026-06-30 --dry-run

# Apply migration before flip
py -3.13 scripts/migrations/create_seed_graph_table.py --dry-run
py -3.13 scripts/migrations/create_seed_graph_table.py --apply
```

Or use existing `trends-engine-phase2` with per-execution env (does not persist):

```bash
gcloud run jobs execute trends-engine-phase2 \
  --project ogilvy-trends-v2 \
  --region us-central1 \
  --update-env-vars TRENDS_ENV=dev,SEED_GRAPH_ENABLED=true,TREND_DATE_INPUT=2026-07-01
```

Never run full pipeline twice same day on prod dataset.

## LP: what is on feat/lp-r-staging

| Ticket | Status |
|---|---|
| LP-R1 client metrics | Done (ab90ac2) |
| LP-R2/R2b posts+comments | Done |
| LP-R3 consolidated doc | Done (API + synth + UI) |
| LP-R5 evidence pack | Done (render_evidence_pack_html + route) |

## LP test results

```
py -3.13 -m pytest tests/unit/test_consolidated_research.py tests/unit/test_behaviours.py -q
20 passed
```

## LP staging URL

Deploy per `Listening Post/docs/staging-deploy.md`:

```bash
gcloud run services describe listening-post-staging \
  --project ogilvy-trends-v2 --region us-central1 --format='value(status.url)'
```

Console: `https://<staging-url>/#/console?work=brief`

## Visual QA checklist (manual, not run in CI)

| Item | Expected | Result |
|---|---|---|
| Console loads | Passcode gate, workbench | **PENDING** (needs staging deploy) |
| Behaviour scan | Step 1 board on brief path | **PENDING** |
| Client metrics | posts/comments/engagement, no trend_score until toggle | **PENDING** |
| Comment examples | voice_kind comment rows | **PENDING** |
| Build consolidated doc | Multi-behaviour synthesis | **PENDING** |
| Evidence pack link | /api/research/{id}/evidence-pack.html | **PENDING** |

## Prod flip blockers

1. TEV2: apply seed_graph migration to prod BQ; one clean cron with SEED_GRAPH_ENABLED=true on dev/staging first
2. LP: staging checklist sign-off; Thapelo deck review
3. One flip per cron day rule for SEED_GRAPH_ENABLED prod flip (after PR #216 merge + 2 Jul cron clean)

## P0 bugs

| Bug | Status |
|---|---|
| None found in unit tests | Fixed N/A |

Open: staging visual QA not executed (no live staging URL confirmed this session).
