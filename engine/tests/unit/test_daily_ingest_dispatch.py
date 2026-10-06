"""The ingest dispatch entry point: run the ingest job, read its receipt back by run ID.

The orchestration identity reads ``intelligence_42_sources_staging`` and never writes it,
so a collect step starts one execution of ``intelligence-42-ingest-staging`` with an empty
run request, reads that execution's collection receipt back from BigQuery by its execution
id, checks it against the caller's own build and digests, and records it in the object
ledger under the caller's attempt. The ingest identity writes the source dataset and its
receipt table; nothing here runs the producer in process.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest
from scripts.staging import collect_42_sources
from scripts.staging.collect_42_sources import entry_point_profile, reconciled_policy_digest
from src.analysis.open_intelligence import daily_native_clients as native
from src.analysis.open_intelligence import ingest_receipts
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_execution_authority import DailyAuthorityUnavailable
from src.analysis.open_intelligence.daily_native_transports import (
    CloudRunTransport,
    IngestRunRefused,
)
from src.analysis.open_intelligence.daily_stages import StageRefusal

from tests.unit.test_daily_native_clients import BUILD
from tests.unit.test_daily_stages import CUTOFF, collection_receipt
from tests.unit.test_daily_store import FakeObjectClient

ATTEMPT = "attempt:collect:1"
INGEST_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-ingest-staging"
INGEST_EXECUTION = "intelligence-42-ingest-staging-9k2xv"
OPERATION = "projects/ogilvy-trends-v2/locations/us-central1/operations/0f1e2d3c"
ENTRY_PROFILE = entry_point_profile()["profile_sha256"]
# The digest the ingest job stamps and its entry point checks: the reconciled estate.
RECONCILED = reconciled_policy_digest()
# What the daily profile and the 14 September recurring grant bind today.
DAILY_PROFILE_POLICY = "b219e607d9dc85336089815711469b5cc8bc28451af3868b8c58b67e56a533c8"
TREND_DATE = CUTOFF.date().isoformat()

OPERATION = "projects/ogilvy-trends-v2/locations/us-central1/operations/0f1e2d3c"
ENTRY_PROFILE = entry_point_profile()["profile_sha256"]
TREND_DATE = CUTOFF.date().isoformat()


def ingest_receipt(**overrides):
    values = {
        "execution_id": INGEST_EXECUTION,
        "policy_sha256": RECONCILED,
        "profile_sha256": ENTRY_PROFILE,
        "authority_kind": "verified_manifest",
    }
    values.update(overrides)
    return collection_receipt(**values)


def daily_receipt():
    receipt = ingest_receipt()
    receipt.pop("authority_kind")
    receipt["execution_id"] = ATTEMPT
    return receipt


def row(receipt, *, execution=INGEST_EXECUTION, trend_date=TREND_DATE, digest=None):
    payload = canonical_bytes(receipt).decode("utf-8")
    return {
        "execution_id": execution,
        "trend_date": trend_date,
        "receipt_json": payload,
        "receipt_sha256": digest or hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def operation(execution=INGEST_EXECUTION):
    return {"name": OPERATION, "metadata": {"name": f"{INGEST_JOB}/executions/{execution}"}}


class FakeRuns:
    def __init__(self, response=None, error=None):
        self.response = operation() if response is None else response
        self.error = error
        self.calls = 0

    def run_ingest_job(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class FakeReceipts:
    """The ingest receipt table read side; the row appears after ``appear_after`` reads.

    ``errors`` are raised, one per read, before any row is answered.
    """

    def __init__(self, rows=(), *, appear_after=1, errors=()):
        self.rows = [dict(item) for item in rows]
        self.appear_after = appear_after
        self.errors = list(errors)
        self.reads: list[str] = []

    def by_execution(self, execution_id):
        self.reads.append(execution_id)
        if self.errors:
            raise self.errors.pop(0)
        if len(self.reads) < self.appear_after:
            return []
        return [dict(item) for item in self.rows if item["execution_id"] == execution_id]


class Fixture:
    def __init__(
        self, monkeypatch, *, runs=None, receipts=None, polls=3, build=BUILD, profile=None
    ):
        monkeypatch.setenv("TRENDS_ENV", "staging")
        monkeypatch.setenv("BIGQUERY_DATASET", "intelligence_42_sources_staging")
        for variable in (
            "COLLECTION_POLICY_SHA256",
            "COLLECTION_PROFILE_SHA256",
            "COLLECTION_SOURCE_SHA",
            "COLLECTION_IMAGE_URI",
        ):
            monkeypatch.delenv(variable, raising=False)
        producer = importlib.import_module("scripts.run_rss_now")

        def forbidden(**_kwargs):
            raise AssertionError("the producer must not run in the orchestration process")

        monkeypatch.setattr(producer, "_run_impl", forbidden)
        self.runs = FakeRuns() if runs is None else runs
        self.receipts = (
            FakeReceipts([row(ingest_receipt())], appear_after=2) if receipts is None else receipts
        )
        self.sleeps: list[float] = []
        self.object_client = FakeObjectClient()
        self.ledger = native.ObjectCollectionLedger(self.object_client)
        self.dispatch = native.IngestDispatch(
            runs=self.runs,
            receipts=self.receipts,
            polls=polls,
            interval=30,
            sleep=self.sleeps.append,
        )
        self.collector = native.ingest_collector(
            self.ledger,
            dispatch=self.dispatch,
            profile=native.ingest_collection_profile(RECONCILED) if profile is None else profile,
            build=build,
        )

    def collect(self, attempt=ATTEMPT):
        return self.collector(stop_after_ingestion=True, attempt_id=attempt)


def refused(fixture, code, *, retry_safe, execution_id=None):
    with pytest.raises(StageRefusal, match=rf"^{code}$") as caught:
        fixture.collect()
    assert caught.value.retry_safe is retry_safe
    assert getattr(caught.value, "execution_id", None) == execution_id
    return caught.value


def test_the_dispatch_runs_the_ingest_job_and_keeps_the_receipt(monkeypatch):
    fixture = Fixture(monkeypatch)
    assert fixture.collect() == daily_receipt()
    assert fixture.runs.calls == 1
    assert fixture.receipts.reads == [INGEST_EXECUTION, INGEST_EXECUTION]
    assert fixture.sleeps == [30]
    assert fixture.ledger.operation(ATTEMPT) == daily_receipt()
    assert fixture.ledger.market_day(TREND_DATE) == [daily_receipt()]


def test_the_ledger_copy_is_the_admitted_receipt_under_the_attempt():
    admitted = ingest_receipt()
    before = dict(admitted)
    assert ingest_receipts.daily_ledger_copy(admitted, attempt_id=ATTEMPT) == daily_receipt()
    assert admitted == before


@pytest.mark.parametrize("attempt", [None, "", "  ", 7])
def test_the_ledger_copy_refuses_an_attempt_that_names_nothing(attempt):
    with pytest.raises(ValueError, match=r"^collection_attempt_invalid$"):
        ingest_receipts.daily_ledger_copy(ingest_receipt(), attempt_id=attempt)


def test_the_source_digest_is_the_digest_of_the_canonical_ledger_copy():
    assert (
        ingest_receipts.receipt_source_digest(daily_receipt())
        == hashlib.sha256(canonical_bytes(daily_receipt())).hexdigest()
    )


def test_the_collector_records_the_shared_ledger_copy(monkeypatch):
    calls = []
    shared = ingest_receipts.daily_ledger_copy

    def spy(receipt, *, attempt_id):
        calls.append(attempt_id)
        return shared(receipt, attempt_id=attempt_id)

    monkeypatch.setattr(native, "daily_ledger_copy", spy)
    fixture = Fixture(monkeypatch)
    assert fixture.collect() == daily_receipt()
    assert calls == [ATTEMPT]


def test_the_collection_profile_needs_no_variable_of_the_calling_process(monkeypatch):
    fixture = Fixture(monkeypatch)
    profile = native.ingest_collection_profile(RECONCILED)
    assert profile["policy_sha256"] == RECONCILED
    assert profile["profile_sha256"] == ENTRY_PROFILE
    assert profile["source_dataset"] == "intelligence_42_sources_staging"
    assert fixture.collector.pre_dispatch_refusal() is None


def test_the_stage_client_factory_carries_no_ingest_wiring():
    import inspect

    assert "ingest" not in inspect.signature(native.build_native_stage_clients).parameters


@pytest.mark.parametrize(
    "stored",
    [
        row(ingest_receipt(source_sha="c" * 40)),
        row(ingest_receipt(image_uri=BUILD["image_uri"].replace("b" * 64, "d" * 64))),
        row(ingest_receipt(policy_sha256="1" * 64)),
        row(ingest_receipt(profile_sha256="2" * 64)),
        row(ingest_receipt(authority_kind="unverified")),
        row(collection_receipt(execution_id=INGEST_EXECUTION, profile_sha256=ENTRY_PROFILE)),
        row(ingest_receipt(execution_id="intelligence-42-ingest-staging-zz9zz")),
        row(ingest_receipt(), trend_date="2026-09-01"),
        row(ingest_receipt(), digest="0" * 64),
        dict(row(ingest_receipt()), receipt_json=json.dumps(ingest_receipt(), indent=1)),
        row(ingest_receipt(market_states={"za": "collected"})),
    ],
)
def test_an_ingest_receipt_that_is_not_the_callers_record_is_refused(monkeypatch, stored):
    fixture = Fixture(monkeypatch, receipts=FakeReceipts([stored]))
    refused(
        fixture,
        "collection_ingest_receipt_mismatch",
        retry_safe=False,
        execution_id=INGEST_EXECUTION,
    )
    assert fixture.ledger.operation(ATTEMPT) is None
    assert fixture.object_client.writes == []


def test_two_rows_for_one_execution_are_refused(monkeypatch):
    fixture = Fixture(monkeypatch, receipts=FakeReceipts([row(ingest_receipt())] * 2))
    refused(
        fixture,
        "collection_ingest_receipt_mismatch",
        retry_safe=False,
        execution_id=INGEST_EXECUTION,
    )


def test_a_receipt_that_never_arrives_raises_pending_after_the_budget(monkeypatch):
    fixture = Fixture(monkeypatch, receipts=FakeReceipts([]), polls=3)
    with pytest.raises(native.IngestReceiptPending, match=rf"^{INGEST_EXECUTION}$") as caught:
        fixture.collect()
    assert caught.value.execution_id == INGEST_EXECUTION
    assert caught.value.retry_safe is False
    assert fixture.sleeps == [30, 30]
    assert fixture.receipts.reads == [INGEST_EXECUTION] * 3
    assert fixture.object_client.writes == []


@pytest.mark.parametrize(
    "response",
    [
        {"name": OPERATION},
        {"name": OPERATION, "metadata": {}},
        {"name": "operations/elsewhere", "metadata": operation()["metadata"]},
        operation(execution="intelligence-42-daily-staging-9k2xv"),
        operation(execution="intelligence-42-ingest-staging-9k2xv/../x"),
        {
            "name": OPERATION,
            "metadata": {"name": INGEST_JOB.replace("ingest", "daily") + "/executions/a1b2c"},
        },
        [],
    ],
)
def test_a_run_answer_that_names_no_ingest_execution_is_not_polled(monkeypatch, response):
    fixture = Fixture(monkeypatch, runs=FakeRuns(response=response))
    with pytest.raises(ValueError, match=r"^collection_ingest_operation_invalid$"):
        fixture.collect()
    assert fixture.receipts.reads == []
    assert fixture.object_client.writes == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PermissionError("daily_native_forbidden"), "collection_ingest_dispatch_forbidden"),
        (IngestRunRefused(404), "collection_ingest_dispatch_refused"),
        (IngestRunRefused(429), "collection_ingest_dispatch_refused"),
    ],
)
def test_a_run_request_refused_before_any_execution_is_retry_safe(monkeypatch, error, code):
    fixture = Fixture(monkeypatch, runs=FakeRuns(error=error))
    refused(fixture, code, retry_safe=True)
    assert fixture.receipts.reads == []


def test_a_run_request_whose_outcome_is_unknown_is_not_refused_as_retry_safe(monkeypatch):
    fixture = Fixture(monkeypatch, runs=FakeRuns(error=DailyAuthorityUnavailable("x")))
    with pytest.raises(DailyAuthorityUnavailable):
        fixture.collect()
    assert fixture.receipts.reads == []


@pytest.mark.parametrize(
    ("policy", "profile_digest", "code"),
    [
        (DAILY_PROFILE_POLICY, ENTRY_PROFILE, "collection_ingest_policy_unstamped"),
        (RECONCILED, "6" * 64, "collection_ingest_profile_unstamped"),
    ],
)
def test_a_profile_the_ingest_job_does_not_stamp_refuses_before_any_run(
    monkeypatch, policy, profile_digest, code
):
    profile = native.collection_profile(policy_sha256=policy, profile_sha256=profile_digest)
    fixture = Fixture(monkeypatch, profile=profile)
    assert fixture.collector.pre_dispatch_refusal() == code
    refused(fixture, code, retry_safe=True)
    assert fixture.runs.calls == 0
    assert fixture.receipts.reads == []


def test_the_real_daily_profile_digest_refuses_and_the_stamped_one_proceeds(monkeypatch):
    daily = Fixture(monkeypatch, profile=native.ingest_collection_profile(DAILY_PROFILE_POLICY))
    refused(daily, "collection_ingest_policy_unstamped", retry_safe=True)
    assert daily.runs.calls == 0
    stamped = Fixture(monkeypatch, profile=native.ingest_collection_profile(RECONCILED))
    assert stamped.collector.pre_dispatch_refusal() is None
    assert stamped.collect() == daily_receipt()
    assert stamped.runs.calls == 1


def test_a_receipt_not_yet_readable_is_read_again(monkeypatch):
    receipts = FakeReceipts(
        [row(ingest_receipt())],
        errors=[ingest_receipts.ReceiptNotYetReadable("unavailable")],
    )
    fixture = Fixture(monkeypatch, receipts=receipts)
    assert fixture.collect() == daily_receipt()
    assert fixture.receipts.reads == [INGEST_EXECUTION, INGEST_EXECUTION]


def test_a_read_that_raises_anything_else_propagates_with_its_execution(monkeypatch):
    receipts = FakeReceipts([row(ingest_receipt())], errors=[KeyError("receipt_json")])
    fixture = Fixture(monkeypatch, receipts=receipts)
    with pytest.raises(KeyError) as caught:
        fixture.collect()
    assert caught.value.execution_id == INGEST_EXECUTION
    assert fixture.receipts.reads == [INGEST_EXECUTION]
    assert fixture.object_client.writes == []


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda m: None, "collection_build_unbound"),
        (lambda m: m.setenv("TRENDS_ENV", "prod"), "collection_target_not_staging"),
        (lambda m: m.setenv("BIGQUERY_DATASET", "trends_v2"), "collection_target_not_staging"),
    ],
)
def test_refusals_before_the_ingest_job_is_run(monkeypatch, setup, code):
    fixture = Fixture(monkeypatch, build=None if code == "collection_build_unbound" else BUILD)
    setup(monkeypatch)
    assert fixture.collector.pre_dispatch_refusal() == code
    refused(fixture, code, retry_safe=True)
    assert fixture.runs.calls == 0


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"attempt_id": ""}, "collection_attempt_invalid"),
        ({"attempt_id": 7}, "collection_attempt_invalid"),
        ({"attempt_id": ATTEMPT, "stop_after_ingestion": False}, "collection_mode_invalid"),
    ],
)
def test_the_request_shape_is_closed(monkeypatch, kwargs, code):
    fixture = Fixture(monkeypatch)
    with pytest.raises(StageRefusal, match=rf"^{code}$") as caught:
        fixture.collector(**{"stop_after_ingestion": True, **kwargs})
    assert caught.value.retry_safe is True
    assert fixture.runs.calls == 0


def test_the_poll_budget_is_a_positive_count_and_interval():
    for polls, interval in ((0, 30), (3, 0), (True, 30), (3, -1)):
        with pytest.raises(ValueError, match=r"^collection_ingest_poll_budget_invalid$"):
            native.IngestDispatch(
                runs=FakeRuns(), receipts=FakeReceipts(), polls=polls, interval=interval
            )


class Session:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = operation() if body is None else body
        self.posts: list[tuple[str, object]] = []

    def post(self, url, *, json, timeout):
        self.posts.append((url, json))
        return SimpleNamespace(status_code=self.status, json=lambda: self.body)


def test_the_transport_runs_the_ingest_job_with_an_empty_request():
    session = Session()
    assert CloudRunTransport(session).run_ingest_job() == operation()
    assert session.posts == [(f"https://run.googleapis.com/v2/{INGEST_JOB}:run", {})]
    with pytest.raises(PermissionError):
        CloudRunTransport(Session(status=403)).run_ingest_job()
    for status in (400, 404, 409, 429):
        with pytest.raises(IngestRunRefused) as caught:
            CloudRunTransport(Session(status=status)).run_ingest_job()
        assert caught.value.status == status
    for status in (500, 503):
        with pytest.raises(DailyAuthorityUnavailable):
            CloudRunTransport(Session(status=status)).run_ingest_job()


def test_the_native_dispatch_is_built_without_a_network_call(monkeypatch):
    built = []
    monkeypatch.setattr(native, "_ingest_session", lambda: built.append(1) or Session())
    dispatch = native.native_ingest_dispatch()
    assert isinstance(dispatch, native.IngestDispatch)
    assert isinstance(dispatch.receipts, ingest_receipts.BigQueryReceiptTable)
    assert built == []
    assert dispatch.runs.run_ingest_job() == operation()
    assert built == [1]


class FakeTable:
    def __init__(self):
        self.rows: list[dict] = []

    def insert_once(self, *, execution_id, trend_date, receipt_json, receipt_sha256):
        if not any(item["execution_id"] == execution_id for item in self.rows):
            self.rows.append(
                {
                    "execution_id": execution_id,
                    "trend_date": trend_date,
                    "receipt_json": receipt_json,
                    "receipt_sha256": receipt_sha256,
                }
            )

    def by_execution(self, execution_id):
        return [dict(item) for item in self.rows if item["execution_id"] == execution_id]

    def by_day(self, trend_date):
        return [dict(item) for item in self.rows if item["trend_date"] == trend_date]


def test_the_ingest_ledger_answers_the_producer_ledger_contract():
    table = FakeTable()
    ledger = ingest_receipts.WarehouseCollectionLedger(table)
    assert ledger.operation(INGEST_EXECUTION) is None
    assert ledger.market_day(TREND_DATE) == []
    ledger.record(ingest_receipt(), trend_date=TREND_DATE)
    ledger.record(ingest_receipt(), trend_date=TREND_DATE)
    assert ledger.operation(INGEST_EXECUTION) == ingest_receipt()
    assert ledger.market_day(TREND_DATE) == [ingest_receipt()]
    assert table.rows == [row(ingest_receipt())]
    with pytest.raises(ValueError, match=r"^collection_receipt_conflict$"):
        ledger.record(ingest_receipt(raw_rows_persisted=11), trend_date=TREND_DATE)
    with pytest.raises(ValueError, match=r"^collection_receipt_invalid$"):
        ledger.record(ingest_receipt(execution_id=ATTEMPT), trend_date=TREND_DATE)
    with pytest.raises(ValueError, match=r"^collection_operation_invalid$"):
        ledger.operation("intelligence-42-daily-staging-9k2xv")
    with pytest.raises(ValueError, match=r"^collection_day_invalid$"):
        ledger.record(ingest_receipt(), trend_date="10 September")


def test_the_ingest_ledger_refuses_a_stored_row_it_cannot_read_back():
    table = FakeTable()
    table.rows.append(row(ingest_receipt(), digest="0" * 64))
    ledger = ingest_receipts.WarehouseCollectionLedger(table)
    with pytest.raises(ValueError, match=r"^collection_receipt_invalid$"):
        ledger.operation(INGEST_EXECUTION)
    with pytest.raises(ValueError, match=r"^collection_receipt_invalid$"):
        ledger.market_day(TREND_DATE)


class RecordingClient:
    def __init__(self, rows=(), *, error=None):
        self.rows = [dict(item) for item in rows]
        self.error = error
        self.calls: list[tuple[str, list]] = []
        self.timeouts: list[object] = []
        self.result_timeouts: list[object] = []

    def query(self, sql, job_config=None, **kwargs):
        parameters = [
            (item.name, item.type_, item.value)
            for item in getattr(job_config, "query_parameters", None) or ()
        ]
        self.calls.append((sql, parameters))
        rows = [dict(item) for item in self.rows]
        if self.error is not None:
            raise self.error
        self.timeouts.append(kwargs.get("timeout"))

        def result(timeout=None):
            self.result_timeouts.append(timeout)
            return iter(rows)

        return SimpleNamespace(result=result)


def test_the_receipt_table_binds_every_value_as_a_query_parameter():
    client = RecordingClient(rows=[row(ingest_receipt())])
    table = ingest_receipts.BigQueryReceiptTable(client)
    payload = canonical_bytes(ingest_receipt()).decode("utf-8")
    table.insert_once(
        execution_id=INGEST_EXECUTION,
        trend_date=TREND_DATE,
        receipt_json=payload,
        receipt_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    assert table.by_execution(INGEST_EXECUTION) == [row(ingest_receipt())]
    assert table.by_day(TREND_DATE) == [row(ingest_receipt())]
    create, insert, by_execution, by_day = client.calls
    qualified = "`ogilvy-trends-v2.intelligence_42_sources_staging.collection_receipts`"
    assert create[0].startswith(f"CREATE TABLE IF NOT EXISTS {qualified}")
    assert create[1] == []
    assert insert[0].startswith(f"INSERT INTO {qualified}")
    assert "NOT EXISTS" in insert[0]
    assert {name for name, _type, _value in insert[1]} == {
        "execution_id",
        "trend_date",
        "receipt_json",
        "receipt_sha256",
    }
    for sql, _parameters in client.calls:
        assert INGEST_EXECUTION not in sql
        assert TREND_DATE not in sql
    assert by_execution[1] == [("execution_id", "STRING", INGEST_EXECUTION)]
    assert by_day[1] == [("trend_date", "STRING", TREND_DATE)]
    table.insert_once(
        execution_id=INGEST_EXECUTION,
        trend_date=TREND_DATE,
        receipt_json=payload,
        receipt_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    assert sum(sql.startswith("CREATE TABLE") for sql, _ in client.calls) == 1
    reader = RecordingClient(rows=[row(ingest_receipt())])
    ingest_receipts.BigQueryReceiptTable(reader).by_execution(INGEST_EXECUTION)
    assert [sql.split(" ")[0] for sql, _ in reader.calls] == ["SELECT"]
    writer = RecordingClient()
    ingest_receipts.BigQueryReceiptTable(writer, create=True).by_day(TREND_DATE)
    assert [sql.split(" ")[0] for sql, _ in writer.calls] == ["CREATE", "SELECT"]


def test_the_entry_point_binds_the_ingest_ledger(monkeypatch):
    ledger = object()
    seen = []
    monkeypatch.setattr(ingest_receipts, "native_collection_ledger", lambda: ledger)
    monkeypatch.setattr(collect_42_sources, "main", lambda **kwargs: seen.append(kwargs) or 0)
    assert collect_42_sources.entry() == 0
    assert seen == [{"receipt_ledger": ledger}]
    source = collect_42_sources.__file__
    with open(source, encoding="utf-8") as stream:
        assert stream.read().rstrip().endswith("raise SystemExit(entry())")


def test_the_poll_budget_outlasts_the_ingest_job_it_waits_for():
    sleeps = (native.INGEST_POLLS - 1) * native.INGEST_POLL_INTERVAL_SECONDS
    assert sleeps >= 2700 + 120
    total = (
        sleeps
        + native.INGEST_POLLS * ingest_receipts.READ_TIMEOUT_SECONDS
        + native.INGEST_RUN_TIMEOUT_SECONDS
    )
    assert total == native.INGEST_TIME_BUDGET_SECONDS
    assert total < 3600


def test_every_receipt_read_is_bounded_by_the_read_timeout():
    client = RecordingClient(rows=[row(ingest_receipt())])
    ingest_receipts.BigQueryReceiptTable(client).by_execution(INGEST_EXECUTION)
    assert client.timeouts == [ingest_receipts.READ_TIMEOUT_SECONDS]
    assert client.result_timeouts == [ingest_receipts.READ_TIMEOUT_SECONDS]


def test_an_absent_table_reads_as_no_receipt_and_an_unavailable_one_as_not_yet_readable():
    from google.api_core import exceptions

    absent = RecordingClient(error=exceptions.NotFound("collection_receipts"))
    assert ingest_receipts.BigQueryReceiptTable(absent).by_execution(INGEST_EXECUTION) == []
    for error in (
        exceptions.ServiceUnavailable("x"),
        exceptions.InternalServerError("x"),
        exceptions.TooManyRequests("x"),
        exceptions.GatewayTimeout("x"),
        TimeoutError("x"),
    ):
        busy = RecordingClient(error=error)
        with pytest.raises(ingest_receipts.ReceiptNotYetReadable):
            ingest_receipts.BigQueryReceiptTable(busy).by_execution(INGEST_EXECUTION)
    broken = RecordingClient(error=exceptions.BadRequest("syntax"))
    with pytest.raises(exceptions.BadRequest):
        ingest_receipts.BigQueryReceiptTable(broken).by_execution(INGEST_EXECUTION)
