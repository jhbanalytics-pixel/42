"""Local DuckDB harness for the BigQuery detection SQL.

Datasets become DuckDB schemas named core and agent. Each statement in views.sql is transpiled from
BigQuery to DuckDB with sqlglot; a CREATE TABLE FUNCTION becomes a DuckDB table macro. Where sqlglot
mistranslates, a narrow rewrite below fixes the DuckDB side only; the BigQuery SQL is never changed.
"""

import re

import duckdb
import sqlglot
from sqlglot import exp

from .. import sqlrun

# DuckDB DDL for every table the views read. Section 1 tables carry their full DATA.md column list;
# tables listed under "Other tables" carry their key columns plus the columns the SQL reads.
# BigQuery to DuckDB types: STRING VARCHAR, INT64 BIGINT, FLOAT64 DOUBLE, BOOL BOOLEAN,
# TIMESTAMP TIMESTAMPTZ, ARRAY<T> T[], STRUCT<...> STRUCT(...), JSON JSON.
TABLES = """
CREATE TABLE core.raw_responses (
  run_id VARCHAR NOT NULL, job VARCHAR, market VARCHAR, route VARCHAR, params_hash VARCHAR,
  lane VARCHAR, seed_key VARCHAR, fetched_at TIMESTAMPTZ NOT NULL, http_status BIGINT,
  credits_quoted DOUBLE, credits_charged DOUBLE, cache_hit BOOLEAN, body JSON);

CREATE TABLE core.posts (
  post_id VARCHAR NOT NULL, platform VARCHAR, native_id VARCHAR, url VARCHAR, creator_id VARCHAR,
  creator_tier_at_post VARCHAR, text VARCHAR, transcript VARCHAR, hashtags VARCHAR[], sound_id VARCHAR,
  thumbnail_url VARCHAR, duration_s DOUBLE, published_at TIMESTAMPTZ, post_date DATE NOT NULL,
  views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, engagement BIGINT,
  geo_market VARCHAR, geo_confidence DOUBLE, geo_source VARCHAR,
  geo_scope VARCHAR, vendor VARCHAR, endpoint VARCHAR, source_regime VARCHAR, vendor_labels JSON, run_id VARCHAR);

CREATE TABLE core.source_market_fixture (
  post_id VARCHAR,
  source_markets VARCHAR[],
  source_sightings STRUCT(
    source_market VARCHAR,
    source_region VARCHAR,
    route VARCHAR,
    protocol VARCHAR,
    observed_at TIMESTAMPTZ,
    obs_date DATE
  )[]
);

CREATE VIEW core.v_post_source_markets AS
SELECT post_id, source_markets, source_sightings FROM core.source_market_fixture;

-- Stands in for L1's v_suppressed_creators (core/schema/core.sql): the creator ids of the suppression list.
CREATE TABLE core.suppressed_fixture (creator_id VARCHAR);

CREATE VIEW core.v_suppressed_creators AS SELECT creator_id FROM core.suppressed_fixture;

CREATE TABLE core.post_observations (
  post_id VARCHAR NOT NULL, observed_at TIMESTAMPTZ NOT NULL, observed_date DATE NOT NULL,
  market VARCHAR NOT NULL, platform VARCHAR, route VARCHAR, series VARCHAR, protocol VARCHAR,
  lane VARCHAR, lane_class VARCHAR NOT NULL, seed_key VARCHAR, pull_seq BIGINT, rank BIGINT,
  views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, run_id VARCHAR);

CREATE TABLE core.item_counter_daily (
  obs_date DATE NOT NULL, market VARCHAR NOT NULL, platform VARCHAR NOT NULL, item_id VARCHAR NOT NULL,
  series VARCHAR NOT NULL, route VARCHAR, protocol VARCHAR NOT NULL, is_board BOOLEAN,
  lane_class VARCHAR NOT NULL, unit VARCHAR NOT NULL, pull_seq BIGINT, value DOUBLE, source VARCHAR,
  observed_at TIMESTAMPTZ, available_at TIMESTAMPTZ, run_id VARCHAR);

CREATE TABLE core.collection_health (
  day DATE NOT NULL, market VARCHAR NOT NULL, platform VARCHAR, route VARCHAR,
  series VARCHAR NOT NULL, protocol VARCHAR NOT NULL, lane_class VARCHAR,
  calls BIGINT, calls_ok BIGINT, units_planned BIGINT, units_ok BIGINT, items BIGINT,
  ref_items DOUBLE, ref_days BIGINT, k DOUBLE, valid BOOLEAN, invalid_reason VARCHAR,
  located_share DOUBLE, run_id VARCHAR);

CREATE TABLE core.item_daily (
  metric_date DATE NOT NULL, market VARCHAR NOT NULL, platform VARCHAR NOT NULL, item_id VARCHAR NOT NULL,
  lane_class VARCHAR NOT NULL, series VARCHAR, protocol VARCHAR,
  posts BIGINT, creators BIGINT, unflagged_creators BIGINT, engagement BIGINT,
  tier_posts STRUCT(nano BIGINT, micro BIGINT, mid BIGINT, macro BIGINT, mega BIGINT),
  first_post_at TIMESTAMPTZ, geo_known_posts BIGINT, local_posts BIGINT,
  source_regime VARCHAR, available_at TIMESTAMPTZ, run_id VARCHAR, rule_version VARCHAR);

CREATE TABLE core.series_test (
  metric_date DATE NOT NULL, series_id VARCHAR NOT NULL, item_id VARCHAR, market VARCHAR, platform VARCHAR,
  series VARCHAR, protocol VARCHAR, lane_class VARCHAR, kind VARCHAR,
  y DOUBLE, trials BIGINT, obs_prior BIGINT, obs28 BIGINT, first_measured DATE,
  baseline_state VARCHAR,
  hist_mean DOUBLE, med DOUBLE, v3 DOUBLE, v7 DOUBLE, peak28 DOUBLE, vel DOUBLE, accel DOUBLE,
  z_display DOUBLE, test VARCHAR,
  mu DOUBLE, alpha DOUBLE, weekday_factor DOUBLE, mu_prior DOUBLE,
  ratio DOUBLE, p_mid DOUBLE, q DOUBLE, significant BOOLEAN,
  run_id VARCHAR, rule_version VARCHAR);

CREATE TABLE core.cultural_map (
  item_id VARCHAR NOT NULL, kind VARCHAR NOT NULL,
  canonical_key VARCHAR, label VARCHAR, aliases VARCHAR[], parent_item_id VARCHAR,
  centroid DOUBLE[],
  first_seen DATE, first_seen_market VARCHAR, first_seen_platform VARCHAR, last_seen DATE,
  recurrences BIGINT, lifecycle VARCHAR, status VARCHAR, rejected_until DATE,
  valid_from TIMESTAMPTZ, valid_to TIMESTAMPTZ);

CREATE TABLE core.post_items (post_id VARCHAR, item_id VARCHAR, via VARCHAR);

CREATE TABLE core.post_enrichment (post_id VARCHAR, embedding DOUBLE[], langs VARCHAR[], tone VARCHAR, stance VARCHAR,
  sponsored BOOLEAN, near_dup_size BIGINT);

CREATE TABLE core.creators (
  creator_id VARCHAR, platform VARCHAR, handle VARCHAR, account_created_at TIMESTAMPTZ,
  home_market VARCHAR, verified_region VARCHAR, coord_score BIGINT);

CREATE TABLE core.clusters (
  cluster_date DATE, cluster_id VARCHAR, market VARCHAR, item_id VARCHAR, match_kind VARCHAR);

CREATE TABLE core.cluster_members (cluster_id VARCHAR NOT NULL, post_id VARCHAR NOT NULL, probability DOUBLE);

CREATE TABLE core.item_state (
  metric_date DATE, market VARCHAR, item_id VARCHAR, kind VARCHAR, state_raw VARCHAR, state VARCHAR,
  untested BOOLEAN, main_series_id VARCHAR, main_y DOUBLE, main_mu DOUBLE, main_ratio DOUBLE,
  q_min DOUBLE, sig_days3 BIGINT, creators3 BIGINT, posts3 BIGINT, top_creator_share3 DOUBLE,
  authenticity VARCHAR, share_flags VARCHAR[], sponsored_share DOUBLE, geo_status VARCHAR,
  local_share DOUBLE, geo_known_posts7 BIGINT, spread_platforms BIGINT, found_platforms BIGINT,
  markets_hot BIGINT, lead_market VARCHAR, diffusion VARCHAR, novelty VARCHAR,
  last_wave STRUCT(peak_date DATE, peak_posts BIGINT), moment VARCHAR, eligible BOOLEAN,
  worth_raw DOUBLE, worth_pct DOUBLE, run_id VARCHAR, rule_version VARCHAR, base_state VARCHAR);

CREATE TABLE core.coord_signals (
  metric_date DATE, item_id VARCHAR, market VARCHAR, run_id VARCHAR,
  signal VARCHAR, component_id VARCHAR, accounts BIGINT, item_posts_share DOUBLE, rule_version VARCHAR);

CREATE TABLE core.calendar (
  moment_date DATE, market VARCHAR, name VARCHAR, kind VARCHAR, source VARCHAR, item_ids VARCHAR[]);

CREATE TABLE core.seed_queue (
  seed_date DATE, market VARCHAR, item_id VARCHAR, query VARCHAR, kind VARCHAR, lane VARCHAR);

CREATE TABLE agent.runs (
  run_id VARCHAR NOT NULL, stage VARCHAR, run_date DATE, status VARCHAR,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, counts JSON, error VARCHAR,
  question VARCHAR, tier VARCHAR, plan JSON, calls BIGINT, credits DOUBLE, tokens BIGINT,
  seconds DOUBLE, outcome VARCHAR, answer JSON, record JSON);

CREATE TABLE agent.briefs (
  brief_date DATE NOT NULL, market VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
  published_at TIMESTAMPTZ, status VARCHAR, payload JSON, rule_version VARCHAR);

CREATE TABLE core.media (sha256 VARCHAR, post_id VARCHAR, gcs_uri VARCHAR, kind VARCHAR);

CREATE TABLE agent.forecasts (
  forecast_id VARCHAR NOT NULL, item_id VARCHAR, market VARCHAR, target VARCHAR, issue_date DATE,
  horizon BIGINT, rule VARCHAR, prob DOUBLE, predicted_arrival BOOLEAN, persistence_arrival BOOLEAN,
  resolve_date DATE, observed_arrival BOOLEAN);

-- One JSON Figure per metric.
CREATE TABLE agent.engine_scorecard (
  week_start DATE NOT NULL, week_end DATE NOT NULL, market VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
  rule_version VARCHAR, time_to_detect JSON, lead_time JSON, "precision" JSON, recall JSON,
  breadth_platforms JSON, expansion_cluster_share JSON, expansion_platform_share JSON,
  expansion_language_share JSON, cost_per_confirmed JSON);

CREATE TABLE agent.watches (
  watch_id VARCHAR NOT NULL, created_at TIMESTAMPTZ, status_at TIMESTAMPTZ, who VARCHAR, target JSON,
  market VARCHAR, rule JSON, label VARCHAR, status VARCHAR);

CREATE TABLE agent.watch_matches (
  watch_id VARCHAR NOT NULL, match_date DATE NOT NULL, item_id VARCHAR, market VARCHAR,
  method VARCHAR, run_id VARCHAR);

-- L1's item_hourly (core/schema/core.sql), written by the intraday pulse.
CREATE TABLE core.item_hourly (
  market VARCHAR NOT NULL, item_id VARCHAR NOT NULL, platform VARCHAR NOT NULL, hour TIMESTAMPTZ NOT NULL,
  posts BIGINT, creators BIGINT, lane_class VARCHAR NOT NULL, run_id VARCHAR NOT NULL);

-- The hourly Breaking rule's output (core/detect/breaking.py), append-only.
CREATE TABLE core.breaking_signals (
  hour TIMESTAMPTZ NOT NULL, market VARCHAR NOT NULL, item_id VARCHAR NOT NULL, posts6 BIGINT, creators6 BIGINT,
  expected6 DOUBLE, ratio DOUBLE, platforms BIGINT, run_id VARCHAR NOT NULL, rule_version VARCHAR);

-- The creator-breakout signals (core/detect/breakout.py), append-only; L1 created it on staging (L2 Needs 15).
CREATE TABLE core.breakout_signals (
  metric_date DATE NOT NULL, market VARCHAR NOT NULL, item_id VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
  creators BIGINT, posts BIGINT, evidence_post_ids VARCHAR[], top_ratio DOUBLE, held_flagged BIGINT,
  rule_version VARCHAR);

-- L1's v_watches_current (core/schema/agent.sql) in DuckDB: the latest row per watch_id, ties to paused.
CREATE VIEW agent.v_watches_current AS
SELECT w.* FROM agent.watches w
QUALIFY ROW_NUMBER() OVER (PARTITION BY w.watch_id
  ORDER BY COALESCE(w.status_at, w.created_at) DESC NULLS LAST, w.status DESC,
           CAST(w.target AS VARCHAR), CAST(w.rule AS VARCHAR)) = 1;
"""

