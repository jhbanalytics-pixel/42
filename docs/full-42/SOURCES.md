# Sources and seeding

How 42 finds what matters without being told, and where every piece of data comes from. Facts checked against SocialCrawl's live OpenAPI (downloaded 28 September 2026, compact copy in docs/full-42/reference/sc_routes.json) and the existing code. Items marked (check) must be probed live in Stage 0 (task 0.5) before relying on them.

## SocialCrawl basics

- Base https://www.socialcrawl.dev/v1, header x-api-key, paths /v1/{platform}/{endpoint}. 67 platforms, about 630 endpoints.
- Credits: most calls 1; advanced 5; premium 10; composite (prism, search/everywhere) more. Cached responses cost 0. Empty pages and 502/503 are refunded.
- Limits: 600 requests a minute, 50 concurrent per key.
- Cursor pagination: pagination.next_cursor, has_more. Many search routes accept seen and max_age_days; profile/posts accepts since.
- Balance: credits/balance. Status: status. Monitors: POST /v1/monitors re-runs any GET route on a schedule (at most hourly) to a signed webhook, recipe cost plus 1 credit.
- Hosted MCP server: https://mcp.socialcrawl.dev/mcp (Bearer or x-api-key).
- Key: SOCIALCRAWL_OGILVY_API_KEY in Secret Manager (funded, about 250,000 credits on 23 September). Never use SOCIALCRAWL_API_KEY (zero balance).

Fix first, from the audit: the old connector called tiktok/trending with the default feed=global, where only about 22% of videos come from the requested country. feed=local gives about 57%. Several prices in the old CREDIT_COST table are stale (instagram/search/reels is 1, not 9; youtube/shorts/trending 5, not 15; youtube/video/comments 1, not 5; reddit/post/comments 5, not 9). Prices come from the OpenAPI spec and are re-checked against the vendor-reported charge on every call.

## Routes 42 uses

Seeding, no keyword needed:

| Route | Returns | Markets | Credits |
|---|---|---|---|
| tiktok/trending?feed=local | About 24 For You videos for a phone in that country, with ext.region | ZA, NG, KE (NG and KE share: check) | 5 |
| tiktok/hashtags/popular | Creative Center hashtag board: rank, posts, views, daily curve, top creators; 7, 30 or 90 days | ZA only (27 countries listed; NG and KE are not) | 6 per board |
| youtube/videos/trending | Trending by region and category | ZA, NG, KE | 1 |
| apple_music/charts | Songs, albums, videos, playlists | ZA (NG, KE: check) | 1 |

Expansion, once a candidate exists: tiktok/search/top with country=ZA,NG,KE (1), tiktok/search/hashtag (1), tiktok/search/music (1), youtube/search/advanced with region and published_after (1), instagram/search/reels (1), instagram/search/location plus location/posts (5 each, the only Instagram geo route), twitter/search/tweets with since: (1), reddit/search and search/comments (1), threads/search (1), facebook/search/posts with location_uid (1), linkedin/search/posts (1), google_news/search with location_name (1; this is Google News, not Google Trends), search/multi across 8 platforms (about 5 to 7), search/creators (10), search/forums (10).

Depth, once something is worth it: post details and comments on every platform (1 to 5), transcripts (YouTube 3; TikTok, Instagram, X, Reddit, Facebook 10), tiktok/video/screen-text (5), tiktok/song and song/videos (1), instagram/audio/reels (1), creator profiles and recent posts (1), prism/creator-card (5).

Never used: every google_trends route except google_trends/trending, which feeds query triage only and is labelled Google search data, never evidence (RULES.md rule 2); prism/trend-board (every call fetches Google Trending Now and cannot be switched off; 42 buys its parts directly for about 19 credits instead of 30); prism/earliness (Google Trends lane); prism/audience-language and tiktok/user/audience (audience samples and demographics); twitter/ai-search, prism/investigate, prism/answers and the AI-visibility route (generated answers); the Perplexity, Tavily, Grok and Polymarket lanes of search/everywhere, which is always called with exclude=perplexity,tavily,twitter-ai-search,polymarket. The client in core/collect refuses all of these in code.

## SocialCrawl's own model labels

Search rows come back with judgments=on by default: computed.relevance plus sponsored, intent and niche labels. These are model outputs. 42 stores them in a separate column (vendor_labels), may use them to route, rank or filter what it collects, and never cites them as evidence or shows them as fact. label=injection (1 credit per 25 posts) runs on every evidence post before the brain reads it, and flagged posts are shown to the brain as quoted text only, with the flag. fit=goal is used only inside Ask.

