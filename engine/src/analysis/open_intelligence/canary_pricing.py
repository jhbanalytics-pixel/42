"""Effective-date pricing for the approved Gemini canary models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CanaryPrice:
    model: str
    input_per_million: Decimal
    output_per_million: Decimal
    pricing_version: str


@dataclass(frozen=True, slots=True)
class _Schedule:
    model: str
    input_per_million: Decimal
    output_per_million: Decimal
    pricing_version: str
    effective_from: datetime
    effective_to: datetime | None


_SCHEDULES = (
    _Schedule(
        "gemini-3.5-flash",
        Decimal("1.50"),
        Decimal("9.00"),
        "gemini_3_5_global_2026_v1",
        datetime(2026, 8, 28, tzinfo=UTC),
        None,
    ),
    _Schedule(
        "gemini-3.7-flash",
        Decimal("0.75"),
        Decimal("3.75"),
        "gemini_3_7_intro_2026_v1",
        datetime(2026, 8, 13, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    ),
    _Schedule(
        "gemini-3.7-flash",
        Decimal("1.50"),
        Decimal("7.50"),
        "gemini_3_7_standard_2027_v1",
        datetime(2027, 1, 1, tzinfo=UTC),
        None,
    ),
)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("pricing timestamp must be timezone-aware UTC")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("pricing timestamp must be UTC")
    return value.astimezone(UTC)


def price_for(model: str, at_utc: datetime) -> CanaryPrice:
    at_utc = _utc(at_utc)
    matches = [
        item
        for item in _SCHEDULES
        if item.model == model
        and item.effective_from <= at_utc
        and (item.effective_to is None or at_utc < item.effective_to)
    ]
    if len(matches) != 1:
        raise ValueError("pricing window is missing, overlapping, or unknown")
    item = matches[0]
    return CanaryPrice(
        model=item.model,
        input_per_million=item.input_per_million,
        output_per_million=item.output_per_million,
        pricing_version=item.pricing_version,
    )


def estimate_cost_usd(price: CanaryPrice, prompt_tokens: int, output_tokens: int) -> Decimal:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (prompt_tokens, output_tokens)
    ):
        raise ValueError("token counts must be nonnegative integers")
    million = Decimal("1000000")
    return (
        Decimal(prompt_tokens) * price.input_per_million
        + Decimal(output_tokens) * price.output_per_million
    ) / million


__all__ = ["CanaryPrice", "estimate_cost_usd", "price_for"]
