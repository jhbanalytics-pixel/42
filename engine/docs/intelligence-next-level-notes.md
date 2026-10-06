# PULSE Intelligence Next Level, working notes

Run date: 2026-07-03
Branch: feat/intel-next-level
Auditor: Albert Meintjes (intel-next-level programme)

North stars: RELEVANCE (act before obvious) and ACCURACY (defend the number with receipts open). Failure modes: FP (false positive), FN (false negative), TR (trust failure).

## Phase A. Math and scoring QA

### A1. Score inventory (every score that reaches a human)

| Score | Formula / weights | Code location | BQ column | Consumer |
|---|---|---|---|---|
| trend_score composite | weighted sum of 10 signals (velocity 0.20, genz 0.17, watchlist 0.12, engagement 0.10, slang 0.10, diversity 0.08, regional 0.08, creator_spread 0.05, search_velocity 0.05, tone 0.05, gcam 0.00) x weight_scale rescale when tone/gcam absent, then x cross_source_multiplier (1.00 to 1.15 by channel families), clamp 1.0, round 4dp | scripts/run_rss_now.py:1036-1072; weights configs/scoring.yaml:27-51 | trend_scores.trend_score | Email card foot "trend score X.XX" (card.py:610,617), status_tag Key/Rising, tier gates |
| component scores (velocity, diversity, engagement, creator, regional, genz, watchlist, search_velocity, slang) | each avg or capped ratio x weight; engagement per-row cap 5000/row-day in composite, plus content-type weights and 50k/day ceiling upstream | run_rss_now.py:952-976, 1036-1046 | trend_scores per-component columns | Composite inputs only, not rendered individually |
| tone_score / gcam_score | tone_avg x 0.05 when tone_rows>0 else weight redistributed to other signals; gcam weight 0.00 (stored, contributes nothing) | run_rss_now.py:991-1026 | tone_score, gcam_score, tone_rows, gcam_rows | tone: composite + seed safety gate. gcam: dead-at-zero |
| momentum | min(vel7d, vel30d) x 0.07 only when MOMENTUM_IN_COMPOSITE=true (off by default). momentum_label computed and displayable regardless of flag | run_rss_now.py:918-985, 1028-1049 | velocity_score_7d/30d, momentum_label | Label can render although it never entered the score |
| cross_source_multiplier | 1 + min(0.05 x (n_channel_families - 1), 0.15); families mapped in _channel_family | run_rss_now.py:1069-1071; scoring.yaml:71-72 | folded into trend_score; channel_diversity stored | Composite multiplier |
| status_tag | Key if trend_score >= 0.45 else Rising, code-derived not model self-report | generate_briefs.py:1226-1239 | status_tag | Email brief pill |
| factual/social corroboration + confidence_tier + recency | factual = clamp01((n_factual x 0.34 + 0.15 entity bonus + 0.15 search-spike bonus) x recency); social = clamp01(n_social x 0.25 x recency); recency = 0.5^(hours/48) or 0.5 neutral; corroborated tier needs n_factual>=1 AND factual>=0.40 | src/scoring/corroboration.py:92-194; scoring.yaml:90-98 | factual_corroboration, social_corroboration, confidence_tier, corroboration_recency_hours | SHADOW. No render consumer anywhere in repo |
| seed_score | audience_fit (genz 0.35, slang 0.25, engagement 0.25, creator 0.15) x format_fit (0.5 + 0.5 x visual_audio_share) x safety_gate (0.6 if tone present and < 0.35), clamp, 4dp | run_rss_now.py:806-899 (_seed_breakdown); scoring.yaml:111-126 | seed_score + seed_audience_fit/format_fit/safety_gate/visual_audio_share | Email SEED chip, hot >= 0.70 (hardcoded dupe of config in card.py:66); fetch_hot_topics gate for C2 (HOT_SEED_SCORE=0.5) |
| C2 seed candidate score | clamp01(0.35 co_occur + 0.25 visual_audio + 0.25 distinctiveness + 0.15 genz_slang); eligibility gates: cap remaining, config/rejection-memory dupe 28d, safety check, freq >=5 weekly / >=3 fast, novelty 7d/14d; fast lane top-decile floor | src/analysis/seed_candidates.py:292-436, 711-730 | seed_candidates.score, seed_fit, status, safety_flags, rationale | NO production render found. BQ table only (discovery loop) |
| reconcile / event_ledger | actions: corroborated / stale (rewrites claim_after when >=2 factual families AND confidence >=0.55) / labelled / no_match; gated RECONCILE_ENABLED, renders nothing | src/analysis/reconcile.py:49-59, 282-297, 430-509; run_rss_now.py:1886-1904 | reconcile_actions, event_ledger | Operator CLI only (reconcile_shadow_digest.py stdout). Nothing rendered |

