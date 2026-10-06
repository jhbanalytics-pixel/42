"""Claim checks for Ask answers and morning explanations (TRUST.md section 2, Stage 1A rules).

Public API, the only names other lanes should import:

    check_answer(answer, *, window_start, window_end, market=None, rerun=None) -> (new_answer, checks)
    named_markets(text) -> the markets text names by the place words below: "ZA", "NG", "KE" first, then
        the neighbour codes of core/detect/geo.py
    located_market(record) -> the market a record is located in, or None when its location is unknown
    source_market(record) -> an explicit known source market, upper case, or None
    place_support(text, records) -> each named market's support: "located", "source" or None
    place_fault(text, records, exempt=None) -> the K3 place fault, or None
    inferred_only_by_source_step(claim, records) -> True when an observation is labelled inferred and K5 allows it
        no higher only because a named place has source-market support: its authors alone allow single_source
    allowed_label(claim, records) -> the highest label K5 allows the claim on these records
    MARKET_NAMES: {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}

answer: the answer dict of core/eval/rubric.md section 1. It is never mutated.
window_start, window_end: datetimes or ISO 8601 datetime strings, both ends inclusive. Pass aware
    values at the market's local day start and day end; a date-only value raises ValueError, and
    a datetime without a timezone is read as UTC.
market: "ZA", "NG" or "KE" when the question is about one market, else None.
rerun: optional callable taking one numbers[] entry and returning the value its query gives
    now. When given, every numbers[] entry is re-run; an exception or a non-numeric result cuts.

new_answer is a deep copy with cut claims removed, labels downgraded and status adjusted. A
short_answer that breaches K6 or carries an unpinned numeral is blanked and makes a complete
answer partial; a context that does either is blanked with status unchanged; so_what and
watch_next items are dropped when
they breach K6, carry an unpinned numeral or rest on a cut or missing claim; gaps items are
dropped when their what or why breaches K6.

checks is a list of intelligence_42_agent.claim_checks rows:
    {"claim_id": str or None, "rule": "K1".."K10", "verdict": "pass" | "cut" | "downgrade" | "breach",
     "checker": "code", "detail": str}
Every claim gets one row for each of K1, K2, K3, K5, K6 and K8; a claim without an id is logged
as "claims[<index>]". Rows about answer-level fields (short_answer, context, so_what, watch_next,
gaps) and the K10 status row carry claim_id None.

Quotation marks mean a quote. Words inside straight or curly double quotes, or curly single
quotes, are exempt from K2 and K6 only when they equal a quote that is verbatim in its record:
for a claim, one of its own quotes; for answer-level fields, a quote of a surviving claim. For
K6 the span must also keep 3 or more words once banned terms are taken out, so a quote that is
only "Gen Z" is read as prose. A quoted span in a claim that matches none of its quotes[]
entries is a K1 cut.

Rules:
    K1  a unique claim id; evidence ids present and resolved; each quote at least 2 words and
        verbatim in a record the claim cites, starting and ending on word boundaries (whitespace,
        NFKC and straight versus curly quote marks may differ). Else cut.
    K2  every numeral matches a numbers[] entry pinned by query_id, run_id and result_hash (for a
        claim, its own entries; for answer-level fields, entries of surviving claims); re-runs
        match exactly for counts and within 2% for ratios and shares. Else cut. Not figures:
        verified quotes, dates, clock times, Covid-19, "last, past or previous N days, weeks or
        hours" when N equals the window length in that unit, rounded, and a 19xx or 20xx year.
        A year is no later than window_end's year plus one, is not followed by % or a counted
        noun, and is preceded by the, a, 's, in, since, of or a sentence start, or followed by
        a capitalised word or wave, season, edition, election(s), festival, moment, calendar or
        cycle. Any other 19xx or 20xx needs a pinned number.
    K3  every cited record was posted inside the window and, when market is given, does not state
        another actual market (a market_assumed flag never hides one). Every named place needs located
        support or explicit source-market support stated only as feed scope. A source-only feed mention
        cannot support a residual place or people claim. Else cut.
    K5  the label is lowered to the highest the evidence allows, one step further when a named place
        has source-market support only. Records flagged brand, brand_owned, paid, sponsored,
        near_duplicate or generated are not independent authors; market_assumed alone is not an
        author-independence exclusion.
    K6  age or generation terms, demographic inference, Google Trends or a generated record as
        evidence are a breach. Checked in claim text, quote translations, a proposal's basis and
        falsifier, and every answer-level field.
    K8  a quote with a translation must carry translation_marked true. Else cut.
    K10 a cut makes a complete answer partial; under 2 surviving claims makes it insufficient_evidence.
"""

