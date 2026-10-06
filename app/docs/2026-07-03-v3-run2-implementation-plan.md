# V3 run 2 implementation plan (Phase D)

Source: Phase C architect blueprint, 3 Jul 2026. Campaign law: docs/prompts/v3-redesign-REFERENCE.md. Direction: docs/v3-visual-redesign-notes.md (P0 list, B1-B5, C1-C3). Branch feat/v3-visual-redesign. Frontend only; zero API, BQ, or python changes in every task. House style everywhere. No new deps, no TypeScript, Chart.js stays.

Two audit corrections the blueprint verified in code:

1. topic.jsx loading copy sits at :105; an error branch already exists at :107-115 but is weak (no retry, single back link). P0-5 rebuilds the error branch; the permanent-loader symptom was a stale-instance artefact.
2. The doubled region code is flag-glyph fallback: FLAG emoji renders as regional-indicator letters on systems without flag glyphs, so flag plus code paints ZA ZA. Fix renders the code once; the tooltip keeps the country name.

## Global constraints (bind every task)

DATA_SPEC real data or hide. No fake progress; percent maps only to real PROGRESS_STEPS states with the campaign mapping. Masthead must not wrap. WCAG AA both themes (node scripts/check_contrast.mjs). Standalone routes stay standalone. Topic hero paragraph measure min(72ch, 100%); hero gap at 1280 under 80px by DOM probe. No em dashes, en dashes, or double hyphens anywhere. bun run build clean per task.

## Batches (deploy staging + critique after each)

Batch I: tasks 1, 2, 3, 4 (primitives and the progress contract)
Batch II: tasks 5, 6 (desk and topic dossier)
Batch III: tasks 7, 8, 9 (console workbench)
Batch IV: tasks 10, 11 (intelligence thread and the full-branch gate)

## Task 1: ProgressRail focus mode + forward-only clamp

Files: frontend/src/ui/ProgressRail.jsx, frontend/src/ui/ui.css.
Add prop focus (boolean, default false) to {status, steps, percent, label}. When focus: wrapper class .ui-progress-rail--focus with role="dialog" aria-label "Generating brief"; sibling dim layer .ui-progress-rail-dim with pointer-events none. No modal machinery, no focus trap. Percent text stays aria-live polite.
Forward-only clamp lives HERE, once, for all consumers: useRef high-water mark, shown = max(peak, clamp(percent, 0, 100)); render shown for fill width, aria-valuenow, and the percent text. Reset the ref when status transitions to 'starting' or empty (useEffect on status); keyed remounts cover batch items (researchLib.jsx:1024 key={id||i}).
New css: .ui-progress-rail--focus, .ui-progress-rail-dim. Bar transition keeps the reduced-motion guard at ui.css:292.
Tests: bun run build; node scripts/check_contrast.mjs; reason the clamp across starting->seeds->bq->brand24->synth->completed with a mid-sequence dip (never regresses); dim layer pointer-events none.
Risk: ref reset on a reused single rail across refine; explicit status-based reset covers it.

## Task 2: progress contract migration

Files: frontend/src/researchLib.jsx, frontend/src/ResearchDocPanel.jsx:228.
progressPercent (:603-614) rewritten: starting 5, gathering_seeds 25, gathering_bq 50, gathering 50, gathering_brand24 75, synthesizing 90, completed 100, completed_degraded 100, failed 0, default 0, with a prefix guard BEFORE default: String(status).startsWith('gathering_behaviour_') returns 50. Order: exact cases, then prefix, then 0.
'starting' is client-side: the gap between the 202 accept and the first real poll stage. ResearchDocPanel.jsx:228 seeds setProgressStatus('starting') instead of 'gathering_seeds'. batchItemRail (researchLib.jsx:994) seeds 'starting' instead of 'gathering_seeds' for running-with-no-status.
progressLabel (:570-579): explicit 'starting' -> 'Starting research'; gathering_behaviour_ prefix -> 'Reading market behaviours'.
progressStepIndex (:589-596): gathering_behaviour_ prefix -> 1; 'starting' stays -1 (bar at 5, no step current).
loadArtifactById :324 keeps 'gathering' (50), verify it does not read as a fresh 5.
Tests: bun run build; read-verify mapping; 'gathering' legacy alias stays 50 (refine + loadArtifact depend on it).

## Task 3: primitives FlowBar, MetaRail, SeedTrail, Skeleton hero

