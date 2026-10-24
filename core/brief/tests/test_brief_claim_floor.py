"""The code checks count a claim-count fault only below the floor an explanation publishes on (18 Oct 2026).

The writer is asked for 3 to 5 claims (explain.MIN_CLAIMS, MAX_CLAIMS), and an explanation publishes with 2 standing
(explain.CLAIM_FLOOR, core/brief/job.py TOO_FEW). _faults used to count 2 passing claims as a fault, so a draft of two
sound claims spent its one repair round on the count and had none left for a support cut that held the draft.
Nothing about what publishes changes: 2 claims pass as before, 1 is held as before.
"""

from core.brief import explain
from core.brief.tests.test_brief_explain import FakeModel, good, run


def two_claims():
    draft = good()
    draft["claims"] = [c for c in draft["claims"] if c["id"] in ("c1", "c3")]
    return draft


def test_two_claims_that_pass_the_code_checks_are_no_fault_and_spend_no_repair():
    model = FakeModel([two_claims()])
    result = run(model)
    assert len(model.writer_calls()) == 1  # no repair round
    assert result["numbers_only"] is False and [c["id"] for c in result["claims"]] == ["c1", "c3"]
    assert not any(r["detail"].startswith("before repair: ") for r in result["checks"])


def test_one_claim_is_still_a_fault_the_repair_round_answers_and_the_floor_still_holds():
    draft = good()
    draft["claims"] = [c for c in draft["claims"] if c["id"] == "c1"]
    draft["explanation_claim_ids"] = ["c1"]
    model = FakeModel([draft, draft])  # the repair returns the same one-claim draft
    result = run(model)
    assert len(model.writer_calls()) == 2
    assert "explanations hold 3 to 5 claims and need at least 2 that pass; 1 passed" in model.writer_calls()[1]["user"]
    assert result["numbers_only"] is True and result["reason"] == "too_few_claims"


def test_the_floor_is_the_one_the_job_words_and_the_fault_names_both_numbers():
    from core.brief import job
    assert explain.CLAIM_FLOOR == 2 and job.TOO_FEW == "Claim checks: fewer than 2 claims passed"
    count_faults = lambda n: [f for f in explain._faults([], {"claims": [{"id": str(i)} for i in range(n)]}, [], {})
                              if f.startswith("explanations hold")]  # noqa: E731
    assert count_faults(1) == ["explanations hold 3 to 5 claims and need at least 2 that pass; 1 passed"]
    assert count_faults(2) == [] and count_faults(5) == []
    assert count_faults(6) == ["explanations hold 3 to 5 claims and need at least 2 that pass; 6 passed"]
