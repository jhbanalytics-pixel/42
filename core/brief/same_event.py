"""Whether two published cards of one market are one event told twice.

    same_event(title_a, title_b, market)   True when the two titles have equal words once nicknames are replaced, and
                                           say something beyond the market's own name
    shares_evidence(a, b)                  True when two candidates have a post in common or MIN_CREATORS creators in
                                           common
    aliases(path)                          {nickname: name} from core/brief/event_aliases.yaml

Both are needed. Matching words alone merges a rugby and a cricket card, and sharing a creator alone merges anything one
creator posts about. The words of a title are folded (lower case, accents off, whole words), a nickname is replaced by
its name, the fixture words vs, v, versus, against and the are dropped, and the market's own country name becomes one
token that never counts as a shared word, so "South Africa" alone is no event. The two word sets must be EQUAL: a title
with a word of its own that the other lacks (banyana, women, u20, pitso, petrol) is another story. Pure text and ids;
nothing is looked up.
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

from core.brief.payload import LABELS

ALIASES = Path(__file__).parent / "event_aliases.yaml"
MIN_SHARED = 1
MIN_CREATORS = 2
FIXTURE_WORDS = frozenset({"vs", "v", "versus", "against", "the"})
MARKET_TOKEN = "<market>"


def fold(text):
    """Lower case, accents removed, every run of other characters one space, padded so a phrase matches whole words."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    return " " + " ".join(re.findall(r"[a-z0-9]+", plain)) + " "


@lru_cache(maxsize=None)
def _table(path):
    table = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("aliases") or {}
    return {fold(k).strip(): fold(v).strip() for k, v in table.items()}


def aliases(path=ALIASES):
    return dict(_table(Path(path)))


def words(title, market, path=ALIASES):
    """The set of words a title says: nicknames replaced by their names, longest nickname first, then the market's own
    name as one token."""
    text = fold(title)
    for nick, name in sorted(_table(Path(path)).items(), key=lambda kv: (-len(kv[0]), kv[0])):
        text = text.replace(f" {nick} ", f" {name} ")
    text = text.replace(fold(LABELS[market]), f" {MARKET_TOKEN} ")
    return frozenset(text.split()) - FIXTURE_WORDS


def same_event(title_a, title_b, market, path=ALIASES):
    a, b = words(title_a, market, path), words(title_b, market, path)
    return a == b and len((a & b) - {MARKET_TOKEN}) >= MIN_SHARED


def _creators(cand):
    return {(str(e.get("platform") or "").lower(), str(e["handle"]).lower())
            for e in cand["pack"].get("evidence") or [] if e.get("handle")}


def shares_evidence(a, b):
    return bool(a["posts"] & b["posts"]) or len(_creators(a) & _creators(b)) >= MIN_CREATORS
