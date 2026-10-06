"""Candidate sweep for the ZA, NG and KE source lists: every panel's candidates from three free sources and one
paid one, all written to one candidates file of JSON lines (CANDIDATES in the system temp folder, outside the repo, or the path --out names).

--from-warehouse   authors and @mentioned accounts in intelligence_42_core.posts and post_observations since
                   WAREHOUSE_SINCE, and in the legacy social tables of docs/full-42/reference/data-inventory.md
                   (enriched_content authors and mentions, seed_graph handle terms; social platforms and social
                   connectors only, never trend_scores, a Google Trends lane or a search source). Every read is
                   parameterised and date-bounded and goes through gdelt._checked: dry run first, refused above
                   5 GB. A refused or failed read is reported and the others still run. Ranked per market and
                   platform by days seen, then engagement.
--from-configs     engine/configs (creator watchlists, the YouTube registry, the accounts, pages and feeds in
                   sources.yaml), core/config/hubs.yaml, core/config/markets.yaml and local_sources.py, each
                   marked in use when the collect job reads it today.
--from-brand24     Albert's Brand24 export of Facebook pages: pages Brand24 tags with one market only, with
                   BRAND24_MIN_ROWS rows or more or seen in August 2026, named from the page URL where there is
                   one. The export holds page ids, names, URLs, counts and dates; any other header stops the read.
                   Seen in August alone keeps a page in the candidates file; a draft needs BRAND24_MIN_ROWS rows.
--discover         paid: tiktok/search/top and search/multi (routes PRICED covers) on each market's top culture
                   terms from the current collect, through the SocialCrawl client on the build share, inside a
                   Cloud Run job only. It stops before any call that would take the sweep past DISCOVER_CAP.
                   Bodies land in raw_responses; --discover --from-run RUN_ID reads them back (BigQuery only,
                   no SocialCrawl) and turns them into candidates.

Every candidate carries handle, platform, market, kind, followers and last post date when known, its source and a
one-line why. The market of an account is the market of the observations that saw it; seen in more than one
market, it is kept only where a strict majority of its sightings are (a foreign drop elsewhere). Also dropped and
counted: rule 1 (gdelt.blocked on the handle, name or why, plus the terms core/config/tests/test_hubs.py refuses
in hubs.yaml), church and religious organisations, government and state accounts, and dead accounts (no post in
DEAD_DAYS days when the last post is known, counted back from when the source last looked). Nothing here reads or
guesses an audience's age.

--propose prices each panel from PRICED, reads the morning's plan totals from the collect job's own plan, and
shares what the collect share (which pays the panels) has left across the markets and panels, best candidates
first, never past that budget. Only pages, outlets and public creators enter it (draft_hold, FOLLOWER_FLOOR).
--write-draft writes that proposal into core/config/hubs.yaml (a top-level draft key, with its budget and a
warning when a --budget override passes the collect share) and core/collect/local_sources.py (IG_GOSSIP_DRAFT
and FEEDS_DRAFT), which the collect job never reads. It refuses when the plan totals would move, and refuses to
replace a draft edited by hand (its stored hash differs) unless --force-draft.

    py -3.13 -m core.collect.sources_sweep --from-configs [--plan]
    py -3.13 -m core.collect.sources_sweep --from-brand24 [PATH] [--plan]
    py -3.13 -m core.collect.sources_sweep --from-warehouse [--plan] [--since 2026-09-28]
    py -3.13 -m core.collect.sources_sweep --discover --plan
    python -m core.collect.sources_sweep --discover                         inside a Cloud Run job only
    py -3.13 -m core.collect.sources_sweep --discover --from-run RUN_ID --run-date D [--plan]
    py -3.13 -m core.collect.sources_sweep --propose --run-date 2026-09-30
    py -3.13 -m core.collect.sources_sweep --write-draft --run-date 2026-09-30 [--plan]
"""

import argparse
import csv
import hashlib
import json
import re
import sys
import tempfile
import types
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from core.collect import job, local_sources
from core.collect.calendar import LEGACY_SOCIAL_PLATFORMS, LEGACY_SOCIAL_SOURCES
from core.collect.gdelt import OverCap, _checked, blocked
from core.collect.socialcrawl_client import PRICED, SAST, load_caps, params_hash, quote_for

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# Outside the repo, so a candidates file is never committed (core/collect/samples/sources/ is also ignored).
CANDIDATES = Path(tempfile.gettempdir()) / "42-sources-sweep" / "candidates.jsonl"
BRAND24_CSV = Path.home() / "dev" / "42-inputs" / "brand24-facebook-pages.csv"
HUBS = ROOT / "core" / "config" / "hubs.yaml"
MARKETS_YAML = ROOT / "core" / "config" / "markets.yaml"
LOCAL = HERE / "local_sources.py"
ENGINE = ROOT / "engine" / "configs"
PROJECT = "ogilvy-trends-v2"
CORE = f"{PROJECT}.intelligence_42_core"
CORE_ORIGIN = "intelligence_42_core"

MARKETS = ("ZA", "NG", "KE")
KINDS = ("gossip_entertainment", "news", "sport", "music", "creator", "brand", "other")
SOURCES = ("warehouse", "configs", "brand24", "discover")
CREATOR_PLATFORMS = ("tiktok", "instagram", "youtube", "threads")

# Source 1. 42's own collect starts on WAREHOUSE_SINCE; the legacy window is the last quarter of the old engine.
WAREHOUSE_SINCE = date(2026, 9, 28)
LEGACY_SINCE, LEGACY_UNTIL = date(2026, 6, 28), date(2026, 9, 28)
LEGACY_CONTENT = ("trends_v2_dev.enriched_content", "trends_v2_staging.enriched_content",
                  "intelligence_42_sources_staging.enriched_content")
LEGACY_SEEDS = ("trends_v2_dev.seed_graph", "trends_v2_staging.seed_graph")
LEGACY_SOURCES = LEGACY_SOCIAL_SOURCES + ("brand24",)   # social connectors only (calendar.py), plus Brand24 pages
LEGACY_PLATFORMS = LEGACY_SOCIAL_PLATFORMS + ("x",)
CORE_PLATFORMS = ("tiktok", "instagram", "twitter", "x", "youtube", "threads", "facebook", "reddit")
MIN_POSTS, TOP_PER_PLATFORM, POST_LOOKBACK_DAYS = 2, 150, 60
MENTION = r"(?:^|[^a-z0-9_.@/])@([a-z0-9_][a-z0-9_.]{1,29})"

# Drops.
DEAD_DAYS = 60
# A St or Saint prefix counts only before a saint's name, after the start or a non-letter, optionally with ack (the
# Anglican Church of Kenya) in front, so ackstpeters and St. Peter's drop while stanley, stone or fastjohnny stay.
RELIGIOUS = re.compile(r"church|ministries|chapel|cathedral|parish|diocese|mosque|masjid|islamic|catholic|"
                       r"anglican|methodist|pentecost|evangel|tabernacle|rccg|redeemed|prophet|pastor|bishop|"
                       r"apostle|apostolic|sermon|bible|quran|deeperlife|christembassy|baptist(?!e)|adventist|gospel|"
                       r"(?:^|[^a-z])(?:ack[\s._-]*)?(?:st|saint)[\s._'-]*(?:peter|paul|john|james|mary|mark(?!et)|"
                       r"luke|matthew|andrew|stephen|michael|joseph|patrick|francis|thomas|philip|jude|augustine|"
                       r"monica|teresa|anthony|kizito|benedict|dominic|ignatius|barnabas|gabriel)")
GOVERNMENT = re.compile(r"government|presiden|parliament|ministry|municipal|police|statehouse|state_house|senate|"
                        r"nnpc|kenyapower|eskom|gcis|interior(ke)?$|(^|[^a-z])gov(t|za|ng|ke)?($|[^a-z])|"
                        r"[a-z]gov(t)?$")
# The terms core/config/tests/test_hubs.py refuses anywhere in hubs.yaml, joined here so the words never sit in
# this file whole. gdelt.blocked lets some of them through as ordinary words; a draft must never hold one.
HUBS_REFUSED = tuple("".join(parts) for parts in (
    ("gen", "z"), ("gen", " z"), ("gen", "-z"), ("you", "th"), ("stu", "dent"), ("te", "en"),
    ("mill", "ennial"), ("gener", "ation"), ("boo", "mer"), ("you", "ng"), ("var", "sity"), ("adoles", "cent")))
HANDLE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}")
DASH_RUN = re.compile("[\u2013\u2014]|-{2,}")

