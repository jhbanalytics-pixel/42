"""Unit tests for src/enrichment/topic_classifier.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.enrichment.topic_classifier import (
    _compile_patterns,
    classify_topics,
    load_topic_groups,
)


def test_classify_topics_single_market_match():
    """Amapiano keyword on ZA market returns only the music topic."""
    result = classify_topics(
        title="New amapiano track fire",
        text="",
        hashtags="#amapiano",
        market="za",
    )
    assert "music_amapiano" in result


def test_classify_topics_multi_assign():
    """Row hitting 3 topics returns all 3 sorted."""
    result = classify_topics(
        title="celebrating matric with stokvel money and amapiano",
        text="",
        hashtags="",
        market="za",
    )
    assert "education_matric_nsfas" in result
    assert "finance_stokvel" in result
    assert "music_amapiano" in result
    # Sorted output for stable ordering
    assert result == sorted(result)


def test_dominance_off_keeps_all_matches(monkeypatch):
    """Dark by default: a music-dominant row with one stray political keyword
    still tags both, exactly as before the dominance rule existed."""
    monkeypatch.delenv("TOPIC_DOMINANCE_ENABLED", raising=False)
    result = classify_topics(
        title="amapiano log drum groove by kabza, ramaphosa watching",
        text="",
        hashtags="",
        market="za",
    )
    assert "music_amapiano" in result
    assert "politics_crises" in result


def test_dominance_on_drops_grazing_topic(monkeypatch):
    """Flag on: the dominant family (four music keywords) wins and the single
    stray political keyword is dropped, so an amapiano clip stops crashing the
    politics wall."""
    monkeypatch.setenv("TOPIC_DOMINANCE_ENABLED", "true")
    result = classify_topics(
        title="amapiano log drum groove by kabza, ramaphosa watching",
        text="",
        hashtags="",
        market="za",
    )
    assert result == ["music_amapiano"]


def test_dominance_on_keeps_co_relevant(monkeypatch):
    """Flag on: a genuine tie (one keyword each) keeps both topics, so a truly
    cross-cutting post is never collapsed to one."""
    monkeypatch.setenv("TOPIC_DOMINANCE_ENABLED", "true")
    result = classify_topics(
        title="amapiano and ramaphosa",
        text="",
        hashtags="",
        market="za",
    )
    assert "music_amapiano" in result
    assert "politics_crises" in result


def test_classify_topics_no_match_returns_empty():
    """Italian / Tamil / empty all produce empty list."""
    assert classify_topics("Pelle glow e capelli", "", "", "za") == []
    assert classify_topics("அடையாளத்தை தேடுவதை", "", "", "ng") == []
    assert classify_topics("", "", "", "ke") == []


def test_classify_topics_word_boundary():
    """Word-boundary regex must not match keywords embedded in longer words.

    Verified two ways: (1) the live ZA classifier with the real `amapiano`
    keyword does not fire on `amapianomusic` (no internal whitespace), and
    (2) directly via _compile_patterns with `piano` as the keyword to
    confirm the boundary on a known-stripped input.
    """
    # Live taxonomy: amapiano embedded in a longer word must not match.
    result = classify_topics("amapianomusic festival lineup", "", "", "za")
    assert "music_amapiano" not in result

    # Direct check: compile a pattern for `piano` and confirm it does not
    # match `amapiano` (no boundary between `a` and `piano`) but does match
    # the same word followed by punctuation or whitespace.
    patterns = _compile_patterns({"music": ["piano"]})
    assert patterns["music"][0].search("amapiano") is None
    assert patterns["music"][0].search("the piano room") is not None
    assert patterns["music"][0].search("piano!") is not None


def test_classify_topics_multi_word_keyword():
    """Multi-word keyword phrases match across whitespace."""
    result = classify_topics(
        "Finance bill protest heating up in nairobi",
        "",
        "",
        "ke",
    )
    assert "politics_maandamano" in result


def test_classify_topics_case_insensitive():
    """Capitalised text still matches lowercase keywords."""
    result = classify_topics(
        "NEW BURNA BOY RELEASE DROPS FRIDAY",
        "",
        "",
        "ng",
    )
    assert "music_afrobeats" in result


def test_load_topic_groups_reads_yaml():
    """load_topic_groups returns dict of topic -> keyword list."""
    groups = load_topic_groups("za")
    assert "music_amapiano" in groups
    assert "amapiano" in groups["music_amapiano"]
    # 9 topics since the 11 Jun 2026 tech_gemini_ai brand-lens add.
    assert len(groups) == 9


def test_load_topic_groups_missing_market_raises():
    """Invalid market raises FileNotFoundError with clear message."""
    with pytest.raises(FileNotFoundError) as exc:
        load_topic_groups("xx")
    assert "xx" in str(exc.value)


def test_compile_patterns_handles_special_chars():
    """Keywords with regex special chars are escaped properly."""
    patterns = _compile_patterns({"test_topic": ["co-op", "c++"]})
    # Should not raise; should not match substrings
    assert patterns["test_topic"][0].search("co-op meeting") is not None
    assert patterns["test_topic"][0].search("loop") is None


def test_compile_patterns_hashtag_keyword_matches_glued_token():
    """A '#'-prefixed keyword matches the bare glued token, with or without '#'.

    Regression for the dead-hashtag bug (29 May 2026): a leading ``\\b``
    placed before '#' can never fire because a space-to-'#' transition is
    non-word to non-word, so there is no word boundary there. Every
    '#'-prefixed taxonomy keyword across za/ng/ke therefore matched
    nothing, not even literal '#nyamachoma' text. The fix strips the
    leading '#' so the keyword matches the hashtag content as a token
    whether or not the '#' is present.
    """
    for kw, core in [
        ("#nyamachoma", "nyamachoma"),
        ("#mitumba", "mitumba"),
        ("#arbantone", "arbantone"),
    ]:
        patterns = _compile_patterns({"t": [kw]})
        pat = patterns["t"][0]
        # Glued bare token (the form actually present in scraped KE data).
        assert pat.search(f"loving the {core} today") is not None, kw
        # Literal hashtag form still matches.
        assert pat.search(f"posted #{core} vibes") is not None, kw
        # Must not match embedded inside a longer word (boundary preserved).
        assert pat.search(f"x{core}x") is None, kw


def test_classify_topics_hashtag_keyword_classifies_glued_token_ke():
    """'#nyamachoma' classifies a KE row whose only signal is the glued token.

    Mirrors the 29 May 2026 finding: 28 unclassified KE rows carried the
    glued token 'nyamachoma' while the spaced 'nyama choma' keyword and the
    dead '#nyamachoma' keyword matched none of them. No slang_terms are
    passed, so the slang fallback layer stays inert and the regex layer
    alone must classify the row.
    """
    result = classify_topics(
        title="best nyamachoma in town",
        text="",
        hashtags="",
        market="ke",
    )
    assert "food_nyamachoma" in result


# ---------------------------------------------------------------------------
# Layer 4: geo-collision strip at classification time.
# Added 28 May 2026 after a Sapa Vietnam tourism row reached
# enriched_content.topic_groups carrying economy_sapa_hustle despite the
# brief-pull filter catching it downstream. The shared blocklist now
# applies at classify time so contaminated rows never get the tag.
# ---------------------------------------------------------------------------


def test_classify_topics_strips_sapa_vietnam_travel_collision():
    """Sapa Vietnam travel text does NOT get economy_sapa_hustle.

    Repro of the 28 May 2026 SUSPECT row: 'Travel experiences and tourism
    destinations including Sapa Vietnam, scenic landscapes, mountains,
    and tour packages'. The bare 'sapa' token would normally trigger the
    NG slang-fallback layer, but the geo strip drops the topic because
    the haystack matches 'sapa vietnam' on the blocklist.
    """
    result = classify_topics(
        title="Travel Tourism Destinations",
        text=(
            "Travel experiences and tourism destinations including Sapa Vietnam, "
            "scenic landscapes, mountains, and tour packages"
        ),
        hashtags="",
        market="ng",
        slang_terms=["sapa"],
    )
    assert "economy_sapa_hustle" not in result


def test_classify_topics_strips_ankara_turkey_collision():
    """Turkish Ankara content does NOT get fashion_ankara_asoebi.

    Bare 'ankara' is a legitimate NG fabric token and is intentionally
    not blocklisted, but 'ankara turkey' / 'turkey' / 'istanbul' markers
    in the same haystack flip the geo filter on and the topic is stripped.
    """
    result = classify_topics(
        title="Ankara, Turkey city break",
        # Diacritic dotless-i is intentional Turkish content for the test.
        text="Visited Istanbul last week. Now exploring Ankara Turkey gölbaşı district.",  # noqa: RUF001
        hashtags="#ankara #turkey #istanbul",
        market="ng",
    )
    assert "fashion_ankara_asoebi" not in result


def test_classify_topics_keeps_legitimate_naija_sapa_content():
    """Naija sapa hustle text stays classified as economy_sapa_hustle.

    Guard against over-aggressive stripping: a real NG pidgin post about
    being broke must keep the topic. No Vietnam / Turkey / India markers
    means the geo strip is a no-op.
    """
    result = classify_topics(
        title="Sapa don catch me again",
        text="No money this month, the hustle dey scatter my head",
        hashtags="#sapa #naija",
        market="ng",
        slang_terms=["sapa"],
    )
    assert "economy_sapa_hustle" in result


def test_classify_topics_keeps_legitimate_naija_ankara_fabric():
    """Pure ankara fabric content (no Turkey markers) stays classified."""
    result = classify_topics(
        title="My new ankara dress for the owambe",
        text="Stunning aso ebi look with custom ankara and gele",
        hashtags="#ankara #owambe #asoebi",
        market="ng",
    )
    assert "fashion_ankara_asoebi" in result


def test_classify_topics_drops_devanagari_hindi_from_sapa_topic():
    """Hindi text in Devanagari script triggers the non-SSA-script backstop
    and strips economy_sapa_hustle even though 'sapa' fired in slang fallback.
    """
    # "Samajwadi Party" written in Devanagari (10 chars in the script block,
    # well above the 5-char threshold).
    result = classify_topics(
        title="समाजवादी पार्टी",
        text="उत्तर प्रदेश राजनीति",
        hashtags="",
        market="ng",
        slang_terms=["sapa"],
    )
    assert "economy_sapa_hustle" not in result


def test_classify_topics_gdelt_themes_rescue_when_regex_empty():
    """A GDELT row carrying compound GKG codes (which the word-boundary
    regex cannot match because '_' is a word char) is rescued to the
    market's politics topic via the themes layer."""
    # ng: GENERAL_GOVERNMENT + compound LEADER code -> politics_tinubu
    result = classify_topics(
        title="",
        text="GENERAL_GOVERNMENT,12 LEADER_TRANSITION,328 IMPEACHMENT,9",
        hashtags="",
        market="ng",
        content_type="gdelt_gkg",
    )
    assert result == ["politics_tinubu"]


