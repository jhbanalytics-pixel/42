"""Read-only check of every 42 page's data on a deployed f42-api.

    py -3.13 ops/runners/check_pages.py <base_url> [--market ZA] [--bq]

For each page it reads the API endpoints that page calls and prints one line per read: page, endpoint, HTTP
status, latency and a content check (row or item counts, the latest date in the payload against today in SAST,
empty lists, fixture markers, placeholder text). A read slower than SLOW_S seconds is marked SLOW with its seconds.
A summary of the pages that look empty, stale, fake or broken follows, with the slow reads and each page's seconds,
then one line for the older desk pages whose links now open 42 pages (MOVED): they are not probed,
each is named with how the 42 pages it lands on read. Saved findings are saved by people, so History is not empty
for want of one. --bq adds a BigQuery freshness section: per source table behind those pages, the latest date and
the rows of the last 3 days, distinct TikTok creators per market over the last 7 days, and the latest ok run per
job stage from intelligence_42_agent.runs.

Safety:
- Only GET. The client refuses any other method before it reaches the network, so nothing here starts an ask,
  an investigation, a watch or anything that spends credits.
- The passcode comes only from the environment variable F42_SMOKE_PASSCODE. It is sent only as the X-Passcode
  header to the base URL given on the command line, redirects are not followed, and it is never printed, logged
  or written. No printed line carries a header value or an exception's text.
- --bq runs SELECT statements only, as the caller's application default credentials, in ogilvy-trends-v2 with
  maximum_bytes_billed of 1 GB per query. It never calls the bq CLI.

Exit 0 when no page looks broken, empty, stale or fake (a slow read is reported, not judged); 1 otherwise; 2 when
the passcode is not set or the URL is refused.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, time as clock, timedelta, timezone
from urllib.parse import quote, urlsplit

import httpx

SAST = timezone(timedelta(hours=2), "SAST")
TIMEOUT = 60.0
MARKETS = ("ZA", "NG", "KE")
PROJECT = "ogilvy-trends-v2"
MAX_BYTES = 1024 ** 3
STALE_DAYS = 1  # the brief publishes at 06:15 SAST, so yesterday is still current before then
SLOW_S = 5.0  # a read slower than this is marked SLOW; the page still loads, but a person waits for it
FIXTURE = re.compile(r"fixture", re.IGNORECASE)
PLACEHOLDER = re.compile(r"coming soon|lorem ipsum|placeholder|not wired|\bTODO\b|\bTBD\b|dummy data|sample data|"
                         r"the app build is not on this server", re.IGNORECASE)
DATE_KEYS = ("date", "run_date", "latest_day", "latest_day_with_data", "published_at", "as_of", "metric_date",
             "updated_at", "created_at", "status_at", "refreshed_at", "brief_date", "match_date", "at", "to",
             "current_day", "last_seen")
ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
# The stand-in titles core/api/today.py _display_title gives an item with no readable label ("TikTok post",
# "Social media post", "Trend item"): no item is called that, so a history search for one finds nothing.
GENERIC_TITLE = re.compile(r"^(?:(?:TikTok|Instagram|YouTube|X|Reddit|Facebook|Telegram|Apple Music|Social media) "
                           r"post(?: by @\S+)?|(?:Hashtag|Sound|Creator|Trend|Format|Meme|Brand|Event) item)$",
                           re.IGNORECASE)


class Refused(Exception):
    pass


def today_sast(now=None):
    return (now or datetime.now(SAST)).astimezone(SAST).date()


def check_base(url):
    """The base URL, or Refused: https anywhere, plain http only to this machine (a local f42-api)."""
    parts = urlsplit(url.strip())
    local = parts.hostname in ("localhost", "127.0.0.1")
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local) or not parts.hostname:
        raise Refused("the base URL must be https, or http to localhost")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise Refused("the base URL must be the service origin only")
    return f"{parts.scheme}://{parts.netloc}"


class Reader:
    """GET only, passcode only in X-Passcode to the one origin, no redirects."""

    def __init__(self, base, passcode, client=None):
        self.base = base
        self._passcode = passcode
        self.client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self.methods = []

    def get(self, path, gated=True):
        if not path.startswith("/") or path.startswith("//"):
            raise Refused("paths must be relative to the base URL")
        self.methods.append("GET")
        headers = {"X-Passcode": self._passcode} if gated else {}
        started = time.monotonic()
        try:
            resp = self.client.request("GET", self.base + path, headers=headers)
        except httpx.HTTPError as exc:
            return None, None, round((time.monotonic() - started) * 1000), type(exc).__name__
        ms = round((time.monotonic() - started) * 1000)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, body, ms, None


# Payload checks.


def walk(value, key=None):
    """(key, value) for every scalar in a JSON value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v, key)
    else:
        yield key, value


def latest_date(body, keys=DATE_KEYS, until=None):
    """The newest date under one of keys, ignoring any after until (calendar moments look ahead)."""
    best = None
    for key, value in walk(body):
        if key in keys and isinstance(value, str):
            m = ISO_DATE.match(value)
            if m:
                try:
                    d = date.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if until is not None and d > until:
                    continue
                best = d if best is None or d > best else best
    return best


