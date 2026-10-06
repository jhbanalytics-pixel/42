import pytest

from ops.evaluation import score_questions
from ops.evaluation.score_questions import (
    classify_case,
    classify_validated_case,
    supported_completion,
)


def claim(verdict):
    return {"verdict": verdict, "verbatim": False}


def case(case_id, *, answerable, state, claims=()):
    return {
        "case_id": case_id,
        "answerable": answerable,
        "state": state,
        "material_claims": list(claims),
    }


def test_verbatim_quote_cannot_override_claim_assessment():
    assert (
        classify_case(
            answerable=True,
            state="complete",
            material_claims=[{"verdict": "contradicted", "verbatim": True}],
        )
        == "fail_claim_support"
    )


def test_unverified_claim_fails_before_transport_state_is_considered():
    result = classify_case(
        answerable=True, state="failed", material_claims=[claim("unverified")]
    )
    assert result == "fail_claim_support"


@pytest.mark.parametrize(
    "state", ["planned", "submitted", "pending", "refused", "failed", "unknown"]
)
def test_supported_request_without_an_answer_fails(state):
    assert (
        classify_case(answerable=True, state=state, material_claims=[])
        == "fail_supported_request"
    )


@pytest.mark.parametrize("state", ["refused", "partial"])
def test_unanswerable_boundary_passes(state):
    assert (
        classify_case(answerable=False, state=state, material_claims=[])
        == "pass_honest_boundary"
    )


@pytest.mark.parametrize("answerable", [True, False])
def test_complete_answer_without_material_claims_is_an_empty_success(answerable):
    assert (
        classify_case(answerable=answerable, state="complete", material_claims=[])
        == "fail_empty_success"
    )


@pytest.mark.parametrize("state", ["complete", "partial"])
def test_supported_answer_goes_to_usefulness_review(state):
    assert (
        classify_case(
            answerable=True, state=state, material_claims=[claim("supported")]
        )
        == "requires_usefulness_review"
    )


@pytest.mark.parametrize("state", ["done", "", None, "COMPLETE", 3])
def test_wrapper_rejects_unknown_transport_state(state):
    with pytest.raises(ValueError, match="state_invalid"):
        classify_validated_case(answerable=True, state=state, material_claims=[])


def test_wrapper_requires_a_verdict_on_every_claim():
    with pytest.raises(ValueError, match="claim_verdict_required"):
        classify_validated_case(
            answerable=True,
            state="complete",
            material_claims=[claim("supported"), {"verbatim": True}],
        )


@pytest.mark.parametrize("verdict", ["supportd", "", None, ["supported"]])
def test_wrapper_rejects_unknown_verdicts(verdict):
    with pytest.raises(ValueError, match="claim_verdict_invalid"):
        classify_validated_case(
            answerable=True, state="complete", material_claims=[claim(verdict)]
        )


@pytest.mark.parametrize("answerable", [1, "yes", None])
def test_wrapper_requires_boolean_answerability(answerable):
    with pytest.raises(ValueError, match="answerable_bool_required"):
        classify_validated_case(
            answerable=answerable, state="complete", material_claims=[]
        )


def test_wrapper_requires_a_claim_list():
    with pytest.raises(ValueError, match="material_claims_list_required"):
        classify_validated_case(
            answerable=True, state="complete", material_claims=claim("supported")
        )


def test_wrapper_matches_the_kernel_on_valid_input():
    for state in sorted(score_questions.TRANSPORT_STATES):
        for answerable in (True, False):
            for claims in ([], [claim("supported")], [claim("contradicted")]):
                assert classify_validated_case(
                    answerable=answerable, state=state, material_claims=claims
                ) == classify_case(
                    answerable=answerable, state=state, material_claims=claims
                )


def test_conditional_quality_can_pass_while_whole_service_completion_fails():
    result = supported_completion(
        [
            case("q1", answerable=True, state="complete", claims=[claim("supported")]),
            case("q2", answerable=True, state="pending"),
            case("q3", answerable=True, state="failed"),
        ]
    )
    assert result["issued"] == 3
    assert result["supported_denominator"] == 3
    assert result["supported_completed"] == 1
    assert result["supported_failed"] == 2
    assert result["supported_completion_rate"] == pytest.approx(1 / 3)
    assert result["whole_service_pass"] is False
    quality = result["conditional_answer_quality"]
    assert quality["label"] == score_questions.CONDITIONAL_LABEL
    assert quality["returned"] == 1
    assert quality["supported"] == 1
    assert quality["rate"] == 1.0
    assert quality["pass"] is True
    assert result["classifications"] == {
        "q1": "requires_usefulness_review",
        "q2": "fail_supported_request",
        "q3": "fail_supported_request",
    }