Files: create frontend/src/ui/FlowBar.jsx, MetaRail.jsx, SeedTrail.jsx; modify Skeleton.jsx, ui/index.js, ui/ui.css, tokens.css.
FlowBar props {step, steps, scope, status, cta}: steps [{key,label}] renders nothing when empty (DATA_SPEC); scope [{code, on, onToggle}] aria-pressed toggles; status idle|active|busy|done drives role="status" aria-live polite and CTA busy affordance; cta {label, onClick, disabled} real button, aria-disabled while busy. Classes .ui-flowbar (position sticky top 0; console has no masthead in researchMode, App.jsx:374-375), .ui-flowbar-inner, -steps, -step[data-state] (reuse the rail marker idiom ui.css:279+), -scope, -chip[aria-pressed], -cta, -count. tokens.css both themes: --flowbar-bg: var(--header-bg); --flowbar-line: var(--header-line); ONLY these two.
MetaRail props {items:[{label, value, tone?, mono?}]}: dl with dt/dd; tone up|down|accent|neutral maps to --up/--down/--accent/--ink and always pairs a glyph (triangle) with color; mono default true applies --data-mono + tabular-nums. Hide null/undefined/empty items; hide the rail when empty. Classes .ui-meta-rail (hairline separators via --hairline directly), -item, -label, -value[data-tone].
SeedTrail props {trail:[{label, route}], active}: nav aria-label "Seed trail"; chips are anchors href="#/..." with go() onClick so middle-click works; active chip aria-current="page"; nothing under 1 entry; past 5 collapse middle to an ellipsis chip expanding on click. Classes .ui-seedtrail, -chip, -chip[aria-current], -sep, -more; lane inherits via var(--accent), no hardcoded hue.
Skeleton hero variant: .ui-skeleton-hero container, three .ui-skeleton children sized by titleWidth ('60%'), subWidth ('40ch'), railRows (3); aria-hidden; existing shimmer keyframe (ui.css:233) and motion guards (:237-239). VARIANT_DEFAULT untouched for existing variants.
Dev QA route #/v3-lab allowed, gated import.meta.env.DEV, removed before PR.
Tests: bun run build; check_contrast (flowbar pair, both themes; meta rail tones); isolation render in v3-lab.
Risk: FlowBar sticky context inside the console scroll; MetaRail tone never color-only.

## Task 4: StatTile desk 26px + metric tile affordance

Files: frontend/src/ui/ui.css, frontend/src/today.jsx:212-219.
.metrics-row .ui-stat-value font-size 26px. .metric-tile-link class on the button branch only: hover background var(--surface-2), persistent trailing chevron or underline-on-hover, cursor pointer. Dead tiles unmarked.
Tests: bun run build; check_contrast; DOM-probe computed 26px and that exactly two tiles carry the affordance. Keep the da3889f baseline fix intact.

## Task 5: desk above-the-fold + boot sequence

Files: frontend/src/today.jsx, frontend/src/App.jsx, frontend/src/ui/ui.css.
CommandBar (today.jsx:183-204): padding 26px 30px 22px -> 16px 30px 14px; input height 44 -> 40; Try row padding 14px 4px 0 -> 10px 4px 0.
MetricsRow: marginTop 22 -> 16 (:229), minHeight 85 -> 78.
Lead band: wrap DigestStrip + LeadSignal (today.jsx:512-514) in div.desk-lead-band; ui.css grid minmax(0,5fr) 7fr gap 16px, collapse to one column under 1100. DigestStrip's internal 150px 1fr grid reflows in the narrow column; through_line measure holds min(72ch,100%). If LeadSignal's internal chart column gets too narrow inside 7fr, drop it at the band breakpoint.
Boot (App.jsx): remove the .gboot-sub "reading the signal" line (:370) entirely (no status-sentence tagline; if a tagline is kept it is the static brand line "Gen Z cultural intelligence"). Splash timers: setFading 1300 -> 700, setGone 2050 -> 1100; splash stays time-based, never desk-gated. DeskWait (App.jsx:53-63) composes Skeleton variant="hero" for the top band plus the existing metric/row skeletons.
Tests: bun run build; check_contrast; DOM-probe at 1280x900: LeadSignal top edge offsetTop under 900; no banned copy on .gboot; masthead no-wrap.
Risk: band collapse under 1100; deep links must not wait on desk fetch.

## Task 6: topic dossier hero + error + loader + 70ch

Files: frontend/src/topic.jsx.
Hero (:168-203) onto PageHero: eyebrow = flag/label/region line (:171-173); title = t.topic; sub = Gloss(t.why) (momentum pill leaves the sub row); actions = watch/copy/PDF buttons (:182-193). Right column: MetaRail with the metaTiles content (:148-158): engagement, share of voice, mentions, momentum (the ONLY toned item, with glyph), seed score. Drop the old full-width .m-2col metrics band (:196-203); no duplicate figures. MomentumPill at :176 goes.
Error branch (:107-115): EmptyState with title per missing vs failed, body from real message, cta Try again (reload), back link to #/topics kept above.
Loading (:104-106): Skeleton variant="hero" railRows={5} replaces SignalLoader + "Reading the topic…".
:208 maxWidth 70ch -> min(72ch, 100%).
Tests: bun run build; check_contrast; DOM-probe #/topic/music_amapiano at 1280: hero gap under 80px; bad id renders EmptyState with retry and back; momentum renders once.
Risk: MetaRail hides null seed score; keep the seed explainer prose below.

