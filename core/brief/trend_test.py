"""The trend test in shadow (contract TREND-TEST-v1, Albert 12 Oct 2026: "go trend test").

After a brief run has finished, record for every candidate and published card of the stored brief a label, TREND, NEWS,
MIXED or UNKNOWN, with every figure it was computed from, as one row of intelligence_42_agent.trend_test_private
(core/schema/trend_test.sql). It records only. No gate, ranking, payload or display reads it, the Ask SQL tool refuses
the table by its suffix, and the cut-offs below are placeholders until Albert sets them on a week of rows.

The inputs come from the core tables, not from the card: the item's posts in the market for the seven days ending at
the brief date, observed before the brief was published and not on one of 42's own search lanes (sql/trend_test.sql).
The one input read from the card is whether its explanation names a dated single event, and it never decides alone:
it counts towards news only beside a one-day spike of new accounts, which comes from the posts. The critic's
news_driven is recorded and not used (it was true on all 11 cards of 1 to 11 October).

    outlet share   posts from the news feed platform or a draft registry outlet (core/config/hubs.yaml) over all posts
    own share      accounts with at least one post of their own (not an outlet, not a repost, not a link to an
                   outlet host) over accounts posting
    growth days    days in the window on which a new non-outlet account first posted; peak day share is the most
                   new accounts on one day over all non-outlet accounts
    format         the most non-outlet accounts on one shared sound, on a challenge hashtag, or on one repeated caption

    news axis   outlet share at the cut-off, or a dated single event in the explanation with a one-day spike
    trend axis  own share at the cut-off, with a format signal or enough growth days
    label       MIXED on both axes, NEWS or TREND on one, UNKNOWN on neither, on too few posts, on a population that
                could not be read, and when a missing explanation would decide the news axis

The hubs.yaml registry is a draft (status draft, confirmed_by null in every market). The pack treats it as no
authority (pack_order.outlet_keys is empty); this shadow reads its news and sport entries anyway, records how many and
its status on every row, and counts its matches apart from the news feed posts.

Entry point: core/brief/job.py main calls record_quietly once the run has finished ok, so nothing here can change the
brief, its runs row or the job's exit code. Spend: SELECT only, at most MAX_BYTES_BILLED a statement, no model call.
"""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from google.cloud import bigquery

from core.api.store import creator_key
from core.brief import pack_order
from core.detect import sqlrun
from core.detect.sqlrun import AGENT, CORE

RULE_VERSION = "trend_test.v1"
SCHEMA_VERSION = 1
TABLE = "trend_test_private"
WINDOW_DAYS = 7
# 42's own searches and the lanes the locality population leaves out (C4 v3): what 42 went looking for is not evidence
# of how the item spreads.
EXCLUDED_LANES = ("confirm", "placebo", "agent_live", "legacy")
MAX_BYTES_BILLED = 2 * 1024 ** 3
JOB_TIMEOUT_MS = 120_000
INSERT_BATCH = 500
# Placeholders for the shadow week. Albert sets the real cut-offs from the recorded figures (W8 decision pending);
# changing one is a new RULE_VERSION, never an edit under this one.
CUTOFFS = {"min_posts": 5, "outlet_share": 0.25, "own_share": 0.5, "peak_day_share": 0.5, "growth_days": 4,
           "format_accounts": 3}
LARGE_TIERS = ("macro", "mega")
HUBS_YAML = pack_order.HUBS_YAML
SQL = Path(__file__).parent / "sql" / "trend_test.sql"
QUERIES = {re.search(r"-- name: (\w+)", s).group(1): s for s in sqlrun.split(SQL.read_text(encoding="utf-8"))}

COLUMNS = (
    "run_date", "market", "item_id", "brief_run_id", "rule_version", "schema_version", "computed_at",
    "population_cutoff", "window_start", "window_end", "placement", "held_rule", "label", "reason", "news_axis",
    "trend_axis", "registry_status", "registry_keys", "outlet_hosts", "posts", "keyless_posts", "accounts",
    "located_posts", "feed_posts", "registry_posts", "outlet_posts", "outlet_accounts", "outlet_share", "share_posts",
    "rt_posts", "outlet_link_posts", "link_posts", "own_accounts", "own_share", "active_days", "growth_days",
    "peak_day_share", "first_post_date", "last_post_date", "top_sound_accounts", "challenge_accounts",
    "caption_accounts", "format_signal", "cause_dated_event", "cause_cue", "news_driven", "verified_accounts",
    "large_accounts")
FIGURES = COLUMNS[COLUMNS.index("outlet_hosts"):COLUMNS.index("cause_dated_event")] + ("verified_accounts",
                                                                                       "large_accounts")

