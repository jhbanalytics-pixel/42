# Methods dossier: using 42's history scientifically (Gamma 3.1)

Prepared 16 October 2026 for the Gamma 3.1 team. Scope: which established methods 42 can use to learn about cultural change from its own history, what each needs, what it costs at 42's scale, how it fails, and how to test it chronologically without fooling ourselves.

Conventions:
- **[n]** points to the numbered source list in section (e). Every source there was opened or shown in a search result during this research. Where a figure came from a secondary source, the source list says so.
- **[Inferred]** marks my own reasoning or a judgement about 42 that no cited source states.
- **ESTABLISHED** means a published method with replication or wide use and a maintained implementation. **EXPERIMENTAL** means the method exists, but using it on data like 42's (sampled, multi-lane, weeks deep, three African markets) has no track record.
- Verdicts: **Adopt now** (cheap, low risk, mostly descriptive), **Test in Gamma** (build behind a flag and score it with the protocol in section (d) before anyone sees it), **Later research** (needs data 42 does not yet have), **Reject** (wrong for 42, with the reason).

Grounding in 42 (from docs/full-42/DATA.md sections 3 to 6, ENGINE.md, TRUST.md, core/detect/backtest.py, core/detect/early_signal.py):
- Series are defined per item, source, market and protocol. A protocol change starts a new series. Tests are a beta-binomial on rank-list presence and a negative binomial on panel and counter counts, with Benjamini-Hochberg per market and day and a weekly Efron empirical-null check. There is a negative binomial CUSUM early signal. Waves are islands of sightings split by 28 quiet days. States run Seasonal > Recurring > Rising > Emerging > Spike > Fading.
- Clustering is BERTopic over 3 days with stored embeddings, matched to a persistent cultural_map by centroid, keyword, hashtag and creator votes.
- Forecasts (7 and 14 days) cover reach_rising, cross_market and persist_50. They are scored against persistence, with a 10% random control sample.
- The backtest replays each day from rows available before that day's cutoff, injects spikes of 1.5 to 5 times, and switches a test on only with at least 14 observed days, at least 60 placebo series-days, a false-alarm rate of at most 0.05 and recall of at least 0.8 at 3 times.
- Data depth: SocialCrawl since about late September 2026, so about 3 weeks on 16 October. Legacy tables (June to September 2026) are a different regime and are memory only. The Kenya feed is about 12% in-country.

**The binding constraint is history length, not compute.** At 42's scale (thousands of posts and hundreds to low thousands of items per market per day), every method in this dossier except multivariate Hawkes fitting and deep models runs in seconds to minutes on one machine [Inferred, from the complexity figures cited per method]. Three weeks of one protocol supports almost nothing that needs outcome labels at a 14 to 28 day horizon, any seasonality beyond day of week, or any analogue library. A large part of the honest answer is therefore "freeze the label definitions now and let the data accrue."

---

## (a) Summary table

Columns: question, method, status, data needed, cost at 42's scale, baseline to beat, main failure modes, verdict for 42.