## Task 7: loader copy replacements

Files: behaviourScan.jsx:130, chat.jsx:313, chat.jsx:572, ConsoleWorkbench.jsx:265, seeds.jsx:207.
behaviourScan aria-label -> "Loading the ZA, NG and KE behaviour scan". ConsoleWorkbench askBusy row -> "Ask is running." (briefBusy branch already compliant; the row may later be superseded by FlowBar, copy fix is safe standalone). chat thinking bubble -> "Classifying the ask · drafting the answer". chat busy placeholder -> "Answering…". seeds loader -> "Loading today's seeds…". chat.jsx:525 decorative loader: leave untouched.
Tests: bun run build; grep diff for banned phrases and dashes; every string gated by its real state.

## Task 8: RegionSwitch doubled-code + Recent briefs labelling

Files: frontend/src/ConsoleWorkbench.jsx.
RegionSwitch (:122-137): remove the FLAG span (:133), render the code once; title keeps the country name. (If task 9 absorbs RegionSwitch into FlowBar scope, delete the component; scope chips render code only.)
normaliseRecent (:22): when json.title is empty, synthesize from real fields: persona_label or persona_id plus market codes via formatMarkets, fallback shortDate(created); keep honest "Research brief" only when persona AND markets are genuinely absent.
Tests: bun run build; DOM-probe region control renders each code once; recent briefs distinguishable; no invented titles.

## Task 9: console FlowBar wiring + approval collapse + focus mode

Files: ConsoleWorkbench.jsx, ResearchDocPanel.jsx, behaviourScan.jsx.
Mount FlowBar once above .workbench-stage (:246). landing: steps [{choose}], status idle, scope = market chips (absorbs RegionSwitch), no cta. ask: steps ask/answer; step 'answer' when the thread has an assistant turn; status busy on askBusy; cta Build cited brief when a thread exists. brief: steps scan/shape/doc; step from ResearchPage via new optional callbacks onFlowChange(step) and onApproveCount(n) (ResearchDocPanel.jsx:47, fired from existing scanDone/phase/approvedBehaviours effects); status busy on briefBusy; cta carries "{n} approved · Continue".
behaviourScan per-market collapse: top 5 expanded (backend order is ranked), rest behind "+ N more behaviours in XX" count row, local useState per market block; every row stays reachable; existing footer Continue stays as the real submit.
Focus mode: ResearchProgressSteps (researchLib.jsx:967) gains a focus passthrough; the single-doc running view sets focus while phase running in embedded console mode; batch rails stay unfocused.
Tests: bun run build; check_contrast (violet flowbar both themes); DOM-probe FlowBar pinned across all three modes, approve count visible above the fold, collapse works, generation dims the stage behind the focused rail.
Risk: do not change batch/consolidated flow logic.

## Task 10: intelligence thread

Files: seeds.jsx, discover.jsx, seedpath.jsx.
SeedTrail under each PageHero. Trail state from the hash params each route already receives (seedpath initialKeyword App.jsx:399, discover initialMarket :400); no new global state. Seeds: trail Seeds (active) -> Discover candidates chip. Discover: Seeds / Discover (active) -> Explorer chip; per-row Trace in Explorer buttons stay (discover.jsx:96-100). Explorer: Seeds / Discover / Explorer · keyword (active); Research in Console button stays (seedpath.jsx:125-129) via the existing alias (App.jsx:116) and initialQuery prefill (ConsoleWorkbench.jsx:145-146).
Eyebrows normalized: "Seeds · editorial activation", "Discover · graph proposals", "Explorer · keyword trace"; existing .eyebrow shape and accent dot.
Tests: bun run build; check_contrast lane inheritance; DOM-probe trail on all three, keyword chip on Explorer, console prefill route works; PageHero sub measure holds.

## Task 11: full-branch verification pass

No code. DOM-probe matrix at 1280 and 1440 both themes: topic hero gap under 80, desk lead in-fold at 1280x900, masthead no-wrap 1280 and 1024, 14 standalone routes mount with /api/desk blocked, no banned loader copy anywhere, no doubled region code, v3-lab removed. check_contrast both themes. bun run build. Any regression bounces to its owning task.
