"""Stage adapters for the daily cycle kernel, driven over fakes only."""

from __future__ import annotations

import ast
import re
import sys
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.staging import release_open_intelligence_run as release
from src.analysis.open_intelligence import daily_stages, live_quality, persistence
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.capture_registry import validate_capture_entry
from src.analysis.open_intelligence.daily_certification import CertificationEvidenceTruncated
from src.analysis.open_intelligence.daily_cycle import STAGE_ORDER, execute_daily_cycle
from src.analysis.open_intelligence.readiness import EvidenceRecord, ReadinessRules
from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest
from src.analysis.open_intelligence.staging_source_profile import validate_completed_source_run

from tests.unit import test_dynamic_quality_review as review_fixtures
from tests.unit.test_daily_cycle import FakeAuthority, FakeStore
from tests.unit.test_open_intelligence_release import _execution_proof, _retained_apply_binding
from tests.unit.test_open_intelligence_release_profiles import (
    STAGING_RUN_ID,
    _ProfileRecorder,
    staging_profile,
    staging_receipt_fields,
)

ENGINE_ROOT = Path(__file__).resolve().parents[2]
MODEL_CLIENT_MODULES = tuple(
    sorted(
        f"src.analysis.{path.stem}"
        for path in (ENGINE_ROOT / "src" / "analysis").glob("*_client.py")
    )
)
CUTOFF = datetime(2026, 9, 11, tzinfo=UTC)
STARTED = CUTOFF + timedelta(minutes=5)
COMPLETED = CUTOFF + timedelta(minutes=30)
POLICY_DIGEST = "e" * 64
OPERATION_ID = "slot-2026-09-10"


class Clock:
    def __init__(self, start: datetime = STARTED):
        self.now = start

    def __call__(self) -> datetime:
        current = self.now
        self.now = current + timedelta(minutes=1)
        return current


def kernel_profile(**overrides):
    base = {
        "schema_version": "42_daily_v1",
        "resource_manifest_digest": "9" * 64,
        "source_policy_digest": POLICY_DIGEST,
        "recurring_grant_digest": "d" * 64,
        "freshness_target_hours": 30,
        "max_publish_lag_hours": 48,
        "max_catchup_cutoffs": 2,
    }
    base.update(overrides)
    return base


def manifest(stage: str, *, predecessor: str = "", **overrides):
    values = {
        "operation_id": OPERATION_ID,
        "stage": stage,
        "cutoff_utc": CUTOFF.isoformat(),
        "profile": kernel_profile(),
        "predecessor_digest": predecessor,
    }
    values.update(overrides)
    return values


class IssuedAuthority:
    """The in process authority object a reservation path answers; identity is the contract."""

    def __init__(self, attempt: str) -> None:
        self.attempt = attempt
        self.generation = f"generation:{attempt}"


def issued_execution(stage: str, attempt: str) -> dict:
    """The receipt's ``execution`` entry as the daily authority issues it, objects and all."""
    authority = IssuedAuthority(attempt)
    return {
        "authority": authority,
        "consumption": {"consumption_id": f"exc_{attempt}", "operation": stage},
        "reservation": {"consumption_id": f"exc_{attempt}", "operation": stage},
        "authority_reference": f"42/daily/authority/{attempt}.json",
        "stage": stage,
        "operation": stage,
        "mode": "new_consume",
        "generation": authority.generation,
    }


def receipt_for(stage: str, *, execution: bool = True):
    receipt = {"business_attempt_id": f"attempt:{stage}:1", "authority_reference": "authority:1"}
    if execution:
        receipt["execution"] = issued_execution(stage, receipt["business_attempt_id"])
    return receipt


