"""Hard-foreign-language guard for ingest-time row dropping.

Mirrors the conservative langdetect filter that has run at brief-generation
time since 26 May 2026 (``src/analysis/generate_briefs.py``), but exposes it as
a reusable check so the topic classifier can drop hard-foreign rows BEFORE they
reach ``trend_scores``. A foreign-language row (Brazilian-Portuguese in NG
japa, Vietnamese in NG sapa, a Tagalog or Portuguese receipt) that survives to
scoring inflates a topic's volume and velocity even when the brief text later
filters it; running the same check at classification removes that inflation at
source.

Conservative by construction so it never strips legitimate Sub-Saharan-African
content. SSA text (English, NG Pidgin, Sheng, Swahili, Afrikaans, Zulu, Xhosa)
does not classify as any HARD_FOREIGN_LANGS code at high confidence on 40+ char
text. The set deliberately EXCLUDES tl / id / ms / so, which NG Pidgin and
Sheng routinely mis-classify into (probed 26 May 2026 with 10 real pidgin
samples: 5x en, 2x tl, 2x so, 1x id). Portuguese (pt) is included for the NG
japa Brazilian-Portuguese spam class; legitimate SSA content does not reach the
0.90 confidence bar for pt on 40+ char text.

The check is pure and side-effect free. The classifier gates it behind the
LANGUAGE_GUARD_ENABLED env flag (default off) so the code can ship dark and be
shadow-validated against a real-data drop-rate before it ever drops a live row.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from langdetect import DetectorFactory, LangDetectException, detect_langs

    # Deterministic detection so the same text always yields the same verdict
    # (langdetect is randomised by default, which would make the guard flap).
    DetectorFactory.seed = 0
    _LANGDETECT_AVAILABLE = True
except ImportError:  # graceful degradation if langdetect is not installed
    _LANGDETECT_AVAILABLE = False

# Hard-foreign language codes. A high-confidence detection of one of these on
# 40+ char text is a reliable signal the row is foreign noise the keyword
# blocklist missed (Vietnamese without diacritics, Hindi transliterated to
# Latin, Turkish without distinctive diacritics, Brazilian Portuguese, etc.).
# EXCLUDES tl / id / ms / so on purpose: NG Pidgin + Sheng mis-classify into
# those, and blocklisting them would strip legitimate local content.
HARD_FOREIGN_LANGS: frozenset[str] = frozenset(
    {
        "vi",  # Vietnamese
        "th",  # Thai
        "ar",  # Arabic
        "hi",  # Hindi
        "de",  # German
        "ru",  # Russian
        "ja",  # Japanese
        "zh-cn",  # Simplified Chinese
        "zh-tw",  # Traditional Chinese
        "ko",  # Korean
        "tr",  # Turkish
        "fa",  # Persian
        "ur",  # Urdu
        "pt",  # Portuguese (NG japa Brazilian-Portuguese spam class)
    }
)

# Minimum text length before running langdetect. Raised to 100 after the
# 8 Jun 2026 shadow-validate: short caps/slang posts (KE "MAAANDAMANO", "NYAMA
# CHOMA EXPERIENCE", music-video titles) cleared the old 40-char floor and
# mis-classified as hard-foreign. Genuine foreign spam runs long, so a higher
# floor keeps the catch while dropping the false positives.
LANGDETECT_MIN_CHARS: int = 100

# Probability threshold for dropping. Conservative so legitimate SSA content is
# never stripped: act only when the top candidate is BOTH hard-foreign AND
# highly confident.
LANGDETECT_DROP_PROB: float = 0.90


def is_hard_foreign_text(text: str, extra_langs: frozenset[str] | None = None) -> bool:
    """True when langdetect identifies ``text`` as hard-foreign at high confidence.

    Acts only on text of at least ``LANGDETECT_MIN_CHARS`` (100) whose top
    langdetect candidate is in ``HARD_FOREIGN_LANGS`` with probability at least
    ``LANGDETECT_DROP_PROB`` (0.90). Returns False when langdetect is
    unavailable, the text is too short, detection errors, or the verdict is not
    a confident hard-foreign hit. Pure.

    ``extra_langs`` widens the hard-foreign set for a caller that can afford a
    stricter check. The global default excludes tl / id / ms because TikTok
    Pidgin and Sheng mis-classify into them, but a caller operating on
    English-prose sources (Reddit) can safely pass those codes to catch the
    foreign-community firehose the default lets through.
    """
    if not _LANGDETECT_AVAILABLE:
        return False
    s = (text or "").strip()
    if len(s) < LANGDETECT_MIN_CHARS:
        return False
    try:
        results = detect_langs(s)
    except LangDetectException:
        return False
    if not results:
        return False
    top = results[0]
    langs = HARD_FOREIGN_LANGS | extra_langs if extra_langs else HARD_FOREIGN_LANGS
    return top.lang in langs and top.prob >= LANGDETECT_DROP_PROB


# Content types whose text is NOT conversational natural language, so langdetect
# is unreliable on them and the guard skips them. gdelt_gkg rows carry V2Themes
# machine codes (WB_/TAX_/ECON_); video/* rows carry music-video titles (artist
# names in caps). YouTube keyword-search and trending emit a bare "video" with
# no categoryId, which the "video/" prefix misses; Apple Music chart rows emit
# "chart_track" (artist + track title, not prose). Their geo-contamination is
# handled by the geo_blocklist + the GKG v2locations guard (phase 2), not by
# language detection.
_SKIP_CONTENT_TYPE_EXACT: frozenset[str] = frozenset({"gdelt_gkg", "video", "chart_track"})
_SKIP_CONTENT_TYPE_PREFIX: tuple[str, ...] = ("video/",)


def should_skip_content_type(content_type: str | None) -> bool:
    """True when a row's content_type is not conversational natural language."""
    ct = (content_type or "").strip()
    return ct in _SKIP_CONTENT_TYPE_EXACT or ct.startswith(_SKIP_CONTENT_TYPE_PREFIX)


def combined_text(title: str | None, text: str | None) -> str:
    """Join title + text, collapsing an exact title==text duplicate to one copy.

    Connectors often store the same string in both fields; doubling it inflates
    the length past the min-char floor and skews langdetect. Dedup so the floor
    reflects the real unique content.
    """
    t = (title or "").strip()
    x = (text or "").strip()
    if not x or x == t:
        return t
    if not t:
        return x
    return f"{t} {x}"
