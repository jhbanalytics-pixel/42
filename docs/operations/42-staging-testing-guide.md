# 42 staging testing guide

This is the operator handoff guide for 42 staging. It is written for one person: the programme
owner, working on a Windows machine with Git Bash and the Google Cloud SDK Shell, gcloud
installed, the owner account signed in and an application default credential in place.

It answers one question: how do I test 42 end to end.

Read it one step at a time. Every step says what it is for, what to run, what success looks
like, and what to do when it does not succeed. Where a step runs somewhere other than your
machine, the step says so, because that difference decides where you look when it fails.

Nothing in this guide is aspirational. Where a step cannot be performed today, it says so in
place rather than being left out, and it says what is blocking it. Section 12 collects every
one of those statements so you can see the whole shape at once.

First written on 18 September 2026. Corrected on 25 September 2026 against the repository at
`integration/core-20260923`, commit `ac2bbd8`.

### Values that are set at release

Several facts in this guide change every time a new candidate is released, and some change
again when the pricing review is renewed. This guide does not write them down, because a
written value goes stale at the next release. Wherever one of them matters, the guide says
**set at release** and tells you where to read the live value.

| Value | Where you read the live value |
|---|---|
| Ask window | The covered window line under the Ask page title, which reads it from the server at `/api/chat/coverage`. |
| Briefing run date | The Briefing header, under Completed run date, beside the Closed window it observed. |
| Revision | The handover for the candidate. As an operator, the release `verify` receipt, or the traffic block of the `listening-post-staging` Cloud Run service. |
| Deployment digest | The release `verify` receipt, or the binding the ledger names as active. |
| Release age | The days between the run close of the released Briefing run and the day you test. |
| Pricing lapse time | Twenty four hours after the `pricing.verified_at` of the policy the serving revision carries, which the latest renewal receipt records. |
| Desk freshness | The checked age in the app's utility strip, which counts from the desk's last refresh. |

The build lead records a release of core `1a6d63a` to staging on 23 September 2026 at 09:52
UTC, as revision `listening-post-staging-q-20260923-1a6d63a`, a release at `b24e27c` on 24
September, and a pricing renewal revision, `listening-post-staging-p-33882f388c`, serving on 25
September. Those are the build lead's records, not readings this guide has taken. The next
release is planned to switch Ask to read this week's captured data, so every value in the table
above changes at that release.

---

## Contents

1. What you can and cannot run today
2. What staging is, and what is in it
3. The two accounts, and which one each step uses
4. Where each step runs
5. Set your machine up once
6. Journey 1: publish the app image
7. Journey 2: release the candidate, and roll it back
8. Journey 3: the daily cycle
9. Journey 4: open the product and walk the capability journeys
10. Telling a real failure from a known open item
11. Where the evidence of each run lands
12. Everything that is not recorded as run

---

## 1. What you can and cannot run today

If you have one hour and you want to see 42 working, go straight to section 9. That is the
journey you can run end to end without any credential beyond the access key, against whichever
revision is serving (set at release).

| Journey | Can you run it today | Where it runs | What is in the way |
|---|---|---|---|
| Set your machine up (section 5) | Yes | Your machine | Nothing |
| Publish the app image (section 6) | Yes | Cloud Build | Nothing recorded. The build lead's release records imply the build has completed for the released revisions. |
| Release a new candidate (section 7) | Yes | Your machine, driving Google APIs | Nothing recorded. The build lead records releases on 23 and 24 September. |
| Renew the pricing review (section 7) | Yes, attended | Your machine, driving Google APIs | The managed renewal job cannot renew until the amendment e grants are applied. |
| Roll back (section 7) | Not recorded as run | Your machine, driving Google APIs | No rollback drill is recorded. |
| Run the daily cycle (section 8) | No | Cloud Scheduler and Cloud Run | The repository records no created job, scheduler or verified runtime. |
| Open the product and walk the journeys (section 9) | Yes | Your browser | Nothing. Two of the eleven journeys are not implemented and say so. |
| Run the local test suites (section 5) | Yes | Your machine | Nothing |

---

## 2. What staging is, and what is in it

### The project

Everything 42 staging uses lives in the Google Cloud project `ogilvy-trends-v2`, region
`us-central1`.

Staging is not a copy of production. It is a named set of resources inside the same project,
each one listed in one reviewed file, `ops/deploy/resource_manifest.json`. Every operations
tool loads that file, checks its SHA256 against the approved digest
`33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09`, and then refuses to touch
any resource that is not an exact full name in it. It does not match prefixes, suffixes,
project aliases or staging looking names. That guard is why a mistyped resource name fails
closed instead of reaching production.

### What native discovery found

`ops/deploy/iam_delta_v1.json` classifies every manifest resource by the native discovery
recorded in `docs/operations/iam-delta.md`: 17 found present and 59 found absent or never
probed. Those present at discovery include these.

| Resource | Name |
|---|---|
| Cloud Run service | `listening-post-staging` |
| Cloud Tasks queue | `oi-general-question-staging` |
| BigQuery datasets | `trends_v2_staging`, `trends_v2_staging_approvals`, `trends_v2_staging_qa`, `trends_v2_staging_funded` |
| Storage bucket | `listening-post-staging-cache` |
| Secrets | `ui-passcode-staging` version 2, `SOCIALCRAWL_OGILVY_API_KEY` version 1 |
| Service account | `listening-post-staging` |
| Cloud Build connection | `tev2-gh` |

The absent list is a snapshot and much of it is now out of date. The connected repository, the
`intelligence-42` image registry, the build and deploy accounts and the release path are
recorded by the build lead as used since, in the releases of 23 and 24 September, and amendment d created
routines and grants on 23 September. The discovery has not been retaken, so read the absent
list as the state at discovery, not as a live reading. Ask the build lead which rows have since
been created before you rely on one being absent.

### The permission delta

`ops/deploy/iam_delta_v1.json` holds the role bindings the staging identities need, in
amendments a to e. `docs/operations/iam-delta.md` records that amendment d is live: Albert
applied it on 23 September 2026 at 12:01 UTC, and its receipts are on Albert's machine.
Amendment e adds rows on top of amendment d. The file's `approval` block records
`state: approved`, approved on 24 September 2026, and `applied: false`, and the status section
of `docs/operations/iam-delta.md` says no amendment e row has been granted or read back yet.
That prose still calls amendment e proposed because it predates the 24 September approval
recorded in `ops/deploy/iam_delta_v1.json`. So
a permission refusal on an amendment e row is expected until it is applied.

### What is in the product

The staging service serves the general question app. What it can do is inventoried in
`app/docs/capability-journeys.json`: eleven capabilities, each with the job it must do, the
route that opens it, the producer behind it and the proof panel that shows where the reading
came from. Nine of the eleven have an implemented producer. Two do not, and they say so on
screen rather than hiding it. Section 9 walks all of them.

