"""Benchmark cluster read market case on fixtures: no BigQuery."""
from datetime import date

import duckdb

from ops.runners import benchmark_moments as bm



def cluster_sql_on_duckdb(rows, moment_market="NG", and_re=""):
    """bm.CLUSTERS_SQL, unchanged, on DuckDB over the given clusters rows and one NG moment."""
    from core.detect.tests import duck

    sql = bm.CLUSTERS_SQL.replace(
        bm.MOMENTS, "WITH m AS (SELECT 'NG1' AS id, DATE '2026-10-04' AS d, "
                    f"'{moment_market}' AS market, 'bbnaija' AS any_re, '{and_re}' AS and_re)\n", 1)
    sql = sql.replace(f"`{bm.CORE}.clusters`", "clusters")
    sql = sql.replace("@start", "DATE '2026-10-01'").replace("@obs_end", "DATE '2026-10-07'")
    with duckdb.connect() as con:
        con.execute("CREATE TABLE clusters(cluster_date DATE, cluster_id VARCHAR, market VARCHAR, item_id VARCHAR, "
                    "label VARCHAR, keywords VARCHAR[], local_terms VARCHAR[])")
        con.executemany("INSERT INTO clusters VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        cur = con.execute(duck.to_duck(sql))
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def cluster_row(market, item_id="item-1", label="temi, teminators, nkem", keywords=("big brother", "bbnaija"),
                day=date(2026, 10, 5), cluster_id=None):
    return (day, cluster_id or f"20261005-{market}-023", market, item_id, label, list(keywords), [])


def test_cluster_read_matches_the_lowercase_market_the_clusters_table_stores():
    """core.clusters stores markets as ng, za and ke (the detect SQL reads them with UPPER(k.market)); the answer
    key uses NG, ZA and KE. Comparing them as written found no cluster for any moment."""
    [row] = cluster_sql_on_duckdb([cluster_row("ng")])
    assert row["id"] == "NG1" and row["n"] == 1


def test_cluster_read_still_leaves_out_another_markets_clusters():
    assert cluster_sql_on_duckdb([cluster_row("za", cluster_id="20261005-za-001")]) == []
    assert cluster_sql_on_duckdb([cluster_row("NG")]) != []  # an uppercase writer would also match
