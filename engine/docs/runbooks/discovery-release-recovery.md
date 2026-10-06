# Discovery release recovery

Operating instructions for the repeatable discovery chain: how a released run is
rolled back safely, what each refusal means, and which rows of the source estate are
still unresolved. The chain is collect, capture, compose, certify, release. Only the
release script writes a display flag, and it writes it inside one transaction. No
recovery path replaces that transaction with an update of a ready flag.

## Safe recovery

Rollback selects an earlier release. It never edits evidence so that an older image
accepts it, and it never deletes or changes a newer run, result or prediction row.

1. Name the target: the image the service is rolling back to (a 40 character
   `source_sha`), the run contract versions that image reads, and the source policy
   digests it accepts. Take them from the image's own release notes or profile, never
   from the runs you hope to select, and never widen them to make a run fit.
2. Call `read_rollback_selection` in
   `engine/scripts/staging/release_open_intelligence_run.py` with the current run id, a
   staging query runner and those three sets. It reads, and only reads:
   `open_intelligence_quality_release_records_v2` under the current release contract,
   the `open_intelligence_run_receipts_v1` row of each released run, and the approved
   profile registry `RELEASE_PROFILES`. The release record carries `run_id` and
   `released_at` but no source policy digest and no cutoff, so the policy is read from
   the profile that names the run and the cutoff is the receipt's `observation_end`. A
   run no profile names, the retained R3 replay among them since its profile carries no
   policy digest, has an unknown policy and is never selected. A run whose receipt is not
   `enabled` is never selected. Every statement passes a guard that refuses with
   `rollback_write_refused` when the raw text carries a `;` or a triple quote, or when,
   once single quoted literals and backtick identifiers are removed, what is left does
   not start with SELECT, carries a double quote, a stray single quote or a comment
   marker (two hyphens, `#`, `/*` or `*/`), or names a write keyword (INSERT, UPDATE, DELETE,
   MERGE, CREATE, DROP, ALTER, TRUNCATE, CALL, BEGIN, COMMIT, EXECUTE) standing apart
   from any letter, digit, hyphen or underscore. A run id such as `r4-update-fix` sits
   inside a quoted literal and is read as data.
3. The selection is the newest release by `released_at` that is earlier than the
   current one, whose cutoff is not later, and whose recorded image, run contract and
   source policy are all in the target's sets. Every release at or after the target other
   than the target itself is retained, so a release tied with the target on `released_at`
   is never dropped. For the retained runs it reads back, and only reads back, the
   `run_id` and `prediction_id` of each prediction in `signal_predictions_v2`, and the
   `run_id`, `prediction_id` and `outcome_id` of each row in `signal_outcomes_v2` that
   scores one of those predictions. Outcomes are read by `prediction_id` because an
   outcome's `run_id` is its evaluation run, not the discovery run. `evidence_edits` is
   always empty and `served` is always false.
4. The selection is not served, and no existing write can serve it. The served run is
   whatever `fetch_dynamic_run_receipt` in `app/src/api/bq.py` reads. It takes one row,
   ordered by `observation_end` descending, then `completed_at` descending, then `run_id`
   ascending, from the receipts that pass every one of these filters:
   `run_contract_version` equal to the single contract `open_intelligence_run_receipt_v1`;
   `client_scope_id` other than `qa_canary`; `status` equal to `completed`;
   `complete_partitions` true; `display_release_state` equal to `enabled`;
   `observation_start` not after `observation_end`; `signal_date` equal to
   `observation_end`; `observation_end` before `CURRENT_DATE('Africa/Johannesburg')`;
   and the market scope filter, which for one market requires that market in
   `market_scope` and for `all` requires `za`, `ng` and `ke` all in `market_scope`. The
   selection orders by `released_at` and applies none of those filters. It accepts a set
   of run contract versions while the served reader accepts exactly one, so a selection
   on any other compatible contract can never be served by that reader. The two orders
   can also disagree, and a run the selection names may be one the served reader would
   never show. A future served pointer must reconcile all of this before it serves a
   selection. The release write enables a blocked
   run only, and refuses a run already released (`release_already_released`,
   `release_duplicate`), so it cannot move display to an older release. Blocking the
   newer receipts would edit newer rows, which this recovery forbids. Moving display is
   therefore open; see the unresolved rows below. Until it closes, a recovery ends at a
   recorded selection and an owner decision.
5. Read the selection back before acting on it. A rollback is proved by the readback,
   not by the command exiting zero.

