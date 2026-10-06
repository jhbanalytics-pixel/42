# Seeding journey, design + spec (2026-06-22)

A full seeding journey woven through the PULSE tool as a first-class lens in the
intelligence layer, parallel to "Follow". 100% clickable, explainable,
explorable, accurate end to end, consistent with the emailer.

Decided (Albert): store the seed components for an exact breakdown; full reach
(Pulse rail + board toggle + chat tool + clickable breakdown); email stays as is
(chip + legend + seed_recommend topline already shipped).

## The journey

Discover -> Understand -> Activate.

1. DISCOVER (Pulse landing): a "Seeds to activate" rail of the top seed-score
   trends, plus a Trending|Seeds toggle on the board to re-rank by seed.
2. UNDERSTAND (everywhere the seed shows): the SEED % is clickable and opens a
   breakdown of the real drivers (audience fit, format fit, brand safety) with
   the actual numbers. Explainable and accurate.
3. ACTIVATE (topic story): the existing Prompt Pulse, reframed as the Seed Kit
   (tool + idea + copy-ready Nanobanana/Lyria prompt, already built).

## Part A, engine (trends-engine-v2): store the seed components

The breakdown must read the same stored numbers everywhere, so the engine
persists the decomposition, not just the final score.

- `scripts/run_rss_now.py`: refactor so a pure `_seed_breakdown(...)` returns
  `{seed_score, audience_fit, format_fit, safety_gate, visual_audio_share}`.
  `compute_seed_score(...)` keeps returning the float (calls `_seed_breakdown`).
  `compute_trend_scores` writes all five onto each `trend_scores` row.
- `infra/bigquery_schemas/trend_scores.sql` + idempotent migration
  `add_seed_components_columns.py`: add `seed_audience_fit`, `seed_format_fit`,
  `seed_safety_gate`, `visual_audio_share` (all FLOAT64, nullable).
- `scripts/backfill_seed_score.py`: write the components too; re-backfill 22-Jun.
- Tests: `_seed_breakdown` returns each component correct for the worked
  example; `compute_trend_scores` rows carry the components; the float wrapper
  is unchanged.

## Part B, tool API (listening-post): expose the breakdown + a seeds tool

- `src/api/bq.py fetch_desk_rows`: SELECT the four component columns plus the
  atom columns already present (`genz_score`, `slang_score`, `engagement_score`,
  `creator_spread`, `tone_score`) so the breakdown can name the drivers.
- `src/api/desk.py _build_topic`: add a `seed_breakdown` object on the topic:
  `{audience_fit, format_fit, safety_gate, visual_audio_share, genz, slang,
  engagement, creators, tone}`. `None` when seed is absent.
- `src/api/topic.py`: pass `seed_breakdown` through.
- `src/api/chat.py`: a `get_seeds(market)` tool that returns the desk pre-filtered
  to the top seed trends, sorted by seed desc, each with seed and the tool fit;
  plus a system-instruction line so "what should we seed" uses it.

## Part C, tool frontend (listening-post): the clickable journey

All seed surfaces become clickable and consistent.

- `frontend/src/seed.jsx` (new): a small shared `SeedBreakdown` popover/panel
  component, fed the `seed` + `seed_breakdown`, rendering audience fit, format
  fit and brand safety as labelled bars with the real numbers, plus the
  one-line meaning. Reused by the lead chip, the tile, and the rail cards.
- `frontend/src/today.jsx`:
  - a `SeedsRail` section after "Emerging this week": top seed trends (seed >=
    floor, default 0.0 so it never reads empty, sorted seed desc), each card
    clickable to open the topic story; the SEED % on each card opens the
    breakdown; a header with the plain-language meaning and the tool split.
  - the lead SEED chip becomes a button that opens the `SeedBreakdown`.
  - a `Trending | Seeds` toggle above the board that re-sorts the board rows by
    `seed` desc when Seeds is active, and swaps the displayed score for the seed.
- `frontend/src/topic.jsx`: the seed tile becomes clickable to open the
  breakdown; keep the existing explainer; the Prompt Pulse stays as the Seed Kit.

## Accuracy + consistency

One source of truth: the engine computes and stores the components; the tool and
chat read them; the email already renders the same seed_score. The backfill
writes the components for 22-Jun so the live tool shows real numbers today.
Every seed number shown anywhere traces to the stored `trend_scores` row.

## Visuals

Reuse the PULSE signal-desk language and components: the rail mirrors "Emerging
this week"; the breakdown bars reuse the tone/sentiment bar styling; the toggle
reuses the market-toggle pill style; "Seed Kit" keeps the Prompt Pulse layout.
No new aesthetic, the seed lens wraps the existing one.

## Testing (TDD)

- engine: `_seed_breakdown` components, persisted columns, backfill components.
- tool api: `seed_breakdown` shape in `_build_topic`; `get_seeds` ordering +
  filter; null-safety when seed absent.
- tool frontend: build-clean; live-verify the rail, the clickable breakdown, the
  board toggle, and the topic tile breakdown on the deployed site.

## Out of scope

No email changes (chip + legend + topline already shipped). No new aesthetic. No
change to the trend score or its ranking.
