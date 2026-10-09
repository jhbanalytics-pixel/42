"""K6 residue after the second review of the brief lane (A11).

Three groups. Known full names of public people are read as names whatever the case, because a kin word inside the
full name is not an age claim (lower case "babu owino" is how an item label is stored). Grandpa and grandad are age
words like grandma and grandmother, in neither list before. And the strings that stay held are pinned, so the name rule
cannot widen by accident: a headline that starts with a bare kin word is still an age term, and "aged 18" still
breaches. Fixed strings, not read from the code."""

import pytest

from core.trust import claims
from core.trust.tests.test_trust_claims import answer, claim, filler, run, verdict

KNOWN_FULL_NAMES = [
    "babu owino", "BABU OWINO said", "BABU Owino said", "babu Owino said", "Babu Owino", "babu owino spoke at the rally",
    "bibi titi mohamed", "Bibi Titi Mohamed", "BIBI TITI MOHAMED",
    "Babu Owino Joins TikTok Dance Trend Today",
]


@pytest.mark.parametrize("text", KNOWN_FULL_NAMES)
def test_a_known_full_name_passes_whatever_its_case(text):
    assert claims._k6_term(text, set()) is None, text


@pytest.mark.parametrize("text", ["babu owino said", "BABU OWINO"])
def test_a_known_full_name_passes_the_claim_check(text):
    c = {"id": "c1", "text": text, "evidence_ids": [], "quotes": []}
    assert claims._k6(c, {}, set())[0] == "pass", text


STILL_HELD = [
    "babu", "babu owino and later babu thanked supporters", "babu owinos queued", "Babu Joins TikTok",
    "babu joins tiktok", "Bibi says no to ceasefire", "bibi titi", "Babu Owino spoke to the teens",
    "babu owino spoke to the grandpas",
]


@pytest.mark.parametrize("text", STILL_HELD)
def test_a_kin_word_outside_a_known_full_name_is_still_held(text):
    assert claims._k6_term(text, set()), text


def test_a_known_full_name_does_not_shield_another_age_term():
    assert claims._k6_term("babu owino spoke to the teens", set()) == "teens"
    assert claims._k6_term("BABU OWINO spoke to the grandmothers", set()) == "grandmothers"


def test_aged_18_still_breaches():
    for text in ("aged 18", "babu owino aged 18", "Babu Owino, aged 18, spoke"):
        assert claims._k6_term(text, set()), text


def test_seed_filtering_is_untouched_by_the_known_names():
    assert claims._breach_term("babu owino", set()) is None
    assert claims._breach_term("the grandpas queued", set()) is None


GRANDPA_FORMS = [
    "grandpa", "Grandpa dances", "the grandpas queued", "grandpa's recipe went viral", "Grandad on TikTok",
    "the grandads agree", "granddad and his dog", "my granddads danced",
]


@pytest.mark.parametrize("text", GRANDPA_FORMS)
def test_grandpa_and_grandad_are_age_terms(text):
    assert claims._k6_term(text, set()), text
    assert claims._breach_term(text, set()) is None, text


@pytest.mark.parametrize("text", GRANDPA_FORMS)
def test_a_claim_with_a_grandpa_form_breaches_k6_on_every_path(text):
    c = {"id": "c1", "text": text, "evidence_ids": [], "quotes": []}
    assert claims._k6(c, {}, set())[0] == "breach", text
    _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
    assert verdict(checks, "c1", "K6") == "breach", text


def test_the_grandpa_forms_do_not_catch_words_that_only_start_alike():
    for text in ("a grand pavilion opened", "the grandstand filled up", "Grand Prix weekend", "the grandeur of Soweto"):
        assert not claims._k6_term(text, set()), text


# Pinned patterns. Each text matches one pattern alone, so deleting that pattern from the list fails here.
PATTERN_PINS = [
    "born in 1995", "born after 2000", "grew up in the nineties", "the 90s generation", "90s born creators",
    "the nineties babies", "noughties-born", "#generation_alpha", "genz2025", "kiddies dancing",
    "zillennials posted", "old people dancing", "old folks joined", "the old heads agree", "old timers remember",
    "older audiences",
]
# On the narrow list too since the rule 1 ruling of wave 8 integration (the brief's list agrees with Ask's on them).
IN_BOTH_LISTS = ["an old man spoke", "the old ladies sang", "an old woman sat", "old men talked", "the elders agreed",
                 "an elder spoke"]