import copy
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone
from itertools import combinations

from core.detect.geo import COUNTRIES, MARKETS, PLACES, _fold, _pattern

LABELS = ("inferred", "single_source", "observed", "corroborated")
NOT_INDEPENDENT = {
    "brand",
    "brand_owned",
    "paid",
    "sponsored",
    "near_duplicate",
    "generated",
    "ai_generated",
}
GENERATED_FLAGS = {"generated", "ai_generated"}
GENERATED_PLATFORMS = {"gemini", "nano_banana"}
MARKET_NAMES = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}
# Place words are the gazetteer of core/detect/geo.py (PLACES and COUNTRIES, for the three markets and their
# neighbours) plus people and nicknames it leaves out because they never place a post. Folded like geo.py
# folds, matched as whole words, as a camel-case part ("#CapeTownVibes") and as the leading or trailing part
# of a hashtag ("#kenyatwitter"). Codes and acronyms count only in capitals.
PLACE_EXTRA = {
    "ZA": ("capetonian", "capetonians", "joburger", "joburgers", "durbanite", "durbanites", "sowetan",
           "sowetans", "saffa", "saffas"),
    "NG": ("lagosian", "lagosians"),
    "KE": ("nairobian", "nairobians"),
}
PLACE_CODES = {"ZA": ("ZA", "SA", "RSA"), "NG": ("NG",), "KE": ("KE", "KOT")}
# A hashtag part shorter than this matches only as a whole hashtag or a camel-case part.
HASHTAG_PART_MIN = 4
RATIO_WORDS = {
    "times",
    "ratio",
    "ratios",
    "share",
    "shares",
    "percent",
    "percentage",
    "%",
    "rate",
    "x",
    "proportion",
    "fraction",
    "multiple",
}

_QUOTE_MARKS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
    }
)
_QUOTED = re.compile(r'"([^"]*)"|\u201c([^\u201c\u201d]*)\u201d|\u2018(.*?)\u2019(?!\w)')
_DATE_ONLY = re.compile(r"\s*\d{4}-\d{2}-\d{2}\s*")
_PLACE_NAMES = {
    code: PLACES.get(code, ()) + COUNTRIES.get(code, ()) + PLACE_EXTRA.get(code, ())
    for code in dict.fromkeys((*MARKET_NAMES, *PLACES, *COUNTRIES))
}
_PLACE_RE = {code: _pattern(names) for code, names in _PLACE_NAMES.items()}
_PLACE_CODE_RE = {
    code: re.compile(r"(?<![^\W_])(?:" + "|".join(codes) + r")(?![^\W_])") for code, codes in PLACE_CODES.items()
}
_TAG_PARTS = {
    code: tuple(n.replace(" ", "") for n in names if len(n.replace(" ", "")) >= HASHTAG_PART_MIN)
    for code, names in _PLACE_NAMES.items()
}
_HASHTAG = re.compile(r"#(\w+)")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# A possessive 's goes before folding, which drops apostrophes and would read "Kenya's" as "kenyas".
_POSSESSIVE = re.compile(r"['\u2019\u02bc]s(?![^\W_])")


def _name_alternatives(names):
    return "|".join(
        sorted((r"[\s_-]*".join(map(re.escape, name.split())) for name in names), key=len, reverse=True)
    )


_FEED_NOUN = (
    r"(?:(?:tiktok|youtube|reddit|x|facebook|instagram)\s+)?(?:local\s+)?"
    r"(?:feeds?|trending(?:\s+(?:lists?|boards?|feeds?|pages?))?|boards?)"
)
_DIRECT_FEED_NOUN = r"(?:(?:tiktok|youtube|reddit|x|facebook|instagram)\s+)?(?:local\s+)?(?:feeds?|boards?)"
_FEED_SCOPE = {}
for _code in MARKETS:
    _country_names = COUNTRIES.get(_code, ())
    _plural_demonyms = {
        name for name in _country_names if name.endswith("s") and name[:-1] in _country_names
    }
    _feed_names = [name for name in _country_names if name not in _plural_demonyms]
    _feed_alternatives = _name_alternatives(_feed_names)
    _FEED_SCOPE[_code] = re.compile(
        r"(?<![^\W_])(?:(?:in|on|across|from)\s+(?:the\s+)?(?:"
        + _feed_alternatives
        + r")\s+"
        + _FEED_NOUN
        + r"|(?:"
        + _feed_alternatives
        + r")\s+"
        + _DIRECT_FEED_NOUN
        + r")(?![^\W_])",
        re.IGNORECASE,
    )

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b\.?"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_YEAR = r"(?:,?\s+(?:19|20)\d{2}\b)?"

