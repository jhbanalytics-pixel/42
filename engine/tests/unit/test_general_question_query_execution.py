import copy
import importlib
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery
from src.analysis.open_intelligence.general_question_calls import GeneralQuestionCalls
from src.analysis.open_intelligence.general_question_plan import validate_question_plan
from src.analysis.open_intelligence.general_question_planning import build_question_planning_request

from tests.unit import test_general_question_runtime as runtime
from tests.unit import test_general_question_store as f


def fixture():
    store, bucket, invocation, identity, credentials = runtime.setup_runtime()
    context = store.read_request(invocation["request_id"], scope=f.scope())
    sdk = build_question_planning_request(
        context["request"], context["intake"], policy=store.policy, remaining_seconds=60
    )
    calls = GeneralQuestionCalls(store)
    permit = calls.claim_call(
        invocation["request_id"],
        stage="planning",
        scope=f.scope(),
        input_digest=sdk.input_digest,
        system_instruction_digest=sdk.system_instruction_digest,
        response_schema_digest=sdk.response_schema_digest,
        counted_input_tokens=50,
        now=f.NOW,
    )
    response = {
        "model_version": "gemini-3.5-flash",
        "usage_metadata": {
            "prompt_token_count": 50,
            "candidates_token_count": 10,
            "thoughts_token_count": 5,
            "total_token_count": 65,
        },
        "candidates": [
            {
                "finish_reason": "STOP",
                "content": {"role": "model", "parts": [{"text": json.dumps(runtime.plan_draft())}]},
            }
        ],
    }
    calls.record_response(
        permit, scope=f.scope(), response=response, received_at=f.NOW + timedelta(seconds=1)
    )
    calls.persist_usage(
        invocation["request_id"], stage="planning", scope=f.scope(), persist=lambda e: e
    )
    plan = validate_question_plan(
        runtime.plan_draft(), request=context["request"], intake=context["intake"]
    )
    store._objects.create(f"requests/{invocation['request_id']}/plan.json", plan)
    return store, bucket, invocation, identity, credentials, plan


class HTTP:
    def __init__(self, identity, mode="success", bucket=None):
        self.identity = identity
        self.bucket = bucket
        self.mode = mode
        self.calls = []
        self.resource = None
        self.mutate = None
        self.result_mutate = None
        self.final_mutate = None

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "bigquery.googleapis.com"
        data = json.loads(kwargs["data"]) if kwargs.get("data") else None
        self.calls.append((method, parsed.path, parse_qs(parsed.query), data))
        code = 200
        if method == "POST":
            self.resource = copy.deepcopy(data)
            self.resource.update(
                user_email=self.identity["service_account_email"],
                status={"state": "DONE"},
                statistics={
                    "query": {
                        "statementType": "SELECT",
                        "totalBytesBilled": "100",
                        "totalBytesProcessed": "90",
                    }
                },
            )
            if self.bucket is not None:
                # The bucket is written by other threads while this stub answers
                # a submit, so the object map is read under the bucket's own lock
                # and snapshotted before it is decoded. Walking the live mapping
                # raises "dictionary changed size during iteration" the moment a
                # neighbouring thread stores anything, and that error reaches the
                # caller inside the suppressed submit, leaving the job resource
                # without a creation time.
                with self.bucket.lock:
                    stored = [
                        raw
                        for name, (_gen, raw) in self.bucket.objects.items()
                        if name.endswith("/execution.json")
                    ]
                markers = [json.loads(raw) for raw in stored]
                marker = next(
                    value for value in markers if value["job_id"] == data["jobReference"]["jobId"]
                )
                self.resource["statistics"]["creationTime"] = str(
                    int(datetime.fromisoformat(marker["recorded_at"]).timestamp() * 1000)
                )
            if self.mutate:
                self.mutate(self.resource)
            if self.mode == "timeout":
                raise requests.exceptions.Timeout("synthetic unknown acknowledgement")
            if self.mode == "conflict":
                code = 409
                body = {"error": {"code": code, "message": "synthetic conflict"}}
            else:
                body = self.resource
        elif (
            self.resource is None
            or self.mode == "missing"
            or parsed.path.rsplit("/", 1)[-1] != self.resource["jobReference"]["jobId"]
        ):
            code = 404
            body = {"error": {"code": code, "message": "synthetic missing"}}
        elif "/queries/" in parsed.path:
            body = {
                "jobReference": self.resource["jobReference"],
                "jobComplete": True,
                "totalRows": "1",
                "schema": {"fields": [{"name": "signal_id", "type": "STRING"}]},
                "rows": [{"f": [{"v": "synthetic_signal"}]}],
            }
        else:
            if self.final_mutate and any("/queries/" in path for _, path, *_ in self.calls):
                self.final_mutate(self.resource)
            body = self.resource
        if "/queries/" in parsed.path and self.result_mutate:
            self.result_mutate(body)
        response = requests.Response()
        response.status_code = code
        response.headers["content-type"] = "application/json"
        response._content = json.dumps(body).encode()
        response.request = requests.Request(method, url).prepare()
        return response


def install(monkeypatch, http):
    monkeypatch.setattr(
        AuthorizedSession, "request", lambda _self, *args, **kwargs: http.request(*args, **kwargs)
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))


def _context_wire(http, rows):
    def result(body):
        names = list(rows[0])
        schema = [
            {
                "name": name,
                "type": "INT64" if type(rows[0][name]) is int else "STRING",
                **({"mode": "REPEATED"} if name == "sample_row_ids" else {}),
            }
            for name in names
        ]

        def cell(name, value):
            return [{"v": v} for v in value] if name == "sample_row_ids" else value

        body.update(
            totalRows=str(len(rows)),
            schema={"fields": schema},
            rows=[{"f": [{"v": cell(name, row[name])} for name in names]} for row in rows],
        )

    http.result_mutate = result


@pytest.mark.parametrize("collision", [False, True])
def test_context_source_reader_uses_owned_job_and_zero_inspection_slots(monkeypatch, collision):
    from tests.unit import test_general_question_context_queries as context_fixture

    values = fixture()
    rows, consumption_id = context_fixture.authority_rows()
    if collision:
        rows[0]["result_count"] = 2
    http = HTTP(values[3], bucket=values[1])
    _context_wire(http, rows)
    install(monkeypatch, http)
    executor = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    output = executor.execute_context_result_query(
        values[2],
        store=values[0],
        scope=f.scope(),
        runtime_identity=values[3],
        credentials=values[4],
        plan=values[5],
        ordinal=1,
        consumption_id=consumption_id,
        maximum_bytes_billed=1000000,
        now=f.NOW + timedelta(seconds=2),
    )
    assert output["application_status"] == ("refused" if collision else "accepted")
    assert output["observed_candidate_count"] == 0
    assert output["receipt"]["template_id"] == "protected_context_result_v1"
    assert len([call for call in http.calls if call[0] == "POST"]) == 1
    if not collision:
        assert output["material"]["consumption"].consumption_id == consumption_id
        assert output["prepared_query"].candidate_limit == 0


def test_context_query_unknown_cap_version_refuses_before_writes(monkeypatch):
    from datetime import UTC

    from tests.unit import test_general_question_context_queries as context_fixture

    monkeypatch.setattr(f, "NOW", datetime(2026, 9, 9, 12, tzinfo=UTC))
    store, bucket, invocation, identity, credentials, plan = fixture()
    source = context_fixture.inputs()[3]
    source["client_scope_id"] = f.scope()["client_scope_id"]
    http = HTTP(identity, bucket=bucket)
    _context_wire(http, context_fixture.candidate_rows(source))
    install(monkeypatch, http)
    executor = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    before = list(bucket.uploads)
    with pytest.raises(executor.QuestionStoreError, match=r"^snapshot_cap_version_unknown$"):
        executor.execute_context_candidate_query(
            invocation,
            store=store,
            scope=f.scope(),
            runtime_identity=identity,
            credentials=credentials,
            plan=plan,
            source_binding=source,
            ordinal=1,
            candidate_limit=10,
            maximum_bytes_billed=1_000_000,
            cap_version=3,
            now=f.NOW + timedelta(seconds=2),
        )
    assert bucket.uploads == before
    assert http.calls == []


