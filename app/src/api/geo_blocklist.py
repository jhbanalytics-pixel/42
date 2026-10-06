"""Shared geo-collision blocklist + foreign-language detectors.

Vendored verbatim from the Trends Engine so 42 applies the
same geo filtering the engine applies at classification time. The engine is
read-only context; this is a frozen copy, not an import across repos.

Three layers:
1. Substring match against TOPIC_GEO_BLOCKLIST after Unicode quote
   normalization (topic-specific).
2. Non-SSA script backstop on the raw text (topic-independent).
3. Foreign-Latin density backstop on the raw text (topic-independent).

Topics not present in TOPIC_GEO_BLOCKLIST are not keyword-filtered, but the
two script/density backstops apply to any text regardless of topic.
"""

from __future__ import annotations

# Per-topic blocklist for rows whose slang collides with a foreign place or
# brand. Each value is a list of substrings; a row is dropped when any
# substring appears in its text or url (case-insensitive). Curated from
# observed engine brief failures over the 7 May to 8 Jun 2026 window.
TOPIC_GEO_BLOCKLIST: dict[str, list[str]] = {
    "economy_sapa_hustle": [
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
        "cat cat village",
        "cat cat",
        "muong hoa",
        "fansipan legend",
        "lao cai",
        "laocai",
        "ta phin",
        "ta van village",
        "red dao",
        "topas ecolodge",
        "hanoi",
        "phuquoc",
        "hochinminhcity",
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
        "bohol",
        "danicop",
        "samajwadi",
        "samajwadi party",
        "uttar pradesh",
        "upelection",
        "up election",
        "yogi sarkar",
        "yogi government",
        "modi sarkar",
        "kisan",
        "vidhayak",
        "jhansi",
        "lucknow",
        "kanpur",
        "hapur",
        "akhilesh",
        "rotting here",
        "best place ever",
        "hidden gem",
        "trekking",
        "backpacking",
        "backpacker",
        "tourism destinations",
        "scenic landscapes",
        "tour packages",
        "travel experiences",
        "travel destinations",
        "san antonio",
        "pets alive",
        "adoption fees",
        "adoption fee",
        "spay",
        "neuter",
        "adoption drive",
        "rescue dog",
        "rescue cat",
        "selbstanalyse",
        "darstellenden",
        "kulturbereich",
        "klimakrise",
        "green culture festival",
        "karlsruhe",
        "nak pm",
        "nak mp",
        "nak random",
        "nak text",
        "assistente social",
        "lésbicas",
        "lesbicas",
        "lgbt+",
        "buwisbuhay",
        "bridgeontheclouds",
        "tour sapa",
        "len nui",
        "lên núi",
        "ta xua",
        "tà xùa",
        "ha noi",
        "hà nội",
        "3n2d",
        "3n2đ",
        "2n1d",
        "tour du lich",
        "tour du lịch",
        "businesstoday.in",
        "techstory.in",
        "ianslive.in",
        "sozcu.com.tr",
        # Minecraft survival-server ("SMP") content that collides with the
        # bare "sapa" token in this product's free-text search.
        "minecraft",
        "unstable smp",
        "hardcore smp",
    ],
    "fashion_ankara_asoebi": [
        "turkey",
        "türkiye",
        "ankara turkey",
        "ankara, turkey",
        "ankara/turkey",
        "ankara türkiye",
        "istanbul",
        "ankaramekan",
        "keşfet",
        "🇹🇷",
        "gölbaşı",
        "gölbası",
        "mogangölü",
        "mogan",
        "angara",
        "ankara'da",
        "ankara'nın",
        "ankara'ya",
        "asker gecesi",
        "ankara mekan",
        "ankara hayat",
        "ankaratiktok",
        "türk",
        "pavyon",
        "yenimahalle",
        "söğütözü",
        "kübana",
        "okul gezileri",
        "okulgezileri",
        "#ankaragezilecek",
        "çankaya",
        "cankaya",
        "kızılay",
        "kizilay",
        "sincan",
        "keçiören",
        "kecioren",
        "dikmen",
        "eryaman",
        "atakule",
        "kocatepe",
        "anıtkabir",
        "anitkabir",
        "aspava",
        "mansur yavaş",
        "mansur yavas",
        "gençlerbirliği",
        "genclerbirligi",
        "ankaragayrimenkul",
        "ankaraemlak",
        "turkish",
        "azerbaijan",
        "azerbaijani",
        "passport",
        "dubai to ankara",
        "r/askturkey",
        "askturkey",
        "r/askmiddleeast",
        "turkiye",
        "kalesi",
        "hacıbayram",
        "hacibayram",
        "antalya",
        "izmir",
        "lokantasi",
        "gezilecekyerler",
        "dr. civas",
    ],
}


NON_SSA_SCRIPT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0900, 0x097F),  # Devanagari
    (0x0400, 0x04FF),  # Cyrillic
    (0x4E00, 0x9FFF),  # CJK Unified
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul
    (0x0E00, 0x0E7F),  # Thai
)


FOREIGN_LATIN_CHARSETS: tuple[tuple[str, str, int], ...] = (
    ("turkish", "ŞşĞğİı", 3),
    ("vietnamese", "ĐđĂăƠơƯư", 3),
)


def normalize_haystack_for_geo_match(s: str) -> str:
    """Lowercase and fold Unicode smart-quotes to ASCII for matching."""
    return (
        s.lower()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("ʼ", "'")
        .replace("”", '"')
        .replace("“", '"')
    )


def has_non_ssa_script(s: str, threshold: int = 5) -> bool:
    """True when s carries at least threshold non-SSA-script characters."""
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
    """Return the language name if s carries enough distinctive characters
    from a foreign Latin-extended set, else None.
    """
    for lang, chars, threshold in FOREIGN_LATIN_CHARSETS:
        count = 0
        for ch in s:
            if ch in chars:
                count += 1
                if count >= threshold:
                    return lang
    return None


def has_foreign_script(text: str) -> bool:
    """Topic-independent backstop: True for non-SSA-script or foreign-Latin
    density. Safe to apply to any search query.
    """
    raw = text or ""
    return has_non_ssa_script(raw) or has_foreign_latin_density(raw) is not None


def text_matches_geo_blocklist(text: str, topic_group: str) -> bool:
    """Three-layer geo filter for a raw text string.

    Returns True when the text references a foreign-collision geo for this
    topic_group. The two script/density backstops fire for any topic; the
    keyword layer only fires for topics present in TOPIC_GEO_BLOCKLIST.
    """
    raw = text or ""
    if has_non_ssa_script(raw):
        return True
    if has_foreign_latin_density(raw):
        return True
    if topic_group not in TOPIC_GEO_BLOCKLIST:
        return False
    blockers = TOPIC_GEO_BLOCKLIST[topic_group]
    haystack = normalize_haystack_for_geo_match(raw)
    return any(b in haystack for b in blockers)
