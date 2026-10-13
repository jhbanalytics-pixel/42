from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from core.agent.context import RunContext
from core.agent.tools.warehouse import _keyword_sql, _store_scope
from core.detect import sqlrun
from core.detect.tests import duck
from core.detect.tests.fixtures import D, creator, obs, post


ROOT = Path(__file__).resolve().parents[3]


def _semantic_query(day, market, k=10):
    tree = sqlglot.parse_one(
        (ROOT / "core/understand/sql/tvf_search_posts_scoped.sql").read_text(encoding="utf-8"),
        read="bigquery",
    ).expression.unnest()
    distance = next(n for n in tree.find_all(exp.Anonymous) if n.name.upper() == "DISTANCE")
    scoring = distance.find_ancestor(exp.Select)
    distance.parent.replace(exp.column("near_dup_size", table="e"))
    scoring.set("joins", [j for j in scoring.args["joins"] if j.this.alias != "qe"])
    for table in tree.find_all(exp.Table):
        if table.db:
            table.set("catalog", None)
            table.set("db", exp.to_identifier("core"))
    for col in list(tree.find_all(exp.Column)):
        if col.table:
            continue
        if col.name in ("since", "until"):
            col.replace(exp.cast(exp.Literal.string(day.isoformat()), "DATE"))
        elif col.name == "market":
            col.replace(exp.Literal.string(market))
        elif col.name in ("platforms", "author"):
            col.replace(exp.Null())
        elif col.name == "min_engagement":
            col.replace(exp.Literal.number(0))
        elif col.name == "k":
            col.replace(exp.Literal.number(k))
    return tree.sql(dialect="duckdb")


@pytest.mark.parametrize("market,post_day,instant", [
    ("NG", date(2026, 9, 19), "2026-09-19T22:30:00+00:00"),
    ("KE", date(2026, 9, 21), "2026-09-20T21:30:00+00:00"),
])
def test_semantic_search_includes_sast_boundary_posts_before_ranking(market, post_day, instant):
    with duck.connect(views=False) as con:
        duck.load(con, "core.posts", [post("edge", "c", post_day, geo_market=market,
                                            published_at=datetime.fromisoformat(instant))])
        duck.load(con, "core.post_enrichment", [{"post_id": "edge", "embedding": [0.0] * 768,
                                                "near_dup_size": 1}])
        rows = con.execute(_semantic_query(D, market)).fetchall()
        assert [r[0] for r in rows] == ["edge"]


def test_semantic_search_excludes_outside_sast_window_before_top_k():
    with duck.connect(views=False) as con:
        duck.load(con, "core.posts", [
            post("outside", "c", D, geo_market="NG",
                 published_at=datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc)),
            post("inside", "c", D, geo_market="NG"),
        ])
        duck.load(con, "core.post_enrichment", [
            {"post_id": "outside", "embedding": [0.0] * 768, "near_dup_size": 0},
            {"post_id": "inside", "embedding": [0.0] * 768, "near_dup_size": 1},
        ])
        rows = con.execute(_semantic_query(D, "NG", k=1)).fetchall()
        assert [r[0] for r in rows] == ["inside"]


def test_evidence_creator_identity_keeps_platform_and_does_not_multiply_posts():
    with duck.connect(views=False) as con:
        stmt = next(s for s in sqlrun.agent_statements("core", "agent")
                    if sqlrun.object_name(s) == "agent.v_item_evidence")
        con.execute(duck.create_statement(stmt))
        duck.load(con, "core.posts", [post("p", "shared", D)])
        duck.load(con, "core.post_observations", [obs("p", D, "panel", "panel")])
        duck.load(con, "core.post_items", [{"post_id": "p", "item_id": "i", "via": "hashtag"}])
        duck.load(con, "core.creators", [creator("shared"),
                                        {"creator_id": "shared", "platform": "x", "handle": "other"}])
        rows = duck.query(con, "SELECT * FROM {agent}.v_item_evidence")
        assert len(rows) == 1
        assert rows[0]["handle"] == "shared"


@pytest.mark.parametrize("mode", ["keyword", "store"])
def test_stored_search_scopes_execute_sast_dates_with_local_partition_edges(mode):
    ctx = RunContext(run_id="boundary", tier="T0", as_of=datetime(2026, 9, 21),
                     window_start=D, window_end=D)
    if mode == "keyword":
        sql, params = _keyword_sql(ctx, "music", D, D, {}, [], "engagement", 10)
    else:
        joins, where, params, _ = _store_scope(ctx, None)
        sql = f"SELECT p.post_id {joins} WHERE {where}"
    tree = sqlglot.parse_one(sql, read="bigquery")
    for table in tree.find_all(exp.Table):
        if table.db:
            table.set("db", exp.to_identifier("core"))
    used = {p.name for p in tree.find_all(exp.Parameter)}
    with duck.connect(views=False) as con:
        duck.load(con, "core.posts", [
            post("ng_edge", "c", date(2026, 9, 19), text="music",
                 published_at=datetime(2026, 9, 19, 22, 30, tzinfo=timezone.utc)),
            post("ke_edge", "c", date(2026, 9, 21), text="music",
                 published_at=datetime(2026, 9, 20, 21, 30, tzinfo=timezone.utc)),
            post("outside", "c", D, text="music",
                 published_at=datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc)),
            post("fallback", "c", D, text="music", published_at=None),
            post("fallback_outside", "c", date(2026, 9, 19), text="music", published_at=None),
        ])
        rows = con.execute(tree.sql(dialect="duckdb"), {k: v for k, v in params.items() if k in used}).fetchall()
        assert {r[0] for r in rows} == {"ng_edge", "ke_edge", "fallback"}


def test_semantic_search_preserves_missing_timestamp_fallback_and_deduplicates_embeddings():
    with duck.connect(views=False) as con:
        duck.load(con, "core.posts", [post("fallback", "c", D, geo_market="ZA", published_at=None),
                                      post("outside", "c", date(2026, 9, 19),
                                           geo_market="ZA", published_at=None)])
        duck.load(con, "core.post_enrichment", [
            {"post_id": "fallback", "embedding": [0.0] * 768, "near_dup_size": 2},
            {"post_id": "fallback", "embedding": [0.0] * 768, "near_dup_size": 1},
            {"post_id": "outside", "embedding": [0.0] * 768, "near_dup_size": 0},
        ])
        rows = con.execute(_semantic_query(D, "ZA")).fetchall()
        assert len(rows) == 1
        assert rows[0][:2] == ("fallback", 1)