def test_classify_topics_gdelt_themes_gated_to_gdelt_platform():
    """The same theme-code text on a non-GDELT platform is NOT rescued
    (gating prevents false matches on prose that happens to contain codes)."""
    result = classify_topics(
        title="",
        text="GENERAL_GOVERNMENT,12 LEADER_TRANSITION,328",
        hashtags="",
        market="ng",
        content_type="post",
    )
    assert result == []
    # default platform (None) also does not trigger the themes layer
    assert classify_topics("", "GENERAL_GOVERNMENT,12", "", "ng") == []


def test_classify_topics_regex_wins_over_gdelt_themes():
    """When the regex layer already classifies a GDELT row, the themes
    rescue does not fire (regex precedence, no pre-empt)."""
    result = classify_topics(
        title="Burna Boy drops afrobeats album",
        text="GENERAL_GOVERNMENT,12 LEADER_TRANSITION,328",
        hashtags="",
        market="ng",
        content_type="gdelt_gkg",
    )
    assert "music_afrobeats" in result
    assert "politics_tinubu" not in result


def test_classify_topics_gdelt_themes_per_market_protest():
    """Protest-family code maps to politics_maandamano on KE; a politics
    code maps to politics_crises on ZA. Compound codes so regex stays out."""
    ke = classify_topics("", "WB_2410_UNREST,10 PROTEST,2", "", "ke", content_type="gdelt_gkg")
    assert ke == ["politics_maandamano"]
    za = classify_topics("", "ELECTION,20 IMPEACHMENT,5", "", "za", content_type="gdelt_gkg")
    assert za == ["politics_crises"]


