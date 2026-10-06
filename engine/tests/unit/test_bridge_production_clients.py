"""The bridge loader's production clients read natively, bounded, and trust nothing they read.

Every capture, chain, job, clone and pin here is synthetic and unissued. The BigQuery and
Cloud Storage transports are faked at the authorized session, the seam every other native
reader test in this tree fakes; no network, provider or native call is made.
"""

import copy
import hashlib
import io
import json
import socket
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import requests
import urllib3
from google.auth.credentials import AnonymousCredentials
from google.auth.transport.requests import AuthorizedSession
from src.analysis.open_intelligence import bridge_native_clients as native
from src.analysis.open_intelligence import general_question_context_admission as admission
from src.analysis.open_intelligence import source_estate_bridge_evidence as evidence_module
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.production_snapshot_storage import (
    SourceCaptureStorageError,
)

from tests.unit import test_bridge_history_loader as chain_route
from tests.unit import test_bridge_ledger_route as ledger_route
from tests.unit import test_production_snapshot_storage_v2 as storage_v2
from tests.unit.test_daily_execution_authority import HEX

IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
SOURCE_BUCKET = "ogilvy-trends-v2-oi-source-artifacts-staging"
EVIDENCE_BUCKET = "ogilvy-trends-v2-execution-approvals-staging"
PREFIX = "/bigquery/v2/projects/ogilvy-trends-v2"
CLONE = "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_trend_analysis"
V3_INPUTS = ("bridge_policy", "collection_receipt_set", "history_completion_set", "temporal_rules")


def response(method, url, status, content, content_type="application/json"):
    value = requests.Response()
    value.status_code = status
    value.request = requests.Request(method, url).prepare()
    value._content = content
    value.headers["Content-Type"] = content_type
    value.headers["Content-Length"] = str(len(content))
    value.raw = urllib3.response.HTTPResponse(
        body=io.BytesIO(content), headers=value.headers, status=status, preload_content=False
    )
    return value


def _cell(value, kind):
    if value is None:
        return None
    return str(value) if kind == "INT64" else value


class BigQuery:
    """Answers jobs.insert, jobs.get, getQueryResults and tables.get over their REST paths."""

    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.tables = {}
        self.answers = []
        self.submitted = {}
        self.billed = "100"
        self.error = None
        self.status = {}

    def answer(self, predicate, rows, schema):
        self.answers.append((predicate, rows, schema))

    def request(self, method, url, data=None, headers=None, **kwargs):
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "bigquery.googleapis.com"
        body = json.loads(data) if data else None
        self.calls.append((method, parsed.path, parse_qs(parsed.query), body, kwargs))
        path = parsed.path
        if path in self.status:
            code = self.status[path]
            return response(method, url, code, json.dumps({"error": {"code": code}}).encode())
        if method == "POST" and path == PREFIX + "/jobs":
            query = body["configuration"]["query"]
            parameters = {
                item["name"]: item["parameterValue"]["value"]
                for item in query.get("queryParameters", [])
            }
            rows, schema = next(
                (rows, schema)
                for predicate, rows, schema in self.answers
                if predicate(query["query"], parameters)
            )
            resource = copy.deepcopy(body)
            resource.update(
                user_email=IDENTITY,
                status={"state": "DONE", **({"errorResult": self.error} if self.error else {})},
                statistics={
                    "creationTime": "1",
                    "startTime": "1",
                    "endTime": "2",
                    "query": {
                        "statementType": "SELECT",
                        "totalBytesBilled": self.billed,
                        "totalBytesProcessed": "90",
                    },
                },
            )
            self.submitted[body["jobReference"]["jobId"]] = (resource, rows, schema)
            return response(method, url, 200, json.dumps(resource).encode())
        job_id = path.rsplit("/", 1)[-1]
        if method == "GET" and path.startswith(PREFIX + "/queries/"):
            resource, rows, schema = self.submitted[job_id]
            reply = {
                "kind": "bigquery#getQueryResultsResponse",
                "jobReference": resource["jobReference"],
                "jobComplete": True,
                "totalRows": str(len(rows)),
                "schema": {"fields": schema},
                "rows": [
                    {"f": [{"v": _cell(row[f["name"]], f["type"])} for f in schema]} for row in rows
                ],
            }
            return response(method, url, 200, json.dumps(reply).encode())
        if method == "GET" and path.startswith(PREFIX + "/jobs/"):
            found = self.submitted.get(job_id, (self.jobs.get(job_id),))[0]
            if found is None:
                return response(method, url, 404, b'{"error":{"code":404}}')
            return response(method, url, 200, json.dumps(found).encode())
        if method == "GET" and path.startswith(PREFIX + "/datasets/"):
            _, dataset, _, table = path.removeprefix(PREFIX + "/").split("/", 3)[0:4]
            found = self.tables.get(f"ogilvy-trends-v2.{dataset}.{table}")
            if found is None:
                return response(method, url, 404, b'{"error":{"code":404}}')
            return response(method, url, 200, json.dumps(found).encode())
        raise AssertionError(f"unexpected {method} {path}")


class Storage(storage_v2.Transport):
    """The source artifact bucket fake, answering the evidence bucket's objects as well."""

    def seed_input(self, name, raw):
        digest = hashlib.sha256(raw).hexdigest()
        self.seed(f"inputs/{digest}/{name}.json", raw)
        return digest


class Router:
    def __init__(self, bigquery=None, storage=None):
        self.bigquery = bigquery or BigQuery()
        self.storage = storage or Storage()

    def request(self, method, url, *args, **kwargs):
        host = urlparse(url).netloc
        if host == "bigquery.googleapis.com":
            return self.bigquery.request(method, url, *args, **kwargs)
        if host == "storage.googleapis.com":
            return self.storage.request(method, url, *args, **kwargs)
        raise AssertionError(f"unexpected host {host}")


