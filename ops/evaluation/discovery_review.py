"""Closed week human review sample and coherence scoring for discovery (L01).

The sampler freezes a deterministic review sample from a closed week: up to
thirty eligible clusters per market, all of them when fewer exist, in a
seeded order derived from the cluster ids and the week rather than from the
input order. An open week is refused. The scorer computes coherence and
duplication on the reviewed set per market and pooled, with the reviewed
count as the exact denominator. Unreviewed clusters stay unknown, never zero.
"""

import hashlib
import json
import math
import re
from datetime import date, timedelta

UTC_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MARKET = re.compile(r"^[a-z]{2}$")
MAX_SAMPLE_PER_MARKET = 30
CLUSTER_FIELDS = ("cluster_id", "market", "week_start", "eligible")
REVIEW_FIELDS = (
    "cluster_id",
    "market",
    "reviewer",
    "cultural_move",
    "coherent",
    "duplicate",
    "foreign_market",
)
REVIEW_FLAGS = ("coherent", "duplicate", "foreign_market")


def _iso_date(value, message):
    if not isinstance(value, str) or not ISO_DATE.match(value):
        raise ValueError(message)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(message) from None


def _instant_date(value, message):
    if not isinstance(value, str) or not UTC_INSTANT.match(value):
        raise ValueError(message)
    return _iso_date(value[:10], message)


def _markets(markets):
    if (
        not isinstance(markets, (list, tuple))
        or not markets
        or any(
            not isinstance(market, str) or not MARKET.match(market)
            for market in markets
        )
        or len(set(markets)) != len(markets)
    ):
        raise ValueError("markets_invalid")
    return tuple(markets)


def _validate_cluster(row, *, week_start, markets, seen_ids):
    if not isinstance(row, dict) or any(field not in row for field in CLUSTER_FIELDS):
        raise ValueError("cluster_row_invalid")
    cluster_id = row["cluster_id"]
    if not isinstance(cluster_id, str) or not cluster_id:
        raise ValueError("cluster_id_required")
    if cluster_id in seen_ids:
        raise ValueError("duplicate_cluster_id")
    seen_ids.add(cluster_id)
    market = row["market"]
    if not isinstance(market, str) or not MARKET.match(market) or market not in markets:
        raise ValueError("cluster_market_invalid")
    if row["week_start"] != week_start:
        raise ValueError("cluster_week_mismatch")
    if type(row["eligible"]) is not bool:
        raise ValueError("eligible_flag_required")


def _seed(week_start, market, cluster_id):
    return hashlib.sha256(f"{week_start}|{market}|{cluster_id}".encode()).hexdigest()


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def freeze_review_sample(clusters, *, week_start, frozen_at, markets):
    """Freeze the review sample for one closed week.

    The week runs from week_start for seven days and is closed only when the
    freeze instant falls after its last day. Every cluster row must belong to
    that week and to a promised market. A promised market with no eligible
    cluster keeps an empty sample and an unmet gate rather than vanishing.
    """
    start = _iso_date(week_start, "week_start_invalid")
    end = start + timedelta(days=6)
    if _instant_date(frozen_at, "frozen_at_invalid") <= end:
        raise ValueError("week_open")
    promised = _markets(markets)
    if not isinstance(clusters, list):
        raise TypeError("rows_list_required")
    seen_ids = set()
    for row in clusters:
        _validate_cluster(
            row, week_start=week_start, markets=promised, seen_ids=seen_ids
        )
    by_market = {}
    for market in promised:
        eligible = [
            row["cluster_id"]
            for row in clusters
            if row["market"] == market and row["eligible"] is True
        ]
        ordered = sorted(
            eligible, key=lambda cluster_id: _seed(week_start, market, cluster_id)
        )
        sampled = sorted(ordered[:MAX_SAMPLE_PER_MARKET])
        by_market[market] = {
            "eligible": len(eligible),
            "sampled": len(sampled),
            "cluster_ids": sampled,
            "gate": "sampled" if sampled else "unmet",
        }
    sample = {
        "week_start": week_start,
        "week_end": end.isoformat(),
        "frozen_at": frozen_at,
        "sample_size_per_market": MAX_SAMPLE_PER_MARKET,
        "markets": by_market,
    }
    sample["sample_digest"] = _digest(sample)
    return sample


