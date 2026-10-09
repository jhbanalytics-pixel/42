"""W8-DEC-03d candidate: narrow score and date exceptions to the K6 age-range pattern, and the N-Ns group noun band.

The exceptions sit behind claims.K6_03D_ENABLED, which ships True after W8-DEC-03d YES. The off fixture pins the K6
answers with it False. With it True every row of the plan's boundary and counterexample table
(PLAN-revision4-copy.md, "K6 boundary table" and "Counterexamples") gets the answer in TABLE below. Expected values are
written here, not read from the checker.

Two rows differ from the plan's "current result" column on purpose. "polls show 45-55 split" and "National Youth Service
Corps" stay breaches: the poll split and the proper noun exceptions are not part of the candidate and were dropped.
"""

import re

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
ABBREVIATED_MONTHS = {"Jan", "Feb", "Mar", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec"}
MONTHS = ["Jan", "January", "Feb", "February", "Mar", "April", "May", "June", "July", "Aug", "Sep", "Sept", "September",
          "Oct", "Nov", "Dec"]

# Clear when on: a falling pair after a score word, a rising day range after a month, a score with no person after it.
# Two rows left this list in the CR round. "Pirates won 24-17, fans cheered" breaches because a comma no longer ends the
# clause for this guard (CR-3, fail safe, accepted by the lead). "lost 30-30 in the end" breaches because a tie is not a
# falling pair, and W8-DEC-03d says falling pair only (CR-4). Both sit in STILL_BREACH_WHEN_ON now.
CLEAR_WHEN_ON = [
    "Chiefs won 24-17 on Saturday", "Won 24-17", "FT: 24-17", "HT 20-15", "Final Score: 31-24", "Half-Time 21-14",
    f"won 24{EN}17", "Event runs May 20-26 in Joburg",
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
    "Pirates won 24-17, fans cheered", "lost 30-30 in the end",
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
    assert not _k6_term(f"Event runs {month} 20-26 in Joburg", set())
    if month in ABBREVIATED_MONTHS:
        assert not _k6_term(f"{month}. 20-26", set())
    else:
        # W1-1: a full stop after a whole month name ends the clause, so the range opens a new one and is no date.
        assert _k6_term(f"{month}. 20-26", set())


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
    # A rising day range after a month with no person or group word anywhere in the clause clears. This is the stated
    # cost of the date exception. The cohort and student sentences used to sit here and pinned a defect: W8-DEC-03d
    # says a person word anywhere in the clause blocks, so they breach (CB-1) and are pinned below.
    assert not _k6_term("Feb 18-24 engagement doubled", set())
    assert not _k6_term("In July 21-29 singles posted", set())


# CB-1: the 16 sentences the closure review constructed, each a breach with the flag off and, until CB-1, a pass with it
# on. "In July 21-29 singles posted" is the seventeenth and stays clear: a single is a music single in this data.
CB1_SENTENCES = [
    "TikTok users in the June 18-24 bracket", "Voters in the May 18-24 bracket", "Women led it in May 18-24",
    "Fans streamed it most in June 18-24", "Users posted it in Sept 18-24", "Kenyans shared it from Aug 18-24",
    "Students drove it over May 18-25", "In May 18-24 students led the trend",
    "Since June 18-24 girls have flocked to the sound", "By Aug 18-24 graduates joined", "In Sept 18-24 moms posted most",
    "In May 18-25 gamers shared clips", "In June 18-24 TikTokers drove it", "In Oct 18-24 residents of Lagos posted",
    "In May 18-24 parents shared it", "In June 18-24 workers posted it",
]
CB1_GROUP_SENTENCES = ["the April 18-24 cohort", "The May 18-24 age group", "the Sept 18-24 group",
                       "By Aug 18-24 students joined"]

# The clause list CB-1 adds to, by word. _PERSON is not touched, so the ones it already holds are not repeated here.
CB1_WORDS = ["man", "men", "woman", "women", "girl", "girls", "boy", "boys", "student", "students", "parent", "parents",
             "mum", "mums", "mom", "moms", "dad", "dads", "gamer", "gamers", "graduate", "graduates", "resident",
             "residents", "worker", "workers", "tiktoker", "tiktokers", "subscriber", "subscribers", "cohort",
             "cohorts", "bracket", "brackets", "demographic", "demographics", "age group", "age groups",
             "Students", "WORKERS", "TikTokers"]


@pytest.mark.parametrize("text", CB1_SENTENCES + CB1_GROUP_SENTENCES)
def test_a_person_or_group_word_anywhere_in_the_clause_blocks_a_date(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", CB1_SENTENCES + CB1_GROUP_SENTENCES)
def test_the_cb1_sentences_breached_when_off_too(off, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("word", CB1_WORDS)
def test_each_cb1_word_blocks_a_date_before_or_after_the_range(on, word):
    assert _k6_term(f"The {word} led it in May 18-24", set())
    assert _k6_term(f"In May 18-24 the {word} posted most", set())
    assert _k6_term(f"Event runs Sept 20-26 for the {word}", set())


@pytest.mark.parametrize("word", CB1_WORDS)
def test_each_cb1_word_blocks_a_score_before_or_after_the_pair(on, word):
    assert _k6_term(f"The {word} saw the Chiefs won 24-17", set())
    assert _k6_term(f"Chiefs won 24-17 in front of the {word}", set())


def test_a_music_single_does_not_block(on):
    assert not _k6_term("In July 21-29 singles posted", set())
    assert not _k6_term("The single won 24-17 on streams", set())


def test_the_word_must_be_in_the_clause_not_the_text(on):
    # A clause ends at ; ! ? : and a full stop that is not a decimal point, a month abbreviation, "e.g." or "i.e.". A
    # comma does not end it (CR-3: this loop used to list the comma, and the pin flipped). Each pair is a clear text
    # with a person word one clause away on the far side of the stop, so dropping any stop from the clause end turns it
    # into a breach.
    for stop in (";", "!", "?", ":", "."):
        assert not _k6_term(f"Women posted{stop} the Chiefs won 24-17 on Saturday", set()), stop
        assert not _k6_term(f"Chiefs won 24-17 on Saturday{stop} women cheered", set()), stop
        assert not _k6_term(f"Students were polled{stop} the event runs Sept 20-26 in Joburg", set()), stop
        assert not _k6_term(f"The event runs Sept 20-26 in Joburg{stop} students are welcome", set()), stop


def test_a_month_full_stop_does_not_end_the_clause(on):
    assert _k6_term("Women led it in Sept. 18-24", set())
    assert _k6_term("Students joined in Aug. 18-24", set())
    assert not _k6_term("Women posted. Event runs Sept. 20-26 in Joburg", set())


# Whole-list pins. The word lists are what decide that a pair is a score or a date, so a widened list is a loosened K6.
SCORE_ALTERNATIVES = ["won", "lost", "beat", "drew", "scored", r"final\s+score", r"full[\s-]time", r"half[\s-]time"]
MONTH_ALTERNATIVES = ["Jan(?:uary)?", "Feb(?:ruary)?", "Mar(?:ch)?", "Apr(?:il)?", "June?", "July?", "Aug(?:ust)?",
                      "Sept?(?:ember)?", "Oct(?:ober)?", "Nov(?:ember)?", "Dec(?:ember)?"]
NEAR_MISS_SCORE_WORDS = ["win", "wins", "winning", "lose", "loses", "beats", "beaten", "draw", "drawn", "score",
                         "scores", "final", "finals", "full", "half", "tied", "edged", "Mon", "result", "ft", "ht"]
NEAR_MISS_MONTH_WORDS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Monday", "Sunday", "Smarch", "Mars", "Junk",
                         "Augusta", "Marc", "may", "week", "Q3"]


def _alternatives(pattern, head):
    return re.search(re.escape(head) + r"(.*?)\)\)", pattern).group(1)


def test_the_score_word_list_is_pinned_whole():
    ci = _alternatives(claims._SCORE_BEFORE.pattern, r"(?i:\b(?:").split("|")
    assert ci == ["won", "lost", "beat", "drew", "scored", r"final\s+score", r"full[\s-]time", r"half[\s-]time"]
    assert ci == SCORE_ALTERNATIVES
    assert re.search(r"\|\\b\(\?:(FT\|HT)\)\)", claims._SCORE_BEFORE.pattern).group(1) == "FT|HT"


def test_the_month_list_is_pinned_whole():
    months = _alternatives(claims._MONTH_BEFORE.pattern, r"(?i:\b(?:").split("|")
    assert months == MONTH_ALTERNATIVES
    assert claims._MONTH_BEFORE.pattern.endswith(r"|\bMay)\b\.?\s+$")


@pytest.mark.parametrize("word", NEAR_MISS_SCORE_WORDS)
def test_a_word_outside_the_score_list_does_not_clear_a_score(on, word):
    assert _k6_term(f"{word} 24-17", set()), word
    assert _k6_term(f"Chiefs {word} 24-17 on Saturday", set()), word


@pytest.mark.parametrize("word", NEAR_MISS_MONTH_WORDS)
def test_a_word_outside_the_month_list_does_not_clear_a_date(on, word):
    assert _k6_term(f"{word} 20-26", set()), word
    assert _k6_term(f"Event runs {word} 20-26 in Joburg", set()), word


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


# CR-3: the 60 person words the closure re-review found outside the clause list, written here and not read from the code.
CR3_WORDS = [
    "pupils", "ladies", "guys", "lads", "folks", "mothers", "fathers", "sisters", "brothers", "daughters", "sons",
    "couples", "families", "Africans", "Ghanaians", "Zimbabweans", "Ugandans", "Tanzanians", "Nairobians", "Lagosians",
    "citizens", "respondents", "participants", "attendees", "members", "supporters", "stans", "players", "athletes",
    "drivers", "commuters", "employees", "staff", "buyers", "readers", "influencers", "streamers", "artists",
    "musicians", "Instagrammers", "YouTubers", "netizens", "tweeps", "individuals", "clients", "patients", "husbands",
    "wives", "girlfriends", "boyfriends", "grads", "freshers", "punters", "bettors", "gamblers", "ravers", "clubgoers",
    "partygoers", "festivalgoers", "churchgoers",
]


def test_the_cr3_list_is_sixty_distinct_words():
    assert len(CR3_WORDS) == 60 and len({w.lower() for w in CR3_WORDS}) == 60


@pytest.mark.parametrize("word", CR3_WORDS)
def test_each_cr3_word_blocks_a_date_before_or_after_the_range(on, word):
    assert _k6_term(f"In May 18-24 {word} led the trend", set())
    assert _k6_term(f"{word} drove it in June 18-24", set())
    assert _k6_term(f"The {word} led it in May 18-24", set())
    assert _k6_term(f"Event runs Sept 20-26 for the {word}", set())


@pytest.mark.parametrize("word", CR3_WORDS)
def test_each_cr3_word_blocks_a_score_before_or_after_the_pair(on, word):
    assert _k6_term(f"The {word} saw the Chiefs won 24-17", set())
    assert _k6_term(f"Chiefs won 24-17 in front of the {word}", set())


@pytest.mark.parametrize("word", CR3_WORDS[:3] + CR3_WORDS[-3:])
def test_each_cr3_word_blocks_in_any_case(on, word):
    assert _k6_term(f"In May 18-24 {word.upper()} led the trend", set())
    assert _k6_term(f"In May 18-24 {word.lower()} led the trend", set())


# CR-3: a comma does not end the clause for this guard. The clause ends at ; : ! ? and a full stop that is not part of a
# month abbreviation, "e.g." or "i.e.".
COMMA_SENTENCES = [
    "Women, in May 18-24, led the trend", "In May 18-24, women led the trend", "Students, Sept 18-24, drove it",
    "Among women, May 18-24 was the peak", "In May 18-24 e.g. students posted", "In June 18-24 i.e. women posted",
    "Women, e.g. in May 18-24, led it", "In May 18-24, e.g. women, posted", "Women, i.e. the 18-24 group, led it",
    "Pirates won 24-17, fans cheered", "Women posted, the Chiefs won 24-17 on Saturday",
    "Chiefs won 24-17 on Saturday, women cheered", "Students were polled, the event runs Sept 20-26 in Joburg",
    "The event runs Sept 20-26 in Joburg, students are welcome", "Women, Sept. 18-24, led it", "In Sept. 18-24 women led it",
    "Women posted it in Sept. 18-24", "Event runs Sept. 20-26 in Joburg, e.g. for students",
    "Event runs Sept 20-26 in Joburg, i.e. for women",
]


@pytest.mark.parametrize("text", COMMA_SENTENCES)
def test_a_comma_does_not_end_the_clause(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", COMMA_SENTENCES)
def test_the_comma_sentences_breached_when_off_too(off, text):
    assert _k6_term(text, set()), text


def test_a_decimal_point_and_a_month_stop_still_behave(on):
    assert not _k6_term("Women posted. Event runs Sept 20-26 in Joburg", set())
    assert not _k6_term("Women posted in Dec to cheers. Event runs Sept 20-26 in Joburg", set())
    assert not _k6_term("Event runs Sept 20-26 in Joburg. Women posted in Dec to cheers", set())
    assert not _k6_term("Women rated it 4.5. Chiefs won 24-17 on Saturday", set())
    assert not _k6_term("Chiefs won 24-17 on Saturday. Women rated it 4.5", set())


# A person word that is only the tail of a longer word is not a person word.
@pytest.mark.parametrize("word", ["Carmen", "Roman", "Norman", "Ramen", "Bowman"])
def test_a_clause_word_must_be_a_whole_word(on, word):
    assert not _k6_term(f"{word} won 24-17 on Saturday", set()), word
    assert not _k6_term(f"Event runs Sept 20-26 with {word}", set()), word


# CR-4: age group, hyphenated or fused, and the other age words that name a group.
AGE_GROUP_SENTENCES = [
    "The May 18-24 age-group", "the May 18-24 agegroup", "The May 18-24 age group", "the May 18-24 age-groups",
    "the May 18-24 agegroups", "The May 18-24 age range", "the June 18-24 age band", "the June 18-24 age bracket",
    "the June 18-24 age category", "the June 18-24 age categories", "the June 18-24 age ranges",
    "the June 18-24 age bands", "The AGE-GROUP was Sept 20-26", "The age  range was Sept 20-26",
    "the Sept 18-24 groups", "the Sept 18-24 group",
]


@pytest.mark.parametrize("text", AGE_GROUP_SENTENCES)
def test_an_age_group_word_blocks_a_date(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", AGE_GROUP_SENTENCES)
def test_the_age_group_sentences_breached_when_off_too(off, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", ["The age of the Chiefs won 24-17", "Event runs Sept 20-26 for the group stage",
                                  "Chiefs won 24-17 in the group stage", "Chiefs won 24-17 in a group match",
                                  "The group stage: Chiefs won 24-17"])
def test_a_group_that_is_not_straight_after_the_range_stays_clear(on, text):
    assert not _k6_term(text, set()), text


# CR-4: a tie is not a falling pair.
@pytest.mark.parametrize("text", ["lost 30-30 in the end", "drew 20-20", "Final Score: 25-25", "FT 30-30",
                                  "Chiefs and Pirates drew 30-30 on Saturday", f"won 30{EN}30", "HT 20-20"])
def test_a_tie_score_breaches_when_on(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", ["lost 30-30 in the end", "drew 20-20", "Final Score: 25-25"])
def test_a_tie_score_breached_when_off_too(off, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", ["won 24-17", "lost 30-29 in the end", "Final Score: 31-24", "drew 25-24", "Sept 20-26",
                                  "Event runs Sept 20-21 in Joburg", "Chiefs won 24-17 on Saturday. Women cheered"])
def test_a_falling_pair_or_a_rising_day_range_with_no_person_word_still_passes(on, text):
    assert not _k6_term(text, set()), text


def test_a_decimal_point_does_not_end_the_clause(on):
    assert _k6_term("Women rated it 4.5 and Chiefs won 24-17 on Saturday", set())
    assert _k6_term("Chiefs won 24-17 on a 4.5 star day for women", set())
    assert _k6_term("Event runs Sept 20-26 and women rated it 4.5", set())


@pytest.mark.parametrize("stop", [".", ";", ":", "!", "?"])
def test_a_stop_straight_after_the_range_ends_the_clause(on, stop):
    assert not _k6_term(f"Chiefs won 24-17{stop} Women cheered", set()), stop
    assert not _k6_term(f"Event runs Sept 20-26{stop} Students are welcome", set()), stop


@pytest.mark.parametrize("word", ["Staffordshire", "Membership", "Readership", "Driversfield", "Playersburg"])
def test_a_clause_word_must_not_be_the_front_of_a_longer_word(on, word):
    assert not _k6_term(f"{word} won 24-17 on Saturday", set()), word
    assert not _k6_term(f"Event runs Sept 20-26 at {word}", set()), word


def test_a_dash_with_spaces_between_age_and_group_still_blocks(on):
    assert _k6_term("The age - range was Sept 20-26", set())
    assert _k6_term("the May 18-24 age -group", set())
    assert _k6_term("the May 18-24 age- band", set())


def test_e_g_must_start_at_a_word(on):
    # "Pre.g." is a word that ends in e, then .g., and not the abbreviation: its full stops end the clause.
    assert not _k6_term("Women cheered Pre.g. Chiefs won 24-17", set())
    assert not _k6_term("Women cheered Chi.e. Chiefs won 24-17", set())


# CR-3b: the singular of each of the 60 CR-3 words, written here by hand. A singular is stricter, never looser.
CR3B_SINGULARS = [
    "pupil", "lady", "guy", "lad", "folk", "mother", "father", "sister", "brother", "daughter", "son", "couple",
    "family", "African", "Ghanaian", "Zimbabwean", "Ugandan", "Tanzanian", "Nairobian", "Lagosian", "citizen",
    "respondent", "participant", "attendee", "member", "supporter", "stan", "player", "athlete", "driver", "commuter",
    "employee", "staff", "buyer", "reader", "influencer", "streamer", "artist", "musician", "Instagrammer", "YouTuber",
    "netizen", "tweep", "individual", "client", "patient", "husband", "wife", "girlfriend", "boyfriend", "grad",
    "fresher", "punter", "bettor", "gambler", "raver", "clubgoer", "partygoer", "festivalgoer", "churchgoer",
]


def test_the_cr3b_singulars_are_sixty_distinct_words():
    assert len(CR3B_SINGULARS) == 60 and len({w.lower() for w in CR3B_SINGULARS}) == 60


@pytest.mark.parametrize("word", CR3B_SINGULARS)
def test_each_cr3b_singular_blocks_a_date_before_or_after_the_range(on, word):
    assert _k6_term(f"In May 18-24 the {word} led the trend", set())
    assert _k6_term(f"The {word} drove it in June 18-24", set())
    assert _k6_term(f"A {word} led it in May 18-24", set())
    assert _k6_term(f"Event runs Sept 20-26 for a {word}", set())


@pytest.mark.parametrize("word", CR3B_SINGULARS)
def test_each_cr3b_singular_blocks_a_score_before_or_after_the_pair(on, word):
    assert _k6_term(f"The {word} saw the Chiefs won 24-17", set())
    assert _k6_term(f"Chiefs won 24-17 in front of the {word}", set())


@pytest.mark.parametrize("word", CR3B_SINGULARS[:3] + CR3B_SINGULARS[-3:])
def test_each_cr3b_singular_blocks_in_any_case(on, word):
    assert _k6_term(f"In May 18-24 the {word.upper()} led the trend", set())


@pytest.mark.parametrize("word", ["Membership", "Readership", "Citizenship", "Fatherland", "Motherwell", "Sonic",
                                  "Clientele", "Playerunknown", "Staffordshire", "Ladybird", "Familyman"])
def test_a_singular_must_be_a_whole_word(on, word):
    assert not _k6_term(f"{word} won 24-17 on Saturday", set()), word
    assert not _k6_term(f"Event runs Sept 20-26 at {word}", set()), word


# CR-3b: a focus group holds anywhere in the clause, as an age group already did. A bare "group" still does not.
FOCUS_SENTENCES = [
    "the Sept 18-24 focus group", "In the focus group, Chiefs won 24-17", "Chiefs won 24-17 in the focus group",
    "Event runs Sept 20-26 in the focus group room", "The focus-group saw the Chiefs won 24-17",
    "Event runs Sept 20-26 for focus groups", "Chiefs won 24-17 in front of focus  groups",
    "The age group saw the Chiefs won 24-17", "Chiefs won 24-17 in front of the age group",
    "The age group was in the room, then Event runs Sept 20-26", "Chiefs won 24-17 in a focus - group", "Chiefs won 24-17 in the focusgroup", "Event runs Sept 20-26 for the age-group",
]


@pytest.mark.parametrize("text", FOCUS_SENTENCES)
def test_a_focus_group_or_age_group_holds_anywhere_in_the_clause(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", FOCUS_SENTENCES)
def test_the_focus_group_sentences_breached_when_off_too(off, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", ["Women posted. The focus group met. Event runs Sept 20-26 in Joburg",
                                  "Event runs Sept 20-26 in Joburg. The focus group met",
                                  "Chiefs won 24-17 on Saturday; the age group met",
                                  "Chiefs won 24-17 on Saturday. The focus group met",
                                  "Chiefs won 24-17 in the group stage", "Event runs Sept 20-26 in the group room",
                                  "Women: Sept 18-24"])
def test_a_focus_group_in_another_clause_and_the_ruled_stops_still_clear(on, text):
    assert not _k6_term(text, set()), text


# W1-1: the month must sit in the range's own clause. A full stop after a whole month name ends the previous clause, so
# the range that opens the next sentence is an age range. A full stop after an abbreviation does not end it.
MONTH_STOP_BREACHES = [
    "It took off in May. 18-24 led the trend.", "Viewers peaked in June. 18-24 was the biggest group.",
    "Peak was in September. 18-24 drove it.", "Most posts came in July. 25-30 were the most active.",
    "Women led it in June. 18-24", "Women led it in September. 18-24",
]


@pytest.mark.parametrize("text", MONTH_STOP_BREACHES)
def test_a_range_that_opens_a_sentence_after_a_month_is_no_date(on, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", MONTH_STOP_BREACHES)
def test_the_month_stop_sentences_breached_when_off_too(off, text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", ["Sept. 18-24", "Sept 20-26", "Event runs Sept. 20-26 in Joburg",
                                  "Peak was in Sept. 18-24", "Women posted. Event runs Sept. 20-26 in Joburg"])
def test_a_month_abbreviation_with_a_full_stop_still_clears_its_own_range(on, text):
    assert not _k6_term(text, set()), text


# W1-2: the quote floor counts words with the list the a80be1d floor used, so a quote whose range reads as a date or a
# score on its own is not stripped just because the 03d list calls it clear.
QUOTE_FLOOR_BREACHES = [
    ('Women said "won 24-17 today"', "won 24-17 today"),
    ('Fans posted "May 18-24 vibes"', "May 18-24 vibes"),
    ('Students wrote "in June 18-24"', "in June 18-24"),
]


@pytest.mark.parametrize("text,quote", QUOTE_FLOOR_BREACHES)
def test_a_verified_three_word_quote_still_breaches_when_its_range_is_its_only_age_word(on, text, quote):
    assert _k6_term(text, {quote}), text
    assert _k6_term(text, set()), text
    assert _k6({"text": text}, {}, {quote})[0] == "breach"


@pytest.mark.parametrize("text,quote", QUOTE_FLOOR_BREACHES)
def test_the_quote_floor_sentences_breach_when_off_too(off, text, quote):
    assert _k6_term(text, {quote}), text


def test_a_verified_quote_with_three_words_besides_its_range_is_still_exempt(on):
    assert not _k6_term('Women said "we won 24-17 today folks"', {"we won 24-17 today folks"})
    assert not _k6_term('Fans posted "see you May 18-24 vibes"', {"see you May 18-24 vibes"})


def test_the_quote_floor_counts_with_the_k6_list_not_the_seeds_list_when_on(on):
    # "elders" is a K6 term and no seeds term, so the floor takes it out of the count when it counts quote words.
    assert _k6_term('He wrote "the elders spoke"', {"the elders spoke"}) == "elders"
    assert not _k6_term('He wrote "the elders spoke today"', {"the elders spoke today"})
