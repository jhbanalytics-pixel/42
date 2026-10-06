"""Unit tests for src/utils/geo_blocklist.py.

The data + helpers were extracted from src/analysis/generate_briefs.py on
28 May 2026 so the topic classifier and the brief generator share a
single source of truth. Existing tests in test_generate_briefs.py
continue to import the legacy private names; this module covers the
new text-based API used by the classifier.
"""

from __future__ import annotations

import pytest
from src.utils.geo_blocklist import (
    FOREIGN_LATIN_CHARSETS,
    NON_SSA_SCRIPT_RANGES,
    TOPIC_GEO_BLOCKLIST,
    has_foreign_latin_density,
    has_non_ssa_script,
    normalize_haystack_for_geo_match,
    row_matches_geo_blocklist,
    text_matches_geo_blocklist,
)


def test_topic_geo_blocklist_carries_expected_topics():
    """Both known-collision topics are populated. Guard against accidental
    deletion of the entire blocklist in a future refactor.
    """
    assert "economy_sapa_hustle" in TOPIC_GEO_BLOCKLIST
    assert "fashion_ankara_asoebi" in TOPIC_GEO_BLOCKLIST
    assert len(TOPIC_GEO_BLOCKLIST["economy_sapa_hustle"]) >= 50
    assert len(TOPIC_GEO_BLOCKLIST["fashion_ankara_asoebi"]) >= 40


def test_text_matches_returns_false_for_topic_not_in_blocklist():
    """Topics outside the blocklist short-circuit to False regardless of text."""
    assert text_matches_geo_blocklist("vietnam sapa tour", "music_amapiano") is False
    assert text_matches_geo_blocklist("ankara turkey", "sports_football") is False


def test_text_matches_keyword_layer_sapa_vietnam():
    """28 May 2026 SUSPECT row. Bare text with 'Sapa Vietnam' must flip
    economy_sapa_hustle on.
    """
    haystack = (
        "Travel experiences and tourism destinations including Sapa Vietnam, "
        "scenic landscapes, mountains, and tour packages"
    )
    assert text_matches_geo_blocklist(haystack, "economy_sapa_hustle") is True


def test_text_matches_keyword_layer_ankara_turkey():
    """Ankara, Turkey content flips fashion_ankara_asoebi on."""
    haystack = "Visiting Ankara, Turkey this weekend, mogan was beautiful"
    assert text_matches_geo_blocklist(haystack, "fashion_ankara_asoebi") is True


def test_text_matches_does_not_false_positive_on_real_naija_sapa():
    """Pure NG pidgin 'sapa' (broke) without Vietnam / India / etc. markers
    must NOT trigger the filter. Critical guard against over-stripping.
    """
    haystack = "Sapa don catch me again, no money this month, hustle dey scatter"
    assert text_matches_geo_blocklist(haystack, "economy_sapa_hustle") is False


def test_text_matches_does_not_false_positive_on_real_naija_ankara():
    """Pure NG fabric 'ankara' without Turkey markers must NOT trigger."""
    haystack = "My new ankara dress with custom gele and aso ebi"
    assert text_matches_geo_blocklist(haystack, "fashion_ankara_asoebi") is False


def test_round11_8jun_ankara_turkey_leaks_now_caught():
    """8 Jun 2026: six Turkey-tourism refs shipped into the SENT NG ankara card
    because the list had only the umlaut 'türkiye' and the data uses ASCII.
    Each reported leak must now flip the filter on.
    """
    leaks = [
        "back to last year #ankara #fyp #travel #turkiye",
        "Hacıbayram'dan Ankara Kalesi",  # noqa: RUF001  (dotless-i in data)
        "6 month update 3600 grafts Ankara Dr. Civas clinic",
        "Mutlu Lokantasi Ankara good food",
        "Magaza Adresi Profilde #ankara #streetwear #gezilecekyerler",
    ]
    for h in leaks:
        assert text_matches_geo_blocklist(h, "fashion_ankara_asoebi") is True, h


