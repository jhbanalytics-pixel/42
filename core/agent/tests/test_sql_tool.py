from datetime import datetime
from types import SimpleNamespace

import pytest

from core.agent.context import Refused, RunContext, result_hash
from core.agent.tools.sql_query import (
    ALLOWED_DATASETS,
    ALLOWED_TVFS,
    MAX_BYTES_BILLED,
    MAX_ROWS,
    BigQueryWarehouse,
    check_sql,
    sql_query,
)


class FakeWarehouse:
    def __init__(self, rows=None, bytes_=1_000, tables=("ogilvy-trends-v2.intelligence_42_core.posts",)):
        self.rows = rows if rows is not None else [{"post_id": "p1", "n": 3}]
        self.bytes = bytes_
        self.tables = list(tables)
        self.dry_runs = []
        self.runs = []

    def dry_run(self, sql, params):
        self.dry_runs.append((sql, params))
        return {"bytes": self.bytes, "tables": list(self.tables)}

    def run(self, sql, params, max_bytes_billed):
        self.runs.append((sql, params, max_bytes_billed))
        return [dict(r) for r in self.rows]


@pytest.fixture
def ctx():
    return RunContext(run_id="run_test", tier="T0", as_of=datetime(2026, 9, 28, 6, 0))


def test_constants():
    assert ALLOWED_DATASETS == ("intelligence_42_core", "intelligence_42_agent")
    assert MAX_BYTES_BILLED == 2_000_000_000


REFUSED_STATEMENTS = {
    "insert": "INSERT INTO intelligence_42_core.posts (post_id) VALUES ('x')",
    "insert_select": "INSERT intelligence_42_agent.findings SELECT * FROM intelligence_42_core.posts",
    "update": "UPDATE intelligence_42_core.posts SET text = 'x' WHERE post_id = 'p1'",
    "delete": "DELETE FROM intelligence_42_core.posts WHERE post_id = 'p1'",
    "merge": (
        "MERGE intelligence_42_core.posts t USING intelligence_42_agent.findings s ON t.post_id = s.post_id "
        "WHEN MATCHED THEN UPDATE SET text = s.text"
    ),
    "create_table": "CREATE TABLE intelligence_42_agent.x AS SELECT * FROM intelligence_42_core.posts",
    "create_view": "CREATE VIEW intelligence_42_agent.v AS SELECT * FROM intelligence_42_core.posts",
    "create_function": "CREATE TEMP FUNCTION f(x INT64) AS (x + 1)",
    "drop": "DROP TABLE intelligence_42_core.posts",
    "alter": "ALTER TABLE intelligence_42_core.posts ADD COLUMN y STRING",
    "truncate": "TRUNCATE TABLE intelligence_42_core.posts",
    "export": "EXPORT DATA OPTIONS(uri='gs://bucket/*.csv', format='CSV') AS SELECT * FROM intelligence_42_core.posts",
    "call": "CALL intelligence_42_core.some_procedure()",
    "declare": "DECLARE x INT64 DEFAULT 1",
    "set": "SET x = 1",
    "begin_block": "BEGIN SELECT * FROM intelligence_42_core.posts; END",
    "begin_transaction": "BEGIN TRANSACTION",
    "grant": "GRANT `roles/bigquery.dataViewer` ON TABLE intelligence_42_core.posts TO 'user:someone@example.com'",
    "assert": "ASSERT (SELECT COUNT(*) FROM intelligence_42_core.posts) > 0",
    "unparseable": "SELEC oops",
    "garbage": "SELECT FROM WHERE ((",
    "empty": "   ",
}


