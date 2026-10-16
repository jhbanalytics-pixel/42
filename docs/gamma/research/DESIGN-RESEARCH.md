# 42 Gamma 3.1: design research

Interfaces for intelligence, research and analysis, the evidence behind them, and three directions for Gamma

Prepared 16 October 2026 for the Gamma 3.1 team. Input: `docs/full-42/EXPERIENCE.md` (screens, anatomy of an answer, Today card, glossary), the brief from Albert, and roughly 47 web searches and fetches. Numbers in square brackets refer to the source list at the end. Where a claim rests only on vendor material or secondary summaries, it says so. Where this pass could not verify something, it is marked **[not verified in this pass]**.

---

## 0. The constraint that decides most of this

42's data is thin and biased. It has weeks of history, not years. Locality is uncertain. Platform coverage is uneven, and the sample is whatever the collectors reached. Three consequences shape every recommendation below.

1. **Most of the time the honest unit is a count with a denominator, not an index.** "31 creators, 3 days, 2.8 times usual" can be checked. "Trend score 87" cannot. Google Trends shows how much explaining a normalised index needs, and it still gets misread [29].
2. **An absence has to be drawn, and drawn differently from a zero.** "We collected Kenya on X and found nothing" is not the same fact as "we did not collect Kenya on X that day". Song and Szafir found that charts which *highlight* missing values are perceived as higher quality, while charts that silently drop them lower confidence and lead to wrong trend judgements [6][7][8].
3. **Uncertainty language must be calibrated and fixed.** People read "likely" as anything from 55% to 90% [15]. In 1951 Kent's colleagues read "serious possibility" as anything from 20% to 80% [13][14]. 42's four confidence words (Observed, Corroborated, Single source, Inferred) are already a calibrated vocabulary. The design job is to keep them visible, consistent and impossible to hide behind a confident layout.

The research also argues against fearing that uncertainty costs trust. In van der Bles et al. (five UK experiments, n = 5,780), stating a numerical range lowered confidence *in the number* but barely lowered trust *in the source* [19]. Google PAIR makes the same point for AI products: flagging that a prediction may be wrong lowers trust in that prediction but builds appropriate reliance over time [23].

---

## 1. Pattern catalogue

Fit is judged against 42's users (agency strategists in ZA, NG and KE who find, understand, inspect, question, compare and save into client dossiers) and 42's data (thin, biased, short history).

### 1a. AI research and answer products

| Pattern | Source | Evidence or rationale | Fit for 42 | Risk if misapplied |
|---|---|---|---|---|
| Numbered inline citations mapping to source cards | Perplexity answers and Pages [45] | A claim sits next to its source. A shareable Page keeps the citations. | Partial. 42's evidence chips (platform mark, handle, +N) carry more than a number does, so keep the chips. | Citation theatre: a footnote looks verified without being checked. The Tow Center found AI search tools gave wrong answers on more than 60% of news-citation queries; Perplexity had the lowest error rate at 37% [47]. A citation is only as good as the check behind it. |
| Citation opens the exact passage in the source | NotebookLM [43] | Hover previews the quoted text; click opens the source at that spot. Answers stay inside a closed set of sources the user chose. | Strong. Matches 42's chip popover (thumbnail, highlighted words, date, views) and the pinned SourcePanel. For video, the equivalent of the passage is the transcript line with its timestamp. | Users may read "grounded in sources" as "true". NotebookLM citations do not verify accuracy, only where text came from [43]. |
| Every extracted cell carries its supporting quote | Elicit systematic review [42] | Clicking any cell of the extraction table shows the supporting quote, so users can check the AI's answer. | Strong for Compare and Dossiers: each cell of a comparison should open the post or posts behind it. | Quote shown but not read. Make the quote one keystroke away, not a hover that is easy to skip. |
| Typed citation context: supporting, contrasting, mentioning | scite [40] | Plain citation counts treat support and contradiction alike. scite classifies each citing sentence. Reported accuracy: mentioning 97%, supporting 64%, contrasting 59% [40]. | Strong idea, cautious use. 42 already has "Sources disagree". Make disagreement first class: a claim shows how many posts support it and how many contradict it. | Automatic typing is far less accurate for the categories that matter. Librarians warn about oversimplification [40]. Treat model-assigned "contradicts" as Inferred until a human checks it. |
| Aggregated verdict meter (yes, no, possibly) | Consensus Meter [41] | Uses the top 20 results and excludes papers the classifier is unsure about. Consensus itself documents the guardrails. | Poor. A meter over a thin, biased sample is vote-counting that looks like measurement. | The meter becomes the answer and the sample disappears. Consensus's own notes say it is not a weighted synthesis, and 20 papers may not represent a field [41]. |
| Coverage estimate: "found ~93% of relevant papers after reading 144" | Undermind discovery curve [46] | Models how the rate of new relevant finds decays and states an estimated completeness. An independent library benchmark put real recall at 30 to 80% depending on topic [46]. | Inspiring but dangerous. The *shape* is right: say how exhaustive the search was. 42 cannot estimate total relevant posts honestly. It can state what it read: "Read 312 of 312 TikTok posts matching #amapiano in ZA, 21 to 27 Sept; X not collected this week." | A completeness percentage over a biased crawl would be false precision, because the vendor's 93% and the independent 30 to 80% do not agree. |
| Plan first, then a visible process log, then a report with citations | OpenAI deep research [44] | In Chat it proposes an editable research plan; other surfaces ask clarifying questions. A sidebar shows the steps, searches and sites visited. | Strong. 42 already specifies this for Investigations (plan, credits, Edit plan, Start) and Ask (research log with counts). Keep it. | A log can become performative noise. Every line needs a count and a target, and it collapses afterwards ("How this was researched · 6 steps"), as EXPERIENCE.md already says. |
| Work surface beside the conversation (artifact or canvas) | Claude artifacts, ChatGPT canvas [48] | The output becomes an object that can be edited, versioned and shared, separate from the chat that produced it. | Strong for Dossiers: the answer is a draft object that assembles itself and can be ticked, approved, frozen and exported. | Two competing surfaces on small screens. On mobile the dossier should be a destination, not a split pane. |

### 1b. Trend and social intelligence

