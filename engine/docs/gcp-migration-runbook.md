# GCP migration runbook: move the deploy off GitHub Actions onto Cloud Build

This runbook is the supervised, human-run half of the deploy migration. The
code half (`cloudbuild.yaml` at the repo root) ships in the same PR. Nothing
here runs automatically. A person runs each command, reads the output, and only
then runs the next one.

The goal: a GitHub billing failure must never be able to block a deploy again.
Today the deploy lives in `.github/workflows/deploy-cron-image.yml`, which needs
GitHub Actions runtime. When GitHub Actions billing failed in the past, the
deploy could not run and the live Cloud Run job drifted onto a stale image.
Cloud Build runs inside GCP and does not need GitHub Actions runtime, so the
deploy survives a GitHub billing outage.

Two workstreams:

- Workstream A: the Cloud Build deploy (this replaces the deploy workflow).
- Workstream B: a second Cloud Scheduler fallback that runs the pipeline job
  directly, so the daily run no longer depends on the GitHub Actions
  `daily-trends.yml` schedule either.

Constants used throughout:

- Project: `ogilvy-trends-v2`
- Region: `us-central1`
- GitHub repo: `jhbanalytics-pixel/trends-engine-v2`
- Artifact Registry image: `us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine:<sha>`
- Cloud Run job: `trends-engine-pipeline`
- Cron runtime SA: `trends-cron-trigger@ogilvy-trends-v2.iam.gserviceaccount.com`

Set the project once before anything else so every command below lands in the
right place:

```bash
gcloud config set project ogilvy-trends-v2
```

## Workstream A: Cloud Build deploy

### A0. Confirm the Cloud Build service account

2nd-gen Cloud Build triggers run as a service account you specify. If you do not
specify one, the trigger runs as the legacy Cloud Build SA
`<project-number>@cloudbuild.gserviceaccount.com`, which Google is retiring. The
clean path is a dedicated SA that holds only the three roles this deploy needs.

Get the project number (used in some SA names):

```bash
gcloud projects describe ogilvy-trends-v2 --format='value(projectNumber)'
```

Create a dedicated build SA:

```bash
gcloud iam service-accounts create cloudbuild-deployer \
  --project=ogilvy-trends-v2 \
  --display-name="Cloud Build deployer for trends-engine cron image"
```

That gives you:

```
cloudbuild-deployer@ogilvy-trends-v2.iam.gserviceaccount.com
```

This is the Cloud Build SA referenced for the rest of Workstream A. The trigger
created in step A3 pins `--service-account` to it, so the build never runs as
the broad default Cloud Build SA.

### A1. Grant the build SA the three roles the deploy needs

These mirror exactly the roles the GitHub deploy SA held
(`roles/artifactregistry.writer`, `roles/run.developer`,
`roles/iam.serviceAccountUser`). Nothing wider.

Push the image to Artifact Registry:

```bash
gcloud projects add-iam-policy-binding ogilvy-trends-v2 \
  --member="serviceAccount:cloudbuild-deployer@ogilvy-trends-v2.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

Update the Cloud Run job:

```bash
gcloud projects add-iam-policy-binding ogilvy-trends-v2 \
  --member="serviceAccount:cloudbuild-deployer@ogilvy-trends-v2.iam.gserviceaccount.com" \
  --role="roles/run.developer"
```

Act as the cron runtime SA so it can deploy a job that runs as that SA. This is
a binding on the runtime SA itself, not a project-level binding:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  trends-cron-trigger@ogilvy-trends-v2.iam.gserviceaccount.com \
  --project=ogilvy-trends-v2 \
  --member="serviceAccount:cloudbuild-deployer@ogilvy-trends-v2.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

The build SA also needs to write its own build logs. With
`options.logging: CLOUD_LOGGING_ONLY` set in cloudbuild.yaml (it is), grant the
log writer role so the build can stream logs:

```bash
gcloud projects add-iam-policy-binding ogilvy-trends-v2 \
  --member="serviceAccount:cloudbuild-deployer@ogilvy-trends-v2.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter"
```

### A2. Connect the GitHub repo to Cloud Build (2nd gen)

This is a one-time link between the GitHub org/repo and Cloud Build. It uses the
2nd-gen Cloud Build GitHub connection plus a repository link. The connection
create step prints an OAuth/installation URL you open in a browser and approve
against the `jhbanalytics-pixel` org; that is the one interactive moment in this
runbook.

Enable the APIs the connection needs:

```bash
gcloud services enable \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  --project=ogilvy-trends-v2
```

Create the GitHub connection:

```bash
gcloud builds connections create github trends-engine-gh \
  --project=ogilvy-trends-v2 \
  --region=us-central1
