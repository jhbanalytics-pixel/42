"""Native transports behind the daily authority adapters.

``NativeDailyAuthorityReadAdapter`` and ``NativeDailyAuthorityWriteAdapter`` meter every
call and shape-check every row; these transports are the provider calls under them:

* ``BigQueryRoutineTransport`` calls one allowlisted daily routine with every parameter
  bound as a STRING query parameter and the job's billed bytes capped;
* ``GcsObservationObjects`` writes and reads execution observations write-once in the
  evidence bucket and reports the storage facts the consume path checks;
* ``GcsWorkloadRates`` reads the one workload rates object, with the provider's storage
  time the derivation authority bounds the declared observation time by;
* ``CloudRunTransport`` reads the daily job and its executions and runs the daily job with
  the step number override only. Provider operation reads, used by derivation
  cancellation, belong to the crash reconciliation lane and refuse here. It also reads
  one execution of the funded pilot job, for the bridge's collection receipt binding,
  and nothing else of that job. It runs the ingest job with the empty run request, the
  daily collect stage's dispatch, and reads nothing of that job.

None of these has been run against the live project from this change.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256

from .daily_child_execution import (
    EXECUTION_ID,
    EXECUTION_PREFIX,
    FUNDED_EXECUTION_PREFIX,
    DailyChildRefusal,
    child_run_request,
    native_execution_view,
    native_funded_execution_view,
    native_job_view,
    stage_for_step,
)
from .daily_cost_policy import RATES_OBJECT
from .daily_execution_authority import DailyAuthorityIntegrityError, DailyAuthorityUnavailable
from .daily_operation_map import DAILY_JOB_RESOURCE
from .daily_store import EVIDENCE_BUCKET

DAILY_ROUTINES = frozenset(
    {
        "sp_derive_open_intelligence_daily_execution_v1",
        "sp_select_open_intelligence_daily_derivation_v1",
        "sp_consume_open_intelligence_daily_derivation_v1",
        "sp_cancel_open_intelligence_daily_derivation_v1",
        "sp_record_open_intelligence_daily_result_v1",
        "sp_read_open_intelligence_daily_derivation_v1",
        "sp_read_open_intelligence_daily_chain_v1",
    }
)
OBSERVATION_PREFIX = "42/daily/execution-observations/"
INGEST_JOB_NAME = DAILY_JOB_RESOURCE.rsplit("/", 1)[0] + "/intelligence-42-ingest-staging"
RUN_API = "https://run.googleapis.com/v2/"


def _integrity(code: str) -> None:
    raise DailyAuthorityIntegrityError(code)


class BigQueryRoutineTransport:
    def __init__(self, *, client, project: str, dataset: str, maximum_bytes_billed: int):
        if type(maximum_bytes_billed) is not int or maximum_bytes_billed <= 0:
            _integrity("daily_routine_budget_invalid")
        self._client = client
        self._prefix = f"{project}.{dataset}."
        self._maximum = maximum_bytes_billed

    def call(self, routine: str, parameters: tuple[object, ...]) -> tuple[dict, int | None]:
        from google.cloud import bigquery

        if routine not in DAILY_ROUTINES:
            _integrity("daily_routine_forbidden")
        if type(parameters) is not tuple or any(
            value is not None and not isinstance(value, str) for value in parameters
        ):
            _integrity("daily_routine_parameter_invalid")
        placeholders = ", ".join(f"@p{index}" for index in range(len(parameters)))
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(f"p{index}", "STRING", value)
                for index, value in enumerate(parameters)
            ],
            maximum_bytes_billed=self._maximum,
            use_legacy_sql=False,
        )
        job = self._client.query(
            f"CALL `{self._prefix}{routine}`({placeholders})", job_config=config
        )
        rows = list(job.result())
        if len(rows) != 1:
            _integrity("daily_routine_rows_invalid")
        return dict(rows[0].items()), job.total_bytes_billed


class GcsObservationObjects:
    def __init__(self, bucket):
        if getattr(bucket, "name", None) != EVIDENCE_BUCKET:
            _integrity("daily_object_bucket_mismatch")
        self._bucket = bucket

    @staticmethod
    def _name(object_name: object) -> str:
        if not isinstance(object_name, str) or not object_name.startswith(OBSERVATION_PREFIX):
            _integrity("daily_object_name_forbidden")
        return object_name

    def write_once(self, object_name: str, body: bytes) -> None:
        from google.api_core import exceptions

        name = self._name(object_name)
        try:
            self._bucket.blob(name).upload_from_string(
                body, content_type="application/json", if_generation_match=0, retry=None
            )
        except exceptions.PreconditionFailed as error:
            raise FileExistsError(name) from error

    def read(self, object_name: str, maximum_bytes: int) -> tuple[bytes, dict]:
        return _read_with_storage(self._bucket, self._name(object_name), maximum_bytes)


def _read_with_storage(bucket, name: str, maximum_bytes: int) -> tuple[bytes, dict]:
    blob = bucket.get_blob(name, retry=None)
    if blob is None:
        raise FileNotFoundError(name)
    if type(blob.size) is not int or blob.size > maximum_bytes:
        raise DailyAuthorityUnavailable("daily_object_budget_exhausted")
    created = blob.time_created
    if not isinstance(created, datetime) or created.utcoffset() is None:
        _integrity("daily_object_storage_time_invalid")
    raw = blob.download_as_bytes(if_generation_match=blob.generation, retry=None)
    if len(raw) != blob.size:
        _integrity("daily_observation_storage_invalid")
    return raw, {
        "content_sha256": sha256(raw).hexdigest(),
        "created_at": created.isoformat(),
        "generation": str(blob.generation),
        "object_name": name,
        "size_bytes": len(raw),
    }


class GcsWorkloadRates:
    """Read-only: the workload rates object and its provider storage facts."""

    def __init__(self, bucket):
        if getattr(bucket, "name", None) != EVIDENCE_BUCKET:
            _integrity("daily_object_bucket_mismatch")
        self._bucket = bucket

    def read(self, object_name: str, maximum_bytes: int) -> tuple[bytes, dict]:
        if object_name != RATES_OBJECT:
            _integrity("daily_object_name_forbidden")
        return _read_with_storage(self._bucket, object_name, maximum_bytes)


class IngestRunRefused(Exception):
    """The ingest job's run request was refused with a 4xx other than 403."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"ingest_run_refused:{status}")