_HEADER = re.compile(
    r"^CREATE\s+OR\s+REPLACE\s+(VIEW|TABLE\s+FUNCTION)\s+([\w.]+)(?:\(([^)]*)\))?\s+AS\s+(.*)$",
    re.IGNORECASE | re.DOTALL)


def split_create(stmt):
    """(kind, name, parameter names, body) of a CREATE OR REPLACE VIEW or TABLE FUNCTION statement."""
    kind, name, params, body = _HEADER.match(stmt).groups()
    names = [p.split()[0] for p in params.split(",")] if params else []
    return " ".join(kind.upper().split()), name, names, body


def _rewrite(node):
    # APPROX_QUANTILES(x, n)[OFFSET(k)]: sqlglot gives DuckDB's t-digest APPROX_QUANTILE, which can
    # return values not in the input; QUANTILE_DISC(x, k/n) returns an input value like BigQuery does.
    if isinstance(node, exp.Bracket) and isinstance(node.this, exp.ApproxQuantiles):
        k = int(node.expressions[0].this) - int(node.args.get("offset") or 0)
        n = int(node.this.expression.this)
        return exp.Anonymous(this="QUANTILE_DISC", expressions=[node.this.this.copy(), exp.Literal.number(k / n)])
    # ARRAY_AGG(x ORDER BY y LIMIT n): DuckDB has no LIMIT inside an aggregate; slice the ordered list.
    if isinstance(node, exp.ArrayAgg) and isinstance(node.this, exp.Limit):
        limit = node.this
        agg = exp.ArrayAgg(this=limit.this.copy())
        return exp.Anonymous(this="LIST_SLICE", expressions=[agg, exp.Literal.number(1), limit.expression.copy()])
    # SAFE.LN(x) and SAFE.SQRT(x): sqlglot passes the SAFE prefix through and DuckDB errors on it;
    # NULL outside the domain (LN needs x > 0, SQRT x >= 0) as BigQuery gives.
    if isinstance(node, exp.SafeFunc) and isinstance(node.this, (exp.Ln, exp.Sqrt)):
        x, fn = node.this.this, type(node.this)
        test = exp.GT if fn is exp.Ln else exp.GTE
        return exp.If(this=test(this=x.copy(), expression=exp.Literal.number(0)), true=fn(this=x.copy()))
    return node