### Data freshness

Every date about the data is set at release: the Ask window, the Briefing run and its close,
the desk refresh date and so the desk freshness, and the revision and deployment digest you are
testing. Read them where the table at the top of this guide says. `app/docs/staging-vetting.md`
describes each one for a tester.

Read the release age as the number of days between the run close and the day you test. Until
the release that moves Ask onto this week's captured data, the corpus is the disclosed retained
corpus. Testing against it is early vetting. It is not fresh acceptance and it is not semantic
acceptance.

---

## 3. The two accounts, and which one each step uses

You have two accounts and they are not interchangeable.

**The owner account, `jhb.analytics@gmail.com`.** This is your default. It signs in to gcloud,
it holds the application default credential, and it runs every step in this guide except the
two named below. Build submissions run under it. The release, rollback and renewal commands
run under it.

**The Ogilvy approver, `albert.meintjes@ogilvy.co.za`.** Used only with an explicit account
flag, never as your default, and only for two things:

1. The grant approval in `ops/deploy/iam_delta_v1.json`. It is recorded, dated 24 September
   2026, and covers amendments a to e, so there is nothing for you to approve here today.
2. The execution generation rotation cutover. `docs/operations/resources.md` states that the
   v1 disable is irreversible and that the disable is issued only after a `pre-disable`
   preflight has reached `ready_for_disable` under the Ogilvy actor. The preflight opens a
   client under the caller credentials and records `SESSION_USER()` in the same process, and
   writes it into the receipt as `session_user`. So the shell that runs it must have the
   approver as its active account, and the way you confirm you got that right is to read
   `session_user` back out of the receipt. That procedure is documented in
   `docs/operations/resources.md` under "Execution generation cutover" and is not repeated
   here. It is a separate piece of work from testing the product.

One thing about that cutover: it takes an `--activation-phrase-file` option naming a file that
is handed to you separately. Never print that file, never paste its contents into a terminal
or a report, and never commit it.

**The identities that are not you.** The build runs as the service account
`intelligence-42-build`. The release deploys as `intelligence-42-deploy`. The daily job runs
as `intelligence-42-orchestration`, and it obtains that identity only through Application
Default Credentials from the Cloud Run metadata server. `ops/runners/managed_runtime.py`
refuses a service account key file, a refresh token file, a user OAuth credential and the
environment variables `GOOGLE_APPLICATION_CREDENTIALS`, `CLOUDSDK_CONFIG`,
`CLOUDSDK_AUTH_ACCESS_TOKEN` and `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE`, before discovery
even runs. Your local credential can never end up inside that container, by design.

### Where the app passcode lives

The app is behind an access gate. The passcode is a Secret Manager secret in the project,
`ui-passcode-staging`. The service reads it as the environment entry `UI_PASSCODE`, by secret
name and the version alias `latest`, never as a literal. Every release receipt records that
entry only as `source: secret`. `test_receipts_never_carry_plain_secret_values` proves no
receipt ever carries the value.

Do not copy the passcode into a finding, a screenshot, a chat message or a file. If you need
it, read it from Secret Manager at the time you need it.

---

## 4. Where each step runs

This split matters more than anything else in the guide, because it decides where you look
when something fails.

| Step | Runs on | If it fails, look at |
|---|---|---|
| Checkout, environments, test suites | Your machine | Your terminal output and the exit status |
| Local image builds (section 5) | Your machine, your Docker daemon | Your terminal output |
| `gcloud builds submit` | Submitted from your machine, executed on a Cloud Build worker | Cloud Logging. The build writes no logs bucket and no artifacts. |
| `ops/deploy/release.py` | Your machine, calling Google APIs over HTTPS | The receipt file the command wrote, first. It carries the refusal code. |
| `ops/deploy/refresh_question_policy.py` | Your machine, calling Google APIs over HTTPS | The receipt file the command wrote. |
| The daily cycle | Cloud Scheduler triggers a Cloud Run job | Cloud Logging and the job execution record |
| Opening the product | Your browser, against the Cloud Run service | The proof panel on the route, then the reply's request and usage details |

The important consequence: when a build fails, nothing on your machine has the answer. The
build ran on a Google worker with its own network and its own Docker daemon. The only thing
your machine holds is the revision you submitted.

---

## 5. Set your machine up once

**What this is for.** Everything else assumes a clean checkout of the exact commit you intend
to test, two Python environments, and the frontend dependencies. The source for this section
is `docs/operations/bootstrap.md`.

A note on the command examples. The operations documents write their examples in PowerShell.
The commands below are shown without shell variables so they work as written from the
repository root in Git Bash, where forward slashes are fine. Where a value is yours to
substitute, it is written in angle brackets.

### Step 1. Get a clean checkout at the commit you are testing

Run from a directory of your choosing.

```
git clone <repository url> 42-test
cd 42-test
git checkout <sha>
git rev-parse HEAD
```

**Success looks like:** `git rev-parse HEAD` prints the same forty character revision you
checked out, and `git status` reports a clean tree.

**If it does not:** do not continue with a dirty tree. The reviewed context builder refuses a
working tree that `git status` reports as dirty, so a dirty tree fails later and further away.

Keep build contexts, receipts and retained request artifacts outside this checkout.

### Step 2. Create the two Python environments

**What this is for.** The engine and the app keep separate dependency sets so their imports
cannot mix. You need CPython 3.13, Git, Bun and a Docker daemon that builds Linux amd64 images.

```
py -3.13 -m venv .venv-engine
py -3.13 -m venv .venv-app
.venv-engine/Scripts/python.exe -m pip install --require-hashes -r engine/requirements-dev-windows.lock
.venv-app/Scripts/python.exe -m pip install --require-hashes -r app/requirements-dev.lock
.venv-engine/Scripts/python.exe -m pip check
.venv-app/Scripts/python.exe -m pip check
```

**Success looks like:** both installs finish and both `pip check` calls report no broken
requirements.

**If it does not:** a hash mismatch means the lock file and the downloaded distribution
disagree. Do not regenerate the locks. That is a change to the candidate, not a fix to your
machine.

### Step 3. Install the frontend dependencies

**What this is for.** The backend route checks import the frontend router, so this is needed
even if you only intend to run the Python suites.

```
cd app/frontend
bun install --frozen-lockfile
cd ../..
```

**Success looks like:** bun installs from the committed lockfile without modifying it.

### Step 4. Run the source checks

**What this is for.** These are the suites that must be green before a candidate is worth
building. Run each from its own package directory. Preserve the full output and the exit
status.

```
cd engine
../.venv-engine/Scripts/python.exe -m pytest tests/unit -q -ra
cd ../app
../.venv-app/Scripts/python.exe -m pytest tests -q -ra
cd ..
.venv-engine/Scripts/python.exe -m pytest ops/tests -q -ra
cd app/frontend
bun run build
bun test
bun run test:journeys
cd ../..
```

