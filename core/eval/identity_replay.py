"""Read-only identity diagnostic over retained daily cluster exports, run where the exports are allowed to live (Albert's
PC or the WPP environment), never in a shared or cloud context.

    py -3.13 -m core.eval.identity_replay --clusters clusters-native.json --centroids cluster-centroids-native.json \
        --out identity-replay [--labels label-me.csv]

Inputs are the two retained exports, each {"rows": [...]}. clusters-native.json rows carry market, item_id,
cluster_date, cluster_id, label, keywords, match_kind, members, min_probability, avg_probability and examples;
cluster-centroids-native.json rows carry market, item_id, cluster_date, cluster_id, centroid, keywords and label. Rows
join on market, cluster_date and cluster_id. members and examples are never read or written.

For every item, consecutive daily clusters assigned to it make one transition, with the cosine between the two daily
centroids and the keyword Jaccard (blanks ignored). Each transition gets selection reasons, worked out from those two
numbers, the gap in days and the labels only:
    missing_centroid                no usable centroid on one side, so no cosine
    missing_keywords                no keywords on one side, so keyword support cannot be judged
    high_cosine_no_keyword_support  cosine 0.82 or more, keyword Jaccard under 0.10
    keywords_without_similarity     keyword Jaccard 0.10 or more, cosine under 0.82
    no_visible_identity             neither reaches the match rule
    gap_over_recent                 more than 7 days between the two clusters
    label_changed                   the two daily labels differ
    supported_control               cosine and keywords both reach the rule and nothing above applies
A reason says why a transition is worth a look. It is not a verdict: these exports lack the hashtag, sound and creator
evidence the matcher also used, and prior daily centroids stand in for the item's EMA centroid, so this is a proxy.

Sampling is deterministic. When there are at most --full-limit transitions every one is selected; otherwise each
category (the first of missing data, high cosine without keywords, keywords without similarity, no visible identity,
other weak signals, supported control) keeps up to --per-category transitions, in sha256 order of transition id.

Outputs in --out: transitions.csv (every transition, its reasons and whether it was selected), label-me.csv (the
selected transitions: selection columns, then the labels and keywords to judge, then an empty same_topic and
label_note), and summary.json (counts only: total, selected, excluded, missing data, categories and reasons per market).
same_topic takes y (one topic), n (separate topics) or u (cannot judge from labels and keywords). With --labels, the
y and n answers become identity cases (cases.json, the core/eval/identity_eval.py format) scored with today's matcher
on cosine and keywords alone; u answers are counted, never scored. label-me.csv, transitions.csv and cases.json hold
labels and keywords and stay local; only a reviewed summary.json may leave the approved environment."""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date
from itertools import pairwise
from pathlib import Path

from core.eval import identity_eval
from core.understand import cluster

CAVEATS = ["prior daily centroids stand in for historical EMA item centroids",
           "hashtag, sound and creator evidence is absent from these exports",
           "selection reasons explain sampling only; they are not labels or proven merges"]
CATEGORIES = ("missing_data", "high_cosine_no_keyword_support", "keywords_without_similarity", "no_visible_identity",
              "other_weak_signal", "supported_control")
ANSWERS = {"y": "same", "n": "separate", "u": "unjudgeable"}
FULL_LIMIT = 60
PER_CATEGORY = 20
TRANSITION_FIELDS = ["transition_id", "market", "item_id", "prev_date", "cur_date", "days", "match_kind", "cosine",
                     "jaccard", "shared_keywords", "selection_category", "selection_reasons", "selected"]
LABEL_FIELDS = ["transition_id", "market", "prev_date", "cur_date", "days", "match_kind", "cosine", "jaccard",
                "selection_category", "selection_reasons",
                "prev_label", "cur_label", "prev_keywords", "cur_keywords",
                "same_topic", "label_note"]


