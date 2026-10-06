"""Native adapter for the R05 managed runtime preflight: Cloud Scheduler v1 and
the Cloud Run Admin API v2 jobs surface over one injectable HTTP transport.

Every mutation is one REST request with an explicit body, so the request the
tests see is the request that runs; nothing here shells out for a mutation. The
call table is closed: it reads a scheduler, creates one, pauses one, resumes
the price policy scheduler and no other, reads a Cloud Run job, its job scoped
IAM policy and an execution, sends the actual empty RunJobRequest and waits on
an operation. There is deliberately no general resume, no delete and no update
call. The one resume call takes a name that must resolve to the approved price
policy scheduler identifier, so the daily scheduler cannot be started through
this adapter, and no command in this package can rewrite a scheduler that
exists.

Every resource name is validated against its own grammar and pinned to the
approved project and region before a URL exists, and each URL is built from the
endpoint, that pinned location and the validated identifier, so no document can
concatenate a verb, a fragment, a query, a deeper path or another project onto
an endpoint and reach a call this table does not hold.

The credential source is the one the release lifecycle already uses, so this
package mints no second kind of token, but the transport is this package's own
and follows no redirect, so a bearer token is never replayed to a redirect
target and no response that arrived after a hop is read as a result. Every
request is recorded for the receipt before it is sent and its answer is filled
in afterwards, so a request whose answer could not be read is still named; the
bearer token is never recorded and never written anywhere.
"""

import json
import re
import urllib.error
import urllib.request

from ops.deploy.release_native_adapter import REQUEST_TIMEOUT_SECONDS, STATES
from ops.deploy.release_native_adapter import NativeAdapter as _Credentials
from ops.runners.managed_runtime import PRICE_POLICY_JOB_ID, PROJECT, REGION

SCHEDULER_ENDPOINT = "https://cloudscheduler.googleapis.com/v1/"
RUN_ENDPOINT = "https://run.googleapis.com/v2/"
EMPTY_RUN_REQUEST = {}
LOCATION = f"projects/{PROJECT}/locations/{REGION}"
_LOCATION = re.escape(LOCATION)
_JOB_ID = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
_EXECUTION_ID = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_OPERATION_ID = r"[a-z0-9](?:[a-z0-9-]{0,125}[a-z0-9])?"
_JOB_NAME = re.compile(_LOCATION + r"/jobs/(" + _JOB_ID + r")")
_EXECUTION_NAME = re.compile(
    _LOCATION + r"/jobs/(" + _JOB_ID + r")/executions/(" + _EXECUTION_ID + r")"
)
_OPERATION_NAME = re.compile(_LOCATION + r"/operations/(" + _OPERATION_ID + r")")


class NativeError(RuntimeError):
    def __init__(self, call, status, detail):
        super().__init__(f"{call}: http {status}: {detail}")
        self.call = call
        self.status = status


def _refuse(code):
    raise ValueError(code)


def _matched(grammar, name):
    """One resource name, pinned to the approved project and region.

    The grammar admits nothing that can change what a request line means: no
    colon, fragment, query separator, percent escape, extra path segment,
    whitespace, control character or non ascii byte survives it, so the caller
    supplied document cannot reach a verb the call table does not hold.
    """
    if not isinstance(name, str):
        _refuse("resource_name_invalid")
    match = grammar.fullmatch(name)
    if match is None:
        _refuse("resource_name_invalid")
    return match.group(1)


def pinned_job(name) -> str:
    """The job identifier of a scheduler or Cloud Run job resource name."""
    return _matched(_JOB_NAME, name)


def pinned_execution(name) -> tuple[str, str]:
    """The job and execution identifiers of an execution resource name."""
    if not isinstance(name, str):
        _refuse("resource_name_invalid")
    match = _EXECUTION_NAME.fullmatch(name)
    if match is None:
        _refuse("resource_name_invalid")
    return match.group(1), match.group(2)


def pinned_operation(name) -> str:
    return _matched(_OPERATION_NAME, name)


def _job_url(endpoint, name, verb=""):
    return endpoint + LOCATION + "/jobs/" + pinned_job(name) + verb


