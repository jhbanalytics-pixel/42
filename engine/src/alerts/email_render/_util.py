"""Shared helpers for the PULSE v2 mailer renderers.

Pure string helpers: HTML escaping, humanizing a topic slug, looking up a
market's display name and dot color from the brand config, parsing the
pipe-delimited creator strings the pipeline stores, and building the small
repeated HTML fragments (channel bars, chips). Renderers import from here so
the binding logic lives in one place.
"""

from __future__ import annotations

import re
from html import escape as _html_escape

from src.alerts.email_render._table import CLS, FONT_MONO

# Em dash (U+2014) and en dash (U+2013), built by codepoint so this source
# file itself stays dash-free. Real Gemini briefs in BigQuery carry these (a
# date range like 2026 to 2027 comes back rendered with an en dash), so the
# renderer strips them defensively at the last mile regardless of model output.
_EM_DASH = chr(0x2014)
_EN_DASH = chr(0x2013)

# Topic-slug de-leak. The first PULSE render off real briefs (30 May 2026)
# carried raw slugs straight from Gemini prose: `The "economy_hustle" topic in
# Kenya is currently trending`, and a later pass found `education_matric_nsfas`
# survive a prefix allowlist that did not list every taxonomy category. The
# prompt now forbids the slug at the source, but clean_copy strips any that slip
# through so the body never shows an underscore slug. Match any lowercase
# ``word_word`` token (two or more underscore-joined segments), and only when
# NOT preceded by ``@`` (a handle), ``/`` (inside a url), ``#`` (a hashtag), or
# another word char, so creator handles (@scholah_meeme), url paths (a_b_c) and
# underscore hashtags (#money_moves) survive untouched.
# Lowercase-anchored so SCREAMING_CASE and CamelCase are never touched.
_TOPIC_SLUG_RE = re.compile(r"(?<![@\w/#])[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


def _deslug(match: re.Match) -> str:
    return humanize_topic(match.group(0))


# Banned house-style buzzwords mapped to neutral words. Gemini brief copy
# occasionally reaches for these (a 22-Jun digest carried "leverage", "seamless",
# "elevate"); the dash guard does not touch them, so neutralise them here as the
# same last-mile pass. Longer inflections first so "leverages" wins before
# "leverage". Word-boundary + case-insensitive; the replacement copies the
# matched word's leading case so a sentence-initial hit stays capitalised.
_BUZZWORD_SUB = [
    (re.compile(r"\bleveraging\b", re.I), "using"),
    (re.compile(r"\bleverages\b", re.I), "uses"),
    (re.compile(r"\bleveraged\b", re.I), "used"),
    (re.compile(r"\bleverage\b", re.I), "use"),
    (re.compile(r"\bseamlessly\b", re.I), "smoothly"),
    (re.compile(r"\bseamless\b", re.I), "smooth"),
    (re.compile(r"\belevating\b", re.I), "lifting"),
    (re.compile(r"\belevates\b", re.I), "lifts"),
    (re.compile(r"\belevated\b", re.I), "lifted"),
    (re.compile(r"\belevate\b", re.I), "lift"),
    (re.compile(r"\brobust\b", re.I), "strong"),
    (re.compile(r"\bholistic\b", re.I), "broad"),
    (re.compile(r"\bsynergy\b", re.I), "fit"),
]


def _match_case(repl: str, matched: str) -> str:
    """Copy the matched word's leading case onto the replacement."""
    return repl[:1].upper() + repl[1:] if matched[:1].isupper() else repl


def clean_copy(s: str) -> str:
    """Sanitise brief-sourced prose for the email body.

    Two passes. First, scrub every dash form (em dash, en dash, double hyphen)
    to a comma and a space, eating any whitespace that hugged the dash so a
    spaced ``ease <em dash> softness`` becomes ``ease, softness`` and not
    ``ease , softness``. This is the last-mile guard: several brief text fields
    (trend_synthesis, headline, sentiment_summary, through_line, summary_text)
    reach the email through clean_copy alone, so the auditor's hard-fail on any
    dash form is covered here for all three. Second, humanise any raw topic slug
    left in the text (``economy_hustle`` becomes ``Hustle``) so the body never
    prints an underscore slug. Applied to every brief TEXT field at render time;
    never to handles, urls, or the mockup's own static copy.
    """
    text = str(s or "")
    text = re.sub(rf"\s*(?:[{_EM_DASH}{_EN_DASH}]|-{{2,}})+\s*", ", ", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    while "  " in text:
        text = text.replace("  ", " ")
    text = _TOPIC_SLUG_RE.sub(_deslug, text)
    for pat, repl in _BUZZWORD_SUB:
        text = pat.sub(lambda m, r=repl: _match_case(r, m.group(0)), text)
    return text


# Market slug -> display name. Used for the flagline ("South Africa").
_MARKET_NAMES = {"za": "South Africa", "ng": "Nigeria", "ke": "Kenya"}

# Sentiment word -> the small glyph the mockup uses in the MOOD chip.
# Positive is a filled circle, mixed is a half circle, negative an open circle.
# Colour-coded filled circle. The earlier codepoints (fisheye, etc.) are not in
# the Outlook/Word web-safe fonts and rendered as missing-glyph boxes; the plain
# filled circle renders everywhere (the channel dot meter proves it), and the
# colour carries the sentiment. Semantic colours, not brand, so hardcoded.
_MOOD_GLYPHS = {
    "positive": '<span style="color:#2f9e6b;">●</span>',
    "mixed": '<span style="color:#b8902a;">●</span>',
    "negative": '<span style="color:#d24b3a;">●</span>',
    "neutral": '<span style="color:#998e76;">●</span>',
}


def esc(value: object) -> str:
    """HTML-escape a value for safe interpolation into the email body.

    Guards against a stray angle bracket or ampersand in a creator handle,
    headline or read breaking the markup. quote=True so attribute-context
    interpolation is also safe.
    """
    return _html_escape(str(value), quote=True)


# Topic-slug category prefixes -> flagline label. The first segment of every
# taxonomy slug is a category (music_amapiano, economy_sapa_hustle). This is the
# canonical map covering every category in configs/topic_groups/{za,ng,ke}.yaml;
# card.py and radar.py import category_label so the set lives in one place.
# humanize_topic uses it to drop the category prefix only when it is a real
# category, so a non-category underscored compound the de-slug regex catches
# (cost_of_living) keeps every word instead of losing its first one. Keep this
# in sync with the taxonomy so a headline never reads "Transport matatu" (the
# prefix duplicates the flagline category) or shows a raw "Genz".
_CATEGORY_LABELS = {
    "music": "Music",
    "lifestyle": "Lifestyle",
    "economy": "Economy",
    "fintech": "Fintech",
    "finance": "Finance",
    "politics": "Politics",
    "sport": "Sport",
    "sports": "Sport",
    "fashion": "Fashion",
    "film": "Film",
    "diaspora": "Diaspora",
    "tech": "Tech",
    "food": "Food",
    "genz": "Culture",
    "education": "Education",
    "infra": "Infrastructure",
    "transport": "Transport",
    "culture": "Culture",
}


def category_label(topic: str) -> str:
    """Display category for a topic slug (``music_amapiano`` -> ``Music``).

    Falls back to the title-cased prefix for a category not in the map, and to
    an empty string for an empty slug. Shared by the card and radar flaglines.
    """
    prefix = topic.split("_")[0] if topic else ""
    return _CATEGORY_LABELS.get(prefix, prefix.title() if prefix else "")


def humanize_topic(topic: str) -> str:
    """Turn a topic slug into a readable headline fallback.

    ``music_amapiano`` becomes ``Amapiano``, ``economy_sapa_hustle`` becomes
    ``Sapa hustle``. The leading category prefix is dropped only when it is a
    real taxonomy category, because it duplicates the section's category label.
    A non-category underscored compound the de-slug regex catches
    (``cost_of_living``) keeps every word and becomes ``Cost of living`` instead
    of losing its first segment. Used as a headline fallback and by clean_copy
    to humanise any raw slug left in brief prose.
    """
    slug = str(topic or "").strip()
    if not slug:
        return ""
    parts = slug.split("_")
    body = parts[1:] if len(parts) > 1 and parts[0] in _CATEGORY_LABELS else parts
    text = " ".join(body).replace("-", " ").strip()
    if not text:
        text = " ".join(parts)
    return text[:1].upper() + text[1:]


def market_name(market: str) -> str:
    """Display name for a market slug, falling back to the upper-cased slug."""
    m = str(market or "").strip().lower()
    return _MARKET_NAMES.get(m, m.upper())


def market_color(market: str, brand: dict) -> str:
    """Per-market dot color from the brand config, with a neutral fallback."""
    m = str(market or "").strip().lower()
    colors = brand.get("market_colors") or {}
    return colors.get(m, "#736853")


def brief_topic(brief: dict) -> str:
    """The topic slug for a brief, accepting either ``topic`` or ``query_group``."""
    return str(brief.get("topic") or brief.get("query_group") or "").strip()


def brief_headline(brief: dict) -> str:
    """The display headline: the real ``headline`` field, else the humanized slug.

    Never invents copy. If neither a headline nor a topic exists the result is
    an empty string and the caller omits the element. Dash-sanitised so a model
    headline carrying an em or en dash still renders dash-free.
    """
    headline = clean_copy(str(brief.get("headline") or "")).strip()
    if headline:
        return headline
    return humanize_topic(brief_topic(brief))


def first_proof_url(brief: dict) -> str:
    """First real evidence URL from the brief's ``social_refs``, else ``""``.

    ``social_refs`` entries are ``'url | platform | title'``. The card and hero
    feet linked to a dead ``#``; the spec forbids a dead anchor. This returns
    the first parseable http(s) URL so the foot can link to real evidence, and
    an empty string when none exists so the caller omits the link entirely.
    """
    for entry in brief.get("social_refs") or []:
        first = str(entry).split("|")[0].strip()
        if first.startswith(("http://", "https://")):
            return first
    return ""


def proof_anchor(brief: dict, label: str, color: str = "#b62a12") -> str:
    """A proof link to real evidence, or plain text when no URL backs it.

    Never emits ``href="#"``: with a real ``social_refs`` URL it links out, and
    with none it returns an empty string so the foot drops the anchor rather
    than shipping a dead one. ``color`` is the inline link color (the on-paper
    ink-accent on a card, the warm accent on the dark hero foot).
    """
    url = first_proof_url(brief)
    if not url:
        return ""
    return (
        f'<a href="{esc(url)}" style="font-family:{FONT_MONO};font-size:10px;font-weight:700;'
        f'color:{color};text-decoration:none;">{label}</a>'
    )


# Raw YouTube channel IDs (UC + 22 chars) sometimes arrive as the creator
# "handle" when the channel name did not resolve upstream. They are unreadable
# (@ucvco8xhpe3imwkxab0aaj4w) and read as noise next to real handles, so
# parse_creator drops them rather than show a raw ID to a stakeholder.
_YT_CHANNEL_ID_RE = re.compile(r"^uc[a-z0-9_-]{22}$", re.IGNORECASE)


def parse_creator(entry: str) -> dict | None:
    """Split a ``'@handle | platform | N mentions'`` string into parts.

    Returns ``{"handle", "platform", "stat", "initials"}`` or None if the
    entry has no handle. The pipeline stores creators pipe-delimited; renderers
    must not guess any field that is not present, so a malformed entry yields
    None and is skipped. An unresolved YouTube channel-ID handle is also
    dropped so the drivers and cast lists never show a raw ID.
    """
    raw = str(entry or "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.split("|")]
    handle = parts[0] if parts else ""
    if not handle:
        return None
    bare = handle.lstrip("@")
    if _YT_CHANNEL_ID_RE.match(bare):
        return None
    # Drop an unresolved purely-numeric platform user-id (e.g. an Instagram
    # numeric id the connector could not map to a username). A real handle is
    # never all digits, and a raw id reads as broken in the cast list.
    if bare.isdigit() and len(bare) >= 5:
        return None
    platform = parts[1] if len(parts) > 1 and parts[1] else ""
    stat = parts[2] if len(parts) > 2 and parts[2] else ""
    return {
        "handle": handle,
        "platform": platform,
        "stat": stat,
        "initials": _initials(handle),
    }


def _initials(handle: str) -> str:
    """Two-character avatar token from a handle, mirroring the mockup avatars.

    ``@kasi_swenka`` -> ``KS``, ``@.kinanda`` -> ``.K``. Strips the leading @,
    then takes the first character of the first two alphanumeric-led segments,
    keeping a leading dot if the handle starts with one (the mockup does this).
    """
    h = handle.lstrip("@")
    if not h:
        return "?"
    segs = [s for s in h.replace(".", " ").replace("-", " ").split("_") if s]
    flat: list[str] = []
    for seg in segs:
        flat.extend(seg.split())
    if h.startswith(".") and flat:
        return ("." + flat[0][:1]).upper()
    if len(flat) >= 2:
        return (flat[0][:1] + flat[1][:1]).upper()
    if flat:
        return flat[0][:2].upper()
    return h[:2].upper()


def mood_glyph(sentiment: str) -> str:
    """Pick the MOOD glyph color for a sentiment phrase.

    Derived from the resolved ``mood_label`` so the dot color always matches
    the word printed on the chip. A plain first-substring match disagreed with
    the negation- and position-aware label (a hedged "largely neutral to
    slightly positive" rendered a green positive dot beside the label Neutral).
    Returns an empty string when no mood word is present so the caller can omit.
    """
    return _MOOD_GLYPHS.get(mood_label(sentiment).lower(), "")


# A "tone score of 0.00" in a sentiment summary means the GDELT tone signal is
# absent, not maximally negative. mood_label short-circuits these to Neutral so
# a downstream "negative" word in the same sentence never flips the MOOD dot red.
# Mirrors the zero-tone markers in email_digest.py:_sentiment_dot.
_ZERO_TONE_MARKERS = (
    "tone score of 0.00",
    "tone score of 0.0",
    "average tone score is 0.00",
    "average tone score is 0.0",
    "average tone score of 0.00",
    "average tone score of 0.0",
    "tone signal not available",
)


# Mood word -> the title-cased chip label. Negative-leaning phrases ("slightly
# negative to neutral") read as the stronger word first, so the order here puts
# negative and mixed ahead of positive to catch a hedged negative read.
_MOOD_LABELS = (
    ("negative", "Negative"),
    ("mixed", "Mixed"),
    ("neutral", "Neutral"),
    ("positive", "Positive"),
)


def mood_label(sentiment: str) -> str:
    """One-word MOOD chip value from a full sentiment sentence.

    The pipeline stores a whole sentence in ``sentiment_summary`` (``"The
    average tone score of 0.49 indicates a largely neutral to slightly positive
    sentiment"``). The chip must stay one token or it overflows the chip row, so
    this returns the single mood word. Empty when no mood word is present, so
    the caller omits the chip rather than printing the sentence.

    Resolution is negation- and position-aware, not first-substring. A mood word
    is suppressed when a negator ("no", "not", ...) sits within the two tokens
    before it, so "no significant negative risk" does not read Negative. Of the
    surviving occurrences the one nearest the head of the sentence wins, so
    "highly positive ... neutral baseline" reads Positive. ``_MOOD_LABELS`` is
    the candidate set only; its order is no longer precedence.

    Tone=0 short-circuit: when the summary references a "tone score of 0.00"
    (or 0.0), the underlying GDELT tone signal is ABSENT, not maximally
    negative. Without this guard a sentence like "average tone score of 0.00
    indicates a predominantly negative sentiment" reads Negative and flips the
    MOOD dot red on a topic with no real negative signal (the same defect
    Thapelo flagged on the v1 path, 7 May 2026). Treat any zero-tone marker as
    Neutral, mirroring ``_sentiment_dot`` in email_digest.py.
    """
    text = str(sentiment or "").lower()
    if any(marker in text for marker in _ZERO_TONE_MARKERS):
        return "Neutral"
    tokens = re.findall(r"[a-z]+", text)
    negators = {"no", "not", "without", "never", "absent", "lacking", "nor"}
    best_index = None
    best_label = ""
    for word, label in _MOOD_LABELS:
        for i, token in enumerate(tokens):
            if token != word:
                continue
            if any(prev in negators for prev in tokens[max(0, i - 2) : i]):
                continue
            if best_index is None or i < best_index:
                best_index = i
                best_label = label
            break
    return best_label


def render_bars(weight: int, pal: dict, dark: bool = False) -> str:
    """Four-dot channel-strength meter, ``weight`` dots filled.

    Plain coloured glyphs in spans, not a table: Outlook has no flex and its
    inline-table support is unreliable, but a coloured glyph span renders in
    every client. Filled dots wear the accent, empty dots the line color (dark
    variant in dark sections). Weight is clamped to 0..4.

    Strength is carried by the NUMBER of filled dots, not only by colour: the
    empty dots are a hollow circle (○), a distinct shape from the filled circle
    (●). An earlier build padded with filled circles and leaned on colour alone
    to separate filled from empty, so any client where the empty tone was not
    recessive enough (or a downscaled screenshot) collapsed every source to an
    identical "●●●●" and the meter read as dead. The hollow pad keeps the meter
    legible regardless of how the empty colour resolves.
    """
    n = max(0, min(4, int(weight)))
    empty = pal["dark_chip_line"] if dark else pal["line"]
    empty_cls = CLS["rule2"] if dark else CLS["rule"]
    filled = "●" * n  # filled circle
    rest = "○" * (4 - n)  # hollow circle, a distinct shape from the filled dot
    out = ""
    if filled:
        out += f'<span class="{CLS["accent"]}" style="color:{pal["vermillion"]};letter-spacing:2px;font-size:9px;">{filled}</span>'
    if rest:
        out += f'<span class="{empty_cls}" style="color:{empty};letter-spacing:2px;font-size:9px;">{rest}</span>'
    return out


def render_seen(channels: list, pal: dict, show_label: bool = True, dark: bool = False) -> str:
    """The full Seen-on row from a list of ``(platform, weight)`` tuples.

    Returns an empty string when there are no channels so the caller omits the
    block. Each source is an inline-block span (bar + name) so the row wraps
    without flex. Platform names are display-cased from the stored slug.
    """
    if not channels:
        return ""
    name_color = pal["on_ink_soft"] if dark else pal["ink2"]
    label = ""
    if show_label:
        label_color = pal["on_ink_mute"] if dark else pal["muted"]
        label = (
            f'<span class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:9px;text-transform:uppercase;'
            f"letter-spacing:0.12em;color:{label_color};margin-right:10px;white-space:nowrap;"
            'display:inline-block;vertical-align:middle;">Seen on</span>'
        )
    srcs = []
    for platform, weight in channels:
        name = _platform_label(str(platform))
        srcs.append(
            '<span style="display:inline-block;vertical-align:middle;margin-right:14px;'
            f'font-family:{FONT_MONO};font-size:10px;">'
            f"{render_bars(weight, pal, dark)}"
            f'<span class="{CLS["ink2"]}" style="color:{name_color};font-weight:500;margin-left:5px;'
            f'vertical-align:middle;">{esc(name)}</span></span>'
        )
    return f'<div style="line-height:1.9;">{label}{"".join(srcs)}</div>'


# Platform slug -> display name for the Seen-on row and footer.
_PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "threads": "Threads",
    "reddit": "Reddit",
    "youtube": "YouTube",
    "brand24": "Brand24",
    "apple_music": "Apple Music",
    "apple music": "Apple Music",
    "google_trends": "Google Trends",
    "bigquery_trends": "Google Trends",
    "gdelt": "GDELT news",
    "news": "News",
    "rss": "News",
    "web": "Web",
    "social": "Social",
    "google_search": "Google Search",
    "linkedin": "LinkedIn",
    "bluesky": "Bluesky",
}


def _platform_label(platform: str) -> str:
    p = platform.strip().lower()
    if p in _PLATFORM_LABELS:
        return _PLATFORM_LABELS[p]
    # Underscore slugs read as "Google_Search" under a bare title(); swap to a
    # space first so an unmapped channel still renders as clean display text.
    return platform.strip().replace("_", " ").title() if platform.strip() else "Web"


def chip(
    key: str, value: str, pal: dict, hot: bool = False, glyph: str = "", dark: bool = False
) -> str:
    """A single labelled chip, the ``.chip`` element from the mockup.

    An inline-block span with an inline border so the chip row wraps without
    flex. ``hot`` wears the accent border + text; on a dark section the resting
    chip uses the dark border + light text. ``glyph`` is an optional fixed HTML
    character reference (from ``_MOOD_GLYPHS``) emitted raw before the escaped
    value so the mood symbol renders as a symbol; the value itself is escaped.
    """
    if dark:
        border = pal["vermillion"] if hot else pal["dark_chip_line"]
        text = pal["on_ink_warm"] if hot else pal["on_ink_soft"]
        key_color = pal["on_ink_mute"]
    else:
        border = pal["vermillion"] if hot else pal["line"]
        text = pal["vermillion_ink"] if hot else pal["ink2"]
        key_color = pal["muted"]
    outer_cls = CLS["accent"] if hot else f"{CLS['chip']} {CLS['ink2']}"
    prefix = f"{glyph} " if glyph else ""
    return (
        f'<span class="{outer_cls}" style="display:inline-block;'
        f"font-family:{FONT_MONO};font-size:10px;font-weight:700;letter-spacing:0.03em;"
        f"padding:5px 8px;border:1px solid {border};color:{text};"
        'margin:0 7px 7px 0;white-space:nowrap;">'
        f'<span class="{CLS["mute"]}" style="color:{key_color};font-weight:500;">{esc(key)}</span> '
        f"{prefix}{esc(value)}</span>"
    )
