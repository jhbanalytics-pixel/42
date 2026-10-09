"""W8-DEC-03d candidate: narrow score and date exceptions to the K6 age-range pattern.

The exceptions sit behind claims.K6_03D_ENABLED, which ships False. With it False the K6 answers are the ones
test_trust_k6_boundary.py already pins. With it True every row of the plan's boundary and counterexample table
(PLAN-revision4-copy.md, "K6 boundary table" and "Counterexamples") gets the answer in TABLE below. Expected values are
written here, not read from the checker.
"""

import pytest

from core.trust import claims
from core.trust.claims import _breach_term, _k6, _k6_term

# (text, breach when K6_03D_ENABLED is True). One row per table row and per counterexample.
TABLE = [
    ("aged 18-24", True), ("18-24 year olds", True), ("ages 25 to 34", True), ("fans aged 18 to 24", True),
    ("the 18-24 group", True), ("aged between 18 and 24", True), ("18-24s", False),
    ("won 24-17", False), ("final score 21-14", False), ("scored 30-28 at the weekend", False),
    ("polls show 45-55 split", False), ("Sept 20-26", False), ("between 20-26 Sept", False),
    ("20-26 September", False), ("from 13-19 October", False), ("Pirates beat Chiefs 2-1", False),
    ("National Youth Service Corps", False), ("mostly women", True), ("google trends shows", True),
    ("won 18-24 voters", True), ("15-30s video", False), ("18-24s response time", False),
]

# What the same rows return with the constant False: the a80be1d answers plus N13-T, nothing else.
TABLE_OFF = dict(TABLE)
TABLE_OFF.update({
    "won 24-17": True, "final score 21-14": True, "scored 30-28 at the weekend": True, "polls show 45-55 split": True,
    "Sept 20-26": True, "National Youth Service Corps": True,
})

# Rows the exceptions must leave alone when True.
SCORE_WORDS = ["won", "lost", "beat", "drew", "scored", "final score", "full-time", "full time", "half-time", "FT", "HT"]
AUDIENCE_NOUNS = ["voters", "fans", "users", "women", "men", "people", "viewers", "listeners", "audience", "customers"]
MONTHS = ["Jan", "January", "Feb", "Mar", "April", "May", "June", "July", "Aug", "Sept", "September", "Oct", "Nov", "Dec"]

STILL_BREACH_WHEN_ON = [
    "Pirates beat Chiefs 24-17",
    "24-17 won",
    "the score was fans 24-17",
    "Sept were 20-26",
    "in September, 20-26",
    "Sept, 20-26",
    "may 20-26 fans",
    "voters may 20-26 online",
    "polls show 45-55 splits",
    "poll of 18-24 split on it",
    "an 18-24 split",
    "polls show 20-40 split",
    "national youth service corps",
    "Youth Service Corps",
    "the Youth Service",
    "National Youth Service Corps youth",
    "National Youth Service Corps and teens",
    "ft 24-17",
    "ht 24-17",
    "a 45-55 split in the room",
    "won aged 18-24",
    "won the 18-24 group",
    "scored 18-24 year olds",
]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(claims, "K6_03D_ENABLED", True)


def test_the_constant_ships_false():
    assert claims.K6_03D_ENABLED is False


@pytest.mark.parametrize(("text", "breach"), TABLE)
def test_the_whole_table_when_on(on, text, breach):
    assert bool(_k6_term(text, set())) is breach, text


@pytest.mark.parametrize(("text", "breach"), sorted(TABLE_OFF.items()))
def test_the_whole_table_when_off(text, breach):
    assert claims.K6_03D_ENABLED is False
    assert bool(_k6_term(text, set())) is breach, text


@pytest.mark.parametrize("word", SCORE_WORDS)
def test_every_score_word_clears_a_score_when_on(on, word):
    assert not _k6_term(f"{word} 24-17", set())
    assert not _k6_term(f"Chiefs {word} 24-17 on Saturday", set())


@pytest.mark.parametrize("word", SCORE_WORDS)
def test_every_score_word_breaches_when_off(word):
    assert _k6_term(f"{word} 24-17", set())