# A hyphen range followed by one of these plural nouns counts things, not ages. Plural only,
# because the singular forms double as verbs ("people 18-24 share it").
_COUNTED = (
    r"(?:percent|posts|creators|days|videos|accounts|credits|times|comments|likes|views|shares"
    r"|replies|reposts|hashtags|sounds|songs|markets|hours|weeks)\b"
)
_PERSON = (
    r"(?:people|persons?|adults?|audiences?|crowds?|demographics?|generations?|segments?|women|men"
    r"|consumers?|viewers?|users?|creators?|posters?|fans?|followers?|listeners?|voters?|shoppers?"
    r"|professionals?|ones|south\s+africans?|nigerians?|kenyans?)"
)
# A 19xx or 20xx is a year only when it is no later than window_end's year plus one, is not
# followed by %, a counted noun, a hyphenated word or a plural-looking word other than the
# listed exceptions, and is preceded by the, a, 's, in, since, of or a sentence start, or
# followed by a capitalised word or one of the calendar nouns below.
_NOT_A_YEAR = re.compile(
    rf"(?!\s*%)(?!\s*{_COUNTED})(?!\s*{_PERSON}\b)(?!\s*(?:subscribers|members|votes|streams"
    r"|plays|downloads|entries|tweets|clips|tickets|units|rand|naira|shillings|dollars)\b)"
    r"(?!-\w)(?!\s+(?!(?:is|was|has|as|its|this|elections|moments|festivals|seasons|editions"
    r"|cycles)\b)[A-Za-z]+s\b)",
    re.I,
)
_YEAR_TOKEN = re.compile(r"(?<![\d.,])\b(?:19|20)\d{2}\b(?![.,]\d)")
_YEAR_BEFORE = re.compile(r"(?:^\s*|[.!?]\s+|\b(?:the|a|in|since|of)\s+|['’]s\s+)$", re.I)
_YEAR_AFTER = re.compile(
    r"\s+(?:[A-Z]|(?i:wave|season|edition|elections?|festival|moment|calendar|cycle)\b)"
)
_WINDOW_PHRASE = re.compile(r"(?i)\b(?:last|past|previous)\s+(\d+)\s+(hour|day|week)s?\b")

# Spans whose numerals are not figures: dates, years, clock times, Covid-19.
_NOT_FIGURES = [
    re.compile(p)
    for p in (
        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?",
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        rf"\b{_DAY}\s*(?:-|\u2013|to|and|until|till)\s*{_DAY}\s+(?:of\s+)?{_MONTH}{_YEAR}",
        rf"\b{_DAY}\s+(?:of\s+)?{_MONTH}{_YEAR}",
        rf"\b{_MONTH}\s+{_DAY}\b{_YEAR}",
        rf"\b{_MONTH}\s+(?:19|20)\d{{2}}\b",
        r"\b(?:19|20)\d0s\b",
        r"\b\d{1,2}:\d{2}(?::\d{2})?\b",
        r"\b\d{1,2}h\d{2}\b",
        r"(?i)\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)(?!\w)",
        r"(?i)\bcovid[\s-]?19\b",
    )
]

_NUMERAL = re.compile(
    r"(?<![\w.#@/])(\d{1,3}(?:[,\u00a0\u202f ]\d{3})+|\d+)(\.\d+)?(?:st|nd|rd|th)?"
    r"(?:\s?(%|percent|k|m|bn|thousand|million|billion|x)(?![a-z]))?(?![\w.]\d)",
    re.I,
)
_MULTIPLIER = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}

