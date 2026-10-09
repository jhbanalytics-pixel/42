"""core/detect/seeds.py (lane 5) filters seed queries with core.trust.claims._breach_term. That function must keep
returning what it returned at a80be1d, so widening the K6 checks of the brief cannot silently drop seed queries.

The pin is fixed here: a count and a sha256 of the a80be1d pattern sources, joined by newlines. It is not read from
the module under test, so a pattern added to or dropped from the seeds list fails the test."""

import hashlib
import re

import pytest

from core.trust import claims

A80_COUNT = 39
A80_SHA256 = "5062d0f38818e91157729b29b688907e61fafb13e833f11be4ba91aa2e183bc8"
# Wave 8 integration added two patterns at the end of the a80be1d list, so the brief's list agrees with Ask's on old
# man, old woman and elders (a tightening: seeds drop those words now). The first 39 are pinned unchanged above.
LIST_COUNT = 41
LIST_SHA256 = "4ee1904d961d256d8747c5568052bcf0c03d286f7f80529d2b240b08bae04078"

# Terms the brief's K6 took on after a80be1d. Seeds must not see them.
K6_ONLY = ["grandma", "toddlers", "middle-aged", "born in 1995", "next generation",
           "babies", "aged between 18 and 24", "koko", "gogos", "mkhulu"]
# Terms seeds already dropped at a80be1d. It still drops them.
A80_TERMS = ["gen z", "teens", "kids", "pensioners", "aged 18", "google trends", "search volume", "mostly women"]
# Terms the two patterns added by the integration ruling drop.
OLD_AGE_TERMS = ["the elders agreed", "an old man", "an old woman", "old ladies"]


def test_the_list_behind_breach_term_is_the_a80_list_and_the_two_old_age_patterns():
    patterns = claims._BREACH_TERMS
    assert len(patterns) == LIST_COUNT == A80_COUNT + 2
    assert {p.flags for p in patterns} == {re.I | re.UNICODE}
    assert hashlib.sha256("\n".join(p.pattern for p in patterns).encode("utf-8")).hexdigest() == LIST_SHA256
    first = patterns[:A80_COUNT]
    assert hashlib.sha256("\n".join(p.pattern for p in first).encode("utf-8")).hexdigest() == A80_SHA256


@pytest.mark.parametrize("text", K6_ONLY)
def test_breach_term_does_not_take_the_k6_only_terms(text):
    assert claims._breach_term(text, frozenset()) is None, text


@pytest.mark.parametrize("text", A80_TERMS + OLD_AGE_TERMS)
def test_breach_term_still_takes_the_a80_terms(text):
    assert claims._breach_term(text, frozenset()), text


@pytest.mark.parametrize("text", K6_ONLY)
def test_the_k6_check_takes_the_k6_only_terms(text):
    assert claims._k6_term(text, frozenset()), text


@pytest.mark.parametrize("text", A80_TERMS + OLD_AGE_TERMS)
def test_the_k6_check_takes_every_a80_term_too(text):
    assert claims._k6_term(text, frozenset()), text


def test_the_k6_list_is_the_a80_list_followed_by_the_new_terms():
    assert claims._K6_TERMS[: len(claims._BREACH_TERMS)] == claims._BREACH_TERMS
    assert len(claims._K6_TERMS) > len(claims._BREACH_TERMS)


def test_seeds_still_calls_that_same_function_and_keeps_its_a80_answers():
    from core.detect import seeds

    assert seeds._breach_term is claims._breach_term
    for text in K6_ONLY:
        assert seeds.allowed({"template": None, "kind": None, "query": text}) is True, text
    for text in A80_TERMS + OLD_AGE_TERMS:
        assert seeds.allowed({"template": None, "kind": None, "query": text}) is False, text