**Build before you test.** `app/frontend/src/ui/__tests__/neutral-contract.test.jsx` reads
`app/web/dist/index.html` and the built assets beside it, and `app/web/dist` is build output
that `app/.gitignore` keeps out of the repository. So on a clean clone the frontend suite fails
until `bun run build` has run. `docs/operations/bootstrap.md` and
`app/.github/workflows/ci.yml` both build first.

The app suite needs two environment entries set before it runs,
`GENERAL_QUESTION_ENGINE_TEST_ROOT` pointing at the `engine` directory and
`GENERAL_QUESTION_ENGINE_TEST_PYTHON` pointing at the engine interpreter.

**Success looks like:** every suite exits zero. `bun run build` writes `app/web/dist`.
`bun run test:journeys` is the browser suite selected by `app/frontend/playwright.config.mjs`;
it runs against the built application.

**If it does not:** a successful frontend compile is not browser proof. A green `bun run build`
with a failing `bun run test:journeys` is a real failure, not a warning.

**Known open item.** The retained answer regression tests take an explicit
`OPEN_INTELLIGENCE_RETAINED_FIXTURES` directory of separately verified historical request and
policy artifacts. If you do not supply it, those tests skip. A skip there is an open
verification obligation, not a pass. Do not substitute generated examples for those bytes.

### Step 5. Build the images locally, once

**What this is for.** It proves the reviewed context builder and both Dockerfiles work on your
machine before you spend a Cloud Build run on them. Generate the review inventory into a fresh
directory outside the checkout, inspect it, then build.

The exact inventory generation snippet and the two build commands are in
`docs/operations/bootstrap.md` under "Reviewed app context". Record the resulting image IDs.

**Success looks like:** the context builder writes a receipt, and both
`docker build` calls succeed.

**If it does not:** the app image build downloads the PDF runtime through
`ops/build/install_pdf_runtime.py`, the same download the Cloud Build run makes. A failure there
names the URL it gave up on; attach it to the build report rather than treating it as a local
fault.

---

## 6. Journey 1: publish the app image

### Where the build stands

The build stopped at step 7, `build-app-image`, as of 18 September 2026, while the image's
build stage downloaded the PDF runtime. At the base this guide was corrected against,
`app/Dockerfile.general-question` runs that download through a wrapper that traces every fetch,
attempts a fetch again after a transport fault up to three times, never attempts again after a
size, digest or redirect refusal, and gives up under a global budget with a readable line. The
build lead records releases on 23 and 24 September. A release needs a successful publish build
of the released commit, so if those records hold, the publish build succeeded for the released
commits; this guide has not read the build receipts. The build ids are not in the repository; read them from the `build` block of the release's activation receipt.

### The submission

**Where it runs.** You submit from your machine. All thirteen steps execute on a Cloud Build
worker.

**Step 1. Confirm your checkout is at the revision you intend to build.**

```
git rev-parse HEAD
```

This matters more than it looks. The build config is read from your local checkout and sent
inline, so the submitting checkout must be at the same revision as the `--revision` you pass.

**Step 2. Submit the build.**

```
gcloud builds submit projects/ogilvy-trends-v2/locations/us-central1/connections/tev2-gh/repositories/jhbanalytics-pixel-42-ogilvy-intelligence --revision=<sha> --substitutions=COMMIT_SHA=<sha> --config=cloudbuild.app.publish.yaml --region=us-central1 --service-account=projects/ogilvy-trends-v2/serviceAccounts/intelligence-42-build@ogilvy-trends-v2.iam.gserviceaccount.com
```

It is submitted from the repository root rather than a subtree, because the reviewed context
needs both the engine and the app trees. The `COMMIT_SHA` substitution is required: a manual
submission leaves the built in `COMMIT_SHA` empty, and the client rejects the resulting image
name before any build is created. Pass the same forty character revision to both flags.

**Success looks like:** every step succeeds and the single image entry is pushed. Cloud Build
pushes nothing until every step has passed.

**If it does not:** the step that failed tells you what was not proven. Use the table below.

**Step 3. Record the three values the finished build produces.** They feed the release plan
under exact names and you will need them in section 7.

| Value | Where it comes from |
|---|---|
| Build resource | the build's `name`, `projects/ogilvy-trends-v2/locations/us-central1/builds/<id>` |
| Revision | `source.connectedRepository.revision`, the forty character revision |
| Image digest | `results.images[0].digest` |

The app image reference is the image name joined to that digest:
`us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/listening-post-question@sha256:<digest>`.

You also record `gate_sha256` by hand. It is the SHA256 of
`ops/tests/fixtures/linux_boundary/manifest.json`, the literal the gate step carries as its
last argument in `cloudbuild.app.publish.yaml`. Read it from that file at the revision you
built, because the manifest is repinned whenever a gated file changes. It is a value you
record, not one the release controller derives.

### What each build step proves

| # | Step | What it proves |
|---|---|---|
| 0 | `build-toolchain` | Builds the test toolchain stage of `engine/Dockerfile.tests`, carrying python 3.13, git 2.47.3 and node v24.18.1 from that image's pinned manifests. |
| 1 | `build-context-builder` | Builds `ops/build/Dockerfile.context-builder`, adding bun 1.4.0 from `ops/build/bun_runtime.json` through `ops/build/install_bun_runtime.py`, which refuses on any size or SHA256 mismatch. The manifest's own digest is pinned in the Dockerfile. |
| 2 | `prepare-context-builder-outputs` | Creates the directories the later steps write and refuses any of them or their parents that is a symlink or is present but not a directory. |
| 3 | `linux-gate-prebuild` | Runs the boundary manifest's prebuild node from the checkout. Fails closed when the boundary manifest is stale. |
| 4 | `frontend-install` | `bun install --frozen-lockfile` under `app/frontend`. |
| 5 | `frontend-build` | `bun run build`, producing `app/web/dist`. |
| 6 | `build-context` | Generates the review inventory for both trees with `allow_reviewed_working_tree` false, runs `app/scripts/build_general_question_context.py`, then checks the receipt: the app commit must equal `COMMIT_SHA`. |
| 7 | `build-app-image` | Builds `Dockerfile.general-question` from the reviewed context, including the traced PDF runtime download. |
| 8 | `context-inventory-inside-image` | The packaged `/app/runtime-build.json` must carry the receipt's `lp_commit` and `engine_bundle_digest`, and the SHA256 of every file under `/app/src` and `/app/configs` must reproduce the reviewed inventory's digest. |
| 9 | `pdf-seccomp-profile-check` | Checks the checked in `ops/tests/fixtures/pdf/seccomp-profile.json` against the digest the step carries, which the boundary manifest records as `pdf_security_profile.sha256`. |
| 10 | `pdf-runtime-smoke` | Runs the built image as uid 10001 with no network, a read only root, a tmpfs at `/tmp`, all capabilities dropped, no new privileges and that seccomp profile, and renders an inline document using the packaged Newsreader font. It exits non zero on a missing browser, a missing font or any export error. |
| 11 | `build-linux-gates` | Builds `ops/tests/Dockerfile.linux-gates` on top of the exact app image just built. |
| 12 | `verify_linux_boundary_tests` | Checks `ops/tests/fixtures/linux_boundary/manifest.json` against the SHA256 given as the step's last argument, then runs the manifest's boundary, combined and engine observer node sets against the gates image with no network, as uid 10001, with no new privileges. |

