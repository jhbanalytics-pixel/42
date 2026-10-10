"""Local sources, the collect side of BUILD.md task 2.12 (SOURCES.md, "Added on the panel's advice").

One extra phase of the 02:00 collect job, about 40 credits a day from the collect share:

- web/scrape at 1 credit a page, never with a proxy or pdf_parse: the Nairaland front page (NG), the
  Boomplay and Audiomack trending pages per market (location_country), Shazam's national charts and its
  Johannesburg and Lagos city charts, the TurnTable Top 100 (NG) and the kworb Spotify daily charts, less
  the pages DROPPED_SCRAPES retires (Nairaland and Audiomack since 3 Oct 2026, kworb KE);
- prism/profiles include=posts since yesterday: an Instagram gossip and blog panel per market, public
  pages chosen by interest (celebrity news, entertainment and music gossip), 2 credits a page;
- google_play/app-list, the top free apps, one market a day on Monday (ZA), Tuesday (NG) and Wednesday
  (KE). The route is not in PRICED yet, so the client refuses it: it is planned at its list price of 5,
  recorded as not_priced and never called until core/collect/socialcrawl_client.py prices it;
- free, fetched with requests under a User-Agent naming 42 and Ogilvy, with no email address: the App
  Store top free chart per market (labelled iPhone only until Google Play lands) and the news RSS feeds.
  The App Store chart is a baseline rank list, so a timeout, connection error, 429 or 5xx on it is fetched
  once more (FREE_RETRIES); the news feeds are context and keep one attempt.

Every URL, scraped or free, is checked against its site's robots.txt first (fetched free, cached per
host for the run); a path the rules forbid, a robots.txt answering 401, 403 or 429, a 5xx or no answer
at all means the page is not fetched. A 404 robots.txt allows everything. A robots.txt read that fails in a
way that passes (a timeout, a connection error, 429 or a 5xx) is read once more after FREE_RETRY_WAIT, as the
App Store chart is; if that fails too, the host's pages are still not fetched.

The local cap counts what the client charged, not what it quoted. Once the charges reach the cap, the
phase stops: every later paid fetch is recorded not_made with the reason.

Rows. Chart and app entries become rank counters in item_counter_daily (is_board true, unit rank,
unbiased_rank, the source's own series, the fetched URL in the protocol), songs as sound items keyed on
the source's native id or else "artist - title", apps as brand items by name. Nairaland front page
threads are forum posts: posts rows and ranked post_observations in board_nairaland. The gossip panel is
parsed by core/collect/parse.py and its observations moved to series panel_ig_gossip; a post from a page
the curated lists hold for that market carries the market as source_market. News headlines
are context, never evidence: they give no posts and no counters, only seed_queue rows for named phrases
found in at least two of a market's headlines from the last two days, with rule 1 phrases dropped.

Coverage. Every planned fetch gives one record in the collect job's record shape (row 2.12), so
writers.health_rows turns them into collection_health rows. A fetch not made (robots, not priced, over
the local cap, stopped) is recorded with 0 calls and fails on Coverage with its reason. A board page that
comes back but gives the parser no entries is recorded no_entries, a failed call, never a valid empty list.

Nothing here writes to BigQuery. run() returns posts, observations, counters, seeds, records and the
credits charged; the collect job appends them.

    py -3.13 -m core.collect.local_sources --plan --run-date 2026-10-05   every fetch and hold, no network
"""

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from core.collect import curated_creators
from core.collect.ids import post_id
from core.collect.parse import (
    COUNTER_COLUMNS, OBSERVATION_COLUMNS, POST_COLUMNS, _local_day, _ok, _protocol, parse, parse_time)
from core.collect.socialcrawl_client import (
    PRICED, SAST, TRANSIENT, Refused, http_failure, list_price, quote_for, retry_fields, transport_failure)

log = logging.getLogger(__name__)

UA_TOKEN = "42-trend-engine"
USER_AGENT = f"{UA_TOKEN}/1.0 (Ogilvy South Africa cultural trend research)"
LOCAL_DAILY_CAP = 40
MARKETS = ("ZA", "NG", "KE")
ROW = "2.12"
FREE_ROUTES = ("rss", "apple_rss")
OK = ("ok", "empty", "cached")
STOP = ("cap_reached", "insufficient_credits", "balance_floor")
HEADLINE_DAYS, MIN_HEADLINES, SEEDS_PER_MARKET, SEED_TTL_DAYS = 2, 2, 10, 3
SEED_TEMPLATE = "search/multi"
# A free rank list (the App Store chart, a baseline series) whose fetch failed in a way that passes (a timeout,
# a connection error, HTTP 429 or a 5xx) is fetched once more after FREE_RETRY_WAIT seconds, as the collect
# client retries the SocialCrawl rank lists and boards. The record keeps calls 1 and the last attempt decides it.
FREE_RETRIES, FREE_RETRY_WAIT = 1, 5


