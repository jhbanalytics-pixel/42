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
    "the 18-24s are watching", "among 18-24s", "popular with the 25-34s.", "among 18-24s and 25-34s",
]

# N-Ns forms that are not age ranges. Still pass after N13-T.
STILL_PASS = ["15-30s video", "18-24s response time", "18-24s", "the 18-24s response time", "clips of 15-30s duration"]


@pytest.mark.parametrize(("text", "breach"), UNCHANGED)
def test_the_current_result_is_unchanged(text, breach):
    assert bool(_k6_term(text, set())) is breach, text


@pytest.mark.parametrize("text", MISSED)
def test_a_missed_age_range_now_breaches(text):
    assert _k6_term(text, set()), text


@pytest.mark.parametrize("text", STILL_PASS)
def test_a_duration_or_count_range_still_passes(text):
    assert not _k6_term(text, set()), text
