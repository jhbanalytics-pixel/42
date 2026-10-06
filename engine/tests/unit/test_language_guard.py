"""Unit tests for src/utils/language_guard.py + the classifier Layer-0 wiring.

The langdetect verdict is monkeypatched so the tests are deterministic and
CI-safe (no dependence on the langdetect model's call on a specific string).
The real langdetect integration is validated by the cloud shadow-validate of
the per-market drop-rate before the flag ever flips.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.enrichment import topic_classifier
from src.enrichment.topic_classifier import DROP_SENTINEL, classify_topics
from src.utils import language_guard as lg
from src.utils.language_guard import (
    HARD_FOREIGN_LANGS,
    LANGDETECT_MIN_CHARS,
    combined_text,
    is_hard_foreign_text,
    should_skip_content_type,
)


class _FakeLang:
    def __init__(self, lang: str, prob: float):
        self.lang = lang
        self.prob = prob


# ----- the hard-foreign language set -----------------------------------


def test_pt_in_hard_foreign_set():
    # Portuguese is included for the NG japa Brazilian-Portuguese spam class.
    assert "pt" in HARD_FOREIGN_LANGS


def test_pidgin_sheng_codes_excluded():
    # tl/id/ms/so are deliberately ABSENT: NG Pidgin + Sheng mis-classify into
    # them, so including them would strip legitimate local content.
    for code in ("tl", "id", "ms", "so"):
        assert code not in HARD_FOREIGN_LANGS


# ----- is_hard_foreign_text thresholds ---------------------------------


def test_short_text_never_flagged():
    # Below the min-chars floor langdetect is too noisy to act on.
    assert is_hard_foreign_text("sushi japa") is False
    assert is_hard_foreign_text("") is False
    assert is_hard_foreign_text("   ") is False


def test_high_confidence_hard_foreign_flagged(monkeypatch):
    monkeypatch.setattr(lg, "_LANGDETECT_AVAILABLE", True)
    monkeypatch.setattr(lg, "detect_langs", lambda _s: [_FakeLang("pt", 0.97)])
    assert is_hard_foreign_text("x" * (LANGDETECT_MIN_CHARS + 5)) is True


def test_low_confidence_not_flagged(monkeypatch):
    # A hard-foreign code but below the 0.90 confidence bar is kept.
    monkeypatch.setattr(lg, "_LANGDETECT_AVAILABLE", True)
    monkeypatch.setattr(lg, "detect_langs", lambda _s: [_FakeLang("pt", 0.70)])
    assert is_hard_foreign_text("x" * (LANGDETECT_MIN_CHARS + 5)) is False


def test_excluded_language_not_flagged_even_high_confidence(monkeypatch):
    # Tagalog at 0.99 must still pass: tl is not in HARD_FOREIGN_LANGS.
    monkeypatch.setattr(lg, "_LANGDETECT_AVAILABLE", True)
    monkeypatch.setattr(lg, "detect_langs", lambda _s: [_FakeLang("tl", 0.99)])
    assert is_hard_foreign_text("x" * (LANGDETECT_MIN_CHARS + 5)) is False


def test_graceful_degrade_when_langdetect_unavailable(monkeypatch):
    monkeypatch.setattr(lg, "_LANGDETECT_AVAILABLE", False)
    assert is_hard_foreign_text("x" * (LANGDETECT_MIN_CHARS + 5)) is False


def test_empty_results_not_flagged(monkeypatch):
    monkeypatch.setattr(lg, "_LANGDETECT_AVAILABLE", True)
    monkeypatch.setattr(lg, "detect_langs", lambda _s: [])
    assert is_hard_foreign_text("x" * (LANGDETECT_MIN_CHARS + 5)) is False


# ----- classifier Layer-0 wiring (the flag gate) -----------------------


def test_classifier_drops_foreign_when_guard_on(monkeypatch):
    monkeypatch.setenv("LANGUAGE_GUARD_ENABLED", "true")
    monkeypatch.setattr(topic_classifier, "is_hard_foreign_text", lambda _t: True)
    out = classify_topics("title", "some long foreign-language body text", "", "ng")
    assert out == [DROP_SENTINEL]


def test_classifier_ignores_guard_when_flag_off(monkeypatch):
    # Flag off: the foreign verdict is never consulted; the row classifies
    # normally (so a True verdict must NOT drop it).
    monkeypatch.delenv("LANGUAGE_GUARD_ENABLED", raising=False)
    monkeypatch.setattr(topic_classifier, "is_hard_foreign_text", lambda _t: True)
    out = classify_topics("amapiano", "amapiano party in soweto this weekend", "", "za")
    assert out != [DROP_SENTINEL]


def test_classifier_keeps_local_when_guard_on_and_not_foreign(monkeypatch):
    monkeypatch.setenv("LANGUAGE_GUARD_ENABLED", "true")
    monkeypatch.setattr(topic_classifier, "is_hard_foreign_text", lambda _t: False)
    out = classify_topics("amapiano", "amapiano party in soweto this weekend", "", "za")
    assert out != [DROP_SENTINEL]


def test_classifier_skips_guard_for_gdelt_content_type(monkeypatch):
    # A gdelt row whose V2Themes machine-code text langdetect would call foreign
    # must NOT drop: the guard skips gdelt + video content (the 8 Jun over-drop).
    monkeypatch.setenv("LANGUAGE_GUARD_ENABLED", "true")
    monkeypatch.setattr(topic_classifier, "is_hard_foreign_text", lambda _t: True)
    out = classify_topics(
        "WB_135_TRANSPORT", "WB_137_WATER,32 WB_137_WATER,189", "", "ke", content_type="gdelt_gkg"
    )
    assert out != [DROP_SENTINEL]


# ----- should_skip_content_type + combined_text ------------------------


def test_should_skip_gdelt_and_video():
    assert should_skip_content_type("gdelt_gkg") is True
    assert should_skip_content_type("video/10") is True
    assert should_skip_content_type("video/2") is True


def test_should_skip_bare_video_and_chart_track():
    # YouTube keyword-search + trending emit a bare "video" (no categoryId),
    # which the "video/" prefix misses; Apple Music emits "chart_track". Both
    # carry titles, not prose, so the guard must skip them.
    assert should_skip_content_type("video") is True
    assert should_skip_content_type("chart_track") is True


def test_should_not_skip_social_content_types():
    assert should_skip_content_type("post") is False
    assert should_skip_content_type("reddit_post") is False
    assert should_skip_content_type("mention") is False
    assert should_skip_content_type(None) is False


def test_combined_text_dedups_exact_duplicate():
    # Connectors often store the same string in title + text; doubling it would
    # inflate the length past the floor and skew langdetect.
    assert combined_text("nyama choma", "nyama choma") == "nyama choma"
    assert combined_text("a", "b") == "a b"
    assert combined_text("a", "") == "a"
    assert combined_text("", "b") == "b"
