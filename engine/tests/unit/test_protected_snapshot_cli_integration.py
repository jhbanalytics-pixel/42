import copy
import hashlib
import io
import json
from datetime import timedelta
from urllib.parse import unquote, urlparse

import pytest
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit import test_protected_snapshot_integration as joined_fixture
from tests.unit.test_open_intelligence_execution_approval_runtime import (
    SOURCE_SNAPSHOT_NOW,
    source_snapshot_runtime_fixture,
)


class CLIJoinedHTTP(joined_fixture.JoinedBigQueryHTTP):
    def __init__(self, plan, physical_rows, source_metadata):
        super().__init__(plan, physical_rows)
        self.source_metadata = source_metadata
        self.source_reads = []

    def request(self, method, url, **kwargs):
        path = unquote(urlparse(url).path)
        if "/datasets/trends_v2_dev/tables/" in path:
            table_id = path.rsplit("/", 1)[1]
            lane = table_id.split("@", 1)[0]
            self.source_reads.append(table_id)
            resource = copy.deepcopy(self.source_metadata["tables"]["trends_v2_dev." + lane])
            resource.update(
                tableReference={
                    "projectId": "ogilvy-trends-v2",
                    "datasetId": "trends_v2_dev",
                    "tableId": table_id,
                },
                type="TABLE",
                location="US",
                numBytes="1024",
            )
            return joined_fixture.storage_fixture.HTTP.response(
                method, url, 200, canonical_bytes(resource)
            )
        response = super().request(method, url, **kwargs)
        if "/tables/" in path and response.status_code == 200:
            resource = response.json()
            resource["numBytes"] = "1024"
            response._content = canonical_bytes(resource)
        return response


def test_actual_cli_refreshes_operation_clock_after_consumption(monkeypatch):
    """Fresh CLI path under successor 1: _main_impl refuses snapshot_fresh_route_unavailable
    after the argument and environment checks and before the clock, any reader, the
    preflights, the consume write, the runner and the result writer. Positive proof
    through the validated body with the retained view: the source preflight and the
    operation run against the same joined fixture on a clock strictly after the retained
    consumption, the v1 result row built from the payload carries the exact payload, and
    an operation clock before the consumption refuses before any creation post."""
    original_http, storage_http, args, view = joined_fixture.joined(monkeypatch)
    module = joined_fixture.operation_fixture.subject()
    for name in module._FORBIDDEN_RUNTIME_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name in (
        "GCP_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_API_USE_CLIENT_CERTIFICATE",
        "GOOGLE_API_USE_MTLS_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    native_http = CLIJoinedHTTP(
        original_http.plan,
        original_http.capture.rows,
        json.loads(args["artifacts"]["source_metadata"]),
    )
    args["client"]._http_internal = native_http
    # Client's injected HTTP transport is its external I/O boundary.
    assert args["client"]._http is native_http
    _, approval, execution, job, build, _contents = source_snapshot_runtime_fixture(
        artifacts=args["artifacts"]
    )
    for name, raw in args["artifacts"].items():
        storage_http.seed(f"inputs/{hashlib.sha256(raw).hexdigest()}/{name}.json", raw)
    consumed_at = SOURCE_SNAPSHOT_NOW + timedelta(seconds=1)
    operation_at = SOURCE_SNAPSHOT_NOW + timedelta(seconds=2)
    completed_at = SOURCE_SNAPSHOT_NOW + timedelta(seconds=4)
    clock_calls = []
    writes = []
    persisted = []

    def clock():
        value = SOURCE_SNAPSHOT_NOW if not clock_calls else operation_at
        clock_calls.append(value)
        return value

    def consume(request):
        writes.append("consume")
        return {
            **request,
            "consumption_contract_version": "open_intelligence_execution_consumption_v1",
            "consumption_id": execution_approval.consumption_id(
                approval.approval_id, execution["name"], consumed_at
            ),
            "consumed_at": consumed_at,
        }

    def record(request):
        writes.append("result")
        value = {
            **request,
            "result_contract_version": "open_intelligence_execution_result_v1",
            "result_id": execution_approval.result_id(
                request["consumption_id"],
                request["result_reference"],
                request["result_digest"],
                request["status"],
                completed_at,
            ),
            "completed_at": completed_at,
        }
        persisted.append(value)
        return value

    stdout = io.StringIO()
    with pytest.raises(ValueError, match=r"^snapshot_fresh_route_unavailable$"):
        module._main_impl(
            ("--cutoff-date", "2026-09-07", "--mode", "initial"),
            now=clock,
            execution_reader=lambda: {"execution": execution, "job": job},
            approval_reader=lambda digest: approval,
            build_reader=lambda resource: build,
            runtime_clients=lambda: (args["client"], args["objects"]),
            operation_runner=module._run_operation,
            source_preflight=module._source_preflight,
            consumption_writer=consume,
            result_writer=record,
            result_reader=lambda consumption_id: tuple(persisted),
            stdout=stdout,
            diagnostics=lambda event: None,
        )
    assert stdout.getvalue() == ""
    assert writes == []
    assert clock_calls == []
    assert persisted == []
    assert native_http.source_reads == []
    assert native_http.creation.calls == []
    assert native_http.capture.calls == []
    assert storage_http.calls == []
    assert view.consumption.consumed_at == consumed_at
    # An operation clock before the retained consumption refuses inside the creation body
    # (snapshot_creation_context_invalid, surfaced by the runner as operation incomplete)
    # before any post and before the view is claimed, so the same fixture runs on afterwards.
    payload, status = module._execute_validated_operation(
        **{**args, "now": consumed_at - timedelta(seconds=1)}
    )
    assert status == "failed"
    assert payload["missing_checks"] == ["snapshot_operation_incomplete"]
    assert native_http.creation.calls == []
    assert native_http.capture.calls == []
    assert storage_http.calls == []
    inputs = module._validate_inputs(args["artifacts"], args["cutoff"], "initial", operation_at)
    module._source_preflight(inputs, args["client"])
    assert len(native_http.source_reads) == 5
    payload, status = module._execute_validated_operation(**{**args, "now": operation_at})
    assert status == "succeeded", payload["missing_checks"]
    assert consumed_at < operation_at
    assert len([call for call in native_http.creation.calls if call[0] == "POST"]) == 5
    assert len([call for call in native_http.capture.calls if call[0] == "POST"]) == 4
    assert payload["stored_artifact"]["generation"] > 0
    result = view.result_record(
        payload,
        status=status,
        completed_at=completed_at,
        reference=view.consumption.execution_name + "#source-snapshot",
    )
    assert json.loads(result.canonical_result_json) == payload
    assert result.status == "succeeded"
    assert args["claim"].calls.count("claim") == 1
