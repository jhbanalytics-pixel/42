"""The exact Cloud Scheduler v1 and Cloud Run v2 requests the R05 preflight sends,
proved over a fake transport: paths, bodies, the empty RunJobRequest, the response
mapping and the refusal of any call that would resume a scheduler or widen scope.
"""

import json
import urllib.error

import pytest

from ops.deploy import runtime_native_adapter as native

SCHEDULER_NAME = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
)
JOB_NAME = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
)
EXECUTION_NAME = JOB_NAME + "/executions/intelligence-42-daily-staging-abcde"
OPERATION_NAME = "projects/ogilvy-trends-v2/locations/us-central1/operations/op-1"


def _json(status, value):
    return {
        "status": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(value).encode("utf-8"),
    }


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, value = self.responses.pop(0)
        return _json(status, value)


def _adapter(responses):
    adapter = native.NativeAdapter(
        "adc", transport=FakeTransport(responses), token_source=lambda: "fake-token"
    )
    return adapter


def test_get_scheduler_reads_the_v1_job_resource():
    request = native.build_request("get_scheduler", name=SCHEDULER_NAME)
    assert request["method"] == "GET"
    assert request["url"] == native.SCHEDULER_ENDPOINT + SCHEDULER_NAME
    assert "json" not in request


def test_create_scheduler_posts_the_job_under_its_parent():
    job = {"name": SCHEDULER_NAME, "schedule": "0 3 * * *"}
    request = native.build_request(
        "create_scheduler",
        parent="projects/ogilvy-trends-v2/locations/us-central1",
        job=job,
    )
    assert request["method"] == "POST"
    assert request["url"] == (
        native.SCHEDULER_ENDPOINT
        + "projects/ogilvy-trends-v2/locations/us-central1/jobs"
    )
    assert request["json"] == job


def test_pause_scheduler_posts_an_empty_body_to_the_pause_verb():
    request = native.build_request("pause_scheduler", name=SCHEDULER_NAME)
    assert request["method"] == "POST"
    assert request["url"] == native.SCHEDULER_ENDPOINT + SCHEDULER_NAME + ":pause"
    assert request["json"] == {}


def test_run_job_sends_the_actual_empty_run_request():
    request = native.build_request("run_job", name=JOB_NAME)
    assert request["method"] == "POST"
    assert request["url"] == native.RUN_ENDPOINT + JOB_NAME + ":run"
    assert request["json"] == {}
    assert json.dumps(request["json"], separators=(",", ":")).encode("utf-8") == b"{}"


def test_get_job_iam_policy_reads_the_job_scoped_policy():
    request = native.build_request("get_job_iam_policy", name=JOB_NAME)
    assert request["method"] == "GET"
    assert request["url"] == native.RUN_ENDPOINT + JOB_NAME + ":getIamPolicy"


def test_get_execution_reads_the_execution_resource():
    request = native.build_request("get_execution", name=EXECUTION_NAME)
    assert request["method"] == "GET"
    assert request["url"] == native.RUN_ENDPOINT + EXECUTION_NAME


def test_read_operation_waits_with_a_bounded_duration():
    request = native.build_request(
        "read_operation", name=OPERATION_NAME, timeout_seconds=30
    )
    assert request["method"] == "POST"
    assert request["url"].endswith("/operations/op-1:wait")
    assert request["json"] == {"timeout": "30s"}


@pytest.mark.parametrize(
    "call",
    ["resume_scheduler", "delete_scheduler", "update_scheduler", "run_scheduler"],
)
def test_no_call_resumes_deletes_or_rewrites_a_scheduler(call):
    with pytest.raises(ValueError, match="^unknown_call$"):
        native.build_request(call, name=SCHEDULER_NAME)


def test_perform_records_the_request_and_never_the_token():
    adapter = _adapter([(200, {"name": SCHEDULER_NAME, "state": "PAUSED"})])
    status, payload = adapter.perform("get_scheduler", name=SCHEDULER_NAME)
    assert (status, payload["state"]) == (200, "PAUSED")
    entry = adapter.record[0]
    assert entry["call"] == "get_scheduler"
    assert entry["status"] == 200
    assert "fake-token" not in json.dumps(adapter.record)
    sent = adapter.transport.requests[0]
    assert sent["headers"]["Authorization"] == "Bearer fake-token"


