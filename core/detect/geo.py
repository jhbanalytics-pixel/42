"""Where a post was made: the per-platform geo recipe (SOURCES.md, TRUST.md A5 and G6).

geo_for_post(platform, market, *, ext_region=None, home_market=None,
             profile_location=None, text=None, language=None)
    returns (geo_market, geo_confidence, geo_source) for one posts row. geo_market is an
    upper-case ISO 3166 alpha-2 code or None; market is the market the post was
    collected for (ZA, NG or KE, any case). The first step that resolves wins:

    1. ext_region, TikTok only: TikTok's own region code. 0.9, source ext_region.
    2. The creator: home_market (an alpha-2 code, or a country name or alias listed in
       COUNTRIES such as "South Africa" or "Naija"; any other string is ignored) or the
       profile location text read with the gazetteer below, where a country name or a
       flag counts. 0.8, source home_market. If the two disagree the result is unknown.
    3. Place mentions in the post text: cities, provinces and safe local nicknames for
       ZA, NG and KE, and neighbour-leak lists that point elsewhere (Ghana and Cameroon
       for NG; Tanzania and Uganda for KE; Zimbabwe, Lesotho and Botswana for ZA). All
       lists are read for every post. 0.7, source place_mention. Two countries named
       gives unknown. A country name, demonym or flag never places a post by itself,
       but it does count toward a conflict, so "Nigeria vs Ghana, Lagos wins" is unknown.
    4. language (a code such as pcm, sw, sheng, zu, or a list of them; "zu-ZA" reads as
       zu): if one belongs to the collection market, (market, 0.3, "language"). Never a
       known location. Skipped when the text names a country other than the market.

    Unknown is (None, None, None), never a default market and never a zero.

    Raw JSON values are safe: a profile_location, text, ext_region or home_market that
    is not a string reads as absent, and so does a language that is neither a string nor
    a list or tuple (non-string entries in a list are dropped). Only a market outside
    ZA, NG and KE raises, with ValueError.

    Names that collide are left out of the gazetteer (Benin, Delta, East London,
    Eastleigh, Meru, Tema, Kimberley and others), and names are matched as whole words
    with accents and apostrophes folded, so "#Lagos" and "Jo'burg" count and
    "#lagosnights" does not; a multi-word name also matches written as one word
    ("#CapeTown"). Text or a profile location that reads as foreign gives no place at
    all: the collision markers lifted from engine/src/utils/geo_blocklist.py (Portugal
    for Lagos, Vietnam and Hanoi for Sapa, Turkey and Istanbul for Ankara, and similar),
    five or more characters of a script the markets do not write, or three or more
    Turkish or Vietnamese letters.

is_known(conf) is True at 0.7 or more, the threshold A5 and G6 use for local_share.
is_local(geo_market, conf, market) is True when the location is known and is market.

Standard library only, so the collect job can import it without the old engine.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

MARKETS = ("ZA", "NG", "KE")
KNOWN = 0.7

# Cities, provinces and safe local nicknames. Written folded: lower case, no accents,
# no apostrophes. A space also matches a hyphen, an underscore or nothing. GH and CM
# leak into NG, TZ and UG into KE, ZW, LS and BW into ZA (SOURCES.md).
PLACES = {
    "ZA": (
        "johannesburg", "joburg", "jozi", "egoli", "pretoria", "tshwane", "pitori",
        "cape town", "kaapstad", "durban", "ethekwini", "soweto", "gqeberha",
        "port elizabeth", "bloemfontein", "polokwane", "mbombela", "nelspruit",
        "pietermaritzburg", "rustenburg", "mahikeng", "stellenbosch", "sandton",
        "khayelitsha", "umlazi", "tembisa", "mamelodi", "gugulethu", "mitchells plain",
        "gauteng", "kwazulu natal", "kzn", "western cape", "eastern cape",
        "northern cape", "limpopo", "mpumalanga",
    ),
    "NG": (
        "lagos", "lasgidi", "las gidi", "abuja", "kano", "ibadan", "port harcourt",
        "enugu", "kaduna", "benin city", "lekki", "ikeja", "surulere", "yaba", "ikorodu",
        "ajegunle", "festac", "onitsha", "owerri", "calabar", "abeokuta", "uyo", "warri",
        "asaba", "ilorin", "zaria", "maiduguri", "sokoto", "akure", "osogbo", "anambra",
        "edo state", "ogun state", "oyo state", "imo state", "rivers state",
        "delta state", "cross river",
    ),
    "KE": (
        "nairobi", "kanairo", "mombasa", "kisumu", "nakuru", "eldoret", "thika",
        "machakos", "nyeri", "malindi", "kitale", "garissa", "kakamega", "naivasha",
        "kiambu", "kibera", "kilifi", "lamu", "kisii", "nanyuki", "ruiru", "kitengela",
        "kasarani", "githurai", "kawangware", "rongai",
    ),
    "GH": ("accra", "kumasi", "takoradi", "sekondi", "cape coast", "kasoa", "east legon"),
    "CM": ("douala", "yaounde", "bamenda", "buea", "garoua"),
    "TZ": ("dar es salaam", "dodoma", "arusha", "zanzibar", "mwanza", "mbeya", "morogoro", "kariakoo"),
    "UG": ("kampala", "entebbe", "jinja", "gulu", "mbarara", "wakiso", "ntinda", "kololo"),
    "ZW": ("harare", "bulawayo", "mutare", "gweru", "masvingo", "chitungwiza"),
    "LS": ("maseru", "mafeteng", "leribe"),
    "BW": ("gaborone", "francistown", "maun", "palapye"),
}

# Country names, demonyms and country nicknames: they place a creator's profile but
# only ever create a conflict in post text. Flags are matched separately.
COUNTRIES = {
    "ZA": ("south africa", "south african", "south africans", "mzansi"),
    "NG": ("nigeria", "nigerian", "nigerians", "naija", "9ja"),
    "KE": ("kenya", "kenyan", "kenyans"),
    "GH": ("ghana", "ghanaian", "ghanaians"),
    "CM": ("cameroon", "cameroonian", "cameroun"),
    "TZ": ("tanzania", "tanzanian", "tanzanians"),
    "UG": ("uganda", "ugandan", "ugandans"),
    "ZW": ("zimbabwe", "zimbabwean", "zimbabweans"),
    "LS": ("lesotho",),
    "BW": ("botswana",),
}

# Language codes that suggest a market. Suggest only: 0.3, never known.
LANGUAGES = {
    "ZA": ("zu", "xh", "af", "st", "tn", "nso", "ts", "ss", "ve", "nr"),
    "NG": ("pcm", "yo", "ha", "ig"),
    "KE": ("sw", "sheng"),
}

# Place and country markers lifted from TOPIC_GEO_BLOCKLIST in
# engine/src/utils/geo_blocklist.py, plus the collisions of this gazetteer (Lagos in
# Portugal, Kano in Japan). Folded like the text and matched as substrings, as there.
COLLISIONS = (
    "vietnam", "hanoi", "fansipan", "lao cai", "laocai", "phuquoc",
    "hochinminhcity", "langkawi", "bohol", "uttar pradesh", "lucknow", "kanpur",
    "jhansi", "hapur", "san antonio", "karlsruhe", "turkey", "turkiye", "istanbul",
    "antalya", "izmir", "cankaya", "kizilay", "kızılay", "kecioren", "anitkabir",
    "anıtkabir", "portugal", "algarve", "japan",
)

# Script blocks the three markets do not write, and letters dense in Turkish and
# Vietnamese, both from engine/src/utils/geo_blocklist.py.
FOREIGN_SCRIPTS = (
    (0x0900, 0x097F), (0x0400, 0x04FF), (0x4E00, 0x9FFF), (0x3040, 0x309F),
    (0x30A0, 0x30FF), (0xAC00, 0xD7AF), (0x0E00, 0x0E7F),
)
FOREIGN_SCRIPT_MIN = 5
FOREIGN_LETTERS = ("ŞşĞğİı", "ĐđĂăƠơƯư")
FOREIGN_LETTER_MIN = 3

_APOSTROPHES = str.maketrans("", "", "'’‘ʼ`")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.translate(_APOSTROPHES)


def _pattern(names: Iterable[str]) -> re.Pattern:
    alts = sorted((r"[\s_-]*".join(map(re.escape, n.split())) for n in names), key=len, reverse=True)
    return re.compile(r"(?<![^\W_])(?:" + "|".join(alts) + r")(?![^\W_])")


def _flag(code: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


_PLACE_RE = {code: _pattern(names) for code, names in PLACES.items()}
_COUNTRY_RE = {code: _pattern(names) for code, names in COUNTRIES.items()}
_FLAGS = {code: _flag(code) for code in COUNTRIES}
_COUNTRY_NAMES = {name: code for code, names in COUNTRIES.items() for name in names}
_COLLISIONS = tuple(_fold(m) for m in COLLISIONS)
_ALPHA2 = re.compile(r"[A-Za-z]{2}")


def _foreign(raw: str) -> bool:
    folded = _fold(raw)
    if any(marker in folded for marker in _COLLISIONS):
        return True
    script = sum(1 for ch in raw if any(lo <= ord(ch) <= hi for lo, hi in FOREIGN_SCRIPTS))
    if script >= FOREIGN_SCRIPT_MIN:
        return True
    return any(sum(ch in letters for ch in raw) >= FOREIGN_LETTER_MIN for letters in FOREIGN_LETTERS)


def _mentions(raw: str) -> tuple[set[str], set[str]] | None:
    """Countries named by a place, and countries named outright; None if it reads foreign."""
    if _foreign(raw):
        return None
    folded = _fold(raw)
    places = {code for code, rx in _PLACE_RE.items() if rx.search(folded)}
    countries = {code for code, rx in _COUNTRY_RE.items() if rx.search(folded)}
    countries |= {code for code, flag in _FLAGS.items() if flag in raw}
    return places, countries


def _alpha2(value: object) -> str | None:
    if isinstance(value, str) and _ALPHA2.fullmatch(value.strip()):
        return value.strip().upper()
    return None


def _country(value: object) -> str | None:
    """An alpha-2 code, or a country name or alias from COUNTRIES; else None."""
    if not isinstance(value, str):
        return None
    return _alpha2(value) or _COUNTRY_NAMES.get(" ".join(_fold(value).split()))


def _languages(language: object) -> set[str]:
    if isinstance(language, str):
        items = [language]
    elif isinstance(language, (list, tuple)):
        items = [x for x in language if isinstance(x, str)]
    else:
        return set()
    return {re.split(r"[-_]", x.strip().casefold())[0] for x in items if x.strip()}


def geo_for_post(
    platform: str | None,
    market: str,
    *,
    ext_region: str | None = None,
    home_market: str | None = None,
    profile_location: str | None = None,
    text: str | None = None,
    language: str | Iterable[str] | None = None,
) -> tuple[str | None, float | None, str | None]:
    market = str(market).strip().upper()
    if market not in MARKETS:
        raise ValueError(f"market must be one of {MARKETS}, got {market!r}")
    unknown = (None, None, None)
    if not isinstance(profile_location, str):
        profile_location = None
    if not isinstance(text, str):
        text = None

    if isinstance(platform, str) and platform.strip().casefold() == "tiktok":
        region = _alpha2(ext_region)
        if region:
            return region, 0.9, "ext_region"

    creator: set[str] = set()
    home = _country(home_market)
    if home:
        creator.add(home)
    if profile_location:
        found = _mentions(profile_location)
        if found is None:
            return unknown
        places, countries = found
        profile = places | countries
        if len(profile) > 1:
            return unknown
        creator |= profile
    if len(creator) > 1:
        return unknown
    if creator:
        return creator.pop(), 0.8, "home_market"

    if text:
        found = _mentions(text)
        if found is None:
            return unknown
        places, countries = found
        if len(places | countries) > 1:
            return unknown
        if places:
            return places.pop(), 0.7, "place_mention"
        if countries and countries != {market}:
            return unknown

    if _languages(language) & set(LANGUAGES[market]):
        return market, 0.3, "language"
    return unknown


def is_known(conf: float | None) -> bool:
    return conf is not None and conf >= KNOWN


def is_local(geo_market: str | None, conf: float | None, market: str) -> bool:
    return is_known(conf) and geo_market is not None and geo_market.upper() == str(market).strip().upper()
