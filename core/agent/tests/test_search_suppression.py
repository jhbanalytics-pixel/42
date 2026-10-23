"""A stored post by someone hidden now never reaches the research model (C5 v2 section 16). search_posts and fetch_posts,
called as the model calls them through build_functions, run on DuckDB fixture tables with L1's own suppressions table
and v_suppressed_creators view: a creator named by creator_id, and one named only by handle, are both left out of the
model's JSON, ctx.evidence and the recorded query rows, a visible peer stays, and a list that cannot be read stores
nothing."""
import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest
import sqlglot

from core.agent import toolset
from core.agent.context import Refused, RunContext
from core.agent.tests.test_history import DuckWarehouse
from core.agent.tools.sql_query import INTERNAL_ALLOW, check_sql
from core.agent.tools.warehouse import fetch_posts, topic_sweep

CORE_SQL = Path(__file__).resolve().parents[2] / "schema" / "core.sql"
WINDOW = (date(2026, 9, 21), date(2026, 9, 28))
POSTS = [  # post_id, platform, creator_id, text
    ("p_by_id", "tiktok", "u_by_id", "braai with the hidden id creator"),
    ("p_by_handle", "x", "u_by_handle", "braai with the hidden handle creator"),
    ("p_peer", "tiktok", "u_peer", "braai with a visible peer"),
]
CREATORS = [("tiktok", "u_by_id", "fixture_id_hidden"), ("twitter", "u_by_handle", "Fixture_Handle_Hidden"),
            ("tiktok", "u_peer", "visible_peer")]
SUPPRESSIONS = [  # one by creator_id, one naming only a platform and handle, and a lifted one for the peer
    ("sup_1", datetime(2026, 9, 1), "suppressed", "u_by_id", None, None),
    ("sup_2", datetime(2026, 9, 1), "suppressed", None, "x", "@fixture_handle_hidden"),
    ("sup_3", datetime(2026, 9, 1), "suppressed", "u_peer", None, None),
    ("sup_3", datetime(2026, 9, 2), "lifted", "u_peer", None, None),
]
HIDDEN = ("p_by_id", "p_by_handle", "fixture_id_hidden", "fixture_handle_hidden", "hidden id", "hidden handle")


def suppressed_view_sql():
    """L1's v_suppressed_creators exactly as core.sql defines it, without the project, for DuckDB."""
    text = CORE_SQL.read_text(encoding="utf-8")
    body = re.search(r"CREATE VIEW IF NOT EXISTS `ogilvy-trends-v2\.intelligence_42_core\.v_suppressed_creators` AS"
                     r"(.*?);", text, re.S).group(1).replace("ogilvy-trends-v2.", "")
    return sqlglot.transpile(body, read="bigquery", write="duckdb")[0]


@pytest.fixture
def con():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, "
                "creator_id VARCHAR, published_at TIMESTAMP, post_date DATE, geo_market VARCHAR, geo_source VARCHAR, "
                "text VARCHAR, views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, engagement BIGINT, "
                "hashtags VARCHAR[])")
    con.execute("CREATE TABLE intelligence_42_core.creators (platform VARCHAR, creator_id VARCHAR, handle VARCHAR, "
                "home_market VARCHAR)")
    con.execute("CREATE TABLE intelligence_42_core.v_post_source_markets (post_id VARCHAR, "
                "source_markets VARCHAR[], source_sightings STRUCT(source_market VARCHAR, source_region VARCHAR, "
                "route VARCHAR, protocol VARCHAR, observed_at TIMESTAMP, obs_date DATE)[])")
    con.execute("CREATE TABLE intelligence_42_core.suppressions (suppression_id VARCHAR, status_at TIMESTAMP, "
                "status VARCHAR, creator_id VARCHAR, platform VARCHAR, handle VARCHAR, reason VARCHAR, who VARCHAR)")
    con.execute("CREATE VIEW intelligence_42_core.v_suppressed_creators AS " + suppressed_view_sql())
    con.executemany("INSERT INTO intelligence_42_core.posts (post_id, platform, url, creator_id, published_at, "
                    "post_date, geo_market, text, engagement) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(pid, platform, f"https://example.test/{pid}", cid, datetime(2026, 9, 27, 9, 0),
                      date(2026, 9, 27), "ZA", text, 10) for pid, platform, cid, text in POSTS])
    con.executemany("INSERT INTO intelligence_42_core.creators VALUES (?, ?, ?, 'ZA')", CREATORS)
    con.executemany("INSERT INTO intelligence_42_core.suppressions VALUES (?, ?, ?, ?, ?, ?, 'fixture', 'Jo')",
                    SUPPRESSIONS)
    return con