@pytest.mark.parametrize("parent_failure", [None, "builder", "decoder", "untrusted"])
def test_context_evidence_requires_actual_owned_candidate_receipt(monkeypatch, parent_failure):
    from datetime import UTC

    from tests.unit import test_general_question_context_queries as context_fixture

    monkeypatch.setattr(f, "NOW", datetime(2026, 9, 9, 12, tzinfo=UTC))
    values = fixture()
    source = context_fixture.inputs()[3]
    source["client_scope_id"] = f.scope()["client_scope_id"]
    rows = context_fixture.candidate_rows(source)
    http = HTTP(values[3], bucket=values[1])
    _context_wire(http, rows)
    install(monkeypatch, http)
    executor = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    common = {
        "store": values[0],
        "scope": f.scope(),
        "runtime_identity": values[3],
        "credentials": values[4],
        "plan": values[5],
        "source_binding": source,
        "maximum_bytes_billed": 1000000,
        "now": f.NOW + timedelta(seconds=2),
    }
    first = executor.execute_context_candidate_query(
        values[2], ordinal=1, candidate_limit=10, **common
    )
    assert first["application_status"] == "accepted"
    assert first["observed_candidate_count"] == 1
    _context_wire(http, context_fixture.index_rows(source))
    indexed = executor.execute_context_index_query(
        values[2],
        ordinal=2,
        lane="enriched_content",
        candidate_rows=first["rows"],
        candidate_limit=10,
        **common,
    )
    assert indexed["application_status"] == "accepted", indexed.get("reason")
    if parent_failure:
        from src.analysis.open_intelligence.general_question_parent_capsule import (
            ParentSourceUnavailable,
        )

        def fail(*args, **kwargs):
            if parent_failure == "untrusted":
                raise ValueError("parent_source_unavailable PRIVATE")
            raise ParentSourceUnavailable()

        monkeypatch.setattr(
            executor,
            "build_context_evidence_query"
            if parent_failure == "builder"
            else "decode_context_rows",
            fail,
        )
    _context_wire(http, [context_fixture.metadata(source, 0, 0, evidence=True)])
    if parent_failure == "builder":
        with pytest.raises(executor.QuestionStoreError, match=r"^parent_source_unavailable$"):
            executor.execute_context_evidence_query(
                values[2],
                ordinal=3,
                lane="enriched_content",
                candidate_rows=first["rows"],
                index_rows=indexed["rows"],
                candidate_limit=10,
                **common,
            )
        return
    second = executor.execute_context_evidence_query(
        values[2],
        ordinal=3,
        lane="enriched_content",
        candidate_rows=first["rows"],
        index_rows=indexed["rows"],
        candidate_limit=10,
        **common,
    )
    if parent_failure:
        assert second["application_status"] == "refused"
        assert second["reason"] == (
            "parent_source_unavailable"
            if parent_failure == "decoder"
            else "context_material_invalid"
        )
        return
    assert second["application_status"] == "accepted", second.get("reason")
    assert second["observed_candidate_count"] == 0
    altered = copy.deepcopy(first["rows"])
    altered[-1]["sample_row_ids"] = ["unowned"]
    with pytest.raises(executor.QuestionStoreError, match="context_predecessor_unavailable"):
        executor.execute_context_evidence_query(
            values[2],
            ordinal=4,
            lane="enriched_content",
            candidate_rows=altered,
            candidate_limit=10,
            index_rows=indexed["rows"],
            **common,
        )
    altered_index = copy.deepcopy(indexed["rows"])
    altered_index[-1]["payload"] = altered_index[-1]["payload"].replace("2026-09-01", "2026-08-01")
    with pytest.raises(executor.QuestionStoreError, match="context_predecessor_unavailable"):
        executor.execute_context_evidence_query(
            values[2],
            ordinal=4,
            lane="enriched_content",
            candidate_rows=first["rows"],
            index_rows=altered_index,
            candidate_limit=10,
            **common,
        )
    assert len([call for call in http.calls if call[0] == "POST"]) == 3


def test_context_index_counts_predecessor_validation_before_native_observation(monkeypatch):
    from datetime import UTC

    from tests.unit import test_general_question_context_queries as context_fixture

    monkeypatch.setattr(f, "NOW", datetime(2026, 9, 9, 12, tzinfo=UTC))
    values = fixture()
    source = context_fixture.inputs()[3]
    source["client_scope_id"] = f.scope()["client_scope_id"]
    rows = context_fixture.candidate_rows(source)
    http = HTTP(values[3], bucket=values[1])
    _context_wire(http, rows)
    install(monkeypatch, http)
    executor = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    common = {
        "store": values[0],
        "scope": f.scope(),
        "runtime_identity": values[3],
        "credentials": values[4],
        "plan": values[5],
        "source_binding": source,
        "maximum_bytes_billed": 1000000,
        "now": f.NOW + timedelta(seconds=2),
    }
    first = executor.execute_context_candidate_query(
        values[2], ordinal=1, candidate_limit=10, **common
    )
    elapsed = [0.0]
    monkeypatch.setattr(executor, "monotonic", lambda: elapsed[0])
    owned = executor._owned_context_rows

    def slow_owned(*args, **kwargs):
        result = owned(*args, **kwargs)
        elapsed[0] += 2
        return result

    monkeypatch.setattr(executor, "_owned_context_rows", slow_owned)

    def native_created_after_submission(resource):
        elapsed[0] += 1
        resource["statistics"]["creationTime"] = str(
            int((common["now"] + timedelta(seconds=3)).timestamp() * 1000)
        )

    http.mutate = native_created_after_submission
    _context_wire(http, context_fixture.index_rows(source))
    indexed = executor.execute_context_index_query(
        values[2],
        ordinal=2,
        lane="enriched_content",
        candidate_rows=first["rows"],
        candidate_limit=10,
        **common,
    )
    assert indexed["application_status"] == "accepted", indexed.get("reason")


def execute(values, **overrides):
    store, _, invocation, identity, credentials, plan = values
    arguments = {
        "store": store,
        "scope": f.scope(),
        "runtime_identity": identity,
        "credentials": credentials,
        "plan": plan,
        "candidate_limit": 2,
        "maximum_bytes_billed": 1000000,
        "now": f.NOW + timedelta(seconds=2),
    }
    arguments.update(overrides)
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    return module.execute_released_candidate_query(invocation, **arguments)


def test_current_copy_route_validates_four_fixed_rows_under_owned_receipt(monkeypatch):
    from tests.unit import test_general_question_copy_validation as copy_fixture

    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    proof = copy_fixture.proof_rows()

    def result(body):
        fields = list(proof[0])
        body.update(
            totalRows=str(len(proof)),
            schema={
                "fields": [
                    {
                        "name": field,
                        "type": "INT64" if type(proof[0][field]) is int else "STRING",
                    }
                    for field in fields
                ]
            },
            rows=[{"f": [{"v": row[field]} for field in fields]} for row in proof],
        )

    http.result_mutate = result
    install(monkeypatch, http)
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    output = module.execute_current_source_copy_query(
        values[2],
        store=values[0],
        scope=f.scope(),
        runtime_identity=values[3],
        credentials=values[4],
        plan=values[5],
        ordinal=1,
        maximum_bytes_billed=1000000,
        now=f.NOW + timedelta(seconds=2),
    )
    assert output["application_status"] == "accepted"
    assert output["receipt"]["template_id"] == "current_source_copy_v1"
    assert output["receipt"]["result_digest"] == module.canonical_digest(output["rows"])
    assert output["material"]["content_validated"] is True
    assert output["material"]["source_authority"] is False
    assert sum(method == "POST" for method, *_ in http.calls) == 1


@pytest.mark.parametrize("mode", ["success", "timeout", "conflict"])
def test_one_insert_and_exact_same_job_recovery(monkeypatch, mode):
    values = fixture()
    http = HTTP(values[3], mode, bucket=values[1])
    install(monkeypatch, http)
    first = execute(values)
    assert first["application_status"] == "accepted"
    assert first["receipt"]["query_state"] == "succeeded"
    assert first["billed_bytes"] == 100
    assert first["total_rows"] == 1
    assert first["rows"] == [{"signal_id": "synthetic_signal"}]
    assert execute(values)["job_id"] == first["job_id"]
    assert sum(method == "POST" for method, *_ in http.calls) == 1


def test_missing_job_after_unknown_submit_never_reinserts(monkeypatch):
    values = fixture()
    http = HTTP(values[3], "timeout", bucket=values[1])
    install(monkeypatch, http)
    original = http.request

    def disappear(method, *args, **kwargs):
        try:
            return original(method, *args, **kwargs)
        finally:
            if method == "POST":
                http.mode = "missing"

    http.request = disappear
    assert execute(values)["application_status"] == "unavailable"
    assert execute(values)["application_status"] == "unavailable"
    assert sum(method == "POST" for method, *_ in http.calls) == 1


@pytest.mark.parametrize("mutation", ["credentials", "plan", "metering", "invocation", "emulator"])
def test_unadmitted_inputs_never_reach_http(monkeypatch, mutation):
    values = fixture()
    _store, bucket, invocation, _, credentials, plan = values
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)
    if mutation == "credentials":
        credentials.service_account_email = "other@example.invalid"
    elif mutation == "plan":
        plan["requirements"][0]["question"] = "Altered plan"
    elif mutation == "metering":
        control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
        control["requests"][invocation["request_id"]]["execution"]["calls"]["planning"][
            "metering_failed"
        ] = True
        bucket.seed(f.LEDGER, control)
    elif mutation == "invocation":
        invocation["request_digest"] = "0" * 64
    else:
        monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "localhost:9050")
    with pytest.raises(f.module().QuestionStoreError):
        execute(values)
    assert http.calls == []


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("creator", "query_job_invalid"),
        ("state", "query_job_invalid"),
        ("sql", "query_job_config_invalid"),
        ("parameters", "query_job_config_invalid"),
        ("timeout", "query_job_config_invalid"),
        ("legacy", "query_job_config_invalid"),
        ("destination", "query_destination_invalid"),
        ("statement", "query_statement_invalid"),
    ],
)
def test_server_metadata_tampering_refuses_without_result_or_reinsert(
    monkeypatch, mutation, reason
):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])

    def alter(value):
        if mutation == "creator":
            value["user_email"] = "other@example.invalid"
        if mutation == "state":
            value["status"] = {}
        if mutation == "sql":
            value["configuration"]["query"]["query"] = "SELECT 2"
        if mutation == "parameters":
            value["configuration"]["query"]["queryParameters"] = []
        if mutation == "timeout":
            value["configuration"]["jobTimeoutMs"] = "999999"
        if mutation == "legacy":
            value["configuration"]["query"]["useLegacySql"] = True
        if mutation == "destination":
            value["configuration"]["query"]["destinationTable"] = {
                "projectId": "ogilvy-trends-v2",
                "datasetId": "trends_v2",
                "tableId": "protected",
            }
        if mutation == "statement":
            value["statistics"]["query"]["statementType"] = "CREATE_TABLE_AS_SELECT"

    http.mutate = alter
    install(monkeypatch, http)
    result = execute(values)
    assert result["application_status"] == "refused"
    assert result["reason"] == reason
    assert sum(method == "POST" for method, *_ in http.calls) == 1
    assert not any("/queries/" in path for _, path, *_ in http.calls)


