# Console as Intelligence Superpower Center

Date: 1 July 2026  
Owner: Albert  
Repo: Listening Post (`listening-post` Cloud Run)  
Status: superseded by `docs/2026-07-01-console-intelligence-superpower-plan-v2.md`  
Related: `docs/2026-06-30-console-research-trust-design.md`, `docs/jo-research-acceptance.md`, commit `c149588` (premium `#/research` UI)

## Problem

Jo's original ask was the Intelligence Console as a strategist research desk. We shipped the trust layer on a separate `#/research` route: persona registry, evidence graph before Gemini, numbered citations, quality gate, refine, persist, share links. Desk chat on `#/console` stayed frozen (`chat.py` untouched).

T will live in the console. Two routes for one job creates friction: the team bounces between ask and research, footer and rail advertise them as siblings, and the console empty state only points at quick asks while the Jo-grade deliverable lives elsewhere.

We need one Intelligence Superpower Center without a mega-rewrite, without breaking the daily Ask on Pulse, and without moving research through the chat tool loop (6 rounds, 90s poll, timeout mess).

## Goals

1. One mental model: `#/console` is where strategists ask *and* generate cited market research docs.
2. Preserve the full trust layer exactly as shipped: evidence before Gemini, quality gate, citations, refine, persist, share, rate limits, Brand24 degrade path.
3. Keep `chat.py` frozen for multi-turn analyst ask. Keep `/api/ask/brief` and the Pulse command bar untouched.
4. Keep `#/research` share links and Seeds deep links working forever (redirect, not break).
5. Reuse the premium research UI from `c149588`; do not rebuild doc rendering.

## Non-goals

1. Merging research synthesis into `chat.py` or exposing research as a chat tool.
2. Free-text persona creation in the UI (personas stay YAML, engineering-owned).
3. Replacing persona-driven research with "ask the model to write a brief" in the chat loop.
4. DOCX export, week-over-week diff, or new data sources (Semrush, Wikipedia) in this workstream.
5. Changing Trends Engine V2 ingestion or cron.
6. Removing `#/research` URLs in the first release (alias only).

## Patterns and conventions found

| Pattern | Where | Implication |
|---------|-------|-------------|
| Hash router, lazy routes | `frontend/src/router.js`, `App.jsx` | Modes can be query params on `#/console` without a new route file |
| Console = signal desk layout | `chat.jsx` L106-L527 | Left rail (240px sticky) + main column + sticky input dock |
| Research = full-page doc scroll | `ResearchDocPanel.jsx`, `app.css` L435+ | Research needs native window scroll, not viewport-locked chat column |
| Async job + poll (not chat loop) | `researchLib.jsx` L68-L97, `research.py` L16 | Research already avoids chat timeout; keep it on `/api/research/*` |
| Chat async job + poll | `chat.jsx` L39-L79, `chat.py` MAX_TOOL_ROUNDS=6 | Ask stays single-turn poll; never chain research here |
| Legacy redirect | `App.jsx` L92-L100 | `#/console?persona_id=` already redirects to `#/research`; extend, do not delete |
| Trust pipeline frozen | `docs/2026-06-30-console-research-trust-design.md` | Backend split `chat.py` vs `research.py` is intentional |
| Daily Ask separate | `ask.jsx`, Pulse `CommandBar` in `today.jsx` | Third surface; out of scope except "do not break" |
| Shared doc components | `researchLib.jsx` | `DocBody`, `CitationDrawer`, `ResearchRefineBar`, poll helpers: extract, do not duplicate |
| Premium UI landed | commit `c149588` | Form presets, outcome preview, toolbar: mount as-is inside console shell |

## Architecture decision

**Unified console shell with explicit mode switch (Ask | Research doc), single left rail, shared masthead context.**

Research stays on `research.py` endpoints. Ask stays on `chat.py`. The frontend composes both inside `#/console` rather than routing strategists to a sibling page.

`#/research` becomes a permanent deep-link alias that sets `mode=research` on the console shell (same component tree, same CSS tokens). Share URLs `#/research?artifact=ra_...` redirect to `#/console?mode=research&artifact=...` once Phase 1 lands; old URLs keep working via `App.jsx` redirect shim.

We do **not** use an LLM intent router in v1. Mode is explicit (rail tab or CTA). Optional intent hint in Phase 3 only as a *suggestion chip*, not auto-routing.

### Why not other options

