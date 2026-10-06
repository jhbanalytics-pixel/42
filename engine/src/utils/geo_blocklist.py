"""Shared geo-collision blocklist + foreign-language detectors.

Centralised so the brief generator (sample-pull / social-refs / creators)
and the topic classifier (enrichment-step) both apply identical filtering
against the same data. Previously the data + functions lived inside
``src/analysis/generate_briefs.py``; extracted 28 May 2026 after a Sapa
Vietnam tourism row reached ``enriched_content.topic_groups`` carrying the
``economy_sapa_hustle`` tag despite the brief-pull filter catching it
downstream. The fix lifts the same logic up to classification time so the
bad row never gets the collision-prone tag in the first place.

Data shape
==========
``TOPIC_GEO_BLOCKLIST`` maps a topic_group name to a list of substring
markers. A row whose text or url contains any of those substrings
(case-insensitive, Unicode-quote-normalised) is treated as a foreign
collision and dropped for that topic.

``NON_SSA_SCRIPT_RANGES`` and ``FOREIGN_LATIN_CHARSETS`` back the two
script + density backstops that catch contamination the keyword list
cannot cover (Hindi Devanagari, Russian Cyrillic, CJK, Turkish
s-cedilla / g-breve density, Vietnamese d-stroke / a-breve density).

Public API
==========
- ``row_matches_geo_blocklist(row, topic_group)``: legacy row-dict shape
  used by the brief generator (sample dict carries title/text/url keys).
- ``text_matches_geo_blocklist(text, topic_group)``: raw-string shape
  used by the topic classifier (single concatenated haystack).

Both layer the same three checks:
1. Substring match against ``TOPIC_GEO_BLOCKLIST`` after Unicode quote
   normalization.
2. Non-SSA script backstop on the raw text.
3. Foreign-Latin density backstop on the raw text.

Topics not present in ``TOPIC_GEO_BLOCKLIST`` are not filtered. The data
list is conservative; pure "ankara" without context is NOT blocked
because it is the legitimate NG fabric name.
"""

from __future__ import annotations

from typing import Any

