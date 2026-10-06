from __future__ import annotations

import hashlib
import json
from dataclasses import fields, replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

_EVENTS_SQL_DIGEST = "0358d093c32559862016a793ae487d4d6bdc83f60df63e4e4066d495d116bb8d"
_GCAM_SQL_DIGEST = "fde6d16a8e1c105866a2a5b287a82675dbca3bf2dd44bbd89b9ec601c0dc71c4"
_MANIFEST_CEILING = 50_000_000_000
_WAVE1_IDENTITY = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
_DRY_RUN_PRINCIPAL = "jhb.analytics@gmail.com"
_EVENTS_RECEIPT_ID = "gdry_14f758562927146926586d8be94b4ee4c13791ef459606fdc9cb8eb6404fb553"
_GCAM_RECEIPT_ID = "gdry_1b928c3273d9b89c0845eb4cd963b562256988768f6144993070712009d61a1c"


def _receipt_payload(
    name: str = "events",
    sql_digest: str = _EVENTS_SQL_DIGEST,
    total_bytes_processed: int = 478_242_050,
    principal_email: str = _DRY_RUN_PRINCIPAL,
) -> dict[str, object]:
    is_events = name == "events"
    return {
        "name": name,
        "sql_digest": sql_digest,
        "start_date": "2026-08-14",
        "end_date": "2026-08-27",
        "total_bytes_processed": total_bytes_processed,
        "created_at": "2026-08-31T10:00:00.000000Z" if is_events else "2026-08-31T10:01:00.000000Z",
        "ended_at": "2026-08-31T10:00:01.000000Z" if is_events else "2026-08-31T10:01:01.000000Z",
        "principal_email": principal_email,
        "etag": "etag-events" if is_events else "etag-gcam",
        "project": "ogilvy-trends-v2",
        "location": "US",
    }


def _receipt(
    name: str = "events",
    sql_digest: str = _EVENTS_SQL_DIGEST,
    total_bytes_processed: int = 478_242_050,
    principal_email: str = _DRY_RUN_PRINCIPAL,
):
    from src.ingestion.connectors.gdelt import GDELTDryRunReceipt

    payload = _receipt_payload(name, sql_digest, total_bytes_processed, principal_email)
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return GDELTDryRunReceipt(
        dry_run_receipt_id="gdry_" + digest,
        name=str(payload["name"]),
        sql_digest=str(payload["sql_digest"]),
        start_date=date.fromisoformat(str(payload["start_date"])),
        end_date=date.fromisoformat(str(payload["end_date"])),
        total_bytes_processed=int(payload["total_bytes_processed"]),
        created_at=datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00")),
        ended_at=datetime.fromisoformat(str(payload["ended_at"]).replace("Z", "+00:00")),
        principal_email=str(payload["principal_email"]),
        etag=str(payload["etag"]),
        project=str(payload["project"]),
        location=str(payload["location"]),
    )


def _receipt_mapping(receipt) -> dict[str, object]:
    return {
        "dry_run_receipt_id": receipt.dry_run_receipt_id,
        **_receipt_payload(receipt.name, receipt.sql_digest, receipt.total_bytes_processed),
    }


def _result_client(query):
    return SimpleNamespace(
        project="ogilvy-trends-v2",
        location="US",
        _credentials=SimpleNamespace(
            service_account_email=_WAVE1_IDENTITY,
            quota_project_id="ogilvy-trends-v2",
        ),
        query=query,
    )


