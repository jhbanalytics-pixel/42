"""The agent's sql_query tool and its read-only guard (AGENT.md, Tools and Guardrails; DATA.md section 7)."""

from __future__ import annotations

import contextvars
import re
import threading
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
# Class D of the private-storage fence (C5 v2 section 12, N2): relations model SQL may not read. They hold the
# suppression list and the view over it, retained copies of answers, dossiers, briefs and findings, typed text,
# per-post copies (v_item_evidence), the checks' own reasons (claim_checks) and the job and ledger tables. The datasets
# are allowlisted whole, so each is named here. check_sql refuses them by default; the only ways past are
# INTERNAL_ALLOW, for the few functions that read their own object, and the exact spend query.
FENCED_TABLES = (
    "intelligence_42_core.suppressions",
    "intelligence_42_core.v_suppressed_creators",
    "intelligence_42_core.raw_responses",
    "intelligence_42_core.credit_ledger",
    "intelligence_42_agent.runs",
    "intelligence_42_agent.skins",
    "intelligence_42_agent.feedback",
    "intelligence_42_agent.schedules",
    "intelligence_42_agent.investigations",
    "intelligence_42_agent.dossier_versions",
    "intelligence_42_agent.dossier_reviews",
    "intelligence_42_agent.watches",
    "intelligence_42_agent.v_watches_current",
    "intelligence_42_agent.briefs",
    "intelligence_42_agent.v_briefs_current",
    "intelligence_42_agent.findings",
    "intelligence_42_agent.v_prior_findings",
    "intelligence_42_agent.v_item_evidence",
    "intelligence_42_agent.claim_checks",
)
# Class P: any object whose name ends like this, in either dataset. None exists; the first planned member is the
# failure-text table, which stays unwritten until W8-DEC-08c.
PRIVATE_SUFFIX = "_private"
# Approved views and functions the model may read, with the class D objects each reads underneath (C5 v2 12.9, computed
# from the SQL files). A dry run may list a view's base tables, so a class D table in the dry-run list is allowed only
# when the parsed SQL names an object that reads it. A table outside the union of the named objects' sets is a direct
# read the parser missed, and is refused.
_CORE, _AGENT = "intelligence_42_core", "intelligence_42_agent"
_RUNS, _BRIEFS = f"{_AGENT}.runs", f"{_AGENT}.briefs"
_SUPPRESSIONS, _SUPPRESSED_VIEW = f"{_CORE}.suppressions", f"{_CORE}.v_suppressed_creators"
_RUNS_READERS = tuple(f"{_CORE}.{n}" for n in (
    "v_good_runs", "v_collection_health_current", "v_item_daily_current", "v_series_test_current",
    "v_coord_signals_current", "v_item_state_current", "v_item_counter_daily_current", "v_series_daily",
    "v_item_spread", "v_item_waves", "v_breaking_signals_current", "v_item_market_scope", "tvf_item_window",
    "tvf_placebo_base", "tvf_series_signal")) + tuple(f"{_AGENT}.{n}" for n in (
    "v_item_origin", "v_news_followthrough", "v_items_today", "v_briefs_current", "v_item_gate_current",
    "tvf_item_timeseries"))
_BRIEFS_READERS = (f"{_AGENT}.v_briefs_current", f"{_AGENT}.v_item_gate_current")
_SUPPRESSION_READERS = (f"{_CORE}.v_item_market_scope", _SUPPRESSED_VIEW)
APPROVED_OBJECT_DEPS = {}
for _name in _RUNS_READERS:
    APPROVED_OBJECT_DEPS.setdefault(_name, set()).add(_RUNS)
for _name in _BRIEFS_READERS:
    APPROVED_OBJECT_DEPS.setdefault(_name, set()).add(_BRIEFS)
for _name in _SUPPRESSION_READERS:
    APPROVED_OBJECT_DEPS.setdefault(_name, set()).update({_SUPPRESSIONS, _SUPPRESSED_VIEW} if _name != _SUPPRESSED_VIEW
                                                         else {_SUPPRESSIONS})
