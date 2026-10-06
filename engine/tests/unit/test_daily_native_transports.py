from datetime import UTC, datetime
from hashlib import sha256

import pytest
from google.api_core import exceptions
from src.analysis.open_intelligence import daily_native_transports as subject
from src.analysis.open_intelligence.daily_execution_authority import (
    DailyAuthorityBudget,
    DailyAuthorityIntegrityError,
    DailyAuthorityUnavailable,
    NativeDailyAuthorityWriteAdapter,
)

from tests.unit.test_daily_child_execution import (
    EXECUTION,
    execution_resource,
    job_resource,
)
from tests.unit.test_daily_derivation_authority import DAILY_JOB

DERIVATION = "exd_" + "c" * 64

# BigQuery routine calls


class Job:
    def __init__(self, rows, billed):
        self.rows = rows
        self.total_bytes_billed = billed

    def result(self):
        return iter(self.rows)


class Row(dict):
    pass


class BigQuery:
    def __init__(self, rows, billed=10485760):
        self.rows = rows
        self.billed = billed
        self.queries = []

    def query(self, sql, *, job_config):
        self.queries.append((sql, job_config))
        return Job(self.rows, self.billed)


def routines(client, **changes):
    arguments = {
        "client": client,
        "project": "ogilvy-trends-v2",
        "dataset": "trends_v2_staging_approvals",
        "maximum_bytes_billed": 1073741824,
    }
    arguments.update(changes)
    return subject.BigQueryRoutineTransport(**arguments)


def test_a_routine_call_binds_every_parameter_as_a_string_and_caps_billing():
    client = BigQuery([Row(derivation={"derivation_id": DERIVATION}, lifecycle_state="derived")])
    row, billed = routines(client).call(
        "sp_read_open_intelligence_daily_derivation_v1", (DERIVATION,)
    )
    assert row == {"derivation": {"derivation_id": DERIVATION}, "lifecycle_state": "derived"}
    assert billed == 10485760
    ((sql, config),) = client.queries
    assert sql == (
        "CALL `ogilvy-trends-v2.trends_v2_staging_approvals."
        "sp_read_open_intelligence_daily_derivation_v1`(@p0)"
    )
    assert config.maximum_bytes_billed == 1073741824
    assert config.use_legacy_sql is False
    ((parameter),) = config.query_parameters
    assert (parameter.name, parameter.type_, parameter.value) == ("p0", "STRING", DERIVATION)


def test_every_daily_routine_is_callable_and_nothing_else():
    assert set(NativeDailyAuthorityWriteAdapter._ROUTINES.values()) <= subject.DAILY_ROUTINES
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_routine_forbidden$"):
        routines(BigQuery([Row()])).call("sp_approve_open_intelligence_recurring_grant_v2", ())


@pytest.mark.parametrize("rows", [[], [Row(a=1), Row(a=2)]])
def test_a_routine_must_return_exactly_one_row(rows):
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_routine_rows_invalid$"):
        routines(BigQuery(rows)).call(
            "sp_read_open_intelligence_daily_derivation_v1", (DERIVATION,)
        )


def test_non_string_parameters_refuse_before_any_query():
    client = BigQuery([Row()])
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_routine_parameter_invalid$"):
        routines(client).call("sp_read_open_intelligence_daily_derivation_v1", (1,))
    assert client.queries == []


def test_an_uncapped_transport_refuses():
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_routine_budget_invalid$"):
        routines(BigQuery([]), maximum_bytes_billed=0)


def test_unmetered_billing_reaches_the_budget_as_unavailable():
    budget = DailyAuthorityBudget(
        queries=1, billed_bytes=2**30, object_reads=0, object_bytes=0, native_reads=0
    )
    adapter = NativeDailyAuthorityWriteAdapter(
        routines=routines(BigQuery([Row(ok=True)], billed=None)),
        objects=None,
        native=None,
        budget=budget,
    )
    with pytest.raises(DailyAuthorityUnavailable, match=r"^daily_query_metering_unavailable$"):
        adapter.select_derivation(DAILY_JOB, "i", "g", "p")


