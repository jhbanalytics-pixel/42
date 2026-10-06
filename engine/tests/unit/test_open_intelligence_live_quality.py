from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from src.analysis.open_intelligence import live_quality

from tests.unit import test_dynamic_signal_persistence as persistence_fixtures

RUN_ID = "run_20260903_dynamic_apply_v2_r16"


def _batch():
    candidate = persistence_fixtures._candidate()
    candidate["run_id"] = RUN_ID
    evidence = persistence_fixtures._evidence()
    evidence["run_id"] = RUN_ID
    membership = persistence_fixtures._membership()
    membership["run_id"] = RUN_ID
    prediction = persistence_fixtures._prediction()
    prediction["run_id"] = RUN_ID
    return persistence_fixtures._batch(
        candidates=(candidate,),
        evidence=(evidence,),
        membership=(membership,),
        predictions=(prediction,),
    )


def test_candidate_projection_has_the_exact_contract_preimage_and_digest():
    batch = _batch()
    payload = live_quality.candidate_projection_payload(RUN_ID, batch)
    assert tuple(payload) == (
        "projection_contract_version",
        "run_id",
        "candidates",
        "evidence",
        "membership",
    )
    assert payload["projection_contract_version"] == "dynamic_quality_projection_v2"
    assert all(
        isinstance(row, str)
        for family in payload.values()
        if isinstance(family, tuple)
        for row in family
    )
    assert live_quality.candidate_projection_digest(RUN_ID, batch) == live_quality.canonical_digest(
        payload
    )
    assert live_quality.candidate_projection_digest(RUN_ID, batch) == (
        "6c5f39e17ecb4a21bae11f430c1cf0222859dd8049398ff48b9e7ed0ce087b74"
    )
    reversed_batch = replace(
        batch,
        candidates=tuple(reversed(batch.candidates)),
        evidence=tuple(reversed(batch.evidence)),
        membership=tuple(reversed(batch.membership)),
        predictions=tuple(reversed(batch.predictions)),
    )
    assert live_quality.candidate_projection_digest(RUN_ID, reversed_batch) == (
        live_quality.candidate_projection_digest(RUN_ID, batch)
    )
    without_predictions = replace(batch, predictions=())
    assert live_quality.candidate_projection_digest(RUN_ID, without_predictions) == (
        live_quality.candidate_projection_digest(RUN_ID, batch)
    )


def test_original_projection_preimage_retains_legacy_serialization_bytes():
    from src.analysis.open_intelligence import persistence

    batch = _batch()
    payload = live_quality.candidate_projection_payload(RUN_ID, batch)
    original_candidate = {**batch.candidates[0], "cluster_build_version": "cluster_v1"}
    payload["candidates"] = (persistence.canonical_typed_json("candidates", original_candidate),)
    assert live_quality.canonical_digest(payload) == (
        "7ca08e7bb3fb99fb455575dddc0787aba46629ca463f99a2d679099dae2d28dd"
    )
    with pytest.raises(persistence.BatchInvalid, match="source_provenance_version_invalid"):
        replace(batch, candidates=(original_candidate,))


def test_projection_refuses_non_nfc_text_without_normalizing_it():
    from src.analysis.open_intelligence import persistence

    candidate = persistence_fixtures._candidate()
    candidate["run_id"] = RUN_ID
    candidate["label"] = "Cafe\u0301"
    candidate["label_member_identity"] = "za|keyword|Cafe\u0301"
    with pytest.raises(persistence.BatchInvalid, match="noncanonical_unicode"):
        persistence_fixtures._batch(candidates=(candidate,))


def test_bigquery_projection_digest_surface_uses_python_field_and_family_order():
    from src.analysis.open_intelligence import persistence

    row_sql = persistence.canonical_typed_json_sql("candidates", "candidate")
    assert row_sql.index('"label"') < row_sql.index('"label_member_identity"')
    assert "TO_JSON_STRING(candidate.label)" in row_sql
    digest_sql = persistence.candidate_projection_digest_sql(RUN_ID)
    assert "LOWER(TO_HEX(SHA256" in digest_sql
    for table in (
        "signal_candidates_v2",
        "signal_evidence_v2",
        "signal_membership_v2",
    ):
        assert table in digest_sql
    assert "signal_predictions_v2" not in digest_sql
    assert "dynamic_quality_projection_v2" in digest_sql
    packet_sql = live_quality.review_packet_digest_sql("quality_release.run_id")
    assert "dynamic_quality_review_v1" in packet_sql
    assert "signal_evidence_v2" in packet_sql
    receipt_sql = live_quality.run_receipt_digest_sql("run_receipt")
    assert "open_intelligence_run_receipt_v1" not in receipt_sql
    assert "LOWER(TO_HEX(SHA256" in receipt_sql


