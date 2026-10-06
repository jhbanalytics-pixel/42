# Monitoring as code

`definitions.json` declares, for project `ogilvy-trends-v2`, one email notification channel (the owner account `jhb.analytics@gmail.com`, no other recipient), seven log based counter metrics and seven alert policies. `apply_monitoring.py` is the only writer. It owns a log metric by its exact `intelligence_42_*` name, and a channel or policy by the `userLabels` pair `managed_by=intelligence-42-monitoring` and `monitoring_key=<key>`. It never deletes, and it never touches a resource without that name or label pair. `ops/tests/test_monitoring_as_code.py` checks every cited source line for its literal, and checks every filter value against those literals and the resource manifest.

## Alerts and the code each filter comes from

All daily and renewal lines are single line JSON on stdout. Cloud Run parses each one into `jsonPayload` under `run.googleapis.com%2Fstdout`. Stderr lines land under `run.googleapis.com%2Fstderr`.

| Policy (severity) | Metric and filter | Source |
|---|---|---|
| `daily-success-absent-30h` (WARNING), `daily-success-absent-48h` (CRITICAL) | `intelligence_42_daily_success`: job `intelligence-42-daily-staging`, stdout, `jsonPayload.mode="daily" AND jsonPayload.status="succeeded"` | `ops/runners/managed_runtime.py:1349` writes the result; `:961`, `:962` set `mode` and `status`; `engine/src/analysis/open_intelligence/daily_cycle.py:166` returns `succeeded` |
| `price-renewal-success-absent-30h` (ERROR) | `intelligence_42_price_renewal_success`: job `intelligence-42-price-policy-staging`, stdout, `jsonPayload.command="renew-unattended" AND jsonPayload.state=("activated" OR "already_activated")` | `ops/deploy/refresh_question_policy.py:1626` prints the summary; `:1420`, `:1460`, `:1463`; `:1075` `_SUCCESS_STATES` |
| `question-policy-refused` (ERROR) | `intelligence_42_question_policy_refused`: service `listening-post-staging`, stderr, `contract_version="general_question_admission_stage_v1"`, `stage="policy_freshness"`, `state="failed"`, `error_code="approval_required"`; OR `intelligence_42_renewal_policy_refused`: price policy job, stdout, `command="renew-unattended"`, `state="refused"`, `error=("observation_stale" OR "policy_expired")` | `engine/src/analysis/open_intelligence/general_question_policy.py:276`, `:279` raise the two messages; `general_question_admission.py:225`, `:191` map them to `approval_required` at stage `policy_freshness`; `general_question_parent_context.py:190` to `:205` build the event; `app/src/api/question_worker_process.py:474` logs it message only (`:57`). Renewal: `ops/deploy/refresh_question_policy.py:93`, `:95`, `:832`, `:1450` |
| `daily-job-failure` (ERROR) | Cloud Run metric `run.googleapis.com/job/completed_execution_count` with `result="failed"` for the daily job; OR `intelligence_42_daily_failure`: daily job, stdout, `jsonPayload.status="refused" OR (jsonPayload.mode="daily" AND jsonPayload.status="failed")` | `ops/runners/managed_runtime.py:1346` (refusal, exit 1); `:961`, `:962`; `daily_cycle.py:29` |
| `vendor-failure` (WARNING) | `intelligence_42_vendor_failure`: jobs `intelligence-42-daily-staging` or `intelligence-42-ingest-staging`, stderr, `textPayload:"Failed to fetch from "` or `textPayload:"socialcrawl account out of credits"` | `engine/src/ingestion/connectors/base.py:166`; `engine/src/ingestion/connectors/socialcrawl.py:659`; plain text format from `engine/scripts/run_rss_now.py:96`, imported in process by `engine/scripts/staging/collect_42_sources.py:255` |
| `semantic-canary-failed` (ERROR) | `intelligence_42_semantic_canary_failed`: daily job, stderr, `severity="ERROR" AND jsonPayload.message="qa_semantic_canary_failed"` | Commit `17ea74c` on `feat/42-l04-daily-canary-check`: `ops/runners/managed_runtime.py:1341`, `:1342`, `:1352`, `:1405`; `engine/scripts/staging/run_42_semantic_canaries.py:48`, `:456`, `:457` |

Notes that change what an alert means:

