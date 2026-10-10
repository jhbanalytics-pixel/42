import hashlib

import pytest

from core.detect.items import KINDS, canonical_key, is_generic, item_id, items_for_post


def test_hashtag_key_folds_hash_case_and_width():
    assert canonical_key("hashtag", "#Amapiano") == "amapiano"
    assert canonical_key("hashtag", "amapiano") == "amapiano"
    assert canonical_key("hashtag", "＃Ａｍａｐｉａｎｏ") == "amapiano"
    assert canonical_key("hashtag", "  #AMAPIANO ") == "amapiano"


def test_item_id_is_stable_across_hashtag_spellings():
    spellings = ["#Amapiano", "amapiano", "＃Ａｍａｐｉａｎｏ", "#AMAPIANO"]
    ids = {item_id("hashtag", canonical_key("hashtag", s)) for s in spellings}
    assert len(ids) == 1


def test_item_id_is_sha256_of_kind_and_key():
    expected = hashlib.sha256(b"hashtag|amapiano").hexdigest()
    assert item_id("hashtag", "amapiano") == expected
    assert len(expected) == 64


def test_item_id_differs_by_kind():
    assert item_id("hashtag", "amapiano") != item_id("topic", "amapiano")


def test_sound_and_creator_keys_carry_platform():
    assert canonical_key("sound", "7301234567890", platform="tiktok") == "tiktok:7301234567890"
    assert canonical_key("sound", 7301234567890, platform="TikTok") == "tiktok:7301234567890"
    assert canonical_key("creator", "@DJ_Maphorisa", platform="instagram") == "instagram:dj_maphorisa"
    assert canonical_key("creator", "dj_maphorisa", platform="instagram") == "instagram:dj_maphorisa"


def test_sound_and_creator_need_a_platform():
    with pytest.raises(ValueError):
        canonical_key("sound", "123")
    with pytest.raises(ValueError):
        canonical_key("creator", "@someone")


def test_free_text_kinds_collapse_whitespace():
    for kind in ("topic", "meme", "brand", "event", "format"):
        assert canonical_key(kind, "  Durban   July\n") == "durban july"


def test_unknown_kind_and_empty_raw_are_refused():
    with pytest.raises(ValueError):
        canonical_key("slang", "x")
    with pytest.raises(ValueError):
        item_id("slang", "x")
    with pytest.raises(ValueError):
        canonical_key("hashtag", "#")
    with pytest.raises(ValueError):
        canonical_key("topic", "   ")


def test_kinds_match_cultural_map():
    assert set(KINDS) == {"topic", "hashtag", "sound", "format", "meme", "creator", "brand", "event"}


def test_items_for_post_builds_one_row_per_item():
    post = {
        "platform": "tiktok",
        "hashtags": ["#Amapiano", "amapiano", "DurbanJuly"],
        "sound_id": "7301234567890",
        "creator_id": "@Kabza",
    }
    rows = items_for_post(post)
    assert [(r["kind"], r["canonical_key"], r["via"]) for r in rows] == [
        ("hashtag", "amapiano", "hashtag"),
        ("hashtag", "durbanjuly", "hashtag"),
        ("sound", "tiktok:7301234567890", "sound"),
        ("creator", "tiktok:kabza", "creator"),
    ]
    for r in rows:
        assert r["item_id"] == item_id(r["kind"], r["canonical_key"])
        assert set(r) == {"item_id", "kind", "canonical_key", "label", "via"}
    assert rows[0]["label"] == "#Amapiano"
    assert rows[1]["label"] == "#DurbanJuly"


def test_items_for_post_skips_empty_and_junk_hashtags():
    post = {
        "platform": "instagram",
        "hashtags": ["", "#", "   ", None, "##", "#!!!", "#two words", "#" + "a" * 120, "#Lagos"],
        "sound_id": None,
        "creator_id": "",
    }
    rows = items_for_post(post)
    assert [r["canonical_key"] for r in rows] == ["lagos"]