def test_default_destination_is_native_metadata_not_requested_write_configuration(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    destination = {
        "projectId": "ogilvy-trends-v2",
        "datasetId": "_synthetic_default",
        "tableId": "anon_result",
    }
    http.mutate = lambda value: value["configuration"]["query"].update(destinationTable=destination)
    install(monkeypatch, http)
    result = execute(values)
    assert result["application_status"] == "accepted"
    posted = next(data for method, _, _, data in http.calls if method == "POST")
    assert "destinationTable" not in posted["configuration"]["query"]
    observation = values[0]._objects.read(result["observation"]["object_key"]).value
    assert observation["native_configuration"]["query"]["destinationTable"] == destination
    assert "destinationTable" not in observation["requested_configuration"]["query"]


@pytest.mark.parametrize("failed", [False, True])
def test_native_cost_overage_is_retained_without_falsifying_native_done(monkeypatch, failed):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])

    def overage(value):
        value["statistics"]["query"]["totalBytesBilled"] = "101"
        if failed:
            value["status"]["errorResult"] = {"reason": "resourcesExceeded", "message": "synthetic"}

    http.mutate = overage
    install(monkeypatch, http)
    result = execute(values, maximum_bytes_billed=100)
    assert result["application_status"] == "refused"
    assert result["native_job_state"] == "DONE"
    assert result["billed_bytes"] == 101
    assert result["receipt"]["query_state"] == "failed"
    assert result["receipt"]["billed_bytes"] == 101
    observation = values[0]._objects.read(result["observation"]["object_key"]).value
    assert observation["native_error_reason"] == ("resourcesExceeded" if failed else None)


def test_existing_reservation_without_execution_marker_never_bootstraps(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)
    first = execute(values)
    key = f.PREFIX + f"requests/{values[2]['request_id']}/queries/1/execution.json"
    del values[1].objects[key]
    count = len(http.calls)
    result = execute(values)
    assert result["reason"] == "query_execution_unknown"
    assert result["job_id"] == first["job_id"]
    assert len(http.calls) == count


@pytest.mark.parametrize(
    "case", ["missing_billing", "boolean_billing", "too_many_rows", "missing_total"]
)
def test_native_unknown_or_over_limit_result_never_claims_complete_data(monkeypatch, case):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    if case == "missing_billing":
        http.mutate = lambda value: value["statistics"]["query"].pop("totalBytesBilled")
    elif case == "boolean_billing":
        http.mutate = lambda value: value["statistics"]["query"].update(totalBytesBilled=False)
    elif case == "too_many_rows":
        http.result_mutate = lambda value: value.update(totalRows="3")
    else:
        http.result_mutate = lambda value: value.pop("totalRows")
    install(monkeypatch, http)
    result = execute(values)
    assert result["application_status"] != "accepted"
    assert result["rows"] == []
    if case in ("missing_billing", "boolean_billing"):
        assert result["billed_bytes"] is None
    if case == "too_many_rows":
        assert result["receipt"]["query_state"] == "failed"
        assert result["receipt"]["observed_candidate_count"] == 3
    assert sum(method == "POST" for method, *_ in http.calls) == 1


def test_slow_execution_marker_never_submits_after_deadline(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    elapsed = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])

    def slow(name):
        if name.endswith("execution.json"):
            elapsed[0] += 5

    values[1].before_upload = slow
    with pytest.raises(f.module().QuestionStoreError, match="request_expired"):
        execute(values, now=f.NOW + timedelta(seconds=239))
    assert http.calls == []


def test_sdk_pagination_is_bounded_and_uses_same_job(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])

    def pages(body):
        body["totalRows"] = "2"
        if http.calls[-1][2].get("pageToken"):
            body["rows"] = [{"f": [{"v": "second_signal"}]}]
        else:
            body["pageToken"] = "page_two"

    http.result_mutate = pages
    install(monkeypatch, http)
    result = execute(values)
    assert result["rows"] == [{"signal_id": "synthetic_signal"}, {"signal_id": "second_signal"}]
    assert result["total_rows"] == 2
    assert sum(method == "POST" for method, *_ in http.calls) == 1


def test_result_cannot_trigger_a_hidden_sdk_job_insert(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)

    def unexpected_begin(job, **_kwargs):
        job._properties["status"] = {}
        job._begin(retry=None, timeout=1)

    monkeypatch.setattr(bigquery.QueryJob, "result", unexpected_begin)
    result = execute(values)
    assert result["reason"] == "query_resubmission_refused"
    assert sum(method == "POST" for method, *_ in http.calls) == 1


def test_concurrent_execution_marker_grants_only_one_post(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)
    barrier = threading.Barrier(2)
    count = [0]
    lock = threading.Lock()

    def race(name):
        if name.endswith("execution.json"):
            with lock:
                count[0] += 1
                wait = count[0] <= 2
            if wait:
                barrier.wait(timeout=3)

    values[1].before_upload = race
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(values), range(2)))
    assert len({result["job_id"] for result in results}) == 1
    assert any(result["application_status"] == "accepted" for result in results)
    assert sum(method == "POST" for method, *_ in http.calls) == 1


def test_expired_recovery_preserves_original_config_receipt_and_no_new_post(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)
    first = execute(values)
    spec_key = f"requests/{values[2]['request_id']}/queries/1/execution.json"
    original = values[0]._objects.read(spec_key).raw
    recovered = execute(values, now=f.NOW + timedelta(days=2))
    assert recovered["reason"] == "request_expired"
    assert recovered["receipt"] == first["receipt"]
    assert recovered["job_id"] == first["job_id"]
    assert values[0]._objects.read(spec_key).raw == original
    assert sum(method == "POST" for method, *_ in http.calls) == 1


@pytest.mark.parametrize("boundary", ["creator", "configuration", "final_configuration"])
def test_unadopted_metadata_is_never_stored_or_charged_to_request(monkeypatch, boundary):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])

    def foreign(value):
        if boundary == "creator":
            value["user_email"] = "FOREIGN_CREATOR@example.invalid"
        else:
            value["configuration"]["query"]["query"] = "SELECT 'FOREIGN_SQL'"
            value["configuration"]["query"]["queryParameters"][0]["parameterValue"]["value"] = (
                "FOREIGN_PARAMETER"
            )
        value["statistics"]["query"]["totalBytesBilled"] = "999999999"

    if boundary == "final_configuration":
        http.final_mutate = foreign
    else:
        http.mutate = foreign
    install(monkeypatch, http)
    result = execute(values)
    assert result["application_status"] == "refused"
    assert result["billed_bytes"] is None
    assert result["total_rows"] is None
    assert result["receipt"]["query_state"] == "running"
    observation = values[0]._objects.read(result["observation"]["object_key"]).value
    for field in (
        "native_configuration",
        "native_creator_email",
        "native_job_reference",
        "native_statement_type",
    ):
        assert observation[field] is None
    assert "FOREIGN_" not in json.dumps(result)
    assert all(b"FOREIGN_" not in raw for _generation, raw in values[1].objects.values())


@pytest.mark.parametrize("state", ["RUNNING", "PENDING"])
def test_final_native_state_must_still_be_done(monkeypatch, state):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    http.final_mutate = lambda value: value["status"].update(state=state)
    install(monkeypatch, http)
    result = execute(values)
    assert result["application_status"] != "accepted"
    assert result["rows"] == []
    assert result["receipt"]["query_state"] == "running"


def test_failed_unknown_cost_can_refine_to_known_overage_and_hold_next_query(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])

    def failed(value):
        value["status"]["errorResult"] = {"reason": "resourcesExceeded", "message": "synthetic"}
        value["statistics"]["query"].pop("totalBytesBilled")

    http.mutate = failed
    install(monkeypatch, http)
    # Each execution takes the clock its caller holds at that moment, and the
    # caller advances that clock between stages. Handing every execution the one
    # frozen instant rewinds it instead: the reservation the first execution
    # stamps carries the real time that execution spent, so a first execution
    # slowed by a loaded machine records a moment the second execution has not
    # reached, and the refusal lands on the job's creation time rather than on
    # its cost. The later calls therefore carry later clocks, as the caller does.
    assert execute(values, maximum_bytes_billed=100)["receipt"]["billed_bytes"] is None
    http.resource["statistics"]["query"]["totalBytesBilled"] = "101"
    refined = execute(values, maximum_bytes_billed=100, now=f.NOW + timedelta(seconds=30))
    assert refined["receipt"]["billed_bytes"] == 101
    before = len(http.calls)
    with pytest.raises(f.module().QuestionStoreError, match="query_budget_exhausted"):
        execute(values, ordinal=2, now=f.NOW + timedelta(seconds=60))
    assert len(http.calls) == before


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "old",
        "pre_marker",
        "future",
        "future_read_clock",
        "at_deadline",
        "malformed",
        "nonfinite",
    ],
)
def test_creation_timestamp_must_match_original_execution_window(monkeypatch, case):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])

    def alter(value):
        if case == "missing":
            value["statistics"].pop("creationTime")
        elif case == "old":
            value["statistics"]["creationTime"] = "1"
        elif case in ("pre_marker", "future_read_clock", "at_deadline"):
            seconds = {"pre_marker": 1, "future_read_clock": 60, "at_deadline": 180}[case]
            value["statistics"]["creationTime"] = str(
                int((f.NOW + timedelta(seconds=seconds)).timestamp() * 1000)
            )
        elif case == "future":
            value["statistics"]["creationTime"] = str(
                int((f.NOW + timedelta(days=2)).timestamp() * 1000)
            )
        elif case == "malformed":
            value["statistics"]["creationTime"] = "not-time"
        else:
            value["statistics"]["creationTime"] = "NaN"

    http.mutate = alter
    install(monkeypatch, http)
    result = execute(values)
    assert result["application_status"] == "refused"
    assert result["billed_bytes"] is None


