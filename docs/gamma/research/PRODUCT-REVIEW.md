# 42 product review for Gamma 3.1 (strategist journey, Alpha 2.4.0 code)

Reviewed 16 October 2026 against `release/beta-20261016` candidate code (VERSION 2.4.0; the working branch was `gamma/research-20261016-407cj1` at the same tree). The live app was not touched.

## 0. How this was done, and how far to trust it

**The app was run locally.** It was not reviewed from code alone.

- **Frontend.** `vite build` of `app/frontend` (bun install, frozen lockfile) into `product-review/dist/`. The build is outside the repo.
- **Backend.** The real `core/api` FastAPI app, run with `F42_DATA=fixtures`, which is the store's default and reads `core/api/fixtures/`. The in-process agent was used and `UI_PASSCODE=local-review` was set. It ran from a scratch venv on 127.0.0.1:8042 and served the built SPA through `WEB_DIST`. There was no BigQuery, no model key and no outbound traffic: the browser aborted every non-local request, and every non-GET `/api` call was answered with 409.
- **Fixture overrides in the browser.** Two states use the frontend's own test fixtures, and the screenshot names say so (`*-fixture`):
  - the populated Today (`src/ui/__tests__/fixtures/today42.json`)
  - a finished Ask answer (`ask42_complete.json`)
- **Every other screen** is what the real API code returns from its fixture store.
- **The empty state is the backend's own output.** With fixtures, the backend produces the same kind of state that live shows today: 0 trends cleared and 16 held (ZA 9, NG 6, KE 1). Live shows 0 trends and 30 held.
- **Tools.** Capture scripts: `product-review/tools/shoot.mjs`, `throttle.mjs`, `res.mjs`, `contrast.mjs`. Raw numbers: `measurements.json`, `throttled.json`, `contrast.json`, `contrast-dark.json`.
- **Clean-up.** At the end, `git status` was clean. Untracked `app/frontend/node_modules` was left in place, as allowed. The `__pycache__` folders created by the first import were removed.

**Limits.**

- Fixture content (`#fixture_za_step`, etc.) is synthetic, so copy quality and real evidence richness are not judged.
- Thumbnails are `example.invalid` URLs, so the image boxes are empty in the screenshots. That is an artefact of the review setup, not a product bug.
- Full-page screenshots show the fixed bottom bar (mobile) and the sticky rail (desktop) at their viewport position. That is also an artefact.
- Some fixture-specific contradictions noted below depend on fixture data. Where that is so, the finding names the code path that would let the same thing happen on live.

---

## 1. Route and screen map (what is actually live in 2.4.0)

The app has a hash router (`src/router.js`) and a route switch at `src/App.jsx:1086-1146`. Navigation is the local rail `src/rail42.jsx:22-50`. The design-system `InstrumentShell` still renders, but its five-job menu is hidden by CSS at `styles/rail42.css:127-128`.

