"""A ledger capture's collection receipts bind to the funded pilot's own executions.

On the approval ledger route each collection receipt is bound to one execution of the
funded Wave 1 pilot job: to that execution's result row on the approval ledger, read by the
receipt's result reference, and to the execution itself, read natively from Cloud Run. The
daily chain route is unchanged. Every execution, chain, capture and pin here is synthetic and
unissued; the Cloud Run reads are faked at the HTTP session, and no network or provider call
is made.
"""

import hashlib
import json
import socket
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
import requests
from google.auth.credentials import AnonymousCredentials
from src.analysis.open_intelligence import bridge_native_clients as native
from src.analysis.open_intelligence import daily_child_execution as core
from src.analysis.open_intelligence import source_estate_bridge_evidence as evidence_module
from src.analysis.open_intelligence.daily_execution_authority import (
    DailyAuthorityIntegrityError,
)
from src.analysis.open_intelligence.daily_native_transports import CloudRunTransport

from tests.unit import test_bridge_history_loader as chain_route
from tests.unit import test_bridge_ledger_route as ledger_route
from tests.unit.test_bridge_ledger_route import (
    COLLECTION_COMPLETED,
    COLLECTION_STARTED,
    FUNDED_COMPLETED,
    FUNDED_EXECUTION_ID,
    FUNDED_IDENTITY,
    FUNDED_JOB,
    FUNDED_NAME,
    FUNDED_RUN,
    FUNDED_STARTED,
    SECOND_EXECUTION_ID,
    SECOND_RUN,
    funded_chain,
    funded_execution,
    funded_ref,
    receipt_item,
)
from tests.unit.test_daily_native_transports import BASE, Response, Session
from tests.unit.test_execution_manifest_origins import IMAGE_URI, load_registry

IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
DAILY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
OTHER_ID = "intelligence-42-funded-pilot-staging-other"
WINDOW_START = datetime(2026, 9, 21, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 21, 0, 20, tzinfo=UTC)


# The core execution view, made job parametric over pinned jobs only.


def test_the_funded_pins_are_the_pilot_s_own_job_and_variables():
    assert core.FUNDED_JOB_RESOURCE == FUNDED_JOB
    assert core.FUNDED_EXECUTION_PREFIX == FUNDED_JOB + "/executions/"
    assert core.FUNDED_VARIABLES == {
        "credential_lane": ("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded"),
        "funded_stage": ("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1"),
    }


def test_the_funded_view_reads_a_completed_execution_of_the_pilot_job():
    assert core.native_funded_execution_view(funded_execution()) == {
        "execution_name": FUNDED_NAME,
        "job_resource": FUNDED_JOB,
        "image_uri": IMAGE_URI,
        "service_identity": FUNDED_IDENTITY,
        "execution_created_at": "2026-09-21T00:00:40.000000+00:00",
        "execution_started_at": "2026-09-21T00:00:45.000000+00:00",
        "credential_lane": "ogilvy_funded",
        "funded_stage": "stage_1_wave_1",
        "terminal_state": "succeeded",
        "completed_at": FUNDED_COMPLETED,
    }


def test_a_running_funded_execution_view_carries_no_terminal_state():
    view = core.native_funded_execution_view(funded_execution("running"))
    assert "terminal_state" not in view
    assert "completed_at" not in view


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_the_funded_view_reads_the_terminal_state_through_the_strict_terminal_rules(state):
    assert core.native_funded_execution_view(funded_execution(state))["terminal_state"] == state


def _succeeded_with_a_failed_task(value):
    value["failedCount"] = 1


def _completed_before_start(value):
    value["completionTime"] = "2026-09-21T00:00:44.000000Z"


@pytest.mark.parametrize("change", [_succeeded_with_a_failed_task, _completed_before_start])
def test_an_ambiguous_funded_terminal_read_refuses_with_the_shared_terminal_code(change):
    value = funded_execution()
    change(value)
    with pytest.raises(
        core.DailyChildRefusal, match=r"^daily_native_execution_terminal_ambiguous$"
    ):
        core.native_funded_execution_view(value)


def _env(value):
    return value["template"]["containers"][0]["env"]


def _daily_name(value):
    value["name"] = DAILY_JOB + "/executions/" + FUNDED_EXECUTION_ID


def _sibling_job(value):
    value["name"] = FUNDED_JOB + "-2/executions/" + FUNDED_EXECUTION_ID
    value["job"] = "intelligence-42-funded-pilot-staging-2"


def _job_field(value):
    value["job"] = "intelligence-42-daily-staging"


def _missing_lane(value):
    _env(value)[:] = [e for e in _env(value) if e["name"] != "SOCIALCRAWL_CREDENTIAL_LANE"]


def _missing_stage(value):
    _env(value)[:] = [e for e in _env(value) if e["name"] != "SOCIALCRAWL_FUNDED_STAGE_NAME"]


def _repeated_lane(value):
    _env(value).append({"name": "SOCIALCRAWL_CREDENTIAL_LANE", "value": "ogilvy_funded"})


def _secret_lane(value):
    for entry in _env(value):
        if entry["name"] == "SOCIALCRAWL_CREDENTIAL_LANE":
            del entry["value"]
            entry["valueSource"] = {"secretKeyRef": {"secret": "lane", "version": "1"}}


def _other_lane(value):
    for entry in _env(value):
        if entry["name"] == "SOCIALCRAWL_CREDENTIAL_LANE":
            entry["value"] = "ogilvy_free"


def _other_stage(value):
    for entry in _env(value):
        if entry["name"] == "SOCIALCRAWL_FUNDED_STAGE_NAME":
            entry["value"] = "stage_2_wave_2"


