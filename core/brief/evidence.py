"""The SQL evidence pack for one morning-brief card (BUILD.md 1.12, TRUST.md section 3 step 4).

    build_pack(client, item_row, d, market, *, core=CORE, agent=AGENT, hidden=None, moments=())
        -> (pack, sparkline, rerun)
    read_hidden(client, *, core=CORE, agent=AGENT) -> (creator keys, creator ids, names) of the suppression list

item_row: a v_item_state_current row as a dict (item_id, run_id, state, untested, main_series_id, ...).
d: the brief date. market: "ZA", "NG" or "KE". client: a google.cloud.bigquery Client.

pack is the shape core/brief/explain.py reads:
    evidence: up to 12 contract Evidence dicts (core/api/contract.md section 4), members of the item's cluster in
        the market's own run first and the rest in the old order (core/brief/pack_order.py, C4 v2 section 19).
        text is the excerpt shown on the card, at most 280 characters of the post text or else its transcript;
        quote_text is the whole stored text. market is the post's located market: its geo_market when geo_confidence is 0.7 or more
        and geo_source is one collect treats as known (KNOWN_GEO in core/collect/writers.py; DATA.md 3.6,
        TRUST.md A5). A post with no known location gets market
        None and the flag market_assumed, never the market it was sighted in, so K3 and K5 treat it as not
        located. The sponsored flag is the enrich model's paid marker or the vendor's paid label
        (posts.vendor_labels). sponsor_checked is true when either gave a reading for the post, so a post with
        sponsor_checked true and no sponsored flag was checked and shows no paid label; false means unknown.
    numbers: {value, unit, query_id, run_id, result_hash} for creators3, posts3 and, when the item was tested,
        main_ratio. result_hash is "sha256:" plus the hex sha256 of the canonical JSON of the query's rows.
    facts: short plain lines about the state, the 3-day counts and the first sighting, then one line per local
        post with its weekday and market-local date, naming a calendar moment only when moments (the market's
        calendar rows job.py reads) has one on that date.
stages: an optional dict build_pack fills with the counts of posts after each stage of the pack (available, after
    the creator cap, after the outlet cap), which pack_order.hold_detail turns into the cause of a floor hold. The
    pack itself keeps its three keys.
sparkline: {"unit", "points": [{date, value, expected_low, expected_high}]} over the main series' last 14
    days, value None on invalid days and before the series started; None when the item has no main series.
hidden: what read_hidden returned, read here when not given. A suppressed creator's posts never enter the pack
(SETUP.md data protection): evidence.sql leaves out their creator ids before it ranks, so other posts fill the pack,
and the pack is then put through core/api/today.py without_hidden, which Today and Discover read briefs through:
a post whose author matches a suppressed creator by creator_key leaves, and their handle is masked in all other
text. A list that cannot be read raises SuppressionUnreadable, so no pack is stored with a person who may be on it.
rerun(entry): re-executes the query named by entry["query_id"] with the same params and returns its value
    now (None when it returns no row). Number queries read item_state pinned to the detect run_id, so a newer
    detect run does not change them. An unknown query_id raises KeyError.
"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from google.cloud import bigquery

from core.api.store import creator_key
from core.api.today import without_hidden
from core.brief import pack_order
from core.brief.gatectx import paid_markers
from core.brief.payload import STATE_WORDS
from core.brief.specificity import local_posts
from core.collect.writers import KNOWN_GEO
from core.detect import sqlrun
from core.detect.sqlrun import AGENT, CORE

SQL = Path(__file__).parent / "sql" / "evidence.sql"
# Market-local time: SAST, WAT and EAT, none of which keeps daylight saving.
OFFSETS = {"ZA": 2, "NG": 1, "KE": 3}
EXCERPT = 280
# DATA.md 3.6: a place counts at geo_confidence 0.7 or more from a KNOWN_GEO source; language alone never counts.
GEO_CONFIDENT = 0.7
WINDOW_DAYS, SPARK_DAYS = 7, 14
NUMBERS = (("creators3", "creators in 3 days"), ("posts3", "posts in 3 days"), ("main_ratio", "times usual"))
SPARK_UNITS = {"panel": "posts a day", "unbiased_rank": "list appearances a day",
               "unbiased_counter": "counter rise a day"}

_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)


def _queries():
    return {_NAME.search(stmt).group(1): stmt for stmt in sqlrun.split(SQL.read_text(encoding="utf-8"))}


QUERIES = _queries()


class SuppressionUnreadable(RuntimeError):
    """The suppression list could not be read, so nothing that may name a suppressed person is written."""


def read_hidden(client, *, core=CORE, agent=AGENT):
    """(creator keys, creator ids, names) of the suppression list, in the shape core/api/today.py hidden_people
    gives: the ids from v_suppressed_creators and the creator_key of each one's creators rows. names is empty: the
    brief does not read the map labels of their YouTube channels, so without_hidden masks a display name here only
    where the value itself names them (their own creator item, an author on a left-out post), and Today, Discover and
    History mask the channel labels again when they read the stored brief through hidden_people."""
    try:
        rows = sqlrun.query(client, QUERIES["suppressed"], {}, core=core, agent=agent)
    except Exception as e:
        raise SuppressionUnreadable(f"the suppression list (v_suppressed_creators) could not be read "
                                    f"({type(e).__name__}); nothing is written that may name a suppressed "
                                    "person") from e
    return ({creator_key(r["platform"], r["handle"]) for r in rows} - {None},
            {r["creator_id"] for r in rows if r["creator_id"]}, set())


def result_hash(rows):
    text = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _query_id(name, params):
    key = json.dumps({"query": name, "sql": QUERIES[name], "params": params}, sort_keys=True, default=str)
    return f"q_{name}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"


def _pack_rows(client, params, core, agent):
    """The evidence statement. The outlet registry is an array parameter, which sqlrun.query cannot type (an empty
    one has no element to type it by), so the query config is built here."""
    keys = bigquery.ArrayQueryParameter("outlet_keys", "STRING", pack_order.outlet_keys())
    config = bigquery.QueryJobConfig(query_parameters=[sqlrun._param(k, v) for k, v in params.items()] + [keys])
    return [dict(r.items()) for r in client.query(sqlrun.render(QUERIES["evidence"], core, agent),
                                                  job_config=config).result()]


def _value(rows):
    return rows[0]["value"] if rows else None


def _located(row):
    """The post's own known market, or None. The market it was sighted in is never a location."""
    known = (row["geo_confidence"] or 0) >= GEO_CONFIDENT and row["geo_source"] in KNOWN_GEO
    return (row["geo_market"] or None) if known else None


