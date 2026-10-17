"""Read-only history audit of what 42 holds for studying change over time (Gamma 3.1 data audit, draft).

    py -3.13 -m core.setup.history_audit --plan                    prints every query; builds no client, no network
    py -3.13 -m core.setup.history_audit --out history-audit.md    runs them and writes aggregate counts only

Run from the root of a checkout of the 42 repo (it imports core/setup/inventory.py), on Albert's PC, with the
default BigQuery client whose application default credentials impersonate f42-builder (RULES.md rule 13). Never
the bq CLI (RULES.md rule 12). Modelled on core/setup/inventory.py and reusing its project, location, error
helpers and refusal class: every statement is one read-only SELECT (or WITH ... SELECT), each is dry-run first and
refused when the estimate is over MAX_BYTES, then run with maximum_bytes_billed at MAX_BYTES, and the whole run
stops before a query whose estimate would take the run past TOTAL_BYTES.

What comes back is counts, dates, rates and code-level categories only (markets, lane classes, series names,
route names, states, reasons, kinds). No statement selects post text, handles, URLs, labels, keywords, quotes,
payloads or vectors as output; the two identity queries read labels, keywords and centroids inside SQL only to
count changes and compute similarities, and return numbers. Every returned row is checked again before it is
written: a string is kept only in a known category column and only when it is short and code-shaped, so a query
that returned anything else is withheld whole and says so. Protocol strings are reported as a 10 hex sha256
prefix, never as written. The output is safe to paste into a cloud chat.
"""
import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from google.api_core.exceptions import GoogleAPIError, NotFound
from google.cloud import bigquery

try:
    from core.setup import inventory as inv
except ImportError:  # run from outside the repo root: look for it beside the working directory
    sys.path.insert(0, str(Path.cwd()))
    from core.setup import inventory as inv

PROJECT, LOCATION = inv.PROJECT, inv.LOCATION
Refused, first_line, bytes_text = inv.Refused, inv.first_line, inv.bytes_text
MAX_BYTES = 2 * 1024 ** 3            # per query, the agent views' cap (DATA.md section 7), below inventory's 5 GB
TOTAL_BYTES = 20 * 1024 ** 3         # the whole run
DEFAULT_SINCE = dt.date(2026, 9, 1)  # before the first SocialCrawl collect day; partitions before it are not read
ROW_LIMIT = 4000
CORE = f"`{PROJECT}`.intelligence_42_core"
AGENT = f"`{PROJECT}`.intelligence_42_agent"
LEGACY = f"`{PROJECT}`.trends_v2_dev"

# Output columns that may hold a string, and the shape such a string must have.
CATEGORIES = {
    "dataset", "table_name", "stage", "status", "market", "lane_class", "lane", "series", "platform", "route",
    "route_platform", "invalid_class", "protocol_hash", "rule_version", "unit", "source", "kind", "match_kind",
    "baseline_state", "test", "state", "novelty", "target", "rule", "via", "tier", "term_type",
    "source_regime", "http_class", "date_column", "month_kind", "bucket",
}
SAFE_STRING = re.compile(r"^[A-Za-z0-9_:./=?&+, -]{0,96}$")
DML = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|EXPORT|CALL|DECLARE|EXECUTE|GRANT|"
                 r"REVOKE|BEGIN|LOAD)\b", re.IGNORECASE)
# Content columns no statement may name. label, keywords and centroid are allowed only where READS_CONTENT says.
CONTENT = re.compile(r"\b(text|transcript|url|thumbnail_url|handle|display_name|body|profile_location|aliases|"
                     r"payload|answer|question|canonical_key|seed_key|query|entity|name|screen_text|video_notes|"
                     r"embedding|record|plan|counts|error)\b", re.IGNORECASE)
CONTENT_INNER = re.compile(r"\b(label|keywords|centroid|prev_label)\b", re.IGNORECASE)


class Withheld(Exception):
    pass


@dataclass
class Query:
    name: str
    purpose: str
    sql: str
    params: dict = field(default_factory=dict)
    reads_content: bool = False  # reads label, keywords or centroid inside SQL to count or compare them


def fill(sql):
    return " ".join(sql.replace("{core}", CORE).replace("{agent}", AGENT).replace("{legacy}", LEGACY).split())


def check_statement(q):
    """One read-only SELECT or WITH statement that names no content column as output."""
    sql = q.sql
    if not re.match(r"^(SELECT|WITH)\s", sql) or ";" in sql:
        raise ValueError(f"{q.name}: refusing a statement that is not a single SELECT")
    if DML.search(sql):
        raise ValueError(f"{q.name}: refusing a statement with a write or script keyword")
    if re.search(r"SELECT\s+(\w+\.)?\*|\.\*", sql, re.IGNORECASE):
        raise ValueError(f"{q.name}: refusing SELECT *")
    if CONTENT.search(sql):
        raise ValueError(f"{q.name}: refusing a statement that names a content column")
    if CONTENT_INNER.search(sql) and not q.reads_content:
        raise ValueError(f"{q.name}: reads label, keywords or centroid without being marked to")
    for ref in re.findall(r"`([^`]+)`", sql):
        if ref != PROJECT:
            raise ValueError(f"{q.name}: refusing a reference outside project {PROJECT}: {ref}")


def scalar_params(params):
    out = []
    for name, value in sorted(params.items()):
        kind = "DATE" if isinstance(value, dt.date) else "INT64" if isinstance(value, int) else "STRING"
        out.append(bigquery.ScalarQueryParameter(name, kind, value))
    return out


def safe_value(column, value):
    if value is None or isinstance(value, (bool, int, float, dt.date, dt.datetime)):
        return value
    if isinstance(value, str) and column in CATEGORIES and SAFE_STRING.match(value):
        return value
    raise Withheld(f"column {column} returned a value that is not a count, date or short code")


