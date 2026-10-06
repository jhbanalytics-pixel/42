# The trend engine

42 is a cultural trend engine. Every morning it crawls every channel it can reach, processes what it found, works out what is starting to trend, confirms it is real, explains it with posts a person can open, and has the key things waiting for a strategist before the working day starts. Asking questions, briefs, dossiers and client skins are ways of using what the engine finds. When a design choice is unclear, choose what makes detection earlier, truer and wider.

Albert, 28 September 2026: "This thing needs to be a powerhouse of cultural trends. All channels... It's not just a culture map, it's a full-on cultural engine." And, on cadence: the morning scrape, crawl, process and pull out the key things works; always-on only if it adds no build days to getting 42 operational.

Decision: the morning run is the engine and ships first. Intraday collection is a later switch on the same code (section 3, "Later"), not part of getting 42 operational.

## 1. The morning loop

Six steps, every day, with no person in the loop.

| Step | What happens | Where |
|---|---|---|
| Crawl | Pull the no-keyword feeds of every channel (what platforms themselves show as popular), yesterday's news and interest signals, and the seeds earned the day before; expand and deepen | Collect job, SOURCES.md seed loop |
| Process | Extraction, embeddings, transcripts for the clips that matter, clustering into the cultural map | Understand job |
| Detect | Compare every item and cluster with its own 28-day normal for that market and platform: lifecycle, velocity, spread, novelty, authenticity, worth-attention | Detect job, DATA.md section 3 |
| Confirm | Chase the top candidates across other platforms with targeted searches since their first sighting; authenticity checks | Confirm lane with its own credit share |
| Explain | The brain writes a short cited explanation for each of the top trends per market: what it is, the earliest post 42 found, where it came from, why now (Inferred unless posts state the cause) | Brief job: a fixed pipeline of one structured call per trend, not agent runs (BUILD.md 1.12) |
| Push | The key things land on Today and the trend feed, watched items fire alerts, one email digest goes out | App and email |

Learning runs alongside: every trend's later path is recorded, forecasts are scored, seeds that paid off earn more budget, false alarms lower their rule's weight, and a weekly scorecard says how early and how right 42 was (section 6).

## 2. What counts as a trend

A trend is any cultural item whose use is growing faster than its own normal: a hashtag, sound, format, meme, phrase, topic, creator, brand, event, product or place. Items are grouped into clusters by meaning (embeddings) so that one idea spread across five hashtags and three languages is one trend.

States, shown as words on every trend card:

| State | Rule (starting values, recalibrated after four weeks) |
|---|---|
| New to 42 | Warm-up only (untested, first measured sighting in the last 14 days): 3 distinct unflagged creators in unbiased-feed or panel lanes in 3 days, or 1 board entry; no earlier wave; counts shown, no growth claim (DATA.md section 3.7) |
| Spike | One significant day without persistence or a second platform; shown as unconfirmed, with counts |
| On the boards | Present on a platform's own board or chart (TikTok hashtag board, YouTube trending, music and app charts); labelled as the platform's list, not 42's finding. The X trends archive is a seed only and never appears on Today |
| Emerging | New or dormant item with at least 5 unflagged creators and 8 posts in 3 days, no creator above 40%. In warm-up, after 5 or more observed days with a 3-day count not below the previous 3 days', without the statistical test; once 14 days exist, it must pass the TRUST.md section 4 test (DATA.md section 3.7) |
| Rising | Significant after false-discovery control on 2 days with ratio 2 or more, or on 1 day plus an independent platform or market (TRUST.md section 4) |
| Peaking | Growth slowing (acceleration below zero) while volume is near its high |
| Mainstream | Reached large creators and news, or 3 or more platforms at volume |
| Fading | Volume down 40% from its peak for 3 days |
| Recurring | Peaked before in the last 365 days, or matches an earlier wave in the cultural map; shows when and how far it went |
| Seasonal | Matches the moments calendar or the same moment last year; shown as Seasonal, not Rising |

Every trend also carries: a worth-attention rank (the percentile behind it stays internal, DATA.md), spread path (first seen where and when, then which platforms, creator tiers and markets), native or news-led (the news to social bridge), authenticity flags, a forecast with a date, and the evidence posts.

## 3. The schedule

