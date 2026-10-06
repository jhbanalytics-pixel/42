# Trends Engine V3 Blueprint (v3.1)

Superseded by [`trends-engine-v3-blueprint-v3.2.md`](trends-engine-v3-blueprint-v3.2.md) (master build document, Discovery Loop + Trust Layer). Kept for trust-layer detail and audit history.

Status: strategy plus architecture, audited 1 Jul 2026. Supersedes [`trends-engine-v3-blueprint.md`](trends-engine-v3-blueprint.md) (2026-06-30 snapshot). Audit findings: [`trends-engine-v3-blueprint-audit.md`](trends-engine-v3-blueprint-audit.md). V3 evolves V2; it does not replace it. Code claims below distinguish **exists on master today** from **planned build**.

## 1. Vision

V3 is V2 with the PULSE Intelligence Core promoted from a shadow table that renders nothing into the mandatory pass every reader-facing claim crosses before it reaches a human. V2 answers "what is loud" (the composite `trend_score` ranks topics by weighted momentum). V3 answers "what is true, on what evidence, as of when," and it refuses to assert anything it cannot defend. The contract becomes: every number is computed by code not by a model, every factual sentence carries a clickable receipt bound to specific source rows, every forward claim is a hedged outlook chip or it is cut, and silence beats a guess. The defensible IP is not the model. It is the deterministic trust boundary (a pure-Python corrector that the generative model can only feed, never override) plus the accumulating `event_ledger`, a recency-true cross-source "what was true when" history of SSA Gen Z that no off-the-shelf social tool assembles because those tools optimise mention volume and have no event-state memory and no honesty rail. The product story to BSA and every white-label client after it: we do not show you a trend until the engine can defend it, and we show you exactly why we believe it.

## 2. The problem with V2 today

Four real limits, grounded in the V2 maps, not hypotheticals.

