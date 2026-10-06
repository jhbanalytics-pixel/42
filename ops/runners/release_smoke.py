"""Tier one release smoke check: reads only, no paid question submission.

Run against the staging service after a release, it proves five things and
writes one append only receipt line per run:

1. revision: the revision serving all traffic runs the app image the release
   index names, with the release commit and deployment digest in its
   environment. The app exposes no revision or image, so this is read from the
   Cloud Run Admin API with application default credentials through the release
   adapter, as ``release.py verify`` reads it.
2. coverage: ``GET /api/chat/coverage``, the read behind the Ask page's Covers
   line, names exactly the expected capture window.
3. saved_answer: ``GET /api/internal/v2/fieldwork/question``, the read the Ask
   page makes when it reopens a saved answer, returns a complete or partial
   answer with claims, R citations that resolve to source records, and a non
   empty source record list.
4. export: ``GET /api/chat/answer/<id>/export.html``, the read behind the
   Download as HTML button, returns the page under the expected file name with
   the expected title and heading.
5. allowance: the question allowance used and remaining. No endpoint the access
   key reaches exposes it, so it is read from the allowance ledger object in the
   staging bucket with application default credentials, as ``release.py
   verify`` reads it.

Nothing here can submit a question or reserve allowance. The staging client
sends only the GET requests on its allowlist and refuses anything else before
the transport is reached; the native reads go through a transport that refuses
every method but GET. The access key is read from a file, sent only in the
X-Passcode header to the staging origin, and never printed, logged, recorded or
carried in an exception. Redirects are refused, so the key never follows one.

Exit status: 0 when all five checks pass, 1 when any check fails, 2 on a
refused or unreadable input or when a check could not be read.
"""

