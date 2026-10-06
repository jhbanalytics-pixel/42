"""GDELT seed generator, the collect side of BUILD.md task 2.5.

One query over the public GKG table gdelt-bq.gdeltv2.gkg_partitioned counts, per market, the people,
organisations, themes and places named in news about that market. A document belongs to ZA, NG or KE
(GDELT FIPS 10-4 codes SF, NI and KE) when that country is its dominant location, or when a local outlet
of the market ran it (document_markets, and MARKET_SQL for the same rule in BigQuery). Dominant means the
country holds more than DOMINANT_SHARE of the document's country-coded V1 Locations entries. V1 lists each
distinct location once and carries no character offsets, so the first entry listed is not reliably where
the story leads (the offsets live in V2Locations, which would add bytes); a strict majority picks at most
one country, so an India v South Africa match at a stadium in Kerala (three Indian entries, one South
African) is not ZA news, and a story naming two countries once each belongs to neither unless a local
outlet ran it. An outlet is local when SourceCommonName is, or ends in a dot and, one of OUTLETS: the
market's country domain (.za covers .co.za, .ng covers .com.ng, .ke covers .co.ke) or a named local
outlet on another domain. A local outlet's story counts for its market whatever it is about, since that
is news the market reads. The news day is the day before the run date, since the 02:00 SAST collect run
sees only a sliver of its own UTC day.

The baseline is four partitions, 7, 14, 21 and 28 days before the news day: a weekly sample of the
trailing 28 days, on the same weekday. A full 29-partition scan of the four entity columns would pass
the 5 GB cap every morning (the 1 Sep 2026 dry run read 37.3 GB for 14 GKG partitions with GCAM), so
the query reads 5 partitions and only the V1 columns Locations, Persons, Organizations and Themes, plus
SourceCommonName (a domain, a few MB a partition) for the outlet rule.

Score = (news day count + 1) / (baseline daily mean + 1). An entity needs MIN_COUNT documents on the
news day and a score of MIN_RATIO to rise. The top TOP_N per market go to seed_queue in lane expansion,
plus floor(15% of that) exploration picks drawn at random, repeatably per run date and market, from
below the cut. Each seed names the search/multi call the collect job runs (since the news day) and its
quote from the SocialCrawl client. Rows are appended by a MERGE on (seed_date, market, item_id) that
only inserts, so a rerun or an item another harvester seeded that day is left as it is.

Item ids come from core.detect.items (canonical_key, item_id), never built here. People, themes and
places are topics, organisations are brands. A theme counts only when it has the shape of a GKG theme
code (THEME_CODE: capital letters, digits and underscores, such as TAX_DISEASE_MALARIA, WB_678_..., ECON_,
UNGP_, EPU_, SOC_, ENV_ or a plain code like ELECTION); any other string has no label and never seeds.
Three theme families never seed: TAX_FNCACT (job titles
and groups of people, not things to search for), and TAX_ETHNICITY and TAX_RELIGION (POPIA special
categories), and so does any theme with the word RELIGION or a word starting ETHNIC anywhere in its
code. Rule 1 (RULES.md) is kept by blocked(), which the collect job's harvest tags and seed_queue
seeds also pass through: any theme code, name or place mixing scripts, or whose skeleton() carries one
of the RULE_ONE words as a whole word, or starts or ends with a cohort root (the fused forms GKG writes
as one token, AGINGPOPULATION and HIGHSCHOOL, and the cohort names social tags fuse into longer words),
is dropped before scoring, unless the token is exactly an ORDINARY word. The skeleton reads fullwidth,
mathematical, accented, small capital and Cyrillic or Greek lookalike letters as plain Latin ones, and
blocked() also reads digits for letters, stretched letters, and dotted or spaced words joined. The words
are stored with a "/" inside so they never appear whole in this file or its bytecode. GDELT is presence
only. A seed is a reason to look, never evidence, and nothing here writes posts or claims.

Every query is dry-run first; a read over MAX_BYTES, or a dry run that reports no size, is refused
before anything is billed, and each live query also carries maximum_bytes_billed = MAX_BYTES.

    py -3.13 -m core.collect.gdelt --plan --run-date 2026-09-29   the SQL and seed shape, no network
    py -3.13 -m core.collect.gdelt                                  dry run, read, then the seed_queue MERGE
"""

import argparse
import os
import random
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta

from google.cloud import bigquery

from core.collect.job import MULTI_PLATFORMS
from core.collect.socialcrawl_client import SAST, quote_for

