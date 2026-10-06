# PULSE Intelligence Upgrade, Master Plan

Date: 16 June 2026
Owner: Albert
Scope: turn PULSE (Listening Post) from a trends dashboard into the central Gen-Z intelligence tool for the Google x Ogilvy team. Search anything, surface the hidden intelligence we already pay for, framed around Gen-Z and the rising use of Gemini and its products.

This is the decomposition and design. Each workstream gets its own implementation plan when we build it. Nothing here is built yet.

## The vision in one line

PULSE becomes the place a strategist types any topic, brand, or creator and gets back the full picture from every source we pay for: trends, sentiment, the people driving it, the audience behind it, and a conversation to interrogate it. Mobile-first, honest, every number real.

## How it decomposes

Six workstreams. They map one-to-one onto your five asks plus the deep QA. You said equal importance, so none is dropped, but they build in a dependency order: the foundation (mobile and correctness) underpins every new surface, then the two quick client wins, then the two big intelligence builds.

| WS | Your ask | What it delivers |
|---|---|---|
| WS-0 | Ask 5 + the deep QA | Mobile-first refactor and the backend math and frontend fixes |
| WS-1 | Ask 1 | The live growth-metrics row on Today |
| WS-2 | Ask 2 | Source maxing in the engine plus the platform-spread proof |
| WS-3 | Ask 3 | The Brand24 intelligence layer: central search, influencer CRM, mention drill-through, demographics |
| WS-4 | Ask 4 | The Intelligence Centre chat |
| WS-5 | cross-cutting | Navigation and IA so it reads as one intelligence tool |

Two codebases are involved. WS-0, WS-1, WS-3, WS-4, WS-5 live in the PULSE repo (the Listening Post folder). WS-2 is mostly engine config in Trends Engine V2, shipped as its own CI-gated PR.

---

## WS-0: Foundation, mobile-first refactor and the QA fixes (Ask 5 + deep QA)

Goal: the base must be mobile-first and honest before any new surface lands on top of it, because every new surface inherits the same CSS system and the same data-truth rules.

### Mobile (Ask 5: "I opened the link on mobile and it was shot")

Root cause, confirmed in the audit: there are zero breakpoints below 640px. The board grid forces a 412px minimum on a 380px phone, the detail panel is a fixed 680px desktop slab, the masthead does not wrap, the ticker jams, and the charts use fixed pixel heights. This is a responsive pass, not a few tweaks.

Design:
- A real breakpoint system at 640, 480, 360.
- The board grid collapses to stacked cards on phones (topic, momentum, score), hiding the spark and secondary columns.
- The detail panel becomes a full-screen sheet with phone padding.
- The ticker stacks or hides on phones rather than horizontal-scroll jamming.
- The masthead wraps; charts go fluid with viewport-relative heights.
- Use the frontend-design skill when we build the UI so the mobile result is sharp, not just unbroken.

### Backend math and correctness fixes (the "check all the math" ask)

| Severity | Where | Problem | Fix |
|---|---|---|---|
| Done, staged | bq.py voice pool + clean_items | Receipts showed 9-month-old viral posts | Recency-decayed ranking plus a 45-day publish-age guard. Coded, 82 tests green, BigQuery dry-run validated. Awaiting your deploy word. |
| P1 | ask score formula | `min(0.99, total/1000)` caps all queries above 1000 matches at 0.99 | Rescale the divisor or normalise against the live max so big topics separate |
| P1 | momentum | Short histories padded with zeros over-call "Building" | Require a minimum real window or return Steady when data is thin |
| P1 | series contract | 30-point series vs a docstring claiming 14 | Reconcile to 30 across code and contract |
| P1 | lexicon wow | Week-over-week can show misleading negatives or divide oddly | Guard the denominator and the sign |
| P2 | series normalisation | All-zero series renders a fake flat line | Add a has-data flag so the client shows "no activity" honestly |
| P2 | delta | Reads wrong when yesterday is missing | Only compute when both days exist |
| P2 | freshness | Clock skew can produce a negative age | Clamp at zero |

Frontend bugs to fix alongside: the async ask poll has no cancellation (add an AbortController), the client API cache grows unbounded (add an LRU cap), and the opportunity fetch needs cleanup on unmount.

Deliverable: a mobile-first, honest base. Ships in slices, each deployed and smoke-tested.

---

## WS-1: Growth-metrics live row (Ask 1)

Goal: the top of the Today page shows that we gather more data every day. You chose a live metrics row.

Design: a row of counters, each with its value and a daily delta, count-up animated, mobile-first.
- Total signals scraped, all time (the hero, always climbing).
- New today.
- Influencers tracked.
- Active sources (8).
- Markets (3).
- Briefs generated.

Data, all real and verified in the audit (no fabrication):

| Counter | Source of truth |
|---|---|
| Total signals, all time | `raw_content` cumulative row count |
| New today | `pipeline_runs.total_rows` summed for today |
| Per-connector | `pipeline_runs.{connector}_rows` |
| Influencers tracked | creator watchlist file sizes in `configs/creators/*` |
| Sources / markets | the 8 connectors, the 3 markets |
| Briefs generated | `pipeline_runs.briefs_generated` |

Build: a `fetch_growth_metrics` query in bq.py, a `/api/metrics` endpoint with caching, a MetricsRow component on Today.

---

## WS-2: Source maxing and platform-spread proof (Ask 2)

Goal: pull a lot more data from every source, and answer the client's "why is TikTok so low" by showing breadth across many platforms.

### Why TikTok looks low (the client answer)

