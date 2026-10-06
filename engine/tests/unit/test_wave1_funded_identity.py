"""The identity every Wave 1 query and writer admits is the registry's wave1_pilot binding.

The v2 origin registry binds wave1_pilot to intelligence-42-funded. The job runs as that
identity, so its GDELT query and every funded writer must admit it and refuse any other,
and the job must refuse before it consumes an approval when its credential is not that
identity. Every native client is a fake.
"""

from __future__ import annotations

import contextlib
import socket
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

import pytest
from scripts import run_rss_now as run
from src.analysis.open_intelligence import (
    execution_approval,
    execution_generations,
    funded_control_persistence,
    funded_control_terminal_persistence,
    funded_lane,
    funded_lane_persistence,
    funded_source_values,
    gdelt_wave1_persistence,
)
from src.ingestion.connectors import gdelt

from tests.unit.test_execution_runtime_v2 import CONSUMPTION_V2
from tests.unit.test_gdelt_wave1_persistence import event, gcam
from tests.unit.test_wave1_execution_manifest_producer import (
    JOB_STARTED_AT,
    FakeBigQuery,
    approved_job,
    build_describe,
    produce,
    tables,
)

FUNDED = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
RETIRED = "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com"
PROJECT = "ogilvy-trends-v2"
FUNDED_DATASET = "trends_v2_staging_funded"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))


def credentials(identity):
    return SimpleNamespace(service_account_email=identity, quota_project_id=PROJECT)


class FirstWrite(Exception):
    pass


@dataclass
class _Job:
    rows: tuple = ()
    state: str = "DONE"
    job_id: str = "job-1"
    total_bytes_processed: int = 0
    errors: object = None

    def result(self, **_kwargs):
        return self.rows


@dataclass
class WriterClient:
    """A BigQuery client under one identity that stops at the first mutating statement."""

    identity: str
    project: str = PROJECT
    location: str = "US"
    dataset: str = FUNDED_DATASET
    statements: list = field(default_factory=list)

    def __post_init__(self):
        self._credentials = credentials(self.identity)
        self.writer_identity = self.identity

    def query(self, sql, *args, job_config=None, **kwargs):
        self.statements.append(sql)
        if getattr(job_config, "dry_run", False):
            return _Job()
        if "gdelt-bq.gdeltv2" in sql:
            rows = (
                tuple(event(index) for index in (1, 2))
                if "events_partitioned" in sql
                else tuple(gcam(index) for index in (1, 2))
            )
            return _Job(rows=rows, job_id=f"job-{len(self.statements)}")
        raise FirstWrite(sql)


# the resolver


def test_the_wave1_identity_is_the_active_registry_binding():
    registry = execution_generations.active_generation().registry
    origin = execution_approval._v2_origin_for_operation(
        "wave1_pilot", mode="new_consume", registry=registry
    )
    assert funded_lane.wave1_pilot_service_identity() == FUNDED
    assert origin.operation_bindings["wave1_pilot"].service_identity == FUNDED


def test_the_wave1_identity_refuses_when_no_trusted_generation_loads(monkeypatch):
    def unavailable():
        raise ValueError("generation unavailable")

    monkeypatch.setattr(execution_generations, "active_generation", unavailable)
    with pytest.raises(funded_lane.Wave1ExecutionBlocked, match="registry"):
        funded_lane.wave1_pilot_service_identity()