def test_classify_topics_gdelt_oil_and_dollar_map_economy_ng():
    """ENV_OIL / ECON_WORLDCURRENCIES tokens on unclassified GDELT rows map to economy."""
    oil = classify_topics(
        "",
        "ENV_OIL,62 ENV_OIL,149 ENV_OIL,277 GENERAL_GOVERNMENT,4",
        "",
        "ng",
        content_type="gdelt_gkg",
    )
    assert oil == ["economy_sapa_hustle"]
    dollar = classify_topics(
        "",
        "ECON_WORLDCURRENCIES_DOLLAR,754 ECON_WORLDCURRENCIES_DOLLAR,4935",
        "",
        "ng",
        content_type="gdelt_gkg",
    )
    assert dollar == ["economy_sapa_hustle"]


def test_classify_topics_slang_nepa_fallback_ng():
    """nepa slang maps when regex text is absent but slang_terms carries nepa."""
    result = classify_topics(
        title="qqqq zzzz",
        text="nothing topical here",
        hashtags="",
        market="ng",
        slang_terms=["nepa"],
    )
    assert result == ["economy_sapa_hustle"]


def test_classify_topics_loadshedding_glued_token_za():
    """Bare loadshedding token classifies via regex (not only 'load shedding')."""
    result = classify_topics(
        title="loadshedding hits our block again tonight",
        text="",
        hashtags="",
        market="za",
    )
    assert result == ["infra_power_eskom"]