PROJECT = "ogilvy-trends-v2"
LOCATION = "US"
SOURCE_TABLE = "gdelt-bq.gdeltv2.gkg_partitioned"
SEED_TABLE = "ogilvy-trends-v2.intelligence_42_core.seed_queue"
MARKETS = {"ZA": "SF", "NG": "NI", "KE": "KE"}
BASELINE_LAGS = (7, 14, 21, 28)
MIN_COUNT = 5
MIN_RATIO = 1.5
TOP_N = 20
EXPLORE_SHARE = 0.15
TTL_DAYS = 3
TEMPLATE = "search/multi"
MAX_BYTES = 5_000_000_000
ENTITY_KINDS = {"person": "topic", "organisation": "brand", "theme": "topic", "place": "topic"}
SEED_FIELDS = (("seed_date", "DATE"), ("market", "STRING"), ("item_id", "STRING"), ("query", "STRING"),
               ("kind", "STRING"), ("lane", "STRING"), ("priority", "FLOAT64"), ("template", "STRING"),
               ("ttl_days", "INT64"), ("credits_estimate", "FLOAT64"))

DOMINANT_SHARE = 0.5
# Per market, its country domain first, then local outlets on other domains; a host matches as itself or
# as a subdomain.
OUTLETS = {
    "ZA": ("za", "news24.com", "enca.com", "sabcnews.com", "thesouthafrican.com"),
    "NG": ("ng", "punchng.com", "vanguardngr.com", "premiumtimesng.com", "thisdaylive.com", "dailytrust.com",
           "channelstv.com", "saharareporters.com"),
    "KE": ("ke", "nation.africa", "citizen.digital", "businessdailyafrica.com"),
}

THEME_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
THEME_PREFIX = re.compile(r"^(TAX_[A-Z]+_|WB_\d+_|CRISISLEX_[A-Z0-9]+_|ECON_)")
DROPPED_THEMES = ("TAX_FNCACT", "TAX_ETHNICITY", "TAX_RELIGION")


def _unslash(words):
    return tuple("".join(w.split("/")) for w in words.split())


RULE_ONE = frozenset(_unslash("""
yo/uth yo/uths chi/ld chi/ldren chi/ldhood k/id k/ids te/en te/ens te/enage te/enager te/enagers
ado/lescent ado/lescence inf/ant inf/ants ba/by ba/bies el/der el/ders el/derly ag/eing ag/ing ag/ed a/ge
mil/lennial mil/lennials gen/eration gen/erations pen/sion pen/sions pen/sioner pen/sioners pen/sionary
stu/dent stu/dents pu/pil pu/pils sch/ool sch/ools mi/nor mi/nors juv/enile or/phan or/phans
se/nior se/niors gi/rl gi/rls bo/y bo/ys ge/nz ol/dage
yo/ung yo/unger new/born new/borns gen/erational tod/dler tod/dlers ret/iree ret/irees ret/irement
tw/een tw/eens ado/lescents yo/ungster yo/ungsters yo/ungin yo/ungins juv/eniles ret/irements chi/ldhoods
pen/sionaries a/ges ag/emate ag/emates zil/lennial zil/lennials
vi/jana ki/jana wa/toto mt/oto aba/ntwana int/sha umn/twana pi/kin kin/ders tie/ner tie/ners je/ug
ol/dpeople ol/derpeople ol/dfolks mid/dleaged un/derage un/deraged te/enybopper ol/derperson ol/derpersons
he/althyaging bo/rnfree bo/rnfrees
"""))
# Numeric and written cohort brackets, including audience ranges and decade forms.
YEAR, KID = r"(?:\d\d|\d{4}|y?2k|\d*0s)", r"(?:baby|babies|kids?)"
DASH = "[-\\u2010-\\u2015\\u2212\\u2e3a\\u2e3b\\ufe58]"
SP, GAP = r"[\s_]*", rf"[\s_]*(?:{DASH}[\s_]*)?"
WHO = (r"(?:people|women|men|ladies|guys|adults?|mums?|moms?|dads?|parents?|singles|professionals?|workers?"
       r"|consumers?|shoppers?|customers?)")
FOLLOWERS = r"(?:fans?|users?|viewers?|listeners?|readers?|buyers?|voters?)"
AUDIENCE = rf"(?:{WHO}|{FOLLOWERS}|audiences?|crowds?|events?|party|parties|only|dating)"
CASUALTY = (r"(?![\s_]+(?:(?:were|was|are|have|had|has|been|now|reportedly|feared|believed)[\s_]+)*"
            r"(?:killed|dead|died|injured|hurt|wounded|arrested|detained|missing|displaced|evacuated|rescued"
            r"|stranded|affected|drowned|shot|trapped|infected|hospitali[sz]ed))")
DECADE = r"\d0['\N{RIGHT SINGLE QUOTATION MARK}]?s"
TENS = r"(?:twent|thirt|fort|fift|sixt|sevent|eight|ninet)"
DECADE_WORD = rf"{TENS}ies"
WORD_NUMBERS = "|".join(_unslash("thir/t/e/e/n four/t/e/e/n fif/t/e/e/n six/t/e/e/n seven/t/e/e/n eigh/t/e/e/n"
                                  " nine/t/e/e/n"))
