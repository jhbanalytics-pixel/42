"""The agent's sql_query tool and its read-only guard (AGENT.md, Tools and Guardrails; DATA.md section 7)."""

from __future__ import annotations

from datetime import date, datetime
from fnmatch import fnmatchcase
from typing import Protocol

import sqlglot
from sqlglot import exp

from core.agent.context import Refused, RunContext

PROJECT = "ogilvy-trends-v2"
ALLOWED_DATASETS = ("intelligence_42_core", "intelligence_42_agent")
ALLOWED_TVFS = {"intelligence_42_agent.tvf_search_posts", "intelligence_42_agent.tvf_item_timeseries"}  # DATA.md 7
MAX_BYTES_BILLED = 2_000_000_000
MAX_ROWS = 500
QUERY_PREVIEW_ROWS = 10
QUERY_PAGE_ROWS = 50
# Forecasts stay hidden from the agent until weekly_score says they beat persistence (TRUST.md K9). log_forecast's
# dedup read is the one query allowed to name the forecasts table, through _run_query.
HIDDEN_TABLES = (
    "intelligence_42_agent.forecasts",
    "intelligence_42_core.v_forecasts_current",
    "intelligence_42_agent.forecast_score",
)
# The tables and columns the model is told about in the tool description (core/schema/core.sql). Without them it
# guessed names ("items", "detection tables") and every query failed (live staging, 4 October).
WAREHOUSE_MAP = {
    "intelligence_42_core.posts": (
        "post_id", "platform", "creator_id", "text", "hashtags", "sound_id", "published_at", "post_date", "views",
        "likes", "comments", "shares", "engagement", "geo_market", "geo_confidence"),
    "intelligence_42_core.post_items": ("post_id", "item_id", "via"),
    "intelligence_42_core.cultural_map": (
        "item_id", "kind", "canonical_key", "label", "aliases", "first_seen", "last_seen", "lifecycle", "status",
        "valid_to"),
    "intelligence_42_core.item_daily": (
        "metric_date", "market", "platform", "item_id", "lane_class", "posts", "creators", "engagement",
        "local_posts"),
    "intelligence_42_core.item_state": (
        "metric_date", "market", "item_id", "kind", "state", "main_ratio", "posts3", "creators3", "local_share",
        "spread_platforms", "eligible"),
    "intelligence_42_core.post_enrichment": ("post_id", "sounds", "formats", "langs", "entities", "tone"),
    "intelligence_42_core.creators": ("creator_id", "platform", "handle", "followers", "tier", "home_market"),
    "intelligence_42_core.post_observations": (
        "post_id", "observed_date", "market", "platform", "route", "lane_class", "rank"),
    "intelligence_42_core.google_search_signals": ("market", "term", "source", "kind", "rank", "refreshed_at",
                                                   "fetched_at"),
    "intelligence_42_agent.tvf_item_timeseries(item_id, market, days)": (
        "series_id", "platform", "series", "day", "value"),
}
WAREHOUSE_NOTES = (
    "posts is partitioned by post_date: always filter on it. geo_market is ZA, NG or KE when a post is located. "
    "Params arrive as strings: wrap a date param in DATE(@name). cultural_map keeps one row per item version: the "
    "current row has valid_to IS NULL. Item kinds: topic, "
    "hashtag, sound, format, meme, creator, brand, event. A TikTok sound is posts.sound_id, or an item of kind sound "
    "linked to posts through post_items. A sound's title is the label of its current cultural_map row with kind "
    "'sound' and canonical_key = CONCAT(p.platform, ':', p.sound_id); no row, or a label equal to the sound id, means "
    "42 holds no title for it, so say so. hashtags is an array: count tags with CROSS JOIN UNNEST(p.hashtags) AS tag. item_state and item_daily hold "
    "one row per item, market and day. Measure reach across the whole store: COUNT(*) and COUNT(DISTINCT "
    "p.creator_id) grouped by p.platform, over every platform unless the question names one. "
    "google_search_signals holds the Google search terms collect stored per market each day (source google_trending, "
    "SocialCrawl trending searches; google_bq, the public Google Trends top and rising terms, ZA and NG; or google_rss, "
    "Google's daily trending searches feed, from 4 October 2026; partitioned by DATE(fetched_at)). Read it before "
    "paying for a live Google Trends call. It is "
    "search interest: context to steer which posts to search, never evidence. A query that reads it gets no "
    "query_id, so no claim or number can cite it. "
)
# Google search interest collect stored. Readable as context to steer searches, never as evidence (culture-read skill,
# rule 8), so a query naming it is not recorded and its rows can pin no number.
SEARCH_SIGNAL_TABLES = ("intelligence_42_core.google_search_signals",)
WAREHOUSE_EXAMPLE = (
    "SELECT p.sound_id, ANY_VALUE(IF(m.label = p.sound_id, NULL, m.label)) AS sound_title, COUNT(*) AS posts, "
    "COUNT(DISTINCT p.creator_id) AS creators FROM intelligence_42_core.posts p "
    "LEFT JOIN intelligence_42_core.cultural_map m ON m.kind = 'sound' AND m.valid_to IS NULL "
    "AND m.canonical_key = CONCAT(p.platform, ':', p.sound_id) "
    "WHERE p.platform = 'tiktok' AND p.geo_market = @market "
    "AND p.post_date BETWEEN DATE(@since) AND DATE(@until) AND p.sound_id IS NOT NULL "
    "GROUP BY p.sound_id ORDER BY creators DESC LIMIT 50"
)