def _second_container(value):
    containers = value["template"]["containers"]
    containers.append(deepcopy(containers[0]))


def _no_identity(value):
    del value["template"]["serviceAccount"]


@pytest.mark.parametrize(
    "change",
    [
        _daily_name,
        _sibling_job,
        _job_field,
        _missing_lane,
        _missing_stage,
        _repeated_lane,
        _secret_lane,
        _other_lane,
        _other_stage,
        _second_container,
        _no_identity,
    ],
)
def test_the_funded_view_refuses_what_is_not_one_execution_of_the_pinned_pilot_job(change):
    value = funded_execution()
    change(value)
    with pytest.raises(core.DailyChildRefusal, match=r"^funded_native_execution_invalid$"):
        core.native_funded_execution_view(value)


def test_a_funded_execution_that_has_not_started_refuses():
    value = funded_execution("running")
    del value["startTime"]
    with pytest.raises(core.DailyChildRefusal, match=r"^funded_native_execution_not_started$"):
        core.native_funded_execution_view(value)


def test_the_daily_view_still_admits_only_the_daily_job():
    with pytest.raises(core.DailyChildRefusal, match=r"^daily_native_execution_invalid$"):
        core.native_execution_view(funded_execution())


# The Cloud Run transport: the same GET, pinned to the pilot job for a funded read.


def test_the_transport_reads_one_funded_execution_through_the_funded_view():
    session = Session({BASE + FUNDED_NAME: Response(200, funded_execution())})
    view = CloudRunTransport(session).read_funded(FUNDED_NAME)
    assert view == core.native_funded_execution_view(funded_execution())
    assert session.calls == [("GET", BASE + FUNDED_NAME, None)]


@pytest.mark.parametrize(
    "name",
    [
        DAILY_JOB + "/executions/intelligence-42-daily-staging-abcde",
        FUNDED_JOB + "-2/executions/" + FUNDED_EXECUTION_ID,
        FUNDED_NAME + ":cancel",
        FUNDED_NAME + "/tasks/t",
    ],
)
def test_the_transport_refuses_a_name_outside_the_pilot_job_before_any_read(name):
    session = Session({})
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_native_name_forbidden$"):
        CloudRunTransport(session).read_funded(name)
    assert session.calls == []


def test_the_daily_read_still_refuses_a_funded_execution_name():
    session = Session({})
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_native_name_forbidden$"):
        CloudRunTransport(session).read(FUNDED_NAME)
    assert session.calls == []


def test_an_ambiguous_funded_terminal_read_is_an_integrity_refusal_at_the_transport():
    session = Session({BASE + FUNDED_NAME: Response(200, funded_execution(failedCount=1))})
    with pytest.raises(
        DailyAuthorityIntegrityError, match=r"^daily_native_execution_terminal_ambiguous$"
    ):
        CloudRunTransport(session).read_funded(FUNDED_NAME)


# The bridge's funded execution reader: one bounded GET under the serving identity.


class Run:
    """Answers GETs on run.googleapis.com below the authorized session, so every retry or
    refresh the session attempted would be seen here."""

    def __init__(self):
        self.calls = []
        self.served = {}
        self.error = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        status, body = self.served.get(url, (404, b'{"error":{"code":404}}'))
        value = requests.Response()
        value.status_code = status
        value._content = body
        value.url = url
        return value


@pytest.fixture
def run_api(monkeypatch):
    value = Run()
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda _self, method, url, **kw: value.request(method, url, **kw),
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))
    for name in (
        "BIGQUERY_EMULATOR_HOST",
        "STORAGE_EMULATOR_HOST",
        "GOOGLE_API_USE_CLIENT_CERTIFICATE",
        "GOOGLE_API_USE_MTLS_ENDPOINT",
        "GENERAL_QUESTION_BRIDGE_READS",
    ):
        monkeypatch.delenv(name, raising=False)
    return value


def credentials():
    value = AnonymousCredentials()
    value.token = "synthetic"
    value.service_account_email = IDENTITY
    return value


def serve(run_api, value, status=200, name=FUNDED_NAME):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    run_api.served[BASE + name] = (status, body)


def read(execution_id=FUNDED_EXECUTION_ID):
    return native.BridgeFundedExecutions(credentials()).read(execution_id)


def test_the_reader_makes_one_bounded_get_of_the_pinned_execution(run_api):
    serve(run_api, funded_execution())
    assert read() == core.native_funded_execution_view(funded_execution())
    ((method, url, kwargs),) = run_api.calls
    assert (method, url) == ("GET", BASE + FUNDED_NAME)
    assert kwargs["timeout"] == native.READ_TIMEOUT == 30
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "execution_id",
    [
        "intelligence-42-daily-staging-abcde",
        "intelligence-42-ingest-staging-abcde",
        "intelligence-42-funded-pilot-staging-2-abcde",
        "intelligence-42-funded-pilot-staging-ABCDE",
        "intelligence-42-funded-pilot-staging-abcd",
        "intelligence-42-funded-pilot-staging-abcdef",
        "intelligence-42-funded-pilot-staging-abc/e",
        "intelligence-42-funded-pilot-staging-abcde\n",
        "exec-1",
        "",
        None,
        7,
    ],
)
def test_the_reader_admits_only_an_execution_id_of_the_pilot_job_before_any_call(
    run_api, execution_id
):
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_invalid$"):
        read(execution_id)
    assert run_api.calls == []


def test_a_denied_read_is_forbidden(run_api):
    serve(run_api, {"error": {"code": 403}}, status=403)
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_forbidden$"):
        read()
    assert len(run_api.calls) == 1