@pytest.mark.parametrize(
    "case", ["job_type", "dry_run", "tier", "write_empty", "truncate_destination", "combined"]
)
def test_narrow_native_defaults_are_accepted_without_rewriting_observation(monkeypatch, case):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])

    def defaults(value):
        config = value["configuration"]
        query = config["query"]
        if case in ("job_type", "combined"):
            config["jobType"] = "QUERY"
        if case in ("dry_run", "combined"):
            config.pop("dryRun")
        if case in ("tier", "combined"):
            query["maximumBillingTier"] = 1
        if case in ("write_empty", "combined"):
            query["writeDisposition"] = "WRITE_EMPTY"
        if case == "truncate_destination":
            query["destinationTable"] = {
                "projectId": "ogilvy-trends-v2",
                "datasetId": "_synthetic_result",
                "tableId": "anon_result",
            }
            query["writeDisposition"] = "WRITE_TRUNCATE"

    http.mutate = defaults
    install(monkeypatch, http)
    result = execute(values)
    assert result["application_status"] == "accepted"
    observation = values[0]._objects.read(result["observation"]["object_key"]).value
    assert observation["native_configuration"] == http.resource["configuration"]


@pytest.mark.parametrize(
    "case",
    [
        "parameter_mode",
        "missing_parameters",
        "truncate_without_destination",
        "unknown_option",
        "tier_bool",
        "dry_run_zero",
        "job_type_load",
    ],
)
def test_raw_configuration_contradictions_are_not_normalized_away(monkeypatch, case):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])

    def alter(value):
        config = value["configuration"]
        query = config["query"]
        if case == "parameter_mode":
            query["parameterMode"] = "POSITIONAL"
        elif case == "missing_parameters":
            query.pop("queryParameters")
        elif case == "truncate_without_destination":
            query["writeDisposition"] = "WRITE_TRUNCATE"
        elif case == "unknown_option":
            query["systemVariables"] = {}
        elif case == "tier_bool":
            query["maximumBillingTier"] = True
        elif case == "dry_run_zero":
            config["dryRun"] = 0
        else:
            config["jobType"] = "LOAD"

    http.mutate = alter
    install(monkeypatch, http)
    result = execute(values)
    assert result["application_status"] == "refused"
    assert result["receipt"]["query_state"] == "running"


@pytest.mark.parametrize("redirect_method", ["GET", "POST"])
def test_actual_requests_adapter_does_not_follow_redirects(monkeypatch, redirect_method):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    sent = []

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **_kwargs):
            sent.append((request.method, request.url))
            if request.method == redirect_method or urlparse(request.url).netloc == "evil.invalid":
                response = requests.Response()
                response.status_code = (
                    307 if urlparse(request.url).netloc != "evil.invalid" else 404
                )
                response.headers["Location"] = "https://evil.invalid/leak"
                response.headers["content-type"] = "application/json"
                response._content = b'{"error":{"code":307,"message":"synthetic redirect"}}'
                response.request = request
                return response
            return http.request(request.method, request.url, data=request.body)

        def close(self):
            pass

    adapter = Adapter()
    monkeypatch.setattr(requests.Session, "get_adapter", lambda _self, url: adapter)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("socket attempted"))
    result = execute(values)
    assert result["application_status"] != "accepted"
    assert all(urlparse(url).netloc == "bigquery.googleapis.com" for _, url in sent)
    assert sum(method == redirect_method for method, _ in sent) == 1


def test_sdk_cannot_read_another_job_on_the_official_host(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)

    def foreign_read(job, **_kwargs):
        job._client._call_api(
            None, method="GET", path="/projects/ogilvy-trends-v2/jobs/other_job", timeout=1
        )

    monkeypatch.setattr(bigquery.QueryJob, "result", foreign_read)
    result = execute(values)
    assert result["reason"] == "query_http_path_invalid"
    assert not any("other_job" in path for _, path, *_ in http.calls)


def test_creation_time_accepts_provider_millisecond_floor(monkeypatch):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    monkeypatch.setattr(module, "monotonic", lambda: 0.0)
    result = execute(values, now=f.NOW + timedelta(seconds=2, microseconds=999))
    assert result["application_status"] == "accepted"
    observation = values[0]._objects.read(result["observation"]["object_key"]).value
    assert observation["native_creation_time"] == int(
        (f.NOW + timedelta(seconds=2)).timestamp() * 1000
    )


@pytest.mark.parametrize("mode", [None, "NAMED"])
def test_empty_parameter_omission_requires_absent_mode_in_actual_sdk_job(monkeypatch, mode):
    values = fixture()
    http = HTTP(values[3], bucket=values[1])
    install(monkeypatch, http)
    assert execute(values)["application_status"] == "accepted"
    spec = (
        values[0]
        ._objects.read(f"requests/{values[2]['request_id']}/queries/1/execution.json")
        .value
    )
    spec["requested_configuration"]["query"]["queryParameters"] = []
    spec["requested_configuration"]["query"].pop("parameterMode")
    resource = copy.deepcopy(http.resource)
    resource["configuration"] = copy.deepcopy(spec["requested_configuration"])
    resource["configuration"]["query"].pop("queryParameters")
    if mode is not None:
        resource["configuration"]["query"]["parameterMode"] = mode
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    client = bigquery.Client(project="ogilvy-trends-v2", credentials=values[4])
    job = bigquery.QueryJob.from_api_repr(resource, client)
    arguments = {
        "deadline": f.NOW + timedelta(seconds=180),
        "observed_at": f.NOW + timedelta(seconds=3),
    }
    if mode is None:
        native = module._validated_job(job, spec, **arguments)
        assert "queryParameters" not in native["configuration"]["query"]
    else:
        with pytest.raises(f.module().QuestionStoreError, match="query_job_config_invalid"):
            module._validated_job(job, spec, **arguments)
    client.close()


def source_lane(monkeypatch):
    from collections.abc import Mapping

    from tests.unit import test_general_question_source_query as material_fixture

    scoped = {**f.scope(), "client_scope_id": "live_scope"}
    monkeypatch.setattr(f, "scope", lambda: copy.deepcopy(scoped))
    values = fixture()
    context = values[0].read_request(values[2]["request_id"], scope=f.scope())
    monkeypatch.setattr(material_fixture, "request", lambda: context["request"])
    original_json = material_fixture.json_value

    def fixture_json(value):
        return dict(value) if isinstance(value, Mapping) else original_json(value)

    monkeypatch.setattr(material_fixture, "json_value", fixture_json)
    wire = material_fixture.rows()
    identity = material_fixture.selected()[0]

    class LaneHTTP(HTTP):
        def __init__(self):
            super().__init__(values[3], bucket=values[1])
            self.resources = {}
            self.discovery_rows = [{**identity, "client_scope_id": "live_scope"}]

        def request(self, method, url, **kwargs):
            path = urlparse(url).path
            if method == "GET":
                self.resource = self.resources.get(path.rsplit("/", 1)[-1])
            response = super().request(method, url, **kwargs)
            if method == "POST" and self.resource is not None:
                self.resources[self.resource["jobReference"]["jobId"]] = self.resource
            if "/queries/" in path and self.resource is not None:
                source = "@run_ids" in self.resource["configuration"]["query"]["query"]
                rows = wire if source else self.discovery_rows
                fields = list(rows[0])
                body = {
                    "jobReference": self.resource["jobReference"],
                    "jobComplete": True,
                    "totalRows": str(len(rows)),
                    "schema": {"fields": [{"name": name, "type": "STRING"} for name in fields]},
                    "rows": [{"f": [{"v": row[name]} for name in fields]} for row in rows],
                }
                response._content = json.dumps(body).encode()
            return response

    http = LaneHTTP()
    install(monkeypatch, http)
    return values, http, identity, wire


def execute_source(values, selected, content_limit):
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    return module.execute_selected_source_query(
        values[2],
        store=values[0],
        scope=f.scope(),
        runtime_identity=values[3],
        credentials=values[4],
        plan=values[5],
        selected_ids=tuple(selected),
        candidate_limit=content_limit,
        evidence_limit=min(200, content_limit),
        maximum_bytes_billed=1000000,
        now=f.NOW + timedelta(seconds=3),
    )


def test_fixed_source_lane_binds_discovery_and_excludes_metadata_from_inspection_count(monkeypatch):
    values, http, identity, wire = source_lane(monkeypatch)
    discovery = execute(values, candidate_limit=1)
    count = sum(row["kind"] not in ("request_metadata", "run_metadata") for row in wire)
    result = execute_source(values, [identity], count)
    assert result["application_status"] == "accepted"
    assert result["total_rows"] == len(wire)
    assert result["receipt"]["observed_candidate_count"] == count
    assert result["material"]["candidate_count"] == count
    assert result["missing_checks"] == result["material"]["missing_checks"]
    assert result["source_authority"] is False
    assert result["discovery_result_digest"] == discovery["receipt"]["result_digest"]
    assert execute_source(values, [identity], count)["job_id"] == result["job_id"]
    assert sum(method == "POST" for method, *_ in http.calls) == 2


@pytest.mark.parametrize(
    "case",
    [
        "no_discovery",
        "foreign_selection",
        "changed_result",
        "missing_marker",
        "duplicate_selection",
        "wrong_market",
    ],
)
def test_source_lane_never_trusts_asserted_or_changed_discovery(monkeypatch, case):
    values, http, identity, _wire = source_lane(monkeypatch)
    if case != "no_discovery":
        execute(values, candidate_limit=1)
    if case == "foreign_selection":
        identity = {**identity, "signal_id": "not_returned"}
    if case == "changed_result":
        http.discovery_rows[0]["signal_id"] = "changed_after_success"
    if case == "missing_marker":
        del values[1].objects[
            f.PREFIX + f"requests/{values[2]['request_id']}/queries/1/execution.json"
        ]
    if case == "wrong_market":
        identity = {**identity, "market": "ng"}
    chosen = [identity, identity] if case == "duplicate_selection" else [identity]
    count = sum(method == "POST" for method, *_ in http.calls)
    with pytest.raises(f.module().QuestionStoreError):
        execute_source(values, chosen, 10)
    assert sum(method == "POST" for method, *_ in http.calls) == count