def test_a_missing_resource_reads_as_absent_rather_than_an_error():
    adapter = _adapter([(404, {"error": {"message": "not found"}})])
    assert adapter.get("get_scheduler", name=SCHEDULER_NAME) is None


def test_a_denied_read_is_raised_with_its_status():
    adapter = _adapter([(403, {"error": {"message": "permission denied"}})])
    with pytest.raises(native.NativeError) as raised:
        adapter.get("get_scheduler", name=SCHEDULER_NAME)
    assert raised.value.status == 403
    assert raised.value.call == "get_scheduler"


def test_an_unknown_adapter_state_refuses_before_any_request():
    with pytest.raises(ValueError, match="^adapter_state_invalid$"):
        native.NativeAdapter(
            "owner-browser",
            transport=FakeTransport([]),
            token_source=lambda: "fake-token",
        )


def test_render_shows_the_request_without_sending_it():
    adapter = _adapter([])
    rendered = adapter.render("pause_scheduler", name=SCHEDULER_NAME)
    assert rendered == {
        "call": "pause_scheduler",
        "method": "POST",
        "url": native.SCHEDULER_ENDPOINT + SCHEDULER_NAME + ":pause",
        "body": {},
    }
    assert adapter.record == []
    assert adapter.transport.requests == []


# Resource names in a URL


FOREIGN = "projects/other-project/locations/us-central1/jobs/victim"


@pytest.mark.parametrize(
    ("case", "name"),
    [
        ("colon", SCHEDULER_NAME + ":resume"),
        ("fragment", FOREIGN + ":resume#"),
        ("query", SCHEDULER_NAME + "?alt=json"),
        ("deeper_path", SCHEDULER_NAME + "/executions/one"),
        ("whitespace", SCHEDULER_NAME + " "),
        ("inner_whitespace", SCHEDULER_NAME.replace("jobs/", "jobs/ ")),
        ("percent_encoding", SCHEDULER_NAME.replace("jobs/", "jobs/%2e%2e%2f")),
        ("control_character", SCHEDULER_NAME + "\n"),
        ("foreign_project", FOREIGN),
        ("foreign_region", SCHEDULER_NAME.replace("us-central1", "europe-west1")),
        ("relative_segment", SCHEDULER_NAME + "/../victim"),
        ("absolute_url", "https://cloudscheduler.googleapis.com/v1/" + SCHEDULER_NAME),
        ("not_a_string", 42),
    ],
)
def test_a_scheduler_name_outside_the_pinned_grammar_never_reaches_a_url(case, name):
    for call in ("get_scheduler", "pause_scheduler"):
        with pytest.raises(ValueError, match="^resource_name_invalid$"):
            native.build_request(call, name=name)


def test_the_pause_url_is_built_from_the_validated_job_identifier():
    request = native.build_request("pause_scheduler", name=SCHEDULER_NAME)
    assert request["url"] == (
        native.SCHEDULER_ENDPOINT
        + native.LOCATION
        + "/jobs/intelligence-42-daily-staging:pause"
    )


def test_a_create_parent_outside_the_approved_location_refuses():
    job = {"name": SCHEDULER_NAME, "schedule": "0 3 * * *"}
    with pytest.raises(ValueError, match="^resource_parent_invalid$"):
        native.build_request(
            "create_scheduler",
            parent="projects/other-project/locations/us-central1",
            job=job,
        )


def test_a_create_body_naming_another_project_is_never_sent():
    job = {"name": FOREIGN, "schedule": "0 3 * * *"}
    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        native.build_request("create_scheduler", parent=native.LOCATION, job=job)


def test_a_run_job_name_outside_the_pinned_grammar_never_reaches_a_url():
    for call in ("get_job", "run_job", "get_job_iam_policy"):
        with pytest.raises(ValueError, match="^resource_name_invalid$"):
            native.build_request(call, name=FOREIGN + ":run")


def test_an_operation_name_outside_the_pinned_grammar_never_reaches_a_url():
    name = "projects/other-project/locations/us-central1/operations/op-1"
    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        native.build_request("read_operation", name=name, timeout_seconds=30)
    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        native.build_request(
            "read_operation", name=JOB_NAME + ":cancel", timeout_seconds=30
        )