class IssuingAuthority(FakeAuthority):
    """The kernel's fake issuer carrying the issued execution authority, once per attempt."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.executions: dict[str, dict] = {}

    def issue(self, stage, manifest):
        receipt = super().issue(stage, manifest)
        if receipt is None:
            return None
        attempt = receipt["business_attempt_id"]
        if attempt not in self.executions:
            self.executions[attempt] = issued_execution(stage, attempt)
        return {**receipt, "execution": self.executions[attempt]}


def digest_of(value) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def collection_receipt(**overrides):
    base = {
        "contract_version": "collection_receipt_v1",
        "run_id": "collect-2026-09-10",
        "execution_id": "attempt:collect:1",
        "source_sha": "a" * 40,
        "image_uri": "region-docker.pkg.dev/project/repo/image@sha256:" + "b" * 64,
        "policy_sha256": POLICY_DIGEST,
        "profile_sha256": "6" * 64,
        "cutoff": "2026-09-10",
        "market_states": {"za": "collected", "ng": "collected", "ke": "collected"},
        "raw_rows_persisted": 10,
        "enriched_rows_persisted": 5,
        "funded_close": None,
        "complete": True,
    }
    base.update(overrides)
    return base


class FakeLedger:
    def __init__(self):
        self.receipts: dict[str, dict] = {}
        self.days: dict[str, list[dict]] = {}

    def operation(self, operation_id):
        found = self.receipts.get(operation_id)
        return None if found is None else dict(found)

    def market_day(self, trend_date):
        return [dict(item) for item in self.days.get(trend_date, [])]

    def record(self, receipt, *, trend_date):
        self.receipts[receipt["execution_id"]] = dict(receipt)
        self.days.setdefault(trend_date, []).append(dict(receipt))


class FakeCollector:
    """The producer terminal over fakes: records its receipt in the ledger, then returns it.

    Like the producer, it stamps the receipt with the attempt it is handed as the
    execution id; ``stamp_attempt=False`` models a terminal that stamps another id.
    """

    def __init__(
        self, ledger: FakeLedger, receipt: dict | None = None, *, explode=False, stamp_attempt=True
    ):
        self.ledger = ledger
        self.receipt = collection_receipt() if receipt is None else receipt
        self.explode = explode
        self.stamp_attempt = stamp_attempt
        self.calls = 0
        self.attempt_ids: list[object] = []

    def __call__(self, *, stop_after_ingestion: bool, attempt_id=None):
        assert stop_after_ingestion is True
        self.calls += 1
        self.attempt_ids.append(attempt_id)
        if self.explode:
            raise RuntimeError("vendor timeout after paid calls")
        receipt = dict(self.receipt)
        if self.stamp_attempt and attempt_id is not None:
            receipt["execution_id"] = attempt_id
        self.ledger.record(receipt, trend_date="2026-09-11")
        return dict(receipt)


class FakeSourceRuns:
    def __init__(self):
        self.runs: dict[str, dict] = {}

    def record(self, run_id, run):
        self.runs[run_id] = dict(run)

    def read(self, run_id):
        return {"status": "ok", "run": self.runs.get(run_id)}


def generation(*, capture_v2: bool, capture_datasets=("trends_v2_staging",)):
    rows = {
        ("open_intelligence_execution_manifest_v1", "5" * 64): SimpleNamespace(
            manifest_version="open_intelligence_execution_manifest_v1",
            operation_bindings={"source_snapshot_capture": object()},
            allowed_execution_modes=frozenset({"historical_read", "historical_replay"}),
        ),
        ("open_intelligence_execution_manifest_v2", "3" * 64): SimpleNamespace(
            manifest_version="open_intelligence_execution_manifest_v2",
            operation_bindings={"r3_release": object()},
            allowed_execution_modes=frozenset({"new_consume", "historical_read"}),
        ),
    }
    if capture_v2:
        rows[("open_intelligence_execution_manifest_v2", "7" * 64)] = SimpleNamespace(
            manifest_version="open_intelligence_execution_manifest_v2",
            operation_bindings={
                "source_snapshot_capture": SimpleNamespace(datasets=capture_datasets)
            },
            allowed_execution_modes=frozenset({"new_consume", "historical_read"}),
        )
    return SimpleNamespace(
        origin_registry_sha256="f" * 64, resource_manifest_sha256="9" * 64, registry=rows
    )


class FakeWriter:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.creates = 0

    def create(self, key, entry):
        self.creates += 1
        if key in self.objects:
            return {"status": "exists"}
        self.objects[key] = dict(entry)
        return {"status": "created"}

    def read(self, key):
        found = self.objects.get(key)
        return None if found is None else dict(found)


SNAPSHOT_LANES = (
    "enriched_content",
    "event_ledger",
    "raw_content",
    "seed_candidates",
    "seed_graph",
)


CAPTURE_ESTATE = "ogilvy-trends-v2.trends_v2_staging"


def creation_records(*, lanes=SNAPSHOT_LANES, states=None, estate=CAPTURE_ESTATE, **overrides):
    """Creation records shaped as the capture runner writes a succeeded one, per lane.

    A succeeded record carries the identity of the native job that was read back: a job
    named for its own lane and the digest over that job's own resource. The destination
    is the whole table name of the executing plan, whose tables are named for the
    retained capture rather than for the profile.
    """
    states = states or {}
    return [
        {
            "lane": lane,
            "destination": f"{estate}.open_intelligence_v3_source_20260910_{lane}",
            "job_id": f"oi_v3_snapshot_{'a' * 64}_{lane}",
            "native_job_digest": "d" * 64,
            "state": states.get(lane, "succeeded"),
            **overrides,
        }
        for lane in lanes
    ]


def compact_payload(*, captured_at: datetime, snapshot_digest: str = "1" * 64):
    return {
        "contract_version": "source_snapshot_capture_v1",
        "cutoff_date": "2026-09-10",
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "source_as_of": COMPLETED.isoformat(),
        "captured_at": captured_at.isoformat(),
        "snapshot_plan_digest": "5" * 64,
        "snapshot_digest": snapshot_digest,
        "capture_receipt_digest": "6" * 64,
        "creation_records": creation_records(),
        "artifact_attempt": 1,
        "stored_artifact": {"generation": "7"},
        "query_count": 3,
        "total_bytes_billed": 1024,
        "limitations": [],
        "missing_checks": [],
    }


class FakeCapture:
    def __init__(
        self,
        *,
        status="succeeded",
        verified_overrides=None,
        explode=False,
        refuse=None,
        verified_absent=(),
    ):
        self.status = status
        self.verified_overrides = verified_overrides or {}
        # Fields the recorded result does not carry, as a result written before the
        # coverage travelled with the readback does not carry the two coverage fields.
        self.verified_absent = tuple(verified_absent)
        self.explode = explode
        self.refuse = refuse
        self.writer = FakeWriter()
        self.calls: list[dict] = []
        self.captured_at = COMPLETED + timedelta(minutes=10)
        self.snapshots: dict[str, datetime] = {}

    def snapshot(self, run, *, snapshot_as_of, attempt_id, origin, authority):
        self.calls.append(
            {
                "run": run,
                "snapshot_as_of": snapshot_as_of,
                "attempt_id": attempt_id,
                "origin": origin,
                "authority": authority,
            }
        )
        if self.explode:
            raise RuntimeError("storage acknowledgement lost")
        if self.refuse is not None:
            raise daily_stages.StageRefusal(self.refuse)
        self.snapshots[attempt_id] = snapshot_as_of
        payload = compact_payload(captured_at=self.captured_at)
        payload["source_as_of"] = snapshot_as_of.isoformat()
        return payload, self.status

    def verified(self, attempt_id):
        if attempt_id not in self.snapshots:
            return None
        values = {
            "result_id": "res_" + "2" * 32,
            "content_digest": "1" * 64,
            "image_digest": "4" * 64,
            "generation": "7",
            "completion_state": self.status,
            "capture_available_at": self.captured_at,
            "snapshot_as_of": self.snapshots[attempt_id],
            "scope": "ogilvy_default",
            "schema_digest": "2" * 64,
            "source_digest": "3" * 64,
            "market_scope": ["ke", "ng", "za"],
            "creation_records": creation_records(),
        }
        values.update(self.verified_overrides)
        for field in self.verified_absent:
            values.pop(field, None)
        return values


class FakeComposer:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, entry, *, cutoff):
        self.calls.append({"entry": entry, "cutoff": cutoff})
        return {
            "result": SimpleNamespace(kind="extraction"),
            "scope": SimpleNamespace(client_scope_id="ogilvy_default"),
            "metrics_by_component": {},
            "receipts_by_component": {},
            "readiness_by_component": {},
            "readiness_rules": ReadinessRules(current_cutoff=CUTOFF, minimum_geo_confidence=0.8),
            "missing_by_component": {},
            "quality_evaluated": True,
        }


class Warehouse:
    """The fake warehouse: no run receipt until the persist client has written the batch,
    and a release record holding exactly what the display transaction inserted."""

    def __init__(self, recorder):
        self.recorder = recorder
        self.persisted = False
        self.inserted: dict = {}
        self.calls: list[str] = []

    def __call__(self, sql: str):
        self.calls.append(sql)
        receipt_read = f"SELECT {', '.join(release.RUN_RECEIPT_ROW_FIELDS)} FROM"
        if not self.persisted and sql.startswith(receipt_read):
            return []
        if sql.startswith("BEGIN TRANSACTION"):
            insert = sql[sql.index("INSERT INTO") :]
            literals = re.findall(
                r"'([^']*)'", insert[insert.index("SELECT") : insert.index(" FROM (SELECT 1)")]
            )
            values = dict(zip(release.QUALITY_RELEASE_FIELDS, literals, strict=True))
            values["released_at"] = datetime.strptime(
                values["released_at"].removesuffix("+00"), "%Y-%m-%d %H:%M:%S.%f"
            ).replace(tzinfo=UTC)
            self.inserted = values
        rows = self.recorder(sql)
        if release.RELEASE_RECORD_TABLE in sql and sql.startswith("SELECT run_id"):
            for row in rows:
                row.update(self.inserted)
        return rows


class FakePersist:
    def __init__(self, warehouse: Warehouse):
        self.warehouse = warehouse
        self.calls: list[dict] = []

    def __call__(self, bridge, *, run_id, signal_date, created_at):
        self.calls.append({"bridge": bridge, "run_id": run_id, "signal_date": signal_date})
        self.warehouse.persisted = True


def review_batch(run_id):
    """The review fixtures' one candidate batch, carried under the staging run id."""
    fixtures = review_fixtures.persistence_fixtures
    candidate = fixtures._candidate()
    candidate["run_id"] = run_id
    evidence = fixtures._evidence()
    evidence["run_id"] = run_id
    membership = fixtures._membership()
    membership["run_id"] = run_id
    return fixtures._batch(candidates=(candidate,), evidence=(evidence,), membership=(membership,))


