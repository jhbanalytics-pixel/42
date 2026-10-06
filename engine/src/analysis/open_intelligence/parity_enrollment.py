"""Immutable parity enrollment for eligible completed discovery runs.

Parity enrollment starts at the first qualifying run of the repeatable discovery
chain. One record binds the legacy chain's output and the current chain's output
to the same observation cutoff, the same source window and the same scope, so a
later comparison reads two outputs of one day rather than two days of one output.

A record binds two completed runs over one source window whose receipts carry two
different observation method strings; it does not prove that two chains produced
them, for the reason given below. It carries the two run receipts themselves, so it
proves its own two receipt digests instead of restating them, and every held record
is validated on construction and again on every read. Identity is the run receipt
digest, never the run id: a run id is a restatable name, so a receipt relabelled is
refused as the same run rather than accepted as its own counterparty, and the
natural key of a record is the two receipt digests in sorted order, which is the
same key whichever way round the pair was enrolled.

The two chains are not proved. ``parity_chains_identical`` refuses a pair whose two
``observation_method`` strings are equal, and that is all it compares. The run receipt
contract validates that field as a nonempty string and nothing more, the retained
replay writes the constant ``dynamic_source_copy_apply_v1``, and the composer's
receipt inputs helper takes the string from its caller with
``daily_source_snapshot_compose_v1`` as the default. There is no registry of chain
names, so two receipts of one chain carrying ``dynamic_source_copy_apply_v1`` and
``dynamic_source_copy_apply_v2``, as two runs either side of a version bump of the
string would, enroll as two chains, and a record whose two row set digests agree then
reports its outputs identical, which a later reader would take for the legacy chain
and the current chain agreeing. The gate closes only the byte equal case: two retained
replay receipts differing only in run id and completion instant refuse. The only thing
that keeps the wider case off the shipped replay is that its run id happens to be a
constant, which is an accident rather than a rule. The cluster build version cannot
carry the gate either: two builds of one chain differ in it, so it separates builds
and not chains.

It carries no measured accuracy, no score and no verdict: later evaluation tasks
collect the eligible completed runs, the real historical replay and the observed
outcomes, and they decide. Repeated executions of one source window enroll
separately, because each is a real completed run, but they count as one eligible
run: ``eligible_run_count`` counts distinct cutoff and source window pairs, never
enrollment rows.

That count does not key on ``source_window_digest``. Two producers in this tree
write that digest into a run receipt, and they compute it two different ways. The
daily composer carries the captured snapshot's own self digest, and the snapshot
dict it hashes holds the capture instant and a snapshot id derived from that
instant, so a second capture of byte identical source yields a different digest.
The retained replay carries a sha256 over the copy run id and the per table
manifest and matched row counts, with no capture instant in it at all, so a repeat
under one copy run id does not move it. Keying the count on the stored digest
would therefore grow the count on a recapture of one window through the composer,
and the digest is also recomputed outside this tree and is already stored on
released rows, so its meaning is not this module's to change either way.
``enrollment_source_window_key`` derives the enrollment's own content key from the
client scope, the market scope and the closed observation window, which cannot
move when one window is captured again. Two genuinely different captures of one
window therefore count once, which understates rather than overstates the
independent evidence a later evaluation may rely on.

Only one of those two chains ships and runs. ``replay_open_intelligence`` is imported
by the release script, the execution proof issuer, the scoring qualification and several
other shipped modules. The daily composer is imported by nothing outside tests:
``build_native_composer``, ``composition_receipt_inputs`` and ``build_native_persist``
are named only by the composer's own two test modules and by this module's test, and the
one other mention of ``daily_composer`` in ``src``, apart from this docstring, is a mutex
identifier string. The shipped daily wiring binds the compose stage's composer and persist
clients to refusing stand ins, ``daily_native_clients.UNBOUND_CLIENTS``, so the composer
is reached from no shipped path. Calling them two real producer chains is generous: one
chain ships and runs, the other exists only in tests.

No record can be built from them as the tree stands, and the reason is structural
rather than operational. The replay writes observation method
``dynamic_source_copy_apply_v1`` with the copy completeness digest; the composer
writes ``daily_source_snapshot_compose_v1`` with the snapshot self digest. Two
sha256 values over different preimages are equal only on a collision, so the
pairing gate ``parity_source_snapshot_differs`` refuses every pair the two real
chains can present. The same root refuses a composer receipt read through
``brain_live_reader._source_copy_authority``, which recomputes the replay formula
and rejects any receipt not carrying it.

The pairing gate is not the only thing in the way, because a record is enrolled from
two released runs and a composer run cannot be released. The release path reads its
quality review receipt through a stored routine that recomputes the copy completeness
formula and asserts the recomputed value equals the stored ``source_window_digest``;
the register routine asserts the same on write; the live quality path ties the review
receipt's digest to the run receipt's; and the desk view joins the release record to
the run receipt on that column. A composer receipt carrying the snapshot self digest
fails those asserts, so it is never released and therefore never enrolled, whatever
the pairing gate does. Options 1, 2 and 3 below all leave the composer's receipt
carrying the snapshot self digest, so none of them by itself produces a record: each
assumes a releasable composer run that the release path will not admit.

The first parity record therefore needs one of these decisions, and none of them is
this module's to take:

1. Pair on the observed window rather than on the stored digest: drop the
   ``source_window_digest`` equality from the pairing and keep the client scope,
   the market scope and the closed observation window, which is already the key
   this module counts on. Nothing stored changes. The pairing gets weaker: two
   chains that observed one window through genuinely different source material
   would pair.
2. Carry a second, shared field on the run receipt, computed the same way by both
   chains over the window both observed, and pair on that. Nothing stored is
   redefined, but the run receipt contract gains a field and every producer and
   reader of a receipt has to write it.
3. Bind the correspondence outside the receipt: an enrollment input that names the
   copy run and the snapshot proved to cover one window, produced by whatever runs
   both chains. Nothing in the receipt changes and the proof moves to that input.
4. Leave the enrollment unbuildable and say so. That is what this tree does today.
5. Have the composer chain compute the copy completeness formula that already exists,
   over the window it observed, and carry that as its ``source_window_digest``. This
   is the only one of the five under which a composer receipt passes the digest
   asserts named above, and it is not a redefinition of the stored column: the stored
   definition and every assert stay exactly as they are. For the digest itself it
   needs no schema change, no new receipt field and no view change. It is not a value
   supplied at the persist seam. ``daily_composer.build_native_persist`` takes its run
   receipt inputs as a caller supplied callable and checks only the key set, but the
   composer's own inputs helper ``composition_receipt_inputs`` reads the digest out of
   ``CompositionFacts``, which ``NativeComposer.__call__`` fills from
   ``result.source_snapshot_digest``; the composer takes a capture entry and a cutoff
   and nothing else, while the formula takes the copy manifest completeness rows and
   a copy run id. Carrying the formula therefore changes the composer, or replaces its
   inputs helper with one that computes the formula outside it. Nor does it by itself
   produce a releasable composer run. The certify stage builds a live profile's
   release evidence through the release script's ``_build_release_inputs``, which
   refuses any execution proof whose contract is not ``r3-execution-proof-v1``, whose
   first argument is not ``scripts/staging/replay_open_intelligence.py`` or whose
   apply binding is not the ``r3_apply`` operation chain, and a composer run executes
   none of those. A live profile is needed as well: ``RELEASE_PROFILES`` registers
   only ``r3`` and adding one is a reviewed change, and the daily kernel's own live
   profile dispatches compose through the composer and persist clients that
   ``daily_native_clients.UNBOUND_CLIENTS`` binds to refusing stand ins. The read
   routine also requires exactly one copy manifest run over the receipt's observation
   window, so the composer's window and the copy manifest must be the same window,
   which is a question about how the chain is run rather than about the contract.

Redefining ``source_window_digest`` so the two chains agree is not on that list. A
BigQuery view joins on that column and rebuilds ``run_receipt_digest`` from a
preimage containing it, and three schemas declare it NOT NULL, so a redefinition
invalidates every stored run receipt digest. That is a durable contract change and
it belongs to the owner of the contract, not here.

An empty enrollment is not a parity result. ``parity_outputs_identical`` refuses an
empty enrollment rather than reporting that every held record agrees, because the
enrollment is empty until a native run produces the first pair and an absence must
never be read as parity.

This module is a pure value. ``enroll_parity_record`` returns a new tuple, holds no
store and takes no lock, so it cannot serialise two writers that each read the same
held view. Whatever holds these records owns that uniqueness, and enforces it with a
unique constraint on the pair key and an agreement constraint from run id to run
receipt digest, which are the two rules applied here inside one view.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.run_receipts import (
    QA_CLIENT_SCOPE_ID,
    OpenIntelligenceRunReceipt,
    RunReceiptError,
    build_run_receipt,
    receipt_qualifies_for_display,
    run_receipt_digest,
)

PARITY_ENROLLMENT_CONTRACT_VERSION = "open_intelligence_parity_enrollment_v1"
PARITY_SOURCE_WINDOW_KEY_DOMAIN = "open_intelligence_parity_source_window_key_v1"
PARITY_RUN_IDENTITY_DOMAIN = "open_intelligence_parity_run_identity_v1"


class ParityEnrollmentError(ValueError):
    """One parity enrollment rule refused. Nothing is repaired or defaulted."""


@dataclass(frozen=True, slots=True)
class ParityEnrollment:
    """One immutable parity record, validated on construction.

    Natural key: the two run receipt digests in sorted order. The record carries
    both receipts, so no projected field and no receipt digest can be restated,
    swapped or invented without the construction refusing.
    """

    contract_version: str
    client_scope_id: str
    market_scope: tuple[str, ...]
    signal_date: date
    observation_start: date
    observation_end: date
    source_window_digest: str
    legacy_run_id: str
    legacy_observation_method: str
    legacy_cluster_build_version: str
    legacy_row_set_digest: str
    legacy_receipt_digest: str
    current_run_id: str
    current_observation_method: str
    current_cluster_build_version: str
    current_row_set_digest: str
    current_receipt_digest: str
    legacy_receipt: OpenIntelligenceRunReceipt
    current_receipt: OpenIntelligenceRunReceipt
    enrolled_at: datetime

    def __post_init__(self) -> None:
        _validate(self)

    @property
    def outputs_identical(self) -> bool:
        """Whether the two bound row sets are byte equal. Not an accuracy claim."""
        return self.legacy_row_set_digest == self.current_row_set_digest


def _receipt(value: object, field: str) -> OpenIntelligenceRunReceipt:
    # The exact type, not an isinstance check: a frozen subclass carrying an extra
    # field is not this contract's receipt, and it would otherwise enroll.
    if type(value) is not OpenIntelligenceRunReceipt:
        raise ParityEnrollmentError(f"parity_receipt_invalid:{field}")
    # There is no separate contract version guard here. ``build_run_receipt``
    # refuses any receipt whose ``run_contract_version`` is not the contract's,
    # and the rebuild below runs it, so a guard on that field would raise nothing
    # the rebuild does not raise with the same code, and no test could tell the
    # two apart. The rebuild is the proof.
    # A receipt that did not come through build_run_receipt can hold anything its
    # fields will take: a naive completion instant, an observation end that is not
    # a date, a slot removed. Every one of those escapes this module's vocabulary
    # later as a TypeError or an AttributeError, so the contract is re-proved here
    # by rebuilding the receipt from its own fields. The rebuild is a proof, not a
    # repair: the original is returned, and a rebuild that does not reproduce it
    # field for field refuses rather than being accepted in the rebuilt form. Field
    # equality, not digest equality: the canonical rendering reads a market scope
    # written as a list and one written as a tuple the same way, so a digest round
    # trip would let the list through and the record would hold it.
    try:
        rebuilt = build_run_receipt(**asdict(value))
        proven = asdict(rebuilt) == asdict(value)
    except (RunReceiptError, AttributeError, TypeError, ValueError) as error:
        raise ParityEnrollmentError(f"parity_receipt_invalid:{field}") from error
    if not proven:
        raise ParityEnrollmentError(f"parity_receipt_invalid:{field}")
    return value


def _cutoff(value: object, field: str) -> date:
    # A datetime is a date subclass, so a bare isinstance check would let one
    # cutoff expressed as a timestamp key separately from the same cutoff as a
    # date and count twice for one cutoff. The run receipt module guards its
    # observation dates the same way and for the same reason; this guard is
    # deliberate, and it refuses rather than narrowing a timestamp to a date.
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ParityEnrollmentError(f"parity_cutoff_invalid:{field}")
    return value


def _instant(value: object) -> datetime:
    # A type gate, not a duck typed one: an object that merely answers utcoffset
    # and astimezone is not an instant, and an aware datetime is normalised to
    # UTC so one instant has exactly one enrolled_at however it was expressed.
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ParityEnrollmentError("parity_enrolled_at_invalid")
    return value.astimezone(UTC)


def _completed(receipt: OpenIntelligenceRunReceipt, field: str, *, today: date) -> None:
    # Eligibility here is the display qualification helper's own answer, called
    # rather than restated in part: it also refuses a run whose observation
    # window has not closed, which a partial restatement would let enroll.
    # A receipt that cannot prove its own contract is not an unfinished run, and
    # naming it one would send a recovery to the completion fields. ``_receipt``
    # already refuses such a receipt on the way in; this is the backstop, and it
    # names the real fault.
    try:
        qualifies = receipt_qualifies_for_display(
            receipt, requested_markets=receipt.market_scope, today=today
        )
    except RunReceiptError as error:
        raise ParityEnrollmentError(f"parity_receipt_invalid:{field}") from error
    if not qualifies:
        raise ParityEnrollmentError(f"run_not_completed:{field}")


def _run_identity(receipt: OpenIntelligenceRunReceipt) -> str:
    """One run's identity by content, with its restatable name left out."""
    fields = asdict(receipt)
    fields.pop("run_id")
    return canonical_digest({"domain": PARITY_RUN_IDENTITY_DOMAIN, "receipt": fields})