| Option | Verdict |
|--------|---------|
| Empty state CTA only | Too thin; T still leaves console for every doc |
| Thread handoff only | Good Phase 3 add-on; needs shell first |
| Split pane day one | Right goal for Phase 2; too much layout risk before mode switch works |
| Intent router (auto) | Research needs persona + markets + product frame; free-text routing would bypass trust layer or misfire |
| Merge into chat.py | Breaks frozen seam, reintroduces 6-round timeout, loses evidence graph contract |

### UX model

```
Intelligence Console (#/console)
├── Rail
│   ├── Mode: Ask (default)
│   ├── Mode: Research doc
│   ├── New conversation / Generate doc (contextual primary)
│   ├── Thread list (Ask mode) OR Recent artifacts (Research mode)
│   └── Optional: link from Ask thread → "Open as research doc" (Phase 3)
├── Main (Ask mode)
│   └── Existing chat bubbles + sticky ask dock (unchanged behaviour)
└── Main (Research mode)
    └── Research controls + doc panel + citation drawer (from ResearchDocPanel.jsx)
        Window scroll, not chat column scroll
```

**Timeout isolation:** Ask mode keeps 90s chat poll. Research mode keeps 95s research job poll (`POLL_DEADLINE_MS` in `researchLib.jsx`). They never share a poll loop. Abort on mode switch (same pattern as `chat.jsx` L315-L320).

## Component design

### 1. `ConsoleShell` (new, extracted from `chat.jsx` + `ResearchDocPanel.jsx`)

**Path:** `frontend/src/ConsoleShell.jsx` (name flexible)

**Responsibilities:**
- Parse `mode` from hash query: `ask` (default) | `research`
- Render shared rail with mode tabs
- Mount `ChatPane` (extracted from current `ChatPage` body) or `ResearchPane` (extracted from `ResearchPage` body)
- On mode change: abort in-flight polls, update URL via `history.replaceState` (preserve artifact/persona params)
- Set `data-screen-label` for a11y: "Intelligence Console · ask" vs "Intelligence Console · research"

**Dependencies:** `router.js`, `researchLib.jsx` URL helpers (extend for `#/console?mode=research`)

### 2. `ChatPane` (extract from `chat.jsx`)

**Responsibilities:** Everything in current `ChatPage` except rail header and Market Research nav button. Unchanged API: `/api/chat/send`, `/api/chat/status`.

### 3. `ResearchPane` (extract from `ResearchDocPanel.jsx`)

**Responsibilities:** Persona picker, markets, generate, doc body, refine, citation drawer. Unchanged API: `/api/research/*`. Remove standalone "Back to Console" button when embedded; rail provides mode switch.

**Dependencies:** All exports from `researchLib.jsx` unchanged.

### 4. `App.jsx` routing shim

**Responsibilities:**
- `route === 'console'` → `<ConsoleShell region={...} />`
- `route === 'research'` → redirect or render same `<ConsoleShell initialMode="research" />`
- Extend legacy redirect (L92-L100): `#/research?*` → `#/console?mode=research&*`
- `researchMode` chrome flag: hide ticker/masthead/footer when `mode=research` OR keep lightweight console chrome (decision in Phase 1: match current research chrome, no ticker)

### 5. Backend (no changes in Phases 0–2)

| Module | Stays | Does not absorb |
|--------|-------|-----------------|
| `chat.py` | Multi-turn analyst ask, tool loop, Brand24/BQ tools | Research evidence, persona registry, artifact persist |
| `research.py` | Evidence gather, quality gate, job orchestration | Chat history, conversational turns |
| `synth.py` | `synthesize_research`, `refine_research_prose` | Chat turn synthesis (separate path today) |
| `persona_registry.py` | Resolve, score, gate | Chat persona inference |
| `/api/ask/brief` | Pulse daily ask | Console or research |

## Integration options ranked

| Rank | Option | Effort | Value | When |
|------|--------|--------|-------|------|
| 1 | Empty state + rail CTA → switch to Research mode in-console | S | Gets T to the doc flow without leaving | Phase 0 |
| 2 | Mode tabs in rail (Ask \| Research doc) | M | One superpower center | Phase 1 |
| 3 | Recent artifacts in rail (Research mode) via `GET /api/research/recent` | M | Jo-style desk history | Phase 2 |
| 4 | Split pane: Ask left, doc right after generate | M–L | Power users, compare ask vs doc | Phase 2 (desktop only) |
| 5 | Thread handoff: "Generate research doc" from chat with persona/markets prefill | L | Connects exploration to deliverable | Phase 3 |
| 6 | LLM intent router on console input | L | Deferred | Not v1 |