## Cheap tricks that make SocialCrawl go further

- seen: one seen id per market per day, shared by the 02:00 collect and the 05:15 confirm, so confirmation pays only for new rows on the 12 search routes that accept it (not search/multi). It lasts 24 hours, so it never replaces the 28-day novelty memory.
- Counts as velocity: tiktok/hashtag and tiktok/song return running totals (global, not per country) at 1 credit, and tiktok/song/videos returns a per-day adoption curve; the day-to-day change is the measurement panel's velocity (TRUST.md section 4).
- Batch re-reads: POST prism/post-stats re-reads up to 100 evidence URLs at 1 credit each; POST prism/profiles with include=posts and since= reads panel creators in bulk.
- Geo: instagram/location/posts is the only Instagram geo route; youtube/search/advanced takes location and location_radius; X geocode: and near: operators may pass through twitter/search/tweets (probe).
- Reconciliation: credits/transactions is free; a nightly job compares every charge with the ledger. Watch prices that conflict in the spec (linkedin/search/posts lists 1 credit but a formula of 5 a page; cohort queries have no listed price) and TikTok country filters, which bill each page before discarding foreign rows.
- web/scrape at 1 credit can stand in for dead news RSS feeds (inferred; probe first).

## The morning run, costed (3 markets, 1,000 credits)

Full reasoning, every platform family's verdict and every route's parameters are in docs/full-42/research/13-socialcrawl-full-map.md.

| # | Collect 02:00 | Calls | Cr |
|---|---|---|---|
| 1 | tiktok/trending feed=local, 3 per market | 9 | 45 |
| 2 | tiktok/hashtags/popular ZA: overall 7-day board daily; industry=all on Mon and Thu (averaged) | 1-2 | 32 |
| 3 | youtube/videos/trending, 5 categories per market | 15 | 15 |
| 4 | youtube/shorts/trending and instagram/music/trending | 2 | 10 |
| 5 | instagram/location/posts, one rotating hub per market | 3 | 15 |
| 6 | apple_music/charts, songs and music-videos | 6 | 6 |
| 7 | reddit/subreddit, rising and hot: ZA 6, NG 3 and KE 3 own subreddits | 24 | 24 |
| 8 | facebook/profile/posts since=yesterday, 4 hub pages per market | 12 | 12 |
| 9 | telegram/profile/posts, 9 channels; facebook/events, 6 cities | 15 | 15 |
| 10 | tiktok/profile/videos for new local-feed authors (breakout baseline) | 36 | 36 |
| 11 | tiktok/song/videos for the top 6 sounds per market | 18 | 18 |
| 12 | tiktok/song and tiktok/hashtag counts, 30 watched items | 30 | 30 |
| 13 | instagram/audio/reels for sounds found on both platforms | 6 | 6 |
| 14 | tiktok/search/top country=XX publish_time=this-week seen= (50 candidates a market, KE 60 with its dropped feed pulls; 15% exploration; no cluster above 25%) | 160 | 160 |
| 15 | tiktok/search/hashtag max_age_days=7; search/suggestions region | 30 | 30 |
| 16 | search/multi on instagram, youtube, reddit, twitter, threads, facebook, since=D-7, top 7 per market | 21 | ~105 |
| 17 | youtube/search/advanced region published_after, candidates ranked 8-10 | 9 | 9 |
| 18 | google_news/search location_name, time_range=week, top 10 per market | 30 | 30 |
| 19 | tiktok/post/comments sort=top (50); youtube/video/comments (10) | 60 | 60 |
| 20 | tiktok/post/transcript (4) and video/screen-text (4) | 8 | 60 |
| 21 | POST youtube/transcripts (5); tweet/replies or facebook/post/comments (10) | 11 | 25 |
| 22 | POST prism/post-stats, about 60 evidence URLs (velocity) | 1 | ~70 |
| 23 | POST prism/profiles include=posts since=yesterday: culture-desk hub panel, 12 accounts per market (replaces the 10 panel creators) | 2 | ~72 |
| 23a | twitter/user/tweets since=yesterday: X hub panel, 8 accounts per market | 24 | 24 |
| 23b | web/scrape of the public X trends archive, ZA, NG, KE, twice a day (seed only; after legal says yes) | 6 | 6 |
| 23c | Placebo expansions: search/multi on random low-ranked items (TRUST.md A2) | 3 | ~15 |
| | **Collect total** (exploration stays at about 15% inside row 14) | | **~801** |
| | **Confirm 05:15, top 10 per market** | | |
| 24 | search/multi on the platforms where the candidate is missing, since=first sighting | 30 | ~90 |
| 25 | tiktok/search/top sort_by=date-posted, same seen id | 20 | ~20 |
| 26 | Account country from tiktok/profile or instagram/profile/about, cached per account | 20 | 20 |
| 27 | prism/creator-card verify=true for 3 originators; label=injection on evidence rows | 3 | ~20 |
| | **Confirm total** | | **~150** |
| | **Reserve** (502/503 are refunded anyway): 6 local-feed calls for breaking items (30); 1 search/everywhere, AI lanes excluded (20) | | **50** |

