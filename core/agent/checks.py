"""Deterministic claim checks for Ask (TRUST.md sections 2 and 3): K1, K2, K3, K5, K6, K8, K9 and K10 in code on every claim."""

from __future__ import annotations

import copy
import functools
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import prod
from urllib.parse import urlparse

from core.agent.answer import normalise
from core.agent.context import result_hash
from core.agent.forecast_promotion import promoted_forecasts, publishable_line
from core.agent.native_review import tone_cap_ids
from core.agent.tools.dates import SAST
from core.agent.tools.socialcrawl import ALLOWED_ROUTES, PLATFORM_NAMES
from core.agent.tools.sql_query import MAX_BYTES_BILLED, _dispatch_query

# K2 re-runs one answer's distinct queries at the same time, at most this many at once.
RERUN_WORKERS = 6
RULES = ("K1", "K2", "K3", "K5", "K6", "K8", "K9", "K10")
FORECAST_NO_LOG_REASON = "forecast has no matching current-run log; publication held pending persistence proof"
FORECAST_LOGGED_REASON = "forecast recorded in this run; publication held pending persistence proof"
FUTURE_ASSERTION = re.compile(
    r"\b(?:will|would|is likely to|are likely to|is expected to|are expected to|is set to|looks set to|is forecast to)\s+"
    r"(?:(?:keep|continue(?:\s+to)?)\s+)?(?:rise|rising|grow|growing|increase|increasing|climb|climbing|"
    r"spread|spreading|persist|persisting|remain|remains|stay|stays|hold|holding)\b", re.I)
CONDITIONAL_WATCH = re.compile(r"^\s*(?:watch|track|monitor|observe|check)\s+(?:whether|if)\b", re.I)
LABEL_RANK = {"inferred": 0, "single_source": 1, "observed": 2, "corroborated": 3}
TOLERANCE = 0.02  # ratios and other floats; counts are exact
IDENTITY_FIELDS = ("url", "handle", "platform", "posted_at", "market")
REQUIRED_FIELDS = ("platform", "handle", "url", "posted_at", "market", "text")
EVIDENCE_KEYS = ("id", "platform", "handle", "url", "posted_at", "market", "source_market", "text", "engagement",
                 "flags")
NOT_INDEPENDENT = {"paid", "brand", "brand_owned", "sponsored", "near_duplicate", "market_assumed"}
GENERATIVE_HOSTS = ("gemini.google.com", "bard.google.com", "aistudio.google.com", "notebooklm.google.com", "labs.google")
PLATFORM_LABELS = {"tiktok": "TikTok", "youtube": "YouTube", "x": "X", "twitter": "X"}

# Same patterns as the no_age_lens assert in core/eval/promptfooconfig.yaml.
AGE_PATTERNS = [re.compile(p, re.I) for p in (
    r"\bgen(?:eration)?[\s\u2010\u2011\u2013\u2014-]?(?:z|alpha|x|y)(?:ers?|'?s)?\b",
    r"\b(?:zoomers?|mill?enn?ials?|boomers?)\b",
    r"\bteen(?:s|age|aged|agers?)?\b",
    r"\bold[\s-]?(?:people|folks?|heads|timers?)\b",
    r"\bborn[\s-]frees?\b",
    r"\b(?:minors|retirees?)\b",
    r"\b(?:nineties|noughties|(?:19|20)?\d0s)[\s-]+(?:bab(?:y|ies)|kids?)\b",
    r"\bdigital[\s-]natives?\b",
    r"\ba\s+generation\s+(?:that|who)\b",
    r"\b(?:tweens|pre-?teens?)\b",
    r"\b(?:aged|ages)\s+(?:(?:between|from|of)\s+)?\d{1,2}\b",
    r"\b(?:under|over)[\s-]?\d{1,2}s\b",
    # under or over a number is an age only where the number ends the phrase or an age or audience word follows it,
    # so "over 20 plates", "under 5 million" and "over 10 years of Sundays" stay figures.
    r"\b(?:under|over)[\s-]?\d{1,2}\b(?![.,]?\d)(?=[^\S\n]*(?:$|\n|[.,;:!?)\]'\"]|\+)|"
    r"[\s-]*(?:year|yr)s?(?:[\s-]*olds?|\s+of\s+age)\b|"
    r"\s+(?:people|users|audiences?|crowds?|fans|viewers|listeners|consumers|voters)\b)",
    # an audience word before under or over a number ("users over 30 in Soweto"), unless a scale or plural follows.
    r"\b(?:people|users|audiences?|crowds?|fans|viewers|listeners|consumers|voters)\s+(?:under|over)[\s-]?\d{1,2}\b"
    r"(?![.,]?\d|\s*(?:%|(?:million|billion|thousand|mn|bn)\b|[a-z]+s\b))",
    r"\b\d{2}\+(?=[^\S\n]*(?:$|\n|[.,;:!?)\]'\"])|\s*(?:people|users|audiences?|crowds?|fans|viewers|listeners|"
    r"consumers|voters)\b)",
    r"\b\d{2}\s?(?:-|\u2013|to)\s?\d{2}[\s-]?(?:year|yr)s?[\s-]?olds?\b",
    r"\b\d{2}[\s-]?(?:year|yr)[\s-]?olds?\b",
    # an age bracket such as "the 25-34s", but not clip lengths in seconds ("10-15s clips").
    r"\b\d{2}\s?(?:-|\u2013|to)\s?\d{2}s\b(?!\s*(?:videos?|clips?|reels?|shorts?|ads?|spots?|edits?|hooks?|snippets?|"
    r"loops?)\b)",
    r"(?<!June\s)\b(?:13|16|18)\s?(?:-|\u2013|to)\s?(?:24|25|29|34|35)s?\b"
    r"(?!\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|%))",
    r"\b(?:twenty|thirty|forty|fifty|sixty|\d0)[\s-]?somethings?\b",
    r"\bin their ['\u2019]?(?:teens|twenties|thirties|forties|fifties|sixties|seventies|[2-7]0s)\b",
    r"\b(?:early|mid|late)[\s-]?['\u2019]?(?:teens|twenties|thirties|forties|fifties|sixties|seventies|[2-7]0s)\b",
    # an apostrophe decade before a person word is an age ("the '20s crowd").
    r"['\u2019][1-9]0s[\s-]+(?:crowds?|creators?|fans?|people|folks|professionals|users|audiences?"
    r"|consumers|viewers|listeners|voters|women|men|set)\b",
    r"\b(?:under|over)[\s-](?:twenties|thirties|forties)\b",
    r"\byouthful\b",
    # young and older are age terms unless the next word names a thing rather than people ("a young brand").
    r"\b(?:young(?:er|est)?|older)\b(?!\s+(?:brands?|labels?|compan(?:y|ies)|business(?:es)?|startups?|platforms?|"
    r"channels?|accounts?|apps?|products?|formats?|genres?|sounds?|songs?|tracks?|trends?|campaigns?|markets?|posts?|"
    r"videos?|clips?|content|tweets?|threads?|episodes?|data|records?|versions?|material|footage|than)\b)",
    r"\byouths?\b(?!\s+(?:day|month)\b)",
    r"\b(?:youngsters?|kids|children|adolescents?|elderly|seniors|pensioners?|(?:u|o|ko)?gogos?|grann(?:y|ies)|"
    r"grandmas?|grandmothers?|grandfathers?|grandparents?|mkhulus?|(?:u|o)?makhulus?|koko|bibi|babu|watoto|"
    r"abantwana)\b",
    # a birth cohort: born in or after a year or decade, a decade before born, generation or babies, and grew up
    # in a decade.
    r"\bborn\s+(?:in|after|before|since|around|between)\s+(?:the\s+)?(?:early|mid|late)?[\s-]*"
    r"['\u2019]?(?:(?:19|20)\d{2}|\d0s)",
    r"(?:\d0s|nineties|noughties)[\s-](?:born|generation|babies)\b",
    r"\bgrew\s+up\s+in\s+the\s+(?:early|mid|late)?[\s-]*"
    r"['\u2019]?(?:(?:19|20)\d0s|\d0s|nineties|noughties)",
    r"\bmiddle[\s-]?aged\b",
    # Gen Z or Gen Alpha run into a longer hashtag or word: at the start of a hashtag ("#genzrevolution"), and
    # elsewhere when a capital, a digit or an underscore follows ("GenZProtests", "genz254"). "Gen Za" stays clean.
    r"#gen(?:eration)?[_-]?(?:z|alpha)",
    r"\bgen(?:eration)?[_-]?(?:z|alpha)(?=(?-i:[A-Z])|[\d_])",
    r"\b(?:school[\s-]?(?:children|kids)|toddlers?|infants?|vijana|wazee|pikins?)\b",
    # babies, but not the named things "fur babies", "plant babies" and "sugar babies".
    r"(?<!\bfur\s)(?<!\bplant\s)(?<!\bsugar\s)\bbabies\b",
    # a generation as an audience, but not "a generation of content" or "a generation ago".
    r"\b(?:next|a|this|(?:the|a)\s+new)[\s-]+generation\b(?!\s+(?:of|ago)\b)",
    # a life stage as an audience, but not "matric results", "first-time buyers" or "school holidays".
    r"\b(?:matriculants?|matric[\s-]+(?:learners?|pupils?|students?)|first[\s-]?time[\s-]+voters?|"
    r"school[\s-]?leavers?)\b",
    # a birth-year cohort in isiZulu slang ("ama2000s", "ama-2000", "ama2k", "ama1990s").
    r"\bama[_-]?(?:(?:19|20)\d{2}'?s?|[12]ks?)\b",
    # the kid family as whole words, so "kidney" and "kidnap" stay clean; rule 1 wins over names such as Kid Cudi.
    r"\b(?:kid(?:s|z|dos?|dies?)?|zillenn?ials?|juveniles?|igen(?:eration)?s?|(?:ama|i)khehla)\b",
    # plain descriptors of a person's age (rule 1): "old man", "elders", "a little girl", "a schoolgirl", "grey-haired".
    # "elderberry" and "Old Mutual" stay clean because the pattern needs a person word or the whole word.
    r"\bold[\s-]+(?:man|men|woman|women|lad(?:y|ies)|guys?|couples?|persons?)\b",
    r"\belders?\b",
    r"\b(?:little|small)\s+(?:girls?|boys?)\b",
    r"\bschool[\s-]?(?:girls?|boys?)\b",
    r"\bgr[ae]y[\s-]?haired\b",
    # the singular child, with the Brief's exception for Child's Day, and the Brief's learner, school-going and
    # senior citizen terms (core/trust/claims.py _BREACH_TERMS), so the two lists agree on them.
    r"\bchild\b(?!'?s?\s+day\b)",
    r"\blearners?\b(?!'?s?\s+(?:licen[cs]es?|drivers?|permits?)\b)",
    r"\bschool[\s-]going\b",
    r"\bsenior\s+citizens?\b",
)]
DEMOGRAPHIC = re.compile(
    r"\b(?:demographics?|income brackets?|(?:middle|working|upper)[\s-]class|(?:low|high)[\s-]income|"
    r"gender split|life[\s-]stages?)\b", re.I)
# Same pattern as the citation_integrity assert.
SEARCH_VOLUME = re.compile(r"google[\W_]*trends?|trends\.google|search[\s-]*(?:volume|interest)", re.I)

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b")
# Ask's warehouse tools, which a gap may name the way it names a SocialCrawl route ("sql_query 7colours").
WAREHOUSE_TOOLS = ("sql_query", "search_posts", "rising_topics", "recall_findings")
# A route Ask may call, or a warehouse tool, as a whole token. None holds a digit, so a route can only hide a figure in
# a version segment after it (reddit/search/v2, one digit) or in a query word after it (see _blank_held). Any other
# word/word token is plain text, so views/2m, tiktok/s5000 and "Amapiano/Soweto 5000-streams" hold figures.
_ROUTE = (r"(?<![\w./-])(?:" + "|".join(map(re.escape, sorted({*ALLOWED_ROUTES, *WAREHOUSE_TOOLS}, key=len,
                                                                reverse=True))) + r")(?:/v[1-9])?")
_TOKEN_END = r"(?![\w-]|[./]\w)"
# An ISO date with an optional time and offset, so "2026-09-24 5000 posts" keeps its 5000. Only _iso_dates blanks one,
# and only once the whole match, time and offset too, parses and its year is in range.
ISO_DATE = re.compile(r"\b(?P<date>\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
                      r"(?![\w-])")
# Day-month dates (27 September, September 26th, 2026, September 2026), d/m/yyyy dates and clock times. Only
# _real_dates blanks one, and only when it is a real date with its year in range, or a real time.
DAY_MONTH = re.compile(r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>" + _MONTH + r")(?:,?\s+(?P<year>\d{4})\b)?")
MONTH_DAY = re.compile(r"\b(?P<month>" + _MONTH + r")\s+(?:(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{4})\b)?"
                       r"|(?P<alone>\d{4})\b)")
SLASH_DATE = re.compile(r"\b(?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{4}|\d{2})\b")
CLOCK = re.compile(r"\b(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?!\d)")
# A clock is a time only when its place says so. Strongly: before am, pm or SAST, before a time noun within three
# words ("the 21:00 peak", "21:00 is the peak hour", "the 18:00 news bulletin"), or straight after at, by, until,
# till, before or after. Weakly: after one of CLOCK_BEFORE's words with at most one hedge word between ("at around
# 21:00", "near 21:00") or straight after a weekday name ("Sunday 18:00", "on Sunday, 18:00"); after "and", "or",
# "to", a comma or a dash that follows an allowed clock ("between 19:00 and 21:00"); or at the start of a range whose
# partner is a real time ("20:00 to 22:00"). A clock with hour 0 or 1 needs a strong place, so "around 0:45" and
# "from 1:20 to 1:10" are figures, and no place counts when a ratio word is within three words ("a ratio of roughly
# 1:20", "a 3:10 split", "1:30 of watch time"). A 12-hour time ("6pm", "6 p.m.") and the ZA form 18h00 are always
# times when they are real.
_HEDGE = r"(?:around|about|roughly|approximately|nearly|almost|exactly|just|precisely)"
CLOCK_BEFORE = re.compile(r"(?:\b(?:at|from|until|till|by|before|between|after|around|since|about|roughly|"
                          r"approximately|past|on|near|towards|toward|during|again|starts|opens|resumes|"
                          r"go(?:es)?\s+live|comes)(?:\s+" + _HEDGE + r")?"
                          r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day,?)\s+$", re.I)
CLOCK_STRONG_BEFORE = re.compile(r"\b(?:at|by|until|till|before|after)\s+$", re.I)
# Nouns that name a time. After a clock within three words they place it strongly ("the 21:00 peak", "the 18:00
# kickoff"); in the six words before it they place it weakly ("the peak was 21:00", "the best time to post was
# 19:00"), but not the event nouns match, game, derby and fixture, which often introduce a score or a ratio ("after
# the derby, replies outnumbered likes 2:45").
_TIME_WORDS = (r"peaks?|slots?|spikes?|windows?|hours?|bulletins?|shows?|kick[\s-]?offs?|episodes?|broadcasts?|news|"
               r"streams?|sets?|premieres?|launch(?:es)?|releases?|deadlines?|times?|lunchtime|prime[\s-]?time|onwards?")
TIME_NOUN = re.compile(r"\b(?:" + _TIME_WORDS + r"|match(?:es)?|games?|derby|derbies|fixtures?)\b", re.I)
TIME_BEFORE = re.compile(r"\b(?:" + _TIME_WORDS + r"|peak(?:s|ed|ing)?|airs|aired)\b", re.I)
# A ratio word in the six words before a clock or the three after it vetoes every place, unless at, by or until straight before it names a
# 24-hour time (hour 13 or later), so "TikTok led the split at 21:00" is a time and "the odds sat at 2:15" a figure.
# A duration word within three words, or "long" straight after it ("a 2:30 long clip"), vetoes every place but at,
# by, until, between or from, or a weekday, straight before it, or am, pm or SAST after it, so "clips ran about 2:30
# each" and "the 2:30 mark of the clip" are figures and "posting ran from 22:00 to 00:30" a time.
RATIO_WORD = re.compile(r"\b(?:ratios?|split|odds)\b", re.I)
RATIO_BEATEN = re.compile(r"\b(?:at|by|until)\s+$", re.I)
DURATION_WORD = re.compile(r"\b(?:watch\s+time|average|lasted|length|runtime|duration|run|runs|ran|each|clips?|"
                           r"marks?)\b", re.I)
