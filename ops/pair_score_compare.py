"""Offline comparison of the vote rule in core/understand/cluster.py with the shadow pair score in
core/understand/pair_score.py, on labelled pairs.

usage: python -m ops.pair_score_compare OUT_DIR [PAIRS_JSON]

Each pair is a cluster profile, an item profile and a label ("true" when both come from one topic, "false" when they
do not). The vote rule's side is the real cluster.assign run on the pair alone; the shadow's side is
pair_score.score_pair with weights taken over every cluster and item in the file, which stands in for the run's own
items. PAIRS_JSON is read in the shape build_fixture returns, so retained cluster and item rows can be replayed once
they are exported with centroids; without it the labelled fixture below is used. The run needs no network, no
BigQuery and no embedding call: every vector is in the pairs.

The fixture's labels are true by construction. A pair is "true" because both of its sides were generated from one
topic and "false" because they were generated from two, so the report measures how each rule treats named failure
modes, not how often either is right on production data. It writes pair_score_comparison.json and
pair_score_comparison.md to OUT_DIR.
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from core.understand import cluster, pair_score

REPO = Path(__file__).resolve().parents[1]
TOPICS_FILE = REPO / "core" / "understand" / "tests" / "fixtures" / "cluster_topics.json"
RUN_DATE = date(2026, 9, 28)
DIM = 16
GENERIC = ("fyp", "foryou", "viral", "trending")
# Topics 0 to 2 are read from the understand test fixtures. The rest are authored for this comparison; drift_words
# are what a topic's posts say when the vocabulary has moved on, with none of the stored keywords in them.
AUTHORED = {
    "fuelprice": {"keywords": ["fuel", "petrol", "price", "hike", "litre", "pump"], "hashtags": ["fuelprice", "petrolhike"],
                  "sound": "snd_fuel_4", "creators": ["cr_d1", "cr_d2", "cr_d3"]},
    "afcon": {"keywords": ["afcon", "qualifier", "eagles", "stadium", "squad", "goal"], "hashtags": ["afcon", "superseagles"],
              "sound": "snd_afcon_5", "creators": ["cr_e1", "cr_e2", "cr_e3"]},
    "matric": {"keywords": ["matric", "results", "pass", "distinction", "exam", "class"], "hashtags": ["matricresults", "matric"],
               "sound": "snd_matric_6", "creators": ["cr_f1", "cr_f2", "cr_f3"]},
    "kota": {"keywords": ["kota", "kasi", "bunny", "chow", "quarter", "loaf"], "hashtags": ["kota", "kasifood"],
             "sound": "snd_kota_7", "creators": ["cr_g1", "cr_g2", "cr_g3"]},
}
DRIFT_WORDS = {
    "amapiano": ["sunday", "residency", "deejay", "bpm", "rollout"], "loadshedding": ["outage", "dark", "utility", "tariff", "relief"],
    "jollof": ["cookoff", "contest", "ghanaian", "nigerian", "verdict"], "fuelprice": ["commuter", "taxi", "fare", "budget", "wallet"],
    "afcon": ["fixture", "kickoff", "supporters", "coach", "bench"], "matric": ["learners", "school", "principal", "bachelor", "marks"],
    "kota": ["township", "spaza", "lunch", "vendor", "takeaway"],
}


def topics():
    raw = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))["topics"]
    out = {name: {"keywords": t["words"], "hashtags": t["hashtags"], "sound": t["sound"], "creators": t["creators"]}
           for name, t in raw.items()}
    return {**out, **AUTHORED}


def _vec(axis, other, cos):
    v = [0.0] * DIM
    v[axis], v[other] = cos, math.sqrt(1 - cos * cos)
    return v


def _profile(topic, mode, n, topic_defs):
    """A profile for one side of a pair. n varies which keywords, generic tags and filler it takes, so documents of
    one topic are not copies of each other."""
    t = topic_defs[topic]
    kw = t["keywords"]
    generic = [GENERIC[0]] + ([GENERIC[1 + n % 3]] if n % 2 else [])  # fyp on every document that carries tags
    common = ["south"] if n % 3 else []
    own = [f"filler{n}"]
    if mode == "core":
        return {"keywords": [kw[(n + k) % len(kw)] for k in range(4)] + common, "hashtags": [t["hashtags"][0]] + generic,
                "sounds": [t["sound"]], "creators": [t["creators"][n % 3]]}
    if mode == "partial":
        return {"keywords": [kw[n % len(kw)], kw[(n + 1) % len(kw)]] + common, "hashtags": [t["hashtags"][0]] + generic,
                "sounds": [], "creators": [t["creators"][n % 3]]}
    if mode == "tag":
        return {"keywords": common + own, "hashtags": [t["hashtags"][-1]] + generic, "sounds": [],
                "creators": [t["creators"][n % 3]]}
    if mode == "drift":
        return {"keywords": DRIFT_WORDS[topic][:3], "hashtags": [f"drift{n}"], "sounds": [], "creators": [f"new_{n}"]}
    if mode == "generic":
        return {"keywords": own, "hashtags": [GENERIC[0]], "sounds": [], "creators": [f"anon_{n}"]}
    if mode == "common_word":
        return {"keywords": ["south", own[0]], "hashtags": [f"tag{n}"], "sounds": [], "creators": [f"anon_{n}"]}
    raise ValueError(mode)


def build_fixture():
    """The labelled pairs. Each case is (label, cluster topic, item topic, cosine, days since the item was seen,
    cluster mode, item mode, why)."""
    defs = topics()
    names = list(defs)
    cases = []

    def add(label, a, b, cos, days, ma, mb, why, shared_creator=False):
        cases.append((label, a, b, cos, days, ma, mb, why, shared_creator))

    same = ["amapiano", "loadshedding", "jollof", "fuelprice", "afcon", "matric", "kota"]
    for i, (cos, days) in enumerate([(0.97, 1), (0.93, 2), (0.90, 4), (0.86, 6), (0.83, 3), (0.80, 5)]):
        add("true", same[i % 7], same[i % 7], cos, days, "core", "core", "same topic, high cosine, shared words and tags, seen within a week")
    for i, (cos, days, ma, mb) in enumerate([(0.92, 10, "partial", "core"), (0.88, 12, "tag", "tag"), (0.85, 20, "partial", "partial")]):
        add("true", same[(i + 2) % 7], same[(i + 2) % 7], cos, days, ma, mb, "same topic, not seen within a week, shares a tag or words")
    for i, (cos, days) in enumerate([(0.75, 2), (0.72, 3), (0.68, 2)]):
        add("true", same[(i + 4) % 7], same[(i + 4) % 7], cos, days, "core", "core", "same topic at a lower cosine, shared specific words, tag and sound")
    for i, (cos, days) in enumerate([(0.95, 35), (0.93, 60), (0.90, 45), (0.88, 90)]):
        add("true", same[(i + 1) % 7], same[(i + 1) % 7], cos, days, "drift", "core", "recurrence after a gap, the cluster uses new vocabulary, high cosine and nothing else shared")
    for i, days in enumerate([8, 9]):
        add("true", same[(i + 3) % 7], same[(i + 3) % 7], 0.90, days, "tag", "tag", "same topic, cosine and a specific tag, seen just over a week ago")
    for i, days in enumerate([2, 3]):
        add("true", same[(i + 5) % 7], same[(i + 5) % 7], 0.86, days, "drift", "core", "same topic, recent, high cosine, the cluster uses new vocabulary")
    for i, days in enumerate([3, 4]):
        add("true", same[i], same[i], 0.78, days, "generic", "generic", "same topic but both sides carry only a generic tag at a middling cosine")
    # false pairs: two different topics
    other = lambda i: (names.index(same[i % 7]) + 1 + i // 7) % len(names)  # noqa: E731
    for i, cos in enumerate([0.30, 0.40, 0.50, 0.60, 0.68]):
        add("false", same[i], names[other(i)], cos, 2, "generic", "generic", "different topics sharing one generic tag, item seen within a week")
    for i, cos in enumerate([0.50, 0.60, 0.75]):
        add("false", same[(i + 2) % 7], names[other(i + 2)], cos, 1, "drift", "drift", "different topics sharing only a creator, item seen within a week", True)
    for i, cos in enumerate([0.84, 0.86, 0.83]):
        add("false", same[(i + 3) % 7], names[other(i + 3)], cos, 20, "generic", "generic", "different topics close in embedding space, one generic tag, not recent")
    for i, cos in enumerate([0.72, 0.74, 0.76]):
        add("false", same[(i + 1) % 7], names[other(i + 1)], cos, 3, "generic", "generic", "different topics at a middling cosine, one generic tag, item seen within a week")
    for i, cos in enumerate([0.55, 0.45]):
        add("false", same[(i + 4) % 7], names[other(i + 4)], cos, 2, "common_word", "common_word", "different topics sharing one very common word, item seen within a week")
    for i in range(2):
        add("false", same[(i + 5) % 7], names[other(i + 5)], 0.20, 1, "drift", "drift", "different topics, nothing shared, item seen within a week")
    for i, cos in enumerate([0.83, 0.85, 0.87, 0.90]):
        add("false", same[i + 1], names[other(i + 1)], cos, 40, "drift", "drift", "different topics close in embedding space, nothing shared, dormant item")
    pairs = []
    for n, (label, a, b, cos, days, ma, mb, why, shared_creator) in enumerate(cases):
        ia = names.index(a)
        c, i = _profile(a, ma, 2 * n, defs), _profile(b, mb, 2 * n + 1, defs)
        if shared_creator:
            i["creators"] = list(c["creators"])
        pairs.append({
            "id": f"P{n + 1:02d}", "label": label, "why": why,
            "cluster": {"cluster_id": f"c{n + 1:02d}", "centroid": _vec(ia, (ia + 8) % DIM, 1.0), **c},
            "item": {"item_id": f"i{n + 1:02d}", "centroid": _vec(ia, (ia + 1) % DIM, cos),
                     "last_seen": (RUN_DATE - timedelta(days=days)).isoformat(), "kind": "topic", **i}})
    return {"source": "fixture: topics read from core/understand/tests/fixtures/cluster_topics.json plus four authored here",
            "run_date": RUN_DATE.isoformat(), "pairs": pairs}


def _item(row):
    last = row["last_seen"]
    return {**row, "last_seen": date.fromisoformat(last) if isinstance(last, str) else last, "status": "active",
            "valid_from": None, "recurrences": 0, "aliases": [], "parent_item_id": None, "canonical_key": row["item_id"],
            "label": row["item_id"], "first_seen": None, "first_seen_market": None, "first_seen_platform": None,
            "lifecycle": None, "rejected_until": None}


def evaluate(data):
    run_date = date.fromisoformat(data["run_date"])
    clusters = [p["cluster"] for p in data["pairs"]]
    items = [_item(p["item"]) for p in data["pairs"]]
    idf = pair_score.build_idf(clusters, items)
    rows = []
    for p, item in zip(data["pairs"], items):
        c = p["cluster"]
        [today] = cluster.assign([c], [item], run_date)
        cos = float(cluster._cosines([c["centroid"]], [item["centroid"]])[0, 0])
        shadow = pair_score.score_pair(c, item, cos, idf)
        rows.append({"id": p["id"], "label": p.get("label"), "why": p.get("why"), "cosine": cos,
                     "days_since_seen": (run_date - item["last_seen"]).days, "today_kind": today["kind"],
                     "today_accept": today["kind"] in ("match", "recurrence"), "today_votes": today["votes"],
                     "shadow_accept": shadow["accept"], "shadow_score": shadow["score"],
                     "shadow_reason": shadow["reason"]})
    return rows


def _confusion(rows, key):
    tp = sum(r["label"] == "true" and r[key] for r in rows)
    fp = sum(r["label"] == "false" and r[key] for r in rows)
    fn = sum(r["label"] == "true" and not r[key] for r in rows)
    tn = sum(r["label"] == "false" and not r[key] for r in rows)
    ratio = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn)}


def summarise(rows):
    labelled = [r for r in rows if r["label"] in ("true", "false")]
    only_today = [r for r in rows if r["today_accept"] and not r["shadow_accept"]]
    only_shadow = [r for r in rows if r["shadow_accept"] and not r["today_accept"]]
    sets = lambda group: dict(sorted(Counter("+".join(r["today_votes"]) or "none" for r in group).items()))  # noqa: E731
    return {
        "pairs": len(rows), "true": sum(r["label"] == "true" for r in rows), "false": sum(r["label"] == "false" for r in rows),
        "agree": sum(r["today_accept"] == r["shadow_accept"] for r in rows),
        "today": _confusion(labelled, "today_accept"), "shadow": _confusion(labelled, "shadow_accept"),
        "today_accepts_shadow_rejects": only_today, "today_accepts_shadow_rejects_vote_sets": sets(only_today),
        "shadow_accepts_today_rejects": only_shadow, "shadow_accepts_today_rejects_vote_sets": sets(only_shadow)}


def _table(rows):
    if not rows:
        return ["None.", ""]
    out = ["| pair | label | cosine | days | today votes | shadow score | shadow reason |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['id']} | {r['label']} | {r['cosine']:.2f} | {r['days_since_seen']} | "
                   f"{'+'.join(r['today_votes']) or 'none'} | {r['shadow_score']:.3f} | {r['shadow_reason']} |")
    return out + [""]


def report(data, summary):
    t, s = summary["today"], summary["shadow"]
    lines = [
        "# Pair score shadow against the vote rule", "",
        f"Source: {data.get('source', 'supplied pairs')}. Run date {data['run_date']}.", "",
        "The labels are true by construction (see the module docstring), so this reads as a test of named failure "
        "modes and not as accuracy on production data.", "",
        f"Pairs: {summary['pairs']} ({summary['true']} true, {summary['false']} false). The two rules agree on "
        f"{summary['agree']} of {summary['pairs']}.", "",
        "| rule | accepted true | accepted false | refused true | refused false | precision | recall |",
        "|---|---|---|---|---|---|---|",
        f"| vote rule | {t['tp']} | {t['fp']} | {t['fn']} | {t['tn']} | {t['precision']} | {t['recall']} |",
        f"| shadow | {s['tp']} | {s['fp']} | {s['fn']} | {s['tn']} | {s['precision']} | {s['recall']} |", "",
        f"## The vote rule accepts and the shadow refuses ({len(summary['today_accepts_shadow_rejects'])})", "",
        f"Vote sets: {json.dumps(summary['today_accepts_shadow_rejects_vote_sets'])}", ""]
    lines += _table(summary["today_accepts_shadow_rejects"])
    lines += [f"## The shadow accepts and the vote rule refuses ({len(summary['shadow_accepts_today_rejects'])})", "",
              f"Vote sets: {json.dumps(summary['shadow_accepts_today_rejects_vote_sets'])}", ""]
    lines += _table(summary["shadow_accepts_today_rejects"])
    return "\n".join(lines) + "\n"


def main(argv):
    out = Path(argv[1])
    data = json.loads(Path(argv[2]).read_text(encoding="utf-8")) if len(argv) > 2 else build_fixture()
    rows = evaluate(data)
    summary = summarise(rows)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pair_score_comparison.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n", encoding="utf-8")
    (out / "pair_score_comparison.md").write_text(report(data, summary), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("pairs", "true", "false", "agree", "today", "shadow")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
