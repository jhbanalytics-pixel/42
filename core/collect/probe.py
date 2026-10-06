"""The Stage 0 SocialCrawl probe (BUILD.md task 0.5), run once as the Cloud Run job f42-probe.

It makes the probes in docs/full-42/research/13-socialcrawl-full-map.md section 5, in that order, plus
probe 14 (web/scrape of the public per-country X trends archive), with the free account calls first and
credits/transactions last. Every call goes through the SocialCrawl client on the build share, before the
morning schedule starts. Each probe runs once and the probe never retries it (the client alone retries
a refunded 502 or 503, once); the run stops on
insufficient_credits, balance_floor or cap_reached. A probe whose ids come from an earlier response is
skipped with a reason when that response has none. One log line per probe: id, route, status, credits
charged, item count. Bodies and the key are never printed.

    py -3.13 -m core.collect.probe --plan   prints the plan with each call's hold and the total, no network
    python -m core.collect.probe            the job itself; core/collect/probe_report.py reads its rows

--bodies is a second mode (lane L3's Needs item 13, and L1's own parser checks): one live body for each
comment, transcript and parser route in BODY_ROUTES whose route is priced. Each call's input is a real
post from ZA, NG or KE in the last BODY_DAYS days of intelligence_42_core.posts, picked by one
parameterised read per route; a hashtag or handle that rule 1 or the generic tag list refuses is never
sent. The calls go through the client on the build share after the morning schedule (BUILD_DAILY 150)
with no lane, and the mode stops before any call whose hold would take this run's charges past
BODIES_CAP. The bodies land in raw_responses like every other call; probe_report.py --bodies redacts them
into a test fixture. A failure ends the run with exit 0, so the job's one retry cannot spend twice.

    py -3.13 -m core.collect.probe --bodies --plan   the routes, their holds and the total, no network
    python -m core.collect.probe --bodies            inside the f42-probe job only

--routes names the routes to call instead, from BODY_ROUTES and PRICE_ROUTES, on the same capped path: one
body each, to read the vendor's charge against PRICED. PRICE_ROUTES are the routes priced for Ask: one post
opened per platform (L3 Needs 5) and prism/mentions and instagram/tagged (L4 Needs 25). Those two take an
Instagram handle from the culture desk in core/config/hubs.yaml, public pages only, never a person's post.

    py -3.13 -m core.collect.probe --bodies --routes tiktok/post,prism/mentions --plan

--ig-locations searches instagram/search/location for three cities a market (IG_CITIES), every query
past rule 1, on the build share after the morning schedule, stopping before any call that would take the
run past IG_CAP (45). It prints each market's candidates inside that market's MARKET_BOXES box: the id
derive() would pass as location_id, the name, lat and lng. --ig-locations-report reads a run's
raw_responses locally and prints the core/config/markets.yaml instagram_locations blocks to paste.

    py -3.13 -m core.collect.probe --ig-locations --plan             the searches and the total hold
    python -m core.collect.probe --ig-locations                      inside the f42-probe job only
    py -3.13 -m core.collect.probe --ig-locations-report <run_id>    local, read only
"""

import argparse
import json
import re
from datetime import datetime, timedelta, timezone

import yaml

from core.collect.chain import PROJECT, SAST
from core.collect.socialcrawl_client import PRICED, ROOT, list_price, quote_for, split_vendor_labels
from core.collect.stores import DATASET

STOP = ("insufficient_credits", "balance_floor", "cap_reached")
SUCCESS_STATUSES = ("ok", "empty", "cached")
TREND_PROBES = ("1-za", "1-ng", "1-ke")
MULTI_PLATFORMS = "instagram,youtube,reddit,twitter,threads,facebook"  # research section 4, row 16
# Johannesburg on Facebook: explore URL format and city id are unverified until the probe answers.
JOBURG_FB_ID = "108151539218136"
TRENDS_ARCHIVE = "https://trends24.in/{}/"


def _p(n, pid, route, params, purpose, market=None, **extra):
    return {"n": n, "id": pid, "route": route, "method": PRICED[route].method, "params": params,
            "market": market, "purpose": purpose, **extra}


