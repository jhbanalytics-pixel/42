"""A calibrated pair score for topic matching, run in shadow beside the vote rule (METHOD-GAPS section 2, G1a).

cluster.assign decides a match by counting votes: cosine, keyword Jaccard, a shared hashtag or sound, a shared creator
and a recent sighting. Two of those votes together let a recent item match on one shared generic hashtag at any cosine.
This module scores the same candidate pairs another way and decides nothing: cluster.assign only records its output
next to the vote rule's own decision.

The score is the logistic of three terms.
  cosine: W_COSINE times the distance of the centroid cosine from COSINE_MID.
  hashtag and sound overlap: W_FACETS times the evidence in the facets the two share.
  keyword overlap: W_KEYWORDS times the evidence in the keywords the two share.
Evidence is 1 - exp(-SAT * mass), where mass is the sum of the IDF weights of the shared terms. A term's weight is
ln((N + 1) / (df + 1)) / ln(N + 1) over the run's own documents (the current topics and today's
clusters that have any term of that kind). IDF_POWER is 1.0, its value before the fixture was run: a power of 2 was
tried afterwards on that same fixture, which made it a tuning on the test, so it is not used. At power 1 a tag on every
document weighs 0 but a tag on three documents in four still lifts the logit by about 0.5, enough to matter near the
cosine midpoint; a held-out set is the way to settle it. There is no
list of generic tags: fyp, foryou and viral are found out by how often the run itself carries them.

Nothing in the score reads when an item was last seen. A recent sighting is not topical evidence, and in the vote rule
it is the vote that lets weak evidence through. Creators are not read either, for the same reason.

Three rules sit outside the score, in code, so no combination of weak features can match. A pair under COSINE_FLOOR is
refused. A pair whose cluster centroid is under DRIFT_FLOOR from the item's birth centroid is refused, which cuts slow
chaining across weeks; the guard applies only where the item row carries birth_centroid (cultural_map does not store
one yet), and the record says drift_cosine None where it could not be checked. Several clusters of one run may each
choose the same item: the shadow keeps them as one group (shadow_group) instead of leaving all but one to be born as a
duplicate, which is the plan-time merge METHOD-GAPS asks for.

The weights are set by hand from the cases the review named, not fitted. They are starting values for the offline
comparison in ops/pair_score_compare.py, and they stay uncalibrated until there are labelled pairs from the merge
review and discovery review sheets.
"""
from __future__ import annotations

import math
from collections import Counter

import numpy as np

COSINE_FLOOR = 0.65
DRIFT_FLOOR = 0.60
SCORE_MATCH = 0.5
COSINE_MID = 0.82
W_COSINE = 14.0
W_FACETS = 4.0
W_KEYWORDS = 3.0
SAT_FACETS = 2.0
SAT_KEYWORDS = 1.0
IDF_POWER = 1.0
DECIMALS = 6


def _facets(entity):
    tags = {str(h).lstrip("#").lower() for h in entity.get("hashtags") or []}
    return tags | {f"sound:{s}" for s in entity.get("sounds") or []}


def _words(entity):
    return {str(k).casefold() for k in entity.get("keywords") or []}


def idf_weights(docs):
    """The weight of every term over docs (an iterable of sets), in [0, 1]: 0 for a term in every document."""
    docs = [d for d in docs if d]
    if not docs:
        return {}
    n, df = len(docs), Counter(t for d in docs for t in d)
    return {t: (math.log((n + 1) / (k + 1)) / math.log(n + 1)) ** IDF_POWER for t, k in df.items()}


def build_idf(clusters, items):
    """The run's own weights: facets and keywords over the current topics and today's clusters."""
    docs = list(items) + list(clusters)
    return {"facets": idf_weights(_facets(e) for e in docs), "keywords": idf_weights(_words(e) for e in docs),
            "docs": len(docs)}


def _evidence(shared, weights, sat):
    return 1.0 - math.exp(-sat * sum(weights.get(t, 0.0) for t in shared))