def _pair(legacy: object, current: object, enrolled_at: object) -> dict[str, object]:
    """Every pairing rule, and the fields one record projects from the two runs."""
    left = _receipt(legacy, "legacy")
    right = _receipt(current, "current")
    instant = _instant(enrolled_at)
    if left.client_scope_id == QA_CLIENT_SCOPE_ID or right.client_scope_id == QA_CLIENT_SCOPE_ID:
        raise ParityEnrollmentError("parity_scope_unqualified")
    _completed(left, "legacy", today=instant.date())
    _completed(right, "current", today=instant.date())
    if left.run_id == right.run_id or _run_identity(left) == _run_identity(right):
        raise ParityEnrollmentError("parity_runs_identical")
    # This gate refuses two receipts whose observation method strings are equal
    # and establishes nothing more. The string is caller supplied and the receipt
    # contract validates it only as nonempty: the retained replay writes the
    # constant dynamic_source_copy_apply_v1 and the daily composer's inputs helper
    # takes it from its caller with daily_source_snapshot_compose_v1 as the
    # default, so two receipts of one chain carrying two versions of the string
    # pass here as two chains. What it does close: two receipts of one chain
    # differing only in run id and completion instant used to enroll cleanly and
    # report outputs_identical true, which reads as the legacy and the current
    # chain agreeing when it is one chain compared with itself. The cluster build
    # version cannot carry the gate either: two builds of one chain differ in it.
    if left.observation_method == right.observation_method:
        raise ParityEnrollmentError("parity_chains_identical")
    if left.signal_date != right.signal_date:
        raise ParityEnrollmentError("parity_cutoff_differs")
    if left.source_window_digest != right.source_window_digest:
        raise ParityEnrollmentError("parity_source_snapshot_differs")
    if (left.observation_start, left.observation_end) != (
        right.observation_start,
        right.observation_end,
    ):
        raise ParityEnrollmentError("parity_source_window_differs")
    if left.client_scope_id != right.client_scope_id or left.market_scope != right.market_scope:
        raise ParityEnrollmentError("parity_scope_differs")
    if instant < left.completed_at or instant < right.completed_at:
        raise ParityEnrollmentError("parity_enrolled_before_completion")
    return {
        "contract_version": PARITY_ENROLLMENT_CONTRACT_VERSION,
        "client_scope_id": left.client_scope_id,
        "market_scope": left.market_scope,
        "signal_date": _cutoff(left.signal_date, "signal_date"),
        "observation_start": _cutoff(left.observation_start, "observation_start"),
        "observation_end": _cutoff(left.observation_end, "observation_end"),
        "source_window_digest": left.source_window_digest,
        "legacy_run_id": left.run_id,
        "legacy_observation_method": left.observation_method,
        "legacy_cluster_build_version": left.cluster_build_version,
        "legacy_row_set_digest": left.row_set_digest,
        "legacy_receipt_digest": run_receipt_digest(left),
        "current_run_id": right.run_id,
        "current_observation_method": right.observation_method,
        "current_cluster_build_version": right.cluster_build_version,
        "current_row_set_digest": right.row_set_digest,
        "current_receipt_digest": run_receipt_digest(right),
        "legacy_receipt": left,
        "current_receipt": right,
        "enrolled_at": instant,
    }