def _tree(sql):
    return sqlglot.parse_one(sql, read="bigquery").transform(_rewrite)


def to_duck(sql):
    """Transpile one BigQuery statement (no CREATE header) to DuckDB."""
    return _tree(sql).sql(dialect="duckdb")


def create_statement(stmt):
    kind, name, params, body = split_create(stmt)
    if kind == "VIEW":
        return f"CREATE OR REPLACE VIEW {name} AS {to_duck(body)}"
    return f"CREATE OR REPLACE MACRO {name}({', '.join(params)}) AS TABLE {to_duck(body)}"


# Shared helpers for SQL files beyond views.sql, such as state.sql and waves.sql.

strip_leading_comments = sqlrun._strip_leading_comments

_TEMP_FN = re.compile(r"^CREATE\s+TEMP\s+FUNCTION\s+(\w+)\(([^)]*)\)\s+AS\s+\((.*)\)$", re.IGNORECASE | re.DOTALL)


def temp_macro(stmt):
    """A BigQuery CREATE TEMP FUNCTION as a DuckDB temp macro."""
    name, params, body = _TEMP_FN.match(stmt).groups()
    names = ", ".join(p.split()[0] for p in params.split(","))
    return f"CREATE OR REPLACE TEMP MACRO {name}({names}) AS ({to_duck(body)})"