def _approval_bound_entry(
    monkeypatch,
    *,
    name: str = "events",
    events_digest: str = _EVENTS_SQL_DIGEST,
):
    from src.analysis.open_intelligence import funded_lane
    from src.ingestion.connectors.gdelt import bind_wave1_gdelt_manifest_entry

    manifest_sha256 = "d" * 64
    events_receipt = _receipt("events", events_digest, 478_242_050)
    gcam_receipt = _receipt("gcam", _GCAM_SQL_DIGEST, 37_284_235_082)
    artifact = json.dumps(
        {
            "contract_version": "wave1-gdelt-dry-run-set-v1",
            "receipts": [_receipt_mapping(events_receipt), _receipt_mapping(gcam_receipt)],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    artifact_sha256 = hashlib.sha256(artifact).hexdigest()
    capability = SimpleNamespace(
        manifest_sha256=manifest_sha256,
        gdelt_dry_run_set_sha256=artifact_sha256,
        maximum_bytes_billed=_MANIFEST_CEILING,
    )

    def require(value, *, action):
        if value is not capability:
            raise funded_lane.Wave1ExecutionBlocked(f"{action}: capability is unavailable")
        return value

    monkeypatch.setattr(
        funded_lane,
        "_require_wave1_execution_capability",
        require,
        raising=False,
    )
    return (
        capability,
        bind_wave1_gdelt_manifest_entry(capability, artifact, name),
        events_receipt if name == "events" else gcam_receipt,
    )


def test_gdelt_wave1_plans_use_partitioned_tables_and_typed_grains() -> None:
    from src.ingestion.connectors.gdelt import (
        GDELT_EVENT_MARKET_SCHEMA,
        GDELT_EVENTS_SCHEMA,
        GDELT_GCAM_SCHEMA,
        build_wave1_gdelt_dry_run_plans,
    )

    plans = build_wave1_gdelt_dry_run_plans(date(2026, 8, 14), date(2026, 8, 27))
    assert tuple(plan.name for plan in plans) == ("events", "gcam")
    assert "`gdelt-bq.gdeltv2.events_partitioned`" in plans[0].sql
    assert "`gdelt-bq.gdeltv2.gkg_partitioned`" in plans[1].sql
    assert "_PARTITIONDATE BETWEEN @start_date AND @end_date" in plans[0].sql
    assert "_PARTITIONDATE BETWEEN @start_date AND @end_date" in plans[1].sql
    assert "Actor1CountryCode IN ('SF', 'NI', 'KE')" in plans[0].sql
    assert "Actor2CountryCode IN ('SF', 'NI', 'KE')" in plans[0].sql
    assert "ActionGeo_CountryCode IN ('SF', 'NI', 'KE')" in plans[0].sql
    assert "CAST(ActionGeo_CountryCode AS STRING) AS action_geo_country_code" in plans[0].sql
    assert "REGEXP_CONTAINS(location, r'^[1-5]#[^#]*#(?:SF|NI|KE)#')" in plans[1].sql
    assert GDELT_EVENTS_SCHEMA[0] == ("GLOBALEVENTID", "INT64", "REQUIRED")
    assert GDELT_EVENT_MARKET_SCHEMA == (
        ("GLOBALEVENTID", "INT64", "REQUIRED"),
        ("market", "STRING", "REQUIRED"),
        ("evidence_role", "STRING", "REQUIRED"),
        ("receipt_id", "STRING", "REQUIRED"),
    )
    assert GDELT_GCAM_SCHEMA == (
        ("document_url", "STRING", "REQUIRED"),
        ("published_at", "TIMESTAMP", "REQUIRED"),
        ("v10_1", "FLOAT64", "NULLABLE"),
        ("v10_2", "FLOAT64", "NULLABLE"),
        ("v19_1", "FLOAT64", "NULLABLE"),
        ("v19_9", "FLOAT64", "NULLABLE"),
        ("v20_1", "FLOAT64", "NULLABLE"),
    )


def test_gdelt_dry_run_never_iterates_results_or_sets_write_fields() -> None:
    from src.ingestion.connectors.gdelt import execute_wave1_gdelt_dry_run

    class Job:
        job_id = None
        total_bytes_processed = 478_242_050
        created = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
        ended = datetime(2026, 8, 31, 10, 0, 1, tzinfo=UTC)
        user_email = _DRY_RUN_PRINCIPAL
        etag = "etag-events"
        project = "ogilvy-trends-v2"
        location = "US"
        state = "DONE"

        def result(self):
            raise AssertionError("dry-run results must never be iterated")

    class Client:
        def query(self, _sql, *, job_config, location):
            assert job_config.dry_run is True
            assert "maximumBytesBilled" not in job_config.to_api_repr()["query"]
            assert job_config.destination is None
            assert job_config.write_disposition is None
            assert location == "US"
            return Job()

    receipt = execute_wave1_gdelt_dry_run(Client(), "events")
    assert tuple(field.name for field in fields(receipt)) == (
        "dry_run_receipt_id",
        "name",
        "sql_digest",
        "start_date",
        "end_date",
        "total_bytes_processed",
        "created_at",
        "ended_at",
        "principal_email",
        "etag",
        "project",
        "location",
    )
    assert receipt.dry_run_receipt_id == _EVENTS_RECEIPT_ID
    assert not hasattr(receipt, "job_id")
    assert receipt.total_bytes_processed == 478_242_050


@pytest.mark.parametrize(
    "missing",
    ["created", "ended", "user_email", "etag", "project", "location"],
)
def test_gdelt_dry_run_refuses_missing_server_receipt_fields(missing) -> None:
    from src.ingestion.connectors.gdelt import execute_wave1_gdelt_dry_run

    values = {
        "job_id": "synthetic-dry-run-id",
        "total_bytes_processed": 478_242_050,
        "created": datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        "ended": datetime(2026, 8, 31, 10, 0, 1, tzinfo=UTC),
        "user_email": _DRY_RUN_PRINCIPAL,
        "etag": "etag-events",
        "project": "ogilvy-trends-v2",
        "location": "US",
        "state": "DONE",
    }
    del values[missing]
    job = SimpleNamespace(**values)
    client = SimpleNamespace(query=lambda *_args, **_kwargs: job)
    with pytest.raises(ValueError, match="dry-run receipt"):
        execute_wave1_gdelt_dry_run(client, "events")


def test_gdelt_dry_run_receipt_rejects_content_id_or_server_field_mutation() -> None:
    receipt = _receipt()
    assert receipt.dry_run_receipt_id == _EVENTS_RECEIPT_ID
    with pytest.raises(ValueError, match="receipt"):
        replace(receipt, dry_run_receipt_id="gdry_" + "0" * 64)
    with pytest.raises(ValueError, match="receipt"):
        replace(receipt, etag="")
    payload = _receipt_payload()
    payload["principal_email"] = "not-an-email"
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ValueError, match="receipt"):
        replace(
            receipt,
            dry_run_receipt_id="gdry_" + digest,
            principal_email="not-an-email",
        )


def test_gdelt_dry_run_receipt_rejects_a_well_formed_foreign_principal() -> None:
    with pytest.raises(ValueError, match="receipt"):
        _receipt(principal_email="other-principal@example.com")


def test_gdelt_dry_run_refuses_a_synthetic_server_job_id() -> None:
    from src.ingestion.connectors.gdelt import execute_wave1_gdelt_dry_run

    job = SimpleNamespace(
        job_id="synthetic-dry-run-id",
        total_bytes_processed=478_242_050,
        created=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        ended=datetime(2026, 8, 31, 10, 0, 1, tzinfo=UTC),
        user_email=_DRY_RUN_PRINCIPAL,
        etag="etag-events",
        project="ogilvy-trends-v2",
        location="US",
        state="DONE",
    )
    client = SimpleNamespace(query=lambda *_args, **_kwargs: job)
    with pytest.raises(ValueError, match="dry-run receipt"):
        execute_wave1_gdelt_dry_run(client, "events")


def test_gdelt_raw_manifest_entry_issuer_is_not_exposed() -> None:
    from src.ingestion.connectors import gdelt

    assert not hasattr(gdelt, "_issue_wave1_gdelt_manifest_entry")


@pytest.mark.parametrize(
    ("name", "table", "expected_ceiling"),
    [
        ("events", "events_partitioned", 573_890_460),
        ("gcam", "gkg_partitioned", 44_741_082_099),
    ],
)
def test_gdelt_result_query_uses_bound_digest_and_exact_derived_ceiling(
    monkeypatch, name, table, expected_ceiling
) -> None:
    from src.ingestion.connectors.gdelt import execute_wave1_gdelt_query

    capability, entry, receipt = _approval_bound_entry(monkeypatch, name=name)
    result_job = object()

    class Client:
        project = "ogilvy-trends-v2"
        location = "US"
        _credentials = SimpleNamespace(
            service_account_email=_WAVE1_IDENTITY,
            quota_project_id="ogilvy-trends-v2",
        )

        def query(self, sql, *, job_config, location):
            assert f"`gdelt-bq.gdeltv2.{table}`" in sql
            assert job_config.dry_run is False
            assert job_config.maximum_bytes_billed == expected_ceiling
            assert job_config.destination is None
            assert job_config.write_disposition is None
            assert location == "US"
            return result_job

    assert (
        execute_wave1_gdelt_query(Client(), entry, receipt, execution_capability=capability)
        is result_job
    )


def test_gdelt_result_query_refuses_a_foreign_target_before_query(monkeypatch) -> None:
    from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked
    from src.ingestion.connectors.gdelt import execute_wave1_gdelt_query

    capability, entry, receipt = _approval_bound_entry(monkeypatch)
    client = SimpleNamespace(
        project="production-project",
        location="US",
        _credentials=SimpleNamespace(
            service_account_email=_WAVE1_IDENTITY,
            quota_project_id="production-project",
        ),
        query=lambda *_args, **_kwargs: pytest.fail("query executed"),
    )
    with pytest.raises(Wave1ExecutionBlocked, match="target"):
        execute_wave1_gdelt_query(
            client,
            entry,
            receipt,
            execution_capability=capability,
        )


def test_gdelt_result_query_requires_the_durable_capability() -> None:
    from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked
    from src.ingestion.connectors.gdelt import execute_wave1_gdelt_query

    class Client:
        calls = 0

        def query(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("query must remain dark")

    client = Client()
    with pytest.raises(Wave1ExecutionBlocked):
        execute_wave1_gdelt_query(
            client,
            object(),
            _receipt(),
            execution_capability=None,
        )
    assert client.calls == 0


def test_gdelt_result_query_rejects_unapproved_sql_or_receipt(monkeypatch) -> None:
    from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked
    from src.ingestion.connectors import gdelt

    capability, entry, receipt = _approval_bound_entry(monkeypatch)
    client = _result_client(lambda *_args, **_kwargs: pytest.fail("query executed"))
    plan = gdelt._wave1_plan("events")
    monkeypatch.setattr(gdelt, "_wave1_plan", lambda _name: replace(plan, sql=plan.sql + "\n"))
    with pytest.raises(Wave1ExecutionBlocked, match="SQL digest"):
        gdelt.execute_wave1_gdelt_query(
            client,
            entry,
            receipt,
            execution_capability=capability,
        )

    monkeypatch.setattr(gdelt, "_wave1_plan", lambda _name: plan)
    capability, entry, receipt = _approval_bound_entry(monkeypatch)
    object.__setattr__(receipt, "total_bytes_processed", 478_242_051)
    with pytest.raises(Wave1ExecutionBlocked, match="dry-run receipt"):
        gdelt.execute_wave1_gdelt_query(
            client,
            entry,
            receipt,
            execution_capability=capability,
        )


def test_gdelt_result_query_has_no_caller_byte_ceiling(monkeypatch) -> None:
    from src.ingestion.connectors.gdelt import execute_wave1_gdelt_query

    capability, entry, receipt = _approval_bound_entry(monkeypatch)
    with pytest.raises(TypeError, match="maximum_bytes_billed"):
        execute_wave1_gdelt_query(
            object(),
            entry,
            receipt,
            execution_capability=capability,
            maximum_bytes_billed=1,
        )


def test_maximum_bytes_billed_uses_lower_ceiling() -> None:
    from src.ingestion.connectors.gdelt import wave1_maximum_bytes_billed

    assert wave1_maximum_bytes_billed(478_242_050, _MANIFEST_CEILING) == 573_890_460
    assert wave1_maximum_bytes_billed(37_284_235_082, _MANIFEST_CEILING) == 44_741_082_099
    with pytest.raises(ValueError):
        wave1_maximum_bytes_billed(0, _MANIFEST_CEILING)
