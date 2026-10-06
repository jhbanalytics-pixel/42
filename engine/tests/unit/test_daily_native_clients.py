"""Native clients for the daily stage handlers, proven over fakes at the store boundary.

Every real client answers the calls its fake in ``test_daily_stages`` answers; the
unbound clients pass the handler builder's validation and refuse with their codes;
the collect stage runs end to end through the native clients with the producer
terminal replaced by an in process double that records one call, and the capture
stage runs end to end with the snapshot capture runner replaced at its boundary.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import itertools
import json
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.staging import capture_protected_production_snapshot as capture_script
from scripts.staging import release_open_intelligence_run as release
from scripts.staging import replay_open_intelligence as replay
from src.analysis.open_intelligence import daily_certification as certification
from src.analysis.open_intelligence import (
    daily_composer,
    daily_stages,
    execution_approval,
    ingest_receipts,
)
from src.analysis.open_intelligence import daily_native_clients as native
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.capture_registry import validate_capture_entry
from src.analysis.open_intelligence.daily_cycle import STAGE_ORDER, execute_daily_cycle
from src.analysis.open_intelligence.daily_stages import (
    CAPTURE_READBACK_FIELDS,
    CERTIFICATION_ARTIFACT_FIELDS,
    CLIENT_FIELDS,
    StageRefusal,
    _validated_clients,
    build_stage_handlers,
)
from src.analysis.open_intelligence.daily_store import (
    DailyStore,
    PreconditionFailed,
    slot_operation_id,
)
from src.analysis.open_intelligence.execution_generations import active_generation
from src.analysis.open_intelligence.geographic_scope import (
    METHOD_CONFIDENCE,
    METHOD_SCOPES,
    ROW_CITING_METHODS,
    GeographicScope,
    resolve_geographic_scope,
)
from src.analysis.open_intelligence.persistence import RUN_RECEIPT_TABLE
from src.analysis.open_intelligence.readiness import (
    EXPLICIT_ORIGIN_POLICY,
    ReadinessRules,
    evaluate_readiness,
)
from src.analysis.open_intelligence.staging_source_profile import read_completed_source_run
from src.utils import geo_blocklist as replay_blocklist
from src.utils.bigquery import get_dataset

from tests.unit import test_daily_composer as composer_tests
from tests.unit.test_daily_cycle import FakeAuthority
from tests.unit.test_daily_stages import (
    CUTOFF,
    OPERATION_ID,
    POLICY_DIGEST,
    Clock,
    IssuingAuthority,
    collection_receipt,
    compact_payload,
    creation_records,
    kernel_profile,
    manifest,
    receipt_for,
)
from tests.unit.test_daily_store import FakeObjectClient

ENGINE_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ENGINE_ROOT / "src" / "analysis" / "open_intelligence" / "daily_native_clients.py"
STARTED = CUTOFF + timedelta(minutes=5)
SLOT = slot_operation_id(
    environment="staging", source_policy_digest=POLICY_DIGEST, cutoff_utc=CUTOFF
)
COMPLETED = STARTED + timedelta(minutes=20)
AUTHORITY = {
    "COLLECTION_POLICY_SHA256": POLICY_DIGEST,
    "COLLECTION_PROFILE_SHA256": "6" * 64,
}
# The runtime's own build facts: the source commit the image was built from and the job's
# own image digest. The Cloud Run execution name is set to something the receipt must never
# carry: the exact authority is keyed by the business attempt, not the execution.
BUILD = {
    "source_sha": "a" * 40,
    "image_uri": "region-docker.pkg.dev/project/repo/image@sha256:" + "b" * 64,
}
EXECUTION_NAME = "intelligence-42-daily-staging-x1"
EXACT_AUTHORITY = {
    "execution_id": "attempt:collect:1",
    "source_sha": BUILD["source_sha"],
    "image_uri": BUILD["image_uri"],
    "policy_sha256": POLICY_DIGEST,
    "profile_sha256": "6" * 64,
}


class FakeBigQueryClient:
    """Answers ``query(sql, ...)`` with a job whose ``result`` iterates the configured rows."""

    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, dict(kwargs)))
        rows = [dict(row) for row in self.rows]
        return SimpleNamespace(result=lambda **_kwargs: iter(rows))


class Terminal:
    """The producer terminal double: records its receipt through the ledger it is handed."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        assert kwargs["stop_after_ingestion"] is True
        ledger = kwargs["receipt_ledger"]
        authority = dict(kwargs["collection_authority"])
        assert set(authority) == set(EXACT_AUTHORITY)
        prior = ledger.operation(authority["execution_id"])
        if prior is not None:
            return prior
        closed_day = CUTOFF.date() - timedelta(days=1)
        receipt = collection_receipt(cutoff=closed_day.isoformat(), **authority)
        ledger.record(receipt, trend_date=CUTOFF.date().isoformat())
        return dict(receipt)


class SnapshotRunner:
    """The capture runner boundary double: the compact payload, a status and the verification.

    Like the runner seam, it answers the payload with the snapshot instant it was handed as
    the source instant; ``source_as_of`` models a runner whose plan read another instant.
    """

    def __init__(self, *, status="succeeded", source_as_of=None, verification=None):
        self.status = status
        self.source_as_of = source_as_of
        self.verification = verification or {}
        self.calls: list[dict] = []

    def __call__(self, run, *, cutoff, snapshot_as_of, attempt_id, origin, client, authority):
        self.calls.append(
            {
                "run": run,
                "cutoff": cutoff,
                "snapshot_as_of": snapshot_as_of,
                "attempt_id": attempt_id,
                "origin": origin,
                "client": client,
                "authority": authority,
            }
        )
        payload = compact_payload(captured_at=snapshot_as_of + timedelta(minutes=1))
        source = snapshot_as_of if self.source_as_of is None else self.source_as_of
        payload["source_as_of"] = source.isoformat()
        payload["cutoff_date"] = cutoff.isoformat()
        verification = {
            "result_id": "res_" + "2" * 32,
            "image_digest": "4" * 64,
            "generation": "7",
            "schema_digest": "2" * 64,
            "source_digest": "3" * 64,
        }
        verification.update(self.verification)
        return payload, self.status, verification


class Fixture:
    def __init__(
        self,
        monkeypatch,
        *,
        policy=POLICY_DIGEST,
        start=STARTED,
        build=BUILD,
        capture_runner=None,
        compose_binding=None,
        warehouse=None,
    ):
        monkeypatch.setenv("TRENDS_ENV", "staging")
        monkeypatch.setenv("BIGQUERY_DATASET", "intelligence_42_sources_staging")
        for variable, value in AUTHORITY.items():
            monkeypatch.setenv(variable, value)
        monkeypatch.setenv("COLLECTION_POLICY_SHA256", policy)
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", EXECUTION_NAME)
        for variable in ("COLLECTION_SOURCE_SHA", "COLLECTION_IMAGE_URI"):
            monkeypatch.delenv(variable, raising=False)
        self.terminal = Terminal()
        producer = importlib.import_module("scripts.run_rss_now")
        monkeypatch.setattr(producer, "_run_impl", self.terminal)
        self.object_client = FakeObjectClient()
        self.bigquery = FakeBigQueryClient() if warehouse is None else warehouse
        self.clock = Clock(start)
        self.engine = SimpleNamespace(
            daily_store=importlib.import_module("src.analysis.open_intelligence.daily_store")
        )
        self.clients = native.build_native_stage_clients(
            engine=self.engine,
            profile=kernel_profile(),
            environment="staging",
            object_client=self.object_client,
            warehouse=self.bigquery,
            now=self.clock,
            build=build,
            capture_runner=capture_runner,
            compose_binding=compose_binding,
        )
        self.kernel_store = DailyStore(self.object_client, owner="run-1", now=self.clock)

    def handlers(self):
        return native.guarded_stage_handlers(self.clients, profile=kernel_profile())

    def run(self, *, authority=None):
        return execute_daily_cycle(
            operation_id=SLOT,
            cutoff_utc=CUTOFF,
            profile=kernel_profile(),
            authority=IssuingAuthority(deny=("release",)) if authority is None else authority,
            store=self.kernel_store,
            stages=self.handlers(),
        )


def test_factory_returns_the_exact_client_fields_and_names_the_unbound_ones(monkeypatch):
    fixture = Fixture(monkeypatch)
    clients = fixture.clients
    assert set(clients) == set(CLIENT_FIELDS) | {"telemetry"}
    assert _validated_clients(clients) == dict(clients)
    described = native.describe_stage_clients(clients)
    assert list(described) == list(CLIENT_FIELDS)
    unbound = {name: entry["code"] for name, entry in described.items() if not entry["bound"]}
    assert unbound == {
        "composer": "client_unbound:composer",
        "persist": "client_unbound:persist",
    }
    bound = [name for name, entry in described.items() if entry["bound"]]
    assert bound == [
        "clock",
        "store",
        "collection_profile",
        "collector",
        "collection_ledger",
        "source_runs",
        "generation",
        "capture",
        "warehouse",
        "certifier",
        "release_profile",
        "products",
    ]
    assert len(bound) == 12
    assert described["capture"]["binding"] == (
        "src.analysis.open_intelligence.daily_native_clients.NativeCapture"
    )
    assert described["certifier"]["binding"] == (
        "src.analysis.open_intelligence.daily_certification.NativeCertifier"
    )
    assert set(native.UNBOUND_CLIENTS) == {"composer", "persist"}
    # Native products are bound over the reviewed history factory and carry no refusal.
    assert {name: entry["code"] for name, entry in described.items() if name in bound} == (
        dict.fromkeys(bound)
    )
    with_runner = Fixture(monkeypatch, capture_runner=SnapshotRunner())
    assert native.describe_stage_clients(with_runner.clients)["capture"]["code"] is None
    unbuilt = Fixture(monkeypatch, build=None)
    assert native.describe_stage_clients(unbuilt.clients)["collector"] == {
        "bound": True,
        "binding": "src.analysis.open_intelligence.daily_native_clients.native_collector.<locals>.collector",
        "code": "collection_build_unbound",
    }
    generation = active_generation()
    assert clients["generation"] == generation
    assert isinstance(clients["generation"].registry, Mapping)
    assert clients["clock"] is fixture.clock
    assert clients["telemetry"].enabled is False
    for entry in described.values():
        assert set(entry) == {"bound", "binding", "code"}
        assert isinstance(entry["binding"], str)
        assert entry["binding"]


def test_unbound_clients_refuse_every_call_and_attribute_use(monkeypatch):
    fixture = Fixture(monkeypatch)
    capture = fixture.clients["capture"]
    composer = fixture.clients["composer"]
    certifier = fixture.clients["certifier"]
    persist = fixture.clients["persist"]
    assert isinstance(capture, native.NativeCapture)
    assert isinstance(certifier, certification.NativeCertifier)
    assert all(hasattr(capture, name) for name in ("snapshot", "verified", "writer"))
    assert all(hasattr(certifier, name) for name in ("readiness", "artifacts", "record", "read"))
    assert isinstance(composer, native.UnboundClient)
    assert callable(composer)
    assert callable(persist)
    assert repr(composer) == "UnboundClient('composer')"
    with pytest.raises(StageRefusal, match=r"^client_unbound:composer$"):
        composer({}, cutoff=CUTOFF)
    with pytest.raises(StageRefusal, match=r"^client_unbound:composer$"):
        _ = composer.anything_else
    with pytest.raises(StageRefusal, match=r"^client_unbound:persist$"):
        persist(None, run_id="r", signal_date=CUTOFF.date(), created_at=CUTOFF)
    with pytest.raises(AttributeError):
        _ = composer.__deepcopy__
    # The capture client is bound over the native runner seam by default; without the
    # issued execution authority of the attempt it refuses before any runner call, and a
    # client constructed with no runner at all refuses its own unbound seam the same way.
    assert callable(native.DEFAULT_CAPTURE_RUNNER)
    assert capture.pre_dispatch_refusal() is None
    assert capture.verified("attempt:capture:1") is None
    assert capture.writer.read("profile/result") is None
    with pytest.raises(StageRefusal, match=r"^capture_authority_unbound$"):
        capture.snapshot({}, snapshot_as_of=CUTOFF, attempt_id="a", origin=None, authority=None)
    monkeypatch.setattr(native, "DEFAULT_CAPTURE_RUNNER", None)
    assert capture.pre_dispatch_refusal() == "capture_runner_unbound"
    with pytest.raises(StageRefusal, match=r"^capture_runner_unbound$"):
        capture.snapshot({}, snapshot_as_of=CUTOFF, attempt_id="a", origin=None, authority=None)
    assert fixture.object_client.writes == []


def test_factory_refuses_a_policy_that_differs_from_the_collection_authority(monkeypatch):
    with pytest.raises(ValueError, match=r"^collection_policy_mismatch$"):
        Fixture(monkeypatch, policy="1" * 64)
    monkeypatch.delenv("COLLECTION_POLICY_SHA256")
    with pytest.raises(ValueError, match=r"^collection_authority_missing$"):
        native.build_native_stage_clients(
            engine=SimpleNamespace(
                daily_store=importlib.import_module("src.analysis.open_intelligence.daily_store")
            ),
            profile=kernel_profile(),
            environment="staging",
            object_client=FakeObjectClient(),
            warehouse=FakeBigQueryClient(),
            now=Clock(),
        )
    with pytest.raises(ValueError, match=r"^profile_fields_inexact$"):
        native.build_native_stage_clients(
            engine=SimpleNamespace(),
            profile={"schema_version": "42_daily_v1"},
            environment="staging",
            object_client=FakeObjectClient(),
            warehouse=FakeBigQueryClient(),
            now=Clock(),
        )


def test_collection_profile_is_the_staging_bootstrap_shape_the_collect_stage_passes(monkeypatch):
    fixture = Fixture(monkeypatch)
    profile = fixture.clients["collection_profile"]
    assert profile["environment"] == "staging"
    assert profile["source_dataset"] == "intelligence_42_sources_staging"
    assert profile["markets"] == ["za", "ng", "ke"]
    assert profile["policy_sha256"] == POLICY_DIGEST
    assert profile["profile_sha256"] == "6" * 64
    assert profile["contract_version"] == "collection_profile_daily_v1"


def test_release_profile_is_a_live_staging_profile_for_the_closed_day(monkeypatch):
    fixture = Fixture(monkeypatch)
    profile = fixture.clients["release_profile"]
    generation = active_generation()
    assert profile.is_replay is False
    assert profile.independence_policy == EXPLICIT_ORIGIN_POLICY
    assert profile.source_policy_digest == POLICY_DIGEST
    assert profile.cutoff == "2026-09-10"
    assert profile.generation_pair == (
        generation.origin_registry_sha256,
        generation.resource_manifest_sha256,
    )
    assert profile.canary_namespace is False
    assert profile.run_id == "run_20260910_staging_daily_v1"
    assert profile.profile_name == "staging_daily_20260910"
    assert native.due_cutoff(datetime(2026, 9, 12, 23, 59, tzinfo=UTC)) == datetime(
        2026, 9, 12, tzinfo=UTC
    )
    with pytest.raises(ValueError, match=r"^clock_invalid$"):
        native.due_cutoff(datetime(2026, 9, 12, 23, 59))


def test_native_ledger_answers_the_fake_ledger_contract(monkeypatch):
    fixture = Fixture(monkeypatch)
    ledger = fixture.clients["collection_ledger"]
    receipt = collection_receipt()
    assert ledger.operation("attempt:collect:1") is None
    assert ledger.market_day("2026-09-11") == []
    ledger.record(receipt, trend_date="2026-09-11")
    assert ledger.operation("attempt:collect:1") == receipt
    assert ledger.market_day("2026-09-11") == [receipt]
    ledger.record(receipt, trend_date="2026-09-11")
    assert ledger.market_day("2026-09-11") == [receipt]
    with pytest.raises(ValueError, match=r"^collection_receipt_conflict$"):
        ledger.record(collection_receipt(raw_rows_persisted=11), trend_date="2026-09-11")
    other = collection_receipt(execution_id="attempt:collect:2", run_id="collect-2")
    ledger.record(other, trend_date="2026-09-11")
    assert [item["execution_id"] for item in ledger.market_day("2026-09-11")] == [
        "attempt:collect:1",
        "attempt:collect:2",
    ]
    names = sorted(fixture.object_client.objects)
    assert "42/daily/collection/days/2026-09-11.json" in names
    assert "42/daily/collection/receipts/attempt:collect:1.json" in names