Stage 1A collects rows 1 to 8, 11, 12, 14, 16, 22, 23, 23a and 23b (about 529 credits); the other rows join in Stage 2 (task 2.4).

**An extra 500 credits a day, most valuable first:**

1. A second local-feed round plus a 14:00 pulse: 75.
2. Instagram location posts at 5 more places: 25.
3. search/multi for the candidates ranked 11-15: 75.
4. 6 more TikTok transcripts and 6 more screen-text reads: 90.
5. The ZA industry=all board every day instead of twice a week: +64.
6. reddit/post/comments on 8 threads: 40.
7. linkedin/search/posts on 8 brand candidates: 40.
8. 2 search/everywhere calls, AI lanes excluded: 40.
9. facebook/group/posts on curated public groups: 10.
10. tiktok/search/users with country=, weekly panel build: about 20.
11. google_play/app-list weekly: about 20.

Total: about 500.


Known dead ends from the old build: tiktok/search returns nothing with any date or region filter; Instagram hashtag search has no geo; Snapchat, Twitch, Kick and Telegram have no discovery routes; SocialCrawl has no X trends route.

## Added after the plan review

- X trends seed: web/scrape (1 credit) of the public per-country X trends archive (for example trends24.in) for ZA, NG and KE twice a day. Presence only: a seed that can be bought and is never evidence; twitter/search/tweets then finds posts that can be cited. Legal checks the site's terms. Probe 14 in task 0.5.
- Hub panels: 8 X accounts and 12 culture-desk accounts per market, confirmed by colleagues in each city (BUILD.md 0.9) (entertainment, gossip, music, sport and news pages that move culture), read with twitter/user/tweets since= and prism/profiles include=posts since=. They give Nigeria and Kenya the market-level volume they otherwise lack. About 96 credits a day, paid for by trimming reddit, search/multi and YouTube search (rows 7, 16, 17, 23 to 23c of the costed table); exploration keeps its share.
- Placebo expansions: about 5% of expansion calls go to random low-ranked items, to measure the base rate of "found on 3 platforms" (TRUST.md A2). About 15 credits a day (row 23c).
- Local sources (Stage 2, about 40 credits a day): the Nairaland front page, an Instagram gossip and blog hub panel per market, Boomplay, Audiomack, Shazam, TurnTable and kworb Spotify country charts through web/scrape, Google Play charts (Android dominates all three markets), and RSS for Briefly, Tuko, Pulse, BellaNaija, Punch, Citizen Digital, TshisaLIVE and EWN after a live check.
- Geo recipe per platform (ext.region, creator home market, language, place mentions) with checks for leaks from neighbouring countries (Ghana and Cameroon into Nigeria; Tanzania and Uganda into Kenya; Zimbabwe, Lesotho and Botswana into South Africa). Coverage shows the share of posts with a confident location per market.
- Languages: add Sesotho, Setswana, Sepedi and Xitsonga, and the Wikipedia projects pcm, st, tn, nso and ts. Tone is benchmarked on AfriSenti and NaijaSenti before any tone claim leaves Single source.

## Other sources (no SocialCrawl credits)