@dataclass(frozen=True)
class Scrape:
    platform: str
    series: str
    urls: dict                 # market -> page
    by_country: bool = False   # sends location_country=<market>
    order: str = "title_artist"
    id_pattern: str | None = None
    wait_for: int | None = None
    note: str = "URL unverified until a live probe"


SHAZAM = "https://www.shazam.com/charts"
SCRAPE_SOURCES = {
    "nairaland": Scrape("nairaland", "board_nairaland", {"NG": "https://www.nairaland.com/"}),
    "turntable": Scrape("turntable", "board_turntable", {"NG": "https://turntablecharts.com/charts/top-100"}),
    "kworb_spotify": Scrape(
        "spotify", "board_kworb_spotify",
        {m: f"https://kworb.net/spotify/country/{m.lower()}_daily.html" for m in ("ZA", "NG")},
        order="artist_title", id_pattern=r"/track/([A-Za-z0-9]{22})"),
    "boomplay": Scrape(
        "boomplay", "board_boomplay", {m: "https://www.boomplay.com/trending-songs" for m in MARKETS},
        by_country=True, id_pattern=r"boomplay\.com/songs/(\d+)", wait_for=3000),
    "audiomack": Scrape(
        "audiomack", "board_audiomack", {m: "https://audiomack.com/trending-now" for m in MARKETS},
        by_country=True, id_pattern=r"audiomack\.com/([\w.-]+/song/[\w.-]+)", wait_for=3000),
    "shazam": Scrape(
        "shazam", "board_shazam",
        {"ZA": f"{SHAZAM}/top-200/south-africa", "NG": f"{SHAZAM}/top-200/nigeria", "KE": f"{SHAZAM}/top-200/kenya"},
        id_pattern=r"shazam\.com/(?:song|track)/(\d+)", wait_for=3000),
    "shazam_city": Scrape(
        "shazam", "board_shazam_city",
        {"ZA": f"{SHAZAM}/top-50/south-africa/johannesburg", "NG": f"{SHAZAM}/top-50/nigeria/lagos"},
        id_pattern=r"shazam\.com/(?:song|track)/(\d+)", wait_for=3000),
}
# Retired scrapes are never planned, held or recorded: plan() skips every (source, market) here, and a page
# that is gone has no URL above. A source that is gone is not a failed day (DATA.md 3.3 judges the routes a
# run called; TRUST.md G1 treats a day with no health row as no history, not as invalid). The plan printout
# and the Fieldwork page's unread sources list them with the reason. Deleting an entry plans the page again.
DROPPED_SCRAPES = {
    ("kworb_spotify", "KE"): "ke_daily.html answered HTTP 404 and the Kworb Spotify country index has no Kenya "
                             "entry (30 Sep 2026)",
    ("nairaland", "NG"): "the robots.txt check refused the front page on every run from 1 to 3 Oct 2026 (0 calls "
                         "of 1 planned); a disallow rule and a robots.txt answering 401, 403, 429 or 5xx both "
                         "refuse it",
    **{("audiomack", m): "trending-now came back with no chart rows the parser reads in ZA, NG and KE on every "
                         "run since 30 Sep 2026; the only sample is a hand-written list, not the page as served, "
                         "so no repair can be checked offline (3 Oct 2026)" for m in MARKETS},
}
# Public Instagram gossip, celebrity news and entertainment blog pages, three a market. A draft from
# desk knowledge: colleagues in each city confirm it as they did hubs.yaml (BUILD.md 0.9).
IG_GOSSIP = {
    "ZA": ("maphephandaba", "zalebs", "tshisalive"),
    "NG": ("instablog9ja", "lindaikejiblogofficial", "gossipmilltv"),
    "KE": ("nairobi_gossip_club", "edaily_kenya", "mpashogram"),
}
GOOGLE_PLAY_DAYS = {"ZA": 0, "NG": 1, "KE": 2}
APP_STORE = "https://rss.marketingtools.apple.com/api/v2/{cc}/apps/top-free/50/apps.json"


@dataclass(frozen=True)
class Feed:
    source: str
    market: str
    url: str
    note: str = "feed URL unverified until a live check"


FEEDS = (
    Feed("briefly", "ZA", "https://briefly.co.za/rss/all.rss"),
    Feed("zalebs", "ZA", "https://www.zalebs.com/feed/"),
    Feed("bellanaija", "NG", "https://www.bellanaija.com/feed/", "in engine/configs/sources.yaml, live in 2026"),
    Feed("punch", "NG", "https://punchng.com/feed/", "in engine/configs/sources.yaml, live in 2026"),
    Feed("legit", "NG", "https://www.legit.ng/rss/all.rss"),
    Feed("nairametrics", "NG", "https://nairametrics.com/feed/"),
    Feed("tuko", "KE", "https://www.tuko.co.ke/rss/all.rss", "engine/configs/sources.yaml, probed 28 May 2026"),
    Feed("kenyans", "KE", "https://www.kenyans.co.ke/feeds/news"),
)
DROPPED_FEEDS = {
    "pulse": "NG and KE feeds 404 on 1 Jul 2026 (engine/configs/sources.yaml)",
    "tshisalive": "TimesLIVE feed paths 404 on 1 Jul 2026 (engine/configs/sources.yaml)",
    "citizen_digital": "/rss answered 400 on 13 Jul 2026 (engine/configs/sources.yaml)",
    "mpasho": "served HTML with no feed items on 13 Jul 2026 (engine/configs/sources.yaml)",
    "ewn": "ZA feed https://ewn.co.za/RSS%20Feeds/Latest%20News redirects to HTTP 404; no official replacement "
           "established (30 Sep 2026)",
}
LOCAL_RANK_SERIES = tuple(sorted({s.series for s in SCRAPE_SOURCES.values()}
                                 | {"board_app_store_iphone", "board_google_play"}))