def test_native_source_runs_answer_the_fake_contract(monkeypatch):
    fixture = Fixture(monkeypatch)
    source_runs = fixture.clients["source_runs"]
    assert source_runs.read("collect-2026-09-10") == {"status": "ok", "run": None}
    run = {
        "receipt": collection_receipt(),
        "collection_started_at": STARTED,
        "collection_completed_at": COMPLETED,
    }
    source_runs.record("collect-2026-09-10", run)
    assert source_runs.read("collect-2026-09-10") == {"status": "ok", "run": run}
    validated = read_completed_source_run(source_runs.read, "collect-2026-09-10")
    assert validated["observation_window_end"] == CUTOFF
    assert validated["collection_completed_at"] == COMPLETED
    with pytest.raises(ValueError, match=r"^source_run_exists$"):
        source_runs.record("collect-2026-09-10", run)


def test_native_store_reads_the_kernel_records_over_the_same_object_client(monkeypatch):
    fixture = Fixture(monkeypatch)
    assert fixture.kernel_store.claim(SLOT, CUTOFF)
    result = {
        "state": "succeeded",
        "input_digest": "1" * 64,
        "output_digest": "2" * 64,
        "result_reference": "source_run:collect-2026-09-10",
        "retry_safe": False,
    }
    fixture.kernel_store.record_stage(SLOT, "collect", result)
    assert fixture.clients["store"].read_stage(SLOT, "collect") == result
    assert fixture.clients["store"].read_stage(SLOT, "capture") is None


def test_warehouse_callable_runs_sql_through_the_client_and_returns_dict_rows(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.bigquery.rows = [{"run_id": "r1", "status": "completed"}]
    rows = fixture.clients["warehouse"]("SELECT run_id, status FROM t")
    assert rows == [{"run_id": "r1", "status": "completed"}]
    sql, kwargs = fixture.bigquery.calls[0]
    assert sql == "SELECT run_id, status FROM t"
    assert kwargs["location"] == "US"
    assert kwargs["retry"] is None
    assert kwargs["job_retry"] is None


def test_collector_runs_the_producer_terminal_in_process_with_the_native_ledger(monkeypatch):
    fixture = Fixture(monkeypatch)
    receipt = fixture.clients["collector"](
        stop_after_ingestion=True, attempt_id="attempt:collect:1"
    )
    assert receipt["execution_id"] == "attempt:collect:1"
    assert fixture.terminal.calls == [
        {
            "stop_after_ingestion": True,
            "receipt_ledger": fixture.clients["collection_ledger"],
            "collection_authority": EXACT_AUTHORITY,
        }
    ]
    assert fixture.clients["collection_ledger"].operation("attempt:collect:1") == receipt
    assert fixture.clients["collection_ledger"].operation(EXECUTION_NAME) is None


def test_collector_builds_a_fresh_authority_per_attempt(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.clients["collector"](stop_after_ingestion=True, attempt_id="attempt:collect:1")
    fixture.clients["collector"](stop_after_ingestion=True, attempt_id="attempt:collect:2")
    assert [call["collection_authority"]["execution_id"] for call in fixture.terminal.calls] == [
        "attempt:collect:1",
        "attempt:collect:2",
    ]
    first, second = (call["collection_authority"] for call in fixture.terminal.calls)
    assert first is not second
    assert {k: v for k, v in first.items() if k != "execution_id"} == {
        k: v for k, v in second.items() if k != "execution_id"
    }


@pytest.mark.parametrize("attempt", ["", "  ", None, 7], ids=["empty", "blank", "none", "int"])
def test_collector_refuses_an_attempt_id_that_is_not_text(monkeypatch, attempt):
    fixture = Fixture(monkeypatch)
    with pytest.raises(StageRefusal, match=r"^collection_attempt_invalid$"):
        fixture.clients["collector"](stop_after_ingestion=True, attempt_id=attempt)
    assert fixture.terminal.calls == []


@pytest.mark.parametrize(
    "build",
    [
        {"source_sha": "a" * 39, "image_uri": BUILD["image_uri"]},
        {"source_sha": "A" * 40, "image_uri": BUILD["image_uri"]},
        {"source_sha": BUILD["source_sha"], "image_uri": "region-docker.pkg.dev/repo/image:latest"},
        {"source_sha": BUILD["source_sha"]},
        {**BUILD, "extra": "x"},
        "not a mapping",
    ],
    ids=["short_sha", "upper_sha", "tag_not_digest", "missing_image", "extra_field", "text"],
)
def test_factory_validates_the_build_provenance_it_binds(monkeypatch, build):
    with pytest.raises(ValueError, match=r"^build_provenance_invalid$"):
        Fixture(monkeypatch, build=build)


def test_unbound_build_refuses_collect_before_dispatch_and_the_producer(monkeypatch):
    fixture = Fixture(monkeypatch, build=None)
    result = fixture.handlers()["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result == {
        "state": "failed",
        "input_digest": native.manifest_digest(manifest("collect")),
        "output_digest": "",
        "result_reference": "collect_refused:collection_build_unbound",
        "retry_safe": True,
    }
    with pytest.raises(StageRefusal, match=r"^collection_build_unbound$"):
        fixture.clients["collector"](stop_after_ingestion=True, attempt_id="attempt:collect:1")
    assert fixture.terminal.calls == []
    assert fixture.clients["collection_ledger"].operation("attempt:collect:1") is None


@pytest.mark.parametrize(
    ("environment", "resolved"),
    [
        ({}, "trends_v2_dev"),
        ({"TRENDS_ENV": "prod", "BIGQUERY_DATASET": "trends_v2"}, "trends_v2"),
        ({"TRENDS_ENV": "staging", "BIGQUERY_DATASET": "trends_v2_staging"}, "trends_v2_staging"),
        (
            {"TRENDS_ENV": "dev", "BIGQUERY_DATASET": "intelligence_42_sources_staging"},
            "intelligence_42_sources_staging_dev",
        ),
        (
            {"TRENDS_ENV": "prod", "BIGQUERY_DATASET": "intelligence_42_sources_staging"},
            "intelligence_42_sources_staging",
        ),
    ],
    ids=["empty", "production", "derived_staging", "dev_suffix", "prod_env_staging_dataset"],
)
def test_collection_target_outside_staging_refuses_before_any_producer_call(
    monkeypatch, environment, resolved
):
    fixture = Fixture(monkeypatch)
    handlers = fixture.handlers()
    monkeypatch.delenv("TRENDS_ENV")
    monkeypatch.delenv("BIGQUERY_DATASET")
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)
    assert get_dataset() == resolved
    profile = fixture.clients["collection_profile"]
    assert native.staging_target_refusal(profile) == "collection_target_not_staging"
    result = handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "failed"
    assert result["retry_safe"] is True
    assert result["result_reference"] == "collect_refused:collection_target_not_staging"
    assert result["input_digest"] == native.manifest_digest(manifest("collect"))
    with pytest.raises(StageRefusal, match=r"^collection_target_not_staging$"):
        fixture.clients["collector"](stop_after_ingestion=True, attempt_id="attempt:collect:1")
    assert fixture.terminal.calls == []
    assert fixture.clients["collection_ledger"].operation("attempt:collect:1") is None


def test_collection_target_at_the_staging_dataset_reaches_the_producer_once(monkeypatch):
    fixture = Fixture(monkeypatch)
    assert get_dataset() == "intelligence_42_sources_staging"
    assert native.staging_target_refusal(fixture.clients["collection_profile"]) is None
    result = fixture.handlers()["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "succeeded"
    assert len(fixture.terminal.calls) == 1


# The deployed job's own environment is checked in
# tests/repository/test_deployed_daily_job_environment.py, which reads the root
# infra/runtime file that the engine build context does not carry.


def test_retried_attempt_under_another_execution_reconciles_through_the_ledger(monkeypatch):
    fixture = Fixture(monkeypatch)
    first = fixture.handlers()["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert first["state"] == "succeeded"
    assert len(fixture.terminal.calls) == 1
    assert fixture.terminal.calls[0]["collection_authority"] == EXACT_AUTHORITY
    ledger = fixture.clients["collection_ledger"]
    assert ledger.operation("attempt:collect:1") == collection_receipt()
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "intelligence-42-daily-staging-retry2")
    again = native.guarded_stage_handlers(fixture.clients, profile=kernel_profile())["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert again == first
    assert len(fixture.terminal.calls) == 1
    assert ledger.operation("intelligence-42-daily-staging-retry2") is None
    assert ledger.operation(EXECUTION_NAME) is None
    assert ledger.market_day(CUTOFF.date().isoformat()) == [collection_receipt()]


def test_daily_invocation_with_an_empty_environment_records_the_target_refusal(monkeypatch):
    fixture = Fixture(monkeypatch)
    monkeypatch.delenv("TRENDS_ENV")
    monkeypatch.delenv("BIGQUERY_DATASET")
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "collect"}
    collect = fixture.kernel_store.read_stage(SLOT, "collect")
    assert collect["state"] == "failed"
    assert collect["retry_safe"] is True
    assert collect["result_reference"] == "collect_refused:collection_target_not_staging"
    assert fixture.terminal.calls == []
    assert fixture.bigquery.calls == []


def test_collect_stage_runs_end_to_end_through_the_native_clients(monkeypatch):
    fixture = Fixture(monkeypatch)
    handlers = fixture.handlers()
    assert set(handlers) == set(STAGE_ORDER)
    result = handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "succeeded", result
    assert result["result_reference"] == "source_run:collect-2026-09-10"
    assert result["retry_safe"] is False
    assert len(fixture.terminal.calls) == 1
    validated = read_completed_source_run(fixture.clients["source_runs"].read, "collect-2026-09-10")
    assert validated["observation_window_end"] == CUTOFF
    assert validated["receipt"]["policy_sha256"] == POLICY_DIGEST
    assert validated["collection_started_at"] >= CUTOFF
    assert validated["collection_completed_at"] > validated["collection_started_at"]
    again = handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert again == result
    assert len(fixture.terminal.calls) == 1
    assert fixture.bigquery.calls == []


def test_kernel_stops_at_capture_with_a_retry_safe_refusal_never_unknown(monkeypatch):
    # The seam is bound and the active generation carries the v2 capture origin; a receipt
    # that carries no issued execution authority (an attempt issued by another process)
    # refuses before the client is reached, retry safe, and the runner is never called.
    fixture = Fixture(monkeypatch)
    issuer = FakeAuthority(deny=("release",))
    result = fixture.run(authority=issuer)
    assert result == {"state": "failed", "operation_id": SLOT, "stage": "capture"}
    collect = fixture.kernel_store.read_stage(SLOT, "collect")
    assert collect["state"] == "succeeded"
    capture = fixture.kernel_store.read_stage(SLOT, "capture")
    assert capture["state"] == "failed"
    assert capture["retry_safe"] is True
    assert capture["output_digest"] == ""
    assert capture["result_reference"] == "capture_refused:capture_authority_unbound"
    assert fixture.kernel_store.read_stage(SLOT, "compose") is None
    assert len(fixture.terminal.calls) == 1
    assert fixture.bigquery.calls == []
    assert fixture.run(authority=issuer) == {
        "state": "failed",
        "operation_id": SLOT,
        "stage": "capture",
    }
    assert len(fixture.terminal.calls) == 1
    assert not any(name.startswith(native.CAPTURE_PREFIX) for name in fixture.object_client.objects)


def test_guard_refuses_each_stage_whose_clients_are_unbound_before_any_native_call(monkeypatch):
    fixture = Fixture(monkeypatch)
    handlers = fixture.handlers()
    expected = {"compose": "compose_refused:client_unbound:composer"}
    for stage, reference in expected.items():
        result = handlers[stage](manifest=manifest(stage), authority_receipt=receipt_for(stage))
        assert result["state"] == "failed", stage
        assert result["retry_safe"] is True
        assert result["result_reference"] == reference
        assert result["input_digest"] == native.manifest_digest(manifest(stage))
    # capture, certify and release carry no unbound client and no pre dispatch refusal, so
    # they are not guarded: over a valid slot each reaches its handler and refuses, retry
    # safe, on the predecessor record the store does not hold.
    for stage, reference in (
        ("capture", "capture_refused:collect_record_unavailable"),
        ("certify", "certify_refused:compose_record_unavailable"),
        ("release", "release_refused:certify_record_unavailable"),
    ):
        slot_manifest = manifest(stage, operation_id=SLOT)
        result = handlers[stage](manifest=slot_manifest, authority_receipt=receipt_for(stage))
        assert result["state"] == "failed", stage
        assert result["retry_safe"] is True
        assert result["result_reference"] == reference
        assert result["input_digest"] == native.manifest_digest(slot_manifest)
    assert fixture.bigquery.calls == []
    with pytest.raises(ValueError, match=r"^stage_manifest_invalid$"):
        handlers["capture"](manifest="not a mapping", authority_receipt=receipt_for("capture"))


def test_one_text_rule_guards_every_field_and_object_names_refuse_a_traversal():
    # The hardening is the geographic scope kernel's own rule, imported rather than
    # restated, and it is applied at every call site of this module rather than only at
    # the four fields of the compose binding. They are the same shape of field everywhere,
    # and a second weaker rule for the identifiers that key the object store is the
    # disagreement this closes rather than opens. It does alias two spellings of one
    # identifier onto one object name, since stripping and NFKC are both many to one, and
    # that aliasing is answered writer by writer, not by one rule: four of the five
    # writers are create only and refuse a second spelling carrying anything different,
    # and the fifth, the day index, is a read, append and replace under the generation it
    # read, which is deduplicated on the normalised id instead. See the note beside _text.
    assert native._text is not native._name
    for value in ("\u200b", "run\u200b1", "run\u202e1", "run\u2028 1", "\U0001f600" * 256, "  "):
        with pytest.raises(ValueError, match=r"^text_invalid$"):
            native._text(value, "text_invalid")
    assert native._text("  run_1  ", "text_invalid") == "run_1"
    assert native._name("  run_1  ", "text_invalid") == "run_1"
    # Where a value goes on to build an object name it may not walk out of its prefix.
    for value in ("../../secrets", "..", "runs/../../secrets", "/absolute"):
        with pytest.raises(ValueError, match=r"^text_invalid$"):
            native._name(value, "text_invalid")
    client = FakeObjectClient()
    ledger = native.ObjectCollectionLedger(client)
    with pytest.raises(ValueError, match=r"^collection_operation_invalid$"):
        ledger.operation("../../secrets")
    with pytest.raises(ValueError, match=r"^collection_day_invalid$"):
        ledger.market_day("../2026-09-12")
    with pytest.raises(ValueError, match=r"^collection_receipt_invalid$"):
        ledger.record({"execution_id": "../../secrets"}, trend_date="2026-09-12")
    with pytest.raises(ValueError, match=r"^collection_day_invalid$"):
        ledger.record({"execution_id": "attempt:collect:1"}, trend_date="../2026-09-12")
    with pytest.raises(ValueError, match=r"^source_run_id_invalid$"):
        native.ObjectSourceRuns(client).record("../../secrets", {})
    assert client.writes == []
    # The aliasing the stripping introduces fails closed rather than overwriting: a
    # differing receipt under a name the store already holds refuses.
    ledger.record({"execution_id": "attempt:collect:1"}, trend_date="2026-09-12")
    with pytest.raises(ValueError, match=r"^collection_receipt_conflict$"):
        ledger.record(
            {"execution_id": " attempt:collect:1 ", "extra": "differs"}, trend_date="2026-09-12"
        )


def test_the_same_padded_receipt_recorded_twice_is_one_day_index_entry():
    # The ledger's own contract is that recording the same receipt again is a no op. It was
    # not, for any execution id not already in NFKC normal form: record normalised the id
    # for the object name and for the membership test and appended the raw receipt to the
    # day index, so the test compared a normalised value against a raw one, never found it,
    # and appended a second copy. Under a retry loop the day index grew once per retry.
    # The existing idempotency test uses a canonical id, where the raw and the normalised
    # value are the same string, and the text rule test records a canonical id and then a
    # padded receipt that also differs in a field, which the create only receipt object
    # refuses before the day index is reached. Neither reaches this. This records the SAME
    # padded receipt twice and nothing else.
    # The normalised spelling each of the three reaches is pinned as a literal here. Read
    # off the function under test it would have proved only that record is consistent with
    # itself: if the guard stopped normalising, the expected value would move with it and
    # every assertion below would still hold.
    for execution_id, normalised in (
        ("attempt:collect:1 ", "attempt:collect:1"),
        (" attempt:collect:1", "attempt:collect:1"),
        ("\uff41ttempt:collect:1", "attempt:collect:1"),
    ):
        assert execution_id != normalised
        assert native._name(execution_id, "unused") == normalised
        client = FakeObjectClient()
        ledger = native.ObjectCollectionLedger(client)
        receipt = collection_receipt(execution_id=execution_id)
        ledger.record(receipt, trend_date="2026-09-11")
        ledger.record(dict(receipt), trend_date="2026-09-11")
        day = ledger.market_day("2026-09-11")
        assert len(day) == 1
        # The day index entry, the receipt object and the object name carry one value: the
        # normalised one. A payload carrying the raw value under a normalised name is a
        # record that disagrees with its own key.
        assert day == [collection_receipt(execution_id=normalised)]
        assert ledger.operation(execution_id) == collection_receipt(execution_id=normalised)
        assert sorted(client.objects) == [
            "42/daily/collection/days/2026-09-11.json",
            f"42/daily/collection/receipts/{normalised}.json",
        ]
        # And the second record wrote nothing at all: one receipt create, one day index
        # write, and no second append under any generation.
        assert client.writes == [
            (f"42/daily/collection/receipts/{normalised}.json", 0),
            ("42/daily/collection/days/2026-09-11.json", 0),
        ]


def test_a_capture_result_readback_is_refused_when_it_names_another_attempt():
    # NativeCapture.verified answered whatever object sat under the normalised attempt
    # name, and the readback field set carries a result id, digests, a state and two
    # instants and no attempt identity, so there was nothing to compare it against. The
    # capture handler reads a non None readback as proof that a native result already
    # exists for this attempt, reconciles it, never snapshots again, and registers a
    # capture entry whose operation id is this attempt carrying another attempt's result id
    # and digests. Normalising is many to one, so two attempt ids reaching one name is
    # exactly the case the object store cannot tell apart.
    client = FakeObjectClient()
    results = native.ObjectCaptureResults(client)
    readback = {
        "result_id": "res-1",
        "content_digest": "c" * 64,
        "image_digest": "b" * 64,
        "generation": "7",
        "completion_state": "succeeded",
        "capture_available_at": COMPLETED,
        "snapshot_as_of": STARTED,
        "scope": "scope-1",
        "schema_digest": "d" * 64,
        "source_digest": "e" * 64,
        "market_scope": ["ke", "ng", "za"],
        "creation_records": creation_records(),
    }
    # The store writes the readback field set and the attempt beside it, and nothing else:
    # a record whose field set is not that set would be written and then refused on every
    # readback, which is a write that can never be read.
    with pytest.raises(ValueError, match=r"^capture_result_record_invalid$"):
        results.record("attempt:capture:1", {**readback, "attempt_id": "attempt:capture:1"})
    with pytest.raises(ValueError, match=r"^capture_result_record_invalid$"):
        results.record(
            "attempt:capture:1", {field: readback[field] for field in list(readback)[:-1]}
        )
    assert client.objects == {}
    results.record("attempt:capture:1", readback)
    assert results.read("attempt:capture:1") == readback
    # The same attempt under a second spelling reaches the same object, and the stored
    # attempt is what decides, not the name: it is the same attempt, so it reconciles.
    assert results.read(" attempt:capture:1 ") == readback
    # A second attempt whose result was filed under this name is refused rather than
    # answered as this attempt's. The object is written by hand here because record itself
    # refuses to overwrite, which is the create only half of the same guard.
    name = "42/daily/captures/results/attempt:capture:1.json"
    client.objects[name] = (
        canonical_bytes(
            {
                **{
                    field: value.isoformat() if hasattr(value, "isoformat") else value
                    for field, value in readback.items()
                },
                "attempt_id": "attempt:capture:2",
            }
        ),
        1,
    )
    with pytest.raises(ValueError, match=r"^capture_result_attempt_differs$"):
        results.read("attempt:capture:1")
    assert native.CAPTURE_RESULT_ATTEMPT_REFUSAL == "capture_result_attempt_differs"
    # A stored result that names no attempt cannot be compared at all, so it is refused
    # rather than trusted: failing open is the defect, and an uncomparable readback is the
    # shape the defect had. It is refused under its own code and not as an unparseable
    # record, because that field set is what every revision before the attempt field wrote
    # and the bucket holds those objects; see the test that pins the condition.
    client.objects[name] = (
        canonical_bytes(
            {
                field: value.isoformat() if hasattr(value, "isoformat") else value
                for field, value in readback.items()
            }
        ),
        1,
    )
    with pytest.raises(ValueError, match=r"^capture_result_attempt_absent$"):
        results.read("attempt:capture:1")
    # The attempt the store writes beside the readback is the normalised one and not the
    # spelling it was handed, which is what lets the comparison hold at all: the object
    # name is normalised either way, so a stored attempt carrying the raw spelling would
    # refuse its own record on the next readback.
    padded = FakeObjectClient()
    native.ObjectCaptureResults(padded).record(" attempt:capture:1 ", readback)
    assert native.ObjectCaptureResults(padded).read("attempt:capture:1") == readback


def test_the_capture_stage_refuses_a_readback_recorded_for_another_attempt(monkeypatch):
    # The behaviour at the stage, driven through the handler rather than asserted of the
    # client alone: an attempt whose results object holds another attempt's result refuses
    # under that condition's own code and registers no capture entry naming this attempt
    # while carrying the other's result id and digests. The earlier form of this test
    # called capture.verified directly, which the client level test above already covers,
    # and asserted neither half of what its name claims.
    learner = SnapshotRunner()
    Fixture(monkeypatch, capture_runner=learner).run()
    attempt = learner.calls[0]["attempt_id"]
    runner = SnapshotRunner()
    fixture = Fixture(monkeypatch, capture_runner=runner)
    readback = {
        "result_id": "res-other",
        "content_digest": "c" * 64,
        "image_digest": "b" * 64,
        "generation": "7",
        "completion_state": "succeeded",
        "capture_available_at": COMPLETED,
        "snapshot_as_of": STARTED,
        "scope": "scope-1",
        "schema_digest": "d" * 64,
        "source_digest": "e" * 64,
        "market_scope": ["ke", "ng", "za"],
        "creation_records": creation_records(),
    }
    other = "attempt:capture:2"
    assert other != attempt
    native.ObjectCaptureResults(fixture.object_client).record(other, readback)
    stored, generation = fixture.object_client.objects[
        f"{native.CAPTURE_PREFIX}/results/{other}.json"
    ]
    fixture.object_client.objects[f"{native.CAPTURE_PREFIX}/results/{attempt}.json"] = (
        stored,
        generation,
    )
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "capture"}
    record = fixture.kernel_store.read_stage(SLOT, "capture")
    assert record["state"] == "failed"
    assert record["result_reference"] == "capture_refused:capture_result_attempt_differs"
    assert record["output_digest"] == ""
    # The runner was never dispatched and no capture entry was registered under this
    # attempt, which is the half the name claims and nothing asserted.
    assert runner.calls == []
    assert not any(
        name.startswith(f"{native.CAPTURE_PREFIX}/registry/")
        for name in fixture.object_client.objects
    )
    assert fixture.bigquery.calls == []
    # The client level refusal the stage carries is still the client's own.
    with pytest.raises(ValueError, match=r"^capture_result_attempt_differs$"):
        fixture.clients["capture"].verified(attempt)


def test_the_capture_stage_names_a_result_recorded_before_the_attempt_field(monkeypatch):
    # A results object written by any revision before the attempt field existed reaches
    # the stage as its own named refusal rather than as the one unresolved string every
    # ValueError from a native call collapses into, so a record written under the older
    # rule is distinguishable from a corrupt one at the operator's boundary.
    learner = SnapshotRunner()
    Fixture(monkeypatch, capture_runner=learner).run()
    attempt = learner.calls[0]["attempt_id"]
    runner = SnapshotRunner()
    fixture = Fixture(monkeypatch, capture_runner=runner)
    fixture.object_client.objects[f"{native.CAPTURE_PREFIX}/results/{attempt}.json"] = (
        canonical_bytes(
            {
                "result_id": "res-legacy",
                "content_digest": "c" * 64,
                "image_digest": "b" * 64,
                "generation": "7",
                "completion_state": "succeeded",
                "capture_available_at": COMPLETED.isoformat(),
                "snapshot_as_of": STARTED.isoformat(),
                "scope": "scope-1",
                "schema_digest": "d" * 64,
                "source_digest": "e" * 64,
            }
        ),
        1,
    )
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "capture"}
    record = fixture.kernel_store.read_stage(SLOT, "capture")
    assert record["result_reference"] == "capture_refused:capture_result_attempt_absent"
    assert runner.calls == []
    assert not any(
        name.startswith(f"{native.CAPTURE_PREFIX}/registry/")
        for name in fixture.object_client.objects
    )


