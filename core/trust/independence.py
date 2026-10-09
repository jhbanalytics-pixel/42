"""Independent authors for Corroborated (W8-DEC-16, TRUST.md section 3 step 5).

    independent_groups(records, *, excluded, names=None, paid_ids=(), author_ids=None, item=None) -> [group]
    is_corroborated(groups, metric) -> bool
    platform_name(platform) -> str

Corroborated: unrelated authors on 2 platforms, or 3 unrelated authors plus a metric. Authors are unrelated when no
post of one reuses media, caption text, a linked page or a reply relation with a post of the other, and a person
posting under several handles counts once. The brief gate (core/brief/gatectx.py), the card label (core/trust/claims.py
K5) and the Ask label (core/agent/checks.py K5) all call this module, so the three agree.

Signals come only from what an evidence record and the stored post already hold: the thumbnail address (media), the
caption or transcript (text), links in the text, the post's own address and @mentions of other cited authors (reply,
quote and repost relations), and, where the caller has it, the creator's display name (one person under several
handles). The tables hold no reply or quote ids, media hashes or links between accounts, so none is read. A signal
that is missing never links two authors.

This module imports nothing from core.detect so the trust package stays light. The text rule is co-action's
(core/detect/coaction.py); core/trust/tests/test_trust_independence.py pins this copy of it equal to coaction's.
"""

import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit

JACCARD = 0.8       # coaction.JACCARD
SHINGLE = 5         # coaction.SHINGLE
MIN_TEXT_CHARS = 20  # coaction.MIN_TEXT_CHARS
NAME_MIN = 5        # letters; a display name shorter than this never folds two handles into one person
PLACEHOLDERS = {"URL", "HANDLE", "NUM"}
PLATFORM_ALIASES = {"twitter": "x"}

URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HANDLE = re.compile(r"@\w+")
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
_TOKEN = re.compile(r"#?\w+")
_MENTION = re.compile(r"(?<![\w@])@(\w[\w.]{0,29})")
_TRACKING = re.compile(r"^(?:utm_.*|fbclid|gclid|igshid|mibextid|si|ref|ref_src|ref_url|s|t|feature|share|cmpid)$")
_HOSTS = {"twitter.com": "x.com"}


def _norm(text):
    return unicodedata.normalize("NFKC", str(text))


def _squash(text):
    return "".join(ch for ch in _norm(text).casefold() if ch.isalpha())


def platform_name(platform):
    """The platform as K5 counts it: lower case, with twitter folded into x."""
    name = str(platform or "").strip().lower()
    return PLATFORM_ALIASES.get(name, name)


def handle_of(record):
    handle = record.get("handle")
    return _norm(handle).strip().lstrip("@").casefold() if handle else None


def mask(text):
    """Lower-cased word and hashtag tokens with links as URL, handles as HANDLE and numbers as NUM (coaction.mask)."""
    t = URL.sub(" URL ", (text or "").lower())
    t = _HANDLE.sub(" HANDLE ", t)
    t = _NUMBER.sub(" NUM ", t)
    return " ".join(_TOKEN.findall(t))


def plain_text(text, key=None):
    """The caption as co-action compares it: lower case, the item's own key (when given), links, handles and numbers
    masked out, hashtags left out; None when fewer than MIN_TEXT_CHARS characters remain."""
    text = str(text or "").casefold()
    if key:
        text = re.sub(r"(?<!\w)#?" + re.escape(str(key).casefold()) + r"(?!\w)", " ", text)
    plain = " ".join(t for t in mask(text).split() if not t.startswith("#") and t not in PLACEHOLDERS)
    return plain if len(plain) >= MIN_TEXT_CHARS else None


def shingles(text):
    return {text[i:i + SHINGLE] for i in range(len(text) - SHINGLE + 1)}


def similar(a, b):
    """Jaccard of two shingle sets at JACCARD or more."""
    union = len(a | b)
    return bool(union) and len(a & b) / union >= JACCARD


def _host(parts):
    host = (parts.hostname or "").casefold()
    for prefix in ("www.", "m.", "mobile."):
        host = host.removeprefix(prefix)
    return _HOSTS.get(host, host)


