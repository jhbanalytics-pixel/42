"""Vertex Gemini cost watchdog.

Costs every Vertex Gemini consumer off ONE basis, the `gemini_usage` ledger,
projects a monthly run rate at each call's own model rate, checks that total
against the billing export, and flags drift over the policy threshold.

Why this exists
===============
The watchdog used to cost off three bases at once. It summed token columns on
the three tables that happen to carry them (trend_analysis, daily_summary,
seed_insights), read the gemini_usage ledger for the two stages that persist no
table (comment_sentiment, driving_hashtags), and ESTIMATED the reconcile shadow
from a hardcoded per-call token guess. Measured on 2026-08-24 that patchwork
read $4.73 over 7 days, projecting to about $20 a month, GREEN, while the
billing export put the Vertex AI line at $24.40 month to date and projected
$32.89 from closed days.

Two faults, in opposite directions, hid inside one number:

  - comment_sentiment wrote no ledger row at all, so a live stage counted zero.
  - seed_insights stamps ONE call's tokens onto EVERY seed row it writes
    (generate_seed_intelligence._rows), so SUM(prompt_tokens) over that table
    priced a single call once per seed. Measured over 2026-07-25..08-24 that
    SUM reports 533,373 prompt tokens against a true 177,791, exactly 3.0x,
    because the pass writes three seed rows a day.

Every consumer now writes its real per-call tokens to the ledger
(src/utils/gemini_usage), including the reconcile shadow, whose spend used to be
the estimate. Nothing here is estimated any more.

Reconciling against billing
===========================
A ledger can only report what its writers record, so a metering gap is invisible
from inside it. The report therefore compares its own total against the Vertex AI
line in the billing export over the days both sides have settled, and when the
ledger cannot account for what Vertex billed it says so, names who could be
responsible, and floors the status at YELLOW. A GREEN projection that cannot
explain the bill is not GREEN.

Not every Vertex biller fits a token ledger. src/enrichment/embedding_classifier.py
calls Vertex embed_content, priced per billable CHARACTER rather than per token,
so it is named in the discrepancy note rather than metered here.

Pricing is sourced from the single canonical table in
src/analysis/gemini_client (_MODEL_PRICING), so the watchdog and the per-call log
line can never drift apart. gemini-3.5-flash is $1.50 per 1M input and $9.00 per
1M output, output including billed thinking tokens.

Read-only. No BQ writes, no Vertex calls, no workflow triggers. Composes into
morning-check as the VERTEX-COST block.

CLI:
    python scripts/vertex_cost_watchdog.py [--window 7] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from numbers import Integral, Real
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Single source of truth for per-1M (input, output) USD rates. Importing the
# canonical table means a price change in gemini_client flows here with no
# second edit, so the watchdog never prices a call differently from the
# per-call log line it is auditing.
from src.analysis.gemini_client import (
    _FALLBACK_PRICING,
    _MODEL_PRICING,
)

# The writers' own declared consumer set, not a copy of it. A hand-maintained
# second list is exactly how a stage ends up billing and never being counted.
from src.utils.gemini_usage import (
    _NEW_CONSUMER_STAGES,
    GEMINI_USAGE_TABLE,
    USAGE_CONSUMERS,
)

EXPECTED_CONSUMERS = USAGE_CONSUMERS
NEW_CONSUMER_STAGES = _NEW_CONSUMER_STAGES
OPEN_INTELLIGENCE_MODEL = "gemini-3.5-flash"
STAGING_USAGE_DATASET = "ogilvy-trends-v2.trends_v2_staging"

# --- Policy thresholds (monthly run rate, USD) --------------------------------
# These are the ALERT policy, not a description of current spend. Keep them
# deliberate: do NOT raise a threshold just because measured spend now exceeds
# it. The watchdog's job is to report the ACCURATE number, not to move the
# goalposts. The billing export's 2026-08-24 projection for the Vertex line was
# $32.89, which reads RED here, and that is the correct outcome.
STEADY_STATE_BASELINE_USD_PER_MONTH = 28.0
WATCH_THRESHOLD_USD_PER_MONTH = 22.0
ALERT_THRESHOLD_USD_PER_MONTH = 30.0

# Days in a month, for the window -> monthly-run-rate projection.
_DAYS_PER_MONTH = 30

# --- Billing reconciliation ---------------------------------------------------
# The billing export lags: the most recent day or two are still settling, and a
# live ledger read against a half-written billing day invents a gap that is not
# real. Only days this far back count as closed.
BILLING_LAG_DAYS = 2

# Billing export lives in its own dataset, not the pipeline dataset.
BILLING_DATASET = "billing_export"
BILLING_TABLE_WILDCARD = "gcp_billing_export_v1_*"
# The service line Gemini bills under. Everything Vertex bills lands here,
# including the per-character embedding spend this token ledger does not carry.
BILLING_SERVICE = "Vertex AI"

# How far the ledger may fall short of billing before it reads as a metering
# gap. Wide enough to absorb rounding, sustained-use discounts and the settling
# tail; narrow enough that a whole missing consumer trips it.
DISCREPANCY_TOLERANCE = 0.15

# Vertex billers that bill on the same service line and CANNOT appear in a
# token ledger, named in the discrepancy note so a persistent gap is attributed
# rather than left as a mystery.
KNOWN_UNMETERED_VERTEX_BILLERS: tuple[tuple[str, str], ...] = (
    (
        "embedding_classifier",
        "src/enrichment/embedding_classifier.py, Vertex embed_content on "
        "text-multilingual-embedding-002, billed per character not per token",
    ),
)


def price_for(model: str | None) -> tuple[float, float]:
    """(input, output) per-1M USD rate for a model id.

    Unknown or NULL model id prices at the fallback (3.5-flash) rate so the
    ledger never under-reports on an unrecognised or missing id.
    """
    if not model:
        return _FALLBACK_PRICING
    return _MODEL_PRICING.get(model, _FALLBACK_PRICING)


def cost_usd(model: str | None, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost of a token bundle at a model's rates."""
    in_rate, out_rate = price_for(model)
    return (prompt_tokens / 1_000_000.0) * in_rate + (completion_tokens / 1_000_000.0) * out_rate


