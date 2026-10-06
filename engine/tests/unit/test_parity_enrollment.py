"""Parity enrollment binds one legacy and one current output to one source window."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest
from scripts.staging.replay_open_intelligence import (
    APPLY_OBSERVATION_METHOD,
    _source_window_digest,
)
from src.analysis.open_intelligence import daily_composer, pipeline
from src.analysis.open_intelligence.daily_composer import (
    DAILY_OBSERVATION_METHOD,
    CompositionFacts,
    composition_receipt_inputs,
)
from src.analysis.open_intelligence.parity_enrollment import (
    PARITY_ENROLLMENT_CONTRACT_VERSION,
    ParityEnrollment,
    ParityEnrollmentError,
    _completed,
    build_parity_enrollment,
    eligible_run_count,
    enroll_parity_record,
    enrollment_pair_key,
    enrollment_snapshot_key,
    enrollment_source_window_key,
    parity_enrollment_digest,
    parity_outputs_identical,
    verify_parity_enrollment,
)
from src.analysis.open_intelligence.production_snapshot import (
    _SNAPSHOT_FIELDS,
    SNAPSHOT_CONTRACT_VERSION,
)
from src.analysis.open_intelligence.production_snapshot import (
    _digest as snapshot_self_digest,
)
from src.analysis.open_intelligence.run_receipts import (
    RUN_RECEIPT_CONTRACT_VERSION,
    OpenIntelligenceRunReceipt,
    build_run_receipt,
    run_receipt_digest,
)
from src.contracts.open_intelligence import ResolvedScope

_SNAPSHOT = "a" * 64
_LEGACY_ROWS = "b" * 64
_CURRENT_ROWS = "c" * 64
_SOURCE_SHA = "769408fc55680ca9d920a1e94dacaddba2c0bb91"
_ENROLLED_AT = datetime(2026, 9, 11, 7, 30, tzinfo=UTC)


def _fields(**overrides) -> dict:
    base = {
        "run_contract_version": RUN_RECEIPT_CONTRACT_VERSION,
        "run_id": "oi_20260910_za_ng_ke_legacy",
        "client_scope_id": "open_intelligence_staging",
        "market_scope": ("za", "ng", "ke"),
        "signal_date": date(2026, 9, 10),
        "observation_start": date(2026, 9, 4),
        "observation_end": date(2026, 9, 10),
        "observation_method": "dynamic_signal_identity_v1",
        "source_window_digest": _SNAPSHOT,
        "cluster_build_version": "hybrid_graph_v2",
        "source_family_map_version": "family_map_v2",
        "rule_version": "rule_v4",
        "status": "completed",
        "complete_partitions": True,
        "display_release_state": "enabled",
        "candidate_count": 12,
        "evidence_count": 40,
        "membership_count": 12,
        "lineage_count": 3,
        "analysis_count": 4,
        "prediction_count": 12,
        "row_set_digest": _LEGACY_ROWS,
        "source_sha": _SOURCE_SHA,
        "completed_at": datetime(2026, 9, 11, 6, 38, 12, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def legacy(**overrides) -> OpenIntelligenceRunReceipt:
    return build_run_receipt(**_fields(**overrides))


def current(**overrides) -> OpenIntelligenceRunReceipt:
    base = {
        "run_id": "oi_20260910_za_ng_ke_current",
        "observation_method": "dynamic_signal_identity_v3",
        "cluster_build_version": "hybrid_graph_v3",
        "row_set_digest": _CURRENT_ROWS,
    }
    base.update(overrides)
    return build_run_receipt(**_fields(**base))


def enrollment(**overrides) -> ParityEnrollment:
    return build_parity_enrollment(
        legacy=overrides.pop("legacy", legacy()),
        current=overrides.pop("current", current()),
        enrolled_at=overrides.pop("enrolled_at", _ENROLLED_AT),
    )


def test_one_eligible_completed_run_enrolls_both_outputs_against_one_snapshot():
    record = enrollment()
    assert record.contract_version == PARITY_ENROLLMENT_CONTRACT_VERSION
    assert record.signal_date == date(2026, 9, 10)
    assert record.observation_start == date(2026, 9, 4)
    assert record.observation_end == date(2026, 9, 10)
    assert record.source_window_digest == _SNAPSHOT
    assert record.client_scope_id == "open_intelligence_staging"
    assert record.market_scope == ("za", "ng", "ke")
    assert record.legacy_run_id == "oi_20260910_za_ng_ke_legacy"
    assert record.current_run_id == "oi_20260910_za_ng_ke_current"
    assert record.legacy_row_set_digest == _LEGACY_ROWS
    assert record.current_row_set_digest == _CURRENT_ROWS
    assert record.legacy_observation_method == "dynamic_signal_identity_v1"
    assert record.current_observation_method == "dynamic_signal_identity_v3"
    assert record.legacy_cluster_build_version == "hybrid_graph_v2"
    assert record.current_cluster_build_version == "hybrid_graph_v3"
    assert record.legacy_receipt_digest == run_receipt_digest(legacy())
    assert record.current_receipt_digest == run_receipt_digest(current())
    assert record.legacy_receipt == legacy()
    assert record.current_receipt == current()
    passed_in = legacy()
    assert (
        build_parity_enrollment(
            legacy=passed_in, current=current(), enrolled_at=_ENROLLED_AT
        ).legacy_receipt
        is passed_in
    )
    assert record.enrolled_at == _ENROLLED_AT
    # The snapshot key is the cutoff and the source window content key. It is
    # deliberately not source_window_digest, which moves on a repeat capture of
    # the composed window. The key is pinned to a value derived here from the
    # canonical rendering by hand, not to the function composed with itself: an
    # assertion against enrollment_source_window_key(record) would hold however
    # the window key were defined and would pin nothing.
    expected_window_key = hashlib.sha256(
        b'{"client_scope_id":"open_intelligence_staging",'
        b'"domain":"open_intelligence_parity_source_window_key_v1",'
        b'"market_scope":["za","ng","ke"],'
        b'"observation_end":"2026-09-10","observation_start":"2026-09-04"}'
    ).hexdigest()
    assert enrollment_source_window_key(record) == expected_window_key
    assert enrollment_snapshot_key(record) == ("2026-09-10", expected_window_key)
    assert enrollment_source_window_key(record) != _SNAPSHOT
    assert parity_enrollment_digest(record) == parity_enrollment_digest(enrollment())
    with pytest.raises(FrozenInstanceError):
        record.current_run_id = "other"


def test_the_enrollment_instant_is_carried_whole():
    # The instant is projected as it is, to the microsecond. Nothing else in this
    # file enrolls at an instant carrying one, so a projection that truncated the
    # instant passed every test and moved every enrollment digest.
    precise = datetime(2026, 9, 11, 7, 30, 15, 123456, tzinfo=UTC)
    record = enrollment(enrolled_at=precise)
    assert record.enrolled_at == precise
    assert record.enrolled_at.microsecond == 123456


def test_an_enrollment_at_the_exact_completion_instant_is_not_early():
    # The rule is no earlier than either completion, so the boundary enrolls. No
    # other test sits on it, so a strict boundary passed every test while refusing
    # a legitimate enrollment.
    completed = datetime(2026, 9, 11, 6, 38, 12, tzinfo=UTC)
    assert legacy().completed_at == completed
    assert current().completed_at == completed
    assert (
        build_parity_enrollment(
            legacy=legacy(), current=current(), enrolled_at=completed
        ).enrolled_at
        == completed
    )


def test_a_cutoff_that_is_no_kind_of_date_refuses_by_name():
    # The cutoff guard refuses a timestamp and it refuses anything that is not a
    # date at all. Only the timestamp clause had a test, so a guard that dropped
    # the date clause let a string cutoff through to the field loop, which names
    # the projection rather than the cutoff, and through to isoformat, which is
    # outside this module's refusal vocabulary.
    record = enrollment()
    with pytest.raises(ParityEnrollmentError, match="parity_cutoff_invalid:signal_date"):
        replace(record, signal_date="2026-09-10")
    with pytest.raises(ParityEnrollmentError, match="parity_cutoff_invalid:observation_start"):
        replace(record, observation_start="2026-09-04")
    tampered = enrollment()
    object.__setattr__(tampered, "observation_end", "2026-09-10")
    with pytest.raises(ParityEnrollmentError, match="parity_cutoff_invalid:observation_end"):
        enrollment_source_window_key(tampered)


def test_one_legacy_run_id_names_one_receipt_across_the_held_records():
    # The run id agreement rule reads both sides of every record. Only the current
    # side had a test, so dropping the legacy side left two records giving one
    # legacy run id two receipt digests enrolling side by side.
    first = enrollment()
    relabelled = enrollment(
        legacy=legacy(row_set_digest="e" * 64),
        current=current(run_id="oi_20260910_za_ng_ke_current_two"),
    )
    assert relabelled.legacy_run_id == first.legacy_run_id
    assert relabelled.legacy_receipt_digest != first.legacy_receipt_digest
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_conflict"):
        enroll_parity_record(enroll_parity_record((), first), relabelled)


def test_the_enrollment_contract_version_is_pinned_to_its_literal():
    # Every other assertion on this constant reads the expected value out of the
    # module under test, so the constant could be changed to any string and this
    # file would still pass. The literal is the pin, the way the window key
    # preimage above is pinned rather than recomputed by the code it checks.
    assert PARITY_ENROLLMENT_CONTRACT_VERSION == "open_intelligence_parity_enrollment_v1"
    assert enrollment().contract_version == "open_intelligence_parity_enrollment_v1"


def test_the_record_carries_no_accuracy_claim():
    record = enrollment()
    names = set(type(record).__dataclass_fields__)
    assert not {name for name in names if "accura" in name or "score" in name}
    assert record.outputs_identical is False
    assert enrollment(current=current(row_set_digest=_LEGACY_ROWS)).outputs_identical is True


@pytest.mark.parametrize(
    "overrides",
    [{"status": "failed"}, {"complete_partitions": False}, {"display_release_state": "blocked"}],
)
def test_an_incomplete_run_never_enrolls(overrides):
    with pytest.raises(ParityEnrollmentError, match="run_not_completed"):
        enrollment(legacy=legacy(**overrides))
    with pytest.raises(ParityEnrollmentError, match="run_not_completed"):
        enrollment(current=current(**overrides))


def test_a_run_whose_observation_window_is_still_open_never_enrolls():
    # Eligibility is the display qualification helper's own answer, which includes
    # the closed observation window clause this module must not restate in part.
    early = {"completed_at": datetime(2026, 9, 10, 20, 0, tzinfo=UTC)}
    with pytest.raises(ParityEnrollmentError, match="run_not_completed:legacy"):
        build_parity_enrollment(
            legacy=legacy(**early),
            current=current(**early),
            enrolled_at=datetime(2026, 9, 10, 23, 0, tzinfo=UTC),
        )
    assert build_parity_enrollment(
        legacy=legacy(**early),
        current=current(**early),
        enrolled_at=datetime(2026, 9, 11, 0, 30, tzinfo=UTC),
    ).signal_date == date(2026, 9, 10)


def test_a_different_cutoff_never_pairs():
    other_day = {
        "signal_date": date(2026, 9, 9),
        "observation_end": date(2026, 9, 9),
        "run_id": "oi_20260909_za_ng_ke_current",
    }
    with pytest.raises(ParityEnrollmentError, match="parity_cutoff_differs"):
        enrollment(current=current(**other_day))


def test_a_different_source_snapshot_never_pairs():
    with pytest.raises(ParityEnrollmentError, match="parity_source_snapshot_differs"):
        enrollment(current=current(source_window_digest="d" * 64))
    with pytest.raises(ParityEnrollmentError, match="parity_source_window_differs"):
        enrollment(current=current(observation_start=date(2026, 9, 5)))


@pytest.mark.parametrize(
    "overrides",
    [{"client_scope_id": "open_intelligence_staging_b"}, {"market_scope": ("za", "ng")}],
)
def test_a_different_scope_never_pairs(overrides):
    with pytest.raises(ParityEnrollmentError, match="parity_scope_differs"):
        enrollment(current=current(**overrides))


@pytest.mark.parametrize("side", ["legacy", "current"])
def test_a_qa_canary_run_never_enrolls(side):
    builder = {"legacy": legacy, "current": current}[side]
    with pytest.raises(ParityEnrollmentError, match="parity_scope_unqualified"):
        enrollment(**{side: builder(client_scope_id="qa_canary")})


def test_one_run_cannot_be_its_own_parity_pair():
    with pytest.raises(ParityEnrollmentError, match="parity_runs_identical"):
        enrollment(current=current(run_id="oi_20260910_za_ng_ke_legacy"))


def test_two_receipts_of_one_chain_are_not_a_parity_pair():
    """The gate refuses two receipts whose observation method strings are equal.

    It does not establish that two receipts came from two chains. Nothing compared
    the method string until this gate. Two receipts both carrying the retained
    replay's observation method and one cluster build version, differing only in
    run id and completion instant, enrolled cleanly, and a record whose two row
    set digests agreed then reported ``outputs_identical`` true. A later reader
    would take that for the legacy chain and the current chain agreeing when it
    is one chain compared with itself. The only thing that kept it off the
    shipped replay is that its run id happens to be a constant, which is an
    accident and not a rule.

    The observation method is a caller supplied string the receipt contract
    validates only as nonempty, so two receipts of one chain carrying two versions
    of the string pass this gate as two chains. The cluster build version cannot
    carry the gate either: two builds of one chain differ in it, so it separates
    builds rather than chains.
    """
    one_chain = {"observation_method": APPLY_OBSERVATION_METHOD}
    left = legacy(**one_chain)
    right = current(
        run_id="oi_20260910_za_ng_ke_replay_again",
        cluster_build_version="hybrid_graph_v2",
        row_set_digest=_LEGACY_ROWS,
        completed_at=datetime(2026, 9, 11, 6, 52, 41, tzinfo=UTC),
        **one_chain,
    )
    assert left.observation_method == right.observation_method
    assert left.cluster_build_version == right.cluster_build_version
    assert left.row_set_digest == right.row_set_digest
    # Two genuinely different receipts: they pass every other pairing rule.
    assert run_receipt_digest(left) != run_receipt_digest(right)
    with pytest.raises(ParityEnrollmentError, match="parity_chains_identical"):
        build_parity_enrollment(legacy=left, current=right, enrolled_at=_ENROLLED_AT)

    # The gate reads the observation method and nothing else: a pair that differs
    # only in cluster build version is still one chain.
    with pytest.raises(ParityEnrollmentError, match="parity_chains_identical"):
        build_parity_enrollment(
            legacy=left,
            current=replace(right, cluster_build_version="hybrid_graph_v3"),
            enrolled_at=_ENROLLED_AT,
        )

    # And a pair that differs in observation method and in nothing else enrolls.
    assert (
        build_parity_enrollment(
            legacy=left,
            current=replace(right, observation_method=DAILY_OBSERVATION_METHOD),
            enrolled_at=_ENROLLED_AT,
        ).outputs_identical
        is True
    )


def test_a_receipt_restated_under_another_name_is_not_a_counterparty():
    # Identity is the receipt digest, not the run id, which is a restatable name.
    # A byte identical receipt relabelled is one run, never its own counterparty.
    restated = replace(legacy(), run_id="oi_20260910_za_ng_ke_legacy_restated")
    assert run_receipt_digest(restated) != run_receipt_digest(legacy())
    with pytest.raises(ParityEnrollmentError, match="parity_runs_identical"):
        build_parity_enrollment(legacy=legacy(), current=restated, enrolled_at=_ENROLLED_AT)


@pytest.mark.parametrize("side", ["legacy", "current"])
def test_enrollment_cannot_predate_either_run_it_binds(side):
    builder = {"legacy": legacy, "current": current}[side]
    later = builder(completed_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    with pytest.raises(ParityEnrollmentError, match="parity_enrolled_before_completion"):
        build_parity_enrollment(
            legacy=later if side == "legacy" else legacy(),
            current=later if side == "current" else current(),
            enrolled_at=datetime(2026, 9, 11, 7, 59, 59, tzinfo=UTC),
        )


def test_the_enrollment_instant_must_be_timezone_aware():
    # The refusal carries a code, like every other refusal in the module. It used
    # to raise a bare sentence, so a caller matching on codes could not classify
    # it, and these three assertions matched that sentence instead.
    with pytest.raises(ParityEnrollmentError, match="parity_enrolled_at_invalid"):
        enrollment(enrolled_at=datetime(2026, 9, 11, 7, 30))
    with pytest.raises(ParityEnrollmentError, match="parity_enrolled_at_invalid"):
        enrollment(enrolled_at="2026-09-11T07:30:00+00:00")


def test_the_enrollment_instant_gate_is_a_type_gate_not_a_duck_typed_check():
    class _LooksLikeAnInstant:
        def utcoffset(self):
            return timedelta(0)

        def astimezone(self, tz=None):
            return _ENROLLED_AT

        def date(self):
            return date(2026, 9, 11)

        def __lt__(self, other):
            return False

    with pytest.raises(ParityEnrollmentError, match="parity_enrolled_at_invalid"):
        enrollment(enrolled_at=_LooksLikeAnInstant())


def test_the_enrollment_instant_is_normalised_to_one_timezone():
    record = enrollment(
        enrolled_at=datetime(2026, 9, 11, 9, 30, tzinfo=timezone(timedelta(hours=2)))
    )
    assert record.enrolled_at == _ENROLLED_AT
    assert record.enrolled_at.utcoffset() == timedelta(0)
    assert record.enrolled_at.tzinfo is UTC
    assert parity_enrollment_digest(record) == parity_enrollment_digest(enrollment())


def test_the_enrollment_digest_binds_the_enrollment_instant():
    later = enrollment(enrolled_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC))
    assert later.enrolled_at != enrollment().enrolled_at
    assert parity_enrollment_digest(later) != parity_enrollment_digest(enrollment())


@pytest.mark.parametrize("side", ["legacy", "current"])
def test_only_a_run_receipt_enrolls(side):
    with pytest.raises(ParityEnrollmentError, match=f"parity_receipt_invalid:{side}"):
        enrollment(**{side: {"run_id": "oi_20260910_za_ng_ke_legacy"}})


@pytest.mark.parametrize("side", ["legacy", "current"])
def test_a_receipt_of_another_contract_version_never_enrolls(side):
    builder = {"legacy": legacy, "current": current}[side]
    replaced = replace(builder(), run_contract_version="open_intelligence_run_receipt_v2")
    with pytest.raises(ParityEnrollmentError, match=f"parity_receipt_invalid:{side}"):
        enrollment(**{side: replaced})


def test_a_record_is_validated_on_construction_and_carries_the_receipts_it_binds():
    record = enrollment()
    with pytest.raises(ParityEnrollmentError, match="parity_receipt_invalid:legacy"):
        replace(record, legacy_receipt={"run_id": "oi_hand_built"})
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        replace(record, legacy_receipt_digest="0" * 64)
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        replace(record, current_run_id="oi_hand_built")


def test_swapping_the_two_receipt_digests_inside_a_record_never_counts():
    record = enrollment()
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        replace(
            record,
            legacy_receipt_digest=record.current_receipt_digest,
            current_receipt_digest=record.legacy_receipt_digest,
        )
    tampered = enrollment()
    object.__setattr__(tampered, "legacy_receipt_digest", record.current_receipt_digest)
    object.__setattr__(tampered, "current_receipt_digest", record.legacy_receipt_digest)
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        eligible_run_count((tampered,))
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        enroll_parity_record((), tampered)


def test_a_record_verifies_only_against_the_receipts_it_claims_to_bind():
    record = enrollment()
    assert verify_parity_enrollment(record, legacy=legacy(), current=current()) is record
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        verify_parity_enrollment(record, legacy=legacy(), current=current(row_set_digest="e" * 64))
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        verify_parity_enrollment(record, legacy=current(), current=legacy())


def test_a_cutoff_expressed_as_a_timestamp_never_keys_separately():
    # A datetime is a date subclass, so an unguarded isoformat would key one
    # cutoff twice. The guard refuses rather than normalising.
    with pytest.raises(ParityEnrollmentError, match="parity_cutoff_invalid:signal_date"):
        replace(enrollment(), signal_date=datetime(2026, 9, 10, tzinfo=UTC))
    tampered = enrollment()
    object.__setattr__(tampered, "signal_date", datetime(2026, 9, 10, tzinfo=UTC))
    with pytest.raises(ParityEnrollmentError, match="parity_cutoff_invalid:signal_date"):
        enrollment_snapshot_key(tampered)
    with pytest.raises(ParityEnrollmentError, match="parity_cutoff_invalid:signal_date"):
        eligible_run_count((tampered,))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observation_start", datetime(2026, 9, 4, tzinfo=UTC)),
        ("observation_end", datetime(2026, 9, 10, tzinfo=UTC)),
    ],
)
def test_an_observation_bound_expressed_as_a_timestamp_names_its_own_field(field, value):
    # _validate guards all three cutoffs and only signal_date had a test. With
    # either of the other two guards removed a tampered record still refused, but
    # as parity_enrollment_unproven out of the field loop, which sends a reader to
    # the projection rather than to the bound that is not a date.
    record = enrollment()
    with pytest.raises(ParityEnrollmentError, match=f"parity_cutoff_invalid:{field}"):
        replace(record, **{field: value})
    tampered = enrollment()
    object.__setattr__(tampered, field, value)
    with pytest.raises(ParityEnrollmentError, match=f"parity_cutoff_invalid:{field}"):
        enrollment_source_window_key(tampered)


def test_an_open_observation_window_on_the_current_side_refuses_on_that_side():
    # The window clause is checked on both sides, and the legacy side raises first
    # in the test above, so the current side's call was never exercised: removing
    # it left every test in this file passing. Here the legacy window has closed by
    # the enrollment day and the current window has not.
    closed = legacy()
    still_open = current(signal_date=date(2026, 9, 11), observation_end=date(2026, 9, 11))
    instant = datetime(2026, 9, 11, 7, 30, tzinfo=UTC)
    assert closed.observation_end < instant.date()
    assert still_open.observation_end == instant.date()
    with pytest.raises(ParityEnrollmentError, match="run_not_completed:current"):
        build_parity_enrollment(legacy=closed, current=still_open, enrolled_at=instant)


def test_every_held_record_is_validated_and_a_malformed_one_is_named():
    # enroll_parity_record validates the record being added and every held record.
    # Only the added one had a test: with the held validation removed a held item
    # that is not an enrollment escaped as an AttributeError out of the run id
    # agreement rule, which is the fault class this slice closed for the new
    # record and left open for the held ones.
    record = enrollment()
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_invalid"):
        enroll_parity_record(({"signal_date": "2026-09-10"},), record)
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_invalid"):
        enroll_parity_record((record, {"signal_date": "2026-09-10"}), record)


def test_a_conflicting_re_enrollment_of_one_run_refuses_and_an_identical_one_is_idempotent():
    record = enrollment()
    held = enroll_parity_record((), record)
    assert held == (record,)
    assert enroll_parity_record(held, enrollment()) == held
    changed = enrollment(current=current(row_set_digest="e" * 64))
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_conflict"):
        enroll_parity_record(held, changed)
    assert held == (record,)


def test_one_pair_binds_one_record_whichever_way_round_it_is_enrolled():
    forward = enrollment()
    reverse = build_parity_enrollment(legacy=current(), current=legacy(), enrolled_at=_ENROLLED_AT)
    assert enrollment_pair_key(forward) == tuple(
        sorted((run_receipt_digest(legacy()), run_receipt_digest(current())))
    )
    assert enrollment_pair_key(reverse) == enrollment_pair_key(forward)
    assert parity_enrollment_digest(reverse) != parity_enrollment_digest(forward)
    held = enroll_parity_record((), forward)
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_conflict"):
        enroll_parity_record(held, reverse)


def test_enrollment_is_a_pure_value_and_uniqueness_belongs_to_whatever_holds_it():
    record = enrollment()
    view = [record]
    held = enroll_parity_record(view, enrollment(current=current(run_id="oi_second_current")))
    assert view == [record]
    assert len(held) == 2
    # Two writers reading one view each produce a value; nothing here serialises
    # them, so the holder enforces the pair key and the run id agreement.
    left = enroll_parity_record((record,), enrollment(current=current(run_id="oi_left_current")))
    right = enroll_parity_record((record,), enrollment(current=current(run_id="oi_right_current")))
    assert len(left) == len(right) == 2
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_conflict"):
        enroll_parity_record(left, enrollment(current=current(row_set_digest="e" * 64)))


def test_repeated_executions_of_one_snapshot_are_not_independent_runs():
    first = enrollment()
    repeat = enrollment(
        current=current(run_id="oi_20260910_za_ng_ke_current_retry"),
        enrolled_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
    )
    fresh_day = {
        "signal_date": date(2026, 9, 11),
        "observation_end": date(2026, 9, 11),
        "source_window_digest": "f" * 64,
    }
    second_day = build_parity_enrollment(
        legacy=legacy(run_id="oi_20260911_za_ng_ke_legacy", **fresh_day),
        current=current(run_id="oi_20260911_za_ng_ke_current", **fresh_day),
        enrolled_at=datetime(2026, 9, 12, 7, 30, tzinfo=UTC),
    )
    held = enroll_parity_record(enroll_parity_record((), first), repeat)
    assert len(held) == 2
    assert eligible_run_count(held) == 1
    assert eligible_run_count(enroll_parity_record(held, second_day)) == 2
    assert eligible_run_count(()) == 0


def test_a_repeat_capture_of_one_source_window_is_not_a_second_eligible_run():
    # Two producers in this repository write source_window_digest onto a run
    # receipt. The daily composer carries the captured snapshot's own self digest,
    # and the snapshot it hashes carries the capture instant and a snapshot id
    # derived from it, so a recapture of byte identical source moves that digest.
    # The retained replay hashes the copy run id with the per table manifest and
    # matched row counts and does not move on a repeat under one copy run id.
    recapture = {"source_window_digest": "9" * 64}
    first = enrollment()
    again = build_parity_enrollment(
        legacy=legacy(run_id="oi_20260910_za_ng_ke_legacy_recapture", **recapture),
        current=current(run_id="oi_20260910_za_ng_ke_current_recapture", **recapture),
        enrolled_at=datetime(2026, 9, 11, 7, 35, tzinfo=UTC),
    )
    assert again.source_window_digest != first.source_window_digest
    assert enrollment_source_window_key(again) == enrollment_source_window_key(first)
    assert enrollment_snapshot_key(again) == enrollment_snapshot_key(first)
    held = enroll_parity_record(enroll_parity_record((), first), again)
    assert len(held) == 2
    assert eligible_run_count(held) == 1


def test_a_different_observation_window_is_a_different_eligible_run():
    first = enrollment()
    wider = {"observation_start": date(2026, 9, 3), "source_window_digest": "8" * 64}
    other = build_parity_enrollment(
        legacy=legacy(run_id="oi_20260910_za_ng_ke_legacy_wide", **wider),
        current=current(run_id="oi_20260910_za_ng_ke_current_wide", **wider),
        enrolled_at=_ENROLLED_AT,
    )
    assert enrollment_source_window_key(other) != enrollment_source_window_key(first)
    assert eligible_run_count(enroll_parity_record(enroll_parity_record((), first), other)) == 2


def test_an_empty_enrollment_is_not_a_parity_result():
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_empty"):
        parity_outputs_identical(())
    assert parity_outputs_identical((enrollment(),)) is False
    agreeing = enrollment(
        current=current(run_id="oi_20260910_za_ng_ke_current_twin", row_set_digest=_LEGACY_ROWS)
    )
    assert parity_outputs_identical((agreeing,)) is True
    assert parity_outputs_identical(enroll_parity_record((agreeing,), enrollment())) is False


def test_eligible_run_count_requires_enrollment_records():
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_invalid"):
        eligible_run_count(({"signal_date": "2026-09-10"},))
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_invalid"):
        enroll_parity_record((), {"signal_date": "2026-09-10"})
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_invalid"):
        parity_enrollment_digest({"signal_date": "2026-09-10"})
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_invalid"):
        parity_outputs_identical(({"signal_date": "2026-09-10"},))


def _copy_completeness(*, rows_by_table: dict[str, int]) -> tuple[dict[str, object], ...]:
    """The partition completeness rows the retained replay hashes, exactly shaped."""
    return tuple(
        {"source_table": table, "manifest_rows": count, "matched_rows": count}
        for table, count in sorted(rows_by_table.items())
    )


def _composed_snapshot(*, cutoff: str, captured_at: str, rows_by_table: dict[str, int]) -> dict:
    """A production snapshot as the daily composer self digests it, minus that digest."""
    snapshot = {
        "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
        "snapshot_id": "production_snapshot_" + "0" * 64,
        "cutoff_date": cutoff,
        "source_as_of": f"{cutoff}T00:00:00+00:00",
        "captured_at": captured_at,
        "client_scope_id": "open_intelligence_staging",
        "market_scope": ["za", "ng", "ke"],
        "source_relation_set_id": "production_v1",
        "row_counts_by_table": dict(sorted(rows_by_table.items())),
        "section_counts": dict(sorted(rows_by_table.items())),
        "section_identity_sets": {},
        "table_digests": {},
        "rows_by_table": {},
        "table_snapshot_receipts": {},
        "physical_capture_complete": False,
        "coverage_receipt_refs": [],
        "coverage_limitations": [],
    }
    assert set(snapshot) == set(_SNAPSHOT_FIELDS) - {"source_digest"}
    return snapshot


def _call_arguments(module, call_name: str) -> tuple[list[str], dict[str, str]]:
    """One call's arguments as they are written in a module's own source."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == call_name
    ]
    assert len(calls) == 1, (call_name, len(calls))
    call = calls[0]
    return (
        [ast.unparse(argument) for argument in call.args],
        {
            keyword.arg: ast.unparse(keyword.value)
            for keyword in call.keywords
            if keyword.arg is not None
        },
    )


