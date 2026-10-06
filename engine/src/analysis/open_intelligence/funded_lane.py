"""Pure monthly allowance and attribution gate for the funded staging lane."""

from __future__ import annotations

import re
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

# The whole-catalog entry remains the historical reference. Funded admission
# uses the Wave 1 route identity below.
APPROVED_CATALOG_EXPECTATION = "socialcrawl_catalog_2026_09_04"
# The two approved digests are the Wave 1 route identity of that catalog, the
# eight approved routes' canonical route, credits, credit label and metered
# flag (source_lab.wave1_route_identity), not the whole-catalog digests: the
# vendor edits unrelated routes within hours and money rests on these eight.
APPROVED_CATALOG_DIGEST = "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6"
HISTORICAL_METADATA_DIGEST = "7d7152b2441f367417c74169a7bd4fb1a190a92763bbb43532167d5d4612fdfc"
APPROVED_METADATA_DIGEST = "501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a"
APPROVED_WAVE1_CATALOG_PAIRS = frozenset(
    {
        (APPROVED_CATALOG_DIGEST, HISTORICAL_METADATA_DIGEST),
        (APPROVED_CATALOG_DIGEST, APPROVED_METADATA_DIGEST),
    }
)


def approved_wave1_catalog_pair(catalog_digest: str, metadata_digest: str) -> bool:
    return (catalog_digest, metadata_digest) in APPROVED_WAVE1_CATALOG_PAIRS
