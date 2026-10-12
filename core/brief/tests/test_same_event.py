"""Two Today cards of one market and run describe the same event (core/brief/same_event.py): 12 October 2026, all
4 ZA cards were the MTN8 final, and 4 of NG's 7 were one Liverpool and Manchester City match. The rule runs in shadow
(RELEASE-E-PLAN item 8h, W8 header): job.py records what it would merge and changes nothing a reader sees.

The rule reads the checked explanation (the why now sentence the claim checks passed) and the member posts of each
card. Pairs that must stay apart come from the three reviews of the parked title merge (lanes/REVIEW-opus-today-dupes*),
the review of the first event merge (lanes/REVIEW-opus-d-event-merge.md, finding I1) and the replay fixtures."""

import pytest

from core.brief import same_event
from core.brief.same_event import dates, names, shared_names, sides, stages


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


def test_a_name_that_carries_a_platform_or_a_market_word_is_not_a_name():
    text = "Fans said Nigerian Twitter, X Spaces and TikTok Live, and SA fans and RSA voters, cheered Davido."
    assert names(text) == {("davido",)}


def test_places_of_the_gazetteer_are_never_names():
    text = "Fans posted, likely because President Cyril Ramaphosa spoke in Pretoria, Cape Town and Lagos."
    assert names(text) == {("president", "cyril", "ramaphosa")}
    assert names("Fans posted, likely because Cape Town City won.") == {("cape", "town", "city")}


def test_a_word_that_only_opens_the_sentence_is_not_a_name():
    assert names("Liverpool beat Arsenal.") == {("arsenal",)}
    assert names("On TikTok, local creators shared clips.") == set()
    assert names("The Springboks beat England.") == {("springboks",), ("england",)}


def test_punctuation_ends_a_name_and_a_sentence_ends_at_its_stop():
    assert names("Fans of Liverpool, Arsenal and Chelsea posted.") == {("liverpool",), ("arsenal",), ("chelsea",)}
    assert names("Fans cheered Arsenal. Chelsea fans sulked.") == {("arsenal",)}


def test_a_handle_is_not_a_name():
    text = "Fans quoted Soccer_Laduma and f.a.m.e_sa and @IOLsport and Www.Kick and Kick.Off about Orlando Pirates."
    assert names(text) == {("orlando", "pirates")}


def test_digits_stay_in_a_name_and_a_word_led_by_a_digit_can_be_one():
    assert names("Fans posted, likely because Eskom announced Stage 2 for Soweto.") == {("eskom",), ("stage", "2")}
    assert names("Fans posted, likely because 5FM played it.") == {("5fm",)}


def test_accents_and_possessives_fold():
    assert names("Fans watched Atlético play.") == names("Fans watched Atletico play.") == {("atletico",)}
    assert names("Fans mourned Liverpool's defeat.") == {("liverpool",)}


def test_a_short_name_with_a_digit_or_in_capitals_opens_a_longer_one():
    assert shared_names(names("Fans watched the MTN8."), names("Fans watched the MTN8 Cup Final.")) == 1
    assert shared_names(names("Fans watched the AFCON."), names("Fans watched the AFCON Final.")) == 1


def test_no_other_name_opens_a_longer_one():
    assert shared_names(names("Fans watched Manchester City."), names("Fans watched Manchester City Women.")) == 0
    assert shared_names(names("Fans visited Table Mountain."), names("Fans visited Table Mountain Park.")) == 0
    assert shared_names(names("Fans watched the MTN8."), names("Fans watched the MTN8 Women.")) == 0


def test_a_shortened_word_matches_the_full_one_when_the_last_words_agree():
    assert shared_names(names("Fans watched Man City."), names("Fans watched Manchester City.")) == 1
    assert shared_names(names("Fans watched Man United."), names("Fans watched Manchester City.")) == 0
    assert shared_names(names("Fans watched Ma City."), names("Fans watched Manchester City.")) == 0


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
    ("likely because of 4 Decades of music, the Marathon 21, Mayor 2 and Junior 3.", set()),
    ("likely because the show starts Oct 10:30 and runs to 40 Oct and 2026-13-40.", set()),
])
def test_dates_are_day_and_month(text, expected):
    assert dates(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Mamelodi Sundowns Ladies beat Orlando Pirates Women.", {"women"}),
    ("Manchester City Women's side won, and Banyana Banyana drew.", {"women"}),
    ("The reserves and the U20 side and the Under-23 side played.", {"reserves", "u20", "u23"}),
    ("Bafana Bafana beat Egypt.", {"bafana"}),
    ("Mamelodi Sundowns beat Orlando Pirates.", set()),
])
def test_sides(text, expected):
    assert sides(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("They met in the MTN8 semi-final.", {"semi-final"}),
    ("They met in the semi final, then the final.", {"semi-final", "final"}),
    ("They met in the MTN8 Cup Final and the MTN8 Finale.", {"final"}),
    ("They met in the quarter-final first leg.", {"quarter-final", "first leg"}),
    ("They met in a friendly.", set()),
])
def test_stages(text, expected):
    assert stages(text) == expected


