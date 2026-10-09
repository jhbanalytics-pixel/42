"""RULES.md rule 1 (no age lens) is enforced by two lists: the brief's K6 terms (core/trust/claims.py) and the Ask
writer checks' AGE_PATTERNS (core/agent/checks.py). They were written apart and had drifted. This pins a corpus of
age and generation words that both must flag, and a corpus of look-alikes that neither may.

The corpus is fixed here. It is not read from either list, so a word dropped from one list fails the test instead of
shrinking what the test expects."""

import pytest

from core.agent.checks import _text_breaches
from core.trust.claims import _k6_term

AGE_WORDS = [
    "the babies are dancing", "grandma and grandpa joined", "gogo in the kitchen", "koko showed the step",
    "toddlers copy it", "infants too", "schoolchildren filmed it", "school children filmed it",
    "matriculants are posting", "first-time voters shared it", "school leavers on holiday",
    "born in 1995", "nineties babies remember it", "grew up in the nineties", "middle-aged viewers",
    "digital natives", "born frees", "retirees", "minors", "tweens", "preteens", "juveniles", "youthful energy",
    "older people", "older women", "early thirties", "mid twenties", "late forties", "next generation",
    "a new generation", "GenZProtests", "#genzrevolution", "ama2000s", "ikhehla", "amakhehla", "kidz",
    "zillennials", "iGen", "grannies", "grandmother", "grandfather", "grandparents", "mkhulu", "makhulu", "bibi",
    "babu", "watoto", "abantwana", "vijana", "wazee", "pikin", "pikins"
]
# Old-age words neither list flags yet. The brief list takes them here; the Ask list is another lane's file.
BRIEF_FIRST = ["the elders agreed", "the old man", "an old woman", "old men", "old women", "the village elder spoke", "an elder"]
LOOK_ALIKES = [
    "fur babies", "plant babies", "sugar babies", "kidney", "kidnap", "a young brand", "a generation of content",
    "a generation ago", "matric results", "school holidays", "10-15s clips", "over 20 plates", "under 5 million",
    "Women's Day", "learner's licence", "Youth Day",
]


@pytest.mark.parametrize("text", AGE_WORDS)
def test_both_lists_flag_the_age_word(text):
    assert _text_breaches(text), f"Ask checks miss {text!r}"
    assert _k6_term(text, set()), f"brief K6 misses {text!r}"


@pytest.mark.parametrize("text", BRIEF_FIRST)
def test_the_brief_flags_the_old_age_words(text):
    assert _k6_term(text, set()), f"brief K6 misses {text!r}"


@pytest.mark.parametrize("text", LOOK_ALIKES)
def test_neither_list_flags_the_look_alike(text):
    assert not _text_breaches(text), f"Ask checks flag {text!r}"
    assert not _k6_term(text, set()), f"brief K6 flags {text!r}"