Refusals during selection, each of which stops the recovery rather than widening it:

| Code | Meaning |
|---|---|
| `rollback_record_invalid` | A released record is malformed, duplicated or has no run receipt. Repair the record, never the filter. |
| `rollback_current_release_unknown` | The named current run is not a released run. |
| `rollback_no_compatible_release` | No earlier release is compatible. There is nothing safe to fall back to. |
| `rollback_compatibility_invalid` | A compatible image, run contract or source policy set is malformed. |
| `rollback_write_refused` | The rollback path was asked to issue a statement other than one SELECT. |

Refusals during release, which a recovery must read before retrying anything.

Eight of the codes below are dropped by the retained replay. The refusal is raised on
the same condition whatever the profile, but the code is carried only for a live one,
so a recovery reading a replay refusal reads its message rather than a code. Seven are
raised through `_live_code`, which returns no code at all when the profile is the
retained replay. The last, `release_profile_identity_differs`, is that same gate
written out by hand: one condition, a replay branch refusing with no code, and the
coded refusal for a live profile after it. It was marked always here until the
derivation was widened to read the enclosing replay guard as well as the helper. Every
other code below is carried whenever its refusal is raised. `release_profile_unknown`
is carried before any profile has been resolved, and `release_profile_invalid` is what
the retained replay raises when it is driven through the live path, so neither is live
gated. The `Carried` column says which is which, and it is derived from the release
script rather than asserted here.

Carriage is not reachability, and for most of the codes marked always the mark is true
only vacuously. Seven of them are never evaluated under the retained replay at all,
because every raise sits inside a `not profile.is_replay` block, or inside a helper
whose only call sites do: `prediction_enrollment_differs`,
`prediction_enrollment_incomplete`, `release_independence_policy_differs`,
`release_membership_changed_after_certification`, `release_profile_cutoff_differs`,
`release_profile_digest_differs` and `release_source_authority_incomplete`.

The remaining four are genuinely carried and genuinely evaluated whatever the profile:
`release_profile_unknown`, `release_profile_invalid`,
`release_profile_adapter_unavailable` and `release_generation_pair_differs`. Of those
four the last is evaluated under the replay and still cannot raise there, for a reason
of its own rather than a profile guard: `_require_profile_generation` returns before the
comparison when the profile names no generation pair, and the only registered profile
names none. The command line cannot select a live profile at all; see the unresolved row
on selectable profiles below.

| Code | Carried | Meaning |
|---|---|---|
| `release_profile_unknown` | always | The named profile is not registered. |
| `release_profile_invalid` | always | The profile is malformed, or the retained replay was driven through the live path. |
| `release_profile_identity_differs` | live profile only | The profile identity does not match the receipt. |
| `release_profile_cutoff_differs` | always | The profile cutoff does not match the receipt. |
| `release_profile_digest_differs` | always | The profile digest changed after the run was certified. |
| `release_profile_adapter_unavailable` | always | A live profile was driven without its compose, certify and release adapter. |
| `release_generation_pair_differs` | always | The profile generation pair is not the active pair. |
| `release_independence_policy_differs` | always | The run was evaluated under another readiness independence policy. |
| `release_membership_changed_after_certification` | always | Membership moved after certification. |
| `release_canary_namespace` | live profile only | A canary namespace never releases for display. |
| `release_source_authority_incomplete` | always | The admitted capture entries do not cover the run. |
| `release_run_not_completed` | live profile only | The run receipt does not report a completed run. |
| `release_partitions_incomplete` | live profile only | The run receipt does not prove complete partitions. |
| `release_already_released` | live profile only | The run is not blocked, so it was released already. |
| `release_run_empty` | live profile only | The run has no candidate, evidence, membership or displayable prediction row. |
| `release_evidence_differs` | live profile only | The blocked run receipt differs from the durable release evidence. |
| `release_duplicate` | live profile only | A release record already exists for the run. |
| `prediction_enrollment_incomplete` | always | The run has nothing to enroll, or the profile declares no prediction rules. |
| `prediction_enrollment_differs` | always | The prediction rows do not match the promoted signals the release would display. |

A duplicate release of one run refuses, and a run whose receipt changed between the read
and the release refuses. Neither is retried by repeating the command.

## Parity enrollment

