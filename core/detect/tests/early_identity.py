"""A detect world for the byte-identity proof of the early signal (core/detect/early_signal.py).

The world has 40 stable negative binomial panel items (so dispersion pools), and four items that heat up in
different ways, each with the posts the Rising floors need. build() runs the series test through run_stats and
the state SQL on DuckDB, exactly as test_detect_stats and test_detect_states do, and dump() returns every table
detect wrote as canonical JSON, apart from the early_signal table, so the same function gives the digest at the
base commit (where that table does not exist) and on the branch.
"""

import datetime as _dt
import hashlib
import json
import re
from pathlib import Path

import numpy as np

from .. import stats
from . import duck
from .fixtures import D, at, day, run
from .test_detect_states import RULE, STATS_RUN, World, detect, sql_statements
from .test_detect_stats import SW_PANEL, StatsClient

FIRST = 56
MU, ALPHA = 10, 0.1
HEATERS = {            # item: the multiplier of its usual level on day(i), i days before D
    "ramp": {5: 1.3, 4: 1.69, 3: 2.2, 2: 2.86, 1: 3.71, 0: 3.71},
    "step": {i: 2.0 for i in range(7, -1, -1)},
    "jump": {0: 4.0},
    "slow": dict(zip(range(9, -1, -1), (1.15, 1.25, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.9, 1.9))),
    **{f"jump{j}": {0: 3.0 + 0.2 * j} for j in range(1, 23)},     # enough eligible items for worth_pct
}


def posts_for(w, item):
    """Five creators, 8 posts over the last 3 days, top creator 25%: the Rising floors."""
    for j, n in enumerate((2, 2, 2, 1, 1)):
        for k in range(n):
            w.posts(item, k, [f"{item}-c{j}"], lane_class="panel", lane="panel", platform="facebook")


def make_world():
    w = World()
    w.protocol("panel_fb_hub", FIRST, platform="facebook", lane_class="panel", k=1.0)
    rng = np.random.default_rng(20261008)
    n = 1 / ALPHA
    items = [f"s{j}" for j in range(40)] + list(HEATERS)
    for item in items:
        ys = rng.negative_binomial(n, n / (n + MU), size=FIRST + 1)
        for i in range(FIRST, -1, -1):
            y = int(ys[FIRST - i])
            if item in HEATERS and i in HEATERS[item]:
                y = int(np.ceil(MU * HEATERS[item][i]))
            w.daily(item, i, y, platform="facebook")
        posts_for(w, item)
    return w


def connect():
    """A DuckDB detect database with the waves view and an empty test_switch table."""
    con = duck.connect()
    con.execute("SET threads = 1")
    for stmt in sql_statements("waves.sql"):
        con.execute(duck.create_statement(stmt))
    con.execute("CREATE TABLE core.test_switch (market VARCHAR NOT NULL, platform VARCHAR NOT NULL, "
                "lane_class VARCHAR NOT NULL, switched_on DATE, backtest_run_id VARCHAR, rule_version VARCHAR)")
    ddl = early_signal_ddl()
    if ddl:
        con.execute(ddl)
    return con


DUCK_TYPES = {"STRING": "VARCHAR", "DATE": "DATE", "FLOAT64": "DOUBLE", "BOOL": "BOOLEAN", "INT64": "BIGINT"}
SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schema" / "early_signal.sql"


def early_signal_ddl():
    """DuckDB DDL for core.early_signal read from core/schema/early_signal.sql, or None where that file does not
    exist (the base commit)."""
    if not SCHEMA_FILE.is_file():
        return None
    body = SCHEMA_FILE.read_text(encoding="utf-8").split("(", 1)[1].rsplit(")", 1)[0]
    cols = ", ".join(f"{n} {DUCK_TYPES[t]}" for n, t in re.findall(r"(\w+)\s+(STRING|DATE|FLOAT64|BOOL|INT64)", body))
    return f"CREATE TABLE core.early_signal ({cols})"


