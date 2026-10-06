"""Full due cohort reporting for outcome evaluation.

Every due prediction stays in the denominator. An unresolved outcome is
reported as unresolved, never folded into either rate, and a cohort with no
resolved case has no conditional rate at all. These are data projections over
rows the evaluator already wrote, not an authority over them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from src.analysis.open_intelligence.source_yield import (
    CreditEnvelope,
    RouteAllocation,
    SourcePerformanceObservation,
    SourceYieldEvaluation,
    SourceYieldRules,
    allocate_optional_credits,
    available_optional_credits,
    evaluate_source_yield,
)
from src.contracts.open_intelligence import OUTCOME_STATES

COHORT_METRIC_FIELDS = (
    "due",
    "resolved",
    "unresolved",
    "conditional_fdr",
    "fdr_lower",
    "fdr_upper",
)
# A fizzled signal was a real signal whose trajectory call missed; only noise is a
# false discovery, and an unresolved outcome is unknown rather than either.
FALSE_DISCOVERY_BY_OUTCOME = MappingProxyType(
    {
        "peaked": False,
        "sustained": False,
        "fizzled": False,
        "noise": True,
        "unresolved": None,
    }
)
FIZZLED_ARE_FALSE_DISCOVERIES = False
SOURCE_MEASUREMENT_FIELDS = (
    "status",
    "calls",
    "confirmed_credits",
    "usable_receipts",
    "unique_receipts",
    "integrity",
    "geo_precision",
    "median_lead_hours",
    "kill_test_passed",
)
# A measurement declares every field. A declared null says the value was never
# measured; a field that is simply absent says nothing at all, and reading it as
# a null would let an unrecorded source pass for an honestly unmeasured one.
CLOSED_COHORT_CONTRACT_VERSION = "outcome_closed_cohort_v1"
BUDGET_BASIS_MEASURED = "measured"
BUDGET_BASIS_UNMEASURED = "unmeasured_funding"
# The sufficiency threshold is a floor in code, never a number the same document
# supplies with the measurements. A document may ask for more; it cannot ask a
# cohort of one to certify as complete.
CLOSED_COHORT_MINIMUM_DUE_FLOOR = 5
# Two different absences, so two different names. A field nobody recorded is a
# hole in the measurement document. A family the evaluator cannot score is a
# cohort that supplies no rate or no contribution, or a recorded zero the score
# cannot divide by; its measurement may be complete and still not be scoreable.
GAP_UNRECORDED_MEASUREMENT_FIELDS = "unrecorded_measurement_fields"
GAP_UNSCOREABLE_SOURCE_FAMILIES = "unscoreable_source_families"
CLOSED_COHORT_DIGEST_FIELD = "record_digest"
CLOSED_COHORT_RECORD_FIELDS = (
    "contract_version",
    "client_scope_id",
    "evaluation_version",
    "rule_version",
    "measurement_document_digest",
    "cohort_close",
    "minimum_due",
    "evaluation_dates",
    "evaluation_run_ids",
    "cohort",
    "state_counts",
    "fizzled_are_false_discoveries",
    "by_source_set",
    "by_source_family",
    "effectiveness_gate",
    "measurement_state",
    "allocation",
    "gaps",
    "state",
)


def cohort_metrics(rows: list[dict]) -> dict:
    """Score one due cohort. Rows carry prediction_id and is_false_discovery as bool or None."""
    ids = [row["prediction_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_prediction")
    flags = [row["is_false_discovery"] for row in rows]
    if any(flag is not None and type(flag) is not bool for flag in flags):
        raise ValueError("invalid_outcome_flag")
    due = len(flags)
    resolved = sum(flag is not None for flag in flags)
    false = sum(flag is True for flag in flags)
    unresolved = due - resolved
    return {
        "due": due,
        "resolved": resolved,
        "unresolved": unresolved,
        "conditional_fdr": false / resolved if resolved else None,
        "fdr_lower": false / due if due else None,
        "fdr_upper": (false + unresolved) / due if due else None,
    }


def cohort_metrics_by_source(rows: list[dict]) -> dict[str, dict]:
    """Score the cohort per source_attribution, keeping every unknown in its source's due count."""
    cohort_metrics(rows)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        source = row.get("source_attribution")
        if not isinstance(source, str) or not source:
            raise ValueError("source_attribution_required")
        grouped.setdefault(source, []).append(row)
    return {source: cohort_metrics(grouped[source]) for source in sorted(grouped)}