def test_a_receipt_recorded_before_the_id_was_normalised_is_read_not_refused():
    # The day index fix changed the stored payload: the execution id it carries is now the
    # normalised one. Objects written by any earlier revision carry the raw spelling under
    # the same normalised name, so the payload this module now computes no longer equals
    # the bytes already in the bucket, and the create only comparison refused them as a
    # conflicting receipt. That is a durable store with no version field, so the read is
    # what has to widen: a stored receipt that differs from the computed one only in the
    # spelling of the execution id is the same receipt written under the older rule, and
    # is tolerated rather than refused. Nothing is rewritten, and the condition a record
    # written under the older rule that also differs in a field reaches is named on its
    # own account rather than arriving as an ordinary conflict.
    raw = "attempt:collect:1 "
    normalised = "attempt:collect:1"
    legacy = collection_receipt(execution_id=raw)
    for trend_date in ("2026-09-11",):
        client = FakeObjectClient()
        client.objects[f"42/daily/collection/receipts/{normalised}.json"] = (
            canonical_bytes(legacy),
            1,
        )
        client.objects[f"42/daily/collection/days/{trend_date}.json"] = (
            canonical_bytes({"receipts": [legacy]}),
            1,
        )
        ledger = native.ObjectCollectionLedger(client)
        ledger.record(dict(legacy), trend_date=trend_date)
        # No byte moved: the stored receipt and the stored day index are exactly what an
        # earlier revision wrote, and the day index did not gain a second entry beside the
        # one already there.
        assert client.writes == []
        assert ledger.market_day(trend_date) == [legacy]
        assert ledger.operation(raw) == legacy
        # A record written under the older rule that also differs in a field is still
        # refused, under its own name so an operator can tell it from a receipt recorded
        # twice with different contents under a canonical id.
        with pytest.raises(ValueError, match=r"^collection_receipt_legacy_id$"):
            ledger.record({**legacy, "raw_rows_persisted": 11}, trend_date=trend_date)
        assert native.COLLECTION_RECEIPT_LEGACY_REFUSAL == "collection_receipt_legacy_id"
        assert client.writes == []


def test_the_day_index_holds_one_entry_beside_a_legacy_entry_under_a_second_spelling():
    # The membership test compares the normalised execution id against what each stored
    # entry names. An entry written before the change names the raw spelling, so comparing
    # it as stored never matched and a second entry was appended beside it on every retry.
    # Normalising both sides of the comparison is a read widening: for an entry written
    # under the current rule the two are the same string.
    client = FakeObjectClient()
    legacy = collection_receipt(execution_id="\uff41ttempt:collect:1")
    client.objects["42/daily/collection/days/2026-09-11.json"] = (
        canonical_bytes({"receipts": [legacy]}),
        1,
    )
    ledger = native.ObjectCollectionLedger(client)
    ledger.record(collection_receipt(execution_id="attempt:collect:1"), trend_date="2026-09-11")
    assert ledger.market_day("2026-09-11") == [legacy]
    assert client.writes == [("42/daily/collection/receipts/attempt:collect:1.json", 0)]


def test_the_day_index_refuses_when_every_write_attempt_loses_its_generation():
    # Under sustained contention the ledger has to say so. Replacing the refusal with a
    # bare return reported success with the receipt absent from the index, and nothing in
    # the suite noticed: the existing race test covers the receipt create race only.
    class ContendedIndex(FakeObjectClient):
        def write(self, name, payload, *, if_generation_match):
            if name.startswith("42/daily/collection/days/"):
                current = self.objects.get(name)
                self.objects[name] = (
                    canonical_bytes({"receipts": []}),
                    (current[1] if current else 0) + 1,
                )
                raise PreconditionFailed(name)
            return super().write(name, payload, if_generation_match=if_generation_match)

    client = ContendedIndex()
    ledger = native.ObjectCollectionLedger(client)
    with pytest.raises(ValueError, match=r"^collection_day_contended$"):
        ledger.record(collection_receipt(), trend_date="2026-09-11")
    assert ledger.market_day("2026-09-11") == []


def test_a_capture_result_stored_before_the_attempt_field_names_its_own_condition():
    # The attempt field was added to the stored object with no version field, no legacy
    # branch and no backfill, and the read required it: every object written by an earlier
    # revision was refused as an unparseable record, which the stage collapses into one
    # opaque unknown. The read now tolerates the field set an earlier revision wrote and
    # answers a condition of its own, so a record written before the change is
    # distinguishable from a corrupt one and from another attempt's result. It is still
    # refused rather than answered: a stored result that names no attempt cannot be shown
    # to belong to this attempt, and answering it is the fail open defect the attempt
    # field closes.
    client = FakeObjectClient()
    readback = {
        "result_id": "res-1",
        "content_digest": "c" * 64,
        "image_digest": "b" * 64,
        "generation": "7",
        "completion_state": "succeeded",
        "capture_available_at": COMPLETED,
        "snapshot_as_of": STARTED,
        "scope": "scope-1",
        "schema_digest": "d" * 64,
        "source_digest": "e" * 64,
    }
    legacy = canonical_bytes(
        {
            field: value.isoformat() if hasattr(value, "isoformat") else value
            for field, value in readback.items()
        }
    )
    name = f"{native.CAPTURE_PREFIX}/results/attempt:capture:1.json"
    client.objects[name] = (legacy, 1)
    results = native.ObjectCaptureResults(client)
    with pytest.raises(ValueError, match=r"^capture_result_attempt_absent$"):
        results.read("attempt:capture:1")
    assert native.CAPTURE_RESULT_LEGACY_REFUSAL == "capture_result_attempt_absent"
    # The three conditions are three codes, not one: a record written under the older
    # rule, another attempt's result, and bytes this module cannot read at all.
    client.objects[name] = (
        canonical_bytes({**json.loads(legacy), "attempt_id": "attempt:capture:2"}),
        1,
    )
    with pytest.raises(ValueError, match=r"^capture_result_attempt_differs$"):
        results.read("attempt:capture:1")
    client.objects[name] = (canonical_bytes({"unexpected": "field"}), 1)
    with pytest.raises(ValueError, match=r"^capture_result_record_invalid$"):
        results.read("attempt:capture:1")
    # The two attempt conditions are stage refusals, so the handler records them by their
    # own code instead of the one unresolved string every ValueError reaches it as.
    assert issubclass(StageRefusal, ValueError)
    client.objects[name] = (legacy, 1)
    with pytest.raises(StageRefusal):
        results.read("attempt:capture:1")
    assert client.objects[name] == (legacy, 1)
    assert client.writes == []


def test_the_capture_attempt_id_is_refused_when_it_would_leave_its_prefix():
    # capture_attempt_invalid is what keeps the results object inside its own prefix, and
    # it is reached by both the read and the record.
    results = native.ObjectCaptureResults(FakeObjectClient())
    for value in ("../../secrets", "", "  ", None, "/absolute"):
        with pytest.raises(ValueError, match=r"^capture_attempt_invalid$"):
            results.read(value)
        with pytest.raises(ValueError, match=r"^capture_attempt_invalid$"):
            results.record(value, {})


