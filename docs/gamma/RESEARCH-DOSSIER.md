# Gamma 3.1 research dossier (output 1)

Prepared 16 October 2026. This is the synthesis; the evidence is in four appendices in this folder:

| Appendix | What it holds | Basis |
|---|---|---|
| [research/METHODS-DOSSIER.md](research/METHODS-DOSSIER.md) | Methods for studying cultural change from history: about 28 methods across 10 questions, each with status (established or experimental), data needed, cost at 42's scale, baseline, failure modes and verdict; the evaluation protocol; 77 numbered sources | about 60 searches and fetches; every source opened or shown in results; unconfirmed papers dropped and listed |
| [research/DESIGN-RESEARCH.md](research/DESIGN-RESEARCH.md) | About 55 interface patterns from AI research tools, trend and social intelligence products, data journalism, uncertainty and explainable-AI research, provenance tools, mobile reading and accessibility; three directions; 20 anti-patterns; 62 sources | about 47 searches and fetches; unverified items marked |
| [research/PRODUCT-REVIEW.md](research/PRODUCT-REVIEW.md) and `research/screens/` | Review of the current product: route map, journey friction ranked by severity with file:line evidence, measured performance and accessibility, what to keep | the real `core/api` backend run locally in fixture mode with the built frontend; 15 screens at 1440x900 and 390x844; throttled phone profile |
| [HISTORICAL-DATA-ASSESSMENT.md](HISTORICAL-DATA-ASSESSMENT.md) | What history 42 holds, its breaks, gaps, identity drift and measurement consistency, and a verdict per scientific use | code, SQL, git history and recorded evidence; no warehouse query (no credentials in the cloud) |

The brief asked to start with /brainstorm; that command is not available in this environment, so discovery started from four parallel investigations (data, methods, design, current product) before any feature was proposed.

## 1. Findings: intelligence track

