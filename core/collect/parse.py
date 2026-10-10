"""Stored SocialCrawl response bodies to rows for posts, post_observations and item_counter_daily.

Pure functions: no network, no SQL, no clock, except ingest (below). The collect job reads a
raw_responses row and calls

    parse(route, params, market, body, fetched_at, run_id, *, item_id_fn, geo_fn,
          lane=None, seed_key=None, pull_seq=None, protocol=None)

which returns {"posts": [...], "observations": [...], "counters": [...]}, each row a dict holding
exactly the DDL columns of DATA.md section 1 in DDL order. Timestamps are ISO 8601 strings in UTC,
dates are YYYY-MM-DD strings, JSON columns are dicts, so every row is ready for a JSON load.

parse_with_creators takes the same arguments and adds "creators": one row per post that names a creator,
CREATOR_COLUMNS in order, keyed (platform, creator_id) with creator_id exactly the post's, from the post's author
block (a panel row's profile fills what it lacks, and the call's handle= or username= parameter gives the handle
when the body has none). first_seen and last_seen are the fetch time.

Geo inputs: TikTok's ext.region; the author's (else the panel profile's) region or country, else ext.author_country
(the country the creator declares on their own Instagram profile, which instagram/search/reels fills with
include=creator), and location; the post text with YouTube's ext.description added; the language from
computed.language, else the post's own, else ext.content_language or ext.defaultAudioLanguage. A call's region=
parameter is never a market: a country's trending list holds foreign posts.

item_id_fn(kind, raw, platform) returns an item id; the job passes
lambda kind, raw, platform: items.item_id(kind, items.canonical_key(kind, raw, platform)) from
core.detect.items (lane L2), and a ValueError from it skips that item. geo_fn is
core.detect.geo.geo_for_post; it is asked only for ZA, NG and KE, and GLOBAL rows stay unlocated.

Keyword arguments: lane is the caller's lane, required on search routes (expansion, exploration,
confirm, placebo, agent_live); pull_seq is the running pull number of a rank list, required on rank
routes; seed_key is the query or item the call was made for; protocol overrides the protocol string
built from the route and its fixed parameters (a panel names its membership version this way).

The brief's confirm lane (lane L2) calls, per SocialCrawlClient Result of tiktok/search/top or search/multi,

    ingest(bq, result, market, lane="confirm", *, params, run_id, fetched_at, item_id_fn, geo_fn, seed_key=None)

which parses the Result's body as above (lane_class search_presence) and writes the rows through
core.collect.writers: posts by the MERGE on post_id, post_observations by append.

Route families (SOURCES.md costed rows 1 to 8, 10 to 16, 22, 23, 23a, 23b; DATA.md section 3.2):
- rank lists of posts (tiktok/trending feed=local, youtube/videos/trending, youtube/shorts/trending,
  reddit/subreddit): posts, observations with rank and pull_seq, and one rank counter per item the
  post names (hashtags, sound, creator), keeping the item's best rank in the pull;
- boards of entries that are not posts (tiktok/hashtags/popular, instagram/music/trending,
  apple_music/charts): rank counters only, the vendor's rank from post.ext.trend else list position.
  The live hashtag board's post.ext.trend.popularity_curve is a 0 to 100 index against each tag's own
  peak, not posts per day, so it is not written as delta counters;
- counters (tiktok/song, tiktok/hashtag totals; tiktok/song/videos adoption curve; instagram/audio/reels,
  a total only when the page returns one): GLOBAL counters, and any posts on those pages as watchlist
  sightings; reels on an Instagram audio page take the page's audio_id when they name no sound;
- tiktok/profile/videos, a new feed author's recent posts (row 10): watchlist sightings, older posts kept,
  since they are the creator's usual views for the breakout baseline;
- panels (facebook/profile/posts, prism/profiles include=posts, twitter/user/tweets): posts and panel
  observations, dropping posts published before since= (timelines arrive unsorted with old posts);
- searches (tiktok/search/top, tiktok/search/hashtag, search/multi, instagram/search/reels) and
  instagram/location/posts:
  search_presence sightings;
  search/multi names the platform on each data.items row and keeps only call metadata in data.sources;
- prism/post-stats: observations re-reading metrics, no posts row (the MERGE keeps stable fields);
- web/scrape (the X trends archive, seed only): no rows here.
The seed queue also calls tiktok/profile/videos and instagram/audio/reels in a search lane (DATA.md
section 5); those reads are search_presence in the caller's lane and write no counter, so a seed never
starts a series.
Every other route raises ValueError. The live probe of 28 September 2026 confirmed the {computed, post,
vendor_labels} envelope for rank lists, boards, charts, tiktok/hashtag (one entry at data level, total
in post.ext.video_count), searches and prism/post-stats (data.results); parse_live.json holds those
shapes. Panels, tiktok/song, tiktok/song/videos, instagram/music/trending, tiktok/search/hashtag,
tiktok/profile/videos and instagram/audio/reels are still unprobed; the last three are parsed on
spec-shaped fixtures (parse_spec.json) in the probed envelope.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

from core.collect.ids import post_id

CONFIG = Path(__file__).resolve().parents[1] / "config"
VENDOR = "socialcrawl"
MARKETS = ("ZA", "NG", "KE")
GLOBAL = "GLOBAL"
GLOBAL_TZ = "Africa/Johannesburg"  # GLOBAL rows take the SAST day the collect job runs on
SEARCH_LANES = ("expansion", "exploration", "anchor", "confirm", "placebo", "agent_live")
INGEST_STATUSES = ("ok", "empty", "cached")  # the client statuses that carry a vendor body
TIERS = ((10_000, "nano"), (100_000, "micro"), (500_000, "mid"), (1_000_000, "macro"))
TWITTER_TIME = "%a %b %d %H:%M:%S %z %Y"
ITEM_KEYS = ("items", "posts", "results", "videos", "data")

POST_COLUMNS = (
    "post_id", "platform", "native_id", "url", "creator_id", "creator_tier_at_post", "text", "transcript",
    "hashtags", "sound_id", "thumbnail_url", "duration_s", "published_at", "post_date", "views", "likes",
    "comments", "shares", "engagement", "geo_market", "geo_confidence", "geo_source", "geo_scope", "vendor",
    "endpoint", "source_regime", "vendor_labels", "run_id")
OBSERVATION_COLUMNS = (
    "post_id", "observed_at", "observed_date", "market", "source_market", "source_region", "platform", "route",
    "series", "protocol", "lane", "lane_class", "seed_key", "pull_seq", "rank", "views", "likes", "comments",
    "shares", "run_id")
# The creators columns collect fills (core/schema/core.sql); tier, verified_region, coord_score and
# account_created_at belong to other jobs. No column holds or infers age.
CREATOR_COLUMNS = ("creator_id", "platform", "handle", "display_name", "followers", "verified", "profile_location", "home_market",
                   "first_seen", "last_seen")
COUNTER_COLUMNS = (
    "obs_date", "market", "platform", "item_id", "series", "route", "protocol", "is_board", "lane_class",
    "unit", "pull_seq", "value", "source", "observed_at", "available_at", "run_id")

# The curated creator panel reads prism/profiles too, in the same lane, beside the culture desk. It keeps its own
# series so a zero day of one is never the day before of the other (RB-C3, Albert's decision of 10 Oct 2026).
# Rows written before the split stay under panel_culture_desk as written. A caller names the curated panel with
# curated=True on a prism/profiles read, since only the caller knows which list the items came from.
CURATED_PANEL_SERIES = "panel_curated_creators"

# family, platform, series, is_board, protocol keys, markets. None platform: read from each row.
ROUTES = {
    "tiktok/trending": ("rank", "tiktok", "feed_tiktok", False, ("region", "feed"), MARKETS),
    "youtube/videos/trending": ("rank", "youtube", "board_youtube", True,
                                ("region", "category", "language", "max_results"), MARKETS),
    "youtube/shorts/trending": ("rank", "youtube", "board_global_music", True, (), (GLOBAL,)),
    "reddit/subreddit": ("rank", "reddit", "list_reddit", False, ("subreddit", "sort", "timeframe"), MARKETS),
    "tiktok/hashtags/popular": ("board", "tiktok", "board_tiktok_hashtag", True,
                                ("countryCode", "period", "industry"), MARKETS),
    "instagram/music/trending": ("board", "instagram", "board_global_music", True, (), (GLOBAL,)),
    "apple_music/charts": ("board", "apple_music", "board_apple_music", True, ("country", "type", "limit"), MARKETS),
    "tiktok/song": ("count", "tiktok", "counter_tiktok_sound", False, (), MARKETS + (GLOBAL,)),
    "tiktok/hashtag": ("count", "tiktok", "counter_tiktok_hashtag", False, (), MARKETS + (GLOBAL,)),
    "tiktok/song/videos": ("curve", "tiktok", "curve_tiktok_sound", False, (), MARKETS + (GLOBAL,)),
    "instagram/audio/reels": ("count", "instagram", "counter_ig_audio", False, (), MARKETS + (GLOBAL,)),
    "tiktok/profile/videos": ("watch", "tiktok", "watch", False, ("sort_by", "region"), MARKETS),
    "facebook/profile/posts": ("panel", "facebook", "panel_fb_hub", False, (), MARKETS),
    "prism/profiles": ("panel", None, "panel_culture_desk", False, ("include",), MARKETS),
    "twitter/user/tweets": ("panel", "twitter", "panel_x_hub", False, (), MARKETS),
    "tiktok/search/top": ("search", "tiktok", "search", False, ("publish_time", "sort_by", "country", "max_pages"),
                          MARKETS),
    "tiktok/search/hashtag": ("search", "tiktok", "search", False,
                              ("region", "max_age_days", "min_views", "sort_rows"), MARKETS),
    "search/multi": ("search", None, "search", False, ("platforms",), MARKETS),
    "instagram/search/reels": ("search", "instagram", "search", False, ("include", "date_posted", "max_pages"),
                               MARKETS),
    "instagram/location/posts": ("location", "instagram", "ig_location", False, ("location_id",), MARKETS),
    "youtube/search/advanced": ("search", "youtube", "search", False,
                                  ("location", "location_radius", "order", "includeExtras"), MARKETS),
    "tiktok/location/posts": ("search", "tiktok", "search", False, ("location_id",), MARKETS),
    "tiktok/profile": ("profile", "tiktok", "profile_country", False, (), MARKETS),
    "instagram/profile/about": ("profile", "instagram", "profile_country", False, (), MARKETS),
    "twitter/tweet": ("search", "twitter", "search", False, (), MARKETS),
    "prism/post-stats": ("restat", None, "counter_post_views", False, (), MARKETS),
    "web/scrape": ("none", None, None, False, (), MARKETS + (GLOBAL,)),
}
# family: (fixed lane, lane class). Search lanes come from the caller.
LANES = {
    "rank": ("sweep", "unbiased_rank"), "panel": ("panel", "panel"), "location": ("sweep", "search_presence"),
    "count": ("watchlist", "watchlist"), "curve": ("watchlist", "watchlist"), "watch": ("watchlist", "watchlist"),
    "restat": ("watchlist", "unbiased_counter"), "search": (None, "search_presence"),
    "profile": ("panel", "search_presence"),
}
SEEDED = ("tiktok/profile/videos", "instagram/audio/reels", "tiktok/song/videos", "twitter/user/tweets",
          "facebook/profile/posts")
HOSTS = {"tiktok.com": "tiktok", "youtube.com": "youtube", "youtu.be": "youtube", "instagram.com": "instagram",
         "x.com": "twitter", "twitter.com": "twitter", "facebook.com": "facebook", "reddit.com": "reddit",
         "threads.net": "threads", "threads.com": "threads", "linkedin.com": "linkedin"}
HASHTAG = re.compile(r"#(\w+)")
OFFSET_SPACE = re.compile(r"\s+(?=[+-]\d{2}:?\d{2}$)")


def parse(route, params, market, body, fetched_at, run_id, *, item_id_fn, geo_fn,
          lane=None, seed_key=None, pull_seq=None, protocol=None, profile_cache=None, curated=False):
    return _parse(route, params, market, body, fetched_at, run_id, item_id_fn=item_id_fn, geo_fn=geo_fn,
                  lane=lane, seed_key=seed_key, pull_seq=pull_seq, protocol=protocol, profile_cache=profile_cache,
                  curated=curated)[0]


def parse_with_creators(route, params, market, body, fetched_at, run_id, *, item_id_fn, geo_fn,
                        lane=None, seed_key=None, pull_seq=None, protocol=None, profile_cache=None, curated=False):
    """parse's rows plus "creators": one row per post that names a creator, CREATOR_COLUMNS in order."""
    out, creators = _parse(route, params, market, body, fetched_at, run_id, item_id_fn=item_id_fn, geo_fn=geo_fn,
                           lane=lane, seed_key=seed_key, pull_seq=pull_seq, protocol=protocol, profile_cache=profile_cache,
                           curated=curated)
    return {**out, "creators": creators}