def test_the_capture_results_record_refuses_a_write_it_cannot_read_back():
    # The create lost its generation and the read that follows finds nothing: the durable
    # outcome cannot be established, so record refuses rather than reporting success. No
    # test reached this branch.
    class LostWrite(FakeObjectClient):
        def write(self, name, payload, *, if_generation_match):
            raise PreconditionFailed(name)

    readback = {
        "result_id": "res-1",
        "content_digest": "c" * 64,
        "image_digest": "b" * 64,
        "generation": "7",
        "completion_state": "succeeded",
        "capture_available_at": COMPLETED,
        "snapshot_as_of": STARTED,
        "scope": "scope-1",
        "schema_digest": "d" * 64,
        "source_digest": "e" * 64,
        "market_scope": ["ke", "ng", "za"],
        "creation_records": creation_records(),
    }
    client = LostWrite()
    with pytest.raises(ValueError, match=r"^capture_result_conflict$"):
        native.ObjectCaptureResults(client).record("attempt:capture:1", readback)
    assert client.objects == {}


def test_the_capture_registry_key_is_normalised_part_by_part_not_as_one_string():
    # _name was applied to the joined "<profile>/<result>" key, so only the outer edges of
    # the whole string were stripped and an interior spelling reached the object name
    # unchanged: four spellings of one result id created two distinct objects, one of them
    # named with a space in it. That is the record that disagrees with its own key the
    # collection receipt fix declared unacceptable. Each part of the composite key is now
    # judged on its own, so the four spellings reach one name and the create only rule
    # decides, and a part that would walk out of the prefix still refuses.
    client = FakeObjectClient()
    writer = native.ObjectCaptureRegistry(client)
    entry = {
        "profile_id": "staging-2026-09-10-eeeeeeeeeeeeeeee",
        "observation_window_end": CUTOFF,
        "expires_at": None,
        "source_tables": ("a", "b"),
        "generation": "7",
    }
    spellings = ("res-1", " res-1", "res-1 ", "\uff52es-1")
    answers = [writer.create(f"profile-a/{spelling}", entry) for spelling in spellings]
    assert answers == [{"status": "created"}] + [{"status": "exists"}] * 3
    assert list(client.objects) == [f"{native.CAPTURE_PREFIX}/registry/profile-a/res-1.json"]
    assert all(writer.read(f"profile-a/{spelling}") is not None for spelling in spellings)
    for value in (
        "profile-a/../../secrets",
        "../profile-a/res-1",
        "/profile-a/res-1",
        "a//b",
        "profile-a/res-1/extra",
        "res-1",
        None,
    ):
        with pytest.raises(ValueError, match=r"^capture_key_invalid$"):
            writer.read(value)
        with pytest.raises(ValueError, match=r"^capture_key_invalid$"):
            writer.create(value, entry)
    assert len(client.objects) == 1


def test_the_capture_runner_is_handed_the_normalised_attempt_and_the_store_agrees():
    # The runner is handed the same attempt the results object is named and keyed by. It
    # was handed the raw spelling, which is unobservable to the store but is what the
    # runner records against its own execution.
    client = FakeObjectClient()
    runner = SnapshotRunner()
    capture = native.NativeCapture(object_client=client, warehouse="warehouse", runner=runner)
    capture.snapshot(
        {"receipt": collection_receipt()},
        snapshot_as_of=COMPLETED + timedelta(minutes=3),
        attempt_id=" attempt:capture:1 ",
        origin="origin",
        authority=receipt_for("capture")["execution"],
    )
    assert runner.calls[0]["attempt_id"] == "attempt:capture:1"
    assert list(client.objects) == [f"{native.CAPTURE_PREFIX}/results/attempt:capture:1.json"]


def test_the_day_index_write_is_retried_under_a_generation_it_lost():
    # The day index write is a read, append and replace under the generation it read, so a
    # writer that loses the generation reads again and appends to what landed. Nothing
    # pinned the retry: reducing the attempts to one left every test in the suite passing.
    class LosesTheFirstWrite(FakeObjectClient):
        def __init__(self):
            super().__init__()
            self.lost = False

        def write(self, name, payload, *, if_generation_match):
            if name.startswith("42/daily/collection/days/") and not self.lost:
                self.lost = True
                other = collection_receipt(execution_id="attempt:collect:9")
                super().write(name, canonical_bytes({"receipts": [other]}), if_generation_match=0)
                raise PreconditionFailed(name)
            return super().write(name, payload, if_generation_match=if_generation_match)

    client = LosesTheFirstWrite()
    ledger = native.ObjectCollectionLedger(client)
    ledger.record(collection_receipt(), trend_date="2026-09-11")
    day = ledger.market_day("2026-09-11")
    assert [entry["execution_id"] for entry in day] == ["attempt:collect:9", "attempt:collect:1"]
    assert client.writes[-1] == ("42/daily/collection/days/2026-09-11.json", 1)


def test_a_receipt_create_that_loses_its_generation_and_finds_nothing_refuses():
    # The create lost the generation precondition and the read that follows finds no
    # object, so the durable outcome cannot be established and record refuses rather than
    # reporting a receipt it cannot see. The existing race test lands a competing create
    # and reads it back, which is the other half of the same branch.
    class LostCreate(FakeObjectClient):
        def write(self, name, payload, *, if_generation_match):
            if name.startswith("42/daily/collection/receipts/"):
                raise PreconditionFailed(name)
            return super().write(name, payload, if_generation_match=if_generation_match)

    client = LostCreate()
    with pytest.raises(ValueError, match=r"^collection_receipt_conflict$"):
        native.ObjectCollectionLedger(client).record(collection_receipt(), trend_date="2026-09-11")
    assert client.objects == {}


def test_a_day_index_object_that_is_not_a_list_of_entries_refuses():
    # The day index is read before every append, so a stored object that is not a list of
    # entries has to refuse rather than be appended to or silently replaced.
    for stored in ({"receipts": "not a list"}, {"receipts": ["not an entry"]}, {}):
        client = FakeObjectClient()
        client.objects["42/daily/collection/days/2026-09-11.json"] = (
            canonical_bytes(stored),
            1,
        )
        ledger = native.ObjectCollectionLedger(client)
        with pytest.raises(ValueError, match=r"^collection_day_invalid$"):
            ledger.market_day("2026-09-11")
        with pytest.raises(ValueError, match=r"^collection_day_invalid$"):
            ledger.record(collection_receipt(), trend_date="2026-09-11")
        assert client.objects["42/daily/collection/days/2026-09-11.json"][1] == 1


def test_the_verification_is_admitted_as_its_own_field_set_and_normalised_value():
    # The runner's verification is read into the stored readback, so its field set is
    # judged and the normalised value of each text field is what is kept. Discarding the
    # normalised return let the raw spelling flow into the entry payload and into the
    # registry key built from it.
    client = FakeObjectClient()
    runner = SnapshotRunner(verification={"result_id": " res_" + "2" * 32 + " "})
    capture = native.NativeCapture(object_client=client, warehouse="warehouse", runner=runner)
    capture.snapshot(
        {"receipt": collection_receipt()},
        snapshot_as_of=COMPLETED + timedelta(minutes=3),
        attempt_id="attempt:capture:1",
        origin="origin",
        authority=receipt_for("capture")["execution"],
    )
    assert capture.verified("attempt:capture:1")["result_id"] == "res_" + "2" * 32
    extra = native.NativeCapture(
        object_client=FakeObjectClient(),
        warehouse="warehouse",
        runner=SnapshotRunner(verification={"unexpected": "field"}),
    )
    with pytest.raises(ValueError, match=r"^capture_verification_invalid$"):
        extra.snapshot(
            {"receipt": collection_receipt()},
            snapshot_as_of=COMPLETED + timedelta(minutes=3),
            attempt_id="attempt:capture:1",
            origin="origin",
            authority=receipt_for("capture")["execution"],
        )
    for value in ("not a mapping", {}, {"result_id": "res-1"}):
        with pytest.raises(ValueError, match=r"^capture_verification_invalid$"):
            native._verification(value)


def test_no_model_client_or_cloud_module_is_imported_at_module_level():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name.endswith("_client")]
    assert not [name for name in imported if name.startswith(("google", "scripts.run_rss_now"))]


class RacingObjectClient(FakeObjectClient):
    """Lands a competing create for one name between a read that found nothing and the create."""

    def __init__(self, name, payload):
        super().__init__()
        self.race_name = name
        self.race_payload = payload
        self.raced = False

    def read(self, name):
        found = super().read(name)
        if name == self.race_name and not self.raced:
            self.raced = True
            super().write(name, self.race_payload, if_generation_match=0)
        return found


def test_ledger_record_absorbs_an_equal_concurrent_creator_and_refuses_a_differing_one():
    receipt = collection_receipt()
    name = "42/daily/collection/receipts/attempt:collect:1.json"
    equal = RacingObjectClient(name, canonical_bytes(receipt))
    ledger = native.ObjectCollectionLedger(equal)
    ledger.record(receipt, trend_date="2026-09-11")
    assert ledger.operation("attempt:collect:1") == receipt
    assert ledger.market_day("2026-09-11") == [receipt]
    assert equal.writes == [(name, 0), ("42/daily/collection/days/2026-09-11.json", 0)]
    differing = RacingObjectClient(name, canonical_bytes(collection_receipt(raw_rows_persisted=11)))
    ledger = native.ObjectCollectionLedger(differing)
    with pytest.raises(ValueError, match=r"^collection_receipt_conflict$"):
        ledger.record(receipt, trend_date="2026-09-11")
    assert differing.writes == [(name, 0)]
    assert differing.read("42/daily/collection/days/2026-09-11.json") is None


def test_source_run_record_refuses_a_concurrent_creator_as_exists():
    run = {
        "receipt": collection_receipt(),
        "collection_started_at": STARTED,
        "collection_completed_at": COMPLETED,
    }
    name = "42/daily/source_runs/collect-2026-09-10.json"
    racing = RacingObjectClient(name, b"{}")
    source_runs = native.ObjectSourceRuns(racing)
    with pytest.raises(ValueError, match=r"^source_run_exists$"):
        source_runs.record("collect-2026-09-10", run)
    assert racing.writes == [(name, 0)]


def test_stage_client_table_mirrors_the_handlers_and_covers_every_client():
    # Read from the handler bodies in daily_stages.py: _collect reaches the clock
    # (_now), the collection profile, the collector, the ledger and the source runs;
    # _capture the clock, the store (_predecessor), the source runs, the generation
    # (_capture_origin) and the capture client; _compose the clock, the store, the
    # capture writer (_admitted_capture), the composer, persist, the warehouse and the
    # release profile; _certify the store, the capture writer (_source_authority), the
    # certifier, the warehouse and the release profile; _release the clock, the store,
    # the certifier (the adapter's certify), the warehouse and the release profile.
    assert native.STAGE_CLIENTS == {
        "collect": (
            "clock",
            "collection_profile",
            "collector",
            "collection_ledger",
            "source_runs",
        ),
        "capture": ("clock", "store", "source_runs", "generation", "capture"),
        "compose": (
            "clock",
            "store",
            "capture",
            "composer",
            "persist",
            "warehouse",
            "release_profile",
            "products",
        ),
        "certify": ("store", "capture", "certifier", "warehouse", "release_profile", "products"),
        "release": ("clock", "store", "certifier", "warehouse", "release_profile", "products"),
    }
    assert tuple(native.STAGE_CLIENTS) == STAGE_ORDER
    assert set().union(*native.STAGE_CLIENTS.values()) == set(CLIENT_FIELDS)
    for stage, names in native.STAGE_CLIENTS.items():
        assert len(names) == len(set(names)), stage


def test_the_capture_readback_carries_the_native_result_own_coverage():
    """The recorded readback repeats the result's market scope and creation records exactly.

    The coverage the admission reads must come from the native result, so a scope or a
    record set that is not the expected one is carried through and refused later, never
    replaced by the expected values or by an empty record list.
    """
    inner = SnapshotRunner()
    scope = ["ng", "za"]
    records = creation_records(lanes=("raw_content",), states={"raw_content": "failed"})

    def runner(run, **kwargs):
        payload, status, verification = inner(run, **kwargs)
        return {**payload, "market_scope": scope, "creation_records": records}, status, verification

    capture = native.NativeCapture(
        object_client=FakeObjectClient(), warehouse="warehouse", runner=runner
    )
    snapshot_as_of = COMPLETED + timedelta(minutes=3)
    capture.snapshot(
        {"receipt": collection_receipt()},
        snapshot_as_of=snapshot_as_of,
        attempt_id="attempt:capture:9",
        origin="origin",
        authority=receipt_for("capture")["execution"],
    )
    verified = capture.verified("attempt:capture:9")
    assert verified["market_scope"] == scope
    assert verified["creation_records"] == records


@pytest.mark.parametrize("absent", [("scope",), ("market_scope",), ("creation_records",)])
def test_a_stored_result_of_any_other_shape_is_an_invalid_result_record(absent):
    """Reading the pre coverage shape is a named exception, not an open door."""
    import json

    client = FakeObjectClient()
    capture = native.NativeCapture(
        object_client=client, warehouse="warehouse", runner=SnapshotRunner()
    )
    capture.snapshot(
        {"receipt": collection_receipt()},
        snapshot_as_of=COMPLETED + timedelta(minutes=3),
        attempt_id="attempt:capture:1",
        origin="origin",
        authority=receipt_for("capture")["execution"],
    )
    name = f"{native.CAPTURE_PREFIX}/results/attempt:capture:1.json"
    recorded, generation = client.objects[name]
    values = {field: value for field, value in json.loads(recorded).items() if field not in absent}
    client.objects[name] = (json.dumps(values).encode("utf-8"), generation)
    with pytest.raises(ValueError, match="capture_result_record_invalid"):
        capture.verified("attempt:capture:1")


def test_a_result_recorded_before_the_coverage_is_readable_and_never_rewritten():
    """An attempt whose result object predates the coverage is read, not called corrupt.

    The result seam is create only and compares bytes, so the coverage can never be added
    to an object already written for that attempt. Refusing to read it would leave the
    attempt with no readable result and no way to record one, forever. It is read with the
    two coverage fields absent through the readback reader, which checks neither attempt
    nor coverage; the acting reader refuses it by name, because an object that names no
    attempt cannot be shown to be this attempt's, and the admission refuses the coverage
    it lacks by its own name.
    """
    import json

    client = FakeObjectClient()
    capture = native.NativeCapture(
        object_client=client, warehouse="warehouse", runner=SnapshotRunner()
    )
    snapshot_as_of = COMPLETED + timedelta(minutes=3)
    capture.snapshot(
        {"receipt": collection_receipt()},
        snapshot_as_of=snapshot_as_of,
        attempt_id="attempt:capture:1",
        origin="origin",
        authority=receipt_for("capture")["execution"],
    )
    name = f"{native.CAPTURE_PREFIX}/results/attempt:capture:1.json"
    recorded, generation = client.objects[name]
    current = json.loads(recorded)
    older = {
        field: value
        for field, value in current.items()
        if field in native.CAPTURE_PRE_COVERAGE_READBACK_FIELDS
    }
    assert set(older) == native.CAPTURE_PRE_COVERAGE_READBACK_FIELDS
    client.objects[name] = (json.dumps(older).encode("utf-8"), generation)
    stored = native.ObjectCaptureResults(client).read_stored("attempt:capture:1")
    assert set(stored) == native.CAPTURE_PRE_COVERAGE_READBACK_FIELDS
    assert stored["snapshot_as_of"] == snapshot_as_of.isoformat()
    with pytest.raises(ValueError, match=r"^capture_result_attempt_absent$"):
        capture.verified("attempt:capture:1")
    readback = {
        field: value
        for field, value in current.items()
        if field != native.CAPTURE_RESULT_ATTEMPT_FIELD
    }
    with pytest.raises(ValueError, match="capture_result_conflict"):
        native.ObjectCaptureResults(client).record("attempt:capture:1", readback)