@pytest.mark.parametrize("kind", sorted(REFUSED_STATEMENTS))
def test_check_sql_refuses_non_queries(kind):
    with pytest.raises(Refused) as err:
        check_sql(REFUSED_STATEMENTS[kind])
    assert str(err.value).strip()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT * FROM intelligence_42_core.posts; SELECT * FROM intelligence_42_agent.v_items_today",
        "SELECT * FROM intelligence_42_core.posts; DROP TABLE intelligence_42_core.posts",
    ],
)
def test_check_sql_refuses_multiple_statements(sql):
    with pytest.raises(Refused):
        check_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM posts",
        "SELECT * FROM `posts`",
        "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')",
        "WITH a AS (SELECT 1 AS x) SELECT * FROM a JOIN posts USING (x)",
    ],
)
def test_check_sql_refuses_unqualified_tables(sql):
    with pytest.raises(Refused):
        check_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM trends_v2_dev.posts",
        "SELECT * FROM `ogilvy-trends-v2.trends_v2_dev.posts`",
        "SELECT * FROM intelligence_42_core.posts WHERE post_id IN (SELECT post_id FROM trends_v2_dev.x)",
        "WITH a AS (SELECT * FROM trends_v2_dev.posts) SELECT * FROM a",
        "SELECT * FROM intelligence_42_core.posts UNION ALL SELECT * FROM other_dataset.posts",
        "SELECT * FROM ML.PREDICT(MODEL intelligence_42_core.m, TABLE intelligence_42_core.posts)",
        "SELECT * FROM trends_v2_dev.tvf_search_items('braai')",
    ],
)
def test_check_sql_refuses_other_datasets(sql):
    with pytest.raises(Refused):
        check_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM intelligence_42_core.INFORMATION_SCHEMA.TABLES",
        "SELECT * FROM `ogilvy-trends-v2.intelligence_42_agent.INFORMATION_SCHEMA.COLUMNS`",
        "SELECT * FROM `region-eu`.INFORMATION_SCHEMA.JOBS",
        "SELECT * FROM INFORMATION_SCHEMA.SCHEMATA",
    ],
)
def test_check_sql_refuses_information_schema(sql):
    with pytest.raises(Refused):
        check_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM `other-project.intelligence_42_core.posts`",
        "SELECT * FROM `ogilvy-42-staging`.intelligence_42_core.posts",
        "SELECT * FROM `bigquery-public-data.intelligence_42_agent.v_items_today`",
    ],
)
def test_check_sql_refuses_wrong_project(sql):
    with pytest.raises(Refused):
        check_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT post_id, text FROM intelligence_42_core.posts WHERE market = @market LIMIT 10",
        (
            "WITH today AS (SELECT item_id, label FROM intelligence_42_agent.v_items_today), "
            "ev AS (SELECT item_id, COUNT(*) AS n FROM intelligence_42_core.posts GROUP BY item_id) "
            "SELECT t.label, e.n FROM today t JOIN ev e ON t.item_id = e.item_id"
        ),
        "SELECT * FROM intelligence_42_agent.tvf_search_posts('amapiano', 'ZA', DATE '2026-09-01', CURRENT_DATE(), 20)",
        "SELECT * FROM intelligence_42_agent.tvf_item_timeseries(@item_id, 'ZA', 30)",
        "SELECT * FROM `ogilvy-trends-v2.intelligence_42_core.posts`",
        "SELECT * FROM `ogilvy-trends-v2`.intelligence_42_core.posts",
        "SELECT market FROM intelligence_42_core.posts UNION DISTINCT SELECT market FROM intelligence_42_agent.v_cross_market",
        "SELECT p.post_id, tag FROM intelligence_42_core.posts AS p, UNNEST(p.tags) AS tag",
        "SELECT 1 AS x",
        "SELECT * FROM intelligence_42_core.posts;",
    ],
)
def test_check_sql_accepts_read_only_queries(sql):
    assert check_sql(sql) is None