def passes_effectiveness_gate(metrics: dict, *, minimum_due: int) -> bool:
    """True only when the cohort is large enough and carries a measured conditional rate.

    The gate judges sufficiency, not quality: it never compares a rate against a
    threshold and never treats an unavailable rate as zero.
    """
    if isinstance(minimum_due, bool) or not isinstance(minimum_due, int) or minimum_due < 1:
        raise ValueError("minimum_due_invalid")
    if not isinstance(metrics, Mapping) or set(metrics) != set(COHORT_METRIC_FIELDS):
        raise ValueError("cohort_metrics_invalid")
    return metrics["due"] >= minimum_due and metrics["conditional_fdr"] is not None


def attributed_cohort_rows(outcome_rows: Iterable[Mapping[str, object]]) -> list[dict]:
    """Project outcome rows into cohort rows with an exact source attribution.

    The attribution is the prediction's sorted source family set joined with a
    plus sign, so cohort_metrics_by_source partitions the cohort exactly. The
    family tuple travels with the row for the overlapping per family view.
    """
    rows = []
    for row in outcome_rows:
        if not isinstance(row, Mapping):
            raise ValueError("outcome row is invalid")
        prediction_id = row.get("prediction_id")
        if not isinstance(prediction_id, str) or not prediction_id:
            raise ValueError("prediction_id_required")
        outcome = row.get("outcome")
        if outcome not in FALSE_DISCOVERY_BY_OUTCOME:
            raise ValueError("outcome state is invalid")
        families = row.get("source_families")
        if (
            not isinstance(families, (tuple, list))
            or not families
            or any(not isinstance(family, str) or not family for family in families)
            or tuple(families) != tuple(sorted(set(families)))
        ):
            raise ValueError("source_families_invalid")
        rows.append(
            {
                "prediction_id": prediction_id,
                "is_false_discovery": FALSE_DISCOVERY_BY_OUTCOME[outcome],
                "outcome": outcome,
                "source_families": tuple(families),
                "source_attribution": "+".join(families),
            }
        )
    return rows


def source_family_metrics(rows: list[dict]) -> dict[str, dict]:
    """Score the cohort per source family; families overlap, so due counts do not sum."""
    cohort_metrics(rows)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        families = row.get("source_families")
        if not isinstance(families, tuple) or not families:
            raise ValueError("source_families_invalid")
        for family in families:
            grouped.setdefault(family, []).append(row)
    return {family: cohort_metrics(grouped[family]) for family in sorted(grouped)}


def admitted_source_measurement(measurement: object) -> dict[str, object]:
    """Admit one source measurement only when every field was actually recorded.

    A field the record never declares is refused by name. A field declared null
    says the value was never measured and travels on as null, which the yield
    evaluator reports as unmeasured. A recorded zero is a measurement and stays
    a zero, so the two can never be read as the same thing.
    """
    if not isinstance(measurement, Mapping):
        raise ValueError("source_measurement_missing")
    declared = set(measurement)
    expected = set(SOURCE_MEASUREMENT_FIELDS)
    if declared - expected:
        raise ValueError("source_measurement_field_unknown")
    if expected - declared:
        raise ValueError("source_measurement_field_missing")
    return {field: measurement[field] for field in SOURCE_MEASUREMENT_FIELDS}


def unmeasured_measurement_fields(measurement: object) -> tuple[str, ...]:
    """Name every field of an admitted measurement that was never measured."""
    admitted = admitted_source_measurement(measurement)
    return tuple(field for field in SOURCE_MEASUREMENT_FIELDS if admitted[field] is None)


def source_performance_observations(
    rows: list[dict], *, measurements: Mapping[str, Mapping[str, object]]
) -> tuple[SourcePerformanceObservation, ...]:
    """Bind measured cohort precision and contribution per family to the yield input.

    The false discovery rate is the family's conditional rate over resolved
    cases and stays None while nothing resolved, which evaluate_source_yield
    reports as unmeasured. Integrity, geo precision, receipts, lead time and the
    kill test come from the supplied per family measurements.
    """
    if not isinstance(measurements, Mapping):
        raise ValueError("source_measurements_invalid")
    family_metrics = source_family_metrics(rows)
    pooled_true = sum(1 for row in rows if row["is_false_discovery"] is False)
    observations = []
    for family, metrics in family_metrics.items():
        measurement = admitted_source_measurement(measurements.get(family))
        family_true = sum(
            1
            for row in rows
            if family in row["source_families"] and row["is_false_discovery"] is False
        )
        observations.append(
            SourcePerformanceObservation(
                endpoint_id=family,
                **{field: measurement[field] for field in SOURCE_MEASUREMENT_FIELDS},
                false_discovery_rate=metrics["conditional_fdr"],
                outcome_contribution=None if pooled_true == 0 else family_true / pooled_true,
            )
        )
    return tuple(observations)