Math smells (A1 side-findings, all file:line verified):

1. All 9 hardcoded fallback weights in run_rss_now.py:1036-1046 are stale (26-May-old values, every one diverges from live scoring.yaml). Dormant until a partial yaml load, then silent mis-scoring with no error.
2. _channel_family default for unknown source string is "news" (run_rss_now.py:284-295), a FACTUAL family. Unmapped connector = phantom factual corroboration + inflated cross_source_multiplier.
3. Seed chip hot threshold duplicated: card.py:66 hardcodes 0.70, scoring.yaml:126 holds 0.70. No shared source; config edit does not reach render.
4. reconcile.py reimplements corroboration with its own constants (2 factual families, 0.55 floor) parallel to corroboration.py (0.34 weight, 0.40 floor). Two unlinked "how much is enough" laws; drift risk.
5. weight_scale redistribution means scoring.yaml weights are NOT the actual per-row weights for any topic lacking tone/gcam rows. Effective weights only recoverable via tone_rows/gcam_rows.
6. Display rounds stored 4dp value again to 2dp (card.py:617). Cosmetic.

### A2. Recompute audit (>=15 topic-days, 5 per market, 7-day window)

Recompute of ALL 230 topic-days in the last 7 days (exceeds the >=15 target), dataset trends_v2_dev via BQ REST (bq CLI and python client both hit transport timeouts from this box; REST with a gcloud token works). Correction to the initial map: component columns store the RAW 0..1 signal, weights apply at recompute time. Formula verified: sum(component x weight) x weight_scale + tone x 0.05 + gcam x 0.00, then x cross_source_multiplier, clamp 1.0, round 4dp.

Result: 100.0% match. 0/230 flagged at |delta| > 0.01, max |delta| 0.0001 (float rounding). MOMENTUM_IN_COMPOSITE confirmed OFF in live data (0.20 velocity weight reproduces all 230 rows; the 0.13 carve fails 13).

| Market | Topic | Date | Stored | Recomputed | Delta | Flag >0.01 |
|---|---|---|---|---|---|---|
| ke | fintech_mpesa | 2026-07-03 | 0.5177 | 0.5177 | 0.0000 | no |
| ke | genz_sheng | 2026-06-30 | 0.4758 | 0.4758 | 0.0000 | no |
| ng | film_nollywood | 2026-07-03 | 0.5012 | 0.5012 | 0.0000 | no |
| ng | economy_sapa_hustle | 2026-06-30 | 0.3751 | 0.3751 | 0.0000 | no |
| za | finance_stokvel | 2026-06-30 | 0.3281 | 0.3280 | -0.0001 | no |
| za | food_rituals_braai | 2026-06-29 | 0.2941 | 0.2942 | +0.0001 | no |

(+224 more rows, all clean; full pull in agent transcript.)

Reproducibility gap (TR): all recompute inputs are persisted per row, but the 11 weights and cross-source constants live only in scoring.yaml with no stored weights_version or config_hash, so a weight retune silently breaks reproducibility of every historical row. Pairs with the 9 stale fallback weights (A1 smell 1).

Anomalies: 11 rows carry a precise score off >=5 zero components (formula-correct but thin); tone_rows=1 still rides the full 0.05 tone weight (no min-row gate); gcam_score stored non-zero on 171/230 rows at weight 0.00 (dead column); 29 rows NULL corroboration shadow columns.

Accuracy watchdog run/mirror result: the script is read-only (no SMTP, no writes, no Vertex) but the python BQ client cannot connect from this box (EXIT=124 timeout), so its checks were mirrored via REST over the same window: composite_integrity = the 100% recompute above; no_social_only_verified 0 violations; corroboration_recompute 0 violations (no corroborated row under the 0.40 factual floor); 0 scores outside [0,1]. confidence_tier distribution: corroborated 190, social-only 8, single-source-factual 3, NULL 29. Note 190/230 "corroborated" (83%) is itself suspicious given the _channel_family news-default leak (A4): the tier may be systematically over-awarded.

### A3. Weight sensitivity

Dominant weight: velocity 0.20 (velocity family incl inert momentum 0.07 = 0.20), then genz_score 0.17, watchlist 0.12. Top two signals carry 0.37 of the composite; the bottom four (diversity, regional, creator_spread, search_velocity) carry 0.26 combined. The engine is front-loaded on velocity + Gen-Z language.