# Per-topic blocklist for sample rows whose slang collides with a foreign
# place / brand and contaminates the brief. Each value is a list of
# substrings; a row is dropped when any substring appears in its title,
# text, or url (case-insensitive). Curated from observed brief failures
# during the 7 May to 28 May 2026 cron observation window.
TOPIC_GEO_BLOCKLIST: dict[str, list[str]] = {
    "economy_sapa_hustle": [
        # Vietnam Sapa town (tourist destination)
        "vietnam",
        "sapavietnam",
        "sapa vietnam",
        "fansipan",
        "sapa town",
        "sailingsapa",
        "vietnamexpress",
        "sapa hotel",
        "sapa hotels",
        "sapa train",
        "sapa view",
        "sapa trek",
        "sapa tour",
        "sapatour",
        "sapavibes",
        "sapa mountain",
        # 8 May 2026 round-6: Vietnamese tourist destination "Cat Cat
        # Village" (a Sapa-area attraction) leaked into NG economy brief.
        "cat cat village",
        "cat cat",
        "muong hoa",
        "fansipan legend",
        # 8 May 2026 round-6 (extender pass): geo-collision-extender
        # a review surfaced 9 addressable Vietnam-Sapa survivors over
        # last 7 days that the round-1 to round-5 list had missed.
        "lao cai",
        "laocai",
        "ta phin",
        "ta van village",
        "red dao",
        "topas ecolodge",
        "hanoi",
        "phuquoc",
        "hochinminhcity",
        # Bahasa Indonesian / Malay where "sapa" = "who" or "siapa"
        "sapa mau",
        "sapa org",
        "sapa ni",
        "sapa yang",
        "sapa tu",
        "sapa ajar",
        "sapa dah",
        "sapa dah tryy",
        "korg rasa",
        "wasap",
        "langkawi",
        # Philippines "sapa" (creek)
        "bohol",
        "danicop",
        # India "सपा" (Samajwadi Party initialism) transliterates as
        # "sapa" in English-language Indian political content.
        "samajwadi",
        "samajwadi party",
        "uttar pradesh",
        "upelection",
        "up election",
        "yogi sarkar",
        "yogi government",
        "modi sarkar",
        "kisan",
        "vidhayak",  # Hindi for MLA
        "jhansi",
        "lucknow",
        "kanpur",
        "hapur",
        "akhilesh",
        # Generic foreign-tourism captions seen in Sapa contamination
        "rotting here",
        "best place ever",
        "hidden gem",
        "trekking",
        "backpacking",
        "backpacker",
        # 28 May 2026 round-9 (extender pass): single SUSPECT row carrying
        # "Travel experiences and tourism destinations including Sapa
        # Vietnam, scenic landscapes, mountains, and tour packages"
        # reached enriched_content because the round-1 to round-8
        # markers were applied at brief-sample time only. Adding the
        # high-yield travel-bundle tokens that pair with bare "Sapa".
        "tourism destinations",
        "scenic landscapes",
        "tour packages",
        "travel experiences",
        "travel destinations",
        # San Antonio Pets Alive! (Texas, USA shelter) abbreviates as
        # "SAPA".
        "san antonio",
        "pets alive",
        "adoption fees",
        "adoption fee",
        "spay",
        "neuter",
        "adoption drive",
        "rescue dog",
        "rescue cat",
        # German cultural-assessment tool "SAPA: Das Selbstanalyse-Tool
        # fur die darstellenden Kunste".
        "selbstanalyse",
        "darstellenden",
        "kulturbereich",
        "klimakrise",
        "green culture festival",
        "karlsruhe",
        # Bahasa Malay "sapa" = "who". Common informal post on Threads.
        "nak pm",
        "nak mp",
        "nak random",
        "nak text",
        # Brazilian Portuguese LGBT slang "sapa" = lesbian.
        "assistente social",
        "lésbicas",
        "lesbicas",
        "LGBT+",
        # Filipino "#sapa" with adventure tags.
        "buwisbuhay",
        "bridgeontheclouds",
        # Vietnamese tour / travel hashtag clusters.
        "tour sapa",
        "len nui",  # "lên núi" stripped of diacritics
        "lên núi",  # "to the mountain", common SE Asian travel phrase
        "ta xua",
        "tà xùa",
        "ha noi",  # variant of "hanoi"
        "hà nội",
        "3n2d",  # tour duration shorthand "3 nights 2 days"
        "3n2đ",  # diacritic variant
        "2n1d",
        "tour du lich",  # Vietnamese "tour du lịch" = "tour travel"
        "tour du lịch",
        # 8 Jun 2026 round-10: GDELT GKG theme rows from Indian + Turkish news
        # sources carry the economy_sapa_hustle tag (contamination at
        # classification; did not reach the email this window but should be
        # stripped). Source-domain markers, validated zero-collateral.
        "businesstoday.in",
        "techstory.in",
        "ianslive.in",
        "sozcu.com.tr",
    ],
    "fashion_ankara_asoebi": [
        # Turkey (Ankara is the capital)
        "turkey",
        "türkiye",
        "ankara turkey",
        "ankara, turkey",
        "ankara/turkey",
        "ankara türkiye",
        "istanbul",
        "ankaramekan",
        "keşfet",
        "#ankaratürkiye",
        # Turkey flag emoji
        "🇹🇷",
        # Turkish district / lake / neighbourhood names.
        "gölbaşı",  # noqa: RUF001
        "gölbası",  # noqa: RUF001
        "mogangölü",
        "mogan",
        "angara",
        # Turkish locative + possessive markers
        "ankara'da",
        "ankara'nın",  # noqa: RUF001
        "ankara'ya",
        "i̇şe gidiyorum",
        "asker gecesi",
        "dostlarkonağı",  # noqa: RUF001
        "doğa",
        "şehri",
        "şehirde",
        "şu şekil",
        "ankara mekan",
        "ankara hayat",
        "ankaratiktok",
        "türk",
        # Common Turkish content phrasings. "tbt" (#tbt, Throwback Thursday)
        # was removed 16 Jun 2026: it is a global hashtag heavily used on NG
        # asoebi/fashion throwback posts, so substring-matching it stripped
        # legitimate fashion_ankara_asoebi rows. The Turkish script/density
        # backstops plus the Ankara place + venue markers still catch real
        # Turkey-the-capital contamination without it.
        "yaşa",
        "yaşam",
        "günlük",
        "kesfet",
        "kesfetdoga",
        "ne var ki",
        "var iste",
        "pavyon",
        # Common Turkish neighbourhood / venue references
        "yenimahalle",
        "ydacenter",
        "söğütözü",
        "kübana",
        "armada",
        # Turkish school / cultural keywords
        "okul gezileri",
        "okulgezileri",
        "iscalling",
        # Turkey-specific hashtags
        "#06",
        "#ankaragezilecek",
        # 8 May 2026 round-7 (extender pass): Ankara districts.
        "çankaya",
        "cankaya",
        "kızılay",  # noqa: RUF001
        "kizilay",
        "sincan",
        "keçiören",
        "kecioren",
        "dikmen",
        "eryaman",
        "bağlum",
        "baglum",
        # Ankara landmarks.
        "atakule",
        "kocatepe",
        "anıtkabir",  # noqa: RUF001
        "anitkabir",
        "kocatepecamii",
        # Ankara restaurants / venues / brands.
        "aspava",
        "kılıç aspava",  # noqa: RUF001
        # Ankara political figures + parties.
        "mansur yavaş",
        "mansur yavas",
        "milliyetçi hareket partisi",
        # Ankara football club.
        "gençlerbirliği",
        "genclerbirligi",
        # Ankara real estate hashtag clusters.
        "ankaragayrimenkul",
        "ankaraemlak",
        "emlakdanışmanlığı",  # noqa: RUF001
        "emlakdanismanligi",
        "lansmanfırsatı",  # noqa: RUF001
        "lansmanfirsati",
        # 30 May 2026 round-10: two NG fashion_ankara_asoebi reference rows
        # leaked Turkey-the-country (Ankara the capital) past the list because
        # they used plain ASCII with no Turkish diacritics. Row 1 EnsembleData
        # "#nevada #ankara #karanfil"; row 2 Reddit r/AskTurkey "drive an RV
        # from Dubai to Ankara, Turkish + Azerbaijani passports". These anchor
        # the foreign travel/passport context that never co-occurs with real
        # asoebi/owambe/gele fabric content. Bare "dubai" is deliberately NOT
        # added (aspirational NG reference); the multiword "dubai to ankara"
        # is used instead. Bare "turkey" IS in this list (added above with the
        # other Ankara-the-capital markers) since it collides with the fabric.
        "turkish",
        "azerbaijan",
        "azerbaijani",
        "karanfil",
        "nevada",
        "passport",
        "dubai to ankara",
        "r/askturkey",
        "askturkey",
        "r/askmiddleeast",
        # 8 Jun 2026 round-11: SIX Turkey-tourism social_refs shipped into the
        # SENT NG ankara card (Ankara the fabric vs Ankara the Turkish capital).
        # Reported: "Hacibayram'dan Ankara Kalesi" (Ankara Castle), a "#travel
        # #turkiye" TikTok, a Dr. Civas hair-transplant clinic, a Turkey
        # restaurant. Root cause: the list carried only the umlaut "türkiye";
        # the data uses ASCII "turkiye", and the script/density backstops miss
        # short refs (1 Turkish-distinctive char). Validated by the
        # geo-collision-extender: zero collateral on real asoebi/owambe/gele.
        "turkiye",
        "kalesi",  # Turkish "castle" (Ankara Kalesi = Ankara Castle)
        "hacıbayram",  # noqa: RUF001  (Ankara district; dotless-i form in data)
        "hacibayram",
        "antalya",
        "izmir",
        "lokantasi",  # Turkish "restaurant"
        "gezilecekyerler",  # Turkish "places to visit"
        "dr. civas",  # Ankara hair-transplant clinic (medical-tourism collision)
    ],
    # 11 Jun 2026: tech_gemini_ai ships with "gemini" as a core keyword,
    # which collides with the zodiac sign in horoscope and astrology
    # content worldwide. Strip the astrology context at classification
    # time so only Google Gemini product conversation carries the tag.
    "tech_gemini_ai": [
        # 11 Jun shadow QA: bare "gemini" also collides with the NASA
        # programme, a neurosurgery charity, the observatory, and a manhwa
        # character inside GDELT org-dump rows. Deny the non-product senses.
        "mission gemini",
        "project gemini",
        "nasa",
        "gemini untwined",
        "gemini observatory",
        "gemini man",
        "manhwa",
        "account for sale",
        "horoscope",
        "astrology",
        "zodiac",
        "star sign",
        "starsign",
        "tarot",
        "birth chart",
        "birthchart",
        "gemini season",
        "gemini man",
        "gemini woman",
        "gemini rising",
        "gemini moon",
        "gemini sun",
        "gemini energy",
        "gemini traits",
        "#geminiseason",
        "#zodiacsigns",
        "#astrologytiktok",
        # 17 Jun 2026 (KE precision): non-product "gemini" senses that reached
        # the KE topic. Foreign music celebrities, a bus model, a handbag.
        "young gemini",  # Zimbabwean / Shona artist
        "hung huynh",  # Vietnamese singer "Gemini Hung Huynh"
        "gemini hung huynh",
        "#geminihunghuynh",
        "wright gemini",  # double-decker bus model (Wright Gemini 2)
        "gemini link tote",  # Tory Burch handbag
        "tory burch",
    ],
}