NUMBER_WORD = (rf"(?:{WORD_NUMBERS}|{TENS}"
               rf"(?:ies|y(?:{GAP}(?:one|two|three|four|five|six|seven|eight|nine))?))")
NUMBER = rf"(?:\d\d(?!\d)|{NUMBER_WORD}(?![^\W_]))"
JOINER = rf"(?:{SP}(?:to|through|thru|until|till|tot|hadi|zuwa|~|{DASH}){SP}|{DASH}{SP}to{SP}{DASH}{SP}|_+)"
MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|may|apr(?:il)?|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?"
         r"|nov(?:ember)?|dec(?:ember)?)")
BRACKETS = re.compile(
    rf"(?<![^\W_])(?:{YEAR}{KID}|ama{YEAR}(?![^\W_])"
    rf"|over{DASH}?{NUMBER}|over[\s_]+(?:\d\ds|{DECADE_WORD}(?![^\W_])"
    rf"|(?:\d\d|{NUMBER_WORD})[\s_]+{AUDIENCE}(?![^\W_]){CASUALTY})"
    rf"|(?:{WHO}|{FOLLOWERS}){SP}over{GAP}{NUMBER}|(?:under|above|below){GAP}{NUMBER}"
    rf"|u{DASH}?\d\ds(?![^\W_])|(?:[apmw]?\d\d|{NUMBER_WORD}){GAP}(?:\+|plus)"
    rf"|\d\d{SP}(?:(?:year|yr)s?{SP})?(?:and|or|&){SP}(?:over|up|under|above|below|older|younger)(?![^\W_])"
    rf"|{NUMBER}{GAP}(?:year|yr)s?{GAP}olds?|{NUMBER}{GAP}(?:yo|y/o|y\.o\.?)(?![^\W_])"
    rf"|(?:\d0|{TENS}y){GAP}somethings?"
    rf"|(?:early|mid|late){GAP}(?:{DECADE}|{DECADE_WORD})(?![^\W_])"
    rf"|(?:in{SP})?(?:his|her|their){SP}(?:{DECADE}|{DECADE_WORD})"
    rf"|in{SP}(?:my|your|our){SP}(?:{DECADE}|{DECADE_WORD})"
    rf"|{WHO}{SP}in{SP}(?:{DECADE}|{DECADE_WORD})"
    rf"|{DECADE}{SP}(?:{WHO}|audiences?|crowds?)(?![^\W_])"
    rf"|between{SP}\d\d{SP}(?:and|&){SP}\d\d(?!\d|{SP}{MONTH}(?![^\W_]))"
    rf"|(?<![.,:/])(?<!\d{DASH})(?:ag[e]s?{SP}|[apmw])?{NUMBER}{JOINER}{NUMBER}"
    rf"(?!\d|[.,:/]\d|{DASH}\d|{SP}{MONTH}(?![^\W_])))|{KID}{YEAR}(?![^\W_])")
# Numbers that read like a bracket and are not one, blanked before BRACKETS is read: a date range after a month
# (June 18-24, but not May 18-24s), a range or an under or over number before a unit (10-20 minutes, 20-30%,
# under 20 minutes), a number before under and a number (30 under 30), and a falling dash pair, which is a
# score (31-24); a cohort range rises (18-24). Nothing followed by a year, plus or an s is blanked.
UNIT = (r"(?:%|\N{DEGREE SIGN}|degrees?|percent|per[\s_]*cent|pc|seconds?|secs?|minutes?|mins?|hours?|hrs?|h|days?"
        r"|weeks?|wks?|months?|km|kms|kilometres?|kilometers?|metres?|meters?|m|cm|mm|kg|kgs|g|grams?|litres?"
        r"|liters?|l|ml|mph|kph|km/h|points?|pts|goals?|runs?|wickets?|overs|laps?)")
NOT_AGE_END = rf"(?!{GAP}(?:year|yr|yo|y/o|\+|plus|s(?![^\W_])))"
NOT_AGE = re.compile(
    rf"(?<![^\W_])(?:{MONTH}{SP}\d\d?(?:{JOINER}\d\d?(?!\d){NOT_AGE_END}|(?!\d|{JOINER}\d){NOT_AGE_END})"
    rf"|(?:\d\d{JOINER}\d\d|(?:under|over|above|below){GAP}(?:\d\d|{NUMBER_WORD})){SP}{UNIT}(?![^\W_])"
    rf"|\d\d{SP}under{GAP}\d\d(?!\d)"
    rf"|(?P<high>\d\d){DASH}(?P<low>\d\d)(?!\d){NOT_AGE_END})")


