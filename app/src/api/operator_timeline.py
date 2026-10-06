"""Operator timeline records beyond publication, and advisory browser delivery reports.

Everything here is a closed, versioned record. No field admits free text, so no
raw question, provider prompt, token, answer, artifact body or private source
body can reach a log line through this module.

question_artifact_event_v1
    One server event for an artifact after its answer or dossier was
    published: preparation, review and export. Fields, all always present:

    event_version     "question_artifact_event_v1"
    request_id        canonical UUID of the question request, or null
    investigation_id  investigation id (inv_...), or null
    artifact_id       dossier artifact id (art_ and 16 hex), or null
    artifact_version  64 hex digest of the artifact content, or null
    dossier_version   64 hex digest of the dossier version, or null
    stage             artifact_preparation, artifact_review or artifact_export
    event             completed or failed
    action            what was asked, from the closed set of the stage
    format            html, pdf or null
    state             draft, pending_review, approved, rejected or null
    occurred_at       UTC time with microseconds, ending in Z
    reason_code       null when completed, otherwise a lower case server code

    At least one of request_id and investigation_id is present, which is what
    links the event to the question or investigation timeline. A dossier does
    not store the question request it came from today, so dossier artifact
    events carry the investigation id and answer exports carry the request id.

question_delivery_report_v1
    The body a browser posts to POST /api/internal/v2/question-delivery after
    it rendered an answer. Exactly these fields, nothing else:

    request_id        canonical UUID of the question request
    response_digest   the rendered response digest the status read served
    asset_version     the served frontend asset version, a bounded token
    event_type        rendered or render_failed
    client_event_at   the browser's own UTC time, ending in Z

    The logged record adds event_version "question_delivery_report_v1" and
    received_at, the server receipt time. A report is advisory telemetry: it
    never changes an answer, a lifecycle state or an approval.

question_delivery_status_v1
    The diagnostic reader for one request. delivery is unknown when this
    process holds no acknowledgement for the request, which includes a report
    that reached another instance. A missing acknowledgement is never read as
    a healthy delivery.

Rendered response digest
    The result store holds no digest of the response alone, only result_digest
    over the whole result record. The rendered response digest is defined here
    as the SHA-256 hex digest of the stored response member encoded as the
    canonical JSON the result record is stored in (sorted keys, no spaces,
    UTF-8 without ASCII escaping). The record bytes are verified to be in that
    encoding, so the digest is taken over the exact response bytes stored.
"""

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from uuid import UUID

logger = logging.getLogger("listening_post.question_worker")

ARTIFACT_EVENT_VERSION = "question_artifact_event_v1"
ARTIFACT_EVENT_FIELDS = (
    "event_version",
    "request_id",
    "investigation_id",
    "artifact_id",
    "artifact_version",
    "dossier_version",
    "stage",
    "event",
    "action",
    "format",
    "state",
    "occurred_at",
    "reason_code",
)
ARTIFACT_STAGE_ACTIONS = {
    "artifact_preparation": frozenset({"prepare"}),
    "artifact_review": frozenset({"submit", "approve", "reject", "preview"}),
    "artifact_export": frozenset({"read", "export_html", "export_pdf"}),
}
ARTIFACT_STATES = frozenset({"draft", "pending_review", "approved", "rejected"})
ARTIFACT_FORMATS = frozenset({"html", "pdf"})

DELIVERY_REPORT_VERSION = "question_delivery_report_v1"
DELIVERY_REPORT_FIELDS = (
    "request_id",
    "response_digest",
    "asset_version",
    "event_type",
    "client_event_at",
)
DELIVERY_RECORD_FIELDS = ("event_version",) + DELIVERY_REPORT_FIELDS + ("received_at",)
DELIVERY_STATUS_VERSION = "question_delivery_status_v1"
DELIVERY_EVENT_TYPES = frozenset({"rendered", "render_failed"})
DELIVERY_BODY_LIMIT = 1024
DELIVERY_RATE_MAX = 30
DELIVERY_RATE_WINDOW_SECONDS = 60.0
DELIVERY_RATE_CLIENTS = 4096
DELIVERY_LEDGER_LIMIT = 2048

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_INVESTIGATION_ID = re.compile(r"inv_[A-Za-z0-9_-]{1,128}\Z")
_ARTIFACT_ID = re.compile(r"art_[0-9a-f]{16}\Z")
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
_ASSET_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SERVER_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_CLIENT_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")


