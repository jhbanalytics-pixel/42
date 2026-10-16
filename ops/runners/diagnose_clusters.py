"""Read-only, offline check of the retained cluster records behind the brief's held topics. Runs from anywhere with the
standard library only; nothing is read from or written to the cloud:
    py -3.13 diagnose_clusters.py HELD-TOPICS.json clusters-native.json [cluster-centroids-native.json] [--out FILE]

HELD-TOPICS.json is the held-topic summary ({"topics": [{"market", "title", "hold_check", ...}]}). The cluster files
are the retained native exports whose records sit under "rows" with the fields market, item_id, cluster_date,
cluster_id, label, keywords, match_kind, members, min_probability, avg_probability, examples (and centroid in the
centroids file).

For each held title it finds the cluster rows of that topic: rows whose label is the title, or, for a title that is a
topic key ("topic:<cluster_id>"), the row of that cluster id; then every row of the same item_id. Per item it prints:
- the cluster rows: market, date, cluster id, match_kind, member count, min and average membership probability;
- coherence of the label: how many of its keywords are function words (FUNCTION_WORDS, English, Swahili, Sheng,
  Nigerian Pidgin and German) and how many are under 3 letters;
- identity chaining: how many dates and markets the item's clusters span, the keyword overlap (Jaccard) between each
  cluster and the one before it, and, with the centroids file, the cosine between consecutive centroids and the
  lowest cosine of any of the item's clusters to its first one.
Hashtag titles have no cluster rows and are listed as such. Prints labels, keywords, ids and numbers only: never
members' post ids, examples or vectors. --out writes the same as JSON (keep it inside the approved local boundary).

What the numbers can show: a label made mostly of function words, a low average probability or a cluster of very
few members points at clustering (an incoherent topic); one item whose clusters span many dates or markets with low
keyword overlap or low centroid cosine points at identity (unrelated clusters chained onto one item). Neither alone
proves the hold was right or wrong."""
import argparse
import json
import math
import sys
from collections import defaultdict

FUNCTION_WORDS = frozenset("""
a an the and or but if of to in on at by for with from as is are was were be been it its this that these those
i me my we our you your he him his she her shes they them their there here what which who whom when where why how
not no yes do does did so too very just also than then all any some more most such only own same can will would
should could may might must let lets im youre dont cant wont isnt its ive ill id
na ni ya wa za la kwa kama hii hiyo huyo hao sasa tu pia lakini au ndio hapana sana mimi wewe yeye sisi nyinyi
wao walai wallahi ki ka ku kwenye bado tena kila hata
dey don wey na abi sha sef oya o ehn
ich du er sie es wir ihr und oder aber nicht ist sind war das der die den dem des ein eine mit von zu auf
""".split())


def load_rows(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: no rows list")
    return [r for r in rows if isinstance(r, dict)]


def words(keywords):
    out = []
    for k in keywords or []:
        out += str(k).casefold().split()
    return out


def coherence(label, keywords):
    terms = words(keywords) or words(str(label or "").split(","))
    if not terms:
        return {"terms": 0, "function_words": 0, "short_words": 0, "function_share": None}
    fw = sum(1 for t in terms if t.strip(".,!?'\"") in FUNCTION_WORDS)
    short = sum(1 for t in terms if len(t) < 3)
    return {"terms": len(terms), "function_words": fw, "short_words": short, "function_share": round(fw / len(terms), 3)}


def jaccard(a, b):
    a, b = set(map(str, a or [])), set(map(str, b or []))
    return round(len(a & b) / len(a | b), 3) if a | b else None


def cosine(u, v):
    if not u or not v or len(u) != len(v):
        return None
    dot = sum(x * y for x, y in zip(u, v))
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(y * y for y in v))
    return round(dot / (nu * nv), 4) if nu and nv else None


def members(row):
    m = row.get("members")
    return len(m) if isinstance(m, list) else m if isinstance(m, int) else None


