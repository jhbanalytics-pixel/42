"""Immutable staging persistence for the funded SocialCrawl credit ledger."""

from __future__ import annotations

import importlib
import importlib.util
import socket
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.cloud import bigquery
from src.contracts.bigquery_ddl import parse_table_ddl

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging_funded"
LOCATION = "US"
WRITER_IDENTITY = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
TABLE = "socialcrawl_credit_ledger_v1"
MODULE = "src.analysis.open_intelligence.funded_lane_persistence"
NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "infra" / "bigquery_schemas" / f"{TABLE}.sql"
)

ROW_FIELDS = (
    "ledger_id",
    "execution_id",
    "run_id",
    "trend_date",
    "recorded_at",
    "credential_lane",
    "market",
    "phase",
    "event_type",
    "calls",
    "budget_debit_credits",
    "vendor_reported_credits",
    "balance_observed",
    "opening_balance",
    "month_opening_balance",
    "month_start",
    "monthly_ledger_debit_before",
    "monthly_balance_delta_before",
    "monthly_effective_spend_before",
    "monthly_cap",
    "reserve_floor",
    "run_allowance",
    "catalog_digest",
    "metadata_digest",
    "attribution_state",
    "created_at",
)
NATURAL_KEY = (
    "execution_id",
    "credential_lane",
    "market",
    "phase",
    "event_type",
)
EXPECTED_SCHEMA = (
    ("ledger_id", "STRING", "REQUIRED"),
    ("execution_id", "STRING", "REQUIRED"),
    ("run_id", "STRING", "REQUIRED"),
    ("trend_date", "DATE", "REQUIRED"),
    ("recorded_at", "TIMESTAMP", "REQUIRED"),
    ("credential_lane", "STRING", "REQUIRED"),
    ("market", "STRING", "NULLABLE"),
    ("phase", "STRING", "REQUIRED"),
    ("event_type", "STRING", "REQUIRED"),
    ("calls", "INT64", "REQUIRED"),
    ("budget_debit_credits", "NUMERIC", "REQUIRED"),
    ("vendor_reported_credits", "NUMERIC", "REQUIRED"),
    ("balance_observed", "NUMERIC", "NULLABLE"),
    ("opening_balance", "NUMERIC", "REQUIRED"),
    ("month_opening_balance", "NUMERIC", "REQUIRED"),
    ("month_start", "DATE", "REQUIRED"),
    ("monthly_ledger_debit_before", "NUMERIC", "REQUIRED"),
    ("monthly_balance_delta_before", "NUMERIC", "REQUIRED"),
    ("monthly_effective_spend_before", "NUMERIC", "REQUIRED"),
    ("monthly_cap", "NUMERIC", "REQUIRED"),
    ("reserve_floor", "NUMERIC", "REQUIRED"),
    ("run_allowance", "NUMERIC", "REQUIRED"),
    ("catalog_digest", "STRING", "REQUIRED"),
    ("metadata_digest", "STRING", "REQUIRED"),
    ("attribution_state", "STRING", "REQUIRED"),
    ("created_at", "TIMESTAMP", "REQUIRED"),
)


def _module():
    importlib.invalidate_caches()
    spec = importlib.util.find_spec(MODULE)
    assert spec is not None, f"missing implementation module: {MODULE}"
    return importlib.import_module(MODULE)


def _row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ledger_id": "ledger_001",
        "execution_id": "execution_001",
        "run_id": "run_001",
        "trend_date": date(2026, 8, 27),
        "recorded_at": NOW,
        "credential_lane": "ogilvy_funded",
        "market": "za",
        "phase": "search",
        "event_type": "phase_close",
        "calls": 1,
        "budget_debit_credits": Decimal("2"),
        "vendor_reported_credits": Decimal("1"),
        "balance_observed": Decimal("250099"),
        "opening_balance": Decimal("250100"),
        "month_opening_balance": Decimal("250100"),
        "month_start": date(2026, 8, 1),
        "monthly_ledger_debit_before": Decimal("0"),
        "monthly_balance_delta_before": Decimal("0"),
        "monthly_effective_spend_before": Decimal("0"),
        "monthly_cap": Decimal("25000"),
        "reserve_floor": Decimal("225000"),
        "run_allowance": Decimal("100"),
        "catalog_digest": "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6",
        "metadata_digest": "501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a",
        "attribution_state": "complete",
        "created_at": NOW,
    }
    values.update(overrides)
    return values