import argparse
import hashlib
import html.parser
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for _entry in (str(_ROOT / "engine"), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from ops.deploy import release, resource_guard  # noqa: E402
from ops.deploy import release_native_adapter as native  # noqa: E402
from src.analysis.open_intelligence.general_question_control import (  # noqa: E402
    MAX_REQUESTS,
    REQUEST_MICROUSD,
)

RECEIPT_CONTRACT = "42_release_smoke_receipt_v1"
TIER = "tier_1_reads_only"
COVERAGE_PATH = "/api/chat/coverage"
COVERAGE_VERSION = "general_question_coverage_v1"
DETAIL_PATH = "/api/internal/v2/fieldwork/question"
DETAIL_VERSION = "general_question_detail_v1"
EXPORT_PATH = "/api/chat/answer/{request_id}/export.html"
KEY_HEADER = "X-Passcode"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_KEY_BYTES = 4096
MAX_BODY_BYTES = 8 * 1024 * 1024
CHECKS = ("revision", "coverage", "saved_answer", "export", "allowance")
ANSWERED_STATES = ("complete", "partial")
REDACTED = "[redacted]"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CITATION = re.compile(r"R[1-9]\d*\Z")
_FILENAME = re.compile(r'attachment;\s*filename="([^"]+)"\Z')

# The only requests the staging client may send: method, exact path and the
# exact query keys. Every entry is a GET, none reserves allowance, and a request
# outside this table is refused before the transport is reached.
ALLOWED_REQUESTS = (
    ("GET", re.compile(re.escape(COVERAGE_PATH) + r"\Z"), frozenset()),
    (
        "GET",
        re.compile(re.escape(DETAIL_PATH) + r"\Z"),
        frozenset({"contract_version", "request_id"}),
    ),
    (
        "GET",
        re.compile(r"/api/chat/answer/" + _UUID + r"/export\.html\Z"),
        frozenset(),
    ),
)
# The native reads: the service and its revisions, and one object in the bucket.
NATIVE_READS = ("get_service", "get_revision", "object_metadata", "object_media")


class Refused(ValueError):
    """An input the run will not proceed on; the code names it, nothing more."""


class Unreadable(RuntimeError):
    """A value the run could not read; the code names why, never a credential."""


class RequestRefused(RuntimeError):
    """A request outside the allowlist; it never reaches the transport."""


def _refuse(code):
    raise Refused(code)


# Access key


class AccessKey:
    """Holds the key; every text form of the holder is redacted."""

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def header(self):
        return {KEY_HEADER: self._value}

    def _pattern(self):
        """The key as written, and as JSON, a repr or a URL would escape it,
        matched without regard to case."""
        value = self._value
        forms = {
            value,
            json.dumps(value)[1:-1],
            json.dumps(value, ensure_ascii=False)[1:-1],
            repr(value)[1:-1],
            ascii(value)[1:-1],
            urllib.parse.quote(value, safe=""),
        }
        ordered = sorted((form for form in forms if form), key=len, reverse=True)
        return re.compile("|".join(map(re.escape, ordered)), re.IGNORECASE)

    def scrub(self, text):
        return self._pattern().sub(REDACTED, text)

    def scrub_value(self, value):
        """Server derived data with every value scrubbed. Mapping keys are the
        tool's own field names and stay as they are. A number or boolean whose
        text holds the key becomes the redaction marker."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {name: self.scrub_value(item) for name, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.scrub_value(item) for item in value]
        if isinstance(value, bool | int | float):
            return REDACTED if self._pattern().search(str(value)) else value
        return value

    def __repr__(self):
        return "AccessKey(" + REDACTED + ")"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("access_key_not_serializable")


def read_access_key(path):
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_KEY_BYTES + 1)
    except OSError:
        _refuse("access_key_unreadable")
    if len(raw) > MAX_KEY_BYTES:
        _refuse("access_key_invalid")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeError:
        _refuse("access_key_invalid")
    # Printable ASCII only: no space, control or non ASCII character.
    if not value or any(not 0x21 <= ord(char) <= 0x7E for char in value):
        _refuse("access_key_invalid")
    return AccessKey(value)


# Staging HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect is refused, so the key header never travels to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unreadable(f"redirect_refused_http_{code}")


_opener = urllib.request.build_opener(_NoRedirect())


def urllib_transport(request):
    http = urllib.request.Request(
        request["url"], method=request["method"], headers=request["headers"]
    )
    try:
        with _opener.open(http, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return {
                "status": response.status,
                "headers": dict(response.headers),
                "body": response.read(MAX_BODY_BYTES + 1),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": error.code,
            "headers": dict(error.headers or {}),
            "body": error.read(MAX_BODY_BYTES + 1),
        }


def validate_staging_url(value):
    parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    if (
        parts is None
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        _refuse("staging_url_invalid")
    return "https://" + parts.netloc


def request_allowed(method, path, query_keys=()):
    keys = frozenset(query_keys)
    return any(
        method == allowed_method and pattern.fullmatch(path) and keys == allowed_keys
        for allowed_method, pattern, allowed_keys in ALLOWED_REQUESTS
    )


class StagingClient:
    """GET requests on the allowlist to one origin, with the key header only."""

    def __init__(self, origin, key, transport=None):
        self.origin = validate_staging_url(origin)
        self._key = key
        self._transport = transport or urllib_transport
        self.sent = []

    def __repr__(self):
        return f"StagingClient({self.origin!r})"

    def request(self, method, path, query=None):
        query = dict(query or {})
        if not request_allowed(method, path, query):
            raise RequestRefused("request_not_allowlisted")
        url = self.origin + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        self.sent.append({"method": method, "path": path})
        try:
            response = self._transport(
                {
                    "method": method,
                    "url": url,
                    "headers": {**self._key.header(), "Accept": "*/*"},
                }
            )
        except Unreadable:
            raise
        except Exception as error:
            # Only the class name leaves here: an exception text is not ours
            # to vouch for.
            raise Unreadable("transport_" + type(error).__name__) from None
        if not isinstance(response, dict) or "status" not in response:
            raise Unreadable("transport_response_invalid")
        body = bytes(response.get("body") or b"")
        if len(body) > MAX_BODY_BYTES:
            raise Unreadable("response_too_large")
        headers = {
            str(name).lower(): str(value)
            for name, value in (response.get("headers") or {}).items()
        }
        return int(response["status"]), headers, body

    def get(self, path, query=None):
        return self.request("GET", path, query)


# A body that names NaN or Infinity is refused by name rather than read.
NON_FINITE = object()
# The refusal codes the app sends on these reads (ask_export.REFUSALS and the
# workspace errors in main.py). A reason is always one of the tool's own words,
# so a code outside this set is reported by its HTTP status instead.
KNOWN_CODES = frozenset(
    {
        "request_invalid",
        "request_unavailable",
        "answer_not_exportable",
        "answer_held",
        "answer_invalid",
        "export_unavailable",
        "workspace_request_invalid",
        "workspace_unavailable",
        "contract_version_unsupported",
    }
)


class _NonFinite(ValueError):
    pass


def _non_finite(_constant):
    raise _NonFinite()


def _json_body(body):
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_non_finite)
    except _NonFinite:
        return NON_FINITE
    except (UnicodeError, ValueError):
        return None


def _detail_code(body):
    value = _json_body(body)
    detail = value.get("detail") if isinstance(value, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if code in KNOWN_CODES else None


# Check results


def _result(state, observed, *, expected=None, reason=None):
    result = {"status": state, "observed": observed}
    if expected is not None:
        result["expected"] = expected
    if reason is not None:
        result["reason"] = reason
    return result


def _day(value):
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def parse_window(value):
    start, _, end = (value or "").partition("/")
    if _day(start) is None or _day(end) is None or start > end:
        _refuse("expected_window_invalid")
    return {"start": start, "end": end}


def parse_request_id(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        _refuse("request_id_invalid")
    return value


# 1. Serving revision and image digests


def read_release_record(path):
    try:
        raw = Path(path).read_bytes()
        index = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        _refuse("release_record_unreadable")
    try:
        release.validate_release_index_shape(index)
    except ValueError:
        _refuse("release_record_invalid")
    return index, hashlib.sha256(raw).hexdigest()


def _digest_of(image):
    if isinstance(image, str) and "@sha256:" in image:
        return image.rsplit("@sha256:", 1)[1]
    return None


def _serving_revision(service):
    statuses = service.get("trafficStatuses")
    if not isinstance(statuses, list):
        return None, []
    traffic = [
        {
            "revision": item.get("revision") or None,
            "percent": item.get("percent"),
            "tag": item.get("tag"),
        }
        for item in statuses
        if isinstance(item, dict)
    ]
    serving = [
        item
        for item in statuses
        if isinstance(item, dict) and item.get("percent") == 100
    ]
    if len(serving) != 1:
        return None, traffic
    name = serving[0].get("revision")
    if not name and serving[0].get("type") == "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST":
        latest = service.get("latestReadyRevision")
        name = latest.rsplit("/", 1)[-1] if isinstance(latest, str) else None
    return (name if isinstance(name, str) and name else None), traffic


def check_revision(index, clients, manifest):
    expected = {
        "commit": index["commit"],
        "app_image": index["app_image"],
        "app_image_digest": _digest_of(index["app_image"]),
        "engine_image_digest": _digest_of(index["engine_image"]),
        "deployment_digest": index["deployment_digest"],
    }
    observed = {
        "revision": None,
        "app_image": None,
        "app_image_digest": None,
        "source_sha": None,
        "deployment_digest": None,
        "ready": None,
        # The engine image is built into the app image; Cloud Run serves only the
        # app image, so the engine digest is carried from the record, not observed.
        "engine_image_served": False,
    }
    try:
        resource_guard.assert_allowed(release.SERVICE_RESOURCE, "read", manifest)
        service = clients["run"].get_service(release.SERVICE_API_NAME)
        if service is None:
            return _result(
                "not_readable",
                observed,
                expected=expected,
                reason="service_unavailable",
            )
        if not isinstance(service, dict):
            return _result(
                "fail", observed, expected=expected, reason="service_invalid"
            )
        name, traffic = _serving_revision(service)
        observed["traffic"] = traffic
        if name is None:
            return _result(
                "fail", observed, expected=expected, reason="no_single_serving_revision"
            )
        observed["revision"] = name
        revision = clients["run"].get_revision(
            release.SERVICE_API_NAME + "/revisions/" + name
        )
    except Exception as error:
        return _result(
            "not_readable", observed, expected=expected, reason=_native_reason(error)
        )
    if revision is None:
        return _result(
            "not_readable", observed, expected=expected, reason="revision_unavailable"
        )
    if not isinstance(revision, dict):
        return _result("fail", observed, expected=expected, reason="revision_invalid")
    containers = revision.get("containers")
    if (
        not isinstance(containers, list)
        or len(containers) != 1
        or not isinstance(containers[0], dict)
    ):
        return _result(
            "fail", observed, expected=expected, reason="revision_containers_invalid"
        )
    image = containers[0].get("image")
    entries = containers[0].get("env") or []
    if not isinstance(entries, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in entries
    ):
        return _result(
            "fail", observed, expected=expected, reason="revision_env_invalid"
        )
    env = {
        item["name"]: item.get("value") if isinstance(item.get("value"), str) else None
        for item in entries
    }
    conditions = revision.get("conditions") or []
    if not isinstance(conditions, list):
        return _result(
            "fail", observed, expected=expected, reason="revision_conditions_invalid"
        )
    observed.update(
        {
            "app_image": image if isinstance(image, str) else None,
            "app_image_digest": _digest_of(image),
            "source_sha": env.get("SOURCE_SHA"),
            "deployment_digest": env.get("GENERAL_QUESTION_DEPLOYMENT_DIGEST"),
            "ready": any(
                isinstance(item, dict)
                and item.get("type") == "Ready"
                and item.get("state") == "CONDITION_SUCCEEDED"
                for item in conditions
            ),
        }
    )
    for field, reason in (
        ("app_image", "app_image_mismatch"),
        ("deployment_digest", "deployment_digest_mismatch"),
    ):
        if observed[field] != expected[field]:
            return _result("fail", observed, expected=expected, reason=reason)
    if observed["source_sha"] != expected["commit"]:
        return _result("fail", observed, expected=expected, reason="commit_mismatch")
    if not observed["ready"]:
        return _result("fail", observed, expected=expected, reason="revision_not_ready")
    return _result("pass", observed, expected=expected)


def _native_reason(error):
    if isinstance(error, native.NativeError):
        return f"native_http_{error.status}"
    if isinstance(error, RequestRefused):
        return "request_not_allowlisted"
    return "native_" + type(error).__name__


# 2. Ask page Covers window


def check_coverage(client, window):
    observed = {
        "http_status": None,
        "state": None,
        "start": None,
        "end": None,
        "cutoff_date": None,
    }
    try:
        status, _headers, body = client.get(COVERAGE_PATH)
    except Unreadable as error:
        return _result("not_readable", observed, expected=window, reason=str(error))
    observed["http_status"] = status
    if status != 200:
        return _result(
            "not_readable", observed, expected=window, reason=f"http_{status}"
        )
    value = _json_body(body)
    if value is NON_FINITE:
        return _result(
            "fail", observed, expected=window, reason="response_non_finite_number"
        )
    if not isinstance(value, dict) or value.get("contract_version") != COVERAGE_VERSION:
        return _result("fail", observed, expected=window, reason="coverage_invalid")
    state = value.get("state")
    observed["state"] = state if isinstance(state, str) else None
    if value.get("state") != "covered":
        return _result("fail", observed, expected=window, reason="coverage_not_covered")
    served = value.get("window")
    if not isinstance(served, dict) or set(served) != {"start", "end"}:
        return _result("fail", observed, expected=window, reason="coverage_invalid")
    start, end = _day(served["start"]), _day(served["end"])
    observed.update(
        {
            "start": start,
            "end": end,
            "cutoff_date": cutoff
            if isinstance(cutoff := value.get("cutoff_date"), str)
            else None,
        }
    )
    # The same reading the Ask page makes before it shows a Covers line.
    if start is None or end is None or start > end or value.get("cutoff_date") != end:
        return _result("fail", observed, expected=window, reason="coverage_invalid")
    if (start, end) != (window["start"], window["end"]):
        return _result("fail", observed, expected=window, reason="window_mismatch")
    return _result("pass", observed, expected=window)


# 3. Saved answer reload


def _rows(value, key):
    if not isinstance(value, list):
        return None
    rows = {}
    for row in value:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get(key), str)
            or row[key] in rows
        ):
            return None
        rows[row[key]] = row
    return rows


def _id_list(value):
    """A list of string ids, an absent list as empty, anything else None."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def check_saved_answer(client, request_id):
    observed = {
        "http_status": None,
        "request_id": None,
        "observed_state": None,
        "claims": 0,
        "citations": 0,
        "source_records": 0,
        "citation_labels": [],
    }
    try:
        status, _headers, body = client.get(
            DETAIL_PATH, {"contract_version": DETAIL_VERSION, "request_id": request_id}
        )
    except Unreadable as error:
        return _result("not_readable", observed, reason=str(error))
    observed["http_status"] = status
    if status in (400, 404):
        return _result("fail", observed, reason=_detail_code(body) or f"http_{status}")
    if status != 200:
        return _result("not_readable", observed, reason=f"http_{status}")
    value = _json_body(body)
    if value is NON_FINITE:
        return _result("fail", observed, reason="response_non_finite_number")
    if not isinstance(value, dict) or value.get("contract_version") != DETAIL_VERSION:
        return _result("fail", observed, reason="detail_invalid")
    for field in ("request_id", "observed_state"):
        if not isinstance(value.get(field), str):
            return _result("fail", observed, reason="detail_invalid")
        observed[field] = value[field]
    if value.get("request_id") != request_id:
        return _result("fail", observed, reason="request_id_mismatch")
    if value.get("observed_state") not in ANSWERED_STATES:
        return _result("fail", observed, reason="answer_not_complete_or_partial")
    response = value.get("response")
    reply = response.get("intelligence") if isinstance(response, dict) else None
    if not isinstance(reply, dict) or response.get("error") is True:
        return _result("fail", observed, reason="answer_missing")
    if (
        reply.get("request_id") != request_id
        or reply.get("status") != value["observed_state"]
    ):
        return _result("fail", observed, reason="answer_identity_mismatch")
    claims = _rows(reply.get("claims"), "claim_id")
    receipts = _rows(reply.get("receipts"), "receipt_id")
    readings = _rows(reply.get("readings") or [], "reading_id")
    if claims is None or receipts is None or readings is None:
        return _result("fail", observed, reason="answer_rows_invalid")
    observed["claims"] = len(claims)
    observed["source_records"] = len(receipts)
    labels = [row.get("citation_label") for row in receipts.values()]
    if not all(
        isinstance(label, str) and _CITATION.fullmatch(label) for label in labels
    ) or len(set(labels)) != len(labels):
        return _result("fail", observed, reason="citation_label_invalid")
    cited = []
    for claim in claims.values():
        references = _id_list(claim.get("receipt_ids"))
        reading_ids = _id_list(claim.get("reading_ids"))
        if references is None or reading_ids is None:
            return _result("fail", observed, reason="claim_references_invalid")
        for reading_id in reading_ids:
            reading = readings.get(reading_id)
            if reading is None:
                return _result("fail", observed, reason="reading_unresolved")
            sources = _id_list(reading.get("source_receipt_ids"))
            if sources is None:
                return _result("fail", observed, reason="reading_references_invalid")
            references.extend(sources)
        for reference in references:
            if reference not in receipts:
                return _result("fail", observed, reason="citation_unresolved")
            if reference not in cited:
                cited.append(reference)
    observed["citations"] = len(cited)
    observed["citation_labels"] = sorted(
        (receipts[item]["citation_label"] for item in cited),
        key=lambda label: int(label[1:]),
    )
    if not claims:
        return _result("fail", observed, reason="no_claims")
    if not receipts:
        return _result("fail", observed, reason="no_source_records")
    if not cited:
        return _result("fail", observed, reason="no_citations")
    return _result("pass", observed)


# 4. HTML export


class _Heads(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.heading = None
        self._open = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if (tag == "title" and self.title is None) or (
            tag == "h1" and self.heading is None
        ):
            self._open, self._text = tag, []

    def handle_endtag(self, tag):
        if tag == self._open:
            text = " ".join("".join(self._text).split())
            if tag == "title":
                self.title = text
            else:
                self.heading = text
            self._open = None

    def handle_data(self, data):
        if self._open is not None:
            self._text.append(data)


def check_export(client, request_id):
    short = request_id[:8]
    expected = {
        "filename": f"42-answer-{short}.html",
        "title": f"42 Ask answer {short}",
        "heading": "Ask answer",
    }
    observed = {
        "http_status": None,
        "filename": None,
        "frontend_filename": expected["filename"],
        "content_type": None,
        "title": None,
        "heading": None,
        "bytes": 0,
        "sha256": None,
    }
    try:
        status, headers, body = client.get(EXPORT_PATH.format(request_id=request_id))
    except Unreadable as error:
        return _result("not_readable", observed, expected=expected, reason=str(error))
    observed["http_status"] = status
    if status in (400, 404, 409, 502):
        return _result(
            "fail",
            observed,
            expected=expected,
            reason=_detail_code(body) or f"http_{status}",
        )
    if status != 200:
        return _result(
            "not_readable", observed, expected=expected, reason=f"http_{status}"
        )
    match = _FILENAME.fullmatch(headers.get("content-disposition", "").strip())
    observed["filename"] = match.group(1) if match else None
    observed["content_type"] = headers.get("content-type")
    observed["bytes"] = len(body)
    observed["sha256"] = hashlib.sha256(body).hexdigest()
    try:
        parser = _Heads()
        parser.feed(body.decode("utf-8"))
        parser.close()
    except UnicodeError:
        return _result("fail", observed, expected=expected, reason="export_not_utf8")
    observed["title"], observed["heading"] = parser.title, parser.heading
    if not (observed["content_type"] or "").startswith("text/html"):
        return _result(
            "fail", observed, expected=expected, reason="content_type_mismatch"
        )
    for field in ("filename", "title", "heading"):
        if observed[field] != expected[field]:
            return _result(
                "fail", observed, expected=expected, reason=field + "_mismatch"
            )
    return _result("pass", observed, expected=expected)


# 5. Question allowance


def check_allowance(clients, manifest):
    observed = {
        "source": "gs://" + release.BUCKET + "/" + release.LEDGER_OBJECT,
        "ledger_generation": None,
        "used": None,
        "limit": MAX_REQUESTS,
        "remaining": None,
        "reserved_microusd": None,
    }
    try:
        resource_guard.assert_allowed(release.BUCKET_RESOURCE, "read", manifest)
        ledger = release._read_ledger(clients)
    except release.Refusal as error:
        state = "not_readable" if str(error) == "ledger_unavailable" else "fail"
        return _result(state, observed, reason=str(error))
    except Exception as error:
        return _result("not_readable", observed, reason=_native_reason(error))
    used = len(ledger["value"]["requests"])
    observed.update(
        {
            "ledger_generation": ledger["generation"],
            "used": used,
            "remaining": MAX_REQUESTS - used,
            "reserved_microusd": ledger["value"]["reserved_microusd"],
        }
    )
    if (
        not 0 <= used <= MAX_REQUESTS
        or observed["reserved_microusd"] != used * REQUEST_MICROUSD
    ):
        return _result("fail", observed, reason="allowance_inconsistent")
    return _result("pass", observed)


# Native reads


def get_only(transport):
    """A native transport that refuses every method but GET before it sends."""

    def send(request):
        if request.get("method") != "GET":
            raise RequestRefused("native_method_refused")
        return transport(request)

    return send


def native_clients(transport=None):
    clients = native.build(
        "adc", transport=get_only(transport or native.urllib_transport)
    )
    return {"run": clients["run"], "objects": clients["objects"]}


# Receipt


def _receipt_path(path):
    target = Path(path)
    try:
        symlink = target.is_symlink()
        present = target.exists()
        regular = target.is_file()
    except OSError:
        _refuse("receipt_unwritable")
    if symlink:
        _refuse("receipt_symlink")
    if present and not regular:
        _refuse("receipt_not_file")
    return target


def append_receipt(path, record):
    """Append one line. Earlier lines are never read back into a write, and a
    file whose last line is unterminated is refused rather than repaired."""
    target = _receipt_path(path)
    try:
        # Native reads parse NaN and Infinity as floats; strict JSON has neither.
        text = json.dumps(
            record, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except ValueError:
        _refuse("receipt_non_finite_value")
    line = (text + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError:
        _refuse("receipt_unwritable")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            _refuse("receipt_not_file")
        if opened.st_size:
            try:
                reader = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                _refuse("receipt_unreadable")
            try:
                seen = os.fstat(reader)
                if (seen.st_dev, seen.st_ino) != (opened.st_dev, opened.st_ino):
                    _refuse("receipt_replaced")
                if os.pread(reader, 1, opened.st_size - 1) != b"\n":
                    _refuse("receipt_unterminated")
            finally:
                os.close(reader)
        try:
            written = os.write(descriptor, line)
        except OSError:
            _refuse("receipt_unwritable")
        if written != len(line):
            _refuse("receipt_unwritable")
    finally:
        os.close(descriptor)


def _now():
    return datetime.now(UTC)


def _stamp(now):
    return now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def overall(checks):
    states = [checks[name]["status"] for name in CHECKS]
    if "fail" in states:
        return "fail", 1
    if all(state == "pass" for state in states):
        return "pass", 0
    return "not_readable", 2


def _describe(name, result):
    observed = result["observed"]
    if name == "revision":
        digest = (observed.get("app_image_digest") or "")[:12]
        detail = f"{observed.get('revision')} app sha256:{digest}"
    elif name == "coverage":
        detail = f"{observed.get('start')} to {observed.get('end')}"
    elif name == "saved_answer":
        detail = (
            f"{observed.get('observed_state')}, {observed.get('claims')} claims, "
            f"{observed.get('citations')} citations, {observed.get('source_records')} source records"
        )
    elif name == "export":
        detail = f"{observed.get('filename')}, title: {observed.get('title')}"
    else:
        detail = f"used {observed.get('used')} of {observed.get('limit')}, remaining {observed.get('remaining')}"
    reason = f" ({result['reason']})" if result.get("reason") else ""
    return f"  {name}: {result['status']}{reason}: {detail}"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="release_smoke",
        description="Tier one release smoke check: reads only, no paid question submission.",
    )
    parser.add_argument(
        "--staging-url", default=release.AUDIENCE, help="staging origin, https"
    )
    parser.add_argument("--release-record", required=True, help="release index JSON")
    parser.add_argument(
        "--expected-window",
        required=True,
        help="expected capture window, START/END as ISO days",
    )
    parser.add_argument("--request-id", required=True, help="saved answer request id")
    parser.add_argument(
        "--access-key-file", required=True, help="file holding the access key"
    )
    parser.add_argument("--receipt", required=True, help="append only JSONL receipt")
    return parser


def _guarded(check):
    """A check that raised is not readable; only the class name is kept."""
    try:
        return check()
    except Exception as error:
        return _result("not_readable", {}, reason="check_error_" + type(error).__name__)


def execute(argv, *, transport=None, native_reads=None, now=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    key = None
    record = {
        "contract_version": RECEIPT_CONTRACT,
        "tier": TIER,
        "checked_at": _stamp(now or _now()),
        "staging_url": None,
        "release_commit": None,
        "release_record_sha256": None,
        "request_id": None,
        "expected_window": None,
    }

    def emit(text, stream):
        # Every line is built from the tool's own words and the record, whose
        # server derived parts were scrubbed before this point.
        print(text, file=stream)

    try:
        _receipt_path(args.receipt)
    except Refused as error:
        emit(f"release smoke: refused ({error})", err)
        return 2
    try:
        record["staging_url"] = validate_staging_url(args.staging_url)
        index, index_sha256 = read_release_record(args.release_record)
        record["release_commit"] = index["commit"]
        record["release_record_sha256"] = index_sha256
        record["expected_window"] = parse_window(args.expected_window)
        record["request_id"] = parse_request_id(args.request_id)
        key = read_access_key(args.access_key_file)
        manifest = resource_guard.load_resource_manifest(
            _ROOT / "ops" / "deploy" / "resource_manifest.json",
            expected_sha256=release.APPROVED_RESOURCE_MANIFEST_SHA256,
        )
    except (Refused, ValueError) as error:
        record["overall_status"] = "refused"
        record["reason"] = str(error)
        code = 2
    else:
        client = StagingClient(record["staging_url"], key, transport)
        clients = native_reads if native_reads is not None else native_clients()
        runs = {
            "revision": lambda: check_revision(index, clients, manifest),
            "coverage": lambda: check_coverage(client, record["expected_window"]),
            "saved_answer": lambda: check_saved_answer(client, record["request_id"]),
            "export": lambda: check_export(client, record["request_id"]),
            "allowance": lambda: check_allowance(clients, manifest),
        }
        record["checks"] = {name: _guarded(runs[name]) for name in CHECKS}
        record["overall_status"], code = overall(record["checks"])
    if key is not None and "checks" in record:
        # Only what the servers sent is scrubbed: each check's observed values.
        # The tool's field names, timestamps, reasons and arguments are its own
        # and are never rewritten, so a short key cannot corrupt the record.
        for result in record["checks"].values():
            result["observed"] = key.scrub_value(result["observed"])
    try:
        append_receipt(args.receipt, record)
    except Refused as error:
        emit(f"release smoke: refused ({error})", err)
        return 2
    lines = [f"release smoke {record['overall_status']} at {record['checked_at']}"]
    if "checks" in record:
        lines.extend(_describe(name, record["checks"][name]) for name in CHECKS)
    else:
        lines.append(f"  reason: {record['reason']}")
    lines.append(f"  receipt: {args.receipt}")
    emit("\n".join(lines), out)
    return code


def main():
    raise SystemExit(execute(sys.argv[1:]))


if __name__ == "__main__":
    main()