`engine/src/analysis/open_intelligence/parity_enrollment.py` holds the enrollment record.
One record binds one legacy run and one current run of the same client scope, market
scope, cutoff and source window, and it carries both run receipts, so a record proves its
own two receipt digests rather than restating them. Identity is the run receipt digest,
never the run id: a run id is a restatable name, and a receipt restated under another name
is refused as the same run instead of accepted as its own counterparty. The pair key is the
two receipt digests in sorted order, so one pair binds one record whichever way round it is
enrolled. The record carries no measured accuracy.

A record is meant to bind two chains, and the pairing does not prove that. The gate
`parity_chains_identical` refuses two receipts whose `observation_method` strings are
equal, and that is all it compares. The run receipt contract validates that field as a
nonempty string and nothing more, the retained replay writes the constant
`dynamic_source_copy_apply_v1`, and the composer's receipt inputs helper takes the string
from its caller with `daily_source_snapshot_compose_v1` as the default. No registry of
chain names exists, so two receipts of one chain carrying `dynamic_source_copy_apply_v1`
and `dynamic_source_copy_apply_v2`, as two runs either side of a version bump of the
method string would, enrol as two chains, and a record whose two row set digests agree
then reports `parity_outputs_identical` true, which reads as the legacy chain and the
current chain agreeing when it is one chain compared with itself. The gate closes only
the case where the string is byte equal: two retained replay receipts differing only in
run id and completion instant refuse. The only thing that keeps the wider case off the
shipped replay is that its run id happens to be a constant, which is an accident and not
a rule. The cluster build version cannot carry the gate either, because two builds of one
chain differ in it.

`eligible_run_count` counts distinct cutoff and source window pairs, so repeated executions
of one source window count once however often the chain ran. It does not key on
`source_window_digest`. Two producers in this repository write that digest onto a run
receipt, and they compute it two different ways. The daily composer carries the captured
snapshot's own self digest, and the snapshot it hashes carries the capture instant and a
snapshot id derived from that instant, so a second capture of byte identical source yields
a different digest and the count would grow on a repeat. The retained replay carries a
sha256 over the copy run id and the per table manifest and matched row counts, with no
capture instant in it, so a repeat under one copy run id does not move it. The digest is
also recomputed outside this tree, by two BigQuery routines, and is already stored on
released rows, so its meaning cannot be changed here either way. The desk view does not
recompute it: it joins the quality release record to the run receipt on the column and
embeds the column in the preimage it rebuilds `run_receipt_digest` from, which is why a
redefinition would invalidate stored digests without any routine being touched. The
enrollment derives its own content key instead, from the client scope, the market scope and
the closed observation window, and that key cannot move when one window is captured again. Two
genuinely different captures of one window therefore count once, which understates rather
than overstates the independent evidence.

### The enrollment cannot be built across the two chains

Two chains write `source_window_digest` onto a run receipt, and only one of them ships
and runs. `replay_open_intelligence` is imported by the release script, the execution proof
issuer, the scoring qualification and several other shipped modules. The daily composer is
imported by nothing outside tests: `build_native_composer`, `composition_receipt_inputs` and
`build_native_persist` are named only by `engine/tests/unit/test_daily_composer.py`,
`engine/tests/unit/test_daily_composer_persistence.py` and the parity enrollment test, and
the only other mention of `daily_composer` in `engine/src`, apart from the enrollment
module's own docstring, is a mutex identifier string in `persistence.py`. The shipped
daily wiring binds the compose stage's composer and persist clients to refusing stand ins,
`daily_native_clients.UNBOUND_CLIENTS`, so the composer is reached from no shipped path.
Calling these two real producer chains is generous: one chain ships and runs, the other
exists only in tests.

No parity record can be built from them as the tree stands, and the reason is structural
rather than operational. The retained replay writes observation method
`dynamic_source_copy_apply_v1` and carries the copy completeness digest. The daily
composer writes observation method `daily_source_snapshot_compose_v1` and carries the
snapshot self digest. The pairing gate `parity_source_snapshot_differs` requires the two
receipts to carry the same `source_window_digest`, and two sha256 values over different
preimages are equal only on a collision, so that gate refuses every pair the two chains
can present. The same root refuses a composer receipt read through the live reader, whose
`_source_copy_authority` recomputes the replay formula and rejects any receipt that does
not carry it; that refusal predates this slice but it has the same cause.