@dataclass
class ConsumerSpend:
    """One Gemini consumer's measured spend over the window."""

    name: str
    calls: int
    active_days: int
    prompt_tokens: int
    completion_tokens: int
    window_cost: float

    @property
    def monthly_cost(self) -> float:
        """Project to a monthly run rate off the consumer's OWN active days.

        Projecting per-consumer (not off a fixed window length) keeps the
        number honest while a consumer is ramping: a stage dark for four of
        seven days is not diluted across the days it did not fire.
        """
        if self.active_days <= 0:
            return 0.0
        return (self.window_cost / self.active_days) * _DAYS_PER_MONTH


@dataclass
class Discrepancy:
    """The ledger's total against the billing export's, over shared days.

    ``within_tolerance`` is None when nothing could be compared (no day is
    settled on both sides). None is not a pass: it means no verdict, and the
    report must not present it as one.
    """

    ledger_cost: float
    billing_cost: float
    gap_usd: float
    gap_pct: float
    days_compared: int
    within_tolerance: bool | None


@dataclass(frozen=True, slots=True)
class PerCallCeiling:
    consumer: str
    model: str
    input_tokens: int
    output_tokens: int
    maximum_usd_per_call: float


@dataclass(frozen=True, slots=True)
class EnablementResult:
    ready: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UsageLedgerRead:
    rows: tuple[dict, ...]
    notes: tuple[str, ...]
    per_call_columns: bool


@dataclass(frozen=True, slots=True)
class CombinedUsageLedgerRead:
    base: UsageLedgerRead
    staging: UsageLedgerRead

    @property
    def rows(self) -> tuple[dict, ...]:
        return self.base.rows + self.staging.rows


@dataclass(frozen=True, slots=True)
class PersistenceFaultRead:
    fault: bool | None
    notes: tuple[str, ...]


@dataclass
class CostReport:
    window_days: int
    window_start: str
    window_end: str
    consumers: list[ConsumerSpend] = field(default_factory=list)
    # Coverage warnings: a consumer that bills Vertex but could not be counted.
    # Rendered on their own lines so an incomplete total is never read as a
    # complete one.
    notes: list[str] = field(default_factory=list)
    discrepancy: Discrepancy | None = None
    enablement: EnablementResult | None = None

    @property
    def window_cost(self) -> float:
        return sum(c.window_cost for c in self.consumers)

    @property
    def monthly_cost(self) -> float:
        return sum(c.monthly_cost for c in self.consumers)

    @property
    def status(self) -> str:
        """GREEN / YELLOW / RED, floored at YELLOW by an unexplained bill.

        A projection that cannot account for what Vertex billed is incomplete,
        not healthy, so it never reads GREEN. A genuine threshold breach still
        outranks the floor.
        """
        base = verdict(self.monthly_cost)
        unexplained = self.discrepancy is not None and self.discrepancy.within_tolerance is False
        if base == "GREEN" and unexplained:
            return "YELLOW"
        return base

    def to_dict(self) -> dict:
        enablement = self.enablement or EnablementResult(
            False, ("staging enablement was not evaluated",)
        )
        return {
            "window_days": self.window_days,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "consumers": [
                asdict(c) | {"monthly_cost": round(c.monthly_cost, 2)} for c in self.consumers
            ],
            "window_cost": round(self.window_cost, 2),
            "projected_monthly": round(self.monthly_cost, 2),
            "coverage_notes": list(self.notes),
            "billing_reconciliation": (
                asdict(self.discrepancy) if self.discrepancy is not None else None
            ),
            "per_call_ceilings": [asdict(row) for row in per_call_ceiling_rows()],
            "staging_enablement": {
                "ready": enablement.ready,
                "reasons": list(enablement.reasons),
            },
            "status": self.status,
            "alert_threshold": ALERT_THRESHOLD_USD_PER_MONTH,
        }