BRAND_KEY_MIN = 3  # letters and digits; a shorter brand key never marks an author


def _alnum(value):
    return "".join(ch for ch in unicodedata.normalize("NFKC", str(value or "")).casefold() if ch.isalnum())


def _brand_owned(row, item):
    """True when the item is a brand and the author's handle is the brand's own key or label (K5: the brand talking,
    not a person). The tables hold no list of brand accounts, so no other handle is read as one."""
    if not item or item.get("kind") != "brand" or not row["handle"]:
        return False
    handle = _alnum(row["handle"])
    return len(handle) >= BRAND_KEY_MIN and handle in {_alnum(item.get("canonical_key")), _alnum(item.get("label"))}


def _record(row, tz, item=None):
    full = row["quote_text"] or ""
    located = _located(row)
    paid = paid_markers({"text": full, "quote_text": full}, row.get("hashtags"))
    flags = [name for name, on in (("flagged", row["flagged"]), ("sponsored", row["sponsored"]), ("paid", paid),
                                   ("brand_owned", _brand_owned(row, item)),
                                   ("near_duplicate", row["near_dup"]), ("market_assumed", located is None)) if on]
    return {
        "id": row["post_id"], "platform": row["platform"], "handle": row["handle"], "url": row["url"],
        "posted_at": row["published_at"].astimezone(tz).isoformat(), "market": located,
        "source_market": row["source_market"],
        "text": full[:EXCERPT], "quote_text": full,
        "engagement": {k: row[k] for k in ("views", "likes", "comments", "shares")},
        "flags": flags, "thumbnail_url": row["thumbnail_url"], "duration_s": row["duration_s"],
        "creator_tier": row["creator_tier"], "sponsor_checked": row["sponsor_checked"] is True,
    }


