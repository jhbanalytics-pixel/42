# Cutover package, 42 / Ogilvy Intelligence redesign

For Albert. Production is untouched; nothing here proposes a merge or a deploy. This states what the redesign changed, what proves it, and what the plan's own cutover criteria still require before production can be considered.

## What shipped, by batch

**A, foundation.** Radius 0 with a 2px form allowance; one Ogilvy red accent replacing four accent families and the violet Console lane; two elevations; an eight-step type ramp and the 4 to 64 spacing scale; a ratcheting token gate; deletion of the Google boot animation with its four brand-colour dots, the ticker, blinking markers, hover lifts and the unwired motion selector. The audit record moved into `docs/audits/`.

**B, Briefing as Live Margin.** The compare-next queue moved onto the comparison strip, its first production caller, carrying movement and a receipt-count proof line. Measurement at 390px found the possible response 1,372px down the page against a 720px rule; the lead now carries a one-line evidence-backed response cue that begins 378px from the top and renders only when a ready response exists.

**C, Discover.** Readiness and discovery-mode filters over the admitted index, with an honest result count and a filtered-empty state that says the run still holds its signals.

**D, signal qualities.** The engine's six measurements reach the strategist as bands: novelty, breadth, source independence, history and geographic confidence, plus optional taxonomy tags. No score, dataset or table name is served. A missing measurement reads unmeasured, never low. Contract moved to `desk_dynamic_signal_v2`; the client refuses v1, which carried no qualities and would present unmeasured signals as measured.

**E, Compare and consolidation.** Compare read curated topics and creators from two retrieval routes and drew two cards with a fixed centre column, duplicating the desk ranking a third time. It now reads the same released run as Briefing and Discover, through the comparison strip: same field, same column, a red rule where the two part company. Three destinations nothing linked to are gone (Voices board, creator profile, Intelligence topics board); the audience refusal they carried moved to the shared UI layer. The raw-font-size ratchet tightened from 787 to 704.

**F, Fieldwork.** Four disagreeing budget models became one reading: three lanes, one vocabulary, each stating its cap, its kill test and its own completeness. No lane reports zero spend, because no consumption ledger is connected and zero would assert a measurement nothing made. It travels with the Source Lab inventory.

**G, state matrix.** The matrix is executed, not written: thirteen cases render Briefing, Discover and Compare in loading, error, no-discovery, stale, insufficient, filtered-empty, thin and contradictory states and read the result. Two deliberate mutations were run against it. The first, leaking a recommendation into the no-discovery state, was caught. The second, removing the guard that downgrades readiness when no receipt exists, was **not** caught: the assertion had been satisfied by the word "unchecked" appearing on a filter control. The assertion now reads the readiness announcement itself, and the same mutation fails it.

## Evidence at this revision

Frontend suite 370 pass, 0 fail. Python suite 835 passed on a clean tree. Ogilvy intelligence harness 56 pass. Redesign contract harness 30 pass. Contrast: every token pair meets WCAG AA in both themes. Token ratchet: 704, shrink-only. Production build clean. Every staging deploy passed its readback contract. Staging revisions `00096-bwf` through `00103-7n9`.

## Cutover criteria, checked

The plan sets six conditions for production. Five are met at `listening-post-staging-00105-dbd`; the sixth is Albert's.

1. Fourth Rams audit at 24/30 or better with no zero principle: **met.** 26/30, no zero. Scorecard and verdict in `docs/audits/2026-08-29-rams-audit-4/`.
2. Zero yes answers on the anti-slop test: **met.** Twelve answers recorded in that verdict, each tied to a check.
3. Golden-task suite green: **met.** 78 assertions over the eleven golden tasks, including the election task end to end. The overclaim detector carries self-tests proving it fires.
4. State matrix complete: **met** for Briefing, Discover and Compare, and mutation-tested. Fieldwork, Source Lab and the historical workspace render refusals from inert backends rather than states of their own.
5. Accessibility and performance gates green at both viewports: **met.** Lighthouse desktop 91 performance, 96 accessibility, 100 best practices; mobile 90, 100, 100. CLS 0 and total blocking time 0 ms on both. LCP 0.8 s desktop, 2.9 s mobile. At 390px the document does not overflow (scrollWidth 375, innerWidth 390).
6. Albert's explicit written sign-off: **not given**, and nothing here asks for it.

## Known open items carried forward

1. Gradients, rounded cards and idle animation survive in `app.css`, `styles/console.css` and `ui/ui.css`, which serve the routes still awaiting migration.
2. `source_lab.fixed_scope()` cannot resolve against the committed `configs/investigation_scopes.yaml`; the YAML is digest-pinned, so the fix needs an approved digest change.
3. Fieldwork, the historical workspace, artifact reads, the investigations list and dossier approval remain refusal stubs. Their surfaces render the refusal rather than inventing a payload.
4. The Ask router's cited-answer surface is built and tested against the frozen contract; the masthead entry control that submits a question to it is not.
