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

import numpy as np

from .. import stats
from . import duck
from .fixtures import D, day, run
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


DUCK_TYPES = {"STRING": "VARCHAR", "DATE": "DATE", "FLOAT": "DOUBLE", "BOOLEAN": "BOOLEAN", "INTEGER": "BIGINT"}


def early_signal_ddl():
    """DuckDB DDL for core.early_signal from the module's BigQuery schema, or None where the module does not
    exist (the base commit)."""
    try:
        from .. import early_signal
    except ImportError:
        return None
    cols = ", ".join(f"{f.name} {DUCK_TYPES[f.field_type]}" for f in early_signal.SCHEMA)
    return f"CREATE TABLE core.early_signal ({cols})"


def build(con):
    """Load the world, run the series test through run_stats with ZA|facebook|panel switched on, then the
    state SQL; returns the item_state rows by item."""
    make_world().load(con)
    duck.load(con, "core.test_switch", [SW_PANEL])
    stats.run_stats(StatsClient(con), D, STATS_RUN, RULE, core="core")
    duck.load(con, "agent.runs", [run("stats", D, STATS_RUN)])
    return detect(con)


def _plain(value):
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    return str(value)


def dump(con, skip=("early_signal",)):
    """Canonical JSON bytes of every core table and agent.runs, rows sorted, apart from the tables in skip."""
    tables = [r[0] for r in con.execute(
        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema IN ('core', 'agent') ORDER BY 1").fetchall()]
    out = {}
    for t in tables:
        if t.split(".")[1] in skip:
            continue
        cur = con.execute(f"SELECT * FROM {t} ORDER BY ALL")
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
