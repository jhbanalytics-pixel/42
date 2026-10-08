# Trust: how 42 avoids putting wrong information forward

Version 1, 28 September 2026. Reads with ENGINE.md, DATA.md, AGENT.md and core/eval. Rule: when trust and coverage conflict, 42 publishes less. An item held back with its reason shown is a success. Numbers in brackets point to section 9.

## 1. Failure modes and guards

| # | Failure | Gap in the current plan | Guard |
|---|---|---|---|
| A1 | Feed personalisation | TikTok feed=local comes through a vendor account, about 24 videos a pull, shaped by that account [1] | Feed rows seed candidates and set "seen unbiased"; they never measure volume. Track daily feed overlap and topic concentration; a jump marks the feed drifted |
| A2 | Targeted-search bias | Spread (`ps` in DATA.md) counts platforms our own searches found; histories rebuilt from one search skew recent | Measurement panel (section 4). Spread counts a platform only if the item also rises there. Search rows are presence only and are never backfilled into a series. Placebo expansions on 5% random items set the base rate for "found on 3 platforms" |
| A3 | Vendor regime change | source_regime stored, breaks not detected | Change-point test (ruptures) on daily route totals and 50 anchor items; a break makes that route cold and suppresses ratio claims for 14 days |
| A4 | Missing or partial days | `IFNULL(posts_adj,0)` turns a failed day into zeros; effort factor k unbounded | collection_health per market, platform, route, day: valid when 80% of calls succeed and items are 0.5 to 2 times the 28-day median. Invalid days are NULL; k only inside [0.5, 2] |
| A5 | Geo misattribution | Diaspora, name collisions, Pidgin and Sheng misread; GDELT geocoding bias [2]; Wikipedia country views noised and cut below 90 to 1,000 views [3] | local_share counts only posts with geo_confidence of 0.7 or more; "diaspora-led" label; Wikipedia country rows are context, never a rise alone |
| A6 | Coordination and bots | Kenya has a paid hashtag industry [4]; LLM bot text beats text classifiers [5] | Section 5 |
| A7 | Sponsored content | Extractor's sponsored field unused in the gate | Platform labels, caption markers and brand-owned accounts give sponsored_share. ARB Appendix K requires #ad in SA [6], but compliance is patchy |
| B1 | Small counts | Emerging passes on `med = 0` with 3 creators | Creator floors and pooled priors (section 4) |
| B2 | Multiple testing | z >= 2 flags about 2 in 100 unchanged items a day, more when counts are overdispersed: dozens of false risers per market | Negative binomial test with Benjamini-Hochberg FDR [7][8] |
| B3 | Seasonality | 28 days cannot see last year's Heritage Day or payday; no weekday factor | Weekday factor, moments calendar, same-moment-last-year check: label Seasonal, not Rising |
| B4 | Cold start | baseline_state 'cold' computed, unused | Valid days 14 to 27 (before that, warm-up states): pooled prior for new items |
| B5 | Recurrence as novelty | Map matching only | A peak in the last 365 days labels it Recurring, with the last wave's date and height |
| C1 | Unsupported claims | Critic from the writer's own model | Section 3; checker and critic in a fresh context, from another model where the blind test allows [9] |
| C2 | Wrong numbers | query_id planned | Every numeral pinned to its run_id and result hash over append-only tables and re-run exactly; every numeral reconciled |
| C3 | Wrong citations | SHA-256 quote check | Code checks every quote word for word against stored post text, then id, handle, date and market |
| C4 | Overconfident labels | The model picks the label | Code sets the highest label evidence allows; models may only downgrade |
| C5 | Local-language misreading | Big LLM gaps in African languages [10] | Tone benchmarked on AfriSenti and NaijaSenti before use; quotes in the original; glosses marked as machine glosses; tone claims on non-English posts capped at Single source until the language passes native checks |
| C6 | Date errors | resolve_dates exists | Day boundaries in SAST, WAT, EAT; every date in text equals a cited published_at or lies in the window |
| C7 | "Who started it" | ENGINE.md asks for it | "Earliest post 42 found", with lanes searched |
| E1 | Over-trust | Percentiles, long explanations | Section 7 |

## 2. The publish gate

Runs on the morning brief before 06:30 and on every Ask answer. Nothing is silently dropped: blocked items go to "Held back" on Today with the reason.

Item rules (trend cards):