def warehouse_map_text() -> str:
    """The tables, notes and one example for the sql_query tool description."""
    tables = "; ".join(f"{name} ({', '.join(cols)})" for name, cols in WAREHOUSE_MAP.items())
    return f"Tables: {tables}. {WAREHOUSE_NOTES}Example: {WAREHOUSE_EXAMPLE}"

# Nodes that change data, schema, permissions or session state. A query root is required anyway;
# this walk also catches any of them nested inside one.
_WRITE_NODES = (
    exp.DML, exp.DDL, exp.Drop, exp.Alter, exp.Command, exp.TruncateTable, exp.Export, exp.Grant,
    exp.Set, exp.Declare, exp.Transaction, exp.Commit, exp.Rollback, exp.Use, exp.LoadData,
)

# Function allowlist. sqlglot types the plain built-ins it knows into these families; everything in its catch-all
# module is refused unless named below, which keeps out AI.*, ML.*, VECTOR_SEARCH, READ_* and session functions.
_FUNCTION_FAMILIES = {f"sqlglot.expressions.{m}" for m in ("aggregate", "array", "json", "math", "query", "string",
                                                          "temporal")}
_GENERAL_FUNCTIONS = {
    exp.And, exp.Or, exp.Xor, exp.ApproxDistinct, exp.Hll, exp.Pow, exp.RegexpLike, exp.SafeFunc, exp.Typeof,
    exp.Case, exp.Cast, exp.CastToStrType, exp.Coalesce, exp.Collate, exp.Convert, exp.DecodeCase, exp.EqualNull,
    exp.Exists, exp.Float64, exp.Greatest, exp.Host, exp.If, exp.Int64, exp.IsArray, exp.JSONCast, exp.LaxBool,
    exp.LaxFloat64, exp.LaxInt64, exp.LaxString, exp.Least, exp.NetFunc, exp.Nullif, exp.Nvl2, exp.ParseIp, exp.Rand,
    exp.RangeBucket, exp.RegDomain, exp.TryCast, exp.Uuid, exp.WeekStart,
}
# Built-ins sqlglot leaves untyped for BigQuery.
_PLAIN_NAMES = {"SEARCH", "CONTAINS_SUBSTR", "IEEE_DIVIDE"}
_PLAIN_PREFIXES = ("JSON_", "REGEXP_", "APPROX_", "SAFE_", "ARRAY_")
_NAMESPACES = {"SAFE", "NET", "HLL_COUNT"}
_NEVER = ("EXTERNAL_QUERY", "EXTERNAL_OBJECT_TRANSFORM", "VECTOR_SEARCH", "DETERMINISTIC_")