±20% shock findings (analytic; a signal value sits in 0..1 and the component = signal x weight): a ±20% move on velocity (0.20 -> 0.24 or 0.16) shifts the composite by at most delta_w x signal = 0.04 x signal, so <=0.04 at signal=1.0 and typically 0.01 to 0.02 at observed signal levels. Live distribution context (scoring.yaml calibration note): max trend_score ever observed 0.589, medians ZA 0.24 / NG 0.32 / KE 0.30, tier floors 0.45 / 0.30 / 0.18. Consequence: a 20% velocity shock cannot manufacture a Trending/Key topic (nothing has ever crossed 0.45) but CAN flip borderline topics across the 0.30 Emerging line, so tier membership near p75 is weight-fragile. genz at 0.17 has nearly identical leverage. The scoring is not knife-edge on any single weight, but the Emerging boundary is thin enough that the ±20% band straddles it for mid-pack topics. Refine with live per-row signals once the BQ recompute agent returns.

Live-at-zero weights still displayed: gcam_score weight 0.00, computed and stored every run, contributes nothing (scoring.yaml:51). momentum_label displayed regardless of the MOMENTUM_IN_COMPOSITE flag, so a "rising/cooling" label can render for a topic where momentum never entered the score. Corroboration + seed C2 columns are shadow (computed, not displayed) so not in scope here but flagged in A1.

### A4. Corroboration honesty

Can social-only reach corroborated in live data? Proof:

Verdict: NO by construction, with one leak path. Code proof: FACTUAL_FAMILIES = {news, search, youtube, music} (corroboration.py:32); factual_raw = n_factual x 0.34 and both bonuses apply only if n_factual > 0 (corroboration.py:149-150), so n_factual = 0 forces factual_corroboration = 0; the corroborated tier requires n_factual >= 1 AND factual >= 0.40 (corroboration.py:179-194); n_factual = 0 with n_social > 0 lands on "social-only". No path lets social terms feed factual.

Leak path (TR risk): _channel_family defaults unknown source strings to "news" (run_rss_now.py:295). A misregistered connector source manufactures a phantom factual family, letting a genuinely social-only topic acquire "corroborated". Guarantee holds only while the source map is complete.

### A5. Seed vs trend split

High-seed low-trend and inverse cases (7-day window, top gaps):

High seed / low trend: za food_rituals_braai (seed 0.44 vs trend 0.27, recurring), ke fashion_mitumba (0.32 vs 0.18), ke/ng tech_gemini_ai (0.39 vs 0.28). High trend / low seed: za sports_rugby (trend 0.45 vs seed 0.15, gap 0.30), ng economy_sapa_hustle (0.40 vs 0.11), za infra_power_eskom (0.33 vs 0.04). seed_score populated 230/230.

Does UI explain the split? No. The email renders the SEED chip and the trend score with no explanation that they measure different things (audience/format fit vs live momentum). A strategist seeing braai with a hot SEED chip and a cold trend score has no way to read why. The split itself is healthy (by design, scoring.yaml:100-110); the rendering is unexplained.

### GATE A verdict

PASS with reservations. The composite math is honest: 100% reproducible from stored data, watchdog laws hold, social-only cannot reach corroborated by construction. The reservations are trust-shaped, not arithmetic: (1) _channel_family unknown-source default of "news" can manufacture factual corroboration, and 83% corroborated-rate suggests over-award; (2) no weights_version stored, so historical reproducibility depends on config never changing; (3) 9 stale fallback weights are a dormant silent-mis-score; (4) a quarter of the C2 seed score (distinctiveness) is never persisted anywhere; (5) displayed labels can outrun the math (momentum_label without momentum in composite). Proceed to Phase B.

## Phase B. Seeds autopsy

### B1. 14-day seed_candidates pull

23 rows total, and only 3 days of coverage (1-3 Jul; the table is that new). Markets: KE 15, NG 8, ZA 0. Lane: fast 23, weekly 0. Type: keyword 23. Status: pending 23, nothing ever approved or rejected, so the human review loop has consumed zero candidates. safety_flags empty and rationale NULL on every row. Dataset trends_v2_dev (no prod dataset exists yet).

### B2. Grading (all 23; fewer than 30 exist)

Full table in the B1-B4 agent transcript; grade distribution is the story:

| Grade | Count | Share |
|---|---|---|
| USEFUL | 0 | 0% |
| OBVIOUS | 1 | 4% |
| NOISE | 20 | 87% |
| UNCLEAR | 2 | 9% |
| HARMFUL | 0 | 0% |

Precision@10: KE 0/10, NG 0/8, ZA no candidates. Overall 0%. The period's TOP scorer is "customer" (0.593, M-Pesa seller ad copy). Representative NOISE: instantly, anthem, quality, flow, fast, available, below, enjoy, save, right, life, coming, next, ever, days, plus a lowercased YouTube channel ID (ucgnt5exzvbnvu7f6insehyg) and seller-spam tags (deliverycountrywide). The 2 UNCLEAR: beauty.mitumba.ke (single-shop handle), gengetone_dynasty (plausibly real account, thin evidence).

### B3. Failure tags

