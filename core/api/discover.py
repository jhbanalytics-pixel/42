"""Discover, Radar, topic pages and alerts (core/api/contract.md sections 10.1 to 10.3 and 10.5).

Every item comes from v_item_state_current for the latest good detect run. Its card is the brief's card when the
item is in that day's brief, else one built from item_state with no explanation (explanation_status not_run).
Nothing the gate holds is dropped: it is listed under held_back with its reason and its card.
"""
import datetime as dt
import hashlib
import json
import re

from core.api import alerts, fast
from core.api.held_words import plain_reason
from core.api.searching import searching_now
from core.api.store import canon_platform, creator_key
from core.api.today import (FLAG_WORDS, LABELS, MARKETS, SAST, NotFound, NotReady, _card, _display_title,
                            _platform_word, _series_name, _warmup, hidden_people, state_word, without_hidden)

SORTS = ("order", "velocity", "reach", "new")
MAX_LIMIT = 200
SERIES_DAYS = 28
HISTORY_DAYS = 90
PAID_LED_SHARE = 0.5
WARMUP_NOTE = "Growth needs 14 days of data; showing reach only"

CARD_FLAG_WORDS = {**FLAG_WORDS, "paid_led": "Paid-led"}
HELD_WORDS = {"data_issue": "Data issue", "likely_coordinated": "Likely coordinated",
              "political_unconfirmed": "Political, not confirmed", "paid_led": "Paid-led",
              "not_local": "Not local to this market", "too_few_creators": "Too few creators",
              "not_confirmed": "Not confirmed", "explanation_failed": "Explanation did not pass the checks",
              "not_active": "Not tracked by 42 right now", "held_by_gate": "Held back by 42's checks"}
# The five-step marker (FEATURES.md row 11) with each state's rule in plain words (DATA.md section 3.7).
LIFECYCLE = {
    "emerging": (1, "Emerging", "New or returning, with 5 or more creators and 8 posts in 3 days"),
    "rising": (2, "Rising", "Clearly up, at least twice its usual level, on 2 days, or on 1 day plus another platform or market"),
    "peaking": (3, "Peaking", "Growth slowing while posting stays near its high"),
    "mainstream": (4, "Mainstream", "Reached large creators and news, or 3 or more platforms"),
    "fading": (5, "Fading", "At or below 60% of its peak on each of the last 3 days"),
}
# young_accounts is account age, a bot signal, never a person's age (contract.md section 10.1).
SIGNAL_WORDS = {"near_duplicates": "Near-duplicate posts", "young_accounts": "Accounts under 30 days old",
                "burst": "Many posts in the busiest 10 minutes", "concentrated": "Three creators hold most posts",
                "coaction": "Accounts posting in step"}
UNREAD = object()  # _cards reads the suppression list itself
SERIES_UNITS = {"unbiased_rank": "appearances a day", "panel": "posts a day", "unbiased_counter": "added a day"}
# Radar's reach measures over 7 days, every collection lane counted (store.item_reach), with each one's unit.
REACH_UNITS = {"posts7": "posts in 7 days", "creators7": "creators in 7 days",
               "measured_posts7": "posts in 7 days on boards and followed accounts",
               "measured_creators7": "creators in 7 days on boards and followed accounts",
               "views7": "views in 7 days", "views_posts7": "posts with a view count in 7 days",
               "engagement7": "engagements in 7 days", "engagement_posts7": "posts with an engagement count in 7 days",
               "located7": "posts with a known place in 7 days", "local7": "posts local to the market in 7 days",
               "own_feed7": "posts from the market's own feeds in 7 days"}
# Counts a successful read gives as 0 for an item it found no post for; views and engagement stay unknown.
REACH_COUNTS = ("posts7", "creators7", "measured_posts7", "measured_creators7", "views_posts7", "engagement_posts7",
                "located7", "local7", "own_feed7")
REACH_DAYS = 7
# A creator id no reader can use as a name: a Reddit account id, a YouTube channel id or a bare number.
_CREATOR_ID = re.compile(r"t2_[a-z0-9]+|uc[a-z0-9_-]{22}|\d+", re.IGNORECASE)


class BadRequest(ValueError):
    """An argument the route should answer with 400."""