@pytest.fixture
def router(monkeypatch):
    value = Router()
    monkeypatch.setattr(
        AuthorizedSession, "request", lambda _self, *args, **kwargs: value.request(*args, **kwargs)
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))
    for name in (
        "BIGQUERY_EMULATOR_HOST",
        "STORAGE_EMULATOR_HOST",
        "GOOGLE_API_USE_CLIENT_CERTIFICATE",
        "GOOGLE_API_USE_MTLS_ENDPOINT",
        admission.BRIDGE_READS_SWITCH,
    ):
        monkeypatch.delenv(name, raising=False)
    return value


def credentials(email=IDENTITY):
    value = AnonymousCredentials()
    value.token = "synthetic"
    value.service_account_email = email
    return value


def production(**overrides):
    return native.production_bridge_clients(credentials(), **overrides)


def calls(router, method=None):
    return [call for call in router.bigquery.calls if method is None or call[0] == method]


# Object reads: the manifest inputs, the stored capture, the runs and completion records.


def test_read_input_serves_the_v3_bridge_input_names(router):
    clients = production()
    for name in (*V3_INPUTS, "capture_plan", "source_metadata", "storage_policy"):
        raw = canonical_bytes({"contract_version": name + "_v1"})
        digest = router.storage.seed_input(name, raw)
        assert clients["read_input"](name, digest) == raw


@pytest.mark.parametrize("name", V3_INPUTS)
def test_source_capture_objects_accept_the_v3_input_names(name):
    from tests.unit.test_production_snapshot_storage import objects

    service, http = objects()
    raw = canonical_bytes({"contract_version": name + "_v1"})
    digest = hashlib.sha256(raw).hexdigest()
    http.seed(f"inputs/{digest}/{name}.json", raw)
    assert service.read_input(name, digest, timeout=5) == raw


@pytest.mark.parametrize(
    "name", ["operation_context", "build_provenance", "cost_policy", "other", "../capture_plan"]
)
def test_read_input_refuses_names_the_loader_never_reads_before_any_read(router, name):
    with pytest.raises(ValueError, match=r"^bridge_input_invalid$"):
        production()["read_input"](name, "a" * 64)
    assert router.storage.calls == []


def test_read_input_refuses_bytes_that_do_not_hash_to_the_manifest_digest(router):
    raw = canonical_bytes({"contract_version": "bridge_policy_v1"})
    digest = router.storage.seed_input("bridge_policy", raw)
    router.storage.objects[f"inputs/{digest}/bridge_policy.json"]["raw"] = raw + b" "
    with pytest.raises(ValueError, match=r"^bridge_input_unavailable$"):
        production()["read_input"]("bridge_policy", digest)


def test_read_input_serves_bytes_only_through_the_digest_check():
    raw = b'{"contract_version":"bridge_policy_v1"}'
    digest = hashlib.sha256(raw).hexdigest()
    read = evidence_module.bridge_input_reader(lambda name, value: raw + b"x")
    with pytest.raises(ValueError, match=r"^bridge_input_invalid$"):
        read("bridge_policy", digest)
    read = evidence_module.bridge_input_reader(lambda name, value: raw.decode())
    with pytest.raises(ValueError, match=r"^bridge_input_invalid$"):
        read("bridge_policy", digest)
    with pytest.raises(ValueError, match=r"^bridge_input_invalid$"):
        evidence_module.bridge_input_reader(lambda name, value: raw)("bridge_policy", "A" * 64)
    assert evidence_module.bridge_input_reader(lambda name, value: raw)("bridge_policy", digest)


def _stored(router):
    service, http, raw, attempt = storage_v2.prepared()
    router.storage.objects.update(http.objects)
    router.storage.counter = http.counter
    stored = service.store_capture(raw, attempt, timeout=5, artifact_version=storage_v2.V2)
    router.storage.objects.update(http.objects)
    return service, http, raw, stored


def test_the_stored_capture_reads_back_its_facts(router):
    _service, _http, raw, stored = _stored(router)
    assert production()["read_capture_facts"](stored) == json.loads(raw)["capture"]


def test_source_capture_objects_read_a_stored_v2_capture_by_its_receipt():
    service, http, raw, attempt = storage_v2.prepared()
    stored = service.store_capture(raw, attempt, timeout=5, artifact_version=storage_v2.V2)
    before = len(http.calls)
    assert service.read_stored_capture(stored, timeout=5) == json.loads(raw)
    reads = http.calls[before:]
    assert all(call[0] == "GET" for call in reads)
    assert all(call[4]["timeout"] == 5 for call in reads)
    assert any(f"generation={stored['generation']}" in call[1] for call in reads)


def _other_bucket(stored):
    stored["uri"] = stored["uri"].replace(SOURCE_BUCKET, "other-bucket")


def _other_digest_in_uri(stored):
    stored["uri"] = stored["uri"].replace(stored["sha256"], "0" * 64)


def _missing_field(stored):
    stored.pop("created_at")


def _extra_field(stored):
    stored["extra"] = True


def _generation_text(stored):
    stored["generation"] = str(stored["generation"])


@pytest.mark.parametrize(
    "change",
    [_other_bucket, _other_digest_in_uri, _missing_field, _extra_field, _generation_text],
)
def test_a_malformed_stored_receipt_is_refused_before_any_read(change):
    service, http, raw, attempt = storage_v2.prepared()
    stored = service.store_capture(raw, attempt, timeout=5, artifact_version=storage_v2.V2)
    change(stored)
    before = len(http.calls)
    with pytest.raises(SourceCaptureStorageError, match=r"^artifact_invalid$"):
        service.read_stored_capture(stored, timeout=5)
    assert len(http.calls) == before