def test_an_execution_name_outside_the_pinned_grammar_never_reaches_a_url():
    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        native.build_request("get_execution", name=EXECUTION_NAME + ":cancel")
    with pytest.raises(ValueError, match="^resource_name_invalid$"):
        native.build_request("get_execution", name=JOB_NAME)


# Transport behaviour


def test_a_redirected_response_is_refused_rather_than_taken_as_a_result():
    adapter = _adapter([(302, {"error": "moved"})])
    with pytest.raises(native.NativeError) as raised:
        adapter.perform("pause_scheduler", name=SCHEDULER_NAME)
    assert raised.value.status == 302


def test_a_response_that_arrived_from_another_url_is_refused():
    def transport(request):
        return {
            "status": 200,
            "headers": {},
            "body": b'{"state": "PAUSED"}',
            "url": "https://cloudscheduler.googleapis.com/v1/elsewhere",
        }

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    with pytest.raises(native.NativeError, match="redirect"):
        adapter.perform("pause_scheduler", name=SCHEDULER_NAME)


def test_a_body_that_does_not_parse_is_refused_rather_than_counted_as_success():
    adapter = native.NativeAdapter(
        "adc",
        transport=lambda request: {
            "status": 200,
            "headers": {},
            "body": b"<html>error page</html>",
        },
        token_source=lambda: "fake-token",
    )
    with pytest.raises(native.NativeError, match="response_not_json"):
        adapter.perform(
            "create_scheduler", parent=native.LOCATION, job={"name": SCHEDULER_NAME}
        )


def test_a_transport_result_without_a_status_refuses_rather_than_raising_a_key_error():
    adapter = native.NativeAdapter(
        "adc",
        transport=lambda request: {"headers": {}, "body": b"{}"},
        token_source=lambda: "fake-token",
    )
    with pytest.raises(native.NativeError, match="transport_response_invalid"):
        adapter.perform("get_scheduler", name=SCHEDULER_NAME)


def test_the_record_carries_the_response_evidence_of_every_call():
    adapter = _adapter([(200, {"name": SCHEDULER_NAME, "state": "PAUSED"})])
    adapter.perform("get_scheduler", name=SCHEDULER_NAME)
    assert adapter.record[0]["response"] == {"name": SCHEDULER_NAME, "state": "PAUSED"}


def test_the_default_transport_never_follows_a_redirect_or_replays_the_token():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path == "/first":
                self.send_response(302)
                self.send_header("Location", "/second")
                self.end_headers()
                return
            body = b'{"state": "PAUSED"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *arguments):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = native.no_redirect_transport(
            {
                "method": "POST",
                "url": f"http://127.0.0.1:{server.server_port}/first",
                "headers": {"Authorization": "Bearer fake-token"},
                "body": b"{}",
            }
        )
    finally:
        server.shutdown()
        server.server_close()
    assert response["status"] == 302
    assert [path for path, _token in seen] == ["/first"]
    assert [token for _path, token in seen] == ["Bearer fake-token"]


def test_a_create_body_that_is_not_an_object_is_never_sent():
    with pytest.raises(ValueError, match="^scheduler_job_invalid$"):
        native.build_request(
            "create_scheduler", parent=native.LOCATION, job=[SCHEDULER_NAME]
        )


# The response guards every call rests on
@pytest.mark.parametrize(
    ("case", "status"),
    [("a_boolean", True), ("a_string", "200"), ("a_float", 200.0), ("absent", None)],
)
def test_a_response_whose_status_is_not_an_integer_is_never_read_as_a_result(
    case, status
):
    def transport(request):
        response = {"headers": {}, "body": b"{}"}
        if case != "absent":
            response["status"] = status
        return response

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    with pytest.raises(native.NativeError, match="transport_response_invalid"):
        adapter.get("get_scheduler", name=SCHEDULER_NAME)


def test_a_transport_result_that_is_not_an_object_is_never_read_as_a_result():
    adapter = native.NativeAdapter(
        "adc", transport=lambda request: ["status", 200], token_source=lambda: "x"
    )
    with pytest.raises(native.NativeError, match="transport_response_invalid"):
        adapter.get("get_scheduler", name=SCHEDULER_NAME)


