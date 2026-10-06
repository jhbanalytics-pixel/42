# PULSE V3 design system

Status: direction approved by Albert 2026-07-03. Phase 1 in progress. This doc is the
single source of truth for tokens, primitives, and per-route treatment across the
redesign. Update it as each phase lands; do not let it drift from the code.

## 0. What changed from the audit

Phase 0 screenshot audit (all 26 routes, 1280px, both themes) found:

- The product already has a strong, consistent type and spacing language (serif display
  headline + sans small-caps label + generous whitespace + hairline borders). The
  "internal dashboard" read comes from six routes (Topics, Discover, Voices, Console
  landing, and the sitemap/map cards) reusing one rounded-card-with-progress-bar
  template regardless of what data they hold. Six other routes (Creator profile, Topic
  story, Compare, Lexicon, Method, Listen) already read as distinct product moments.
  V3 brings the first group up to the bar the second group already sets, rather than
  starting from zero.
- Every loading state site-wide is the same `SignalLoader` + "Reading the X..." text,
  including research/brief generation, which already has a real 4-stage backend state
  machine (`gathering_seeds -> gathering_bq -> synthesizing ->
  completed`) sitting unused behind a step-dot list. This is the highest-leverage fix:
  map real stages to a real percentage instead of building progress from scratch.
- `.page-hero-sub` (`max-width: min(72ch, 100%)`) reproduces its dead-whitespace bug on
  Discover and Explorer at 1280px (664px of empty space beside the filter pills), but
  not on Seeds or Topic story, so the fix has to be systematic, not a one-off patch.
- `#/board` always redirects to Discover instead of rendering an empty state.
  `#/network` shows a bare "No creator-topic links in this market yet" with no CTA.
  `#/browse` renders a literal `## All Signals` (unstripped markdown). Topics renders
  ~150 country rows down to 0% with no top-N cutoff.
- The three unused theme-picker swatches (red/yellow/green) are the seed of the accent-
  lane system below; a fourth (violet) was added for Console. See token addition in
  `tokens.css` and `scripts/check_contrast.mjs` (four families, both themes, all AA).
- The apparent "router hijacks navigation" behavior reported mid-audit was traced to
  three concurrent chrome-devtools sessions sharing one browser profile during the
  audit, not an app bug: no `setInterval` exists anywhere in `frontend/src`, and every
  hash write traces to an explicit click handler or the documented research-to-console
  alias redirect. Verified clean with a single session (`#/network` held 3s untouched;
  leaving Console via direct hash change resolved and stayed at `#/pulse`). No routing
  fix is in scope.

## 1. Typography

Keep the existing pairing, it is the strongest asset in the current build:

- Display / headline: `--serif` (Newsreader). Push it harder on hero moments than it
  currently is used. It is already good, V3 just uses it with more confidence (larger,
  tighter tracking) on the lead signal, topic story h1, and creator profile h1.
- Label / UI: `--sans` (Hanken Grotesk), small-caps eyebrow style already defined as
  `.eyebrow` in `tokens.css`. Unchanged.
- Data / instrument: `--mono` (IBM Plex Mono) is the monospace data face for metrics,
  timestamps, and score digits, sharpening the "instrument" read against the editorial
  serif. In `tokens.css` it is the real mono stack (`'IBM Plex Mono', 'JetBrains Mono',
  ui-monospace, SFMono-Regular, Menlo, monospace`) and `--data-mono` is kept as an
  alias of `--mono` so the `StatTile` / `ui.css` call sites that reference it keep
  working unchanged. New code should reference `--mono` (or the `.mono` utility class);
  apply it to `.tnum` contexts and `StatTile` values, tabular-nums throughout.

## 2. Color lanes

One token file, four accent families already defined for the picker
(`[data-accent="red|yellow|green|violet"]`, blue is the base). V3 repurposes them as
per-surface lanes instead of a user preference:

| Lane | Family | Surfaces |
|---|---|---|
| Desk | blue (default, no override) | Pulse, Topics, Voices, Compare, Listen, Map, Method, Board |
| Growth | green | Seeds |
| Graph | yellow (amber) | Explorer (seedpath), Discover, Network, Lexicon |
| Console | violet | Console workbench (all three modes), Ask |

Shared chrome (masthead, footer, ticker) stays neutral and never carries a lane color.
Implementation: wrap each route's `<main>` content in a container that sets
`data-accent` locally (CSS attribute selectors already cascade this way; no JS state
needed beyond what the picker already does). The existing theme-picker widget keeps
working as a manual global override for users who want one accent everywhere; lane
colors are the *default* when no manual override is set, not a replacement for user
choice. Verify with `node scripts/check_contrast.mjs` after any hue tuning, it now
checks all four families in both themes.