```

Read back the connection. While the GitHub App install is still pending it
prints an `actionUri`. Open that URL, install/authorize the Cloud Build GitHub
App on the `jhbanalytics-pixel` org, then re-run the describe until it shows
`installationState: COMPLETE`:

```bash
gcloud builds connections describe trends-engine-gh \
  --project=ogilvy-trends-v2 \
  --region=us-central1
```

Link the repository:

```bash
gcloud builds repositories create trends-engine-v2 \
  --connection=trends-engine-gh \
  --remote-uri=https://github.com/jhbanalytics-pixel/trends-engine-v2.git \
  --project=ogilvy-trends-v2 \
  --region=us-central1
```

### A3. Create the push trigger on master

Same path filter the current deploy workflow uses, plus `cloudbuild.yaml` so a
change to the build config also rebuilds. Branch pattern `^master$` mirrors the
workflow's `branches: [master]`. The trigger points at `cloudbuild.yaml` and
pins the dedicated build SA from A0.

```bash
gcloud builds triggers create github trends-engine-deploy \
  --project=ogilvy-trends-v2 \
  --region=us-central1 \
  --repository="projects/ogilvy-trends-v2/locations/us-central1/connections/trends-engine-gh/repositories/trends-engine-v2" \
  --branch-pattern="^master$" \
  --build-config="cloudbuild.yaml" \
  --service-account="projects/ogilvy-trends-v2/serviceAccounts/cloudbuild-deployer@ogilvy-trends-v2.iam.gserviceaccount.com" \
  --included-files="src/**,scripts/**,configs/**,infra/**,Dockerfile,pyproject.toml,requirements.txt,cloudbuild.yaml"
