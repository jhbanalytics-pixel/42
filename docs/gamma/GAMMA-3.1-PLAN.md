# 42 Gamma 3.1: vision, outcomes, experiments and delivery plan

Prepared 16 October 2026 by the Gamma thread. Source: branch `gamma/research-20261016-407cj1`, cut from `release/beta-20261016` at 6a7f90c (Beta candidate; Alpha 2.4 live on f9bd07c9). Nothing here is deployed or authorised for deployment.

Supporting material, all in this folder:

| Output asked for | Where |
|---|---|
| 1. Sourced research dossier | [RESEARCH-DOSSIER.md](RESEARCH-DOSSIER.md), with the full methods, design and product reviews in `research/` |
| 2. What the historical data can support | [HISTORICAL-DATA-ASSESSMENT.md](HISTORICAL-DATA-ASSESSMENT.md) |
| 3. Vision and user outcomes | sections 1 and 2 below |
| 4. Experiments with baselines and go/no-go | section 3 below |
| 5. Visual concepts and working prototype | the published prototype (link in the thread) and `prototype/gamma-prototype.html` |
| 6. Delivery plan | section 4 below |

**What Albert needs to do now: nothing.** Gamma waits for Beta. The four decisions Gamma will eventually need are listed in section 5, each with a recommendation, and none is due before Beta is live and accepted.

---

## 0. The finding that shapes everything

42 has far less usable history than its screens suggest, and the statistical engine it was specified with has never been switched on.

- SocialCrawl-era collection began about 28 September 2026 (inferred from code defaults and panel evidence; the warehouse must confirm it). Most protocols in use today restarted between 8 and 13 October after parser fixes, the v2 panel tokens and the item_daily link change. On 16 October that is 3 to 8 clean days for most series.
- No series has passed the backtest that switches its test on, so every `series_test` row has test `none`. Rising, Peaking, Fading and Mainstream cannot occur, `early_signal.py` writes nothing (it skips any series whose test is not `nb`, `early_signal.py:217`), and two of the three forecast targets can only resolve as non-arrivals.
- History views mix regimes. `v_item_waves` counts search-lane and legacy posts in the same wave, and it drives Recurring, Seasonal, History and analogues. A wave can therefore grow because 42 searched harder, and Recurring can be an artefact of the June to September legacy boundary.
- Topic identities drifted on f9 (one Kenya item went from forestry to a heater over three days). Beta's matcher fixes prevent some future bad matches; they repair no history and are not deployed.
- Kenya locality is weak: one feed pull a day against three in ZA and NG, about 12% of the feed in-country, and eligibility still admits market-unconfirmed items.

So the research-led step forward is not a new model on top of this. It is making 42's memory trustworthy, letting the statistics it already has earn their place, and then answering the strategist's most valuable longitudinal question, lasting pattern or spike, with a measured result or an honest abstention. The methods research agrees: at 42's scale every candidate method runs in seconds; the binding constraint is history length and measurement consistency, not compute (METHODS-DOSSIER, opening section).

## 1. Gamma vision

**42 remembers, and shows its working.** Gamma turns 42 from a daily snapshot that often says nothing into a longitudinal instrument. Everything 42 watches carries a record of how it has behaved over time, who takes part, and what 42 could and could not see. A strategist can tell a lasting behaviour from a spike, see what is building before it clears the gate, and judge how far to trust it, quickly, on a phone or a desk.

"Wow" comes from three moments the prototype demonstrates: opening Today on a day with no cleared trends and still finding something worth watching with a clear reason it is not yet a trend; opening one item and reading, in about ten seconds, whether it has lasted, who is taking part and where 42 was blind; and seeing 42 decline to judge when the evidence is not there.

Gamma is not a redesign for its own sake, not a forecasting product and not a culture map. Those are rejected or deferred below with reasons.

## 2. Three user outcomes

Each outcome answers the four questions the brief set.

### O1. Tell a lasting behaviour from an event spike