## 3. Motion system

Keep `.preveal` / `data-motion` / the `pulse-motion` toggle exactly as they are (four
levels: off/calm/balanced/lively, `--motion` CSS var scales animation-duration
multipliers, all already wired and `prefers-reduced-motion`-aware). V3 adds on top:

- Boot sequence: extend `.gboot` (currently a Google-dot bounce + wordmark fade) with a
  staged reveal (wordmark -> tagline -> first paint) rather than a flat 1.8s fade. Same
  timer-based unmount, same reduced-motion override that already forces `opacity: 0
  !important` on the splash.
- Route transitions: `.route-enter` exists on `<main>` already (`App.jsx:327`) but is
  unstyled beyond whatever `app.css` gives it. Give each lane a distinct but
  same-duration entrance (desk: fade+rise, graph surfaces: fade+scale-from-98%,
  console: slide-from-right, matching its workbench framing). All variants share timing
  so the app never feels inconsistent in *pace*, only in *character*.
- Chart draw-in and count-up: apply to `StatTile` (new primitive, see below) and the
  existing `charts.jsx` line/area components. `CountUp` already exists in `parts.jsx`
  (used on `intel.jsx`), reuse it, do not reimplement.

## 4. Loading and progress — the state machine

Two tiers, chosen by whether the surface is backed by a real job or a simple fetch:

**Tier 1 — simple fetch (desk, list, card grid).** Replace the plain
`SignalLoader` + text pattern with a shape-matched `Skeleton` (see primitives) sized to
the route's actual final layout: metrics row + lead card + board rows for Pulse, dual
chart panels for Topic story, card grid for Lexicon/Discover/Voices. `SignalLoader`
itself is kept as the small indeterminate mark used *inside* a skeleton or button, not
as the sole loading UI for a full page.

**Tier 2 — real job (research brief, behaviour scan, consolidated batch).** The backend
already reports `gathering_seeds -> gathering_bq -> synthesizing ->
completed` via `pollResearchJob` (`researchLib.jsx:90`) and already exposes
`progressStepIndex` / `PROGRESS_STEPS` (`researchLib.jsx:581-595`). Map those states,
plus the client-side `starting` gap before the first poll response and the
`gathering_behaviour_*` per-behaviour scan sub-states, to a fixed, honest percentage
rather than animating smoothly between polls (the campaign mapping, `progressPercent`
in `researchLib.jsx`):

```
starting                    -> 5%
gathering_seeds             -> 25%
gathering_bq                -> 50%
gathering                   -> 50%   (legacy alias, refine, loadArtifactById)
synthesizing                -> 90%
completed                    -> 100%
completed_degraded           -> 100%
failed                        -> 0%
gathering_behaviour_*        -> 50%   (prefix match, checked before default)
```

The new `ProgressRail` primitive renders this as a determinate bar plus the existing
step list (reuse `PROGRESS_STEPS` labels), with the percentage only ever moving forward
on an actual status change from the poll, never on a timer. `completed_degraded` maps to
100% with a distinct "ready, degraded" badge rather than a different percentage. Behaviour
scan (`behaviourScan.jsx`) and consolidated batch (`ResearchBatchProgress`,
`researchLib.jsx:974`) get the same treatment: batch already tracks `phase` (`queued /
running / done / error`) per item; render as a `ProgressRail` per item inside the
existing list rather than the current plain dot-and-label row.

No page gets a fake smooth-fill animation that does not correspond to a real poll
response. If the API ever collapses to `pending|running|done` with no finer stages, that
maps to fixed milestones (20/60/90/100) per the brief's own instruction, not an
animated guess.

## 5. Component library — `frontend/src/ui/`

New directory, built once, imported everywhere. Names and contracts (props are the
contract other phases build against, keep them stable once Phase 1 ships):

- `ProgressRail({status, steps, percent, label})` — determinate bar + step list + ETA
  slot. `percent` is derived by the caller from the status-to-milestone map above, not
  computed inside the component (keeps the honesty rule enforceable in one place per
  caller).
- `Skeleton` — variants `text`, `metric`, `card`, `row`, `chart`, sized via props, not
  separate components, so a page can compose its own skeleton layout from one import.