```

The current workflow path filter is `src/**, scripts/**, configs/**, infra/**,
pyproject.toml, requirements.txt, Dockerfile` plus the workflow file itself. The
trigger swaps the workflow file for `cloudbuild.yaml`, which is the equivalent
self-rebuild trigger.

### A4. Test with a no-op push

Push a trivial commit to master (a whitespace change to a README, or an empty
commit) and watch the build:

```bash
gcloud builds list \
  --project=ogilvy-trends-v2 \
  --region=us-central1 \
  --limit=5
```

Tail the running build by id:

```bash
gcloud builds log <BUILD_ID> \
  --project=ogilvy-trends-v2 \
  --region=us-central1 \
  --stream
```

Confirm the job image updated to the new SHA:

```bash
gcloud run jobs describe trends-engine-pipeline \
  --project=ogilvy-trends-v2 \
  --region=us-central1 \
  --format="value(template.template.containers[0].image)"
```

The tag on the end of that image path must equal the 7-char SHA of the commit
you just pushed.

### A5. Cutover

Run Cloud Build and the GitHub deploy workflow in parallel for a short window
(see the parallel-cutover order below). Once a Cloud Build deploy has run green
and updated the job image at least once, make Cloud Build the only deploy path.

Edit `.github/workflows/deploy-cron-image.yml` and comment out (or delete) the
`on: push` block so the workflow no longer fires on a master push:

```yaml
on:
  # Deploy moved to Cloud Build (cloudbuild.yaml). Disabled here so there is one
  # deploy path. Re-enable to roll back.
  # push:
  #   branches: [master]
  #   paths:
  #     - "src/**"
  #     - "scripts/**"
  #     - "configs/**"
  #     - "infra/**"
  #     - "pyproject.toml"
  #     - "requirements.txt"
  #     - "Dockerfile"
  #     - ".github/workflows/deploy-cron-image.yml"
  workflow_dispatch:
```

Keep `workflow_dispatch` so the old path is still runnable by hand during the
rollback window.

### A6. Rollback

Re-enable the `on: push` block in `.github/workflows/deploy-cron-image.yml`
(uncomment what A5 commented). The workflow resumes deploying on the next master
push. To stop Cloud Build at the same time, disable its trigger:

```bash
gcloud builds triggers update trends-engine-deploy \
  --project=ogilvy-trends-v2 \
  --region=us-central1 \
  --disabled
```

Re-enable later with `--no-disabled`.

## Workstream B: second Cloud Scheduler fallback

Today the daily run has a Cloud Run primary (Cloud Scheduler at 00:30 UTC) plus
the GitHub Actions `daily-trends.yml` fallback (four cron fires between 02:15 and
03:30 UTC). Workstream B replaces that GitHub fallback with a second Cloud
Scheduler job at 02:30 UTC that runs the same Cloud Run job directly. The
pipeline's own idempotency guard (`scripts/check_already_ran_today.py`, plus the
per-market success check in `run_rss_now.py`) makes a redundant fire a no-op, so
a second scheduler firing after a successful primary just exits clean.

### B1. Create the 02:30 UTC fallback scheduler job

Two equivalent ways to run the Cloud Run job on a schedule.

Preferred: the native Cloud Run jobs scheduler integration. It runs
`jobs:run` for you and needs no hand-built HTTP target:

```bash
gcloud scheduler jobs create http trends-engine-fallback-0230 \
  --project=ogilvy-trends-v2 \
  --location=us-central1 \
  --schedule="30 2 * * *" \
  --time-zone="Etc/UTC" \
  --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/ogilvy-trends-v2/jobs/trends-engine-pipeline:run" \
  --http-method=POST \
  --oauth-service-account-email="trends-cron-trigger@ogilvy-trends-v2.iam.gserviceaccount.com"
```

The `trends-cron-trigger` SA must hold `roles/run.invoker` on the job (it
already does for the 00:30 primary). The job timeout is 5400s; the primary at
00:30 can still be in flight near 02:00, so 02:30 keeps this fallback clear of
the primary's worst-case finish, exactly the reasoning the GitHub fallback used
for its 02:15 earliest fire.

### B2. Verify the fallback fires and self-guards

Force one run by hand and confirm it exits clean via the guard if the primary
already wrote today's success rows:

```bash
gcloud scheduler jobs run trends-engine-fallback-0230 \
  --project=ogilvy-trends-v2 \
  --location=us-central1
```

Then read the job's recent executions and confirm the latest exited 0 (a guarded
no-op completes in well under a minute):

```bash
gcloud run jobs executions list \
  --job=trends-engine-pipeline \
  --project=ogilvy-trends-v2 \
  --region=us-central1 \
  --limit=5
```

### B3. Remove the GitHub Actions daily-trends.yml schedule

Only after the 02:30 Cloud Scheduler fallback has been observed firing and
self-guarding cleanly for a few days, drop the schedule from
`.github/workflows/daily-trends.yml`. Comment out the `schedule:` block (keep the
file and its `workflow_dispatch` so a manual fire is still possible):

```yaml
on:
  # Daily schedule moved to Cloud Scheduler (trends-engine-fallback-0230).
  # Disabled here so the only scheduled fallback is the Cloud Run one.
  # schedule:
  #   - cron: '15 2 * * *'
  #   - cron: '30 2 * * *'
  #   - cron: '0 3 * * *'
  #   - cron: '30 3 * * *'
  workflow_dispatch:
    # inputs unchanged
```

Do not remove the schedule before the Cloud Scheduler fallback has a clean
observation window. Removing it early would leave a single fallback layer during
the cutover.

## Cost and IAM-risk note

Cost. Cloud Build's first 120 build-minutes per day are free on the default
machine type; this deploy is one build per master push to the watched paths,
typically a few minutes each, so it stays inside the free tier on normal commit
volume. The second Cloud Scheduler job is three free jobs per account, so it is
free. Artifact Registry storage for the image tags is a few cents per month.
Net: effectively zero new spend, and it removes the GitHub Actions minutes the
old deploy + fallback consumed.

IAM risk. The dedicated `cloudbuild-deployer` SA holds exactly three deploy
roles plus log-writer, the same surface the GitHub deploy SA held, and nothing
broader. Pinning the trigger to this SA (A3) keeps the build off the legacy
default Cloud Build SA, which carries the wide `roles/cloudbuild.builds.builder`
surface. The `roles/iam.serviceAccountUser` binding is scoped to the single
`trends-cron-trigger` runtime SA, not project-wide, so the build SA can act as
that one cron SA and no other. No service account key file exists anywhere in
this path; the build authenticates via its attached SA through ADC, which
removes the long-lived `GCP_SA_KEY` secret the GitHub workflow depended on.

## Parallel-cutover order

Run the two deploy paths side by side, then retire the old one only after the
new one is proven. Order:

1. Ship this PR (cloudbuild.yaml + this runbook). No GCP change yet.
2. Workstream A steps A0 to A3: create the build SA, grant the three roles,
   connect the repo, create the trigger. The GitHub deploy workflow still runs;
   now both fire on a master push.
3. A4: push a no-op, watch both deploy paths land the same image SHA on the job.
   This is the parallel window; both paths produce an identical result.
4. A5: once a Cloud Build deploy is green and has updated the job image,
   comment out `on: push` in deploy-cron-image.yml. Cloud Build is now the only
   deploy path. Keep `workflow_dispatch` for rollback.
5. Workstream B in parallel: B1 creates the 02:30 scheduler fallback while the
   GitHub `daily-trends.yml` schedule still runs. Both fallbacks fire; the
   idempotency guard makes the second a no-op.
6. B3: after a clean observation window, comment out the `daily-trends.yml`
   schedule. Cloud Scheduler is now the only scheduled fallback.

Rollback at any point is the reverse: re-enable the GitHub `on: push` (A6) and
disable the Cloud Build trigger, and re-enable the `daily-trends.yml` schedule.
The old GitHub paths stay in the repo, only disabled, through the whole cutover
so rollback is a one-line uncomment, not a rebuild.