| Tag | Count | Examples |
|---|---|---|
| formula (va saturation and/or distinctiveness fallback) | 23/23 | va = 1.0 on 22/23; dist backs out to exactly 1.0 on "customer" |
| eligibility (English stoplist absent) | 18 | save, right, life, customer, instantly... |
| platform skew (handle/ID/tag artifacts) | 4 | ucgnt5..., beauty.mitumba.ke, deliverycountrywide, travelvlogger |
| UI honesty (rationale NULL, no why) | 23 | every row |
| thin evidence (evidence_topics empty) | 2 | channel-ID token, travelvlogger |
| taxonomy dupe | 2 | music, gengetone_dynasty |
| co_occur dead (structural) | 23 | co_occur = 0.00 on every row; the heaviest weight (0.35) contributes nothing engine-wide |
| safety / geo leak / cap starvation | 0 | none observed; ZA producing zero is an eligibility question, not starvation |

### B4. Five worst, end-to-end trace through compute_c2_score

Distinctiveness is not stored, backed out as dist = (score - 0.35co - 0.25va - 0.15(genz+slang)/2) / 0.25.

1. ucgnt5exzvbnvu7f6insehyg, KE, 0.5333: co 0, va 1.0 (+0.25), genz_slang 0.5 via INHERITED slang 1.0 (+0.075), dist 0.833 (+0.208). A raw YouTube channel ID with zero evidence topics clears the floor without the term itself scoring anything.
2. ever, NG, 0.4631: va alone is 54% of the score. At honest va 0.4 it sits at 0.31 and dies.
3. customer, KE, 0.593 (period top): dist exactly 1.0 = the freq/10 no-baseline fallback maxed. The two formula bugs contribute 0.50 of 0.593.
4. below, KE, 0.5335: "link below" caption boilerplate; va + inherited slang + dist 0.909.
5. travelvlogger, KE, 0.5224: empty evidence topics, inherited slang 1.0, generic global tag.

Common shape: co_occur = 0 everywhere, so every pass rides 0.25va + 0.25dist + 0.15gs against a 0.45 floor with va pinned at 1.0. Junk passes by construction. (The fix on branch fix/seed-candidate-quality addresses stoplist, inherited slang, and the fallback cap; co_occur deadness and tokenizer artifacts remain open, ranked in C4.)

### B5. FN probe

Ground truth: 12 verifiable SSA Gen-Z cultural moves, 19 Jun - 3 Jul (receipts in agent transcript). Verdict: SURFACED 6, INGESTED-NOT-RANKED 6, NEVER-INGESTED 0. Miss rate 50%, and every miss is post-ingestion: the pipes deliver, the ranking/synthesis loses it.

| Move | Rows ingested | Verdict |
|---|---|---|
| KE 25 Jun protest anniversary | 1,195 maandamano | SURFACED |
| Missing Gen-Z protesters found tortured (~27-30 Jun) | 683 + 107 | INGESTED-NOT-RANKED |
| Bafana first-ever WC knockout | 2,434 | SURFACED |
| Canada 1-0 SA exit | 1,381 | SURFACED |
| Tyla "Is It Love" single | 85 | SURFACED |
| Tyla cast in Toy Story 5 | 14 | INGESTED-NOT-RANKED |
| Springboks 80-31 Barbarians | 2,103 | SURFACED |
| Senegal 5-0 record (Super Eagles shame angle) | 517 | INGESTED-NOT-RANKED |
| NG petrol landing cost crash / Dangote undercut | 701 | SURFACED (narrative only, Dangote never named) |
| Mopepe amapiano dance challenge | 17 | INGESTED-NOT-RANKED |
| Wizkid/Davido/Rema/Ayra MOBO noms | 158 | INGESTED-NOT-RANKED |
| Saba Saba 7 Jul buildup | 13 | INGESTED-NOT-RANKED |

Which gate killed each miss:

1. Torture follow-up (worst miss, a story a strategist NEEDED): Phase-2 sample selection inside the already-briefed maandamano topic; anniversary framing crowded out the atrocity follow-up; many rows carried no topic label (gdelt_other/news). Corroboration recency cut the topic score exactly when the story broke (0.265 -> 0.131 on 27-28 Jun).
2. Toy Story 5, Mopepe, MOBO: taxonomy resolution. Items dissolve into broad music_* buckets; no hook for film casting, a named dance challenge, or award noms; 14-17 rows sits under distinct-treatment thresholds.
3. Senegal record: brief item selection favours search-velocity volume (fixture lookups) over the cultural angle sitting in 517 rows.
4. Saba Saba: genuinely thin pre-event (13 rows), defensible; will matter 7 Jul.

Structural: ZA has NO sports_football query_group; all Bafana World Cup coverage rode inside sports_rugby and surfaced only because rugby briefs absorbed it. Bites the day rugby and football compete for the same brief slot.