# The functions that may read a class D or hidden object, and exactly which (C5 v2 12.5). Each reads through
# internal_read, which is never offered to the model. An entry is added in the commit that adds the read it allows:
# an allowance for a read that does not exist is a hole. The producer predicate reads of C5 8.1 join this registry with
# that work.
INTERNAL_ALLOW = {
    "discover_creators": (_SUPPRESSED_VIEW, _SUPPRESSIONS),
    "recall_findings": (f"{_AGENT}.v_prior_findings", f"{_AGENT}.findings"),
    "get_trending_fallback_snapshot": (f"{_AGENT}.v_briefs_current", _BRIEFS),
    "log_forecast": (f"{_AGENT}.forecasts",),
}
# What the eval harness may let through: every internal object. The harness calls check_sql_harness on each statement
# that reaches the warehouse, internal reads included.
HARNESS_ALLOW = tuple(sorted({name for names in INTERNAL_ALLOW.values() for name in names}))
MAX_DRY_RUN_TABLES = 50  # BigQuery truncates the referenced-table list near this size; refusing here is safe either way
# The tables and columns the model is told about in the tool description (core/schema/core.sql). Without them it
# guessed names ("items", "detection tables") and every query failed (live staging, 4 October).
WAREHOUSE_MAP = {
    "intelligence_42_core.posts": (
        "post_id", "platform", "creator_id", "text", "hashtags", "sound_id", "published_at", "post_date", "views",
        "likes", "comments", "shares", "engagement", "geo_market", "geo_source", "geo_confidence"),
    "intelligence_42_core.post_items": ("post_id", "item_id", "via"),
    "intelligence_42_core.cultural_map": (
        "item_id", "kind", "canonical_key", "label", "aliases", "first_seen", "last_seen", "lifecycle", "status",
        "valid_to"),
    "intelligence_42_core.v_item_daily_current": (
        "metric_date", "market", "platform", "item_id", "lane_class", "posts", "creators", "engagement",
        "local_posts"),
    "intelligence_42_core.v_item_state_current": (
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
    "posts is partitioned by post_date: always filter on it. A post is located only when geo_market is ZA, NG or KE and "
    "IFNULL(p.geo_source, '') != 'home_market'; geo_source home_market means the market came from the creator's "
    "profile, so say it was seen in that market's feeds, never that it was located there. "
    "Params arrive as strings: wrap a date param in DATE(@name). cultural_map keeps one row per item version: the "
    "current row has valid_to IS NULL. Item kinds: topic, "
    "hashtag, sound, format, meme, creator, brand, event. A TikTok sound is posts.sound_id, or an item of kind sound "
    "linked to posts through post_items. A sound's title is the label of its current cultural_map row with kind "
    "'sound' and canonical_key = CONCAT(p.platform, ':', p.sound_id); no row, or a label equal to the sound id, means "
    "42 holds no title for it, so say so. hashtags is an array: count tags with CROSS JOIN UNNEST(p.hashtags) AS tag. Read the derived counts only through "
    "v_item_daily_current and v_item_state_current, which keep the one good run per day; the raw item_daily and "
    "item_state tables are append-only and hold every run. v_item_state_current holds one row per item, market and "
    "day. v_item_daily_current holds one row per day, market, platform, item, lane_class and panel series, and a "
    "post counts on every lane_class row it was seen in, so never sum posts across lane_class rows or platforms: an "
    "item's whole posts in a market for a day is the single row with lane_class = '_any' and platform = '_all'. "
    "Measure reach across the whole store: COUNT(*) and COUNT(DISTINCT "
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
    "WHERE p.platform = 'tiktok' AND p.geo_market = @market AND IFNULL(p.geo_source, '') != 'home_market' "
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


def _hidden(dataset: str, name: str, allowed: tuple = (), tables: tuple = HIDDEN_TABLES) -> bool:
    """True when dataset.name is one of tables, matched without case and through a wildcard or decorator."""
    ref = f"{dataset}.{name.split('$', 1)[0]}".casefold()
    return any(fnmatchcase(hidden, ref) for hidden in tables if hidden not in allowed)


def _could_end_private(pattern: str) -> bool:
    """True when a wildcard pattern could match some name that ends PRIVATE_SUFFIX. Deliberately generous: a pattern
    that ends in a wildcard can match any suffix, so it is refused."""
    if "*" not in pattern and "?" not in pattern:
        return False
    tail = re.split(r"[*?]", pattern)[-1]
    return PRIVATE_SUFFIX.endswith(tail) or tail.endswith(PRIVATE_SUFFIX)


def _always_denied(name: str) -> bool:
    """Class P and the dataset metadata tables: no registry entry lifts these, so a dry-run dependency cannot either."""
    part = name.split("$", 1)[0].casefold()
    return part.startswith("__") or part.endswith(PRIVATE_SUFFIX)


def _denied(dataset: str, name: str, allowed: tuple = ()) -> bool:
    """One decision for the parsed layer and the dry-run layer (C5 v2 12.2), so the two cannot drift. A decorator is
    stripped, case is folded, and the reference is the fnmatch pattern against the registered names, so a wildcard that
    could reach a denied name is itself refused. The class D names in `allowed` (the registry entry of an internal
    read) are the only exception."""
    return _always_denied(name) or _hidden(dataset, name, allowed, FENCED_TABLES)


def _refuse_fenced(shown: str) -> None:
    raise Refused(f"Table {shown} is not available to the agent: it holds records the agent may not read.")


def _refuse_hidden(shown: str) -> None:
    raise Refused(f"Table {shown} is not available to the agent: forecasts stay hidden until they beat persistence.")


def _spend_sql_text() -> str:
    from core.agent.ask import SPEND_SQL

    return " ".join(SPEND_SQL.split())


def check_sql(sql: str, hidden_ok: tuple = ()) -> None:
    """Refuse anything but one read-only query over the allowlisted datasets, outside the forecast tables and the
    private-storage fence (class D and P). hidden_ok names the objects an internal read may use; the model path never
    supplies it. One statement passes the fence by exact text: the daily spend query."""
    try:
        _check_sql(sql, hidden_ok)
    except Refused:
        if sql and " ".join(sql.split()) == _spend_sql_text():
            return
        raise


def check_sql_harness(sql: str) -> None:
    """check_sql for the eval harness, which checks every statement that reaches the warehouse, internal reads
    included: the same checks with the objects of INTERNAL_ALLOW allowed. Model SQL is refused earlier, by the guard
    and by the query tool, before it can reach a harness."""
    check_sql(sql, HARNESS_ALLOW)


def _check_sql(sql: str, hidden_ok: tuple) -> None:
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


def _named_objects(sql: str) -> frozenset:
    """dataset.object for every table, view and table function the SQL names, folded and without decorators."""
    named = set()
    for statement in sqlglot.parse(sql, dialect="bigquery"):
        if statement is None:
            continue
        for table in statement.find_all(exp.Table):
            name = table.this.name if isinstance(table.this, exp.Func) else table.name
            if table.db:
                named.add(f"{table.db}.{name.split('$', 1)[0]}".casefold())
    return frozenset(named)


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
    if _denied(table.db, table.name, hidden_ok):
        _refuse_fenced(shown)
    if _hidden(table.db, table.name, hidden_ok):
        _refuse_hidden(shown)
    if _could_end_private(table.name.split("$", 1)[0].casefold()):
        _refuse_fenced(shown)  # a wildcard that could reach a private name
    if isinstance(table.this, exp.Func) and f"{table.db}.{table.this.name}" not in ALLOWED_TVFS:
        raise Refused(f"Table function {shown} is not allowed. Allowed: {', '.join(sorted(ALLOWED_TVFS))}.")


def _check_dry_run_table(ref: str, hidden_ok: tuple = (), named: frozenset = frozenset()) -> None:
    """A table the dry run lists. Forecasts, class P and the metadata tables are refused outright. A class D table is
    allowed only when it is in hidden_ok, or when an approved object the parsed SQL names reads it underneath."""
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
    refused = f"The dry run shows the query reads {ref}, which holds records the agent may not read."
    if _always_denied(parts[1]):
        raise Refused(refused)
    if _hidden(parts[0], parts[1], hidden_ok, FENCED_TABLES):
        full = f"{parts[0]}.{parts[1]}".casefold()
        if not any(full in APPROVED_OBJECT_DEPS.get(obj, ()) for obj in named):
            raise Refused(refused)


def _reads_search_signals(refs) -> bool:
    """True when the dry run shows the query reads a search-interest table."""
    return any(ref.split(".", ref.count(".") - 1)[-1] in SEARCH_SIGNAL_TABLES for ref in refs or ())


class Warehouse(Protocol):
    def dry_run(self, sql: str, params: dict | None) -> dict: ...

    def run(self, sql: str, params: dict | None, max_bytes_billed: int) -> list[dict]: ...


# Seconds a read on this thread may wait for its BigQuery job, or None for no limit. Set by the topic sweep for its own
# reads, so a slow search ends as a failed search; every other read keeps the client's behaviour.
QUERY_TIMEOUT_S: contextvars.ContextVar = contextvars.ContextVar("query_timeout_s", default=None)


def _wait() -> dict:
    """The timeout argument for job.result, only when this thread has set one."""
    seconds = QUERY_TIMEOUT_S.get()
    return {} if seconds is None else {"timeout": seconds}


class BigQueryWarehouse:
    """google-cloud-bigquery behind the Warehouse protocol. The client is built on first use."""

    def __init__(self, project: str = PROJECT):
        self.project = project
        self._client = None
        self._client_lock = threading.Lock()

    def _bq(self):
        from google.cloud import bigquery

        if self._client is None:
            with self._client_lock:  # research and the store count both ask in the first seconds
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
            rows = job.result(max_results=MAX_ROWS + 1, job_retry=None, **_wait())
        else:
            job = self._client.query(sql, job_config=config)
            rows = job.result(max_results=MAX_ROWS + 1, **_wait())
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


def model_sql_query(
    ctx: RunContext,
    warehouse: Warehouse,
    sql: str,
    purpose: str,
    params: dict | None = None,
    max_bytes_billed: int = MAX_BYTES_BILLED,
) -> dict:
    """sql_query for SQL the model wrote. It takes no hidden_ok, so the model path cannot reach a class D object."""
    return _run_query(ctx, warehouse, sql, purpose, params, max_bytes_billed)


def internal_read(function: str, ctx: RunContext, warehouse: Warehouse, sql: str, purpose: str,
                  params: dict | None = None, max_bytes_billed: int = MAX_BYTES_BILLED) -> dict:
    """sql_query for a tool's own read of an object the model may not name. The objects allowed are the function's
    entry in INTERNAL_ALLOW, and an unregistered function is a KeyError. Never offered to the model."""
    return _run_query(ctx, warehouse, sql, purpose, params, max_bytes_billed, hidden_ok=INTERNAL_ALLOW[function])


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


def _dispatch_query(ctx: RunContext, warehouse: Warehouse, sql: str, params: dict | None,
                    max_bytes_billed: int) -> list[dict]:
    """Execute an already-validated read, reserving embedded-model cost before dispatch, including numeric reruns."""
    embedding_texts = []
    if "tvf_search_posts" in sql.casefold():
        for table in sqlglot.parse_one(sql, dialect="bigquery").find_all(exp.Table):
            function = table.this
            if isinstance(function, exp.Func) and function.name.casefold().rsplit(".", 1)[-1] == "tvf_search_posts":
                content = function.expressions[0]
                if isinstance(content, exp.Parameter):
                    text = (params or {}).get(content.name)
                elif isinstance(content, exp.Literal) and content.is_string:
                    text = content.this
                else:
                    raise Refused("Semantic search needs a literal query or a named text parameter.")
                if not isinstance(text, str):
                    raise Refused("Semantic search needs a text query.")
                embedding_texts.append(text)
    reservation = None
    budget = ctx.model_budget
    if embedding_texts:
        from core.understand.embed import CHARS_PER_TOKEN, EMBED_USD_PER_MILLION_TOKENS, MAX_TOKENS_PER_POST

        ceiling = len(embedding_texts) * MAX_TOKENS_PER_POST * EMBED_USD_PER_MILLION_TOKENS / 1_000_000
        estimated = sum(min(len(text) / CHARS_PER_TOKEN, MAX_TOKENS_PER_POST) for text in embedding_texts)
        estimated = estimated * EMBED_USD_PER_MILLION_TOKENS / 1_000_000
        if budget is not None:
            reservation = budget.reserve_cost(ceiling, research=True)
    try:
        rows = warehouse.run(sql, params, max_bytes_billed)
    except BaseException:
        if reservation is not None:
            billed = budget.settle_cost(reservation)
            with ctx.lock:
                ctx.model_usd_extra += billed
        raise
    else:
        if reservation is not None:
            billed = budget.settle_cost(reservation)
        elif embedding_texts:
            billed = estimated
        else:
            billed = 0.0
        if billed:
            with ctx.lock:
                ctx.model_usd_extra += billed
        return rows


def _run_query(ctx: RunContext, warehouse: Warehouse, sql: str, purpose: str, params: dict | None = None,
               max_bytes_billed: int = MAX_BYTES_BILLED, hidden_ok: tuple = ()) -> dict:
    """sql_query, with hidden_ok naming the objects an internal read may use. Only internal_read passes it."""
    check_sql(sql, hidden_ok)
    cap = min(int(max_bytes_billed), MAX_BYTES_BILLED)

    dry = warehouse.dry_run(sql, params)
    if len(dry["tables"]) >= MAX_DRY_RUN_TABLES:
        raise Refused(f"The dry run lists {len(dry['tables'])} tables, which is more than can be checked, so the "
                      f"query is refused. Narrow it.")
    named = _named_objects(sql)
    for ref in dry["tables"]:
        _check_dry_run_table(ref, hidden_ok, named)
    scanned = int(dry["bytes"])
    if scanned > cap:
        raise Refused(f"The query would scan {scanned:,} bytes, over the {cap:,} byte cap. Narrow it.")

    rows = _dispatch_query(ctx, warehouse, sql, params, cap)
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
