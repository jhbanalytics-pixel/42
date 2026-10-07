# Operating 42

How 42 runs on Google Cloud: the jobs, the schedule, the caps, how a release goes out, how it is watched and who can open it. [Back to the README](../../README.md).

## Where things live

| What | Where |
|---|---|
| Project | `ogilvy-trends-v2` (staging), region `us-central1` |
| Warehouse | BigQuery, location `US`: `intelligence_42_core` and `intelligence_42_agent` |
| Images | Artifact Registry repository `intelligence-42` |
| Media | A staging bucket for clips, keyframes and exports, with a 30-day soft-delete backstop |
| Secrets | Secret Manager. Each secret is mounted only on the job or service that needs it and is never printed or logged |
| Production | Not deployed. A separate project, on the owner's written word |

## Jobs and schedule

Jobs are defined in `core/setup/deploy_jobs.py` and their schedules in `core/setup/schedule.py`. The table describes those source definitions, not a live readback of enabled jobs or schedules. Schedules use the Africa/Johannesburg time zone (SAST, UTC+2).

| Job | Runs | When (SAST) |
|---|---|---|
| `f42-gdelt` | `core.collect.gdelt`: news events into the seed queue | 01:30 daily |
| `f42-gdelt-daily` | `core.collect.gdelt_daily`: daily news aggregates | 03:30 daily |
| `f42-collect` | `core.collect.job`: chain stage 1 | 02:00 daily |
| `f42-understand` | `core.understand.job`: chain stage 2 | when collect finishes clean |
| `f42-detect` | `core.detect.job`: chain stage 3 | when understand finishes clean |
| `f42-brief` | `core.brief.job`: chain stage 4 | when detect finishes clean, with a 06:15 deadline trigger |
| `f42-digest` | `core.api.digest --send`: the daily alert email | 06:45 daily |
| `f42-scheduled-asks` | `core.api.scheduled --live`: questions saved on a schedule | 07:00 daily |
| `f42-calendar` | `core.collect.calendar --apply`: moments refresh | Monday 06:30 |
| `f42-drift` | `core.collect.drift`: seed drift report and calendar analogues | Monday 07:00 |
| `f42-learn` | `core.detect.learn`: scorecard, forecast scoring, weekly quality | Monday 07:30 |
| `f42-reconcile` | `core.collect.reconcile`: credit ledger against the supplier's totals | 23:30 daily |
| `f42-watchdog` | `core.setup.watchdog`: checks every stage and logs alerts | every 15 minutes 02:00 to 07:45, hourly otherwise |
| `f42-probe` | `core.collect.probe`: a one-off supplier probe | by hand |
| `f42-pulse`, `f42-breaking` | Intraday pulse and the hourly Breaking rule | built, switched off (`PULSE_READY = False`) |

The chain lives in `core/collect/chain.py`. Each stage calls `begin()`, which refuses unless the upstream stage wrote an ok `runs` row for the same day, then `finish()`, then starts the next stage with the same run date. From 06:15 SAST the brief publishes whatever passed, with a data-issue banner.

## Services

| Service | App | Does |
|---|---|---|
| `f42-api` | `core.api.app:app` | Serves the web app from `app/web/dist`, the read API and the sign-in gate. Forwards Ask, investigations, dossiers, findings, watches, schedules and skins to `f42-agent` |
| `f42-agent` | `core.api.agent_app:app` | Runs Ask and every write the app makes. Private, called only by `f42-api` with an ID token |

Flags for both live in `core/api/deploy_flags.env`.

## Caps

All credit and spend caps live in one file, `core/config/caps.yaml`, read by `core/config/caps.py` and enforced in `core/collect/socialcrawl_client.py` before every supplier call. A test fails if the file drifts from the documented table.

| Cap | Value | What stops |
|---|---|---|
| `ENGINE_DAILY` | 2,400 credits: collect 2,000, confirm 300, reserve 100 | Each morning step stops at its share |
| `ASK_DAILY` | 600 credits, all questions and investigations | Ask answers from the warehouse only, with a notice |
| `SCHEDULED_DAILY` | 120 credits, a share of `ASK_DAILY` | The due schedule is skipped and tried next time |
| `VIDEO_DAILY` | 60 clips and 600 credits | Video reading stops for the day |
| `PULSE_DAILY` | 60 credits | The intraday pulse stops |
| `EVAL_DAILY` | 0 live credits (100 for a deliberate refresh) | Evaluations run in replay mode |
| `MONTHLY` | 80,000 credits | Ask and build work are throttled first; the morning run is protected |
| `BALANCE_FLOOR` | 20,000 credits | Every job refuses to start below it |
| `MODEL_DAILY_USD` | USD 50 a day | Ask falls back to a quick lookup; the brief falls back to numbers and posts |

