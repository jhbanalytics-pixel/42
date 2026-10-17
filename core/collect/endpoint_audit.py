"""Every SocialCrawl endpoint in the catalogue (docs/full-42/reference/sc_routes.json), with what 42 does with it.

Each route gets one verdict and a reason, so an endpoint 42 does not call is a decision on record, not an oversight:

    live        called today by a job or by Ask; where says which
    candidate   not called yet, and worth adding; the reason says what it would add and what has to happen first
    later       parked on purpose until something else exists (a lane, a flag Albert holds, a probe)
    lookup      reads one known object (a post, profile, album or page); 42 opens objects through post-stats,
                profiles and the Ask detail routes it already has, so these add no reach
    never       ruled out: a RULES.md or SOURCES.md rule, generated answers, demographics or personal data
    off_market  no South African, Nigerian or Kenyan reach (enum without ZA/NG/KE, or a market 42 does not cover)
    out_of_scope  commerce, reviews, jobs, finance, SEO, ads or account plumbing that cultural detection does not use

core/collect/tests/test_endpoint_audit.py fails when a catalogue route has no verdict, when a live route is not priced
in core/collect/socialcrawl_client.py PRICED or allowed in Ask, and when a priced route is neither live nor explained.

    python -m core.collect.endpoint_audit            the table as Markdown, and the counts by verdict
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

CATALOGUE = Path(__file__).resolve().parents[2] / "docs" / "full-42" / "reference" / "sc_routes.json"

# Called today: route -> where. Morning means f42-collect at 02:00 SAST (core/collect/job.py, row numbers as in
# docs/full-42/SOURCES.md); confirm is the 05:15 step (core/brief/confirm.py); Ask is ALLOWED_ROUTES in
# core/agent/tools/socialcrawl.py; video is core/understand/video.py with enrich_tools.TRANSCRIPT_ROUTES.
LIVE = {
    "tiktok/trending": "morning row 1, local feed per market (5)",
    "tiktok/hashtags/popular": "morning row 2, ZA hashtag board (the board has no NG or KE)",
    "youtube/videos/trending": "morning row 3, five categories per market",
    "youtube/shorts/trending": "morning row 4, global",
    "instagram/music/trending": "morning row 4, global",
    "instagram/location/posts": "morning row 5, place-tagged posts at hub locations",
    "apple_music/charts": "morning row 6, songs and music videos per market",
    "reddit/subreddit": "morning row 7, hot and rising on market subreddits",
    "facebook/profile/posts": "morning row 8, hub pages; seed template",
    "tiktok/song/videos": "morning row 11, top sounds; seed template",
    "tiktok/song": "morning row 12, sound counters; pulse when on",
    "tiktok/hashtag": "morning row 12, hashtag counters; Ask; pulse when on",
    "tiktok/search/top": "morning row 14, country-filtered search; confirm; Ask",
    "tiktok/search/hashtag": "morning row 14 seed template; Ask",
    "instagram/search/reels": "morning row 14i with creator country; Ask",
    "search/multi": "morning row 16 (Instagram, YouTube, Reddit, X, Threads, Facebook) and 16w (LinkedIn); "
                    "confirm; Ask; local sources",
    "bluesky/search": "morning row 16w; Ask",
    "pinterest/search": "morning row 16w, off (count 0) until the answer schema takes pinterest",
    "reddit/subreddit/search": "morning row 16w, inside r/southafrica, r/Nigeria and r/Kenya. Candidate probe 16 Oct: "
                               "7 posts a search at 1 credit, every placed post in market",
    "instagram/audio/reels": "morning seed template (sounds on both platforms)",
    "tiktok/profile/videos": "morning seed template (creator breakout baseline)",
    "twitter/user/tweets": "morning row 23a, X hub accounts; seed template",
    "prism/post-stats": "morning row 22, evidence-post velocity",
    "prism/profiles": "morning row 23, culture desk and curated creator panels; local sources",
    "web/scrape": "morning row 23b (X trends archive, when x_trends is on); local sources",
    "google_trends/trending": "morning Google Trends phase, query triage only (RULES.md rule 2); Ask",
    "youtube/search/advanced": "morning location lane (radius search, when a reviewed place exists); Ask",
    "tiktok/location/posts": "morning location lane (when a reviewed place id exists)",
    "tiktok/profile": "morning country phase C1 (creator country)",
    "instagram/profile/about": "morning country phase C1 (account country)",
    "google_play/app-list": "local sources, Android chart",
    "twitter/tweet": "local sources X discovery (one known post)",
    "twitter/search/tweets": "Ask; X also searched every morning inside row 16 search/multi",
    "threads/search": "Ask; Threads also searched every morning inside row 16 search/multi",
    "reddit/search": "Ask",
    "facebook/search/posts": "Ask; Facebook also searched every morning inside row 16 search/multi",
    "linkedin/search/posts": "Ask; LinkedIn searched every morning through row 16w search/multi",
    "google_news/search": "Ask",
    "search/everywhere": "Ask, with the generated-answer lanes always excluded",
    "tiktok/post/comments": "Ask enrichment",
    "instagram/post/comments": "Ask enrichment",
    "youtube/video/comments": "Ask enrichment",
    "reddit/post/comments": "Ask enrichment",
    "facebook/post/comments": "Ask enrichment",
    "twitter/tweet/replies": "Ask enrichment",
    "threads/post/comments": "Ask enrichment",
    "tiktok/post/transcript": "video reading and Ask enrichment",
    "instagram/media/transcript": "video reading and Ask enrichment",
    "youtube/video/transcript": "video reading and Ask enrichment",
    "twitter/tweet/transcript": "video reading and Ask enrichment",
    "reddit/post/transcript": "video reading and Ask enrichment",
    "facebook/post/transcript": "video reading and Ask enrichment",
    "tiktok/video/screen-text": "video reading and Ask",
    "credits/balance": "every job: balance floor",
    "credits/transactions": "nightly reconciliation (f42-reconcile)",
    "status": "client: circuit state before a retry",
    "utility/capabilities": "route and price checks",
    "utility/endpoints": "route and price checks",
    "utility/endpoint": "route and price checks",
}

# Not called yet and worth adding, most useful first. Each needs a small probe of its response shape and its
# in-market share before it joins a run, and its credits come from the collect share's room or a cap Albert sets.
CANDIDATE = {
    "tiktok/search/suggestions": "TikTok autocomplete per region (1). Market-true phrases people are typing; a "
                                 "seed source for row 14 that needs no news trigger. Candidate probe 16 Oct: ten "
                                 "phrases a call, local for a real term (ZA 'orlando pirates vs sundowns'), noise "
                                 "for a term nobody types (NG 'take ham play'). Next: wire as a term source.",
    "facebook/events": "City events with RSVP counts (1): the cultural calendar from below. Probed on 28 Sept, "
                       "never scheduled.",
    "tiktok/effect/videos": "Videos made with one effect (1): a format trend's spread, the way song/videos tracks "
                            "a sound. Needs effect ids, which TikTok posts carry.",
    "tiktok/search/users": "TikTok creators by account country (1, 5 a row with country=, enum has ZA, NG, KE): "
                           "a weekly build of the creator panels the morning run reads.",
    "app_store/app-list": "Apple's app chart per storefront (5): the iPhone half of the app chart that "
                          "google_play/app-list already reads, for ZA where iPhone share is higher.",
    "search/news": "Multi-country news in one call (2 + 1 a leg). Probed, not scheduled; would let Ask read "
                   "NG and KE editions directly. Morning news already comes from GDELT and Google RSS.",
    "reddit/search/comments": "Reddit comment search (1). Priced and unused: conversation-level evidence "
                              "for Ask where post search finds only titles.",
    "prism/creator-card": "One handle across 4-7 platforms (5-8): the originator check in the confirm design "
                          "(research row 27), never built. verify=true is generated and must not be cited.",
}

LATER = {
    # Measured by the 16 Oct candidate probe (probe-candidates-20261016T181157Z, ZA and NG; KE not reached).
    "instagram/search/hashtag": "Probed 16 Oct: 3 of 4 placed posts in market (ZA), but 5 credits a call and a "
                                "missing hashtag is still charged 5 (NG 404). Reel search covers Instagram for less.",
    "youtube/search/hashtag": "Probed 16 Oct: 16 Shorts for ZA none placeable by country, NG empty. Not worth "
                              "a lane until a placed share can be shown.",
    "facebook/search/groups": "Probed 16 Oct: 4 to 7 groups a search, none placeable by country from the row. "
                              "Revisit with a group list chosen by hand.",
    "facebook/group/posts": "Probed 16 Oct: 4 posts a group, none placeable by country. Follows "
                            "facebook/search/groups.",
    "telegram/profile/posts": "Curated Telegram channels. Off until Albert says go (TELEGRAM_READY).",
    "monitors": "Hourly replays for the intraday pulse. The pulse is off until Albert says go (PULSE_READY).",
    "monitors/{id}": "As monitors.",
    "monitors/{id}/export": "As monitors.",
    "monitors/{id}/runs": "As monitors.",
    "monitors/{id}/timeseries": "As monitors.",
    "cohorts": "Creator panels of up to 10,000 handles; price not published (x-credit-cost null). Probe first.",
    "cohorts/{cohortId}": "As cohorts.",
    "cohorts/{cohortId}/members": "As cohorts.",
    "cohorts/{cohortId}/queries": "As cohorts.",
    "cohort-queries/{queryId}": "As cohorts.",
    "cohort-queries/{queryId}/results": "As cohorts.",
    "search/creators": "Fused creator discovery (10). Priced; useful for panel building after tiktok/search/users.",
    "search/forums": "Fused Reddit, Hacker News and Naver (10). Reddit is already read directly at 1 a call; "
                     "Hacker News is off-market and Naver is Korean, so it adds cost, not reach.",
    "tiktok/profile/region": "Creator country (1). Priced; the country phase reads tiktok/profile, which also "
                             "returns it.",
    "tiktok/search/music": "Sound search (1). Priced; sounds arrive from feeds and boards with their ids.",
    "youtube/transcripts": "Batch transcripts (3 each, up to 100). Same price as the per-video route video reading "
                           "uses; worth switching only when a run reads many YouTube clips at once.",
    "instagram/reels/trending": "Global trending reels with null views: no market and no counts.",
    "prism/mentions": "Posts mentioning a handle (1-5). Probed; a brand-skin tool, not detection.",
    "instagram/tagged": "Posts an account is tagged in (5). Probed; a brand-skin tool.",
    "prism/jobs": "Batches of 5,000 profiles or URLs. Morning batches fit prism/profiles and post-stats today.",
    "prism/jobs/{job_id}": "As prism/jobs.",
    "prism/lookup": "Resolves a pasted URL at its route's price. Ask could use it for pasted links.",
    "prism/video-intel": "One video's detail, stats, transcript and comments (5). Its tone blocks are generated.",
    "prism/format-lift": "Which hook works for a creator (4). A skin tool.",
    "prism/comments": "Every comment on a post, paginated (2). Ask enrichment uses the native comment routes.",
    "twitter/community/tweets": "X Communities (1). Research LATER: needs a curated list of local communities.",
    "facebook/search/videos": "Facebook video search (1). Facebook search already runs inside row 16.",
    "reddit/search/media": "Reddit image and video search (1). Reddit search already runs in Ask.",
    "reddit/omni-search": "One keyword across all of Reddit with subreddit context (5). Ask candidate once the "
                          "1-credit reddit/search proves too thin.",
    "reddit/subreddits/search": "Find subreddits by topic (1): grows the subreddit list, a manual review step.",
    "threads/user/posts": "A Threads account's posts (1): for a Threads hub panel, which does not exist yet.",
    "bluesky/user/posts": "A Bluesky account's posts (1): for a hub panel, after row 16w shows Bluesky's reach.",
    "pinterest/url-stats": "Pinterest saves for external URLs (1): a velocity read, after row 16w shows reach.",
    "facebook/profile/reels": "A page's reels (1). Hub pages are read through profile/posts.",
    "youtube/channel/shorts": "A channel's Shorts (1). Channels are read through prism/profiles.",
    "youtube/channel/videos": "A channel's videos (1). Channels are read through prism/profiles.",
    "youtube/channel/community-posts": "Channel community posts (1). No channel panel reads them yet.",
    "instagram/profile/reels": "An account's reels (1). Accounts are read through prism/profiles.",
    "instagram/profile/posts": "An account's posts (1). Accounts are read through prism/profiles.",
    "instagram/search/popular": "Popular posts for a query (1). Instagram search runs through reels and multi.",
    "instagram/search/location": "Finds place ids (5). Used by hand to pick row 5 hubs; ZA needs new busier ones.",
    "facebook/events/search": "Events by keyword (1), after facebook/events.",
    "facebook/event/details": "One event's details (1), after facebook/events.",
    "facebook/profile/events": "A page's events (1), after facebook/events.",
    "spotify/search": "Spotify search (1). Spotify has no chart route, so it gives no ranking.",
    "kwai/user/posts": "No discovery route; profile reads only. Kwai is small in these markets.",
    "twitch/user/videos": "No discovery route; profile reads only.",
    "rumble/search": "Rumble search (1). Its audience is US-centred; no measured local reach.",
    "hackernews/search": "Tech discussion (1), US-centred. Off-market for cultural detection; a Gamma tech-skin "
                         "candidate at most.",
    "quora/search": "Quora questions (5). Nigeria is one of Quora's larger audiences, so this is the off-market "
                    "route most worth a probe; unmeasured.",
    "tiktok/search": "Plain TikTok search (1). The morning run uses tiktok/search/top, which filters by country.",
    "youtube/search": "Plain YouTube search (1). The morning run and Ask use youtube/search/advanced, which takes "
                      "region and exact dates.",
    "youtube/search/suggestions": "YouTube autocomplete per region (1); after tiktok/search/suggestions proves "
                                  "the idea.",
    "instagram/search": "Accounts, hashtags and places by name (1): a lookup for picking hubs, done by hand.",
    "instagram/search/music": "Instagram sound search (5). Sounds arrive from music/trending and audio/reels.",
    "instagram/search/profiles": "Account search (1): panel building, a manual review step.",
    "facebook/search/pages": "Page search (1): finding hub pages, a manual review step.",
    "threads/search/users": "Account search (1): panel building, a manual review step.",
    "twitter/search/users": "Account search (1): panel building, a manual review step.",
    "linkedin/search/hashtag": "LinkedIn posts by hashtag (10). search/posts at 5 covers the same posts by query.",
    "content_analysis/search": "Web citations with sentiment (20). Skins only; sentiment is generated.",
    "google_play/app-search": "App search (5). Charts cover what is rising.",
}

NEVER = {
    "google_trends/explore": "RULES.md rule 2: explore and rising stay parked; trending is triage only.",
    "google_trends/rising": "RULES.md rule 2, as google_trends/explore.",
    "prism/trend-board": "SOURCES.md Never used: every call pulls the Google Trends lane; its parts cost less "
                         "bought directly, and its hashtag board is empty for NG and KE.",
    "prism/earliness": "SOURCES.md Never used: includes a Google Trends lane.",
    "prism/audience-language": "SOURCES.md Never used: opens the audience sample (demographics rule).",
    "tiktok/user/audience": "SOURCES.md Never used: audience demographics.",
    "twitter/ai-search": "SOURCES.md Never used: a Grok answer, generated.",
    "prism/investigate": "SOURCES.md Never used: an LLM walk.",
    "prism/answers": "SOURCES.md Never used: generated multi-engine answers.",
    "prism/ai-visibility": "SOURCES.md Never used: generated answers.",
    "prism/crisis-postmortem": "Generated timeline; research NEVER.",
    "perplexity/research": "Generated answers are never evidence.",
    "tavily/search": "Generated answers are never evidence.",
    "polymarket/research": "A betting market, never cultural evidence.",
    "web/agent": "An LLM web agent; research NEVER.",
    "prism/adverse-screen": "Screens people: personal data, never.",
    "prism/audience-overlap": "Commenter audiences of named creators: personal data at scale.",
    "youtube/channel/about": "25 credits and returns contact emails: personal data.",
    "tiktok/videos/popular": "Its board covers the US, Japan, Vietnam, Thailand and Indonesia only.",
    "tiktok/ads/top": "Its country enum has no ZA, NG or KE.",
    "pinterest/trends": "Its 23-country enum has no ZA, NG or KE (search is used instead, row 16w).",
    "google/search": "Web search results are never cultural evidence (research NEVER).",
}

# Family rules for everything not named above: (prefix, verdict, reason). The first match wins.
FAMILIES = (
    ("credits/", "live", "account plumbing"),
    ("utility/", "out_of_scope", "account plumbing; the three 42 needs are live"),
    ("web/monitors", "out_of_scope", "page-change monitors for skins, not detection"),
    ("web/sessions", "out_of_scope", "interactive browser sessions; web/scrape covers the pages 42 reads"),
    ("web/jobs", "out_of_scope", "async web jobs; web/scrape covers the pages 42 reads"),
    ("web/", "out_of_scope", "web crawling and extraction; web/scrape covers the pages 42 reads"),
    ("google_trends/", "never", "RULES.md rule 2"),
    ("google_news/", "live", "Ask"),
    ("linkedin/", "never", "people, jobs, companies, schools and ads: research verdict NEVER (only search/posts "
                           "is used)"),
    ("prism/", "out_of_scope", "brand, campaign, commerce or developer intelligence for skins, not detection "
                               "(research section 3)"),
    ("monitors", "later", "intraday pulse, off (PULSE_READY)"),
    ("cohort", "later", "creator panels; price not published"),
    ("douyin/", "off_market", "China"),
    ("xiaohongshu/", "off_market", "China"),
    ("naver/", "off_market", "Korea"),
    ("truthsocial/", "off_market", "US political network"),
    ("hackernews/", "off_market", "US tech discussion"),
    ("github/", "off_market", "code hosting"),
    ("producthunt/", "off_market", "US product launches"),
    ("rumble/", "off_market", "US-centred video; no measured local reach"),
    ("quora/", "off_market", "unmeasured local reach; quora/search is the one to probe"),
    ("kick/", "later", "no discovery route; clip reads only"),
    ("twitch/", "later", "no discovery route; profile reads only"),
    ("kwai/", "later", "no discovery route; profile reads only"),
    ("snapchat/", "later", "no discovery route; Spotlight has no search or trending read"),
    ("telegram/", "later", "Telegram is off until Albert says go (TELEGRAM_READY)"),
    ("spotify/", "lookup", "Spotify has no chart route; Apple Music charts are read instead"),
    ("apple_music/", "lookup", "single album, artist, track or search reads; charts are live"),
    ("linkbio/", "later", "link-in-bio pages: creator enrichment, research LATER"),
    ("linkme/", "later", "link-in-bio pages: creator enrichment, research LATER"),
    ("linktree/", "later", "link-in-bio pages: creator enrichment, research LATER"),
    ("komi/", "later", "link-in-bio pages: creator enrichment, research LATER"),
    ("pillar/", "later", "link-in-bio pages: creator enrichment, research LATER"),
    ("content_analysis/", "out_of_scope", "generated sentiment at 20 a call; skins only"),
    ("google_play/", "out_of_scope", "app details, reviews and listings; the chart is live"),
    ("app_store/", "out_of_scope", "app details, reviews and listings; app-list is the candidate"),
    ("google/", "out_of_scope", "ads, business profiles and hotels"),
    ("google_shopping/", "out_of_scope", "commerce"),
    ("tiktokshop/", "out_of_scope", "commerce"),
    ("amazon/", "out_of_scope", "commerce; best-sellers has no ZA"),
    ("aliexpress/", "out_of_scope", "commerce"),
    ("ebay/", "out_of_scope", "commerce"),
    ("etsy/", "out_of_scope", "commerce"),
    ("walmart/", "out_of_scope", "US commerce"),
    ("target/", "out_of_scope", "US commerce"),
    ("kohls/", "out_of_scope", "US commerce"),
    ("home_depot/", "out_of_scope", "US commerce"),
    ("wayfair/", "out_of_scope", "commerce"),
    ("hm/", "out_of_scope", "commerce"),
    ("sephora/", "out_of_scope", "commerce"),
    ("klarna/", "out_of_scope", "commerce"),
    ("gumtree/", "out_of_scope", "classifieds (large in ZA, but listings are commerce, not culture; a skin source at most)"),
    ("yelp/", "out_of_scope", "reviews"),
    ("tripadvisor/", "out_of_scope", "reviews"),
    ("trustpilot/", "out_of_scope", "reviews"),
    ("g2/", "out_of_scope", "software reviews"),
    ("jobs/", "out_of_scope", "job listings"),
    ("finance/", "out_of_scope", "markets data"),
    ("us_congress_trades/", "out_of_scope", "US finance"),
    ("on_page/", "out_of_scope", "SEO"),
    ("perplexity/", "never", "generated answers"),
    ("tavily/", "never", "generated answers"),
    ("polymarket/", "never", "betting market"),
)

# Platform reads of one known object, by path ending or segment. Matched after FAMILIES' explicit platform entries
# above do not apply (the social platforms have no family rule).
LOOKUP_HINTS = ("/post", "/tweet", "/video", "/profile", "/channel", "/playlist", "/album", "/artist", "/track",
                "/comment", "/story", "/stories", "/highlight", "/pin", "/board", "/group", "/community", "/clip",
                "/event", "/podcast", "/item", "/product", "/basic-profile", "/user/", "/collection", "/effects",
                "/song", "/media", "/engagement", "/similar", "/username-suggestions", "/subreddit/details",
                "/subreddit/search", "/followers", "/following", "/likers", "/retweeters", "/liked", "/live",
                "/audio", "/files", "/subtitles", "/thumbnails", "/sponsors", "/channels", "/videos")
PERSONAL = ("followers", "following", "likers", "retweeters", "liked", "people", "username-suggestions", "stories",
            "story/download", "user/live", "audience")
ADS = ("adlibrary", "/ad", "/ads", "company/ads")


def routes(path=CATALOGUE):
    """(path, method, credits) for every catalogue route, path without its leading slash."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))["routes"]
    return sorted({(r["path"].strip("/"), r["method"], r.get("credits")) for r in data})