def _batch(*rows: dict[str, object]):
    module = _module()
    return module.FundedLaneLedgerBatch(rows=rows or (_row(),))


def _wave1_rows() -> tuple[dict[str, object], ...]:
    phases = (
        "wave1_tiktok_sound",
        "wave1_instagram_reels",
        "wave1_youtube_shorts_comments",
        "wave1_reddit_comments",
    )
    phase_rows = tuple(
        _row(
            ledger_id=f"ledger_{phase}",
            market=None,
            phase=phase,
            event_type="phase_close",
            calls=1,
            budget_debit_credits=Decimal("1"),
            vendor_reported_credits=Decimal("1"),
        )
        for phase in phases
    )
    return (
        *phase_rows,
        _row(
            ledger_id="ledger_wave1_close",
            market=None,
            phase="run_close",
            event_type="run_close",
            calls=4,
            budget_debit_credits=Decimal("4"),
            vendor_reported_credits=Decimal("4"),
        ),
    )


def test_wave1_batch_accepts_exactly_four_global_phase_closes_and_one_run_close():
    assert len(_batch(*_wave1_rows()).rows) == 5


@pytest.mark.parametrize("mutation", ["missing", "market", "mixed", "totals"])
def test_wave1_batch_refuses_every_close_cardinality_mutation(mutation):
    rows = [dict(row) for row in _wave1_rows()]
    if mutation == "missing":
        rows.pop(0)
    elif mutation == "market":
        rows[0]["market"] = "za"
    elif mutation == "mixed":
        rows[0]["phase"] = "search"
    else:
        rows[-1]["calls"] = 3
    with pytest.raises(_module().LedgerBatchInvalid):
        _batch(*rows)


class _Credentials:
    def __init__(
        self,
        service_account_email: str = WRITER_IDENTITY,
        quota_project_id: str | None = PROJECT,
    ) -> None:
        self.service_account_email = service_account_email
        self.quota_project_id = quota_project_id


class _LoadJob:
    errors = None

    def __init__(self, output_rows: int) -> None:
        self.output_rows = output_rows

    def result(self) -> None:
        return None


class _QueryJob:
    errors = None

    def __init__(self, row: dict[str, int]) -> None:
        self.row = row

    def result(self, *, retry=None, job_retry=None):
        assert retry is None
        assert job_retry is None
        return (self.row,)