# Kinds, read from the handle and name. The first family with a hit wins, so a music page that says daily is
# music, not news. Anything else is a creator on a creator platform, else other.
KIND_WORDS = (
    ("gossip_entertainment", ("gossip", "gist", "celeb", "blog", "showbiz", "entertainment", "tattle", "zalebs",
                              "maphephandaba", "mpasho", "ghafla", "tshisa", "lindaikeji", "bellanaija", "memes",
                              "comedy", "drama")),
    ("sport", ("soccer", "football", "sport", "rugby", "cricket", "athletic", "league", "laduma", "kickoff",
               "bafana", "supereagles", "harambee", "chiefs", "pirates", "sundowns", "gormahia")),
    ("music", ("music", "records", "afrobeat", "amapiano", "gengetone", "beats", "hiphop", "boomplay",
               "audiomack", "songs", "dj")),
    ("news", ("news", "newz", "times", "daily", "tv", "fm", "radio", "herald", "tribune", "gazette", "citizen",
              "nation", "standard", "punch", "vanguard", "guardian", "channels", "sabc", "enca", "ewn", "tuko",
              "kenyans", "legit", "arise", "nairametrics", "briefly", "sowetan", "reporters")),
    ("brand", ("safaricom", "vodacom", "mtn", "airtel", "telkom", "cellc", "bank", "shoprite", "checkers", "kfc",
               "nandos", "woolworths", "jumia", "takealot", "dstv", "multichoice", "showmax", "plc", "ltd")),
)
HUB_KINDS = {"gossip": "gossip_entertainment", "entertainment": "creator", "news": "news", "sport": "sport",
             "music": "music"}

# Source 3.
BRAND24_COLUMNS = ("page_slug", "markets", "rows_", "first_seen", "last_seen", "sample_url")
BRAND24_MIN_ROWS = 10
BRAND24_MONTH = (date(2026, 8, 1), date(2026, 8, 31))
# A handle is person-shaped when it is one lowercase token (letters, digits, underscores) of fewer than
# PERSON_LETTERS letters with no KIND_WORDS word, no PAGE_WORDS word and no PLACE_WORDS word: itsdennyc is, while
# mzansimagic (a place) and channelsforum (a news word) are not. A person-shaped Brand24 page needs its followers.
PERSON_LETTERS = 15
PAGE_WORDS = ("brand", "media", "club", "magazine", "online", "channel", "network", "studio", "hub", "info", "tv",
              "fm", "radio", "news", "music", "sport")
PLACE_WORDS = ("mzansi", "africa", "naija", "nigeria", "lagos", "abuja", "kenya", "nairobi", "mombasa", "kisumu",
               "joburg", "jozi", "durban", "soweto", "kasi", "pretoria", "capetown")
FACEBOOK_NOT_PAGES = ("share", "profile.php", "people", "groups", "pages", "watch", "events", "story.php",
                      "permalink", "permalink.php", "photo", "photos", "photo.php", "posts", "videos", "reel", "reels")
YOUTUBE_PROBED = date(2026, 5, 28)   # the research probe behind engine/configs/creators_youtube_handles.yaml

# Source 4.
DISCOVER_CAP = 150
TERMS_PER_MARKET, TERMS_READ = 8, 50
TERM_KINDS = ("hashtag", "topic", "meme", "sound")
DISCOVER_PLATFORMS = "instagram,youtube,twitter,facebook"   # the panels' platforms; TikTok has its own search
DISCOVER_ROUTES = ("tiktok/search/top", "search/multi")
STOP = ("insufficient_credits", "balance_floor", "cap_reached")

# Proposal. prism/profiles takes at most 25 items with include=posts, so a culture desk or gossip panel stays at 25.
PANELS = ("x", "culture_desk", "facebook", "ig_gossip", "feeds")
PANEL_ROUTES = {"x": "twitter/user/tweets", "culture_desk": "prism/profiles", "facebook": "facebook/profile/posts",
                "ig_gossip": "prism/profiles", "feeds": "rss (free)"}
PANEL_LIMIT = {"x": 25, "culture_desk": 25, "facebook": 25, "ig_gossip": 25, "feeds": 10}
HUB_PANELS = ("x", "culture_desk", "facebook")
CULTURE_KINDS = ("gossip_entertainment", "news", "sport", "music", "creator")
SOURCE_ORDER = ("configs", "brand24", "warehouse", "discover")
# Only pages, outlets and public creators reach a draft: anything the configs list (curated names, pages and
# feeds) outside the watchlists' tier_3, a Brand24 page with a page name (never a website or a number), at least
# BRAND24_MIN_ROWS rows and not person_shaped, a verified or business account where the source says
# so, or an account with at least FOLLOWER_FLOOR followers on its platform. Every other candidate, such as a
# warehouse author or @mention with no known follower count, stays in the candidates file only.
FOLLOWER_FLOOR = {"instagram": 10000, "tiktok": 10000, "x": 10000, "youtube": 10000, "facebook": 10000,
                  "threads": 10000}
# A queue ranks pages and outlets first, then the watchlists' tier_1 and tier_2, then everything else. tier_3 is the
# watchlists' micro-influencer and fan-account blob: it needs FOLLOWER_FLOOR like any account the configs do not list.
TIER_RANK = {"page": 0, "tier_1": 1, "tier_2": 2}
HASH_LINE = "# draft hash: "
HUBS_MARK = "# Sweep draft, written by core/collect/sources_sweep.py in its write-draft mode."
HUBS_NOTE = ("# The collect job reads markets only, never this draft, so the morning plan is unchanged.\n"
             "# Colleagues confirm an entry before it moves into its market's list.\n")
LOCAL_BEGIN, LOCAL_END = "# Sweep draft (begin)", "# Sweep draft (end)"
LOCAL_ANCHOR = "\n\n# The plan printout\n"


class Brand24Header(Exception):
    """The export's header is not the one this sweep reads; only the header is reported."""


class DraftChangesPlan(Exception):
    """A draft would change the collect plan's totals."""


class DraftEdited(Exception):
    """The draft on disk is not the one the sweep last wrote: someone edited it by hand."""


# Records ----------------------------------------------------------------------------------------------------

def norm_platform(platform):
    platform = str(platform or "").strip().lower()
    return "x" if platform == "twitter" else platform


def clean_handle(raw):
    """A handle as a list entry takes it (no @, a URL's first path part), or None when it is not one."""
    text = str(raw or "").strip()
    if "://" in text:
        parts = [p for p in urlsplit(text).path.split("/") if p]
        text = parts[0] if parts else ""
    text = text.lstrip("@")
    if not HANDLE.fullmatch(text) or "--" in text:
        return None
    return text


def classify(handle, name=None, platform=None):
    text = f"{handle} {name or ''}".lower()
    for kind, words in KIND_WORDS:
        if any(w in text for w in words):
            return kind
    return "creator" if norm_platform(platform) in CREATOR_PLATFORMS and not str(handle).isdigit() else "other"


def _iso(value):
    if value in (None, ""):
        return None
    return value.isoformat()[:10] if isinstance(value, (date, datetime)) else str(value)[:10]


def record(handle, platform, market, *, source, why, kind=None, name=None, followers=None, last_post=None,
           days_seen=None, engagement=None, posts=None, sightings=1, total_sightings=None, origin=None,
           seen_until=None, in_use=False, verified=None, tier=None):
    platform = norm_platform(platform)
    handle = str(handle).strip().lstrip("@")
    return {"handle": handle, "platform": platform, "market": market, "kind": kind or classify(handle, name, platform),
            "followers": followers, "last_post": _iso(last_post), "source": source, "why": why, "name": name,
            "in_use": in_use, "days_seen": days_seen, "engagement": engagement, "posts": posts,
            "sightings": sightings, "total_sightings": total_sightings, "origin": origin or source,
            "seen_until": _iso(seen_until), "verified": verified, "tier": tier}


def tier_rank(tier):
    return TIER_RANK.get(tier, len(TIER_RANK))


def rule_one(*texts):
    for text in texts:
        if text and (blocked(str(text)) or any(w in str(text).lower() for w in HUBS_REFUSED)):
            return True
    return False