@pytest.mark.parametrize("text", PATTERN_PINS)
def test_each_late_k6_pattern_is_pinned_through_the_claim_check(text):
    assert claims._breach_term(text, set()) is None, text
    c = {"id": "c1", "text": text, "evidence_ids": [], "quotes": []}
    assert claims._k6(c, {}, set())[0] == "breach", text


@pytest.mark.parametrize("text", IN_BOTH_LISTS)
def test_the_old_age_person_words_are_on_the_narrow_list_and_breach_k6(text):
    assert claims._breach_term(text, set()), text
    c = {"id": "c1", "text": text, "evidence_ids": [], "quotes": []}
    assert claims._k6(c, {}, set())[0] == "breach", text


# B1. A mid, early or late decade is an age beside any word that names a group of people, not only beside aged, people,
# fans, women, men or users. Weather and score sentences with none of those words still pass.
DECADE_WITH_AUDIENCE = [
    "The audience skews mid 20s", "Viewers in the mid 20s share it most", "Creators in the late 20s drive it",
    "Followers in the early 30s repost it", "Students in the early 20s copy it", "Youth in the mid 20s lead it",
    "Adults in the early 40s joined", "Girls in the mid 20s posted it", "Boys in the late 20s posted it",
    "Parents in the early 30s shared it", "Listeners in the mid 20s tuned in", "Audiences in the early 20s agree",
    "The viewers are mostly mid 20s", "Followers, mid 20s, repost it",
]


@pytest.mark.parametrize("text", DECADE_WITH_AUDIENCE)
def test_a_decade_beside_an_audience_word_breaches(text):
    assert claims._k6_term(text, set()), text
    c = {"id": "c1", "text": text, "evidence_ids": [], "quotes": []}
    assert claims._k6(c, {}, set())[0] == "breach", text


DECADE_WITHOUT_AGE_CONTEXT = [
    "Temperatures sit in the mid 20s", "Highs in the late 20s all week", "He scored in the mid 20s",
    "Prices rose to the early 30s", "A viewer count of 25. Temperatures in the mid 20s",
]


@pytest.mark.parametrize("text", DECADE_WITHOUT_AGE_CONTEXT)
def test_a_decade_without_age_context_still_passes(text):
    assert not claims._k6_term(text, set()), text


# B4. A real name in a headline in title case passes through the known name list; the headline rule itself holds at a
# share of 0.8 capitalised words: four of five is a headline, three of five is not.
def test_a_known_name_inside_a_title_case_headline_passes():
    assert claims._k6_term("Edwin Sifuna Backs Babu Owino For Nairobi Seat", set()) is None


def test_the_headline_share_is_pinned_at_four_in_five():
    four_of_five = "Babu Visits The Market today"  # 4 of 5 words capitalised: a headline, so Babu is not read as a name
    assert claims._KinName._headline(four_of_five) is True
    three_of_five = "Babu visits The Market today"
    assert claims._KinName._headline(three_of_five) is False
    assert claims._KinName._headline("Babu Visits The Market") is False  # fewer than five words


def test_the_headline_share_holds_between_seven_and_eight_in_ten():
    assert claims._KinName._headline("Babu Visits The Old Market With Friends and some people") is False  # 7 of 10
    assert claims._KinName._headline("Babu Visits The Old Market With Friends And some people") is True  # 8 of 10


# B7. A mid, early or late decade is an age unless a weather, score or money word sits beside it. The test is the word
# that makes it a measure, not a list of the words that make it a person, so a new way to name a group cannot slip past.
DECADE_AGE_PHRASINGS = [
    "A woman in her late 20s started the trend", "A man in his mid 30s runs the account",
    "The core demographic is mid 20s", "Its fanbase skews early 20s", "The crowd skews late 20s",
    "Shoppers in the mid 20s are driving it", "Most TikTokers posting it are in the mid 20s",
    "Kenyans in the mid 20s love it", "Girl in her early 20s goes viral", "a mid-20s crowd",
    "Gamers in the early 20s share clips", "Consumers in the late 20s buy it", "Moms in the early 30s share it",
    "Subscribers in the mid 20s comment most", "Young adults in the mid 20s", "Customers in their mid 20s",
    "Mostly mid 20s and female", "Professionals in the late 20s", "Nigerians in their late 20s",
    "Lagos residents in the early 30s share it", "The fan base is mid 20s", "The age group is mid 20s",
    "Mostly men in the late 20s", "the mid 20s", "Twenty-somethings and the mid-30s set",
]