def build(con):
    """Load the world, run the series test through run_stats with ZA|facebook|panel switched on, then the
    state SQL; returns the item_state rows by item."""
    make_world().load(con)
    duck.load(con, "core.test_switch", [SW_PANEL])
    # A switch row is in force only once the backtest run it cites has an ok runs row (core/detect/sql/stats.sql).
    # That row is a fixture, not something detect writes, so dump() leaves it out and the digests stay the base ones.
    duck.load(con, "agent.runs", [{**run("backtest", day(3), run_id=SW_PANEL["backtest_run_id"]),
                                   "finished_at": at(day(3))}])
    stats.run_stats(StatsClient(con), D, STATS_RUN, RULE, core="core", agent="agent")
    duck.load(con, "agent.runs", [run("stats", D, STATS_RUN)])
    return detect(con)


def _plain(value):
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    return str(value)


# The tables and columns the base commit 24fc597 had in this world. Later lanes add tables and columns (the locality
# tables, post_items.linked_on, the item_state switch columns, and so on) that detect writes empty or null here, so the
# digests pinned from the base commit compare what the base commit had and nothing a later lane grew.
BASE_SCHEMA = {
    "agent.briefs": ("brief_date", "market", "run_id", "published_at", "status", "payload", "rule_version",),
    "agent.engine_scorecard": ("week_start", "week_end", "market", "run_id", "rule_version", "time_to_detect",
        "lead_time", "precision", "recall", "breadth_platforms", "expansion_cluster_share",
        "expansion_platform_share", "expansion_language_share", "cost_per_confirmed",),
    "agent.forecasts": ("forecast_id", "item_id", "market", "target", "issue_date", "horizon", "rule", "prob",
        "predicted_arrival", "persistence_arrival", "resolve_date", "observed_arrival",),
    "agent.runs": ("run_id", "stage", "run_date", "status", "started_at", "finished_at", "counts", "error",
        "question", "tier", "plan", "calls", "credits", "tokens", "seconds", "outcome", "answer", "record",),
    "agent.watch_matches": ("watch_id", "match_date", "item_id", "market", "method", "run_id",),
    "agent.watches": ("watch_id", "created_at", "status_at", "who", "target", "market", "rule", "label", "status",),
    "core.breaking_signals": ("hour", "market", "item_id", "posts6", "creators6", "expected6", "ratio", "platforms",
        "run_id", "rule_version",),
    "core.breakout_signals": ("metric_date", "market", "item_id", "run_id", "creators", "posts",
        "evidence_post_ids", "top_ratio", "held_flagged", "rule_version",),
    "core.calendar": ("moment_date", "market", "name", "kind", "source", "item_ids",),
    "core.cluster_members": ("cluster_id", "post_id", "probability",),
    "core.clusters": ("cluster_date", "cluster_id", "market", "item_id", "match_kind",),
    "core.collection_health": ("day", "market", "platform", "route", "series", "protocol", "lane_class", "calls",
        "calls_ok", "units_planned", "units_ok", "items", "ref_items", "ref_days", "k", "valid", "invalid_reason",
        "located_share", "run_id",),
    "core.coord_signals": ("metric_date", "item_id", "market", "run_id", "signal", "component_id", "accounts",
        "item_posts_share", "rule_version",),
    "core.creators": ("creator_id", "platform", "handle", "account_created_at", "home_market", "verified_region",
        "coord_score",),
    "core.cultural_map": ("item_id", "kind", "canonical_key", "label", "aliases", "parent_item_id", "centroid",
        "first_seen", "first_seen_market", "first_seen_platform", "last_seen", "recurrences", "lifecycle", "status",
        "rejected_until", "valid_from", "valid_to",),
    "core.item_counter_daily": ("obs_date", "market", "platform", "item_id", "series", "route", "protocol",
        "is_board", "lane_class", "unit", "pull_seq", "value", "source", "observed_at", "available_at", "run_id",),
    "core.item_daily": ("metric_date", "market", "platform", "item_id", "lane_class", "series", "protocol", "posts",
        "creators", "unflagged_creators", "engagement", "tier_posts", "first_post_at", "geo_known_posts",
        "local_posts", "source_regime", "available_at", "run_id", "rule_version",),
    "core.item_hourly": ("market", "item_id", "platform", "hour", "posts", "creators", "lane_class", "run_id",),
    "core.item_state": ("metric_date", "market", "item_id", "kind", "state_raw", "state", "untested",
        "main_series_id", "main_y", "main_mu", "main_ratio", "q_min", "sig_days3", "creators3", "posts3",
        "top_creator_share3", "authenticity", "share_flags", "sponsored_share", "geo_status", "local_share",
        "geo_known_posts7", "spread_platforms", "found_platforms", "markets_hot", "lead_market", "diffusion",
        "novelty", "last_wave", "moment", "eligible", "worth_raw", "worth_pct", "run_id", "rule_version",
        "base_state",),
    "core.media": ("sha256", "post_id", "gcs_uri", "kind",),
    "core.post_enrichment": ("post_id", "embedding", "langs", "tone", "stance", "sponsored", "near_dup_size",),
    "core.post_items": ("post_id", "item_id", "via",),
    "core.post_observations": ("post_id", "observed_at", "observed_date", "market", "platform", "route", "series",
        "protocol", "lane", "lane_class", "seed_key", "pull_seq", "rank", "views", "likes", "comments", "shares",
        "run_id",),
    "core.posts": ("post_id", "platform", "native_id", "url", "creator_id", "creator_tier_at_post", "text",
        "transcript", "hashtags", "sound_id", "thumbnail_url", "duration_s", "published_at", "post_date", "views",
        "likes", "comments", "shares", "engagement", "geo_market", "geo_confidence", "geo_source", "geo_scope",
        "vendor", "endpoint", "source_regime", "vendor_labels", "run_id",),
    "core.raw_responses": ("run_id", "job", "market", "route", "params_hash", "lane", "seed_key", "fetched_at",
        "http_status", "credits_quoted", "credits_charged", "cache_hit", "body",),
    "core.seed_queue": ("seed_date", "market", "item_id", "query", "kind", "lane",),
    "core.series_test": ("metric_date", "series_id", "item_id", "market", "platform", "series", "protocol",
        "lane_class", "kind", "y", "trials", "obs_prior", "obs28", "first_measured", "baseline_state", "hist_mean",
        "med", "v3", "v7", "peak28", "vel", "accel", "z_display", "test", "mu", "alpha", "weekday_factor",
        "mu_prior", "ratio", "p_mid", "q", "significant", "run_id", "rule_version",),
    "core.source_market_fixture": ("post_id", "source_markets", "source_sightings",),
    "core.suppressed_fixture": ("creator_id",),
    "core.test_switch": ("market", "platform", "lane_class", "switched_on", "backtest_run_id", "rule_version",),
}