| Time (SAST) | Job | What it does | SocialCrawl credits a day (3 markets) |
|---|---|---|---|
| 02:00 | collect | Harvest all channels, score candidates, expand, deepen, remember (SOURCES.md) | about 800 |
| 04:00 | understand | Extraction, embeddings, selected transcripts and video reading, clustering, cultural map merge | 0 (transcripts inside collect's share) |
| 05:00 | detect | Baselines, states, spread, novelty, authenticity, worth-attention, forecasts | 0 |
| 05:15 | confirm and explain | One cross-platform search on each of the top 10 candidates per market, then a cited explanation for each confirmed trend | about 150 |
| 06:30 | publish | Today brief, trend feed, alerts, email digest; the seed queue for tomorrow | 0 |
| Monday 07:00 | learn | Question score, detection scorecard, drift report, forecast scoring | 0 |

Caps are set in one table in SETUP.md (morning engine 2,400 credits a day with collect's share 2,000, Ask 600, month 80,000 with Ask throttled first, balance floor 20,000, daily model spend). Confirmation uses cheap cross-platform searches (since= on search/multi, and the seen id on the search routes that accept it, SOURCES.md; about 5 credits a trend); explanations read the warehouse and the confirmation results, so they spend model tokens, not credits. The jobs are chained on success rather than fixed clocks, so the times above are targets; at 06:15 the brief publishes whatever has passed the gate.

Later (Stage 4, only if Albert wants it): an intraday pulse. The collect job already takes a route list and a window, so the pulse is the same job on a second schedule (TikTok local feed, YouTube trending, Reddit rising, X and Threads searches on the day's hot items, every 2 to 4 hours, about 300 to 550 credits a day), plus an hourly Breaking rule over an item_hourly table (6-hour count at least 3 times the expected share of the daily baseline, at least 8 creators, seen in an unbiased feed or on 2 platforms). GDELT's 15-minute updates are free and can join the pulse at no credit cost. None of this is needed to make 42 operational.

## 4. Why the morning is enough to start

Most cultural trends take days to weeks to move from niche to mainstream; what a strategist needs is to see them on day one or two of that climb, with proof. A daily run with 28-day baselines, cross-platform confirmation and creator-tier spread catches that climb. What it misses is the same-day spike (a news shock, a viral moment), which Ask covers on demand because the agent calls SocialCrawl live inside an answer.

## 5. All channels

| Channel | How 42 sees it | Notes |
|---|---|---|
| TikTok | Local For You feed per market (feed=local), ZA hashtag board, country-filtered search, sound pages, comments, transcripts, screen text | Main discovery channel |
| Instagram | Trending reels and trending audio (global context), reels search, location posts, audio pages, tagged posts, comments, transcripts | No country-level trending exists |
| YouTube | Trending per region and category, Shorts trending, region search, comments, transcripts | Regional chart covers music, film and gaming only since July 2025 (check) |
| X | Hub panels (twitter/user/tweets since=), search with since=, replies, communities, transcripts | No official trends route; the public trends archive seeds searches only (presence, never evidence) |
| Facebook | Post search with location, public pages, events search | Events feed the moments calendar |
| Reddit | Country and city subreddits sorted by rising and hot, search, comments | Cheap and early for NG and KE diaspora talk |
| Threads and Bluesky | Keyword search with dates; Threads topic tags | Small in these markets; cheap confirmation |
| LinkedIn | Post and hashtag search | Business and corporate culture |
| Telegram | Public channel posts for a curated list per market | Kept to a short list; no discovery route |
| X trends archive | Public per-country trend pages via web/scrape, twice a day | Presence-only seed; can be bought; never evidence |
| Local sites and charts | Nairaland, Boomplay, Audiomack, Shazam, TurnTable, kworb, local news RSS (Stage 2) | Through web/scrape and RSS |
| Kwai, Snapchat, Twitch, Kick | Profiles and Spotlight comments only | No discovery routes; watched only through creators |
| Podcasts and music | Spotify search and podcast episodes, Apple Music and Shazam charts, TurnTable (NG) | Sounds and artists are cultural items |
| News | GDELT daily aggregates (15-minute updates available later), news RSS, Google News search | Seeds and the news to social bridge |
| Interest | Wikipedia pageviews by country and language project; app store charts | Free early signals |
| Calendar | Holidays, festivals, fixtures, Facebook events | Moments calendar |

Not seen, said plainly on every screen that could imply otherwise: WhatsApp and other private groups, official X location trends (the public archive is used only as a seed), Pinterest Trends (SocialCrawl's route does not cover ZA, NG or KE), Google Trends (excluded by rule).

## 6. How we know the engine works

A weekly detection scorecard, written by the learn job to intelligence_42_agent.engine_scorecard and shown on Coverage:

- Time to detect: days from an item's first unbiased sighting to its first Emerging or Rising state. Target: within 2 days for items that later reach Mainstream.
- Lead time: days 42 flagged an item before it reached a held-out reference list that 42 does not use as an input (news roundups, the curated ground truth in engine/configs/watchlist_ground_truth.yaml scored by the lifted leadtime_eval.py, a weekly strategist list). Boards and charts 42 collects are inputs, so scoring against them would be circular. Target: positive median lead time by week 4. "Real" has a written rubric in TRUST.md section 6.
- Precision: share of the morning's top trends that the weekly random review marks real (TRUST.md section 6). Target: 80% by week 4. One-tap feedback shows where to look but never feeds this number.
- Recall: share of calendar moments and large GDELT events that 42 surfaced on social within 24 hours.
- Breadth: share of trends seen on 2 or more platforms; share of expansion credits per cluster, platform and language (no more than 25% each).
- Cost per confirmed trend.

## 7. Where the trends show up

Today is the home screen and opens on the morning's key things: five cards per market, staged as EXPERIENCE.md describes (Stage 1A card first; spread, flags and Watch arrive in Stage 2). Discover (Stage 2) is the full feed of every live trend, filterable by market, platform, kind and state, with Radar showing growth against reach. Alerts push watched items.

## 8. Skins, later

Once 42 runs, client skins are saved lenses over the same engine: a query set, a watchlist of accounts and stakeholders, and a report template. Nothing about a client changes the engine. A client weekly report, for example, would be built from: the week's main conversations and trends in the lens; public mentions and tags of the client's handles (prism/mentions, instagram/tagged, X search) with a respond-or-amplify flag; the tracked accounts' activity; and the coming week's moments with proposed content. Inbound and outbound community management activity (direct messages, the client's own replies, escalations) sits in the client's own social tools and needs an export or access from them; 42 cannot see it from public data. Skins are Stage 5.