def test_run_receipt_digest_sql_renders_completed_at_with_six_fraction_digits():
    # Refused the r15 release on staging, 4 Sep 2026: the run receipt digest in
    # Python renders completed_at with a fixed six digit fraction (.540200Z),
    # while the SQL side trimmed trailing zeros (.5402Z), so the release
    # transaction's first assert failed on a receipt whose fraction ended in
    # zeros. Every earlier released run had a fraction without one.
    from datetime import UTC, datetime

    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    receipt_sql = live_quality.run_receipt_digest_sql("run_receipt")
    assert "FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S', run_receipt.completed_at, 'UTC')" in receipt_sql
    assert "%E*S" not in receipt_sql
    rendered = canonical_bytes(
        {"completed_at": datetime(2026, 9, 4, 12, 28, 45, 540200, tzinfo=UTC)}
    )
    assert rendered == b'{"completed_at":"2026-09-04T12:28:45.540200Z"}'


def test_predictions_bind_only_reviewed_signal_evidence_and_membership():
    batch = _batch()
    reviewed = replace(batch, predictions=())
    live_quality.validate_prediction_review_binding(reviewed, batch)

    foreign_prediction = dict(batch.predictions[0])
    foreign_prediction["signal_id"] = "sig_" + "9" * 64
    foreign = replace(batch, predictions=(foreign_prediction,))
    with pytest.raises(live_quality.QualityRefusal, match="reviewed signal"):
        live_quality.validate_prediction_review_binding(reviewed, foreign)

    no_evidence = replace(reviewed, evidence=())
    final_no_evidence = replace(batch, evidence=())
    with pytest.raises(live_quality.QualityRefusal, match="reviewed evidence"):
        live_quality.validate_prediction_review_binding(no_evidence, final_no_evidence)

    no_membership = replace(reviewed, membership=())
    final_no_membership = replace(batch, membership=())
    with pytest.raises(live_quality.QualityRefusal, match="reviewed membership"):
        live_quality.validate_prediction_review_binding(no_membership, final_no_membership)


def test_holm_two_sided_direction_and_non_directional_families_are_exact():
    results = live_quality.family_directions(
        {"news": (5, 5), "reddit": (0, 20), "youtube": (1, 20)},
        exposure_complete={"news": True, "reddit": True, "youtube": False},
    )
    assert results["news"].raw_p == pytest.approx(0.086365187043)
    assert results["news"].direction == "not_applicable"
    assert results["reddit"].direction == "declining"
    assert results["youtube"].direction is None
    assert results["youtube"].reason == "family_exposure_authority_unavailable"


@pytest.mark.parametrize(
    ("current", "baseline", "direction"),
    [(1000, 2000, "rising"), (2000, 10000, "declining"), (10000, 10000, "rising")],
)
def test_large_family_counts_keep_exact_binomial_authority_until_display(
    current, baseline, direction
):
    exact = live_quality._two_sided_p_fraction(current, baseline)
    assert isinstance(exact, Fraction)
    result = live_quality.family_directions(
        {"news": (current, baseline)}, exposure_complete={"news": True}
    )["news"]
    assert result.direction == direction
    assert result.raw_p == float(exact)
    assert result.corrected_p == float(exact)


def test_technical_quality_requires_exact_complete_current_component_evidence():
    from tests.unit import test_dynamic_signal_rows as rows_fixtures

    component = replace(
        rows_fixtures.component(), label="repair routine", build_version="hybrid_graph_v2"
    )
    receipts = rows_fixtures.receipts()
    memberships = (
        {
            "member_identity": "za|keyword|repair routine",
            "canonical_value": "repair routine",
            "candidate_type": "keyword",
        },
    )
    result = live_quality.evaluate_technical_quality(
        component=component,
        receipts=receipts,
        memberships=memberships,
        as_of=datetime(2026, 8, 27, 23, 59, tzinfo=UTC),
    )
    assert result.passed is True
    assert result.reasons == ()

    future = dict(receipts)
    future["fixture_row_001"] = replace(
        future["fixture_row_001"],
        published_at=datetime(2026, 8, 28, tzinfo=UTC),
    )
    bad = live_quality.evaluate_technical_quality(
        component=component,
        receipts=future,
        memberships=memberships,
        as_of=datetime(2026, 8, 27, 23, 59, tzinfo=UTC),
    )
    assert bad.passed is False
    assert "future_available_receipt" in bad.reasons


