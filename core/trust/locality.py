"""The locality_v2 reader (C4 v3 appendix D, sections 9 and 12) and the constants every other piece of code and SQL is
tested against (test L-12).

One reader for every consumer, on the acting path and on the rendering path alike. Nothing it decides comes from the
row's own claims about itself: the status and share are recomputed from the integer counts with this module's
constants, the expected metric version is the caller's, and the authority is the LOCALITY_AUTHORITY constant below.
verify() is the write-time check the detect step runs on each key with its members in hand.

W8-DEC-17 label: the stored row is counts. Whether a `local` row may carry the label Local is a second result of the
same counts, label(), which needs the lower end of the 95% Wilson interval of the local share to reach LABEL_FLOOR.
"""

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass

METRIC_VERSION = "locality_v2.1"
MIN_KNOWN = 8                 # fewer valid known posts than this: Market unconfirmed
SHARE_NUM, SHARE_DEN = 3, 5   # 0.6 as an integer ratio, so the boundary never meets a float
MIN_CONFIDENCE = 0.7
VALID_SOURCES = ("ext_region", "home_market", "place_mention")
LANE_CLASSES = ("unbiased_rank", "panel")
FEED_EXCLUDED_ROUTE = "youtube/videos/trending"
WINDOW_DAYS = 7
ZONES = {"ZA": "Africa/Johannesburg", "NG": "Africa/Lagos", "KE": "Africa/Nairobi"}
COUNTS = ("population_posts", "known_posts", "local_posts", "foreign_posts", "unknown_posts")
DERIVED_COUNTS = ("feed_only_posts", "vetoed_feed_posts", "local_creators", "known_creators", "feed_only_creators",
                  "breadth_creators")

# W8-DEC-17 (typed 8 Oct 2026). The payload block version moves with these two, never the metric version.
WILSON_Z = 1.96
LABEL_FLOOR = 0.5
BLOCK_VERSION = 1

# W8-DEC-01a (typed 9 Oct 2026): which rule writes item_state.eligible. The release that switches sets it to "v2";
# a row records the rule that wrote it in item_state.locality_basis, and the readers follow the row.
LOCALITY_AUTHORITY = "v1"
V1_BASIS = "v1"
V2_BASIS = METRIC_VERSION

# Q14 (typed 9 Oct 2026): a label-only change between consecutive days applies only when the two clusters share at
# least this many member posts. The identity code that applies it is core/understand/cluster.py.
MIN_SHARED_MEMBERS = 2


@dataclass(frozen=True)
class Locality:
    status: str            # local | not_local | market_unconfirmed | unreadable
    reason: str | None     # why unreadable, else None
    known: int | None
    local: int | None
    share: float | None


def _unreadable(reason):
    return Locality("unreadable", reason, None, None, None)


def _count(value):
    return type(value) is int


def status_of(known, local):
    """The admission status of two integer counts, section 6.5."""
    if known < MIN_KNOWN:
        return "market_unconfirmed"
    return "local" if SHARE_DEN * local >= SHARE_NUM * known else "not_local"


def read_locality(row, *, expected_version=METRIC_VERSION):
    """The one reader every consumer calls on the acting path and on the rendering path.

    Nothing it decides is taken from the row's own claims about itself: the status and share are recomputed from
    the integer counts with this module's constants, the row's own status is only compared, and a row that carries
    different constants or another version is unreadable, never reinterpreted."""
    if not isinstance(row, Mapping):
        return _unreadable("missing")
    if row.get("metric_version") != expected_version:
        return _unreadable("version")
    values = [row.get(name) for name in COUNTS]
    if any(v is None for v in values):
        return _unreadable("null_count")
    if not all(_count(v) for v in values):
        return _unreadable("count_type")
    population, known, local, foreign, unknown = values
    if min(values) < 0:
        return _unreadable("negative_count")
    if local + foreign != known or known + unknown != population:
        return _unreadable("inconsistent_counts")
    status = status_of(known, local)
    share = local / known if known else None
    if row.get("status") != status:
        return _unreadable("status_mismatch")
    stored = row.get("local_share")
    if (share is None) != (stored is None) or (share is not None and abs(share - stored) > 1e-9):
        return _unreadable("share_mismatch")
    return Locality(status, None, known, local, share)


