# Trends Engine V2 Status

> **README.md is canonical** for architecture, the connector table, the
> scoring model, and the cron topology. This file holds only the things
> README does not: open decisions and the weekly-review notes. Do not
> restate connector counts or scoring weights here; they drift. Link the
> README section instead.

**Last reviewed:** 2026-07-15. **Master:** `156a85d`.

## Where the state lives

| Question | Source of truth |
|---|---|
| What connectors run, at what volume | README.md connector table (13 live) |
| Scoring signals and weights | `configs/scoring.yaml` + docs/scoring-methodology.md |
| Cron topology | README.md. GCP-native: Cloud Scheduler 00:30 primary + 02:30 fallback to the Cloud Run job `:run` API, watchdog 06:30 UTC. GitHub holds no execution path, it is version control only. |
| Live run health this morning | the `morning-check` skill (reads BigQuery) |
| Current build direction | docs/trends-engine-v3-blueprint-v3.8.md (canonical blueprint) |

## Outstanding decisions

| Item | Forcing function |
|---|---|
| `SEARCH_VELOCITY_ENABLED` flip | Signal is wired end-to-end but stays off until a backtest shows it adds signal over the current composite. Validate, then flip or drop. |
| Forecast stays retired | The 7-day predictor is mean-reverting with no learnable signal, so it is retired, not merely dormant. Revisit only with a model that beats naive persistence on the accuracy-watchdog backtest gate. |
| Intelligence Core reconcile promotion | Reconcile runs in shadow (`RECONCILE_ENABLED=true`, event_ledger + reconcile_actions live). Promote through the phases per docs/intel-core-activation-runbook.md after a clean window. |
| Weekly review ritual | Monday 09:00 SAST recurring. Refresh this file's "Last reviewed" stamp and prune resolved rows at that review. |

## Reading this doc

- **Stakeholder ask "where are we"** → README for the system, this file for open decisions.
- **Decision required** → add a row to "Outstanding decisions" with its forcing function.
- **Decision resolved** → delete the row (git keeps the history); do not let this table grow stale.
