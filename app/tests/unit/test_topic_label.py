"""Frontend topicLabel parity checks via the backend canonical implementation."""

from src.api import bq, desk


def test_topic_label_known_keys():
    assert bq.topic_label("politics_maandamano") == "Maandamano protests"
    assert bq.topic_label("economy_sapa_hustle") == "Sapa hustle"
    assert bq.topic_label("tech_gemini_ai") == "Gemini & AI adoption"


def test_topic_label_unknown_key_prettifies():
    assert bq.topic_label("music_amapiano") == "Amapiano"
    assert bq.topic_label("genz_lifestyle") == "Lifestyle"


def test_legacy_genz_domain_key_has_a_neutral_visible_label():
    assert desk._mono_label("genz_lifestyle", "za") == "Culture · South Africa"