def _not_age(match):
    """A NOT_AGE span as spaces, except a rising dash pair, which is left for BRACKETS."""
    if match.group("high") and int(match.group("high")) < int(match.group("low")):
        return match.group(0)
    return " " * len(match.group(0))


# Cohort roots read at the start of a token and at its end (singular, or with one trailing s): every RULE_ONE
# word or joined phrase of four letters or more, and the cohort names below. The head only ones would take
# ordinary words at the end; so would the words built on the three letter one for years (pages, managing,
# damaged), which are read at the end only as the second half of two RULE_ONE words joined.
FUSED_ANY = _unslash("""
ge/nz ge/nalpha ge/nx mil/lennial mil/lenial boo/mer zoo/mer stu/dent te/en tw/een yo/uth k/id k/idz k/iddo
k/iddie tod/dler new/born se/nior el/derly ret/iree sch/ool tw/entysomething th/irtysomething ag/egap
vi/jana ki/jana wa/toto mt/oto aba/ntwana int/sha umn/twana pi/kin kin/ders tie/ner je/ug
""")
HEAD_ONLY = _unslash("a/ges ag/ed ag/ing ag/eing")
LONG_WORDS = tuple(sorted(w for w in RULE_ONE if len(w) >= 4))
FUSED_START = FUSED_ANY + _unslash("chi/ld ag/ing ag/eing ag/erelated inf/antile ba/by a/ge bo/y ol/der") + LONG_WORDS
FUSED_END = FUSED_ANY + tuple(w for w in LONG_WORDS if w not in HEAD_ONLY)
# Roots read anywhere inside a token (happykidsday, backtoschoolsale), once an ORDINARY word the token starts
# with is cut off (kidneystone is read as stone, kidneykids as kids).
ANYWHERE = _unslash("""
yo/uth yo/ung chi/ld sch/ool k/id te/en gi/rl bo/y ba/by ol/dage ag/eing bo/rnfree ge/nz mil/lennial
mil/lenial zil/lennial boo/mer zoo/mer tod/dler
new/born el/derly ret/iree stu/dent vi/jana ki/jana wa/toto mt/oto aba/ntwana int/sha umn/twana pi/kin kin/ders
tie/ner je/ug se/nior or/phan pu/pil juv/enile pen/sioner ado/lescent el/der ol/derperson
""")
# Ordinary words holding an ANYWHERE root that are cut out wherever they sit in a token, before any reading
# (worldkidneyday is read as world day, proteasfielder as proteas), longest first; the welder only at the
# start of a token, since new elders would read as one.
MASKS = re.compile("|".join(sorted(_unslash("""
k/idney fi/elder el/derflower el/derberry co/wgirl co/wboy to/mboy pl/ayboy cr/ybaby bo/ycott bo/yfriend bo/yle
bo/ysenberr bo/yega mb/oya ba/bylon yo/ungstown
"""), key=len, reverse=True)) + r"|(?<![^\W_])welder")
# Words that start, end or hold a cohort root and are not one: excused as the whole token (or the token less
# one trailing s), and as the start of a token for the roots inside them, so a cohort word fused after one
# stays blocked (kidneykids) and one only ends a token as a whole word (lagoskids is not excused as a skid).
# The number words 13 to 19, nouns and names, the Afrikaans for stone and against and their compounds,
# Swahili words in ki, and the words starting like the short year and boy roots.
ORDINARY = frozenset(_unslash("""
thirte/en fourte/en fifte/en sixte/en sevente/en eighte/en ninete/en cante/en velvete/en sate/en late/en umpte/en
mangoste/en poste/en karante/en springste/en malmste/en ste/en bakste/en hoekste/en grafste/en s/kid nons/kid
boo/merang zoo/merang ge/nzyme te/eny te/ensy betw/een inbetw/een
k/idney k/idnap k/idnapped k/idnapper k/idnapping k/idding k/idskin k/idzania k/idman k/idal k/idane
k/idogo k/idole k/idonge k/idonda se/niority ba/bylon ba/bylonian ba/bylonia
te/enoor te/enstander te/enwoordig te/enwoordigheid te/enspoed te/enaan te/enkanting te/engif
gi/rlfriend co/wboys we/lder sus/pension rege/neration dege/neration electricalge/neration mi/nority
mi/norities in/fantry in/fantino bra/inchild el/derflower el/derberry el/derberries yo/ungstown
bukidnon rothschild a/genda a/gency a/gencies a/gent a/gents a/gege bo/ycott bo/ycotted bo/yfriend bo/yle
bo/ysenberry bo/ysenberries bo/yega bo/yd
"""))
# Artists and a surname that hold a cohort word and are names, not a cohort. Each is excused only as the
# exact name in the skeleton, its words joined or split by any non letter, with nothing fused on either side,
# and the text on either side of it is read on its own (NAMES.split), so a RULE_ONE word next to one still blocks.
NAMES = re.compile(r"(?<![^\W_])(?:{})(?![^\W_])".format("|".join(
    r"[\W_]*".join(map(re.escape, name.replace("/", "").split())) for name in
    ("wizkidayo", "wizkid", "burna boy", "kiddominant", "k1 de ultimate", "baby face", "kidd", "kid x", "kidum", "young famous",
     "young jonn", "young stunna", "youngsta cpt", "youngsta", "young duu", "moonchild sanelly", "kid tini",
     "kidi music", "kidi", "childish gambino", "gen/erations the legacy", "gen/erations", "fireboy dml", "fireboy",
     "joeboy", "rudeboy", "boy spyce", "buruklyn boyz", "kidjo", "young john", "youngsfield", "baby city"))))
