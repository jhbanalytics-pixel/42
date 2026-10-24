# 14. SocialCrawl endpoint audit (16 Oct 2026)

Every route in the SocialCrawl catalogue (`docs/full-42/reference/sc_routes.json`, 637 paths, 649 path and method pairs) checked against what the code calls today. The verdicts live in `core/collect/endpoint_audit.py`, and `core/collect/tests/test_endpoint_audit.py` fails when a catalogue route has no verdict, a live route is not priced, or a priced route is neither live nor explained. The table below is its output (`python -m core.collect.endpoint_audit`).

## What the morning run reads

The 02:00 run (`f42-collect`) already covers more than six platforms. TikTok is read through its local feed, hashtag board, country-filtered search, sound and hashtag counters and creator videos; YouTube through its regional charts and Shorts; Instagram through location hubs, music, reel search and audio reels; Reddit through market subreddits; Facebook and X through hub pages and accounts; Apple Music charts; Google Trends as triage only. Row 16, 21 `search/multi` calls a run, searches each seed on six platforms at once: Instagram, YouTube, Reddit, X, Threads and Facebook. That list of six is the limit Albert saw. X and Threads were already searched every morning inside it.

## What this change adds

- Row 16w, the wider net: each market's first three row 16 queries are also searched inside the market's own subreddit (`reddit/subreddit/search` on r/southafrica, r/Nigeria or r/Kenya, 1 credit), on LinkedIn (`search/multi` with `platforms=linkedin`, 5), Bluesky (`bluesky/search`, 1) and Pinterest (`pinterest/search`, 1). The plan holds 24 credits a market, 72 a run.
- Row 16w takes only the room the share has left after every other row and the curated panel, and comes before row 14i. So no earlier lane loses a credit. Row 14i (Instagram reel search with the creator card) gets what is left: one call a run in the 17 Oct plan instead of two. The 16 Oct location audit found those reels mostly foreign (ZA 2 in market against 285 elsewhere).
- At the 2,000 collect share, the planned run for 17 Oct held 1,954 credits, plus 29 for local sources and 15 for Google Trends: 1,998 of 2,000. The curated panel still reads its whole list (155 a market).
- Ask may now call `linkedin/search/posts` and `bluesky/search` directly. Both platforms are already in the answer schema.
- `google_play/app-list` (the Android app chart, one market a day Monday to Wednesday) was planned at 5 credits but never called, because the client had no price for it. It is priced now and runs.

How to judge the new lanes: their posts are search-presence rows placed only from text, like X, Threads and Facebook (9 to 15% placed in the 16 Oct audit). Judge each on its own `collection_health` series after a week: the share of its posts placed in market, and how many reach candidates. Turn one off by setting its count in `WIDE_TOP` to 0.

## Designed but not running

The costed collect table in SOURCES.md plans rows the 17 Oct plan (`python -m core.collect.job --plan`) does not make: row 9 (Telegram channels and `facebook/events` for six cities; Telegram is off until Albert says go, the events call was only ever probed), row 15's `tiktok/search/suggestions`, row 17 (`youtube/search/advanced` on candidates ranked 8 to 10), row 18 (`google_news/search` per market) and rows 19 to 21 (comments and transcripts, which video reading and Ask now do on demand instead). Rows 10 and 13 run as seed templates when a seed calls for them. Row 23b runs only when `x_trends` is on. The remaining room in the 2,000 share is spoken for, so restoring any of these means a cap decision or taking credits from a lane that runs now.

## Worth adding next

Ranked in the `candidate` section below. The strongest are `tiktok/search/suggestions` (what people in each country are typing into TikTok search, 1 credit), `reddit/subreddit/search` (a query run inside r/southafrica, r/Nigeria or r/Kenya, located by where it was asked), `instagram/search/hashtag`, Facebook groups, `facebook/events` and `tiktok/effect/videos` (format trends, the way song/videos tracks a sound). Each needs a probe of its response shape and in-market share, and the collect share has no room left for them.

`python -m core.collect.probe --candidates` is that probe: one call a market to TikTok suggestions, the market's subreddit, Facebook groups and the first group's posts, and Instagram and YouTube Shorts by hashtag, on each market's first research term of the day. 18 calls, 30 credits held, capped at 40 on the build share after the morning schedule; it stops after any call charged above its hold. It prints each route's rows and how many core.detect.geo places in the market, abroad or nowhere. `facebook/events` is left out (the 28 Sept probe returned no Johannesburg events and no Lagos or Nairobi city id is known), and so is `tiktok/effect/videos` (it needs an effect id no stored post carries). A candidate earns a morning lane on its placed-in-market share, against 2 to 15% for X, Facebook, Reddit, YouTube and Threads search in the 16 Oct audit.

Albert ran it on 16 Oct (run probe-candidates-20261016T181157Z, 12 calls, 19 credits). It stopped after NG's Instagram hashtag call returned 404 for #TakeHamPlay and was still charged 5, so KE was not reached. `reddit/subreddit/search` returned 7 posts a search for 1 credit, and every post core.detect.geo could place was in market (2 of 14, none abroad); it is local by construction and joins row 16w. `tiktok/search/suggestions` returned ten phrases a call, market-true for a real term (ZA: "orlando pirates vs sundowns", "orlando pirates jersey") and noise for a term nobody types (NG: "take ham play"); it stays a candidate, to be wired as a term source. `instagram/search/hashtag` placed 3 of 4 in market (ZA) but costs 5 a call and charges for a missing hashtag. `youtube/search/hashtag` (16 Shorts, none placeable; NG empty) and Facebook groups and group posts (none placeable) showed no measurable local share. Those four move to `later` with the measurement as the reason.

## Ruled out, and why

- `search/forums` fuses Reddit, Hacker News and Naver. Reddit is already read at 1 credit a call, Hacker News is US tech and Naver is Korean, so it adds 10 credits a call and no local reach.
- `pinterest/trends`, `tiktok/videos/popular` and `tiktok/ads/top` have no ZA, NG or KE in their country lists.
- Google Trends explore and rising, `prism/trend-board`, `prism/earliness`, Grok search and the other generated-answer routes are excluded by RULES.md rule 2 and SOURCES.md "Never used".
- Followers, likers, audiences, stories and people search are personal data.
- Commerce, reviews, jobs, finance, SEO and ad libraries are skins work, not detection.