| Pattern | Source | Evidence or rationale | Fit for 42 | Risk if misapplied |
|---|---|---|---|---|
| Normalised 0 to 100 index, with comparison of up to five terms | Google Trends [29] | 100 is the peak inside the chosen window and region. A sample is used and re-normalised on every request. 0 can mean "insufficient data". A falling line means falling *relative* popularity, not necessarily falling searches. | The comparison is good (the same window and units, which Compare already requires). The index is poor for 42. | The best-known example of an honest chart that is misread anyway. Its disclaimers live in a FAQ that no one reads. If 42 normalises anything, the label has to sit on the chart. |
| Absolute volume overlaid on a relative chart, plus a forecast line | Glimpse [31] | Glimpse sells the missing piece: real volumes and a 12-month forecast. Its accuracy claims (87%, or above 95%) are vendor figures that do not agree with each other [31]. | Poor. 42 has weeks of history; a 12-month forecast would be invention. | Forecast lines carry authority they have not earned. |
| Topic card: name, sparkline, volume, growth %, state word (Exploding, Peaked, Regular) | Exploding Topics [30] | Fast to scan, and the state word does the interpretation. A model finds the candidates and analysts review them. | Strong shape, already in Today's card (state word, count line, sparkline). Keep 42's state from ENGINE.md and its rule of one state per card. | Growth % on a tiny base ("+900%" from 3 to 30 posts). Always show the base: "3 to 30 posts". |
| Platform charts: hashtags, songs and creators by region and industry; "New to Top 100"; Popular vs Breakout | TikTok Creative Center [32] | Platform-owned ranked lists. They include only commercially licensed sounds and Creator Marketplace creators [32]. | Good as context, and 42 already plans "On the boards today, labelled as the platforms' own lists". | Passing a platform's commercial list off as cultural truth. User-generated sounds, which drive much of SA and NG culture, are excluded [32]. |
| Audience communities segmented from the conversation, network graph (2D or 3D), export to Gephi | Pulsar TRAC [33] | "How different communities talk about the same topic". The graph updates with filters; the vendor pitches 3D as making clusters clearer [33]. | The communities idea is strong for 42's "communities" job. The 3D graph is weak. | Decorative network views. Force layouts put things near each other for reasons the reader cannot see. Clusters from thin data are mostly noise. Export to analysis tools is the honest part. |
| Sentiment pies and share-of-voice | Brandwatch, Meltwater, Talkwalker [34][35][36] | Standard in social listening. Users report sentiment misclassification, worse outside English, and conversation sentiment attributed to the brand [34]. Brandwatch says it prioritises precision over coverage [34]. | Poor for SA, NG and KE: code-switching, Pidgin, Sheng and isiZulu slang lower accuracy further (inference from [34]; not tested here). | A confident pie chart over unreliable classification. |
| Spike detection with a plain-language "why" and a drill-down to mentions | Meltwater [35] | An AI insight on the trend widget, alerts by email, Slack and in-app, linking straight to the saved search and date range [35]. | Strong for Alerts and Today: a spike always links to the posts behind it. | The "why" is generated text and sounds causal. 42 already hedges only the why-now clause; keep that rule. |
| Value-equivalence metrics (AVE, EMV) | PR and listening tools [38] | Rejected by AMEC's Barcelona Principles since 2010. EMV formulas are not comparable between vendors [38]. | Never. | A cautionary tale for any composite "impact score" 42 might invent. |
| Analyst-led macro forecast with a stated method; "analyst in the loop" | WGSN [37] | Five-stage method, more than 2,800 sources, triangulated with AI, and analyst sense-checking. It claims "high accuracy" but publishes no figure [37]. | The tone suits strategists: a forecast as an argued story. 42's planned track-record chip ("74% of last month's Rising calls held a week later", only after four weeks of scored data) goes further than WGSN publicly does. | Authority without a track record. 42's advantage is publishing its own hit rate. |
| Personal-data storytelling: one fact per card, shareable, celebratory | Spotify Wrapped [39] | A narrative sequence where each card can be shared on its own. Critics note flattering framing ("top 1% of listeners") and that bright animation hides data collection [39]. | The card that can be shared is useful for the dossier export (one finding with its proof, for a client slide). The flattery is not. | Superlatives with no denominator. |

### 1c. Analysis and data storytelling

