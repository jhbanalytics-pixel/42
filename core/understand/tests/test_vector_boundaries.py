import json
from datetime import timedelta

import pytest

from core.understand import cluster, embed
from core.understand.tests.test_cluster import CORE, DAY, core_db, duck_execute, fake_run, map_row
from core.understand.tests.test_embed import fixture_warehouse, run_embed_sql


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), None])
def test_nonfinite_embedding_is_not_complete_or_selected_for_clustering(bad):
    con = core_db()
    for post_id in ("malformed", "duplicate"):
        con.execute(f"INSERT INTO {CORE}.posts VALUES (?, 'tiktok', 'creator', 'text', [], NULL, ?)", [post_id, DAY])
        con.execute(f"INSERT INTO {CORE}.post_observations VALUES (?, ?, 'ZA')", [post_id, DAY])
        con.execute(f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?)", [post_id, [-1.0, bad] + [0.25] * 766])
    con.execute(f"INSERT INTO {CORE}.post_enrichment VALUES ('duplicate', ?)", [[0.25] * 768])
    text = embed.load("embed")
    start = text.index("SELECT DISTINCT e.post_id", text.index("LEFT JOIN ("))
    query = text[start:text.index(") AS done", start)]
    complete = duck_execute(con)(query, {})
    assert [r["post_id"] for r in complete] == ["duplicate"]
    selected = duck_execute(con)(cluster.load("cluster_posts"), {"run_date": DAY, "market": "za"})
    assert [(r["post_id"], r["embedding"][0]) for r in selected] == [("duplicate", 0.25)]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), None])
def test_nonfinite_generated_embedding_is_counted_failed_and_not_persisted(bad):
    con = fixture_warehouse([("p", "text", None, DAY)], [("p", DAY)])
    counts = run_embed_sql(con, DAY, DAY, model=lambda _: ([bad] + [0.25] * 767, ""))
    assert counts["embedded"] == 0 and counts["failed"] == 1
    assert con.execute(f"SELECT COUNT(*) FROM {CORE}.post_enrichment").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), None, 1e100])
def test_fit_matrix_rejects_invalid_components_before_consuming_that_post(bad):
    post = {"embedding": [bad] + [0.25] * 767}
    with pytest.raises(ValueError, match="embedding"):
        cluster.embedding_matrix([post])
    assert "embedding" in post


@pytest.mark.parametrize("bad", [[], [1.0], [float("nan")] * 768, [float("inf")] * 768, None])
def test_malformed_prior_identity_is_held_while_healthy_match_is_persisted(monkeypatch, bad):
    a, b = [1.0] + [0.0] * 767, [0.0, 1.0] + [0.0] * 766
    clusters = [{"cluster_id": cid, "centroid": vec, "keywords": ["dance"], "label": "dance",
                 "hashtags": ["amapiano"], "sounds": [], "creators": [], "members": [(cid + "-p", 1.0)],
                 "market": "za", "platform": "tiktok", "local_terms": []}
                for cid, vec in (("healthy-cluster", a), ("uncertain-cluster", b))]
    items = [{"item_id": name, "kind": "topic", "canonical_key": "topic:" + name, "label": name,
              "centroid": vec, "keywords": ["dance"], "hashtags": ["amapiano"], "sounds": [], "creators": [],
              "last_seen": DAY - timedelta(days=1)} for name, vec in (("healthy", a), ("malformed", bad))]
    monkeypatch.setattr(cluster, "label_net", lambda execute, clusters, **kw:
                        (clusters, {"model_usd": 0.0, "booked_usd": 0.0}))
    counts, writes = fake_run(monkeypatch, clusters, items=items)
    written = [r for batch in writes for r in json.loads(batch["cluster_rows"])]
    assert [(r["cluster_id"], r["item_id"]) for r in written] == [("healthy-cluster", "healthy")]
    assert counts["invalid_prior_centroids"] == 1
    assert counts["invalid_prior_centroid_ids"] == ["malformed"]
    assert counts["held_cluster_ids"] == ["uncertain-cluster"]
    assert counts["held_clusters"] == 1


def test_empty_prior_centroid_remains_visible_to_the_runtime_hold():
    con = core_db()
    con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({','.join('?' * 17)})",
                map_row("broken", [], last_seen=DAY))
    rows = duck_execute(con)(cluster.load("cluster_items"), cluster.item_params(DAY, "za"))
    assert [r["item_id"] for r in rows] == ["broken"]


def test_only_malformed_prior_identity_holds_every_new_cluster_without_a_write(monkeypatch):
    current = {"cluster_id": "uncertain", "centroid": [1.0, 0.0], "keywords": ["dance"], "label": "dance",
               "hashtags": [], "sounds": [], "creators": [], "members": [("p", 1.0)],
               "market": "za", "platform": "tiktok", "local_terms": []}
    monkeypatch.setattr(cluster, "label_net", lambda execute, clusters, **kw:
                        (clusters, {"model_usd": 0.2, "booked_usd": 0.2}))
    counts, writes = fake_run(monkeypatch, [current], items=[{"item_id": "broken", "centroid": []}])
    assert writes == [] and counts["new"] == 0 and counts["members"] == 0
    assert counts["held_cluster_ids"] == ["uncertain"] and counts["invalid_prior_centroid_ids"] == ["broken"]
    assert counts["model_usd"] == counts["booked_usd"] == 0.2
