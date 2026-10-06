# V3 visual redesign, notes for Albert

Date: 3 July 2026
Branch: `feat/v3-visual-redesign` (15 commits), PR open to `feat/lp-r-staging`, not merged
Staging: `https://listening-post-staging-fibxg5ynpq-uc.a.run.app` (revision 00023, byte-matched to the PR head bundle)
Plan: `docs/2026-07-03-v3-visual-redesign-plan.md` · Direction doc: `frontend/src/v3-design-system.md`

## What changed

A shared primitive library now lives at `frontend/src/ui/`: ProgressRail, Skeleton,
PageShell/PageHero/PageAside, EmptyState, StatTile, MomentumPill, PlatformGlyph,
MarketChip, Toast, Banner. Every route consumes these instead of hand-rolled
one-offs, so the app finally reads as one product. `tokens.css` split into a raw
primitives tier and a semantic tier that references it, with a new `--data-mono`
face on all data numerals.

Each intelligence surface has its own accent lane, applied automatically per
route: blue for the desk routes, green for Seeds, yellow for Explorer, Discover,
Network and Lexicon, violet for Console. Your manual accent pick in the footer
still overrides everything globally. Shared chrome (masthead, ticker, footer)
stays neutral.

The headline feature: brief generation in the Console now shows a determinate
progress bar driven by the real backend stages, 15 percent at seed gathering, 40
at engine signal, 65 at Brand24, 90 at synthesis, 100 at completion, and only
moves when the poll actually reports a stage change. Degraded completions show a
distinct "ready, degraded" badge. Batch generation gets a rail per item. The
behaviour scan's one-line "Reading the signal..." text is now a skeleton shaped
like the layout that follows. Verified live on staging: the rail advanced
through the real stages and the finished doc rendered.

Loading states across the app went from spinner-plus-text to skeletons matching
the destination layout. Boot is a staged reveal instead of a flat fade. Route
transitions differ subtly by lane but share timing.

Real bugs fixed along the way: the dead-whitespace hero bug on Discover and
Explorer (545px text clamp inside a 1210px container, structurally fixed by
PageHero's flex/min-width rules), Browse rendering a literal `## All Signals`
markdown string, Network's dead-end empty state (now an EmptyState with a
market-switch CTA), Topics rendering all ~150 countries down to 0 percent (now
top 12 with a show-all toggle).

## What to eyeball on staging

1. Console, Build Brief: approve a behaviour, generate, watch the bar. This is
   the flagship.
2. Seeds (green), Discover/Explorer (yellow), Console (violet): does the lane
   coloring read as intentional zones or as noise to you?
3. Voices vs Topics side by side: Voices rows lead with handle and platform,
   Topics rows lead with the share-of-voice bar. Distinct enough?
4. Method page: its hero typography normalized to the shared PageHero scale,
   slightly smaller title and sub than the bespoke treatment it had. Reviewer
   judged it an intended consequence of primitive consistency, flag if you want
   the old scale back (one-line override).
5. Hard-refresh first. You caught a mid-rollout glitch during the deploy window
   (missing masthead); it is not reproducible on the settled revision, verified
   at 1440px and 1720px, both themes, scrolled and unscrolled.

## Known gaps, deliberately left

- StatTile exposes four tones only, so Topic story's momentum tile maps cooling
  onto the red "down" tone rather than the blue cooling hue the MomentumPill
  next to it uses. Directionally correct, not hue-exact. Extending StatTile's
  tone set would fix it properly.
- Topics' expanded country list renders unbounded (no scroll container) when
  "show all" is open. Matches the brief's ask, may read long on short screens.
- Network's fallback-market CTA rotates ZA and NG only, never suggests KE.
- Orphaned CSS: `.method-hero h1` and `.method-hero .lead-p` in app.css match
  nothing since the PageHero swap; `.research-progress-*` dot-list rules are
  likewise inert since the ProgressRail swap. Harmless, worth a sweep sometime.
- Batch item errors reuse the degraded badge styling with a Failed label and
  red border. Reads fine, slightly mixed vocabulary in one component.
- No JS unit test runner exists in this repo, so all task verification was
  build-plus-review plus the live staging pass; the lp_qa.py script covers the
  API side only.

## Process record

Executed as twelve tasks, one implementer and
one independent reviewer per task, fix cycles on four tasks (an accidentally
committed scratch file, two commit-message trailer violations, a momentum tone
regression), one false-positive bug claim from the audit correctly rejected
during implementation (Board never redirected when empty). Full ledger kept
locally.
