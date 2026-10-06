# Repository bootstrap

Run from a clean checkout of the selected commit. Keep build contexts, receipts and retained request artifacts outside the checkout. No original source checkout is required by the shipped images.

## Prerequisites

Use CPython 3.13, Git, Bun and a Docker daemon that builds Linux amd64 images. Dependency installation downloads the distributions selected by the committed hash locks. Do not regenerate locks as part of bootstrap.

The examples use PowerShell. Resolve the repository root first and create two fresh environments. On Linux, use an explicitly verified `python3.13` to create them and replace `Scripts/python.exe` with `bin/python`; the engine uses `requirements-dev.lock` on Linux.

```powershell
$ROOT = (Get-Location).Path
py -3.13 -m venv "$ROOT/.venv-engine"
if ($LASTEXITCODE -ne 0) { throw 'Engine environment creation failed' }
py -3.13 -m venv "$ROOT/.venv-app"
if ($LASTEXITCODE -ne 0) { throw 'App environment creation failed' }
$PY_ENGINE = "$ROOT/.venv-engine/Scripts/python.exe"
$PY_APP = "$ROOT/.venv-app/Scripts/python.exe"
& $PY_ENGINE -m pip install --require-hashes -r engine/requirements-dev-windows.lock
if ($LASTEXITCODE -ne 0) { throw 'Engine dependency installation failed' }
& $PY_APP -m pip install --require-hashes -r app/requirements-dev.lock
if ($LASTEXITCODE -ne 0) { throw 'App dependency installation failed' }
& $PY_ENGINE -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Engine dependencies are inconsistent' }
& $PY_APP -m pip check
if ($LASTEXITCODE -ne 0) { throw 'App dependencies are inconsistent' }
Set-Location "$ROOT/app/frontend"
bun install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw 'Frontend installation failed' }
Set-Location $ROOT
```

Backend route checks import the frontend router. Install its locked dependencies before running the app suite, including in a backend-only validation checkout.

## Source checks

Run each suite from its own package directory so that engine and app imports stay separate. Preserve full output and exit status in the verification receipt. Build the frontend before running its suite: the contract tests read the built output at `app/web/dist`, which a clean checkout does not carry, so the suite fails on every platform when the build has not run yet.

```powershell
Set-Location "$ROOT/engine"
& $PY_ENGINE -m pytest tests/unit -q -ra
if ($LASTEXITCODE -ne 0) { throw 'Engine suite failed' }
Set-Location "$ROOT/app"
$env:GENERAL_QUESTION_ENGINE_TEST_ROOT = "$ROOT/engine"
$env:GENERAL_QUESTION_ENGINE_TEST_PYTHON = $PY_ENGINE
& $PY_APP -m pytest tests -q -ra
if ($LASTEXITCODE -ne 0) { throw 'App suite failed' }
Set-Location $ROOT
& $PY_ENGINE -m pytest ops/tests -q -ra
if ($LASTEXITCODE -ne 0) { throw 'Operations suite failed' }
Set-Location "$ROOT/app/frontend"
bun run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
bun test
if ($LASTEXITCODE -ne 0) { throw 'Frontend suite failed' }
bun run test:journeys
if ($LASTEXITCODE -ne 0) { throw 'Browser journey suite failed' }
Set-Location $ROOT
```

The frontend build creates `app/web/dist`, which the frontend unit tests require. The browser journey suite is selected by `app/frontend/playwright.config.mjs`; run it against the built application and preserve its results. A successful frontend compile is not browser proof.

Retained answer regression tests accept an explicit `OPEN_INTELLIGENCE_RETAINED_FIXTURES` directory. Supply the separately verified historical request and policy artifacts, then rerun `tests/unit/test_general_question_answer.py` and `tests/unit/test_general_question_model_migration.py` from `engine/`. Do not substitute generated examples for those historical bytes. Missing artifacts or platform skips remain open verification obligations.

## Reviewed app context

The context builder accepts `engine/` and `app/` within this repository. It also retains standalone source support. Its inventory binds the common Git commit, selected files and the externally pinned PDF dependency manifest. Modified or omitted required inputs refuse packaging.

