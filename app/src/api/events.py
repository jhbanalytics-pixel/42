"""Write 42 events to the shared system_events table. Non-fatal.

The engine owns the system_events table (one queryable history across the
stack); 42 writes its own rows here so a chat turn or an API
error leaves a trace instead of failing silently, which is what triggered the
observability work in the first place. The writer never raises: observability
must not break the chat. A fatal ERROR also emits a [TEV2_FATAL] Cloud Logging
line, the marker the engine's alert policy keys on.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

FATAL_MARKER = "[TEV2_FATAL]"
_VALID_SEVERITY = {"INFO", "WARN", "ERROR"}
_MESSAGE_CAP = 2000
_DETAIL_CAP = 8000


def _project() -> str:
    return os.environ.get("GCP_PROJECT", "ogilvy-trends-v2")


def _dataset() -> str:
    return os.environ.get("BQ_DATASET", "trends_v2_dev")


def record_event(
    source: str,
    severity: str,
    event_type: str | None = None,
    *,
    status: str | None = None,
    market: str | None = None,
    message: str = "",
    error_detail: str | None = None,
    latency_ms: int | None = None,
    meta: dict | None = None,
    fatal: bool = False,
) -> None:
    """Append one row to system_events. Never raises."""
    sev = severity if severity in _VALID_SEVERITY else "INFO"

    if sev == "ERROR" and fatal:
        head = (
            (message or error_detail or "").splitlines()[0]
            if (message or error_detail)
            else ""
        )
        logger.error(
            "%s %s/%s %s",
            FATAL_MARKER,
            source,
            event_type or "error",
            head[:_MESSAGE_CAP],
        )

    # Never write to the shared table from a test run, so the suite (and the
    # keyless CI gate) cannot stream junk rows into prod system_events.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    try:
        from google.cloud import bigquery

        client = bigquery.Client(project=_project())
        table = f"{_project()}.{_dataset()}.system_events"
        row = {
            "event_id": str(uuid.uuid4()),
            "event_time": datetime.now(UTC).isoformat(),
            "environment": os.environ.get("TRENDS_ENV", "prod"),
            "source": str(source),
            "severity": sev,
            "event_type": str(event_type) if event_type else None,
            "status": str(status) if status else None,
            "market": str(market) if market else None,
            "message": (message or "")[:_MESSAGE_CAP],
            "error_detail": (str(error_detail)[:_DETAIL_CAP] if error_detail else None),
            "latency_ms": int(latency_ms) if latency_ms is not None else None,
            "meta": (
                json.dumps(meta, default=str)[:_DETAIL_CAP]
                if meta is not None
                else None
            ),
        }
        errors = client.insert_rows_json(table, [row])
        if errors:
            logger.warning(
                "system_events insert returned errors (non-fatal): %s", errors
            )
    except Exception as exc:  # non-fatal by contract
        logger.warning(
            "record_event non-fatal write failure (%s/%s): %s", source, event_type, exc
        )
