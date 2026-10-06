"""Discovery review (BUILD.md 2.2 check), lifted from ops/evaluation/discovery_review.py, which stays as it was.

Kept from the old module: the sampler, which takes up to 30 clusters per market in the order of
sha256(seed|market|cluster_id) and returns them by cluster id, so the input order never changes the sample, and
refuses a cluster listed twice; and the scorer, which tallies each market and the pool with the reviewed count as
the denominator, leaves an unreviewed cluster unknown rather than scoring it zero, and gives the gate verdicts:
unknown when nothing is reviewed, incomplete while any sampled cluster is unreviewed, unmet for a promised market
with no cluster at all, otherwise met at coherence 80% or more with duplicates under 10%. The overall verdict is met
only when every market and the pool are met, else unmet, incomplete or unknown, in that order.

Changed: the rows are cluster_review.sql rows, every one eligible, and the seed stands for the week, since the SQL's
date window already bounds the week (there is no frozen_at and no open-week refusal). Marks come from the CSV sheet
the reviewer fills in, not from dict rows, and a row with neither mark is unreviewed. Dropped: the sample digest,
the reviewer and cultural_move fields, the foreign-market flag, and the re-derivation of a stored result. The C07
foreign-market check (no reviewed cluster may be foreign-market) is dropped with that flag.

The promised markets default to za, ng and ke, the markets C07 promises; a market set that is empty, repeats a market
or names one outside those three is refused, as the old module refused one. Sheet rows of a market outside the
promised set, such as pan, are reported apart under other_markets and do not feed the pool or the overall verdict.
"""
from __future__ import annotations

import csv
import hashlib
import io

REVIEW_PER_MARKET = 30
PRODUCT_MARKETS = ("za", "ng", "ke")
MIN_COHERENCE = 0.8
DUPLICATION_BELOW = 0.1
REVIEW_COLUMNS = ["cluster_id", "cluster_date", "market", "item_id", "match_kind", "label", "keywords", "posts",
                  "coherent", "duplicate", "note"]
YES = {"yes", "y", "1", "true"}
NO = {"no", "n", "0", "false"}
COUNTED = ("sampled", "reviewed", "coherent", "duplicate")


def _seed(seed, market, cluster_id):
    return hashlib.sha256(f"{seed}|{market}|{cluster_id}".encode("utf-8")).hexdigest()


def review_sample(rows, seed, per_market=REVIEW_PER_MARKET):
    """Up to per_market cluster_review.sql rows per market, the first in seeded hash order, listed by market and then
    cluster id."""
    by_market, seen = {}, set()
    for row in rows:
        cluster_id = str(row["cluster_id"])
        if cluster_id in seen:
            raise ValueError(f"{cluster_id}: listed twice")
        seen.add(cluster_id)
        by_market.setdefault(str(row["market"]), []).append(row)
    sample = []
    for market in sorted(by_market):
        ordered = sorted(by_market[market], key=lambda r: _seed(seed, market, r["cluster_id"]))
        sample += sorted(ordered[:per_market], key=lambda r: str(r["cluster_id"]))
    return sample


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def review_sheet(rows, path):
    """The reviewer's CSV: one row per sampled cluster with coherent and duplicate left blank."""
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "cluster_id": row["cluster_id"], "cluster_date": _iso(row.get("cluster_date")),
                "market": row["market"], "item_id": row.get("item_id"), "match_kind": row.get("match_kind"),
                "label": row.get("label"), "keywords": "; ".join(row.get("keywords") or []),
                "posts": " | ".join(row.get("posts") or []), "coherent": "", "duplicate": "", "note": ""})
    return path


def _mark(value, cluster_id, column):
    mark = str(value or "").strip().lower()
    if mark == "":
        return None
    if mark in YES:
        return True
    if mark in NO:
        return False
    raise ValueError(f"{cluster_id}: {column} is {value!r}; mark yes or no, or leave it blank")


def _tally(counts):
    reviewed = counts["reviewed"]
    return {**counts, "unreviewed": counts["sampled"] - reviewed,
            "coherence": counts["coherent"] / reviewed if reviewed else None,
            "duplication": counts["duplicate"] / reviewed if reviewed else None}


def _gate(tally):
    if tally["sampled"] == 0:
        return "unmet"
    if tally["reviewed"] == 0:
        return "unknown"
    if tally["unreviewed"] > 0:
        return "incomplete"
    met = tally["coherence"] >= MIN_COHERENCE and tally["duplication"] < DUPLICATION_BELOW
    return "met" if met else "unmet"


def review_score(csv_text, markets=PRODUCT_MARKETS):
    """Coherence and duplication per market and pooled over the reviewed clusters of a marked sheet. markets names
    the markets promised a review; one with no row in the sheet is unmet, and so is the pool. Rows of any other
    market (the pooled 'pan' run's clusters) are tallied under other_markets, with a gate of their own, and feed
    neither the pool nor the overall verdict."""
    if (not isinstance(markets, (list, tuple)) or not markets or len(set(markets)) != len(markets)
            or any(m not in PRODUCT_MARKETS for m in markets)):
        raise ValueError(f"markets must be distinct markets of {PRODUCT_MARKETS}, got {markets!r}")
    counts = {m: dict.fromkeys(COUNTED, 0) for m in markets}
    other = {}
    seen = set()
    for row in csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))):
        if row["cluster_id"] in seen:
            raise ValueError(f"{row['cluster_id']}: reviewed twice")
        seen.add(row["cluster_id"])
        tally = (counts if row["market"] in counts else other).setdefault(row["market"], dict.fromkeys(COUNTED, 0))
        tally["sampled"] += 1
        coherent = _mark(row.get("coherent"), row["cluster_id"], "coherent")
        duplicate = _mark(row.get("duplicate"), row["cluster_id"], "duplicate")
        if coherent is None and duplicate is None:
            continue
        if coherent is None or duplicate is None:
            raise ValueError(f"{row['cluster_id']}: mark both coherent and duplicate, or neither")
        tally["reviewed"] += 1
        tally["coherent"] += coherent
        tally["duplicate"] += duplicate
    tallies = {m: _tally(c) for m, c in sorted(counts.items())}
    result = {m: {**t, "gate": _gate(t)} for m, t in tallies.items()}
    pooled = _tally({k: sum(c[k] for c in counts.values()) for k in COUNTED})
    if pooled["reviewed"] == 0:
        pooled["gate"] = "unknown"
    elif any(t["sampled"] == 0 for t in tallies.values()):
        pooled["gate"] = "unmet"
    else:
        pooled["gate"] = _gate(pooled)
    gates = [t["gate"] for t in result.values()] + [pooled["gate"]]
    overall = "met" if all(g == "met" for g in gates) else next(
        (g for g in ("unmet", "incomplete") if g in gates), "unknown")
    other_tallies = {m: _tally(c) for m, c in sorted(other.items())}
    other_markets = {m: {**t, "gate": _gate(t)} for m, t in other_tallies.items()}
    return {"markets": result, "pooled": pooled, "gate": overall, "other_markets": other_markets}