def test_round11_ascii_turkiye_was_the_gap():
    """The exact root cause: ASCII 'turkiye' (no umlaut) was missing."""
    assert text_matches_geo_blocklist("loving turkiye vibes", "fashion_ankara_asoebi") is True


def test_tbt_removed_so_ng_throwback_fashion_survives():
    """16 Jun 2026: '#tbt' (Throwback Thursday) is a global hashtag used heavily
    on NG asoebi/fashion throwback posts, so the 3-char substring entry stripped
    legitimate fashion_ankara_asoebi rows. The entry is gone; a bare NG fashion
    throwback now survives, while real Turkey contamination still trips.
    """
    assert "tbt" not in TOPIC_GEO_BLOCKLIST["fashion_ankara_asoebi"]
    survives = "#tbt my asoebi look from last owambe, ankara gele and lace"
    assert text_matches_geo_blocklist(survives, "fashion_ankara_asoebi") is False
    # A #tbt post that IS Turkey-the-capital still gets caught by the place markers.
    leak = "#tbt throwback to ankara turkey, istanbul was unreal"
    assert text_matches_geo_blocklist(leak, "fashion_ankara_asoebi") is True


def test_round11_does_not_over_block_real_asoebi():
    """The round-11 Turkish terms must not strip legitimate NG fashion content."""
    legit = [
        "owambe asoebi gele lace ankara fabric for the wedding",
        "my tailor made this ankara two-piece, asoebi bella vibes",
    ]
    for h in legit:
        assert text_matches_geo_blocklist(h, "fashion_ankara_asoebi") is False, h


def test_round10_sapa_foreign_news_sources_caught():
    """GDELT Indian + Turkish news-source domains on the sapa tag now stripped."""
    assert text_matches_geo_blocklist("report via businesstoday.in", "economy_sapa_hustle") is True
    assert text_matches_geo_blocklist("sozcu.com.tr haberler", "economy_sapa_hustle") is True


def test_text_matches_devanagari_script_layer():
    """5+ Devanagari characters trigger the non-SSA-script backstop."""
    haystack = "समाजवादी पार्टी political content"  # 11 Devanagari chars
    assert text_matches_geo_blocklist(haystack, "economy_sapa_hustle") is True


def test_text_matches_turkish_latin_density_layer():
    """Turkish-distinctive characters trigger the Latin-density backstop."""
    # Ambiguous-unicode warnings (RUF001) suppressed per-line because the
    # Turkish characters are the actual input the filter is meant to catch.
    haystack = "Şehirde gölbaşı Ankara'ya İçişleri"  # noqa: RUF001
    assert text_matches_geo_blocklist(haystack, "fashion_ankara_asoebi") is True


def test_text_matches_vietnamese_latin_density_layer():
    """Vietnamese-distinctive characters trigger the Latin-density backstop."""
    # Vietnamese diacritics are the test target; RUF001 noqa is intentional.
    haystack = "Đến Sapa du lịch với đăng ký ơn"
    assert text_matches_geo_blocklist(haystack, "economy_sapa_hustle") is True


def test_normalize_haystack_folds_smart_quotes():
    """Smart-quotes fold to ASCII so blocklist matches survive iOS captions."""
    # U+2019 right single quotation mark is the test target; RUF001 noqa
    # is intentional, the whole point of this test is the smart-quote fold.
    raw = "Ankara’da haftasonu"  # noqa: RUF001
    normalised = normalize_haystack_for_geo_match(raw)
    assert "ankara'da" in normalised


def test_has_non_ssa_script_threshold():
    """Below threshold returns False; above returns True."""
    # 3 Cyrillic chars (да!) below 5-char threshold
    assert has_non_ssa_script("да! hello world", threshold=5) is False
    # 6 Cyrillic chars above threshold (приве т = 6 chars)
    assert has_non_ssa_script("привет hello", threshold=5) is True