@dataclass
class Fetch:
    source: str
    market: str
    route: str        # a SocialCrawl route, or rss and apple_rss for free fetches
    url: str
    platform: str
    series: str
    lane_class: str
    params: dict = field(default_factory=dict)
    note: str = ""

    @property
    def paid(self):
        return self.route not in FREE_ROUTES

    @property
    def method(self):
        return PRICED[self.route].method if self.route in PRICED else "GET"

    def priced(self):
        try:
            self.hold_quote()
        except Refused:
            return False
        return True

    def hold_quote(self):
        return quote_for(self.route, self.method, self.params) if self.paid else 0

    def hold(self):
        """The client's quote, or the list price for a route the client cannot price yet."""
        try:
            return self.hold_quote()
        except Refused:
            return list_price(self.route, self.method)

    def protocol(self):
        if self.route == "prism/profiles":
            from core.collect.job import PRISM_PROTOCOL_VERSION, panel_protocol
            return panel_protocol(self.params["items"], PRISM_PROTOCOL_VERSION)
        if self.route == "web/scrape":
            return _protocol(self.route, self.params, ("url", "location_country"))
        if self.route == "apple_rss":
            return f"apple_rss?device=iphone_only&url={self.url}"
        if self.route == "rss":
            return f"rss?url={self.url}"
        return _protocol(self.route, self.params, ("app_collection", "country"))


def plan(day):
    """Every fetch of the day, market by market: scrapes, the gossip panel, the app charts, then the feeds."""
    since = (day - timedelta(days=1)).isoformat()
    fetches = []
    for market in MARKETS:
        for name, s in SCRAPE_SOURCES.items():
            if market not in s.urls or (name, market) in DROPPED_SCRAPES:
                continue
            params = {"url": s.urls[market], **({"location_country": market} if s.by_country else {}),
                      **({"wait_for": s.wait_for} if s.wait_for else {})}
            fetches.append(Fetch(name, market, "web/scrape", s.urls[market], s.platform, s.series,
                                 "unbiased_rank", params, s.note))
        handles = IG_GOSSIP[market]
        fetches.append(Fetch(
            "ig_gossip_panel", market, "prism/profiles",
            " ".join(f"https://www.instagram.com/{h}/" for h in handles), "instagram", "panel_ig_gossip", "panel",
            {"items": [{"platform": "instagram", "handle": h} for h in handles], "include": "posts", "since": since},
            "handles are a draft for colleagues to confirm"))
        if day.weekday() == GOOGLE_PLAY_DAYS[market]:
            params = {"app_collection": "topselling_free", "country": market.lower()}
            fetches.append(Fetch("google_play", market, "google_play/app-list",
                                 _protocol("google_play/app-list", params, ("app_collection", "country")),
                                 "google_play", "board_google_play", "unbiased_rank", params,
                                 "not priced in the client yet; held at the list price, never called"))
        fetches.append(Fetch("app_store", market, "apple_rss", APP_STORE.format(cc=market.lower()), "app_store",
                             "board_app_store_iphone", "unbiased_rank", note="iPhone only; live-verified 4 Jul 2026"))
        fetches += [Fetch(f.source, market, "rss", f.url, "news", "news_rss", "context", note=f.note)
                    for f in FEEDS if f.market == market]
    return fetches


def total_hold(fetches):
    return sum(f.hold() for f in fetches)


# Free fetches

def http_get(url, timeout=(10, 60)):
    """(connect, read) seconds: a free chart can answer slowly (NG App Store, 3 Oct 2026)."""
    import requests

    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    return response.status_code, response.text


def _log_free_failure(run_id, fetch, status, *, http_status=None, exception_class=None):
    event = {
        "event": "local_source_failure",
        "run_id": run_id,
        "source": fetch.source,
        "market": fetch.market,
        "route": fetch.route,
        "status": status,
    }
    if http_status is not None:
        event["http_status"] = http_status
    if exception_class is not None:
        event["exception_class"] = exception_class
    log.warning(json.dumps(event, separators=(",", ":"), sort_keys=True))