def test_the_stored_reader_returns_any_older_or_current_result_object_unchanged():
    """The readback reader answers the stored mapping as it is, whatever shape it has.

    It is the reader for readback and replay, never for acting: it checks neither the
    attempt the object names nor the coverage it carries, so a result recorded under any
    earlier rule stays readable while ``read`` and ``verified`` keep refusing to act on it.
    """
    client = FakeObjectClient()
    capture = native.NativeCapture(
        object_client=client, warehouse="warehouse", runner=SnapshotRunner()
    )
    capture.snapshot(
        {"receipt": collection_receipt()},
        snapshot_as_of=COMPLETED + timedelta(minutes=3),
        attempt_id="attempt:capture:1",
        origin="origin",
        authority=receipt_for("capture")["execution"],
    )
    name = f"{native.CAPTURE_PREFIX}/results/attempt:capture:1.json"
    recorded, generation = client.objects[name]
    current = json.loads(recorded)
    assert set(current) == CAPTURE_READBACK_FIELDS | {native.CAPTURE_RESULT_ATTEMPT_FIELD}
    results = native.ObjectCaptureResults(client)
    # The thirteen field object every current write produces comes back as stored, with
    # the attempt field still in it and the instants still text.
    assert results.read_stored("attempt:capture:1") == current
    assert results.read_stored(" attempt:capture:1 ") == current
    # The ten field object an earlier revision wrote comes back as stored too, where the
    # acting reader refuses it by name.
    older = {
        field: value
        for field, value in current.items()
        if field in native.CAPTURE_PRE_COVERAGE_READBACK_FIELDS
    }
    client.objects[name] = (json.dumps(older).encode("utf-8"), generation)
    assert results.read_stored("attempt:capture:1") == older
    with pytest.raises(ValueError, match=r"^capture_result_attempt_absent$"):
        results.read("attempt:capture:1")
    assert results.read_stored("attempt:capture:2") is None
    client.objects[name] = (b"not json", generation)
    with pytest.raises(ValueError, match=r"^capture_result_record_invalid$"):
        results.read_stored("attempt:capture:1")


def test_capture_client_answers_the_fake_capture_contract_over_the_runner_boundary():
    client = FakeObjectClient()
    runner = SnapshotRunner()
    capture = native.NativeCapture(object_client=client, warehouse="warehouse", runner=runner)
    assert capture.pre_dispatch_refusal() is None
    assert capture.verified("attempt:capture:1") is None
    run = {"receipt": collection_receipt()}
    snapshot_as_of = COMPLETED + timedelta(minutes=3)
    carried = receipt_for("capture")["execution"]
    payload, status = capture.snapshot(
        run,
        snapshot_as_of=snapshot_as_of,
        attempt_id="attempt:capture:1",
        origin="origin",
        authority=carried,
    )
    assert status == "succeeded"
    assert set(payload) == capture_script._COMPACT_RESULT_FIELDS
    assert payload["source_as_of"] == snapshot_as_of.isoformat()
    call = runner.calls[0]
    assert call["run"] is run
    assert call["cutoff"] == snapshot_as_of.date() - timedelta(days=1)
    assert call["attempt_id"] == "attempt:capture:1"
    assert call["origin"] == "origin"
    assert call["client"] == "warehouse"
    assert call["authority"] is carried
    verified = capture.verified("attempt:capture:1")
    assert set(verified) == CAPTURE_READBACK_FIELDS
    assert verified == {
        "result_id": "res_" + "2" * 32,
        "content_digest": payload["snapshot_digest"],
        "image_digest": "4" * 64,
        "generation": "7",
        "completion_state": "succeeded",
        "capture_available_at": snapshot_as_of + timedelta(minutes=1),
        "snapshot_as_of": snapshot_as_of,
        "scope": "ogilvy_default",
        "schema_digest": "2" * 64,
        "source_digest": "3" * 64,
        "market_scope": payload["market_scope"],
        "creation_records": payload["creation_records"],
    }
    assert f"{native.CAPTURE_PREFIX}/results/attempt:capture:1.json" in client.objects
    assert capture.verified("attempt:capture:2") is None
    # The same runner result again under the same attempt is a no op; a differing one refuses.
    writes = len(client.writes)
    capture.snapshot(
        run,
        snapshot_as_of=snapshot_as_of,
        attempt_id="attempt:capture:1",
        origin="origin",
        authority=carried,
    )
    assert len(client.writes) == writes
    other = native.NativeCapture(
        object_client=client,
        warehouse="warehouse",
        runner=SnapshotRunner(verification={"generation": "8"}),
    )
    with pytest.raises(ValueError, match=r"^capture_result_conflict$"):
        other.snapshot(
            run,
            snapshot_as_of=snapshot_as_of,
            attempt_id="attempt:capture:1",
            origin="origin",
            authority=carried,
        )
    assert capture.verified("attempt:capture:1") == verified
    invalid = native.NativeCapture(
        object_client=client,
        warehouse="warehouse",
        runner=SnapshotRunner(verification={"image_digest": "not a digest"}),
    )
    with pytest.raises(ValueError, match=r"^capture_verification_invalid$"):
        invalid.snapshot(
            run,
            snapshot_as_of=snapshot_as_of,
            attempt_id="attempt:capture:3",
            origin="origin",
            authority=carried,
        )
    assert capture.verified("attempt:capture:3") is None


def test_capture_registry_writer_answers_the_fake_writer_contract():
    client = FakeObjectClient()
    writer = native.ObjectCaptureRegistry(client)
    entry = {
        "profile_id": "staging-2026-09-10-eeeeeeeeeeeeeeee",
        "observation_window_end": CUTOFF,
        "expires_at": None,
        "source_tables": ("a", "b"),
        "generation": "7",
    }
    assert writer.read("staging-2026-09-10-eeeeeeeeeeeeeeee/res_1") is None
    assert writer.create("staging-2026-09-10-eeeeeeeeeeeeeeee/res_1", entry) == {
        "status": "created"
    }
    assert writer.create("staging-2026-09-10-eeeeeeeeeeeeeeee/res_1", entry) == {"status": "exists"}
    stored = writer.read("staging-2026-09-10-eeeeeeeeeeeeeeee/res_1")
    assert stored["observation_window_end"] == CUTOFF
    assert stored["expires_at"] is None
    assert stored["source_tables"] == ["a", "b"]
    assert client.writes == [
        (f"{native.CAPTURE_PREFIX}/registry/staging-2026-09-10-eeeeeeeeeeeeeeee/res_1.json", 0)
    ]

    class LostAcknowledgement(FakeObjectClient):
        def write(self, name, payload, *, if_generation_match):
            super().write(name, payload, if_generation_match=if_generation_match)
            raise OSError("acknowledgement lost")

    lost = native.ObjectCaptureRegistry(LostAcknowledgement())
    assert lost.create("profile/res_2", entry) == {"status": "ambiguous"}
    assert lost.read("profile/res_2")["generation"] == "7"


def test_capture_stage_runs_end_to_end_and_admits_through_the_registry(monkeypatch):
    runner = SnapshotRunner()
    fixture = Fixture(monkeypatch, capture_runner=runner)
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "compose"}
    result = fixture.kernel_store.read_stage(SLOT, "capture")
    assert result["state"] == "succeeded", result
    assert result["retry_safe"] is False
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["attempt_id"].startswith("attempt:capture:")
    assert call["client"] is fixture.bigquery
    assert call["run"]["receipt"]["execution_id"].startswith("attempt:collect:")
    assert call["snapshot_as_of"] >= call["run"]["collection_completed_at"]
    assert call["cutoff"] == CUTOFF.date() - timedelta(days=1)
    assert call["origin"].manifest_version == "open_intelligence_execution_manifest_v2"
    assert "source_snapshot_capture" in call["origin"].operation_bindings
    key = result["result_reference"].removeprefix("capture:")
    stored = validate_capture_entry(fixture.clients["capture"].writer.read(key))
    assert key == f"{stored['profile_id']}/{stored['result_id']}"
    assert stored["operation_id"] == call["attempt_id"]
    assert stored["snapshot_as_of"] == call["snapshot_as_of"]
    assert stored["capture_available_at"] == call["snapshot_as_of"] + timedelta(minutes=1)
    assert stored["observation_window_end"] == CUTOFF
    assert stored["policy_digest"] == POLICY_DIGEST
    assert stored["completion_state"] == "succeeded"
    assert stored["image_digest"] == "4" * 64
    assert stored["result_id"] == "res_" + "2" * 32
    assert f"{native.CAPTURE_PREFIX}/registry/{key}.json" in fixture.object_client.objects
    assert result["output_digest"] == canonical_digest(
        {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in stored.items()
        }
    )
    assert fixture.bigquery.calls == []


def test_daily_invocation_proceeds_past_capture_and_stops_at_compose(monkeypatch):
    runner = SnapshotRunner()
    fixture = Fixture(monkeypatch, capture_runner=runner)
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "compose"}
    capture = fixture.kernel_store.read_stage(SLOT, "capture")
    assert capture["state"] == "succeeded"
    compose = fixture.kernel_store.read_stage(SLOT, "compose")
    assert compose["state"] == "failed"
    assert compose["retry_safe"] is True
    assert compose["output_digest"] == ""
    assert compose["result_reference"] == "compose_refused:client_unbound:composer"
    assert fixture.kernel_store.read_stage(SLOT, "certify") is None
    assert len(runner.calls) == 1
    key = capture["result_reference"].removeprefix("capture:")
    entry = fixture.clients["capture"].writer.read(key)
    objects = dict(fixture.object_client.objects)
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "compose"}
    assert len(runner.calls) == 1
    assert len(fixture.terminal.calls) == 1
    assert fixture.kernel_store.read_stage(SLOT, "capture") == capture
    assert fixture.clients["capture"].writer.read(key) == entry
    assert {
        name: value
        for name, value in fixture.object_client.objects.items()
        if name.startswith(native.CAPTURE_PREFIX)
    } == {name: value for name, value in objects.items() if name.startswith(native.CAPTURE_PREFIX)}
    assert fixture.bigquery.calls == []


def test_capture_snapshots_the_receipts_closed_day_when_the_clock_is_two_days_on(monkeypatch):
    runner = SnapshotRunner()
    fixture = Fixture(monkeypatch, capture_runner=runner, start=STARTED + timedelta(days=2))
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "compose"}
    capture = fixture.kernel_store.read_stage(SLOT, "capture")
    assert capture["state"] == "succeeded", capture
    (call,) = runner.calls
    assert call["snapshot_as_of"].date() == CUTOFF.date() + timedelta(days=2)
    assert call["run"]["receipt"]["cutoff"] == "2026-09-10"
    assert call["cutoff"] == date(2026, 9, 10)
    assert call["cutoff"] != call["snapshot_as_of"].date() - timedelta(days=1)
    key = capture["result_reference"].removeprefix("capture:")
    stored = validate_capture_entry(fixture.clients["capture"].writer.read(key))
    assert stored["observation_window_end"] == CUTOFF
    assert stored["snapshot_as_of"] == call["snapshot_as_of"]
    client = native.NativeCapture(object_client=FakeObjectClient(), warehouse=None, runner=runner)
    run = {"receipt": collection_receipt()}
    carried = receipt_for("capture")["execution"]
    for bad in ({}, {"receipt": {}}, {"receipt": {"cutoff": "2026-09-10T00:00:00"}}):
        with pytest.raises(ValueError, match=r"^capture_run_invalid$"):
            client.snapshot(
                bad, snapshot_as_of=COMPLETED, attempt_id="a", origin=None, authority=carried
            )
    with pytest.raises(ValueError, match=r"^capture_cutoff_invalid$"):
        client.snapshot(
            run,
            snapshot_as_of=datetime(2026, 9, 10, 23, tzinfo=UTC),
            attempt_id="a",
            origin=None,
            authority=carried,
        )
    assert len(runner.calls) == 1


def test_failed_or_mismatched_runner_results_are_not_admitted(monkeypatch):
    failed = SnapshotRunner(status="failed")
    fixture = Fixture(monkeypatch, capture_runner=failed)
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "capture"}
    record = fixture.kernel_store.read_stage(SLOT, "capture")
    assert record["retry_safe"] is False
    assert record["result_reference"].startswith("capture:staging-2026-09-10-")
    assert not any(
        name.startswith(f"{native.CAPTURE_PREFIX}/registry/")
        for name in fixture.object_client.objects
    )
    # The runner's own failed result is the attempt's readback, and no other attempt has one.
    attempt = failed.calls[0]["attempt_id"]
    readback = fixture.clients["capture"].verified(attempt)
    assert readback["completion_state"] == "failed"
    assert readback["result_id"] == "res_" + "2" * 32
    assert record["result_reference"].endswith(f"/{readback['result_id']}")
    assert [
        name for name in fixture.object_client.objects if name.startswith(native.CAPTURE_PREFIX)
    ] == [f"{native.CAPTURE_PREFIX}/results/{attempt}.json"]
    assert fixture.run() == {"state": "unavailable", "operation_id": SLOT, "stage": "capture"}
    assert len(failed.calls) == 1

    mismatched = SnapshotRunner(source_as_of=COMPLETED)
    fixture = Fixture(monkeypatch, capture_runner=mismatched)
    assert fixture.run() == {"state": "unknown", "operation_id": SLOT, "stage": "capture"}
    record = fixture.kernel_store.read_stage(SLOT, "capture")
    assert record["result_reference"] == "capture_unresolved:capture_result_invalid"
    assert record["retry_safe"] is False
    assert not any(
        name.startswith(f"{native.CAPTURE_PREFIX}/registry/")
        for name in fixture.object_client.objects
    )
    assert fixture.run() == {"state": "unavailable", "operation_id": SLOT, "stage": "capture"}
    assert len(mismatched.calls) == 1


def test_snapshot_capture_runner_drives_the_runner_body_and_reads_its_own_result(monkeypatch):
    calls: dict[str, list] = {"validate": [], "run": [], "record": []}
    statements = (
        SimpleNamespace(source_schema_digest="a" * 64),
        SimpleNamespace(source_schema_digest="b" * 64),
    )

    def validate_inputs(artifacts, cutoff, mode, now):
        calls["validate"].append((artifacts, cutoff, mode, now))
        return {"plan": SimpleNamespace(statements=statements)}

    def run_operation(artifacts, cutoff, mode, **kwargs):
        calls["run"].append((artifacts, cutoff, mode, kwargs))
        payload = compact_payload(captured_at=kwargs["now"] + timedelta(minutes=1))
        payload["source_as_of"] = kwargs["now"].isoformat()
        return payload, "succeeded"

    def record_result(authority, consumption, name, canonical, digest, status):
        calls["record"].append((authority, consumption, name, canonical, digest, status))
        return SimpleNamespace(result_id="res_" + "9" * 32)

    monkeypatch.setattr(capture_script, "_validate_inputs", validate_inputs)
    monkeypatch.setattr(capture_script, "_run_operation", run_operation)
    monkeypatch.setattr(execution_approval, "_record_execution_result", record_result)
    digested = []
    shared_digest = ingest_receipts.receipt_source_digest
    monkeypatch.setattr(
        native,
        "receipt_source_digest",
        lambda receipt: (digested.append(receipt), shared_digest(receipt))[1],
    )
    authority = SimpleNamespace(
        execution_name="projects/p/locations/l/jobs/j/executions/e",
        image_uri="region-docker.pkg.dev/project/repo/image@sha256:" + "c" * 64,
    )
    execution = {"artifacts": {"capture_plan": b"{}"}, "objects": "objects"}
    carried = {
        "authority": authority,
        "consumption": "consumption",
        "stage": "capture",
        "mode": "new_consume",
    }
    seen = []
    runner = native.snapshot_capture_runner(
        lambda attempt, origin, issued: (seen.append((attempt, origin, issued)), execution)[1]
    )
    run = {"receipt": collection_receipt()}
    snapshot_as_of = COMPLETED + timedelta(minutes=2)
    payload, status, verification = runner(
        run,
        cutoff=CUTOFF.date() - timedelta(days=1),
        snapshot_as_of=snapshot_as_of,
        attempt_id="attempt:capture:1",
        origin="origin",
        client="client",
        authority=carried,
    )
    assert seen == [("attempt:capture:1", "origin", carried)]
    assert status == "succeeded"
    assert calls["validate"] == [
        (execution["artifacts"], CUTOFF.date() - timedelta(days=1), "initial", snapshot_as_of)
    ]
    (_artifacts, _cutoff, mode, kwargs) = calls["run"][0]
    assert mode == "initial"
    assert kwargs == {
        "authority": authority,
        "consumption": "consumption",
        "client": "client",
        "objects": "objects",
        "now": snapshot_as_of,
    }
    canonical = canonical_bytes(payload).decode("utf-8")
    assert calls["record"] == [
        (
            authority,
            "consumption",
            authority.execution_name + "#source-snapshot",
            canonical,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "succeeded",
        )
    ]
    assert verification == {
        "result_id": "res_" + "9" * 32,
        "image_digest": "c" * 64,
        "generation": "7",
        "schema_digest": hashlib.sha256(canonical_bytes(["a" * 64, "b" * 64])).hexdigest(),
        "source_digest": hashlib.sha256(canonical_bytes(collection_receipt())).hexdigest(),
    }
    # The digest comes from the one function the candidate binding also rebuilds it with.
    assert digested == [run["receipt"]]
    with pytest.raises(ValueError, match=r"^capture_execution_invalid$"):
        native.snapshot_capture_runner("not callable")
    with pytest.raises(ValueError, match=r"^capture_execution_invalid$"):
        native.snapshot_capture_runner(lambda attempt, origin, issued: {"artifacts": {}})(
            run,
            cutoff=CUTOFF.date(),
            snapshot_as_of=snapshot_as_of,
            attempt_id="a",
            origin=None,
            client=None,
            authority=carried,
        )
    # Without the issued authority the seam refuses before its execution is asked anything.
    asked = []
    with pytest.raises(StageRefusal, match=r"^capture_authority_unbound$"):
        native.snapshot_capture_runner(lambda *args: asked.append(args))(
            run,
            cutoff=CUTOFF.date(),
            snapshot_as_of=snapshot_as_of,
            attempt_id="a",
            origin=None,
            client=None,
            authority={**carried, "consumption": None},
        )
    assert asked == []
    assert calls["run"] == calls["run"][:1]


