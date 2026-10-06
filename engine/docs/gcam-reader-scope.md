# GCAM-reader feature scope

Status: scoped, not built (2026-06-25). Makes the dark `v2gcam` signal flow into trend scoring as an emotional-arousal-intensity term. Today `v2gcam` is write-only: connectors write it, nothing reads it, so the `gcam_enabled` flip banks history but adds no live signal. This is the reader.

## Signal definition

`gcam_score` means emotional arousal intensity (not valence). A topic generating strong negative emotion (protest, crime spike) is as culturally significant for trends as one generating strong positive emotion (a celebration), so positive and negative arousal both lift the score.

Per-row intensity from the persisted `v2gcam` code:value string, using three of the target dims:

- `v10.1` Hedonometer happiness, 1-9 scale. Normalise `(v - 1) / 8`, clamp 0-1.
- `v19.1` LIWC-style positive-emotion aggregate. Normalise `min(v / 100, 1.0)`.
- `v19.9` LIWC-style negative-emotion aggregate. Same as v19.1.

Row score is the mean of whatever dims are present (divide by count-present, 1/2/3, not always 3), `NaN` when none present. `v10.2` excluded (redundant with v10.1), `v20.1` excluded (overlaps the existing `v2tone` signal).

## Data flow (confirmed against the code)

`v2gcam` does NOT need an `enriched_content` schema column. It rides the same path as the existing `tone_avg`: written on the raw row, parsed in memory during enrichment, consumed by aggregation, never persisted on `enriched_content`.

1. `GDELTConnector._fetch_gkg` writes `v2gcam` (gcam_enabled true). `gdelt.py`.
2. `build_raw_row` bridges the non-empty string to the raw row. `scripts/run_rss_now.py:197`.
3. `enrich_dataframe` parses `v2gcam` to a float `gcam_intensity` per row. `src/ingestion/enrichment.py`.
4. `_aggregate_by_topic` weight-sums `gcam_intensity` per topic, emits `gcam_avg_mean` + `gcam_rows`. `scripts/run_rss_now.py:278`.
5. `compute_trend_scores` folds `gcam_score * weight` into the composite, stores `gcam_score` + `gcam_rows` on the trend_scores row. `scripts/run_rss_now.py:853`.
6. `accuracy_watchdog.recompute_composite` includes `gcam_score` in the integrity check. `scripts/accuracy_watchdog.py:165`.

GDELT-only: `gcam_intensity` is NaN for every non-GDELT source, so only GDELT rows feed `gcam_rows`. A topic with no GDELT rows gets `gcam_rows=0`, `gcam_score=0.0`.

## Changes

Create:

- `scripts/migrations/add_gcam_score_column.py`. Idempotent `ADD COLUMN IF NOT EXISTS gcam_score FLOAT64, gcam_rows INT64` on `trend_scores`. Copy `add_v2gcam_column.py`. Apply before code ships.

Modify:

- `src/ingestion/enrichment.py`. Add `_parse_gcam_intensity(raw) -> float` after `_parse_v2tone` (replicate the token-split from `GDELTConnector._parse_gcam`, no import to avoid a circular dep). Add the `gcam_intensity` column block in `enrich_dataframe` after the v2tone block, mirroring it exactly.
- `scripts/run_rss_now.py`. (a) Three accumulators in the `_aggregate_by_topic` defaultdict: `gcam_intensity_sum`, `gcam_weight_sum`, `gcam_rows`. (b) Accumulate block after the tone block (skip NaN via `math.isnan`, weight-sum with the 1/N multi-assign correction). (c) Emit `gcam_avg_mean` + `gcam_rows` in the topic output. (d) In `compute_trend_scores`, add the gcam weight block and the `gcam_score * gcam_weight_effective` term to `signal_sum`. (e) Store `gcam_score` + `gcam_rows` on the row.
- `configs/scoring.yaml`. Add `gcam_score: 0.00`. Ships at zero so the live cron stays byte-identical and weights still sum to 1.00. Intended live weight `0.03`, carved from `tone_score` (0.05 -> 0.02), since tone is polarity and GCAM is arousal, complementary.
- `scripts/accuracy_watchdog.py`. Add `("gcam_score", "gcam_score")` to `_NON_TONE_KEYS`. Add `gcam_score` (and `gcam_rows`) to the `check_composite_integrity` SELECT. Extend `recompute_composite` with `gcam_rows` so the absent-signal redistribution matches `compute_trend_scores`. Without this the watchdog raises DRIFT.

## Weight redistribution (the byte-identical invariant)

When a signal is absent for a topic (`gcam_rows == 0` or `tone_rows == 0`), its nominal weight is redistributed across the present signals via `weight_scale`. The change: `absent_weight` must include `gcam_weight_nominal` when gcam is absent, so

```
absent_weight   = (tone_nominal if tone_effective==0 else 0) + (gcam_nominal if gcam_effective==0 else 0)
weight_scale    = 1 / (1 - absent_weight)
```

At `gcam_score: 0.00` on ship, `gcam_nominal = 0`, so `absent_weight` reduces to `tone_nominal` and the formula is identical to today. The redistribution only changes once the weight is raised to 0.03.

## Build sequence (TDD, failing tests first)

1. Migration: run `add_gcam_score_column.py --apply` on `trends_v2_dev`, confirm both columns. Confirm `scoring.yaml` weights still sum to 1.00 (`test_scoring_weights_sum_to_one`).
2. Parse + enrich. New `tests/unit/test_gcam_signal.py`: empty -> NaN; single dim uses only that dim; clamps to 0-1; full three dims = mean; aggregate accumulates; NaN rows give `gcam_rows=0`. Then implement.
3. Composite. Add to `tests/unit/test_run_rss_now.py`: weight-0.00 is byte-identical to no-gcam (the ship-safe invariant); nonzero weight lifts a high-gcam topic; both-absent redistribution still sums to 1.0. Then implement.
4. Watchdog. `tests/unit/test_accuracy_watchdog.py`: recompute includes gcam_score; absent-gcam redistribution matches run_rss_now. Then implement.
5. Flip + validate. Flag is already live, so rows accumulate immediately once the reader ships. Run a shadow cron, confirm `gcam_rows > 0` on at least one row, watchdog stays HEALTHY. Hold weight at 0.00 until 3+ days of non-zero `gcam_rows`, then raise `gcam_score: 0.03` / `tone_score: 0.02` and re-run the watchdog.

## Ordering trap (dead_signals)

At weight 0.00, `gcam_score` is excluded from the `dead_signals` check (it only inspects signals with weight > 0). Raise the weight ONLY after confirming `gcam_rows` are consistently non-zero. Raising the weight while every stored `gcam_score` is still 0.0 makes `dead_signals` FAIL.