def _composer_over(facts: CompositionFacts):
    """A stand in carrying only what ``composition_receipt_inputs`` reads."""

    class _Composer:
        scope = ResolvedScope(
            "open_intelligence_staging",
            ("za", "ng", "ke"),
            "ogilvy",
            (),
            "open_intelligence",
            facts.run_id,
            "2.0.0",
        )
        compositions = MappingProxyType({facts.run_id: facts})

    return _Composer()


def test_the_two_real_producer_chains_cannot_satisfy_the_source_window_gate():
    """The pairing gate refuses every pair the two producer chains can present.

    Two producers write ``source_window_digest`` onto a run receipt. The retained
    replay writes ``dynamic_source_copy_apply_v1`` and hashes the copy run id with
    the per table manifest and matched row counts. The daily composer writes
    ``daily_source_snapshot_compose_v1`` and carries the captured snapshot's own
    self digest. Only the retained replay ships and runs: the composer is reached
    from no caller in ``src`` or ``scripts`` and exists as a chain only in tests.

    Both real digest formulas are called here over one window, and the composer's
    own receipt inputs helper is driven over composition facts carrying a real
    snapshot self digest, so the receipt this test pairs is the receipt the
    composer's persist stage would build. This test therefore fails the day either
    formula changes, and the day the composer stops carrying the snapshot digest
    into its receipt inputs.

    One step of the composer chain is not executed here, because running it needs
    a warehouse, a semantic provider and the certified rule bundle: the assignment
    that puts ``result.source_snapshot_digest`` into the composition facts, and the
    pipeline assignment that puts the snapshot's ``source_digest`` into that field.
    Those two are pinned below by reading the composer's and the pipeline's own
    source, which is weaker than executing them and is not prose.

    The rest of this file pairs receipts carrying observation methods no producer
    in ``src`` or ``scripts`` emits, and one invented digest shared by both sides,
    which is why nothing else here notices.
    """
    rows_by_table = {"signal_candidates": 118, "signal_evidence": 946}
    legacy_digest = _source_window_digest(
        _copy_completeness(rows_by_table=rows_by_table), "copy_run_20260910"
    )
    current_digest = snapshot_self_digest(
        _composed_snapshot(
            cutoff="2026-09-10",
            captured_at="2026-09-11T02:15:00+00:00",
            rows_by_table=rows_by_table,
        )
    )
    assert legacy_digest != current_digest

    # Not an artefact of these inputs. Each digest is a function of its own chain's
    # material and of nothing the other chain holds: moving the copy run id moves
    # only the replay digest, and moving the capture instant moves only the
    # composer digest, so there is no pair of inputs that walks the two together.
    other_copy_run = _source_window_digest(
        _copy_completeness(rows_by_table=rows_by_table), "copy_run_20260910_again"
    )
    other_capture = snapshot_self_digest(
        _composed_snapshot(
            cutoff="2026-09-10",
            captured_at="2026-09-11T09:45:00+00:00",
            rows_by_table=rows_by_table,
        )
    )
    assert other_copy_run != legacy_digest
    assert other_capture != current_digest
    assert {legacy_digest, other_copy_run}.isdisjoint({current_digest, other_capture})

    # The composer's own receipt inputs helper, driven over composition facts that
    # carry the snapshot self digest computed above. This is the seam the persist
    # stage builds its run receipt through, so the digest the receipt carries is
    # read out of the composer rather than restated here.
    facts = CompositionFacts(
        run_id="oi_20260910_za_ng_ke_compose",
        result_id="result_20260910",
        composed_at=datetime(2026, 9, 11, 2, 15, tzinfo=UTC),
        source_window_digest=current_digest,
        observation_start=date(2026, 9, 4),
        observation_end=date(2026, 9, 10),
        cluster_build_version="hybrid_graph_v3",
        rule_version="composition_rules_v3",
        complete_partitions=True,
        geography_by_component=MappingProxyType({}),
    )
    composed = composition_receipt_inputs(_composer_over(facts), source_sha=_SOURCE_SHA)(
        facts.run_id
    )
    assert composed["observation_method"] == DAILY_OBSERVATION_METHOD
    assert composed["source_window_digest"] == current_digest

    # The one composer step this test cannot execute, pinned to the composer's and
    # the pipeline's own source: the facts carry the pipeline result's snapshot
    # digest, and that digest is the captured snapshot's own self digest.
    _, composition_fields = _call_arguments(daily_composer, "CompositionFacts")
    assert composition_fields["source_window_digest"] == "result.source_snapshot_digest"
    provenance_arguments, _ = _call_arguments(pipeline, "ProvenanceSignalRunResult")
    assert provenance_arguments[2] == "snapshot['source_digest']"

    # And so the record cannot be built from the two real observation methods.
    left = legacy(observation_method=APPLY_OBSERVATION_METHOD, source_window_digest=legacy_digest)
    right = build_run_receipt(
        **_fields(
            run_id=facts.run_id,
            signal_date=facts.observation_end,
            observation_start=composed["observation_start"],
            observation_end=facts.observation_end,
            observation_method=composed["observation_method"],
            source_window_digest=composed["source_window_digest"],
            cluster_build_version=composed["cluster_build_version"],
            rule_version=composed["rule_version"],
            source_family_map_version=composed["source_family_map_version"],
            source_sha=composed["source_sha"],
            complete_partitions=composed["complete_partitions"],
            client_scope_id=composed["client_scope_id"],
            market_scope=composed["market_scope"],
            row_set_digest=_CURRENT_ROWS,
        )
    )
    assert left.observation_method == "dynamic_source_copy_apply_v1"
    assert right.observation_method == "daily_source_snapshot_compose_v1"
    assert right.source_window_digest == current_digest
    with pytest.raises(ParityEnrollmentError, match="parity_source_snapshot_differs"):
        build_parity_enrollment(legacy=left, current=right, enrolled_at=_ENROLLED_AT)

    # The fixtures the rest of this file uses carry neither real method.
    assert legacy().observation_method not in {
        APPLY_OBSERVATION_METHOD,
        DAILY_OBSERVATION_METHOD,
    }
    assert current().observation_method not in {
        APPLY_OBSERVATION_METHOD,
        DAILY_OBSERVATION_METHOD,
    }