def _parse(route, params, market, body, fetched_at, run_id, *, item_id_fn, geo_fn,
           lane=None, seed_key=None, pull_seq=None, protocol=None, profile_cache=None, curated=False):
    if route not in ROUTES:
        raise ValueError(f"no parser for route {route!r}")
    family, platform, series, is_board, keys, markets = ROUTES[route]
    market = str(market).strip().upper()
    if market not in markets:
        raise ValueError(f"{route} does not take market {market!r}; it takes {markets}")
    if curated:
        if route != "prism/profiles":
            raise ValueError(f"only prism/profiles reads the curated panel, not {route!r}")
        series = CURATED_PANEL_SERIES
    params = dict(params or {})
    fixed_lane, lane_class = LANES.get(family, (None, None))
    seeded = route in SEEDED and lane in SEARCH_LANES
    post_series = "watch" if family in ("count", "curve") else series  # posts on sound and hashtag pages
    if family == "search" or seeded:
        if lane not in SEARCH_LANES:
            raise ValueError(f"{route} needs a search lane from {SEARCH_LANES}, got {lane!r}")
        lane_class = "search_presence"
        post_series = lane if lane in ("placebo", "agent_live") else "search"
    elif lane is not None and lane != fixed_lane:
        raise ValueError(f"{route} runs in lane {fixed_lane!r}, not {lane!r}")
    if family in ("rank", "board"):
        if not isinstance(pull_seq, int) or isinstance(pull_seq, bool):
            raise ValueError(f"{route} is a rank list and needs an integer pull_seq")
        lane_class = "unbiased_rank"
    if route == "tiktok/trending" and params.get("feed") != "local":
        raise ValueError("tiktok/trending is parsed only for feed=local; feed=global is another regime")
    observed = _utc(fetched_at)
    out = {"posts": [], "observations": [], "counters": []}
    if family == "none" or not isinstance(body, dict) or body.get("success") is False:
        return out, []
    source_market, source_region = _source_provenance(route, params, market)
    ctx = {
        "route": route, "market": market, "is_board": is_board, "run_id": run_id, "counter_series": series,
        "series": post_series,
        "lane": lane or fixed_lane, "lane_class": lane_class, "seed_key": seed_key, "pull_seq": pull_seq,
        "source_market": source_market, "source_region": source_region,
        "protocol": protocol or _protocol(route, params, keys), "observed_at": observed.isoformat(),
        "observed_date": _local_day(observed, market), "item_id_fn": item_id_fn, "geo_fn": geo_fn,
        "since": _since(params.get("since"), market) if family == "panel" else None,
        "handle": _first(params, "handle", "username"), "creators": [], "profile_cache": profile_cache,
    }
    data = body.get("data")
    if route in ("youtube/search/advanced", "tiktok/location/posts"):
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError(f"{route} needs data.items as a list")
    fallback = _first(params, "handle", "pageId", "url", "username")
    if family == "rank":
        for rank, item in enumerate(_rows(data), 1):
            _post(out, ctx, item, platform, rank=rank, fallback=fallback)
        _rank_counters(out, ctx, platform)
    elif family == "board":
        _board(out, ctx, route, platform, _rows(data))
    elif family in ("count", "curve"):
        if not seeded:
            _count(out, ctx, route, family, platform, params, data)
        for item in _rows(data):
            _post(out, ctx, item, platform, sound=params.get("audio_id"))
    elif family == "panel" and platform is None:
        for row in _rows(data):
            if not _ok(row):
                continue
            author = _dict(row.get("author")) or _dict(row.get("profile"))
            handle = _first(row, "handle") or _first(author, "username")
            for item in _list(row.get("posts")) or _list(row.get("items")):
                _post(out, ctx, item, row.get("platform"), fallback=handle, author=author, handle=handle)
    elif family == "profile":
        from core.collect.location_sources import profile_country

        author = _dict(_dict(data).get("author"))
        handle = params.get("handle")
        if handle and str(author.get("username") or "").lower() == str(handle).lower():
            raw_location = author.get("location")
            country = profile_country(body, handle, platform=platform)
            name = author.get("display_name")
            ctx["creators"].append({"creator_id": str(handle), "platform": platform, "handle": str(handle),
                "display_name": name if isinstance(name, str) and name.strip() else None, "followers": _number(author, "followers"),
                "verified": author.get("verified") if isinstance(author.get("verified"), bool) else None,
                "profile_location": raw_location if isinstance(raw_location, str) and raw_location.strip()
                    and (platform != "tiktok" or country is not None) else None,
                "home_market": country,
                "first_seen": ctx["observed_at"], "last_seen": ctx["observed_at"]})
            if profile_cache is not None:
                profile_cache.track_creator(ctx["creators"][-1], source=route)
    elif route == "twitter/tweet":
        from core.collect.x_discovery import status_url, verified_lookup

        url = status_url(params.get("url"))
        if url and verified_lookup(body, url, (url.split("/")[-3],)):
            _post(out, ctx, {"post": data["post"], "computed": data.get("computed")}, platform)
    elif family in ("panel", "location", "watch") or route in ("tiktok/search/top", "tiktok/search/hashtag",
                                                               "instagram/search/reels", "youtube/search/advanced",
                                                               "tiktok/location/posts"):
        for item in _rows(data):
            _post(out, ctx, item, platform, fallback=fallback)
    elif route == "search/multi":
        for source, rows in _sources(data):
            for item in rows:
                _post(out, ctx, item, source or item.get("platform"))
    elif family == "restat":
        for row in _rows(data):
            _restat(out, ctx, row)
    return out, ctx["creators"]


