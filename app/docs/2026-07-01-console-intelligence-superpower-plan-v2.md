# Console Intelligence Superpower Center (v2)

Date: 1 July 2026  
Owner: Albert  
Repo: Listening Post (`listening-post` Cloud Run)  
Status: plan only, supersedes `docs/2026-07-01-console-intelligence-superpower-plan.md`  
Related: `docs/2026-06-30-console-research-trust-design.md`, `docs/jo-research-acceptance.md`, commit `c149588`

---

## Critique of v1 (why this rewrite exists)

1. **Phase 0 is disposable.** Footer CTAs and empty-state links that still route to `#/research` get ripped out a week later in Phase 1. That is churn, not value.

2. **Five phases for a routing problem.** Jo and T need one desk, not a product roadmap. Split pane, thread handoff, and rename polish are backlog items dressed as phases.

3. **"Superpower center" is never defined.** v1 describes two tabs. Magic is the evidence progress ladder, outcome preview, numbered citations, and a doc Jo can paste into a deck. Tabs are plumbing, not the superpower.

4. **Open questions block Phase 1.** Chrome, naming, and split-pane defaults are left for Albert instead of decided. Ship calls are part of the plan, not footnotes.

5. **No automated gate.** Manual Jo checklist only. Redirect shims, mode-switch abort, and share-link backward compat need at least one frontend or API smoke test or they will regress silently.

6. **Recent sidebar is fiction.** v1 says "UI stub in CSS only." `ResearchDocPanel.jsx` has no recent list. Phase 2 underestimated the UI work.

7. **Legacy redirect will fight itself.** Today `App.jsx` L92–100 sends `#/console?persona_id=` to `#/research`. Phase 1 wants the reverse. v1 never sequences that flip.

8. **ConsoleShell scope is right, framing is wrong.** Extraction is correct for scroll isolation, but v1 treats it as a multi-week programme. It is a 2–3 day frontend refactor with zero backend risk.

---

## North star

**One hash route (`#/console`) where a strategist can explore the signal and ship a cited, persona-gated research doc without changing mental context or breaking the trust layer.**

---

## What we will NOT do

1. Merge research into `chat.py` or expose it as a chat tool.
2. Auto-route free-text asks to a persona (no LLM intent router).
3. Split pane (ask + doc side by side) in this workstream.
4. Thread-to-persona heuristic handoff in the first release.
5. Rename the product to "Superpower Center" in nav (display copy only, slug stays `console`).
6. Touch `chat.py`, `research.py`, `synth.py`, or TEV2 ingestion.
7. Remove `#/research` URLs before six months of alias-only service.

---

## Architecture decision

**Make `#/console` the hub. Research is a mode inside it, not a sibling route.**

Extract a thin `ConsoleShell` that owns mode state (`ask` | `research`), the shared left rail, poll abort on switch, and URL sync. Mount existing chat body and existing research body unchanged. Backend stays split: `chat.py` for ask, `research.py` for docs.

### Unified shell vs research-primary hub

| Approach | Verdict |
|----------|---------|
| Keep `#/research` as primary, improve cross-links | Fixes navigation, not mental model. Footer still advertises two desks. Jo acceptance already says "open Console, confirm rail shows Research doc." Wrong direction. |
| **`#/console` hub with explicit Ask \| Research doc modes** | **Ship this.** One entry point, one rail, shared chrome rules. Research keeps window scroll and chromeless layout. Ask keeps sticky dock and thread list. |
| Split pane day one | Layout risk for a workflow nobody has validated. Cut. |

Thread handoff ("Build research doc from this ask") is a nice Phase 3 add-on only if Phase 1–2 prove the hub. It is not required for the superpower to land.

### What makes it feel magical (not just two tabs)

Magic is not the tab control. Magic is:

1. **One desk identity.** Same rail, same passcode, same "Intelligence console" kicker whether you are asking or generating.
2. **Visible trust work.** Progress steps (seeds → engine → Brand24 → synthesis) show the engine doing real work before Gemini writes prose.
3. **Outcome preview before generate.** Premium UI from `c149588` tells T what lanes the doc will contain.
4. **Deliverable-grade output.** Numbered citations, drawer receipts, quality gate, refine loop, share link. That is Jo's bar, already shipped.
5. **Desk memory.** Recent artifacts in the rail so returning to yesterday's KE doc is one click, not a Slack link hunt.
6. **Zero route whiplash.** Switching modes does not reload the app shell, drop the ticker mid-thought, or lose an in-flight job without explicit abort.

Two tabs that still feel like different apps (different chrome, back button, footer-only entry) fail. One shell with mode-specific main column passes.