@pytest.mark.parametrize("status", [401, 404, 429, 500, 503])
def test_any_other_status_is_unavailable_after_one_call_and_no_retry(run_api, status):
    serve(run_api, {"error": {"code": status}}, status=status)
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_unavailable$"):
        read()
    assert len(run_api.calls) == 1


def test_a_failed_connection_is_unavailable_after_one_call(run_api):
    run_api.error = requests.ConnectionError("synthetic")
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_unavailable$"):
        read()
    assert len(run_api.calls) == 1


def _ambiguous(value):
    value["failedCount"] = 1


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", _daily_name, _ambiguous, _missing_lane, _other_stage]
)
def test_a_body_that_is_not_the_pinned_execution_is_invalid(run_api, body):
    if callable(body):
        value = funded_execution()
        body(value)
        body = value
    serve(run_api, body)
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_invalid$"):
        read()


def test_a_served_execution_other_than_the_one_asked_for_reads_as_that_execution(run_api):
    serve(run_api, funded_execution(execution_id=OTHER_ID))
    # The reader reports what Cloud Run served; the binding compares it with the ledger.
    assert read()["execution_name"].endswith("-other")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GOOGLE_API_USE_CLIENT_CERTIFICATE", "true"),
        ("GOOGLE_API_USE_MTLS_ENDPOINT", "always"),
    ],
)
def test_the_reader_refuses_a_redirected_environment_before_any_call(
    run_api, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=r"^bridge_environment_invalid$"):
        read()
    assert run_api.calls == []


class _Recorder:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return "served"


def test_the_one_get_session_serves_one_get_of_its_own_url_at_the_read_timeout():
    url = BASE + FUNDED_NAME
    for other_url, timeout in ((url + "x", native.READ_TIMEOUT), (url, 31)):
        recorder = _Recorder()
        with pytest.raises(ValueError, match=r"^bridge_native_request_refused$"):
            native._OneGet(recorder, url).get(other_url, timeout=timeout)
        assert recorder.calls == []
    recorder = _Recorder()
    session = native._OneGet(recorder, url)
    assert session.get(url, timeout=native.READ_TIMEOUT) == "served"
    with pytest.raises(ValueError, match=r"^bridge_native_request_refused$"):
        session.get(url, timeout=native.READ_TIMEOUT)
    assert recorder.calls == [("GET", url, {"timeout": 30, "allow_redirects": False})]


class RefreshingCredentials(AnonymousCredentials):
    """Credentials whose refresh succeeds, so a session that retried on 401 would retry."""

    def refresh(self, request):
        self.token = "refreshed"


def test_an_unauthorized_read_is_not_retried_after_a_credential_refresh(run_api):
    serve(run_api, {"error": {"code": 401}}, status=401)
    value = RefreshingCredentials()
    value.token = "synthetic"
    value.service_account_email = IDENTITY
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_unavailable$"):
        native.BridgeFundedExecutions(value).read(FUNDED_EXECUTION_ID)
    assert len(run_api.calls) == 1


def test_the_production_evidence_reads_funded_executions_through_the_reader(run_api):
    serve(run_api, funded_execution())
    evidence = native.production_bridge_clients(credentials())["evidence"]
    assert evidence.read_funded_execution(FUNDED_EXECUTION_ID)["execution_name"] == FUNDED_NAME
    assert [call[1] for call in run_api.calls] == [BASE + FUNDED_NAME]


def test_evidence_without_a_funded_reader_refuses_the_ledger_binding():
    evidence = evidence_module.BridgeEvidence(
        locate_chain=None, read_source_run=None, read_completion=None, read_product_rows=None
    )
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_unavailable$"):
        evidence.read_funded_execution(FUNDED_EXECUTION_ID)


def test_evidence_wraps_an_uncoded_reader_failure_as_unavailable():
    def fail(execution_id):
        raise RuntimeError("synthetic")

    evidence = evidence_module.BridgeEvidence(
        locate_chain=None,
        read_source_run=None,
        read_completion=None,
        read_product_rows=None,
        read_funded_execution=fail,
    )
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_unavailable$"):
        evidence.read_funded_execution(FUNDED_EXECUTION_ID)
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        evidence.read_funded_execution(" padded")


def test_evidence_keeps_a_coded_reader_refusal():
    def refuse(execution_id):
        raise ValueError("bridge_funded_execution_forbidden")

    evidence = evidence_module.BridgeEvidence(
        locate_chain=None,
        read_source_run=None,
        read_completion=None,
        read_product_rows=None,
        read_funded_execution=refuse,
    )
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_forbidden$"):
        evidence.read_funded_execution(FUNDED_EXECUTION_ID)


# The binding of one collection receipt to its funded execution.


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def chains(*values):
    """A ledger reader over funded chains keyed by consumption id."""
    served = {value["consumption"].consumption_id: value["rows"] for value in values}
    reads = []

    def read_chain(consumption_id):
        reads.append(consumption_id)
        return deepcopy(served.get(consumption_id, []))

    read_chain.reads = reads
    return read_chain


def views(*executions):
    """A funded execution reader over raw executions keyed by id, through the core view."""
    served = {value["name"].rsplit("/", 1)[1]: value for value in executions}
    reads = []

    def read_execution(execution_id):
        reads.append(execution_id)
        return core.native_funded_execution_view(deepcopy(served[execution_id]))

    read_execution.reads = reads
    return read_execution


def bind(chain, *, item=None, read_chain=None, reader=None, generation_loader=None, **window):
    arguments = {"window_start": WINDOW_START, "window_end": WINDOW_END, **window}
    return evidence_module.resolve_funded_collection(
        receipt_item(chain) if item is None else item,
        read_chain=read_chain or chains(chain),
        read_funded_execution=reader or views(funded_execution()),
        generation_loader=generation_loader or chain["catalogue"],
        **arguments,
    )


