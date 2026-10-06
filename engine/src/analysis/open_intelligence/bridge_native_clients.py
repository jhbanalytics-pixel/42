"""The bridge loader's production native clients, run as the brain's own service identity.

Each read is one bounded call with no retry and the timeout the other native snapshot
readers use: BigQuery jobs.get for each clone's creating job and tables.get for each clone,
fixed parameterized statements for the approval ledger chain, the recurring grant and the
product rows, and object reads for the manifest inputs, the stored capture, the retained
source runs and the product completion records. Every resource name is checked against a
pinned grammar before it reaches a URL. Nothing a reader returns is trusted here beyond its
shape: the loader's validators recompute every digest over what was read.

The daily execution chain has no production read transport in this tree (the routine row
decoding, the execution observation object reader and the native execution normalizer are
not built), so the daily chain route and the evidence chain locator refuse with a coded
refusal rather than reading a chain through anything weaker.

The approval ledger route binds its collection receipts to the funded pilot job's own
executions instead: one Cloud Run v2 executions.get of a pinned funded execution name, read
through the daily lane's Cloud Run transport and native execution view, with no redirect, no
retry and no credential refresh retry. None of this has been run against the live project.
"""

import json
import os
import re
from datetime import date
from urllib.parse import urlencode, urlparse

from .daily_child_execution import FUNDED_EXECUTION_PREFIX, DailyChildRefusal
from .daily_execution_authority import DailyAuthorityIntegrityError, DailyAuthorityUnavailable
from .source_estate_bridge import LANES
from .source_estate_bridge_evidence import (
    RECORDED_HISTORY_LANES,
    BridgeEvidence,
    bridge_grant_reader,
    bridge_input_reader,
)

PROJECT = "ogilvy-trends-v2"
LOCATION = "US"
HOST = "bigquery.googleapis.com"
ENDPOINT = f"https://{HOST}"
IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
# The per call timeout the native snapshot readers use for a bounded read.
READ_TIMEOUT = 30
# The authority cap the protected context route reserves for one approval ledger read.
LEDGER_BYTES_BILLED = 35_000_000
# The per statement ceiling the Ask query executor admits.
PRODUCT_BYTES_BILLED = 1_000_000_000
# The daily product readback refuses a read of more rows than this.
PRODUCT_ROW_LIMIT = 100_000
_API = f"/bigquery/v2/projects/{PROJECT}"
# An execution of the funded pilot job: the job's exact name and the five character suffix
# Cloud Run gives each execution it creates.
_FUNDED_EXECUTION = re.compile(r"intelligence-42-funded-pilot-staging-[a-z0-9]{5}")
_FUNDED_INVALID = "bridge_funded_execution_invalid"
_FUNDED_UNAVAILABLE = "bridge_funded_execution_unavailable"
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,1024}")
_SUBMIT_PATH = re.compile(re.escape(f"{_API}/jobs"))
_READ_PATH = re.compile(re.escape(_API) + r"/(?:jobs|queries)/[A-Za-z0-9_-]{1,1024}")
_CONSUMPTION_ID = re.compile(r"exc_[0-9a-f]{64}")
_CLONE = re.compile(
    rf"{PROJECT}\.trends_v2_staging\.(staging_bridge_v3_[0-9]{{8}}_(?:{'|'.join(LANES)}))"
)
# The native readback of one recorded product lane for one product date, as the completion
# readback reads it: every column of the lane's rows on that date.
_PRODUCT_SQL = {
    "seed_candidates": (
        f"SELECT * FROM `{PROJECT}.trends_v2_staging.seed_candidates`"
        " WHERE proposed_date = @product_date"
    ),
    "trend_analysis": (
        f"SELECT * FROM `{PROJECT}.trends_v2_staging.trend_analysis`"
        " WHERE trend_date = @product_date"
    ),
}
if set(_PRODUCT_SQL) != set(RECORDED_HISTORY_LANES):
    raise RuntimeError("bridge_product_readback_unpinned")
_FORBIDDEN_ENVIRONMENT = ("BIGQUERY_EMULATOR_HOST", "STORAGE_EMULATOR_HOST")
_UNAVAILABLE = "bridge_daily_chain_unavailable"


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _require_environment():
    _require(
        not any(os.environ.get(name) for name in _FORBIDDEN_ENVIRONMENT)
        and os.environ.get("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false") in ("", "false")
        and os.environ.get("GOOGLE_API_USE_MTLS_ENDPOINT", "auto") in ("auto", "never"),
        "bridge_environment_invalid",
    )


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "bridge_native_resource_invalid")
        value[key] = item
    return value