649 catalogue routes: 59 live, 8 candidate, 88 later, 71 lookup, 105 never, 57 off_market, 261 out_of_scope.

## live (59)

| Route | Method | Credits | Why |
|---|---|---|---|
| apple_music/charts | GET | 1 | morning row 6, songs and music videos per market |
| bluesky/search | GET | 1 | morning row 16w; Ask |
| credits/balance | GET | 0 | every job: balance floor |
| credits/transactions | GET | 0 | nightly reconciliation (f42-reconcile) |
| facebook/post/comments | GET | 1 | Ask enrichment |
| facebook/post/transcript | GET | 10 | video reading and Ask enrichment |
| facebook/profile/posts | GET | 1 | morning row 8, hub pages; seed template |
| facebook/search/posts | GET | 1 | Ask; Facebook also searched every morning inside row 16 search/multi |
| google_news/search | GET | 1 | Ask |
| google_play/app-list | GET | 5 | local sources, Android chart |
| google_trends/trending | GET | 5 | morning Google Trends phase, query triage only (RULES.md rule 2); Ask |
| instagram/audio/reels | GET | 1 | morning seed template (sounds on both platforms) |
| instagram/location/posts | GET | 5 | morning row 5, place-tagged posts at hub locations |
| instagram/media/transcript | GET | 10 | video reading and Ask enrichment |
| instagram/music/trending | GET | 5 | morning row 4, global |
| instagram/post/comments | GET | 5 | Ask enrichment |
| instagram/profile/about | GET | 1 | morning country phase C1 (account country) |
| instagram/search/reels | GET | 1 | morning row 14i with creator country; Ask |
| linkedin/search/posts | GET | 1 | Ask; LinkedIn searched every morning through row 16w search/multi |
| pinterest/search | GET | 1 | morning row 16w |
| prism/post-stats | POST | 1 | morning row 22, evidence-post velocity |
| prism/profiles | POST | 1 | morning row 23, culture desk and curated creator panels; local sources |
| reddit/post/comments | GET | 5 | Ask enrichment |
| reddit/post/transcript | GET | 10 | video reading and Ask enrichment |
| reddit/search | GET | 1 | Ask |
| reddit/subreddit | GET | 1 | morning row 7, hot and rising on market subreddits |
| reddit/subreddit/search | GET | 1 | morning row 16w, inside r/southafrica, r/Nigeria and r/Kenya. Candidate probe 16 Oct: 7 posts a search at 1 credit, every placed post in market |
| search/everywhere | GET | 20 | Ask, with the generated-answer lanes always excluded |
| search/multi | GET | 0 | morning row 16 (Instagram, YouTube, Reddit, X, Threads, Facebook) and 16w (LinkedIn); confirm; Ask; local sources |
| status | GET | 0 | client: circuit state before a retry |
| threads/post/comments | GET | 1 | Ask enrichment |
| threads/search | GET | 1 | Ask; Threads also searched every morning inside row 16 search/multi |
| tiktok/hashtag | GET | 1 | morning row 12, hashtag counters; Ask; pulse when on |
| tiktok/hashtags/popular | GET | 6 | morning row 2, ZA hashtag board (the board has no NG or KE) |
| tiktok/location/posts | GET | 1 | morning location lane (when a reviewed place id exists) |
| tiktok/post/comments | GET | 1 | Ask enrichment |
| tiktok/post/transcript | GET | 10 | video reading and Ask enrichment |
| tiktok/profile | GET | 1 | morning country phase C1 (creator country) |
| tiktok/profile/videos | GET | 1 | morning seed template (creator breakout baseline) |
| tiktok/search/hashtag | GET | 1 | morning row 14 seed template; Ask |
| tiktok/search/top | GET | 1 | morning row 14, country-filtered search; confirm; Ask |
| tiktok/song | GET | 1 | morning row 12, sound counters; pulse when on |
| tiktok/song/videos | GET | 1 | morning row 11, top sounds; seed template |
| tiktok/trending | GET | 5 | morning row 1, local feed per market (5) |
| tiktok/video/screen-text | GET | 5 | video reading and Ask |
| twitter/search/tweets | GET | 1 | Ask; X also searched every morning inside row 16 search/multi |
| twitter/tweet | GET | 1 | local sources X discovery (one known post) |
| twitter/tweet/replies | GET | 1 | Ask enrichment |
| twitter/tweet/transcript | GET | 10 | video reading and Ask enrichment |
| twitter/user/tweets | GET | 1 | morning row 23a, X hub accounts; seed template |
| utility/capabilities | GET | 0 | route and price checks |
| utility/endpoint | GET | 0 | route and price checks |
| utility/endpoints | GET | 0 | route and price checks |
| web/scrape | GET | 1 | morning row 23b (X trends archive, when x_trends is on); local sources |
| youtube/search/advanced | GET | 1 | morning location lane (radius search, when a reviewed place exists); Ask |
| youtube/shorts/trending | GET | 5 | morning row 4, global |
| youtube/video/comments | GET | 1 | Ask enrichment |
| youtube/video/transcript | GET | 3 | video reading and Ask enrichment |
| youtube/videos/trending | GET | 1 | morning row 3, five categories per market |

## candidate (8)

| Route | Method | Credits | Why |
|---|---|---|---|
| app_store/app-list | GET | 5 | Apple's app chart per storefront (5): the iPhone half of the app chart that google_play/app-list already reads, for ZA where iPhone share is higher. |
| facebook/events | GET | 1 | City events with RSVP counts (1): the cultural calendar from below. Probed on 28 Sept, never scheduled. |
| prism/creator-card | GET | 5 | One handle across 4-7 platforms (5-8): the originator check in the confirm design (research row 27), never built. verify=true is generated and must not be cited. |
| reddit/search/comments | GET | 1 | Reddit comment search (1). Priced and unused: conversation-level evidence for Ask where post search finds only titles. |
| search/news | GET | 2 | Multi-country news in one call (2 + 1 a leg). Probed, not scheduled; would let Ask read NG and KE editions directly. Morning news already comes from GDELT and Google RSS. |
| tiktok/effect/videos | GET | 1 | Videos made with one effect (1): a format trend's spread, the way song/videos tracks a sound. Needs effect ids, which TikTok posts carry. |
| tiktok/search/suggestions | GET | 1 | TikTok autocomplete per region (1). Market-true phrases people are typing; a seed source for row 14 that needs no news trigger. Candidate probe 16 Oct: ten phrases a call, local for a real term (ZA 'orlando pirates vs sundowns'), noise for a term nobody types (NG 'take ham play'). Next: wire as a term source. |
| tiktok/search/users | GET | 1 | TikTok creators by account country (1, 5 a row with country=, enum has ZA, NG, KE): a weekly build of the creator panels the morning run reads. |

