"""Contract tests for the shared immutable Open Intelligence run receipt.

The receipt is the only commit marker that proves one exact engine run closed.
Every gate here exists because the producer contract forbids a released display
run that cannot prove its own scope, window, counts and digests.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime

import pytest
from src.analysis.open_intelligence.run_receipts import (
    RUN_RECEIPT_CONTRACT_VERSION,
    OpenIntelligenceRunReceipt,
    RunReceiptError,
    build_run_receipt,
    receipt_qualifies_for_display,
    run_receipt_digest,
    select_display_run,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_SOURCE_SHA = "769408fc55680ca9d920a1e94dacaddba2c0bb91"
_TODAY = date(2026, 8, 29)


def _fields(**overrides) -> dict:
    base = {
        "run_contract_version": RUN_RECEIPT_CONTRACT_VERSION,
        "run_id": "oi_20260829_za_ng_ke_001",
        "client_scope_id": "open_intelligence_staging",
        "market_scope": ("za", "ng", "ke"),
        "signal_date": date(2026, 8, 28),
        "observation_start": date(2026, 8, 22),
        "observation_end": date(2026, 8, 28),
        "observation_method": "dynamic_signal_identity_v1",
        "source_window_digest": _DIGEST_A,
        "cluster_build_version": "cluster_v3",
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
        "row_set_digest": _DIGEST_B,
        "source_sha": _SOURCE_SHA,
        "completed_at": datetime(2026, 8, 29, 6, 38, 12, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def _receipt(**overrides) -> OpenIntelligenceRunReceipt:
    return build_run_receipt(**_fields(**overrides))


def test_a_complete_receipt_builds_and_is_frozen():
    receipt = _receipt()
    assert receipt.run_contract_version == RUN_RECEIPT_CONTRACT_VERSION
    assert receipt.market_scope == ("za", "ng", "ke")
    with pytest.raises(FrozenInstanceError):
        receipt.status = "failed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_contract_version": "open_intelligence_run_receipt_v2"},
        {"run_contract_version": ""},
        {"status": "partial"},
        {"display_release_state": "released"},
        {"observation_method": ""},
        {"run_id": ""},
        {"client_scope_id": ""},
    ],
    ids=[
        "unsupported_contract_version",
        "blank_contract_version",
        "unknown_status",
        "unknown_release_state",
        "blank_observation_method",
        "blank_run_id",
        "blank_client_scope",
    ],
)
def test_enumerated_and_required_string_fields_are_rejected(overrides):
    with pytest.raises(RunReceiptError):
        _receipt(**overrides)


@pytest.mark.parametrize(
    "field",
    ["source_window_digest", "row_set_digest"],
)
@pytest.mark.parametrize(
    "bad",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64, ""],
    ids=["upper_case", "too_short", "too_long", "non_hex", "empty"],
)
def test_digests_must_be_sixty_four_lower_case_hex(field, bad):
    with pytest.raises(RunReceiptError):
        _receipt(**{field: bad})


@pytest.mark.parametrize(
    "bad",
    ["769408FC55680CA9D920A1E94DACADDBA2C0BB91", "769408fc", "z" * 40, ""],
    ids=["upper_case", "abbreviated", "non_hex", "empty"],
)
def test_source_sha_must_be_a_full_forty_character_lower_case_sha(bad):
    with pytest.raises(RunReceiptError):
        _receipt(source_sha=bad)


@pytest.mark.parametrize(
    "field",
    [
        "candidate_count",
        "evidence_count",
        "membership_count",
        "lineage_count",
        "analysis_count",
        "prediction_count",
    ],
)
def test_counts_may_not_be_negative(field):
    with pytest.raises(RunReceiptError):
        _receipt(**{field: -1})


def test_counts_of_zero_are_accepted_because_zero_is_a_measured_value():
    receipt = _receipt(candidate_count=0, prediction_count=0)
    assert receipt.candidate_count == 0
    assert receipt.prediction_count == 0


def test_a_boolean_is_not_accepted_where_a_count_is_required():
    with pytest.raises(RunReceiptError):
        _receipt(candidate_count=True)


def test_window_must_be_ordered():
    with pytest.raises(RunReceiptError):
        _receipt(observation_start=date(2026, 8, 29), observation_end=date(2026, 8, 28))


def test_signal_date_must_equal_observation_end():
    with pytest.raises(RunReceiptError):
        _receipt(signal_date=date(2026, 8, 27))


@pytest.mark.parametrize(
    "scope",
    [(), ("za", "za"), ("ng", "za"), ("za", "uk"), ("ZA",)],
    ids=["empty", "duplicate", "wrong_order", "unknown_market", "upper_case"],
)
def test_market_scope_must_be_unique_known_and_in_canonical_order(scope):
    with pytest.raises(RunReceiptError):
        _receipt(market_scope=scope)


def test_completed_at_must_be_timezone_aware_utc():
    with pytest.raises(RunReceiptError):
        _receipt(completed_at=datetime(2026, 8, 29, 6, 38, 12))


def test_digest_is_deterministic_and_order_insensitive_at_the_call_site():
    assert run_receipt_digest(_receipt()) == run_receipt_digest(_receipt())


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": "oi_20260829_za_ng_ke_002"},
        {"row_set_digest": "c" * 64},
        {"candidate_count": 13},
        {"display_release_state": "blocked"},
        {"completed_at": datetime(2026, 8, 29, 6, 38, 13, tzinfo=UTC)},
    ],
    ids=["run_id", "row_set_digest", "count", "release_state", "completed_at"],
)
def test_digest_changes_when_any_bound_field_changes(overrides):
    assert run_receipt_digest(_receipt()) != run_receipt_digest(_receipt(**overrides))


def test_a_fully_released_closed_run_qualifies():
    assert receipt_qualifies_for_display(_receipt(), requested_markets=("za",), today=_TODAY)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "failed"},
        {"complete_partitions": False},
        {"display_release_state": "blocked"},
        {"client_scope_id": "qa_canary"},
    ],
    ids=["failed_run", "incomplete_partitions", "release_blocked", "qa_scope"],
)
def test_a_run_missing_any_release_gate_does_not_qualify(overrides):
    assert not receipt_qualifies_for_display(
        _receipt(**overrides), requested_markets=("za",), today=_TODAY
    )


def test_a_window_ending_today_does_not_qualify_because_it_is_not_closed():
    receipt = _receipt(
        signal_date=_TODAY, observation_end=_TODAY, observation_start=date(2026, 8, 23)
    )
    assert not receipt_qualifies_for_display(receipt, requested_markets=("za",), today=_TODAY)


def test_a_window_ending_in_the_future_does_not_qualify():
    future = date(2026, 8, 30)
    receipt = _receipt(
        signal_date=future, observation_end=future, observation_start=date(2026, 8, 24)
    )
    assert not receipt_qualifies_for_display(receipt, requested_markets=("za",), today=_TODAY)


def test_a_requested_market_outside_the_run_scope_does_not_qualify():
    receipt = _receipt(market_scope=("za",))
    assert not receipt_qualifies_for_display(receipt, requested_markets=("za", "ng"), today=_TODAY)


def test_selection_takes_the_latest_closed_window_then_completion_then_run_id():
    older = _receipt(
        run_id="oi_a",
        signal_date=date(2026, 8, 27),
        observation_end=date(2026, 8, 27),
        observation_start=date(2026, 8, 21),
    )
    newer_a = _receipt(run_id="oi_b", completed_at=datetime(2026, 8, 29, 6, 0, 0, tzinfo=UTC))
    newer_b = _receipt(run_id="oi_c", completed_at=datetime(2026, 8, 29, 7, 0, 0, tzinfo=UTC))
    selected = select_display_run(
        (older, newer_a, newer_b), requested_markets=("za",), today=_TODAY
    )
    assert selected is not None
    assert selected.run_id == "oi_c"


def test_selection_breaks_an_exact_completion_tie_on_run_id():
    first = _receipt(run_id="oi_aaa")
    second = _receipt(run_id="oi_bbb")
    selected = select_display_run((second, first), requested_markets=("za",), today=_TODAY)
    assert selected is not None
    assert selected.run_id == "oi_bbb"


def test_selection_returns_none_when_nothing_qualifies_rather_than_a_blocked_run():
    blocked = _receipt(display_release_state="blocked")
    failed = _receipt(run_id="oi_z", status="failed")
    assert select_display_run((blocked, failed), requested_markets=("za",), today=_TODAY) is None


def test_selection_of_an_empty_set_is_none_and_never_an_error():
    assert select_display_run((), requested_markets=("za",), today=_TODAY) is None


def test_a_duplicate_run_id_carrying_unequal_content_is_rejected():
    first = _receipt()
    second = _receipt(candidate_count=99)
    with pytest.raises(RunReceiptError):
        select_display_run((first, second), requested_markets=("za",), today=_TODAY)