### B6. LP staging read (READ ONLY)

Does UI show breakdown or only terms?

Read-path map (all file:line cites verified by explorer agent, LP repo read-only): GET /api/seed-discover -> seed_discover.build_discover_payload -> bq.fetch_seed_candidates_week -> seed_candidates table -> discover.jsx CandidateCard with FitBar. Trace in Explorer -> /seedpath/:term -> fetch_seed_graph_adjacency (seed_graph + v_seed_first_seen), pure BQ, zero AI. Research in Console -> /console/:query -> research.build_research_evidence: 7 parallel BQ fetches + Brand24 topics/demographics; only the Synthesis stage calls Vertex Gemini (synth.py:231, Vertex-only, WPP-clean).

Findings:

1. Bars are honest raw values. _seed_fit_dict (bq.py:3311-3317) passes stored seed_fit through unrescaled; FitBar only clamps and formats. Good.
2. TR gap: distinctiveness (0.25 of the score) is never stored in seed_fit and never selected, so a quarter of every displayed score is unaccountable from the UI AND from the stored row (seed_candidates.py:320-325 omits it). The four bars shown (co_occur, visual_audio, genz, slang) cover only 0.75 of the weight; genz and slang render as two bars though only their average (0.15) scores.
3. UI honesty gap: safety_flags fetched from BQ then silently dropped in _shape_candidate (seed_discover.py:57-77). A safety_auto_reject row shows status "rejected" with no reason. rationale forwards, flags do not.
4. No client-side re-ranking or floors; one LIMIT 200 display cap (harmless at current caps).
5. Dataset via BQ_DATASET env (default trends_v2_dev), deploy-time not runtime; surfaced in Discover footer and health endpoint. Staging currently reads dev.

### GATE B verdict

FAIL as shipped, root causes fully pinned. Seed precision is 0% over the table's entire life; every candidate that passed did so on two non-discriminative components (va saturation + distinctiveness fallback) against a floor they mechanically clear; the one component designed to anchor cultural relevance (co_occur, 0.35 weight) is dead engine-wide; the UI cannot show why any score is high because a quarter of the score is never persisted and rationale is never written. Albert-authorized fix for the formula/eligibility half is committed on fix/seed-candidate-quality (gate PASS, 43 tests green, unpushed). Open after the fix: co_occur deadness, tokenizer artifacts (channel IDs, handles), ZA emitting nothing, weekly lane emitting nothing, rationale never populated. Proceed to Phase C.

## Phase C. FP / FN / TR ranked

### C1. FP receipts

1. Seeds: 20/23 NOISE, 0/23 USEFUL, precision@10 = 0% (B2). Top scorer of the table's life is "customer", M-Pesa ad copy at 0.593. Receipt class: formula, not bad luck; every pass rides two non-discriminative components.
2. Corroboration tier: 83% of topic-days "corroborated" (190/230). A tier that fires on 5 of 6 rows discriminates nothing, and the news-default leak (A4) means part of it may be unearned. It is shadow today, so the FP is latent, but it becomes a live FP machine the day the label renders.
3. Cross-source multiplier history rhymes: the old flat 1.15x fired on ~95% of topic-days before the channel-family regrade (scoring.yaml:57-70). Same failure shape now visible in the corroborated tier and in va = 1.0 on 22/23 seeds: a "signal" that is always on.
4. Brief-level "would I act": the ng sports_football brief in the window is search-fixture-lookup driven while the actual cultural angle (Senegal record shaming the absent Super Eagles) sat unused in 517 ingested rows (B5). Renders as insight, reads as fixture list.

### C2. FN miss rate (ingested-not-ranked vs never-ingested)

50% miss rate on a 12-move verified ground-truth set (B5). Split: 6 ingested-not-ranked, 0 never-ingested. The ingestion layer is NOT the bottleneck; ranking, taxonomy resolution, and Phase-2 sample selection are. Worst single miss: five missing Gen-Z protesters found tortured, 790 rows ingested, zero brief mentions, while the engine briefed the anniversary framing of the same topic. Second class: sub-topic culture (Toy Story casting, Mopepe challenge, MOBO noms) dissolving into broad music_* buckets. Third: ZA has no sports_football topic at all.

### C3. TR findings (stale/future claims, reconcile action mix, fake precision, null-shaped numbers)

