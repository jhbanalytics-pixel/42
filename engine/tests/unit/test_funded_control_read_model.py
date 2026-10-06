from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from src.analysis.open_intelligence.funded_control import (
    CONTROL_CONTRACT_VERSION,
    FUNDED_ACCOUNT,
    FUNDED_CONTROL_TABLE,
    FUNDED_CONTROL_VIEW,
    FundedControlReceipt,
    build_funded_control_receipt,
)
from src.analysis.open_intelligence.funded_control_persistence import (
    FundedControlConflict,
    persist_funded_control_receipt,
)
from src.analysis.open_intelligence.funded_control_reader import (
    FundedBudgetReadInvalid,
    read_funded_budget,
)
from src.analysis.open_intelligence.funded_lane import (
    APPROVED_CATALOG_DIGEST,
    APPROVED_METADATA_DIGEST,
    FUNDED_CREDENTIAL_LANE,
    MONTHLY_CAP,
    OPENING_BALANCE,
    RESERVE_FLOOR,
    FundedLaneRules,
    MonthlySpendSnapshot,
    evaluate_funded_lane,
)
from src.contracts.bigquery_ddl import parse_table_ddl

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "infra" / "bigquery_schemas" / "socialcrawl_funded_control_receipts_v1.sql"
VIEW = ROOT / "infra" / "bigquery_views" / "v_socialcrawl_funded_budget_v1.sql"
NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)

EXPECTED_FIELDS = (
    ("control_contract_version", "STRING", "REQUIRED"),
    ("control_id", "STRING", "REQUIRED"),
    ("recorded_at", "TIMESTAMP", "REQUIRED"),
    ("month_start", "DATE", "REQUIRED"),
    ("credential_lane", "STRING", "REQUIRED"),
    ("funding_account", "STRING", "REQUIRED"),
    ("activation_stage", "INT64", "REQUIRED"),
    ("kill_state", "STRING", "REQUIRED"),
    ("current_balance", "NUMERIC", "REQUIRED"),
    ("balance_observed_at", "TIMESTAMP", "REQUIRED"),
    ("opening_balance", "NUMERIC", "REQUIRED"),
    ("month_opening_balance", "NUMERIC", "REQUIRED"),
    ("monthly_cap", "NUMERIC", "REQUIRED"),
    ("reserve_floor", "NUMERIC", "REQUIRED"),
    ("stage_cap", "NUMERIC", "REQUIRED"),
    ("monthly_ledger_debit", "NUMERIC", "REQUIRED"),
    ("monthly_balance_delta", "NUMERIC", "REQUIRED"),
    ("monthly_effective_spend", "NUMERIC", "REQUIRED"),
    ("monthly_remaining", "NUMERIC", "REQUIRED"),
    ("reserve_remaining", "NUMERIC", "REQUIRED"),
    ("run_allowance", "NUMERIC", "REQUIRED"),
    ("attribution_state", "STRING", "REQUIRED"),
    ("consecutive_complete_runs", "INT64", "REQUIRED"),
    ("runs_today", "INT64", "REQUIRED"),
    ("catalog_digest", "STRING", "REQUIRED"),
    ("metadata_digest", "STRING", "REQUIRED"),
    ("source_sha", "STRING", "REQUIRED"),
    ("created_at", "TIMESTAMP", "REQUIRED"),
)


def _rules() -> FundedLaneRules:
    return FundedLaneRules(
        credential_lane=FUNDED_CREDENTIAL_LANE,
        opening_balance=OPENING_BALANCE,
        monthly_cap=MONTHLY_CAP,
        reserve_floor=RESERVE_FLOOR,
        stage_caps={
            0: Decimal("0"),
            1: Decimal("100"),
            2: Decimal("250"),
            3: Decimal("750"),
        },
        catalog_digest=APPROVED_CATALOG_DIGEST,
        metadata_digest=APPROVED_METADATA_DIGEST,
    )


def _gate(*, stage: int = 1, complete_runs: int = 0):
    return evaluate_funded_lane(
        MonthlySpendSnapshot(
            as_of=NOW,
            month_start=date(2026, 8, 1),
            current_balance=Decimal("250100"),
            month_opening_balance=Decimal("250100"),
            monthly_ledger_debit=Decimal("0"),
            monthly_vendor_reported=Decimal("0"),
            unreconciled_execution_ids=(),
            activation_stage=stage,
            consecutive_complete_runs=complete_runs,
            runs_today=0,
            catalog_digest=APPROVED_CATALOG_DIGEST,
            metadata_digest=APPROVED_METADATA_DIGEST,
        ),
        _rules(),
    )