| Rail position | Route | Screen (file) | Main API reads (api42.js) | Key fields drawn |
|---|---|---|---|---|
| Start here | `#/pulse` (also `#/today?date=`) | Today `today42.jsx` | `GET /api/today[?date]`, `/api/alerts`, `/api/investigations?status=complete`, `/api/schedules` | `heading`, `headline{text,item_id}`, `warmup{day,of}`, `published_at`, `status`, `run_receipt`. Per market: `cards[]`/`more[]` (title, state_word, explanation, `specificity`, claims, numbers, sparkline.points, evidence, tag), `held_back{count,text,items[reason,reason_text,failed_reason,held_detail,evidence,numbers]}`, `dropped`, `not_assessed`, `boards`, `moments`, `coverage{posts,located_share,issues}`, `banners`, `breaking`, `searching_now` |
| Start here | `#/ask` (`?q=&draft`, `?follow=<ask_id>`) | Ask `ask42.jsx` | `POST /api/ask` (cost-confirmed only), `GET /api/ask/{id}`, SSE `/api/ask/{id}/events` | `answer{short_answer,claims[label,evidence_ids,numbers],evidence,so_what,watch_next,gaps}`, `run{credits,seconds,window,posts,platforms,source_status,followups}`, `steps[]` |
| Start here | `#/explore` (`#/discover` canonicalises to it) | Discover `discover42.jsx` | `GET /api/discover?market&sort&kind&state&platform`, `GET /api/discover/radar` | items: title, state, `lifecycle{step,word,rule}`, `novelty`, `market_scope`, `reach`, `reach7`, `growth`, `explanation_status`; `held_back`; radar points (posts 7d, platforms, local share, flag) |
| Your work | `#/alerts` | Alerts `alerts42.jsx` | `/api/watches`, `/api/alerts` | watch rules, fired alerts |
| Your work | `#/investigations[/id]` | `investigations42.jsx` | `/api/investigations`, `/api/investigations/{id}` | plan, status, log, findings |
| Your work | `#/dossiers`, `#/dossiers/{id}`, `#/d/{id}/{ver}` (share) | `dossiers42.jsx` | `/api/dossiers`, `/api/history/asks` | drafts, claims ticks, frozen versions, export |
| Your work | `#/history`, `#/history/items/{id}?market=` | History `history42.jsx` | `/api/history/asks`, `/briefs`, `/findings`, `/search`, `/items/{id}` | asks (status, answer_status), brief dates (cards count), findings, item `waves[]`, `recurrences`, `analogues[]`, `current_day` |
| (from cards) | `#/t/{item_id}?market=` | Topic `topic42.jsx` | `GET /api/topics/{id}?market` | card + "How it developed", Spread, Where it started, News, Daily posts by platform, Earlier waves, posts |
| More: Dig deeper | `#/compare?...` | `compare42.jsx` | `/api/compare`, `/api/discover` | subjects, series per window (7/14/28 d) |
| More: Dig deeper | `#/lexicon`, `#/communities[/id]`, `#/seedpath[/term]`, `#/seeds` | lexicon.jsx, people42.jsx, seedpath.jsx, seeds.jsx | lexicon, communities, seedpath endpoints | specialist |
| More: How 42 works | `#/coverage?date=` | Coverage `coverage42.jsx` | `GET /api/coverage[?date]` | per-market `series[]` (posts, located_share, calls_ok, valid, invalid_words), `credits.rows`, `model_spend`, `runs[]`, `scorecard`, `not_seen` |
| More: How 42 works | `#/fieldwork`, `#/method`, `#/schedules`, `#/skins`, `#/people/hidden`, `#/map` | various | various | operator and specialist pages |
| Not in menu | `#/console` (Build42 / ConsoleWorkbench) | build42.jsx, ConsoleWorkbench.jsx (197 KB chunk) | v2 investigations | "Build left the menu" (`rail42.jsx:46-47`) |
| Legacy redirects | `#/browse`→explore, `#/network`→communities, `#/board`→alerts, `#/source-lab`→fieldwork, `#/historical`→history, `#/listen/x`→seedpath | `legacyRoutes.js:27-33` |, | the desk API is not served by f42-api |

Phone layout (below 1024 px): a bottom bar with Today, Ask, Discover, Alerts and More. Investigations, Dossiers, History and every More page sit in the sheet (`rail42.jsx:61-62`).

**Divergence from EXPERIENCE.md "Navigation".** The spec rail is Today, Ask, Discover, Compare, Investigations, Dossiers, with History, Alerts and Coverage at the foot. In the build, Compare and Coverage are buried under More, and Alerts is promoted. `app/DESIGN_CONTEXT.md` is badly stale:

- It says there are "NO real URLs" and that the font set is Hanken Grotesk and JetBrains Mono.
- It gives the theme default as midnight.
- It routes through `/api/desk`.

`app/docs/capability-journeys.json` still names `src/api/desk.py` as the producer for Today and Discover. Gamma should not use these as specs.

---

## 2. Journey walkthrough and friction, ranked

Severity: **S1** means a strategist draws a wrong conclusion or the journey dead-ends. **S2** means significant comprehension or effort cost. **S3** means polish.

### Arrive: Today in the all-held state (what live shows now)

Screens: `screens/today-all-held-desktop.png`, `-mobile.png`, `-mobile-fold.png`, `today-all-markets-*.png`.

What a strategist sees, top to bottom:

1. "Today, 30 September 2026".
2. "Earlier brief … not today's brief".
3. Publication time, a warm-up meter, and "Some markets are incomplete".
4. A three-step guide: "1 Read what is moving below…".
5. A "Choose a past brief in History" link.
6. A three-column market glance (1 700 / 1 090 / 240 items collected, a located-share bar, and "0 trends cleared · 9 held back").
7. Market tabs.
8. A red-ruled notice: "No trend cleared our checks in South Africa on 30 September 2026. 9 are held back."
9. A coloured parts bar of held reasons.
10. Held items grouped by reason, each behind "Posts and figures".
11. "Left out", "Not assessed", On the boards, and Coming up.