def dump(con, skip=("early_signal",)):
    """Canonical JSON bytes of every table and column the base commit had (BASE_SCHEMA), rows sorted, apart from the
    tables in skip."""
    tables = [r[0] for r in con.execute(
        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema IN ('core', 'agent') ORDER BY 1").fetchall()]
    out = {}
    for t in tables:
        if t.split(".")[1] in skip or t not in BASE_SCHEMA:
            continue
        where = f" WHERE run_id != '{SW_PANEL['backtest_run_id']}'" if t == "agent.runs" else ""
        cur = con.execute(f"SELECT {', '.join(BASE_SCHEMA[t])} FROM {t}{where} ORDER BY ALL")
        cols = [c[0] for c in cur.description]
        out[t] = [dict(zip(cols, row)) for row in cur.fetchall()]
    return json.dumps(out, sort_keys=True, default=_plain, separators=(",", ":")).encode("utf-8")


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def today_digest():
    """The Today payload built from the fixture store, the same way test_today builds it."""
    from core.api import today
    from core.api.store import FixtureStore

    return digest(json.dumps(today.build_today(FixtureStore(), "2026-09-30"), sort_keys=True,
                             separators=(",", ":"), default=_plain).encode("utf-8"))


def state_rows(rows):
    """The part of item_state a strategist reads: state, worth, ranks and eligibility, by item."""
    keys = ("state_raw", "state", "base_state", "eligible", "worth_raw", "worth_pct", "untested", "sig_days3",
            "main_ratio", "q_min")
    return {item: {k: r[k] for k in keys} for item, r in sorted(rows.items())}


def main():
    con = connect()
    rows = build(con)
    print(json.dumps({"items": len(rows), "states": {k: v["state"] for k, v in sorted(rows.items())
                                                       if v["state"]},
                      "tables_sha256": digest(dump(con)),
                      "state_sha256": digest(json.dumps(state_rows(rows), sort_keys=True, default=_plain).encode()),
                      "today_sha256": today_digest()}, indent=1))


if __name__ == "__main__":
    main()
