"""Runs rows for the detect job's own steps, aggregate and stats (DATA.md section 3 opening).

One row per finished step, streamed into intelligence_42_agent.runs with insert_rows_json, the same way
lane L1's chain helper writes its rows. Append only: nothing here updates, replaces or removes a row.
"""

import json
import uuid
from datetime import datetime, timezone

from .sqlrun import AGENT


def new_run_id(stage, d):
    return f"{stage}-{d:%Y%m%d}-{uuid.uuid4().hex[:12]}"


def now():
    return datetime.now(timezone.utc).isoformat()


def append(client, run_id, stage, d, status, started_at, finished_at, counts=None, error=None, agent=AGENT):
    """Append one runs row and return it. counts is stored as JSON text with model_usd 0.0 added, since
    detect's steps call no model and runs has no model_usd column; the caller's dict is left unchanged."""
    row = {"run_id": run_id, "stage": stage, "run_date": d.isoformat(), "status": status,
           "started_at": started_at, "finished_at": finished_at,
           "counts": json.dumps({"model_usd": 0.0, **(counts or {})}), "error": error}
    table = f"{agent}.runs"
    errors = client.insert_rows_json(table, [row])
    if errors:
        raise RuntimeError(f"append to {table} failed: {errors}")
    return row
