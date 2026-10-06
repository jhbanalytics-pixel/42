import copy
import importlib
import json
import socket
from collections import Counter
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
from src.analysis.open_intelligence.brain_contract import canonical_digest

from tests.unit import test_production_snapshot as fixture

CREATOR = "snapshot-reader@example.invalid"


def module():
    return importlib.import_module("src.analysis.open_intelligence.production_snapshot_capture")


def wire(value, schema):
    if schema.get("mode") == "REPEATED":
        return [{"v": wire(item, {**schema, "mode": "NULLABLE"})} for item in value]
    if value is None:
        return None
    if isinstance(value, datetime):
        return str(int(value.timestamp() * 1_000_000))
    if isinstance(value, date):
        return value.isoformat()
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return str(value)
    return value


class HTTP:
    is_mtls = False

    def __init__(self):
        self.inputs = fixture.inputs()
        self.calls = []
        self.jobs = {}
        self.rows = copy.deepcopy(self.inputs["rows_by_table"])
        self.mutate_job = lambda value: value
        self.mutate_result = lambda value: value
        self.mutate_metadata = lambda value: value
        self.running_first = False
        self.lose_ack = False
        self.paginate = False
        self.fail_next_page = False
        self.event_pages = []

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "bigquery.googleapis.com"
        data = json.loads(kwargs["data"]) if kwargs.get("data") else None
        self.calls.append((method, parsed.path, data))
        if "/tables/" in parsed.path:
            lane = next(lane for lane in self.rows if parsed.path.endswith("_" + lane))
            body = copy.deepcopy(self.inputs["snapshot_metadata"][lane])
            body["location"] = "US"
            body = self.mutate_metadata(body)
        elif method == "POST":
            assert parsed.path.endswith("/jobs")
            sql = data["configuration"]["query"]["query"]
            lane = next(lane for lane in self.rows if f"_{lane}`" in sql)
            body = copy.deepcopy(data)
            body.update(
                user_email=CREATOR,
                status={"state": "DONE"},
                statistics={
                    "creationTime": str(int(fixture.row_fixture.CAPTURED_AT.timestamp() * 1000)),
                    "query": {"statementType": "SELECT", "totalBytesBilled": "100"},
                },
            )
            self.jobs[data["jobReference"]["jobId"]] = (self.mutate_job(body), lane)
            body = self.jobs[data["jobReference"]["jobId"]][0]
            if self.lose_ack:
                raise requests.Timeout("synthetic unknown acknowledgement")
            if self.running_first:
                body = copy.deepcopy(body)
                body["status"]["state"] = "RUNNING"
        elif "/queries/" in parsed.path:
            job, lane = self.jobs[parsed.path.rsplit("/", 1)[1]]
            rows = copy.deepcopy(self.rows[lane])
            params = {
                p["name"]: p["parameterValue"]
                for p in job["configuration"]["query"]["queryParameters"]
            }
            if "sample_ids" in params:
                keys = set(
                    zip(
                        [v["value"] for v in params["sample_markets"]["arrayValues"]],
                        [v["value"] for v in params["sample_ids"]["arrayValues"]],
                        strict=True,
                    )
                )
                rows = [row for row in rows if (row["market"], row["id"]) in keys]
            fields = fixture.row_fixture.field_schema(lane)
            schema = [fields[name] for name in fixture.row_fixture.subject.physical_fields(lane)]
            if lane in ("raw_content", "enriched_content"):
                schema.append({"name": "match_count", "type": "INT64"})
                counts = Counter((row["market"], row["id"]) for row in rows)
                for row in rows:
                    row["match_count"] = counts[(row["market"], row["id"])]
            schema.append({"name": "capture_row_count", "type": "INT64"})
            for row in rows:
                row["capture_row_count"] = len(rows)
            body = {
                "jobReference": job["jobReference"],
                "jobComplete": True,
                "totalRows": str(len(rows)),
                "schema": {"fields": schema},
                "rows": [
                    {"f": [{"v": wire(row[field["name"]], field)} for field in schema]}
                    for row in rows
                ],
            }
            if self.paginate and lane == "event_ledger":
                token = parse_qs(parsed.query).get("pageToken", [None])[0]
                self.event_pages.append(token)
                if token is None:
                    body["rows"] = body["rows"][:1]
                    body["pageToken"] = "synthetic-page-2"
                else:
                    assert token == "synthetic-page-2"
                    if self.fail_next_page:
                        raise requests.ConnectionError("synthetic next-page failure")
                    body["rows"] = body["rows"][1:]
            body = self.mutate_result(body)
        else:
            body = self.jobs[parsed.path.rsplit("/", 1)[1]][0]
        response = requests.Response()
        response.status_code = 200
        response.headers["content-type"] = "application/json"
        response._content = json.dumps(body).encode()
        response.request = requests.Request(method, url).prepare()
        return response