# Letters that look like Latin ones, read as the Latin letter: dotless and IPA letters, Latin small capitals,
# and Cyrillic and Greek lookalikes, all lower case since the skeleton casefolds first.
LOOKALIKES = str.maketrans({
    "\u0131": "i", "\u0237": "j", "\u0261": "g", "\u0251": "a", "\u0269": "i",
    "\u1d00": "a", "\u0299": "b", "\u1d04": "c", "\u1d05": "d", "\u1d07": "e", "\u0262": "g", "\u029c": "h",
    "\u026a": "i", "\u1d0a": "j", "\u1d0b": "k", "\u029f": "l", "\u1d0d": "m", "\u0274": "n", "\u1d0f": "o",
    "\u1d18": "p", "\u0280": "r", "\ua731": "s", "\u1d1b": "t", "\u1d1c": "u", "\u1d20": "v", "\u1d21": "w",
    "\u028f": "y", "\u1d22": "z",
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y", "\u0445": "x",
    "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u043a": "k", "\u043c": "m", "\u0442": "t", "\u043d": "h",
    "\u0432": "b", "\u0501": "d", "\u04af": "y", "\u04bb": "h", "\u04cf": "l",
    "\u03b1": "a", "\u03b2": "b", "\u03b5": "e", "\u03b7": "n", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v",
    "\u03bf": "o", "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03c7": "x",
})
# Digits and symbols written for letters, read as letters in one of the forms blocked() tries.
LEET = str.maketrans("013457@$", "oieastas")
# The first word of a letter's Unicode name is its script; these name the Japanese writing system's parts.
SCRIPT_GROUPS = {"CJK": "HAN", "HIRAGANA": "HAN", "KATAKANA": "HAN", "KATAKANA-HIRAGANA": "HAN", "IDEOGRAPHIC": "HAN"}
WORD_SPLIT = re.compile(r"[\W_]+")
LONG_RUN = re.compile(r"(.)\1{2,}")
ANY_RUN = re.compile(r"(.)\1+")

MERGE_SQL = f"""MERGE `{SEED_TABLE}` t
USING (SELECT * FROM UNNEST(@rows)) s
ON t.seed_date = s.seed_date AND t.market = s.market AND t.item_id = s.item_id
  AND t.seed_date = @seed_date
WHEN NOT MATCHED THEN
  INSERT (seed_date, market, item_id, query, kind, lane, priority, template, ttl_days, credits_estimate)
  VALUES (s.seed_date, s.market, s.item_id, s.query, s.kind, s.lane, s.priority, s.template, s.ttl_days,
          s.credits_estimate)
"""


def outlet_pattern(market):
    return r"(^|\.)(" + "|".join(re.escape(d) for d in OUTLETS[market]) + r")$"


# The GDELT codes of the markets a GKG row d belongs to, as one join in the FROM clause; the SQL form of
# document_markets. The outlet CASE and the dominant location CASE each name at most one market.
MARKET_SQL = """UNNEST(ARRAY(
  SELECT DISTINCT m
  FROM UNNEST([
    CASE {outlets} END,
    (SELECT CASE {dominant} END
     FROM UNNEST(SPLIT(d.Locations, ';')) loc
     WHERE SPLIT(loc, '#')[SAFE_OFFSET(2)] != '')
  ]) m
  WHERE m IN ({codes})
)) fips""".format(
    outlets="\n      ".join(f"WHEN REGEXP_CONTAINS(LOWER(TRIM(IFNULL(d.SourceCommonName, ''))), "
                            f"r'{outlet_pattern(market)}') THEN '{fips}'" for market, fips in MARKETS.items()),
    dominant="\n       ".join(f"WHEN COUNTIF(SPLIT(loc, '#')[SAFE_OFFSET(2)] = '{fips}') > {DOMINANT_SHARE} * COUNT(*) "
                             f"THEN '{fips}'" for fips in MARKETS.values()),
    codes=", ".join(f"'{fips}'" for fips in MARKETS.values()))


