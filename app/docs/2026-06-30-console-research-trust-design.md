# Console Research: Trust Layer + Persona Registry (v1)

Date: 30 June 2026
Owner: Albert
Repo: Listening Post (`listening-post` Cloud Run)
Status: shipped to `main` (commit `051dd63` and prior)

## Problem

The Intelligence Console could not produce Jo-style market research deliverables with audit trail. One-shot chat had no numbered citations, no persisted artifacts, and persona inference drifted run to run.

## Solution

A trust-first research pipeline beside desk chat (desk chat frozen, zero changes to `chat.py`):

1. Deterministic persona registry in YAML + Python scoring
2. Evidence graph assembled before any Gemini call
3. Quality gate in Python (`thin` / `moderate` / `strong`)
4. Persisted artifacts with refine loop and share links
5. Brand24 mandatory per market (15s timeout, degrade to BQ-only)
6. Search proof from BQ `search_velocity_score` and rising terms (Semrush out of scope)

## Architecture

```
Persona picker → persona_registry.resolve → research.build_research_evidence
  → persona_registry.rank_and_filter + quality_gate
  → synth.synthesize_research (one JSON call)
  → persist (GCS default) → ResearchDocPanel.jsx
```

Data sources (read-only on engine BQ): `seed_insights`, `trend_analysis`, `enriched_content`, `trend_scores`, `daily_summary`, `lexicon`, Brand24 topics/demographics.

## Persona registry

Config: `configs/research_personas.yaml`

**Users cannot create personas in v1.** Personas are YAML definitions tuned by engineering and validated by Jo. Console shows enabled personas only. See `docs/research-personas.md` for lifecycle, request process, and YAML schema.

| Persona | Launch state |
|---------|--------------|
| `digital_architect_genz` | enabled |
| `hustle_economy_genz` | stub (`enabled: false`) |
| `ai_adopter_genz` | stub (`enabled: false`) |

`resolve_persona`: exact id, then rapidfuzz on enabled aliases (threshold 85, fail-closed on tie within 3pts). Disabled stubs never resolve.

`score_ref`: weighted sum in Python. `rank_and_filter`: sort desc, drop below `min_relevance`, cap 40 refs/market.

Golden fixtures: `tests/fixtures/research/golden_digital_architect.json` with frozen `trend_date` 2026-06-15. CI hash stable; live ref content may change daily (correct behaviour).

## Evidence ref types

`seed`, `brief`, `post`, `brand24_topic`, `brand24_demo`, `lexicon`, `digest`, `google_trends_rising`, `wikipedia_top` (when TEV2 flips Wikipedia).

Synthesis bullets carry `evidence_indices` validated in `_validate_research`.

## Quality gate

| Level | Behaviour |
|-------|-----------|
| `thin` | Metrics + quotes only; no inference, positioning, or activation |
| `moderate` | Inference labelled `low_confidence`; positioning needs corroboration |
| `strong` | Full three-lane doc |

## Job protection

| Endpoint | Limit |
|----------|-------|
| `POST /api/research/generate` | 3 per 10 min per client IP |
| `POST /api/research/refine` | 5 per 10 min per client IP |

90s wall-clock job timeout. Brand24 per-call 15s cap. Status values: `gathering`, `synthesizing`, `completed`, `completed_degraded`, `failed`.

## Persist

Default: `RESEARCH_PERSIST_BACKEND=gcs` (`listening-post-cache` bucket via `CACHE_BUCKET`). BQ table `research_artifacts` available when IAM gate passes (`scripts/verify_research_artifacts_iam.py --apply`).

Share links: `GET /api/research/{artifact_id}` (frontend `loadArtifact` uses this path).

## API surface

| Route | Purpose |
|-------|---------|
| `GET /api/research/personas` | Enabled personas for picker |
| `POST /api/research/generate` | Start async job (202 + `job_id`) |
| `GET /api/research/status?job_id=` | Poll job |
| `GET /api/research/{artifact_id}` | Load persisted artifact |
| `GET /api/research/recent` | Sidebar history |
| `POST /api/research/refine` | Refine with frozen evidence |

## Frontend

`frontend/src/ResearchDocPanel.jsx`: persona picker, doc panel, citation drawer, refine bar, degraded banner, recent sidebar, share URL.

`chat.jsx`: thin wiring only (rail button, URL param `?research=`). `seeds.jsx`: deep link via `buildResearchDeepLink`.

## TEV2 boundary

LP reads engine BQ only. No changes to `scripts/run_rss_now.py` or cron image. TEV2 parallel: Wikipedia yaml flip (Albert confirms), Twitter handles deferred.

## Jo acceptance (manual)

1. Pick Digital Architect Gen Z, markets KE + NG
2. Doc panel shows numbered refs
3. At least one `google_trends_rising` or `brand24_topic` ref (or degraded banner)
4. Ref click opens citation drawer
5. Positioning only when quality gate allows
6. Refine produces new artifact
7. Share link reloads artifact
8. Desk chat FOLLOW/SEED unchanged in same session
9. Fourth generate in 10 min returns 429

## Path to 10/10

| Piece | When |
|-------|------|
| Trust layer + one tuned persona | v1 (shipped) |
| Enable stub personas after Jo tunes weights | v1.0.1 |
| Jo live sign-off | after deploy test |
| Week-over-week artifact diff | v1.1 |
| DOCX export | v1.1 |

## Files

| File | Role |
|------|------|
| `configs/research_personas.yaml` | Persona definitions |
| `docs/research-personas.md` | Persona lifecycle, YAML schema, request process |
| `src/api/persona_registry.py` | Resolve, score, gate |
| `src/api/research.py` | Orchestrator, timeout, degrade |
| `src/api/synth.py` | Research synth, validate, render, refine |
| `src/api/bq.py` | Fetches + artifact CRUD |
| `src/api/main.py` | Routes + rate limits |
| `frontend/src/ResearchDocPanel.jsx` | UI |
| `scripts/qa_research_acceptance.py` | Golden acceptance |
| `scripts/verify_research_artifacts_iam.py` | BQ IAM gate |
| `tests/unit/test_persona_registry.py` | Registry golden tests |
| `tests/test_research_pipeline.py` | Pipeline integration tests |