## later (88)

| Route | Method | Credits | Why |
|---|---|---|---|
| bluesky/user/posts | GET | 1 | A Bluesky account's posts (1): for a hub panel, after row 16w shows Bluesky's reach. |
| cohort-queries/{queryId} | DELETE | 0 | As cohorts. |
| cohort-queries/{queryId} | GET | 0 | As cohorts. |
| cohort-queries/{queryId}/results | GET | 0 | As cohorts. |
| cohorts | POST | 0 | Creator panels of up to 10,000 handles; price not published (x-credit-cost null). Probe first. |
| cohorts/{cohortId} | DELETE | 0 | As cohorts. |
| cohorts/{cohortId} | GET | 0 | As cohorts. |
| cohorts/{cohortId}/members | PUT | 0 | As cohorts. |
| cohorts/{cohortId}/queries | POST |  | As cohorts. |
| content_analysis/search | GET | 20 | Web citations with sentiment (20). Skins only; sentiment is generated. |
| facebook/event/details | GET | 1 | One event's details (1), after facebook/events. |
| facebook/events/search | GET | 1 | Events by keyword (1), after facebook/events. |
| facebook/group/posts | GET | 1 | Probed 16 Oct: 4 posts a group, none placeable by country. Follows facebook/search/groups. |
| facebook/profile/events | GET | 1 | A page's events (1), after facebook/events. |
| facebook/profile/reels | GET | 1 | A page's reels (1). Hub pages are read through profile/posts. |
| facebook/search/groups | GET | 1 | Probed 16 Oct: 4 to 7 groups a search, none placeable by country from the row. Revisit with a group list chosen by hand. |
| facebook/search/pages | GET | 1 | Page search (1): finding hub pages, a manual review step. |
| facebook/search/videos | GET | 1 | Facebook video search (1). Facebook search already runs inside row 16. |
| google_play/app-search | GET | 5 | App search (5). Charts cover what is rising. |
| hackernews/search | GET | 1 | Tech discussion (1), US-centred. Off-market for cultural detection; a Gamma tech-skin candidate at most. |
| instagram/profile/posts | GET | 1 | An account's posts (1). Accounts are read through prism/profiles. |
| instagram/profile/reels | GET | 1 | An account's reels (1). Accounts are read through prism/profiles. |
| instagram/reels/trending | GET | 5 | Global trending reels with null views: no market and no counts. |
| instagram/search | GET | 1 | Accounts, hashtags and places by name (1): a lookup for picking hubs, done by hand. |
| instagram/search/hashtag | GET | 5 | Probed 16 Oct: 3 of 4 placed posts in market (ZA), but 5 credits a call and a missing hashtag is still charged 5 (NG 404). Reel search covers Instagram for less. |
| instagram/search/location | GET | 5 | Finds place ids (5). Used by hand to pick row 5 hubs; ZA needs new busier ones. |
| instagram/search/music | GET | 5 | Instagram sound search (5). Sounds arrive from music/trending and audio/reels. |
| instagram/search/popular | GET | 1 | Popular posts for a query (1). Instagram search runs through reels and multi. |
| instagram/search/profiles | GET | 1 | Account search (1): panel building, a manual review step. |
| instagram/tagged | GET | 5 | Posts an account is tagged in (5). Probed; a brand-skin tool. |
| kick/clip | GET | 1 | no discovery route; clip reads only |
| komi/page | GET | 1 | link-in-bio pages: creator enrichment, research LATER |
| kwai/post | GET | 1 | no discovery route; profile reads only |
| kwai/profile | GET | 1 | no discovery route; profile reads only |
| kwai/user/posts | GET | 1 | No discovery route; profile reads only. Kwai is small in these markets. |
| linkbio/page | GET | 1 | link-in-bio pages: creator enrichment, research LATER |
| linkedin/search/hashtag | GET | 10 | LinkedIn posts by hashtag (10). search/posts at 5 covers the same posts by query. |
| linkme/page | GET | 1 | link-in-bio pages: creator enrichment, research LATER |
| linktree/page | GET | 1 | link-in-bio pages: creator enrichment, research LATER |
| monitors | GET | 0 | Hourly replays for the intraday pulse. The pulse is off until Albert says go (PULSE_READY). |
| monitors | POST | 0 | Hourly replays for the intraday pulse. The pulse is off until Albert says go (PULSE_READY). |
| monitors/{id} | DELETE | 0 | As monitors. |
| monitors/{id} | GET | 0 | As monitors. |
| monitors/{id} | PATCH | 0 | As monitors. |
| monitors/{id}/export | GET | 0 | As monitors. |
| monitors/{id}/runs | GET | 0 | As monitors. |
| monitors/{id}/timeseries | GET | 0 | As monitors. |
| pillar/page | GET | 1 | link-in-bio pages: creator enrichment, research LATER |
| pinterest/url-stats | GET | 1 | Pinterest saves for external URLs (1): a velocity read, after row 16w shows reach. |
| prism/comments | GET | 2 | Every comment on a post, paginated (2). Ask enrichment uses the native comment routes. |
| prism/format-lift | GET | 4 | Which hook works for a creator (4). A skin tool. |
| prism/jobs | GET | 0 | Batches of 5,000 profiles or URLs. Morning batches fit prism/profiles and post-stats today. |
| prism/jobs | POST | 1 | Batches of 5,000 profiles or URLs. Morning batches fit prism/profiles and post-stats today. |
| prism/jobs/{job_id} | GET | 0 | As prism/jobs. |
| prism/lookup | GET | 0 | Resolves a pasted URL at its route's price. Ask could use it for pasted links. |
| prism/mentions | GET | 1 | Posts mentioning a handle (1-5). Probed; a brand-skin tool, not detection. |
| prism/video-intel | GET | 5 | One video's detail, stats, transcript and comments (5). Its tone blocks are generated. |
| quora/search | GET | 5 | Quora questions (5). Nigeria is one of Quora's larger audiences, so this is the off-market route most worth a probe; unmeasured. |
| reddit/omni-search | GET | 5 | One keyword across all of Reddit with subreddit context (5). Ask candidate once the 1-credit reddit/search proves too thin. |
| reddit/search/media | GET | 1 | Reddit image and video search (1). Reddit search already runs in Ask. |
| reddit/subreddits/search | GET | 1 | Find subreddits by topic (1): grows the subreddit list, a manual review step. |
| rumble/search | GET | 1 | Rumble search (1). Its audience is US-centred; no measured local reach. |
| search/creators | GET | 10 | Fused creator discovery (10). Priced; useful for panel building after tiktok/search/users. |
| search/forums | GET | 10 | Fused Reddit, Hacker News and Naver (10). Reddit is already read directly at 1 a call; Hacker News is off-market and Naver is Korean, so it adds cost, not reach. |
| snapchat/profile | GET | 1 | no discovery route; Spotlight has no search or trending read |
| snapchat/spotlight/comments | GET | 1 | no discovery route; Spotlight has no search or trending read |
| spotify/search | GET | 1 | Spotify search (1). Spotify has no chart route, so it gives no ranking. |
| telegram/post | GET | 1 | Telegram is off until Albert says go (TELEGRAM_READY) |
| telegram/profile | GET | 1 | Telegram is off until Albert says go (TELEGRAM_READY) |
| telegram/profile/posts | GET | 1 | Curated Telegram channels. Off until Albert says go (TELEGRAM_READY). |
| threads/search/users | GET | 1 | Account search (1): panel building, a manual review step. |
| threads/user/posts | GET | 1 | A Threads account's posts (1): for a Threads hub panel, which does not exist yet. |
| tiktok/profile/region | GET | 1 | Creator country (1). Priced; the country phase reads tiktok/profile, which also returns it. |
| tiktok/search | GET | 1 | Plain TikTok search (1). The morning run uses tiktok/search/top, which filters by country. |
| tiktok/search/music | GET | 1 | Sound search (1). Priced; sounds arrive from feeds and boards with their ids. |
| twitch/clip | GET | 1 | no discovery route; profile reads only |
| twitch/profile | GET | 1 | no discovery route; profile reads only |
| twitch/user/schedule | GET | 1 | no discovery route; profile reads only |
| twitch/user/videos | GET | 1 | No discovery route; profile reads only. |
| twitter/community/tweets | GET | 1 | X Communities (1). Research LATER: needs a curated list of local communities. |
| twitter/search/users | GET | 1 | Account search (1): panel building, a manual review step. |
| youtube/channel/community-posts | GET | 1 | Channel community posts (1). No channel panel reads them yet. |
| youtube/channel/shorts | GET | 1 | A channel's Shorts (1). Channels are read through prism/profiles. |
| youtube/channel/videos | GET | 1 | A channel's videos (1). Channels are read through prism/profiles. |
| youtube/search | GET | 1 | Plain YouTube search (1). The morning run and Ask use youtube/search/advanced, which takes region and exact dates. |
| youtube/search/hashtag | GET | 1 | Probed 16 Oct: 16 Shorts for ZA none placeable by country, NG empty. Not worth a lane until a placed share can be shown. |
| youtube/search/suggestions | GET | 1 | YouTube autocomplete per region (1); after tiktok/search/suggestions proves the idea. |
| youtube/transcripts | POST | 3 | Batch transcripts (3 each, up to 100). Same price as the per-video route video reading uses; worth switching only when a run reads many YouTube clips at once. |

