"""The SQL evidence pack for one morning-brief card (BUILD.md 1.12, TRUST.md section 3 step 4).

    build_pack(client, item_row, d, market, *, core=CORE, agent=AGENT, hidden=None, moments=())
        -> (pack, sparkline, rerun)
    read_hidden(client, *, core=CORE, agent=AGENT) -> (creator keys, creator ids, names) of the suppression list

item_row: a v_item_state_current row as a dict (item_id, run_id, state, untested, main_series_id, ...).
d: the brief date. market: "ZA", "NG" or "KE". client: a google.cloud.bigquery Client.

pack is the shape core/brief/explain.py reads:
    evidence: up to 12 contract Evidence dicts (core/api/contract.md section 4). text is the excerpt shown on
        the card, at most 280 characters of the post text or else its transcript; quote_text is the whole
        stored text. market is the post's located market: its geo_market when geo_confidence is 0.7 or more
        and geo_source is one collect treats as known (KNOWN_GEO in core/collect/writers.py; DATA.md 3.6,
        TRUST.md A5). A post with no known location gets market
        None and the flag market_assumed, never the market it was sighted in, so K3 and K5 treat it as not
        located. The sponsored flag is the enrich model's paid marker or the vendor's paid label
        (posts.vendor_labels). sponsor_checked is true when either gave a reading for the post, so a post with
        sponsor_checked true and no sponsored flag was checked and shows no paid label; false means unknown.
    numbers: {value, unit, query_id, run_id, result_hash} for creators3, posts3 and, when the item was tested,
        main_ratio. result_hash is "sha256:" plus the hex sha256 of the canonical JSON of the query's rows.
        After those, when detect has them for the item, the rival-explanation numbers of RIVAL_NUMBERS (posts7,
        burst_share, top3_share, near_dup_share, sponsored_share, local_share, markets_hot), each pinned the same way
        and carrying rival_field, its detect name (METHOD-GAPS Gap 7). They reach the writer and the critic like any
        pack number, K2 re-runs them, and the card's own numbers leave them out (payload.py).
    pinned: present only when detect gave one: rows {value, unit, query_id, run_id, result_hash, rival_field} for
        the values that are not numbers (share_flags, diffusion, small_at, large_at, lead_market, novelty, moment),
        pinned and re-run like the numbers. A time is its ISO 8601 text. No claim cites them; they back the why-now
        sentences in facts and the code-found rivals core/brief/rivals.py records.
    facts: short plain lines about the state, the 3-day counts and the first sighting, then up to two why-now
        sentences built in code from pinned diffusion, small_at, large_at, lead_market and markets_hot (why_now), each
        naming its query ids, then one line per local
        post with its weekday and market-local date, naming a calendar moment only when moments (the market's
        calendar rows job.py reads) has one on that date.
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
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from core.api.store import creator_key
from core.api.today import without_hidden
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
# Detect's rival-explanation values (METHOD-GAPS Gap 7): (query, column, unit). rival_state reads item_state pinned to
# the detect run; rival_window reads the table function detect itself reads, because item_state does not store them.
RIVAL_NUMBERS = (
    ("rival_window", "posts7", "posts in 7 days"),
    ("rival_window", "burst_share", "share of 7-day posts in the busiest 10 minutes"),
    ("rival_window", "top3_share", "share of 7-day posts by the top 3 creators"),
    ("rival_window", "near_dup_share", "share of 7-day posts that are near duplicates"),
    ("rival_state", "sponsored_share", "share of 7-day posts marked sponsored"),
    ("rival_state", "local_share", "share of located 7-day posts from this market"),
    ("rival_state", "markets_hot", "markets with a significant rise in 14 days"),
)
RIVAL_PINS = (
    ("rival_state", "share_flags", "detect share flags"),
    ("rival_state", "diffusion", "detect diffusion"),
    ("rival_window", "small_at", "earliest measured nano or micro post"),
    ("rival_window", "large_at", "earliest measured macro or mega post"),
    ("rival_state", "lead_market", "detect lead market"),
    ("rival_state", "novelty", "detect novelty"),
    ("rival_state", "moment", "detect calendar moment"),
)
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


def _query_id(name, params, column=None):
    """q_<name>_<12 hex>. A query that gives several columns is named by the column pinned, and the column is part of
    what is hashed, so two columns of one query never share an id."""
    parts = {"query": name, "sql": QUERIES[name], "params": params}
    if column is not None:
        parts["column"] = column
    key = json.dumps(parts, sort_keys=True, default=str)
    return f"q_{column or name}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"


def _value(rows, column="value"):
    return rows[0][column] if rows else None


def _plain(value):
    """A pinned value as stored in the pack: a time as ISO 8601 text, an array as a list, anything else as it is."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _located(row):
    """The post's own known market, or None. The market it was sighted in is never a location."""
    known = (row["geo_confidence"] or 0) >= GEO_CONFIDENT and row["geo_source"] in KNOWN_GEO
    return (row["geo_market"] or None) if known else None