def verdict(monthly_usd: float) -> str:
    """GREEN / YELLOW / RED against the policy thresholds."""
    if monthly_usd >= ALERT_THRESHOLD_USD_PER_MONTH:
        return "RED"
    if monthly_usd >= WATCH_THRESHOLD_USD_PER_MONTH:
        return "YELLOW"
    return "GREEN"


def _int0(value: object) -> int:
    """Coerce a possibly-NULL BQ number to int, NaN included.

    prompt_tokens / calls are nullable, and a SUM() over an all-NULL group comes
    back as pd.NA / NaN through pandas. ``int(nan or 0)`` RAISES, because NaN is
    truthy, so the `or 0` idiom used elsewhere is not safe here.
    """
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n


def per_call_ceiling_rows() -> tuple[PerCallCeiling, ...]:
    specs = (
        ("dynamic_signal_summary", 8_000, 800),
        ("open_question_answer", 32_000, 4_000),
    )
    return tuple(
        PerCallCeiling(
            consumer=consumer,
            model=OPEN_INTELLIGENCE_MODEL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            maximum_usd_per_call=cost_usd(OPEN_INTELLIGENCE_MODEL, input_tokens, output_tokens),
        )
        for consumer, input_tokens, output_tokens in specs
    )


def ledger_fault_counts(rows: Sequence[dict]) -> tuple[int, int]:
    return (
        sum(_int0(row.get("invalid_events")) for row in rows),
        sum(_int0(row.get("duplicate_events")) for row in rows),
    )


def ledger_integrity_notes(rows: Sequence[dict]) -> tuple[str, ...]:
    notes: list[str] = []
    for row in rows:
        identity = (
            f"{row.get('consumer') or 'unknown'} / "
            f"{row.get('model') or 'unknown model'} / {row.get('day') or 'unknown day'}"
        )
        invalid = _int0(row.get("invalid_events"))
        duplicate = _int0(row.get("duplicate_events"))
        if invalid:
            notes.append(f"{identity}: {invalid} invalid per-call event(s), a metering fault.")
        if duplicate:
            notes.append(
                f"{identity}: {duplicate} duplicate physical event row(s), "
                "a ledger integrity fault."
            )
    return tuple(notes)


def staging_enablement_result(
    *,
    declared_consumers: Sequence[str],
    per_call_columns: bool,
    ledger_notes: Sequence[str],
    billing_notes: Sequence[str],
    invalid_events: int,
    duplicate_events: int,
    discrepancy: Discrepancy | None,
    projected_monthly: object,
    valid_new_consumers: Collection[str],
    observed_call_consumers: Collection[str] | None,
    persistence_fault: bool,
) -> EnablementResult:
    reasons: list[str] = []
    missing_registry = [
        consumer for consumer in NEW_CONSUMER_STAGES if consumer not in declared_consumers
    ]
    if missing_registry:
        reasons.append(
            f"New consumers not declared in the usage registry: {', '.join(missing_registry)}"
        )
    if not per_call_columns:
        reasons.append("Required per-call columns are unavailable in gemini_usage")
    reasons.extend(str(note) for note in ledger_notes)
    reasons.extend(str(note) for note in billing_notes)
    if invalid_events:
        reasons.append(f"{invalid_events} invalid per-call event(s) are a metering fault")
    if duplicate_events:
        reasons.append(
            f"{duplicate_events} duplicate per-call event row(s) are a ledger integrity fault"
        )
    if persistence_fault:
        reasons.append("A metering persistence fault was observed")
    if discrepancy is None:
        reasons.append("Settled billing discrepancy unavailable")
    elif discrepancy.days_compared <= 0:
        reasons.append("Billing comparison has no settled overlapping day")
    elif discrepancy.within_tolerance is None:
        reasons.append("Settled billing verdict unknown")
    elif discrepancy.within_tolerance is False:
        reasons.append("Settled billing discrepancy is outside tolerance")
    projection_is_valid = (
        not isinstance(projected_monthly, bool)
        and isinstance(projected_monthly, Real)
        and math.isfinite(float(projected_monthly))
        and float(projected_monthly) >= 0
    )
    if not projection_is_valid:
        reasons.append("Projected monthly cost is not a finite nonnegative number")
    elif float(projected_monthly) >= ALERT_THRESHOLD_USD_PER_MONTH:
        reasons.append(
            f"Projected monthly cost is at or above ${ALERT_THRESHOLD_USD_PER_MONTH:.2f}"
        )
    if observed_call_consumers:
        for consumer in NEW_CONSUMER_STAGES:
            if consumer not in valid_new_consumers:
                reasons.append(
                    f"{consumer} has no valid per-call ledger event after calls were observed"
                )
    return EnablementResult(ready=not reasons, reasons=tuple(reasons))