## lookup (71)

| Route | Method | Credits | Why |
|---|---|---|---|
| apple_music/album | GET | 1 | single album, artist, track or search reads; charts are live |
| apple_music/artist | GET | 1 | single album, artist, track or search reads; charts are live |
| apple_music/search | GET | 1 | single album, artist, track or search reads; charts are live |
| apple_music/track | GET | 1 | single album, artist, track or search reads; charts are live |
| bluesky/post | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| bluesky/profile | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| facebook/group | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| facebook/post | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| facebook/post/comment/replies | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| facebook/profile | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| facebook/profile/full | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| facebook/profile/photos | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| facebook/profile/reels/full | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| instagram/basic-profile | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/comment | GET | 5 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/engagement | GET | 5 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/highlight/detail | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/highlights | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/media/screen-text | GET | 5 | one known post's speech or text; video reading covers the six main platforms |
| instagram/post | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/post/comment/replies | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/post/stats | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| instagram/profile | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/profile/full | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| instagram/profile/posts/full | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| instagram/profile/reels/full | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| instagram/similar | GET | 5 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| instagram/user/embed | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| pinterest/board | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| pinterest/pin | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| pinterest/user/boards | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| reddit/post | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| reddit/profile | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| reddit/profile/comments | GET | 2 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| reddit/profile/posts | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| reddit/subreddit/details | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| spotify/album | GET | 1 | Spotify has no chart route; Apple Music charts are read instead |
| spotify/artist | GET | 1 | Spotify has no chart route; Apple Music charts are read instead |
| spotify/podcast | GET | 1 | Spotify has no chart route; Apple Music charts are read instead |
| spotify/podcast/episodes | GET | 1 | Spotify has no chart route; Apple Music charts are read instead |
| spotify/track | GET | 1 | Spotify has no chart route; Apple Music charts are read instead |
| threads/post | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| threads/profile | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| tiktok/collection/videos | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| tiktok/comment | GET | 2 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| tiktok/effects | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| tiktok/playlist/videos | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| tiktok/post | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| tiktok/profile/full | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| tiktok/profile/playlists | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| tiktok/video/comment/replies | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| twitter/community | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| twitter/profile | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| twitter/profile/full | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| twitter/user/media | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/channel | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/channel/lives | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/channel/playlists | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/channels | POST | 5 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/community-post | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/playlist | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/playlist/items | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/profile/full | GET | 5 | a richer read of one known account or post at 5; prism/profiles and post-stats cover it |
| youtube/video | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/video/audio | GET | 5 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/video/comment/replies | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/video/files | GET | 5 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/video/sponsors | GET | 10 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/video/subtitles | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/video/thumbnails | GET | 1 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |
| youtube/videos | POST | 5 | reads one known object; 42 opens objects through post-stats, profiles and the Ask routes |

