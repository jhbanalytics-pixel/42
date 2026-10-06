# Batch critique, 2026-08-29

Scope: staging revisions `listening-post-staging-00096-bwf` through `00101-dfs`, source branch `feat/42-redesign-staging`. This is the per-batch gate the redesign plan requires between batches: the twelve-question anti-slop test, checked mechanically where a check exists, plus what each answer rests on.

## Anti-slop test

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Could the screen belong to an unnamed AI startup after changing the logo? | No | The mark is the 42 folio with the Ogilvy Intelligence ownership line; the Briefing is an editorial margin with a red evidence thread, not a card dashboard. |
| 2 | Glass, aurora, glow, floating pills, gradient atmosphere? | Partly, in legacy sheets only | 13 gradient or backdrop-filter declarations remain, all in `app.css`, `styles/console.css`, `ui/ui.css`, which serve routes not yet migrated. Zero in the redesigned sheets. `--accent-glow` resolves to `transparent` in both themes. |
| 3 | Rounded card as the default grouping? | No in the new grammars, yes in legacy | Radius tokens are 0 with a 2px form allowance; 170 non-zero `border-radius` declarations survive in the same three legacy sheets. |
| 4 | A colour without stable semantic meaning? | No | Accent is red everywhere: thread, active position, primary action. The four alternate accent families and the violet Console lane are deleted; up and down keep direction only. |
| 5 | Motion while the user is idle? | Partly, in legacy sheets only | 9 infinite animations remain in `app.css` and `ui/ui.css`. The boot splash, brand dots, ticker and blinking markers are deleted. The redesigned surfaces animate only on hover, focus and chapter advance. |
| 6 | Does the screen begin with a generic chat or search box? | No | Briefing opens on the lead signal; Ask is a masthead entrance, not the first element. |
| 7 | Model, vendor, table, score or internal route exposed before the question requires it? | No | The six engine measurements are banded server-side into low/moderate/high; no score, dataset or table name is served. The only occurrences of `graph_score` in the client tree are negative assertions in tests. |
| 8 | An unsupported audience claim more confident than unavailable? | No | No demographic claim ships. Unmeasured qualities render as Unmeasured, never as a low band. |
| 9 | An empty state pretending zero is intelligence? | No | `no_discovery`, `unavailable`, stale and filtered-empty each state what was checked and what is absent. The filtered-empty state names how many admitted signals the run still holds. |
| 10 | More than three visual grammars on one screen? | No on Briefing and Discover | Briefing renders exactly one article; the queue is comparison strips; evidence is the drawer. |
| 11 | Mobile preserving desktop chrome at the expense of the recommendation? | No | Measured at 390px: the response cue begins 378px down, inside the 720px bound. Before the fix the response chapter began at 1,372px. |
| 12 | Any Google name, logo, colour system, font request or product metaphor visible? | No | The four-colour boot animation is deleted. Remaining matches in the tree are Chrome binary paths in test harnesses and one negative assertion; no external font transport. |

Two answers are qualified rather than clean: 2, 3 and 5 hold for every surface the redesign has reached and fail inside the three legacy stylesheets. Those sheets serve the routes Batch E merges or deletes, so the finding is scheduled work, not an accepted exception. Recording it here rather than claiming a clean sweep.

## Gate results at this revision

Frontend suite 350 pass, 0 fail. Ogilvy intelligence harness 56 pass, 0 fail. Redesign contract harness 30 pass, 0 fail. Python suite 827 passed on a clean tree. Contrast: all token pairs meet WCAG AA in both themes. Token ratchet: 787 raw font sizes, unchanged, shrink-only. Production build clean. Every staging deploy passed its readback contract.

## Carried into the next batches

1. Legacy sheets keep gradients, rounded cards and idle animation; they leave with their routes in Batch E.
2. `source_lab.fixed_scope()` still cannot resolve against the committed `configs/investigation_scopes.yaml`; the YAML is digest-pinned, so the fix needs an approved digest change.
3. Fieldwork, historical workspace, artifacts, investigations list and dossier approval remain refusal stubs; Batch F and G design against those refusals rather than inventing payloads.
4. No fourth Rams score exists yet. This critique is a gate, not an audit, and it does not substitute for the scored review the cutover requires.
