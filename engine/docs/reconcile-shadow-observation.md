# PULSE Intelligence Core: daily shadow observation checklist

Companion to `docs/intel-core-activation-runbook.md` (the one-time activation
steps and the Step 4 promotion criteria). This is the day-to-day routine for
the ~10-cron-day observation window: what to run each morning, what a healthy
day looks like, and what to do when it is not.

Nothing here renders anything or changes cron behaviour. Every command below
is read-only.

## Morning routine (after the 00:30 UTC primary + 02:30 UTC fallback)

1. `python scripts/reconcile_shadow_digest.py --date <today>`
   Per-market ledger event count, the resolved/scheduled/unknown state
   split, and reconcile action counts (stale / corroborated / labelled /
   no_match) with a sample of any corrected claim and its receipts. This is
   the fast daily glance; wire it into `morning-check` if not already there.
2. If the `unknown` share of ledger events is high (the 2026-07-04 baseline
   ran 100% `unknown` on every kept-but-not-graduated claim, see the
   graduation pack), that is expected during the observation window while
   Phase 2 state-pattern coverage settles. A rising `unknown` share over the
   window, rather than a flat or falling one, is the signal something
   regressed (a new headline shape the deterministic patterns do not cover,
   or a GDELT-blob leak past `_clean_evidence_quote`).
3. `python scripts/reconcile_coverage_report.py --date <today>` for the
   deeper per-claim "why" when the digest alone does not explain an action.
   Run this on Cloud Run / CI, not this local Windows+Py3.13 box past a
   small row count (see the script's docstring and
   `docs/reconcile-graduation-pack-2026-07-04.md`'s methodology note for the
   segfault gotcha).

## What a clean day looks like

- Every `stale` correction's `claim_after` reads as a real, coherent
  sentence (never a GDELT theme-code blob) and its receipts resolve to real
  channel families.
- No correction inverts a true claim into a false one (the human sign-off
  the runbook's Step 4 requires; spot-check every `stale` row, there are
  rarely more than a handful per market per day).
- The SA-vs-SK golden case (`tests/unit/test_reconcile.py`) and the live-
  shaped fixtures (`tests/unit/test_reconcile_ledger_integration.py`:
  Springboks vs England, Super Eagles, Ramaphosa) still pass in CI; a red
  build on either file is a stop-the-window signal, not a log-and-continue
  one.
- Cost stays under the runbook's +$15/mo ceiling (`vertex-cost-watchdog`).

## When something looks wrong

- A `stale` correction that reads wrong: capture the `trend_date`, `market`,
  `matched_entity_key`, and the row from `reconcile_actions`, then check
  whether `_correction_allowed` (2+ factual families, confidence >= 0.55)
  legitimately passed on thin evidence. If the gate itself is not the
  problem, the ledger's `state_text` is; trace it back through
  `event_ledger.build_event_ledger` for that entity_key.
- A spike in `no_match`: check whether `_reconcile_claims_for_market` or
  `_filter_claims_to_ledger` in `scripts/run_rss_now.py` changed, or whether
  the day's briefs simply carry fewer source-backed reference titles.
- Any of the above resets the ~10-day clean-day count per the runbook; do
  not promote on a partial pass.

## Promotion

See `docs/intel-core-activation-runbook.md` Step 4 for the full promotion
criteria (10 clean cron days, golden case green, zero receipt failures, cost
under ceiling, human sign-off on zero true-to-false inversions). This
document is the daily habit that earns that sign-off; it does not replace it.

## Phase 4 observation window

Window start: 2026-07-04 (PR #241 merged 2026-07-04T17:45 UTC; Cloud Build
deploy follows master push; day 0 is the first 00:30 UTC cron after deploy).

Day 0 baseline: PR #241 merged to master. Shadow reconcile unchanged:
`RECONCILE_ENABLED` still shadow (writes `event_ledger` + `reconcile_actions`,
renders nothing). Pre-merge probe: 0 `no_match` after claim prefilter; ~36%
of kept claims graduate past `labelled`. See
`docs/reconcile-graduation-pack-2026-07-04.md`.

Daily checklist (after 00:30 UTC primary + 02:30 UTC fallback):
1. `python scripts/reconcile_shadow_digest.py --date <today>`
2. Optional deep dive: `python scripts/reconcile_coverage_report.py --date <today>`
3. Spot-check every `stale` row in the digest

Promotion: 10 clean cron days per `docs/intel-core-activation-runbook.md`
Step 4 (golden case green, zero receipt failures, cost under ceiling, human
sign-off on zero true-to-false inversions). A broken day resets the count.