A record is enrolled from two released runs, so the pairing gate is not the only thing in
the way. Release reads its quality review receipt through
`sp_read_open_intelligence_quality_review_receipt_v1`, which recomputes the copy
completeness formula over the admitted copy manifest and asserts the recomputed value
equals the stored `source_window_digest`;
`sp_register_open_intelligence_quality_review_receipt_v1` asserts the same on write; the
live quality path ties the review receipt's digest to the run receipt's through
`validate_review_receipt`; and `v_desk_dynamic_signals_v2.sql` joins the release record to
the run receipt on the column. A composer run receipt carrying the snapshot self digest
therefore fails those asserts and can never be released, and what is never released can
never be enrolled. Options one, two and three below all leave the composer's receipt
carrying the snapshot self digest, so none of them by itself produces a record: each
silently assumes a releasable composer run that the release path will not admit.

The first parity record therefore needs one of these decisions, and none of them belongs
to the enrollment module:

1. Pair on the observed window rather than on the stored digest. Drop the
   `source_window_digest` equality from the pairing and keep the client scope, the market
   scope and the closed observation window, which is already the key the count uses.
   Nothing stored changes and no contract moves. The pairing gets weaker: two chains that
   observed one window through genuinely different source material would pair, and only
   the window would prove they belong together.
2. Carry a second, shared field on the run receipt, computed the same way by both chains
   over the window both observed, and pair on that. Nothing stored is redefined, but the
   run receipt contract gains a field, its three schemas gain a column, and every producer
   and reader of a receipt has to write and read it.
3. Bind the correspondence outside the receipt. An enrollment input names the copy run and
   the snapshot proved to cover one window, produced by whatever runs both chains, and the
   pairing reads that input instead of comparing two stored digests. Nothing in the receipt
   changes and the proof moves into that input, which then needs its own authority.
4. Leave the enrollment unbuildable and say so. That is what this tree does today, and it
   is honest as long as nobody reads an empty enrollment as parity, which
   `parity_outputs_identical` already refuses to allow.
5. Have the composer chain compute the copy completeness formula that already exists,
   over the window it observed, and carry that as its `source_window_digest`. This is the
   only one of the five under which a composer receipt passes the digest asserts named
   above, and it is not a redefinition of the stored column: the stored definition and
   every assert stay exactly as they are. For the digest itself it needs no schema change,
   no new receipt field and no view change. It is not a value supplied at the persist
   seam. `daily_composer.build_native_persist` takes its run receipt inputs as a caller
   supplied callable and checks only the key set, but the composer's own inputs helper
   `composition_receipt_inputs` reads the digest out of `CompositionFacts`, which
   `NativeComposer.__call__` fills from `result.source_snapshot_digest`; the composer
   takes a capture entry and a cutoff and nothing else, while the formula takes the copy
   manifest completeness rows and a copy run id. Carrying the formula therefore changes
   the composer, or replaces its inputs helper with one that computes the formula outside
   it. Nor does it by itself produce a releasable composer run. The certify stage builds a
   live profile's release evidence through `_build_release_inputs`, which refuses any
   execution proof whose contract is not `r3-execution-proof-v1`, whose first argument is
   not `scripts/staging/replay_open_intelligence.py` or whose apply binding is not the
   `r3_apply` operation chain, and a composer run executes none of those. A live profile
   is needed as well: `RELEASE_PROFILES` registers only `r3` and adding one is a reviewed
   change, and the daily kernel's own live profile dispatches compose through the composer
   and persist clients that `daily_native_clients.UNBOUND_CLIENTS` binds to refusing stand
   ins. The read routine also requires exactly one copy manifest run over the receipt's
   observation window, so the composer's window and the copy manifest must be the same
   window, which is a question about how the chain is run rather than about the contract,
   and the answer is not taken here.

Redefining `source_window_digest` so the two chains agree is not on that list.
`engine/infra/bigquery_views/v_desk_dynamic_signals_v2.sql` joins on that column and rebuilds
`run_receipt_digest` from a preimage containing it, and three schemas declare the column
NOT NULL, so redefining it invalidates every stored run receipt digest. That is a durable
contract change and it belongs to the owner of the contract.

An empty enrollment is not a parity result. `parity_outputs_identical` refuses an empty
enrollment rather than reporting that every held record agrees, because the enrollment is
empty until a native run produces the first pair and an absence must never be read as
parity.

The module is a pure value. `enroll_parity_record` returns a new tuple, holds no store and
takes no lock, so it cannot serialise two writers that each read the same held view.
Whatever holds these records owns that uniqueness, and enforces it with a unique constraint
on the pair key and an agreement constraint from run id to run receipt digest, which are the
two rules `enroll_parity_record` applies inside one view. Evaluation tasks read these
records; they are not written by the release transaction.