MODEL_AND_EXTERNAL_CALLS = {
    "ai_generate_select": (
        "SELECT AI.GENERATE(CONCAT('summarise ', p.text), endpoint => 'gemini-2.5-pro').result "
        "FROM intelligence_42_core.posts p"
    ),
    "ai_if_where": (
        "SELECT p.post_id FROM intelligence_42_core.posts p "
        "WHERE AI.IF(('is this about food', p.text), connection_id => 'us.f42-vertex')"
    ),
    "ai_score": "SELECT AI.SCORE(('rate this', p.text)) AS s FROM intelligence_42_core.posts p",
    "ai_classify": "SELECT AI.CLASSIFY(p.text, ['music', 'food']) AS c FROM intelligence_42_core.posts p",
    "ai_embed": "SELECT AI.EMBED(p.text) AS e FROM intelligence_42_core.posts p",
    "ai_generate_pipe": "FROM intelligence_42_core.posts |> EXTEND AI.GENERATE(text) AS g",
    "ai_generate_no_table": "SELECT AI.GENERATE('write a trend', connection_id => 'us.f42-vertex')",
    "ai_lowercase": "SELECT ai.generate(p.text) FROM intelligence_42_core.posts p",
    "ai_backticked": "SELECT `AI.GENERATE`(p.text) FROM intelligence_42_core.posts p",
    "ai_other": "SELECT AI.SIMILARITY(p.text, 'braai') FROM intelligence_42_core.posts p",
    "obj_access_url": "SELECT OBJ.GET_ACCESS_URL(OBJ.MAKE_REF('gs://b/a.jpg', 'us.f42-vertex'), 'rw')",
    "ml_generate_text": (
        "SELECT ML.GENERATE_TEXT(MODEL intelligence_42_core.m, (SELECT p.text AS prompt FROM intelligence_42_core.posts p))"
    ),
    "ml_generate_text_from": (
        "SELECT * FROM ML.GENERATE_TEXT(MODEL intelligence_42_core.m, (SELECT text AS prompt FROM intelligence_42_core.posts))"
    ),
    "keys_new_keyset": "SELECT KEYS.NEW_KEYSET('AEAD_AES_GCM_256')",
    "aead_encrypt": "SELECT AEAD.ENCRYPT(k, p.text, '') FROM intelligence_42_core.posts p, intelligence_42_core.k k",
    "deterministic_encrypt": "SELECT DETERMINISTIC_ENCRYPT(k, p.text, '') FROM intelligence_42_core.posts p",
    "external_query_select": "SELECT EXTERNAL_QUERY('us.conn', 'SELECT 1')",
    "external_query_where": (
        "SELECT p.post_id FROM intelligence_42_core.posts p WHERE p.post_id IN (SELECT id FROM EXTERNAL_QUERY('c', 's'))"
    ),
    "external_object_transform": "SELECT EXTERNAL_OBJECT_TRANSFORM('intelligence_42_core.objs', 'SIGNED_URL')",
    "vector_search_select": (
        "SELECT VECTOR_SEARCH(TABLE intelligence_42_core.e, 'emb', (SELECT [1.0] AS emb))"
    ),
    "vector_search_from": (
        "SELECT * FROM VECTOR_SEARCH(TABLE intelligence_42_core.e, 'emb', (SELECT [1.0] AS emb))"
    ),
    "safe_wraps_external": "SELECT SAFE.EXTERNAL_QUERY('c', 's')",
    "persistent_udf": "SELECT intelligence_42_core.my_udf(p.text) FROM intelligence_42_core.posts p",
    "project_udf": "SELECT `ogilvy-trends-v2`.intelligence_42_core.f(1)",
    "other_namespace": "SELECT s.f.g() FROM intelligence_42_core.posts s",
    "in_cte": (
        "WITH g AS (SELECT AI.GENERATE(p.text) AS out FROM intelligence_42_core.posts p) SELECT * FROM g"
    ),
    "in_subquery": (
        "SELECT * FROM intelligence_42_core.posts WHERE post_id IN "
        "(SELECT post_id FROM intelligence_42_core.posts q WHERE AI.IF(q.text, connection_id => 'c'))"
    ),
    "in_function_argument": "SELECT CONCAT('x', AI.GENERATE(p.text).result) FROM intelligence_42_core.posts p",
    "in_window": (
        "SELECT ROW_NUMBER() OVER (PARTITION BY AI.GENERATE(p.text).result ORDER BY p.post_id) "
        "FROM intelligence_42_core.posts p"
    ),
    "unknown_plain_function": "SELECT SESSION_USER() FROM intelligence_42_core.posts",
}


