# V3 visual redesign, run notes

Run date: 2026-07-03 (run 2, full re-run per docs/prompts/v3-redesign-REFERENCE.md)
Branch: feat/v3-visual-redesign (recreated off origin/feat/lp-r-staging; run 1 archived as feat/v3-visual-redesign-r1, notes at docs/v3-visual-redesign-notes-r1.md)
Baseline commit: 70cdcd4 (lp-r-staging head) + da3889f (metrics baseline fix) + 641005d (campaign docs)
Staging revision at start: listening-post-staging 00023

Plugin substitutions (4 of 7 designer plugins not installed in this session, /plugin unavailable non-interactively):
A3 prototyping-testing:evaluate -> visual-critique:critique-ux. B1 ux-strategy:strategize and B2/B3/D ui-design commands -> ui-ux-pro-max skill plus visual-critique:critique-color / critique-typography. E1 design-ops:handoff -> design-systems:documentation-template. frontend-design and senior-frontend stay banned per the brief.

## Skill log

| Step | Command | Ran | Substituted with | Output section |
|---|---|---|---|---|
| A2 | /visual-critique:critique-screen | Y | native | A2 critiques |
| A3 | /prototyping-testing:evaluate | Y | visual-critique:critique-ux | A3 evaluations |
| B1 | /ux-strategy:strategize | Y | ui-ux-pro-max + controller synthesis | B1 strategy |
| B2 | /ui-design:color-palette | Y | ui-ux-pro-max + contrast gate | B2 palette |
| B3 | /ui-design:type-system | Y | ui-ux-pro-max + controller synthesis | B3 type scale |
| B4 | /design-systems:tokenize | Y | native | B4 tokenize |
| B5 | /design-systems:create-component | Y | native | B5 component specs |
| C1 | /interaction-design:map-states | Y | native | C1 state maps |
| C2 | /interaction-design:design-interaction | Y | native | C2 interaction spec |
| C3 | /interaction-design:error-flow | Y | native | C3 error flows |
| D (per batch) | /ui-design:design-screen + /visual-critique:critique-screen | Y | architect blueprint + controller design specs before code; critique via DOM probes plus screenshot review after every deploy (00024, 00025, 00026, 00027) | D batches |
| E1 | /design-ops:handoff | Y | design-systems:documentation-template | E handoff |

## A0 code-explorer inventory

Explorer run 3 Jul 2026. Full findings:

### Loading states

