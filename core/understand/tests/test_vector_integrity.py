from core.understand import cluster, embed
import sqlglot
from core.understand.tests.test_cluster import CORE, DAY, core_db, duck_execute


def test_embed_does_not_treat_short_vectors_as_complete():
    text = embed.load("embed")
    start = text.index("SELECT DISTINCT e.post_id", text.index("LEFT JOIN ("))
    end = text.index(") AS done", start)
    query = text[start:end].replace(
        "`ogilvy-trends-v2.intelligence_42_core.post_enrichment`", "post_enrichment")
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE post_enrichment (post_id VARCHAR, embedding DOUBLE[])")
    con.executemany("INSERT INTO post_enrichment VALUES (?, ?)", [
        ("complete", [0.25] * 768),
        ("short", [0.5]),
    ])
    assert con.execute(sqlglot.transpile(query, read="bigquery", write="duckdb")[0]).fetchall() == [("complete",)]


def test_cluster_posts_ignores_short_vectors_and_keeps_a_valid_duplicate():
    con = core_db()
    for post_id in ("complete", "short"):
        con.execute(f"INSERT INTO {CORE}.posts VALUES (?, 'tiktok', 'creator', 'text', [], NULL, ?)",
                    [post_id, DAY])
        con.execute(f"INSERT INTO {CORE}.post_observations VALUES (?, ?, 'ZA')", [post_id, DAY])
    con.executemany(f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?)", [
        ("complete", [-0.5]),
        ("complete", [0.25] * 768),
        ("short", [0.5]),
    ])
    rows = duck_execute(con)(cluster.load("cluster_posts"), {"run_date": DAY, "market": "za"})
    assert [(row["post_id"], len(row["embedding"])) for row in rows] == [("complete", 768)]