def refuses(code, chain, **arguments):
    with pytest.raises(ValueError, match=f"^{code}$"):
        bind(chain, **arguments)


def test_a_sound_receipt_binds_to_its_result_row_and_its_succeeded_execution(registry):
    chain = funded_chain(registry)
    read_chain, reader = chains(chain), views(funded_execution())
    assert bind(chain, read_chain=read_chain, reader=reader) == {
        "execution_id": FUNDED_EXECUTION_ID,
        "pipeline_run_id": FUNDED_RUN,
        "result_json": chain["result"].canonical_result_json,
    }
    assert read_chain.reads == [chain["consumption"].consumption_id]
    assert reader.reads == [FUNDED_EXECUTION_ID]


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("failed", "bridge_funded_execution_not_succeeded"),
        ("cancelled", "bridge_funded_execution_not_succeeded"),
        ("running", "bridge_funded_execution_running"),
    ],
)
def test_an_execution_that_did_not_succeed_binds_nothing(registry, state, code):
    refuses(code, funded_chain(registry), reader=views(funded_execution(state)))


def test_an_execution_of_another_job_binds_nothing(registry):
    view = core.native_funded_execution_view(funded_execution())
    view.update(
        execution_name=DAILY_JOB + "/executions/" + FUNDED_EXECUTION_ID, job_resource=DAILY_JOB
    )
    refuses(
        "bridge_funded_execution_differs",
        funded_chain(registry),
        reader=lambda execution_id: deepcopy(view),
    )


def test_an_execution_other_than_the_ledger_s_binds_nothing(registry):
    other = funded_execution(execution_id=OTHER_ID)
    refuses(
        "bridge_funded_execution_differs",
        funded_chain(registry),
        reader=lambda execution_id: core.native_funded_execution_view(other),
    )


@pytest.mark.parametrize(
    ("started", "completed"),
    [
        ("2026-09-20T23:59:59.999999Z", FUNDED_COMPLETED),
        (FUNDED_STARTED, "2026-09-21T00:20:00.000001Z"),
    ],
)
def test_an_execution_outside_the_capture_window_binds_nothing(registry, started, completed):
    refuses(
        "bridge_funded_execution_outside_window",
        funded_chain(registry),
        reader=views(funded_execution(started=started, completed=completed)),
    )


def test_the_window_edges_bind(registry):
    chain = funded_chain(registry)
    reader = views(
        funded_execution(started="2026-09-21T00:00:00Z", completed="2026-09-21T00:20:00Z")
    )
    assert bind(chain, reader=reader)["execution_id"] == FUNDED_EXECUTION_ID


# The execution proves one collection day: it started at or after the observation window
# closed and before the next day's window closed. Both edges, to the microsecond.
_STARTED = datetime(2026, 9, 21, 0, 0, 45, tzinfo=UTC)
_TICK = timedelta(microseconds=1)
_DAY = timedelta(days=1)


@pytest.mark.parametrize(
    ("window_start", "binds"),
    [
        (_STARTED + _TICK, False),
        (_STARTED, True),
        (_STARTED - _TICK, True),
        (_STARTED - _DAY + _TICK, True),
        (_STARTED - _DAY, False),
        (_STARTED - _DAY - _TICK, False),
    ],
    ids=[
        "lower_edge_after_start",
        "lower_edge_at_start",
        "lower_edge_before_start",
        "upper_edge_after_start",
        "upper_edge_at_start",
        "upper_edge_before_start",
    ],
)
def test_the_execution_start_binds_only_inside_the_one_day_after_the_window(
    registry, window_start, binds
):
    assert funded_execution()["startTime"] == "2026-09-21T00:00:45.000000Z"
    chain = funded_chain(registry)
    if binds:
        assert bind(chain, window_start=window_start)["execution_id"] == FUNDED_EXECUTION_ID
    else:
        refuses("bridge_funded_execution_outside_window", chain, window_start=window_start)


def test_ask_admission_binds_the_funded_run_from_the_profile_s_window_end(monkeypatch, tmp_path):
    # Ask admission uses the same binding, so the one day bound holds for Ask as well: the
    # window it passes starts where the approved profile's observation window ends.
    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    real = evidence_module.resolve_funded_collection
    windows = []

    def recorded(item, **arguments):
        windows.append((arguments["window_start"], arguments["window_end"]))
        return real(item, **arguments)

    monkeypatch.setattr(evidence_module, "resolve_funded_collection", recorded)
    assert ledger_route.load(w).collection_runs == (FUNDED_EXECUTION_ID,)
    profile = w["plan"]["snapshot_plan"]
    assert profile["observation_window_end"] == "2026-09-21T00:00:00.000000Z"
    assert windows == [
        (
            datetime(2026, 9, 21, tzinfo=UTC),
            datetime.fromisoformat(profile["collection_snapshot_as_of"]),
        )
    ]
    monkeypatch.setattr(
        evidence_module,
        "resolve_funded_collection",
        lambda item, **arguments: real(
            item, **dict(arguments, window_start=arguments["window_start"] - _DAY)
        ),
    )
    ledger_route.refused(w)


@pytest.mark.parametrize(
    ("identity", "image"),
    [
        ("intelligence-42-ingest@ogilvy-trends-v2.iam.gserviceaccount.com", IMAGE_URI),
        (FUNDED_IDENTITY, IMAGE_URI.rsplit(":", 1)[0] + ":" + "0" * 64),
    ],
)
def test_an_execution_whose_identity_or_image_differ_binds_nothing(registry, identity, image):
    value = funded_execution(image=image)
    value["template"]["serviceAccount"] = identity
    refuses("bridge_funded_execution_differs", funded_chain(registry), reader=views(value))