LONG_AFTER = re.compile(r"\s+long\b", re.I)
VETO_BEATEN = re.compile(r"(?:\b(?:at|by|until|between|from)|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day,?)\s+$", re.I)
# "the 19:30" at a quarter hour is a time.
THE_QUARTER_HOUR = re.compile(r"\bthe\s+$", re.I)
CLOCK_AFTER = re.compile(r"\s?(?:[ap]\.?m\b\.?|SAST\b)", re.I)
CLOCK_CHAIN = re.compile(r"\s*(?:,\s*(?:(?:and|or|to)\s+)?|(?:and|or|to|until|till)\s+|[-\u2013]\s*)", re.I)
CLOCK_RANGE = re.compile(r"\s*(?:to|until|till|and|-|\u2013)\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})(?!\d)", re.I)
CLOCK_12 = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s?(?:[ap]\.m\.|[ap]m\b)", re.I)
CLOCK_H = re.compile(r"\b(?P<hour>\d{1,2})h(?P<minute>\d{2})\b")
# A quarter, half or financial year with its year ("Q3 2026", "Q3 of 2026", "Q2, 2026", "H1 2026", "FY2026") and a
# decade ("the 1990s", "2000s", "'90s", "90s kwaito") are periods, not counts. A decade after "their", "his", "her",
# "our", "early", "mid" or "late", or before a person word, is an age ("in their '20s", "the '20s crowd"), so it stays
# for the age check and K2.
QUARTER = re.compile(r"\b(?:(?:Q[1-4]|H[12]),?\s+(?:of\s+)?|FY\s?)(?P<year>\d{4}|(?<=FY)\d{2}|(?<=FY )\d{2})\b")
_PERSON_WORDS = (r"crowds?|creators?|fans?|people|folks|professionals|users|audiences?|consumers|viewers|listeners|"
                 r"voters|women|men|set")
DECADE = re.compile(r"(?<!born in )(?<!born in the )(?<!grew up in )(?<!grew up in the )"
                    r"(?<!their )(?<!his )(?<!her )(?<!our )(?<!early )(?<!early-)(?<!mid )(?<!mid-)(?<!late )"
                    r"(?<!late-)(?:\b(?P<year>(?:19|20)\d0)s\b|'\d0s\b|\b[1-9]0s(?=\s+(?:kwaito|house|hip[\s-]?hop|"
                    r"r&b|rnb|pop|music|sound)\b))(?![\s-]+(?:" + _PERSON_WORDS + r"|generation|born|babies)\b)", re.I)
# A season ("2026/27", "2026-27") and a named half or quarter of a year ("the second quarter of 2026"), bounded by
# year like any date, and a bare quarter or half after "in" ("in Q4", "in H2").
SEASON = re.compile(r"\b(?P<year>(?:19|20)\d{2})[/-](?P<next>\d{2})\b")
NAMED_PERIOD = re.compile(r"\b(?:first|second|third|fourth)\s+(?:quarter|half)\s+of\s+(?P<year>\d{4})\b", re.I)
BARE_PERIOD = re.compile(r"(?<=\bin\s)(?:Q[1-4]|H[12])\b(?!\s*,?\s*(?:of\s+)?\d)")
# A d/m date with no year ("24/09"), allowed when it is a real date inside the window.
DAY_SLASH = re.compile(r"\b(?P<day>\d{1,2})/(?P<month>\d{1,2})\b(?!/\d)")
DOT_DATE = re.compile(r"\b(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\b")
# The ordinal day range "the 21st to the 27th", allowed when both days fall inside the window.
ORDINAL_RANGE = re.compile(r"\b(?:the\s+)?(?P<d1>\d{1,2})(?:st|nd|rd|th)\s+(?:to|and|-)\s+(?:the\s+)?"
                           r"(?P<d2>\d{1,2})(?:st|nd|rd|th)\b", re.I)
# Named things whose digits are part of the name: Covid-19, and the Sunday dish 7 colours.
NAMED = re.compile(r"\bcovid[\s-]?19\b|\b7\s+colou?rs\b", re.I)
# The years a date or an event year may name: from 1900, the first year _Y reads, to EVENT_YEARS_AHEAD past the
# as_of year. With no as_of at hand, to LAST_YEAR, the last year _Y reads.
FIRST_YEAR, LAST_YEAR, EVENT_YEARS_AHEAD = 1900, 2099, 2
# Every run of digits is a figure that needs a numbers entry, except inside route names such as reddit/search/v2 and
# the allowed forms. An id, handle, hashtag or link passes in any field only when this run holds it (_blank_held).
NOT_NUMERALS = re.compile(_ROUTE + _TOKEN_END)
# One figure: the letters touching a run of digits on either side, or a scale word after a space, and a percent sign.
# Only a scale word changes the value (900k and 900 thousand are 900000, 5 dozen is 60); any other letters (R450,
# USD5, 3rd, 2.8x, top10, Q3) leave it as written. A scale no entry is checked against (lakh, crore, grand, thou) or
# two scale words in a row (412 thousand million) makes the figure UNMATCHED.
SCALE = {"k": 3, "m": 6, "mn": 6, "million": 6, "b": 9, "bn": 9, "billion": 9, "hundred": 2, "thousand": 3}
ODD_SCALES = ("lakhs", "lakh", "crores", "crore", "grand", "thou")
# A spaced one-letter scale (k, m, b) must stand alone, so "5 k-pop" keeps its 5; any longer scale word may take a
# hyphen after it ("a 1.2 million-view clip", "20 thousand-plus", "5 mn-view").
_SCALE_WORD = (r"(?i:(?:" + "|".join(sorted([w for w in [*SCALE, "dozen", *ODD_SCALES] if len(w) > 1], key=len,
                                           reverse=True)) + r")(?!\w)|[kmb](?![\w-]))")
FIGURE = re.compile(r"(?:\u20a6|[^\W\d_]+)?(?P<num>\d{1,3}(?: \d{3})+(?![\d,.])|\d+(?:,\d{3})*(?:\.(?P<dec>\d+))?)"
                    r"(?:(?P<suffix>[^\W\d_]+)|\s+(?P<word>" + _SCALE_WORD + r"))?(?P<more>\s+" + _SCALE_WORD + r")?"
                    r"(?:(?P<pct>\s?%|[\s-]+(?i:percent|per\s+cent)\b)|(?P<range_pct>(?=\s?(?:-|\u2013|to)\s?\d+(?:\.\d+)?\s?%)))?")
# The value of a figure no numbers entry shows: an empty range.
UNMATCHED = (0, 0)
# Plural nouns that make "the 2000 posts" a count and not a year.
COUNT_NOUNS = (r"posts|videos|views|likes|comments|shares|creators|accounts|followers|streams|songs|tracks|plates|"
               r"reposts|tweets|mentions|hashtags|clips|people|users|fans|times|hours|minutes|days|weeks|months|"
               r"results|rows|votes|tickets|messages|replies|reactions|impressions|downloads|subscribers|members|"
               r"listeners|viewers|visits|clicks|searches|units|items|retweets|signatures|voices|million|billion|"
               r"thousand")
# After "the", a name or "by", a count qualifier and a lowercase plural make a year a count ("the 2027 unique
# voices"). Event plurals ("the 2027 new elections") and words ending in ss, us or is are not counts.
# A plural straight after the year counts only when it is one of COUNT_NOUNS, since a verb can stand there too ("the
# 2026 looks like"); after "in", "since" or "during" a qualifier is left alone ("in 2026 new creators joined").
_EVENT_PLURALS = (r"elections|polls|primaries|qualifiers|finals|playoffs|games|awards|olympics|championships|"
                  r"fixtures|matches|debates|protests|rallies|celebrations|festivities|hearings|talks|series")
_QUALIFIERS = (r"unique|new|total|more|other|individual|separate|distinct|different|extra|additional|original|active|"
               r"organic|verified|fresh|daily|weekly|monthly")
_PLURAL = r"(?![a-z]*(?:ss|us|is)\b)(?!(?:" + _EVENT_PLURALS + r")\b)[a-z]+s\b"
# A year phrase such as "in 2026" or "the 2026 Durban July". Every field may carry one whose year is no later than
# the as_of year; a later year, or a number before a count noun, is a figure.
_NOT_YEAR = r"(?![-/.,]?\d|\s*%|\s+(?:" + COUNT_NOUNS + r")\b)"
_COUNTED = r"(?!(?-i:\s+(?:" + _QUALIFIERS + r")\s+" + _PLURAL + r"))"
_Y = r"(?:19|20)\d{2}"
YEAR = re.compile(r"\b(?:in|since|during|the(?=\s+" + _Y + r"\b" + _COUNTED + r"))\s+(?P<year>" + _Y + r")\b"
                  + _NOT_YEAR, re.I)
# A year that names an event may run up to two years past the as_of year: "the 2027 election" (the year, then a word
# that is not a count noun or a joining word), "the 2027 and 2028 elections", "AFCON 2027" or "Kenya's 2027 election"
# (a capitalised name, possessive or not, that is not a preposition or a quantity word), and "ahead of 2027", "ahead
# to 2027", "look to 2027", "by 2027" and "the run-up to 2027". "in 2027", "2027 posts" and "Over 2027 posts" stay figures.
_JOINING = r"(?:and|or|to|of|in|on|at|for|with|by|from|as|is|are|was|were|will|would|could|should|may|might|has|have|had)"
_NOT_A_NAME = (r"(?:In|The|Since|During|By|From|Until|Till|Before|After|Of|On|At|To|For|And|Or|But|As|Than|Over|"
               r"Under|About|Around|Nearly|Almost|Roughly|Some|Only|Just|Exactly|Approximately|Up|Top|Past|Beyond|"
               r"Early|Mid|Late|Twice|Double|Triple|Half|No|Number|Rank|Page|R|USD|ZAR|KES|NGN|EUR|GBP)")
EVENT_YEAR = re.compile(r"(?:\b(?i:the)\s+(?P<first>" + _Y + r")\s+(?:and|or|to)\s+(?P<second>" + _Y + r")\b"
                        r"(?=\s+[^\W\d_])"
                        r"|\b(?i:the)\s+(?P<the>" + _Y + r")\b(?=\s+(?!(?i:" + COUNT_NOUNS + r"|" + _JOINING +
                        r")\b)[^\W\d_])"
                        r"|\b(?!" + _NOT_A_NAME + r"\b)[A-Z][A-Za-z]*(?:'s)?\s+(?P<name>" + _Y + r")\b"
                        r"|\b(?i:ahead\s+(?:of|to)|look\s+to|by|run-up\s+to)\s+(?P<lead>" + _Y + r")\b)" + _NOT_YEAR + _COUNTED)
# A day range in window_text's format, with or without a year: "21 to 27 September", "26 and 27 September",
# "26-27 September 2026". Every field may carry one whose days both fall inside the window.
DAY_RANGE = re.compile(r"\b(?P<d1>\d{1,2})(?:st|nd|rd|th)?(?:\s*(?:-|\u2013)\s*|\s+(?:to|and)\s+)"
                       r"(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>" + _MONTH + r")(?:,?\s+(?P<year>\d{4})\b)?",
                       re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# Spelled multipliers and counts are figures too: twice, double or triple the, half the, three times, tenfold,
# a dozen, dozens, thousands, a few or several thousand, a cardinal with a scale word (a million, three hundred
# thousand, a thousand thanks), a spelled percent (fifty percent), a share (one in five, nine out of ten, a quarter
# of) and a tens word before a plural (forty creators). A number word from one to twenty alone ("one creator said",
# "two platforms", "one of the") is not.
_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
          "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty")
_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
SCALE_WORDS = {"hundred": 100, "thousand": 1000, "million": 1000000, "billion": 1000000000}
WORD_VALUES = ({w: i for i, w in enumerate(_WORDS, 1)} | {w: 20 + 10 * i for i, w in enumerate(_TENS)}
               | {"a": 1, "an": 1, "half a": 0.5} | SCALE_WORDS)
# A spelled cardinal keeps the rounding of its last scale word, as 12k and 1m do, but never coarser than its own
# trailing zeros: half a million rounds to hundred thousands, a quarter of a million to ten thousands. "three hundred"
# is exact.
CARDINAL_DECIMALS = {"thousand": -3, "million": -6, "billion": -9}
# "a quarter of", "two thirds of", "three-quarters of"; half is the half group below.
FRACTIONS = {"quarter": 4, "third": 3, "fifth": 5}
# A number word: a tens word with or without its unit (forty, twenty-five, twenty five), or one to twenty.
_TENS_WORD = r"(?:" + "|".join(_TENS) + r")\b(?:[\s-](?:" + "|".join(_WORDS[:9]) + r")\b)?"
_NUMBER = r"(?:" + _TENS_WORD + r"|(?:" + "|".join(_WORDS) + r")\b)"
_SCALES = r"(?:" + "|".join(SCALE_WORDS) + r")"
# A fraction of a scale, with or without "of a" ("a quarter of a million", "a quarter million"), half with or without
# "a" ("half a million", "half-million"), and a tail after "and" ("a hundred and fifty").
_FRACTION_OF = r"(?:a|one|two|three|four)[\s-]+(?:" + "|".join(FRACTIONS) + r")s?(?:\s+of\s+an?)?"
_CARDINAL = (r"(?:half(?:\s+a)?|" + _FRACTION_OF + r"|an?|" + _NUMBER + r")(?:[\s-]+" + _SCALES + r")+"
             r"(?:\s+and\s+" + _NUMBER + r")?\b")
# "one half of the duo" names a person, not a share.
_NOT_A_SHARE = r"(?!\s+of\s+(?:the\s+)?(?:duo|pair|couple|partnership|team|act|band)\b)"
# "every other post", "every second post", "every tenth post": the share 1/n.
EVERY = {"other": 2, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "tenth": 10}
_QUANTITY = r"(?=\s+(?:the|that|what|this|its|their|his|her|our|your|those|these|all|a|an|of|as)\b)"
# Plural counts read as a range, as dozens does: an entry from twice the unit up to the next scale shows it.
MANY = {"dozens": (24, 144), "hundreds": (200, 1000), "thousands": (2000, 10 ** 6), "millions": (2 * 10 ** 6, 10 ** 9),
        "billions": (2 * 10 ** 9, 10 ** 12)}