class BridgeWarehouse:
    """BigQuery reads for the bridge loader under one set of runtime credentials."""

    def __init__(self, credentials):
        self._credentials = credentials

    def _get(self, path, params, *, invalid, unavailable):
        """One GET of one pinned resource path, returning the resource exactly as served."""
        from google.auth.transport.requests import AuthorizedSession

        _require_environment()
        session = AuthorizedSession(self._credentials)
        try:
            reply = session.request(
                "GET",
                ENDPOINT + path + ("?" + urlencode(params) if params else ""),
                timeout=READ_TIMEOUT,
                allow_redirects=False,
            )
            status = reply.status_code
            content = reply.content
        except Exception as error:
            raise ValueError(unavailable) from error
        finally:
            session.close()
        _require(status == 200, unavailable)
        try:
            resource = json.loads(content, object_pairs_hook=_unique_object)
        except ValueError as error:
            raise ValueError(invalid) from error
        _require(type(resource) is dict, invalid)
        return resource

    def read_job(self, job_id):
        """jobs.get for one job in the project, returned whole, including its statistics."""
        _require(
            type(job_id) is str and _JOB_ID.fullmatch(job_id) is not None,
            "bridge_native_job_invalid",
        )
        resource = self._get(
            f"{_API}/jobs/{job_id}",
            {"location": LOCATION},
            invalid="bridge_native_job_invalid",
            unavailable="bridge_native_job_unavailable",
        )
        _require(
            resource.get("jobReference")
            == {"projectId": PROJECT, "location": LOCATION, "jobId": job_id},
            "bridge_native_job_invalid",
        )
        return resource

    def read_clone(self, table):
        """tables.get for one bridge clone in the staging dataset, returned whole."""
        match = _CLONE.fullmatch(table) if type(table) is str else None
        _require(match is not None, "bridge_clone_invalid")
        name = match.group(1)
        resource = self._get(
            f"{_API}/datasets/trends_v2_staging/tables/{name}",
            None,
            invalid="bridge_clone_invalid",
            unavailable="bridge_clone_unavailable",
        )
        _require(
            resource.get("tableReference")
            == {"projectId": PROJECT, "datasetId": "trends_v2_staging", "tableId": name},
            "bridge_clone_invalid",
        )
        return resource

    def query_rows(self, sql, parameters, *, maximum_bytes_billed, row_limit):
        """One fixed parameterized statement, submitted once, capped and fully metered."""
        from google.cloud import bigquery

        _require_environment()
        client = bigquery.Client(
            project=PROJECT,
            location=LOCATION,
            credentials=self._credentials,
            default_query_job_config=None,
            client_options={"api_endpoint": ENDPOINT},
        )
        submitted = [False]
        original = client._http.request

        def guarded(method, url, **kwargs):
            target = urlparse(url)
            path = target.path
            # The whole path is pinned: dot segments, encoded dots, extra segments and path
            # parameters (which urlparse moves out of the path) never pass.
            allowed = (
                target.scheme == "https"
                and target.netloc == HOST
                and not target.params
                and (
                    (
                        method == "POST"
                        and _SUBMIT_PATH.fullmatch(path) is not None
                        and not submitted[0]
                    )
                    or (method == "GET" and _READ_PATH.fullmatch(path) is not None)
                )
            )
            _require(allowed, "bridge_native_request_refused")
            if method == "POST":
                submitted[0] = True
            kwargs["allow_redirects"] = False
            kwargs["timeout"] = READ_TIMEOUT
            return original(method, url, **kwargs)

        client._http.request = guarded
        config = bigquery.QueryJobConfig(
            query_parameters=list(parameters),
            maximum_bytes_billed=maximum_bytes_billed,
            use_query_cache=False,
            use_legacy_sql=False,
        )
        try:
            job = client.query(
                sql,
                job_config=config,
                location=LOCATION,
                retry=None,
                job_retry=None,
                timeout=READ_TIMEOUT,
            )
            rows = [
                dict(row.items())
                for row in job.result(
                    max_results=row_limit + 1, retry=None, job_retry=None, timeout=READ_TIMEOUT
                )
            ]
            state, error, billed = job.state, job.error_result, job.total_bytes_billed
        except Exception as error:
            raise ValueError("bridge_native_query_unavailable") from error
        finally:
            client.close()
        _require(
            state == "DONE"
            and error is None
            and type(billed) is int
            and 0 <= billed <= maximum_bytes_billed
            and len(rows) <= row_limit,
            "bridge_native_query_invalid",
        )
        return rows