def build_request(call, **kwargs):
    """The exact request one adapter call sends; the dry run renders these.

    Every URL is built from the endpoint, the pinned location and the validated
    identifier, never by concatenating a caller supplied string onto an endpoint.
    """
    if call == "get_scheduler":
        return {
            "call": call,
            "method": "GET",
            "url": _job_url(SCHEDULER_ENDPOINT, kwargs["name"]),
        }
    if call == "get_job":
        return {
            "call": call,
            "method": "GET",
            "url": _job_url(RUN_ENDPOINT, kwargs["name"]),
        }
    if call == "create_scheduler":
        if kwargs["parent"] != LOCATION:
            _refuse("resource_parent_invalid")
        job = kwargs["job"]
        if type(job) is not dict:
            _refuse("scheduler_job_invalid")
        # The created resource is named by the body, so it is pinned there too.
        pinned_job(job.get("name"))
        return {
            "call": call,
            "method": "POST",
            "url": SCHEDULER_ENDPOINT + LOCATION + "/jobs",
            "json": job,
        }
    if call == "pause_scheduler":
        return {
            "call": call,
            "method": "POST",
            "url": _job_url(SCHEDULER_ENDPOINT, kwargs["name"], ":pause"),
            "json": {},
        }
    if call == "resume_price_policy_scheduler":
        # The only resume this table holds, and it is pinned to one identifier:
        # the daily scheduler's activation belongs to another gate.
        if pinned_job(kwargs["name"]) != PRICE_POLICY_JOB_ID:
            _refuse("scheduler_resume_unapproved")
        return {
            "call": call,
            "method": "POST",
            "url": _job_url(SCHEDULER_ENDPOINT, kwargs["name"], ":resume"),
            "json": {},
        }
    if call == "get_job_iam_policy":
        return {
            "call": call,
            "method": "GET",
            "url": _job_url(RUN_ENDPOINT, kwargs["name"], ":getIamPolicy"),
        }
    if call == "get_execution":
        job, execution = pinned_execution(kwargs["name"])
        return {
            "call": call,
            "method": "GET",
            "url": RUN_ENDPOINT
            + LOCATION
            + "/jobs/"
            + job
            + "/executions/"
            + execution,
        }
    if call == "run_job":
        # The actual empty RunJobRequest: no overrides travel with a managed run.
        return {
            "call": call,
            "method": "POST",
            "url": _job_url(RUN_ENDPOINT, kwargs["name"], ":run"),
            "json": dict(EMPTY_RUN_REQUEST),
        }
    if call == "read_operation":
        seconds = format(float(kwargs["timeout_seconds"]), ".3f").rstrip("0")
        return {
            "call": call,
            "method": "POST",
            "url": RUN_ENDPOINT
            + LOCATION
            + "/operations/"
            + pinned_operation(kwargs["name"])
            + ":wait",
            "json": {"timeout": seconds.rstrip(".") + "s"},
        }
    raise ValueError("unknown_call")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """No 3xx is followed, so a bearer token is never replayed to a new host."""

    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def no_redirect_transport(request):
    """R05's own transport: one request, one response, and no redirect hop.

    An answered status, including a refusal, comes back as a response. A read
    timeout, a reset connection, a name resolution failure and a TLS failure
    are not answers: they are raised, and every caller of this adapter treats
    an OSError as a refusal that may already have changed something.
    """
    http = urllib.request.Request(
        request["url"],
        data=request.get("body"),
        method=request["method"],
        headers=request["headers"],
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(http, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return {
                "status": response.status,
                "headers": dict(response.headers),
                "body": response.read(),
                "url": response.geturl(),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": error.code,
            "headers": dict(error.headers or {}),
            "body": error.read(),
            "url": error.url,
        }


class NativeAdapter:
    def __init__(self, state, *, transport=None, token_source=None):
        if state not in STATES:
            raise ValueError("adapter_state_invalid")
        self.state = state
        self.transport = transport or no_redirect_transport
        self.record = []
        self._token_source = token_source or _Credentials(state).token

    def token(self):
        return self._token_source()

    def render(self, call, **kwargs):
        request = build_request(call, **kwargs)
        rendered = {key: request[key] for key in ("call", "method", "url")}
        if "json" in request:
            rendered["body"] = request["json"]
        return rendered

    def perform(self, call, note=None, **kwargs):
        """Send the request for one call and record what went out and came back."""
        request = build_request(call, **kwargs)
        headers = {
            "Authorization": "Bearer " + self.token(),
            "Accept": "application/json",
        }
        data = None
        entry = {"call": call, "method": request["method"], "url": request["url"]}
        if "json" in request:
            data = json.dumps(
                request["json"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
            entry["request"] = request["json"]
        # The record names a request from the moment it leaves the client, not
        # from the moment an answer could be read: a request whose answer is a
        # redirect, is not json, or never arrives has still been sent, and a
        # receipt that leaves it out understates what exists on the project.
        self.record.append(entry)
        response = self.transport(
            {
                "method": request["method"],
                "url": request["url"],
                "headers": headers,
                "body": data,
            }
        )
        status = _status(call, response, request["url"])
        payload = _payload(call, status, response)
        entry["response"] = payload
        entry["status"] = status
        entry.update(note or {})
        return status, payload

    def get(self, call, **kwargs):
        status, payload = self.perform(call, **kwargs)
        if status == 404:
            return None
        if status != 200:
            raise NativeError(call, status, payload)
        return payload

    def require_ok(self, call, status, payload):
        if status != 200:
            raise NativeError(call, status, payload)
        return payload


def _status(call, response, url):
    """The status of one response, refusing anything that arrived after a hop."""
    if type(response) is not dict or "status" not in response:
        raise NativeError(call, None, "transport_response_invalid")
    status = response["status"]
    if isinstance(status, bool) or not isinstance(status, int):
        raise NativeError(call, None, "transport_response_invalid")
    if 300 <= status < 400:
        raise NativeError(call, status, "redirect_refused")
    arrived = response.get("url")
    if arrived is not None and arrived != url:
        raise NativeError(call, status, "redirect_refused")
    return status


def _payload(call, status, response):
    """The response object, or a refusal: an unparsed body is never a result."""
    body = bytes(response.get("body") or b"")
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise NativeError(call, status, "response_not_json") from None
    if type(payload) is not dict:
        raise NativeError(call, status, "response_not_json")
    return payload


def build(state, *, transport=None, token_source=None):
    return NativeAdapter(state, transport=transport, token_source=token_source)


def factory(state):
    return build(state)