def document_markets(locations, source):
    """The markets a GKG document belongs to, from its V1 Locations and SourceCommonName, as MARKET_SQL
    reads them: its dominant country, and the market of its outlet."""
    host = (source or "").strip().lower()
    codes = [parts[2] for parts in (entry.split("#") for entry in (locations or "").split(";"))
             if len(parts) > 2 and parts[2]]
    found = set()
    for market, fips in MARKETS.items():
        if any(host == d or host.endswith("." + d) for d in OUTLETS[market]):
            found.add(market)
        if codes.count(fips) > DOMINANT_SHARE * len(codes):
            found.add(market)
    return found


class OverCap(Exception):
    """The dry run says the read would pass MAX_BYTES, or it could not say."""


def news_day(run_date):
    return run_date - timedelta(days=1)


def scan_days(run_date):
    day = news_day(run_date)
    return [day] + [day - timedelta(days=lag) for lag in BASELINE_LAGS]


def read_sql(run_date):
    days = ", ".join(f"DATE '{d.isoformat()}'" for d in scan_days(run_date))
    return f"""SELECT fips, e.entity_kind, e.entity,
  COUNTIF(d.is_today) AS today_count,
  COUNTIF(NOT d.is_today) AS baseline_count
FROM (
  SELECT _PARTITIONDATE = DATE '{news_day(run_date).isoformat()}' AS is_today,
    Locations, Persons, Organizations, Themes, SourceCommonName
  FROM `{SOURCE_TABLE}`
  WHERE _PARTITIONDATE IN ({days})
) d,
{MARKET_SQL},
UNNEST(ARRAY_CONCAT(
  ARRAY(SELECT AS STRUCT 'person' AS entity_kind, TRIM(x) AS entity FROM UNNEST(SPLIT(d.Persons, ';')) x),
  ARRAY(SELECT AS STRUCT 'organisation' AS entity_kind, TRIM(x) AS entity FROM UNNEST(SPLIT(d.Organizations, ';')) x),
  ARRAY(SELECT AS STRUCT 'theme' AS entity_kind, TRIM(x) AS entity FROM UNNEST(SPLIT(d.Themes, ';')) x),
  ARRAY(SELECT AS STRUCT 'place' AS entity_kind,
          TRIM(SPLIT(SPLIT(x, '#')[SAFE_OFFSET(1)], ',')[SAFE_OFFSET(0)]) AS entity
        FROM UNNEST(SPLIT(d.Locations, ';')) x
        WHERE SPLIT(x, '#')[SAFE_OFFSET(0)] IN ('4', '5') AND SPLIT(x, '#')[SAFE_OFFSET(2)] = fips)
)) e
WHERE e.entity != ''
GROUP BY fips, e.entity_kind, e.entity
HAVING today_count >= {MIN_COUNT}
"""


def _singular(word):
    return word[:-1] if word.endswith("s") else word


def _once(word):
    return ANY_RUN.sub(r"\1", word)


# The word lists as blocked() reads them: as written, for the skeleton with runs of three or more of a letter
# cut to two, and, for the skeleton with every run cut to one, only the words with no doubled letter (the
# rest would take ordinary words: a cohort root with its double cut is the number ten) and ORDINARY cut too.
def _alternation(words, head=""):
    return re.compile(head + "(?:" + "|".join(map(re.escape, sorted(set(words), key=lambda w: (-len(w), w)))) + ")")


def _level(keep, cut):
    """One reading of the word lists: whole words, start and end roots, ORDINARY, the ANYWHERE roots and the
    longest ORDINARY word a token starts with."""
    ordinary = frozenset(cut(w) for w in ORDINARY)
    return (frozenset(w for w in RULE_ONE if keep(w)), tuple(r for r in FUSED_START if keep(r)),
            tuple(r for r in FUSED_END if keep(r)), ordinary, _alternation(r for r in ANYWHERE if keep(r)),
            _alternation(ordinary, "^"))


AS_WRITTEN = _level(lambda w: True, lambda w: w)
RUNS_CUT = _level(lambda w: _once(w) == w, _once)
WHOLE_HEADS = tuple(RULE_ONE)  # a quick test before looking for two RULE_ONE words joined


def skeleton(text, gap=""):
    """text as the letters it shows, for matching only: NFKC normalised (fullwidth and mathematical letters
    become plain ones), casefolded, decomposed with its combining marks (accents, diaereses) dropped, its
    format characters (zero width joiner, soft hyphen) replaced by gap, and each LOOKALIKES letter read as
    the Latin one."""
    kept = []
    for ch in unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", str(text)).casefold()):
        kind = unicodedata.category(ch)
        if kind != "Mn":
            kept.append(gap if kind == "Cf" else ch)
    return "".join(kept).translate(LOOKALIKES)


