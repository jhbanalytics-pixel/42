# 13. SocialCrawl full utilisation map (28 Sept 2026)

Source: sc_openapi.json (637 paths). Cr = `x-credit-cost` plus the formula. ISO = any country code accepted, no enum published (probe). (inf) = inferred. **[AI]** = server-side model output: may route or filter, never cited.

## 1. Family verdicts

| Family | Verdict and routes |
|---|---|
| tiktok | USE (section 2). LATER: location/posts, effect/videos. NEVER: user/audience (demographics); videos/popular (US/JP/VN/TH/ID only); ads/top (enum has no ZA/NG/KE) |
| youtube | USE (section 2). NEVER: channel/about (25 cr, emails), audio and files |
| instagram | USE (section 2). LATER: reels/trending (global, views null). NEVER: followers, likers, stories |
| twitter | USE search/tweets, replies, user/tweets. LATER: community/tweets. ai-search is a Grok answer, [AI], so it is not used |
| facebook | USE (section 2). LATER: group/posts (3-4 posts a page). NEVER: marketplace. Ad library for skins only |
| reddit, threads, bluesky, telegram | USE (section 2). Telegram has no discovery route: use a curated list |
| linkedin | USE search/posts only. The other 67 routes (people, jobs, companies) are NEVER |
| kwai, snapchat, twitch, kick, rumble, truthsocial | LATER: profile, clip and comment routes only |
| apple_music | USE. spotify: LATER (no chart route) |
| pinterest | NEVER for seeding: trends enum has 23 countries, no ZA/NG/KE |
| google_news, search, web | USE multi, news and scrape. everywhere in reserve only. creators and forums LATER |
| prism | USE post-stats, profiles, jobs, creator-card, lookup. The rest is in section 3 |
| monitors, cohorts / credits, status, utility | LATER / USE |
| google_play, app_store | LATER: google_play/app-list (5) weekly as the Android chart for NG and KE |
| content_analysis | LATER, skins only (20 cr, [AI] sentiment) |
| google_trends | NEVER (rule) |
| google/search, tavily, perplexity, polymarket | NEVER as evidence |
| hackernews, quora, github, producthunt, naver, douyin, xiaohongshu | NEVER (off-market) |
| Ecommerce (amazon, aliexpress, ebay, etsy, walmart, target, kohls, home_depot, wayfair, hm, sephora, klarna, gumtree, tiktokshop, google_shopping), reviews (yelp, tripadvisor, trustpilot, g2), jobs, finance, us_congress_trades, on_page | NEVER (commerce, US-only, SEO; amazon best-sellers has no ZA). Link-in-bio (linktree and similar): LATER, for creator enrichment |

## 2. Routes in use