def ingest(bq, result, market, lane="confirm", *, params=None, run_id, fetched_at, item_id_fn, geo_fn,
           seed_key=None):
    """One search Result of the brief's confirm lane into posts (the writers MERGE on post_id, so a post
    already collected keeps its first sighting and takes the new metrics) and post_observations (append).
    params are the call's own, for the protocol; fetched_at is when the call returned. Market must be ZA, NG
    or KE. A post carrying the item it was searched for (seed_key) also gets its post_items row (_seed_links). A
    Result that did not return a readable body writes nothing. Returns the rows written."""
    from core.collect import writers  # writers imports this module

    market = str(market or "").strip().upper()
    if market not in MARKETS:
        raise ValueError(f"ingest takes a market from {MARKETS}, not {market!r}")
    if result.status not in INGEST_STATUSES or not isinstance(result.body, dict):
        return {"posts": [], "observations": []}
    out = parse(result.route, params, market, result.body, fetched_at, run_id, item_id_fn=item_id_fn,
                geo_fn=geo_fn, lane=lane, seed_key=seed_key)
    writers.merge_posts(bq, out["posts"])
    writers.append(bq, "post_observations", out["observations"])
    writers.insert_post_items(bq, _seed_links(out["posts"], seed_key))
    return {"posts": out["posts"], "observations": out["observations"]}


