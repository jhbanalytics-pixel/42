"""Discovery parity rows and their evaluation over enrolled paired runs (L01).

One row binds one verified parity enrollment to one market of its cutoff and
records, for that market, the legacy chain's qualified identities, how each is
represented in the current chain's output, the legacy identities the legacy
chain itself rejected with their reasons, and the ground truth recall of both
chains against one known event set. The ledger holds one immutable row per UTC
cutoff and market. The evaluation counts days the way ``eligible_run_count``
counts runs, so a repeated execution of one source window never adds a day,
and it reports pass only with at least ten complete eligible days, no lost
qualified identity and no recall regression. A recall that was not measured is
unknown, never zero, and an unknown recall keeps the verdict from passing.

What the two chains are, as the tree stands. The enrollment binds two
``OpenIntelligenceRunReceipt`` rows of ``open_intelligence_run_receipts_v1``.
Its legacy run is the retained replay chain, ``replay_open_intelligence`` with
observation method ``dynamic_source_copy_apply_v1``, released through
``release_open_intelligence_run``; its current run is the daily composer,
``daily_composer`` with observation method ``daily_source_snapshot_compose_v1``.
Both chains write their output under their own run id to the same row
families, ``persistence.TABLE_BINDINGS``: ``signal_candidates_v2`` (one row per
signal, with ``evidence_state`` and ``topic_tags``) and
``signal_membership_v2`` (one row per member, whose ``member_identity`` is the
``market|candidate_type|term`` identity ``pipeline._observation_identity``
derives from the legacy trend tables ``event_ledger``, ``seed_graph`` and the
``seed_candidates`` rows whose status is in
``pipeline.ACCEPTED_SEED_CANDIDATE_STATUSES``), plus ``signal_evidence_v2``,
``signal_lineage_v2``, ``signal_predictions_v2`` and the release record
``open_intelligence_quality_release_records_v2``. The receipt's
``row_set_digest`` is computed over those written rows by
``persistence.row_set_digest_sql``.

What is not persisted anywhere, and why the builder takes an explicit input
contract instead of reading rows:

1. No enrollment can be built today. ``parity_enrollment`` explains why: the
   two chains compute ``source_window_digest`` from different preimages, and a
   composer run cannot be released. Every row here therefore rests on an
   enrollment that does not yet exist natively.
2. The qualified and represented identity sets are readable per run from
   ``signal_membership_v2`` and ``signal_candidates_v2``, but nothing binds a
   caller's list of them to the receipt: ``row_set_digest`` covers whole rows in
   a BigQuery expression that this pure module cannot recompute from identity
   lists. The sets a row carries are the caller's statement, digested into the
   row, not proved against the receipt.
3. Rejected legacy identities with reasons have no per identity record. The
   current chain keeps no rejection per member identity. The legacy
   ``seed_candidates`` table keeps ``status`` and ``rationale`` per candidate,
   but ``pipeline._load_source`` selects only accepted statuses and never reads
   ``rationale``. The disposition sidecar
   ``open_intelligence_observation_dispositions_v1`` keys its reason codes by
   source observation, not by member identity.
4. Ground truth recall is not stored for any run. Known events exist only in
   retained replay input bundles (``known_event_set``), and
   ``replay_manifest.evaluate_replay_manifest`` computes per market recall from
   caller supplied signals without writing it. No known event set exists for a
   composer run.

This module is a pure value. It reads nothing, writes nothing and takes no
lock; whatever holds the ledger owns the uniqueness of its key.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.parity_enrollment import (
    ParityEnrollment,
    ParityEnrollmentError,
    eligible_run_count,
    enrollment_snapshot_key,
    parity_enrollment_digest,
    verify_parity_enrollment,
)

DISCOVERY_PARITY_ROW_VERSION = "open_intelligence_discovery_parity_row_v1"
DISCOVERY_PARITY_EVALUATION_VERSION = "open_intelligence_discovery_parity_evaluation_v1"
PARITY_REQUIRED_DAYS = 10
PROMISED_MARKETS = ("za", "ng", "ke")
REPRESENTATION_RELATIONS = frozenset({"membership", "topic_tag"})
_DIGEST_LENGTH = 64


class DiscoveryParityError(ValueError):
    """One parity row or ledger rule refused. Nothing is repaired or defaulted."""


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DiscoveryParityError(code)
    return value


def _identities(value: object, code: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise DiscoveryParityError(code)
    items = tuple(value)
    for item in items:
        _text(item, code)
    if len(set(items)) != len(items):
        raise DiscoveryParityError(f"{code}:duplicate")
    return tuple(sorted(items))


def _recall(value: object, field: str) -> tuple[int, int] | None:
    """One recall as numerator and denominator, or None when not measured."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"numerator", "denominator"}:
        raise DiscoveryParityError(f"recall_invalid:{field}")
    numerator = value["numerator"]
    denominator = value["denominator"]
    if (
        type(numerator) is not int
        or type(denominator) is not int
        or denominator < 1
        or not 0 <= numerator <= denominator
    ):
        raise DiscoveryParityError(f"recall_invalid:{field}")
    return numerator, denominator