@pytest.fixture
def ctx():
    ctx = RunContext(run_id="run_test", tier="T0", as_of=datetime(2026, 9, 28, 6, 0), market="ZA")
    ctx.window_start, ctx.window_end = WINDOW
    return ctx


def functions(ctx, warehouse):
    return toolset.build_functions(ctx, warehouse, None, None)


def recorded(ctx):
    return json.dumps([q["rows"] for q in ctx.queries.values()], default=str).casefold()


def leaks(text):
    return [h for h in HIDDEN if h in text.casefold()]


def test_search_posts_leaves_out_posts_by_hidden_creators_and_keeps_the_peer(ctx, con):
    out = functions(ctx, DuckWarehouse(con))["search_posts"](query="braai")
    shown = json.dumps(out, default=str)
    assert [e["id"] for e in out["evidence"]] == ["p_peer"]
    assert not leaks(shown)
    assert set(ctx.evidence) == {"p_peer"}
    assert not leaks(recorded(ctx))


def test_fetch_posts_never_stores_a_hidden_creator_post(ctx, con):
    out = fetch_posts(ctx, DuckWarehouse(con), ["p_by_id", "p_by_handle", "p_peer"], WINDOW)
    assert [e["id"] for e in out["evidence"]] == ["p_peer"]
    assert set(ctx.evidence) == {"p_peer"}
    assert not leaks(json.dumps(out, default=str)) and not leaks(recorded(ctx))


def test_both_search_legs_and_the_fetch_read_the_list_through_the_internal_fence(ctx, con):
    warehouse = DuckWarehouse(con)
    functions(ctx, warehouse)["search_posts"](query="braai")
    fetch_posts(ctx, warehouse, ["p_peer"], WINDOW)
    sqls = [sql for sql, _ in warehouse.runs]
    assert len(sqls) >= 3 and any("tvf_search_posts" in sql for sql in sqls)
    for sql in sqls:
        if "intelligence_42_core.posts" in sql or "tvf_search_posts" in sql:
            assert "intelligence_42_core.v_suppressed_creators" in sql
            check_sql(sql, INTERNAL_ALLOW["search_posts"])
            with pytest.raises(Refused):
                check_sql(sql)  # the model's own SQL can never name the list


def test_a_list_that_cannot_be_read_stores_no_post(ctx, con):
    down = DuckWarehouse(con, fail={"intelligence_42_core.v_suppressed_creators": RuntimeError("Access Denied")})
    with pytest.raises(Refused):
        functions(ctx, down)["search_posts"](query="braai")
    with pytest.raises(RuntimeError):
        fetch_posts(ctx, down, ["p_by_id", "p_peer"], WINDOW)
    assert ctx.evidence == {}


def test_the_topic_sweep_never_stores_a_hidden_creator_post(ctx, con):
    """The depth sweep stores its fused rows as evidence too, so its keyword and semantic reads carry the same
    predicate through the internal fence, and a list that cannot be read fails each leg."""
    warehouse = DuckWarehouse(con)
    topic_sweep(ctx, warehouse, ["braai"], platforms=None)
    assert not set(ctx.evidence) & {"p_by_id", "p_by_handle"}
    assert not leaks(recorded(ctx))
    for sql, _ in warehouse.runs:
        if "intelligence_42_core.posts" in sql or "tvf_search_posts" in sql:
            assert "intelligence_42_core.v_suppressed_creators" in sql
            check_sql(sql, INTERNAL_ALLOW["topic_sweep"])
    down = DuckWarehouse(con, fail={"intelligence_42_core.v_suppressed_creators": RuntimeError("Access Denied")})
    other = RunContext(run_id="run_test_2", tier="T0", as_of=datetime(2026, 9, 28, 6, 0), market="ZA")
    other.window_start, other.window_end = WINDOW
    topic_sweep(other, down, ["braai"], platforms=None)
    assert other.evidence == {}
