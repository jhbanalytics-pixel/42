"""Whether two Today cards of one market and run describe the same event (core/brief/job.py _merge_events, which
records the answer in shadow and changes no card, RELEASE-E-PLAN item 8h).

Pure Python. Each card brings its explanation, the why now sentence that passed the claim checks, and its member
posts, every post its card could rest on (brief.sql post_set plus the pack). Two cards are one event only when all of
these hold:

- they share at least MIN_SHARED_POSTS member posts, a card's worth (MIN_EVIDENCE);
- both explanations name a date, and they name the same dates (the same dated cause);
- they name the same sides (SIDES: a women's, reserves, juniors or age group side, Banyana or Bafana) and the same
  stages (STAGES: a final, a semi-final, a leg), so a final and its semi, or a men's side and a women's side, are
  two events;
- their explanations share at least SHARED_NAMES names.

A name is a run of capitalised words, such as Mamelodi Sundowns or Premier League, cut at punctuation; a number
right after a capitalised word stays in the name (Stage 2). The word that opens a sentence is a name only when the
next word is capitalised too, and leading words such as "The" or "Local" are dropped. Never a name: a place of the
gazetteer (core/detect/geo.py PLACES and COUNTRIES, the whole name), a name with a platform word or any of the three
markets and their people in it, calendar words, and a handle (a word with "_" in it, after "@", or joined to another
word by a dot). Words are compared after NFKD accent folding, case folding and dropping a final possessive.

Two names match when they are equal; when they have the same words but one shortens a word of the other to 3 or more
of its first letters, the last word equal (Man City and Manchester City); or when one is a single word with a digit or
in capitals (MTN8, AFCON) that opens the other and the other adds no side word. Each name counts once.

A date is a day and a month ("11 Oct", "10 October", "October 4", "2026-10-01"); the year is not read."""

import re
import unicodedata
from datetime import date

from core.brief.specificity import MIN_EVIDENCE
from core.detect.geo import COUNTRIES, PLACES

MIN_SHARED_POSTS = MIN_EVIDENCE
SHARED_NAMES = 3
SHORTENED_MIN = 3

PLATFORMS = {"tiktok", "twitter", "x", "instagram", "youtube", "facebook", "threads", "reddit", "nairaland",
             "boomplay", "spotify", "apple", "audiomack", "whatsapp", "telegram", "snapchat"}
PEOPLES = {"south", "africa", "african", "africans", "nigeria", "nigerian", "nigerians", "kenya", "kenyan", "kenyans",
           "mzansi", "naija", "sa", "rsa", "za", "ng", "ke", "kot"}
CALENDAR = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
            "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
            "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "sast"}
LEADING = {"a", "an", "the", "on", "in", "at", "after", "as", "across", "ahead", "during", "following", "since",
           "with", "this", "these", "their", "his", "her", "its", "multiple", "local", "creators", "several", "many",
           "some", "users", "fans", "posts", "both", "other", "while", "when", "and", "but", "or", "from", "for",
           "by", "to", "of", "it", "they"}
SIDES = (("women", r"women'?s?|woman|ladies|girls|banyana"), ("reserves", r"reserves?"), ("juniors", r"juniors?|youth"),
         ("bafana", r"bafana"))
_AGE = re.compile(r"\b(?:u|under)[- ]?(\d{2})\b", re.I)
STAGES = (("semi-final", r"semi[- ]?finals?"), ("quarter-final", r"quarter[- ]?finals?"),
          ("first leg", r"first leg"), ("second leg", r"second leg"), ("play-off", r"play[- ]?offs?"),
          ("final", r"finale?s?"))
SIDE_WORDS = {"women", "woman", "ladies", "girls", "banyana", "reserve", "reserves", "junior", "juniors", "youth",
              "bafana"}

_PLACES = {" ".join(n.casefold().split()) for names_ in (*PLACES.values(), *COUNTRIES.values()) for n in names_}
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = (r"(Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|"
          r"October|Nov|November|Dec|December)")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r"\b")
_MONTH_DAY = re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})\b(?![-:/]\d)")
_ISO = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")
_WORD = re.compile(r"[^\W_][\w'’-]*")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class _Word(str):
    """A folded word that remembers whether it was written in capitals (AFCON), which folding loses."""

    capitals = False


def _fold(word):
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", word) if not unicodedata.combining(ch)).casefold()
    out = _Word(re.sub(r"['’]s$", "", folded).strip("'’-"))
    out.capitals = len(word) > 1 and word.isupper()
    return out


