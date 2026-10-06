# Trends Engine V2, Composite Score Methodology

Reference for how the daily `trend_score` is built and why each constant is what it is. Every constant below is fitted to the live 30-day `trend_scores` distribution (801 rows, 3 markets, pulled 2026-06-16), not chosen by feel. The goal is that every number in a stakeholder-facing brief can be defended.

The composite is a weighted sum of eight signals in `configs/scoring.yaml` that carry an unconditional weight and sum to 1.00. A ninth, momentum, carries a configured 0.07 carved from the velocity weight and applies only when its flag is active. Three more signals are configured at 0.00 and take no part. A cross-source confirmation step then applies, capped at 1.0. Tier floors (Key 0.45, Emerging 0.30, Monitoring 0.18) are the p90/p75/p50 of the observed distribution.

## Live distribution (30-day pull, the basis for every constant)

- item_count per (market, topic, day): p10 22, p25 55, p50 74, p75 151, p90 310, max 790. Only 3 of 801 rows sit below 3 items; none at 0.
- trend_score: p50 0.302, p90 0.403, max 0.589 (consistent with the 28-May recalibration).
- velocity_score == 1.0 (the new-topic case): 17 rows (2.1%), average item_count 225, none thin.
- engagement_score saturates at 1.0 for 14.5% of rows. Per-row engagement-per-day (row level, 14-day): p50 4, p90 23,960, p95 150,201, p99 6.68M, max 380M (heavy-tailed, as expected for social).
- Cross-source: under the platform-string rule that was live at the time of the pull, the multiplier fired on 234 of 247 topic-days (95%). Under the channel-family rule that replaced it (section 2) the distinct-channel count per topic-day spreads 1:10%, 2:7%, 3:20%, 4:37%, 5:22%, 6:4%.

## 1. Velocity for new topics (velocity.py)

Retired: `_normalise` used to return a flat `NEW_TOPIC_SCORE = 1.0` whenever there was no baseline and `today_count > 0`, on the largest-weight signal (0.20).

Data check: the thin-new-topic risk the audit raised is empirically near-absent. New-topic rows average 225 items and none are below the 3-item floor, because a topic is an aggregate of many rows. So a hard `min_items` gate would change essentially nothing today. We say this plainly rather than overstate the fix.

Live (shipped, low live impact, principled and future-proof): the flat 1.0 is replaced by a volume-scaled new-topic score in `velocity.py`
`new_topic_score = min(1.0, today_count / NEW_TOPIC_FULL_VOLUME)`
with `NEW_TOPIC_FULL_VOLUME = 75` (the live median item_count). A brand-new topic earns full velocity only once it reaches typical-topic volume; a 5-row novelty scores 0.07, not 1.0. This is the standard fix for ranking by a rate with no denominator: scale confidence by sample size rather than award the maximum on any positive count.

`min_items_for_scoring = 3` is kept and documented as a non-binding evidence floor (it guards the degenerate sub-3 case, 3 rows in 30 days), not promoted to a hard filter, because raising it toward the p10 (22) would start dropping legitimate emerging topics.

## 2. Cross-source confirmation multiplier (run_rss_now.py + scoring.yaml)

Retired: a flat `1.15x` when `len(platform_set) >= 2`. Two defects, both confirmed in data:
1. It fires on 95% of topic-days, so it is near-constant inflation that compresses scores toward the 1.0 cap rather than a signal that discriminates strong from weak corroboration.
2. `platform_set` counts distinct `platform` strings, and Brand24 alone emits web/facebook/tiktok/instagram from one feed, so a single-vendor topic earns a "confirmed across channels" bump (about 5% of fires).

Live: the bonus is graded by the number of independent CHANNEL FAMILIES, not platform strings. A channel family is one ingestion channel type: `ensemble`, `reddit`, `brand24`, `search` (BigQuery Trends, Google Trends RSS, Semrush), `music` (Apple Music, Audiomack), `apps` (app charts), `youtube` (API plus yt-dlp scrape), `wikipedia`, `bluesky`, `web_attention` (Cloudflare Radar), and `news` (all RSS plus GDELT domains collapse to one, since 6,063 of the distinct sources are news outlets). Mapping is by `source` first (`_CHANNEL_FAMILY_BY_SOURCE`), then by `platform` for rows whose source is a feed or channel name rather than a connector name (`_CHANNEL_FAMILY_BY_PLATFORM`), and anything still unmatched falls through to `news`. Same-platform content from a second social vendor collapses into the same family, so a second vendor on TikTok is not counted as a second channel.

Graded multiplier, live in `run_rss_now.py`: `1 + min(BETA * (n_channels - 1), MAX_BONUS)` with `BETA = 0.05` (`cross_source_bonus_per_channel`) and `MAX_BONUS = 0.15` (`cross_source_max_bonus`), both in `configs/scoring.yaml`.
- n=1 (single channel, 10% of topics) -> 1.00 (no corroboration, no bonus; under the retired rule these wrongly got 1.15)
- n=2 -> 1.05, n=3 -> 1.10, n>=4 -> 1.15 (capped)

`BETA` and the cap were chosen so the median topic (n=4, 37% of topics) keeps the 1.15 multiplier it had under the flat rule. That preserves the tier calibration (floors were fitted with the flat 1.15 in place) while making the bonus scale with independent corroboration and removing it from single-channel topics. Net effect is a small downward shift confined to low-corroboration topics, checked against the validation protocol below before it shipped.

## 3. Engagement normalization (enrichment.py + scoring.yaml + run_rss_now.py)

Live: per-row engagement is content-type weighted, capped at `engagement_per_day_cap = 50000` per day, summed, divided by `item_count`, then a `/5000` per-row score ceiling.

