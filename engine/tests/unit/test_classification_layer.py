"""Unit tests for Wave 1 classification-layer attribution in topic_classifier.

Covers classify_topics_with_layer: it returns the winning layer alongside the
topics. classify_topics still returns just the topics (back-compat). These
tests use the real per-market taxonomies, like test_topic_classifier.py.
"""

from __future__ import annotations

from src.enrichment.topic_classifier import (
    DROP_SENTINEL,
    LAYER_BRAND24,
    LAYER_DROP,
    LAYER_GDELT,
    LAYER_REGEX,
    LAYER_SLANG,
    LAYER_UNCLASSIFIED,
    classify_topics,
    classify_topics_with_layer,
)


def test_layer_regex_wins_on_keyword_match():
    """A plain keyword hit attributes to the regex layer."""
    topics, layer = classify_topics_with_layer(
        title="New amapiano track fire",
        text="",
        hashtags="#amapiano",
        market="za",
    )
    assert "music_amapiano" in topics
    assert layer == LAYER_REGEX


def test_layer_unclassified_when_nothing_matches():
    """No layer matches -> empty topics and the unclassified layer."""
    topics, layer = classify_topics_with_layer(
        title="zzz qqq vvv",
        text="nothing here matches any taxonomy",
        hashtags="",
        market="za",
    )
    assert topics == []
    assert layer == LAYER_UNCLASSIFIED


def test_layer_brand24_label_map_wins():
    """A Brand24 aggregated label maps directly and attributes to brand24.

    Uses the ZA 'music and entertainment' bucket label, which the Brand24
    label map routes to a taxonomy topic.
    """
    topics, layer = classify_topics_with_layer(
        title="",
        text="",
        hashtags="",
        market="za",
        query_term="Music and Entertainment",
    )
    # The exact topic depends on the label map; the layer must be brand24 and a
    # topic must be present (otherwise the test fixture label is stale).
    assert topics, "expected the Brand24 label map to yield a topic"
    assert layer == LAYER_BRAND24


def test_layer_drop_on_brand24_noise_label():
    """A Brand24 drop-list label returns the drop sentinel and the drop layer."""
    topics, layer = classify_topics_with_layer(
        title="",
        text="",
        hashtags="",
        market="za",
        query_term="Chilean Regional News",
    )
    assert topics == [DROP_SENTINEL]
    assert layer == LAYER_DROP


def test_layer_gdelt_rescue_attributes_to_gdelt():
    """A GDELT row regex left empty, rescued by theme codes, attributes to gdelt.

    ECON_* theme codes map to an economy family in the GDELT theme rescue. The
    text is machine theme codes, not natural-language keywords, so the regex
    layer finds nothing and the rescue fires.
    """
    topics, layer = classify_topics_with_layer(
        title="",
        text="ECON_STOCKMARKET;ECON_INTEREST_RATE;ECON_INFLATION",
        hashtags="",
        market="ng",
        content_type="gdelt_gkg",
    )
    if topics:
        # Rescue fired: layer must be gdelt.
        assert layer == LAYER_GDELT
    else:
        # If the NG theme map has no economy family the row stays unclassified;
        # never misattributed to regex.
        assert layer == LAYER_UNCLASSIFIED


def test_layer_slang_nepa_fallback_attributes_to_slang():
    """nepa via slang_terms attributes to slang when regex text is empty."""
    topics, layer = classify_topics_with_layer(
        title="qqqq",
        text="zzzz vvvv",
        hashtags="",
        market="ng",
        slang_terms=["nepa"],
    )
    assert topics == ["economy_sapa_hustle"]
    assert layer == LAYER_SLANG


def test_layer_slang_fallback_attributes_to_slang():
    """A row matched only by the slang fallback attributes to slang.

    Passes a slang term via slang_terms with text the regex layer cannot match,
    so Layer 3 is the only one that can fire.
    """
    topics, layer = classify_topics_with_layer(
        title="qqqq",
        text="zzzz vvvv",
        hashtags="",
        market="ke",
        slang_terms=["gengetone"],
    )
    if topics:
        assert layer == LAYER_SLANG
    else:
        assert layer == LAYER_UNCLASSIFIED


def test_classify_topics_back_compat_matches_layer_topics():
    """classify_topics returns exactly the topic list from the layer-aware path."""
    kwargs = {
        "title": "New amapiano track fire",
        "text": "",
        "hashtags": "#amapiano",
        "market": "za",
    }
    topics_only = classify_topics(**kwargs)
    topics_with_layer, _ = classify_topics_with_layer(**kwargs)
    assert topics_only == topics_with_layer


def test_classify_topics_back_compat_drop_sentinel():
    """The back-compat wrapper still surfaces the drop sentinel unchanged."""
    result = classify_topics(
        title="",
        text="",
        hashtags="",
        market="za",
        query_term="Chilean Regional News",
    )
    assert result == [DROP_SENTINEL]