# observation objects


class Blob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.generation = None
        self.size = None
        self.time_created = None

    def upload_from_string(self, body, *, content_type, if_generation_match, retry):
        assert content_type == "application/json"
        assert retry is None
        if if_generation_match != 0 or self.name in self.bucket.stored:
            raise exceptions.PreconditionFailed(self.name)
        self.bucket.stored[self.name] = body

    def download_as_bytes(self, *, if_generation_match, retry):
        assert if_generation_match == self.generation
        return self.bucket.stored[self.name]


class Bucket:
    name = "ogilvy-trends-v2-execution-approvals-staging"

    def __init__(self):
        self.stored = {}

    def blob(self, name):
        return Blob(self, name)

    def get_blob(self, name, *, retry):
        if name not in self.stored:
            return None
        blob = Blob(self, name)
        blob.generation = 7
        blob.size = len(self.stored[name])
        blob.time_created = datetime(2026, 9, 23, 6, tzinfo=UTC)
        return blob


def test_observation_objects_write_once_and_read_back_their_storage_facts():
    bucket = Bucket()
    objects = subject.GcsObservationObjects(bucket)
    name = "42/daily/execution-observations/" + "a" * 64 + ".json"
    objects.write_once(name, b"{}")
    with pytest.raises(FileExistsError):
        objects.write_once(name, b"{}")
    raw, metadata = objects.read(name, 1024)
    assert raw == b"{}"
    assert metadata == {
        "content_sha256": sha256(b"{}").hexdigest(),
        "created_at": "2026-09-23T06:00:00+00:00",
        "generation": "7",
        "object_name": name,
        "size_bytes": 2,
    }


def test_observation_objects_refuse_other_prefixes_other_buckets_and_oversize_reads():
    bucket = Bucket()
    objects = subject.GcsObservationObjects(bucket)
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_object_name_forbidden$"):
        objects.write_once("42/daily/slots/x/control.json", b"{}")
    name = "42/daily/execution-observations/" + "b" * 64 + ".json"
    objects.write_once(name, b"{}")
    with pytest.raises(DailyAuthorityUnavailable, match=r"^daily_object_budget_exhausted$"):
        objects.read(name, 1)
    with pytest.raises(FileNotFoundError):
        objects.read("42/daily/execution-observations/" + "c" * 64 + ".json", 1024)
    bucket.name = "other"
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_object_bucket_mismatch$"):
        subject.GcsObservationObjects(bucket)


def test_the_rates_reader_reads_only_the_rates_object_with_its_provider_storage_time():
    bucket = Bucket()
    bucket.stored["42/daily/workload-rates/current.json"] = b"{}"
    bucket.stored["42/daily/execution-observations/" + "a" * 64 + ".json"] = b"{}"
    rates = subject.GcsWorkloadRates(bucket)
    raw, metadata = rates.read("42/daily/workload-rates/current.json", 1024)
    assert raw == b"{}"
    assert metadata["created_at"] == "2026-09-23T06:00:00+00:00"
    assert metadata["object_name"] == "42/daily/workload-rates/current.json"
    for name in (
        "42/daily/execution-observations/" + "a" * 64 + ".json",
        "42/daily/workload-rates/other.json",
    ):
        with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_object_name_forbidden$"):
            rates.read(name, 1024)
    assert not hasattr(rates, "write_once")
    bucket.name = "other"
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_object_bucket_mismatch$"):
        subject.GcsWorkloadRates(bucket)


@pytest.mark.parametrize("time_created", [datetime(2026, 9, 23, 6), None, "2026-09-23T06:00:00Z"])
@pytest.mark.parametrize(
    ("reader", "name"),
    [
        (subject.GcsObservationObjects, "42/daily/execution-observations/" + "a" * 64 + ".json"),
        (subject.GcsWorkloadRates, "42/daily/workload-rates/current.json"),
    ],
)
def test_a_storage_time_without_a_zone_refuses_with_a_code(reader, name, time_created):
    """The provider storage time bounds the record's own times; naive or missing refuses."""
    bucket = Bucket()
    bucket.stored[name] = b"{}"
    get_blob = bucket.get_blob

    def unzoned(object_name, *, retry):
        blob = get_blob(object_name, retry=retry)
        blob.time_created = time_created
        return blob

    bucket.get_blob = unzoned
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_object_storage_time_invalid$"):
        reader(bucket).read(name, 1024)