class Robots:
    """robots.txt per host, read once a run through get(url) -> (status, text). A read that fails in a way that
    passes (socialcrawl_client.TRANSIENT) is tried FREE_RETRIES more times, sleep(FREE_RETRY_WAIT) apart; the
    last attempt decides, and a failed read never allows."""

    def __init__(self, get, sleep=None):
        self.get, self.rules, self.sleep = get, {}, sleep or time.sleep

    def allowed(self, url):
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self.rules:
            self.rules[base] = self._read(base + "/robots.txt")
        rules = self.rules[base]
        return rules if isinstance(rules, bool) else rules.can_fetch(UA_TOKEN, url)

    def _read(self, url):
        status, text, error, _ = _get_with_retry(self.get, url, 1 + FREE_RETRIES, self.sleep)
        if error is not None:  # no answer: the rules are unknown, so the page is not fetched
            return False
        if status in (401, 403, 429) or status >= 500:
            return False
        if status != 200:
            return True
        rules = RobotFileParser()
        rules.parse(text.splitlines())
        return rules


# Parsers

IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
RANK = re.compile(r"^\d{1,3}$")
NUMBERED = re.compile(r"^(\d{1,3})[.)]\s+(.+)$")
FILLER = re.compile(r"^([+=-]?[\d,.]*|\(x\d+\)|new|re)$", re.I)
PART_SPLIT = re.compile(r"\s+-\s+|\s+by\s+")
SEPARATORS = " -|/:"
THREAD = re.compile(r"\[([^\]]+)\]\(((?:https?://(?:www\.)?nairaland\.com)?/(\d{4,})/[\w-]+)\)")


def _text(raw):
    plain = LINK.sub(r"\1", IMAGE.sub("", raw))
    return re.sub(r"\s+", " ", re.sub(r"[*`#]", "", plain)).strip()


def _parts(raw):
    """Link labels in order, then the plain text around them split on ' - ' or ' by '."""
    raw = IMAGE.sub("", raw)
    labels = [_text(label) for label, _ in LINK.findall(raw) if _text(label)]
    rest = _text(LINK.sub(" ", raw)).strip(SEPARATORS)
    return labels + [p.strip(SEPARATORS) for p in PART_SPLIT.split(rest) if p.strip(SEPARATORS)]


def _song(rank, parts, raw, source):
    if not parts:
        return None
    first, second = parts[0], parts[1] if len(parts) > 1 else ""
    title, artist = (second, first) if source.order == "artist_title" and second else (first, second)
    found = re.search(source.id_pattern, raw) if source.id_pattern else None
    return {"rank": rank, "title": title, "artist": artist, "native": found.group(1) if found else None}


def _table(lines, source):
    out = []
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not RANK.match(_text(cells[0])):
            continue
        rest = [c for c in cells[1:] if _text(c) and not FILLER.match(_text(c))]
        linked = next((c for c in rest if LINK.search(c)), None)
        parts = _parts(linked) if linked else [_text(c) for c in rest]
        out.append(_song(int(_text(cells[0])), parts, " ".join(rest), source))
    return out


def _numbered(lines, source):
    numbered = [(i, m) for i, line in enumerate(lines) if (m := NUMBERED.match(line))]
    out = []
    for n, (start, m) in enumerate(numbered):
        raw = m.group(2)
        parts = _parts(raw)
        if source.platform == "boomplay" and RANK.fullmatch(_text(raw)):
            stop = numbered[n + 1][0] if n + 1 < len(numbered) else len(lines)
            raw = "\n".join(lines[start + 1:stop])
            names = {}
            for label, href in LINK.findall(IMAGE.sub("", raw)):
                kind = re.match(r"https?://(?:www\.)?boomplay\.com/(songs|artists)/", href)
                if kind:
                    names.setdefault(kind.group(1), _text(label))
            parts = [names["songs"], names["artists"]] if names.get("songs") and names.get("artists") else []
        out.append(_song(int(m.group(1)), parts, raw, source))
    return out


def _blocks(lines, source):
    """A rank alone on its line, then the title and artist lines that follow it."""
    blocks = []
    for line in lines:
        if RANK.match(line):
            blocks.append((int(line), []))
        elif line and blocks:
            blocks[-1][1].append(line)
    return [_song(rank, [p for line in body[:2] for p in _parts(line)][:2], " ".join(body[:2]), source)
            for rank, body in blocks]


def chart_entries(markdown, source):
    """Ranked songs from a chart page's markdown: a table, a numbered list or rank blocks, whichever finds most."""
    lines = [line.strip() for line in (markdown or "").splitlines()]
    best = max((_table(lines, source), _numbered(lines, source), _blocks(lines, source)),
               key=lambda found: len([e for e in found if e and e["title"]]))
    entries, ranks = [], set()
    for e in best:
        if e and e["title"] and e["rank"] not in ranks:
            ranks.add(e["rank"])
            entries.append(e)
    return sorted(entries, key=lambda e: e["rank"])


def song_key(entry):
    return f"{entry['artist']} - {entry['title']}" if entry["artist"] else entry["title"]


def nairaland_threads(markdown):
    """Front page thread links in page order, each thread once."""
    out, seen = [], set()
    for label, href, native in THREAD.findall(markdown or ""):
        if native in seen or not _text(label):
            continue
        seen.add(native)
        out.append({"rank": len(out) + 1, "native": native, "url": urljoin("https://www.nairaland.com/", href),
                    "title": _text(label)})
    return out