# "tens of thousands" is 10000 to 99999 and "hundreds of thousands" 200000 to 999999: one figure, not two.
MANY_OF = {"tens": (10, 100), "hundreds": (200, 1000)}
# A vague head before one or more scale words: a few, few or several is two to ten of them, a couple two to under
# three, and some about one ("some thousand" is 1000 to 1999). "several hundred thousand" is 200000 to 999999.
VAGUE = {"few": (2, 10), "several": (2, 10), "couple": (2, 3), "some": (1, 2)}
SPELLED = re.compile(
    r"\b(?:(?P<dozen>a|an|half\s+a|" + "|".join(_WORDS) + r")\s+dozen\b"
    r"|(?P<head>a\s+few|few|several|a\s+couple(?:\s+of)?|some)\s+(?P<vague>" + _SCALES + r"s?(?:\s+" + _SCALES
    + r"s?)*)\b"
    r"|(?P<many_of>tens|hundreds)\s+of\s+(?P<of_scale>thousands|millions|billions)\b"
    r"|(?P<many>" + "|".join(MANY) + r")\b"
    r"|(?P<and_half>" + _NUMBER + r")\s+and\s+a\s+half\s+times\b"
    r"|(?:nearly|almost|about|roughly|over|under|by|in)\s+(?P<hedged_half>half)\b"
    r"(?!\s+an?\s+(?:hundred|thousand|million|billion)\b)"
    r"|(?:open|opens|opening|trading|hours)\s+(?P<hours>" + _NUMBER + r"\s+to\s+" + _NUMBER + r")"
    r"|every\s+(?P<every>" + "|".join(EVERY) + r")\b"
    r"|the\s+(?P<mark_half>half[\s-]+)?(?P<mark>" + _SCALES + r")(?:[\s-]+[a-z]+)?\s+mark\b"
    r"|a\s+factor\s+of\s+(?P<factor>" + _NUMBER + r")"
    r"|(?P<pp>" + _CARDINAL + r"|" + _NUMBER + r")\s+percentage\s+points?\b"
    r"|(?P<n>" + _NUMBER + r")\s+(?:in|out\s+of)(?:\s+every)?\s+(?P<m>" + _CARDINAL + r"|" + _NUMBER + r")"
    r"|(?!nine\s+to\s+five\b)(?P<to_a>" + _NUMBER + r")\s+to\s+(?P<to_b>" + _NUMBER + r")"
    r"|(?P<spelled_pct>" + _CARDINAL + r"|" + _NUMBER + r")\s*(?:percent|per\s+cent)\b"
    r"|(?P<cardinal>" + _CARDINAL + r")(?P<multiplier>\s+times\b|[\s-]?fold\b)?"
    r"|(?P<numer>a|one|two|three|four)[\s-]+(?P<frac>" + "|".join(FRACTIONS) + r")s?\s+of\b"
    r"|(?P<bare>a|one|two|three|four)[\s-]+(?P<bare_frac>half|(?:" + "|".join(FRACTIONS) + r")s?)\b"
    r"(?![\s-]+(?:party|parties|time|times|final|finals|place|round|term|wave|world|way|hour|day|dozen|wheel)\b)"
    # "a third", "a quarter" or "a fifth" as an order word before a singular noun, one listed adjective allowed
    # between: "a third prominent thread" is no share, "a third started threads" and "two thirds shared stories" are.
    r"(?!(?:(?<=\ba\sthird)|(?<=\ba-third)|(?<=\ba\sfifth)|(?<=\ba-fifth)|(?<=\ba\squarter)|(?<=\ba-quarter))"
    r"\s+(?:(?:prominent|big|major|main|new|separate|distinct|clear|recurring|smaller|larger)\s+)?"
    r"(?:thread|topic|theme|story|conversation)\b)"
    + _NOT_A_SHARE +
    r"|(?P<tens>" + _TENS_WORD + r")(?=\s+" + _PLURAL + r")"
    r"|(?P<times>" + "|".join(_WORDS) + r"|hundred|thousand|million)(?:\s+times|[\s-]?fold)\b"
    r"|(?P<two>twice|doubled|double" + _QUANTITY + r")\b"
    r"|(?P<three>tripled|triple" + _QUANTITY + r")\b"
    r"|(?P<half>halved|(?<!first )(?<!second )(?<!other )(?<!latter )(?<!last )half" + _QUANTITY + _NOT_A_SHARE
    + r")\b)", re.I)
# Allowed in any gap text on top of NOT_NUMERALS and the allowances of every field: the tier names T0 to T3. With the
# route query word _blank_held allows, this is all a writer or source gap may carry.
WRITER_GAP_ALLOWED = re.compile(r"\bT[0-3]\b", re.I)
# The claim ids, item numbers, rule names and id lists the code's own gaps write ("Claim c3 cut", "(K2)", "stored
# evidence p1, p2", "and 2 ids that do not resolve"). Only a gap the code wrote may carry them, so a writer gap never
# hides a figure behind this wording. A claim id holds a letter, so "Claim 5000 cut" is no code gap. write_answer
# renames every claim id to c1 to c999, so a digit-only id only reaches this when check_answer is called directly.
_CLAIM_ID = r"(?=[\w.-]*[^\W\d_])[\w.-]+"
CODE_GAP_ALLOWED = re.compile("|".join((
    r"\b(?:so_what|watch_next) item \d+ removed\b",
    r"\bclaim " + _CLAIM_ID + r" (?:cut|removed)\b",
    r"\(K(?:10|[1-9])\)",
    r"\b(?:stored evidence|cited posts|recorded queries) [a-z][\w.-]*(?:, [a-z][\w.-]*)*",
    r"\b\d+ ids? that do(?:es)? not resolve\b",
)), re.I)
# What a gap the code wrote may carry.
GAP_ALLOWED = re.compile(WRITER_GAP_ALLOWED.pattern + "|" + CODE_GAP_ALLOWED.pattern, re.I)
# Gaps the checks write for their own cuts name the rule and the field, never the numeral, span or term they cut,
# so they pass the gap checks like any other gap. The verdict rows keep the detail.
RULE_GAPS = {
    "K1": ("a citation that does not match its stored post",
           "every evidence id must resolve to a stored post, and every quote must be verbatim in it"),
    "K2": ("a number with no query behind it",
           "every number must come from a query recorded in this run and come back the same on re-run"),
    "K3": ("a post outside the window or the market, or a place no cited post is located in",
           "every cited post must fall inside the question's window and market, and text that names a place must "
           "cite a post located there"),
    "K6": ("a term or source the trust rules do not allow",
           "the text used a term or cited a source that the trust rules do not allow"),
    "K8": ("a quote not found in its posts",
           "every quoted span must be verbatim in a cited post"),
    "K9": ("a forecast before it has beaten persistence",
           "forecasts remain held until they beat the persistence baseline"),
}
WORDS = re.compile(r"[\w']+")

# K3 places (Albert, 29 September 2026): "a post with no location may support a claim that names no place; only posts
# confirmed from another market are cut, and a place claim still needs a post located there." A place in an asked
# market needs a cited post located in that market. A place outside the asked markets (NG or KE in a ZA ask, or any
# other country, "") is what the claim is about, not where it comes from ("Bafana beat Nigeria", "South Africans
# rallied for Palestine"), so a cited post located in an asked market backs it; with none, it is cut. A claim that
# names exactly one of the asked markets must also cite only that market. People named by where they are from, a
# plural demonym ("Kenyans") or a demonym and a people noun ("Nigerian fans", "Kenyan TikTok creators"), are a claim
# about people there, not a place the claim is about, so they need a cited post located in their own market with no
# fallback, and people from outside ZA, NG and KE are always cut (review round 3). SA, ZA, RSA, KZN, JHB, CPT, US, UK,
# Free State and North West match case-sensitively, so "us" and "a free state" are no place, and so do Turkey, China,
# Chile, Guinea, Mali, Malta and Panama, which are a food, plates, a pepper, a pig, money, a drink and a hat in lower
# case; the other countries match in any case. Bare "Somali" is a language and no place; "Somalis" are people. A
# place word inside a name is no place: Durban Poison (a strain), the Durban July (a race), the Soweto derby (a
# fixture). Chad, Jordan and Georgia are left off: in social posts they are far more often a name, a trainer or a US
# state than a country. So are Basotho, Batswana and Swazi, which name communities inside South Africa too.
_CASED = {"US", "UK", "Turkey", "China", "Chile", "Guinea", "Mali", "Malta", "Panama", "Togo", "Brazil", "Fiji",
          "Iceland", "Japan", "Zim"}
_COUNTRIES = (
    "Afghanistan Albania Algeria Andorra Angola Argentina Armenia Australia Austria Azerbaijan Bahamas Bahrain "
    "Bangladesh Barbados Belarus Belgium Belize Benin(?!\\s+City) Bhutan Bolivia Bosnia Botswana Brazil Brunei "
    "Bulgaria Burkina\\s+Faso Burundi Cambodia Cameroon Canada Cabo\\s+Verde Cape\\s+Verde Chile China Colombia Comoros Congo "
    "Costa\\s+Rica Croatia Cuba Cyprus Czechia Denmark Djibouti Dominica Ecuador Egypt El\\s+Salvador Eritrea Estonia "
    "Eswatini Swaziland Ethiopia Fiji Finland France Gabon Gambia Germany Ghana Greece Grenada Guatemala Guinea Guyana "
    "Haiti Honduras Hungary Iceland India Indonesia Iran Iraq Ireland Israel Italy Ivory\\s+Coast Jamaica Japan "
    "Kazakhstan Kiribati Kosovo Kuwait Kyrgyzstan Laos Latvia Lebanon Lesotho Liberia Libya Liechtenstein Lithuania "
    "Luxembourg Madagascar Malawi Malaysia Maldives Mali Malta Mauritania Mauritius Mexico Moldova Monaco Mongolia "
    "Montenegro Morocco Mozambique Myanmar Namibia Nauru Nepal Netherlands New\\s+Zealand Nicaragua Niger(?!\\s+Delta) "
    "Korea Dominican\\s+Republic Central\\s+African\\s+Republic Czech\\s+Republic Côte\\s+d['’]Ivoire "
    "Macedonia Norway Oman Pakistan Palau Palestine Panama Paraguay Peru Philippines Poland Portugal Qatar Romania "
    "Russia Rwanda Samoa San\\s+Marino Saudi\\s+Arabia Senegal Serbia Seychelles Sierra\\s+Leone Singapore Slovakia "
    "Slovenia Somalia Sudan Spain Sri\\s+Lanka Suriname Sweden Switzerland Syria Taiwan Tajikistan Tanzania Thailand "
    "Togo Tonga Trinidad Tunisia Turkey Türkiye Turkmenistan Tuvalu Uganda Ukraine United\\s+Arab\\s+Emirates UAE "
    "United\\s+Kingdom UK Britain England Scotland Wales United\\s+States USA US America Uruguay Uzbekistan Vanuatu "
    "Vatican Venezuela Vietnam Yemen Zambia Zimbabwe Zim "
    # demonyms of the neighbours and the places posts name most, regions, and foreign cities
    "Ghanaians? Zimbabweans? British Americans? Malawians? Mozambicans? Namibians? Ugandans? Tanzanians? Ethiopians? "
    "Somalians? Somalis Cameroonians? Congolese Zambians? Angolans? Rwandans? West\\s+Africa(?:ns?)? East\\s+Africa(?:ns?)? "
    "Accra Kumasi London Harare Bulawayo Kampala Kinshasa Maputo Gaborone Windhoek Maseru Mbabane Lusaka Lilongwe "
    "Addis\\s+Ababa Dar\\s+es\\s+Salaam Dubai Paris New\\s+York"
).split()
PLACES = {
    "ZA": re.compile(
        r"\b(?:SA|ZA|RSA|KZN|JHB|CPT|Free\s+State|North\s+West|(?i:South\s+Africa(?:ns?)?|Mzansi|Gauteng"
        r"|Kwa[\s-]?Zulu[\s-]+Natal|(?:Western|Eastern|Northern)\s+Cape|Limpopo|Mpumalanga|Soweto(?!\s+derby\b)"
        r"|Sowetans?|Johannesburg|Jo['’]?burg(?:ers?)?|Jozi|Pretoria|Pretorians?|Tshwane|Cape\s+Town|Capetonians?"
        r"|Durban(?!\s+(?:poison|july)\b)|Durbanites?|Gqeberha|Port\s+Elizabeth|Bloemfontein|Polokwane|Khayelitsha"
        r"|Tembisa|Umlazi|Mamelodi|Soshanguve|Sandton))\b"),
    "NG": re.compile(r"\b(?:Nigeria(?:ns?)?|Naija|Lagos|Lagosians?|Abuja|Kano|Ibadan|Port\s+Harcourt|Enugu|Kaduna"
                     r"|Benin\s+City|Niger\s+Delta|Lekki|Ikeja|Surulere|Owerri|Calabar)\b", re.I),
    "KE": re.compile(r"\b(?:Kenya(?:ns?)?|Nairobi|Nairobians?|Mombasa|Kisumu|Nakuru|Eldoret|Kibera|Thika|Malindi"
                     r"|Kilifi)\b", re.I),
    "": re.compile(r"\b(?:" + "|".join(c for c in _COUNTRIES if c in _CASED)
                   + r"|(?i:" + "|".join(c for c in _COUNTRIES if c not in _CASED) + r"))\b"),
}
# People located somewhere (review rounds 3 to 5). Text names people there, not a place it is about, when a people
# noun sits right against the place or demonym: a plural demonym ("Kenyans"); a demonym, further places joined by
# and, or, & or a comma, and an optional word before a people noun ("Nigerian audiences", "Kenyan food creators");
# a place and a people noun, directly or with a scene word between ("Lagos creators", "Lagos food creators"), or
# after 's or -based with any word between ("Nigeria's music lovers", "Lagos-based creators"); a people noun, a
# preposition of place and the place ("fans across Nigeria", "the people of Kenya"); a fronted place ("In Lagos,
# creators"); or a place and where ("Nigeria and Kenya, where fans"). A bare place and a people noun with a verb
# between is the place as topic, so "Bafana beat Nigeria as fans cheered" and "Palestine got users talking" name no
# people there. _NOT_PLACES are names, not places.
_NOT_PLACES = re.compile(r"\b(?:British\s+Airways|American\s+Idol|African[\s-]+Americans?|Kenya\s+Airways"
                         r"|Kenya\s+Power|Nigerian\s+Breweries)\b", re.I)
_DEMONYMS = {
    "ZA": r"South\s+African|Sowetan|Capetonian|Durbanite|Jo['’]?burger|Pretorian",
    "NG": r"Nigerian|Lagosian",
    "KE": r"Kenyan|Nairobian",
    "": r"Ghanaian|Zimbabwean|British|American|Malawian|Mozambican|Namibian|Ugandan|Tanzanian|Ethiopian|Somalian"
        r"|Somali|Cameroonian|Congolese|Zambian|Angolan|Rwandan|West\s+African|East\s+African",
}
# No age word is ever a people noun here (RULES.md rule 1).
_PEOPLE_NOUNS = (r"(?:creator|fan|user|supporter|voter|influencer|artist|comedian|musician|resident|worker|trader"
                 r"|driver|commuter|follower|audience|listener|viewer|tiktoker|tweep|stan|customer|shopper|consumer"
                 r"|netizen|streamer|partygoer|crowd|lover)s?|people|folks?|wom[ae]n|m[ae]n|twitter|tiktok")
_SCENES = (r"food|music|fashion|beauty|tech|comedy|football|soccer|gaming|church|club|nightlife|party|amapiano"
           r"|afrobeats|dance|film|fitness|lifestyle|travel|crypto|business")
_JOINING = r"(?:as|and|or|but|while|when|then|so|after|before|because|if|to|the|a|an|for|of|in|on|at|by|with|is|was)"
_OPTIONAL = rf"(?:\s+(?!{_JOINING}\b)[\w'’-]+)?"
_PLACE_SOURCES = {m: p.pattern if m in ("ZA", "") else f"(?i:{p.pattern})" for m, p in PLACES.items()}
_ANY_PLACE = "|".join(_PLACE_SOURCES.values())
_JOINED = rf"(?:(?i:\s*,\s*|\s+(?:and|or|&)\s+)(?:{_ANY_PLACE}))*"


def _people(demonyms, place):
    """The people pattern of one market, from its demonyms and its place pattern (see the comment above)."""
    nouns = rf"(?i:(?:{_PEOPLE_NOUNS})\b)"
    return re.compile(
        rf"(?i:\b(?:{demonyms})s\b)"
        rf"|(?i:\b(?:{demonyms})){_JOINED}(?i:{_OPTIONAL})\s+{nouns}"
        rf"|(?:{place})(?i:\s+(?:(?:{_SCENES})\s+)?|(?:['’]s|-based){_OPTIONAL}\s+){nouns}"
        rf"|(?i:\b(?:{_PEOPLE_NOUNS})\s+(?:in|from|based\s+in|out\s+of|across|throughout|around|all\s+over|of|among)"
        rf"\s+(?:the\s+)?)(?:{place})"
        rf"|(?i:\b(?:in|across|throughout|around|all\s+over)\s+(?:the\s+)?)(?:{place}){_JOINED}"
        rf"(?i:\s*,\s*(?:[\w'’-]+\s+){{0,3}}?){nouns}"
        rf"|(?:{place}){_JOINED}(?i:,?\s+where\s+(?:[\w'’-]+\s+){{0,2}}?){nouns}")