def test_empty_denominator_is_not_a_pass():
    empty = supported_completion([])
    assert empty["supported_denominator"] == 0
    assert empty["supported_completion_rate"] is None
    assert empty["whole_service_pass"] is False
    assert empty["conditional_answer_quality"]["rate"] is None
    assert empty["conditional_answer_quality"]["pass"] is False

    only_unsupported = supported_completion(
        [case("u1", answerable=False, state="refused")]
    )
    assert only_unsupported["supported_denominator"] == 0
    assert only_unsupported["whole_service_pass"] is False
    assert only_unsupported["unsupported_issued"] == 1
    assert only_unsupported["honest_boundaries"] == 1


@pytest.mark.parametrize("state", ["planned", "submitted", "unknown", "refused"])
def test_held_timed_out_and_refused_supported_cases_fail_completion(state):
    result = supported_completion(
        [
            case("q1", answerable=True, state="complete", claims=[claim("supported")]),
            case("q2", answerable=True, state=state),
        ]
    )
    assert result["supported_failed"] == 1
    assert result["whole_service_pass"] is False


def test_contradicted_claim_fails_both_views():
    result = supported_completion(
        [
            case("q1", answerable=True, state="complete", claims=[claim("supported")]),
            case(
                "q2",
                answerable=True,
                state="complete",
                claims=[claim("supported"), claim("contradicted")],
            ),
        ]
    )
    assert result["supported_completed"] == 1
    assert result["whole_service_pass"] is False
    assert result["conditional_answer_quality"]["returned"] == 2
    assert result["conditional_answer_quality"]["supported"] == 1
    assert result["conditional_answer_quality"]["pass"] is False


def test_held_transport_outcome_cannot_count_as_a_useful_completed_answer():
    result = supported_completion(
        [
            case(
                "q-held",
                answerable=True,
                state="failed",
                claims=[],
            )
        ]
    )
    assert result["classifications"] == {"q-held": "fail_supported_request"}
    assert result["supported_completed"] == 0
    assert result["whole_service_pass"] is False


def test_every_supported_case_completed_passes_and_unsupported_cases_stay_out():
    result = supported_completion(
        [
            case("q1", answerable=True, state="complete", claims=[claim("supported")]),
            case("q2", answerable=True, state="partial", claims=[claim("supported")]),
            case("u1", answerable=False, state="refused"),
            case("u2", answerable=False, state="complete"),
        ]
    )
    assert result["supported_denominator"] == 2
    assert result["supported_completed"] == 2
    assert result["supported_completion_rate"] == 1.0
    assert result["whole_service_pass"] is True
    assert result["unsupported_issued"] == 2
    assert result["honest_boundaries"] == 1
    assert result["classifications"]["u2"] == "fail_empty_success"


def test_duplicate_case_ids_cannot_inflate_the_denominator():
    with pytest.raises(ValueError, match="duplicate_case_id"):
        supported_completion(
            [
                case("q1", answerable=True, state="pending"),
                case("q1", answerable=True, state="pending"),
            ]
        )


def test_case_records_are_validated_before_counting():
    with pytest.raises(ValueError, match="case_id_required"):
        supported_completion([{"answerable": True, "state": "complete"}])
    with pytest.raises(ValueError, match="state_invalid"):
        supported_completion([case("q1", answerable=True, state="held")])
    with pytest.raises(ValueError, match="answerable_bool_required"):
        supported_completion([case("q1", answerable="yes", state="complete")])


# Blind claim assessment


def label(**overrides):
    fields = {
        "case_id": "discovery-01",
        "answerable": True,
        "corpus_sha256": "a" * 64,
        "scope": {"market": "za", "client_scope_id": "ogilvy_default"},
        "window": {"start": "2026-08-01", "end": "2026-09-01"},
        "assessor": "corpus-assessor-1",
        "labelled_at": "2026-09-10T09:00:00Z",
    }
    fields.update(overrides)
    return score_questions.answerability_label(**fields)


def assessed_claim(verdict="supported", **overrides):
    fields = {
        "claim_id": "c1",
        "kind": "observation",
        "text": "Thrifting videos rose in the window.",
        "cited_passage": "passage-7",
        "verbatim": False,
        "checks": {"identity": "pass", "quote_span": "pass"},
        "verdict": verdict,
    }
    fields.update(overrides)
    return fields


def assess(label_record=None, **overrides):
    fields = {
        "state": "complete",
        "material_claims": [assessed_claim()],
        "output_recorded_at": "2026-09-12T10:00:00Z",
        "reviewer": "reviewer-a",
    }
    fields.update(overrides)
    return score_questions.blind_claim_assessment(label_record or label(), **fields)


def test_label_is_digested_before_any_model_output_is_read():
    record = label()
    assert record["contract_version"] == "answerability_label_v1"
    assert len(record["label_digest"]) == 64
    assert record == label()
    assert record["label_digest"] != label(answerable=False)["label_digest"]