FUNDED_CREDENTIAL_LANE = "ogilvy_funded"
OPENING_BALANCE = Decimal("250100")
MONTHLY_CAP = Decimal("25000")
RESERVE_FLOOR = Decimal("225000")
STAGE_CAPS = MappingProxyType(
    {0: Decimal("0"), 1: Decimal("100"), 2: Decimal("250"), 3: Decimal("750")}
)
WAVE1_STAGE = "stage_1_wave_1"
MAXIMUM_AUTHORIZED_QUOTED_DEBIT = 63
WAVE1_PHASE_CAPS = MappingProxyType(
    {
        "wave1_tiktok_sound": 25,
        "wave1_instagram_reels": 30,
        "wave1_youtube_shorts_comments": 25,
        "wave1_reddit_comments": 20,
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EXECUTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


class Wave1ExecutionBlocked(RuntimeError):
    pass


_SERVICE_IDENTITY = re.compile(
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@ogilvy-trends-v2\.iam\.gserviceaccount\.com"
)


def wave1_pilot_service_identity() -> str:
    """The one identity every Wave 1 query and writer admits.

    It is the wave1_pilot service identity the active trusted origin registry binds, the
    registry the loader admits authority under and whose binding the approved manifest
    must name. It is never read from a caller, a client or the environment.
    """
    from src.analysis.open_intelligence import execution_approval, execution_generations

    try:
        registry = execution_generations.active_generation().registry
        origin = execution_approval._v2_origin_for_operation(
            "wave1_pilot", mode="new_consume", registry=registry
        )
        identity = origin.operation_bindings["wave1_pilot"].service_identity
    except Exception as error:
        raise Wave1ExecutionBlocked(
            "Wave 1 identity is not bound by the trusted origin registry"
        ) from error
    if not isinstance(identity, str) or _SERVICE_IDENTITY.fullmatch(identity) is None:
        raise Wave1ExecutionBlocked("Wave 1 identity is not bound by the trusted origin registry")
    return identity


# The pilot job of the retained v1 record family. A fresh authority carries a trusted
# generation and is refused this branch, so no fresh run can bind to this job.
_RETAINED_WAVE1_JOB = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-open-intelligence-staging"
)


def _verify_wave1_consumption_v2(authority: object, consumption: object, generation) -> None:
    """The v2 consumption must name the authority it was bound to and the origin it selects.

    Job and execution parent come from the origin binding of the generation the authority
    carries; approval, manifest, execution and both generation digests must equal the
    authority's own. Any other generation, record family or operation refuses.
    """
    from src.analysis.open_intelligence.execution_origins import select_origin

    if consumption.consumption_contract_version != "open_intelligence_execution_consumption_v2":
        raise Wave1ExecutionBlocked("Wave 1 consumption family is invalid")
    if consumption.operation != "wave1_pilot":
        raise Wave1ExecutionBlocked("Wave 1 consumption operation differs")
    try:
        manifest = authority.manifest
        approval = authority.approval
        pair = (generation.origin_registry_sha256, generation.resource_manifest_sha256)
        origin = select_origin(
            manifest_version=manifest.manifest_version,
            contract_sha256=manifest.contract_sha256,
            mode="new_consume",
            registry=generation.registry,
        )
        expected_job = origin.operation_bindings["wave1_pilot"].job_resource
        approval_pair = (approval.origin_registry_sha256, approval.resource_manifest_sha256)
        approval_id = approval.approval_id
        manifest_sha256 = approval.manifest_sha256
        execution_name = authority.execution_name
    except Exception as error:
        raise Wave1ExecutionBlocked("Wave 1 durable consumption is invalid") from error
    if (
        pair != approval_pair
        or (consumption.origin_registry_sha256, consumption.resource_manifest_sha256) != pair
    ):
        raise Wave1ExecutionBlocked("Wave 1 consumption generation differs from the authority")
    if consumption.job_resource != expected_job or not consumption.execution_name.startswith(
        expected_job + "/executions/"
    ):
        raise Wave1ExecutionBlocked("Wave 1 consumption job differs from the origin binding")
    if consumption.approval_id != approval_id:
        raise Wave1ExecutionBlocked("Wave 1 consumption approval differs from the authority")
    if consumption.manifest_sha256 != manifest_sha256:
        raise Wave1ExecutionBlocked("Wave 1 consumption manifest differs from the authority")
    if consumption.execution_name != execution_name:
        raise Wave1ExecutionBlocked("Wave 1 consumption execution differs from the authority")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class Wave1ExecutionCapability:
    consumption_id: str
    manifest_sha256: str
    execution_name: str
    source_sha: str
    image_uri: str
    gdelt_dry_run_set_sha256: str
    maximum_bytes_billed: int
    route_set_sha256: str | None
    capability_digest: str

    def __init__(self, *_args, **_kwargs):
        raise Wave1ExecutionBlocked("Wave 1 capability is issued only after durable consumption")


def _wave1_capability_state():
    from src.analysis.open_intelligence.brain_contract import canonical_digest

    registry: dict[int, tuple[weakref.ReferenceType, str]] = {}

    def payload(value: Wave1ExecutionCapability) -> dict[str, object]:
        return {
            "consumption_id": value.consumption_id,
            "manifest_sha256": value.manifest_sha256,
            "execution_name": value.execution_name,
            "source_sha": value.source_sha,
            "image_uri": value.image_uri,
            "gdelt_dry_run_set_sha256": value.gdelt_dry_run_set_sha256,
            "maximum_bytes_billed": value.maximum_bytes_billed,
            "route_set_sha256": value.route_set_sha256,
        }

    def issue(
        authority: object,
        consumption: object,
        *,
        route_set_sha256: str | None = None,
    ) -> Wave1ExecutionCapability:
        from src.analysis.open_intelligence.execution_approval import (
            ExecutionConsumption,
            ExecutionConsumptionV2,
            _require_consumed_execution,
        )
        from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SET_SHA256

        if route_set_sha256 is not None and route_set_sha256 != WAVE1_ROUTE_SET_SHA256:
            raise Wave1ExecutionBlocked("Wave 1 route set digest is not the approved route table")

        generation = getattr(authority, "generation", None)
        if type(consumption) is ExecutionConsumptionV2:
            _verify_wave1_consumption_v2(authority, consumption, generation)
        elif type(consumption) is ExecutionConsumption and generation is None:
            # Retained v1 record under a v1 authority: the job it names is the retained
            # pilot job, which no fresh authority can issue.
            expected_job = _RETAINED_WAVE1_JOB
            if (
                consumption.operation != "wave1_pilot"
                or consumption.job_resource != expected_job
                or not consumption.execution_name.startswith(expected_job + "/executions/")
            ):
                raise Wave1ExecutionBlocked("Wave 1 durable consumption is invalid")
        elif type(consumption) is ExecutionConsumption:
            raise Wave1ExecutionBlocked("Wave 1 consumption family differs from the authority")
        else:
            raise Wave1ExecutionBlocked("Wave 1 durable consumption is invalid")
        try:
            _require_consumed_execution(authority, consumption, "wave1_pilot")
            manifest = authority.manifest
            gdelt_digest = dict(manifest.input_artifacts)["gdelt_dry_run_set"]
            maximum_bytes_billed = manifest.limits["max_bytes_billed"]
        except Exception as error:
            raise Wave1ExecutionBlocked("Wave 1 durable consumption is invalid") from error
        if (
            not isinstance(gdelt_digest, str)
            or _DIGEST.fullmatch(gdelt_digest) is None
            or maximum_bytes_billed != 50_000_000_000
        ):
            raise Wave1ExecutionBlocked("Wave 1 durable manifest is invalid")
        capability = object.__new__(Wave1ExecutionCapability)
        values = {
            "consumption_id": consumption.consumption_id,
            "manifest_sha256": consumption.manifest_sha256,
            "execution_name": consumption.execution_name,
            "source_sha": consumption.source_sha,
            "image_uri": consumption.image_uri,
            "gdelt_dry_run_set_sha256": gdelt_digest,
            "maximum_bytes_billed": maximum_bytes_billed,
            "route_set_sha256": route_set_sha256,
        }
        for field, item in values.items():
            object.__setattr__(capability, field, item)
        digest = canonical_digest(values)
        object.__setattr__(capability, "capability_digest", digest)
        identity = id(capability)

        def cleanup(_reference):
            registry.pop(identity, None)

        registry[identity] = (weakref.ref(capability, cleanup), digest)
        return capability

    def require(value: object, action: str) -> Wave1ExecutionCapability:
        if type(value) is not Wave1ExecutionCapability:
            raise Wave1ExecutionBlocked(f"{action}: Wave 1 capability is unavailable")
        try:
            digest = canonical_digest(payload(value))
            entry = registry.get(id(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise Wave1ExecutionBlocked(f"{action}: Wave 1 capability is unavailable") from error
        if (
            entry is None
            or entry[0]() is not value
            or entry[1] != digest
            or value.capability_digest != digest
        ):
            raise Wave1ExecutionBlocked(f"{action}: Wave 1 capability is unavailable")
        return value

    return issue, require


_issue_wave1_execution_capability, _require_wave1_execution_capability = _wave1_capability_state()
del _wave1_capability_state


def _decimal(value: object, field: str, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a nonnegative Decimal")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lower case SHA256 digest")
    return value


@dataclass(frozen=True, slots=True)
class FundedLaneRules:
    credential_lane: str
    opening_balance: Decimal
    monthly_cap: Decimal
    reserve_floor: Decimal
    stage_caps: Mapping[int, Decimal]
    catalog_digest: str
    metadata_digest: str

    def __post_init__(self) -> None:
        if self.credential_lane != FUNDED_CREDENTIAL_LANE:
            raise ValueError("credential lane is not approved")
        opening = _decimal(self.opening_balance, "opening balance")
        monthly_cap = _decimal(self.monthly_cap, "monthly cap")
        reserve = _decimal(self.reserve_floor, "reserve floor")
        if opening != OPENING_BALANCE:
            raise ValueError("opening balance differs from the approved activation baseline")
        if monthly_cap != MONTHLY_CAP:
            raise ValueError("monthly cap differs from the approved contract")
        if reserve != RESERVE_FLOOR:
            raise ValueError("reserve floor differs from the approved contract")
        if not isinstance(self.stage_caps, Mapping) or dict(self.stage_caps) != dict(STAGE_CAPS):
            raise ValueError("stage caps differ from the approved activation ladder")
        object.__setattr__(self, "stage_caps", MappingProxyType(dict(STAGE_CAPS)))
        digest = _digest(self.catalog_digest, "catalog digest")
        if digest != APPROVED_CATALOG_DIGEST:
            raise ValueError("catalog digest differs from the approved snapshot")
        metadata_digest = _digest(self.metadata_digest, "metadata digest")
        if metadata_digest != APPROVED_METADATA_DIGEST:
            raise ValueError("metadata digest differs from the approved snapshot")


@dataclass(frozen=True, slots=True)
class MonthlySpendSnapshot:
    as_of: datetime
    month_start: date
    current_balance: Decimal | None
    month_opening_balance: Decimal
    monthly_ledger_debit: Decimal
    monthly_vendor_reported: Decimal
    unreconciled_execution_ids: tuple[str, ...]
    activation_stage: int
    consecutive_complete_runs: int
    runs_today: int
    catalog_digest: str
    metadata_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.as_of, datetime)
            or self.as_of.tzinfo is None
            or self.as_of.utcoffset() != timedelta(0)
        ):
            raise ValueError("as of must be a UTC timestamp")
        if isinstance(self.month_start, datetime) or not isinstance(self.month_start, date):
            raise ValueError("month start must be a date")
        expected_start = date(self.as_of.year, self.as_of.month, 1)
        if self.month_start != expected_start:
            raise ValueError("month start does not match the UTC calendar month")
        object.__setattr__(
            self,
            "current_balance",
            _decimal(self.current_balance, "current balance", nullable=True),
        )
        month_opening = _decimal(self.month_opening_balance, "month opening balance")
        if month_opening > OPENING_BALANCE:
            raise ValueError("month opening balance exceeds the activation baseline")
        ledger = _decimal(self.monthly_ledger_debit, "monthly ledger debit")
        vendor = _decimal(self.monthly_vendor_reported, "monthly vendor reported")
        if ledger < vendor:
            raise ValueError("monthly ledger debit cannot be below vendor reported spend")
        if not isinstance(self.unreconciled_execution_ids, tuple):
            raise ValueError("unreconciled execution IDs must be a tuple")
        executions = tuple(self.unreconciled_execution_ids)
        if len(executions) != len(set(executions)) or any(
            not isinstance(item, str) or _EXECUTION_ID.fullmatch(item) is None
            for item in executions
        ):
            raise ValueError("unreconciled execution IDs are invalid")
        stage = _integer(self.activation_stage, "activation stage")
        if stage not in STAGE_CAPS:
            raise ValueError("activation stage is unsupported")
        object.__setattr__(
            self,
            "consecutive_complete_runs",
            _integer(self.consecutive_complete_runs, "consecutive complete runs"),
        )
        object.__setattr__(self, "runs_today", _integer(self.runs_today, "runs today"))
        object.__setattr__(self, "catalog_digest", _digest(self.catalog_digest, "catalog digest"))
        object.__setattr__(
            self, "metadata_digest", _digest(self.metadata_digest, "metadata digest")
        )


@dataclass(frozen=True, slots=True)
class FundedLaneGateResult:
    allowed: bool
    reasons: tuple[str, ...]
    activation_stage: int
    stage_cap: Decimal
    current_balance: Decimal | None
    month_opening_balance: Decimal
    monthly_ledger_debit: Decimal
    monthly_vendor_reported: Decimal
    monthly_balance_delta: Decimal | None
    monthly_effective_spend: Decimal
    monthly_remaining: Decimal
    reserve_remaining: Decimal
    run_allowance: Decimal
    attribution_gap_required: Decimal
    attribution_state: str
    consecutive_complete_runs: int
    runs_today: int
    unreconciled_execution_ids: tuple[str, ...]
    catalog_digest: str
    metadata_digest: str

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("allowed must be boolean")
        reasons = tuple(sorted(self.reasons))
        if any(not isinstance(item, str) or not item for item in reasons):
            raise ValueError("reasons are invalid")
        if len(reasons) != len(set(reasons)):
            raise ValueError("reasons must be unique")
        object.__setattr__(self, "reasons", reasons)
        if self.attribution_state not in {"complete", "conservative", "gap_detected"}:
            raise ValueError("attribution state is unsupported")
        stage = _integer(self.activation_stage, "activation stage")
        if stage not in STAGE_CAPS or self.stage_cap != STAGE_CAPS[stage]:
            raise ValueError("stage and stage cap are inconsistent")
        current = _decimal(self.current_balance, "current balance", nullable=True)
        month_opening = _decimal(self.month_opening_balance, "month opening balance")
        ledger = _decimal(self.monthly_ledger_debit, "monthly ledger debit")
        vendor = _decimal(self.monthly_vendor_reported, "monthly vendor reported")
        balance_delta = _decimal(self.monthly_balance_delta, "monthly balance delta", nullable=True)
        effective = _decimal(self.monthly_effective_spend, "monthly effective spend")
        remaining = _decimal(self.monthly_remaining, "monthly remaining")
        reserve = _decimal(self.reserve_remaining, "reserve remaining")
        allowance = _decimal(self.run_allowance, "run allowance")
        gap = _decimal(self.attribution_gap_required, "attribution gap required")
        if ledger < vendor:
            raise ValueError("ledger debit cannot be below vendor reported spend")
        expected_delta = (
            month_opening - current if current is not None and current <= month_opening else None
        )
        if balance_delta != expected_delta:
            raise ValueError("monthly balance delta is inconsistent")
        expected_effective = max(ledger, balance_delta or Decimal("0"))
        expected_remaining = max(Decimal("0"), MONTHLY_CAP - expected_effective)
        expected_reserve = (
            max(Decimal("0"), current - RESERVE_FLOOR) if current is not None else Decimal("0")
        )
        expected_allowance = min(self.stage_cap, expected_remaining, expected_reserve)
        expected_gap = (
            max(Decimal("0"), balance_delta - ledger) if balance_delta is not None else Decimal("0")
        )
        if (
            effective != expected_effective
            or remaining != expected_remaining
            or reserve != expected_reserve
            or allowance != expected_allowance
            or gap != expected_gap
        ):
            raise ValueError("funded lane financial arithmetic is inconsistent")
        if (
            current is None
            or current > month_opening
            or gap > 0
            or "unreconciled_execution" in reasons
        ):
            expected_state = "gap_detected"
        elif ledger > vendor or (balance_delta is not None and ledger > balance_delta):
            expected_state = "conservative"
        else:
            expected_state = "complete"
        if self.attribution_state != expected_state:
            raise ValueError("attribution state contradicts financial evidence")
        complete_runs = _integer(self.consecutive_complete_runs, "consecutive complete runs")
        runs_today = _integer(self.runs_today, "runs today")
        if not isinstance(self.unreconciled_execution_ids, tuple):
            raise ValueError("unreconciled execution IDs must be a tuple")
        execution_ids = tuple(self.unreconciled_execution_ids)
        if len(execution_ids) != len(set(execution_ids)) or any(
            not isinstance(item, str) or _EXECUTION_ID.fullmatch(item) is None
            for item in execution_ids
        ):
            raise ValueError("unreconciled execution IDs are invalid")
        catalog_digest = _digest(self.catalog_digest, "catalog digest")
        metadata_digest = _digest(self.metadata_digest, "metadata digest")
        required_reasons = _required_reasons(
            activation_stage=stage,
            consecutive_complete_runs=complete_runs,
            runs_today=runs_today,
            current_balance=current,
            month_opening_balance=month_opening,
            monthly_ledger_debit=ledger,
            monthly_vendor_reported=vendor,
            monthly_balance_delta=balance_delta,
            monthly_remaining=remaining,
            reserve_remaining=reserve,
            run_allowance=allowance,
            stage_cap=self.stage_cap,
            attribution_gap_required=gap,
            unreconciled_execution_ids=execution_ids,
            catalog_digest=catalog_digest,
            metadata_digest=metadata_digest,
        )
        if set(reasons) != required_reasons:
            raise ValueError("gate reasons do not match the funded lane evidence")
        if self.allowed != (not required_reasons):
            raise ValueError("allowed contradicts the funded lane evidence")


def _required_reasons(
    *,
    activation_stage: int,
    consecutive_complete_runs: int,
    runs_today: int,
    current_balance: Decimal | None,
    month_opening_balance: Decimal,
    monthly_ledger_debit: Decimal,
    monthly_vendor_reported: Decimal,
    monthly_balance_delta: Decimal | None,
    monthly_remaining: Decimal,
    reserve_remaining: Decimal,
    run_allowance: Decimal,
    stage_cap: Decimal,
    attribution_gap_required: Decimal,
    unreconciled_execution_ids: tuple[str, ...],
    catalog_digest: str,
    metadata_digest: str,
) -> set[str]:
    reasons: set[str] = set()
    if current_balance is None:
        reasons.add("balance_unavailable")
    elif current_balance > month_opening_balance:
        reasons.add("balance_increase_unreconciled")
    elif current_balance <= RESERVE_FLOOR:
        reasons.add("reserve_floor_reached")
    if not approved_wave1_catalog_pair(catalog_digest, metadata_digest):
        reasons.add("catalog_mismatch")
    if activation_stage == 0:
        reasons.add("stage_zero")
    if activation_stage == 1 and consecutive_complete_runs >= 1:
        reasons.add("stage_one_complete")
    if activation_stage == 2 and consecutive_complete_runs < 1:
        reasons.add("stage_two_unproven")
    if activation_stage == 3:
        if consecutive_complete_runs < 3:
            reasons.add("stage_three_unproven")
        if runs_today >= 1:
            reasons.add("daily_run_limit")
    if unreconciled_execution_ids:
        reasons.add("unreconciled_execution")
    if attribution_gap_required > 0:
        reasons.add("attribution_gap_required")
    if monthly_remaining == 0:
        reasons.add("monthly_cap_exhausted")
    if stage_cap > 0 and run_allowance < stage_cap:
        reasons.add("full_stage_allowance_unavailable")
    exact_reconciliation = (
        monthly_balance_delta is not None
        and monthly_ledger_debit == monthly_vendor_reported == monthly_balance_delta
        and not unreconciled_execution_ids
    )
    if activation_stage == 2 and not exact_reconciliation:
        reasons.add("stage_two_reconciliation_not_exact")
    return reasons


def evaluate_funded_lane(
    snapshot: MonthlySpendSnapshot,
    rules: FundedLaneRules,
) -> FundedLaneGateResult:
    if not isinstance(snapshot, MonthlySpendSnapshot):
        raise ValueError("monthly spend snapshot is invalid")
    if not isinstance(rules, FundedLaneRules):
        raise ValueError("funded lane rules are invalid")

    stage_cap = rules.stage_caps[snapshot.activation_stage]
    balance_delta: Decimal | None = None
    if snapshot.current_balance is None:
        reserve_remaining = Decimal("0")
    elif snapshot.current_balance > snapshot.month_opening_balance:
        reserve_remaining = snapshot.current_balance - rules.reserve_floor
    else:
        balance_delta = snapshot.month_opening_balance - snapshot.current_balance
        reserve_remaining = max(Decimal("0"), snapshot.current_balance - rules.reserve_floor)

    gap = Decimal("0")
    if balance_delta is not None and balance_delta > snapshot.monthly_ledger_debit:
        gap = balance_delta - snapshot.monthly_ledger_debit
    effective = max(snapshot.monthly_ledger_debit, balance_delta or Decimal("0"))
    monthly_remaining = max(Decimal("0"), rules.monthly_cap - effective)
    run_allowance = min(stage_cap, monthly_remaining, reserve_remaining)
    reasons = _required_reasons(
        activation_stage=snapshot.activation_stage,
        consecutive_complete_runs=snapshot.consecutive_complete_runs,
        runs_today=snapshot.runs_today,
        current_balance=snapshot.current_balance,
        month_opening_balance=snapshot.month_opening_balance,
        monthly_ledger_debit=snapshot.monthly_ledger_debit,
        monthly_vendor_reported=snapshot.monthly_vendor_reported,
        monthly_balance_delta=balance_delta,
        monthly_remaining=monthly_remaining,
        reserve_remaining=reserve_remaining,
        run_allowance=run_allowance,
        stage_cap=stage_cap,
        attribution_gap_required=gap,
        unreconciled_execution_ids=snapshot.unreconciled_execution_ids,
        catalog_digest=snapshot.catalog_digest,
        metadata_digest=snapshot.metadata_digest,
    )

    if (
        snapshot.current_balance is None
        or snapshot.current_balance > snapshot.month_opening_balance
        or gap > 0
        or snapshot.unreconciled_execution_ids
    ):
        attribution_state = "gap_detected"
    elif snapshot.monthly_ledger_debit > snapshot.monthly_vendor_reported or (
        balance_delta is not None and snapshot.monthly_ledger_debit > balance_delta
    ):
        attribution_state = "conservative"
    else:
        attribution_state = "complete"

    return FundedLaneGateResult(
        allowed=not reasons,
        reasons=tuple(reasons),
        activation_stage=snapshot.activation_stage,
        stage_cap=stage_cap,
        current_balance=snapshot.current_balance,
        month_opening_balance=snapshot.month_opening_balance,
        monthly_ledger_debit=snapshot.monthly_ledger_debit,
        monthly_vendor_reported=snapshot.monthly_vendor_reported,
        monthly_balance_delta=balance_delta,
        monthly_effective_spend=effective,
        monthly_remaining=monthly_remaining,
        reserve_remaining=reserve_remaining,
        run_allowance=run_allowance,
        attribution_gap_required=gap,
        attribution_state=attribution_state,
        consecutive_complete_runs=snapshot.consecutive_complete_runs,
        runs_today=snapshot.runs_today,
        unreconciled_execution_ids=snapshot.unreconciled_execution_ids,
        catalog_digest=snapshot.catalog_digest,
        metadata_digest=snapshot.metadata_digest,
    )