def guard_rows(rows):
    """Plain dicts of the rows, or Withheld when any value is not aggregate-shaped."""
    out = []
    for r in rows:
        items = r.items() if hasattr(r, "items") else dict(r).items()
        out.append({k: safe_value(k, v) for k, v in items})
    return out


def run_query(client, q, log, budget):
    """inventory.run_query with this audit's caps: dry run, refuse over MAX_BYTES or the run budget, then run
    capped. Every call is appended to log."""
    check_statement(q)
    params = scalar_params(q.params)
    entry = {"name": q.name, "sql": q.sql, "params": q.params, "estimate": None, "processed": None,
             "billed": None, "status": "ok", "rows": None}
    log.append(entry)
    try:
        dry = client.query(q.sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=params), location=LOCATION)
        entry["estimate"] = dry.total_bytes_processed or 0
        if entry["estimate"] > MAX_BYTES:
            entry["status"] = "refused"
            raise Refused(f"dry run estimate {entry['estimate']:,} bytes is over the {MAX_BYTES:,} byte cap")
        if budget["spent"] + entry["estimate"] > TOTAL_BYTES:
            entry["status"] = "refused"
            raise Refused(f"the run budget of {TOTAL_BYTES:,} bytes would be passed")
        job = client.query(q.sql, job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=MAX_BYTES, query_parameters=params), location=LOCATION)
        rows = list(job.result())
    except NotFound:
        entry["status"] = "not found"
        raise
    except GoogleAPIError as error:
        entry["status"] = f"failed: {type(error).__name__}"
        raise
    entry["processed"] = job.total_bytes_processed
    entry["billed"] = job.total_bytes_billed
    budget["spent"] += entry["billed"] or entry["processed"] or 0
    try:
        entry["rows"] = guard_rows(rows[:ROW_LIMIT])
    except Withheld as error:
        entry["status"] = f"withheld: {error}"
        entry["rows"] = None
    entry["truncated"] = len(rows) > ROW_LIMIT
    return entry["rows"]


# Table, date expression and purpose for the per-table span queries: when each table started holding rows.
SPANS = [
    ("core", "raw_responses", "DATE(t.fetched_at)", "every SocialCrawl response"),
    ("core", "posts", "t.post_date", "one row per post; post_date from published_at"),
    ("core", "post_observations", "t.observed_date", "one row per sighting"),
    ("core", "item_counter_daily", "t.obs_date", "rank and counter reads"),
    ("core", "collection_health", "t.day", "validity per market, series, protocol and day"),
    ("core", "item_daily", "t.metric_date", "daily aggregates, legacy included"),
    ("core", "series_test", "t.metric_date", "the series test, test none while switched off"),
    ("core", "item_state", "t.metric_date", "daily states"),
    ("core", "clusters", "t.cluster_date", "daily clusters"),
    ("core", "post_items", "t.linked_on", "post to item links (NULL linked_on counted as dates NULL)"),
    ("core", "coord_signals", "t.metric_date", "co-action"),
    ("core", "breakout_signals", "t.metric_date", "creator breakouts"),
    ("core", "test_switch", "t.switched_on", "series test switches"),
    ("core", "item_hourly", "DATE(t.hour)", "hourly counts, not wired"),
    ("core", "breaking_signals", "DATE(t.hour)", "Breaking, off while PULSE_READY is False"),
    ("core", "gdelt_daily", "t.day", "GDELT news mentions"),
    ("core", "calendar_analogues", "DATE(t.computed_at)", "last year's analogue per moment"),
    ("core", "seed_queue", "t.seed_date", "expansion, exploration and placebo seeds"),
    ("core", "credit_ledger", "t.trend_date", "credits"),
    ("core", "item_locality", "t.run_date", "locality v2 shadow"),
    ("core", "post_item_lineage", "t.linked_on", "dated topic links"),
    ("agent", "runs", "t.run_date", "every job run"),
    ("agent", "briefs", "t.brief_date", "daily briefs"),
    ("agent", "forecasts", "t.issue_date", "engine and Ask forecasts"),
    ("agent", "forecast_score", "t.week_start", "weekly forecast scoring"),
    ("agent", "engine_scorecard", "t.week_start", "weekly detection scorecard"),
    ("agent", "weekly_quality", "t.week_start", "weekly quality score"),
]


def span_query(dataset, table, expr, purpose):
    ref = "{core}" if dataset == "core" else "{agent}"
    return Query(f"span_{table}", f"{dataset}.{table}: {purpose}",
                 fill(f"SELECT COUNT(*) AS total_rows, MIN({expr}) AS first_date, MAX({expr}) AS last_date, "
                      f"COUNT(DISTINCT {expr}) AS dates FROM {ref}.{table} t"))