def test_an_execution_that_started_after_the_approval_was_consumed_binds_nothing(registry):
    # The pilot consumes its approval once it runs, so the consumption is inside it.
    refuses(
        "bridge_funded_collection_outside_execution",
        funded_chain(registry),
        reader=views(funded_execution(started="2026-09-21T00:01:00.000001Z")),
    )


def test_a_result_recorded_after_the_execution_completed_binds_nothing(registry):
    refuses(
        "bridge_funded_collection_outside_execution",
        funded_chain(registry),
        reader=views(funded_execution(completed="2026-09-21T00:14:29.999999Z")),
    )


@pytest.mark.parametrize(
    ("started", "completed"),
    [
        ("2026-09-21T00:00:44.999999Z", COLLECTION_COMPLETED),
        (COLLECTION_STARTED, "2026-09-21T00:14:30.000001Z"),
    ],
)
def test_a_collection_outside_its_execution_and_result_binds_nothing(registry, started, completed):
    chain = funded_chain(registry)
    item = receipt_item(chain, collection_started_at=started, collection_completed_at=completed)
    refuses("bridge_funded_collection_outside_execution", chain, item=item)


def test_the_collection_may_span_the_execution_to_its_recorded_result(registry):
    chain = funded_chain(registry)
    item = receipt_item(
        chain,
        collection_started_at=FUNDED_STARTED,
        collection_completed_at="2026-09-21T00:14:30.000000Z",
    )
    assert bind(chain, item=item)["execution_id"] == FUNDED_EXECUTION_ID


def test_a_swapped_receipt_binds_nothing(registry):
    chain = funded_chain(registry)
    other = funded_chain(registry, payload={"attribution_state": "gap_detected"})
    item = receipt_item(chain, collection_receipt_digest=other["result"].result_digest)
    refuses("bridge_funded_receipt_differs", chain, item=item)


def test_a_receipt_naming_another_execution_binds_nothing_even_when_that_one_succeeded(
    registry,
):
    chain = funded_chain(registry)
    item = receipt_item(chain, run_id=OTHER_ID)
    reader = views(funded_execution(), funded_execution(execution_id=OTHER_ID))
    refuses("bridge_funded_receipt_differs", chain, item=item, reader=reader)
    assert reader.reads == []


def test_a_result_ref_whose_fields_differ_from_the_ledger_row_binds_nothing(registry):
    chain = funded_chain(registry)
    for name in ("manifest_sha256", "result_id", "result_digest"):
        ref = funded_ref(chain)
        ref[name] = "e" * 64 if name != "result_id" else "exr_" + "e" * 64
        refuses("bridge_funded_result_differs", chain, item=receipt_item(chain, result_ref=ref))


def test_a_result_ref_of_another_consumption_binds_nothing(registry):
    chain = funded_chain(registry)
    other = funded_chain(registry, consumed_at=datetime(2026, 9, 21, 0, 1, 1, tzinfo=UTC))
    ref = funded_ref(chain)
    ref["consumption_id"] = other["consumption"].consumption_id
    refuses(
        "bridge_funded_result_differs",
        chain,
        item=receipt_item(chain, result_ref=ref),
        read_chain=chains(chain, other),
    )


def test_a_failed_funded_result_binds_nothing(registry):
    chain = funded_chain(registry, status="failed")
    refuses("bridge_funded_result_not_succeeded", chain)


def test_a_result_ref_of_another_operation_binds_nothing(registry):
    chain = funded_chain(registry)
    ref = dict(funded_ref(chain), operation="source_snapshot_capture")
    reader = chains(chain)
    refuses(
        "bridge_funded_result_not_funded",
        chain,
        item=receipt_item(chain, result_ref=ref),
        read_chain=reader,
    )
    assert reader.reads == []


def test_a_funded_result_under_another_reference_binds_nothing(registry):
    chain = funded_chain(registry, reference_suffix="#funded-run-open")
    refuses("bridge_funded_receipt_differs", chain)


def test_a_chain_the_ledger_does_not_hold_binds_nothing(registry):
    chain = funded_chain(registry)
    refuses("bridge_funded_result_invalid", chain, read_chain=chains())
    other = funded_chain(registry, consumed_at=datetime(2026, 9, 21, 0, 1, 1, tzinfo=UTC))
    refuses("bridge_funded_result_invalid", chain, read_chain=lambda _id: deepcopy(other["rows"]))


def test_a_generation_the_catalogue_does_not_trust_binds_nothing(registry):
    from tests.unit.test_execution_records_v2 import RESOURCE_SHA
    from tests.unit.test_retained_readers_v2 import FakeCatalogue

    chain = funded_chain(registry)
    loader = FakeCatalogue(registry, (registry.sha256, "f" * 64))
    refuses("bridge_funded_result_invalid", chain, generation_loader=loader)
    loader = FakeCatalogue(registry, ("f" * 64, RESOURCE_SHA))
    refuses("bridge_funded_result_invalid", chain, generation_loader=loader)


def test_an_unreadable_ledger_is_unavailable_and_a_coded_refusal_is_kept(registry):
    chain = funded_chain(registry)

    def fail(consumption_id):
        raise RuntimeError("synthetic")

    def refuse(consumption_id):
        raise ValueError("bridge_ledger_invalid")

    refuses("bridge_funded_result_unavailable", chain, read_chain=fail)
    refuses("bridge_ledger_invalid", chain, read_chain=refuse)


def test_a_reader_that_returns_no_view_binds_nothing(registry):
    refuses("bridge_funded_execution_invalid", funded_chain(registry), reader=lambda _id: None)


