"""The hourly Breaking rule (ENGINE.md section 3, "Later"): intraday spikes in posts new to 42.

Entry point: python -m core.detect.breaking [--hour YYYY-MM-DDTHH:00], the Cloud Run job f42-breaking, run hourly
at :05 by its own schedule (core/setup/schedule.py), never by the detect job. --hour is the start of the last
hour judged, in SAST unless the text names its zone; without it the job judges the last complete SAST hour.

sql/breaking.sql counts posts in item_daily's unit (DATA.md 3.4): each post once per market, item, lane class and
panel series, at its first sighting there, with its items from post_items. The window is [--hour minus 5 hours,
--hour plus 1 hour), in the measured lanes (unbiased_rank and panel) of ZA, NG and KE; a placebo or agent_live
sighting never counts. A post the pulse sees again, or an old post still on a feed, is not new and does not count.
item_hourly is not read: it counts every post each pull shows, so it is not the baseline's unit. An item in a
market is Breaking when all three hold:
- posts6, its posts first sighted in the window, is at least 3 times expected6, the expected six-hour share of
  its daily baseline. Each platform and lane class the item has new posts on gets its own baseline: the item's
  mean daily posts there in item_daily over the days, among the 28 SAST days before the hour's day, on which
  every series those posts were first sighted through ran and was valid (DATA.md 3.3). A failed, drifted or not
  yet started series never adds zero-post days, and a sibling series that ran longer adds none either (DATA.md
  3.2 and 3.4). Health rows are matched by series, so a panel with no platform (prism/profiles) covers every
  platform its posts are on; a panel baseline counts only those series' item_daily rows. With fewer than 14 such
  days that platform and lane has no baseline, and then the item has no expected6 and is not Breaking. expected6
  adds up the baselines of the item's platforms and lanes with new posts. DATA.md defines no hour-of-day profile,
  so the share is 6/24.
- creators6, the distinct creators of those posts, is at least 8.
- it was seen on an unbiased_rank list, or on at least 2 platforms.

A post counts only once post_items names its item. The aggregate step writes post_items for posts first sighted
on its run date when it runs at 05:00, and the pulse (core/collect/pulse_job.py) appends the same links for the
posts it finds, so a post first seen by a pulse is seen here from that pulse on.

An item with no posts in the baseline on valid days has expected6 0 and ratio NULL, and is Breaking on the
creator and sighting tests alone. Anything that shows such a row must label it "new, no prior posts" and never
show a ratio for it; ratio stays NULL.

Each Breaking item is appended to intelligence_42_core.breaking_signals (hour, market, item_id, posts6,
creators6, expected6, ratio, platforms, run_id, rule_version), then one runs row with stage 'breaking' and
run_date the hour's SAST date; counts hold model_usd 0.0, since no model is called. Append only. An hour, market
and item already written by an ok breaking run is not written again, so a rerun adds no duplicates; rows of a
failed run never count as written.

core/schema/core.sql declares breaking_signals and v_breaking_signals_current. Until they are applied on the
project, the job computes the rows, prints them, appends a 'skipped' runs row with the reason and exits 0. Any
other failure appends a 'failed' runs row and exits 1.

Readers go through v_breaking_signals_current (CURRENT_VIEW_SQL), which keeps only rows of ok breaking runs, so
rows of a run that failed after its INSERT are never read. The job does not create the view; core/schema/apply.py
does, from the same body in core.sql.
"""

import argparse
import json
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from core.collect.chain import SAST

from . import aggregate, runs, sqlrun
from .sqlrun import AGENT, CORE

PROJECT = "ogilvy-trends-v2"
TABLE = "breaking_signals"
SQL = Path(__file__).parent / "sql" / "breaking.sql"
RULE_VERSION = "breaking-1"
RATIO = 3.0
MIN_CREATORS = 8
MIN_PLATFORMS = 2
SHARE = 6 / 24
MIN_DAYS = 14

FIELDS = (("hour", "TIMESTAMP"), ("market", "STRING"), ("item_id", "STRING"), ("posts6", "INT64"),
          ("creators6", "INT64"), ("expected6", "FLOAT64"), ("ratio", "FLOAT64"), ("platforms", "INT64"),
          ("run_id", "STRING"), ("rule_version", "STRING"))

APPEND_SQL = """
INSERT INTO {core}.breaking_signals (hour, market, item_id, posts6, creators6, expected6, ratio, platforms, run_id,
  rule_version)
SELECT n.hour, n.market, n.item_id, n.posts6, n.creators6, n.expected6, n.ratio, n.platforms, n.run_id,
  n.rule_version
FROM UNNEST(@rows) n
"""

# The rows of ok breaking runs only; a run that failed after its INSERT leaves rows no reader sees.
CURRENT_VIEW_SQL = """
CREATE OR REPLACE VIEW {core}.v_breaking_signals_current AS
SELECT s.* FROM {core}.breaking_signals s
JOIN {agent}.runs r ON r.run_id = s.run_id AND r.stage = 'breaking' AND r.status = 'ok'
"""

