"""A held card names the check that held it. Before 16 October 2026 a K9 forecast cut on a claim the sentence does
not rest on was shown as "did not rest on claims that passed", writer overflow was shown as a failed support check
although none ran, and a source text cut mid-word let a quote of the half word pass the word-for-word check."""
from core.brief import explain
from core.brief.job import NO_REST, _reason_code, failed_reason
from core.trust.claims import _norm, _quote_fault


def test_a_forecast_cut_on_a_side_claim_is_named():
    rows = [{"claim_id": "c1", "rule": "K1", "verdict": "pass", "checker": "code", "detail": "ok"},
            {"claim_id": "c4", "rule": "K9", "verdict": "cut", "checker": "code",
             "detail": "direct future assertion held while forecast promotion is off"}]
    reason = failed_reason({"reason": "failed_checks", "checks": rows, "rests_on": ["c1", "c2"]})
    assert reason != NO_REST
    assert reason.startswith("Claim checks: ")  # K9 has no rule name; stored rows keep "Claim checks"


def test_writer_overflow_is_not_a_failed_support_check():
    row = {"claim_id": None, "rule": "K4", "verdict": "cut", "checker": "code",
           "detail": "writer returned 6 claims; maximum is 5, so support checks were withheld"}
    reason = failed_reason({"reason": "failed_checks", "checks": [row], "rests_on": []})
    assert "not supported" not in reason and "too many claims" in reason
    assert _reason_code(row) == "support_withheld_overflow"


def test_a_real_sentence_support_cut_keeps_its_words():
    row = {"claim_id": None, "rule": "K4", "verdict": "cut", "checker": "model", "detail": "unsupported"}
    assert failed_reason({"reason": "failed_checks", "checks": [row], "rests_on": []}) == (
        "Support check: the explanation sentence was not supported by its posts")


def test_long_source_text_is_cut_on_a_word_boundary():
    text = "x " * ((explain.SOURCE_CONTEXT_MAX_CHARS - len("this is amaz")) // 2) + "this is amazingly good"
    shown = explain._source_text({"text": text})
    assert len(shown) <= explain.SOURCE_CONTEXT_MAX_CHARS
    assert not shown.endswith("amaz") and _quote_fault(_norm("this is amaz"), shown) is not None


def test_short_source_text_is_unchanged():
    assert explain._source_text({"text": "hello world"}) == "hello world"