`docs/operations/release.md` under "Publishing the app image" is the full description of each
step.

### The engine image

There is a second publish build, `engine/cloudbuild.publish.yaml`, which puts the engine image
into the same registry. It is a separate submission with its own command, documented in
`docs/operations/release.md` under "Publishing the engine image". The release index names both
images, so a release that binds a release index needs the engine build receipt as well.

---

## 7. Journey 2: release the candidate, and roll it back

**Status.** The build lead records native releases to staging on 23 and 24 September 2026 and
a pricing renewal serving on 25 September. `docs/operations/release.md` still lists the native
release and the rollback drill as unproven under "Proof status", because it was written before
those runs and the receipts are kept outside the repository. No rollback drill is recorded.
Everything below is also covered by local tests: `ops/tests/test_foundation_release.py` against
a fake adapter, and `ops/tests/test_release_native_adapter.py`, which drives the real native
adapter against one injected HTTP transport.

**Where it runs.** On your machine. The controller itself runs no Git, gcloud, HTTP or SDK
call; every native client is supplied through an adapter named on the command line. For a
native run that adapter is `ops/deploy/release_native_adapter.py:factory`, with the state
`owner-gcloud`, or `adc` to use the application default credential. The adapter drives Cloud
Run v2, Cloud Tasks v2 and the storage JSON API over REST, records every request and response
with the bearer token never written, and follows no redirect at all.

Two rules apply to every command in this section:

- **Receipts are write once.** Every command, including `plan`, refuses an existing output path
  with `output_exists` before it makes any native read. Choose a fresh path each time.
- **A refusal is one JSON line.** It prints `state`, `error` and `output` and exits 1. Only a
  proven result exits 0. Read the `error` value first; it is the name of what was not proven.

Pick a receipts directory outside the checkout. The steps below call it `<receipts>`.

### Step 1. Produce the activation receipt

**What this is for.** The activation binds the build you just made to a named revision, pauses
the queue, and writes the ledger binding. It is the input the release plan verifies.

```
.venv-engine/Scripts/python.exe ops/deploy/activate_question_deployment.py --build <receipts>/build.json --lp-commit <hex40> --engine-commit <hex40> --engine-bundle-digest <hex64> --gate-sha256 <hex64> --policy-digest <hex64> --revision-name listening-post-staging-q-<date>-<commit7> --sdk-version <sdk version> --adapter ops/deploy/release_native_adapter.py:factory --adapter-state owner-gcloud --output <receipts>/activation.json
```

`--dry-run` renders every mutation request and writes nothing. Use it first. The revision name
must start `listening-post-staging-q-`; the released revisions on record follow the form
`listening-post-staging-q-<yyyymmdd>-<commit7>`.

**Success looks like:** the receipt is written and the queue reads back paused and empty.

**If it does not:** the refusals are `queue_not_empty`, `queue_not_paused`, `binding_conflict`,
`ledger_binding_conflict` and `ledger_moved`. None of them is retried. A rerun after a success
reuses the binding object and skips the ledger write, so a rerun is safe.

### Step 2. Plan the release

**What this is for.** `plan` is read only. It verifies the activation receipt in full, loads
the release authority object, captures the immutable before state of the service, queue and
ledger, and writes a plan whose `idempotency_key` is the SHA256 of its own canonical bytes.

```
.venv-engine/Scripts/python.exe ops/deploy/release.py plan --activation <receipts>/activation.json --authority <receipts>/release-authority.json --adapter ops/deploy/release_native_adapter.py:factory --adapter-state owner-gcloud --output <receipts>/release-plan.json
```

**Success looks like:** exit 0 and a plan file naming the expected revision, image, source
commit and the exact changes to make.

**If it does not:** `authority_expired` means the authority window does not contain the adapter
clock, and it refuses before any native read. A plan also refuses unless the queue is paused
and empty and the ledger equals the activation's `control_after`.

**Do not edit the plan.** Any later edit changes the key and `apply` refuses with
`idempotency_key_mismatch`.

### Step 3. Apply

**What this is for.** `apply` deploys the planned revision with no traffic, then routes 100
percent of traffic to it.

```
.venv-engine/Scripts/python.exe ops/deploy/release.py apply --plan <receipts>/release-plan.json --adapter ops/deploy/release_native_adapter.py:factory --adapter-state owner-gcloud --output <receipts>/release-result.json
```

**Success looks like:** the receipt state is `traffic_verified_100_queue_paused`.

**If it does not, read the state, because the three failure shapes need different responses:**

- `before_state_changed`: something moved between plan and apply. Replan.
- a `failed` operation: it stops immediately.
- `unproven_requires_reconciliation`: the operation was submitted but its outcome was not
  proven within the deadline. **Do not run `apply` again.** It will refuse anyway, because the
  receipt exists. The retry path is `verify`, which reconciles the saved operation identity.
  The default deadline is 600 seconds and the default poll interval is 5 seconds; a deadline
  yields `unproven`, never a success and never a resubmission.

### Step 4. Verify

**What this is for.** `verify` rereads the service, revision, queue and ledger, reconciles any
recorded unproven operation by its saved name, and lists every drift item. It is also the
command that binds the release index.

```
.venv-engine/Scripts/python.exe ops/deploy/release.py verify --plan <receipts>/release-plan.json --result <receipts>/release-result.json --release-index <receipts>/release-index.json --adapter ops/deploy/release_native_adapter.py:factory --adapter-state owner-gcloud --output <receipts>/release-verified.json
```

The release index is produced by `ops/deploy/release_index.py`, described in
`docs/operations/release.md` under "Release index".

**Success looks like:** exit 0, no drift items, and `release_index_sha256` recorded. This
receipt is where the revision and deployment digest of the release are recorded (both set at
release).

**If it does not:** any drift yields state `unproven` and exit 1.
`release_index_bytes_unavailable` means a recorded digest has no readable bytes behind it;
`release_index_digest_mismatch` means the bytes are there but hash to something else. Verify
only certifies what it actually read.

### Step 5. Resume the queue