def _uuid_text(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _optional(pattern, value):
    return value is None or (type(value) is str and pattern.fullmatch(value) is not None)


def _now_text():
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _log(record):
    try:
        logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
    except Exception:
        pass


def rendered_response_digest(response):
    """SHA-256 of the stored response member in the result record's canonical encoding."""
    data = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def validate_artifact_event(value):
    """Refuse any artifact event beyond the closed shape; no field admits free text."""
    if (
        type(value) is not dict
        or tuple(sorted(value)) != tuple(sorted(ARTIFACT_EVENT_FIELDS))
        or value["event_version"] != ARTIFACT_EVENT_VERSION
        or not (value["request_id"] is None or _uuid_text(value["request_id"]))
        or not _optional(_INVESTIGATION_ID, value["investigation_id"])
        or (value["request_id"] is None and value["investigation_id"] is None)
        or not _optional(_ARTIFACT_ID, value["artifact_id"])
        or not _optional(_DIGEST, value["artifact_version"])
        or not _optional(_DIGEST, value["dossier_version"])
        or value["stage"] not in ARTIFACT_STAGE_ACTIONS
        or value["action"] not in ARTIFACT_STAGE_ACTIONS[value["stage"]]
        or value["event"] not in ("completed", "failed")
        or not (value["format"] is None or value["format"] in ARTIFACT_FORMATS)
        or not (value["state"] is None or value["state"] in ARTIFACT_STATES)
        or type(value["occurred_at"]) is not str
        or _SERVER_TIME.fullmatch(value["occurred_at"]) is None
        or (value["event"] == "failed") != (value["reason_code"] is not None)
        or not _optional(_REASON_CODE, value["reason_code"])
    ):
        raise ValueError("artifact_event_invalid")
    return value


def artifact_event(
    *,
    stage,
    action,
    event,
    request_id=None,
    investigation_id=None,
    artifact_id=None,
    artifact_version=None,
    dossier_version=None,
    format=None,
    state=None,
    reason_code=None,
):
    """Build one artifact event, or None when it would link to nothing.

    Identifiers come from routes that may be refusing a malformed caller value,
    so any value that does not fit its grammar is recorded as null rather than
    copied, and a refusal code outside the code grammar becomes a generic one.
    """

    def keep(pattern, candidate):
        return candidate if _optional(pattern, candidate) else None

    if event == "failed" and (reason_code is None or not _optional(_REASON_CODE, reason_code)):
        reason_code = "artifact_refused"
    record = {
        "event_version": ARTIFACT_EVENT_VERSION,
        "request_id": request_id if _uuid_text(request_id) else None,
        "investigation_id": keep(_INVESTIGATION_ID, investigation_id),
        "artifact_id": keep(_ARTIFACT_ID, artifact_id),
        "artifact_version": keep(_DIGEST, artifact_version),
        "dossier_version": keep(_DIGEST, dossier_version),
        "stage": stage,
        "event": event,
        "action": action,
        "format": format if format in ARTIFACT_FORMATS else None,
        "state": state if state in ARTIFACT_STATES else None,
        "occurred_at": _now_text(),
        "reason_code": reason_code if event == "failed" else None,
    }
    if record["request_id"] is None and record["investigation_id"] is None:
        return None
    return validate_artifact_event(record)


def emit_artifact_event(**fields):
    """Log one artifact event. Observability never breaks the route it watches."""
    try:
        record = artifact_event(**fields)
    except Exception:
        logger.warning("question_artifact_event_invalid")
        return None
    if record is not None:
        _log(record)
    return record


class DeliveryRefused(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate_field")
    return dict(pairs)


def parse_delivery_report(data):
    """Exactly the five report fields, each inside its grammar, or a refusal."""
    if type(data) is not bytes or len(data) > DELIVERY_BODY_LIMIT:
        raise DeliveryRefused(413, "delivery_report_too_large")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise DeliveryRefused(400, "delivery_report_invalid") from None
    if (
        type(value) is not dict
        or set(value) != set(DELIVERY_REPORT_FIELDS)
        or not _uuid_text(value["request_id"])
        or type(value["response_digest"]) is not str
        or _DIGEST.fullmatch(value["response_digest"]) is None
        or type(value["asset_version"]) is not str
        or _ASSET_VERSION.fullmatch(value["asset_version"]) is None
        or value["event_type"] not in DELIVERY_EVENT_TYPES
        or type(value["client_event_at"]) is not str
        or _CLIENT_TIME.fullmatch(value["client_event_at"]) is None
    ):
        raise DeliveryRefused(400, "delivery_report_invalid")
    try:
        datetime.fromisoformat(value["client_event_at"].replace("Z", "+00:00"))
    except ValueError:
        raise DeliveryRefused(400, "delivery_report_invalid") from None
    return {field: value[field] for field in DELIVERY_REPORT_FIELDS}


class DeliveryRateLimit:
    """A per-process sliding window per client, with a bound on tracked clients."""

    def __init__(self, maximum=DELIVERY_RATE_MAX, window=DELIVERY_RATE_WINDOW_SECONDS,
                 clients=DELIVERY_RATE_CLIENTS):
        self.maximum, self.window, self.clients = maximum, window, clients
        self._hits = {}
        self._lock = threading.Lock()

    def check(self, key, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            for name in list(self._hits):
                hits = self._hits[name]
                while hits and now - hits[0] > self.window:
                    hits.popleft()
                if not hits:
                    del self._hits[name]
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self.clients:
                    raise DeliveryRefused(429, "delivery_rate_limited")
                hits = self._hits[key] = deque()
            if len(hits) >= self.maximum:
                raise DeliveryRefused(429, "delivery_rate_limited")
            hits.append(now)

    def reset(self):
        with self._lock:
            self._hits.clear()


class DeliveryLedger:
    """The latest accepted report per request, bounded, held by this process only."""

    def __init__(self, limit=DELIVERY_LEDGER_LIMIT):
        self.limit = limit
        self._records = OrderedDict()
        self._lock = threading.Lock()

    def record(self, report):
        record = {"event_version": DELIVERY_REPORT_VERSION, **report, "received_at": _now_text()}
        with self._lock:
            self._records.pop(report["request_id"], None)
            self._records[report["request_id"]] = record
            while len(self._records) > self.limit:
                self._records.popitem(last=False)
        _log(record)
        return record

    def status(self, request_id):
        if not _uuid_text(request_id):
            raise DeliveryRefused(400, "delivery_request_invalid")
        with self._lock:
            record = self._records.get(request_id)
        return {
            "contract_version": DELIVERY_STATUS_VERSION,
            "request_id": request_id,
            "delivery": "unknown" if record is None else record["event_type"],
            "authority": "advisory",
            "report": None
            if record is None
            else {field: record[field] for field in DELIVERY_RECORD_FIELDS[2:]},
        }

    def reset(self):
        with self._lock:
            self._records.clear()


DELIVERY_RATE_LIMIT = DeliveryRateLimit()
DELIVERY_LEDGER = DeliveryLedger()
