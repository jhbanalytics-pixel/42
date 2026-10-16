# Ask: deep research at volume. Research notes (2026-10-16)

Web research only. Prices are public list prices as fetched on 2026-10-16; anything marked UNVERIFIED came from third-party pages or could not be checked against a primary source.

---

## 1. SocialCrawl (socialcrawl.dev)

Found. Primary sources: https://www.socialcrawl.dev/pricing, https://www.socialcrawl.dev/docs-md/endpoint-pricing, https://www.socialcrawl.dev/docs/credits. Internal evidence: `/home/user/42/engine/docs/socialcrawl-probe-2026-07-23.md` (live probe, 20,000 credits bought).

**Coverage.** The endpoint-pricing page lists 68 platforms and 651 active endpoints. Counts differ between pages and locales: the credits page says 176 Standard / 90 Advanced / 18 Premium, and the endpoint page says 240 / 167 / 25 plus 219 Custom. Platforms include TikTok, Instagram, Facebook, X/Twitter, YouTube, Reddit, Threads, LinkedIn, Bluesky, Pinterest, Telegram, Quora, Google, Google News, Trustpilot, Hacker News and others.

**Tiers.** Standard 1 credit, Advanced 5, Premium 10 (transcripts, LinkedIn people/jobs). "Custom/metered" endpoints hold credits based on `limit` up front, then refund what was not used.

**Search endpoints relevant to Ask** (credits per call/page):
| Endpoint | Credits | Notes |
|---|---|---|
| `/v1/search/everywhere` | 20 flat | "Universal social search across 15 platforms" |
| `/v1/twitter/search/tweets` | 1-9 metered | exists in our approved catalog fixture |
| `/v1/reddit/search` | 1-34 metered | probe: `reddit/omni-search` billed 5, not 1 |
| `/v1/threads/search` | 1-34 metered | ~15 results per window; probe: very stale tail (2023) |
| `/v1/tiktok/search` | 1-42 metered | <=30 results at 1 credit; probe: `date_posted`/`region` return 0 rows; `tiktok/search/top` works |
| `/v1/facebook/search/posts` | 1-9 metered | |
| `/v1/instagram/search` | 1 | reels search 1-69 |
| `/v1/youtube/search` | 1-19 metered | up to 50 rows/page; `region=` works for ZA (probe) |
| `/v1/linkedin/search/posts` | 5/page | |
| `/v1/web/search` | 2 per 10 results, max 100 (20 cr); `include_content=true` +1/result (max 120) | |
| `/v1/search/news` | 2 + 1 per Google leg, max 14 (62 with Bing) | multi-country |
| `/v1/google_news/search` | 1 | `location_name=` ZA works (probe) |

**Pagination/billing rules.** Every page of a list is billed separately. `max_pages` walks pages at the per-page price. `seen` lowers the price for rows already received. Cache hits cost 0. Empty pages, not-found, upstream timeouts are refunded. Optional `relevant_to=` / `label=` LLM relevance judging adds credits per batch of 25. Cursor-based pagination is mentioned, but the full spec was not read (UNVERIFIED).

**Limits.** 600 requests/min, 50 concurrent per key, no monthly cap.

**Pricing (one-time packs, credits never expire).** Free 100 credits. Starter 2,500 for GBP 15. Growth 20,000 for GBP 49 (~GBP 0.00245/credit). Pro 150,000 for GBP 299 (~GBP 0.002/credit). Enterprise custom. The pricing page also shows smaller figures (8,000 / 50,000) next to Growth/Pro, which it does not explain. The page's own blended estimate is "~3.1 credits per call" for mixed tiers.

**What this means for Ask.** About 200 posts is roughly 10-20 one-credit pages. Getting 3,000-5,000 posts means roughly 150-500 page calls. At typical page sizes of 10-30 rows and 1-5 credits per page, that is about 200-1,500 credits, or about GBP 0.50-3.70 per deep question at Growth-pack rates. Concurrency of 50 makes fan-out feasible. The real caps are page size and recency: several surfaces return stale content and need a `max_age_days` filter.