| Route | Cr | Key parameters | ZA/NG/KE | What it gives |
|---|---|---|---|---|
| tiktok/trending | 5 | region, **feed=local** | ISO | ~24 For You videos, 57% from that country (spec measurement). ext.region on every row. Empty page = refunded 503 |
| tiktok/hashtags/popular | 6-96 | countryCode, period 7/30/90, industry=all | **ZA only** (27-country enum) | Rank, views, daily curve, top_creators. 2 cr a tag |
| tiktok/search/top (and tiktok/search, the multi lane) | 1 | publish_time, **country**, exclude_country, max_age_days, seen, max_pages | Any: country filters on ext.region | Country-true expansion (billed before the filter) |
| tiktok/search/hashtag, search/music | 1 | max_age_days, seen; sort_by=most-used | Proxy only | Tag videos. Sounds with video_count and DSP ids |
| tiktok/search/suggestions | 1 | query, region | ISO | Autocomplete in that country |
| tiktok/song, song/videos, hashtag | 1 | clipId, use=1 | Global counts | Counts whose daily change is velocity; data.adoption per-day curve, `returning` flag |
| tiktok/profile/videos | 1 | since, stop_at_id | n/a | Creator median, so 42 computes breakouts (at least 3x) itself |
| tiktok/profile/region | 1 | handle | Returns ISO | Creator country plus counts |
| tiktok/post/comments | 1 (up to 7) | sort=top, scan_pages | n/a | Comments |
| tiktok/post/transcript; video/screen-text | 10; 5 | use_ai_as_fallback | n/a | Speech and on-screen text. ASR and OCR are machine extraction: cite the post, not the text |
| tiktok/search/users | 1; 5 a row with country= | include=profile, country | **Enum includes ZA, NG, KE** | Creators by account country |
| youtube/videos/trending | 1 (up to 6) | region, category, max_results, include=channel | ISO | Regional chart (non-music categories: probe) |
| youtube/shorts/trending | 5 | none | Global | Formats arriving from abroad |
| youtube/search/advanced | 1 (+5 with includeExtras) | region, **published_after**, order, **location plus location_radius**, seen | ISO | Exact dates; radius search (probe) |
| youtube/video/comments; POST youtube/transcripts | 1; 3 each | order; up to 100 ids | n/a | Depth. Failed rows are refunded |
| instagram/search/location, then location/posts | 5 + 5 | location_id | Any place | About 60 local posts a page |
| instagram/search/reels, search/popular | 1 | date_posted, max_age_days, seen | None | Expansion with no geo |
| instagram/audio/reels; music/trending | 1; 5 | audio_id | Account-built, not a region | Sound spread |
| twitter/search/tweets | 1 | since:, until: and min_faves: operators; sort; seen | Operators only (geocode: to probe) | X expansion and confirmation |
| facebook/search/posts; profile/posts | 1 | start_date, **location_uid** (no documented lookup), seen; since | ? | Facebook expansion; local hub pages |
| facebook/events | 1 (+1 an event for details) | city explore url, time | Any city | Calendar, RSVP counts |
| reddit/subreddit; search; post/comments | 1; 1; 5 | sort=rising/hot; timeframe, seen | Subreddits | r/southafrica, johannesburg, capetown, Nigeria, lagos, Kenya, nairobi |
| threads/search; bluesky/search | 1 a window; 1 | start_date, **expand=false**; since, lang | None | Confirmation |
| linkedin/search/posts | **Conflict: x-credit-cost 1, formula 5 a page** | date_posted, limit | None | Business culture |
| telegram/profile/posts | 1 | handle | n/a | Curated channels |
| apple_music/charts | 1 | country, type | ISO (a free 400 if Apple publishes no chart) | Chart rank |
| google_news/search | 1 | location_name, language_code, time_range | Free text | News bridge |
| search/news | 2 + 1 a leg (up to 14) | countries (50 editions, not listed), max_legs | Unverified | Articles are evidence; translation and group=stories are [AI] |
| web/scrape | 1 | url | Any | Pages behind dead RSS feeds (inf) |

## 3. Server-side features