def queries(since=DEFAULT_SINCE):
    s = {"since": since}
    qs = [
        Query("tables_core", "Tables and views in intelligence_42_core with row counts and dates (metadata, 0 bytes)",
              fill("SELECT 'intelligence_42_core' AS dataset, t.table_id AS table_name, t.type, t.row_count, "
                   "DATE(TIMESTAMP_MILLIS(t.creation_time)) AS created_on, "
                   "DATE(TIMESTAMP_MILLIS(t.last_modified_time)) AS modified_on "
                   "FROM {core}.__TABLES__ t ORDER BY t.table_id")),
        Query("tables_agent", "Tables and views in intelligence_42_agent (metadata, 0 bytes)",
              fill("SELECT 'intelligence_42_agent' AS dataset, t.table_id AS table_name, t.type, t.row_count, "
                   "DATE(TIMESTAMP_MILLIS(t.creation_time)) AS created_on, "
                   "DATE(TIMESTAMP_MILLIS(t.last_modified_time)) AS modified_on "
                   "FROM {agent}.__TABLES__ t ORDER BY t.table_id")),
    ]
    qs += [span_query(*row) for row in SPANS]
    qs += [
        Query("runs_by_stage", "Runs per stage and status: first and last run dates; legacy backfill runs apart",
              fill("SELECT r.stage, r.status, STARTS_WITH(r.run_id, 'legacy') AS legacy_run, COUNT(*) AS runs, "
                   "COUNT(DISTINCT r.run_date) AS run_dates, MIN(r.run_date) AS first_run_date, "
                   "MAX(r.run_date) AS last_run_date, MIN(DATE(r.started_at)) AS first_started "
                   "FROM {agent}.runs r GROUP BY r.stage, r.status, legacy_run ORDER BY r.stage, r.status")),
        Query("ok_runs_by_day", "Good runs per day of the daily chain stages (gaps are failed or skipped days)",
              fill("SELECT r.run_date, r.stage, COUNTIF(r.status = 'ok') AS ok_runs, "
                   "COUNTIF(r.status != 'ok') AS other_runs FROM {agent}.runs r "
                   "WHERE r.run_date >= @since AND NOT STARTS_WITH(r.run_id, 'legacy') "
                   "AND r.stage IN ('collect', 'understand', 'aggregate', 'stats', 'detect', 'coaction', 'breakout', "
                   "'forecast', 'brief', 'learn', 'backtest') "
                   "GROUP BY r.run_date, r.stage ORDER BY r.run_date, r.stage"), s),
        Query("health_by_series", "Per market and series: days, valid days, protocols and invalid reasons "
              "(current good collect run per day)",
              fill("SELECT h.market, h.series, h.lane_class, MIN(h.day) AS first_day, MAX(h.day) AS last_day, "
                   "COUNT(DISTINCT h.day) AS days, COUNT(DISTINCT IF(h.valid, h.day, NULL)) AS valid_days, "
                   "COUNT(DISTINCT h.protocol) AS protocols, "
                   "COUNTIF(STARTS_WITH(IFNULL(h.invalid_reason, ''), 'calls')) AS invalid_calls, "
                   "COUNTIF(h.invalid_reason = 'items') AS invalid_items, "
                   "COUNTIF(h.invalid_reason = 'effort') AS invalid_effort, "
                   "COUNTIF(h.invalid_reason = 'zero_yield') AS invalid_zero_yield, "
                   "COUNTIF(h.invalid_reason = 'drift') AS invalid_drift, "
                   "ROUND(AVG(h.located_share), 3) AS mean_located_share, ROUND(AVG(h.k), 3) AS mean_k "
                   "FROM {core}.v_collection_health_current h WHERE h.day >= @since "
                   "GROUP BY h.market, h.series, h.lane_class ORDER BY h.market, h.series, h.lane_class"), s),
        Query("health_by_day", "Per day, market and lane class: series judged and how many were invalid, by class",
              fill("SELECT h.day, h.market, h.lane_class, COUNT(*) AS series_days, COUNTIF(h.valid) AS valid, "
                   "COUNTIF(NOT h.valid) AS invalid, "
                   "COUNTIF(STARTS_WITH(IFNULL(h.invalid_reason, ''), 'calls')) AS invalid_calls, "
                   "COUNTIF(h.invalid_reason = 'zero_yield') AS invalid_zero_yield, "
                   "SUM(h.calls) AS calls, SUM(h.calls_ok) AS calls_ok, SUM(h.items) AS items "
                   "FROM {core}.v_collection_health_current h WHERE h.day >= @since "
                   "GROUP BY h.day, h.market, h.lane_class ORDER BY h.day, h.market, h.lane_class"), s),
        Query("protocol_starts", "Every protocol of every series: its first and last day and valid days "
              "(each is a series start; protocol shown as a hash prefix)",
              fill("SELECT h.market, h.series, h.lane_class, SUBSTR(TO_HEX(SHA256(h.protocol)), 1, 10) AS protocol_hash, "
                   "LOGICAL_OR(ENDS_WITH(h.protocol, ':v2') OR STRPOS(h.protocol, 'proto=v2') > 0) AS v2_token, "
                   "MIN(h.day) AS first_day, MAX(h.day) AS last_day, COUNT(DISTINCT h.day) AS days, "
                   "COUNT(DISTINCT IF(h.valid, h.day, NULL)) AS valid_days "
                   "FROM {core}.v_collection_health_current h WHERE h.day >= @since "
                   "GROUP BY h.market, h.series, h.lane_class, protocol_hash ORDER BY h.market, h.series, first_day"), s),
        Query("protocol_run_lengths", "Per market and series: how long one protocol lasts. Fewer than 14 valid days "
              "under one protocol means the series test can never run on it (TRUST.md section 4 warm-up)",
              fill("WITH p AS (SELECT h.market, h.series, h.lane_class, h.protocol, "
                   "COUNT(DISTINCT IF(h.valid, h.day, NULL)) AS vd FROM {core}.v_collection_health_current h "
                   "WHERE h.day >= @since GROUP BY h.market, h.series, h.lane_class, h.protocol) "
                   "SELECT p.market, p.series, p.lane_class, COUNT(*) AS protocols, MAX(p.vd) AS max_valid_days, "
                   "APPROX_QUANTILES(p.vd, 2)[OFFSET(1)] AS median_valid_days, "
                   "COUNTIF(p.vd >= 14) AS protocols_14_valid, COUNTIF(p.vd >= 28) AS protocols_28_valid, "
                   "COUNTIF(p.vd >= 42) AS protocols_42_valid "
                   "FROM p GROUP BY p.market, p.series, p.lane_class ORDER BY p.market, p.series"), s),
        Query("sightings_by_lane", "Sightings and distinct posts per day, market and lane class; runs above 1 mean "
              "a day written by more than one collect run",
              fill("SELECT po.observed_date, po.market, po.lane_class, COUNT(*) AS sightings, "
                   "COUNT(DISTINCT po.post_id) AS posts, COUNT(DISTINCT po.run_id) AS runs "
                   "FROM {core}.post_observations po WHERE po.observed_date >= @since "
                   "GROUP BY po.observed_date, po.market, po.lane_class "
                   "ORDER BY po.observed_date, po.market, po.lane_class"), s),
        Query("posts_by_lane_platform_week", "Distinct posts per week, market, lane and platform",
              fill("SELECT DATE_TRUNC(po.observed_date, WEEK(MONDAY)) AS week, po.market, po.lane, po.platform, "
                   "COUNT(DISTINCT po.post_id) AS posts FROM {core}.post_observations po "
                   "WHERE po.observed_date >= @since GROUP BY week, po.market, po.lane, po.platform "
                   "ORDER BY week, po.market, po.lane, po.platform"), s),
        Query("located_share", "Posts by the day of their first sighting in a market (from @since), measured lanes "
              "or not, and how many carry a known location (geo_confidence 0.7 or more), by geo_source",
              fill("WITH f AS (SELECT po.post_id, po.market, MIN(po.observed_date) AS first_day, "
                   "LOGICAL_OR(po.lane_class IN ('unbiased_rank', 'panel')) AS measured "
                   "FROM {core}.post_observations po WHERE po.observed_date >= @since AND po.lane_class != 'legacy' "
                   "AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live') GROUP BY po.post_id, po.market) "
                   "SELECT f.first_day, f.market, ps.platform, f.measured, COUNT(*) AS posts, "
                   "COUNTIF(ps.geo_confidence >= 0.7) AS located, "
                   "COUNTIF(ps.geo_confidence >= 0.7 AND UPPER(ps.geo_market) = f.market) AS located_here, "
                   "COUNTIF(ps.geo_confidence >= 0.7 AND UPPER(ps.geo_market) != f.market) AS located_elsewhere, "
                   "COUNTIF(ps.geo_source = 'ext_region') AS src_ext_region, "
                   "COUNTIF(ps.geo_source = 'home_market') AS src_home_market, "
                   "COUNTIF(ps.geo_source = 'place_mention') AS src_place_mention, "
                   "COUNTIF(ps.geo_source = 'language') AS src_language, COUNTIF(ps.geo_source IS NULL) AS src_none "
                   "FROM f JOIN {core}.posts ps ON ps.post_id = f.post_id "
                   "GROUP BY f.first_day, f.market, ps.platform, f.measured "
                   "ORDER BY f.first_day, f.market, ps.platform, f.measured"), s),
        Query("post_dates", "Post date quality per platform and source regime: missing, epoch-like and far-past dates",
              fill("SELECT ps.platform, ps.source_regime, COUNT(*) AS posts, "
                   "COUNTIF(ps.published_at IS NULL) AS no_published_at, "
                   "COUNTIF(EXTRACT(YEAR FROM ps.published_at) = 1970) AS year_1970, "
                   "COUNTIF(ps.published_at < TIMESTAMP '2001-09-09') AS before_sep_2001, "
                   "COUNTIF(ps.post_date < DATE '2026-01-01') AS post_date_before_2026, "
                   "MIN(ps.post_date) AS min_post_date, MAX(ps.post_date) AS max_post_date, "
                   "COUNTIF(ps.creator_tier_at_post IS NULL) AS tier_null, COUNTIF(ps.creator_id IS NULL) AS creator_null "
                   "FROM {core}.posts ps GROUP BY ps.platform, ps.source_regime ORDER BY ps.platform, ps.source_regime")),
        Query("tier_mix", "Creator tier at post for posts first sighted per week in measured lanes, per market and "
              "platform (participation; NULL tier counted)",
              fill("WITH f AS (SELECT po.post_id, po.market, DATE_TRUNC(MIN(po.observed_date), WEEK(MONDAY)) AS week "
                   "FROM {core}.post_observations po WHERE po.observed_date >= @since "
                   "AND po.lane_class IN ('unbiased_rank', 'panel') GROUP BY po.post_id, po.market) "
                   "SELECT f.week, f.market, ps.platform, IFNULL(ps.creator_tier_at_post, 'none') AS tier, "
                   "COUNT(*) AS posts, COUNT(DISTINCT ps.creator_id) AS creators "
                   "FROM f JOIN {core}.posts ps ON ps.post_id = f.post_id "
                   "GROUP BY f.week, f.market, ps.platform, tier ORDER BY f.week, f.market, ps.platform, tier"), s),
        Query("creators_table", "creators rows by platform and tier, with home market known (the MERGE may be off)",
              fill("SELECT c.platform, IFNULL(c.tier, 'none') AS tier, COUNT(*) AS creators, "
                   "COUNTIF(c.home_market IS NOT NULL) AS home_market_known, "
                   "COUNTIF(IFNULL(c.coord_score, 0) >= 1) AS coord_flagged "
                   "FROM {core}.creators c GROUP BY c.platform, tier ORDER BY c.platform, tier")),
        Query("raw_calls_by_day", "SocialCrawl responses per day and market by HTTP class, and credits charged",
              fill("SELECT DATE(r.fetched_at) AS day, r.market, "
                   "CASE WHEN r.http_status BETWEEN 200 AND 299 THEN '2xx' WHEN r.http_status BETWEEN 400 AND 499 "
                   "THEN '4xx' WHEN r.http_status >= 500 THEN '5xx' ELSE 'other' END AS http_class, "
                   "COUNT(*) AS responses, ROUND(SUM(IFNULL(r.credits_charged, 0)), 1) AS credits, "
                   "COUNT(DISTINCT r.run_id) AS runs "
                   "FROM {core}.raw_responses r WHERE DATE(r.fetched_at) >= @since "
                   "GROUP BY day, r.market, http_class ORDER BY day, r.market, http_class"), s),
        Query("raw_calls_by_route", "Per route over the whole period: responses, 2xx share, days called",
              fill("SELECT r.route, COUNT(*) AS responses, COUNTIF(r.http_status BETWEEN 200 AND 299) AS ok, "
                   "COUNT(DISTINCT DATE(r.fetched_at)) AS days, MIN(DATE(r.fetched_at)) AS first_day, "
                   "MAX(DATE(r.fetched_at)) AS last_day FROM {core}.raw_responses r "
                   "WHERE DATE(r.fetched_at) >= @since GROUP BY r.route ORDER BY r.route"), s),
        Query("counter_reads", "item_counter_daily per series, unit and source: rows, items, dates, negative deltas, "
              "NULL values and vendor_history rows that restate past days",
              fill("SELECT c.market, c.series, c.lane_class, c.unit, c.source, COUNT(*) AS reads, "
                   "COUNT(DISTINCT c.item_id) AS items, COUNT(DISTINCT c.obs_date) AS obs_dates, "
                   "MIN(c.obs_date) AS first_obs, MAX(c.obs_date) AS last_obs, "
                   "COUNTIF(c.unit = 'delta' AND c.value < 0) AS negative_deltas, COUNTIF(c.value IS NULL) AS null_values, "
                   "COUNTIF(c.obs_date < DATE(c.available_at)) AS read_after_the_day, "
                   "COUNT(DISTINCT c.protocol) AS protocols "
                   "FROM {core}.item_counter_daily c WHERE c.obs_date >= DATE '2000-01-01' "
                   "GROUP BY c.market, c.series, c.lane_class, c.unit, c.source "
                   "ORDER BY c.market, c.series, c.unit, c.source")),
        Query("item_daily_rules", "Rule version of the current aggregate per day and lane class (a change is a "
              "break in how posts were linked and counted)",
              fill("SELECT i.metric_date, i.lane_class, i.rule_version, COUNT(*) AS rows_current, "
                   "COUNT(DISTINCT i.item_id) AS items, SUM(i.posts) AS posts "
                   "FROM {core}.v_item_daily_current i WHERE i.metric_date >= @since "
                   "GROUP BY i.metric_date, i.lane_class, i.rule_version ORDER BY i.metric_date, i.lane_class"), s),
        Query("item_daily_reruns", "Aggregate runs written per day in the raw table (re-runs, append only)",
              fill("SELECT i.metric_date, COUNT(DISTINCT i.run_id) AS runs, COUNT(*) AS rows_all "
                   "FROM {core}.item_daily i WHERE i.metric_date >= @since AND i.lane_class != 'legacy' "
                   "GROUP BY i.metric_date ORDER BY i.metric_date"), s),
        Query("item_counts_daily", "Items, posts, creators, tier and location totals per day, market and lane class "
              "from the current aggregate",
              fill("SELECT i.metric_date, i.market, i.lane_class, COUNT(DISTINCT i.item_id) AS items, "
                   "SUM(i.posts) AS posts, SUM(i.creators) AS creators, SUM(i.tier_posts.nano) AS nano, "
                   "SUM(i.tier_posts.micro) AS micro, SUM(i.tier_posts.mid) AS mid, SUM(i.tier_posts.macro) AS macro, "
                   "SUM(i.tier_posts.mega) AS mega, SUM(i.geo_known_posts) AS geo_known_posts, "
                   "SUM(i.local_posts) AS local_posts FROM {core}.v_item_daily_current i "
                   "WHERE i.metric_date >= @since GROUP BY i.metric_date, i.market, i.lane_class "
                   "ORDER BY i.metric_date, i.market, i.lane_class"), s),
        Query("legacy_in_core", "Legacy memory written into item_daily (lane_class legacy) per market",
              fill("SELECT i.market, COUNT(*) AS item_daily_rows, COUNT(DISTINCT i.item_id) AS items, "
                   "COUNT(DISTINCT i.metric_date) AS days, MIN(i.metric_date) AS first_day, "
                   "MAX(i.metric_date) AS last_day, SUM(i.posts) AS posts, "
                   "COUNTIF(i.first_post_at < TIMESTAMP '2000-01-01') AS first_post_before_2000 "
                   "FROM {core}.item_daily i WHERE i.lane_class = 'legacy' GROUP BY i.market ORDER BY i.market")),
        Query("legacy_enriched", "Old engine enriched_content per market: collection day span, junk publish dates",
              fill("SELECT UPPER(e.market) AS market, COUNT(*) AS rows_all, MIN(DATE(e.collected_at)) AS first_collected, "
                   "MAX(DATE(e.collected_at)) AS last_collected, "
                   "COUNTIF(e.published_at < TIMESTAMP '2000-01-01') AS published_before_2000, "
                   "COUNTIF(e.published_at > e.collected_at) AS published_after_collected, "
                   "COUNTIF(e.published_at IS NULL) AS no_published_at "
                   "FROM {legacy}.enriched_content e GROUP BY market ORDER BY market")),
        Query("legacy_enriched_monthly", "Old engine enriched_content rows per collection month, market and platform",
              fill("SELECT DATE_TRUNC(DATE(e.collected_at), MONTH) AS month, UPPER(e.market) AS market, "
                   "LOWER(e.platform) AS platform, COUNT(*) AS rows_all FROM {legacy}.enriched_content e "
                   "GROUP BY month, market, platform ORDER BY month, market, platform")),
        Query("legacy_seed_monthly", "Old engine seed_graph rows per month, market and term type",
              fill("SELECT DATE_TRUNC(g.trend_date, MONTH) AS month, UPPER(g.market) AS market, g.term_type, "
                   "COUNT(*) AS rows_all, COUNT(DISTINCT g.trend_date) AS days FROM {legacy}.seed_graph g "
                   "GROUP BY month, market, g.term_type ORDER BY month, market, g.term_type")),
        Query("cultural_map_summary", "Open cultural_map rows by kind and status: recurrences, variants, first_seen span",
              fill("SELECT m.kind, m.status, COUNT(*) AS items, SUM(IFNULL(m.recurrences, 0)) AS recurrences, "
                   "COUNTIF(IFNULL(m.recurrences, 0) > 0) AS items_recurred, "
                   "COUNTIF(m.parent_item_id IS NOT NULL) AS variant_children, "
                   "MIN(m.first_seen) AS first_seen_min, MAX(m.first_seen) AS first_seen_max, "
                   "COUNTIF(m.first_seen < DATE '2026-09-28') AS first_seen_before_sep28 "
                   "FROM {core}.cultural_map m WHERE m.valid_to IS NULL GROUP BY m.kind, m.status ORDER BY m.kind, m.status")),
        Query("label_churn", "cultural_map versions per day and kind, and how many changed the label from the "
              "previous version (counts only)", fill(
                  "WITH v AS (SELECT m.item_id, m.kind, m.valid_from, (m.label != LAG(m.label) OVER "
                  "(PARTITION BY m.item_id ORDER BY m.valid_from)) AS changed FROM {core}.cultural_map m) "
                  "SELECT DATE(v.valid_from) AS version_day, v.kind, COUNT(*) AS versions, "
                  "COUNTIF(v.changed) AS label_changes, COUNT(DISTINCT v.item_id) AS items "
                  "FROM v GROUP BY version_day, v.kind ORDER BY version_day, v.kind"), reads_content=True),
        Query("versions_per_item", "How many cultural_map versions an item has, bucketed, per kind",
              fill("WITH n AS (SELECT m.kind, m.item_id, COUNT(*) AS versions FROM {core}.cultural_map m "
                   "GROUP BY m.kind, m.item_id) SELECT n.kind, CASE WHEN n.versions = 1 THEN '1' WHEN n.versions <= 3 "
                   "THEN '2-3' WHEN n.versions <= 7 THEN '4-7' WHEN n.versions <= 14 THEN '8-14' ELSE '15+' END AS bucket, "
                   "COUNT(*) AS items FROM n GROUP BY n.kind, bucket ORDER BY n.kind, bucket")),
        Query("cluster_decisions", "Daily clusters per market by match kind (match, recurrence, variant, new)",
              fill("SELECT c.cluster_date, LOWER(c.market) AS market, c.match_kind, COUNT(*) AS clusters, "
                   "COUNT(DISTINCT c.item_id) AS items FROM {core}.clusters c WHERE c.cluster_date >= @since "
                   "GROUP BY c.cluster_date, market, c.match_kind ORDER BY c.cluster_date, market, c.match_kind"), s),
        Query("cluster_transitions", "Consecutive clusters of one item in one market (previous within 7 days): "
              "centroid cosine and keyword Jaccard, and how many pass on cosine with no keyword support", fill(
                  "WITH c AS (SELECT cl.cluster_date, LOWER(cl.market) AS market, cl.item_id, cl.keywords AS kw, "
                  "cl.centroid AS v FROM {core}.clusters cl WHERE cl.cluster_date >= @since "
                  "AND cl.item_id IS NOT NULL AND ARRAY_LENGTH(cl.centroid) > 0 "
                  "QUALIFY ROW_NUMBER() OVER (PARTITION BY cl.cluster_date, LOWER(cl.market), cl.item_id "
                  "ORDER BY cl.cluster_id) = 1), "
                  "t AS (SELECT b.cluster_date, b.market, "
                  "SAFE_DIVIDE((SELECT SUM(x * y) FROM UNNEST(a.v) x WITH OFFSET i JOIN UNNEST(b.v) y WITH OFFSET j "
                  "ON i = j), SQRT((SELECT SUM(x * x) FROM UNNEST(a.v) x)) * SQRT((SELECT SUM(y * y) FROM UNNEST(b.v) y))) "
                  "AS cosine, SAFE_DIVIDE((SELECT COUNT(DISTINCT k) FROM UNNEST(a.kw) k WHERE k != '' AND k IN "
                  "UNNEST(b.kw)), (SELECT COUNT(DISTINCT k) FROM UNNEST(ARRAY_CONCAT(a.kw, b.kw)) k WHERE k != '')) "
                  "AS jaccard FROM c a JOIN c b ON b.item_id = a.item_id AND b.market = a.market "
                  "AND b.cluster_date > a.cluster_date AND b.cluster_date <= DATE_ADD(a.cluster_date, INTERVAL 7 DAY) "
                  "WHERE TRUE QUALIFY ROW_NUMBER() OVER (PARTITION BY b.item_id, b.market, b.cluster_date "
                  "ORDER BY a.cluster_date DESC) = 1) "
                  "SELECT t.cluster_date, t.market, COUNT(*) AS transitions, "
                  "COUNTIF(t.cosine >= 0.82 AND IFNULL(t.jaccard, 0) < 0.10) AS high_cosine_no_keywords, "
                  "COUNTIF(t.cosine < 0.82) AS below_match_cosine, COUNTIF(IFNULL(t.jaccard, 0) = 0) AS no_keyword_overlap, "
                  "ROUND(APPROX_QUANTILES(t.cosine, 2)[OFFSET(1)], 3) AS median_cosine, "
                  "ROUND(APPROX_QUANTILES(IFNULL(t.jaccard, 0), 2)[OFFSET(1)], 3) AS median_jaccard "
                  "FROM t GROUP BY t.cluster_date, t.market ORDER BY t.cluster_date, t.market"), s, reads_content=True),
        Query("cluster_member_overlap", "Consecutive-day clusters of one item in one market: shared member posts "
              "(members are posts sighted that day, so low overlap is partly by design)", fill(
                  "WITH c AS (SELECT cl.cluster_date, LOWER(cl.market) AS market, cl.item_id, cl.cluster_id "
                  "FROM {core}.clusters cl WHERE cl.cluster_date >= @since AND cl.item_id IS NOT NULL), "
                  "m AS (SELECT c.cluster_date, c.market, c.item_id, cm.post_id FROM c "
                  "JOIN {core}.cluster_members cm ON cm.cluster_id = c.cluster_id), "
                  "n AS (SELECT m.cluster_date, m.market, m.item_id, COUNT(DISTINCT m.post_id) AS size FROM m "
                  "GROUP BY m.cluster_date, m.market, m.item_id), "
                  "sh AS (SELECT b.cluster_date, b.market, b.item_id, COUNT(DISTINCT b.post_id) AS shared FROM m a "
                  "JOIN m b ON b.item_id = a.item_id AND b.market = a.market AND b.post_id = a.post_id "
                  "AND b.cluster_date = DATE_ADD(a.cluster_date, INTERVAL 1 DAY) "
                  "GROUP BY b.cluster_date, b.market, b.item_id), "
                  "p AS (SELECT n2.cluster_date, n2.market, IFNULL(sh.shared, 0) AS shared, LEAST(n1.size, n2.size) AS smaller "
                  "FROM n n2 JOIN n n1 ON n1.item_id = n2.item_id AND n1.market = n2.market "
                  "AND n1.cluster_date = DATE_SUB(n2.cluster_date, INTERVAL 1 DAY) "
                  "LEFT JOIN sh ON sh.item_id = n2.item_id AND sh.market = n2.market AND sh.cluster_date = n2.cluster_date) "
                  "SELECT p.cluster_date, p.market, COUNT(*) AS transitions, COUNTIF(p.shared = 0) AS none_shared, "
                  "COUNTIF(p.shared >= 2) AS two_or_more_shared, COUNTIF(SAFE_DIVIDE(p.shared, p.smaller) >= 0.5) AS half_shared "
                  "FROM p GROUP BY p.cluster_date, p.market ORDER BY p.cluster_date, p.market"), s),
        Query("series_test_states", "Current series_test rows per day, market and lane class by baseline state and test",
              fill("SELECT t.metric_date, t.market, t.lane_class, t.baseline_state, t.test, COUNT(*) AS series, "
                   "COUNTIF(t.significant) AS significant FROM {core}.v_series_test_current t "
                   "WHERE t.metric_date >= @since GROUP BY t.metric_date, t.market, t.lane_class, t.baseline_state, t.test "
                   "ORDER BY t.metric_date, t.market, t.lane_class, t.baseline_state, t.test"), s),
        Query("item_states", "Current item_state per day and market: states, untested, eligible and novelty counts",
              fill("SELECT s.metric_date, s.market, COUNT(*) AS items, COUNTIF(s.untested) AS untested, "
                   "COUNTIF(s.state = 'new_to_42') AS new_to_42, COUNTIF(s.state = 'spike') AS spike, "
                   "COUNTIF(s.state = 'emerging') AS emerging, COUNTIF(s.state = 'rising') AS rising, "
                   "COUNTIF(s.state = 'peaking') AS peaking, COUNTIF(s.state = 'fading') AS fading, "
                   "COUNTIF(s.state = 'mainstream') AS mainstream, COUNTIF(s.state = 'on_the_boards') AS on_the_boards, "
                   "COUNTIF(s.state = 'recurring') AS recurring, COUNTIF(s.state = 'seasonal') AS seasonal, "
                   "COUNTIF(s.eligible) AS eligible, COUNTIF(s.novelty = 'recurrence') AS novelty_recurrence, "
                   "COUNTIF(s.novelty = 'variant') AS novelty_variant, COUNTIF(s.diffusion = 'bottom_up') AS bottom_up, "
                   "COUNTIF(s.diffusion = 'top_down') AS top_down "
                   "FROM {core}.v_item_state_current s WHERE s.metric_date >= @since "
                   "GROUP BY s.metric_date, s.market ORDER BY s.metric_date, s.market"), s),
        Query("forecast_record", "Forecasts per rule, target and horizon: issued, resolved, arrivals, first and last issue",
              fill("WITH f AS (SELECT x.forecast_id, x.rule, x.target, x.horizon, MIN(x.issue_date) AS issue_date, "
                   "LOGICAL_OR(x.observed_arrival IS NOT NULL) AS resolved, LOGICAL_OR(IFNULL(x.observed_arrival, FALSE)) "
                   "AS arrived, LOGICAL_OR(IFNULL(x.persistence_arrival, FALSE)) AS persistence FROM {agent}.forecasts x "
                   "GROUP BY x.forecast_id, x.rule, x.target, x.horizon) "
                   "SELECT f.rule, f.target, f.horizon, COUNT(*) AS issued, COUNTIF(f.resolved) AS resolved, "
                   "COUNTIF(f.arrived) AS arrived, COUNTIF(f.persistence) AS persistence_said_yes, "
                   "MIN(f.issue_date) AS first_issue, MAX(f.issue_date) AS last_issue FROM f "
                   "GROUP BY f.rule, f.target, f.horizon ORDER BY f.rule, f.target, f.horizon")),
        Query("waves", "v_item_waves per market: waves, items with 2 or more waves, and waves resting on legacy days "
              "(before the first collection_health day)", fill(
                  "WITH s AS (SELECT MIN(h.day) AS start_day FROM {core}.collection_health h) "
                  "SELECT w.market, COUNT(*) AS waves, COUNT(DISTINCT w.item_id) AS items, "
                  "COUNTIF(w.peak_posts >= 8) AS waves_peak_8, "
                  "COUNTIF(w.wave_end < s.start_day) AS waves_before_collect, "
                  "COUNTIF(w.wave_start < s.start_day AND w.wave_end >= s.start_day) AS waves_across_start, "
                  "COUNTIF(w.wave_start >= s.start_day) AS waves_after_start, ANY_VALUE(s.start_day) AS collect_start "
                  "FROM {core}.v_item_waves w CROSS JOIN s GROUP BY w.market ORDER BY w.market")),
        Query("items_with_waves", "Items by how many waves they have per market (2 or more makes Recurring possible)",
              fill("WITH n AS (SELECT w.market, w.item_id, COUNT(*) AS waves FROM {core}.v_item_waves w "
                   "GROUP BY w.market, w.item_id) SELECT n.market, CASE WHEN n.waves = 1 THEN '1' WHEN n.waves = 2 "
                   "THEN '2' ELSE '3+' END AS bucket, COUNT(*) AS items FROM n GROUP BY n.market, bucket "
                   "ORDER BY n.market, bucket")),
        Query("post_item_links", "post_items links by via, with linked_on and link_market present or not",
              fill("SELECT p.via, p.linked_on IS NOT NULL AS has_linked_on, p.link_market IS NOT NULL AS has_link_market, "
                   "COUNT(*) AS links, COUNT(DISTINCT p.item_id) AS items FROM {core}.post_items p "
                   "GROUP BY p.via, has_linked_on, has_link_market ORDER BY p.via")),
        Query("enrichment_coverage", "Posts first sighted per week in measured lanes and how many have an enrichment "
              "row with tone or formats (embedding not read, to keep bytes small)", fill(
                  "WITH f AS (SELECT po.post_id, po.market, DATE_TRUNC(MIN(po.observed_date), WEEK(MONDAY)) AS week "
                  "FROM {core}.post_observations po WHERE po.observed_date >= @since "
                  "AND po.lane_class IN ('unbiased_rank', 'panel') GROUP BY po.post_id, po.market), "
                  "e AS (SELECT pe.post_id, LOGICAL_OR(pe.tone IS NOT NULL OR ARRAY_LENGTH(pe.formats) > 0) AS enriched, "
                  "LOGICAL_OR(ARRAY_LENGTH(pe.formats) > 0) AS has_formats, LOGICAL_OR(ARRAY_LENGTH(pe.sounds) > 0) "
                  "AS has_sounds FROM {core}.post_enrichment pe GROUP BY pe.post_id) "
                  "SELECT f.week, f.market, COUNT(*) AS posts, COUNTIF(IFNULL(e.enriched, FALSE)) AS enriched, "
                  "COUNTIF(IFNULL(e.has_formats, FALSE)) AS with_formats, COUNTIF(IFNULL(e.has_sounds, FALSE)) AS with_sounds "
                  "FROM f LEFT JOIN e ON e.post_id = f.post_id GROUP BY f.week, f.market ORDER BY f.week, f.market"), s),
    ]
    return qs