def test_technical_quality_refuses_missing_membership_duplicate_rows_and_wrong_build():
    from tests.unit import test_dynamic_signal_rows as rows_fixtures

    component = replace(rows_fixtures.component(), build_version="hybrid_graph_v1")
    result = live_quality.evaluate_technical_quality(
        component=component,
        receipts=rows_fixtures.receipts(),
        memberships=(),
        as_of=datetime(2026, 8, 27, tzinfo=UTC),
    )
    assert result.passed is False
    assert "cluster_build_version_invalid" in result.reasons
    assert "membership_projection_incomplete" in result.reasons


def test_promotion_requires_validated_review_and_technical_quality():
    from scripts.staging.scoring_qualification import approved_decision_strength_rules

    from tests.unit import test_dynamic_quality_review as review_fixtures
    from tests.unit import test_dynamic_signal_rows as rows_fixtures

    component = replace(
        rows_fixtures.component(), label="repair routine", build_version="hybrid_graph_v2"
    )
    candidate, _evidence = rows_fixtures.build(
        component=component,
        quality_evaluated=True,
    )
    memberships = (
        {
            "member_identity": component.label_member_identity,
            "canonical_value": component.label,
            "candidate_type": "keyword",
            "qualifies_evidence": True,
        },
    )
    technical = live_quality.evaluate_technical_quality(
        component=component,
        receipts=rows_fixtures.receipts(),
        memberships=memberships,
        as_of=datetime(2026, 8, 27, tzinfo=UTC),
    )
    packet = live_quality.build_review_packet(RUN_ID, review_fixtures._batch())
    receipt = review_fixtures._receipt(packet)
    review = live_quality.validate_review_receipt(
        receipt,
        packet=packet,
        source_window_digest="a" * 64,
        candidate_projection_digest=live_quality.candidate_projection_digest(
            RUN_ID, review_fixtures._batch()
        ),
    )
    result = live_quality.score_quality_candidate(
        candidate=candidate,
        readiness=rows_fixtures.readiness(),
        receipts=rows_fixtures.receipts(),
        memberships=memberships,
        technical_quality=technical,
        review_authority=review,
        rules=approved_decision_strength_rules(),
        directional_conflict=False,
    )
    assert result.promotion_eligible is True
    # Without a registered human review the candidate still scores, but the foreign-market
    # sample stands unreviewed and the decision rules keep it from promotion.
    unreviewed = live_quality.score_quality_candidate(
        candidate=candidate,
        readiness=rows_fixtures.readiness(),
        receipts=rows_fixtures.receipts(),
        memberships=memberships,
        technical_quality=technical,
        review_authority=None,
        rules=approved_decision_strength_rules(),
        directional_conflict=False,
    )
    assert unreviewed.promotion_eligible is False
    assert "foreign_market_sample_unreviewed" in unreviewed.reasons


def test_digest_sql_orders_arrays_by_columns_the_cte_projects():
    # Each family CTE projects row_json and the natural key columns by bare name; the
    # ARRAY_AGG that reads the CTE cannot see the table alias, so its ORDER BY must name
    # the projected columns unqualified or BigQuery refuses to plan the digest.
    import re

    from src.analysis.open_intelligence import persistence

    for sql in (
        persistence.candidate_projection_digest_sql(RUN_ID),
        persistence.row_set_digest_sql(RUN_ID),
    ):
        orders = re.findall(r"ARRAY_AGG\(row_json ORDER BY ([^)]*)\)", sql)
        assert orders, "no array orderings found"
        for order in orders:
            names = [item.strip() for item in order.split(",")]
            assert names[-1] == "row_json"
            assert all("." not in name for name in names), order


def _rerun_batch(batch, run_id, created_at):
    """The same rows persisted again under another run identity and clock."""
    from dataclasses import replace as _replace

    def _move(rows):
        return tuple({**row, "run_id": run_id, "created_at": created_at} for row in rows)

    return _replace(
        batch,
        candidates=_move(batch.candidates),
        evidence=_move(batch.evidence),
        membership=_move(batch.membership),
        predictions=tuple({**row, "run_id": run_id} for row in batch.predictions),
    )


