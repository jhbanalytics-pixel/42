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


def test_transitions_carry_reasons_from_cosine_keywords_and_labels(tmp_path):
    cl, ce = export(tmp_path)
    rows = ir.select(ir.transitions(ir.join(ir._rows(cl), ir._rows(ce))))
    got = {(t["item_id"], t["cur_date"]): (t["cosine"], t["jaccard"], t["selection_reasons"]) for t in rows}
    assert got[("chain", "2026-10-02")] == (0.6, 0.0, "no_visible_identity label_changed")  # forestry to phones
    assert got[("chain", "2026-10-03")][2] == "label_changed"  # phone to phone, relabelled
    assert got[("steady", "2026-10-02")][2] == "supported_control"
    total = ir.summary(rows)["total"]
    assert (total["transitions"], total["selected"], total["excluded"], total["missing_centroid"]) == (3, 3, 0, 0)
    assert total["categories"]["no_visible_identity"] == 1 and total["categories"]["supported_control"] == 1


def test_outputs_never_carry_members_or_examples_and_labels_become_cases(tmp_path):
    cl, ce = export(tmp_path)
    out = tmp_path / "out"
    assert ir.main(["--clusters", str(cl), "--centroids", str(ce), "--out", str(out)]) == 0
    for name in ("transitions.csv", "label-me.csv", "summary.json"):
        text = (out / name).read_text(encoding="utf-8")
        assert "never read" not in text and "min_probability" not in text
    sheet = out / "label-me.csv"
    with open(sheet, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3 and all(r["same_topic"] == "" and r["label_note"] == "" for r in rows)
    for r in rows:
        r["same_topic"] = {"2026-10-02": "n", "2026-10-03": "u"}[r["cur_date"]] if r["cur_date"] else ""
    by_date = {(r["transition_id"].split(":")[1], r["cur_date"]): r for r in rows}
    by_date[("steady", "2026-10-02")]["same_topic"] = ""
    with open(sheet, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ir.LABEL_FIELDS)
        w.writeheader()
        w.writerows(rows)
    assert ir.main(["--clusters", str(cl), "--centroids", str(ce), "--out", str(out), "--labels", str(sheet)]) == 0
    with open(sheet, encoding="utf-8") as f:
        assert [r["same_topic"] for r in csv.DictReader(f)] == [r["same_topic"] for r in rows]  # sheet kept
    result = json.loads((out / "summary.json").read_text())
    assert result["answers"] == {"same": 0, "separate": 1, "unjudgeable": 1, "unlabelled": 1}
    # Under today's rule, cosine 0.6 with no keyword overlap and no retained facets is a new identity.
    assert result["labelled"]["incorrect_merges_of_separate"] == 0 and result["labelled"]["expect_separate"] == 1
    [case] = json.loads((out / "cases.json").read_text())["cases"]  # u is counted, never scored
    assert case["expect"] is None and case["cluster"]["keywords"] == ["phone", "airtime"]
    text = (out / "summary.json").read_text()
    assert not any(word in text for word in ("forestry", "airtime", "amapiano", "chain", "steady"))
    assert result["caveats"] == ir.CAVEATS


def high_cosine_export(tmp_path):
    """One item that continues at high cosine with no shared keyword, one supported continuation (a positive control)
    and one item with no visible identity at all."""
    def c(day, cid, item, label, keywords):
        return {"market": "za", "item_id": item, "cluster_date": day, "cluster_id": cid, "label": label,
                "keywords": keywords, "match_kind": "match", "members": [], "examples": []}

    def v(day, cid, item, vec):
        return {"market": "za", "item_id": item, "cluster_date": day, "cluster_id": cid, "centroid": vec}

    clusters = [c("2026-10-01", "h1", "drift", "school shoes", ["school", "shoes"]),
                c("2026-10-02", "h2", "drift", "school fees", ["fees", "tuition"]),
                c("2026-10-01", "s1", "steady", "amapiano", ["amapiano", "dance"]),
                c("2026-10-02", "s2", "steady", "amapiano", ["amapiano", "dance"]),
                c("2026-10-01", "n1", "chain", "forestry", ["forestry", "trees"]),
                c("2026-10-02", "n2", "chain", "airtime", ["phone", "airtime"])]
    centroids = [v("2026-10-01", "h1", "drift", [1.0, 0.0, 0.0]), v("2026-10-02", "h2", "drift", [0.95, 0.31, 0.0]),
                 v("2026-10-01", "s1", "steady", [0.0, 0.0, 1.0]), v("2026-10-02", "s2", "steady", [0.0, 0.1, 1.0]),
                 v("2026-10-01", "n1", "chain", [1.0, 0.0, 0.0]), v("2026-10-02", "n2", "chain", [0.6, 0.8, 0.0])]
    (tmp_path / "clusters.json").write_text(json.dumps({"rows": clusters}))
    (tmp_path / "centroids.json").write_text(json.dumps({"rows": centroids}))
    return tmp_path / "clusters.json", tmp_path / "centroids.json"


def label_rows(out):
    with open(out / "label-me.csv", encoding="utf-8") as f:
        return {r["transition_id"].split(":")[1]: r for r in csv.DictReader(f)}


def test_high_cosine_without_keyword_support_is_sampled_beside_a_positive_control(tmp_path):
    # The first selector called cosine >= 0.82 OR Jaccard >= 0.10 visible identity and sampled only the rest, so a
    # high-cosine continuation with no shared keyword never reached the labelling sheet.
    cl, ce = high_cosine_export(tmp_path)
    out = tmp_path / "out"
    assert ir.main(["--clusters", str(cl), "--centroids", str(ce), "--out", str(out)]) == 0
    rows = label_rows(out)
    assert set(rows) == {"drift", "steady", "chain"}  # a set this small is labelled in full
    assert "high_cosine_no_keyword_support" in rows["drift"]["selection_reasons"].split()
    assert rows["steady"]["selection_reasons"] == "supported_control"
    assert "no_visible_identity" in rows["chain"]["selection_reasons"].split()
    assert all(r["same_topic"] == "" for r in rows.values())


def test_a_large_export_is_sampled_per_category_the_same_way_twice(tmp_path):
    def t(n, cat):
        return {"transition_id": f"ZA:item{n}:2026-10-02:c{n}", "market": "ZA", "item_id": f"item{n}",
                "selection_category": cat, "selection_reasons": cat}
    rows = ([t(n, "high_cosine_no_keyword_support") for n in range(30)]
            + [t(n, "supported_control") for n in range(30, 90)] + [t(n, "missing_data") for n in range(90, 95)])
    first = {r["transition_id"] for r in ir.select([dict(r) for r in rows], full_limit=60, per_category=10)
             if r["selected"]}
    again = {r["transition_id"] for r in ir.select([dict(r) for r in reversed(rows)], full_limit=60, per_category=10)
             if r["selected"]}
    assert first == again and len(first) == 25
    cats = [r["selection_category"] for r in rows if r["transition_id"] in first]
    assert (cats.count("high_cosine_no_keyword_support"), cats.count("supported_control"),
            cats.count("missing_data")) == (10, 10, 5)


def test_missing_centroid_or_keywords_is_missing_data_not_absent_support():
    prev = {"keywords": [], "label": "a"}
    cur = {"keywords": ["x"], "label": "a"}
    assert ir.selection_reasons(0.95, 0.0, 1, prev, cur) == ["missing_keywords"]
    assert ir.category(["missing_keywords"]) == "missing_data"
    both = {"keywords": ["x"], "label": "a"}
    assert ir.selection_reasons(None, 1.0, 1, both, both) == ["missing_centroid"]
    assert ir.selection_reasons(0.95, 1.0, 9, both, both) == ["gap_over_recent"]
    assert ir.category(["gap_over_recent"]) == "other_weak_signal"