def item_report(item_id, rows, centroids):
    rows = sorted(rows, key=lambda r: (str(r.get("cluster_date")), str(r.get("market")), str(r.get("cluster_id"))))
    out, prev, first = [], None, None
    for r in rows:
        cen = centroids.get(r.get("cluster_id"))
        entry = {"market": r.get("market"), "cluster_date": r.get("cluster_date"), "cluster_id": r.get("cluster_id"),
                 "match_kind": r.get("match_kind"), "label": r.get("label"), "keywords": r.get("keywords"),
                 "members": members(r), "min_probability": r.get("min_probability"),
                 "avg_probability": r.get("avg_probability"), **coherence(r.get("label"), r.get("keywords")),
                 "keyword_jaccard_to_previous": jaccard(prev.get("keywords"), r.get("keywords")) if prev else None,
                 "cosine_to_previous": cosine(centroids.get(prev.get("cluster_id")), cen) if prev else None,
                 "cosine_to_first": cosine(centroids.get(first.get("cluster_id")), cen) if first else None}
        out.append(entry)
        prev, first = r, first or r
    cos_first = [e["cosine_to_first"] for e in out if e["cosine_to_first"] is not None]
    jac = [e["keyword_jaccard_to_previous"] for e in out if e["keyword_jaccard_to_previous"] is not None]
    return {"item_id": item_id, "clusters": out,
            "dates": len({e["cluster_date"] for e in out}), "markets": sorted({str(e["market"]) for e in out}),
            "lowest_cosine_to_first": min(cos_first) if cos_first else None,
            "lowest_keyword_jaccard_to_previous": min(jac) if jac else None}


def diagnose(held, clusters, centroid_rows=()):
    by_item, by_label, by_cluster = defaultdict(list), defaultdict(list), {}
    for r in clusters:
        by_item[r.get("item_id")].append(r)
        by_label[str(r.get("label") or "").strip().casefold()].append(r)
        by_cluster[str(r.get("cluster_id"))] = r
    centroids = {r.get("cluster_id"): r.get("centroid") for r in centroid_rows if isinstance(r.get("centroid"), list)}
    report = []
    for t in held.get("topics") or []:
        title = str(t.get("title") or "").strip()
        if title.startswith("#"):
            report.append({**_held(t), "found": "hashtag: no cluster rows"})
            continue
        hits = (by_cluster.get(title[len("topic:"):]),) if title.casefold().startswith("topic:") else ()
        hits = [h for h in hits if h] or by_label.get(title.casefold(), [])
        items = sorted({h.get("item_id") for h in hits if h.get("item_id")})
        report.append({**_held(t), "found": f"{len(items)} item(s)" if items else "no cluster rows with this title",
                       "items": [item_report(i, by_item[i], centroids) for i in items]})
    return report


def _held(t):
    return {k: t.get(k) for k in ("market", "title", "hold_check", "hold_reason")}


def show(report):
    for h in report:
        print(f"== {h['market']} {h['title']} | {h['hold_check']} | {h['found']}")
        for it in h.get("items") or []:
            print(f"  item {str(it['item_id'])[:12]}: {len(it['clusters'])} clusters on {it['dates']} dates in "
                  f"{','.join(it['markets'])}; lowest cosine to first {it['lowest_cosine_to_first']}; lowest keyword "
                  f"jaccard to previous {it['lowest_keyword_jaccard_to_previous']}")
            for c in it["clusters"]:
                print(f"    {c['cluster_date']} {c['market']} {c['cluster_id']} {c['match_kind']} members "
                      f"{c['members']} prob min {c['min_probability']} avg {c['avg_probability']} function words "
                      f"{c['function_words']}/{c['terms']} short {c['short_words']} | cos prev "
                      f"{c['cosine_to_previous']} jac prev {c['keyword_jaccard_to_previous']} | {c['label']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("held")
    ap.add_argument("clusters")
    ap.add_argument("centroids", nargs="?")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    with open(args.held, encoding="utf-8") as fh:
        held = json.load(fh)
    report = diagnose(held, load_rows(args.clusters), load_rows(args.centroids) if args.centroids else ())
    show(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"held_source": args.held, "report": report}, fh, indent=1, default=str)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