@pytest.mark.parametrize("side", ["legacy", "current"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("completed_at", datetime(2026, 9, 11, 6, 38, 12)),
        ("observation_end", "2026-09-10"),
        ("signal_date", datetime(2026, 9, 10, tzinfo=UTC)),
        ("market_scope", ("za", "xx")),
        # A market scope written as a list rebuilds to the tuple the contract
        # declares, and the canonical rendering cannot tell the two apart, so
        # only a field for field comparison of the rebuild refuses it.
        ("market_scope", ["za", "ng", "ke"]),
        ("row_set_digest", "not a digest"),
    ],
)
def test_a_receipt_that_did_not_come_through_its_builder_never_enrolls(side, field, value):
    # A receipt whose fields were written past build_run_receipt can hold anything
    # the dataclass will take. Before the receipt is rebuilt from its own fields a
    # naive completion instant escaped as a TypeError from the completion compare,
    # a non date observation end escaped as a TypeError from the display helper,
    # and a malformed market scope was diagnosed as an unfinished run.
    builder = {"legacy": legacy, "current": current}[side]
    tampered = replace(builder())
    object.__setattr__(tampered, field, value)
    with pytest.raises(ParityEnrollmentError, match=f"parity_receipt_invalid:{side}"):
        enrollment(**{side: tampered})