def _representations(value: object, qualified: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, Mapping):
        raise DiscoveryParityError("represented_invalid")
    rows = []
    for identity, relations in value.items():
        _text(identity, "represented_invalid")
        if identity not in qualified:
            raise DiscoveryParityError("represented_outside_qualified")
        if isinstance(relations, (str, bytes, Mapping)) or not isinstance(relations, Iterable):
            raise DiscoveryParityError("represented_invalid")
        entries = tuple(relations)
        if not entries:
            raise DiscoveryParityError("represented_empty")
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {"relation", "signal_id"}:
                raise DiscoveryParityError("represented_invalid")
            if entry["relation"] not in REPRESENTATION_RELATIONS:
                raise DiscoveryParityError("represented_relation_invalid")
            rows.append(
                (
                    identity,
                    entry["relation"],
                    _text(entry["signal_id"], "represented_invalid"),
                )
            )
    if len(set(rows)) != len(rows):
        raise DiscoveryParityError("represented_invalid:duplicate")
    return tuple(sorted(rows))


def _rejections(value: object, qualified: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise DiscoveryParityError("rejected_invalid")
    rows = []
    for identity, reason in value.items():
        _text(identity, "rejected_invalid")
        if identity in qualified:
            raise DiscoveryParityError("rejected_identity_is_qualified")
        rows.append((identity, _text(reason, "rejection_reason_missing")))
    return tuple(sorted(rows))


def _recall_status(legacy: tuple[int, int] | None, current: tuple[int, int] | None) -> str:
    if legacy is None or current is None:
        return "unknown"
    if legacy[1] != current[1]:
        raise DiscoveryParityError("recall_denominator_differs")
    return "regressed" if current[0] < legacy[0] else "held"


@dataclass(frozen=True, slots=True)
class DiscoveryParityRow:
    """One immutable parity row for one UTC cutoff and one market.

    Natural key: the cutoff and the market. ``row_digest`` binds every other
    field, and the row is rebuilt whenever it is read. That rebuild checks the
    carried enrollment against the two receipts the enrollment itself carries,
    not against receipts read back from storage, so it proves the row is
    internally consistent; tying it to stored receipts happens only when the
    row is built, from the receipts its caller read back.
    """

    row_version: str
    cutoff: str
    market: str
    enrollment: ParityEnrollment
    enrollment_digest: str
    snapshot_key: tuple[str, str]
    legacy_run_id: str
    current_run_id: str
    known_event_set_digest: str
    legacy_qualified_ids: tuple[str, ...]
    represented: tuple[tuple[str, str, str], ...]
    lost_ids: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]
    legacy_recall: tuple[int, int] | None
    current_recall: tuple[int, int] | None
    recall_status: str
    row_digest: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.cutoff, self.market)