@pytest.mark.parametrize("defect", ["generation", "size", "hash", "created", "bytes"])
def test_a_stored_capture_that_differs_from_its_receipt_is_refused(defect):
    service, http, raw, attempt = storage_v2.prepared()
    stored = service.store_capture(raw, attempt, timeout=5, artifact_version=storage_v2.V2)
    name = stored["uri"].split("/", 3)[3]
    if defect == "generation":
        stored["generation"] += 1
    elif defect == "size":
        stored["size_bytes"] += 1
    elif defect == "hash":
        stored["sha256"] = "0" * 64
        stored["uri"] = stored["uri"].replace(attempt["sha256"], "0" * 64)
    elif defect == "created":
        stored["created_at"] = "2026-09-21T00:31:00.000000Z"
    else:
        # Same length, other bytes: only the digest of what was read can tell them apart.
        changed = raw.replace(b'"captured_at":"2026', b'"captured_at":"2027', 1)
        assert changed != raw
        assert len(changed) == len(raw)
        http.objects[(name, stored["generation"])]["raw"] = changed
    with pytest.raises(SourceCaptureStorageError):
        service.read_stored_capture(stored, timeout=5)


def test_a_v1_capture_is_not_bridge_capture_facts():
    service, http, raw, _attempt = storage_v2.prepared()
    value = json.loads(raw)
    value["contract_version"] = "open_intelligence_protected_source_capture_artifact_v1"
    raw = canonical_bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    name = f"captures/{storage_v2.MANIFEST}/{digest}/capture.json"
    generation = http.seed(name, raw)
    stored = {
        "uri": f"gs://{SOURCE_BUCKET}/{name}",
        "generation": generation,
        "size_bytes": len(raw),
        "sha256": digest,
        "created_at": "2026-09-21T00:30:00.000000Z",
    }
    with pytest.raises(SourceCaptureStorageError, match=r"^artifact_conflict$"):
        service.read_stored_capture(stored, timeout=5)


def _source_run(router):
    _, record, item = chain_route._collection()
    serialized = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in record.items()
    }
    router.storage.seed(f"42/daily/source_runs/{item['run_id']}.json", canonical_bytes(serialized))
    return record, item


def test_the_source_run_and_completion_readers_read_the_evidence_bucket(router):
    from tests.unit.test_daily_product_completion import completion

    record, item = _source_run(router)
    value = completion()
    router.storage.seed("42/daily/products/daily-1/completion-v1.json", canonical_bytes(value))
    clients = production()
    assert clients["evidence"].read_source_run(item["run_id"]) == {"status": "ok", "run": record}
    assert clients["evidence"].read_source_run("run-absent") == {"status": "ok", "run": None}
    assert clients["evidence"].read_completion("daily-1") == value
    assert clients["evidence"].read_completion("daily-2") is None
    # Object reads only; the storage client may also warm its bucket metadata cache.
    reads = [call for call in router.storage.calls if "/o/" in call[1]]
    assert len(reads) == 6
    assert all(f"/b/{EVIDENCE_BUCKET}/o/42/daily/" in unquote(call[1]) for call in reads)
    assert all(call[4]["timeout"] == native.READ_TIMEOUT for call in reads)


def test_the_evidence_stores_read_through_an_object_reader_that_cannot_write(router):
    reader = native._ReadOnlyObjects(None)
    with pytest.raises(ValueError, match=r"^bridge_object_write_refused$"):
        reader.write("42/daily/products/daily-1/completion-v1.json", b"{}", if_generation_match=0)


# BigQuery reads: jobs.get, tables.get and the fixed statements.


def _job(job_id="oi_v3_snapshot_" + "a" * 64 + "_trend_analysis"):
    return {
        "kind": "bigquery#job",
        "etag": "unissued-etag",
        "id": "ogilvy-trends-v2:US." + job_id,
        "jobReference": {"projectId": "ogilvy-trends-v2", "location": "US", "jobId": job_id},
        "user_email": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        "configuration": {"query": {"query": "CREATE SNAPSHOT TABLE `x` CLONE `y`"}},
        "status": {"state": "DONE"},
        "statistics": {
            "creationTime": "1758414180000",
            "startTime": "1758414180000",
            "endTime": "1758414240000",
            "query": {
                "statementType": "CREATE_SNAPSHOT_TABLE",
                "ddlOperationPerformed": "CREATE",
                "ddlTargetTable": {
                    "projectId": "ogilvy-trends-v2",
                    "datasetId": "trends_v2_staging",
                    "tableId": "staging_bridge_v3_20260920_trend_analysis",
                },
            },
        },
    }


def test_jobs_get_returns_the_job_resource_whole(router):
    job = _job()
    job_id = job["jobReference"]["jobId"]
    router.bigquery.jobs[job_id] = job
    assert production()["read_native_job"](job_id) == job
    ((method, path, query, body, kwargs),) = router.bigquery.calls
    assert (method, path, body) == ("GET", f"{PREFIX}/jobs/{job_id}", None)
    assert query["location"] == ["US"]
    assert kwargs["timeout"] == native.READ_TIMEOUT
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("job_id", ["", "a/b", "../jobs", "a b", "a" * 1025, None, 7])
def test_jobs_get_refuses_an_unformed_job_id_before_any_call(router, job_id):
    with pytest.raises(ValueError, match=r"^bridge_native_job_invalid$"):
        production()["read_native_job"](job_id)
    assert router.bigquery.calls == []


@pytest.mark.parametrize("code", [404, 403, 500, 503])
def test_jobs_get_fails_closed_once_without_a_retry(router, code):
    job_id = _job()["jobReference"]["jobId"]
    router.bigquery.status[f"{PREFIX}/jobs/{job_id}"] = code
    with pytest.raises(ValueError, match=r"^bridge_native_job_unavailable$"):
        production()["read_native_job"](job_id)
    assert len(router.bigquery.calls) == 1


@pytest.mark.parametrize(
    "reference",
    [
        {"projectId": "ogilvy-trends-v2", "location": "US", "jobId": "other"},
        {"projectId": "other-project", "location": "US"},
        {"projectId": "ogilvy-trends-v2", "location": "EU"},
        None,
    ],
)
def test_jobs_get_refuses_a_resource_for_another_job(router, reference):
    job = _job()
    job_id = job["jobReference"]["jobId"]
    if reference is None:
        job.pop("jobReference")
    else:
        job["jobReference"] = {"jobId": job_id, **reference}
    router.bigquery.jobs[job_id] = job
    with pytest.raises(ValueError, match=r"^bridge_native_job_invalid$"):
        production()["read_native_job"](job_id)