def evidence_record(row_id, family, vendor, channel):
    return EvidenceRecord(
        row_id=row_id,
        source_family=family,
        direction="rising",
        published_at=CUTOFF - timedelta(hours=6),
        availability="available",
        geo_confidence=0.91,
        vendor_family=vendor,
        channel_family=channel,
    )


class FakeCertifier:
    def __init__(self, *, evidence_state="ready", records=None):
        self.evidence_state = evidence_state
        self.records = (
            [
                evidence_record("row-1", "reddit", "socialcrawl", "reddit"),
                evidence_record("row-2", "youtube", "google_youtube", "youtube"),
            ]
            if records is None
            else records
        )
        self.certifications: dict[str, dict] = {}
        self.calls: list[str] = []

    def readiness(self, run_id):
        self.calls.append(f"readiness:{run_id}")
        return {
            "candidates": [
                {
                    "candidate": {
                        "signal_id": "sig_" + "1" * 64,
                        "evidence_state": self.evidence_state,
                    },
                    "records": list(self.records),
                }
            ],
            "rules": ReadinessRules(
                current_cutoff=CUTOFF - timedelta(days=1), minimum_geo_confidence=0.8
            ),
            "quality_evaluated": True,
        }

    def artifacts(self, receipt):
        self.calls.append(f"artifacts:{receipt.run_id}")
        blocked = run_receipt_digest(receipt)
        proof = {
            **_execution_proof(blocked),
            "run_id": receipt.run_id,
            "signal_date": receipt.signal_date.isoformat(),
        }
        batch = review_batch(receipt.run_id)
        packet = live_quality.build_review_packet(receipt.run_id, batch)
        projection = live_quality.candidate_projection_digest(receipt.run_id, batch)
        review = review_fixtures._receipt(
            packet, run_id=receipt.run_id, candidate_projection_digest=projection
        )
        return {
            "execution_proof_bytes": canonical_bytes(proof),
            "execution_proof_manifest_sha256": "1" * 64,
            "quality_review_receipt": review,
            "review_packet": packet,
            "candidate_projection_digest": projection,
            "apply_binding": _retained_apply_binding(),
        }

    def record(self, run_id, evidence):
        self.certifications[run_id] = dict(evidence)

    def read(self, run_id):
        found = self.certifications.get(run_id)
        return None if found is None else dict(found)


class Fixture:
    def __init__(
        self,
        monkeypatch,
        *,
        capture_v2=True,
        collector=None,
        capture=None,
        capture_datasets=("trends_v2_staging",),
    ):
        self.collector = FakeCollector(FakeLedger()) if collector is None else collector
        self.ledger = self.collector.ledger
        self.source_runs = FakeSourceRuns()
        self.capture = FakeCapture() if capture is None else capture
        self.composer = FakeComposer()
        self.recorder = _ProfileRecorder(staging_receipt_fields())
        self.warehouse = Warehouse(self.recorder)
        self.persist = FakePersist(self.warehouse)
        self.certifier = FakeCertifier()
        self.store = FakeStore()
        self.clock = Clock()
        self.batches: list[dict] = []
        monkeypatch.setenv("TRENDS_ENV", "staging")
        monkeypatch.setenv("BIGQUERY_DATASET", "intelligence_42_sources_staging")

        def build_batch(result, **kwargs):
            self.batches.append({"result": result, **kwargs})
            return SimpleNamespace(batch=SimpleNamespace(rows=1), admitted=("c-1",), skipped=())

        monkeypatch.setattr(persistence, "build_producing_batch", build_batch)
        self.clients = {
            "clock": self.clock,
            "store": self.store,
            "collection_profile": {
                "environment": "staging",
                "source_dataset": "intelligence_42_sources_staging",
            },
            "collector": self.collector,
            "collection_ledger": self.ledger,
            "source_runs": self.source_runs,
            "generation": generation(capture_v2=capture_v2, capture_datasets=capture_datasets),
            "capture": self.capture,
            "composer": self.composer,
            "persist": self.persist,
            "warehouse": self.warehouse,
            "certifier": self.certifier,
            "release_profile": staging_profile(),
        }
        self.handlers = daily_stages.build_stage_handlers(self.clients, profile=kernel_profile())

    def run(self, *, deny=("release",), authority=None):
        return execute_daily_cycle(
            operation_id=OPERATION_ID,
            cutoff_utc=CUTOFF,
            profile=kernel_profile(),
            authority=IssuingAuthority(deny=deny) if authority is None else authority,
            store=self.store,
            stages=self.handlers,
        )

    def record(self, stage):
        return self.store.records[(OPERATION_ID, stage)]


def test_handler_set_matches_the_kernel_order_and_binds_the_profile_policy(monkeypatch):
    fixture = Fixture(monkeypatch)
    assert set(fixture.handlers) == set(STAGE_ORDER)
    with pytest.raises(ValueError, match="profile_policy_mismatch"):
        daily_stages.build_stage_handlers(
            fixture.clients, profile=kernel_profile(source_policy_digest="1" * 64)
        )
    with pytest.raises(ValueError, match="clients_inexact"):
        daily_stages.build_stage_handlers(
            {key: value for key, value in fixture.clients.items() if key != "warehouse"},
            profile=kernel_profile(),
        )


def test_collect_runs_the_terminal_once_and_reads_its_receipt_back(monkeypatch):
    fixture = Fixture(monkeypatch)
    result = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "succeeded"
    assert result["input_digest"] == digest_of(manifest("collect"))
    assert result["retry_safe"] is False
    assert result["result_reference"] == "source_run:collect-2026-09-10"
    recorded = fixture.source_runs.runs["collect-2026-09-10"]
    assert recorded["receipt"] == collection_receipt()
    assert recorded["collection_started_at"] == STARTED
    assert recorded["collection_completed_at"] == STARTED + timedelta(minutes=1)
    assert result["output_digest"] == canonical_digest(
        {
            "receipt": collection_receipt(),
            "observation_window_end": CUTOFF.isoformat(),
            "collection_started_at": STARTED.isoformat(),
            "collection_completed_at": (STARTED + timedelta(minutes=1)).isoformat(),
        }
    )
    assert fixture.collector.calls == 1
    again = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert again == result
    assert fixture.collector.calls == 1