def usage_ledger_spends(rows: list[dict]) -> list[ConsumerSpend]:
    """Group gemini_usage rows into one ConsumerSpend per consumer. Pure.

    Each row is ``{consumer, model, day, calls, prompt_in, prompt_out}`` and is
    priced at its OWN model rate, so a window spanning a model cutover prices
    each side correctly, and the reconcile shadow's cheaper model is not priced
    at the brief model's rate. ``active_days`` counts the distinct days that
    consumer actually fired.

    Consumers are returned in EXPECTED_CONSUMERS order, then any unknown
    consumer name alphabetically, so a new writer shows up instead of being
    silently dropped.
    """
    grouped: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("consumer") or "unknown")
        g = grouped.setdefault(
            name,
            {"calls": 0, "prompt_in": 0, "prompt_out": 0, "days": set(), "cost": 0.0},
        )
        prompt_in = _int0(row.get("prompt_in"))
        prompt_out = _int0(row.get("prompt_out"))
        g["calls"] += _int0(row.get("calls"))
        g["prompt_in"] += prompt_in
        g["prompt_out"] += prompt_out
        g["cost"] += cost_usd(row.get("model"), prompt_in, prompt_out)
        if row.get("day") is not None:
            g["days"].add(str(row["day"]))
    order = {name: i for i, name in enumerate(EXPECTED_CONSUMERS)}
    out: list[ConsumerSpend] = []
    for name in sorted(grouped, key=lambda n: (order.get(n, len(order)), n)):
        g = grouped[name]
        out.append(
            ConsumerSpend(
                name=name,
                calls=g["calls"],
                active_days=len(g["days"]),
                prompt_tokens=g["prompt_in"],
                completion_tokens=g["prompt_out"],
                window_cost=g["cost"],
            )
        )
    return out


def ledger_cost_by_day(rows: list[dict]) -> dict[str, float]:
    """Total ledger cost per day across ALL consumers. Pure.

    This is the side of the billing comparison that comes from the ledger. It
    is per-day because the billing export settles per day, and only settled
    days may be compared.
    """
    by_day: dict[str, float] = {}
    for row in rows:
        if row.get("day") is None:
            continue
        day = str(row["day"])
        by_day[day] = by_day.get(day, 0.0) + cost_usd(
            row.get("model"), _int0(row.get("prompt_in")), _int0(row.get("prompt_out"))
        )
    return by_day


def missing_ledger_consumers(spends: list[ConsumerSpend]) -> list[str]:
    """Expected consumers that wrote no ledger row over the window.

    A live stage with no ledger row is a dark stage or a broken writer, not
    proof of zero spend, so the caller reports it as a coverage gap.
    """
    seen = {c.name for c in spends}
    return [name for name in EXPECTED_CONSUMERS if name not in seen]


def scope_aware_missing_consumers(
    missing: Sequence[str], observed_new_consumers: Collection[str]
) -> list[str]:
    """Missing consumers relevant to the current observed-call scope."""
    return [
        consumer
        for consumer in missing
        if consumer not in NEW_CONSUMER_STAGES or observed_new_consumers
    ]


def closed_window(window_days: int, today: date) -> tuple[date, date]:
    """The (start, end) dates over which billing has settled. Pure.

    The end is pulled back by BILLING_LAG_DAYS: comparing a live ledger against
    a billing day that is still being written reads as a gap that is not real.
    """
    return today - timedelta(days=window_days), today - timedelta(days=BILLING_LAG_DAYS)


def reconcile_against_billing(
    ledger_by_day: dict[str, float],
    billing_by_day: dict[str, float],
    tolerance: float = DISCREPANCY_TOLERANCE,
) -> Discrepancy:
    """Compare the ledger's total against billing's, over shared days. Pure.

    Only days present on BOTH sides count. A day the ledger has and billing has
    not settled would otherwise show up as a phantom gap, and the reverse would
    hide a real one.

    The gap is one-directional. A ledger total ABOVE billing is not a metering
    gap; it means a stale rate or a credit, and reporting it as an under-count
    would send the reader hunting for a consumer that is not missing.
    """
    days = sorted(set(ledger_by_day) & set(billing_by_day))
    if not days:
        return Discrepancy(0.0, 0.0, 0.0, 0.0, 0, None)
    ledger_cost = sum(ledger_by_day[d] for d in days)
    billing_cost = sum(billing_by_day[d] for d in days)
    gap_usd = max(0.0, billing_cost - ledger_cost)
    gap_pct = (gap_usd / billing_cost) if billing_cost > 0 else 0.0
    return Discrepancy(
        ledger_cost=ledger_cost,
        billing_cost=billing_cost,
        gap_usd=gap_usd,
        gap_pct=gap_pct,
        days_compared=len(days),
        within_tolerance=gap_pct <= tolerance,
    )


def discrepancy_notes(discrepancy: Discrepancy | None, missing: list[str]) -> list[str]:
    """Lines naming what could account for an unexplained billing gap. Pure.

    Empty when there is no gap to explain. When there is one, it names the
    consumers that wrote nothing (a dark stage or a broken writer) and the
    standing billers that cannot appear in a token ledger at all, so the reader
    starts from a list of suspects rather than from scratch.
    """
    if discrepancy is None or discrepancy.within_tolerance is not False:
        return []
    notes = [
        f"The ledger accounts for ${discrepancy.ledger_cost:.2f} of the "
        f"${discrepancy.billing_cost:.2f} the billing export shows for "
        f"{BILLING_SERVICE} over {discrepancy.days_compared} settled day(s): "
        f"${discrepancy.gap_usd:.2f} ({discrepancy.gap_pct:.0%}) is unaccounted for."
    ]
    if missing:
        notes.append(
            f"No counted {GEMINI_USAGE_TABLE} row in the window: {', '.join(missing)}. "
            "Their spend is unknown, not zero."
        )
    for name, where in KNOWN_UNMETERED_VERTEX_BILLERS:
        notes.append(f"Not metered by design: {name} ({where}).")
    return notes