---

## Component design

### `ConsoleShell.jsx` (new)

- Parse `mode` from hash query: `ask` (default) | `research`
- Render shared rail: mode tabs, contextual primary action (New conversation | Generate doc), mode-specific history (threads | recent artifacts)
- Mount `ChatPane` or `ResearchPane`
- On mode change: abort in-flight poll, `history.replaceState`, preserve `artifact` / `persona_id` / `markets`
- Set `data-screen-label`: `Intelligence Console · ask` | `Intelligence Console · research`
- When `mode=research`: apply `app-research` chrome rules (no ticker, masthead, footer) via prop to App or local class

### `ChatPane` (extract from `chat.jsx`)

- Current `ChatPage` body minus rail header and Market Research nav row
- Unchanged: `/api/chat/send`, `/api/chat/status`, localStorage threads

### `ResearchPane` (extract from `ResearchDocPanel.jsx`)

- Current `ResearchPage` body minus "Back to Console" header row
- Unchanged: `/api/research/*`, window scroll, citation drawer, refine bar
- `ResearchPage` becomes thin alias wrapper for `#/research` redirect only

### `App.jsx`

- `route === 'console'` → `<ConsoleShell region={...} />`
- `route === 'research'` → redirect to `#/console?mode=research&...` (replaceState, preserve query)
- **Remove** L92–100 redirect that sends `#/console?persona_id=` to `#/research` (flip direction)
- Footer: drop separate "Research" link; Console entry covers both modes
- `researchMode` flag: true when `route === 'console'` AND `mode=research` (parse hash), not when `route === 'research'` alone

### `researchLib.jsx`

- Add `parseConsoleUrl()`, `setConsoleModeUrl()`, dual-path share URL (emit console URL, accept both on load)
- `buildResearchDeepLink` → `/console?mode=research&persona_id=...`

### Backend

No changes in this workstream.

---

## Data flow (unchanged backend)

**Ask:** input → `POST /api/chat/send` → poll `/api/chat/status` → answer + source pills → localStorage `pulse-chat`

**Research:** persona + markets + product_frame → `POST /api/research/generate` → poll `/api/research/status` → evidence graph → quality gate → synth → GCS persist → `DocBody` + citations → refine → new artifact

**Isolation:** Ask poll 90s. Research poll 95s. Never shared. Abort on mode switch.

---

## Phases

### Phase 1: One Desk

**User story:** T opens `#/console`, taps Research doc in the rail, generates a cited doc, shares it, switches back to Ask in the same session without hunting footer links or losing trust-layer behaviour.

**Effort:** 2.5 days

**Files touched:**

| File | Change |
|------|--------|
| `frontend/src/ConsoleShell.jsx` | Create: mode state, rail tabs, abort on switch |
| `frontend/src/chat.jsx` | Extract `ChatPane`; remove Market Research row |
| `frontend/src/ResearchDocPanel.jsx` | Extract `ResearchPane`; remove back button; thin `ResearchPage` alias |
| `frontend/src/researchLib.jsx` | Console URL helpers; share URL emits `#/console?mode=research&artifact=` |
| `frontend/src/App.jsx` | Wire shell; flip legacy redirect; footer single Console link; `researchMode` from hash |
| `frontend/src/seeds.jsx` | Deep links to console mode URL |
| `frontend/src/app.css` | `.console-research` scroll: research main uses page scroll, not chat column |
| `docs/jo-research-acceptance.md` | Route references → `#/console?mode=research` |
| `tests/test_frontend_routes.py` or `scripts/qa_console_hub.py` | **New smoke:** redirect pairs, share URL round-trip |

**Acceptance criteria:**

1. From `#/console`, one click to Research doc mode. No full-page route change feel (shell persists).
2. Full Jo acceptance checklist passes at `#/console?mode=research` (generate, citations, refine, share, 429, degraded banner).
3. Ask mode: FOLLOW/SEED desk chat unchanged; thread persist works.
4. `#/research?artifact=ra_*` redirects to console mode and loads doc.
5. `#/console?persona_id=...&markets=...` redirects to console research mode (not to `#/research`).
6. Seeds "Generate research doc" opens console research mode and auto-starts.
7. Pulse daily Ask (`/pulse` command bar) unchanged.
8. Mode switch during in-flight job aborts poll and resets UI (no zombie spinner).
9. Mobile 380px: mode tabs usable; research doc scrolls to refine bar.

**Rollback:** Revert frontend deploy. `#/research` route still registered as standalone `ResearchPage` until Phase 1 is stable (keep alias render path for 48h canary if needed). No backend rollback required.

---

