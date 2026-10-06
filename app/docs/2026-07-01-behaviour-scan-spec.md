# Behaviour scan: brief scoping step

Repo: Listening Post (`listening-post` Cloud Run)
Status: build in progress, shipped dark behind flags
Owner ask: Thapelo Masebe (T), locked over Slack 1 Jul 2026

## Problem

The Build brief flow goes persona, gather, full synthesis in one shot. T wants a pause in the middle. Show the behaviours the pulled posts actually carry, with proof, let a human approve, then write the brief. Today evidence only appears after the brief exists. T wants evidence before synthesis so the behaviour is seen and signed off first.

His words: the first approach goes too far ahead, it synthesises the evidence before it is seen. What is needed is breaking down the signal topics with post examples and numbers, then a go ahead to the brief.

## Locked decisions

These are confirmed by T, not assumptions.

1. Behaviour is one plain-English line, like "constant conversation around M-Pesa signalling mobile money use". Not a topic slug, not an internal label.
2. Markets: ZA, NG, KE are all in. 10 behaviours per market.
3. Spread is diverse, not just the top by volume. Market-wide scan first, persona is chosen after behaviour approval, not at the start.
4. Proof per behaviour: post count (volume) or mention count (engagement), plus 2 to 3 real post examples, platform and market tags when available.
5. Interaction: approve or reject per row, with a short free-text note per row.
6. Minimum one approved behaviour to generate. Up to three can go forward, but the executions stay separate.
7. Output per approved behaviour is its own brief. Not one document with three blocks merged. Separate output, one activation per behaviour.
8. Output keeps the existing Jo sections (Objective, Know the user, Evidence bank, AI Mode activation) and adds T's storyboard beat table: Friction, Interaction, Resolution, plus a why it works line and the evidence under it. His Commute Crisis example is the shape.

## Jo's answers, reconciled with T

Jo answered the same questions. Where they differ, both are folded in.

- Count and markets: agree with T. Around 10 per market. NG and KE for this brief, ZA handled too, with a market filter so it scales. Built: 10 per market, plus a market filter in the scan.
- Ranking: ranked by volume and strength, with a diverse spread shown as behaviour buckets. Matches the trend_score ranking plus distinct-topic spread already built.
- Persona: market-wide view first, persona targets this brief, dig to persona level and compare later. Matches T (persona after approval). Compare is a later slice.
- Proof: minimum 3 example posts with links, platform mix, market tag, trend or velocity score. Built: examples now carry the post link, platform, handle, and the row shows the trend score. Was 2 to 3, now shows up to 3 and surfaces the link count.
- Go-ahead: Jo prefers a topline behaviour list that expands to dig deeper, and is unsure about approve/reject. T wants approve/reject plus notes. Built: topline rows that expand to the examples on demand, and keep approve/reject plus a note (T is the primary console user). Notes tie into the market filter Jo asked for.
- Output: keep the current brief structure scoped to the chosen behaviour. Jo also wants an HTML file download of the findings and recommendations. Added as slice 3.

## Open items (not blockers, resolve before the generate slice)

- Product frame timing: chosen before scan, or per behaviour after approval. T's commute example bakes Google AI Mode into the interaction beat, so likely per behaviour.
- Evidence placement in the output: per beat, per behaviour, or one Evidence bank for the whole brief.
- Whether approve/reject stays or collapses to Jo's lighter select-to-dig model once both use it live.
- Compare across persona or market groupings (Jo, as we scale).
- HTML download of findings and recommendations (Jo). Slice 3, output stage.
- Data depth: reuse the current gather pool, or a deeper pull to guarantee 10 diverse behaviours with real examples per market.

## Flow

```
Open console
  -> Build brief
  -> Behaviour scan (NEW): market-wide, 10 per market, proof under each
  -> Approve / reject / note per row
  -> Choose persona and product frame  (moves to AFTER approval)
  -> Generate one brief per approved behaviour
  -> Each brief: Jo sections + storyboard beats + evidence
```

The scan step is new. The persona picker moves from step one to after approval. The synth stays frozen except for the added storyboard beats, which is a later slice.

## Data model

A behaviour is derived from the engine's own topic clustering. No new AI call, no new ingestion.

Source tables (read-only, already live):

- `trend_scores`: `market`, `query_group`, `trend_score`, `velocity_score`, `item_count`, `trend_date`
- `trend_analysis`: `headline`, `trend_synthesis`, `cultural_context`, `sentiment_summary` per market and query_group
- `enriched_content`: post text, platform, author_handle, engagement_total, topic_groups

Per market, take the top topics by `trend_score` (no persona filter). Each topic becomes one behaviour candidate:

| Field | Source |
|---|---|
| `id` | market plus query_group |
| `market` | trend_scores.market |
| `signal_topic` | `topic_label(query_group)` |
| `behaviour` | one line from `cultural_context` or `headline`, humanised |
| `metric.post_count` | trend_scores.item_count |
| `metric.mention_count` | Brand24 mention count when available, else null |
| `metric.trend_score` | trend_scores.trend_score |
| `examples` | 2 to 3 posts from enriched_content for that market plus topic_group, ranked by engagement |
| `platforms` | distinct platform across the examples |
| `query_group` | raw key, kept for the generate step |

Diverse spread: rank by trend_score, then keep the top N distinct query_groups per market so one topic family does not fill the list.

## Slices

### Slice 1 (this build): behaviour scan, dark behind a flag

- `bq.fetch_market_topics(markets, per_market, trend_date)`: top topics per market, no group filter.
- `behaviours.scan_market_behaviours(markets, per_market=10, trend_date=None)`: derive behaviour rows plus examples, no synth, no Gemini.
- `GET /api/research/behaviours?markets=za,ng,ke`: returns the scan, gated by env `BEHAVIOUR_SCAN_ENABLED`.
- Frontend `BehaviourScan` screen behind `BEHAVIOUR_SCAN` const (default off): list per market, approve/reject/note per row, selection count, continue action.
- Tests: derive logic with monkeypatched bq, endpoint gate, route and boundary gates unchanged.

No live behaviour change. The flag is off, so the current brief flow is untouched.

### Slice 2 (next): persona after approval, generate per behaviour

- Move persona and product frame picker to after approval.
- Pass approved behaviour context (query_group, market, behaviour line, evidence refs) into `generate` per behaviour.
- Fan out one job per approved behaviour, one artifact each.

### Slice 3 (next): storyboard beats in synth

- Extend the synth activation contract with a beat structure: friction, interaction, resolution, why_it_works, evidence_indices.
- Keep the paste-ready `prompt` for copy and export.
- Render the beat table in the brief, evidence under it.

## Guardrails

- No new ingestion, no new Gemini call in slice 1. The scan is pure BigQuery reads.
- Do not touch the live persona brief flow while the flag is off.
- Keep `#/research` and `#/console?work=brief` compatibility intact.
- No PII in the scan payload beyond what the desk already exposes (public post text and handles).
- Respect the research rate limiter.