def test_a_coded_reader_refusal_is_the_binding_s_refusal(registry):
    def refuse(execution_id):
        raise ValueError("bridge_funded_execution_forbidden")

    refuses("bridge_funded_execution_forbidden", funded_chain(registry), reader=refuse)


@pytest.mark.parametrize(
    "change",
    [
        lambda item: 0,
        lambda item: [],
        lambda item: {"run_id": item["run_id"]},
        lambda item: dict(item, extra=1),
    ],
)
def test_a_malformed_receipt_row_binds_nothing(registry, change):
    chain = funded_chain(registry)
    refuses("bridge_evidence_invalid", chain, item=change(receipt_item(chain)))


def test_a_malformed_result_ref_binds_nothing(registry):
    chain = funded_chain(registry)
    ref = funded_ref(chain)
    del ref["result_id"]
    refuses("bridge_evidence_invalid", chain, item=receipt_item(chain, result_ref=ref))


@pytest.mark.parametrize(
    "window",
    [
        {"window_start": datetime(2026, 9, 21)},
        {"window_end": "2026-09-21T00:20:00Z"},
    ],
)
def test_an_unformed_capture_window_binds_nothing(registry, window):
    refuses("bridge_evidence_invalid", funded_chain(registry), **window)


# The loader: the ledger route takes this binding; the daily chain route is untouched.


def test_the_ledger_route_binds_its_receipt_to_the_funded_execution_not_a_chain(
    monkeypatch, tmp_path
):
    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    loaded = ledger_route.load(w)
    assert loaded.collection_runs == (FUNDED_EXECUTION_ID,)
    assert w["evidence"].execution_reads == [FUNDED_EXECUTION_ID]
    assert w["ledger_reads"][1:] == [w["funded"]["consumption"].consumption_id]
    reads = w["evidence"].reads
    assert ("chain", "daily_source_collection") not in reads
    assert not [read for read in reads if read[0] == "run"]


def test_the_daily_chain_route_keeps_its_chain_binding(monkeypatch, tmp_path):
    w = chain_route.world()
    chain_route.register(monkeypatch, tmp_path, w["capture"])
    assert not hasattr(w["evidence"], "read_funded_execution")
    loaded = chain_route.load(w)
    assert loaded.collection_runs == ("run-20260920",)
    assert ("chain", "daily_source_collection") in w["evidence"].reads
    assert ("run", "run-20260920") in w["evidence"].reads


def _failed(w):
    w["executions"][FUNDED_EXECUTION_ID] = funded_execution("failed")


def _running(w):
    w["executions"][FUNDED_EXECUTION_ID] = funded_execution("running")


def _other_job_served(w):
    value = funded_execution()
    _sibling_job(value)
    w["executions"][FUNDED_EXECUTION_ID] = value


def _other_execution_served(w):
    w["executions"][FUNDED_EXECUTION_ID] = funded_execution(execution_id=OTHER_ID)


def _early(w):
    w["executions"][FUNDED_EXECUTION_ID] = funded_execution(started="2026-09-20T23:59:59.999999Z")


def _late(w):
    w["executions"][FUNDED_EXECUTION_ID] = funded_execution(completed="2026-09-21T00:20:00.000001Z")


def _other_identity(w):
    value = funded_execution()
    value["template"]["serviceAccount"] = IDENTITY
    w["executions"][FUNDED_EXECUTION_ID] = value


def _absent(w):
    del w["executions"][FUNDED_EXECUTION_ID]


@pytest.mark.parametrize(
    "forge",
    [
        _failed,
        _running,
        _other_job_served,
        _other_execution_served,
        _early,
        _late,
        _other_identity,
        _absent,
    ],
)
def test_a_forged_funded_execution_refuses_the_ledger_capture(monkeypatch, tmp_path, forge):
    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    forge(w)
    ledger_route.refused(w)


def test_a_swapped_receipt_refuses_the_ledger_capture(monkeypatch, tmp_path):
    w = ledger_route.ledger_world(tmp_path, receipt_changes={"collection_receipt_digest": "9" * 64})
    ledger_route.register(monkeypatch, tmp_path, w)
    ledger_route.refused(w)
    assert w["evidence"].execution_reads == []


def test_a_swapped_funded_result_row_refuses_the_ledger_capture(monkeypatch, tmp_path):
    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    registry = w["funded"]["catalogue"].registry
    other = funded_chain(registry, payload={"attribution_state": "gap_detected"})
    w["funded"] = dict(w["funded"], rows=other["rows"])
    ledger_route.refused(w)


def test_a_failed_funded_result_refuses_the_ledger_capture(monkeypatch, tmp_path):
    w = ledger_route.ledger_world(tmp_path, funded_changes={"status": "failed"})
    ledger_route.register(monkeypatch, tmp_path, w)
    ledger_route.refused(w)


def test_a_collection_outside_its_execution_refuses_the_ledger_capture(monkeypatch, tmp_path):
    w = ledger_route.ledger_world(
        tmp_path, receipt_changes={"collection_started_at": "2026-09-21T00:00:30.000000Z"}
    )
    ledger_route.register(monkeypatch, tmp_path, w)
    ledger_route.refused(w)


def test_the_production_evidence_admits_a_ledger_capture_through_its_funded_read(
    run_api, monkeypatch, tmp_path
):
    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    serve(run_api, w["executions"][FUNDED_EXECUTION_ID])
    production = native.production_bridge_clients(credentials())["evidence"]

    class Evidence:
        """The loader world's chain evidence for history, the production funded read."""

        def __getattr__(self, name):
            return getattr(w["evidence"], name)

        read_funded_execution = staticmethod(production.read_funded_execution)

    loaded = ledger_route.load(w, evidence=Evidence())
    assert loaded.collection_runs == (FUNDED_EXECUTION_ID,)
    assert [call[1] for call in run_api.calls] == [BASE + FUNDED_NAME]
    assert w["evidence"].execution_reads == []
    run_api.calls.clear()
    serve(run_api, funded_execution("failed"))
    ledger_route.refused(w, evidence=Evidence())
    assert len(run_api.calls) == 1


