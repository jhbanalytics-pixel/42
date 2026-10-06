"""Source Lab 2.1 deterministic row and staging persistence boundaries."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from google.cloud import bigquery
from src.analysis.open_intelligence import source_lab
from src.analysis.open_intelligence import source_lab_persistence as persistence
from src.contracts.open_intelligence import resolve_client_scope

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
WRITER = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
CHECKED_AT = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
FIXTURES = {
    "socialcrawl_catalog_2026_08_26": Path(__file__).resolve().parents[1]
    / "fixtures"
    / "socialcrawl_catalog_2026_08_26.json",
    "socialcrawl_catalog_2026_08_27": Path(__file__).resolve().parents[1]
    / "fixtures"
    / "socialcrawl_catalog_2026_08_27.json",
    "socialcrawl_catalog_2026_09_02": Path(__file__).resolve().parents[1]
    / "fixtures"
    / "socialcrawl_catalog_2026_09_02.json",
    "socialcrawl_catalog_2026_09_03": Path(__file__).resolve().parents[1]
    / "fixtures"
    / "socialcrawl_catalog_2026_09_03.json",
    "socialcrawl_catalog_2026_09_04": Path(__file__).resolve().parents[1]
    / "fixtures"
    / "socialcrawl_catalog_2026_09_04.json",
    "socialcrawl_catalog_2026_09_05": Path(__file__).resolve().parents[1]
    / "fixtures"
    / "socialcrawl_catalog_2026_09_05.json",
}
INVENTORY_ID = "srcperf_2bb022db8c42183178ec4a2489505ca219d30d61e9779c39afb0de25035e6582"
BALANCE_ID = "srcperf_7729e4b948eecf87d28e0a2a647dc54b57aceb78fa2d55a81453190119885ff3"


def _snapshots(expectation_version="socialcrawl_catalog_2026_08_27"):
    raw = json.loads(FIXTURES[expectation_version].read_text(encoding="utf-8"))
    catalog = source_lab.normalize_catalog(
        raw,
        checked_at=CHECKED_AT,
        expected_platforms=raw["data"]["stats"]["platforms"],
        expected_endpoints=raw["data"]["stats"]["endpoints"],
    )
    balance = source_lab.normalize_balance(
        {
            "success": True,
            "endpoint": "/v1/credits/balance",
            "data": {"balance": 6879, "recent_deductions": 428},
            "credits_used": 0,
            "credits_remaining": 6879,
            "request_id": "balance-request",
        },
        observed_at=CHECKED_AT,
    )
    return catalog, balance


def _rows(expectation_version="socialcrawl_catalog_2026_08_27"):
    catalog, balance = _snapshots(expectation_version)
    return source_lab.build_source_performance_rows(
        scope=resolve_client_scope(run_id="source_lab_run_001"),
        catalog=catalog,
        balance=balance,
        expectation_version=expectation_version,
    )


class _Credentials:
    service_account_email = WRITER
    quota_project_id = PROJECT


class _LoadJob:
    errors = None

    def __init__(self, count: int) -> None:
        self.output_rows = count

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
        receipt: dict[str, int] | None = None,
        writer: str = WRITER,
        delete_error: Exception | None = None,
    ) -> None:
        self.project = PROJECT
        self.location = LOCATION
        self._credentials = _Credentials()
        self._credentials.service_account_email = writer
        self.receipt = receipt or {
            "inserted_count": 401,
            "unchanged_count": 0,
            "conflict_count": 0,
            "status": 0,
        }
        self.delete_error = delete_error
        self.created: list[bigquery.Table] = []
        self.loaded: list[list[dict[str, object]]] = []
        self.queries: list[str] = []
        self.deleted: list[bigquery.Table] = []

    def create_table(self, table: bigquery.Table, *, exists_ok: bool = False):
        assert exists_ok is False
        self.created.append(table)
        return table

    def load_table_from_json(self, rows, destination, *, location, job_config):
        assert location == LOCATION
        assert job_config.write_disposition == bigquery.WriteDisposition.WRITE_TRUNCATE
        self.loaded.append(rows)
        return _LoadJob(len(rows))

    def query(self, sql, *, location, job_config, retry, job_retry):
        assert location == LOCATION
        assert job_config.use_legacy_sql is False
        self.queries.append(sql)
        return _QueryJob(self.receipt)

    def delete_table(self, table, *, not_found_ok):
        assert not_found_ok is True
        self.deleted.append(table)
        if self.delete_error:
            raise self.delete_error


def _batch(rows=None):
    return persistence.SourceLabRowBatch(rows=rows or _rows())


def _write(client, rows=None, *, project=PROJECT, dataset=DATASET):
    return persistence.persist_source_lab_rows(
        project=project,
        dataset=dataset,
        client=client,
        batch=_batch(rows),
        dry_run=False,
    )


def test_builder_emits_exactly_four_hundred_inventory_rows_and_one_balance_row():
    rows = _rows()

    assert len(rows) == 401
    assert sum(row["route_role"] == "inventory" for row in rows) == 400
    assert sum(row["route_role"] == "utility_balance" for row in rows) == 1
    assert len({row["source_performance_id"] for row in rows}) == 401


@pytest.mark.parametrize("expectation_version", tuple(FIXTURES))
def test_batch_accepts_each_approved_route_and_metadata_pair(expectation_version):
    batch = persistence.SourceLabRowBatch(rows=_rows(expectation_version))

    assert len(batch.rows) == source_lab.CATALOG_EXPECTATIONS[expectation_version][1] + 1


def test_batch_accepts_only_the_pinned_normalized_subset_when_outside_rows_drop(monkeypatch):
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    path = Path(__file__).resolve().parents[1] / "fixtures" / "source_lab" / "wave1_metered_catalog_rows.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    catalog = source_lab.normalize_catalog(
        raw,
        checked_at=datetime(2026, 9, 27, 13, 10, tzinfo=UTC),
        expected_platforms=4,
        expected_endpoints=9,
        wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
    )
    balance = source_lab.normalize_balance(
        {
            "success": True,
            "endpoint": "/v1/credits/balance",
            "data": {"balance": 6879, "recent_deductions": 0},
            "credits_used": 0,
            "credits_remaining": 6879,
            "request_id": "balance-request",
        },
        observed_at=catalog.checked_at,
    )
    rows = source_lab.build_source_performance_rows(
        scope=resolve_client_scope(run_id="source_lab_2026-09-27"),
        catalog=catalog,
        balance=balance,
        expectation_version="socialcrawl_catalog_2026_09_05",
        wave1_identity=(
            tuple(WAVE1_ROUTE_SPECS),
            (
                "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6",
                "501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a",
            ),
        ),
    )
    assert (catalog.route_count, len(catalog.endpoints), len(catalog.dropped_rows)) == (9, 8, 1)
    monkeypatch.setattr(
        persistence,
        "CATALOG_EXPECTATIONS",
        MappingProxyType({
            "bounded_drop_fixture": (
                catalog.platform_count,
                catalog.route_count,
                catalog.social_platform_count,
                catalog.catalog_digest,
                catalog.normalized_content_digest,
            )
        }),
    )
    monkeypatch.setattr(
        persistence,
        "CATALOG_NORMALIZED_ROUTE_COUNTS",
        MappingProxyType({"bounded_drop_fixture": 8}),
    )

    assert len(persistence.SourceLabRowBatch(rows=rows).rows) == 9
    missing = tuple(row for row in rows if row["route_path"] != "/v1/instagram/search/reels")
    with pytest.raises(persistence.SourceLabBatchInvalid, match="inventory rows"):
        persistence.SourceLabRowBatch(rows=missing)
    changed = tuple(
        {**row, "official_capability": "changed catalog content"}
        if row["route_path"] == "/v1/instagram/search/reels"
        else row
        for row in rows
    )
    with pytest.raises(persistence.SourceLabBatchInvalid, match="metadata digest"):
        persistence.SourceLabRowBatch(rows=changed)


def test_batch_rejects_route_and_metadata_digests_from_different_expectations(monkeypatch):
    route_digest = "f843d6ad934e36b005090c1c642eb4b8e920bf2ca5d8828d7b9c1e7e4f143daa"
    metadata_digest = "47290bc1da76f4c29226a70c22f25b239557bbe56ce27d9931a29d174ffdd7ca"
    monkeypatch.setattr(
        persistence,
        "CATALOG_EXPECTATIONS",
        MappingProxyType(
            {
                "route_only": (50, 400, 28, route_digest, "0" * 64),
                "metadata_only": (50, 400, 28, "f" * 64, metadata_digest),
            }
        ),
    )

    with pytest.raises(persistence.SourceLabBatchInvalid, match="metadata digest"):
        persistence.SourceLabRowBatch(rows=_rows())


def test_builder_uses_fixed_length_prefixed_natural_ids_and_exact_row_order():
    rows = _rows()
    inventory = next(row for row in rows if row["route_path"] == "/v1/tiktok/profile")
    balance = next(row for row in rows if row["route_path"] == "/v1/credits/balance")

    assert tuple(inventory) == source_lab.SOURCE_PERFORMANCE_FIELDS
    assert inventory["source_performance_id"] == INVENTORY_ID
    assert balance["source_performance_id"] == BALANCE_ID
    assert tuple(row["source_performance_id"] for row in rows) == tuple(
        sorted(row["source_performance_id"] for row in rows)
    )


def test_inventory_rows_keep_native_catalog_facts_and_all_unmeasured_values_empty():
    inventory = next(row for row in _rows() if row["route_path"] == "/v1/tiktok/profile")

    assert inventory["status"] == "inventory_only"
    assert inventory["market"] is None
    assert inventory["official_credits"] == Decimal("1")
    assert inventory["official_credits_label"] == "1 (standard)"
    assert inventory["platform"] == "tiktok"
    assert inventory["resource"] == "profile"
    for field in (
        "calls",
        "credits",
        "rows",
        "integrity",
        "geo_precision",
        "unique_lift",
        "last_success_at",
        "balance",
        "observed_at",
        "recent_deductions",
        "monthly_optional_credits_used",
    ):
        assert inventory[field] is None
    assert inventory["downstream_consumers"] == ()
    assert inventory["official_price_components"] == ()


def test_balance_control_row_fails_closed_without_becoming_catalog_inventory():
    balance = next(row for row in _rows() if row["route_role"] == "utility_balance")

    assert balance["balance"] == Decimal("6879")
    assert balance["recent_deductions"] == Decimal("428")
    assert balance["funding_math_status"] == "unknown"
    assert balance["funded_increase_observed"] is False
    assert balance["optional_calls_enabled"] is False
    assert balance["monthly_optional_credit_cap"] == Decimal("25000")
    assert balance["platform"] is None
    assert balance["official_credits"] is None


@pytest.mark.parametrize(
    "mutator",
    [
        lambda row: {**row, "status": "active"},
        lambda row: {**row, "calls": 0},
        lambda row: {**row, "optional_calls_enabled": True},
        lambda row: {**row, "official_credits": Decimal("0.01"), "official_credits_label": "$0.01"},
    ],
)
def test_batch_rejects_inventory_semantic_mutations(mutator):
    rows = list(_rows())
    index = next(i for i, row in enumerate(rows) if row["route_role"] == "inventory")
    rows[index] = mutator(dict(rows[index]))

    with pytest.raises(persistence.SourceLabBatchInvalid):
        _batch(tuple(rows))


def test_batch_rejects_a_missing_route_and_duplicate_natural_key():
    rows = _rows()
    with pytest.raises(persistence.SourceLabBatchInvalid, match=r"inventory rows|one balance row"):
        _batch(rows[:-1])
    with pytest.raises(persistence.SourceLabBatchInvalid, match="duplicate natural key"):
        _batch((*rows[:-1], rows[0]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_scope_id", "other_scope"),
        ("market_scope", ("za",)),
        ("brand_config_id", "other_brand"),
        ("audience_lens_ids", ("gen_z",)),
        ("theme_id", "other_theme"),
        ("run_id", "other_run"),
        ("contract_version", "2.0.0"),
        ("metric_date", CHECKED_AT.date().replace(day=25)),
        ("catalog_digest", "0" * 64),
    ],
)
def test_batch_rejects_mixed_snapshot_identity(field, value):
    rows = list(_rows())
    index = next(i for i, row in enumerate(rows) if row["route_role"] == "inventory")
    rows[index] = {**rows[index], field: value}
    if field in {"client_scope_id", "metric_date"}:
        rows[index]["source_performance_id"] = source_lab._source_performance_id(
            rows[index]["client_scope_id"],
            rows[index]["metric_date"],
            rows[index]["vendor"],
            rows[index]["http_method"],
            rows[index]["route_path"],
            rows[index]["market"],
        )

    with pytest.raises(persistence.SourceLabBatchInvalid, match="snapshot"):
        _batch(tuple(rows))


def test_batch_rejects_altered_catalog_credit_through_metadata_digest():
    rows = list(_rows())
    index = next(i for i, row in enumerate(rows) if row["route_role"] == "inventory")
    rows[index] = {
        **rows[index],
        "official_credits": Decimal("0.01"),
        "official_credits_label": "0.01 USD",
    }

    with pytest.raises(persistence.SourceLabBatchInvalid, match="metadata digest"):
        _batch(tuple(rows))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("balance", Decimal("1")),
        ("observed_at", CHECKED_AT),
        ("balance_read_status", "ok"),
        ("recent_deductions", Decimal("1")),
        ("deductions_interval", {"start_at": CHECKED_AT, "end_at": CHECKED_AT}),
        ("funding_math_status", "unknown"),
        ("funded_increase_amount", Decimal("1")),
        ("funded_increase_observed", False),
        ("selected_top_up_credits", Decimal("1")),
        ("baseline_credits_per_day", Decimal("1")),
        (
            "baseline_window",
            {"start_date": CHECKED_AT.date(), "end_date": CHECKED_AT.date(), "completed_days": 1},
        ),
        ("runway_days", Decimal("1")),
        ("monthly_optional_credit_cap", Decimal("25000")),
        ("monthly_optional_credits_used", Decimal("1")),
        ("optional_calls_enabled", False),
    ],
)
def test_inventory_rejects_every_balance_only_field(field, value):
    rows = list(_rows())
    index = next(i for i, row in enumerate(rows) if row["route_role"] == "inventory")
    rows[index] = {**rows[index], field: value}

    with pytest.raises(persistence.SourceLabBatchInvalid, match="funding"):
        _batch(tuple(rows))


def test_dry_run_and_write_refuse_nonstaging_target_or_wrong_writer():
    target = SimpleNamespace(
        project=PROJECT, dataset=DATASET, location=LOCATION, writer_identity=WRITER
    )
    result = persistence.persist_source_lab_rows(
        project=PROJECT,
        dataset=DATASET,
        client=target,
        batch=_batch(),
        dry_run=True,
    )
    assert result.validated_count == 401
    assert result.cleanup_state == "not_started"

    with pytest.raises(persistence.TargetInvalid):
        _write(_FakeClient(), dataset="trends_v2_dev")
    with pytest.raises(persistence.TargetInvalid):
        _write(_FakeClient(writer="default"))


@pytest.mark.parametrize(
    "receipt",
    [
        {"inserted_count": 401, "unchanged_count": 0, "conflict_count": 0, "status": 0},
        {"inserted_count": 0, "unchanged_count": 401, "conflict_count": 0, "status": 0},
    ],
)
def test_write_accepts_complete_nonconflicting_transaction_receipt(receipt):
    client = _FakeClient(receipt=receipt)

    result = _write(client)

    assert (result.inserted_count, result.unchanged_count, result.conflict_count) == (
        receipt["inserted_count"],
        receipt["unchanged_count"],
        receipt["conflict_count"],
    )
    assert client.deleted == client.created
    assert client.created[0].expires is not None
    assert (
        tuple(field.name for field in client.created[0].schema)
        == source_lab.SOURCE_PERFORMANCE_FIELDS
    )
    sql = client.queries[0]
    assert f"INSERT INTO `{PROJECT}.{DATASET}.source_performance_daily_v2`" in sql
    assert "MERGE " not in sql
    assert "UPDATE " not in sql
    assert "DELETE FROM" not in sql
    assert "target.`source_performance_id` = staged.`source_performance_id`" in sql
    for field in source_lab.SOURCE_PERFORMANCE_FIELDS:
        assert f"target.`{field}` AS `{field}`" in sql
        assert f"staged.`{field}` AS `{field}`" in sql


@pytest.mark.parametrize(
    "receipt",
    [
        {"inserted_count": 400, "unchanged_count": 0, "conflict_count": 0, "status": 0},
        {"inserted_count": 401, "unchanged_count": 0, "conflict_count": 1, "status": 0},
        {"inserted_count": 1, "unchanged_count": 0, "conflict_count": 1, "status": 1},
        {"inserted_count": 0, "unchanged_count": 1, "conflict_count": 1, "status": 1},
        {"inserted_count": 0, "unchanged_count": 0, "conflict_count": 0, "status": 1},
        {"inserted_count": 0, "unchanged_count": 401, "conflict_count": 0, "status": 2},
    ],
)
def test_write_rejects_incomplete_or_invalid_transaction_receipt(receipt):
    with pytest.raises(persistence.PersistenceError, match="receipt"):
        _write(_FakeClient(receipt=receipt))


def test_write_raises_immutable_conflict_only_for_conflict_receipt():
    client = _FakeClient(
        receipt={"inserted_count": 0, "unchanged_count": 0, "conflict_count": 401, "status": 1}
    )

    with pytest.raises(persistence.ImmutableConflict) as raised:
        _write(client)

    assert raised.value.result.conflict_count == 401
    assert client.deleted == client.created


def test_cleanup_failure_is_reported_and_runtime_never_opens_a_socket(monkeypatch):
    def forbid_socket(*args, **kwargs):
        raise AssertionError("Source Lab persistence cannot open a network socket")

    monkeypatch.setattr(socket, "socket", forbid_socket)
    with pytest.raises(persistence.CleanupFailure) as raised:
        _write(_FakeClient(delete_error=RuntimeError("cleanup denied")))

    assert raised.value.result.inserted_count == 401
    assert raised.value.result.cleanup_state == "failed"


@pytest.mark.parametrize(
    "fragment",
    [
        "ASSERT conflict_count = 0 AS 'immutable_conflict';",
        "SET unchanged_count = (",
        "WHERE NOT EXISTS (",
    ],
)
def test_rendered_transaction_sql_rejects_missing_guard(monkeypatch, fragment):
    original = persistence._source_lab_transaction_sql

    def missing_guard(temporary):
        sql = original(temporary)
        mutated = sql.replace(fragment, "", 1)
        assert mutated != sql
        return mutated

    monkeypatch.setattr(persistence, "_source_lab_transaction_sql", missing_guard)

    with pytest.raises(persistence.PersistenceError, match="transaction SQL"):
        _write(_FakeClient())


def test_batch_size_follows_the_approved_catalog_route_count() -> None:
    # The 2 September catalog has 449 routes; the batch is those rows plus one balance row.
    rows = _rows("socialcrawl_catalog_2026_09_02")
    batch = persistence.SourceLabRowBatch(rows=rows)
    assert len(batch.rows) == 450
    assert sum(row["route_role"] == "inventory" for row in batch.rows) == 449
    inventory = [row for row in rows if row["route_role"] == "inventory"]
    balance = [row for row in rows if row["route_role"] == "utility_balance"]
    with pytest.raises(persistence.SourceLabBatchInvalid, match="inventory rows"):
        persistence.SourceLabRowBatch(rows=tuple(inventory[:-1] + balance))
    with pytest.raises(persistence.SourceLabBatchInvalid, match="one balance row"):
        persistence.SourceLabRowBatch(rows=tuple(inventory))