def _table(name=CLONE):
    project, dataset, table = name.split(".")
    return {
        "kind": "bigquery#table",
        "etag": "unissued-etag",
        "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
        "type": "SNAPSHOT",
        "numRows": "3",
        "location": "US",
    }


def test_tables_get_returns_the_clone_resource_whole(router):
    router.bigquery.tables[CLONE] = _table()
    assert production()["read_clone"](CLONE) == _table()
    ((method, path, _query, _body, kwargs),) = router.bigquery.calls
    table = CLONE.rsplit(".", 1)[1]
    assert (method, path) == ("GET", f"{PREFIX}/datasets/trends_v2_staging/tables/{table}")
    assert kwargs["timeout"] == native.READ_TIMEOUT
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "name",
    [
        "ogilvy-trends-v2.trends_v2_staging.trend_analysis",
        "ogilvy-trends-v2.trends_v2_staging_approvals.staging_bridge_v3_20260920_trend_analysis",
        "other-project.trends_v2_staging.staging_bridge_v3_20260920_trend_analysis",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_2026092_trend_analysis",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_daily_summary",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_trend_analysis/x",
        CLONE + " ",
        None,
    ],
)
def test_tables_get_admits_only_a_bridge_clone_name(router, name):
    with pytest.raises(ValueError, match=r"^bridge_clone_invalid$"):
        production()["read_clone"](name)
    assert router.bigquery.calls == []


def test_tables_get_refuses_a_resource_for_another_table_and_fails_closed(router):
    router.bigquery.tables[CLONE] = _table(CLONE.replace("trend_analysis", "trend_scores"))
    with pytest.raises(ValueError, match=r"^bridge_clone_invalid$"):
        production()["read_clone"](CLONE)
    del router.bigquery.tables[CLONE]
    with pytest.raises(ValueError, match=r"^bridge_clone_unavailable$"):
        production()["read_clone"](CLONE)


LEDGER_SCHEMA = [
    {"name": "consumption_count", "type": "INT64"},
    {"name": "approval_count", "type": "INT64"},
    {"name": "result_count", "type": "INT64"},
    {"name": "consumption_json", "type": "STRING"},
    {"name": "approval_json", "type": "STRING"},
    {"name": "result_json", "type": "STRING"},
]


def _is_ledger(sql, parameters):
    from src.analysis.open_intelligence.general_question_context_queries import _RESULT_SQL_V2

    return sql == _RESULT_SQL_V2


def _answer_ledger(router, w):
    """The ledger rows of the capture and of the funded chain its receipt names, by id."""
    funded = w["funded"]["consumption"].consumption_id
    router.bigquery.answer(
        lambda sql, parameters: (
            _is_ledger(sql, parameters) and parameters.get("consumption_id") == funded
        ),
        w["funded"]["rows"],
        LEDGER_SCHEMA,
    )
    router.bigquery.answer(_is_ledger, w["ledger"]["rows"], LEDGER_SCHEMA)


def test_the_ledger_reader_runs_the_v2_chain_statement_by_consumption_id(router, tmp_path):
    from src.analysis.open_intelligence.general_question_context_queries import _RESULT_SQL_V2

    w = ledger_route.ledger_world(tmp_path)
    rows = w["ledger"]["rows"]
    router.bigquery.answer(_is_ledger, rows, LEDGER_SCHEMA)
    consumption_id = w["ledger"]["consumption"].consumption_id
    assert production()["ledger_reader"](consumption_id) == rows
    ((_, path, _, body, _),) = calls(router, "POST")
    assert path == PREFIX + "/jobs"
    query = body["configuration"]["query"]
    assert query["query"] == _RESULT_SQL_V2
    assert query["queryParameters"] == [
        {
            "name": "consumption_id",
            "parameterType": {"type": "STRING"},
            "parameterValue": {"value": consumption_id},
        }
    ]
    assert query["useQueryCache"] is False
    assert query["useLegacySql"] is False
    assert query["maximumBytesBilled"] == str(native.LEDGER_BYTES_BILLED)
    assert body["jobReference"]["location"] == "US"
    assert all(call[4]["timeout"] == native.READ_TIMEOUT for call in router.bigquery.calls)
    assert all(call[4]["allow_redirects"] is False for call in router.bigquery.calls)


@pytest.mark.parametrize(
    "consumption_id", ["exc_" + "A" * 64, "exc_" + "a" * 63, "exr_" + "a" * 64]
)
def test_the_ledger_reader_refuses_an_unformed_consumption_id_before_any_call(
    router, consumption_id
):
    with pytest.raises(ValueError, match=r"^bridge_ledger_invalid$"):
        production()["ledger_reader"](consumption_id)
    assert router.bigquery.calls == []


def _two_rows(bigquery, rows):
    bigquery.answer(_is_ledger, rows * 2, LEDGER_SCHEMA)


def _over_cap(bigquery, rows):
    bigquery.answer(_is_ledger, rows, LEDGER_SCHEMA)
    bigquery.billed = str(native.LEDGER_BYTES_BILLED + 1)


def _unmetered(bigquery, rows):
    bigquery.answer(_is_ledger, rows, LEDGER_SCHEMA)
    bigquery.billed = None


def _failed(bigquery, rows):
    bigquery.answer(_is_ledger, rows, LEDGER_SCHEMA)
    bigquery.error = {"reason": "invalidQuery", "message": "synthetic"}


@pytest.mark.parametrize("defect", [_two_rows, _over_cap, _unmetered, _failed])
def test_a_ledger_read_that_is_not_one_metered_row_fails_closed(router, tmp_path, defect):
    w = ledger_route.ledger_world(tmp_path)
    defect(router.bigquery, w["ledger"]["rows"])
    with pytest.raises(ValueError, match=r"^bridge_native_query_(unavailable|invalid)$"):
        production()["ledger_reader"](w["ledger"]["consumption"].consumption_id)
    assert len(calls(router, "POST")) == 1