def plan(today):
    """Every probe call in order. today is the SAST run date."""
    week_ago = (today - timedelta(days=7)).isoformat()
    seen = f"probe-ke-{today.isoformat()}"
    repeat = {"query": "gengetone", "country": "KE", "publish_time": "this-week", "seen": seen}
    return [
        _p(0, "0a", "credits/balance", {}, "balance before the run"),
        _p(0, "0b", "status", {}, "platform circuit state"),
        _p(0, "0c", "utility/capabilities", {}, "parameters that work across endpoints"),
        _p(1, "1-za", "tiktok/trending", {"region": "ZA", "feed": "local"}, "in-market share of ext.region", "ZA"),
        _p(1, "1-ng", "tiktok/trending", {"region": "NG", "feed": "local"}, "in-market share; NG vs KE", "NG"),
        _p(1, "1-ke", "tiktok/trending", {"region": "KE", "feed": "local"}, "in-market share; NG vs KE", "KE"),
        _p(2, "2-ng", "youtube/videos/trending", {"region": "NG", "category": "24"}, "non-music category", "NG"),
        _p(2, "2-za", "youtube/videos/trending", {"region": "ZA"}, "no category", "ZA"),
        _p(2, "2-ke", "youtube/videos/trending", {"region": "KE", "category": "17"}, "non-music category", "KE"),
        _p(3, "3", "tiktok/hashtags/popular", {"countryCode": "ZA", "period": "7"}, "overall 7-day board", "ZA"),
        _p(4, "4-ng", "apple_music/charts", {"country": "ng", "type": "songs"}, "chart exists (400 is free)", "NG"),
        _p(4, "4-ke", "apple_music/charts", {"country": "ke", "type": "songs"}, "chart exists (400 is free)", "KE"),
        _p(5, "5", "search/news", {"query": "afrobeats", "countries": "ZA,NG,KE", "max_legs": 3},
           "which editions answer"),
        _p(6, "6a", "twitter/search/tweets", {"query": "geocode:-26.20,28.04,50km"}, "geocode operator", "ZA"),
        _p(6, "6b", "twitter/search/tweets", {"query": "near:Lagos"}, "near operator", "NG"),
        _p(7, "7", "youtube/search/advanced",
           {"query": "lagos", "location": "6.52,3.37", "location_radius": "50km"}, "radius search", "NG"),
        _p(8, "8a", "instagram/search/location", {"query": "Soweto"}, "location lookup", "ZA"),
        _p(8, "8b", "instagram/location/posts", {}, "local posts at the first Soweto location", "ZA",
           derive={"location_id": "8a"}),
        _p(9, "9a", "facebook/events",
           {"url": f"https://www.facebook.com/events/explore/johannesburg-south-africa/{JOBURG_FB_ID}/",
            "time": "this_week"}, "city events (explore URL unverified)", "ZA"),
        _p(9, "9b", "facebook/search/posts", {"query": "johannesburg", "location_uid": JOBURG_FB_ID},
           "location_uid filter (id unverified)", "ZA"),
        _p(10, "10a", "search/multi",
           {"query": "amapiano", "platforms": MULTI_PLATFORMS, "since": week_ago, "dry_run": 1}, "cost preview"),
        _p(10, "10b", "search/multi", {"query": "amapiano", "platforms": MULTI_PLATFORMS, "since": week_ago},
           "one live multi-platform search"),
        _p(11, "11a", "tiktok/search/top", dict(repeat), "seen id, first call", "KE", use_cache=True),
        _p(11, "11b", "tiktok/search/top", dict(repeat), "same seen id again: should cost about 0", "KE",
           use_cache=False),
        _p(12, "12a", "tiktok/song/videos", {"use": 1}, "adoption curve of a trending sound",
           derive={"clipId": TREND_PROBES}),
        _p(12, "12b", "tiktok/hashtag", {"hashtag": "amapiano"}, "running hashtag counts"),
        _p(13, "13", "prism/post-stats", {}, "re-read 3 trending posts", derive={"urls": TREND_PROBES}),
        _p(14, "14-za", "web/scrape", {"url": TRENDS_ARCHIVE.format("south-africa")}, "X trends seed", "ZA"),
        _p(14, "14-ng", "web/scrape", {"url": TRENDS_ARCHIVE.format("nigeria")}, "X trends seed", "NG"),
        _p(14, "14-ke", "web/scrape", {"url": TRENDS_ARCHIVE.format("kenya")}, "X trends seed", "KE"),
        _p(0, "15", "credits/transactions", {"limit": 50}, "vendor charge per call, for the report"),
    ]