# The cause: an event word in the explanation's "because" clause (the whole text when it has none) and a date anywhere in
# the explanation, which often places the event before the clause ("on 11 Oct, likely because ..."). An event word is
# matched by its stem, so "defeated" and "announced" count as their nouns do.
_BECAUSE = re.compile(r"\bbecause\b(.*)", re.I | re.S)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"|" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day|yesterday|today|tonight|last night|this weekend|last weekend"
    r"|on the \d{1,2}(?:st|nd|rd|th))\b", re.I)
_EVENT = re.compile(
    r"\b(?:match(?:es)?|final|semi-final|game|fixture|friendl(?:y|ies)|derby|qualif\w*|race|fight"
    r"|releas\w*|dropped|premiere\w*|launch\w*|unveil\w*|announc\w*|press conference|speech\w*|interview\w*"
    r"|ceremon\w*|memorial\w*|anniversar\w*|funeral\w*|burial\w*|wedding\w*|inaugurat\w*|swearing-in"
    r"|independence|holiday\w*|celebration\w*|election\w*|by-election\w*|vote|voting|budget|ruling\w*|verdict\w*"
    r"|judg(?:e)?ment\w*|court|hearing\w*|arrest\w*|strike\w*|protest\w*|attack\w*|crash\w*|accident\w*"
    r"|flood\w*|storm\w*|death\w*|died|killed|concert\w*|festival\w*|award\w*|summit\w*|conference\w*|rall(?:y|ies)"
    r"|groundbreaking|signing\w*|transfer\w*|win|wins|won|beat|beats|victor\w*|defeat\w*|draw|drew|lost"
    r"|appoint\w*|resign\w*|sacked)\b", re.I)
_URL = re.compile(r"https?://[^\s<>\"')]+", re.I)
_MENTION = re.compile(r"[@#][\w.]+")
_WORD = re.compile(r"[a-z\u00c0-\u024f']+")


def cause(text):
    """(True, "date|event") when the explanation names a dated single event, (False, None) when it does not, and
    (None, None) when there is no explanation to read. The cue holds the two matched words, never a name."""
    if not text or not str(text).strip():
        return None, None
    m = _BECAUSE.search(text)
    when, what = _DATE.search(text), _EVENT.search(m.group(1) if m else text)
    if when and what:
        return True, f"{when.group(0).lower()}|{what.group(0).lower()}"
    return False, None


def registry(path=HUBS_YAML):
    """The draft outlet registry as sorted platform:handle keys (news and sport entries, as pack_order reads them once
    confirmed) with its top-level status. An unreadable file gives no keys and status None."""
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {"status": None, "keys": []}
    if not isinstance(doc, dict) or not isinstance(doc.get("markets"), dict):
        return {"status": None, "keys": []}
    keys = set()
    for block in doc["markets"].values():
        for entries in (block.values() if isinstance(block, dict) else ()):
            for entry in (entries if isinstance(entries, list) else ()):
                if isinstance(entry, dict) and entry.get("kind") in pack_order.OUTLET_KINDS:
                    key = creator_key(entry.get("platform"), str(entry.get("handle") or ""))
                    if key:
                        keys.add(key)
    status = doc.get("status")
    return {"status": str(status) if status is not None else None, "keys": sorted(keys)}


def _host(url):
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _caption(text):
    words = _WORD.findall(_MENTION.sub(" ", _URL.sub(" ", text or "")).lower())
    return " ".join(words[:4]) if len(words) >= 3 else None


def _share(count, total):
    return round(count / total, 6) if total else None


