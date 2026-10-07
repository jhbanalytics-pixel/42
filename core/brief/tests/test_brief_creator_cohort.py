"""Creator wording must rest on the posts the claim actually cites."""

import copy

from core.brief import explain
from core.brief.tests.test_brief_explain import FakeModel, assert_numbers_only, good, make_pack, run


def test_multiple_creators_cannot_borrow_uncited_creators_from_the_pack():
    draft = good()
    draft["claims"][0].update(text='Multiple creators post the "shaya step" dance, 31 creators in three days.',
                              evidence_ids=["tt_1"])
    result = run(FakeModel([draft, copy.deepcopy(draft)]))
    assert_numbers_only(result, "failed_checks")
    assert any(row["rule"] == "K4" and row["checker"] == "code" and row["claim_id"] == "c1"
               and "cited posts show 1" in row["detail"] for row in result["checks"])


def test_many_creators_cannot_borrow_a_three_day_aggregate_count():
    draft = good()
    draft["claims"][0]["text"] = 'Many creators post the "shaya step" dance, 31 creators in three days.'
    result = run(FakeModel([draft, copy.deepcopy(draft)]))
    assert_numbers_only(result, "failed_checks")
    assert any(row["rule"] == "K4" and row["checker"] == "code" and row["claim_id"] == "c1"
               and "cited posts show 2" in row["detail"] for row in result["checks"])


def test_explanation_counts_only_the_creators_its_resting_claims_cite():
    draft = good(explanation="Multiple creators do the shaya step, likely because of the holiday weekend.")
    for claim in draft["claims"]:
        claim.update(evidence_ids=["tt_1"])
    result = run(FakeModel([draft, copy.deepcopy(draft)]))
    assert any(row["claim_id"] is None and row["rule"] == "K4" and row["checker"] == "code"
               and "cited posts show 1" in row["detail"] for row in result["checks"])
    assert_numbers_only(result, "failed_checks")


def test_two_cited_creators_support_multiple_without_borrowing_the_pack_count():
    draft = good()
    draft["claims"][0]["text"] = 'Multiple creators post the "shaya step" dance, 31 creators in three days.'
    assert run(FakeModel([draft]))["reason"] is None


def test_writer_separates_windows_and_support_keeps_its_existing_number_scope():
    pack = make_pack()
    user = explain._writer_user({}, pack, "ZA", "start", "end")
    assert "The evidence sample and the aggregate can cover different windows" in user
    prompt = explain._support_user({"text": "Multiple creators post it.", "label": "observed",
                                    "numbers": pack["numbers"][:1]}, pack["evidence"][:1], pack, "ZA")
    assert "Cited creator count:" not in prompt
    assert '"unit": "creators in 3 days"' in prompt
    assert explain.NUMBER_SCOPE in prompt
