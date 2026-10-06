# Trends Engine V3 Blueprint Audit

Audited: 1 Jul 2026 against `master` (commit `969c4fd`, PR #218 merged same day). Original blueprint: [`trends-engine-v3-blueprint.md`](trends-engine-v3-blueprint.md). Corrected successor: [`trends-engine-v3-blueprint-v3.1.md`](trends-engine-v3-blueprint-v3.1.md).

## Executive scorecard

The v3.0 blueprint is one of the strongest strategy docs in this repo: reuse-first, file-grounded, shadow discipline, and an honest split between loudness and defensibility. The Intelligence Core code it points at (`corroboration.py`, `reconcile.py`, `event_ledger.py`) is real, tested, and mostly matches the prose.

Five load-bearing factual errors and one internal cost contradiction would mis-route Phase 0 decisions if left unfixed. The snapshot is also stale: PR #218 re-enabled `RECONCILE_ENABLED` on 1 Jul 2026 while the blueprint still reads "paused awaiting restart."

**Strengths:** corroboration two-numbers law, reconcile as pure deterministic boundary, writer-first LP handshake, thin-day digest framing, pattern-detector backtest gate cloned from forecast failure, WPP compliance boundary.

**Factual errors:** ledger state enum, Gemini call cardinality (Section 10 vs code), `schema_version` presented as existing, ensemble ledger framed as net-new, `vertex-cost-watchdog` implied as deployed script.

**Staleness:** PR #218 reconcile ON, creator tier_2 trims (PR #217), Semrush dark (PR #213), Cloud Build sole deploy path (28 Jun), test count drift.

**Scope risk:** Phase 4 bundles four heavy subsystems in one phase; grounding verifier cost understated relative to ledger-only shadow.

## Finding table

| Severity | Blueprint section | Claim | Verdict | Evidence | Fix in v3.1 |
|---|---|---|---|---|---|
| BLOCKER | §4, §8, §10 | Ledger adds one Gemini call per (market, watched-entity) | FALSE | `_resolve_with_gemini` is one batched call per market (~3/day). Docstring: "One Gemini call over the clusters" (`src/analysis/event_ledger.py` ~374, `src/analysis/prompts/event_ledger.py` ~3) | Replace with 3 batched calls/day; separate grounding verifier as dominant cost once live |
| MAJOR | §4 Stage 2 | Ledger states: scheduled, ongoing, resolved, evergreen | FALSE | Code emits `resolved`, `scheduled`, `unknown` only. `ongoing` is dead branch in `reconcile.py`; `evergreen` never assigned | Fix enum; plan `ongoing`/`evergreen` only if enum extended deliberately |
| MAJOR | §8, §10 | `render_payload` carries `schema_version` today | FALSE | Zero hits in `src/`; payload is `{"display": {...}}` only (`generate_briefs.py` ~215) | Mark as planned build item |
| MAJOR | §3, §8 Phase 0 | Unified EnsembleData token ledger is new work | PARTIAL | `_global_units_spent`, `DEFAULT_BOOST_BUDGET` exist (`ensemble.py` ~37, ~418). Gap: pre-call gate uses `DEFAULT_ESTIMATED_UNITS_PER_CALL = 1` (~484) | Reframe as harden existing ledger + vendor-truth cross-check via `fetch_units_history` |
| MAJOR | §7, §8 | Both cron paths read `cron_flags.env` (GitHub Actions fallback) | STALE | Cloud Build sole deploy since 28 Jun (`cloudbuild.yaml`, `test_workflow_env_parity.py`). Two Scheduler triggers, one Cloud Run job | Cloud Build + dual Scheduler wording |
| MAJOR | §8 Phase 0 | Shadow paused; resume gated first | STALE | PR #218 merged 1 Jul 2026: `RECONCILE_ENABLED=true` on master | Document live un-gated shadow + 7-day cost watch |
| MINOR | §9 | ~1550 tests / ~70 files | CLOSE | Actual: 1579 tests / 76 files (1 Jul 2026) | Update counts |
| MINOR | §2, §10 | 14 hardcoded `trends_v2_dev` in accuracy_watchdog | TRUE | Exact count in `scripts/accuracy_watchdog.py` | Keep; Phase 0 fix via `get_dataset()` |
| MINOR | §9, §10 | `vertex-cost-watchdog` $30 RED tripwire in code | PARTIAL | No `scripts/vertex_cost_watchdog.py`. $30 threshold in morning-check runbook manual procedure | Label as morning-check cost gate, not deployed script |
| MINOR | §8 | PR #201 title exact quote | UNVERIFIED | `gh pr view 201` title: `chore(cost): pause reconcile shadow + revert brief-cap raise`. No "brief-cap" elsewhere in repo | Keep title if citing gh; drop uncorroborated brief-cap detail from narrative |
| INFO | §5 | LP lexical search, grounded chat, render_payload | TRUE | Listening Post `bq.py`, `chat.py`, `desk.py` verified | No change |
| INFO | §4 Stage 1, Stage 3 | Corroboration + reconcile behaviour | TRUE | `corroboration.py`, `reconcile.py` match blueprint thresholds | No change |

## Corrected cost model

**Event ledger (RECONCILE_ENABLED shadow, live since PR #218):** one batched Vertex Gemini call per market per cron run = **3 calls/day**, not one per watched entity. The runbook already states this correctly (`docs/intel-core-activation-runbook.md` Step 2: "one per-market resolution pass (3 calls per day)"). v3.0 Section 10 contradicts both the runbook and the code.

**Reconcile pass:** zero Gemini (`reconcile.py` has no genai imports).

**Grounding verifier (not built):** this is the real cost driver once it ships. Batched per topic, gated Key/Watch tier first, shadow before live. Budget **$8–15/mo** applies to ledger + reconcile observation window; add separate headroom for grounding once measured.

**Corroboration:** free every cron, already live.

## Live-state delta since v3.0 snapshot (2026-06-30)

| Change | PR / date | Impact on blueprint |
|---|---|---|
| `RECONCILE_ENABLED=true` | #218, 1 Jul 2026 | Phase 0 "resume shadow" is done; promotion clock should start |
| ZA/KE/NG creator tier_2 trimmed | #217, 1 Jul 2026 | tier_2 flip preconditions improved; still need 2–3 clean `fetch_units_history` days |
| Semrush connector dark | #213, 1 Jul 2026 | 9th connector exists; not in v3.0 connector count |
| RSS dead feeds fixed | #212 | Ingestion reliability improved |
| Cloud Build sole deploy | 28 Jun 2026 | "Dual-path" = two Schedulers, not GH Actions |

## What to keep unchanged

Reuse-first spine promotion (corroboration → ledger → reconcile → grounding verifier). Two-numbers law. Corrector-is-never-trust-boundary. Shadow discipline (~10 clean days before client-facing promote). Writer-first TEV2 before Listening Post reader. Thin-day digest header. Pattern detectors must beat persistence + clear degeneracy gate. No external vector store, no non-Vertex judge (WPP). `get_dataset()` routing as Phase 0 substrate fix.

## Open questions (reframed with corrected math)

1. **PR #218 posture:** shadow is ON un-gated. Default for v3.1: keep running, watch Vertex spend for 7 days via morning-check. If spend exceeds ~$15/mo ceiling, revert or add Key/Watch entity gate in code (not just docs).

2. **Grounding verifier tier gate:** default Key-tier only at first shadow (tighter than full ledger entity set).

3. **BSA THE READ timing:** default internal ZA/NG/KE digest proof ~30 days before BSA sees reconciled read.

4. **First pattern detector:** default emergence (highest desk value); cross-market diffusion second.

5. **Forecast:** leave dark; event-conditioned calendar is optional future work, not V3 blocker.

## Verification performed

Four parallel code audits (Intelligence Core, substrate, Listening Post, git history) plus direct grep/count on `master`. Commands run during audit execution:

```bash
py -3.13 -c "..."  # 1579 tests / 76 files
gh pr view 184 --json title,mergedAt
gh pr view 201 --json title,mergedAt
rg -c trends_v2_dev scripts/accuracy_watchdog.py  # 14
rg schema_version src/  # 0 hits
```