# The pipeline run the funded close names: the key raw_content and enriched_content rows
# carry (pipeline_run_id) that ties a collected row to the funded execution that wrote it.


def close_json(**gdelt):
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    from tests.unit.test_bridge_ledger_route import FUNDED_CLOSE

    value = deepcopy(FUNDED_CLOSE)
    value["gdelt"] = gdelt
    raw = canonical_bytes(value).decode()
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def test_the_funded_close_names_the_pipeline_run_its_rows_carry():
    raw, digest = close_json(persistence={"complete": True, "run_id": FUNDED_RUN})
    assert evidence_module.funded_pipeline_run(raw, digest) == FUNDED_RUN


@pytest.mark.parametrize(
    "gdelt",
    [
        {},
        {"persistence": None},
        {"persistence": {"run_id": FUNDED_RUN}},
        {"persistence": {"complete": False, "run_id": FUNDED_RUN}},
        {"persistence": {"complete": 1, "run_id": FUNDED_RUN}},
        {"persistence": {"complete": True}},
        {"persistence": {"complete": True, "run_id": None}},
        {"persistence": {"complete": True, "run_id": ""}},
        {"persistence": {"complete": True, "run_id": "Run-1"}},
        {"persistence": {"complete": True, "run_id": "-run"}},
        {"persistence": {"complete": True, "run_id": "r" * 129}},
        {"persistence": {"complete": True, "run_id": FUNDED_RUN + "\n"}},
    ],
)
def test_a_funded_close_without_a_complete_named_run_names_none(gdelt):
    raw, digest = close_json(**gdelt)
    with pytest.raises(ValueError, match=r"^bridge_funded_run_invalid$"):
        evidence_module.funded_pipeline_run(raw, digest)


def test_a_funded_close_that_is_not_the_receipted_bytes_names_none():
    raw, digest = close_json(persistence={"complete": True, "run_id": FUNDED_RUN})
    for value, expected in ((raw + " ", digest), (raw, "0" * 64), (raw.encode(), digest)):
        with pytest.raises(ValueError, match=r"^bridge_funded_run_differs$"):
            evidence_module.funded_pipeline_run(value, expected)
    with pytest.raises(ValueError, match=r"^bridge_funded_run_invalid$"):
        evidence_module.funded_pipeline_run("[]", hashlib.sha256(b"[]").hexdigest())


def test_a_funded_result_without_a_named_run_binds_nothing(registry):
    chain = funded_chain(registry, payload={"attribution_state": "complete", "gdelt": {}})
    refuses("bridge_funded_run_invalid", chain)


def test_the_loader_keeps_the_pipeline_runs_of_the_bound_executions(monkeypatch, tmp_path):
    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    loaded = ledger_route.load(w)
    assert loaded.collection_row_runs == (FUNDED_RUN,)
    receipts = json.loads(w["ledger_raw"]["collection_receipt_set"])
    assert loaded.collection_evidence == {
        "collection_receipt_set": receipts,
        "funded_results": [w["funded"]["result"].canonical_result_json],
    }


def test_the_daily_chain_route_keeps_no_row_run_filter(monkeypatch, tmp_path):
    w = chain_route.world()
    chain_route.register(monkeypatch, tmp_path, w["capture"])
    loaded = chain_route.load(w)
    assert loaded.collection_row_runs is None
    assert loaded.collection_evidence is None


# The stored source record carries what readback re-proves the row runs from.


def plan_for(window_end="2026-09-20"):
    return {"window": {"start": "2026-09-14", "end": window_end}, "markets": ["ng", "za"]}


def request_for():
    return {"as_of": chain_route.AS_OF, "client_scope_id": "ogilvy_default"}