# The haystack is lowercased before the substring check, so any blocklist
# entry carrying an uppercase character (e.g. "LGBT+") could never match and
# was dead. Lowercase every entry once at module load so the data stays
# readable above while the matcher works regardless of casing.
TOPIC_GEO_BLOCKLIST = {
    topic: [marker.lower() for marker in markers] for topic, markers in TOPIC_GEO_BLOCKLIST.items()
}


# Unicode script blocks for languages SSA content does NOT use. Any row
# with `threshold` or more characters from these blocks is foreign noise
# regardless of keyword match. Catches Hindi / Russian / Chinese / Thai.
NON_SSA_SCRIPT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0900, 0x097F),  # Devanagari
    (0x0400, 0x04FF),  # Cyrillic
    (0x4E00, 0x9FFF),  # CJK Unified
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul
    (0x0E00, 0x0E7F),  # Thai
)


# Distinctive Latin-extended character sets per foreign language. These
# are characters overwhelmingly used in ONE foreign language and rarely
# in SSA content. Catches Turkish + Vietnamese with-diacritics
# contamination that the script-block check cannot see.
FOREIGN_LATIN_CHARSETS: tuple[tuple[str, str, int], ...] = (
    # Turkish: s-cedilla (lower + upper), g-breve (lower + upper),
    # dotless i (lower) and capital dotted i.
    ("turkish", "ŞşĞğİı", 3),
    # Vietnamese: Đ (d-stroke), đ, ă (a-breve), Ă, ơ (o-horn), Ơ, ư
    # (u-horn), Ư.
    ("vietnamese", "ĐđĂăƠơƯư", 3),
)


