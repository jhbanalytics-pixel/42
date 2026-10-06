"""Pinned SDK metadata reads through a socket-blocked requests adapter."""

import copy
import importlib
import json
import socket
from datetime import timedelta
from urllib.parse import urlparse

import pytest
import requests
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery

from tests.unit import test_general_question_runtime as runtime
from tests.unit import test_general_question_schema as schema
from tests.unit import test_general_question_store as f


def setup(monkeypatch, case="success"):
    values = runtime.setup_runtime()
    tables = schema.resources()
    for table in tables:
        table.update(etag="synthetic-etag", lastModifiedTime="123")
    calls = []

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            calls.append((request, kwargs))
            assert urlparse(request.url).netloc == "bigquery.googleapis.com"
            response = requests.Response()
            response.request = request
            response.url = request.url
            response.status_code = 200
            table = copy.deepcopy(
                next(
                    t
                    for t in tables
                    if t["tableReference"]["tableId"]
                    == urlparse(request.url).path.rsplit("/", 1)[-1]
                )
            )
            if case == "redirect":
                response.status_code = 307
                response.headers["Location"] = "https://evil.invalid/schema"
            elif case == "null_mode":
                table["schema"]["fields"][0]["mode"] = None
            elif case == "identity":
                table["tableReference"]["datasetId"] = "trends_v2"
            elif case == "oversized":
                table["description"] = "x" * 1_000_001
            response._content = json.dumps(table).encode()
            return response

        def close(self):
            pass

    original = AuthorizedSession.__init__

    def session_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.mount("https://", Adapter())

    monkeypatch.setattr(AuthorizedSession, "__init__", session_init)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    return values, calls


def execute(values, **overrides):
    store, _bucket, invocation, identity, credentials = values
    module = importlib.import_module("src.analysis.open_intelligence.general_question_schema_read")
    return module.read_question_source_schemas(
        invocation,
        store=store,
        scope=f.scope(),
        runtime_identity=identity,
        credentials=credentials,
        cluster_build_version="hybrid_graph_v3",
        now=overrides.get("now", f.NOW + timedelta(seconds=2)),
    )


def test_fixed_sdk_reads_preserve_native_metadata_and_grant_no_authority(monkeypatch):
    values, calls = setup(monkeypatch)
    result = execute(values)
    assert result["source_authority"] is False
    assert result["source_receipt_version_bound"] is False
    assert len(calls) == 8
    assert all(request.method == "GET" for request, _ in calls)
    assert all(0 < kwargs["timeout"] <= 5 for _, kwargs in calls)
    assert all(
        t["etag"] == "synthetic-etag" and t["lastModifiedTime"] == "123"
        for t in result["table_resources"]
    )


@pytest.mark.parametrize("case", ["redirect", "null_mode", "identity", "oversized"])
def test_native_response_refusals(monkeypatch, case):
    values, calls = setup(monkeypatch, case)
    with pytest.raises((ValueError, f.module().QuestionStoreError)):
        execute(values)
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["expired", "credentials", "invocation", "emulator"])
def test_admission_refuses_before_http(monkeypatch, case):
    values, calls = setup(monkeypatch)
    kwargs = {}
    if case == "expired":
        kwargs["now"] = f.NOW + timedelta(seconds=241)
    elif case == "credentials":
        values[-1].service_account_email = "foreign"
    elif case == "invocation":
        values[2]["request_digest"] = "f" * 64
    else:
        monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "http://evil.invalid")
    with pytest.raises(f.module().QuestionStoreError):
        execute(values, **kwargs)
    assert calls == []


@pytest.mark.parametrize("case", ["method", "path", "host"])
def test_guard_rejects_sdk_boundary_tampering_before_send(monkeypatch, case):
    values, calls = setup(monkeypatch)

    def tampered(self, table, *, retry, timeout):
        assert retry is None
        url = "https://bigquery.googleapis.com/bigquery/v2/projects/ogilvy-trends-v2/datasets/trends_v2_staging/tables/signal_candidates_v2"
        if case == "path":
            url += "/data"
        elif case == "host":
            url = url.replace("bigquery.googleapis.com", "evil.invalid")
        self._http.request("POST" if case == "method" else "GET", url)

    monkeypatch.setattr(bigquery.Client, "get_table", tampered)
    with pytest.raises(f.module().QuestionStoreError):
        execute(values)
    assert calls == []


def test_elapsed_deadline_after_slow_read_stops_before_second_table(monkeypatch):
    values, calls = setup(monkeypatch)
    module = importlib.import_module("src.analysis.open_intelligence.general_question_schema_read")
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    original = AuthorizedSession.request

    def delayed(self, *args, **kwargs):
        response = original(self, *args, **kwargs)
        clock[0] += 241
        return response

    monkeypatch.setattr(AuthorizedSession, "request", delayed)
    with pytest.raises(f.module().QuestionStoreError, match="request_expired"):
        execute(values)
    assert len(calls) == 1


def test_actual_get_table_is_always_called_without_retry(monkeypatch):
    values, _calls = setup(monkeypatch)
    original = bigquery.Client.get_table
    requests_seen = []

    def observed(self, table, *, retry, timeout):
        requests_seen.append((table, retry, timeout))
        return original(self, table, retry=retry, timeout=timeout)

    monkeypatch.setattr(bigquery.Client, "get_table", observed)
    execute(values)
    assert len(requests_seen) == 8
    assert all(retry is None and 0 < timeout <= 5 for _, retry, timeout in requests_seen)