def _validate(record: ParityEnrollment) -> None:
    """Re-derive the record from the receipts it carries and refuse any drift."""
    _cutoff(record.signal_date, "signal_date")
    _cutoff(record.observation_start, "observation_start")
    _cutoff(record.observation_end, "observation_end")
    expected = _pair(record.legacy_receipt, record.current_receipt, record.enrolled_at)
    if record.enrolled_at.utcoffset() != timedelta(0):
        raise ParityEnrollmentError("parity_enrollment_unproven")
    for name, value in expected.items():
        if getattr(record, name) != value:
            raise ParityEnrollmentError("parity_enrollment_unproven")


def _enrollment(value: object) -> ParityEnrollment:
    if not isinstance(value, ParityEnrollment):
        raise ParityEnrollmentError("parity_enrollment_invalid")
    _validate(value)
    return value


def build_parity_enrollment(
    *,
    legacy: object,
    current: object,
    enrolled_at: object,
) -> ParityEnrollment:
    """Bind one legacy and one current completed run over one source window.

    Both receipts must be eligible completed runs of the same client scope, market
    scope, cutoff and source window, they must be two different runs by content and
    not merely by name, and their caller supplied observation method strings must
    differ, which does not establish that they came from two different chains. The
    enrollment instant must be timezone aware and no
    earlier than either completion, and its UTC date is the day by which each
    observation window must have closed.
    """
    return ParityEnrollment(**_pair(legacy, current, enrolled_at))  # type: ignore[arg-type]