def test_capture_client_hands_the_runner_the_issued_authority_and_the_origin(monkeypatch):
    runner = SnapshotRunner()
    fixture = Fixture(monkeypatch, capture_runner=runner)
    assert native.describe_stage_clients(fixture.clients)["capture"] == {
        "bound": True,
        "binding": "src.analysis.open_intelligence.daily_native_clients.NativeCapture",
        "code": None,
    }
    issuer = IssuingAuthority(deny=("release",))
    result = fixture.run(authority=issuer)
    assert result == {"state": "failed", "operation_id": SLOT, "stage": "compose"}
    assert fixture.kernel_store.read_stage(SLOT, "capture")["state"] == "succeeded"
    [call] = runner.calls
    execution = issuer.executions[call["attempt_id"]]
    assert call["authority"] is execution
    assert call["authority"]["authority"] is execution["authority"]
    assert call["authority"]["consumption"] is execution["consumption"]
    assert call["authority"]["stage"] == "capture"
    assert call["authority"]["mode"] == "new_consume"
    origin = call["origin"]
    assert origin.manifest_version == "open_intelligence_execution_manifest_v2"
    assert "source_snapshot_capture" in origin.operation_bindings
    assert "new_consume" in origin.allowed_execution_modes
    assert call["client"] is fixture.bigquery
    assert fixture.run(authority=issuer) == {
        "state": "failed",
        "operation_id": SLOT,
        "stage": "compose",
    }
    assert len(runner.calls) == 1


def test_native_capture_execution_reads_the_artifacts_the_approval_names():
    digests = {name: hashlib.sha256(name.encode()).hexdigest() for name in capture_script._INPUTS}
    digests["build_provenance"] = "d" * 64
    approval = SimpleNamespace(
        canonical_manifest_json=json.dumps(
            {
                "input_artifacts": [
                    {"name": name, "sha256": value} for name, value in digests.items()
                ]
            }
        )
    )
    reads = []

    class Objects:
        def read_input(self, name, digest, *, timeout):
            reads.append((name, digest, timeout))
            return name.encode()

    objects = Objects()
    opened = []

    def runtime_clients():
        opened.append(True)
        return "query", objects

    carried = {
        "authority": SimpleNamespace(approval=approval),
        "consumption": "consumption",
        "stage": "capture",
        "mode": "new_consume",
    }
    execution = native.native_capture_execution(runtime_clients)
    assert opened == []
    bound = execution("attempt:capture:1", "origin", carried)
    assert opened == [True]
    assert bound == {
        "artifacts": {name: name.encode() for name in capture_script._INPUTS},
        "objects": objects,
    }
    assert reads == [(name, digests[name], 30) for name in sorted(capture_script._INPUTS)]
    with pytest.raises(StageRefusal, match=r"^capture_authority_unbound$"):
        execution("attempt:capture:1", "origin", {**carried, "authority": None})
    with pytest.raises(ValueError, match=r"^snapshot_runtime_authority_invalid$"):
        execution("attempt:capture:1", "origin", {**carried, "authority": object()})
    assert len(opened) == 1


class RoutedBigQueryClient(FakeBigQueryClient):
    """Answers each query with the rows routed by a substring of its SQL."""

    def __init__(self, routes):
        super().__init__()
        self.routes = list(routes)

    def query(self, sql, **kwargs):
        self.calls.append((sql, dict(kwargs)))
        rows = next(
            ([dict(row) for row in rows] for needle, rows in self.routes if needle in sql), []
        )

        def result(**kwargs):
            limit = kwargs.get("max_results")
            return iter(rows if limit is None else rows[:limit])

        return SimpleNamespace(result=result)


def evidence_rows(signal_id, published_at, geo_confidence=0.91):
    return [
        {
            "signal_id": signal_id,
            "row_id": "row-1",
            "source_family": "reddit",
            "direction": "rising",
            "published_at": published_at,
            "availability": "available",
            "geo_confidence": geo_confidence,
            "vendor_family": "socialcrawl",
            "channel_family": "reddit",
        },
        {
            "signal_id": signal_id,
            "row_id": "row-2",
            "source_family": "youtube",
            "direction": "rising",
            "published_at": published_at.isoformat(),
            "availability": "available",
            "geo_confidence": geo_confidence,
            "vendor_family": "google_youtube",
            "channel_family": "youtube",
        },
    ]


def test_certifier_rebuilds_the_readiness_inputs_from_the_persisted_rows(monkeypatch):
    fixture = Fixture(monkeypatch)
    certifier = fixture.clients["certifier"]
    profile = fixture.clients["release_profile"]
    rules = native.daily_readiness_rules(CUTOFF)
    # The daily floor is 0.6, the weakest tier the scope kernel can decide a local scope by
    # anywhere. The daily chain itself cannot reach that tier, and what the floor separates
    # there is {0.0, 0.9}; see the note beside DAILY_READINESS_GEO_FLOOR and the test that
    # derives the reachable set. What matters here is that the certifier re-evaluates each
    # candidate under the same rules the composer's readiness ran under.
    assert rules == ReadinessRules(
        current_cutoff=datetime(2026, 9, 10, tzinfo=UTC), minimum_geo_confidence=0.6
    )
    signal_id = "sig_" + "1" * 64
    rows = evidence_rows(signal_id, rules.current_cutoff - timedelta(hours=6))
    records = certification.readiness_records(rows)[signal_id]
    expected = evaluate_readiness(
        records, rules, quality_evaluated=True, independence_policy=EXPLICIT_ORIGIN_POLICY
    )
    warehouse = RoutedBigQueryClient(
        [
            (
                release.CANDIDATES_TABLE,
                [{"signal_id": signal_id, "evidence_state": expected.state}],
            ),
            (release.EVIDENCE_TABLE, rows),
            ("sp_read_open_intelligence_quality_review_receipt_v1", [{"run_id": profile.run_id}]),
        ]
    )
    bound = certification.NativeCertifier(
        object_client=fixture.object_client,
        warehouse=warehouse,
        release_profile=profile,
        generation=fixture.clients["generation"],
        readiness_rules=rules,
    )
    inputs = bound.readiness(profile.run_id)
    assert set(inputs) == {"candidates", "rules", "quality_evaluated"}
    assert inputs["rules"] == rules
    assert inputs["quality_evaluated"] is True
    assert inputs["candidates"] == [
        {
            "candidate": {"signal_id": signal_id, "evidence_state": expected.state},
            "records": list(records),
        }
    ]
    assert [record.vendor_family for record in records] == ["socialcrawl", "google_youtube"]
    assert records[1].published_at == rules.current_cutoff - timedelta(hours=6)
    assert daily_stages._certified_policy(bound, profile.run_id) == EXPLICIT_ORIGIN_POLICY
    for sql, kwargs in warehouse.calls:
        assert "run_id = @run_id" in sql or "(@run_id)" in sql
        assert [parameter.value for parameter in kwargs["job_config"].query_parameters] == [
            profile.run_id
        ]
    with pytest.raises(ValueError, match=r"^certify_run_differs$"):
        bound.readiness("run_other")
    unreviewed = certification.NativeCertifier(
        object_client=fixture.object_client,
        warehouse=RoutedBigQueryClient([(release.EVIDENCE_TABLE, rows)]),
        release_profile=profile,
        generation=fixture.clients["generation"],
        readiness_rules=rules,
    )
    assert unreviewed.readiness(profile.run_id) == {
        "candidates": [],
        "rules": rules,
        "quality_evaluated": False,
    }
    with pytest.raises(ValueError, match=r"^certify_evidence_rows_invalid$"):
        certification.readiness_records([{"signal_id": signal_id}])
    capped_rows = [dict(rows[0], row_id=f"row-{index}") for index in range(51_000)]
    capped = certification.NativeCertifier(
        object_client=fixture.object_client,
        warehouse=RoutedBigQueryClient(
            [
                (release.CANDIDATES_TABLE, [{"signal_id": signal_id, "evidence_state": "ready"}]),
                (release.EVIDENCE_TABLE, capped_rows),
            ]
        ),
        release_profile=profile,
        generation=fixture.clients["generation"],
        readiness_rules=rules,
    )
    with pytest.raises(
        certification.CertificationEvidenceTruncated,
        match=r"^certification_evidence_truncated:signal_evidence_v2$",
    ):
        capped.readiness(profile.run_id)
    # The truncation is a stage refusal: the handler's native boundary passes it through
    # with the table name intact, so the certify stage records it retry safe.
    with pytest.raises(
        StageRefusal, match=r"^certification_evidence_truncated:signal_evidence_v2$"
    ):
        daily_stages._certified_policy(capped, profile.run_id)
    assert certification.CERTIFICATION_ROW_CAP == 50_000
    exact = certification.NativeCertifier(
        object_client=fixture.object_client,
        warehouse=RoutedBigQueryClient([(release.EVIDENCE_TABLE, capped_rows[:50_000])]),
        release_profile=profile,
        generation=fixture.clients["generation"],
        readiness_rules=rules,
    )
    assert exact.readiness(profile.run_id)["candidates"] == []
    with pytest.raises(ValueError, match=r"^certify_readiness_rules_invalid$"):
        certification.NativeCertifier(
            object_client=fixture.object_client,
            warehouse=warehouse,
            release_profile=profile,
            generation=None,
            readiness_rules={"current_cutoff": CUTOFF},
        )
    assert certifier is fixture.clients["certifier"]


def test_certifier_reads_the_six_artifacts_through_the_release_script_readers(monkeypatch):
    fixture = Fixture(monkeypatch)
    certifier = fixture.clients["certifier"]
    profile = fixture.clients["release_profile"]
    generation = fixture.clients["generation"]
    seen: dict[str, list] = {"chain": [], "proof": [], "control": [], "packet": [], "review": []}
    chain = {
        "r3_apply": {"binding": "apply-binding", "registry": "registry-apply"},
        "r3_proof_issue": {"binding": "proof-binding", "registry": "registry-proof"},
    }

    def read_chain(client, operation, **kwargs):
        seen["chain"].append((client, operation, kwargs))
        return chain[operation]

    def proof_binding(proof_result, apply_result, **kwargs):
        seen["proof"].append((proof_result, apply_result, kwargs))
        return b"proof-bytes", "1" * 64

    def control_digests(client, *, profile):
        seen["control"].append((client, profile))
        return "5" * 64, "6" * 64

    def read_packet(client, *, profile):
        seen["packet"].append((client, profile))
        return {"run_id": profile.run_id, "packet_digest": "6" * 64}

    def read_review(client, **kwargs):
        seen["review"].append((client, kwargs))
        return {"receipt_digest": "7" * 64}

    monkeypatch.setattr(release, "_read_operation_result_chain", read_chain)
    monkeypatch.setattr(release, "_proof_ledger_binding", proof_binding)
    monkeypatch.setattr(release, "_control_digests", control_digests)
    monkeypatch.setattr(release, "_read_review_packet", read_packet)
    monkeypatch.setattr(release, "_read_quality_review_receipt", read_review)
    receipt = SimpleNamespace(run_id=profile.run_id, source_window_digest="8" * 64)
    artifacts = certifier.artifacts(receipt)
    assert set(artifacts) == CERTIFICATION_ARTIFACT_FIELDS
    assert artifacts == {
        "execution_proof_bytes": b"proof-bytes",
        "execution_proof_manifest_sha256": "1" * 64,
        "quality_review_receipt": {"receipt_digest": "7" * 64},
        "review_packet": {"run_id": profile.run_id, "packet_digest": "6" * 64},
        "candidate_projection_digest": "5" * 64,
        "apply_binding": "apply-binding",
    }
    expected_kwargs = {
        "version": execution_approval._RESULT_VERSION_V2,
        "mode": "historical_read",
        "generation": generation,
        "profile": profile,
    }
    assert seen["chain"] == [
        (fixture.bigquery, "r3_apply", expected_kwargs),
        (fixture.bigquery, "r3_proof_issue", expected_kwargs),
    ]
    assert seen["proof"] == [
        (
            chain["r3_proof_issue"],
            chain["r3_apply"],
            {"mode": "historical_read", "registry": "registry-proof", "profile": profile},
        )
    ]
    assert seen["control"] == [(fixture.bigquery, profile)]
    assert seen["packet"] == [(fixture.bigquery, profile)]
    assert seen["review"] == [
        (
            fixture.bigquery,
            {
                "packet": artifacts["review_packet"],
                "source_window_digest": "8" * 64,
                "candidate_projection_digest": "5" * 64,
                "profile": profile,
            },
        )
    ]
    with pytest.raises(ValueError, match=r"^certify_receipt_differs$"):
        certifier.artifacts(SimpleNamespace(run_id="other", source_window_digest="8" * 64))
    monkeypatch.setattr(
        release, "_read_review_packet", lambda client, *, profile: {"packet_digest": "0" * 64}
    )
    with pytest.raises(ValueError, match=r"^certify_packet_digest_differs$"):
        certifier.artifacts(receipt)


def test_certification_records_are_create_only_objects(monkeypatch):
    fixture = Fixture(monkeypatch)
    certifier = fixture.clients["certifier"]
    run_id = fixture.clients["release_profile"].run_id
    assert certifier.read(run_id) is None
    evidence = {"blocked_run_receipt_digest": "1" * 64, "independence_policy": "explicit_origin_v2"}
    certifier.record(run_id, evidence)
    name = f"{certification.CERTIFICATION_PREFIX}/{run_id}.json"
    assert fixture.object_client.writes == [(name, 0)]
    assert certifier.read(run_id) == evidence
    certifier.record(run_id, dict(evidence))
    assert fixture.object_client.writes == [(name, 0)]
    with pytest.raises(ValueError, match=r"^certification_conflict$"):
        certifier.record(run_id, {**evidence, "independence_policy": "other"})
    assert certifier.read(run_id) == evidence
    racing = RacingObjectClient(name, canonical_bytes(evidence))
    records = certification.ObjectCertificationRecords(racing)
    records.record(run_id, evidence)
    assert records.read(run_id) == evidence
    assert racing.writes == [(name, 0)]
    differing = RacingObjectClient(name, canonical_bytes({**evidence, "source_sha": "a" * 40}))
    with pytest.raises(ValueError, match=r"^certification_conflict$"):
        certification.ObjectCertificationRecords(differing).record(run_id, evidence)
    with pytest.raises(ValueError, match=r"^certification_record_invalid$"):
        records.record(run_id, "not a mapping")