| Pattern | Source | Evidence or rationale | Fit for 42 | Risk if misapplied |
|---|---|---|---|---|
| Chart, then source, then caveat, built into the chart: Chart, Table and Sources tabs, "What you should know about this indicator", "with major processing by OWID" | Our World in Data [49] | The source and the processing note travel with the chart, with a citation line, a retrieval date and a download [49]. | Very strong. Every 42 figure should carry its window, market, platforms, n and processing note in a footer, and the footer should survive export into a dossier. | Caveats hidden behind tabs that no one opens. The single most important caveat should sit under the chart. |
| Choose the chart by the relation to show: deviation, correlation, ranking, distribution, change over time, magnitude, part-to-whole, spatial, flow | FT Visual Vocabulary [50] | A shared vocabulary from the FT graphics desk. It warns that readers assume correlation means causation [50]. | Strong as a discipline. For 42 the honest categories are mostly ranking, change over time (short windows) and distribution of analogues. The spatial and flow categories are mostly off limits until locality and spread can be measured. | Choosing "spatial" because a map looks impressive. |
| Show uncertainty live, and the cost of doing it badly | NYT Upshot election needle [22] | The 2016 needle jittered to show uncertainty. Many readers hated it; the Upshot argued that removing the jitter hid uncertainty, then turned it off in 2017 [22]. | Lesson rather than pattern: motion that encodes uncertainty causes anxiety when the stakes feel high. A "methodology" or "how we did this" box is good practice **[not verified in this pass for specific Upshot pages]**. | Meaningful animation still feels like noise. |
| Missing dates: break the line, or bridge it and mark the filled points | Datawrapper [51] | Datawrapper detects gaps and offers to connect them. It recommends point markers on bridged segments, because a gap might have hidden an outlier [51]. | Strong. 42 sparklines must break, or be hatched, on days not collected. | Smooth interpolation over gap days creates trends that were never there. |
| Show uncertainty only when it changes the reading; keep the full estimates downloadable | ONS service manual [20] | Government guidance on uncertainty in charts [20]. | Strong. It matches "at most one confidence label per card" and "Details" holding the method. | Over-applied, it becomes a reason to hide uncertainty. 42's rule should be that a card is never shown without its confidence word. |
| Visual essays: scroll-led and explanatory, with methods and sources stated openly | The Pudding [52] | Data essays with fewer words and an open method. Each takes days or months to build [52]. | Good for an occasional "story of one thing" topic page. | Too slow and bespoke to produce every morning; scroll-jacking. |
| Notebook-style live analysis (cells, reactive charts) | Observable **[not verified in this pass]** | Analysis as a readable document whose method is the content. | Useful in spirit for Investigations: the findings pack can show the steps that produced each number. | Exposes too much machinery to strategists. |
| Extreme density for experts; hide complexity without removing it; keep it consistent | Bloomberg Terminal [53] | Bloomberg says it "hides complexity" and treats even small layout changes as disruptive for 350,000 daily users [53]. Matt Stromberg's essay on density uses the Terminal as its example [53]. | Partial. Strategists are not traders, but a dense Coverage and Compare view earns its place. Stromberg's distinction between density of value and density of pixels is the useful idea. | Dense terminal styling as decoration (green on black, tickers) puts the look of rigour ahead of rigour. |
| Command palette (Cmd+K), single-key shortcuts shown in menus, a calm dense list, row actions revealed on focus | Linear (third-party analyses) [54]; Raycast **[not verified in this pass]** | Shortcuts are learnable because they appear next to the commands. A minimal resting state uses progressive disclosure [54]. | Strong for 42 on desktop: "Ask about this", "Add to dossier", "Watch", "Compare with" and market switching from the keyboard. | Does not transfer to mobile [54]. A Linear-style dark UI with a purple accent has become a template, which is the "generic" look Albert wants to avoid. |
| Collecting as connection: blocks, channels, a block in many channels, no likes | Are.na [55] | Calm, library-like collecting. A block can live in several channels; channels can nest [55]. | Strong model for Dossiers: one post or claim can belong to several client dossiers without copying, and the dossier keeps provenance. | Dossiers that are only bookmarks. 42's dossier has review, approval and frozen versions, so keep that rigour. |
| Infinite canvas for synthesis | Miro, Notion **[not verified in this pass]** | Freeform arrangement for workshops. | Weak for the core product; strategists already have these tools. Export to them. | A canvas inside 42 becomes the place where evidence loses its citations. |

### 1d. Uncertainty, trust and explainable-AI research

| Pattern | Source | Evidence | Fit for 42 | Risk |
|---|---|---|---|---|
| Quantile dotplot: about 20 dots, each an equal share of the outcome distribution | Kay et al. 2016 [1]; Fernandes et al. 2018 [2] | On phones, quantile dotplots cut the variance of probability estimates by about 1.15 times compared with density plots and increased confidence [1]. In a follow-up test of real decisions they gave better decisions; CDF plots were nearly as good [2]. | Very strong when each dot is a **real past analogue**: "Of 20 earlier items that looked like this on day 3, 11 were still on Today by day 14." Each dot can open the analogue it stands for. That is honest because it is counting. | With fewer than about 10 analogues the plot looks precise and is not. Below that threshold, say "Not enough history: 4 analogues" and list them. |
| Hypothetical outcome plots (HOPs): animated draws | Hullman et al. 2015 [3]; Kale et al. 2018 [4] | More accurate than error bars or violins for comparing two or three quantities [3]. Untrained viewers inferred trends from weaker evidence [4]. Stephen Few disputed the 2015 study in a blog post [5]. | Limited. Animation conflicts with reduced motion and calm reading. It could be a static grid of hypothetical outcomes. | Jitter that reads as live data, which is the needle problem [22]. |
| People read area, width and boundaries literally | Hurricane cone studies (Ruginski 2016, Boone et al. 2018) [21] | Viewers read the cone's widening as the storm growing, and its edge as a safety boundary [21]. | A warning for any band or cone. 42's "expected band" on sparklines must be labelled as the range of usual days. | A band becomes "safe" or "normal" territory; cones imply forecasting. |
| Visualise missing data on purpose | Song and Szafir 2018/19 [6]; Song, Fu, Saket, Stasko [7]; survey [8]; Sarma et al. 2022 [9] | Highlighting missing values raises perceived quality. Breaking continuity lowers it. Silently removing data degrades confidence and accuracy [6][7]. Uncertainty displays on imputed values reduce bias but make people less certain [9]. | Core. 42 needs a dedicated mark for "not collected", separate from "collected, none found". | Hatching everywhere. Use it only where it changes the reading. |
| A cognitive framework: visuals direct bottom-up attention, encoding techniques create visual-spatial biases, and cognitive fit matters | Padilla et al. 2018 [10]; uncertainty survey [12] | Fast, type 1 reading is driven by salience. Make the salient thing the right thing [10]. | Strong. The most salient mark on a card must not be the most uncertain number. | A big confident number takes over fast reading. |
| Authors omit uncertainty for rhetorical reasons | Hullman 2020 [11] | 90 surveyed authors and 13 interviewed designers value uncertainty but usually leave it out [11]. | An organisational risk: under deadline, Gamma's designers and engineers will be tempted to drop "Single source" labels. Make the label part of the component, not a prop that can be turned off. | |
| Calibrated estimative words tied to ranges | Kent 1964 [13][14]; IPCC AR5 guidance note [16]; Mauboussin [15] | Kent: almost certain about 93%, probable about 75%, chances about even 50%, probably not about 30%, almost certainly not about 7% [14]. IPCC separates *likelihood* (likely means 66 to 100%, very likely 90 to 100%, about as likely as not 33 to 66%) from *confidence*, which combines amount of evidence with agreement [16] (ranges from the AR5 note, which returned 403 to the fetcher; the values are the standard published ones). | Very strong. 42's four labels are a *confidence* scale based on evidence type (IPCC-like). Keep them separate from any *likelihood* judgement ("likely to hold a week"), which should appear only with a track record. | Mixing the two scales on one card ("Corroborated, likely") will confuse readers. |
| Intelligent transparency: accessible, intelligible, usable, assessable; be confident about what is uncertain | Spiegelhalter [17][18]; van der Bles et al. 2020 [19] | Numerical ranges barely lowered trust in the source [19]. | Strong: "What we do not know" belongs in the main answer, not at the bottom. | |
| Calibrate trust: say what the system can do and how well; explain why; support correction | PAIR Guidebook [23]; Microsoft HAX guidelines (18 guidelines over four phases: initially, during interaction, when wrong, over time) [24] | Validated over four rounds [24]. | Strong: the track-record chip (over time), "Held back" with reasons (when wrong), the method in Details (explain why), one-tap Real / Not real (feedback). | Feedback that looks as if it trains the system when it does not. 42 already says the taps never feed trust numbers; say so in the interface as well. |
| Skeleton screens for whole-page loads; spinners and progress bars for others | NN/g [25] | Skeletons reduce perceived wait for full-page loads only [25]. | Good for Today at 06:30. For Ask, the research log with counts is better than a skeleton because it is real progress. | A skeleton for a 40-second agent run is a lie about how close the answer is. |
| Progressive disclosure | NN/g [26] | Show the main options by default and defer secondary ones; this makes products easier to learn and less error-prone [26]. | Strong: card, then answer, then evidence, then method. | Hiding the caveat behind a disclosure. Caveats stay at the top level, method goes in Details. |
| Dashboards are for glancing, not exploring; use preattentive features | NN/g [27] | Separates operational from analytical dashboards; length and 2D position are best for quantities [27]. | Today is a brief, not a dashboard. Coverage could be a dashboard. | A grid of KPI tiles is exactly the generic template to avoid. |
| Designed empty states: say what the state is, why it is empty, and what to do | NN/g [28]; SAP, Pega via [28] | Blank space lowers confidence. An empty state must distinguish "no content" from "failed to load" [28]. | Core. "First morning: nothing to compare yet" and "Thin coverage" are 42's best empty states. | |