def verify_parity_enrollment(
    enrollment: object, *, legacy: object, current: object
) -> ParityEnrollment:
    """Prove one held record against the two receipts it claims to bind.

    The record already carries its receipts; this ties it to receipts read back
    from wherever they are held, so a record whose two receipt digests were
    swapped, invented or attached to other runs refuses instead of counting.
    """
    record = _enrollment(enrollment)
    rebuilt = build_parity_enrollment(
        legacy=legacy, current=current, enrolled_at=record.enrolled_at
    )
    if parity_enrollment_digest(rebuilt) != parity_enrollment_digest(record):
        raise ParityEnrollmentError("parity_enrollment_unproven")
    return record


def parity_enrollment_digest(enrollment: object) -> str:
    """The canonical digest binding every field of one enrollment."""
    return canonical_digest(_enrollment(enrollment))


def enrollment_source_window_key(enrollment: object) -> str:
    """The content identity of the source window one enrollment observed.

    Deliberately not ``source_window_digest``. Two producers write that digest and
    they compute it two different ways. The daily composer carries the captured
    snapshot's own self digest, which is a function of when the snapshot was
    captured as well as what it holds, so it moves on a repeat capture of byte
    identical source and would grow the eligible run count on a repetition the
    counting rule forbids. The retained replay hashes the copy run id with the per
    table manifest and matched row counts and does not move on a repeat under one
    copy run id. This key is a function of the window that was observed and of
    nothing else, so it is the same key under either producer.
    """
    record = _enrollment(enrollment)
    return canonical_digest(
        {
            "domain": PARITY_SOURCE_WINDOW_KEY_DOMAIN,
            "client_scope_id": record.client_scope_id,
            "market_scope": list(record.market_scope),
            "observation_start": _cutoff(record.observation_start, "observation_start").isoformat(),
            "observation_end": _cutoff(record.observation_end, "observation_end").isoformat(),
        }
    )


