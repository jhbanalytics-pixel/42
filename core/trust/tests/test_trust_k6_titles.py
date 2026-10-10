"""K6 and written-as-a-name titles, and two technical identifiers.

The strictness impact report (STRICTNESS-IMPACT.md, K6 section) found that most K6 holds the new term lists add to
real posts and repo text are false: Granny the horror game, Babu and Bibi as names, the Luo Council of Elders, APC
Elders, The Hospitality Pikin, the column genz_score and the storage precondition "a generation match". This pins
the narrow exemptions that clear those, and pins everything that must stay held beside them.

Four things are fixed here and not read from the code under test:
  1. the real strings that were false holds (verbatim, or the verbatim sentence around the hit) now pass;
  2. the real strings that were true catches, and the pet names, stay held;
  3. every exemption is only the written form: lower case, plurals, bare capitalised words, all capitals and a
     different word in the same slot stay held;
  4. nothing that breaches at a80be1d passes through an exemption. A fixed list of sentences that breach at a80be1d
     is joined to each exempt form on either side, and the K6 check must still hold it. The a80be1d term list is
     pinned by test_trust_seeds_input.py, so _breach_term here is the a80be1d answer."""

import pytest

from core.trust import claims

DASH = "\u2014"
APOS = "\u2019"

# Real strings that K6 held and should not have, from the report's sample.
FALSE_HOLDS = [
    # row 5: an institution title
    "Filling Raila's Political Shoes:  \n\nWinnie Odinga has been symbolically crowned by the Luo Council of Elders to "
    "take up the political mantle of her late father, Raila Odinga.",
    # row 6: a game title, with its own hashtag
    "Granny Live || Granny Horror Gameplay #granny #shortslive #shortsfeed #short #horror",
    # row 10: a persona name at the foot of a post
    f"There is still a Nigeria worth believing in.\n\n NAIJA CALLS FOR HEALING.\n\n{DASH} Gidiyke\nThe Hospitality "
    "Pikin\n\n#NaijaCallsForHealing #Nigeria #HealingNigeria #HopeForNigeria #TheHospitalityPikin",
    # row 17: a party title
    "Ondo APC Elders Urge Nigerians To Endure Tinubu's Reforms",
    # rows 23, 24, 25, 29, 30: a storage precondition
    "that intelligence-42-orchestration can replace a slot control object and the collection day index under a "
    "generation match; that it cannot delete or replace any other object under 42/daily/",
    "derived: Ask admission replaces the allowance ledger object under a generation match as the serving identity "
    "through the retained custom role QuestionControlReplace",
    "derived: the daily account replaces exactly two kinds of object under a generation match, its slot control "
    "objects (42/daily/slots/<operation>/control.json, daily_store.py)",
    "Pointers are created once under a generation match of zero and never replaced, so\n    a new grant for the same "
    "image is a new pointer at the next number.",
    "and creates two objects in `ogilvy-trends-v2-execution-approvals-staging` under a generation match of zero: "
    "the grant object at `42/daily/grants/<grant digest>.json`",
    # rows 26, 27, 31, 35: a column name
    "| enriched_content | partition DATE(collected_at), no expiry; raw_content expires 90d (raw_content.sql:37) | "
    "No trend_date column, no channel_family column; carries source :4, platform :5, published_at, genz_score :25, "
    "slang_score :29, query_term :9 |",
    "- enriched_content to posts, post_items and post_observations with lane_class='legacy': recompute ids, rerun "
    "geographic scope, leave tier NULL, drop genz_score and search velocity. Re-embed only the last 90 days.",
    "The daily source read pulls only `id, market, platform, source, title, text, slang_terms, topic_groups, "
    "genz_score, slang_score, published_at, collected_at` from the day's enriched_content partition",
    "- `genz_score`, `slang_score`, `regional_score` FLOAT64",
]

# Real strings and report probes that are true catches and stay held, with the term each one is held for.
TRUE_CATCHES = [
    ("giving the next generation something worth inheriting.", "next generation"),
    (f"Her Grandpa{APOS}s Lessons Got Her in Trouble", "Grandpa"),
    ("I Made My Grandma Immortal in Minecraft", "Grandma"),
    ("I love my grandma the most #movie #filmcommentary #foryou #usa #fyp", "grandma"),
    ("You can't tell a Kenyan adult anything without them pulling the \"respect your elders\" card.", "elders"),
    (f"Part 2 | I don{APOS}t want to be a grandfather yet", "grandfather"),
    ("Can their 50+ grandma ever?", "grandma"),
    ("since the baby was staying with the grandmother.", "grandmother"),
    ("Today, the babies in heaven are celebrating Nigeria", "babies"),
    ("- born free generation", "born free"),
    ("infant", "infant"),
    ("school leavers", "school leavers"),
    ("gogo", "gogo"),
    ("#genzrevolution", "#genz"),
    ("#genzprotest", "#genz"),
    ("Babu, Kenya's favourite grandpa, dances", "grandpa"),
]