def markers(body):
    """(fixture, placeholder) found anywhere in the payload's strings."""
    fixture = placeholder = False
    for _key, value in walk(body):
        if isinstance(value, str):
            fixture = fixture or bool(FIXTURE.search(value))
            placeholder = placeholder or bool(PLACEHOLDER.search(value))
    return fixture, placeholder


def n(value):
    return len(value) if isinstance(value, list) else 0


def age_text(d, today):
    return f"{d.isoformat()} ({(today - d).days}d)" if d else "no date"


# One function per endpoint: (detail text, empty) from the payload.


def s_health(b):
    checks = b.get("checks") or {}
    return f"ok={b.get('ok')} version={b.get('version')} " + " ".join(f"{k}={v}" for k, v in checks.items()), False


def s_today(b):
    parts = []
    total = 0
    held_total = 0
    for m in b.get("markets") or []:
        cards = n(m.get("cards")) + n(m.get("more"))
        total += cards
        held = n((m.get("held_back") or {}).get("items")) if isinstance(m.get("held_back"), dict) else 0
        held_total += held
        parts.append(f"{m.get('market')} {cards}c/{held}h")
    warm = b.get("warmup")
    warm_text = f" warmup day {warm.get('day')}" if isinstance(warm, dict) and warm.get("day") else ""
    breaking = sum(n(m.get("breaking")) for m in b.get("markets") or [])
    # A day where the checks held every topic is a read of real data. Only no cards and nothing held is empty.
    return f"{b.get('status')} {' '.join(parts)} breaking {breaking}{warm_text}", total == 0 and held_total == 0


def s_items(key, label):
    def summary(b):
        rows = b.get(key)
        return f"{n(rows)} {label}", n(rows) == 0
    return summary


def s_discover(b):
    states = {}
    for item in b.get("items") or []:
        states[item.get("state")] = states.get(item.get("state"), 0) + 1
    top = ", ".join(f"{k} {v}" for k, v in sorted(states.items(), key=lambda kv: -kv[1])[:3])
    return f"{n(b.get('items'))} items run {b.get('run_id') or '-'}; {top}", n(b.get("items")) == 0


def s_topic(b):
    series = b.get("series") or {}
    points = n(series.get("points")) if isinstance(series, dict) else n(series)
    news = b.get("news") or {}
    news_n = n(news.get("items")) if isinstance(news, dict) else n(news)
    return (f"series {points} pts, evidence {n(b.get('evidence'))}, history {n(b.get('history'))}, "
            f"news {news_n}, waves {n(b.get('waves'))}"), n(b.get("evidence")) == 0


def s_compare(b):
    return f"{n(b.get('subjects'))} subjects, {n(b.get('rows'))} rows", n(b.get("rows")) == 0


def s_coverage(b):
    runs = b.get("runs") or []
    failed = sum(1 for r in runs if r.get("status") == "failed")
    credits = ((b.get("credits") or {}).get("total") or {}).get("value")
    card = "scorecard" if b.get("scorecard") else "no scorecard"
    return (f"collection {b.get('collection')}, {len(runs)} runs ({failed} failed), credits {credits}, {card}",
            b.get("collection") in ("not_recorded", "empty"))


def s_fieldwork(b):
    return f"health {b.get('health_state')}, plan {b.get('plan_state')}, groups {n(b.get('groups'))}", \
        b.get("health_state") not in ("recorded",)


def s_alerts(b):
    return f"{n(b.get('alerts'))} alerts, {n(b.get('waiting'))} waiting", False


def s_schedules(b):
    rows = b.get("schedules") or []
    ran = sum(1 for s in rows if s.get("last_run"))
    return f"{len(rows)} schedules, {ran} with a last run", False


def s_investigations(b):
    rows = b.get("investigations") or []
    states = {}
    for r in rows:
        states[r.get("status")] = states.get(r.get("status"), 0) + 1
    return (f"{len(rows)} investigations " + " ".join(f"{k} {v}" for k, v in states.items())).strip(), False


def s_communities(b):
    note = " (note: " + str(b.get("note"))[:40] + ")" if b.get("note") else ""
    return f"{n(b.get('communities'))} communities{note}", n(b.get("communities")) == 0


def s_creator(b):
    return (f"tier {b.get('tier') or (b.get('creator') or {}).get('tier')}, "
            f"recent posts {n(b.get('recent_posts'))}, items {n(b.get('top_items'))}"), False


def s_seed_path(b):
    posts = b.get("matched_posts")
    posts = posts.get("value") if isinstance(posts, dict) else posts
    return f"{b.get('status')}, {posts} posts, {n(b.get('platforms'))} platforms", b.get("status") != "ok"


def s_seeds(b):
    """Seeds (contract.md section 19): the queued day and its searches, and the last day with results."""
    queue, results = b.get("queue") or {}, b.get("results") or {}
    total = (queue.get("seeds_total") or {}).get("value")
    posts = ((results.get("totals") or {}).get("posts") or {}).get("value")
    return (f"{b.get('status')}, queued {queue.get('seed_date') or '-'} {total or 0} searches, "
            f"results {results.get('seed_date') or '-'} {posts or 0} posts"), b.get("status") != "ok"