def plan_params(p):
    """The probe's params with a placeholder for each id that comes from an earlier response."""
    params = dict(p["params"])
    for name, source in p.get("derive", {}).items():
        where = ",".join(source) if isinstance(source, tuple) else source
        params[name] = [f"https://www.tiktok.com/<from {where}>"] * 3 if name == "urls" else f"<from {where}>"
    return params


def hold(p):
    return quote_for(p["route"], p["method"], plan_params(p))


# Where ids sit in a response row. Tried in order; the first present value wins.
PATHS = {
    "location_id": (("location_id",), ("pk",), ("id",), ("location", "pk"), ("location", "id")),
    "clipId": (("post", "music", "id"), ("post", "music", "clip_id"), ("post", "music_id"), ("music", "id")),
    "urls": (("post", "url"), ("url",)),
}


def _dig(row, path):
    for key in path:
        if not isinstance(row, dict) or row.get(key) in (None, ""):
            return None
        row = row[key]
    return row


def _found(rows, name):
    out = []
    for row in rows:
        value = next((v for v in (_dig(row, path) for path in PATHS[name]) if v is not None), None)
        if value is not None and value not in out:
            out.append(str(value))
    return out


def derive(p, results):
    """(params, reason): params with the derived ids filled in, or None and why the probe is skipped."""
    params = dict(p["params"])
    for name, source in p.get("derive", {}).items():
        sources = source if isinstance(source, tuple) else (source,)
        rows = [row for s in sources if s in results for row in results[s].items]
        values = _found(rows, name)
        if not values:
            return None, f"no {name} in {','.join(sources)}"
        params[name] = values[:3] if name == "urls" else values[0]
    return params, ""


def live_client(run_id, schedule_started=False):
    from google.cloud import bigquery

    from core.collect.socialcrawl_client import SocialCrawlClient, requests_http
    from core.collect.stores import BigQueryLedgerStore, BigQueryRawStore

    bq = bigquery.Client(project=PROJECT)
    return SocialCrawlClient(
        share="build", run_id=run_id, mode="live", ledger=BigQueryLedgerStore(bq, PROJECT),
        raw=BigQueryRawStore(bq, PROJECT), http=requests_http, clock=lambda: datetime.now(timezone.utc),
        schedule_started=schedule_started,
    )


# --bodies ------------------------------------------------------------------------------------------------

