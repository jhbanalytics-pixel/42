# Independent review: Gamma 3.1 package (RULES.md rule 11)

Reviewed 16 October 2026 against branch `gamma/research-20261016-407cj1` (head 6a7f90c, two untracked files). Nothing under review was edited. Tests run: `core/setup/tests/test_history_audit.py` plus `test_inventory.py`, 27 passed (14 + 13), matching the plan's G0 line.

## Verdict: pass with fixes

The package is unusually honest about what the data cannot support, the code citations are accurate to within a few lines, the commit ids and dates check out, and the audit script is genuinely read-only with real caps. The problems are concentrated in (1) a G0 claim the branch does not back, (2) experiment definitions that are not yet leak-free or frozen in one place, (3) a prototype that in a few spots draws measurements 42 does not have, and (4) ownership gaps against BETA-LANES.md.

Citation spot-checks (all verified unless noted): `early_signal.py:222` is actually line 217 (`if r["test"] != "nb": continue`); `state.sql:175-176, 188-191` correct; `today42.jsx:143-236` correct (todaySpecificityView and prepareTodayMarket, ClientHeld at 883-902); `main.jsx:43` correct; `backtest.py:84-91` thresholds correct (14 observed, 60 tested, 20 injected, FA 0.05, recall 0.8 at 3x); `waves.sql:2-4, 6-7, 13-18` correct; `legacy.py:124, 131` and `legacy.sql:6, 17, 56, 73, 83-100` correct; `cluster.py` lines 87, 93, 99, 100, 481-482, 491, 520-523, 532, 541, 546-548, 609-617, 631, 635-643 correct; `job.py:152-156, 231-245, 317-326`, `writers.py:22-23, 513-524, 599-611`, `locality.py:38`, `enrich.py:98`, `embed.py:49-50`, `scorecard.py:87`, `parse.py:235, 361-368`, `schedule.py:15, 96` all correct; `test_applied_storage.py` exists but does not mention `core/detect/sql` (see N1). Commits 875fcdf, 36d928a, 13f1e46, d10eb7b, ce341dd, f276363, 04bc5cc, c6d84d6, f555cb4, 412b1ba, b7a6806, ecff3e2, 6a7f90c and PRs #2 and #4 have the dates stated; 981 commits from 6 October confirmed. DATA.md, TRUST.md, SALVAGE.md, INV, KX, CP, RC, DA and REC line cites all land on the quoted content. No em or en dashes in any deliverable. All 65 audit queries use only columns present in `core/schema/core.sql`, `agent.sql`, `early_signal.sql` and `core/detect/sql/views.sql`, and dataset location US matches SETUP.md.

---

## Blocker

**B1. G0 claims a label freeze that does not exist in the branch, and the deliverables disagree on the label.**
`GAMMA-3.1-PLAN.md:98` (G0: "Label and feature definitions, evaluation protocol and config hashes committed") and `:109` ("G0 done in this branch, awaiting review"). `git status` shows only `core/setup/history_audit.py` and its test. No file freezes `lasting_14`, its features, the abstention thresholds or any config hash. Worse, the plan's label (`GAMMA-3.1-PLAN.md:81`: days 14 to 27 after first seen, present on at least 7 days, at least 3 distinct creators) differs from the methods dossier's (`research/METHODS-DOSSIER.md:91`: `lasting_28`, at the 5-creator or 8-post floor on 7 of days 15 to 28; `lasting_14` only "an earlier proxy", undefined). A freeze that exists in two versions in prose is not a freeze.
Fix: add one committed file (for example `core/eval/gamma_labels.yaml` with a `label_version`, the exact `lasting_14` rule including how invalid collection days and protocol breaks are treated (see S1), the E2 feature list, abstention thresholds including the "identity drift" one the plan never states, and a sha256 recorded in the plan), make METHODS-DOSSIER.md defer to it, and reword G0's acceptance to name that file. Until then, mark G0 "partly done".

---

## Should fix

