# 42 spec: New seeds this week

Status: spec only (not built).  
Repo: 42 (`feat/lp-r-staging` → main after staging QA).
Data: TEV2 `seed_candidates` table (same BQ dataset as 42, `BQ_DATASET` env).
Blueprint ref: V3.8 Track C, Phase 4 review surface (C3 read path in 42).

## Problem

Explorer answers "show me the graph for a word I already typed." Seeds answers "what behaviour should we activate today (Gemini editorial)." Neither surfaces the discovery loop output: **terms the engine ranked as new taxonomy/watchlist candidates this week**.

Albert should open one desk and see: here are 15 new graph-born proposals per market this week, why each scored, what topics it touches, and one click to trace or research. That closes the loop from backfill → ranker → human glance → Explorer/Console.

## Product placement

| Surface | Route | Source | Question |
|---------|-------|--------|----------|
| Seeds (existing) | `#/seeds` | `seed_insights` / Gemini | What behaviour to activate? |
| **New seeds this week (new)** | `#/discover` | `seed_candidates` | What new terms did the graph propose? |
| Explorer (existing) | `#/seedpath` | `seed_graph` | Where did this word travel? |
| Console (existing) | `#/console` | research API | Show me the posts |

Nav label: **Discover** (between Explorer and Topics). Alt rejected: "New seeds" collides with Seeds; "Candidates" is engineer-speak.

Cross-links on every page:

- Seeds intro links Discover ("graph proposals") and Explorer ("trace a keyword").
- Discover card primary action: **Trace** → `#/seedpath/{term}?market=za`.
- Discover card secondary: **Research** → `#/console/{encoded query}`.

## Data contract

Table: `{project}.{dataset}.seed_candidates` (schema in TEV2 `infra/bigquery_schemas/seed_candidates.sql`).

Rows the UI cares about:

| Field | UI use |
|-------|--------|
| `candidate_id` | Stable key, approve/reject POST body |
| `proposed_date` | Week grouping, "proposed Mon 30 Jun" |
| `market` | ZA/NG/KE filter |
| `candidate_type` | Badge: keyword, slang, handle, … |
| `candidate_value` | Headline term |
| `lane` | Badge: **weekly** (Monday batch) vs **fast** (daily) |
| `score` | Rank within market/week, show 0.00–1.00 |
| `seed_fit` | Mini breakdown: co_occur, visual_audio, genz, slang |
| `evidence_topics` | Topic chips → `#/topic/{id}` |
| `sample_row_ids` | Optional "N sample posts" count only in v1 (no row fetch) |
| `safety_flags` | Hide from default list if non-empty (status already `rejected`) |
| `status` | pending / approved / rejected / applied / reverted |
| `source` | Footnote: seed_graph, coverage, … |
| `rationale` | Show when rejected |

### Query window

**This week** = Monday-anchored week containing today (same as ranker cap accounting in C2).

```sql
SELECT *
FROM seed_candidates
WHERE proposed_date >= @week_start   -- Monday on or before today
  AND proposed_date <= @today
  AND status = 'pending'             -- default tab
ORDER BY market, lane DESC, score DESC   -- weekly before fast at equal score
```

Tabs:

1. **Pending** (default): `status = 'pending'`
2. **Decided**: `status IN ('approved','rejected','applied','reverted')` last 28 days
3. **All this week**: pending + decided for audit

Cap display: footer note "Engine cap: 10 weekly + 5 fast per market per week."

### Empty states

| Condition | Copy |
|-----------|------|
| Table missing / query error | "Discovery proposals not available yet." |
| Zero pending, graph off | "No proposals this week. seed_candidates runs when SEED_CANDIDATES_ENABLED is on and seed_graph has history." |
| Zero pending, graph on | "Nothing cleared the score floor today. Check back after Monday weekly batch." |

## API

### `GET /api/seed-discover`

Query params:

- `market` optional: `za` | `ng` | `ke` | omit = all three
- `status` optional: `pending` (default) | `decided` | `week`
- `week_start` optional ISO date (default: Monday this week)

Response:

```json
{
  "week_start": "2026-06-30",
  "week_end": "2026-07-06",
  "as_of": "2026-07-02",
  "dataset": "trends_v2_dev",
  "markets": {
    "za": {
      "pending_count": 0,
      "weekly_count": 0,
      "fast_count": 0,
      "candidates": [ /* ordered list */ ]
    },
    "ng": { "...": "..." },
    "ke": { "...": "..." }
  }
}
```

Candidate object (42-shaped, not raw BQ):

```json
{
  "id": "abc123…",
  "term": "lagos",
  "term_type": "keyword",
  "market": "ng",
  "lane": "weekly",
  "score": 0.612,
  "proposed_date": "2026-06-30",
  "status": "pending",
  "source": "seed_graph",
  "fit": {
    "co_occur": 0.71,
    "visual_audio": 0.55,
    "genz": 0.42,
    "slang": 0.38,
    "distinctiveness": null
  },
  "topics": [
    { "id": "economy_sapa_hustle", "label": "Sapa hustle" }
  ],
  "sample_post_count": 5,
  "why": "Co-occurs with 2 hot topics; 6 visual/audio platforms; first seen 4 days ago."
}
```

`why` is assembled server-side from `seed_fit` + lane + `evidence_topics` (template strings, no Gemini in v1).

Implementation:

- New module `src/api/seed_discover.py` with `build_discover_payload(market, status, week_start)`.
- BQ helper in `src/api/bq.py`: `fetch_seed_candidates_week(...)`.
- Route in `main.py`, passcode gated like `/api/seed-path`.
- Cache: `_market_cached("seed-discover", cache_key, …)` TTL 10 min (same family as seed-path).

Optional enrich (same request, bounded):

- Join `v_seed_first_seen` for `first_seen_event_date` and platform count (one extra query per market max 15 rows).

### `POST /api/seed-discover/review` (v1.5, spec now, ship later)

Body: `{ "candidate_id", "action": "approve"|"reject", "target": "topic_group:…", "reason": "…" }`

v1: **read-only in 42**. Approve/reject stays TEV2 CLI (`review_seed_candidates.py`) until audit trail and YAML paste UX are signed off. UI shows copy: "Approve via engine CLI" with `candidate_id` copy button.

Rationale: C3 never auto-writes `sources.yaml`. 42 approval without a hardened backend duplicates risk.

## Frontend

### Route