APPROVAL_SCHEMA = [
    {"name": "approval_count", "type": "INT64"},
    {"name": "approval_json", "type": "STRING"},
]


def _grant_rows(grant, digest, count=1, **changes):
    approval = {
        "manifest_sha256": digest,
        "canonical_manifest_json": canonical_bytes(grant).decode(),
        **changes,
    }
    return [{"approval_count": count, "approval_json": json.dumps(approval)}]


def _is_approval(sql, parameters):
    from src.analysis.open_intelligence.general_question_context_queries import (
        _APPROVAL_SQL_V2,
    )

    return sql == _APPROVAL_SQL_V2


def test_the_grant_reader_returns_the_grant_whose_terms_hash_to_the_digest(router):
    from src.analysis.open_intelligence.recurring_grant import grant_digest

    grant = chain_route.world()["grant"]
    digest = grant_digest(grant)
    router.bigquery.answer(_is_approval, _grant_rows(grant, digest), APPROVAL_SCHEMA)
    assert production()["read_grant"](digest) == grant
    ((_, _, _, body, _),) = calls(router, "POST")
    parameters = body["configuration"]["query"]["queryParameters"]
    assert parameters[0]["name"] == "manifest_sha256"
    assert parameters[0]["parameterValue"] == {"value": digest}


def _other_terms(grant, digest):
    changed = copy.deepcopy(grant)
    changed["source_policy_digest"] = "1" * 64
    return _grant_rows(changed, digest)


def _other_row_digest(grant, digest):
    return _grant_rows(grant, "0" * 64)


def _two_approvals(grant, digest):
    return _grant_rows(grant, digest, count=2)


def _no_approval(grant, digest):
    return [{"approval_count": 0, "approval_json": None}]


def _duplicate_key(grant, digest):
    rows = _grant_rows(grant, digest)
    rows[0]["approval_json"] = rows[0]["approval_json"][:-1] + ',"manifest_sha256":"x"}'
    return rows


@pytest.mark.parametrize(
    "rows", [_other_terms, _other_row_digest, _two_approvals, _no_approval, _duplicate_key]
)
def test_the_grant_reader_refuses_a_grant_it_cannot_bind_to_the_digest(router, rows):
    from src.analysis.open_intelligence.recurring_grant import grant_digest

    grant = chain_route.world()["grant"]
    digest = grant_digest(grant)
    router.bigquery.answer(_is_approval, rows(grant, digest), APPROVAL_SCHEMA)
    with pytest.raises(ValueError, match=r"^bridge_grant_invalid$"):
        production()["read_grant"](digest)


def test_the_grant_reader_refuses_an_unformed_digest_before_any_call(router):
    with pytest.raises(ValueError, match=r"^bridge_grant_invalid$"):
        production()["read_grant"]("A" * 64)
    assert router.bigquery.calls == []


PRODUCT_SCHEMA = [
    {"name": "market", "type": "STRING"},
    {"name": "trend_date", "type": "DATE"},
    {"name": "term", "type": "STRING"},
]


def test_the_product_readback_reads_one_recorded_lane_day_once(router):
    router.bigquery.answer(
        lambda sql, parameters: True, copy.deepcopy(chain_route.PRODUCT_ROWS), PRODUCT_SCHEMA
    )
    evidence = production()["evidence"]
    first = evidence.read_product_rows("trend_analysis", "2026-09-19")
    assert [
        {**row, "trend_date": row["trend_date"].isoformat()} for row in first
    ] == chain_route.PRODUCT_ROWS
    first.append({"market": "za"})
    assert len(evidence.read_product_rows("trend_analysis", "2026-09-19")) == 3
    ((_, _, _, body, _),) = calls(router, "POST")
    query = body["configuration"]["query"]
    assert query["query"] == (
        "SELECT * FROM `ogilvy-trends-v2.trends_v2_staging.trend_analysis`"
        " WHERE trend_date = @product_date"
    )
    assert query["queryParameters"] == [
        {
            "name": "product_date",
            "parameterType": {"type": "DATE"},
            "parameterValue": {"value": "2026-09-19"},
        }
    ]
    assert query["maximumBytesBilled"] == str(native.PRODUCT_BYTES_BILLED)
    evidence.read_product_rows("seed_candidates", "2026-09-19")
    seed = calls(router, "POST")[1][3]["configuration"]["query"]["query"]
    assert seed.endswith(
        "`ogilvy-trends-v2.trends_v2_staging.seed_candidates` WHERE proposed_date = @product_date"
    )


@pytest.mark.parametrize(
    ("lane", "day"),
    [
        ("trend_scores", "2026-09-19"),
        ("event_ledger", "2026-09-19"),
        ("trend_analysis`; DROP", "2026-09-19"),
        ("trend_analysis", "2026-9-19"),
        ("trend_analysis", "2026-09-19T00:00:00"),
        ("trend_analysis", None),
    ],
)
def test_the_product_readback_refuses_lanes_without_a_completion_record(router, lane, day):
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        production()["evidence"].read_product_rows(lane, day)
    assert router.bigquery.calls == []


@pytest.mark.parametrize("lane", ["trend_scores", "event_ledger", "raw_content", None])
def test_the_evidence_reads_product_rows_only_for_lanes_the_completion_record_carries(lane):
    reads = []
    evidence = evidence_module.BridgeEvidence(
        locate_chain=None,
        read_source_run=None,
        read_completion=None,
        read_product_rows=lambda *args: reads.append(args) or [],
    )
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        evidence.read_product_rows(lane, "2026-09-19")
    assert reads == []
    assert evidence.read_product_rows("seed_candidates", "2026-09-19") == []
    assert reads == [("seed_candidates", "2026-09-19")]