**S1. E2's label is not leak-free or selection-safe as written.** `GAMMA-3.1-PLAN.md:81`.
(a) "present on at least 7 of days 14 to 27" with no rule for days that were invalid or not collected turns a collection gap into a negative label (the assessment's own NULL-not-zero rule, `HISTORICAL-DATA-ASSESSMENT.md:165`). (b) "first seen" is left-censored by the 9 to 13 October protocol restarts: everything on the v2 panels is "first seen" on 12 October, so the first cohort is dominated by items that were already running. (c) Topic items carry the identity drift the assessment documents (section 4), so a drifted chain can be "present" on 14 days while being three different things; the assessment says only lexical items are fit for longitudinal work (`:21`) but E2 does not restrict kind. (d) No minimum n or positives per market is stated, so "Brier skill of at least 0.05 with the interval above 0 in two markets" has no stated sample behind it.
Fix: define the label on valid days only, requiring at least 10 valid days in the 14-day window or the item is excluded (not negative); exclude items first seen within 7 days of a protocol start on their main lane; run E2 on hashtag, sound and creator items first and report topic items separately until E4 passes; state a minimum of, say, 150 labelled items and 30 positives per market before any read, else "no read yet".

**S2. G2 and D2 dates are too early by about a week, and the gate is misquoted.** `GAMMA-3.1-PLAN.md:100, 111, 134`. The backtest only counts a series as tested after 14 observed days (`backtest.py:84-91`, `WARMUP_DAYS` and `MIN_OBSERVED_DAYS`); a key also needs 60 tested placebo series-days and 20 feasible injected series-days at 3x. On 26 October the 12 October protocols have just finished warm-up with zero tested days, so the earliest a key with 10 series could meet the gate is about 1 November, later for keys with fewer series. The plan also omits the 20 injected series-days floor.
Fix: say "first backtest run from 26 October; earliest possible switch-on about early November, per key, and only for keys with enough series"; quote all five thresholds.

**S3. E6 is satisfiable by construction.** `GAMMA-3.1-PLAN.md:85`. If Watching lists every held candidate, every later-cleared trend "appeared on Watching" the day before. The metric needs a cut.
Fix: score "in the top 5 of Watching per market at least 1 day before clearing", against the baseline of the top 5 of today's held order.

**S4. E1's pass condition is an absence of significance on about 18 points.** `GAMMA-3.1-PLAN.md:80`. "Correlation with daily search calls not significant" is weak evidence of independence on three weeks of data.
Fix: state an equivalence bound (for example the 95% interval of the correlation lies within plus or minus 0.3) or, better, show that clean wave heights do not change on the 1 to 4 October credit-cap steps while `_any` heights do.

**S5. E0 cannot recover breaks one to four days apart with PELT on daily totals.** `GAMMA-3.1-PLAN.md:79`. The known breaks (8, 9, 12, 13 October) are closer together than any sensible minimum segment, and `ruptures` is not in any requirements file (`grep` finds none; `test_requirement_pins.py` guards pins).
Fix: treat registry breaks as given (they are already in the assessment's section 2 table), use the changepoint method only for unregistered breaks on lane totals with a stated minimum segment of 7 days, evaluate it on injected level shifts, and add the dependency through the requirements pin process.

**S6. The three-mark coverage strip implies a per-item "collected, not seen" fact that only exists for lexical items on list and panel series.** `prototype/gamma-prototype.html:260-261, 344-351`; `GAMMA-3.1-PLAN.md:67` (O3). DATA.md:8 and :231 define a zero only for a valid day on a series that watches every item. For a topic item (every item in the prototype is a Format, Sound or Community topic) "collected, not seen" means "no cluster matched", which is an identity decision, not an observation. The strip is the plan's central honesty device, so it must not overstate.
Fix: in the plan, scope the strip to the item's measured series (list and panel lanes) and to lexical items; for topic items show only "present" and "not collected", with the third mark replaced by "collected, no match" worded as such. Adjust the prototype legend accordingly.

**S7. Prototype copy implies measurements 42 does not have.** `gamma-prototype.html:308` ("among people with no shared following": 42 has no follower graph, `RESEARCH-DOSSIER.md:18`); `:323` ("Same household as Creator G": no such inference exists and it would be a rule 1 style audience inference); `:293` ("which is what lasting items tend to look like": an outcome claim before E2 has run).
Fix: "among creators who do not post together" (co-action is measured); "Shares posts with Creator G" (co-action); drop the "tend to look like" clause or label it Inferred.

**S8. "Not collected" hatch fails non-text contrast in both themes, against the design research's own 3:1 rule.** `gamma-prototype.html:12` (`--hatch #B9AC9C` on `#F5EEE4`, about 1.9:1), `:21` (`#5C554F` on `#0B0A09`, about 2.7:1); `research/DESIGN-RESEARCH.md:142` ("All uncertainty marks at 3:1 or better"). The distinction between hatched and empty is load-bearing.
Fix: `--hatch` to about `#8F8375` (light) and `#8A8178` (dark), or draw the hatch in `--ink-3` with a solid `--ink-3` border.

**S9. Single-key shortcuts with no way to turn them off (WCAG 2.1.4).** `gamma-prototype.html:566-571`: `t`, `a`, `c` fire on any keypress outside inputs. Screen-reader and speech users trigger these by accident. Also the Ctrl+K palette (`:555-565`) is `aria-modal` with no focus trap, and Escape on the tray (`:570`) does not return focus to the opening specimen as the close button does (`:538`).
Fix: make single-key shortcuts opt-in (a setting stored like the theme) or require a modifier; trap Tab inside `.pal`; reuse the `data-close` focus-return path on Escape.

**S10. On a phone the days-present strip overflows and hides the newest days.** `gamma-prototype.html:88-95, 205-210`: 28 cells at 13 px (row) and 19 px (`.big`) are 364 and 532 px wide inside a 358 px column, with `overflow-x:auto`, so the right end (this week) is off-screen and the strip scrolls inside a `<button>`. `DESIGN-RESEARCH.md:143` promises strips "stay" on mobile.
Fix: `flex:1 1 0; min-width:0` on `.d` with `gap:2px` so 28 cells fit, or show the last 14 days at phone width with the window stated.

**S11. Beta ownership is wider than section 4.3 says.** `GAMMA-3.1-PLAN.md:120` lists six Beta-owned areas; `/mnt/project-files/42-beta/BETA-LANES.md` also assigns `app/frontend` Today, Ask and Evidence to a Beta lane (P3 UI journeys) and "everything outside the files above" to the audit lane. G3 (`core/detect/sql`) and G4 (`today42.jsx`, `main.jsx`) therefore touch Beta-lane files. The plan's sequencing (after Beta is accepted) makes this workable, but it is not stated, and `main.jsx:43` is a small fix Beta may ship first.
Fix: name the P3 and audit lanes in 4.3, state that G3 and G4 inherit whatever Beta ships there, and offer the `main.jsx` font fix to the audit lane now as a proposed patch.

**S12. D4 is "not due now" but the plan starts E7 now.** `GAMMA-3.1-PLAN.md:16, 109, 136`. E7 needs five strategists, which is decision D4.
Fix: either move D4 to "now, if Albert wants early usefulness evidence" or move the E7 test to after Beta acceptance.

**S13. history_audit.py: the protocol test lets any query containing "SHA256" pass, and no test covers a struct, array or Decimal value reaching `safe_value`.** `core/setup/tests/test_history_audit.py:165-168` (`or "SHA256" in head` short-circuits the whole assertion); `history_audit.py:107-112`. The guard itself is correct (lists and dicts raise Withheld; no NUMERIC column is read, so Decimal cannot occur today), but the test should prove it.
Fix: assert the head contains no bare `h.protocol` outside `SHA256(` by regex rather than the `or`; add a test row with `{"market": "ZA", "tier_posts": {"nano": 1}}` and one with a list, expecting "withheld".

---

## Nit

N1. `GAMMA-3.1-PLAN.md:101` cites `core/setup/tests/test_applied_storage.py` for "`core/detect/sql` is pinned to applied storage". That test checks release-manifest refusal of storage changes (`durable_prior.build_manifest`) and never names `core/detect/sql`. Cite `core/setup/release/applied_storage.py` and the storage-effects hash instead, or drop the file cite.

N2. `HISTORICAL-DATA-ASSESSMENT.md:59, 173, 200` and `GAMMA-3.1-PLAN.md:25` cite `early_signal.py:222`; the line is 217.

N3. `GAMMA-3.1-PLAN.md:102` sets "no target under 24 px" while the repo's own certification gate is 44 px (`PRODUCT-REVIEW.md:181`). Use the repo gate for touch targets. In the prototype `.ev button` (28 px, `:161`), `.back` (32 px, `:109`) and the banner controls (32 px, `:45`) are under it.

N4. Retention is defined two ways: `GAMMA-3.1-PLAN.md:49` "7 and 14 days after the peak"; `gamma-prototype.html:431` "A week later ... needs 7 days after the first high day". Pick one (the methods dossier's peak-based definition, `METHODS-DOSSIER.md:77`).

N5. `gamma-prototype.html:308`: "kept going for three weeks" for an item first seen 29 September (18 days, as the derived record says, `:333-339`). Say "since 29 September".

N6. `gamma-prototype.html:373`: `render()` sets `aria-current` on every `[data-go]` element, including the Back button and the "Go to Today" and "Compare markets" action buttons, which are not navigation. Scope the selector to `nav [data-go]`.

N7. `gamma-prototype.html:561`: the palette shows "D" and "S" key hints for Dossier and Concepts, but no such shortcuts exist (`:569`). Show hints only for keys that work.

N8. `gamma-prototype.html:476-477`: Ask runs on submit with no cost shown beforehand; the current product states the credit cost before asking (`PRODUCT-REVIEW.md:221`), and the brief forbids loosening checks. Print the estimate on the form.

N9. `gamma-prototype.html:455`: the spec button's platform name is inside an `aria-hidden` thumb, so a screen reader hears the quote and creator but not the platform. Move the platform into the visible caption or add it to the button's accessible name.

N10. `gamma-prototype.html:214`: `.tray{transition:transform}` has no transform change, so it does nothing; remove or animate.

N11. `GAMMA-3.1-PLAN.md:83` (E4): "human-labelled transitions, at least 50 per market" has no owner or labeller named; the dossier's labelling budget is TRUST.md's 60 minutes a week. Name who labels and state that labellers are blind to later days (METHODS d2(h)).

N12. `GAMMA-3.1-PLAN.md:86` (E7): "at least 5 of 6 tasks correct" does not say per participant or pooled, and the within-subject comparison against Alpha screens needs counterbalanced order. State both.

N13. `HISTORICAL-DATA-ASSESSMENT.md:234` already says the SQL has not been dry-run. The highest-risk statement is `cluster_transitions` (`history_audit.py:409-430`: correlated `UNNEST ... WITH OFFSET` join inside a scalar subquery and a `QUALIFY` on a self-join). If BigQuery refuses it the script records the refusal and continues, which is the right behaviour; tell Albert in the D1 note that one or two refusals are expected and harmless.

N14. `history_audit.py:238-240` counts `invalid_reason = 'drift'`, which `writers.py:601-611` never writes (the A1 check is not built). Harmless; a comment saying "reserved" avoids a false reading of 0.

N15. `GAMMA-3.1-PLAN.md:30` and `RESEARCH-DOSSIER.md:16` say "3 to 8 clean days"; on 16 October the 9 October protocols have 8 days, the 12 October ones 5 and the 13 October link rule 4. Fine, but the Beta identity fixes are not deployed, so "clean" for topic items is 0 days; say so in the sentence.

---

## What is sound and should stay

- The assessment's regime split, NULL-not-zero rule, and the v_item_waves lane-mixing finding are correct against `waves.sql` and `state.sql` and are the right centre of the plan.
- Every outcome answers the four questions, and the abstention rules are concrete (apart from the unstated drift threshold, B1).
- Ship versus research is separated (G0 to G4 and G7 ship; G5 and G6 shadow; 4.4 later), approvals are flagged (D1 reads, D2 writes test_switch, D3 storage release, G7 deploy), costs are real (G1 at most about US$0.13 at 20 GiB), and no paid call or deploy is implied anywhere.
- history_audit.py is read-only in fact: single SELECT or WITH, no DML keywords, no `SELECT *`, project-pinned references, content columns refused statically, every returned value re-checked, protocols hashed, dry run before every run, 2 GiB per query and 20 GiB per run enforced, no client in plan mode. The 65 queries map to the "inferred" points in the assessment.
- The prototype labels its data as illustrative in the banner, the "Lasting" verdict says "Example only", analogues read "N of 10 needed", Kenya abstains with its reason, and the failed and loading states show no fake content.

---

## Re-review (same day, after fixes)

Heads checked: `/home/user/42` untracked `core/eval/gamma/labels_v1.yaml` (sha256 a378c40fa6123af0b37a4a142231fe9c73c3ccbc5b32df34e37a0e49a78191cc, matches the plan), `core/eval/tests/test_gamma_labels.py`, `core/setup/history_audit.py` and its tests, plus a `docs/gamma/` copy of the package (identical to `/mnt/project-files/42-gamma` apart from README.md and the screens folder). Tests: 32 passed (14 audit, 13 inventory, 1 labels, plus the new withholding test). No em or en dashes in any changed file.

| Finding | State | Evidence |
|---|---|---|
| B1 label freeze | Fixed | labels_v1.yaml carries label_version, window, valid-day exclusion, left censoring, lexical-first, features, baselines, embargo 28, min n (150 eligible, 30 positives), go thresholds and the drift threshold (0.70); PLAN:75, :81, :98 cite the file and hash; METHODS-DOSSIER.md:60 defers to it |
| S1 E2 label | Fixed | labels_v1.yaml `lasting_14`; PLAN:81 restates it consistently |
| S2 G2/D2 dates and gate | Fixed | PLAN:100 quotes backtest.py:84-91 with all five floors; PLAN:111, :135 say early November |
| S3 E6 | Fixed | PLAN:85 top 5 vs top 5, 1.5x with counts stated |
| S4 E1 | Fixed | PLAN:80 natural steps in search volume, under 10% change in clean waves |
| S5 E0 | Fixed | PLAN:79 registry first, minimum segment 5 days, lane totals only, dependency note |
| S6 topic strip | Fixed | prototype:344-345 lexical test, "collected, no matching cluster"; :440 explanatory line on topic item pages |
| S7 copy | Fixed | :308 counts only; :323 "Reposted 2 of Creator G's posts"; :293 no outcome claim |
| S8 hatch | Fixed | `#857A6E` on paper about 3.6:1, `#7D746B` on dark paper about 4.3:1 |
| S9 shortcuts, palette, Escape | Fixed | :574-577 only Ctrl/Cmd+K; :565-571 opener focus returned, Tab trapped, Escape handled; tray Escape returns focus to the specimen |
| S10 phone strip | Fixed | :206 cells 8 px and 9 px under 620 px, 28 cells fit in 358 px |
| S11 Beta lanes | Fixed | PLAN:123 names P3, audit lane and the storage release |
| S12 E7 vs D4 | Fixed | PLAN:86, :109, :137 |
| S13 audit tests | Fixed | test_protocols_are_never_returned_as_written now asserts on the output list; withholding test for arrays, structs and bytes present |
| N1, N2, N3, N6, N8, N9 | Fixed | applied_storage.py:1252 cite (verified: that line collects `core/detect/sql/*.sql` as renderer inputs); early_signal.py:217 in plan; 44 px and non-text contrast in G4 acceptance; aria-current scoped to `nav`; cost line before Ask; platform in spec caption |
| N4 retention wording | Partly | prototype:293 still reads "a week after its first high day"; the plan's O1 uses "after the peak". Cosmetic |
| N7 palette key hints | Fixed | hints removed with the shortcuts |
| N2 in the assessment | Open | HISTORICAL-DATA-ASSESSMENT.md:59, :173, :200 still cite early_signal.py:222 (line is 217). Cosmetic |
| N5, N10 to N15 | Open or partly | nits only; none affects correctness |

**Final verdict: pass.** Nothing blocks. The two leftover items (the assessment's three `early_signal.py:222` cites and the prototype's retention wording) are nits and can be folded into the next edit of those files.

Note for the committer: `docs/gamma/` now puts the whole package, including the 52 KB prototype, into the repo. That is a choice, not a defect, but RULES.md rule 10 (no trailers) applies to the commit, and the G0 head should be the one that holds labels_v1.yaml at the recorded hash.