# The markets and items of the hour already written by an ok breaking run.
DONE_SQL = """
SELECT DISTINCT s.market, s.item_id FROM {core}.breaking_signals s
JOIN {agent}.runs r ON r.run_id = s.run_id
WHERE s.hour = @hour AND r.stage = 'breaking' AND r.status = 'ok'
"""

_HOUR = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}(:\d{2}(:\d{2})?)?(Z|[+-]\d{2}:\d{2})?$")


def parse_hour(text):
    """The hour start text names, SAST unless it carries a zone."""
    if not _HOUR.match(text):
        raise argparse.ArgumentTypeError(f"{text} is not an hour, YYYY-MM-DDTHH:00")
    hour = datetime.fromisoformat(text)
    hour = hour.replace(tzinfo=SAST) if hour.tzinfo is None else hour.astimezone(SAST)
    if (hour.minute, hour.second, hour.microsecond) != (0, 0, 0):
        raise argparse.ArgumentTypeError(f"{text} is not on the hour")
    return hour


def hour_for(now=None):
    """The start of the last complete SAST hour before now."""
    now = (now or datetime.now(timezone.utc)).astimezone(SAST)
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def values(hour):
    return {"hour": hour, "day": hour.astimezone(SAST).date(), "share": SHARE, "ratio": RATIO,
            "min_creators": MIN_CREATORS, "min_platforms": MIN_PLATFORMS, "min_days": MIN_DAYS}


def params(hour):
    """breaking.sql's query parameters for hour."""
    return [sqlrun._param(k, v) for k, v in values(hour).items()]


def compute(client, hour, core=CORE, agent=AGENT):
    """Every market and item seen in a measured lane in the six hours to hour, with its figures and breaking."""
    return sqlrun.query(client, SQL.read_text(encoding="utf-8"), values(hour), core=core, agent=agent)


def table_exists(client, core=CORE):
    try:
        client.get_table(f"{core}.{TABLE}")
    except NotFound:
        return False
    return True


def run(client, hour, *, core=CORE, agent=AGENT):
    """Judge hour and append its new Breaking items, then the runs row. Returns the run_id, status, error, counts
    and the rows written or, when the table is missing, the rows that would have been. On any failure appends a
    'failed' runs row and re-raises."""
    day = hour.astimezone(SAST).date()
    run_id = runs.new_run_id("breaking", day)
    started = runs.now()
    counts = {"hour": hour.isoformat()}
    try:
        exists = table_exists(client, core)
        found = compute(client, hour, core=core, agent=agent)
        counts["candidates"] = len(found)
        done = set()
        if exists:
            done = {(r["market"], r["item_id"]) for r in sqlrun.query(client, DONE_SQL, {"hour": hour},
                                                                      core=core, agent=agent)}
        counts["already_written"] = [list(k) for k in sorted(done)]
        rows = [{"hour": hour, "market": r["market"], "item_id": r["item_id"], "posts6": r["posts6"],
                 "creators6": r["creators6"], "expected6": r["expected6"], "ratio": r["ratio"],
                 "platforms": r["platforms"], "run_id": run_id, "rule_version": RULE_VERSION}
                for r in found if r["breaking"] and (r["market"], r["item_id"]) not in done]
        if not exists:
            status = "skipped"
            error = f"{core}.{TABLE} does not exist yet (lane L1); {len(rows)} rows printed, none written"
        else:
            if rows:
                aggregate._run(client, APPEND_SQL, [aggregate._struct_array("rows", rows, FIELDS)], core, agent)
            status, error = "ok", None
        counts["written"] = [[r["market"], r["item_id"]] for r in rows] if status == "ok" else []
    except Exception as e:
        runs.append(client, run_id, "breaking", day, "failed", started, runs.now(), counts,
                    error=f"{type(e).__name__}: {e}", agent=agent)
        raise
    runs.append(client, run_id, "breaking", day, status, started, runs.now(), counts, error=error, agent=agent)
    return {"run_id": run_id, "status": status, "error": error, "counts": counts, "rows": rows}


def main(argv=None, client=None, now=None, core=CORE, agent=AGENT):
    """Exit code: 0 when the hour is judged or skipped, 1 on any failure."""
    ap = argparse.ArgumentParser(prog="python -m core.detect.breaking",
                                 description="Append the hour's Breaking items to breaking_signals.")
    ap.add_argument("--hour", type=parse_hour,
                    help="start of the last hour judged, YYYY-MM-DDTHH:00 in SAST; default the last complete hour")
    a = ap.parse_args(argv)
    hour = a.hour or hour_for(now)
    if client is None:
        client = bigquery.Client(project=PROJECT)
    try:
        res = run(client, hour, core=core, agent=agent)
    except Exception:
        traceback.print_exc()
        return 1
    print(json.dumps({"breaking": hour.isoformat(), **res}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