def _capitalised(word):
    return word[0].isupper() or (any(ch.isdigit() for ch in word) and any(ch.isupper() for ch in word))


def _handle(sentence, start, end):
    return ("_" in sentence[start:end] or start > 0 and sentence[start - 1] == "@"
            or sentence[end:end + 1] == "." and sentence[end + 1:end + 2].isalnum()
            or start > 1 and sentence[start - 1] == "." and sentence[start - 2].isalnum())


def _name(run):
    words = list(run)
    while words and words[0] in LEADING:
        words.pop(0)
    if any(w in CALENDAR for w in words):
        words = [w for w in words if w not in CALENDAR and not w.isdigit()]
    if not words or all(w.isdigit() for w in words) or " ".join(words) in _PLACES:
        return None
    if any(w in PLATFORMS or w in PEOPLES for w in words):
        return None
    return tuple(words)


def names(text):
    """The names an explanation gives, each a tuple of folded words."""
    found = set()
    for sentence in _SENTENCE.split(text or ""):
        runs, prev_end = [], None
        for index, m in enumerate(_WORD.finditer(sentence)):
            word = m.group(0)
            joined = prev_end is not None and not sentence[prev_end:m.start()].strip()
            if _handle(sentence, m.start(), m.end()) or not (_capitalised(word) or word.isdigit() and joined):
                prev_end = None
                continue
            if joined:
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


def sides(text):
    """The side qualifiers an explanation names: women, reserves, juniors, bafana and age groups such as u20."""
    text = text or ""
    found = {label for label, pattern in SIDES if re.search(r"\b(?:" + pattern + r")\b", text, re.I)}
    return found | {f"u{m.group(1)}" for m in _AGE.finditer(text)}


def stages(text):
    """The competition stages an explanation names; a semi-final or quarter-final is not also read as a final."""
    text, found = text or "", set()
    for label, pattern in STAGES:
        rx = re.compile(r"\b(?:" + pattern + r")\b", re.I)
        if rx.search(text):
            found.add(label)
            text = rx.sub(" ", text)
    return found


def _match(a, b):
    if a == b:
        return True
    if len(a) == len(b) and a[-1] == b[-1] and all(
            x == y or min(len(x), len(y)) >= SHORTENED_MIN and (x.startswith(y) or y.startswith(x))
            for x, y in zip(a, b)):
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return (len(short) == 1 and long_[0] == short[0] and _marked(short[0])
            and not any(w in SIDE_WORDS for w in long_[1:]))


def _marked(word):
    return any(ch.isdigit() for ch in word) or getattr(word, "capitals", False)


def shared_names(a, b):
    """How many names two sets share, each name counted once on each side."""
    matches = {}

    def assign(x, seen):
        for y in b:
            if y in seen or not _match(x, y):
                continue
            seen.add(y)
            if y not in matches or assign(matches[y], seen):
                matches[y] = x
                return True
        return False

    return sum(assign(x, set()) for x in a)


def _all_but_dates(a, b):
    """{"shared_posts", "shared_names"} when a and b pass every check but the dates, else None."""
    shared_posts = len(set(a.get("posts") or ()) & set(b.get("posts") or ()))
    if shared_posts < MIN_SHARED_POSTS:
        return None
    ea, eb = a.get("explanation"), b.get("explanation")
    if sides(ea) != sides(eb) or stages(ea) != stages(eb):
        return None
    n = shared_names(names(ea), names(eb))
    if n < SHARED_NAMES:
        return None
    return {"shared_posts": shared_posts, "shared_names": n}


def reason(a, b):
    """Why a and b ({"explanation": str, "posts": set of member post ids}) are one event, or None when they are not:
    {"shared_posts", "shared_names", "date"}, the date as MM-DD. No name or post text is returned."""
    da = dates(a.get("explanation"))
    if not da or da != dates(b.get("explanation")):
        return None
    passed = _all_but_dates(a, b)
    return None if passed is None else {**passed, "date": [f"{m:02d}-{d:02d}" for m, d in sorted(da)]}


def near(a, b):
    """A near pair: a and b pass every check but the dates (one or neither names a date, or both do and the dates
    differ). {"shared_posts", "shared_names", "dated"}, dated being how many of the two explanations name a date;
    None for a pair that is one event or fails another check. The shadow records these so a case such as the four
    MTN8 cards of 12 October 2026, of which only one names its date, shows in the data."""
    da, db = dates(a.get("explanation")), dates(b.get("explanation"))
    if da and da == db:
        return None
    passed = _all_but_dates(a, b)
    return None if passed is None else {**passed, "dated": bool(da) + bool(db)}