@pytest.mark.parametrize("field", ["policy_sha256", "profile_sha256"])
@pytest.mark.parametrize("value", ["x", "Z" * 64, "6" * 63, "6" * 65, "not-a-digest"])
def test_collection_authority_holds_both_digests_to_the_receipt_shape(field, value):
    """The receipt requires sixty-four hexadecimal characters of both digests.

    Reading only the policy digest for shape left a malformed profile digest to
    refuse inside the producer, after collection had already run, which the daily
    readback turns into an unknown stage rather than a clean refusal here.
    """
    variables = {
        "COLLECTION_POLICY_SHA256": POLICY_DIGEST,
        "COLLECTION_PROFILE_SHA256": "6" * 64,
    }
    assert native.collection_authority(variables) == {
        "policy_sha256": POLICY_DIGEST,
        "profile_sha256": "6" * 64,
    }
    variables[
        "COLLECTION_POLICY_SHA256" if field == "policy_sha256" else "COLLECTION_PROFILE_SHA256"
    ] = value
    with pytest.raises(ValueError, match=r"^collection_authority_invalid$"):
        native.collection_authority(variables)


# The compose binding


_ABSENT = object()


def composition_binding(**overrides):
    """What the runtime must hand over before a paid composition can be dispatched."""
    binding = {
        "source_window": lambda entry: {"rows_by_table": {}},
        "approved_by": "principal:daily-composition",
        "approved_at": datetime(2026, 9, 11, tzinfo=UTC),
        "rule_version": "composition_rules_v3",
    }
    binding.update(overrides)
    return {name: value for name, value in binding.items() if value is not _ABSENT}


def test_the_composer_and_persist_bind_only_over_a_composition_binding(monkeypatch):
    bound = Fixture(monkeypatch, compose_binding=composition_binding())
    composer = bound.clients["composer"]
    persist = bound.clients["persist"]
    assert isinstance(composer, daily_composer.NativeComposer)
    assert callable(persist)
    assert composer.scope.run_id == bound.clients["release_profile"].run_id
    assert composer.scope.client_scope_id == "ogilvy_default"
    assert composer.scope.market_scope == ("za", "ng", "ke")
    assert composer.readiness_rules == native.daily_readiness_rules(CUTOFF)
    assert composer._now is bound.clock
    described = native.describe_stage_clients(bound.clients)
    assert described["composer"] == {
        "bound": True,
        "binding": "src.analysis.open_intelligence.daily_composer.NativeComposer",
        "code": None,
    }
    assert described["persist"]["bound"] is True
    assert described["persist"]["code"] is None
    assert [name for name, entry in described.items() if not entry["bound"]] == []
    # Shape-valid composition fields cannot dispatch a composition: with the products bound,
    # compose still refuses on its missing admitted capture before any warehouse call.
    handlers = bound.handlers()
    result = handlers["compose"](
        manifest=manifest("compose", operation_id=SLOT), authority_receipt=receipt_for("compose")
    )
    assert result["result_reference"] == "compose_refused:capture_record_unavailable"
    assert bound.bigquery.calls == []


def test_the_factory_leaves_compose_unbound_when_the_runtime_binds_no_composition(monkeypatch):
    # The load bearing half: no native authorization exists for a paid composition, the
    # managed runtime passes no binding, and the seam defaults to none, so the compose
    # stage refuses before dispatch and no warehouse call of any kind is made.
    assert (
        inspect.signature(native.build_native_stage_clients).parameters["compose_binding"].default
        is None
    )
    fixture = Fixture(monkeypatch)
    assert isinstance(fixture.clients["composer"], native.UnboundClient)
    assert isinstance(fixture.clients["persist"], native.UnboundClient)
    # The managed runtime names no compose binding at all; built the way it builds them,
    # the two clients are unbound just the same.
    unnamed = native.build_native_stage_clients(
        engine=fixture.engine,
        profile=kernel_profile(),
        environment="staging",
        object_client=fixture.object_client,
        warehouse=fixture.bigquery,
        now=fixture.clock,
        build=BUILD,
    )
    assert isinstance(unnamed["composer"], native.UnboundClient)
    assert isinstance(unnamed["persist"], native.UnboundClient)
    result = fixture.handlers()["compose"](
        manifest=manifest("compose", operation_id=SLOT), authority_receipt=receipt_for("compose")
    )
    assert result["state"] == "failed"
    assert result["retry_safe"] is True
    assert result["result_reference"] == "compose_refused:client_unbound:composer"
    assert fixture.bigquery.calls == []
    assert fixture.object_client.writes == []


def _nothing_built(monkeypatch):
    """Make every client the binding layer would build raise, so reaching one is visible."""

    def refuse(*args, **kwargs):
        raise AssertionError("the binding layer built a client before refusing")

    monkeypatch.setattr(daily_composer, "build_native_composer", refuse)
    monkeypatch.setattr(daily_composer, "build_native_persist", refuse)


def test_a_composition_binding_short_of_exact_binds_nothing(monkeypatch):
    for overrides, code in (
        ({"source_window": _ABSENT}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": _ABSENT}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_at": _ABSENT}, native.COMPOSE_BINDING_REFUSAL),
        ({"rule_version": _ABSENT}, native.COMPOSE_BINDING_REFUSAL),
        # A source window that is not a reader refuses under a code of its own. It shared
        # compose_binding_invalid with NativeComposer's own refusal of the same shape, which
        # made keeping the guard unobservable: it was deleted as a mutation with the whole
        # suite passing. The earlier lane answered that with an assertion on the stack
        # location instead of a distinct code, on the ground that a distinct code would
        # have changed a pinned expected result; the reasoning was circular, since the pin
        # in question was written by the same commit, in this very loop.
        ({"source_window": "not callable"}, native.COMPOSE_SOURCE_WINDOW_REFUSAL),
        ({"source_window": None}, native.COMPOSE_SOURCE_WINDOW_REFUSAL),
        ({"source_window": {"rows_by_table": {}}}, native.COMPOSE_SOURCE_WINDOW_REFUSAL),
        ({"approved_by": ""}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": None}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_at": datetime(2026, 9, 11)}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_at": "2026-09-11T00:00:00+00:00"}, native.COMPOSE_BINDING_REFUSAL),
        ({"rule_version": ""}, native.COMPOSE_BINDING_REFUSAL),
        ({"unexpected": "field"}, native.COMPOSE_BINDING_REFUSAL),
        # A field that is nothing but whitespace names nobody and nothing. Each of the
        # next six bound a live composer while the binding layer tested falsiness instead
        # of stripping first, which is what geographic_scope._text already did.
        ({"approved_by": "   "}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": "\t\n "}, native.COMPOSE_BINDING_REFUSAL),
        ({"rule_version": " "}, native.COMPOSE_BINDING_REFUSAL),
        # Nor is a principal a place to carry control characters or four kilobytes.
        ({"approved_by": "principal:\x00daily-composition"}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": "principal:daily\x07composition"}, native.COMPOSE_BINDING_REFUSAL),
        ({"rule_version": "composition\rrules_v3"}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": "principal:" + "a" * 4096}, native.COMPOSE_BINDING_REFUSAL),
        ({"rule_version": "v" * 4096}, native.COMPOSE_BINDING_REFUSAL),
        # Stripping and a control character class were all the hardening did, and all of
        # the following bound a live composer under it. An approver that names nobody at
        # all: a zero width space, a byte order mark, a word joiner, a soft hyphen.
        ({"approved_by": "\u200b"}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": "\ufeff"}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": "\u2060"}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": "\u00ad"}, native.COMPOSE_BINDING_REFUSAL),
        ({"approved_by": "principal:\u200bdaily-composition"}, native.COMPOSE_BINDING_REFUSAL),
        # A bidirectional override, which makes a principal read as another principal.
        ({"approved_by": "principal:\u202edaily-composition"}, native.COMPOSE_BINDING_REFUSAL),
        ({"rule_version": "composition\u202brules_v3"}, native.COMPOSE_BINDING_REFUSAL),
        # Line breaks, in fields documented as single lines.
        ({"approved_by": "principal:daily\u2028composition"}, native.COMPOSE_BINDING_REFUSAL),
        ({"rule_version": "composition\u2029rules_v3"}, native.COMPOSE_BINDING_REFUSAL),
        # A lone surrogate, which does not even encode without surrogatepass.
        ({"approved_by": "principal:\ud800"}, native.COMPOSE_BINDING_REFUSAL),
        # 256 emoji: 256 characters and 1024 bytes, so a character bound alone admits it.
        ({"approved_by": "\U0001f600" * 256}, native.COMPOSE_BINDING_REFUSAL),
        ({"rule_version": "\U0001f600" * 256}, native.COMPOSE_BINDING_REFUSAL),
        # An approval that has not been granted yet admits nothing. The comparand is the
        # observation cutoff the composition closes: an approval cannot postdate the
        # observations it admits a composition of. The earlier lane compared against the
        # composition instant instead, which admitted the whole span from the cutoff to the
        # run: long enough to read the closed day, choose the rule version those
        # observations suit, have it approved that morning and bind.
        ({"approved_at": datetime(2099, 1, 1, tzinfo=UTC)}, native.COMPOSE_APPROVAL_REFUSAL),
        ({"approved_at": CUTOFF + timedelta(seconds=1)}, native.COMPOSE_APPROVAL_REFUSAL),
        # This one is a behaviour withdrawn, not a weakness closed, and it is counted as a
        # changed expected result on that account. The previous revision of this lane
        # asserted that an approval dated exactly at the composition instant binds a
        # composer; the identical input now refuses. What follows from the cutoff comparand
        # is that no approval granted on the day of the run can bind that run at all: the
        # cutoff is the midnight that closed the observation day and the run is after it,
        # so the whole span from that midnight to the composition refuses. An approval has
        # to exist before the day's midnight, which in practice means the day before the
        # run at the latest. Whoever operates this will meet it.
        ({"approved_at": STARTED}, native.COMPOSE_APPROVAL_REFUSAL),
        ({"approved_at": CUTOFF + timedelta(microseconds=1)}, native.COMPOSE_APPROVAL_REFUSAL),
        ({"approved_at": STARTED + timedelta(seconds=1)}, native.COMPOSE_APPROVAL_REFUSAL),
        ({"approved_at": STARTED + timedelta(days=365)}, native.COMPOSE_APPROVAL_REFUSAL),
        # And the guard was one sided, so an unset instant made aware bound a composition.
        ({"approved_at": datetime(1970, 1, 1, tzinfo=UTC)}, native.COMPOSE_APPROVAL_STALE_REFUSAL),
        (
            {"approved_at": native.COMPOSE_APPROVAL_NOT_BEFORE - timedelta(seconds=1)},
            native.COMPOSE_APPROVAL_STALE_REFUSAL,
        ),
    ):
        with monkeypatch.context() as unbuilt:
            _nothing_built(unbuilt)
            refused = pytest.raises(ValueError, match=rf"^{code}$")
            with refused:
                Fixture(monkeypatch, compose_binding=composition_binding(**overrides))
    # Each of those is the binding layer's own refusal, raised before it builds anything,
    # which is what the patched builders above assert directly. That replaces an assertion
    # on the file and function the refusal was raised in: the behaviour is that nothing is
    # built, and the stack location was a stand in for it that any extraction would break.
    # The one case the stack assertion was really covering now carries its own code.
    assert native.COMPOSE_SOURCE_WINDOW_REFUSAL != native.COMPOSE_BINDING_REFUSAL
    with pytest.raises(daily_stages.StageRefusal, match=r"^compose_source_window_invalid$"):
        daily_composer.build_native_composer(
            warehouse=FakeBigQueryClient(),
            scope=composer_tests.scope(),
            source_window_from_entry=lambda entry: None,
            readiness_rules=native.daily_readiness_rules(CUTOFF),
            semantic_provider=composer_tests.Provider(),
            now=Clock(STARTED),
        )(composer_tests.entry_for(composer_tests.stages.FakeCapture()), cutoff=CUTOFF)
    # The cutoff is derived from the instant, and this builder is exported and takes them
    # as independent arguments, so it says so rather than assuming it. Without this a
    # caller choosing its own cutoff moves the approval comparand with it.
    with pytest.raises(ValueError, match=r"^compose_cutoff_underived$"):
        native.compose_clients(
            composition_binding(),
            warehouse=FakeBigQueryClient(),
            release_profile=Fixture(monkeypatch).clients["release_profile"],
            cutoff=CUTOFF + timedelta(days=1),
            build=BUILD,
            now=Clock(STARTED),
            instant=STARTED,
        )
    # An approval dated before the observation cutoff is not this guard's business; a rule
    # approved long ago still admits today's run.
    bound = Fixture(
        monkeypatch,
        compose_binding=composition_binding(approved_at=datetime(2026, 1, 2, tzinfo=UTC)),
    )
    assert isinstance(bound.clients["composer"], daily_composer.NativeComposer)
    # An approval dated exactly at the cutoff is granted, not pending: the comparison is
    # inclusive, which is the shape of every approval this repository records, the replay
    # geo floor of 2026-09-03 over a window closing 2026-09-03 among them. That precedent
    # never required comparing against the composition instant.
    at_the_close = Fixture(monkeypatch, compose_binding=composition_binding(approved_at=CUTOFF))
    assert isinstance(at_the_close.clients["composer"], daily_composer.NativeComposer)
    at_the_bound = Fixture(
        monkeypatch,
        compose_binding=composition_binding(approved_at=native.COMPOSE_APPROVAL_NOT_BEFORE),
    )
    assert isinstance(at_the_bound.clients["composer"], daily_composer.NativeComposer)
    # Surrounding whitespace is stripped rather than refused, so a field that names
    # something still names it, and the value that binds is the stripped one.
    assert (
        native._text("  principal:daily-composition  ", "unused") == "principal:daily-composition"
    )
    padded = Fixture(
        monkeypatch,
        compose_binding=composition_binding(
            approved_by="  principal:daily-composition  ", rule_version=" composition_rules_v3 "
        ),
    )
    assert isinstance(padded.clients["composer"], daily_composer.NativeComposer)
    assert callable(padded.clients["persist"])
    with pytest.raises(ValueError, match=r"^compose_binding_invalid$"):
        Fixture(monkeypatch, compose_binding=("source_window",))
    # The run receipt names the source commit the image was built from; without the
    # runtime's own build fact there is none to name, so nothing binds.
    with pytest.raises(ValueError, match=r"^compose_binding_unbuilt$"):
        Fixture(monkeypatch, build=None, compose_binding=composition_binding())


def test_no_ambient_route_inside_the_factory_can_reach_a_composition_binding():
    # The signature default is pinned beside the unbound clients, but a default is not the
    # only route a binding could arrive by: a fallback in the factory body, reading an
    # environment variable or a module constant, binds a live composer while every caller
    # still names nothing. Such a fallback was added as a mutation and the full suite
    # passed, because the pins on the signature and on two call shapes forbid nothing in
    # the body. This reads the body instead.
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_native_stage_clients"
    )
    references = [
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Name) and node.id == "compose_clients"
    ]
    assert len(references) == 1
    guarded = [
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.IfExp)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "compose_binding"
        and [type(operator) for operator in node.test.ops] == [ast.Is]
        and [getattr(value, "value", native) for value in node.test.comparators] == [None]
    ]
    assert len(guarded) == 1
    # The one reference sits under the arm taken when the binding is absent, and nowhere
    # else, so the composer is built only where a caller handed the binding over.
    assert [
        node
        for node in ast.walk(guarded[0].orelse)
        if isinstance(node, ast.Name) and node.id == "compose_clients"
    ] == references
    call = next(
        node
        for node in ast.walk(guarded[0].orelse)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "compose_clients"
    )
    # What it binds over is the parameter itself, not a fallback expression around it.
    assert isinstance(call.args[0], ast.Name)
    assert call.args[0].id == "compose_binding"
    # And the parameter is never rebound anywhere in the body, so nothing ambient can fill
    # it before that branch is chosen. This is what the mutation did.
    assert not [
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Name)
        and node.id == "compose_binding"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    # Nor is there a module level binding for such a route to reach for: no name in this
    # module is a mapping carrying the binding's own fields.
    fields = set(native.COMPOSE_BINDING_FIELDS)
    carriers = [
        name
        for name, value in vars(native).items()
        if isinstance(value, Mapping) and fields <= set(value)
    ]
    assert carriers == []