### 1e. Provenance and evidence

| Pattern | Source | Evidence | Fit for 42 | Risk |
|---|---|---|---|---|
| "About this result": why the result matched, when the source was first indexed, Wikipedia description, others about the source; a bottom sheet on mobile | Google [56] | Rolled out in 2021 with matched terms and region signals [56]. | Strong as "About this post" in the source panel: first seen by 42, locality confidence and why, independence from other sources, whether paid or coordinated flags were checked. | |
| Events on a time-and-place canvas, filterable by tag, each event backed by sources | Forensic Architecture TimeMap, used by Bellingcat [57] | An open-source tool built from investigation work [57]. | Strong for Spread (Stage 2): "First seen on TikTok 12 Sept, X 18 Sept, news 24 Sept" as a source-backed timeline. The map half only when locality is confirmed. | A map of Africa with uncertain pins implies geographic knowledge 42 does not have. |
| A marked gap: "citation needed" versus "dubious" | Wikipedia [58] | An unsupported claim is marked where it sits. "Dubious" is a separate tag. Overuse ("drive-by tagging") is a known problem [58]. Redi et al. built a taxonomy of why sentences need citations [58]. | Strong: "Inferred" claims carry an inline mark and a reason. Dossier approval already requires a tick on Single source and Inferred claims. | Marks so frequent that readers stop seeing them. |
| Link analysis graphs (Palantir-style) | Pulsar network view [33] as a comparable | Network views are useful when edges are real, typed and dated. | Use only lineage: A derived from B, with the evidence for the derivation. No force-directed "culture graph". | Decorative graphs: proximity without meaning, and clusters from noise. |

### 1f. Mobile reading of dense intelligence

| Pattern | Source | Evidence | Fit for 42 | Risk |
|---|---|---|---|---|
| Headline, then "Why it matters", then "Go deeper", in fixed labelled sections | Axios Smart Brevity [59] | Readers choose their depth; the labelled "why it matters" is the centre of most items [59]. Criticised as formulaic, and "Go deeper" links often lead only to more Axios [59]. | Very strong for Today on mobile and for dossier exports. It matches the card: finding, why it matters, proof, Ask about this. | Brevity that drops the hedge. The confidence word must survive the cut. |
| FT app and Bloomberg app (myFT digest, single-column briefs, market tickers) | **[not verified in this pass]** | A single column, one story at a time, a persistent market selector. | Good: one market per screen on mobile, swiped or tabbed. | Tickers that create urgency without information. |

### 1g. Accessibility (WCAG 2.2 AA and colour)

| Criterion or practice | Source | What it means for 42 |
|---|---|---|
| 2.4.7 Focus Visible (AA) and 2.4.11 Focus Not Obscured, Minimum (AA, new in 2.2) | [60] | The sticky rail, Dossier tray and pinned SourcePanel must never fully cover the focused chip. |
| 2.5.8 Target Size, Minimum (AA): 24 × 24 CSS px, or enough spacing | [60] | Evidence chips and days-present dots need a 24 px hit area, even when the visible dot is 6 px. |
| 1.4.10 Reflow (AA): 320 CSS px wide with no two-dimensional scrolling (data tables are exempt) | [60] | Compare tables may scroll sideways; everything else may not. |
| 1.4.11 Non-text Contrast (AA): 3:1 for meaningful graphics and component boundaries | [60] | Hatching, hollow dots and dashed lines that encode uncertainty need 3:1 against the background in both themes. Pale grey "inferred" lines usually fail. |
| 1.4.1 Use of Color (A) | WCAG (via [60]) | Confidence, market and state are never shown by colour alone. EXPERIENCE.md already asks for a shape that survives greyscale. |
| 2.3.3 Animation from Interactions (AAA, recommended); `prefers-reduced-motion: reduce` | [60][61] | Treat it as a requirement. Under reduced motion, the research log appends with no slide, and dotplots never animate. |
| Colour-blind-safe palettes: Okabe-Ito for categories, viridis or cividis for sequences | [62] (secondary) | Use only if colour encodes category, and always with a label. |

---

## 2. Three directions for Gamma

Shared ground for all three: Newsreader for one moment per screen; Recursive at 400 and 600; four sizes (13, 16, 21, 32 px); Ogilvy red for the single primary action only; light default, dark and system themes; the four confidence words, each with a fixed shape; and an explicit mark for "not collected".

