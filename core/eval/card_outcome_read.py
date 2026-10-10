"""The capped, read-only fetch of the card and hold outcome inputs (core/eval/card_outcome.py), shared by the manual
runner (ops/card_outcome_report.py) and the weekly learn step (core/detect/learn.py), so both read under one set of
byte caps.

    read_inputs(client, queries, start, end, core=, agent=, deadline=) -> ({name: rows}, [one meta dict per query])
    briefs(rows)                              -> the briefs query rows as agent.briefs rows with a payload

Every query is checked to be a plain SELECT or WITH before the first call, each is dry run first, and each runs with
maximum_bytes_billed. An estimate over QUERY_CAP, or estimates that together pass TOTAL_CAP, raise Refused before the
real run. Nothing is written to the warehouse.

The queries hold {core} and {agent} where the dataset names go, and read_inputs fills them from the caller, so a run
with other datasets reads those and never a production one. A deadline (a time.monotonic() value) bounds the whole
read: no call starts after it and none waits past it, and DeadlineExceeded is raised.
"""

import json
import re
import time
from datetime import timedelta
from pathlib import Path

from google.cloud import bigquery

from core.detect.sqlrun import AGENT, CORE, render

from . import card_outcome as co

GB = 1024 ** 3
QUERY_CAP = 5 * GB
TOTAL_CAP = 20 * GB
LATER_DAYS = max(co.HORIZONS)
QUERIES = co.split_queries((Path(__file__).parent / "sql" / "card_outcome.sql").read_text(encoding="utf-8"))
WRITES = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|EXPORT|CALL|EXECUTE|LOAD|GRANT|BEGIN|DECLARE|SET)\b", re.I)


class Refused(Exception):
    pass


class DeadlineExceeded(Refused):
    pass


def time_left(deadline, cap):
    """The seconds a call may wait: cap, or what is left to the deadline when that is less."""
    if deadline is None:
        return cap
    left = deadline - time.monotonic()
    if left <= 0:
        raise DeadlineExceeded("the read passed its deadline")
    return min(cap, left)


def check_select(name, sql):
    body = re.sub(r"--[^\n]*", "", sql).strip()
    if not re.match(r"(SELECT|WITH)\b", body, re.I) or WRITES.search(body):
        raise Refused(f"query {name} is not a plain SELECT")


def params(start, end):
    last = end + timedelta(days=LATER_DAYS)
    return [bigquery.ScalarQueryParameter(k, "DATE", v) for k, v in (("start", start), ("end", end), ("last", last))]


def read_one(client, name, sql, query_params, query_cap, spent_estimate, total_cap, deadline=None):
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=query_params, dry_run=True, maximum_bytes_billed=query_cap),
        timeout=time_left(deadline, 60))
    estimate = dry.total_bytes_processed
    if estimate is None or estimate > query_cap:
        raise Refused(f"{name}: estimate {estimate} is over the {query_cap} byte cap")
    if spent_estimate + estimate > total_cap:
        raise Refused(f"{name}: estimates would pass the {total_cap} byte total cap")
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=query_params, maximum_bytes_billed=query_cap), timeout=time_left(deadline, 60))
    rows = [dict(r) for r in job.result(timeout=time_left(deadline, 300))]
    return rows, {"name": name, "job_id": job.job_id, "estimated_bytes": estimate,
                  "bytes_processed": job.total_bytes_processed, "bytes_billed": job.total_bytes_billed,
                  "maximum_bytes_billed": query_cap, "cache_hit": job.cache_hit, "row_count": len(rows)}


def read_inputs(client, queries, start, end, *, query_cap=QUERY_CAP, total_cap=TOTAL_CAP, core=CORE, agent=AGENT,
                deadline=None):
    """Run the named queries for the run dates start to end (states and detect days are read LATER_DAYS past end),
    on the datasets core and agent. A deadline is a time.monotonic() value for the whole read."""
    if start > end:
        raise Refused("start is after end")
    rendered = {name: render(sql, core, agent) for name, sql in queries.items()}
    for name, sql in rendered.items():
        check_select(name, sql)
    query_params = params(start, end)
    got, metas, spent = {}, [], 0
    for name, sql in rendered.items():
        got[name], meta = read_one(client, name, sql, query_params, query_cap, spent, total_cap, deadline)
        spent += meta["estimated_bytes"]
        metas.append(meta)
    return got, metas


def briefs(rows):
    out = []
    for r in rows:
        payload = {"cards": json.loads(r["cards_json"]), "more": json.loads(r["more_json"]),
                   "held_back": {"items": json.loads(r["held_json"])}}
        out.append({k: r[k] for k in ("brief_date", "market", "run_id", "published_at", "status")} | {"payload": payload})
    return out