def _namespace(dot: exp.Dot) -> str:
    return dot.this.sql(dialect="bigquery").replace("`", "").upper()


def _name(node: exp.Func) -> str:
    if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
        return node.name.upper()
    return node.sql(dialect="bigquery").split("(", 1)[0].strip().upper()


def _check_function(node: exp.Func) -> None:
    parent = node.parent
    if isinstance(parent, exp.Dot) and parent.expression is node:
        namespace = _namespace(parent)
        shown = f"{namespace}.{_name(node)}"
        if namespace not in _NAMESPACES:
            raise Refused(f"Function {shown} is not allowed. Only plain built-in functions and the SAFE., NET. and "
                          f"HLL_COUNT. families may be used.")
        if namespace != "SAFE":
            return
    if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
        name = _name(node)
        if name.startswith(_NEVER):
            raise Refused(f"Function {name} is not allowed.")
        if "." in name:
            raise Refused(f"Function {name} is not allowed. Only plain built-in functions and the SAFE., NET. and "
                          f"HLL_COUNT. families may be used.")
        if isinstance(parent, exp.Table) and parent.this is node:
            return  # a table function; _check_table holds it to ALLOWED_TVFS
        if not (name in _PLAIN_NAMES or name.startswith(_PLAIN_PREFIXES)):
            raise Refused(f"Function {name} is not on the agent's allowlist of built-in functions.")
        return
    cls = type(node)
    name = _name(node)
    if name.startswith(_NEVER) or cls.__name__.startswith(("AI", "ML")):
        raise Refused(f"Function {name} is not allowed.")
    if cls.__module__ not in _FUNCTION_FAMILIES and cls not in _GENERAL_FUNCTIONS:
        raise Refused(f"Function {name} is not on the agent's allowlist of built-in functions.")


def _hidden(dataset: str, name: str, allowed: tuple = ()) -> bool:
    """True when dataset.name is a hidden table, matched without case and through a wildcard or decorator."""
    ref = f"{dataset}.{name.split('$', 1)[0]}".casefold()
    return any(fnmatchcase(hidden, ref) for hidden in HIDDEN_TABLES if hidden not in allowed)


def _refuse_hidden(shown: str) -> None:
    raise Refused(f"Table {shown} is not available to the agent: forecasts stay hidden until they beat persistence.")


def check_sql(sql: str, hidden_ok: tuple = ()) -> None:
    """Refuse anything but one read-only query over the allowlisted datasets, outside the hidden tables."""
    if not sql or not sql.strip():
        raise Refused("Empty SQL.")
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="bigquery") if s is not None]
    except sqlglot.errors.SqlglotError as e:
        raise Refused(f"The SQL could not be parsed, so it is refused: {str(e).splitlines()[0]}") from None
    if len(statements) != 1:
        raise Refused(f"Exactly one statement is allowed; found {len(statements)}.")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise Refused(f"Only read-only queries (SELECT or WITH) are allowed; this statement parses as {type(tree).__name__}.")
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            raise Refused(f"Only read-only queries are allowed; found {type(node).__name__} inside the query.")
        if isinstance(node, exp.Func):
            _check_function(node)

    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        _check_table(table, cte_names, hidden_ok)


def _check_table(table: exp.Table, cte_names: set, hidden_ok: tuple = ()) -> None:
    parts = [p.name for p in table.parts]
    shown = ".".join(parts) or table.sql(dialect="bigquery")
    if any("INFORMATION_SCHEMA" in p.upper() for p in parts):
        raise Refused(f"INFORMATION_SCHEMA is not available to the agent ({shown}).")
    if not table.db:
        if not table.catalog and table.name and table.name in cte_names:
            return
        raise Refused(
            f"Table {shown} must be dataset-qualified, for example intelligence_42_core.posts. "
            f"Allowed datasets: {', '.join(ALLOWED_DATASETS)}."
        )
    if len(parts) > 3:
        raise Refused(f"Table reference {shown} has too many parts.")
    if table.catalog and table.catalog != PROJECT:
        raise Refused(f"Only project {PROJECT} is allowed; {shown} names project {table.catalog}.")
    if table.db not in ALLOWED_DATASETS:
        raise Refused(f"Dataset {table.db} is not allowed. Allowed datasets: {', '.join(ALLOWED_DATASETS)}.")
    if _hidden(table.db, table.name, hidden_ok):
        _refuse_hidden(shown)
    if isinstance(table.this, exp.Func) and f"{table.db}.{table.this.name}" not in ALLOWED_TVFS:
        raise Refused(f"Table function {shown} is not allowed. Allowed: {', '.join(sorted(ALLOWED_TVFS))}.")


