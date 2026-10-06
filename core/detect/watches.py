"""Watch matches, detect side (BUILD.md 2.9, core/api/contract.md sections 10.5 and 10.7).

run_watches(client, d, detect_run_id, run_id) reads the active watches (L1's v_watches_current, status
'active') and today's item_state rows of the detect run, and appends one intelligence_42_agent.watch_matches
row (watch_id, match_date d, item_id, market, method, run_id) for each item a watch targets whose rule holds
today. Nothing is replaced or removed: the INSERT leaves out a row whose watch, day, item and market the table
already holds, so a rerun on the same day adds nothing. No model is called, so the step spends nothing.

Items. Only today's rows of this detect run that the gate would not hold on item_state alone: eligible,
authenticity other than likely_coordinated, geo_status other than not_local, sponsored_share under 0.5 (G5) and cultural_map
status active (so never generic). The rest of the gate (G1 data issue, G4b political, G5b campaign hashtags,
evidence floors) runs in the brief after detect, so the app still gates a match it shows (TRUST.md section 2).

Targets (contract 10.5), and the method each row carries:
- {"kind": "item", "item_id": X}: the item X. method item_id.
- {"kind": "hashtag|sound|creator", "value": V}: cultural_map kind equal and canonical_key equal after the
  engine's own normalising (items.canonical_key for a hashtag; NFKC, casefold and a leading @ dropped for a
  creator; NFKC only for a sound, whose ids keep their case). Sound and creator keys are platform:id, so a
  value without the platform matches the id part on any platform. method canonical_key.
- {"kind": "brand|query", "value": V}: V as whole words, case-insensitive with runs of spaces as one, in the
  label, else the canonical_key, else an alias; a word character on either side means no match, so V never
  matches inside another word. V is text, never a pattern. method label_words, key_words or alias_words.
  The contract's nightly semantic search (tvf_search_items) is not built here; this is its keyword reading.
- Any other kind matches nothing.

Rules (contract 10.5), each read as a level on today's item_state; whether the item entered that level today
is for the reader, which compares with the day before (core/api/alerts.py on lane L4):
- {"state_in": [states]}: state is one of them.
- {"ratio_over": x}: main_ratio at or above x; a NULL ratio (warm-up) never fires.
- {"reach_over": n}: creators3 at or above n.
- {"breakout": true}: the item has a creator breakout today in that market, that is breakout_signals (written by
  core/detect/breakout.py, BUILD.md 2.13, before this step in the detect job) holds a row for it on day d with
  creators at 1 or more, from any run that day.
- {"tone_flip": true}: the item's tone in that market changed sign from the day before: tone_today and
  tone_before (the tones statement, from v_item_tone_daily in agent_views.sql) are both set and one is above 0
  and the other below. Tone is the mean sign of the enrichment tone (task 2.1) of the item's enriched posts dated
  that day, NULL under 5 of them, so a thin day never flips; 0 is no sign, so neutral is never a flip.
- {"creator_surge": true}: a creator item (cultural_map kind creator) has a post dated d at 3 or more times
  its creator's usual views: creator_surges, from core/detect/breakout.py's run_creator_surges (its usual
  views and is_breakout, so also 1,000 views or more), is 1 or more. It is NULL on any other kind of item and
  for a creator that cannot be judged that day (no post dated d with a first reading, or fewer than 5 earlier
  posts with a baseline reading in a measured lane or on the TikTok creator-profile route), never 0.
The tone and surge reads run only when an active watch has that rule. Either one failing (the view or a table
not there yet) leaves its columns NULL, so the rule waits without firing, and names it in counts["unread"];
the other watches still match.
A rule with more than one key fires only when every key holds; an unknown key or an empty rule never fires.

Watch market: ZA, NG or KE matches items in that market only; all matches every market.

A watch whose target or rule is not a JSON object (an array, a string, unparsable text) is skipped, and its
watch_id is listed in counts["bad_watches"]; the other watches still match.
"""

import json
import re
from pathlib import Path

from . import aggregate, breakout, sqlrun
from .items import _SPACE, _fold, _nfkc, canonical_key
from .sqlrun import AGENT, CORE, query

SQL = Path(__file__).parent / "sql" / "watches.sql"
_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)

MATCH_FIELDS = (("watch_id", "STRING"), ("match_date", "DATE"), ("item_id", "STRING"), ("market", "STRING"),
                ("method", "STRING"), ("run_id", "STRING"))
KEY_KINDS = ("hashtag", "sound", "creator")
WORD_KINDS = ("brand", "query")
WAITING = ()


def statements():
    """The named statements of watches.sql, dataset placeholders left in."""
    out = {}
    for piece in sqlrun.split(SQL.read_text(encoding="utf-8")):
        m = _NAME.search(piece)
        out[m.group(1)] = sqlrun._strip_leading_comments(piece[m.end():])
    return out


def as_obj(value):
    """A JSON column as a dict: BigQuery may give it parsed, DuckDB gives text. ValueError when it is not a
    JSON object."""
    if value is None:
        return {}
    obj = json.loads(value) if isinstance(value, str) else value
    if not isinstance(obj, dict):
        raise ValueError(f"not a JSON object: {type(obj).__name__}")
    return obj


