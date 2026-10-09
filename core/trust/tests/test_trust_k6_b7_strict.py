"""B7 closure. A mid, early or late decade never passes where 3d5dd63 held it.

At 3d5dd63 the decade was an age only beside an audience or age word. The next commit made it an age unless a measure
word sat beside it, and dropped the audience words, so "Women in the mid 20s are paying the price for data bundles"
went from held to clear. The rule is now both: an audience or age word anywhere in the sentence makes it an age,
whatever measure word sits beside it, and with none of those a measure word within six words still clears it. A build
may be stricter than the typed rule, never looser. Fixed strings, not read from the code."""

import itertools
import re

import pytest

from core.trust import claims

# The 3d5dd63 context list, copied here so the pin does not read from the code it pins.
BASE_CONTEXT = re.compile(
    r"\b(?:aged?|ages|in\s+their|people|fans?|women|men|users?|audiences?|viewers?|creators?|followers?|students?"
    r"|youth|adults?|girls|boys|parents|listeners?)\b", re.I)

# The 25 constructed in the review, then the sentences the first B7 commit pinned as passing.
CONSTRUCTED_AGE_CLAIMS = [
    "Women in the mid 20s are paying the price for data bundles",
    "A mid 30s man runs the account",
    "The trend is led by mid-20s women",
    "Late 20s R&B fans drive the sound",
    "Mid 20s couples call it relationship goals",
    "Fans in the mid 20s love this hot new sound",
    "Mid 20s creators are worth watching",
    "A late 20s creator wins the challenge every week",
    "Shoppers in the mid 20s say the costs are too high",
    "Users in the late 20s are driving the trend that won the internet",
    "Kenyans in the mid 20s are hit hardest by fuel price rises",
    "Mid 20s graduates say rent costs too much",
    "The core demographic is mid 20s, mostly hot girl walk fans",
    "Viewers in the late 20s share the heat of the debate",
    "Its fanbase skews early 20s and buys at a low price",
    "Most posters are early 20s, sharing it in the cold",
    "Mid 20s women post a warm reaction",
    "Students in the early 20s dominate, scoring the viral clip",
    "The crowd skews late 20s and pays R50 entry",
    "Mid 20s Nigerians spend naira on it",
    "Mid-30s dads lead by example in the clips",
    "Mid 20s TikTokers are posting about rand weakness",
    "A woman, mid 30s, wins the dance-off",
    "Early 20s fans cheer as Pirates win",
    "Mid 20s users post it on rainy days",
    "Viewers watched temperatures climb into the mid 30s",
    "Creators filmed outdoors with highs in the mid 30s",
    "Adults pay entry fees in the mid 20s rand",
    "Followers saw the score reach the late 20s",
    "Users reported highs in the mid 30s in Nairobi",
    "Fans sweated through temperatures in the mid 30s at the match",
    "Listeners heard the rand trade in the mid 20s",
    "The audience watched the score settle in the early 20s",
]
HELD_AT_3D5DD63 = [t for t in CONSTRUCTED_AGE_CLAIMS if BASE_CONTEXT.search(t)]


def test_the_sentences_that_held_at_3d5dd63_are_the_ones_counted():
    assert len(CONSTRUCTED_AGE_CLAIMS) == 33
    assert len(HELD_AT_3D5DD63) == 21


@pytest.mark.parametrize("text", HELD_AT_3D5DD63)
def test_a_decade_that_held_at_3d5dd63_still_breaches(text):
    assert claims._k6_term(text, set()), text
    c = {"id": "c1", "text": text, "evidence_ids": [], "quotes": []}
    assert claims._k6(c, {}, set())[0] == "breach", text


AUDIENCE_WORDS = [
    "aged", "age", "ages", "in their", "in her", "in his", "people", "fan", "fans", "women", "men", "user", "users",
    "audience", "audiences", "viewer", "viewers", "creator", "creators", "follower", "followers", "student",
    "students", "youth", "adult", "adults", "girls", "boys", "parents", "listener", "listeners",
]
DECADES = ["mid 20s", "mid-30s", "late 20s", "early 20s", "early 70s", "mid '40s"]
MEASURE_WORDS = [
    "temperatures", "temps", "degrees", "celsius", "weather", "forecast", "highs", "lows", "heat", "hot", "cold",
    "warm", "humid", "rain", "rainfall", "score", "scored", "won", "wins", "goals", "points", "wickets", "overs",
    "runs", "bowled", "all out", "dismissed", "innings", "a lead", "the lead", "leads by", "led by", "rand", "naira",
    "dollars", "usd", "pounds", "euros", "shillings", "ksh", "price", "priced", "costs", "costing", "fees", "revenue",
    "salary", "salaries", "wages", "worth", "R50", "R", "$", "\u00b0",
]


@pytest.mark.parametrize(
    "audience,decade,measure", list(itertools.product(AUDIENCE_WORDS, DECADES[:3], MEASURE_WORDS[::7])))