def s_lexicon(b):
    """Page port, 3 October 2026: Lexicon reads /api/lexicon (contract.md section 20), one market at a time."""
    return f"{b.get('status')}, {n(b.get('terms'))} terms", b.get("status") != "ok"


def s_history_item(b):
    return f"{n(b.get('waves'))} waves, {n(b.get('analogues'))} analogues", n(b.get("waves")) == 0


def s_history_briefs(b):
    """Brief days (contract.md section 12.4), a brief that held every item included, and what the newest showed."""
    dates = b.get("dates") or []
    newest = dates[0] if dates and isinstance(dates[0], dict) else {}
    parts = []
    for m in newest.get("markets") or []:
        shown, held = (m.get(k).get("value") if isinstance(m.get(k), dict) else None for k in ("cards", "held"))
        parts.append(f"{m.get('market')} {'-' if shown is None else shown}c/{'-' if held is None else held}h")
    latest = f"; newest {' '.join(parts)}" if parts else ""
    return f"{n(dates)} brief days{latest}", n(dates) == 0


def s_findings(b):
    """Saved findings are saved by people, so none saved yet is not an empty page; the count still prints."""
    rows = b.get("findings")
    return f"{n(rows)} findings" + ("" if n(rows) else " (none saved yet)"), False


def s_skin_today(b):
    """Today narrowed to the skin (core/api/skins.py narrow_today): its markets and their kept cards."""
    cards = sum(n(m.get("cards")) + n(m.get("more")) for m in b.get("markets") or [])
    return f"{n(b.get('markets'))} markets, {cards} cards kept", False


def s_spa(b):
    text = b if isinstance(b, str) else ""
    ok = "<div id=" in text or "<script" in text
    return ("app shell served" if ok else "no app build"), not ok


# Pages: (page, path, summary, gated, dated). Paths with {..} are filled from earlier reads.

PAGES = [
    ("App", "/", s_spa, False, False),
    ("Health", "/api/health", s_health, False, False),
    ("Today", "/api/today", s_today, True, True),
    ("Today", "/api/trends/{item}?market={market}", lambda b: (f"{n(b.get('evidence'))} evidence, "
                                                              f"explained {b.get('explained')}", False), True, True),
    ("Today", "/api/alerts", s_alerts, True, True),
    ("Discover", "/api/discover?market=all&limit=200", s_discover, True, True),
    ("Discover", "/api/discover/radar?market={market}", s_items("points", "points"), True, True),
    ("Topic", "/api/topics/{item}?market={market}", s_topic, True, True),
    ("Compare", "/api/compare?mode=items&items={items}&market={market}&days=28", s_compare, True, True),
    ("Investigations", "/api/investigations", s_investigations, True, False),
    ("Investigations", "/api/investigations/{investigation}", lambda b: (f"status {b.get('status')}", False),
     True, False),
    ("Dossiers", "/api/dossiers", s_items("dossiers", "dossiers"), True, False),
    ("Dossiers", "/api/dossiers/{dossier}", lambda b: (f"version {b.get('version')} {b.get('state')}", False),
     True, False),
    ("History", "/api/history/briefs", s_history_briefs, True, True),
    ("History", "/api/history/asks", s_items("asks", "asks"), True, True),
    ("History", "/api/history/findings", s_findings, True, False),
    ("History", "/api/history/items/{item}?market={market}", s_history_item, True, True),
    ("History", "/api/history/search?q={term}", s_items("items", "matches"), True, False),
    ("Alerts", "/api/watches", s_items("watches", "watches"), True, False),
    ("Coverage", "/api/coverage", s_coverage, True, True),
    ("Fieldwork", "/api/fieldwork", s_fieldwork, True, True),
    ("Seeds", "/api/seeds?market={market}", s_seeds, True, True),
    ("Lexicon", "/api/lexicon?market={market}", s_lexicon, True, True),
    # Seed path traces a word people write, so it is probed with the Lexicon's top word or hashtag (its seed_term),
    # not a Discover title: a topic's label is 42's name for a theme and is rarely in any post (contract section 20).
    ("Seed path", "/api/seed-path?keyword={seed_term}&market={market}", s_seed_path, True, True),
    ("Schedules", "/api/schedules", s_schedules, True, False),
    ("Skins", "/api/skins", s_items("skins", "skins"), True, False),
    ("Skins", "/api/skins/{skin}/today", s_skin_today, True, True),
    ("Communities", "/api/communities?market={market}", s_communities, True, True),
    ("Creator", "/api/creators/{creator}?market={market}", s_creator, True, False),
    ("Hidden people", "/api/suppressions", s_items("suppressions", "hidden"), True, False),
]
UNDATED_EMPTY_OK = {"Alerts", "Schedules", "Skins", "Investigations", "Dossiers", "Hidden people", "Creator"}

