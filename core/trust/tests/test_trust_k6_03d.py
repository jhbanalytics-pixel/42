"""W8-DEC-03d candidate: narrow score and date exceptions to the K6 age-range pattern, and the N-Ns group noun band.

The exceptions sit behind claims.K6_03D_ENABLED, which ships True after W8-DEC-03d YES. The off fixture pins the K6
answers with it False. With it True every row of the plan's boundary and counterexample table
(PLAN-revision4-copy.md, "K6 boundary table" and "Counterexamples") gets the answer in TABLE below. Expected values are
written here, not read from the checker.

Two rows differ from the plan's "current result" column on purpose. "polls show 45-55 split" and "National Youth Service
Corps" stay breaches: the poll split and the proper noun exceptions are not part of the candidate and were dropped.
"""

import pytest

from core.trust import claims
from core.trust.claims import _breach_term, _k6, _k6_term

EN = "\u2013"

# (text, breach when K6_03D_ENABLED is True). One row per table row and per counterexample.
TABLE = [
    ("aged 18-24", True), ("18-24 year olds", True), ("ages 25 to 34", True), ("fans aged 18 to 24", True),
    ("the 18-24 group", True), ("aged between 18 and 24", True), ("18-24s", True),
    ("won 24-17", False), ("final score 21-14", False), ("scored 30-28 at the weekend", False),
    ("polls show 45-55 split", True), ("Sept 20-26", False), ("between 20-26 Sept", False),
    ("20-26 September", False), ("from 13-19 October", False), ("Pirates beat Chiefs 2-1", False),
    ("National Youth Service Corps", True), ("mostly women", True), ("google trends shows", True),
    ("won 18-24 voters", True), ("15-30s video", False), ("18-24s response time", False),
]

# The same rows with the constant False: the a80be1d answers plus N13-T, nothing else.
TABLE_OFF = dict(TABLE)
TABLE_OFF.update({
    "won 24-17": True, "final score 21-14": True, "scored 30-28 at the weekend": True, "Sept 20-26": True,
    "18-24s": False,
})

SCORE_WORDS = ["won", "lost", "beat", "drew", "scored", "final score", "full-time", "full time", "half-time", "FT", "HT"]
AUDIENCE_NOUNS = ["voters", "fans", "users", "women", "men", "people", "viewers", "listeners", "audience", "customers",
                  "voter", "crowds", "consumers", "followers", "shoppers", "adults", "Kenyans",
                  "South Africans", "Nigerians", "Voters", "FANS"]
MONTHS = ["Jan", "January", "Feb", "February", "Mar", "April", "May", "June", "July", "Aug", "Sep", "Sept", "September",
          "Oct", "Nov", "Dec"]

# Clear when on: a falling pair after a score word, a rising day range after a month, a score with no person after it.
CLEAR_WHEN_ON = [
    "Chiefs won 24-17 on Saturday", "Won 24-17", "FT: 24-17", "HT 20-15", "Final Score: 31-24", "Half-Time 21-14",
    f"won 24{EN}17", "Pirates won 24-17, fans cheered", "lost 30-30 in the end", "Event runs May 20-26 in Joburg",
    "Sept. 20-26", f"Sept 20{EN}26", "Sept 20-31",
]