def test_every_audience_word_holds_a_decade_whatever_measure_word_sits_beside_it(audience, decade, measure):
    text = f"The {audience} {measure} in the {decade}"
    assert claims._k6_term(text, set()), text


@pytest.mark.parametrize("measure", MEASURE_WORDS)
def test_an_audience_word_holds_a_decade_beside_each_measure_word(measure):
    assert claims._k6_term(f"Women in the mid 20s and {measure}", set()), measure
    assert claims._k6_term(f"{measure} for fans in the late 30s", set()), measure


def test_an_audience_word_in_another_sentence_does_not_hold_the_decade():
    assert not claims._k6_term("Women posted it. Temperatures stayed in the mid 20s", set())
    assert not claims._k6_term("Temperatures stayed in the mid 20s. Women posted it", set())


def test_an_audience_word_far_from_the_decade_in_the_same_sentence_still_holds_it():
    far = "Women posted a lot while temperatures one two three four five six seven eight nine ten stayed in the mid 20s"
    assert claims._k6_term(far, set())


def test_the_audience_word_is_a_whole_word():
    assert not claims._k6_term("Temperatures in the mid 20s at the manhunt", set())
    assert not claims._k6_term("Temperatures in the mid 20s at the fanfare", set())
    assert not claims._k6_term("Temperatures in the mid 20s on the agenda", set())


# Pins on the lists that clear a decade. Each of these widenings loosens K6 and was green before (B7c, B7k, B7l).
FILLER = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]


def test_the_measure_word_window_is_six_words():
    assert claims._AgeContext._WORDS_BESIDE == 6
    six_before = "temperatures " + " ".join(FILLER[:5]) + " mid 20s"
    seven_before = "temperatures " + " ".join(FILLER[:6]) + " mid 20s"
    assert not claims._k6_term(six_before, set()), six_before
    assert claims._k6_term(seven_before, set()), seven_before
    six_after = "mid 20s " + " ".join(FILLER[:5]) + " temperatures"
    seven_after = "mid 20s " + " ".join(FILLER[:6]) + " temperatures"
    assert not claims._k6_term(six_after, set()), six_after
    assert claims._k6_term(seven_after, set()), seven_after


NEAR_MISSES = [
    "run", "led", "lead", "point", "wicket", "over", "bowl", "rands", "heated", "hots", "scoreboard", "worthy", "feed",
    "highway", "lowly", "winner", "goalkeeper", "costly", "salaried", "wagering", "ranch",
]


@pytest.mark.parametrize("word", NEAR_MISSES)
def test_a_near_miss_of_a_measure_word_does_not_clear_a_decade(word):
    text = f"Entry {word} in the mid 20s"
    assert claims._k6_term(text, set()), text


def test_runs_clears_a_decade_and_run_does_not():
    assert not claims._k6_term("Entry runs in the mid 20s", set())
    assert claims._k6_term("Entry run in the mid 20s", set())


def test_led_by_clears_a_decade_and_led_alone_does_not():
    assert not claims._k6_term("Mpho led by one in the mid 20s", set())
    assert claims._k6_term("Mpho led in the mid 20s", set())
    assert claims._k6_term("Mpho led them in the mid 20s", set())


def test_a_lead_and_leads_by_clear_a_decade_and_lead_alone_does_not():
    assert not claims._k6_term("Mpho took a lead in the mid 20s", set())
    assert not claims._k6_term("Mpho leads by one in the mid 20s", set())
    assert claims._k6_term("Mpho will lead in the mid 20s", set())


@pytest.mark.parametrize("measure", MEASURE_WORDS)
def test_each_measure_word_alone_clears_a_decade(measure):
    text = f"Entry {measure} in the mid 20s"
    assert not claims._k6_term(text, set()), text


def test_the_whole_measure_list_is_pinned():
    assert claims._AgeContext._MEASURE.pattern == (
        r"\b(?:temperatures?|temps?|degrees?|celsius|fahrenheit|weather|forecasts?|highs?|lows?|heat|hot|cold|warm"
        r"|humid|rain(?:fall)?|scores?|scored|scoring|won|wins?|goals?|points|wickets|overs|runs"
        r"|bowled|all\s+out|dismissed|innings"
        r"|(?:a|the)\s+lead|leads?\s+by|led\s+by|rand|naira|dollars?|usd|pounds?|euros?|shillings?|ksh|price[sd]?"
        r"|costs?|costing|fees?|revenue|salary|salaries|wages?|worth)\b|\u00b0|[$\u00a3\u20ac]|(?-i:\bR\s?\d|\bR\b)")


def test_the_whole_audience_and_age_word_list_is_pinned():
    assert claims._AgeContext._AGE_MARKER.pattern == (
        r"\b(?:aged?|ages|in\s+(?:their|her|his)|people|fans?|women|men|users?|audiences?|viewers?|creators?"
        r"|followers?|students?|youth|adults?|girls|boys|parents|listeners?)\b")