def drop_reason(r, as_of):
    """rule_1, bad_handle, not_a_page, religious, government or dead; None when the record stays."""
    if rule_one(r["handle"], r.get("name"), r.get("why")):
        return "rule_1"
    if r["platform"] != "rss" and clean_handle(r["handle"]) is None:
        return "bad_handle"
    if r["platform"] == "facebook" and r["handle"].lower() in FACEBOOK_NOT_PAGES:
        return "not_a_page"
    parts = [str(p).lower() for p in (r["handle"], r.get("name")) if p]
    if any(RELIGIOUS.search(p) for p in parts):
        return "religious"
    if any(GOVERNMENT.search(p) for p in parts):
        return "government"
    if r.get("last_post"):
        looked = min(date.fromisoformat(r["seen_until"]), as_of) if r.get("seen_until") else as_of
        if date.fromisoformat(r["last_post"]) < looked - timedelta(days=DEAD_DAYS):
            return "dead"
    return None


def rank(records):
    """rank 1 is the best per source, market and platform: most days seen, then most engagement."""
    groups = defaultdict(list)
    for r in records:
        groups[(r["source"], r["market"], r["platform"])].append(r)
    for group in groups.values():
        group.sort(key=lambda r: (-(r["days_seen"] or 0), -(r["engagement"] or 0), -(r["followers"] or 0),
                                  r["handle"].lower()))
        for n, r in enumerate(group, 1):
            r["rank"] = n


def apply_rules(records, as_of, in_use=None):
    """(kept, drops): the market rule, then the drop reasons, then in-use marks and ranks.

    An account's sightings are summed over every origin (table or route) that saw it; per origin the total is
    its total_sightings when the read gave one (it saw markets beyond this read's rows), else the rows' own sum.
    A market keeps the account only with a strict majority of the total."""
    in_use = in_use_keys() if in_use is None else in_use
    groups = defaultdict(list)
    for r in records:
        groups[(r["platform"], r["handle"].lower())].append(r)
    kept, drops = [], Counter()
    for group in groups.values():
        origins = defaultdict(list)
        for r in group:
            origins[r["origin"]].append(r)
        total = sum(max(max(r["total_sightings"] or 0 for r in rs), sum(r["sightings"] for r in rs))
                    for rs in origins.values())
        per_market = Counter()
        for r in group:
            per_market[r["market"]] += r["sightings"]
        for r in group:
            if r["market"] not in MARKETS or per_market[r["market"]] * 2 <= total:
                drops["foreign"] += 1
                continue
            reason = drop_reason(r, as_of)
            if reason:
                drops[reason] += 1
                continue
            r["in_use"] = r["in_use"] or (r["platform"], r["handle"].lower(), r["market"]) in in_use
            kept.append(r)
    rank(kept)
    return kept, drops


def in_use_keys():
    """(platform, handle lowercased, market) -> where the collect job reads it today."""
    hubs = yaml.safe_load(HUBS.read_text(encoding="utf-8"))["markets"]
    markets = yaml.safe_load(MARKETS_YAML.read_text(encoding="utf-8"))["markets"]
    keys = {}
    for m in MARKETS:
        block = hubs[m.lower()]
        for e in block.get("x") or []:
            keys[("x", str(e["handle"]).lower(), m)] = "hubs.yaml x"
        for e in block.get("culture_desk") or []:
            keys[(norm_platform(e["platform"]), str(e["handle"]).lower(), m)] = "hubs.yaml culture_desk"
        for page in markets[m.lower()]["facebook_pages"]["values"][:job.FB_PAGES]:
            keys[("facebook", str(page).lower(), m)] = "markets.yaml facebook_pages (row 8)"
        for handle in local_sources.IG_GOSSIP[m]:
            keys[("instagram", handle.lower(), m)] = "local_sources.py IG_GOSSIP"
    for f in local_sources.FEEDS:
        keys[("rss", f.url.lower(), f.market)] = "local_sources.py FEEDS"
    return keys


