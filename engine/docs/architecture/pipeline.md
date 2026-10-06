# Pipeline Architecture

## Data flow

The live pipeline is a single Python run, once a day on UTC. Everything is
GCP-native: Cloud Scheduler POSTs the Cloud Run job's `:run` API at 00:30 UTC,
a second Scheduler fires the same job at 02:30 UTC as a fallback behind an
idempotency guard, and a watchdog runs at 06:30 UTC. GitHub holds no execution
path, only `ci.yml` for lint, test and scan. The job invokes
`scripts/run_rss_now.py`, which runs the whole chain in one process.

```
Cloud Scheduler -> Cloud Run job (00:30 UTC primary, 02:30 UTC fallback, idempotency-guarded)
  scripts/run_rss_now.py:
    per market (za, ng, ke), the enabled connectors in registry order
      (CONNECTORS in run_rss_now.py; the README connector table is the
       authoritative live list, the dark ones stay wired in at zero rows)
    concat -> enrich_dataframe() -> BigQuery: raw_content + enriched_content
    composite scoring -> BigQuery: trend_scores
    Vertex AI Gemini briefs (model from GEMINI_MODEL, live gemini-3.5-flash)
      -> BigQuery: trend_analysis
    Gmail SMTP PULSE digest to stakeholders
    log run -> BigQuery: pipeline_runs
  Looker Studio <- BigQuery views
```

The BQML 7-day forecast OUTLOOK chip is retired, not dormant:
`FORECAST_ENABLED=false` since 7 Jun 2026 because the predictor lost to naive
persistence twice.

The earlier Pub/Sub plus scheduled-query design (separate ingest, score,
analyze, report functions wired through Pub/Sub triggers, five connectors,
three SAST fires a day) is superseded, and the function names in the table
below are left over from it.

## Component contracts

### Connectors

All connectors extend `src/ingestion/connectors/base.BaseConnector`. They accept a
`market` string at instantiation and implement `fetch(**kwargs) -> pd.DataFrame`.

Output: exactly the `RAW_COLUMNS` schema defined in
`src/ingestion/connectors/base.py` and returned by
`BaseConnector.empty_dataframe()`. It is 21 columns now (the original 16 plus
the GDELT GKG `v2tone` / `v2persons` / `v2orgs` / `v2locations` fields and
`v2gcam`), so read the list rather than a count.

Failure contract: `safe_fetch()` catches all exceptions and returns an empty DataFrame.
The pipeline continues. The failure is logged to `pipeline_runs.error_message`.

### Enrichment layer

`src/ingestion/enrichment.enrich_dataframe(df, market)` takes the concatenated raw output
of all connectors for a market and returns an enriched DataFrame ready for the
`enriched_content` BigQuery table. It adds: `query_group`, `slang_score`, `slang_terms`,
`regional_score`, `genz_score`, `creator_watchlist_tier`, `creator_watchlist_score`,
`search_velocity_score`, `pipeline_run_id`, `tone_avg`, `tone_polarity` and
`topic_groups`. `ENRICHED_COLUMNS` in `base.py` is the full list. It
deduplicates on `url`.

### Scoring

Scoring is Python inside the same run, not a BigQuery scheduled query.
`scripts/run_rss_now.py` reads the enriched rows and computes the composite per
topic per market, then applies the cross-source channel-family multiplier and
caps at 1.0. Output is written to `trend_scores`. The weights come from
`configs/scoring.yaml` and must sum exactly to 1.00: ten signals carry live
weight, and two more (`momentum`, `gcam_score`) sit in the block at zero live
contribution behind their own flags. See docs/scoring-methodology.md.

### BigQuery tables

| Table | Writer | Reader |
|---|---|---|
| `raw_content` | ingest function | enrichment layer, ad-hoc analysis |
| `enriched_content` | enrichment layer | scoring query |
| `trend_scores` | scoring query | Looker Studio, analyze function |
| `trend_analysis` | analyze function (Phase 2) | creator brief generation |
| `creator_briefs` | analyze function (Phase 2) | report function, Looker Studio |
| `trend_cycles` | manual (configs/trend_cycles.yaml) | analyze function |
| `ugc_tracking` | tracking module (Phase 2) | Looker Studio |
| `pipeline_runs` | ingest function | monitoring, incident response |

## Security model

**Dev:** Application Default Credentials (user account via `gcloud auth application-default login`). Secrets from `.env`.

**Staging/Prod:** Service account with least-privilege IAM roles (`roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, `roles/secretmanager.secretAccessor`). Secrets from Secret Manager.

Logs pass through `src/utils/log_redactor.py` before writing to Cloud Logging. API tokens are redacted from all log output including error URLs.

## Scaling assumptions

These were the Phase 1 launch assumptions. The run count is the one that
changed: the pipeline fires once a day, covering all three markets in one
sequential pass, not three times.

- 1 pipeline run per day covering 3 markets in one process
- ~500-1,000 raw rows per market per run
- Enrichment and scoring on ~15,000 rows per day
- 30 Gemini analyses per day (Phase 2)
- Looker Studio reads: on-demand, cached by BigQuery

Cloud Run `min-instances=1` for the ingest function to avoid cold-start timeout on the paginated social pull. Timeout set to 540 seconds.
