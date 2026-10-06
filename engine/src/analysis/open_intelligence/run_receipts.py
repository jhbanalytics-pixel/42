"""The shared immutable commit marker for one exact Open Intelligence run.

A receipt is written last, after every row family has been written and read
back. Nothing downstream may present a dynamic signal for display unless a
receipt proves the run closed, its partitions are complete, its observation
window is closed, and an operator released it.

This module owns the only run-receipt type. Scoring qualification and the desk
producer both import it rather than defining a second one, so a run can never
be described two different ways.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from src.analysis.open_intelligence.brain_contract import canonical_digest

RUN_RECEIPT_CONTRACT_VERSION = "open_intelligence_run_receipt_v1"

CANONICAL_MARKETS = ("za", "ng", "ke")
QA_CLIENT_SCOPE_ID = "qa_canary"

RUN_RECEIPT_STATUSES = ("completed", "failed")
RUN_RECEIPT_RELEASE_STATES = ("blocked", "enabled")

RUN_RECEIPT_ROW_FIELDS = (
    "run_contract_version",
    "run_id",
    "client_scope_id",
    "market_scope",
    "signal_date",
    "observation_start",
    "observation_end",
    "observation_method",
    "source_window_digest",
    "cluster_build_version",
    "source_family_map_version",
    "rule_version",
    "status",
    "complete_partitions",
    "display_release_state",
    "candidate_count",
    "evidence_count",
    "membership_count",
    "lineage_count",
    "analysis_count",
    "prediction_count",
    "row_set_digest",
    "source_sha",
    "completed_at",
)

_COUNT_FIELDS = (
    "candidate_count",
    "evidence_count",
    "membership_count",
    "lineage_count",
    "analysis_count",
    "prediction_count",
)

_DIGEST_FIELDS = ("source_window_digest", "row_set_digest")

_REQUIRED_TEXT_FIELDS = (
    "run_id",
    "client_scope_id",
    "observation_method",
    "cluster_build_version",
    "source_family_map_version",
    "rule_version",
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


class RunReceiptError(ValueError):
    """Raised when a run receipt cannot prove its own contract."""


@dataclass(frozen=True, slots=True)
class OpenIntelligenceRunReceipt:
    """One immutable completion marker. Natural key: run_id."""

    run_contract_version: str
    run_id: str
    client_scope_id: str
    market_scope: tuple[str, ...]
    signal_date: date
    observation_start: date
    observation_end: date
    observation_method: str
    source_window_digest: str
    cluster_build_version: str
    source_family_map_version: str
    rule_version: str
    status: str
    complete_partitions: bool
    display_release_state: str
    candidate_count: int
    evidence_count: int
    membership_count: int
    lineage_count: int
    analysis_count: int
    prediction_count: int
    row_set_digest: str
    source_sha: str
    completed_at: datetime


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunReceiptError(f"{field} must be a nonempty string")
    return value


def _count(value: object, field: str) -> int:
    # A bool is an int in Python. A boolean where a count belongs is a caller
    # mistake that would otherwise silently record 0 or 1 rows.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunReceiptError(f"{field} must be an integer count")
    if value < 0:
        raise RunReceiptError(f"{field} must not be negative")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise RunReceiptError(f"{field} must be 64 lower case hexadecimal characters")
    return value


def _market_scope(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise RunReceiptError("market_scope must be a sequence of market codes")
    scope = tuple(value)
    if not scope:
        raise RunReceiptError("market_scope must name at least one market")
    unknown = [market for market in scope if market not in CANONICAL_MARKETS]
    if unknown:
        raise RunReceiptError(f"market_scope contains unknown markets: {unknown}")
    if len(set(scope)) != len(scope):
        raise RunReceiptError("market_scope must not repeat a market")
    canonical = tuple(market for market in CANONICAL_MARKETS if market in scope)
    if scope != canonical:
        raise RunReceiptError(f"market_scope must be in canonical order {canonical}, got {scope}")
    return scope


def _utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise RunReceiptError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise RunReceiptError(f"{field} must be timezone aware")
    return value.astimezone(UTC)


def _observation_date(value: object, field: str) -> date:
    # A datetime is a date subclass, so an accidental timestamp would pass a
    # bare isinstance check and silently widen the window.
    if isinstance(value, datetime) or not isinstance(value, date):
        raise RunReceiptError(f"{field} must be a date")
    return value


def build_run_receipt(**fields: object) -> OpenIntelligenceRunReceipt:
    """Validate every contract rule, then return the frozen receipt."""
    missing = [name for name in RUN_RECEIPT_ROW_FIELDS if name not in fields]
    if missing:
        raise RunReceiptError(f"run receipt is missing required fields: {missing}")
    unexpected = [name for name in fields if name not in RUN_RECEIPT_ROW_FIELDS]
    if unexpected:
        raise RunReceiptError(f"run receipt carries unknown fields: {unexpected}")

    if fields["run_contract_version"] != RUN_RECEIPT_CONTRACT_VERSION:
        raise RunReceiptError(
            "run_contract_version must be exactly "
            f"{RUN_RECEIPT_CONTRACT_VERSION!r}, got {fields['run_contract_version']!r}"
        )
    if fields["status"] not in RUN_RECEIPT_STATUSES:
        raise RunReceiptError(f"status must be one of {RUN_RECEIPT_STATUSES}")
    if fields["display_release_state"] not in RUN_RECEIPT_RELEASE_STATES:
        raise RunReceiptError(f"display_release_state must be one of {RUN_RECEIPT_RELEASE_STATES}")
    if not isinstance(fields["complete_partitions"], bool):
        raise RunReceiptError("complete_partitions must be a boolean")
    if not isinstance(fields["source_sha"], str) or not _COMMIT_SHA.fullmatch(fields["source_sha"]):
        raise RunReceiptError("source_sha must be 40 lower case hexadecimal characters")

    signal_date = _observation_date(fields["signal_date"], "signal_date")
    observation_start = _observation_date(fields["observation_start"], "observation_start")
    observation_end = _observation_date(fields["observation_end"], "observation_end")
    if observation_start > observation_end:
        raise RunReceiptError("observation_start must not be later than observation_end")
    if signal_date != observation_end:
        raise RunReceiptError("signal_date must equal observation_end")

    resolved: dict[str, object] = {
        "run_contract_version": fields["run_contract_version"],
        "market_scope": _market_scope(fields["market_scope"]),
        "signal_date": signal_date,
        "observation_start": observation_start,
        "observation_end": observation_end,
        "status": fields["status"],
        "complete_partitions": fields["complete_partitions"],
        "display_release_state": fields["display_release_state"],
        "source_sha": fields["source_sha"],
        "completed_at": _utc_timestamp(fields["completed_at"], "completed_at"),
    }
    for name in _REQUIRED_TEXT_FIELDS:
        resolved[name] = _text(fields[name], name)
    for name in _DIGEST_FIELDS:
        resolved[name] = _digest(fields[name], name)
    for name in _COUNT_FIELDS:
        resolved[name] = _count(fields[name], name)

    return OpenIntelligenceRunReceipt(**resolved)  # type: ignore[arg-type]


def run_receipt_digest(receipt: OpenIntelligenceRunReceipt) -> str:
    """The canonical digest binding every field of one receipt."""
    if not isinstance(receipt, OpenIntelligenceRunReceipt):
        raise RunReceiptError("run_receipt_digest requires a run receipt")
    return canonical_digest(receipt)


def receipt_qualifies_for_display(
    receipt: OpenIntelligenceRunReceipt,
    *,
    requested_markets: Sequence[str],
    today: date,
) -> bool:
    """True only when this run may release signals for the requested markets.

    Every clause is a separate reason to refuse. Nothing here repairs, defaults
    or widens a value; an unprovable run is simply not eligible.
    """
    if not isinstance(receipt, OpenIntelligenceRunReceipt):
        raise RunReceiptError("receipt_qualifies_for_display requires a run receipt")
    markets = _market_scope(requested_markets)
    return (
        receipt.run_contract_version == RUN_RECEIPT_CONTRACT_VERSION
        and receipt.client_scope_id != QA_CLIENT_SCOPE_ID
        and receipt.status == "completed"
        and receipt.complete_partitions
        and receipt.display_release_state == "enabled"
        and receipt.observation_end < today
        and set(markets).issubset(receipt.market_scope)
    )


def select_display_run(
    receipts: Iterable[OpenIntelligenceRunReceipt],
    *,
    requested_markets: Sequence[str],
    today: date,
) -> OpenIntelligenceRunReceipt | None:
    """The one run that may be displayed, or None.

    None means no run could be proved. It never means an empty result set was
    observed, and a blocked or failed run is never returned as a consolation.
    """
    candidates = tuple(receipts)
    by_run_id: dict[str, OpenIntelligenceRunReceipt] = {}
    for receipt in candidates:
        seen = by_run_id.get(receipt.run_id)
        if seen is not None and run_receipt_digest(seen) != run_receipt_digest(receipt):
            raise RunReceiptError(
                f"duplicate run_id {receipt.run_id!r} carries unequal receipt content"
            )
        by_run_id[receipt.run_id] = receipt

    qualifying = [
        receipt
        for receipt in by_run_id.values()
        if receipt_qualifies_for_display(receipt, requested_markets=requested_markets, today=today)
    ]
    if not qualifying:
        return None
    return max(
        qualifying,
        key=lambda receipt: (
            receipt.observation_end,
            receipt.completed_at,
            receipt.run_id,
        ),
    )