def _seed_links(posts, seed_key):
    """post_items rows linking each post to the item it was searched for (seed_key, the candidate's item id), only
    when the post itself carries that item by core.detect.items' own rules. Detect itemises only posts first sighted
    on its own day, so without this a find ingested after detect would never be linked to its item."""
    if not seed_key:
        return []
    from core.detect import items  # detect reads collect's config; imported only when a find is linked

    links = []
    for post in posts:
        for it in items.items_for_post(post):
            if it["item_id"] == seed_key:
                links.append({"post_id": post["post_id"], "item_id": seed_key, "via": it["via"]})
                break
    return links


def parse_time(raw):
    """A vendor time as an aware UTC datetime, or None: ISO (Z or offset, space or T), Twitter's
    'Wed Oct 30 05:45:03 +0000 2019', or epoch seconds or milliseconds as a number or digit string."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw.astimezone(UTC)
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        number = float(raw)
        if number > 1e12:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, TWITTER_TIME).astimezone(UTC)
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(OFFSET_SPACE.sub("", text.replace("Z", "+00:00")))
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@lru_cache(maxsize=None)
def _zone(market):
    if market == GLOBAL:
        return ZoneInfo(GLOBAL_TZ)
    config = yaml.safe_load((CONFIG / "markets.yaml").read_text(encoding="utf-8"))
    zones = {m["country_code"]: m["timezone"]["value"] for m in config["markets"].values()}
    return ZoneInfo(zones[market])


def _local_day(moment, market):
    return moment.astimezone(_zone(market)).date().isoformat()


def _utc(fetched_at):
    moment = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")) if isinstance(fetched_at, str) else fetched_at
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("fetched_at must be an aware datetime or an ISO string with an offset")
    return moment.astimezone(UTC)


def _since(raw, market):
    """The first market-local day a panel keeps, from since= as a date, a time or an epoch."""
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError:
        moment = parse_time(raw)
        return moment.astimezone(_zone(market)).date() if moment else None


def _protocol(route, params, keys):
    fixed = sorted((k, params[k]) for k in keys if params.get(k) not in (None, ""))
    return route + ("?" + "&".join(f"{k}={v}" for k, v in fixed) if fixed else "")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _first(node, *keys):
    for key in keys:
        value = _dict(node).get(key)
        if value not in (None, "", []):
            return value
    return None


def _number(node, *keys):
    value = _first(node, *keys)
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _rows(data):
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    for key in ITEM_KEYS:
        if isinstance(_dict(data).get(key), list):
            return [row for row in data[key] if isinstance(row, dict)]
    return []


def _sources(data):
    """search/multi rows: data.items naming their platform each, else grouped under data.sources.<platform>.items."""
    rows = _rows(data)
    sources = _dict(data).get("sources")
    if rows or not isinstance(sources, dict):
        return [(None, rows)]
    return [(name, _rows(group)) for name, group in sources.items()]


def _ok(row):
    return not row.get("error") and str(row.get("status") or "ok").lower() in ("ok", "success")


def _tier(followers):
    if followers is None:
        return None
    return next((name for limit, name in TIERS if followers < limit), "mega")


def _hashtags(node, content, text):
    listed = _list(content.get("hashtags")) or _list(node.get("hashtags"))
    names = [(_first(h, "name", "tag", "title") if isinstance(h, dict) else h) for h in listed] if listed \
        else HASHTAG.findall(text or "")
    out = []
    for name in names:
        name = str(name or "").strip().lstrip("#")
        if name and name not in out:
            out.append(name)
    return out


def _sound(node, ext):
    music = _dict(node.get("music")) or _dict(node.get("sound"))
    value = _first(ext, "music_id", "audio_id") or _first(node, "music_id", "audio_id", "sound_id") \
        or _first(music, "id")
    return str(value) if value is not None else None


def _metrics(node):
    eng = _dict(node.get("engagement")) or _dict(node.get("stats")) or _dict(node.get("metrics"))
    return {k: _number(eng, k) for k in ("views", "likes", "comments", "shares")}


def _geo(ctx, platform, market, **signals):
    if market not in MARKETS:
        return None, None, None
    return ctx["geo_fn"](platform, market, **signals)


def _post(out, ctx, item, platform, *, rank=None, fallback=None, author=None, sound=None, handle=None):
    """One post row, its observation and its creator row; None when the post has no platform, id or URL, or
    predates since=. author is the panel row's profile, read where the post's own author block is silent."""
    item = _dict(item)
    node = _dict(item.get("post")) or item
    platform = str(platform or node.get("platform") or "").strip().lower()
    native, url = node.get("id"), node.get("url")
    pid = post_id(platform, native, url) if platform else None
    if pid is None:
        return None
    content, ext, media = _dict(node.get("content")), _dict(node.get("ext")), _dict(node.get("media"))
    published = parse_time(_first(node, "published_at", "created_at", "create_time", "taken_at")) \
        or parse_time(_first(ext, "published_at_epoch"))
    if ctx["since"] and published and published.astimezone(_zone(ctx["market"])).date() < ctx["since"]:
        return None
    profile = _dict(author)
    author = _dict(node.get("author")) or profile
    title = _first(content, "title") or _first(node, "title")
    body = _first(content, "text", "description") or _first(node, "text", "description", "caption")
    text = "\n".join(str(part) for part in (title, body) if part) or None
    metrics = _metrics(node)
    counted = [metrics[k] for k in ("likes", "comments", "shares")]
    followers = _number(author, "followers", "follower_count", "followers_count", "subscribers")
    if followers is None:
        followers = _number(ext, "author_followers")
    if followers is None:
        followers = _number(profile, "followers", "follower_count", "followers_count", "subscribers")
    location = _first(author, "location") or _first(profile, "location")
    location = location if isinstance(location, str) else None
    language = _first(_dict(item.get("computed")), "language") or _first(node, "language") \
        or _first(ext, "content_language", "defaultAudioLanguage")
    # YouTube keeps the description in ext; it is post text for place mentions, not a stored column.
    described = _first(ext, "description")
    geo_text = "\n".join(p for p in (text, described) if isinstance(p, str)) \
        if isinstance(described, str) and described not in (text or "") else text
    handle = _first(author, "username", "handle") or _first(profile, "username", "handle") or handle or ctx["handle"]
    signals = {"ext_region": _first(ext, "region"),
               "home_market": _first(author, "region", "country") or _first(profile, "region", "country")
               or _first(ext, "author_country"), "profile_location": location, "text": geo_text, "language": language}
    if platform in ("tiktok", "instagram"):
        from core.collect.location_sources import country_code

        signals["home_market"] = country_code(signals["home_market"]) or signals["home_market"]
    cache = ctx["profile_cache"]
    account_country = country_code(_first(ext, "author_country")) if platform == "instagram" \
        and ctx["route"] == "instagram/search/reels" else None
    if cache is not None and account_country is not None:
        cache.remember(platform, handle, account_country)
    enriched = cache.signals(platform, handle, signals) if cache is not None else signals
    geo = _geo(ctx, platform, ctx["market"], **enriched)
    creator = _first(author, "username") or fallback or _first(author, "id", "channel_id") or _first(ext, "channel_id")
    row = {
        "post_id": pid, "platform": platform, "native_id": None if native is None else str(native),
        "url": url, "creator_id": None if creator is None else str(creator), "creator_tier_at_post": _tier(followers),
        "text": text, "transcript": None, "hashtags": _hashtags(node, content, text),
        "sound_id": _sound(node, ext) or (None if sound in (None, "") else str(sound)),
        "thumbnail_url": _first(media, "thumbnail_url", "thumbnail", "cover") or _first(content, "thumbnail_url")
        or _first(node, "thumbnail_url"),
        "duration_s": _duration(media, content, node),
        "published_at": published.isoformat() if published else None,
        "post_date": _local_day(published, ctx["market"]) if published else ctx["observed_date"],
        **metrics, "engagement": None if None in counted else sum(counted),  # never a partial sum
        "geo_market": geo[0], "geo_confidence": geo[1], "geo_source": geo[2], "geo_scope": None,
        "vendor": VENDOR, "endpoint": ctx["route"], "source_regime": VENDOR,
        "vendor_labels": item.get("vendor_labels"), "run_id": ctx["run_id"],
    }
    out["posts"].append(row)
    if cache is not None:
        cache.bind(row, platform, ctx["market"], handle, signals)
    out["observations"].append(_observation(ctx, pid, platform, metrics, rank))
    if creator is not None:
        verified = author.get("verified")
        name = _first(author, "display_name", "name", "nickname")
        ctx["creators"].append({
            "creator_id": str(creator), "platform": platform, "handle": None if handle is None else str(handle),
            "display_name": None if name is None else str(name), "followers": followers,
            "verified": verified if isinstance(verified, bool) else None, "profile_location": location,
            "home_market": cache.country(platform, handle) if cache is not None else account_country,
            "first_seen": ctx["observed_at"], "last_seen": ctx["observed_at"],
        })
        if cache is not None:
            cache.track_creator(ctx["creators"][-1])
    return row


def _duration(media, content, node):
    value = _first(media, "duration", "duration_s") or _first(content, "duration_seconds") \
        or _first(node, "duration", "duration_s")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _observation(ctx, pid, platform, metrics, rank):
    return {
        "post_id": pid, "observed_at": ctx["observed_at"], "observed_date": ctx["observed_date"],
        "market": ctx["market"], "source_market": ctx["source_market"], "source_region": ctx["source_region"],
        "platform": platform, "route": ctx["route"], "series": ctx["series"],
        "protocol": ctx["protocol"], "lane": ctx["lane"], "lane_class": ctx["lane_class"],
        "seed_key": ctx["seed_key"], "pull_seq": ctx["pull_seq"] if rank is not None else None, "rank": rank,
        **metrics, "run_id": ctx["run_id"],
    }


def _source_provenance(route, params, market):
    scoped = {
        "tiktok/trending": ("region",),
        "youtube/videos/trending": ("region",),
        "tiktok/search/top": ("country", "region"),
        "tiktok/search/hashtag": ("region",),
    }.get(route)
    if scoped is None or (route == "tiktok/trending" and params.get("feed") != "local"):
        return None, None
    request_market = str(market or "").strip().upper()
    if request_market not in MARKETS:
        return None, None
    explicit = [params[key] for key in scoped if key in params]
    if not explicit:
        return None, None
    normalized = []
    for value in explicit:
        if not isinstance(value, str):
            return None, None
        value = value.strip().upper()
        if value not in MARKETS:
            return None, None
        normalized.append(value)
    if len(set(normalized)) != 1 or normalized[0] != request_market:
        return None, None
    if route == "tiktok/search/top" and params.get("exclude_country"):
        return None, None
    return request_market, normalized[0]


def _item(ctx, kind, raw, platform):
    if raw in (None, ""):
        return None
    if kind == "hashtag":
        raw = str(raw).strip().lstrip("#")
        if not raw or len(raw) > 100 or re.search(r"\s", raw) or not any(ch.isalnum() for ch in raw):
            return None
    try:
        return ctx["item_id_fn"](kind, raw, platform)
    except ValueError:
        return None


def _counter(ctx, *, market, platform, item_id, series, lane_class, unit, value, is_board=False,
             pull_seq=None, obs_date=None, source="live"):
    return {
        "obs_date": obs_date or _local_day(datetime.fromisoformat(ctx["observed_at"]), market),
        "market": market, "platform": platform, "item_id": item_id, "series": series, "route": ctx["route"],
        "protocol": ctx["protocol"], "is_board": is_board, "lane_class": lane_class, "unit": unit,
        "pull_seq": pull_seq, "value": None if value is None else float(value), "source": source,
        "observed_at": ctx["observed_at"], "available_at": ctx["observed_at"], "run_id": ctx["run_id"],
    }


def _rank_counters(out, ctx, platform):
    """Each item a post names, at the best rank of any post naming it in this pull."""
    best = {}
    for post, obs in zip(out["posts"], out["observations"]):
        named = [("hashtag", h) for h in post["hashtags"]] + [("sound", post["sound_id"]),
                                                              ("creator", post["creator_id"])]
        for kind, raw in named:
            item_id = _item(ctx, kind, raw, post["platform"])
            if item_id and (item_id not in best or obs["rank"] < best[item_id]):
                best[item_id] = obs["rank"]
    out["counters"] += [_rank(ctx, platform, item_id, rank) for item_id, rank in best.items()]


def _rank(ctx, platform, item_id, rank):
    return _counter(ctx, market=ctx["market"], platform=platform, item_id=item_id, series=ctx["series"],
                    lane_class="unbiased_rank", unit="rank", value=rank, is_board=ctx["is_board"],
                    pull_seq=ctx["pull_seq"])


def _board(out, ctx, route, platform, rows):
    """Board and chart entries, bare or in the post envelope: the vendor's rank when it gives one, else list position."""
    for position, row in enumerate(rows, 1):
        node = _dict(row.get("post")) or row
        ext = _dict(node.get("ext"))
        rank = _number(_dict(ext.get("trend")), "rank") or _number(ext, "rank", "position") \
            or _number(node, "rank", "position") or position
        if route == "tiktok/hashtags/popular":
            path = [part for part in urlsplit(str(node.get("url") or "")).path.split("/") if part]
            raw = _first(node, "hashtag_name", "hashtag", "name", "title") or _first(_dict(node.get("content")), "text") \
                or (path[1] if len(path) == 2 and path[0] == "tag" else None)
            item_id = _item(ctx, "hashtag", raw, platform)
        elif route == "instagram/music/trending":
            track = _dict(node.get("track"))
            raw = _first(track, "audio_cluster_id", "audio_asset_id", "audio_id", "id") \
                or _first(node, "audio_id", "music_id", "id")
            item_id = _item(ctx, "sound", raw, platform)
        else:
            item_id = _item(ctx, "sound", _first(node, "id", "song_id", "apple_id"), platform)
        if item_id is None:
            continue
        out["counters"].append(_rank(ctx, platform, item_id, rank))
        if route == "tiktok/hashtags/popular":
            _curve(out, ctx, ctx["market"], platform, item_id, "curve_tiktok_hashtag",
                   _list(row.get("trend")) or _list(row.get("curve")) or _list(row.get("daily")))


