# 42 staging vetting guide

This is the guide for early vetting of the integrated Ask, Fieldwork and Briefing journey on staging. It is written for a strategist opening the app in a browser. It contains no secrets and it does not require reading any operations script. The staging URL and the access key are handed over separately by the person who deployed the candidate.

## Current corpus date

Every date about the data changes when a new candidate is released, and the revision changes again when the pricing review is renewed. This guide does not write those values down, because a written value goes stale at the next release. Each row below reads **set at release** and says where you read the live value.

| Fact | Value |
|---|---|
| Ask window | Set at release. Read it on the Ask page, in the line under the page title. |
| Released run date shown on the Briefing | Set at release. Read it in the Briefing header, under Completed run date, beside the Closed window the run observed. |
| Desk freshness | Set at release. Read the checked age in the app's utility strip, which counts from the desk's last refresh. |
| Release age at the time of vetting | Set at release. Count the days from the run close of the released Briefing run to the day you vet. |
| Revision vetted | Set at release. The app does not display it; copy the revision and its deployment digest from the handover for the candidate. |

The Ask page states its window once, under the page title, as a line such as "Covers South Africa," followed by the dates. The market in that line follows the market chips in the header, and the workbench rail does not repeat the window. The line reads the window from the server, so it moves when a newer profile is served; if it disagrees with a handover or any document, the page is right. The shell rail line that ends in (Briefing) is a different window: the one the released Briefing run observed. A question about dates outside the Ask window is refused with that window named and an offer to ask about the latest available week instead.

Until the release that switches Ask to this week's captured data, the corpus is the disclosed retained corpus. Early vetting works against that corpus. It is not fresh acceptance, and it is not semantic acceptance. Those come after the D04 freshness gate and the Q01 to Q04 work.

## How to enter

1. Open the staging URL in a current desktop browser.
2. The access gate asks for an access key. Enter the key you were given. A wrong key is refused and the gate stays on screen.
3. After the key is accepted the app opens on the Briefing at `#/pulse`. The market chips in the header set the market scope for every route that reads a market.
4. Every route is a hash URL and every hash URL is shareable. Paste one into the address bar to land on that route directly.

## What is implemented

The candidate carries the routes below. Each one has a job it must do and a proof panel that shows where the reading came from. The full inventory, with the producer and contract version behind each route, is `app/docs/capability-journeys.json`.

| Route | Job | What you should see |
|---|---|---|
| `#/pulse` | Today and Briefing | A dated lead from the released run, its receipts, and an action into the lead's topic or into Discover. |
| `#/explore` | Discovery | Released candidates with membership and evidence readiness, and deep links into a topic story or the seed explorer. |
| `#/compare` | Compare | Signals in the same units and window against a named comparator. |
| `#/console?work=ask` | Questions and follow-ups | A request bound answer with claims, receipts, limitations and missing work, and a follow-up on the same request. |
| `#/fieldwork` | Fieldwork | Source and research operations with readiness, market scope, status and gaps, and an Open question link on a research operation. |
| `#/source-lab` | Coverage | Source routes with status, credit cost, kill state and the catalog snapshot digest. |
| `#/console?work=brief&investigation=...` | Investigation, build and review | The investigation frame, claims, decision and artifact. |
| `#/console?work=brief` | Build brief | On staging, a cited brief asked of 42's question engine and opened as an Ask answer. See below. |

Two rows of the inventory are not implemented in this candidate and are recorded as such rather than hidden. The Historical workspace answers unavailable for every investigation until the historical backend gate passes, so `#/historical/<investigation>/<mode>` shows the unavailable state with its error code. There is no configured persona or audience lens for Brand South Africa, and on staging the persona picker is not reached at all, because Build brief asks 42's question engine instead.

## How to open sources