def _cosine(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.round(a @ b / (na * nb), DECIMALS)) if na and nb else 0.0


def score_pair(cluster, item, cos, idf):
    """The shadow verdict on one candidate pair: logit, score, the two evidence terms, drift_cosine (None when the
    item carries no birth centroid), accept and the first rule that refused it."""
    facets = _evidence(_facets(cluster) & _facets(item), idf["facets"], SAT_FACETS)
    words = _evidence(_words(cluster) & _words(item), idf["keywords"], SAT_KEYWORDS)
    logit = W_COSINE * (cos - COSINE_MID) + W_FACETS * facets + W_KEYWORDS * words
    score = 1.0 / (1.0 + math.exp(-logit))
    birth = item.get("birth_centroid")
    try:
        with np.errstate(invalid="ignore"):
            drift = _cosine(cluster["centroid"], birth) if birth is not None else None
    except (TypeError, ValueError):
        drift = None  # one malformed birth centroid costs its own pair the guard, not the run its shadow
    if drift is not None and not math.isfinite(drift):
        drift = None  # a nan or inf component would pass the floor and write a nan the sink cannot encode
    if cos < COSINE_FLOOR:
        reason = "cosine_floor"
    elif drift is not None and drift < DRIFT_FLOOR:
        reason = "drift"
    elif score < SCORE_MATCH:
        reason = "score"
    else:
        reason = "accept"
    return {"logit": round(logit, DECIMALS), "score": round(score, DECIMALS), "facet_evidence": round(facets, DECIMALS),
            "keyword_evidence": round(words, DECIMALS), "drift_cosine": drift, "accept": reason == "accept",
            "reason": reason}


def shadow_records(clusters, items, judged, eligible, decisions, run_date, *, variant_cosine, is_dormant):
    """One record per candidate pair the vote rule looked at, in its order. judged[r] is cluster r's candidates as
    (item index, cosine, votes), eligible the (cluster, item) index pairs the vote rule made eligible and decisions
    its decisions. Each record carries the vote rule's side (today_*) and the shadow's (shadow_*); the shadow decision
    is the cluster's, repeated on each of its records."""
    idf = build_idf(clusters, items)
    scored = [[score_pair(c, items[j], cos, idf) for j, cos, _ in judged[r]] for r, c in enumerate(clusters)]
    chosen = []
    for r, per in enumerate(judged):
        ok = [(-s["score"], items[j]["item_id"], j) for (j, _, _), s in zip(per, scored[r]) if s["accept"]]
        if ok:
            j = min(ok)[2]
            chosen.append(("recurrence" if is_dormant(items[j].get("last_seen"), run_date) else "match", j))
        elif per and per[0][1] >= variant_cosine:
            chosen.append(("variant", per[0][0]))
        else:
            chosen.append(("new", None))
    groups = {}
    for c, (kind, j) in zip(clusters, chosen):
        if kind in ("match", "recurrence"):
            groups.setdefault(j, []).append(c["cluster_id"])
    records = []
    for r, c in enumerate(clusters):
        kind, j = chosen[r]
        group = sorted(groups[j]) if kind in ("match", "recurrence") and len(groups[j]) > 1 else None
        for (k, cos, got), s in zip(judged[r], scored[r]):
            records.append({
                "cluster_id": c["cluster_id"], "item_id": items[k]["item_id"], "cosine": cos, "today_votes": got,
                "today_eligible": (r, k) in eligible, "today_kind": decisions[r]["kind"],
                "today_item_id": decisions[r]["item_id"], "shadow_logit": s["logit"], "shadow_score": s["score"],
                "shadow_accept": s["accept"], "shadow_reason": s["reason"], "facet_evidence": s["facet_evidence"],
                "keyword_evidence": s["keyword_evidence"], "drift_cosine": s["drift_cosine"],
                "shadow_kind": kind, "shadow_item_id": items[j]["item_id"] if j is not None else None,
                "shadow_group": group, "corpus_docs": idf["docs"]})
    return records