* **Absence windows.** A Cloud Monitoring metric absence condition cannot exceed 23.5 hours, and alerting reads only the most recent 25 hours of a user log based metric. So each absence policy is a PromQL condition, `absent_over_time(logging_googleapis_com:user_<metric>{monitored_resource="cloud_run_job"}[24h])`, held for 6h (30h in total) or 24h (48h in total), evaluated every 300s. Unlike a metric absence condition, it also fires when the metric has never had data. The daily job still ships in `verify-runtime` mode and both schedulers are `PAUSED` (`infra/runtime/daily-staging.json`, `infra/runtime/scheduler-staging.json`), so enabled absence policies would email the owner every day while nothing is scheduled. The three absence policies (`daily-success-absent-30h`, `daily-success-absent-48h`, `price-renewal-success-absent-30h`) are therefore declared with `"enabled": false`. They are created and read back (readback compares `enabled` too), but they do not notify. All other policies are enabled. The D04 and C04 activation brief flips those three to `"enabled": true` in `definitions.json` and reapplies: dry run, then `--apply` with the new `plan_sha256`, then `--readback`.
* **Stale and expired are not told apart in the service.** The texts `pricing review is stale` and `question policy is expired` never reach a log line. The service turns every admission policy failure, including a future dated review and a cost above the reservation, into `approval_required` at stage `policy_freshness`. The renewal job does keep them apart, as `observation_stale` and `policy_expired`. Splitting them in the service would take a new field in the engine admission event. That change is out of scope here, so none was added.
* **No success line was added.** The daily and renewal success lines already exist.
* **Canary.** The alarm line exists only on `feat/42-l04-daily-canary-check` (commit `17ea74c`). Until that branch merges, the source test reads those lines from that commit with `git show` and fails if the commit is missing.

## Native brief step

Identity: the owner account `jhb.analytics@gmail.com`, through Application Default Credentials on the owner machine. It needs `roles/logging.configWriter`, plus `roles/monitoring.alertPolicyEditor` and `roles/monitoring.notificationChannelEditor` (or `roles/monitoring.editor`). The owner holds `roles/owner` on the project, which includes all of them, so no IAM change is needed. The native run needs `google-auth` and `requests`, both already in `engine/requirements.lock`. It needs no Google Cloud monitoring client library and no command line tool.

Run from the repository root:

1. Dry run (reads only, prints planned `create`, `update` or `no-op` per resource and `plan_sha256`):

   ```
   python -m ops.monitoring.apply_monitoring
   ```

   `python -m ops.monitoring.apply_monitoring --offline` prints the same plan and digest with no API call.

2. Apply (owned resources only, refuses unless the digest is the one the dry run printed):

   ```
   python -m ops.monitoring.apply_monitoring --apply --plan-sha256 <plan_sha256 from step 1>
   ```

   The first apply creates 15 resources, in order: the channel, the seven metrics, then the seven policies. A second apply reports `no-op` for all 15. A policy create can fail right after its metric is created, because the new metric can take a few minutes to appear in Cloud Monitoring. If that happens, run the same command again.

3. Readback:

   ```
   python -m ops.monitoring.apply_monitoring --readback
   ```

   Expected output, exit code 0 (names are the project's own):

   ```
   {
     "channels": {"owned_count": 1, "recipients": ["jhb.analytics@gmail.com"], "status": "pass"},
     "mode": "readback",
     "plan_sha256": "<same digest as step 1>",
     "project": "ogilvy-trends-v2",
     "resources": [
       {"key": "owner_email", "kind": "notification_channel", "name": "projects/ogilvy-trends-v2/notificationChannels/<id>", "status": "pass"},
       {"key": "intelligence_42_daily_success", "kind": "log_metric", "name": "intelligence_42_daily_success", "status": "pass"},
       ... one row per metric and per policy, 15 rows in all, each "status": "pass" ...
     ],
     "status": "pass"
   }
   ```

   Any row with `"status": "fail"` names the drifted fields under `differences`, or carries `"missing": true`. The command then exits 1. Refusals print `{"status": "refused", "error": ...}` and exit 2. Any other error (for example missing credentials or a network failure) prints one line, `{"status": "failed", "error": "<exception class>"}`, and exits 2. Apply patches only the top level fields that differ. If the channel type or email differs, it refuses with `channel_identity_drift:owner_email` instead of patching.