| Rule | Condition | Action |
|---|---|---|
| G1 | Any of the last 3 market-days invalid on the item's main platform (days with no history yet are not invalid; they put the item in warm-up) | Hold; "Data issue" |
| G2 | Fails section 4 (q, ratio, floors, persistence) | Not a trend |
| G3 | Seen only in expansion or agent_live lanes | Never Rising; "Found by search" |
| G4 | Likely coordinated (section 5) | Held back, flag and evidence shown |
| G4b | Election or political item (SA local elections 4 November 2026; NG and KE campaign seasons) | Publishes only when Corroborated and authenticity Clear |
| G5b | Tag on the Ogilvy and client campaign hashtag list | Paid-led automatically |
| G5 | sponsored or brand-owned share 0.5 or more | "Paid-led", out of the organic list |
| G6 | local_share under 0.6, computed only over posts with a known location (geo_confidence 0.7 or more from ext.region, creator home market or place mentions; language alone never counts), with at least 8 such posts | Out of that market's list; under 8 known posts the card says "Market unconfirmed" instead of dropping it |
| G7 | Route regime break in the last 14 days | No growth figure |
| G8 | Calendar or last-year match | Seasonal, not Rising |
| G9 | Cluster label not supported by 3 of 12 representative posts | Named by top hashtag or sound |
| G10 | Explanation fails claim checks after one repair (and, when the critic held only its why-now, after one critic-informed second draft) | Held, reason shown |

Claim rules (answers and explanations):

| Rule | Condition | Action |
|---|---|---|
| K1 | Evidence id missing or unresolved; quote not verbatim | Cut |
| K2 | Numeral without query_id, or re-run differs (counts exact, ratios 2%) | Cut |
| K3 | Evidence outside the window, or from another market | Cut |
| K4 | Checker unsupported or below calibrated threshold | Cut; partial gives downgrade |
| K5 | Label above what code allows | Downgrade |
| K6 | Age or generation term, demographic inference, Google Trends, Gemini output as evidence | Cut; logged as breach |
| K7 | Coordination or payment attributed to a named party | Cut |
| K8 | Non-English quote without original, or unmarked translation | Cut |
| K9 | Forecast before forecasts beat persistence | Cut |
| K10 | Headline rests on a cut claim | Status partial; under 2 supported claims gives insufficient_evidence |