# The older desk pages. Their desk API was never served by f42-api; since 3 October 2026 their links open the 42
# page that does the same job (app/frontend/src/legacyRoutes.js, a client-side hash redirect with no API route of
# its own). So they are not probed: each is reported with the result of the 42 pages it now lands on, read above.
# (legacy page, its route in legacyRoutes.js, the hash it lands on, the pages above whose reads it now shows)
MOVED = (
    ("Browse", "browse", "#/explore", ("Discover",)),
    ("Network", "network", "#/communities", ("Communities",)),
    ("Board", "board", "#/alerts", ("Alerts",)),
    ("Listen", "listen", "#/seedpath", ("Seed path",)),
    ("Source Lab", "source-lab", "#/fieldwork", ("Fieldwork",)),
    ("Historical", "historical", "#/history", ("History",)),
    ("Build console", "console", "#/console", ("Investigations", "Dossiers", "Schedules", "History")),
)
WORST = ("FAIL", "FAKE", "EMPTY", "STALE", "SKIP", "OK")
TAG_WORDS = {"FAIL": "broken", "FAKE": "fake", "EMPTY": "empty", "STALE": "stale", "SKIP": "not opened",
             "OK": "live"}


def moved_line(results):
    """One plain line: each moved desk page and how the 42 pages it lands on read in this run."""
    tags = {}
    for page, tag, *_rest in results:
        tags.setdefault(page, set()).add(tag)
    parts = []
    for old, _route, _hash, pages in MOVED:
        seen = set().union(*(tags.get(p, set()) for p in pages)) & set(WORST)
        word = TAG_WORDS[next(t for t in WORST if t in seen)] if seen else "not read"
        parts.append(f"{old} to {'/'.join(pages)} {word}")
    return "; ".join(parts)


def fill(path, ids):
    def one(m):
        value = ids.get(m.group(1))
        return quote(value, safe=",") if value else "\0"
    out = re.sub(r"\{(\w+)\}", one, path)
    return None if "\0" in out else out


def learn_ids(path, body, ids):
    """Ids later reads need, taken from the payloads read before them."""
    if not isinstance(body, dict):
        return
    if path.startswith("/api/today"):
        for m in body.get("markets") or []:
            cards = (m.get("cards") or []) + (m.get("more") or [])
            if cards and m.get("market") == ids["market"]:
                ids.setdefault("item", cards[0].get("item_id"))
    elif path.startswith("/api/discover?"):
        items = [i for i in body.get("items") or [] if i.get("market") in (None, ids["market"])]
        if items:
            ids.setdefault("item", items[0].get("item_id"))
            ids["items"] = ",".join(i.get("item_id") for i in items[:2] if i.get("item_id"))
            # History search matches item labels, so a stand-in title is skipped and a hashtag, the item's own
            # word, is tried first.
            titled = [i for i in items if i.get("kind") != "creator" and i.get("title")
                      and not GENERIC_TITLE.match(str(i["title"]).strip())]
            pick = next((i for i in titled if i.get("kind") == "hashtag"), titled[0] if titled else None)
            title = pick and str(pick["title"]).strip()
            if title:
                ids.setdefault("term", re.sub(r"^[#@]", "", title)[:60])
    elif path == "/api/investigations":
        rows = body.get("investigations") or []
        if rows:
            ids["investigation"] = rows[0].get("investigation_id")
    elif path == "/api/dossiers":
        rows = body.get("dossiers") or []
        if rows:
            ids["dossier"] = rows[0].get("dossier_id")
    elif path == "/api/skins":
        rows = body.get("skins") or []
        if rows:
            ids["skin"] = rows[0].get("skin_id")
    elif path.startswith("/api/lexicon?"):
        terms = [t for t in body.get("terms") or [] if isinstance(t, dict) and t.get("seed_term")]
        # A hashtag first: Seed path matches post hashtags as well as words, so a hashtag Lexicon counts posts for
        # is the surest probe; a word or phrase otherwise.
        pick = next((t for t in terms if t.get("kind") == "hashtag"), terms[0] if terms else None)
        if pick:
            ids.setdefault("seed_term", pick["seed_term"])
    elif path.startswith("/api/communities?"):
        for c in body.get("communities") or []:
            for member in c.get("members") or []:
                if isinstance(member, dict) and member.get("creator_id"):
                    ids.setdefault("creator", member["creator_id"])


