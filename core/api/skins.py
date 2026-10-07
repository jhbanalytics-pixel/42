"""Client skins: a saved lens over the same engine (core/api/contract.md section 15.1). Pure functions; the routes
and the skins table live in agent_app.py and app.py.

A skin's words pass rule 1 through L1's core.collect.gdelt.blocked(), which also refuses mixed script, and rule 2
here. Its accounts come only from the approved list in skin_accounts.yaml, which holds Albert's entries and no
one else's. In a skin report, an author who is neither an approved organisation account nor allowed a named page
(section 12.1) is never named: display copies lose that author's handle, name and URL, and any @handle that is
not an approved account is masked. Stored quotes stay verbatim, so the K1 and freeze re-checks still pass.
"""
import copy
import logging
import re
import unicodedata
from pathlib import Path

from core.api.investigations import MAX_SUB_QUESTIONS
from core.api.store import creator_key
from core.api.today import LABELS

log = logging.getLogger("f42.skins")

ACCOUNTS_FILE = Path(__file__).resolve().parent / "skin_accounts.yaml"
MARKETS = ("ZA", "NG", "KE")
TEMPLATES = ("weekly_report",)
SKILLS = ("brand-implication", "creator-read", "context-pack")  # Ask skills (section 15.2)
MAX_WORDS = 30
MAX_WATCHES = 50
ANGLES = MAX_SUB_QUESTIONS
ANGLE_CHARS = 200  # agent_app.MAX_VALUE
KEY_RE = re.compile(r"^[a-z0-9_]{2,40}\Z")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")
SCRIPT_GROUPS = {"CJK": "HAN", "HIRAGANA": "HAN", "KATAKANA": "HAN", "KATAKANA-HIRAGANA": "HAN", "IDEOGRAPHIC": "HAN"}
# The whole handle, with the dots and hyphens inside it, so no part of it is left.
MENTION = re.compile(r"@[\w.\-]*\w")
# Profile and post URLs in text: host, then the handle segment (with its @ where the platform writes one).
PROFILE_URL = re.compile(
    r"(?i)(?<![\w-])((?:[\w-]+\.)*(?:"
    r"(?:(?:x|twitter|vxtwitter|fxtwitter|fixupx|fixvx|instagram|facebook)\.com|fb\.(?:com|me))/@?"
    r"|(?:tiktok\.com|threads\.net|threads\.com)/@"
    r"|youtube\.com/(?:@|c/|user/)"
    r"|reddit\.com/(?:u|user)/"
    r"|linkedin\.com/in/"
    r"))([\w.\-]*\w)")
# A Reddit user written on its own, as u/name.
REDDIT_USER = re.compile(r"(?<![\w/])u/[\w-]*\w")
# Short links hide whose post they open, so the whole link is masked.
SHORT_LINK = re.compile(r"(?i)(?<![\w.\-])(?:https?://)?(?:t\.co|vm\.tiktok\.com|vt\.tiktok\.com|instagr\.am|bit\.ly)/"
                        r"[^\s,;)\]}'\"<>]*")
# A known handle or name ends at anything but a word character or hyphen; a dot and a word after it is the same
# person inside a longer name (privperson.bsky.social), while privperson2 is someone else.
KNOWN_END = r"(?![\w\-])"
# For a hidden handle that is itself a host word (HOST_WORDS only), a dot and a top-level domain after it is a host
# (tiktok.com, x.com), not the person, so a hidden "tiktok" or "x" never damages an approved account's link.
HOST_AHEAD = r"(?!\.(?:[\w\-]+\.)*(?:com|net|org|app|io|co|me|am|be|ly|tv)\b)"
# Hidden handles that are also host or short-link words; only these get HOST_AHEAD.
HOST_WORDS = {"x", "twitter", "tiktok", "instagram", "facebook", "fb", "youtube", "youtu", "reddit", "threads",
              "linkedin", "bsky", "t", "vm", "vt", "bit", "lnkd", "instagr", "com", "net", "org", "app", "io", "co",
              "me", "am", "be", "ly", "tv"}
# A handle written as platform:handle or platform_handle (creator keys, item ids).
TAG_PLATFORMS = r"(?:x|twitter|tiktok|instagram|youtube|facebook|fb|reddit|threads|bluesky|linkedin)"
MASK = "@***"
URL_MASK = "***"
LINK_MASK = "[link]"
NAME_KEYS = ("author_name", "display_name", "name")
# Fields that hold structure, not words: never masked, whatever handle an author has.
STRUCTURAL = {"id", "label", "kind", "platform", "market", "markets", "status", "answer_status", "seq", "tier",
              "check", "mode", "at", "date", "as_of"}