**What this is for.** The queue is paused for the whole release. Resuming it is a separate,
deliberate step with its own preflight.

```
.venv-engine/Scripts/python.exe ops/deploy/release.py plan --kind resume --mode empty --route <receipts>/release-result.json --activation <receipts>/activation.json --authority <receipts>/release-authority.json --adapter ops/deploy/release_native_adapter.py:factory --adapter-state owner-gcloud --output <receipts>/resume-plan.json
.venv-engine/Scripts/python.exe ops/deploy/release.py resume --plan <receipts>/resume-plan.json --adapter ops/deploy/release_native_adapter.py:factory --adapter-state owner-gcloud --output <receipts>/resume-result.json
```

There are three modes, chosen at plan time. Use `empty` when no request is pending; it refuses
any pending request flag with `pending_flags_forbidden`. Use `recovery` or `drain-expired` when
a request is pending; both require `--pending-request`, `--parent-request` and
`--anchor-request` together or they refuse with `pending_request_required`.

**Success looks like:** state `running_verified` and exit 0.

**If it does not:** anything other than success is `resume_unproven`. The common early refusal
is `drain_not_elapsed`: the preflight requires at least 125 seconds since the route finished.
Wait, then rerun the plan with a fresh output path.

### Renewing the pricing review

**What this is for.** Ask refuses every question once the pricing review of the serving policy
has lapsed, twenty four hours after its `pricing.verified_at`. The lapse time is therefore set
at release and moves again with every renewal. A renewal deploys a new revision named
`listening-post-staging-p-<first ten characters of the policy digest>`, so the serving revision
and deployment digest change with it.

The renewal command is `python -m ops.deploy.refresh_question_policy` with the forms `observe`,
`renew` and, for the managed job, `renew-unattended`. The exact commands, the grant they need
and every refusal are in `docs/operations/release.md` under "Pricing renewal". Run the dry
run form of `renew` before the real one. The managed job `intelligence-42-price-policy-staging`
cannot renew until the amendment e grants for its objects are applied.

**Success looks like:** the renewal receipt names the new policy and revision, and a question
asked on Ask afterwards is admitted.

### Rolling back

**What this is for.** Returning traffic to a previous revision without losing any request,
reservation or ledger row.

```
.venv-engine/Scripts/python.exe ops/deploy/release.py rollback-plan --target-revision <revision name> --authority <receipts>/rollback-authority.json --adapter ops/deploy/release_native_adapter.py:factory --adapter-state owner-gcloud --output <receipts>/rollback-plan.json
.venv-engine/Scripts/python.exe ops/deploy/release.py rollback-apply --plan <receipts>/rollback-plan.json --adapter ops/deploy/release_native_adapter.py:factory --adapter-state owner-gcloud --output <receipts>/rollback-result.json
```

The authority object must carry purpose `rollback`, not `release`.

Four things about rollback that are worth knowing before you need them:

1. **You do not pause the queue by hand.** If the queue is running, `rollback-plan` records an
   explicit pause step as the first change and `rollback-apply` runs it first. If the queue is
   already paused, no pause step is planned and no pause call is made.
2. **It reads the target revision itself, not the service template.** The revision's image,
   deployment digest and source commit must match the stored binding for that digest. A missing
   revision refuses `rollback_target_missing`; a binding that fails the current deployment
   contract refuses `rollback_incompatible_schema`.
3. **It refuses while a request is executing**, with `rollback_in_flight_request`.
4. **Every request, result pointer, reservation and unknown debit is copied byte for byte.**
   The ledger is never replaced by an older snapshot.

**Success looks like:** the receipt state is `rollback_verified_queue_paused`. That receipt is
then the `--route` input for the resume plan, with no `--activation` (passing one refuses
`activation_forbidden`).

**If it does not:** the state to recognise is
`failed_or_unproven_requires_native_reconciliation`. It means a write was issued and its result
was not proven. The queue is left paused and the receipt carries a `forward_recovery_plan` that
names the operation, its readback state, the target revision and the activation generation.
Repeating the same `rollback-apply` refuses with `before_state_changed`, and a fresh
`rollback-plan` for the same target refuses with `rollback_target_already_active`. The repair
is native: keep the queue paused, reconcile the named operation, route the target revision
again and read the traffic back.

After a rollback, resume behaves differently and this catches people out. The 125 second drain
leaves fewer than the 190 seconds `recovery` requires, so `recovery` refuses with
`insufficient_remaining_time`, and `drain-expired` resumes once the request has expired. A task
bound to the newer binding refuses `task_audience_mismatch` in every mode, and the queue stays
paused until that request is handled natively.

### What is proven and what is not, in this section

Proven locally: readback, plan, apply, verify, all three resume paths, the rollback refusals
including the ledger compare and swap and the pause readback, the renewal controller and the
release index validator, against the test double; and the whole lifecycle through the real
native adapter against an injected transport.

Recorded natively by the build lead: the releases of 23 and 24 September and a pricing renewal
serving on 25 September.

Not recorded as run natively: the rollback drill, including the route failure path with a real
traffic readback, and the managed renewal job.

---

## 8. Journey 3: the daily cycle

**Status: cannot be run yet. The repository records no created job or scheduler.**

### What the daily cycle is meant to be

Cloud Scheduler fires a job on a schedule. That job runs on Cloud Run as
`intelligence-42-orchestration`. The scheduler request is a plain
`POST .../jobs/intelligence-42-daily-staging:run` carrying an OAuth token for
`intelligence-42-scheduler` and the body `{}`. No payload, override, argument or environment
value travels in that request; the runtime takes every invocation field from the configuration
baked into the image.

That is deliberate and it is the single most important thing to understand about this journey:
**the scheduler firing is never execution authority.** Every cycle still passes through the
existing durable consume and result validators. A scheduler that fires twice does not get two
runs; a terminal record with known usage is returned as a duplicate trigger without a new
launch.

### Why it cannot run today

1. **No job, scheduler or verified runtime is recorded.** `docs/operations/scheduling.md` under
   "What remains native" says no receipt in the repository records a created scheduler, a
   triggered execution or an observed principal. `infra/runtime/daily-staging.json` and
   `infra/runtime/scheduler-staging.json` hold the proposals, and both schedulers are proposed
   in state `PAUSED`.
2. **The daily job ships in `verify-runtime` mode.** The daily job's configured mode in
   `infra/runtime/daily-staging.json` is `verify-runtime`, not `daily`. Changing it is an edit
   to that digest bound file, and therefore a reviewed deployment change, never a scheduler
   override. Once the mode says `daily`, `run_managed_mode` refuses with
   `daily_wiring_unavailable` when no daily wiring is bound, and with
   `stage_adapter_unavailable` when a bound wiring still carries the unavailable stage seam.
