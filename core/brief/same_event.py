"""Whether two Today cards of one market and run describe the same event (core/brief/job.py _merge_events).

Pure Python. Each card brings its explanation, the why now sentence that passed the claim checks, and its member
posts, every post its card could rest on (brief.sql post_set plus the pack). Two cards are one event when all of
these hold:

- they share at least MIN_SHARED_POSTS member posts, a card's worth (MIN_EVIDENCE);
- their explanations do not name dates that all differ: when both name a date, they name one in common;
- their explanations share names: DATED_NAMES of them when both name a date in common, else UNDATED_NAMES.

A name is a run of capitalised words, such as Mamelodi Sundowns or Premier League, cut at punctuation. The word that
opens a sentence is a name only when the next word is capitalised too, and leading words such as "The" or "Local"
are dropped. Never a name: a platform, any of the three markets and their people, a month, weekday or number, and a
handle (a word with "_" in it, after "@", or joined to the next word by a dot). Words are compared after NFKD accent
folding, case folding and dropping a final possessive. Two names match when they are equal or one opens the other
(MTN8 and MTN8 Cup Final, never Manchester City and Manchester United), and each name counts once.

A date is a day and a month ("11 Oct", "10 October", "October 4", "2026-10-01"); the year is not read."""

import re
import unicodedata

from core.brief.specificity import MIN_EVIDENCE

MIN_SHARED_POSTS = MIN_EVIDENCE
DATED_NAMES = 2
UNDATED_NAMES = 3

PLATFORMS = {"tiktok", "twitter", "x", "instagram", "youtube", "facebook", "threads", "reddit", "nairaland",
             "boomplay", "spotify", "apple music", "audiomack", "whatsapp", "telegram", "snapchat"}
PEOPLES = {"south africa", "south african", "south africans", "nigeria", "nigerian", "nigerians", "kenya", "kenyan",
           "kenyans", "africa", "african", "africans", "mzansi", "naija"}
CALENDAR = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
            "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
            "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "sast"}
LEADING = {"a", "an", "the", "on", "in", "at", "after", "as", "across", "ahead", "during", "following", "since",
           "with", "this", "these", "their", "his", "her", "its", "multiple", "local", "creators", "several", "many",
           "some", "users", "fans", "posts", "both", "other", "while", "when", "and", "but", "or", "from", "for",
           "by", "to", "of", "it", "they"}

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r"\b")
_MONTH_DAY = re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})\b(?![-:/]\d)")
_ISO = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")
_WORD = re.compile(r"[^\W_][\w'’-]*")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _fold(word):
    word = "".join(ch for ch in unicodedata.normalize("NFKD", word) if not unicodedata.combining(ch)).casefold()
    return re.sub(r"['’]s$", "", word).strip("'’-")


def _capitalised(word):
    return word[0].isupper() or (any(ch.isdigit() for ch in word) and any(ch.isupper() for ch in word))


def _handle(sentence, start, end):
    word = sentence[start:end]
    return ("_" in word or sentence[start - 1:start] == "@" and start > 0
            or sentence[end:end + 1] == "." and sentence[end + 1:end + 2].isalnum()
            or start > 1 and sentence[start - 1] == "." and sentence[start - 2].isalnum())


def _name(run):
    words = list(run)
    while words and words[0] in LEADING:
        words.pop(0)
    words = [w for w in words if w not in CALENDAR and not w.isdigit()]
    name = " ".join(words)
    return None if not words or name in PLATFORMS or name in PEOPLES else tuple(words)


def names(text):
    """The names an explanation gives, each a tuple of folded words."""
    found = set()
    for sentence in _SENTENCE.split(text or ""):
        runs, prev_end = [], None
        for index, m in enumerate(_WORD.finditer(sentence)):
            word = m.group(0)
            if not _capitalised(word) or _handle(sentence, m.start(), m.end()):
                prev_end = None
                continue
            if prev_end is not None and not sentence[prev_end:m.start()].strip():
                runs[-1][1].append(_fold(word))
            else:
                runs.append((index, [_fold(word)]))
            prev_end = m.end()
        for index, run in runs:
            name = None if index == 0 and len(run) == 1 else _name(run)
            if name:
                found.add(name)
    return found


def dates(text):
    """The (month, day) pairs an explanation names."""
    text = text or ""
    out = {(MONTHS.index(m.group(2)[:3].lower()) + 1, int(m.group(1))) for m in _DAY_MONTH.finditer(text)}
    out |= {(MONTHS.index(m.group(1)[:3].lower()) + 1, int(m.group(2))) for m in _MONTH_DAY.finditer(text)}
    out |= {(int(m.group(1)), int(m.group(2))) for m in _ISO.finditer(text)}
    return {d for d in out if 1 <= d[0] <= 12 and 1 <= d[1] <= 31}


def _match(a, b):
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def shared_names(a, b):
    """How many names two sets share, each name counted once on each side."""
    left = sum(1 for x in a if any(_match(x, y) for y in b))
    right = sum(1 for y in b if any(_match(x, y) for x in a))
    return min(left, right)


def same_event(a, b):
    """a and b: {"explanation": str, "posts": set of member post ids}."""
    if len(set(a.get("posts") or ()) & set(b.get("posts") or ())) < MIN_SHARED_POSTS:
        return False
    da, db = dates(a.get("explanation")), dates(b.get("explanation"))
    if da and db and not da & db:
        return False
    need = DATED_NAMES if da & db else UNDATED_NAMES
    return shared_names(names(a.get("explanation")), names(b.get("explanation"))) >= need