def test_schema_and_view_are_exactly_scoped_to_the_funded_staging_dataset() -> None:
    parsed = parse_table_ddl(
        SCHEMA.read_text(encoding="utf-8")
        .replace("{project}", "ogilvy-trends-v2")
        .replace("{dataset}", "trends_v2_staging_funded")
    )

    assert (
        tuple((name, kind, mode) for name, kind, mode, _, _ in parsed["fields"]) == EXPECTED_FIELDS
    )
    assert parsed["partition"] == "month_start"
    assert parsed["cluster"] == (
        "credential_lane",
        "activation_stage",
        "kill_state",
        "attribution_state",
    )

    sql = VIEW.read_text(encoding="utf-8")
    assert "`{project}.{view_dataset}.v_socialcrawl_funded_budget_v1`" in sql
    assert "`{project}.{source_dataset}.socialcrawl_funded_control_receipts_v1`" in sql
    assert "`{project}.{source_dataset}.socialcrawl_credit_ledger_v1`" in sql
    assert "AS ledger_through" in sql
    assert f".{FUNDED_CONTROL_TABLE}`" in sql
    assert f".{FUNDED_CONTROL_VIEW}`" in sql
    assert "credential_lane = 'ogilvy_funded'" in sql
    assert "DATE_TRUNC(CURRENT_DATE('UTC'), MONTH)" in sql
    assert "event_type IN ('phase_close', 'attribution_gap')" in sql
    assert "event_type = 'run_close'" not in sql
    assert "recorded_at > control.recorded_at" in sql
    assert APPROVED_CATALOG_DIGEST in sql
    assert APPROVED_METADATA_DIGEST in sql
    assert "control.control_id = LOWER(TO_HEX(SHA256(CONCAT(" in sql
    assert "REGEXP_CONTAINS(control.source_sha, r'^[0-9a-f]{40}$')" in sql
    assert "latest_control AS (" in sql
    assert "FROM latest_control AS control" in sql
    assert sql.index("QUALIFY ROW_NUMBER()") < sql.index("candidate_controls AS (")
    assert "control.recorded_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 MINUTE)" in sql
    assert (
        "control.balance_observed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 MINUTE)"
        in sql
    )
    assert "control.recorded_at <= CURRENT_TIMESTAMP()" in sql
    assert "control.balance_observed_at <= control.recorded_at" in sql
    assert "jhb_core" not in sql
    compact_sql = "".join(sql.split())
    assert (
        "AND((control.catalog_digest='6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6'"
        "ANDcontrol.metadata_digest='7d7152b2441f367417c74169a7bd4fb1a190a92763bbb43532167d5d4612fdfc')"
        "OR(control.catalog_digest='6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6'"
        "ANDcontrol.metadata_digest='501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a'))"
        in compact_sql
    )

    import scripts.setup_bigquery as setup

    assert setup.FUNDED_LANE_VIEW_ORDER == [
        "v_socialcrawl_funded_budget_v1.sql",
        "v_socialcrawl_funded_budget_v2.sql",
        "v_socialcrawl_wave1_source_values_v1.sql",
    ]
    assert setup.view_order_for_dataset("trends_v2_staging_funded") == setup.FUNDED_LANE_VIEW_ORDER
    assert setup.view_order_for_dataset("trends_v2_staging") == []
    principal_access = {
        (entry.role, entry.entity_type, entry.entity_id)
        for entry in setup._funded_lane_access_entries()
        if entry.entity_type != "view"
    }
    assert (
        "READER",
        "userByEmail",
        "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    ) not in principal_access
    authorized_views = {
        tuple(sorted(entry.entity_id.items()))
        for entry in setup._funded_lane_access_entries()
        if entry.entity_type == "view"
    }
    assert authorized_views == {
        (
            ("datasetId", "trends_v2_staging"),
            ("projectId", "ogilvy-trends-v2"),
            ("tableId", "v_socialcrawl_funded_budget_v1"),
        ),
        (
            ("datasetId", "trends_v2_staging"),
            ("projectId", "ogilvy-trends-v2"),
            ("tableId", "v_socialcrawl_funded_budget_v2"),
        ),
        (
            ("datasetId", "trends_v2_staging"),
            ("projectId", "ogilvy-trends-v2"),
            ("tableId", "v_socialcrawl_wave1_source_values_v1"),
        ),
    }
    assert all("prod" not in identity for _, _, identity in principal_access)


def test_every_funded_writer_uses_the_durable_wave1_execution_identity() -> None:
    import scripts.setup_bigquery as setup
    from src.analysis.open_intelligence import (
        execution_approval,
        execution_generations,
        execution_origins,
        funded_lane,
    )

    # Fresh Wave 1 executions run under the v2 origin, whose registry binds wave1_pilot
    # to intelligence-42-funded; every funded writer admits that binding and no other.
    registry = execution_generations.active_generation().registry
    origin = execution_approval._v2_origin_for_operation(
        "wave1_pilot", mode="new_consume", registry=registry
    )
    identity = origin.operation_bindings["wave1_pilot"].service_identity

    policy = json.loads(
        execution_origins._policy_bytes_for_origin(registry=registry, origin=origin)
    )

    assert identity == "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
    # The job and every writer resolve it, the policy rule the loader checks the
    # manifest against names it, and the dataset setup script grants it.
    assert identity == funded_lane.wave1_pilot_service_identity()
    assert identity == policy["operation_validation"]["wave1_pilot"]["service_identity"]
    assert identity == setup.FUNDED_LANE_WRITER
    # The retained v1 contract identity is unchanged and is not the one setup grants.
    retained = execution_approval._OPERATION_CONTRACTS["wave1_pilot"]["identity"]
    assert retained == "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert retained != setup.FUNDED_LANE_WRITER


def test_receipt_is_canonical_formula_checked_and_content_addressed() -> None:
    receipt = build_funded_control_receipt(
        gate=_gate(),
        recorded_at=NOW,
        balance_observed_at=NOW,
        source_sha="a" * 40,
        kill_state="not_tested",
    )

    assert isinstance(receipt, FundedControlReceipt)
    assert receipt.control_contract_version == CONTROL_CONTRACT_VERSION
    assert receipt.credential_lane == FUNDED_CREDENTIAL_LANE
    assert receipt.funding_account == FUNDED_ACCOUNT
    assert receipt.kill_state == "not_tested"
    assert receipt.monthly_effective_spend == Decimal("0")
    assert receipt.monthly_remaining == Decimal("25000")
    assert receipt.reserve_remaining == Decimal("25100")
    assert receipt.run_allowance == Decimal("100")
    assert len(receipt.control_id) == 64
    assert (
        build_funded_control_receipt(
            gate=_gate(),
            recorded_at=NOW,
            balance_observed_at=NOW,
            source_sha="a" * 40,
            kill_state="not_tested",
        )
        == receipt
    )

    changed = build_funded_control_receipt(
        gate=_gate(),
        recorded_at=NOW,
        balance_observed_at=NOW,
        source_sha="b" * 40,
        kill_state="not_tested",
    )
    assert changed.control_id != receipt.control_id


def test_receipt_rejects_formula_or_identity_mutation() -> None:
    receipt = build_funded_control_receipt(
        gate=_gate(),
        recorded_at=NOW,
        balance_observed_at=NOW,
        source_sha="a" * 40,
        kill_state="not_tested",
    )
    values = {field: getattr(receipt, field) for field in receipt.__dataclass_fields__}

    for field, value in (
        ("monthly_effective_spend", Decimal("1")),
        ("run_allowance", Decimal("101")),
        ("credential_lane", "jhb_core"),
        ("funding_account", "jhb_analytics"),
        ("catalog_digest", "b" * 64),
        ("source_sha", "bad"),
    ):
        with pytest.raises(ValueError):
            FundedControlReceipt(**{**values, field: value})


def test_completed_stage_one_does_not_infer_a_passed_kill_state() -> None:
    receipt = build_funded_control_receipt(
        gate=_gate(complete_runs=1),
        recorded_at=NOW,
        balance_observed_at=NOW,
        source_sha="a" * 40,
        kill_state="not_tested",
    )

    assert receipt.kill_state == "not_tested"
    assert receipt.run_allowance == Decimal("100")


class _Credentials:
    service_account_email = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
    quota_project_id = "ogilvy-trends-v2"


class _Job:
    errors = None

    def __init__(self, rows=()):
        self.rows = rows

    def result(self, *, max_results=None, retry=None, job_retry=None):
        return self.rows


class _ResponseLossJob:
    errors = None

    def result(self, *, max_results=None, retry=None, job_retry=None):
        raise TimeoutError("transaction response was lost")


class _ControlClient:
    project = "ogilvy-trends-v2"
    location = "US"
    dataset = "trends_v2_staging_funded"
    writer_identity = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
    _credentials = _Credentials()

    def __init__(
        self,
        receipt: FundedControlReceipt,
        *,
        conflict: bool = False,
        response_loss: bool = False,
    ):
        self.receipt = receipt
        self.conflict = conflict
        self.response_loss = response_loss
        self.calls = []

    def query(self, sql, *, location, job_config, retry=None, job_retry=None):
        self.calls.append((sql, location, job_config, retry, job_retry))
        if job_config.dry_run:
            return _Job()
        if sql.startswith("DECLARE"):
            if self.response_loss:
                return _ResponseLossJob()
            return _Job(
                ({"inserted_count": 0, "unchanged_count": 0, "conflict_count": 1},)
                if self.conflict
                else ({"inserted_count": 1, "unchanged_count": 0, "conflict_count": 0},)
            )
        return _Job(
            (
                self.receipt.__dict__
                if hasattr(self.receipt, "__dict__")
                else {
                    field: getattr(self.receipt, field)
                    for field in self.receipt.__dataclass_fields__
                },
            )
        )


def test_control_writer_dry_runs_inserts_and_reads_back_exactly() -> None:
    receipt = build_funded_control_receipt(
        gate=_gate(),
        recorded_at=NOW,
        balance_observed_at=NOW,
        source_sha="a" * 40,
        kill_state="not_tested",
    )
    client = _ControlClient(receipt)

    result = persist_funded_control_receipt(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging_funded",
        client=client,
        receipt=receipt,
    )

    assert result.inserted_count == 1
    assert result.unchanged_count == 0
    assert result.control_id == receipt.control_id
    assert len(client.calls) == 3
    assert client.calls[0][2].dry_run is True
    assert (
        "INSERT INTO `ogilvy-trends-v2.trends_v2_staging_funded.socialcrawl_funded_control_receipts_v1`"
        in client.calls[1][0]
    )
    assert "UPDATE " not in client.calls[1][0]
    assert "DELETE " not in client.calls[1][0]
    assert "WHERE control_id = @control_id" in client.calls[2][0]


def test_control_writer_reconciles_an_exact_row_after_transaction_response_loss() -> None:
    receipt = build_funded_control_receipt(
        gate=_gate(),
        recorded_at=NOW,
        balance_observed_at=NOW,
        source_sha="a" * 40,
        kill_state="not_tested",
    )
    client = _ControlClient(receipt, response_loss=True)

    result = persist_funded_control_receipt(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging_funded",
        client=client,
        receipt=receipt,
    )

    assert result.control_id == receipt.control_id
    assert result.inserted_count == 0
    assert result.unchanged_count == 1
    assert len(client.calls) == 3
    assert "WHERE control_id = @control_id" in client.calls[2][0]


def test_control_writer_accepts_the_real_client_whose_dataset_is_a_method() -> None:
    # Refused the ninth Wave 1 pilot on staging, 4 Sep 2026: the writer compared
    # getattr(client, "dataset") to the dataset name, and the real BigQuery
    # client exposes dataset() as a method, so every real client failed while
    # the test fakes carried a string attribute. The dataset is validated by
    # the target check; the client is validated by validate_real_client.
    receipt = build_funded_control_receipt(
        gate=_gate(),
        recorded_at=NOW,
        balance_observed_at=NOW,
        source_sha="a" * 40,
        kill_state="not_tested",
    )
    client = _ControlClient(receipt)
    client.dataset = lambda name: name
    persist_funded_control_receipt(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging_funded",
        client=client,
        receipt=receipt,
    )
    assert client.calls


def test_control_writer_refuses_conflict_and_nonexact_target() -> None:
    receipt = build_funded_control_receipt(
        gate=_gate(),
        recorded_at=NOW,
        balance_observed_at=NOW,
        source_sha="a" * 40,
        kill_state="not_tested",
    )
    with pytest.raises(FundedControlConflict):
        persist_funded_control_receipt(
            project="ogilvy-trends-v2",
            dataset="trends_v2_staging_funded",
            client=_ControlClient(receipt, conflict=True),
            receipt=receipt,
        )

    client = _ControlClient(receipt)
    with pytest.raises(ValueError, match="staging target"):
        persist_funded_control_receipt(
            project="ogilvy-trends-v2",
            dataset="trends_v2",
            client=client,
            receipt=receipt,
        )
    assert client.calls == []


class _ReadClient:
    project = "ogilvy-trends-v2"
    location = "US"
    _credentials = type(
        "Credentials",
        (),
        {
            "service_account_email": (
                "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
            )
        },
    )()

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, sql, *, location, job_config):
        self.calls.append((sql, location, job_config))
        return _Job(self.rows)


