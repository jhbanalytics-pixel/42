"""Searching now (contract.md section 21): the Google search interest rows collect wrote to google_search_signals, as
the five-field rows the app's SearchingNow strip reads on Today and Discover. Search interest, never posts: these
rows are not cards, never pass or skip the trust gate and are never evidence."""
import datetime as dt
import logging
import re
import unicodedata

log = logging.getLogger("f42.api.searching")

MARKETS = ("ZA", "NG", "KE")
SOURCES = ("google_trending",)  # W8-DEC-04: google_bq is parked and google_rss is triage only, so neither is shown
SEARCH_DAYS = 3  # SAST fetch days read, ending on the day asked for
PER_MARKET = 10
SAST = dt.timezone(dt.timedelta(hours=2))
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_UTC = re.compile(r"(\d{4}-\d{2}-\d{2})T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?Z")


def _real_date(text):
    try:
        return dt.date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def _refreshed(value):
    """A calendar date, or a UTC timestamp ending in Z, as the strip accepts them."""
    if not isinstance(value, str):
        return False
    if _DATE.fullmatch(value):
        return _real_date(value)
    m = _UTC.fullmatch(value)
    return bool(m) and _real_date(m[1])


def _day(value):
    """The plain day a valid refreshed_at stands for: a date as it is, a UTC timestamp as its day in SAST."""
    if _DATE.fullmatch(value):
        return value
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(SAST).date().isoformat()


# Fixtures ("croatia vs england"): a match between two national teams, neither of them the strip's own market, is
# left out, and one fixture written several ways is shown once. A club fixture or one naming the market stays.
_VS = {"vs", "v", "versus"}
_FILLER = {"national", "football", "soccer", "team", "teams", "standings", "standing", "lineups", "lineup", "live",
           "score", "scores", "result", "results", "highlights", "prediction", "predictions", "h2h", "today", "match",
           "fc", "women", "womens", "men", "mens", "u17", "u20", "u23", "stats", "tickets", "timeline", "squad"}
_OWN = {"ZA": ("south africa", "sa", "rsa", "bafana bafana", "bafana", "banyana banyana", "banyana", "mzansi"),
        "NG": ("nigeria", "naija", "super eagles", "super falcons"),
        "KE": ("kenya", "harambee stars", "harambee starlets")}
_COUNTRY_ALIASES = {"czech republic": "czechia", "usa": "united states",
                    "united states of america": "united states", "cote d ivoire": "ivory coast", "drc": "dr congo",
                    "congo dr": "dr congo", "democratic republic of the congo": "dr congo", "korea republic":
                    "south korea", "republic of ireland": "ireland", "uae": "united arab emirates", "turkiye": "turkey",
                    "cabo verde": "cape verde", "eswatini": "eswatini", "swaziland": "eswatini"}
_COUNTRIES = frozenset(_COUNTRY_ALIASES) | frozenset(_COUNTRY_ALIASES.values()) | frozenset((
    "afghanistan, albania, algeria, andorra, angola, argentina, armenia, australia, austria, azerbaijan, bahamas, "
    "bahrain, bangladesh, barbados, belarus, belgium, belize, benin, bhutan, bolivia, bosnia and herzegovina, "
    "bosnia, botswana, brazil, brunei, bulgaria, burkina faso, burundi, cambodia, cameroon, canada, cape verde, "
    "central african republic, chad, chile, china, colombia, comoros, congo, costa rica, croatia, cuba, cyprus, "
    "czechia, denmark, djibouti, dominican republic, ecuador, egypt, el salvador, england, equatorial guinea, "
    "eritrea, estonia, eswatini, ethiopia, fiji, finland, france, gabon, gambia, georgia, germany, ghana, greece, "
    "guatemala, guinea, guinea bissau, haiti, honduras, hong kong, hungary, iceland, india, indonesia, iran, "
    "iraq, ireland, northern ireland, israel, italy, ivory coast, jamaica, japan, jordan, kazakhstan, kenya, "
    "kosovo, kuwait, kyrgyzstan, latvia, lebanon, lesotho, liberia, libya, liechtenstein, lithuania, luxembourg, "
    "madagascar, malawi, malaysia, mali, malta, mauritania, mauritius, mexico, moldova, montenegro, morocco, "
    "mozambique, namibia, netherlands, holland, new zealand, nicaragua, niger, nigeria, north korea, "
    "north macedonia, norway, oman, pakistan, palestine, panama, paraguay, peru, philippines, poland, portugal, "
    "qatar, romania, russia, rwanda, san marino, saudi arabia, scotland, senegal, serbia, seychelles, "
    "sierra leone, singapore, slovakia, slovenia, somalia, south africa, south korea, south sudan, spain, sudan, "
    "sweden, switzerland, syria, tanzania, thailand, togo, trinidad and tobago, tunisia, turkey, uganda, ukraine, "
    "united arab emirates, united states, uruguay, uzbekistan, venezuela, vietnam, wales, yemen, zambia, "
    "zimbabwe, dr congo"
).split(", "))



