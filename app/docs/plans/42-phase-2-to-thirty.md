# 42 / Ogilvy Intelligence: phase two, 26 to 30

Status: PROPOSED. Nothing here runs until Albert approves it.

Base: staging `listening-post-staging-00105-dbd`, branch `feat/42-redesign-staging`, commit `39a159b`. Phase one is done and audited at 26/30 with no principle at zero (`docs/audits/2026-08-29-rams-audit-4/`).

This plan targets the four principles that scored 2 and the engineering debt behind them. It is not a redesign. Everything below either finishes something phase one started or deletes something phase one left standing.

## The four points, and what each actually costs

### 1. Innovative, 2 to 3: make the thread mark relationships, not reading position

Today the red thread advances as the reader scrolls. The constitution asks it to mark real relationships: two sources agreeing, a signal crossing markets, history repeating, evidence contradicting, a response citing a receipt.

The data exists. `signal_lineage_v2` carries merge and split edges with an overlap score, and the frozen fixtures include both. `signal_evidence_v2` carries `direction` and `source_family`, which is exactly the agreement and contradiction mechanic already enforced server-side as `direction_by_family`.

Verified before planning, and it changes the sequencing: the staging lineage table holds **2 rows**. There is a source, not a corpus. So the thread ships in two steps rather than one.

Step one, agreement and contradiction, which has data now: each receipt in the ledger carries its source family and direction; where two families agree the thread joins them, where they disagree it forks and the contradiction is stated at the fork. This is a rendering of `direction_by_family`, not a new inference.

Step two, lineage and recurrence, gated on data: the thread carries merge, split and recurrence edges when the lineage table holds enough of them to be worth reading. The gate is explicit, a count, and until it passes the surface says the archive is too thin rather than drawing a thread from two rows.

Cost: one contract addition (`relationships` on the served signal), one rendering change in the Briefing thread, one honest gate. No new engine work.

### 2. Aesthetic, 2 to 3: serve the typeface instead of hoping for it

The audit recorded the Ogilvy faces as unavailable. That was half wrong, and the correction matters. `OgilvyJBaskerville` is installed on this machine, five files including the OTF and TTF, which is why every screenshot I have read shows the real face. It is not installed on a colleague's machine, and the app makes no font request, so for them `tokens.css` falls through to Baskerville, then Georgia. The product I have been auditing is not the product they see.

Fix: self-host the face as a subset woff2 from the app's own origin, `font-display: swap`, no external transport, so the constitution's "no external font request" rule holds while the identity actually renders. Latin subset only; the archive is ZA, NG and KE English.

Two things need Albert before this ships. Whether the internal brand library is the source of the file rather than the machine's font folder, and whether embedding it in an authenticated internal tool is within its licence. Both are questions for him, not for me, and the batch stops until they are answered.

The remainder of the point is the three legacy stylesheets, addressed below.

### 3. As little design as possible, 2 to 3: eleven destinations to five jobs

`VIEWS` still admits: pulse, explore, compare, console, source-lab, historical, method, network, lexicon, board, browse, map, seeds, seedpath, listen, topic, research. Seventeen names, five jobs.

Disposition, one line each, no route surviving because it exists:

- **Fold into Discover**: `topic` becomes the selected signal's own view; `listen` and `browse` become one evidence search inside the Discover drawer; `seeds` and `seedpath` become the provenance view of a signal's origin.
- **Fold into Fieldwork**: `network`, `map`, `method` are all descriptions of how the engine works; they become chapters of Method inside Fieldwork.
- **Fold into Build**: `research` is Console's own mode already.
- **Keep as utility**: `lexicon` and `board`, both reader-owned rather than engine-owned.
- **Keep as job**: pulse, explore, compare, console, source-lab, historical, fieldwork.

Every folded route keeps its hash as a redirect for one release, then the alias is deleted. Nothing 404s silently on a saved link.

### 4. Long-lasting, 2 to 3: the six refusal stubs

Twelve refusal sites across the API: fieldwork workspace, historical workspace, artifact reads, investigations list, dossier approval and the empty approved-role registry. Each renders an honest refusal today, which is why Honest scores 3. But a product whose promised surfaces are refusals is not a durable product, and the audit was right to hold this at 2.

This plan does not build six backends. It does the one thing that makes the debt legible and finite: each stub gets a stated reason, an owner and a condition. What is missing, who decides, and what has to be true before it can return a payload. That turns six mysteries into six decisions Albert can take or defer, and it stops the surfaces from implying that work is underway when it is not.

## The legacy stylesheets

`app.css` carries 208 radius, gradient or infinite-animation declarations, `ui/ui.css` 19, `styles/console.css` 13. They serve the routes above. As each route folds, its sheet section goes with it; the token gate ratchets down as it happens. The target is not zero declarations by fiat, it is zero routes needing them.

## Batches

Same discipline as phase one: focused tests first with recorded expected failures, smallest implementation, full local gates, deploy the exact commit to staging, record the revision, critique before the next batch starts.

- **H, typeface.** Self-hosted subset woff2, licence and source confirmed by Albert first. Gate: no external font request, identity renders on a machine without the face installed, verified by rendering with the local font disabled.
- **I, thread as relationship, step one.** Agreement and contradiction marked on the thread from `direction_by_family`. Gate: a contradictory signal shows the fork and states it; a single-family signal shows no relationship rather than an invented one.
- **J, route consolidation.** The eleven-to-five fold above, with one release of redirects. Gate: every retired hash resolves to its new home; no saved link breaks; the token ratchet falls.
- **K, stub disposition.** Reason, owner and condition for each of the twelve refusal sites, written into the code beside the refusal and surfaced in the UI where a reader meets it.
- **L, lineage gate.** The recurrence thread, behind an explicit row-count gate, with the honest under-threshold state shipped first.
- **M, fifth audit and cutover package.** Full matrix, Lighthouse both viewports, anti-slop, scored audit, package for Albert.

## What 30/30 requires that this plan does not promise

An audit is a judgement, not an arithmetic. This plan closes the four named reasons the fourth audit gave for holding points back. If the fifth audit finds new ones, that is the audit working. I would rather hand you a plan that closes the known gaps honestly than one that asserts a score in advance.

## Decisions needed from Albert before batch H

1. Is the internal brand library the source for the typeface file, and does its licence permit self-hosting inside an authenticated internal tool?
2. Do the folded routes match how the team actually works, particularly `board` and `lexicon` staying as utilities?
3. For the six refusal stubs: which are being built, which are parked, and by whom? Batch K records the answers rather than inventing them.
