from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta

import pytest


def analogue_module():
    name = "src.analysis.open_intelligence.historical_analogue"
    assert importlib.util.find_spec(name) is not None, "historical analogue module is missing"
    return importlib.import_module(name)


CUTOFF = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def receipt(
    module,
    receipt_id: str,
    day: int,
    *,
    signal_id: str | None = None,
    family="social",
    market="za",
    hour=0,
):
    return module.AnalogueReceipt(
        receipt_id=receipt_id,
        signal_id=signal_id or receipt_id.rsplit("_r", 1)[0],
        published_at=datetime(2026, 7, 1, hour, tzinfo=UTC) + timedelta(days=day),
        source_family=family,
        market=market,
    )


def snapshot(module, signal_id: str, as_of: datetime, **overrides):
    values = {
        "market": "za",
        "terms": ("pickup football", "street football", "local match"),
        "source_families": ("social", "search"),
        "trajectory_signature": ("rising", "rising", "flat"),
        "evidence_state": "ready",
        "receipts": (
            receipt(module, signal_id + "_r1", 0, family="social"),
            receipt(module, signal_id + "_r2", 0, family="search"),
        ),
    }
    values.update(overrides)
    return module.HistoricalSignalSnapshot(signal_id=signal_id, as_of=as_of, **values)


def rules(module, **overrides):
    values = {
        "term_weight": 0.4,
        "source_weight": 0.2,
        "trajectory_weight": 0.3,
        "geography_weight": 0.1,
        "minimum_similarity": 0.6,
        "allow_cross_market": False,
        "minimum_trajectory_points": 2,
        "eligible_evidence_states": ("ready",),
    }
    values.update(overrides)
    return module.HistoricalAnalogueRules(**values)


def test_analogue_returns_receipts_similarity_and_structured_difference():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    prior = snapshot(
        module,
        "sig_prior",
        datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        terms=("pickup football", "street football", "neighbourhood game"),
    )

    result = module.find_historical_analogues(current, (prior,), rules(module), limit=3)
    matches = result.matches

    assert len(matches) == 1
    assert result.rejections == ()
    match = matches[0]
    assert match.signal_id == "sig_prior"
    assert match.similarity == pytest.approx(0.8)
    assert match.term_similarity == pytest.approx(0.5)
    assert match.source_similarity == pytest.approx(1.0)
    assert match.trajectory_similarity == pytest.approx(1.0)
    assert match.geography_similarity == pytest.approx(1.0)
    assert match.difference_codes == ("terms_changed",)
    assert match.what_is_different_now == ("terms_changed",)
    assert match.receipt_ids == ("sig_prior_r1", "sig_prior_r2")
    assert match.transfer_limits == ()


def test_future_candidates_are_not_visible_and_future_receipts_fail_closed():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    future_candidate = snapshot(module, "sig_future", CUTOFF + timedelta(days=1))
    future_receipt = snapshot(
        module,
        "sig_bad_receipt",
        datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        receipts=(
            module.AnalogueReceipt(
                receipt_id="future_receipt",
                signal_id="sig_bad_receipt",
                published_at=datetime(2026, 7, 15, 11, 0, tzinfo=UTC),
                source_family="social",
                market="za",
            ),
            receipt(
                module,
                "sig_bad_receipt_r2",
                0,
                signal_id="sig_bad_receipt",
                family="search",
            ),
        ),
    )

    future_result = module.find_historical_analogues(
        current, (future_candidate,), rules(module), limit=3
    )
    assert future_result.matches == ()
    assert future_result.rejections[0].reason == "candidate_not_prior"
    receipt_result = module.find_historical_analogues(
        current, (future_receipt,), rules(module), limit=3
    )
    assert receipt_result.matches == ()
    assert receipt_result.rejections[0].reason == "future_receipt"