def _record(row, tz):
    full = row["quote_text"] or ""
    located = _located(row)
    flags = [name for name, on in (("flagged", row["flagged"]), ("sponsored", row["sponsored"]),
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


def _instant(value):
    """(readable, datetime): (True, None) for a missing value, (False, None) for text that is not a time."""
    if value is None:
        return True, None
    try:
        return True, datetime.fromisoformat(str(value))
    except ValueError:
        return False, None


def why_now(got, tz):
    """At most two sentences, built in code from detect's pinned values and naming the query ids behind them. got maps
    a detect field to (value, query_id); tz is the market's local zone. Nothing is invented: a sentence needs every
    value it states, and the diffusion sentence is derived from small_at and large_at, then only worded when detect's
    own label agrees with them (state.sql: bottom_up when small_at is before large_at, small_only when there is no
    large_at, top_down otherwise), so a label that disagrees with its own times is not stated. small_at and large_at
    are the earliest measured posts by nano or micro and by macro or mega creators among posts first seen in the
    last 28 days (views.sql tvf_item_window)."""
    def value(field):
        return got.get(field, (None, None))[0]

    def qid(*fields):
        return "queries " + ", ".join(got[f][1] for f in fields)

    out = []
    label = value("diffusion")
    small_ok, small = _instant(value("small_at"))
    large_ok, large = _instant(value("large_at"))
    if label in ("bottom_up", "top_down", "small_only") and small_ok and large_ok and all(
            f in got for f in ("small_at", "large_at")):
        date = lambda t: t.astimezone(tz).date().isoformat()  # noqa: E731
        ids = qid("diffusion", "small_at", "large_at")
        if label == "bottom_up" and small and large and small < large:
            out.append(f"Of measured posts first seen in the last 28 days, the earliest by nano or micro creators "
                       f"is dated {date(small)}, before the earliest by macro or mega creators on {date(large)} "
                       f"({ids}).")
        elif label == "top_down" and large and small and small >= large:
            out.append(f"Of measured posts first seen in the last 28 days, the earliest by macro or mega creators "
                       f"is dated {date(large)}, no later than the earliest by nano or micro creators on "
                       f"{date(small)} ({ids}).")
        elif label == "top_down" and large and small is None:
            out.append(f"Of measured posts first seen in the last 28 days, the earliest by macro or mega creators "
                       f"is dated {date(large)}; none by nano or micro creators are on record ({ids}).")
        elif label == "small_only" and small and large is None:
            out.append(f"Of measured posts first seen in the last 28 days, the earliest by nano or micro creators "
                       f"is dated {date(small)}; no macro or mega creator has a measured post ({ids}).")
    lead, hot = value("lead_market"), value("markets_hot")
    if isinstance(lead, str) and lead and isinstance(hot, (int, float)) and not isinstance(hot, bool) and hot >= 1:
        ids = qid("lead_market", "markets_hot")
        if hot == 1:
            out.append(f"{lead} is the only market with a significant rise in the last 14 days ({ids}).")
        else:
            out.append(f"{lead} is listed first of {int(hot)} markets with a significant rise in the last 14 days, "
                       f"by earliest day and then market code ({ids}).")
    return out


def _facts(item_row, market, pinned, first_seen, evidence=(), moments=(), why=()):
    state = item_row.get("state")
    facts = [f"State: {STATE_WORDS.get(state, state)}"]
    counts = [f"{pinned[name]} {word}" for name, word in (("creators3", "creators"), ("posts3", "posts"))
              if name in pinned]
    if counts:
        facts.append(" and ".join(counts) + " in 3 days")
    if first_seen:
        facts.append(f"First seen in {market} on {first_seen.isoformat()}")
    return facts + list(why) + _day_lines(evidence, market, moments)


def _rival_pins(run, params, run_id, registry):
    """(numbers, pinned rows, got) for detect's rival-explanation values, or three empties when the read fails: these
    values are extra evidence, so a failed read leaves the pack as it was and the card is not held for it. A null in a
    row detect wrote is itself pinned (no calendar moment, no macro post), so it can be stated; a query that gave no
    row, or only nulls, pins nothing."""
    try:
        rows = {name: run(name, params) for name in ("rival_state", "rival_window")}
    except Exception as e:
        print(f"brief {params['d'].isoformat()}: rival_evidence_read_failed {type(e).__name__}", file=sys.stderr)
        return [], [], {}
    numbers, pinned, got = [], [], {}
    real = {name: bool(r) and any(v is not None for v in r[0].values()) for name, r in rows.items()}

    def pin(name, column, unit, number):
        raw = _value(rows[name], column)
        if raw is None and (number or not real[name]):
            return None
        query_id = _query_id(name, params, column)
        registry[query_id] = (name, params, column)
        value = _plain(raw)
        got[column] = (value, query_id)
        return {"value": value, "unit": unit, "query_id": query_id, "run_id": run_id,
                "result_hash": result_hash([{"value": raw}]), "rival_field": column}

    for name, column, unit in RIVAL_NUMBERS:
        entry = pin(name, column, unit, True)
        if entry:
            numbers.append(entry)
    for name, column, unit in RIVAL_PINS:
        entry = pin(name, column, unit, False)
        if entry:
            pinned.append(entry)
    return numbers, pinned, got


def build_pack(client, item_row, d, market, *, core=CORE, agent=AGENT, hidden=None, moments=()):
    """See the module docstring."""
    hidden = read_hidden(client, core=core, agent=agent) if hidden is None else hidden

    def run(name, params):
        return sqlrun.query(client, QUERIES[name], params, core=core, agent=agent)

    tz = timezone(timedelta(hours=OFFSETS[market]))
    item_id, run_id = item_row["item_id"], item_row["run_id"]

    start = datetime.combine(d - timedelta(days=WINDOW_DAYS - 1), time(), tz)
    end = datetime.combine(d + timedelta(days=1), time(), tz)
    posts = run("evidence", {"item_id": item_id, "market": market, "d": d, "start": start, "end": end})
    evidence = without_hidden({"evidence": [_record(r, tz) for r in posts]}, hidden)["evidence"]

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
        registry[query_id] = (name, params, "value")
        pinned[name] = value
        numbers.append({"value": value, "unit": unit, "query_id": query_id, "run_id": run_id,
                        "result_hash": result_hash(rows)})

    series_id = item_row.get("main_series_id")
    sparkline = _sparkline(run("sparkline", {"series_id": series_id, "d": d}), d) if series_id else None
    first = run("first_seen", {"item_id": item_id, "market": market, "d": d})
    rival_numbers, rival_pinned, got = _rival_pins(run, params, run_id, registry)
    numbers += rival_numbers
    facts = _facts(item_row, market, pinned, first[0]["first_seen"] if first else None, evidence, moments,
                   why_now(got, tz))

    def rerun(entry):
        name, query_params, column = registry[entry["query_id"]]
        return _plain(_value(run(name, query_params), column))

    pack = {"evidence": evidence, "numbers": numbers, "facts": facts}
    if rival_pinned:
        pack["pinned"] = rival_pinned
    return pack, sparkline, rerun