| Feature (spec) | Cost | 42 decision |
|---|---|---|
| **judgments** [AI], on by default: "every row gains free SocialCrawl judgments (computed.labels, … computed.relevance)", with nothing dropped | 0 | Keep; store in model_signals. `niche` routes; `sponsored` is an authenticity flag ("signals to review, never a finding"). Comment labels route only |
| **relevance** score/filter, relevance_threshold, relevant_to [AI]: filter "drops the rows that are not about your query" | score/filter 0; relevant_to +1 per 25 rows | score everywhere, threshold in 42's code; filter plus relevant_to only for homonyms; log dropped_ids |
| **label** quality/mention/injection, exclude=engagement_bait, brand, brand_description, offer, label_evidence [AI] | +1 per 25 posts | **injection** ("flags text that addresses an AI system") on evidence posts before the brain reads them. quality down-weights bait. mention and brand: skins |
| **fit=goal**, goal, fit_tokens [AI]: stubs the rows the model judges unneeded | 0 | Never in collect. Ask only |
| **dry_run=1**: cost preview | 0 | Budget guard, Stage 0 |
| **seen**: drops rows already received under an id; price = credits x new rows / rows; memory **24 h** | Less | One id per market per day, shared by collect and confirm. It cannot be the 28-day novelty memory. Available on 12 search routes, **not on search/multi** |
| **max_pages**, min_views, max_age_days, country; **include** joins; **since/stop_at_id**; **expand** (threads) | Pages billed; joins 1 a row; expand up to +4 | Filters cut payload, not credits. Joins only when needed. since on every creator and page poll. expand=false in confirm |
| **Cache** (account-wide; search 2 min, posts 10, profiles 15); **Idempotency-Key** (24 h replay at 0) | 0 | Cache helps within a run and in Ask; never send `no-cache`. Deterministic idempotency key per call |
| **credits/balance, credits/transactions** (with request_id), **status** (circuit state) | 0 | Balance floor per job; nightly price reconciliation; status before retrying a 502 |
| **utility** endpoints, endpoint, capabilities | 0 | Weekly route and price diff |
| **monitors**: replays any GET route at most hourly; `track` keeps numbers; timeseries reads are free | Recipe + 1 a run | LATER: intraday pulse |
| **cohorts / cohort-queries**: up to 10,000 handles; literal keyword match over their authored posts; match=topics is [AI] | "1 per upstream page"; x-credit-cost null | LATER: creator panels per market for tier spread; probe the price |
| **search/multi**: the same query across 8 native searches, "each platform billed at its own endpoint's price", empty lanes free; since, tiktok.region, youtube.region | about 5 (0-28) | Main expansion and confirmation lane; filter TikTok rows on ext.region |
| **search/everywhere**: 17 lanes including perplexity, tavily, twitter-ai-search and polymarket; stance [AI] | 20 | Reserve only, always with `exclude=perplexity,tavily,twitter-ai-search,polymarket` |
| search/creators, search/forums (brief and account_kind are [AI]) | 10 | LATER |
| **prism/post-stats, profiles, jobs**: batches of 100 URLs, 50 handles, or 5,000 as a job; failures refunded | 1 a row (Instagram 2, LinkedIn 5) | USE for evidence-post velocity and panel polling |
| **prism/creator-card**: one handle on 4-7 platforms; verify=true adds an [AI] identity verdict | 5-8 | USE for originators |
| prism/lookup | Price of the route it resolves to | Ask: pasted URLs |
| **prism/trend-board**: TikTok breakouts, sounds, the hashtag board **and Google Trending Now** | 30 | **NEVER**: every call pulls the Google Trends lane. The same parts bought directly cost 5+6+5+3 = 19, and the hashtag board is empty for NG and KE |
| prism/earliness (includes a Google Trends lane); prism/audience-language ("opens the audience sample") | 25; 2-26 | NEVER: Google Trends rule and demographics rule |
| prism/video-intel, voice, format-lift, handle-audit, mentions, comments, creator-vet, audience-overlap | 1-50 | LATER: same-price bundles or skin tools; their tone, claim and brand_safety blocks are [AI] |
| prism/investigate, answers, ai-visibility, crisis-postmortem, twitter/ai-search, perplexity, tavily, web/agent | Varies | NEVER: LLM walks or generated answers |
| Brand and commerce prism routes (brand-mentions, campaign, crisis-radar, share-of-voice, reputation, leads and others); adverse-screen | 15-50 | LATER for skins. adverse-screen screens people: NEVER |

## 4. Morning run, 3 markets, 1,000 credits

