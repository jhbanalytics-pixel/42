# The 42 experience

42 is a trend engine you read like a morning paper and question like an analyst: it opens on what is taking off, and every trend and answer carries proof (design principles, 23 September 2026, updated 28 September for the trend engine). The existing React 18 and Vite app and the vendored design system (ogilvy-intelligence-design-system 2.0.22: Newsreader, Recursive, Ogilvy red used sparingly) stay. What changes: 42 shows more than it gates, every answer has media and checkable citations, and the agent's work is visible while it happens.

## Navigation

Rail: Today, Ask, Discover, Compare, Investigations, Dossiers. At the foot: History, Alerts, Coverage. Everything else (Seeds, Seed path, Map, Board, Listen, Network, Lexicon, Browse, Method) moves under More; nothing is deleted. Ask gets its own route #/ask (#/console?work=ask stays as an alias). Themes: Light (default), Dark, Match system.

Type: four sizes only, 13, 16, 21 and 32 px; Recursive at 400 and 600; Newsreader for one moment per screen (the short answer, the brief's finding sentence). Red marks the one primary action, never direction or alarm.

## Screens

| Screen | Its one job | Built from |
|---|---|---|
| Today | Show the morning's key trends per market, explained and proven | today.jsx, reworked |
| Ask | Answer a question with checkable claims | chat.jsx and ui/GeneralIntelligence.jsx |
| Discover | The full trends feed: every live trend by state, market, platform and kind, plus Radar (growth against reach) | explore.jsx (its DISCOVERY_MODES become tabs) |
| Topic and creator pages | Tell the story of one thing | releasedSignal.jsx, creator.jsx |
| Compare | Two to five things side by side, same units and window | compare.jsx |
| Investigations | Deep, budgeted research that runs in the background | fieldwork.jsx, repurposed |
| Dossiers | Assemble, review and export | evidenceRoom.jsx and DossierReview.jsx merged |
| History | Past asks, briefs, dossiers and analogues | historicalWorkspace plus stored threads |
| Alerts | Watch a topic, sound, creator or brand | new |
| Coverage | What was collected, where, when, at what cost, and the gaps | sourceLab.jsx plus new |

## Ask: anatomy of an answer

1. The question (21 px, 600).
2. Meta line: "South Africa · posts from 21 to 27 September 2026 · 214 posts from 6 platforms".
3. Short answer: two or three sentences in Newsreader 21 px.
4. Claims. Each sentence carries evidence chips (platform mark, handle, +N). Hover or focus shows a popover with thumbnail, the quoted words highlighted, date and views; click pins the source in a side panel so the answer stays in view. A muted confidence word leads each claim, with a small shape so it survives greyscale: Observed, Corroborated, Single source, Inferred. "Sources disagree" links to the conflict.
5. Posts strip: four to eight cards, each a 9:16 thumbnail with duration, creator, date, views and a transcript line with its timestamp. No autoplay.
6. Numbers: two to four figures written as sentences with a labelled sparkline ("Mentions 12,400, up 38% on last week").
7. What it means for a brand, then What we would do (each with its basis and what would change it).
8. What we do not know: gaps, failed sources, what would change the answer.
9. Actions: one red "Add to dossier"; quiet "Compare with", "Watch", "Go deeper" (starts an investigation); three suggested follow-ups built from the gaps.
10. Footer: "214 credits · 41 s", with Details holding the method, tokens and run id.

While the agent works, the answer area shows a research log in plain words with counts ("Reading TikTok posts tagged #amapiano, South Africa, 21 to 27 September: 312 found", "Transcribing 24 videos", "Checking claims"), thumbnails gathering as sources arrive, claims appearing marked "checking" until verified, and a Stop button. Afterwards it collapses to "How this was researched · 6 steps".

## Today: the morning's key things

Ready by 06:30 SAST. Heading "Taking off, 28 September 2026" (32 px) and one serif sentence stating the biggest finding. Market tabs (South Africa, Nigeria, Kenya, All; All shows the top three per market). Five cards per market, ranked; "Show all" expands in place to the ten confirmed candidates (it opens Discover once Discover exists in Stage 2).

Stage 1 card (only fields Stage 1 can fill): a day-over-day tag (First time on Today; Moved up, when its rank is higher than on yesterday's published Today; Held its place, when it was on yesterday's Today at the same or a lower rank; none on the first published morning), the state word, the title, the one-sentence explanation with any hedge on the why-now clause only, a count line ("31 creators, 3 days, 2.8 times usual"), a sparkline with the expected band, two thumbnails, "Ask about this". Stage 2 adds the spread line, the authenticity flag, Watch and topic pages. The track-record chip ("74% of last month's Rising calls held a week later") appears only after four weeks of scored data. At most one state, one confidence label and one flag per card.

Below the cards:
- Dropped since yesterday: one line per item that left the list, with its reason (faded, held back, reclassified as Seasonal). On the first published morning this line reads "First morning: nothing to compare yet".
- Held back: count and reasons ("3 held back: too few creators"), each openable, so nothing disappears silently.
- Moments: calendar moments in the next 14 days and Seasonal items, in their own row, so they are never passed off as new.
- On the boards today: what platform boards and charts show, labelled as the platforms' own lists.
- Coverage strip per market: posts collected, platforms, share of posts with a confident location, any data issue.
- Banners when they apply: "Warming up: day 4 of 14" (first two weeks, when states are limited to New to 42, Spike, On the boards, Seasonal, Recurring and Emerging), "Thin coverage" when a market's collection fell short, "Data issue" when detection or collection failed.

One-tap feedback on each card (Real, Not real, Useful) tells reviewers where to look. It never feeds the precision or trust numbers, which come from the weekly random review only (TRUST.md section 6).

## Words 42 uses (one glossary for every screen)

| Kind | Words | Meaning |
|---|---|---|
| Trend state (one per card) | New to 42, Spike, On the boards, Emerging, Rising, Peaking, Mainstream, Fading, Recurring, Seasonal | ENGINE.md section 2; warm-up states in the first two weeks |
| Confidence label (one per claim) | Observed, Corroborated, Single source, Inferred | TRUST.md section 3; code sets the highest allowed |
| Flag (at most one per card) | Check pattern, Likely coordinated, Paid-led, Market unconfirmed, Not assessed (thin sample), Data issue | TRUST.md sections 2 and 5; Clear means no flag and shows nothing |
| Action | Ask about this, Watch, Add to dossier, Go deeper | Watch is only ever this verb |

Lifecycle is shown as a word with a five-step neutral marker. Spread (Stage 2) is a sentence plus per-platform sparklines: "First seen on TikTok 12 September, X 18 September, news 24 September; 38 creators; Gauteng to Western Cape".

## Investigations

The plan appears first: sub-questions, platforms, credit estimate, with Edit plan and Start. It runs in the background with the live log; a notice arrives in Today and by email when done. The result is a findings pack that opens as a draft dossier.

## Dossiers

A draft assembles itself from an answer or investigation: summary, claims, posts, numbers, gaps. A reviewer ticks claims; Single source and Inferred claims need a tick before approval. Approval freezes that version (write-once storage) and enables HTML and PDF export with working citations (existing exporter). Share by link inside IAP.

## From brief to PDF in five minutes

Today, "Ask about this" on a change (0:00); the answer and chips arrive (1:00); one follow-up, "Add to dossier" (2:00); the draft dossier opens assembled (3:00); approve and download the PDF (4:00).

## Build notes

- GeneralIntelligence.jsx becomes the one answer renderer; retire ui/CitedAnswer.jsx and the AnswerBody path.
- New components: EvidenceChip, SourcePanel, PostCard and PostStrip, FigureLine with sparkline (reuse Sparkline from parts.jsx), ResearchLog, LifecycleMarker, SpreadLine, DossierTray, Watch button, Alerts screen.
- New API needs: server-sent event stream of research steps; media fields on evidence (thumbnail, duration, transcript span with timestamp); confidence label and independence per claim; follow-ups; credits; lifecycle and spread per trend; watchlist and alert endpoints.
- The design system package is compiled and its InstrumentShell accepts only five jobs. Either find its source and release 2.1 (six jobs, four-size scale), or wrap the shell with a thin local rail. Stage 3 decides; Stage 1 works with the current shell.
- Remove every Gen Z, Prompt Pulse and Nano Banana string from copy (model.js, topic.jsx, views.jsx, briefText.js, researchLib.jsx).
- Keep: honest empty and failed states, focus management, live regions, contrast checks, Playwright journeys.