@pytest.mark.parametrize("case", ["overage", "unknown", "terminal_conflict"])
def test_nonaccepted_source_result_removes_provisional_material(monkeypatch, case):
    values, http, identity, wire = source_lane(monkeypatch)
    execute(values, candidate_limit=1)
    count = sum(row["kind"] not in ("request_metadata", "run_metadata") for row in wire)

    def mutate(resource):
        statistics = resource["statistics"]["query"]
        if case == "overage":
            statistics["totalBytesBilled"] = "1000001"
        elif case == "unknown":
            statistics.pop("totalBytesBilled", None)

    http.mutate = mutate
    if case == "terminal_conflict":
        module = importlib.import_module(
            "src.analysis.open_intelligence.general_question_query_execution"
        )
        original = module.GeneralQuestionQueries.record_query_receipt

        def conflicting_receipt(self, request_id, *, scope, ordinal, receipt):
            if ordinal == 2:
                failed = {**receipt, "query_state": "failed", "result_digest": None}
                original(self, request_id, scope=scope, ordinal=ordinal, receipt=failed)
            return original(self, request_id, scope=scope, ordinal=ordinal, receipt=receipt)

        monkeypatch.setattr(
            module.GeneralQuestionQueries, "record_query_receipt", conflicting_receipt
        )
    result = execute_source(values, [identity], count)
    assert result["application_status"] == ("unavailable" if case == "unknown" else "refused")
    assert result["rows"] == []
    assert "material" not in result
    assert isinstance(result["missing_checks"], list)
    assert result["observed_candidate_count"] == count
    assert result["total_rows"] == len(wire)
    assert result["native_job_state"] == "DONE"
    assert result["billed_bytes"] == (
        None if case == "unknown" else 1000001 if case == "overage" else 100
    )
    assert result["receipt"]["query_state"] == ("running" if case == "unknown" else "failed")
    if case == "terminal_conflict":
        assert result["reason"] == "query_terminal_conflict"


@pytest.mark.parametrize("collision", [False, True])
def test_context_ancestor_batch_has_one_owned_job_and_no_candidate_slots(monkeypatch, collision):
    from tests.unit import test_general_question_context_queries as context_fixture

    values = fixture()
    rows, identifier = context_fixture.authority_rows()
    rows = [{"requested_consumption_id": identifier, **rows[0]}]
    if collision:
        rows[0]["result_count"] = 2
    http = HTTP(values[3], bucket=values[1])
    _context_wire(http, rows)
    install(monkeypatch, http)
    executor = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    output = executor.execute_context_result_batch_query(
        values[2],
        store=values[0],
        scope=f.scope(),
        runtime_identity=values[3],
        credentials=values[4],
        plan=values[5],
        ordinal=1,
        consumption_ids=(identifier,),
        maximum_bytes_billed=35000000,
        now=f.NOW + timedelta(seconds=2),
    )
    assert output["application_status"] == ("refused" if collision else "accepted")
    assert output["observed_candidate_count"] == 0
    assert output["receipt"]["template_id"] == "protected_context_results_v1"
    assert len([call for call in http.calls if call[0] == "POST"]) == 1
    if not collision:
        assert output["material"][identifier]["consumption"].consumption_id == identifier


_NATIVE_EMPTY_FETCH = {
    "jobReference": {
        "projectId": "ogilvy-trends-v2",
        "jobId": "gq_9a559e09a89a458fa2e011bc813719fc_7_c0ed752c1185be870494daaffe221be880a03cad7dcdd78437006d76eeaa31d9",
        "location": "US",
    },
    "configuration": {
        "query": {
            "query": "WITH requested AS (\n SELECT @sample_markets[OFFSET(position)] AS market,@sample_ids[OFFSET(position)] AS id,@sample_dates[OFFSET(position)] AS collected_date\n FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@sample_ids)-1)) position\n), eligible AS (\n SELECT 'raw_content' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,CAST(NULL AS STRING) AS payload,CAST(NULL AS INT64) AS match_count FROM UNNEST(ARRAY<INT64>[]) AS empty_row WHERE FALSE\n), picked AS (\n SELECT *,ROW_NUMBER() OVER(ORDER BY market,id,payload) AS ordering FROM eligible\n ORDER BY market,id,payload LIMIT @candidate_limit\n), output AS (\n SELECT '__metadata__' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,TO_JSON_STRING(STRUCT((SELECT COUNT(*) FROM picked) AS candidate_count,\n (SELECT COUNT(*) FROM eligible) AS full_matching_count,\n (SELECT COUNT(*) FROM eligible) > (SELECT COUNT(*) FROM picked) AS overflow,\n @source_snapshot_digest AS source_snapshot_digest, @profile_id AS profile_id)) AS payload,CAST(NULL AS INT64) AS match_count,0 AS ordering\n UNION ALL SELECT lane,market,id,payload,match_count,ordering FROM picked\n) SELECT lane,market,id,payload,match_count FROM output ORDER BY ordering",
            "destinationTable": {
                "projectId": "ogilvy-trends-v2",
                "datasetId": "_d95c52e9e0862a98b7cf24af13a57931c7e26e41",
                "tableId": "anone6a081d7_b246_4461_8328_eda59f2053d9",
            },
            "writeDisposition": "WRITE_TRUNCATE",
            "priority": "INTERACTIVE",
            "useQueryCache": False,
            "maximumBytesBilled": "300000000",
            "useLegacySql": False,
            "parameterMode": "NAMED",
            "queryParameters": [
                {
                    "name": "capture_cutoff",
                    "parameterType": {"type": "DATE"},
                    "parameterValue": {"value": "2026-09-07"},
                },
                {
                    "name": "markets",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                    "parameterValue": {"arrayValues": [{"value": "za"}]},
                },
                {
                    "name": "search_terms",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                    "parameterValue": {
                        "arrayValues": [
                            {"value": "budget"},
                            {"value": "cost of living"},
                            {"value": "food prices"},
                            {"value": "groceries"},
                            {"value": "grocery"},
                            {"value": "inflation"},
                            {"value": "spending"},
                            {"value": "supermarket"},
                        ]
                    },
                },
                {
                    "name": "as_of",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 21:06:00.740672+00:00"},
                },
                {
                    "name": "source_as_of",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 00:00:00+00:00"},
                },
                {
                    "name": "publication_start",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-01 00:00:00+00:00"},
                },
                {
                    "name": "publication_end",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 00:00:00+00:00"},
                },
                {
                    "name": "candidate_limit",
                    "parameterType": {"type": "INT64"},
                    "parameterValue": {"value": "189"},
                },
                {
                    "name": "source_snapshot_digest",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {
                        "value": "91f5de861871bb6224be355f9922313d477041556b9d11141b53845716ccfc22"
                    },
                },
                {
                    "name": "profile_id",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {"value": "protected_context_20260907_v1"},
                },
                {
                    "name": "source_binding_digest",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {
                        "value": "0fc5e5d9cc407d77667870125d8ad6c1bedb280c4b08f5fc9d7b5c825a11b062"
                    },
                },
                {
                    "name": "sample_dates",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "DATE"}},
                },
                {
                    "name": "selected_dates",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "DATE"}},
                },
                {
                    "name": "sample_markets",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                },
                {
                    "name": "sample_ids",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                },
            ],
        },
        "dryRun": False,
        "jobTimeoutMs": "18176",
        "jobType": "QUERY",
    },
    "statistics": {
        "creationTime": "1788901724316",
        "startTime": "1788901724383",
        "endTime": "1788901724563",
        "query": {"statementType": "SELECT", "totalBytesProcessed": "0", "totalBytesBilled": "0"},
    },
    "status": {"state": "DONE"},
    "user_email": "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
}
_NATIVE_EMPTY_SPEC = {
    "contract_version": "general_question_query_execution_v1",
    "creator_email": "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    "job_id": "gq_9a559e09a89a458fa2e011bc813719fc_7_c0ed752c1185be870494daaffe221be880a03cad7dcdd78437006d76eeaa31d9",
    "ordinal": 7,
    "owner_nonce": "3df2153e-1071-4aac-95b8-f8411efb798c",
    "plan_digest": "f47fa863487c90f201745a16da478e818b37e6fed7e60f2057acf51b1eae032b",
    "query_digest": "c0ed752c1185be870494daaffe221be880a03cad7dcdd78437006d76eeaa31d9",
    "recorded_at": "2026-09-08T21:08:42.564047Z",
    "request_id": "9a559e09-a89a-458f-a2e0-11bc813719fc",
    "requested_configuration": {
        "dryRun": False,
        "jobTimeoutMs": "18176",
        "query": {
            "maximumBytesBilled": "300000000",
            "parameterMode": "NAMED",
            "query": "WITH requested AS (\n SELECT @sample_markets[OFFSET(position)] AS market,@sample_ids[OFFSET(position)] AS id,@sample_dates[OFFSET(position)] AS collected_date\n FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@sample_ids)-1)) position\n), eligible AS (\n SELECT 'raw_content' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,CAST(NULL AS STRING) AS payload,CAST(NULL AS INT64) AS match_count FROM UNNEST(ARRAY<INT64>[]) AS empty_row WHERE FALSE\n), picked AS (\n SELECT *,ROW_NUMBER() OVER(ORDER BY market,id,payload) AS ordering FROM eligible\n ORDER BY market,id,payload LIMIT @candidate_limit\n), output AS (\n SELECT '__metadata__' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,TO_JSON_STRING(STRUCT((SELECT COUNT(*) FROM picked) AS candidate_count,\n (SELECT COUNT(*) FROM eligible) AS full_matching_count,\n (SELECT COUNT(*) FROM eligible) > (SELECT COUNT(*) FROM picked) AS overflow,\n @source_snapshot_digest AS source_snapshot_digest, @profile_id AS profile_id)) AS payload,CAST(NULL AS INT64) AS match_count,0 AS ordering\n UNION ALL SELECT lane,market,id,payload,match_count,ordering FROM picked\n) SELECT lane,market,id,payload,match_count FROM output ORDER BY ordering",
            "queryParameters": [
                {
                    "name": "capture_cutoff",
                    "parameterType": {"type": "DATE"},
                    "parameterValue": {"value": "2026-09-07"},
                },
                {
                    "name": "markets",
                    "parameterType": {"arrayType": {"type": "STRING"}, "type": "ARRAY"},
                    "parameterValue": {"arrayValues": [{"value": "za"}]},
                },
                {
                    "name": "search_terms",
                    "parameterType": {"arrayType": {"type": "STRING"}, "type": "ARRAY"},
                    "parameterValue": {
                        "arrayValues": [
                            {"value": "budget"},
                            {"value": "cost of living"},
                            {"value": "food prices"},
                            {"value": "groceries"},
                            {"value": "grocery"},
                            {"value": "inflation"},
                            {"value": "spending"},
                            {"value": "supermarket"},
                        ]
                    },
                },
                {
                    "name": "as_of",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 21:06:00.740672+00:00"},
                },
                {
                    "name": "source_as_of",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 00:00:00+00:00"},
                },
                {
                    "name": "publication_start",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-01 00:00:00+00:00"},
                },
                {
                    "name": "publication_end",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 00:00:00+00:00"},
                },
                {
                    "name": "candidate_limit",
                    "parameterType": {"type": "INT64"},
                    "parameterValue": {"value": "189"},
                },
                {
                    "name": "source_snapshot_digest",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {
                        "value": "91f5de861871bb6224be355f9922313d477041556b9d11141b53845716ccfc22"
                    },
                },
                {
                    "name": "profile_id",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {"value": "protected_context_20260907_v1"},
                },
                {
                    "name": "source_binding_digest",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {
                        "value": "0fc5e5d9cc407d77667870125d8ad6c1bedb280c4b08f5fc9d7b5c825a11b062"
                    },
                },
                {
                    "name": "sample_dates",
                    "parameterType": {"arrayType": {"type": "DATE"}, "type": "ARRAY"},
                    "parameterValue": {"arrayValues": []},
                },
                {
                    "name": "selected_dates",
                    "parameterType": {"arrayType": {"type": "DATE"}, "type": "ARRAY"},
                    "parameterValue": {"arrayValues": []},
                },
                {
                    "name": "sample_markets",
                    "parameterType": {"arrayType": {"type": "STRING"}, "type": "ARRAY"},
                    "parameterValue": {"arrayValues": []},
                },
                {
                    "name": "sample_ids",
                    "parameterType": {"arrayType": {"type": "STRING"}, "type": "ARRAY"},
                    "parameterValue": {"arrayValues": []},
                },
            ],
            "useLegacySql": False,
            "useQueryCache": False,
        },
    },
}