---

## 2. Web search and retrieval APIs

| API | List price | Rate limit | Full text | Date filter | Country (ZA) |
|---|---|---|---|---|---|
| **Exa** ([pricing](https://exa.ai/pricing), [search ref](https://exa.ai/docs/reference/search), [rate limits](https://exa.ai/docs/reference/rate-limits)) | Search instant $4/1k, fast/auto $7/1k, deep $12-15/1k (10 results incl., +$1/1k extra results); Contents $1/1k pages; Answer $5/1k; Agent $0.012-$1.00/request by effort. $10/month free credit | /search 10 QPS, /contents 100 QPS, /answer 10 QPS (official table, via search summary) | Yes: `contents.text` up to 1M chars, highlights, summaries | `startPublishedDate`/`endPublishedDate` (ISO) | `userLocation` ISO code; `includeDomains` up to 1,200 (e.g. `*.co.za`, news24.com) |
| **Tavily** ([pricing](https://www.tavily.com/pricing), [credits](https://docs.tavily.com/documentation/api-credits), [search ref](https://docs.tavily.com/documentation/api-reference/endpoint/search)) | $0.008/credit PAYG; Project $30/4k credits; Growth $500/100k. Search basic 1 credit (~$8/1k), advanced 2 (~$16/1k). Extract 1 credit per 5 URLs. Research 4-250 credits. 1,000 free/month | Not published (429 with Retry-After) | `include_raw_content` (markdown/text); Extract endpoint | `time_range` d/w/m/y; `start_date`/`end_date` | `country` (only with topic=general); max 20 results |
| **Brave Search API** ([page](https://brave.com/search/api/), [params](https://api-dashboard.search.brave.com/app/documentation/web-search/query)) | Search $5/1k; Answers $4/1k + $5/M tokens; $5/month free credit (card required) | Search 50 QPS; Answers 2 QPS | No full page text; up to 5 `extra_snippets` per result. Needs a separate fetcher | `freshness` pd/pw/pm/py or `YYYY-MM-DDtoYYYY-MM-DD` | `country=ZA`; `count` max 20, `offset` max 9 (so ~200 results/query) |
| **Serper** (serper.dev) | UNVERIFIED (official pricing page 404s per third parties): ~$1.00/1k at the $50 pack, down to ~$0.30/1k at the 12.5M pack; credits expire after 6 months; 2,500 free. 11-100 results cost 2 credits ([source](https://apiserpent.com/blog/serper-pricing-credits-explained)) | UNVERIFIED | Snippets only (Google SERP) | `tbs=qdr:h/d/w/m/y` (Google syntax, UNVERIFIED for Serper) | `gl=za`, `num` up to 100 |
| **SerpAPI** ([pricing](https://serpapi.com/pricing)) | $25/1k (Starter) down to $7.25/1k (100k/month) and $1.96/1k (54M Cloud). 250 free/month | 50/hr free; 20,000/hr at the 100k plan | Snippets only | Google `tbs` | Google `gl`/`location` |
| **Firecrawl** ([pricing](https://www.firecrawl.dev/pricing)) | Scrape 1 credit/page (+4 for JSON extraction); Search 2 credits/10 results. Standard $83/month for 100k credits (~$0.83/1k pages); Growth $333 for 500k. 1,000 free/month | Concurrency 2 (free) to 100 (Scale) | Yes: clean markdown, JS rendering | Search supports `tbs` (UNVERIFIED) | Location param (UNVERIFIED) |
| **Jina Reader / Search** ([reader](https://jina.ai/reader/)) | Token-metered; ~$0.05/M tokens per third parties (UNVERIFIED, official pricing page reportedly 404). 10M free tokens (reportedly non-commercial). s.jina.ai costs >=10k tokens per call | Reader 20 RPM no key, 500 RPM with key, 5,000 premium; Search 100 / 1,000 RPM | Yes (r.jina.ai -> markdown) | Not checked | Not checked |
| **Perplexity** ([pricing](https://docs.perplexity.ai/getting-started/pricing), [filters](https://docs.perplexity.ai/docs/sonar/filters)) | Search API $5/1k (Fast $1/1k). Sonar $1/$1 per M tokens + $5-12/1k requests by context size; Sonar Pro $3/$15 + $6-14/1k; Deep Research $2 in / $8 out / $3 reasoning per M + $5/1k searches | See their Rate Limits page (not read) | Synthesized answer + citations; raw page text not returned | `search_recency_filter` hour..year; after/before date filters | `user_location.country` (Sonar); `country` on the Search API |
| **Google Programmable Search (Custom Search JSON API)** ([overview](https://developers.google.com/custom-search/v1/overview)) | $5/1k after 100 free/day, capped at 10k/day | 10k/day | Snippets only | `dateRestrict` | `gl`, `cr=countryZA` |

**Google CSE is closed to new customers.** Existing customers must migrate by 1 January 2027. Do not build on it.

**Practical fit for Ask.**
- Primary web retrieval: **Exa** or **Tavily**. Both return full text in one call and filter by date and country/domain. Exa's `includeDomains` with an SA news allowlist plus `startPublishedDate` suits "recent polls" questions well.
- High-volume and cheap discovery: **Brave** (50 QPS, `country=ZA`, freshness ranges) or Serper, followed by **Firecrawl** or **Jina** to fetch full text for the top-k URLs only.
- SocialCrawl's own `/v1/web/search` and `/v1/search/news` already cover basic web and news under the existing key.
- **Perplexity Sonar** works as a single-call "answer with citations" baseline, but it hides the source text, which makes the figure-extraction step in section 3 hard to audit.

---

## 3. How deep-research engines structure the pipeline

Sources:
- Anthropic, "How we built our multi-agent research system": https://www.anthropic.com/engineering/multi-agent-research-system
- Gemini Deep Research: https://gemini.google/overview/deep-research/ and https://workspace.google.com/blog/ai-and-machine-learning/meet-deep-research-your-new-ai-research-assistant
- Perplexity: https://www.perplexity.ai/hub/blog/introducing-perplexity-deep-research
- OpenAI: https://openai.com/index/introducing-deep-research/ (403 when fetched; details below are from memory and UNVERIFIED this session)
- Consensus: https://consensus.app/home/blog/consensus-meter/
- Elicit: https://elicit.com/blog/how-we-evaluated-elicit-systematic-review, plus the independent evaluation in Research Synthesis Methods (Cambridge)
- SAFE/FActScore/VeriScore: https://arxiv.org/abs/2305.14251 (FActScore), https://arxiv.org/html/2406.19276v1 (VeriScore)

**Common stages:**
1. **Plan / decompose.** Gemini writes a multi-point research plan the user can edit, and decides which sub-tasks run in parallel and which run in sequence. Anthropic's lead agent plans, saves the plan to memory, and spawns subagents with clearly divided tasks.
2. **Fan-out.** Anthropic launches 3-5 subagents in parallel, each running 3+ tools in parallel, and reports up to 90% lower time on complex queries. Perplexity runs "dozens of searches, reads hundreds of sources" in 2-4 minutes. Gemini says "up to hundreds of websites".
3. **Start broad, then narrow.** Anthropic found short, broad queries beat long, specific ones. Gemini also changes its queries as it learns.
4. **Scale effort to the question.** Anthropic: simple fact = 1 agent with 3-10 tool calls; comparison = 2-4 subagents with 10-15 calls each; complex = 10+ subagents. Multi-agent runs use about 15x the tokens of chat. Token spend explained ~80% of performance variance on BrowseComp.
5. **Dedupe and judge source quality.** Anthropic found agents favoured SEO content farms over authoritative sources, which they had to correct. OpenAI states that Deep Research can struggle to tell authoritative information from rumour and is weakly calibrated on confidence (UNVERIFIED this session).
6. **Claim extraction.** FActScore splits text into atomic facts and scores the share supported. SAFE decomposes, makes each fact self-contained, and verifies it with up to 5 search rounds. VeriScore extracts only verifiable claims. Elicit pulls structured values into table columns with supporting quotes.
7. **Citation pass.** Anthropic uses a dedicated CitationAgent after synthesis to tie each claim to its source span.
8. **Aggregation.** Consensus Meter classifies the top 20 relevant results as yes/no/possibly. Its first version weighted a meta-analysis and an n=1 case report equally. Meter 2.0 added quality indicators. Lesson: show the counts and the quality, not just a roll-up.

**Reliability warning (Elicit).** An independent study found extracted *values* agreed 90% across repeat runs, but supporting *quotes* matched only 46% of the time. Extracted numbers therefore need a deterministic check that the number actually appears in the cited text.

**Recommended structure for numeric poll questions.** No vendor documents this; it is a synthesis.
1. Decompose: pollster x geography x date window.
2. Search an SA news/pollster allowlist with a date filter.
3. Extract a typed record per poll: `{pollster, client/commissioner, fieldwork dates, geography, population (adults/registered/likely), mode (phone/online/f2f), n, MoE, turnout model, party -> %, source URL, quote}`.
4. Verify that each % string appears in the source text.
5. Dedupe by (pollster, fieldwork dates), because the same poll is republished by many outlets and by parties.
6. Reconcile; never average blindly. Show each poll as its own bar with its MoE. Optionally compute a weighted average using the 538-style weights below. Flag partisan-commissioned polls, mismatched geography (Gauteng vs Johannesburg metro), and different populations or turnout models.

Polling-average practice: 538 weighted polls by sqrt(n), capped at 5,000. It weighted LV > RV > adults (1 / 0.9 / 0.7 in one derivative description), adjusted for pollster rating, recency and Bayesian-shrunk house effects. See https://fivethirtyeight.com/features/how-our-polling-averages-work/ (538 averages are now discontinued; specifics are via secondary sources).

**Live example of why reconciliation matters.** This is search-engine summary text and is UNVERIFIED. For Johannesburg in 2026, SRF/The Common Sense reported DA 42%, ANC 18% (phone, n=500, MoE 4.4%), while Ipsos reported ANC 31%, DA 27%. The DA's own release republished SRF numbers. Sources: https://www.dailymaverick.co.za/article/2026-09-04-anc-and-da-in-dead-heat-across-key-metros-as-mk-dominates-ethekwini, https://www.da.org.za/2026/08/da-narrows-national-gap-with-anc-to-7-points-in-latest-public-poll. A good Ask answer shows both polls with their methods. It does not pick one or blend them.

---

## 4. From thousands of posts to honest percentages

**Why social share is not a poll.**
- Self-selection and coverage bias: platform users are not the electorate.
- Activity concentration: Pew found the top 10% of US tweeters produce 80% of tweets (https://www.pewresearch.org/internet/2019/04/24/sizing-up-twitter-users/).
- Volume measures attention, not support. Negative mentions also count as mentions.
- Gayo-Avello's survey of 25 papers found Twitter election "predictions" were retrospective and overstated (https://arxiv.org/abs/1204.6441).
- Non-representative data *can* be adjusted, but only with demographic covariates and MRP. Wang, Rothschild, Goel, Gelman (2015) did this with 750k Xbox respondents (https://sites.stat.columbia.edu/gelman/research/unpublished/forecasting-with-nonrepresentative-polls.pdf). Social posts almost never carry those covariates.

**Standard techniques:**
- **Unit of analysis = unique author, not post.** Aggregate stance per account (one account, one vote) to remove the heavy-poster skew.
- **Dedupe.** Exact hash on normalised text; near-duplicates with MinHash + LSH (Jaccard on shingles) or SimHash; collapse retweets, quote-posts and copypasta into "campaign clusters".
- **Bot/coordination filtering.** Account-age, posting-rate and follower-ratio heuristics; coordinated-behaviour detection (many accounts posting the same text inside a short time window). Botometer X is archival only: scores exist only for accounts seen before June 2023, and the model predates LLM bots (https://osome.iu.edu/research/blog/introducing-botometer-x). Do not rely on it.
- **Relevance + stance classification.** Stance is target-specific (favour / against / neither toward party X), per SemEval-2016 Task 6. Use an LLM zero/few-shot classifier and validate it against a hand-labelled gold set of ~200-500 posts per market. Report precision/recall, and correct prevalence with confusion-matrix "adjusted classify-and-count" (quantification learning, Forman 2008).
- **Sampling + confidence intervals.** When there are more posts than you can afford to classify, take a random or stratified sample (by platform and by day). Use Wilson score intervals for proportions (better than normal approximations at small n or extreme p). Weighting reduces precision: use Kish effective sample size, n_eff = (sum w)^2 / sum w^2. Bootstrap over authors for the CI on author-level shares.
- **Weighting / post-stratification.** Stratify by platform (fixed weights, or equal per platform) and, where inferable, by region. Raking or MRP needs known population margins and covariates per author, which social data rarely has. Say so in the answer.
- **Geo-scoping.** Use verified location (`tiktok/profile/region`, YouTube `region`) over inferred location. Keep a geo-confidence flag.
- **Honest labelling.** Label the output "share of unique accounts expressing support among N classified relevant posts", never "support". Always show N, the time window, the platform mix and the CI. When a question asks for "support in polls", answer from polls (section 3) and use social only as a secondary "conversation" chart.

---

## 5. Cost control patterns

- **Tiered depth (Anthropic's effort scaling, mapped to Ask):**
  - Quick: 1 planner call; 3-5 web searches plus about 5 social pages; ~100-300 posts; target < $0.10.
  - Standard: 3-5 sub-queries; ~20 searches with full text for the top 20 URLs; ~1,000 posts; target ~$0.50-1.
  - Deep: 10+ sub-queries across all platforms; ~3,000-5,000 posts; LLM classification on a sample; target ~$2-5.
- **Budget caps per question.** Hold a credit/$ budget in the planner. Each tool call debits an estimate before running, mirroring SocialCrawl's own hold-then-refund model. Hard-stop and synthesise with whatever has been gathered when the budget runs out.
- **Caching.**
  - SocialCrawl cache hits are free.
  - Exa `maxAgeHours` controls content cache freshness.
  - Add your own cache keyed on normalised query + date window + market. Cache extracted poll records by URL so later questions reuse them.
  - Semantic (embedding) cache for repeated questions: reported 61-69% hit rates in FAQ-style workloads (https://arxiv.org/abs/2411.05276). Expect less for research.
  - Use SocialCrawl's `seen` parameter for cheaper re-pagination.
- **Early stopping / convergence.** No vendor documents this. Suggested patterns:
  - Stop fetching social pages when the stance-share CI half-width falls below a target (e.g. ±3 pp) or the share moves by less than 1 pp over the last k pages.
  - Stop web search when new searches yield no new distinct polls (deduped by pollster + fieldwork date) for 2 rounds.
  - Stop when the marginal new-unique-author rate falls below a threshold (saturation).
- **Cheap-first routing.**
  - Use snippet-only search (Brave/Serper at ~$1-5/1k) for discovery, then fetch full text only for shortlisted URLs (Exa Contents $1/1k, Firecrawl ~$0.83/1k).
  - Classify with a small model; escalate only ambiguous posts.
  - Sample before classifying: 1,000 classified posts give about ±3 pp at 95% before weighting.

---

## Not verified / gaps
- SocialCrawl: full cursor/pagination spec, exact page sizes on Twitter `search/tweets` and `search/everywhere`, and the meaning of the dual credit figures on the pricing page.
- Serper and Jina official pricing (official pages unavailable; third-party figures used).
- Exa rate limits came via a search summary of the official page, not a direct fetch.
- Tavily and Perplexity numeric rate limits.
- OpenAI Deep Research details (page returned 403).
- The SA poll figures quoted in section 3 are illustrative and come from search summaries, not the original articles.