def run(monkeypatch, http, **overrides):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network prohibited"))
    monkeypatch.setattr(module(), "_utc_now", lambda: fixture.row_fixture.CAPTURED_AT)
    monkeypatch.setattr(module(), "monotonic", lambda: 0)
    client = bigquery.Client(
        project="ogilvy-trends-v2", credentials=AnonymousCredentials(), _http=http
    )
    args = {
        "client_scope_id": fixture.CLIENT_SCOPE_ID,
        "market_scope": fixture.row_fixture.MARKETS,
        "reviewed_metadata": fixture.table_fixture.source_metadata_bytes(),
        "delegate": client,
        "expected_creator_email": CREATOR,
    }
    args.update(overrides)
    return module().capture_production_snapshot(fixture.row_fixture.CUTOFF, **args)


def test_actual_sdk_capture_uses_five_native_selects_and_preserves_fallback_ambiguity(monkeypatch):
    http = HTTP()
    result = run(monkeypatch, http)
    assert result["query_count"] == 5
    assert result["total_bytes_billed"] == 500
    assert result["source_authority"] is False
    assert result["execution_authority"] is False
    material = result["assembly"]["row_material"]
    assert material["ambiguous_refs"] == [["za", "duplicate"]]
    assert material["unsupported_refs"] == [["za", "unsupported"]]
    assert material["missing_refs"] == [["za", "missing"]]
    posts = [data for method, _, data in http.calls if method == "POST"]
    assert len(posts) == 5
    for index, job in enumerate(posts):
        sql = job["configuration"]["query"]["query"]
        assert "open_intelligence_v3_source_20300102_" in sql
        assert "COUNT(*) OVER() AS capture_row_count" in sql
        assert "CREATE" not in sql
        assert "INSERT" not in sql
        assert (
            int(job["configuration"]["query"]["maximumBytesBilled"]) == 1_000_000_000 - index * 100
        )
    raw_params = posts[-1]["configuration"]["query"]["queryParameters"]
    ids = next(p["parameterValue"]["arrayValues"] for p in raw_params if p["name"] == "sample_ids")
    assert [v["value"] for v in ids] == ["missing", "raw"]
    assert len(result["query_receipts"]) == 5
    for receipt in result["query_receipts"]:
        assert receipt["creator_email"] == CREATOR
        assert receipt["execution_authority"] is False
        assert receipt["filtered_row_count"] == receipt["fetched_row_count"]
        assert len(receipt["result_digest"]) == 64
        assert all("capture_row_count" not in row for row in receipt["result_rows"])
        assert all("match_count" not in row for row in receipt["result_rows"])
    json.dumps(result)