class _ReadOnlyObjects:
    """The object reader the retained run and completion stores read through; no writes."""

    def __init__(self, bucket):
        self._bucket = bucket

    def read(self, name):
        blob = self._bucket.get_blob(name, timeout=READ_TIMEOUT, retry=None)
        if blob is None:
            return None
        generation = blob.generation
        _require(type(generation) is int and generation > 0, "bridge_object_invalid")
        raw = blob.download_as_bytes(
            if_generation_match=generation, raw_download=True, timeout=READ_TIMEOUT, retry=None
        )
        return raw, generation

    def write(self, name, payload, *, if_generation_match):
        raise ValueError("bridge_object_write_refused")


class BridgeObjects:
    """Cloud Storage reads for the bridge loader under one set of runtime credentials."""

    def __init__(self, credentials):
        self._credentials = credentials

    def _with_bucket(self, name, read):
        from google.cloud import storage

        _require_environment()
        client = storage.Client(project=PROJECT, credentials=self._credentials)
        try:
            return read(client.bucket(name))
        finally:
            client.close()

    def read_input(self, name, digest):
        from .production_snapshot_storage import BUCKET_NAME, SourceCaptureObjects

        return self._with_bucket(
            BUCKET_NAME,
            lambda bucket: SourceCaptureObjects(bucket).read_input(
                name, digest, timeout=READ_TIMEOUT
            ),
        )

    def read_capture_facts(self, stored_artifact):
        from .production_snapshot_storage import BUCKET_NAME, SourceCaptureObjects

        try:
            artifact = self._with_bucket(
                BUCKET_NAME,
                lambda bucket: SourceCaptureObjects(bucket).read_stored_capture(
                    stored_artifact, timeout=READ_TIMEOUT
                ),
            )
        except ValueError as error:
            if error.args == ("bridge_environment_invalid",):
                raise
            raise ValueError("bridge_capture_unavailable") from error
        return artifact["capture"]

    def read_source_run(self, run_id):
        from .daily_native_clients import ObjectSourceRuns
        from .daily_store import EVIDENCE_BUCKET

        return self._with_bucket(
            EVIDENCE_BUCKET, lambda bucket: ObjectSourceRuns(_ReadOnlyObjects(bucket)).read(run_id)
        )

    def read_completion(self, operation_id):
        from .daily_product_completion import CompletionStore
        from .daily_store import EVIDENCE_BUCKET

        return self._with_bucket(
            EVIDENCE_BUCKET,
            lambda bucket: CompletionStore(_ReadOnlyObjects(bucket)).read(operation_id),
        )


class _OneGet:
    """The session the Cloud Run transport reads through: one GET of one URL, no redirect."""

    def __init__(self, session, url):
        self._session = session
        self._url = url
        self._used = False

    def get(self, url, *, timeout):
        _require(
            not self._used and url == self._url and timeout == READ_TIMEOUT,
            "bridge_native_request_refused",
        )
        self._used = True
        return self._session.request("GET", url, timeout=timeout, allow_redirects=False)


class BridgeFundedExecutions:
    """Cloud Run reads of the funded pilot's executions under one set of runtime credentials."""

    def __init__(self, credentials):
        self._credentials = credentials

    def read(self, execution_id):
        """The native view of one funded pilot execution, by its id, read once.

        Denied is ``bridge_funded_execution_forbidden``; any other status or a failed call
        is ``bridge_funded_execution_unavailable``; a body that is not one execution of the
        funded job, or whose terminal facts disagree, is ``bridge_funded_execution_invalid``.
        """
        from google.auth.transport.requests import AuthorizedSession

        from .daily_native_transports import RUN_API, CloudRunTransport

        _require(
            type(execution_id) is str and _FUNDED_EXECUTION.fullmatch(execution_id) is not None,
            _FUNDED_INVALID,
        )
        _require_environment()
        name = FUNDED_EXECUTION_PREFIX + execution_id
        session = AuthorizedSession(self._credentials, refresh_status_codes=())
        try:
            transport = CloudRunTransport(_OneGet(session, RUN_API + name), timeout=READ_TIMEOUT)
            return transport.read_funded(name)
        except PermissionError as error:
            raise ValueError("bridge_funded_execution_forbidden") from error
        except DailyAuthorityUnavailable as error:
            raise ValueError(_FUNDED_UNAVAILABLE) from error
        except (DailyAuthorityIntegrityError, DailyChildRefusal, ValueError) as error:
            raise ValueError(_FUNDED_INVALID) from error
        except Exception as error:
            raise ValueError(_FUNDED_UNAVAILABLE) from error
        finally:
            session.close()