| Question | Answer |
|---|---|
| What decision does this improve? | Whether a strategist builds a recommendation, creative route or client conversation on a pattern, and how urgently. A spike suits reactive content; a lasting behaviour suits planning. Today the card shows only that something moved. |
| What evidence supports it? | A wave record computed from measured lanes only (unbiased rank lists, counters, panels; never search or legacy): days present, peak-day share, retention in the week after the first high day, distinct and returning creators, creator tier breadth with the unknown share shown, platforms with a measured series. These are established descriptors (Crane and Sornette 2008; Lehmann et al. 2012; Chao and Jost 2012 for coverage-standardised diversity), and the dossier treats them as the baselines any model must beat. |
| How will we measure improvement? | Experiment E2: a frozen `lasting_14` label scored chronologically per market. The wave record is shown as soon as it is valid; a "Lasting" or "Spike" verdict is shown only if E2 passes. A strategist comprehension check on the prototype (section 3, E7). |
| When should the system abstain? | Fewer than 14 days since first seen ("Too early"); any invalid collection day inside the window; the item seen only in search lanes; identity drift above the frozen threshold; under 5 distinct creators in measured lanes; in Kenya, more than half the posts market-unconfirmed. Abstention is shown as a reason, never as a blank. |

### O2. See what is building, even on a day nothing clears

| Question | Answer |
|---|---|
| What decision does this improve? | Where a strategist spends attention: what to Ask about, Watch, or brief a colleague on. Today a 0-of-30 day reads as failure; the held reasons are pipeline wording ("The local why-now was not checked") and no item says how close it came (PRODUCT-REVIEW S1). |
| What evidence supports it? | The trust gate's own results, made legible: for each held candidate, which check it failed in plain words, how far from the bar it is (for example "4 local creators of 8"), the same wave record as O1, and the next check time. One gate shared by every screen, replacing Today's second client-side gate (`today42.jsx:143-236`, which hid 6 of 7 server-cleared fixture trends). |
| How will we measure improvement? | E6: share of later-cleared trends that appeared on Watching first, and the lead in days, per market. Comprehension in E7: a strategist can say why an item is not a trend and what would change that. |
| When should the system abstain? | A held item is never styled or ranked as a trend, never given a state word, and the list order is by distance to the bar, not by a score. Items held for event reaction, promotion or generic tags are grouped as "Not a trend pattern" so they do not crowd the list. |

### O3. Know how far to trust it, and what came before

| Question | Answer |
|---|---|
| What decision does this improve? | How strongly a strategist presents a finding to a client (Observed or Inferred, new or a returning wave) and whether to dig further before a dossier goes out. |
| What evidence supports it? | Coverage per day per item (collected and present, collected and absent, not collected), measurement breaks drawn on every history chart (E0), a lineage of how the item formed, split or merged (E4), recurrence only inside one collection regime, and analogues only once the library exists (E5). |
| How will we measure improvement? | E0 break recall, E4 transition stability, E5 skill over base rate, E7 comprehension of coverage marks. |
| When should the system abstain? | Lineage is hidden when the transition is unstable on a double refit; analogues read "N analogues so far" until at least 10 close analogues and 200 resolved items exist in that market and regime; Recurring and Seasonal are never computed across the legacy boundary. |

A fourth candidate outcome, "see how a behaviour spreads across platforms, markets and communities", is deferred: the data assessment rates cross-platform diffusion as needing repair, and cross-market and cross-community diffusion as not supported (HISTORICAL-DATA-ASSESSMENT section 7). Gamma shows "first seen by 42 on" ordering in words only, which is honest about detection order.

## 3. Experiments

Rules for every experiment, from the methods dossier section (d): labels, features and configs frozen and hashed before tuning (version 1 is `core/eval/gamma/labels_v1.yaml`, sha256 `a378c40fa6123af0b37a4a142231fe9c73c3ccbc5b32df34e37a0e49a78191cc`; it supersedes any label wording in the appendices); rolling-origin evaluation with `available_at <= as_of` (reusing `core/detect/backtest.py`); a purge and an embargo equal to the label horizon; ZA, NG and KE reported separately with block-bootstrap intervals; positive controls (moments calendar, for example Mashujaa Day 20 October in KE) and negative controls (staple items whose share should stay flat, placebo items from the existing placebo lane) in every run; the same retained evidence for baseline and candidate. All runs are offline replays or read-only queries: no SocialCrawl credits (RULES.md rule 14) and no model calls unless stated.