- Hash: `#/discover` with optional `#/discover/ng` for market preselect.
- Lazy import `discover.jsx` in `App.jsx`, add to `STANDALONE` and `ROUTE_LABELS`.
- Nav: `['discover', 'Discover']` after Explorer in `today.jsx` NAV.

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│ DISCOVER · graph proposals this week                        │
│ Terms the engine ranked as new. Not editorial Seeds cards.  │
├─────────────────────────────────────────────────────────────┤
│ [ZA] [NG] [KE] [ALL]     [Pending | Decided | All week]     │
├─────────────────────────────────────────────────────────────┤
│ ┌─ NG · weekly · score 0.61 ─────────────────────────────┐ │
│ │ lagos                                                    │ │
│ │ Co-occurs with hot topics · 7 platforms · novel 4d       │ │
│ │ [Sapa hustle] [Japa migration]  ← topic chips            │ │
│ │ ▓▓▓▓▓▓▓░░░ co_occur  ▓▓▓▓░░░░░ visual  … fit bars       │ │
│ │ [Trace in Explorer]  [Research in Console]  [Copy ID]    │ │
│ └──────────────────────────────────────────────────────────┘ │
│ … up to 15 cards per market …                               │
└─────────────────────────────────────────────────────────────┘
```

### Card fields (priority order)

1. Market flag + lane badge (weekly = solid, fast = outline)
2. Term (serif, large) + type pill
3. One-line `why` (plain English)
4. Score (mono, top-right)
5. Topic overlap chips (max 5, rest "+N")
6. Fit bars (4 components from `seed_fit`; distinctiveness omitted until C2 exposes it in BQ)
7. Actions row

### Actions

| Button | Target |
|--------|--------|
| Trace in Explorer | `go('/seedpath/' + encodeURIComponent(term))` + set market |
| Research in Console | Same query template as Explorer adjacent chips |
| Open top topic | First `evidence_topics[0]` if present |
| Copy ID | Clipboard `candidate_id` for CLI review |

### Relationship callout (top of page)

Static explainer box, same pattern as Explorer "What you get":

"Discover lists **new terms** the graph wants added to taxonomy or watchlists. **Seeds** lists **behaviours** ready for Lyria. **Explorer** traces one term you pick."

## Dependencies and flags

| Dependency | Required for data |
|------------|-------------------|
| `seed_candidates` table migrated | Yes |
| `seed_graph` history (14+ days ideal) | Yes for meaningful weekly batch |
| `SEED_GRAPH_ENABLED=true` on cron | Nightly graph growth |
| `SEED_CANDIDATES_ENABLED=true` on cron | Daily/Monday inserts |

Staging: already on `trends_v2_dev` with 11 pending rows (mostly KE fast lane from 2026-07-01 backfill run). Discover page will look thin until weekly Monday batch + more history.

Prod: no flip until `feat/v3-staging` merged and two clean seed_graph cron days per flip-readiness.

## Files to create/modify

| File | Change |
|------|--------|
| `src/api/bq.py` | `fetch_seed_candidates_week`, topic label join |
| `src/api/seed_discover.py` | Payload builder, `why` templates |
| `src/api/main.py` | `GET /api/seed-discover` |
| `frontend/src/discover.jsx` | Page |
| `frontend/src/App.jsx` | Route + lazy load |
| `frontend/src/today.jsx` | NAV Discover |
| `frontend/src/seeds.jsx` | Link to Discover |
| `frontend/src/seedpath.jsx` | Link to Discover in explainer |
| `tests/unit/test_seed_discover_api.py` | Auth, shape, empty |
| `tests/unit/test_seed_discover_bq.py` | Mock BQ row mapping |

Estimate: 2 files backend, 4 frontend, 2 test files. One PR on 42.

## Acceptance criteria

1. `#/discover` loads pending candidates for current week from BQ.
2. Cards sort by score within market; weekly lane visually primary.
3. Trace opens Explorer with term + market pre-filled and auto-traces.
4. Empty state renders when table empty or flag off (no crash).
5. Staging QA: with dev data, at least one NG/KE card shows topics + fit bars.
6. No write to `sources.yaml`, no approve without explicit v1.5 POST.
7. Page load cold < 3s (single BQ query, 10 min cache).

## Out of scope v1

- In-app approve/reject (CLI only)
- Gemini rationale per card (C4 coverage lane)
- Embedding near-miss candidates (`source=embedding_near_miss`)
- Email section (`SEED_BEHAVIOUR_EMAIL_ENABLED`)
- Pushing candidates into Seeds editorial cards automatically
- Prod `seed_candidates` until engine flags flip

## v1.5 follow-ups

- POST review endpoint mirroring CLI, audit log only (still YAML paste for apply)
- Monday digest line in morning-check output linking the 42 Discover URL
- Filter: hide generic geo tokens (`nigeria`, `kenya`, `2026`) via server-side denylist shared with ranker stoplist
- Distinctiveness bar when C2 persists velocity component in `seed_fit` or sidecar

## Success metric

Albert opens Discover Monday morning, sees 3–10 non-generic pending terms per market with topic overlap he recognises, traces one in Explorer, sends one to Console, and can say "the graph proposed these without me typing keywords." That is the V3 wow the backfill was for.