The stale-read trust break is the original sin. THE READ once showed a football match as upcoming after the team had already played and won, because the Brand24 ai-insights grounding returns a weekly retrospective regardless of the requested window and the renderer surfaced it raw. Nothing in `trend_brief.py` or `daily_summary.py` checks whether a dated claim is still in the future. The fix shipped at the time was to remove the Brand24 weekly card (LP #39/#40), not to reconcile the claim. Demoting stale content is not removing it. The ledger `validity_window` supersession (planned, Section 4) is the structural fix; it is not live yet.

Accuracy is not yet a property of the engine. The brief prompt in `src/analysis/prompts/trend_brief.py` is told to ground every claim and not invent, but nothing verifies it did. The honesty rails (the foreign-collision rule, "say the data is thin honestly") are soft prose constraints on the LLM. Only headline emptiness (`assert_sourced`) and the response schema are hard-enforced. The truthfulness of any sentence inside `description_rationale` or `summary_text` is never checked against the underlying rows at render time. This is the future-tense gap specifically: nothing checks whether a dated claim is still in the future. It is distinct from the existing `daily_summary` HARD PICK velocity rule, which governs which topic gets picked, not whether that topic's prose has gone stale.

There is no real pattern intelligence. The engine reasons about `trend_score` velocity and a birth/growth/maturity/decline lifecycle, but it has no model of a discrete real-world event having an outcome (scheduled then happened then resolved). Football is the only event shape with a curated fixture rule; every other event type relies on literal keyword presence. Velocity is pure volume (today vs a 14-day average), so a connector flip or a new RSS feed reads as a cultural surge.

There is no intelligent data navigation. Listening Post retrieval is purely lexical: `search_content` in `bq.py` does token-AND `REGEXP_CONTAINS` with one broadened-OR retry. The M4 multilingual-embedding asset is used only for offline classification rescue, never at query time, so code-switched and slang Gen Z queries (the exact content the keyword classifier caps at ~40%) silently miss.

A substrate fragility sits under all four: watchdogs and live ops scripts hardcode `trends_v2_dev` rather than routing through `get_dataset()`, so a prod move silently reads the wrong table. Fourteen hits in `scripts/accuracy_watchdog.py` alone. Production pipeline code in `src/` already uses `get_dataset()`. This is the single biggest foolproofness prerequisite and V3 fixes it first in Phase 0.

## 3. The V3 framework

V3 is layered. Each layer states its role and whether it is reuse or build. The dominant move is reuse; the new code is narrow and additive.

Ingestion (8 live connectors plus Semrush dark as 9th, plus the `BaseConnector` spine) is the evidence source. Role: unchanged data intake, but every kept row will carry a stable `receipt_id` and a geo-confidence score at ingestion so the spine can bind a claim to exact rows. **Exists today:** `BaseConnector`, `RAW_COLUMNS`/`ENRICHED_COLUMNS`, `safe_fetch`, all vendor normalisers, the `CONNECTORS` registry and the ensemble-before-reddit order, circuit breakers, geo-defence primitives, `_global_units_spent` shared ledger in `ensemble.py`. **Planned build:** `receipt_id` column, harden pre-call Ensemble budget gating (today uses `DEFAULT_ESTIMATED_UNITS_PER_CALL = 1` while post-call accounting uses real vendor units; cross-check daily with `fetch_units_history` against the 5000 account cap), idempotent per-(market, source, date) re-ingestion guard with `FORCE_REINGEST` recovery override, cross-connector `geo_confidence`.

The `receipt_id` definition has two failure modes that the spec pins down rather than waving past, because a receipt that collides or 404s is worse than no receipt for a trust product. Collisions: many normalised rows (EnsembleData and Brand24 derived feeds, GDELT entity rows) carry no url, so the hash is `sha256(source, content_type, market, url-or-rowtext-fallback, collected_at, intra-batch-sequence)`, where the row-text fallback plus the per-batch sequence number guarantees a urlless row is still unique within its (source, collected_at) group. Dead links: `raw_content` has a 90-day TTL, so a receipt that pointed only at a raw row would 404 once that row aged out. The receipt therefore binds to `enriched_content` (the same TTL-stable surface the embeddings bind to), and the chip degrades to a non-clickable "source aged out" label rather than a live link once the backing enriched row is past retention. The receipt never promises a click it cannot honour.

Classification and composite scoring is the loudness signal. Role: unchanged. `trend_score` still ranks what is moving and the spine sits after it and never touches it, exactly as corroboration does today. Reuse the full layered classifier, the embedding rescue (already live), `_aggregate_by_topic`, `compute_trend_scores`, the channel-family cross-source multiplier, velocity. The channel-family collapse that the trust chip depends on already exists: `_channel_family` in `run_rss_now.py` maps every Brand24 row to a single `brand24` family regardless of its web/facebook/tiktok/instagram platform string (the mapping was fitted against the live source distribution on 2026-06-16), and `corroboration.py` counts `FACTUAL_FAMILIES = {news, search, youtube, music}` over those collapsed families, never over raw platform strings. So V3 reuses the existing family map, it does not build a new one, and a single Brand24 feed already cannot read as multi-source agreement. The one promotion is carrying each row's winning-layer and embedding-cosine confidence into aggregation so a low-confidence keyword graze is a weaker evidence node than a high-confidence multi-channel embedding match.

The Intelligence Core (Section 4) is the new spine: corroboration, event-state ledger, reconcile, and a new per-claim grounding verifier. Mostly reuse, narrow build.

A deterministic relevance re-rank (grafted from the Relevance proposal, **`relevance.py` does not exist yet**) sits between scoring and surfacing and is covered in Section 6.

Pattern intelligence detectors (grafted from the Pattern proposal) read the substrate and are covered in Section 5.

Navigation (BigQuery `VECTOR_SEARCH` plus the promoted Listening Post agent) is covered in Section 5.

Surfacing (email, Listening Post, THE READ) becomes a pure consumer of the spine. It renders `claim_receipts`; it does not compute trust. Reuse `render_payload` JSON column (exists today as `{"display": {...}}`), the `render_card` binding rule, `confidence_label`, the NOT MEASURED footnote, `assert_sourced`, the LP grounded tool loop. **Planned build:** `schema_version` on `render_payload`, `ConfidenceReceipt` chip, `claim_receipt` field, `fetch_ledger_read` in the LP API, THE READ swap, render-time future-tense validator.

Trust observability guards the spine with deterministic recompute checks, the way `composite_integrity` guards `trend_score`. Reuse the `accuracy_watchdog` UNVERIFIED tier and its four trust checks, `reconcile_shadow_digest`, `engine_pulse`/`engine_evolve`, the `predictions_archive` backtest harness, `dry_run_sql`. **Planned build:** grounding-faithfulness check and a `detections_archive`-style honest backtest. **Cost monitoring:** the $30/mo Vertex RED threshold lives in the morning-check skill procedure (`morning-check runbook`), not a deployed `scripts/vertex_cost_watchdog.py`.

## 4. The Intelligence Core

The centerpiece. Three stages plus a grounding verifier, deepened from audit-only into the producer of a new render contract field, `claim_receipts`, that the surfacing layer is hard-gated on. Almost all of it already exists as tested code on `master`.

Stage 1, the corroboration scorer (`src/scoring/corroboration.py`, LIVE and free every cron). Per (market, topic) it emits two separate numbers that are never combined: `factual_corroboration` (news/search/youtube/music family breadth, a GDELT-entity bonus only if `tone_rows>0`, a search-spike bonus, all times an exponential 48h-half-life recency factor) and `social_corroboration`. The tier gate is the deterministic law: "corroborated" requires at least one factual family AND `factual_corroboration>=0.40`, and social-only can NEVER reach corroborated. It is persisted to `trend_scores` every run and does not touch `trend_score`. V3 surfaces this immediately as a receipt, with near-zero risk, because the data already lands.

Stage 2, the event-state ledger (`src/analysis/event_ledger.py`, built, **shadow ON since PR #218, 1 Jul 2026**). Per (market, `entity_key`) it holds the current resolved state of a watched real-world event. **States emitted today:** `resolved`, `scheduled`, `unknown` (Gemini schema enum and deterministic fallback in `event_ledger.py`). `ongoing` appears only as a dead branch in `reconcile.py` and is never assigned by the ledger builder; `evergreen` is not in the code path. Extend the enum deliberately if product needs those labels. Anti-hallucination is enforced in Python: Gemini may PROPOSE a state, but it is dropped unless it cites a supplied evidence index pointing at THIS entity's rows, and the deterministic regex guess wins otherwise. Confidence is always computed in Python (`0.4*min(source_count/3,1) + 0.3*recency + 0.3*citation`), never verbalised by the model. **Gemini cardinality:** one batched call per market per cron (~3 calls/day total), not one call per watched entity. V3 deepens it two ways: add `validity_window` and `current_state` supersession (direct fix for the stale-football bug), and widen `_fetch_factual_rows` from GDELT plus RSS plus search to also feed YouTube and music, which are already in `FACTUAL_FAMILIES` for the scorer but absent from the ledger's evidence fetch today.

Stage 3, reconcile (`src/analysis/reconcile.py`, built, shadow ON, 100% pure deterministic, zero Gemini). The trust boundary. Per claim it matches entities against the ledger and returns one of four actions: corroborated, stale, labelled, no_match. STALE fires only when the claim implies future AND the matched event is resolved AND the event is fresher than the claim. On stale the require-a-source gate runs: correct to `state_text` verbatim only if the event has at least 2 factual families AND confidence at least 0.55, otherwise label, never correct. Reconcile never writes free text. V3 promotes its output from an audit row into the render contract: it emits, per claim, a `claim_receipt` `{action, matched_entity_key, receipt_ids, confidence_tier, factual_n, social_m, as_of}` that surfacing is hard-gated on, and it runs over the `daily_summary` claims too, not just briefs.

The grounding verifier (**planned**, the faithfulness layer, and the single largest genuinely-new build in V3). Today the brief is told to ground but nothing checks it. There is zero grounding-verifier code on `master` today, so this is a from-scratch component on the cron critical path and it gets its own phase and its own backtest. V3 decomposes each brief into atomic claims and runs each against that topic's actual source rows. Below the citation threshold a sentence is demoted to social-signal phrasing or cut, never asserted as fact. **This is the dominant Vertex cost once live**, not the ledger (3 calls/day). Gate Key-tier topics first at shadow; batch per topic.

Four decisions the build has to pin, because each is a real failure point, not a solved seam:

Model and threshold. Vertex Gemini with a structured grounding prompt (claim plus candidate source rows, return supported / partially-supported / unsupported with supporting row ids). A claim renders as fact only when at least one returned supporting row id resolves to a real `receipt_id` for THIS topic AND the verdict is "supported". Confidence computed by our scorer plus row-id resolution, never read off the brief model.

Verifier unavailable mid-cron. Fall back to deterministic `key_metrics`; unverified prose demotes to hedged social-signal phrasing.

Latency budget. Batched per topic, Key/Watch tier first, shadow-first to measure fit inside the 00:30 UTC window.

Claim decomposition. Clause-level split on conjunction boundaries; uncertain splits fall back to whole-sentence demote, not cut. Unit tests on hand-labelled compound sentences.

The architectural principle the whole Core encodes is corrector-is-never-the-trust-boundary: the generative model PROPOSES; a pure deterministic layer DISPOSES.

## 5. Pattern intelligence and intelligent data navigation

Pattern intelligence (**all planned**, gated hard). Four additive detector families read the V2 substrate after scoring, each keyed on (market, query_group), each gated by `channel_diversity`. Emergence, anomaly, cross-market diffusion, lifecycle quantification. Each ships dark, logs to `detections_archive` cloned from `predictions_archive`, and must beat persistence/velocity on a trailing 4-week backtest AND clear a >90%-single-bucket degeneracy check before it renders. Forecast itself stays dark: it lost to persistence twice (`docs/flip-readiness.md` confirms both ARIMA and boosted-tree failed walk-forward) and only re-flips on a non-degenerate win.

Intelligent data navigation (**VECTOR_SEARCH planned**; LP agent exists today). Semantic retrieval over `enriched_content` via `ML.GENERATE_EMBEDDING`, GA `VECTOR_INDEX`, fused with lexical `search_content`. Embed delta-only, post-cron job, fuse with lexical to avoid index lag misses. The analyst-agent navigation already exists: Listening Post `src/api/chat.py` grounded tool-calling loop and Console Research evidence-graph plus quality gate. **Planned:** `semantic_search` tool, ledger time-travel read in LP.

## 6. Relevance, how the read always matters

Grafted from the Relevance Layer proposal (**`relevance.py` planned**). Relevance is two-axis: loudness (`trend_score`) ranks what to LOOK AT; defensibility (corroboration, event-state, grounding verdict) decides what may be ASSERTED. Deterministic re-rank: R = audience_fit x trust_factor x novelty x usefulness_prior, multiplicative, usefulness_prior defaults to 1.0 until feedback threshold met. Reconcile action is the relevance gate on assertion: DOWNGRADE-to-social for emerging thin signals; DROP only for contradicted/zero-evidence.

## 7. What exists today vs what is planned

**Exists on master (reuse as-is):** `corroboration.py`, `reconcile.py`, `event_ledger.py`, `display_layer.confidence_label`, `assert_sourced`, `_build_key_metrics`, `_clamp_status_tag`, `_derive_risk_flags`, NOT MEASURED footnote, `render_payload` column, `render_card`, `setup_bigquery.py` SCHEMA_ORDER with `event_ledger`/`reconcile_actions`, LP grounded chat + lexical search, M4 embedding classifier, `composite_integrity`, `predictions_archive`, `cloudbuild.yaml` + `configs/cron_flags.env` deploy path, ensemble `_global_units_spent` ledger.

**Planned (narrow additive build):** `receipt_id`, hardened ensemble pre-call budget + vendor-truth check, idempotency guard, `schema_version`, `claim_receipt` + ConfidenceReceipt chip, grounding verifier, ledger `validity_window` + widened factual fetch, pattern detectors + `detections_archive`, VECTOR_SEARCH hybrid + `semantic_search` LP tool, `relevance.py`, THE READ swap, future-tense validator, `get_dataset()` routing in ops scripts.

## 8. Phased rollout

Each phase ships code dark behind a `cron_flags.env` flag where applicable, proves the OFF path byte-identical by unit test, lands writer-first (TEV2) before reader (Listening Post), and clears its watchdog gate before client-facing promote. Deploy: Cloud Build on master push parses `configs/cron_flags.env` and pushes env to all four Cloud Run jobs (`trends-engine-pipeline`, phase2, regen, resend). Two Cloud Schedulers (00:30 primary, 02:30 fallback) hit the same job. GitHub Actions is CI only.

Writer-first handshake (**planned `schema_version`**): LP read path falls back to prior render when payload version is newer than reader understands; empty-and-dark columns treated as absent.

### Phase 0 (operational, week 1, no reader-facing change)

**Substrate fixes:** route ops scripts through `get_dataset()` (14 hits in `accuracy_watchdog.py`, plus `engine_pulse.py`, `engine_evolve.py`, others). `dry_run_sql` REGISTRY completeness as a test. Harden ensemble pre-call budget (per-endpoint unit estimate, daily `fetch_units_history` cross-check). Idempotency guard with `FORCE_REINGEST` override.

**Shadow status (live 1 Jul 2026):** PR #218 merged `RECONCILE_ENABLED=true`. Shadow is running un-gated (all entities, not Key/Watch-only). This differs from the v3.0 recommendation to gate before resume; Albert chose to resume and watch cost. **Next 7 days:** after each 00:30 UTC cron, run `reconcile_shadow_digest.py`, check `event_ledger`/`reconcile_actions` row counts, monitor Vertex spend via morning-check ($30/mo RED). If spend exceeds ~$15/mo ceiling, revert flag or add Key/Watch entity gate in code. Promotion clock for Phase 1 starts now.

Historical context: PR #184 activated shadow 27 Jun; PR #201 paused 28 Jun for cost. Ledger cost is **3 batched Gemini calls/day** (~$3–5/mo typical), not per-entity calls. The old $8–15/mo figure included reconcile observation overhead and heavy-event days; grounding verifier (Phase 2) is the next cost step.

Surface Stage-1 corroboration (free, live) as internal-only receipt calibration in parallel.

### Phase 1 (labels, add-only)

After ~10 clean shadow days: ship `claim_receipt` chip as labels (factual N / social M). No correct/suppress yet. Test: single Brand24 feed reads as one family end-to-end. `accuracy_watchdog` UNVERIFIED tier green.

### Phase 2 (grounding verifier shadow, ledger deepening)

Grounding pass in shadow with archive backtest. Widen ledger factual sourcing. Add `validity_window`. Gate Key-tier only at first shadow. Human-labelled 100–200 claim set for escape/false-cut bounds.

### Phase 3 (drop/correct live)

Reconcile stale-correction and future-tense validator gate live output. Shadow diff audit with zero true-to-false inversions sign-off.

### Phase 4a: THE READ swap

Ledger-reconciled read; Brand24 demoted to dated social lens. Internal digest first; BSA after ~30-day clean internal window (default).

### Phase 4b: VECTOR_SEARCH hybrid

Post-cron embed job, `semantic_search` LP tool, lexical fusion.

### Phase 4c: Pattern detectors (one at a time)

Ship emergence first (default), then cross-market diffusion. Each clears backtest + degeneracy gate individually.

### Phase 4d: Relevance re-rank

Flip `relevance_rank` ordering; usefulness_prior feedback loop after capture surface ships.

## 9. The foolproof guarantees

Ten rails, each enforced in code or by process, not by prose. (Same substance as v3.0; corrections noted.)

No-Verified honesty rail. Corrector-is-never-trust-boundary. Require-a-source gate (2 factual families, confidence 0.55). Silence beats a guess with thin-day digest variant. Two-numbers law. Every number computed by code. Provenance or it does not render (extends `assert_sourced` to source-pointer-exists once `receipt_id` ships).

Testing and QA. Unit suite: **1579 test functions across 76 files** (1 Jul 2026), Cloud Build deploy gate. `composite_integrity` recomputes every `trend_score`. `audit-eml` and `morning-check` stay daily human QA.

Shadow discipline. ~10 clean days, digest reviewed, watchdog green, human sign-off on zero true-to-false inversions.

Cost guardrails. Vertex-only (WPP). $50 Cloud Billing outer bound; **$30/mo Vertex RED via morning-check procedure** (not an automated script today). Ledger: 3 calls/day. Grounding verifier: shadow-first, Key-tier gated. Embedding: post-cron, enriched_content only. Idempotency guard + `FORCE_REINGEST` for recovery.

## 10. Risks and mitigations

Self-judging bias on grounding verifier: three-layer mitigation (key_metrics backstop, receipt_id resolution gate, human-labelled set).

Claim decomposition: clause split + conservative demote fallback.

Over-claiming corroboration: family collapse shipped; Phase 1 regression test.

Recency/staleness: `validity_window` planned.

**Cost:** ledger shadow is cheap (3 calls/day). PR #201 paused when shadow felt expensive relative to zero reader benefit; corrected math shows ledger alone is not the big line item. **Risk today:** PR #218 resumed un-gated; watch 7 days. **Future risk:** grounding verifier at scale; gate Key-tier, shadow-first.

PR #218 un-gated resume vs documented Key/Watch gate: if cost stays under ceiling, acceptable; if not, revert or code-gate entities.

Abstention recall cost: DOWNGRADE-to-social, not DROP.

Multiplicative R: cap factors, `relevance_integrity` watchdog planned.

Pattern detectors on short series: ship dark, backtest gate.

Semantic recall geo-leak: fuse with lexical, geo-exclusion, threshold+margin.

Two-repo deploy skew: writer-first, **`schema_version` planned**, `get_dataset()` Phase 0.

## 11. What V3 is NOT

Not a rewrite of ingestion or scoring. Not turning forecast on until it beats persistence (`accuracy_watchdog` check 5). Not enabling pattern detectors without `detections_archive` backtest. Not external vector stores or non-Vertex judges. Not client-facing trust labels before ~10 clean shadow days.

## 12. Open questions for Albert

1. **PR #218 watch window:** keep un-gated shadow for 7 days and revert only if cost breaches ~$15/mo, or add Key/Watch entity gate now in a follow-up PR?

2. **Grounding verifier:** Key-tier only at first shadow (recommended), or same entity set as ledger?

3. **BSA THE READ:** internal ~30-day proof before BSA sees reconciled read (recommended default)?

4. **Relevance feedback:** Listening Post thumbs; trusted cohort TBD (Mel, Thapelo, Albert).

5. **Forecast:** leave on hedged outlook chip; event-conditioned calendar is optional future work.

6. **First pattern detector:** emergence first (recommended) vs cross-market diffusion.

7. **Promotion authority:** Albert sign-off on zero true-to-false inversions; Jo co-sign for BSA-facing THE READ swap?