| ID | Question | Candidate | Baselines to beat | Success criteria | Go means / no-go means | Earliest honest read |
|---|---|---|---|---|---|---|
| E0 | Can 42 tell a collection change from a cultural change? | Registered breaks (protocol tokens, parser and rule changes from git) are taken as given. Changepoint detection (PELT, Poisson cost, minimum segment 5 days) runs only on daily lane totals per market (posts, items, calls_ok, located share) to find unregistered breaks such as vendor changes. `ruptures` would be a new dependency, added through the repo's pinned requirements | `collection_health` invalid flags alone | Every changepoint either matches a registered break within 2 days or is explained after inspection; at most 1 unexplained break per market and lane per 30 days. Breaks closer together than the minimum segment are not expected to separate and are handled by the registry | Go: breaks become a guard (waves split at breaks, breaks drawn on charts, E1 to E5 respect them). No-go: registry-only breaks, with the miss recorded | As soon as the history audit has run (G1) |
| E1 | Do clean waves change what 42 calls Recurring and what History shows? | Waves from measured lanes only, tagged by regime and protocol | Current `v_item_waves` (mixed lanes and regimes) | Report the share of current Recurring items and History waves that disappear or split. Use the registered steps in search-lane volume (credit-cap and expansion changes) as natural tests: across each step where search volume changed at least twofold, clean wave counts change by less than 10% while the current view's change is reported beside it | Go: clean waves feed History, Recurring and analogues (needs a storage release, decision D3). No-go would mean the current view is already clean; keep it and record why | With G1 output |
| E2 | Can 42 tell lasting from spike better than simple rules? | Regularised logistic model on the O1 wave record and participation features at day 7 after first seen | B0 market base rate; B1 persistence (still present on day 7); B2 days present in days 1 to 7; B3 log early count (Szabo and Huberman 2010) | Label `lasting_14` as frozen in `labels_v1.yaml`: lexical items first (hashtags, sounds, creators), topics reported separately until E4 passes; items first seen within 7 days of a protocol start are excluded (left censoring); in days 14 to 27 after first seen, at least 10 valid days or the item is excluded, never counted negative; positive when present on at least half the valid days with at least 3 distinct creators. No read below 150 eligible items and 30 positives per market. Go needs, in at least two markets: Brier skill of at least 0.05 over the best baseline with the bootstrap interval above 0; calibration slope 0.8 to 1.2; at 80% coverage, precision at least 10 points above the best baseline. Kenya is judged on its own and abstains if it fails | Go: show Lasting / Spike / Too early on cards and item pages with the record. No-go: show the record only, keep scoring in shadow, re-read monthly | Basis: protocols restarted 9 to 13 Oct, label needs 28 days, a first cohort needs about 3 weeks of items. First read early December 2026 if collection stays valid; earlier only on fewer items with wider intervals |
| E3 | Does breadth of participation add earlier information than volume? | Ablation inside E2: add effective number of creators (Hill q=1 at equal coverage), new vs returning share, tier breadth | E2 without participation features | Brier improvement with interval above 0 in at least two markets | Go: participation features stay in E2 and the "who is taking part" line leads the record. No-go: show participation descriptively only | With E2 |
| E4 | Is a trend's lineage stable enough to show? | Transition log (survive, split, merge, disappear, emerge; MONIC style) over stored daily clusters, plus drift from birth centroid | Current matcher (match or new) | On a double refit of the same day with different seeds, false split or merge under 10%; agreement with human-labelled transitions at least 80% on at least 50 labelled transitions per market, labelled blind to the matcher's output by two reviewers who are not the builder (strategists from D4, or the review lane), disagreements adjudicated | Go: lineage on item pages. No-go: lineage stays internal and feeds identity work | After the Beta matcher is live plus 14 days |
| E5 | Do historical analogues say anything a base rate does not? | k=20 nearest resolved trajectories, same market and regime, z-normalised log counts (constrained DTW as an option), shown as an outcome dotplot | Market base rate; persistence | Brier or CRPS skill over the base rate above 0 with interval, at least 200 resolved items per market | Go: analogue dotplot on item pages. No-go: "N analogues so far" with the list, no outcome claim | Not before about February 2027 on current item volumes (inferred: needs 200 resolved waves per market under one regime) |
| E6 | Does Watching point at what clears next? | Top 5 held candidates per market by distance to the bar | Top 5 of today's held list order on the same days | Over 4 weeks, the Watching top 5 contains at least 1.5 times as many items that clear within 7 days as the baseline top 5, per market, with the count of cleared items stated | Go: Watching is the default zero-day view. No-go: keep the honest held list, fix its wording only | 4 weeks after G4 is live |
| E7 | Do strategists understand it faster and trust it appropriately? | The prototype's journey (Today, item, evidence, Ask, compare history, save) | Current Alpha screens on the same fixture day | 5 strategists, 6 tasks each: at least 5 of 6 tasks correct on the prototype; median time to "is this lasting, and how sure is 42?" under 30 s; nobody reads a held item or an abstention as a trend | Go: build G4 as designed. No-go: revise the components that failed before building | As soon as D4 names the strategists |