def test_items_for_post_without_platform_keeps_only_hashtags():
    rows = items_for_post({"hashtags": ["#Sheng"], "sound_id": "1", "creator_id": "x"})
    assert [r["kind"] for r in rows] == ["hashtag"]


def test_items_for_post_handles_missing_fields():
    assert items_for_post({"platform": "x"}) == []
    assert items_for_post({"platform": "x", "hashtags": None}) == []


# The platform-generic stoplist (core/detect/stoplist.yaml)


@pytest.mark.parametrize("raw", ["#fyp", "FYP", "#FYP", "＃ＦＹＰ", "  #ForYou ", "#foryoupage", "#Viral",
                                 "#trending", "#Shorts", "#reels", "#Explore", "#funny", "#Comedy", "#TikTok",
                                 "#CapCut", "#duet", "#fypシ", "#fypｼ"])
def test_is_generic_for_stoplisted_hashtags_after_canonicalisation(raw):
    assert is_generic("hashtag", canonical_key("hashtag", raw))


def test_is_generic_folds_a_raw_spelling_too():
    assert is_generic("hashtag", "＃ＦｏｒＹｏｕＰａｇｅ")


@pytest.mark.parametrize("raw", ["#amapiano", "#Sheng", "#japa", "#fypamapiano", "#lagos", "#fyb"])
def test_is_generic_false_for_real_hashtags(raw):
    assert not is_generic("hashtag", canonical_key("hashtag", raw))


def test_is_generic_only_applies_to_hashtags():
    assert not is_generic("meme", "fyp")
    assert not is_generic("topic", "viral")
    assert not is_generic("creator", canonical_key("creator", "fyp", "tiktok"))


def test_stoplist_file_is_canonical_and_holds_only_hashtags():
    import yaml

    from core.detect import items

    raw = yaml.safe_load(items.STOPLIST_PATH.read_text(encoding="utf-8"))
    assert set(raw) == {"hashtag"}
    terms = raw["hashtag"]
    assert len(terms) == len(set(terms))
    assert all(canonical_key("hashtag", t) == t for t in terms)
    for t in ("fyp", "foryou", "foryoupage", "viral", "trending", "shorts", "reels", "explore", "funny", "comedy",
              "tiktok", "capcut", "duet", "fypシ"):
        assert t in terms


# Elongated and zero-width variants of a stoplisted tag are still that tag (N11). The stoplist is a set of exact
# keys, so a tag stretched by repeated letters or carrying an invisible suffix must be matched by its pattern.


@pytest.mark.parametrize("raw", ["#fyppppppppppppppppppppppp", "#fypppppp", "#fyyyyp", "#foryouuuuu", "#viraaaal",
                                 "#fyp\u200b", "#fy\u200bp", "#foryou\u200d", "#fyp\ufe0f", "#FYPPPPPP",
                                 "#fypシ\u200b", "#trendinggg",
                                 "#reeeeels", "#funnnnny", "#foryoupageofficialll", "#reeels", "#commmmedy",
                                 "#memmmes", "#sh​orrrrts"])
def test_is_generic_matches_elongated_and_zero_width_variants_of_a_stoplisted_tag(raw):
    assert is_generic("hashtag", canonical_key("hashtag", raw))


@pytest.mark.parametrize("raw", ["#fypamapiano", "#fyb", "#fypaa", "#foryoupageamapiano", "#goooal", "#viralnigeria",
                                 "#coool", "#lagoss", "#a\u200bb", "#lagosss", "#amapianooo", "#gooooal",
                                 "#fyypp", "#reelss", "#funnyy", "#viraal"])
def test_is_generic_leaves_a_real_tag_alone_when_pattern_matching(raw):
    assert not is_generic("hashtag", canonical_key("hashtag", raw))


def test_pattern_matching_never_changes_the_item_identity():
    # Only is_generic reads through the pattern; the key and the item_id of a variant stay its own.
    stretched = canonical_key("hashtag", "#fyppppp")
    assert stretched == "fyppppp"
    assert item_id("hashtag", stretched) != item_id("hashtag", "fyp")