def _short(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 10_000:
        return f"{round(n / 1000)}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


# Source 1: the warehouse --------------------------------------------------------------------------------------

def _tail(followers, join):
    return f""",
agg AS (
  SELECT s.market, s.platform, s.handle, COUNT(DISTINCT s.seen_day) days_seen, COUNT(DISTINCT s.post_id) posts,
    MAX(s.post_day) last_post, STRING_AGG(DISTINCT s.via, '+' ORDER BY s.via) via
  FROM sightings s
  WHERE IFNULL(s.handle, '') != ''
  GROUP BY s.market, s.platform, s.handle),
eng AS (
  SELECT market, platform, handle, CAST(ROUND(SUM(engagement)) AS INT64) engagement
  FROM (SELECT market, platform, handle, post_id, MAX(IFNULL(engagement, 0)) engagement FROM sightings
        WHERE IFNULL(handle, '') != '' GROUP BY market, platform, handle, post_id)
  GROUP BY market, platform, handle),
ranked AS (
  SELECT a.*, e.engagement, SUM(a.posts) OVER (PARTITION BY a.platform, a.handle) total_posts
  FROM agg a JOIN eng e USING (market, platform, handle))
SELECT r.market, r.platform, r.handle, r.days_seen, r.posts, r.total_posts, r.engagement, r.last_post, r.via,
  {followers} followers
FROM ranked r{join}
WHERE r.total_posts >= @min_posts
QUALIFY ROW_NUMBER() OVER (PARTITION BY r.market, r.platform ORDER BY r.days_seen DESC, r.engagement DESC,
                                                                       r.handle) <= @top
ORDER BY r.market, r.platform, r.days_seen DESC, r.engagement DESC, r.handle"""


SIGHTINGS = f""",
sightings AS (
  SELECT market, seen_day, post_id, platform, author handle, 'author' via, engagement, post_day FROM seen
  UNION ALL
  SELECT market, seen_day, post_id, platform, RTRIM(mention, '.'), 'mention', engagement, CAST(NULL AS DATE)
  FROM seen, UNNEST(REGEXP_EXTRACT_ALL(LOWER(text), r'{MENTION}')) mention
  WHERE RTRIM(mention, '.') != author)"""

CORE_SQL = f"""WITH seen AS (
  SELECT o.market, o.observed_date seen_day, p.post_id,
    IF(LOWER(p.platform) = 'twitter', 'x', LOWER(p.platform)) platform,
    LOWER(LTRIM(IFNULL(p.creator_id, ''), '@')) author, IFNULL(p.text, '') text, p.engagement,
    DATE(p.published_at) post_day
  FROM `{CORE}.post_observations` o
  JOIN `{CORE}.posts` p ON p.post_id = o.post_id
  WHERE o.observed_date BETWEEN @since AND @until
    AND p.post_date BETWEEN DATE_SUB(@since, INTERVAL {POST_LOOKBACK_DAYS} DAY) AND @until
    AND o.market IN UNNEST(@markets)
    AND LOWER(p.platform) IN UNNEST(@platforms))""" + SIGHTINGS + _tail("c.followers", f"""
LEFT JOIN (
  SELECT IF(LOWER(platform) = 'twitter', 'x', LOWER(platform)) platform, LOWER(LTRIM(handle, '@')) handle,
    MAX(followers) followers
  FROM `{CORE}.creators` WHERE handle IS NOT NULL GROUP BY 1, 2) c
ON c.platform = r.platform AND c.handle = r.handle""")


def legacy_content_sql(table):
    return f"""WITH seen AS (
  SELECT UPPER(e.market) market, DATE(e.collected_at) seen_day, CAST(e.id AS STRING) post_id,
    IF(LOWER(e.platform) = 'twitter', 'x', LOWER(e.platform)) platform,
    LOWER(LTRIM(IFNULL(e.author_handle_norm, ''), '@')) author,
    CONCAT(IFNULL(e.title, ''), ' ', IFNULL(e.text, '')) text, e.engagement_total engagement,
    DATE(e.published_at) post_day
  FROM `{PROJECT}.{table}` e
  WHERE DATE(e.collected_at) BETWEEN @since AND @until
    AND UPPER(e.market) IN UNNEST(@markets)
    AND LOWER(e.platform) IN UNNEST(@platforms)
    AND LOWER(e.source) IN UNNEST(@sources)
    AND NOT (LOWER(e.source) = 'brand24' AND LOWER(IFNULL(e.content_type, '')) IN ('link', 'author', 'aggregate')))""" \
        + SIGHTINGS + _tail("CAST(NULL AS INT64)", "")


def legacy_seed_sql(table):
    return f"""WITH daily AS (
  SELECT UPPER(market) market, IF(LOWER(platform) = 'twitter', 'x', LOWER(platform)) platform,
    LOWER(LTRIM(term, '@')) handle, trend_date, MAX(row_count) posts
  FROM `{PROJECT}.{table}`
  WHERE trend_date BETWEEN @since AND @until AND UPPER(market) IN UNNEST(@markets) AND term_type = 'handle'
    AND LOWER(platform) IN UNNEST(@platforms)
  GROUP BY 1, 2, 3, 4),
agg AS (
  SELECT market, platform, handle, COUNT(*) days_seen, CAST(SUM(posts) AS INT64) posts
  FROM daily WHERE IFNULL(handle, '') != '' GROUP BY 1, 2, 3),
ranked AS (
  SELECT a.*, 0 engagement, SUM(a.posts) OVER (PARTITION BY a.platform, a.handle) total_posts FROM agg a)
SELECT r.market, r.platform, r.handle, r.days_seen, r.posts, r.total_posts, r.engagement,
  CAST(NULL AS DATE) last_post, 'term' via, CAST(NULL AS INT64) followers
FROM ranked r
WHERE r.total_posts >= @min_posts
QUALIFY ROW_NUMBER() OVER (PARTITION BY r.market, r.platform ORDER BY r.days_seen DESC, r.posts DESC,
                                                                       r.handle) <= @top
ORDER BY r.market, r.platform, r.days_seen DESC, r.posts DESC, r.handle"""


def warehouse_queries(since, until, legacy_since=LEGACY_SINCE, legacy_until=LEGACY_UNTIL):
    """[(origin, sql, params)]: 42's own tables first, then each legacy table in data-inventory.md that holds
    social authors or handle terms."""
    base = {"markets": list(MARKETS), "min_posts": MIN_POSTS, "top": TOP_PER_PLATFORM}
    legacy = {**base, "since": legacy_since, "until": legacy_until, "platforms": list(LEGACY_PLATFORMS)}
    out = [(CORE_ORIGIN, CORE_SQL, {**base, "since": since, "until": until, "platforms": list(CORE_PLATFORMS)})]
    out += [(t, legacy_content_sql(t), {**legacy, "sources": list(LEGACY_SOURCES)}) for t in LEGACY_CONTENT]
    out += [(t, legacy_seed_sql(t), dict(legacy)) for t in LEGACY_SEEDS]
    return out


def warehouse_records(origin, rows, window_days, seen_until):
    where = "feeds" if origin == CORE_ORIGIN else "legacy feeds"
    out = []
    for row in rows:
        market, via = str(row["market"]).upper(), str(row.get("via") or "author")
        what = "handle term" if via == "term" else via.replace("+", " and ")
        why = (f"seen {row['days_seen']} of {window_days} days in {market} {where}, "
               f"{_short(row.get('engagement'))} engagements, {row['posts']} posts as {what} ({origin})")
        out.append(record(row["handle"], row["platform"], market, source="warehouse", why=why,
                          followers=row.get("followers"), last_post=row.get("last_post"),
                          days_seen=row["days_seen"], engagement=row.get("engagement") or 0, posts=row["posts"],
                          sightings=row["posts"], total_sightings=row.get("total_posts"), origin=origin,
                          seen_until=seen_until))
    return out


def from_warehouse(query, since, until, *, as_of, in_use=None, legacy_since=LEGACY_SINCE,
                   legacy_until=LEGACY_UNTIL):
    """(kept, drops, notes). query(sql, params) -> rows; a read it refuses or fails is a note, not a stop."""
    records, notes = [], []
    for origin, sql, params in warehouse_queries(since, until, legacy_since, legacy_until):
        try:
            rows = query(sql, params)
        except OverCap as error:
            notes.append(f"{origin}: refused, {error}")
            continue
        except Exception as error:  # a legacy table without a column the read names: report it, read the rest
            notes.append(f"{origin}: failed, {type(error).__name__}: {str(error)[:200]}")
            continue
        start, end = (since, until) if origin == CORE_ORIGIN else (legacy_since, legacy_until)
        records += warehouse_records(origin, rows, (end - start).days + 1, end)
    kept, drops = apply_rules(records, as_of, in_use)
    return kept, drops, notes


def _param(name, value):
    from google.cloud import bigquery

    if isinstance(value, (list, tuple)):
        return bigquery.ArrayQueryParameter(name, "STRING", list(value))
    kind = "DATE" if isinstance(value, date) else "INT64" if isinstance(value, int) else "STRING"
    return bigquery.ScalarQueryParameter(name, kind, value)


def bq_query(client):
    """query(sql, params) -> rows, each read dry-run first and capped by gdelt._checked."""
    def query(sql, params):
        return [dict(row) for row in _checked(client, sql, [_param(k, v) for k, v in params.items()]).result()]
    return query


# Source 2: the configs ----------------------------------------------------------------------------------------

def _rel(path):
    return path.relative_to(ROOT).as_posix()


def config_entries():
    """(platform, raw handle, market, file, detail, extra) for every account, page and feed the configs name."""
    out = []
    hubs = yaml.safe_load(HUBS.read_text(encoding="utf-8"))["markets"]
    markets = yaml.safe_load(MARKETS_YAML.read_text(encoding="utf-8"))["markets"]
    sources = yaml.safe_load((ENGINE / "sources.yaml").read_text(encoding="utf-8"))
    youtube = yaml.safe_load((ENGINE / "creators_youtube_handles.yaml").read_text(encoding="utf-8"))
    page = {"tier": "page"}
    for m in MARKETS:
        low = m.lower()
        for name in ("x", "culture_desk"):
            for e in hubs[low].get(name) or []:
                platform = "x" if name == "x" else e["platform"]
                out.append((platform, e["handle"], m, _rel(HUBS), f"hubs.yaml {name}",
                            {"kind": HUB_KINDS.get(e.get("kind")), "tier": "page" if name == "x" else None}))
        for fb in markets[low]["facebook_pages"]["values"]:
            out.append(("facebook", str(fb), m, _rel(MARKETS_YAML), "facebook_pages", page))
        for handle in local_sources.IG_GOSSIP[m]:
            out.append(("instagram", handle, m, _rel(LOCAL), "IG_GOSSIP", {"kind": "gossip_entertainment", **page}))
        creators = ENGINE / "creators" / f"{low}.yaml"
        for platform, tiers in (yaml.safe_load(creators.read_text(encoding="utf-8")).get("watchlists") or {}).items():
            for tier, handles in (tiers or {}).items():
                for handle in handles or []:
                    out.append((platform, handle, m, _rel(creators), f"{platform} {tier}", {"tier": tier}))
        registry = youtube.get(low) or {}
        channels = [(t, e) for t, es in (registry.get("active_handles") or {}).items() for e in es or []]
        channels += [("dormant", e) for e in registry.get("dormant_handles") or []]
        for tier, e in channels:
            out.append(("youtube", e["handle"], m, _rel(ENGINE / "creators_youtube_handles.yaml"), f"YouTube {tier}",
                        {"name": e.get("name"), "followers": e.get("subscribers"), "last_post": e.get("last_upload"),
                         "seen_until": YOUTUBE_PROBED, "tier": tier}))
        engine = _rel(ENGINE / "sources.yaml")
        crawl = sources["socialcrawl"]["markets"][low]
        for handle in crawl.get("twitter_handles") or []:
            out.append(("x", handle, m, engine, "socialcrawl twitter_handles", page))
        for fb in crawl.get("facebook_pages") or []:
            out.append(("facebook", str(fb), m, engine, "socialcrawl facebook_pages", page))
        for handle in sources["ensembledata"][low].get("twitter_handles") or []:
            out.append(("x", handle, m, engine, "ensembledata twitter_handles", page))
        for handle in sources["youtube_queries"][low].get("playlist_creator_handles") or []:
            out.append(("youtube", handle, m, engine, "playlist_creator_handles", {}))
        for feed in sources["rss_feeds"].get(low) or []:
            out.append(("rss", feed["url"], m, engine, "rss_feeds", {"name": feed.get("name"), "kind": "news", **page}))
    for f in local_sources.FEEDS:
        out.append(("rss", f.url, f.market, _rel(LOCAL), "FEEDS", {"name": f.source, "kind": "news", **page}))
    return out


def from_configs(*, as_of, in_use=None):
    """(kept, drops): one record per platform, handle and market the configs name, with every place it sits."""
    in_use = in_use_keys() if in_use is None else in_use
    merged = {}
    for platform, raw, market, place, detail, extra in config_entries():
        platform = norm_platform(platform)
        handle = str(raw).strip() if platform == "rss" else str(raw).strip().lstrip("@")
        key = (platform, handle.lower(), market)
        if key not in merged:
            merged[key] = {"handle": handle, "platform": platform, "market": market, "places": [], "extra": {}}
        entry = merged[key]
        entry["places"].append(f"{place} ({detail})")
        for name, value in extra.items():
            old = entry["extra"].get(name)
            if value is not None and (old is None or name == "tier" and tier_rank(value) < tier_rank(old)):
                entry["extra"][name] = value
    records = []
    for key, e in merged.items():
        e["places"] = list(dict.fromkeys(e["places"]))
        places = e["places"][:3] + ([f"{len(e['places']) - 3} more"] if len(e["places"]) > 3 else [])
        where = in_use.get(key)
        why = f"listed in {', '.join(places)}; " + (f"in use in {where}" if where else "not in use")
        records.append(record(e["handle"], e["platform"], e["market"], source="configs", why=why,
                              in_use=bool(where), **e["extra"]))
    return apply_rules(records, as_of, in_use)


# Source 3: the Brand24 export ---------------------------------------------------------------------------------

def open_brand24(path):
    return open(path, encoding="utf-8", newline="")


def _page_handle(url):
    parts = urlsplit(str(url or "").strip())
    path = [p for p in parts.path.split("/") if p]
    if not parts.netloc.lower().endswith("facebook.com") or not path or path[0].lower() in FACEBOOK_NOT_PAGES:
        return None
    return clean_handle(path[0])


def person_shaped(handle):
    h = str(handle).lower()
    letters = sum(ch.isalpha() for ch in h)
    if not re.fullmatch(r"[a-z0-9_]+", h) or not 0 < letters < PERSON_LETTERS:
        return False
    return classify(h) in ("creator", "other") and not any(w in h for w in PAGE_WORDS + PLACE_WORDS)


def from_brand24(stream, *, as_of, in_use=None):
    """(kept, drops) from the export's rows. Multi-market pages have no per-market counts, so no majority."""
    reader = csv.DictReader(stream)
    header = tuple(reader.fieldnames or ())
    if header != BRAND24_COLUMNS:
        raise Brand24Header("unexpected Brand24 header: " + ",".join(header))
    rows = list(reader)
    end = max(date.fromisoformat(r["last_seen"]) for r in rows)   # the export saw nothing after this day
    records, drops = [], Counter()
    for r in rows:
        markets = [m.strip().upper() for m in r["markets"].split(",") if m.strip()]
        if len(markets) != 1 or markets[0] not in MARKETS:
            drops["foreign"] += 1
            continue
        n, first, last = int(r["rows_"]), date.fromisoformat(r["first_seen"]), date.fromisoformat(r["last_seen"])
        if n < BRAND24_MIN_ROWS and not (first <= BRAND24_MONTH[1] and last >= BRAND24_MONTH[0]):
            drops["below_threshold"] += 1
            continue
        handle = _page_handle(r["sample_url"]) or r["page_slug"].strip()
        why = f"Brand24 tag {markets[0]}, {n} rows from {first} to {last}"
        records.append(record(handle, "facebook", markets[0], source="brand24", why=why, last_post=last,
                              posts=n, sightings=n, seen_until=end, tier="page"))
    kept, more = apply_rules(records, as_of, in_use)
    return kept, drops + more


# Source 4: discovery ------------------------------------------------------------------------------------------

TERMS_SQL = f"""SELECT d.market, m.label, m.kind, SUM(IFNULL(d.posts, 0)) posts
FROM `{CORE}.item_daily` d
JOIN `{CORE}.cultural_map` m ON m.item_id = d.item_id AND m.valid_to IS NULL
WHERE d.metric_date BETWEEN @since AND @until AND d.market IN UNNEST(@markets) AND d.lane_class != 'legacy'
  AND m.kind IN UNNEST(@kinds) AND IFNULL(m.status, 'active') = 'active' AND IFNULL(m.label, '') != ''
GROUP BY d.market, m.label, m.kind
QUALIFY ROW_NUMBER() OVER (PARTITION BY d.market ORDER BY SUM(IFNULL(d.posts, 0)) DESC, m.label) <= @top"""

RAW_SQL = f"""SELECT route, market, seed_key, params_hash, fetched_at, TO_JSON_STRING(body) body
FROM `{CORE}.raw_responses`
WHERE DATE(fetched_at) BETWEEN @since AND @until AND run_id = @run_id AND http_status = 200
  AND route IN UNNEST(@routes)"""


def terms_params(until):
    return {"since": WAREHOUSE_SINCE, "until": until, "markets": list(MARKETS), "kinds": list(TERM_KINDS),
            "top": TERMS_READ}


def top_terms(rows):
    """market -> up to TERMS_PER_MARKET search terms, most posts first; rule 1 and the generic tags refused."""
    out, seen = {m: [] for m in MARKETS}, {m: set() for m in MARKETS}
    for row in sorted(rows, key=lambda r: (-(r.get("posts") or 0), str(r.get("label")))):
        market = str(row.get("market") or "").upper()
        term = str(row.get("label") or "").strip().lstrip("#").strip().casefold()
        if market not in out or not term or term in job.GENERIC_TAGS or term in seen[market]:
            continue
        if len(out[market]) >= TERMS_PER_MARKET or rule_one(term):
            continue
        seen[market].add(term)
        out[market].append(term)
    return out


def discover_calls(terms, run_date):
    """Two searches per term, markets interleaved so a stop leaves each market some calls."""
    since = (run_date - timedelta(days=7)).isoformat()
    calls = []
    for i in range(TERMS_PER_MARKET):
        for market in MARKETS:
            listed = terms.get(market) or []
            if i >= len(listed):
                continue
            term = listed[i]
            for route, params in (("tiktok/search/top", {"query": term, "country": market, "publish_time": "this-week"}),
                                  ("search/multi", {"query": term, "platforms": DISCOVER_PLATFORMS, "since": since})):
                method = PRICED[route].method
                calls.append({"route": route, "method": method, "params": params, "market": market, "term": term,
                              "hold": quote_for(route, method, params), "phash": params_hash(method, params)})
    return calls


def cap_calls(calls, cap):
    """(made, held): calls in order while their holds fit the cap; the first that does not, and all after it, held."""
    total = 0
    for i, call in enumerate(calls):
        if total + call["hold"] > cap:
            return calls[:i], calls[i:]
        total += call["hold"]
    return list(calls), []


def run_discover(client, calls, cap, out=sys.stdout):
    """(results, spent, stopped): results are (call, Result). Stops before any call whose hold would take the
    charges past cap, and after any client stop."""
    results, spent, stopped = [], 0, ""
    for call in calls:
        if spent + call["hold"] > cap:
            stopped = (f"stopped before {call['route']} {call['market']} {call['term']!r}: {spent:g} charged plus "
                       f"{call['hold']} held is over the cap of {cap}")
            break
        result = client.call(call["route"], dict(call["params"]), method=call["method"], market=call["market"],
                             seed_key=call["term"], lane=None)
        spent += result.credits_charged or 0
        results.append((call, result))
        print(f"{call['route']} {call['market']} {call['term']!r} {result.status} "
              f"charged={result.credits_charged or 0:g} items={len(result.items)}", file=out)
        if result.status in STOP:
            stopped = f"stopped after {call['route']} {call['market']} {call['term']!r}: {result.status}"
            break
    if stopped:
        print(stopped, file=out)
    print(f"done: {len(results)} calls, {spent:g} credits charged, cap {cap}", file=out)
    return results, spent, stopped


def discover_records(results, fetched=None, *, item_id_fn, geo_fn, as_of, in_use=None):
    """(kept, drops) from (call, body) or (call, body, fetched_at) pairs. Each post is parsed as the collect job
    parses it; its author's market evidence is the post's located market, and a post with none counts only in
    the total. An author with no located post at all is a foreign drop."""
    from core.collect.parse import parse

    geo = job.safe_geo(geo_fn)
    found = {}
    for item in results:
        call, body = item[0], item[1]
        when = item[2] if len(item) > 2 else fetched
        when = datetime.fromisoformat(str(when)) if not isinstance(when, datetime) else when
        parsed = parse(call["route"], call["params"], call["market"], body, when, "sources-sweep",
                       item_id_fn=item_id_fn, geo_fn=geo, lane="exploration", seed_key=call["term"])
        for post in parsed["posts"]:
            handle = clean_handle(post.get("creator_id"))
            if not handle:
                continue
            key = (norm_platform(post["platform"]), handle.lower())
            entry = found.setdefault(key, {"handle": handle, "posts": {}, "how": set()})
            entry["how"].add((call["route"], call["term"], call["market"]))
            entry["posts"][post["post_id"]] = post
    records, drops = [], Counter()
    for (platform, _), entry in found.items():
        posts = list(entry["posts"].values())
        located = Counter(p["geo_market"] for p in posts if p.get("geo_market") in MARKETS)
        if not located:
            drops["foreign"] += 1
            continue
        routes = sorted({route for route, _, _ in entry["how"]})
        for market, n in located.items():
            mine = [p for p in posts if p.get("geo_market") == market]
            terms = sorted({t for _, t, m in entry["how"] if m == market}) or sorted({t for _, t, _ in entry["how"]})
            engagement = sum(p.get("engagement") or 0 for p in mine)
            last = max((p.get("published_at") or "")[:10] for p in mine) or None
            why = (f"found by {' and '.join(routes)} for {', '.join(repr(t) for t in terms[:3])} in {market}: "
                   f"{n} of {len(posts)} posts located in {market}, {_short(engagement)} engagements")
            records.append(record(entry["handle"], platform, market, source="discover", why=why, last_post=last,
                                  engagement=engagement, posts=n, sightings=n, total_sightings=len(posts),
                                  origin="discover"))
    kept, more = apply_rules(records, as_of, in_use)
    return kept, drops + more


def match_run_rows(rows, run_date):
    """([(call, body, fetched_at)], unmatched): raw_responses rows of a discover run, their params rebuilt from
    seed_key (the term), market and run_date, and kept only when the rebuilt params hash to the stored hash."""
    terms = defaultdict(list)
    for row in rows:
        market, term = str(row["market"]).upper(), row.get("seed_key")
        if term and term not in terms[market]:
            terms[market].append(term)
    calls = {(c["route"], c["phash"]): c for c in discover_calls(dict(terms), run_date)}
    matched, unmatched = [], 0
    for row in rows:
        call = calls.get((row["route"], row["params_hash"]))
        if call is None:
            unmatched += 1
            continue
        body = json.loads(row["body"]) if isinstance(row["body"], str) else row["body"]
        matched.append((call, body, row["fetched_at"]))
    return matched, unmatched


def live_client(run_id):
    from core.collect.probe import live_client as probe_client

    return probe_client(run_id, schedule_started=True)


# The candidates file ------------------------------------------------------------------------------------------

def merge_lines(lines, source, records):
    """The file's lines with this source's lines replaced by records."""
    kept = [line for line in lines if line.strip() and json.loads(line).get("source") != source]
    return kept + [json.dumps(r, ensure_ascii=False, default=str) for r in records]


def write_candidates(path, source, records):
    path = Path(path)
    old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(merge_lines(old, source, records)) + "\n", encoding="utf-8")