A per-question budget sits inside `ASK_DAILY` (`core/agent/context.py`):

| Depth | Credits | Calls | Turns |
|---|---|---|---|
| T0 quick lookup | 10 | 8 | 12 |
| T1 quick scan (default) | 60 | 20 | 30 |
| T2 deep read | 300 | 60 | 40 |
| T3 investigation | 600 | 120 | needs a confirmed plan |

## Releasing

Every release is a reviewed commit, built once, deployed by image digest. The commands below describe the release scripts. Staging writes require Albert's go for the complete reviewed paste; a dry run does not grant permission to apply.

```bash
# Jobs: dry run first, then build the image for HEAD and deploy every enabled job
py -3.13 core/setup/deploy_jobs.py
py -3.13 core/setup/deploy_jobs.py --build --apply

# Services: build, deploy f42-agent and f42-api, run the health check
bash core/api/deploy.sh
```

`deploy_jobs.py` refuses to build or deploy while `core/` has uncommitted changes, so an image tag always names exactly what is inside it. Nothing in these scripts removes a resource or starts a job. GitHub Actions workflows for staging and production are written and kept in `.github/parked-workflows/` until keyless deploys are switched on.

Scheduler access and changes are reserved for Albert's separate approval. Telegram, intraday pulse and Breaking stay off until he explicitly approves activation. The services build uses the source staging directory in `core/api/deploy_flags.env`, shared with the jobs builder. The recorded directory is `gs://ogilvy-trends-v2-f42-media-staging/build-source`; its presence in source does not prove a build or deployment succeeded.

## Identities and access

`core/setup/bootstrap.py` is the only thing that grants access. Without `--apply` it prints every create and grant it would make; with `--apply` it only adds what is missing, never removes or replaces a binding, and ends with a readback of every expected binding. Each job and service runs as its own service account with the narrowest roles it needs (collector, enricher, brief, agent, web, scheduler, deployer). No machine identity holds owner, editor or IAM admin.

The caller submitting a build, the build service account and the deployed service accounts are separate identities. The reviewed 7 October services paste expects Albert's existing owner-login caller, `f42-deployer` for Cloud Build, and `f42-agent` and `f42-web` for the running services. That caller exception applies to that paste only. Check all identities and the staging project before any write, and stop if they differ. Warehouse reads retain `f42-builder` impersonation. See [rule 13](../full-42/RULES.md).

People open 42 in one of two modes, set by `F42_AUTH_MODE` (`core/api/auth.py`):

- **Passcode** (the default). The app asks for the team passcode, checks it against the server, and rate-limits failed attempts per address. If the passcode secret is unset the gate stays closed.
- **Identity-Aware Proxy, read only.** Google sign-in checked against an allowlist. Only reads are allowed, and the app shows "Read-only access". See `core/api/IAP-PILOT.md`.

## Monitoring

`core/setup/monitoring.py` creates log-based alert policies, and the `f42-watchdog` job writes a "42 ALERT" line whenever something is wrong. Alerts cover a missing collection, a failed job, a brief not published by 06:30, low credits, model spend over 80% of its cap, zero rows collected, schema drift on a supplier route, a high Ask error rate, a reconcile mismatch, seed drift, a failed seed queue and failed detect views. Alerts are emailed to the owner.

The requested job memory alert above 70% is not defined by this module yet. Its separate policy needs a metric and notification-channel readback before creation. Preparing that policy does not change existing alerts.

The Coverage page shows the same picture to every user: runs of the day, credits charged, model spend against its cap and the weekly scorecard.

## Data protection

42 collects public posts in three countries, so South Africa's POPIA, Nigeria's NDPA 2023 and Kenya's Data Protection Act 2019 all apply. Legal review of all three comes before production. The policy 42 follows now:

- Collect only what people made public. Store handle and post id, never contact details.
- Never classify individuals by religion, race, health, politics, sex life or crime. Stance on political or religious topics appears only in aggregate.
- Link to posts rather than copying media of private people.
- A removal request suppresses the person everywhere at once (the Hidden people page). Data is deleted only on the owner's written word, and logged.
- No data is dropped, truncated or expired by any job.