# Cloud Run


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append(("GET", url, None))
        return self.responses[url]

    def post(self, url, *, json, timeout):
        self.calls.append(("POST", url, json))
        return self.responses[url]


BASE = "https://run.googleapis.com/v2/"


def test_cloud_run_reads_map_to_the_native_views():
    execution = execution_resource()
    session = Session(
        {
            BASE + EXECUTION: Response(200, execution),
            BASE + DAILY_JOB: Response(200, job_resource()),
        }
    )
    run = subject.CloudRunTransport(session)
    assert run.read(EXECUTION)["execution_name"] == EXECUTION
    assert run.read_job(DAILY_JOB)["job_resource"] == DAILY_JOB
    assert run.read_execution(EXECUTION) == execution
    assert run.read_raw_job(DAILY_JOB) == job_resource()
    assert [call[1] for call in session.calls] == [BASE + EXECUTION, BASE + DAILY_JOB] * 2


def test_cloud_run_runs_only_the_daily_job_with_only_the_step_number_override():
    operation = {"name": "projects/ogilvy-trends-v2/locations/us-central1/operations/op-1"}
    session = Session({BASE + DAILY_JOB + ":run": Response(200, operation)})
    run = subject.CloudRunTransport(session)
    body = {
        "overrides": {
            "containerOverrides": [{"env": [{"name": "DAILY_STEP_NUMBER", "value": "6"}]}]
        }
    }
    assert run.run_job(DAILY_JOB, body) == operation
    assert session.calls == [("POST", BASE + DAILY_JOB + ":run", body)]
    for forbidden in (
        {"overrides": {"taskCount": 2}},
        dict(body, validateOnly=False),
        {"overrides": {"containerOverrides": [{"env": [{"name": "X", "value": "6"}]}]}},
        {
            "overrides": {
                "containerOverrides": [{"env": [{"name": "DAILY_STEP_NUMBER", "value": "7"}]}]
            }
        },
        {
            "overrides": {
                "containerOverrides": [
                    {"env": [{"name": "DAILY_DERIVATION_ID", "value": DERIVATION}]}
                ]
            }
        },
    ):
        with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_run_request_forbidden$"):
            run.run_job(DAILY_JOB, forbidden)
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_native_name_forbidden$"):
        run.run_job(DAILY_JOB.replace("daily-staging", "price-policy-staging"), body)


def test_cloud_run_refuses_names_outside_the_daily_job_and_maps_denials():
    session = Session({BASE + EXECUTION: Response(403, {})})
    run = subject.CloudRunTransport(session)
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_native_name_forbidden$"):
        run.read("projects/ogilvy-trends-v2/locations/us-central1/jobs/other/executions/e")
    with pytest.raises(PermissionError):
        run.read_execution(EXECUTION)
    assert session.calls == [("GET", BASE + EXECUTION, None)]


def test_provider_operation_reads_are_left_to_the_reconciliation_lane():
    with pytest.raises(DailyAuthorityUnavailable, match=r"^daily_provider_operation_unmapped$"):
        subject.CloudRunTransport(Session({})).read_operation("operations/op-1")


@pytest.mark.parametrize(
    "execution_id",
    ["child1:run", "child1?alt=media", "child1#x", "Child1", "-child1", "child1-", "a" * 64],
)
def test_cloud_run_refuses_an_execution_id_cloud_run_does_not_issue_before_any_read(execution_id):
    session = Session({})
    run = subject.CloudRunTransport(session)
    name = DAILY_JOB + "/executions/" + execution_id
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_native_name_forbidden$"):
        run.read_execution(name)
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_native_name_forbidden$"):
        run.read(name)
    assert session.calls == []