BODIES_CAP = 70
BODY_MARKETS = ("ZA", "NG", "KE")
BODY_DAYS = 7
# (id, group, route, platform, what the call needs from a post). Comments and transcripts are L3's
# Needs item 13; parsers are the three routes L1's parser reads from spec shapes only.
BODY_ROUTES = (
    ("c1", "comments", "tiktok/post/comments", "tiktok", "url"),
    ("c2", "comments", "instagram/post/comments", "instagram", "url"),
    ("c3", "comments", "youtube/video/comments", "youtube", "url"),
    ("c4", "comments", "reddit/post/comments", "reddit", "url"),
    ("c5", "comments", "twitter/tweet/replies", "twitter", "url"),
    ("t1", "transcripts", "youtube/video/transcript", "youtube", "video"),
    ("t2", "transcripts", "tiktok/post/transcript", "tiktok", "video"),
    ("t3", "transcripts", "instagram/media/transcript", "instagram", "video"),
    ("t4", "transcripts", "twitter/tweet/transcript", "twitter", "video"),
    ("t5", "transcripts", "reddit/post/transcript", "reddit", "video"),
    ("p1", "parsers", "tiktok/search/hashtag", "tiktok", "hashtag"),
    ("p2", "parsers", "tiktok/profile/videos", "tiktok", "handle"),
    ("p3", "parsers", "instagram/audio/reels", "instagram", "sound"),
)
GROUPS = ("comments", "transcripts", "parsers")
# Called only when --routes names them: the routes priced for Ask, to confirm each price live.
PRICE_ROUTES = (
    ("d1", "details", "tiktok/post", "tiktok", "url"),
    ("d2", "details", "instagram/post", "instagram", "url"),
    ("d3", "details", "youtube/video", "youtube", "url"),
    ("d4", "details", "twitter/tweet", "twitter", "url"),
    ("d5", "details", "reddit/post", "reddit", "url"),
    ("d6", "details", "facebook/post", "facebook", "url"),
    ("d7", "details", "threads/post", "threads", "url"),
    ("a1", "ask", "instagram/tagged", "instagram", "hub_handle"),
    ("a2", "ask", "prism/mentions", "instagram", "hub_handle"),
)
PRICE_GROUPS = ("details", "ask")
BODY_ROUTE_NAMES = tuple(r[2] for r in BODY_ROUTES + PRICE_ROUTES)
# prism/mentions searches X and Reddit pages and the Instagram tag page, never the web lane (rule 2).
ROUTE_PARAMS = {"prism/mentions": {"platforms": "twitter,reddit,instagram"}}
HUBS = ROOT / "core" / "config" / "hubs.yaml"
PLATFORMS = {"twitter": ("twitter", "x")}
# A post is a video when posts.duration_s is above 0 or its url has the platform's video form.
VIDEO_URLS = {
    "youtube": r"youtube\.com/(watch|shorts/)|youtu\.be/",
    "tiktok": r"tiktok\.com/@[^/]+/video/",
    "instagram": r"instagram\.com/(reels?|tv)/",
    "twitter": r"/video/",
    "reddit": r"v\.redd\.it/",
}
PLACEHOLDERS = {
    "url": lambda pf: {"url": f"<{pf} post url from posts>"},
    "video": lambda pf: {"url": f"<{pf} video url from posts>"},
    "hashtag": lambda pf: {"hashtag": "<hashtag from posts>", "region": "<its market>"},
    "handle": lambda pf: {"handle": "<creator handle from a posts url>"},
    "sound": lambda pf: {"audio_id": "<posts.sound_id>"},
    "hub_handle": lambda pf: {"handle": f"<{pf} handle from the hubs.yaml culture desk>"},
}
POSTS_SQL = f"""SELECT url, platform, geo_market, hashtags, sound_id
FROM `{PROJECT}.{DATASET}.posts`
WHERE post_date BETWEEN @since AND @today
  AND platform IN UNNEST(@platforms) AND geo_market IN UNNEST(@markets) AND url IS NOT NULL
  AND (NOT @video OR IFNULL(duration_s, 0) > 0 OR REGEXP_CONTAINS(url, @video_url))
  AND (NOT @sound OR sound_id IS NOT NULL)
  AND (NOT @hashtag OR ARRAY_LENGTH(hashtags) > 0)
ORDER BY IFNULL(comments, 0) DESC, published_at DESC, post_id
LIMIT 20"""
TIKTOK_HANDLE = re.compile(r"tiktok\.com/@([A-Za-z0-9_.]+)")


def bodies_plan(routes=None):
    """(calls, dropped): one call per BODY_ROUTES row whose route is priced, and the routes that are not.
    routes, when given, picks rows from BODY_ROUTES and PRICE_ROUTES by route name, in table order."""
    rows = BODY_ROUTES if routes is None else [r for r in BODY_ROUTES + PRICE_ROUTES if r[2] in routes]
    calls = [{"id": pid, "group": group, "route": route, "method": PRICED[route].method, "platform": platform,
              "need": need, "params": {**PLACEHOLDERS[need](platform), **ROUTE_PARAMS.get(route, {})},
              "market": None}
             for pid, group, route, platform, need in rows if route in PRICED]
    return calls, [route for _, _, route, _, _ in rows if route not in PRICED]


def hub_rows():
    """The Instagram culture desk handles in hubs.yaml for ZA, NG and KE, in file order."""
    markets = yaml.safe_load(HUBS.read_text(encoding="utf-8"))["markets"]
    return [{"handle": str(e["handle"]), "geo_market": m.upper()}
            for m, hub in markets.items() if m.upper() in BODY_MARKETS
            for e in (hub.get("culture_desk") or []) if e.get("platform") == "instagram"]


def candidates(p, today, query):
    """Up to 20 recent posts that fit call p, from one read of posts parameterised by platform and need;
    for a hub_handle call, the culture desk handles instead."""
    if p["need"] == "hub_handle":
        return hub_rows()
    return query(POSTS_SQL, {
        "since": today - timedelta(days=BODY_DAYS), "today": today,
        "platforms": list(PLATFORMS.get(p["platform"], (p["platform"],))), "markets": list(BODY_MARKETS),
        "video": p["need"] == "video", "video_url": VIDEO_URLS.get(p["platform"], ""),
        "sound": p["need"] == "sound", "hashtag": p["need"] == "hashtag",
    })