| # | Collect 02:00 | Calls | Cr |
|---|---|---|---|
| 1 | tiktok/trending feed=local, 3 per market | 9 | 45 |
| 2 | tiktok/hashtags/popular ZA: overall 7-day board daily; industry=all on Mon and Thu (averaged) | 1-2 | 32 |
| 3 | youtube/videos/trending, 5 categories per market | 15 | 15 |
| 4 | youtube/shorts/trending and instagram/music/trending | 2 | 10 |
| 5 | instagram/location/posts, one rotating hub per market | 3 | 15 |
| 6 | apple_music/charts, songs and music-videos | 6 | 6 |
| 7 | reddit/subreddit, rising and hot | 16 | 16 |
| 8 | facebook/profile/posts since=yesterday, 4 hub pages per market | 12 | 12 |
| 9 | telegram/profile/posts, 9 channels; facebook/events, 6 cities | 15 | 15 |
| 10 | tiktok/profile/videos for new local-feed authors (breakout baseline) | 36 | 36 |
| 11 | tiktok/song/videos for the top 6 sounds per market | 18 | 18 |
| 12 | tiktok/song and tiktok/hashtag counts, 30 watched items | 30 | 30 |
| 13 | instagram/audio/reels for sounds found on both platforms | 6 | 6 |
| 14 | tiktok/search/top country=XX publish_time=this-week seen= (15 candidates a market; 15% exploration; no cluster above 25%) | 45 | 45 |
| 15 | tiktok/search/hashtag max_age_days=7; search/suggestions region | 30 | 30 |
| 16 | search/multi on instagram, youtube, reddit, twitter, threads, facebook, since=D-7, top 10 per market | 30 | ~150 |
| 17 | youtube/search/advanced region published_after, candidates ranked 11-15 | 15 | 15 |
| 18 | google_news/search location_name, time_range=week, top 10 per market | 30 | 30 |
| 19 | tiktok/post/comments sort=top (50); youtube/video/comments (10) | 60 | 60 |
| 20 | tiktok/post/transcript (4) and video/screen-text (4) | 8 | 60 |
| 21 | POST youtube/transcripts (5); tweet/replies or facebook/post/comments (10) | 11 | 25 |
| 22 | POST prism/post-stats, about 60 evidence URLs (velocity) | 1 | ~70 |
| 23 | POST prism/profiles include=posts since=yesterday, 10 panel creators per market | 2 | ~60 |
| | **Collect total** | | **~801** |
| | **Confirm 05:15, top 10 per market** | | |
| 24 | search/multi on the platforms where the candidate is missing, since=first sighting | 30 | ~90 |
| 25 | tiktok/search/top sort_by=date-posted, same seen id | 20 | ~20 |
| 26 | tiktok/profile/region or prism/profiles for the lead creators | 20 | 20 |
| 27 | prism/creator-card verify=true for 3 originators; label=injection on evidence rows | 3 | ~20 |
| | **Confirm total** | | **~150** |
| | **Reserve** (502/503 are refunded anyway): 6 local-feed calls for breaking items (30); 1 search/everywhere, AI lanes excluded (20) | | **50** |

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

## 5. Gaps and Stage 0 probes

**Gaps in the spec.** No X trends route; no Instagram or Facebook country trending route; the TikTok board is ZA only; TikTok song and hashtag counts are global; no Spotify charts, Boomplay, Audiomack or Shazam; Snapchat, Kwai, Twitch and Kick have no discovery routes; WhatsApp is not covered. seen lasts 24 h. search/news does not list its editions. The cohort price is null. linkedin/search/posts has a price conflict. location_uid has no lookup. TikTok country filters bill before filtering.

**Stage 0 probes (60 credits, in priority order):**

1. tiktok/trending feed=local for ZA, NG, KE (15): in-market share of ext.region; are NG and KE distinct?
2. youtube/videos/trending: NG category 24, ZA with no category, KE category 17 (3). Do non-music categories return?
3. tiktok/hashtags/popular ZA period=7, overall board (6).
4. apple_music/charts for ng and ke (2; free if 400).
5. search/news countries=ZA,NG,KE max_legs=3 (up to 5).
6. twitter/search/tweets with `geocode:-26.20,28.04,50km`, and with `near:Lagos` (2).
7. youtube/search/advanced location=6.52,3.37 location_radius=50km (1).
8. instagram/search/location "Soweto" plus location/posts (10).
9. facebook/events on a Johannesburg explore URL, and facebook/search/posts with a location_uid (2).
10. search/multi with dry_run=1 (0), then one live call (about 6).
11. tiktok/search/top country=KE, called twice with the same seen id (2): the repeat should cost about 0.
12. tiktok/song/videos and tiktok/hashtag (2).
13. prism/post-stats with 3 URLs (3).

Free alongside: credits/transactions (compare each charge with x-credit-cost), balance, status, utility/capabilities.
