# 42 analyst application

The 42 analyst application presents evidence from the cultural intelligence engine across South Africa, Nigeria and Kenya. It provides a ranked board, creator and topic views, a slang lexicon, question and investigation workflows, review surfaces and exports.

It is a passcode gated team tool, one service that serves the API and compiled UI. The app contains read routes and controlled write interfaces for question submission, investigation creation and research runs, plus research export routes. Review and exact-version artifact workflows are being completed on the staging branch. These writes do not authorize production changes, client approval or external sends.

## Architecture

- Backend: FastAPI (`src/api/`), one process. Serves `/api/*` and the built UI from `web/dist`.
- Frontend: Vite + React, vanilla function components, Chart.js plus inline SVG charts. Source in `frontend/src`, compiled to `web/dist`.
- Data: Google BigQuery (`ogilvy-trends-v2.trends_v2_dev`), read only. Vertex Gemini for the Ask brief and the console.
- Hosting: one Cloud Run service `listening-post` in `ogilvy-trends-v2`, `us-central1`, pinned to a single instance.
- Gate: a passcode (`UI_PASSCODE`). The client sends it as the `X-Passcode` header on every `/api/*` call.

## Quick start

```bash
# Backend tests (BigQuery and Vertex are fully mocked, no credentials needed)
python -m pytest tests/ -q

# Lint
ruff check src/

# Frontend build (outputs to ../web/dist, which FastAPI serves)
cd frontend && bun run build

# Frontend gates
node scripts/check_jsx.mjs        # every src module parses
node scripts/check_contrast.mjs   # WCAG AA in both themes
```

Config lives in a `.env` at the repo root (not committed). Copy `infra/env/env-template` and fill it in: `GCP_PROJECT`, `BQ_DATASET`, `UI_PASSCODE`, plus the Gemini values.

## Deploy

The deployment notes below describe the historical Listening Post service path. Treat them as legacy operational material while 42 staging release work is reviewed. Follow the repository bootstrap and current staging runbooks before using any deployment command.

Two interchangeable paths, both deploy the same way:

- Historical automatic path: a main branch update triggered the former Listening Post workflow. That workflow is not configured as the 42 deployment path in this repository. Do not infer hosted deployment from a green local check or a branch update.
- Manual: `bash infra/cloud-run/deploy.sh` from a machine with `gcloud` authenticated. It reads the local `.env` and deploys.

Keep the service pinned to one instance (`--min-instances 1 --max-instances 1`): a second instance would split the in-process cache and async asks would poll the wrong one.

## The data rules

Every figure on screen obeys these. They are the point of the product.

1. Voices and creators rank by engagement (summed post engagement), never post count (activity). Engagement is the winsorized sum of post engagement.
2. Share of voice is a relative percent per single market, summing to 100 across the board, and consistent with the mention count on the same row.
3. Every figure carries its unit, window and market (for example "312M engagement, 30d, ZA"). No bare numbers.
4. Markets (ZA, NG, KE) never blend into one figure.
5. Nothing is a forecast. Momentum is observed volume.
6. Green means up or positive, red means down or negative, only. The accent colour is brand and interactive, never a "good" signal.

## Gotchas

- Stale cached payloads outlive code fixes. Purge the GCS cache blob and redeploy when a payload shape changes.
- Generated brief payloads must carry `topic` or the client rejects them as unreadable.
- Tests mock `bq._run_query` with a SQL routing dispatcher and the Gemini client at the synth layer. A new BigQuery query needs a dispatcher branch or the suite raises "unexpected sql". Keep every test credential free.