def pick(p, rows):
    """(params, market, "") from the first post that fits call p, or (None, None, why not). Rule 1 and the
    generic tag list hold on every hashtag, handle and url that would be sent."""
    from core.collect.gdelt import blocked
    from core.collect.job import _tag

    if not rows:
        return None, None, f"no {p['platform']} post in {','.join(BODY_MARKETS)} in the last {BODY_DAYS} days"
    for row in rows:
        url, market = str(row.get("url") or ""), row.get("geo_market")
        if p["need"] == "hub_handle":
            handle = str(row.get("handle") or "")
            if handle and not blocked(handle):
                return {"handle": handle, **ROUTE_PARAMS.get(p["route"], {})}, market, ""
            continue
        if not url or blocked(url):
            continue
        if p["need"] in ("url", "video"):
            return {"url": url}, market, ""
        if p["need"] == "hashtag":
            tag = next((t for t in map(_tag, row.get("hashtags") or []) if t), None)
            if tag:
                return {"hashtag": tag, "region": market}, market, ""
        elif p["need"] == "handle":
            found = TIKTOK_HANDLE.search(url)
            if found and not blocked(found.group(1)):
                return {"handle": found.group(1)}, market, ""
        elif row.get("sound_id") and not blocked(str(row["sound_id"])):
            return {"audio_id": str(row["sound_id"])}, market, ""
    return None, None, f"no {p['platform']} post passed rule 1"


def posts_query():
    """A function (sql, params) -> rows that runs a parameterised read of posts in BigQuery."""
    from datetime import date

    from google.cloud import bigquery

    bq = bigquery.Client(project=PROJECT)
    kinds = {bool: "BOOL", date: "DATE", str: "STRING"}

    def query(sql, params):
        qp = [bigquery.ArrayQueryParameter(k, "STRING", v) if isinstance(v, list)
              else bigquery.ScalarQueryParameter(k, kinds[type(v)], v) for k, v in params.items()]
        job = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=qp))
        return [dict(r) for r in job.result()]
    return query


def print_bodies_plan(calls, dropped):
    print(f"{'id':3} {'group':11} {'route':28} {'hold':>4}  params")
    for p in calls:
        print(f"{p['id']:3} {p['group']:11} {p['route']:28} {hold(p):>4}  "
              f"{json.dumps(p['params'], separators=(',', ':'))}")
    for group in GROUPS + PRICE_GROUPS:
        if any(p["group"] == group for p in calls):
            print(f"{group}: {sum(hold(p) for p in calls if p['group'] == group)} credits")
    if dropped:
        print("dropped, not priced: " + ", ".join(dropped))
    print(f"{len(calls)} calls, total hold {sum(hold(p) for p in calls)} credits, cap {BODIES_CAP}")


def run_bodies(client, calls, today, query, run_id):
    spent, made = 0, 0
    try:
        for p in calls:
            params, market, why = pick(p, candidates(p, today, query))
            if params is None:
                print(f"{p['id']} {p['route']} skipped: {why}")
                continue
            held = quote_for(p["route"], p["method"], params)
            if spent + held > BODIES_CAP:
                print(f"stopped before {p['id']}: {spent:g} charged plus {held} held is over the cap of {BODIES_CAP}")
                break
            result = client.call(p["route"], params, method=p["method"], market=market, seed_key=p["id"], lane=None)
            made += 1
            spent += result.credits_charged
            print(f"{p['id']} {p['route']} {market} {result.status} held={held} charged={result.credits_charged:g} "
                  f"items={len(result.items)}")
            if result.status not in SUCCESS_STATUSES:
                print(f"stopped after {p['id']}: {result.status}")
                break
    except Exception as exc:
        print(f"stopped: {type(exc).__name__}")
    print(f"done: {made} calls, {spent:g} credits charged for run_id {run_id}")
    return 0


# --ig-locations -------------------------------------------------------------------------------------------

# Three city searches a market, 5 credits each on the build share: 45 credits at most.
IG_ROUTE = "instagram/search/location"
IG_CITIES = {"ZA": ("Johannesburg", "Cape Town", "Durban"), "NG": ("Lagos", "Abuja", "Port Harcourt"),
             "KE": ("Nairobi", "Mombasa", "Kisumu")}