def _validate_sample(sample):
    if not isinstance(sample, dict) or "sample_digest" not in sample:
        raise ValueError("sample_invalid")
    canonical = dict(sample)
    digest = canonical.pop("sample_digest")
    if digest != _digest(canonical):
        raise ValueError("sample_digest_mismatch")
    return sample["markets"]


def _validate_review(row, *, sampled_by_market, seen):
    if not isinstance(row, dict) or any(field not in row for field in REVIEW_FIELDS):
        raise ValueError("review_row_invalid")
    key = (row["market"], row["cluster_id"])
    if key[0] not in sampled_by_market or key[1] not in sampled_by_market[key[0]]:
        raise ValueError("review_outside_sample")
    if key in seen:
        raise ValueError("duplicate_review")
    seen.add(key)
    if not isinstance(row["reviewer"], str) or not row["reviewer"]:
        raise ValueError("reviewer_required")
    if not isinstance(row["cultural_move"], str) or not row["cultural_move"].strip():
        raise ValueError("cultural_move_required")
    if any(type(row[flag]) is not bool for flag in REVIEW_FLAGS):
        raise ValueError("review_flag_required")


def _tally(sampled, rows):
    reviewed = len(rows)
    counts = {flag: sum(row[flag] for row in rows) for flag in REVIEW_FLAGS}
    return {
        "sampled": sampled,
        "reviewed": reviewed,
        "unreviewed": sampled - reviewed,
        "coherent": counts["coherent"],
        "duplicate": counts["duplicate"],
        "foreign_market": counts["foreign_market"],
        "coherence": counts["coherent"] / reviewed if reviewed else None,
        "duplication": counts["duplicate"] / reviewed if reviewed else None,
    }


def score_review_sample(sample, reviews):
    """Score reviews against a frozen sample with reviewed-count denominators.

    Only clusters in the sample may be reviewed, once each. Coherence and
    duplication divide by the reviewed count per market and pooled; a market
    with nothing reviewed reports None for both, and the pooled result is
    complete only when every promised market was observed, meaning it has at
    least one sampled cluster, and every sampled cluster in every market was
    reviewed. A promised market with no observed cluster can never be
    complete, because its gate is unmet rather than vacuously satisfied.
    """
    by_market = _validate_sample(sample)
    if not isinstance(reviews, list):
        raise TypeError("rows_list_required")
    sampled_by_market = {
        market: set(values["cluster_ids"]) for market, values in by_market.items()
    }
    seen = set()
    for row in reviews:
        _validate_review(row, sampled_by_market=sampled_by_market, seen=seen)
    markets = {
        market: _tally(
            values["sampled"], [row for row in reviews if row["market"] == market]
        )
        for market, values in by_market.items()
    }
    pooled = _tally(sum(values["sampled"] for values in by_market.values()), reviews)
    return {
        "sample_digest": sample["sample_digest"],
        "markets": markets,
        "pooled": pooled,
        "complete": _complete(pooled, markets.values()),
    }


