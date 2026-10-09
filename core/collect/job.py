"""The 02:00 collect job (BUILD.md task 1.3): the Stage 1A rows of the costed table in SOURCES.md.

Row 4 is platform-wide and runs once first. Then the markets ZA, NG and KE run interleaved, row by row,
in four phases: the harvest (rows 1 to 8, the cheap unbiased lists first: the TikTok local feed, YouTube
trending, Reddit, Apple Music, then the ZA hashtag board and the Facebook hub pages; row 5 only when
Instagram location ids are configured); candidates scored from each market's harvest (frequency across
rows and platforms) feeding rows 11, 12, 14 and 16, where core/collect/seeds.py shares the slots of rows 14
and 16 between those candidates, live seed_queue seeds and rotated research terms (an anchor slice of at most
15% of expansion credits, about 15% of row 14 as exploration by Thompson sampling, no cluster above 25% of
expansion credits
or calls); the hub panels (row 23 culture-desk profiles and row 23a X accounts from core/config/hubs.yaml),
plus the rotating UEFA profile batch in row 23; and row 22 (prism/post-stats), one call per market for up to 20 evidence posts
first sighted there. The three local feed pulls open the first three phases, and pull_seq continues
each list's running pull number from item_counter_daily. Row 23b, the X trends archive, runs only with
X_TRENDS_SEED=1, which stays off until legal says yes.

Last, the local sources of task 2.12 (core/collect/local_sources.py, row 2.12) run on the same client with
the ledger lane set to local. They are paid from the collect share under their own 40 credit sub-limit, so
the collect cap binds both: the phase is not made when the cap less what the SocialCrawl phases charged is
under its hold, or when those phases stopped for credits or balance, and every fetch is then recorded
not_made with the reason. A local stop never stops the SocialCrawl phases, and a SocialCrawl market stop
does not stop the local phase. A local fetch whose market day has moved is not made (day_changed), and an
exception in the phase is recorded as local_error, failing its fetches on Coverage, without stopping the
writes. Its posts, observations, counters and Coverage records go through the same
writers; its news seeds, rule 1 phrases dropped by gdelt.blocked, are appended to seed_queue.

Google search terms (RULES.md rule 2, query triage only): before the first call, Google's free daily trends
feed is read for ZA, NG and KE (core/collect/google_rss.py, 0 credits, rows to google_search_signals as
google_rss), and the previous run date's eligible google_search_signals rows are read (google_rss and
google_trending only; BigQuery terms are excluded). Up to google_trends.GOOGLE_SEEDS_PER_MARKET terms a market join
that market's seed_queue seeds for row 14 (google_seed_phase): they take existing row 14 slots, never add a
call, and the posts they find pass the same locality checks as any row 14 post.

The watchlist lane (DATA.md 3.2 series watch, contract 10.7): the active watches in
intelligence_42_agent.v_watches_current become row 12 count reads in GLOBAL, lane watchlist, one per
hashtag or TikTok sound, at most row 12's 30 a run; they open the expansion phase and take their slots from
the candidates' row 12 reads, so no watches leaves the plan as it was. Creator, brand and query watches
have no route yet and are counted as not planned. --plan reads no BigQuery and so plans no watches.

Row 14i (REELS_TOP): Instagram reel searches with the creator card on each market's first row 14 queries, whose
reels carry the country each creator declares on their own profile. They come after row 22, last of the SocialCrawl
phases, and a call is made only while the collect share less what the run has charged still holds its hold beside
the Google Trends and local sources holds that follow; --plan shows the ones that fit on planned holds. They never
take a credit from another lane, none run under a cap override or in a repair run, and their health rows are
search_presence, so a failed one never decides a baseline day.

Every call goes through the SocialCrawl client, which appends raw_responses and the ledger; every
response is then parsed by core/collect/parse.py. cap_reached stops the rest of that market;
insufficient_credits and balance_floor stop everything. core/collect/writers.py then writes posts by
one MERGE, appends post_observations, item_counter_daily and collection_health, and merges every counter
item that has a readable name (body_names, chart_names, default_label) into cultural_map with L2's MERGE.

A live run refuses to start unless SAST, WAT and EAT are all on its run date (RUN_DATE or --run-date,
else today) at the clock and it is 01:30 to 22:30 SAST; any date works with --plan. A call whose market
day has moved past the run date (KE after 23:00 SAST) is not made, and one that comes back after it is
not parsed; both are recorded as day_changed. After the writes, one seed_queue row per seed used that
run is appended with its yield (seeds.yield_rows). When collect already ran ok but the next stage has no runs row
for the date, a repeat start only starts the next job. COLLECT_CAP_OVERRIDE may only lower the collect
share's daily cap (the 150 credit staging run); a higher value is refused before anything runs. Under
an override the industry board is skipped and the cap is split: a small GLOBAL share for row 4 and the
row 12 counter reads, and equal shares for ZA, NG and KE; a call that does not fit its share is not made.

The Telegram public channels (core/public_feeds/telegram.py, core/collect/telegram_collect.py) run after the public
feeds only when TELEGRAM_READY is True, which it is not: off, nothing is planned, fetched, written or counted.

COLLECT_ONLY_ROUTES (comma separated, with FORCE_RERUN=1, rank list and board routes only, else refused before
anything runs) makes a repair run on a day that already has an ok collect run, for a list the vendor failed
at 02:00: only those routes' calls, read live, less any whose health row the day's good collect run (the
base) already has valid. No local sources, Google Trends, Google terms, public feeds, seeds, watches or
seed_queue rows. As the newest ok run it becomes the day's good collect run, so writers.write_run appends the base's health
rows for every key it did not read under its own run_id (writers.carry_forward); every other collect table
is read across all ok runs and keeps the base's rows. Its runs counts are the base's, with its own under
"repair". A day with no ok collect run is refused.

    py -3.13 -m core.collect.job --plan --run-date 2026-09-29   every planned call and hold, no network
    python -m core.collect.job                                    the Cloud Run job f42-collect
"""

import argparse
import copy
import hashlib
import html
import json
import logging
import math
import os
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import yaml

from core.collect import chain, curated_creators, google_rss, google_trends, local_sources, research_terms, writers
from core.collect import location_sources
from core.collect import seeds as seeding
from core.collect.parse import (LANES, adoption_sample_points, ROUTES, board_hashtag, SEARCH_LANES, SEEDED, _dict, _first, _local_day, _protocol, _rows,
                                parse_with_creators)
from core.collect.socialcrawl_client import PRICED, TRANSIENT, Refused, Result, load_caps, quote_for

log = logging.getLogger(__name__)

CONFIG = Path(__file__).resolve().parents[1] / "config"
MARKETS = ("ZA", "NG", "KE")
GLOBAL = "GLOBAL"
OK = ("ok", "empty", "cached")          # HTTP 2xx with a parsable body
STOP_ALL = ("insufficient_credits", "balance_floor")

# Row parameters (SOURCES.md costed table; docs/full-42/research/13-socialcrawl-full-map.md section 2).
# YouTube's own public category ids: the overall chart, Music, Film and Animation, Gaming, Entertainment.
YOUTUBE_CATEGORIES = ("", "10", "1", "20", "24")
APPLE_TYPES = ("songs", "music-videos")
# Subreddits read a day per market, first in core/config/markets.yaml order. Each list puts the market's own
# subreddits first, so the pan-African ones further down (r/Africa, r/AfricanMusic, r/Afrobeats) are never read
# as a market's list. One credit a call; ZA reads more because most of its trends are held for thin evidence.
REDDIT_SUBS = {"ZA": 6, "NG": 3, "KE": 3}
REDDIT_SORTS = ("hot", "rising")
FB_PAGES = 4
INDUSTRY_DAYS = (0, 3)                  # the ZA industry=all board on Monday and Thursday
MULTI_PLATFORMS = "instagram,youtube,reddit,twitter,threads,facebook"
NEWS_PLATFORMS = "twitter,threads,reddit"
# The Telegram phase's route (core/collect/telegram_collect.py ROUTE): its records stay out of the job's own
# call counts.
TELEGRAM_ROUTE = "telegram_preview"
SONG_VIDEOS, COUNT_SOUNDS, COUNT_TAGS = 6, 5, 5
# Row 12 reads counts for 30 watched items a run (SOURCES.md). The Alerts watches take those slots first,
# as the watchlist lane's row 12 reads in GLOBAL, and the candidates share what is left per market.
COUNT_CALLS = len(MARKETS) * (COUNT_SOUNDS + COUNT_TAGS)
WATCH_ROW = "12w"
# Row 14 country searches a market. Their posts come back located by TikTok's own region field, which is what
# the Today locality check reads, so the collect share's headroom goes here: 60 a market (was 50, and 25 before
# that) under the 2,000 collect share. The planned count is part of their protocol (search_protocol), so a change
# of it starts a new search series instead of judging the bigger days against the old reference.
SEARCH_TOP, EXPLORE_SHARE, MULTI_TOP, MAX_SHARE = 60, 0.15, 7, 0.25
# Under a collect share of PAGES_SHARE or more, row 14 reads SEARCH_PAGES result pages a search (a credit a page),
# and every credit above PAGES_SHARE goes half to more row 14 searches and half to more row 16 search/multi
# calls (Instagram, YouTube, Reddit, X, Threads and Facebook), split evenly over the markets (volume()).
SEARCH_PAGES, PAGES_SHARE, MULTI_CREDITS = 2, 2000, 10
# Row 14i: Instagram reel searches with the creator card (include=creator), on a market's first row 14 queries. Each
# reel comes back with post.ext.author_country, the country its creator declares on their own Instagram profile, which
# parse reads as the creator's home market. A call holds 61 credits: 1 for the page and 60 for the creator lookups of
# a full page of 30, refunded for every creator not looked up or not found. The most a market reads a run; 0 turns its
# lane off. They take only the collect share's room left once every other call, the Google Trends phase and the local
# sources are paid for (reels_plan on planned holds, _Runner on what the run has charged), market by market in the
# run's order, so no other lane gives up a credit; none under a cap override.
REELS_TOP = {"ZA": 1, "NG": 1, "KE": 1}
REELS_ROW, REELS_ROUTE = "14i", "instagram/search/reels"
# Local feed pulls a day. The task 0.5 probe measured the feed=local in-country share at ZA 83% (20 of 24),
# NG 65% (17 of 26) and KE 12% (1 of 8), NG to KE overlap 1 video. BUILD.md 0.5 moves a market under 40%
# to country-filtered search: KE keeps one pull as its measurement series, and the credits of its other
# pulls buy extra tiktok/search/top country=KE calls in row 14 (search_calls).
FEED_PULLS = {"ZA": 3, "NG": 3, "KE": 1}
PROFILES_PER_CALL = 25                  # prism/profiles with posts takes at most 25 profiles a call
# A live run starts only inside this SAST window, which keeps it clear of 23:00 SAST (midnight EAT) and
# 01:00 SAST (midnight WAT), where a market's day would change under it.
START_WINDOW = (time(1, 30), time(22, 30))
# Country capture stops this long before the task timeout, so the public feeds, the Google reads and every write
# that follows it still run inside the task.
COUNTRY_TAIL_RESERVE = timedelta(minutes=40)
EVIDENCE_PER_MARKET, EVIDENCE_RATE = 20, 2  # post-stats: hosts at 1 or 2 credits a URL only
TRENDS_ARCHIVE = "https://trends24.in/{}/"
# Platform-wide tags that sit on most posts and say nothing about the market.
GENERIC_TAGS = frozenset({"fyp", "foryou", "foryoupage", "viral", "trending", "shorts", "reels", "explore"})
POST_FAMILIES = ("rank", "panel", "search", "location", "restat")
GLOBAL_SHARE = 0.10                     # of an overridden cap, for row 4 and the row 12 counter reads


@dataclass
class Call:
    row: str
    route: str
    params: dict
    market: str
    lane: str | None
    seed_key: str | None = None
    pull_seq: int | None = None
    protocol: str | None = None
    use_cache: bool = True
    seed: seeding.Pick | None = None
    source_market: str | None = None

    @property
    def method(self):
        return PRICED[self.route].method

    @property
    def family(self):
        return ROUTES[self.route][0]

    def hold(self):
        return quote_for(self.route, self.method, self.params)

    def series(self):
        if self.seed is not None:
            return "placebo" if self.lane == "placebo" else "search"
        return "x_trends" if self.route == "web/scrape" else ROUTES[self.route][2]

    def series_protocol(self):
        return self.protocol or _protocol(self.route, self.params, ROUTES[self.route][4])

    def health_market(self):
        """Counter series are platform-wide, so their health rows sit in GLOBAL like their counters."""
        return GLOBAL if self.family in ("count", "curve") and self.seed is None else self.market

    def lane_class(self):
        if self.seed is not None:
            return "search_presence"
        return "unbiased_rank" if self.family in ("rank", "board", "none") else LANES[self.family][1]