def test_the_product_readback_refuses_a_truncated_read(router, monkeypatch):
    monkeypatch.setattr(native, "PRODUCT_ROW_LIMIT", 2)
    router.bigquery.answer(
        lambda sql, parameters: True, copy.deepcopy(chain_route.PRODUCT_ROWS), PRODUCT_SCHEMA
    )
    with pytest.raises(ValueError, match=r"^bridge_native_query_invalid$"):
        production()["evidence"].read_product_rows("trend_analysis", "2026-09-19")


# The query request guard: the pinned host and paths, https only, and a single submission.

JOB_PATH = PREFIX + "/jobs"
RESULT_PATH = PREFIX + "/queries/bridge_job"


def _guarded_query(router, monkeypatch, *outgoing):
    """Run query_rows with the client's query replaced by one that sends ``outgoing`` through
    the guarded session; return the refusal the guard raised."""
    from google.cloud import bigquery

    def query(client, sql, **kwargs):
        for method, url in outgoing:
            client._http.request(method, url, data=b"{}")
        raise AssertionError("every outgoing request was admitted")

    monkeypatch.setattr(bigquery.Client, "query", query)
    router.bigquery.status[JOB_PATH] = 200
    with pytest.raises(ValueError, match=r"^bridge_native_query_unavailable$") as caught:
        native.BridgeWarehouse(credentials()).query_rows(
            "SELECT 1", [], maximum_bytes_billed=1, row_limit=1
        )
    return caught.value.__cause__


def _refused(cause):
    return type(cause) is ValueError and str(cause) == "bridge_native_request_refused"


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "https://bigquery.example.invalid" + JOB_PATH),
        ("GET", "https://storage.googleapis.com" + RESULT_PATH),
        ("POST", "http://bigquery.googleapis.com" + JOB_PATH),
        ("GET", "http://bigquery.googleapis.com" + RESULT_PATH),
    ],
    ids=["other_host_post", "other_host_get", "plain_http_post", "plain_http_get"],
)
def test_the_query_guard_refuses_another_host_or_scheme_before_it_goes_out(
    router, monkeypatch, method, url
):
    assert _refused(_guarded_query(router, monkeypatch, (method, url)))
    assert router.bigquery.calls == []
    assert router.storage.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", PREFIX + "/queries"),
        ("POST", PREFIX + "/jobs/bridge_job/cancel"),
        ("POST", "/bigquery/v2/projects/other-project/jobs"),
        ("GET", PREFIX + "/datasets/trends_v2_staging/tables/raw_content"),
        ("GET", "/bigquery/v2/projects/other-project/jobs/bridge_job"),
        ("GET", PREFIX + "/jobs/bridge job"),
    ],
    ids=["queries", "cancel", "other_project_post", "tables", "other_project_get", "job_id"],
)
def test_the_query_guard_refuses_a_path_outside_jobs_and_queries(router, monkeypatch, method, path):
    assert _refused(_guarded_query(router, monkeypatch, (method, "https://" + native.HOST + path)))
    assert router.bigquery.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", PREFIX + "/jobs/../datasets/trends_v2_staging/tables/raw_content/data"),
        ("GET", PREFIX + "/queries/../datasets/trends_v2_staging/tables/raw_content/data"),
        ("GET", PREFIX + "/jobs/../../other-project/datasets/d/tables/t/data"),
        ("GET", PREFIX + "/jobs/%2e%2e/datasets/trends_v2_staging/tables/raw_content/data"),
        ("GET", PREFIX + "/jobs/%2E%2E/datasets/trends_v2_staging/tables/raw_content/data"),
        ("GET", PREFIX + "/jobs/%2e%2E/datasets/trends_v2_staging/tables/raw_content/data"),
        ("GET", PREFIX + "/jobs/bridge_job/cancel"),
        ("GET", PREFIX + "/queries/bridge_job/extra"),
        ("GET", PREFIX + "//jobs/bridge_job"),
        ("GET", PREFIX + "/jobs/bridge_job;x"),
        ("GET", PREFIX + "/queries/bridge_job;x"),
        ("POST", PREFIX + "/jobs;x"),
        ("POST", PREFIX + "/jobs/"),
        ("GET", PREFIX + "/jobs/."),
        ("GET", PREFIX + "/jobs/.."),
        ("GET", PREFIX + "/queries/%2e%2e"),
        ("GET", PREFIX + "/datasets/trends_v2_staging"),
    ],
    ids=[
        "jobs_dot_segments",
        "queries_dot_segments",
        "other_project_dot_segments",
        "encoded_lower",
        "encoded_upper",
        "encoded_mixed",
        "jobs_extra_segment",
        "queries_extra_segment",
        "empty_segment",
        "jobs_params",
        "queries_params",
        "post_params",
        "post_trailing_slash",
        "jobs_dot",
        "jobs_dot_dot",
        "queries_encoded_dot_dot",
        "datasets_get",
    ],
)
def test_the_query_guard_refuses_a_path_that_only_starts_like_jobs_or_queries(
    router, monkeypatch, method, path
):
    """The whole path must be the jobs path, or one job id under jobs or queries: dot
    segments, encoded dots, extra segments and path parameters never reach the transport."""
    assert _refused(_guarded_query(router, monkeypatch, (method, "https://" + native.HOST + path)))
    assert router.bigquery.calls == []


def test_the_query_guard_admits_one_submission_only(router, monkeypatch):
    submit = ("POST", "https://" + native.HOST + JOB_PATH)
    assert _refused(_guarded_query(router, monkeypatch, submit, submit))
    assert [call[:2] for call in router.bigquery.calls] == [("POST", JOB_PATH)]


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "HEAD"])
@pytest.mark.parametrize("path", [JOB_PATH, PREFIX + "/jobs/bridge_job", RESULT_PATH])
def test_the_query_guard_refuses_any_method_but_one_post_and_reads(
    router, monkeypatch, method, path
):
    assert _refused(_guarded_query(router, monkeypatch, (method, "https://" + native.HOST + path)))
    assert router.bigquery.calls == []