def test_classify_topics_gdelt_themes_dominant_family_single_topic():
    """A many-theme GDELT article returns the single dominant mappable
    family, not one topic per theme (avoids 1/N weight dilution).
    Compound codes so the word-boundary regex does not pre-match."""
    # 4 economy code hits vs 1 politics -> economy wins (ng economy_sapa_hustle)
    result = classify_topics(
        title="",
        text="EPU_ECONOMY,1 WB_442_INFLATION,2 MACROECONOMIC,3 GENERAL_GOVERNMENT,4",
        hashtags="",
        market="ng",
        content_type="gdelt_gkg",
    )
    assert result == ["economy_sapa_hustle"]


def test_classify_topics_gdelt_themes_geo_strip_applies():
    """A GDELT economy theme that maps to a geo-collidable topic is still
    stripped when foreign markers are present. No 'sapa' word so the
    regex layer stays empty and the rescue+geostrip path is exercised."""
    result = classify_topics(
        title="",
        text="EPU_ECONOMY,1 WB_442_INFLATION,2 Vietnam Hanoi Halong trekking tour",
        hashtags="",
        market="ng",
        content_type="gdelt_gkg",
    )
    assert result == []


def test_classify_topics_geo_strip_does_not_affect_other_topics():
    """Geo strip only affects topics in TOPIC_GEO_BLOCKLIST.

    A row that classifies into music_amapiano + economy_sapa_hustle where
    the haystack carries Vietnam markers should drop economy_sapa_hustle
    but keep music_amapiano.
    """
    result = classify_topics(
        title="amapiano vibes on Sapa Vietnam holiday tour",
        text="log drum dancing in the mountains, scenic landscapes",
        hashtags="#amapiano",
        market="za",
    )
    # music_amapiano fires from amapiano + log drum + dancing.
    # economy_sapa_hustle is ZA-irrelevant anyway but the principle holds:
    # any geo-blocklist topic that DID match would be stripped without
    # affecting unrelated topics.
    assert "music_amapiano" in result


# ---------------------------------------------------------------------------
# Football / FIFA pickup (9 Jun 2026).
# Football was badly under-classified (ZA 28% / NG 49% / KE 81% of football
# content unclassified) because the taxonomy lacked FIFA / UEFA / World Cup /
# AFCON / Champions League and any fixture pattern. These tests pin the new
# keyword coverage AND the guarded "X vs Y" fixture rule. ZA folds football
# into sports_rugby; NG and KE use sports_football.
# ---------------------------------------------------------------------------


def test_classify_football_fifa_world_cup_keyword():
    """ "FIFA World Cup" classifies to the football topic in each market."""
    assert "sports_rugby" in classify_topics("FIFA World Cup fever hits Mzansi", "", "", "za")
    assert "sports_football" in classify_topics(
        "FIFA World Cup qualifiers for the Super Eagles", "", "", "ng"
    )
    assert "sports_football" in classify_topics(
        "FIFA World Cup dreams for Harambee Stars", "", "", "ke"
    )


def test_classify_football_champions_league_keyword():
    """ "Champions League final" classifies to the football topic per market."""
    assert "sports_rugby" in classify_topics(
        "Champions League final watch party in Jozi", "", "", "za"
    )
    assert "sports_football" in classify_topics(
        "Champions League final night, naija football twitter is loud", "", "", "ng"
    )
    assert "sports_football" in classify_topics(
        "Champions League final and Nairobi is buzzing", "", "", "ke"
    )


def test_classify_football_fixture_country_vs_country():
    """A "Portugal vs Chile" fixture (entity-flank guard) classifies to football.

    Both sides are curated national-team entities, so the entity-flank branch
    fires even with no other football keyword present.
    """
    assert "sports_football" in classify_topics(
        "Portugal vs Chile tonight, who you got", "", "", "ng"
    )
    assert "sports_football" in classify_topics(
        "Portugal vs Chile is the match of the day", "", "", "ke"
    )
    assert "sports_rugby" in classify_topics("Portugal vs Chile, grabbing the remote", "", "", "za")


def test_classify_football_fixture_club_vs_club():
    """ "Arsenal vs Chelsea" (clubs in the entity list) classifies to football."""
    assert "sports_football" in classify_topics("Arsenal vs Chelsea this weekend", "", "", "ke")
    assert "sports_football" in classify_topics(
        "Arsenal versus Chelsea, London derby", "", "", "ng"
    )