def test_assessment_carries_the_frozen_label_and_the_kernel_classification():
    record = assess()
    assert record["contract_version"] == "blind_claim_assessment_v1"
    assert record["case_id"] == "discovery-01"
    assert record["answerable"] is True
    assert record["label_digest"] == label()["label_digest"]
    assert record["classification"] == "requires_usefulness_review"
    assert record["reviewer"] == "reviewer-a"
    assert record["material_claims"][0]["verdict"] == "supported"


def test_supported_case_that_failed_is_never_recategorized():
    record = assess(state="failed", material_claims=[])
    assert record["answerable"] is True
    assert record["classification"] == "fail_supported_request"
    with pytest.raises(TypeError):
        assess(answerable=False)


def test_tampered_label_is_refused():
    record = label()
    record["answerable"] = False
    with pytest.raises(ValueError, match="label_digest_mismatch"):
        assess(record)


def test_label_recorded_after_model_output_is_refused():
    with pytest.raises(ValueError, match="label_after_output"):
        assess(output_recorded_at="2026-09-10T08:59:59Z")
    with pytest.raises(ValueError, match="label_after_output"):
        assess(output_recorded_at="2026-09-10T09:00:00Z")


def test_assessment_compares_fractional_utc_instants_not_stamp_strings():
    frozen_label = label(labelled_at="2026-09-10T09:00:00.5Z")
    with pytest.raises(ValueError, match="label_after_output"):
        assess(frozen_label, output_recorded_at="2026-09-10T09:00:00Z")


def test_assessment_preserves_arbitrary_fractional_second_ordering():
    frozen_label = label(labelled_at="2026-09-10T09:00:00.0000001Z")
    assert (
        assess(frozen_label, output_recorded_at="2026-09-10T09:00:00.0000002Z")[
            "classification"
        ]
        == "requires_usefulness_review"
    )
    with pytest.raises(ValueError, match="label_after_output"):
        assess(frozen_label, output_recorded_at="2026-09-10T09:00:00.0000000Z")
    with pytest.raises(ValueError, match="label_after_output"):
        assess(frozen_label, output_recorded_at="2026-09-10T09:00:00.000000100Z")
    assert (
        assess(
            label(labelled_at="2026-09-10T09:00:00.999999999Z"),
            output_recorded_at="2026-09-10T09:00:01.000000000Z",
        )["classification"]
        == "requires_usefulness_review"
    )


def test_calendar_invalid_label_and_output_stamps_are_refused():
    with pytest.raises(ValueError, match="label_field_invalid"):
        label(labelled_at="2026-02-30T09:00:00Z")
    with pytest.raises(ValueError, match="output_recorded_at_invalid"):
        assess(output_recorded_at="2026-02-30T09:00:00Z")


def test_label_fields_are_validated():
    with pytest.raises(ValueError, match="answerable_bool_required"):
        label(answerable="yes")
    with pytest.raises(ValueError, match="corpus_sha256_invalid"):
        label(corpus_sha256="abc")
    with pytest.raises(ValueError, match="label_field_invalid"):
        label(assessor="")
    with pytest.raises(ValueError, match="label_field_invalid"):
        label(labelled_at="yesterday")


def test_failed_deterministic_check_must_read_as_contradicted():
    claim = assessed_claim(checks={"numeric": "fail"})
    with pytest.raises(ValueError, match="verdict_inconsistent"):
        assess(material_claims=[claim])
    record = assess(material_claims=[claim | {"verdict": "contradicted"}])
    assert record["classification"] == "fail_claim_support"


def test_claim_without_a_cited_passage_is_unverified():
    with pytest.raises(ValueError, match="verdict_inconsistent"):
        assess(material_claims=[assessed_claim(cited_passage=None)])
    record = assess(
        material_claims=[assessed_claim("unverified", cited_passage=None, checks={})]
    )
    assert record["classification"] == "fail_claim_support"


def test_proposal_needs_a_basis_and_a_falsifier():
    with pytest.raises(ValueError, match="proposal_basis_required"):
        assess(material_claims=[assessed_claim(kind="proposal")])
    record = assess(
        material_claims=[
            assessed_claim(kind="proposal", basis="passage-7", falsifier="no rise")
        ]
    )
    assert record["material_claims"][0]["kind"] == "proposal"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("kind", "fact", "claim_kind_invalid"),
        ("checks", {"vibes": "pass"}, "claim_check_invalid"),
        ("checks", {"identity": "ok"}, "claim_check_invalid"),
        ("claim_id", "", "claim_field_invalid"),
        ("verbatim", "yes", "claim_field_invalid"),
    ],
)
def test_claim_fields_are_validated(field, value, code):
    with pytest.raises(ValueError, match=code):
        assess(material_claims=[assessed_claim(**{field: value})])


def test_duplicate_claim_ids_and_empty_reviewer_are_refused():
    with pytest.raises(ValueError, match="duplicate_claim_id"):
        assess(material_claims=[assessed_claim(), assessed_claim()])
    with pytest.raises(ValueError, match="reviewer_required"):
        assess(reviewer="")