1. Unaccountable quarter: distinctiveness (0.25 of C2 score) never persisted, not in seed_fit, not in any column (B6). Score cannot be defended with BQ open.
2. Phantom factual family: _channel_family defaults unknown sources to "news" (run_rss_now.py:295), silently inflating cross_source_multiplier and factual corroboration. Combined with 83% corroborated-rate: the tier is likely over-awarded.
3. No weights_version/config_hash stored: a retune orphans all historical scores (A2). Plus 9 stale fallback weights waiting for a partial yaml load (A1).
4. Labels outrun math: momentum_label renders regardless of MOMENTUM_IN_COMPOSITE; SEED chip hot threshold duplicated in code vs config; genz + slang render as two bars though only their average scores.
5. Silent honesty drops in LP: safety_flags fetched then dropped; rationale NULL on 23/23 rows; rejected candidates show no reason (B6).
6. Recency law can suppress exactly when it matters: corroboration recency halved maandamano corroboration (0.265 -> 0.131) on the days the torture follow-up broke (B5). Freshness-weighting punished a developing story.
7. Reconcile action mix: READ 3 Jul (first daily read ever, now a morning-check sub-check). The shadow runs but matches nothing: 13 ledger events, 281 reconcile actions, 0 corroborated, 0 stale, 6 labelled, 99% no_match; KE 0 events; event text is raw GDELT theme dumps (AGRICULTURE,941 ENV_FORESTRY,978...) with state mostly unknown. The ledger's entity anchoring is not resolving real events, so the runbook promotion window cannot accumulate evidence. Core promotion is blocked on ledger quality, not just floor recalibration. New finding, feeds D1.
8. Two unlinked corroboration laws (corroboration.py 0.34/0.40 vs reconcile.py 2-family/0.55): whichever renders first will contradict the other on some topic eventually.

### C4. Ranked list (frequency x damage x fixability 2w/8w)

| Rank | Class | Finding | Frequency | Damage | Fixability | First fix class |
|---|---|---|---|---|---|---|
| 1 | FP | Seed formula + eligibility lets junk pass (0% precision) | every fast-lane day | flagship discovery surface shows garbage to the team | 2w (fix committed, unpushed) | formula/gate |
| 2 | FN | Phase-2 sample selection + taxonomy resolution loses ingested culture (50% miss) | weekly, systemic | strategists miss the exact stories they pay for | 8w (taxonomy hooks + selection rework) | ranking/synthesis |
| 3 | TR | Corroboration over-award: news-default leak + 83% corroborated + recency suppressing developing stories | every run | Core promotion would render inflated trust labels | 2w (default "other" not "news"; floor recalibration; observe) | one-line default + recalibration |
| 4 | TR | Score accountability: distinctiveness unpersisted, no weights_version, stale fallbacks | every row | cannot defend numbers with receipts open | 2w (persist components + config hash; sync fallbacks) | persistence |
| 5 | FP | co_occur dead (0.35 weight contributes nothing engine-wide) | every seed | the one relevance anchor in C2 never fires; HOT_SEED_SCORE=0.5 gate + empty topic_groups on graph rows | 8w (needs diagnosis: hot-topic threshold vs graph labeling) | gate calibration |
| 6 | TR | UI honesty: rationale never written, safety_flags dropped, labels without math (momentum) | every card | reviewers approve/reject blind; trust erodes quietly | 2w (LP + writer patches) | render honesty |
| 7 | FN | Taxonomy blind spots: no ZA sports_football; sub-topic culture dissolves into buckets | structural | recurring silent misses on the biggest cultural stories | 8w (taxonomy expansion discipline exists; needs seed loop feeding it, which is the point of C2) | taxonomy |
| 8 | TR | Reconcile shadow health unverified; two corroboration laws unlinked | unknown | unknown until read | 2w (run digest daily, unify constants) | observation |

### C5. One-pager: what you are not seeing

WHAT YOU ARE NOT SEEING, 3 Jul 2026

The math you CAN trust: every one of the 230 trend scores of the last week recomputes to 4 decimal places from stored data. Nothing is fabricated. Social-only topics cannot buy the corroborated tier. The pipes work: of 12 real cultural moves this fortnight, all 12 landed rows in your warehouse.

What you are not seeing, in order of pain:

1. The engine read the rows about five missing Gen-Z protesters found tortured, 790 of them, and briefed the protest anniversary instead. The selection layer, not the data layer, decides what you see, and it favours the already-loud. Same fortnight: Tyla's Toy Story casting, the Mopepe dance challenge, and the MOBO sweep all sat ingested and unbriefed because the taxonomy has no slot small enough to hold them.

2. Your discovery surface has never proposed one useful seed. 23 candidates, 0 useful, top scorer "customer" from M-Pesa ad copy. Two always-on components carry anything frequent over the floor, and the one component built to anchor relevance has been zero on every candidate ever scored. The fix for the junk half is written and gated green; the dead-anchor half needs a decision.

3. The trust labels you are about to turn on are inflated. 83% of topic-days already qualify as "corroborated" in shadow, partly because any unrecognized source string silently counts as news, a factual family. Promote the Core today and it will decorate briefs with confidence the math has not earned. One line of code plus a floor recalibration fixes the leak before it ever renders.