@pytest.mark.parametrize(
    "mutation", [None, "nonempty", "scalar", "null", "extra", "type", "used_array"]
)
def test_native_empty_array_echo_adoption_and_replay_are_narrow(mutation):
    from datetime import UTC, datetime

    from google.auth.credentials import AnonymousCredentials

    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    native = copy.deepcopy(_NATIVE_EMPTY_FETCH)
    spec = copy.deepcopy(_NATIVE_EMPTY_SPEC)
    expected = next(
        p
        for p in spec["requested_configuration"]["query"]["queryParameters"]
        if p["name"] == "sample_ids"
    )
    echoed = next(
        p for p in native["configuration"]["query"]["queryParameters"] if p["name"] == "sample_ids"
    )
    if mutation == "nonempty":
        expected["parameterValue"]["arrayValues"] = [{"value": "real"}]
    elif mutation == "scalar":
        expected["parameterType"] = echoed["parameterType"] = {"type": "STRING"}
    elif mutation == "null":
        echoed["parameterValue"] = {"value": None}
    elif mutation == "extra":
        echoed["unexpected"] = True
    elif mutation == "type":
        echoed["parameterType"] = {"type": "ARRAY", "arrayType": {"type": "INT64"}}
    elif mutation == "used_array":
        spec["requested_configuration"]["query"]["query"] = native["configuration"]["query"][
            "query"
        ] = "SELECT @sample_ids"
    client = bigquery.Client(project="ogilvy-trends-v2", credentials=AnonymousCredentials())
    arguments = {
        "deadline": datetime.fromtimestamp(
            (int(native["statistics"]["endTime"]) + 60000) / 1000, UTC
        ),
        "observed_at": datetime.fromtimestamp(
            (int(native["statistics"]["endTime"]) + 1000) / 1000, UTC
        ),
    }
    job = bigquery.QueryJob.from_api_repr(copy.deepcopy(native), client)
    if mutation:
        with pytest.raises(f.module().QuestionStoreError, match="query_job_config_invalid"):
            module._validated_job(job, spec, **arguments)
    else:
        retained = module._validated_job(job, spec, **arguments)
        assert "parameterValue" not in next(
            p
            for p in retained["configuration"]["query"]["queryParameters"]
            if p["name"] == "sample_ids"
        )
        replay = bigquery.QueryJob.from_api_repr(copy.deepcopy(retained), client)
        assert module._validated_job(replay, spec, **arguments) == retained
        assert spec == _NATIVE_EMPTY_SPEC
    client.close()


@pytest.mark.parametrize("name", ["eligible_partition_dates", "search_terms"])
@pytest.mark.parametrize(
    "mutation", [None, "nonempty", "null", "wrong_type", "extra", "used_array"]
)
def test_builder_empty_optional_array_omission_remains_guarded(name, mutation):
    from datetime import UTC, datetime

    from google.auth.credentials import AnonymousCredentials

    from tests.unit import test_general_question_context_queries as wires

    args = wires.inputs(terms=[] if name == "search_terms" else ["commute"])
    kwargs = {
        "candidate_rows": [wires.metadata(args[3], 0, 0)],
        "lane": "enriched_content",
        "candidate_limit": 200,
    }
    if name == "eligible_partition_dates":
        kwargs["partition_rows"] = [
            {"lane": "__metadata__", "partition_date": None, "partition_count": 0}
        ]
    query = wires.subject().build_context_index_query(*args, **kwargs)
    spec = copy.deepcopy(_NATIVE_EMPTY_SPEC)
    config = {
        "query": {
            "query": query.sql,
            "useLegacySql": False,
            "queryParameters": [p.to_api_repr() for p in query.parameters],
            "parameterMode": "NAMED",
        }
    }
    spec["requested_configuration"] = copy.deepcopy(config)
    native = copy.deepcopy(_NATIVE_EMPTY_FETCH)
    native["configuration"] = copy.deepcopy(config)
    expected = next(
        p for p in spec["requested_configuration"]["query"]["queryParameters"] if p["name"] == name
    )
    echoed = next(
        p for p in native["configuration"]["query"]["queryParameters"] if p["name"] == name
    )
    assert expected["parameterValue"] == {"arrayValues": []}
    del echoed["parameterValue"]
    if mutation == "nonempty":
        expected["parameterValue"] = {
            "arrayValues": [
                {"value": "2026-09-07" if name == "eligible_partition_dates" else "commute"}
            ]
        }
    elif mutation == "null":
        echoed["parameterValue"] = {"value": None}
    elif mutation == "wrong_type":
        echoed["parameterType"] = {"type": "STRING"}
    elif mutation == "extra":
        echoed["extra"] = True
    elif mutation == "used_array":
        spec["requested_configuration"]["query"]["query"] = native["configuration"]["query"][
            "query"
        ] = "SELECT @" + name
    client = bigquery.Client(project="ogilvy-trends-v2", credentials=AnonymousCredentials())
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    job = bigquery.QueryJob.from_api_repr(copy.deepcopy(native), client)
    times = {
        "deadline": datetime.fromtimestamp(
            (int(native["statistics"]["endTime"]) + 60000) / 1000, UTC
        ),
        "observed_at": datetime.fromtimestamp(
            (int(native["statistics"]["endTime"]) + 1000) / 1000, UTC
        ),
    }
    if mutation:
        with pytest.raises(f.module().QuestionStoreError, match="query_job_config_invalid"):
            module._validated_job(job, spec, **times)
    else:
        retained = module._validated_job(job, spec, **times)
        assert "parameterValue" not in next(
            p for p in retained["configuration"]["query"]["queryParameters"] if p["name"] == name
        )
        assert (
            module._validated_job(
                bigquery.QueryJob.from_api_repr(copy.deepcopy(retained), client), spec, **times
            )
            == retained
        )
    client.close()