def test_collect_refuses_before_dispatch_when_the_terminal_guards_fire(monkeypatch):
    fixture = Fixture(monkeypatch)
    monkeypatch.setenv("TRENDS_ENV", "production")
    result = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "failed"
    assert result["retry_safe"] is True
    assert result["result_reference"] == "collect_refused:runtime_environment"
    assert fixture.collector.calls == 0


def test_collect_hands_the_collector_the_attempt_it_keys_the_ledger_with(monkeypatch):
    fixture = Fixture(monkeypatch)
    reads: list[str] = []
    operation = fixture.ledger.operation
    monkeypatch.setattr(
        fixture.ledger, "operation", lambda key: (reads.append(key), operation(key))[1]
    )
    result = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "succeeded"
    attempt = receipt_for("collect")["business_attempt_id"]
    assert fixture.collector.attempt_ids == [attempt]
    assert reads[0] == attempt
    assert fixture.ledger.operation(attempt) == collection_receipt()


def test_collect_refuses_a_receipt_planted_under_the_attempt_with_a_foreign_execution_id(
    monkeypatch,
):
    fixture = Fixture(monkeypatch)
    attempt = receipt_for("collect")["business_attempt_id"]
    foreign = collection_receipt(execution_id="intelligence-42-daily-staging-exec9")
    fixture.ledger.receipts[attempt] = dict(foreign)
    result = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "unknown"
    assert result["retry_safe"] is False
    assert result["result_reference"] == ("collect_unresolved:collection_receipt_attempt_mismatch")
    assert fixture.collector.calls == 0
    assert fixture.source_runs.runs == {}


def test_collect_refuses_a_terminal_receipt_stamped_under_another_execution_id(monkeypatch):
    stamped = collection_receipt(execution_id="intelligence-42-daily-staging-exec9")
    fixture = Fixture(
        monkeypatch, collector=FakeCollector(FakeLedger(), stamped, stamp_attempt=False)
    )
    result = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "unknown"
    assert result["retry_safe"] is False
    assert result["result_reference"] == ("collect_unresolved:collection_receipt_attempt_mismatch")
    assert fixture.collector.calls == 1
    assert fixture.collector.attempt_ids == [receipt_for("collect")["business_attempt_id"]]
    assert fixture.source_runs.runs == {}


def test_collect_partial_and_unknown_results_are_never_repeated(monkeypatch):
    partial = collection_receipt(
        market_states={"za": "collected", "ng": "partial", "ke": "failed"}, complete=False
    )
    fixture = Fixture(monkeypatch, collector=FakeCollector(FakeLedger(), partial))
    first = fixture.run()
    assert first == {"state": "partial", "operation_id": OPERATION_ID, "stage": "collect"}
    assert fixture.record("collect")["retry_safe"] is False
    # The reference names the attempt the authority issued, which the terminal stamped.
    (attempt,) = fixture.collector.attempt_ids
    assert attempt.startswith("attempt:collect:")
    assert fixture.record("collect")["result_reference"] == f"collection:{attempt}"
    assert fixture.run() == {
        "state": "unavailable",
        "operation_id": OPERATION_ID,
        "stage": "collect",
    }
    assert fixture.collector.calls == 1

    exploding = FakeCollector(FakeLedger(), explode=True)
    fixture = Fixture(monkeypatch, collector=exploding)
    assert fixture.run() == {"state": "unknown", "operation_id": OPERATION_ID, "stage": "collect"}
    assert fixture.record("collect")["retry_safe"] is False
    assert fixture.record("collect")["output_digest"] == ""
    assert fixture.run() == {
        "state": "unavailable",
        "operation_id": OPERATION_ID,
        "stage": "collect",
    }
    assert exploding.calls == 1


def test_collect_holds_a_producer_shaped_timestamp_cutoff_as_a_clean_failure(monkeypatch):
    stamped = collection_receipt(cutoff="2026-09-11T00:30:00+00:00")
    fixture = Fixture(monkeypatch, collector=FakeCollector(FakeLedger(), stamped))
    result = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "failed"
    assert result["retry_safe"] is False
    assert result["result_reference"] == "collect_refused:source_run_invalid"


def test_collect_admits_a_terminal_shaped_receipt_with_the_closed_day_cutoff(monkeypatch):
    """A receipt shaped as the producer terminal writes it passes collect.

    The terminal derives the cutoff from its own start instant: the day before
    the run's start day, the last day that closed before collection began. The
    instants stay outside the receipt, on the source run record.
    """

    class TerminalShapedCollector(FakeCollector):
        clock = None

        def __call__(self, *, stop_after_ingestion: bool, attempt_id=None):
            assert stop_after_ingestion is True
            self.attempt_ids.append(attempt_id)
            self.calls += 1
            trend_date = self.clock().date()
            closed_day = trend_date - timedelta(days=1)
            self.receipt = collection_receipt(cutoff=closed_day.isoformat())
            self.ledger.record(self.receipt, trend_date=trend_date.isoformat())
            return dict(self.receipt)

    terminal = TerminalShapedCollector(FakeLedger())
    fixture = Fixture(monkeypatch, collector=terminal)
    terminal.clock = fixture.clock
    result = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "succeeded", result
    assert result["retry_safe"] is False
    assert result["result_reference"] == "source_run:collect-2026-09-10"
    assert terminal.calls == 1
    completed = STARTED + timedelta(minutes=2)
    recorded = fixture.source_runs.runs["collect-2026-09-10"]
    assert set(recorded["receipt"]) == set(collection_receipt())
    assert recorded["receipt"]["cutoff"] == "2026-09-10"
    assert recorded["collection_started_at"] == STARTED
    assert recorded["collection_completed_at"] == completed
    validated = validate_completed_source_run(recorded)
    assert validated["cutoff"] == date(2026, 9, 10)
    assert validated["observation_window_end"] == CUTOFF
    assert validated["collection_completed_at"] == completed
    assert result["output_digest"] == canonical_digest(
        {
            "receipt": recorded["receipt"],
            "observation_window_end": CUTOFF.isoformat(),
            "collection_started_at": STARTED.isoformat(),
            "collection_completed_at": completed.isoformat(),
        }
    )


def test_capture_hands_the_issued_authority_of_the_receipt_to_the_client(monkeypatch):
    fixture = Fixture(monkeypatch)
    issuer = IssuingAuthority(deny=("release",))
    fixture.run(authority=issuer)
    assert fixture.record("capture")["state"] == "succeeded"
    [call] = fixture.capture.calls
    attempt = call["attempt_id"]
    execution = issuer.executions[attempt]
    assert call["authority"] is execution
    assert call["authority"]["authority"] is execution["authority"]
    assert call["authority"]["consumption"] is execution["consumption"]
    assert call["authority"]["stage"] == "capture"
    assert call["authority"]["mode"] == "new_consume"
    assert call["authority"]["generation"] == execution["authority"].generation
    assert call["origin"] is not None
    assert (
        issuer.issue(
            "capture", manifest("capture", predecessor=fixture.record("collect")["output_digest"])
        )["execution"]
        is execution
    )


