"""K6 and personal names. Babu, Bibi, Koko and the Gogo and Mkhulu forms are kin words (grandfather, grandmother,
grandparent) and also names (Babu Owino, a Kenyan MP; Bibi Titi Mohamed; Koko Rapapa). On 7 Oct the published KE card
"sassa, grant, rubber bullets" carried a claim naming Babu Owino and would have been held for an age term.

A kin word is read as a name only when it is written like one: first letter capital, singular, and either followed by
a capitalised word (a surname), set in a list with a capitalised name that is not another kin word, or the short form
of a name given in full elsewhere in the same text, or part of a known full name in any case (test_trust_k6_residue.py).
Anything else stays an age term, including lower case, all capitals,
a plural, and a capitalised word with nothing around it that marks it as a name. Fixed strings, not read from the code."""

import pytest

from core.trust import claims

# The claim text of the 7 Oct KE card, verbatim from the retained staging payload (markets[2].cards[1].claims[0]).
OCT7_KE_CLAIM = (
    "In Kenya, creator teacher.sheyii reported on TikTok that Edwin Sifuna declared Babu Owino as his candidate for "
    "Nairobi governor, while creator publicsquare8 shared remarks wishing that Babu becomes governor and Sifuna "
    "becomes president."
)

NAMES = [
    "Babu Owino said", "Bibi Titi Mohamed", "Koko Rapapa", "Hon. Babu Owino spoke", "Gogo Skhotheni new show",
    "Koko Chanel", "Ugogo Skhotheni was on air", "Raila, Babu and Sifuna joined the rally",
    "Babu Owino won and later Babu thanked supporters", OCT7_KE_CLAIM,
    "babu owino said", "BABU OWINO said", "BABU Owino said", "babu Owino said",
]

KIN_WORDS = [
    "babu na bibi wanalalamika", "the gogos queued for grants", "Gogos queued for grants",
    "Koko showed the step", "Gogo queued for her grant", "Babu and Bibi queued for grants",
    "Gogo and Mkhulu queued for grants", "Babu Owino spoke and the gogos queued", "Babu becomes governor",
    "Bibi says no to ceasefire", "Mkhulus queued", "the Makhulus queued",
    "Gogos Club members queued for grants", "Babu Bibi Owino spoke",
]


@pytest.mark.parametrize("text", NAMES)
def test_a_kin_word_written_as_a_name_is_not_an_age_term(text):
    assert claims._k6_term(text, set()) is None, text


@pytest.mark.parametrize("text", KIN_WORDS)
def test_a_kin_word_not_written_as_a_name_is_still_an_age_term(text):
    assert claims._k6_term(text, set()), text


def test_the_7_oct_ke_claim_passes_the_k6_check():
    claim = {"id": "c1", "text": OCT7_KE_CLAIM, "evidence_ids": [], "quotes": []}
    assert claims._k6(claim, {}, set())[0] == "pass"


def test_a_name_does_not_shield_another_age_term_in_the_same_text():
    assert claims._k6_term("Babu Owino spoke to the teens", set()) == "teens"
    assert claims._k6_term("Babu Owino spoke to the grandmothers", set()) == "grandmothers"


def test_the_seeds_function_is_untouched_by_names():
    assert claims._breach_term("the gogos queued", set()) is None
    assert claims._breach_term("Babu Owino said", set()) is None


def test_a_quote_of_only_a_name_still_counts_its_words():
    quote = "Babu Owino grills the officer"
    text = f'He said "{quote}" in court.'
    assert claims._k6_term(text, {quote}) is None
    assert claims._k6_term('She said "gogos queued" outside.', {"gogos queued"}) == "gogos"


# Second review. A word after a kin word is a surname only if it is not a platform or a common word, a possessive does
# not make a list, and a headline in title case is not read for names at all. "Bibi says" stays held (lead); the known
# full names, in any case, are in test_trust_k6_residue.py.
NOT_NAMES = [
    "Gogo TikTok is the new trend", "Babu Joins TikTok Dance Trend", "Gogo Culture Takes Over Mzansi",
    "Babu, Kenya's favourite grandpa, dances", "Koko Instagram Reels go viral", "Bibi Dance Challenge Wins Fans",
    "Gogo Club members queued", "Gogo Zodwa Dances Into Soweto Hearts",
]


@pytest.mark.parametrize("text", NOT_NAMES)
def test_a_platform_a_common_word_or_a_headline_is_not_a_name(text):
    assert claims._k6_term(text, set()), text


def test_a_real_surname_that_is_a_title_case_word_elsewhere_still_passes():
    assert claims._k6_term("Babu Owino joined Gogo Skhotheni on stage", set()) is None
    assert claims._k6_term("Edwin Sifuna backs Babu Owino for Nairobi", set()) is None
