# V3 WOW demo: intelligence vs proof

## What changed (42 phase R intelligence)

Before this pass, the Evidence sources panel surfaced raw post text first. Six TikTok snippets with no synthesis read like hashtag soup, not report-grade intelligence.

After 42 phase R intelligence, evidence is tiered:

Engine signal (Tier 1) always leads. This is brief synthesis from `trend_analysis` (headline, trend_synthesis, cultural_context), seed behaviours from `seed_insights`, the daily digest through_line from `daily_summary`, and rising search terms per focus topic. Lexicon terms sit in Search & reach.

Voice proof (Tier 2) is posts and comments from `enriched_content`. They are receipts linked to the engine read, not the headline evidence.

## UI

Evidence sources groups into three sections:

| Section | What you see |
|---------|----------------|
| Engine signal | Behaviour line + synthesis excerpt, topic tag, market |
| Voice proof | Post or comment text, platform, engagement, topic tag |
| Search & reach | Rising search velocity, lexicon term |

Header badge example: `12 engine · 34 voice · 4 search`.

Citation drawer labels engine refs as "Engine intelligence" and shows synthesis fields, not raw post dumps.

## Gather and rank

`_parallel_gather` pulls Tier 1 sources in parallel, then voice with a 15+ post floor per pull.

`rank_and_filter` reserves quotas by ref_type before the relevance cut so brief, seed, and digest refs are never zeroed by post scores.

Consolidated docs enforce minimums: 8 brief-derived refs, 5 seed refs, 3 search refs, 15 voice receipts per behaviour section. Each section orders brief + seed + search before voice samples for that topic.

## Synthesis guard

Gemini prompts require objective and tension to cite brief and seed refs primarily. Validation rejects docs when more than 70% of grounded citations are post-only.

## Staging

Branch `feat/lp-r-staging`. Redeploy `listening-post` on Cloud Run after merge to pick up the new evidence UI and gather logic.