| Source | Use | Status |
|---|---|---|
| GDELT (BigQuery public dataset gdelt-bq.gdeltv2, free within the 1 TB monthly query tier) | Seed generator: rising people, organisations, themes and places in news per market against a 28-day baseline; events spikes; tone swings | Rework the existing connector engine/src/ingestion/connectors/gdelt.py from raw rows to daily aggregates. Drop gdelt_slang and the Gen Z queries. |
| News RSS | Headlines as seeds and context | Keep the feed list, replace dead feeds (News24 feeds.24.com dead; TimesLIVE and Pulse 404; Premium Times and Guardian NG bot-blocked). Add Legit.ng, Nairametrics, Kenyans.co.ke, Mpasho, SowetanLIVE, ZAlebs after a live probe. |
| Wikipedia pageviews | Interest signal: top articles per country, and per language project zu, xh, af, sw, yo, ha, ig | Keep; add language projects; send a proper User-Agent. |
| Music charts | Apple Music, Shazam national and city charts (Johannesburg, Lagos), TurnTable Top 100 (Nigeria) | Keep Apple Music; add Shazam and TurnTable. |
| App charts | App Store top charts za, ng, ke | Keep. |
| Calendars | Public holidays (date.nager.at, checked for ZA, NG, KE), festivals, sports fixtures, Facebook events/search | New: moments calendar (see FEATURES.md). |
| YouTube Data API and yt-dlp | Chart, comments, free transcripts | Keep chart and comments; yt-dlp carries terms-of-service risk, low priority. |
| Cloudflare Radar | Outages and shutdowns as context | Keep at low weight. |

Removed: bigquery_trends, google_trends_rss, search velocity SQL, semrush, EnsembleData connectors, brand24, spotify, pulsar.

## The morning seed loop (per market, collect job at 02:00 SAST)

1. Harvest from feeds that need no keyword: TikTok local feed three times, the ZA hashtag board, YouTube trending by category, Reddit country and city subreddits (hot), charts, Wikipedia, GDELT rising entities, news headlines. Extract candidates: hashtags, sounds, creators, named entities, phrases.
2. Score each candidate: novelty against memory (28-day baseline, not seen in N days), velocity (board curve, change since yesterday), agreement across platforms and source families, locality (ext.region, language). No demographic or slang weight.
3. Expand the top candidates: search/multi across TikTok (country filter), YouTube (region), Reddit, X, Threads, Facebook; sounds to song videos and audio reels; hashtags to hashtag search; creators to recent posts.
4. Deepen the survivors: comments on top posts, transcripts of the top one or two videos, creator region check.
5. Remember: every candidate and its yield go to intelligence_42_core.seed_queue with a time to live. Tomorrow's harvest reads it. No hand-edited YAML.

Morning collect credit split (about 800 of the 1,000 daily cap across three markets; confirm takes about 150 and 50 stay in reserve, ENGINE.md section 3): harvest about 130, hub panels and the X trends seed about 150, expansion about 270, depth about 150, measurement panel and watchlist about 100 (a fixed daily protocol on every live candidate from first sighting, TRUST.md section 4; use since=, rotate, drop stale), and exploration at 10 to 15% of expansion (about 27 to 40 credits) inside row 14, not an extra share. The reserve is SETUP.md's separate share, not part of collect's 800.

Keeping it open and unbiased:
- Seeds are earned from unseeded feeds. The old fixed term pools become an anchor slice of at most 15% of expansion credits, and each anchor expires if it stops yielding.
- 10 to 15% exploration: random picks from below the cut-off, rotation through TikTok industry boards and YouTube categories, new subreddits, wildcard GDELT and Wikipedia entities, and a bandit (Thompson sampling) on yield.
- Diversity quotas: no single cluster, platform or source family gets more than about 25% of expansion credits; stratify across en, pcm, sw and Sheng, zu, xh, af, yo, ha, ig.
- Geography is checked on the data (ext.region, profile region, language), not by market keyword lists. The old geo blocklist stays for name collisions.
- Weekly drift report: concentration by topic, platform and language (Herfindahl index) against the unseeded feeds; alert on drift. Recall check against the calendar and GDELT events.

## News to social bridge

GDELT's top ten rising entities per market become search/multi queries with since=yesterday. 42 records whether social chatter follows and how fast. Culture that rises on social with no news coverage is marked as native, which is often the most useful signal for a strategist.

## Blind spots, stated honestly

WhatsApp is private and dominant in all three markets; no tool can see it. Official X trends by location need a paid X tier; the public trends archive and the X hub panels are how 42 sees X in Nigeria and Kenya. YouTube's regional trending chart only covers music, film and gaming since July 2025 (check whether SocialCrawl's route inherits this). The TikTok hashtag board covers ZA only; Nigeria and Kenya rely on the local feed and country-filtered search.

The supplier reply dated 7 October 2026 confirms account country at author.location on tiktok/profile and author.ext.country on instagram/profile/about, one credit each. The collect job reuses explicit reel creator country and cached source-qualified country receipts before another account lookup. Request region and collection market remain separate from post location. Unknown charges retain their existing holds.