def same_event(a, b):
    return reason(a, b) is not None


def fixture_anchor(text, market, brief_date, *, checked=False, allow_undated=False):
    """A checked, explicitly dated football fixture; unsupported event wording stays unresolved."""
    if checked is not True or market not in {"ZA", "NG", "KE"} or not isinstance(text, str):
        return None
    parts = re.split(r"\blikely because\b", text, flags=re.I)
    if len(parts) != 2:
        return None
    lead, cause = parts
    competitions = [c for c in ("Premier League", "MTN8") if re.search(r"\b" + c + r"\b", text, re.I)]
    if len(competitions) != 1 or re.search(
            r"\b(?:boycott\w*|cancel\w*|postpon\w*|abandon\w*|protest\w*|transfer\w*|sign\w*|"
            r"injur\w*|train\w*|announc\w*|arrest\w*|detain\w*|sack\w*|fire\w*|died|death|"
            r"not|never|would|will|could|may|might)\b", cause, re.I):
        return None
    competition = competitions[0]
    if len(dates(cause)) > 1:
        return None
    proper = r"[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,4}"
    day_text = r"(?:\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"|" + _MONTH + r"\s+\d{1,2})(?:,?\s+20\d{2})?"
    modifier = (r"(?:\s+(?:in\s+(?:the\s+)?" + competition +
                r"(?:\s+(?:football\s+)?(?:match|fixture|final|semi-final|quarter-final))?"
                r"|at\s+(?:Anfield|Wembley(?: Stadium)?|Moses Mabhida Stadium)))*")
    event_phrases = (
        proper + r"\s+(?:beat|defeated|played(?: against)?|faced|met|drew with)\s+(?:rivals?\s+)?" + proper,
        r"(?:match|fixture|final)\s+(?:between|involving)\s+" + proper + r"\s+(?:and|against|vs\.?|versus)\s+" + proper +
            r"\s+(?:took place|was played|was held|occurred|happened)",
        proper + r"\s+claimed\s+(?:the\s+)?" + competition + r"\s+(?:football\s+)?final\s+(?:over|against)\s+" + proper,
        competition + r"\s+(?:Cup\s+)?(?:football\s+)?(?:[Ff]inal|[Ff]inale)\s+(?:took place|was played|was held)",
    )
    # Bind the date to explicit fixture syntax, never to another clause's discussion, season or posting time.
    timed = [m.group("event_date") for phrase in event_phrases
             for m in re.finditer(r"\b" + phrase + modifier +
                 r"\s+on\s+(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+)?"
                 r"(?P<event_date>" + day_text + r")\b", cause)]
    dated = set().union(*(dates(t) for t in timed)) if timed else set()
    if re.search(r"\b(?:fixture|match|final)\s+(?:took place|was played|was held)\b", lead, re.I):
        return None
    if len(dated) > 1 or (not dated and not allow_undated):
        return None
    years = {int(y) for timed_text in timed for y in re.findall(r"\b(20\d{2})\b", timed_text)}
    if len(years) > 1:
        return None
    try:
        reference = date.fromisoformat(str(brief_date))
        month, day = next(iter(dated)) if dated else (None, None)
        event_date = date(next(iter(years), reference.year), month, day).isoformat() if dated else None
    except (ValueError, TypeError):
        return None
    qualifiers = sides(text)
    stage = stages(text)
    # Unknown extra names, including venue names not in the gazetteer, refuse an anchor.
    actor_context = text
    without_competition = re.sub(r"\b" + competition + r"\b(?:\s+Cup\s+Final)?", " ", actor_context, flags=re.I)
    without_competition = re.sub(r"\b(?:[A-Z][a-z]+\s+){1,4}Stadium\b", " ", without_competition)
    explicit = []
    action_matches = 0
    for index, pattern in enumerate((
            r"(?P<a>" + proper + r")\s+(?:beat|defeated|played(?: against)?|faced|met|drew with)\s+(?:rivals?\s+)?(?P<b>" + proper + r")",
            r"\b(?:match|fixture|final)\s+(?:between|involving)\s+(?:clubs? including\s+)?(?P<a>" + proper + r")\s+(?:and|against|vs\.?|versus)\s+(?P<b>" + proper + r")")):
        for match in re.finditer(pattern, text):
            if index == 0:
                action_matches += 1
            if re.match(r"\s+(?:and|against|vs\.?|versus)\s+" + proper, text[match.end():]):
                return None
            explicit.append(tuple(sorted((str(_fold(match.group("a"))), str(_fold(match.group("b")))))))
    if action_matches > 1 or len(set(explicit)) > 1 or re.search(
            r"\b(?:reverse fixture|reverse match|rematch|return fixture|twice|another match|another fixture|both legs)\b",
            cause, re.I):
        return None
    actors = ({tuple(a.split()) for a in explicit[0]} if explicit else names("Event: " + without_competition))
    actors = {a for a in actors if a not in {("final",), ("finale",), ("cup", "final")}}
    if len(actors) != 2:
        return None
    folded = str(_fold(cause))
    actor_words = sorted(" ".join(a) for a in actors)
    winner = None
    relation = False
    for a, b in (actor_words, actor_words[::-1]):
        pa, pb = re.escape(a), re.escape(b)
        result = re.search(r"\b" + pa + r"\s+(beat|defeated)\s+(?:rivals?\s+)?" + pb + r"\b", folded)
        played = re.search(r"\b" + pa + r"\s+(?:played(?: against)?|faced|met|drew with)\s+" + pb + r"\b", folded)
        fixture = re.search(r"\b(?:match|fixture|final)\s+(?:between|involving)\s+" + pa +
                            r"\s+(?:and|against|vs\.?|versus)\s+" + pb + r"\b", folded)
        claimed = re.search(r"\b" + pa + r"\s+claimed\s+(?:the\s+)?" + re.escape(competition.casefold()) +
                            r"\s+(?:football\s+)?final\s+(?:over|against)\s+(?:rivals including\s+)?" + pb + r"\b", folded)
        if result or claimed:
            winner = a
        relation |= bool(result or played or fixture or claimed)
    if not relation:
        neutral_final = re.match(r"\s*(?:of\s+)?(?:the\s+)?" + competition +
                                 r"\s+(?:Cup\s+)?(?:football\s+)?(?:final|finale)\s+"
                                 r"(?:at|on|took place|takes place)\b", cause, re.I)
        if not (explicit or neutral_final):
            return None
    venues = sorted({str(_fold(m.group(1))) for m in re.finditer(r"\bat\s+(" + proper + r")", cause)})
    return {"kind": "sports_fixture", "market": market, "date": event_date, "competition": competition,
            "actors": actor_words, "side": sorted(qualifiers), "stage": sorted(stage), "winner": winner,
            "venues": venues}