A useful extension in all three: **Recursive is a variable font with a Monospace axis (MONO 0 to 1) and a Casual axis (CASL 0 to 1)**. Setting MONO to 1 for counts, dates, timestamps, handles and run ids gives 42 an "instrument" voice without adding a typeface or a size. This comes from practitioner knowledge of the font **[not verified in this pass]**; check that the axes exist in the vendored design-system build.

### Direction A: "Morning Paper" (the editorial intelligence paper)

**Idea.** 42 as a well-edited daily, per market. A front page, an argued finding, figures in sentences, and proof in footnotes that open. Draws on Axios [59], OWID [49], the FT Visual Vocabulary [50] and The Pudding [52].

- **Hierarchy.** Date line and market at 13 px in Recursive Mono, then the 32 px heading "Taking off, 28 September 2026", then one Newsreader 21 px finding sentence, then ranked items as short articles: state word, title, "Why it matters", count line, two thumbnails, "Ask about this". Below the fold: Dropped, Held back, Moments, Boards, Coverage, laid out like a newspaper's corrections and notices column.
- **Type.** The most Newsreader-heavy option: the finding sentence on Today and the short answer in Ask. Everything else in Recursive. Labels in small caps (Recursive 600, 13 px, tracked).
- **Colour.** Near-monochrome on warm paper (light) or ink (dark). Market shown by a word and position, not colour. Confidence by the word plus its shape (●, ●● for Corroborated, ○ for Single source, ◌ dashed for Inferred). "Not collected" is a hatched grey cell. Red only on "Add to dossier".
- **Layout.** Desktop: one wide reading column (about 680 px) with a margin column for evidence chips, tufte-style sidenotes that pin open into the SourcePanel. Mobile: one column; the margin notes become numbered chips that open a bottom sheet, as Google's panel does [56].
- **Motion.** Almost none. Disclosures expand in place (150 ms, none under reduced motion).
- **Charts.** Inline word-sized sparklines with gaps broken; figures written as sentences ("12,400 mentions, up 38% on last week, 5 of 7 days collected"); ranked lists; small multiples per market under one shared axis as a "same story, three markets" strip.
- **Evidence.** Sidenotes and footnotes; a "What we do not know" box styled like an editor's note, placed above the actions.
- **Trade-offs.** + Fastest to read, closest to how strategists already write for clients, exports naturally into dossiers and PDFs. + Distinctive against dashboard competitors. − Prose authority can hide thinness: a well-set paragraph reads as more certain than its evidence. − Weak for exploration, Compare and Coverage. − Least "futuristic" of the three; it risks reading as nostalgic.

### Direction B: "Field Notebook" (the instrument and evidence-first workspace)

**Idea.** 42 as an observer's notebook with instruments: every finding is a specimen with its collection record. The interface shows what was observed, when, where, how many, and what was not observed, before it shows the interpretation. Draws on Elicit cells with quotes [42], NotebookLM passage citations [43], scite typed context [40], OWID's source tab [49], Song and Szafir on missing data [6], Kay's quantile dotplots of real analogues [1][2], Linear's keyboard model [54] and Are.na collecting [55].

- **Hierarchy.** Every object (a trend card, a claim, a figure) has three layers in a fixed order: the **finding** (one line); the **record** (count line, days-present strip, platforms, market, window, n, confidence word); and the **specimens** (posts). Interpretation (what it means, what we would do) follows the record, never comes before it. Today's heading and its single Newsreader sentence keep the editorial moment.
- **Type.** Recursive does most of the work. Recursive Mono 400 at 13 px for every number, date, handle and timestamp, so instrument readings look different from prose. Recursive Sans 600 for titles. Newsreader 21 px only for the finding sentence and the short answer. This is the most distinctive and "modern" option without becoming a cliché.
- **Colour.** A neutral graphite and paper base. Uncertainty is encoded by **mark fill, not hue**: filled = Observed; filled with a second ring = Corroborated; hollow = Single source; dashed outline = Inferred. **Coverage**: hatched = not collected; empty tick = collected, none found; filled = present. **Market**: a fixed row order ZA, NG, KE in small multiples, with a two-letter label; optional single desaturated hue per market from an Okabe-Ito triple, always labelled [62]. One functional accent (cool blue-grey) for focus and selection; red only for the primary action. All uncertainty marks at 3:1 or better [60].
- **Layout.** Desktop: a three-zone workspace. A left rail; centre "notebook" (cards or the answer); right "specimen tray", the pinned SourcePanel, where posts open with the quoted words or transcript span highlighted and an "About this post" block (first seen by 42, locality confidence and basis, independence, flags checked) [56]. Cmd+K palette for Ask, Watch, Compare with and Add to dossier, with single-key shortcuts listed in menus [54]. Mobile: a single column; the specimen tray becomes a full-height sheet; days-present strips stay (they are only 14 to 28 small dots wide); actions sit in a bottom bar with 24 px or larger targets [60].
- **Motion.** Purposeful only. Research log lines append with their count incrementing; claims switch from "checking" to their label with a 120 ms fade; the tray slides in at 160 ms. No pulsing "live" dots, no chart draw-in. Everything is instant under reduced motion [61].
- **Charts (the honest set).**
  - **Days-present strip**: one dot per day in the observation window; filled when the item was present, an empty tick when collected and absent, hatched when not collected. It replaces most sparklines on Today and works in greyscale.
  - **Count line with a band**: a sparkline of daily counts where gaps break the line [51], the "usual range" band is labelled as such (not "normal" or "safe") [21], and the base count is always printed.
  - **Ranked list with rank movement in words** ("Moved up · was 4th").
  - **Small multiples per market** on a shared scale and window, with "Not collected" panels shown rather than dropped.
  - **Analogue dotplot** (quantile-dotplot style): 20 dots, each a real earlier item at the same age and state, coloured by what happened next (held, faded, became seasonal). It is shown only when 10 or more analogues exist; otherwise it reads "4 analogues so far" with the list [1][2].
  - **Lineage line** on topic pages: a dated, vertical sequence of derivations (sound, then format, then remix, then brand use), each step with its evidence chip.
  - **Evidence balance** on claims: "9 posts support · 2 contradict · 1 unclear", with each group openable (scite-style [40]); model-assigned contradictions are labelled Inferred.
  - **Avoid**: indices from 0 to 100, gauges, sentiment pies, word clouds, choropleth maps, force graphs, forecast cones, stacked share-of-voice, any "score".