@pytest.mark.parametrize(
    "shape",
    [
        "absent",
        "authority_none",
        "consumption_none",
        "other_stage",
        "other_mode",
    ],
)
def test_capture_refuses_a_receipt_without_the_issued_authority_before_dispatch(monkeypatch, shape):
    class ReferenceOnlyAuthority(IssuingAuthority):
        def issue(self, stage, manifest):
            receipt = super().issue(stage, manifest)
            if receipt is None or stage != "capture":
                return receipt
            if shape == "absent":
                del receipt["execution"]
            else:
                execution = dict(receipt["execution"])
                execution.update(
                    {
                        "authority_none": {"authority": None},
                        "consumption_none": {"consumption": None},
                        "other_stage": {"stage": "collect"},
                        "other_mode": {"mode": "historical_read"},
                    }[shape]
                )
                receipt["execution"] = execution
            return receipt

    fixture = Fixture(monkeypatch)
    result = fixture.run(authority=ReferenceOnlyAuthority(deny=("release",)))
    assert result == {"state": "failed", "operation_id": OPERATION_ID, "stage": "capture"}
    record = fixture.record("capture")
    assert record["result_reference"] == "capture_refused:capture_authority_unbound"
    assert record["retry_safe"] is True
    assert record["output_digest"] == ""
    assert fixture.capture.calls == []
    assert fixture.capture.writer.objects == {}
    # Retry safe: a later invocation whose receipt carries the authority snapshots once.
    again = fixture.run(authority=IssuingAuthority(deny=("release",)))
    assert again == {"state": "release_pending", "operation_id": OPERATION_ID, "stage": "release"}
    assert fixture.record("capture")["state"] == "succeeded"
    assert len(fixture.capture.calls) == 1


@pytest.mark.parametrize("failure", ["explode", "refuse"])
def test_capture_failure_after_entry_stays_unknown_never_retry_safe(monkeypatch, failure):
    capture = (
        FakeCapture(explode=True)
        if failure == "explode"
        else FakeCapture(refuse="capture_authority_unbound")
    )
    fixture = Fixture(monkeypatch, capture=capture)
    result = fixture.run()
    assert result == {"state": "unknown", "operation_id": OPERATION_ID, "stage": "capture"}
    record = fixture.record("capture")
    assert record["retry_safe"] is False
    assert record["result_reference"] == (
        "capture_unresolved:capture_snapshot:RuntimeError"
        if failure == "explode"
        else "capture_unresolved:capture_authority_unbound"
    )
    assert len(capture.calls) == 1
    assert fixture.run() == {
        "state": "unavailable",
        "operation_id": OPERATION_ID,
        "stage": "capture",
    }
    assert len(capture.calls) == 1


def test_native_boundary_passes_a_stage_refusal_through_and_wraps_everything_else():
    def refusing():
        raise daily_stages.StageRefusal("certification_evidence_truncated:signal_evidence_v2")

    def exploding():
        raise RuntimeError("socket closed")

    with pytest.raises(daily_stages.StageRefusal, match=r"^certification_evidence_truncated:"):
        daily_stages._native(refusing, "certifier_readiness")
    with pytest.raises(daily_stages._Unresolved) as unresolved:
        daily_stages._native(exploding, "certifier_readiness")
    assert unresolved.value.code == "certifier_readiness:RuntimeError"
    assert daily_stages._native(lambda: "value", "certifier_readiness") == "value"


def test_certify_records_a_truncated_evidence_read_as_a_retry_safe_refusal(monkeypatch):
    fixture = Fixture(monkeypatch)

    def truncated(run_id):
        fixture.certifier.calls.append(f"readiness:{run_id}")
        raise CertificationEvidenceTruncated("signal_evidence_v2")

    fixture.certifier.readiness = truncated
    result = fixture.run()
    assert result == {"state": "failed", "operation_id": OPERATION_ID, "stage": "certify"}
    record = fixture.record("certify")
    assert record["result_reference"] == (
        "certify_refused:certification_evidence_truncated:signal_evidence_v2"
    )
    assert record["retry_safe"] is True
    assert record["output_digest"] == ""
    assert fixture.certifier.certifications == {}
    assert fixture.certifier.calls == [f"readiness:{STAGING_RUN_ID}"]
    assert issubclass(CertificationEvidenceTruncated, daily_stages.StageRefusal)


def test_capture_refuses_cleanly_while_no_v2_capture_origin_is_active(monkeypatch):
    fixture = Fixture(monkeypatch, capture_v2=False)
    result = fixture.run()
    assert result == {"state": "failed", "operation_id": OPERATION_ID, "stage": "capture"}
    record = fixture.record("capture")
    assert record["result_reference"] == "capture_refused:capture_origin_unavailable"
    assert record["retry_safe"] is True
    assert record["output_digest"] == ""
    assert fixture.capture.calls == []
    assert fixture.record("collect")["state"] == "succeeded"


def test_capture_snapshots_after_the_run_writes_and_admits_through_the_registry(monkeypatch):
    fixture = Fixture(monkeypatch)
    collect = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    fixture.store.record_stage(OPERATION_ID, "collect", collect)
    capture_manifest = manifest("capture", predecessor=collect["output_digest"])
    result = fixture.handlers["capture"](
        manifest=capture_manifest, authority_receipt=receipt_for("capture")
    )
    assert result["state"] == "succeeded", result
    call = fixture.capture.calls[0]
    assert call["snapshot_as_of"] >= call["run"]["collection_completed_at"]
    assert call["attempt_id"] == "attempt:capture:1"
    key = result["result_reference"].removeprefix("capture:")
    stored = validate_capture_entry(fixture.capture.writer.read(key))
    assert stored["observation_window_end"] == CUTOFF
    assert stored["snapshot_as_of"] == call["snapshot_as_of"]
    assert stored["capture_available_at"] == fixture.capture.captured_at
    assert stored["policy_digest"] == POLICY_DIGEST
    assert stored["operation_id"] == "attempt:capture:1"
    assert stored["completion_state"] == "succeeded"
    assert result["output_digest"] == canonical_digest(
        {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in stored.items()
        }
    )
    before = dict(fixture.capture.writer.objects)
    again = fixture.handlers["capture"](
        manifest=capture_manifest, authority_receipt=receipt_for("capture")
    )
    assert again == result
    assert len(fixture.capture.calls) == 1
    assert fixture.capture.writer.objects == before


