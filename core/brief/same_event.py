"""Whether two published cards of one market are one event told twice.

    same_event(title_a, title_b)   True when, nicknames replaced, one title's words are all in the other's and at
                                   least MIN_SHARED words are shared
    shares_evidence(a, b)          True when two candidates have a post in common or MIN_CREATORS creators in common
    aliases(path)                  {nickname: name} from core/brief/event_aliases.yaml

Both are needed. Matching words alone merges a rugby and a cricket card ("springboks, south africa, england" and
"proteas, south africa, england"), and sharing a creator alone merges anything one creator posts about. The words of a
title are folded (lower case, accents off, whole words), a nickname is replaced by its name, and the fixture words vs,
v, versus, against and the are dropped. One title must add nothing to the other (a subset), so a title with a word of
its own that the other lacks (banyana, proteas, jollof) is another story. Pure text and ids; nothing is looked up.
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

ALIASES = Path(__file__).parent / "event_aliases.yaml"
MIN_SHARED = 2
MIN_CREATORS = 2
FIXTURE_WORDS = frozenset({"vs", "v", "versus", "against", "the"})


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


def words(title, path=ALIASES):
    """The set of words a title says, nicknames replaced by their names, longest nickname first."""
    text = fold(title)
    for nick, name in sorted(_table(Path(path)).items(), key=lambda kv: (-len(kv[0]), kv[0])):
        text = text.replace(f" {nick} ", f" {name} ")
    return frozenset(text.split()) - FIXTURE_WORDS


def same_event(title_a, title_b, path=ALIASES):
    a, b = words(title_a, path), words(title_b, path)
    return len(a & b) >= MIN_SHARED and (a <= b or b <= a)


def _creators(cand):
    return {(str(e.get("platform") or "").lower(), str(e["handle"]).lower())
            for e in cand["pack"].get("evidence") or [] if e.get("handle")}


def shares_evidence(a, b):
    return bool(a["posts"] & b["posts"]) or len(_creators(a) & _creators(b)) >= MIN_CREATORS