def judge(page, path, status, body, summary, dated, today, err):
    """(tag, detail): OK, EMPTY, STALE, FAKE or FAIL. Health is FAIL unless its ok is true."""
    if status is None:
        return "FAIL", f"request failed ({err})"
    if status != 200:
        message = body.get("message") if isinstance(body, dict) else None
        code = body.get("error") if isinstance(body, dict) else None
        tag = "EMPTY" if status == 409 else "FAIL"
        return tag, f"{code or 'error'}: {str(message or '')[:70]}"
    try:
        detail, empty = summary(body if isinstance(body, (dict, str)) else {})
    except Exception as exc:  # a payload of an unexpected shape is reported, never raised
        return "FAIL", f"unexpected payload shape ({type(exc).__name__})"
    fixture, placeholder = markers(body)
    keys = DATE_KEYS
    if path == "/api/coverage":
        keys = ("latest_day_with_data",)
    elif path == "/api/fieldwork":
        keys = ("latest_day",)
    elif path.startswith("/api/seeds"):
        keys = ("seed_date",)  # the queue is dated tomorrow, so the newest day up to today is the last results
    latest = latest_date(body, keys, until=today) if dated else None
    if dated and path.startswith(("/api/today", "/api/trends/", "/api/discover", "/api/alerts")):
        latest = latest_date({"date": body.get("date")}, ("date",)) if isinstance(body, dict) else None
    if dated:
        detail = f"{age_text(latest, today)}; {detail}"
    if path == "/api/health" and not (isinstance(body, dict) and body.get("ok") is True):
        return "FAIL", detail  # a 200 health with ok not true (a failed check) is not a live service
    if fixture:
        return "FAKE", "fixture marker in payload; " + detail
    if placeholder:
        return "FAKE", "placeholder text in payload; " + detail
    if empty and page not in UNDATED_EMPTY_OK:
        return "EMPTY", detail
    if dated and latest and (today - latest).days > STALE_DAYS:
        return "STALE", detail
    return "OK", detail


def run_pages(reader, market, today, out=print):
    ids = {"market": market, "region": market.lower()}
    results = []
    # The gate: a read with no passcode must be refused.
    status, _b, ms, _e = reader.get("/api/today", gated=False)
    gate = "OK" if status == 401 else "FAIL"
    out(f"{'Gate':<14} {'/api/today (no passcode)':<44} {status or '-':>4} {ms:>6}ms {gate:<6} expects 401")
    results.append(("Gate", gate, "", ms))
    for page, template, summary, gated, dated in PAGES:
        path = fill(template, ids)
        if path is None:
            results.append((page, "SKIP", template))  # named in the summary, one line for all
            continue
        status, body, ms, err = reader.get(path, gated=gated)
        learn_ids(path.split("&limit")[0] if path.startswith("/api/discover?") else path, body, ids)
        tag, detail = judge(page, path, status, body, summary, dated, today, err)
        results.append((page, tag, path, ms))
        shown = re.sub(r"[0-9a-f]{40,}", lambda m: m.group(0)[:8] + "..", path)
        slow = f"SLOW {ms / 1000:.1f} s; " if ms / 1000 > SLOW_S else ""
        out(f"{page:<14} {shown[:44]:<44} {status or '-':>4} {ms:>6}ms {tag:<6} {(slow + detail)[:110]}")
    return results


def summarise(results, out=print):
    by_tag = {}
    seconds, slow = {}, []
    for page, tag, *rest in results:
        by_tag.setdefault(tag, [])
        if page not in by_tag[tag]:
            by_tag[tag].append(page)
        ms = rest[1] if len(rest) > 1 else None
        if ms is not None:
            seconds[page] = seconds.get(page, 0) + ms / 1000
            if ms / 1000 > SLOW_S:
                slow.append(f"{page} {ms / 1000:.1f} s")
    bad = {t: by_tag.get(t, []) for t in ("FAIL", "FAKE", "EMPTY", "STALE")}
    live = [p for p in by_tag.get("OK", []) if not any(p in v for v in bad.values())]
    out("")
    out("Summary")
    out(f"  live:   {', '.join(live) or 'none'}")
    for tag, words in (("FAIL", "broken"), ("FAKE", "fake (fixture or placeholder data)"),
                       ("EMPTY", "empty"), ("STALE", f"stale (latest date over {STALE_DAYS} day old)")):
        if bad[tag]:
            out(f"  {words}: {', '.join(bad[tag])}")
    if slow:
        out(f"  slow (a read over {SLOW_S:.0f} s): {', '.join(slow)}")
    if seconds:
        out("  seconds per page (every read added up): "
            + ", ".join(f"{page} {s:.1f} s" for page, s in seconds.items()))
    out(f"  moved to 42 pages (legacyRoutes.js, not probed): {moved_line(results)}")
    if by_tag.get("SKIP"):
        out(f"  not opened (no record to open yet): {', '.join(by_tag['SKIP'])}")
    return 1 if any(bad.values()) else 0


# BigQuery freshness (--bq).