PEOPLE = {mkt: _people(d, _PLACE_SOURCES[mkt]) for mkt, d in _DEMONYMS.items()}

# Nationality of a named person (DEMO-03, progress/L3.md). A demonym, a birthplace or a country's possessive attached
# to a named creator or person ("Nigerian creator @x", "Kenyan rapper Khaligraph Jones", "@x, a Kenyan rapper", "@x
# (Nigerian)", "Burna Boy is Nigerian", "@x, who is Nigerian", "The Nigerian Burna Boy", "Lagos-born @x", "Nigeria's
# @x", "Kenya's own Sauti Sol") is a claim about that person. A post located in a market, or seen in its feeds, says
# where a post came from, never what its author is, so the claim needs a cited post whose own text states it: a post
# that itself says it of the person in one of these forms, or the person's own post saying it affirmatively ("I'm
# Nigerian", "proud Nigerian", "as a Nigerian", "born in Lagos"), so "I'm not Nigerian" licenses nothing. Without one
# the claim is cut, and the writer words it by place ("a creator posting from Lagos", "seen in Nigeria's feeds").
# Naija and Mzansi count as Nigerian and South African, SA, RSA and ZA too before a person noun ("SA rapper Nasty C"),
# and a hyphenated pair ("Ghanaian-British star @z") is one nationality. A demonym before a capitalised word with no
# person noun between ("Nigerian Breweries", "Nigerian Afrobeats") names no person. A possessive is about a person
# only before a handle, a person noun or own ("Nigeria's top comedian @x", "Kenya's own Sauti Sol"), never before an
# event, place or brand ("Nigeria's Independence Day", "Kenya's Mount Kenya"). A plain name with no person noun, as
# in "Burna Boy is Nigerian" or "the Nigerian Burna Boy", is a person only with person context (_person_context), so
# "Jollof Rice is Nigerian" names a dish. A news, media or brand account, or a plain name called a streamer or an
# account ("Showmax, a South African streamer", "Kenyan news account Tuko"), is an organisation. A person with no
# name ("an American rapper") is left to the place rules above.
_NATIONALITY_SAME = {"naija": "nigerian", "nigeria": "nigerian", "mzansi": "south african",
                     "south africa": "south african", "sa": "south african", "za": "south african",
                     "rsa": "south african", "kenya": "kenyan"}
_NATIONALITY_DEMONYMS = "|".join([*_DEMONYMS.values(), "Naija", "Mzansi"])
_PERSON_NOUNS = (r"creator|influencer|rapper|singer|comedian|comic|artist|musician|dj|producer|actor|actress"
                 r"|footballer|player|striker|athlete|streamer|tiktoker|youtuber|star|personality|presenter|host|chef"
                 r"|cook|model|dancer|blogger|vlogger|podcaster|journalist|activist|politician|celebrity|entertainer"
                 r"|skitmaker|writer|author|designer|photographer|filmmaker|director|user|poster|account|man|woman|guy"
                 r"|lady|national|citizen|native")
_ORG_MODIFIERS = {"news", "media", "brand", "company", "business", "government", "official", "streaming", "radio",
                  "tv", "betting", "record", "telecom", "fintech", "parody", "fan", "meme"}
_ORG_NOUNS = {"streamer", "account"}
_HANDLE = r"@[A-Za-z0-9_](?:[A-Za-z0-9_.]*[A-Za-z0-9_])?"
_ORG_WORDS = (r"League|Premier|Cup|Awards?|Breweries|Airways|Airlines|Power|Police|Government|Army|Navy|Senate"
              r"|Parliament|Federation|Union|Association|Embassy|Open|Idol|Broadcasting|Corporation|Bank|Exchange"
              r"|Times|Post|Tribune|News|Railways?|Revenue|Service|Eagles|Stars|Bafana|Banyana|Harambee|Springboks"
              r"|Proteas|Party|Congress|Council|Ministry|Commission|University|College|School|Church|Museum|Festival"
              r"|Network|Radio|TV|Television|Records|Media|Group|Company|Limited|Ltd|Inc|Holdings|Music|Film|Week"
              r"|Month|Show|Brother|Family|Forces|Defence|State|City|Province|County|Region|Valley|Coast|Island")
# A capitalised name of one to four words that is not a sentence opener ("On Sunday, a Nigerian creator") or a place
# ("Nigeria's Lagos State", "South Africa's Western Cape") and holds no organisation word.
_NAME_START = (r"(?!(?:On|In|At|By|For|From|To|Today|Yesterday|Tonight|Meanwhile|Also|Then|This|That|These|Those"
               r"|Last|Next|One|Another|The|A|An|Earlier|Later|However|But|And|Or|So|Still|Here|There|When|While"
               r"|After|Before|Since|Over|During|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|January"
               r"|February|March|April|May|June|July|August|September|October|November|December|Its|His|Her|Their"
               r"|Our|My|We|They|He|She|It)\b)"
               rf"(?!(?:[A-Z][\w'’.-]*\s+){{0,3}}(?:{_ORG_WORDS})\b)(?!{_ANY_PLACE})")
_NAME = _NAME_START + r"[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){0,3}"
_NAME2 = _NAME_START + r"[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){1,3}"
_WHO = rf"(?P<who>{_HANDLE}|{_NAME})"
_WHO2 = rf"(?P<who>{_HANDLE}|{_NAME2})"
_DEMONYM_RUN = rf"(?:{_NATIONALITY_DEMONYMS})(?:-(?:{_NATIONALITY_DEMONYMS}))*"
_NAT = rf"(?i:(?P<nat>{_DEMONYM_RUN}))\b"
# SA, RSA and ZA are a demonym only before a person noun ("SA rapper Nasty C"); "SA artists" names no one.
_NAT_NOUNED = rf"(?P<nat>(?i:{_DEMONYM_RUN})|SA|RSA|ZA)\b"
_NOUN_AFTER = rf"(?i:(?:\s+(?P<mod>[\w'’-]+))?\s+(?P<noun>{_PERSON_NOUNS})\b)"
_CLAUSE_END = r"(?=\s*(?:[.,;:!?)]|$)|\s+(?i:and|but|so|who|with|yet)\b)"
_BE = r"(?i:\s+(?:is|was)\s+(?:(?:a|an)\s+)?)"
_BARE_BE = r"(?i:\s+(?:is|was)\s+)"
_ARTICLE_BE = r"(?i:\s+(?:is|was)\s+(?:a|an)\s+)"
_POSS = rf"(?P<poss>{_ANY_PLACE})['’]s"
# Each form, and whether a plain name in it (not a handle) needs person context, see _person_context.
_NATIONALITY_FORMS = [(re.compile(r"(?<![\w@-])" + form), context) for form, context in (
    (rf"{_NAT_NOUNED}{_NOUN_AFTER}(?i:\s*,?\s*)(?P<who>{_HANDLE}|{_NAME})", False),
    (rf"{_NAT}\s+(?P<who>{_HANDLE})", False),
    (rf"(?i:the)\s+{_NAT}\s+(?P<who>{_NAME})", True),
    (rf"{_WHO}\s*[,(]\s*(?i:(?:a|an|the)\s+)(?i:[\w'’-]+\s+)?{_NAT}{_NOUN_AFTER}", False),
    (rf"{_WHO}\s*\(\s*{_NAT}\s*\)", True),
    (rf"{_WHO}{_BE}(?i:[\w'’-]+\s+)?{_NAT}{_NOUN_AFTER}", False),
    (rf"{_WHO2}{_ARTICLE_BE}{_NAT}{_CLAUSE_END}", False),
    (rf"{_WHO}{_BARE_BE}{_NAT}{_CLAUSE_END}", True),
    (rf"{_WHO}\s*,?\s+(?i:who){_BE}{_NAT}(?:{_NOUN_AFTER}|{_CLAUSE_END})", False),
    (rf"(?P<born>(?:{_ANY_PLACE}|(?i:{_NATIONALITY_DEMONYMS})))(?i:-born)(?i:(?:\s+[\w'’-]+)?\s+(?:{_PERSON_NOUNS})\b)?"
     rf"(?i:\s*,?\s*)(?P<who>{_HANDLE}|{_NAME})", False),
    (rf"{_POSS}(?i:\s+own)?\s+(?P<who>{_HANDLE})", False),
    (rf"{_POSS}\s+(?i:own)\s+(?P<who>{_NAME})", False),
    (rf"{_POSS}(?i:\s+own)?{_NOUN_AFTER}(?i:\s*,?\s*)(?P<who>{_HANDLE}|{_NAME})", False),
)]
_LEADING_NOUN = re.compile(rf"(?i:(?:{_PERSON_NOUNS})\s+)")
_PRONOUN = re.compile(r"(?i:\b(?:he|she|his|her|him|himself|herself)\b)")


def _flat(text) -> str:
    return " ".join(str(text or "").replace("’", "'").split()).lower()


def _nationality_key(m) -> str:
    """"nigerian" for a demonym or a country's possessive (Naija and Nigeria's both), "ghanaian-british" for a
    hyphenated pair, "born lagos" for a birthplace, "of <place>" for the possessive of any other place."""
    groups = m.groupdict()
    if groups.get("born"):
        return "born " + _NATIONALITY_SAME.get(_flat(groups["born"]), _flat(groups["born"]))
    if groups.get("poss"):
        place = _flat(groups["poss"])
        return _NATIONALITY_SAME.get(place) or "of " + place
    return "-".join(_NATIONALITY_SAME.get(part, part) for part in _flat(groups["nat"]).split("-"))


def _person_context(text, m) -> bool:
    """Whether a plain name is a person: a person noun right before it ("rapper Burna Boy is Nigerian"), or he, she,
    his, her or him later in its sentence ("The Nigerian Burna Boy posted his skit"). "Jollof Rice is Nigerian" and
    "the Nigerian Independence Day" have neither."""
    before = text[:m.start("who")]
    rest = re.split(r"[.!?](?:\s|$)", text[m.end():], maxsplit=1)[0]
    return bool(re.search(rf"(?i:\b(?:{_PERSON_NOUNS})\s*,?\s*)$", before) or _PRONOUN.search(rest))


def _nationality_claims(text) -> tuple[tuple[str, str], ...]:
    """The (nationality, person) pairs text states, lower case: "Nigerian creator @x" gives ("nigerian", "@x")."""
    return _nationality_claims_of(str(text or ""))


@functools.lru_cache(maxsize=4096)
def _nationality_claims_of(text) -> tuple[tuple[str, str], ...]:
    # Cached, so each cited post is parsed once however many claims cite it.
    text = _NOT_PLACES.sub(" ", text)
    found = []
    for form, context in _NATIONALITY_FORMS:
        for m in form.finditer(text):
            groups = m.groupdict()
            who = groups["who"]
            if not who.startswith("@") and (lead := _LEADING_NOUN.match(who)):
                who = who[lead.end():]
            elif not who.startswith("@") and context and not _person_context(text, m):
                continue
            who = re.sub(r"'s$", "", _flat(who).rstrip("."))
            noun, mod = _flat(groups.get("noun")), _flat(groups.get("mod"))
            if not who or mod in _ORG_MODIFIERS or noun in _ORG_NOUNS and not who.startswith("@"):
                continue
            pair = (_nationality_key(m), who)
            if pair not in found:
                found.append(pair)
    return tuple(found)


def _says_own_nationality(text, key) -> bool:
    """Whether a person's own post states the nationality or birthplace affirmatively: "I'm Nigerian", "proud
    Nigerian", "as a Nigerian", "born in Lagos", "Lagos-born", never "I'm not Nigerian"."""
    text = _flat(text)
    words = [w for w, same in _NATIONALITY_SAME.items() if same == key.removeprefix("born ")] + [key.removeprefix("born ")]
    alts = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in words)
    if key.startswith("born "):
        return bool(re.search(rf"(?<!not )(?<!\w)(?:born\s+(?:and\s+raised\s+)?in\s+(?:{alts})|(?:{alts})-born)(?!\w)",
                              text))
    return bool(re.search(r"(?<!\w)(?:i'?m|i\s+am|proud\s+to\s+be|proud(?:ly)?|as)\s+(?:(?:a|an|proud|true|proper)\s+)"
                          rf"{{0,2}}(?:{alts})(?!\w)", text))


def _nationality_problems(text, records) -> list[str]:
    """One problem per nationality or birthplace text gives a named person that no cited post's own text states.
    records is (id, record) pairs."""
    problems = []
    for key, who in _nationality_claims(text):
        if any(_flat(r.get("handle")).lstrip("@") == who.lstrip("@") and _says_own_nationality(r.get("text"), key)
               or (key, who) in _nationality_claims(r.get("text")) for _, r in records):
            continue
        problems.append(f"calls {who} {key} but no cited post's own text states that nationality for them; a post's "
                        "market, location or feed does not establish nationality: word it by place, as a creator "
                        "posting from a place or seen in a market's feeds")
    return problems


_UNSET = object()

# K5 tone cap (progress/L3.md, Decisions from Albert; TRUST.md section 6). Until a Lagos or Nairobi reviewer is named,
# a tone claim resting on a mostly non-English NG or KE post is capped at single_source.
TONE_CAPPED_MARKETS = ("NG", "KE")
# A claim is about tone when it uses one of these words, the word with -s, -d, -ed or -ing (a final e dropped
# before -ing), so pride, prides, prided and priding all count, or its adverb: -ily for a final y (angrily), -ally
# after -ic (sarcastically) and -ly otherwise (bitterly, jokingly).
TONE_WORDS = ("tone", "mood", "sentiment", "angry", "anger", "furious", "outraged", "outrage", "frustrated",
              "frustration", "annoyed", "upset", "bitter", "proud", "joking", "jokey", "sarcastic", "sarcasm", "ironic",
              "irony", "cynical", "cynicism", "celebratory", "mocking", "playful", "hopeful", "excited", "resigned",
              "defiant", "pride", "joke", "humour", "humor", "banter", "negative", "positive", "optimistic",
              "pessimistic", "fed up", "disappointed", "happy", "hostile", "mock", "mockery", "praise", "celebrate",
              "complain", "grief", "grieve", "laugh", "rage", "fury", "funny", "humorous", "cheerful", "upbeat",
              "livid", "irritated", "resentment", "disgust")


def _adverb(word):
    return word[:-1] + "ily" if word.endswith("y") else word + "ally" if word.endswith("ic") else word + "ly"


TONE = re.compile(r"\b(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") + r"(?:s|d|ed|ing)?"
                                      + (f"|{w[:-1]}ing" if w.endswith("e") else "") + f"|{_adverb(w)}"
                                      for w in TONE_WORDS) + r")\b",
                  re.I)
# Words that mark a post as Pidgin, Sheng, Swahili, Yoruba, Hausa or Igbo, matched in any case. None is also an
# English word or a common name.
LANGUAGE_MARKERS = frozenset((
    "abeg wetin dey sabi oya wahala sef shey una dem pikin oga sha jare comot nawa gbege ehn"  # Pidgin
    " maze manze niaje msee mathe buda fiti mbogi noma mtaa ganji keja rada"  # Sheng
    " hii kabisa sana hapana ndio lakini wewe sisi wao serikali watu kwa ni hakuna pesa kazi bado tena"
    " yetu wetu huyu nini gani sawa asante karibu habari"  # Swahili
    " sugbon awon oluwa olorun ekaro jor ehen"  # Yoruba
    " wallahi gaskiya sannu yanzu babu akwai mutane kuma"  # Hausa
    " biko nna chineke kedu nwanne ndi ihe anyi ndewo daalu"  # Igbo
).split())
# Common English words. A text is mostly non-English when it holds a marker, or when under ENGLISH_SHARE of its
# words, leaving out names (capitalised words not on this list), hashtags, handles and links, are on this list.
COMMON_ENGLISH = frozenset((
    "a an the and or but if so because as than then that this these those there here what which who whom whose when"
    " where why how all any some no not none every each both few more most much many other such only own same too"
    " very just also again still even ever never always now today tonight yesterday tomorrow week weekend day time"
    " i me my mine we us our you your he him his she her it its they them their is am are was were be been being"
    " have has had do does did doing done will would shall should can could may might must go goes going went gone"
    " get gets got getting make makes made take takes took come comes came see saw seen know knew think thought say"
    " says said tell told want wants give gave look looks feel feels like love hate need needs let keep put"
    " of in on at to for from by with about into over after before under up down out off through against"
    " between during without within around one two three first last next new old good bad best better worst big"
    " small long little right wrong real really well way thing things people man woman guys everyone someone nobody"
    " nothing something everything another yes yeah ok okay please thanks thank lol omg"
    " can't don't won't isn't aren't wasn't didn't doesn't i'm it's that's we're you're they're i've let's"
).split())
ENGLISH_SHARE = 0.25
_LINKS = re.compile(r"https?://\S+|[#@][\w.]+")
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")