def load_config():
    def read(name):
        return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))

    return {"markets": read("markets.yaml")["markets"], "hubs": read("hubs.yaml")}


PRISM_PROTOCOL_VERSION = "v2"  # prism/profiles panels landed nothing before the parser fix; v2 starts a fresh health reference


def panel_protocol(members, version=None):
    """panel: plus a short hash of the membership, so a changed list starts a new series (DATA.md 3.2); a version
    starts one too, which is how a panel that parsed to zero items leaves its zero item health reference."""
    text = json.dumps(sorted(members, key=json.dumps), separators=(",", ":"))
    return "panel:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] + (f":{version}" if version else "")


def search_protocol(route, params, planned):
    """The route's fixed parameters plus the planned row 14 call count, so a changed plan starts a new series
    with its own items reference, as panel_protocol does for a changed membership (DATA.md 3.2: list length
    is part of a protocol). The day's queries change every run and are left out."""
    return _protocol(route, {**params, "planned": planned}, ROUTES[route][4] + ("planned",))


def _since(day, days=1):
    return (day - timedelta(days=days)).isoformat()


def feed_pull(market, n):
    return Call("1", "tiktok/trending", {"region": market, "feed": "local"}, market, "sweep", pull_seq=n,
                use_cache=n == 1)


def global_calls():
    return [Call("4", "youtube/shorts/trending", {}, GLOBAL, "sweep", pull_seq=1),
            Call("4", "instagram/music/trending", {}, GLOBAL, "sweep", pull_seq=1)]


def harvest_calls(market, day, config, *, x_trends=False, override=False):
    """Rows 1 to 8 for one market, cheap unbiased lists first (row 1 as its first pull), plus 23b when on.
    The industry=all board is skipped under a cap override. Returns (calls, skipped)."""
    cfg = config["markets"][market.lower()]
    calls, skipped = [feed_pull(market, 1)], []
    for category in YOUTUBE_CATEGORIES:
        params = {"region": market, **({"category": category} if category else {})}
        calls.append(Call("3", "youtube/videos/trending", params, market, "sweep", pull_seq=1))
    own = {str(s).lower() for s in cfg["subreddits"].get("own") or ()}
    for sub in cfg["subreddits"]["values"][:REDDIT_SUBS[market]]:
        # A post read from the market's own subreddit was seen in its feeds; genre and pan-African ones were not.
        source = market if str(sub).lower() in own else None
        for sort in REDDIT_SORTS:
            calls.append(Call("7", "reddit/subreddit", {"subreddit": sub, "sort": sort}, market, "sweep",
                              pull_seq=1, source_market=source))
    for kind in APPLE_TYPES:
        calls.append(Call("6", "apple_music/charts", {"country": market.lower(), "type": kind}, market, "sweep",
                          pull_seq=1))
    if market == "ZA":
        board = {"countryCode": "ZA", "period": "7"}
        calls.append(Call("2", "tiktok/hashtags/popular", board, market, "sweep", pull_seq=1))
        if day.weekday() in INDUSTRY_DAYS and not override:
            calls.append(Call("2", "tiktok/hashtags/popular", {**board, "industry": "all"}, market, "sweep",
                              pull_seq=1))
    pages = [str(p) for p in cfg["facebook_pages"]["values"][:FB_PAGES]]
    protocol = panel_protocol(pages)
    for page in pages:
        calls.append(Call("8", "facebook/profile/posts", {"pageId": page, "since": _since(day)}, market, "panel",
                          seed_key=page, protocol=protocol, source_market=market))
    locations = (cfg.get("instagram_locations") or {}).get("values") or []
    # Every hub every day: location-tagged posts are located Instagram posts in the market, at 5 credits a hub.
    # Under a cap override the hubs take turns, one a day, so the expansion rows keep their share.
    hubs = [locations[day.toordinal() % len(locations)]] if override and locations else locations
    for hub in map(str, hubs):
        calls.append(Call("5", "instagram/location/posts", {"location_id": hub}, market, "sweep", seed_key=hub,
                          source_market=market))
    if not locations:
        skipped.append({"row": "5", "route": "instagram/location/posts", "market": market,
                        "series": "ig_location", "protocol": "instagram/location/posts",
                        "lane_class": "search_presence", "platform": "instagram",
                        "detail": "no Instagram location ids in core/config/markets.yaml"})
    if x_trends:
        slug = "-".join(cfg["name"].lower().split())
        calls.append(Call("23b", "web/scrape", {"url": TRENDS_ARCHIVE.format(slug)}, market, None))
    return calls, skipped


def panel_calls(market, day, config, curated_records=(), curated_limit=0):
    """Rows 23 and 23a: culture desk and X hubs, plus the rotating UEFA profiles in row 23."""
    hubs = config["hubs"]["markets"][market.lower()]
    calls = []
    desk = [{"platform": e["platform"], "handle": e["handle"]} for e in hubs.get("culture_desk") or []]
    if desk:
        # Own feeds of the market, like the curated panel below (Albert's yes to D2, 4 Oct 2026).
        calls.append(Call("23", "prism/profiles", {"items": desk, "include": "posts", "since": _since(day)}, market,
                          "panel", protocol=panel_protocol(desk, PRISM_PROTOCOL_VERSION), source_market=market))
    selected = curated_creators.daily_rotation(curated_records, market, day, curated_limit)
    if selected:
        items = [{"platform": row["platform"], "handle": row["handle"]} for row in selected]
        # One panel over the day's whole rotation, read in batches of the vendor's 25 profiles a call.
        protocol = panel_protocol(items, PRISM_PROTOCOL_VERSION)
        for start in range(0, len(items), PROFILES_PER_CALL):
            calls.append(Call("23", "prism/profiles",
                              {"items": items[start:start + PROFILES_PER_CALL], "include": "posts",
                               "since": _since(day)},
                              market, "panel", protocol=protocol, source_market=market))
    handles = [e["handle"] for e in hubs.get("x") or []]
    protocol = panel_protocol(handles)
    for handle in handles:
        calls.append(Call("23a", "twitter/user/tweets", {"handle": handle, "since": _since(day)}, market, "panel",
                          seed_key=handle, protocol=protocol))
    return calls


def cap_share(calls, share=MAX_SHARE, key=lambda c: c.seed_key, weight=lambda c: 1):
    """Trim calls, lowest-ranked first, until no key holds more than share of the total weight (of the
    calls by default)."""
    calls = list(calls)
    while calls:
        totals = Counter()
        for c in calls:
            totals[key(c)] += weight(c)
        top = max(totals.values())
        if top <= share * sum(totals.values()):
            break
        del calls[max(i for i, c in enumerate(calls) if totals[key(c)] == top)]
    return calls


def cap_expansion(calls):
    """No candidate above MAX_SHARE of the calls of rows 14 and 16, and no cluster above it of their credits."""
    while True:
        capped = cap_share(cap_share(calls), key=lambda c: c.seed.cluster, weight=Call.hold)
        if len(capped) == len(calls):
            return capped
        calls = capped


class Volume(NamedTuple):
    search_top: int
    pages: int
    multi_top: int


