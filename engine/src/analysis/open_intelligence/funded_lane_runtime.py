"""Funded SocialCrawl preflight, phase attribution, and run reconciliation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import requests
from google.cloud import bigquery

from src.analysis.open_intelligence.funded_control import (
    FundedControlReceipt,
    build_funded_control_receipt,
)
from src.analysis.open_intelligence.funded_control_persistence import (
    persist_funded_control_receipt,
)
from src.analysis.open_intelligence.funded_control_terminal import (
    FundedTerminalEvent,
    build_funded_terminal_event,
)
from src.analysis.open_intelligence.funded_control_terminal_persistence import (
    persist_funded_terminal_event,
)
from src.analysis.open_intelligence.funded_lane import (
    APPROVED_CATALOG_DIGEST,
    APPROVED_METADATA_DIGEST,
    FUNDED_CREDENTIAL_LANE,
    MONTHLY_CAP,
    OPENING_BALANCE,
    RESERVE_FLOOR,
    WAVE1_PHASE_CAPS,
    WAVE1_STAGE,
    FundedLaneRules,
    MonthlySpendSnapshot,
    Wave1ExecutionCapability,
    _require_wave1_execution_capability,
    evaluate_funded_lane,
)
from src.analysis.open_intelligence.funded_lane_persistence import (
    FundedLaneLedgerBatch,
    persist_funded_lane_rows,
)
from src.analysis.open_intelligence.funded_lane_reader import (
    FundedLaneLedgerSnapshot,
    read_monthly_funded_lane,
)
from src.analysis.open_intelligence.funded_source_values import (
    build_wave1_source_value_rows,
    persist_wave1_source_value_rows,
    wave1_close_succeeded,
)
from src.analysis.open_intelligence.source_lab import (
    Wave1SourceValueResult,
    normalize_balance,
    normalize_catalog,
    resolve_wave1_activation,
    wave1_route_identity,
)
from src.contracts.open_intelligence import encode_identifier_part
from src.ingestion.connectors.socialcrawl import PHASE_ORDER, SocialCrawlPhaseUsage
from src.utils.secrets import get_secret

BALANCE_URL = "https://www.socialcrawl.dev/v1/credits/balance"
CATALOG_URL = "https://www.socialcrawl.dev/v1/utility/endpoints"
SECRET_ID = "SOCIALCRAWL_OGILVY_API_KEY"
EXPECTED_PHASE_KEYS = frozenset(
    (market, phase) for market in ("za", "ng", "ke") for phase in PHASE_ORDER
)
WAVE1_EXPECTED_PHASE_KEYS = frozenset((None, phase) for phase in WAVE1_PHASE_CAPS)
_CONTROL_TABLE = "socialcrawl_funded_control_receipts_v1"
_CONTROL_ACCOUNT = "ogilvy_albert"


class FundedLaneActivationBlocked(RuntimeError):
    pass


def read_latest_funded_control_state(
    *, client: object, dataset: str, as_of: datetime, source_sha: str | None = None
) -> str | None:
    if (
        getattr(client, "project", None) != "ogilvy-trends-v2"
        or getattr(client, "location", None) != "US"
        or dataset != "trends_v2_staging_funded"
        or not isinstance(as_of, datetime)
        or as_of.tzinfo is None
        or as_of.utcoffset() != timedelta(0)
    ):
        raise FundedLaneActivationBlocked("prior funded control target is invalid")
    sql = (
        "SELECT kill_state, recorded_at, source_sha FROM "
        f"`ogilvy-trends-v2.{dataset}.{_CONTROL_TABLE}` "
        "WHERE credential_lane = 'ogilvy_funded' "
        f"AND funding_account = '{_CONTROL_ACCOUNT}' "
        "AND recorded_at < @as_of "
        "ORDER BY recorded_at DESC, control_id DESC LIMIT 1"
    )
    try:
        job = client.query(
            sql,
            location="US",
            job_config=bigquery.QueryJobConfig(
                use_legacy_sql=False,
                query_parameters=[bigquery.ScalarQueryParameter("as_of", "TIMESTAMP", as_of)],
            ),
            retry=None,
            job_retry=None,
        )
        if getattr(job, "errors", None):
            raise FundedLaneActivationBlocked("prior funded control query returned errors")
        rows = tuple(job.result(max_results=2, retry=None, job_retry=None))
    except FundedLaneActivationBlocked:
        raise
    except Exception as error:
        raise FundedLaneActivationBlocked("prior funded control read failed") from error
    if not rows:
        return None
    if len(rows) != 1:
        raise FundedLaneActivationBlocked("prior funded control cardinality is invalid")
    row = rows[0] if isinstance(rows[0], Mapping) else dict(rows[0].items())
    kill_state = row.get("kill_state")
    recorded_at = row.get("recorded_at")
    if (
        kill_state not in {"not_tested", "passed", "failed", "killed"}
        or not isinstance(recorded_at, datetime)
        or recorded_at.tzinfo is None
        or recorded_at.utcoffset() != timedelta(0)
        or recorded_at >= as_of
    ):
        raise FundedLaneActivationBlocked("prior funded control row is invalid")
    if kill_state == "not_tested" and not _ledger_phases_since(client, dataset, recorded_at, as_of):
        # The control was written at preflight and never resolved because
        # its execution stopped before any phase (attempt 12 on staging,
        # 4 Sep 2026, blocked every later run). It spent nothing on the
        # ledger and no kill switch was exercised, so it is void rather
        # than a block; a vendor-side debit surfaces as a balance delta gap.
        return None
    control_sha = row.get("source_sha")
    if (
        kill_state == "failed"
        and isinstance(source_sha, str)
        and source_sha
        and isinstance(control_sha, str)
        and control_sha != source_sha
    ):
        # A failed control stops the code that failed from running again on
        # the funded key. The fix arrives as a new commit on the lane, and that
        # commit is the operator's acknowledgement; the failed row stays in the
        # table. A killed control keeps blocking whatever the source is.
        return None
    return str(kill_state)


def _ledger_phases_since(client: object, dataset: str, since: datetime, as_of: datetime) -> bool:
    sql = (
        "SELECT COUNT(*) AS phase_rows FROM "
        f"`ogilvy-trends-v2.{dataset}.socialcrawl_credit_ledger_v1` "
        "WHERE credential_lane = 'ogilvy_funded' AND event_type = 'phase_close' "
        "AND recorded_at >= @since AND recorded_at < @as_of"
    )
    try:
        job = client.query(
            sql,
            location="US",
            job_config=bigquery.QueryJobConfig(
                use_legacy_sql=False,
                query_parameters=[
                    bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
                    bigquery.ScalarQueryParameter("as_of", "TIMESTAMP", as_of),
                ],
            ),
            retry=None,
            job_retry=None,
        )
        if getattr(job, "errors", None):
            raise FundedLaneActivationBlocked("prior funded control query returned errors")
        rows = tuple(job.result(max_results=2, retry=None, job_retry=None))
    except FundedLaneActivationBlocked:
        raise
    except Exception as error:
        raise FundedLaneActivationBlocked("prior funded control read failed") from error
    if len(rows) != 1:
        raise FundedLaneActivationBlocked("prior funded control cardinality is invalid")
    row = rows[0] if isinstance(rows[0], Mapping) else dict(rows[0].items())
    count = row.get("phase_rows")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise FundedLaneActivationBlocked("prior funded control row is invalid")
    return count > 0


@dataclass(frozen=True, slots=True)
class FundedRunCloseReceipt:
    attribution_state: str
    calls: int
    budget_debit_credits: Decimal
    vendor_reported_credits: Decimal
    balance_delta: Decimal | None
    attribution_gap_credits: Decimal | None
    balance_read_status: str = "measured"
    terminal_id: str | None = None
    source_values_state: str = "persisted"
    source_values: Mapping[str, Mapping[str, object]] = MappingProxyType({})
    # This run's own movement of the balance, preflight read to close read;
    # balance_delta above is the month's running total (attempt 19, 4 Sep 2026).
    run_balance_delta: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Wave1PhaseUsage:
    market: None
    phase: str
    calls: int
    budget_debit_credits: int
    vendor_reported_credits: int
    authorized_quoted_debit: int | None = None

    def __post_init__(self) -> None:
        if self.market is not None:
            raise ValueError("Wave 1 phases are global")
        if self.phase not in WAVE1_PHASE_CAPS:
            raise ValueError("Wave 1 phase is unsupported")
        for field in ("calls", "budget_debit_credits", "vendor_reported_credits"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field.replace('_', ' ')} is invalid")
        if self.budget_debit_credits > WAVE1_PHASE_CAPS[self.phase]:
            raise ValueError("Wave 1 phase debit exceeds its lane cap")
        if self.vendor_reported_credits > self.budget_debit_credits:
            raise ValueError("vendor reported credits cannot exceed budget debit")
        quoted = (
            self.budget_debit_credits
            if self.authorized_quoted_debit is None
            else self.authorized_quoted_debit
        )
        if (
            isinstance(quoted, bool)
            or not isinstance(quoted, int)
            or not 0 <= quoted <= self.budget_debit_credits
        ):
            raise ValueError("authorized quoted debit is invalid")
        object.__setattr__(self, "authorized_quoted_debit", quoted)


def make_wave1_source_value_writer(
    *, client: object, dataset: str, runtime: FundedLaneRuntime
) -> Callable[[object], None]:
    """Ledger the closing run's measured values beside its funded receipts.

    The pilot never writes Source Lab: the daily snapshot owns those rows and
    carries the latest succeeded pilot's values into them (Albert, 5 Sep 2026,
    after attempt 16 collided with the snapshot's immutable rows). The verdict
    written here is the one the durable result records, so the snapshot reads
    success from the funded dataset it can see, not from the approval store.
    """

    created_at = None

    def write(values: object) -> None:
        nonlocal created_at
        receipt = runtime.close_receipt
        verdict = runtime.close_verdict
        if (
            receipt is None
            or runtime.close_recorded_at is None
            or verdict not in {"succeeded", "failed"}
        ):
            raise FundedLaneActivationBlocked("Wave 1 close receipt is unavailable")
        if created_at is None:
            created_at = runtime._clock()
        rows = build_wave1_source_value_rows(
            execution_id=runtime.execution_id,
            run_id=runtime.run_id,
            recorded_at=runtime.close_recorded_at,
            close_verdict=verdict,
            attribution_state=receipt.attribution_state,
            source_sha=runtime._source_sha,
            catalog_digest=getattr(runtime.gate, "catalog_digest", None),
            metadata_digest=getattr(runtime.gate, "metadata_digest", None),
            values=values,
            created_at=created_at,
        )
        persist_wave1_source_value_rows(
            project="ogilvy-trends-v2", dataset=dataset, client=client, rows=rows
        )

    return write


def _ledger_id(
    execution_id: str,
    credential_lane: str,
    market: str | None,
    phase: str,
    event_type: str,
) -> str:
    canonical = "".join(
        encode_identifier_part(value)
        for value in (execution_id, credential_lane, market, phase, event_type)
    )
    return "ledger_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_catalog(session: requests.Session, checked_at: datetime):
    response = session.get(CATALOG_URL, timeout=60)
    response.raise_for_status()
    payload = response.json()
    # The funded lane gates on the Wave 1 route identity, not on the vendor's
    # platform and route counts, which move several times a day; the
    # normaliser still proves the payload is internally consistent.
    stats = payload.get("data", {}).get("stats", {}) if isinstance(payload, dict) else {}
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    return normalize_catalog(
        payload,
        checked_at=checked_at,
        expected_platforms=stats.get("platforms", 0),
        expected_endpoints=stats.get("endpoints", 0),
        wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
    )


def _wave1_identity(catalog: object) -> tuple[str, str]:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    return wave1_route_identity(catalog, tuple(WAVE1_ROUTE_SPECS))


def _read_balance(session: requests.Session, observed_at: datetime):
    response = session.get(BALANCE_URL, timeout=60)
    response.raise_for_status()
    return normalize_balance(response.json(), observed_at=observed_at)


def make_funded_ledger_writer(
    *,
    client: object,
    dataset: str,
    stage_name: str | None = None,
    execution_capability: Wave1ExecutionCapability | None = None,
) -> Callable[[dict[str, object]], None]:
    def write(row: dict[str, object]) -> None:
        if stage_name == WAVE1_STAGE:
            _require_wave1_execution_capability(execution_capability, action="funded_ledger_write")
        persist_funded_lane_rows(
            project="ogilvy-trends-v2",
            dataset=dataset,
            client=client,
            batch=FundedLaneLedgerBatch(rows=(row,)),
            dry_run=False,
        )

    return write


def make_funded_control_writer(
    *, client: object, dataset: str
) -> Callable[[FundedControlReceipt], None]:
    def write(receipt: FundedControlReceipt) -> None:
        persist_funded_control_receipt(
            project="ogilvy-trends-v2",
            dataset=dataset,
            client=client,
            receipt=receipt,
        )

    return write


def make_funded_terminal_writer(
    *,
    client: object,
    dataset: str,
    stage_name: str | None = None,
    execution_capability: Wave1ExecutionCapability | None = None,
) -> Callable[[FundedTerminalEvent], None]:
    def write(event: FundedTerminalEvent) -> None:
        if stage_name == WAVE1_STAGE:
            _require_wave1_execution_capability(
                execution_capability, action="funded_terminal_write"
            )
        persist_funded_terminal_event(
            project="ogilvy-trends-v2",
            dataset=dataset,
            client=client,
            event=event,
        )

    return write


def read_funded_balance(
    *,
    observed_at: datetime,
    secret_reader: Callable[[str], str] = get_secret,
    session: requests.Session | None = None,
    stage_name: str | None = None,
    execution_capability: Wave1ExecutionCapability | None = None,
) -> Decimal:
    if stage_name == WAVE1_STAGE:
        _require_wave1_execution_capability(execution_capability, action="balance_read")
    key = secret_reader(SECRET_ID)
    if not isinstance(key, str) or not key or key.strip() != key:
        raise FundedLaneActivationBlocked("funded secret is unavailable or malformed")
    session = session or requests.Session()
    session.headers.update({"x-api-key": key})
    try:
        return _read_balance(session, observed_at).balance
    except Exception as error:
        raise FundedLaneActivationBlocked("post-run balance read failed") from error


def _rules() -> FundedLaneRules:
    return FundedLaneRules(
        credential_lane=FUNDED_CREDENTIAL_LANE,
        opening_balance=OPENING_BALANCE,
        monthly_cap=MONTHLY_CAP,
        reserve_floor=RESERVE_FLOOR,
        stage_caps={0: Decimal("0"), 1: Decimal("100"), 2: Decimal("250"), 3: Decimal("750")},
        catalog_digest=APPROVED_CATALOG_DIGEST,
        metadata_digest=APPROVED_METADATA_DIGEST,
    )


class FundedLaneRuntime:
    def __init__(
        self,
        *,
        run_id: str,
        execution_id: str,
        as_of: datetime,
        gate: object,
        ledger_writer: Callable[[dict[str, object]], None],
        clock: Callable[[], datetime],
        stage_name: str | None = None,
        execution_capability: Wave1ExecutionCapability | None = None,
        source_value_writer: Callable[[object], None] | None = None,
        call_authority_check: Callable[[int], None] | None = None,
        control_writer: Callable[[FundedControlReceipt], None] | None = None,
        terminal_writer: Callable[[FundedTerminalEvent], None] | None = None,
        source_sha: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.execution_id = execution_id
        self.as_of = as_of
        self.gate = gate
        self._ledger_writer = ledger_writer
        self._clock = clock
        self.stage_name = stage_name
        self.execution_capability = execution_capability
        self._source_value_writer = source_value_writer
        self._call_authority_check = call_authority_check
        self._control_writer = control_writer
        self._terminal_writer = terminal_writer
        self._source_sha = source_sha
        self._pending_source_value_results = None
        self._source_value_persisted = False
        self.close_receipt: FundedRunCloseReceipt | None = None
        self.close_recorded_at: datetime | None = None
        self.close_vendor_overage = False
        self.close_verdict: str | None = None
        self._seen: set[tuple[str, str]] = set()
        self._calls = 0
        self._budget = Decimal("0")
        self._vendor = Decimal("0")
        self._quoted = Decimal("0")
        self._closed = False

    def _write_row(self, row: dict[str, object]) -> None:
        if self.stage_name == WAVE1_STAGE:
            _require_wave1_execution_capability(
                self.execution_capability, action="funded_ledger_write"
            )
        self._ledger_writer(row)

    @property
    def run_allowance(self) -> int:
        return int(self.gate.run_allowance)

    def assert_call_authority(self, projected_run_debit: int) -> None:
        if self.stage_name == WAVE1_STAGE:
            _require_wave1_execution_capability(
                self.execution_capability, action="funded_call_authority"
            )
        if (
            isinstance(projected_run_debit, bool)
            or not isinstance(projected_run_debit, int)
            or projected_run_debit <= 0
        ):
            raise FundedLaneActivationBlocked("projected funded debit is invalid")
        if not callable(self._call_authority_check):
            raise FundedLaneActivationBlocked("fresh funded call authority is unavailable")
        self._call_authority_check(projected_run_debit)

    def _base_row(
        self,
        *,
        market: str | None,
        phase: str,
        event_type: str,
        recorded_at: datetime,
        calls: int,
        budget: Decimal,
        vendor: Decimal,
        balance: Decimal | None,
        attribution_state: str,
        monthly_ledger_before: Decimal,
        monthly_balance_delta_before: Decimal,
    ) -> dict[str, object]:
        return {
            "ledger_id": _ledger_id(
                self.execution_id, FUNDED_CREDENTIAL_LANE, market, phase, event_type
            ),
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "trend_date": self.as_of.date(),
            "recorded_at": recorded_at,
            "credential_lane": FUNDED_CREDENTIAL_LANE,
            "market": market,
            "phase": phase,
            "event_type": event_type,
            "calls": calls,
            "budget_debit_credits": budget,
            "vendor_reported_credits": vendor,
            "balance_observed": balance,
            "opening_balance": OPENING_BALANCE,
            "month_opening_balance": self.gate.month_opening_balance,
            "month_start": date(self.as_of.year, self.as_of.month, 1),
            "monthly_ledger_debit_before": monthly_ledger_before,
            "monthly_balance_delta_before": monthly_balance_delta_before,
            "monthly_effective_spend_before": max(
                monthly_ledger_before, monthly_balance_delta_before
            ),
            "monthly_cap": MONTHLY_CAP,
            "reserve_floor": RESERVE_FLOOR,
            "run_allowance": self.gate.run_allowance,
            "catalog_digest": APPROVED_CATALOG_DIGEST,
            "metadata_digest": APPROVED_METADATA_DIGEST,
            "attribution_state": attribution_state,
            "created_at": recorded_at,
        }

    def write_preflight(self) -> None:
        balance_delta = self.gate.monthly_balance_delta
        if balance_delta is None:
            raise FundedLaneActivationBlocked("preflight balance is unavailable")
        self._write_row(
            self._base_row(
                market=None,
                phase="preflight",
                event_type="preflight",
                recorded_at=self.as_of,
                calls=0,
                budget=Decimal("0"),
                vendor=Decimal("0"),
                balance=self.gate.current_balance,
                attribution_state=self.gate.attribution_state,
                monthly_ledger_before=self.gate.monthly_ledger_debit,
                monthly_balance_delta_before=balance_delta,
            )
        )

    def phase_close(self, usage: SocialCrawlPhaseUsage | Wave1PhaseUsage) -> None:
        if self._closed:
            raise ValueError("funded run is already closed")
        expected_type = Wave1PhaseUsage if self.stage_name == WAVE1_STAGE else SocialCrawlPhaseUsage
        if not isinstance(usage, expected_type):
            raise ValueError("phase usage is invalid")
        key = (usage.market, usage.phase)
        if key in self._seen:
            raise ValueError("duplicate phase close")
        recorded_at = self._clock()
        if recorded_at.tzinfo is None or recorded_at.utcoffset() != timedelta(0):
            raise ValueError("phase close clock must be UTC")
        budget = Decimal(usage.budget_debit_credits)
        vendor = Decimal(usage.vendor_reported_credits)
        state = "complete" if budget == vendor else "conservative"
        balance_delta = self.gate.monthly_balance_delta
        if balance_delta is None:
            raise FundedLaneActivationBlocked("phase close balance is unavailable")
        self._write_row(
            self._base_row(
                market=usage.market,
                phase=usage.phase,
                event_type="phase_close",
                recorded_at=recorded_at,
                calls=usage.calls,
                budget=budget,
                vendor=vendor,
                balance=None,
                attribution_state=state,
                monthly_ledger_before=self.gate.monthly_ledger_debit + self._budget,
                monthly_balance_delta_before=balance_delta,
            )
        )
        self._seen.add(key)
        self._calls += usage.calls
        self._budget += budget
        self._vendor += vendor
        self._quoted += Decimal(usage.authorized_quoted_debit)

    def _write_terminal(
        self,
        *,
        recorded_at: datetime,
        balance_read_status: str,
        last_measured_balance: Decimal,
        last_balance_observed_at: datetime,
        attribution_state: str,
        reason_codes: tuple[str, ...],
    ) -> FundedTerminalEvent:
        if (
            not callable(self._terminal_writer)
            or not isinstance(self._source_sha, str)
            or self.execution_capability is None
        ):
            raise FundedLaneActivationBlocked("funded terminal writer authority is unavailable")
        manifest_sha256 = getattr(self.execution_capability, "manifest_sha256", None)
        event = build_funded_terminal_event(
            execution_id=self.execution_id,
            run_id=self.run_id,
            recorded_at=recorded_at,
            balance_read_status=balance_read_status,
            last_measured_balance=last_measured_balance,
            last_balance_observed_at=last_balance_observed_at,
            authorized_quoted_debit=self._quoted,
            vendor_reported_debit=self._vendor,
            attribution_state=attribution_state,
            reason_codes=reason_codes,
            source_sha=self._source_sha,
            manifest_sha256=manifest_sha256,
        )
        self._terminal_writer(event)
        return event

    def close_unavailable(self, *, recorded_at: datetime) -> FundedRunCloseReceipt:
        if self._closed:
            raise ValueError("funded run is already closed")
        expected = (
            WAVE1_EXPECTED_PHASE_KEYS if self.stage_name == WAVE1_STAGE else EXPECTED_PHASE_KEYS
        )
        if self._seen != expected:
            count = 4 if self.stage_name == WAVE1_STAGE else 21
            raise ValueError(f"funded run requires exactly {count} phase close rows")
        if (
            not isinstance(recorded_at, datetime)
            or recorded_at.tzinfo is None
            or recorded_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("run close time must be UTC")
        if self.gate.current_balance is None:
            raise FundedLaneActivationBlocked("last measured funded balance is unavailable")
        reasons = ["post_balance_unavailable"]
        if self._vendor > self._quoted:
            reasons.append("vendor_overage")
        event = self._write_terminal(
            recorded_at=recorded_at,
            balance_read_status="unavailable",
            last_measured_balance=self.gate.current_balance,
            last_balance_observed_at=self.as_of,
            attribution_state="gap_detected",
            reason_codes=tuple(reasons),
        )
        self._closed = True
        return FundedRunCloseReceipt(
            attribution_state="gap_detected",
            calls=self._calls,
            budget_debit_credits=self._budget,
            vendor_reported_credits=self._vendor,
            balance_delta=None,
            attribution_gap_credits=None,
            balance_read_status="unavailable",
            terminal_id=event.terminal_id,
        )

    def close(
        self,
        *,
        post_balance: Decimal,
        recorded_at: datetime,
        source_value_results: object = None,
        gdelt_runtime_proof: object = None,
    ) -> FundedRunCloseReceipt:
        if self._closed:
            raise ValueError("funded run is already closed")
        expected = (
            WAVE1_EXPECTED_PHASE_KEYS if self.stage_name == WAVE1_STAGE else EXPECTED_PHASE_KEYS
        )
        if self._seen != expected:
            count = 4 if self.stage_name == WAVE1_STAGE else 21
            raise ValueError(f"funded run requires exactly {count} phase close rows")
        activation_error = None
        gdelt_complete = False
        if self.stage_name == WAVE1_STAGE:
            expected_routes = {
                "/v1/tiktok/song",
                "/v1/tiktok/song/videos",
                "/v1/instagram/music/trending",
                "/v1/instagram/audio/reels",
                "/v1/instagram/search/reels",
                "/v1/youtube/shorts/trending",
                "/v1/youtube/video/comments",
                "/v1/reddit/post/comments",
            }
            # The measured set is the routes the seeds could fill; an unfilled
            # route stays inventory-only. One route must carry value and no
            # measured route may be rejected (attempt 17, 4 Sep 2026).
            if (
                not isinstance(source_value_results, Mapping)
                or not source_value_results
                or not set(source_value_results) <= expected_routes
            ):
                activation_error = "Wave 1 source value is unavailable"
            elif any(
                not isinstance(result, Wave1SourceValueResult)
                for result in source_value_results.values()
            ):
                activation_error = "Wave 1 source value is invalid"
            else:
                blocked = tuple(
                    route
                    for route, result in source_value_results.items()
                    if resolve_wave1_activation(result) != "active"
                )
                if blocked:
                    activation_error = "Wave 1 source value is permanently_rejected: " + ",".join(
                        sorted(blocked)
                    )
                elif not any(
                    resolve_wave1_activation(result) == "active"
                    for result in source_value_results.values()
                ):
                    activation_error = "Wave 1 source value has no active route"
            if not callable(self._source_value_writer):
                activation_error = activation_error or "Wave 1 source value writer is unavailable"
            gdelt_complete = (
                gdelt_runtime_proof is not None
                and tuple(getattr(gdelt_runtime_proof, "query_job_ids", ()))
                and tuple(name for name, _value in gdelt_runtime_proof.query_job_ids)
                == ("events", "gcam")
                and tuple(name for name, _value in gdelt_runtime_proof.query_bytes_processed)
                == ("events", "gcam")
                and getattr(getattr(gdelt_runtime_proof, "persistence", None), "complete", None)
                is True
            )
            if not gdelt_complete:
                activation_error = activation_error or "Wave 1 GDELT proof is incomplete"
        if (
            not isinstance(post_balance, Decimal)
            or not post_balance.is_finite()
            or post_balance < 0
        ):
            raise ValueError("post balance must be a nonnegative Decimal")
        if (
            not isinstance(recorded_at, datetime)
            or recorded_at.tzinfo is None
            or recorded_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("run close time must be UTC")
        if post_balance > self.gate.month_opening_balance:
            raise FundedLaneActivationBlocked("post balance increase is unreconciled")
        post_delta = self.gate.month_opening_balance - post_balance
        ledger_after = self.gate.monthly_ledger_debit + self._budget
        vendor_after = self.gate.monthly_vendor_reported + self._vendor
        gap = max(Decimal("0"), post_delta - ledger_after)
        before_delta = self.gate.monthly_balance_delta
        if before_delta is None:
            raise FundedLaneActivationBlocked("run close balance is unavailable")
        if gap > 0:
            self._write_row(
                self._base_row(
                    market=None,
                    phase="unattributed",
                    event_type="attribution_gap",
                    recorded_at=recorded_at,
                    calls=0,
                    budget=gap,
                    vendor=Decimal("0"),
                    balance=post_balance,
                    attribution_state="gap_detected",
                    monthly_ledger_before=ledger_after,
                    monthly_balance_delta_before=post_delta,
                )
            )
            state = "gap_detected"
        elif ledger_after > post_delta or self._budget > self._vendor:
            state = "conservative"
        elif ledger_after == vendor_after == post_delta:
            state = "complete"
        else:
            state = "gap_detected"
        self._write_row(
            self._base_row(
                market=None,
                phase="run_close",
                event_type="run_close",
                recorded_at=recorded_at,
                calls=self._calls,
                budget=self._budget,
                vendor=self._vendor,
                balance=post_balance,
                attribution_state=state,
                monthly_ledger_before=ledger_after + gap,
                monthly_balance_delta_before=post_delta,
            )
        )
        self._closed = True
        run_delta = (
            None
            if self.gate.current_balance is None
            else Decimal(self.gate.current_balance) - Decimal(post_balance)
        )
        receipt = FundedRunCloseReceipt(
            attribution_state=state,
            calls=self._calls,
            budget_debit_credits=self._budget,
            vendor_reported_credits=self._vendor,
            balance_delta=post_delta,
            attribution_gap_credits=gap,
            run_balance_delta=run_delta,
        )
        source_value_error = None
        if self.stage_name == WAVE1_STAGE and isinstance(source_value_results, Mapping):
            self._pending_source_value_results = MappingProxyType(dict(source_value_results))
            receipt = FundedRunCloseReceipt(
                attribution_state=receipt.attribution_state,
                calls=receipt.calls,
                budget_debit_credits=receipt.budget_debit_credits,
                vendor_reported_credits=receipt.vendor_reported_credits,
                balance_delta=receipt.balance_delta,
                attribution_gap_credits=receipt.attribution_gap_credits,
                run_balance_delta=receipt.run_balance_delta,
                source_values=_source_values_record(source_value_results),
            )
            # The writer reads the close verdict off the runtime. It is the
            # verdict the durable result will record: every fault close()
            # already holds (a rejected or missing route, an incomplete GDELT
            # proof), the held-bound attribution rule, and the vendor overage
            # terminal decided below. Only a fault after close can differ.
            self.close_receipt = receipt
            self.close_recorded_at = recorded_at
            self.close_vendor_overage = self._vendor > self._quoted
            self.close_verdict = (
                "succeeded"
                if activation_error is None
                and wave1_close_succeeded(receipt)
                and not self.close_vendor_overage
                else "failed"
            )
            try:
                self.retry_source_value_persistence()
            except Exception as error:
                source_value_error = error
                activation_error = "Wave 1 source value persistence failed after run close"
        if (
            self.stage_name == WAVE1_STAGE
            and callable(self._control_writer)
            and isinstance(self._source_sha, str)
        ):
            post_gate = evaluate_funded_lane(
                MonthlySpendSnapshot(
                    as_of=recorded_at,
                    month_start=date(recorded_at.year, recorded_at.month, 1),
                    current_balance=post_balance,
                    month_opening_balance=self.gate.month_opening_balance,
                    monthly_ledger_debit=ledger_after + gap,
                    monthly_vendor_reported=vendor_after,
                    unreconciled_execution_ids=(),
                    activation_stage=self.gate.activation_stage,
                    consecutive_complete_runs=(
                        self.gate.consecutive_complete_runs + 1 if state == "complete" else 0
                    ),
                    runs_today=self.gate.runs_today + 1,
                    catalog_digest=self.gate.catalog_digest,
                    metadata_digest=self.gate.metadata_digest,
                ),
                _rules(),
            )
            hard_breaker = (
                state == "gap_detected"
                or post_balance <= RESERVE_FLOOR
                or post_gate.monthly_remaining == 0
                or self._budget > self.gate.stage_cap
            )
            if hard_breaker:
                kill_state = "killed"
            elif (
                activation_error is not None
                or state not in {"complete", "conservative"}
                or not gdelt_complete
            ):
                kill_state = "failed"
            else:
                kill_state = "passed"
            if self._vendor > self._quoted:
                event = self._write_terminal(
                    recorded_at=recorded_at,
                    balance_read_status="measured",
                    last_measured_balance=post_balance,
                    last_balance_observed_at=recorded_at,
                    attribution_state=state,
                    reason_codes=("vendor_overage",),
                )
                receipt = FundedRunCloseReceipt(
                    attribution_state=receipt.attribution_state,
                    calls=receipt.calls,
                    budget_debit_credits=receipt.budget_debit_credits,
                    vendor_reported_credits=receipt.vendor_reported_credits,
                    balance_delta=receipt.balance_delta,
                    attribution_gap_credits=receipt.attribution_gap_credits,
                    terminal_id=event.terminal_id,
                )
            else:
                self._control_writer(
                    build_funded_control_receipt(
                        gate=post_gate,
                        recorded_at=recorded_at,
                        balance_observed_at=recorded_at,
                        source_sha=self._source_sha,
                        kill_state=kill_state,
                    )
                )
        if activation_error is not None:
            if source_value_error is not None:
                raise FundedLaneActivationBlocked(activation_error) from source_value_error
            raise FundedLaneActivationBlocked(activation_error)
        return receipt

    def retry_source_value_persistence(self) -> bool:
        if not self._closed:
            raise ValueError("funded run must close before Source Lab persistence")
        if self._source_value_persisted or self._pending_source_value_results is None:
            return False
        if not callable(self._source_value_writer):
            raise FundedLaneActivationBlocked("Wave 1 source value writer is unavailable")
        self._source_value_writer(self._pending_source_value_results)
        self._source_value_persisted = True
        return True


def _source_values_record(
    results: Mapping[str, object],
) -> Mapping[str, Mapping[str, object]]:
    record = {}
    for route in sorted(results):
        result = results[route]
        record[route] = MappingProxyType(
            {
                "state": getattr(result, "state", None),
                "reason": getattr(result, "reason", None),
                "unique_observations": getattr(result, "unique_observations", None),
                "marginal_candidates": getattr(result, "marginal_candidates", None),
                "marginal_evidence": getattr(result, "marginal_evidence", None),
            }
        )
    return MappingProxyType(record)


def prepare_funded_lane(
    *,
    client: object,
    dataset: str,
    run_id: str,
    execution_id: str,
    as_of: datetime,
    activation_stage: int,
    environment: str,
    secret_reader: Callable[[str], str] = get_secret,
    session: requests.Session | None = None,
    catalog_reader: Callable[[requests.Session, datetime], object] = _read_catalog,
    ledger_reader: Callable[..., FundedLaneLedgerSnapshot] = read_monthly_funded_lane,
    control_state_reader: Callable[..., str | None] = read_latest_funded_control_state,
    ledger_writer: Callable[[dict[str, object]], None],
    control_writer: Callable[[FundedControlReceipt], None] | None = None,
    terminal_writer: Callable[[FundedTerminalEvent], None] | None = None,
    source_sha: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    stage_name: str | None = None,
    execution_capability: Wave1ExecutionCapability | None = None,
) -> FundedLaneRuntime:
    if stage_name == WAVE1_STAGE:
        _require_wave1_execution_capability(execution_capability, action="funded_preflight")
    if environment != "staging":
        raise FundedLaneActivationBlocked("funded lane is staging only")
    if not isinstance(run_id, str) or not run_id.strip():
        raise FundedLaneActivationBlocked("run ID is invalid")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise FundedLaneActivationBlocked("execution ID is invalid")
    if as_of.tzinfo is None or as_of.utcoffset() != timedelta(0):
        raise FundedLaneActivationBlocked("preflight time must be UTC")
    if stage_name == WAVE1_STAGE:
        prior_control = control_state_reader(
            client=client,
            dataset=dataset,
            as_of=as_of,
            source_sha=(
                execution_capability.source_sha if execution_capability is not None else source_sha
            ),
        )
        if prior_control is not None and prior_control != "passed":
            raise FundedLaneActivationBlocked(
                f"prior funded control blocks execution: {prior_control}"
            )
    key = secret_reader(SECRET_ID)
    if not isinstance(key, str) or not key or key.strip() != key:
        raise FundedLaneActivationBlocked("funded secret is unavailable or malformed")
    session = session or requests.Session()
    session.headers.update({"x-api-key": key})
    try:
        balance = _read_balance(session, as_of)
        catalog = catalog_reader(session, as_of)
    except Exception as error:
        raise FundedLaneActivationBlocked("zero-credit funded preflight failed") from error
    try:
        route_digest, route_metadata_digest = _wave1_identity(catalog)
    except Exception as error:
        raise FundedLaneActivationBlocked("catalog identity is not approved") from error
    if (
        route_digest != APPROVED_CATALOG_DIGEST
        or route_metadata_digest != APPROVED_METADATA_DIGEST
        or getattr(catalog, "credits_used", None) != Decimal("0")
    ):
        raise FundedLaneActivationBlocked("catalog identity is not approved")
    monthly = ledger_reader(
        client=client,
        dataset=dataset,
        as_of=as_of,
        credential_lane=FUNDED_CREDENTIAL_LANE,
    )
    if not isinstance(monthly, FundedLaneLedgerSnapshot):
        raise FundedLaneActivationBlocked("monthly ledger snapshot is invalid")
    gate = evaluate_funded_lane(
        MonthlySpendSnapshot(
            as_of=as_of,
            month_start=monthly.month_start,
            current_balance=balance.balance,
            month_opening_balance=(
                monthly.month_opening_balance
                if monthly.month_opening_balance is not None
                else balance.balance
            ),
            monthly_ledger_debit=monthly.monthly_ledger_debit,
            monthly_vendor_reported=monthly.monthly_vendor_reported,
            unreconciled_execution_ids=monthly.unreconciled_execution_ids,
            activation_stage=activation_stage,
            consecutive_complete_runs=monthly.consecutive_complete_runs,
            runs_today=monthly.runs_today,
            catalog_digest=route_digest,
            metadata_digest=route_metadata_digest,
        ),
        _rules(),
    )

    def check_call_authority(projected_run_debit: int) -> None:
        if stage_name == WAVE1_STAGE:
            _require_wave1_execution_capability(
                execution_capability, action="funded_call_authority"
            )
        checked_at = clock()
        if checked_at.tzinfo is None or checked_at.utcoffset() != timedelta(0):
            raise FundedLaneActivationBlocked("funded call authority clock must be UTC")
        try:
            fresh_balance = _read_balance(session, checked_at)
            fresh_monthly = ledger_reader(
                client=client,
                dataset=dataset,
                as_of=checked_at,
                credential_lane=FUNDED_CREDENTIAL_LANE,
            )
        except Exception as error:
            raise FundedLaneActivationBlocked("fresh funded call authority failed") from error
        if not isinstance(fresh_monthly, FundedLaneLedgerSnapshot):
            raise FundedLaneActivationBlocked("fresh funded ledger snapshot is invalid")
        other_unreconciled = tuple(
            item for item in fresh_monthly.unreconciled_execution_ids if item != execution_id
        )
        projected = Decimal(projected_run_debit)
        fresh_gate = evaluate_funded_lane(
            MonthlySpendSnapshot(
                as_of=checked_at,
                month_start=fresh_monthly.month_start,
                current_balance=fresh_balance.balance,
                month_opening_balance=(
                    fresh_monthly.month_opening_balance
                    if fresh_monthly.month_opening_balance is not None
                    else fresh_balance.balance
                ),
                monthly_ledger_debit=fresh_monthly.monthly_ledger_debit + projected,
                monthly_vendor_reported=fresh_monthly.monthly_vendor_reported,
                unreconciled_execution_ids=other_unreconciled,
                activation_stage=activation_stage,
                consecutive_complete_runs=fresh_monthly.consecutive_complete_runs,
                runs_today=fresh_monthly.runs_today,
                catalog_digest=route_digest,
                metadata_digest=route_metadata_digest,
            ),
            _rules(),
        )
        if not fresh_gate.allowed or projected > fresh_gate.run_allowance:
            reasons = fresh_gate.reasons or ("projected_run_allowance",)
            raise FundedLaneActivationBlocked("funded call blocked: " + ",".join(reasons))

    admitted_source_sha = (
        execution_capability.source_sha
        if stage_name == WAVE1_STAGE and execution_capability is not None
        else source_sha
    )
    control_receipt = build_funded_control_receipt(
        gate=gate,
        recorded_at=as_of,
        balance_observed_at=balance.observed_at,
        source_sha=admitted_source_sha,
        kill_state="not_tested",
    )
    if not callable(control_writer):
        raise FundedLaneActivationBlocked("funded control writer is unavailable")
    control_writer(control_receipt)
    if not gate.allowed:
        raise FundedLaneActivationBlocked("funded lane blocked: " + ",".join(gate.reasons))
    runtime = FundedLaneRuntime(
        run_id=run_id,
        execution_id=execution_id,
        as_of=as_of,
        gate=gate,
        ledger_writer=ledger_writer,
        clock=clock,
        stage_name=stage_name,
        execution_capability=execution_capability,
        call_authority_check=check_call_authority,
        control_writer=control_writer,
        terminal_writer=terminal_writer,
        source_sha=admitted_source_sha,
    )
    if stage_name == WAVE1_STAGE:
        runtime._source_value_writer = make_wave1_source_value_writer(
            client=client, dataset=dataset, runtime=runtime
        )
    runtime.write_preflight()
    return runtime