@pytest.mark.parametrize("kind", sorted(MODEL_AND_EXTERNAL_CALLS))
def test_check_sql_refuses_model_and_external_functions(kind):
    with pytest.raises(Refused):
        check_sql(MODEL_AND_EXTERNAL_CALLS[kind])


@pytest.mark.parametrize("kind", ["ai_generate_select", "ai_generate_pipe", "obj_access_url", "external_query_select"])
def test_sql_query_refuses_model_calls_before_dry_run(ctx, kind):
    wh = FakeWarehouse()
    with pytest.raises(Refused):
        sql_query(ctx, wh, MODEL_AND_EXTERNAL_CALLS[kind], purpose="bad")
    assert wh.dry_runs == [] and wh.runs == []


@pytest.mark.parametrize(
    "sql",
    [
        (
            "SELECT COUNT(*), COUNTIF(p.views > 10), APPROX_COUNT_DISTINCT(p.creator_id), APPROX_QUANTILES(p.views, 4), "
            "SUM(p.views), AVG(p.likes), STRING_AGG(p.text, ' '), ARRAY_AGG(p.post_id LIMIT 5) "
            "FROM intelligence_42_core.posts p"
        ),
        (
            "SELECT LOWER(p.text), CONCAT(p.platform, '/', p.post_id), SUBSTR(p.text, 1, 20), REGEXP_CONTAINS(p.text, r'braai'), "
            "REGEXP_EXTRACT(p.text, r'#(\\w+)'), CONTAINS_SUBSTR(p.text, 'amapiano'), SEARCH(p.text, 'amapiano'), "
            "JSON_VALUE(p.raw, '$.id'), JSON_QUERY(p.raw, '$.a'), SAFE_CAST(p.views AS INT64), SAFE_DIVIDE(p.likes, p.views) "
            "FROM intelligence_42_core.posts p"
        ),
        (
            "SELECT DATE_TRUNC(p.post_date, WEEK), DATE_DIFF(CURRENT_DATE(), p.post_date, DAY), "
            "FORMAT_DATE('%F', p.post_date), TIMESTAMP_TRUNC(p.published_at, HOUR), EXTRACT(DAYOFWEEK FROM p.post_date), "
            "ROUND(LOG(1 + p.views), 2), ABS(p.likes - p.comments), GREATEST(p.likes, 0), IF(p.views > 0, 1, 0), "
            "IFNULL(p.shares, 0), COALESCE(p.shares, 0), CASE WHEN p.views > 0 THEN 'a' ELSE 'b' END "
            "FROM intelligence_42_core.posts p"
        ),
        (
            "SELECT ROW_NUMBER() OVER (PARTITION BY p.platform ORDER BY p.views DESC), "
            "LAG(p.views) OVER (ORDER BY p.post_date), RANK() OVER (ORDER BY p.views), "
            "ARRAY(SELECT x FROM UNNEST(p.tags) x), STRUCT(p.post_id AS id, p.views AS v), ARRAY_LENGTH(p.tags) "
            "FROM intelligence_42_core.posts p WHERE EXISTS (SELECT 1 FROM UNNEST(p.tags) t WHERE t = 'braai')"
        ),
        "SELECT SAFE.PARSE_DATE('%Y%m%d', p.day), NET.HOST(p.url), HLL_COUNT.MERGE(p.sketch) FROM intelligence_42_core.posts p",
        "SELECT * FROM intelligence_42_agent.tvf_search_posts('amapiano', NULL, @since, @until, 20)",
        "SELECT * FROM `ogilvy-trends-v2`.intelligence_42_agent.tvf_search_posts(@q, @market, @since, @until, @k)",
    ],
)
def test_check_sql_accepts_plain_builtin_functions(sql):
    assert check_sql(sql) is None