def run_duck(con, sql, params):
    """Run one BigQuery statement with @params on DuckDB, passing only the params it uses; returns nothing."""
    tree = _tree(sql)
    used = {p.name for p in tree.find_all(exp.Parameter)}
    con.execute(tree.sql(dialect="duckdb"), {k: v for k, v in params.items() if k in used})


def connect(views=True):
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA core; CREATE SCHEMA agent;")
    con.execute(TABLES)
    if views:
        for stmt in sqlrun.statements(core="core", agent="agent"):
            con.execute(create_statement(stmt))
    return con


def load(con, table, rows):
    """Insert dict rows into a table such as 'core.item_daily'; rows may name different columns."""
    for row in rows:
        cols = list(row)
        con.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [row[c] for c in cols])


def query(con, sql, params=None):
    """Run a BigQuery-dialect query with {core}, {agent} and @params on DuckDB; rows as dicts."""
    tree = _tree(sqlrun.render(sql, core="core", agent="agent"))
    used = {p.name for p in tree.find_all(exp.Parameter)}
    cur = con.execute(tree.sql(dialect="duckdb"), {k: v for k, v in (params or {}).items() if k in used})
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _value(param):
    """The Python value of a google-cloud-bigquery query parameter; struct arrays become lists of dicts."""
    if hasattr(param, "struct_values"):
        return dict(param.struct_values)
    if hasattr(param, "values"):
        return [_value(v) for v in param.values]
    return param.value


class _Job:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return self.rows


class Client:
    """Stands in for a google.cloud.bigquery Client: each query runs through query() on DuckDB.
    Code under test renders dataset names as core and agent. Every SQL text is kept in .sql."""

    def __init__(self, con):
        self.con = con
        self.sql = []

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        params = {p.name: _value(p) for p in (job_config.query_parameters if job_config else [])}
        return _Job(query(self.con, sql, params))