def enrollment_snapshot_key(enrollment: object) -> tuple[str, str]:
    """The cutoff and the source window content key one enrollment observed."""
    record = _enrollment(enrollment)
    return (
        _cutoff(record.signal_date, "signal_date").isoformat(),
        enrollment_source_window_key(record),
    )


def enrollment_pair_key(enrollment: object) -> tuple[str, str]:
    """The order independent natural key of the pair one record binds."""
    record = _enrollment(enrollment)
    left, right = sorted((record.legacy_receipt_digest, record.current_receipt_digest))
    return (left, right)


def _run_digest_agreement(records: tuple[ParityEnrollment, ...]) -> None:
    """One run id names one receipt. Two receipts under one name is a conflict."""
    seen: dict[str, str] = {}
    for record in records:
        for run_id, receipt_digest in (
            (record.legacy_run_id, record.legacy_receipt_digest),
            (record.current_run_id, record.current_receipt_digest),
        ):
            if seen.setdefault(run_id, receipt_digest) != receipt_digest:
                raise ParityEnrollmentError("parity_enrollment_conflict")


def enroll_parity_record(
    held: Iterable[object], enrollment: object
) -> tuple[ParityEnrollment, ...]:
    """Add one enrollment to the held records, keyed by the pair it binds.

    The key is the two run receipt digests in sorted order, so one pair binds one
    record whichever way round it was enrolled. An identical re-enrollment of one
    pair is idempotent; any other content for that pair refuses, as does a record
    that gives one run id a second receipt digest. The held records are read, never
    mutated: this returns a new tuple and takes no lock, so two writers over one
    view are the holder's problem, not this function's.
    """
    record = _enrollment(enrollment)
    records = tuple(_enrollment(item) for item in held)
    _run_digest_agreement((*records, record))
    key = enrollment_pair_key(record)
    digest = parity_enrollment_digest(record)
    for item in records:
        if enrollment_pair_key(item) != key:
            continue
        if parity_enrollment_digest(item) != digest:
            raise ParityEnrollmentError("parity_enrollment_conflict")
        return records
    return (*records, record)


def eligible_run_count(held: Iterable[object]) -> int:
    """How many independent eligible runs the held enrollments prove.

    Distinct cutoff and source window pairs, never enrollment rows: repeated
    executions of one source window are one eligible run however many times the
    chain ran, and a repeat capture of one window is not a second window.
    """
    return len({enrollment_snapshot_key(item) for item in held})


def parity_outputs_identical(held: Iterable[object]) -> bool:
    """Whether every held record binds two byte equal row sets.

    An empty enrollment refuses rather than answering true over no record. The
    enrollment is empty until a native run produces the first pair, so an empty
    aggregate would otherwise turn an absence into a parity result.
    """
    records = tuple(_enrollment(item) for item in held)
    if not records:
        raise ParityEnrollmentError("parity_enrollment_empty")
    return all(record.outputs_identical for record in records)