def verdict(route):
    """(verdict, reason) for a catalogue route."""
    if route in LIVE:
        return "live", LIVE[route]
    if route in CANDIDATE:
        return "candidate", CANDIDATE[route]
    if route in NEVER:
        return "never", NEVER[route]
    if route in LATER:
        return "later", LATER[route]
    for prefix, name, why in FAMILIES:
        if route.startswith(prefix):
            return name, why
    if any(word in route for word in PERSONAL):
        return "never", "followers, likers, audiences or private-feeling account data: personal data rule"
    if any(word in route for word in ADS):
        return "out_of_scope", "ad libraries: brand skins, not detection"
    if "marketplace" in route:
        return "out_of_scope", "commerce (research NEVER)"
    if "full" in route.split("/")[-1] or route.endswith("/stats"):
        return "lookup", "a richer read of one known account or post at 5; prism/profiles and post-stats cover it"
    if "transcript" in route or "screen-text" in route:
        return "lookup", "one known post's speech or text; video reading covers the six main platforms"
    if any(hint in route for hint in LOOKUP_HINTS):
        return "lookup", "reads one known object; 42 opens objects through post-stats, profiles and the Ask routes"
    if "/search" in route:
        return "later", "a search route with no measured cultural reach in ZA, NG or KE"
    return "lookup", "reads one known object"


def table(path=CATALOGUE):
    rows = []
    for route, method, credits in routes(path):
        name, why = verdict(route)
        rows.append({"route": route, "method": method, "credits": credits, "verdict": name, "reason": why})
    return rows


ORDER = ("live", "candidate", "later", "lookup", "never", "off_market", "out_of_scope")


def markdown(rows):
    counts = Counter(r["verdict"] for r in rows)
    out = [f"{len(rows)} catalogue routes: " + ", ".join(f"{counts[v]} {v}" for v in ORDER if counts[v]) + ".", ""]
    for name in ORDER:
        part = [r for r in rows if r["verdict"] == name]
        if not part:
            continue
        out += [f"## {name} ({len(part)})", "", "| Route | Method | Credits | Why |", "|---|---|---|---|"]
        out += [f"| {r['route']} | {r['method']} | {'' if r['credits'] is None else r['credits']} | {r['reason']} |"
                for r in part]
        out.append("")
    return "\n".join(out)


def main(argv=None):
    rows = table()
    sys.stdout.write(markdown(rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
