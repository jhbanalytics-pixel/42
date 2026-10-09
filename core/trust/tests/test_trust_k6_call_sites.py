"""The brief runs K6 through three call sites in core/trust/claims.py: _k6 on a claim, field_fault on the answer fields
(short_answer, context, so_what, watch_next) and the gaps loop. Each must read the wide list (_k6_term), not the
a80be1d list seeds uses (_breach_term). Every text below is a term added after a80be1d, so a call site switched back to
_breach_term lets it through and a test here fails. The texts are fixed here, not read from the code."""

import pytest

from core.trust import claims
from core.trust.tests.test_trust_claims import answer, claim, filler, run, verdict

# Each is breached by the wide list and passed by the a80be1d list. (An elder, the elders, an old man, an old woman and
# the plural forms are on the narrow list too since the rule 1 ruling of wave 8 integration, so they are not here.)
NEW_ONLY = [
    "grandmas love it", "aged between 18 and 24", "the gogos queued", "toddlers copy it",
    "90s born creators", "the nineties babies", "old people dancing", "old folks joined", "the old heads agree",
    "old timers remember", "middle-aged viewers", "born in 1995", "digital natives", "retirees posted",
]


def test_every_text_is_new_to_the_wide_list():
    for text in NEW_ONLY:
        assert claims._breach_term(text, set()) is None, text
        assert claims._k6_term(text, set()), text


@pytest.mark.parametrize("text", NEW_ONLY)
def test_k6_on_a_claim_reads_the_wide_list(text):
    c = {"id": "c1", "text": text, "evidence_ids": [], "quotes": []}
    assert claims._k6(c, {}, set())[0] == "breach", text


@pytest.mark.parametrize("text", NEW_ONLY)
def test_check_answer_breaches_a_claim_on_a_new_term(text):
    _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
    assert verdict(checks, "c1", "K6") == "breach", text


@pytest.mark.parametrize("field", ["short_answer", "context"])
@pytest.mark.parametrize("text", NEW_ONLY)
def test_an_answer_field_with_a_new_term_is_blanked(field, text):
    ans = answer(filler(3))
    ans[field] = text
    new, checks = run(ans)
    assert new[field] == "", (field, text)
    assert any(r["rule"] == "K6" and r["verdict"] == "breach" and r["claim_id"] is None for r in checks)


@pytest.mark.parametrize("field", ["so_what", "watch_next"])
@pytest.mark.parametrize("text", NEW_ONLY)
def test_a_so_what_or_watch_next_item_with_a_new_term_is_dropped(field, text):
    item = {"text": text, "claim_ids": ["f0"]}
    if field == "watch_next":
        item["forecast"] = False
    ans = answer(filler(3), so_what=[{"text": "Join the step", "claim_ids": ["f0"]}])
    ans[field] = ans[field] + [item]
    new, checks = run(ans)
    assert text not in [s["text"] for s in new[field]], (field, text)
    assert any(r["rule"] == "K6" and r["verdict"] == "breach" for r in checks)


@pytest.mark.parametrize("key", ["what", "why"])
@pytest.mark.parametrize("text", NEW_ONLY)
def test_a_gap_with_a_new_term_is_dropped(key, text):
    ans = answer(filler(3))
    ans["gaps"] = [{"what": "No Reddit data", "why": "Not collected"}, {"what": "x", "why": "y", key: text}]
    new, checks = run(ans)
    assert [g["what"] for g in new["gaps"]] == ["No Reddit data"], (key, text)
    assert any(r["rule"] == "K6" and r["verdict"] == "breach" for r in checks)


def test_quote_words_are_counted_with_the_wide_list():
    # "grandmas love it" is 3 words only if the new term counts as a word to strip: with the wide list it leaves 2, so
    # the verified quote does not shield the term. Counted with the a80be1d list it would leave 3 and pass.
    quote = "grandmas love it"
    text = f'People said "{quote}" today.'
    assert claims._k6_term(text, {quote}) == "grandmas"
    assert claims._breach_term(text, {quote}) is None