def figures(rows, market, registry_keys, outlet_links):
    """Every input figure of one item in one market from its population rows (the population statement's columns).
    rows None means the population could not be read: every figure is None. An empty list is a valid zero."""
    if rows is None:
        return {name: None for name in FIGURES}
    registry_keys = set(registry_keys)
    hosts = {h for h in (_host(u) for u in outlet_links) if h}
    accounts, outlets, own, verified, large = set(), set(), set(), set(), set()
    first_day, sounds, challenges, captions = {}, defaultdict(set), set(), defaultdict(set)
    n = Counter()
    days = set()
    for r in rows:
        platform, text = r.get("platform"), r.get("text") or ""
        handle_key = creator_key(platform, r.get("handle") or "")
        if platform == "news":
            account = f"news:{_host(r.get('url') or '')}" if _host(r.get("url") or "") else None
        elif handle_key:
            account = handle_key
        elif r.get("creator_id"):
            account = f"{platform}:id:{r['creator_id']}"
        else:
            account = None
        feed, listed = platform == "news", bool(handle_key and handle_key in registry_keys)
        outlet = feed or listed
        links = _URL.findall(text)
        retweet = text.lstrip().startswith("RT @")
        outlet_link = any(_host(link) in hosts for link in links)
        shared = not outlet and (retweet or outlet_link)
        n["posts"] += 1
        n["feed_posts"] += feed
        n["registry_posts"] += listed and not feed
        n["outlet_posts"] += outlet
        n["located_posts"] += (r.get("geo_market") or "").strip().upper() == market
        if not outlet:
            n["rt_posts"] += retweet
            n["outlet_link_posts"] += outlet_link
            n["share_posts"] += shared
            n["link_posts"] += bool(links)
        if r.get("post_date") is not None:
            days.add(r["post_date"])
        if account is None:
            n["keyless_posts"] += 1
            continue
        accounts.add(account)
        if r.get("verified") is True:
            verified.add(account)
        if (r.get("tier") or "") in LARGE_TIERS:
            large.add(account)
        if outlet:
            outlets.add(account)
            continue
        if r.get("post_date") is not None:
            first_day[account] = min(first_day.get(account, r["post_date"]), r["post_date"])
        if not shared:
            own.add(account)
            if not links:
                key = _caption(text)
                if key:
                    captions[key].add(account)
        if r.get("sound_id"):
            sounds[r["sound_id"]].add(account)
        if any("challenge" in str(tag).lower() for tag in (r.get("hashtags") or [])):
            challenges.add(account)
    new_per_day = Counter(first_day.values())
    makers = len({a for a in accounts if a not in outlets})
    top_sound = max((len(v) for v in sounds.values()), default=0)
    caption_top = max((len(v) for v in captions.values()), default=0)
    return {
        "outlet_hosts": len(hosts), "posts": n["posts"], "keyless_posts": n["keyless_posts"], "accounts": len(accounts),
        "located_posts": n["located_posts"], "feed_posts": n["feed_posts"], "registry_posts": n["registry_posts"],
        "outlet_posts": n["outlet_posts"], "outlet_accounts": len(outlets),
        "outlet_share": _share(n["outlet_posts"], n["posts"]), "share_posts": n["share_posts"],
        "rt_posts": n["rt_posts"], "outlet_link_posts": n["outlet_link_posts"], "link_posts": n["link_posts"],
        "own_accounts": len(own), "own_share": _share(len(own), len(accounts)), "active_days": len(days),
        "growth_days": len(new_per_day), "peak_day_share": _share(max(new_per_day.values(), default=0), makers),
        "first_post_date": min(days) if days else None, "last_post_date": max(days) if days else None,
        "top_sound_accounts": top_sound, "challenge_accounts": len(challenges), "caption_accounts": caption_top,
        "format_signal": max(top_sound, len(challenges), caption_top) >= CUTOFFS["format_accounts"],
        "verified_accounts": len(verified), "large_accounts": len(large)}


def classify(fig, cutoffs=CUTOFFS):
    """(label, reason, news_axis, trend_axis) for one item's figures under the v1 table (module docstring)."""
    if fig.get("posts") is None:
        return "UNKNOWN", "population_unreadable", None, None
    if fig["posts"] < cutoffs["min_posts"]:
        return "UNKNOWN", "too_few_posts", None, None
    if fig.get("own_share") is None:
        return "UNKNOWN", "no_accounts", None, None
    trend = fig["own_share"] >= cutoffs["own_share"] and (
        bool(fig.get("format_signal")) or (fig.get("growth_days") or 0) >= cutoffs["growth_days"])
    reported = (fig.get("outlet_share") or 0) >= cutoffs["outlet_share"]
    spike = fig.get("peak_day_share") is not None and fig["peak_day_share"] >= cutoffs["peak_day_share"]
    dated = fig.get("cause_dated_event")
    if dated is None and spike and not reported:
        return "UNKNOWN", "cause_missing", None, trend
    news = reported or (dated is True and spike)
    if news and trend:
        return "MIXED", "both_axes", True, True
    if news:
        return "NEWS", "news_axis", True, False
    if trend:
        return "TREND", "trend_axis", False, True
    return "UNKNOWN", "neither_axis", False, False