STRUCTURAL_ENDS = ("_id", "_ids", "_at", "_date", "_hash")
PERSON_KEYS = ("handle", "author", "author_name", "author_id", "name", "display_name", "creator_id", "url",
               "profile_url")
NOT_ACCEPTED = "Not accepted (rule 1, rule 2 or mixed script)"
NOT_APPROVED = "Only approved organisation accounts"
NO_CHECK = "The word check is not available yet"


class Unavailable(Exception):
    """L1's word check cannot be imported here, so no skin word can be accepted."""


def load_accounts(path=None):
    """{skin_key: [{"platform", "handle", "org", "role"}]} from the approved list."""
    import yaml
    data = yaml.safe_load(Path(path or ACCOUNTS_FILE).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("skin_accounts.yaml must map each skin_key to a list of accounts")
    return {key: [dict(a) for a in entries or []] for key, entries in data.items()}


def _rule_two(value):
    """Rule 2 on the letters alone after NFKC, so no separator of any kind gets the phrase through."""
    letters = "".join(ch for ch in unicodedata.normalize("NFKC", value) if ch.isalpha()).casefold()
    return "googletrends" in letters or "trendsgoogle" in letters


def _mixed_script(value):
    """True when the letters, after NFKC, come from more than one script (a Cyrillic letter standing in for a Latin
    one). The script is the first word of each letter's Unicode name; Japanese kana and kanji count as one."""
    scripts = set()
    for ch in unicodedata.normalize("NFKC", value):
        if ch.isalpha():
            script = unicodedata.name(ch, "").split(" ")[0]
            scripts.add(SCRIPT_GROUPS.get(script, script))
    return len(scripts - {"", "MODIFIER"}) > 1


def check_words(values):
    """None when every value passes rule 1, rule 2 and the mixed-script check, else the refusal (never the word).

    Mixed script is refused here as well as by L1's blocked(), since not every copy of it checks. Raises
    Unavailable when L1's check is not importable: the check fails closed."""
    try:
        from core.collect.gdelt import blocked
    except ImportError as exc:
        raise Unavailable(NO_CHECK) from exc
    for value in values:
        if _rule_two(str(value)) or _mixed_script(str(value)) or blocked(str(value)):
            return NOT_ACCEPTED
    return None


def _words(value, name):
    if value is None:
        return [], None
    if (not isinstance(value, list) or len(value) > MAX_WORDS
            or not all(isinstance(v, str) and 2 <= len(v.strip()) <= 60 for v in value)):
        return None, f"{name} must be a list of at most {MAX_WORDS} phrases, each 2 to 60 characters."
    return list(dict.fromkeys(v.strip() for v in value)), None


def _accounts(value, approved):
    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(a, dict) for a in value):
        return None, "accounts must be a list of {platform, handle}."
    chosen = {}
    for account in value:
        platform, handle = account.get("platform"), account.get("handle")
        key = creator_key(platform, handle) if isinstance(platform, str) and isinstance(handle, str) else None
        if key not in approved:
            return None, NOT_APPROVED
        entry = approved[key]
        chosen.setdefault(key, {f: entry.get(f) for f in ("platform", "handle", "org", "role")})
    return list(chosen.values()), None


def validate(body, accounts):
    """(the clean skin fields, None) or (None, the reason in plain words), for every field limit of section 15.1."""
    key, name, markets = body.get("skin_key"), body.get("name"), body.get("markets")
    if not isinstance(key, str) or not KEY_RE.match(key):
        return None, "skin_key must be 2 to 40 lower-case letters, digits or underscores."
    if not isinstance(name, str) or not 3 <= len(name.strip()) <= 80:
        return None, "name must be 3 to 80 characters."
    if not isinstance(markets, list) or not markets or any(m not in MARKETS for m in markets):
        return None, "markets must list ZA, NG or KE."
    terms, why = _words(body.get("terms"), "terms")
    if why:
        return None, why
    hashtags, why = _words(body.get("hashtags"), "hashtags")
    if why:
        return None, why
    approved = {creator_key(a.get("platform"), a.get("handle")): a for a in accounts.get(key) or []}
    chosen, why = _accounts(body.get("accounts"), approved)
    if why:
        return None, why
    watch_ids = body.get("watch_ids") or []
    if (not isinstance(watch_ids, list) or len(watch_ids) > MAX_WATCHES
            or not all(isinstance(w, str) and ID_RE.match(w) for w in watch_ids)):
        return None, f"watch_ids must be a list of up to {MAX_WATCHES} watch ids."
    if body.get("template") not in TEMPLATES:
        return None, "template must be weekly_report."
    return {"skin_key": key, "name": name.strip(), "markets": list(dict.fromkeys(markets)), "terms": terms,
            "hashtags": hashtags, "accounts": chosen, "watch_ids": list(dict.fromkeys(watch_ids)),
            "template": body["template"]}, None


