"""Benchmark held-item identity from the cluster read, on fixtures: no BigQuery."""
from datetime import date

import duckdb

from ops.runners import benchmark_moments as bm

MOMENT = {"id": "bbnaija", "market": "NG", "d": date(2026, 10, 5), "any_re": r"bbnaija", "and_re": ""}


def held_item(**changes):
    return {"item_id": "bbnaija-item", "title": "temi, teminators, nkem", "rule": "G10",
            "reason": "explanation_failed", "evidence": [{"id": "post-fixture", "platform": "twitter",
                "text": "Breaking: Temi Nkem wins BBNaija season 11"}], **changes}


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


def test_cluster_read_returns_the_item_ids_whose_own_label_or_keywords_match():
    [row] = cluster_sql_on_duckdb([cluster_row("ng", item_id="held-item"),
                                   cluster_row("ng", item_id="other", label="fyp, viral", keywords=("fyp",),
                                               cluster_id="20261005-ng-024")])
    assert row["item_ids"] == ["held-item"]


def test_cluster_identity_honours_the_second_pattern():
    assert cluster_sql_on_duckdb([cluster_row("ng")], and_re="finale")[0]["item_ids"] in (None, [])
    [row] = cluster_sql_on_duckdb([cluster_row("ng", keywords=("bbnaija", "finale"))], and_re="finale")
    assert row["item_ids"] == ["item-1"]


def test_a_held_item_is_matched_by_its_cluster_identity_when_its_title_does_not_match():
    """T4, 7 October: the held topic is titled 'temi, teminators, nkem'. Its cluster keywords name BBNaija, and the
    cluster read gives its item id. No title match and no reviewed list is needed."""
    moment = {**MOMENT, "identity_items": {"bbnaija-item"}}
    cards, held = bm.brief_hits(moment, {("2026-10-05", "NG"): {"held_back": {"items": [held_item()]}}})
    assert cards == [] and len(held) == 1
    assert "cluster identity match" in held[0] and "headline not matched" in held[0]
    assert "G10 explanation_failed" in held[0]
    assert bm.verdict({"cards": cards, "held": held}) == "HELD"


def test_an_item_outside_the_cluster_identity_is_not_matched_by_its_evidence_alone():
    moment = {**MOMENT, "identity_items": {"some-other-item"}}
    briefs = {("2026-10-05", "NG"): {"held_back": {"items": [held_item()]}}}
    assert bm.brief_hits(moment, briefs) == ([], [])


def test_a_card_matched_by_cluster_identity_is_a_card_and_is_not_also_counted_held():
    moment = {**MOMENT, "identity_items": {"bbnaija-item"}}
    item = held_item(title="temi, teminators, nkem")
    briefs = {("2026-10-05", "NG"): {"cards": [item], "held_back": {"items": [item]}}}
    cards, held = bm.brief_hits(moment, briefs)
    assert len(cards) == 1 and held == []
    assert "cluster identity match" in cards[0]