| # | Sev | Friction | Evidence |
|---|---|---|---|
| T1 | S1 | **The empty day reads as failure, not as intelligence.** The dominant reason on the page is reader-side jargon: "The local why-now was not checked" (5 of 9). Others are "The cultural pattern has not been assessed" and "The explanation did not pass our checks". These are pipeline states, not reasons a strategist can act on. Nothing says what 42 *did* see, which held items are closest to clearing, or when they will be checked again. A strategist arriving on a 0-trend day gets no lead at all. | `core/api/today.py:128-135` (REJECT_* words), `today42.jsx:810-812`, `:1218-1251`; screenshot `today-all-held-desktop.png` |
| T2 | S1 | **Today runs its own gate on top of the server's.** `todaySpecificityView` (`today42.jsx:143-219`) re-validates every published card's specificity, claims and quotes, and drops any card that fails (`prepareTodayMarket`, `:221-236`). The only trace is a line: "6 trends the brief cleared are not shown here, because their example posts did not pass this page's own check" (`ClientHeld`, `:883-902`). With the populated fixture, **6 of 7 cleared ZA trends vanished** this way (`today-populated-fixture-desktop.png`). Two gates that can disagree make the count on the glance ("1 trend cleared") and the server's `cards` disagree. They also hide items from strategists without the item-level reason that held items get. | `today42.jsx:143-236`, `:883-902` |
| T3 | S2 | **The same market is chosen in three places:** the shell header "South Africa ▾" (`App.jsx:1068`), the glance buttons (`aria-pressed`, `today42.jsx:447`, `:582-616`) and the tablist (`:452-468`). They are synchronised, but on a phone the glance alone takes a full screen before the tabs. The header still says "South Africa" on the All tab while the glance shows all three. | `today-all-held-mobile-fold.png` |
| T4 | S2 | **The status area stacks four or five facts in 12 px grey** ("Earlier brief", Published, Warming up, incomplete markets, run receipt), plus a 14-segment meter with no label visible to sighted readers. On a stale day, "Earlier brief: 30 September … This is not today's brief" is the most important fact. It sits in the same small type as the publication time. | `today42.jsx:399-416` |
| T5 | S2 | **The first-visit guide is wrong on an empty day.** It says "Read what is moving below" when nothing is moving (`today42.jsx:649-665`). | screenshot |
| T6 | S2 | **The heading flips between "Taking off, …" and "Today, …"** depending on the selected tab, through a client-side regex (`today42.jsx:379`). The page's editorial voice depends on string surgery over server copy. | `today42.jsx:376-379` |
| T7 | S3 | "Not assessed" with no count reads as a dead control, and "Left out on 30 September 2026: 3 dropped since yesterday" is collapsed. Both are useful history signals, and both are hidden. | `today42.jsx:961-1019` |
| T8 | S3 | The held-reason parts bar uses five hues for categories, alongside market hues (orange/green/blue in the glance). EXPERIENCE.md says colour is reserved ("Red marks the one primary action"). The reasons are already listed in text below, so the bar adds colour, not information. | `today42.jsx:1218-1226`; `ui/Charts42.jsx` PartsBar |

### Discover something meaningful: Today populated, Discover, Topic

Screens: `today-populated-fixture-*.png`, `discover-*.png`, `topic-*.png`.