def ledger_reader(warehouse):
    """The approval ledger chain rows of one consumed execution, by consumption id."""
    from google.cloud import bigquery

    from .general_question_context_queries import _RESULT_SQL_V2

    def read(consumption_id):
        _require(
            type(consumption_id) is str and _CONSUMPTION_ID.fullmatch(consumption_id) is not None,
            "bridge_ledger_invalid",
        )
        return warehouse.query_rows(
            _RESULT_SQL_V2,
            (bigquery.ScalarQueryParameter("consumption_id", "STRING", consumption_id),),
            maximum_bytes_billed=LEDGER_BYTES_BILLED,
            row_limit=1,
        )

    return read


def approval_reader(warehouse):
    """The approval ledger rows of one manifest digest, for the recurring grant reader."""
    from google.cloud import bigquery

    from .general_question_context_queries import _APPROVAL_SQL_V2

    def read(digest):
        return warehouse.query_rows(
            _APPROVAL_SQL_V2,
            (bigquery.ScalarQueryParameter("manifest_sha256", "STRING", digest),),
            maximum_bytes_billed=LEDGER_BYTES_BILLED,
            row_limit=1,
        )

    return read


def product_reader(warehouse):
    """The native readback of one recorded product lane on one product date."""
    from google.cloud import bigquery

    def read(lane, day):
        _require(type(lane) is str and lane in _PRODUCT_SQL, "bridge_evidence_invalid")
        return warehouse.query_rows(
            _PRODUCT_SQL[lane],
            (bigquery.ScalarQueryParameter("product_date", "DATE", date.fromisoformat(day)),),
            maximum_bytes_billed=PRODUCT_BYTES_BILLED,
            row_limit=PRODUCT_ROW_LIMIT,
        )

    return read


class _UnavailableDailyChain:
    """Daily chain read clients for a tree with no production daily chain transport."""

    def _refuse(self, *args, **kwargs):
        raise DailyAuthorityUnavailable(_UNAVAILABLE)

    read_derivation = _refuse
    read_execution_observation = _refuse
    read_chain = _refuse
    read_native_execution = _refuse


def _daily_chain_unavailable(reference):
    raise ValueError(_UNAVAILABLE)


def bridge_evidence(credentials, *, locate_chain=_daily_chain_unavailable):
    """The loader's evidence object over the production object, product and funded readers."""
    objects = BridgeObjects(credentials)
    return BridgeEvidence(
        locate_chain=locate_chain,
        read_source_run=objects.read_source_run,
        read_completion=objects.read_completion,
        read_product_rows=product_reader(BridgeWarehouse(credentials)),
        read_funded_execution=BridgeFundedExecutions(credentials).read,
    )


def production_bridge_clients(credentials):
    """Every client ``load_bridge_history_source`` takes, run as the brain's own identity.

    The approval ledger route is served whole, its collection receipts bound through the
    ledger reader and the funded execution reader. The daily chain route and the evidence
    chain locator refuse until a daily chain read transport exists. The packaged catalogue
    admits the ledger generation (``generation_loader`` None).
    """
    _require(
        getattr(credentials, "service_account_email", None) == IDENTITY,
        "bridge_identity_invalid",
    )
    _require_environment()
    warehouse = BridgeWarehouse(credentials)
    objects = BridgeObjects(credentials)
    return {
        "capture_clients": _UnavailableDailyChain(),
        "read_input": bridge_input_reader(objects.read_input),
        "read_grant": bridge_grant_reader(approval_reader(warehouse)),
        "evidence": bridge_evidence(credentials),
        "ledger_reader": ledger_reader(warehouse),
        "generation_loader": None,
        "read_native_job": warehouse.read_job,
        "read_capture_facts": objects.read_capture_facts,
        "read_clone": warehouse.read_clone,
    }


__all__ = [
    "IDENTITY",
    "LEDGER_BYTES_BILLED",
    "PRODUCT_BYTES_BILLED",
    "PRODUCT_ROW_LIMIT",
    "READ_TIMEOUT",
    "BridgeFundedExecutions",
    "BridgeObjects",
    "BridgeWarehouse",
    "bridge_evidence",
    "ledger_reader",
    "production_bridge_clients",
]