@pytest.mark.parametrize("text", DECADE_AGE_PHRASINGS)
def test_a_decade_with_no_measure_word_beside_it_breaches(text):
    assert claims._k6_term(text, set()), text
    c = {"id": "c1", "text": text, "evidence_ids": [], "quotes": []}
    assert claims._k6(c, {}, set())[0] == "breach", text


DECADE_MEASURES = [
    "Viewers watched temperatures climb into the mid 30s", "Creators filmed outdoors with highs in the mid 30s",
    "Adults pay entry fees in the mid 20s rand", "Followers saw the score reach the late 20s",
    "Users reported highs in the mid 30s in Nairobi", "Fans sweated through temperatures in the mid 30s at the match",
    "Listeners heard the rand trade in the mid 20s", "The audience watched the score settle in the early 20s",
    "Temperatures in the mid 20s all week", "Highs in the early 30s across Lagos", "Scores in the mid 20s",
    "The price stayed in the mid 20s", "The Proteas were bowled out in the late 20s for a low score",
    "Kenyans paid costs in the mid 20s KSh", "Naira traded in the mid 30s", "Tickets cost in the early 30s dollars",
    "Nairobi stayed in the mid 20s degrees", "Lows in the early 20s overnight",
    "The Proteas were bowled out in the late 20s", "The side was all out in the early 30s", "Dismissed in the mid 20s",
    "Entries of R250 in the mid 20s", "Seats at $40 in the early 30s", "It reached 25° in the mid 20s",
    "The Chiefs won the set in the early 30s", "He took a lead in the mid 20s", "Chiefs lead by a margin in the late 20s",
    "Pay R in the mid 20s",
]


@pytest.mark.parametrize("text", DECADE_MEASURES)
def test_a_decade_beside_a_weather_score_or_money_word_passes(text):
    assert not claims._k6_term(text, set()), text


def test_a_measure_word_far_from_the_decade_does_not_shield_it():
    far = "Highs were mild all week and then a long list of other words follow before women in the mid 20s drive it"
    assert claims._k6_term(far, set()), far


def test_a_measure_word_in_another_sentence_does_not_shield_it():
    assert claims._k6_term("Temperatures were mild. Women in the mid 20s drive it", set())


def test_an_age_marker_beats_a_measure_word():
    assert claims._k6_term("Fans aged mid 20s paid fees", set())
    assert claims._k6_term("Women in their mid 20s paid the price", set())
    assert claims._k6_term("A woman in her late 20s who scored highly", set())


def test_aged_18_still_breaches():
    assert claims._k6_term("aged 18", set())
    assert claims._k6_term("Posts by people aged 18", set())


def test_a_measure_word_after_the_decade_passes_it_and_a_measure_word_before_passes_it():
    assert not claims._k6_term("Viewers saw it stay in the mid 20s degrees", set())
    assert not claims._k6_term("Viewers saw degrees stay in the mid 20s", set())


def test_a_measure_word_in_the_next_sentence_does_not_shield_it():
    assert claims._k6_term("Women in the mid 20s drive it. The price rose.", set())


def test_the_letter_r_alone_is_a_money_word_only_in_capitals():
    assert claims._k6_term("Women in the mid 20s read r in the late 20s", set())


def test_ages_beats_a_measure_word_like_aged_does():
    assert claims._k6_term("People ages mid 20s paid the price", set())


# One measure word per sentence, so each word in the list is held by a test of its own.
DECADE_ONE_MEASURE_WORD = [
    "Entry ran in the mid 20s KSh", "Kenyans pay fees in the mid 20s", "Kenyans pay costs in the mid 20s",
    "Stalls sold it in the mid 20s dollars", "Kenyans pay a fee in the early 30s", "Kenyans pay cost in the early 30s",
]


@pytest.mark.parametrize("text", DECADE_ONE_MEASURE_WORD)
def test_each_money_word_alone_passes_a_decade(text):
    assert not claims._k6_term(text, set()), text