class _FakeClient:
    def __init__(
        self,
        *,
        project: str = PROJECT,
        location: str = LOCATION,
        writer_identity: str = WRITER_IDENTITY,
        target_rows: tuple[dict[str, object], ...] = (),
        delete_error: Exception | None = None,
    ) -> None:
        self.project = project
        self.dataset = DATASET
        self.location = location
        self.writer_identity = writer_identity
        self._credentials = _Credentials(writer_identity)
        self.target_rows = [dict(row) for row in target_rows]
        self.delete_error = delete_error
        self.created: list[bigquery.Table] = []
        self.loaded: list[tuple[list[dict[str, object]], bigquery.Table]] = []
        self.queries: list[str] = []
        self.deleted: list[bigquery.Table] = []

    def create_table(self, table: bigquery.Table, *, exists_ok: bool = False):
        assert exists_ok is False
        self.created.append(table)
        return table

    def load_table_from_json(
        self,
        rows: list[dict[str, object]],
        destination: bigquery.Table,
        *,
        location: str,
        job_config: bigquery.LoadJobConfig,
    ) -> _LoadJob:
        assert location == LOCATION
        assert job_config.write_disposition == bigquery.WriteDisposition.WRITE_TRUNCATE
        self.loaded.append((rows, destination))
        return _LoadJob(len(rows))

    @staticmethod
    def _key(row: dict[str, object]) -> tuple[object, ...]:
        return tuple(row[field] for field in NATURAL_KEY)

    def query(
        self,
        sql: str,
        *,
        location: str,
        job_config: bigquery.QueryJobConfig,
        retry,
        job_retry,
    ) -> _QueryJob:
        assert location == LOCATION
        assert job_config.use_legacy_sql is False
        assert retry is None
        assert job_retry is None
        self.queries.append(sql)
        staged_rows = self.loaded[-1][0]
        unchanged = 0
        conflicts = 0
        absent: list[dict[str, object]] = []
        for staged in staged_rows:
            matches = [row for row in self.target_rows if self._key(row) == self._key(staged)]
            if not matches:
                absent.append(staged)
            elif matches[0] == staged:
                unchanged += 1
            else:
                conflicts += 1
        guarded = (
            "ASSERT conflict_count = 0" in sql and "TO_JSON_STRING(STRUCT(" in sql and " != " in sql
        )
        if conflicts and guarded:
            return _QueryJob(
                {
                    "inserted_count": 0,
                    "unchanged_count": 0,
                    "conflict_count": conflicts,
                    "status": 1,
                }
            )
        inserted = 0
        if f"INSERT INTO `{PROJECT}.{DATASET}.{TABLE}`" in sql:
            self.target_rows.extend(dict(row) for row in absent)
            inserted = len(absent)
        return _QueryJob(
            {
                "inserted_count": inserted,
                "unchanged_count": unchanged,
                "conflict_count": 0,
                "status": 0,
            }
        )

    def delete_table(self, table: bigquery.Table, *, not_found_ok: bool) -> None:
        assert not_found_ok is True
        self.deleted.append(table)
        if self.delete_error is not None:
            raise self.delete_error


class _UncertainCreateClient(_FakeClient):
    def create_table(self, table: bigquery.Table, *, exists_ok: bool = False):
        super().create_table(table, exists_ok=exists_ok)
        raise RuntimeError("connection lost after create")


def _write(client: object, batch=None):
    module = _module()
    return module.persist_funded_lane_rows(
        project=PROJECT,
        dataset=DATASET,
        client=client,
        batch=batch or _batch(),
        dry_run=False,
    )


def test_schema_matches_the_approved_ledger_fields_exactly() -> None:
    assert SCHEMA_PATH.is_file(), f"missing ledger schema: {SCHEMA_PATH}"
    rendered = (
        SCHEMA_PATH.read_text(encoding="utf-8")
        .replace("{project}", PROJECT)
        .replace("{dataset}", DATASET)
    )

    parsed = parse_table_ddl(rendered)

    assert (
        tuple((name, field_type, mode) for name, field_type, mode, _, _ in parsed["fields"])
        == EXPECTED_SCHEMA
    )
    assert parsed["partition"] == "trend_date"
    assert parsed["cluster"] == ("credential_lane", "event_type", "execution_id", "phase")
    assert parsed["expiration"] is None
    assert "immutable" in parsed["description"].lower()
    assert "staging" in parsed["description"].lower()


def test_public_constants_pin_the_staging_table_writer_and_natural_key() -> None:
    module = _module()

    assert module.TARGET_PROJECT == PROJECT
    assert module.TARGET_DATASET == DATASET
    assert module.TARGET_LOCATION == LOCATION
    assert module.wave1_pilot_service_identity() == WRITER_IDENTITY
    assert module.TABLE_NAME == TABLE
    assert module.ROW_FIELDS == ROW_FIELDS
    assert module.NATURAL_KEY == NATURAL_KEY


