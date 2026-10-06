"""Current six-table schema validation over the actual guarded SDK transport."""

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
from src.analysis.open_intelligence.general_question_copy_validation import (
    source_copy_schema_requirements,
)

from tests.unit import test_general_question_runtime as runtime
from tests.unit import test_general_question_store as fixture


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_copy_schema")


def resources():
    result = []
    for relation, fields in source_copy_schema_requirements().items():
        resource = bigquery.Table(
            relation,
            schema=[bigquery.SchemaField(name, kind, mode=mode) for name, kind, mode in fields],
        ).to_api_repr()
        resource.update(etag="synthetic-current", lastModifiedTime="123")
        result.append(resource)
    return result


def setup(monkeypatch, change=None):
    values = runtime.setup_runtime()
    tables, calls = resources(), []
    if change:
        change(tables)

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            calls.append((request, kwargs))
            assert urlparse(request.url).netloc == "bigquery.googleapis.com"
            table = next(
                row
                for row in tables
                if row["tableReference"]["tableId"] == urlparse(request.url).path.rsplit("/", 1)[-1]
            )
            response = requests.Response()
            response.request, response.url = request, request.url
            response.status_code = table.get("test_status", 200)
            if response.status_code == 307:
                response.headers["Location"] = "https://evil.invalid/schema"
            response._content = json.dumps(table).encode()
            return response

        def close(self):
            pass

    original = AuthorizedSession.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.mount("https://", Adapter())

    monkeypatch.setattr(AuthorizedSession, "__init__", initialize)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    return values, calls


def read(values, now=None):
    return module().read_question_copy_schemas(
        values[2],
        store=values[0],
        scope=fixture.scope(),
        runtime_identity=values[3],
        credentials=values[4],
        now=now or fixture.NOW + timedelta(seconds=2),
    )


def test_six_exact_current_schemas_pass_through_owned_transport(monkeypatch):
    values, calls = setup(monkeypatch)
    result = read(values)
    expected = source_copy_schema_requirements()
    assert len(calls) == 6
    assert set(result["schema_digests"]) == set(expected)
    assert result["source_authority"] is False
    assert len(result["table_resources"]) == len(result["observations"]) == 6
    assert all(row["etag"] == "synthetic-current" for row in result["table_resources"])
    assert all(0 < kwargs["timeout"] <= 5 for _, kwargs in calls)
    assert all(request.method == "GET" for request, _ in calls)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "mode",
        "type",
        "nested",
        "duplicate",
        "foreign",
        "null_mode",
        "redirect",
        "control_extra",
    ],
)
def test_current_physical_mismatches_refuse_without_historical_digest_substitution(
    monkeypatch, case
):
    def mutate(tables):
        row = tables[0]
        fields = row["schema"]["fields"]
        if case == "missing":
            fields.pop()
        elif case == "mode":
            fields[0]["mode"] = "REQUIRED" if fields[0]["mode"] == "NULLABLE" else "NULLABLE"
        elif case == "type":
            fields[0]["type"] = "BYTES"
        elif case == "nested":
            fields[0].update(
                type="RECORD", fields=[{"name": "child", "type": "STRING", "mode": "REQUIRED"}]
            )
        elif case == "duplicate":
            fields.append(copy.deepcopy(fields[0]))
        elif case == "foreign":
            row["tableReference"]["projectId"] = "foreign"
        elif case == "null_mode":
            fields[0]["mode"] = None
        elif case == "redirect":
            row["test_status"] = 307
        else:
            tables[-1]["schema"]["fields"].append(
                {"name": "extra", "type": "STRING", "mode": "NULLABLE"}
            )

    values, calls = setup(monkeypatch, mutate)
    with pytest.raises((ValueError, fixture.module().QuestionStoreError)):
        read(values)
    if case in ("foreign", "null_mode", "duplicate", "redirect"):
        assert len(calls) == 1


def test_target_addition_and_reordering_are_compatible(monkeypatch):
    def mutate(tables):
        for table in tables:
            table["schema"]["fields"].reverse()
        tables[0]["schema"]["fields"].append(
            {"name": "extra_note", "type": "STRING", "mode": "NULLABLE"}
        )

    values, _calls = setup(monkeypatch, mutate)
    assert read(values)["source_authority"] is False


@pytest.mark.parametrize("case", ["credentials", "expired", "invocation"])
def test_admission_refuses_before_metadata_reads(monkeypatch, case):
    values, calls = setup(monkeypatch)
    if case == "credentials":
        values[4].service_account_email = "foreign"
    elif case == "invocation":
        values[2]["request_digest"] = "f" * 64
    with pytest.raises(fixture.module().QuestionStoreError):
        read(values, fixture.NOW + timedelta(seconds=241) if case == "expired" else None)
    assert calls == []


def test_persisted_metadata_can_be_revalidated_without_accepting_a_saved_digest():
    rows = resources()
    result = module().validate_current_source_copy_schemas(rows)
    assert module().validate_current_source_copy_schemas(result["table_resources"]) == result
    changed = copy.deepcopy(result["table_resources"])
    changed[0]["schema"]["fields"][0]["type"] = "BYTES"
    changed[0]["schema_digest"] = result["schema_digests"][next(iter(result["schema_digests"]))]
    with pytest.raises(ValueError):
        module().validate_current_source_copy_schemas(changed)
    result["table_resources"][0]["schema"]["fields"].clear()
    assert rows[0]["schema"]["fields"]


@pytest.mark.parametrize("case", ["missing_table", "duplicate_table", "oversized", "too_deep"])
def test_pure_metadata_revalidation_refuses_incomplete_or_unbounded_data(case):
    rows = resources()
    if case == "missing_table":
        rows.pop()
    elif case == "duplicate_table":
        rows[-1] = copy.deepcopy(rows[0])
    elif case == "oversized":
        rows[0]["description"] = "x" * 1_000_001
    else:
        field = {"name": "leaf", "type": "STRING", "mode": "NULLABLE"}
        for _ in range(10):
            field = {"name": "nested", "type": "RECORD", "mode": "NULLABLE", "fields": [field]}
        rows[0]["schema"]["fields"].append(field)
    with pytest.raises(ValueError):
        module().validate_current_source_copy_schemas(rows)