def _row_body(row: DiscoveryParityRow) -> dict[str, Any]:
    return {
        "row_version": row.row_version,
        "cutoff": row.cutoff,
        "market": row.market,
        "enrollment_digest": row.enrollment_digest,
        "snapshot_key": list(row.snapshot_key),
        "legacy_run_id": row.legacy_run_id,
        "current_run_id": row.current_run_id,
        "known_event_set_digest": row.known_event_set_digest,
        "legacy_qualified_ids": list(row.legacy_qualified_ids),
        "represented": [list(item) for item in row.represented],
        "lost_ids": list(row.lost_ids),
        "rejected": [list(item) for item in row.rejected],
        "legacy_recall": None if row.legacy_recall is None else list(row.legacy_recall),
        "current_recall": None if row.current_recall is None else list(row.current_recall),
        "recall_status": row.recall_status,
    }


def build_discovery_parity_row(
    *,
    enrollment: object,
    legacy_receipt: object,
    current_receipt: object,
    market: object,
    known_event_set_digest: object,
    legacy_qualified_ids: object,
    represented: object,
    rejected: object,
    legacy_recall: object,
    current_recall: object,
) -> DiscoveryParityRow:
    """Build one row after proving its enrollment against the read back receipts.

    ``represented`` maps a qualified legacy identity to the current signals that
    carry it, each as ``{"relation": "membership" | "topic_tag", "signal_id"}``.
    ``rejected`` maps a legacy identity the legacy chain rejected to the reason
    from its decision record; a qualified identity cannot be excused this way.
    A qualified identity with no representation is lost. Each recall is
    ``{"numerator", "denominator"}`` over the known event set named by its
    digest, or None when it was not measured.
    """
    try:
        record = verify_parity_enrollment(
            enrollment, legacy=legacy_receipt, current=current_receipt
        )
    except ParityEnrollmentError as error:
        raise DiscoveryParityError(f"parity_enrollment_unverified:{error}") from error
    market_code = _text(market, "market_invalid")
    if market_code not in record.market_scope:
        raise DiscoveryParityError("market_outside_enrollment_scope")
    digest = _text(known_event_set_digest, "known_event_set_digest_invalid")
    if len(digest) != _DIGEST_LENGTH or any(c not in "0123456789abcdef" for c in digest):
        raise DiscoveryParityError("known_event_set_digest_invalid")
    qualified = _identities(legacy_qualified_ids, "legacy_qualified_ids_invalid")
    representations = _representations(represented, qualified)
    represented_ids = {item[0] for item in representations}
    rejections = _rejections(rejected, qualified)
    # A member identity is market|candidate_type|term, so its market is its
    # first part; a row for one market cannot carry another market's items.
    prefix = f"{market_code}|"
    if any(
        not identity.startswith(prefix)
        for identity in (*qualified, *(item[0] for item in rejections))
    ):
        raise DiscoveryParityError("identity_market_mismatch")
    legacy_value = _recall(legacy_recall, "legacy")
    current_value = _recall(current_recall, "current")
    row = DiscoveryParityRow(
        row_version=DISCOVERY_PARITY_ROW_VERSION,
        cutoff=record.signal_date.isoformat(),
        market=market_code,
        enrollment=record,
        enrollment_digest=parity_enrollment_digest(record),
        snapshot_key=enrollment_snapshot_key(record),
        legacy_run_id=record.legacy_run_id,
        current_run_id=record.current_run_id,
        known_event_set_digest=digest,
        legacy_qualified_ids=qualified,
        represented=representations,
        lost_ids=tuple(item for item in qualified if item not in represented_ids),
        rejected=rejections,
        legacy_recall=legacy_value,
        current_recall=current_value,
        recall_status=_recall_status(legacy_value, current_value),
        row_digest="",
    )
    return _sealed(row)


