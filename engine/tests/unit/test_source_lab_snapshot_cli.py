"""Zero-credit Source Lab staging snapshot runner tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from google.auth.compute_engine import credentials as compute_credentials
from src.analysis.open_intelligence import source_lab
from src.analysis.open_intelligence import source_lab_persistence as persistence

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "staging" / "run_source_lab_snapshot.py"
FIXTURE = ROOT / "tests" / "fixtures" / "socialcrawl_catalog_2026_09_05.json"
LIVE_FUNDED_FIXTURE = (
    ROOT / "tests" / "fixtures" / "source_lab" / "wave1_metered_catalog_rows.json"
)
STAMP = datetime(2026, 8, 27, 6, 15, tzinfo=UTC)
LATER = datetime(2026, 8, 27, 23, 5, tzinfo=UTC)
PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
WRITER = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
SECRET = "secret-shaped-source-lab-value"
MISSING = object()


def _load_module():
    spec = importlib.util.spec_from_file_location("run_source_lab_snapshot", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _credentials(*, identity: str = WRITER, quota_project: str | None = None):
    return compute_credentials.Credentials(
        service_account_email=identity, quota_project_id=quota_project
    )


def _catalog_payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _balance_payload(*, credits_used: int = 0) -> dict[str, object]:
    return {
        "success": True,
        "endpoint": "/v1/credits/balance",
        "data": {"balance": 250_100, "recent_deductions": 0},
        "credits_used": credits_used,
        "credits_remaining": 250_100,
        "request_id": "balance-request",
    }


class _Response:
    def __init__(self, payload, *, status_code=200, json_error=None) -> None:
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error
        self.history = ()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(SECRET)

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class _Session:
    def __init__(self, catalog=None, balance=None, *, close_error=None) -> None:
        self.headers = {}
        self.catalog = catalog or _Response(_catalog_payload())
        self.balance = balance or _Response(_balance_payload())
        self.close_error = close_error
        self.calls = []

    def get(self, url, *, timeout, allow_redirects):
        self.calls.append((url, timeout, allow_redirects))
        if url.endswith("/v1/utility/endpoints"):
            return self.catalog
        if url.endswith("/v1/credits/balance"):
            return self.balance
        raise AssertionError(f"unexpected route: {url}")

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error


class _ExistingRowsJob:
    def __init__(self, rows, calls) -> None:
        self.rows = rows
        self.calls = calls

    def result(self, *, max_results):
        self.calls["timestamp_max_results"] = max_results
        return tuple(self.rows)


class _CarryJob:
    def __init__(self, rows, calls) -> None:
        self.rows = rows
        self.calls = calls

    def result(self, *, max_results, retry, job_retry):
        self.calls["carry_max_results"] = max_results
        return tuple(self.rows)


class _PoisonExistingRow:
    def __getitem__(self, key):
        raise AssertionError("the 558th row must be rejected before field access")


class _Client(SimpleNamespace):
    def __init__(self, *, credentials, existing_rows, calls, wave1_rows=()) -> None:
        super().__init__(project=PROJECT, location=LOCATION, _credentials=credentials)
        self.existing_rows = existing_rows
        self.wave1_rows = wave1_rows
        self.calls = calls

    def query(self, sql, *, location, job_config, retry, job_retry):
        if "v_socialcrawl_wave1_source_values_v1" in sql:
            self.calls["carry_query"] = {
                "sql": sql,
                "location": location,
                "job_config": job_config,
                "retry": retry,
                "job_retry": job_retry,
            }
            return _CarryJob(self.wave1_rows, self.calls)
        self.calls["timestamp_query"] = {
            "sql": sql,
            "location": location,
            "job_config": job_config,
            "retry": retry,
            "job_retry": job_retry,
        }
        return _ExistingRowsJob(self.existing_rows, self.calls)


def _unexpected(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"{name} must not be called")

    return fail


def _result(*, dry_run, inserted=0, unchanged=0, validated_count=557):
    return persistence.SourceLabPersistenceResult(
        project=PROJECT,
        dataset=DATASET,
        dry_run=dry_run,
        validated_count=validated_count,
        inserted_count=inserted,
        unchanged_count=unchanged,
        conflict_count=0,
        statement_digest="statement-digest",
        cleanup_state="not_started" if dry_run else "complete",
    )


def _run(
    module,
    *,
    apply=False,
    stamp=STAMP,
    session=None,
    existing_rows=(),
    persist_result=None,
    persist_error=None,
    wave1_rows=(),
):
    session = session or _Session()
    credentials = _credentials()
    calls = {}

    def client_factory(**kwargs):
        calls["client_factory"] = kwargs
        return _Client(
            credentials=credentials,
            existing_rows=existing_rows,
            calls=calls,
            wave1_rows=wave1_rows,
        )

    def persist(**kwargs):
        calls["persist"] = kwargs
        if persist_error is not None:
            raise persist_error
        return persist_result or _result(dry_run=not apply, inserted=557 if apply else 0)

    output = module.run_snapshot(
        target="staging",
        apply=apply,
        clock=lambda: stamp,
        credential_loader=lambda: (credentials, PROJECT),
        secret_reader=lambda name: SECRET,
        session_factory=lambda: session,
        client_factory=client_factory,
        persist=persist,
    )
    return output, session, calls, credentials


def _existing_timestamp_rows(batch):
    return [
        {
            "source_performance_id": row["source_performance_id"],
            "route_role": row["route_role"],
            "last_checked_at": row["last_checked_at"],
            "observed_at": row["observed_at"],
            "status": row["status"],
            "rows": row["rows"],
            "unique_lift": row["unique_lift"],
            "blocking_reason": row["blocking_reason"],
            "kill_test_result": row["kill_test_result"],
            "downstream_consumers": list(row["downstream_consumers"]),
        }
        for row in batch.rows
    ]


PILOT_EXECUTION = "trends-engine-open-intelligence-staging-rh8vq"
PILOT_CLOSED_AT = datetime(2026, 8, 26, 21, 28, 7, tzinfo=UTC)


def _wave1_carry_rows(*, catalog_digest=None, metadata_digest=None, execution_id=PILOT_EXECUTION):
    from src.analysis.open_intelligence.funded_lane import (
        APPROVED_CATALOG_DIGEST,
        HISTORICAL_METADATA_DIGEST,
    )

    base = {
        "execution_id": execution_id,
        "recorded_at": PILOT_CLOSED_AT,
        "catalog_digest": catalog_digest or APPROVED_CATALOG_DIGEST,
        "metadata_digest": metadata_digest or HISTORICAL_METADATA_DIGEST,
    }
    return [
        {
            **base,
            "route_path": "/v1/instagram/search/reels",
            "state": "passed",
            "reason": None,
            "unique_observations": 87,
            "marginal_candidates": 3,
            "marginal_evidence": 87,
        },
        {
            **base,
            "route_path": "/v1/youtube/video/comments",
            "state": "passed",
            "reason": None,
            "unique_observations": 335,
            "marginal_candidates": 4,
            "marginal_evidence": 335,
        },
    ]


def test_parser_accepts_only_the_two_exact_commands() -> None:
    parser = _load_module().build_parser()
    dry_run = parser.parse_args(["--target", "staging"])
    apply = parser.parse_args(["--target", "staging", "--apply"])
    assert (dry_run.target, dry_run.apply) == ("staging", False)
    assert (apply.target, apply.apply) == ("staging", True)


def test_parser_leaves_target_validation_to_the_bounded_runner_path() -> None:
    args = _load_module().build_parser().parse_args(["--target", "production"])

    assert args.target == "production"


def test_parser_still_rejects_unknown_arguments() -> None:
    with pytest.raises(SystemExit) as raised:
        _load_module().build_parser().parse_args(["--target", "staging", "--unknown"])

    assert raised.value.code == 2


@pytest.mark.parametrize(
    ("target",),
    [
        ("qa",),
        ("development",),
        ("production",),
        ("staging_qa",),
        ("STAGING",),
        ("staging ",),
        ("",),
    ],
)
def test_invalid_target_subprocess_emits_only_bounded_json(target) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--target", target],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr.splitlines() == [b'{"error":"target_invalid"}']


def test_default_secret_reader_uses_only_the_fixed_secret_manager_path(monkeypatch) -> None:
    module = _load_module()
    reader = getattr(module, "_default_secret_reader", None)
    assert reader is not None
    requests = []

    class Client:
        def access_secret_version(self, *, request):
            requests.append(request)
            return SimpleNamespace(payload=SimpleNamespace(data=SECRET.encode("utf-8")))

    monkeypatch.setattr(module.secretmanager, "SecretManagerServiceClient", Client)

    assert reader(module.SECRET_ID) == SECRET
    # Version 1 by name: the approved IAM condition admits that version only.
    assert module.SECRET_VERSION == "1"
    assert requests == [
        {"name": ("projects/ogilvy-trends-v2/secrets/SOCIALCRAWL_OGILVY_API_KEY/versions/1")}
    ]


def test_default_secret_reader_never_falls_back_to_the_environment(monkeypatch) -> None:
    module = _load_module()
    reader = getattr(module, "_default_secret_reader", None)
    assert reader is not None
    monkeypatch.setenv(module.SECRET_ID, "environment-secret-must-not-win")

    class Client:
        def access_secret_version(self, *, request):
            raise RuntimeError("secret manager unavailable")

    monkeypatch.setattr(module.secretmanager, "SecretManagerServiceClient", Client)

    with pytest.raises(RuntimeError, match="secret manager unavailable"):
        reader(module.SECRET_ID)


def test_target_rejection_precedes_every_external_factory() -> None:
    module = _load_module()
    with pytest.raises(module.SnapshotFailure) as raised:
        module.run_snapshot(
            target="production",
            apply=False,
            clock=_unexpected("clock"),
            credential_loader=_unexpected("credentials"),
            secret_reader=_unexpected("secret"),
            session_factory=_unexpected("session"),
            client_factory=_unexpected("BigQuery"),
            persist=_unexpected("persistence"),
        )
    assert (raised.value.code, str(raised.value)) == ("target_invalid", "target_invalid")


@pytest.mark.parametrize(
    ("credentials", "adc_project"),
    [
        (SimpleNamespace(), PROJECT),
        (_credentials(), "wrong-project"),
        (_credentials(identity="wrong@ogilvy-trends-v2.iam.gserviceaccount.com"), PROJECT),
        (_credentials(quota_project="wrong-project"), PROJECT),
    ],
)
def test_runtime_identity_fails_closed_before_secret_http_or_bigquery(
    credentials, adc_project
) -> None:
    module = _load_module()
    with pytest.raises(module.SnapshotFailure) as raised:
        module.run_snapshot(
            target="staging",
            apply=False,
            clock=lambda: STAMP,
            credential_loader=lambda: (credentials, adc_project),
            secret_reader=_unexpected("secret"),
            session_factory=_unexpected("session"),
            client_factory=_unexpected("BigQuery"),
            persist=_unexpected("persistence"),
        )
    assert (raised.value.code, str(raised.value)) == (
        "runtime_identity_invalid",
        "runtime_identity_invalid",
    )


def test_deferred_compute_identity_is_resolved_before_secret_access(monkeypatch) -> None:
    module = _load_module()
    credentials = _credentials(identity="default")
    refreshed = []

    def refresh(request) -> None:
        refreshed.append(request)
        credentials._service_account_email = WRITER

    monkeypatch.setattr(credentials, "refresh", refresh)
    monkeypatch.setattr(module, "Request", lambda: "metadata-request")
    with pytest.raises(module.SnapshotFailure) as raised:
        module.run_snapshot(
            target="staging",
            apply=False,
            clock=lambda: STAMP,
            credential_loader=lambda: (credentials, PROJECT),
            secret_reader=lambda name: (_ for _ in ()).throw(RuntimeError(SECRET)),
            session_factory=_unexpected("session"),
            client_factory=_unexpected("BigQuery"),
            persist=_unexpected("persistence"),
        )
    assert refreshed == ["metadata-request"]
    assert raised.value.code == "secret_unavailable"
    assert SECRET not in str(raised.value)


def test_dry_run_uses_only_the_two_free_get_routes_and_validates_the_full_batch() -> None:
    module = _load_module()
    output, session, calls, _credentials = _run(module)
    assert session.calls == [
        ("https://www.socialcrawl.dev/v1/utility/endpoints", 30, False),
        ("https://www.socialcrawl.dev/v1/credits/balance", 30, False),
    ]
    assert session.headers == {"x-api-key": SECRET}
    assert "client_factory" not in calls
    persisted = calls["persist"]
    assert (persisted["project"], persisted["dataset"], persisted["dry_run"]) == (
        PROJECT,
        DATASET,
        True,
    )
    assert isinstance(persisted["batch"], persistence.SourceLabRowBatch)
    assert len(persisted["batch"].rows) == 557
    assert {row["contract_version"] for row in persisted["batch"].rows} == {"2.1.0"}
    assert persisted["client"] == module.PersistenceTarget(
        project=PROJECT,
        dataset=DATASET,
        location=LOCATION,
        writer_identity=WRITER,
    )
    assert {
        (
            row["client_scope_id"],
            row["market_scope"],
            row["brand_config_id"],
            row["audience_lens_ids"],
            row["theme_id"],
            row["run_id"],
        )
        for row in persisted["batch"].rows
    } == {
        (
            "ogilvy_default",
            ("za", "ng", "ke"),
            "ogilvy_42",
            (),
            "ogilvy_intelligence",
            "source_lab_2026-08-27",
        )
    }
    assert output == {
        "mode": "dry-run",
        "target": "staging",
        "run_id": "source_lab_2026-08-27",
        "metric_date": "2026-08-27",
        "platforms_observed": 64,
        "endpoints_observed": 556,
        "endpoints_normalized": 556,
        "dropped_endpoint_rows": [],
        "catalog_digest": "2424a5760fb55b44b78971875b64a6c176fcd34d95de83b2ad158a3b866c0a34",
        "balance": 250_100,
        "credits_used": 0,
        "rows_planned": 557,
        "inserted": 0,
        "unchanged": 0,
        "conflicts": 0,
        "cleanup_state": "not_started",
        "wave1_values": {"state": "unread_in_dry_run"},
    }


def test_dry_run_reaches_real_persistence_with_the_exact_approved_target() -> None:
    module = _load_module()
    credentials = _credentials()

    output = module.run_snapshot(
        target="staging",
        apply=False,
        clock=lambda: STAMP,
        credential_loader=lambda: (credentials, PROJECT),
        secret_reader=lambda name: SECRET,
        session_factory=_Session,
        client_factory=_unexpected("BigQuery"),
        persist=persistence.persist_source_lab_rows,
    )

    assert output["mode"] == "dry-run"
    assert output["target"] == "staging"
    assert output["rows_planned"] == 557
    assert (output["inserted"], output["unchanged"], output["conflicts"]) == (0, 0, 0)
    assert output["cleanup_state"] == "not_started"


def test_receipt_validation_follows_the_injected_approved_batch_count() -> None:
    module = _load_module()
    receipt = _result(dry_run=True, validated_count=7)

    module._validate_receipt(receipt, dry_run=True, expected_count=7)


@pytest.mark.parametrize(("inserted", "unchanged"), [(557, 0), (0, 557)])
def test_apply_reports_inserted_or_immutable_unchanged_rows(inserted, unchanged) -> None:
    module = _load_module()
    receipt = _result(dry_run=False, inserted=inserted, unchanged=unchanged)
    output, _, calls, credentials = _run(module, apply=True, persist_result=receipt)
    assert calls["client_factory"] == {
        "project": PROJECT,
        "location": LOCATION,
        "credentials": credentials,
    }
    assert calls["persist"]["client"]._credentials is credentials
    assert calls["persist"]["dry_run"] is False
    assert output["mode"] == "apply"
    assert (output["inserted"], output["unchanged"], output["conflicts"]) == (
        inserted,
        unchanged,
        0,
    )
    assert output["cleanup_state"] == "complete"


def test_first_apply_reads_exact_scope_and_keeps_fresh_timestamps() -> None:
    module = _load_module()

    output, _, calls, _ = _run(module, apply=True, existing_rows=())

    batch = calls["persist"]["batch"]
    assert output["inserted"] == 557
    assert {row["last_checked_at"] for row in batch.rows} == {STAMP}
    assert {row["observed_at"] for row in batch.rows if row["route_role"] == "utility_balance"} == {
        STAMP
    }
    assert output["wave1_values"] == {"state": "none"}
    carry = calls["carry_query"]
    assert (
        "`ogilvy-trends-v2.trends_v2_staging.v_socialcrawl_wave1_source_values_v1`" in carry["sql"]
    )
    assert "close_verdict = 'succeeded'" in carry["sql"]
    assert (carry["location"], carry["retry"], carry["job_retry"]) == ("US", None, None)
    assert {
        parameter.name: (parameter.type_, parameter.value)
        for parameter in carry["job_config"].query_parameters
    } == {
        "observed_at": ("TIMESTAMP", STAMP),
        "window_start": ("TIMESTAMP", STAMP - timedelta(hours=48)),
    }
    assert calls["carry_max_results"] == 9
    query = calls["timestamp_query"]
    assert query["sql"] == (
        "SELECT `source_performance_id`, `route_role`, `last_checked_at`, `observed_at`, "
        "`status`, `rows`, `unique_lift`, `blocking_reason`, `kill_test_result`, "
        "`downstream_consumers`\n"
        "FROM `ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2`\n"
        "WHERE `client_scope_id` = @client_scope_id\n"
        "  AND `run_id` = @run_id\n"
        "  AND `contract_version` = @contract_version\n"
        "  AND `metric_date` = @metric_date\n"
        "LIMIT 558"
    )
    assert (query["location"], query["retry"], query["job_retry"]) == ("US", None, None)
    assert query["job_config"].use_legacy_sql is False
    assert {
        parameter.name: (parameter.type_, parameter.value)
        for parameter in query["job_config"].query_parameters
    } == {
        "client_scope_id": ("STRING", "ogilvy_default"),
        "run_id": ("STRING", "source_lab_2026-08-27"),
        "contract_version": ("STRING", "2.1.0"),
        "metric_date": ("DATE", STAMP.date()),
    }
    assert calls["timestamp_max_results"] == 558


def test_complete_same_day_rerun_reuses_only_first_observation_timestamps() -> None:
    module = _load_module()
    _, _, first_calls, _ = _run(module, apply=True, existing_rows=())
    first_batch = first_calls["persist"]["batch"]
    existing_rows = _existing_timestamp_rows(first_batch)
    for index, existing in enumerate(existing_rows):
        persisted_at = STAMP + timedelta(microseconds=index)
        existing["last_checked_at"] = persisted_at
        if existing["route_role"] == "utility_balance":
            existing["observed_at"] = persisted_at
    existing_rows.reverse()

    output, _, second_calls, _ = _run(
        module,
        apply=True,
        stamp=LATER,
        existing_rows=existing_rows,
        persist_result=_result(dry_run=False, unchanged=557),
    )

    second_batch = second_calls["persist"]["batch"]
    persisted_timestamps = {
        row["source_performance_id"]: (row["last_checked_at"], row["observed_at"])
        for row in existing_rows
    }
    assert {
        row["source_performance_id"]: (row["last_checked_at"], row["observed_at"])
        for row in second_batch.rows
    } == persisted_timestamps
    assert (output["inserted"], output["unchanged"], output["conflicts"]) == (0, 557, 0)
    assert output["wave1_values"] == {"state": "reused_first_snapshot", "routes": 0}


def test_apply_carries_the_latest_succeeded_pilot_values_into_the_days_rows() -> None:
    # Ruled 5 Sep 2026: the snapshot is the only Source Lab writer and merges the
    # latest succeeded pilot's measured values from the funded dataset.
    output, _, calls, _ = _run(module := _load_module(), apply=True, wave1_rows=_wave1_carry_rows())

    batch = calls["persist"]["batch"]
    by_route = {row["route_path"]: row for row in batch.rows if row["route_role"] == "inventory"}
    reels = by_route["/v1/instagram/search/reels"]
    comments = by_route["/v1/youtube/video/comments"]
    assert (reels["status"], reels["rows"], reels["unique_lift"]) == ("active", 87, 90.0)
    assert (comments["status"], comments["rows"], comments["unique_lift"]) == ("active", 335, 339.0)
    assert comments["downstream_consumers"] == ("signal_candidates_v2", "signal_evidence_v2")
    assert comments["kill_test_result"] == "passed"
    assert comments["run_id"] == "source_lab_2026-08-27"
    assert {
        row["status"]
        for path, row in by_route.items()
        if path not in ("/v1/instagram/search/reels", "/v1/youtube/video/comments")
    } == {"inventory_only"}
    assert sum(row["status"] != "inventory_only" for row in batch.rows) == 2
    assert output["wave1_values"] == {
        "state": "merged",
        "execution_id": PILOT_EXECUTION,
        "recorded_at": "2026-08-26T21:28:07Z",
        "routes": 2,
    }
    assert len(batch.rows) == 557


def test_apply_withholds_pilot_values_whose_route_identity_drifted() -> None:
    module = _load_module()
    output, _, calls, _ = _run(
        module, apply=True, wave1_rows=_wave1_carry_rows(catalog_digest="f" * 64)
    )

    batch = calls["persist"]["batch"]
    assert {row["status"] for row in batch.rows} == {"inventory_only"}
    assert output["wave1_values"] == {
        "state": "withheld_route_identity_drift",
        "execution_id": PILOT_EXECUTION,
        "recorded_at": "2026-08-26T21:28:07Z",
        "routes": 2,
    }


def test_same_day_rerun_keeps_the_first_snapshots_measured_fields() -> None:
    module = _load_module()
    _, _, first_calls, _ = _run(module, apply=True, wave1_rows=_wave1_carry_rows())
    first_batch = first_calls["persist"]["batch"]
    existing_rows = _existing_timestamp_rows(first_batch)

    output, _, second_calls, _ = _run(
        module,
        apply=True,
        stamp=LATER,
        existing_rows=existing_rows,
        persist_result=_result(dry_run=False, unchanged=557),
    )

    second_batch = second_calls["persist"]["batch"]
    measured = (
        "status",
        "rows",
        "unique_lift",
        "blocking_reason",
        "kill_test_result",
        "downstream_consumers",
    )
    assert {
        row["source_performance_id"]: tuple(row[field] for field in measured)
        for row in second_batch.rows
    } == {
        row["source_performance_id"]: tuple(row[field] for field in measured)
        for row in first_batch.rows
    }
    assert sum(row["status"] == "active" for row in second_batch.rows) == 2
    assert output["wave1_values"] == {"state": "reused_first_snapshot", "routes": 2}


@pytest.mark.parametrize("case", ["too_many", "mixed", "disagreeing"])
def test_apply_rejects_an_invalid_pilot_carry(case) -> None:
    module = _load_module()
    rows = _wave1_carry_rows()
    if case == "too_many":
        rows = [{**rows[0], "route_path": f"/v1/route/{index}"} for index in range(9)]
    elif case == "mixed":
        rows[1] = {**rows[1], "execution_id": "other-execution"}
    else:
        rows[1] = {**rows[1], "state": "permanently_rejected"}

    with pytest.raises(module.SnapshotFailure) as raised:
        _run(module, apply=True, wave1_rows=rows)

    assert raised.value.code == "wave1_values_invalid"


@pytest.mark.parametrize(
    ("route_role", "field", "value"),
    [
        ("inventory", "official_credits_label", "changed catalog metadata"),
        ("utility_balance", "balance", Decimal("249999")),
    ],
)
def test_timestamp_reuse_does_not_mask_non_timestamp_drift(route_role, field, value) -> None:
    module = _load_module()
    _, _, first_calls, credentials = _run(module, apply=True, existing_rows=())
    first_batch = first_calls["persist"]["batch"]
    existing_rows = _existing_timestamp_rows(first_batch)
    fresh_rows = [dict(row) for row in first_batch.rows]
    target = next(row for row in fresh_rows if row["route_role"] == route_role)
    target[field] = value
    target["last_checked_at"] = LATER
    if route_role == "utility_balance":
        target["observed_at"] = LATER
    calls = {}
    client = _Client(credentials=credentials, existing_rows=existing_rows, calls=calls)

    reused = module._reuse_first_observation_timestamps(
        client,
        tuple(fresh_rows),
        scope=module._scope(LATER),
        metric_date=LATER.date(),
        expected_row_count=len(first_batch.rows),
    )

    reused_target = next(
        row for row in reused if row["source_performance_id"] == target["source_performance_id"]
    )
    assert reused_target[field] == value
    assert reused_target["last_checked_at"] == STAMP
    assert reused_target["observed_at"] == (STAMP if route_role == "utility_balance" else None)


@pytest.mark.parametrize("case", ["partial", "duplicate", "mixed", "unknown", "too_many"])
def test_apply_rejects_incomplete_or_mixed_existing_snapshot(case) -> None:
    module = _load_module()
    _, _, first_calls, _ = _run(module, apply=True, existing_rows=())
    existing_rows = _existing_timestamp_rows(first_calls["persist"]["batch"])
    if case == "partial":
        existing_rows = existing_rows[:-1]
    elif case == "duplicate":
        existing_rows = [*existing_rows[:-1], dict(existing_rows[0])]
    elif case == "mixed":
        existing_rows[0] = {**existing_rows[0], "route_role": "utility_balance"}
    elif case == "unknown":
        existing_rows[0] = {**existing_rows[0], "source_performance_id": "srcperf_" + "0" * 64}
    else:
        existing_rows.append(_PoisonExistingRow())

    with pytest.raises(module.SnapshotFailure) as raised:
        _run(module, apply=True, stamp=LATER, existing_rows=existing_rows)

    assert raised.value.code == "immutable_conflict"


@pytest.mark.parametrize(
    ("catalog", "balance"),
    [
        (_Response(_catalog_payload(), status_code=302), _Response(_balance_payload())),
        (_Response(_catalog_payload()), _Response(_balance_payload(), status_code=302)),
    ],
)
def test_redirects_are_rejected_without_following_them(catalog, balance) -> None:
    module = _load_module()
    session = _Session(catalog, balance)
    with pytest.raises(module.SnapshotFailure) as raised:
        _run(module, session=session)
    assert raised.value.code in {"catalog_read_failed", "balance_read_failed"}
    assert all(call[2] is False for call in session.calls)


@pytest.mark.parametrize(("route",), [("catalog",), ("balance",)])
@pytest.mark.parametrize(
    ("receipt", "receipt_id"),
    [
        (MISSING, "missing"),
        (False, "boolean_false"),
        (True, "boolean_true"),
        ("0", "string"),
        (None, "null"),
        ([], "list"),
        ({}, "object"),
        (1, "nonzero"),
        (float("nan"), "not_a_number"),
        (float("inf"), "infinity"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_each_vendor_receipt_requires_an_explicit_finite_numeric_zero(
    route, receipt, receipt_id
) -> None:
    module = _load_module()
    catalog_payload = _catalog_payload()
    balance_payload = _balance_payload()
    payload = catalog_payload if route == "catalog" else balance_payload
    if receipt is MISSING:
        payload.pop("credits_used")
    else:
        payload["credits_used"] = receipt
    session = _Session(_Response(catalog_payload), _Response(balance_payload))
    with pytest.raises(module.SnapshotFailure) as raised:
        _run(module, session=session)
    assert raised.value.code == "nonzero_credit_receipt", receipt_id


def test_catalog_zero_credit_guard_isolated_from_catalog_normalization(monkeypatch) -> None:
    module = _load_module()
    catalog_payload = _catalog_payload()
    guarded = []

    def guard(payload) -> None:
        guarded.append(payload)
        raise module.SnapshotFailure("nonzero_credit_receipt")

    monkeypatch.setattr(module, "_require_zero_credit", guard)
    monkeypatch.setattr(module, "normalize_catalog", _unexpected("catalog normalization"))
    session = _Session(_Response(catalog_payload), _Response(_balance_payload()))

    with pytest.raises(module.SnapshotFailure) as raised:
        _run(module, session=session)

    assert raised.value.code == "nonzero_credit_receipt"
    assert guarded == [catalog_payload]


def test_balance_zero_credit_guard_isolated_from_balance_normalization(monkeypatch) -> None:
    module = _load_module()
    catalog_payload = _catalog_payload()
    balance_payload = _balance_payload()
    guarded = []

    def guard(payload) -> None:
        guarded.append(payload)
        if payload is balance_payload:
            raise module.SnapshotFailure("nonzero_credit_receipt")

    monkeypatch.setattr(module, "_require_zero_credit", guard)
    monkeypatch.setattr(module, "normalize_balance", _unexpected("balance normalization"))
    session = _Session(_Response(catalog_payload), _Response(balance_payload))

    with pytest.raises(module.SnapshotFailure) as raised:
        _run(module, session=session)

    assert raised.value.code == "nonzero_credit_receipt"
    assert guarded == [catalog_payload, balance_payload]


def test_pinned_fixture_drift_fails_closed_before_persistence() -> None:
    module = _load_module()
    catalog = _catalog_payload()
    catalog["data"]["endpoints"][0]["summary"] = "changed catalog content"
    with pytest.raises(module.SnapshotFailure) as raised:
        _run(module, session=_Session(_Response(catalog), _Response(_balance_payload())))
    assert raised.value.code == "catalog_contract_invalid"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (persistence.ImmutableConflict(SECRET), "immutable_conflict"),
        (persistence.PersistenceError(SECRET), "persistence_failed"),
        (persistence.CleanupFailure(SECRET), "cleanup_failed"),
    ],
)
def test_apply_maps_persistence_failures_without_retry_or_secret_echo(error, code) -> None:
    module = _load_module()
    persistence_calls = 0
    query_calls = {}

    def persist(**kwargs):
        nonlocal persistence_calls
        persistence_calls += 1
        raise error

    session = _Session()
    credentials = _credentials()
    with pytest.raises(module.SnapshotFailure) as raised:
        module.run_snapshot(
            target="staging",
            apply=True,
            clock=lambda: STAMP,
            credential_loader=lambda: (credentials, PROJECT),
            secret_reader=lambda name: SECRET,
            session_factory=lambda: session,
            client_factory=lambda **kwargs: _Client(
                credentials=credentials,
                existing_rows=(),
                calls=query_calls,
            ),
            persist=persist,
        )
    assert persistence_calls == 1
    assert (raised.value.code, str(raised.value)) == (code, code)
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("identity", "runtime_identity_invalid"),
        ("secret", "secret_unavailable"),
        ("catalog", "catalog_read_failed"),
        ("balance", "balance_read_failed"),
        ("batch", "batch_contract_invalid"),
        ("cleanup", "cleanup_failed"),
    ],
)
def test_every_bounded_failure_is_sanitized(monkeypatch, scenario, code) -> None:
    module = _load_module()
    credentials = _credentials()
    session = _Session()

    def credential_loader():
        if scenario == "identity":
            raise RuntimeError(SECRET)
        return credentials, PROJECT

    def secret_reader(name):
        if scenario == "secret":
            raise RuntimeError(SECRET)
        return SECRET

    if scenario == "catalog":
        session.catalog = _Response({}, json_error=RuntimeError(SECRET))
    elif scenario == "balance":
        session.balance = _Response({}, json_error=RuntimeError(SECRET))
    elif scenario == "batch":
        monkeypatch.setattr(
            module,
            "SourceLabRowBatch",
            lambda **kwargs: (_ for _ in ()).throw(persistence.SourceLabBatchInvalid(SECRET)),
        )
    elif scenario == "cleanup":
        session.close_error = RuntimeError(SECRET)
    with pytest.raises(module.SnapshotFailure) as raised:
        module.run_snapshot(
            target="staging",
            apply=False,
            clock=lambda: STAMP,
            credential_loader=credential_loader,
            secret_reader=secret_reader,
            session_factory=lambda: session,
            client_factory=_unexpected("BigQuery"),
            persist=lambda **kwargs: _result(dry_run=True),
        )
    assert (raised.value.code, str(raised.value)) == (code, code)
    assert SECRET not in str(raised.value)


def test_main_prints_one_stably_ordered_json_object_with_native_numbers(
    monkeypatch, capsys
) -> None:
    module = _load_module()
    expected, _, _, _ = _run(module)
    runner_kwargs = {}

    def runner(**kwargs):
        runner_kwargs.update(kwargs)
        return expected

    monkeypatch.setattr(module, "run_snapshot", runner)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--target", "staging"])
    assert module.main() is None
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == json.dumps(expected, separators=(",", ":")) + "\n"
    decoded = json.loads(captured.out)
    assert tuple(decoded) == tuple(expected)
    assert type(decoded["balance"]) is int
    assert type(decoded["credits_used"]) is int
    assert runner_kwargs["secret_reader"] is module._default_secret_reader


def test_snapshot_runner_records_dropped_rows_from_the_funded_catalog(monkeypatch):
    module = _load_module()
    live_catalog = json.loads(LIVE_FUNDED_FIXTURE.read_text(encoding="utf-8"))
    normalized = source_lab.normalize_catalog(
        live_catalog,
        checked_at=datetime(2026, 9, 27, 13, 10, tzinfo=UTC),
        expected_platforms=4,
        expected_endpoints=9,
    )
    version = module.EXPECTATION_VERSION
    fixture_expectation = (
        normalized.platform_count,
        normalized.route_count,
        normalized.social_platform_count,
        normalized.catalog_digest,
        normalized.normalized_content_digest,
    )
    expectations = MappingProxyType({**source_lab.CATALOG_EXPECTATIONS, version: fixture_expectation})
    normalized_counts = MappingProxyType(
        {**source_lab.CATALOG_NORMALIZED_ROUTE_COUNTS, version: 8}
    )
    monkeypatch.setattr(source_lab, "CATALOG_EXPECTATIONS", expectations)
    monkeypatch.setattr(persistence, "CATALOG_EXPECTATIONS", expectations)
    monkeypatch.setattr(source_lab, "CATALOG_NORMALIZED_ROUTE_COUNTS", normalized_counts)
    monkeypatch.setattr(persistence, "CATALOG_NORMALIZED_ROUTE_COUNTS", normalized_counts)
    session = _Session(catalog=_Response(live_catalog))

    result, _, _, _ = _run(
        module,
        stamp=datetime(2026, 9, 27, 13, 10, tzinfo=UTC),
        session=session,
        persist_result=_result(dry_run=True, validated_count=9),
    )

    assert result["endpoints_observed"] == 9
    assert result["endpoints_normalized"] == 8
    assert len(result["dropped_endpoint_rows"]) == 1
    assert all(row["route"] and row["reason"] for row in result["dropped_endpoint_rows"])
    assert result["rows_planned"] == 9
    assert result["catalog_digest"] == normalized.catalog_digest


@pytest.mark.parametrize("mutation", ["range", "metered"])
def test_current_snapshot_refuses_wave1_price_or_meter_flag_drift(mutation):
    module = _load_module()
    catalog = json.loads(LIVE_FUNDED_FIXTURE.read_text(encoding="utf-8"))
    reels = next(
        row for row in catalog["data"]["endpoints"]
        if row["path"] == "/v1/instagram/search/reels"
    )
    if mutation == "range":
        reels["credits_label"] = reels["credits_label"].replace("1-9 (metered)", "1-8 (metered)", 1)
    else:
        reels["metered"] = False
    with pytest.raises(module.SnapshotFailure) as raised:
        _run(
            module,
            stamp=datetime(2026, 9, 27, 13, 10, tzinfo=UTC),
            session=_Session(catalog=_Response(catalog)),
        )
    assert raised.value.code == "catalog_contract_invalid"


def test_current_snapshot_refuses_historical_wave1_prices():
    module = _load_module()
    with pytest.raises(module.SnapshotFailure) as raised:
        _run(module, stamp=datetime(2026, 9, 27, 13, 10, tzinfo=UTC))
    assert raised.value.code == "catalog_contract_invalid"


def test_main_emits_only_the_sanitized_error_code(monkeypatch, capsys) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "run_snapshot",
        lambda **kwargs: (_ for _ in ()).throw(module.SnapshotFailure("persistence_failed")),
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--target", "staging", "--apply"])
    with pytest.raises(SystemExit) as raised:
        module.main()
    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.out == ""
    assert captured.err == '{"error":"persistence_failed"}\n'
    assert SECRET not in captured.err
