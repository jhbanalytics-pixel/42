"""The offline cluster diagnostic on synthetic records shaped like the retained native exports (field names only)."""
import json

from ops.runners import diagnose_clusters as dc

HELD = {"topics": [
    {"market": "KE", "title": "ni, walai, hii", "hold_check": "Critic", "hold_reason": "collection artefact"},
    {"market": "ZA", "title": "topic:20261007-za-037", "hold_check": "Critic", "hold_reason": "collection artefact"},
    {"market": "NG", "title": "#psg", "hold_check": "Support check", "hold_reason": "support"},
    {"market": "ZA", "title": "amapiano, dance, challenge", "hold_check": "Critic", "hold_reason": "event"}]}


def row(cid, item, date, market, label, keywords, n=12, avg=.8, kind="new"):
    return {"market": market, "item_id": item, "cluster_date": date, "cluster_id": cid, "label": label,
            "keywords": keywords, "match_kind": kind, "members": [[f"p{cid}{i}", .9] for i in range(n)],
            "min_probability": .5, "avg_probability": avg, "examples": ["never printed"]}


CLUSTERS = [
    row("20261016-ke-004", "i-sw", "2026-10-16", "KE", "ni, walai, hii", ["ni", "walai", "hii", "sasa"], n=9, avg=.41),
    # one item chained from 7 Oct across unrelated clusters: falling keyword overlap and centroid cosine
    row("20261007-za-037", "i-old", "2026-10-07", "ZA", "pilates, studio, class", ["pilates", "studio", "class"]),
    row("20261012-za-011", "i-old", "2026-10-12", "ZA", "moving, apartment, boxes", ["moving", "apartment", "studio"],
        kind="match"),
    row("20261016-za-002", "i-old", "2026-10-16", "NG", "car, dealer, loan", ["car", "dealer", "loan"], kind="match"),
    row("20261016-za-009", "i-dance", "2026-10-16", "ZA", "amapiano, dance, challenge", ["amapiano", "dance",
                                                                                        "challenge"])]
CENTROIDS = [{"cluster_id": "20261007-za-037", "centroid": [1.0, 0.0, 0.0]},
             {"cluster_id": "20261012-za-011", "centroid": [0.8, 0.6, 0.0]},
             {"cluster_id": "20261016-za-002", "centroid": [0.1, 0.2, 0.97]}]


def test_function_word_labels_and_chained_identities_are_measured_and_coherent_topics_are_not_flagged():
    report = {h["title"]: h for h in dc.diagnose(HELD, CLUSTERS, CENTROIDS)}
    sw = report["ni, walai, hii"]["items"][0]["clusters"][0]
    assert (sw["function_words"], sw["terms"], sw["avg_probability"]) == (4, 4, .41)
    old = report["topic:20261007-za-037"]["items"][0]
    assert old["dates"] == 3 and old["markets"] == ["NG", "ZA"]
    assert old["lowest_keyword_jaccard_to_previous"] == 0.0 and old["lowest_cosine_to_first"] < .2
    assert report["#psg"]["found"] == "hashtag: no cluster rows"
    dance = report["amapiano, dance, challenge"]["items"][0]["clusters"][0]
    assert dance["function_words"] == 0 and dance["members"] == 12


def test_the_printed_and_written_report_carries_no_member_post_ids_examples_or_vectors(tmp_path, capsys):
    paths = {}
    for name, rows in (("held", HELD), ("clusters", {"rows": CLUSTERS}), ("centroids", {"rows": CENTROIDS})):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(rows))
    out = tmp_path / "out.json"
    assert dc.main([str(paths["held"]), str(paths["clusters"]), str(paths["centroids"]), "--out", str(out)]) == 0
    text = capsys.readouterr().out + out.read_text()
    assert "p20261016" not in text and "never printed" not in text and "0.97" not in text