def well_formed(w):
    try:
        as_obj(w["target"])
        as_obj(w["rule"])
    except ValueError:
        return False
    return True


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def key_matches(kind, value, key):
    if not isinstance(value, str) or not key:
        return False
    if kind == "hashtag":
        try:
            return canonical_key("hashtag", value) == key
        except ValueError:
            return False
    want = _fold(_nfkc(value).lstrip("@")) if kind == "creator" else _nfkc(value)
    return bool(want) and want in (key, key.split(":", 1)[-1])


def _words(text):
    return _SPACE.sub(" ", _fold(str(text))).strip()


def word_method(value, label, key, aliases):
    """label_words, key_words or alias_words for the first place value appears as whole words, else None."""
    words = _words(value or "")
    if not words:
        return None
    pattern = re.compile(r"(?<!\w)" + re.escape(words) + r"(?!\w)")
    for method, names in (("label_words", [label]), ("key_words", [key]), ("alias_words", aliases or [])):
        if any(name and pattern.search(_words(name)) for name in names):
            return method
    return None


def method_for(target, row):
    kind = target.get("kind")
    if kind == "item":
        return "item_id" if row["item_id"] == target.get("item_id") else None
    if kind in KEY_KINDS:
        hit = row["kind"] == kind and key_matches(kind, target.get("value"), row["canonical_key"])
        return "canonical_key" if hit else None
    if kind in WORD_KINDS:
        return word_method(target.get("value"), row["label"], row["canonical_key"], row["aliases"])
    return None


def fires(rule, row):
    if not rule:
        return False
    for key, value in rule.items():
        if key == "state_in":
            ok = isinstance(value, list) and row["state"] in value
        elif key == "ratio_over":
            ok = _number(value) and _number(row["main_ratio"]) and row["main_ratio"] >= value
        elif key == "reach_over":
            ok = _number(value) and _number(row["creators3"]) and row["creators3"] >= value
        elif key == "breakout":
            ok = value is True and _number(row.get("breakout_creators")) and row["breakout_creators"] >= 1
        elif key == "tone_flip":
            now, then = row.get("tone_today"), row.get("tone_before")
            ok = value is True and _number(now) and _number(then) and now * then < 0
        elif key == "creator_surge":
            ok = value is True and _number(row.get("creator_surges")) and row["creator_surges"] >= 1
        else:
            ok = False
        if not ok:
            return False
    return True


def add_tones(client, d, items, core=CORE, agent=AGENT):
    """Set tone_today and tone_before on each item row from the tones statement; False, with them left NULL,
    when the read fails."""
    for row in items:
        row.update(tone_today=None, tone_before=None)
    try:
        tones = {(r["item_id"], r["market"]): r for r in query(client, statements()["tones"], {"d": d}, core, agent)}
    except Exception:
        return False
    for row in items:
        t = tones.get((row["item_id"], row["market"]))
        if t:
            row.update(tone_today=t["tone_today"], tone_before=t["tone_before"])
    return True


def add_surges(client, d, items, core=CORE, agent=AGENT):
    """Set creator_surges on each item row: the day's breakout post count of a creator item's creator, None for
    other kinds and creators not judged that day. False, with every count left NULL, when the read fails."""
    for row in items:
        row["creator_surges"] = None
    try:
        surges = breakout.run_creator_surges(client, d, core, agent)
    except Exception:
        return False
    by_key = {}
    for (platform, creator_id), n in surges.items():
        try:
            by_key[canonical_key("creator", creator_id, platform)] = n
        except ValueError:
            continue
    for row in items:
        if row["kind"] == "creator":
            row["creator_surges"] = by_key.get(row["canonical_key"])
    return True


def plan(active, items, d, run_id):
    """The watch_matches rows for today: one per watch, item and market, in watch then item order."""
    rows = {}
    for w in active:
        target, rule, market = as_obj(w["target"]), as_obj(w["rule"]), w["market"]
        for row in items:
            if market != "all" and row["market"] != market:
                continue
            method = method_for(target, row)
            if method and fires(rule, row):
                rows.setdefault((w["watch_id"], row["item_id"], row["market"]), {
                    "watch_id": w["watch_id"], "match_date": d, "item_id": row["item_id"],
                    "market": row["market"], "method": method, "run_id": run_id})
    return list(rows.values())


def run_watches(client, d, detect_run_id, run_id, core=CORE, agent=AGENT):
    """Append today's watch_matches rows for detect run detect_run_id under run_id and return counts."""
    st = statements()
    read = query(client, st["watches"], None, core, agent)
    active = [w for w in read if well_formed(w)]
    counts = {"watches": len(read), "waiting": sum(1 for w in active if set(as_obj(w["rule"])) & set(WAITING)),
              "matches": 0, "appended": 0}
    if len(active) < len(read):
        counts["bad_watches"] = [w["watch_id"] for w in read if not well_formed(w)]
    if not active:
        return counts
    items = query(client, st["items"], {"d": d, "run_id": detect_run_id}, core, agent)
    rules = set().union(*(as_obj(w["rule"]) for w in active))
    unread = [rule for rule, add in (("tone_flip", add_tones), ("creator_surge", add_surges))
              if rule in rules and not add(client, d, items, core, agent)]
    if unread:
        counts["unread"] = unread
    rows = plan(active, items, d, run_id)
    counts["matches"] = len(rows)
    if rows:
        aggregate._run(client, st["append"], [aggregate._struct_array("rows", rows, MATCH_FIELDS)], core, agent)
        counts["appended"] = query(client, st["appended"], {"d": d, "run_id": run_id}, core, agent)[0]["n"]
    return counts
