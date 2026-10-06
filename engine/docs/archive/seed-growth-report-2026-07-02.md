# Seed growth loop report (2026-07-02)

Dataset: `ogilvy-trends-v2.trends_v2_dev` only. Branch: `feat/v3-staging`. No prod cron, no `sources.yaml` changes.

## Dry-run (2026-06-18 to 2026-07-01)

`py -3.13 scripts/backfill_seed_graph.py --start 2026-06-18 --end 2026-07-01 --dry-run`

Bytes processed: **2,115,592** (enriched_content scan for the window).

## Backfill execution

Persist path: partition load `seed_graph$YYYYMMDD` with `WRITE_TRUNCATE` (see `persist_seed_graph` in `src/analysis/seed_graph.py`, commit lineage includes 6e7c059 / 8cffcb7). Shell uses `logging.disable(CRITICAL)`, `TRENDS_ENV=dev`, `GCP_PROJECT=ogilvy-trends-v2`.

### Failures on first pass

The PowerShell day loop stopped on **2026-06-18..2026-06-22 OK**, then **2026-06-23**:

- Attempts 1 and 2: process exit **-1073741819** (Windows access violation on the BigQuery client during heavy work).
- Attempt 3: build returned empty counts; root cause was **MemoryError** in `build_seed_graph_rows` co-occurrence aggregation on wide term sets.

Fix shipped in tree: cap co-occur keys per bucket (`MAX_CO_OCCUR_KEYS`), skip co-occur when a row has more than `MAX_CO_OCCUR_ROW_TERMS` terms, static load schema (no `get_table` on persist).

### Second pass (2026-06-23 to 2026-07-01)

Single script run completed with exit 0 (~27.5 min). Per-day builder log (enriched rows scanned, seed_graph rows written per market cap 5000):

| trend_date | enriched_rows | za | ng | ke | partition_total |
|------------|---------------|-----|-----|-----|-----------------|
| 2026-06-18 | 13249 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-19 | 13232 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-20 | 12792 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-21 | 11437 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-22 | 11302 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-23 | 13388 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-24 | 14398 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-25 | 14773 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-26 | 14592 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-27 | 38236 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-28 | 53721 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-29 | 18094 | 5000 | 5000 | 5000 | 15000 |
| 2026-06-30 | 20304 | 5000 | 5000 | 5000 | 15000 |
| 2026-07-01 | 14931 | 5000 | 5000 | 5000 | 15000 |

**BigQuery verify** (`seed_graph`, grouped by trend_date and market): 42 rows, **210,000** total (14 days x 3 markets x 5000 cap). Every cell is 5000.

View `v_seed_first_seen` reapplied via `scripts/migrations/create_seed_graph_table.py --apply`.

## C2 ranker (2026-07-01)

`run_seed_candidates_stage(date(2026, 7, 1))` after `create_seed_candidates_table.py --apply`.

Code fixes during run:

- `_seq_field()` for ARRAY columns coming back as numpy/pandas types in `aggregate_terms`.
- `proposed_date` cast to date before `load_table_from_dataframe`.

**Inserted:** 6 rows (fast lane only; 2026-07-01 is not a Monday so weekly lane skipped).

| market | lane | count |
|--------|------|-------|
| ke | fast | 5 |
| ng | fast | 1 |
| za | fast | 0 |

### Top candidates by score (2026-07-01)

| score | market | lane | type | value |
|-------|--------|------|------|-------|
| 0.5637 | ke | fast | keyword | anthem |
| 0.5579 | ke | fast | keyword | quality |
| 0.5510 | ke | fast | keyword | flow |
| 0.5450 | ke | fast | keyword | fast |
| 0.5348 | ke | fast | keyword | available |
| 0.4560 | ng | fast | keyword | days |

Low za/ng yield is expected on a single fast-lane day with caps, novelty gates, and hot-topic overlap filters. Re-run weekly lane on a Monday after more history for richer pools.

## Growth receipts (cross-platform, Jun window)

Terms with 2+ platforms and event_date in 2026-06-10..2026-07-01 (generic geo tokens excluded):

| term | market | platforms | span_days | platform trail |
|------|--------|-------------|-----------|----------------|
| energy | ke | 6 | 19 | instagram, news, reddit, tiktok, web, youtube |
| bafana | za | 6 | 16 | instagram, news, reddit, search, tiktok, web |
| reach | ke | 6 | 14 | facebook, instagram, reddit, tiktok, web, youtube |
| google | ke | 5 | 20 | facebook, news, reddit, web, youtube |
| group | ke | 5 | 20 | instagram, news, reddit, web, youtube |
| mentions | ke | 5 | 19 | facebook, instagram, tiktok, web, youtube |
| every | ke | 5 | 19 | instagram, reddit, tiktok, web, youtube |
| year | ke | 5 | 19 | instagram, news, reddit, tiktok, web |

Example trail detail (energy, ke): instagram@2026-06-25, news@2026-06-17..30, reddit@2026-06-20..28, tiktok@2026-06-11, web@2026-06-21, youtube@2026-06-11..24.

## Blockers resolved

1. Co-occur MemoryError on dense days (2026-06-23+).
2. Windows BQ client instability under load (mitigated by logging off, partition loads, smaller in-memory co-occur maps).
3. Ranker numpy truth-value bug on repeated fields.
4. Ranker parquet date typing on insert.

## Next steps for prod `SEED_GRAPH_ENABLED`

1. Merge `feat/v3-staging` after review; ensure Cloud Run job image includes seed_graph + seed_candidates modules and migrations applied on **prod** dataset (separate runbook step, not done here).
2. Flip `SEED_GRAPH_ENABLED=true` on dev cron first; watch one nightly partition (row counts ~15k/day, runtime on heavy enriched days).
3. Schedule weekly candidate lane (Monday) plus monitor fast-lane insert counts.
4. Consider raising or market-splitting `TERM_CAP_PER_MARKET` if 5000 truncation hides tail terms on spike days (2026-06-27/28 enriched 38k/53k).
5. Optional: NDJSON partition load for `seed_candidates` to match seed_graph and avoid dataframe/pyarrow struct edge cases on Windows ops laptops.

## Commands reference

```bash
py -3.13 scripts/backfill_seed_graph.py --start 2026-06-18 --end 2026-07-01 --dry-run
py -3.13 scripts/backfill_seed_graph.py --start 2026-06-18 --end 2026-07-01
py -3.13 scripts/migrations/create_seed_graph_table.py --apply
py -3.13 scripts/migrations/create_seed_candidates_table.py --apply
```