1. **History length and consistency bind, not compute.** Every method worth considering runs in seconds to minutes at 42's scale. With 3 to 8 clean days on most current protocols (HISTORICAL-DATA-ASSESSMENT section 2), nothing that needs a 14 to 28 day outcome label can be scored yet. The right move now is to freeze labels and protocol and let data accrue (METHODS section (a), opening).
2. **The simplest wave descriptors are the baselines to beat for lasting versus spike.** Peak-day share (Crane and Sornette 2008, about 5 million YouTube videos), activity before, at and after the peak (Lehmann et al. 2012, 402 hashtags), retention after the peak, days present and creator persistence. Literature finds most attention bursts are exogenous and fade fast (Asur et al. on Twitter trends; Lehmann et al.), which is why 42's decision to stop publishing pure event reactions (04bc5cc) is consistent with the evidence.
3. **Participation beats volume as a measure of behaviour change,** provided diversity is compared at equal sample coverage (Chao and Jost 2012; Hill numbers). Early breadth predicts reach better than early depth where follower graphs exist (Cheng et al. 2014; Weng, Menczer and Ahn 2013). 42 has no follower graph, so breadth is tested as a proxy, not assumed.
4. **Measurement change masquerades as cultural change.** The Google Flu Trends parable (Lazer et al. 2014), platform sampling bias (Morstatter et al. 2013; Ruths and Pfeffer 2014; Tufekci 2014) and 42's own record (8 to 13 October protocol restarts, a Kenya feed about 12% in-country) all point to the same guard: a registry of protocol breaks plus changepoint detection on collection health, with share-of-lane denominators, placebo items and negative controls.
5. **Analogues are useful only as a distribution of real past outcomes,** and only when the library is large and close (Lorenz's analogue warning; analog ensembles in meteorology; quantile dotplots in Kay et al. 2016). 42 has no resolved library yet.
6. **Simple baselines are hard to beat on short histories** (M3 and M4 forecasting competitions; M5 favoured machine learning only with long histories). 42's own record agrees: the old ARIMA forecast lost to "today holds" (DATA.md section 6).
7. **The existing code already contains most of the evaluation machinery** Gamma needs: replay by availability date and injected spikes (`core/detect/backtest.py`), NULL-not-zero series (`v_series_daily`), a placebo lane, forecast cohorts and Brier scoring against persistence, identity evaluation and replay. Gamma reuses these rather than rebuilding them.

## 2. Findings: design track

1. **Trust comes from one click between a claim and its passage,** not from a polished summary. Elicit, NotebookLM and scite show this; the Tow Center found AI search tools answered more than 60% of news-citation queries wrongly despite showing citations. 42's Ask anatomy already does this best of all its screens (PRODUCT-REVIEW, keep list).
2. **Absence must be drawn, and drawn differently from zero.** Charts that highlight missing values are judged higher quality and support better trend judgements than charts that drop them (Song and Szafir 2018). 42 needs three marks: present, collected and absent, not collected.
3. **Stating uncertainty barely costs trust in the source** (van der Bles et al. 2020, n = 5,780), and fixed vocabularies matter because readers spread "likely" from 55% to 90%. 42's four confidence words should stay fixed, visible and separate from any likelihood statement (the IPCC separation of confidence and likelihood).
4. **Quantile dotplots of real outcomes** improve probability judgements and decisions over intervals or densities (Kay et al. 2016; Fernandes et al. 2018), which makes them the right future shape for analogues.
5. **The current product's biggest failures are about honesty and consistency, not style** (PRODUCT-REVIEW S1 items): the empty day reads as failure; Today runs its own second gate and hid 6 of 7 server-cleared fixture trends; Today and Discover disagree on what is a trend; Discover shows "Rising" on warm-up day 2 of 14; the History analogue chart contradicts its caption; Coverage drops sample sizes. Performance is the other real problem: first paint about 6.9 s on a throttled phone because rendering waits for 702 KB of Recursive (`main.jsx:43`).
6. **Recommended direction: "Field Notebook".** Every object shows its finding, then its record (counts, days present, window, n, confidence), then its specimens (posts), with interpretation after the record. Uncertainty is encoded by mark fill and words, not colour; Recursive's monospace axis carries every reading; Today keeps one Newsreader finding sentence. The two alternatives, "Morning Paper" (editorial) and "Culture Atlas" (spatial map), are shown in the prototype's Concepts view with their trade-offs.

## 3. Limitations

- No warehouse fact after 28 September was verified from the cloud. The SocialCrawl start date, valid-day counts and test-switch state are inferred from code and records until the read-only audit runs (decision D1 in the plan).
- Git history in this repo starts on 6 October; changes from 28 September to 5 October are known only from the knowledge export.
- The product review used fixture data; it shows behaviour of the code, not of a live morning. Thumbnails were blocked by the review setup.
- Some design references (Observable, Raycast, Miro, Notion, FT and Bloomberg apps, specific NYT methodology pages) were not source-checked and are marked in the appendix. Vendor capability claims (Pulsar, Glimpse, WGSN, Elicit) rest on vendors' own pages.
- The eight identity replay labels in 42-beta are provisional model-assisted judgments and are not used here as an accuracy rate.
- No strategist has yet used the prototype. E7 in the plan is the first usefulness evidence.

## 4. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Deep cascade or graph neural network predictors | Need reshare cascades and a follower graph 42 does not have; even with full data, cascade size R² stays below 0.5 (Martin et al. 2016) |
| LSTM or other deep forecasting | Weeks of data; machine learning lost to simple methods in M3 and M4 and won M5 only with long histories |
| Granger causality, transfer entropy or PCMCI as influence claims between platforms or markets | Unequal pull cadence, changing coverage and common external drivers (about 29% of URL volume external even on full Twitter, Myers et al. 2012) produce artefacts; allowed only as an internal diagnostic |
| Hawkes "endogenous versus exogenous" labels | Missing events badly bias Hawkes estimates; 42 sees samples, not streams |
| Dynamic topic models; BERTopic refits to assign identity | Short, code-switched posts, fixed K, and refits renumber topics |
| STL or Prophet yearly seasonality | Needs at least two years; Seasonal stays calendar-driven |
| Unconstrained DTW for analogues | Warping removes the timing that lead time depends on |
| Public forecasts or cones | Not before a forecast beats persistence on 200 resolved cases per target |
| Vendor accuracy claims (Glimpse, WGSN, Exploding Topics) as benchmarks | None states a metric, horizon or backtest |
| "Culture Atlas" as the main interface | Implies distances and relationships 42 cannot measure; weakest for accessibility and mobile |
| Scores and indices from 0 to 100, growth percentages on small bases, sentiment pies, word clouds, choropleths, force graphs, KPI tile grids | Imply measurements 42 does not have (DESIGN-RESEARCH section 3) |