# Held when on, each a rising or otherwise unproved case the review constructed.
STILL_BREACH_WHEN_ON = [
    "won 18-24", "The EFF won 18-24 by a wide margin", "Harris won 18-29 by 24 points",
    "Netflix lost 18-24 share to TikTok", "the campaign drew 18-24 crowds", "won 18-24 South Africans",
    "won 18-24 female voters", "won 18-24 TikTok users", "won 18-24 among women", "fans aged won 18-24",
    "won: 18-24", f"won 18{EN}24", "Bulls lost 18-24 to the Sharks", "scored 18-24 year olds", "won aged 18-24",
    "won the 18-24 group", "Pirates beat Chiefs 24-17", "24-17 won", "the score was fans 24-17", "Andrew 24-17", "Smarch 20-26",
    "won 24-17 against a women's side", "Pirates won 24-17 and fans cheered", "won 24-17 in front of 40,000 fans",
    "won 24-17 and the 18-24 group loved it", "won 24-17 among teens", "won 24-17, then aged 18-24",
    "ft 24-17", "ht 24-17", "Left 24-17",
    "May 18-24 plus", "May 18-24+", f"May 18{EN}24 plus", "In June 35-44 shoppers led", "In June 35-44",
    "Since June 18-24 TikTok users have flocked to the sound", "In May 18-24 Kenyans drove the trend",
    "Sept 24-20", "Sept 20-32", "Sept 20-20", "Sept were 20-26", "in September, 20-26", "Sept, 20-26", "may 18-24 fans",
    "voters may 18-24 online", "Smarch 18-24 viewers", "Sept 20-26 fans", "Sept 20-26 Voters",
    "polls show 45-55 split", "In the poll, 45-54 split evenly between the ANC and the DA", "Poll: 35-64 split down the middle",
    "National Youth Service Corps", "national youth service corps", "the Youth Service", "National Youth Service",
]

# The N-Ns group noun band (plan line 197): a breach only as a group noun, never a duration or a decade.
BAND_BREACH_WHEN_ON = [
    "the 18-24s are watching", "among 18-24s", "popular with the 25-34s.", "among 18-24s and 25-34s", "18-24s",
    "18-24s are watching", "the 18-24s", "amongst the 18-24s, 25-34s lead", f"the 18{EN}24s are watching",
    "reach the 25-34s and the 35-44s", "amongst 18-24s", "the 20-34s are watching", "the 18-30s are watching",
]
BAND_PASS_WHEN_ON = [
    "15-30s video", "18-24s response time", "the 18-24s response time", "clips of 15-30s duration", "the 1980s are back",
    "temperatures in the 20-30s", "marks in the 70-80s are rare", "Music from the 70-80s is trending again",
    "Throwback hits from the 70-80s.", "Highs in Joburg sit in the 20-30s.", "Most marks were in the 60-70s.",
    "videos run 15-30s.", "the 20-30s are warm", "the 70-80s", "Videos run for 18-24s", "the 24-18s are watching",
]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(claims, "K6_03D_ENABLED", True)


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setattr(claims, "K6_03D_ENABLED", False)


def test_the_constant_ships_true():
    assert claims.K6_03D_ENABLED is True


def test_the_table_has_every_row_and_counterexample():
    assert len(TABLE) == 22
    assert len({text for text, _ in TABLE}) == 22
    assert len(TABLE_OFF) == 22


@pytest.mark.parametrize(("text", "breach"), TABLE)
def test_the_whole_table_when_on(on, text, breach):
    assert bool(_k6_term(text, set())) is breach, text


@pytest.mark.parametrize(("text", "breach"), sorted(TABLE_OFF.items()))
def test_the_whole_table_when_off(off, text, breach):
    assert claims.K6_03D_ENABLED is False
    assert bool(_k6_term(text, set())) is breach, text


@pytest.mark.parametrize("text", CLEAR_WHEN_ON)
def test_a_falling_score_or_a_day_range_clears_when_on(on, text):
    assert not _k6_term(text, set()), text


@pytest.mark.parametrize("word", SCORE_WORDS)
def test_every_score_word_clears_a_falling_pair_when_on(on, word):
    assert not _k6_term(f"{word} 24-17", set())
    assert not _k6_term(f"Chiefs {word} 24-17 on Saturday", set())


@pytest.mark.parametrize("word", SCORE_WORDS)
def test_every_score_word_breaches_a_rising_pair_when_on(on, word):
    assert _k6_term(f"{word} 18-24", set())


@pytest.mark.parametrize("word", SCORE_WORDS)
def test_every_score_word_breaches_when_off(off, word):
    assert _k6_term(f"{word} 24-17", set())


@pytest.mark.parametrize("noun", AUDIENCE_NOUNS)
def test_a_person_noun_after_the_range_blocks_a_score(on, noun):
    assert _k6_term(f"won 24-17 {noun}", set())
    assert _k6_term(f"scored 24-17 {noun} online", set())
    assert _k6_term(f"won 24-17 over the {noun}", set())


