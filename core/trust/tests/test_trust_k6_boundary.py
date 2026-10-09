"""K6 age-range boundary (plan section 9, "K6 boundary table"). The first table pins what the checker returned at
a80be1d for each string, so tightening cannot change a currently correct result. The second table is N13-T: forms
that are age ranges and were missed. Nothing here adds a score or date exception (W8-DEC-03d is not built)."""

import pytest

from core.trust.claims import _k6_term

# (text, breach) as the checker at a80be1d returned it. Every row stays as it is.
UNCHANGED = [
    ("aged 18-24", True), ("18-24 year olds", True), ("ages 25 to 34", True), ("fans aged 18 to 24", True),
    ("the 18-24 group", True), ("won 24-17", True), ("final score 21-14", True), ("scored 30-28 at the weekend", True),
    ("polls show 45-55 split", True), ("Sept 20-26", True), ("between 20-26 Sept", False),
    ("20-26 September", False), ("from 13-19 October", False), ("Pirates beat Chiefs 2-1", False),
    ("National Youth Service Corps", True), ("mostly women", True), ("google trends shows", True),
    ("won 18-24 voters", True), ("15-30s video", False), ("18-24s response time", False), ("18-24s", False),
    ("Mostly 18-24", True), ("people 18-24 share it", True), ("Viewers aged 18-24 engage", True),
    ("queues of 20-30 minutes", True),
]

# Age ranges the checker missed. N13-T: breach.
MISSED = [
    "aged between 18 and 24", "Viewers aged between 25 and 34 engage", "ages between 18 and 24",
    "ages from 18 to 24", "aged from 25 to 34",
]

# The N-Ns band as a group noun. N13-T drops it (plan section 9: no pattern for it passes the decade, temperature
# and mark cases below), so these stay as the a80be1d checker returned them. They move to W8-DEC-03d, which is not
# built. When that decision lands these rows flip to breach; until then they pin what is shipped.
DEFERRED_W8_DEC_03D = ["the 18-24s are watching", "among 18-24s", "popular with the 25-34s.", "among 18-24s and 25-34s"]

# N-Ns forms that are not age ranges. Still pass after N13-T.
STILL_PASS = [
    "15-30s video", "18-24s response time", "18-24s", "the 18-24s response time", "clips of 15-30s duration",
    "the 1980s are back", "temperatures in the 20-30s", "marks in the 70-80s are rare",
    "Music from the 70-80s is trending again", "Throwback hits from the 70-80s.",
    "Highs in Joburg sit in the 20-30s.", "Most marks were in the 60-70s.",
]


@pytest.mark.parametrize(("text", "breach"), UNCHANGED)
def test_the_current_result_is_unchanged(text, breach):
    assert bool(_k6_term(text, set())) is breach, text


@pytest.mark.parametrize("text", MISSED)
def test_a_missed_age_range_now_breaches(text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", STILL_PASS)
def test_a_duration_or_count_range_still_passes(text):
    assert not _k6_term(text, set()), text


@pytest.mark.parametrize("text", DEFERRED_W8_DEC_03D)
def test_the_n_ns_group_noun_form_is_left_to_w8_dec_03d(text):
    assert not _k6_term(text, set()), text


# Lead ruling after the second review. "generation" as a length of time is not an audience; as a group it still is.
GENERATION_PASSES = [
    "once in a generation", "a once in a generation talent", "a once-in-a-generation talent", "a generation ago",
    "for a generation", "a generation of content", "Kept the title for a generation",
]
GENERATION_BREACHES = [
    "a generation of young fans", "this generation is", "the next generation", "a new generation",
    "a generation talent", "the new generation",
]

# "mid 20s" and its kin are a temperature or a score as often as an age. A breach only with age context in the same
# sentence (aged, in their, people, fans, women, men, users); with neither, or with weather or score context, it passes.
DECADE_PASSES = [
    "temperatures in the mid 20s", "highs in the mid-20s C", "26 degrees, in the mid 20s", "the weather hit the mid 20s",
    "he scored in the early 30s", "scores in the late 60s", "Joburg sat in the mid 20s all week", "the mid 20s",
    "Fans cheered. Highs hit the mid 20s.", "Highs hit the mid 20s. Women cheered",
]
DECADE_BREACHES = [
    "women in the mid 20s", "people in the early 30s", "fans in their mid 20s", "men aged mid 20s",
    "users in the late 20s", "aged mid 20s", "fans love it. Women in the mid 20s lead", "mid 20s fans",
]
# The word forms stay a breach without any context.
DECADE_WORDS = ["early thirties", "mid twenties", "late forties", "in the mid-twenties"]


@pytest.mark.parametrize("text", GENERATION_PASSES)
def test_generation_as_a_length_of_time_passes(text):
    assert not _k6_term(text, set()), text


@pytest.mark.parametrize("text", GENERATION_BREACHES)
def test_generation_as_an_audience_group_still_breaches(text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", DECADE_PASSES)
def test_a_mid_decade_without_age_context_passes(text):
    assert not _k6_term(text, set()), text


@pytest.mark.parametrize("text", DECADE_BREACHES)
def test_a_mid_decade_with_age_context_breaches(text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", DECADE_WORDS)
def test_the_spelled_out_decades_always_breach(text):
    assert _k6_term(text, set()), text
