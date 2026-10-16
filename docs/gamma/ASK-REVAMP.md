# Gamma 3.1: Deep Ask

Prepared on 16 October 2026 by the Gamma thread, at Albert's request: "push Ask to the next level... pull vast amounts of data live from the internet and all social channels, then lay out the facts, with the posts scraped as proof. Accuracy is the name of the game."

The near-term fix to today's Ask belongs to the "Ask depth and visual answers" thread. This document covers the 3.1 redesign only. Nothing in it is built, deployed or paid for yet.

Supporting material:

| What | Where |
|---|---|
| Working prototype (the Ask tab) | `prototype/gamma-prototype.html` and the published prototype link |
| Provider, pricing and method research with sources | [research/ASK-RETRIEVAL-RESEARCH.md](research/ASK-RETRIEVAL-RESEARCH.md) |

## 1. Why Ask gives thin answers today (checked in code on 16 Oct)

42 has two question paths, and both explain part of the Gauteng poll answer (7 of 194 posts cited, no figures).

**The Ask screen (`core/agent/ask.py`, a Beta lane).** This path does search live through SocialCrawl, under a credit and model budget. The "Ask depth and visual answers" thread found six causes and fixes them in draft PR #12 (branch `ask-depth-visual-d2lkha`, not yet merged):

- Live items arrive wrapped (`{"post":{...}}`, `{"article":{...}}`), but `core/agent/tools/socialcrawl.py` read the top-level fields. Every live result was therefore uncitable.
- Google News search needs `keyword`, but researchers sent `query`, so the call was refused.
- A one-market question searched TikTok, X, Instagram and YouTube only, never news.
- Ask had no way to read an article.
- The whole-store topic sweep ran only for hashtags or quoted phrases.
- The writer may state only figures 42 computed itself, so reported poll figures had nowhere to go.

PR #12 adds:

- item unwrapping and the keyword mapping;
- news-first lanes for questions that ask for figures;
- article reading (robots respected, 8 per question);
- a free sweep for figures over the whole window;
- figures checked in code, drawn as one bar chart per source.

**The general question pipeline (`engine/.../open_intelligence`, the admitted-question queue).** This path makes no live calls. It reads only pinned BigQuery snapshot tables (`general_question_context_queries.py:87-96`).

- Evidence is capped at 200 records and candidates at 100 (`general_question_execution.py:611-612`).
- Quote sentences containing a number are dropped (`general_question_answer.py:90-92`).
- Generated prose may not contain numbers, and election claims are banned (`general_question_answer.py:317-319`).
- The schema has `readings[]` (`intelligence_reply.py:408-436`) but no chart field.

**What is left for 3.1.** PR #12 makes today's Ask cite live results and show reported figures. It does not cover the following, all of which Deep Ask takes on:

- volume in the thousands, with cleaning and account-level counting;
- a topic sweep for questions in plain language;
- comparing several sources in one view;
- figures that are not percentages, such as seats and counts;
- figures in the export;
- a measured accuracy test.

Reporting published election polls also needs a policy decision (D6), because the general pipeline bans election claims.

The honesty rules already in Ask stay. They are abstention, "Unmeasured" rather than zero, hypothesis labels, and withholding ungrounded claims. Deep Ask adds figures without loosening any of them.

## 2. What Deep Ask does

Albert's own question is the reference case: "What share of the vote does each party have in Gauteng, according to recent polls?"

1. **Plan.** One planner call splits the question into 4 to 10 searches. Questions that ask for figures from published sources (polls, prices, market shares) are flagged as figure questions.
2. **Live sweep.** Searches fan out in parallel through SocialCrawl, which Ogilvy already pays for, so no new provider is needed:
   - platform search on TikTok, X, Facebook, Instagram, YouTube, Reddit, Threads, LinkedIn, Pinterest, Quora, Rumble and Hacker News, plus forum search;
   - `/v1/search/everywhere` (15 platforms in one call);
   - `/v1/web/search` with full text;
   - `/v1/search/news`.

   Results are paged until the depth budget or the stopping rule (step 6) ends the sweep. Deep reads up to 8,000 posts and Ultra Search up to 20,000.
3. **Clean.** The cleaning steps are:
   - exact and near-duplicate removal (MinHash);
   - collapsing copied-text campaigns;
   - removing likely bots (account age, posting rate, identical text across accounts within minutes);
   - locating posts, using platform-verified region before inferred location, with each post keeping a location-confidence flag.
4. **Extract figures with proof.** Each published poll becomes a typed record:
   - pollster, commissioner and fieldwork dates;
   - geography and population;
   - mode, sample size and margin of error;
   - party percentages, source URL and the source sentence.

   **Each figure is then checked in code against the source sentence.** A figure that does not appear verbatim is dropped, never guessed. This answers the main accuracy risk the research found: in one independent study an extraction tool's numbers agreed across runs 90% of the time, but its supporting quotes matched only 46%. Polls are deduplicated by pollster and fieldwork dates, partisan-commissioned polls are flagged, and a mismatch such as Johannesburg versus Gauteng is shown.
5. **Count social honestly.**
   - Stance is classified per party with a small model, validated on a hand-labelled set and corrected for its known error rates.
   - The unit is the account, not the post.
   - Shares carry Wilson 95% intervals.
   - Social shares are labelled "share of accounts backing a party", are never called support, and are never blended into poll figures.