_BREACH_TERMS = [
    re.compile(p, re.I)
    for p in (
        r"(?<!next-)\bgen[\s-]?x(?:s|ers?)?\b",
        r"\bgen(?:eration)?[\s-]?(?:z|alpha|y)(?:s|ers?)?\b",
        r"\bgeneration[\s-]?x(?:s|ers?)?\b",
        r"\bgenz\b",
        r"\bgen[\s-]?zers?\b",
        r"\bzoomers?\b",
        r"\bmillennials?\b",
        r"\bboomers?\b",
        r"\byouths?\b(?!\s+(?:day|month)\b)",
        r"\bteens?\b",
        r"\bteenagers?\b",
        r"\bteenage\b",
        rf"\b(?:young|younger|youngest)\s+{_PERSON}\b",
        r"\byoungsters?\b",
        r"\badolescents?\b",
        r"\bkids?\b",
        r"\bchild(?:ren)?\b(?!'?s?\s+day\b)",
        r"\bschool[\s-]going\b",
        r"\blearners?\b(?!'?s?\s+(?:licen[cs]es?|drivers?|permits?)\b)",
        r"\bpensioners?\b",
        r"\belderly\b",
        r"\bseniors\b",
        r"\bsenior\s+citizens?\b",
        r"\bage[ds]?\s+\d{1,2}\b",
        rf"(?<![\d:/.\-])\b(?:1[3-9]|[2-9]\d)\s*(?:-|\u2013)\s*(?:1[4-9]|[2-9]\d)\b(?!\s*[%\d])(?!\s+{_MONTH})"
        rf"(?!\s*{_COUNTED})",
        r"\b\d{1,2}\s*(?:-|\u2013|to)\s*\d{1,2}[\s-]*(?:years?|yrs?|yo)\b",
        r"\b\d{1,2}[\s-]*(?:years?[\s-]*olds?|yo)\b",
        r"\b(?:under|over)[\s-]?\d{2}s\b",
        r"\b(?:twenty|thirty|forty|fifty)somethings?\b",
        r"\btheir\s+(?:early\s+|late\s+|mid[\s-]?)?(?:\d0s|twenties|thirties|forties|fifties|sixties)\b",
        r"\bskew(?:s|ed|ing)?\s+(?:towards?\s+)?(?:young|younger|old|older|female|male|women|men|affluent"
        r"|wealthy|middle[\s-]class|working[\s-]class)\b",
        r"\b(?:likely|probably|presumably|mostly|predominantly|mainly|largely|overwhelmingly)\s+(?:female|male"
        r"|women|men|young|younger|older|affluent|wealthy|middle[\s-]class|working[\s-]class)\b",
        r"\b(?:female|male|women|men)[\s-]skew(?:ing|ed)?\b",
        r"\bgender\s+split\b",
        r"\blsm\s*\d",
        r"\bincome\s+(?:brackets?|groups?|bands?)\b",
        r"\bgoogle[\s_-]*trends?\b",
        r"\bsearch[\s-]volumes?\b",
        r"\bsearch\s+interest\b",
    )
]