def _threshold(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError("threshold_invalid")
    return float(value)


def _counted(tally, field):
    value = tally[field]
    if type(value) is not int or value < 0:
        raise ValueError("result_inconsistent")
    return value


def _rated(tally, field):
    """One stored rate, which is a fraction or nothing at all.

    A count or a flag compares equal to a fraction, so the type is checked
    here rather than left to the comparison that follows.
    """
    value = tally[field]
    if value is not None and type(value) is not float:
        raise ValueError("result_inconsistent")
    return value


def _derived_tally(tally):
    """Recompute every derived field of one tally from its own counts.

    Only sampled, reviewed and the three flag counts are read as counts, and
    each must be a plain count rather than a flag. Both rates must be a
    fraction or nothing. The unreviewed remainder and both rates are derived
    here and compared with what the tally carries, so a stored remainder or
    rate that no longer follows from the counts is refused instead of trusted.
    """
    # ValueError and not TypeError: callers catch the inconsistent result code.
    if not isinstance(tally, dict):
        raise ValueError("result_inconsistent")
    sampled = _counted(tally, "sampled")
    reviewed = _counted(tally, "reviewed")
    counts = {flag: _counted(tally, flag) for flag in REVIEW_FLAGS}
    _rated(tally, "coherence")
    _rated(tally, "duplication")
    # The three flag counts are read out of the tally and written straight back
    # into the derived tally below, so comparing them there compares each count
    # with itself and establishes nothing. Nothing here recomputes a flag count
    # from the review rows, which this function never sees. All that stands
    # behind a flag count is that it is a plain count at or above zero and the
    # bound on the next line, so that bound is not optional.
    #
    # The reviewed bound is not optional either: a tally reviewed more times
    # than it was sampled derives a negative remainder, a negative remainder
    # does not read as incomplete, and in the pooled sum it cancels another
    # market's genuine remainder, so the pool reads complete while clusters are
    # still unreviewed.
    if reviewed > sampled or any(count > reviewed for count in counts.values()):
        raise ValueError("result_inconsistent")
    derived = {
        "sampled": sampled,
        "reviewed": reviewed,
        "unreviewed": sampled - reviewed,
        **counts,
        "coherence": counts["coherent"] / reviewed if reviewed else None,
        "duplication": counts["duplicate"] / reviewed if reviewed else None,
    }
    if set(tally) != set(derived) or any(
        tally[key] != value for key, value in derived.items()
    ):
        raise ValueError("result_inconsistent")
    return derived


def _pooled_from_markets(markets):
    """Derive the pooled tally by summing the per-market tallies.

    The pool is the markets added up and nothing else, so a pooled row that
    was edited on its own, or a completion claim resting on it, cannot pass.
    """
    fields = ("sampled", "reviewed", *REVIEW_FLAGS)
    totals = {field: sum(tally[field] for tally in markets) for field in fields}
    reviewed = totals["reviewed"]
    return {
        **totals,
        "unreviewed": totals["sampled"] - reviewed,
        "coherence": totals["coherent"] / reviewed if reviewed else None,
        "duplication": totals["duplicate"] / reviewed if reviewed else None,
    }


def _complete(pooled, markets):
    return (
        pooled["sampled"] > 0
        and pooled["unreviewed"] == 0
        and all(tally["sampled"] > 0 for tally in markets)
    )


def _gate(tally, minimum_coherence, maximum_duplication):
    # A promised market with no observed cluster has nothing that could meet
    # the gate, and the plan leaves its gate unmet, never unknown.
    if tally["sampled"] == 0:
        return "unmet"
    if tally["reviewed"] == 0:
        return "unknown"
    if tally["unreviewed"] > 0:
        return "incomplete"
    met = (
        tally["coherence"] >= minimum_coherence
        and tally["duplication"] <= maximum_duplication
    )
    return "met" if met else "unmet"


def gate_review_thresholds(result, *, minimum_coherence, maximum_duplication):
    """Apply the frozen C07 thresholds to a scored sample.

    Both thresholds are explicit arguments because they are approved policy
    values, never properties of the data, so there are no defaults. A market
    or the pool is met when its coherence reaches the minimum and its
    duplication stays within the maximum, unmet otherwise, unknown when
    nothing was reviewed, and incomplete while any sampled cluster is still
    unreviewed, so a partial review never reads as met. Every derived field
    is recomputed before it is read: the unreviewed remainder and both rates
    from each tally's own counts, the pooled tally by summing the markets,
    and completion from that derived pool. A stored remainder, pooled row or
    completion flag that disagrees is refused rather than believed, so no
    gate verdict rests on a field the result merely carries.

    What this does not establish: everything is derived from the result's own
    numbers, and nothing here reaches the frozen sample or the review rows
    behind them. The sample digest is copied through, never recomputed and
    never compared with a sample, and no reviewed cluster is checked against
    the sample it claims to come from. A result that was fabricated whole, and
    is internally consistent, passes this gate. It says the verdict follows
    from the numbers, not that the numbers were ever measured.
    """
    minimum = _threshold(minimum_coherence)
    maximum = _threshold(maximum_duplication)
    # A market collection that is not a mapping is an inconsistent result, not
    # an attribute error, because the inconsistency is what callers catch. One
    # tally that is not a mapping is refused where it is derived.
    if not isinstance(result["markets"], dict):
        raise ValueError("result_inconsistent")
    markets = {
        market: _derived_tally(tally) for market, tally in result["markets"].items()
    }
    pooled = _pooled_from_markets(markets.values())
    if _derived_tally(result["pooled"]) != pooled:
        raise ValueError("result_inconsistent")
    complete = _complete(pooled, markets.values())
    if result["complete"] is not complete:
        raise ValueError("result_inconsistent")
    return {
        "sample_digest": result["sample_digest"],
        "thresholds": {"minimum_coherence": minimum, "maximum_duplication": maximum},
        "markets": {
            market: _gate(tally, minimum, maximum) for market, tally in markets.items()
        },
        "pooled": _pooled_gate(pooled, markets.values(), minimum, maximum),
        "complete": complete,
    }


def _pooled_gate(pooled, markets, minimum_coherence, maximum_duplication):
    """The pooled verdict, which no unobserved promised market lets pass.

    Nothing reviewed anywhere stays unknown. Otherwise a promised market with
    no observed cluster makes the pool unmet, so clusters in the other markets
    can never carry the pool past a market the plan leaves unmet.
    """
    if pooled["reviewed"] == 0:
        return "unknown"
    if any(tally["sampled"] == 0 for tally in markets):
        return "unmet"
    return _gate(pooled, minimum_coherence, maximum_duplication)


# The C07 cluster review gate in the plan contracts (2026-09-12-42-plan-contracts.md,
# section C07): reviewed cluster coherence at least 80%, duplicates below 10%
# and zero reviewed foreign-market leakage. Pinned here, never a caller value.
PRODUCT_MARKETS = ("za", "ng", "ke")
C07_REVIEW_THRESHOLDS = {
    "minimum_coherence": 0.8,
    "duplication_below": 0.1,
    "maximum_reviewed_foreign_market": 0,
}


def _c07_verdict(verdict, tally):
    if verdict != "met":
        return verdict
    if (
        tally["duplication"] >= C07_REVIEW_THRESHOLDS["duplication_below"]
        or tally["foreign_market"]
        > C07_REVIEW_THRESHOLDS["maximum_reviewed_foreign_market"]
    ):
        return "unmet"
    return "met"


def gate_c07_thresholds(result):
    """Apply the pinned C07 review gate to a scored sample.

    It runs gate_review_thresholds with the C07 coherence minimum and the
    duplication limit, which re-derives and checks every tally, and then
    tightens a met verdict to unmet where duplication is not strictly below
    the limit or any reviewed cluster was foreign-market, since C07 requires
    duplicates below 10% and zero reviewed foreign-market leakage. The result
    must cover exactly the product markets za, ng and ke; a narrower or wider
    market set refuses, because C07 promises ZA/NG/KE.

    The pooled verdict stays the pooled measurement. The overall ``verdict``
    is the C07 answer: met only when every promised market and the pool are
    met, unmet when any of them is unmet, otherwise incomplete while any is
    incomplete, otherwise unknown. A pool that reads met therefore never
    stands in for a market that failed.
    """
    if not isinstance(result.get("markets"), dict) or set(result["markets"]) != set(
        PRODUCT_MARKETS
    ):
        raise ValueError("markets_not_promised")
    gate = gate_review_thresholds(
        result,
        minimum_coherence=C07_REVIEW_THRESHOLDS["minimum_coherence"],
        maximum_duplication=C07_REVIEW_THRESHOLDS["duplication_below"],
    )
    markets = {
        market: _c07_verdict(verdict, result["markets"][market])
        for market, verdict in gate["markets"].items()
    }
    pooled = _c07_verdict(gate["pooled"], result["pooled"])
    return {
        **gate,
        "thresholds": dict(C07_REVIEW_THRESHOLDS),
        "markets": markets,
        "pooled": pooled,
        "verdict": _overall_verdict((*markets.values(), pooled)),
    }


def _overall_verdict(verdicts):
    if all(verdict == "met" for verdict in verdicts):
        return "met"
    for state in ("unmet", "incomplete"):
        if state in verdicts:
            return state
    return "unknown"