| Q | Method | Status | Data needed | Cost at 42 scale | Baseline to beat | Main failure modes | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | Wave shape descriptors: peak-day share F (Crane and Sornette [2]); before/at/after-peak fractions (Lehmann et al. [3]); post-peak retention ratio; days present; creator persistence | ESTABLISHED (descriptors) | One completed wave plus about 7 to 14 days after its peak | Trivial (SQL) | none: these are the baselines | Peak falls inside a collection outage. A truncated wave looks like a spike. Rank-list presence saturates at 3 pulls | **Adopt now**, as descriptive card fields and as the baseline set for every Q1 model |
| 1 | Changepoint segmentation: PELT [5] via `ruptures` [6]; Bayesian online changepoint detection (BOCPD) [4] | ESTABLISHED | about 10+ points per segment. Poisson or NB cost for counts | O(n) per series (PELT, under its conditions) or O(n x run-length) for BOCPD. Seconds for all series | 42's rule of 28 quiet days, plus the NB CUSUM already in early_signal.py | Penalty choice drives the result. Count series with many zeros. Protocol breaks look like changepoints | **Test in Gamma** for wave boundaries. **Adopt now** BOCPD or PELT on collection-health series (lane totals, yield, overlap) to catch silent vendor changes |
| 1 | Kleinberg burst automaton [1] | ESTABLISHED | Event stream or daily counts | Dynamic programming, cheap | 42's NB test + BH + CUSUM | Largely duplicates 42's test. The scaling parameter s and cost gamma are arbitrary | **Later research** (not worth a second burst test) |
| 1 | Endogenous vs exogenous classification by decay exponent [2]; Hawkes / HIP [8]; SEISMIC [10] | ESTABLISHED for complete streams; EXPERIMENTAL for sampled ones | Complete or known-rate event timestamps; [10] needs reshare times plus follower degrees; [2] used about 5M videos | Univariate Hawkes per item takes seconds; multivariate takes minutes or hours (`tick` [7]) | Peak-day share F plus retention ratio | Missing events bias the branching ratio [11]. 42 sees 3 pulls a day and a panel sample, so the "endogenous" share is unidentifiable. Exogenous drivers dominate hashtags [3] | **Reject** as a published label. **Later research** for INAR(1) on daily bins [9] as a diagnostic only |
| 2 | Early growth to later size (Szabo and Huberman log-linear [13]); NB CUSUM (already built) | ESTABLISHED | Early counts plus a resolved outcome. Per [13], about 2 hours for Digg and about 10 days for YouTube | Trivial | Persistence ("Rising now stays Rising"); base rate | Regression to the mean after selection on a high day. Platform-specific shelf life [13] | **Adopt now**, as a logistic baseline next to persistence in forecasts |
| 2 | Nonparametric trajectory voting (Nikolov and Shah [14]) | EXPERIMENTAL on 42 | Labelled reference trajectories ([14] used 200 trend plus 200 non-trend topics) | kNN, cheap | Persistence; log-linear [13] | Few labels; labels defined by a platform list that 42 also uses as a seed (circularity) | **Test in Gamma**, folded into the Q6 analogue engine |
| 2 | Structural or community-breadth early features (Weng, Menczer and Ahn [16]; Cheng et al. [15]: breadth beats depth early) | ESTABLISHED with a follower graph; EXPERIMENTAL with proxies | [16]: 10% Twitter sample, 121.8M tweets, reciprocal follower graph of 595k users. 42 has no follower graph | Leiden on a creator co-occurrence graph: seconds [26] | Early creator count; log-linear [13] | The proxy communities are not social communities. Panel lanes fix the creators by design | **Test in Gamma** as "early community breadth" from unbiased lanes only |
| 2 | Deep cascade predictors (graph neural networks, recurrent networks over cascades) | Published but data-hungry | Full cascades plus a social graph | GPU; large | log-linear [13] | 42 has neither cascades nor a graph. A predictability ceiling below R^2 = 0.5 even with full data [17] | **Reject** |
| 3 | Observed first-seen ordering across platform, market and tier, corrected for detection delay per lane | ESTABLISHED (descriptive) | Lane cadence and coverage metadata | Trivial | none | Order of detection ≠ order of occurrence. A lane with faster cadence appears to "lead" [Inferred] | **Adopt now**, worded as "first seen by 42 in…" |
| 3 | Hashtag, sound and creator co-occurrence network plus Leiden [26] | ESTABLISHED | One week or more of co-occurrence | Seconds | Number of distinct hashtags or creators | Generic hashtags create hubs (42 already has a stoplist). Resolution parameter | **Test in Gamma**, feeding Q7 breadth and Q4 lineage |
| 3 | NetInf [21] / multivariate Hawkes across communities [24] / external-influence model [22] | ESTABLISHED on complete or near-complete data | Many cascades with node-level infection times; [21] used about 170M blog and news items over a year; [24] 160M images over 13 months | Minutes to hours | Observed first-seen ordering | Sampling bias, unequal coverage per platform, and confounding by an external common cause ([22] attributes about 29% of Twitter URL volume to external sources) | **Later research**, on (platform x market) nodes once 6+ months of a stable protocol exist |
| 3 | Granger causality [27] / transfer entropy [28,29] / PCMCI [30] between lanes or markets | ESTABLISHED as predictive tests; never causal under hidden common drivers | Long, stationary, equally sampled series | Cheap | Cross-correlation at lag 0 | Common exogenous drivers, unequal sampling cadence, changing coverage. Granger himself attributes apparent instantaneous causality to slow recording or omitted variables [27] | **Reject** for causal or "influence" claims. A lead-lag diagnostic is allowed internally only |
| 4 | MONIC-style transition taxonomy (survive, split, merge, disappear, emerge) [33]; Jaccard community matching [35] | ESTABLISHED | Cluster memberships per day (already stored) | Trivial | 42's current matcher (match or new) | Threshold sensitivity. HDBSCAN instability between days shows up as false split and merge events | **Adopt now** (log transitions), **Test in Gamma** before showing split and merge on cards |
| 4 | Identity drift: centroid and keyword drift from the birth state | ESTABLISHED idea [32,36]; EXPERIMENTAL thresholds | Daily centroids (stored) | Trivial | none | A change in the embedding model invalidates all drift (needs versioning) | **Adopt now** as an internal metric; relabel when drift exceeds a frozen threshold |
| 4 | BERTopic topics_over_time [32] | ESTABLISHED | One global fit over the period | Minutes | Per-day c-TF-IDF keywords already stored | A global refit renumbers topics (which is why 42 avoids merge_models). It is descriptive only | **Test in Gamma**, only as a "how the wording changed" view inside one lineage |
| 4 | Dynamic topic model (Blei and Lafferty [31]); evolutionary clustering [34] | ESTABLISHED (DTM on long corpora: Science 1880 to 2000) | Many time slices, fixed K, bag of words | Moderate to high | 42's matcher | Short, code-switched multilingual posts; fixed K; slow | **Reject** DTM. Evolutionary clustering is **Later research** |
| 5 | Day-of-week factors (already in stats.py); seasonal-naive benchmark [38] | ESTABLISHED | 3+ weeks for day of week | Trivial | Flat mean | Three cycles is a very noisy estimate. Protocol breaks alias with weekly effects | **Adopt now** (keep); report its uncertainty |
| 5 | STL [37] / Prophet holidays [39] | ESTABLISHED | STL: at least 2 full periods (weekly needs 2+ weeks; yearly needs 2+ years). Prophet is designed for long daily histories | Cheap | Seasonal naive | Inapplicable to annual seasonality for at least a year. Overfits on weeks of data | **Later research** (annual); weekly STL only on lane totals |
| 5 | Moments calendar plus external long-history priors (e.g. Google Trends, which reaches back to 2004 [70]) | ESTABLISHED source, EXPERIMENTAL use | Curated calendar; 5 years of search interest per term and market | Cheap (free source; access fragile [Inferred]) | Moments calendar alone | Search interest ≠ social posting. Google Trends values are sampled and rescaled per query [70] | **Adopt now** (calendar); **Test in Gamma** (Google Trends annual prior for Seasonal) |
| 6 | Analogue / kNN retrieval of past trajectories with an outcome distribution over k analogues (Lorenz [40]; analog ensemble [41]) | ESTABLISHED in meteorology; EXPERIMENTAL here | Library of resolved past trajectories from the same market and protocol era | Vector search, milliseconds | Persistence; base rate | Too few or poor analogues (Lorenz's own warning [40]); analogues from a different collection regime | **Test in Gamma**, the main new history feature |
| 6 | DTW 1-NN [45] / matrix profile [42,43] / shapelets [44] | ESTABLISHED for time series classification | Longer series (matrix profile and shapelets need subsequences well beyond 7 to 14 points) | Cheap at n ≤ 60 | Euclidean on log, z-normalized series | Warping destroys the timing information that lead time needs [Inferred]. Short series | **Test in Gamma** constrained DTW only as a distance option; matrix profile and shapelets are **Later research** |
| 7 | Participation metrics: distinct creators; new vs returning; creator tier mix; Hill numbers q = 0, 1, 2 [46]; coverage-standardised diversity [47] | ESTABLISHED (ecology) | Creator IDs per post in unbiased lanes | Trivial | Raw post volume; distinct creators | Richness grows with sample size, so compare at equal coverage [47]. Panels fix the creators | **Adopt now** (descriptive). **Test in Gamma** whether broadening predicts persistence |
| 8 | Protocol registry, effort k, share-of-lane denominators, placebo and negative-control items, positive controls, interrupted time series at breaks [52,53] | ESTABLISHED | Already mostly built | Trivial | Raw counts | Unknown vendor changes (the Google Flu Trends lesson [49]); locality misattribution | **Adopt now**, extending 42's existing guards |
| 9 | Rolling-origin backtest, purge and embargo, label freeze, Brier and Brier skill vs persistence, reliability diagrams with consistency bars, Platt scaling, adaptive conformal inference (ACI) or EnbPI intervals, risk-coverage abstention, per-market reporting | ESTABLISHED | Resolved forecasts (hundreds for calibration) | Trivial | Persistence, base rate, seasonal naive | Too few resolved events per market; tuning on the test period; isotonic overfit on small n [61] | **Adopt now** (protocol); calibration maps wait until resolved n is about 300+ per target [Inferred] |
| 10 | Industry systems as design references (Google Trends, X Trends, Exploding Topics, Glimpse, WGSN, GDELT, Media Cloud) | Mixed | n/a | n/a | n/a | Accuracy claims with no stated metric or backtest | Use Google Trends, the X trends archive and Media Cloud as **external reference clocks** for evaluation; treat vendor accuracy claims as marketing |

---

## (b) Per-question sections

### Q1. Lasting patterns vs event spikes

> Note added at review, 16 October 2026: the label wording in this appendix (lasting_14, lasting_28) is research input. The frozen Gamma definitions are in `core/eval/gamma/labels_v1.yaml` and GAMMA-3.1-PLAN.md section 3, which take precedence.

**What the literature establishes**
- **Shape classes from the peak.** Crane and Sornette [2] used daily views of nearly 5 million YouTube videos.
  - About 90% are well described as Poisson noise. About 10% (about 500,000) burst.
  - The bursting videos fall into three classes by the share F of total views on the peak day: exogenous subcritical F of 80 to 100%, exogenous critical F of 20 to 80%, endogenous critical F of 0 to 20%.
  - The post-peak decay exponents are about 1.4, 0.6 and 0.2, with θ = 0.4 ± 0.1. (Figures from the ar5iv full text.)
- **Activity fractions around the peak.** Lehmann et al. [3] used 131.7M tweets and 402 popular hashtags (115 peaks). They summarised each peak by the fractions of activity before, during and after the peak in a two-week window, and found four classes chosen by BIC: anticipatory, reactive, symmetric, and on the peak day only. Their conclusion is that epidemic spreading plays a minor role and that popularity is mostly exogenous. Peaks were detected at 10 times the median baseline in a 61-day window.
- **Fast decay is the norm.** Asur et al. [12] report that most Twitter trending topics fade within roughly 20 to 40 minutes, and that trends mostly amplify traditional media content.
- **Self-exciting models.** Hawkes models separate background (exogenous) intensity from self-excitation:
  - HIP [8] reduced YouTube popularity forecast error by 28.6% over history-only methods.
  - SEISMIC [10] needs reshare timestamps and follower degrees.
  - With missing or noisy event times, Hawkes parameters are "severely" biased [11].
  - Kirchner [9] shows that bin counts of a Hawkes process are approximately INAR(p), estimable by conditional least squares. This is the only Hawkes route that matches 42's daily bins.
- **Changepoints.** PELT [5] gives exact penalised segmentation at a cost linear in n under mild conditions. BOCPD [4] gives an online posterior over run length. `ruptures` [6] (BSD-licensed, about 36M downloads per PyPI) implements PELT and others.
- **Bursts.** Kleinberg [1] models a stream with an infinite-state automaton and finds nested bursts by dynamic programming. The R package `bursts` implements it.

**Simplest honest baseline (adopt now).** For each wave (v_item_waves), compute:
- **days_present_28:** observed days with sightings in the 28 days from wave start, using measured lanes only.
- **retention_7 / retention_14:** mean daily count in days peak+7 to peak+13 (and +14 to +20), divided by the peak-week mean.
- **F, peak-day share:** peak-day posts over wave posts. This is the Crane and Sornette statistic, but on 42's sampled counts it is descriptive, not a class [Inferred].
- **f_before, f_peak, f_after:** the Lehmann fractions in a ±7-day window.
- **creator persistence:** the share of week-1 creators seen again in week 2, and the number of distinct creators in the week after the peak vs the week of the peak.

These are cheap and interpretable. They should be the features every fancier Q1 model must beat.

**Assumptions and data.** A full post-peak window means at least 14 days after the peak. As of 16 October, waves that peaked before about 2 October qualify. Those all straddle the SocialCrawl start, so the first fair evaluation set will exist around mid-November 2026 [Inferred].

**Failure modes specific to 42 [Inferred]**
- A collection outage on the peak day mislabels the peak. Use only days that are valid in collection_health and mark waves that contain invalid days.
- Rank-list presence saturates at 3 pulls, so F and retention on beta-binomial series are coarse. Prefer creator counts from unbiased lanes.
- Board lanes are exogenous by construction: the platform's own algorithm decides inclusion. An item that leaves a board decays by the board's rules, not by culture.

**Lasting label (to freeze now):** `lasting_28` = present at or above the floor (5 creators or 8 posts in 3 days, the same as DATA.md) on at least 7 of days 15 to 28 after first measured detection, within one protocol. Use `lasting_14` as an earlier proxy. The thresholds are examples [Inferred]; the point is to fix them before looking at results.

**Changepoints for 42**
- (i) Run BOCPD or PELT on collection-health series per lane and market (total posts, distinct items, feed overlap). An undisclosed vendor change appears as a changepoint in many items at once. A changepoint across the whole lane is a measurement event, not culture. This is the cheapest high-value guard. **Adopt now.**
- (ii) Use PELT with a Poisson or NB cost on item series to split waves, instead of the fixed 28-quiet-days rule. **Test in Gamma.** The penalty is chosen on the training period only and scored by whether segments predict lasting_14 better than the island rule.

**Hawkes verdict.** Do not publish "endogenous" or "self-sustaining" labels.
- [3] found hashtag dynamics mostly exogenous even with the follower graph.
- 42 lacks complete streams, so the branching ratio is not identifiable [11].
- INAR(1) on daily bins [9] may be tested as one more descriptive feature (an "autocorrelation of counts"), named as such.

**Testing chronologically.** Classify each wave using only days up to its peak plus h days, with h = 3 or 7. Score against lasting_28 resolved later. Compare against days_present_so_far and retention-so-far by Brier score and AUC, per market. Abstain when the wave contains an invalid collection day, when the item has fewer than 5 observed days, or when the peak is within 2 days of a protocol change.

### Q2. Earlier signals and lead time

**Literature**
- **Nikolov and Shah [14]** classified Twitter topics nonparametrically, by voting against reference trajectories. The training set was 200 trending and 200 non-trending topics. MIT News reports 95% accuracy, a 4% false-positive rate, and detection about 1.5 hours before Twitter's list on average (sometimes 4 to 5 hours). The authors noted the small training sets.
- **Cheng et al. [15]** (Facebook photo reshares) found strong performance at predicting whether a cascade will continue to grow. Relative growth becomes more predictable as more reshares are observed. Temporal and structural features are key, and early breadth beats depth as an indicator of size. I could not confirm specific accuracy figures from the abstract and do not quote any.
- **Weng, Menczer and Ahn [16]** used a 10% Twitter sample: 121.8M tweets, 10.4M hashtags, and a reciprocal-follow graph of 595,460 users, with Infomap communities and a random forest on the first 50 tweets.
  - At the 90th-percentile adopter threshold, precision was about 7 times random and more than 3 times a community-blind model.
  - Recall was more than 350% better than random.
  - Memes that spread across many communities early go viral.
- **Szabo and Huberman [13]** show long-term popularity is predictable from early counts: about 2 hours of data for Digg (30 days ahead) and about 10 days for YouTube. This is a strong, simple baseline.
- **Limits.**
  - Martin et al. [17] find the best Twitter cascade-size models explain less than half the variance, and argue that realistic limits are near that level.
  - Bakshy et al. [18] (1.6M users, 74M diffusion events) find that predicting which user or URL will cause a large cascade is "relatively unreliable".
  - Goel et al. [19] (about 1 billion Twitter diffusion events) find popularity is mostly driven by the largest single broadcast, with low structural virality.
- **Platform trend lists reward velocity.** Twitter's list was widely reported to reward velocity over volume and to penalise steady growth (Lotan's analysis of #OccupyWallStreet [72]). X's current help page says trends are personalised and that post count is "just one of the factors" [71].

**Implications for 42 [Inferred]**
1. Rising is a velocity statistic against the item's own baseline. That matches what platforms do and suffers the same blind spot for slow, steady growth. 42's NB CUSUM (early_signal.py, tuned to a 1.5 times shift) is the right complement. Keep it, and evaluate it by lead time, not only by false-alarm rate.
2. **Base rates.** Any "Rising" alert must be scored against two things:
   - the base rate of the target among all candidates in the same market and day (the 10% random control sample already supports this);
   - persistence.
   Report lift = precision / base rate. Selecting on a high day guarantees some regression to the mean, so "stays Rising" is easier than it looks for high-count items and harder for small ones. Stratify by count decile.
3. **Lead time needs a reference clock that 42 does not use as evidence.** Otherwise it is circular. Candidates:
   - (a) first appearance in the X trends archive. It is already seed-only and "never evidence", so it is a clean external clock for ZA, NG and KE.
   - (b) entry onto a platform's own board (TikTok hashtag board, YouTube trending). Usable only if the board entry is not one of the alert's inputs. Otherwise lead time is zero by construction.
   - (c) the first day of a Google Trends spike for the term in the market.
   - (d) the first mainstream news story (Media Cloud [77] indexes African news sources; free research access [Inferred]).
   Lead time = reference date − first alert date. Report the distribution (median, interquartile range), the share of reference events 42 alerted on before the reference date (recall at lead ≥ 1 day), and the share of alerts never followed by a reference event (false discovery).
4. **Fixed alert budget.** Strategists see about 10 items per market per day. Report precision@k (k = 5, 10) and lead time among the top k per market per day, not AUC over everything. The existing top-10 precision in backtest.py is the right shape. Extend it with the lead-time columns.
5. **Structural signals without a follower graph.** Build a bipartite creator to hashtag/sound graph over the trailing 14 days from unbiased lanes. Project it to creators and run Leiden [26]; communities are "creator neighbourhoods", not social communities. For each item's first 3 days, compute the number of distinct neighbourhoods its creators belong to, and the normalised entropy over them. That is the Weng-style breadth [16] and the Cheng-style early breadth [15]. **Test in Gamma.** Exclude panel lanes, where 42 chose the creators.

**Testing.** Use rolling origin. On each day t, features come only from rows with available_at before t's cutoff (backtest.py already does this). Targets are resolved at t+7 and t+14. Purge and embargo as in section (d). Compare, per market:
- persistence;
- log-linear early growth [13];
- the current Rising rule;
- CUSUM;
- breadth-augmented logistic.

Report Brier skill vs persistence, precision@10, median lead time vs the X-trends clock, and a 95% block-bootstrap interval over days.

**Abstain** when:
- the item's market has fewer than 14 valid days on the protocol (already the switch rule);
- the item has fewer than 3 observed days;
- or more than 40% of the item's posts come from one lane that changed protocol in the last 7 days [Inferred threshold, freeze before testing].

### Q3. Diffusion across platforms, markets, creator tiers and communities

**Literature**
- **NetInf [21]** infers a hidden diffusion network from infection times. It is NP-hard, with a near-optimal approximation, and was applied to about 170M blog and news items over a year. Implementation: SNAP.
- **Myers, Zhu and Leskovec [22]** estimate that only about 71% of Twitter URL volume is attributable to network diffusion and about 29% to external sources.
- **Zannettou et al. [23,24]** measured cross-community influence (Twitter, Reddit, 4chan, Gab) with one Hawkes model per meme cluster on 160M images from 2.6B posts over 13 months. A fitted weight of 1.2 from Twitter to Reddit means each Twitter event produces about 1.2 expected Reddit events.
- **MemeTracker [25]** (about 1.6M media sites and blogs over 3 months) reported a typical 2.5-hour lag between news and blog peaks for phrases.
- **Leiden [26]** guarantees connected communities. Louvain gave up to 25% badly connected and 16% disconnected communities.
- **Romero, Meeder and Kleinberg [20]** show that hashtags on different topics spread by different mechanics (political hashtags behave like complex contagion).
- **Granger [27]** defines causality as incremental predictability and itself warns that apparent instantaneous causality arises from slow recording or omitted variables.
  - Transfer entropy [28] is the nonparametric analogue.
  - Ver Steeg and Galstyan [29] apply it to Twitter users and call it "causal", but it is still predictive information transfer.
  - PCMCI [30] improves false-positive control under autocorrelation. It still assumes causal sufficiency (no hidden common causes) [Inferred from the method class; not stated in the snippet].

**Why lead-lag between 42's lanes is not causal [Inferred, supported by [22,27,48,51]]**
1. **Unequal detection delay.** TikTok trending is pulled 3 times a day, panels once with since=, boards weekly or daily. An item that appears on all lanes simultaneously will be first seen on the most frequently pulled lane. "TikTok leads Facebook" can be a cadence artefact.
2. **Common exogenous drivers.** A match, an election result or a celebrity post drives all platforms. [22]'s 29% external share and [3]'s mostly-exogenous finding mean that a common cause is the default.
3. **Changing coverage.** If the X panel grows during a test window, X "starts leading".
4. **Locality.** About 12% of the Kenya feed is in-country, so "KE before NG" may be "non-Kenyan accounts in the KE feed before NG".

**What to adopt now**
- **Platform and market path as observed first-seen order.** Each step carries the lane's cadence and the detection-delay bound (e.g., "first seen by 42 on TikTok ZA, 3 pulls a day; on Facebook hubs ZA 2 days later, daily panel"). Never say "originated on" or "spread from".
- Report the share of an item's creators who are locality-confirmed (geo_confidence ≥ 0.7) for each market step.
- **Creator tier path:** the first day seen among nano, micro, mid and macro creators. A "grassroots-first" vs "macro-first" label is descriptive and testable (Q7).

**Test in Gamma.** Co-occurrence network plus Leiden for community breadth, as in Q2 and Q7.

**Later research (6+ months of stable protocols)**
- NetInf or multivariate Hawkes over a small node set: (platform × market), about 24 nodes, with items as cascades.
- Validate on a negative control first. Shuffle item labels within day: any inferred edges that survive are artefacts of cadence and coverage.
- Validate on a positive control next. Inject synthetic cascades with known edges into the real sampling process: subsample synthetic posts at each lane's observed capture rate and check recovery.

**Reject.** Granger, transfer entropy or PCMCI used to claim influence between markets or platforms, in any user-facing copy.

### Q4. Trend evolution: splitting, merging, relationships, identity drift

**Literature**
- **The dynamic topic model [31]** chains topic parameters through a state-space model: Kalman-filter variational inference on the Science archive, 1880 to 2000.
- **BERTopic's topics-over-time [32]** fits one global model and then recomputes c-TF-IDF per time slice. The topic keeps its identity while its wording shifts.
- **Evolutionary clustering [34]** adds a temporal-smoothness term that trades fidelity to today's data against change from yesterday's clustering.
- **MONIC [33]** classifies external transitions (survive, split, merge, disappear, emerge) and internal ones (location, cohesion). R implementation: `clusTransition`.
- **Greene, Doyle and Cunningham [35]** match communities across snapshots with a Jaccard threshold and many-to-many mapping, and declare death after d consecutive absences.
- **Hamilton et al. [36]** measure semantic change with diachronic embeddings over two centuries and four languages. Change slows with frequency (a power law) and speeds with polysemy.

**For 42.** 42 already does the core thing right: it keeps stable IDs by matching rather than merge_models. Three gaps:
1. **Transition log (adopt now).** Each night, for each cultural_map entry, record the MONIC transition type from member overlap and centroid similarity:
   - A survives in B if B holds at least τ of A's matched members and is A's best match.
   - A splits when two or more of today's clusters each take at least τ_s of A's members.
   - B merges A1 and A2 when B absorbs at least τ_s of each.
   - A disappears after d days unmatched.
   Use the same vote rules as the matcher. τ around 0.5 and d = 3 are examples [Inferred]; freeze them before evaluation.
2. **HDBSCAN instability [Inferred].** Day-to-day reclustering of a 3-day window produces false splits and merges. Measure the false-transition rate on a negative control: refit the same day twice with different random_state values, or on bootstrap resamples of the posts. Any "split" that appears between two fits of identical data is noise. Show split and merge on cards only for transitions that persist 2 or more days and exceed the bootstrap noise rate.
3. **Identity drift (adopt now, internal).** For each lineage, track:
   - cosine(centroid_today, centroid_at_birth);
   - Jaccard(keywords_today, keywords_at_birth);
   - the share of today's members from creators unseen at birth.
   When drift crosses a frozen threshold, trigger relabelling (42 already relabels drifted clusters) and record a version, so a strategist sees "this has shifted from X to Y since 3 Oct". Embedding-model changes are protocol breaks for drift: version them.

**BERTopic topics_over_time.** Use only inside one lineage, to show how its wording changed. Never refit globally to obtain IDs.

**Reject DTM** for 42:
- fixed K;
- bag of words on short, code-switched posts in Pidgin, Sheng and Tsotsitaal, which language ID mislabels (DATA.md section 4);
- time-slice granularity;
- cost.

Evolutionary clustering is a principled future replacement for "cluster then match" (later research). It needs a custom HDBSCAN/UMAP variant, and nothing standard exists [Inferred].

**Testing.** A human-labelled audit of about 50 transitions a week, inside the existing 60 minutes of human sampling a week (TRUST.md section 6), gives precision per transition type. A split is useful if both children are independently judged coherent and distinct. Report the false-transition rate from the re-fit control alongside it.

### Q5. Recurrence and seasonality with weeks of data

**Literature**
- STL [37] decomposes a series into seasonal, trend and remainder with loess. Implementations: R `stl`; statsmodels STL, which fixes a bug in the original Fortran robust weights.
- Prophet [39] adds holiday and calendar components to a trend.
- The seasonal-naive method is a standard benchmark (Hyndman and Athanasopoulos, FPP3 [38], cited via sktime's NaiveForecaster).
- Google Trends offers a sample of searches back to 2004, normalised by total searches in place and time and rescaled 0 to 100 per query [70].

**What is possible now [Inferred]**
- **Weekly:** with about 3 weeks of one protocol, a day-of-week factor rests on 3 observations per weekday. stats.py's weekday_factor is right to exist but must carry an interval and shrink toward 1. Estimate it on lane totals (thousands of posts), not per item.
- **Annual:** impossible from SocialCrawl until late 2027. The legacy tables (June to September 2026) give memory of wave dates but not comparable magnitudes, because the regime is different. 42 already treats them as memory only. Keep that.
- **Recurrence** within a year: v_item_waves plus cultural_map lineage already define Recurring. The risk is false recurrence from collection gaps: two waves split by a 28-day outage are one wave.
  - Require collection_health validity over the quiet gap. A gap of invalid days is "unknown", not "quiet".
- **Calendar priors (adopt now).** A curated moments calendar per market: public holidays, school and exam calendars, sport fixtures, award shows, payday cycles, Black Friday, December festive season. Examples of fixed dates: ZA Heritage Day 24 September and Youth Day 16 June; NG Independence Day 1 October; KE Mashujaa Day 20 October and Jamhuri Day 12 December.
  - Upcoming dates are positive controls (section (d)). If Mashujaa Day does not surface as Seasonal in KE around 20 October 2026, something is broken.
- **External annual prior (test in Gamma).** For items that are terms (not sounds or creators):
  - Pull 5 years of Google Trends weekly interest for the market.
  - Mark as "historically seasonal" when the same ±1 ISO week peaks at or above 2 times the term's annual median in at least 2 of the last 5 years.
  - Use this only to route the item to the Seasonal state (not Rising), never as evidence of current growth.
  - Failure modes: search interest ≠ posting; sampled values vary between pulls [70]. Access is through unofficial clients and is rate-limited [Inferred]. Cache results.

**Later research.** STL on lane totals once 8+ weeks of one protocol exist; annual seasonality in late 2027.

**Testing.**
- Positive controls: calendar moments must be detected as Seasonal (recall).
- Negative controls: non-moment days must not produce Seasonal for terms with no seasonal history.
- A Seasonal label is correct if the item falls back to baseline within 14 days after the moment (the definition of a calendar spike), so it is resolvable within weeks.

### Q6. Historical analogue retrieval

**Literature**
- **Lorenz [40]** introduced naturally occurring analogues for predictability, and warned that good analogues are rarely found within the recorded history.
- **The analog ensemble [41]** builds a probabilistic forecast from the best-matching past forecasts. It was tested on 0 to 48 hour wind and temperature at 550 stations, April to July 2011. The authors report high statistical consistency and reliability, with skill equal or superior to logistic-regression forecasts.
- **Time series classification.** 1-NN with DTW remains a benchmark that many newer methods fail to beat in the bake-off over 85 UCR datasets [45]. COTE beat it by about 8% on average.
- **Matrix profile [42]** (implementation: STUMPY [43], BSD-3, tested on 100M+ points) finds motifs and discords within long series.
- **Shapelets [44]** are discriminative subsequences for classification.

**Design for 42 (test in Gamma)**
1. **Unit:** an item-market trajectory aligned at first measured detection (day 0). Feature vector over days 0 to 6:
   - log1p daily posts;
   - distinct creators;
   - lane mix;
   - number of platforms;
   - creator-tier shares;
   - item kind (sound, hashtag, phrase, etc.).
   Optionally concatenate the cluster centroid embedding for a "similar meaning" mode.
2. **Library:** resolved past trajectories with known outcomes at +7, +14 and +28 days (reach_rising, persist_50, lasting_28, cross_market). Strict rules:
   - the same market;
   - the same protocol era for the main lane;
   - only trajectories whose outcome had resolved before today's cutoff. This is the leakage rule: an analogue that is still unresolved on day t cannot be used on day t.
3. **Retrieval:** k = 20 nearest by Euclidean distance on standardised features. Offer constrained DTW (Sakoe-Chiba window ≤ 1 day) as an alternative distance. Unconstrained DTW would let a slow riser match a fast one, erasing the timing that lead time depends on [Inferred].
4. **Display, honestly.** Show the outcome distribution, never a single prediction. For example: "Of the 20 most similar past items in NG, 6 reached Rising within 14 days, 3 were still present at 28 days; median days present 9 (IQR 5 to 14)." Add:
   - a reliability statement from the backtest ("when 42 shows 30% here, about N% happened");
   - an analogue-quality bar: mean distance to the k analogues relative to the library's typical nearest-neighbour distance;
   - two or three named analogues as examples a strategist can open.
5. **Abstain** when:
   - the library has fewer than about 200 resolved trajectories in that market and era [Inferred threshold];
   - the k-th neighbour distance exceeds the 90th percentile of the library's leave-one-out k-th distances (no good analogue: Lorenz's problem);
   - or the analogues come mostly from a different protocol era.
6. **Baselines:** the unconditional base rate in that market (the "climatology" of the analog-ensemble literature) and persistence. The analogue distribution must beat both by Brier skill score over rolling origins before it is shown.

Matrix profile and shapelets need longer series than 7 to 28 daily points. They are **later research**; revisit for lane totals and for long-lived lineages.

**Cost.** Library size is thousands of trajectories per market per month. Exact kNN is milliseconds. BigQuery VECTOR_SEARCH, which 42 already uses, works too.

### Q7. Participation and behaviour change, not volume

**Literature**
- Jost [46] argues that entropy is not diversity. Convert Shannon entropy to an effective number with exp(H): Hill number q = 1. q = 0 is richness and q = 2 is inverse Simpson.
- Chao and Jost [47] show that comparing diversity at equal sample size misleads. They standardise by sample coverage (completeness) through rarefaction and extrapolation. Implementation: iNEXT (Chao et al. 2014; Hsieh, Ma and Chao 2016, per the iNEXT vignette).
- Weng et al. [16] and Cheng et al. [15] tie early breadth to later spread.

**Adopt now (descriptive, per item or cluster and market, weekly, unbiased lanes only; panels separately and labelled)**
- **Effective creators:** Hill numbers q = 0, 1, 2 over posts per creator, compared at a common coverage (e.g. 0.8) or rarefied to a common number of posts. Without this, a week with more collection looks "more diverse".
- **New vs returning creators:** the share of this week's creators seen with this item or lineage for the first time. A sustained high share of new creators means adoption is still spreading. A falling new share with stable volume means the same people are talking (a niche consolidating) [Inferred interpretation; to be validated].
- **Tier mix:** shares of nano, micro, mid and macro creators. Tier thresholds come from follower counts at the time of the post, which are versioned. Grassroots-first vs macro-first, and the direction of the shift (macro creators joining = mainstreaming).
- **Community breadth:** the number and entropy of creator neighbourhoods (Leiden, Q2), markets and platforms.
- **Top-3 creator share:** already in 42 as a concentration guard. Keep it.

**Why this is closer to "culture" [Inferred].** Volume moves with collection effort, vendor ranking and a single viral post (Goel et al.: popularity mostly tracks the largest broadcast [19]). Who participates (how many distinct people, how many new people, from how many communities) is less sensitive to one large broadcast, and harder for a single account or coordinated group to fake. That is consistent with 42's existing floors (5 creators, no creator above 40%).

**Test in Gamma.** Does the change in effective creators (q = 1) or the new-creator share over days 0 to 6 add Brier skill over volume-only features for lasting_28 and cross_market? Report per market.

**Failure modes**
- Panel lanes, where creators are fixed by design.
- Creator ID instability across platforms: one person, several handles.
- Follower counts read at different times.
- Kenya locality: a "new creator" may simply be a non-Kenyan account in the KE feed. Stratify by geo-confirmed.

### Q8. Measurement validity under biased, changing collection

**Literature**
- **Ruths and Pfeffer [48]** (Science 2014):
  - platform population biases;
  - opaque and changing samples from data providers;
  - methods reported on easy subsets. Political orientation inference reaches about 65% accuracy for typical users vs about 90% claimed for politically active users.
  - Remedies exist in epidemiology and statistics.
- **Lazer et al. [49]:** Google Flu Trends over-predicted flu. The secondary report says 100 of 108 weeks (August 2011 to September 2013), including more than 50% overestimation in the 2011-12 and 2012-13 seasons. The causes were "big data hubris" and "algorithm dynamics": changes to Google's own search product altered the input.
- **Morstatter et al. [50]:** the Streaming API returned about 43.5% of Firehose volume (528,592 vs 1,280,344 tweets), with coverage falling when volume spiked.
  - Top-hashtag rankings were unreliable at small n (sometimes negatively correlated with the Firehose) and better at large n.
  - LDA topics were significantly worse than random samples of equal size on 4 of 5 days.
  - The sample performed worse than random sampling.
- **Tufekci [51]:** the model-organism problem (over-reliance on one platform), hashtag sampling bias, the denominator problem, platform ecology, and field effects.
- **Interrupted time series [52]:** segmented regression with a pre-specified impact model, handling autocorrelation, over-dispersion, seasonality and time-varying confounders. A 2020 corrigendum fixes the regression equation; use the corrected form.
- **Negative controls [53]:** exposure or outcome controls that should show no effect reveal confounding and bias.

**What 42 already does well.** Protocol-versioned series; effort factor k with validity [0.5, 2]; a collection_health reference of the median of prior valid days; placebo expansions (about 5%) that set the spread base rate; the Efron empirical-null check; search lanes kept out of baselines; legacy kept out of tests.

**Add now [Inferred design, grounded in [49,51,52,53]]**
1. **Denominators.** Store and show the share of a lane's day: item posts / lane posts, and item creators / lane creators. For search lanes, also posts per planned call. "Up 3 times" with a lane that is itself up 2 times is "up 1.5 times in share". Make share the default for Today copy when a lane total moved more than 25% vs its reference [threshold Inferred].
2. **Negative-control items.** Per market, a frozen list of staple items whose cultural share should be roughly flat week to week: platform-generic hashtags from the stoplist, evergreen local terms, the national anthem sound. Track their share. Correlated movement of the staples = a collection event.
   - Add a weekly report: "staples moved X% vs items moved Y%."
   - A day on which staples move more than their 28-day 95% band raises a lane-level drift flag, like the A1 overlap check.
3. **Positive controls.**
   - (a) Spike injection (backtest.py has it).
   - (b) Calendar moments that must surface (Q5).
   - (c) Known platform events: a big release or award with a fixed date.
   Report recall on these per market.
4. **Interrupted time series at every protocol change**, vendor parser fix and panel rotation. Use segmented regression on lane totals and on negative-control shares, with the impact model written before looking. Any "culture shift" claim whose onset falls within ±3 days of a break is held [Inferred window].
5. **Silent breaks.** Run BOCPD on lane-level metrics (Q1). This is the Google Flu Trends lesson [49]: the vendor's or platform's algorithm changes under you.
6. **Locality.** Show per-market locality share on every market claim. Compute "KE-confirmed" states only from geo-confirmed creators, and report how many items survive that stratification.
7. **Volume ≠ culture.** Volume changes are first explained by, in order: (i) collection effort and coverage; (ii) platform ranking changes (boards and feeds are the platform's choice); (iii) a single broadcast; (iv) coordinated activity (42's flag); and only then (v) cultural change. Cards should state which of these 42 checked.

**Evaluation of validity guards.** Each guard is a classifier of "measurement event". Score it on logged known vendor incidents (positive) and stable weeks (negative) with precision and recall, per market.

### Q9. Evaluation methodology

**Literature**
- **Rolling-origin evaluation:** each test point uses only earlier data. Bergmeir and Benítez [56] found blocked cross-validation gave robust model selection for stationary autoregressive series. 42's data is non-stationary, so use pure rolling origin [Inferred].
- **Purging and embargo** (López de Prado, 2018; described via [55]): remove training observations whose label windows overlap the test period, and add an embargo after each test fold for serial correlation.
- **Leakage is common.** Kapoor and Narayanan [54] found leakage in 294 papers across 17 fields, with a taxonomy of 8 types. In a civil-war case study, ML's advantage over regression disappeared once leakage was fixed.
- **Scoring and calibration.**
  - The Brier score [58] is strictly proper (Gneiting and Raftery [59]).
  - Reliability diagrams need consistency bars from resampling to judge deviations (Bröcker and Smith [60]).
  - Platt scaling vs isotonic regression [61]: isotonic is more flexible but needs more data and overfits on small calibration sets.
- **Intervals.** Adaptive conformal inference [62] gives long-run coverage without exchangeability by updating one parameter online. EnbPI [63] gives approximately valid sequential intervals under strongly mixing errors without retraining. MAPIE implements both (`TimeSeriesRegressor`, methods "enbpi" and "aci"; the class name differs by version).
- **Selective prediction** [64]: risk-coverage curves describe the full trade-off; set a target risk and abstain to meet it.
- **Multiple comparisons:** Benjamini-Hochberg [65], already used per market and day.
- **Simple baselines are hard to beat.**
  - Makridakis et al. [66]: on 1,045 monthly M3 series, ML methods were beaten by 8 statistical methods at every horizon, at greater cost.
  - M4 [67] (100,000 series, 61 methods): per a SAS summary, pure ML entries were beaten by a simple combination benchmark. The winner was a statistical/ML hybrid.
  - M5 (Walmart, 28-day horizon; secondary sources [68]): LightGBM-based methods did well. ML wins when there are many related series and long histories.
  - MASE [69] scales error by the in-sample naive forecast and avoids undefined percentage errors at zero counts. That matters for 42's sparse counts.

**42-specific evaluation rules** are in section (d).

### Q10. Credible industry and academic systems: evidence vs marketing

| System | What is documented | Evidence quality | Lesson for 42 |
|---|---|---|---|
| Google Trends [70] | Uses a sample of searches (a real-time sample for 7 days, a non-real-time one back to 2004); each point is divided by total searches for that place and time, then rescaled 0 to 100; some automated searches are filtered | Documented method; no sampling rate published | The share-of-total denominator is the right idea. Rescaling per query makes values from different windows non-comparable. A good external annual prior and lead-time reference clock |
| X / Twitter Trends [71,72] | Algorithmic and personalised by default (follows, interests, location); favours "popular now" over staples; post count is "one of the factors"; related hashtags are grouped | Help-page level. The velocity-over-volume claim is an external analysis (Lotan, via Nieman Lab), not an official spec | 42's own-baseline velocity test is the same family. Steady risers are under-served (hence the CUSUM). Use the X trends archive as an external clock, not evidence (already policy) |
| Exploding Topics [73] | Scrapes Reddit, Quora, Google, Amazon, YouTube and podcasts; ML flags growth; humans approve only topics with "steady growth over an extended period"; status labels are driven by Google search data | Self-described. The "6 months before mainstream" claim has no published backtest | Human approval on sustained growth is effectively a lasting filter. 42 should measure what Exploding Topics only asserts |
| Glimpse [74] | Built on Google Trends; year-on-year and month-on-month growth, deseasonalised; says it is not for fleeting trends | Claims of "over 95%" and "87%" 12-month forecast accuracy on different pages, with no metric or test period | Marketing. A useful reminder to always state the metric, horizon and baseline |
| WGSN [75] | Proprietary plus 2,800 third-party sources, AI triangulation, expert review; "high accuracy" for TrendCurve | No figures, no backtest, no independent validation | Marketing. 42's differentiator can be a published scorecard |
| GDELT [76] | Large-scale machine-coded news events | Critiques: no cross-article event deduplication; of GDELT's valid protest URLs, only 21% were true protest events after filtering; US/Western media over-represented (ONS); a 2025 study estimates about 55% accuracy on key fields and about 20% redundancy (secondary) | Use only as a coarse news context signal, deduplicated, never as counts of events |
| Media Cloud [77] | Open-source news collection running for 10+ years, documented collection strategy, open API (ICWSM 2021) | Academic, documented | Best free candidate for "news-led vs native" and as a lead-time reference clock for news uptake, if its ZA, NG and KE source lists are adequate (check) |
| Academic trend detection: Aiello et al. [78] | Compared 6 topic-detection methods on 3 Twitter datasets: standard NLP works for focused topics; heterogeneous streams need methods that mine temporal distributions (they propose df-idf_t over n-gram co-occurrence) | Peer-reviewed comparison | 42's streams are heterogeneous; temporal per-item baselines (what 42 does) are the right family |

---

## (c) Rejected alternatives

| Alternative | Why rejected for 42 |
|---|---|
| Deep cascade predictors (graph neural networks or recurrent models over reshare cascades; deep Hawkes variants) | Need full reshare cascades and a follower graph, which 42 does not collect. Even with full data, cascade-size R^2 stays below 0.5 [17]. Individual influencer prediction is unreliable [18]. Leakage risk is high [54] |
| LSTM, Transformer or "foundation model" forecasting of item counts on weeks of data | About 3 weeks of one protocol, sparse counts. On 1,045 M3 series, ML lost to statistical methods [66]; pure ML lost to simple combinations in M4 [67]. ML won M5 only with long histories over many related series [68]. Revisit for lane totals after 12+ months |
| Granger causality, transfer entropy or PCMCI used to say "platform A influences platform B" or "NG drives KE" | They test incremental predictability. Under sampling bias, unequal pull cadence, changing coverage and common exogenous drivers (about 29% external even on full Twitter [22]), they produce confident artefacts. Allowed only as an internal diagnostic, labelled "leads in 42's sample" |
| Endogenous/exogenous labels from Hawkes branching ratios or Crane-Sornette exponents | Missing events severely bias Hawkes estimates [11]; 42 sees samples, not streams; the exponents in [2] need about 5M items and dense daily views. Keep F and the retention ratio as descriptors only |
| NetInf / diffusion-network inference over creators | Needs many cascades over a stable node set with infection times. 42's creator set is a rotating sample. Possibly later over (platform × market) nodes |
| Dynamic topic models (Blei and Lafferty) | Fixed K and bag of words on short, multilingual, code-switched text; the cost of the slice structure. 42's embedding plus matcher design is better suited |
| BERTopic merge_models or a global refit for IDs | Renumbers topics and breaks stable item IDs (DATA.md already rejects it) |
| STL or Prophet for annual seasonality now | Needs at least 2 full yearly periods. The legacy regime is not comparable. Use a moments calendar and an external long-history prior instead |
| Isotonic calibration before about 1,000 resolved forecasts per target and market | Overfits small calibration sets [61]. Use Platt (logistic) or no recalibration, and report reliability with consistency bars [60] |
| Unconstrained DTW for analogues | Warping removes timing, which is exactly what lead time and "how fast" questions need [Inferred]. Use Euclidean or a ≤ 1-day Sakoe-Chiba window |
| Vendor accuracy claims (Glimpse 87% or 95%, WGSN "high accuracy", Exploding Topics "6 months early") as benchmarks | No metric, horizon, baseline or backtest is published. Not evidence |
| A second burst test (Kleinberg) alongside the NB test + BH | Duplicates the existing test and adds a family of tests and a tuning surface without new information [Inferred] |

---

## (d) Recommended evaluation protocol for 42 (Gamma 3.1)

### d1. Freeze before tuning
1. **Write the label definitions down and version them as `label_version`** before any model is tuned:
   - lasting_14 and lasting_28;
   - reach_rising, cross_market, persist_50 (existing);
   - seasonal_correct (back to baseline within 14 days after the moment);
   - reference events for lead time (first X-trends archive appearance, first board entry where not an input, first Google Trends spike week, first Media Cloud story).
   A change makes a new version; old results are not re-scored silently.
2. **Write each model's feature list, hyperparameter grid, abstention rule and decision threshold** into a config with a hash, committed before the test period opens.
3. **Hold out a final test period** that nobody looks at until the end: the last 3 weeks before the Gamma decision [Inferred length]. All tuning happens on earlier origins.

### d2. Chronological splits and leakage prevention
- **Rolling origin by day.** For each origin day t from the first day with 14 valid protocol days, fit or tune on data available before t's cutoff, predict t+7 and t+14, and score when resolved. This is backtest.py's cutoff rule: available_at before the start of t's good stats run.
- **Purge:** a training example from day s with horizon h is usable at origin t only if s + h < t, i.e. its label resolved before the cutoff.
- **Embargo:** after each test block, skip h days (7 or 14) before reusing data for training, so serially correlated neighbours of test labels do not leak [55].
- **42-specific leakage sources to close [Inferred from DATA.md]**
  - (a) Vendor curves restate past days. Always read the version available before the cutoff, never the latest (v_item_counter_daily_current is the latest, so backtests must filter on available_at).
  - (b) cultural_map versions and labels created after t. Use the map version begun before t (backtest.py does this).
  - (c) The 28-day baseline and the dispersion pooled over a window that includes t+ days.
  - (d) The Benjamini-Hochberg family must be the family as it would have existed on t.
  - (e) Analogue libraries may only contain trajectories resolved before t.
  - (f) Thresholds (Rising ratio 2, floors) were set from the same weeks. Report results on the held-out period only.
  - (g) Placebo and control samples must be drawn by the same random process as in production.
  - (h) Human labels made after seeing outcomes (labelers must be blind to t+ data).
- **Protocol eras.** Never train on one era and test on another without reporting it as a transfer test. The legacy era is excluded from training for any magnitude-based model.

### d3. Baselines (every model must beat all of these, per market)
1. **Persistence:** the state or level today = the state or level at the horizon (existing).
2. **Base rate (climatology):** the market's rate of the target among candidates in the same week, and in the 10% random control sample.
3. **Simple rules:** days present so far; retention so far; log-linear early growth [13]; the seasonal naive (same weekday last week) for counts.
4. **The current production rule** (Rising, CUSUM).

### d4. Metrics
- **Probabilistic targets:**
  - the Brier score and the Brier skill score vs persistence and vs base rate;
  - reliability diagrams with consistency bars [60], in 5 to 10 bins with at least 20 per bin;
  - sharpness (the spread of forecast probabilities).
- **Alerts:**
  - precision@k for k = 5 and 10 per market per day, with lift over the base rate;
  - recall of reference events;
  - the lead-time distribution vs each external reference clock (median, interquartile range, share with lead ≥ 1 day);
  - false alarms per week per market.
- **Counts:** MASE vs seasonal naive [69]; interval coverage and width for ACI [62] or EnbPI [63] at 80% nominal.
- **Uncertainty on every metric:** a block bootstrap over days (blocks of 7) with 95% intervals. With n events below 30 per market, report counts, not rates, and Wilson intervals.

### d5. Per-market reporting and multiple comparisons
- Report ZA, NG and KE separately, never pooled only. Pooled results can hide that one market (likely KE, given about 12% locality) fails.
- Report KE both overall and restricted to geo-confirmed creators.
- When comparing many variants (thresholds, models), count them. Report all results, not the best one. Use the held-out period for the single chosen variant. Treat a variant's win as real only if its 95% bootstrap interval for the Brier skill difference excludes 0 in the held-out period, or apply BH [65] across variants and markets.

### d6. Positive and negative controls (run every backtest)
- **Positive controls:** injected spikes at 1.5, 2, 3 and 5 times (existing); upcoming calendar moments per market (Q5); logged known events with fixed dates.
- **Negative controls:**
  - placebo series-days (existing);
  - staple items whose share should be flat (Q8);
  - shuffled-date controls: permute item labels within a day and re-run the diffusion and lead-lag analyses, which should find nothing;
  - re-fit controls for clustering transitions (Q4).
- **A method fails if** it "finds" signal in negative controls at a rate above its nominal level (e.g. above 5%).

### d7. Calibration
- Until about 300 resolved forecasts per target and market [Inferred], publish raw rule outputs with their observed reliability ("of past cases like this, X of Y happened"), not recalibrated probabilities.
- From about 300: Platt scaling fitted on rolling past windows only.
- From about 1,000 or more: isotonic is allowed [61].
- Recalibrate on a schedule (weekly), never on the test block.
- Check calibration per market and per lane class. A model calibrated in ZA can be badly off in KE.

### d8. Abstention (selective prediction)
- Build risk-coverage curves [64]: rank forecasts by a confidence score (analogue-quality, distance from a protocol break, number of observed days, collection validity, locality share) and plot Brier score or precision against coverage.
- Choose the abstention threshold on training origins to meet a target, e.g. "precision@10 ≥ 0.5 at the highest coverage achieving it". Freeze it, then report realised risk and coverage on the held-out period.
- **Hard abstentions [Inferred, consistent with 42's existing gates]**
  - fewer than 14 valid protocol days in the market and lane;
  - any invalid collection day in the item's last 3 days;
  - onset within ±3 days of a protocol, vendor or panel change;
  - k-th analogue distance above the library's 90th percentile;
  - fewer than 200 resolved analogues;
  - locality share under 30% for a market-specific claim (show "Market unconfirmed", as 42 already does);
  - the Efron empirical-null spread above 1.1 in the market that week.
- Abstention is shown to users as "Not enough history to judge", with the reason. It is never silently omitted.

### d9. Timeline [Inferred from data depth]
- **Now (mid-October 2026):** freeze labels and configs; adopt the descriptive features (Q1 shape fields, Q7 participation, Q4 transition log, Q8 denominators and controls); run BOCPD on lane health.
- **About 1 November:** first rolling origins with 14-day outcomes, but only for items first detected after about 1 October with a 28-day baseline. Expect small n; report counts.
- **Late November:** the first lasting_28 labels; the analogue library reaches a few hundred per market; first Platt calibration if n allows.
- **Gamma decision:** use the held-out final 3 weeks; switch on per market only where the baseline is beaten with intervals excluding 0.
- **2027+:** STL on lane totals, cross-platform Hawkes or NetInf on (platform × market) nodes, evolutionary clustering, an annual seasonality check.

---

## (e) Source list

Each entry gives author(s), year, title, venue, and the URL opened or shown in a search result. "Secondary" means the cited figure came from a summary rather than the primary text.

1. Kleinberg, J. (2002). Bursty and hierarchical structure in streams. Proc. KDD 2002; journal version in Data Mining and Knowledge Discovery 7:373 to 397. https://cs.cmu.edu/~wcohen/10-802/fixed/Kleinberg%2C_KDD_2002.html ; R implementation: https://archive.linux.duke.edu/cran/web/packages/bursts/refman/bursts.html
2. Crane, R., Sornette, D. (2008). Robust dynamic classes revealed by measuring the response function of a social system. PNAS 105(41):15649 to 15653. https://arxiv.org/abs/0803.2189 (figures from https://ar5iv.labs.arxiv.org/html/0803.2189)
3. Lehmann, J., Gonçalves, B., Ramasco, J.J., Cattuto, C. (2012). Dynamical classes of collective attention in Twitter. WWW 2012, 251 to 260. https://arxiv.org/abs/1111.1896 (figures from https://ar5iv.arxiv.org/html/1111.1896)
4. Adams, R.P., MacKay, D.J.C. (2007). Bayesian online changepoint detection. arXiv:0710.3742. https://arxiv.org/abs/0710.3742
5. Killick, R., Fearnhead, P., Eckley, I.A. (2012). Optimal detection of changepoints with a linear computational cost. JASA 107(500):1590 to 1598. https://arxiv.org/abs/1101.1438v3
6. Truong, C., Oudre, L., Vayatis, N. (2020). Selective review of offline change point detection methods. Signal Processing 167:107299; software `ruptures`. https://arxiv.org/abs/1801.00826 ; https://pypi.org/project/ruptures
7. Bacry, E., Bompaire, M., Deegan, P., Gaïffas, S., Poulsen, S.V. (2018). tick: a Python library for statistical learning, with an emphasis on Hawkes processes. JMLR 18. https://jmlr.org/papers/v18/17-381.html
8. Rizoiu, M.-A., Xie, L., Sanner, S., Cebrián, M., Yu, H., Van Hentenryck, P. (2017). Expecting to be HIP: Hawkes intensity processes for social media popularity. WWW 2017, 735 to 744. https://arxiv.org/abs/1602.06033
9. Kirchner, M. (2017). An estimation procedure for the Hawkes process. Quantitative Finance 17:571 to 595. https://arxiv.org/abs/1509.02017
10. Zhao, Q., Erdogdu, M.A., He, H.Y., Rajaraman, A., Leskovec, J. (2015). SEISMIC: A self-exciting point process model for predicting tweet popularity. KDD 2015. https://arxiv.org/abs/1506.02594
11. Deutsch, I., Ross, G.J. (2020). ABC learning of Hawkes processes with missing or noisy event times. arXiv:2006.09015. https://arxiv.org/abs/2006.09015 (author names as given in the search summary)
12. Asur, S., Huberman, B.A., Szabo, G., Wang, C. (2011). Trends in social media: persistence and decay. ICWSM 2011. https://arxiv.org/abs/1102.1402 ; https://ojs.aaai.org/index.php/ICWSM/article/view/14167
13. Szabo, G., Huberman, B.A. (2010). Predicting the popularity of online content. Communications of the ACM 53(8):80 to 88. https://arxiv.org/abs/0811.0405
14. Nikolov, S. (2012). Trend or no trend: a novel nonparametric method for classifying time series. M.Eng. thesis, MIT (advisors D. Shah, S. Banerjee). https://dspace.mit.edu/handle/1721.1/85399 ; figures from MIT News (2012): https://news.mit.edu/2012/predicting-twitter-trending-topics-1101
15. Cheng, J., Adamic, L.A., Dow, P.A., Kleinberg, J.M., Leskovec, J. (2014). Can cascades be predicted? WWW 2014, 925 to 936. https://arxiv.org/abs/1403.4608
16. Weng, L., Menczer, F., Ahn, Y.-Y. (2013). Virality prediction and community structure in social networks. Scientific Reports 3:2522. https://arxiv.org/abs/1306.0158 (figures from https://ar5iv.labs.arxiv.org/html/1306.0158); related ICWSM 2014 paper "Predicting successful memes using network and community structure": https://ojs.aaai.org/index.php/ICWSM/article/view/14530
17. Martin, T., Hofman, J.M., Sharma, A., Anderson, A., Watts, D.J. (2016). Exploring limits to prediction in complex social systems. WWW 2016. https://arxiv.org/abs/1602.01013 (R^2 below 0.5 from summaries; secondary)
18. Bakshy, E., Hofman, J.M., Mason, W.A., Watts, D.J. (2011). Everyone's an influencer: quantifying influence on Twitter. WSDM 2011, 65 to 74. https://www.microsoft.com/en-us/research/?p=162831
19. Goel, S., Anderson, A., Hofman, J., Watts, D.J. (2016). The structural virality of online diffusion. Management Science 62(1):180 to 196. https://pubsonline.informs.org/doi/fpi/10.1287/mnsc.2015.2158
20. Romero, D.M., Meeder, B., Kleinberg, J. (2011). Differences in the mechanics of information diffusion across topics. WWW 2011, 695 to 704. https://ir.webis.de/anthology/2011.wwwconf_conference-2011.73
21. Gomez-Rodriguez, M., Leskovec, J., Krause, A. (2010). Inferring networks of diffusion and influence. KDD 2010, 1019 to 1028; TKDD 5(4) 2012. https://snap.stanford.edu/netinf/
22. Myers, S.A., Zhu, C., Leskovec, J. (2012). Information diffusion and external influence in networks. KDD 2012. https://arxiv.org/abs/1206.1331
23. Zannettou, S., Caulfield, T., De Cristofaro, E., Kourtellis, N., Leontiadis, I., Sirivianos, M., Stringhini, G., Blackburn, J. (2017). The Web Centipede. IMC 2017, 405 to 417. https://arxiv.org/abs/1705.06947
24. Zannettou, S., et al. (2018). On the origins of memes by means of fringe Web communities. IMC 2018. https://arxiv.org/abs/1805.12512
25. Leskovec, J., Backstrom, L., Kleinberg, J. (2009). Meme-tracking and the dynamics of the news cycle. KDD 2009. https://snap.stanford.edu/memetracker/ (2.5-hour lag from a 2010 Berkman talk abstract: https://cyber.harvard.edu/events/luncheon/2010/02/leskovec; secondary)
26. Traag, V.A., Waltman, L., van Eck, N.J. (2019). From Louvain to Leiden: guaranteeing well-connected communities. Scientific Reports 9:5233. https://arxiv.org/abs/1810.08473
27. Granger, C.W.J. (1969). Investigating causal relations by econometric models and cross-spectral methods. Econometrica 37(3):424 to 438. https://www.econometricsociety.org/publications/econometrica/1969/08/01/investigating-causal-relations-econometric-models-and-cross
28. Schreiber, T. (2000). Measuring information transfer. Physical Review Letters 85(2):461 to 464. Via https://en.wikipedia.org/wiki/Transfer_entropy (secondary)
29. Ver Steeg, G., Galstyan, A. (2012). Information transfer in social media. WWW 2012, 509 to 518. https://ar5iv.arxiv.org/html/1110.2724
30. Runge, J., Nowack, P., Kretschmer, M., Flaxman, S., Sejdinovic, D. (2019). Detecting and quantifying causal associations in large nonlinear time series datasets. Science Advances 5(11):eaau4996. https://pmc.ncbi.nlm.nih.gov/articles/PMC6881151
31. Blei, D.M., Lafferty, J.D. (2006). Dynamic topic models. ICML 2006, 113 to 120. https://mlanthology.org/icml/2006/blei2006icml-dynamic
32. Grootendorst, M. (2022). BERTopic: Neural topic modeling with a class-based TF-IDF procedure. arXiv:2203.05794. https://arxiv.org/abs/2203.05794
33. Spiliopoulou, M., Ntoutsi, I., Theodoridis, Y., Schult, R. (2006). MONIC: modeling and monitoring cluster transitions. KDD 2006, 706 to 711. https://researchr.org/publication/SpiliopoulouNTS06/bibtex ; R `clusTransition`: https://cran.uni-muenster.de/web/packages/clusTransition/refman/clusTransition.html
34. Chakrabarti, D., Kumar, R., Tomkins, A. (2006). Evolutionary clustering. KDD 2006. https://dblp1.uni-trier.de/db/conf/kdd/kdd2006.html
35. Greene, D., Doyle, D., Cunningham, P. (2010). Tracking the evolution of communities in dynamic social networks. ASONAM 2010, 176 to 183. https://researchrepository.ucd.ie/entities/publication/b5d546e4-d02d-43bb-a5f3-c4755d60b915 ; tool: https://github.com/derekgreene/dynamic-community/blob/master/README.md
36. Hamilton, W.L., Leskovec, J., Jurafsky, D. (2016). Diachronic word embeddings reveal statistical laws of semantic change. ACL 2016, 1489 to 1501. https://aclanthology.org/P16-1141/
37. Cleveland, R.B., Cleveland, W.S., McRae, J.E., Terpenning, I. (1990). STL: a seasonal-trend decomposition procedure based on loess. Journal of Official Statistics 6:3 to 73. Via statsmodels: https://www.statsmodels.org/dev/generated/statsmodels.tsa.seasonal.STL.html
38. Hyndman, R.J., Athanasopoulos, G. (2021). Forecasting: principles and practice, 3rd ed. OTexts. Cited via sktime NaiveForecaster docs: https://sktime-backup.readthedocs.io/en/v0.18.0/api_reference/auto_generated/sktime.forecasting.naive.NaiveForecaster.html
39. Taylor, S.J., Letham, B. (2018). Forecasting at scale. The American Statistician 72(1):37 to 45. https://ideas.repec.org/a/taf/amstat/v72y2018i1p37-45.html
40. Lorenz, E.N. (1969). Atmospheric predictability as revealed by naturally occurring analogues. Journal of the Atmospheric Sciences 26:636 to 646. https://wind.mit.edu/~emanuel/Lorenz/EdLorenz/publications_all.htm
41. Delle Monache, L., Eckel, F.A., Rife, D.L., Nagarajan, B., Searight, K. (2013). Probabilistic weather prediction with an analog ensemble. Monthly Weather Review 141:3498 to 3516. https://opensky.library.ucar.edu/islandora/object/articles%3A12922
42. Yeh, C.-C.M., Zhu, Y., Ulanova, L., Begum, N., Ding, Y., Dau, H.A., Silva, D.F., Mueen, A., Keogh, E. (2016). Matrix Profile I: all pairs similarity joins for time series. ICDM 2016, 1317 to 1322. https://www.cs.ucr.edu/~eamonn/PID4481997_extend_Matrix%20Profile_I.pdf
43. Law, S.M. (2019). STUMPY: a powerful and scalable Python library for time series data mining. JOSS 4(39):1504. https://joss.theoj.org/papers/10.21105/joss.01504
44. Ye, L., Keogh, E. (2009). Time series shapelets: a new primitive for data mining. KDD 2009. https://www.cs.ucr.edu/~eamonn/shaplet.pdf
45. Bagnall, A., Lines, J., Bostrom, A., Large, J., Keogh, E. (2017). The great time series classification bake off. Data Mining and Knowledge Discovery (extended version arXiv:1602.01711). https://arxiv.org/abs/1602.01711
46. Jost, L. (2006). Entropy and diversity. Oikos 113(2):363 to 375. Via https://jonlefcheck.net/2012/10/23/diversity-as-effective-numbers/ (secondary)
47. Chao, A., Jost, L. (2012). Coverage-based rarefaction and extrapolation. Ecology 93:2533 to 2547. https://esapubs.org/archive/ecol/E093/238/appendix-F.pdf ; iNEXT vignette: https://mirrors.sjtug.sjtu.edu.cn/cran/web/packages/iNEXT/vignettes/Introduction.pdf
48. Ruths, D., Pfeffer, J. (2014). Social media for large studies of behavior. Science 346(6213):1063 to 1064. https://people.cs.umass.edu/~brenocon/smacss2015/papers/Science-2014-Ruths-1063-4.pdf ; 65% vs 90% figures from the McGill press release: https://www.mcgill.ca/newsroom/node/21651
49. Lazer, D., Kennedy, R., King, G., Vespignani, A. (2014). The parable of Google Flu: traps in big data analysis. Science 343:1203 to 1205. https://www.networkscienceinstitute.org/publications/the-parable-of-google-flu-traps-in-big-data-analysis-2 ; "100 of 108 weeks" from https://bigdatawire.com/2014/03/14/google_s_flu_analytics_fail_highlights_big_data_s_shortcomings (secondary)
50. Morstatter, F., Pfeffer, J., Liu, H., Carley, K.M. (2013). Is the sample good enough? Comparing data from Twitter's Streaming API with Twitter's Firehose. ICWSM 2013, 400 to 408. https://arxiv.org/abs/1306.5204 (figures from https://ar5iv.labs.arxiv.org/html/1306.5204)
51. Tufekci, Z. (2014). Big questions for social media big data: representativeness, validity and other methodological pitfalls. ICWSM 2014, 505 to 514. https://ojs.aaai.org/index.php/ICWSM/article/view/14517
52. Lopez Bernal, J., Cummins, S., Gasparrini, A. (2017). Interrupted time series regression for the evaluation of public health interventions: a tutorial. International Journal of Epidemiology 46(1):348 to 355; corrigendum 2020. https://pmc.ncbi.nlm.nih.gov/articles/PMC5407170 ; https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7750921/
53. Lipsitch, M., Tchetgen Tchetgen, E., Cohen, T. (2010). Negative controls: a tool for detecting confounding and bias in observational studies. Epidemiology 21(3):383 to 388. https://pmc.ncbi.nlm.nih.gov/articles/PMC3053408/
54. Kapoor, S., Narayanan, A. (2023). Leakage and the reproducibility crisis in machine-learning-based science. Patterns 4:100804. https://pmc.ncbi.nlm.nih.gov/articles/PMC10499856
55. López de Prado, M. (2018). Advances in Financial Machine Learning (purged and embargoed cross-validation). Via https://en.wikipedia.org/wiki/Purged_cross-validation (secondary)
56. Bergmeir, C., Benítez, J.M. (2012). On the use of cross-validation for time series predictor evaluation. Information Sciences. https://research.monash.edu/en/publications/on-the-use-of-cross-validation-for-time-series-predictor-evaluati/
57. (unused)
58. Brier, G.W. (1950). Verification of forecasts expressed in terms of probability. Monthly Weather Review 78(1):1 to 3. Via https://en.wikipedia.org/wiki/Glenn_W._Brier
59. Gneiting, T., Raftery, A.E. (2007). Strictly proper scoring rules, prediction, and estimation. JASA 102(477):359 to 378. https://apps.dtic.mil/sti/tr/pdf/ADA459827.pdf
60. Bröcker, J., Smith, L.A. (2007). Increasing the reliability of reliability diagrams. Weather and Forecasting 22(3):651 to 661 (LSE preprint dated 2006). https://www.lse.ac.uk/CATS/Assets/PDFs/Publications/Papers/2007/73-IncreasingReliabilityDiagrams-2006-Brocker-Smith.pdf
61. Niculescu-Mizil, A., Caruana, R. (2005). Predicting good probabilities with supervised learning. ICML 2005, 625 to 632. https://mlanthology.org/icml/2005/niculescumizil2005icml-predicting
62. Gibbs, I., Candès, E. (2021). Adaptive conformal inference under distribution shift. NeurIPS 2021. https://proceedings.neurips.cc/paper/2021/hash/0d441de75945e5acbc865406fc9a2559-Abstract.html
63. Xu, C., Xie, Y. (2021). Conformal prediction interval for dynamic time-series. ICML 2021, PMLR 139:11559 to 11569. https://proceedings.mlr.press/v139/xu21h ; MAPIE: https://mapie.readthedocs.io/en/latest/generated/mapie.regression.MapieTimeSeriesRegressor.html
64. Geifman, Y., El-Yaniv, R. (2017). Selective classification for deep neural networks. NeurIPS 2017. https://arxiv.org/abs/1705.08500
65. Benjamini, Y., Hochberg, Y. (1995). Controlling the false discovery rate. JRSS B 57(1):289 to 300. https://www.dcscience.net/Benjamini-Hochberg-1995-FDR.pdf
66. Makridakis, S., Spiliotis, E., Assimakopoulos, V. (2018). Statistical and machine learning forecasting methods: concerns and ways forward. PLOS ONE 13(3):e0194889. https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5870978/
67. Makridakis, S., Spiliotis, E., Assimakopoulos, V. (2020). The M4 Competition: 100,000 time series and 61 forecasting methods. International Journal of Forecasting 36(1):54 to 74. https://pure.unic.ac.cy/en/publications/the-m4-competition-100000-time-series-and-61-forecasting-methods/ ; pure-ML finding via SAS blog: https://blogs.sas.com/content/forecasting/2020/01/08/m4-forecasting-competition-results-and-commentary (secondary)
68. M5 competition results (secondary; IJF 2022 primary not opened): https://github.com/umitkaanusta/m5-methods ; https://arxiv.org/abs/2401.13912
69. Hyndman, R.J., Koehler, A.B. (2006). Another look at measures of forecast accuracy. International Journal of Forecasting 22(4):679 to 688. https://www.doi.org/10.1016/J.IJFORECAST.2006.03.001
70. Google (n.d.). FAQ about Google Trends data. https://support.google.com/trends/answer/4365533 ; https://support.google.com/trends/answer/4355213
71. X Corp. (n.d.). X Trends FAQ. https://help.x.com/en/using-x/x-trending-faqs
72. Nieman Lab (2011), reporting G. Lotan's analysis: Why hasn't #OccupyWallStreet trended in New York? https://www.niemanlab.org/2011/10/why-hasnt-occupywallstreet-trended-in-new-york/
73. Exploding Topics (n.d.). Methodology and blog pages. https://explodingtopics.com/methodology ; https://explodingtopics.com/blog/find-trending-topics
74. Glimpse (n.d.). Trend forecasting guide. https://meetglimpse.com/software-guides/trend-forecasting/ ; https://meetglimpse.com/software-guides/exploding-topics-alternatives/
75. WGSN (n.d.). AI and data / methodology pages. https://www.wgsn.com/en/what-we-do/ai-data
76. GDELT critiques: Political Violence at a Glance (2014), Raining on the parade. https://politicalviolenceataglance.org/2014/02/20/raining-on-the-parade-some-cautions-regarding-the-global-database-of-events-language-and-tone-dataset/ ; UK ONS GDELT data quality note: https://www.ons.gov.uk/peoplepopulationandcommunity/birthsdeathsandmarriages/deaths/methodologies/globaldatabaseofeventslanguageandtonegdeltdataqualitynote ; protest-event validation (21% figure): https://people.cs.vt.edu/~naren/papers/1502.full.pdf ; 2025 accuracy study (secondary): https://ideas.repec.org/a/gam/jdataj/v10y2025i10p158-d1762908.html
77. Roberts, H., et al. (2021). Media Cloud: massive open source collection of global news on the open web. ICWSM 2021, 15(1):1034 to 1045. https://arxiv.org/abs/2104.03702
78. Aiello, L.M., Petkos, G., Martin, C., Corney, D., Papadopoulos, S., Skraba, R., et al. (2013). Sensing trending topics in Twitter. IEEE Transactions on Multimedia 15(6):1268 to 1282. https://www.nokia.com/bell-labs/publications-and-media/publications/sensing-trending-topics-in-twitter/

Not verified and therefore not cited: Tashman (2000), on rolling-origin evaluation; El-Yaniv and Wiener (2010); the Kwak and An and the Hammond and Weidmann GDELT papers; Chao and Shen (2003); specific accuracy numbers for Cheng et al. (2014); the exact R^2 values in Martin et al. (2016).
