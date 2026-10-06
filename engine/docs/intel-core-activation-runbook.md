# PULSE Intelligence Core: shadow activation runbook

This is the supervised, human-run procedure to turn on the Intelligence Core
SHADOW. It is the live step that the activation-prep PR deliberately did NOT do.
Nothing here runs automatically. Run each step by hand, in order, and watch the
proof before moving on.

What shadow means: with `RECONCILE_ENABLED=true` the cron builds the event
ledger and reconciles each market's brief claims against it, then persists ONLY
an audit trail to `reconcile_actions`. It renders NOTHING into any email or
dashboard and mutates no brief. The output is observe-only. Promotion to Phase 1
(where reconcile labels actually surface) is a separate, later decision gated on
the criteria at the bottom of this doc.

Engine safety: until the flag is flipped, cron behaviour is byte-identical to
today. Applying the migrations alone changes nothing the cron reads.

## Step 1: apply the three migrations

Idempotent and additive. They create the two tables and add the reconcile
columns on `trend_analysis`. Re-running them is safe (each uses
`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`). Run against the live
project with application-default credentials:

```
python scripts/migrations/add_event_ledger_table.py --apply
python scripts/migrations/add_reconcile_actions_table.py --apply
python scripts/migrations/add_reconcile_columns.py --apply
```

Without `--apply` each script is a dry print of what it would do. Only `--apply`
touches BigQuery. After this step the `event_ledger` and `reconcile_actions`
tables exist and are empty, and `trend_analysis` carries the reconcile columns.
The cron still reads none of them, so behaviour is unchanged.

## Step 2: flip the flag (a reviewed PR)

`configs/cron_flags.env` is the single source of truth for cron flags. Both cron
paths (the primary Cloud Run job and the GitHub Actions fallback) read this one
file, so flip it here, never on the job with gcloud.

Change the one line at the bottom of the file:

```
RECONCILE_ENABLED=false
```

to

```
RECONCILE_ENABLED=true
```

Open it as its own PR, get it reviewed, merge. The configs path filter triggers
a cron-image rebuild, so the live job picks the flag up on the next deploy. This
is the cron behaviour change.

Cost note: the flag adds Gemini calls. The ledger runs one per-market resolution
pass (3 calls per day) and reconcile runs at most one pass per market topic
(<=24 calls per day). Typical run-rate is about +$8/mo, with a +$15/mo ceiling
on a heavy-event day. The Core still renders nothing while shadow, so this buys
observation, not output.

## Step 3: watch the proof after the next cron

After the next 00:30 UTC cron fires with the flag on, confirm the shadow wrote
data and the new query is healthy:

1. `event_ledger` and `reconcile_actions` populate for the run's `trend_date`
   (non-zero rows across the three markets on a normal event day; a quiet day
   can legitimately be sparse).
2. `python scripts/dry_run_sql.py` passes, including the new
   `event_ledger._fetch_factual_rows` entry (it should report OK, not FAIL).
3. `python scripts/reconcile_shadow_digest.py --date <today>` prints the
   per-market summary: ledger events with state samples, and reconcile actions
   with any corrected claim and its receipt sources. Before the first populated
   cron it prints the no-data line and exits 0, which is the expected
   pre-activation result.

Read the digest each cron day through the window. The thing to eyeball is every
`stale` correction: confirm the corrected text and its receipts are right and
that no correction inverts a true claim into a false one.

## Step 4: promotion criteria (shadow to Phase 1)

Promote the Core from shadow to Phase 1 (labels rendered) only after a rolling
~10 cron days that are all clean:

- the watchdog trust checks pass every day,
- the SA-vs-SK golden case resolves correctly,
- zero receipt-exist failures (every cited receipt resolves to a real source),
- cost stays under the +$15/mo ceiling,
- a human sign-off that no shadow correction over the window was a true->false
  inversion.

Miss any of these and the window resets; do not promote on a partial pass.

## Rollback

Flip `RECONCILE_ENABLED` back to `false` in `configs/cron_flags.env` as one PR.
The cron returns to byte-identical baseline on the next deploy. The two tables
and the `trend_analysis` columns are harmless if left in place (nothing reads
them with the flag off), so there is no need to drop anything to roll back.