## Data flow

### Ask mode (unchanged)

```
User input → POST /api/chat/send → poll /api/chat/status
  → chat.py tool loop (max 6 rounds) → answer + source pills
  → localStorage threads (pulse-chat)
```

### Research mode (unchanged backend)

```
Persona + markets + product_frame
  → POST /api/research/generate (202, rate limited)
  → poll /api/research/status
  → research.build_research_evidence → persona_registry.quality_gate
  → synth.synthesize_research → persist GCS/BQ
  → ResearchPane renders DocBody + citations
  → refine → POST /api/research/refine → new artifact_id
```

### Phase 3 handoff (future)

```
Chat thread last user message + desk market
  → heuristic map to persona_id (keyword → persona alias, no LLM required v1)
  → switch mode=research&persona_id=...&markets=...
  → user confirms markets, taps Generate (trust layer unchanged)
```

## Migration path from `#/research`

| URL | After Phase 1 |
|-----|----------------|
| `#/research` | `#/console?mode=research` (replaceState redirect, bookmark updates) |
| `#/research?artifact=ra_x` | `#/console?mode=research&artifact=ra_x` |
| `#/research?persona_id=digital_architect_genz&markets=ke,ng` | same params on console |
| `#/console?persona_id=...` (legacy) | Already redirects; retarget to console mode=research |
| Seeds `buildResearchDeepLink` | Update to `#/console?mode=research&persona_id=...` |
| `researchShareUrl()` | Emit console URL with mode param; accept both loaders |

Keep `#/research` route registered for 6+ months. Render path: alias only, no duplicate UI.

Footer (`App.jsx` FOOTER_COLS): collapse "Research" link into "Console" label or rename column entry to "Intelligence Console" pointing at `#/console`. Masthead NAV (`today.jsx` L11): no second Research item.

## Implementation map

| File | Change |
|------|--------|
| `frontend/src/ConsoleShell.jsx` | **Create.** Mode state, rail, abort on switch |
| `frontend/src/chat.jsx` | Extract `ChatPane`; rail moves to shell; rename kickers to "Intelligence Superpower Center" or keep "Intelligence console" |
| `frontend/src/ResearchDocPanel.jsx` | Extract `ResearchPane`; export both for shell; keep `ResearchPage` as thin wrapper for alias route |
| `frontend/src/researchLib.jsx` | Extend URL helpers: `parseConsoleUrl`, `setConsoleResearchUrl`, dual-path share URL |
| `frontend/src/App.jsx` | Wire `ConsoleShell`; research alias; footer copy |
| `frontend/src/seeds.jsx` | `buildResearchDeepLink` → console mode URL |
| `frontend/src/app.css` | Console research layout: `.console-research` scroll rules; split pane grid (Phase 2) |
| `docs/jo-research-acceptance.md` | Update route references after Phase 1 |
| `chat.py`, `research.py`, `main.py` | **No changes** Phases 0–2 |

## Build sequence

### Phase 0: Wayfinding (S, ~0.5 day)

Ship first if Albert wants immediate signal before shell refactor.

- [ ] Console empty state: secondary CTA "Generate a cited research doc" → `go('/console?mode=research')` or interim `go('/research')`
- [ ] Rail kicker copy: "Ask · Research doc" subline under Intelligence console
- [ ] Footer: single Intelligence Console link; remove duplicate Research entry or make it redirect
- [ ] Map page (`map.jsx`): Console desc mentions research docs

**Acceptance:** T opens console, sees research CTA without hunting footer. Daily Ask on Pulse unchanged (smoke: one brief generate).

### Phase 1: Unified shell (M, ~2 days)

- [ ] Extract `ConsoleShell` with Ask | Research mode tabs in rail
- [ ] Mount existing chat and research bodies; no backend edits
- [ ] URL contract: `#/console?mode=research&artifact=&persona_id=&markets=`
- [ ] `#/research` alias → same shell (`initialMode=research`)
- [ ] Hide ticker/masthead/footer in research mode (match current `researchMode` behaviour)
- [ ] Abort in-flight job on mode switch
- [ ] Update `buildResearchDeepLink`, share URLs, Seeds button
- [ ] Mobile: Research mode full-width stack (rail collapses to top tabs, same breakpoint patterns as `app.css` console rules)