def test_has_foreign_latin_density_returns_language():
    """Returns the matching language name or None."""
    # Turkish + Vietnamese diacritics are intentional test inputs.
    assert has_foreign_latin_density("şehirde gölbaşı İçişleri") == "turkish"  # noqa: RUF001
    assert has_foreign_latin_density("đến Đăng ơ ư") == "vietnamese"
    assert has_foreign_latin_density("plain english text") is None


def test_row_matches_wraps_text_helper():
    """row_matches_geo_blocklist is a thin wrapper around text_matches.

    Passing the same content as a row dict produces the same answer as
    passing the joined text directly.
    """
    row = {
        "title": "Travel",
        "text": "Sapa Vietnam tourism",
        "url": "",
    }
    assert row_matches_geo_blocklist(row, "economy_sapa_hustle") is True

    row_clean = {"title": "Sapa hustle", "text": "no money this month", "url": ""}
    assert row_matches_geo_blocklist(row_clean, "economy_sapa_hustle") is False


def test_foreign_latin_charsets_metadata():
    """Sanity: each charset entry is (language, chars, threshold)."""
    for entry in FOREIGN_LATIN_CHARSETS:
        assert len(entry) == 3
        lang, chars, threshold = entry
        assert isinstance(lang, str)
        assert lang
        assert isinstance(chars, str)
        assert chars
        assert isinstance(threshold, int)
        assert threshold > 0


def test_non_ssa_script_ranges_sorted_and_valid():
    """Sanity: each range is a (low, high) tuple with low < high."""
    for lo, hi in NON_SSA_SCRIPT_RANGES:
        assert isinstance(lo, int)
        assert isinstance(hi, int)
        assert lo < hi


# Round-10 (30 May 2026): foreign-Ankara homograph leak into NG fashion.
# Two rows reached the 2026-05-30 digest's NG fashion_ankara_asoebi card
# references. Both are plain ASCII so the Turkish-diacritic density backstop
# never fired and the Turkey place list did not cover them.


def test_fashion_ankara_blocks_nevada_karanfil_hashtags():
    text = "#nevada #ankara #karanfil"
    assert text_matches_geo_blocklist(text, "fashion_ankara_asoebi") is True


def test_fashion_ankara_blocks_turkish_passport_rv_post():
    text = (
        "[r/AskTurkey] Hi everyone, my friend and I are planning to drive an "
        "RV from Dubai to Ankara. I have a Turkish passport and my friend has "
        "an Azerbaijani passport"
    )
    assert text_matches_geo_blocklist(text, "fashion_ankara_asoebi") is True


@pytest.mark.parametrize(
    "text",
    [
        "Ankara gown styles for owambe in Lagos, see my tailor",
        "Beautiful asoebi and gele for the wedding, ankara fabric slay",
        "Owambe season, my ankara print is giving, Lagos party loading",
    ],
)
def test_fashion_ankara_keeps_legit_ng_fabric_rows(text):
    # The new foreign anchors must not strip genuine NG fabric content.
    assert text_matches_geo_blocklist(text, "fashion_ankara_asoebi") is False


def test_blocklist_entries_are_all_lowercase():
    # The haystack is lowercased before matching, so any entry with an
    # uppercase character can never match. Normalising every entry at module
    # load keeps the matcher case-robust.
    for topic, markers in TOPIC_GEO_BLOCKLIST.items():
        for marker in markers:
            assert marker == marker.lower(), f"uppercase blocker {marker!r} under {topic}"


def test_lgbt_plus_marker_now_matches():
    # "LGBT+" was dead under economy_sapa_hustle because the haystack is
    # lowercased; the module-load normalisation makes it match real text.
    assert "lgbt+" in TOPIC_GEO_BLOCKLIST["economy_sapa_hustle"]
    assert text_matches_geo_blocklist("sapa lgbt+ pride post", "economy_sapa_hustle") is True
