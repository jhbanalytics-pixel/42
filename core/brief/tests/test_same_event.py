"""Two Today cards of one market and run describe the same event (core/brief/same_event.py): 12 October 2026, all
4 ZA cards were the MTN8 final, and 4 of NG's 7 were one Liverpool and Manchester City match.

The rule reads the checked explanation (the why now sentence the claim checks passed) and the member posts of each
card. Pairs that must stay apart come from the three reviews of the parked title merge (lanes/REVIEW-opus-today-dupes*)
and from the replay fixtures."""

import pytest

from core.brief import same_event
from core.brief.same_event import dates, names, shared_names


def card(explanation, posts=("p1", "p2", "p3")):
    return {"explanation": explanation, "posts": set(posts)}


def test_names_are_runs_of_capitalised_words():
    text = ("Creators in South Africa discussed football, likely because Mamelodi Sundowns defeated rivals Orlando "
            "Pirates in the MTN8 final.")
    assert names(text) == {("mamelodi", "sundowns"), ("orlando", "pirates"), ("mtn8",)}


def test_platforms_markets_demonyms_and_calendar_words_are_never_names():
    text = ("Multiple creators in Nigeria and Kenya posted on TikTok, Instagram, YouTube and Twitter, while South "
            "African and Kenyan fans on Threads reacted on Saturday 10 October.")
    assert names(text) == set()


def test_a_word_that_only_opens_the_sentence_is_not_a_name():
    assert names("Liverpool beat Arsenal.") == {("arsenal",)}
    assert names("On TikTok, local creators shared clips.") == set()
    assert names("The Springboks beat England.") == {("springboks",), ("england",)}


def test_punctuation_ends_a_name():
    assert names("Fans of Liverpool, Arsenal and Chelsea posted.") == {("liverpool",), ("arsenal",), ("chelsea",)}


def test_a_handle_is_not_a_name():
    text = "Fans quoted Soccer_Laduma and f.a.m.e_sa and @IOLsport about Orlando Pirates."
    assert names(text) == {("orlando", "pirates")}


def test_accents_and_possessives_fold():
    assert names("Fans watched Atlético play.") == names("Fans watched Atletico play.") == {("atletico",)}
    assert names("Fans mourned Liverpool's defeat.") == {("liverpool",)}


def test_a_name_matches_a_longer_one_it_opens_and_no_other():
    a = names("Fans watched the MTN8 Cup Final and Manchester City.")
    b = names("Fans watched the MTN8 and Manchester United.")
    assert shared_names(a, b) == 1


def test_a_name_counts_once_however_many_it_matches():
    a = names("Fans watched the MTN8 Cup Final.")
    b = names("Fans watched the MTN8 and the MTN8 Finale.")
    assert shared_names(a, b) == 1


def test_two_longer_names_one_short_name_opens_count_once():
    a = names("Fans watched the MTN8.")
    b = names("Fans watched the MTN8 Cup Final and the MTN8 Finale.")
    assert shared_names(a, b) == shared_names(b, a) == 1


@pytest.mark.parametrize("text,expected", [
    ("likely because Manchester City played on 11 Oct.", {(10, 11)}),
    ("likely because it was released on 2026-10-01.", {(10, 1)}),
    ("likely because the final took place on Saturday 10 October.", {(10, 10)}),
    ("likely because of the match on October 4 and on 5th Oct.", {(10, 4), (10, 5)}),
    ("likely because of the match.", set()),
])
def test_dates_are_day_and_month(text, expected):
    assert dates(text) == expected


DATED_CITY = "Fans posted on 11 Oct, likely because Manchester City beat Liverpool in the Premier League."
DATED_CITY_2 = "Fans discussed the Premier League, likely because Man City played Liverpool on 11 Oct."
UNDATED_MTN8 = "Fans posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final."
UNDATED_MTN8_2 = "Fans reacted, likely because Mamelodi Sundowns claimed the MTN8 against Orlando Pirates."


def test_two_shared_names_and_a_shared_date_are_one_event():
    assert same_event.same_event(card(DATED_CITY), card(DATED_CITY_2))


def test_without_a_date_in_both_three_shared_names_are_needed():
    assert same_event.same_event(card(UNDATED_MTN8), card(UNDATED_MTN8_2))
    two = "Fans posted, likely because Orlando Pirates reached the MTN8 final."
    assert not same_event.same_event(card(UNDATED_MTN8), card(two))