# --- BQ-touching layer (not unit-tested; validated by the live merge run) -----


def _run_query(sql: str):
    from src.utils.bigquery import run_query

    return run_query(sql)


def _usage_schema_sql(dataset: str) -> str:
    return f"""
    SELECT column_name
    FROM `{dataset}.INFORMATION_SCHEMA.COLUMNS`
    WHERE table_name = '{GEMINI_USAGE_TABLE}'
      AND column_name IN ('run_id', 'stage', 'call_index')
    """


def _legacy_usage_sql(window_days: int, dataset: str) -> str:
    return f"""
    SELECT
      consumer,
      gemini_model AS model,
      trend_date AS day,
      SUM(calls) AS calls,
      SUM(prompt_tokens) AS prompt_in,
      SUM(completion_tokens) AS prompt_out
    FROM `{dataset}.{GEMINI_USAGE_TABLE}`
    WHERE trend_date > DATE_SUB(CURRENT_DATE(), INTERVAL {window_days} DAY)
      AND trend_date <= CURRENT_DATE()
    GROUP BY consumer, model, day
    """


def _base_usage_sql(window_days: int, dataset: str) -> str:
    new_consumers = ", ".join(f"'{name}'" for name in NEW_CONSUMER_STAGES)
    return f"""
    SELECT
      consumer,
      gemini_model AS model,
      trend_date AS day,
      SUM(calls) AS calls,
      SUM(prompt_tokens) AS prompt_in,
      SUM(completion_tokens) AS prompt_out
    FROM `{dataset}.{GEMINI_USAGE_TABLE}`
    WHERE trend_date > DATE_SUB(CURRENT_DATE(), INTERVAL {window_days} DAY)
      AND trend_date <= CURRENT_DATE()
      AND consumer NOT IN ({new_consumers})
    GROUP BY consumer, model, day
    """


def _mixed_usage_sql(window_days: int, dataset: str, *, include_legacy: bool = True) -> str:
    new_consumers = ", ".join(f"'{name}'" for name in NEW_CONSUMER_STAGES)
    source_scope = "" if include_legacy else f"\n        AND consumer IN ({new_consumers})"
    legacy_classification = (
        f"WHEN consumer NOT IN ({new_consumers}) THEN 'legacy'\n          "
        if include_legacy
        else ""
    )
    legacy_cost_rows = (
        """
      SELECT
        consumer,
        gemini_model,
        trend_date,
        calls,
        prompt_tokens,
        completion_tokens,
        0 AS invalid_events,
        0 AS duplicate_events
      FROM classified
      WHERE row_kind = 'legacy'
      UNION ALL
"""
        if include_legacy
        else ""
    )
    stage_pairs = " OR ".join(
        (
            f"consumer = '{consumer}' AND stage = '{next(iter(stages))}'"
            if len(stages) == 1
            else f"consumer = '{consumer}' AND stage IN "
            f"({', '.join(repr(stage) for stage in sorted(stages))})"
        )
        for consumer, stages in NEW_CONSUMER_STAGES.items()
    )
    return f"""
    WITH source AS (
      SELECT
        usage_id,
        run_id,
        stage,
        call_index,
        trend_date,
        consumer,
        gemini_model,
        calls,
        prompt_tokens,
        completion_tokens,
        recorded_at
      FROM `{dataset}.{GEMINI_USAGE_TABLE}`
      WHERE trend_date > DATE_SUB(CURRENT_DATE(), INTERVAL {window_days} DAY)
        AND trend_date <= CURRENT_DATE(){source_scope}
    ),
    classified AS (
      SELECT
        *,
        CASE
          {legacy_classification}WHEN usage_id IS NOT NULL
            AND run_id IS NOT NULL
            AND stage IS NOT NULL
            AND call_index IS NOT NULL
            AND calls = 1
            AND prompt_tokens >= 0
            AND completion_tokens >= 0
            AND ({stage_pairs})
          THEN 'valid_new'
          ELSE 'invalid_new'
        END AS row_kind
      FROM source
    ),
    new_rows_with_physical_counts AS (
      SELECT
        *,
        ROW_NUMBER() OVER (
          PARTITION BY usage_id
          ORDER BY recorded_at, consumer, gemini_model, trend_date, run_id, stage, call_index
        ) AS physical_event_rank,
        COUNT(*) OVER (PARTITION BY usage_id) AS event_physical_rows
      FROM classified
      WHERE consumer IN ({new_consumers}) AND usage_id IS NOT NULL
    ),
    valid_new AS (
      SELECT
        *,
        ROW_NUMBER() OVER (
          PARTITION BY usage_id
          ORDER BY recorded_at, consumer, gemini_model, trend_date, run_id, stage, call_index
        ) AS valid_event_rank
      FROM new_rows_with_physical_counts
      WHERE row_kind = 'valid_new'
    ),
    cost_rows AS (
{legacy_cost_rows}
      SELECT
        consumer,
        gemini_model,
        trend_date,
        calls,
        prompt_tokens,
        completion_tokens,
        0 AS invalid_events,
        0 AS duplicate_events
      FROM valid_new
      WHERE valid_event_rank = 1
    ),
    integrity_rows AS (
      SELECT
        consumer,
        gemini_model,
        trend_date,
        0 AS calls,
        0 AS prompt_tokens,
        0 AS completion_tokens,
        1 AS invalid_events,
        0 AS duplicate_events
      FROM classified
      WHERE row_kind = 'invalid_new'
      UNION ALL
      SELECT
        consumer,
        gemini_model,
        trend_date,
        0 AS calls,
        0 AS prompt_tokens,
        0 AS completion_tokens,
        0 AS invalid_events,
        event_physical_rows - 1 AS duplicate_events
      FROM new_rows_with_physical_counts
      WHERE physical_event_rank = 1 AND event_physical_rows > 1
    ),
    combined AS (
      SELECT * FROM cost_rows
      UNION ALL
      SELECT * FROM integrity_rows
    )
    SELECT
      consumer,
      gemini_model AS model,
      trend_date AS day,
      SUM(calls) AS calls,
      SUM(prompt_tokens) AS prompt_in,
      SUM(completion_tokens) AS prompt_out,
      SUM(invalid_events) AS invalid_events,
      SUM(duplicate_events) AS duplicate_events
    FROM combined
    GROUP BY consumer, model, day
    """