INSUFFICIENT = ("42 found fewer than two claims that passed its checks, so it cannot answer this yet. "
                "The gaps list what was searched and why claims were held back.")


def check_answer(draft: dict, ctx, warehouse, *, window, markets=None, market=_UNSET) -> tuple[dict, list[dict]]:
    """Run the code claim checks on a draft answer. Returns the checked answer and one verdict row per rule per claim,
    plus a row for each short_answer, context, so_what, watch_next or gaps item that is blanked or dropped.

    markets lists the markets the question asks about (ZA, NG, KE); an empty list checks only the window.
    market is the older single-market form: market="ZA" means markets=["ZA"], and market=None keeps its
    fallback of holding each claim to the market of its first cited record.
    """
    if markets is None:
        if market is _UNSET:
            raise TypeError("check_answer needs markets=[...] or market=")
        markets = [market] if market else None
    reruns, ids = {}, _id_pattern(draft.get("claims") or [], ctx)
    prefetch_reruns(draft.get("claims") or [], ctx, warehouse, reruns)
    allow = _allowance(window, ctx.as_of)
    _load_promotion(ctx, warehouse)
    draft_records = {r.get("id"): r for r in draft.get("evidence") or [] if isinstance(r, dict)}
    verdicts, gaps = [], copy.deepcopy(draft.get("gaps") or [])
    survivors, cut = [], {}  # cut: claim id -> (claim, {rule: reason})

    def row(claim_id, rule, verdict, reason):
        verdicts.append({"claim_id": claim_id, "rule": rule, "verdict": verdict, "reason": reason,
                         "checker": "code", "run_id": ctx.run_id})

    claim_rows, seen = [], set()
    for original in draft.get("claims") or []:
        claim = copy.deepcopy(original)
        eids = [e for e in claim.get("evidence_ids") or [] if isinstance(e, str)]
        records = [ctx.evidence[e] for e in eids if e in ctx.evidence]
        k1 = _k1(claim, eids, ctx.evidence, draft_records)
        if claim.get("id") in seen:
            k1.insert(0, f"claim id {claim.get('id')} repeats an earlier claim's id")
        seen.add(claim.get("id"))
        k3, leaned = _k3(claim.get("text"), eids, ctx.evidence, window, markets)
        problems = {
            "K1": k1,
            "K2": _k2(claim, ctx, warehouse, reruns, records, ids, allow),
            "K3": k3,
            "K6": _k6(claim, records),
            "K8": _k8(claim.get("text"), records),
            "K9": [reason] if (reason := _forecast_problem(claim.get("text"), ctx, eids, records=records)) else [],
        }
        reasons = {rule: "; ".join(p) for rule, p in problems.items() if p}
        if "K6" in reasons:
            reasons["K6"] = "breach: " + reasons["K6"]

        top, why = _max_label(claim, records, leaned, ctx)
        label = claim.get("label")
        if LABEL_RANK.get(label, len(LABEL_RANK)) > LABEL_RANK[top]:
            claim["label"] = top
            k5 = ("downgrade", f"label {label} lowered to {top}: {why}")
        else:
            k5 = ("pass", f"label {label} is within the maximum {top}: {why}")

        claim_rows.append((claim, reasons, k5))
        if reasons:
            cut[claim.get("id")] = (claim, reasons)
        else:
            survivors.append(claim)

    short_answer = draft.get("short_answer") or ""
    for claim, reasons, k5 in claim_rows:
        cid = claim.get("id")
        for rule in ("K1", "K2", "K3"):
            row(cid, rule, "cut" if rule in reasons else "pass", reasons.get(rule, _PASS[rule]))
        row(cid, "K5", *k5)
        for rule in ("K6", "K8"):
            row(cid, rule, "cut" if rule in reasons else "pass", reasons.get(rule, _PASS[rule]))
        row(cid, "K9", "cut" if "K9" in reasons else "pass",
            reasons.get("K9") or _k9_pass_reason(claim.get("text"), ctx, claim.get("evidence_ids")))
        if reasons:
            rests = "; the short answer rests on it" if _mentions(short_answer, cid, claim.get("text")) else ""
            row(cid, "K10", "cut", f"cut by {', '.join(reasons)}{rests}; the answer is at most partial")
            for rule in reasons:
                gaps.append(_code_gap(f"Claim {cid} cut", rule, _searched(rule, claim, ctx.evidence, ctx.queries)))
        else:
            row(cid, "K10", "pass", "claim survives every code check")

    fields = {k: draft.get(k) for k in ("short_answer", "context", "so_what", "watch_next")}
    fields["gaps"] = gaps
    fields, headline_cut = _answer_fields(fields, survivors, ctx, row, allow=allow,
                                          gap_indexes=range(len(draft.get("gaps") or [])))
    gaps = fields["gaps"]
    status, short_answer = _lower_status(draft.get("status"), fields["short_answer"], bool(cut) or headline_cut,
                                         len(survivors))

    gaps.extend(source_gaps(ctx, window))

    cited = []
    for claim in survivors:
        for eid in claim.get("evidence_ids") or []:
            if eid not in cited:
                cited.append(eid)

    answer = {
        "status": status,
        "as_of": draft.get("as_of"),
        "short_answer": short_answer,
        "claims": survivors,
        "evidence": [_output_record(ctx.evidence[eid]) for eid in cited],
        "so_what": fields["so_what"],
        "watch_next": fields["watch_next"],
        "gaps": gaps,
    }
    if "context" in fields:
        answer["context"] = fields["context"]
    return answer, verdicts


def recheck_fields(answer: dict, ctx, *, window=None, code_gaps=()) -> tuple[dict, list[dict]]:
    """Re-run the answer-level checks against the claims now in the answer, without touching the claims or evidence:
    K6, K2 and K8 on short_answer, context, so_what, watch_next and gaps, then the K10 status rule. Run it after
    apply_support. Every gap is rescanned, the checks' own included. Pass window to allow its phrase and day ranges.
    code_gaps holds the indexes of the gaps the code wrote (code_gap_indexes); every other gap is read as the
    writer's, whatever its shape."""
    answer = copy.deepcopy(answer)
    verdicts = []

    def row(where, rule, verdict, reason):
        verdicts.append({"claim_id": where, "rule": rule, "verdict": verdict, "reason": reason,
                         "checker": "code", "run_id": ctx.run_id})

    claims = [c for c in answer.get("claims") or [] if isinstance(c, dict)]
    fields, headline_cut = _answer_fields(answer, claims, ctx, row, allow=_allowance(window, ctx.as_of),
                                          gap_indexes=range(len(answer.get("gaps") or [])), code_gaps=code_gaps)
    answer.update(fields)
    answer["status"], answer["short_answer"] = _lower_status(answer.get("status"), fields["short_answer"],
                                                             headline_cut, len(claims))
    return answer, verdicts


def _lower_status(status, short_answer, lowered, surviving):
    """K10: a cut makes a complete answer partial, and under 2 surviving claims gives insufficient_evidence.
    A status is only ever lowered, and a refusal stays a refusal."""
    if status != "refused":
        if status == "complete" and lowered:
            status = "partial"
        if surviving < 2:
            status, short_answer = "insufficient_evidence", INSUFFICIENT
    return status, short_answer


def code_gap_indexes(draft_gaps, gaps) -> set:
    """The indexes in gaps of the gaps the code wrote. check_answer keeps the draft's surviving gaps first and in their
    order, and the checks, source_gaps and apply_support only append, so every gap past the draft's surviving run is
    the code's. A code gap that equals a later draft gap is read as the writer's, the stricter reading."""
    rest = iter(draft_gaps)
    for i, gap in enumerate(gaps):
        if not any(gap == drafted for drafted in rest):  # consumes rest up to the match
            return set(range(i, len(gaps)))
    return set()


def _answer_fields(fields, claims, ctx, row, *, allow, gap_indexes, code_gaps=()):
    """K6, K2 and K8 on short_answer, context, so_what and watch_next, and K6 and K2 on the gaps at gap_indexes,
    all against claims. A gap at an index in code_gaps is read as the code's, every other as the writer's. Returns
    the checked fields and whether the short answer was blanked."""
    gaps = copy.deepcopy(fields.get("gaps") or [])
    scanned, gap_indexes, code_gaps = len(gaps), set(gap_indexes), set(code_gaps)
    kept = {c.get("id"): c for c in claims}
    ids = _id_pattern(claims, ctx)

    def drop(where, label, section_text, reasons):
        for rule, why in reasons.items():
            row(where, rule, "cut", why)
            gaps.append(_code_gap(f"{label} removed", rule, section_text))

    out = {}
    short_answer = fields.get("short_answer") or ""
    problems = ({} if short_answer == INSUFFICIENT
                else _field_problems(short_answer, claims, ctx.evidence, ids, allow, ctx))
    if problems:
        drop("short_answer", "Short answer", "the short answer text", problems)
        short_answer = ""
    out["short_answer"], headline_cut = short_answer, bool(problems)

    context = fields.get("context")
    if isinstance(context, str):
        problems = _field_problems(context, claims, ctx.evidence, ids, allow, ctx)
        for rule, why in problems.items():
            row("context", rule, "cut", why)
        out["context"] = "" if problems else context

    kept_gaps = []
    for i, gap in enumerate(gaps[:scanned]):
        problems = _gap_problems(gap, claims, allow, ids, i in code_gaps, ctx) if i in gap_indexes else {}
        for rule, why in problems.items():
            row(f"gaps/{i}", rule, "cut", why)
        if not problems:
            kept_gaps.append(gap)
    gaps[:scanned] = kept_gaps

    for section in ("so_what", "watch_next"):
        out[section] = []
        for i, item in enumerate(fields.get(section) or []):
            item = copy.deepcopy(item)
            refs = item.get("claim_ids") or []
            item["claim_ids"] = [r for r in refs if r in kept]
            hits = _text_breaches(item.get("text"))
            if not hits and not item["claim_ids"]:
                why = (f"rests only on cut claims {', '.join(map(str, refs))}" if refs
                       else "rests on no claim: claim_ids is empty or missing")
                row(f"{section}/{i}", "K10", "cut", why)
                continue
            problems = _field_problems(item.get("text"), [kept[r] for r in item["claim_ids"]], ctx.evidence, ids,
                                       allow, ctx, forecast_flag=section == "watch_next" and item.get("forecast") is True,
                                       watch=section == "watch_next")
            if problems:
                drop(f"{section}/{i}", f"{section} item {i}", f"the {section} text", problems)
                continue
            out[section].append(item)
    out["gaps"] = gaps
    return out, headline_cut


def _id_pattern(claims, ctx):
    """The claim ids in write_answer's c1 to c999 form, stored evidence ids and recorded query ids that mix letters
    and digits, as one pattern, so c1, p12 or q_1 reads as an id and not a figure. Any other claim id is model text, so
    x_5000 stays a figure. None when there are none."""
    ids = {c.get("id") for c in claims if isinstance(c, dict) and re.fullmatch(r"c[0-9]{1,3}", str(c.get("id")))}
    ids |= set(ctx.evidence) | set(ctx.queries)
    ids = sorted(i for i in ids if isinstance(i, str) and re.search(r"[^\W\d_]", i) and re.search(r"\d", i))
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, ids)) + r")(?!\w)") if ids else None


def _unpinned(text, claims, records=None, ids=None, allow=None, ctx=None):
    """Numerals in text that no claim shows in its own text or its numbers. Quoted spans verbatim in records are
    left out; records=None scans every span. ids is the pattern of ids to read as ids, not figures, allow blanks
    the allowed spans, and ctx, when given, blanks what _blank_held pins to this run."""
    numbers = [n for c in claims for n in c.get("numbers") or [] if isinstance(n, dict)]
    in_claims = {(v, p) for c in claims
                 for _, v, _, p in _numerals(c.get("text"), lambda span: True, ids=ids, allow=allow, ctx=ctx)}
    return [t for t, v, d, p in _numerals(text, None if records is None else _in_records(records), ids=ids, allow=allow,
                                          ctx=ctx)
            if (v, p) not in in_claims and not any(_shows(n.get("value"), v, d, p) for n in numbers)]


def _forecast_phrase(text):
    return " ".join(WORDS.findall(normalise(str(text or "")).casefold()))


def _conditional_watch_residual(text):
    match = CONDITIONAL_WATCH.match(str(text or ""))
    if not match:
        return text
    remainder = str(text or "")[match.end():]
    boundary = re.search(r"[.!?;:,]\s*|\n+|\s+[-\u2013\u2014]\s+|\b(?:and|but|while|whereas)\b",
                         remainder, re.I)
    return remainder[boundary.end():] if boundary else ""


def _load_promotion(ctx, warehouse):
    """Read promotion once for each forecast logged in this run (forecast_promotion). A forecast counts as promoted
    only when its cohort is, and its logged statement is exactly the line built from its stored row: the item's label,
    market, target and horizon. Only a run that logged a forecast reads anything; a failed read promotes none. The
    logged forecasts themselves are never changed."""
    forecasts = getattr(ctx, "forecasts", None)
    read = getattr(ctx, "promotion_read", None)
    if not isinstance(forecasts, dict) or read is None:
        return
    unread = [fid for fid, f in forecasts.items() if isinstance(f, dict) and f.get("statement") and fid not in read]
    if not unread:
        return
    today = ctx.as_of.astimezone(SAST).date() if ctx.as_of.tzinfo else ctx.as_of.date()
    rows = promoted_forecasts(warehouse, unread, today)
    read.update(unread)
    promoted = []
    for fid in unread:
        line = publishable_line(rows[fid]) if fid in rows else None
        if line and _forecast_phrase(line) == _forecast_phrase(forecasts[fid].get("statement")):
            ctx.promoted_forecasts[fid] = {**rows[fid], "line": line}
            promoted.append(fid)
    if promoted:
        ctx.emit("forecast_promotion", read=unread, promoted=promoted,
                 score_run_ids=[ctx.promoted_forecasts[f].get("score_run_id") for f in promoted])


def _promoted_match(text, ctx, evidence_ids):
    """The promoted forecast a text publishes, or None. Its logged statement must equal the text and its evidence
    must all be shown, and no other forecast logged in this run may share that statement."""
    phrase = _forecast_phrase(text)
    forecasts = getattr(ctx, "forecasts", {}) or {}
    promoted = getattr(ctx, "promoted_forecasts", {}) or {}
    if not phrase or not isinstance(forecasts, dict):
        return None
    shown_ids = {eid for eid in evidence_ids or [] if isinstance(eid, str)}
    same = [f for f in forecasts.values() if isinstance(f, dict) and _forecast_phrase(f.get("statement")) == phrase]
    if len(same) != 1:
        return None
    logged_ids = {eid for eid in same[0].get("evidence_ids") or [] if isinstance(eid, str)}
    found = promoted.get(same[0].get("forecast_id"))
    if not (found and logged_ids and logged_ids.issubset(shown_ids)):
        return None
    return found


def _forecast_problem(text, ctx, evidence_ids, *, flagged=False, watch=False, records=None):
    text = str(text or "")
    if not flagged:
        forecast_text = _conditional_watch_residual(text) if watch else text
        forecast_text = _blank_spans(normalise(forecast_text), _in_records(records)) if records else forecast_text
        if not FUTURE_ASSERTION.search(forecast_text):
            return None
    phrase = _forecast_phrase(text)
    shown_ids = {eid for eid in evidence_ids or [] if isinstance(eid, str)}
    forecasts = getattr(ctx, "forecasts", {}) or {}
    if isinstance(forecasts, dict):
        for forecast in forecasts.values():
            if not isinstance(forecast, dict):
                continue
            statement = _forecast_phrase(forecast.get("statement"))
            logged_ids = {eid for eid in forecast.get("evidence_ids") or [] if isinstance(eid, str)}
            if statement and statement == phrase and logged_ids and logged_ids.issubset(shown_ids):
                return None if _promoted_match(text, ctx, evidence_ids) else FORECAST_LOGGED_REASON
    return FORECAST_NO_LOG_REASON