IG_CAP = 45
LONG_DASHES = "[" + chr(0x2013) + chr(0x2014) + "]|-{2,}"
IG_KEEP = 5  # ids a market's markets.yaml block takes; collect rotates one a day
# (south, north, west, east): each country's mainland lat/lng extent, rounded outward. A box is not a
# border, so a place just over one can pass; the printed name and coordinates are read before pasting.
MARKET_BOXES = {"ZA": (-34.9, -22.1, 16.4, 32.95), "NG": (4.2, 13.9, 2.6, 14.7), "KE": (-4.75, 5.5, 33.9, 41.95)}


def ig_plan():
    """(calls, dropped): one instagram/search/location call per IG_CITIES query that rule 1 lets through,
    and the (market, query) pairs it blocks."""
    from core.collect.gdelt import blocked

    calls, dropped = [], []
    for market, cities in IG_CITIES.items():
        for i, city in enumerate(cities):
            if blocked(city):
                dropped.append((market, city))
                continue
            calls.append({"id": f"ig-{market.lower()}-{i}", "route": IG_ROUTE, "method": PRICED[IG_ROUTE].method,
                          "params": {"query": city}, "market": market})
    return calls, dropped


def in_market(market, lat, lng):
    south, north, west, east = MARKET_BOXES[market]
    return south <= lat <= north and west <= lng <= east