## never (105)

| Route | Method | Credits | Why |
|---|---|---|---|
| facebook/search/people | GET | 1 | followers, likers, audiences or private-feeling account data: personal data rule |
| google/search | GET | 1 | Web search results are never cultural evidence (research NEVER). |
| google_trends/explore | GET | 5 | RULES.md rule 2: explore and rising stay parked; trending is triage only. |
| google_trends/rising | GET | 5 | RULES.md rule 2, as google_trends/explore. |
| instagram/followers | GET | 5 | followers, likers, audiences or private-feeling account data: personal data rule |
| instagram/following | GET | 5 | followers, likers, audiences or private-feeling account data: personal data rule |
| instagram/post/likers | GET | 5 | followers, likers, audiences or private-feeling account data: personal data rule |
| instagram/stories | GET | 5 | followers, likers, audiences or private-feeling account data: personal data rule |
| instagram/story/download | GET | 5 | followers, likers, audiences or private-feeling account data: personal data rule |
| instagram/username-suggestions | GET | 5 | followers, likers, audiences or private-feeling account data: personal data rule |
| linkedin/ad | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/ads/search | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/article | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/article/comments | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/article/reactions | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/affiliated-pages | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/also-viewed | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/by-domain | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/employees-count | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/insights | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/job-count | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/jobs | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/people | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/company/posts | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/group | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/group/posts | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/job | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/job/hiring-team | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/post | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/post/comments | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/post/comments/replies | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/post/reactions | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/post/reposts | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/post/transcript | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/post/with-comments | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/about | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/activity | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/all | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/articles | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/certifications | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/comments | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/complete | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/contact | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/educations | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/experiences | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/full | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/honors | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/images | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/interests/companies | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/interests/groups | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/interests/newsletters | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/interests/schools | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/interests/top-voices | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/position-skills | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/posted-jobs | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/posts | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/posts/archive | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/publications | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/reactions | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/recommendations | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/similar | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/skills | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/stats | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/top-position | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/videos | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/volunteers | GET | 5 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/profile/with-posts | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/search/companies | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/search/industry | GET | 1 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/search/jobs | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/search/location | GET | 1 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/search/people | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/search/people/by-url | GET | 10 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| linkedin/search/schools | GET | 1 | people, jobs, companies, schools and ads: research verdict NEVER (only search/posts is used) |
| perplexity/research | GET | 1 | Generated answers are never evidence. |
| pinterest/trends | GET | 10 | Its 23-country enum has no ZA, NG or KE (search is used instead, row 16w). |
| polymarket/research | GET | 5 | A betting market, never cultural evidence. |
| prism/adverse-screen | GET | 25 | Screens people: personal data, never. |
| prism/ai-visibility | GET | 2 | SOURCES.md Never used: generated answers. |
| prism/answers | GET | 15 | SOURCES.md Never used: generated multi-engine answers. |
| prism/audience-language | GET | 2 | SOURCES.md Never used: opens the audience sample (demographics rule). |
| prism/audience-overlap | GET | 20 | Commenter audiences of named creators: personal data at scale. |
| prism/crisis-postmortem | GET | 35 | Generated timeline; research NEVER. |
| prism/earliness | GET | 25 | SOURCES.md Never used: includes a Google Trends lane. |
| prism/investigate | GET | 1 | SOURCES.md Never used: an LLM walk. |
| prism/trend-board | GET | 30 | SOURCES.md Never used: every call pulls the Google Trends lane; its parts cost less bought directly, and its hashtag board is empty for NG and KE. |
| tavily/crawl | GET | 1 | generated answers |
| tavily/extract | GET | 1 | generated answers |
| tavily/map | GET | 1 | generated answers |
| tavily/search | GET | 1 | Generated answers are never evidence. |
| tiktok/ads/top | GET | 10 | Its country enum has no ZA, NG or KE. |
| tiktok/user/audience | GET | 5 | SOURCES.md Never used: audience demographics. |
| tiktok/user/followers | GET | 1 | followers, likers, audiences or private-feeling account data: personal data rule |
| tiktok/user/following | GET | 1 | followers, likers, audiences or private-feeling account data: personal data rule |
| tiktok/user/liked | GET | 1 | followers, likers, audiences or private-feeling account data: personal data rule |
| tiktok/user/live | GET | 1 | followers, likers, audiences or private-feeling account data: personal data rule |
| tiktok/videos/popular | GET | 26 | Its board covers the US, Japan, Vietnam, Thailand and Indonesia only. |
| twitter/ai-search | GET | 5 | SOURCES.md Never used: a Grok answer, generated. |
| twitter/tweet/retweeters | GET | 1 | followers, likers, audiences or private-feeling account data: personal data rule |
| twitter/user/followers | GET | 1 | followers, likers, audiences or private-feeling account data: personal data rule |
| twitter/user/following | GET | 1 | followers, likers, audiences or private-feeling account data: personal data rule |
| web/agent | POST | 25 | An LLM web agent; research NEVER. |
| youtube/channel/about | GET | 25 | 25 credits and returns contact emails: personal data. |

## off_market (57)

