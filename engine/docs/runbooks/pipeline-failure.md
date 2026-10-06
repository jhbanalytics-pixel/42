# Pipeline Failure Runbook

## Symptoms

The pipeline is failing if any of the following are true:

- `pipeline_runs` has no successful row for the current day
- `raw_content` row count is zero or far below baseline for the current day
- Slack alert fires for a failed run (once alerts are configured)
- `trend_scores` has no rows for today

## Diagnosis

```bash
# Check recent pipeline runs
bq query --nouse_legacy_sql \
  'SELECT market, run_id, status, total_rows, briefs_generated, errors, started_at
   FROM `ogilvy-trends-v2.trends_v2_dev.pipeline_runs`
   ORDER BY started_at DESC LIMIT 10'

# Check raw content volume today
bq query --nouse_legacy_sql \
  'SELECT source, market, COUNT(*) as row_count
   FROM `ogilvy-trends-v2.trends_v2_dev.raw_content`
   WHERE DATE(collected_at) = CURRENT_DATE()
   GROUP BY source, market ORDER BY source, market'

# Check Cloud Run logs
gcloud logging read \
  'resource.type=cloud_run_revision AND severity>=ERROR' \
  --limit 20 --project ogilvy-trends-v2 --format json | jq '.[] | .textPayload'
```

## Common causes and fixes

**Connector import error.** A syntax error in a connector file prevents the module from loading. Check `error_message` in `pipeline_runs`. Fix the code, redeploy.

**Config validation failure.** `config_loader.py` raises on startup if a YAML config fails JSON Schema validation. Check the error message. Fix the config file, rerun.

**BigQuery insert failure.** The service account may lack write permissions. Verify `roles/bigquery.dataEditor` is granted to the Cloud Run service account on the `trends_v2` dataset.

**Secret Manager lookup failure.** The service account needs `roles/secretmanager.secretAccessor`. Check the IAM binding and verify the secret version is not disabled.

**Memory exceeded on Cloud Run.** EnsembleData fetches can be large. If the function OOMs, increase the memory limit in the Cloud Run configuration and reduce pagination limits in `sources.yaml`.

## Manual rerun

The full pipeline runs in the cloud (the BigQuery + Vertex RPC path segfaults on
Win + Py3.13), and one run covers all three markets. There is no per-market or
`--dry-run` flag.

```bash
# Cloud Run job (all markets, one pass). The run is GCP-native; GitHub holds
# no execution path.
gcloud run jobs execute trends-engine-pipeline --project=ogilvy-trends-v2 --region=us-central1
```

The entry point is `scripts/run_rss_now.py`; it loops za, ng and ke in one pass.
The idempotency guard skips any market already marked successful in
`pipeline_runs`, so a rerun is safe. See DEVELOPMENT.md "Trigger" for the job region.