def _k9_pass_reason(text, ctx, evidence_ids):
    found = _promoted_match(text, ctx, evidence_ids)
    if not found:
        return _PASS["K9"]
    return (f"forecast {found.get('forecast_id')} is the line of its stored row, and its cohort {found.get('rule')} "
            f"{found.get('target')} {found.get('horizon')} days beat persistence in forecast_score run "
            f"{found.get('score_run_id')}")


def _field_problems(text, resting, evidence, ids=None, allow=None, ctx=None, *, forecast_flag=False, watch=False):
    records = [evidence[e] for c in resting for e in c.get("evidence_ids") or [] if e in evidence]
    hits = _text_breaches(text, records)
    if hits:
        return {"K6": "breach: " + "; ".join(hits)}
    unpinned = _unpinned(text, resting, records, ids, allow, ctx)
    evidence_ids = list(dict.fromkeys(e for c in resting for e in c.get("evidence_ids") or [] if e in evidence))
    forecast = _forecast_problem(text, ctx, evidence_ids, flagged=forecast_flag, watch=watch, records=records)
    # A field's home markets are the located markets of the posts its claims cite, which K3 already held to the ask;
    # with none, the ask's market.
    cited = {e: evidence[e] for c in resting for e in c.get("evidence_ids") or [] if e in evidence}
    home = {r.get("market") for r in cited.values() if _located(r)} or {getattr(ctx, "market", None)} - {None}
    cited_rows = list(cited.items())
    problems = {
        "K2": "; ".join(f"numeral {t} is not a number of the claims it rests on" for t in unpinned),
        "K3": "; ".join(f"the text {p}" for p in
                         [*_place_problems(_places(text), cited_rows, home),
                          *_source_scope_problems(text, cited_rows),
                          *_nationality_problems(text, cited_rows)]),
        "K8": "; ".join(_k8(text, records)),
        "K9": forecast or "",
    }
    return {rule: why for rule, why in problems.items() if why}


def _live_params(ctx, route=None):
    """The string param values of the SocialCrawl calls this run made (ctx.sc_calls, never the streamed events), on
    route only when route is given."""
    return {v for call in ctx.sc_calls if route is None or call.get("route") == route
            for v in (call.get("params") or {}).values() if isinstance(v, str)}


def _blank_held(text, ctx):
    """Blank what this run holds (ctx), in any field or gap: a query word holding a digit and a letter straight after
    a SocialCrawl route when a SocialCrawl call on that route, as ctx.sc_calls records it, had it as a param
    ("reddit/search 7colours"), a link that is a stored post's url, a handle of a stored post, and a hashtag holding a
    letter and a digit that is in a stored post's text or is a param of a SocialCrawl call. Ids are _id_pattern's,
    blanked later. Any other word after a route is plain text, so "reddit/search 5000-plus" keeps its 5000, and a word
    an sql_query param holds pins nothing."""
    for route in dict.fromkeys(call.get("route") for call in ctx.sc_calls):
        words = {v for v in _live_params(ctx, route) if re.fullmatch(r"(?=.*\d)(?=.*[^\W\d_])[\w.-]+", v)}
        if isinstance(route, str) and words:
            text = re.sub(r"(?<![\w./-])" + re.escape(route) + r"(?:/v[1-9])?\s+(?:"
                          + "|".join(map(re.escape, sorted(words, key=len, reverse=True))) + r")" + _TOKEN_END,
                          " ", text, flags=re.I)
    records = [r for r in ctx.evidence.values() if isinstance(r, dict)]
    urls = {r.get("url") for r in records}
    text = re.sub(r"https?://\S+", lambda m: " " if m.group(0).rstrip(".,;:!?)'\"") in urls
                  and m.group(0).rstrip(".,;:!?)'\"") else m.group(0), text)
    handles = {str(r.get("handle") or "").lower().lstrip("@") for r in records} - {""}
    text = re.sub(r"@[\w.]*\w", lambda m: " " if m.group(0)[1:].lower() in handles else m.group(0), text)
    params = {v.lower() for v in _live_params(ctx)}
    posts = " ".join(str(r.get("text") or "") for r in records)

    def tag(m):
        held = m.group(0).lower() in params or m.group(0)[1:].lower() in params
        held = held or re.search(r"(?<![\w#])" + re.escape(m.group(0)) + r"(?!\w)", posts, re.I)
        return " " if held else m.group(0)
    return re.sub(r"#(?=[\w.]*[^\W\d_])[\w.]*\d[\w.]*(?<=\w)", tag, text)


def _gap_problems(gap, claims, allow, ids=None, code=False, ctx=None):
    """K6 on a gap, then K2 on every numeral no claim shows. The spans allow blanks, the route names, what
    _blank_held pins to ctx and WRITER_GAP_ALLOWED are allowed, and the code's own gap wording only when code says the
    code wrote the gap. ids defaults to _id_pattern's when ctx is given. Quoted spans are scanned too, since a gap gets
    no K8."""
    texts = [str(gap.get(k) or "") for k in ("what", "searched", "why")]
    hits = list(dict.fromkeys(h for t in texts for h in _text_breaches(t)))
    if hits:
        return {"K6": "breach: " + "; ".join(hits)}
    evidence_ids = [e for c in claims for e in c.get("evidence_ids") or [] if ctx and e in ctx.evidence]
    forecast = _forecast_problem(" ".join(texts), ctx, evidence_ids)
    if forecast:
        return {"K9": forecast}
    unpinned = dict.fromkeys(t for text in texts for t in gap_numerals(text, claims, ctx, allow, ids, code))
    if not unpinned:
        return {}
    return {"K2": "; ".join(f"numeral {t} is not a number of the surviving claims" for t in unpinned)}


def gap_numerals(text, claims, ctx=None, allow=None, ids=None, code=False):
    """The numerals in one gap text, or a follow-up built from one, that no claim shows and nothing allows."""
    if ids is None and ctx is not None:
        ids = _id_pattern(claims, ctx)
    return _unpinned((GAP_ALLOWED if code else WRITER_GAP_ALLOWED).sub(" ", text), claims, ids=ids, allow=allow,
                     ctx=ctx)


def _allowance(window, as_of):
    """What every field may carry with no numbers entry: the window phrase ("last 8 days", "past 8 days", "8-day")
    and day ranges inside the window, when there is a window, ISO dates that parse and fall from FIRST_YEAR to
    EVENT_YEARS_AHEAD past the as_of year, year phrases no later than the as_of year, and years that name an event up
    to EVENT_YEARS_AHEAD past it, and the day-month dates, d/m/yyyy dates and clock times _real_dates allows, to the
    same year. Returns a function that blanks them. ISO dates go first, so "28 September, 2026-09-21" keeps its ISO
    date whole, and _real_dates goes last, so a day range keeps its first day."""
    phrase = None
    if window is not None:
        days = (window[1] - window[0]).days + 1
        phrase = re.compile(rf"\b(?:last|past)\s+{days}\s+days\b|(?<![\d.,]){days}-day\b", re.I)
        window_days = {(window[0] + timedelta(days=i)).day for i in range(days)}

    def in_window(m):
        month = MONTHS.index(m.group("month")[:3].lower()) + 1
        years = [int(m.group("year"))] if m.group("year") else sorted({window[0].year, window[1].year})
        for year in years:
            try:
                first, last = date(year, month, int(m.group("d1"))), date(year, month, int(m.group("d2")))
            except ValueError:
                continue
            if window[0] <= first <= last <= window[1]:
                return True
        return False

    def day_in_window(day, month):
        for year in sorted({window[0].year, window[1].year}):
            try:
                if window[0] <= date(year, month, day) <= window[1]:
                    return True
            except ValueError:
                continue
        return False

    def allow(text):
        text = _iso_dates(text, as_of.year + EVENT_YEARS_AHEAD)
        if phrase is not None:
            text = phrase.sub(" ", text)
            text = DAY_RANGE.sub(lambda m: " " if in_window(m) else m.group(0), text)
            text = DAY_SLASH.sub(lambda m: " " if day_in_window(int(m.group("day")), int(m.group("month")))
                                 else m.group(0), text)
            text = ORDINAL_RANGE.sub(lambda m: " " if int(m.group("d1")) <= int(m.group("d2")) and
                                     {int(m.group("d1")), int(m.group("d2"))} <= window_days else m.group(0), text)
        text = EVENT_YEAR.sub(event, text)
        text = YEAR.sub(lambda m: " " if int(m.group("year")) <= as_of.year else m.group(0), text)
        return _real_dates(text, as_of.year + EVENT_YEARS_AHEAD)

    def event(m):
        groups = [g for g in ("first", "second", "the", "name", "lead") if m.group(g)]
        if any(int(m.group(g)) > as_of.year + EVENT_YEARS_AHEAD for g in groups):
            return m.group(0)
        text = m.group(0)
        for g in reversed(groups):
            start, end = m.start(g) - m.start(), m.end(g) - m.start()
            text = text[:start] + " " + text[end:]
        return text
    return allow


def _iso_dates(text, last_year):
    """Blank each ISO date that datetime.fromisoformat parses whole, time and offset included, with a year from
    FIRST_YEAR to last_year. Any other stays, so 2026-99-99, 5000-09-21, 2026-09-24T99:99:99Z and a +99:99 offset
    are figures."""
    def blank(m):
        text = m.group(0)
        try:
            year = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text).year
        except ValueError:
            return text
        return " " if FIRST_YEAR <= year <= last_year else text
    return ISO_DATE.sub(blank, text)


def _real_dates(text, last_year):
    """Blank each day-month date, d/m/yyyy date and clock time that is real: a day the month has (29 February with
    no year, since some year has it), a year from FIRST_YEAR to last_year (two digits read as 20yy), an hour 0 to 23
    and a minute and second 0 to 59 where CLOCK_BEFORE, CLOCK_CHAIN, CLOCK_RANGE or CLOCK_AFTER places it, plus real
    12-hour and 18h00 times, and quarters with their year and decades to the same year. Any other stays, so
    99/99/2026, 31/12/5000, 31 February, 99:99, a 3:10 ratio and Q1 5000 are figures."""
    def real(year, month, day):
        try:
            date(year if year is not None else 2024, month, day)
        except ValueError:
            return False
        return year is None or FIRST_YEAR <= year <= last_year

    def named(m):
        month = MONTHS.index(m.group("month")[:3].lower()) + 1
        if m.groupdict().get("alone"):
            ok = real(int(m.group("alone")), month, 1)
        else:
            ok = real(int(m.group("year")) if m.group("year") else None, month, int(m.group("day")))
        return " " if ok else m.group(0)

    def slash(m):
        year = int(m.group("year")) + (2000 if len(m.group("year")) == 2 else 0)
        return " " if real(year, int(m.group("month")), int(m.group("day"))) else m.group(0)

    def in_range(m):
        year = m.group("year")
        if year is None:
            return " "
        year = int(year) + (2000 if len(year) == 2 else 0)
        return " " if FIRST_YEAR <= year <= last_year else m.group(0)

    def season(m):
        ok = int(m.group("next")) == (int(m.group("year")) + 1) % 100
        return in_range(m) if ok else m.group(0)

    def time(hour, minute, second=0, first=0):
        return first <= int(hour) <= (12 if first else 23) and int(minute or 0) <= 59 and int(second or 0) <= 59

    last = [None, 0]  # where the last allowed clock ended, and its hour

    def clock(m):
        before = re.findall(r"[\w']+", m.string[:m.start()])[-3:]
        after = re.findall(r"[\w']+", m.string[m.end():])[:3]
        near = " ".join(before + after)
        vetoed = (RATIO_WORD.search(" ".join(re.findall(r"[\w']+", m.string[:m.start()])[-6:] + after))
                  and not (RATIO_BEATEN.search(m.string, 0, m.start()) and int(m.group("hour")) >= 13)
                  or (DURATION_WORD.search(near) or LONG_AFTER.match(m.string, m.end()))
                  and not (VETO_BEATEN.search(m.string, 0, m.start()) or CLOCK_AFTER.match(m.string, m.end())))
        if vetoed or not time(m.group("hour"), m.group("minute"), m.group("second")):
            return m.group(0)
        partner = CLOCK_RANGE.match(m.string, m.end())
        chained = last[0] is not None and CLOCK_CHAIN.fullmatch(m.string, last[0], m.start())
        strong = (CLOCK_AFTER.match(m.string, m.end()) or TIME_NOUN.search(" ".join(after))
                  or CLOCK_STRONG_BEFORE.search(m.string, 0, m.start()) or chained and last[1] > 1)
        weak = (CLOCK_BEFORE.search(m.string, 0, m.start()) or chained
                or TIME_BEFORE.search(" ".join(re.findall(r"[\w']+", m.string[:m.start()])[-6:]))
                or partner and time(partner.group("hour"), partner.group("minute"))
                or m.group("minute") in ("00", "15", "30", "45") and THE_QUARTER_HOUR.search(m.string, 0, m.start()))
        if not (strong or weak and int(m.group("hour")) > 1):
            return m.group(0)
        last[:] = [m.end(), int(m.group("hour"))]
        return " "

    text = MONTH_DAY.sub(named, DAY_MONTH.sub(named, text))
    text = DOT_DATE.sub(slash, SLASH_DATE.sub(slash, text))
    text = NAMED_PERIOD.sub(in_range, SEASON.sub(season, text))
    text = BARE_PERIOD.sub(" ", DECADE.sub(in_range, QUARTER.sub(in_range, text)))
    text = CLOCK_12.sub(lambda m: " " if time(m.group("hour"), m.group("minute"), first=1) else m.group(0), text)
    text = CLOCK_H.sub(lambda m: " " if time(m.group("hour"), m.group("minute")) else m.group(0), text)
    return CLOCK.sub(clock, text)


_PASS = {
    "K1": "every evidence id resolves to a stored record that matches the draft, and every quote is verbatim",
    "K2": "every numeral is pinned to a recorded query and reproduced on re-run",
    "K3": "every cited post is inside the window and market, every named place has located or feed evidence, feed-only "
          "claims say they were seen in that market's feeds with no other mention of that market, and people named "
          "by origin have a post located in their own market",
    "K6": "no age, demographic, Google Trends or generated-evidence breach",
    "K8": "every quoted span is verbatim in a cited record",
    "K9": "no forecast is published before it beats the persistence baseline",
}


# K1 citations

def _k1(claim, eids, stored, draft_records):
    problems = [] if eids else ["claim cites no evidence ids"]
    odd = sum(not isinstance(e, str) for e in claim.get("evidence_ids") or [])
    if odd:  # counted, never shown: the value is model text
        problems.append(f"{odd} evidence id that is not a string" if odd == 1 else
                        f"{odd} evidence ids that are not strings")
    for eid in eids:
        record = stored.get(eid)
        if record is None:
            problems.append(f"evidence {eid} does not resolve to a stored record")
            continue
        for field in REQUIRED_FIELDS:
            if not record.get(field):
                problems.append(f"stored record {eid} has no {field}")
        shown = draft_records.get(eid)
        if shown is not None:
            for field in IDENTITY_FIELDS:
                if not _same(field, shown.get(field), record.get(field)):
                    problems.append(f"draft {field} for {eid} does not match the stored record")
    for quote in claim.get("quotes") or []:
        eid = quote.get("evidence_id")
        if eid not in eids:
            problems.append(f"quote attributed to {eid}, which the claim does not cite")
        elif eid in stored and not _verbatim(quote.get("text"), stored[eid].get("text")):
            problems.append(f"quote not verbatim in stored record {eid}")
    return problems


