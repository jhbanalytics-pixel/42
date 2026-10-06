"""Record system observability events to BigQuery, never breaking the caller.

``record_event`` lands one append-only row in ``system_events`` for any notable
system event (a cron run, a digest failure, a connector failure, a Listening
Post chat turn or error). ``track`` is a context manager that times a block and
records an ``ok`` event on success or an ``ERROR`` event carrying the exception
cause on failure, then re-raises; it is the anti-swallow primitive, a drop-in
for a risky ``except`` that would otherwise hide the cause.

Two hard contracts:

1. Non-fatal. The whole write path is guarded so a BigQuery failure logs a
   warning and returns. Observability must never break what it observes.
2. Fatal marker. When an ERROR event is flagged ``fatal=True`` the writer also
   emits a Cloud Logging line carrying the literal marker ``[TEV2_FATAL]``. The
   real-time email alert policy (scripts/deploy_alert_policy.sh) keys on that
   marker, so it fires on genuinely fatal events and does not flap on the
   disciplined benign ERROR logs the pipeline already emits (velocity fallback,
   etc.).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.utils.bigquery import insert_dataframe
from src.utils.log_redactor import get_logger, redact_text, sanitize_value

logger = get_logger(__name__)

FATAL_MARKER = "[TEV2_FATAL]"
_VALID_SEVERITY = frozenset({"INFO", "WARN", "ERROR"})
_MESSAGE_CAP = 2000
_DETAIL_CAP = 8000
_BATCH_SIZE = 100
_STREAM_LOCK = threading.Lock()
_CONTEXT_FIELDS = frozenset(
    {
        "run_id",
        "request_id",
        "stage",
        "cloud_execution",
        "trend_date",
        "source_sha",
        "image_digest",
        "deployment_digest",
        "policy_digest",
        "job_id",
        "reason_code",
        "error_type",
    }
)


class _EventBatch:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.closed = False
        self.lock = threading.Lock()


_EVENT_BATCH: ContextVar[_EventBatch | None] = ContextVar("system_event_batch", default=None)


def _safe_text(value: object, cap: int, fallback: str) -> str:
    try:
        return redact_text(str(value))[:cap]
    except Exception:
        return fallback


def _bounded_json(value: object, cap: int, *, preserved: dict | None = None) -> str:
    serialized = json.dumps(value, default=str, ensure_ascii=False, allow_nan=False)
    if len(serialized) <= cap:
        return serialized

    low = 0
    high = len(serialized)
    base = {"_truncated": True, **(preserved or {})}
    best = json.dumps({**base, "preview": ""}, ensure_ascii=False, allow_nan=False)
    if len(best) > cap:
        base = {"_truncated": True, "_context_unavailable": True}
        best = json.dumps({**base, "preview": ""}, ensure_ascii=False, allow_nan=False)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = json.dumps(
            {**base, "preview": serialized[:midpoint]}, ensure_ascii=False, allow_nan=False
        )
        if len(candidate) <= cap:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _safe_meta_json(
    meta: dict[str, Any] | None, *, invalid_fields: tuple[str, ...] = ()
) -> str | None:
    if meta is None and not invalid_fields:
        return None
    meta = {} if meta is None else meta
    preserved = {"_invalid_fields": list(invalid_fields)} if invalid_fields else {}
    if isinstance(meta, dict):
        invalid = []
        for key in sorted(_CONTEXT_FIELDS.intersection(meta)):
            value = sanitize_value(meta[key])
            if value is None or (
                isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable()
            ):
                preserved[key] = value
            else:
                preserved[key] = "***UNAVAILABLE***"
                invalid.append(key)
        if invalid:
            preserved["_invalid_context_fields"] = invalid
    try:
        sanitized = sanitize_value(meta)
        if isinstance(sanitized, dict):
            sanitized.update(preserved)
        else:
            sanitized = {"_serialization_error": True, **preserved}
        return _bounded_json(sanitized, _DETAIL_CAP, preserved=preserved)
    except Exception:
        return _bounded_json(
            {"_serialization_error": True, **preserved}, _DETAIL_CAP, preserved=preserved
        )


def _environment() -> str:
    return os.environ.get("TRENDS_ENV", "dev")


def _stream_event(row: dict[str, Any]) -> None:
    try:
        payload = {
            **{key: value for key, value in row.items() if key not in {"event_time", "meta"}},
            "time": row["event_time"].isoformat().replace("+00:00", "Z"),
            "severity": "WARNING" if row["severity"] == "WARN" else row["severity"],
            "meta": json.loads(row["meta"]) if row["meta"] is not None else None,
            "logging.googleapis.com/insertId": row["event_id"],
        }
        serialized = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        with _STREAM_LOCK:
            sys.stderr.write(serialized + "\n")
            sys.stderr.flush()
    except Exception as exc:
        with suppress(Exception):
            logger.warning("structured event stream unavailable: error_type=%s", type(exc).__name__)


def _persist_events(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    reason = "write_failed"
    try:
        frame = pd.DataFrame(rows)
        frame["latency_ms"] = pd.array([row["latency_ms"] for row in rows], dtype="Int64")
        acknowledged = insert_dataframe(frame, "system_events")
        if type(acknowledged) is int and acknowledged == len(rows):
            return
        reason = "write_count_unconfirmed"
    except Exception as exc:
        error_type = type(exc).__name__
    else:
        error_type = None
    shared_context = {}
    for key in ("run_id", "request_id"):
        values = {json.loads(row["meta"] or "{}").get(key) for row in rows}
        if len(values) == 1:
            shared_context[key] = values.pop()
    _stream_event(
        {
            "event_id": str(uuid.uuid4()),
            "event_time": datetime.now(UTC),
            "environment": _environment(),
            "source": "observability",
            "severity": "WARN",
            "event_type": "telemetry_delivery_failed",
            "status": "unavailable",
            "market": None,
            "message": "system_events warehouse delivery is unconfirmed",
            "error_detail": None,
            "latency_ms": None,
            "meta": _safe_meta_json(
                {
                    "reason_code": reason,
                    "error_type": error_type,
                    "event_ids": [row["event_id"] for row in rows],
                    **shared_context,
                }
            ),
        }
    )


@contextmanager
def batch_events() -> Iterator[None]:
    """Stream immediately and flush bounded warehouse batches on context exit."""
    batch = _EventBatch()
    token = _EVENT_BATCH.set(batch)
    try:
        yield
    finally:
        with batch.lock:
            batch.closed = True
            pending = batch.rows[:]
            batch.rows.clear()
        _EVENT_BATCH.reset(token)
        with suppress(Exception):
            _persist_events(pending)


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
    meta: dict[str, Any] | None = None,
    fatal: bool = False,
) -> None:
    """Append one row to system_events. Never raises.

    ``source`` is the originating service (engine_cron, lp_chat, lp_api,
    watchdog, email). ``severity`` is normalised to INFO/WARN/ERROR. When
    ``severity`` is ERROR and ``fatal`` is set, a Cloud Logging line carrying
    the ``[TEV2_FATAL]`` marker is emitted first (before the BQ write, so the
    alert still fires even if the insert fails).
    """
    sev = severity if isinstance(severity, str) and severity in _VALID_SEVERITY else "INFO"
    safe_source = _safe_text(source, _MESSAGE_CAP, "redaction_failed")
    safe_event_type = (
        _safe_text(event_type, _MESSAGE_CAP, "redaction_failed") if event_type is not None else None
    )
    safe_status = (
        _safe_text(status, _MESSAGE_CAP, "redaction_failed") if status is not None else None
    )
    safe_market = (
        _safe_text(market, _MESSAGE_CAP, "redaction_failed") if market is not None else None
    )
    safe_message = _safe_text(
        message if message is not None else "", _MESSAGE_CAP, "redaction_failed"
    )
    safe_error_detail = (
        _safe_text(error_detail, _DETAIL_CAP, "redaction_failed")
        if error_detail is not None
        else None
    )
    safe_latency = latency_ms if type(latency_ms) is int and latency_ms >= 0 else None
    invalid_fields = ("latency_ms",) if latency_ms is not None and safe_latency is None else ()
    safe_meta = _safe_meta_json(meta, invalid_fields=invalid_fields)

    # Emit the fatal marker first and unconditionally: the alert must fire even
    # if the BigQuery insert below fails.
    if sev == "ERROR" and fatal is True:
        with suppress(Exception):
            fatal_detail = safe_message or safe_error_detail or ""
            first_line = fatal_detail.splitlines()[0] if fatal_detail else ""
            logger.error(
                "%s %s/%s %s",
                FATAL_MARKER,
                safe_source,
                safe_event_type or "error",
                first_line,
            )

    try:
        row = {
            "event_id": str(uuid.uuid4()),
            "event_time": datetime.now(UTC),
            "environment": _environment(),
            "source": safe_source,
            "severity": sev,
            "event_type": safe_event_type,
            "status": safe_status,
            "market": safe_market,
            "message": safe_message,
            "error_detail": safe_error_detail,
            "latency_ms": safe_latency,
            "meta": safe_meta,
        }
        _stream_event(row)
        batch = _EVENT_BATCH.get()
        if batch is None:
            _persist_events([row])
        else:
            pending = []
            with batch.lock:
                if batch.closed:
                    pending = [row]
                else:
                    batch.rows.append(row)
                    if len(batch.rows) >= _BATCH_SIZE:
                        pending = batch.rows[:]
                        batch.rows.clear()
            if pending:
                _persist_events(pending)
    except Exception as exc:  # non-fatal by contract
        safe_failure = _safe_text(exc, _DETAIL_CAP, "write failure unavailable")
        with suppress(Exception):
            logger.warning(
                "record_event non-fatal write failure (%s/%s): %s",
                safe_source,
                safe_event_type,
                safe_failure,
            )


@contextmanager
def track(
    source: str,
    event_type: str,
    *,
    market: str | None = None,
    message: str = "",
    fatal_on_error: bool = True,
) -> Iterator[None]:
    """Time a block and record its outcome to system_events, then re-raise.

    On success records an ``ok`` INFO event with the elapsed latency. On
    exception records an ``ERROR`` event whose ``error_detail`` is the formatted
    traceback (so the cause is captured, not swallowed), marks it ``fatal`` by
    default so the alert fires, and re-raises. Use it in place of a broad
    ``except`` that would hide the cause.
    """
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        latency = int((time.monotonic() - start) * 1000)
        record_event(
            source,
            "ERROR",
            event_type,
            status="failed",
            market=market,
            message=message or f"{event_type} failed: {exc}",
            error_detail="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            latency_ms=latency,
            fatal=fatal_on_error,
        )
        raise
    else:
        latency = int((time.monotonic() - start) * 1000)
        record_event(
            source,
            "INFO",
            event_type,
            status="ok",
            market=market,
            message=message,
            latency_ms=latency,
        )


__all__ = ["FATAL_MARKER", "batch_events", "record_event", "track"]