### Phase 2: Desk Memory

**User story:** Jo returns to the console and opens yesterday's artifact from the rail without pasting a share link.

**Effort:** 1 day

**Files touched:**

| File | Change |
|------|--------|
| `frontend/src/ConsoleShell.jsx` | Research mode rail: list from `GET /api/research/recent` |
| `frontend/src/ResearchDocPanel.jsx` | Click recent row → load artifact, update URL |
| `frontend/src/app.css` | Wire `.research-recent` styles (exist but unused) |
| `frontend/src/researchLib.jsx` | Optional client cache of last 5 artifact ids in sessionStorage for instant rail paint |

**Acceptance criteria:**

1. After two generates, rail shows at least two recent rows with persona label and date.
2. Click recent row loads doc with citations intact.
3. Share URLs from Phase 1 still work.
4. Empty recent list does not break rail layout.

**Rollback:** Hide recent block via CSS or feature flag constant. API unchanged.

---

### Phase 3 (backlog, not scheduled): Exploration Bridge

**User story:** After an ask, T can jump to research mode with desk region pre-filled as a market chip.

**Effort:** 1 day when requested

**Scope (minimal, not v1 handoff fantasy):**

- Chat empty state and assistant bubble footer: link "Generate a research doc" → `go('/console?mode=research&markets=' + region)`
- No keyword persona inference. User picks persona manually. Trust layer unchanged.

**Cut from v1 Phase 3:** Heuristic persona map, confidence gating, auto-prefill persona_id. That is 3 days of guessing for marginal gain.

---

## Migration table

| URL | After Phase 1 |
|-----|---------------|
| `#/research` | `#/console?mode=research` |
| `#/research?artifact=ra_x` | `#/console?mode=research&artifact=ra_x` |
| `#/research?persona_id=...&markets=...` | same params on console |
| `#/console?persona_id=...` (legacy) | `#/console?mode=research&persona_id=...` |
| `researchShareUrl()` output | `#/console?mode=research&artifact=...` (loaders accept both) |

Keep `#/research` route registered 6+ months. Alias redirect only.

---

## Testing

| Layer | What |
|-------|------|
| Existing unit | `test_research_pipeline.py`, `test_persona_registry.py` unchanged |
| Existing API | `tests/test_api.py` research routes unchanged |
| **New smoke** | Script or test: `#/research?artifact=` → console URL; mode switch does not 500 |
| Manual | `docs/jo-research-acceptance.md` on `#/console?mode=research` |
| Regression | Console thread save/load; one Pulse Ask brief |

---

## Risks

| Risk | Mitigation |
|------|------------|
| Research trapped in chat column scroll | Research mode main column is `ResearchPane` with page scroll, not `scrollRef` |
| Redirect loop console ↔ research | Single direction: research → console. Delete old console → research redirect before deploy |
| Share links in Slack break | Loaders accept both URL shapes for 6 months |
| CSS drift embedded vs standalone | One `ResearchPane`, one CSS namespace |

---

## Decisions locked (no longer open)

| Question | Decision |
|----------|----------|
| Final nav name | Slug `console`, kicker "Intelligence console", subline "Ask · Research doc" |
| Research chrome | Chromeless like today: no ticker, masthead, footer when `mode=research` |
| Split pane | Not in scope |
| Thread handoff | Backlog Phase 3 minimal link only |
| Default mode | `ask` (desk exploration first; research one click away) |

---

## Executive brief for Albert

Jo and T do not have a research problem. They have a desk problem. We built a Jo-grade trust pipeline on `#/research` and left the console pretending research lives somewhere else. The footer still lists Console and Research as siblings. The research page still has a back button. That reads as two tools bolted together, not a superpower center.

v1 over-corrected in the wrong places. Five phases, a throwaway Phase 0, split pane nobody asked for, and a three-day thread handoff that guesses personas from chat keywords. That is roadmap bloat. The actual job is a 2.5 day frontend refactor: one shell, two modes, flip the redirect, run Jo's checklist again.

Approve Phase 1 only. That is the whole bet. Phase 2 is a day of desk memory once T is living in the hub. Cut split pane, cut heuristic handoff, cut the rename theatre. Magic is already in the premium research UI and the evidence ladder. We are unifying the front door, not reinventing the engine.

Do not ship Phase 0 wayfinding. It is a half measure that routes to `#/research` and gets deleted next week. If you want signal before the refactor, change the footer copy in one commit. Otherwise go straight to One Desk.

Backend stays frozen. Trust layer stays frozen. Pulse Ask stays frozen. This is routing and composition only.

---

*v2 plan. Ship Phase 1 first.*
