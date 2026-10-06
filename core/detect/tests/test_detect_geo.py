import pytest

from core.detect.geo import geo_for_post, is_known, is_local

UNKNOWN = (None, None, None)


def test_tiktok_ext_region_wins():
    assert geo_for_post("tiktok", "NG", ext_region="ng", text="Accra vibes") == ("NG", 0.9, "ext_region")
    assert geo_for_post("tiktok", "NG", ext_region="GH") == ("GH", 0.9, "ext_region")


def test_ext_region_counts_only_on_tiktok():
    assert geo_for_post("instagram", "NG", ext_region="NG") == UNKNOWN


def test_ext_region_must_be_alpha2():
    assert geo_for_post("tiktok", "KE", ext_region="Kenya") == UNKNOWN
    assert geo_for_post("tiktok", "KE", ext_region="", home_market="KE") == ("KE", 0.8, "home_market")


def test_creator_home_market_then_profile_location():
    assert geo_for_post("x", "KE", home_market="ke") == ("KE", 0.8, "home_market")
    assert geo_for_post("x", "KE", profile_location="Nairobi, Kenya") == ("KE", 0.8, "home_market")
    assert geo_for_post("x", "ZA", profile_location="Mzansi \U0001f1ff\U0001f1e6") == ("ZA", 0.8, "home_market")
    assert geo_for_post("x", "NG", profile_location="Accra, Ghana") == ("GH", 0.8, "home_market")


def test_creator_evidence_that_disagrees_gives_none():
    assert geo_for_post("x", "KE", home_market="KE", profile_location="Kampala", text="Nairobi") == UNKNOWN


def test_accra_mention_on_ng_post_points_to_ghana():
    geo = geo_for_post("instagram", "NG", text="Owambe for Accra this weekend, who dey come?", language="pcm")
    assert geo == ("GH", 0.7, "place_mention")
    assert is_known(geo[1])
    assert not is_local(geo[0], geo[1], "NG")


def test_dar_es_salaam_mention_on_ke_post_points_to_tanzania():
    geo = geo_for_post("tiktok", "KE", text="Tukutane Dar es Salaam kesho #daressalaam", language="sw")
    assert geo == ("TZ", 0.7, "place_mention")
    assert not is_local(geo[0], geo[1], "KE")


def test_pidgin_post_with_no_place_is_language_only_and_not_known():
    geo = geo_for_post("x", "NG", text="Wetin dey happen? Una no go believe am", language="pcm")
    assert geo == ("NG", 0.3, "language")
    assert not is_known(geo[1])
    assert not is_local(geo[0], geo[1], "NG")


def test_sheng_post():
    assert geo_for_post("tiktok", "KE", text="Niaje msee, form ni gani leo?", language="sheng") == ("KE", 0.3, "language")
    geo = geo_for_post("tiktok", "KE", text="Form ni gani Kanairo leo?", language="sheng")
    assert geo == ("KE", 0.7, "place_mention")
    assert is_local(geo[0], geo[1], "KE")


def test_isizulu_post_naming_durban():
    geo = geo_for_post("instagram", "ZA", text="Sihamba eThekwini, siyabonga Durban!", language="zu")
    assert geo == ("ZA", 0.7, "place_mention")
    assert is_local(geo[0], geo[1], "ZA")


def test_neighbour_leaks_into_za():
    assert geo_for_post("x", "ZA", text="Live from Harare tonight")[0] == "ZW"
    assert geo_for_post("x", "ZA", text="Maseru stand up")[0] == "LS"
    assert geo_for_post("x", "ZA", text="Gaborone vibes")[0] == "BW"


def test_neighbour_leaks_into_ng_and_ke():
    assert geo_for_post("x", "NG", text="Douala na my side")[0] == "CM"
    assert geo_for_post("x", "KE", text="Kampala nightlife")[0] == "UG"


def test_conflicting_mentions_give_none():
    assert geo_for_post("x", "NG", text="Lagos to Accra road trip", language="pcm") == UNKNOWN
    assert geo_for_post("x", "KE", text="Nairobi or Dar es Salaam?") == UNKNOWN


def test_country_name_alone_does_not_place_a_post():
    assert geo_for_post("x", "NG", text="Naija no dey carry last", language="pcm") == ("NG", 0.3, "language")
    assert geo_for_post("x", "NG", text="Jollof war with Ghana", language="pcm") == UNKNOWN
    assert geo_for_post("x", "NG", text="Nigeria vs Ghana jollof, Lagos wins") == UNKNOWN


def test_city_with_its_own_country_is_fine():
    assert geo_for_post("x", "KE", text="Nairobi, Kenya is buzzing") == ("KE", 0.7, "place_mention")