def check_answer(answer, *, window_start, window_end, market=None, rerun=None):
    """Run K1, K2, K3, K5, K6, K8 and K10 on one answer. See the module docstring."""
    start, end = _window(window_start, "window_start"), _window(window_end, "window_end")
    seconds = (end - start).total_seconds()
    spans = {
        unit: round(seconds / size)
        for unit, size in (("hour", 3600), ("day", 86400), ("week", 604800))
    }
    spans["max_year"] = end.year + 1
    new = copy.deepcopy(answer)
    records = {r.get("id"): r for r in new.get("evidence") or []}
    checks = []

    def row(claim_id, rule, verdict, detail):
        checks.append(
            {
                "claim_id": claim_id,
                "rule": rule,
                "verdict": verdict,
                "checker": "code",
                "detail": detail,
            }
        )

    claims = new.get("claims") or []
    id_counts = Counter(c.get("id") for c in claims)
    cut, own_quotes = set(), []
    for i, c in enumerate(claims):
        cid = c.get("id")
        verified = _verified_quotes(c, records)
        own_quotes.append(verified)
        results = [
            ("K1", _k1(c, records, id_counts)),
            ("K2", _k2(c, rerun, verified, spans)),
            ("K3", _k3(c, records, start, end, market)),
            ("K6", _k6(c, records, verified)),
            ("K8", _k8(c)),
            ("K5", _k5(c, records)),
        ]
        for rule, (verdict, detail) in results:
            row(cid if cid else f"claims[{i}]", rule, verdict, detail)
            if verdict in ("cut", "breach"):
                cut.add(i)

    survivors = [c for i, c in enumerate(claims) if i not in cut]
    new["claims"] = survivors
    live_ids = {c.get("id") for c in survivors}
    live_quotes = set().union(*(own_quotes[i] for i in range(len(claims)) if i not in cut))
    live_numbers = [e for c in survivors for e in c.get("numbers") or [] if _pinned(e)]

    def field_fault(text):
        term = _breach_term(text, live_quotes)
        if term:
            return "K6", "breach", f"banned term {term!r}"
        raw = _unmatched_numeral(text, live_quotes, live_numbers, spans)
        if raw:
            return "K2", "cut", f"numeral {raw!r} has no pinned numbers entry on a surviving claim"
        return None

    short_fault = False
    for field in ("short_answer", "context"):
        fault = field_fault(new.get(field) or "")
        if fault:
            row(None, fault[0], fault[1], f"{field}: {fault[2]}")
            new[field] = ""
            short_fault = short_fault or field == "short_answer"

    for field in ("so_what", "watch_next"):
        kept = []
        for i, item in enumerate(new.get(field) or []):
            fault = field_fault(item.get("text") or "")
            if fault:
                row(None, fault[0], fault[1], f"{field}[{i}]: {fault[2]}")
                continue
            ids = item.get("claim_ids") or []
            if not ids or any(x not in live_ids for x in ids):
                row(None, "K10", "cut", f"{field}[{i}] rests on a cut or missing claim: {ids}")
                continue
            kept.append(item)
        new[field] = kept

    kept = []
    for i, gap in enumerate(new.get("gaps") or []):
        faults = [(k, _breach_term(gap.get(k) or "", live_quotes)) for k in ("what", "why")]
        faults = [(k, t) for k, t in faults if t]
        if faults:
            row(None, "K6", "breach", f"gaps[{i}].{faults[0][0]}: banned term {faults[0][1]!r}")
            continue
        kept.append(gap)
    if "gaps" in new:
        new["gaps"] = kept

    status = new.get("status")
    if status in ("complete", "partial"):
        if len(survivors) < 2:
            new["status"] = "insufficient_evidence"
        elif status == "complete" and (cut or short_fault):
            new["status"] = "partial"
    if new.get("status") != status:
        row(
            None,
            "K10",
            "downgrade",
            f"status {status} to {new['status']}, {len(survivors)} claims survive",
        )
    else:
        row(None, "K10", "pass", f"status {status}, {len(survivors)} claims survive")
    return new, checks


def _window(value, name):
    if not isinstance(value, datetime) and (
        isinstance(value, date) or (isinstance(value, str) and _DATE_ONLY.fullmatch(value))
    ):
        raise ValueError(
            f"{name} {value!r} is a date only; pass an aware datetime at the market's local day end"
            " (day start for window_start)"
        )
    return _when(value)


def _when(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(text):
    return " ".join(unicodedata.normalize("NFKC", text or "").translate(_QUOTE_MARKS).split())


def _quoted(match):
    return _norm(next(g for g in match.groups() if g is not None))


def _other_words(content):
    for pattern in _BREACH_TERMS:
        content = pattern.sub(" ", content)
    return len(content.split())


def _strip_quotes(text, exempt, k6=False):
    """Remove quoted spans whose content equals one of the exempt verified quotes.

    For K6 a span is removed only if it keeps 3 or more words once banned terms are taken out,
    so a quote that is only the banned term is still read as prose.
    """

    def keep(m):
        content = _quoted(m)
        if content in exempt and (not k6 or _other_words(content) >= 3):
            return " "
        return m.group(0)

    return _QUOTED.sub(keep, unicodedata.normalize("NFKC", text or ""))


def _cited(claim, records):
    ids = list(claim.get("evidence_ids") or [])
    ids += [q.get("evidence_id") for q in claim.get("quotes") or []]
    return [records[i] for i in dict.fromkeys(ids) if i in records]


def _pinned(entry):
    value = entry.get("value")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and all(
            isinstance(entry.get(k), str) and entry.get(k).strip()
            for k in ("query_id", "run_id", "result_hash")
        )
    )