def test_allowed_tvfs_are_the_two_agent_table_functions():
    assert ALLOWED_TVFS == {"intelligence_42_agent.tvf_search_posts", "intelligence_42_agent.tvf_item_timeseries"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM intelligence_42_agent.tvf_search_items('amapiano')",
        "SELECT * FROM `ogilvy-trends-v2`.intelligence_42_agent.tvf_search_items(@q, @market, @since, @until, @k)",
    ],
)
def test_check_sql_refuses_the_broken_vector_search_tvf(sql):
    # tvf_search_items exists on staging with a VECTOR_SEARCH body that cannot run; it is left in place, never used.
    with pytest.raises(Refused, match="tvf_search_items"):
        check_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM intelligence_42_agent.other_fn('amapiano')",
        "SELECT * FROM `ogilvy-trends-v2`.intelligence_42_agent.tvf_other(@q)",
        "SELECT * FROM intelligence_42_core.tvf_search_posts('amapiano')",
        "SELECT * FROM intelligence_42_core.tvf_item_timeseries(@item_id, 'ZA', 30)",
        "SELECT * FROM intelligence_42_agent.TVF_SEARCH_POSTS('amapiano')",
        (
            "SELECT * FROM intelligence_42_core.posts p "
            "JOIN intelligence_42_agent.read_everything(1) r ON p.post_id = r.post_id"
        ),
    ],
)
def test_check_sql_refuses_table_functions_off_the_allowlist(sql):
    with pytest.raises(Refused):
        check_sql(sql)


def test_check_sql_checks_the_function_after_safe():
    with pytest.raises(Refused):
        check_sql("SELECT SAFE.DETERMINISTIC_DECRYPT_STRING(k, c, '') FROM intelligence_42_core.posts")


def test_sql_query_runs_and_records(ctx):
    wh = FakeWarehouse(rows=[{"post_id": "p1", "n": 3}, {"post_id": "p2", "n": 1}], bytes_=12_345)
    sql = "SELECT post_id, COUNT(*) AS n FROM intelligence_42_core.posts WHERE market = @market GROUP BY post_id"
    out = sql_query(ctx, wh, sql, purpose="count posts", params={"market": "ZA"})

    assert out["rows"] == [{"post_id": "p1", "n": 3}, {"post_id": "p2", "n": 1}]
    assert out["bytes"] == 12_345
    assert out["truncated"] is False
    qid = out["query_id"]
    assert qid in ctx.queries
    recorded = ctx.queries[qid]
    assert recorded["sql"] == sql
    assert recorded["params"] == {"market": "ZA"}
    assert recorded["purpose"] == "count posts"
    assert recorded["result_hash"] == out["result_hash"]
    assert out["result_hash"] == result_hash(out["rows"])
    assert ctx.events[-1] == {"step": "sql", "query_id": qid, "purpose": "count posts"}
    assert wh.dry_runs == [(sql, {"market": "ZA"})]
    assert wh.runs == [(sql, {"market": "ZA"}, MAX_BYTES_BILLED)]


def test_sql_query_ids_are_distinct(ctx):
    wh = FakeWarehouse()
    a = sql_query(ctx, wh, "SELECT * FROM intelligence_42_core.posts", purpose="a")
    b = sql_query(ctx, wh, "SELECT * FROM intelligence_42_core.posts", purpose="b")
    assert a["query_id"] != b["query_id"]
    assert set(ctx.queries) == {a["query_id"], b["query_id"]}


def test_result_hash_stable_across_key_order():
    ctx_a = RunContext(run_id="a", tier="T0", as_of=datetime(2026, 9, 28))
    ctx_b = RunContext(run_id="b", tier="T0", as_of=datetime(2026, 9, 28))
    a = sql_query(ctx_a, FakeWarehouse(rows=[{"post_id": "p1", "n": 3, "market": "ZA"}]), "SELECT 1 AS x", purpose="x")
    b = sql_query(ctx_b, FakeWarehouse(rows=[{"market": "ZA", "n": 3, "post_id": "p1"}]), "SELECT 1 AS x", purpose="x")
    assert a["result_hash"] == b["result_hash"]
    assert ctx_a.queries[a["query_id"]]["result_hash"] == ctx_b.queries[b["query_id"]]["result_hash"]