def fetch_usage_ledger(window_days: int, dataset: str) -> UsageLedgerRead:
    """Read legacy aggregates or validated per-call events over the window.

    A read failure (table absent because the migration has not run, or a BQ
    outage) returns no rows and a note NAMING every consumer that therefore
    goes uncounted. Reporting a total that silently excludes a live biller is
    the exact blind spot this ledger exists to close.

    Bounded by explicit dates with no LIMIT: a row cap would return a
    complete-looking result from a partial window. The lower bound is strict,
    so a 7-day window covers exactly 7 dates ending today. An inclusive `>=`
    over DATE spans EIGHT, which inflates the window total against its own
    label while leaving the per-consumer monthly projection untouched, so it
    reads as a discrepancy with no cause.
    """
    notes: list[str] = []
    required_columns = {"run_id", "stage", "call_index"}
    try:
        capability = _run_query(_usage_schema_sql(dataset))
        available_columns = {
            str(row["column_name"])
            for row in ([] if capability.empty else capability.to_dict("records"))
        }
        per_call_columns = required_columns <= available_columns
    except Exception as exc:
        per_call_columns = False
        notes.append(
            f"{GEMINI_USAGE_TABLE} schema capability unreadable ({exc}); per-call "
            "Open Intelligence rows cannot be validated in that dataset."
        )
    if not per_call_columns and not notes:
        notes.append(
            f"{GEMINI_USAGE_TABLE} lacks run_id, stage, and call_index; per-call "
            "Open Intelligence rows cannot be validated in that dataset."
        )
    sql = (
        _mixed_usage_sql(window_days, dataset)
        if per_call_columns
        else _legacy_usage_sql(window_days, dataset)
    )
    try:
        df = _run_query(sql)
    except Exception as exc:
        notes.append(
            f"{GEMINI_USAGE_TABLE} unreadable ({exc}); "
            f"NOT counted: {', '.join(EXPECTED_CONSUMERS)}. "
            "Run the approved schema and backfill migrations before enabling calls."
        )
        return UsageLedgerRead((), tuple(notes), per_call_columns)
    rows = tuple([] if df.empty else df.to_dict("records"))
    return UsageLedgerRead(rows, tuple(notes), per_call_columns)


def fetch_usage_ledger_rows(window_days: int, dataset: str) -> tuple[list[dict], list[str]]:
    """Compatibility wrapper for callers that consume the legacy tuple."""
    read = fetch_usage_ledger(window_days, dataset)
    return list(read.rows), list(read.notes)


def fetch_base_usage_ledger(window_days: int, dataset: str) -> UsageLedgerRead:
    """Read existing aggregate consumers from the deployed base dataset."""
    try:
        df = _run_query(_base_usage_sql(window_days, dataset))
    except Exception as exc:
        existing_consumers = [
            consumer for consumer in EXPECTED_CONSUMERS if consumer not in NEW_CONSUMER_STAGES
        ]
        return UsageLedgerRead(
            (),
            (
                f"Base {GEMINI_USAGE_TABLE} unreadable ({exc}); NOT counted: "
                f"{', '.join(existing_consumers)}. Project cost truth is incomplete.",
            ),
            False,
        )
    rows = tuple([] if df.empty else df.to_dict("records"))
    return UsageLedgerRead(rows, (), False)


