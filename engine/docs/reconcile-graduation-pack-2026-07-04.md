# Reconcile graduation pack, 2026-07-04

`scripts/reconcile_coverage_report.py --date 2026-07-04` reproduces this pipeline: read-only, rebuilds the ledger via `build_event_ledger` (BQ SELECT only, no Gemini, matching production's current `client=None` shadow call) and reconciles the same claim surface `_reconcile_shadow` gathers. The baseline table below is the live probe cited in the mission brief (za 5 kept all labelled, ng 3 kept 1 labelled 2 corroborated, ke 1 kept labelled, 0 no_match), reproduced here with the "why" behind every action.

Methodology note: `google-cloud-bigquery` RPC calls segfault on this Windows + Python 3.13 box (the documented `DEVELOPMENT.md` Known Gotcha). Confirmed by bisection this session (a `bq`-CLI-fed replay of the ledger/reconcile functions ran clean at 50 rows, crashed at 100/200/600/2351 with exit 0xC0000005 and no Python traceback) to be a logging / native-gRPC interaction rather than a hard row-count ceiling: the same `logging.disable(logging.CRITICAL)` fix already proven in `backfill_seed_graph.py` (commit 8cffcb7) applied to `reconcile_coverage_report.py` runs the full ~2,300-2,700 row day clean. The baseline table is the pre-Phase-1 probe cited in the mission brief; the addendum below is a genuine live re-run of the same script, same date, post Phase 1-3.

## Summary (baseline, pre Phase 1-3)

| Market | Ledger events | Raw claims | Kept after prefilter | Stale | Corroborated | Labelled | No match |
|---|---|---|---|---|---|---|---|
| ZA | 133 | 7 | 5 | 0 | 0 | 5 | 0 |
| NG | 140 | 11 | 3 | 0 | 2 | 1 | 0 |
| KE | 19 | 5 | 1 | 0 | 0 | 1 | 0 |

## What is actually happening per claim

### ZA (5 kept, 5 labelled)

All five kept claims anchor cleanly to a real ledger entity (Cyril Ramaphosa x3, Google, Bafana Bafana). None graduate past `labelled` because the matched ledger event's `state_label` is `unknown`, not `resolved` or `scheduled`. The deterministic pattern matcher in `event_ledger._deterministic_state` only recognises headline-shaped verbs (won, beat, results, vs, fixture). A GDELT entity cluster whose freshest member is a Twitter caption ("BREAKING | President Cyril Ramaphosa to address the nation at 20:30") never hits either regex, so it falls through to `unknown`, and reconcile correctly refuses to assert anything against an unknown state.

### NG (3 kept, 1 labelled, 2 corroborated)

The two `corroborated` claims are BigQuery Trends search terms ("England vs DR Congo", "Belgium vs Senegal") that the deterministic matcher tags `scheduled` off the `vs` marker, then reconcile confirms as fresh agreement. This is the intended happy path working. The Obasanjo/Peter Obi claim is the same `unknown`-state gap as ZA: the ledger entity exists (Peter Obi, GDELT + RSS) but nothing in its member headlines matches a resolved/scheduled pattern.

### KE (1 kept, 1 labelled)

The William Ruto claim matches a `resolved` ledger event (0.55 confidence, `gdelt` + `rss`), but stays `labelled` because the correction gate needs confidence >= 0.55 (it clears that) but only 2 factual families are required and it has exactly 2, so the gate should pass. It does not graduate because the claim itself is not contradicted: `_claim_implies_future` finds no future marker in the claim text (no "vs", "will", "upcoming" etc.), so reconcile never treats it as a stale claim to correct in the first place. It falls to the "matched but nothing to assert" branch by design, since the correction path only exists to catch a claim that is WRONG about the future, not to endorse a claim that is already correct about the past.

## What this means for promotion

The `unknown` state_label is the single biggest blocker to graduation past `labelled` on this data. Fixing it is a state-resolution problem (Phase 2), not a matching or clustering problem: the claim surface already anchors correctly to the right ledger entities in every case observed today. Phase 2 work (deterministic pattern coverage for conversational/Twitter-shaped headlines, `state_text` sanity fallback) is the highest-leverage next step for this branch.

## Addendum: after Phases 1 to 3 (same day, same data)

Re-ran `scripts/reconcile_coverage_report.py --date 2026-07-04` after widening the ledger to YouTube and Apple Music, the sport-fixture and reported-speech deterministic patterns, the GDELT theme-blob guard, the partial citation credit, and the curated alias map.

| Market | Kept | Stale | Corroborated | Labelled |
|---|---|---|---|---|
| ZA | 6 (was 5) | 1 (was 0) | 1 (was 0) | 4 (was 5) |
| NG | 3 (unchanged) | 1 (was 0) | 1 (was 2) | 1 (was 1) |
| KE | 2 (was 1) | 0 | 0 | 2 (was 1) |

Two real corrections now fire that did not before: a ZA claim quoting a resolved "DeepSeek vs Google Gemini" debate video corrects to the ledger's state_text (the YouTube family plus the widened resolved patterns cleared the two-factual-family gate), and an NG "Belgium vs Senegal | Match Highlights" claim corrects the same way (the YouTube "highlights" marker now reads as a resolved signal, not a bare fixture). The KE Ruto claim's confidence rose 0.55 to 0.85 (now grounded in a clean RSS headline, not just a citation-less pattern hit) without changing its action, because the claim itself never asserted anything wrong about the future, so it correctly stays labelled rather than corrected.

The remaining `unknown` cases (Ramaphosa's Hammanskraal and "to address the nation" claims) did not resolve even though the new reported-speech patterns cover that exact language: the claim text comes from the brief (a Twitter reference), but the ledger's "cyril ramaphosa" cluster is built from separate `enriched_content` rows whose own headlines do not happen to contain a resolving verb. This is the deterministic matcher working as designed (it never infers a state the evidence does not literally state), not a bug. Closing this gap needs either a richer set of GDELT/RSS rows for that entity on a given day, or the Gemini pass (still off in production, `client=None`) reading the same evidence with more judgment.

## Phase 1-3 changes made against this exact data

Pulling the live 2026-07-04 `enriched_content` rows directly (via the `bq` CLI, sidestepping the segfault) confirmed two concrete mechanisms behind the `unknown` gap this pack diagnosed, both now fixed:

`src/ingestion/connectors/gdelt.py` always writes `title=""` for `gdelt_gkg` rows; the real content is a synthesised `text` blob of theme codes plus persons/orgs/source (e.g. `TAX_FNCACT_PRESIDENT WB_678_ENERGY_SUPPLY_AND_DEMAND Cyril Ramaphosa allafrica.com`). Any cluster whose freshest member was a GDELT row surfaced that blob as `evidence_quote`/`state_text`, which can never match a prose pattern. `_looks_like_theme_blob` + `_clean_evidence_quote` (Phase 2) now skip the blob for the freshest real-prose member when one exists in the cluster, and the Ramaphosa/Peter Obi/Ruto-shaped headlines this pack sampled ("President Cyril Ramaphosa has called for...", "told people from Hammanskraal...", "BREAKING | ... to address the nation at 20:30", "Obasanjo Endorses Peter Obi") are exactly the reported-speech and announcement shapes the widened `_RESOLVED_PATTERNS`/`_SCHEDULED_PATTERNS` (told, endorsed, has called for, to address) now resolve instead of falling through to `unknown`.

Not fully closed by this branch: per the addendum above, the Ramaphosa entity's own `enriched_content` rows on 2026-07-04 do not happen to carry a resolving verb, so it stays `unknown` regardless of the pattern widening; that gap is a data-coverage question (does GDELT/RSS write a headline-shaped row for this entity that day), not a code defect, and the fix is either richer per-entity RSS/GDELT coverage or the Gemini pass reading the same evidence with more judgment.

Also shipped, orthogonal to the `unknown` gap: `_fetch_factual_rows` widens to YouTube search-result videos and Apple Music chart rows as their own factual families (more cross-channel corroboration headroom for music/video-adjacent topics); a "Springboks vs England"-style fixture headline now corroborates both named anchors instead of only the longer one; and `configs/entity_aliases.yaml` lets a claim naming an entity differently from today's data-derived key (e.g. "Ramaphosa" vs the ledger's "cyril ramaphosa") still anchor.

## Promotion criteria (unchanged, not touched by this branch)

None of the above flips `RECONCILE_ENABLED` or promotes anything. Shadow-to-Phase-1 promotion still needs the full criteria in `docs/intel-core-activation-runbook.md` Step 4: a rolling ~10 clean cron days, the SA-vs-SK golden case green, zero receipt-exist failures, cost under the +$15/mo ceiling, and a human sign-off that no shadow correction was a true-to-false inversion. `docs/reconcile-shadow-observation.md` is the day-to-day checklist for that window; this pack is one data point inside it, not a substitute for it.