- **Evidence.** First class: chips, tray, passage-level highlights, transcript timestamps, typed support or contradiction, "About this post", and Wikipedia-style inline marks on Inferred claims with their reason [58]. Dossiers as Are.na-style channels: a specimen can be filed in several dossiers and keeps its provenance; approval freezes the version.
- **Trade-offs.** + Makes thin data look thin *honestly*, and makes the gaps legible. + Directly supports the five-minute brief-to-PDF journey (find, inspect, file). + Distinctive, with an instrument aesthetic grounded in real readings rather than neon. + Scales to Coverage, Compare and Investigations with the same grammar. − Denser; risks feeling like work rather than reading at 06:30. − The specimen tray competes for space at 1,280 px. − Needs strict editorial discipline to stop "record" metadata swamping the finding.

### Direction C: "Culture Atlas" (the spatial and relational map of culture)

**Idea.** 42 as a navigable map of the cultural field: communities, formats, sounds and creators as places, with routes (lineage and spread) between them and markets as territories. Draws on Pulsar communities [33], TimeMap [57] and the FT spatial and flow categories [50].

- **Hierarchy.** A home "atlas" view: a stable, hand-tuned layout of clusters (communities and kinds), not a force layout. Today's items appear as marked locations; selecting one opens a side panel with the card. Topic pages show lineage as routes.
- **Type.** Recursive for labels at 13 and 16 px; Newsreader for place names of major clusters (an atlas convention) and the finding sentence.
- **Colour.** Territories (markets) as light tints; state shown by marker shape; uncertain placement drawn as a fuzzy halo; not-collected regions as hatched "unsurveyed" zones, a cartographic convention that fits honesty well.
- **Layout.** Desktop: a full-bleed canvas with a list view toggle (needed for accessibility). Mobile: list first, map optional. A canvas does not reflow, so the list has to be the primary mobile and assistive-technology experience [60].
- **Motion.** Pan and zoom to the selection, with transitions that keep the reader oriented; nothing ambient.
- **Charts.** Lineage routes, spread timelines, and community-by-market matrices. Avoid force graphs and choropleths.
- **Evidence.** Through the side panel, as in B.
- **Trade-offs.** + The most "futuristic" and memorable, and good for pitching to clients and for seeing communities. − The layout implies relationships and distances 42 cannot measure; with weeks of data the clusters are mostly guesswork. − The worst fit for accessibility, mobile and speed. − It invites the decorative graph Albert explicitly warns against. − It is expensive to keep a stable, meaningful layout as categories churn daily.

### Recommendation: Direction B, "Field Notebook", keeping A's editorial moment

Reasons, tied to 42's users and data:

1. **The data argues for it.** With weeks of history and uncertain locality, the most honest and most useful thing 42 can show is *what was observed and what was not*. B is the only direction whose basic grammar (finding, record, specimens; filled, hollow, dashed and hatched marks) shows thinness and gaps as visible facts rather than footnotes. The research on missing data [6][7], on salience [10] and on why authors drop uncertainty under pressure [11] all favour building uncertainty into the component itself.
2. **The users' job is inspection and filing.** Strategists must defend claims to clients. Elicit, NotebookLM and scite show that what earns professional trust is *one click from claim to passage* [40][42][43], not a polished summary. B makes that the main interaction and feeds the dossier review directly (Single source and Inferred need a tick).
3. **It is distinctive without decoration.** Recursive Mono readings beside a single Newsreader sentence, dot strips instead of gauges, and a calm keyboard-first workspace look like nothing else in social listening, which is full of KPI tiles, sentiment pies and network graphs [34][35][36], and they avoid the generic Linear-clone look [54].
4. **It keeps what A does best.** Today keeps the newspaper front (32 px heading, one Newsreader finding sentence, a Smart Brevity order of finding, why it matters, proof, ask [59]). B governs everything beneath that sentence.
5. **It borrows C's one honest idea.** Lineage lines and the spread timeline appear on topic pages, as dated evidence-backed sequences, not as a map.

The track-record chip and analogue dotplots switch on only when the data supports them: four weeks of scored calls, and 10 or more analogues. Until then the interface says so in words. That rule is the clearest single expression of the direction.

---

## 3. Anti-patterns (with examples)

1. **A normalised score with no denominator.** "Trend score 87/100", or Google Trends-style indices used outside a comparison [29]. Use counts with bases: "3 to 30 posts".
2. **Growth % on a tiny base.** "+900%", as on Exploding Topics-style cards [30], when it is 3 posts to 30.
3. **Forecast lines and cones from weeks of data.** Glimpse-style 12-month forecasts [31]; hurricane cones misread as size and safe boundaries [21].
4. **Sentiment pies and share-of-voice donuts** over a classifier that is unreliable in code-switched languages [34].
5. **Decorative network graphs.** 3D or force-directed "culture graphs" whose proximity means nothing [33].
6. **Choropleth maps of SA, NG and KE** when most posts lack a confident location. Use the coverage strip's "share of posts with a confident location" instead.
7. **Meters and verdict gauges.** A Consensus-meter-style "yes 70%" over a thin sample [41]; speedometer confidence dials.
8. **Interpolating across days not collected.** A smooth sparkline across gaps [51]; hiding missing days entirely [6].
9. **Skeleton screens or fake progress bars during agent runs.** Use the research log with real counts [25][44].
10. **Jitter, pulse and "live" animation** that encodes nothing, or encodes uncertainty in a way that causes anxiety [22]; chart draw-in on every load.
11. **Confidence shown by colour alone**, or pale grey "inferred" lines below 3:1 [60].
12. **Citation theatre.** Numbered footnotes that link to a homepage, not the passage or timestamp [47].
13. **Superlatives without a frame.** Wrapped-style "Top 1%!" [39], or "everyone in Lagos is…".
14. **KPI tile grids** as the home screen: the generic dashboard [27].
15. **Platform lists passed off as culture.** TikTok Creative Center lists exclude user-generated sounds [32]. Label them "the platform's own list", as planned.
16. **Neon-on-black "intelligence" styling**, scanlines, glowing globes: the look of rigour instead of rigour.
17. **Mixing likelihood and confidence on one card** ("Corroborated · likely to peak") without the separate, calibrated scales IPCC uses [16].
18. **Feedback that looks as if it trains the system.** Real / Not real taps must say they guide reviewers only [24].
19. **Caveats behind tabs.** OWID-style source tabs are good, but the one caveat that changes the reading must sit under the figure [49][20].
20. **Composite "impact" or "value" metrics** in the manner of AVE and EMV [38].

