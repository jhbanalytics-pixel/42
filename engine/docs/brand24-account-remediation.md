# Brand24 account remediation

Written 2 Jul 2026, after Maja Krawczyk (Brand24 CS) flagged the account at 500k+ mentions used, projected 2.2M this month, against a 100k monthly limit. Live API audit run the same morning against all three projects. This doc is the fix spec for the session with Maja and for whoever holds the app login (jhb.analytics@gmail.com); the api-data key is read-only, so every change below is made in the Brand24 app UI or by Maja.

## What the audit found

June collection volume per project (from `/mentions/sentiment`, 1 Jun to 2 Jul):

| project | June mentions | daily avg |
|---|---|---|
| South Africa trends (1397483532) | 728,759 | ~24k |
| Nigeria trends (1397483537) | 835,277 | ~28k |
| Kenya trends (1397483539) | 430,480 | ~14k |
| total | ~1.99M | ~66k |

That is 20x the 100k cap. Maja's 2.2M projection is real, not a billing glitch.

Root cause 1, global keywords with no geo anchor. Every project carries `Gemini` and `Nano Banana` as keywords with an empty `required` list and only 12 excluded terms (the local-culture keywords each carry 47 exclusions). So each project ingests the entire planet's Gemini AI chatter three times over. A sample of 100 KE mentions matching "Gemini" surfaced Indonesian motorcycle reviews, French LLM-security blogs, Bosnian Android Auto news, a Toronto Deloitte job ad, Turkish tech press, Brazilian email-hosting listicles, and zodiac TikToks. Zero Kenya relevance. In the text-attributable sample, Gemini + Nano Banana were 87% of KE volume and 68% of ZA volume.

Root cause 2, empty social pings. Measured on the 1 Jul stream: 67% of ZA, 69% of NG, and 63% of KE mentions carry null title and null content, almost all X + Instagram (the engine's own Day-1 measurement: 97% of X rows and 99% of IG rows are empty). They count against the cap and carry zero classifiable signal; the engine already skips them at ingestion.

Root cause 3, other unanchored broad terms. ZA `kasi` collides with Malay/Indonesian/Filipino "kasi" (the exclusion list patches some of it but the sample still shows leakage), and NG `lagos` picks up Lagos, Portugal.

## The fix, in order of leverage

1. Geo-anchor `Gemini` and `Nano Banana` in all three projects. In the app's keyword settings, populate the `required` list (any-of co-occurrence) per market:
   - ZA: `south africa, mzansi, joburg, johannesburg, cape town, durban, pretoria, saffa, eskom, rand`
   - NG: `nigeria, naija, lagos, abuja, nollywood, nairaland, pidgin`
   - KE: `kenya, nairobi, mombasa, sheng, mpesa, kot, safaricom`
   This is the single biggest cut. Expected to remove most of the estimated 50 to 70% of volume these two keywords drive.
2. Turn off the X and Instagram sources per project, or ask Maja whether empty-body pings can be excluded from the count. The engine gets its social depth from EnsembleData (TikTok, IG, Threads) natively; Brand24's X/IG rows are mostly empty pings we discard anyway. Trade-off to raise with Maja: non-empty X rows are the engine's only X signal, so prefer "exclude empty pings" over "kill the source" if they can do it.
3. Anchor `kasi` (ZA) with the ZA required list above, and add `portugal` to the `lagos` exclusions (NG).
4. Re-measure after 48 hours via `/mentions/sentiment` daily counts. Target is under ~3.3k/day total to fit 100k/month. If steps 1 to 3 land at 5 to 10k/day (plausible: amapiano, afrobeats, jollof are genuinely high-volume global terms), the residual conversation with Maja is plan sizing: either a tier that covers 150 to 300k/month or further required-anchoring of the culture terms.

## What this does NOT touch

The engine's own connector budget is already safe and unrelated: `mentions_max_per_run: 600` x 3 projects caps our API pulls at ~54k rows/month, and the aggregate endpoints are ~9 calls/day. Maja's limit is about what the projects COLLECT, not what we pull. Nothing in `configs/sources.yaml` needs to change for this, and the engine's brief quality should improve, not degrade, because the topics/hashtags/ai-insights surfaces will stop summarising Indonesian motorcycle content into the SSA lens.

## Verification loop (after changes land)

```bash
# daily counts per project, run from the TEV2 repo with the brand24 key in B24_KEY
curl -s -H "X-Api-Key: $B24_KEY" "https://api-data.brand24.com/api-data/v1/project/1397483532/mentions/sentiment?date_from=<from>&date_to=<to>"
```

Watch three things: total daily volume trending to target, the KE Gemini sample turning Kenyan, and the engine's `brand24` row counts in the morning check staying healthy (engine-pulse expects ~275/day; the connector reads capped surfaces so it should barely move).