3. **The schedule is a placeholder.** The daily schedule string in the configuration file is a
   placeholder that is replaced with the approved cutoff schedule before activation.

### What you can run today

The contract tests, on your machine, from the repository root:

```
.venv-engine/Scripts/python.exe -m pytest ops/tests/test_foundation_scheduler.py ops/tests/test_runtime_jobs.py ops/tests/test_runtime_native_adapter.py ops/tests/test_runtime_schedulers.py ops/tests/test_runtime_preflight.py -q -p no:cacheprovider
```

**Success looks like:** the suite exits zero. That proves the invocation contract, the refusal
codes, the mode dispatch and the job, scheduler and preflight tools against a test transport.
It does not prove anything ran in the project.

### What has to happen before a cycle can run at all

From `docs/operations/scheduling.md`, the native preflight list, none of which is recorded as
done:

- build the image through the gate
- create the jobs with the configuration digest annotation and the pinned image
- create both schedulers, paused
- grant the scheduler identity `run.jobs.run` on the daily job
- grant the orchestration identity permission to read its own execution and job
- run `verify-runtime` through the real run path and read back the principal and a terminal
  execution

Each step now has a reviewed tool. `ops/deploy/runtime_jobs.py` creates the jobs and
`ops/deploy/runtime_schedulers.py` creates the schedulers paused, each run in the order `plan`,
`apply`, `readback`, and each `apply` accepts `--dry-run`. `ops/deploy/runtime_preflight.py`
runs `trigger`, `confirm` and `handoff` for the `verify-runtime` proof. `runtime_jobs.py plan` requires a build record that pushed exactly one image with the
given digest, and refuses `iam_delta_unapproved` while the delta's approval state is
`proposed`. The scheduler stays paused throughout; no command in those tools can resume it.

One unresolved decision sits on top of this, recorded in `docs/operations/iam-delta.md`: Cloud
Scheduler has no job level IAM, so no scheduler binding is proposed, and the person who creates
the two scheduler jobs is the principal who needs `roles/iam.serviceAccountUser` on
`intelligence-42-scheduler`. That grant is made at provisioning time.

### How you would read a cycle, once it runs

This part is unverified, because no cycle is recorded. On the evidence available:

- A `verify-runtime` run returns a receipt naming the attached principal and the bound digests.
  It touches no authority adapter, no vendor and no model.
- A `daily` run reads the existing operation for the request identifier first. A terminal
  record with known usage is a duplicate trigger. Any other existing record refuses with
  `operation_usage_unknown`, and an unresolved previous operation refuses with
  `operation_unresolved`.
- The runtime writes no local completion file and keeps no budget ledger of its own. Whatever
  you read about a cycle, you read from the durable stores, not from the runner.

---

## 9. Journey 4: open the product and walk the capability journeys

**Status: this one works today.** It runs in your browser, against the revision that is
currently serving. The revision and its deployment digest are set at release; take them from
the handover, or from the release `verify` receipt or the latest renewal receipt, whichever is
newer.

`app/docs/staging-vetting.md` is the tester's version of this section, and it is the document
to hand to anyone else who is vetting. It contains no secrets and requires no operations
script. This section does not repeat it; where a step is written there, this section points to
it.

### Step 1. Enter

Follow "How to enter" in `app/docs/staging-vetting.md`. The staging URL and the access key are
handed over separately. After the key is accepted the app opens on the Briefing at `#/pulse`.

### Step 2. Know what dates you are looking at

Before you judge anything, read the dates off the product, because every one of them is set at
release:

1. On the Briefing, the header names the Closed window the released run observed and its
   Completed run date. The checked age in the app's utility strip counts from the desk's last
   refresh.
2. On Ask, at `#/console?work=ask`, the line under the page title states the Ask window, for
   example "Covers South Africa, " followed by the dates. That line reads the window from the
   server, so when it disagrees with a handover or a document, the page is right.
3. The rail line that ends in (Briefing) is the Briefing's window, not the Ask window.

Write the Ask window and the Briefing run date at the top of your findings, so every finding is
read against the dates you actually tested.

### Step 3. Walk the journeys

There are eleven capabilities in `app/docs/capability-journeys.json`. Each has a job, a primary
action and a proof panel. Walk them in this order; the later ones depend on having a request
from the earlier ones.

| # | Capability | Route | The job | The proof panel |
|---|---|---|---|---|
| 1 | `today_briefing` | `#/pulse` | Read a dated editorial lead from the released run with its source proof and a useful next action. | Briefing evidence summary with receipts, run date and the readiness state on the lead card. |
| 2 | `discovery_exploration` | `#/explore` | Read released candidates with membership and evidence, then follow a topic or seed explorer deep link without losing market scope. | Discover candidate rows with membership counts, evidence readiness and the released run identity. |
| 3 | `compare` | `#/compare` | Compare released signals in the same units and window against a named comparator. | Comparison strips showing question, unit, time window, comparator and source per row. |
| 4 | `questions_followups` | `#/console?work=ask` | Ask a question and read a request bound answer with its follow up, challenge and explicit missing coverage. | Structured reply with admitted claims, receipts, readings, limitations, missing work, and the request and usage details disclosure. |
| 5 | `sources_evidence` | from any cited answer | Open a citation and read the exact record identity, excerpt, date, link, market and support relationship. | The receipt record with citation label, source label, excerpt, published date, market, record reference and the claim's support state. |
| 6 | `fieldwork` | `#/fieldwork` | Read real source and research operations with route coverage, and follow Open question to the stored question. | Operation rows with readiness, market scope, status and gaps, the observed operations strip, and the Source Lab snapshot inventory. |
| 7 | `coverage_source_lab` | `#/source-lab` | Read source units, denominators, freshness, unknown states and source to product loss. | Source Lab inventory rows with route status, downstream use, credit budget authority and the catalog snapshot digest. |
| 8 | `investigation_build_review` | `#/console?work=brief&investigation=...` | Read the durable investigation frame, its selections, the human review decision and the exact artifact. | Evidence Room claims list, decision record and artifact title, with the read details disclosure on failure. |
| 9 | `creators_network_language` | `#/creator/<handle>`, `#/network` | Read broad observed contributors, their actual relationships and language evidence, with no invented demographic authority. | Creator profile posts wall and reach series, network voices and topic links, listen mentions with market and sentiment. |
| 10 | `history` | `#/historical/<investigation>/<mode>` | **Not implemented.** Expect the unavailable state with its error code. | Historical rows, or the unavailable state with its error code. |
| 11 | `bsa_configuration` | the brief route persona picker | **Not implemented.** There is no configured persona or audience lens for Brand South Africa. On staging the persona picker is not reached at all, because Build brief asks 42's question engine instead (Step 6). | Persona picker with description, default markets, product frame and signal topics. |

### Step 4. Open a source from an answer