Experiments considered and rejected (reasons in RESEARCH-DOSSIER section 4): deep cascade predictors, LSTM or other deep forecasting on weeks of data, Granger causality or transfer entropy as influence claims, Hawkes "endogenous" labels on sampled data, dynamic topic models, STL or Prophet yearly seasonality, unconstrained DTW, and any forecast shown to users before it beats persistence.

## 4. Delivery plan

Gamma ships slices G0 to G4 and G7, plus G2 if its backtest passes. G5 and G6 run in shadow inside Gamma and are shown only on a go. Everything after section 4.2 is longer-term research.

### 4.1 Slices

| Slice | What | Owner | Depends on | Cost | Acceptance check |
|---|---|---|---|---|---|
| G0 Freeze | Labels, features, baselines, evaluation and abstention settings frozen in `core/eval/gamma/labels_v1.yaml` (sha256 above); read-only history audit script `core/setup/history_audit.py` with tests | Gamma thread; independent reviewer | nothing | none | audit, inventory and label tests pass (32, run 16 Oct); the core/setup suite shows no new failure against the Beta base; reviewer pass on the head SHA (RULES.md rule 11) |
| G1 History facts | Albert runs the audit on his PC; Gamma turns the aggregate output into the facts table, E0 and E1 | Albert (one command), Gamma thread | Beta live, G0 merged into the Gamma branch | BigQuery reads capped at 20 GiB for the run (2 GiB per query); at the on-demand list price of about US$6.25 per TiB that is at most about US$0.13 | all 65 queries return aggregates or a stated refusal; facts table replaces every "inferred" in the data assessment; E0 and E1 reports written |
| G2 Statistics earn their switch | Run the existing backtest (`core/detect/backtest.py`) on retained data per series, in replay mode; report which series meet the existing gate (`backtest.py:84-91`: a series is tested only after 14 observed days, then the key needs at least 60 tested placebo series-days and 20 injected spikes, false alarms at most 0.05, recall at least 0.8 at 3 times) | Gamma thread, coordinated with the Beta thread so forecast work (Beta P3) is not duplicated | G1; 14 observed days plus enough tested days to reach 60 per key: on the 12 October protocols, about early November at the earliest | none (replay) | per-series pass/fail table with denominators; Albert's switch-on decision (D2) for passing series only |
| G3 The record | Clean waves (measured lanes, regime and protocol tags, split at breaks), wave descriptors, participation measures with unknown share, coverage per day per item, break marks; exposed through the API to the item page | Gamma thread | E0, E1; storage release (D3) because every `core/detect/sql` file belongs to the applied storage release (`core/setup/release/applied_storage.py:1252`) | none beyond normal job compute | red-then-green fixture tests; replay on retained data shows the E1 differences; old and new readers rehearsed against both schemas; reviewer pass |
| G4 One gate, the Watching day, the item page | Server gate result used by every screen; Today's zero day becomes Watching (distance to bar, missing evidence, next check); one item page (finding, record, specimens, lineage when E4 passes, "N analogues so far"); one figure component that always prints window and basis; warm-up guard on states and charts; fonts no longer block first paint (`main.jsx:43`); one market control; save to dossier from any card | Gamma thread (product experience) | G3 API; E7 result | none | Playwright journeys desktop and mobile, keyboard-only and reduced motion; throttled phone profile (150 ms, 1.6 Mbps, CPU 4x) largest contentful paint at or under 3.0 s against 6.9 to 8.0 s measured on 16 Oct; no target under 24 px anywhere and primary controls at the repo's 44 px; zero text and non-text contrast failures in both themes, including the not-collected hatch; empty, partial and failed states each covered by a test |
| G5 Lasting or spike (shadow, then shown on go) | E2 and E3 scored weekly in shadow; verdict shown only after a go | Gamma thread | G3; label maturity (first read early December) | none | E2 go criteria met and reported per market, or a documented no-go with the record shipped alone |
| G6 Lineage and analogues (shadow) | E4 transition log and E5 analogue scoring | Gamma thread | Beta matcher live plus 14 days; E4 human labels | none | E4 and E5 go or no-go reports; nothing shown without a go |
| G7 Release | Rebase on Beta's final accepted source; full suite on the exact candidate; independent review; release packet; Albert's release approval; deploy; live readiness and one real data cycle checked; menu version and What's new describe only what shipped | Gamma thread; reviewer; Albert approves | Beta live and accepted; G0 to G4 | normal deploy | exact revision and image digest serving, smoke checks, one morning cycle inspected, pending items listed as undeployed |