| # | File:line | Surface | Type | Copy |
|---|---|---|---|---|
| 1 | App.jsx:365-370 (.gboot, app.css:279-308) | Boot splash to ~2.05s | Generic 4-dot bounce | "PULSE." + "reading the signal" FLAGGED |
| 2 | App.jsx:334 RouteWait (authReady null) | Pre-auth whole app | Generic card skeleton | none |
| 3 | App.jsx:380 DeskWait | Desk + non-standalone routes | Shape-matched skeleton (6 metric tiles + card + 5 rows) | none |
| 4 | App.jsx:388,394,455 Suspense fallbacks | Lazy route chunks | Generic RouteWait; line 455 has NO fallback | none |
| 5 | App.jsx:381-386 | Desk fetch error | Text + retry | "The desk could not load." |
| 6 | behaviourScan.jsx:130,184 | Console brief step 1 scan | Text loader, no skeleton | "Reading the signal across ZA, NG, KE…" FLAGGED |
| 7 | chat.jsx:313 | Ask thinking bubble | SignalLoader spinner (parts.jsx:558-565) | "Reading the signal · classifying · drafting" FLAGGED |
| 8 | chat.jsx:572 | Ask input while busy | Placeholder swap | "Reading the signal…" FLAGGED |
| 9 | chat.jsx:525 | Ask landing, decorative | SignalLoader with no fetch in flight | n/a |
| 10 | ConsoleWorkbench.jsx:263-266 | Workbench rail busy row | Text only | "Desk is reading the signal." FLAGGED |
| 11 | seeds.jsx:190 | Seeds body loading | Plain p, no skeleton | "Reading the signal…" FLAGGED |
| 12 | topic.jsx:105 | Topic story loading | SignalLoader, not shape-matched | "Reading the topic…" |
| 13 | researchLib.jsx:625-649 ResearchProgressSteps | Brief single-job generation | SignalLoader + step list, no rail | progressLabel(status) |
| 14 | researchLib.jsx:651-685 ResearchBatchProgress | Brief batch generation | Text list | "Brief N of M" |
| 15 | ui/ProgressRail.jsx | Shared determinate rail | LIVE, wired at researchLib.jsx:970 and :1030 via ui/index.js (explorer's dead-code claim was wrong, its ui/ dir reads failed mid-run; controller re-verified by grep) | n/a |
| 16 | ui/Skeleton.jsx | Primitive (text/metric/card/row/chart) | Consumers: App.jsx only | n/a |

### Progress flow

PROGRESS_STEPS researchLib.jsx:582-587: gathering_seeds/gathering_bq/gathering_brand24/synthesizing. progressStepIndex :589-596 exact match, 'gathering' aliases index 1, completed maps past end, UNKNOWN RETURNS -1 (renders nothing-started). progressPercent :603-614: seeds 15, bq 40, brand24 65, synth 90, completed 100, unknown floors to 0. 'starting' is NOT handled: label falls back to "Starting research", percent 0. Brief demands starting 5 / seeds 25 / bq+gathering 50 / brand24 75 / synth 90 / done 100. Flow: generateResearch POST -> pollResearchJob -> onProgress(j.status) -> ResearchDocPanel setProgressStatus (:56,168,187,204,258) -> ResearchProgressSteps -> ProgressRail (researchLib.jsx:970). Batch: generateResearchBatch :123-166 -> onBatchProgress {index,total,behaviour,status,phase} -> batchJobs -> ResearchBatchProgress -> per-item ProgressRail (:1030).

### Routes

Hash routing (router.js). ROUTE_LANES App.jsx:71-75: seeds green; seedpath/discover/network/lexicon yellow; console violet; rest blue. STANDALONE App.jsx:338 contains all 14 required routes. FLAG: listen is desk-gated though every sibling lens is standalone. pulse/browse/method/listen gated.

### Masthead

today.jsx:117-148. mast-row flexWrap nowrap minHeight 74 sticky; mast-nav minWidth 0 overflowX auto; mast-tools flexShrink 0; lockup flexShrink 0. Breakpoints tokens.css:310-336 (1340 sub-lockup drop, 680 nav own row, 600 ticker hidden, 420 metrics 2-col).

### Topic hero

topic.jsx:169-194 BESPOKE (not PageHero) grid 1fr auto. t.why paragraph carries min(72ch,100%) at :180 ad hoc; seed explainer :208 uses hardcoded 70ch (inconsistency). No 1280 breakpoint override. Most other routes use ui/PageHero (console :52, seeds :189, discover :154, creator :99, intel :384, lexicon :34, map :68, compare :157, seedpath :189, views :448, listen :52).

### Console workbench

One shell, ConsoleWorkbench.jsx:142-317. Rail (mode tabs :255-262, busy row :263-267, RecentBriefs :78-94 + RecentAsks :96-108), stage renders one of landing (WorkbenchStart :49-76), ask (ChatPage embedded), brief (ResearchPage embedded). Ask/Brief are full page components reused embedded, styling not unified.

### Desk composition

today.jsx TodayPage :465+: CommandBar :183-204, MetricsRow :226-233 (StatTile via MetricTile :212-219), DigestStrip :235-248, LeadSignal :250-289, BoardRow list :291-323, MarketTiles :325-355, PlatformHeat :360-387, ToneBar :393-429, PanAfrican :435-463. CountUp lives parts.jsx:530-549, reached via StatTile countUp prop; also intel.jsx:64.

## A1 screenshot matrix

78 shots captured off staging (revision 00023) via puppeteer-core headless
Chrome, authenticated, both themes at 1280 and 1440, fullPage except pulse.
Local corpus: session scratchpad `a1/shots/` (manifest.txt lists all). Naming:
`<route>_<width>_<theme>.png`. Routes: pulse, topics, topic-genz, voices,
seeds, discover, seedpath, seedpath-amapiano, console-brief, console-ask, map,
compare, network, lexicon, board, listen, browse, method, creator (click-
discovered), passcode (both widths, cleared storage). Supplemental from the A4
probe run: topic-real_1280_midnight.png (real topic id, resolved) and
pulse-real_1280_midnight.png (desk resolved past the skeleton).

Capture notes with audit value:
1. `#/topic/Gen Z lifestyle` (the literal route from the brief) never resolves:
   the topic page expects a topic id, a label 404s silently into a permanent
   "READING THE TOPIC…" loader. No error state exists on that page.
2. The pulse matrix shot caught the desk skeleton because a cold staging
   instance takes longer than 6s to first byte of /api/desk; the skeleton has
   no brand voice and follows a splash that says "reading the signal".
3. First matrix run produced 78 gate screens: storing the passcode without a
   document reload never re-runs the gate probe (hash navigation is same-
   document). Filed as a UX observation too: a user pasting a passcode into
   localStorage-restored sessions has no path back in without a hard refresh.

## A2 critiques

Run via /visual-critique:critique-screen (installed plugin), evidence: A1 matrix
shots at 1280 midnight plus daylight variants. Seven dimensions per screen,
merged P1/P2/P3.

### Pulse desk (pulse_1280_midnight.png, pulse-real_1280_midnight.png)

P1 issues:
1. Hierarchy: the desk's first paint is a full skeleton with no brand voice and
   no resolved content for many seconds on a cold instance; the boot splash
   before it says "reading the signal" in lowercase, the exact copy the brief
   bans on primary loaders. The two loaders stack (splash then skeleton), so
   time-to-signal feels doubled.
2. Density: above the fold at 1280 x 900 the resolved desk shows the command
   bar, six metric tiles and the digest strip, but the lead signal (the actual
   news) sits below the fold. A command centre leads with the signal, not with
   chrome.
3. Affordance: two of six metric tiles navigate (Voices, Topics) and four do
   not, with zero visual differentiation. Clickability is invisible.

P2 issues:
4. Composition: CommandBar hero, metrics row, digest strip and lead card are
   four separate full-width boxes with near-equal visual weight; the eye has no
   entry point. The desk reads as stacked widgets, the exact dashboard-template
   feel the brief bans.
5. Colour: momentum tones (up green, down red) appear only inside the board
   rows; the top half of the desk is monochrome surfaces, so the "live
   instrument" read only starts halfway down the page.
6. Typography: metric values (30px data-mono) outweigh the digest through_line
   (16px sans), inverting importance: the engine's one-sentence read of the day
   is typographically junior to raw throughput counters.

P3: ticker items at the top use 10px caps with 0.16em tracking that reads as
texture, not as information, at 1280.

### Console workbench (console-brief_1280_midnight.png, console-ask, fullPage)

P1 issues:
1. Density: Build Brief renders roughly 35 near-identical approval cards in one
   unbroken column (fullPage height over 5500px at 1280). No ranking cue, no
   grouping beyond market headers, no progressive disclosure. The approval
   decision the screen exists for is buried in repetition.
2. Hierarchy: the step banner ("See the behaviours first") is the only
   orientation device and scrolls away immediately; after one viewport the user
   has no idea where they are in the flow. The sticky footer CTA appears only
   at the very bottom.
3. Affordance: Approve and Reject are equal-weight ghost buttons on every card;
   the primary action of the whole surface (approve at least one, continue) has
   no primacy anywhere on screen.

P2 issues:
4. Brand: the workbench rail (Ask / Build brief / Recent) is visually a
   different product from the stage: different card language, tighter type,
   no lane accent on the rail itself. Ask and Brief stages are reused full
   pages with their own internal styling, so spacing and headers jump when
   switching modes.
5. Composition: the rail's Recent panels truncate titles at ~20 chars with no
   tooltip, and the busy row is plain text with no visual state.
6. Colour: violet lane is present on the step banner border only; the cards
   themselves are neutral, so the lane read disappears one viewport in.

P3: behaviour-card engagement figures use four different formats (8073 posts,
6.07M engagement, 45%, 9 platforms) with no shared numeral face application.

### Topic dossier (topic-real_1280_midnight.png, #/topic/music_amapiano)

P1 issues:
1. Composition: measured 340px of dead whitespace right of the hero sub at
   1280 (A4 probe: paragraph 720px inside a 1060px column). The dossier's
   opening move is emptiness. Brief demands under 80px.
2. Hierarchy: the hero carries only the h1, a badge row and one thin sentence
   while the page below is the deepest surface in the product (metrics band,
   dual charts, five-step Prompt Pulse, receipts, platform bars, voices,
   wall). The cover page undersells the report.
3. Density: the loading state for a wrong or slow topic id is a permanent
   centered "READING THE TOPIC…" with no error state, no retry, no way back
   (topic-genz shots). A dossier must fail like a dossier: named, dated,
   recoverable.

P2 issues:
4. Typography: the momentum word ("Building") renders twice in two different
   styles within one viewport (badge chip in hero, big serif word in the
   metrics band).
5. Consistency: the seed explainer uses a hardcoded 70ch while the hero sub
   uses min(72ch, 100%) (topic.jsx:208 vs :180); two measures three lines
   apart.
6. Colour: the metrics band is monochrome except the momentum word; seed
   score (26%) reads visually identical to share-of-voice (10.7%), though one
   is a readiness verdict and the other a market share.

P3: receipts rows and wall cards are solid; light polish only.

## A3 evaluations

Run as /visual-critique:critique-ux (substitute for prototyping-testing:
evaluate, plugin absent). Task-flow findings on the same three screens:

1. Desk: primary task is "what moved today"; the answer (lead signal) is below
   the fold at 1280 x 900 behind chrome (command bar, metrics, digest). Time
   to signal is one scroll plus two loaders.
2. Desk: no path from a metric tile to its meaning except Voices/Topics; the
   digest names topics but (pre-D0) rendered them as plain text, zero
   navigation from the day's summary.
3. Console brief flow: approve-then-generate needs the sticky footer, which
   only appears after scrolling 35 cards; nothing above the fold says what
   done looks like. Batch scope (which markets) is set at the top, outcome
   count at the bottom, no persistent state indicator.
4. Console ask: six recent briefs all labelled "Research brief", so reopening
   prior work is guess-based. Region pills render doubled codes ("ZA ZA").
5. Topic: no in-page anchor nav; reaching Prompt Pulse or the receipts means
   blind scrolling through a 4600px page.
6. Cross-cutting: Seeds, Discover, Explorer share no visible thread; Seeds
   names behaviours, Discover lists raw tokens, Explorer starts empty with no
   handoff from either. The intelligence loop exists only in the footer nav.

## P0 list (merged A2 + A3)

P0-1 Topic hero dead whitespace, measured 340px at 1280. Structural hero
     layout fix, not a width patch.
P0-2 Desk boot: splash copy "reading the signal" (banned) plus doubled
     loaders; replace with staged skeleton reveal, no banned copy anywhere
     (A0 rows 1, 6, 7, 8, 10, 11 list every instance).
P0-3 Console Build Brief approval wall: 35 identical cards, no hierarchy, no
     persistent flow state, sticky CTA invisible until page end.
P0-4 Progress mapping does not match the campaign contract: current
     15/40/65/90/100 with unknown stages flooring to 0; required starting 5 /
     seeds 25 / bq+gathering 50 / brand24 75 / synth 90 / done 100, plus
     gathering_behaviour_N (real backend stage, currently unmapped -> 0%).
P0-5 Topic page has no error state for a bad id: permanent loader.
P0-6 Discover renders unranked junk tokens as equal cards; needs tiering,
     cutoff and a quality floor presentation (engine-side cleanup exists on
     master, PR 228; the surface must still not present 25 identical cards).
P0-7 Intelligence thread invisible: Seeds -> Discover -> Explorer share no
     language, no cross-links in the body, no shared structure.
P0-8 Recent briefs unlabelled ("Research brief" x6) in Console rail; doubled
     region codes on the console region switch.
P0-9 Metric tiles: clickable and dead tiles indistinguishable (fixed for
     baseline zigzag in da3889f; affordance still open).
P0-10 Lead signal below the fold at 1280 x 900: the desk leads with chrome,
      not signal.

## A3 evaluations

## P0 list (merged A2 + A3)

## A4 topic hero gap probe

DOM probes on staging rev 00023, puppeteer headless Chrome, 3 Jul 2026:

1. Masthead: no wrap at 1280 (row height 74, no child top offset over 20px, no
   body horizontal scroll) and the same at 1024. PASS against the
   masthead-must-not-wrap non-negotiable at both widths.
2. Standalone routes with /api/desk blocked at the network layer: console,
   voices, topics, lexicon, map, board, compare, network, seeds, seedpath,
   discover, research all mount and render their own content. PASS. (creator
   and topic are param routes in the same STANDALONE set; topic verified
   separately by the hero probe.)
3. Desk resolve: after the skeleton, metrics land (first StatTile reads
   628.4k), resolved shot pulse-real_1280_midnight.png.
4. Topic hero gap at 1280, #/topic/music_amapiano (real id from /api/desk
   topics): gap 340px, FAIL against the under-80px rule. Paragraph resolves
   min(72ch, 100%) to 720px inside a 1060px hero column; the measure cap is
   correct, the hero LAYOUT strands the leftover 340px. Fix is structural:
   the hero must give the freed width to content (meta rail, actions, or a
   two-column hero) rather than leaving it empty. Screenshot
   topic-real_1280_midnight.png.