- `PageShell`, `PageHero`, `PageAside` — layout primitives replacing the inline-style
  wrappers currently duplicated per route (e.g. `creator.jsx:246`'s inline `maxWidth:
  'var(--maxw)'` block). `PageHero` owns the `.page-hero-sub` width fix: full column
  width up to `min(72ch, 100%)` is the rule already stated in the brief, but the
  *component* enforces it structurally (the sub-text and any adjacent action row share
  a flex parent with `min-width: 0` on the text side) so the Discover/Explorer bug
  class cannot recur route-by-route.
- `EmptyState({title, body, cta})` — typographically strong, no illustration, replaces
  bare text like Network's "No creator-topic links in this market yet" and gives Board
  a real empty state instead of the current unconditional redirect to Discover.
- `StatTile`, `MomentumPill`, `PlatformGlyph`, `MarketChip` — small data-display atoms,
  pull shared logic (momentum label/color mapping, platform icon set, market flag +
  code) out of the per-page duplication currently in `today.jsx`, `topic.jsx`,
  `creator.jsx`, `intel.jsx`.
- `SignalLoader` — already exists in `parts.jsx`, kept as-is, just re-scoped to Tier 1
  inline use per section 4 above rather than full-page loading.
- `Toast` / `Banner` — inline error and confirmation surfaces, replacing any bare
  `alert()`-shaped patterns (audit found none in current code, this is forward cover
  for Phase 2-4 work, not a fix for an existing violation).

A dev-only `#/v3-lab` route may exist during Phase 1-4 build for visual QA of these
primitives in isolation; gate it behind `import.meta.env.DEV` and remove before the
Phase 5 PR.

## 6. Per-route mandate

Carried from the brief's own table, annotated with the Phase 0 finding for each route.

| Route | File | Current state | V3 angle |
|---|---|---|---|
| `#/pulse` | `today.jsx` | Clean, already has metrics row + lead card + board; the template this redesign is closest to already | Command centre: lean into what is there — skeleton it properly, tighten the lead-signal hero, keep the board as-is structurally |
| `#/topics` | `intel.jsx` | Ranked list, reused card template, unfiltered ~150-row country table | Ranked intelligence board; cap country breakdown to top 10-15 with a "show more" |
| `#/topic/:id` | `topic.jsx` | Already the deepest, strongest page (evidence wall, numbered Prompt Pulse, receipts) | Preserve structure, apply lane/typography polish only, this is a reference page not a rebuild target |
| `#/voices` | `creator.jsx` (board) | 40-row list, no pagination, reused card template | Influence ranking with the same card language as Topics, differentiated by lane only |
| `#/creator/:handle` | `creator.jsx` | Already strong: dark hero band, Read/Move split, numbers grid | Reference page, light-touch only |
| `#/seeds` | `seeds.jsx` | Editorial, evidence-driven, hero paragraph already full-width (no `.page-hero-sub` bug here) | Green lane, keep structure, tier badges get `MomentumPill` |
| `#/discover` | `discover.jsx` | Reused card template, confirmed `.page-hero-sub` bug | Yellow lane, apply `PageHero` fix, differentiate card type from Topics/Voices |
| `#/seedpath` | `seedpath.jsx` | Minimal, confirmed `.page-hero-sub` bug, otherwise near-empty canvas | Yellow lane, apply `PageHero` fix, needs real designed empty/trace states |
| `#/console` | `ConsoleWorkbench.jsx`, `behaviourScan.jsx`, `ResearchDocPanel.jsx`, `chat.jsx` | Landing/Ask already visually calmer than Build Brief's dense approval list; highest-value `ProgressRail` target | Violet lane end to end; Build Brief's real job states get the full Tier 2 treatment |
| `#/map` | `map.jsx` | Clean, consistent card grid, the best current reference for the sitemap pattern | Light-touch, this is already close to the bar |
| `#/compare` | `compare.jsx` | Already distinct: dual-slot picker + shared chart | Light-touch |
| `#/network` | `network.jsx` | Bare empty state, no CTA, on ZA at least | `EmptyState` primitive with a market-switch CTA |
| `#/lexicon` | `lexicon.jsx` | Already distinct, dense unique grid | Light-touch |
| `#/board` | `board.jsx` | Currently unconditionally redirects to Discover when empty | Real `EmptyState`, stop the redirect |
| `#/listen` | `listen.jsx` | Already distinct, search-landing minimalism | Light-touch |
| `#/browse`, `#/method` | `views.jsx` | Browse has an unstripped `## All Signals` markdown artifact; Method is pure editorial and fine | Fix the markdown artifact; Method light-touch |
| Passcode | `passcode.jsx` | Already uses `SignalLoader`, simple centered form | First-impression polish only, low risk given it gates everything |

## 7. What is explicitly not changing

Per the brief: no backend/API changes beyond what progress polling already returns, no
BQ query changes, Chart.js stays, no TypeScript migration, no merge to `main` without
sign-off. The router "fix" originally scoped for this branch is dropped, see section 0.