def mixed_script(text):
    """True when the letters of text, NFKC normalised, come from more than one script (a Cyrillic letter
    standing in for a Latin one); Japanese kana and kanji count as one script."""
    names = {unicodedata.name(ch, "").split(" ")[0] for ch in unicodedata.normalize("NFKC", text) if ch.isalpha()}
    return len({SCRIPT_GROUPS.get(n, n) for n in names} - {"", "MODIFIER"}) > 1


def _pieces(tokens):
    """Each token, each two and three adjacent tokens joined, and each longer run of one letter tokens joined."""
    for i in range(len(tokens)):
        for j in range(i + 1, min(i + 3, len(tokens)) + 1):
            yield tokens[i:j]
    run = []
    for token in tokens + [""]:
        if len(token) == 1:
            run.append(token)
            continue
        if len(run) > 3:
            yield run
        run = []


def _hit(text, whole, start, end, ordinary, anywhere, head):
    """True when a piece of text is a whole word or two whole words joined, or starts or ends with a cohort
    root and is not an ordinary word, or a token holds an ANYWHERE root after the ordinary word it starts
    with (head) is cut off. A start root counts only past that ordinary word (kidneystone is open,
    a word listed in RULE_ONE that starts with one is not). A joined piece counts a root of four letters or
    more only where it crosses a join, since its first and last tokens are read on their own too (kidney
    failure is read as kidney)."""
    for piece in _pieces([t for t in WORD_SPLIT.split(text) if t]):
        word = "".join(piece)
        if word in whole:
            return True
        base = _singular(word)
        if word in ordinary or base in ordinary:
            continue
        if word.startswith(WHOLE_HEADS) and any(word[:i] in whole and word[i:] in whole
                                                for i in range(3, len(word) - 2)):
            return True
        if len(piece) == 1:
            cut = head.match(word)
            skip = cut.end() if cut else 0
            if anywhere.search(word, skip) or (word.startswith(start) and any(
                    len(r) > skip for r in start if word.startswith(r))) or word.startswith(start, skip):
                return True
        elif word.startswith(start) and any(3 < len(r) > len(piece[0]) for r in start if word.startswith(r)):
            return True
        joined = len(piece) > 1
        for w, last in ((base, _singular(piece[-1])), (word, piece[-1])):
            if w.endswith(end) and (not joined or any(3 < len(r) > len(last) for r in end if w.endswith(r))):
                return True
    return False


def blocked(text):
    """True when text mixes scripts, or its skeleton carries a RULE_ONE word, whole or fused onto another
    word at the start or end. The skeleton is read with format characters removed and as word breaks, with
    LEET digits and symbols as themselves and as letters, and with long runs of a letter cut to two and all
    runs cut to one (RUNS_CUT). Any other character that is not a letter or digit is a word break, and two
    or three adjacent words, or a run of single letters, are also read joined. A token starting with a
    BRACKETS cohort (a numeric or written bracket, NOT_AGE numbers read as blank) is blocked too, and a NAMES
    name is cut out first, with the text either side of it read on its own, and so is each MASKS word."""
    text = str(text)
    if mixed_script(text):
        return True
    forms = {part for gap in ("", " ") for part in NAMES.split(skeleton(text, gap))}
    for form in forms | {f.translate(LEET) for f in forms}:
        form = MASKS.sub(" ", form)
        two, one = LONG_RUN.sub(r"\1\1", form), _once(form)
        if BRACKETS.search(NOT_AGE.sub(_not_age, form)) or _hit(two, *AS_WRITTEN) or (one != two and _hit(one, *RUNS_CUT)):
            return True
    return False


def theme_label(code):
    """A GKG theme code as a search phrase, or None for the dropped families and anything not shaped like
    a code (THEME_CODE)."""
    code = (code or "").strip()
    words = code.split("_")
    if not THEME_CODE.fullmatch(code) or code.startswith(DROPPED_THEMES) or "RELIGION" in words or any(w.startswith("ETHNIC") for w in words):
        return None
    label = THEME_PREFIX.sub("", code).replace("_", " ").lower().strip()
    return label or None


def search_params(query, run_date):
    return {"query": query, "platforms": MULTI_PLATFORMS, "since": news_day(run_date).isoformat()}


def _item_functions():
    from core.detect import items
    return items