def wilson_lower(known, local):
    """The lower end of the 95% Wilson interval of local / known (None when nothing is known). The SQL view writes
    the same expression in the same order, and a test compares the two on every pair the table can hold."""
    if not known:
        return None
    p, n, z = local / known, known, WILSON_Z
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def label(status, known, local):
    """W8-DEC-17. `local` is an admission status and not yet a label: the card says Local only when the lower end of
    the Wilson interval of the local share is LABEL_FLOOR or more, else Market unconfirmed. A not_local row is held
    by G6 and never labelled Local, whatever its interval; an unreadable row has no label."""
    if status == "local":
        return "local" if wilson_lower(known, local) >= LABEL_FLOOR else "market_unconfirmed"
    if status in ("market_unconfirmed", "not_local"):
        return status
    return None


def scope_basis(row_basis):
    """market_scope_basis for a market_scope written from a row whose item_state.locality_basis is row_basis (ruling
    M4): locality_v2.1 for a row admitted under the retained locality row, v1 for any other row once the authority
    constant is v2, and None, so that the key stays out of the payload, for a v1 row while the constant is v1. The
    shadow payload therefore stays what it was."""
    if row_basis == V2_BASIS:
        return V2_BASIS
    return V1_BASIS if LOCALITY_AUTHORITY == "v2" else None


def digest(members):
    """The population digest of one key from its retained member rows (dicts with post_id, locality_class,
    creator_key, feed_sighted). Same bytes as locality_summary.sql builds, computed with this code."""
    lines = sorted((m["post_id"], m["locality_class"], m["creator_key"] or "", "1" if m["feed_sighted"] else "0")
                   for m in members)
    return hashlib.sha256("\n".join("|".join(x) for x in lines).encode("utf-8")).hexdigest()


def row_from_prefixed(row, prefix="lrow_"):
    """The brief's mapping from a candidate row to the record read_locality takes: the columns that carry the prefix,
    with the prefix removed. The prefix is lrow_ and not locality_ because item_state already carries a column named
    locality_status (the checked status copied at state time), which s.* brings into the same select."""
    return {k[len(prefix):]: v for k, v in row.items() if k.startswith(prefix)}


def counts_from_members(members):
    """All eleven counts of a summary row, recounted from its member rows: the five class counts and the six derived
    ones. A creator is the creator key, or the post id when the post has none (section 6.4)."""
    def who(m):
        return m["creator_key"] or m["post_id"]

    classes = [m["locality_class"] for m in members]
    feed = [m for m in members if m["feed_sighted"]]
    local = [m for m in members if m["locality_class"] == "local"]
    known = [m for m in members if m["locality_class"] != "unknown"]
    feed_only = [m for m in feed if m["locality_class"] == "unknown"]
    return {
        "population_posts": len(classes), "known_posts": len(known), "local_posts": len(local),
        "foreign_posts": classes.count("foreign"), "unknown_posts": classes.count("unknown"),
        "feed_only_posts": len(feed_only),
        "vetoed_feed_posts": sum(m["locality_class"] == "foreign" for m in feed),
        "local_creators": len({who(m) for m in local}), "known_creators": len({who(m) for m in known}),
        "feed_only_creators": len({who(m) for m in feed_only}),
        "breadth_creators": len({who(m) for m in local} | {who(m) for m in feed_only}),
    }


def verify(summary, members):
    """Write-time verification of one key, in Python with its own code: recount every count from the members,
    compare the digest and the status. True only when every check agrees. The step writes a verification row for a
    key only when this returns True, and both views require that row. A derived count the consumers read, breadth
    among them, is checked too: the reader cannot, because it never holds the members (ruling finding F1)."""
    want = counts_from_members(members)
    if any(summary.get(k) != v for k, v in want.items()):
        return False
    if summary.get("population_digest") != digest(members):
        return False
    got = read_locality(summary)
    return got.status != "unreadable" and got.status == summary.get("status")


def locality_block(record):
    """The versioned `locality_v2` block of a card or held item: what the reader decided from one retained row, the
    W8-DEC-17 label, and the counts that support them. record: the columns of the row, prefix removed. An unreadable
    or absent row gives a block with no counts, never zeros."""
    got = read_locality(record)
    record = record if isinstance(record, Mapping) else {}
    readable = got.status != "unreadable"
    keep = ("population_posts", "unknown_posts", "foreign_posts", "feed_only_posts", "vetoed_feed_posts",
            "breadth_creators")
    return {
        "block_version": BLOCK_VERSION, "metric_version": METRIC_VERSION, "schema_version": record.get("schema_version"),
        "status": got.status, "reason": got.reason, "label": label(got.status, got.known, got.local),
        "known_posts": got.known, "local_posts": got.local, "local_share": got.share,
        **{k: (record.get(k) if readable else None) for k in keep},
    }