**Acceptance (Jo/T):**
1. From `#/console`, one click to Research mode; no separate "Market Research" page feel
2. Full Jo acceptance checklist passes with URL `#/console?mode=research` (generate, citations, refine, share, 429)
3. Ask mode: FOLLOW/SEED desk chat unchanged; thread persist works
4. Pulse Ask: command bar still opens brief panel
5. Old `#/research?artifact=` links load doc in console shell

### Phase 2: Desk history + split pane (M, ~2 days)

- [ ] Research mode rail: `GET /api/research/recent` list (API exists, UI stub in CSS only today)
- [ ] Desktop optional split: toggle "Pin doc" opens doc beside ask (min-width 1200px); doc column uses window scroll
- [ ] Region selector: research markets independent of desk region (already true); show desk region badge in ask mode only

**Acceptance:**
1. Recent artifacts visible in rail after two generates
2. Split pane: generate in research mode, switch to ask, return without losing artifact
3. No regression on phone (split disabled below 1024px)

### Phase 3: Thread handoff (L, ~3 days)

- [ ] Chat bubble action: "Build research doc" on assistant turn (when persona match confidence high)
- [ ] Heuristic persona map from chat keywords → `persona_registry` aliases (read-only API or static frontend map from `/api/research/personas`)
- [ ] Prefill markets from console `region` prop
- [ ] No auto-generate; user must tap Generate (rate limit respect)

**Acceptance:**
1. Ask "What's moving for digital natives in KE?" → handoff offers Digital Architect Gen Z with KE selected
2. Handoff never skips evidence gather or quality gate
3. Failed persona match: CTA still opens research mode with empty picker, no wrong persona

### Phase 4: Polish (S–M, backlog)

- [ ] Rename route label "Intelligence Superpower Center" in masthead/map if Jo signs off on name
- [ ] DOCX export (v1.1 from trust design doc)
- [ ] Week-over-week artifact diff

## Acceptance criteria summary (Jo + T)

| Check | Owner |
|-------|-------|
| One entry point for ask + research (`#/console`) | T |
| Cited research doc with numbered refs, citation drawer | Jo |
| Quality gate respected (thin/moderate/strong sections) | Jo |
| Share link reloads artifact | Jo |
| Refine loop with frozen evidence | Jo |
| Degraded banner when Brand24 times out | Jo |
| Desk chat unchanged in same session | Jo |
| Fourth generate in 10 min → 429 | Engineering |
| Pulse daily Ask still works | Albert |
| No client data outside Vertex | Compliance |

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Research doc trapped in chat column scroll | Research mode uses page scroll container, not `chat.jsx` scrollRef |
| Breaking share links in Slack/email | Permanent `#/research` alias + redirect tests in `test_api` or frontend smoke |
| Accidental research via chat tools | Code review gate: no imports of `research` in `chat.py` |
| Mode switch mid-job leaves zombie UI | Shared abort controller pattern from chat; reset phase on switch |
| Mobile layout regression | Phase 1 QA on 380px width; reuse `useIsMobile` from research |
| Rate limit confusion | Research generate only from Research mode button, not on every mode enter |
| CSS drift between standalone and embedded research | Single `ResearchPane` component, one CSS namespace |

## What NOT to do

1. Do not add `generate_research_doc` as a `chat.py` tool.
2. Do not poll research status from the chat send handler.
3. Do not let Gemini pick persona_id without `persona_registry.resolve`.
4. Do not remove citation validation in `synth._validate_research`.
5. Do not touch `configs/research_personas.yaml` from the frontend.
6. Do not merge `/api/ask/brief` into console routes.
7. Do not run a full pipeline re-ingest from Listening Post (TEV2 boundary).
8. Do not delete `#/research` until analytics show near-zero direct hits (6 month minimum).

## Testing

| Layer | What |
|-------|------|
| Unit | Existing `test_research_pipeline.py`, `test_persona_registry.py` unchanged |
| API | `tests/test_api.py` research routes unchanged |
| Manual | `docs/jo-research-acceptance.md` re-run on `#/console?mode=research` |
| Script | `scripts/qa_research_acceptance.py` with `LP_BASE_URL` + console URL |
| Regression | Console chat thread save/load; one Pulse Ask brief |

## Open questions for Albert

1. Final name: "Intelligence Console", "Intelligence Superpower Center", or keep both (console slug, superpower display name)?
2. Phase 2 split pane: default for desktop or opt-in "Pin doc"?
3. Should research mode show desk region masthead or stay chromeless like today?

---

*Plan only. Phase 0 nav CTA is optional immediate ship; Phases 1–3 are the unified centre.*