def fixture_reason(a, b, *, allow_undated=False):
    """Only code-produced fixture anchors can authorize the bounded publication path."""
    keys = {"kind", "market", "date", "competition", "actors", "side", "stage", "winner", "venues"}
    for value in (a, b):
        if not isinstance(value, dict) or set(value) != keys or value.get("kind") != "sports_fixture":
            return None
        if (value.get("market") not in {"ZA", "NG", "KE"} or value.get("competition") not in {"Premier League", "MTN8"}
                or not isinstance(value.get("actors"), list) or len(value["actors"]) != 2
                or not all(isinstance(x, str) and x for x in value["actors"])
                or not isinstance(value.get("side"), list) or not isinstance(value.get("stage"), list)
                or not isinstance(value.get("venues"), list)
                or not all(isinstance(x, str) for k in ("side", "stage", "venues") for x in value[k])
                or value.get("winner") is not None and not isinstance(value["winner"], str)
                or len(set(value["actors"])) != 2):
            return None
        if value["date"] is not None or not allow_undated:
            try:
                date.fromisoformat(value["date"])
            except (ValueError, TypeError):
                return None
    if any(a[k] != b[k] for k in ("market", "competition", "side", "stage")):
        return None
    if a["date"] != b["date"] and (not allow_undated or a["date"] and b["date"]):
        return None
    if a["venues"] and b["venues"] and a["venues"] != b["venues"]:
        return None
    if shared_names({tuple(x.split()) for x in a["actors"]}, {tuple(x.split()) for x in b["actors"]}) != 2:
        return None
    if a["winner"] and b["winner"] and not _match(tuple(a["winner"].split()), tuple(b["winner"].split())):
        return None
    return {"kind": "sports_fixture", "date": a["date"], "competition": a["competition"]}
