"""core/eval/identity_replay.py on synthetic exports shaped like the retained native files (field names only)."""
import csv
import json

from core.eval import identity_replay as ir


def export(tmp_path):
    def c(day, cid, item, label, keywords, kind="match"):
        return {"market": "ke", "item_id": item, "cluster_date": day, "cluster_id": cid, "label": label,
                "keywords": keywords, "match_kind": kind, "members": [["p", 1.0]], "min_probability": 0.5,
                "avg_probability": 0.7, "examples": ["never read"]}

    def v(day, cid, item, vec):
        return {"market": "ke", "item_id": item, "cluster_date": day, "cluster_id": cid, "centroid": vec,
                "keywords": [], "label": ""}

    clusters = [c("2026-10-01", "a1", "chain", "forestry, trees", ["forestry", "trees"], "new"),
                c("2026-10-02", "a2", "chain", "phone, airtime", ["phone", "airtime", ""]),
                c("2026-10-03", "a3", "chain", "phone, mobile", ["phone", "mobile"]),
                c("2026-10-01", "b1", "steady", "amapiano", ["amapiano", "dance"], "new"),
                c("2026-10-02", "b2", "steady", "amapiano", ["amapiano", "dance"])]
    centroids = [v("2026-10-01", "a1", "chain", [1.0, 0.0, 0.0]), v("2026-10-02", "a2", "chain", [0.6, 0.8, 0.0]),
                 v("2026-10-03", "a3", "chain", [0.6, 0.79, 0.1]), v("2026-10-01", "b1", "steady", [0.0, 0.0, 1.0]),
                 v("2026-10-02", "b2", "steady", [0.0, 0.1, 1.0])]
    (tmp_path / "clusters.json").write_text(json.dumps({"rows": clusters}))
    (tmp_path / "centroids.json").write_text(json.dumps({"rows": centroids}))
    return tmp_path / "clusters.json", tmp_path / "centroids.json"


def test_transitions_flag_continuations_without_visible_identity_evidence(tmp_path):
    cl, ce = export(tmp_path)
    rows = ir.transitions(ir.join(ir._rows(cl), ir._rows(ce)))
    got = {(t["item_id"], t["cur_date"]): (t["cosine"], t["jaccard"], t["flag"]) for t in rows}
    assert got[("chain", "2026-10-02")] == (0.6, 0.0, ir.FLAG)  # forestry to phones: nothing visible
    assert got[("chain", "2026-10-03")][2] == ""  # phone to phone: cosine and keyword overlap
    assert got[("steady", "2026-10-02")][2] == ""
    assert ir.summary(rows) == {"KE": {"transitions": 3, "flagged": 1, "items": 2, "items_with_a_flag": 1,
                                       "transitions_without_centroid": 0}}


def test_outputs_never_carry_members_or_examples_and_labels_become_cases(tmp_path):
    cl, ce = export(tmp_path)
    out = tmp_path / "out"
    assert ir.main(["--clusters", str(cl), "--centroids", str(ce), "--out", str(out)]) == 0
    for name in ("transitions.csv", "label-me.csv", "summary.json"):
        text = (out / name).read_text(encoding="utf-8")
        assert "never read" not in text and "min_probability" not in text
    with open(out / "label-me.csv", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    row["same_topic"] = "n"
    labelled = tmp_path / "labelled.csv"
    with open(labelled, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ir.LABEL_FIELDS)
        w.writeheader()
        w.writerow(row)
    assert ir.main(["--clusters", str(cl), "--centroids", str(ce), "--out", str(out), "--labels", str(labelled)]) == 0
    result = json.loads((out / "summary.json").read_text())
    # Under today's rule, cosine 0.6 with no keyword overlap and no retained facets is a new identity.
    assert result["labelled"]["incorrect_merges_of_separate"] == 0 and result["labelled"]["expect_separate"] == 1
    [case] = json.loads((out / "cases.json").read_text())["cases"]
    assert case["expect"] is None and case["cluster"]["keywords"] == ["phone", "airtime"]