def _sides(term):
    """(left words, right words) of an "X vs Y" term with filler words ("national football team", "standings")
    dropped, or None when the term is not one fixture."""
    words = _tokens(term)
    at = [i for i, w in enumerate(words) if w in _VS]
    if len(at) != 1:
        return None
    left, right = ([w for w in part if w not in _FILLER] for part in (words[:at[0]], words[at[0] + 1:]))
    return (left, right) if left and right else None


def _country(words, at_end):
    """The country a side names (its last words on the left, its first words on the right), or None."""
    for n in range(min(len(words), 5), 0, -1):
        name = " ".join(words[-n:] if at_end else words[:n])
        if name in _COUNTRIES:
            return _COUNTRY_ALIASES.get(name, name)
    return None


def _names(words, phrase):
    want = phrase.split()
    return any(words[i:i + len(want)] == want for i in range(len(words) - len(want) + 1))


def _fixture(term, market):
    """None for a term that is not a fixture; else (shown, key): shown False for a match between two national
    teams neither of which is the market's, key the same for every way of writing one fixture."""
    sides = _sides(term)
    if sides is None:
        return None
    left, right = sides
    own = any(_names(side, phrase) for side in sides for phrase in _OWN.get(market, ()))
    names = (_country(left, True), _country(right, False))
    shown = own or not all(names)
    key = frozenset(n or " ".join(side) for n, side in zip(names, sides))
    return shown, key


def _signal(r, markets, start, end):
    """The row in the strip's shape, or None when the strip would reject it or it is outside the read."""
    term, rank, day = r.get("term"), r.get("rank"), r.get("fetch_day")
    if r.get("market") not in markets or r.get("source") not in SOURCES:
        return None
    if not isinstance(term, str) or not term.strip() or not isinstance(day, str) or not start <= day <= end:
        return None
    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 1):
        return None
    if not _refreshed(r.get("refreshed_at")):
        return None
    return {"term": term.strip(), "market": r["market"], "source": r["source"], "rank": rank,
            "refreshed_at": r["refreshed_at"] if r["source"] == "google_trending" else _day(r["refreshed_at"])}


def _tokens(text):
    """Lower-case words with accents folded, split on spaces, punctuation and underscores."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return [t for t in re.split(r"[\W_]+", folded.casefold()) if t]


def _names_hidden(term, hidden):
    """True when the term names a suppressed person (contract.md section 16): their handle, creator id or display
    name, read as words with accents, case, spaces, punctuation and underscores ignored, equals any run of whole
    words in the term written together ("fixture hidden handle live" names fixture_hidden_handle)."""
    keys, ids, names = hidden
    words = _tokens(term)
    runs = {"".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, len(words) + 1)}
    needles = {k.split(":", 1)[1] for k in keys} | set(ids) | set(names)
    return any((n := "".join(_tokens(str(needle)))) and n in runs for needle in needles)


def _order(s):
    return (s["rank"] is None, s["rank"] or 0, s["term"], s["source"], s["refreshed_at"])


def searching_now(store, markets, as_of, hidden):
    """Up to PER_MARKET rows a market from the newest fetch day of each market and source in the SEARCH_DAYS SAST
    days ending on as_of (YYYY-MM-DD): one row a term in a market, at its best reported rank, ranked rows first,
    markets in ZA, NG, KE order. hidden is the suppression list as today.hidden_people gives it; a term naming a
    suppressed person is left out, and while the list cannot be read (None) no term is sent. [] when there is
    nothing, the table does not exist yet or the read fails."""
    if hidden is None:
        return []
    markets = [m for m in MARKETS if m in markets]
    end = dt.date.fromisoformat(as_of)
    start = (end - dt.timedelta(days=SEARCH_DAYS - 1)).isoformat()
    end = end.isoformat()
    try:
        rows = store.search_signals(start, end, markets) if markets else None
        if not rows:
            return []
        from core.collect.gdelt import blocked  # rule 1: no term with an age lens leaves the API, whatever wrote it
        picked = [(r["fetch_day"], s) for r in rows
                  if (s := _signal(r, markets, start, end)) and not _names_hidden(s["term"], hidden)
                  and not blocked(s["term"])]
        newest = {}
        for day, s in picked:
            key = (s["market"], s["source"])
            newest[key] = max(newest.get(key, day), day)
        best = {}
        for day, s in sorted(picked, key=lambda p: _order(p[1])):
            fixture = _fixture(s["term"], s["market"])
            if fixture is not None and not fixture[0]:
                continue
            key = (s["market"], fixture[1] if fixture else s["term"].casefold())
            if day == newest[(s["market"], s["source"])] and key not in best:
                best[key] = s
    except Exception as exc:  # noqa: BLE001 - the strip is extra; Today and Discover must not fail on it
        log.warning("searching now: the search signal read failed (%s); the page is shown without it",
                    type(exc).__name__)
        return []
    out = []
    for market in markets:  # the cap applies after fixtures are left out, so the next terms fill the strip
        out += sorted((s for (m, _), s in best.items() if m == market), key=_order)[:PER_MARKET]
    return out