_DIRECT_INDEX_NATIVE = {
    "jobReference": {
        "projectId": "ogilvy-trends-v2",
        "jobId": "gq_2327c0e336964ce698b33e0b26b68c38_5_6e462ade3e369c32f277556201b96157cee27d3dd35bbcf03300cf0efc2a5ebf",
        "location": "US",
    },
    "configuration": {
        "query": {
            "query": "WITH requested AS (\n SELECT @sample_markets[OFFSET(position)] AS market,@sample_ids[OFFSET(position)] AS id\n FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@sample_ids)-1)) position\n), eligible AS (\n SELECT DISTINCT * FROM (SELECT 'enriched_content' AS lane,evidence.market,evidence.id,TO_JSON_STRING(STRUCT(evidence.market, evidence.id, CAST(DATE(evidence.collected_at) AS STRING) AS collected_date)) AS payload,1 AS match_count\n FROM `ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_enriched_content` evidence WHERE evidence.market IN UNNEST(@markets)\n AND evidence.collected_at<=@as_of AND evidence.collected_at<=@source_as_of\n AND (evidence.published_at IS NULL OR (evidence.published_at>=@publication_start AND evidence.published_at<@publication_end AND evidence.published_at<=@as_of AND evidence.published_at<=@source_as_of))\n AND COALESCE(evidence.published_at,evidence.collected_at)>=@publication_start\n AND COALESCE(evidence.published_at,evidence.collected_at)<@publication_end\n\n AND DATE(evidence.collected_at) IN UNNEST(@eligible_partition_dates)\n AND EXISTS(SELECT 1 FROM UNNEST(@search_terms) term WHERE STRPOS(LOWER(CONCAT(COALESCE(evidence.text,''),' ',COALESCE(evidence.title,''))),term)>0)\n AND @source_binding_digest IS NOT NULL )\n), picked AS (\n SELECT *,ROW_NUMBER() OVER(ORDER BY market,id,payload) AS ordering FROM eligible\n ORDER BY market,id,payload LIMIT @candidate_limit\n), output AS (\n SELECT '__metadata__' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,TO_JSON_STRING(STRUCT((SELECT COUNT(*) FROM picked) AS candidate_count,\n (SELECT COUNT(*) FROM eligible) AS full_matching_count,\n (SELECT COUNT(*) FROM eligible) > (SELECT COUNT(*) FROM picked) AS overflow,\n @source_snapshot_digest AS source_snapshot_digest, @profile_id AS profile_id)) AS payload,CAST(NULL AS INT64) AS match_count,0 AS ordering\n UNION ALL SELECT lane,market,id,payload,match_count,ordering FROM picked\n) SELECT lane,market,id,payload,match_count FROM output ORDER BY ordering",
            "destinationTable": {
                "projectId": "ogilvy-trends-v2",
                "datasetId": "_d95c52e9e0862a98b7cf24af13a57931c7e26e41",
                "tableId": "anon6bcd1b79_87f9_4e93_a785_b7ba0dd66fbb",
            },
            "writeDisposition": "WRITE_TRUNCATE",
            "priority": "INTERACTIVE",
            "useQueryCache": False,
            "maximumBytesBilled": "100000000",
            "useLegacySql": False,
            "parameterMode": "NAMED",
            "queryParameters": [
                {
                    "name": "capture_cutoff",
                    "parameterType": {"type": "DATE"},
                    "parameterValue": {"value": "2026-09-07"},
                },
                {
                    "name": "markets",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                    "parameterValue": {"arrayValues": [{"value": "za"}]},
                },
                {
                    "name": "search_terms",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                    "parameterValue": {
                        "arrayValues": [
                            {"value": "cost of living"},
                            {"value": "everyday spending"},
                            {"value": "food prices"},
                            {"value": "grocery retailer"},
                            {"value": "grocery shopping"},
                            {"value": "south africa"},
                            {"value": "supermarket"},
                        ]
                    },
                },
                {
                    "name": "as_of",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-09 01:13:39.699646+00:00"},
                },
                {
                    "name": "source_as_of",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 00:00:00+00:00"},
                },
                {
                    "name": "publication_start",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-01 00:00:00+00:00"},
                },
                {
                    "name": "publication_end",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 00:00:00+00:00"},
                },
                {
                    "name": "candidate_limit",
                    "parameterType": {"type": "INT64"},
                    "parameterValue": {"value": "200"},
                },
                {
                    "name": "source_snapshot_digest",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {
                        "value": "91f5de861871bb6224be355f9922313d477041556b9d11141b53845716ccfc22"
                    },
                },
                {
                    "name": "profile_id",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {"value": "protected_context_20260907_v1"},
                },
                {
                    "name": "source_binding_digest",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {
                        "value": "0fc5e5d9cc407d77667870125d8ad6c1bedb280c4b08f5fc9d7b5c825a11b062"
                    },
                },
                {
                    "name": "eligible_partition_dates",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "DATE"}},
                    "parameterValue": {
                        "arrayValues": [
                            {"value": "2026-09-01"},
                            {"value": "2026-09-02"},
                            {"value": "2026-09-03"},
                            {"value": "2026-09-04"},
                            {"value": "2026-09-05"},
                            {"value": "2026-09-06"},
                            {"value": "2026-09-07"},
                        ]
                    },
                },
                {
                    "name": "direct_content_fallback",
                    "parameterType": {"type": "BOOL"},
                    "parameterValue": {"value": "true"},
                },
                {
                    "name": "sample_markets",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                },
                {
                    "name": "sample_ids",
                    "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                },
            ],
        },
        "dryRun": False,
        "jobTimeoutMs": "70534",
        "jobType": "QUERY",
    },
    "statistics": {
        "creationTime": "1788916530839",
        "startTime": "1788916531196",
        "endTime": "1788916532615",
        "query": {"statementType": "SELECT"},
    },
    "status": {"state": "DONE"},
    "user_email": "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
}
_DIRECT_INDEX_SPEC = {
    "contract_version": "general_question_query_execution_v1",
    "creator_email": "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    "job_id": "gq_2327c0e336964ce698b33e0b26b68c38_5_6e462ade3e369c32f277556201b96157cee27d3dd35bbcf03300cf0efc2a5ebf",
    "ordinal": 5,
    "owner_nonce": "14229d47-2d0b-43eb-8295-38010307486a",
    "plan_digest": "09d4242c52fd351094d14bf463c87ce85a310ee6b7e563304f839ed37e3f20f4",
    "query_digest": "6e462ade3e369c32f277556201b96157cee27d3dd35bbcf03300cf0efc2a5ebf",
    "recorded_at": "2026-09-09T01:15:29.165430Z",
    "request_id": "2327c0e3-3696-4ce6-98b3-3e0b26b68c38",
    "requested_configuration": {
        "dryRun": False,
        "jobTimeoutMs": "70534",
        "query": {
            "maximumBytesBilled": "100000000",
            "parameterMode": "NAMED",
            "query": "WITH requested AS (\n SELECT @sample_markets[OFFSET(position)] AS market,@sample_ids[OFFSET(position)] AS id\n FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@sample_ids)-1)) position\n), eligible AS (\n SELECT DISTINCT * FROM (SELECT 'enriched_content' AS lane,evidence.market,evidence.id,TO_JSON_STRING(STRUCT(evidence.market, evidence.id, CAST(DATE(evidence.collected_at) AS STRING) AS collected_date)) AS payload,1 AS match_count\n FROM `ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_enriched_content` evidence WHERE evidence.market IN UNNEST(@markets)\n AND evidence.collected_at<=@as_of AND evidence.collected_at<=@source_as_of\n AND (evidence.published_at IS NULL OR (evidence.published_at>=@publication_start AND evidence.published_at<@publication_end AND evidence.published_at<=@as_of AND evidence.published_at<=@source_as_of))\n AND COALESCE(evidence.published_at,evidence.collected_at)>=@publication_start\n AND COALESCE(evidence.published_at,evidence.collected_at)<@publication_end\n\n AND DATE(evidence.collected_at) IN UNNEST(@eligible_partition_dates)\n AND EXISTS(SELECT 1 FROM UNNEST(@search_terms) term WHERE STRPOS(LOWER(CONCAT(COALESCE(evidence.text,''),' ',COALESCE(evidence.title,''))),term)>0)\n AND @source_binding_digest IS NOT NULL )\n), picked AS (\n SELECT *,ROW_NUMBER() OVER(ORDER BY market,id,payload) AS ordering FROM eligible\n ORDER BY market,id,payload LIMIT @candidate_limit\n), output AS (\n SELECT '__metadata__' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,TO_JSON_STRING(STRUCT((SELECT COUNT(*) FROM picked) AS candidate_count,\n (SELECT COUNT(*) FROM eligible) AS full_matching_count,\n (SELECT COUNT(*) FROM eligible) > (SELECT COUNT(*) FROM picked) AS overflow,\n @source_snapshot_digest AS source_snapshot_digest, @profile_id AS profile_id)) AS payload,CAST(NULL AS INT64) AS match_count,0 AS ordering\n UNION ALL SELECT lane,market,id,payload,match_count,ordering FROM picked\n) SELECT lane,market,id,payload,match_count FROM output ORDER BY ordering",
            "queryParameters": [
                {
                    "name": "capture_cutoff",
                    "parameterType": {"type": "DATE"},
                    "parameterValue": {"value": "2026-09-07"},
                },
                {
                    "name": "markets",
                    "parameterType": {"arrayType": {"type": "STRING"}, "type": "ARRAY"},
                    "parameterValue": {"arrayValues": [{"value": "za"}]},
                },
                {
                    "name": "search_terms",
                    "parameterType": {"arrayType": {"type": "STRING"}, "type": "ARRAY"},
                    "parameterValue": {
                        "arrayValues": [
                            {"value": "cost of living"},
                            {"value": "everyday spending"},
                            {"value": "food prices"},
                            {"value": "grocery retailer"},
                            {"value": "grocery shopping"},
                            {"value": "south africa"},
                            {"value": "supermarket"},
                        ]
                    },
                },
                {
                    "name": "as_of",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-09 01:13:39.699646+00:00"},
                },
                {
                    "name": "source_as_of",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 00:00:00+00:00"},
                },
                {
                    "name": "publication_start",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-01 00:00:00+00:00"},
                },
                {
                    "name": "publication_end",
                    "parameterType": {"type": "TIMESTAMP"},
                    "parameterValue": {"value": "2026-09-08 00:00:00+00:00"},
                },
                {
                    "name": "candidate_limit",
                    "parameterType": {"type": "INT64"},
                    "parameterValue": {"value": "200"},
                },
                {
                    "name": "source_snapshot_digest",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {
                        "value": "91f5de861871bb6224be355f9922313d477041556b9d11141b53845716ccfc22"
                    },
                },
                {
                    "name": "profile_id",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {"value": "protected_context_20260907_v1"},
                },
                {
                    "name": "source_binding_digest",
                    "parameterType": {"type": "STRING"},
                    "parameterValue": {
                        "value": "0fc5e5d9cc407d77667870125d8ad6c1bedb280c4b08f5fc9d7b5c825a11b062"
                    },
                },
                {
                    "name": "eligible_partition_dates",
                    "parameterType": {"arrayType": {"type": "DATE"}, "type": "ARRAY"},
                    "parameterValue": {
                        "arrayValues": [
                            {"value": "2026-09-01"},
                            {"value": "2026-09-02"},
                            {"value": "2026-09-03"},
                            {"value": "2026-09-04"},
                            {"value": "2026-09-05"},
                            {"value": "2026-09-06"},
                            {"value": "2026-09-07"},
                        ]
                    },
                },
                {
                    "name": "direct_content_fallback",
                    "parameterType": {"type": "BOOL"},
                    "parameterValue": {"value": "true"},
                },
                {
                    "name": "sample_markets",
                    "parameterType": {"arrayType": {"type": "STRING"}, "type": "ARRAY"},
                    "parameterValue": {"arrayValues": []},
                },
                {
                    "name": "sample_ids",
                    "parameterType": {"arrayType": {"type": "STRING"}, "type": "ARRAY"},
                    "parameterValue": {"arrayValues": []},
                },
            ],
            "useLegacySql": False,
            "useQueryCache": False,
        },
    },
}