def candidates(payload):
    """Every candidate of one stored market brief, once each: the cards, the more list, the held items and the items
    merged into a card or a hold, in that order. Only a card or a more entry carries an explanation."""
    out, seen = [], set()

    def add(item_id, placement, held_rule=None, explanation=None, news_driven=None):
        if item_id and item_id not in seen:
            seen.add(item_id)
            out.append({"item_id": item_id, "placement": placement, "held_rule": held_rule,
                        "explanation": explanation, "news_driven": news_driven})

    shown = [("card", c) for c in payload.get("cards") or []] + [("more", c) for c in payload.get("more") or []]
    held = (payload.get("held_back") or {}).get("items") or []
    for placement, c in shown:
        add(c.get("item_id"), placement, explanation=c.get("explanation"), news_driven=c.get("news_driven"))
    for h in held:
        add(h.get("item_id"), "held", held_rule=h.get("rule"))
    for _, c in shown + [("held", h) for h in held]:
        for merged in c.get("also") or []:
            add(merged.get("item_id") if isinstance(merged, dict) else None, "merged")
    return out


def _payload(value):
    while isinstance(value, str):
        value = json.loads(value)
    return value or {}


def _as_datetime(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _query(client, name, params, core, agent):
    config = bigquery.QueryJobConfig(
        query_parameters=[sqlrun._param(k, v) for k, v in params.items()],
        maximum_bytes_billed=MAX_BYTES_BILLED, job_timeout_ms=JOB_TIMEOUT_MS)
    job = client.query(sqlrun.render(QUERIES[name], core, agent), job_config=config)
    return [dict(row.items()) for row in job.result()]


def _json_ready(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def build_rows(client, d, *, core=CORE, agent=AGENT, now=None, registry_path=HUBS_YAML):
    """The shadow rows of day d's stored brief, one per market and candidate, in COLUMNS order. Writes nothing."""
    now = now or datetime.now(timezone.utc)
    reg = registry(registry_path)
    rows = []
    for brief in _query(client, "briefs", {"d": d}, core, agent):
        listed = candidates(_payload(brief["payload"]))
        if not listed:
            continue
        market, cutoff = brief["market"], _as_datetime(brief["published_at"])
        ids = [c["item_id"] for c in listed]
        params = {"d": d, "market": market, "cutoff": cutoff, "excluded_lanes": ",".join(EXCLUDED_LANES),
                  "item_ids": ",".join(ids)}
        try:
            if any("," in i for i in ids):
                raise ValueError("an item id holds a comma")
            population = defaultdict(list)
            for r in _query(client, "population", params, core, agent):
                population[r["item_id"]].append(r)
            links = [r["url"] for r in _query(client, "outlet_links", {k: params[k] for k in ("d", "market", "cutoff")},
                                              core, agent)]
        except Exception as e:
            print(f"trend test {d.isoformat()} {market}: population not read ({type(e).__name__}: {str(e)[:200]})",
                  file=sys.stderr)
            population, links = None, []
        for c in listed:
            fig = figures(None if population is None else population.get(c["item_id"], []), market, reg["keys"], links)
            dated, cue = cause(c["explanation"])
            label, reason, news_axis, trend_axis = classify({**fig, "cause_dated_event": dated})
            row = {
                "run_date": d, "market": market, "item_id": c["item_id"], "brief_run_id": brief["run_id"],
                "rule_version": RULE_VERSION, "schema_version": SCHEMA_VERSION, "computed_at": now,
                "population_cutoff": cutoff, "window_start": d - timedelta(days=WINDOW_DAYS - 1), "window_end": d,
                "placement": c["placement"], "held_rule": c["held_rule"], "label": label, "reason": reason,
                "news_axis": news_axis, "trend_axis": trend_axis, "registry_status": reg["status"],
                "registry_keys": len(reg["keys"]), **fig, "cause_dated_event": dated, "cause_cue": cue,
                "news_driven": c["news_driven"]}
            rows.append({k: _json_ready(row[k]) for k in COLUMNS})
    return rows


def record(client, d, *, core=CORE, agent=AGENT, now=None):
    """Append day d's shadow rows and return {rows, labels}."""
    rows = build_rows(client, d, core=core, agent=agent, now=now)
    for i in range(0, len(rows), INSERT_BATCH):
        errors = client.insert_rows_json(f"{agent}.{TABLE}", rows[i:i + INSERT_BATCH])
        if errors:
            raise RuntimeError(f"append to {agent}.{TABLE} failed after {i} rows: {str(errors)[:300]}")
    return {"rows": len(rows), "labels": dict(sorted(Counter(r["label"] for r in rows).items()))}


def record_quietly(client, d, *, core=CORE, agent=AGENT):
    """record, with every failure printed and swallowed: the shadow never changes the brief's outcome."""
    try:
        summary = record(client, d, core=core, agent=agent)
    except Exception as e:
        print(f"brief {d.isoformat()}: trend test not recorded ({type(e).__name__}: {str(e)[:300]})", file=sys.stderr)
        return None
    print(json.dumps({"trend_test": d.isoformat(), "rule_version": RULE_VERSION, **summary}), file=sys.stderr)
    return summary