def budget_basis_for(envelope: CreditEnvelope) -> str:
    """Name where a budget came from: measured funding maths, or unknown ones."""
    if not isinstance(envelope, CreditEnvelope):
        raise ValueError("credit envelope is invalid")
    return (
        BUDGET_BASIS_MEASURED
        if envelope.funding_math_status == "complete"
        else BUDGET_BASIS_UNMEASURED
    )


@dataclass(frozen=True, slots=True)
class AllocationProposal:
    """A bounded optional credit proposal. It never spends; automatic spend is refused.

    A source that was never measured is named, so an empty allocation list can
    never be read as a measured decision to grant it nothing. The budget basis
    says the same thing about the envelope: a zero from unknown funding math is
    not the zero of a funded envelope that is spent out.

    The proposal carries the envelope it was built from, so the basis and the
    budget it publishes are re-derived here rather than believed. A proposal
    that cannot re-derive what it claims is refused.
    """

    evaluations: tuple[SourceYieldEvaluation, ...]
    allocations: tuple[RouteAllocation, ...]
    available_credits: int
    envelope: CreditEnvelope
    automatic_spend_enabled: bool = False
    budget_basis: str | None = None
    unmeasured_endpoint_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.automatic_spend_enabled is not False:
            raise ValueError("automatic spending is disabled by construction")
        if not isinstance(self.evaluations, tuple) or any(
            not isinstance(item, SourceYieldEvaluation) for item in self.evaluations
        ):
            raise ValueError("allocation proposal evaluations are invalid")
        if not self.evaluations:
            raise ValueError("allocation proposal has no evaluated source")
        if self.budget_basis not in {BUDGET_BASIS_MEASURED, BUDGET_BASIS_UNMEASURED}:
            raise ValueError("allocation proposal budget basis is invalid")
        if not isinstance(self.envelope, CreditEnvelope):
            raise ValueError("allocation proposal envelope is invalid")
        if self.budget_basis != budget_basis_for(self.envelope):
            raise ValueError("allocation proposal budget basis does not match the envelope")
        if self.available_credits != available_optional_credits(self.envelope):
            raise ValueError("allocation proposal available credits do not match the envelope")
        if self.unmeasured_endpoint_ids != tuple(
            sorted(item.endpoint_id for item in self.evaluations if item.state == "unmeasured")
        ):
            raise ValueError("allocation proposal unmeasured sources are invalid")
        if not isinstance(self.allocations, tuple) or any(
            not isinstance(item, RouteAllocation) for item in self.allocations
        ):
            raise ValueError("allocation proposal allocations are invalid")
        if (
            isinstance(self.available_credits, bool)
            or not isinstance(self.available_credits, int)
            or self.available_credits < 0
            or sum(item.credits for item in self.allocations) > self.available_credits
        ):
            raise ValueError("allocation proposal exceeds the credit envelope")


def propose_allocation(
    observations: Iterable[SourcePerformanceObservation],
    *,
    rules: SourceYieldRules,
    envelope: CreditEnvelope,
    route_caps: Mapping[str, int],
) -> AllocationProposal:
    """Evaluate each measured source and propose credits inside the envelope."""
    evaluations = tuple(evaluate_source_yield(item, rules) for item in observations)
    if not isinstance(envelope, CreditEnvelope):
        raise ValueError("credit envelope is invalid")
    return AllocationProposal(
        evaluations=evaluations,
        allocations=allocate_optional_credits(evaluations, envelope, route_caps=route_caps),
        available_credits=available_optional_credits(envelope),
        envelope=envelope,
        budget_basis=budget_basis_for(envelope),
        unmeasured_endpoint_ids=tuple(
            sorted(item.endpoint_id for item in evaluations if item.state == "unmeasured")
        ),
    )


def cohort_report(outcome_rows: Iterable[Mapping[str, object]], *, minimum_due: int) -> dict:
    """Report one due cohort by the full denominator, with every rate labelled."""
    rows = attributed_cohort_rows(outcome_rows)
    metrics = cohort_metrics(rows)
    return {
        "cohort": metrics,
        "state_counts": {
            state: sum(1 for row in rows if row["outcome"] == state) for state in OUTCOME_STATES
        },
        "fizzled_are_false_discoveries": FIZZLED_ARE_FALSE_DISCOVERIES,
        "by_source_set": cohort_metrics_by_source(rows),
        "by_source_family": source_family_metrics(rows),
        "effectiveness_gate": passes_effectiveness_gate(metrics, minimum_due=minimum_due),
    }