6. **Answer, and stop when done.** The answer opens with one sentence and the figure. It then shows:
   - the poll chart (median, range and each poll as its own mark);
   - the social chart with intervals;
   - the cleaning funnel and per-platform counts;
   - facts with confidence labels, and what 42 does not know;
   - a Proof section listing every poll record with its source sentence, the posts behind the social shares (filterable by party), and the web pages used.

   The run stops early once the shares move less than 1 point over the last pages, or once two rounds find no new polls.

## 3. Depth and cost

| Depth | Posts, up to | Platforms | Web pages | Credit cap | Estimated cost per question |
|---|---|---|---|---|---|
| Quick | 600 | 3 | 6 | 120 | about £0.30 |
| Standard | 2,500 | 8 | 16 | 500 | about £1.20 |
| Deep (recommended) | 8,000 | 13 | 40 | 1,600 | about £4 |
| Ultra Search | 20,000 | 13 | 80 | 4,000 | about £10, plus about £2 of model cost |

How the estimates are built (list prices fetched on 16 Oct; full table with sources in the research note):

- **SocialCrawl.** Search pages cost 1 to 42 credits depending on the endpoint and return 10 to 50 rows. `search/everywhere` costs 20 and web search costs 2 per 10 results. Cache hits are free and empty pages are refunded. A realistic blend is 0.05 to 0.2 credits per post, so each cap above is posts times 0.2, the expensive end. At the Growth pack (20,000 credits for £49, about £0.00245 a credit) the caps convert to the pounds shown; most runs should land nearer half the cap, because the stopping rule ends them early.
- **Ultra Search, worked out.** 20,000 posts at 0.05 to 0.2 credits a post is 1,000 to 4,000 credits, or £2.45 to £9.80. Stance classification on the flash model is about 3 million input tokens for 20,000 posts, roughly US$2.25 at the repo's own price constant (`general_question_policy.py:182-185`). Wall time is about 8 minutes at SocialCrawl's concurrency of 50. Ultra Search pauses at 1,600 credits (the Deep cap) and asks before it spends more.
- **Platforms.** SocialCrawl's approved catalogue (`engine/tests/fixtures/socialcrawl_catalog_2026_09_04.json`) has keyword search for TikTok, X, Facebook, Instagram, YouTube, Reddit, Threads, LinkedIn, Pinterest, Quora, Rumble and Hacker News, plus `search/forums`, `search/everywhere`, `google_news/search` and `web/search`. Deep and Ultra Search use all of them, 13 sources counting forums. Bluesky, Snapchat, Twitch, Kick, Kwai, Truth Social and Telegram have no search route in that catalogue, only profile, post or comment lookups, so Ask cannot sweep them by topic.
- **Stance classification.** Classifying about 4,000 posts on the flash model is about 600,000 input tokens, roughly US$0.45.

Cost controls:

- The planner holds a credit budget, and each call debits its quote before it runs, matching how the funded lane already works (`socialcrawl.py` pre-call hook, `CREDIT_COST`).
- When the cap is reached, Ask answers from what it has and says so.
- The estimate is shown before the run.
- Searches and poll records are cached by query, window and market, so a repeat question costs little.
- Deep asks before spending past its cap.
- Spend draws on the existing funded-lane ledger (`funded_lane.py:34-41`: monthly cap 25,000 credits, reserve floor 225,000).

**The whole monthly ledger (25,000 credits) would cover only about 15 Deep questions or 6 Ultra Search questions at their caps, and daily collection already draws on it.** So Ask needs its own allowance, which is decision D5.

## 4. Experiment E8: accuracy before anything ships

| Check | Pass bar |
|---|---|
| Figure precision on 30 questions with known published answers (polls, prices, shares) | 100% of shown figures match the source sentence; recall at least 90% |
| Stance classifier on 400 hand-labelled posts per market | macro F1 at least 0.80; shares shown corrected for measured error |
| Duplicate and bot removal on a labelled sample | at most 5% duplicates left; removal reasons logged |
| Honesty regression | every current Ask honesty test still passes; social shares never appear in a poll figure |
| Cost | 95% of Deep runs within cap; mean cost reported per depth |

Ask stays on the current pipeline until E8 passes.

## 5. Delivery

**G8 Deep Ask** is added to the plan's slice table. It covers:

- live retrieval through SocialCrawl;
- the figure-record extractor and checker;
- the account-level stance counter;
- a `charts[]` field in the reply schema, validated server-side like `readings[]`;
- the answer and Proof screens from the prototype.

The work depends on four things:

- Beta live and accepted. The Ask screen and `ask.py` belong to Beta lanes (BETA-LANES.md), so G8 starts after those lanes close, with ownership confirmed through the coordinator.
- The near-term fix in PR #12 from the "Ask depth and visual answers" thread, which G8 builds on rather than redoes.
- D5 and D6.
- E8 passing.

## 6. Decisions this adds for Albert (not due now)

| # | Decision | When | Recommendation |
|---|---|---|---|
| D5 | Paid live retrieval for Ask: a monthly SocialCrawl credit allowance for Ask, separate from daily collection | Before G8 build, after E8's offline run | Yes. Buy the Pro pack (150,000 credits for £299, credits never expire) and give Ask its own ledger of 30,000 credits a month, about £60 at Pro rates, which covers roughly 10 Deep and 3 Ultra Search questions at their caps, or about twice that if runs stop near half the cap as expected; review from real cost per question after E8 |
| D6 | Policy: may Ask report published election poll figures? Today's instruction bans election claims | Before G8 build | Yes for reporting published polls with their source and method; keep the ban on persuasion, prediction and any claim drawn from social posts about how people will vote |