def _curve(out, ctx, market, platform, item_id, series, points):
    """A vendor per-day curve as delta counters: past days vendor_history, the read's own day live."""
    today = _local_day(datetime.fromisoformat(ctx["observed_at"]), market)
    for point in points:
        if isinstance(point, (list, tuple)) and len(point) == 2:
            when, value = point
        elif isinstance(point, dict):
            when = _first(point, "date", "day", "time", "timestamp")
            value = _first(point, "value", "count", "videos", "posts")
        else:
            continue
        try:
            day = date.fromisoformat(str(when)).isoformat()
        except ValueError:
            moment = parse_time(when)
            day = _local_day(moment, market) if moment else None
        if day is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out["counters"].append(_counter(
            ctx, market=market, platform=platform, item_id=item_id, series=series, lane_class="unbiased_counter",
            unit="delta", value=value, obs_date=day, source="vendor_history" if day < today else "live"))


def _count(out, ctx, route, family, platform, params, data):
    node = _dict(data)
    post = _dict(node.get("post"))  # live: one {computed, post} entry at data level, the total in post.ext
    if route == "tiktok/hashtag":
        raw = params.get("hashtag") or _first(node, "name", "title") or _first(_dict(post.get("content")), "text")
        item_id = _item(ctx, "hashtag", raw, "tiktok")
    else:
        raw = params.get("clipId") or params.get("audio_id") or _first(node, "id", "music_id") or _first(post, "id")
        item_id = _item(ctx, "sound", raw, platform)
    if item_id is None:
        return
    if family == "curve":
        _curve(out, ctx, GLOBAL, platform, item_id, ctx["counter_series"], _list(node.get("adoption")))
        return
    total = None
    for part in (node, _dict(post.get("ext")), *(v for v in node.values() if isinstance(v, dict))):
        total = _number(part, "video_count", "videoCount", "user_count", "userCount", "media_count", "reels_count",
                        "clips_count", "videos", "posts")
        if total is not None:
            break
    if total is not None:
        out["counters"].append(_counter(ctx, market=GLOBAL, platform=platform, item_id=item_id,
                                        series=ctx["counter_series"], lane_class="unbiased_counter", unit="total",
                                        value=total))


def _restat(out, ctx, row):
    """A prism/post-stats re-read: one observation with fresh metrics, keyed like the original post."""
    if not _ok(row):
        return
    node = _dict(row.get("post")) or row
    url = node.get("url") or row.get("url")
    platform = row.get("platform") or node.get("platform") or _platform_of(url)
    pid = post_id(platform, node.get("id") or row.get("id"), url) if platform else None
    if pid is None:
        return
    read = _metrics(row)
    metrics = read if any(v is not None for v in read.values()) else _metrics(node)
    out["observations"].append(_observation(ctx, pid, str(platform).lower(), metrics, None))


def _platform_of(url):
    host = (urlsplit(url).hostname or "") if isinstance(url, str) else ""
    return next((name for suffix, name in HOSTS.items() if host == suffix or host.endswith("." + suffix)), None)
