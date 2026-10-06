# Incident Response Runbook

**GCP project:** `ogilvy-trends-v2`
**Slack channel:** TBD (configure in Phase 1.10)
**On-call week 1:** Albert Meintjes (albert.meintjes@ogilvy.co.za)
**On-call week 2+:** Thapelo Masebe

## Severity levels

**Green** (resolve within 24h, no escalation): single connector offline, data quality dip < 2%, cosmetic dashboard issue.

**Yellow** (escalate to Jo within 4h): two or more connectors offline, data quality 2-10%, SLA at risk, brief approval delay.

**Red** (Jo notifies Google within 1h): all connectors down, SLA missed, data quality > 10%, legal/compliance issue, creator brand incident.

## Detection

Check these in order:

1. `pipeline_runs` BigQuery table: `SELECT * FROM trends_v2_dev.pipeline_runs ORDER BY started_at DESC LIMIT 5`
2. Cloud Run logs: `gcloud logging read "resource.type=cloud_run_revision" --limit 50 --project ogilvy-trends-v2`
3. Slack alerts (once configured): #trends-alerts channel

## Triage checklist

- Which market(s) are affected?
- Which connector(s) are failing?
- Is it a transient error (network, quota) or a permanent failure (auth, config)?
- How many rows are in `raw_content` for today? `SELECT COUNT(*) FROM trends_v2_dev.raw_content WHERE DATE(collected_at) = CURRENT_DATE()`
- Is the enrichment and scoring pipeline downstream affected?

## Common fixes

**SocialCrawl `INSUFFICIENT_CREDITS`.** The credit balance is prepaid and does not reset at midnight, so waiting does not help. Check the balance with the free `GET /v1/credits/balance` and buy more; under 5,000 is the reorder trigger. A same-day re-run is close to free because cache hits and failed lookups bill nothing. The per-run breaker is `socialcrawl.budget_credits_per_run` in `configs/sources.yaml`.

**EnsembleData 495 (quota exhausted).** HISTORICAL. The vendor account was cancelled on 23 Jul 2026 and `ensemble_budget.enabled` is false, so this cannot fire on a live path.

**YouTube quota exhausted (HTTP 403 quotaExceeded).** Daily quota resets at midnight Pacific. No workaround. Reduce `max_results` in `sources.yaml` if hitting this regularly.

**GDELT rate limit (HTTP 429).** Wait 5 seconds between requests. The connector enforces `RATE_LIMIT_DELAY = 5.0`. If a bug bypassed this, the delay will self-correct on the next run.

**RSS feed down (HTTP 403 or 503).** Remove the feed from `configs/sources.yaml` for the affected market. Log it as a known broken feed. The pipeline continues without it.

**BigQuery auth failure.** Run `gcloud auth application-default login` and verify ADC is current. In Cloud Run, check the service account has `roles/bigquery.dataEditor` and `roles/bigquery.jobUser`.

## Rollback

The full pipeline (BigQuery + Vertex) runs in the cloud, not locally (the gRPC
RPC path segfaults on Win + Py3.13), and one run covers all three markets in a
single sequential pass. There is no per-market or `--dry-run` flag.

To rerun a failed pipeline run, trigger the Cloud Run job. There is no second
runner to fall back to:

```bash
# Rerun the Cloud Run job (all markets, one pass). The run is GCP-native;
# GitHub holds no execution path.
gcloud run jobs execute trends-engine-pipeline --project=ogilvy-trends-v2 --region=us-central1
```

The idempotency guard skips any market already marked successful in
`pipeline_runs`, so a rerun is safe. On a cloud-auth box the same entry point is
`python scripts/run_rss_now.py` (no flags, all markets). See DEVELOPMENT.md
"Trigger" for the exact job region.

## Contacts

| Name | Role | Contact |
|---|---|---|
| Albert Meintjes | Architecture lead, primary on-call week 1 | albert.meintjes@ogilvy.co.za |
| Thapelo Masebe | Operations, primary on-call week 2+ | thapelo.masebe@ogilvy.co.za |
| Joseph De Bruyn | Data Director, Google liaison | joseph.debruyn@ogilvy.co.za |
| Melissa Carney | Insights and reporting | melissa.carney@ogilvy.co.za |