# Not cleared on purpose. Pet names are the owner's call and the two name rows need the all capitals and bare
# capitalised rules relaxed, which this change does not do.
STILL_HELD_UNTOUCHED = [
    "I become a talkative when hyping my babies lol it" + APOS + "s mother behavior",
    "My babies Olori Ebi #bigbrotherwinner #tram #oyinpeppers #bbnaijaseason11",
    "I" + APOS + "m proud of my babies !!",
    "hyper-realistic AI babies speaking in Nigerian accents and Pidgin",
    "Ikhehla lami",
    "#granny",
    "'Babu may you end up being the best governor Nairobi has ever had, Sifuna may you end up being the best "
    "president Kenya has ever had' Richard Onyonka",
    "EPE AFTER DARK - MAURICE SAM, PEARL WATS, BIBI SONYE, Latest 2026 Nigerian Movie",
    "female, and in my early 20s",
]

# Sentences that breach at a80be1d. _breach_term is the pinned a80be1d list.
A80_BREACHES = [
    "Gen Z drove it", "genz fans", "generation z", "gen alpha", "Gen X", "millennials posted", "boomers reacted",
    "teens posted it", "teenagers shared it", "kids loved it", "children danced", "youths marched",
    "young people shared it", "younger women led it", "youngest viewers", "adolescents", "pensioners queued",
    "the elderly queued", "seniors queued", "senior citizens", "aged 18", "18-24 year olds", "18-24 yo",
    "25 year olds", "under 30s", "twentysomethings", "their thirties", "skews young", "mostly women",
    "gender split", "LSM 7", "income brackets", "google trends", "search volume", "search interest",
    "learners", "youth", "school-going kids", "zoomers", "gen-z", "gen zers", "generation alpha", "teen", "kid",
]

# Every exempt form, one string each. Each must pass on its own.
EXEMPT_FORMS = [
    "the Luo Council of Elders",
    "Ondo APC Elders",
    "Granny Horror Gameplay",
    "The Hospitality Pikin",
    "the column genz_score",
    "under a generation match",
]


@pytest.mark.parametrize("text", FALSE_HOLDS)
def test_a_real_false_hold_now_passes(text):
    assert claims._k6_term(text, set()) is None, text


@pytest.mark.parametrize("text, term", TRUE_CATCHES)
def test_a_true_catch_stays_held(text, term):
    assert claims._k6_term(text, set()) == term, text


@pytest.mark.parametrize("text", STILL_HELD_UNTOUCHED)
def test_a_pet_name_or_bare_name_is_still_held(text):
    assert claims._k6_term(text, set()), text


@pytest.mark.parametrize("text", EXEMPT_FORMS)
def test_each_exempt_form_passes_on_its_own(text):
    assert claims._k6_term(text, set()) is None, text


# Council of Elders and <ACRONYM> Elders, written as a title.
ELDERS_HELD = [
    "elders", "Elders", "the elders agreed", "the Elders agreed", "council of elders", "Council of elders",
    "Council of ELDERS", "the council of Elders", "Councils of Elders", "Council Elders", "Council and Elders",
    "APC elders", "APC ELDERS", "apc Elders", "A Elders", "APCDEFG Elders", "Village Elders", "Church Elders",
    "Kikuyu Elders", "THE Elders", "OUR Elders", "ALL Elders", "OLD Elders", "elder", "Elder", "an Elder",
    "elder people", "elder women", "APC Elder", "APC Elders' wives and the grandmothers", "The Hospitality Elder",
    "Elder Mavuso", "The Hospitality Elders",
]


@pytest.mark.parametrize("text", ELDERS_HELD)
def test_elders_stay_held_unless_written_as_a_title(text):
    assert claims._k6_term(text, set()), text


ELDERS_TITLES = [
    "Council of Elders", "the Luo Council of Elders", "APC Elders", "Ondo APC Elders Urge Calm", "the ODM Elders",
    "NEC Elders spoke", "a Council of Elders sat",
]


@pytest.mark.parametrize("text", ELDERS_TITLES)
def test_a_title_built_on_elders_passes(text):
    assert claims._k6_term(text, set()) is None, text


def test_a_title_does_not_shield_another_age_term_beside_it():
    assert claims._k6_term("Council of Elders and the teens", set()) == "teens"
    assert claims._k6_term("APC Elders and grandmothers", set()) == "grandmothers"
    assert claims._k6_term("the Council of Elders, elders, queued", set()) == "elders"
    assert claims._k6_term("the elders and the Council of Elders", set()) == "elders"


# Granny as a name, and a hashtag of a name that is read as a name in the same text.
GRANNY_NAMES = [
    "Granny Horror Gameplay", "Granny Live", "Granny Smith apples", "Granny Live #granny",
    "Granny Horror Gameplay #granny #horror", "Hon. Granny Mavuso spoke", "Raila, Granny and Sifuna joined",
]
GRANNY_HELD = [
    "granny", "Granny", "my granny", "Granny knitted a scarf", "Granny, knitting", "Grannies", "Grannies Live",
    "GRANNY HORROR GAMEPLAY", "granny Horror", "#granny", "Granny #granny", "granny #granny Live",
    "Granny Live and the grannies", "Watch Granny Live Today Here", "Granny Live and my granny",
    "Granny Live granny", "Granny Live #grannies", "Granny Live #GRANNY", "Granny #granny Live granny",
]