def _verbatim(quote, source):
    """Whole-word match of at least two words, or one word of eight characters or more."""
    quote = normalise(quote)
    if len(WORDS.findall(quote)) < 2 and len(quote) < 8:
        return False
    return re.search(r"(?<!\w)" + re.escape(quote) + r"(?!\w)", normalise(source)) is not None


def _same(field, shown, stored):
    if shown == stored:
        return True
    if field == "posted_at":
        a, b = _parse_time(shown), _parse_time(stored)
        return a is not None and b is not None and a == b
    return False


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


# K2 numbers

def _k2(claim, ctx, warehouse, reruns, records, ids=None, allow=None):
    problems = []
    entries = [n for n in claim.get("numbers") or [] if isinstance(n, dict)]
    good = []
    for n in entries:
        why = _number_problem(n, ctx, warehouse, reruns)
        if why:
            problems.append(why)
        else:
            good.append(n)
    for text, value, decimals, percent in _numerals(claim.get("text"), _in_records(records), ids=ids, allow=allow,
                                                    ctx=ctx):
        if not any(_shows(n.get("value"), value, decimals, percent) for n in good):
            problems.append(f"numeral {text} has no pinned, reproduced numbers entry")
    return problems


def _recorded_problem(n, ctx):
    """What is wrong with a numbers entry against its recorded query, before any re-run; None when it needs one."""
    qid, value = n.get("query_id"), n.get("value")
    query = ctx.queries.get(qid)
    if query is None:
        return f"number {value}: query_id {qid} is not a query recorded in this run"
    if n.get("run_id") != ctx.run_id:
        return f"number {value}: run_id {n.get('run_id')} is not the pinned run {ctx.run_id}"
    if n.get("result_hash") != query["result_hash"] or result_hash(query["rows"]) != query["result_hash"]:
        return f"number {value}: result_hash does not match the recorded result of {qid}"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return f"number {value!r} is not numeric"
    if not any(_matches(value, cell) for cell in _cells(query["rows"])):
        return f"number {value} is not in the recorded result of {qid}"
    return None


def _rerun(warehouse, query, ctx):
    try:
        return _dispatch_query(ctx, warehouse, query["sql"], query["params"], MAX_BYTES_BILLED)
    except Exception as e:  # the warehouse is a system boundary; a failed re-run cuts, never crashes the answer
        return e


def prefetch_reruns(claims, ctx, warehouse, reruns) -> None:
    """Re-run, at the same time, every query a K2 check below will re-run, once each, into reruns. Only numbers that
    pass every recorded check reach a re-run, exactly as in _number_problem, so nothing extra is queried. Each
    number is still held to its own fresh re-run; only the waiting is shared."""
    wanted = []
    for claim in claims:
        for n in (claim.get("numbers") or []) if isinstance(claim, dict) else ():
            if isinstance(n, dict) and _recorded_problem(n, ctx) is None:
                qid = n.get("query_id")
                if qid not in reruns and qid not in wanted:
                    wanted.append(qid)
    if len(wanted) < 2:
        return  # one re-run gains nothing from a thread; _number_problem runs it in place
    with ThreadPoolExecutor(max_workers=min(RERUN_WORKERS, len(wanted))) as pool:
        results = list(pool.map(lambda qid: _rerun(warehouse, ctx.queries[qid], ctx), wanted))
    reruns.update(zip(wanted, results))


def _number_problem(n, ctx, warehouse, reruns):
    qid, value = n.get("query_id"), n.get("value")
    problem = _recorded_problem(n, ctx)
    if problem:
        return problem
    if qid not in reruns:
        reruns[qid] = _rerun(warehouse, ctx.queries[qid], ctx)
    rerun = reruns[qid]
    if isinstance(rerun, Exception):
        return f"number {value}: re-run of {qid} failed ({type(rerun).__name__})"
    if not any(_matches(value, cell) for cell in _cells(rerun)):
        return f"number {value}: re-run of {qid} no longer returns it"
    places = _number_places(value, ctx.queries[qid]["rows"])
    used = set()
    if not all(_same_number_place(value, place, rerun, used) for place in places):
        return f"number {value}: re-run of {qid} no longer returns it in the same row and column"
    return None


def _number_places(value, rows):
    """The path and complete row of each matching numeric leaf, or the legacy unplaced scalar row."""
    for row in rows if isinstance(rows, (list, tuple)) else [rows]:
        if not isinstance(row, dict):
            yield None, None
            continue
        pending = [((), row)]
        while pending:
            path, cell = pending.pop()
            if isinstance(cell, dict):
                pending.extend(((*path, key), child) for key, child in cell.items())
            elif isinstance(cell, (list, tuple)):
                pending.extend(((*path, index), child) for index, child in enumerate(cell))
            elif isinstance(cell, (int, float, Decimal)) and not isinstance(cell, bool) and _matches(value, cell):
                yield path, row


def _same_number_place(value, place, rerun, used):
    """Consume one matching rerun cell with the complete cohort identity. Only the measured leaf uses the existing
    numeric tolerance; a rerun occurrence cannot satisfy two recorded occurrences."""
    path, recorded = place
    if path is None:
        return True

    def identical(before, after):
        if type(before) is not type(after):
            return False
        if isinstance(before, dict):
            return before.keys() == after.keys() and all(identical(cell, after[key]) for key, cell in before.items())
        if isinstance(before, (list, tuple)):
            return len(before) == len(after) and all(identical(left, right) for left, right in zip(before, after))
        return before == after

    def same(before, after, remaining):
        if not remaining:
            return (isinstance(after, (int, float, Decimal)) and not isinstance(after, bool)
                    and _matches(value, after))
        key, *rest = remaining
        if isinstance(before, dict):
            if not isinstance(after, dict) or before.keys() != after.keys():
                return False
            return all(same(cell, after[name], rest) if name == key else identical(cell, after[name])
                       for name, cell in before.items())
        if not isinstance(after, (list, tuple)) or len(before) != len(after):
            return False
        return all(same(cell, after[index], rest) if index == key else identical(cell, after[index])
                   for index, cell in enumerate(before))

    for index, row in enumerate(rerun or []):
        occurrence = (index, path)
        if occurrence not in used and isinstance(row, dict) and same(recorded, row, path):
            used.add(occurrence)
            return True
    return False