def test_the_query_guard_admits_the_submission_and_its_reads(router, monkeypatch):
    """The guard's own admitted set, so each refusal above is the guard's and not the fake's."""
    base = "https://" + native.HOST
    for path in (PREFIX + "/jobs/bridge_job", RESULT_PATH):
        router.bigquery.status[path] = 200
    cause = _guarded_query(
        router,
        monkeypatch,
        ("POST", base + JOB_PATH),
        ("GET", base + PREFIX + "/jobs/bridge_job"),
        ("GET", base + RESULT_PATH),
    )
    assert type(cause) is AssertionError
    assert [call[:2] for call in router.bigquery.calls] == [
        ("POST", JOB_PATH),
        ("GET", PREFIX + "/jobs/bridge_job"),
        ("GET", RESULT_PATH),
    ]
    assert all(call[4]["allow_redirects"] is False for call in router.bigquery.calls)
    assert all(call[4]["timeout"] == native.READ_TIMEOUT for call in router.bigquery.calls)


# The provider: identity, environment, the daily chain and the configuration switch.


def test_the_provider_serves_exactly_the_loader_s_clients(router):
    from src.analysis.open_intelligence.general_question_snapshot import BRIDGE_CLIENT_NAMES

    clients = production()
    assert set(clients) == BRIDGE_CLIENT_NAMES
    assert clients["generation_loader"] is None
    assert router.bigquery.calls == []
    assert router.storage.calls == []


def test_the_provider_runs_only_as_the_brain_identity(router):
    with pytest.raises(ValueError, match=r"^bridge_identity_invalid$"):
        native.production_bridge_clients(
            credentials("other@ogilvy-trends-v2.iam.gserviceaccount.com")
        )
    with pytest.raises(ValueError, match=r"^bridge_identity_invalid$"):
        native.production_bridge_clients(AnonymousCredentials())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BIGQUERY_EMULATOR_HOST", "localhost:9050"),
        ("STORAGE_EMULATOR_HOST", "localhost:9023"),
        ("GOOGLE_API_USE_CLIENT_CERTIFICATE", "true"),
        ("GOOGLE_API_USE_MTLS_ENDPOINT", "always"),
    ],
)
def test_the_provider_refuses_a_redirected_environment(router, monkeypatch, name, value):
    clients = production()
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=r"^bridge_environment_invalid$"):
        production()
    with pytest.raises(ValueError, match=r"^bridge_environment_invalid$"):
        clients["read_clone"](CLONE)
    assert router.bigquery.calls == []


def test_the_daily_chain_has_no_production_transport_and_refuses(router):
    from src.analysis.open_intelligence.daily_execution_authority import (
        DailyAuthorityUnavailable,
        read_daily_execution_chain,
    )

    from tests.unit.source_bridge_chain_fixture import result_ref

    clients = production()
    with pytest.raises(DailyAuthorityUnavailable, match=r"^bridge_daily_chain_unavailable$"):
        read_daily_execution_chain(derivation_id="exd_" + HEX, clients=clients["capture_clients"])
    chain = chain_route._collection()[0][2]
    with pytest.raises(ValueError, match=r"^bridge_daily_chain_unavailable$"):
        clients["evidence"].chain_source(result_ref(chain))
    with pytest.raises(ValueError, match=r"^bridge_daily_chain_unavailable$"):
        clients["evidence"].completion_operation(result_ref(chain))
    assert router.bigquery.calls == []
    assert router.storage.calls == []


# The evidence object against the loader.


def _chain_evidence(w, router, **overrides):
    chains = {
        w["collection"][2].result["result_id"]: w["collection"][:2],
    }
    for derivation_id, clients, chain in w["composition"]:
        chains[chain.result["result_id"]] = (derivation_id, clients)

    def locate(reference):
        return chains[reference["result_id"]]

    return native.bridge_evidence(credentials(), locate_chain=locate, **overrides)


def _completion_world(monkeypatch):
    """The loader world with its completion record under the composition chain's slot."""
    from tests.unit.test_daily_product_completion import completion

    monkeypatch.setattr(chain_route, "completion", lambda: {**completion(), "operation_id": HEX})
    composition = []
    original = chain_route._composition

    def record_composition():
        value = original()
        composition.append(value[0])
        return value

    monkeypatch.setattr(chain_route, "_composition", record_composition)
    w = chain_route.world()
    w["composition"] = composition
    return w


def _serve_evidence(w, router):
    record = w["evidence"].record
    router.storage.seed(f"42/daily/products/{HEX}/completion-v1.json", canonical_bytes(record))
    run = w["evidence"].source_run
    serialized = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in run.items()
    }
    router.storage.seed(
        f"42/daily/source_runs/{run['receipt']['run_id']}.json", canonical_bytes(serialized)
    )
    router.bigquery.answer(
        lambda sql, parameters: (
            "trend_analysis" in sql and parameters["product_date"] == "2026-09-19"
        ),
        copy.deepcopy(chain_route.PRODUCT_ROWS),
        PRODUCT_SCHEMA,
    )
    router.bigquery.answer(lambda sql, parameters: True, [], PRODUCT_SCHEMA)


def _serve_clones(w, router):
    router.bigquery.jobs.update(copy.deepcopy(w["clones"]["native_jobs"]))
    router.bigquery.tables.update(copy.deepcopy(w["clones"]["clone_metadata"]))