@pytest.mark.parametrize("text", GRANNY_NAMES)
def test_granny_written_as_a_name_passes(text):
    assert claims._k6_term(text, set()) is None, text


@pytest.mark.parametrize("text", GRANNY_HELD)
def test_granny_not_written_as_a_name_is_held(text):
    assert claims._k6_term(text, set()), text


# The Hospitality Pikin: the, one capitalised word, then the kin word capitalised.
PIKIN_HELD = [
    "pikin", "Pikin", "the pikin", "The Pikin", "pikins", "Pikins", "The Hospitality Pikins", "The Hospitality pikin",
    "the Hospitality Pikin", "A Hospitality Pikin", "The hospitality Pikin", "The Little Pikin", "The Young Pikin",
    "The Old Pikin", "The Baby Pikin", "The Hospitality Pikin and the pikins",
    "The Hospitality Pikin Pikin",
]


@pytest.mark.parametrize("text", PIKIN_HELD)
def test_pikin_not_written_as_a_title_is_held(text):
    assert claims._k6_term(text, set()), text


def test_the_other_kin_names_keep_their_old_behaviour():
    for text in ("Babu Owino said", "Bibi Titi Mohamed", "Koko Rapapa", "Raila, Babu and Sifuna joined the rally"):
        assert claims._k6_term(text, set()) is None, text
    for text in ("Gogos queued for grants", "Babu becomes governor", "Bibi says no to ceasefire", "bibi", "BABU"):
        assert claims._k6_term(text, set()), text


# The identifier genz_score (and genz_markers): lower case, the whole identifier, nothing around it.
IDENT_HELD = [
    "genz_audience", "genz_", "genz_fans", "genz_scores", "genz_scorecard", "genz_score_x", "genz_marker",
    "GenZ_score", "GENZ_SCORE", "Genz_score", "gen_z_score", "gen-z_score", "generation_z_score", "Gen_Z_voters",
    "genzScore", "genz2025", "genz_2025", "#genz_score", "@genz_score", "genz_score and teens", "genz-score",
]
IDENT_PASS = ["genz_score", "genz_markers", "avg_genz_score", "the genz_score column", "`genz_score`, `slang_score`"]


@pytest.mark.parametrize("text", IDENT_HELD)
def test_a_genz_form_that_is_not_the_identifier_is_held(text):
    assert claims._k6_term(text, set()), text


@pytest.mark.parametrize("text", IDENT_PASS)
def test_the_genz_identifiers_pass(text):
    assert claims._k6_term(text, set()) is None, text


# "a generation match": a storage precondition, and only that.
GENERATION_HELD = [
    "a generation", "a new generation", "the next generation match", "this generation match",
    "next generation match", "a generation matches", "a generation matched", "a generation matching",
    "a generation match-up", "a generation apart", "a generation z match",
    "a generation match and the next generation", "Gen Z generation match",
]
GENERATION_PASS = [
    "a generation match", "under a generation match", "under a generation match of zero", "A generation match.",
    "a generation  match", "a generation\nmatch", "a-generation match", "ifGenerationMatch 0",
]


@pytest.mark.parametrize("text", GENERATION_HELD)
def test_a_generation_that_is_not_a_storage_match_is_held(text):
    assert claims._k6_term(text, set()), text


@pytest.mark.parametrize("text", GENERATION_PASS)
def test_a_generation_match_passes(text):
    assert claims._k6_term(text, set()) is None, text


# Invariant: nothing that breaches at a80be1d passes through an exemption.
@pytest.mark.parametrize("breach", A80_BREACHES)
def test_the_a80_breach_list_is_what_it_says(breach):
    assert claims._breach_term(breach, set()), breach


@pytest.mark.parametrize("form", EXEMPT_FORMS)
@pytest.mark.parametrize("breach", A80_BREACHES)
def test_an_a80_breach_beside_an_exempt_form_is_still_held(form, breach):
    for text in (f"{form} {breach}", f"{breach} {form}", f"{form}, {breach}.", f"{breach}: {form}",
                 f"{form} {DASH} {breach} {DASH} {form}"):
        assert claims._k6_term(text, set()), text


def test_no_string_in_the_fixed_corpus_passes_k6_that_breaches_at_a80be1d():
    corpus = A80_BREACHES + [t for t, _ in TRUE_CATCHES] + [f for f in FALSE_HOLDS]
    for text in corpus:
        if claims._breach_term(text, set()):
            assert claims._k6_term(text, set()), text


def test_the_a80_list_is_untouched_by_the_exemptions():
    for text in ("Council of Elders", "APC Elders", "Granny Live", "The Hospitality Pikin", "genz_score",
                 "under a generation match", "grandma", "granny"):
        assert claims._breach_term(text, set()) is None, text