## B direction outputs

### B1 strategy (substitute run: ui-ux-pro-max reference search + controller
synthesis grounded in the P0 list)

The run-1 system (tokens, primitives, lanes) is sound. Run 2's thesis: the
product now has the right vocabulary but still speaks in widgets. Every P0
traces to one of three structural gaps, so the strategy is three moves:

Move 1, lead with the signal. Every surface opens with its intelligence, not
its chrome. Desk: the lead signal enters the first viewport (command bar
compresses to one row, metrics tighten, digest and lead share a two-column
band at 1280+). Topic: the hero becomes a dossier cover, the freed 340px
carries a meta rail (engagement, share, mentions, seed score) so the header
answers "why am I reading this" before the scroll. Console: the flow state
(step, scope, outcome) pins to the viewport, cards queue under it.

Move 2, one thread through the loop. Seeds, Discover, Explorer become
chapters of one investigation: shared eyebrow grammar ("SEEDS / DISCOVER /
EXPLORER · <chapter role>"), a shared SeedTrail breadcrumb primitive that
carries a keyword from any chip into Explorer and back, and body-level
cross-links (each Seed card names its Discover candidates; Discover rows
open the Explorer trail; Explorer offers "brief this" into Console). The
lane colors stay; the thread is structural, not chromatic.

Move 3, theatre for real state only. One progress grammar everywhere: the
ProgressRail percent contract moves to the campaign mapping (starting 5,
seeds 25, bq/gathering 50, brand24 75, synthesizing 90, done 100,
gathering_behaviour_N at 50) and the rail centres the stage during
generation (workbench dims, rail owns focus). All eleven "Reading the
signal" instances die; Tier 1 fetches get shape-matched skeletons with the
surface's name and date, never a sentence pretending to be status.

### B2 palette (substitute run; contrast gate is the arbiter)

No hue changes. The four lane families pass AA in both themes today
(check_contrast.mjs covers all four in both themes) and the audit found no
lane-legibility failure. Changes are usage rules only:
1. Momentum tones (up/down/building/cooling) are reserved for signal state;
   they never decorate chrome. The topic metrics band gets tone only on the
   momentum cell.
2. A lane accent appears exactly once above the fold as a structural marker
   (hero rule or eyebrow), then only on interactive emphasis. Console violet
   currently vanishes after the banner; the pinned flow bar carries it.
3. Digest rising chips use the up family soft-fill pattern (D0 fix aligns
   them with the gate).

### B3 type scale (substitute run)

Faces stay (Newsreader, Hanken Grotesk, IBM Plex Mono). Scale moves:
1. Dossier scale: topic h1 to clamp(44px, 4.8vw, 64px) matching the lead
   signal's confidence; hero sub stays min(72ch, 100%) but sits in a
   constrained text column so the cap never strands whitespace (P0-1 fix is
   layout, not measure).
2. Data numerals: all metric values, scores, percents, timestamps use
   --data-mono with tabular-nums (StatTile already does; topic metrics band
   and console engagement figures join it). One numeral format grammar:
   compact k/M/B via the existing human(), percents one decimal, dates as
   d MMM in mono.
3. The engine's sentence outranks counters: digest through_line at 20 to
   24px serif (D0 shipped) and the desk metric values drop from 30px to
   26px so the hierarchy reads sentence first, counters second.
4. Eyebrow grammar unified: 10.5px, 0.14em tracking, uppercase, one shape
   across all surfaces (several currently improvise sizes 9.5 to 11px).

### B4 tokenize (design-systems:tokenize, native)

Audited tokens.css semantic tiers (midnight :96-140, daylight :142-186)
against the B1-B3 direction. The two-tier system already carries everything
the redesign needs; the delta is four semantic names, all referencing
existing primitives, zero new hues, plus one component tier note:

1. --flowbar-bg: var(--header-bg); --flowbar-line: var(--header-line);
   the pinned console flow bar reuses the header pair so the pinned state
   reads as chrome, not content. Both themes already define the pair.
2. --meta-rail-line: var(--hairline); alias only if the dossier meta rail
   needs a distinct knob later; otherwise use --hairline directly. Decision:
   use --hairline directly, no new token (avoid rot).
3. Skeleton shimmer: .ui-skeleton (ui.css:217) already animates with motion
   and reduced-motion guards; no token needed.
4. Digest chips: covered by the D0 fix moving them onto class-declared
   --up-soft / --up and surface pairs the gate parses. No new token.

Net new tokens: --flowbar-bg, --flowbar-line (two aliases, both themes).
Everything else rides existing names. Migration: none, additive.

### B5 component specs (design-systems:create-component, native)

New or changed primitives for run 2, contracts stable once shipped:

1. FlowBar({step, steps, scope, status, cta}) new. Pinned console state
   strip: current step of a named flow, market scope chips, one primary CTA
   slot. Violet lane inherits from the console main. Renders nothing without
   a real steps array (DATA_SPEC).
2. MetaRail({items: [{label, value, tone?, mono?}]}) new. The dossier hero's
   right rail: label plus value pairs in --data-mono, hairline separators.
   Absorbs the topic hero's 340px. Reused by creator hero later.
3. SeedTrail({trail: [{label, route}], active}) new. The intelligence-loop
   breadcrumb: keyword journey chips shared by Seeds, Discover, Explorer.
   Yellow lane on Explorer, green on Seeds, inherits per main.
4. ProgressRail: percent contract change only (campaign mapping via
   progressPercent), plus a centre-stage mode prop {focus: boolean} that the
   brief-generation view uses to dim the workbench behind it. Rendering
   contract otherwise unchanged.
5. Skeleton: new variant 'hero' (title bar + sub bar + rail block) so topic
   and desk skeletons can be composed without bespoke divs.
6. StatTile: no API change; desk usage moves value size to 26px via the
   existing CSS (one rule), countUp stays.

Refined states and a11y per component (create-component pass):

FlowBar: states idle / active-step / busy / done. role="status" with
aria-live="polite" on step changes; CTA is a real button, disabled while
busy with aria-disabled; scope chips are toggles with aria-pressed. Hides
entirely when steps is empty or undefined. Keyboard: chips and CTA in tab
order, no focus traps.

MetaRail: purely presentational, a dl (dt label, dd value) so screen
readers get pairs; tone applies color only with the glyph carrying the
non-color signal (triangle up, not color alone). Hides items with null or
undefined value; hides whole rail when items resolve empty.

SeedTrail: nav element with aria-label "Seed trail"; chips are links (go()
on click, href="#/..." for middle-click); active chip aria-current="page".
Renders nothing for a trail shorter than 1. Truncates middle entries past 5
with an ellipsis chip that expands on click.

ProgressRail focus mode: when focus is true the wrapper gets role="dialog"
aria-label "Generating brief" semantics via the existing panel (no new
modal machinery), background dim is pointer-events none, motion respects
data-motion and prefers-reduced-motion (bar width transitions at 200ms or
none). Percent text is aria-live polite, announced on stage change only
(the poll dedup already guarantees this).

Skeleton hero variant: aria-hidden true like existing variants; sized by
props (titleWidth, subWidth, railRows), shimmer inherits existing keyframes
and motion guards.

## C blueprints and interaction specs

### C1 state maps (interaction-design:map-states, native)

Brief generation (single):
idle -> submitting (POST accepted, client enters 'starting', rail 5%,
label "Starting research") -> per poll tick, status moves through
gathering_seeds 25 -> gathering_bq or gathering 50 -> gathering_brand24 75
-> synthesizing 90 -> completed 100 (rail full, doc swap-in) or
completed_degraded 100 (distinct ready-degraded badge) or failed (rail
freezes at last real value, error panel, retry action re-enters
submitting). Guards: percent = max(prev, mapped(status)), forward-only
clamp lives in ONE place (the rail's caller state, not the component).
Unknown status: keep prev percent, label falls back to "Working" tier
label, never 0-reset mid-job. A failed poll tick is a skipped beat, state
unchanged (existing proxy-proof rule).

Brief generation (batch): outer state machine per item {queued, running,
done, error} feeding one rail per item; the batch header derives count
state (n of m done) from item states only, no synthesized percent for the
batch as a whole. gathering_behaviour_N maps to the 50 milestone of the
running item.

Ask chat: idle -> composing (input non-empty) -> sent (bubble committed,
busy placeholder ON input only, thinking indicator in-thread tied to the
real request) -> answered (sources rendered) or error (inline retry
bubble). No decorative spinner in the empty state (A0 row 9 dies): the
empty state becomes typographic suggestions only.

Desk load: booting (staged reveal, no sentence copy) -> authProbe
{open -> gate, stored -> deskFetch} -> deskFetch {skeleton command centre}
-> ready (preveal stagger, count-ups fire once) or error (retry panel,
standalone routes unaffected). metricsFetch is independent: tiles hold
reserved height, swap in when landed (existing rule), count-up only on
first land per session.

### C2 interaction spec (interaction-design:design-interaction, native)

Brief theatre focus mode: on generate, the doc area transitions 240ms
(var(--ease), scaled by --motion) into focus: the rail block centres in
the stage column, everything else in the stage drops to 40 percent opacity
and pointer-events none; the FlowBar stays interactive (cancel lives
there). Stage changes: bar width animates 200ms to the new milestone, the
step row's current item swaps its dot for the live pulse, completed steps
get a check glyph, label crossfades 120ms. Completion: bar reaches 100,
holds 300ms, focus releases (reverse transition), doc content reveals with
the existing preveal stagger. Degraded completion: same, plus the
ready-degraded Banner pinned above the doc. Reduced motion or motion=off:
all transitions become instant state swaps, no dimming animation (opacity
jump), rail width jumps.

FlowBar micro-interactions: mode tabs (Ask / Brief) are radio-style
buttons, active tab carries the violet accent underline; scope chips
toggle with aria-pressed and a 150ms fill transition; the approve counter
ticks with a single 150ms scale pulse on change (no count-up); primary CTA
disabled until its guard passes (at least one approval), tooltip on
disabled hover names the guard ("Approve at least one behaviour"). Cancel
during generation: click asks nothing, stops polling, marks job dismissed
client-side, rail freezes then collapses 200ms; the job continues
server-side and lands in Recent when done (matches the async
backend contract, nothing is killed server-side).

### C3 error flows (interaction-design:error-flow, native)

1. Brief generation failed: rail freezes at last real percent, step row
   marks the failing stage with the down tone, panel below states the
   failure in plain words plus a Retry button (re-POST, fresh job) and a
   "Keep the evidence" link when partial evidence exists (real payload
   check). No auto-retry.
2. Topic id invalid or fetch fails: EmptyState with the topic id shown,
   "Back to Topics" primary and "Retry" secondary. Never a permanent
   loader (P0-5).
3. Desk fetch error: existing retry panel, copy rewritten to name the desk
   and the date it last had ("The desk could not load. Retry, or open a
   standalone lens."), links to Topics and Seeds (both standalone).
4. Ask send fails: inline error bubble in-thread with retry; input
   preserves the failed text, never clears on error.
5. Passcode wrong: gate shows one plain line under the field ("That
   passcode did not open the desk."), input keeps focus, no shake theatre
   at motion=off.

Error-flow validation pass (interaction-design:error-flow): two gaps found
and added, one deferred.
6. 401 mid-session (passcode rotated): onAuth flips to the gate; the gate
   must preserve the route in the hash so re-auth returns to where the user
   was, and the run-1 observation stands: after storing a passcode the app
   must re-probe without a manual hard refresh (gate submit already does
   this via session bump; verify in D).
7. Batch partial failure: item-level error rows keep their error state in
   the batch list with a per-item retry; the batch header counts errors
   separately ("4 done, 1 failed"), completion of others is never blocked.
Deferred: poll timeout ceiling (a job stuck in one stage over N minutes)
is a backend contract question, out of frontend scope; the UI already
survives indefinite polling without lying (percent holds, no timer).

## D batches

| Batch | Surfaces | Design output | Deploy rev | Critique | P0s | Reviewer |
|---|---|---|---|---|---|---|
| D0 (early, Albert direct order) | Engine Digest strip on the desk | Spec below (controller-authored under the substitution rule; desk-wide design command still runs in the main desk batch) | 00024 | PASS, no P0 (see Batch I row) | none | Approved after 1 fix cycle (chip classes + gate) |
| I | Tasks 1-4: ProgressRail focus+clamp, campaign percent mapping, FlowBar/MetaRail/SeedTrail/Skeleton-hero, desk 26px + tile affordance | Plan tasks 1-4 (architect blueprint; design command substitution logged) | 00024 (listening-post-staging-00024-vz8) | critique-screen on the deployed desk: digest reads as an instrument panel (eyebrow rail + mono dateline + serif lead + chips + action row); metric baseline spread 0px; two link tiles carry the chevron affordance, dead tiles do not. No P0. P2 carried: neutral chips sit quiet against the panel (acceptable, gate-passed); primitives without consumers (FlowBar, MetaRail, SeedTrail) render nowhere yet so their visual critique lands with Batches II-III. | none | Task 1 approved after 1 fix (render-time peak reset); Task 2 controller-verified; Task 3 approved after 1 fix (busy CTA natively disabled); Task 4 accepted on evidence |
| II | Tasks 5-6: desk fold + boot, topic dossier hero | Plan tasks 5-6 | 00025-rsb | probes all PASS: hero gap 0, lead top 352, splash clean, bad-id EmptyState, no overflow 1101 | none | Task 5 approved after 1 fix (splash retiming); Task 6 approved, composition decision logged |
| III | Tasks 7-9: loader copy, region + recents, FlowBar wiring + collapse + focus | Plan tasks 7-9 | 00026 | probes all PASS incl live brief theatre (50 percent real stage, focus dim, completion to doc) | none | Task 7 grep-proven; Task 8 approved after 1 follow-up (flagless titles); Task 9 approved after 1 fix cycle (active-thread answer, guarded CTA, cancel) |
| IV | Task 9 fix wave + Task 10 SeedTrail + minor sweep | Plan task 10 + review findings | 00027-6n7 (final wave commits 2958635, 10e0581 deploy with the merge-gate wave) | task11.mjs 27-check gate ALL GREEN on 00027 | none | d3bc706 approved after 1 fix (9405f76 ask CTA guard); c76ec52 approved |

DOM verification on 00024 (headless probe): metricFontSize 26px, valueTops
all 301.0 (baseline spread 0), linkTiles 2, digest present with 8 chips of
which 4 rising, through_line font var(--serif). Shots
b1-pulse_1280_midnight.png, b1-digest_1280_midnight.png.

Batch II (tasks 5-6) deployed rev 00025-rsb. Acceptance probes, all PASS:
splash carries no status copy; lead signal top 352px (inside the 900px fold
at 1280); desk-lead-band present, no horizontal scroll at 1280 or 1101
(band columns 427/598 at 1101); topic hero effective gap 0px against the
under-80 rule (sub fills its text column, MetaRail 300px owns the right
side, subMaxWidth resolves min(576px, 100%) = 72ch at the sub's ch size);
momentum renders once; bad topic id renders EmptyState with retry, no
permanent loader. Shots b2-pulse_1280_midnight.png,
b2-topic_1280_midnight.png. P0-1, P0-2, P0-5, P0-10 closed.
Critique note on the deployed dossier: composition reads as a report cover
(title and one-line sub left, instrument rail right); the reviewer's
actions-placement question is resolved by evidence, actions sit quietly
under the title with no dead corner. Observation for the final pass: Prompt
Pulse section 3 renders an empty body for this topic; if the payload field
is genuinely empty the zone should hide per DATA_SPEC (candidate final-
review fix, not a batch blocker).

Batch III (tasks 7-9) deployed rev 00026. Probes, all PASS: FlowBar present
and sticky in all three modes with correct step states (landing choose,
ask ask:current/answer:pending, brief scan:current/shape/doc); scope chips
single codes ZA NG KE ALL, no doubled letters; approval collapse live
(top 5 per market, "+ N more behaviours in <MARKET>" toggles, 15 visible
rows from 29); recents carry persona plus date labels, zero "Research
brief" fallbacks. BRIEF THEATRE PROVEN LIVE: one real generation sampled
mid-flight at 50 percent with label "Pulling engine signal from BigQuery",
step states done,current,pending,pending, focus mode active (dialog plus
dim layer), ran to completion with the doc rendered. Signature moment 1
verified against the campaign percent contract. P0-3, P0-4, P0-8 closed.
Shots b3-console-brief_1280_midnight.png, b3-theatre_1280_midnight.png,
b3-doc_1280_midnight.png.
Minor carried to final review: six same-day same-persona recents read
identically (honest data limit; candidate: append HH:mm for same-day
duplicates).

### D0 design spec: Engine Digest

Problem: the current DigestStrip is a grey card with a red eyebrow and one or two
paragraphs. It reads as a footnote, not as the engine speaking. It also throws away
three fields the same daily_summary row already carries.

Direction (within the approved v3 system, no new tokens):

1. Structure: full-width instrument panel between the metrics row and the lead
   signal. Two-zone grid: a slim left rail carrying the eyebrow "Engine digest",
   a live pulse dot, and a dateline in --data-mono (the digest trend_date,
   real, labelled stale when older than the desk date); the body zone carries
   the editorial content.
2. Voice: through_line set in --serif at lead size (clamp 20 to 24px, line-height
   1.35, letter-spacing -0.01em), the single sentence the engine leads with.
   summary_text below it in --sans 14px muted, measure min(72ch, 100%).
3. Signal chips: key_topics and rising_topics render as MarketChip-style pills in
   a single row under the graf: key topics neutral surface, rising topics carry
   the up tone with a rising glyph. Each chip routes to its topic page. Rising
   chips ONLY when the backend row has them; no synthesis client-side.
4. Action line: call_to_action, when present, renders as a hairline-separated
   footer row, eyebrow "Action" in accent, text in --sans 13.5px ink-2.
5. Honesty: every zone renders only from its real field; a missing field removes
   its zone entirely (DATA_SPEC). No fabricated counts, no decorative metrics.
6. Motion: the panel participates in the desk preveal stagger as one unit; the
   pulse dot keeps the existing pBlink; no count-ups here (no numerals).
7. Both themes AA. Chips reuse existing chip classes so check_contrast.mjs
   coverage holds.

Data contract change (additive, LP backend only, same daily_summary read):
fetch_digest returns trend_date, through_line, summary_text, call_to_action,
key_topics, rising_topics using the same humanize_topic_refs + _topic_chip
treatment the verdict query already applies at bq.py:1293.

## E handoff

Run 2 complete. Branch feat/v3-visual-redesign, 30 commits on top of
lp-r-staging 70cdcd4, final staging revision recorded in the qa_log row of
the same date. Merge-gate review (whole branch): ready to merge, zero
must-fix findings, all minors consciously deferred with reasons below.

What shipped, by signature moment:
1. Brief theatre: campaign percent contract live (starting 5, seeds 25,
   bq/gathering 50, brand24 75, synthesizing 90, done 100, behaviour scan
   sub-stages at 50), forward-only clamp inside ProgressRail, focus mode
   dims the workbench during single-doc generation, cancel stops client
   polling and the job lands in Recent. Proven live mid-generation on
   staging.
2. Desk reveal: splash carries no status copy and clears at 1.1s into a
   shape-matched skeleton with hero band; lead signal enters the first
   viewport at 1280x900; digest is a two-zone instrument panel with mono
   dateline, serif through-line, key and rising topic chips, action row,
   every zone real-data-or-hide; metric tiles share one baseline and only
   navigable tiles carry an affordance.
3. Intelligence thread: SeedTrail breadcrumb under the hero on Seeds,
   Discover, Explorer, normalized eyebrow grammar, Explorer carries the
   active keyword chip, existing body cross-links kept (Trace in Explorer,
   Research in Console).
4. Topic dossier: hero rebuilt on PageHero plus MetaRail (engagement,
   share, mentions, momentum as the only toned item, seed score), gap 0px
   at 1280 against the under-80 rule, one momentum render, EmptyState with
   retry for bad ids, hero skeleton loader, 72ch measure everywhere.
5. Console workbench: one pinned FlowBar across landing, ask, brief
   (scope chips absorb the region switch, single codes, no glyph-fallback
   doubles); ask CTA and brief CTA always render with disabled state and a
   guard tooltip until their guard passes; approval wall collapses to top
   5 per market with count toggles; recents carry persona plus date
   labels.

How to QA in ten minutes: hard refresh staging, watch the boot resolve into
the desk (no status sentence, lead visible without scrolling); open the
top topic and check the hero rail fills the right side; open Console >
Build brief, approve one behaviour, continue, pick a persona, watch the
rail move only on real stages and the workbench dim; walk Seeds >
Discover > Explorer on the trail chips; flip both themes.

Deferred consciously (merge-gate triage): MetaRail mono flag gates only
tabular-nums (no consumer passes false); FlowBar findIndex cost is nil at
3 steps; faint-on-header-bg gate coverage is a pre-existing repo-wide gap;
same-day same-persona recents stay identical until a real distinguishing
field exists (HH:mm suffix is the logged candidate); the topic 30d change
row placement is a design choice; cancel-button CSS sits in the Task 10
commit (cosmetic history nit). Verify on this final revision: empty Prompt
Pulse opportunity zones hide (guard landed after the 00025 sighting).

Not in scope, unchanged: prod main, engine repo, BQ schemas, email mailer.
Merge path when Albert signs off: PR feat/v3-visual-redesign into
feat/lp-r-staging, then the existing staging-to-main flow.

## Post-completion critique-ux (Albert order, every element and sentence, rev 00028)

Corpus: fresh 78-shot matrix plus rendered innerText of all 19 routes on
00028. Confirmations: Prompt Pulse opportunity body now real on the live
topic (m5 resolved); no dashes and no junk placeholders in any rendered
copy; ticker, desk, dossier, console, thread all match the shipped intent.

P1 (fix before merge):
1. Voices rows render flag emoji beside the market code; on Windows
   without flag glyphs every row paints "ZA ZA" (Affordance/Brand; same
   glyph-fallback class fixed in the console). Fix: drop the emoji or
   render code once in creator.jsx row meta, same pattern as adad6b2.
2. Explorer platform trail renders duplicate platform rows (Threads x2,
   TikTok x3, Instagram x3, YouTube x3, Reddit x2, Web x2 on amapiano)
   (Density/Trust). Fix: dedupe trail rows by platform in seedpath.jsx
   render (keep earliest date, sum or max posts), or hide dupes; data
   comes from the API as-is so the dedupe is a render-side guard.

P2:
3. Explorer adjacent-terms chips include raw id tokens
   ("ucfvyld7qmnykomnikip0a0q") (Trust). Render-side filter: drop tokens
   over 15 chars with no vowels or mixed digit runs; engine-side cleanup
   exists on master (PR 228) but staging dataset still carries junk.
4. Brand name splits: "Nanobanana" (pulse seed explainer, seeds hero) vs
   "Nano Banana" (method, briefs). Pick "Nano Banana" everywhere (copy).
5. Seed explainer fragment "Higher seeds better." reads broken (copy).
   Candidate: "A higher seed is a stronger bet."
6. Digest action line can double the market ("Target Sapa hustle in
   Nigeria in Naija"): Gemini output, engine-side prompt nit; log for the
   engine repo, not fixable here under DATA_SPEC.
7. Voices board: 40 rows, one template, no tier break after the top
   handles (Density). Candidate: rank-tier separators at 10/25 or a
   top-10 emphasis treatment.

P3: ticker duplicate innerText from the aria-hidden marquee copy row
(harmless, AT-hidden); voices row activity bars lack a scale cue; board
page and network empty-state CTAs could cross-link the thread pages.

Biggest functional risk: the Voices doubled-code render on Windows
machines, because it hits every row of a primary board in the exec demo
path.

## Round 2 refinement (5 Jul 2026, PR #67, staging rev 00039-lms)

Round 2 tuned the built instrument against the second design handoff bundle; no new tokens,
fonts, hues or dependencies. Ten items, five commits, squash-merged to feat/lp-r-staging.

Desk aside: rebuilt as two cards. The Ask card carries the rail's only filled accent button;
the digest card runs trend_date (mono, right) then through_line as an 18px/600 serif hero,
summary_text, two DigestList tiers (In play with square bullets, Rising with the green up-caret),
and call_to_action reframed as the THE PLAY inset (left 2px accent border on accent-soft, a div,
never a button). New primitive frontend/src/ui/DigestList.jsx; null fields remove their blocks.

Console: the recents rail row leads with a 15px mono time spine, demotes persona to a serif
context line, and carries market pips, the monochrome confidence ramp (3/2/1 pips on
ink/muted/faint, never a hue) and the short artifact_id hash as the same-minute tiebreaker.
The theatre now resolves 100% into a landed beat (check ring, doc title preview, scope and
citations, Open the brief) before the doc develops in via the v3develop blur-to-sharp entrance.

Topic dossier: both mid-page charts are instrument readouts. The header holds the big serif
reading with unit, window, market and a momentum or relative tag; the numeric y-axis labels are
gone (the honest figures live in the header), the x-axis carries four real dates ending Today,
and each chart states its honest caption.

Emptiness as material: the four round-1 loaders each own a surface through the shared EmptyState
primitive in parts.jsx. ScanSweep is Discover's designed empty week, Orbit is Network's
thin-market state, SignalRings is the Board's first look, LogDrum is Listen's pre-rows beat with
the sentiment split gated on loaded rows. All freeze static, still visible, under data-motion off
and reduced motion.

Shell: the theme and accent control moved into the masthead as the ThemeDock (segmented moon/sun,
hairline divider, four Google swatches, same pulse-dir2 and pulse-accent writes), collapsing to an
icon popover below 1101px so the mast-row never wraps; the bottom-bar accent picker is gone. The
delta ticker renders on the Desk route only.

Verification: 17-probe DOM suite 34/34 both themes; 19-route sweep both themes clean of overflow,
console errors and failed requests (the /api/__idle__ sentinel 404 predates round 2); bundle UI
strings dash-free; contrast AA both themes; vite build green. A pre-existing 768px sample-wall
bleed on the dossier (nowrap footer span) was fixed in the follow-up QA PR.