def stored(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.general_question_context_admission import (
        bridge_source_record,
    )

    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    return w, bridge_source_record(ledger_route.load(w))


def restored(record):
    from src.analysis.open_intelligence.general_question_context_admission import (
        restore_bridge_source,
    )

    return restore_bridge_source(record, request=request_for(), plan=plan_for())


def test_readback_re_proves_the_row_runs_from_the_stored_record(monkeypatch, tmp_path):
    w, record = stored(monkeypatch, tmp_path)
    assert record["collection_evidence"]["funded_results"] == [
        w["funded"]["result"].canonical_result_json
    ]
    source = restored(record)
    assert source.collection_row_runs == (FUNDED_RUN,)
    assert source.collection_evidence == record["collection_evidence"]


def _swap_result(record):
    raw, _ = close_json(persistence={"complete": True, "run_id": "replay-run"})
    record["collection_evidence"]["funded_results"] = [raw]


def _extra_result(record):
    results = record["collection_evidence"]["funded_results"]
    results.append(results[0])


def _no_results(record):
    record["collection_evidence"]["funded_results"] = []


def _swap_receipt_set(record):
    receipts = record["collection_evidence"]["collection_receipt_set"]["receipts"]
    receipts[0]["collection_receipt_digest"] = "9" * 64


def _reworded_receipt(record):
    # Every receipt keeps its digest, so each stored close still hashes to its receipt; only
    # the pinned digest of the whole receipt set tells this set from the capture's.
    receipts = record["collection_evidence"]["collection_receipt_set"]["receipts"]
    receipts[0]["collection_started_at"] = "2026-09-21T00:02:30.000000Z"


def _extra_set_key(record):
    record["collection_evidence"]["collection_receipt_set"]["note"] = "replayed"


def _no_evidence(record):
    record["collection_evidence"] = None


def _extra_key(record):
    record["collection_evidence"]["rows"] = []


def _dropped_field(record):
    del record["collection_evidence"]


@pytest.mark.parametrize(
    "change",
    [
        _swap_result,
        _extra_result,
        _no_results,
        _swap_receipt_set,
        _reworded_receipt,
        _extra_set_key,
        _no_evidence,
        _extra_key,
        _dropped_field,
    ],
)
def test_readback_refuses_a_record_whose_row_runs_it_cannot_prove(monkeypatch, tmp_path, change):
    from src.analysis.open_intelligence.general_question_context_admission import (
        BridgeContextInvalid,
    )

    _, record = stored(monkeypatch, tmp_path)
    change(record)
    with pytest.raises(BridgeContextInvalid, match=r"^bridge_context_invalid$"):
        restored(record)


def test_readback_refuses_one_more_or_one_fewer_close_than_receipts(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.general_question_context_admission import (
        BridgeContextInvalid,
    )

    _, record = stored(monkeypatch, tmp_path)
    receipts = record["collection_evidence"]["collection_receipt_set"]["receipts"]
    results = record["collection_evidence"]["funded_results"]
    assert len(results) == len(receipts) == 1
    for changed in (results * 2, []):
        altered = deepcopy(record)
        altered["collection_evidence"]["funded_results"] = changed
        with pytest.raises(BridgeContextInvalid, match=r"^bridge_context_invalid$"):
            restored(altered)
    assert restored(record).collection_row_runs == (FUNDED_RUN,)


def test_a_daily_chain_record_carries_no_collection_evidence(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.general_question_context_admission import (
        BridgeContextInvalid,
        bridge_source_record,
        restore_bridge_source,
    )

    w = chain_route.world()
    chain_route.register(monkeypatch, tmp_path, w["capture"])
    record = bridge_source_record(chain_route.load(w))
    assert record["collection_evidence"] is None
    source = restore_bridge_source(record, request=request_for(), plan=plan_for())
    assert source.collection_row_runs is None
    record["collection_evidence"] = {"collection_receipt_set": {}, "funded_results": []}
    with pytest.raises(BridgeContextInvalid):
        restore_bridge_source(record, request=request_for(), plan=plan_for())


# The read: a ledger capture's collection read selects only its bound runs' rows.


def run_ids(query):
    assert "  AND pipeline_run_id IN UNNEST(@run_ids)\n" in query["sql"]
    (runs,) = [item for item in query["parameters"] if item["name"] == "run_ids"]
    return [item["value"] for item in runs["parameterValue"]["arrayValues"]]


@pytest.mark.parametrize("lane", ["raw_content", "enriched_content"])
def test_the_ledger_collection_read_selects_only_the_bound_runs(monkeypatch, tmp_path, lane):
    from src.analysis.open_intelligence.bridge_history_queries import bridge_collection_query

    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    query = bridge_collection_query(
        ledger_route.load(w), lane, window_start="2026-09-14", window_end="2026-09-20"
    )
    assert run_ids(query) == [FUNDED_RUN]


def test_a_capture_of_two_funded_runs_binds_and_reads_both(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.bridge_history_queries import bridge_collection_query
    from src.analysis.open_intelligence.general_question_context_admission import (
        bridge_source_record,
    )

    w = ledger_route.ledger_world(tmp_path, second_run=SECOND_RUN)
    ledger_route.register(monkeypatch, tmp_path, w)
    loaded = ledger_route.load(w)
    both = tuple(sorted((FUNDED_RUN, SECOND_RUN)))
    assert sorted(loaded.collection_runs) == sorted((FUNDED_EXECUTION_ID, SECOND_EXECUTION_ID))
    assert loaded.collection_row_runs == both
    for lane in ("raw_content", "enriched_content"):
        query = bridge_collection_query(
            loaded, lane, window_start="2026-09-14", window_end="2026-09-20"
        )
        assert run_ids(query) == list(both)
    # Readback re-proves both runs from the stored receipt set and both funded closes.
    assert restored(bridge_source_record(loaded)).collection_row_runs == both


def test_a_ledger_capture_with_no_bound_run_reads_nothing(monkeypatch, tmp_path):
    # An empty run filter would select no row at all; the read refuses rather than issue a
    # query that silently reads nothing for a ledger capture.
    from dataclasses import replace

    from src.analysis.open_intelligence.bridge_history_queries import bridge_collection_query

    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    loaded = replace(ledger_route.load(w), collection_row_runs=())
    for lane in ("raw_content", "enriched_content"):
        with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
            bridge_collection_query(
                loaded, lane, window_start="2026-09-14", window_end="2026-09-20"
            )


def test_the_daily_chain_collection_read_is_unchanged(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.bridge_history_queries import bridge_collection_query

    w = chain_route.world()
    chain_route.register(monkeypatch, tmp_path, w["capture"])
    query = bridge_collection_query(
        chain_route.load(w), "raw_content", window_start="2026-09-14", window_end="2026-09-20"
    )
    assert "pipeline_run_id" not in query["sql"]
    assert [item["name"] for item in query["parameters"]] == [
        "markets",
        "window_start",
        "window_end",
    ]


def test_the_collection_projection_carries_the_row_s_pipeline_run():
    from src.analysis.open_intelligence.general_question_context_queries import (
        BRIDGE_COLLECTION_FIELDS,
    )

    assert "pipeline_run_id" in BRIDGE_COLLECTION_FIELDS