def _sealed(row: DiscoveryParityRow) -> DiscoveryParityRow:
    fields = {name: getattr(row, name) for name in DiscoveryParityRow.__slots__}
    fields["row_digest"] = canonical_digest(_row_body(row))
    return DiscoveryParityRow(**fields)


def _verified_row(value: object) -> DiscoveryParityRow:
    """Rebuild a held row from its own inputs and refuse any drift.

    The rebuild runs every rule the builder runs, including the enrollment
    verification against the receipts the enrollment carries (not against
    stored receipts, which a held row does not reach), so a held row whose
    fields were restated and whose digest was recomputed to match still
    refuses.
    """
    if type(value) is not DiscoveryParityRow:
        raise DiscoveryParityError("parity_row_invalid")
    enrollment = value.enrollment
    if not isinstance(enrollment, ParityEnrollment):
        raise DiscoveryParityError("parity_enrollment_unverified")
    represented: dict[str, list[dict[str, str]]] = {}
    rejected: dict[str, object] = {}
    try:
        for identity, relation, signal_id in value.represented:
            represented.setdefault(identity, []).append(
                {"relation": relation, "signal_id": signal_id}
            )
        for identity, reason in value.rejected:
            if identity in rejected:
                raise DiscoveryParityError("parity_row_unproven")
            rejected[identity] = reason
        legacy_recall, current_recall = (
            None if pair is None else {"numerator": pair[0], "denominator": pair[1]}
            for pair in (value.legacy_recall, value.current_recall)
        )
    except (TypeError, ValueError) as error:
        raise DiscoveryParityError("parity_row_unproven") from error
    rebuilt = build_discovery_parity_row(
        enrollment=enrollment,
        legacy_receipt=enrollment.legacy_receipt,
        current_receipt=enrollment.current_receipt,
        market=value.market,
        known_event_set_digest=value.known_event_set_digest,
        legacy_qualified_ids=value.legacy_qualified_ids,
        represented=represented,
        rejected=rejected,
        legacy_recall=legacy_recall,
        current_recall=current_recall,
    )
    if rebuilt != value:
        raise DiscoveryParityError("parity_row_unproven")
    return value


def add_discovery_parity_row(held: Iterable[object], row: object) -> tuple[DiscoveryParityRow, ...]:
    """Append one row to the held ledger, keyed by UTC cutoff and market.

    A second row for a key already held refuses, identical or not: a repeated
    run of one day is the same day and never a second row. Rows of one cutoff
    must all bind one enrollment, so one day cannot mix two runs' markets.
    """
    candidate = _verified_row(row)
    rows = tuple(_verified_row(item) for item in held)
    for item in rows:
        if item.key == candidate.key:
            raise DiscoveryParityError("parity_row_duplicate")
        if (
            item.cutoff == candidate.cutoff
            and item.enrollment_digest != candidate.enrollment_digest
        ):
            raise DiscoveryParityError("parity_cutoff_enrollment_conflict")
    return (*rows, candidate)


def _ratio(pair: tuple[int, int] | None) -> dict[str, object] | None:
    if pair is None:
        return None
    return {"numerator": pair[0], "denominator": pair[1], "rate": pair[0] / pair[1]}