def normalize_haystack_for_geo_match(s: str) -> str:
    """Lowercase and fold Unicode smart-quotes to ASCII for matching."""
    # Unicode quote characters are intentional inputs to .replace();
    # ruff RUF001 flags them as ambiguous which is the point.
    return (
        s.lower()
        .replace("’", "'")  # noqa: RUF001  (U+2019 right single quotation mark)
        .replace("‘", "'")  # noqa: RUF001  (U+2018 left single quotation mark)
        .replace("ʼ", "'")  # noqa: RUF001  (U+02BC modifier letter apostrophe)
        .replace("”", '"')  # U+201D right double quotation mark
        .replace("“", '"')  # U+201C left double quotation mark
    )


def has_non_ssa_script(s: str, threshold: int = 5) -> bool:
    """True when `s` carries at least `threshold` non-SSA-script characters."""
    count = 0
    for ch in s:
        cp = ord(ch)
        for lo, hi in NON_SSA_SCRIPT_RANGES:
            if lo <= cp <= hi:
                count += 1
                if count >= threshold:
                    return True
                break
    return False


def has_foreign_latin_density(s: str) -> str | None:
    """Return language name (turkish / vietnamese) if `s` carries enough
    distinctive characters from a foreign Latin-extended set, else None.
    """
    for lang, chars, threshold in FOREIGN_LATIN_CHARSETS:
        count = 0
        for ch in s:
            if ch in chars:
                count += 1
                if count >= threshold:
                    return lang
    return None


def text_matches_geo_blocklist(text: str, topic_group: str) -> bool:
    """Three-layer geo filter for a raw text string.

    Used at classification time where the caller has a single concatenated
    haystack (title + text + hashtags) rather than a structured row.

    Returns True when the text references a foreign-collision geo for
    this topic_group. Topics not in TOPIC_GEO_BLOCKLIST return False.

    Layers:
    1. Non-SSA script backstop on raw text (case-independent).
    2. Foreign-Latin density backstop on raw text.
    3. Substring match against keyword blocklist after smart-quote
       normalisation and lowercase.
    """
    if topic_group not in TOPIC_GEO_BLOCKLIST:
        return False
    raw = text or ""
    if has_non_ssa_script(raw):
        return True
    if has_foreign_latin_density(raw):
        return True
    blockers = TOPIC_GEO_BLOCKLIST[topic_group]
    haystack = normalize_haystack_for_geo_match(raw)
    return any(b in haystack for b in blockers)


def row_matches_geo_blocklist(row: dict[str, Any], topic_group: str) -> bool:
    """Three-layer geo filter for a row dict with title/text/url keys.

    Thin wrapper around ``text_matches_geo_blocklist`` that builds the
    haystack from a row dict. Used by the brief generator at sample-pull,
    top-creators-pull, and social_refs-format steps.
    """
    if topic_group not in TOPIC_GEO_BLOCKLIST:
        return False
    raw = " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("text") or ""),
            str(row.get("url") or ""),
        ]
    )
    return text_matches_geo_blocklist(raw, topic_group)


__all__ = [
    "FOREIGN_LATIN_CHARSETS",
    "NON_SSA_SCRIPT_RANGES",
    "TOPIC_GEO_BLOCKLIST",
    "has_foreign_latin_density",
    "has_non_ssa_script",
    "normalize_haystack_for_geo_match",
    "row_matches_geo_blocklist",
    "text_matches_geo_blocklist",
]