def _address(raw, *, query=True):
    """host and path (and the query, less tracking parameters) of a link, or None for a bare site address."""
    raw = _norm(raw or "").strip().rstrip(".,;:!?)\"'")
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else "//" + raw)
    host, path = _host(parts), parts.path.rstrip("/").casefold()
    kept = sorted((k.casefold(), v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k.casefold()))
    if not host or not (path or (query and kept)):
        return None
    tail = "&".join(f"{k}={v}" for k, v in kept) if query else ""
    return f"{host}{path}?{tail}" if tail else f"{host}{path}"


def _text_of(record):
    return record.get("quote_text") or record.get("text") or ""


def _links(record):
    return {a for a in (_address(u) for u in URL.findall(_text_of(record))) if a}


def _name_key(name):
    key = _squash(name) if name else ""
    return key if len(key) >= NAME_MIN else None


def independent_groups(records, *, excluded, names=None, paid_ids=(), author_ids=None, item=None):
    """The groups of unrelated authors among records (evidence records).

    An author is a record's handle, folded across platforms, from a record whose flags (lower-cased) miss excluded, that
    is not in paid_ids and, when author_ids is given, is one of those ids. Two handles are one person when the creators
    table gives them the same display name of NAME_MIN letters or more (names: post id to display name). Two people are
    linked when a post of one shares a thumbnail address, a caption (co-action's near-duplicate text rule), a link
    (tracking parameters and bare site addresses left out) or a reply relation (the post mentions the other's handle, or
    links the other's post) with a post of the other. Every record of a person links, also the ones not counted as
    authors, so records may be a wider pool than the cited ones. Linked people are one group, through any chain.
    A signal that is missing never links. Returns [{"handles", "platforms", "post_ids"}] in order of first handle.
    """
    names = names or {}
    key = (item or {}).get("canonical_key")
    rows = []
    for r in records:
        handle = handle_of(r)
        if not handle:
            continue
        own = _address(r.get("url"))
        text = plain_text(_text_of(r), key)
        rows.append({"r": r, "handle": handle, "own": own, "media": _address(r.get("thumbnail_url"), query=False),
                     "shingles": shingles(text) if text else None, "links": _links(r) - {own},
                     "mentions": {m.rstrip(".").casefold() for m in _MENTION.findall(_text_of(r))}})
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def link(a, b):
        parent[find(a)] = find(b)

    for row in rows:
        find(row["handle"])
    by_name = {}
    for row in rows:
        name = _name_key(names.get(row["r"].get("id")))
        if name:
            link(row["handle"], by_name.setdefault(name, row["handle"]))
    for i, x in enumerate(rows):
        for y in rows[i + 1:]:
            if find(x["handle"]) == find(y["handle"]):
                continue
            if ((x["media"] and x["media"] == y["media"])
                    or (x["shingles"] and y["shingles"] and similar(x["shingles"], y["shingles"]))
                    or (x["links"] & y["links"])
                    or y["handle"] in x["mentions"] or x["handle"] in y["mentions"]
                    or (y["own"] and y["own"] in x["links"]) or (x["own"] and x["own"] in y["links"])):
                link(x["handle"], y["handle"])
    groups = {}
    for row in rows:
        r = row["r"]
        flags = {str(f).lower() for f in r.get("flags") or []}
        if (excluded & flags or r.get("id") in paid_ids
                or (author_ids is not None and r.get("id") not in author_ids)):
            continue
        g = groups.setdefault(find(row["handle"]), {"handles": set(), "platforms": set(), "post_ids": set()})
        g["handles"].add(row["handle"])
        if platform_name(r.get("platform")):  # a record with no platform is not a platform
            g["platforms"].add(platform_name(r.get("platform")))
        g["post_ids"].add(r.get("id"))
    return list(groups.values())


def is_corroborated(groups, metric):
    """Unrelated groups on 2 different platforms, or 3 unrelated groups plus a metric."""
    two_platforms = any(p1 != p2 for i, g1 in enumerate(groups) for g2 in groups[i + 1:]
                        for p1 in g1["platforms"] for p2 in g2["platforms"])
    return two_platforms or (len(groups) >= 3 and bool(metric))