def test_capture_failed_or_mismatched_native_results_are_not_admitted(monkeypatch):
    failed = FakeCapture(status="failed")
    fixture = Fixture(monkeypatch, capture=failed)
    result = fixture.run()
    assert result == {"state": "failed", "operation_id": OPERATION_ID, "stage": "capture"}
    assert fixture.record("capture")["retry_safe"] is False
    assert failed.writer.objects == {}

    mismatched = FakeCapture(verified_overrides={"content_digest": "8" * 64})
    fixture = Fixture(monkeypatch, capture=mismatched)
    assert fixture.run() == {"state": "unknown", "operation_id": OPERATION_ID, "stage": "capture"}
    assert fixture.record("capture")["result_reference"] == "capture_unresolved:authority_mismatch"
    assert mismatched.writer.objects == {}
    assert fixture.run() == {
        "state": "unavailable",
        "operation_id": OPERATION_ID,
        "stage": "capture",
    }
    assert len(mismatched.calls) == 1


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"creation_records": []}, "capture_tables_unadmitted"),
        (
            {"creation_records": creation_records(lanes=("raw_content", "enriched_content"))},
            "capture_tables_unadmitted",
        ),
        (
            {"creation_records": creation_records(states={"seed_graph": "failed"})},
            "capture_table_unadmitted:failed",
        ),
        (
            {"creation_records": creation_records(states={"raw_content": "unresolved"})},
            "capture_table_unadmitted:unresolved",
        ),
        ({"market_scope": ["ke", "ng"]}, "capture_markets_unadmitted"),
        ({"market_scope": []}, "capture_markets_unadmitted"),
        (
            {"creation_records": creation_records(native_job_digest=None)},
            "capture_native_job_unobserved:enriched_content",
        ),
        (
            {"creation_records": creation_records(native_job_digest="")},
            "capture_native_job_unobserved:enriched_content",
        ),
        (
            {"creation_records": creation_records(job_id="")},
            "capture_job_unobserved:enriched_content",
        ),
        (
            {"creation_records": creation_records(job_id=None)},
            "capture_job_unobserved:enriched_content",
        ),
        (
            {
                "creation_records": creation_records(
                    destination=(
                        "ogilvy-trends-v2.trends_v2_staging."
                        "open_intelligence_v3_source_20260910_raw_content"
                    )
                )
            },
            "capture_snapshot_tables_unadmitted:enriched_content",
        ),
        (
            {"creation_records": creation_records(estate="another-project.trends_v2_staging")},
            "capture_estate_unadmitted",
        ),
    ],
)
def test_capture_is_registered_only_after_every_table_and_market_is_admitted(
    monkeypatch, overrides, code
):
    """A native result that did not reach the whole table and market set is never registered."""
    partial = FakeCapture(verified_overrides=overrides)
    fixture = Fixture(monkeypatch, capture=partial)
    assert fixture.run() == {"state": "unknown", "operation_id": OPERATION_ID, "stage": "capture"}
    assert fixture.record("capture")["result_reference"] == f"capture_unresolved:{code}"
    assert fixture.record("capture")["retry_safe"] is False
    assert partial.writer.objects == {}
    assert len(partial.calls) == 1


def test_capture_coverage_is_admitted_again_when_a_prior_native_result_is_reconciled(monkeypatch):
    """A second invocation reconciles the recorded result and admits its coverage again."""
    partial = FakeCapture(verified_overrides={"creation_records": []})
    fixture = Fixture(monkeypatch, capture=partial)
    collect = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    fixture.store.record_stage(OPERATION_ID, "collect", collect)
    capture_manifest = manifest("capture", predecessor=collect["output_digest"])
    first = fixture.handlers["capture"](
        manifest=capture_manifest, authority_receipt=receipt_for("capture")
    )
    assert first["state"] == "unknown"
    reconciled = fixture.handlers["capture"](
        manifest=capture_manifest, authority_receipt=receipt_for("capture")
    )
    assert reconciled["state"] == "unknown"
    assert reconciled["result_reference"] == "capture_unresolved:capture_tables_unadmitted"
    assert partial.writer.objects == {}
    assert len(partial.calls) == 1


def test_a_result_that_says_succeeded_without_an_observed_native_job_is_not_registered(
    monkeypatch,
):
    """Five records saying succeeded, with no native job behind any of them, register nothing.

    This is the shape the coverage had to rule out: the state of a creation record is the
    result's own word, while the native job digest is the one field derived from a job
    that was read back.
    """
    asserted = FakeCapture(
        verified_overrides={
            "creation_records": creation_records(native_job_digest=None, job_id=""),
        }
    )
    fixture = Fixture(monkeypatch, capture=asserted)
    assert fixture.run() == {"state": "unknown", "operation_id": OPERATION_ID, "stage": "capture"}
    reference = fixture.record("capture")["result_reference"]
    assert reference == "capture_unresolved:capture_job_unobserved:enriched_content"
    assert asserted.writer.objects == {}


@pytest.mark.parametrize(
    ("overrides", "absent"),
    [
        ({"contract_version": "capture_v1"}, ()),
        ({}, ("scope",)),
        ({}, ("creation_records",)),
        ({}, ("market_scope",)),
    ],
)
def test_a_recorded_result_of_any_other_shape_is_still_an_invalid_readback(
    monkeypatch, overrides, absent
):
    """Two readback shapes are read, the current one and the pre coverage one, and no other.

    Reading the older shape is a named exception, not an open door: a result carrying an
    unknown field, a missing field or one coverage field without the other is refused.
    """
    odd = FakeCapture(verified_overrides=overrides, verified_absent=absent)
    fixture = Fixture(monkeypatch, capture=odd)
    assert fixture.run() == {"state": "unknown", "operation_id": OPERATION_ID, "stage": "capture"}
    assert (
        fixture.record("capture")["result_reference"]
        == "capture_unresolved:capture_readback_invalid"
    )
    assert odd.writer.objects == {}


def test_a_result_recorded_before_the_coverage_is_read_and_named_not_treated_as_corrupt(
    monkeypatch,
):
    """An attempt whose result predates the coverage reports the one thing missing from it.

    The result seam is create only and compares bytes, so such an object can never be
    rewritten with the coverage. Reading it as an invalid record would wedge the attempt
    for good; reading it and refusing the admission by name leaves the operator a reason.
    """
    older = FakeCapture(verified_absent=("market_scope", "creation_records"))
    fixture = Fixture(monkeypatch, capture=older)
    collect = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    fixture.store.record_stage(OPERATION_ID, "collect", collect)
    capture_manifest = manifest("capture", predecessor=collect["output_digest"])
    first = fixture.handlers["capture"](
        manifest=capture_manifest, authority_receipt=receipt_for("capture")
    )
    assert first["result_reference"] == "capture_unresolved:capture_coverage_unrecorded"
    reconciled = fixture.handlers["capture"](
        manifest=capture_manifest, authority_receipt=receipt_for("capture")
    )
    assert reconciled["state"] == "unknown"
    assert reconciled["result_reference"] == "capture_unresolved:capture_coverage_unrecorded"
    assert older.writer.objects == {}
    assert len(older.calls) == 1


def test_a_capture_writing_outside_the_datasets_its_origin_binds_is_not_registered(monkeypatch):
    """The estate the capture writes into must be one the approved origin declares.

    The stage resolves the capture origin before it dispatches, and that origin binds the
    datasets the capture operation may write. The coverage admission is given them, so a
    producer whose destinations drift out of the approved estate registers nothing.
    """
    fixture = Fixture(monkeypatch, capture_datasets=("intelligence_42_sources_staging",))
    assert fixture.run() == {"state": "unknown", "operation_id": OPERATION_ID, "stage": "capture"}
    record = fixture.record("capture")
    assert record["result_reference"] == "capture_unresolved:capture_dataset_undeclared"
    assert record["retry_safe"] is False
    assert fixture.capture.writer.objects == {}