1. On a question reply, each cited claim carries a citation label such as R1. Press the Open button for that label. The receipt record opens below the claim with the source label, the excerpt, the published date, the market and the record reference.
2. Press View original source on the receipt to open the record at its source in a new tab. Opening a receipt never moves you off the request URL.
3. On the Briefing, the evidence summary under the lead names the receipts behind it. On Fieldwork and Source Lab, each source route row states its status and its downstream use.
4. A receipt with no link or no excerpt is a finding. Report it with the request and the citation label.

## How to download an answer

1. A completed answer carries two buttons in its actions row: Download as HTML and Download as PDF. They appear only on an answer whose usage has settled and that carries no error.
2. Download as HTML saves `42-answer-<first eight characters of the request id>.html`, and Download as PDF saves the same copy as a PDF. The server rebuilds the copy from the stored answer, with its question, answer and cited sources.
3. A refused download shows its reason in plain words under the buttons. "Only a completed answer can be exported", "This answer is held until its usage record is settled" and "Another PDF is being made. Try again in a moment." are expected. "A PDF cannot be made on this server right now" and "The stored answer did not pass its checks" are findings; report them with the request id. The others mean: "The download did not complete. Try again in a moment." (the download was lost or no reason came back; try again), "The stored answer could not be read just now. Try again in a moment." (a passing read failure; try again), "This answer is not available in this workspace." (the request id names no answer your access key can see) and "The answer reference is invalid." (the request id in the address is malformed; reopen the answer from its request URL). Report any of them that repeats, with the request id.

## Build brief on staging

On staging only 42's question engine may use the model, so Build brief asks it for the brief the way Ask does.

1. Press Build brief in the workbench rail beside Ask, or press Turn into a cited brief on an answer in the Ask conversation, which fills the topic from that question.
2. The page shows the heading Build a cited brief with 42's question engine and names the covered window, the same window as the Ask page line.
3. Fill in Topic, choose a Market and, if you want, a Framing (optional). The page shows the exact question under The question 42 will be asked. Check it says what you meant.
4. Press Ask 42 for a cited brief. When the answer is ready the page opens it at `#/console?work=ask&request=<request id>`. That cited answer, with its sources and its HTML and PDF downloads, is the brief.
5. If the question is not answered, the page stays put, shows the missing work in plain words and offers an Open the request link. That is correct behaviour.

A persona picker, a behaviour scan or a brief writer starting on staging is a finding. A revision without this change shows Cited briefs are not available here with a Go to Ask button instead; note which one you saw and the revision.

## How to return to a saved request

1. Every admitted question has a request URL of the form `#/console?work=ask&request=<request id>`. The request id is the 36 character identifier shown under Request and usage details on the reply. When a reply arrives under General 42 the address bar changes to that URL, so you can copy it straight from there to share or reopen the answer. A reply asked under a named client lens keeps the lens in the address instead, because the stored request URL carries no lens.
2. Fieldwork lists research operations. The Open question link on an operation row opens that request URL.
3. Opening the request URL, or reloading it, reads the stored question from the server and shows the exact question text and the stored reply. The requested and resolved market and window and the reserved allowance in US dollars sit one click down, under Request and usage details. A question that named its dates only in its wording shows the window it resolved to. A request whose execution is unconfirmed says so under its question and shows no answer.
4. Back to Fieldwork on the stored question returns you to `#/fieldwork`.
5. Ask a follow-up, the field under the stored answer, opens a new conversation that carries the stored turns and asks the question you typed there.

## How to report a finding

Report one finding per row. A finding needs all six fields so it can be reproduced and ranked.

| Field | What to write |
|---|---|
| Route | The full hash URL you were on, including any query. |
| Request | The request id if the route carries one, otherwise none. |
| Revision | The revision named in the handover for the candidate you are vetting. The app does not display it, so copy it from the handover. |
| Expected | What the job for that route says you should see. |
| Actual | What you saw, quoted exactly where the text matters. |
| Severity | Blocking if the job cannot be done, major if the job is done but a proof or action is wrong, minor otherwise. |

Attach a screenshot when the finding is visual. Do not paste the access key or any other credential into a report.