Prepare the external review JSON from `inventory_source(engine_root, kind="engine")` and `inventory_source(app_root, kind="lp")` in `app/scripts/build_general_question_context.py`. The review object has `contract_version: general_question_context_review_v1`, `engine` and `lp` inventory objects. `allow_reviewed_working_tree` is false for a clean release commit. Any true value requires an exact reviewed working diff and produces a receipt that is not deployment ready.

Generate the review inventory in a fresh directory outside the checkout. Inspect this exact inventory before building. The context directory and receipt file must not already exist.

```powershell
$BUILD_ROOT = Join-Path (Split-Path $ROOT -Parent) ("42-build-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $BUILD_ROOT | Out-Null
$CONTEXT = Join-Path $BUILD_ROOT 'context'
$REVIEW = Join-Path $BUILD_ROOT 'context-review.json'
$RECEIPT = Join-Path $BUILD_ROOT 'context-receipt.json'
@'
import json, sys
from pathlib import Path
root, target = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "app/scripts"))
from build_general_question_context import inventory_source
review = {
    "contract_version": "general_question_context_review_v1",
    "allow_reviewed_working_tree": False,
    "engine": inventory_source(root / "engine", kind="engine"),
    "lp": inventory_source(root / "app", kind="lp"),
}
with target.open("x", encoding="utf-8") as output:
    json.dump(review, output, sort_keys=True, indent=2)
    output.write("\n")
'@ | & $PY_APP - $ROOT $REVIEW
if ($LASTEXITCODE -ne 0) { throw 'Context inventory failed' }
```

```powershell
& $PY_APP app/scripts/build_general_question_context.py --engine-root engine --lp-root app --destination $CONTEXT --review-manifest $REVIEW --receipt $RECEIPT
if ($LASTEXITCODE -ne 0) { throw 'Reviewed context refused' }
docker build --file "$CONTEXT/Dockerfile.general-question" --tag 42-app:local $CONTEXT
if ($LASTEXITCODE -ne 0) { throw 'App image build failed' }
docker build --file engine/Dockerfile.staging --tag 42-engine:local engine
if ($LASTEXITCODE -ne 0) { throw 'Engine image build failed' }
```

Record the resulting immutable image IDs. The engine image intentionally refuses an unspecified workload. Test explicit entrypoints with networking disabled; no collection or cloud mutation is part of this bootstrap.

## Linux runtime gates

`ops/tests/Dockerfile.linux-gates` extends an exact app image for testing. It creates separate hash-locked test environments. Test adapters and pytest are excluded from the product image.

`ops/tests/fixtures/linux_boundary/manifest.json` binds the engine bundle, test source closure, adapters and required node IDs. Its commands describe the boundary tests, combined app-to-engine observer test and engine observer tests. Supply the reviewed immutable app image and bind the resulting test image ID in the execution receipt. The prebuild source-layout test runs from the checkout; native tests run as UID 10001 with networking disabled. Changed bundle bytes must fail before any engine launch.

Repin the manifest with `ops/build/pin_linux_gate_manifest.py` at every candidate. `--check` lists every stale or missing entry and exits non-zero; `--write` rewrites only the digest fields, then moves the new manifest sha256 into the publish build config when that file is present. The sha256 of the manifest file is the `gate_sha256` the activation record carries, so the activation gate binds the exact manifest bytes the image was verified against.

Both modes refuse with exit 2 for CRLF conversion, an input absent from HEAD, an unavailable HEAD or a checkout changing during computation. `pin_tool_checkout_dirty` identifies bytes that differ from their HEAD blob and cannot be repinned in that invocation; write mode admits a changed tracked input only when its computed pin moves. Commit a written repin before the next check or write.

Run `app/tests/unit/test_pdf_renderer_linux.py` in a test environment using the actual final app image, with `PDF_RENDERER_NATIVE=1`. Confirm that all four native cases execute. The renderer must emit parseable PDF text with the packaged font, reject fallback fonts and an unsandboxed browser, remove detached descendants after timeout or cancellation, and leave no request files behind. A missing browser or a native skip is not a passing export check.

Repeat bootstrap and image preparation from a disposable clone at a different path. Retain the exact commit, lock and context hashes, image IDs, complete test logs and skip dispositions. Do not mount an original source checkout into the product images.