def _canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cohort_day(value: object) -> date:
    if isinstance(value, (datetime, bool)):
        raise ValueError("evaluation_date_invalid")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("evaluation_date_invalid") from error
    raise ValueError("evaluation_date_invalid")


def _evaluation_projection(item: SourceYieldEvaluation) -> dict[str, object]:
    return {
        "endpoint_id": item.endpoint_id,
        "state": item.state,
        "score": item.score,
        "unique_lift": item.unique_lift,
        "lead_time_score": item.lead_time_score,
        "precision": item.precision,
        "eligible_for_optional": item.eligible_for_optional,
        "kill_test_passed": item.kill_test_passed,
        "reasons": list(item.reasons),
    }


def _identifier(value: object, field: str) -> str:
    """Take one identifier. Whitespace is stripped before it is judged nonempty."""
    if not isinstance(value, str):
        raise ValueError(f"{field}_required")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field}_required")
    return stripped


def closed_cohort_record_digest(body: Mapping[str, object]) -> str:
    """Digest one closed cohort record body, digest field excluded."""
    if not isinstance(body, Mapping) or CLOSED_COHORT_DIGEST_FIELD in body:
        raise ValueError("closed_cohort_record_fields_invalid")
    return _canonical_digest(dict(body))


def verify_closed_cohort_record(record: Mapping[str, object]) -> None:
    """Recompute the digest of a published record and refuse it on a mismatch.

    A record is only what its own body says it is. Nothing here trusts the
    digest that travelled with it, and a record whose body has moved since it
    was written is refused by name rather than read.

    The key set is compared, not the key order. The digest is taken over
    sorted keys, so a record that has passed through anything that
    canonicalises JSON is the same record and still verifies; a missing or
    extra key is refused.
    """
    if not isinstance(record, Mapping) or CLOSED_COHORT_DIGEST_FIELD not in record:
        raise ValueError("closed_cohort_record_fields_invalid")
    if set(record) != {*CLOSED_COHORT_RECORD_FIELDS, CLOSED_COHORT_DIGEST_FIELD}:
        raise ValueError("closed_cohort_record_fields_invalid")
    body = {key: value for key, value in record.items() if key != CLOSED_COHORT_DIGEST_FIELD}
    try:
        expected = closed_cohort_record_digest(body)
    except (TypeError, ValueError) as error:
        raise ValueError("closed_cohort_record_fields_invalid") from error
    if record[CLOSED_COHORT_DIGEST_FIELD] != expected:
        raise ValueError("closed_cohort_record_digest_mismatch")