4. A quarter of every seed score is unaccountable: the distinctiveness component is computed, used, and thrown away, never stored, never displayed. And no score row records which weights produced it, so the day you retune, history goes dark. If a Google x Ogilvy room asks "show me why this number", today you cannot, for that quarter.

5. The engine cannot see WhatsApp or Facebook, where ~73% of your markets' adults actually talk. No pipe exists at any price that fixes this cleanly. The honest product says so on the tin instead of implying total coverage.

Highest-leverage fix: repoint the selection layer at what the warehouse already holds. Ingestion is done and paid for; every miss this fortnight was a ranking, taxonomy, or sampling decision. Fix selection (2 in the list above is committed, 1 and 3 are days of work each) and the same data you already collect becomes the product you wanted.

GATE C: STOPPED. Phase D awaits go.

### GATE C. STOP FOR ALBERT

## Phase D. Core + next level (after Albert go)

Full D-phase deliverable in docs/intelligence-next-level-direction.md. Summaries:

### D1. Core readiness matrix

Verdict: NOT ready to promote. Corroboration computes and persists every run but would over-award (83% corroborated-rate + news-default leak); reconcile is live shadow with health unverified (digest never read daily); grounding verifier is a dark stub (grounding_verifier.py returns empty receipts); relevance.py and claim_receipts do not exist. Both promotion blockers are cheap: family-map default fix + corroborated-floor recalibration.

### D2. Relevance law (ASSERT / RANK / PROPOSE)

ASSERT needs a resolvable factual receipt with a date; social-only renders as social chatter, never as fact. RANK is open to anything ingested, but trust labels need corroboration tier above social-only. PROPOSE needs cultural signal (co_occur, slang provenance, or a genz floor), never volume alone. No blended trust score anywhere.

### D3. Accuracy daily checks

Five checks: composite recompute (exists); corroboration law extended with a corroborated-rate band alert (>60% = FAIL) and an unknown-source-family counter; daily reconcile digest reads with true-to-false inversion resetting the promotion window; seed precision@10 per market via the LP grading loop (target >=30% within 30 days); standing FN watchlist with alert under 60% surfaced.

### D4. Open possibilities (8-12), max 3 do-next

Thirteen possibilities across data spine (Google Trends RSS, Wikipedia pageviews, Bluesky Jetstream, SocialCrawl graduation), trust spine (config_hash, persist distinctiveness + rationale, corroboration hardening, claim_receipts + grounding build), discovery loop (co_occur revival, bigram/entity extraction, sub-topic micro-brief slot), analyst loop (LP approve/reject writeback), product shape ("what you are not seeing" panel). Each with failure mode, cost class, dependency, kill criteria in the direction doc. Do-next three: corroboration hardening, co_occur revival, micro-brief slot.