def test_sql_query_caps_rows_but_hashes_all(ctx):
    rows = [{"i": i} for i in range(750)]
    wh = FakeWarehouse(rows=rows)
    out = sql_query(ctx, wh, "SELECT i FROM intelligence_42_core.posts", purpose="many")
    assert len(out["rows"]) == 500
    assert out["rows"] == rows[:500]
    assert out["truncated"] is True
    assert out["result_hash"] == result_hash(rows)
    assert out["result_hash"] != result_hash(rows[:500])
    assert ctx.queries[out["query_id"]]["result_hash"] == result_hash(rows)


def test_sql_query_exactly_500_rows_not_truncated(ctx):
    out = sql_query(ctx, FakeWarehouse(rows=[{"i": i} for i in range(500)]), "SELECT 1 AS i", purpose="edge")
    assert len(out["rows"]) == 500
    assert out["truncated"] is False


@pytest.mark.parametrize("kind", ["insert", "delete", "drop", "declare", "unparseable"])
def test_sql_query_refusal_never_touches_warehouse(ctx, kind):
    wh = FakeWarehouse()
    with pytest.raises(Refused):
        sql_query(ctx, wh, REFUSED_STATEMENTS[kind], purpose="bad")
    assert wh.dry_runs == []
    assert wh.runs == []
    assert ctx.queries == {}


def test_sql_query_refuses_unqualified_before_dry_run(ctx):
    wh = FakeWarehouse()
    with pytest.raises(Refused):
        sql_query(ctx, wh, "SELECT * FROM posts", purpose="bad")
    assert wh.dry_runs == [] and wh.runs == []


@pytest.mark.parametrize(
    "tables",
    [
        ["trends_v2_dev.posts"],
        ["intelligence_42_core.posts", "trends_v2_dev.posts"],
        ["other-project.intelligence_42_core.posts"],
        ["posts"],
    ],
)
def test_sql_query_refuses_dry_run_tables_outside_allowlist(ctx, tables):
    wh = FakeWarehouse(tables=tables)
    with pytest.raises(Refused):
        sql_query(ctx, wh, "SELECT * FROM intelligence_42_agent.v_items_today", purpose="view")
    assert len(wh.dry_runs) == 1
    assert wh.runs == []
    assert ctx.queries == {}


def test_sql_query_accepts_dry_run_tables_in_allowlist(ctx):
    wh = FakeWarehouse(tables=["intelligence_42_core.posts", "ogilvy-trends-v2.intelligence_42_agent.findings"])
    sql_query(ctx, wh, "SELECT * FROM intelligence_42_agent.v_items_today", purpose="view")
    assert len(wh.runs) == 1


def test_sql_query_refuses_bytes_over_default_cap(ctx):
    wh = FakeWarehouse(bytes_=MAX_BYTES_BILLED + 1)
    with pytest.raises(Refused):
        sql_query(ctx, wh, "SELECT * FROM intelligence_42_core.posts", purpose="big")
    assert wh.runs == []
    assert ctx.queries == {}


def test_sql_query_bytes_at_cap_runs(ctx):
    wh = FakeWarehouse(bytes_=MAX_BYTES_BILLED)
    sql_query(ctx, wh, "SELECT * FROM intelligence_42_core.posts", purpose="edge")
    assert len(wh.runs) == 1


def test_sql_query_refuses_bytes_over_lower_cap(ctx):
    wh = FakeWarehouse(bytes_=5_000)
    with pytest.raises(Refused):
        sql_query(ctx, wh, "SELECT * FROM intelligence_42_core.posts", purpose="big", max_bytes_billed=4_000)
    assert wh.runs == []


def test_sql_query_lower_cap_passed_to_run(ctx):
    wh = FakeWarehouse(bytes_=1_000)
    sql_query(ctx, wh, "SELECT * FROM intelligence_42_core.posts", purpose="small", max_bytes_billed=4_000)
    assert wh.runs[0][2] == 4_000


def test_sql_query_cap_never_raised_above_max(ctx):
    wh = FakeWarehouse(bytes_=MAX_BYTES_BILLED + 1)
    with pytest.raises(Refused):
        sql_query(ctx, wh, "SELECT * FROM intelligence_42_core.posts", purpose="big", max_bytes_billed=10 * MAX_BYTES_BILLED)
    assert wh.runs == []

    wh = FakeWarehouse(bytes_=10)
    sql_query(ctx, wh, "SELECT * FROM intelligence_42_core.posts", purpose="ok", max_bytes_billed=10 * MAX_BYTES_BILLED)
    assert wh.runs[0][2] == MAX_BYTES_BILLED