def read_candidates(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# The proposal -------------------------------------------------------------------------------------------------

def morning_totals(day):
    """(SocialCrawl phases, local sources): the holds core.collect.job --plan prints for day, less the row 14i reel
    searches, which take only the room every other call leaves, so an entry added here takes its credits from them."""
    return job.plan(day, reels={})["total"], local_sources.total_hold(local_sources.plan(day))


def budget_for(day):
    """The panels are paid from the collect share, so new entries get what it has left after the morning plan."""
    plan_total, local = morning_totals(day)
    collect = load_caps()["ENGINE_DAILY"]["collect"]
    left = collect - plan_total - local
    return {"budget": left, "headroom": left,
            "basis": f"collect share {collect} minus the morning plan {plan_total + local} "
                     f"(SocialCrawl phases {plan_total}, local sources {local})"}


def panel_prices(day):
    """Credits one more entry adds to each panel a morning, from PRICED."""
    since = (day - timedelta(days=1)).isoformat()
    x = quote_for("twitter/user/tweets", "GET", {"handle": "h", "since": since})
    desk = quote_for("prism/profiles", "POST",
                     {"items": [{"platform": "instagram", "handle": "h"}], "include": "posts", "since": since})
    page = quote_for("facebook/profile/posts", "GET", {"pageId": "1", "since": since})
    return {"x": x, "culture_desk": desk, "facebook": page, "ig_gossip": desk, "feeds": 0}


def current_sizes():
    hubs = yaml.safe_load(HUBS.read_text(encoding="utf-8"))["markets"]
    markets = yaml.safe_load(MARKETS_YAML.read_text(encoding="utf-8"))["markets"]
    return {m: {"x": len(hubs[m.lower()].get("x") or []),
                "culture_desk": len(hubs[m.lower()].get("culture_desk") or []),
                "facebook": min(job.FB_PAGES, len(markets[m.lower()]["facebook_pages"]["values"])),
                "ig_gossip": len(local_sources.IG_GOSSIP[m]),
                "feeds": sum(f.market == m for f in local_sources.FEEDS)} for m in MARKETS}


def panel_for(c):
    platform = c["platform"]
    if platform in ("x", "facebook"):
        return platform
    if platform == "rss":
        return "feeds"
    if platform == "instagram" and c["kind"] == "gossip_entertainment":
        return "ig_gossip"
    return "culture_desk" if platform in ("instagram", "tiktok", "youtube") else None


def merge_candidates(records):
    """One candidate per platform, handle and market, holding every source that found it."""
    groups = defaultdict(list)
    for r in records:
        groups[(r["platform"], r["handle"].lower(), r["market"])].append(r)
    out = []
    for group in groups.values():
        group.sort(key=lambda r: SOURCE_ORDER.index(r["source"]) if r["source"] in SOURCE_ORDER else 9)
        kinds = [r["kind"] for r in group]
        kind = next((k for k in kinds if k not in ("creator", "other")), None) or \
            ("creator" if "creator" in kinds else kinds[0])
        whys = list(dict.fromkeys(r["why"] for r in group))

        def best(name, fn=max):
            values = [r.get(name) for r in group if r.get(name) is not None]
            return fn(values) if values else None

        out.append({**group[0], "kind": kind, "sources": sorted({r["source"] for r in group}),
                    "in_use": any(r.get("in_use") for r in group), "days_seen": best("days_seen"),
                    "engagement": best("engagement"), "followers": best("followers"),
                    "last_post": best("last_post"), "verified": any(r.get("verified") for r in group),
                    "tier": min((r.get("tier") for r in group), key=tier_rank), "why": "; ".join(whys[:2]),
                    "brand24_rows": max((r.get("posts") or 0 for r in group if r["source"] == "brand24"),
                                        default=None)})
    return out


def _score(c):
    """Kind group, tier, sources, days seen, engagement, followers, then fewest characters that are not letters,
    so a tie never puts a fan account's underscores first. A full tie keeps the candidates file's order."""
    group = 0 if c["kind"] in CULTURE_KINDS else 2 if c["kind"] == "brand" else 1
    return (group, tier_rank(c.get("tier")), -len(c["sources"]), -(c.get("days_seen") or 0),
            -(c.get("engagement") or 0), -(c.get("followers") or 0), sum(not ch.isalpha() for ch in c["handle"]))


def propose(records, budget, current, prices):
    """Per market and panel, the candidates to add. Free panels take their best up to PANEL_LIMIT. Paid ones fill
    one entry at a time: the market that has been given the fewest credits so far, then its panel given the
    fewest, while the next entry's price still fits the budget and the panel is under PANEL_LIMIT."""
    queues, held = {(m, p): [] for m in MARKETS for p in PANELS}, Counter()
    for c in merge_candidates(records):
        panel = panel_for(c)
        if c["in_use"] or c["market"] not in MARKETS or not panel:
            continue
        reason = draft_hold(c)
        if reason:
            held[reason] += 1
            continue
        queues[(c["market"], panel)].append(c)
    for queue in queues.values():
        queue.sort(key=_score)
    added = {m: {p: [] for p in PANELS} for m in MARKETS}
    for (m, p), queue in queues.items():
        if prices[p] == 0:
            added[m][p] = queue[:max(0, PANEL_LIMIT[p] - current[m][p])]
    spent_market, spent_queue, used, total = Counter(), Counter(), Counter(), 0
    while True:
        options = []
        for mi, m in enumerate(MARKETS):
            for pi, p in enumerate(PANELS):
                k, price = (m, p), prices[p]
                if (price == 0 or used[k] >= len(queues[k]) or current[m][p] + used[k] >= PANEL_LIMIT[p]
                        or total + price > budget):
                    continue
                options.append((spent_market[m], spent_queue[k], mi, pi, k))
        if not options:
            break
        m, p = min(options)[-1]
        added[m][p].append(queues[(m, p)][used[(m, p)]])
        used[(m, p)] += 1
        total += prices[p]
        spent_market[m] += prices[p]
        spent_queue[(m, p)] += prices[p]
    return {"added": added, "credits": {m: spent_market[m] for m in MARKETS}, "total": total, "budget": budget,
            "current": current, "prices": prices, "held": held, "basis": f"a budget of {budget}", "headroom": None}


def draft_hold(c):
    """Why a candidate stays out of a draft, or None when it is a page, outlet or public creator."""
    if c.get("verified") or "configs" in c["sources"] and c.get("tier") != "tier_3":
        return None
    followers, floor = c.get("followers"), FOLLOWER_FLOOR.get(c["platform"])
    public = followers is not None and floor is not None and followers >= floor
    if "brand24" in c["sources"]:
        handle = c["handle"]
        if "." in handle or handle.lower().startswith("www"):
            return "brand24_not_a_page"
        if public:
            return None
        if handle.isdigit():
            return "brand24_numeric_id" if followers is None else "below_follower_floor"
        if (c.get("brand24_rows") or 0) < BRAND24_MIN_ROWS:
            return "brand24_few_rows"
        if person_shaped(handle):
            return "brand24_person_shaped" if followers is None else "below_follower_floor"
        return None
    if followers is None or floor is None:
        return "followers_unknown"
    return None if public else "below_follower_floor"


def print_proposal(proposal, day, out=sys.stdout):
    plan_total, local = morning_totals(day)
    caps = load_caps()["ENGINE_DAILY"]
    prices, current, added = proposal["prices"], proposal["current"], proposal["added"]
    print(f"morning plan for {day.isoformat()}: SocialCrawl phases {plan_total}, local sources {local}, "
          f"together {plan_total + local}", file=out)
    print(f"budget {proposal['budget']}: {proposal['basis']}", file=out)
    held = proposal.get("held") or {}
    if held:
        print(f"kept out of the draft (candidates file only): {json.dumps(dict(sorted(held.items())))}; floors "
              f"{json.dumps(FOLLOWER_FLOOR)}", file=out)
    print(f"{'market':6} {'panel':13} {'route':24} {'price':>5} {'now':>4} {'add':>4} {'new':>4} {'credits':>7}",
          file=out)
    for m in MARKETS:
        for p in PANELS:
            n = len(added[m][p])
            print(f"{m:6} {p:13} {PANEL_ROUTES[p]:24} {prices[p]:>5} {current[m][p]:>4} {n:>4} "
                  f"{current[m][p] + n:>4} {n * prices[p]:>7}", file=out)
    per_market = ", ".join(f"{m} {proposal['credits'][m]}" for m in MARKETS)
    print(f"added credits per morning: {proposal['total']} ({per_market})", file=out)
    morning = plan_total + local + proposal["total"]
    if morning > caps["collect"]:
        print(f"the panels are paid from the collect share ({caps['collect']}); the morning would hold {morning}, "
              f"{morning - caps['collect']} over it, so the lists need that share raised or fewer entries", file=out)
    gossip = sum(len(added[m]["ig_gossip"]) for m in MARKETS) * prices["ig_gossip"]
    if gossip and local + gossip > local_sources.LOCAL_DAILY_CAP:
        print(f"the gossip panel runs in the local phase: its sub-limit {local_sources.LOCAL_DAILY_CAP} would "
              f"need to become {local + gossip}", file=out)


# The draft ----------------------------------------------------------------------------------------------------

def _plain(text):
    return re.sub(r"\s+", " ", DASH_RUN.sub(" ", str(text or ""))).strip()


def _entry(c):
    out = {"handle": c["handle"], "platform": c["platform"], "kind": c["kind"], "source": "+".join(c["sources"]),
           "why": _plain(c["why"])}
    if c.get("followers") is not None:
        out["followers"] = int(c["followers"])
    if c.get("last_post"):
        out["last_post"] = c["last_post"]
    return out


def _digest(block):
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:16]