This is the single most important check in the whole product, because it is the difference
between an answer and a claim. Follow "How to open sources" in `app/docs/staging-vetting.md`.
**A receipt with no link or no excerpt is a finding.** Report it with the request and the
citation label.

### Step 5. Download an answer

A completed Ask answer carries two buttons in its actions row: Download as HTML and Download as
PDF. They appear only on an answer whose usage has settled and that carries no error. In the Ask
conversation the same row also offers Turn into a cited brief, which Step 6 covers.

1. Press Download as HTML. The browser saves `42-answer-<first eight characters of the request
   id>.html`. Open it. It carries the question, the answer and its cited sources, rebuilt by the
   server from the stored answer, with the dates written as the page writes them.
2. Press Download as PDF. The browser saves the same copy as a PDF with the same name and the
   `.pdf` ending.
3. Reload the answer from its request URL and download again. The copy must be the same answer.

If a download is refused, the answer shows the reason in plain words under the buttons. Four of
them are expected rather than faults:

| What the page says | What it means |
|---|---|
| Only a completed answer can be exported. This request has no completed answer to export. | The request has not finished, or finished without an answer. |
| This answer is held until its usage record is settled, so it cannot be exported yet. | Wait and try again. |
| Another PDF is being made. Try again in a moment. | One PDF is made at a time. Try again. |
| Sign in again to download this answer. | The access key has expired in this browser. |
| The download did not complete. Try again in a moment. | The browser lost the download or the server gave no reason. Try again; report it if it repeats. |
| The stored answer could not be read just now. Try again in a moment. | The server could not read the stored answer at that moment. Try again; report it with the request id if it repeats. |
| This answer is not available in this workspace. | The request id does not name an answer this access key can see. Check you are on the request URL you meant. |
| The answer reference is invalid. | The request id in the address is not a well formed request id. Reopen the answer from its request URL. |

Two are findings: "A PDF cannot be made on this server right now", because the PDF runtime
failed on the serving revision (the HTML copy should still download), and "The stored answer
did not pass its checks, so it was not exported". Report either with the request id.

### Step 6. Build a cited brief on staging

Open Ask, then press Build brief in the rail, or go to `#/console?work=brief`. You can also press
Turn into a cited brief on an answer in the Ask conversation, which opens the same page with
that question as the topic. On staging only 42's question engine may use the model, so the
brief is written by the same engine Ask uses. The page reads `/api/research/availability` when
it opens and shows:

1. The heading Build a cited brief with 42's question engine, with a sentence that names the
   covered window (the same window as the Ask page line, set at release).
2. Three fields: Topic, Market (South Africa, Nigeria, Kenya or All three markets) and Framing
   (optional).
3. Once a topic is filled in, the exact question under The question 42 will be asked, for
   example "Write a cited brief on mobile data prices in South Africa." Check that it says what
   you typed.
4. Press Ask 42 for a cited brief. The page says it is sending, then that 42 is writing the
   brief, which can take a few minutes.
5. When the answer is ready the page opens it at `#/console?work=ask&request=<request id>`.
   That cited answer, with its sources and its Download as HTML and Download as PDF buttons,
   is the brief. Download both and check them as in Step 5.

If the question is not answered, the page stays where it is, shows the missing work in plain
words and offers an Open the request link to the stored request. That is correct behaviour,
not a fault. A persona picker, a behaviour scan or a brief writer starting on staging is a
finding. A brief link that names an existing artifact still opens that artifact, and an
investigation link opens its Evidence Room as in row 8 of Step 3.

This page depends on the cited brief change being on the serving revision. A revision without
it shows Cited briefs are not available here with a Go to Ask button instead; note which one
you saw in your findings.

### Step 7. Reopen a saved request

Follow "How to return to a saved request" in `app/docs/staging-vetting.md`. Two things to check
closely: a request whose execution is unconfirmed says so and shows no answer, which is correct
behaviour and not a missing answer; and Ask a follow-up on a stored answer opens a new
conversation carrying the stored turns.

### Step 8. Read the state a route is in

Each capability declares which of eight states it can show: `populated`, `loading`, `empty`,
`thin`, `stale`, `contradictory`, `failed`, `held`. Where a state cannot apply to a capability,
`app/docs/capability-journeys.json` records an `inapplicable_reason` in place of the state,
rather than leaving it unexplained. Two examples so you recognise the pattern:

- The Briefing cannot be `contradictory`, because it reads one released run and one run cannot
  disagree with itself. Disagreement between sources is a Compare state.
- A citation receipt cannot be `stale`, because it is bound to a closed snapshot and window. It
  is either the record or it is absent, and its age is stated on the record.

When a route shows a state you did not expect, check the capability's row first. A declared
inapplicable state appearing on screen is a finding. A declared state appearing is not.

### Step 9. Record a finding

Use the six field format in "How to report a finding" in `app/docs/staging-vetting.md`: route,
request, revision, expected, actual and severity. The app does not display the revision, so
copy it from the handover. Attach a screenshot when the finding is visual. Do not paste the
access key or any other credential into a report.

### What this journey does not prove

`app/docs/capability-route-states.json` records 104 capability and state rows. Every one of
them has `native_data_binding: fixture`. Sixty five of them carry the remaining gate
`native staging data`. So the state matrix behind these journeys was measured against fixtures,
not against staging data.

The same is true of the frontend certification numbers. `app/docs/frontend-certification.md`
states that its suites run against the populated fixtures of the state matrix served by the
local preview build, that the candidate name is `unbound` until a native run sets it, and that
every binding field is null with the reason `native binding pending`. Until those fields are
filled from a real deployment record, that output is a fixture receipt and cannot replace a
failing native check.

Your browser walkthrough against the live service is therefore the more meaningful evidence
today, and it is the reason this journey is worth your hour.

---

## 10. Telling a real failure from a known open item

Work down this list in order. Stop at the first row that matches.