@pytest.mark.parametrize(
    ("case", "body"),
    [
        ("an_array", b"[]"),
        ("a_string", b'"paused"'),
        ("a_number", b"7"),
        ("a_null", b"null"),
        ("a_boolean", b"true"),
        ("not_json", b"<html>denied</html>"),
        ("not_utf8", b"\xff\xfe"),
    ],
)
def test_a_response_body_that_is_not_a_json_object_is_never_read_as_a_result(
    case, body
):
    def transport(request):
        return {"status": 200, "headers": {}, "body": body}

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    with pytest.raises(native.NativeError, match="response_not_json"):
        adapter.get("get_scheduler", name=SCHEDULER_NAME)


def test_an_empty_response_body_reads_as_an_empty_object():
    def transport(request):
        return {"status": 200, "headers": {}, "body": b""}

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    assert adapter.get("get_scheduler", name=SCHEDULER_NAME) == {}


def test_a_json_object_response_body_reads_as_that_object():
    def transport(request):
        return {"status": 200, "headers": {}, "body": b'{"state":"PAUSED"}'}

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    assert adapter.get("get_scheduler", name=SCHEDULER_NAME) == {"state": "PAUSED"}


# The record names a request from the moment it leaves the client


def test_a_request_whose_answer_is_refused_is_still_named_in_the_record():
    """A 302 to a pause is a request that went out with no readable answer."""

    def transport(request):
        return {
            "status": 302,
            "headers": {"Location": "https://elsewhere/"},
            "body": b"",
        }

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    with pytest.raises(native.NativeError, match="redirect_refused"):
        adapter.perform("pause_scheduler", name=SCHEDULER_NAME)
    assert [entry["call"] for entry in adapter.record] == ["pause_scheduler"]
    assert adapter.record[0]["request"] == {}
    assert "status" not in adapter.record[0]
    assert "response" not in adapter.record[0]


def test_a_request_whose_answer_is_not_json_is_still_named_in_the_record():
    def transport(request):
        return {"status": 200, "headers": {}, "body": b"<html>denied</html>"}

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    with pytest.raises(native.NativeError, match="response_not_json"):
        adapter.perform("pause_scheduler", name=SCHEDULER_NAME)
    assert [entry["call"] for entry in adapter.record] == ["pause_scheduler"]
    assert "response" not in adapter.record[0]


def test_a_request_the_transport_never_answered_is_still_named_in_the_record():
    """The failure a transport that always answers cannot show."""

    def transport(request):
        raise urllib.error.URLError(
            ConnectionResetError(104, "Connection reset by peer")
        )

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    with pytest.raises(OSError):
        adapter.perform(
            "create_scheduler",
            parent=native.LOCATION,
            job={"name": SCHEDULER_NAME, "schedule": "0 5 * * *"},
        )
    assert [entry["call"] for entry in adapter.record] == ["create_scheduler"]
    assert adapter.record[0]["request"]["name"] == SCHEDULER_NAME
    assert "status" not in adapter.record[0]


def test_a_recorded_request_that_was_never_answered_carries_no_bearer_token():
    def transport(request):
        raise urllib.error.URLError(ConnectionResetError(104, "reset"))

    adapter = native.NativeAdapter(
        "adc", transport=transport, token_source=lambda: "fake-token"
    )
    with pytest.raises(OSError):
        adapter.perform("pause_scheduler", name=SCHEDULER_NAME)
    assert "fake-token" not in json.dumps(adapter.record)


PRICE_POLICY_SCHEDULER_NAME = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
    "intelligence-42-price-policy-staging"
)


def test_resume_price_policy_scheduler_posts_an_empty_body_to_the_resume_verb():
    request = native.build_request(
        "resume_price_policy_scheduler", name=PRICE_POLICY_SCHEDULER_NAME
    )
    assert request == {
        "call": "resume_price_policy_scheduler",
        "method": "POST",
        "url": native.SCHEDULER_ENDPOINT + PRICE_POLICY_SCHEDULER_NAME + ":resume",
        "json": {},
    }


@pytest.mark.parametrize(
    "name",
    [
        SCHEDULER_NAME,
        PRICE_POLICY_SCHEDULER_NAME + ":resume",
        (
            "projects/other-project/locations/us-central1/jobs/"
            "intelligence-42-price-policy-staging"
        ),
    ],
)
def test_resume_refuses_every_scheduler_but_the_pinned_price_policy_one(name):
    with pytest.raises(
        ValueError, match="^(scheduler_resume_unapproved|resource_name_invalid)$"
    ):
        native.build_request("resume_price_policy_scheduler", name=name)