def test_content_digests_ignore_run_identity_and_clock_but_not_rows():
    # Approved 3 Sep 2026 (content-bound quality review): a review attests rows,
    # so the content digests of the same rows under a later run identity and a
    # later created_at are identical, while the run-bound digests differ.
    batch = _batch()
    later = _rerun_batch(
        batch, "run_20260903_dynamic_apply_v2_r16", datetime(2026, 8, 25, 6, 0, tzinfo=UTC)
    )
    assert live_quality.candidate_projection_digest(
        RUN_ID, batch
    ) != live_quality.candidate_projection_digest("run_20260903_dynamic_apply_v2_r16", later)
    assert live_quality.candidate_projection_content_digest(
        batch
    ) == live_quality.candidate_projection_content_digest(later)
    assert live_quality.review_packet_content_digest(
        batch
    ) == live_quality.review_packet_content_digest(later)
    payload = live_quality.candidate_projection_content_payload(batch)
    assert tuple(payload) == (
        "projection_contract_version",
        "candidates",
        "evidence",
        "membership",
    )
    assert payload["projection_contract_version"] == "dynamic_quality_projection_content_v1"
    for family in ("candidates", "evidence", "membership"):
        for row_json in payload[family]:
            assert '"run_id"' not in row_json
            assert '"created_at"' not in row_json
    changed = replace(
        batch,
        evidence=tuple({**row, "excerpt": "changed excerpt"} for row in batch.evidence),
    )
    assert live_quality.candidate_projection_content_digest(
        changed
    ) != live_quality.candidate_projection_content_digest(batch)


def test_content_digest_sql_mirrors_the_python_preimage_shape():
    from src.analysis.open_intelligence import persistence

    sql = persistence.candidate_projection_content_digest_sql(RUN_ID)
    assert "dynamic_quality_projection_content_v1" in sql
    assert f"run_id = '{RUN_ID}'" in sql
    assert '"run_id":' not in sql.replace(f"run_id = '{RUN_ID}'", "")
    assert '"created_at":' not in sql
    packet_sql = live_quality.review_packet_content_digest_sql(RUN_ID, sql_expression=False)
    assert "dynamic_quality_review_content_v1" in packet_sql
    assert '"run_id":' not in packet_sql
    run_bound = persistence.candidate_projection_digest_sql(RUN_ID)
    assert '"run_id":' in run_bound
    assert '"created_at":' in run_bound


def test_content_review_authority_accepts_a_prior_run_review_for_the_same_rows():
    current = _batch()
    prior_run = "run_20260903_dynamic_apply_v2_r6"
    batch = _rerun_batch(current, prior_run, datetime(2026, 8, 25, 6, 0, tzinfo=UTC))
    packet = live_quality.build_review_packet(prior_run, batch)
    receipt = {
        "review_contract_version": live_quality.REVIEW_CONTRACT_VERSION,
        "run_id": prior_run,
        "source_window_digest": "6" * 64,
        "candidate_projection_digest": live_quality.candidate_projection_digest(prior_run, batch),
        "packet_digest": packet["packet_digest"],
        "reviewed_evidence_ids": tuple(item["evidence_id"] for item in packet["review_items"]),
        "foreign_market_evidence_ids": (),
        "factual_conflict_evidence_ids": (),
        "uncertain_evidence_ids": (),
        "reviewed_by": "Albert",
        "reviewed_at": datetime(2026, 9, 3, 17, 35, 15, 1, tzinfo=UTC),
        "decision": "approved",
    }
    receipt["receipt_digest"] = live_quality.review_receipt_digest(receipt)
    prior = live_quality.PriorRunReview(
        receipt=receipt,
        run_id=prior_run,
        candidate_projection_digest=receipt["candidate_projection_digest"],
        packet_digest=receipt["packet_digest"],
        packet_evidence_ids=receipt["reviewed_evidence_ids"],
        candidate_projection_content_digest=live_quality.candidate_projection_content_digest(batch),
        review_packet_content_digest=live_quality.review_packet_content_digest(batch),
    )
    authority = live_quality.content_review_authority(
        prior, source_window_digest="6" * 64, batch=current
    )
    assert authority.run_id == prior_run
    assert authority.receipt_digest == receipt["receipt_digest"]
    assert authority.reviewed_by == "Albert"
    # A different window, a changed row, or a prior review whose stored digests do
    # not match its own rows all read as no authority.
    assert (
        live_quality.content_review_authority(prior, source_window_digest="7" * 64, batch=current)
        is None
    )
    changed = replace(
        current, evidence=tuple({**row, "excerpt": "changed excerpt"} for row in current.evidence)
    )
    assert (
        live_quality.content_review_authority(prior, source_window_digest="6" * 64, batch=changed)
        is None
    )
    forged = replace(prior, candidate_projection_digest="9" * 64)
    assert (
        live_quality.content_review_authority(forged, source_window_digest="6" * 64, batch=current)
        is None
    )