def _quote_fault(text, record_text):
    """Why a normalised quote fails against its record's text, or None when it is found."""
    if len(text.split()) < 2:
        return "under 2 words"
    head = r"(?<!\w)" if re.match(r"\w", text) else ""
    tail = r"(?!\w)" if re.search(r"\w$", text) else ""
    if not re.search(head + re.escape(text) + tail, _norm(record_text)):
        return "not verbatim on word boundaries"
    return None


def _verified_quotes(claim, records):
    ids = claim.get("evidence_ids") or []
    out = set()
    for q in claim.get("quotes") or []:
        eid, text = q.get("evidence_id"), _norm(q.get("text"))
        if eid in ids and eid in records and _quote_fault(text, records[eid].get("text")) is None:
            out.add(text)
    return out


def _k1(claim, records, id_counts):
    cid = claim.get("id")
    if not cid:
        return "cut", "claim has no id"
    if id_counts[cid] > 1:
        return "cut", f"claim id {cid!r} is used {id_counts[cid]} times"
    ids = claim.get("evidence_ids") or []
    if not ids:
        return "cut", "no evidence ids"
    missing = [i for i in ids if i not in records]
    if missing:
        return "cut", f"unresolved evidence ids: {missing}"
    quote_texts = set()
    for q in claim.get("quotes") or []:
        eid = q.get("evidence_id")
        if eid not in ids:
            return "cut", f"quote cites {eid!r}, which the claim does not cite"
        text = _norm(q.get("text"))
        fault = _quote_fault(text, records[eid].get("text"))
        if fault:
            return "cut", f"quote {text!r} from {eid}: {fault}"
        quote_texts.add(text)
    for m in _QUOTED.finditer(unicodedata.normalize("NFKC", claim.get("text") or "")):
        if _quoted(m) not in quote_texts:
            return "cut", f"quoted words {_quoted(m)!r} have no quotes[] entry"
    return "pass", f"{len(ids)} ids resolved, {len(quote_texts)} quotes verbatim"


def _is_year(m, max_year):
    text = m.string
    if int(m.group(0)) > max_year or not _NOT_A_YEAR.match(text, m.end()):
        return False
    return bool(_YEAR_BEFORE.search(text, 0, m.start()) or _YEAR_AFTER.match(text, m.end()))


def _numerals(text, exempt, spans):
    text = _strip_quotes(text, exempt)
    text = _WINDOW_PHRASE.sub(
        lambda m: " " if int(m.group(1)) == spans[m.group(2).lower()] else m.group(0), text
    )
    for pattern in _NOT_FIGURES:
        text = pattern.sub(" ", text)
    text = _YEAR_TOKEN.sub(lambda m: " " if _is_year(m, spans["max_year"]) else m.group(0), text)
    for m in _NUMERAL.finditer(text):
        digits, frac, suffix = m.group(1), m.group(2) or "", (m.group(3) or "").lower()
        whole = re.sub(r"[,\u00a0\u202f ]", "", digits)
        yield m.group(0).strip(), float(whole + frac), max(len(frac) - 1, 0), suffix


def _shown_matches(shown, decimals, suffix, value):
    mult = _MULTIPLIER.get(suffix, 1)
    tolerance = 0.5 * 10**-decimals * mult + 1e-9
    targets = [abs(value)] + ([abs(value) * 100] if suffix in ("%", "percent") else [])
    return any(abs(t - shown * mult) <= tolerance for t in targets)


def _unmatched_numeral(text, exempt, entries, spans):
    for raw, shown, decimals, suffix in _numerals(text, exempt, spans):
        if not any(_shown_matches(shown, decimals, suffix, e["value"]) for e in entries):
            return raw
    return None


def _is_count(entry):
    words = set(re.findall(r"[a-z]+|%", str(entry.get("unit") or "").lower()))
    return not words & RATIO_WORDS and float(entry["value"]).is_integer()


