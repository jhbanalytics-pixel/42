"""Pure fail-closed quality, direction, projection, and review contracts."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.persistence import (
    CONTENT_PROJECTION_EXCLUDED_FIELDS,
    NATURAL_KEYS,
    OpenIntelligenceRowBatch,
    canonical_typed_json,
)
from src.analysis.open_intelligence.run_receipts import RUN_RECEIPT_ROW_FIELDS

PROJECTION_CONTRACT_VERSION = "dynamic_quality_projection_v2"
REVIEW_CONTRACT_VERSION = "dynamic_quality_review_v1"
REVIEW_ITEM_FIELDS = (
    "run_id",
    "signal_id",
    "market",
    "evidence_id",
    "row_id",
    "source_family",
    "platform",
    "url",
    "published_at",
    "source_label",
    "author_label",
    "excerpt",
)
REVIEW_RECEIPT_FIELDS = (
    "review_contract_version",
    "run_id",
    "source_window_digest",
    "candidate_projection_digest",
    "packet_digest",
    "reviewed_evidence_ids",
    "foreign_market_evidence_ids",
    "factual_conflict_evidence_ids",
    "uncertain_evidence_ids",
    "reviewed_by",
    "reviewed_at",
    "decision",
    "receipt_digest",
)
QUALITY_RELEASE_FIELDS = (
    "run_id",
    "run_receipt_digest",
    "source_window_digest",
    "candidate_projection_digest",
    "packet_digest",
    "review_receipt_digest",
    "approval_addendum_sha256",
    "released_at",
    "release_contract_version",
)
_DIGEST_LENGTH = 64


class QualityRefusal(ValueError):
    """Quality authority could not be proved without inventing state."""


@dataclass(frozen=True, slots=True)
class ReviewAuthority:
    run_id: str
    receipt_digest: str
    reviewed_by: str
    reviewed_at: datetime


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualityRefusal(f"{field} must be a nonempty string")
    if unicodedata.normalize("NFC", value) != value:
        raise QualityRefusal(f"noncanonical_unicode:{field}")
    return value


def validate_nfc(value: object, field: str = "value") -> None:
    if isinstance(value, str):
        _text(value, field)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _text(key, f"{field}.key")
            validate_nfc(item, f"{field}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            validate_nfc(item, f"{field}[{index}]")


def _run_id(value: object) -> str:
    return _text(value, "run_id")


def _digest(value: object, field: str) -> str:
    value = _text(value, field)
    if len(value) != _DIGEST_LENGTH or any(char not in "0123456789abcdef" for char in value):
        raise QualityRefusal(f"{field} must be lower-case SHA256")
    return value


def _projection_rows(table: str, rows: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    for row in rows:
        validate_nfc(row, table)
    return tuple(
        canonical_typed_json(table, row)
        for row in sorted(
            rows,
            key=lambda item: (
                tuple(item[field] for field in NATURAL_KEYS[table]),
                canonical_typed_json(table, item),
            ),
        )
    )


def candidate_projection_payload(run_id: str, batch: OpenIntelligenceRowBatch) -> dict[str, object]:
    run_id = _run_id(run_id)
    if not isinstance(batch, OpenIntelligenceRowBatch):
        raise QualityRefusal("candidate projection batch is invalid")
    return {
        "projection_contract_version": PROJECTION_CONTRACT_VERSION,
        "run_id": run_id,
        "candidates": _projection_rows("candidates", batch.candidates),
        "evidence": _projection_rows("evidence", batch.evidence),
        "membership": _projection_rows("membership", batch.membership),
    }


def candidate_projection_digest(run_id: str, batch: OpenIntelligenceRowBatch) -> str:
    return canonical_digest(candidate_projection_payload(run_id, batch))


CONTENT_PROJECTION_CONTRACT_VERSION = "dynamic_quality_projection_content_v1"
CONTENT_REVIEW_CONTRACT_VERSION = "dynamic_quality_review_content_v1"
_CONTENT_EXCLUDED_FIELDS = CONTENT_PROJECTION_EXCLUDED_FIELDS


def _content_projection_rows(table: str, rows: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    for row in rows:
        validate_nfc(row, table)
    keys = tuple(field for field in NATURAL_KEYS[table] if field != "run_id")
    return tuple(
        canonical_typed_json(table, row, exclude=_CONTENT_EXCLUDED_FIELDS)
        for row in sorted(
            rows,
            key=lambda item: (
                tuple(item[field] for field in keys),
                canonical_typed_json(table, item, exclude=_CONTENT_EXCLUDED_FIELDS),
            ),
        )
    )


def candidate_projection_content_payload(batch: OpenIntelligenceRowBatch) -> dict[str, object]:
    """The projection of the rows alone, without run identity or the persistence clock.

    Approved 3 Sep 2026 (content-bound quality review). Mirrors
    persistence.candidate_projection_content_digest_sql field for field.
    """
    if not isinstance(batch, OpenIntelligenceRowBatch):
        raise QualityRefusal("candidate projection batch is invalid")
    return {
        "projection_contract_version": CONTENT_PROJECTION_CONTRACT_VERSION,
        "candidates": _content_projection_rows("candidates", batch.candidates),
        "evidence": _content_projection_rows("evidence", batch.evidence),
        "membership": _content_projection_rows("membership", batch.membership),
    }


def candidate_projection_content_digest(batch: OpenIntelligenceRowBatch) -> str:
    return canonical_digest(candidate_projection_content_payload(batch))


def review_packet_content_digest(batch: OpenIntelligenceRowBatch) -> str:
    """The review packet digest over the items alone, without the run identity."""
    if not isinstance(batch, OpenIntelligenceRowBatch):
        raise QualityRefusal("review packet batch is invalid")
    items = []
    seen = set()
    for row in batch.evidence:
        validate_nfc(row, "review_item")
        if row["evidence_id"] in seen:
            raise QualityRefusal("review packet contains duplicate evidence IDs")
        seen.add(row["evidence_id"])
        item = review_item(row)
        del item["run_id"]
        items.append(item)
    ordered = tuple(
        sorted(items, key=lambda item: (item["market"], item["signal_id"], item["evidence_id"]))
    )
    return canonical_digest(
        {"review_contract_version": CONTENT_REVIEW_CONTRACT_VERSION, "review_items": ordered}
    )


@dataclass(frozen=True, slots=True)
class PriorRunReview:
    """The review the human registered on the prior run, with what that run's rows say.

    Every digest here is read from BigQuery over the prior run's persisted rows;
    the apply step compares them with the batch it is about to persist.
    """

    receipt: Mapping[str, object]
    run_id: str
    candidate_projection_digest: str
    packet_digest: str
    packet_evidence_ids: tuple[str, ...]
    candidate_projection_content_digest: str
    review_packet_content_digest: str


def content_review_authority(
    prior: PriorRunReview, *, source_window_digest: str, batch: OpenIntelligenceRowBatch
) -> ReviewAuthority | None:
    """Authority for ``batch`` from a review registered on the prior run.

    Approved 3 Sep 2026: a review attests rows, not a run label. The prior
    receipt must be genuine for its own rows (its run-bound digests equal the
    digests BigQuery reports for that run), the window must be the same, and
    the content digests of the prior rows must equal the content digests of the
    batch. Anything short of that is no authority, exactly as a pending review.
    """
    if not isinstance(prior, PriorRunReview) or not isinstance(batch, OpenIntelligenceRowBatch):
        return None
    try:
        packet = {
            "review_contract_version": REVIEW_CONTRACT_VERSION,
            "run_id": prior.run_id,
            "review_items": tuple(
                {"evidence_id": evidence_id} for evidence_id in prior.packet_evidence_ids
            ),
            "packet_digest": prior.packet_digest,
        }
        authority = validate_review_receipt(
            prior.receipt,
            packet=packet,
            source_window_digest=source_window_digest,
            candidate_projection_digest=prior.candidate_projection_digest,
        )
        if prior.receipt["candidate_projection_digest"] != _digest(
            prior.candidate_projection_digest, "prior candidate_projection_digest"
        ) or prior.receipt["packet_digest"] != _digest(prior.packet_digest, "prior packet_digest"):
            return None
        if candidate_projection_content_digest(batch) != _digest(
            prior.candidate_projection_content_digest, "prior content projection digest"
        ):
            return None
        if review_packet_content_digest(batch) != _digest(
            prior.review_packet_content_digest, "prior content packet digest"
        ):
            return None
    except QualityRefusal:
        return None
    return authority


def validate_prediction_review_binding(
    reviewed_batch: OpenIntelligenceRowBatch, final_batch: OpenIntelligenceRowBatch
) -> None:
    if not isinstance(reviewed_batch, OpenIntelligenceRowBatch) or not isinstance(
        final_batch, OpenIntelligenceRowBatch
    ):
        raise QualityRefusal("prediction review batches are invalid")
    run_ids = {row["run_id"] for row in reviewed_batch.candidates}
    if len(run_ids) != 1:
        raise QualityRefusal("reviewed candidate run is invalid")
    run_id = next(iter(run_ids))
    if candidate_projection_digest(run_id, reviewed_batch) != candidate_projection_digest(
        run_id, final_batch
    ):
        raise QualityRefusal("reviewed projection changed during prediction scoring")
    candidate_signals = {row["signal_id"] for row in reviewed_batch.candidates}
    evidence_signals = {row["signal_id"] for row in reviewed_batch.evidence}
    membership_signals = {row["signal_id"] for row in reviewed_batch.membership}
    for prediction in final_batch.predictions:
        signal_id = prediction["signal_id"]
        if signal_id not in candidate_signals:
            raise QualityRefusal("prediction references an unreviewed signal")
        if signal_id not in evidence_signals:
            raise QualityRefusal("prediction lacks reviewed evidence")
        if signal_id not in membership_signals:
            raise QualityRefusal("prediction lacks reviewed membership")


def bigquery_timestamp_text(value: object) -> str | None:
    """Render a timestamp the way FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S') plus 'Z' does.

    The register routine and the release recompute the packet digest in SQL with that
    format, which trims trailing zeros from the fraction and drops it on a whole second.
    """
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise QualityRefusal("packet timestamp must be timezone aware")
    value = value.astimezone(UTC)
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += "." + f"{value.microsecond:06d}".rstrip("0")
    return text + "Z"


def review_item(row: Mapping[str, object]) -> dict[str, object]:
    """One packet item with its timestamp rendered as the SQL digest renders it."""
    item = {field: row[field] for field in REVIEW_ITEM_FIELDS}
    item["published_at"] = bigquery_timestamp_text(item["published_at"])
    return item


def build_review_packet(run_id: str, batch: OpenIntelligenceRowBatch) -> dict[str, object]:
    run_id = _run_id(run_id)
    if not isinstance(batch, OpenIntelligenceRowBatch):
        raise QualityRefusal("review packet batch is invalid")
    items = []
    seen = set()
    for row in batch.evidence:
        validate_nfc(row, "review_item")
        if row["run_id"] != run_id:
            raise QualityRefusal("review packet evidence run differs")
        evidence_id = row["evidence_id"]
        if evidence_id in seen:
            raise QualityRefusal("review packet contains duplicate evidence IDs")
        seen.add(evidence_id)
        items.append(review_item(row))
    ordered = tuple(
        sorted(items, key=lambda item: (item["market"], item["signal_id"], item["evidence_id"]))
    )
    preimage = {
        "review_contract_version": REVIEW_CONTRACT_VERSION,
        "run_id": run_id,
        "review_items": ordered,
    }
    return {**preimage, "packet_digest": canonical_digest(preimage)}


def review_receipt_digest(receipt: Mapping[str, object]) -> str:
    if not isinstance(receipt, Mapping):
        raise QualityRefusal("review receipt is invalid")
    validate_nfc(receipt, "review_receipt")
    keys = tuple(receipt)
    expected = REVIEW_RECEIPT_FIELDS[:-1]
    if keys not in (expected, REVIEW_RECEIPT_FIELDS):
        raise QualityRefusal("review receipt field order is invalid")
    return canonical_digest({field: receipt[field] for field in expected})


def _id_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise QualityRefusal(f"{field} must be an ordered sequence")
    items = tuple(_text(item, field) for item in value)
    if len(items) != len(set(items)):
        raise QualityRefusal(f"{field} contains duplicates")
    return items


def validate_review_receipt(
    receipt: Mapping[str, object],
    *,
    packet: Mapping[str, object],
    source_window_digest: str,
    candidate_projection_digest: str,
) -> ReviewAuthority:
    if tuple(receipt) != REVIEW_RECEIPT_FIELDS:
        raise QualityRefusal("review receipt field order is invalid")
    if receipt["review_contract_version"] != REVIEW_CONTRACT_VERSION:
        raise QualityRefusal("review contract version is invalid")
    if receipt["run_id"] != packet.get("run_id"):
        raise QualityRefusal("review run differs from packet")
    if receipt["source_window_digest"] != _digest(source_window_digest, "source_window_digest"):
        raise QualityRefusal("review source window digest differs")
    if receipt["candidate_projection_digest"] != _digest(
        candidate_projection_digest, "candidate_projection_digest"
    ):
        raise QualityRefusal("review candidate projection digest differs")
    if receipt["packet_digest"] != packet.get("packet_digest"):
        raise QualityRefusal("review packet digest differs")
    packet_ids = tuple(item["evidence_id"] for item in packet.get("review_items", ()))
    if _id_tuple(receipt["reviewed_evidence_ids"], "reviewed_evidence_ids") != packet_ids:
        raise QualityRefusal("reviewed evidence IDs are incomplete or reordered")
    packet_set = set(packet_ids)
    for field in (
        "foreign_market_evidence_ids",
        "factual_conflict_evidence_ids",
        "uncertain_evidence_ids",
    ):
        items = _id_tuple(receipt[field], field)
        if tuple(sorted(items)) != items or not set(items).issubset(packet_set):
            raise QualityRefusal(f"{field} is not a sorted packet subset")
        if items:
            raise QualityRefusal(f"{field} prevents approval")
    if receipt["reviewed_by"] != "Albert":
        raise QualityRefusal("reviewed_by is not the approved human")
    reviewed_at = receipt["reviewed_at"]
    if not isinstance(reviewed_at, datetime) or reviewed_at.tzinfo is None:
        raise QualityRefusal("reviewed_at must be timezone aware")
    if receipt["decision"] != "approved":
        raise QualityRefusal("review decision is not approved")
    if receipt["receipt_digest"] != review_receipt_digest(receipt):
        raise QualityRefusal("review receipt digest differs")
    return ReviewAuthority(
        run_id=receipt["run_id"],
        receipt_digest=receipt["receipt_digest"],
        reviewed_by=receipt["reviewed_by"],
        reviewed_at=reviewed_at,
    )


@dataclass(frozen=True, slots=True)
class FamilyDirection:
    direction: str | None
    raw_p: float | None
    corrected_p: float | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TechnicalQuality:
    passed: bool
    reasons: tuple[str, ...]


def evaluate_technical_quality(
    *, component, receipts, memberships, as_of: datetime
) -> TechnicalQuality:
    reasons = set()
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise QualityRefusal("quality as_of must be timezone aware")
    row_receipts = tuple(getattr(component, "row_receipts", ()))
    member_identities = tuple(getattr(component, "member_identities", ()))
    if len(row_receipts) != len(set(row_receipts)):
        reasons.add("duplicate_component_row_id")
    if not isinstance(receipts, Mapping) or set(receipts) != set(row_receipts):
        reasons.add("component_receipt_projection_incomplete")
        receipt_values = tuple(receipts.values()) if isinstance(receipts, Mapping) else ()
    else:
        receipt_values = tuple(receipts.values())
    if {getattr(item, "member_identity", None) for item in receipt_values} != set(
        member_identities
    ):
        reasons.add("component_member_receipt_incomplete")
    if getattr(component, "build_version", None) != "hybrid_graph_v2":
        reasons.add("cluster_build_version_invalid")
    membership_values = tuple(memberships) if isinstance(memberships, (tuple, list)) else ()
    if {
        item.get("member_identity") for item in membership_values if isinstance(item, Mapping)
    } != set(member_identities):
        reasons.add("membership_projection_incomplete")
    label_rows = [
        item
        for item in membership_values
        if isinstance(item, Mapping)
        and item.get("member_identity") == getattr(component, "label_member_identity", None)
    ]
    if len(label_rows) != 1 or label_rows[0].get("canonical_value") != getattr(
        component, "label", None
    ):
        reasons.add("label_member_identity_invalid")
    for receipt in receipt_values:
        row_id = getattr(receipt, "row_id", None)
        source_family = getattr(receipt, "source_family", None)
        platform = getattr(receipt, "platform", None)
        member_identity = getattr(receipt, "member_identity", None)
        if not all(isinstance(value, str) and value for value in (row_id, source_family, platform)):
            reasons.add("canonical_receipt_identity_invalid")
        if not isinstance(member_identity, str) or not member_identity.startswith(
            f"{getattr(component, 'market', '')}|"
        ):
            reasons.add("receipt_market_invalid")
        if getattr(receipt, "availability", None) == "available":
            published_at = getattr(receipt, "published_at", None)
            if not isinstance(published_at, datetime) or published_at.tzinfo is None:
                reasons.add("available_receipt_timestamp_unavailable")
            elif published_at > as_of:
                reasons.add("future_available_receipt")
            if getattr(receipt, "factual_conflict", False):
                reasons.add("integrity_clean_receipts_failed")
    ordered = tuple(sorted(reasons))
    return TechnicalQuality(not ordered, ordered)


def score_quality_candidate(
    *,
    candidate: Mapping[str, object],
    readiness,
    receipts: Mapping[str, object],
    memberships: Sequence[Mapping[str, object]],
    technical_quality: TechnicalQuality,
    review_authority: ReviewAuthority | None,
    rules,
    directional_conflict: bool,
):
    from src.analysis.open_intelligence.scoring import (
        DecisionStrengthRules,
        SignalScoreInput,
        score_signal,
    )

    if review_authority is not None and not isinstance(review_authority, ReviewAuthority):
        raise QualityRefusal("review authority is unavailable")
    if not isinstance(technical_quality, TechnicalQuality):
        raise QualityRefusal("technical quality authority is invalid")
    if not isinstance(rules, DecisionStrengthRules):
        raise QualityRefusal("decision strength rules are invalid")
    available = tuple(
        item for item in receipts.values() if getattr(item, "availability", None) == "available"
    )
    source_integrity = None
    if available:
        source_integrity = sum(
            not getattr(item, "factual_conflict", False) for item in available
        ) / len(available)
    membership_complete = technical_quality.passed and any(
        item.get("member_identity") == candidate.get("label_member_identity")
        and item.get("canonical_value") == candidate.get("label")
        and item.get("qualifies_evidence") is True
        for item in memberships
    )
    item = SignalScoreInput(
        signal_id=candidate["signal_id"],
        market=candidate["market"],
        signal_date=candidate["signal_date"],
        discovery_mode=candidate["discovery_mode"],
        evidence_state=candidate["evidence_state"],
        qualifying_source_families=len(readiness.qualifying_families),
        qualifying_current_receipts=len(available),
        source_integrity=source_integrity,
        velocity=candidate["velocity_score"],
        breadth=candidate["breadth_score"],
        source_independence=candidate["independence_score"],
        geo_confidence=candidate["geo_confidence"],
        # Only a registered human review attests the foreign-market sample; without one the
        # candidate scores but can never be promoted.
        foreign_market_sample_reviewed=review_authority is not None,
        foreign_market_leakage=0,
        duplicate_identity=not technical_quality.passed,
        factual_conflict=any(
            getattr(value, "factual_conflict", False) for value in receipts.values()
        ),
        directional_conflict=directional_conflict,
        membership_receipts_complete=membership_complete,
        cluster_build_version=candidate["cluster_build_version"],
    )
    return score_signal(item, rules)


def _lower_tail_numerator(total: int, current: int) -> tuple[int, int]:
    term = 11**total
    cumulative = term
    for value in range(1, current + 1):
        term = term * (total - value + 1) * 3 // (value * 11)
        cumulative += term
    return cumulative, term


def _upper_tail_numerator(total: int, current: int) -> tuple[int, int]:
    term = 3**total
    cumulative = term
    for value in range(total, current, -1):
        term = term * value * 11 // ((total - value + 1) * 3)
        cumulative += term
    return cumulative, term


def _two_sided_p_fraction(current: int, baseline: int) -> Fraction:
    total = current + baseline
    denominator = 14**total
    if current * 14 <= total * 3:
        lower_numerator, mass_numerator = _lower_tail_numerator(total, current)
        upper_numerator = denominator - lower_numerator + mass_numerator
    else:
        upper_numerator, mass_numerator = _upper_tail_numerator(total, current)
        lower_numerator = denominator - upper_numerator + mass_numerator
    return min(
        Fraction(1),
        2 * min(Fraction(lower_numerator, denominator), Fraction(upper_numerator, denominator)),
    )


def family_directions(
    counts: Mapping[str, tuple[int, int]], *, exposure_complete: Mapping[str, bool]
) -> dict[str, FamilyDirection]:
    raw: dict[str, Fraction] = {}
    output: dict[str, FamilyDirection] = {}
    for family in sorted(counts):
        current, baseline = counts[family]
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (current, baseline)
        ):
            raise QualityRefusal("family counts must be nonnegative integers")
        if exposure_complete.get(family) is not True:
            output[family] = FamilyDirection(
                None, None, None, "family_exposure_authority_unavailable"
            )
        elif current + baseline < 5:
            output[family] = FamilyDirection("not_applicable", None, None)
        else:
            raw[family] = _two_sided_p_fraction(current, baseline)
    adjusted: dict[str, Fraction] = {}
    running = Fraction(0)
    ordered = sorted(raw, key=lambda family: (raw[family], family))
    for index, family in enumerate(ordered):
        running = max(running, min(Fraction(1), (len(ordered) - index) * raw[family]))
        adjusted[family] = running
    for family in ordered:
        current, baseline = counts[family]
        direction = "not_applicable"
        if adjusted[family] <= Fraction(1, 20) and current * 11 > baseline * 3:
            direction = "rising"
        elif adjusted[family] <= Fraction(1, 20) and current * 11 < baseline * 3:
            direction = "declining"
        output[family] = FamilyDirection(direction, float(raw[family]), float(adjusted[family]))
    return {family: output[family] for family in sorted(output)}


def _sql_alias(value: str) -> str:
    if (
        not isinstance(value, str)
        or __import__("re").fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", value)
        is None
    ):
        raise QualityRefusal("SQL alias is invalid")
    return value


def _plain_sql_value(field: str, expression: str) -> str:
    if field in {"signal_date", "observation_start", "observation_end", "exposure_date"}:
        return f"TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', {expression}))"
    if field in {"completed_at", "released_at", "published_at", "reviewed_at"}:
        rendered = (
            "TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', "
            f"{expression}, 'UTC'), 'Z'))"
        )
        return f"IF({expression} IS NULL, 'null', {rendered})"
    return f"TO_JSON_STRING({expression})"


def run_receipt_digest_sql(alias: str) -> str:
    alias = _sql_alias(alias)
    arguments = ["'{'"]
    for index, field in enumerate(sorted(RUN_RECEIPT_ROW_FIELDS)):
        if index:
            arguments.append("','")
        if field == "completed_at":
            # The Python receipt digest (brain_contract canonical) renders a
            # fixed six digit fraction; %E*S trims trailing zeros and refused
            # the r15 release on staging, 4 Sep 2026.
            value = (
                "TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S', "
                f"{alias}.{field}, 'UTC'), 'Z'))"
            )
        else:
            value = _plain_sql_value(field, f"{alias}.{field}")
        arguments.extend((f"'\"{field}\":'", value))
    arguments.append("'}'")
    payload = "CONCAT(" + ", ".join(arguments) + ")"
    return f"LOWER(TO_HEX(SHA256({payload})))"


def review_packet_content_digest_sql(
    run_expression: str,
    *,
    sql_expression: bool = True,
    project: str = "ogilvy-trends-v2",
    dataset: str = "trends_v2_staging",
) -> str:
    """The review packet digest over the items alone, mirroring review_packet_content_digest."""
    run_filter = _sql_alias(run_expression) if sql_expression else f"'{_run_id(run_expression)}'"
    item_fields = tuple(sorted(field for field in REVIEW_ITEM_FIELDS if field != "run_id"))
    arguments = ["'{'"]
    for index, field in enumerate(item_fields):
        if index:
            arguments.append("','")
        arguments.extend((f"'\"{field}\":'", _plain_sql_value(field, f"e.{field}")))
    arguments.append("'}'")
    item_json = "CONCAT(" + ", ".join(arguments) + ")"
    review_items = (
        "IFNULL((SELECT CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT "
        + item_json
        + f" FROM `{project}.{dataset}.signal_evidence_v2` e "
        + f"WHERE e.run_id = {run_filter} AND e.client_scope_id != 'qa_canary' "
        + "ORDER BY e.market, e.signal_id, e.evidence_id), ','), ']')), '[]')"
    )
    preimage = (
        f'CONCAT(\'{{"review_contract_version":"{CONTENT_REVIEW_CONTRACT_VERSION}"\', '
        "',\"review_items\":', " + review_items + ", '}')"
    )
    return f"LOWER(TO_HEX(SHA256({preimage})))"


def review_packet_digest_sql(
    run_expression: str,
    *,
    sql_expression: bool = True,
    project: str = "ogilvy-trends-v2",
    dataset: str = "trends_v2_staging",
) -> str:
    if sql_expression:
        run_filter = _sql_alias(run_expression)
        run_json = f"TO_JSON_STRING(CAST({run_filter} AS STRING))"
    else:
        run_filter = _run_id(run_expression)
        run_filter = f"'{run_filter}'"
        run_json = f"TO_JSON_STRING('{run_expression}')"
    item_fields = tuple(sorted(REVIEW_ITEM_FIELDS))
    arguments = ["'{'"]
    for index, field in enumerate(item_fields):
        if index:
            arguments.append("','")
        arguments.extend((f"'\"{field}\":'", _plain_sql_value(field, f"e.{field}")))
    arguments.append("'}'")
    item_json = "CONCAT(" + ", ".join(arguments) + ")"
    review_items = (
        "IFNULL((SELECT CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT "
        + item_json
        + f" FROM `{project}.{dataset}.signal_evidence_v2` e "
        + f"WHERE e.run_id = {run_filter} AND e.client_scope_id != 'qa_canary' "
        + "ORDER BY e.market, e.signal_id, e.evidence_id), ','), ']')), '[]')"
    )
    preimage = (
        'CONCAT(\'{"review_contract_version":"dynamic_quality_review_v1"\', '
        "',\"review_items\":', " + review_items + ", ',\"run_id\":', " + run_json + ", '}')"
    )
    return f"LOWER(TO_HEX(SHA256({preimage})))"


__all__ = [
    "PROJECTION_CONTRACT_VERSION",
    "QUALITY_RELEASE_FIELDS",
    "REVIEW_CONTRACT_VERSION",
    "REVIEW_ITEM_FIELDS",
    "FamilyDirection",
    "PriorRunReview",
    "QualityRefusal",
    "ReviewAuthority",
    "TechnicalQuality",
    "build_review_packet",
    "candidate_projection_content_digest",
    "candidate_projection_content_payload",
    "candidate_projection_digest",
    "candidate_projection_payload",
    "canonical_digest",
    "content_review_authority",
    "evaluate_technical_quality",
    "family_directions",
    "review_packet_content_digest",
    "review_packet_content_digest_sql",
    "review_packet_digest_sql",
    "review_receipt_digest",
    "run_receipt_digest_sql",
    "score_quality_candidate",
    "validate_nfc",
    "validate_prediction_review_binding",
    "validate_review_receipt",
]