def closed_cohort_record(
    outcome_rows: Iterable[Mapping[str, object]],
    *,
    cohort_close: date,
    client_scope_id: str,
    evaluation_version: str,
    rule_version: str,
    measurement_document_digest: str,
    evaluation_run_ids: Iterable[str],
    minimum_due: int,
    measurements: Mapping[str, Mapping[str, object]],
    rules: SourceYieldRules,
    envelope: CreditEnvelope,
    route_caps: Mapping[str, int],
) -> dict:
    """Score one closed cohort and propose an allocation from what was measured.

    The cohort's outcome time is the declared close the caller names, checked
    against the evaluation dates the predictions carried when they were issued.
    Nothing here reads a clock, and nothing reads the instant the evaluator ran,
    so the record and its digest are the same every time they are computed over
    the same written rows. A cohort with no rows is refused rather than scored,
    and every source and envelope value that was never measured is named.

    Provenance is checked, never taken from the rows. The caller names the
    client scope and the runs it asked for; a row carrying another run or
    another client is refused rather than scored, and the runs the record
    publishes are the ones the caller asked for. The scope, the evaluation
    version, the measurement document digest and the rule version all sit
    inside the digested body, so two cohorts that differ only in whose they are
    cannot share a digest, and a record cannot be detached from the document
    that produced it and still verify.
    """
    if isinstance(cohort_close, datetime) or not isinstance(cohort_close, date):
        raise ValueError("cohort_close_invalid")
    client_scope_id = _identifier(client_scope_id, "client_scope")
    evaluation_version = _identifier(evaluation_version, "evaluation_version")
    rule_version = _identifier(rule_version, "rule_version")
    measurement_document_digest = _identifier(
        measurement_document_digest, "measurement_document_digest"
    )
    expected_runs = {
        _identifier(run_id, "evaluation_run_id") for run_id in tuple(evaluation_run_ids)
    }
    if not expected_runs:
        raise ValueError("evaluation_run_ids_required")
    if isinstance(minimum_due, bool) or not isinstance(minimum_due, int) or minimum_due < 1:
        raise ValueError("minimum_due_invalid")
    # The document may raise the floor; it can never lower it.
    minimum_due = max(minimum_due, CLOSED_COHORT_MINIMUM_DUE_FLOOR)
    admitted = list(outcome_rows)
    if not admitted:
        raise ValueError("closed_cohort_empty")
    days: set[date] = set()
    for row in admitted:
        if not isinstance(row, Mapping):
            raise ValueError("outcome row is invalid")
        run_id = _identifier(row.get("run_id"), "evaluation_run_id")
        if run_id not in expected_runs:
            raise ValueError("evaluation_run_id_unexpected")
        if _identifier(row.get("client_scope_id"), "client_scope") != client_scope_id:
            raise ValueError("client_scope_mismatch")
        day = _cohort_day(row.get("evaluation_date"))
        if day > cohort_close:
            raise ValueError("outcome_after_cohort_close")
        days.add(day)
    rows = attributed_cohort_rows(admitted)
    metrics = cohort_metrics(rows)
    by_family = source_family_metrics(rows)
    observations = source_performance_observations(rows, measurements=measurements)
    proposal = propose_allocation(
        observations, rules=rules, envelope=envelope, route_caps=route_caps
    )
    scored = {item.endpoint_id: item for item in proposal.evaluations}
    measurement_state = {}
    for family in sorted(by_family):
        unmeasured = list(unmeasured_measurement_fields(measurements.get(family)))
        evaluation = scored[family]
        measurement_state[family] = {
            "state": "unmeasured" if unmeasured else "measured",
            "unmeasured_fields": unmeasured,
            # The scoring state cannot silently disagree with the measurement
            # state: a family whose every field was recorded and which still
            # cannot be scored says so here, with the evaluator's own reason.
            "scoring_state": evaluation.state,
            "scoring_reasons": list(evaluation.reasons),
        }
    gaps = []
    if max(days) < cohort_close:
        gaps.append("cohort_close_not_reached")
    if metrics["resolved"] == 0:
        gaps.append("no_resolved_outcome")
    if metrics["due"] < minimum_due:
        gaps.append("cohort_below_minimum_due")
    if any(state["unmeasured_fields"] for state in measurement_state.values()):
        gaps.append(GAP_UNRECORDED_MEASUREMENT_FIELDS)
    if proposal.unmeasured_endpoint_ids:
        gaps.append(GAP_UNSCOREABLE_SOURCE_FAMILIES)
    if proposal.budget_basis == BUDGET_BASIS_UNMEASURED:
        gaps.append("unmeasured_funding_math")
    record = {
        "contract_version": CLOSED_COHORT_CONTRACT_VERSION,
        "client_scope_id": client_scope_id,
        "evaluation_version": evaluation_version,
        "rule_version": rule_version,
        "measurement_document_digest": measurement_document_digest,
        "cohort_close": cohort_close.isoformat(),
        "minimum_due": minimum_due,
        "evaluation_dates": [day.isoformat() for day in sorted(days)],
        "evaluation_run_ids": sorted(expected_runs),
        "cohort": metrics,
        "state_counts": {
            state: sum(1 for row in rows if row["outcome"] == state) for state in OUTCOME_STATES
        },
        "fizzled_are_false_discoveries": FIZZLED_ARE_FALSE_DISCOVERIES,
        "by_source_set": cohort_metrics_by_source(rows),
        "by_source_family": by_family,
        "effectiveness_gate": passes_effectiveness_gate(metrics, minimum_due=minimum_due),
        "measurement_state": measurement_state,
        "allocation": {
            "budget_basis": proposal.budget_basis,
            "available_credits": proposal.available_credits,
            "automatic_spend_enabled": proposal.automatic_spend_enabled,
            "unmeasured_endpoint_ids": list(proposal.unmeasured_endpoint_ids),
            "evaluations": [_evaluation_projection(item) for item in proposal.evaluations],
            "allocations": [
                {"endpoint_id": item.endpoint_id, "credits": item.credits, "score": item.score}
                for item in proposal.allocations
            ],
        },
        "gaps": gaps,
        "state": "incomplete" if gaps else "complete",
    }
    if tuple(record) != CLOSED_COHORT_RECORD_FIELDS:
        raise ValueError("closed_cohort_record_field_drift")
    record[CLOSED_COHORT_DIGEST_FIELD] = closed_cohort_record_digest(record)
    return record