def _k2(claim, rerun, verified, spans):
    entries = claim.get("numbers") or []
    unpinned = [e for e in entries if not _pinned(e)]
    if unpinned:
        return "cut", f"numbers entry without value, query_id, run_id or result_hash: {unpinned[0]}"
    raw = _unmatched_numeral(claim.get("text") or "", verified, entries, spans)
    if raw:
        return "cut", f"numeral {raw!r} has no pinned numbers entry"
    if rerun is not None:
        for e in entries:
            try:
                now = rerun(e)
            except Exception as exc:
                return "cut", f"re-run of {e['query_id']} failed: {type(exc).__name__}"
            if not isinstance(now, (int, float)) or isinstance(now, bool):
                return "cut", f"re-run of {e['query_id']} returned no number"
            old = e["value"]
            if _is_count(e):
                if now != old:
                    return (
                        "cut",
                        f"re-run of {e['query_id']} gave {now}, answer says {old} (counts exact)",
                    )
            elif abs(now - old) > 0.02 * abs(old):
                return "cut", f"re-run of {e['query_id']} gave {now}, answer says {old} (over 2%)"
    return "pass", f"{len(entries)} numbers pinned" + (
        ", re-run" if rerun is not None and entries else ""
    )


def named_markets(text):
    """The markets text names: ZA, NG and KE in that order, then the neighbours geo.py lists."""
    raw = _POSSESSIVE.sub("", unicodedata.normalize("NFKC", text or ""))
    split = _CAMEL.sub(" ", raw)
    folded = (_fold(raw), _fold(split))
    tags = [_fold(t).replace("_", "") for t in _HASHTAG.findall(raw)]
    found = []
    for code, rx in _PLACE_RE.items():
        codes = _PLACE_CODE_RE.get(code)
        if (any(rx.search(f) for f in folded)
                or any(t.startswith(part) or t.endswith(part) for t in tags for part in _TAG_PARTS[code])
                or codes is not None and (codes.search(raw) or codes.search(split))):
            found.append(code)
    return found


def located_market(record):
    """The market a record is located in, upper case, or None when it has no market or is market_assumed."""
    if "market_assumed" in {str(f).lower() for f in record.get("flags") or []}:
        return None
    return str(record.get("market") or "").upper() or None


def source_market(record):
    """The explicit source market for a record, upper case, or None when it is unknown."""
    value = record.get("source_market")
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if code in MARKETS else None


def place_support(text, records):
    """Return each named market's strongest support from the supplied evidence records."""
    located = set()
    sourced = set()
    for record in records:
        actual = located_market(record)
        if actual:
            located.add(actual)
        source = source_market(record)
        if source:
            sourced.add(source)
    return {
        code: "located" if code in located else "source" if code in sourced else None
        for code in named_markets(text)
    }


def place_fault(text, records, exempt=None):
    """Explain unsupported place claims, including source markets used outside feed scope."""
    evidence = list(records)
    support = place_support(text, evidence)
    missing = [MARKET_NAMES.get(code, code) for code, kind in support.items() if kind is None]
    if missing:
        return f"names {', '.join(missing)} but cites no record located there or from its feeds"

    scoped_text = _POSSESSIVE.sub("", _strip_quotes(text or "", exempt or set()))
    feed_scopes = {
        code: pattern for code, pattern in _FEED_SCOPE.items() if pattern.search(scoped_text)
    }
    source_codes = {source_market(record) for record in evidence}
    for code in feed_scopes:
        if code not in source_codes:
            name = MARKET_NAMES.get(code, code)
            return f"names {name} in feed wording but cites no record with source market {code}"

    source_only = [code for code, kind in support.items() if kind == "source"]
    missing_scope = [code for code in source_only if code not in feed_scopes]
    if missing_scope:
        name = MARKET_NAMES.get(missing_scope[0], missing_scope[0])
        return f"names {name} on source-market evidence without feed-scoped wording"

    residual = scoped_text
    for pattern in feed_scopes.values():
        residual = pattern.sub(" ", residual)

    residual_places = set(named_markets(residual)) & set(source_only)
    if residual_places:
        names = ", ".join(MARKET_NAMES.get(code, code) for code in source_only if code in residual_places)
        return f"names {names} outside its feed-scoped phrase; source evidence cannot support place or people claims"
    return None


def _k3(claim, records, start, end, market):
    cited = _cited(claim, records)
    for r in cited:
        try:
            posted = _when(r.get("posted_at"))
        except (TypeError, ValueError):
            return "cut", f"{r.get('id')} has no readable posted_at"
        if not start <= posted <= end:
            return "cut", f"{r.get('id')} posted {r.get('posted_at')}, outside the window"
        stated = str(r.get("market") or "").upper()
        if market and stated and stated != market.upper():
            return "cut", f"{r.get('id')} is located in {r.get('market')!r}, not {market}"
    fault = place_fault(claim.get("text") or "", cited, _verified_quotes(claim, records))
    if fault:
        return "cut", fault
    by_source = [MARKET_NAMES.get(code, code) for code, kind in place_support(claim.get("text") or "", cited).items()
                 if kind == "source"]
    return "pass", "cited records inside window" + (f" and {market}" if market else "") + (
        f"; {', '.join(by_source)} by source, in feed wording" if by_source else ""
    )