class CloudRunTransport:
    def __init__(self, session, *, timeout: int = 30):
        self._session = session
        self._timeout = timeout

    @staticmethod
    def _job(name: object) -> str:
        if name != DAILY_JOB_RESOURCE:
            _integrity("daily_native_name_forbidden")
        return name

    @staticmethod
    def _execution(name: object, prefix: str = EXECUTION_PREFIX) -> str:
        if (
            not isinstance(name, str)
            or not name.startswith(prefix)
            or EXECUTION_ID.fullmatch(name[len(prefix) :]) is None
        ):
            _integrity("daily_native_name_forbidden")
        return name

    def _json(self, response) -> dict:
        if response.status_code == 403:
            raise PermissionError("daily_native_forbidden")
        if response.status_code != 200:
            raise DailyAuthorityUnavailable("daily_native_unavailable")
        body = response.json()
        if not isinstance(body, dict):
            _integrity("daily_native_response_invalid")
        return body

    def _get(self, name: str) -> dict:
        return self._json(self._session.get(RUN_API + name, timeout=self._timeout))

    def read_execution(self, execution_name: str) -> dict:
        return self._get(self._execution(execution_name))

    def read_raw_job(self, job_resource: str) -> dict:
        return self._get(self._job(job_resource))

    @staticmethod
    def _view(execution: dict, view) -> dict:
        try:
            return view(execution)
        except DailyChildRefusal as error:
            # Terminal facts that disagree are not a transient read; retrying cannot fix them.
            if error.code == "daily_native_execution_terminal_ambiguous":
                raise DailyAuthorityIntegrityError(error.code) from error
            raise

    def read(self, execution_name: str) -> dict:
        return self._view(self.read_execution(execution_name), native_execution_view)

    def read_funded(self, execution_name: str) -> dict:
        """One execution of the funded pilot job, by the same GET and the same terminal read."""
        execution = self._get(self._execution(execution_name, FUNDED_EXECUTION_PREFIX))
        return self._view(execution, native_funded_execution_view)

    def read_job(self, job_resource: str) -> dict:
        return native_job_view(self.read_raw_job(job_resource))

    def run_job(self, job_resource: str, body: Mapping[str, object]) -> dict:
        job = self._job(job_resource)
        try:
            step = body["overrides"]["containerOverrides"][0]["env"][0]["value"]
            expected = child_run_request(stage_for_step(step))
        except (KeyError, IndexError, TypeError, DailyChildRefusal) as error:
            raise DailyAuthorityIntegrityError("daily_run_request_forbidden") from error
        if body != expected:
            _integrity("daily_run_request_forbidden")
        return self._json(
            self._session.post(f"{RUN_API}{job}:run", json=dict(body), timeout=self._timeout)
        )

    def run_ingest_job(self) -> dict:
        """Start one execution of the ingest job with the empty run request.

        The orchestration identity holds ``roles/run.invoker`` on the ingest job, which
        runs a job and carries no override, so the request body is ``{}`` and nothing
        else: the ingest execution reads its identity from its own runtime.

        A 403 raises ``PermissionError`` and any other 4xx ``IngestRunRefused``: the API
        refused the request, so no execution exists. Anything else that is not a 200,
        a 5xx or a transport failure, leaves unknown whether an execution was created.
        """
        response = self._session.post(
            f"{RUN_API}{INGEST_JOB_NAME}:run", json={}, timeout=self._timeout
        )
        if response.status_code != 403 and 400 <= response.status_code < 500:
            raise IngestRunRefused(response.status_code)
        return self._json(response)

    def read_operation(self, operation_name: str) -> dict:
        raise DailyAuthorityUnavailable("daily_provider_operation_unmapped")


__all__ = [
    "DAILY_ROUTINES",
    "INGEST_JOB_NAME",
    "BigQueryRoutineTransport",
    "CloudRunTransport",
    "GcsObservationObjects",
    "GcsWorkloadRates",
    "IngestRunRefused",
]