@pytest.mark.parametrize("noun", AUDIENCE_NOUNS)
def test_a_person_noun_after_the_range_blocks_a_date(on, noun):
    assert _k6_term(f"Sept 20-26 {noun}", set())
    assert _k6_term(f"Sept 20-26 for the {noun}", set())


@pytest.mark.parametrize("month", MONTHS)
def test_every_month_clears_a_day_range_when_on(on, month):
    assert not _k6_term(f"{month} 20-26", set())
    assert not _k6_term(f"{month}. 20-26", set())
    assert not _k6_term(f"Event runs {month} 20-26 in Joburg", set())


@pytest.mark.parametrize("month", MONTHS)
def test_every_month_breaches_a_day_range_when_off(off, month):
    assert _k6_term(f"{month} 20-26", set())


@pytest.mark.parametrize("text", STILL_BREACH_WHEN_ON)
def test_nothing_wider_than_the_candidate_clears(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", BAND_BREACH_WHEN_ON)
def test_the_group_noun_band_breaches_when_on(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", BAND_PASS_WHEN_ON)
def test_a_duration_or_a_decade_still_passes_when_on(on, text):
    assert not _k6_term(text, set()), text


@pytest.mark.parametrize("text", BAND_BREACH_WHEN_ON[:4] + BAND_PASS_WHEN_ON)
def test_the_band_is_untouched_when_off(off, text):
    assert not _k6_term(text, set()), text


@pytest.mark.parametrize("text", [t for t in STILL_BREACH_WHEN_ON if t not in ("Sept 20-32", "Sept 20-20", "Sept 24-20")])
def test_the_held_texts_breach_when_off_too(off, text):
    assert _k6_term(text, set()), text


def test_a_residual_the_candidate_accepts_is_pinned(on):
    # A rising day range after a month with no person noun clears. This is the stated cost of the date exception.
    assert not _k6_term("the April 18-24 cohort", set())
    assert not _k6_term("By Aug 18-24 students joined", set())
    assert not _k6_term("Feb 18-24 engagement doubled", set())


def test_seeds_list_is_not_touched_when_on(on):
    assert _breach_term("won 24-17", set())
    assert _breach_term("Sept 20-26", set())
    assert _breach_term("National Youth Service Corps", set())
    assert _breach_term("polls show 45-55 split", set())
    assert not _breach_term("the 18-24s are watching", set())


def test_one_term_is_swapped_and_one_is_added():
    base, new = claims._K6_TERMS, claims._K6_TERMS_03D
    assert len(new) == len(base) + 1
    assert [i for i, (a, b) in enumerate(zip(base, new)) if a is not b] == [
        next(i for i, t in enumerate(base) if t.pattern.startswith("(?<![\\d:/.\\-])\\b(?:1[3-9]")),
    ]
    for a, b in zip(base, new):
        assert a.pattern == b.pattern and a.flags == b.flags
    assert new[-1].pattern.endswith("s\\b")


def test_off_reads_the_original_list_object(monkeypatch):
    seen = []
    monkeypatch.setattr(claims, "_first_term", lambda text, exempt, terms: seen.append(terms))
    monkeypatch.setattr(claims, "K6_03D_ENABLED", False)
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
    c["text"] = "Pirates won 18-24"
    assert _k6(c, {}, set())[0] == "breach"
    c["text"] = "popular with the 25-34s."
    assert _k6(c, {}, set())[0] == "breach"


def test_a_claim_breaches_the_same_text_when_off(off):
    c = {"id": "c1", "text": "Pirates won 24-17 on Saturday", "evidence_ids": [], "quotes": []}
    assert _k6(c, {}, set())[0] == "breach"
    c["text"] = "popular with the 25-34s."
    assert _k6(c, {}, set())[0] == "pass"


def test_a_quoted_verified_span_is_still_exempt_when_on(on):
    assert not _k6_term('He said "the 18-24 group was loud today" at the show', {"the 18-24 group was loud today"})