def render_rows(rows):
    if not rows:
        return ["(no rows)"]
    cols = list(rows[0].keys())
    out = ["| " + " | ".join(cols) + " |", "|" + ("-" * 3 + "|") * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |")
    return out


def render(log, when, since):
    processed = sum(e["processed"] or 0 for e in log)
    lines = ["# 42 history audit (aggregate counts only)", "",
             f"Run at {when} by history_audit.py, project {PROJECT}, location {LOCATION}, since {since}. Read only: "
             f"every query is one SELECT, dry-run first, refused above {MAX_BYTES:,} bytes and run with "
             f"maximum_bytes_billed at that cap; the run stops before {TOTAL_BYTES:,} bytes. Rows hold counts, dates "
             "and code-level categories only; protocols are sha256 prefixes.", "",
             f"{len(log)} queries, {processed:,} bytes processed in total.", ""]
    for e in log:
        lines += [f"## {e['name']}", "", e.get("purpose", ""), ""]
        if e["rows"] is None:
            lines += [f"Not shown: {e['status']}" + (f" ({e['problem']})" if e.get("problem") else "") + ".", ""]
            continue
        lines += render_rows(e["rows"])
        if e.get("truncated"):
            lines.append(f"Only the first {ROW_LIMIT} rows are shown.")
        lines.append("")
    lines += ["## Queries", ""]
    for i, e in enumerate(log, 1):
        ran = ("not run" if e["processed"] is None else
               f"{bytes_text(e['processed'])} bytes processed, {bytes_text(e['billed'])} bytes billed")
        params = ", ".join(f"@{k} = {v}" for k, v in sorted(e["params"].items()))
        lines.append(f"{i}. {e['name']}: {e['status']}: estimate {bytes_text(e['estimate'])} bytes, {ran}"
                     + (f", {params}" if params else ""))
        lines += ["", "```sql", e["sql"], "```", ""]
    return "\n".join(lines)


def audit(client, since=DEFAULT_SINCE):
    log, budget = [], {"spent": 0}
    for q in queries(since):
        try:
            run_query(client, q, log, budget)
        except (Refused, GoogleAPIError) as error:
            log[-1]["problem"] = first_line(error)
        log[-1]["purpose"] = q.purpose
        if log[-1]["status"].startswith("refused") and "budget" in log[-1].get("problem", ""):
            break
    return log


def plan(since=DEFAULT_SINCE):
    lines = [f"Planned queries, project {PROJECT}, location {LOCATION}, since {since}. Each is a dry run first, "
             f"refused if the estimate is over {MAX_BYTES:,} bytes, then run with maximum_bytes_billed at that cap; "
             f"the run stops before {TOTAL_BYTES:,} bytes in all.", ""]
    for i, q in enumerate(queries(since), 1):
        check_statement(q)
        lines += [f"{i}. {q.name}: {q.purpose}", f"   {q.sql}", ""]
    return "\n".join(lines)


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description="Read only history audit of the 42 warehouse, aggregate counts only.")
    parser.add_argument("--plan", action="store_true", help="print the planned queries; no client, no network")
    parser.add_argument("--out", type=Path, default=Path("history-audit.md"), help="markdown file to write")
    parser.add_argument("--since", type=dt.date.fromisoformat, default=DEFAULT_SINCE,
                        help="first day of the SocialCrawl-era partitions read")
    args = parser.parse_args(argv)
    if args.plan:
        print(plan(args.since))
        return 0
    client = client or bigquery.Client(project=PROJECT, location=LOCATION)
    when = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = audit(client, args.since)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(log, when, args.since), encoding="utf-8")
    print(f"wrote {args.out}: {len(log)} queries, {sum(e['processed'] or 0 for e in log):,} bytes processed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