@pytest.mark.parametrize("side", ["legacy", "current"])
def test_a_receipt_with_a_field_removed_never_enrolls(side):
    # Removing a slot escaped as an AttributeError out of asdict, outside this
    # module's refusal vocabulary.
    builder = {"legacy": legacy, "current": current}[side]
    stripped = replace(builder())
    object.__delattr__(stripped, "rule_version")
    with pytest.raises(ParityEnrollmentError, match=f"parity_receipt_invalid:{side}"):
        enrollment(**{side: stripped})


@pytest.mark.parametrize("side", ["legacy", "current"])
def test_a_frozen_receipt_subclass_is_not_this_contract_s_receipt(side):
    # An isinstance check admitted a subclass, including one carrying an invented
    # field that no receipt contract declares. The gate is the exact type.
    @dataclass(frozen=True, slots=True)
    class _Relabelled(OpenIntelligenceRunReceipt):
        pass

    @dataclass(frozen=True, slots=True)
    class _WithAnInventedField(OpenIntelligenceRunReceipt):
        invented_authority: str = "granted"

    builder = {"legacy": legacy, "current": current}[side]
    fields = {name: getattr(builder(), name) for name in type(builder()).__dataclass_fields__}
    for subclass in (_Relabelled, _WithAnInventedField):
        with pytest.raises(ParityEnrollmentError, match=f"parity_receipt_invalid:{side}"):
            enrollment(**{side: subclass(**fields)})