| Route | Method | Credits | Why |
|---|---|---|---|
| douyin/comment/replies | GET | 5 | China |
| douyin/post | GET | 10 | China |
| douyin/post/comments | GET | 2 | China |
| douyin/profile | GET | 6 | China |
| douyin/profile/posts | GET | 5 | China |
| douyin/search | GET | 5 | China |
| douyin/search/users | GET | 5 | China |
| douyin/trending | GET | 25 | China |
| github/issue | GET | 1 | code hosting |
| github/issue/comments | GET | 1 | code hosting |
| github/profile | GET | 1 | code hosting |
| github/profile/repos | GET | 1 | code hosting |
| github/repo | GET | 1 | code hosting |
| github/repo/dossier | GET | 5 | code hosting |
| github/repo/issues | GET | 1 | code hosting |
| github/repo/readme | GET | 1 | code hosting |
| github/repo/releases | GET | 1 | code hosting |
| github/repo/top-issues | GET | 5 | code hosting |
| github/search | GET | 1 | code hosting |
| github/user/profile-velocity | GET | 10 | code hosting |
| hackernews/profile | GET | 1 | US tech discussion |
| hackernews/story | GET | 1 | US tech discussion |
| hackernews/story/comments | GET | 1 | US tech discussion |
| naver/adult | GET | 1 | Korea |
| naver/blog/search | GET | 1 | Korea |
| naver/brief | GET | 10 | Korea |
| naver/cafearticle/search | GET | 1 | Korea |
| naver/encyc/search | GET | 1 | Korea |
| naver/errata | GET | 1 | Korea |
| naver/image/search | GET | 1 | Korea |
| naver/kin/search | GET | 1 | Korea |
| naver/local/search | GET | 1 | Korea |
| naver/news/search | GET | 1 | Korea |
| naver/search-trend | GET | 5 | Korea |
| naver/shopping-insight/category | GET | 5 | Korea |
| naver/shopping-insight/keyword | GET | 5 | Korea |
| naver/webkr/search | GET | 1 | Korea |
| producthunt/launches | GET | 1 | US product launches |
| quora/answers | GET | 5 | unmeasured local reach; quora/search is the one to probe |
| quora/post | GET | 5 | unmeasured local reach; quora/search is the one to probe |
| quora/posts | GET | 5 | unmeasured local reach; quora/search is the one to probe |
| quora/profiles | GET | 5 | unmeasured local reach; quora/search is the one to probe |
| quora/spaces | GET | 5 | unmeasured local reach; quora/search is the one to probe |
| quora/topics | GET | 5 | unmeasured local reach; quora/search is the one to probe |
| rumble/channel/videos | GET | 1 | US-centred video; no measured local reach |
| rumble/video | GET | 1 | US-centred video; no measured local reach |
| rumble/video/comments | GET | 1 | US-centred video; no measured local reach |
| rumble/video/transcript | GET | 10 | US-centred video; no measured local reach |
| truthsocial/post | GET | 1 | US political network |
| truthsocial/profile | GET | 1 | US political network |
| truthsocial/user/posts | GET | 1 | US political network |
| xiaohongshu/post | GET | 5 | China |
| xiaohongshu/post/comments | GET | 5 | China |
| xiaohongshu/profile | GET | 5 | China |
| xiaohongshu/profile/posts | GET | 5 | China |
| xiaohongshu/search | GET | 5 | China |
| xiaohongshu/trending | GET | 5 | China |

## out_of_scope (261)