---

## 4. Sources

1. Kay, Kola, Hullman, Munson (2016). When(ish) is My Bus? CHI. https://idl.uw.edu/papers/when-ish-is-my-bus ; https://mucollective.northwestern.edu/project/when-ish-is-my-bus
2. Fernandes, Walls, Munson, Hullman, Kay (2018). Uncertainty displays using quantile dotplots or CDFs improve transit decision-making. https://idl.uw.edu/papers/uncertainty-bus
3. Hullman, Resnick, Adar (2015). Hypothetical Outcome Plots outperform error bars and violin plots. PLOS ONE. https://pmc.ncbi.nlm.nih.gov/articles/PMC4646698
4. Kale, Nguyen, Kay, Hullman (2018/19). HOPs help untrained observers judge trends in ambiguous data. https://idl.uw.edu/papers/hops-trends
5. Few, S. HOPs, Skips, and Jumps to Silly Visualizations (blog critique). https://www.perceptualedge.com/blog/?p=2275
6. Song, Szafir (2018/19). Where's My Data? Evaluating visualizations with missing data. https://cmci.colorado.edu/visualab/MissingData
7. Song, Fu, Saket, Stasko. Understanding the effects of visualizing missing values on visual data exploration. https://arxiv.org/pdf/2109.08723
8. Visualization of missing data: a state-of-the-art survey (2024). https://arxiv.org/pdf/2410.03712
9. Sarma et al. (2022). Uncertainty visualisations for imputations of data missing at random in scatterplots. https://content.ieeevis.org/year/2022/paper_v-full-1121.html
10. Padilla, Creem-Regehr, Hegarty, Stefanucci (2018). Decision making with visualizations: a cognitive framework across disciplines. https://pmc.ncbi.nlm.nih.gov/articles/PMC6091269
11. Hullman (2020). Why Authors Don't Visualize Uncertainty. https://mucollective.northwestern.edu/project/2019-value-of-uncertainty-vis
12. Maybe, Maybe Not: A Survey on Uncertainty in Visualization (2023). https://arxiv.org/pdf/2301.07687
13. Kent, S. (1964). Words of Estimative Probability. CIA Studies in Intelligence 8(4). https://www.cia.gov/resources/csi/studies-in-intelligence/archives/vol-8-no-4/words-of-estimative-probability
14. Words of estimative probability (Kent's table). Wikipedia. https://en.wikipedia.org/wiki/Words_of_estimative_probability
15. Mauboussin and Mauboussin (2018), summarised by J. Collins: How likely is "likely"? https://www.jasoncollins.blog/posts/how-likely-is-likely
16. IPCC AR5 Guidance Note on Consistent Treatment of Uncertainties (Mastrandrea et al. 2010). https://www.ipcc.ch/site/assets/uploads/2017/08/AR5_Uncertainty_Guidance_Note.pdf ; https://boris.unibe.ch/155719
17. Spiegelhalter (2017). Risk and uncertainty communication. Annual Review of Statistics. https://www.repository.cam.ac.uk/handle/1810/260626
18. Spiegelhalter (2018). Communicating uncertainty without losing trust (EFSA slides). https://www.efsa.europa.eu:443/sites/default/files/event/180918-conference/presentations/18-0_04_Spiegelhalter.pdf
19. van der Bles, van der Linden, Freeman, Spiegelhalter (2020). The effects of communicating uncertainty on public trust in facts and numbers. PNAS. https://www.repository.cam.ac.uk/handle/1810/304699
20. ONS Service Manual: Showing uncertainty in charts. https://service-manual.ons.gov.uk/data-visualisation/guidance/showing-uncertainty-in-charts
21. Hurricane cone of uncertainty literature (Ruginski 2016; Boone et al. 2018). https://bmcnoldy.earth.miami.edu/papers/MMCMEB2022_IJHCI.pdf ; https://www.aoml.noaa.gov/general/lib/lib1/nhclib/Bibliographies/Cone%20of%20Uncertainty%20Literature%20Review_4_10_19%20%281%29%20%281%29.pdf
22. NYT election needle and jitter. https://flowingdata.com/2018/03/14/needle-of-uncertainty ; https://www.niemanlab.org/2020/11/bad-polls-fake-news-and-that-damned-needle-heres-how-the-media-covered-a-twisty-election-night/
23. Google PAIR, People + AI Guidebook: Explainability + Trust. https://pair.withgoogle.com/chapter/explainability-trust/
24. Amershi et al. (2019). Guidelines for Human-AI Interaction. Microsoft Research. https://www.microsoft.com/en-us/research/project/guidelines-for-human-ai-interaction/
25. NN/g. Skeleton Screens 101. https://nngroup.com/articles/skeleton-screens/
26. NN/g. Progressive Disclosure. https://www.nngroup.com/videos/progressive-disclosure/
27. NN/g. Dashboards: Making Charts and Graphs Easier to Understand (preattentive processing). https://www.nngroup.com/articles/dashboards-preattentive/
28. NN/g. Designing Empty States in Complex Applications. https://nngroup.com/articles/empty-state-interface-design
29. Google News Initiative: Google Trends, understanding the data. https://newsinitiative.withgoogle.com/fr-ca/resources/trainings/google-trends-understanding-the-data ; SerpApi explainer https://serpapi.com/blog/google-trends-numbers-from-0-to-100-what-is-it/
30. Exploding Topics: Google Trends vs Exploding Topics. https://explodingtopics.com/blog/google-trends-vs-et
31. Glimpse: absolute search volume in Google Trends. https://meetglimpse.com/insights/google-trends-search-volume/
32. TikTok Ads Help: How to use Trends (Creative Center). https://ads.tiktok.com/help/article/how-to-use-trends
33. Pulsar TRAC; Pulsar help, network graph. https://pulsarplatform.com/pulsar-trac ; https://intercom.help/pulsar/en/articles/7211144-trac-general-listening-audience-insights-network
34. Brandwatch sentiment white paper and user reviews. https://www.brandwatch.com/wp-content/uploads/brandwatch/Sentiment-Analysis.pdf ; https://www.rfp.wiki/vendors/brandwatch
35. Meltwater: spike detection analysis in Explore. https://help.meltwater.com/en/articles/8069613-using-spike-detection-analysis-in-explore
36. Hootsuite trend research (Talkwalker-powered). https://www.hootsuite.com/platform/trend-research
37. WGSN methodology. https://wgsn.com/en/methodology
38. The several premature autopsies of AVE (Journal of Professional Communication). https://mulpress.mcmaster.ca/jpc/article/view/84
39. An algorithmic event: the celebration and critique of Spotify Wrapped (New Media & Society, 2025); How Spotify Wrapped nails data storytelling. https://metatoc.com/papers/158135-an-algorithmic-event-the-celebration-and-critique-of-spotify-wrapped ; https://figuresandfigures.substack.com/p/how-spotify-wrapped-nails-data-storytelling
40. scite Smart Citations (Quantitative Science Studies, 2021); HKUST Library trial notes. https://doaj.org/article/81fa50fb3c294f6ba01cc597d41a974e ; https://library.hkust.edu.hk/sc/trial-scite/
41. Consensus Meter: guardrails and limitations. https://consensus.app/home/blog/consensus-meter/
42. Elicit: systematic review data extraction; Introducing Elicit Systematic Review. https://support.elicit.com/en/articles/4267649 ; https://elicit.com/blog/systematic-review
43. NotebookLM grounded Q&A and citations (guide). https://freeacademy.ai/lessons/notebooklm-grounded-qa-citations
44. OpenAI Help: Deep research FAQ. https://help.openai.com/en/articles/10500283-deepresearch-faq
45. Perplexity citations and Pages (secondary descriptions). https://llmpulse.ai/blog/how-perplexity-works/ ; https://suprmind.ai/hub/?p=5211
46. Undermind product review (JCHLA); Undermind whitepaper; SMU Libraries note. https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12352444/ ; https://www.undermind.ai:443/whitepaper.pdf ; https://library.smu.edu.sg/topics-insights/undermindai-now-available-postgraduates-faculty-and-staff
47. Tow Center / Columbia Journalism Review (2025). AI search has a citation problem; coverage in Computing. https://www.cjr.org/?p=108431 ; https://www.computing.co.uk/news/2025/ai/ai-search-engines-plagued-by-inaccuracy
48. Claude Artifacts vs ChatGPT Canvas (comparison). https://instapods.com/blog/claude-artifacts-vs-chatgpt-canvas/
49. Our World in Data: how to use OWID; example chart with sources and processing note. https://ourworldindata.org/how-to-use-our-world-in-data ; https://ourworldindata.org/grapher/taxes-on-goods-and-services-gdp
50. FT Visual Vocabulary (GIJN; FT GitHub). https://gijn.org/resource/document-of-the-day-visual-vocabulary/ ; https://github.com/ft-interactive/visual-vocabulary/blob/main/v2-data.js
51. Datawrapper: connect all points in line charts (missing data). https://www.datawrapper.de/blog/connect-all-points-in-line-charts
52. The Pudding: telling stories in visual data-driven essays (FlowingData). https://flowingdata.com/2020/09/08/telling-stories-in-visual-data-driven-essays
53. Bloomberg: How Bloomberg Terminal UX designers conceal complexity; Stromberg, UI density. https://www.bloomberg.com/company/stories/how-bloomberg-terminal-ux-designers-conceal-complexity ; https://mattstromawn.com/writing/ui-density/
54. The Linear aesthetic decoded: tokens, density and keyboard-first UX (third-party analysis). https://www.buildmvpfast.com/blog/linear-aesthetic-tokens-density-keyboard-first-ux-2026
55. Are.na: About; Channels. https://www.are.na/about ; https://help.are.na/getting-started/channels
56. Google Search "About this result" (2021 expansion). https://9to5google.com/2021/09/29/google-search-about-this-result/
57. Forensic Architecture TimeMap (used in Bellingcat deployments). https://github.com/forensic-architecture/timemap
58. MediaWiki Template:Citation needed; Redi et al. (2019). Citation Needed: a taxonomy and algorithmic assessment of Wikipedia's verifiability. https://www.mediawiki.org/wiki/Template:Citation_needed ; https://arxiv.org/pdf/1902.11116
59. How Axios is reinventing text journalism with Smart Brevity; CJR critique. https://www.journalism.co.uk/how-axios-is-reinventing-text-journalism-with-smart-brevity/ ; https://www.cjr.org/criticism/axios-smart-brevity-longform.php
60. What's new in WCAG 2.2 (TetraLogical); W3C What's New in WCAG 2.1 (Reflow, Non-text Contrast, Animation from Interactions). https://tetralogical.com/blog/2023/10/05/whats-new-wcag-2.2 ; https://www.w3.org/WAI/standards-guidelines/wcag/new-in-21/
61. MDN: prefers-reduced-motion. https://developer.mozilla.org/fr/docs/Web/CSS/Reference/At-rules/%40media/prefers-reduced-motion
62. Accessible data visualization: colour vision and contrast (Okabe-Ito, viridis). https://r-statistics.co/Accessible-Data-Visualization-in-R.html

**Gaps in this pass:** Observable, Raycast, Miro, Notion, the FT and Bloomberg mobile apps, and NYT Upshot "how we did this" pages were not source-checked, and are marked where used. The IPCC likelihood ranges are the standard AR5 values; the PDF returned 403 to the fetcher. Several vendor capabilities (Pulsar, Glimpse, WGSN, Elicit) rest on vendor pages. Recursive's MONO and CASL axes should be confirmed in the vendored design-system build.