| # | Sev | Friction | Evidence |
|---|---|---|---|
| D1 | S1 | **Numbers on the same card contradict each other with no visible explanation.** One Discover card shows "**31** accounts posting, last 3 days", "58 posts in 3 days" and "**3** accounts posting, last 7 days, every source". Another shows 18 accounts in 3 days but 0 in 7 days. The Radar row above shows "5 posts, last 7 days" against "58 posts in 3 days". The explanation (different collection lanes) exists only in a `title` tooltip and `sr-only` text (`REACH_MEASURED_NOTE`, `REACH_EVERY_NOTE`), so mouse-hover only. Touch users and most sighted users never see it. | `ui/TrendCard.jsx:427-457`; `discover-desktop.png` |
| D2 | S1 | **Screens disagree on what is a trend.** For the same day and market, Today holds `#fixture_za_step` ("The cultural pattern has not been assessed"), while Discover lists it as a live Emerging trend with its explanation, and the Topic page shows it as Emerging with a full card. Today's held list (9 items) and Discover's "Held back" (6 items) are different sets. A strategist cannot tell which screen is authoritative. Today runs reader-side checks (`today.py` `_reader_held_item`, `:1056-1088`) that Discover does not. | `today-all-held-desktop.png` vs `discover-desktop.png`, `topic-desktop.png` |
| D3 | S1 | **Lifecycle words the data cannot support.** On warm-up day 2 of 14, Discover shows "Rising", with the rule "at least twice its usual level, on 2 days". It also shows a 5-step lifecycle marker (`TrendCard.jsx:372-390`). The same page states "Growth needs 14 days of data; showing reach only" (`discover42.jsx:50`). The Topic page states "Growth: needs 14 days of data". EXPERIENCE.md limits warm-up states to New to 42, Spike, On the boards, Seasonal, Recurring and Emerging. The client renders whatever `lifecycle` arrives, with no warm-up guard. | `discover42.jsx:50`, `ui/TrendCard.jsx:372-390`, `ui/StoryCard.jsx:135-136,219` |
| D4 | S2 | **The sparklines imply continuity and a rate.** They use a 7-day line with a filled area and a dotted bridge across uncollected days (`TrendCard.jsx:535-640`). The expected band is absent in all fixtures (`expected_low/high` are null), so there is no "usual" to compare against. The minimum to draw is 3 measured days (`MIN_CHART_DAYS = 3`, `:475`). On Today the same 31/58 figures and the same sparkline are drawn twice, in the lead panel (`ui/LeadPanel.jsx:69`) and the story card (`ui/StoryCard.jsx:220`). The headline mixes windows: "posted by 31 accounts in 3 days", above a 7-day "posts a day" chart, beside "Local examples: from the last 7 days … the counts above cover 3 days". | `today-populated-fixture-desktop.png` |
| D5 | S2 | **"Radar" is a table.** Growth against reach (the EXPERIENCE spec) is unavailable during warm-up. The honest fallback is a sortable table, which is good, but it keeps the name "Radar" and sits above the feed. Its column "Local: none known" next to "no flag" invites misreading. | `discover42.jsx:545-600` |
| D6 | S2 | **"Also popular outside South Africa" prints on every card** whenever `market_scope === 'global'` (`discover42.jsx:77-83`). With no figure behind it, it reads as a claim of cross-market spread. It should be a measured spread line. | `discover-desktop.png` |
| D7 | S2 | **Filter chips are marked up as tabs.** Kind and platform filters use `role="tab"` (`discover42.jsx:351`), and none of the Today or Discover tablists support arrow keys. History's tablist does (`history42.jsx:197-216`), so the pattern is inconsistent across screens. | grep evidence |
| D8 | S3 | The Topic page is mostly empty headings: Spread, Where it started, News, Daily posts by platform, Earlier waves all say "not measured yet". Six H2s of absence push the posts below the fold. | `topic-desktop.png`, `topic42.jsx` |

### Understand why it matters, and inspect evidence: Ask

Screens: `ask-empty-*.png`, `ask-answer-fixture-*.png`.

Ask is the strongest screen. It has:

- the question
- a meta line (market and window)
- "Mixed confidence"
- "Answer based on 2 posts by 2 creators · 214 posts read on 2 platforms"
- the short answer in Newsreader
- claims with confidence words and a segment shape (Corroborated ▬▬▬▬, Inferred ▬)
- inline post cards with timestamped transcript snippets
- "The numbers", with a unit and counting basis
- posts read by platform (including 0 for Instagram)
- "What it means for a brand" and "What to watch"
- "What we do not know"
- a red "Add to dossier"
- follow-ups
- a collapsed method

| # | Sev | Friction | Evidence |
|---|---|---|---|
| A1 | S2 | **The short answer runs past the "two or three sentences in Newsreader 21 px" contract.** It renders at about 30 px and is part bold, part regular, for roughly six lines on desktop. On a phone it is the whole first screen. | `ask-answer-fixture-mobile.png` |
| A2 | S2 | **The answer has no history context.** It cannot say "this is the second wave", "last time this pattern faded in 13 days", or "42 first saw it on X". There is no lineage of a claim across asks: History lists asks, but an answer never links back to earlier answers on the same item. | `ask42.jsx:379-600` |
| A3 | S3 | The same two evidence cards repeat under each claim, which doubles the scroll. | screenshot |
| A4 | S3 | **The empty Ask page is good.** It has suggested trend questions with their state, says "nothing is asked until you press Ask", and states the credit cost. However, its suggestions offer "Rising" items that Today holds (see D2). | `ask-empty-desktop.png` |

### Compare history: History, item history, Compare

Screens: `history-*.png`, `history-item-*.png`, `compare-*.png`.

