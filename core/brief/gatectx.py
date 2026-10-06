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
    corroborated_unbiased  TRUST.md section 3 step 5 on posts sighted in unbiased_rank or panel: independent authors
                           on 2 platforms, or 3 such authors plus a pinned number.
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

import yaml

from core.detect import sqlrun
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


def _paid(record, hashtags):
    if "sponsored" in (record.get("flags") or []) or any(_tag(h) in PAID_POST_TAGS for h in hashtags):
        return True
    return any(_PAID_TEXT.search(record.get(k) or "") for k in ("text", "quote_text"))


def _handle(record):
    handle = record.get("handle")
    return _norm(handle).strip().lstrip("@").casefold() if handle else None


def _corroborated(evidence, measured_ids, numbers):
    authors = {}
    for record in evidence:
        handle = _handle(record)
        if not handle or record.get("id") not in measured_ids or NOT_INDEPENDENT & set(record.get("flags") or []):
            continue
        authors.setdefault(handle, set()).add(record.get("platform"))
    two_platforms = any(p1 != p2 for a1, s1 in authors.items() for a2, s2 in authors.items() if a1 != a2
                        for p1 in s1 for p2 in s2)
    pinned = any(n.get("query_id") for n in numbers)
    return two_platforms or (len(authors) >= 3 and pinned)


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
    measured = set()
    if ids:
        measured = {r["post_id"] for r in run("post_lanes", {"post_ids": ",".join(ids), "market": market, "d": d})}

    post_tags = {}
    if ids:
        post_tags = {r["post_id"]: r["hashtags"] or [] for r in run("post_tags", {"post_ids": ",".join(ids)})}
    paid = sum(1 for r in evidence if _paid(r, post_tags.get(r.get("id"), ())))
    key = item_row.get("canonical_key") or next((r["canonical_key"] for r in names if r["canonical_key"]), None)

    return {
        "sponsored_share": paid / len(evidence) if evidence else 0,
        "paid_key": _tag(key) if key and _tag(key) in PAID_KEY_TAGS else None,
        "valid_days": valid_days,
        "lane_classes": sorted(lanes),
        "campaign_hashtags": list(campaign_hashtags),
        "political": political,
        "corroborated_unbiased": _corroborated(evidence, measured, numbers),
        "explanation_passed": explanation_passed,
    }
