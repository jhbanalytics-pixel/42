import re
from datetime import datetime

import pytest
import sqlglot
from sqlglot import exp

from core.agent.context import RunContext
from core.agent.tools.warehouse import _search_filters, _semantic_sql
from core.detect.tests import duck
from core.detect.tests.fixtures import D, post
from core.schema.tests.test_warehouse_accuracy import ROOT


def _generated_semantic_query(platforms=None, author=None, min_engagement=None):
    ctx = RunContext(run_id="cohort", tier="T0", as_of=datetime(2026, 9, 21), market="ZA")
    filters, shared = _search_filters(platforms, author, min_engagement)
    sql, params = _semantic_sql(ctx, "paraphrase", D, D, shared, filters, 2)
    outer = sqlglot.parse_one(sql, read="bigquery")
    fn = next(t for t in outer.find_all(exp.Table) if isinstance(t.this, exp.Func))
    name = fn.this.name
    text = (ROOT / f"core/understand/sql/{name}.sql").read_text(encoding="utf-8")
    body = sqlglot.parse_one(text, read="bigquery").expression.unnest()
    declarations = re.search(r"CREATE TABLE FUNCTION.*?\((.*?)\)\s*AS", text, re.S).group(1)
    formal = [p.strip().split()[0] for p in declarations.split(",")]
    arguments = dict(zip(formal, fn.this.expressions))
    for col in list(body.find_all(exp.Column)):
        if not col.table and col.name in arguments:
            col.replace(arguments[col.name].copy())
    distance = next(n for n in body.find_all(exp.Anonymous) if n.name.upper() == "DISTANCE")
    scoring = distance.find_ancestor(exp.Select)
    distance.parent.replace(exp.column("near_dup_size", table="e"))
    scoring.set("joins", [j for j in scoring.args["joins"] if j.this.alias != "qe"])
    fn.replace(exp.Subquery(this=body, alias=fn.args["alias"].copy()))
    for table in outer.find_all(exp.Table):
        if table.db:
            table.set("catalog", None)
            table.set("db", exp.to_identifier("core"))
    used = {p.name for p in outer.find_all(exp.Parameter)}
    return outer.sql(dialect="duckdb"), {k: v for k, v in params.items() if k in used}


@pytest.mark.parametrize("platforms,author,minimum,target_platform,target_id,target_handle,target_engagement", [
    (["tiktok"], None, None, "tiktok", "target", "target", 0),
    (["x"], None, None, "twitter", "target", "target", 0),
    (["twitter"], None, None, "twitter", "target", "target", 0),
    (None, "@Wanted", None, "tiktok", "native", "wanted", 0),
    (None, "target", None, "tiktok", "target", "different", 0),
    (None, None, 7, "tiktok", "target", "target", 7),
    (["tiktok"], "@Wanted", 7, "tiktok", "target", "wanted", 7),
])
def test_generated_semantic_query_filters_cohort_before_top_k(
    platforms, author, minimum, target_platform, target_id, target_handle, target_engagement
):
    with duck.connect(views=False) as con:
        distractor_platform = "instagram"
        duck.load(con, "core.posts", [
            post("near1", "other1", D, platform=distractor_platform, geo_market="ZA", engagement=0),
            post("near2", "other2", D, platform=distractor_platform, geo_market="ZA", engagement=0),
            post("target", target_id, D, platform=target_platform, geo_market="ZA", engagement=target_engagement),
        ])
        duck.load(con, "core.creators", [{"creator_id": target_id, "platform": target_platform,
                                         "handle": target_handle},
                                        {"creator_id": "other1", "platform": "x", "handle": "wanted"}])
        duck.load(con, "core.post_enrichment", [
            {"post_id": p, "embedding": [0.0] * 768, "near_dup_size": distance}
            for p, distance in [("near1", 0), ("near2", 1), ("target", 2)]
        ])
        query, params = _generated_semantic_query(platforms, author, minimum)
        rows = con.execute(query, params).fetchall()
        assert [r[0] for r in rows] == ["target"]


def test_generated_semantic_query_unfiltered_and_null_engagement_preserve_nearest():
    with duck.connect(views=False) as con:
        duck.load(con, "core.posts", [post("near", "c", D, geo_market="ZA", engagement=None),
                                      post("far", "c", D, geo_market="ZA", engagement=10)])
        duck.load(con, "core.post_enrichment", [{"post_id": "near", "embedding": [0.0] * 768,
                                                "near_dup_size": 0},
                                               {"post_id": "far", "embedding": [0.0] * 768,
                                                "near_dup_size": 1}])
        query, params = _generated_semantic_query(platforms=[], min_engagement=None)
        assert [r[0] for r in con.execute(query, params).fetchall()] == ["near", "far"]


def test_scoped_semantic_query_retains_embedding_admission_and_byte_cap():
    from core.agent.tests.test_ask import NOW, SEARCH_EMBED_USD
    from core.agent.tests.test_ask_model_budget import make_budget
    from core.agent.tests.test_sql_tool import FakeWarehouse
    from core.agent.model_budget import BudgetRefused
    from core.agent.tools.sql_query import MAX_BYTES_BILLED, sql_query

    ctx = RunContext(run_id="cohort-budget", tier="T1", as_of=NOW,
                     model_budget=make_budget(hold=SEARCH_EMBED_USD, research=SEARCH_EMBED_USD))
    filters, shared = _search_filters(["tiktok"], None, 0)
    sql, params = _semantic_sql(ctx, "query", D, D, shared, filters, 2)
    wh = FakeWarehouse(rows=[])
    sql_query(ctx, wh, sql, purpose="cohort", params=params)
    assert ctx.model_budget.booked_usd == ctx.model_usd_extra == SEARCH_EMBED_USD
    assert wh.runs[0][2] == MAX_BYTES_BILLED
    with pytest.raises(BudgetRefused):
        sql_query(ctx, wh, sql, purpose="cohort again", params=params)
    assert len(wh.runs) == 1


def test_scoped_semantic_query_retains_disabled_job_retry(monkeypatch):
    from core.agent.tests.test_semantic_job_retry import test_run_disables_job_retries_only_for_semantic_embedding_queries

    test_run_disables_job_retries_only_for_semantic_embedding_queries(
        monkeypatch,
        "SELECT * FROM intelligence_42_agent.tvf_search_posts_scoped(@q, NULL, @since, @until, @k, "
        "CAST(NULL AS ARRAY<STRING>), CAST(NULL AS STRING), 0)",
        True,
    )


def test_scoped_semantic_query_foreign_dataset_is_refused():
    from core.agent.context import Refused
    from core.agent.tools.sql_query import check_sql

    with pytest.raises(Refused):
        check_sql("SELECT * FROM intelligence_42_core.tvf_search_posts_scoped"
                  "('q', NULL, DATE '2026-09-20', DATE '2026-09-20', 2, [], NULL, 0)")