def _pattern(skin):
    words = [" ".join(w.lstrip("#").split()) for w in (skin.get("terms") or []) + (skin.get("hashtags") or [])]
    words = [w for w in words if w]
    if not words:
        return None
    return re.compile(r"(?<!\w)#?(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.IGNORECASE)


def _matches(pattern, item):
    texts = [item.get("title"), item.get("label"), *(item.get("hashtags") or [])]
    return pattern is not None and any(pattern.search(" ".join(str(t).split())) for t in texts if t)


def _market_words(codes):
    names = [LABELS[m] for m in codes]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def narrow_today(resp, skin, alerts=None):
    """Today narrowed to the skin: its markets, and the cards, held-back and dropped items whose title, label or
    hashtags match a term or hashtag of the skin (whole words, any case). Kept items are unchanged. Each market
    says how many items were kept and left out, and the markets outside the skin are named; the headline stays
    only when its card is kept. alerts, the skin's watches' alerts, are added as they are."""
    out = copy.deepcopy(resp)
    pattern = _pattern(skin)
    markets = []
    for market in out["markets"]:
        if market["market"] not in skin["markets"]:
            continue
        held = market["held_back"]
        dropped = market["dropped"]
        before = len(market["cards"]) + len(market["more"]) + len(held["items"]) + len(dropped["items"])
        market["cards"] = [c for c in market["cards"] if _matches(pattern, c)]
        market["more"] = [c for c in market["more"] if _matches(pattern, c)]
        kept_held = [h for h in held["items"] if _matches(pattern, h)]
        market["held_back"] = {**held, "count": len(kept_held), "items": kept_held,
                               "text": f"{len(kept_held)} held back" if kept_held else "Nothing held back"}
        market["dropped"] = {**dropped, "items": [d for d in dropped["items"] if _matches(pattern, d)]}
        audit = market.get("not_assessed")
        if isinstance(audit, dict):
            unassessed = [item for item in audit.get("items") or [] if _matches(pattern, item)]
            market["not_assessed"] = {**audit, "count": len(unassessed), "items": unassessed}
        if "breaking" in market:
            market["breaking"] = [b for b in market["breaking"] if _matches(pattern, b)]
        kept = len(market["cards"]) + len(market["more"]) + len(kept_held) + len(market["dropped"]["items"])
        market["skin_note"] = {"kept": kept, "left_out": before - kept,
                               "text": f"{kept} of {before} items match this skin's terms and hashtags; "
                                       f"{before - kept} left out"}
        markets.append(market)
    out["markets"] = markets
    if "searching_now" in out:
        out["searching_now"] = [s for s in out["searching_now"] if s.get("market") in skin["markets"]]
    shown = {c["item_id"] for m in markets for c in m["cards"] + m["more"]}
    if out.get("headline") and out["headline"].get("item_id") not in shown:
        out["headline"] = None
    left = [m for m in MARKETS if m not in skin["markets"]]
    out["skin"] = {"skin_id": skin.get("skin_id"), "name": skin.get("name"), "markets": list(skin["markets"]),
                   "left_out_markets": left,
                   "text": f"Not in this skin: {_market_words(left)}" if left else "Every market is in this skin"}
    if alerts is not None:
        ids = set(skin.get("watch_ids") or [])
        alerts = {**alerts, "alerts": [a for a in alerts.get("alerts") or [] if a.get("watch_id") in ids],
                  "waiting": [w for w in alerts.get("waiting") or [] if w.get("watch_id") in ids]}
    out["alerts"] = alerts
    return out


def _angles(groups):
    """Each (heading, words) group packed into phrases of up to ANGLE_CHARS, at most ANGLES in all; when they do
    not fit, the last phrase counts what was not listed."""
    lines = []
    for heading, words in groups:
        line, count = "", 0
        for word in words:
            longer = f"{line}, {word}" if line else f"{heading}: {word}"
            if line and len(longer) > ANGLE_CHARS:
                lines.append((line, count))
                line, count = f"{heading}: {word}", 1
            else:
                line, count = longer, count + 1
        if line:
            lines.append((line, count))
    if len(lines) <= ANGLES:
        return [text for text, _ in lines]
    rest = sum(count for _, count in lines[ANGLES - 1:])
    return [text for text, _ in lines[:ANGLES - 1]] + [
        f"{rest} more terms, hashtags or accounts of the skin are not listed here"]


def report_plan(skin):
    """The POST /api/investigations body for the skin's weekly report (template weekly_report, ENGINE.md section 8)."""
    accounts = [f"{a.get('handle')} on {a.get('platform')}, {a.get('role')} ({a.get('org')})"[:150]
                for a in skin.get("accounts") or []]
    question = (f"Weekly report for {skin['name']} in {_market_words(skin['markets'])}: the main conversations and "
                "trends in its lens, public mentions and tags of its approved accounts, the tracked accounts' "
                "public activity, and the moments of the coming week.")
    return {"question": question, "market": skin["markets"][0] if len(skin["markets"]) == 1 else None,
            "angles": _angles([("Terms", skin.get("terms") or []), ("Hashtags", skin.get("hashtags") or []),
                               ("Approved accounts", accounts)]),
            "skin_id": skin["skin_id"]}


def nameable_authors(store, evidence):
    """Creator keys of the evidence authors a named page allows (section 12.1): every creators row behind the key
    is at page tier now and none is suppressed. An author 42 has no creators row for is not named."""
    from core.api.people import PAGE_TIERS

    keys = {creator_key(e.get("platform"), e.get("handle")) for e in evidence} - {None}
    if not keys:
        return set()
    rows = store.creators_by_handle(sorted(keys)) or []
    suppressed = store.suppressed_creators()
    if suppressed is None:
        return set()  # no suppression list, no one named (fail closed, as the people routes)
    hidden = {creator_key(c.get("platform"), c.get("handle"))
              for c in (store.creators_by_id(sorted(suppressed)) or [] if suppressed else [])}
    by_key = {}
    for c in rows:
        by_key.setdefault(creator_key(c.get("platform"), c.get("handle")), []).append(c)
    return {k for k, cs in by_key.items() if k in keys and k not in hidden
            and all(c.get("tier") in PAGE_TIERS and c.get("creator_id") not in suppressed for c in cs)}


def _structural(key):
    return key in STRUCTURAL or (isinstance(key, str) and key.endswith(STRUCTURAL_ENDS))


def _mask_strings(value, masked, shaped):
    """Every string inside value put through masked, in place; under a structural key, only through shaped."""
    if isinstance(value, str):
        return masked(value)
    if isinstance(value, dict):
        for key in value:
            value[key] = _mask_strings(value[key], shaped if _structural(key) else masked, shaped)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            value[i] = _mask_strings(item, masked, shaped)
    return value


def _known(handles):
    """A pattern for these handles and names as whole words or phrases, any case, with or without @, inside URLs
    too; None when there are none. Spaces inside a name match any run of spaces."""
    if not handles:
        return None

    def alternation(group):
        return "|".join(r"\s+".join(re.escape(part) for part in h.split())
                        for h in sorted(group, key=len, reverse=True))

    hosts = {h for h in handles if h in HOST_WORDS}
    people = set(handles) - hosts
    # Only a handle that is itself a host word gets the host guard; anyone else's own domain names them.
    branches = ([f"(?:{alternation(people)}){KNOWN_END}"] if people else []) + (
        [f"(?:{alternation(hosts)}){KNOWN_END}{HOST_AHEAD}"] if hosts else [])
    return re.compile(r"(?i)(?<![\w.\-])@?(?:" + "|".join(branches) + ")")


def _tagged(handles):
    """A pattern for platform:handle and platform_handle with one of these handles; None when there are none."""
    handles = [h for h in handles if not any(ch.isspace() for ch in h)]
    if not handles:
        return None
    words = "|".join(re.escape(h) for h in sorted(handles, key=len, reverse=True))
    return re.compile(r"(?i)(?<![\w.\-])(" + TAG_PLATFORMS + r"[:_])@?(?:" + words + r")" + KNOWN_END)


def mask_people(record, approved_handles, allowed_authors, hidden=None):
    """A display copy of an Ask record, a dossier body or an event for a skin report.

    Evidence whose author is neither an approved account nor in allowed_authors (creator keys, "platform:handle")
    loses its handle, author name and URL. Then every string in the copy, whichever field holds it, is masked in
    three layers: the known handle of every author stripped that way, as a whole word in any form or URL; the
    handle segment of profile and post URLs; and every @handle. Approved accounts are never masked. The one
    exception is the author fields (handle, name, url) of evidence whose author may be named, which stay as they
    are. hidden, when given, is a set of stripped handles kept across calls (a live event stream); it is added to.
    The record passed in is not changed."""
    approved = set(approved_handles or ())
    named = approved | set(allowed_authors or ())
    bare = {key.split(":", 1)[1] for key in approved}
    hidden = set() if hidden is None else hidden

    out = copy.deepcopy(record)
    kept = []
    for holder in (out, out.get("answer")):
        if not isinstance(holder, dict):
            continue
        for item in holder.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            key = creator_key(item.get("platform"), item.get("handle"))
            if key in named:
                kept.append((item, {k: item[k] for k in PERSON_KEYS if k in item}))
                continue
            if key:
                hidden.add(key.split(":", 1)[1])
            hidden.update(" ".join(item[k].split()).lower() for k in NAME_KEYS
                          if isinstance(item.get(k), str) and item[k].strip())
            for k in PERSON_KEYS:
                item.pop(k, None)
    known, tagged = _known(hidden - bare), _tagged(hidden - bare)

    def shaped(text):
        """The shape layers: short links, platform:handle tags, profile and post URLs, u/name and @handles."""
        text = SHORT_LINK.sub(LINK_MASK, text)
        if tagged is not None:
            text = tagged.sub(lambda m: m.group(1) + URL_MASK, text)
        text = PROFILE_URL.sub(lambda m: m.group(0) if m.group(2).lower() in bare else m.group(1) + URL_MASK, text)
        text = REDDIT_USER.sub(lambda m: m.group(0) if m.group(0)[2:].lower() in bare else "u/" + URL_MASK, text)
        return MENTION.sub(lambda m: m.group(0) if m.group(0)[1:].lower() in bare else MASK, text)

    def masked(text):
        """Every layer: the shapes, then each hidden handle or name as a bare word."""
        text = shaped(text)
        if known is not None:
            text = known.sub(lambda m: MASK if m.group(0).startswith("@") else URL_MASK, text)
        return text

    _mask_strings(out, masked, shaped)
    for item, fields in kept:
        item.update(fields)
    return out


def still_nameable(allowed, get_store):
    """The allowed authors a named page still allows at this moment: page tier now and not on the suppression list
    (the same store reads people.py makes). Anything that cannot be read names no one."""
    if not allowed:
        return []
    try:
        evidence = [{"platform": key.split(":", 1)[0], "handle": key.split(":", 1)[1]} for key in allowed if ":" in key]
        return sorted(nameable_authors(get_store(), evidence) & set(allowed))
    except Exception as exc:
        log.warning("page tiers or suppression could not be read (%s); naming no author", type(exc).__name__)
        return []


def people_for(skin_key, evidence, get_store):
    """Who a skin report may name: {"approved": the approved accounts for skin_key, "allowed": the evidence authors a
    named page allows now}, as creator keys. A list or tier that cannot be read names fewer people, never more."""
    try:
        listed = (load_accounts().get(skin_key) or []) if skin_key else []
    except Exception:
        log.exception("the approved accounts list could not be read; naming no account")
        listed = []
    approved = sorted({k for k in (creator_key(a.get("platform"), a.get("handle")) for a in listed) if k})
    try:
        allowed = sorted(nameable_authors(get_store(), evidence)) if evidence else []
    except Exception as exc:
        log.warning("page tiers could not be read (%s); naming no author", type(exc).__name__)
        allowed = []
    return {"approved": approved, "allowed": allowed}


def mask_event(data, approved_handles, allowed_authors, hidden=None):
    """One live step, evidence or claim event of a skin run, masked as mask_people masks a record. hidden carries
    the handles stripped from earlier events of the same stream, so a later claim naming one is masked too."""
    evidence = data.get("evidence")
    holder = {"evidence": [evidence] if isinstance(evidence, dict) else [], "event": data}
    return mask_people(holder, approved_handles, allowed_authors, hidden)["event"]