@pytest.mark.parametrize(
    "mutation",
    [
        "creator",
        "jobid",
        "location",
        "sql",
        "billing",
        "boolean_billing",
        "overage",
        "status",
        "statement",
        "error",
        "creation",
    ],
)
def test_actual_sdk_job_mutations_refuse_without_next_query(monkeypatch, mutation):
    http = HTTP()

    def change(job):
        if mutation == "creator":
            job["user_email"] = "foreign@example.invalid"
        if mutation == "jobid":
            job["jobReference"]["jobId"] += "wrong"
        if mutation == "location":
            job["jobReference"]["location"] = "EU"
        if mutation == "sql":
            job["configuration"]["query"]["query"] += " altered"
        if mutation == "billing":
            job["statistics"]["query"].pop("totalBytesBilled")
        if mutation == "boolean_billing":
            job["statistics"]["query"]["totalBytesBilled"] = True
        if mutation == "overage":
            job["statistics"]["query"]["totalBytesBilled"] = "1000000001"
        if mutation == "status":
            job["status"]["state"] = "INVALID"
        if mutation == "statement":
            job["statistics"]["query"]["statementType"] = "INSERT"
        if mutation == "error":
            job["status"]["errorResult"] = {
                "reason": "accessDenied",
                "message": "synthetic private text",
            }
        if mutation == "creation":
            job["statistics"]["creationTime"] = "1"
        return job

    http.mutate_job = change
    with pytest.raises(ValueError, match="snapshot_capture") as error:
        run(monkeypatch, http)
    assert "private text" not in str(error.value)
    assert len([v for v in http.calls if v[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "mutation", ["truncated", "before_limit_count", "foreign_result", "schema", "missing_count"]
)
def test_actual_sdk_readback_mutations_cannot_claim_complete_capture(monkeypatch, mutation):
    http = HTTP()

    def change(body):
        if mutation == "truncated":
            body["totalRows"] = "2"
        if mutation == "before_limit_count":
            body["rows"][0]["f"][-1]["v"] = "999999"
        if mutation == "foreign_result":
            body["jobReference"] = {**body["jobReference"], "jobId": "foreign"}
        if mutation == "schema":
            next(field for field in body["schema"]["fields"] if field["name"] == "trend_date")[
                "type"
            ] = "STRING"
        if mutation == "missing_count":
            body["schema"]["fields"].pop()
            for row in body["rows"]:
                row["f"].pop()
        return body

    http.mutate_result = change
    with pytest.raises(ValueError, match="snapshot_capture"):
        run(monkeypatch, http)
    assert len([v for v in http.calls if v[0] == "POST"]) == 1


def test_empty_candidates_skip_evidence_queries_without_inventing_coverage(monkeypatch):
    http = HTTP()
    http.rows = {lane: [] for lane in http.rows}
    result = run(monkeypatch, http)
    assert result["query_count"] == 3
    assert result["assembly"]["row_material"]["requested_sample_keys"] == []
    assert result["assembly"]["collection_complete"] is False


def test_native_metadata_refusal_occurs_before_any_select(monkeypatch):
    http = HTTP()
    http.mutate_metadata = lambda row: {**row, "type": "TABLE"}
    with pytest.raises(ValueError, match="snapshot_capture"):
        run(monkeypatch, http)
    assert not any(method == "POST" for method, _, _ in http.calls)


def test_actual_sdk_running_job_completes_via_same_job_readback(monkeypatch):
    http = HTTP()
    http.running_first = True
    assert run(monkeypatch, http)["query_count"] == 5
    assert len([value for value in http.calls if value[0] == "POST"]) == 5


def test_actual_sdk_lost_insert_ack_never_reposts_or_continues(monkeypatch):
    http = HTTP()
    http.lose_ack = True
    with pytest.raises(ValueError, match="snapshot_capture") as error:
        run(monkeypatch, http)
    assert len([value for value in http.calls if value[0] == "POST"]) == 1
    assert len(error.value.query_receipts) == 1
    assert error.value.query_receipts[0]["status"] == "unresolved"
    assert error.value.query_receipts[0]["total_bytes_billed"] is None


def test_exact_byte_budget_exhaustion_retains_receipt_and_stops(monkeypatch):
    http = HTTP()

    def billed(job):
        job["statistics"]["query"]["totalBytesBilled"] = "1000000000"
        return job

    http.mutate_job = billed
    with pytest.raises(ValueError, match="snapshot_capture") as error:
        run(monkeypatch, http)
    assert len([value for value in http.calls if value[0] == "POST"]) == 1
    assert error.value.query_receipts[0]["total_bytes_billed"] == 1_000_000_000


def paginated_http():
    http = HTTP()
    http.paginate = True
    second = copy.deepcopy(http.rows["event_ledger"][0])
    second["ledger_id"] += "_second"
    http.rows["event_ledger"].append(second)
    http.inputs["snapshot_metadata"]["event_ledger"]["numRows"] = "2"
    return http


def test_actual_sdk_two_pages_preserve_both_rows_and_full_filtered_count(monkeypatch):
    http = paginated_http()
    result = run(monkeypatch, http)
    assert http.event_pages == [None, "synthetic-page-2"]
    receipt = result["query_receipts"][0]
    assert receipt["filtered_row_count"] == 2
    assert receipt["fetched_row_count"] == 2
    assert [row["ledger_id"] for row in receipt["result_rows"]] == [
        row["ledger_id"] for row in http.rows["event_ledger"]
    ]
    assert result["assembly"]["snapshot"]["row_counts_by_table"]["event_ledger"] == 2
    assert len([value for value in http.calls if value[0] == "POST"]) == 5


def test_actual_sdk_missing_second_page_preserves_unresolved_receipt_and_stops(monkeypatch):
    http = paginated_http()
    http.fail_next_page = True
    with pytest.raises(ValueError, match="snapshot_capture") as error:
        run(monkeypatch, http)
    assert http.event_pages == [None, "synthetic-page-2"]
    assert len([value for value in http.calls if value[0] == "POST"]) == 1
    assert len(error.value.query_receipts) == 1
    receipt = error.value.query_receipts[0]
    assert receipt["status"] == "unresolved"
    assert receipt["total_bytes_billed"] == 100
    assert receipt["native_job"]["jobReference"]["jobId"] == receipt["job_id"]
    assert "result_rows" not in receipt


def validate_capture(value, **changes):
    kwargs = {
        "cutoff_date": fixture.row_fixture.CUTOFF,
        "client_scope_id": fixture.CLIENT_SCOPE_ID,
        "market_scope": fixture.row_fixture.MARKETS,
        "reviewed_metadata": fixture.table_fixture.source_metadata_bytes(),
        "expected_creator_email": CREATOR,
    }
    kwargs.update(changes)
    return module().validate_captured_production_snapshot(value, **kwargs)


@pytest.mark.parametrize("boundary", ["collector", "retained"])
def test_unknown_native_schema_mode_is_refused(monkeypatch, boundary):
    http = HTTP()
    if boundary == "collector":

        def malformed_mode(body):
            body["schema"]["fields"][0]["mode"] = "INVALID_MODE"
            return body

        http.mutate_result = malformed_mode
        with pytest.raises(module().SnapshotCaptureError):
            run(monkeypatch, http)
        assert len([call for call in http.calls if call[0] == "POST"]) == 1
    else:
        captured = run(monkeypatch, http)
        captured["query_receipts"][0]["result_readback"]["schema"][0]["mode"] = "INVALID_MODE"
        with pytest.raises(ValueError, match="snapshot_capture_validation_failed"):
            validate_capture(captured)


@pytest.mark.parametrize("mode", ["full", "empty", "no_raw", "pages"])
def test_capture_validation_rebuilds_original_time_and_skipped_evidence(monkeypatch, mode):
    http = paginated_http() if mode == "pages" else HTTP()
    if mode == "empty":
        http.rows = {lane: [] for lane in http.rows}
    if mode == "no_raw":
        http.rows["seed_graph"][0]["sample_row_ids"] = ["enriched"]
    captured = json.loads(json.dumps(run(monkeypatch, http)))
    before = copy.deepcopy(captured)
    monkeypatch.setattr(
        module(), "_utc_now", lambda: pytest.fail("validation must not use current time")
    )
    validated = validate_capture(captured)
    assert validated == before
    assert validated is not captured
    validated["assembly"]["snapshot"]["rows_by_table"]["event_ledger"].clear()
    assert captured == before
    expected_lanes = list(fixture.subject.LANES)
    if mode == "empty":
        expected_lanes = expected_lanes[:3]
    if mode == "no_raw":
        expected_lanes = expected_lanes[:4]
    assert [receipt["lane"] for receipt in captured["query_receipts"]] == expected_lanes


@pytest.mark.parametrize(
    "mutation",
    [
        "rows",
        "rows_rehashed",
        "config_rehashed",
        "native_rehashed",
        "partial",
        "wrong_keys",
        "unknown_spend",
        "total_spend",
        "skipped_required",
        "extra_skipped",
        "count",
        "bool_count",
        "resealed_assembly",
        "wrong_digest_domain",
        "authority",
        "observed_after_capture",
        "ordinal",
    ],
)
def test_capture_validation_refuses_mutated_receipts_and_resealed_assembly(monkeypatch, mutation):
    captured = run(monkeypatch, HTTP())
    row = captured["query_receipts"][0]
    if mutation in {"rows", "rows_rehashed"}:
        row["result_rows"][0]["ledger_id"] += "_tampered"
        if mutation == "rows_rehashed":
            row["result_digest"] = canonical_digest(row["result_rows"])
    if mutation == "config_rehashed":
        row["requested_configuration"]["query"]["query"] += " altered"
        row["configuration_digest"] = canonical_digest(row["requested_configuration"])
    if mutation == "native_rehashed":
        row["native_job"]["user_email"] = "foreign@example.invalid"
        row["native_job_digest"] = canonical_digest(row["native_job"])
    if mutation == "partial":
        row["status"] = "unresolved"
    if mutation == "wrong_keys":
        row = captured["query_receipts"][3]
        parameters = row["requested_configuration"]["query"]["queryParameters"]
        ids = next(p for p in parameters if p["name"] == "sample_ids")
        ids["parameterValue"]["arrayValues"][0]["value"] = "unselected"
        row["native_job"]["configuration"] = copy.deepcopy(row["requested_configuration"])
        row["configuration_digest"] = canonical_digest(row["requested_configuration"])
        row["native_job_digest"] = canonical_digest(row["native_job"])
    if mutation == "unknown_spend":
        row["total_bytes_billed"] = None
    if mutation == "total_spend":
        captured["total_bytes_billed"] = 0
    if mutation == "skipped_required":
        captured["query_receipts"].pop()
        captured["query_count"] -= 1
        captured["total_bytes_billed"] -= 100
    if mutation == "extra_skipped":
        captured["query_receipts"].append(copy.deepcopy(row))
    if mutation == "count":
        row["filtered_row_count"] += 1
    if mutation == "bool_count":
        row["fetched_row_count"] = True
    if mutation == "resealed_assembly":
        physical = copy.deepcopy(captured["assembly"]["row_material"]["physical_rows_by_table"])
        physical["event_ledger"][0]["ledger_id"] += "_tampered"
        captured["assembly"] = fixture.build(rows_by_table=physical, coverage_receipt_refs=())
    if mutation == "wrong_digest_domain":
        row["result_digest"] = fixture.subject._digest(row["result_rows"])
        assert row["result_digest"] != canonical_digest(row["result_rows"])
    if mutation == "authority":
        captured["execution_authority"] = True
    if mutation == "observed_after_capture":
        row["observed_at"] = "2030-01-04T00:00:00+00:00"
    if mutation == "ordinal":
        row["job_id"] = row["job_id"][:-1] + "2"
    with pytest.raises(ValueError, match="snapshot_capture"):
        validate_capture(captured)


@pytest.mark.parametrize(
    "mutation", ["job", "incomplete", "total", "schema", "full_count", "match_count", "missing"]
)
def test_capture_validation_checks_independent_native_result_readback(monkeypatch, mutation):
    captured = run(monkeypatch, HTTP())
    readback = captured["query_receipts"][3]["result_readback"]
    if mutation == "job":
        readback["jobReference"]["jobId"] = "foreign"
    if mutation == "incomplete":
        readback["jobComplete"] = False
    if mutation == "total":
        readback["totalRows"] = "1"
    if mutation == "schema":
        readback["schema"][0]["type"] = "BOOL"
    if mutation == "full_count":
        readback["capture_row_counts"][0] += 1
    if mutation == "match_count":
        readback["match_counts"] = [1] * len(readback["match_counts"])
    if mutation == "missing":
        captured["query_receipts"][3].pop("result_readback")
    with pytest.raises(ValueError, match="snapshot_capture_validation_failed"):
        validate_capture(captured)


def test_capture_validation_rejects_a_receipted_but_unneeded_raw_read(monkeypatch):
    full = run(monkeypatch, HTTP())
    http = HTTP()
    http.rows["seed_graph"][0]["sample_row_ids"] = ["enriched"]
    selected = run(monkeypatch, http)
    extra = copy.deepcopy(full["query_receipts"][-1])
    capture_prefix = selected["query_receipts"][0]["job_id"].rsplit("_", 1)[0]
    extra["job_id"] = capture_prefix + "_5"
    extra["native_job"]["jobReference"]["jobId"] = extra["job_id"]
    extra["result_readback"]["jobReference"]["jobId"] = extra["job_id"]
    extra["native_job_digest"] = canonical_digest(extra["native_job"])
    selected["query_receipts"].append(extra)
    selected["query_count"] = 5
    selected["total_bytes_billed"] += extra["total_bytes_billed"]
    with pytest.raises(ValueError, match="snapshot_capture_validation_failed"):
        validate_capture(selected)


def test_capture_validation_rejects_native_jobs_before_snapshot_as_of(monkeypatch):
    captured = run(monkeypatch, HTTP())
    as_of = fixture.row_fixture.SOURCE_AS_OF
    for index, receipt in enumerate(captured["query_receipts"]):
        recorded = as_of - timedelta(seconds=30 - index * 2)
        receipt["recorded_at"] = recorded.isoformat(timespec="microseconds").replace("+00:00", "Z")
        receipt["observed_at"] = (recorded + timedelta(seconds=1)).isoformat()
        receipt["native_job"]["statistics"]["creationTime"] = str(int(recorded.timestamp() * 1000))
        receipt["native_job_digest"] = canonical_digest(receipt["native_job"])
    captured["assembly"] = fixture.build(
        captured_at=as_of + timedelta(seconds=30), coverage_receipt_refs=()
    )
    with pytest.raises(ValueError, match="snapshot_capture_validation_failed"):
        validate_capture(captured)


def test_capture_validation_reconstructs_once_with_nonempty_raw_fallback(monkeypatch):
    captured = run(monkeypatch, HTTP())
    assert captured["assembly"]["row_material"]["physical_rows_by_table"]["raw_content"]
    production = importlib.import_module("src.analysis.open_intelligence.production_snapshot")
    original = production.build_production_snapshot
    calls = []

    def counted(**kwargs):
        calls.append(True)
        return original(**kwargs)

    monkeypatch.setattr(production, "build_production_snapshot", counted)
    monkeypatch.setattr(module(), "build_production_snapshot", counted)
    assert validate_capture(captured) == captured
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mutation", ["reordered_rows", "boolean_scalar", "raw_ref", "missing_raw", "extra_receipt"]
)
def test_single_reconstruction_preserves_receipt_refutations(monkeypatch, mutation):
    captured = run(monkeypatch, paginated_http() if mutation == "reordered_rows" else HTTP())
    if mutation == "reordered_rows":
        row = captured["query_receipts"][0]
        row["result_rows"].reverse()
        row["result_digest"] = canonical_digest(row["result_rows"])
        assert validate_capture(captured) == captured
        return
    if mutation == "boolean_scalar":
        row = captured["query_receipts"][0]
        numeric = next(
            key for key, value in row["result_rows"][0].items() if type(value) in (int, float)
        )
        row["result_rows"][0][numeric] = True
        row["result_digest"] = canonical_digest(row["result_rows"])
    elif mutation == "raw_ref":
        row = captured["query_receipts"][-1]
        row["result_rows"][0]["id"] = "unrequested"
        row["result_digest"] = canonical_digest(row["result_rows"])
    elif mutation == "missing_raw":
        captured["query_receipts"].pop()
        captured["query_count"] -= 1
        captured["total_bytes_billed"] -= 100
    else:
        captured["query_receipts"].append(copy.deepcopy(captured["query_receipts"][-1]))
        captured["query_count"] += 1
    with pytest.raises(ValueError, match="snapshot_capture_validation_failed"):
        validate_capture(captured)