CORE, AGENT = "intelligence_42_core", "intelligence_42_agent"
# (table, date expression, what reads it). Partition columns first, so each query reads three days at most.
SOURCES = (
    (f"{CORE}.post_observations", "observed_date", "every page (collect)"),
    (f"{CORE}.posts", "post_date", "evidence, people, seed path (collect)"),
    (f"{CORE}.collection_health", "day", "Today, Coverage, Fieldwork (collect)"),
    (f"{CORE}.item_counter_daily", "obs_date", "boards, Discover (collect)"),
    (f"{CORE}.credit_ledger", "trend_date", "Coverage, Fieldwork (collect)"),
    (f"{CORE}.google_search_signals", "DATE(fetched_at)", "Searching now strip (collect)"),
    (f"{CORE}.clusters", "cluster_date", "topics (understand, BERTopic)"),
    (f"{CORE}.item_daily", "metric_date", "Compare, Topic (detect aggregate)"),
    (f"{CORE}.series_test", "metric_date", "states, Discover (detect stats)"),
    (f"{CORE}.coord_signals", "metric_date", "authenticity flags (detect coaction)"),
    (f"{CORE}.item_state", "metric_date", "Discover, Topic, Alerts (detect)"),
    (f"{CORE}.breakout_signals", "metric_date", "Discover (detect breakout)"),
    (f"{CORE}.seed_queue", "seed_date", "next collect, news bridge (gdelt, detect seeds)"),
    (f"{CORE}.item_hourly", "DATE(hour)", "Breaking (pulse, off)"),
    (f"{CORE}.breaking_signals", "DATE(hour)", "Today Breaking (breaking, off)"),
    (f"{AGENT}.briefs", "brief_date", "Today, History (brief)"),
    (f"{AGENT}.watch_matches", "match_date", "Alerts (detect watch)"),
    (f"{AGENT}.forecasts", "issue_date", "forecasts (detect forecast)"),
    (f"{AGENT}.runs", "run_date", "Coverage, History, Schedules"),
)
# Undated or weekly tables: latest date and total rows.
LATEST_ONLY = (
    (f"{CORE}.calendar", "moment_date", "Today moments, Seasonal (calendar.py --apply, by hand; last moment)"),
    (f"{CORE}.calendar_analogues", "DATE(computed_at)", "Seasonal analogues (drift, weekly)"),
    (f"{CORE}.gdelt_daily", "day", "rising news entities (manual only)"),
    (f"{CORE}.test_switch", "switched_on", "statistical test switch (backtest --apply, manual)"),
    (f"{AGENT}.engine_scorecard", "week_start", "Coverage scorecard (learn, weekly)"),
    (f"{AGENT}.investigations", "DATE(created_at)", "Investigations"),
    (f"{AGENT}.dossier_versions", "DATE(created_at)", "Dossiers"),
    (f"{AGENT}.schedules", "DATE(COALESCE(status_at, created_at))", "Schedules"),
    (f"{AGENT}.findings", "DATE(as_of)", "History findings"),
)
# Views and table functions f42-api reads (core/api/store.py). A missing one empties its part of a page without an
# error, because the store treats it as not built yet. v_sensitive_items_complete stays missing while
# SENSITIVE_COMPLETE_READY in core/detect/job.py is False, which keeps creator item lists and named members off.
API_OBJECTS = ("v_good_runs", "v_briefs_current", "v_collection_health_current", "v_item_state_current",
               "v_series_test_current", "v_item_tone_daily", "v_item_gate_current", "v_item_market_scope",
               "tvf_item_timeseries", "v_item_waves", "v_coord_signals_current", "v_item_spread", "v_item_origin",
               "v_news_followthrough", "v_item_evidence", "v_item_daily_current", "v_breaking_signals_current",
               "v_suppressed_creators", "v_sensitive_items", "v_sensitive_items_complete", "watch_matches",
               "engine_scorecard", "cultural_map", "breakout_signals")

# core/detect/stats.py TESTABLE: the baseline states past warm-up that a switched-on key tests.
TESTABLE_STATES = "('short', 'ok')"
# Stages that should have an ok run every day (the morning chain, its detect steps, reconcile and the watchdog).
DAILY = {"collect", "understand", "aggregate", "stats", "coaction", "detect", "breakout", "watch", "seeds",
         "forecast", "brief", "reconcile", "watchdog"}
# Reconcile runs at 23:30 SAST for yesterday (core/collect/reconcile.py run_day), so through the day its newest ok
# run_date is two days back; it gets one day more before it is marked. Its job times out after 15 minutes
# (core/setup/deploy_jobs.py), so from 23:45 SAST tonight's run is due and the extra day no longer applies.
LAG_DAYS = {"reconcile": 1}
LAG_UNTIL = {"reconcile": clock(23, 45)}
# Ask and investigation runs finish as complete, not ok (core/api/agent_app.py sink, core/api/investigations.py), and
# scheduled runs hand over to an ask, so for these a complete run is their ok run. Daily stages are ok only on ok.
DONE_IS_OK = {"ask", "investigation", "scheduled"}
# CO-8 (remaining-2026-10-04): distinct TikTok creators per market over the last 7 days, by the post's own market
# and by the market whose collection saw it, with how many came from that market's own feeds: a sighting whose
# source_market is the market (docs/full-42/TRUST.md market by source, core/api/store.py own_feed).
CREATORS_BY_POST = (f"SELECT p.geo_market AS market, COUNT(DISTINCT p.creator_id) AS creators, COUNT(*) AS posts "
                    f"FROM `{PROJECT}.{CORE}.posts` p WHERE p.platform = 'tiktok' "
                    "AND p.post_date BETWEEN @start AND @today GROUP BY market ORDER BY creators DESC")