def test_a_runner_that_reports_no_coverage_is_named_apart_from_a_result_that_predates_it(
    monkeypatch,
):
    """Two causes of a missing coverage, told apart by name.

    A result object written before the coverage travelled carries neither field; a runner
    that answered nothing today carries both and reports nothing in them. An operator
    reading the attempt can tell a legacy object from a broken runner.
    """
    silent = FakeCapture(verified_overrides={"market_scope": None, "creation_records": None})
    fixture = Fixture(monkeypatch, capture=silent)
    assert fixture.run() == {"state": "unknown", "operation_id": OPERATION_ID, "stage": "capture"}
    assert (
        fixture.record("capture")["result_reference"]
        == "capture_unresolved:capture_coverage_unreported"
    )
    assert silent.writer.objects == {}


def test_the_registered_entry_declares_the_tables_the_capture_actually_created(monkeypatch):
    """The stored entry's snapshot tables are the tables the admitted records name.

    The entry is what the registry keeps of a capture. Its snapshot tables are the last
    segments of the destinations the executing plan writes, so the field an operator
    reads names tables that exist rather than tables of a plan that cannot run.
    """
    fixture = Fixture(monkeypatch)
    fixture.run()
    assert fixture.record("capture")["state"] == "succeeded"
    stored = next(iter(fixture.capture.writer.objects.values()))
    assert stored["snapshot_tables"] == tuple(
        sorted(f"open_intelligence_v3_source_20260910_{lane}" for lane in SNAPSHOT_LANES)
    )
    admitted = {record["destination"].rsplit(".", 1)[-1] for record in creation_records()}
    assert set(stored["snapshot_tables"]) == admitted


@pytest.mark.parametrize("datasets", [(), ["trends_v2_staging"], None, ("", "x"), (7,)])
def test_a_capture_origin_that_does_not_name_its_datasets_refuses_before_dispatch(
    monkeypatch, datasets
):
    """An origin binding the capture operation without naming its datasets is not it.

    The admission compares the estate the capture wrote into against the datasets the
    approved origin binds, so an origin that names none, or names something other than a
    non empty tuple of dataset names, is refused while the refusal is still retry safe
    and before any capture is paid for.
    """
    fixture = Fixture(monkeypatch, capture_datasets=datasets)
    result = fixture.run()
    assert result == {"state": "failed", "operation_id": OPERATION_ID, "stage": "capture"}
    record = fixture.record("capture")
    assert record["result_reference"] == "capture_refused:capture_origin_unavailable"
    assert record["retry_safe"] is True
    assert fixture.capture.calls == []
    assert fixture.capture.writer.objects == {}


def test_compose_builds_the_batch_under_the_explicit_origin_policy(monkeypatch):
    fixture = Fixture(monkeypatch)
    result = fixture.run()
    assert result == {"state": "release_pending", "operation_id": OPERATION_ID, "stage": "release"}
    compose = fixture.record("compose")
    assert compose["state"] == "succeeded"
    assert len(fixture.batches) == 1
    batch = fixture.batches[0]
    assert batch["independence_policy"] == "explicit_origin_v2"
    assert batch["signal_date"].isoformat() == "2026-09-10"
    assert batch["result"].kind == "extraction"
    assert fixture.persist.calls[0]["run_id"] == STAGING_RUN_ID
    composed_entry = fixture.composer.calls[0]["entry"]
    assert composed_entry["completion_state"] == "succeeded"
    receipt = build_run_receipt(**staging_receipt_fields())
    assert compose["output_digest"] == run_receipt_digest(receipt)
    assert compose["result_reference"] == (
        f"bq://{release.PROJECT}.{release.DATASET}.{release.RECEIPT_TABLE}#{STAGING_RUN_ID}"
    )