def test_the_factory_binds_the_certifier_under_the_daily_floor(monkeypatch):
    # The floor's only BOUND consumer on this branch, and the one that was pinned nowhere.
    # It was called the only live consumer and the one guard with live effect, and neither
    # is true: on this branch the floor governs nothing that executes. The compose stage is
    # unbound in both production callers, so no composer ever reads the rules; the daily
    # cycle returns as soon as a stage is not succeeded, so compose refuses and certify is
    # never reached, so the certifier the factory always builds never re-evaluates anything
    # either. What is true is that the certifier is the one client the factory binds these
    # rules into unconditionally, so it is where the floor takes effect the moment the
    # chain runs at all, and it is the only place a change to the floor is observable
    # without binding a composer. Binding it with a minimum geographic confidence of 0.0
    # left the entire suite passing, because every test that touched the rules built a
    # certifier of its own rather than reading the one the factory bound. This reads that
    # one, through its own readiness. The pin stays for the same reason it was added.
    fixture = Fixture(monkeypatch)
    profile = fixture.clients["release_profile"]
    bound = fixture.clients["certifier"]
    expected = native.daily_readiness_rules(CUTOFF)
    assert expected.minimum_geo_confidence == native.DAILY_READINESS_GEO_FLOOR
    assert bound.readiness(profile.run_id)["rules"] == expected
    assert bound.readiness(profile.run_id)["rules"].minimum_geo_confidence == 0.6
    # And the same rules object the composer would be built under, so the two halves of the
    # daily chain cannot drift apart while only one of them is bound.
    assert native.daily_readiness_rules(CUTOFF) == expected


def test_no_ambient_route_can_reach_the_daily_geo_floor():
    # The previous lane closed a parse based route to the compose binding and left the
    # same route open to the floor itself: replacing the constant with a read of an
    # environment variable, the constant as its fallback, moved the live certifier's floor
    # while every test passed. The floor is closed the way the binding was, by reading the
    # module rather than its values.
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    bindings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "DAILY_READINESS_GEO_FLOOR"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    assert len(bindings) == 1
    assignment = next(
        node for node in tree.body if isinstance(node, ast.Assign) and bindings[0] in node.targets
    )
    # A literal at module level and nothing else: no call, no environment read, no
    # expression with the constant as a fallback inside it.
    assert isinstance(assignment.value, ast.Constant)
    assert assignment.value.value == 0.6
    assert native.DAILY_READINESS_GEO_FLOOR == 0.6
    # The rules read the constant rather than restating its value, so the two cannot be
    # moved apart. Hardcoding 0.6 here survived the whole suite as well.
    rules = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "daily_readiness_rules"
    )
    floors = [
        keyword
        for node in ast.walk(rules)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "minimum_geo_confidence"
    ]
    assert len(floors) == 1
    assert isinstance(floors[0].value, ast.Name)
    assert floors[0].value.id == "DAILY_READINESS_GEO_FLOOR"
    # And the certifier the factory always binds takes its rules from that function over
    # the cutoff, rather than assembling rules of its own.
    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_native_stage_clients"
    )
    certifier = next(
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "NativeCertifier"
    )
    bound_rules = next(
        keyword for keyword in certifier.keywords if keyword.arg == "readiness_rules"
    )
    assert isinstance(bound_rules.value, ast.Call)
    assert bound_rules.value.func.id == "daily_readiness_rules"
    assert [argument.id for argument in bound_rules.value.args] == ["cutoff"]


def _scopes_the_daily_chain_decides():
    """Which scopes the daily composer's own geography resolution can actually reach.

    Read off ``daily_composer._resolve_geography`` by exercising it over the shapes an
    evidence row takes, rather than off the kernel's tier table or off the kernel's own
    resolver: the composer decides what it hands the kernel, and what it hands the kernel
    is a strict subset of what the kernel reads.
    """
    decided: dict[str, set[str]] = {}
    marker = replay_blocklist.TOPIC_GEO_BLOCKLIST["economy_sapa_hustle"][0]
    shapes = itertools.product(
        ("ng", "za", "ke"),
        (
            None,
            "",
            "Nigerian street style",
            "South African mzansi weekend",
            "Kenyan runners take the field",
            "Sapa tour tour tour " + marker,
            "\u041b\u0430\u0433\u043e\u0441",
        ),
        (None, "geo_m1"),
        (None, "geo_r1"),
        (None, (), ("economy_sapa_hustle",), ("music",), ("economy_sapa_hustle", "music")),
        # A framed source, a vendor measured source, and a row that names no source.
        (None, "rss", "apple_music"),
    )
    for market, title, method_id, receipt_id, topics, source in shapes:
        row = {
            "source": source,
            "market": market,
            "title": title,
            "text": None,
            "url": None,
            "geo_method_id": method_id,
            "geo_receipt_id": receipt_id,
            "topic_groups": topics,
        }
        try:
            record = daily_composer._resolve_geography(market, "row_1", row)
        except ValueError:
            continue
        decided.setdefault(record.method, set()).add(record.scope)
    return decided


def _scopes_the_kernel_decides():
    """Which scopes each deciding method of the geographic kernel can resolve at all.

    Read off ``resolve_geographic_scope`` by exercising it over the shapes its inputs take,
    rather than restated from the tier table: the tier table ranks how strong a deciding
    method is and says nothing at all about which scope that method can reach. This is the
    kernel in principle; ``_scopes_the_daily_chain_decides`` is the subset the daily
    composer can actually reach, and the two differ.
    """
    decided: dict[str, set[str]] = {}
    shapes = itertools.product(
        ("ng", "za"),
        (
            None,
            "Nigerian street style",
            "Sapa Vietnam tour tour tour",
            "\u041b\u0430\u0433\u043e\u0441",
        ),
        (None, "ng", "za", "gh"),
        (None, "vendor_r1"),
        (None, "ng", "za", "gh"),
        (None, "geo_m1"),
        (None, "geo_r1"),
        (None, "tourism", "music"),
    )
    for market, title, vendor, vendor_receipt, retained, method_id, receipt_id, topic in shapes:
        try:
            record = resolve_geographic_scope(
                market=market,
                row_id="row_1",
                title=title,
                topic_group=topic,
                vendor_market=vendor,
                vendor_receipt_id=vendor_receipt,
                retained_geo_market=retained,
                geo_method_id=method_id,
                geo_receipt_id=receipt_id,
            )
        except ValueError:
            continue
        decided.setdefault(record.method, set()).add(record.scope)
    return decided


def test_the_daily_geo_floor_partitions_the_confidences_the_daily_chain_can_reach():
    # Replaces a pin that asserted the floor equalled the minimum over every tier in the
    # table, and corrects the claim that replaced it. The table ranks how strong a deciding
    # method is, not which scope it can reach, and the kernel in principle is not the chain
    # the floor governs. Both are derived here, and they differ.
    kernel = _scopes_the_kernel_decides()
    local_tiers = {
        method: METHOD_CONFIDENCE[method] for method, scopes in kernel.items() if "local" in scopes
    }
    # In the kernel, a local scope is reached only through _origin_scope, where the resolved
    # origin equals the target, and only these two methods are ever handed to it.
    assert set(local_tiers) == {"retained_geo_receipt", "vendor_region"}
    assert min(local_tiers.values()) == METHOD_CONFIDENCE["vendor_region"] == 0.6
    # The daily chain reaches less than that. The composer hands the kernel the row's text
    # fields, its topic groups, and its own partition market as the retained geography with
    # that geography's method and receipt ids. No evidence relation carries a vendor market
    # or a vendor receipt id column; the composer names a vendor region only for a row whose
    # source the reviewed inventory records as measuring the requested country's own people,
    # citing the row the vendor answered, and that region is the row's own market. So the
    # vendor region decides local only here, never foreign, and local is decided by the
    # retained geography receipt at 0.9 or the vendor region at 0.6.
    chain = _scopes_the_daily_chain_decides()
    assert {method: sorted(scopes) for method, scopes in sorted(chain.items())} == {
        "blocklist_match": ["foreign"],
        "content_marker": ["contextual"],
        "none": ["unknown"],
        "retained_geo_receipt": ["local"],
        "script_marker": ["foreign"],
        "vendor_region": ["local"],
    }
    assert kernel["vendor_region"] == {"local", "foreign"}
    assert not {"vendor_market", "vendor_receipt_id"} & {
        column for table in composer_columns() for column in table
    }
    # The retained geography the composer passes is the row's own partition market, so the
    # origin equals the target by construction; a local scope in this chain means the row
    # carries a geographic method id and a geographic receipt id, and nothing more than
    # that was measured about where the behaviour happened. That is why retained_geo_receipt
    # never decides foreign here although the kernel lets it.
    assert kernel["retained_geo_receipt"] == {"local", "foreign"}
    # So the geo confidence a daily evidence row can carry is exactly {0.0, 0.6, 0.9}: the
    # composer writes _geo_confidence and nothing else into that column, a local row
    # carrying its deciding tier and every measured contextual or foreign row carrying zero.
    reachable = {
        daily_composer._geo_confidence(GeographicScope(method, reference, scope, tier))
        for method, scopes in chain.items()
        if method != "none"
        for scope in scopes
        for tier in (METHOD_CONFIDENCE[method],)
        for reference in ("row_1" if method in ROW_CITING_METHODS else "geo_r1",)
    }
    assert reachable == {0.0, 0.6, 0.9}
    # The floor stays a literal so a later change to a tier cannot move it silently.
    assert native.DAILY_READINESS_GEO_FLOOR == 0.6
    assert native.daily_readiness_rules(CUTOFF).minimum_geo_confidence == 0.6
    # It therefore sits on the weakest tier the kernel can decide local by at all, which is
    # now a tier the chain produces: a vendor measured row is admitted at exactly the floor,
    # by the inclusive comparison, and every measured contextual or foreign row is refused.
    assert native.DAILY_READINESS_GEO_FLOOR in reachable
    admitted = {value for value in reachable if value >= native.DAILY_READINESS_GEO_FLOOR}
    assert admitted == {0.6, 0.9}
    assert {value for value in reachable if value < native.DAILY_READINESS_GEO_FLOOR} == {0.0}
    assert min(local_tiers.values()) == native.DAILY_READINESS_GEO_FLOOR
    # The content marker, where the floor used to sit, decides contextual and nothing else
    # in the kernel and in the chain alike, so the old value sat on a tier that can never
    # be local.
    assert kernel["content_marker"] == chain["content_marker"] == {"contextual"}
    assert METHOD_CONFIDENCE["content_marker"] < native.DAILY_READINESS_GEO_FLOOR
    # The floor sits strictly above the highest confidence a not local row could carry into
    # readiness, over every method the type admits with that scope and not only the
    # reachable ones, because the composer zeroes a measured contextual or foreign row.
    # That zeroing is what keeps such a row out and the floor could not do it alone: the
    # blocklist decides foreign at the highest tier there is.
    carried = {
        daily_composer._geo_confidence(
            GeographicScope(
                method,
                "row_1" if method in ROW_CITING_METHODS else "geo_r1",
                scope,
                METHOD_CONFIDENCE[method],
            )
        )
        for method, scopes in METHOD_SCOPES.items()
        for scope in scopes & {"contextual", "foreign"}
    }
    assert carried == {0.0}
    assert max(carried) < native.DAILY_READINESS_GEO_FLOOR
    assert max(METHOD_CONFIDENCE.values()) > native.DAILY_READINESS_GEO_FLOOR
    # Readiness compares with <, so the floor is inclusive: a row at exactly the floor is
    # admitted, the one reachable local row at 0.9 is admitted, and a row at the content
    # marker's tier is not.
    rules = native.daily_readiness_rules(CUTOFF)
    fresh = rules.current_cutoff + timedelta(hours=6)
    signal_id = "sig_" + "2" * 64
    for confidence, expected in (
        (native.DAILY_READINESS_GEO_FLOOR, True),
        (METHOD_CONFIDENCE["retained_geo_receipt"], True),
        (METHOD_CONFIDENCE["content_marker"], False),
        (0.0, False),
    ):
        records = certification.readiness_records(
            evidence_rows(signal_id, fresh, geo_confidence=confidence)
        )[signal_id]
        result = evaluate_readiness(
            records, rules, quality_evaluated=True, independence_policy=EXPLICIT_ORIGIN_POLICY
        )
        assert ("weak_geo_evidence" not in result.reasons) is expected
        assert bool(result.qualifying_row_ids) is expected
    # The replay chain keeps its own floor at zero under its own name: Albert approved that
    # value on 2026-09-03 over a window whose rows carry no resolved geography at all, and
    # no composition of this slice changes a decision recorded there.
    assert replay.REPLAY_READINESS_GEO_FLOOR == 0.0


def composer_columns():
    """The evidence columns the composer's own snapshot rows are allowed to carry."""
    from src.analysis.open_intelligence.pipeline import EVIDENCE_COLUMNS_BY_TABLE

    return tuple(EVIDENCE_COLUMNS_BY_TABLE[table] for table in daily_composer._RECEIPT_TABLES)


class ComposeWarehouse(composer_tests.FakePersistClient):
    """One BigQuery client for both halves of compose, as the factory binds a single one.

    The persist side is the persistence suite's own writer with the daily relations; every
    other statement is the composer's read of the staging tables behind the snapshot.
    """

    project = "ogilvy-trends-v2"
    location = "US"
    _PERSIST_SHAPES = ("daily_composition_atomic_v1", RUN_RECEIPT_TABLE, "AS row_set_digest")

    def __init__(self, snapshot):
        super().__init__()
        self.reader = composer_tests.FakeWarehouse(snapshot)

    def query(self, sql, *, job_config, location, retry=None, job_retry=None):
        target = super() if any(shape in sql for shape in self._PERSIST_SHAPES) else self.reader
        return target.query(
            sql, job_config=job_config, location=location, retry=retry, job_retry=job_retry
        )


def test_explicit_legacy_adapter_runs_dynamic_composition_to_its_run_receipt(monkeypatch):
    snapshot = composer_tests.snapshot()
    warehouse = ComposeWarehouse(snapshot)
    fixture = Fixture(
        monkeypatch,
        capture_runner=SnapshotRunner(),
        warehouse=warehouse,
        compose_binding=composition_binding(source_window=lambda entry: snapshot),
    )
    legacy = {name: value for name, value in fixture.clients.items() if name != "products"}
    legacy.update(
        native.compose_clients(
            composition_binding(source_window=lambda entry: snapshot),
            warehouse=warehouse,
            release_profile=fixture.clients["release_profile"],
            cutoff=CUTOFF,
            build=BUILD,
            now=fixture.clock,
            instant=STARTED,
        )
    )
    fixture.handlers = lambda: build_stage_handlers(legacy, profile=kernel_profile())
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "certify"}
    compose = fixture.kernel_store.read_stage(SLOT, "compose")
    assert compose["state"] == "succeeded", compose
    assert compose["retry_safe"] is False
    run_id = fixture.clients["release_profile"].run_id
    assert compose["result_reference"].endswith(f"{RUN_RECEIPT_TABLE}#{run_id}")
    assert len(warehouse.receipts) == 1
    assert warehouse.receipts[0]["run_id"] == run_id
    assert warehouse.receipts[0]["candidate_count"] == 1
    assert warehouse.receipts[0]["source_sha"] == BUILD["source_sha"]
    assert [len(rows) for rows in warehouse.target_rows.values()][:3] == [1, 2, 2]
    facts = legacy["composer"].compositions[run_id]
    assert facts.complete_partitions is True
    assert facts.rule_version == "composition_rules_v3"
    # Every admitted row carries its resolved geographic record, and the readiness the
    # composer evaluated ran under the daily floor rather than the replay's.
    (component,) = facts.geography_by_component
    assert {record.scope for record in facts.geography_by_component[component].values()} == {
        "local"
    }
    # The same cutoff run again reconciles to the one receipt and writes no second row.
    assert fixture.run() == {"state": "failed", "operation_id": SLOT, "stage": "certify"}
    assert len(warehouse.receipts) == 1