| # | Sev | Friction | Evidence |
|---|---|---|---|
| H1 | S1 | **The analogue chart contradicts its own caption.** The copy says "Each is aligned on days since its wave began, read from day 4 onward", but `WaveTimeline` draws every wave on one absolute calendar axis. The axis is labelled "10 Sep … 30 Sep" while the data spans 10 September 2025 to 30 September 2026, so the year is missing. Analogues are listed with outcomes ("peaked 10 days later at 240 posts; fell below half its peak 13 days later") but with no similarity score, no basis for the match, no count of analogues considered, and no transfer limit. A strategist will read three outcomes as a forecast. | `history42.jsx:702-712` (copy), `:725-760` (WaveTimeline); `history-item-desktop.png` |
| H2 | S2 | **Figures conflict across screens.** History says "Peaked 30 September at 12 posts on the peak day, still going". On the same day, Today's and the Topic page's sparkline says "latest 19 posts a day", and Discover's radar says 5 posts in 7 days. There are three different post counts for one item and one day. | screenshots |
| H3 | S2 | **History is a list of records, not a timeline.** Asks, Briefs, Findings and Search are separate tabs. There is no per-item lineage joining "first seen → on Today → held → asked about → saved to dossier". The brief history only links to `#/today?date=`, which re-renders a whole past day. There is no diff between days (only the collapsed "Dropped since yesterday"). | `history42.jsx:216-380` |
| H4 | S2 | **"Read answer" can dead-end.** With the backend fixture store, two of three History asks returned 404 from `/api/ask/{id}`. Ids listed in history do not all resolve. This is fixture-dependent and unverified on live, but the UI has no recovery copy beyond a generic error. | `curl /api/ask/a_20260930_h2` → `not_found` |
| H5 | S3 | **Compare defaults to a 28-day window** (`compare-desktop.png`) while the engine is on warm-up day 2. Every line will be mostly "not collected". It also lists held items as addable without warning. | `compare42.jsx` |

### Coverage and uncertainty

Screens: `coverage-*.png`, `coverage-day-*.png`.

| # | Sev | Friction | Evidence |
|---|---|---|---|
| C1 | S1 | **The scorecard shows precision, recall and breadth (60/70/80 %) with no n and no interval.** It appears on the Coverage page for "today" (16 October), where it is a stale week (21-27 September) shown under an unrelated date. The `Figure` objects carry `n` (core/api/coverage.py:34-35), but the UI does not show it (`coverage42.jsx:556-640`). EXPERIENCE.md says a track-record chip appears "only after four weeks of scored data". Nothing in the client or server enforces that, and the server test even asserts the four-week wording is gone (`core/api/tests/test_coverage_scorecard_words.py:14`). | `coverage-desktop.png` |
| C2 | S2 | **Coverage is a separate destination under More.** The Today glance gives items collected and a located share, but per-day source health (for example "TikTok hashtag board: Not usable, 2 of 12 calls") lives two clicks away. There is no per-trend coverage ("this trend's evidence came from 2 of 5 usable sources"). | `today42.jsx:582-616`; `coverage-day-desktop.png` |
| C3 | S3 | **"Coverage, 16 October 2026" opens on a day with no collection.** It needs a link to the last day with data; one is present but secondary. Its "Model spend USD 0.00 of 50.00" makes an empty day look like a calm, healthy one. | `coverage-desktop.png` |

### Save and share: Dossiers, Watch, Alerts

| # | Sev | Friction | Evidence |
|---|---|---|---|
| S1 | S2 | **The only way to save is through an Ask answer.** Today's "Ask about this" goes to a draft question, which costs credits. Watch is the only save action on a trend card. You cannot add a trend card, a held item or a comparison to a dossier directly. The empty Dossiers page says this honestly ("Start a dossier from any finished answer on Ask"). | `dossiers-desktop.png`, `dossiers42.jsx` |
| S2 | S2 | **Sharing exists only for frozen dossiers** (`#/d/{id}/{ver}`). Topic and history-item URLs are shareable deep links, but there is no "copy link" affordance and no snapshot: a Today link with `?date` re-renders today's code over the stored brief. | `App.jsx:262-272` |
| S3 | S3 | **The feedback row ("Was this right? Real / Not real / Useful")** sits on every card at the same weight as Watch. It is reviewer tooling exposed to all strategists. | `ui/TrendCard.jsx:359` |

---

## 3. Accessibility and performance: measured vs inferred

### Measured

Local build, real API on fixtures, Chromium 1194 headless. Raw data is in `measurements.json`, `throttled.json`, `contrast*.json`.

