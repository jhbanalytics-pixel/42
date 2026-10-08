"""The context core/trust/gate.py's gate_card needs for one morning-brief candidate (BUILD.md 1.12, TRUST.md
section 2).

    build_ctx(client, item_row, d, market, evidence, *, campaign_hashtags, political_terms,
              explanation_passed=None, numbers=(), core=CORE, agent=AGENT) -> ctx
    load_political_terms(market, path=POLITICAL) -> [term]
    load_campaign_hashtags(path=CAMPAIGN) -> [tag]

item_row: a v_item_state_current row as a dict, optionally with label, canonical_key and hashtags.
evidence and numbers: the evidence pack's lists from core/brief/evidence.py.

ctx keys:
    valid_days             [d, d-1, d-2] on the item's main platform in the market: True when every baseline series
                           (unbiased_rank, panel, unbiased_counter) of that platform was valid that day, False when
                           any was invalid, None with no baseline health row. Search, placebo and watchlist rows are
                           never baseline and do not count. The main platform is the main series' platform, else the
                           platform with most sightings in 14 days. The main series' own route counts as well when
                           its health rows name no platform (the culture desk panel).
    lane_classes           sorted distinct lane classes of the item's sightings in the market over 14 days, plus
                           unbiased_rank when a rank list or board read it there (board-only items are measured).
                           A counter_post_views re-read counts as watchlist (DATA.md 3.2).
    campaign_hashtags      passed through.
    political              True when a political term for the market matches the item's label, canonical key,
                           hashtags or any evidence text; else False, never None.
    corroborated_unbiased  TRUST.md section 3 step 5 on posts sighted in unbiased_rank or panel: unrelated authors
                           on 2 platforms, or 3 unrelated authors plus a pinned number (W8-DEC-16). Authors are
                           unrelated when no post of one reuses media, caption text, a linked page or a reply
                           relation with a post of the other, and a person posting under several handles counts
                           once. independent_groups below builds the groups; a signal that is not stored never
                           links two authors.
    explanation_passed     passed through.
    sponsored_share        share of the evidence records that are paid: flagged sponsored, or carrying a paid-post
                           marker (PAID_POST_TAGS as a whole hashtag in the text or the post's stored hashtags, or
                           the phrase "paid partnership"). The brief gates G5 on the larger of this and item_state's.
    paid_key               the item's own key when it is one of PAID_KEY_TAGS (the item is always Paid-led), else
                           None.
"""

import re
import unicodedata
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml

from core.detect import sqlrun
from core.detect.coaction import URL
from core.detect.neardup import plain_text, shingles, similar
from core.detect.sqlrun import AGENT, CORE

HERE = Path(__file__).parent
SQL = HERE / "sql" / "gatectx.sql"
POLITICAL = HERE / "political.yaml"
CAMPAIGN = HERE / "campaign_hashtags.yaml"
NOT_INDEPENDENT = {"brand", "paid", "sponsored", "brand_owned", "generated", "near_duplicate", "flagged"}
LONG_TERM = 5  # letters; a term this long matches anywhere inside a hashtag
# Caption markers of paid posts, read until enrichment fills post_enrichment.sponsored. #collab and #partner also
# mark ordinary music collaborations, so in a post they are not paid markers; as an item's own key they are.
PAID_POST_TAGS = {"ad", "ads", "sponsored", "spon", "paidpartnership", "advert", "advertisement"}
PAID_KEY_TAGS = PAID_POST_TAGS | {"partner", "collab"}
_PAID_TEXT = re.compile(r"(?<![\w#])#(?:" + "|".join(sorted(PAID_POST_TAGS)) + r")(?!\w)|\bpaid\s+partnership\b",
                        re.IGNORECASE)

_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)
QUERIES = {_NAME.search(s).group(1): s for s in sqlrun.split(SQL.read_text(encoding="utf-8"))}
_TAG_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[^\W\d_]+")


def load_political_terms(market, path=POLITICAL):
    lists = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return list(lists.get("all") or []) + list(lists.get(market) or [])