Data check: at the individual-row level the 50,000/day cap sits at roughly the p90-p92 of per-row engagement-per-day (p90 23,960, p95 150,201), so it does real work capping the viral tail. The audit framing that the cap is "ten times the score ceiling and mostly dead" conflated the per-row cap with the per-topic-average ceiling, which operate at different stages and are not directly comparable. We therefore do NOT loosen the cap.

Shipped:
- `engagement_per_day_cap = 50000` is documented in `configs/scoring.yaml` as an empirical winsorization at approximately p90 of the live per-row distribution (winsorization is the standard outlier-resistant treatment for heavy-tailed engagement: cap extreme values at a percentile rather than let one viral row dominate). The value stayed; the justification is now data-cited rather than magic.
- Row 096: zero-engagement catalog rows (`content_type == youtube_channel_upload`, views/likes/comments all 0) are excluded from the engagement average denominator via `_ZERO_ENGAGEMENT_CONTENT_TYPES` in `run_rss_now.py`, so a creator's back-catalog does not dilute a shared topic's engagement_score. These rows still count for volume and classification; they are removed only from the engagement-intensity divisor.

## 4. genz_score and regional_score keyword relevance (enrichment.py + configs/keywords)

The `relevance_score` computation below is shared by both signals and is still live. genz_score's resulting value is retained for historical and audit purposes only and carries zero weight in the composite (`configs/scoring.yaml`); regional_score's value keeps its configured weight.

Retired: `relevance_score = min(1.0, hits / 3.0)` where `hits` counted markers that appeared as a plain substring of the text. Two defects:
1. Substring matching on ubiquitous tokens (`tiktok`, `meme`, `student`) produces false hits inside unrelated words and on platform names present in almost any social text.
2. `hits / 3.0` quantizes hard to {0, 0.33, 0.67, 1.0} and caps at 3, so `genz_score` (then the second-largest signal at 0.17, now retained at 0.00) behaved as a near-binary "mentions a youth word" flag.

Live, all three parts shipped in `src/ingestion/enrichment.py` and `configs/keywords/`:
- Boundary matching, written as lookarounds rather than `\b` so multi-word and punctuated markers such as `gen z` and `+254` anchor correctly. `student` no longer fires inside `students'-union-building`-style runs and short markers do not match inside longer words.
- The hard `hits/3` is replaced by a saturation function `hits / (hits + K)`, `K = 1.5` (`RELEVANCE_SATURATION_K`, the Robertson/BM25 term-frequency saturation form: first hits count most, additional hits give diminishing returns, no hard cap). hits=1 -> 0.40, 2 -> 0.57, 3 -> 0.67, 5 -> 0.77. Smoother and saturating rather than capped, so a strongly on-topic text outscores a one-word match without a cliff at 3.
- The marker lists dropped the ubiquitous platform names that carry no youth-distinctiveness (`tiktok` in all three markets, plus `youtube shorts` and `meme` in ZA) and kept the distinctive slang and context terms. Applied to both genz and regional markers (they share `relevance_score`).

## Validation protocol

This is the gate every change above passed, and the gate the next one has to pass. The last 14 days of `trend_scores` are re-scored with the old and new formulas and the tier-distribution and top-N ranking shifts are reported. A change ships only if the shift is explained by the intended mechanism (thin/low-corroboration/false-keyword topics moving down, genuinely corroborated high-volume topics holding or rising) and the tier floors still partition sensibly. If the cross-source regrade moves the median materially, the tier floors are re-fitted in the same change rather than left stale.

Sources for the methods used: winsorization (robust capping of heavy-tailed metrics at a percentile), Robertson/BM25 term-frequency saturation (diminishing returns on repeated term hits), and sample-size confidence scaling for rate-style signals.

## 5. Per-market embedding threshold (066)

`EmbeddingClassifier` carries a per-market `(threshold, margin)` map (`MARKET_THRESHOLDS`); a market absent from it uses the instance default (0.65 / 0.05). KE was the motivation: it classifies worst (58.6 percent unclassified vs NG 48.9 / ZA 46.1, 14-day pull).

A 2026-06-16 sweep embedded a 400-row random KE unclassified residual sample plus the KE topic anchors via the Vertex REST predict endpoint (the gRPC client segfaults on the Windows dev box, so REST is the local-safe path), max-pooled cosine faithfully to the live gate (KE exclude-set, low-signal prefilter), and swept the threshold at margin 0.05:

| threshold | label-rate | of which clean | note |
|---|---|---|---|
| 0.65 (current) | 4.2 percent | 4.2 percent | precision-safe |
| 0.62 | 10.8 percent | 10.0 percent | recovered rows mostly false positives |
| 0.60 | 15.2 percent | 13.8 percent | more false positives |

Precision spot-read at 0.62 was decisive: the recovered rows are mostly generic `[r/Nairobi]` Sheng chatter ("Ni wewe tu", "Na kwani aliwacha mkate") magnetized to `tech_gemini_ai` (whose KE anchors include Sheng like "gemini app imenisaidia na assignment yangu"), plus a Vietnamese Gemini-zodiac geo leak. So lowering KE's threshold manufactures inaccurate topic tags rather than recovering signal.

Decision: KE stays at 0.65 (the precision-safe operating point this experiment validates). The per-market map is wired so the decision is explicit and data-backed, and so a future change can tune one market without touching the global default. The real KE lever is anchor curation (tighten or drop the over-attracting `tech_gemini_ai` Sheng anchors, or add it to KE's `EMBEDDING_RESCUE_EXCLUDE`), which is a separate change that needs its own precision validation; it is not the threshold.