def _stamp(block):
    """block with a hash of itself on its second line, so a later write can tell a hand edit."""
    first, _, rest = block.partition("\n")
    return f"{first}\n{HASH_LINE}{_digest(block)}\n{rest}"


def _unedited(stamped):
    first, _, rest = stamped.partition("\n")
    line, _, body = rest.partition("\n")
    return line.startswith(HASH_LINE) and line[len(HASH_LINE):].strip() == _digest(f"{first}\n{body}")


def _over(proposal):
    """The warning line when the proposal adds more than the collect share has left, else None."""
    headroom = proposal.get("headroom")
    if headroom is None or proposal["total"] <= headroom:
        return None
    return (f"this proposal adds {proposal['total']} credits a morning, {proposal['total'] - headroom} over the "
            f"collect share left ({headroom}); raise the collect share or trim the lists before any entry moves up")


def render_hubs_draft(text, proposal, day, force=False):
    """hubs.yaml with its draft key (re)written at the end; everything above the draft mark is kept as it is.
    A draft that is not the one this sweep last wrote (its hash differs) is refused unless force."""
    cut = text.find(HUBS_MARK)
    base = text[:cut] if cut >= 0 else text
    if cut < 0 and re.search(r"(?m)^draft:", base):
        raise ValueError("hubs.yaml holds a draft key this sweep did not write")
    if cut >= 0 and not force and not _unedited(text[cut:]):
        raise DraftEdited("the hubs.yaml draft was edited since the sweep wrote it; pass --force-draft to replace it")
    added, warning = proposal["added"], _over(proposal)
    draft = {"written": day.isoformat(), "budget": proposal["budget"], "budget_basis": proposal["basis"],
             "added_credits": sum(len(added[m][p]) * proposal["prices"][p] for m in MARKETS for p in HUB_PANELS)}
    if warning:
        draft["warning"] = warning
    draft["markets"] = {m.lower(): {p: [_entry(c) for c in added[m][p]] for p in HUB_PANELS} for m in MARKETS}
    dumped = yaml.safe_dump({"draft": draft}, sort_keys=False, allow_unicode=True, width=110,
                            default_flow_style=False)
    block = HUBS_MARK + "\n" + HUBS_NOTE + (f"# WARNING: {warning}\n" if warning else "") + _plain_lines(dumped)
    return base.rstrip("\n") + "\n\n" + _stamp(block)