It is throttled, not broken. Three caps stack: only 2 hashtags and 2 keywords run per market per cycle out of a 12-to-20 term pool, the per-run unit budget is 800 and shared, and the richest TikTok surfaces (comments, post info) are dark.

### The levers (engine repo, config, ~+600 to 900 rows/day, low risk)

1. TikTok hashtags and keywords 2 to 4 per market.
2. EnsembleData per-run budget 800 to 1200.
3. BigQuery Trends top_terms on (+200/day).
4. Brand24 AI Insights on.
5. YouTube playlist items on.
6. Then the creator-tier boost once validated.

These ship as a separate CI-gated PR in Trends Engine V2. They are your call to merge, since they touch the live cron.

### The PULSE side

A platform-spread surface tied to the metrics row that shows the full breadth (all eight sources, counts climbing), so the "where it lives" bars read as proof of a wide, growing base rather than a thin TikTok line.

---

## WS-3: Brand24 intelligence layer (Ask 3), the core of the intelligence tool

Goal: tap everything we pay Brand24 for, across all three markets, into one searchable central tool. This is the heart of "make the Pulse an intelligence tool."

Verified: the entire Brand24 surface is reachable from one REST API with the key we already hold, server-side, no MCP and no scraping. Confirmed live against the real ZA, NG, KE projects. Demographics included (the earlier "dashboard-only" call was wrong).

The endpoints we will use (base `https://api-data.brand24.com/api-data/v1`, header `X-Api-Key`):

| Endpoint | Feeds |
|---|---|
| `/project/{id}/topics` | AI Topic Analysis: clusters with share of voice, reach, sentiment split |
| `/project/{id}/mentions` | the drill-through (sentiment and category filters, cursor pagination) |
| `/project/{id}/mentions/sentiment` and `/daily-metrics` | sentiment streams, engagement, per-platform split |
| `/project/{id}/most-followers` and `/domains` | influencers and sources, the latter with an influence score |
| `/project/{id}/demographics` | age by gender, country, interests, education, income (7-day windows; wide windows time out) |
| `/project/{id}/ai-insights` and `/ai-summary` | Brand24's own narrative intelligence |
| `/project/{id}/hot-hours` | best day and hour to post |
| `/project/{id}/project_events` | spike and anomaly detection |
| `/account/mentions-usage-estimation` | our own cap headroom, shown in the UI |

Architecture: a new server-side `brand24.py` client in the PULSE backend, the key from env, responses cached in-process and in the GCS bucket, demographics sliced into 7-day windows. New `/api/intel/*` endpoints.

Surfaces in PULSE:
- Central search. Type anything. It resolves to a topic or keyword and returns topics, sentiment, mentions, top authors, and demographics for it across markets. The "Google for the intelligence we pay for."
- Influencer CRM. Click any influencer to a mini-profile: followers, reach, mentions, platforms, sentiment, sample posts, influence score. Save and tag them into lists. A real mini-CRM, as you asked.
- Mention drill-through. Click a topic or influencer to the actual mentions, filtered by sentiment or platform.
- Demographics panel. Age by gender with the Gen-Z bands (18-24, 25-34) highlighted, plus country and interests.
- The Gen-Z and Gemini lens. The seeded `tech_gemini_ai` topic and the AI-adoption angle surfaced as a first-class view.

---

## WS-4: Intelligence Centre chat (Ask 4)

Goal: Jo's chat with a history log, an intelligence centre behind a login wall, the Sunday Times use case. You chose a standalone Intelligence Centre page.

Design: a conversation-first page with a saved per-user history log, paywall-gated. You ask it anything and it answers from our data with citations.

Architecture (compliance-safe, stays inside Vertex, no external LLM):
- Gemini on Vertex AI with function-calling. The model is given tools that query our own data: query_trends (BigQuery), query_brand24 (the WS-3 client: topics, sentiment, mentions, authors, demographics), search_mentions, get_metrics.
- Every answer is grounded and cited: each claim traces to a BigQuery row or a Brand24 endpoint, so it never free-associates.
- Per-user conversation history stored in a BigQuery table or GCS, retrieved per session.
- Reuses the proven async pattern (202 plus poll) for long answers.

This is what makes PULSE the intelligence centre rather than a dashboard.

---

## WS-5: One intelligence tool (cross-cutting)

Navigation and information architecture so the whole thing reads as one tool: Today, Desk, Intelligence (Brand24 search and CRM), Chat. Mobile-first throughout. Consistent voice and the honesty rails everywhere.

---

## Sequencing

All equal priority, built in dependency order so each slice ships and is verified:

1. WS-0 foundation. Mobile-first base plus the math and correctness fixes. Everything else sits on this.
2. WS-1 metrics row and WS-2 source maxing. Fast, visible client wins. WS-2's engine PR is the TikTok answer.
3. WS-3 Brand24 intelligence layer. The centerpiece.
4. WS-4 Intelligence Centre chat. Consumes WS-3's data client.

Each ships in deployable slices, mobile-first, verified, smoke-tested live.

## Open decisions for you

1. The chat paywall: reuse the existing passcode gate, or a real per-user login.
2. CRM persistence: saved influencer lists in BigQuery, or local to the browser to start.
3. WS-2 engine PR: ship the source-maxing levers now as the TikTok answer, or hold for the batch.
4. Demographics window: default 7-day slices, with an option to step back week by week.

## What is already done

The receipts recency fix (part of WS-0) is coded, tested (82 green), and BigQuery-validated. It is staged and waiting only on your deploy word.