def test_self_match_and_empty_receipts_are_quarantined():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    self_snapshot = snapshot(module, "sig_current", CUTOFF - timedelta(days=10))
    no_receipts = snapshot(
        module,
        "sig_no_receipts",
        CUTOFF - timedelta(days=20),
        receipts=(),
    )

    result = module.find_historical_analogues(
        current, (self_snapshot, no_receipts), rules(module), limit=3
    )
    assert result.matches == ()
    assert [(item.signal_id, item.reason) for item in result.rejections] == [
        ("sig_current", "self_match"),
        ("sig_no_receipts", "missing_receipts"),
    ]


def test_cross_market_match_is_explicit_and_rule_gated():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    cross_market = snapshot(
        module,
        "sig_ng",
        CUTOFF - timedelta(days=30),
        market="ng",
        receipts=(
            receipt(module, "ng_r1", 0, signal_id="sig_ng", family="social", market="ng"),
            receipt(module, "ng_r2", 0, signal_id="sig_ng", family="search", market="ng"),
        ),
    )

    blocked = module.find_historical_analogues(current, (cross_market,), rules(module), limit=3)
    assert blocked.matches == ()
    assert blocked.rejections[0].reason == "market_not_allowed"
    allowed = module.find_historical_analogues(
        current,
        (cross_market,),
        rules(module, allow_cross_market=True, minimum_similarity=0.8),
        limit=3,
    ).matches
    assert len(allowed) == 1
    assert allowed[0].similarity == pytest.approx(0.9)
    assert allowed[0].difference_codes == ("market_changed",)
    assert allowed[0].transfer_limits == ("market_context_not_transferable",)


def test_source_and_trajectory_differences_are_visible_and_thresholded():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    candidate = snapshot(
        module,
        "sig_changed",
        CUTOFF - timedelta(days=30),
        source_families=("social", "news"),
        trajectory_signature=("rising", "falling", "falling"),
        receipts=(
            receipt(module, "sig_changed_r1", 0, family="social"),
            receipt(module, "sig_changed_r2", 0, family="news"),
        ),
    )

    matches = module.find_historical_analogues(
        current,
        (candidate,),
        rules(module, minimum_similarity=0.5),
        limit=3,
    ).matches
    assert len(matches) == 1
    assert matches[0].source_similarity == pytest.approx(1 / 3)
    assert matches[0].trajectory_similarity == pytest.approx(1 / 3)
    assert matches[0].difference_codes == ("source_mix_changed", "trajectory_changed")
    assert matches[0].transfer_limits == (
        "source_mix_not_transferable",
        "time_regime_not_transferable",
    )

    thresholded = module.find_historical_analogues(
        current,
        (candidate,),
        rules(module, minimum_similarity=0.7),
        limit=3,
    )
    assert thresholded.matches == ()
    assert thresholded.rejections[0].reason == "below_similarity_threshold"


def test_order_is_similarity_then_recency_then_signal_id_and_limit_is_bounded():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    older = snapshot(module, "sig_b", CUTOFF - timedelta(days=30))
    newer_z = snapshot(module, "sig_z", CUTOFF - timedelta(days=20))
    newer_a = snapshot(module, "sig_a", CUTOFF - timedelta(days=20))

    matches = module.find_historical_analogues(
        current,
        (newer_z, older, newer_a),
        rules(module),
        limit=2,
    ).matches

    assert [match.signal_id for match in matches] == ["sig_a", "sig_z"]
    with pytest.raises(ValueError, match="limit must be between one and twenty"):
        module.find_historical_analogues(current, (older,), rules(module), limit=21)


def test_duplicate_candidate_snapshot_identity_is_rejected():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    candidate = snapshot(module, "sig_prior", CUTOFF - timedelta(days=30))

    with pytest.raises(ValueError, match="duplicate historical candidate identity"):
        module.find_historical_analogues(
            current,
            (candidate, candidate),
            rules(module),
            limit=3,
        )


