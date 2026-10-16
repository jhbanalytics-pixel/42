"""Topic identity evaluation: how often cluster.assign joins clusters that should stay separate, and how often it
splits clusters that should continue one identity. The two failure kinds are counted separately and never netted.

    py -3.13 -m core.eval.identity_eval [--cases FILE] [--run-date YYYY-MM-DD] [--json]

Each case is one daily cluster, the items it is judged against, and a human label: expect names the item the
cluster should continue, or null when it should start its own identity. Items take either a stored centroid or a
cosine to the cluster (synthetic cases), and either last_seen or days_since_seen. Retained native exports in the
same shape can be scored locally; they must not be committed or copied out of the approved environment.

Outcomes per case:
    correct          match or recurrence to the expected item, or new when expect is null
    incorrect_merge  match or recurrence to an item when expect is null, or to a different item than expected
    fragmented       new when an item was expected
    variant_split    variant child of the expected item instead of a continuation (linked, but not one identity)
    variant_link     variant child of some item when expect is null (a weak link, not a merge)

The bundled synthetic cases show the scorer and the rule's behaviour on chosen shapes. They are not a sample of
real topics, so their rates are not accuracy figures for 42."""
import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from core.understand import cluster

CASES = Path(__file__).resolve().parent / "fixtures" / "identity_cases.json"
OUTCOMES = ("correct", "incorrect_merge", "fragmented", "variant_split", "variant_link")
DIM = 16


def _at(cos, towards):
    v = [0.0] * DIM
    v[0], v[towards] = cos, math.sqrt(max(0.0, 1 - cos * cos))
    return v


def _cluster(case):
    c = case["cluster"]
    return {"cluster_id": case["id"], "centroid": c.get("centroid") or _at(1.0, 1),
            "keywords": list(c.get("keywords", [])), "hashtags": list(c.get("hashtags", [])),
            "sounds": list(c.get("sounds", [])), "creators": list(c.get("creators", []))}


def _item(spec, index, run_date):
    if spec.get("last_seen"):
        last_seen = date.fromisoformat(spec["last_seen"])
    elif spec.get("days_since_seen") is not None:
        last_seen = run_date - timedelta(days=spec["days_since_seen"])
    else:
        last_seen = None
    # Synthetic items sit on separate axes so each one's cosine to the cluster is exactly as written.
    centroid = spec.get("centroid") or _at(spec["cosine"], index + 2)
    return {"item_id": spec["item_id"], "centroid": centroid, "last_seen": last_seen,
            "keywords": list(spec.get("keywords", [])), "hashtags": list(spec.get("hashtags", [])),
            "sounds": list(spec.get("sounds", [])), "creators": list(spec.get("creators", []))}


def outcome(decision, expect):
    kind, got = decision["kind"], decision["item_id"]
    if kind in ("match", "recurrence"):
        return "correct" if got == expect else "incorrect_merge"
    if kind == "variant":
        return "variant_split" if got == expect else ("variant_link" if expect is None else "fragmented")
    return "correct" if expect is None else "fragmented"


def score(cases, run_date):
    """One row per case, judged alone so a case never competes with another case's items."""
    rows = []
    for case in cases:
        day = date.fromisoformat(case["run_date"]) if case.get("run_date") else run_date
        items = [_item(spec, i, day) for i, spec in enumerate(case["items"])]
        [decision] = cluster.assign([_cluster(case)], items, day)
        rows.append({"id": case["id"], "category": case["category"], "expect": case["expect"],
                     "kind": decision["kind"], "item_id": decision["item_id"], "votes": decision["votes"],
                     "outcome": outcome(decision, case["expect"]), "known_open": bool(case.get("known_open"))})
    return rows


def summary(rows):
    by_category = defaultdict(Counter)
    for r in rows:
        by_category[r["category"]][r["outcome"]] += 1
    totals = Counter(r["outcome"] for r in rows)
    expect_separate = sum(r["expect"] is None for r in rows)
    expect_same = len(rows) - expect_separate
    return {"cases": len(rows), "outcomes": {o: totals[o] for o in OUTCOMES},
            "incorrect_merges_of_separate": sum(r["outcome"] == "incorrect_merge" and r["expect"] is None
                                                for r in rows),
            "expect_separate": expect_separate,
            "fragmented_of_same": sum(r["outcome"] == "fragmented" for r in rows if r["expect"] is not None),
            "expect_same": expect_same,
            "by_category": {k: dict(v) for k, v in sorted(by_category.items())}}


def load(path=CASES):
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cases", default=str(CASES))
    p.add_argument("--run-date", default="2026-09-28")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    rows = score(load(a.cases), date.fromisoformat(a.run_date))
    out = {"rows": rows, "summary": summary(rows)}
    if a.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    for r in rows:
        flag = " (known open)" if r["known_open"] else ""
        print(f"{r['outcome']:16} {r['category']:28} {r['id']}: {r['kind']} {r['item_id']} {r['votes']}{flag}")
    s = out["summary"]
    print(f"incorrect merges {s['incorrect_merges_of_separate']} of {s['expect_separate']} expected separate; "
          f"fragmented {s['fragmented_of_same']} of {s['expect_same']} expected continuous")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