| Route | Method | Credits | Why |
|---|---|---|---|
| aliexpress/categories | GET | 1 | commerce |
| aliexpress/product | GET | 5 | commerce |
| aliexpress/product/shipping | GET | 5 | commerce |
| aliexpress/product/similar | GET | 5 | commerce |
| aliexpress/promo | GET | 1 | commerce |
| aliexpress/reviews | GET | 5 | commerce |
| aliexpress/search | GET | 5 | commerce |
| aliexpress/search/hot | GET | 5 | commerce |
| aliexpress/search/promo | GET | 5 | commerce |
| amazon/best-sellers | GET | 1 | commerce; best-sellers has no ZA |
| amazon/deals | GET | 1 | commerce; best-sellers has no ZA |
| amazon/product | GET | 5 | commerce; best-sellers has no ZA |
| amazon/product-search | GET | 1 | commerce; best-sellers has no ZA |
| amazon/reviews | GET | 5 | commerce; best-sellers has no ZA |
| amazon/seller | GET | 1 | commerce; best-sellers has no ZA |
| amazon/sellers | GET | 1 | commerce; best-sellers has no ZA |
| amazon/shop | GET | 1 | commerce; best-sellers has no ZA |
| app_store/app-info | GET | 5 | app details, reviews and listings; app-list is the candidate |
| app_store/app-listings-search | GET | 10 | app details, reviews and listings; app-list is the candidate |
| app_store/app-reviews | GET | 5 | app details, reviews and listings; app-list is the candidate |
| app_store/app-search | GET | 5 | app details, reviews and listings; app-list is the candidate |
| app_store/categories | GET | 1 | app details, reviews and listings; app-list is the candidate |
| app_store/languages | GET | 1 | app details, reviews and listings; app-list is the candidate |
| app_store/locations | GET | 1 | app details, reviews and listings; app-list is the candidate |
| app_store/search-suggestions | GET | 1 | app details, reviews and listings; app-list is the candidate |
| content_analysis/categories | GET | 1 | generated sentiment at 20 a call; skins only |
| content_analysis/category-trends | GET | 20 | generated sentiment at 20 a call; skins only |
| content_analysis/filters | GET | 1 | generated sentiment at 20 a call; skins only |
| content_analysis/languages | GET | 1 | generated sentiment at 20 a call; skins only |
| content_analysis/locations | GET | 1 | generated sentiment at 20 a call; skins only |
| content_analysis/phrase-trends | GET | 20 | generated sentiment at 20 a call; skins only |
| content_analysis/rating-distribution | GET | 20 | generated sentiment at 20 a call; skins only |
| content_analysis/sentiment | GET | 20 | generated sentiment at 20 a call; skins only |
| content_analysis/summary | GET | 20 | generated sentiment at 20 a call; skins only |
| ebay/product | GET | 5 | commerce |
| ebay/search | GET | 5 | commerce |
| etsy/product | GET | 5 | commerce |
| etsy/product/similar | GET | 5 | commerce |
| etsy/search/suggestions | GET | 5 | commerce |
| etsy/shop/products | GET | 5 | commerce |
| facebook/adlibrary/ad | GET | 5 | ad libraries: brand skins, not detection |
| facebook/adlibrary/ad/transcript | GET | 10 | ad libraries: brand skins, not detection |
| facebook/adlibrary/company/ads | GET | 5 | ad libraries: brand skins, not detection |
| facebook/adlibrary/search/ads | GET | 5 | ad libraries: brand skins, not detection |
| facebook/adlibrary/search/companies | GET | 5 | ad libraries: brand skins, not detection |
| facebook/marketplace/item | GET | 1 | commerce (research NEVER) |
| facebook/marketplace/location/search | GET | 1 | commerce (research NEVER) |
| facebook/marketplace/search | GET | 1 | commerce (research NEVER) |
| finance/history | GET | 1 | markets data |
| finance/markets | GET | 1 | markets data |
| finance/news | GET | 1 | markets data |
| finance/options | GET | 5 | markets data |
| finance/quote | GET | 5 | markets data |
| finance/statements | GET | 5 | markets data |
| finance/ticker-search | GET | 1 | markets data |
| g2/categories | GET | 1 | software reviews |
| g2/category | GET | 5 | software reviews |
| g2/product | GET | 5 | software reviews |
| g2/product-index | GET | 5 | software reviews |
| g2/reviews | GET | 5 | software reviews |
| g2/seller | GET | 5 | software reviews |
| g2/seller/products | GET | 5 | software reviews |
| google/ad | GET | 5 | ads, business profiles and hotels |
| google/adlibrary/advertisers/search | GET | 5 | ads, business profiles and hotels |
| google/business/extended-reviews | GET | 5 | ads, business profiles and hotels |
| google/business/info | GET | 1 | ads, business profiles and hotels |
| google/business/questions | GET | 5 | ads, business profiles and hotels |
| google/business/updates | GET | 1 | ads, business profiles and hotels |
| google/company/ads | GET | 5 | ads, business profiles and hotels |
| google/hotels/info | GET | 5 | ads, business profiles and hotels |
| google/hotels/search | GET | 1 | ads, business profiles and hotels |
| google_play/app-info | GET | 5 | app details, reviews and listings; the chart is live |
| google_play/app-listings-search | GET | 10 | app details, reviews and listings; the chart is live |
| google_play/app-reviews | GET | 5 | app details, reviews and listings; the chart is live |
| google_play/categories | GET | 1 | app details, reviews and listings; the chart is live |
| google_play/languages | GET | 1 | app details, reviews and listings; the chart is live |
| google_play/locations | GET | 1 | app details, reviews and listings; the chart is live |
| google_play/search-suggestions | GET | 1 | app details, reviews and listings; the chart is live |
| google_shopping/price-history | GET | 1 | commerce |
| google_shopping/product | GET | 1 | commerce |
| google_shopping/product-search | GET | 5 | commerce |
| google_shopping/reviews | GET | 1 | commerce |
| google_shopping/sellers | GET | 1 | commerce |
| gumtree/categories | GET | 1 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/filters | GET | 1 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/locations | GET | 5 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/locations/nearest | GET | 5 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/product | GET | 5 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/product/similar | GET | 5 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/search | GET | 5 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/search/suggestions | GET | 5 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/seller | GET | 5 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/seller/listings | GET | 5 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| gumtree/trending | GET | 1 | classifieds (large in ZA, but listings are commerce, not culture; a skin source at most) |
| hm/categories | GET | 1 | commerce |
| hm/countries | GET | 1 | commerce |
| hm/product/suppliers | GET | 5 | commerce |
| hm/search | GET | 5 | commerce |
| hm/search/suggestions | GET | 5 | commerce |
| hm/stores | GET | 5 | commerce |
| home_depot/product | GET | 5 | US commerce |
| home_depot/reviews | GET | 5 | US commerce |
| home_depot/search | GET | 5 | US commerce |
| home_depot/stores | GET | 1 | US commerce |
| jobs/bing | GET | 5 | job listings |
| jobs/bing/search | GET | 10 | job listings |
| jobs/indeed | GET | 5 | job listings |
| jobs/indeed/search | GET | 10 | job listings |
| jobs/linkedin | GET | 5 | job listings |
| jobs/linkedin/organizations | GET | 1 | job listings |
| jobs/linkedin/search | GET | 10 | job listings |
| jobs/salary | GET | 1 | job listings |
| jobs/salary/titles | GET | 1 | job listings |
| jobs/xing | GET | 5 | job listings |
| jobs/xing/search | GET | 10 | job listings |
| klarna/categories | GET | 1 | commerce |
| klarna/categories/children | GET | 1 | commerce |
| klarna/category | GET | 5 | commerce |
| klarna/category/filters | GET | 1 | commerce |
| klarna/category/guide | GET | 1 | commerce |
| klarna/category/keywords | GET | 1 | commerce |
| klarna/product | GET | 5 | commerce |
| klarna/product/compare | GET | 5 | commerce |
| klarna/product/offers | GET | 5 | commerce |
| klarna/product/price-history | GET | 5 | commerce |
| klarna/reviews | GET | 5 | commerce |
| klarna/reviews/overview | GET | 5 | commerce |
| klarna/reviews/pro | GET | 5 | commerce |
| klarna/search | GET | 5 | commerce |
| klarna/search/suggestions | GET | 1 | commerce |
| klarna/store/filters | GET | 1 | commerce |
| klarna/store/products | GET | 5 | commerce |
| klarna/stores | GET | 1 | commerce |
| kohls/categories | GET | 1 | US commerce |
| kohls/questions | GET | 5 | US commerce |
| kohls/reviews | GET | 5 | US commerce |
| kohls/search | GET | 5 | US commerce |
| kohls/stores | GET | 5 | US commerce |
| on_page/page | GET | 1 | SEO |
| prism/app-reviews | GET | 10 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/apps-lookup | GET | 30 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/audience-questions | GET | 30 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/brand-mentions | GET | 50 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/brief-check | GET | 15 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/campaign | GET | 35 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/comment-leads | GET | 0 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/comment-lookup | POST | 2 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/creator-vet | GET | 50 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/crisis-radar | GET | 15 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/demand-signals | GET | 30 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/devtool-pulse | GET | 20 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/earned-media | GET | 25 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/employer-brand | GET | 30 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/find-accounts | GET | 2 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/handle-audit | GET | 5 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/korea-gap | GET | 15 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/launch-echo | GET | 20 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/leads | GET | 50 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/org-radar | GET | 6 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/product-reviews | GET | 30 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/reputation | GET | 30 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/review-integrity | GET | 30 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/share-of-voice | GET | 20 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/truthsocial-pulse | GET | 20 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| prism/voice | GET | 5 | brand, campaign, commerce or developer intelligence for skins, not detection (research section 3) |
| sephora/availability | GET | 5 | commerce |
| sephora/brand/products | GET | 5 | commerce |
| sephora/brands | GET | 1 | commerce |
| sephora/categories | GET | 1 | commerce |
| sephora/category | GET | 5 | commerce |
| sephora/category/data | GET | 1 | commerce |
| sephora/product | GET | 5 | commerce |
| sephora/reviews | GET | 5 | commerce |
| sephora/search | GET | 5 | commerce |
| sephora/search/suggestions | GET | 1 | commerce |
| sephora/stores | GET | 1 | commerce |
| target/categories | GET | 1 | US commerce |
| target/category | GET | 5 | US commerce |
| target/product | GET | 5 | US commerce |
| target/reviews | GET | 5 | US commerce |
| target/stores | GET | 1 | US commerce |
| tiktok/adlibrary/ad | GET | 5 | ad libraries: brand skins, not detection |
| tiktok/adlibrary/search | GET | 5 | ad libraries: brand skins, not detection |
| tiktokshop/product | GET | 1 | commerce |
| tiktokshop/product/reviews | GET | 1 | commerce |
| tiktokshop/products | GET | 1 | commerce |
| tiktokshop/search | GET | 1 | commerce |
| tiktokshop/user/showcase | GET | 1 | commerce |
| tripadvisor/attraction | GET | 1 | reviews |
| tripadvisor/attraction/reviews | GET | 1 | reviews |
| tripadvisor/attractions | GET | 1 | reviews |
| tripadvisor/autocomplete | GET | 1 | reviews |
| tripadvisor/cruise | GET | 1 | reviews |
| tripadvisor/cruise/reviews | GET | 1 | reviews |
| tripadvisor/cruises | GET | 1 | reviews |
| tripadvisor/experience-types | GET | 1 | reviews |
| tripadvisor/hotel | GET | 1 | reviews |
| tripadvisor/hotels | GET | 1 | reviews |
| tripadvisor/place | GET | 1 | reviews |
| tripadvisor/restaurant | GET | 1 | reviews |
| tripadvisor/restaurant/reviews | GET | 1 | reviews |
| tripadvisor/restaurants | GET | 1 | reviews |
| tripadvisor/reviews | GET | 1 | reviews |
| tripadvisor/search | GET | 1 | reviews |
| trustpilot/business-search | GET | 1 | reviews |
| trustpilot/reviews | GET | 5 | reviews |
| us_congress_trades/members | GET | 1 | US finance |
| us_congress_trades/politician | GET | 1 | US finance |
| us_congress_trades/politician/trades | GET | 1 | US finance |
| us_congress_trades/state/trades | GET | 1 | US finance |
| us_congress_trades/stats | GET | 1 | US finance |
| us_congress_trades/stats/buy-sell | GET | 1 | US finance |
| us_congress_trades/stats/issuers | GET | 1 | US finance |
| us_congress_trades/stats/party | GET | 1 | US finance |
| us_congress_trades/stats/politicians | GET | 1 | US finance |
| us_congress_trades/stats/reporting-gaps | GET | 1 | US finance |
| us_congress_trades/stats/sectors | GET | 1 | US finance |
| us_congress_trades/stats/tickers | GET | 1 | US finance |
| us_congress_trades/stats/unusual | GET | 1 | US finance |
| us_congress_trades/stats/volume | GET | 1 | US finance |
| us_congress_trades/ticker | GET | 1 | US finance |
| us_congress_trades/ticker/trades | GET | 1 | US finance |
| us_congress_trades/trades | GET | 1 | US finance |
| us_congress_trades/trades/latest | GET | 1 | US finance |
| us_congress_trades/trades/recent | GET | 1 | US finance |
| utility/llms | GET | 0 | account plumbing; the three 42 needs are live |
| utility/plan | GET | 0 | account plumbing; the three 42 needs are live |
| utility/quickstart | GET | 0 | account plumbing; the three 42 needs are live |
| walmart/category | GET | 5 | US commerce |
| walmart/offers | GET | 5 | US commerce |
| walmart/product | GET | 5 | US commerce |
| walmart/reviews | GET | 5 | US commerce |
| walmart/search | GET | 5 | US commerce |
| wayfair/product | GET | 5 | commerce |
| wayfair/reviews | GET | 5 | commerce |
| wayfair/search | GET | 5 | commerce |
| web/batch-scrape | POST | 1 | web crawling and extraction; web/scrape covers the pages 42 reads |
| web/crawl | POST | 1 | web crawling and extraction; web/scrape covers the pages 42 reads |
| web/extract | GET | 5 | web crawling and extraction; web/scrape covers the pages 42 reads |
| web/jobs | GET | 0 | async web jobs; web/scrape covers the pages 42 reads |
| web/jobs/{job_id} | DELETE | 0 | async web jobs; web/scrape covers the pages 42 reads |
| web/jobs/{job_id} | GET | 0 | async web jobs; web/scrape covers the pages 42 reads |
| web/map | GET | 1 | web crawling and extraction; web/scrape covers the pages 42 reads |
| web/monitors | GET | 0 | page-change monitors for skins, not detection |
| web/monitors | POST | 0 | page-change monitors for skins, not detection |
| web/monitors/{monitor_id} | DELETE | 0 | page-change monitors for skins, not detection |
| web/monitors/{monitor_id} | GET | 0 | page-change monitors for skins, not detection |
| web/monitors/{monitor_id} | PATCH | 0 | page-change monitors for skins, not detection |
| web/monitors/{monitor_id}/checks | GET | 0 | page-change monitors for skins, not detection |
| web/parse | POST | 1 | web crawling and extraction; web/scrape covers the pages 42 reads |
| web/search | GET | 2 | web crawling and extraction; web/scrape covers the pages 42 reads |
| web/sessions | GET | 0 | interactive browser sessions; web/scrape covers the pages 42 reads |
| web/sessions | POST | 5 | interactive browser sessions; web/scrape covers the pages 42 reads |
| web/sessions/{session_id} | DELETE | 0 | interactive browser sessions; web/scrape covers the pages 42 reads |
| web/sessions/{session_id} | GET | 0 | interactive browser sessions; web/scrape covers the pages 42 reads |
| web/sessions/{session_id}/execute | POST | 0 | interactive browser sessions; web/scrape covers the pages 42 reads |
| yelp/business/info | GET | 1 | reviews |
| yelp/business/reviews | GET | 5 | reviews |
| yelp/search | GET | 1 | reviews |
| yelp/search/full | GET | 1 | reviews |
| yelp/search/suggestions | GET | 1 | reviews |