def _check_dry_run_table(ref: str, hidden_ok: tuple = ()) -> None:
    parts = ref.split(".")
    if len(parts) == 3:
        if parts[0] != PROJECT:
            raise Refused(f"The dry run shows the query reads {ref}, which is outside project {PROJECT}.")
        parts = parts[1:]
    if len(parts) != 2 or parts[0] not in ALLOWED_DATASETS:
        raise Refused(f"The dry run shows the query reads {ref}, which is outside {', '.join(ALLOWED_DATASETS)}.")
    if _hidden(parts[0], parts[1], hidden_ok):
        raise Refused(f"The dry run shows the query reads {ref}, which is not available to the agent: forecasts "
                      f"stay hidden until they beat persistence.")


def _reads_search_signals(refs) -> bool:
    """True when the dry run shows the query reads a search-interest table."""
    return any(ref.split(".", ref.count(".") - 1)[-1] in SEARCH_SIGNAL_TABLES for ref in refs or ())


class Warehouse(Protocol):
    def dry_run(self, sql: str, params: dict | None) -> dict: ...

    def run(self, sql: str, params: dict | None, max_bytes_billed: int) -> list[dict]: ...


class BigQueryWarehouse:
    """google-cloud-bigquery behind the Warehouse protocol. The client is built on first use."""

    def __init__(self, project: str = PROJECT):
        self.project = project
        self._client = None

    def _bq(self):
        from google.cloud import bigquery

        if self._client is None:
            self._client = bigquery.Client(project=self.project)
        return bigquery

    def _params(self, params: dict | None) -> list:
        bigquery = self._bq()
        out = []
        for name, value in (params or {}).items():
            if isinstance(value, bool):
                kind = "BOOL"
            elif isinstance(value, int):
                kind = "INT64"
            elif isinstance(value, float):
                kind = "FLOAT64"
            elif isinstance(value, str):
                kind = "STRING"
            elif isinstance(value, datetime):
                kind = "TIMESTAMP" if value.tzinfo else "DATETIME"
            elif isinstance(value, date):
                kind = "DATE"
            else:
                raise Refused(f"Query parameter {name} has unsupported type {type(value).__name__}.")
            out.append(bigquery.ScalarQueryParameter(name, kind, value))
        return out

    def dry_run(self, sql: str, params: dict | None) -> dict:
        bigquery = self._bq()
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=self._params(params))
        job = self._client.query(sql, job_config=config)
        tables = [f"{t.project}.{t.dataset_id}.{t.table_id}" for t in (job.referenced_tables or [])]
        return {"bytes": int(job.total_bytes_processed or 0), "tables": tables}

    def run(self, sql: str, params: dict | None, max_bytes_billed: int) -> list[dict]:
        bigquery = self._bq()
        config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed, query_parameters=self._params(params))
        semantic = False
        if "tvf_search_posts" in sql.casefold():
            try:
                statements = sqlglot.parse(sql, dialect="bigquery")
            except sqlglot.errors.SqlglotError:
                statements = []
            semantic = any(
                isinstance(table.this, exp.Func)
                and table.this.name.casefold().rsplit(".", 1)[-1] == "tvf_search_posts"
                for statement in statements if statement is not None
                for table in statement.find_all(exp.Table)
            )
        if semantic:
            job = self._client.query(sql, job_config=config, job_retry=None)
            rows = job.result(max_results=MAX_ROWS + 1, job_retry=None)
        else:
            job = self._client.query(sql, job_config=config)
            rows = job.result(max_results=MAX_ROWS + 1)
        return [dict(row.items()) for row in rows]


