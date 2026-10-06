# Quota Exhaustion Runbook

## SocialCrawl (credit exhaustion)

SocialCrawl is the primary social vendor since 23 Jul 2026. Credits are prepaid and do
not reset daily, so there is no quota to wait out. The per-run breaker is
`socialcrawl.budget_credits_per_run` in `configs/sources.yaml`, shared across all three
markets, with each phase taking a share of it.

**How to detect:** `pipeline_runs.errors` carries `INSUFFICIENT_CREDITS`, which halts the
run rather than letting it read as an empty day. Spend per run lands in
`pipeline_runs.socialcrawl_credits`.

**Immediate fix:** Read the balance with the free `GET /v1/credits/balance` and buy
credits. Under 5,000 is the reorder trigger. Then rerun the pipeline via the Cloud Run
job (`gcloud run jobs execute trends-engine-pipeline --project=ogilvy-trends-v2 --region=us-central1`);
one run covers all markets, there is no per-market entry point. The re-run is close to
free because cache hits and failed lookups bill nothing.

**Prevent recurrence:** Watch the balance, not the day. Derive the observed daily burn
from `pipeline_runs.socialcrawl_credits`, never from a figure written in a config file,
and divide the balance by it for runway. Support is `admin@ridiocompany.com`.

## EnsembleData (HTTP 495), historical

HTTP 495 was EnsembleData's custom quota-exhaustion code on the Bronze tier (5,000
units/day). The vendor account was cancelled on 23 Jul 2026 and `ensemble_budget.enabled`
is false, so no live path can raise it. Kept because the connector is still in the
registry.

## YouTube Data API (HTTP 403 quotaExceeded)

YouTube's free tier: 10,000 quota units per day. A single search request costs 100 units.
At one run per day across 3 markets with 10 queries each: 3,000 units. The uncapped
yt-dlp path (`youtube_scrape`, live on all three markets) carries the extra breadth at
zero quota.

**How to detect:** `error_message` contains "quotaExceeded". The response body includes
`reason: quotaExceeded`.

**Immediate fix:** Wait for midnight Pacific (quota resets per GCP project per calendar
day Pacific time). No paid upgrade path exists for the YouTube Data API v3.

**Prevent recurrence:** Reduce `max_results` or query count in `configs/sources.yaml`
under `youtube`. Batch similar queries across markets where possible.

## BigQuery (scan volume)

BigQuery is billed per TB scanned for ad-hoc queries. Scoring runs in Python inside the
daily job, not as a scheduled query, and it reads the `enriched_content` table, which
grows over time.

**How to detect:** GCP billing alert at $50/month threshold fires. Check the BigQuery
billing breakdown in the GCP console.

**Prevent recurrence:** Add partition filters to queries. The `enriched_content` table
partitions on `collected_at`. Always include `WHERE DATE(collected_at) >= DATE_SUB(...)`.

**Emergency:** If a rogue query is scanning far more than expected, kill it:

```bash
# List running jobs
bq ls -j --project ogilvy-trends-v2

# Cancel a specific job
bq cancel --project ogilvy-trends-v2 {job_id}
```
