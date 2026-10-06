# Runtime entry points

`runtime-entry-points.json` is the E03 inventory of every way code in this repository is started at runtime and the privileges it starts with. It is written from the code and the deploy sources, not from a live readback, and two tests keep it honest:

- `app/tests/unit/test_runtime_entry_points.py` rebuilds the HTTP table from the real FastAPI application object and the handler source, and calls every guarded route with no credential and with wrong, rotated, misplaced, forged and expired ones.
- `ops/tests/test_runtime_entry_points.py` rebuilds the service, queue, job, scheduler, privilege and container sections from the resource manifest, the job runtime files and the scheduler file under `infra/runtime`, the release module, the approved IAM delta and the container files.

A route, job, scheduler, role or entrypoint that changes without the inventory changing fails one of them. To update the inventory, change the code first, run the two tests, and copy the observed values they report into the JSON.

## http

- `application`: the served ASGI object.
- `framework_docs`: the FastAPI documentation URLs. All three are null, and the test asserts `/docs`, `/redoc` and `/openapi.json` return 404.
- `mounts`: every `app.mount` in the module. `/assets` is mounted only when a Vite build exists at startup and serves content hashed static files.
- `routes`: one row per method and path.
  - `endpoint`: module and function that handles it.
  - `dependencies`: FastAPI dependencies the route declares. `require_passcode` is the constant time `X-Passcode` check against `UI_PASSCODE`; with no passcode configured it refuses with 503 unless `LP_ALLOW_OPEN_GATE=true`.
  - `handler_guards`: checks the handler runs in its own body or in a helper it reaches: `same_origin` (Origin and Host must match, same site fetch), `review_session` (a live dossier review session cookie plus its CSRF header), `review_credential` (a Google signed review credential bound to the login nonce and the subject allowlist), `worker_oidc` (a Google signed OIDC token for the fixed audience whose email is the one invoker), `card_token` (the handler compares the per card HMAC token it was given), `card_token_mint` (the handler computes a card token for a caller its dependency already admitted and compares nothing; `/api/card-link` is the one such route), `workspace_scope` (the investigation must resolve to the server's workspace scope).
  - `access`: the one word summary derived from the two lists: `public`, `passcode`, `passcode_and_review_session`, `passcode_and_review_credential`, `worker_oidc` or `card_token_or_passcode`.
  - `public_reason`: why a `public` route is intentionally open; null on every other route. Only `/`, `/api/health` and `/api/auth/verify` may be public.
  - `invoker`: on the worker route only, the one service account whose token it accepts.

The two `/api/internal/v2/question-delivery` routes take an advisory browser report that a stored answer was rendered and read back what this process recorded. Both are `passcode` routes, and both read the request through the same scope bound status read as `/api/chat/status` before accepting or showing anything, so a request unknown in the server's scope is refused with 404. The report is size and rate limited and writes only an in process ledger, never the question store or a review record.

## service and queues

`service` is the Cloud Run service the release module deploys: image repository, container file, runtime service account, the secret mounted into its environment, and the members that hold a role on the service in the approved delta (`resource_iam`). `queues` is the Cloud Tasks queue that calls the worker route, with the OIDC identity and audience its tasks carry.

## jobs and schedulers

`jobs` has one row per Cloud Run job the resource manifest declares. `runtime` is the image repository, command, arguments, environment names, mode, service account and entry module from the one runtime file under `infra/runtime` that declares the job, named in `declared_in`, or null when no runtime file declares it. Every JSON file under `infra/runtime` with a `jobs` object is read: `daily-staging.json` declares the daily and price policy jobs and `ingest-staging.json` declares the ingest job alone, and a job declared in two files fails the test. The ingest job's `env_names` are the ones the file declares; the job creation step adds `COLLECTION_SOURCE_SHA` and `COLLECTION_IMAGE_URI` from the build it deploys (`ops/deploy/ingest_runtime.py`), so they are not in the file or the inventory. `entry_script` is the script that binds itself to the job, with the constant in it that names the job, or null when no script does; today only the ingest job has one (`engine/scripts/staging/collect_42_sources.py`, `INGEST_JOB`, run in the engine image as `python -m scripts.staging.collect_42_sources`), and the test checks the constant, the `CLOUD_RUN_EXECUTION` read and the program guard in that file. No origin registry binds an operation to the ingest job: `source_collection` in the bridge registry is bound to the daily job, which dispatches the collection and waits for the ingest receipt, and names the `intelligence_42_sources_staging` dataset. `origin_bindings` lists every operation the execution origin registries bind to the job (`execution_origins_v1.json`, the registry the resource manifest pins by digest, and `execution_origins_bridge_v3.json`, the one the active execution generation names), each with its source file, service identity, image repository and datasets. A job with a null `runtime`, a null `entry_script` and no `origin_bindings` has nothing in the repository that says what it runs or as whom; only its `resource_iam` is known. `triggered_by` names the scheduler that runs it. `resource_iam` lists who holds a role on the job itself. `schedulers` records schedule, time zone, state, target and the OAuth identity of each scheduler job in `infra/runtime/scheduler-staging.json`.

## privileges and service_accounts

`service_accounts` maps each service account to every role it holds in `ops/deploy/iam_delta_v1.json`: the delta `bindings`, the `retained` existing grants and the bindings of every amendment in `amendments`, with resource, role, condition and the section it came from (`amendment_f`, `amendment_g`). An amendment's rows are listed whatever its approval state, so a proposed grant is visible before it is approved: at this revision amendment f is approved and not applied, and amendment g is proposed, with no approver, and not applied. Amendment g extends `QuestionVertexPredict` to 2026-12-31 for the serving account and the daily account, extends the serving account's `QuestionControlReplace` on the allowance ledger object to the same date, gives the daily account bucket viewer on the source artifact bucket, object viewer on its `inputs/` and `captures/` prefixes and object creator on `captures/`, and gives the serving account object viewer on the same two prefixes. `privileges` records which delta was read and its approval state. `live_state_verified` is false: these are the grants the delta records, approved or proposed, not a readback of the project. Comparing them with live IAM needs a native read and is a separate E03 step.

## engine_entry_points and containers

`engine_entry_points` lists the engine modules the daily job imports in `daily` mode and the modes the managed runtime accepts. `containers` records the final stage of every Dockerfile (base, user, entrypoint, command), the Procfile processes, and for every Cloud Build file the Dockerfiles it builds and the images it pushes. A null `entrypoint` or `cmd` means the final stage does not set it, so the value inherited from the base image applies. A Cloud Build Dockerfile is read from the `-f`, `--file` and `--file=` forms, and a separate test checks that every Dockerfile a Cloud Build file names reaches its row.

## Out of scope

- `routine_authorizations` in the IAM delta: these authorize a BigQuery routine to act on a dataset. They are grants to a routine, not to a principal, so no entry point gains a role from them; they are applied by the execution store migration and belong with its checks.
- Scheduler actAs constraints: Cloud Scheduler has no job level IAM, and the delta's `unresolved` section records that the operator who creates the two scheduler jobs needs actAs on the scheduler account at provisioning time. That is an operator grant made outside the delta, not a privilege of any runtime identity, so it is not inventoried here.
- GitHub workflows: `app/.github/workflows` and `engine/.github/workflows` are test pipelines carried over from the source repositories. GitHub runs workflows only from `.github/workflows` at the repository root, which this repository does not have, and none of these files authenticates to the project or deploys anything.