def test_blocklist_collisions_void_place_mentions():
    assert geo_for_post("x", "NG", text="Sunset at Lagos, Algarve, Portugal") == UNKNOWN
    assert geo_for_post("x", "NG", text="Sapa Vietnam trek then Hanoi, Lagos next") == UNKNOWN
    assert geo_for_post("x", "NG", text="Ankara Turkey trip, then Abuja") == UNKNOWN
    assert geo_for_post("x", "NG", text="Москва Lagos привет") == UNKNOWN
    assert geo_for_post("x", "NG", profile_location="Lagos, Portugal") == UNKNOWN


def test_names_that_collide_are_not_in_the_gazetteer():
    assert geo_for_post("x", "NG", text="Holiday in Benin") == UNKNOWN
    assert geo_for_post("x", "KE", text="Eastleigh, Hampshire") == UNKNOWN
    assert geo_for_post("x", "ZA", text="East London pubs") == UNKNOWN


def test_place_must_be_a_whole_word():
    assert geo_for_post("x", "NG", text="#lagosnights") == UNKNOWN
    assert geo_for_post("x", "NG", text="#Lagos nights") == ("NG", 0.7, "place_mention")
    assert geo_for_post("x", "ZA", text="#CapeTown sunsets") == ("ZA", 0.7, "place_mention")
    assert geo_for_post("x", "ZA", text="Jo’burg traffic") == ("ZA", 0.7, "place_mention")


def test_language_outside_the_market_is_unknown():
    assert geo_for_post("x", "ZA", text="hello", language="pcm") == UNKNOWN
    assert geo_for_post("x", "ZA", text="hello", language="en") == UNKNOWN
    assert geo_for_post("x", "ZA", language=["en", "zu"]) == ("ZA", 0.3, "language")
    assert geo_for_post("x", "ZA", language="zu-ZA") == ("ZA", 0.3, "language")


def test_nothing_gives_unknown():
    assert geo_for_post("youtube", "KE") == UNKNOWN


def test_market_must_be_one_of_three():
    with pytest.raises(ValueError):
        geo_for_post("x", "GH")
    assert geo_for_post("x", "ke", home_market="KE") == ("KE", 0.8, "home_market")


def test_raw_json_values_that_are_not_strings_read_as_absent():
    assert geo_for_post("x", "NG", profile_location={"city": "Accra"}, text="Lagos") == ("NG", 0.7, "place_mention")
    assert geo_for_post("x", "NG", profile_location=["Accra"]) == UNKNOWN
    assert geo_for_post("x", "NG", text=12345, language="pcm") == ("NG", 0.3, "language")
    assert geo_for_post("x", "NG", text={"body": "Lagos"}) == UNKNOWN
    assert geo_for_post("x", "NG", language=7) == UNKNOWN
    assert geo_for_post("x", "NG", language={"code": "pcm"}) == UNKNOWN
    assert geo_for_post("x", "NG", language=["pcm", 3]) == ("NG", 0.3, "language")
    assert geo_for_post("x", "NG", language=("yo",)) == ("NG", 0.3, "language")
    assert geo_for_post("tiktok", "NG", ext_region=566, home_market=None) == UNKNOWN


def test_home_market_accepts_a_country_name_or_alias():
    assert geo_for_post("x", "ZA", home_market="South Africa") == ("ZA", 0.8, "home_market")
    assert geo_for_post("x", "ZA", home_market=" mzansi ") == ("ZA", 0.8, "home_market")
    assert geo_for_post("x", "NG", home_market="Nigeria") == ("NG", 0.8, "home_market")
    assert geo_for_post("x", "NG", home_market="Naija") == ("NG", 0.8, "home_market")
    assert geo_for_post("x", "KE", home_market="Kenya") == ("KE", 0.8, "home_market")
    assert geo_for_post("x", "NG", home_market="Ghana") == ("GH", 0.8, "home_market")


def test_home_market_that_is_not_a_country_gives_unknown():
    assert geo_for_post("x", "ZA", home_market="Atlantis") == UNKNOWN
    assert geo_for_post("x", "ZA", home_market="Durban") == UNKNOWN
    assert geo_for_post("x", "ZA", home_market="") == UNKNOWN
    assert geo_for_post("x", "ZA", home_market=27) == UNKNOWN


def test_is_known_threshold():
    assert is_known(0.7)
    assert is_known(0.9)
    assert not is_known(0.69)
    assert not is_known(0.3)
    assert not is_known(None)


def test_is_local():
    assert is_local("KE", 0.8, "KE")
    assert is_local("KE", 0.8, "ke")
    assert not is_local("TZ", 0.8, "KE")
    assert not is_local("KE", 0.3, "KE")
    assert not is_local(None, None, "KE")
