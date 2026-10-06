"""Wave 1 measured source values: funded ledger rows and the snapshot's carry read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import funded_source_values as values_module
from src.analysis.open_intelligence.funded_lane import (
    APPROVED_CATALOG_DIGEST,
    APPROVED_METADATA_DIGEST,
)
from src.analysis.open_intelligence.funded_lane_runtime import FundedRunCloseReceipt
from src.analysis.open_intelligence.funded_source_values import (
    Wave1SourceValueConflict,
    Wave1SourceValuePersistenceError,
    Wave1SourceValueRow,
    Wave1SourceValuesInvalid,
    build_wave1_source_value_rows,
    persist_wave1_source_value_rows,
    read_latest_wave1_source_values,
    source_value_id,
    wave1_close_succeeded,
)
from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

NOW = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)
WRITER = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"


def _receipt(**over):
    values = {
        "attribution_state": "conservative",
        "calls": 8,
        "budget_debit_credits": Decimal("8"),
        "vendor_reported_credits": Decimal("7"),
        "balance_delta": Decimal("31"),
        "attribution_gap_credits": Decimal("0"),
        "run_balance_delta": Decimal("7"),
    }
    values.update(over)
    return FundedRunCloseReceipt(**values)


def _values():
    return {
        "/v1/youtube/video/comments": evaluate_wave1_source_value(
            unique_observations=335, marginal_candidates=4, marginal_evidence=335
        ),
        "/v1/instagram/search/reels": evaluate_wave1_source_value(
            unique_observations=87, marginal_candidates=3, marginal_evidence=87
        ),
    }


def _rows(**over):
    kwargs = {
        "execution_id": "trends-engine-open-intelligence-staging-rh8vq",
        "run_id": "run_wave1_values",
        "recorded_at": NOW,
        "close_verdict": "succeeded",
        "attribution_state": "conservative",
        "source_sha": "a" * 40,
        "catalog_digest": APPROVED_CATALOG_DIGEST,
        "metadata_digest": APPROVED_METADATA_DIGEST,
        "values": _values(),
        "created_at": NOW + timedelta(seconds=1),
    }
    kwargs.update(over)
    return build_wave1_source_value_rows(**kwargs)


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        (_receipt(attribution_state="complete", vendor_reported_credits=Decimal("8")), True),
        (_receipt(), True),
        (_receipt(attribution_gap_credits=Decimal("1")), False),
        (_receipt(run_balance_delta=None), False),
        (_receipt(run_balance_delta=Decimal("6")), False),
        (_receipt(vendor_reported_credits=Decimal("9"), run_balance_delta=Decimal("9")), False),
        (_receipt(attribution_state="gap_detected"), False),
        (_receipt(balance_read_status="unavailable"), False),
        (_receipt(terminal_id="term_" + "0" * 64), False),
    ],
)
def test_close_verdict_is_the_held_bound_with_a_measured_balance_and_no_terminal(
    receipt, expected
) -> None:
    # Ratified by Albert 5 Sep 2026: complete, or conservative with zero gap where the
    # run's balance movement equals what the vendor reported inside the quote.
    assert wave1_close_succeeded(receipt) is expected


def test_rows_are_one_per_route_sorted_and_content_addressed() -> None:
    rows = _rows()

    assert [row.route_path for row in rows] == [
        "/v1/instagram/search/reels",
        "/v1/youtube/video/comments",
    ]
    assert all(isinstance(row, Wave1SourceValueRow) for row in rows)
    assert rows[0].source_value_id == source_value_id(
        "trends-engine-open-intelligence-staging-rh8vq", "/v1/instagram/search/reels"
    )
    assert rows[0].source_value_id.startswith("swsv_")
    assert len(rows[0].source_value_id) == 69
    assert rows[0].source_value_id != rows[1].source_value_id
    assert (rows[1].state, rows[1].reason, rows[1].unique_observations) == ("passed", None, 335)
    assert (rows[1].marginal_candidates, rows[1].marginal_evidence) == (4, 335)
    assert rows[1].values_contract_version == "socialcrawl_wave1_source_values_v1"
    assert rows[1].credential_lane == "ogilvy_funded"
    assert (rows[1].close_verdict, rows[1].attribution_state) == ("succeeded", "conservative")
    assert rows[1].created_at == NOW + timedelta(seconds=1)
    assert _rows(source_sha=None)[0].source_sha is None


@pytest.mark.parametrize(
    "over",
    [
        {"values": {}},
        {"values": {"/v1/linkedin/search": _values()["/v1/youtube/video/comments"]}},
        {"values": {"/v1/youtube/video/comments": "passed"}},
        {"close_verdict": "maybe"},
        {"attribution_state": "held"},
        {"catalog_digest": "abc"},
        {"metadata_digest": None},
        {"source_sha": "xyz"},
        {"recorded_at": NOW.replace(tzinfo=None)},
        {"created_at": NOW.astimezone(UTC).replace(tzinfo=None)},
        {"execution_id": ""},
        {"run_id": " run"},
    ],
)
def test_rows_refuse_malformed_inputs(over) -> None:
    with pytest.raises(Wave1SourceValuesInvalid):
        _rows(**over)


class _Credentials:
    service_account_email = WRITER
    quota_project_id = "ogilvy-trends-v2"


class _Job:
    errors = None

    def __init__(self, rows=()):
        self.rows = rows

    def result(self, *, max_results=None, retry=None, job_retry=None):
        return self.rows


class _LostJob:
    errors = None

    def result(self, *, max_results=None, retry=None, job_retry=None):
        raise TimeoutError("transaction response was lost")


class _Client:
    project = "ogilvy-trends-v2"
    location = "US"
    _credentials = _Credentials()

    def __init__(self, rows, *, conflict=False, lost=False, readback_drift=False):
        self.by_id = {row.source_value_id: row for row in rows}
        self.conflict = conflict
        self.lost = lost
        self.readback_drift = readback_drift
        self.calls = []

    def query(self, sql, *, location, job_config, retry=None, job_retry=None):
        self.calls.append((sql, location, job_config, retry, job_retry))
        if job_config.dry_run:
            return _Job()
        parameters = {parameter.name: parameter.value for parameter in job_config.query_parameters}
        if sql.startswith("DECLARE"):
            if self.lost:
                return _LostJob()
            counts = (
                {"inserted_count": 0, "unchanged_count": 0, "conflict_count": 1}
                if self.conflict
                else {"inserted_count": 1, "unchanged_count": 0, "conflict_count": 0}
            )
            return _Job((counts,))
        row = self.by_id[parameters["source_value_id"]]
        payload = {field: getattr(row, field) for field in values_module._FIELDS}
        if self.readback_drift:
            payload["unique_observations"] += 1
        return _Job((payload,))


def test_persist_runs_one_dry_and_one_real_transaction_per_row_and_reads_each_back() -> None:
    rows = _rows()
    client = _Client(rows)

    result = persist_wave1_source_value_rows(
        project="ogilvy-trends-v2", dataset="trends_v2_staging_funded", client=client, rows=rows
    )

    assert (result.inserted_count, result.unchanged_count) == (2, 0)
    assert len(client.calls) == 6
    for offset, row in enumerate(rows):
        dry, real, readback = client.calls[offset * 3 : offset * 3 + 3]
        assert dry[2].dry_run is True
        assert dry[2].use_query_cache is False
        assert dry[0] == real[0]
        assert real[0].startswith("DECLARE inserted_count")
        assert (
            "`ogilvy-trends-v2.trends_v2_staging_funded.socialcrawl_wave1_source_values_v1`"
            in real[0]
        )
        assert "target.source_value_id = @source_value_id" in real[0]
        assert "IF conflict_count = 0 AND unchanged_count = 0 THEN" in real[0]
        assert readback[0].startswith("SELECT `values_contract_version`")
        assert readback[0].endswith("WHERE source_value_id = @source_value_id LIMIT 2")
        parameters = {parameter.name: parameter for parameter in real[2].query_parameters}
        assert parameters["source_value_id"].value == row.source_value_id
        assert parameters["recorded_at"].type_ == "TIMESTAMP"
        assert parameters["unique_observations"].type_ == "INT64"
        assert parameters["reason"].type_ == "STRING"
        assert parameters["reason"].value is None
        assert all(
            call[1] == "US" and call[3] is None and call[4] is None
            for call in (dry, real, readback)
        )


def test_persist_maps_conflicts_lost_responses_and_foreign_identities() -> None:
    rows = _rows()

    with pytest.raises(Wave1SourceValueConflict):
        persist_wave1_source_value_rows(
            project="ogilvy-trends-v2",
            dataset="trends_v2_staging_funded",
            client=_Client(rows, conflict=True),
            rows=rows,
        )

    lost = persist_wave1_source_value_rows(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging_funded",
        client=_Client(rows, lost=True),
        rows=rows,
    )
    assert (lost.inserted_count, lost.unchanged_count) == (0, 2)

    with pytest.raises(Wave1SourceValueConflict):
        persist_wave1_source_value_rows(
            project="ogilvy-trends-v2",
            dataset="trends_v2_staging_funded",
            client=_Client(rows, readback_drift=True),
            rows=rows,
        )

    foreign = _Client(rows)
    foreign._credentials = SimpleNamespace(
        service_account_email="trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        quota_project_id=None,
    )
    with pytest.raises(ValueError, match="exact funded writer"):
        persist_wave1_source_value_rows(
            project="ogilvy-trends-v2",
            dataset="trends_v2_staging_funded",
            client=foreign,
            rows=rows,
        )
    assert foreign.calls == []

    with pytest.raises(ValueError, match="exact staging target"):
        persist_wave1_source_value_rows(
            project="ogilvy-trends-v2", dataset="trends_v2_staging", client=_Client(rows), rows=rows
        )
    with pytest.raises(ValueError, match="rows are invalid"):
        persist_wave1_source_value_rows(
            project="ogilvy-trends-v2",
            dataset="trends_v2_staging_funded",
            client=_Client(rows),
            rows=rows + rows[:1],
        )


def test_writer_retry_preserves_first_creation_time_after_a_partial_commit() -> None:
    from src.analysis.open_intelligence.funded_lane_runtime import make_wave1_source_value_writer

    class PartialCommitClient(_Client):
        def __init__(self):
            self.stored = {}
            self.fail_second_route = True

        def query(self, sql, *, location, job_config, retry=None, job_retry=None):
            if job_config.dry_run:
                return _Job()
            parameters = {
                parameter.name: parameter.value for parameter in job_config.query_parameters
            }
            row_id = parameters["source_value_id"]
            if sql.startswith("DECLARE"):
                row = Wave1SourceValueRow(**parameters)
                if row.route_path == "/v1/youtube/video/comments" and self.fail_second_route:
                    self.fail_second_route = False
                    raise TimeoutError("second route did not commit")
                existing = self.stored.get(row_id)
                if existing is None:
                    self.stored[row_id] = row
                return _Job(
                    (
                        {
                            "inserted_count": int(existing is None),
                            "unchanged_count": int(existing == row),
                            "conflict_count": int(existing is not None and existing != row),
                        },
                    )
                )
            existing = self.stored.get(row_id)
            return _Job(
                ()
                if existing is None
                else ({field: getattr(existing, field) for field in values_module._FIELDS},)
            )

    clock_time = NOW + timedelta(seconds=1)
    runtime = SimpleNamespace(
        execution_id="execution_retry",
        run_id="run_retry",
        close_receipt=_receipt(),
        close_recorded_at=NOW,
        close_verdict="succeeded",
        _source_sha="a" * 40,
        gate=SimpleNamespace(
            catalog_digest=APPROVED_CATALOG_DIGEST, metadata_digest=APPROVED_METADATA_DIGEST
        ),
        _clock=lambda: clock_time,
    )
    client = PartialCommitClient()
    writer = make_wave1_source_value_writer(
        client=client, dataset="trends_v2_staging_funded", runtime=runtime
    )

    with pytest.raises(Wave1SourceValuePersistenceError, match="invalid cardinality"):
        writer(_values())
    assert len(client.stored) == 1
    original_row = next(iter(client.stored.values()))

    clock_time += timedelta(minutes=5)
    writer(_values())

    assert len(client.stored) == 2
    assert client.stored[original_row.source_value_id] == original_row
    assert {row.created_at for row in client.stored.values()} == {NOW + timedelta(seconds=1)}

    changed = _values()
    changed["/v1/instagram/search/reels"] = evaluate_wave1_source_value(
        unique_observations=88, marginal_candidates=3, marginal_evidence=88
    )
    with pytest.raises(Wave1SourceValueConflict):
        writer(changed)
    assert client.stored[original_row.source_value_id] == original_row


class _ReadClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, sql, *, location, job_config, retry=None, job_retry=None):
        self.calls.append((sql, location, job_config, retry, job_retry))
        return _Job(self.rows)


def _carry_rows(rows=None, **over):
    rows = rows or _rows()
    return [
        {
            "execution_id": row.execution_id,
            "recorded_at": row.recorded_at,
            "catalog_digest": row.catalog_digest,
            "metadata_digest": row.metadata_digest,
            "route_path": row.route_path,
            "state": row.state,
            "reason": row.reason,
            "unique_observations": row.unique_observations,
            "marginal_candidates": row.marginal_candidates,
            "marginal_evidence": row.marginal_evidence,
            **over,
        }
        for row in rows
    ]


def test_read_returns_the_latest_succeeded_pilot_inside_the_carry_window() -> None:
    client = _ReadClient(_carry_rows())
    observed_at = NOW + timedelta(hours=9)

    carry = read_latest_wave1_source_values(client=client, observed_at=observed_at)

    assert carry is not None
    assert carry.execution_id == "trends-engine-open-intelligence-staging-rh8vq"
    assert carry.recorded_at == NOW
    assert (carry.catalog_digest, carry.metadata_digest) == (
        APPROVED_CATALOG_DIGEST,
        APPROVED_METADATA_DIGEST,
    )
    assert dict(carry.values) == _values()
    with pytest.raises(TypeError):
        carry.values["/v1/tiktok/song"] = None
    (call,) = client.calls
    sql, location, job_config, retry, job_retry = call
    assert "`ogilvy-trends-v2.trends_v2_staging.v_socialcrawl_wave1_source_values_v1`" in sql
    assert "close_verdict = 'succeeded'" in sql
    assert "recorded_at < @observed_at" in sql
    assert "recorded_at >= @window_start" in sql
    assert "ORDER BY recorded_at DESC, execution_id DESC" in sql
    assert "LIMIT 1" in sql
    assert (location, retry, job_retry) == ("US", None, None)
    assert {parameter.name: parameter.value for parameter in job_config.query_parameters} == {
        "observed_at": observed_at,
        "window_start": observed_at - timedelta(hours=48),
    }
    assert read_latest_wave1_source_values(client=_ReadClient(()), observed_at=observed_at) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows * 5,
        lambda rows: [rows[0], {**rows[1], "execution_id": "other"}],
        lambda rows: [rows[0], {**rows[1], "recorded_at": NOW + timedelta(seconds=1)}],
        lambda rows: [rows[0], {**rows[1], "state": "permanently_rejected"}],
        lambda rows: [rows[0], {**rows[1], "reason": "zero_unique_observations"}],
        lambda rows: [rows[0], {**rows[1], "route_path": rows[0]["route_path"]}],
        lambda rows: [rows[0], {**rows[1], "route_path": "/v1/linkedin/search"}],
        lambda rows: [rows[0], {**rows[1], "unique_observations": -1}],
        lambda rows: [rows[0], {**rows[1], "catalog_digest": "short"}],
        lambda rows: [{key: value for key, value in rows[0].items() if key != "reason"}],
    ],
)
def test_read_refuses_carries_that_mix_executions_or_disagree_with_their_counts(mutate) -> None:
    client = _ReadClient(mutate(_carry_rows()))

    with pytest.raises(Wave1SourceValuesInvalid):
        read_latest_wave1_source_values(client=client, observed_at=NOW + timedelta(hours=1))


def test_read_requires_a_utc_observation_time() -> None:
    with pytest.raises(Wave1SourceValuesInvalid):
        read_latest_wave1_source_values(
            client=_ReadClient(()), observed_at=NOW.replace(tzinfo=None)
        )


def test_readback_cardinality_and_count_faults_are_persistence_errors() -> None:
    rows = _rows()

    class TwoRows(_Client):
        def query(self, sql, *, location, job_config, retry=None, job_retry=None):
            job = super().query(
                sql, location=location, job_config=job_config, retry=retry, job_retry=job_retry
            )
            if sql.startswith("SELECT"):
                return _Job(tuple(job.rows) * 2)
            return job

    with pytest.raises(Wave1SourceValuePersistenceError, match="invalid cardinality"):
        persist_wave1_source_value_rows(
            project="ogilvy-trends-v2",
            dataset="trends_v2_staging_funded",
            client=TwoRows(rows),
            rows=rows,
        )