def _plain_lines(text):
    return "\n".join(DASH_RUN.sub(" ", line) for line in text.splitlines()) + "\n"


def _py(value):
    return json.dumps(value, ensure_ascii=False)


def render_local_draft(text, proposal, day, force=False):
    """local_sources.py with IG_GOSSIP_DRAFT and FEEDS_DRAFT (re)written just above its plan printout. A draft
    that is not the one this sweep last wrote (its hash differs) is refused unless force."""
    if LOCAL_BEGIN in text:
        begin, finish = text.index(LOCAL_BEGIN), text.index(LOCAL_END) + len(LOCAL_END)
        if not force and not _unedited(text[begin:finish]):
            raise DraftEdited("the local_sources.py draft was edited since the sweep wrote it; pass --force-draft "
                              "to replace it")
        text = text[:begin - 2] + text[finish + 1:]
    at = text.find(LOCAL_ANCHOR)
    if at < 0:
        raise ValueError("local_sources.py has no plan printout section to write the draft above")
    added = proposal["added"]
    lines = [f"{LOCAL_BEGIN}. Written by core/collect/sources_sweep.py in its write-draft mode on {day.isoformat()}.",
             "# plan() and the collect job never read IG_GOSSIP_DRAFT or FEEDS_DRAFT. Colleagues confirm an entry",
             "# before it moves into IG_GOSSIP or FEEDS, and LOCAL_DAILY_CAP grows with IG_GOSSIP.",
             "IG_GOSSIP_DRAFT = {"]
    for m in MARKETS:
        if not added[m]["ig_gossip"]:
            lines.append(f"    {_py(m)}: (),")
            continue
        lines.append(f"    {_py(m)}: (")
        for c in added[m]["ig_gossip"]:
            e = _entry(c)
            lines.append(f"        {{\"handle\": {_py(e['handle'])}, \"kind\": {_py(e['kind'])}, "
                         f"\"source\": {_py(e['source'])},")
            lines.append(f"         \"why\": {_py(e['why'])}}},")
        lines.append("    ),")
    lines += ["}", "FEEDS_DRAFT = ("]
    for m in MARKETS:
        for c in added[m]["feeds"]:
            name = c.get("name") or urlsplit(c["handle"]).netloc
            lines.append(f"    {{\"source\": {_py(name)}, \"market\": {_py(m)}, \"url\": {_py(c['handle'])},")
            lines.append(f"     \"why\": {_py(_plain(c['why']))}}},")
    lines += [")", LOCAL_END]
    return text[:at] + "\n\n" + _stamp("\n".join(lines)) + "\n" + text[at:]