def fetch_staging_usage_ledger(window_days: int) -> UsageLedgerRead:
    """Read only validated new-consumer events from the exact staging dataset."""
    required_columns = {"run_id", "stage", "call_index"}
    try:
        capability = _run_query(_usage_schema_sql(STAGING_USAGE_DATASET))
    except Exception as exc:
        return UsageLedgerRead(
            (),
            (
                f"Staging {GEMINI_USAGE_TABLE} schema capability unreadable ({exc}); "
                "per-call Open Intelligence rows cannot be validated.",
            ),
            False,
        )
    available_columns = {
        str(row["column_name"])
        for row in ([] if capability.empty else capability.to_dict("records"))
    }
    per_call_columns = required_columns <= available_columns
    if not per_call_columns:
        return UsageLedgerRead(
            (),
            (
                f"Staging {GEMINI_USAGE_TABLE} lacks run_id, stage, and call_index; "
                "per-call Open Intelligence rows cannot be validated.",
            ),
            False,
        )
    try:
        df = _run_query(_mixed_usage_sql(window_days, STAGING_USAGE_DATASET, include_legacy=False))
    except Exception as exc:
        return UsageLedgerRead(
            (),
            (
                f"Staging {GEMINI_USAGE_TABLE} unreadable ({exc}); the two new "
                "consumers are not counted and staging enablement remains blocked.",
            ),
            True,
        )
    rows = tuple([] if df.empty else df.to_dict("records"))
    return UsageLedgerRead(rows, (), True)


def fetch_combined_usage_ledger(window_days: int, base_dataset: str) -> CombinedUsageLedgerRead:
    """Combine deployed aggregate cost with exact staging per-call cost."""
    return CombinedUsageLedgerRead(
        base=fetch_base_usage_ledger(window_days, base_dataset),
        staging=fetch_staging_usage_ledger(window_days),
    )


def fetch_metering_persistence_fault(window_days: int, dataset: str) -> PersistenceFaultRead:
    """Read the approved persistence failure event over the watchdog window."""
    sql = f"""
    SELECT COUNT(*) AS n
    FROM `{dataset}.system_events`
    WHERE DATE(event_time) > DATE_SUB(CURRENT_DATE(), INTERVAL {window_days} DAY)
      AND DATE(event_time) <= CURRENT_DATE()
      AND event_type = 'metering_persistence_failed'
      AND status = 'failed'
    """
    try:
        df = _run_query(sql)
    except Exception as exc:
        return PersistenceFaultRead(
            None,
            (
                f"Metering persistence events could not be checked ({exc}); "
                "staging enablement remains blocked.",
            ),
        )
    records = [] if df.empty else df.to_dict("records")
    count = records[0].get("n") if len(records) == 1 and "n" in records[0] else None
    count_is_valid = (
        len(records) == 1
        and not isinstance(count, bool)
        and isinstance(count, Integral)
        and count >= 0
    )
    if not count_is_valid:
        return PersistenceFaultRead(
            None,
            (
                "Metering persistence events could not be checked because the bounded "
                "query did not return exactly one nonnegative integer count; staging "
                "enablement remains blocked.",
            ),
        )
    return PersistenceFaultRead(bool(count > 0), ())


def fetch_billing_by_day(window_days: int, project: str) -> tuple[dict[str, float], list[str]]:
    """Vertex AI cost per settled day from the billing export. Returns (by_day, notes).

    Bounded on BOTH ends by explicit dates and never truncated by a row cap.
    An unreadable export (no export configured, or no permission on the dataset)
    returns nothing plus a note, so the report says the comparison did not run
    rather than implying the ledger matched.
    """
    start, end = closed_window(window_days, datetime.now(UTC).date())
    sql = f"""
    SELECT
      DATE(usage_start_time) AS day,
      SUM(cost) AS cost
    FROM `{project}.{BILLING_DATASET}.{BILLING_TABLE_WILDCARD}`
    WHERE service.description = '{BILLING_SERVICE}'
      AND DATE(usage_start_time) >= DATE '{start.isoformat()}'
      AND DATE(usage_start_time) <= DATE '{end.isoformat()}'
    GROUP BY day
    """
    try:
        df = _run_query(sql)
    except Exception as exc:
        return {}, [
            f"Billing export unreadable ({exc}); the ledger total was NOT checked "
            f"against what {BILLING_SERVICE} actually billed. A metering gap would "
            f"not show up in this run."
        ]
    if df.empty:
        return {}, [
            f"Billing export returned no {BILLING_SERVICE} rows for "
            f"{start.isoformat()}..{end.isoformat()}; the ledger total was NOT checked."
        ]
    return {str(r["day"]): float(r["cost"] or 0.0) for r in df.to_dict("records")}, []


