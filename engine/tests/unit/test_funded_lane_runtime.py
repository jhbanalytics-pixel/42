"""End-to-end local orchestration for the funded SocialCrawl lane."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import funded_lane_runtime
from src.analysis.open_intelligence.funded_lane_reader import FundedLaneLedgerSnapshot
from src.analysis.open_intelligence.funded_lane_runtime import (
    FundedLaneActivationBlocked,
    _wave1_identity as real_wave1_identity,
    prepare_funded_lane,
    read_latest_funded_control_state,
)
from src.ingestion.connectors.socialcrawl import PHASE_ORDER, SocialCrawlPhaseUsage

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
ROUTE_DIGEST = "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6"
METADATA_DIGEST = "501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a"


def endpoint(path="/v1/tiktok/profile", platform="tiktok"):
    return {
        "id": f"{platform}/{path.rsplit('/', 1)[-1]}",
        "path": path,
        "method": "GET",
        "platform": platform,
        "resource": path.rsplit("/", 1)[-1],
        "summary": "Reads public data.",
        "credits": 1,
        "credits_label": "1 (standard)",
        "archetype": "Author",
        "required_params": ["handle"],
        "one_of": [],
        "optional_params": [],
        "paginated": False,
        "metered": False,
        "cache_ttl_seconds": 900,
        "delivery": None,
        "docs_url": f"https://www.socialcrawl.dev/platforms/{platform}",
    }


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class Session:
    def __init__(self, balance=250_100):
        self.headers = {}
        self.calls = []
        self.balance = balance

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        if url.endswith("/credits/balance"):
            return Response(
                {
                    "success": True,
                    "endpoint": "/v1/credits/balance",
                    "data": {"balance": self.balance, "recent_deductions": 0},
                    "credits_used": 0,
                    "credits_remaining": self.balance,
                    "request_id": "balance-receipt",
                }
            )
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture(autouse=True)
def _wave1_identity_from_fake_catalog(monkeypatch):
    # The fakes carry the approved digests directly; the real helper is proven
    # against the committed catalog in test_source_lab_inventory.
    monkeypatch.setattr(
        funded_lane_runtime,
        "_wave1_identity",
        lambda catalog: (catalog.catalog_digest, catalog.normalized_content_digest),
    )


def catalog_snapshot():
    return SimpleNamespace(
        platform_count=64,
        route_count=556,
        social_platform_count=30,
        catalog_digest=ROUTE_DIGEST,
        normalized_content_digest=METADATA_DIGEST,
        credits_used=Decimal("0"),
    )


def ledger(**overrides):
    values = {
        "month_start": date(2026, 8, 1),
        "monthly_ledger_debit": Decimal("0"),
        "monthly_vendor_reported": Decimal("0"),
        "month_opening_balance": None,
        "unreconciled_execution_ids": (),
        "consecutive_complete_runs": 0,
        "runs_today": 0,
    }
    values.update(overrides)
    return FundedLaneLedgerSnapshot(**values)


def prepare(**overrides):
    written = []
    controls = []
    values = {
        "client": SimpleNamespace(project="ogilvy-trends-v2", location="US"),
        "dataset": "trends_v2_staging_funded",
        "run_id": "run_001",
        "execution_id": "execution_001",
        "as_of": NOW,
        "activation_stage": 1,
        "environment": "staging",
        "secret_reader": lambda secret_id: "funded_key",
        "session": Session(),
        "catalog_reader": lambda session, checked_at: catalog_snapshot(),
        "ledger_reader": lambda **kwargs: ledger(),
        "control_state_reader": lambda **kwargs: None,
        "ledger_writer": lambda row: written.append(row),
        "control_writer": lambda receipt: controls.append(receipt),
        "source_sha": "a" * 40,
        "clock": lambda: NOW,
    }
    values.update(overrides)
    runtime = prepare_funded_lane(**values)
    values["_controls"] = controls
    return runtime, written, values


def all_phase_usage(*, search_budget=100, search_vendor=100):
    rows = []
    for market in ("za", "ng", "ke"):
        for phase in PHASE_ORDER:
            budget = search_budget if (market, phase) == ("za", "search") else 0
            vendor = search_vendor if (market, phase) == ("za", "search") else 0
            rows.append(
                SocialCrawlPhaseUsage(
                    market=market,
                    phase=phase,
                    calls=1 if budget or vendor else 0,
                    budget_debit_credits=budget,
                    vendor_reported_credits=vendor,
                )
            )
    return rows


def test_prepare_uses_funded_secret_free_receipts_and_writes_preflight() -> None:
    secret_calls = []
    session = Session()
    runtime, written, values = prepare(
        secret_reader=lambda secret_id: secret_calls.append(secret_id) or "funded_key",
        session=session,
    )

    assert secret_calls == ["SOCIALCRAWL_OGILVY_API_KEY"]
    assert session.headers["x-api-key"] == "funded_key"
    assert session.calls == [("https://www.socialcrawl.dev/v1/credits/balance", 60)]
    assert runtime.run_allowance == 100
    assert len(values["_controls"]) == 1
    assert values["_controls"][0].kill_state == "not_tested"
    assert len(written) == 1
    row = written[0]
    assert row["phase"] == row["event_type"] == "preflight"
    assert row["budget_debit_credits"] == row["vendor_reported_credits"] == Decimal("0")
    assert row["balance_observed"] == Decimal("250100")
    assert row["catalog_digest"] == ROUTE_DIGEST
    assert row["metadata_digest"] == METADATA_DIGEST
    assert "funded_key" not in repr(runtime)
    assert "funded_key" not in repr(row)


@pytest.mark.parametrize("kill_state", ["not_tested", "failed", "killed"])
def test_prior_nonpassing_control_blocks_wave1_before_secret_or_vendor(
    kill_state: str,
) -> None:
    from tests.unit.test_source_wave1_contract import _wave1_capability

    secret_calls = []
    session = Session()
    with pytest.raises(FundedLaneActivationBlocked, match="prior funded control"):
        prepare(
            stage_name="stage_1_wave_1",
            execution_capability=_wave1_capability(),
            control_state_reader=lambda **_kwargs: kill_state,
            secret_reader=lambda _secret_id: secret_calls.append("secret") or "funded_key",
            session=session,
        )

    assert secret_calls == []
    assert session.calls == []


def test_latest_funded_control_reader_is_bounded_and_returns_exact_state() -> None:
    class Job:
        errors = None

        def result(self, *, max_results, retry, job_retry):
            assert (max_results, retry, job_retry) == (2, None, None)
            return (
                {
                    "kill_state": "killed",
                    "recorded_at": NOW - timedelta(minutes=1),
                },
            )

    class Client:
        project = "ogilvy-trends-v2"
        location = "US"

        def __init__(self):
            self.calls = []

        def query(self, sql, **kwargs):
            self.calls.append((sql, kwargs))
            return Job()

    client = Client()
    state = read_latest_funded_control_state(
        client=client,
        dataset="trends_v2_staging_funded",
        as_of=NOW,
    )

    assert state == "killed"
    assert len(client.calls) == 1
    sql, kwargs = client.calls[0]
    assert "socialcrawl_funded_control_receipts_v1" in sql
    assert "SELECT kill_state, recorded_at, source_sha FROM" in sql
    assert "ORDER BY recorded_at DESC, control_id DESC LIMIT 1" in sql
    assert kwargs["retry"] is kwargs["job_retry"] is None


@pytest.mark.parametrize(
    ("kill_state", "control_sha", "source_sha", "expected"),
    [
        ("failed", "a" * 40, "b" * 40, None),
        ("failed", "a" * 40, "a" * 40, "failed"),
        ("failed", "a" * 40, None, "failed"),
        ("killed", "a" * 40, "b" * 40, "killed"),
        ("passed", "a" * 40, "b" * 40, "passed"),
    ],
)
def test_failed_control_blocks_only_the_source_that_failed(
    kill_state, control_sha, source_sha, expected
) -> None:
    # Attempt 14 on staging, 4 Sep 2026: the run closed on the ledger, the
    # source value writer refused, the control was written failed, and every
    # later run refused on it. The fix is a new commit on the lane, so a failed
    # control from another source is void; a killed control blocks any source.
    class Job:
        errors = None

        def result(self, *, max_results, retry, job_retry):
            return (
                {
                    "kill_state": kill_state,
                    "recorded_at": NOW - timedelta(minutes=1),
                    "source_sha": control_sha,
                },
            )

    class Client:
        project = "ogilvy-trends-v2"
        location = "US"

        def query(self, sql, **kwargs):
            return Job()

    state = read_latest_funded_control_state(
        client=Client(),
        dataset="trends_v2_staging_funded",
        as_of=NOW,
        source_sha=source_sha,
    )
    assert state == expected


def test_prepare_hands_the_admitted_source_to_the_control_reader() -> None:
    from tests.unit.test_source_wave1_contract import _wave1_capability

    capability = _wave1_capability()
    seen = {}

    def reader(**kwargs):
        seen.update(kwargs)
        return None

    prepare(
        stage_name="stage_1_wave_1",
        execution_capability=capability,
        control_state_reader=reader,
    )
    assert seen["source_sha"] == capability.source_sha


@pytest.mark.parametrize(("phase_rows", "expected"), [(0, None), (2, "not_tested")])
def test_not_tested_control_without_phase_rows_is_void(phase_rows, expected) -> None:
    # Attempt 12 on staging, 4 Sep 2026: the control receipt written at
    # preflight stayed not_tested because the execution stopped before any
    # phase, and every later run refused on it. Without ledger phase rows since
    # that control it is void; with phase rows it still blocks.
    class Job:
        errors = None

        def __init__(self, rows):
            self.rows = rows

        def result(self, *, max_results, retry, job_retry):
            assert (max_results, retry, job_retry) == (2, None, None)
            return self.rows

    class Client:
        project = "ogilvy-trends-v2"
        location = "US"

        def __init__(self):
            self.calls = []

        def query(self, sql, **kwargs):
            self.calls.append((sql, kwargs))
            if "socialcrawl_credit_ledger_v1" in sql:
                return Job(({"phase_rows": phase_rows},))
            return Job(({"kill_state": "not_tested", "recorded_at": NOW - timedelta(minutes=5)},))

    client = Client()
    state = read_latest_funded_control_state(
        client=client, dataset="trends_v2_staging_funded", as_of=NOW
    )
    assert state == expected
    assert len(client.calls) == 2
    ledger_sql = client.calls[1][0]
    assert "event_type = 'phase_close'" in ledger_sql
    assert "recorded_at >= @since AND recorded_at < @as_of" in ledger_sql


def test_new_month_empty_ledger_uses_current_balance_as_month_opening() -> None:
    runtime, written, _ = prepare(
        as_of=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        session=Session(balance=230_000),
        ledger_reader=lambda **kwargs: ledger(
            month_start=date(2026, 9, 1),
            month_opening_balance=None,
        ),
    )

    assert runtime.run_allowance == 100
    assert runtime.gate.monthly_effective_spend == Decimal("0")
    assert written[0]["month_opening_balance"] == Decimal("230000")
    assert written[0]["monthly_balance_delta_before"] == Decimal("0")


def test_each_paid_call_rechecks_balance_and_ledger_before_authority() -> None:
    session = Session()
    reads = []

    def ledger_read(**kwargs):
        reads.append(kwargs["as_of"])
        return ledger(
            month_opening_balance=Decimal("250100"),
            unreconciled_execution_ids=("execution_001",) if len(reads) > 1 else (),
        )

    runtime, _, _ = prepare(
        session=session,
        ledger_reader=ledger_read,
    )

    runtime.assert_call_authority(25)

    assert len(session.calls) == 2
    assert len(reads) == 2


def test_paid_call_recheck_fails_closed_on_fresh_reserve_or_projected_cap() -> None:
    session = Session()
    runtime, _, _ = prepare(session=session)
    session.balance = 225_000
    with pytest.raises(FundedLaneActivationBlocked, match="funded call blocked"):
        runtime.assert_call_authority(25)

    runtime, _, _ = prepare()
    with pytest.raises(FundedLaneActivationBlocked, match="funded call blocked"):
        runtime.assert_call_authority(101)


def test_wave1_complete_proof_writes_a_later_passed_control_receipt() -> None:
    from src.analysis.open_intelligence.funded_lane import (
        WAVE1_PHASE_CAPS,
        WAVE1_STAGE,
    )
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    from tests.unit.test_source_wave1_contract import _wave1_capability

    now = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
        source_sha=None,
        clock=lambda: now + timedelta(minutes=1),
    )
    runtime._source_value_writer = lambda _values: None
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    source_value = evaluate_wave1_source_value(
        unique_observations=1,
        marginal_candidates=1,
        marginal_evidence=1,
    )
    routes = {
        "/v1/tiktok/song",
        "/v1/tiktok/song/videos",
        "/v1/instagram/music/trending",
        "/v1/instagram/audio/reels",
        "/v1/instagram/search/reels",
        "/v1/youtube/shorts/trending",
        "/v1/youtube/video/comments",
        "/v1/reddit/post/comments",
    }
    gdelt_proof = SimpleNamespace(
        query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
        query_bytes_processed=(("events", 100), ("gcam", 200)),
        persistence=SimpleNamespace(complete=True),
    )

    runtime.close(
        post_balance=Decimal("250100"),
        recorded_at=now + timedelta(minutes=2),
        source_value_results=dict.fromkeys(routes, source_value),
        gdelt_runtime_proof=gdelt_proof,
    )

    controls = values["_controls"]
    assert [receipt.kill_state for receipt in controls] == ["not_tested", "passed"]
    assert controls[-1].recorded_at == now + timedelta(minutes=2)


def test_wave1_post_run_control_distinguishes_failed_and_killed() -> None:
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    from tests.unit.test_source_wave1_contract import _wave1_capability

    routes = {
        "/v1/tiktok/song",
        "/v1/tiktok/song/videos",
        "/v1/instagram/music/trending",
        "/v1/instagram/audio/reels",
        "/v1/instagram/search/reels",
        "/v1/youtube/shorts/trending",
        "/v1/youtube/video/comments",
        "/v1/reddit/post/comments",
    }
    active = evaluate_wave1_source_value(
        unique_observations=1, marginal_candidates=1, marginal_evidence=1
    )
    rejected = evaluate_wave1_source_value(
        unique_observations=0, marginal_candidates=0, marginal_evidence=0
    )
    proof = SimpleNamespace(
        query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
        query_bytes_processed=(("events", 100), ("gcam", 200)),
        persistence=SimpleNamespace(complete=True),
    )

    def runtime_case():
        runtime, _written, values = prepare(
            stage_name=WAVE1_STAGE,
            execution_capability=_wave1_capability(),
        )
        runtime._source_value_writer = lambda _values: None
        for phase in WAVE1_PHASE_CAPS:
            runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
        return runtime, values

    failed_runtime, failed_values = runtime_case()
    failed_sources = dict.fromkeys(routes, active)
    failed_sources["/v1/tiktok/song"] = rejected
    with pytest.raises(FundedLaneActivationBlocked, match="permanently_rejected"):
        failed_runtime.close(
            post_balance=Decimal("250100"),
            recorded_at=NOW + timedelta(minutes=2),
            source_value_results=failed_sources,
            gdelt_runtime_proof=proof,
        )
    assert failed_values["_controls"][-1].kill_state == "failed"

    killed_runtime, killed_values = runtime_case()
    killed_runtime.close(
        post_balance=Decimal("250099"),
        recorded_at=NOW + timedelta(minutes=2),
        source_value_results=dict.fromkeys(routes, active),
        gdelt_runtime_proof=proof,
    )
    assert killed_values["_controls"][-1].kill_state == "killed"


def test_wave1_close_without_gdelt_proof_writes_failed_control_and_refuses() -> None:
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    from tests.unit.test_source_wave1_contract import _wave1_capability

    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
    )
    runtime._source_value_writer = lambda _values: None
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    active = evaluate_wave1_source_value(
        unique_observations=1, marginal_candidates=1, marginal_evidence=1
    )
    routes = {
        "/v1/tiktok/song",
        "/v1/tiktok/song/videos",
        "/v1/instagram/music/trending",
        "/v1/instagram/audio/reels",
        "/v1/instagram/search/reels",
        "/v1/youtube/shorts/trending",
        "/v1/youtube/video/comments",
        "/v1/reddit/post/comments",
    }

    with pytest.raises(FundedLaneActivationBlocked, match="GDELT proof"):
        runtime.close(
            post_balance=Decimal("250100"),
            recorded_at=NOW + timedelta(minutes=2),
            source_value_results=dict.fromkeys(routes, active),
        )

    assert values["_controls"][-1].kill_state == "failed"


def test_wave1_vendor_charge_below_the_quoted_debit_is_a_held_control() -> None:
    # Attempt 14 on staging, 4 Sep 2026: two Instagram calls, 10 credits quoted,
    # 5 reported by the vendor and 5 gone from the balance. The ledger over-counts
    # and nothing leaks, so the control passed; only a gap or a breaker fails it.
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    from tests.unit.test_source_wave1_contract import _wave1_capability

    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
    )
    runtime._source_value_writer = lambda _values: None
    phases = list(WAVE1_PHASE_CAPS)
    runtime.phase_close(Wave1PhaseUsage(None, phases[0], 2, 10, 5))
    for phase in phases[1:]:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    active = evaluate_wave1_source_value(
        unique_observations=1, marginal_candidates=1, marginal_evidence=1
    )
    routes = {
        "/v1/tiktok/song",
        "/v1/tiktok/song/videos",
        "/v1/instagram/music/trending",
        "/v1/instagram/audio/reels",
        "/v1/instagram/search/reels",
        "/v1/youtube/shorts/trending",
        "/v1/youtube/video/comments",
        "/v1/reddit/post/comments",
    }
    proof = SimpleNamespace(
        query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
        query_bytes_processed=(("events", 100), ("gcam", 200)),
        persistence=SimpleNamespace(complete=True),
    )

    receipt = runtime.close(
        post_balance=Decimal("250095"),
        recorded_at=NOW + timedelta(minutes=2),
        source_value_results=dict.fromkeys(routes, active),
        gdelt_runtime_proof=proof,
    )

    assert receipt.attribution_state == "conservative"
    assert values["_controls"][-1].kill_state == "passed"
    assert values["_controls"][-1].consecutive_complete_runs == 0


def test_wave1_close_hands_the_writer_the_close_verdict_and_keeps_the_values() -> None:
    # Ruled 5 Sep 2026: the pilot never writes Source Lab. The writer ledgers the
    # measured values in the funded dataset with the verdict the durable result
    # records, read off the runtime at close.
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage
    from src.analysis.open_intelligence.funded_source_values import wave1_close_succeeded
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    from tests.unit.test_source_wave1_contract import _wave1_capability

    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
    )
    seen = []
    runtime._source_value_writer = lambda measured: seen.append(
        (
            dict(measured),
            runtime.close_receipt,
            runtime.close_recorded_at,
            runtime.close_verdict,
        )
    )
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    active = evaluate_wave1_source_value(
        unique_observations=1, marginal_candidates=1, marginal_evidence=1
    )
    routes = {"/v1/instagram/search/reels", "/v1/youtube/video/comments"}
    proof = SimpleNamespace(
        query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
        query_bytes_processed=(("events", 100), ("gcam", 200)),
        persistence=SimpleNamespace(complete=True),
    )

    receipt = runtime.close(
        post_balance=Decimal("250100"),
        recorded_at=NOW + timedelta(minutes=2),
        source_value_results=dict.fromkeys(routes, active),
        gdelt_runtime_proof=proof,
    )

    assert receipt.source_values_state == "persisted"
    assert set(receipt.source_values) == routes
    assert len(seen) == 1
    measured, close_receipt, close_recorded_at, verdict = seen[0]
    assert set(measured) == routes
    assert close_receipt.attribution_state == receipt.attribution_state
    assert close_receipt.source_values == receipt.source_values
    assert close_recorded_at == NOW + timedelta(minutes=2)
    assert verdict == "succeeded"
    assert wave1_close_succeeded(close_receipt) is True
    assert values["_controls"][-1].kill_state == "passed"
    assert runtime.retry_source_value_persistence() is False


def test_wave1_close_verdict_fails_on_a_fault_close_already_holds() -> None:
    # Refuter on 9b37a58: an incomplete GDELT proof (or a rejected route) sets the
    # activation error before the writer runs, and the durable result records
    # failed; the ledgered verdict must say the same or the snapshot would carry
    # a failed pilot as succeeded.
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import (
        FundedLaneActivationBlocked,
        Wave1PhaseUsage,
    )
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    from tests.unit.test_source_wave1_contract import _wave1_capability

    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
    )
    seen = []
    runtime._source_value_writer = lambda measured: seen.append(runtime.close_verdict)
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    active = evaluate_wave1_source_value(
        unique_observations=1, marginal_candidates=1, marginal_evidence=1
    )

    with pytest.raises(FundedLaneActivationBlocked, match="GDELT proof is incomplete"):
        runtime.close(
            post_balance=Decimal("250100"),
            recorded_at=NOW + timedelta(minutes=2),
            source_value_results={"/v1/instagram/search/reels": active},
            gdelt_runtime_proof=None,
        )

    assert seen == ["failed"]
    assert values["_controls"][-1].kill_state == "failed"


def test_wave1_writer_ledgers_the_funded_rows_with_the_close_verdict(monkeypatch) -> None:
    from src.analysis.open_intelligence.funded_lane import (
        APPROVED_CATALOG_DIGEST,
        APPROVED_METADATA_DIGEST,
    )
    from src.analysis.open_intelligence.funded_lane_runtime import (
        FundedLaneActivationBlocked,
        FundedRunCloseReceipt,
        make_wave1_source_value_writer,
    )
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    captured = []
    monkeypatch.setattr(
        funded_lane_runtime,
        "persist_wave1_source_value_rows",
        lambda **kwargs: captured.append(kwargs),
    )
    held = FundedRunCloseReceipt(
        attribution_state="conservative",
        calls=8,
        budget_debit_credits=Decimal("8"),
        vendor_reported_credits=Decimal("7"),
        balance_delta=Decimal("31"),
        attribution_gap_credits=Decimal("0"),
        run_balance_delta=Decimal("7"),
    )
    values = {
        "/v1/youtube/video/comments": evaluate_wave1_source_value(
            unique_observations=335, marginal_candidates=4, marginal_evidence=335
        ),
        "/v1/instagram/search/reels": evaluate_wave1_source_value(
            unique_observations=0, marginal_candidates=0, marginal_evidence=0
        ),
    }
    runtime = SimpleNamespace(
        execution_id="execution_wave1_ledger",
        run_id="run_wave1_ledger",
        close_receipt=held,
        close_recorded_at=NOW,
        close_vendor_overage=False,
        close_verdict="succeeded",
        _source_sha="a" * 40,
        gate=SimpleNamespace(
            catalog_digest=APPROVED_CATALOG_DIGEST, metadata_digest=APPROVED_METADATA_DIGEST
        ),
        _clock=lambda: NOW + timedelta(seconds=1),
    )
    client = object()
    writer = make_wave1_source_value_writer(
        client=client, dataset="trends_v2_staging_funded", runtime=runtime
    )

    writer(values)

    assert len(captured) == 1
    call = captured[0]
    assert (call["project"], call["dataset"], call["client"]) == (
        "ogilvy-trends-v2",
        "trends_v2_staging_funded",
        client,
    )
    rows = call["rows"]
    assert [row.route_path for row in rows] == [
        "/v1/instagram/search/reels",
        "/v1/youtube/video/comments",
    ]
    assert {row.close_verdict for row in rows} == {"succeeded"}
    assert {row.execution_id for row in rows} == {"execution_wave1_ledger"}
    assert {row.recorded_at for row in rows} == {NOW}
    assert {row.created_at for row in rows} == {NOW + timedelta(seconds=1)}
    assert rows[1].unique_observations == 335
    assert rows[1].state == "passed"
    assert rows[0].state == "permanently_rejected"
    assert rows[0].reason == "zero_unique_observations"

    runtime.close_verdict = "failed"
    writer(values)
    assert {row.close_verdict for row in captured[1]["rows"]} == {"failed"}

    runtime.close_verdict = "maybe"
    with pytest.raises(FundedLaneActivationBlocked, match="close receipt is unavailable"):
        writer(values)
    runtime.close_verdict = "succeeded"
    runtime.close_receipt = None
    with pytest.raises(FundedLaneActivationBlocked, match="close receipt is unavailable"):
        writer(values)
    assert len(captured) == 2


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ({"/v1/instagram/search/reels": "active"}, None),
        (
            {"/v1/instagram/search/reels": "active", "/v1/reddit/post/comments": "zero"},
            "permanently_rejected",
        ),
        ({"/v1/instagram/search/reels": "zero"}, "permanently_rejected"),
        ({}, "unavailable"),
        ({"/v1/instagram/search/reels": "active", "/v1/not/a/route": "active"}, "unavailable"),
    ],
)
def test_wave1_close_measures_only_the_seeded_routes(results, expected) -> None:
    # Attempt 17 on staging, 4 Sep 2026: every route the seeds could not fill
    # counted as zero observations and blocked the close. A measured subset
    # closes when one route carries value and none of the measured is rejected.
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    from tests.unit.test_source_wave1_contract import _wave1_capability

    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
    )
    runtime._source_value_writer = lambda _values: None
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    verdicts = {
        "active": evaluate_wave1_source_value(
            unique_observations=1, marginal_candidates=1, marginal_evidence=1
        ),
        "zero": evaluate_wave1_source_value(
            unique_observations=0, marginal_candidates=0, marginal_evidence=0
        ),
    }
    proof = SimpleNamespace(
        query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
        query_bytes_processed=(("events", 100), ("gcam", 200)),
        persistence=SimpleNamespace(complete=True),
    )
    kwargs = {
        "post_balance": Decimal("250100"),
        "recorded_at": NOW + timedelta(minutes=2),
        "source_value_results": {route: verdicts[name] for route, name in results.items()},
        "gdelt_runtime_proof": proof,
    }
    if expected is None:
        receipt = runtime.close(**kwargs)
        assert set(receipt.source_values) == {"/v1/instagram/search/reels"}
        assert values["_controls"][-1].kill_state == "passed"
    else:
        with pytest.raises(FundedLaneActivationBlocked, match=expected):
            runtime.close(**kwargs)
        assert values["_controls"][-1].kill_state == "failed"


def test_wave1_source_value_persistence_failure_still_writes_failed_control() -> None:
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    from tests.unit.test_source_wave1_contract import _wave1_capability

    runtime, _written, values = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
    )
    runtime._source_value_writer = lambda _values: (_ for _ in ()).throw(
        RuntimeError("source value write failed")
    )
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    active = evaluate_wave1_source_value(
        unique_observations=1, marginal_candidates=1, marginal_evidence=1
    )
    routes = {
        "/v1/tiktok/song",
        "/v1/tiktok/song/videos",
        "/v1/instagram/music/trending",
        "/v1/instagram/audio/reels",
        "/v1/instagram/search/reels",
        "/v1/youtube/shorts/trending",
        "/v1/youtube/video/comments",
        "/v1/reddit/post/comments",
    }
    proof = SimpleNamespace(
        query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
        query_bytes_processed=(("events", 100), ("gcam", 200)),
        persistence=SimpleNamespace(complete=True),
    )

    with pytest.raises(FundedLaneActivationBlocked, match="source value persistence"):
        runtime.close(
            post_balance=Decimal("250100"),
            recorded_at=NOW + timedelta(minutes=2),
            source_value_results=dict.fromkeys(routes, active),
            gdelt_runtime_proof=proof,
        )

    assert values["_controls"][-1].kill_state == "failed"


def test_all_phase_rows_and_exact_post_balance_write_one_complete_run_close() -> None:
    runtime, written, _ = prepare()
    for usage in all_phase_usage():
        runtime.phase_close(usage)

    receipt = runtime.close(post_balance=Decimal("250000"), recorded_at=NOW + timedelta(minutes=5))

    assert receipt.attribution_state == "complete"
    assert receipt.budget_debit_credits == Decimal("100")
    assert receipt.vendor_reported_credits == Decimal("100")
    assert receipt.balance_delta == Decimal("100")
    assert len(written) == 23
    assert sum(row["event_type"] == "phase_close" for row in written) == 21
    assert written[-1]["event_type"] == "run_close"
    assert written[-1]["budget_debit_credits"] == Decimal("100")


def test_hidden_balance_spend_writes_gap_before_gap_detected_run_close() -> None:
    runtime, written, _ = prepare()
    for usage in all_phase_usage():
        runtime.phase_close(usage)

    receipt = runtime.close(post_balance=Decimal("249990"), recorded_at=NOW + timedelta(minutes=5))

    assert receipt.attribution_state == "gap_detected"
    assert receipt.attribution_gap_credits == Decimal("10")
    assert [row["event_type"] for row in written[-2:]] == ["attribution_gap", "run_close"]
    assert written[-2]["budget_debit_credits"] == Decimal("10")
    assert written[-2]["vendor_reported_credits"] == Decimal("0")


def test_conservative_transport_debit_is_not_reported_as_vendor_spend() -> None:
    runtime, written, _ = prepare()
    for usage in all_phase_usage(search_budget=100, search_vendor=90):
        runtime.phase_close(usage)

    receipt = runtime.close(post_balance=Decimal("250010"), recorded_at=NOW + timedelta(minutes=5))

    assert receipt.attribution_state == "conservative"
    assert receipt.budget_debit_credits == Decimal("100")
    assert receipt.vendor_reported_credits == Decimal("90")
    assert written[-1]["attribution_state"] == "conservative"


def test_missing_or_duplicate_phase_close_refuses_run_close() -> None:
    runtime, written, _ = prepare()
    usages = all_phase_usage()
    runtime.phase_close(usages[0])
    with pytest.raises(ValueError, match="duplicate phase close"):
        runtime.phase_close(usages[0])
    with pytest.raises(ValueError, match="21 phase close"):
        runtime.close(post_balance=Decimal("250100"), recorded_at=NOW)
    assert all(row["event_type"] != "run_close" for row in written)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"environment": "prod"}, "staging"),
        ({"environment": "dev"}, "staging"),
        ({"secret_reader": lambda secret_id: ""}, "secret"),
        ({"session": Session(balance=225_000)}, "reserve"),
        ({"activation_stage": 0}, "stage_zero"),
        (
            {
                "catalog_reader": lambda session, checked_at: SimpleNamespace(
                    **{**catalog_snapshot().__dict__, "catalog_digest": "f" * 64}
                )
            },
            "catalog",
        ),
    ],
)
def test_preflight_refuses_before_ledger_writer_or_runtime(overrides, message) -> None:
    written = []
    controls = []
    with pytest.raises(FundedLaneActivationBlocked, match=message):
        prepare(
            **overrides,
            ledger_writer=lambda row: written.append(row),
            control_writer=lambda receipt: controls.append(receipt),
        )
    assert written == []
    if message in {"reserve", "stage_zero"}:
        assert len(controls) == 1
    else:
        assert controls == []


@pytest.mark.parametrize("mutation", ["range", "metered", "control_character"])
def test_real_live_route_identity_refuses_price_drift_before_control_or_paid_http(
    monkeypatch, mutation
) -> None:
    monkeypatch.setattr(funded_lane_runtime, "_wave1_identity", real_wave1_identity)
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "source_lab"
        / "wave1_metered_catalog_rows.json"
    )
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    baseline = funded_lane_runtime.normalize_catalog(
        raw,
        checked_at=NOW,
        expected_platforms=4,
        expected_endpoints=9,
    )
    assert real_wave1_identity(baseline) == (ROUTE_DIGEST, METADATA_DIGEST)
    reels = next(
        row for row in raw["data"]["endpoints"]
        if row["path"] == "/v1/instagram/search/reels"
    )
    if mutation == "range":
        assert "1-9 (metered)" in reels["credits_label"]
        reels["credits_label"] = reels["credits_label"].replace(
            "1-9 (metered)", "1-8 (metered)", 1
        )
    elif mutation == "metered":
        reels["metered"] = False
    else:
        reels["credits_label"] += "\x1f"

    class CatalogSession(Session):
        def get(self, url, *, timeout):
            if url.endswith("/v1/utility/endpoints"):
                self.calls.append((url, timeout))
                return Response(raw)
            return super().get(url, timeout=timeout)

    session = CatalogSession()
    ledger_reads = []
    ledger_writes = []
    controls = []
    expected_error = (
        "zero-credit funded preflight failed"
        if mutation == "control_character"
        else "catalog identity is not approved"
    )
    with pytest.raises(FundedLaneActivationBlocked, match=expected_error):
        prepare(
            session=session,
            catalog_reader=funded_lane_runtime._read_catalog,
            ledger_reader=lambda **kwargs: ledger_reads.append(kwargs),
            ledger_writer=lambda row: ledger_writes.append(row),
            control_writer=lambda receipt: controls.append(receipt),
        )
    assert session.calls == [
        ("https://www.socialcrawl.dev/v1/credits/balance", 60),
        ("https://www.socialcrawl.dev/v1/utility/endpoints", 60),
    ]
    assert ledger_reads == ledger_writes == controls == []


def test_runtime_refuses_second_close_and_non_decimal_balance() -> None:
    runtime, _, _ = prepare()
    for usage in all_phase_usage():
        runtime.phase_close(usage)
    runtime.close(post_balance=Decimal("250000"), recorded_at=NOW)
    with pytest.raises(ValueError, match="already closed"):
        runtime.close(post_balance=Decimal("250000"), recorded_at=NOW)
    with pytest.raises(ValueError):
        prepare()[0].close(post_balance=250_000.0, recorded_at=NOW)


def account_debit(written, *, opening=Decimal("250100")):
    close = written[-1]
    assert close["event_type"] == "run_close"
    return opening - close["balance_observed"]


def test_concurrent_incremental_debit_is_held_as_unknown_on_the_shared_account() -> None:
    # The baseline run closes while another execution's incremental request has
    # debited the same account by 30 credits the baseline ledger never saw.
    runtime, written, _ = prepare()
    for usage in all_phase_usage():
        runtime.phase_close(usage)
    receipt = runtime.close(post_balance=Decimal("249970"), recorded_at=NOW + timedelta(minutes=5))
    assert receipt.attribution_state == "gap_detected"
    attributable_completed = receipt.budget_debit_credits
    held_unknown = receipt.attribution_gap_credits
    assert account_debit(written) == attributable_completed + held_unknown == Decimal("130")
    assert (attributable_completed, held_unknown) == (Decimal("100"), Decimal("30"))
    assert receipt.vendor_reported_credits == Decimal("100")
    assert receipt.run_balance_delta == Decimal("130")
    gap_row, close_row = written[-2], written[-1]
    assert (gap_row["phase"], gap_row["event_type"]) == ("unattributed", "attribution_gap")
    assert (gap_row["budget_debit_credits"], gap_row["vendor_reported_credits"]) == (
        Decimal("30"),
        Decimal("0"),
    )
    assert close_row["monthly_ledger_debit_before"] == close_row["monthly_balance_delta_before"]
    assert close_row["monthly_ledger_debit_before"] == Decimal("130")
    for field in ("budget_debit_credits", "vendor_reported_credits", "balance_observed"):
        assert isinstance(close_row[field], Decimal), field


@pytest.mark.parametrize(
    ("post_balance", "held_charged", "ledger_over_hold"),
    [
        (Decimal("250000"), Decimal("10"), Decimal("0")),
        (Decimal("250010"), Decimal("0"), Decimal("10")),
    ],
)
def test_unknown_vendor_completion_is_held_in_budget_and_never_as_vendor_credit(
    post_balance, held_charged, ledger_over_hold
) -> None:
    # One call lost its response after the request went out: budget 100 is
    # held, the vendor confirmed 90, so 10 credits are a held unknown. The
    # package equation, account debit equals attributable completed calls plus
    # held unknowns, is asserted literally when the vendor charged the unknown
    # call; when it did not, the same 10 sits in the ledger as the conservative
    # over hold and the account debit is the confirmed calls alone.
    runtime, written, _ = prepare()
    for usage in all_phase_usage(search_budget=100, search_vendor=90):
        runtime.phase_close(usage)
    receipt = runtime.close(post_balance=post_balance, recorded_at=NOW + timedelta(minutes=5))
    attributable_completed = receipt.vendor_reported_credits
    held_unknowns = receipt.budget_debit_credits - receipt.vendor_reported_credits
    assert (attributable_completed, held_unknowns) == (Decimal("90"), Decimal("10"))
    assert receipt.attribution_state == "conservative"
    assert receipt.attribution_gap_credits == Decimal("0")
    debit = account_debit(written)
    assert debit == attributable_completed + held_charged
    assert debit + ledger_over_hold == attributable_completed + held_unknowns
    if ledger_over_hold == 0:
        assert debit == attributable_completed + held_unknowns
    close_row = written[-1]
    assert close_row["monthly_ledger_debit_before"] == attributable_completed + held_unknowns
    assert close_row["monthly_ledger_debit_before"] - close_row["monthly_balance_delta_before"] == (
        ledger_over_hold
    )
    assert close_row["vendor_reported_credits"] == Decimal("90")


def wave1_close_kwargs(post_balance):
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    return {
        "post_balance": post_balance,
        "recorded_at": NOW + timedelta(minutes=9),
        "source_value_results": {
            "/v1/instagram/search/reels": evaluate_wave1_source_value(
                unique_observations=3, marginal_candidates=2, marginal_evidence=1
            )
        },
        "gdelt_runtime_proof": SimpleNamespace(
            query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
            query_bytes_processed=(("events", 100), ("gcam", 200)),
            persistence=SimpleNamespace(complete=True),
        ),
    }


def test_baseline_then_incremental_requests_share_one_account_month() -> None:
    from src.analysis.open_intelligence.funded_lane import WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage

    from tests.unit.test_source_wave1_contract import _wave1_capability

    baseline, baseline_rows, _ = prepare()
    for usage in all_phase_usage():
        baseline.phase_close(usage)
    first = baseline.close(post_balance=Decimal("250000"), recorded_at=NOW + timedelta(minutes=5))
    assert first.attribution_state == "complete"

    incremental, incremental_rows, values = prepare(
        run_id="run_002",
        execution_id="execution_002",
        as_of=NOW + timedelta(minutes=6),
        session=Session(balance=250_000),
        ledger_reader=lambda **kwargs: ledger(
            month_opening_balance=Decimal("250100"),
            monthly_ledger_debit=Decimal("100"),
            monthly_vendor_reported=Decimal("100"),
        ),
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
        clock=lambda: NOW + timedelta(minutes=7),
    )
    persisted = []
    incremental._source_value_writer = persisted.append
    usages = {
        "wave1_tiktok_sound": (5, 5, 5),
        "wave1_instagram_reels": (4, 10, 10),
        "wave1_youtube_shorts_comments": (2, 6, 6),
        "wave1_reddit_comments": (1, 5, 5),
    }
    for phase, (calls, budget, vendor) in usages.items():
        incremental.phase_close(Wave1PhaseUsage(None, phase, calls, budget, vendor))
    second = incremental.close(**wave1_close_kwargs(Decimal("249974")))
    assert second.attribution_state == "complete"
    assert second.budget_debit_credits == Decimal("26")
    assert second.vendor_reported_credits == Decimal("26")
    assert second.attribution_gap_credits == Decimal("0")
    assert second.run_balance_delta == Decimal("26")
    assert len(persisted) == 1
    assert values["_controls"][-1].kill_state == "passed"
    shared_debit = Decimal("250100") - incremental_rows[-1]["balance_observed"]
    attributable = first.budget_debit_credits + second.budget_debit_credits
    held_unknown = first.attribution_gap_credits + second.attribution_gap_credits
    assert shared_debit == attributable + held_unknown == Decimal("126")
    assert first.vendor_reported_credits + second.vendor_reported_credits == Decimal("126")
    assert incremental_rows[-1]["monthly_ledger_debit_before"] == Decimal("126")
    assert incremental_rows[-1]["monthly_balance_delta_before"] == Decimal("126")
    assert baseline_rows[-1]["execution_id"] != incremental_rows[-1]["execution_id"]


def test_completed_values_replay_persists_once_and_cannot_reclose() -> None:
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import Wave1PhaseUsage

    from tests.unit.test_source_wave1_contract import _wave1_capability

    runtime, written, _ = prepare(
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
    )
    persisted = []
    runtime._source_value_writer = persisted.append
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    kwargs = wave1_close_kwargs(Decimal("250100"))
    receipt = runtime.close(**kwargs)
    assert receipt.source_values_state == "persisted"
    assert len(persisted) == 1
    rows_after_close = len(written)
    assert runtime.retry_source_value_persistence() is False
    assert len(persisted) == 1
    with pytest.raises(ValueError, match="already closed"):
        runtime.close(**kwargs)
    with pytest.raises(ValueError, match="already closed"):
        runtime.phase_close(Wave1PhaseUsage(None, "wave1_tiktok_sound", 0, 0, 0))
    assert len(written) == rows_after_close
    assert dict(receipt.source_values) == dict(runtime.close_receipt.source_values)


def test_the_funded_ledger_reports_credits_and_leaves_currency_to_the_priced_record() -> None:
    # The shared account is denominated in vendor credits. Money, with its currency,
    # its meter and its amount, belongs to the priced Source Lab record and is never
    # inferred from a credit count, so no funded row may carry a currency or an amount.
    from dataclasses import fields as dataclass_fields

    from src.analysis.open_intelligence.source_lab_persistence import (
        SOURCE_LAB_TABLE,
        _source_lab_schema,
    )

    runtime, written, _ = prepare()
    for usage in all_phase_usage():
        runtime.phase_close(usage)
    receipt = runtime.close(post_balance=Decimal("250000"), recorded_at=NOW + timedelta(minutes=5))
    money = ("currency", "amount", "price", "zar", "usd")
    assert receipt.budget_debit_credits == receipt.vendor_reported_credits == Decimal("100")
    receipt_fields = {field.name for field in dataclass_fields(receipt)}
    assert not [name for name in receipt_fields if any(word in name for word in money)]
    assert written
    for row in written:
        assert not [name for name in row if any(word in name for word in money)], row
        assert {"budget_debit_credits", "vendor_reported_credits"} <= set(row)
        assert row["vendor_reported_credits"] <= row["budget_debit_credits"]
        assert isinstance(row["budget_debit_credits"], Decimal)

    priced = {field.name: field for field in _source_lab_schema()}
    components = priced["official_price_components"]
    subfields = {field.name for field in components.fields}
    assert {"currency", "amount", "unit", "billing_basis"} <= subfields
    assert SOURCE_LAB_TABLE != funded_lane_runtime._CONTROL_TABLE
    assert "currency" not in priced