### 4.2 Order and what can start now

1. Now, while Beta ships: G0 (in this branch, reviewed). E7 runs whenever D4 names strategists. No Beta file is touched.
2. When Beta is live and accepted: rebase, then G1 (one command for Albert), E0 and E1.
3. From about early November (14 observed days plus 60 tested placebo series-days per key on the 12 October protocols): G2 backtest; switch-on decision.
4. G3 then G4, with a storage release for G3.
5. G5 and G6 in shadow from G3; E2 first read early December; E5 not before about February 2027.
6. G7 when G0 to G4 pass. Lasting/Spike joins later as a small follow-up release only if E2 passes.

Dates rest on protocol start dates in the data assessment and the label horizons above; any invalid collection week pushes them out by that week.

### 4.3 Integration boundary

- Gamma works only on `gamma/...` branches cut from `release/beta-20261016` and in its own worktree. It does not edit files owned by Beta lanes (BETA-LANES.md: ask.py, cluster.py, brief/job.py, warehouse.py, DATA.md identity section, forecast files) and does not merge into Beta.
- After Beta is live and accepted, Gamma rebases onto Beta's final source; Beta's own fixes are inherited, not re-implemented.
- Shared-file changes Gamma needs later (for example brief selection for the Watching list) go to the owning lane as a proposed patch through the coordinator.
- BETA-LANES.md assigns the frontend Today, Ask and Evidence screens to Beta P3 and everything else to the audit lane, and `core/detect/sql` belongs to the storage release. G3 and G4 touch those areas, so they start only after Beta is accepted and those lanes have closed, with ownership confirmed through the coordinator first.
- Commits are authored as Albert Meintjes with no trailers (RULES.md rule 10); every head gets an independent review before merge (rule 11).

### 4.4 Longer-term research (not Gamma)

Cross-platform diffusion after id de-duplication across routes and the tests are on; cross-market diffusion after locality v2 is live; community mapping only with a fixed creator-graph protocol and a review under RULES.md rule 1; forecasting shown to users only after it beats persistence on 200 resolved forecasts per target; yearly seasonality not before late 2027; Hawkes or INAR diagnostics after six months of stable protocols.

## 5. Decisions Gamma will need from Albert (none due now)

| # | Decision | When | Recommendation |
|---|---|---|---|
| D1 | Run the read-only history audit on his PC (one command, given then, with its expected output) | After Beta is accepted | Yes: at most about US$0.13 of BigQuery reads, aggregates only, safe to paste back |
| D2 | Switch on statistical tests for the series that pass the existing backtest gate | After G2, from about early November | Yes for passing series only; failing series stay `none` with the reason shown |
| D3 | Storage release for the clean-wave views | Before G3 merges | Yes, additive views only, with old and new readers rehearsed and a rollback target |
| D4 | Five strategists for the 30-minute E7 prototype test (two of them can also label E4 transitions) | Any time before G4; the earlier, the earlier E7 runs | Yes; it is the only usefulness evidence the plan has before release |

Gamma's own release approval comes separately at G7.