def test_receipts_must_bind_to_signal_and_cover_scored_source_families():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    wrong_signal = snapshot(
        module,
        "sig_wrong_binding",
        CUTOFF - timedelta(days=30),
        receipts=(
            receipt(module, "other_r1", 0, signal_id="other", family="social"),
            receipt(module, "other_r2", 0, signal_id="other", family="search"),
        ),
    )
    incomplete = snapshot(
        module,
        "sig_incomplete",
        CUTOFF - timedelta(days=40),
        receipts=(receipt(module, "sig_incomplete_r1", 0, family="social"),),
    )

    result = module.find_historical_analogues(
        current,
        (wrong_signal, incomplete),
        rules(module),
        limit=3,
    )

    assert result.matches == ()
    assert [(item.signal_id, item.reason) for item in result.rejections] == [
        ("sig_incomplete", "source_receipt_coverage_incomplete"),
        ("sig_wrong_binding", "receipt_signal_mismatch"),
    ]


def test_trajectory_windows_must_have_equal_length():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    shorter = snapshot(
        module,
        "sig_shorter",
        CUTOFF - timedelta(days=30),
        trajectory_signature=("rising", "flat"),
    )

    result = module.find_historical_analogues(current, (shorter,), rules(module), limit=3)

    assert result.matches == ()
    assert result.rejections[0].reason == "trajectory_window_mismatch"


def test_invalid_candidate_does_not_poison_valid_matches():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    valid = snapshot(module, "sig_valid", CUTOFF - timedelta(days=20))
    invalid = snapshot(
        module,
        "sig_invalid",
        CUTOFF - timedelta(days=10),
        receipts=(),
    )

    result = module.find_historical_analogues(
        current,
        (invalid, valid),
        rules(module),
        limit=3,
    )

    assert [item.signal_id for item in result.matches] == ["sig_valid"]
    assert [(item.signal_id, item.reason) for item in result.rejections] == [
        ("sig_invalid", "missing_receipts")
    ]


def test_candidate_evidence_state_eligibility_is_explicit():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    contradictory = snapshot(
        module,
        "sig_contradictory",
        CUTOFF - timedelta(days=20),
        evidence_state="contradictory",
    )

    blocked = module.find_historical_analogues(
        current,
        (contradictory,),
        rules(module),
        limit=3,
    )
    assert blocked.matches == ()
    assert blocked.rejections[0].reason == "evidence_state_ineligible"

    allowed = module.find_historical_analogues(
        current,
        (contradictory,),
        rules(module, eligible_evidence_states=("ready", "contradictory")),
        limit=3,
    )
    assert len(allowed.matches) == 1
    assert allowed.matches[0].difference_codes == ("evidence_state_changed",)


def test_cross_market_total_tie_uses_market_as_final_order():
    module = analogue_module()
    current = snapshot(module, "sig_current", CUTOFF)
    candidates = []
    for market in ("ng", "ke"):
        candidates.append(
            snapshot(
                module,
                "sig_tie",
                CUTOFF - timedelta(days=20),
                market=market,
                receipts=(
                    receipt(
                        module,
                        market + "_r1",
                        0,
                        signal_id="sig_tie",
                        family="social",
                        market=market,
                    ),
                    receipt(
                        module,
                        market + "_r2",
                        0,
                        signal_id="sig_tie",
                        family="search",
                        market=market,
                    ),
                ),
            )
        )

    result = module.find_historical_analogues(
        current,
        tuple(candidates),
        rules(module, allow_cross_market=True),
        limit=1,
    )

    assert [(item.signal_id, item.market) for item in result.matches] == [("sig_tie", "ke")]


@pytest.mark.parametrize(
    "weights",
    [
        {"term_weight": -0.1},
        {"term_weight": 0.5},
        {"trajectory_weight": float("nan")},
    ],
)
def test_similarity_weights_must_be_finite_nonnegative_and_sum_to_one(weights):
    module = analogue_module()
    with pytest.raises(ValueError, match="similarity weights must be finite and sum to one"):
        rules(module, **weights)