def evaluate_discovery_parity(rows: Iterable[object]) -> dict[str, object]:
    """Evaluate a parity ledger against the L01 gate.

    A day counts only when its enrollment's market scope is exactly the
    product's promised market set, za, ng and ke, which no caller can narrow,
    and its rows cover every one of those markets. A day of any other scope is
    listed under ``scope_mismatched_days`` and never counted, so single
    market days, or days of different scopes, cannot add up to a pass. Days
    are counted with ``eligible_run_count`` over those enrollments, so two
    enrollments of one cutoff and source window count once. The verdict is
    ``fail`` on any lost qualified identity or recall regression, otherwise
    ``unknown`` while any recall is unmeasured, otherwise
    ``insufficient_days`` below ten eligible days, otherwise ``pass``.
    """
    promised = frozenset(PROMISED_MARKETS)
    ledger = tuple(_verified_row(item) for item in rows)
    keys = [row.key for row in ledger]
    if len(set(keys)) != len(keys):
        raise DiscoveryParityError("parity_row_duplicate")
    by_cutoff: dict[str, list[DiscoveryParityRow]] = {}
    for row in ledger:
        by_cutoff.setdefault(row.cutoff, []).append(row)
    complete = []
    incomplete = []
    mismatched = []
    for cutoff in sorted(by_cutoff):
        cutoff_rows = by_cutoff[cutoff]
        if len({row.enrollment_digest for row in cutoff_rows}) != 1:
            raise DiscoveryParityError("parity_cutoff_enrollment_conflict")
        record = cutoff_rows[0].enrollment
        if frozenset(record.market_scope) != promised:
            mismatched.append({"cutoff": cutoff, "market_scope": list(record.market_scope)})
            continue
        missing = sorted(set(record.market_scope) - {row.market for row in cutoff_rows})
        if missing:
            incomplete.append({"cutoff": cutoff, "missing_markets": missing})
        else:
            complete.append(record)
    eligible_days = eligible_run_count(complete)
    lost = sorted((row.cutoff, row.market, item) for row in ledger for item in row.lost_ids)
    regressed = sorted(
        (row.cutoff, row.market) for row in ledger if row.recall_status == "regressed"
    )
    unknown = sorted((row.cutoff, row.market) for row in ledger if row.recall_status == "unknown")
    if lost or regressed:
        verdict = "fail"
    elif unknown:
        verdict = "unknown"
    elif eligible_days < PARITY_REQUIRED_DAYS:
        verdict = "insufficient_days"
    else:
        verdict = "pass"
    markets: dict[str, dict[str, object]] = {}
    for row in sorted(ledger, key=lambda item: item.key):
        tally = markets.setdefault(
            row.market,
            {"rows": 0, "qualified": 0, "represented": 0, "lost": 0, "rejected": 0},
        )
        tally["rows"] += 1
        tally["qualified"] += len(row.legacy_qualified_ids)
        tally["represented"] += len(row.legacy_qualified_ids) - len(row.lost_ids)
        tally["lost"] += len(row.lost_ids)
        tally["rejected"] += len(row.rejected)
    return {
        "evaluation_version": DISCOVERY_PARITY_EVALUATION_VERSION,
        "required_days": PARITY_REQUIRED_DAYS,
        "eligible_days": eligible_days,
        "row_count": len(ledger),
        "promised_markets": sorted(promised),
        "scope_mismatched_days": mismatched,
        "incomplete_days": incomplete,
        "lost_qualified": [
            {"cutoff": cutoff, "market": market, "identity": identity}
            for cutoff, market, identity in lost
        ],
        "recall_regressions": [{"cutoff": c, "market": m} for c, m in regressed],
        "recall_unknown": [{"cutoff": c, "market": m} for c, m in unknown],
        "rows": [
            {
                "cutoff": row.cutoff,
                "market": row.market,
                "row_digest": row.row_digest,
                "enrollment_digest": row.enrollment_digest,
                "legacy_recall": _ratio(row.legacy_recall),
                "current_recall": _ratio(row.current_recall),
                "recall_status": row.recall_status,
            }
            for row in sorted(ledger, key=lambda item: item.key)
        ],
        "markets": markets,
        "verdict": verdict,
    }


__all__ = [
    "DISCOVERY_PARITY_EVALUATION_VERSION",
    "DISCOVERY_PARITY_ROW_VERSION",
    "PARITY_REQUIRED_DAYS",
    "PROMISED_MARKETS",
    "REPRESENTATION_RELATIONS",
    "DiscoveryParityError",
    "DiscoveryParityRow",
    "add_discovery_parity_row",
    "build_discovery_parity_row",
    "evaluate_discovery_parity",
]