def _rows(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["rows"] if isinstance(data, dict) else data


def _key(row):
    return (str(row["market"]).upper(), str(row["cluster_date"])[:10], row["cluster_id"])


def _words(keywords):
    return [str(k) for k in keywords or [] if str(k).strip()]


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return None
    if not all(math.isfinite(float(x)) for x in list(a) + list(b)):
        return None
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return None if na == 0 or nb == 0 else round(sum(x * y for x, y in zip(a, b)) / (na * nb), 4)


def _jaccard(a, b):
    a, b = set(a), set(b)
    return round(len(a & b) / len(a | b), 4) if a and b else 0.0


def join(cluster_rows, centroid_rows):
    centroids = {_key(r): r for r in centroid_rows}
    joined = []
    for r in cluster_rows:
        c = centroids.get(_key(r))
        joined.append({"market": _key(r)[0], "cluster_date": _key(r)[1], "cluster_id": r["cluster_id"],
                       "item_id": r.get("item_id"), "label": r.get("label") or "",
                       "keywords": _words(r.get("keywords") or (c or {}).get("keywords")),
                       "match_kind": r.get("match_kind"), "centroid": (c or {}).get("centroid")})
    return joined


def selection_reasons(cos, jac, days, prev, cur):
    """Why a transition is worth labelling, from cosine, keywords, gap and labels only. Never a verdict."""
    match, keywords = cluster.MATCH_COSINE, cluster.KEYWORD_JACCARD
    reasons = []
    if cos is None:
        reasons.append("missing_centroid")
    if not prev["keywords"] or not cur["keywords"]:
        reasons.append("missing_keywords")
    elif cos is not None and cos >= match and jac < keywords:
        reasons.append("high_cosine_no_keyword_support")
    elif cos is not None and cos < match and jac >= keywords:
        reasons.append("keywords_without_similarity")
    elif cos is not None and cos < match and jac < keywords:
        reasons.append("no_visible_identity")
    if days > cluster.RECENT_DAYS:
        reasons.append("gap_over_recent")
    if prev["label"].strip().lower() != cur["label"].strip().lower():
        reasons.append("label_changed")
    return reasons or ["supported_control"]


def category(reasons):
    if {"missing_centroid", "missing_keywords"} & set(reasons):
        return "missing_data"
    for name in CATEGORIES[1:4]:
        if name in reasons:
            return name
    return "supported_control" if reasons == ["supported_control"] else "other_weak_signal"


def select(rows, full_limit=FULL_LIMIT, per_category=PER_CATEGORY):
    """Marks rows selected: all of them when the set is small, else up to per_category of each category in sha256
    order of transition id, so a rerun on the same exports picks the same sample."""
    if len(rows) <= full_limit:
        chosen = {t["transition_id"] for t in rows}
    else:
        chosen = set()
        for name in CATEGORIES:
            ids = sorted((t["transition_id"] for t in rows if t["selection_category"] == name),
                         key=lambda i: hashlib.sha256(i.encode()).hexdigest())
            chosen.update(ids[:per_category])
    for t in rows:
        t["selected"] = t["transition_id"] in chosen
    return rows


def transitions(joined):
    by_item = defaultdict(list)
    for r in joined:
        if r["item_id"]:
            by_item[(r["market"], r["item_id"])].append(r)
    out = []
    for (market, item_id), rows in sorted(by_item.items()):
        rows.sort(key=lambda r: (r["cluster_date"], r["cluster_id"]))
        for prev, cur in pairwise(rows):
            cos = _cosine(prev["centroid"], cur["centroid"])
            jac = _jaccard(prev["keywords"], cur["keywords"])
            days = (date.fromisoformat(cur["cluster_date"]) - date.fromisoformat(prev["cluster_date"])).days
            reasons = selection_reasons(cos, jac, days, prev, cur)
            out.append({"transition_id": f"{market}:{item_id[:12]}:{cur['cluster_date']}:{cur['cluster_id']}",
                        "market": market, "item_id": item_id, "prev_date": prev["cluster_date"],
                        "cur_date": cur["cluster_date"],
                        "days": days,
                        "match_kind": cur["match_kind"], "prev_label": prev["label"], "cur_label": cur["label"],
                        "cosine": cos, "jaccard": jac,
                        "shared_keywords": " ".join(sorted(set(prev["keywords"]) & set(cur["keywords"]))),
                        "selection_category": category(reasons), "selection_reasons": " ".join(reasons),
                        "_prev": prev, "_cur": cur})
    return out


def _counts(rows):
    reasons = Counter(r for t in rows for r in t["selection_reasons"].split())
    return {"transitions": len(rows), "selected": sum(t.get("selected", False) for t in rows),
            "excluded": sum(not t.get("selected", False) for t in rows),
            "items": len({t["item_id"] for t in rows}),
            "missing_centroid": reasons["missing_centroid"], "missing_keywords": reasons["missing_keywords"],
            "categories": {c: sum(t["selection_category"] == c for t in rows) for c in CATEGORIES},
            "selected_by_category": {c: sum(t["selection_category"] == c and t.get("selected", False) for t in rows)
                                     for c in CATEGORIES},
            "reasons": dict(sorted(reasons.items()))}


def summary(rows):
    """Counts only, so it can be reviewed and shared: no labels, keywords, ids or answers."""
    per = defaultdict(list)
    for t in rows:
        per[t["market"]].append(t)
    return {"total": _counts(rows), "markets": {m: _counts(v) for m, v in sorted(per.items())}}


def cases_from_labels(rows, labels):
    """Labelled transitions as identity cases: the current daily cluster against the prior one standing in for the
    item. y means one topic (expect the item), n means separate (expect a new identity); u and blanks are not cases."""
    by_id = {t["transition_id"]: t for t in rows}
    cases = []
    for lab in labels:
        answer = (lab.get("same_topic") or "").strip().lower()
        t = by_id.get(lab["transition_id"])
        if answer not in ("y", "n") or t is None or t["cosine"] is None:
            continue
        cases.append({"id": t["transition_id"], "category": f"native_{t['market'].lower()}",
                      "expect": t["item_id"] if answer == "y" else None, "run_date": t["cur_date"],
                      "cluster": {"centroid": t["_cur"]["centroid"], "keywords": t["_cur"]["keywords"]},
                      "items": [{"item_id": t["item_id"], "centroid": t["_prev"]["centroid"],
                                 "keywords": t["_prev"]["keywords"], "last_seen": t["prev_date"]}]})
    return cases


def answer_counts(labels):
    counts = Counter(ANSWERS.get((lab.get("same_topic") or "").strip().lower(), "unlabelled") for lab in labels)
    return {k: counts[k] for k in (*ANSWERS.values(), "unlabelled")}


def _write_csv(path, fields, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--clusters", required=True)
    p.add_argument("--centroids", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--labels")
    p.add_argument("--full-limit", type=int, default=FULL_LIMIT)
    p.add_argument("--per-category", type=int, default=PER_CATEGORY)
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = select(transitions(join(_rows(a.clusters), _rows(a.centroids))), a.full_limit, a.per_category)
    _write_csv(out / "transitions.csv", TRANSITION_FIELDS, rows)
    if not a.labels:  # never overwrite a sheet that is being filled in
        _write_csv(out / "label-me.csv", LABEL_FIELDS,
                   [{**t, "prev_keywords": " ".join(t["_prev"]["keywords"]),
                     "cur_keywords": " ".join(t["_cur"]["keywords"]), "same_topic": "", "label_note": ""}
                    for t in rows if t["selected"]])
    result = {"caveats": CAVEATS, "sampling": {"full_limit": a.full_limit, "per_category": a.per_category},
              **summary(rows)}
    if a.labels:
        with open(a.labels, encoding="utf-8", newline="") as f:
            labels = list(csv.DictReader(f))
        cases = cases_from_labels(rows, labels)
        (out / "cases.json").write_text(json.dumps({"cases": cases}), encoding="utf-8")
        result["answers"] = answer_counts(labels)
        result["labelled"] = identity_eval.summary(identity_eval.score(cases, date.today()))
    (out / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
