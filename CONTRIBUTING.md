# Contributing to 42

Read [the rules](docs/full-42/RULES.md) first, then the part of [the specification](docs/full-42/README.md) your change touches. The product lives in `core/` and the web app in `app/frontend/`. The older `engine/` and `app/` code is kept for reference; lift from it as [SALVAGE.md](docs/full-42/SALVAGE.md) says, and do not rewrite or delete files you are not lifting.

Keep changes narrow. Preserve stored data and the API contract (`core/api/contract.md`). Write the test or check first, then the code. Checks are real: a unit test, a SQL test on fixtures, a dry run, or a staging run with row counts. A local test is never reported as a staging result.

Run the focused checks for what you changed, from the repo root:

```bash
py -3.13 -m pytest core/<area> -q
cd app/frontend && bun install --frozen-lockfile && bun test
```

Every change gets an independent review before it is committed, and blockers are fixed first. Pull requests state the effect for users or operations, the files changed, the checks run and anything still unproven. Never include credentials, client data, local paths or secret values.