@pytest.mark.parametrize("mutation", [None, "nonempty", "null", "type", "extra", "used_array"])
def test_direct_index_native_omitted_unused_arrays_are_narrow(mutation):
    from datetime import UTC, datetime

    from google.auth.credentials import AnonymousCredentials

    native = copy.deepcopy(_DIRECT_INDEX_NATIVE)
    spec = copy.deepcopy(_DIRECT_INDEX_SPEC)
    expected = next(
        p
        for p in spec["requested_configuration"]["query"]["queryParameters"]
        if p["name"] == "sample_ids"
    )
    actual = next(
        p for p in native["configuration"]["query"]["queryParameters"] if p["name"] == "sample_ids"
    )
    if mutation == "nonempty":
        expected["parameterValue"] = {"arrayValues": [{"value": "real"}]}
    elif mutation == "null":
        actual["parameterValue"] = {"value": None}
    elif mutation == "type":
        actual["parameterType"] = {"type": "STRING"}
    elif mutation == "extra":
        actual["extra"] = True
    elif mutation == "used_array":
        for config in (spec["requested_configuration"], native["configuration"]):
            config["query"]["query"] += " LIMIT ARRAY_LENGTH(@sample_ids)"
    client = bigquery.Client(project="ogilvy-trends-v2", credentials=AnonymousCredentials())
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    job = bigquery.QueryJob.from_api_repr(copy.deepcopy(native), client)
    times = {
        "deadline": datetime.fromtimestamp(
            (int(native["statistics"]["endTime"]) + 60000) / 1000, UTC
        ),
        "observed_at": datetime.fromtimestamp(
            (int(native["statistics"]["endTime"]) + 1000) / 1000, UTC
        ),
    }
    if mutation:
        with pytest.raises(f.module().QuestionStoreError, match="query_job_config_invalid"):
            module._validated_job(job, spec, **times)
    else:
        retained = module._validated_job(job, spec, **times)
        assert (
            module._validated_job(
                bigquery.QueryJob.from_api_repr(copy.deepcopy(retained), client), spec, **times
            )
            == retained
        )
        assert spec == _DIRECT_INDEX_SPEC
    client.close()


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "name",
        "type",
        "nonempty",
        "null",
        "config",
        "budget",
        "sql",
        "legacy",
        "positive_limit",
        "used_array",
    ],
)
def test_v4_zero_tail_native_empty_array_echo_is_narrow(mutation):
    from datetime import UTC

    from google.auth.credentials import AnonymousCredentials

    from tests.unit import test_general_question_context_queries as wires

    args = wires.inputs()
    enriched = [wires.metadata(args[3], 2, 2, evidence=True)] + [
        {
            "lane": "enriched_content",
            "market": "za",
            "id": key,
            "match_count": 1,
            "payload": json.dumps({"market": "za", "id": key}),
        }
        for key in ("one", "two")
    ]
    query = wires.subject().build_context_index_query(
        *args,
        candidate_rows=wires.candidate_rows(args[3]),
        lane="raw_content",
        candidate_limit=0,
        enriched_rows=enriched,
        geo_policy=True,
        continuity_policy=True,
        partition_rows=[
            {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
            {"lane": "raw_content", "partition_date": "2026-09-07", "partition_count": 1},
        ],
    )
    assert query.template_id == "protected_context_raw_index_v4"
    config = {
        "query": {
            "query": query.sql,
            "useLegacySql": False,
            "queryParameters": [p.to_api_repr() for p in query.parameters],
            "parameterMode": "NAMED",
            "maximumBytesBilled": "300000000",
        }
    }
    spec = copy.deepcopy(_NATIVE_EMPTY_SPEC)
    spec["requested_configuration"] = copy.deepcopy(config)
    native = copy.deepcopy(_NATIVE_EMPTY_FETCH)
    native["configuration"] = copy.deepcopy(config)
    for parameter in native["configuration"]["query"]["queryParameters"]:
        if parameter["name"] in ("sample_markets", "sample_ids"):
            assert parameter.pop("parameterValue") == {"arrayValues": []}
    expected = next(
        p
        for p in spec["requested_configuration"]["query"]["queryParameters"]
        if p["name"] == "sample_ids"
    )
    actual = next(
        p for p in native["configuration"]["query"]["queryParameters"] if p["name"] == "sample_ids"
    )
    if mutation == "name":
        actual["name"] = "other_ids"
    elif mutation == "type":
        actual["parameterType"]["arrayType"]["type"] = "INT64"
    elif mutation == "nonempty":
        expected["parameterValue"] = {"arrayValues": [{"value": "one"}]}
    elif mutation == "null":
        actual["parameterValue"] = {"arrayValues": None}
    elif mutation == "config":
        native["configuration"]["query"]["useQueryCache"] = True
    elif mutation == "budget":
        native["configuration"]["query"]["maximumBytesBilled"] = "180000001"
    elif mutation == "sql":
        native["configuration"]["query"]["query"] += " LIMIT 1"
    elif mutation in ("legacy", "positive_limit", "used_array"):
        for value in (spec["requested_configuration"], native["configuration"]):
            if mutation == "legacy":
                value["query"]["query"] = value["query"]["query"].replace(
                    " AND @parent_context_digest IS NOT NULL", ""
                )
            elif mutation == "used_array":
                value["query"]["query"] += " LIMIT ARRAY_LENGTH(@sample_ids)"
            else:
                next(
                    p for p in value["query"]["queryParameters"] if p["name"] == "candidate_limit"
                )["parameterValue"] = {"value": "1"}
    original = copy.deepcopy(native)
    module = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    client = bigquery.Client(project="ogilvy-trends-v2", credentials=AnonymousCredentials())
    try:
        job = bigquery.QueryJob.from_api_repr(copy.deepcopy(native), client)
        times = {
            "deadline": datetime.fromtimestamp(
                (int(native["statistics"]["endTime"]) + 60000) / 1000, UTC
            ),
            "observed_at": datetime.fromtimestamp(
                (int(native["statistics"]["endTime"]) + 1000) / 1000, UTC
            ),
        }
        if mutation:
            with pytest.raises(f.module().QuestionStoreError, match="query_job_config_invalid"):
                module._validated_job(job, spec, **times)
        else:
            retained = module._validated_job(job, spec, **times)
            assert native == original
            assert retained["configuration"] == original["configuration"]
            assert (
                module._validated_job(
                    bigquery.QueryJob.from_api_repr(copy.deepcopy(retained), client), spec, **times
                )
                == retained
            )
    finally:
        client.close()


@pytest.mark.asyncio
async def test_challenge_planned_request_reaches_its_first_owned_query(monkeypatch):
    """A plan produced by the live challenge planner must revalidate under the same adapter.

    The runtime plans a source window request under discovery_challenge_v6, so every
    requirement carries evidence_purpose and the stored plan is general_question_plan_v4. The query
    layer re-reads the raw planning response before any owned job; validating that draft
    through the older key set refuses the plan and no retrieval query is ever issued.
    """
    from tests.unit import test_general_question_context_queries as context_fixture

    store, bucket, invocation, identity, credentials = runtime.setup_runtime(
        monkeypatch, source_window=True
    )
    runtime.intercept(monkeypatch, runtime.challenge_draft())
    plan = await runtime.runtime_module().execute_question_planning(
        invocation,
        store=store,
        scope=f.scope(),
        runtime_identity=identity,
        credentials=credentials,
        persist_usage=lambda event: event,
        now=f.NOW,
    )
    assert plan["contract_version"] == "general_question_plan_v4"
    assert all("evidence_purpose" in item for item in plan["requirements"])
    rows, consumption_id = context_fixture.authority_rows()
    http = HTTP(identity, bucket=bucket)
    _context_wire(http, rows)
    install(monkeypatch, http)
    executor = importlib.import_module(
        "src.analysis.open_intelligence.general_question_query_execution"
    )
    output = executor.execute_context_result_query(
        invocation,
        store=store,
        scope=f.scope(),
        runtime_identity=identity,
        credentials=credentials,
        plan=plan,
        ordinal=1,
        consumption_id=consumption_id,
        maximum_bytes_billed=1000000,
        now=f.NOW + timedelta(seconds=2),
    )
    assert output["application_status"] == "accepted"
    assert output["receipt"]["template_id"] == "protected_context_result_v1"
    assert len([call for call in http.calls if call[0] == "POST"]) == 1