def load_module(text, name="local_sources_draft_check"):
    """local_sources.py text as a module of its own, to read its plan without importing the file."""
    module = types.ModuleType(name)
    module.__file__ = str(LOCAL)
    sys.modules[name] = module
    try:
        exec(compile(text, str(LOCAL), "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def _totals(hubs_text, local_text, day):
    config = {"markets": job.load_config()["markets"], "hubs": yaml.safe_load(hubs_text)}
    module = load_module(local_text)
    return job.plan(day, config=config, reels={})["total"], module.total_hold(module.plan(day))


def check_draft(hubs, local, new_hubs, new_local, day):
    """(before, after) plan totals; DraftChangesPlan when the draft moves them."""
    before, after = _totals(hubs, local, day), _totals(new_hubs, new_local, day)
    if before != after:
        raise DraftChangesPlan(f"the draft moves the plan totals from {before} to {after}")
    return before, after


# CLI ----------------------------------------------------------------------------------------------------------

def _counts(label, kept, drops, out):
    by = Counter((r["market"], r["platform"]) for r in kept)
    used = sum(bool(r.get("in_use")) for r in kept)
    print(f"{label}: {len(kept)} kept ({used} in use), drops {json.dumps(dict(sorted(drops.items())))}", file=out)
    for (market, platform), n in sorted(by.items()):
        print(f"  {market} {platform:10} {n}", file=out)


def _print_sql(origin, sql, params, out):
    shown = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in params.items()}
    print(f"-- {origin}\n{sql}\n-- params {json.dumps(shown)}\n", file=out)


def main(argv=None, *, env=None, query=None, make_client=None, out=None):
    import os

    env = os.environ if env is None else env
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--from-warehouse", action="store_true", help="authors and mentions in BigQuery")
    mode.add_argument("--from-configs", action="store_true", help="engine/configs and the current lists")
    mode.add_argument("--from-brand24", nargs="?", const=str(BRAND24_CSV), metavar="PATH",
                      help="the Brand24 export of Facebook pages")
    mode.add_argument("--discover", action="store_true", help="paid discovery, inside a Cloud Run job only")
    mode.add_argument("--propose", action="store_true", help="list sizes per market and panel for the budget")
    mode.add_argument("--write-draft", action="store_true", help="write the proposal as drafts")
    parser.add_argument("--from-run", metavar="RUN_ID", help="with --discover: read that run's bodies back")
    parser.add_argument("--plan", action="store_true", help="print what would run; no network, no file written")
    parser.add_argument("--run-date", type=date.fromisoformat, help="YYYY-MM-DD; default today in SAST")
    parser.add_argument("--since", type=date.fromisoformat, default=WAREHOUSE_SINCE)
    parser.add_argument("--out", default=str(CANDIDATES), help="the candidates file")
    parser.add_argument("--budget", type=int,
                        help="credits a morning for new entries; default what the collect share has left")
    parser.add_argument("--force-draft", action="store_true", help="replace a draft that was edited by hand")
    args = parser.parse_args(argv)
    day = args.run_date or datetime.now(SAST).date()

    def bigquery_query():
        if query is not None:
            return query
        from google.cloud import bigquery

        return bq_query(bigquery.Client(project=PROJECT))

    if args.from_warehouse:
        if args.plan:
            for origin, sql, params in warehouse_queries(args.since, day):
                _print_sql(origin, sql, params, out)
            print("plan only: no query run, no file written", file=out)
            return 0
        kept, drops, notes = from_warehouse(bigquery_query(), args.since, day, as_of=day)
        for note in notes:
            print(note, file=out)
        _counts("warehouse", kept, drops, out)
        write_candidates(args.out, "warehouse", kept)
        print(f"wrote {args.out}", file=out)
        return 0

    if args.from_configs:
        kept, drops = from_configs(as_of=day)
        _counts("configs", kept, drops, out)
        if not args.plan:
            write_candidates(args.out, "configs", kept)
            print(f"wrote {args.out}", file=out)
        return 0

    if args.from_brand24 is not None:
        try:
            with open_brand24(args.from_brand24) as stream:
                kept, drops = from_brand24(stream, as_of=day)
        except Brand24Header as error:
            print(f"stopped: {error}", file=out)
            return 2
        _counts("brand24", kept, drops, out)
        if not args.plan:
            write_candidates(args.out, "brand24", kept)
            print(f"wrote {args.out}", file=out)
        return 0

    if args.discover and args.from_run:
        params = {"since": day - timedelta(days=1), "until": day + timedelta(days=1), "run_id": args.from_run,
                  "routes": list(DISCOVER_ROUTES)}
        if args.plan:
            _print_sql("raw_responses", RAW_SQL, params, out)
            print("plan only: no query run, no file written", file=out)
            return 0
        matched, unmatched = match_run_rows(bigquery_query()(RAW_SQL, params), day)
        item_id_fn, geo_fn = job.detect_fns()
        kept, drops = discover_records(matched, item_id_fn=item_id_fn, geo_fn=geo_fn, as_of=day)
        print(f"{len(matched)} bodies read, {unmatched} not from this sweep's calls", file=out)
        _counts("discover", kept, drops, out)
        write_candidates(args.out, "discover", kept)
        print(f"wrote {args.out}", file=out)
        return 0

    if args.discover:
        if args.plan:
            placeholders = {m: [f"<{m} term {i}>" for i in range(1, TERMS_PER_MARKET + 1)] for m in MARKETS}
            calls = discover_calls(placeholders, day)
            made, held = cap_calls(calls, DISCOVER_CAP)
            for c in calls:
                flag = "" if c in made else "  (held: past the cap)"
                print(f"{c['route']:18} {c['market']} {c['hold']:>3}  {json.dumps(c['params'])}{flag}", file=out)
            print(f"{len(calls)} calls, total hold {sum(c['hold'] for c in calls)} credits, cap {DISCOVER_CAP} "
                  f"on the build share; {len(held)} held. Terms: the top {TERMS_PER_MARKET} culture terms per "
                  f"market since {WAREHOUSE_SINCE.isoformat()}, rule 1 refused:", file=out)
            _print_sql("terms", TERMS_SQL, terms_params(day), out)
            return 0
        if not env.get("CLOUD_RUN_JOB"):
            print("refused: --discover spends SocialCrawl credits and runs only inside a Cloud Run job "
                  "(CLOUD_RUN_JOB is not set); --discover --plan shows its calls", file=out)
            return 2
        terms = top_terms(bigquery_query()(TERMS_SQL, terms_params(day)))
        calls = discover_calls(terms, day)
        run_id = "sweep-discover-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(f"run_id {run_id}; terms {json.dumps(terms)}", file=out)
        client = (make_client or live_client)(run_id)
        run_discover(client, calls, DISCOVER_CAP, out)
        print(f"read back with: --discover --from-run {run_id} --run-date {day.isoformat()}", file=out)
        return 0

    records = read_candidates(args.out)
    left = budget_for(day)
    budget = left["budget"] if args.budget is None else args.budget
    proposal = propose(records, budget, current_sizes(), panel_prices(day))
    proposal.update(headroom=left["headroom"],
                    basis=left["basis"] if args.budget is None else
                    f"--budget {args.budget}, against the {left['basis']}, which leaves {left['headroom']}")
    print(f"{len(records)} candidate records in {args.out}", file=out)
    print_proposal(proposal, day, out)
    if args.propose:
        return 0
    hubs, local = HUBS.read_text(encoding="utf-8"), LOCAL.read_text(encoding="utf-8")
    try:
        new_hubs = render_hubs_draft(hubs, proposal, day, force=args.force_draft)
        new_local = render_local_draft(local, proposal, day, force=args.force_draft)
    except DraftEdited as error:
        print(f"refused: {error}", file=out)
        return 2
    try:
        _, after = check_draft(hubs, local, new_hubs, new_local, day)
    except DraftChangesPlan as error:
        print(f"refused: {error}", file=out)
        return 2
    print(f"plan totals unchanged: {after[0]} and local {after[1]}", file=out)
    if args.plan:
        print("plan only: hubs.yaml and local_sources.py not written", file=out)
        return 0
    HUBS.write_text(new_hubs, encoding="utf-8")
    LOCAL.write_text(new_local, encoding="utf-8")
    print(f"wrote the draft into {HUBS.relative_to(ROOT).as_posix()} and {LOCAL.relative_to(ROOT).as_posix()}",
          file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