def load_campaign_hashtags(path=CAMPAIGN):
    return list((yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("hashtags") or [])


def _norm(text):
    return unicodedata.normalize("NFKC", str(text))


def _squash(text):
    return "".join(ch for ch in _norm(text).casefold() if ch.isalpha())


def _in_text(term, text):
    """Whole-word match; a term in capitals matches only in capitals."""
    words = r"\s+".join(re.escape(w) for w in term.split())
    flags = 0 if term.isupper() else re.IGNORECASE
    return re.search(rf"(?<!\w){words}(?!\w)", _norm(text), flags) is not None


def _in_tag(term, tag):
    tag = _norm(tag).lstrip("#")
    squashed, key = _squash(tag), _squash(term)
    if not key:
        return False
    if squashed == key or (len(key) >= LONG_TERM and key in squashed):
        return True
    return _in_text(term, " ".join(_TAG_WORDS.findall(tag)))


def _political(terms, texts, tags):
    return any(_in_text(t, x) for t in terms for x in texts) or any(_in_tag(t, g) for t in terms for g in tags)


def _tag(value):
    return _norm(value).strip().lstrip("#").casefold()


def paid_markers(record, hashtags):
    """True when the post's stored hashtags or its caption carry a paid-post marker (not the sponsored flag)."""
    if any(_tag(h) in PAID_POST_TAGS for h in hashtags or ()):
        return True
    return any(_PAID_TEXT.search(record.get(k) or "") for k in ("text", "quote_text"))


def _paid(record, hashtags):
    return "sponsored" in (record.get("flags") or []) or paid_markers(record, hashtags)


def _handle(record):
    handle = record.get("handle")
    return _norm(handle).strip().lstrip("@").casefold() if handle else None


# Independence (W8-DEC-16). Signals come only from what the pack record and the stored post already hold: the
# thumbnail address (media), the caption or transcript (text), links in the text, the post's own address and
# @mentions of other cited authors (reply, quote and repost relations), and the creator's display name (one person
# under several handles). The tables hold no reply or quote ids, media hashes or links between accounts, so none is
# read.
NAME_MIN = 5  # letters; a display name shorter than this never folds two handles into one person
_MENTION = re.compile(r"(?<![\w@])@(\w[\w.]{0,29})")
_TRACKING = re.compile(r"^(?:utm_.*|fbclid|gclid|igshid|mibextid|si|ref|ref_src|ref_url|s|t|feature|share|cmpid)$")
_HOSTS = {"twitter.com": "x.com"}


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


def independent_groups(records, *, names=None, paid_ids=(), measured_ids=None, item=None):
    """The groups of unrelated authors among records (evidence records).

    An author is a record's handle, folded across platforms, from a record that is not flagged
    NOT_INDEPENDENT, is not in paid_ids and, when measured_ids is given, was sighted in a measured lane. Two handles
    are one person when the creators table gives them the same display name of NAME_MIN letters or more (names:
    post id to display name). Two people are linked when a post of one shares a thumbnail address, a caption
    (co-action's near-duplicate text rule), a link (tracking parameters and bare site addresses left out) or a reply
    relation (the post mentions the other's handle, or links the other's post) with a post of the other. Every
    record of a person links, also the ones not counted as authors. Linked people are one group, through any chain.
    A signal that is missing never links. Returns [{"handles", "platforms", "post_ids"}] in order of first handle.
    """
    names = names or {}
    key = (item or {}).get("canonical_key")
    rows = []
    for r in records:
        handle = _handle(r)
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
        if (NOT_INDEPENDENT & set(r.get("flags") or []) or r.get("id") in paid_ids
                or (measured_ids is not None and r.get("id") not in measured_ids)):
            continue
        g = groups.setdefault(find(row["handle"]), {"handles": set(), "platforms": set(), "post_ids": set()})
        g["handles"].add(row["handle"])
        g["platforms"].add(r.get("platform"))
        g["post_ids"].add(r.get("id"))
    return list(groups.values())


def _corroborated(evidence, measured_ids, numbers, *, names=None, post_tags=None, item=None):
    post_tags = post_tags or {}
    paid_ids = {r.get("id") for r in evidence if _paid(r, post_tags.get(r.get("id"), ()))}
    groups = independent_groups(evidence, names=names, paid_ids=paid_ids, measured_ids=measured_ids, item=item)
    two_platforms = any(p1 != p2 for i, g1 in enumerate(groups) for g2 in groups[i + 1:]
                        for p1 in g1["platforms"] for p2 in g2["platforms"])
    pinned = any(n.get("query_id") for n in numbers)
    return two_platforms or (len(groups) >= 3 and pinned)


def read_post_signals(run, ids, market, d):
    """(measured post ids, post id to creator display name, post id to stored hashtags) for the cited post ids.
    run(name, params) runs the named query of this module."""
    if not ids:
        return set(), {}, {}
    joined = ",".join(ids)
    measured = {r["post_id"] for r in run("post_lanes", {"post_ids": joined, "market": market, "d": d})}
    names = {r["post_id"]: r["display_name"] for r in run("post_authors", {"post_ids": joined})
             if r["display_name"]}
    tags = {r["post_id"]: r["hashtags"] or [] for r in run("post_tags", {"post_ids": joined})}
    return measured, names, tags


def build_ctx(client, item_row, d, market, evidence, *, campaign_hashtags, political_terms, explanation_passed=None,
              numbers=(), core=CORE, agent=AGENT):
    """See the module docstring."""
    def run(name, params):
        return sqlrun.query(client, QUERIES[name], params, core=core, agent=agent)

    item_id = item_row["item_id"]
    base = {"item_id": item_id, "market": market, "d": d}
    sightings = run("sightings", base)

    platform = None
    if item_row.get("main_series_id"):
        rows = run("series_platform", {"series_id": item_row["main_series_id"], "d": d})
        platform = rows[0]["platform"] if rows else None
    if platform is None:
        per_platform = {}
        for row in sightings:
            if row["platform"]:
                per_platform[row["platform"]] = per_platform.get(row["platform"], 0) + row["n"]
        if per_platform:
            platform = min(per_platform, key=lambda p: (-per_platform[p], p))
    days = {}
    if platform:
        days = {r["day"]: r["ok"] for r in run("health", {"market": market, "platform": platform, "d": d,
                                                          "item_id": item_id,
                                                          "series_id": item_row.get("main_series_id") or ""})}
    valid_days = [days.get(d - timedelta(days=i)) for i in range(3)]

    lanes = {row["lane_class"] for row in sightings}
    if run("board", base)[0]["n"]:
        lanes.add("unbiased_rank")

    names = run("names", {"item_id": item_id})
    texts = [item_row.get("label")] + [r["label"] for r in names]
    texts += [r.get(k) for r in evidence for k in ("text", "quote_text")]
    tags = [item_row.get("canonical_key")] + [r["canonical_key"] for r in names] + list(item_row.get("hashtags") or [])
    political = _political(political_terms, [t for t in texts if t], [t for t in tags if t])

    ids = sorted({r["id"] for r in evidence if r.get("id")})
    measured, names_of, post_tags = read_post_signals(run, ids, market, d)
    paid = sum(1 for r in evidence if _paid(r, post_tags.get(r.get("id"), ())))
    key = item_row.get("canonical_key") or next((r["canonical_key"] for r in names if r["canonical_key"]), None)

    return {
        "sponsored_share": paid / len(evidence) if evidence else 0,
        "paid_key": _tag(key) if key and _tag(key) in PAID_KEY_TAGS else None,
        "valid_days": valid_days,
        "lane_classes": sorted(lanes),
        "campaign_hashtags": list(campaign_hashtags),
        "political": political,
        "corroborated_unbiased": _corroborated(evidence, measured, numbers, names=names_of, post_tags=post_tags,
                                               item=item_row),
        "explanation_passed": explanation_passed,
    }