def test_a_date_in_one_explanation_only_still_needs_three_shared_names():
    dated = "Fans posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final on 10 Oct."
    assert same_event.same_event(card(dated), card(UNDATED_MTN8_2))
    assert not same_event.same_event(card(DATED_CITY), card("Fans posted about Liverpool in the Premier League."))


def test_different_dates_are_different_events_whatever_the_names_share():
    a = "Fans posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 at Moses Mabhida on 10 Oct."
    b = "Fans posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 at Moses Mabhida on 3 Oct."
    assert not same_event.same_event(card(a), card(b))


def test_fewer_than_three_shared_member_posts_are_never_one_event():
    assert not same_event.same_event(card(DATED_CITY, ("p1", "p2", "x")), card(DATED_CITY_2, ("p1", "p2", "y")))
    assert same_event.same_event(card(DATED_CITY, ("p1", "p2", "p3")), card(DATED_CITY_2, ("p1", "p2", "p3")))


def test_the_rule_is_symmetric():
    a, b = card(DATED_CITY), card(DATED_CITY_2)
    assert same_event.same_event(a, b) == same_event.same_event(b, a)


# Pairs that must stay two cards. Each shares every post, so only the explanations keep them apart.
APART = {
    "two premier league matches on one day": (
        "Fans posted, likely because Arsenal beat Chelsea in the Premier League on 11 Oct.",
        "Fans posted, likely because Manchester City beat Liverpool in the Premier League on 11 Oct."),
    "one side, two matches a week apart": (
        "Fans posted, likely because Manchester City beat Liverpool in the Premier League on 11 Oct.",
        "Fans posted, likely because Liverpool beat Arsenal in the Premier League on 4 Oct."),
    "one side, two matches, one undated": (
        "Fans posted, likely because Manchester City beat Liverpool in the Premier League on 11 Oct.",
        "Fans posted, likely because Liverpool beat Arsenal in the Premier League."),
    "the men's side and the women's side against one opponent": (
        "Fans posted, likely because Bafana Bafana beat Egypt in a friendly.",
        "Fans posted, likely because Banyana Banyana drew with Egypt in a friendly."),
    "rugby and cricket against England on one day": (
        "Fans posted, likely because the Springboks beat England at Twickenham on 11 Oct.",
        "Fans posted, likely because the Proteas lost to England at Lord's on 11 Oct."),
    "Kenya against Mali and the Swahili word mali": (
        "Fans posted, likely because Harambee Stars played Mali in a qualifier.",
        "Fans posted, likely because people argued that mali ya umma was misused."),
    "a Nigeria and Ghana match and the jollof argument": (
        "Fans posted, likely because the Super Eagles played the Black Stars of Ghana.",
        "Fans posted, likely because Nigerian and Ghanaian cooks argued about jollof rice."),
    "one politician, two stories on two dates": (
        "Fans posted, likely because Edwin Sifuna announced the Linda Mwananchi Movement on 4 Oct.",
        "Fans posted, likely because Edwin Sifuna backed Babu Owino for Nairobi governor on 11 Oct."),
    "one politician, two stories without dates": (
        "Fans posted, likely because Edwin Sifuna announced the Linda Mwananchi Movement.",
        "Fans posted, likely because Edwin Sifuna backed Babu Owino for Nairobi governor."),
    "a semi-final and a final of one cup": (
        "Fans posted, likely because Orlando Pirates beat Stellenbosch in the MTN8 semi-final on 20 Sep.",
        "Fans posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final on 10 Oct."),
    "one cup, two undated matches of one side": (
        "Fans posted, likely because Orlando Pirates beat Stellenbosch in the MTN8.",
        "Fans posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8."),
    "Manchester City and Manchester United": (
        "Fans posted, likely because Manchester City won in the Premier League on 11 Oct.",
        "Fans posted, likely because Manchester United lost in the Premier League on 11 Oct."),
    "South Africa alone in both": (
        "Creators in South Africa posted, likely because South Africa hosted a summit on 11 Oct.",
        "Creators in South Africa posted, likely because South Africa raised the petrol price on 11 Oct."),
}


@pytest.mark.parametrize("pair", sorted(APART))
def test_distinct_events_that_share_their_posts_stay_apart(pair):
    a, b = APART[pair]
    assert not same_event.same_event(card(a), card(b))
    assert not same_event.same_event(card(b), card(a))


def test_an_empty_or_missing_explanation_is_no_event():
    assert not same_event.same_event(card(""), card(""))
    assert not same_event.same_event({"explanation": None, "posts": {"p1", "p2", "p3"}}, card(DATED_CITY))