CREATORS_BY_COLLECTION = (
    "SELECT o.market AS market, COUNT(DISTINCT p.creator_id) AS creators, "
    "COUNT(DISTINCT IF(o.source_market = o.market, p.creator_id, NULL)) AS own_feed_creators, "
    "COUNT(DISTINCT o.post_id) AS posts "
    f"FROM `{PROJECT}.{CORE}.post_observations` o JOIN `{PROJECT}.{CORE}.posts` p ON p.post_id = o.post_id "
    "WHERE o.platform = 'tiktok' AND p.platform = 'tiktok' AND o.observed_date BETWEEN @start AND @today "
    "GROUP BY market ORDER BY creators DESC")
STAGES = ("collect", "understand", "aggregate", "stats", "coaction", "detect", "breakout", "watch", "seeds",
          "forecast", "brief", "reconcile", "watchdog", "learn", "scheduled", "digest", "pulse", "breaking",
          "backtest", "ask", "investigation")


def select_only(sql):
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in ("SELECT", "WITH"):
        raise Refused("only SELECT statements are run")
    return sql


class BQ:
    def __init__(self, client=None):
        from google.cloud import bigquery

        self.bigquery = bigquery
        self.client = client or bigquery.Client(project=PROJECT)

    def rows(self, sql, **params):
        qp = [self.bigquery.ScalarQueryParameter(k, "DATE", v) for k, v in params.items()]
        cfg = self.bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES, query_parameters=qp)
        return [dict(r.items()) for r in self.client.query(select_only(sql), job_config=cfg).result()]


def lag_days(stage, now):
    """The extra days a stage gets before it is marked late at now (a datetime, or None for any time of day)."""
    until = LAG_UNTIL.get(stage)
    if until is not None and now is not None and now.astimezone(SAST).time() >= until:
        return 0
    return LAG_DAYS.get(stage, 0)


def creator_counts(bq, today, out=print):
    """CO-8: one line per view of distinct TikTok creators per market, over today and the 7 days before it."""
    start = today - timedelta(days=7)
    out(f"  TikTok creators {start.isoformat()} to {today.isoformat()} (distinct creator_id, posts in brackets; "
        "by post market counts posts dated in the window, by collected market counts posts sighted in it)")
    try:
        by_post = bq.rows(CREATORS_BY_POST, start=start, today=today)
        by_seen = bq.rows(CREATORS_BY_COLLECTION, start=start, today=today)
    except Exception as exc:
        out(f"  TikTok creators read failed ({type(exc).__name__})")
        return
    out("    by post market: " + ("; ".join(
        f"{r.get('market') or 'unlocated'} {r.get('creators') or 0:,} ({r.get('posts') or 0:,} posts)"
        for r in by_post) or "none"))
    out("    by collected market: " + ("; ".join(
        f"{r.get('market') or 'unknown'} {r.get('creators') or 0:,}, "
        f"{r.get('own_feed_creators') or 0:,} from own feeds ({r.get('posts') or 0:,} posts)"
        for r in by_seen) or "none"))