def test_batch_deep_copies_rows_and_preserves_decimal_precision() -> None:
    source = _row()
    batch = _batch(source)
    source["calls"] = 99

    assert batch.rows[0]["calls"] == 1
    assert batch.rows[0]["budget_debit_credits"] == Decimal("2")
    with pytest.raises(TypeError):
        batch.rows[0]["calls"] = 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credential_lane", "unknown"),
        ("phase", "other"),
        ("event_type", "other"),
        ("attribution_state", "other"),
        ("calls", True),
        ("calls", -1),
        ("budget_debit_credits", 1.0),
        ("vendor_reported_credits", Decimal("-1")),
        ("recorded_at", datetime(2026, 8, 27, 10, 0)),
        ("trend_date", NOW),
        ("catalog_digest", "bad"),
        ("ledger_id", ""),
    ],
)
def test_batch_rejects_malformed_or_out_of_contract_values(field: str, value: object) -> None:
    module = _module()

    with pytest.raises(module.LedgerBatchInvalid):
        _batch(_row(**{field: value}))


def test_batch_rejects_duplicate_natural_keys_even_when_content_changes() -> None:
    module = _module()
    changed = _row(ledger_id="ledger_002", budget_debit_credits=Decimal("3"))

    with pytest.raises(module.LedgerBatchInvalid, match="duplicate natural key"):
        _batch(_row(), changed)


def test_batch_rejects_run_close_that_does_not_reconcile_twenty_one_phases() -> None:
    phase_rows = []
    for market in ("za", "ng", "ke"):
        for index, phase in enumerate(
            ("discover", "creators", "search", "reddit", "accounts", "news", "facebook")
        ):
            spend = Decimal("2") if (market, phase) == ("za", "search") else Decimal("0")
            reported = Decimal("1") if spend else Decimal("0")
            phase_rows.append(
                _row(
                    ledger_id=f"ledger_{market}_{index}",
                    market=market,
                    phase=phase,
                    event_type="phase_close",
                    calls=1 if spend else 0,
                    budget_debit_credits=spend,
                    vendor_reported_credits=reported,
                )
            )
    run_close = _row(
        ledger_id="ledger_run_close",
        market=None,
        phase="run_close",
        event_type="run_close",
        calls=999,
        budget_debit_credits=Decimal("999"),
        vendor_reported_credits=Decimal("999"),
    )

    with pytest.raises(_module().LedgerBatchInvalid, match="run close totals"):
        _batch(*phase_rows, run_close)


def test_global_market_is_a_valid_nullable_natural_key_member() -> None:
    batch = _batch(_row(market=None, phase="run_close", event_type="run_close"))

    assert batch.rows[0]["market"] is None