| Check | Result |
|---|---|
| Horizontal overflow | 0 px at 390 and 1440 on every captured route (scrollWidth equals innerWidth). |
| Text contrast (composited, WCAG AA; large text at 3:1) | **0 failures** in daylight and midnight on Today held and populated, Discover, Coverage day and History item (128 to 297 text runs per page). Text links, focus outline (solid 2 px) and skip link are present. |
| Type sizes actually rendered | Today held: 11, 12 (242 runs), 14, 16, 18, 24, 40, 44, 52 px. Today populated adds 15, 30, 32, 64, 69, 72. EXPERIENCE.md says "four sizes only, 13, 16, 21 and 32". The design system defines `--type-1..7` = 11/12/14/16/18/24/32 plus clamps. **12 px is the dominant body size on Today**, which is below the spec's 13 px floor. |
| Targets under 24 px (WCAG 2.2 AA minimum) | Today held: 10. Examples: "Posts and figures" summaries at 117×18, the "Why each was held" link at 132×21. Compare has 13×13 radios. Alerts has 18×18 checkboxes (labels may extend the hit area). Coverage note marker is 12×16. |
| Targets under 44 px tall (the repo's own certification threshold, `frontend-certification.md`) | 10 on Today held at 390, 6 on Compare, 7 on Alerts, 6 on Discover, all at 390. |
| Keyboard | The first tab stops are the skip link, then the rail. Market tablists on Today and Discover have no arrow-key handling; History's does. The sparkline value probe is pointer-only (`TrendCard.jsx:610-620`). The SVG is `aria-hidden`, but a caption carries the latest value. |
| Live regions | 2 to 7 per page. Route changes are announced (`App.jsx` route label live region). The Today loading status is polite. |
| Unthrottled load (loopback) | LCP 200 to 770 ms on every route. DOMContentLoaded under 160 ms. |
| **Throttled load** (390 px, 150 ms RTT, 1.6 Mbps, CPU 4×) | **FCP about 6.9 s on every route, LCP 6.9 to 8.0 s, 1.22 to 1.26 MB transferred per cold route.** |
| What the bytes are | Fonts are about 1 000 KB of 1 260 KB: recursive-variable.woff2 **702 KB**, newsreader 210 KB, archivo 88 KB. JS is about 165 KB gzip on Today (index 54 KB, react-vendor 45 KB, today42 18 KB). CSS is about 74 KB gzip over 16 files. The whole JS output also contains globe 471 KB, chart 205 KB and ConsoleWorkbench 197 KB raw, all lazy-loaded and not on core routes. |
| Root cause of the 7 s first paint | `main.jsx:38-43`: `root.render(<App/>)` waits for `document.fonts.load()` of three faces plus Archivo. So the whole UI waits for about 1 MB of fonts on a cold visit. The repo's certification runs with "Throttling: none" (`frontend-certification.md`), so its 2.5 s LCP gate cannot catch this. |

### Inferred (not measured)

- Screen-reader reading order and the quality of announcements in VoiceOver or NVDA were not tested.
- Reduced motion is handled in code (`rail42.css:236-244`, `BackToTop`) but was not measured.
- CLS was not measured. The skeletons look designed to avoid it.

---

## 4. What the current UI cannot express that a history-aware layer needs

1. **Lineage of a trend.** Today has no "first seen by 42 on …, on Today N times, held on these days and why, renamed or merged from …". `topic42` "How it developed" is a two-row list of state changes. History item waves are post counts only. There is no field or component for merges or splits, alias history beyond "also seen as", or the reason a card was consolidated (2.4.0 now consolidates fragments, per `CHANGELOG.md`).
2. **Recurrence as a first-class state.** "Recurring" is a state word plus one sentence ("last wave peaked on 24 September 2025 at 240 posts"). There is no visual of wave N against wave N-1 on an aligned day axis, and no seasonality calendar tie-in, although Moments exists separately.
3. **Analogue outcomes with authority.** Similarity method and score, the basis of the match, the number of candidates considered, the spread of outcomes (not three anecdotes), and a transfer limit ("analogues from another market or kind"). The design system already ships `ProofHistoricalRun{similarity{method,score,authority},transferLimit}` (`types/ui/ProofRibbon.d.ts`) and `validateHistoricalRun`, and no live screen uses them.
4. **Coverage per day, per trend.** There is no strip of the last N days showing which sources were usable for this item, so a strategist cannot tell "quiet" from "not collected". The sparkline's dotted bridge is the only hint.
5. **Abstention reasons as intelligence.** A held item needs:
   - what was observed (the counts)
   - which check stopped it, in strategist words
   - how close it was to clearing
   - when it is next checked
   - whether it was held yesterday too (a streak)

   Today has reason text and a "Posts and figures" disclosure, but not distance to threshold, streak or next check. A 0-trend day should lead with the strongest held signals as "watching, not confirmed".
6. **Day-over-day diff.** "Dropped since yesterday" exists but is collapsed. There is no "new / moved up / held its place" view across the whole brief and no way to compare two brief dates.
7. **Confidence over time.** Claim labels (Observed, Corroborated, Single source, Inferred) are per answer. Nothing shows a claim strengthening or weakening across days, or a finding still holding: History findings carry the note "Whether a finding still holds is not checked yet".
8. **Track record with sample size.** Scorecard measures need n, the window, and weeks of scored data before display.
9. **One canonical number per concept.** The UI needs a way to name the counting lane visibly (boards and followed accounts vs every lane) and the window on every figure, instead of in `title` tooltips. That needs a shared "figure with basis" component.

---

## 5. What to keep

- **Honesty primitives.** Held-back with reasons, grouped and never silently dropped. "What we do not know". Unit and window on figures ("accounts posting, last 3 days"). "Not measured yet" instead of invented numbers. "Earlier brief: … This is not today's brief". `data-query-id` and `query_id` on every figure (provenance hooks).
- **Ask's anatomy.** It is close to EXPERIENCE.md and is the most trustworthy screen: confidence words with shapes, inline evidence cards with transcript timestamps, platform zeros shown, cost stated before asking, and nothing asked from a link.
- **Hash routes for every entity** (topic, history item, dossier version), plus legacy redirects that never strand old links.
- **The local rail.** It has named groups, a skip link, focus management, Escape to close, and a bottom bar on phones. It works around the five-job shell cleanly.
- **Coverage per source.** The tables (posts, located share, calls worked, Usable / Not usable with reason) are the right raw material for per-day and per-trend coverage.
- **Accessibility baseline.** It has zero contrast failures in both themes, a visible focus ring and live regions. The Playwright journey and certification suites (`tests/browser/*.pw.mjs`, `playwright.*.config.mjs`) are a strong regression net, and `scripts/gate-c-local-server.mjs` plus `core/api` with `F42_DATA=fixtures` make local review possible.
- **The DS StateView and loading skeletons** for route waits.

## 6. What Gamma should change (prioritised)

1. **One gate, one truth.**
   - Remove the client-side specificity re-gate (`today42.jsx:143-236`), or make the server the only gate and expose its verdict.
   - Make Today, Discover, Topic, Ask suggestions and Compare read the same admitted and held sets per day and market.
   - Show a single "status of this item on this day" everywhere: cleared, held (reason), or watching.
2. **Redesign the 0-trend day** as a first-class state: "Nothing cleared today. Here is what 42 is watching". For each of the top held signals, show:
   - the observed counts
   - the stopping check in plain words
   - its distance to threshold
   - how long it has been held
   - when it is next checked

   Lead with coverage health, so "nothing cleared" can be told apart from "we could not see".
3. **Figures with visible basis.** One figure component that always shows its window and counting lane in text. Never show two contradictory counts side by side without a visible reconciling line. Retire `title`-only explanations.
4. **Suppress charts and labels the data cannot carry.**
   - No lifecycle step marker, and no Rising, Peaking or Fading, before the engine has a baseline: enforce the warm-up word list in the client as a guard.
   - No sparkline without an expected band, or label it plainly "posts a day, no baseline yet". Do not draw the fill area.
   - Do not draw the same chart twice on Today.
   - Rename Radar until growth exists.
5. **A history-native item view.** Merge Topic and History item into one item page with:
   - a lineage timeline (first seen, Today appearances, holds, asks, dossiers)
   - waves aligned on day-since-start
   - analogues with similarity, n and transfer limit (reuse the DS `ProofHistoricalRun` contract)
   - a per-day coverage strip

   Fix the yearless axis and the caption-versus-chart contradiction now (`history42.jsx:702-760`).
6. **Scorecard discipline.** Show n and the window on each measure, hide a measure until n and weeks reach a threshold, and never show last week's scorecard under an unrelated date.
7. **Performance.**
   - Stop blocking first render on fonts (`main.jsx:43`). Render with fallback metrics and use `font-display: swap`.
   - Subset Recursive (702 KB) to the axes and glyphs used, and preload only the interface face.
   - Add a throttled profile to the certification suite.
8. **Market control.** One control. Keep the header scope or the tabs, not three.
9. **Type scale.** Reconcile EXPERIENCE.md (13/16/21/32) with the DS tokens (11 to 32 plus clamps). Pick the DS tokens, raise body text to 14 px minimum, and keep Newsreader to one moment per screen at the DS size.
10. **Save from anywhere.** Allow "Add to dossier" from a trend card or held item without forcing an Ask. Add copy-link on item and history pages.
11. **Keyboard and targets.**
    - Give all tablists arrow keys.
    - Use `aria-pressed` toggle buttons, not `role="tab"`, for filter chips.
    - Bring summaries, inline action links and checkboxes to 24 px minimum (44 px on touch, the repo's own gate).
12. **Docs.** Retire or rewrite `app/DESIGN_CONTEXT.md` and the desk-producer rows in `capability-journeys.json`. They contradict the shipped product.

## 7. Design-system constraints and reusable parts

- **Package.** `ogilvy-intelligence-design-system` 2.0.22 is vendored as a tgz (`app/frontend/vendor/`) with sha256 files and a manifest of hashes (`dist/manifest.json`). It is compiled: no source in the repo. Changes need a new package release.
- **`InstrumentShell`** accepts only `route: 'briefing'|'discover'|'compare'|'build'|'fieldwork'|null` (`types/ui/InstrumentShell.d.ts:20`). The app passes `null` for most routes and hides the job menu with CSS (`styles/rail42.css:127-128`). It uses the shell for masthead, market scope, theme and the utility "you are here" link (`App.jsx:177-219`, `:1060-1070`). Any Gamma navigation change happens in `rail42.jsx`, not the shell.
- **Type tokens:** `--type-1..7` = 11, 12, 14, 16, 18, 24, 32 px; `--type-8` = clamp(48 to 88 px); `--type-numeral` = clamp(40 to 64 px).
- **Fonts:**
  - Newsreader (editorial)
  - Recursive Sans (interface)
  - Recursive Mono (provenance)
  - Archivo 42 (the "Night Desk" display face, which is local, not from the DS)

  `scripts/check_design_tokens.mjs` with `design_token_baseline.json` gates new hard-coded values.
- **DS exports the live screens do not use.** Only `InstrumentShell` and `StateView` are used by the 42 screens; legacy screens use `InstrumentBriefing`, `ComparisonInstrument` and `EvidenceRoom`. These exports are available and unused:
  - `ProofRibbon` (strands by source family with receipt anchors, plus historical runs with similarity, authority and transfer limit)
  - `MeasurementBands` (velocity, novelty, breadth, independence, history and geo as low/moderate/high/**unmeasured**)
  - `HeldNotice`, `EvidenceLedger`, `EvidenceReadiness`, `Banner`, `MomentumPill`, `MarketChip`
  - `validateHistoricalRun`, `validateEvidenceSummary`

  `MeasurementBands` with "unmeasured" and `ProofRibbon` are the most direct fit for history-aware uncertainty.
- **Reusable local components** (`src/ui/`):
  - `Sparkline` (gap-aware, expected band, but see D4)
  - `PartsBar` and `StepMeter` (`Charts42.jsx`)
  - `FigureLine`, `Facts`, `EvidenceList` and `PostsShownNote` (TrendCard)
  - `StoryCard`, `LeadPanel`, `SearchingNow`, `TodayBoards`
  - `WatchDialog`/`useCardWatch`, `EvidenceChip`, `SourcePanel`
  - `AnswerExport`, `DossierReview`
- **Test harnesses Gamma should reuse:**
  - `scripts/gate-c-local-server.mjs` (static plus fixture API)
  - the `tests/browser/*.pw.mjs` fixture-boundary pattern (blocks writes and external hosts)
  - `core/api` with `F42_DATA=fixtures` and `UI_PASSCODE` set (which needs fastapi, starlette, uvicorn, httpx, google-auth, cryptography, pyyaml, jsonschema, sqlglot and google-cloud-bigquery to import)

## 8. Screenshot index

All are in `product-review/screens/`. Each has a full-page `*-{desktop,mobile}.png` (1440×900 and 390×844 viewports) and a viewport-only `*-fold.png`.

- `today-all-held`: the real API on fixtures, 0 cleared and 16 held. This matches the live situation.
- `today-populated-fixture`: frontend fixture today42.json.
- `today-all-markets`: the All tab in the held state.
- `ask-empty`, `ask-answer-fixture` (ask42_complete.json).
- `discover`, `topic`, `history`, `history-item`, `coverage` (today, no collection), `coverage-day` (30 Sep), `dossiers`, `compare`, `alerts`, `investigations`.
- Known artefacts:
  - thumbnails are blank because external images are blocked
  - in full-page captures, fixed bars appear mid-page
  - `coverage-desktop.png` shows the More sheet mid-fade after the scripted Escape key