def volume(cap=None):
    """Row 14 searches and pages and row 16 calls a market under the collect share (caps.yaml by default)."""
    cap = load_caps()["ENGINE_DAILY"]["collect"] if cap is None else cap
    if cap < PAGES_SHARE:
        return Volume(SEARCH_TOP, 1, MULTI_TOP)
    spare = (cap - PAGES_SHARE) // 2 // len(MARKETS)
    return Volume(SEARCH_TOP + spare // SEARCH_PAGES, SEARCH_PAGES, MULTI_TOP + spare // MULTI_CREDITS)


def _search_params(query, market, day, pages):
    params = {"query": query, "country": market, "publish_time": "this-week",
              "seen": f"f42-{market.lower()}-{day.isoformat()}"}
    return {**params, "max_pages": pages} if pages > 1 else params


def seed_params(template, query, kind, market, seed_date, day, pages=1):
    since = _since(seed_date) if seed_date else _since(day, 7)
    if template == "search/multi":
        platforms = NEWS_PLATFORMS if kind in ("brand", "event") else MULTI_PLATFORMS
        return {"query": query, "platforms": platforms, "since": since}
    if template == "tiktok/search/top":
        return _search_params(query, market, day, pages)
    if template == "tiktok/search/hashtag":
        return {"hashtag": query, "region": market, "max_age_days": 7}
    if template == "tiktok/song/videos":
        return {"clipId": query, "use": 1}
    if template == "instagram/audio/reels":
        return {"audio_id": query}
    if template == "tiktok/profile/videos":
        return {"handle": query}
    if template == "twitter/user/tweets":
        return {"handle": query, "since": since}
    if template == "facebook/profile/posts":
        page = {"pageId": query} if query.isdigit() else {"url": f"https://www.facebook.com/{query}"}
        return {**page, "since": since}
    raise ValueError(f"no seed call for template {template!r}")


def seed_call(pick, market, day, pages=1):
    params = seed_params(pick.template, pick.query, pick.kind, market, pick.seed_date, day, pages)
    return Call(pick.row, pick.template, params, market, pick.lane, seed_key=pick.seed_key, seed=pick)


def expansion_calls(market, day, ranked, rng, counted, search_top=SEARCH_TOP, *, queue=(), uses=None,
                    counts=(COUNT_SOUNDS, COUNT_TAGS), cut=None, pages=1, multi_top=MULTI_TOP):
    """Rows 11 and 12 from ranked candidates, counts being row 12's (sounds, hashtags) for the market; rows
    14 and 16 from seeds.mix over the ranked hashtags and the market's live seeds (queue), uses being each
    cluster's recorded yields. counted holds (kind, key) already read this run."""
    sounds, tags = ranked.get("sound", []), ranked.get("hashtag", [])
    calls = [Call("11", "tiktok/song/videos", {"clipId": s, "use": 1}, market, "watchlist", seed_key=s)
             for s in sounds[:SONG_VIDEOS]]
    for kind, route, name, keys in (("sound", "tiktok/song", "clipId", sounds[:counts[0]]),
                                    ("hashtag", "tiktok/hashtag", "hashtag", tags[:counts[1]])):
        for key in keys:
            if (kind, key) not in counted:
                counted.add((kind, key))
                calls.append(Call("12", route, {name: key}, market, "watchlist", seed_key=key))
    seen = f"f42-{market.lower()}-{day.isoformat()}"

    def top(p):
        params = _search_params(p.query, market, day, pages)
        return Call("14", "tiktok/search/top", params, market, p.lane, seed_key=p.seed_key, seed=p)

    def multi(p):
        # A queued seed searches from the day before it was seeded (GDELT's news day); the harvest, a week.
        since = _since(p.seed_date) if p.seed_date else _since(day, 7)
        return Call("16", "search/multi", {"query": p.query, "platforms": MULTI_PLATFORMS, "since": since}, market,
                    p.lane, seed_key=p.seed_key, seed=p)

    def place(p, row):
        if p.template is not None:
            return seed_call(p, market, day, pages)
        return top(p) if row == "14" else multi(p)

    def hold(seed):
        template = seed["template"]
        return quote_for(template, PRICED[template].method,
                         seed_params(template, seed["query"], seed.get("kind"), market, seed.get("seed_date"), day,
                                     pages))

    probe = seeding.Pick("x", "expansion", "", "")
    picks = seeding.mix(ranked, queue, uses or {}, rng, slots14=search_top, slots16=multi_top,
                        cost14=top(probe).hold(), cost16=multi(probe).hold(), explore_share=EXPLORE_SHARE,
                        hold=hold)
    placebo = [seed_call(p, market, day, pages) for p in picks["placebo"]]
    regular = cap_expansion([place(p, "14") for p in picks["14"]] + [place(p, "16") for p in picks["16"]])
    for c in regular:
        # Row 14's country searches share one series a market; its day's items grow with the planned calls.
        if c.row == "14" and c.route == "tiktok/search/top" and c.series() == "search":
            c.protocol = search_protocol(c.route, c.params, search_top)
    if cut is not None:
        cut.extend((seed_call(p, market, day, pages), why) for p, why in picks["cut"])
    return calls + placebo + regular


def reels_calls(market, regular, count):
    """Row 14i for a market: count Instagram reel searches with include=creator, on the first queries of its planned
    row 14 country searches (placebo searches left out). They carry no seed, so a seed's recorded yield stays its
    row 14 and 16 reads, and their health rows are search_presence, never baseline. The protocol names the market's
    REELS_TOP count, as row 14's names its planned searches."""
    queries = []
    for c in regular:
        if len(queries) >= count:
            break
        if c.row == "14" and c.route == "tiktok/search/top" and c.seed is not None and c.lane != "placebo" \
                and c.seed.query not in [q for q, _, _ in queries]:
            queries.append((c.seed.query, c.seed_key, c.lane))
    calls = []
    for query, key, lane in queries:
        params = {"query": query, "include": "creator"}
        calls.append(Call(REELS_ROW, REELS_ROUTE, params, market, lane, seed_key=key,
                          protocol=search_protocol(REELS_ROUTE, params, REELS_TOP.get(market, 0))))
    return calls


def reels_counts(room, top=None):
    """Row 14i calls a market within room credits, market by market in the run's order (MARKETS), each up to its
    count in top (REELS_TOP) while a call's hold still fits, as the run makes them."""
    top = REELS_TOP if top is None else top
    hold = quote_for(REELS_ROUTE, "GET", {"query": "x", "include": "creator"})
    counts = {}
    for market in MARKETS:
        counts[market] = max(0, min(top.get(market, 0), int(room // hold)))
        room -= counts[market] * hold
    return counts


def cut_record(call, why):
    return {"row": call.row, "route": call.route, "market": call.market, "series": call.series(),
            "protocol": call.series_protocol(), "lane_class": call.lane_class(), "platform": ROUTES[call.route][1],
            "detail": f"placebo {call.seed_key} cut: {why}"}


def count_slots(watched):
    """Row 12's (sounds, hashtags) per market once the watches have taken their reads."""
    left = (COUNT_CALLS - watched) // len(MARKETS)
    return (left + 1) // 2, left // 2


def _watch_key(w):
    """(kind, key) a watch reads, or (None, why not). Hashtags pass _tag (rule 1 and the generic tags); a
    sound is read only as a TikTok clip id ("tiktok:<id>", the cultural_map key); an item target reads the
    kind and key cultural_map holds for it."""
    target = w.get("target") or {}
    kind, value = target.get("kind"), target.get("value")
    if kind == "item":
        kind, value = w.get("item_kind"), w.get("item_key")
        if kind is None:
            return None, "item not in cultural_map"
    if kind == "hashtag":
        tag = _tag(value)
        return (("hashtag", tag), None) if tag else (None, "rule 1, platform-generic or not a hashtag")
    if kind == "sound":
        platform, _, clip = str(value or "").partition(":")
        if platform.strip().casefold() == "tiktok" and clip.strip():
            return ("sound", clip.strip()), None
        return None, "only TikTok sounds have a count route"
    return None, f"no watch route for {kind} targets"


def watch_calls(watches):
    """The watchlist lane (DATA.md 3.2 series watch; contract 10.5 and 10.7): one row 12 count read in GLOBAL
    per hashtag or TikTok sound the active watches name, however many watches name it, oldest watch first
    and at most COUNT_CALLS. A count is platform-wide, so the watch's market only selects it. Rule 1 holds on
    the key and on the watch's and item's labels (gdelt.blocked). Returns (calls, [(watch_id, why not)])."""
    from core.collect.gdelt import blocked

    calls, keys, skipped = [], set(), []
    for w in watches:
        if w.get("status") != "active":
            skipped.append((w["watch_id"], "not active"))
            continue
        key, why = _watch_key(w)
        if why is None and any(blocked(t) for t in (w.get("label"), w.get("item_label")) if t):
            why = "rule 1"
        if why is None and key not in keys and len(calls) >= COUNT_CALLS:
            why = f"over the {COUNT_CALLS} row 12 reads a run"
        if why:
            skipped.append((w["watch_id"], why))
            continue
        if key not in keys:
            keys.add(key)
            route, name = ("tiktok/song", "clipId") if key[0] == "sound" else ("tiktok/hashtag", "hashtag")
            calls.append(Call(WATCH_ROW, route, {name: key[1]}, GLOBAL, "watchlist", seed_key=key[1]))
    return calls, skipped


def _tag(raw):
    """A harvest hashtag as a candidate key, or None. Every harvest tag enters the plan here, so a rule 1
    tag (gdelt.blocked, which also refuses a tag mixing scripts) is never a candidate for any row. The key
    is the tag as written, casefolded, not its skeleton, so item ids do not change."""
    from core.collect.gdelt import blocked

    tag = str(raw or "").strip().lstrip("#").casefold()
    if not tag or len(tag) > 100 or any(ch.isspace() for ch in tag) or not any(ch.isalnum() for ch in tag):
        return None
    return None if tag in GENERIC_TAGS or blocked(tag) else tag


def sightings(done):
    """(kind, key, row, platform) for every hashtag and TikTok sound in a market's harvest."""
    out = []
    for call, result, parsed in done:
        if call.route == "tiktok/hashtags/popular" and result.status in OK:
            for entry in result.items:
                raw = board_hashtag(entry)
                if _tag(raw):
                    out.append(("hashtag", _tag(raw), call.row, "tiktok"))
        for post in (parsed or {}).get("posts", []):
            for raw in post["hashtags"]:
                if _tag(raw):
                    out.append(("hashtag", _tag(raw), call.row, post["platform"]))
            if post["platform"] == "tiktok" and post["sound_id"]:
                out.append(("sound", post["sound_id"], call.row, "tiktok"))
    return out


def score_candidates(seen):
    """Ranked keys per kind: most platforms, then most rows, then most sightings, then the key. origin maps
    each hashtag to the (platform, row) that saw it most, for the seed mix's platform and family caps."""
    stats = {}
    for kind, key, row, platform in seen:
        entry = stats.setdefault((kind, key), [set(), set(), 0, Counter()])
        entry[0].add(platform)
        entry[1].add(row)
        entry[2] += 1
        entry[3][(platform, row)] += 1
    ranked = {"hashtag": [], "sound": [], "origin": {}}
    for (kind, key), (platforms, rows, n, where) in sorted(
            stats.items(), key=lambda kv: (-len(kv[1][0]), -len(kv[1][1]), -kv[1][2], kv[0][1])):
        ranked[kind].append(key)
        if kind == "hashtag":
            ranked["origin"][key] = min(where, key=lambda pr: (-where[pr], pr))
    return ranked


def _rate(url):
    try:
        return quote_for("prism/post-stats", "POST", {"urls": [url]})
    except Refused:
        return None


def pick_evidence(first_seen, focus):
    """Up to EVIDENCE_PER_MARKET URLs per market by each post's first-sighting market in this run: posts
    naming a candidate first, then by views. first_seen maps post_id to (market, post row)."""
    picks = {m: [] for m in MARKETS}
    for pid, (market, post) in first_seen.items():
        url = post.get("url")
        rate = _rate(url) if url else None
        if rate is None or rate > EVIDENCE_RATE:
            continue
        keys = {str(h).casefold() for h in post["hashtags"]} | {post.get("sound_id")}
        picks[market].append((not keys & focus.get(market, set()), -(post.get("views") or 0), pid, url))
    return {m: [url for *_, url in sorted(rows)[:EVIDENCE_PER_MARKET]] for m, rows in picks.items() if rows}


def search_calls(market, size=None):
    """Row 14 calls for a market: the volume's searches plus what its dropped local feed pulls would have cost
    at the volume's pages a search."""
    size = size or volume()
    feed = quote_for("tiktok/trending", "GET", {"region": market, "feed": "local"})
    search = quote_for("tiktok/search/top", "GET", _search_params("x", market, date(2026, 1, 1), size.pages))
    return size.search_top + (max(FEED_PULLS.values()) - FEED_PULLS[market]) * feed // search


def placeholders(market):
    return {"hashtag": [f"<{market} hashtag {i}>" for i in range(1, search_calls(market) + 1)],
            "sound": [f"<{market} sound {i}>" for i in range(1, SONG_VIDEOS + 1)]}


def shares(cap):
    """An overridden collect cap split into a small GLOBAL share and equal market shares."""
    small = math.floor(GLOBAL_SHARE * cap)
    each = math.floor((cap - small) / len(MARKETS))
    return {GLOBAL: small, **{m: each for m in MARKETS}}


def budget_key(call):
    """The share a call is paid from: GLOBAL for row 4 and the row 12 counter reads, else its market."""
    return GLOBAL if call.market == GLOBAL or (call.family in ("count", "curve") and call.seed is None) else call.market


class Budget:
    """Per-share credit limits under a cap override; no limits without one."""

    def __init__(self, share_cap=None):
        self.limits = shares(share_cap) if share_cap is not None else None
        self.spent = {}

    def fits(self, call, hold):
        if self.limits is None:
            return True
        key = budget_key(call)
        return self.spent.get(key, 0) + hold <= self.limits[key]

    def spend(self, call, credits):
        key = budget_key(call)
        self.spent[key] = self.spent.get(key, 0) + (credits or 0)


def interleaved(by_market):
    """(market, calls) blocks: each row in turn, in first-seen order, for GLOBAL, ZA, NG and KE."""
    rows = []
    for market in (GLOBAL,) + MARKETS:
        for call in by_market.get(market, []):
            if call.row not in rows:
                rows.append(call.row)
    for row in rows:
        for market in (GLOBAL,) + MARKETS:
            block = [c for c in by_market.get(market, []) if c.row == row]
            if block:
                yield market, block


def drive(execute, day, config, *, x_trends, override, rank, evidence, queue=lambda market: ((), {}),
          watched=(), curated_records=(), curated_limit=0, reels=None):
    """The run's four phases. execute(by_market) runs one phase interleaved; rank(market) gives a market's
    ranked candidates after the harvest; queue(market) its live seeds and recorded uses; watched the watch
    calls (watch_calls), which open the expansion phase and take row 12 slots from the candidates; evidence()
    gives {market: urls} at the end. reels maps a market to the most row 14i calls it makes (reels_counts in a
    plan, REELS_TOP in a run), last, after row 22, on that market's row 14 queries, so a refused, failed or unmade
    one leaves every other call as it was.
    Returns the skips."""
    execute({GLOBAL: global_calls()})
    harvest, skipped = {}, []
    for market in MARKETS:
        harvest[market], skips = harvest_calls(market, day, config, x_trends=x_trends, override=override)
        skipped += skips
    execute(harvest, skipped)
    counted = {("sound" if c.route == "tiktok/song" else "hashtag", c.seed_key) for c in watched}
    expansion, cut, size = {GLOBAL: list(watched)}, [], volume()
    for m in MARKETS:
        live, uses = queue(m)
        expansion[m] = [feed_pull(m, n) for n in (2,) if n <= FEED_PULLS[m]] + expansion_calls(
            m, day, rank(m), random.Random(f"{day.isoformat()}-{m}"), counted, search_calls(m, size), queue=live,
            uses=uses, counts=count_slots(len(watched)), cut=cut, pages=size.pages, multi_top=size.multi_top)
    execute(expansion, [cut_record(call, why) for call, why in cut])
    later = {m: reels_calls(m, expansion[m], (reels or {}).get(m, 0)) for m in MARKETS}
    execute({m: panel_calls(m, day, config, curated_records, curated_limit)
             + [feed_pull(m, n) for n in (3,) if n <= FEED_PULLS[m]]
             for m in MARKETS})
    urls = evidence()
    execute({m: [Call("22", "prism/post-stats", {"urls": urls[m]}, m, "watchlist")] for m in MARKETS if urls.get(m)})
    execute({m: calls for m, calls in later.items() if calls})
    return skipped


def _curated_limit(day, records, *, config, x_trends, share_cap, watches):
    baseline = plan(day, config=config, x_trends=x_trends, share_cap=share_cap, watches=watches,
                    include_curated=False, reels={})
    cap = share_cap if share_cap is not None else load_caps()["ENGINE_DAILY"]["collect"]
    local_hold = local_sources.total_hold(local_sources.plan(day))
    residual = cap - baseline["total"] - local_hold
    return curated_creators.limit_from_residual(records, day, residual)


def reels_plan(day, curated_records, curated_limit, *, config, x_trends, share_cap, watches):
    """Row 14i calls a market (reels_counts) in the collect share's room left by the planned hold of every other
    call, the curated panel at curated_limit included, the Google Trends phase's hold and the local sources' hold.
    None under a cap override."""
    if share_cap is not None or not any(REELS_TOP.values()):
        return {}
    others = plan(day, config=config, x_trends=x_trends, watches=watches, reels={},
                  curated=(curated_records, curated_limit))
    room = load_caps()["ENGINE_DAILY"]["collect"] - others["total"] - google_trends.MAX_SC_CREDITS \
        - local_sources.total_hold(local_sources.plan(day))
    return reels_counts(room)


def plan(day, *, config=None, x_trends=False, share_cap=None, watches=(), include_curated=True, only_routes=None,
         reels=None, curated=None):
    """Every call of a run per market in run order, with placeholders standing for the candidates, the
    seed_queue seeds and the evidence posts a live run finds, and the watch calls for watches (none from the
    command line, which reads no BigQuery). Under a cap override, calls that do not fit their share at their
    hold go to "over". With only_routes (a repair run) only those routes' calls are planned; a live repair
    also leaves out the ones the base run got valid. Returns {"calls": {market: [Call]}, "over": {...},
    "skipped": [...], "total": hold}. reels maps a market to its row 14i calls (reels_plan when None);
    curated is (records, limit) for the curated panel when already known."""
    config = config or load_config()
    if curated is not None:
        curated_records, curated_limit = curated
    else:
        curated_records = curated_creators.load_manifest() if include_curated else ()
        curated_limit = _curated_limit(day, curated_records, config=config, x_trends=x_trends, share_cap=share_cap,
                                       watches=watches) if include_curated else 0
    if reels is None:
        reels = reels_plan(day, curated_records, curated_limit, config=config, x_trends=x_trends,
                           share_cap=share_cap, watches=watches) if only_routes is None else {}
    budget = Budget(share_cap)
    calls = {m: [] for m in (GLOBAL,) + MARKETS}
    over = {m: [] for m in (GLOBAL,) + MARKETS}
    research = seeding.research_rows(research_terms.load_terms(), day)

    def execute(by_market, skipped=()):
        for market, block in interleaved(by_market):
            for call in block:
                if only_routes is not None and call.route not in only_routes:
                    continue
                if budget.fits(call, call.hold()):
                    budget.spend(call, call.hold())
                    calls[market].append(call)
                else:
                    over[market].append(call)

    def evidence():
        return {m: [f"https://www.instagram.com/p/{m}-evidence-{i}/" for i in range(1, EVIDENCE_PER_MARKET + 1)]
                for m in MARKETS}

    skipped = drive(execute, day, config, x_trends=x_trends, override=share_cap is not None, rank=placeholders,
                    evidence=evidence, queue=lambda m: (seeding.placeholders(m, day) +
                                                        [s for s in research if s["market"] == m] +
                                                        google_trends.placeholder_seeds(m, day), {}),
                    watched=watch_calls(watches)[0], curated_records=curated_records,
                    curated_limit=curated_limit, reels=reels)
    if only_routes is None:
        location_calls, _ = location_sources.plan(day, config)
        execute({m: [Call(**c) for c in location_calls if c["market"] == m] for m in MARKETS})
    if only_routes is not None:
        skipped = [x for x in skipped if x["route"] in only_routes]
    total = sum(c.hold() for cs in calls.values() for c in cs)
    return {"calls": calls, "over": over, "skipped": skipped, "total": total}


# Running

def safe_geo(fn):
    """geo_for_post with text or None for every free-text signal; its TypeError reads as unknown."""
    def only(value):
        return value if isinstance(value, str) else None

    def geo(platform, market, *, ext_region=None, home_market=None, profile_location=None, text=None,
            language=None):
        if not (isinstance(language, str) or (isinstance(language, list) and all(isinstance(x, str) for x in language))):
            language = None
        try:
            return fn(platform, market, ext_region=only(ext_region), home_market=only(home_market),
                      profile_location=only(profile_location), text=only(text), language=language)
        except TypeError:
            return None, None, None

    return geo


class _CountedIds:
    """item_id_fn that counts the ValueErrors it raises (parse skips those items) and keeps the kind, raw
    value and platform each id was first made from, for the cultural_map rows of the counter items."""

    def __init__(self, fn):
        self.fn, self.skipped, self.made = fn, 0, {}

    def __call__(self, kind, raw, platform):
        try:
            made = self.fn(kind, raw, platform)
        except ValueError:
            self.skipped += 1
            raise
        self.made.setdefault(made, (kind, raw, platform))
        return made


def _named(title, artist=None):
    title, artist = (str(v).strip() if v not in (None, "") else "" for v in (title, artist))
    return (f"{title} by {artist}" if title and artist else title) or None


AUTHOR_ONLY = "original sound - "  # the label prefix _sound_named writes for a TikTok sound known only by its author


def add_names(names, new):
    """Add new labels to names in place. A later label replaces an earlier one, except that an author-only label
    ("original sound - <author>") never replaces a name already held for the same item, so a sound's "title by
    artist" from one response is not lost to another response that carried only its author."""
    for key, label in new.items():
        held = names.get(key)
        if held and str(label).startswith(AUTHOR_ONLY) and not str(held).startswith(AUTHOR_ONLY):
            continue
        names[key] = label
    return names


def _sound_named(platform, title, author):
    """A sound's label: "title by artist" as _named writes it. A TikTok sound with no title but an author is named
    as TikTok itself shows an original sound, "original sound - <author>", so 42 can name it by its real author
    instead of its id. Never a made-up title: no author and no title is no name."""
    named = _named(title, author)
    author = author.strip() if isinstance(author, str) else ""
    if named or platform != "tiktok" or not author:
        return named
    return AUTHOR_ONLY + author


def body_names(route, params, body):
    """Readable names for counter items whose raw value is an id: (kind, platform, raw) -> label. A chart,
    music board or sound count row names its sound "title by artist"; a ranked post names its creator by
    display name (a YouTube channel title) and its sound by the music title when the post carries one.
    Hashtags and names need no body: default_label reads them from the raw value."""
    family, platform = ROUTES[route][0], ROUTES[route][1]
    data = _dict(body).get("data")
    out = {}
    for row in _rows(data) or ([data] if isinstance(data, dict) else []):  # a sound count is one bare node
        node = _dict(row.get("post")) or row
        author, content, ext = _dict(node.get("author")), _dict(node.get("content")), _dict(node.get("ext"))
        if route in ("apple_music/charts", "instagram/music/trending", "tiktok/song"):
            track = _dict(node.get("track"))
            label = _sound_named(
                platform,
                _first(track, "title", "name") or _first(content, "title", "text") or _first(node, "title", "name"),
                _first(track, "display_artist", "artist_name", "artist") or _first(node, "artist_name", "artist")
                or _first(author, "display_name", "name"))
            raws = [_first(track, k) for k in ("audio_cluster_id", "audio_asset_id", "audio_id", "id")]
            raws += [_first(node, k) for k in ("id", "song_id", "apple_id", "audio_id", "music_id")]
            raws += [params.get("clipId")] if route == "tiktok/song" else []
            add_names(out, {("sound", platform, str(raw)): label for raw in raws if raw is not None and label})
        elif family == "rank":
            creator = _named(_first(author, "display_name", "name") or _first(author, "username"))
            for raw in (_first(author, "username"), _first(author, "id"), _first(author, "channel_id"),
                        _first(ext, "channel_id")):
                if raw is not None and creator:
                    out[("creator", platform, str(raw))] = creator
            music = _dict(node.get("music")) or _dict(node.get("sound"))
            sound = _sound_named(platform, _first(music, "title", "name"),
                                 _first(music, "author", "author_name", "authorName", "artist"))
            for raw in (_first(ext, "music_id", "audio_id"), _first(node, "music_id", "audio_id", "sound_id"),
                        _first(music, "id")):
                if raw is not None and sound:
                    add_names(out, {("sound", platform, str(raw)): sound})
    return out


def chart_names(fetch, body):
    """(kind, platform, raw) -> "title by artist" for a local chart page, keyed as local_sources keys it."""
    source = local_sources.SCRAPE_SOURCES.get(fetch.source)
    if source is None or fetch.source == "nairaland":
        return {}
    entries = local_sources.chart_entries(local_sources._markdown(body), source)
    return {("sound", fetch.platform, str(e["native"] or local_sources.song_key(e))):
            _named(*_visible_song(e["title"], e["artist"])) for e in entries}


def _visible_song(title, artist):
    """A chart cell's readable title and artist. TurnTable writes a song as "<br>Title<br>Artist": two visible
    lines and no artist read as title and artist, any other count joins on spaces. The raw cell stays the key."""
    def lines(value):
        text = html.unescape(re.sub(r"<[^>]*>", "\n", str(value or "")))
        return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]

    shown, by = lines(title), lines(artist)
    if len(shown) == 2 and not by:
        return shown[0], shown[1]
    return " ".join(shown), " ".join(by)


def default_label(kind, raw):
    """The label a raw value gives by itself: '#' and the tag for a hashtag, the name for a brand, topic,
    event, meme or format; sounds and creators are ids and need body_names."""
    if kind == "hashtag":
        return "#" + str(raw).strip().lstrip("#")
    return None if kind in ("sound", "creator") else str(raw).strip() or None


def named_items(made, names):
    """item_id -> (kind, raw, platform, label or None) for every id the run made."""
    return {item: (kind, raw, platform, names.get((kind, platform, str(raw))) or default_label(kind, raw))
            for item, (kind, raw, platform) in made.items()}


def _row_ok(row):
    return isinstance(row, dict) and not row.get("error") and \
        str(row.get("status") or "ok").lower() in ("ok", "success")


@dataclass
class Collected:
    run_id: str
    posts: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    counters: list = field(default_factory=list)
    creators: list = field(default_factory=list)
    records: list = field(default_factory=list)
    candidates: dict = field(default_factory=dict)
    client_calls: list = field(default_factory=list)
    stopped: str | None = None
    balance_unread: bool = False
    stopped_markets: set = field(default_factory=set)
    credits: float = 0
    spent: dict = field(default_factory=dict)
    item_id_skips: int = 0
    song_curve_sample_skipped: int = 0
    seed_rows: list = field(default_factory=list)
    local_credits: float = 0
    local_error: str | None = None
    news_seeds: list = field(default_factory=list)
    names: dict = field(default_factory=dict)
    items: dict = field(default_factory=dict)
    watches: int = 0
    watch_calls: int = 0
    watches_not_planned: list = field(default_factory=list)
    public_feed_raw_rows: list = field(default_factory=list)
    public_feed_summary: dict | None = None
    public_feed_error: str | None = None
    telegram_raw_rows: list = field(default_factory=list)
    telegram_summary: dict | None = None
    telegram_error: str | None = None
    trends_credits: float = 0
    trends_error: str | None = None
    search_signals: list = field(default_factory=list)
    search_states: dict | None = None
    search_rows: list = field(default_factory=list)
    bq_search_states: dict | None = None
    bq_trends_bytes: int = 0
    bq_trends_error: str | None = None
    rss_states: dict | None = None
    rss_error: str | None = None
    google_terms_read: int = 0
    google_terms_error: str | None = None
    google_seeds: dict | None = None

    def counts(self):
        """credits_by_share holds the SocialCrawl phases' spend by budget share (GLOBAL and the markets) and
        the local sources' as LOCAL, so it sums to credits_charged."""
        public_records = [r for r in self.records if r.get("route") == "public_feed"]
        job_records = [r for r in self.records if r.get("route") not in ("public_feed", TELEGRAM_ROUTE)]
        trends = {} if self.search_states is None else {
            "search_signals": len(self.search_signals), "search_signal_states": dict(self.search_states),
            "trends_credits": self.trends_credits, "trends_error": self.trends_error}
        counts = {"calls": len(self.client_calls), "calls_ok": sum(r["ok"] for r in job_records if r["row"] != local_sources.ROW),
                "credits_charged": self.credits + self.local_credits,
                "credits_by_share": {**self.spent, "LOCAL": self.local_credits},
                "local_credits": self.local_credits, "local_error": self.local_error,
                "local_records": sum(r["row"] == local_sources.ROW and r.get("route") != "public_feed"
                                     for r in self.records),
                "news_seeds": len(self.news_seeds),
                "posts": len({p["post_id"] for p in self.posts}), "observations": len(self.observations),
                "counters": len(self.counters),
                "creators": len({(c["platform"], c["creator_id"]) for c in self.creators}),
                "item_id_skips": self.item_id_skips,
                "song_curve_sample_skipped": self.song_curve_sample_skipped, "stopped": self.stopped,
                "markets_stopped": sorted(self.stopped_markets),
                "not_made": sum(r["status"] in ("not_made", "over_share", "day_changed")
                                 for r in job_records),
                "day_changed": sum(r["status"] == "day_changed" for r in job_records),
                "seeds_used": len(self.seed_rows), "watches": self.watches, "watch_calls": self.watch_calls,
                "watches_not_planned": len(self.watches_not_planned),
                "public_feed_records": len(public_records),
                "public_feed_records_by_market": {m: sum(r["market"] == m for r in public_records)
                                                   for m in MARKETS},
                "public_feed_raw_rows": len(self.public_feed_raw_rows),
                "public_feed_summary": self.public_feed_summary,
                "public_feed_error": self.public_feed_error}
        if trends:
            counts["credits_charged"] += self.trends_credits
            counts["credits_by_share"]["TRENDS"] = self.trends_credits
        if self.bq_search_states is not None:
            trends.update({"google_bq_signals": sum(r.get("source") == "google_bq" for r in self.search_rows),
                           "google_bq_states": dict(self.bq_search_states),
                           "google_bq_bytes_billed": self.bq_trends_bytes, "google_bq_error": self.bq_trends_error})
        if self.google_seeds is not None:
            counts.update({"google_rss_signals": sum(r.get("source") == google_rss.SOURCE for r in self.search_rows),
                           "google_rss_states": dict(self.rss_states or {}), "google_rss_error": self.rss_error,
                           "google_terms_read": self.google_terms_read, "google_terms_error": self.google_terms_error,
                           "google_seeds": dict(self.google_seeds)})
        if self.telegram_summary is not None or self.telegram_error is not None:
            # Only when the Telegram phase ran (core/public_feeds/telegram.py TELEGRAM_READY); off, no key is added.
            counts.update({"telegram_summary": self.telegram_summary, "telegram_error": self.telegram_error,
                           "telegram_records": sum(r.get("route") == TELEGRAM_ROUTE for r in self.records),
                           "telegram_raw_rows": len(self.telegram_raw_rows)})
        return {**counts, **trends}


class _Runner:
    def __init__(self, client, run, ids, geo, clock, budget, last_pulls, run_date, only=None, keep=frozenset(),
                 reels_room=None, profiles=None):
        self.client, self.run, self.ids, self.geo, self.clock = client, run, ids, geo, clock
        self.day = run_date.isoformat()
        self.budget, self.pulls = budget, dict(last_pulls or {})
        self.first_seen, self.done = {}, {m: [] for m in (GLOBAL,) + MARKETS}
        self.only, self.keep = only, frozenset(keep)
        self.reels_room = reels_room  # the run's charges a row 14i call may bring up to, with its hold
        self.profiles = profiles
        self.country_deadline = None

    def wanted(self, call):
        """In a repair run (only set): a call of a listed route whose health key the base run did not already
        get valid. Every call otherwise."""
        if self.only is None:
            return True
        return call.route in self.only and \
            (call.health_market(), call.series(), call.series_protocol()) not in self.keep

    def execute(self, by_market, skipped=()):
        for market, block in interleaved(by_market):
            block = [c for c in block if self.wanted(c)]
            if block:
                self.calls(block, market)
        for s in skipped:
            if self.only is None:
                self._skip(s)

    def calls(self, calls, market):
        for call in calls:
            if self.run.stopped or market in self.run.stopped_markets:
                self._record(call, "not_made", self.clock())
                continue
            moment = self.clock()
            if not self._on_day(call, moment):
                self._record(call, "day_changed", moment)  # its rows would land on another market day
                continue
            try:
                hold = call.hold()
            except Refused:
                hold = 0  # the client refuses it and says why
            if not self.budget.fits(call, hold) or ((call.route in (REELS_ROUTE,) + location_sources.ROUTES or call.family == "profile") and self.reels_room is not None
                                                    and self.run.credits + hold > self.reels_room):
                self._record(call, "over_share", self.clock())
                continue
            if self.only is not None:
                # A repair reads the vendor again: a same-day stored response is the one being repaired.
                call = copy.copy(call)
                call.use_cache = False
            if call.pull_seq is not None:
                key = (call.market, call.series(), call.series_protocol())
                self.pulls[key] = self.pulls.get(key, 0) + 1
                call = copy.copy(call)
                call.pull_seq = self.pulls[key]
            if self.profiles is not None and call.family == "profile":
                self.profiles.attempted.add(self.profiles.key(ROUTES[call.route][1], call.params.get("handle")))
                result = self.client.account_profile(ROUTES[call.route][1], call.params["handle"], method=call.method,
                    market=call.market, seed_key=call.seed_key, lane=call.lane, use_cache=call.use_cache)
            else:
                result = self.client.call(call.route, call.params, method=call.method, market=call.market,
                                          item_id=call.seed.item_id if call.seed else None, seed_key=call.seed_key,
                                          lane=call.lane, use_cache=call.use_cache)
            fetched = self.clock()
            self.run.client_calls.append({"route": call.route, "params": call.params, "market": call.market,
                                          "status": result.status})
            self.run.credits += result.credits_charged or 0
            self.budget.spend(call, result.credits_charged)
            if not self._on_day(call, fetched):
                # It came back on another market day: ledger and raw_responses hold it, but it gives no rows.
                self._record(call, "day_changed", fetched)
            else:
                status = result.status
                try:
                    parsed = self._parse(call, result, fetched)
                except Exception:
                    if call.route not in (REELS_ROUTE,) + location_sources.ROUTES and call.family != "profile":
                        raise
                    # Row 14i is an extra search lane: a body it cannot read fails that call, not the run.
                    log.exception("collect %s %s: response not parsed", call.market, call.route)
                    parsed, status = None, "unparsable"
                self._record(call, status, fetched, result, parsed)
                self.done[market].append((call, result, parsed))
            if result.status not in OK:
                log.warning("collect %s %s %s: %s", call.market, call.route, result.status, result.reason)
            if result.status == "cap_reached":
                self.run.stopped_markets.add(market)
            elif result.status in STOP_ALL:
                self.run.stopped = result.status
                self.run.balance_unread = self.run.balance_unread or result.failure == "balance_unread"

    def _on_day(self, call, moment):
        return {_local_day(moment, call.market), _local_day(moment, call.health_market())} == {self.day}

    def _skip(self, s):
        self.run.records.append({"row": s["row"], "route": s["route"], "market": s["market"],
                                 "day": self.day, "platform": s["platform"],
                                 "series": s["series"], "protocol": s["protocol"], "lane_class": s["lane_class"],
                                 "status": "skipped", "ok": False, "calls": 0, "units_planned": 1, "units_ok": 0,
                                 "items": 0, "post_ids": [], "reason": s["detail"]})

    def _parse(self, call, result, fetched):
        if result.status not in OK or result.body is None:
            return None
        parsed = parse_with_creators(
            call.route, call.params, call.market, result.body, fetched, self.run.run_id, item_id_fn=self.ids,
            geo_fn=self.geo, lane=call.lane if call.family == "search" or call.seed else None, seed_key=call.seed_key,
            pull_seq=call.pull_seq, protocol=call.protocol, profile_cache=self.profiles)
        if call.family == "profile" and not parsed["creators"]:
            raise ValueError("account profile owner unavailable")
        if call.route == "tiktok/song/videos" and call.seed is None:
            self.run.song_curve_sample_skipped += adoption_sample_points(result.body)
        listed_key = "pageId" if call.route == "facebook/profile/posts" else \
            "location_id" if call.route == "instagram/location/posts" else \
            "subreddit" if call.route == "reddit/subreddit" else None
        listed_source = listed_key and call.params.get(listed_key)
        curated_source = call.route == "prism/profiles" and call.params.get("items")
        if (listed_source or curated_source) and call.source_market in MARKETS and call.source_market == call.market:
            for observation in parsed["observations"]:
                observation["source_market"] = call.source_market
        if call.seed is not None and call.seed.source == seeding.RESEARCH_SOURCE:
            for observation in parsed["observations"]:
                observation["source_market"] = None
                observation["source_region"] = None
        self.run.creators += parsed.pop("creators")
        self.run.posts += parsed["posts"]
        self.run.observations += parsed["observations"]
        self.run.counters += parsed["counters"]
        if call.family in ("rank", "board", "count"):
            add_names(self.run.names, body_names(call.route, call.params, result.body))
        posts = {p["post_id"]: p for p in parsed["posts"]}
        for obs in parsed["observations"]:
            if obs["market"] in MARKETS and obs["post_id"] in posts:
                self.first_seen.setdefault(obs["post_id"], (obs["market"], posts[obs["post_id"]]))
        return parsed

    def _record(self, call, status, moment, result=None, parsed=None):
        """One call's health inputs, dated on the market-local day of its fetch, as parse dates its rows."""
        ok = status in OK and result is not None and result.body is not None
        profiles = call.route == "prism/profiles"
        # Left out of the day's plan: by the override's shares, or because the market day moved on.
        planned = status not in ("over_share", "day_changed")
        day = self.day if status == "day_changed" else _local_day(moment, call.health_market())
        units_planned = (len(call.params["items"]) if profiles else 1) if planned else 0
        units_ok = sum(_row_ok(r) for r in result.items) if ok and profiles else int(ok)
        parsed = parsed or {"posts": [], "observations": [], "counters": []}
        items = len(parsed["observations"] if call.family in POST_FAMILIES or call.seed else parsed["counters"])
        market = call.health_market()
        self.run.records.append({
            "row": call.row, "route": call.route, "market": market, "day": day,
            "platform": ROUTES[call.route][1], "series": call.series(), "protocol": call.series_protocol(),
            "lane_class": call.lane_class(), "status": status, "ok": ok, "calls": int(planned),
            "units_planned": units_planned, "units_ok": units_ok, "items": items,
            "post_ids": [p["post_id"] for p in parsed["posts"]], "reason": result.reason if result is not None else ("day_changed" if status == "day_changed" else ""),
            "failure": getattr(result, "failure", "") if result is not None else "",
        })


DAY_CHANGED = "day_changed:"


class _DayChanged(Exception):
    pass


class _LocalLane:
    """The collect client for the local sources phase: every call is ledgered in lane local, a call whose
    market day has moved past the run date is not made, and the charges are summed here, so they are known
    even when the phase fails. Page bodies are kept by URL for the chart names (chart_names)."""

    def __init__(self, client, day, clock):
        self.client, self.day, self.clock = client, day, clock
        self.charged = 0
        self.pages = {}

    def call(self, route, params=None, *, market=None, **kw):
        if _local_day(self.clock(), market) != self.day:
            return Result("day_changed", route, reason=f"{DAY_CHANGED} {market} is past {self.day}")
        result = self.client.call(route, params, market=market, **{**kw, "lane": "local"})
        self.charged += result.credits_charged or 0
        if route == "web/scrape" and result.status in OK and isinstance(result.body, dict):
            self.pages[(params or {}).get("url"), market] = result.body
        return result


def _day_get(get, fetches, day, clock):
    """get for the free fetches, refusing a page whose market day has moved past the run date."""
    markets = {f.url: f.market for f in fetches if not f.paid}

    def on_day(url):
        market = markets.get(url)
        if market and _local_day(clock(), market) != day:
            raise _DayChanged(f"{DAY_CHANGED} {market} is past {day}")
        return get(url)

    return on_day


def _local_not_made(run, fetches, clock, status, reason):
    for f in fetches:
        run.records.append({
            "row": local_sources.ROW, "route": f.route, "market": f.market, "day": _local_day(clock(), f.market),
            "platform": f.platform, "series": f.series, "protocol": f.protocol(), "lane_class": f.lane_class,
            "status": status, "ok": False, "calls": 0,
            "units_planned": len(f.params["items"]) if f.route == "prism/profiles" else 1, "units_ok": 0,
            "items": 0, "post_ids": [], "reason": reason})


def _same_day(out, day):
    """Rows dated on another market day are dropped and their fetches recorded day_changed, as the other
    phases do for a call made or answered after its market's day moved on."""
    kept = [o for o in out["observations"] if o["observed_date"] == day]
    dropped = {o["post_id"] for o in out["observations"]} - {o["post_id"] for o in kept}
    out["observations"] = kept
    out["posts"] = [p for p in out["posts"] if p["post_id"] not in dropped]
    out["counters"] = [c for c in out["counters"] if c["obs_date"] == day]
    for r in out["records"]:
        if r["reason"].startswith(DAY_CHANGED) or r["day"] != day:
            r.update(status="day_changed", ok=False, calls=0, day=day, units_ok=0, items=0, post_ids=[],
                     reason=r["reason"] if r["reason"].startswith(DAY_CHANGED) else f"{DAY_CHANGED} answered late")


def local_phase(run, client, day, *, get, clock, item_id_fn, geo_fn, last_pulls, cap, profile_cache=None):
    """Task 2.12's local sources after the SocialCrawl phases, paid from the collect share under their own
    sub-limit (local_sources.LOCAL_DAILY_CAP). Not made when the run stopped for credits or balance, or when
    cap less what the run has charged is under the phase's hold: every planned fetch is then recorded
    not_made with the reason. A fetch whose market day has moved is not made (day_changed). An exception
    is recorded as run.local_error with every fetch failed on Coverage; it never stops the writes. News
    seeds keep only phrases gdelt.blocked lets through (rule 1)."""
    from core.collect.gdelt import blocked

    fetches = local_sources.plan(day)
    hold = local_sources.total_hold(fetches)
    left = cap - run.credits - run.trends_credits
    reason = f"stopped: {run.stopped}" if run.stopped else \
        f"{left:g} of the collect share's {cap} left is under the local hold {hold}" if left < hold else None
    if reason:
        _local_not_made(run, fetches, clock, "not_made", reason)
        return
    lane = _LocalLane(client, day.isoformat(), clock)
    try:
        out = local_sources.run(day, run.run_id, client=lane, get=_day_get(get, fetches, day.isoformat(), clock),
                                clock=clock, item_id_fn=item_id_fn, geo_fn=geo_fn, last_pulls=last_pulls,
                                profile_cache=profile_cache)
    except Exception as exc:
        log.exception("collect %s: the local sources phase failed", run.run_id)
        run.local_error = f"{type(exc).__name__}: {exc}"[:500]
        run.local_credits = lane.charged
        _local_not_made(run, fetches, clock, "error", f"local phase failed: {run.local_error}")
        return
    _same_day(out, day.isoformat())
    run.posts += out["posts"]
    run.observations += out["observations"]
    run.counters += out["counters"]
    run.records += out["records"]
    run.local_credits = out["credits"]
    for f in fetches:
        if (f.url, f.market) in lane.pages:
            run.names.update(chart_names(f, lane.pages[f.url, f.market]))
    run.news_seeds = [dict(s, seed_date=str(s["seed_date"])) for s in out["seeds"] if not blocked(s["query"])]


class _TrendsLane:
    """The collect client for the Google Trends phase: the share and run id pass through for
    google_trends.collect_search_signals, and the charges are summed here, so they are known even when the
    phase fails."""

    def __init__(self, client):
        self.client, self.charged = client, 0
        self.share, self.run_id = getattr(client, "share", None), getattr(client, "run_id", None)

    def call(self, route, params=None, **kw):
        result = self.client.call(route, params, **kw)
        self.charged += getattr(result, "credits_charged", 0) or 0
        return result


def trends_phase(run, client, day, *, clock, cap, reserve=0):
    """Google Trends after the SocialCrawl phases: one 24 hour google_trends/trending read per market on the
    collect share, lane google_trending, at most google_trends.MAX_SC_CREDITS. Not made when the run stopped
    for credits or balance, or when cap less what the run has charged and less reserve (the local phase's
    hold) is under that hold; every market is then recorded not_made. An exception is recorded as
    run.trends_error and never stops the run. The bodies are kept in raw_responses by the client; the
    signals go to the run's counts and, as source google_trending, to run.search_rows for
    google_search_signals, the only Google search interest KE has (the public tables hold no KE rows)."""
    hold = google_trends.MAX_SC_CREDITS
    left = cap - run.credits - reserve
    if run.stopped or left < hold:
        run.search_states = {m: "not_made" for m in google_trends.MARKETS}
        why = f"stopped: {run.stopped}" if run.stopped else \
            f"{left:g} of the collect share's {cap} left after the local hold {reserve:g} is under {hold}"
        log.info("collect %s: Google Trends not made: %s", run.run_id, why)
        return
    lane = _TrendsLane(client)
    try:
        batch = google_trends.collect_search_signals(lane, run_id=run.run_id, run_date=day, fetched_at=clock(),
                                                     raw_receipts=(), ledger_receipts=())
        signals = [s.as_consumer_row() for s in batch.signals]
        states = {m: state.status for (m, _), state in batch.states.items()}
        rows = batch.load_rows()
    except Exception as exc:
        log.exception("collect %s: the Google Trends phase failed", run.run_id)
        run.trends_error = f"{type(exc).__name__}: {exc}"[:500]
        run.search_states = {m: "error" for m in google_trends.MARKETS}
    else:
        run.search_signals, run.search_states = signals, states
        run.search_rows = run.search_rows + rows
    run.trends_credits = lane.charged


def google_bq_enabled():
    """False while the public BigQuery Google Trends phase is parked (core/config/google_sources.yaml, W8-DEC-04).
    A missing file or key reads as parked."""
    try:
        config = yaml.safe_load((CONFIG / "google_sources.yaml").read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return False
    return (config.get("google_bq") or {}).get("enabled") is True


def bq_trends_phase(run, bq, day, *, clock):
    """The free BigQuery Google Trends tables for ZA and NG (google_trends.read_bq_signals): the newest
    partition of the top and rising terms, no SocialCrawl credits. The rows join run.search_signals with the
    same label and join run.search_rows for google_search_signals. Search interest only: never posts, evidence
    or a Today card, and never a seed. An exception is recorded as run.bq_trends_error and never stops the run."""
    keys = [f"{m}:{k}" for k in google_trends.BQ_TABLES for m in google_trends.BQ_MARKETS]
    if not google_bq_enabled():
        run.bq_search_states = {key: "parked" for key in keys}
        return
    try:
        batch = google_trends.read_bq_signals(bq, run_date=day, fetched_at=clock())
        rows = batch.load_rows()
    except Exception as exc:
        log.exception("collect %s: the Google Trends tables phase failed", run.run_id)
        run.bq_trends_error = f"{type(exc).__name__}: {exc}"[:500]
        run.bq_search_states = {key: "error" for key in keys}
        return
    run.search_signals = run.search_signals + [s.as_consumer_row() for s in batch.signals]
    run.search_rows = run.search_rows + rows
    run.bq_search_states = {f"{m}:{k}": batch.states[m, k].status for k in google_trends.BQ_TABLES
                            for m in google_trends.BQ_MARKETS}
    run.bq_trends_bytes = batch.bytes_billed


def google_seed_phase(run, live, day, *, transport, terms, clock):
    """Google search terms into the day's row 14 queue, before the expansion phase: Google's daily trends feed
    read now through transport (google_rss, 0 credits; its rows join run.search_rows for google_search_signals),
    then the previous run date's google_rss and google_trending rows that terms() reads. BigQuery terms are excluded
    by TERMS_SQL and SEED_SOURCES; the current trending read runs after expansion, so its previous day is used. Up to
    google_trends.GOOGLE_SEEDS_PER_MARKET terms a market, deduplicated against live[market] by cluster, are
    taken in turn with live[market]'s own seeds (a Google term first), so a long queue cannot push them out:
    they take existing row 14 slots in seeds.mix and add no call or credit. A failed read is recorded on run
    and never stops the run. Query triage only (RULES.md rule 2)."""
    fresh = []
    if transport is not None:
        try:
            batch = google_rss.read_signals(transport, run_id=run.run_id, run_date=day, clock=clock)
            fresh = batch.load_rows()
            run.rss_states = {m: state.status for (m, _), state in batch.states.items()}
        except Exception as exc:
            log.exception("collect %s: the Google daily trends feed read failed", run.run_id)
            run.rss_error = f"{type(exc).__name__}: {exc}"[:500]
            run.rss_states = {m: "error" for m in google_rss.MARKETS}
        run.search_rows = run.search_rows + fresh
    previous = []
    if terms is not None:
        try:
            previous = list(terms())
        except Exception as exc:
            log.warning("collect %s: the previous day's Google search terms were not read (%s)", run.run_id,
                        type(exc).__name__)
            run.google_terms_error = f"{type(exc).__name__}: {exc}"[:500]
        run.google_terms_read = len(previous)
    picked = google_trends.queue_seeds(fresh + previous, run_date=day, existing_terms_by_market={
        m: [s.get("query") for s in live.get(m, [])] for m in MARKETS})
    for m in MARKETS:
        live[m] = seeding._interleave(picked[m], list(live.get(m, [])))
    run.google_seeds = {m: len(picked[m]) for m in MARKETS}


def country_phase(run, runner, profile_reader, cap):
    cache = runner.profiles
    if cache is None:
        return
    def apply():
        cache.apply(runner.geo)
        present = {id(row) for row in run.creators}
        run.creators += [row for row in cache.creators if id(row) not in present and row.get("home_market") is not None]

    keys = list(cache.accounts)
    timeout = getattr(runner.client, "timeout", None)
    deadline = runner.country_deadline
    reader_kwargs = {}
    if deadline is not None:
        remaining = (deadline - runner.clock()).total_seconds()
        if remaining <= 0:
            apply()
            return
        reader_kwargs["timeout"] = min(timeout, remaining / 2)
    if keys:
        try:
            cache.seed(profile_reader(keys, **reader_kwargs), today=runner.clock().astimezone(timezone.utc).date())
        except Exception as exc:
            log.warning("collect %s: account country cache unreadable (%s)", run.run_id, type(exc).__name__)
            apply()
            return
    runner.reels_room = cap - run.local_credits - run.trends_credits
    blocked = set()
    for account in cache.needed():
        route = location_sources.COUNTRY_ROUTES[account["platform"]]
        call = Call("C1", route, {"handle": account["handle"]}, account["market"], "panel")
        late = deadline is not None and (deadline - runner.clock()).total_seconds() < 2 * timeout + runner.client.retry_wait
        if route in blocked or late:
            runner._record(call, "not_made", runner.clock(), Result("not_made", route,
                reason="country capture time budget" if late else "country endpoint failed earlier"))
            continue
        try:
            runner.calls([call], account["market"])
        except Exception as exc:
            log.warning("collect %s: account country capture stopped (%s)", run.run_id, type(exc).__name__)
            raise
        if runner.done.get(account["market"]) and runner.done[account["market"]][-1][0] is call:
            result = runner.done[account["market"]][-1][1]
            if result.failure == "store":
                break
            if result.failure in TRANSIENT:
                blocked.add(route)
    apply()


def collect(client, run_date, run_id, *, item_id_fn, geo_fn, clock, config=None, x_trends=False, share_cap=None,
            last_pulls=None, seeds=None, local_get=None, watches=None, known_posts=None,
            public_feed_transport=None, search_signals=False, only_routes=None, keep=frozenset(),
            google_rss_transport=None, google_terms=None, country_profiles=None, on_run=None):
    """Make every call of the run through client and parse each response. Nothing is written here.
    share_cap is the overridden collect cap (None without an override); last_pulls maps (market, series,
    protocol) to the last pull number already stored; seeds holds the seed_queue rows seeds.read gave;
    watches the current watches writers.read_watches gave. run.seed_rows gets one yield row per seed used,
    and run.items names every item the run made an id for (named_items). With local_get, the free getter
    (local_sources.http_get live), the local sources phase runs last on the same client. With search_signals,
    the Google Trends phase (trends_phase) runs before it, keeping the local hold in reserve. With
    google_rss_transport (the public feed reader's transport live) or google_terms (a callable giving the previous
    day's google_search_signals rows), google_seed_phase adds the day's Google search terms to the row 14 queue
    before the run's calls.

    A repair run (only_routes, from COLLECT_ONLY_ROUTES) makes only the calls of those routes, less the ones
    whose (market, series, protocol) is in keep (valid in the base run, so carried forward instead), each
    read live without the same-day cache. The Google Trends, Google seed, local sources and public feed phases
    are not run, and no other call is made or recorded."""
    config = config or load_config()
    run = Collected(run_id)
    if on_run is not None:
        on_run(run)  # a failed run still has its calls and credits to report
    ids = _CountedIds(item_id_fn)
    budget = Budget(share_cap)
    cap = share_cap if share_cap is not None else load_caps()["ENGINE_DAILY"]["collect"]
    # Row 14i leaves room in the share for the Google Trends and local sources phases that follow it.
    reels_room = cap - (google_trends.MAX_SC_CREDITS if search_signals else 0) \
        - (local_sources.total_hold(local_sources.plan(run_date)) if local_get is not None else 0)
    runner = _Runner(client, run, ids, safe_geo(geo_fn), clock, budget, last_pulls, run_date, only=only_routes,
                     keep=keep, reels_room=reels_room,
                     profiles=location_sources.ProfileCache() if country_profiles is not None and only_routes is None else None)
    timeout = getattr(client, "timeout", None)
    if runner.profiles is not None and isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0:
        runner.country_deadline = clock() + chain.TIMEOUTS["collect"] - COUNTRY_TAIL_RESERVE             - timedelta(seconds=timeout)
    if only_routes is not None:
        search_signals, local_get, public_feed_transport = False, None, None
        google_rss_transport, google_terms = None, None
    focus = {}

    def rank(market):
        ranked = score_candidates(sightings(runner.done[market]))
        run.candidates[market] = ranked
        focus[market] = set(ranked["hashtag"][:search_calls(market)]) | set(ranked["sound"][:SONG_VIDEOS])
        return ranked

    live, record = seeding.live(seeds or [], run_date, MARKETS), seeding.record(seeds or [])
    if google_rss_transport is not None or google_terms is not None:
        google_seed_phase(run, live, run_date, transport=google_rss_transport, terms=google_terms, clock=clock)

    def queue(market):
        return live[market], {c: uses for (m, c), uses in record.items() if m == market}

    watched, run.watches_not_planned = watch_calls(watches or [])
    run.watches, run.watch_calls = len(watches or []), len(watched)
    for watch_id, why in run.watches_not_planned:
        log.info("collect %s: watch %s not read: %s", run_id, watch_id, why)
    curated_records = curated_creators.load_manifest()
    curated_limit = _curated_limit(run_date, curated_records, config=config, x_trends=x_trends,
                                   share_cap=share_cap, watches=watches or ())
    reels = dict(REELS_TOP) if share_cap is None and only_routes is None else {}
    drive(runner.execute, run_date, config, x_trends=x_trends, override=share_cap is not None, rank=rank,
          evidence=lambda: pick_evidence(runner.first_seen, focus), queue=queue, watched=watched,
          curated_records=curated_records, curated_limit=curated_limit, reels=reels)
    if only_routes is None:
        location_calls, held = location_sources.plan(run_date, config)
        runner.execute({m: [Call(**c) for c in location_calls if c["market"] == m] for m in MARKETS})
        for entry in held:
            log.warning("collect %s %s held: %s", entry["market"], entry["route"], entry["reason"])
    found = {p["post_id"] for m in MARKETS for call, _, parsed in runner.done[m] if call.seed
             for p in (parsed or {}).get("posts", [])}
    known = set()
    if known_posts is not None and found:
        try:
            known = known_posts(found)
        except Exception:
            log.exception("collect %s: the posts read for seed yields failed", run_id)
            known = None
    run.seed_rows = [row for m in MARKETS for row in seeding.yield_rows(run_date, m, runner.done[m], known)]
    if search_signals:
        reserve = local_sources.total_hold(local_sources.plan(run_date)) if local_get is not None else 0
        trends_phase(run, client, run_date, clock=clock, cap=cap, reserve=reserve)
    if local_get is not None:
        local_phase(run, client, run_date, get=local_get, clock=clock, item_id_fn=ids, geo_fn=geo_fn,
                    last_pulls=last_pulls, cap=cap, profile_cache=runner.profiles)
    if country_profiles is not None and only_routes is None:
        country_phase(run, runner, country_profiles, cap)
    public_items = {}
    if public_feed_transport is not None:
        from core.collect import public_feed_collect

        try:
            public = public_feed_collect.run(
                run_date,
                run.run_id,
                transport=public_feed_transport,
                clock=clock,
                item_id_fn=ids,
                last_pulls=last_pulls,
                stopped=lambda: run.stopped,
            )
        except Exception as exc:
            run.public_feed_error = type(exc).__name__
            log.error("collect %s: public feed phase failed (%s)", run_id, run.public_feed_error)
        else:
            run.posts += public["posts"]
            run.observations += public["observations"]
            run.counters += public["counters"]
            run.records += public["records"]
            run.public_feed_raw_rows += public["raw_rows"]
            run.public_feed_summary = public["summary"]
            public_items = public["items"]
    from core.public_feeds import telegram as telegram_feed

    if telegram_feed.TELEGRAM_READY and public_feed_transport is not None:
        telegram_phase(run, run_date, transport=public_feed_transport, clock=clock)
    run.item_id_skips = ids.skipped
    run.items = named_items(ids.made, run.names)
    for item, entry in public_items.items():
        if item not in run.items or run.items[item][3] is None:
            run.items[item] = entry
    run.spent = dict(budget.spent)
    return run


def telegram_phase(run, run_date, *, transport, clock):
    """The vetted public Telegram channels (core/collect/telegram_collect.py), 0 credits, only when
    core/public_feeds/telegram.py TELEGRAM_READY is True. Its posts are sightings in their channel's market's own
    feeds (source_market), so they count as local one step lower, as the gossip panel's listed pages do. An
    exception is kept as run.telegram_error and never fails or stops the run."""
    from core.collect import telegram_collect

    try:
        out = telegram_collect.run(run_date, run.run_id, transport=transport, clock=clock,
                                   stopped=lambda: run.stopped)
    except Exception as exc:
        run.telegram_error = type(exc).__name__
        log.error("collect %s: Telegram phase failed (%s)", run.run_id, run.telegram_error)
        return
    run.posts += out["posts"]
    run.observations += out["observations"]
    run.creators += out["creators"]
    run.records += out["records"]
    run.telegram_raw_rows += out["raw_rows"]
    run.telegram_summary = out["summary"]


# Entry point

def collect_caps(env):
    """caps.yaml, with COLLECT_CAP_OVERRIDE applied when it lowers the collect share; ValueError otherwise."""
    caps = copy.deepcopy(load_caps())
    raw = env.get("COLLECT_CAP_OVERRIDE")
    if raw in (None, ""):
        return caps
    share = caps["ENGINE_DAILY"]["collect"]
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"COLLECT_CAP_OVERRIDE={raw!r} is not a whole number") from None
    if not 0 <= value <= share:
        raise ValueError(f"COLLECT_CAP_OVERRIDE may only lower the collect cap of {share}, not set it to {value}")
    caps["ENGINE_DAILY"]["collect"] = value
    return caps


ONLY_ROUTES = "COLLECT_ONLY_ROUTES"


def only_routes(env):
    """The routes a repair run re-collects, from COLLECT_ONLY_ROUTES (comma separated), or None when it is unset.
    ValueError unless FORCE_RERUN=1 is set too, it names at least one route (not only commas) and every route is
    a rank list or board (REPAIR_ROUTES): the routes whose one failed day holds a platform's items (TRUST.md G1),
    and whose health keys stand alone."""
    raw = env.get(ONLY_ROUTES)
    if raw is None or not raw.strip():
        return None
    if env.get("FORCE_RERUN") != "1":
        raise ValueError(f"{ONLY_ROUTES} repairs a day that already has an ok collect run, so it needs FORCE_RERUN=1")
    routes = [r.strip() for r in raw.split(",") if r.strip()]
    if not routes:
        raise ValueError(f"{ONLY_ROUTES} is set but names no route; unset it for a normal run")
    bad = [r for r in routes if r not in REPAIR_ROUTES]
    if bad:
        raise ValueError(f"{ONLY_ROUTES} takes only rank list and board routes ({', '.join(sorted(REPAIR_ROUTES))}), "
                         f"not {', '.join(bad)}")
    return frozenset(routes)


def x_trends_on(env):
    return env.get("X_TRENDS_SEED") == "1"


def detect_fns():
    """Lane L2's item ids and geo, imported only when a run starts."""
    from core.detect import geo, items

    return (lambda kind, raw, platform: items.item_id(kind, items.canonical_key(kind, raw, platform))), \
        geo.geo_for_post


# The creators and geo backfill (--backfill-creators): stored responses only, no SocialCrawl call.

BACKFILL_JOBS = ("collect", "confirm", "pulse")  # the shares whose responses become posts; the probe's do not
BACKFILL_ROUTES = tuple(sorted(r for r, entry in ROUTES.items()
                               if entry[0] in ("rank", "count", "curve", "watch", "panel", "search", "location")))


def backfill_rows(raw_rows, geo_fn):
    """(creators, posts, skipped) from stored raw_responses rows. raw_responses keeps params_hash, not the
    params, so each body is parsed with what parse needs to accept it: feed=local on the TikTok feed (the only
    feed collect and the pulse call), pull 0 on rank lists, and the stored lane on search routes. No since=
    applies, so a panel's older posts are parsed too; fill_geo only touches posts already stored. A row
    parse refuses (a lane or market it does not take) is skipped and counted."""
    creators, posts, skipped = [], [], 0
    geo = safe_geo(geo_fn)
    for r in raw_rows:
        route, lane = r["route"], r["lane"]
        family = ROUTES[route][0]
        body = json.loads(r["body"]) if isinstance(r["body"], str) else r["body"]
        searched = family == "search" or (route in SEEDED and lane in SEARCH_LANES)
        try:
            out = parse_with_creators(
                route, {"feed": "local"} if route == "tiktok/trending" else {}, r["market"], body, r["fetched_at"],
                r["run_id"], item_id_fn=lambda kind, raw, platform: None, geo_fn=geo,
                lane=lane if searched else None, seed_key=r["seed_key"],
                pull_seq=0 if family == "rank" else None)
        except ValueError:
            skipped += 1
            continue
        creators += out["creators"]
        posts += out["posts"]
    return creators, posts, skipped


def backfill(client, since, until, geo_fn):
    """Creators (only under writers.CREATORS_WRITE) and NULL post geo at the known threshold from raw_responses
    fetched from since to until. Returns the counts."""
    raw = writers.read_raw(client, since, until, BACKFILL_JOBS, BACKFILL_ROUTES)
    creators, posts, skipped = backfill_rows(raw, geo_fn)
    found = len({(c["platform"], c["creator_id"]) for c in creators})
    if writers.CREATORS_WRITE:
        writers.merge_creators(client, creators)
    writers.fill_geo(client, posts)
    return {"raw_rows": len(raw), "skipped": skipped, "creators": found,
            "creators_written": found if writers.CREATORS_WRITE else 0,
            "geo_rows": len({p["post_id"] for p in posts if writers._known(p)}),
            "posts_seen": len({p["post_id"] for p in posts})}


def gdelt_cap():
    from core.collect.gdelt import MAX_BYTES  # gdelt imports this module

    return MAX_BYTES


def print_backfill_plan(since, until):
    print(f"backfill creators and post geo from raw_responses fetched {since.isoformat()} to {until.isoformat()} "
          "(UTC days)")
    print(f"  jobs: {', '.join(BACKFILL_JOBS)}")
    print(f"  routes: {', '.join(BACKFILL_ROUTES)}")
    print("  read: " + " ".join(writers.RAW_SQL.format(table=writers.table("raw_responses")).split()))
    print(f"  read cap: dry run first, refused above {gdelt_cap():,} bytes, then maximum_bytes_billed at that cap")
    if writers.CREATORS_WRITE:
        print("  write: the creators MERGE (new (platform, creator_id) rows inserted; handle, display_name, "
              "followers, verified, profile_location and last_seen moved; first_seen kept; nothing removed)")
    else:
        print("  creators write is off (writers.CREATORS_WRITE) until lane L2's readers join creators on platform "
              "too: creators are parsed and counted, not written")
    print("  write: the posts geo fill MERGE (geo_market, geo_confidence, geo_source only where the stored "
          f"geo_market is NULL and the new geo_confidence is at least {writers.GEO_KNOWN}; no INSERT, nothing "
          "removed)")
    print("plan only: no network, no BigQuery, no SocialCrawl call")


# Rows 1 to 7, the rank lists and boards: one failed day on any of them fails its platform's baseline health
# in the market (DATA.md 3.3) and so holds every item led by that platform for three days (TRUST.md G1). The
# client retries their transient failures (socialcrawl_client, retry_routes); local scrapes and panels keep
# one attempt, so the local cap and the panel effort stay as planned.
RETRY_ROUTES = frozenset(route for route, entry in ROUTES.items() if entry[0] in ("rank", "board"))
# The same lists are the ones COLLECT_ONLY_ROUTES may repair (only_routes).
REPAIR_ROUTES = RETRY_ROUTES


def live_client(run_id, caps, bq, clock):
    from core.collect.socialcrawl_client import SocialCrawlClient, requests_http
    from core.collect.stores import BigQueryLedgerStore, BigQueryRawStore

    return SocialCrawlClient(share="collect", run_id=run_id, mode="live", ledger=BigQueryLedgerStore(bq, chain.PROJECT),
                             raw=BigQueryRawStore(bq, chain.PROJECT), http=requests_http, clock=clock, caps=caps,
                             retry_routes=RETRY_ROUTES)


def next_stage():
    """The stage chain.start_next would start after collect, from CHAIN_UNDERSTAND; nothing is probed."""
    return next((stage for stage in chain.STAGES[1:] if chain.in_chain(stage)), None)


def restart_next(run_date, runs, jobs):
    """After AlreadyDone: when collect is ok for the date and the next stage has no runs row, start it.
    A failure exits non-zero so Cloud Run's retry tries again."""
    latest = runs.latest("collect", run_date)
    stage = next_stage()
    if latest is None or latest["status"] != "ok" or stage is None or runs.latest(stage, run_date) is not None:
        return 0
    try:
        chain.start_next("collect", run_date, jobs=jobs)
    except Exception:
        log.exception("collect for %s is ok but %s did not start", run_date, stage)
        return 1
    print(f"collect for {run_date.isoformat()} was ok and {stage} had not started; started it")
    return 0


def repair_counts(base, counts, written, only, keep):
    """A repair run's runs counts: the base run's own counts, which still describe the day's collection (Today's
    receipt reads posts, Fieldwork the Google Trends states, from the good collect run's counts), with the
    repair's own figures under "repair"."""
    return {**base["counts"], "repair": {
        **counts, "base_run_id": base["run_id"], "routes": sorted(only), "kept_valid": len(keep),
        "health_written": written["health_own"], "health_carried": written["health_carried"]}}


def print_plan(day, *, x_trends=False, share_cap=None, only_routes=None):
    planned = plan(day, x_trends=x_trends, share_cap=share_cap, only_routes=only_routes)
    cap = share_cap if share_cap is not None else load_caps()["ENGINE_DAILY"]["collect"]
    limits = shares(share_cap) if share_cap is not None else None
    print(f"collect plan for {day.isoformat()} ({day:%A}), collect cap {cap}"
          + (f", override shares {json.dumps(limits)}" if limits else ""))
    for market, calls in planned["calls"].items():
        print(market)
        for c in calls:
            params = json.dumps(c.params, separators=(",", ":"))
            print(f"  {c.row:4} {c.method:4} {c.route:25} {c.lane or '':11} {c.hold():>4}  {params}")
        for s in planned["skipped"]:
            if s["market"] == market:
                print(f"  {s['row']:4} skip {s['route']:25} {'':11} {0:>4}  {s['detail']}")
        expansion = [c for c in calls if c.row in ("14", "16") and c.seed]
        if expansion:
            paid = sum(c.hold() for c in expansion)
            clusters = Counter()
            for c in expansion:
                clusters[c.seed.cluster] += c.hold()

            def pct(test):
                return round(100 * sum(c.hold() for c in expansion if test(c.seed)) / paid)

            print(f"  expansion {paid} credits in rows 14 and 16: "
                  f"queue seeds {pct(lambda s: s.family == 'seed_queue')}%, anchor {pct(lambda s: s.lane == 'anchor')}%, "
                  f"exploration {pct(lambda s: s.lane == 'exploration')}%, "
                  f"largest cluster {round(100 * max(clusters.values()) / paid)}%")
        over = planned["over"][market]
        if over:
            routes = Counter(c.route for c in over)
            print(f"  over share, not made: {len(over)} calls holding {sum(c.hold() for c in over)} "
                  f"({', '.join(f'{n} {r}' for r, n in routes.items())})")
        paid = {}
        for c in calls:
            paid[budget_key(c)] = paid.get(budget_key(c), 0) + c.hold()
        print(f"  {market} hold {sum(c.hold() for c in calls)} over {len(calls)} calls"
              + (f"; paid from shares {json.dumps(paid)}" if limits else ""))
    if only_routes is not None:
        print(f"repair run ({ONLY_ROUTES}={','.join(sorted(only_routes))}, with FORCE_RERUN=1): total hold "
              f"{planned['total']} credits at most, read live; a live run leaves out each call whose health row is "
              "already valid in the day's good collect run and carries that run's health rows forward for every "
              "series it does not read. No local sources, Google Trends, public feeds or seed_queue rows.")
        return
    fetches = local_sources.plan(day)
    local = local_sources.total_hold(fetches)
    print(f"LOCAL sources, row {local_sources.ROW}: after the SocialCrawl phases, paid from the collect share, "
          "ledger lane local")
    for f in fetches:
        print(f"  {local_sources.ROW:4} {f.method:4} {f.route:25} {f.market:11} {f.hold():>4}  {f.url}"
              + ("" if f.priced() else "  (not priced, never called)"))
    print(f"  local sources hold {local} credits over {len(fetches)} fetches, "
          f"sub-limit {local_sources.LOCAL_DAILY_CAP} of the collect share")
    print(f"row {REELS_ROW} Instagram reel searches with the creator card: the ones above fit on planned holds; a live "
          f"run makes up to {json.dumps(REELS_TOP)} after row 22 while the collect share less what it has charged "
          f"keeps each call's hold and the Google Trends ({google_trends.MAX_SC_CREDITS}) and local sources holds")
    print("watches: none read in plan mode, which reads no BigQuery; a live run reads the active watches in "
          f"intelligence_42_agent.v_watches_current and gives them up to {COUNT_CALLS} row {WATCH_ROW} count "
          "reads in lane watchlist, taken from the candidates' row 12 reads")
    n = sum(len(cs) for cs in planned["calls"].values())
    print(f"total hold {planned['total']} credits over {n} calls (collect cap {cap}); "
          "placeholders stand for the candidates, seed_queue seeds and evidence posts a live run finds")
    print(f"combined hold {planned['total'] + local} credits against the collect cap {cap}: "
          f"SocialCrawl phases {planned['total']}, local sources {local}")
    if planned["total"] + local > cap:
        print("  the local phase is made only when the collect share still has its hold left "
              "after the SocialCrawl phases")
    if google_bq_enabled():
        print(f"Google Trends tables: {', '.join(google_trends.BQ_MARKETS)}, 0 SocialCrawl credits; the newest "
              f"partition from {google_trends.BQ_DATASET}.INFORMATION_SCHEMA.PARTITIONS (dry run first, refused above "
              f"{google_trends.BQ_META_MAX_BYTES:,} bytes), then each table dry-run first, refused above "
              f"{google_trends.BQ_MAX_BYTES:,} bytes and run with maximum_bytes_billed at that cap; rows append to "
              "google_search_signals as search interest, never posts; the reads below show the day before, a live "
              "run reads the newest partition")
        for kind in google_trends.BQ_TABLES:
            print("  " + " ".join(google_trends.bq_read_sql(kind, day - timedelta(days=1)).split()))
    else:
        print("Google Trends tables: parked (core/config/google_sources.yaml), no read, no bytes billed")
    print(f"Google daily trends feed: {', '.join(google_rss.MARKETS)}, one anonymous HTTPS read a market before the "
          "SocialCrawl phases after one robots.txt read, 0 SocialCrawl credits; rows append to google_search_signals "
          f"as source {google_rss.SOURCE}")
    for market in google_rss.MARKETS:
        print(f"  {google_rss.URL.format(market)}")
    print(f"Google search terms as row 14 queries: at most {google_trends.GOOGLE_SEEDS_PER_MARKET} a market from the "
          "feed and the day before's google_search_signals rows (trending, feed, public tables' rising), taking "
          "existing row 14 slots (the placeholders above); query triage only, never evidence")
    from core.public_feeds.catalog import confirmed_feeds

    feeds = confirmed_feeds()
    print(f"public feeds: {len(feeds)} confirmed reads, 0 SocialCrawl credits")
    for feed in feeds:
        print(f"  {feed.market} {feed.name} {feed.url}")
    from core.public_feeds import telegram as telegram_feed

    if telegram_feed.TELEGRAM_READY:
        channels = telegram_feed.CHANNELS[:telegram_feed.MAX_CHANNELS_PER_RUN]
        print(f"Telegram public channels: {len(channels)} web preview reads, 0 SocialCrawl credits, "
              f"{telegram_feed.POLITE_DELAY_SECONDS:g} s apart, newest {telegram_feed.MAX_POSTS_PER_CHANNEL} posts each")
        for channel in channels:
            print(f"  {channel.market} {channel.name} {channel.url}")


def _failed_counts(collected, started):
    """The counts of a run that failed: the finished collection's, else those of the partial one collect() had built
    when it raised, so the spend and calls made before the failure are not lost. {} when neither can be counted."""
    partial = collected or (started[0] if started else None)
    try:
        return partial.counts() if partial is not None else {}
    except Exception:
        log.exception("the counts of a failed collect run could not be built")
        return {}


def main(argv=None, *, env=None, runs=None, jobs=None, bq=None, make_client=None, fns=None, clock=None, get=None,
         public_feed_transport=None):
    env = os.environ if env is None else env
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--plan", action="store_true", help="print the planned calls and holds; no network")
    args.add_argument("--run-date", help="YYYY-MM-DD; default RUN_DATE or today in SAST")
    args.add_argument("--backfill-creators", action="store_true",
                      help="build creators and fill NULL post geo from stored raw_responses; needs --since")
    args.add_argument("--since", help="YYYY-MM-DD, the first UTC fetch day the backfill reads")
    args.add_argument("--until", help="YYYY-MM-DD, the last UTC fetch day the backfill reads; default today (UTC)")
    opts = args.parse_args(argv)
    if opts.backfill_creators:
        if not opts.since:
            print("refused: --backfill-creators needs --since YYYY-MM-DD", file=sys.stderr)
            return 2
        since = date.fromisoformat(opts.since)
        until = date.fromisoformat(opts.until) if opts.until else datetime.now(timezone.utc).date()
        if opts.plan:
            print_backfill_plan(since, until)
            return 0
        if bq is None:
            from google.cloud import bigquery

            bq = bigquery.Client(project=chain.PROJECT)
        print(json.dumps(backfill(bq, since, until, (fns or detect_fns())[1])))
        return 0
    run_date = date.fromisoformat(opts.run_date) if opts.run_date else chain.today()
    x_trends = x_trends_on(env)
    try:
        caps = collect_caps(env)
        only = only_routes(env)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    share_cap = caps["ENGINE_DAILY"]["collect"] if env.get("COLLECT_CAP_OVERRIDE") not in (None, "") else None
    if opts.plan:
        print_plan(run_date, x_trends=x_trends, share_cap=share_cap, only_routes=only)
        return 0
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    days = {m: _local_day(now, m) for m in (GLOBAL,) + MARKETS}
    in_window = START_WINDOW[0] <= now.astimezone(chain.SAST).time() <= START_WINDOW[1]
    if set(days.values()) != {run_date.isoformat()} or not in_window:
        print(f"refused: a live collect runs only while SAST, WAT and EAT are all on {run_date.isoformat()} "
              f"and it is 01:30 to 22:30 SAST; now {json.dumps(days)}. Use --plan for other dates", file=sys.stderr)
        return 2
    if bq is None:
        from google.cloud import bigquery

        bq = bigquery.Client(project=chain.PROJECT)
    runs = runs or chain.BigQueryRunsStore(bq)
    jobs = jobs or chain.CloudRunJobs()
    make_client = make_client or (lambda run_id, c: live_client(run_id, c, bq, clock))
    if public_feed_transport is None:
        from core.collect.public_feed_collect import https_transport

        public_feed_transport = https_transport
    base, keep = None, frozenset()
    if only is not None:
        base = writers.repair_base(bq, run_date)
        if base is None:
            print(f"refused: {ONLY_ROUTES} repairs the day's good collect run, and {run_date.isoformat()} has no ok "
                  "collect run", file=sys.stderr)
            return 2
        keep = frozenset((h["market"], h["series"], h["protocol"]) for h in base["health"] if h["valid"])
    try:
        run = chain.begin("collect", run_date, runs=runs, jobs=jobs)
    except chain.AlreadyDone as exc:
        print(f"nothing to collect: {exc}")
        return restart_next(run_date, runs, jobs)
    collected, started = None, []
    try:
        item_id_fn, geo_fn = fns or detect_fns()
        last = writers.last_pulls(bq, run_date)
        queue, watches = [], []
        if only is None:
            queue = seeding.read(bq, run_date, MARKETS)
            queue.extend(seeding.research_rows(research_terms.load_terms(), run_date))
            watches = writers.read_watches(bq, MARKETS)
        collected = collect(make_client(run.run_id, caps), run_date, run.run_id, item_id_fn=item_id_fn,
                            geo_fn=geo_fn, clock=clock, x_trends=x_trends, share_cap=share_cap, last_pulls=last,
                            seeds=queue, local_get=get or local_sources.http_get, watches=watches,
                            known_posts=lambda ids: writers.known_posts(bq, ids),
                            public_feed_transport=public_feed_transport, search_signals=True,
                            only_routes=only, keep=keep, google_rss_transport=public_feed_transport,
                            google_terms=lambda: google_trends.read_terms(bq, run_date, MARKETS),
                            country_profiles=lambda keys, **kw: writers.read_profile_countries(bq, keys, **kw),
                            on_run=started.append)
        if only is None:
            bq_trends_phase(collected, bq, run_date, clock=clock)
        written = writers.write_run(bq, collected, run.run_id, carry=base["health"] if base else None)
        if only is None:
            writers.append(bq, "seed_queue", collected.seed_rows + collected.news_seeds)
    except Exception as exc:
        if isinstance(exc, writers.PublicFeedWriteError):
            log.error("collect %s failed (%s)", run.run_id, exc.category)
            counts = _failed_counts(collected, started)
            counts["public_feed_write_error"] = exc.category
            chain.finish(run, "failed", counts, exc.category, runs=runs)
            return 1
        log.exception("collect %s failed", run.run_id)
        chain.finish(run, "failed", _failed_counts(collected, started), f"{type(exc).__name__}: {exc}"[:1000],
                     runs=runs)
        return 1
    counts = {**collected.counts(),
              **{k: written[k] for k in ("cultural_map", "labels_blocked", "labels_missing", "creators_written",
                                         "creators_error", "public_feed_posts", "public_feed_raw_rows",
                                         "public_feed_raw_error", "public_feed_merge_statements", "google_search_signals",
                                         "google_search_signals_error", "google_search_signals_blocked")},
              **{k: written[k] for k in ("telegram_raw_rows_written", "telegram_raw_error") if k in written}}
    if collected.public_feed_error:
        counts["public_feed_error"] = "phase_failed"
        chain.finish(run, "failed", counts, "public_feed_phase_failed", runs=runs)
        return 1
    if written["public_feed_raw_error"]:
        counts["public_feed_raw_error"] = "append_failed"
        chain.finish(run, "failed", counts, "public_feed_raw_append_failed", runs=runs)
        return 1
    if collected.balance_unread:
        chain.finish(run, "failed", counts, "balance_unreadable: the SocialCrawl balance could not be read, "
                     "so no paid call was made", runs=runs)
        return 1
    if base is not None:
        counts = repair_counts(base, counts, written, only, keep)
    chain.finish(run, "ok", counts, runs=runs)
    print(json.dumps({"run_id": run.run_id, **counts}))
    try:
        chain.start_next("collect", run_date, jobs=jobs)
    except Exception:
        log.exception("collect %s is ok but the next job did not start; a retry starts it", run.run_id)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