## Unresolved rows

These are open at the time of writing and none of them is closed by code.

- No native run has happened from the build environment. The Google Cloud command line,
  a container daemon and the package download hosts are all unavailable there, so every
  capture, certification and release against staging belongs on the owner machine.
- Release of a live profile stays pending without the exact human authority and the
  separately approved recurring delegation. The adapter seam exists and refuses without
  it.
- Parity enrollment holds no records, and a native chain run would not produce one. The
  two producers carry `source_window_digest` computed two different ways, so the pairing
  gate refuses every pair they can present, and a composer receipt carrying the snapshot
  self digest cannot be released in the first place. Only one of the two chains ships and
  runs; the composer chain has no caller outside its own tests. See the section above for
  the five options and for why redefining the digest is not one of them.
- No live profile is selectable from the command line. `RELEASE_PROFILES` in
  `engine/scripts/staging/release_open_intelligence_run.py` registers only `r3`, the
  retained replay, so the profile argument can name only a replay or an unregistered name.
  Of the nineteen release codes tabled above, one, `release_profile_unknown`, is reachable
  that way. `release_profile_invalid` needs either a malformed profile object or the
  retained replay driven through `release_live_profile`, which the command line never calls.
  The remaining seventeen can only be raised once a live profile exists, whether because the
  raise sits inside a `not profile.is_replay` block, because the code is dropped under the
  replay by `_live_code` or by the same gate written out by hand, or because it is raised
  only on the adapter path. They are tabled because the code that raises them is shipped,
  not because the command line can reach them today. Registering a live profile is a reviewed change and is not made here.
- Release admission does not refuse a copy. `validate_released_run_admission` reconstructs
  the legacy R16 release with `evidence_authority` false and keeps
  `current_source_copy_validation` among the remaining checks, which
  `test_legacy_r16_chain_reconstructs_release_without_issuing_evidence_authority` asserts.
  Copy validation is open work, not a refusal already in place.
- The closed week human review material is not collected. The inherited coherence,
  duplicate and zero reviewed foreign leakage gates stay open, and no synthetic review
  closes them.
- The named monitoring tool roster is empty. The brief that names those tools is held
  outside this repository, so anything that depends on it stays refused.
- A rollback selection cannot be served. No served run pointer exists: the app serves
  the newest enabled run receipt, the release write refuses a run already released, and
  blocking a newer receipt would edit a newer row. Serving an older release needs a
  reviewed display selection record that the served reader honours, which is a durable
  contract and app change and is not made here. Until then a recovery stops at the
  selection.
- Today every rollback refuses with `rollback_no_compatible_release`. The source policy
  of a run is read from `RELEASE_PROFILES` in
  `engine/scripts/staging/release_open_intelligence_run.py`, which registers only `r3`,
  and the R3 profile carries a source policy digest of None. Every released run is
  therefore of unknown policy and none is ever compatible. This closes only when a
  reviewed live profile naming a released run and its policy digest is registered.
- The rollback selection has not been run against staging. It needs the owner machine,
  like every other native read.
- `docs/operations/source-estate.md` carries the short form of the recovery above and the
  source estate rows that are not qualified live. It is generated by
  `source_estate_document`, so its unresolved rows move only with the reconciliation.
  Every one of its source and market rows is unresolved at the time of writing: none is
  qualified live.

## Closed rows

Rows that were open when this runbook was written and have since been closed by a commit.
The record stays here so the history is kept and no test reads it as an open drift.

- The pinned linux boundary gate manifest was stale from the first commit of the parity
  enrollment branch. Adding `engine/src/analysis/open_intelligence/parity_enrollment.py` put
  a file inside the shipped engine bundle, whose file count moved from 503 to 504 while
  `ops/tests/fixtures/linux_boundary/manifest.json` pinned 503, and
  `test_engine_bundle_computation_is_deterministic_and_pinned` skips rather than fails when
  the computed bundle does not match the pin, so the drift was silent. Later merges moved
  the computed count to 506. Commit `9ed5cbe`, "fix(ops): repin the linux boundary gate
  manifest to the merged tree", on 20 September 2026 repinned the manifest at 506 with the
  bundle and closure digests recomputed, and commit `cccdfaa` the same day repinned the
  bundle digests again at the same count after the measured byte cap fix. The drift is
  closed. `ops/tests/test_linux_gate_manifest_drift.py` tied the open drift to this row and
  was written to fail on a repin as the signal to retire the row, so it was deleted when
  the row moved here.