def test_certify_carries_the_readiness_policy_into_the_release_evidence(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.run()
    certify = fixture.record("certify")
    assert certify["state"] == "succeeded"
    recorded = fixture.certifier.certifications[STAGING_RUN_ID]
    assert recorded["independence_policy"] == "explicit_origin_v2"
    assert recorded["profile_digest"] == staging_profile().digest
    assert recorded["blocked_run_receipt_digest"] == run_receipt_digest(
        build_run_receipt(**staging_receipt_fields())
    )
    assert certify["output_digest"] == canonical_digest(recorded)
    assert certify["result_reference"] == f"certification:{STAGING_RUN_ID}"
    assert fixture.certifier.calls == [
        f"readiness:{STAGING_RUN_ID}",
        f"artifacts:{STAGING_RUN_ID}",
    ]


def test_certify_refuses_a_ready_candidate_without_independent_support(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.certifier.records = [
        evidence_record("row-1", "reddit", "socialcrawl", "reddit"),
        evidence_record("row-2", "news", "socialcrawl", "news"),
    ]
    result = fixture.run()
    assert result == {"state": "failed", "operation_id": OPERATION_ID, "stage": "certify"}
    record = fixture.record("certify")
    assert record["result_reference"] == "certify_refused:certify_readiness_differs"
    assert record["retry_safe"] is True
    assert fixture.certifier.certifications == {}


def test_release_stays_pending_without_authority_and_releases_under_it(monkeypatch):
    fixture = Fixture(monkeypatch)
    first = fixture.run()
    assert first == {"state": "release_pending", "operation_id": OPERATION_ID, "stage": "release"}
    assert [fixture.record(stage)["state"] for stage in STAGE_ORDER[:4]] == ["succeeded"] * 4
    assert (OPERATION_ID, "release") not in fixture.store.records
    assert not any(sql.startswith("BEGIN TRANSACTION") for sql in fixture.warehouse.calls)
    held = fixture.handlers["release"](
        manifest=manifest("release", predecessor=fixture.record("certify")["output_digest"]),
        authority_receipt=None,
    )
    assert held["state"] == "release_pending"
    assert held["retry_safe"] is False
    assert not any(sql.startswith("BEGIN TRANSACTION") for sql in fixture.warehouse.calls)

    second = fixture.run(deny=())
    assert second == {"state": "succeeded", "operation_id": OPERATION_ID, "stage": "release"}
    transactions = [sql for sql in fixture.warehouse.calls if sql.startswith("BEGIN TRANSACTION")]
    assert len(transactions) == 1
    release_record = fixture.record("release")
    assert release_record["state"] == "succeeded"
    assert release_record["result_reference"] == (
        f"bq://{release.PROJECT}.{release.DATASET}.{release.RELEASE_RECORD_TABLE}#{STAGING_RUN_ID}"
    )
    readback = fixture.warehouse(
        f"SELECT {', '.join(release.QUALITY_RELEASE_FIELDS)} FROM "
        f"`{release.PROJECT}.{release.DATASET}.{release.RELEASE_RECORD_TABLE}` "
        f"WHERE run_id = '{STAGING_RUN_ID}'"
    )
    assert release_record["output_digest"] == canonical_digest(dict(readback[0]))
    assert fixture.collector.calls == 1
    assert len(fixture.capture.calls) == 1
    assert len(fixture.composer.calls) == 1
    assert fixture.certifier.calls.count(f"artifacts:{STAGING_RUN_ID}") == 1


def test_release_script_live_path_runs_through_the_adapter_seam(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.run()
    adapter = daily_stages.release_adapter(fixture.clients, operation_id=OPERATION_ID)
    profile = staging_profile()
    monkeypatch.setitem(release.RELEASE_PROFILES, profile.profile_name, profile)
    monkeypatch.setattr(release.bigquery, "Client", lambda **_kwargs: pytest.fail("client built"))
    monkeypatch.setattr(
        release.execution_generations, "active_generation", lambda: fixture.clients["generation"]
    )
    monkeypatch.setattr(release, "_operation_binding", lambda *a, **k: pytest.fail("bound"))
    with pytest.raises(release.ReleaseRefusal) as refused:
        release.main(["--profile", profile.profile_name])
    assert refused.value.code == "release_profile_adapter_unavailable"
    assert release.release_with_adapter(["--profile", profile.profile_name], adapter=adapter) == 0
    with pytest.raises(release.ReleaseRefusal) as missing:
        release.release_with_adapter(["--profile", profile.profile_name], adapter=None)
    assert missing.value.code == "release_profile_adapter_unavailable"
    transactions = [sql for sql in fixture.warehouse.calls if sql.startswith("BEGIN TRANSACTION")]
    assert len(transactions) == 1
    with pytest.raises(release.ReleaseRefusal) as replay:
        release.release_live_profile(release.R3_PROFILE, adapter=adapter, released_at=CUTOFF)
    assert replay.value.code == "release_profile_invalid"


def test_input_digest_conflicts_are_refused(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.store.records[(OPERATION_ID, "collect")] = {
        "state": "succeeded",
        "input_digest": "0" * 64,
        "output_digest": "1" * 64,
        "result_reference": "source_run:collect-2026-09-10",
        "retry_safe": False,
    }
    with pytest.raises(ValueError, match="stage_input_conflict"):
        fixture.run()
    assert fixture.collector.calls == 0
    with pytest.raises(ValueError, match="stage_manifest_invalid"):
        fixture.handlers["capture"](
            manifest=manifest("collect"), authority_receipt=receipt_for("capture")
        )
    with pytest.raises(ValueError, match="authority_receipt_invalid"):
        fixture.handlers["collect"](manifest=manifest("collect"), authority_receipt={})


def test_capture_refuses_a_predecessor_that_differs_from_the_collect_record(monkeypatch):
    fixture = Fixture(monkeypatch)
    collect = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    fixture.store.record_stage(OPERATION_ID, "collect", collect)
    result = fixture.handlers["capture"](
        manifest=manifest("capture", predecessor="2" * 64), authority_receipt=receipt_for("capture")
    )
    assert result["state"] == "failed"
    assert result["retry_safe"] is True
    assert result["result_reference"] == "capture_refused:predecessor_record_differs"
    assert fixture.capture.calls == []


def test_no_model_client_reaches_any_handler_path(monkeypatch):
    assert MODEL_CLIENT_MODULES
    tree = ast.parse(
        (ENGINE_ROOT / "src" / "analysis" / "open_intelligence" / "daily_stages.py").read_text(
            encoding="utf-8"
        )
    )
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert [name for name in imported if name in MODEL_CLIENT_MODULES] == []
    for name in MODEL_CLIENT_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    fixture = Fixture(monkeypatch)
    fixture.run()
    fixture.run(deny=())
    loaded = sorted(name for name in sys.modules if name in MODEL_CLIENT_MODULES)
    assert loaded == []


# D05: the capture boundary records its observation level telemetry as unknown


def test_capture_records_the_boundary_as_unknown_when_a_telemetry_client_is_present(monkeypatch):
    from src.analysis.open_intelligence.coverage_telemetry_sink import (
        BoundaryTelemetry,
        MemoryDispositionSink,
    )

    fixture = Fixture(monkeypatch)
    telemetry = BoundaryTelemetry(MemoryDispositionSink())
    clients = {**fixture.clients, "telemetry": telemetry}
    handlers = daily_stages.build_stage_handlers(clients, profile=kernel_profile())
    collect = handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    fixture.store.record_stage(OPERATION_ID, "collect", collect)
    result = handlers["capture"](
        manifest=manifest("capture", predecessor=collect["output_digest"]),
        authority_receipt=receipt_for("capture"),
    )
    assert result["state"] == "succeeded", result
    assert telemetry.records == []
    assert telemetry.notes == [
        {
            "kind": "unavailable",
            "boundary": "capture",
            "operation_id": "attempt:capture:1",
            "reason_code": "capture_result_without_observation_identity",
            "market": None,
            "observed_at": None,
        }
    ]
    with pytest.raises(ValueError, match="clients_inexact"):
        daily_stages.build_stage_handlers({**clients, "extra": object()}, profile=kernel_profile())


def test_compose_hands_the_telemetry_client_to_the_bridge_and_reserves_the_field(monkeypatch):
    from src.analysis.open_intelligence.coverage_telemetry_sink import (
        BoundaryTelemetry,
        MemoryDispositionSink,
    )

    fixture = Fixture(monkeypatch)
    fixture.run()
    assert fixture.batches[0]["telemetry"] is None
    telemetry = BoundaryTelemetry(MemoryDispositionSink())
    fixture = Fixture(monkeypatch)
    fixture.clients["telemetry"] = telemetry
    fixture.handlers = daily_stages.build_stage_handlers(fixture.clients, profile=kernel_profile())
    fixture.run()
    assert fixture.batches[0]["telemetry"] is telemetry
    assert "telemetry" in daily_stages._RESERVED_BATCH_FIELDS


def test_collect_reaches_the_boundary_under_the_authority_this_module_declares(monkeypatch):
    """The call site passes the declared constant, not some other name.

    Reading ``COLLECTION_AUTHORITY`` proves only what the constant says. This
    reads what the stage hands the collection boundary when it runs, so a call
    site naming a different unverified authority is caught here.
    """
    fixture = Fixture(monkeypatch)
    seen = []
    real = daily_stages.collect_staging

    def spy(profile, *, authority, collector):
        seen.append(authority)
        return real(profile, authority=authority, collector=collector)

    monkeypatch.setattr(daily_stages, "collect_staging", spy)
    result = fixture.handlers["collect"](
        manifest=manifest("collect"), authority_receipt=receipt_for("collect")
    )
    assert result["state"] == "succeeded"
    assert seen == [daily_stages.COLLECTION_AUTHORITY]
    assert seen == [{"authority_kind": "unverified", "declared_by": "daily_collect_stage"}]