def run_bq(bq, today, out=print, now=None):
    since = today - timedelta(days=2)
    out("")
    out(f"BigQuery freshness ({PROJECT}, rows of {since.isoformat()} to {today.isoformat()})")
    stale = 0
    cells = []
    for table, col, what in SOURCES:
        sql = (f"SELECT MAX({col}) AS latest, COUNTIF({col} >= @since) AS last3 FROM `{PROJECT}.{table}` "
               f"WHERE {col} >= DATE_SUB(@since, INTERVAL 28 DAY)")
        try:
            row = bq.rows(sql, since=since)[0]
        except Exception as exc:
            cells.append(f"{table.split('.')[-1]:<21} read failed ({type(exc).__name__})")
            stale += 1
            continue
        latest = row.get("latest")
        old = latest is None or (today - latest).days > STALE_DAYS
        switched_off = what.endswith("off)")  # written only once a job the owner keeps off is switched on
        stale += old and not switched_off
        mark = "off" if switched_off and old else "STALE" if old else "ok"
        cells.append(f"{table.split('.')[-1]:<21} {mark:<5} {latest.isoformat() if latest else 'none in 28d':<11} "
                     f"{row.get('last3') or 0:>9,}")
    head = f"{'table':<21} {'check':<5} {'latest':<11} {'3d rows':>9}"
    out(f"  {head}   {head}")
    for i in range(0, len(cells), 2):
        out("  " + "   ".join(cells[i:i + 2]))
    once = []
    for table, col, what in LATEST_ONLY:
        try:
            row = bq.rows(f"SELECT MAX({col}) AS latest, COUNT(*) AS total FROM `{PROJECT}.{table}`")[0]
        except Exception as exc:
            once.append(f"{table.split('.')[-1]} read failed ({type(exc).__name__})")
            continue
        once.append(f"{table.split('.')[-1]} {row.get('latest') or 'none'} ({row.get('total')} rows)")
    out("  Weekly or by hand: " + "; ".join(once[:5]))
    out("  Saved by people: " + "; ".join(once[5:]))
    try:
        names = set()
        for ds in (CORE, AGENT):
            names |= {r["n"] for r in bq.rows(
                f"SELECT t.table_name AS n FROM `{PROJECT}.{ds}.INFORMATION_SCHEMA.TABLES` t UNION ALL "
                f"SELECT r.routine_name AS n FROM `{PROJECT}.{ds}.INFORMATION_SCHEMA.ROUTINES` r")}
        missing = [o for o in API_OBJECTS if o not in names]
        out("  API views missing (their page parts stay empty): " + (", ".join(missing) or "none"))
        stale += any(o != "v_sensitive_items_complete" for o in missing)
    except Exception as exc:
        out(f"  API views read failed ({type(exc).__name__})")
        stale += 1  # the required views are not shown to exist, so the check does not pass
    try:
        tested = bq.rows(f"SELECT COUNTIF(t.test != 'none') AS tested, COUNT(*) AS total, "
                         f"COUNTIF(t.baseline_state IN {TESTABLE_STATES}) AS testable FROM "
                         f"`{PROJECT}.{CORE}.series_test` t WHERE t.metric_date >= @since", since=since)[0]
        out(f"  series_test tested     {tested.get('tested')} of {tested.get('total')} series in 3 days ran the "
            f"statistical test; {tested.get('testable')} were past warm-up (short or ok). A series is tested only "
            "when past warm-up and its key has a test_switch row (backtest --apply), so 0 tested with some past "
            "warm-up means no key is switched on")
    except Exception as exc:
        out(f"  series_test tested     read failed ({type(exc).__name__})")
    creator_counts(bq, today, out=out)
    out("Latest run per stage (intelligence_42_agent.runs, latest row per run_id, 60 days)")
    sql = (f"WITH latest AS (SELECT r.stage, r.run_date, r.status, r.finished_at FROM `{PROJECT}.{AGENT}.runs` r "
           f"WHERE r.run_date >= DATE_SUB(@since, INTERVAL 58 DAY) "
           "QUALIFY ROW_NUMBER() OVER (PARTITION BY r.run_id ORDER BY r.finished_at DESC NULLS LAST) = 1) "
           "SELECT stage, MAX(IF(status = 'ok', run_date, NULL)) AS last_ok, "
           "MAX(IF(status = 'complete', run_date, NULL)) AS last_complete, MAX(run_date) AS last_any, "
           "ARRAY_AGG(status ORDER BY run_date DESC, finished_at DESC LIMIT 1)[OFFSET(0)] AS last_status "
           "FROM latest GROUP BY stage")
    try:
        found = {r["stage"]: r for r in bq.rows(sql, since=since)}
    except Exception as exc:
        out(f"  runs read failed ({type(exc).__name__})")
        return stale + 1
    line = []
    for stage in STAGES:
        r = found.get(stage)
        if r is None:
            line.append(f"{stage} never{'!' if stage in DAILY else ''}")
            stale += stage in DAILY
            continue
        ok = r.get("last_ok")
        done = ("ok", "complete") if stage in DONE_IS_OK else ("ok",)
        if stage in DONE_IS_OK and r.get("last_complete"):
            ok = max(d for d in (ok, r["last_complete"]) if d)
        late = stage in DAILY and (ok is None or (today - ok).days > STALE_DAYS + lag_days(stage, now))
        stale += late
        line.append(f"{stage} {ok.isoformat()[5:] if ok else 'no ok'}{'!' if late else ''}"
                    + ("" if r.get("last_status") in done else f" (last {r.get('last_status')})"))
    for i in range(0, len(line), 6):
        out("  " + "; ".join(line[i:i + 6]))
    out(f"  ! marks a daily stage with no ok run since {(today - timedelta(days=STALE_DAYS)).isoformat()} "
        f"(reconcile {(today - timedelta(days=STALE_DAYS + lag_days('reconcile', now))).isoformat()})")
    return stale


def main(argv=None, client=None, bq=None, now=None, out=print):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("--market", default="ZA", choices=MARKETS)
    parser.add_argument("--bq", action="store_true", help="add the BigQuery freshness section")
    args = parser.parse_args(argv)
    passcode = os.environ.get("F42_SMOKE_PASSCODE", "")
    if not passcode:
        print("F42_SMOKE_PASSCODE is not set. Load the passcode into it and run again.", file=sys.stderr)
        return 2
    try:
        base = check_base(args.base_url)
    except Refused as exc:
        print(f"Refused: {exc}.", file=sys.stderr)
        return 2
    if out is print and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a payload's words never stop the run on a narrow console
    today = today_sast(now)
    reader = Reader(base, passcode, client=client)
    out(f"42 page data check, {base}, {today.isoformat()} SAST, market {args.market}. GET only.")
    out(f"{'Page':<14} {'Endpoint':<44} {'HTTP':>4} {'Time':>8} {'Check':<6} Content")
    results = run_pages(reader, args.market, today, out=out)
    code = summarise(results, out=out)
    if args.bq:
        try:
            stale = run_bq(bq or BQ(), today, out=out, now=now or datetime.now(SAST))
        except Exception as exc:
            out(f"BigQuery section could not run ({type(exc).__name__}).")
            stale = 1
        code = code or (1 if stale else 0)
    return code


if __name__ == "__main__":
    sys.exit(main())