def test_bigquery_warehouse_builds_without_credentials():
    wh = BigQueryWarehouse()
    assert wh.project == "ogilvy-trends-v2"
    assert wh._client is None


class _FakeJob:
    def __init__(self, tables, bytes_=1_000, rows=(), results=None):
        self.referenced_tables = [SimpleNamespace(project=p, dataset_id=d, table_id=t) for p, d, t in tables]
        self.total_bytes_processed = bytes_
        self.rows = list(rows)
        self.results = results if results is not None else []

    def result(self, max_results=None):
        # Like BigQuery: max_results caps the rows fetched from the server.
        self.results.append(max_results)
        return self.rows if max_results is None else self.rows[:max_results]


class _FakeBQClient:
    def __init__(self, tables, rows=()):
        self.tables = tables
        self.rows = list(rows)
        self.queries = []
        self.max_results = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return _FakeJob(self.tables, rows=self.rows, results=self.max_results)


def _bq_warehouse(tables, rows=()):
    wh = BigQueryWarehouse()
    wh._client = _FakeBQClient(tables, rows)
    return wh


def test_bigquery_dry_run_names_the_project():
    wh = _bq_warehouse([("ogilvy-trends-v2", "intelligence_42_core", "posts"),
                        ("other-project", "intelligence_42_core", "posts")])
    assert wh.dry_run("SELECT 1 AS x", None)["tables"] == [
        "ogilvy-trends-v2.intelligence_42_core.posts", "other-project.intelligence_42_core.posts"]


def test_sql_query_refuses_dry_run_table_in_another_project(ctx):
    # A view in an allowed dataset that reads the same dataset name in another project.
    wh = _bq_warehouse([("other-project", "intelligence_42_core", "posts")])
    with pytest.raises(Refused):
        sql_query(ctx, wh, "SELECT * FROM intelligence_42_agent.v_items_today", purpose="view")
    assert len(wh._client.queries) == 1
    assert ctx.queries == {}


def test_sql_query_accepts_dry_run_table_in_own_project(ctx):
    wh = _bq_warehouse([("ogilvy-trends-v2", "intelligence_42_core", "posts")])
    sql_query(ctx, wh, "SELECT * FROM intelligence_42_agent.v_items_today", purpose="view")
    assert len(wh._client.queries) == 2


def test_bigquery_run_fetches_at_most_one_row_past_the_cap(ctx):
    rows = [{"i": i} for i in range(750)]
    wh = _bq_warehouse([("ogilvy-trends-v2", "intelligence_42_core", "posts")], rows)
    out = sql_query(ctx, wh, "SELECT i FROM intelligence_42_core.posts", purpose="many")
    assert wh._client.max_results == [MAX_ROWS + 1]
    assert out["rows"] == rows[:MAX_ROWS]
    assert out["truncated"] is True
    returned = rows[:MAX_ROWS + 1]
    assert out["result_hash"] == result_hash(returned)
    assert ctx.queries[out["query_id"]]["rows"] == returned


def test_bigquery_run_under_the_cap_is_not_truncated(ctx):
    rows = [{"i": i} for i in range(MAX_ROWS)]
    wh = _bq_warehouse([("ogilvy-trends-v2", "intelligence_42_core", "posts")], rows)
    out = sql_query(ctx, wh, "SELECT i FROM intelligence_42_core.posts", purpose="edge")
    assert wh._client.max_results == [MAX_ROWS + 1]
    assert out["rows"] == rows and out["truncated"] is False
    assert out["result_hash"] == result_hash(rows)