def _budget_row(**overrides):
    values = {
        "month_start": date(2026, 8, 1),
        "credential_lane": "ogilvy_funded",
        "funding_account": "ogilvy_albert",
        "activation_stage": 1,
        "opening_balance": Decimal("250100"),
        "current_balance": Decimal("250000"),
        "month_opening_balance": Decimal("250100"),
        "monthly_cap": Decimal("25000"),
        "monthly_ledger_debit": Decimal("100"),
        "monthly_balance_delta": Decimal("100"),
        "monthly_effective_spend": Decimal("100"),
        "monthly_remaining": Decimal("24900"),
        "reserve_floor": Decimal("225000"),
        "reserve_remaining": Decimal("25000"),
        "stage_cap": Decimal("100"),
        "run_allowance": Decimal("100"),
        "attribution_state": "complete",
        "kill_state": "passed",
        "ledger_through": NOW,
    }
    values.update(overrides)
    return values


def test_fixed_reader_returns_ready_without_internal_authority_fields() -> None:
    client = _ReadClient([_budget_row()])

    result = read_funded_budget(client=client)

    assert result.state == "ready"
    assert result.run_allowance == Decimal("100")
    assert len(client.calls) == 1
    sql, location, config = client.calls[0]
    assert location == "US"
    assert config.query_parameters == []
    assert "`ogilvy-trends-v2.trends_v2_staging.v_socialcrawl_funded_budget_v1`" in sql
    assert "socialcrawl_credit_ledger_v1" not in sql
    projection = sql.split("FROM `ogilvy-trends-v2", 1)[0]
    for secret_field in ("control_id", "catalog_digest", "metadata_digest", "source_sha"):
        assert secret_field not in projection
    assert "jhb_core" not in sql