def _cells(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _cells(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _cells(v)
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        yield value


def _matches(value, cell):
    if isinstance(cell, int) and float(value).is_integer():
        return int(value) == cell
    cell = float(cell)
    if cell == 0:
        return value == 0
    return abs(value - cell) <= TOLERANCE * abs(cell)


def _numerals(text, skip=None, ids=None, allow=None, ctx=None):
    """(text, value, decimals, percent) per figure: every run of digits outside the route names (NOT_NUMERALS), what
    _blank_held pins to ctx, the real dates and times and the ids pattern, whatever letters touch it, and every
    spelled multiplier or count. skip(span) says which quoted spans to
    leave out; None scans every span. allow(text) blanks the allowed spans, such as "in 2026". A scaled figure keeps
    the rounding it was written with, so 2.4bn has value 2400000000 and decimals -8. A whole figure with no scale or
    percent, such as 3, 41, 3rd, Q3, USD5 or "three times", has decimals None: only an entry of that exact integer
    shows it. "dozens" and "thousands" have a range as value: an entry from 24 to 143, or 2000 to 999999, shows it.
    A scale no entry is checked against has the empty range UNMATCHED."""
    text = normalise(text)
    text = _blank_spans(text, skip) if skip else text
    text = _blank_held(text, ctx) if ctx is not None else text
    text = NAMED.sub(" ", text)
    # no as_of here, so LAST_YEAR bounds the dates
    text = allow(text) if allow else _real_dates(_iso_dates(text, LAST_YEAR), LAST_YEAR)
    text = NOT_NUMERALS.sub(" ", text)
    text = ids.sub(" ", text) if ids else text
    for m in FIGURE.finditer(text):
        scale = (m.group("suffix") or m.group("word") or "").lower()
        num = Decimal(m.group("num").replace(",", "").replace(" ", ""))
        percent = bool(m.group("pct")) or m.group("range_pct") is not None  # "40-50%" reads both ends as percents
        if m.group("more") or scale in ODD_SCALES:
            yield m.group(0).strip(), UNMATCHED, None, percent
            continue
        if scale == "dozen":
            yield m.group(0).strip(), float(num * 12), None, percent
            continue
        shift = SCALE.get(scale, 0)
        value = float(num.scaleb(shift))
        whole = not (m.group("dec") or shift or percent)
        yield m.group(0).strip(), value, None if whole else len(m.group("dec") or "") - shift, percent
    for m in SPELLED.finditer(text):
        if m.group("cardinal"):
            value = _spelled_value(m.group("cardinal"))
            last = re.findall(r"[a-z]+", m.group("cardinal").lower())[-1]
            decimals = None if m.group("multiplier") else _cardinal_decimals(value, last)
            yield m.group(0), value if decimals is None else round(value, decimals), decimals, False
            continue
        if m.group("every"):  # every second post is the share 0.5
            yield m.group(0), round(1 / EVERY[m.group("every").lower()], 2), 2, False
            continue
        if m.group("and_half"):  # two and a half times is 2.5
            yield m.group(0), _spelled_value(m.group("and_half")) + 0.5, 1, False
            continue
        if m.group("hours"):  # open eight to five names trading hours, not a figure
            continue
        if m.group("hedged_half"):  # nearly half, by half, in half
            yield m.group(0), 0.5, 1, False
            continue
        if m.group("mark"):  # the million mark, the half-million mark
            scale = m.group("mark").lower()
            value = SCALE_WORDS[scale] * (0.5 if m.group("mark_half") else 1)
            decimals = _cardinal_decimals(value, scale)
            yield m.group(0), value if decimals is None else round(value, decimals), decimals, False
            continue
        if m.group("factor") or m.group("pp"):  # a factor of three, five percentage points
            yield m.group(0), _spelled_value(m.group("factor") or m.group("pp")), None, False
            continue
        if m.group("to_a"):  # two to one is the ratio 2, to two places; two to three is the range 2 to 3
            low, high = _spelled_value(m.group("to_a")), _spelled_value(m.group("to_b"))
            if low > high:
                yield m.group(0), round(low / high, 2), 2, False
            else:
                yield m.group("to_a"), low, None, False
                yield m.group("to_b"), high, None, False
            continue
        if m.group("bare"):  # a third, one half, three-quarters: the share, to two places
            frac = m.group("bare_frac").lower().rstrip("s")
            denominator = 2 if frac == "half" else FRACTIONS[frac]
            yield m.group(0), round(WORD_VALUES[m.group("bare").lower()] / denominator, 2), 2, False
            continue
        if m.group("many_of"):
            lo, hi = MANY_OF[m.group("many_of").lower()]
            scale = SCALE_WORDS[m.group("of_scale").lower()[:-1]]
            yield m.group(0), (lo * scale, hi * scale), None, False
            continue
        if m.group("many"):
            yield m.group(0), MANY[m.group("many").lower()], None, False
            continue
        if m.group("vague"):
            lo, hi = next(v for k, v in VAGUE.items() if k in m.group("head").lower())
            scale = prod(SCALE_WORDS[w.rstrip("s")] for w in m.group("vague").lower().split())
            yield m.group(0), (lo * scale, hi * scale), None, False
            continue
        if m.group("frac"):  # a share, to two places: a third of is 0.33
            numer = 1 if m.group("numer").lower() == "a" else WORD_VALUES[m.group("numer").lower()]
            yield m.group(0), round(numer / FRACTIONS[m.group("frac").lower()], 2), 2, False
            continue
        if m.group("n"):  # one in five is the share 0.2, to two places
            yield m.group(0), round(_spelled_value(m.group("n")) / _spelled_value(m.group("m")), 2), 2, False
            continue
        if m.group("spelled_pct"):
            yield m.group(0), _spelled_value(m.group("spelled_pct")), 0, True
            continue
        if m.group("tens"):
            yield m.group(0), _spelled_value(m.group("tens")), None, False
            continue
        if m.group("dozen"):
            value = 12 * WORD_VALUES[" ".join(m.group("dozen").lower().split())]
        elif m.group("times"):
            value = WORD_VALUES[m.group("times").lower()]
        else:
            value = 2 if m.group("two") else 3 if m.group("three") else 0.5
        yield m.group(0), value, None if float(value).is_integer() else 1, False


def _cardinal_decimals(value, last):
    """The rounding of a spelled cardinal: its last scale word's, as 12k and 1m round, but never coarser than the
    value's own trailing zeros. None, exact, when it ends in no thousand, million or billion."""
    if last not in CARDINAL_DECIMALS:
        return None
    whole = str(int(round(value)))
    return max(CARDINAL_DECIMALS[last], len(whole.rstrip("0")) - len(whole))


def _spelled_value(phrase):
    """The value of a spelled number: a, half or half a, a fraction of a (a quarter of a, two thirds of a, a quarter),
    one to twenty, or a tens word and its unit, times each scale word, plus any tail after "and"."""
    head, _, tail = phrase.lower().partition(" and ")
    if tail:
        return _spelled_value(head) + _spelled_value(tail)
    words = re.findall(r"[a-z]+", phrase.lower())
    fraction = next((FRACTIONS[w.rstrip("s")] for w in words if w.rstrip("s") in FRACTIONS), None)
    if words[0] == "half":
        count = 0.5
    elif fraction:
        count = WORD_VALUES[words[0]] / fraction
    else:
        count = sum(WORD_VALUES[w] for w in words if w not in SCALE_WORDS)
    return count * prod(SCALE_WORDS[w] for w in words if w in SCALE_WORDS)


def _shows(entry_value, value, decimals, percent):
    if not isinstance(entry_value, (int, float)) or isinstance(entry_value, bool):
        return False
    if isinstance(value, tuple):  # a range such as dozens or thousands of; UNMATCHED is empty
        return float(entry_value).is_integer() and value[0] <= entry_value < value[1]
    if decimals is None:
        return float(entry_value).is_integer() and entry_value == value
    candidates = [entry_value, entry_value * 100] if percent else [entry_value]
    return any(abs(round(c, decimals) - value) < 1e-9 for c in candidates)


# K3 window and market

_SOURCE_MARKET_NAMES = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}


def _market_feed_span(text, market):
    name = _SOURCE_MARKET_NAMES.get(market)
    return re.search(rf"\bseen\s+in\s+{re.escape(name)}['’]s\s+feeds\b", str(text or ""), re.I) if name else None


def _k3(text, eids, stored, window, markets):
    """Every cited post inside the window and market; feed support without a located post must be scoped to feeds."""
    problems = []
    records = [(eid, stored[eid]) for eid in eids if eid in stored]
    if markets is None:
        markets = next(([r.get("market")] for _, r in records if _located(r)), [])
    places = _places(text)
    named = [m for m in markets if m in places]
    claim_market = markets[0] if len(markets) == 1 else named[0] if len(named) == 1 else None
    for eid, record in records:
        posted = _parse_time(record.get("posted_at"))
        if posted is None:
            problems.append(f"{eid} has no ISO posted_at with a UTC offset")
        else:
            day = posted.astimezone(SAST).date()
            if not window[0] <= day <= window[1]:
                problems.append(f"{eid} was posted {day.isoformat()}, outside {window[0].isoformat()} to {window[1].isoformat()}")
        if not _located(record):
            source = _by_source(record)
            name = _SOURCE_MARKET_NAMES.get(source, source)
            if source and claim_market and source != claim_market:
                problems.append(f"{eid} was seen in {name}'s feeds, not {claim_market}'s")
            elif source and markets and source not in markets:
                names = [_SOURCE_MARKET_NAMES.get(m, m) for m in markets]
                problems.append(f"{eid} was seen in {name}'s feeds, not in those of {', '.join(names)}")
            continue
        if claim_market and record.get("market") != claim_market:
            problems.append(f"{eid} is from {record.get('market')}, not {claim_market}")
        elif markets and record.get("market") not in markets:
            problems.append(f"{eid} is from {record.get('market')}, not one of {', '.join(markets)}")
    place_problems, leaned = _place_support(places, records, markets or ("ZA", "NG", "KE"))
    problems += place_problems + _source_scope_problems(text, records) + _nationality_problems(text, records)
    return problems, leaned


def _by_source(record):
    """The source market of a post not located by its own evidence."""
    return None if _located(record) else record.get("source_market") or None


def _source_scope_problems(text, records):
    text = str(text or "")
    located = {r.get("market") for _, r in records if _located(r)}
    source_only = {_by_source(r) for _, r in records} - {None} - located
    problems = []
    for source in sorted(source_only):
        name = _SOURCE_MARKET_NAMES.get(source, source)
        span = _market_feed_span(text, source)
        if not span:
            problems.append(f"support from {name}'s feeds without a located post must be worded as seen in {name}'s feeds")
            continue
        outside = f"{text[:span.start()]} {text[span.end():]}"
        if source in _places(outside):
            problems.append(f"mentions {name} outside the feed wording, so its feed posts cannot support that market claim")
    return problems


def _located(record) -> bool:
    """A post's market is confirmed when it is set and was not assumed from the question."""
    return bool(record.get("market")) and "market_assumed" not in {str(f).lower() for f in record.get("flags") or []}


def _places(text) -> dict:
    """The markets text names a place or people in, each as (words, people): the first people phrase found when there
    is one ("Kenyan creators", "Nigerians"), with people True, else the first place word; "" is a country outside ZA,
    NG and KE."""
    found, text = {}, _NOT_PLACES.sub(" ", str(text or ""))
    for mkt, pattern in PLACES.items():
        m = PEOPLE[mkt].search(text) or pattern.search(text)
        if m:
            found[mkt] = (" ".join(m.group(0).split()), m.re is PEOPLE[mkt])
    return found


def _place_problems(places, records, home) -> list[str]:
    return _place_support(places, records, home)[0]


def _country_people_candidates(text) -> list[tuple[str, str]]:
    return [(market, word) for market, (word, people) in _places(text).items() if not people]


def _country_people_problems(text, indexes, records, home) -> list[str]:
    candidates = _country_people_candidates(text)
    places = {candidates[index][0]: (candidates[index][1], True) for index in indexes}
    return _place_problems(places, records, home)


def _place_support(places, records, home) -> tuple[list[str], list[str]]:
    """One problem per named place no located cited post backs. A place in a home market needs a post located in that
    market or seen in its feeds; any other place needs a post located in or seen in a home market's feeds. People
    named by a demonym need a post located in their own market, wherever home is, so people from outside ZA, NG and KE
    are always cut. records is (id, record) pairs. Returns feed-supported place words for label lowering."""
    problems, leaned = [], []
    located = {r.get("market") for _, r in records if _located(r)}
    by_source = {_by_source(r) for _, r in records} - {None}
    unlocated = [str(eid) for eid, r in records if not _located(r)]
    tail = f"; {', '.join(unlocated)} {'has' if len(unlocated) == 1 else 'have'} no located market" if unlocated else ""
    for mkt, (word, people) in places.items():
        if mkt in located:
            continue
        if people and not mkt:
            problems.append(f"names {word}, people from outside ZA, NG and KE, and no cited post can be located "
                            "in their market")
        elif people and mkt in by_source:
            name = _SOURCE_MARKET_NAMES.get(mkt, mkt)
            problems.append(f"names {word}, people of {mkt}, but its posts are only seen in {name}'s feeds and none "
                            f"is located in {mkt}: word it as seen in {name}'s feeds, never as what people there are "
                            "or think")
        elif people:
            problems.append(f"names {word} but cites no post located in {mkt}, where those people are{tail}")
        elif mkt in by_source:
            leaned.append(word)
        elif mkt in home:
            problems.append(f"names {word} but cites no post located in {mkt}{tail}")
        elif by_source & set(home):
            leaned.append(word)
        elif not located & set(home):
            where = f"located in {' or '.join(sorted(home))}" if home else "with a located market"
            problems.append(f"names {word} but cites no post {where}{tail}")
    return problems, leaned


# K5 labels

def _max_label(claim, records, leaned=(), ctx=None):
    if claim.get("label") == "inferred" or claim.get("kind") in ("interpretation", "proposal"):
        return "inferred", "inferred claims, interpretations and proposals stay inferred"
    top, why = _evidence_label(claim, records)
    fed = [r for r in records if _by_source(r) and _by_source(r) == r.get("market")]
    if fed:
        counted = [dict(r, flags=[f for f in r.get("flags") or [] if str(f).lower() != "market_assumed"])
                   if r in fed else r for r in records]
        lifted, lifted_why = _evidence_label(claim, counted)
        step = next(label for label, rank in LABEL_RANK.items() if rank == max(LABEL_RANK[lifted] - 1, 0))
        if leaned or not any(_located(r) for r in records):
            top, why = step, (f"{lifted_why}; one step lower because its support is only posts seen in a market's "
                              "feeds, not located there")
        elif LABEL_RANK[step] > LABEL_RANK[top]:
            top, why = step, (f"{lifted_why}; one step lower because {len(fed)} of its posts are only seen in a "
                              "market's feeds, not located there")
    return _tone_cap(claim, records, top, why, ctx)


# Ask's evidence records carry only the market_assumed flag, so K5 also reads a post's own disclosure: an ad marker
# in its words, or the same words posted again under another handle. Neither changes the stored record.
_PAID_TEXT = re.compile(
    r"(?<![\w#])#(?:ad|ads|advert|advertisement|sponsored|sponsoredpost|paidpartnership|paidpartner|paidpromo|gifted|"
    r"prgifted)(?![\w])|\b(?:paid\s+(?:partnership|promotion)|sponsored\s+(?:by|post|content))\b", re.I)
_DUPLICATE_WORDS = 8  # short captions repeat by chance ("Rate my plate honestly"); a longer one does not


def _disclosed_paid(record) -> bool:
    return _PAID_TEXT.search(str(record.get("text") or "")) is not None


def _copied_ids(records) -> set:
    """Ids of posts whose words, once links, tags and handles are dropped, repeat an earlier post's: the first of
    each group stands as the author, the rest are copies."""
    seen, copies = {}, set()
    for record in sorted(records, key=lambda r: str(r.get("id"))):
        key = " ".join(_WORD.findall(_LINKS.sub(" ", str(record.get("text") or "")).lower()))
        if len(key.split()) < _DUPLICATE_WORDS:
            continue
        if key in seen:
            copies.add(record.get("id"))
        else:
            seen[key] = record.get("id")
    return copies


def _evidence_label(claim, records):
    copies = _copied_ids(records)
    independent = [r for r in records if not NOT_INDEPENDENT & {str(f).lower() for f in r.get("flags") or []}
                   and not _disclosed_paid(r) and r.get("id") not in copies]
    pairs = {(str(r.get("handle") or "").lower().lstrip("@"),
              PLATFORM_NAMES.get(str(r.get("platform") or "").lower(), str(r.get("platform") or "").lower()))
             for r in independent}  # x and twitter are one platform
    authors = {h for h, _ in pairs}
    platforms = {p for _, p in pairs}
    most_on_one = max((len({h for h, p in pairs if p == plat}) for plat in platforms), default=0)
    across = any(h1 != h2 and p1 != p2 for h1, p1 in pairs for h2, p2 in pairs)
    why = f"{len(authors)} independent author(s) on {len(platforms)} platform(s)"
    if across or (most_on_one >= 3 and claim.get("numbers")):
        top = "corroborated"
    elif len(authors) >= 2:
        top = "observed"
    else:
        top = "single_source"
    return top, why


def _tone_cap(claim, records, top, why, ctx=None):
    capped = tone_cap_ids(records, getattr(ctx, "native_languages", {}), getattr(ctx, "native_statuses", {}),
                          mostly_non_english, native_review_loaded=getattr(ctx, "native_review_loaded", False),
                          native_review_available=getattr(ctx, "native_review_available", False))
    if capped and is_tone_claim(claim.get("text")) and LABEL_RANK[top] > LABEL_RANK["single_source"]:
        return "single_source", (f"{why}; the native-language tone cap applies to non-English post(s) "
                                 f"{', '.join(map(str, capped))} without a cleared eligible review")
    return top, why


def is_tone_claim(text) -> bool:
    """A claim describes tone, sentiment or register when it uses one of TONE_WORDS."""
    return TONE.search(normalise(text)) is not None


def mostly_non_english(text) -> bool:
    """True when text holds one of LANGUAGE_MARKERS, or when under ENGLISH_SHARE of its words are COMMON_ENGLISH.
    Hashtags, handles and links are dropped first, and a capitalised word not on the list is a name and not counted.
    A text with no words left to count is not mostly non-English."""
    words = _WORD.findall(_LINKS.sub(" ", normalise(text)))
    if any(w.lower() in LANGUAGE_MARKERS for w in words):
        return True
    counted = [w for w in words if w.lower() in COMMON_ENGLISH or not w[0].isupper()]
    if not counted:
        return False
    return sum(w.lower() in COMMON_ENGLISH for w in counted) / len(counted) < ENGLISH_SHARE


# K6 breaches

def _text_breaches(text, records=None):
    """The kinds of breach in text. Never the matched words, so no reason or gap repeats them. A quoted span verbatim
    in one of records is left out of the age and demographic scan, since a post's own words are allowed as quotation;
    Google Trends is a breach even quoted. records=None scans every span."""
    text = str(text or "")
    lens = _blank_spans(normalise(text), _in_records(records)) if records else text
    hits = []
    if any(pattern.search(lens) for pattern in AGE_PATTERNS):
        hits.append("age or generation term")
    if DEMOGRAPHIC.search(lens):
        hits.append("demographic inference")
    if SEARCH_VOLUME.search(text):
        hits.append("Google Trends or search volume")
    return hits


def _k6(claim, records):
    hits = _text_breaches(claim.get("text"), records)
    for record in records:
        host = (urlparse(str(record.get("url") or "")).hostname or "").lower()
        generative_host = any(host == h or host.endswith("." + h) for h in GENERATIVE_HOSTS) or "nanobanana" in host
        flagged = any("generated" in str(f).lower() for f in record.get("flags") or [])
        if (record.get("platform") == "web" and generative_host) or flagged:
            hits.append(f"generated output {record.get('id')} cited as evidence")
    return hits


# K8 quoted spans

def _quoted_spans(text):
    """(span, end) for every double- or single-quoted span in normalised text."""
    spans = [(m.group(1), m.end()) for m in re.finditer(r'"([^"]+)"', text)]
    rest = re.sub(r'"[^"]+"', lambda m: " " * len(m.group(0)), text)
    spans += [(m.group(1), m.end()) for m in re.finditer(r"(?<!\w)'(.+?)'(?!\w)", rest)]
    return spans


def _blank_spans(text, skip):
    for span, end in _quoted_spans(text):
        if skip(span):
            start = end - len(span) - 2
            text = text[:start] + " " * (end - start) + text[end:]
    return text


def _in_records(records):
    """The skip test for _numerals: a span verbatim in one of records. Any other span is scanned for numerals."""
    return lambda span: any(_verbatim(span.strip(), r.get("text")) for r in records)


def _k8(text, records):
    problems = []
    for span, _ in _quoted_spans(normalise(text)):
        span = span.strip()
        if not span or any(_verbatim(span, r.get("text")) for r in records):
            continue
        shown = "a quoted span" if _text_breaches(span) else f"quoted span {span!r}"
        problems.append(f"{shown} is not verbatim in a cited record")
    return problems


# K10 and assembly

def _mentions(short_answer, cid, claim_text):
    if cid and re.search(r"(?<!\w)" + re.escape(cid) + r"(?!\w)", short_answer):
        return True
    head = WORDS.findall(normalise(short_answer).lower())
    body = WORDS.findall(normalise(claim_text).lower())
    grams = {tuple(body[i:i + 8]) for i in range(len(body) - 7)}
    return any(tuple(head[i:i + 8]) in grams for i in range(len(head) - 7))


def _code_gap(label, rule, searched):
    what, why = RULE_GAPS[rule]
    return {"what": f"{label}: {what} ({rule})", "searched": searched, "why": why}


def _searched(rule, claim, evidence, queries):
    """What a cut claim's check looked at. Only ids that resolve are named: evidence ids stored in evidence and, for
    K2, query ids recorded in queries. Any other id is model text, so it is counted and never written."""
    if rule == "K2":
        cited = [n.get("query_id") for n in claim.get("numbers") or [] if isinstance(n, dict) and "query_id" in n]
        label, known = "recorded queries", queries
    else:
        cited = claim.get("evidence_ids") or []
        label, known = "stored evidence", evidence
    resolves = [isinstance(c, str) and c in known for c in cited]
    named = list(dict.fromkeys(c for c, ok in zip(cited, resolves) if ok))
    unresolved = len({repr(c) for c, ok in zip(cited, resolves) if not ok})
    if rule == "K2":
        named.sort()
    if not unresolved:
        return f"{label} " + (", ".join(named) or "none cited")
    count = f"{unresolved} id that does not resolve" if unresolved == 1 else f"{unresolved} ids that do not resolve"
    return f"{label} {', '.join(named)} and {count}" if named else f"{label}: {count}"


def platform_label(platform: str | None) -> str:
    return PLATFORM_LABELS.get(platform or "", (platform or "source").capitalize())


def window_text(window: tuple[date, date]) -> str:
    start, end = window
    if start == end:
        return f"{end.day} {end:%B}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day} to {end.day} {end:%B}"
    return f"{start.day} {start:%B} to {end.day} {end:%B}"


# The run's steps that each make one SocialCrawl client call: a search, the two enrichment tools, and watch_video's
# screen text (its transcript call is logged as get_transcript).
SOURCE_STEPS = ("socialcrawl", "get_comments", "get_transcript", "watch_video")


def _enrichment_what(step, route, label, statuses):
    """What a failed get_comments, get_transcript or watch_video screen-text call missed, in plain words. An empty
    result is honest as none returned, never as no live data."""
    thing = f"{'an' if label[:1] in 'AEIOUX' else 'a'} {label} {'video' if label == 'YouTube' else 'post'}"
    if step == "get_transcript":
        subject, noun = f"Transcript of {thing}", "transcript"
    elif step == "watch_video":
        subject, noun = f"Screen text of {thing}", "screen text"
    elif route.endswith("/replies"):
        subject, noun = f"Replies to {thing}", "replies"
    else:
        subject, noun = f"Comments on {thing}", "comments"
    if statuses == ["empty"]:
        return f"No {noun} returned for {thing}"
    if statuses == ["partial"]:
        return f"{subject} came back partial"
    return f"{subject} not fetched"


def source_gaps(ctx, window) -> list[dict]:
    """One gap per SocialCrawl route, and per enrichment route of get_comments, get_transcript and watch_video, that
    did not return a full result. The only place source gaps are written."""
    failed = {}  # (step, route) -> its failed statuses, in the order first seen
    for event in ctx.events:
        if event.get("step") not in SOURCE_STEPS or event.get("status") == "ok":
            continue
        statuses = failed.setdefault((event.get("step"), str(event.get("route") or "")), [])
        if event.get("status") not in statuses:
            statuses.append(event.get("status"))
    gaps = []
    for (step, route), statuses in failed.items():
        label = platform_label(route.split("/", 1)[0])
        if statuses == ["cap_reached"]:  # a spent budget is not a failed source
            what = "Live search budget for today is spent"
        elif step != "socialcrawl":
            what = _enrichment_what(step, route, label, statuses)
        elif statuses == ["partial"]:
            what = f"Live {label} data is partial"
        else:
            what = f"No live {label} data"
        searched = route if step != "socialcrawl" else f"{route}, {window_text(window)}"
        gaps.append({"what": what, "searched": searched, "why": ", ".join(map(str, statuses))})
    return gaps


def _output_record(record):
    out = {k: record[k] for k in EVIDENCE_KEYS if record.get(k) is not None}
    if "engagement" in out:
        engagement = out["engagement"] if isinstance(out["engagement"], dict) else {}
        out["engagement"] = {k: v for k, v in engagement.items()
                             if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    if "flags" in out:
        out["flags"] = [f for f in out["flags"] if isinstance(f, str)] if isinstance(out["flags"], list) else []
    return out