def _coordinate(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(name):
    """A location name fit for a one-line YAML comment: no line breaks, no long dashes."""
    return re.sub(r"\s+", " ", re.sub(LONG_DASHES, " ", str(name or ""))).strip()


def ig_candidates(market, rows):
    """Search results inside the market's box, in vendor order, once each: the id is the one derive()
    passed as location_id (the first of PATHS location_id, location.pk in the live shape)."""
    from core.collect.gdelt import blocked

    out, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        loc = row["location"] if isinstance(row.get("location"), dict) else row
        found = next((v for v in (_dig(row, path) for path in PATHS["location_id"]) if v is not None), None)
        lat, lng = _coordinate(loc.get("lat")), _coordinate(loc.get("lng"))
        name = _clean(loc.get("name") or row.get("title"))
        if found is None or lat is None or lng is None or not in_market(market, lat, lng):
            continue
        if not name or blocked(name) or str(found) in seen:
            continue
        seen.add(str(found))
        out.append({"id": str(found), "name": name, "lat": lat, "lng": lng})
    return out


def _where(c):
    return f"{round(c['lat'], 6)} {round(c['lng'], 6)}"


def print_ig_plan(calls, dropped):
    for p in calls:
        print(f"{p['id']:8} {p['market']:3} {p['route']:28} {hold(p):>4}  {p['params']['query']}")
    for market, city in dropped:
        print(f"dropped by rule 1: {market} {city}")
    print(f"{len(calls)} calls, total hold {sum(hold(p) for p in calls)} credits, cap {IG_CAP}")


def run_ig_locations(client, calls, run_id):
    spent, made, found = 0, 0, {m: [] for m in IG_CITIES}
    try:
        for p in calls:
            held = hold(p)
            if spent + held > IG_CAP:
                print(f"stopped before {p['id']}: {spent:g} charged plus {held} held is over the cap of {IG_CAP}")
                break
            result = client.call(p["route"], p["params"], method=p["method"], market=p["market"], seed_key=p["id"],
                                 lane=None)
            made += 1
            spent += result.credits_charged
            print(f"{p['id']} {p['route']} {p['market']} {result.status} held={held} "
                  f"charged={result.credits_charged:g} items={len(result.items)}")
            found[p["market"]] += ig_candidates(p["market"], result.items)
            if result.status not in SUCCESS_STATUSES:
                print(f"stopped after {p['id']}: {result.status}")
                break
    except Exception as exc:
        print(f"stopped: {type(exc).__name__}")
    for market, rows in found.items():
        for c in rows:
            print(f"{market} {c['id']} {c['name']} {_where(c)}")
    print(f"done: {made} calls, {spent:g} credits charged for run_id {run_id}")
    print(f"paste block: py -3.13 -m core.collect.probe --ig-locations-report {run_id}")
    return 0


def ig_report(run_id, raw):
    """Prints each market's candidates from the run's instagram/search/location answers, then a
    markets.yaml instagram_locations block per market with any, the first IG_KEEP ids in run order."""
    found = {m: [] for m in IG_CITIES}
    for r in raw:
        body = r.get("body")
        if r.get("route") != IG_ROUTE or r.get("http_status") != 200 or r.get("market") not in found or not body:
            continue
        known = {c["id"] for c in found[r["market"]]}
        _, items, _ = split_vendor_labels(body)
        found[r["market"]] += [c for c in ig_candidates(r["market"], items) if c["id"] not in known]
    for market, rows in found.items():
        print(f"{market}: {len(rows)} candidates, the first {min(len(rows), IG_KEEP)} go in the block")
        for c in rows:
            print(f"  {c['id']} {c['name']} {_where(c)}")
    for market, rows in found.items():
        if not rows:
            print(f"# {market}: no candidates in run {run_id}")
            continue
        print(f"# markets.{market.lower()}")
        print("    instagram_locations:")
        print(f"      source: {IG_ROUTE} in probe run {run_id}")
        print("      values:")
        for c in rows[:IG_KEEP]:
            print(f"        - \"{c['id']}\"  # {c['name']}, {round(c['lat'], 6)}, {round(c['lng'], 6)}")
    return 0


def print_plan(probes):
    print(f"{'id':6} {'method':6} {'route':28} {'market':6} {'list':>4} {'hold':>4}  params  purpose")
    for p in probes:
        print(f"{p['id']:6} {p['method']:6} {p['route']:28} {p['market'] or '':6} "
              f"{list_price(p['route'], p['method']):>4} {hold(p):>4}  "
              f"{json.dumps(plan_params(p), separators=(',', ':'))}  {p['purpose']}")
    print(f"{len(probes)} calls, total hold {sum(hold(p) for p in probes)} credits")


def main(argv=None, *, client=None, clock=None, today=None, query=None, read=None):
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--plan", action="store_true", help="print the plan and the holds; no network")
    args.add_argument("--bodies", action="store_true", help="one body per comment, transcript and parser route")
    args.add_argument("--routes", help="comma-separated routes from BODY_ROUTES and PRICE_ROUTES: one body each")
    args.add_argument("--ig-locations", action="store_true", help="Instagram location searches for IG_CITIES")
    args.add_argument("--ig-locations-report", metavar="RUN_ID", help="print markets.yaml blocks from a run; local")
    opts = args.parse_args(argv)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    today = today or now.astimezone(SAST).date()
    if opts.ig_locations_report:
        if read is None:
            from core.collect.probe_report import read_run

            def read(run_id):
                return read_run(run_id)[1]
        return ig_report(opts.ig_locations_report, read(opts.ig_locations_report))
    if opts.ig_locations:
        calls, dropped = ig_plan()
        if opts.plan:
            print_ig_plan(calls, dropped)
            return 0
        run_id = "probe-ig-" + now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        client = client or live_client(run_id, schedule_started=True)
        print(f"run_id {run_id}")
        for market, city in dropped:
            print(f"dropped by rule 1: {market} {city}")
        return run_ig_locations(client, calls, run_id)
    if opts.bodies or opts.routes:
        routes = None
        if opts.routes:
            routes = [r.strip() for r in opts.routes.split(",") if r.strip()]
            unknown = [r for r in routes if r not in BODY_ROUTE_NAMES]
            if unknown:
                print("not a body route: " + ", ".join(unknown))
                return 2
        calls, dropped = bodies_plan(routes)
        if opts.plan:
            print_bodies_plan(calls, dropped)
            return 0
        run_id = "probe-bodies-" + now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        client = client or live_client(run_id, schedule_started=True)
        print(f"run_id {run_id}")
        if dropped:
            print("dropped, not priced: " + ", ".join(dropped))
        return run_bodies(client, calls, today, query or posts_query(), run_id)

    probes = plan(today)
    if opts.plan:
        print_plan(probes)
        return 0

    run_id = "probe-" + now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = client or live_client(run_id)
    print(f"run_id {run_id}")
    results, calls = {}, 0
    for p in probes:
        params, why = derive(p, results)
        if params is None:
            print(f"{p['id']} {p['route']} skipped: {why}")
            continue
        result = client.call(p["route"], params, method=p["method"], market=p["market"], seed_key=p["id"],
                             lane=None, use_cache=p.get("use_cache", True))
        calls += 1
        results[p["id"]] = result
        print(f"{p['id']} {p['route']} {result.status} charged={result.credits_charged:g} "
              f"items={len(result.items)}")
        if result.status in STOP:
            print(f"stopped after {p['id']}: {result.status}")
            break
    print(f"done: {calls} calls for run_id {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
