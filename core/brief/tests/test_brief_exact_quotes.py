"""A quote K1 accepts is shown as the post's own characters (core/brief/explain.py _exact_quotes).

K1 (core/trust/claims.py) matches a quote to its post after NFKC, curly or straight quote marks and whitespace runs
are normalised; the local specificity rule (core/brief/specificity.py) and Today (core/api/today.py) need the exact
characters. A quote K1 verified across a caption line break or with a straightened apostrophe used to fail
specificity, and the card was held with no check row naming why. Now the quote, its claim and the sentence carry
the post's own stretch of text, and a sentence that still fails specificity writes a check row that names it.
Neither K1's acceptance nor specificity's exact match changes.
"""

import copy

from core.brief import job
from core.brief.explain import _exact_span
from core.brief.tests.test_brief_explain import FakeModel, good, make_pack, run
from core.trust.claims import check_answer

SPECIFICITY_HELD = ("Specificity check: the explanation did not rest on 2 local posts and a quote copied exactly from "
                    "one of them")


def quoting(source_quote, *, in_sentence=False):
    """good() with c1 quoting tt_1 as source_quote, and the sentence quoting it too when in_sentence."""
    draft = good()
    draft["claims"][0]["text"] = f'Creators post "{source_quote}" clips, 31 creators in three days.'
    draft["claims"][0]["quotes"] = [{"evidence_id": "tt_1", "text": source_quote}]
    if in_sentence:
        draft["explanation"] = (f'@thandi_moves writes "{source_quote}", @jozi_dance ties it to a Heritage Day braai '
                                "and @kasi_kicks to a long-weekend braai, likely because the holiday weekend brings "
                                "those gatherings to mind.")
    return draft


def k1_verdict(draft, pack):
    """K1's own verdict on c1, read straight from check_answer."""
    answer = {"status": "complete", "short_answer": draft["explanation"], "claims": copy.deepcopy(draft["claims"]),
              "evidence": pack["evidence"], "so_what": [], "watch_next": [], "gaps": [], "context": ""}
    _, rows = check_answer(answer, window_start="2026-09-25T00:00:00+02:00", window_end="2026-09-28T23:59:59+02:00",
                           market="ZA")
    return next(r["verdict"] for r in rows if r["claim_id"] == "c1" and r["rule"] == "K1")


def test_the_exact_span_is_found_across_line_breaks_quote_marks_and_nfkc_forms():
    assert _exact_span("shaya step to", "Doing the shaya\nstep to the track") == "shaya\nstep to"
    assert _exact_span("We're doing the shaya", "We’re doing the shaya step") == "We’re doing the shaya"
    assert _exact_span("shaya step", "Doing the ｓｈａｙａ  step") == (
        "ｓｈａｙａ  step")


def test_no_span_is_taken_when_k1_would_not_match_or_the_match_is_ambiguous():
    assert _exact_span("shaya steps", "Doing the shaya step") is None
    assert _exact_span("haya step", "Doing the shaya step") is None
    # Two different stretches read as the same quote: neither is chosen.
    assert _exact_span("shaya step", "shaya\nstep and then shaya  step") is None
    # The same stretch twice is one stretch.
    assert _exact_span("shaya  step", "shaya step, shaya step") == "shaya step"


def test_a_quote_k1_matched_across_a_caption_line_break_publishes_in_the_posts_own_characters():
    pack = make_pack("Doing the shaya\nstep to the new amapiano track, who else")
    draft = quoting("shaya step to")
    assert k1_verdict(draft, pack) == "pass"
    model = FakeModel([draft, copy.deepcopy(draft)])

    result = run(model, pack)

    assert result["numbers_only"] is False and result["reason"] is None
    assert result["specificity"]["status"] == "pass"
    assert result["specificity"]["quote"] == {"evidence_id": "tt_1", "text": "shaya\nstep to"}
    c1 = next(c for c in result["claims"] if c["id"] == "c1")
    assert c1["quotes"] == [{"evidence_id": "tt_1", "text": "shaya\nstep to"}]
    assert c1["text"] == 'Creators post "shaya\nstep to" clips, 31 creators in three days.'
    # The exact quote passed specificity first time, so the writer was not asked to repair it.
    assert len(model.writer_calls()) == 1


def test_a_straightened_apostrophe_is_shown_as_the_creators_own_in_quote_claim_and_sentence():
    pack = make_pack("We’re doing the shaya step to the new amapiano track, who else")
    draft = quoting("We're doing the shaya step", in_sentence=True)
    assert k1_verdict(draft, pack) == "pass"
    model = FakeModel([draft, copy.deepcopy(draft)])

    result = run(model, pack)

    exact = "We’re doing the shaya step"
    assert result["numbers_only"] is False and result["reason"] is None
    assert result["specificity"]["quote"] == {"evidence_id": "tt_1", "text": exact}
    c1 = next(c for c in result["claims"] if c["id"] == "c1")
    assert c1["quotes"][0]["text"] == exact and f'"{exact}"' in c1["text"]
    assert f'"{exact}"' in result["explanation"] and "We're" not in result["explanation"]
    assert len(model.writer_calls()) == 1


def test_an_ambiguous_quote_is_left_as_written_and_held_with_a_specificity_reason():
    pack = make_pack("Doing the shaya\nstep, then the shaya  step again to the new amapiano track")
    draft = quoting("shaya step")
    assert k1_verdict(draft, pack) == "pass"
    model = FakeModel([draft, copy.deepcopy(draft)])

    result = run(model, pack)

    assert result["numbers_only"] is True and result["reason"] == "failed_checks"
    assert result["specificity"]["reason"] == "quote_not_verbatim"
    rows = [r for r in result["checks"] if r["rule"] == "specificity"]
    assert rows == [{"claim_id": None, "rule": "specificity", "verdict": "cut", "checker": "code",
                     "detail": "short_answer: local specificity: quote_not_verbatim",
                     "span_sha256": "9b75a183d13b21a4655a5688dc605eebad5d1898b4ac3286d0ca4cdb7536c485"}]
    assert job.failed_reason({**result, "rests_on": ["c1", "c3"]}) == SPECIFICITY_HELD
    assert job.check_reason(rows[0]) == SPECIFICITY_HELD
    # Specificity is never asked to accept a quote that is not the post's exact characters.
    assert model.support_calls() == [] and model.critic_calls() == []


def test_a_missing_local_quote_is_held_with_a_specificity_reason_not_a_generic_one():
    draft = good()
    draft["claims"][0]["text"] = "Creators post the shaya step dance, 31 creators in three days."
    draft["claims"][0]["quotes"] = []
    model = FakeModel([draft, copy.deepcopy(draft)])

    result = run(model)

    assert result["specificity"]["reason"] == "missing_local_quote"
    assert job.failed_reason({**result, "rests_on": ["c1", "c3"]}) == SPECIFICITY_HELD