@pytest.mark.parametrize("noun", AUDIENCE_NOUNS)
def test_an_audience_noun_after_the_range_blocks_a_score(on, noun):
    assert _k6_term(f"won 18-24 {noun}", set())
    assert _k6_term(f"scored 18-24 {noun} online", set())


@pytest.mark.parametrize("noun", AUDIENCE_NOUNS)
def test_an_audience_noun_after_the_range_blocks_a_date(on, noun):
    assert _k6_term(f"Sept 20-26 {noun}", set())


@pytest.mark.parametrize("month", MONTHS)
def test_every_month_clears_a_date_when_on(on, month):
    assert not _k6_term(f"{month} 20-26", set())
    assert not _k6_term(f"{month}. 20-26", set())
    assert not _k6_term(f"Event runs {month} 20-26 in Joburg", set())


@pytest.mark.parametrize("month", MONTHS)
def test_every_month_breaches_when_off(month):
    assert _k6_term(f"{month} 20-26", set())


@pytest.mark.parametrize("text", STILL_BREACH_WHEN_ON)
def test_nothing_wider_than_the_candidate_clears(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", STILL_BREACH_WHEN_ON)
def test_the_same_texts_breach_when_off(text):
    assert _k6_term(text, set()), text


def test_a_score_beside_an_age_group_breaks_on_the_group(on):
    assert _k6_term("won 24-17 and the 18-24 group loved it", set())
    assert _k6_term("Sept 20-26 and the 18-24 group", set())
    assert _k6_term("won 24-17 among teens", set())


def test_the_score_exception_does_not_hide_a_second_range(on):
    assert _k6_term("won 24-17, then aged 18-24", set())
    assert _k6_term("won 24-17. The 25-34 group", set())


def test_the_age_word_patterns_still_fire_beside_a_clear_range(on):
    assert _k6_term("Sept 20-26 fans aged 18", set())
    assert _k6_term("youth won 24-17", set())


def test_seeds_list_is_not_touched_when_on(on):
    for text in ("won 24-17", "Sept 20-26", "National Youth Service Corps", "polls show 45-55 split"):
        assert _breach_term(text, set()), text


def test_only_two_terms_differ_and_none_is_removed():
    base, new = claims._K6_TERMS, claims._K6_TERMS_03D
    assert len(base) == len(new)
    assert [i for i, (a, b) in enumerate(zip(base, new)) if a is not b] == [
        next(i for i, t in enumerate(base) if t.pattern.startswith("\\byouths?")),
        next(i for i, t in enumerate(base) if t.pattern.startswith("(?<![\\d:/.\\-])\\b(?:1[3-9]")),
    ]
    for a, b in zip(base, new):
        assert a.pattern == b.pattern and a.flags == b.flags


def test_off_reads_the_original_list_object(monkeypatch):
    seen = []
    monkeypatch.setattr(claims, "_first_term", lambda text, exempt, terms: seen.append(terms))
    claims._k6_term("anything", set())
    assert seen == [claims._K6_TERMS]
    monkeypatch.setattr(claims, "K6_03D_ENABLED", True)
    claims._k6_term("anything", set())
    assert seen[-1] is claims._K6_TERMS_03D


def test_a_claim_reads_the_exceptions_when_on(on):
    c = {"id": "c1", "text": "Pirates won 24-17 on Saturday", "evidence_ids": [], "quotes": []}
    assert _k6(c, {}, set())[0] == "pass"
    c["text"] = "Pirates won 18-24 voters"
    assert _k6(c, {}, set())[0] == "breach"


def test_a_claim_breaches_the_same_text_when_off():
    c = {"id": "c1", "text": "Pirates won 24-17 on Saturday", "evidence_ids": [], "quotes": []}
    assert _k6(c, {}, set())[0] == "breach"


def test_a_quoted_verified_span_is_still_exempt_when_on(on):
    assert not _k6_term('He said "the 18-24 group was loud today" at the show', {"the 18-24 group was loud today"})
