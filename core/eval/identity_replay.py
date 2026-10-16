"""Read-only identity diagnostic over retained daily cluster exports, run where the exports are allowed to live (Albert's
PC or the WPP environment), never in a shared or cloud context.

    py -3.13 -m core.eval.identity_replay --clusters clusters-native.json --centroids cluster-centroids-native.json \
        --out identity-replay [--labels labelled.csv]

Inputs are the two retained exports, each {"rows": [...]}. clusters-native.json rows carry market, item_id,
cluster_date, cluster_id, label, keywords, match_kind, members, min_probability, avg_probability and examples;
cluster-centroids-native.json rows carry market, item_id, cluster_date, cluster_id, centroid, keywords and label. Rows
join on market, cluster_date and cluster_id. members and examples are never read or written.

For every item, consecutive daily clusters assigned to it make one transition. Each transition records the cosine
between the two daily centroids and the keyword Jaccard (blanks ignored), and is flagged no_visible_identity when
neither reaches the match rule (cosine 0.82, Jaccard 0.10). Such a continuation rested on a shared hashtag, sound,
creator or the recent sighting, none of which these exports retain, so a flag marks a transition to review, not a
proven wrong merge. Prior daily centroids stand in for the item's EMA centroid, so this is a proxy replay.

Outputs in --out: transitions.csv (one row per transition, labels and keywords only), summary.json (counts per
market), and label-me.csv (flagged transitions with an empty same_topic column). With --labels, a label-me.csv filled
with y or n becomes identity cases (cases.json, the core/eval/identity_eval.py format) and is scored with today's
matcher; facets the exports lack are left empty, so that score sees cosine and keywords only."""
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date
from itertools import pairwise
from pathlib import Path

from core.eval import identity_eval
from core.understand import cluster

FLAG = "no_visible_identity"
TRANSITION_FIELDS = ["transition_id", "market", "item_id", "prev_date", "cur_date", "days", "match_kind",
                     "prev_label", "cur_label", "cosine", "jaccard", "shared_keywords", "flag"]
LABEL_FIELDS = ["transition_id", "market", "prev_label", "cur_label", "prev_keywords", "cur_keywords", "cosine",
                "jaccard", "same_topic"]


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
            visible = (cos is not None and cos >= cluster.MATCH_COSINE) or jac >= cluster.KEYWORD_JACCARD
            out.append({"transition_id": f"{market}:{item_id[:12]}:{cur['cluster_date']}:{cur['cluster_id']}",
                        "market": market, "item_id": item_id, "prev_date": prev["cluster_date"],
                        "cur_date": cur["cluster_date"],
                        "days": (date.fromisoformat(cur["cluster_date"]) - date.fromisoformat(prev["cluster_date"])).days,
                        "match_kind": cur["match_kind"], "prev_label": prev["label"], "cur_label": cur["label"],
                        "cosine": cos, "jaccard": jac,
                        "shared_keywords": " ".join(sorted(set(prev["keywords"]) & set(cur["keywords"]))),
                        "flag": "" if visible else FLAG,
                        "_prev": prev, "_cur": cur})
    return out


def summary(rows):
    per = defaultdict(lambda: {"transitions": 0, "flagged": 0, "items": set(), "flagged_items": set(),
                               "no_centroid": 0})
    for t in rows:
        m = per[t["market"]]
        m["transitions"] += 1
        m["items"].add(t["item_id"])
        m["no_centroid"] += t["cosine"] is None
        if t["flag"]:
            m["flagged"] += 1
            m["flagged_items"].add(t["item_id"])
    return {k: {"transitions": v["transitions"], "flagged": v["flagged"], "items": len(v["items"]),
                "items_with_a_flag": len(v["flagged_items"]), "transitions_without_centroid": v["no_centroid"]}
            for k, v in sorted(per.items())}


def cases_from_labels(rows, labels):
    """Labelled transitions as identity cases: the current daily cluster against the prior one standing in for the
    item. y means one topic (expect the item), n means separate (expect a new identity)."""
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
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = transitions(join(_rows(a.clusters), _rows(a.centroids)))
    _write_csv(out / "transitions.csv", TRANSITION_FIELDS, rows)
    _write_csv(out / "label-me.csv", LABEL_FIELDS,
               [{**t, "prev_keywords": " ".join(t["_prev"]["keywords"]), "cur_keywords": " ".join(t["_cur"]["keywords"]),
                 "same_topic": ""} for t in rows if t["flag"]])
    result = {"proxy": "prior daily centroids stand in for EMA item centroids; hashtags, sounds and creators absent",
              "markets": summary(rows)}
    if a.labels:
        with open(a.labels, encoding="utf-8", newline="") as f:
            cases = cases_from_labels(rows, list(csv.DictReader(f)))
        (out / "cases.json").write_text(json.dumps({"cases": cases}), encoding="utf-8")
        scored = identity_eval.score(cases, date.today())
        result["labelled"] = identity_eval.summary(scored)
    (out / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