Data-spine candidates researched 3 Jul (deep-research fan-out + self-verified primary docs; deep-research verifier rate-limited so the ranking below is my own verification, not the workflow's). Framing: the engine is corroboration-heavy and lead-time-poor. Most existing pipes confirm what is already loud. The prize is pipes that give lead-time (search intent, attention, music discovery) and a truly-open social family that costs no vendor units.

Tier 1, verified live, free, no auth, clean license, build first:

1. Google Trends trending RSS. https://trends.google.com/trending/rss?geo=ZA (NG, KE by geo swap). VERIFIED live 3 Jul: returned real ZA trending searches dated today with ht:approx_traffic and ht:news_item links. Near-real-time search intent, fresher than the BigQuery public google_trends tables the engine reads now. Factual family (search). Effort tiny: same RSS shape as the existing RSS connector. Unique value: leading search-intent signal plus built-in news corroboration links in one payload. This is the strongest single add.

2. Wikipedia Pageviews API, per-country top. https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html endpoint "List most-viewed pages for a country". VERIFIED endpoint exists, data CC BY-SA 4.0, no auth, REST JSON, daily granularity, aggregate counts so no PII. Per-country daily top-viewed articles for ZA/NG/KE is a pure curiosity/attention signal, a factual family, mostly corroboration with occasional lead on a breaking name. Effort tiny.

Tier 2, verified concept, more effort or thinner SSA volume:

3. Bluesky Jetstream. Public JSON WebSocket over the atproto firehose, collection-filterable (app.bsky.feed.post), no auth, no vendor cost. The only genuinely open social firehose. New social family. SSA/Gen-Z volume is currently thin, so treat as breadth not depth. Effort medium (a streaming consumer, not a cron GET). Compliance: honor user deletes on stored posts.

4. Mastodon trends. GET /api/v1/trends/{tags,statuses,links}, no auth, per-instance. Zero SSA concentration, weak for this region. Park.

5. Shazam / Soundcharts music discovery. Music-discovery lead-time ahead of the Apple Music RSS charts the engine already reads. Soundcharts free tier is 1000 calls, eval only not production. Music family is already covered, so marginal gain. Lower priority.

ToS / compliance cautions (do NOT auto-ingest):

- TikTok Creative Center: free official trend surface but explicitly not designed for automated scanning, so automated pull is ToS-gray. TikTok is already covered via EnsembleData. Manual reference only.
- Telegram public channels: large NG/KE Gen-Z presence but carries PII, moderation, and ToS load. High effort, compliance-heavy. Park.
- Meta Ad Library API: only political/social-issue ads are programmatically queryable, targeting data is gated to approved researchers. Not a general trend signal. Skip.

Known structural blind spot (FN, not a buildable pipe): WhatsApp and Facebook dominate SSA (Pew 2024: WhatsApp ~73% median adult use across these markets) but expose no public content API. The engine cannot see the region's biggest channels. Name this honestly rather than imply full coverage.

Recommended do-next (max 3): (1) Google Trends trending RSS, (2) Wikipedia per-country pageviews, (3) Bluesky Jetstream as the free social-breadth pilot. First two are days of work and add real lead-time; the third is the free hedge against paid social vendor lock-in.

### D5. 30-day programme + non-goals

W1 measure/honesty (merge PR #228, config_hash, persist seed components, corroboration default fix, daily digest reads, FN watchlist). W2 one gate fix (co_occur revival + regrade). W3 core promotion or deepen shadow (recalibrated floor, runbook window with receipts). W4 surface truth in LP (handoff to visual programme). Non-goals: no new paid vendors, no WhatsApp workaround, no LP visual redesign here, no blended score, no test re-runs of the full pipeline.

### D6. Direction doc written: docs/intelligence-next-level-direction.md

Done, 3 Jul 2026.

### GATE D. STOP FOR ALBERT

## Phase E. Handoff

### E1. Visual redesign handoff
### E2. Ordered fix prompt (only if Albert names one)
### E3. Deliverables checklist + max 5 open decisions

## Addendum, 4 Jul 2026: outside-the-box pipes, verified shortlist

Deep-research rerun's verifier stage died on session limits twice; winners re-verified manually against primary sources. Live probes 3-4 Jul: Apple app-charts RSS 200 on za/ng/ke (KE #1 free app MyPower, HustleSasa events; NG OPay/Moniepoint/MovieBox); Google suggest per-gl works unauthenticated ("how to vote on big brother" NG/KE); Cloudflare Radar needs a free account token (anon 400, endpoint real).

Ranked: 1 Apple App Store top charts RSS (build now, official, free, no auth); 2 Cloudflare Radar (free token, CC BY 4.0, country trending domains + anomaly/shutdown detection); 3 Audiomack API (official, OAuth1, NG streaming culture pre-chart); 4 SoundCloud client-credentials (registration friction to check); 5 EskomSePush (paid tier only, license note needed before client-facing use); 6 Genius API (slang lexicon enrichment); 7 Google suggest (ToS-gray, enrichment only); 8 events platforms (parked). Killed: Urban Dictionary (ToS), 42matters/SerpApi (paid resellers), Wiktionary, Steam, Patreon.

WPP grant note (Albert, 3 Jul): special full access for this project as a future case study; ambition and budget posture raised. Does not override third-party ToS, GDPR/POPIA, or the Vertex-only AI rule absent written scope.

## Addendum 2, 4 Jul 2026: v3 next-level upgrade shortlist + seed cohort graded

Seed grading: all 23 pre-fix pending candidates rejected via review CLI with autopsy reasons (status_by albert, 23/23 ok). First labeled precision cohort: 0/23 useful, the baseline the fixed formula is measured against. Rejection memory now blocks the junk class for 28 days.

Deep-research sweep (verifier stage died on session limits a fourth time; claims are primary-source, marked unverified): ranked v3 upgrades. 1 burst-detection layer (STL residual + Kleinberg onset per topic/term series) to replace threshold-only triggering and time-stamp trend onset; 2 lead-time eval harness (soft-match precision/recall vs the standing watchlist + detected-vs-mainstream lag metric, Microsoft ISE pattern), makes the "days early" case-study claim a number; 3 unsupervised phrase mining (UCPhrase class) to replace unigram extraction, the structural fix for the junk-token class; 4 Wikipedia new-article + edit-velocity emerging-entity signal (pageviews pipe already live); 5 slang lexicon induction from our own M4 embeddings; 6 confidence UX with lead-time badges + receipt drawers (visual programme handoff); 7 STUMPY matrix-profile streaming, optional after 1. Killed: Twitter AnomalyDetection (archived, R-only), academic-only clustering. 90-day order 2 then 1 then 3; those three are also highest leverage-per-effort.