def test_a_receipt_that_cannot_prove_its_contract_is_not_reported_as_an_unfinished_run():
    # _completed used to re-raise every run receipt refusal as run_not_completed,
    # which would send a recovery to the completion fields of a run that finished.
    # _receipt refuses such a receipt on the way in, so this calls the backstop
    # directly to prove the diagnosis it would give names the real fault.
    malformed = replace(legacy())
    object.__setattr__(malformed, "market_scope", ("za", "xx"))
    with pytest.raises(ParityEnrollmentError, match="parity_receipt_invalid:legacy"):
        _completed(malformed, "legacy", today=date(2026, 9, 11))


def test_records_of_differing_scope_over_one_window_are_separate_eligible_runs():
    # No other test in this file enrolls two records of differing scope, because a
    # differing scope refuses at pairing and so never reaches the window key. These
    # three records share one cutoff and one observation window and differ only in
    # the scope both sides agree on, which is the only way the scope fields of
    # enrollment_source_window_key are ever exercised.
    window = {"signal_date": date(2026, 9, 10), "observation_start": date(2026, 9, 4)}

    def _pair_over(scope_id: str, markets: tuple[str, ...], suffix: str) -> ParityEnrollment:
        scope = {"client_scope_id": scope_id, "market_scope": markets, **window}
        return build_parity_enrollment(
            legacy=legacy(run_id=f"oi_20260910_legacy_{suffix}", **scope),
            current=current(run_id=f"oi_20260910_current_{suffix}", **scope),
            enrolled_at=_ENROLLED_AT,
        )

    all_markets = _pair_over("open_intelligence_staging", ("za", "ng", "ke"), "a")
    other_client = _pair_over("open_intelligence_staging_b", ("za", "ng", "ke"), "b")
    fewer_markets = _pair_over("open_intelligence_staging", ("za", "ng"), "c")

    # The three share the cutoff and the observation window exactly, so only the
    # scope fields can separate them.
    assert {record.signal_date for record in (all_markets, other_client, fewer_markets)} == {
        date(2026, 9, 10)
    }
    assert {
        (record.observation_start, record.observation_end)
        for record in (all_markets, other_client, fewer_markets)
    } == {(date(2026, 9, 4), date(2026, 9, 10))}

    # Drop client_scope_id from the key and the first two collapse; drop
    # market_scope and the first and third collapse.
    assert enrollment_source_window_key(all_markets) != enrollment_source_window_key(other_client)
    assert all_markets.market_scope == other_client.market_scope
    assert enrollment_source_window_key(all_markets) != enrollment_source_window_key(fewer_markets)
    assert all_markets.client_scope_id == fewer_markets.client_scope_id

    held = enroll_parity_record(
        enroll_parity_record(enroll_parity_record((), all_markets), other_client), fewer_markets
    )
    assert len(held) == 3
    assert eligible_run_count(held) == 3


def test_a_hand_built_record_carrying_a_non_utc_instant_never_proves():
    # Pairing normalises the instant and datetime equality compares instants across
    # offsets, so the field loop in _validate cannot see a non UTC rendering. The
    # explicit offset check is the only thing that refuses one, and nothing else
    # builds a record by hand.
    record = enrollment()
    rendered = record.enrolled_at.astimezone(timezone(timedelta(hours=2)))
    assert rendered == record.enrolled_at
    assert rendered.utcoffset() == timedelta(hours=2)
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        replace(record, enrolled_at=rendered)
    with pytest.raises(ParityEnrollmentError, match="parity_enrollment_unproven"):
        ParityEnrollment(
            **{
                **{name: getattr(record, name) for name in type(record).__dataclass_fields__},
                "enrolled_at": rendered,
            }
        )
