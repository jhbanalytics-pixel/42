import pytest

from core.understand import cluster, embed
from core.understand.tests.test_cluster import CORE, DAY, core_db, duck_execute
from core.understand.tests.test_embed import fixture_warehouse, run_embed_sql


@pytest.mark.parametrize("bad", [-1e100, 1e100, -3.5e38, 3.5e38])
def test_float32_incompatible_embedding_is_repairable_and_cannot_displace_safe_duplicate(bad):
    con = core_db()
    for post_id in ("only_bad", "duplicate"):
        con.execute(f"INSERT INTO {CORE}.posts VALUES (?, 'tiktok', 'creator', 'text', [], NULL, ?)", [post_id, DAY])
        con.execute(f"INSERT INTO {CORE}.post_observations VALUES (?, ?, 'ZA')", [post_id, DAY])
        con.execute(f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?)", [post_id, [bad] + [0.25] * 767])
    con.execute(f"INSERT INTO {CORE}.post_enrichment VALUES ('duplicate', ?)", [[0.25] * 768])
    text = embed.load("embed")
    start = text.index("SELECT DISTINCT e.post_id", text.index("LEFT JOIN ("))
    complete = duck_execute(con)(text[start:text.index(") AS done", start)], {})
    assert [row["post_id"] for row in complete] == ["duplicate"]
    selected = duck_execute(con)(cluster.load("cluster_posts"), {"run_date": DAY, "market": "za"})
    assert [(row["post_id"], row["embedding"][0]) for row in selected] == [("duplicate", 0.25)]
    cluster.embedding_matrix(selected)


@pytest.mark.parametrize("bad", [-1e100, 1e100])
def test_generated_float32_incompatible_vector_is_failed_and_never_persisted(bad):
    con = fixture_warehouse([("p", "dance", None, DAY)], [("p", DAY)])
    counts = run_embed_sql(con, DAY, DAY, model=lambda _: ([bad] + [0.25] * 767, ""))
    assert counts["embedded"] == 0 and counts["failed"] == 1
    assert con.execute(f"SELECT COUNT(*) FROM {CORE}.post_enrichment").fetchone()[0] == 0
