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
                           any was invalid, False with no baseline health row on d-1 or d-2 after the platform's history began (a gap),
                           None with no baseline health row otherwise (no history yet, or d itself). Search, placebo and watchlist rows are
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

import yaml

from core.detect import sqlrun
from core.detect.sqlrun import AGENT, CORE
from core.trust.independence import independent_groups as _groups
from core.trust.independence import is_corroborated

HERE = Path(__file__).parent
SQL = HERE / "sql" / "gatectx.sql"
POLITICAL = HERE / "political.yaml"
CAMPAIGN = HERE / "campaign_hashtags.yaml"
NOT_INDEPENDENT = {"brand", "paid", "sponsored", "brand_owned", "generated", "near_duplicate", "flagged"}
LONG_TERM = 5  # letters; a term this long matches anywhere inside a hashtag
FUSED_MIN = 3  # letters; a term this long, written straight onto ballot or voter in a tag, makes the tag political
# Caption markers of paid posts, read until enrichment fills post_enrichment.sponsored. #collab and #partner also
# mark ordinary music collaborations, so in a post they are not paid markers; as an item's own key they are.
PAID_POST_TAGS = {"ad", "ads", "sponsored", "spon", "paidpartnership", "advert", "advertisement"}
PAID_KEY_TAGS = PAID_POST_TAGS | {"partner", "collab"}
_PAID_TEXT = re.compile(r"(?<![\w#])#(?:" + "|".join(sorted(PAID_POST_TAGS)) + r")(?!\w)|\bpaid\s+partnership\b",
                        re.IGNORECASE)

_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)
QUERIES = {_NAME.search(s).group(1): s for s in sqlrun.split(SQL.read_text(encoding="utf-8"))}
_TAG_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[^\W\d_]+")


class Companion(str):
    """A term that is political only beside one of `beside` (a leader name that is no term on its own) in the same
    text or tag. Beside a term of the list the post is political through that term, so it is not named here."""

    beside = ()


def load_political_terms(market, path=POLITICAL):
    """The market's terms: the list's "all" and its own, then the companion-only terms (Companion strings, each
    carrying the leader companions of the file) that count only beside a companion."""
    lists = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    names = tuple(lists.get("companions") or [])
    companion_only = []
    for word in lists.get("companion_only") or []:
        term = Companion(word)
        term.beside = names
        companion_only.append(term)
    return list(lists.get("all") or []) + list(lists.get(market) or []) + companion_only


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
    plain = [t for t in terms if not isinstance(t, Companion)]
    # A hashtag item's stored key is casefolded; its label and the posts keep the tag as written, so read those too.
    tags = list(tags) + [w for x in texts for w in re.findall(r"#\w+", _norm(x))]
    if any(_in_text(t, x) for t in plain for x in texts) or any(_in_tag(t, g) for t in plain for g in tags):
        return True
    return any(any(_in_text(t, x) and any(_in_text(n, x) for n in t.beside) for x in texts)
               or any(_in_tag(t, g) and (any(_in_tag(n, g) for n in t.beside) or any(_caps_in_tag(n, g) for n in plain)
                                      or _fused_in_tag(t, g, plain))
                      for g in tags)
               for t in terms if isinstance(t, Companion))


def _fused_in_tag(companion, tag, plain):
    """A plain term of three letters or more written straight before or after the companion in the tag, case folded
    and squashed (zumaballot, voteballot, ballotwike); an s after the companion is its plural."""
    squashed, word = _squash(tag), _squash(companion)
    keys = {_squash(n) for n in plain if len(_squash(n)) >= FUSED_MIN}
    for i in range(len(squashed)):
        if squashed.startswith(word, i):
            before, after = squashed[:i], squashed[i + len(word):]
            tails = (after, after[1:]) if after.startswith("s") else (after,)
            if any(before.endswith(k) for k in keys) or any(tail.startswith(k) for tail in tails for k in keys):
                return True
    return False


def _caps_in_tag(term, tag):
    """A party or electoral acronym fused into a tag (ANCvoterdrive): all capitals, bounded by non capitals."""
    return term.isupper() and re.search(r"(?<![A-Z])" + re.escape(term) + r"(?![A-Z])", _norm(tag)) is not None


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


def independent_groups(records, *, names=None, paid_ids=(), measured_ids=None, item=None):
    """The groups of unrelated authors among records, with the gate's not-independent flags
    (core/trust/independence.py independent_groups). measured_ids limits authors to posts sighted in a measured
    lane; every record still links."""
    return _groups(records, excluded=NOT_INDEPENDENT, names=names, paid_ids=paid_ids, author_ids=measured_ids,
                   item=item)


def _corroborated(evidence, measured_ids, numbers, *, names=None, post_tags=None, item=None):
    post_tags = post_tags or {}
    paid_ids = {r.get("id") for r in evidence if _paid(r, post_tags.get(r.get("id"), ()))}
    groups = independent_groups(evidence, names=names, paid_ids=paid_ids, measured_ids=measured_ids, item=item)
    return is_corroborated(groups, any(n.get("query_id") for n in numbers))


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
    days, first = {}, None
    if platform:
        health = {"market": market, "platform": platform, "d": d, "item_id": item_id,
                  "series_id": item_row.get("main_series_id") or ""}
        days = {r["day"]: r["ok"] for r in run("health", health)}
        first = (run("health_first", health) or [{}])[0].get("first_day")
    valid_days = [days.get(d - timedelta(days=i)) for i in range(3)]
    # A day before d with no baseline row, after the platform's history began, is a gap and so invalid (TRUST.md G1).
    # d itself stays None: the brief runs only after a good collect run for d, so a missing d is no collect failure.
    for i in (1, 2):
        if valid_days[i] is None and first is not None and first < d - timedelta(days=i):
            valid_days[i] = False

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