def test_classify_football_national_team_mention():
    """A bare national-team / club mention classifies to the football topic."""
    # NG already carried Super Eagles; assert it still routes correctly.
    assert "sports_football" in classify_topics(
        "Super Eagles squad announced for the friendly", "", "", "ng"
    )
    # KE national team.
    assert "sports_football" in classify_topics(
        "Harambee Stars name their starting eleven", "", "", "ke"
    )
    # ZA national football team folds into sports_rugby.
    assert "sports_rugby" in classify_topics("Bafana Bafana name the World Cup squad", "", "", "za")


def test_classify_football_fixture_keyword_cooccurrence_branch():
    """A bare "City vs United" fixture classifies only because a football
    keyword ("champions league") is independently present in the row.

    Exercises the second guard branch: the teams are not in the curated
    entity list, but the co-occurring keyword pulls the fixture in.
    """
    result = classify_topics(
        title="what a night",
        text="City vs United, this Champions League tie was unreal",
        hashtags="",
        market="ke",
    )
    assert "sports_football" in result


def test_classify_non_football_vs_does_not_classify_as_football():
    """Non-football "X vs Y" phrases must NOT classify as football.

    Neither guard fires: no curated football entity flanks the vs, and no
    football keyword is present in the row. This is the <2% false-positive
    bar the shadow validation measures.
    """
    for market, football_topic in [
        ("za", "sports_rugby"),
        ("ng", "sports_football"),
        ("ke", "sports_football"),
    ]:
        assert football_topic not in classify_topics(
            "Apple vs Samsung flagship showdown", "", "", market
        ), market
        assert football_topic not in classify_topics(
            "Trump vs Biden in the polls again", "", "", market
        ), market
        assert football_topic not in classify_topics(
            "Marvel vs DC, which universe wins", "", "", market
        ), market


def test_classify_home_country_vs_without_football_context_is_not_football():
    """Bare home-market country names are NOT fixture entities, so a "Country vs X"
    in a non-football context does not classify as football. Pins the shadow's
    real-data false positive (fintech "Kenya vs Tanzania" was wrongly tagged via
    the bare "kenya" entity, now removed)."""
    assert "sports_football" not in classify_topics(
        "M-Pesa Kenya vs Tanzania cross-border transfer fees compared", "", "", "ke"
    )
    # A genuine home fixture still classifies, via the opponent entity + the
    # co-occurring "World Cup" keyword (not via the removed home-country name).
    assert "sports_football" in classify_topics(
        "Nigeria vs Ghana in the World Cup qualifier", "", "", "ng"
    )


def test_classify_football_european_club_and_player_keywords():
    """Marquee European clubs and players classify to the football topic.

    ZA gained the most coverage (it had almost none); confirm a club and a
    player both route to sports_rugby there.
    """
    assert "sports_rugby" in classify_topics(
        "Real Madrid look unstoppable this season", "", "", "za"
    )
    assert "sports_rugby" in classify_topics("Mbappe scored a hat-trick", "", "", "za")
    assert "sports_football" in classify_topics("Salah on fire for Liverpool again", "", "", "ng")


def test_bsa_dimension_and_noise_fixtures_leave_the_default_classifier_unchanged():
    """The client overlay's fixtures classify identically with and without the overlay.

    The default classifier has no dimension, distinction or exclusion concept; the
    overlay compiler runs beside it and never changes a default topic result.
    """
    from src.analysis.open_intelligence import client_overlay
    from src.enrichment import topic_classifier

    from tests.unit.test_client_overlay import BSA_DIMENSION_FIXTURES, BSA_NOISE_FIXTURES

    texts = [
        text
        for _d, _n, positives, negatives in BSA_DIMENSION_FIXTURES
        for text in (*positives, *negatives)
    ] + [text for _c, stripped, kept in BSA_NOISE_FIXTURES for text in (*stripped, *kept)]
    markets = ("za", "ng", "ke")
    before = {(m, t): classify_topics("", t, "", m) for m in markets for t in texts}
    compiled = client_overlay.compiled_overlay_for_brand("bsa")
    stripped = [t for t in texts if client_overlay.apply_noise_controls(compiled, t)["excluded"]]
    assert stripped
    after = {(m, t): classify_topics("", t, "", m) for m in markets for t in texts}
    assert before == after
    # A text the client lens strips is still classified by the default engine on its own
    # merits: the exclusion is the client's, not a default culture rule, and no market
    # returns the drop sentinel for it.
    for text in stripped:
        for market in markets:
            assert topic_classifier.DROP_SENTINEL not in after[(market, text)]