Market by source (Albert's decision, 29 September 2026). A post from a market's own feeds may back a claim about that market, in the brief's place check and in Ask's (K3 and G6). A post counts as market by source when it came from a market-scoped route (that country's trending or local feed, or a hashtag or search sent with that country's region), from a local outlet listed for that market, or from a creator whose profile names that country. A located post (geo_confidence 0.7 or more) still ranks above a market-by-source post. A market-by-source claim is worded as seen in that market's feeds ("seen in Kenya's feeds"), never as what people there are or think ("Kenyans"). A post from another market's feed never backs the claim. Its confidence label sits one step below the label the same claim would get from located evidence. Collect is adding each post's source market per sighting (route and region), so detect and Ask will read the same field.

If over 30% of a market's candidates are held for data reasons, that market carries a banner. The brief is never silently delayed.

Place claims (Albert's decision, 29 September 2026). A claim that names a market, a place in it or its people needs a cited post tied to that market. A located post (geo_confidence 0.7 or more from a known source) ties it best. A post that is "market by source" also counts: it came from a market-scoped route (that country's trending or local feed, or a hashtag or search run with that country's region), from a local outlet listed for that market, or from a creator whose profile names that country. Located evidence ranks above market-by-source evidence. A claim resting on market-by-source posts is worded as seen in that market's feeds ("seen in Kenya's feeds"), never as what the people there are, do or think ("Kenyans"), and its confidence label sits one step lower than the same claim on located evidence. A post from another market's feed never backs a claim about this market. K3 and the support check (K4) apply this rule in the brief and in Ask.

## 3. Verification pipeline

Every Ask answer and every morning explanation. Steps 3 to 5 are code on 100% of claims.

1. Evidence pack from the warehouse. In Stage 1 the writer, critic and checker are plain structured Gemini calls that return strict JSON claims with evidence ids and quotes, and code checks every quote word for word against the stored post text. No model-side citation feature is used.
2. Decompose sentences into atomic claims, only where the reading is unambiguous (Claimify) [11].
3. Resolve ids, handles, URLs, dates, markets, window; verbatim quotes.
4. Numbers: every numeral matched to numbers[]; derived tables are append-only with a run_id and read through views of the latest good run, so every numeral is pinned to its run_id and a hash of its query result and re-run exactly (BigQuery time travel covers only 7 days and not views); post counts recounted.
5. Labels and gaps: code computes the maximum label. Corroborated: unrelated authors on 2 platforms, or 3 unrelated authors plus a metric. Authors are unrelated when no post of one reuses media, caption text, a linked page or a reply relation with a post of the other, and a person posting under several handles counts once. Non-ok sources insert gaps; cited-post flags propagate.
6. Support check per claim, fresh context, claim plus cited text only (FActScore and SAFE style) [12][13]. Until 300 claims are human-labelled, a claim passes only when the checker marks it supported; after that the threshold is set by conformal calibration so accepted claims are wrong at most 3% of the time [14].
7. Critic, plus one test: name the simplest non-cultural explanation (collection change, one creator, a campaign, a scheduled event, bots) and say whether evidence rules it out.
8. One repair round, then cut. One exception (Albert, 4 October 2026): when the critic rules out the simpler explanation, or passes a news or scheduled event on local reaction, and holds the morning explanation only because its why-now is not shown, the writer gets one second draft with the critic's reason. Every check in steps 3 to 7 runs again in full on that draft, with no further repair round; if it fails, cut. A simpler explanation left standing never gets a second draft.
9. Log verdicts to intelligence_42_agent.claim_checks.

Morning trends: section 4, section 5, confirm lane, this pipeline, gate. Explanations hold 3 to 5 claims; "why now" is Inferred unless posts state the cause.

## 4. Detection statistics

Stage 1 subset (28 September): Stage 1A runs gate rules G1, G3, G4b, G5, G5b, G6, G8, G10 (in Stage 1A, before the authenticity networks exist, a political or election item publishes only when Corroborated in an unbiased lane, otherwise it shows as Not assessed) and claim rules K1, K2, K3, K5, K6, K8, K10 in code, with the warm-up states below; the full statistics, authenticity networks and calibrated thresholds switch on in Stage 1B and Stage 2 as BUILD.md says.

Measurement units. Each series is measured in the unit its source supports, recorded in item_counter_daily and item_daily with the protocol that produced it:
- Rank lists (TikTok local feed, hashtag board, YouTube trending, charts, the X trends archive): entry, rank, rank climb and days present. Post counts in a fixed-length list are small and zero-sum, so they are never a volume.
- Counters (hashtag and sound totals, view counts on re-read posts): daily deltas. Sound adoption points from tiktok/song/videos are a page sample by publish day, so they write no series; the job counts them as song_curve_sample_skipped.
- Searches (expansion, confirmation, agent live calls): presence only, never volume, never baseline.
- Panels (hub accounts, sentinel creators, measurement panel): posts per day by a fixed protocol, which is the market-level volume.
Days before a series was tracked are NULL, never zero, so a newly watched item never reads as a surge. A rank list or panel tracks every item from the day the list or panel started, so an item's absence from it is a real zero; a counter is tracked from the item's first read (DATA.md section 3).

Warm-up. A series needs 14 observed days of the same protocol before the statistical test runs. Until then it can be New to 42, Spike, On the boards, Seasonal, Recurring or, after 5 valid days with the floors met, Emerging. Legacy rows and the old feed=global rows are a different regime and never form a baseline; they give first_seen and Recurring only.

Unit: item, market, platform, market-local day, valid days only.

- Measurement: unbiased lanes (boards, trending and hot lists, charts) plus a measurement panel that records platform counters (videos using a sound, hashtag counts) by one fixed protocol daily from first sighting. Feed appearances are a rank list: tested on how often the item appears (DATA.md section 3), never as a volume.
- Expected: mu = 28-day valid-day median, with past surge days down-weighted (Farrington flexible [7]), times a weekday factor per market and platform.
- Test: upper-tail mid-p under a negative binomial; dispersion pooled by market, platform, kind and volume band, since 28 points cannot estimate it per item. Robust z is for display only.
- Cold start (valid days 14 to 27, and any item first seen under 28 days ago on an older list): mu is the 90th percentile of first-week counts for new items of that kind, market and platform.
- Multiple testing: Benjamini-Hochberg at q = 0.05 per market and day [8]; weekly empirical-null check [15], rescaling if central z-scores are wider than theory.
- Floors: 5 distinct unflagged creators and 8 posts in 3 days; no creator above 40%; ratio 2 or more for Rising.
- Persistence: Rising needs 2 significant days, or one plus an independent significant platform or market. Otherwise Spike (unconfirmed).

Backtest before use and monthly: replay with available_at <= as_of; inject synthetic spikes (1.5, 2, 3, 5 times) to measure recall; placebo windows for false alarms; top-10 precision per market against later persistence and ENGINE.md reference lists. Pick q and floors for brief precision of 80% or more with positive median lead time, then freeze rule_version before the next month's data.

## 5. Authenticity

No platform-internal data needed. A meme trend is, by nature, reused audio, template captions and bursts, so those alone never hold an item.

1. Co-action network (Pacheco et al. [16], CooRTweet [17]) built from features other than the item's defining key (for a sound trend, not the sound): the same URL, caption template, hashtag sequence or near-identical text (MinHash Jaccard 0.8, embedding cosine 0.95) by the same pair of accounts within 10 minutes, across 2 or more items or days. Components of 5 or more accounts are candidate networks. Fused similarity types beat single ones [18][19]; on TikTok add reused voiceover and screen text [20].
2. Election-season signals, because bought X trends in Kenya often run for one evening [4]: the same pool of accounts reused across unrelated tags within 30 days, and templated text posted the same evening by many accounts.
3. Share-based flags from DATA.md (near-duplicates, accounts under 30 days old, busiest 10 minutes, top-3 creator share) give Check pattern only, never a hold.
4. creators.coord_score: networks an account joined across items. Machine-text tells are a weak signal only [5].

Status: Clear, Check pattern (one signal), Likely coordinated (a network holding 20% of the item's posts, or two network signals), Not assessed (42 saw too small a share of the item's posts to judge; never shown as Clear). Validate on a labelled library of past ZA, NG and KE campaigns (including the Kenyan hashtag-for-hire cases documented by Mozilla in 2021 [4]) plus reviewer tags; report flag precision monthly. Card wording: "posting pattern consistent with coordination"; no named party is ever accused.

## 6. Human sampling (about 60 minutes a week)

- Claims, 30 min: 30 uniformly random published claims plus 10 highest-risk (numbers, causal, near threshold, non-English); open the post URL; supported, contradicted or unverifiable. Zero errors in 120 random claims over 4 weeks bounds the error rate at about 2.5% (rule of three).
- Trend cards, 15 min: every top-3 card per market plus 11 random; Real, Useful, reason code.
  "Real" rubric: the item is a genuine, growing cultural conversation in that market (not a collection artefact, a single creator, a scheduled event passed off as new, a paid push or a coordinated campaign), the posts behind it say what the card says, and the explanation's facts check out. "Useful" means a strategist would mention it to a client or team this week.
- Reviewers are named in Stage 0 (task 0.9): one each in Johannesburg, Lagos and Nairobi.
- Native speakers, 15 min, rotating weekly (isiZulu and isiXhosa, Sesotho, Setswana, Sepedi and Xitsonga, Afrikaans, Pidgin, Yoruba, Hausa and Igbo, Swahili and Sheng): 15 quotes with glosses and tone labels, via Ogilvy colleagues in Johannesburg, Lagos and Nairobi.
- One tap on cards (Real, Not real, Useful) and "Wrong" on claims with a reason. Taps are self-selected: they show where to look; metrics come from random samples only.
- 10% double-reviewed; Cohen's kappa 0.6 or more.

Monthly recalibration: prediction-powered inference [21] joins the human sample with checker scores on all claims for a valid error-rate interval; the checker threshold is reset; q and floors re-chosen by backtest; authenticity thresholds tuned on tagged cases; the promptfoo grader must match humans on 85% of verdicts or the rubric gains a worked example [22]. A language under 80% native accuracy has tone claims capped at Single source.

## 7. Trust metrics and presentation

| Metric | Source | Target |
|---|---|---|
| Citation integrity | Code, all claims | 100% |
| Number reproducibility | Re-run | 100% |
| Claim support | Random sample with PPI, 4 weeks | 97%, lower bound 95%; Observed 99% |
| Brief precision | Reviewer "Real" on top trends | 80% |
| Rising holds at 7 days | Still rising or peaking, or new platform | 60%; Corroborated above Single source |
| Authenticity misses | Organic trends later judged coordinated or paid | 2% or less |
| Language accuracy | Native checks | 90% per group |
| Honest gaps | Non-ok sources shown | 100%; no false refusals |
| Data health | collection_health | 95% of market-days valid |

Presentation:

- Show the counts behind a state ("31 creators, 3 days, 2.8 times usual") and the expected band on the sparkline. Numeric uncertainty costs little trust; vague verbal hedges cost more [23]. Worth-attention shows as a rank, not a percentile.
- Single source and Inferred claims open with a plain hedge ("42 is not sure yet"); first-person uncertainty reduces over-reliance [24].
- Short explanations: length raises confidence without raising accuracy [25].
- Evidence one tap away, with counts of independent authors and platforms, and coverage chips for what 42 could not see.
- Track record on the card ("74% of last month's Rising calls held a week later"), shown only after four weeks of scored data; the figure here is an illustration.
- Dossier export asks the strategist to open the evidence for each key claim; such prompts cut over-reliance [26].

## 8. Open-source tools

| Tool | Licence | Use |
|---|---|---|
| promptfoo | MIT | CI and weekly score; K1 to K10 as asserts |
| Inspect AI | MIT | Replaying past mornings on frozen snapshots with tool traces |
| RAGAS | Apache-2.0 | Faithfulness second opinion in monthly audit; not a gate |
| DeepEval | Apache-2.0 | Not adopted; overlaps promptfoo |
| MiniCheck (RoBERTa, DeBERTa, Flan-T5) | Apache-2.0 code | CPU audit of the model checker (Gemini) on English claims. Bespoke-MiniCheck-7B is non-commercial: excluded |
| AlignScore; HHEM-2.1-Open | MIT; Apache-2.0 | Same audit role, English only |
| statsmodels, SciPy | BSD-3 | Negative binomial, BH, Wilson intervals |
| ruptures | BSD-2 | Regime breaks |
| datasketch; NetworkX | MIT; BSD-3 | Near-duplicates; coordination components |
| CooRTweet | MIT (R) | Reference check for the SQL port |
| GlotLID | Apache-2.0 | Language ID, 2,000+ labels |
| ppi_py | MIT | Error-rate intervals |
| AfroXLMR-large-76L | MIT | Later: local-language tone |

## 9. References

1. Boeker, Urman, TikTok personalisation, WWW 2022. https://arxiv.org/abs/2201.12271
2. Hammond, Weidmann, GDELT at micro level, 2014. https://journals.sagepub.com/doi/10.1177/2053168014539924
3. Wikimedia DP pageviews. https://analytics.wikimedia.org/published/datasets/country_project_page/00_README.html
4. Madung, Obilo, Mozilla 2021. https://www.mozillafoundation.org/en/blog/fellow-research-inside-the-shadowy-world-of-disinformation-for-hire-in-kenya/
5. Yang, Menczer, AI botnet, 2024. https://arxiv.org/abs/2307.16336
6. ARB Social Media Code. https://www.polity.org.za/article/compliance-with-advertising-standards-south-african-influencers-and-the-arb-2025-01-30
7. Noufaily et al., 2013. https://onlinelibrary.wiley.com/doi/10.1002/sim.5595
8. Benjamini, Hochberg, 1995. https://doi.org/10.1111/j.2517-6161.1995.tb02031.x
9. Panickssery et al., self-preference, 2024. https://arxiv.org/abs/2404.13076
10. IrokoBench, 2025. https://arxiv.org/abs/2406.03368
11. Claimify, ACL 2025. https://arxiv.org/abs/2502.10855
12. FActScore, 2023. https://arxiv.org/abs/2305.14251
13. SAFE, 2024. https://arxiv.org/abs/2403.18802
14. Conformal factuality, ICML 2024. https://arxiv.org/abs/2402.10978
15. Efron, empirical null, 2004. https://doi.org/10.1198/016214504000000089
16. Pacheco et al., ICWSM 2021. https://arxiv.org/abs/2001.05658
17. CooRTweet. https://github.com/nicolarighetti/CooRTweet
18. Luceri et al., WWW 2024. https://arxiv.org/abs/2310.09884
19. Cinus et al., 2025. https://arxiv.org/abs/2410.22716
20. Luceri et al., TikTok, 2025. https://arxiv.org/abs/2505.10867
21. Prediction-powered inference, Science 2023. https://www.science.org/doi/10.1126/science.adi6000
22. Shankar et al., UIST 2024. https://arxiv.org/abs/2404.12272
23. van der Bles et al., PNAS 2020. https://www.pnas.org/doi/10.1073/pnas.1913678117
24. Kim et al., FAccT 2024. https://arxiv.org/abs/2405.00623
25. Steyvers et al., 2025. https://arxiv.org/abs/2401.13835
26. Buçinca et al., CSCW 2021. https://arxiv.org/abs/2102.09692