def test_the_wave1_identity_is_never_taken_from_the_environment(monkeypatch):
    for name in ("GOOGLE_SERVICE_ACCOUNT", "SERVICE_ACCOUNT", "K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.setenv(name, RETIRED)
    assert funded_lane.wave1_pilot_service_identity() == FUNDED


# every query and writer


def _funded_rows():
    from tests.unit.test_funded_source_values import _rows

    return _rows()


WRITERS = {
    "gdelt_persistence": lambda client: gdelt_wave1_persistence._client(client),
    "funded_ledger": lambda client: funded_lane_persistence._validate_client(
        client, PROJECT, FUNDED_DATASET
    ),
    "funded_ledger_descriptor": lambda client: funded_lane_persistence._validate_descriptor(
        client, PROJECT, FUNDED_DATASET
    ),
    "funded_control": lambda client: funded_control_persistence._client(client),
    "funded_terminal": lambda client: funded_control_terminal_persistence._client(client),
    "funded_source_values": lambda client: funded_source_values.persist_wave1_source_value_rows(
        project=PROJECT, dataset=FUNDED_DATASET, client=client, rows=_funded_rows()
    ),
}


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_every_funded_writer_admits_the_registry_identity(writer):
    client = WriterClient(FUNDED)
    with contextlib.suppress(FirstWrite):
        WRITERS[writer](client)
    assert all("gdelt-bq" not in sql for sql in client.statements)


@pytest.mark.parametrize("identity", [RETIRED, "default", None])
@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_every_funded_writer_refuses_any_other_identity(writer, identity):
    client = WriterClient(identity)
    with pytest.raises(Exception) as caught:
        WRITERS[writer](client)
    assert not isinstance(caught.value, FirstWrite)
    assert [sql for sql in client.statements if not sql.lstrip().startswith("SELECT")] == []


@pytest.mark.parametrize(("identity", "admitted"), [(FUNDED, True), (RETIRED, False)])
def test_the_gdelt_result_query_admits_only_the_registry_identity(monkeypatch, identity, admitted):
    from tests.unit.test_source_wave1_gdelt import _approval_bound_entry

    capability, entry, receipt = _approval_bound_entry(monkeypatch)
    client = WriterClient(identity)
    if admitted:
        job = gdelt.execute_wave1_gdelt_query(
            client, entry, receipt, execution_capability=capability
        )
        assert job.state == "DONE"
    else:
        with pytest.raises(funded_lane.Wave1ExecutionBlocked, match="target"):
            gdelt.execute_wave1_gdelt_query(client, entry, receipt, execution_capability=capability)
        assert client.statements == []


def test_no_wave1_module_names_the_retired_identity():
    import inspect

    for module in (
        gdelt,
        gdelt_wave1_persistence,
        funded_lane_persistence,
        funded_control_persistence,
        funded_control_terminal_persistence,
        funded_source_values,
        run,
    ):
        assert RETIRED not in inspect.getsource(module)


# the job


def _job(monkeypatch, tmp_path, identity, consumed):
    code, output, _client = produce(tmp_path)
    assert code == 0
    approval, execution, job = approved_job(output.read_bytes())
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setattr(run, "_ACTIVE_WAVE1_EXECUTION", None)
    monkeypatch.setattr(run, "_wave1_artifact_client", lambda: FakeBigQuery(tables()))
    result_client = WriterClient(identity)
    monkeypatch.setattr(run, "_wave1_result_client", lambda: result_client)
    monkeypatch.setattr(
        execution_approval,
        "_load_execution_authority",
        partial(
            execution_approval._load_execution_authority,
            execution_reader=lambda: {"execution": execution, "job": job},
            approval_reader=lambda digest: approval,
            build_reader=lambda name: build_describe(),
            now=lambda: JOB_STARTED_AT,
        ),
    )
    consumed_at = JOB_STARTED_AT + timedelta(seconds=1)

    def writer(request):
        consumed.append(request)
        return {
            **request,
            "consumption_contract_version": CONSUMPTION_V2,
            "consumption_id": execution_approval.consumption_id_v2(
                request["approval_id"],
                request["execution_name"],
                consumed_at,
                origin_registry_sha256=request["origin_registry_sha256"],
                resource_manifest_sha256=request["resource_manifest_sha256"],
            ),
            "consumed_at": consumed_at,
        }

    monkeypatch.setattr(
        execution_approval,
        "_consume_execution_authority",
        partial(execution_approval._consume_execution_authority, consumption_writer=writer),
    )
    return result_client


def test_the_funded_job_reaches_its_first_write_as_intelligence_42_funded(
    monkeypatch, tmp_path, capsys
):
    consumed = []
    client = _job(monkeypatch, tmp_path, FUNDED, consumed)
    try:
        durable = run._begin_wave1_durable_execution("run_wave1_pilot", JOB_STARTED_AT)
        assert len(consumed) == 1
        assert durable.authority.manifest.service_identity == FUNDED
        with pytest.raises(FirstWrite) as write:
            run._execute_wave1_gdelt(durable, "run_wave1_pilot")
    finally:
        run._ACTIVE_WAVE1_EXECUTION = None
    capsys.readouterr()
    statement = str(write.value)
    assert "BEGIN TRANSACTION" in statement
    assert gdelt_wave1_persistence.EVENTS_TABLE in statement
    assert sum("gdelt-bq.gdeltv2" in sql for sql in client.statements) == 2


@pytest.mark.parametrize("identity", [RETIRED, "default", None])
def test_the_job_refuses_before_consuming_when_its_identity_cannot_write(
    monkeypatch, tmp_path, capsys, identity
):
    consumed = []
    client = _job(monkeypatch, tmp_path, identity, consumed)
    try:
        with pytest.raises(RuntimeError, match="identity"):
            run._begin_wave1_durable_execution("run_wave1_pilot", JOB_STARTED_AT)
    finally:
        run._ACTIVE_WAVE1_EXECUTION = None
    capsys.readouterr()
    assert consumed == []
    assert client.statements == []
    assert run._DURABLE_WAVE1_ARTIFACT_CONTEXT is None