# Forecasts stay hidden from the agent until weekly_score says they beat persistence (TRUST.md K9). Only
# log_forecast's own dedup read touches the forecasts table, through its own path.
HIDDEN_FORECAST_SQL = [
    "SELECT * FROM intelligence_42_agent.forecasts",
    "SELECT * FROM intelligence_42_core.v_forecasts_current",
    "SELECT * FROM intelligence_42_agent.forecast_score",
    "SELECT f.prob FROM intelligence_42_agent.forecasts AS f",
    "SELECT f.prob FROM intelligence_42_agent.forecasts f WHERE f.item_id = 'x'",
    "SELECT * FROM `ogilvy-trends-v2.intelligence_42_agent.forecasts`",
    "SELECT * FROM `ogilvy-trends-v2`.intelligence_42_agent.forecasts",
    "SELECT * FROM `intelligence_42_agent`.`forecasts`",
    "SELECT * FROM `intelligence_42_core.v_forecasts_current`",
    "SELECT * FROM intelligence_42_agent.Forecasts",
    "SELECT * FROM intelligence_42_core.V_FORECASTS_CURRENT",
    "SELECT * FROM intelligence_42_agent.forecast*",
    "SELECT * FROM `intelligence_42_core.v_forecast*`",
    "WITH f AS (SELECT * FROM intelligence_42_agent.forecasts) SELECT * FROM f",
    "WITH forecasts AS (SELECT 1 AS x) SELECT * FROM intelligence_42_agent.forecasts",
    "SELECT * FROM intelligence_42_core.posts WHERE post_id IN "
    "(SELECT item_id FROM intelligence_42_core.v_forecasts_current)",
    "SELECT * FROM intelligence_42_core.posts p JOIN intelligence_42_agent.forecasts f ON f.item_id = p.post_id",
    "SELECT * FROM intelligence_42_core.posts UNION ALL SELECT * FROM intelligence_42_agent.forecasts",
    "SELECT (SELECT MAX(prob) FROM intelligence_42_agent.forecasts) AS p",
    "SELECT EXISTS (SELECT 1 FROM intelligence_42_core.v_forecasts_current) AS e",
]


@pytest.mark.parametrize("sql", HIDDEN_FORECAST_SQL)
def test_check_sql_refuses_forecast_tables(sql):
    with pytest.raises(Refused) as err:
        check_sql(sql)
    assert "forecast" in str(err.value).lower() and "hidden" in str(err.value).lower()


@pytest.mark.parametrize("sql", ["SELECT * FROM forecasts", "SELECT * FROM v_forecasts_current"])
def test_check_sql_refuses_unqualified_forecast_names(sql):
    with pytest.raises(Refused):
        check_sql(sql)


@pytest.mark.parametrize("sql", HIDDEN_FORECAST_SQL[:3])
def test_sql_query_refuses_forecast_tables_before_dry_run(ctx, sql):
    wh = FakeWarehouse()
    with pytest.raises(Refused):
        sql_query(ctx, wh, sql, purpose="peek")
    assert wh.dry_runs == [] and wh.runs == [] and ctx.queries == {}


@pytest.mark.parametrize(
    "tables",
    [
        ["ogilvy-trends-v2.intelligence_42_agent.forecasts"],
        ["intelligence_42_core.posts", "intelligence_42_agent.forecasts"],
        ["ogilvy-trends-v2.intelligence_42_core.v_forecasts_current"],
        ["ogilvy-trends-v2.intelligence_42_agent.forecast_score"],
    ],
)
def test_sql_query_refuses_forecast_tables_named_by_the_dry_run(ctx, tables):
    # A view in an allowed dataset that reads the forecasts underneath.
    wh = FakeWarehouse(tables=tables)
    with pytest.raises(Refused) as err:
        sql_query(ctx, wh, "SELECT * FROM intelligence_42_agent.v_items_today", purpose="view")
    assert "hidden" in str(err.value).lower()
    assert wh.runs == [] and ctx.queries == {}


@pytest.mark.parametrize(
    "sql",
    [
        "WITH forecasts AS (SELECT post_id FROM intelligence_42_core.posts) SELECT * FROM forecasts",
        "SELECT forecast_id FROM intelligence_42_core.posts",
        "SELECT * FROM intelligence_42_core.posts AS forecasts",
    ],
)
def test_check_sql_still_accepts_the_word_forecast_elsewhere(sql):
    check_sql(sql)