def build_report(window_days: int) -> CostReport:
    from src.utils.bigquery import get_client, get_dataset

    project = get_client().project
    dataset = f"{project}.{get_dataset()}"
    now = datetime.now(UTC).date()
    report = CostReport(
        window_days=window_days,
        window_start=(now - timedelta(days=window_days)).isoformat(),
        window_end=now.isoformat(),
    )

    ledger_read = fetch_combined_usage_ledger(window_days, dataset)
    ledger_rows = list(ledger_read.rows)
    base_notes = list(ledger_read.base.notes)
    staging_notes = list(ledger_read.staging.notes)
    ledger_notes = [*base_notes, *staging_notes]
    report.consumers.extend(usage_ledger_spends(ledger_rows))
    report.notes.extend(ledger_notes)
    report.notes.extend(ledger_integrity_notes(ledger_read.staging.rows))

    observed_new_consumers = frozenset(
        str(row.get("consumer"))
        for row in ledger_read.staging.rows
        if row.get("consumer") in NEW_CONSUMER_STAGES and _int0(row.get("calls")) > 0
    )
    missing = scope_aware_missing_consumers(
        missing_ledger_consumers(report.consumers), observed_new_consumers
    )
    if missing and not ledger_notes:
        report.notes.append(
            f"No counted {GEMINI_USAGE_TABLE} row in the window: {', '.join(missing)}. "
            "Their spend is unknown, not zero."
        )

    persistence_read = fetch_metering_persistence_fault(window_days, STAGING_USAGE_DATASET)
    report.notes.extend(persistence_read.notes)

    billing_by_day, billing_notes = fetch_billing_by_day(window_days, project)
    report.notes.extend(billing_notes)
    if billing_by_day and not ledger_notes:
        report.discrepancy = reconcile_against_billing(
            ledger_cost_by_day(ledger_rows), billing_by_day
        )
        report.notes.extend(discrepancy_notes(report.discrepancy, missing))
    invalid_events, duplicate_events = ledger_fault_counts(ledger_read.staging.rows)
    report.enablement = staging_enablement_result(
        declared_consumers=EXPECTED_CONSUMERS,
        per_call_columns=ledger_read.staging.per_call_columns,
        ledger_notes=(*ledger_notes, *persistence_read.notes),
        billing_notes=billing_notes,
        invalid_events=invalid_events,
        duplicate_events=duplicate_events,
        discrepancy=report.discrepancy,
        projected_monthly=report.monthly_cost,
        valid_new_consumers=observed_new_consumers,
        observed_call_consumers=observed_new_consumers,
        persistence_fault=persistence_read.fault is True,
    )
    return report


def render_text(report: CostReport) -> str:
    out: list[str] = []
    out.append(f"VERTEX GEMINI COST  window={report.window_days}d  ({report.window_end})")
    out.append("")
    for c in report.consumers:
        out.append(
            f"  {c.name:<18} calls={c.calls:<4} active_days={c.active_days:<2} "
            f"in={c.prompt_tokens:<9} out={c.completion_tokens:<9} "
            f"window=${c.window_cost:6.2f} -> ${c.monthly_cost:6.2f}/mo"
        )
    out.append("")
    out.append(f"  window total:      ${report.window_cost:.2f}")
    out.append(f"  projected monthly: ${report.monthly_cost:.2f}/mo")
    out.append(
        f"  baseline ${STEADY_STATE_BASELINE_USD_PER_MONTH:.0f}/mo  "
        f"watch >=${WATCH_THRESHOLD_USD_PER_MONTH:.0f}  "
        f"alert >=${ALERT_THRESHOLD_USD_PER_MONTH:.0f}"
    )
    out.append("")
    out.append("  PER-CALL CEILINGS:")
    for ceiling in per_call_ceiling_rows():
        out.append(
            f"    {ceiling.consumer}: {ceiling.model}, "
            f"{ceiling.input_tokens} input tokens, "
            f"{ceiling.output_tokens} output tokens, "
            f"${ceiling.maximum_usd_per_call:.6f} maximum per call"
        )
    enablement = report.enablement or EnablementResult(
        False, ("staging enablement was not evaluated",)
    )
    out.append("")
    out.append(f"  STAGING ENABLEMENT: {'READY' if enablement.ready else 'NOT READY'}")
    for reason in enablement.reasons:
        out.append(f"    - {reason}")
    d = report.discrepancy
    if d is not None and d.days_compared:
        out.append("")
        mark = "ok" if d.within_tolerance else "GAP"
        out.append(
            f"  BILLING CHECK ({d.days_compared} settled day(s)): "
            f"ledger ${d.ledger_cost:.2f} vs {BILLING_SERVICE} ${d.billing_cost:.2f} "
            f"-> ${d.gap_usd:.2f} ({d.gap_pct:.0%}) unaccounted [{mark}]"
        )
    if report.notes:
        out.append("")
        out.append("  COVERAGE GAP (the total above is INCOMPLETE):")
        for note in report.notes:
            out.append(f"    - {note}")
    out.append("")
    out.append(f"  STATUS: {report.status}")
    if report.status == "RED":
        out.append(
            "  RED: projected monthly is over the alert threshold. Every consumer "
            "is measured, so this is real spend, not an estimate. Options: pause "
            "RECONCILE_ENABLED (it renders nothing today), gate Key-Watch volume, "
            "or route a stage to a cheaper model."
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vertex Gemini cost watchdog")
    parser.add_argument("--window", type=int, default=7, help="lookback window in days (default 7)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    try:
        report = build_report(args.window)
    except Exception as exc:  # pragma: no cover - top-level safety
        print(f"vertex_cost_watchdog failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