def candidates(rows, ids):
    """Per market, every item passing the count floor with its score, best first."""
    markets = {fips: market for market, fips in MARKETS.items()}
    found = defaultdict(lambda: {"today": 0, "baseline": 0})
    for r in rows:
        market = markets.get(r["fips"])
        kind = ENTITY_KINDS.get(r["entity_kind"])
        raw = theme_label(r["entity"]) if r["entity_kind"] == "theme" else r["entity"]
        if market is None or kind is None or raw is None or blocked(r["entity"]) or blocked(raw):
            continue
        try:
            key = ids.canonical_key(kind, raw)
        except ValueError:
            continue
        entry = found[(market, ids.item_id(kind, key))]
        entry.update(market=market, kind=kind, key=key)
        entry["today"] += r["today_count"]
        entry["baseline"] += r["baseline_count"]
    ranked = defaultdict(list)
    for (market, item), entry in found.items():
        if entry["today"] < MIN_COUNT:
            continue
        score = (entry["today"] + 1) / (entry["baseline"] / len(BASELINE_LAGS) + 1)
        ranked[market].append({**entry, "item_id": item, "score": score})
    for market in ranked:
        ranked[market].sort(key=lambda c: (-c["score"], -c["today"], c["kind"], c["key"]))
    return ranked


def _seed(c, lane, run_date):
    credits = quote_for(TEMPLATE, "GET", search_params(c["key"], run_date))
    return {"seed_date": run_date, "market": c["market"], "item_id": c["item_id"], "query": c["key"],
            "kind": c["kind"], "lane": lane, "priority": c["score"], "template": TEMPLATE,
            "ttl_days": TTL_DAYS, "credits_estimate": float(credits)}


def seeds(rows, run_date, ids=None):
    """seed_queue rows: the top TOP_N rising items per market, then exploration picks from below the cut."""
    ids = ids or _item_functions()
    out = []
    for market, ranked in candidates(rows, ids).items():
        top = [c for c in ranked if c["score"] >= MIN_RATIO][:TOP_N]
        chosen = {c["item_id"] for c in top}
        below = [c for c in ranked if c["item_id"] not in chosen]
        n = min(int(EXPLORE_SHARE * len(top)), len(below))
        rng = random.Random(f"gdelt|{run_date.isoformat()}|{market}")
        out += [_seed(c, "expansion", run_date) for c in top]
        out += [_seed(c, "exploration", run_date) for c in rng.sample(below, n)]
    return out


def rows_parameter(seed_rows):
    return bigquery.ArrayQueryParameter("rows", "STRUCT", [
        bigquery.StructQueryParameter(
            None, *(bigquery.ScalarQueryParameter(name, kind, s[name]) for name, kind in SEED_FIELDS))
        for s in seed_rows])


def _checked(client, sql, parameters=()):
    """Dry-run sql, refuse it over MAX_BYTES, then run it capped at MAX_BYTES."""
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=list(parameters)), location=LOCATION)
    size = dry.total_bytes_processed
    if size is None or size > MAX_BYTES:
        raise OverCap(f"dry run reports {size} bytes; the cap is {MAX_BYTES:,}")
    return client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=False, maximum_bytes_billed=MAX_BYTES, query_parameters=list(parameters)), location=LOCATION)


def run(client, run_date, ids=None):
    """Read GDELT, build the seeds and append them to seed_queue. Returns the rows written."""
    rows = list(_checked(client, read_sql(run_date)).result())
    seed_rows = seeds(rows, run_date, ids)
    if not seed_rows:
        return 0
    job = _checked(client, MERGE_SQL, [rows_parameter(seed_rows),
                                       bigquery.ScalarQueryParameter("seed_date", "DATE", run_date)])
    job.result()
    return job.num_dml_affected_rows


def print_plan(run_date):
    baseline = ", ".join(d.isoformat() for d in scan_days(run_date)[1:])
    credits = quote_for(TEMPLATE, "GET", search_params("x", run_date))
    explore = int(EXPLORE_SHARE * TOP_N)
    print(f"GDELT seeds for {run_date.isoformat()}: news day {news_day(run_date).isoformat()}, baseline {baseline}")
    print(read_sql(run_date))
    print(f"dry-run bytes: not estimated under --plan (no network); a live run dry-runs first and refuses above "
          f"{MAX_BYTES:,} bytes")
    print(f"seeds: up to {TOP_N} expansion and {explore} exploration per market ({', '.join(MARKETS)}) into "
          f"{SEED_TABLE}, template {TEMPLATE} at {credits} credits each, ttl {TTL_DAYS} days")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed rising GDELT entities per market into seed_queue.")
    parser.add_argument("--run-date", type=date.fromisoformat,
                        default=os.environ.get("RUN_DATE") or datetime.now(SAST).date().isoformat())
    parser.add_argument("--plan", action="store_true", help="print the SQL and seed shape; no network")
    args = parser.parse_args(argv)
    run_date = args.run_date
    if args.plan:
        print_plan(run_date)
        return 0
    try:
        written = run(bigquery.Client(project=PROJECT), run_date)
    except OverCap as error:
        print(f"refused: {error}")
        return 1
    print(f"seed_queue: {written} GDELT seeds written for {run_date.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