def sql_query(
    ctx: RunContext,
    warehouse: Warehouse,
    sql: str,
    purpose: str,
    params: dict | None = None,
    max_bytes_billed: int = MAX_BYTES_BILLED,
) -> dict:
    """Check, dry-run, run and record one read-only query. Returns at most 500 rows."""
    return _run_query(ctx, warehouse, sql, purpose, params, max_bytes_billed)


def query_rows(ctx: RunContext, query_id: str, offset: int = 0, limit: int = QUERY_PAGE_ROWS) -> dict:
    """Read a bounded page of an existing query's visible rows, without another warehouse call."""
    for name, value in (("offset", offset), ("limit", limit)):
        if not (type(value) is int or type(value) is float and value.is_integer()):
            raise Refused(f"{name} must be an integer.")
    offset, limit = int(offset), int(limit)
    if not 1 <= limit <= QUERY_PAGE_ROWS:
        raise Refused(f"limit must be between 1 and {QUERY_PAGE_ROWS}.")
    with ctx.lock:
        query = ctx.queries.get(query_id) if isinstance(query_id, str) else None
    if query is None:
        raise Refused("query_id must name a query recorded in this question.")
    recorded = query["rows"]
    rows = recorded[:MAX_ROWS]
    if not 0 <= offset <= len(rows):
        raise Refused("offset must be within this query's visible recorded rows.")
    page = rows[offset:offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < len(rows)
    return {
        "query_id": query_id,
        "rows": page,
        "row_count": len(rows),
        "recorded_row_count": len(recorded),
        "columns": sorted({column for row in rows for column in row}),
        "result_hash": query["result_hash"],
        "truncated": len(recorded) > MAX_ROWS,
        "preview": {"offset": offset, "limit": limit, "returned": len(page), "has_more": has_more,
                    "next_offset": next_offset if has_more else None},
    }


def query_preview(ctx: RunContext, result: dict) -> dict:
    """Compact the researcher's recorded result; unrecorded search-interest rows stay whole."""
    if result.get("query_id") is None:
        return result
    return {**result, **query_rows(ctx, result["query_id"], limit=QUERY_PREVIEW_ROWS)}


def _run_query(ctx: RunContext, warehouse: Warehouse, sql: str, purpose: str, params: dict | None = None,
               max_bytes_billed: int = MAX_BYTES_BILLED, hidden_ok: tuple = ()) -> dict:
    """sql_query, with hidden_ok naming the hidden tables an internal read may use. Never offered to the model."""
    check_sql(sql, hidden_ok)
    cap = min(int(max_bytes_billed), MAX_BYTES_BILLED)

    dry = warehouse.dry_run(sql, params)
    for ref in dry["tables"]:
        _check_dry_run_table(ref, hidden_ok)
    scanned = int(dry["bytes"])
    if scanned > cap:
        raise Refused(f"The query would scan {scanned:,} bytes, over the {cap:,} byte cap. Narrow it.")

    rows = warehouse.run(sql, params, cap)
    if _reads_search_signals(dry["tables"]):
        ctx.emit("search_interest", purpose=purpose)
        return {
            "rows": rows[:MAX_ROWS],
            "query_id": None,
            "signals": {"source": "Google search data", "signal_type": "search_interest"},
            "note": "Google search interest: context to steer searches, never evidence. Cite nothing from these rows.",
            "bytes": scanned,
            "truncated": len(rows) > MAX_ROWS,
        }
    query_id, digest = ctx.record_query(sql, params, rows, purpose)
    ctx.emit("sql", query_id=query_id, purpose=purpose)
    return {
        "rows": rows[:MAX_ROWS],
        "query_id": query_id,
        "bytes": scanned,
        "result_hash": digest,
        "truncated": len(rows) > MAX_ROWS,
    }