DATED_CITY = "Fans posted on 11 Oct, likely because Manchester City beat Liverpool in the Premier League."
DATED_CITY_2 = "Fans discussed the Premier League, likely because Man City played Liverpool on 11 Oct."
MTN8 = "Fans posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final on 10 Oct."
MTN8_2 = "Fans reacted, likely because Mamelodi Sundowns claimed the MTN8 final over Orlando Pirates on 10 Oct."


def test_three_shared_names_and_the_same_date_are_one_event():
    assert same_event.same_event(card(DATED_CITY), card(DATED_CITY_2))
    assert same_event.same_event(card(MTN8), card(MTN8_2))


def test_the_reason_says_what_was_shared():
    assert same_event.reason(card(MTN8), card(MTN8_2)) == {"shared_posts": 3, "shared_names": 3, "date": ["10-10"]}
    assert same_event.reason(card(MTN8), card(DATED_CITY)) is None


def test_both_explanations_must_name_the_same_dates():
    undated = "Fans reacted, likely because Mamelodi Sundowns claimed the MTN8 final over Orlando Pirates."
    assert not same_event.same_event(card(MTN8), card(undated))
    assert not same_event.same_event(card(undated), card(undated))
    other_day = MTN8_2.replace("10 Oct", "3 Oct")
    assert not same_event.same_event(card(MTN8), card(other_day))
    two_days = MTN8_2.replace("on 10 Oct", "on 10 Oct and 11 Oct")
    assert not same_event.same_event(card(MTN8), card(two_days))


