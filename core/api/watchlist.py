"""The current watch list (contract.md section 10.5), without the web stack.

core/api/agent_app.py serves the watches and writes them; the f42-digest job only reads them, and runs on the jobs
image, which has no fastapi or starlette. So the read lives here and agent_app imports it: WATCHES and _lock are the
same objects in both modules, and agent_app.current_watches passes its own bigquery_client and WATCHES, so a client
or list patched there still reaches this read.
"""
import json
import logging
import os
import threading
from datetime import datetime

log = logging.getLogger("f42-agent")

WATCHES = []  # the in-memory watches table outside BigQuery, appended to by agent_app
_lock = threading.Lock()


def project():
    return os.environ.get("F42_PROJECT", "ogilvy-trends-v2")


def bigquery_client():
    from google.cloud import bigquery
    return bigquery.Client(project=project())


def watch_of(row):
    out = dict(row)
    for key in ("target", "rule"):
        if isinstance(out[key], str):
            out[key] = json.loads(out[key])
    for key in ("created_at", "status_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


def current_watches(watch_id=None, client=None, held=None):
    """The latest row per watch_id by status time (created_at on rows written before status_at), newest first."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.api_core.exceptions import NotFound
        from google.cloud import bigquery
        where = "watch_id = @watch_id" if watch_id else "TRUE"
        # Same order as L1's v_watches_current, so a same-microsecond tie resolves the way the view does.
        sql = (f"SELECT w.watch_id, w.created_at, w.status_at, w.who, w.target, w.market, w.rule, w.label, w.status "
               f"FROM `{project()}.intelligence_42_agent.watches` w WHERE {where} "
               f"QUALIFY ROW_NUMBER() OVER (PARTITION BY w.watch_id ORDER BY COALESCE(w.status_at, w.created_at) "
               f"DESC NULLS LAST, w.status DESC, TO_JSON_STRING(w)) = 1 "
               f"ORDER BY COALESCE(status_at, created_at) DESC")
        params = [bigquery.ScalarQueryParameter("watch_id", "STRING", watch_id)] if watch_id else []
        config = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=2 * 1024 ** 3)
        try:
            rows = (client or bigquery_client)().query(sql, job_config=config).result()
        except NotFound:
            log.warning("intelligence_42_agent.watches does not exist yet; no watches")
            return []
        return [watch_of(dict(row)) for row in rows]
    with _lock:
        rows = list(WATCHES if held is None else held)
    latest = {}
    for row in rows:
        if watch_id in (None, row["watch_id"]):
            latest.pop(row["watch_id"], None)
            latest[row["watch_id"]] = row
    return [watch_of(row) for row in reversed(latest.values())]