def figure(value, unit, query_id, run_id, rows):
    """A number with the query, run and sha256 of the canonical JSON of the rows it came from."""
    if value is None:
        return None
    text = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return {"value": value, "unit": unit, "query_id": query_id, "run_id": run_id,
            "result_hash": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _state_figure(row, column, unit):
    """A Figure from one item_state column, hashed over the row that holds it."""
    narrow = [{"item_id": row["item_id"], "market": row["market"], "metric_date": row["metric_date"],
               "run_id": row["run_id"], column: row.get(column)}]
    return figure(row.get(column), unit, "q_discover_items", row["run_id"], narrow)


def _sentence(snake):
    return (snake or "").replace("_", " ").strip().capitalize()


def _run(store, until=None):
    run = store.latest_detect_run(until=until) if until else store.latest_detect_run()
    if run is None:
        raise NotReady("No detect run has finished yet")
    return run


def _brief_items(store, date):
    """The day's brief by (market, item_id): its cards and more, and apart from them its held_back.items. A G10 hold
    sits in held_back.items, so the cards alone never say why an explanation was held."""
    shown, held = {}, {}
    for row in store.briefs(date):
        if row.get("brief_date") != date:
            continue
        payload = row["payload"]
        for c in (payload.get("cards") or []) + (payload.get("more") or []):
            shown[(row["market"], c["item_id"])] = c
        block = payload.get("held_back") if isinstance(payload.get("held_back"), dict) else {}
        for h in block.get("items") or []:
            if isinstance(h, dict) and isinstance(h.get("item_id"), str):
                held[(row["market"], h["item_id"])] = h
    return shown, held


def _hold_basis(brief_card, brief_held):
    """(explanation_status, failed_reason) for the words of a G10 hold, from the brief's own record of the item: its
    card when it has one, else its held item. The held item carries the job's failed_reason (a check it failed, or
    a busy model's fixed wording) and, when the job writes it, explanation_status. Nothing else shows the model
    skipped a topic, so an item with neither gets no status and the words for a failed check."""
    if brief_card is not None:
        return _explanation_status(brief_card), brief_card.get("failed_reason")
    if brief_held is not None:
        status = brief_held.get("explanation_status")
        return (status if isinstance(status, str) else None), brief_held.get("failed_reason")
    return None, None


def _flag(row):
    if (row.get("sponsored_share") or 0) >= PAID_LED_SHARE:
        return "paid_led"
    if row.get("authenticity") in ("likely_coordinated", "check_pattern"):
        return row["authenticity"]
    if row.get("geo_status") == "market_unconfirmed":
        return "market_unconfirmed"
    if row.get("authenticity") == "not_assessed":
        return "not_assessed"
    return None


def _gate_for(rows, run_date):
    """The gate's own placement per (item, market) for the run: the rows of each market's latest brief date on or
    before run_date, and nothing from an earlier brief, so an item the newest brief does not name keeps no old hold
    (N42). v_item_gate_current keeps every brief date and the admitted rows ('today', 'moments', with no reason)
    beside the held ones, so only a held_back row is a hold (contract.md section 10.2). A row with no place or
    date, as older fixtures give, counts as a hold on the run date."""
    day = str(run_date)[:10] if run_date else None
    dated = []
    for g in rows:
        when = str(g.get("brief_date") or day or "")[:10]
        if day and when > day:
            continue
        dated.append((when, g))
    latest = {}
    for when, g in dated:
        latest[g["market"]] = max(latest.get(g["market"], when), when)
    best = {}
    for when, g in dated:
        if when == latest[g["market"]]:
            best.setdefault((g["item_id"], g["market"]), g)
    return {k: g for k, g in best.items() if (g.get("place") or "held_back") == "held_back"}


def _held(row, gate):
    """(rule, reason, reason_text) when the gate holds the item, else None (contract.md section 10.2)."""
    g = gate.get((row["item_id"], row["market"]))
    if g:
        return g.get("rule"), g["reason"], g.get("reason_text") or HELD_WORDS.get(g["reason"]) or _sentence(
            g["reason"])
    paid = (row.get("sponsored_share") or 0) >= PAID_LED_SHARE
    if row.get("eligible") and not paid:
        return None
    if row.get("authenticity") == "likely_coordinated":
        rule, reason = "G4", "likely_coordinated"
    elif row.get("geo_status") == "not_local":
        rule, reason = "G6", "not_local"
    elif paid:
        rule, reason = "G5", "paid_led"
    elif row.get("map_status") not in ("active", "unknown"):
        rule, reason = None, "not_active"
    else:
        rule, reason = None, "held_by_gate"
    return rule, reason, HELD_WORDS[reason]


def _explanation_status(brief_card):
    if brief_card is None:
        return "not_run"
    if brief_card.get("explanation_status"):
        return brief_card["explanation_status"]
    return "explained" if brief_card.get("explained") else None


def _build_card(row, brief_card, warmup, order):
    market, date = row["market"], row["metric_date"]
    market_scope = row.get("market_scope")
    if market_scope is None and brief_card is not None:
        market_scope = brief_card.get("market_scope")
    if market_scope not in ("market", "global"):
        market_scope = "global"
    scope_basis = row if row.get("market_scope") is not None else brief_card or {}
    market_posts7 = scope_basis.get("market_posts7")
    total_posts7 = scope_basis.get("total_posts7")
    market_share7 = scope_basis.get("market_share7")
    if type(market_posts7) is not int or not 0 <= market_posts7 <= 12:
        market_posts7 = None
    if type(total_posts7) is not int or not 0 <= total_posts7 <= 12:
        total_posts7 = None
    if market_posts7 is not None and total_posts7 is not None and market_posts7 > total_posts7:
        market_posts7 = total_posts7 = market_share7 = None
    elif market_posts7 is None or total_posts7 is None or total_posts7 == 0:
        market_share7 = None
    elif (type(market_share7) not in (int, float) or not 0 <= market_share7 <= 1
          or round(market_share7 * 100, 1) != round(market_posts7 / total_posts7 * 100, 1)):
        market_share7 = None
    title = row.get("creator_name") or _display_title(
        [row.get("label"), row.get("canonical_key"), (brief_card or {}).get("title")],
        evidence=(brief_card or {}).get("evidence"), platforms=row.get("platforms"), kind=row.get("kind"))
    reach = _state_figure(row, "creators3", "creators in 3 days")
    growth = None if warmup else _state_figure(row, "main_ratio", "times usual")
    if brief_card is not None:
        card = _card(brief_card, market, date, None)
    else:
        creators = row.get("creators3") or 0
        creator_word = "creator" if creators == 1 else "creators"
        count_line = f"{creators} {creator_word}, 3 days"
        if growth:
            count_line += f", {growth['value']:.1f} times usual"
        card = {"item_id": row["item_id"], "market": market, "date": date, "rank": None, "kind": row.get("kind"),
                "title": title, "tag": None, "explanation": None, "explanation_claim_ids": [], "claims": [],
                "count_line": count_line, "numbers": [f for f in (reach, growth) if f], "sparkline": None,
                "thumbnails": [], "evidence_ids": [], "evidence": [],
                "ask": f"What is behind {title} in {LABELS.get(market, market)} this week?"}
    card["title"] = title
    card["ask"] = f"What is behind {title} in {LABELS.get(market, market)} this week?"
    count_line = card.get("count_line")
    if isinstance(count_line, str) and count_line.startswith("1 creators"):
        card["count_line"] = "1 creator" + count_line[len("1 creators"):]
    status = _explanation_status(brief_card)
    if status != "explained":
        card.update(explanation=None, explanation_claim_ids=[], claims=[])
    flag = _flag(row) or card.get("flag")
    step = LIFECYCLE.get(row.get("state"))
    last_wave = row.get("last_wave") if row.get("novelty") == "recurrence" else None
    card.update(
        kind=row.get("kind") or card.get("kind"), state=row.get("state"),
        market_scope=market_scope,
        market_posts7=market_posts7, total_posts7=total_posts7, market_share7=market_share7,
        state_word=state_word(row.get("state"), row.get("creators3") or 0), flag=flag,
        flag_word=CARD_FLAG_WORDS.get(flag),
        explained=status == "explained", explanation_status=status,
        lifecycle={"step": step[0], "word": step[1], "rule": step[2]} if step else None,
        novelty=row.get("novelty"),
        last_wave={"peak_date": last_wave["peak_date"],
                   "peak_posts": figure(last_wave["peak_posts"], "posts on the peak day", "q_discover_items",
                                        row["run_id"], [{"item_id": row["item_id"], "market": market,
                                                         "metric_date": date, "run_id": row["run_id"],
                                                         "last_wave": last_wave}])} if last_wave else None,
        diffusion=row.get("diffusion"), origin=row.get("origin"), spread_line=row.get("spread_line"),
        reach=reach, growth=growth, order=order, watch_id=None)
    return card


def _raw_creator(row):
    """(platform, raw id) when a creator item's label is only the id its key was built from, else None."""
    key = row.get("canonical_key")
    if row.get("kind") != "creator" or not isinstance(key, str) or ":" not in key:
        return None
    platform, raw = key.split(":", 1)
    label = str(row.get("label") or "").strip().lstrip("@").casefold()
    return (platform, raw) if not label or label in (raw, key.casefold()) else None


def _creator_names(store, rows, hidden):
    """rows with creator_name set on each creator item whose label is only its id: the creators row's display name,
    else its @handle, else "<Platform> account, name not collected" when the id is not readable at all. A suppressed
    creator is never named, and nothing is named while the suppression list cannot be read (hidden None); an id no
    reader can use still gets the unnamed label then, so a raw platform id is never shown as a title."""
    raw = {r["item_id"]: _raw_creator(r) for r in rows}
    keys = {f"{p}:{i}" for p, i in (v for v in raw.values() if v)}
    if not keys:
        return rows
    hidden_keys, hidden_ids, _ = hidden if hidden is not None else (set(), set(), set())
    reader = getattr(store, "creator_names", None)
    found = (reader(keys) if callable(reader) and hidden is not None else None) or {}
    out = []
    for r in rows:
        pair = raw[r["item_id"]]
        if not pair:
            out.append(r)
            continue
        platform, ident = pair
        unnamed = f"{_platform_word(canon_platform(platform))} account, name not collected"
        c = found.get(f"{platform}:{ident}") or {}
        theirs = c.get("creator_id") in hidden_ids or creator_key(platform, c.get("handle") or ident) in hidden_keys
        if hidden is None or theirs:
            out.append(dict(r, creator_name=unnamed) if _CREATOR_ID.fullmatch(ident) else r)
            continue
        handle = str(c.get("handle") or "").strip().lstrip("@")
        name = str(c.get("display_name") or "").strip()
        if not name and handle and handle.casefold() != ident and not _CREATOR_ID.fullmatch(handle):
            name = "@" + handle
        if not name and _CREATOR_ID.fullmatch(ident):
            name = unnamed
        out.append(dict(r, creator_name=name) if name else r)
    return out


def _worth_key(row):
    pct = row.get("worth_pct")
    return (pct is not None, pct if pct is not None else (row.get("worth_raw") or 0), row.get("creators3") or 0)


def _cards(store, run, market, hidden=UNREAD):
    """Every item of the run as (row, card, held) with order set on the shown items. Each card is read through
    without_hidden, with hidden as hidden_people(store) gives it (read here when not given)."""
    hidden = hidden_people(store) if hidden is UNREAD else hidden
    rows = _creator_names(store, store.item_states(run, market), hidden)
    spreads = store.item_spread(run["run_date"], market)
    if spreads is not None:  # L2's v_item_spread writes the card's spread_line once it exists
        # An item with no row there (no measured post in the view's 7 days) keeps item_state's line.
        lines = {(s["item_id"], s["market"]): s.get("spread_line") for s in spreads}
        rows = [dict(r, spread_line=lines.get((r["item_id"], r["market"]), r.get("spread_line"))) for r in rows]
    gate = _gate_for(store.item_gate(market) or [], run["run_date"])
    briefs, brief_held = _brief_items(store, run["run_date"])
    warmup = _warmup(store, run["run_date"])["active"]
    held = {r["item_id"] + "|" + r["market"]: _held(r, gate) for r in rows}
    order = {}
    for m in {r["market"] for r in rows}:
        shown = [r for r in rows if r["market"] == m and held[r["item_id"] + "|" + m] is None]
        for n, r in enumerate(sorted(shown, key=lambda r: (_worth_key(r), r["item_id"]), reverse=True), 1):
            order[r["item_id"] + "|" + m] = n
    out = []
    for r in rows:
        key = r["item_id"] + "|" + r["market"]
        card = without_hidden(_build_card(r, briefs.get((r["market"], r["item_id"])), warmup, order.get(key)), hidden)
        if held[key]:
            rule, reason, text = held[key]
            status, failed = _hold_basis(briefs.get((r["market"], r["item_id"])),
                                         brief_held.get((r["market"], r["item_id"])))
            wording = {"rule": rule, "reason": reason, "reason_text": text}
            if isinstance(failed, str) and failed.strip():
                wording["failed_reason"] = failed.strip()
            card["held_back"] = plain_reason(wording, status)
        out.append((r, card, held[key]))
    return out, warmup


def _platforms(row):
    return [canon_platform(p) for p in row.get("platforms") or []]


def _matches(row, kind, state, platform):
    return ((not kind or row.get("kind") == kind) and (not state or row.get("state") == state)
            and (not platform or canon_platform(platform) in _platforms(row)))


def _market_arg(market):
    if market != "all" and market not in MARKETS:
        raise BadRequest("market must be ZA, NG, KE or all.")
    return market


def _sort_key(sort):
    def desc(v):
        return (v is None, -(v or 0))

    if sort == "velocity":
        return lambda rc: (desc(rc[0].get("main_vel")), rc[1]["order"])
    if sort == "reach":
        return lambda rc: (desc(rc[0].get("creators3")), rc[1]["order"])
    if sort == "new":
        return lambda rc: (desc(dt.date.fromisoformat(rc[0]["first_seen"]).toordinal()
                                if rc[0].get("first_seen") else None), rc[1]["order"])
    return lambda rc: (rc[1]["order"], MARKETS.index(rc[0]["market"]) if rc[0]["market"] in MARKETS else 9)


def build_discover(store, market, kind=None, state=None, platform=None, sort="order", limit=50, cursor=None,
                   now=None):
    market = _market_arg(market)
    if sort not in SORTS:
        raise BadRequest("sort must be order, velocity, reach or new.")
    if not 1 <= limit <= MAX_LIMIT:
        raise BadRequest(f"limit must be 1 to {MAX_LIMIT}.")
    if cursor and not cursor.isdigit():
        raise BadRequest("That is not a Discover cursor.")
    start = int(cursor or 0)
    run = _run(store)
    hidden = hidden_people(store)
    cards, _ = _cards(store, run, market, hidden)
    filters = {"kinds": sorted({r["kind"] for r, _, _ in cards if r.get("kind")}),
               "states": sorted({r["state"] for r, _, _ in cards if r.get("state")}),
               "platforms": sorted({p for r, _, _ in cards for p in _platforms(r)})}
    picked = [(r, c, h) for r, c, h in cards if _matches(r, kind, state, platform)]
    shown = sorted([(r, c) for r, c, h in picked if h is None], key=_sort_key(sort))
    held = [{"item_id": c["item_id"], "title": c["title"], "reason": h[1],
             **{k: v for k, v in c["held_back"].items() if k in ("reason_text", "reason_raw")}, "card": c}
            for r, c, h in sorted(picked, key=lambda x: (x[0]["market"], x[1]["title"])) if h]
    page = shown[start:start + limit]
    return {"date": run["run_date"], "market": market, "run_id": run["run_id"],
            "items": _with_reach7(store, run, [c for _, c in page]),
            "next_cursor": str(start + limit) if start + limit < len(shown) else None,
            "held_back": {"count": len(held), "items": held}, "filters": filters,
            "searching_now": searching_now(store, MARKETS if market == "all" else [market],
                                           (now or dt.datetime.now(SAST)).astimezone(SAST).date().isoformat(),
                                           hidden)}


def _with_reach7(store, run, cards):
    """The page's cards, each with reach7: the creators7 Figure Radar reads (every collection lane, 7 days), shown
    beside the card's 3-day reach, which counts only the boards and followed accounts the checks use. Display only:
    the order, the gate and the 3-day reach stay as they were. None when the market's read gave nothing."""
    ids = {m: sorted(c["item_id"] for c in cards if c["market"] == m) for m in sorted({c["market"] for c in cards})}
    for market, item_ids in ids.items():
        fast.start(store, "item_reach", run["run_date"], market, item_ids)
    measures = {market: _reach(store, run, market, item_ids) for market, item_ids in ids.items()}
    for c in cards:
        c["reach7"] = (measures[c["market"]].get(c["item_id"]) or {}).get("creators7")
    return cards


def build_radar(store, market, kind=None):
    market = _market_arg(market)
    run = _run(store)
    cards, warmup = _cards(store, run, market)
    picked = [(r, c, h) for r, c, h in cards if _matches(r, kind, None, None)]
    shown = sorted([(r, c) for r, c, h in picked if h is None], key=_sort_key("order"))
    reach = _reach(store, run, market, [c["item_id"] for _, c in shown])
    return {"date": run["run_date"], "market": market, "window": _window(store, run, market),
            "points": [{"item_id": c["item_id"], "label": c["title"], "kind": c["kind"], "state": c["state"],
                        "flag": c["flag"], "growth": c["growth"], "reach": c["reach"],
                        "measures": reach.get(c["item_id"])} for _, c in shown],
            "held_back_count": sum(1 for _, _, h in picked if h), "note": WARMUP_NOTE if warmup else None}


def _reach(store, run, market, item_ids):
    """{item_id: measures} from store.item_reach for one market: each measure a Figure over the 7 days ending the run
    date, every collection lane counted, plus the platforms its posts came from. An item the read found no post for
    gets its counts as 0 Figures and views and engagement unknown; the All market, a failed read or a store without
    it gives no measures (None on the point), so unknown is never drawn as zero."""
    reader = getattr(store, "item_reach", None)
    if market == "all" or not item_ids or not callable(reader):
        return {}
    item_ids = sorted(item_ids)
    rows = reader(run["run_date"], market, item_ids)
    if rows is None:
        return {}
    found = {r["item_id"] for r in rows}
    rows = list(rows) + [{"item_id": i, **{k: 0 for k in REACH_COUNTS}, "platforms": []}
                         for i in item_ids if i not in found]
    out = {}
    for row in rows:
        narrow = [{k: row.get(k) for k in ("item_id", *REACH_UNITS, "platforms")}
                  | {"market": market, "metric_date": run["run_date"]}]
        measures = {k: figure(row.get(k), unit, "q_item_reach", run["run_id"], narrow)
                    for k, unit in REACH_UNITS.items()}
        located, local = row.get("located7"), row.get("local7")
        share = round(local / located, 4) if isinstance(located, int) and isinstance(local, int) and located else None
        measures["local_share7"] = figure(share, "share of located posts local to the market", "q_item_reach",
                                          run["run_id"], narrow)
        measures["platforms"] = [canon_platform(p) for p in row.get("platforms") or []]
        out[row["item_id"]] = measures
    return out


def _window(store, run, market):
    """The days Radar's measures cover (from and to, inclusive) and, per platform, how many of them had baseline
    health rows and how many of those were valid, so a short or broken week is said, never hidden. None on All."""
    if market == "all":
        return None
    end = dt.date.fromisoformat(run["run_date"])
    start = (end - dt.timedelta(days=REACH_DAYS - 1)).isoformat()
    reader = getattr(store, "health_days", None)
    rows = reader(market, start, end.isoformat()) if callable(reader) else None
    return {"from": start, "to": end.isoformat(), "days": REACH_DAYS,
            "platforms": None if rows is None else [
                {"platform": canon_platform(r["platform"]), "days": r["days"], "days_ok": r["days_ok"]} for r in rows]}


def _series(rows, end):
    if rows is None:
        return None
    last = dt.date.fromisoformat(end)
    days = [(last - dt.timedelta(days=n)).isoformat() for n in range(SERIES_DAYS - 1, -1, -1)]
    groups = {}
    for r in rows:
        r = dict(r, platform=canon_platform(r.get("platform")))
        g = groups.setdefault((r.get("platform") or "", r["series"]), {"row": r, "values": {}})
        g["values"][str(r.get("day") or r.get("date"))] = r.get("value")
    out = []
    for (_, _), g in sorted(groups.items()):
        r = g["row"]
        out.append({"platform": r.get("platform"), "series": r["series"], "series_words": _series_name(r),
                    "unit": r.get("unit") or SERIES_UNITS.get(r.get("lane_class"), "a day"),
                    "points": [{"date": d, "value": g["values"].get(d), "expected_low": None, "expected_high": None}
                               for d in days]})
    return out


def _evidence(rows):
    """The topic page's posts, each platform post once. v_item_evidence can hold one post under two record ids (read
    twice, visual QA 5 October 2026: two identical Instagram cards); the first row for a link is kept."""
    if rows is None:
        return None
    out, seen = [], set()
    for r in rows:
        url = (r.get("url") or "").strip()
        if url:
            if url in seen:
                continue
            seen.add(url)
        engagement = r.get("engagement")
        if not isinstance(engagement, dict):
            engagement = {k: r.get(k) for k in ("views", "likes", "comments", "shares")}
        out.append({"id": r.get("post_id") or r.get("id"), "platform": canon_platform(r.get("platform")),
                    "handle": r.get("handle"),
                    "url": r.get("url"), "posted_at": r.get("posted_at") or r.get("published_at"),
                    "market": r.get("market"), "text": r.get("excerpt") or r.get("text"), "engagement": engagement,
                    "flags": r.get("flags") or [], "thumbnail_url": r.get("thumbnail_url") or r.get("thumbnail"),
                    "creator_tier": r.get("creator_tier") or r.get("tier")})
    return out


SPREAD_TIERS = ("nano", "micro", "mid", "macro", "mega")


def _spread(rows, item_id, market, date, run_id):
    """The topic page's spread (contract 10.3) from L2's v_item_spread rows for the item on the run date in every
    market, or None when the page's market has no row. The view gives each platform's first measured sighting and
    the measured posts per creator tier first seen in the 7 days ending the date. markets lists each market the
    view has a row for (so only those with a measured post first seen in those 7 days), with its earliest platform
    sighting and its tier counts summed; a market without a row is left out, never filled in."""
    rows = [r for r in rows or [] if r["item_id"] == item_id]
    row = next((r for r in rows if r["market"] == market), None)
    if row is None:
        return None
    platforms = {}
    for p in row.get("platform_first_seen") or []:  # the view orders them by first day
        platforms.setdefault(canon_platform(p.get("platform")), p.get("first_seen"))
    tiers = row.get("tiers") or {}
    narrow = [{"item_id": item_id, "market": market, "metric_date": date, "tiers": tiers}]
    return {"platforms": [{"platform": k, "first_seen": v} for k, v in platforms.items() if k],
            "tiers": {t: figure(tiers.get(t), "posts first seen in 7 days", "q_item_spread", run_id, narrow)
                      for t in SPREAD_TIERS},
            "markets": _spread_markets(rows, item_id, date, run_id)}


def _spread_markets(rows, item_id, date, run_id):
    out = []
    for r in rows:
        seen = [p.get("first_seen") for p in r.get("platform_first_seen") or [] if p.get("first_seen")]
        tiers = r.get("tiers") or {}
        counts = [tiers.get(t) for t in SPREAD_TIERS]
        total = sum(counts) if all(isinstance(c, int) and not isinstance(c, bool) for c in counts) else None
        narrow = [{"item_id": item_id, "market": r["market"], "metric_date": date, "tiers": tiers}]
        out.append({"market": r["market"], "first_seen": min(seen, default=None),
                    "posts": figure(total, "posts first seen in 7 days", "q_item_spread", run_id, narrow)})
    order = {m: i for i, m in enumerate(MARKETS)}
    return sorted(out, key=lambda m: (m["first_seen"] is None, m["first_seen"] or "", order.get(m["market"], 9)))


def _origin(rows, item_id, market):
    """The topic page's origin (contract 10.3) from L2's v_item_origin row for the item and market, or None when
    there is no row. first_measured, after_collection_began and first_state_day are measured; origin, lead_news_day
    and lag_days stay null until the seed queue records a news source and its date. Only these fields leave."""
    row = next((r for r in rows or [] if r["item_id"] == item_id and r["market"] == market), None)
    if row is None:
        return None
    return {"origin": row.get("origin"), "market": market, "first_measured": row.get("first_measured"),
            "after_collection_began": bool(row.get("after_collection_began")),
            "first_state_day": row.get("first_state_day"), "lead_news_day": row.get("lead_news_day"),
            "lag_days": row.get("lag_days")}


NEWS_LIMIT = 20


def _news(rows, item_id):
    """The topic page's news (contract 10.3): L2's v_news_followthrough rows whose news seed is the item or names
    it, newest seed first, or None when there are none. matched_by says how this topic matched the story. The
    view's label (the item label, or the seed's query text) leaves as title, as a card's does, so without_hidden
    masks a handle written in it; it does not mask a string under a label key.
    news_day, news_day_from and lag_days stay null until the seed queue records a news source and its date."""
    out = []
    for r in rows or []:
        match = next((m for m in r.get("matched_items") or [] if m.get("item_id") == item_id), None)
        if match is None and r.get("item_id") != item_id:
            continue
        out.append({"seed_date": r.get("seed_date"), "news_day": r.get("news_day"),
                    "news_day_from": r.get("news_day_from"), "title": r.get("label"),
                    "matched_by": (match or {}).get("matched_by") or "seed",
                    "collect_ran": bool(r.get("collect_ran")), "reached_state": bool(r.get("reached_state")),
                    "first_state_day": r.get("first_state_day"), "first_measured": r.get("first_measured"),
                    "lag_days": r.get("lag_days")})
    out.sort(key=lambda n: n["seed_date"] or "", reverse=True)
    return out[:NEWS_LIMIT] or None


def build_topic(store, item_id, market):
    if market not in MARKETS:
        raise BadRequest("market must be ZA, NG or KE.")
    run = _run(store)
    hidden = hidden_people(store)
    cards, _ = _cards(store, run, market, hidden)
    found = next(((r, c) for r, c, _ in cards if r["item_id"] == item_id), None)
    if found is None:
        raise NotFound(f"{item_id} is not in the {market} detect run of {run['run_date']}")
    row, card = found
    end = run["run_date"]
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    history = [{"date": h["metric_date"], "state": h["state"],
                "state_word": state_word(h["state"], h.get("creators3") or 0)}
               for h in store.item_history(item_id, market, start, end)]
    waves = store.item_waves(item_id, market)
    signals = [{"signal": s, "words": SIGNAL_WORDS.get(s) or _sentence(s), "share": None}
               for s in row.get("share_flags") or []]
    for c in store.coord_signals(item_id, market, end) or []:
        share = figure(c.get("item_posts_share"), "share of the item's posts", "q_coord_signals", c.get("run_id"),
                       [c])
        signals.append({"signal": c["signal"], "words": SIGNAL_WORDS.get(c["signal"]) or _sentence(c["signal"]),
                        "share": share})
    # null only when a stored check found nothing; not_stored when the row holds no check, so the topic page never
    # reads a missing check as clear. Card flags (_flag) and the gate (_held) read the row itself and are unchanged.
    stored = row.get("authenticity")
    auth = None if stored == "clear" else stored if isinstance(stored, str) and stored.strip() else "not_stored"
    evidence = store.item_evidence(item_id, market)
    return without_hidden({
        "card": card, "aliases": list(row.get("aliases") or []), "history": history,
        "series": _series(store.item_series(item_id, market, SERIES_DAYS), end),
        "spread": _spread(store.item_spread(end, "all", item_id), item_id, market, end, run["run_id"]),
        "origin": _origin(store.item_origin(item_id, market), item_id, market),
        "news": _news(store.news_followthrough(item_id, market, end), item_id),
        "waves": None if waves is None else [
            {"peak_date": w["peak_date"],
             "peak_posts": figure(w["peak_posts"], "posts on the peak day", "q_item_waves", run["run_id"], [w])}
            for w in sorted(waves, key=lambda w: w["peak_date"], reverse=True)],
        "authenticity": {"flag": auth, "flag_word": FLAG_WORDS.get(auth), "signals": signals},
        "evidence": _evidence(None if evidence is None else without_hidden({"evidence": evidence}, hidden)["evidence"]),
    }, hidden)


def build_alerts(store, watches, date=None):
    """Alerts for the latest good detect run on or before `date`, against the good run before it (contract 10.5).

    Each alert carries the item's Discover card with watch_id set; rules whose data does not exist yet are
    listed under waiting with the watch's label."""
    run = _run(store, until=date)
    cards, _ = _cards(store, run, "all")
    before = store.latest_detect_run(
        until=(dt.date.fromisoformat(run["run_date"]) - dt.timedelta(days=1)).isoformat())
    fired = alerts.evaluate(watches, [r for r, _, _ in cards], store.item_states(before, "all") if before else None,
                            store.watch_matches(run), store.watch_matches(before) if before else None)
    by_item = {(r["item_id"], r["market"]): c for r, c, _ in cards}
    labels = {w.get("watch_id"): w.get("label") for w in watches}
    out, waiting = [], []
    for a in fired:
        if "waiting" in a:
            waiting.append({"watch_id": a["watch_id"], "label": labels.get(a["watch_id"]), "waiting": a["waiting"]})
        else:
            card = dict(by_item[(a["item_id"], a["market"])], watch_id=a["watch_id"])
            label = alerts._written_title(card, run["run_date"], a["market"]) or a["label"]
            out.append({**a, "label": label, "card": card})
    return {"date": run["run_date"], "run_id": run["run_id"], "alerts": out, "waiting": waiting}