def test_two_shared_names_are_not_enough():
    two = "Fans posted, likely because Orlando Pirates reached the MTN8 final on 10 Oct."
    assert not same_event.same_event(card(MTN8), card(two))


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
        "Fans posted, likely because Bafana Bafana beat Egypt in a friendly on 11 Oct.",
        "Fans posted, likely because Banyana Banyana drew with Egypt in a friendly on 11 Oct."),
    "rugby and cricket against England on one day": (
        "Fans posted, likely because the Springboks beat England at Twickenham on 11 Oct.",
        "Fans posted, likely because the Proteas lost to England at Lord's on 11 Oct."),
    "Kenya against Mali and the Swahili word mali": (
        "Fans posted, likely because Harambee Stars played Mali in a qualifier on 11 Oct.",
        "Fans posted, likely because people argued that mali ya umma was misused on 11 Oct."),
    "a Nigeria and Ghana match and the jollof argument": (
        "Fans posted, likely because the Super Eagles played the Black Stars of Ghana on 11 Oct.",
        "Fans posted, likely because Nigerian and Ghanaian cooks argued about jollof rice on 11 Oct."),
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
    # REVIEW-opus-d-event-merge I1, as the reviewer wrote them (probes/pairs.py)
    "I1 men and women, same clubs, same day": (
        "Creators in South Africa posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final on "
        "10 Oct.",
        "Creators in South Africa posted, likely because Mamelodi Sundowns Ladies beat Orlando Pirates Women in the "
        "Hollywoodbets Super League on 10 Oct."),
    "I1 City men and City Women, same day": (
        "Creators in Nigeria posted on 11 Oct, likely because Manchester City defeated Liverpool in the Premier League.",
        "Creators in Nigeria posted on 11 Oct, likely because Manchester City Women defeated Liverpool Women in the "
        "Women's Super League."),
    "I1 two league matches, one names the other club": (
        "Multiple creators in Nigeria posted commentary on TikTok regarding a football fixture on 11 Oct, likely "
        "because Manchester City defeated Liverpool in their Premier League match.",
        "Creators in Nigeria posted on TikTok, likely because Chelsea beat Bournemouth on 11 Oct to move level with "
        "Manchester City in the Premier League."),
    "I1 semi and final, undated, same venue": (
        "Creators in South Africa posted, likely because Orlando Pirates beat Stellenbosch in the MTN8 semi-final at "
        "Moses Mabhida Stadium in Durban.",
        "Creators in South Africa posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final at "
        "Moses Mabhida Stadium in Durban."),
    "I1 semi names the final's date": (
        "Creators in South Africa posted, likely because Orlando Pirates beat Stellenbosch in the MTN8 semi-final to "
        "set up a final against Mamelodi Sundowns on 10 Oct.",
        "Creators in South Africa posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final on "
        "10 Oct."),
    "I1 semi dated, final undated": (
        "Creators in South Africa posted, likely because Orlando Pirates beat Stellenbosch in the MTN8 semi-final in "
        "Durban on 20 Sep.",
        "Creators in South Africa posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final in "
        "Durban."),
    "I1 semi and final, same date, same venue": (
        "Creators in South Africa posted, likely because Orlando Pirates beat Stellenbosch in the MTN8 semi-final at "
        "Moses Mabhida Stadium on 10 Oct.",
        "Creators in South Africa posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final at "
        "Moses Mabhida Stadium on 10 Oct."),
    "I1 the final and a reserves fixture of the same clubs": (
        "Creators in South Africa posted, likely because Mamelodi Sundowns beat Orlando Pirates in the MTN8 final at "
        "Moses Mabhida Stadium on 10 Oct.",
        "Creators in South Africa posted, likely because Mamelodi Sundowns reserves beat Orlando Pirates reserves at "
        "Moses Mabhida Stadium on 10 Oct."),
    "I1 Proteas and Proteas Women against England": (
        "Creators in South Africa posted, likely because the Proteas beat England at Lord's in the World Cup on 11 Oct.",
        "Creators in South Africa posted, likely because the Proteas Women beat England at Lord's in the World Cup on "
        "11 Oct."),
    "I1 Stage 2 and Stage 6, undated": (
        "Creators in South Africa complained on TikTok, likely because Eskom announced Stage 2 load shedding for "
        "Johannesburg.",
        "Creators in South Africa complained on TikTok, likely because Eskom escalated to Stage 6 load shedding in "
        "Johannesburg after Kusile failed."),
    "I1 Stage 2 and Stage 6, same day": (
        "Creators in South Africa complained on 11 Oct, likely because Eskom announced Stage 2 load shedding at Kusile "
        "Power Station.",
        "Creators in South Africa complained on 11 Oct, likely because Eskom suspended Stage 6 at Kusile Power "
        "Station."),
    "I1 one artist, two events, same day, one city": (
        "Creators in Nigeria posted, likely because Burna Boy performed a sold-out show in Lagos on 11 Oct.",
        "Creators in Nigeria posted, likely because Burna Boy was detained by police in Lagos on 11 Oct."),
    "I1 president, two stories, same day": (
        "Creators in Nigeria posted, likely because President Bola Tinubu announced a fuel price cut in Abuja on 11 Oct.",
        "Creators in Nigeria posted, likely because President Bola Tinubu sacked the Minister of Works in Abuja on "
        "11 Oct."),
    "I1 Ramaphosa, two stories, same day": (
        "Creators in South Africa posted, likely because President Cyril Ramaphosa signed the BELA Act in Pretoria on "
        "11 Oct.",
        "Creators in South Africa posted, likely because President Cyril Ramaphosa fired the Minister of Police in "
        "Pretoria on 11 Oct."),
    "I1 one artist, two releases, undated": (
        "Creators in South Africa posted, likely because Tyla released Water Remix with Travis Scott on Apple Music.",
        "Creators in South Africa posted, likely because Tyla won a Grammy for Water after Travis Scott presented it."),
    "I1 three KE politicians, two stories, undated": (
        "Creators in Kenya discussed President William Ruto, Raila Odinga and the ODM, likely because the ODM signed a "
        "cooperation deal with UDA.",
        "Creators in Kenya discussed President William Ruto, Raila Odinga and the ODM, likely because police clashed "
        "with protesters in Nairobi."),
    "I1 platform names carry an undated pair": (
        "Nigerian Twitter users argued on X Spaces and TikTok Live, likely because Davido released a new single.",
        "Nigerian Twitter users argued on X Spaces and TikTok Live, likely because Wizkid cancelled a concert."),
    "I1 platform names carry a dated pair": (
        "Nigerian Twitter users argued on X Spaces and TikTok Live on 11 Oct, likely because Davido released a single.",
        "Nigerian Twitter users argued on X Spaces and TikTok Live on 11 Oct, likely because Wizkid cancelled a show."),
    "I1 SA and FNB Stadium carry a pair": (
        "Creators in South Africa posted, likely because SA fans backed Bafana Bafana before the Nigeria qualifier at "
        "FNB Stadium on 11 Oct.",
        "Creators in South Africa posted, likely because SA fans mourned a stampede at FNB Stadium on 11 Oct."),
    "I1 Cape Town and Cape Town City": (
        "Creators in South Africa posted on 11 Oct, likely because Cape Town flooded and Table Mountain trails closed.",
        "Creators in South Africa posted on 11 Oct, likely because Cape Town City beat Kaizer Chiefs at Table Mountain "
        "view park."),
}


@pytest.mark.parametrize("pair", sorted(APART))
def test_distinct_events_that_share_their_posts_stay_apart(pair):
    a, b = APART[pair]
    assert not same_event.same_event(card(a), card(b))
    assert not same_event.same_event(card(b), card(a))


def test_an_empty_or_missing_explanation_is_no_event():
    assert not same_event.same_event(card(""), card(""))
    assert not same_event.same_event({"explanation": None, "posts": {"p1", "p2", "p3"}}, card(DATED_CITY))