def _breach_term(text, exempt):
    text = _norm(_strip_quotes(text, exempt, k6=True))
    for pattern in _BREACH_TERMS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


def _k6(claim, records, verified):
    for r in _cited(claim, records):
        flags = {str(f).lower() for f in r.get("flags") or []}
        if flags & GENERATED_FLAGS or str(r.get("platform") or "").lower() in GENERATED_PLATFORMS:
            return "breach", f"{r.get('id')} is generated output"
    texts = [("text", claim.get("text"))]
    texts += [
        (f"quotes[{i}].translation", q.get("translation"))
        for i, q in enumerate(claim.get("quotes") or [])
    ]
    texts += [("basis", claim.get("basis")), ("falsifier", claim.get("falsifier"))]
    for field, text in texts:
        term = _breach_term(text or "", verified)
        if term:
            return "breach", f"{field}: banned term {term!r}"
    return "pass", "no age, demographic, Google Trends or generated evidence"


def _k8(claim):
    for q in claim.get("quotes") or []:
        if q.get("translation") and q.get("translation_marked") is not True:
            return "cut", f"unmarked translation on quote from {q.get('evidence_id')}"
    return "pass", "translations marked"


def _k5_basis(claim, records):
    """(the label the claim's authors allow, whether a named place has source-market support only)."""
    allowed = _allowed_label(claim, records)
    source_only = any(
        kind == "source" for kind in place_support(claim.get("text") or "", _cited(claim, records)).values()
    )
    return allowed, source_only


def inferred_only_by_source_step(claim, records):
    """True when the claim is an observation labelled inferred and K5's source-only step alone took it there: one
    independent creator allows single_source and a named place has source-market support only. The support check then
    judges it as an observation, not as interpretation (Albert, 3 Oct). The label and every evidence rule stay."""
    if claim.get("kind") != "observation" or claim.get("label") != "inferred":
        return False
    allowed, source_only = _k5_basis(claim, records)
    return source_only and allowed == "single_source"


def _k5_ceiling(claim, records):
    """(the highest label K5 allows, whether a named place has source-market support only)."""
    allowed, source_only = _k5_basis(claim, records)
    return (LABELS[max(LABELS.index(allowed) - 1, 0)] if source_only else allowed), source_only


def allowed_label(claim, records):
    """The highest label K5 allows the claim on these records: what its authors allow, one step lower when a named
    place has source-market support only."""
    return _k5_ceiling(claim, records)[0]


def _k5(claim, records):
    allowed, source_only = _k5_ceiling(claim, records)
    label = claim.get("label")
    if label not in LABELS:
        claim["label"] = "inferred"
        return "downgrade", f"label {label!r} not recognised, set to inferred"
    if LABELS.index(label) > LABELS.index(allowed):
        claim["label"] = allowed
        return "downgrade", f"label {label} above allowed {allowed}" + (
            ", one step lower for a source-only place" if source_only else ""
        )
    return "pass", f"label {label}, allowed {allowed}" + (
        ", one step lower for a source-only place" if source_only else ""
    )


def _allowed_label(claim, records):
    if claim.get("kind") != "observation":
        return "inferred"
    cited = [records[i] for i in dict.fromkeys(claim.get("evidence_ids") or []) if i in records]
    authors = []
    for r in cited:
        flags = {str(f).lower() for f in r.get("flags") or []}
        handle = str(r.get("handle") or "").strip().lower().lstrip("@")
        if handle and not flags & NOT_INDEPENDENT:
            authors.append((handle, str(r.get("platform") or "").lower()))
    handles = {h for h, _ in authors}
    cross_platform = any(a[0] != b[0] and a[1] != b[1] for a, b in combinations(authors, 2))
    metric = any(_pinned(n) for n in claim.get("numbers") or [])
    if cross_platform or (len(handles) >= 3 and metric):
        return "corroborated"
    if len(handles) >= 2:
        return "observed"
    return "single_source" if handles else "inferred"