| What you see | Is it a failure | What it is |
|---|---|---|
| `#/historical/...` shows unavailable with an error code | No | Known open item. The historical producer is unimplemented and the route is required to say so. |
| Build brief shows Build a cited brief with 42's question engine and opens the answer on Ask | No | Correct on staging. The cited answer is the brief. |
| Build brief shows Cited briefs are not available here | No, but note it | The serving revision predates the cited brief change. Record the revision. |
| There is no Brand South Africa persona or lens | No | Known open item. There is no configured persona or audience lens for it. |
| The desk reads stale or amber | Not by itself | The desk freshness depends on when the desk was last refreshed, which is set at release. Compare the checked age with the handover. |
| The Ask window or Briefing run date differs from a handover or a document | No | Those values are set at release. The page is right. |
| Ask refuses with "The service's pricing review has lapsed and needs renewing" | Not a product fault | The pricing review of the serving policy has lapsed. Renew it (section 7) and report the time you saw it. |
| A retained answer regression test skips | No, but it is not a pass either | Open verification obligation. The historical fixtures were not supplied. |
| A frontend test fails reading `app/web/dist` on a clean clone | No | You ran `bun test` before `bun run build`. Build first, as section 5 does. |
| A route shows a state its capability row declares applicable | No | Expected behaviour for that state. |
| A route shows a state its capability row declares inapplicable | Yes | Report it. |
| A citation receipt has no link or no excerpt | Yes | Report it with the request and the citation label. |
| A PDF download is refused as not possible on this server | Yes | Report it with the request id. The HTML copy should still download. |
| A release command exits 1 with a named `error` value | Depends | Look the code up in section 7. Most are guards refusing before any write, which is the system working. |
| A release command leaves `unproven` or `failed_or_unproven_requires_native_reconciliation` | Yes, and handle it carefully | A write was issued and its outcome was not proven. Do not retry the same command. Follow the forward recovery path in section 7. |
| The app build fails at any step | Yes | Report it with the step id and the build id. A step 7 failure in the PDF runtime download names the URL it gave up on. |
| A daily cycle refuses with `daily_wiring_unavailable` or `stage_adapter_unavailable` | No | Known open item. The daily wiring is not bound yet. |
| A gcloud call refuses on permission for an amendment e row | No | Expected. Amendment e is approved in the file but `applied: false`, so the repository records none of its grants as applied. |

The general rule behind that table: this system is built to refuse rather than to guess. A
refusal with a named code, before any write, is the system working. The states worth your
attention are the ones where something was written and the result was not proven, and the ones
where a document told you to expect a thing and the product showed you a different thing.

---

## 11. Where the evidence of each run lands

| Run | Where its evidence lands |
|---|---|
| Local suites (section 5) | Your terminal. Preserve the full output and the exit status in the verification receipt, and keep it outside the checkout. |
| Local image builds | The image IDs. Record them. |
| Cloud Build (section 6) | Cloud Logging only. The build config names no logs bucket, no artifacts and no secrets, by design. The three values you carry forward are the build `name`, `source.connectedRepository.revision` and `results.images[0].digest`. |
| Activation, release, resume, rollback (section 7) | The file you named with `--output`, written once and never overwritten. On a refusal the same file carries the state, the error and, where a write was issued, the forward recovery plan. |
| Renewal (section 7) | The receipt the renewal command writes, carrying `42_renewal_receipt_v1`, the command and the inputs it ran on, including refusals. |
| Job and scheduler creation (section 8) | The `runtime_jobs.py` and `runtime_schedulers.py` receipts, bound to the plan they applied. |
| Browser certification suites | A fresh disposable directory per run under the system temporary directory, unless you set `CERTIFICATION_EVIDENCE_DIR` to an absolute path to retain the run. The file is `frontend-certification-<candidate>.json`. Failed cases keep a full page screenshot under `app/frontend/test-results`, and the proof output names the file. |
| Browser journey suite | The Playwright results for `bun run test:journeys`. Preserve them. |
| Product findings (section 9) | Your finding rows. Six fields each, with the Ask window and Briefing run date you tested against. |
| Scheduler reference documentation | Retained in the evidence directory as `r05-scheduler-doc-cloud-run-jobs-run.md` and `r05-scheduler-doc-cloud-scheduler-httptarget.md`. |
| IAM discovery and amendment d apply | The `r03-native-*.json` readbacks named throughout `docs/operations/iam-delta.md`, and the amendment d receipts on Albert's machine. |

Two things never appear in any receipt, and if you ever see them, that is a serious finding:
the bearer token the native adapter uses, and the value of any secret backed environment entry.
Receipts record such an entry only as `source: secret`.

---

## 12. Everything that is not recorded as run

Collected here so you can see the whole shape without reading the rest of the guide again.
Every line is something the repository and the build lead's records do not show as run
natively, or something proven only against a local test double.

**Recorded as run by the build lead, with receipts outside the repository.**

- The app image publish build for the released revisions, implied by the recorded releases
  rather than recorded separately.
- The releases of 23 and 24 September 2026, and a pricing renewal serving on 25 September.
- Amendment d of the permission delta, applied on 23 September 2026.

**Not recorded as run natively against staging.**

- The rollback drill, including the route failure path with a real traffic readback.
- The managed pricing renewal job.
- The daily cycle, in either mode.
- `verify-runtime` through the real run path with a principal and terminal execution readback.
- Job creation, scheduler creation and the scheduler grants.

**Approved but not applied.**

- Every amendment e row in `ops/deploy/iam_delta_v1.json`. The approval is recorded; `applied`
  is `false`; no native permission has been read back against those rows.

**Measured against fixtures, not staging data.**

- All 104 rows of `app/docs/capability-route-states.json`.
- Every measurement in the frontend certification output, whose candidate binding fields are
  null with the reason `native binding pending`.

**Unverified in this guide.**

- Everything the build lead records. This guide cites those records; it has not read the
  receipts.
- How a daily cycle reads once it runs. Section 8 describes it from the runtime contract and the
  configuration files, because no cycle is recorded.
- Which rows of the absent resource list in `docs/operations/iam-delta.md` have since been
  created. The discovery has not been retaken.
- `docs/operations/release.md` still lists the native release under "Proof status" as unproven.
  It predates the recorded releases.

---

## Where each part of this guide comes from

| Section | Source |
|---|---|
| Resource guard, manifest digest, cutover | `docs/operations/resources.md` |
| Release, rollback, resume, renewal, both publish builds | `docs/operations/release.md` |
| Daily cycle, scheduler, job creation | `docs/operations/scheduling.md` |
| Project inventory, identities, permissions | `docs/operations/iam-delta.md`, `ops/deploy/iam_delta_v1.json` |
| Checkout, environments, suites, local images, gates | `docs/operations/bootstrap.md`, `app/.github/workflows/ci.yml` |
| Product entry, routes, receipts, findings, dates | `app/docs/staging-vetting.md` |
| Answer download | `app/src/api/ask_export.py`, `app/frontend/src/ui/AnswerExport.jsx` |
| Build brief on staging | `app/src/api/main.py` for the `/api/research/availability` route, `app/src/api/research.py` for the message text, `app/frontend/src/ResearchDocPanel.jsx`, and `app/frontend/src/briefViaQuestion.jsx` on the cited brief change (branch `fix/42-staging-cited-brief`) |
| The eleven capabilities and their states | `app/docs/capability-journeys.json` |
| Fixture binding of the state matrix | `app/docs/capability-route-states.json` |
| Certification thresholds, evidence location, candidate binding | `app/docs/frontend-certification.md` |
| Build steps and their arguments | `cloudbuild.app.publish.yaml`, `app/Dockerfile.general-question` |
| Recorded releases and renewal | The build lead's release records, not in the repository |