def app_board(body):
    """App names in chart order from the App Store feed or a google_play/app-list body."""
    rows = (body.get("feed") or {}).get("results") if isinstance(body.get("feed"), dict) else None
    if rows is None:
        data = body.get("data")
        rows = data if isinstance(data, list) else (data or {}).get("items") or []
    names = [str(r.get("name") or r.get("title") or r.get("app_name") or "").strip() for r in rows
             if isinstance(r, dict)]
    return [n for n in names if n]


def app_store_board(body):
    """App names from the App Store feed. A body without its feed.results list raises ValueError, so a 200 that
    is not the chart is an unparsable fetch, not a valid empty chart; an empty results list is a read chart."""
    feed = body.get("feed") if isinstance(body, dict) else None
    if not isinstance(feed, dict) or not isinstance(feed.get("results"), list):
        raise ValueError("App Store body has no feed.results list")
    return app_board(body)


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _when(text):
    if not text:
        return None
    try:
        moment = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        return parse_time(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def headlines(xml_text, fetched):
    """Titles of RSS items and Atom entries, dropping any dated more than HEADLINE_DAYS before the fetch."""
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    oldest = fetched - timedelta(days=HEADLINE_DAYS)
    out = []
    for item in (e for e in root.iter() if _tag(e) in ("item", "entry")):
        fields = {_tag(child): (child.text or "").strip() for child in item}
        title = fields.get("title")
        moment = _when(fields.get("pubDate") or fields.get("published") or fields.get("updated")
                       or fields.get("date"))
        if title and (moment is None or moment >= oldest):
            out.append(title)
    return out


STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have he her his how i if in into is it
its just more most new no not of off on one or our out over says said she so than that the their them there
these they this those to top two up us was watch we were what when where who why will with would you your
after ahead amid before during under against about again also live latest breaking news update video photos
here now today week day year first last all
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november december
""".split())
PLACE_NAMES = frozenset({"south africa", "south african", "south africans", "sa", "mzansi", "nigeria", "nigerian",
                         "nigerians", "naija", "kenya", "kenyan", "kenyans"})
TOKEN = re.compile(r"[^\s,:;!?()\[\]\"|]+")


def _runs(headline):
    """Runs of capitalised words that are not stopwords; a lone word opening the headline is skipped."""
    runs, current = [], []
    for i, token in enumerate(t.strip(".'‘’") for t in TOKEN.findall(headline)):
        if token[:1].isupper() and token.casefold() not in STOPWORDS:
            current.append((i, token))
            continue
        runs.append(current)
        current = []
    runs.append(current)
    return [[t for _, t in run] for run in runs if run and not (len(run) == 1 and run[0][0] == 0)]


def news_seeds(market, titles, day, item_id_fn):
    """seed_queue rows for phrases named in at least MIN_HEADLINES of the market's headlines."""
    from core.collect.gdelt import blocked
    from core.collect.job import MULTI_PLATFORMS

    counts, spelling = Counter(), {}
    for title in titles:
        found = set()
        for run in _runs(title):
            for n in range(1, min(4, len(run)) + 1):
                for i in range(len(run) - n + 1):
                    phrase = " ".join(run[i:i + n])
                    spelling.setdefault(phrase.casefold(), phrase)
                    found.add(phrase.casefold())
        counts.update(found)
    kept = {k: n for k, n in counts.items() if n >= MIN_HEADLINES and k not in PLACE_NAMES}
    kept = {k: n for k, n in kept.items()
            if not any(k != o and f" {k} " in f" {o} " and kept[o] >= n for o in kept)}
    since = (day - timedelta(days=1)).isoformat()
    seeds = []
    for key, n in sorted(kept.items(), key=lambda kv: (-kv[1], kv[0])):
        if blocked(key) or len(seeds) == SEEDS_PER_MARKET:
            continue
        try:
            item = item_id_fn("topic", spelling[key], None)
        except ValueError:
            continue
        query = spelling[key]
        credits = quote_for(SEED_TEMPLATE, "GET", {"query": query, "platforms": MULTI_PLATFORMS, "since": since})
        seeds.append({"seed_date": day, "market": market, "item_id": item, "query": query, "kind": "topic",
                      "lane": "expansion", "priority": float(n), "template": SEED_TEMPLATE,
                      "ttl_days": SEED_TTL_DAYS, "credits_estimate": float(credits)})
    return seeds


# Rows

def _row(columns, **values):
    unknown = set(values) - set(columns)
    if unknown:
        raise ValueError(f"not a column: {sorted(unknown)}")
    return {c: values.get(c) for c in columns}


def _utc(moment):
    return moment.astimezone(timezone.utc).isoformat()


def _counters(f, item_ids, pull_seq, fetched, run_id):
    """One rank row per item, at its best rank in this pull."""
    rows, seen = [], set()
    for rank, item in item_ids:
        if item is None or item in seen:
            continue
        seen.add(item)
        rows.append(_row(COUNTER_COLUMNS, obs_date=_local_day(fetched, f.market), market=f.market,
                         platform=f.platform, item_id=item, series=f.series, route=f.route, protocol=f.protocol(),
                         is_board=True, lane_class="unbiased_rank", unit="rank", pull_seq=pull_seq,
                         value=float(rank), source="live", observed_at=_utc(fetched), available_at=_utc(fetched),
                         run_id=run_id))
    return rows


def _item(item_id_fn, kind, raw, platform=None):
    try:
        return item_id_fn(kind, raw, platform)
    except ValueError:
        return None


def _threads(out, f, threads, pull_seq, fetched, run_id, geo):
    day = _local_day(fetched, f.market)
    ids = []
    for t in threads:
        pid = post_id(f.platform, t["native"], t["url"])
        located = geo(f.platform, f.market, text=t["title"]) or (None, None, None)
        out["posts"].append(_row(
            POST_COLUMNS, post_id=pid, platform=f.platform, native_id=t["native"], url=t["url"], text=t["title"],
            hashtags=[], post_date=day, geo_market=located[0], geo_confidence=located[1], geo_source=located[2],
            vendor="socialcrawl", endpoint=f.route, source_regime="socialcrawl", run_id=run_id))
        out["observations"].append(_row(
            OBSERVATION_COLUMNS, post_id=pid, observed_at=_utc(fetched), observed_date=day, market=f.market,
            source_market="NG", source_region=None,
            platform=f.platform, route=f.route, series=f.series, protocol=f.protocol(), lane="sweep",
            lane_class="unbiased_rank", pull_seq=pull_seq, rank=t["rank"], run_id=run_id))
        ids.append(pid)
    return ids


def _markdown(body):
    page = ((body or {}).get("data") or {}).get("page") or {}
    if (page.get("status_code") or 200) >= 400:
        return None
    return (page.get("content") or {}).get("markdown")


# The phase

def _free_get(get, f, sleep):
    """(status, text, exception, earlier failure classes) of a free fetch: one attempt, or for a rank list up
    to FREE_RETRIES more after a failure in socialcrawl_client.TRANSIENT."""
    return _get_with_retry(get, f.url, 1 + (FREE_RETRIES if f.lane_class == "unbiased_rank" else 0), sleep)


def _get_with_retry(get, url, tries, sleep):
    """(status, text, exception, earlier failure classes) of up to tries attempts at get(url), the next made
    only after a failure in socialcrawl_client.TRANSIENT, FREE_RETRY_WAIT seconds later."""
    failures = []
    while True:
        try:
            status, text = get(url)
            error, failure = None, None if status == 200 else http_failure(status)
        except Exception as exc:
            status, text, error, failure = None, None, exc, transport_failure(exc)
        if failure not in TRANSIENT or len(failures) + 1 >= tries:
            return status, text, error, failures
        failures.append(failure)
        sleep(FREE_RETRY_WAIT)


def run(day, run_id, *, client, get, clock, item_id_fn, geo_fn, last_pulls=None, sleep=None, profile_cache=None):
    """Make the day's local fetches and return their rows. client is the collect share's SocialCrawlClient,
    get(url) -> (status, text) the free getter (http_get live), last_pulls the job's
    (market, series, protocol) -> last stored pull_seq, sleep the wait before a free rank list's retry."""
    from core.collect.job import safe_geo

    geo = safe_geo(geo_fn)
    out = {"posts": [], "observations": [], "counters": [], "seeds": [], "records": [], "credits": 0.0}
    robots, pulls, stopped = Robots(get, sleep or time.sleep), dict(last_pulls or {}), None
    news = {m: {} for m in MARKETS}
    listed = None

    receipt = {}  # the receipt keys of the fetch in hand when its call was retried (socialcrawl_client.retry_fields)

    def record(f, status, *, calls=0, ok=False, items=0, units_ok=0, post_ids=(), reason="", failure=""):
        planned = len(f.params["items"]) if f.route == "prism/profiles" else 1
        out["records"].append({
            "row": ROW, "route": f.route, "market": f.market, "day": _local_day(clock(), f.market),
            "platform": f.platform, "series": f.series, "protocol": f.protocol(), "lane_class": f.lane_class,
            "status": status, "ok": ok, "calls": calls, "units_planned": planned, "units_ok": units_ok,
            "items": items, "post_ids": list(post_ids), "reason": reason, "failure": failure, **receipt})

    def next_pull(f):
        key = (f.market, f.series, f.protocol())
        pulls[key] = pulls.get(key, 0) + 1
        return pulls[key]

    fetches = plan(day)
    for i, f in enumerate(fetches):
        receipt.clear()
        if f.paid:
            if stopped:
                record(f, "not_made", reason=f"stopped: {stopped}")
                continue
            if not f.priced():
                record(f, "not_priced", reason=f"{f.route} is not in socialcrawl_client.PRICED")
                continue
            if out["credits"] + f.hold() > LOCAL_DAILY_CAP:
                record(f, "over_local_cap",
                       reason=f"{out['credits']} charged plus {f.hold()} is over {LOCAL_DAILY_CAP}")
                continue
        if f.route in ("web/scrape",) + FREE_ROUTES and not robots.allowed(f.url):
            record(f, "robots_disallowed", reason=f"robots.txt does not allow {f.url}")
            continue
        if f.paid:
            pull = next_pull(f) if f.lane_class == "unbiased_rank" else None
            lane = "panel" if f.route == "prism/profiles" else "sweep"
            # A board or the gossip panel answered 5xx is tried once more inside the local cap, leaving the
            # holds of the paid fetches still to come (W8-DEC-19); the chart pulls and the plan stay as planned.
            room = {}
            if f.lane_class == "unbiased_rank" or f.route == "prism/profiles":
                later = sum(g.hold() for g in fetches[i + 1:] if g.paid and g.priced())
                room = {"server_retry_room": LOCAL_DAILY_CAP - out["credits"] - later}
            result = client.call(f.route, f.params, method=f.method, market=f.market, lane=lane, **room)
            receipt.update(retry_fields(result))
            fetched = clock()
            out["credits"] += result.credits_charged or 0
            if result.status in STOP:
                stopped = result.status
            elif out["credits"] >= LOCAL_DAILY_CAP:
                stopped = f"local cap reached, {out['credits']} charged against {LOCAL_DAILY_CAP}"
            if result.status not in OK or result.body is None:
                record(f, result.status, calls=1, reason=result.reason, failure=getattr(result, "failure", ""))
                continue
            if f.route == "prism/profiles":
                parsed = parse(f.route, f.params, f.market, result.body, fetched, run_id, item_id_fn=item_id_fn,
                               geo_fn=geo, protocol=f.protocol(), profile_cache=profile_cache)
                # A post from a gossip page the curated lists hold for this market is a local outlet's post:
                # seen in the market's feeds (TRUST.md, market by source). Any other page's posts stay unscoped.
                if listed is None:
                    manifest = curated_creators.load_manifest()
                    listed = {m: curated_creators.listed_handles(manifest, m) for m in MARKETS}
                own = {p["post_id"] for p in parsed["posts"]
                       if (p["platform"], str(p.get("creator_id") or "").casefold()) in listed[f.market]}
                for obs in parsed["observations"]:
                    obs["series"] = f.series
                    if obs["post_id"] in own:
                        obs["source_market"] = f.market
                out["posts"] += parsed["posts"]
                out["observations"] += parsed["observations"]
                units = sum(isinstance(r, dict) and _ok(r) for r in result.items)
                record(f, result.status, calls=1, ok=True, items=len(parsed["observations"]), units_ok=units,
                       post_ids=[p["post_id"] for p in parsed["posts"]])
                continue
            if f.route == "google_play/app-list":
                names = app_board(result.body)
                rows = _counters(f, [(r, _item(item_id_fn, "brand", n)) for r, n in enumerate(names, 1)], pull,
                                 fetched, run_id)
                out["counters"] += rows
                record(f, result.status, calls=1, ok=True, items=len(rows), units_ok=1)
                continue
            markdown = _markdown(result.body)
            if markdown is None:
                record(f, "no_page", calls=1, reason="web/scrape returned no page markdown")
                continue
            threads = nairaland_threads(markdown) if f.source == "nairaland" else None
            entries = None if threads is not None else chart_entries(markdown, SCRAPE_SOURCES[f.source])
            if not (threads or entries):
                # A board page the parser reads nothing from is not a parsable body (DATA.md 3.3), so the
                # day fails on calls instead of passing as a valid empty list.
                record(f, "no_entries", calls=1, reason=f"web/scrape page for {f.url} gave no entries the "
                                                        f"{f.source} parser reads")
                continue
            if threads is not None:
                ids = _threads(out, f, threads, pull, fetched, run_id, geo)
                record(f, result.status, calls=1, ok=True, items=len(ids), units_ok=1, post_ids=ids)
                continue
            rows = _counters(f, [(e["rank"], _item(item_id_fn, "sound", e["native"] or song_key(e), f.platform))
                                 for e in entries], pull, fetched, run_id)
            out["counters"] += rows
            record(f, result.status, calls=1, ok=True, items=len(rows), units_ok=1)
            continue
        pull = next_pull(f) if f.lane_class == "unbiased_rank" else None
        status, text, error, failures = _free_get(get, f, sleep or time.sleep)
        tried = f" ({len(failures) + 1} attempts: {', '.join(failures)} first)" if failures else ""
        if error is not None:  # a dead host is a failed fetch on Coverage, not a failed run
            _log_free_failure(run_id, f, "get_error", exception_class=type(error).__name__)
            record(f, "error", calls=1, reason=str(error) + tried, failure=transport_failure(error))
            continue
        fetched = clock()
        if status != 200:
            _log_free_failure(run_id, f, "http_error", http_status=status)
            record(f, f"http_{status}", calls=1, reason=f"{f.url} answered {status}{tried}",
                   failure=http_failure(status))
            continue
        try:
            if f.route == "rss":
                titles = headlines(text, fetched)
                for title in titles:
                    news[f.market].setdefault(title.casefold(), title)
                record(f, "ok", calls=1, ok=True, items=len(titles), units_ok=1)
            else:
                names = app_store_board(json.loads(text))
                rows = _counters(f, [(r, _item(item_id_fn, "brand", n)) for r, n in enumerate(names, 1)], pull,
                                 fetched, run_id)
                out["counters"] += rows
                record(f, "ok", calls=1, ok=True, items=len(rows), units_ok=1)
        except (ElementTree.ParseError, ValueError, AttributeError) as error:
            _log_free_failure(run_id, f, "parse_error", http_status=status,
                              exception_class=type(error).__name__)
            record(f, "unparsable", calls=1, reason=str(error))
    for market in MARKETS:
        out["seeds"] += news_seeds(market, list(news[market].values()), day, item_id_fn)
    return out


# Sweep draft (begin). Written by core/collect/sources_sweep.py in its write-draft mode on 2026-09-30.
# draft hash: ddbc59a51093ea59
# plan() and the collect job never read IG_GOSSIP_DRAFT or FEEDS_DRAFT. Colleagues confirm an entry
# before it moves into IG_GOSSIP or FEEDS, and LOCAL_DAILY_CAP grows with IG_GOSSIP.
IG_GOSSIP_DRAFT = {
    "ZA": (),
    "NG": (),
    "KE": (),
}
FEEDS_DRAFT = (
    {"source": "Mail & Guardian", "market": "ZA", "url": "https://mg.co.za/rss/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "TechCentral", "market": "ZA", "url": "https://techcentral.co.za/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Htxt Africa", "market": "ZA", "url": "https://htxt.co.za/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "The South African", "market": "ZA", "url": "https://www.thesouthafrican.com/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "SABC News", "market": "ZA", "url": "https://www.sabcnews.com/sabcnews/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "BusinessTech", "market": "ZA", "url": "https://businesstech.co.za/news/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "IOL News", "market": "ZA", "url": "https://www.iol.co.za/rss/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "The Nerve Africa", "market": "NG", "url": "https://thenerveafrica.com/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Vanguard", "market": "NG", "url": "https://www.vanguardngr.com/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Information Nigeria", "market": "NG", "url": "https://www.informationng.com/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Within Nigeria", "market": "NG", "url": "https://www.withinnigeria.com/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Premium Times Sport", "market": "NG", "url": "https://www.premiumtimesng.com/category/sports/feed",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "BBC News Pidgin", "market": "NG", "url": "https://feeds.bbci.co.uk/pidgin/rss.xml",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Nairobi Wire", "market": "KE", "url": "https://nairobiwire.com/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Nation Africa", "market": "KE", "url": "https://nation.africa/kenya/rss.xml",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Kahawa Tungu", "market": "KE", "url": "https://www.kahawatungu.com/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Ghafla Kenya", "market": "KE", "url": "https://www.ghafla.co.ke/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "KBC", "market": "KE", "url": "https://www.kbc.co.ke/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "The Standard", "market": "KE", "url": "https://www.standardmedia.co.ke/rss/headlines.php",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Capital FM Kenya Lifestyle", "market": "KE", "url": "https://www.capitalfm.co.ke/lifestyle/feed/",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
    {"source": "Kenyan Post", "market": "KE", "url": "https://www.kenyan-post.com/feeds/posts/default",
     "why": "listed in engine/configs/sources.yaml (rss_feeds); not in use"},
)
# Sweep draft (end)


# The plan printout

def print_plan(day):
    fetches = plan(day)
    print(f"local sources plan for {day.isoformat()} ({day:%A}), local cap {LOCAL_DAILY_CAP} credits a day "
          "from the collect share")
    for market in MARKETS:
        mine = [f for f in fetches if f.market == market]
        print(market)
        for f in mine:
            flag = "" if f.priced() else "  [not priced]"
            print(f"  {f.route:22} {f.source:16} {f.hold():>3}  {f.url}  ({f.note}){flag}")
        paid = [f for f in mine if f.paid]
        print(f"  {market} hold {total_hold(mine)} over {len(paid)} paid and {len(mine) - len(paid)} free fetches")
    for (name, market), why in DROPPED_SCRAPES.items():
        print(f"dropped scrape {name} {market}: {why}")
    for name, why in DROPPED_FEEDS.items():
        print(f"dropped feed {name}: {why}")
    print(f"total hold {total_hold(fetches)} credits over {len(fetches)} fetches (cap {LOCAL_DAILY_CAP}); "
          "news seeds are spent later by the expansion phase, not here")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", help="print every planned fetch and hold; no network")
    parser.add_argument("--run-date", type=date.fromisoformat, help="YYYY-MM-DD; default today in SAST")
    args = parser.parse_args(argv)
    if not args.plan:
        parser.error("the local sources run inside the collect job; --plan prints the day's fetches")
    print_plan(args.run_date or datetime.now(SAST).date())
    return 0


if __name__ == "__main__":
    sys.exit(main())