def test_the_production_evidence_and_readers_admit_a_daily_chain_capture(
    router, monkeypatch, tmp_path
):
    from src.analysis.open_intelligence.recurring_grant import grant_digest

    w = _completion_world(monkeypatch)
    chain_route.register(monkeypatch, tmp_path, w["capture"])
    _serve_evidence(w, router)
    _serve_clones(w, router)
    for name, raw in w["raw"].items():
        if name in evidence_module.BRIDGE_INPUT_NAMES:
            router.storage.seed_input(name, raw)
    digest = grant_digest(w["grant"])
    router.bigquery.answer(_is_approval, _grant_rows(w["grant"], digest), APPROVAL_SCHEMA)
    router.bigquery.answers.insert(0, router.bigquery.answers.pop())
    clients = production()
    loaded = chain_route.load(
        w,
        read_input=clients["read_input"],
        read_grant=clients["read_grant"],
        evidence=_chain_evidence(w, router),
        read_native_job=clients["read_native_job"],
        read_clone=clients["read_clone"],
    )
    assert [(c["lane"], c["market"], c["state"]) for c in loaded.cells] == [
        ("trend_analysis", "ng", "completed"),
        ("trend_analysis", "za", "completed"),
    ]
    assert loaded.collection_runs == ("run-20260920",)
    job_reads = [call[1] for call in calls(router, "GET") if "/jobs/capture_" in call[1]]
    assert len(job_reads) == len(w["clones"]["native_jobs"])


def test_the_completion_operation_is_the_composition_chain_s_own_slot(router, monkeypatch):
    from tests.unit.source_bridge_chain_fixture import result_ref

    w = _completion_world(monkeypatch)
    evidence = _chain_evidence(w, router)
    (composition,) = w["composition"]
    assert evidence.completion_operation(result_ref(composition[2])) == HEX
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        evidence.completion_operation(result_ref(w["collection"][2]))


def test_the_production_evidence_refuses_a_substituted_completion_record(
    router, monkeypatch, tmp_path
):
    w = _completion_world(monkeypatch)
    chain_route.register(monkeypatch, tmp_path, w["capture"])
    _serve_evidence(w, router)
    record = copy.deepcopy(w["evidence"].record)
    record["dynamic_receipt_digest"] = "9" * 64
    name = f"42/daily/products/{HEX}/completion-v1.json"
    router.storage.seed(name, canonical_bytes(record))
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        chain_route.load(w, evidence=_chain_evidence(w, router))


def test_the_production_readers_admit_a_ledger_capture(router, tmp_path, monkeypatch):
    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    _answer_ledger(router, w)
    _serve_clones(w, router)
    for name, raw in w["ledger_raw"].items():
        if name in evidence_module.BRIDGE_INPUT_NAMES:
            router.storage.seed_input(name, raw)
    clients = production()
    loaded = ledger_route.load(
        w,
        read_input=clients["read_input"],
        ledger_reader=clients["ledger_reader"],
        read_native_job=clients["read_native_job"],
        read_clone=clients["read_clone"],
    )
    assert loaded.binding["result_id"] == w["ledger"]["result"].result_id
    # One ledger query for the capture and one for the funded chain its receipt names.
    assert len(calls(router, "POST")) == 2


def test_a_clone_replaced_after_capture_is_refused_through_the_production_reader(
    router, tmp_path, monkeypatch
):
    w = ledger_route.ledger_world(tmp_path)
    ledger_route.register(monkeypatch, tmp_path, w)
    _answer_ledger(router, w)
    _serve_clones(w, router)
    for table in router.bigquery.tables.values():
        table["creationTime"] = str(int(table["creationTime"]) + 1)
        break
    for name, raw in w["ledger_raw"].items():
        if name in evidence_module.BRIDGE_INPUT_NAMES:
            router.storage.seed_input(name, raw)
    clients = production()
    ledger_route.refused(
        w,
        read_input=clients["read_input"],
        ledger_reader=clients["ledger_reader"],
        read_native_job=clients["read_native_job"],
        read_clone=clients["read_clone"],
    )


# The configuration switch.


def test_the_bridge_switch_defaults_off(router):
    assert admission.bridge_clients_provider() is None


@pytest.mark.parametrize("value", ["", "1", "true", "on", "Enabled", " enabled", "enabled\n"])
def test_only_the_explicit_value_turns_bridge_reads_on(router, monkeypatch, value):
    monkeypatch.setenv(admission.BRIDGE_READS_SWITCH, value)
    assert admission.bridge_clients_provider() is None


def test_the_switch_serves_the_production_provider(router, monkeypatch):
    monkeypatch.setenv(admission.BRIDGE_READS_SWITCH, "enabled")
    assert admission.bridge_clients_provider() is native.production_bridge_clients
    from src.analysis.open_intelligence.general_question_snapshot import (
        BRIDGE_CLIENT_NAMES,
        production_bridge_clients,
    )

    assert set(production_bridge_clients(credentials())) == BRIDGE_CLIENT_NAMES


def test_the_ceiling_moves_only_when_switched_on_and_a_bridge_row_is_registered(
    router, monkeypatch, tmp_path
):
    from tests.unit.test_general_question_bridge_context import bridge

    as_of = "2026-09-23T01:00:00Z"
    monkeypatch.setenv(admission.BRIDGE_READS_SWITCH, "enabled")
    assert admission.source_window_hint(as_of)["cutoff_date"] == "2026-09-07"
    route = bridge()
    w = route.ledger_world(tmp_path / "bridge")
    route.register(monkeypatch, tmp_path, w)
    assert admission.source_window_hint(as_of)["cutoff_date"] == "2026-09-22"
    monkeypatch.delenv(admission.BRIDGE_READS_SWITCH)
    assert admission.source_window_hint(as_of)["cutoff_date"] == "2026-09-07"


def test_switched_on_ask_reads_through_the_production_provider(router, monkeypatch, tmp_path):
    from tests.unit import test_general_question_bridge_context as context

    values = context.ask(monkeypatch, tmp_path, wired=False)
    monkeypatch.setenv(admission.BRIDGE_READS_SWITCH, "enabled")
    built = []
    original = native.production_bridge_clients

    def spy(value):
        built.append(value)
        return original(value)

    monkeypatch.setattr(native, "production_bridge_clients", spy)
    result = context.build(values, bridge_clients=None)
    assert built == [values["credentials"]]
    assert result["status"] == "coverage_gap"
    # The synthetic generation is one the packaged catalogue does not trust, and the daily
    # chain behind every collection receipt has no production transport yet; either way
    # the production provider refuses the capture with the loader's own code.
    assert result["missing_work"] == ["bridge_source_invalid"]