def test_reader_returns_unknown_with_null_measurements_on_zero_rows() -> None:
    result = read_funded_budget(client=_ReadClient([]))

    assert result.state == "unknown"
    assert result.current_balance is None
    assert result.monthly_ledger_debit is None
    assert result.run_allowance is None


def test_reader_refuses_non_lp_or_production_identity_before_query() -> None:
    client = _ReadClient([_budget_row()])
    client._credentials = type(
        "Credentials",
        (),
        {"service_account_email": "listening-post@ogilvy-trends-v2.iam.gserviceaccount.com"},
    )()

    with pytest.raises(FundedBudgetReadInvalid, match="identity"):
        read_funded_budget(client=client)
    assert client.calls == []


@pytest.mark.parametrize(
    "rows",
    [
        [_budget_row(), _budget_row()],
    ],
)
def test_reader_refuses_duplicate_rows(rows) -> None:
    with pytest.raises(FundedBudgetReadInvalid):
        read_funded_budget(client=_ReadClient(rows))


@pytest.mark.parametrize(
    "row",
    [
        _budget_row(credential_lane="jhb_core"),
        _budget_row(monthly_effective_spend=Decimal("99")),
        _budget_row(run_allowance=Decimal("101")),
    ],
)
def test_reader_turns_identity_or_formula_disagreement_into_unknown(row) -> None:
    result = read_funded_budget(client=_ReadClient([row]))
    assert result.state == "unknown"
    assert result.current_balance is None