def test_dry_run_is_local_only_and_requires_the_exact_target_descriptor() -> None:
    module = _module()
    target = SimpleNamespace(
        project=PROJECT,
        dataset=DATASET,
        location=LOCATION,
        writer_identity=WRITER_IDENTITY,
    )

    result = module.persist_funded_lane_rows(
        project=PROJECT,
        dataset=DATASET,
        client=target,
        batch=_batch(),
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.validated_count == 1
    assert result.inserted_count == 0
    assert result.unchanged_count == 0
    assert result.conflict_count == 0
    assert result.cleanup_state == "not_started"
    assert len(result.statement_digest) == 64


@pytest.mark.parametrize(
    ("project", "dataset", "location", "writer"),
    [
        ("other", DATASET, LOCATION, WRITER_IDENTITY),
        (PROJECT, "trends_v2", LOCATION, WRITER_IDENTITY),
        (PROJECT, DATASET, "EU", WRITER_IDENTITY),
        (PROJECT, DATASET, LOCATION, "default"),
    ],
)
def test_nonexact_target_refuses_before_any_bigquery_write(
    project: str, dataset: str, location: str, writer: str
) -> None:
    module = _module()
    client = _FakeClient(project=project, location=location, writer_identity=writer)

    with pytest.raises(module.TargetInvalid):
        module.persist_funded_lane_rows(
            project=project,
            dataset=dataset,
            client=client,
            batch=_batch(),
            dry_run=False,
        )

    assert client.created == []
    assert client.loaded == []
    assert client.queries == []


def test_real_write_inserts_once_with_exact_schema_and_cleans_temp() -> None:
    client = _FakeClient()

    result = _write(client)

    assert result.inserted_count == 1
    assert result.unchanged_count == 0
    assert result.conflict_count == 0
    assert result.cleanup_state == "complete"
    assert len(client.created) == 1
    temporary = client.created[0]
    assert temporary.project == PROJECT
    assert temporary.dataset_id == DATASET
    assert temporary.table_id.startswith(f"_sccl_{result.statement_digest[:12]}_")
    assert temporary.expires is not None
    assert tuple(field.name for field in temporary.schema) == ROW_FIELDS
    assert client.deleted == [temporary]
    loaded = client.loaded[0][0][0]
    assert loaded["budget_debit_credits"] == "2"
    assert loaded["recorded_at"] == "2026-08-27T10:00:00Z"


def test_real_bigquery_client_shape_does_not_require_a_dataset_attribute() -> None:
    client = _FakeClient()
    del client.dataset

    result = _write(client)

    assert result.inserted_count == 1


def test_identical_rerun_is_unchanged_and_adds_no_second_row() -> None:
    client = _FakeClient()

    first = _write(client)
    second = _write(client)

    assert first.inserted_count == 1
    assert second.inserted_count == 0
    assert second.unchanged_count == 1
    assert len(client.target_rows) == 1
    assert len(client.deleted) == 2


def test_changed_content_at_the_same_natural_key_raises_and_cleans() -> None:
    module = _module()
    client = _FakeClient()
    _write(client)
    changed = _batch(_row(budget_debit_credits=Decimal("3")))

    with pytest.raises(module.ImmutableConflict) as raised:
        _write(client, changed)

    assert raised.value.result.conflict_count == 1
    assert raised.value.result.inserted_count == 0
    assert raised.value.result.cleanup_state == "complete"
    assert len(client.target_rows) == 1
    assert client.deleted == client.created


def test_transaction_is_insert_only_and_compares_every_field() -> None:
    client = _FakeClient()

    _write(client, _batch(_row(market=None, phase="run_close", event_type="run_close")))

    sql = client.queries[0]
    assert f"INSERT INTO `{PROJECT}.{DATASET}.{TABLE}`" in sql
    assert "MERGE " not in sql
    assert "UPDATE " not in sql
    assert f"DELETE FROM `{PROJECT}.{DATASET}.{TABLE}`" not in sql
    for field in NATURAL_KEY:
        operator = "IS NOT DISTINCT FROM" if field == "market" else "="
        assert f"target.`{field}` {operator} staged.`{field}`" in sql
    for field in ROW_FIELDS:
        assert f"target.`{field}` AS `{field}`" in sql
        assert f"staged.`{field}` AS `{field}`" in sql
    assert "ASSERT conflict_count = 0" in sql
    assert "CASE WHEN COALESCE(wave1_phase_close_count, 0) > 0" in sql
    assert "COALESCE(phase_close_count, 0) != 4" in sql
    assert "ELSE COALESCE(phase_close_count, 0) != 21 END" in sql
    assert "run close totals" in sql


def test_uncertain_create_still_attempts_temp_cleanup() -> None:
    module = _module()
    client = _UncertainCreateClient()

    with pytest.raises(module.PersistenceError) as raised:
        _write(client)

    assert raised.value.__cause__ is not None
    assert raised.value.result.cleanup_state == "complete"
    assert client.deleted == client.created


def test_cleanup_failure_is_reported_after_an_otherwise_complete_write() -> None:
    module = _module()
    client = _FakeClient(delete_error=RuntimeError("cleanup denied"))

    with pytest.raises(module.CleanupFailure) as raised:
        _write(client)

    assert raised.value.result.inserted_count == 1
    assert raised.value.result.cleanup_state == "failed"
    assert client.deleted == client.created


def test_persistence_runtime_never_opens_a_network_socket(monkeypatch) -> None:
    client = _FakeClient()

    def forbid_socket(*args, **kwargs):
        raise AssertionError("persistence cannot call SocialCrawl or any network service")

    monkeypatch.setattr(socket, "socket", forbid_socket)

    result = _write(client)

    assert result.inserted_count == 1