def _sparkline(rows, d):
    values = {r["day"]: r["value"] for r in rows}
    lane = rows[0]["lane_class"] if rows else None
    days = [d - timedelta(days=i) for i in range(SPARK_DAYS - 1, -1, -1)]
    return {"unit": SPARK_UNITS.get(lane, "posts a day"),
            "points": [{"date": x.isoformat(), "value": values.get(x), "expected_low": None, "expected_high": None}
                       for x in days]}


def _day_lines(evidence, market, moments):
    """One line per local post (specificity's rule: located in the market or found in its feeds) with its weekday
    and market-local date, so the writer can date a cause a post already names (WRITER_SYSTEM law 12 allows a cause
    "that its posted_at dates"). A calendar moment is named only when the market's calendar has one on that date.
    These are facts lines: the why-now never rests on them (WRITER_SYSTEM, "never on the facts lines")."""
    on = {}
    for m in moments or ():
        on.setdefault(str(m.get("date")), m.get("name"))
    lines = []
    for r in local_posts(evidence, market):
        try:
            when = datetime.fromisoformat(str(r.get("posted_at")))
        except ValueError:
            continue
        day = when.date().isoformat()
        line = f"Post {r['id']}: posted on {when.strftime('%A')} {day}"
        lines.append(f"{line}, the calendar's {on[day]}" if on.get(day) else line)
    return lines


def _facts(item_row, market, pinned, first_seen, evidence=(), moments=()):
    state = item_row.get("state")
    facts = [f"State: {STATE_WORDS.get(state, state)}"]
    counts = [f"{pinned[name]} {word}" for name, word in (("creators3", "creators"), ("posts3", "posts"))
              if name in pinned]
    if counts:
        facts.append(" and ".join(counts) + " in 3 days")
    if first_seen:
        facts.append(f"First seen in {market} on {first_seen.isoformat()}")
    return facts + _day_lines(evidence, market, moments)


def build_pack(client, item_row, d, market, *, core=CORE, agent=AGENT, hidden=None, moments=(), stages=None):
    """See the module docstring."""
    hidden = read_hidden(client, core=core, agent=agent) if hidden is None else hidden

    def run(name, params):
        return sqlrun.query(client, QUERIES[name], params, core=core, agent=agent)

    tz = timezone(timedelta(hours=OFFSETS[market]))
    item_id, run_id = item_row["item_id"], item_row["run_id"]

    start = datetime.combine(d - timedelta(days=WINDOW_DAYS - 1), time(), tz)
    end = datetime.combine(d + timedelta(days=1), time(), tz)
    rows = _pack_rows(client, {"item_id": item_id, "market": market, "d": d, "start": start, "end": end,
                               "outlet_cap": pack_order.OUTLET_CAP}, core, agent)
    posts = [r for r in rows if r["post_id"] is not None]
    records = [_record(r, tz, item_row) for r in posts]
    if stages is not None:
        stages.update(pack_order.read_stages(rows) or {})
        stages["final"] = pack_order.final_counts(records, market)  # as the query left it, before posts are hidden
    evidence = without_hidden({"evidence": records}, hidden)["evidence"]

    registry, numbers, pinned = {}, [], {}
    params = {"item_id": item_id, "market": market, "d": d, "run_id": run_id}
    for name, unit in NUMBERS:
        if name == "main_ratio" and item_row.get("untested") is not False:
            continue
        rows = run(name, params)
        value = _value(rows)
        if value is None:
            continue
        query_id = _query_id(name, params)
        registry[query_id] = (name, params)
        pinned[name] = value
        numbers.append({"value": value, "unit": unit, "query_id": query_id, "run_id": run_id,
                        "result_hash": result_hash(rows)})

    series_id = item_row.get("main_series_id")
    sparkline = _sparkline(run("sparkline", {"series_id": series_id, "d": d}), d) if series_id else None
    first = run("first_seen", {"item_id": item_id, "market": market, "d": d})
    facts = _facts(item_row, market, pinned, first[0]["first_seen"] if first else None, evidence, moments)

    def rerun(entry):
        name, query_params = registry[entry["query_id"]]
        return _value(run(name, query_params))

    pack = {"evidence": evidence, "numbers": numbers, "facts": facts}
    if item_row.get("kind") == "topic":
        from core.brief.title_purity import read_snapshot

        pack["title_snapshot"] = read_snapshot(client, item_row, d, market, pack, core=core, agent=agent)
    return pack, sparkline, rerun
