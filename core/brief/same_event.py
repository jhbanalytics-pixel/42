"""Whether two items of one market name the same event (a fixture between two national teams).

    event_key(row) -> frozenset of team entities, or None

The entities are the teams of core/brief/event_aliases.yaml found in the item's title, label and key by whole words,
a nickname counting for its team ("Bafana Bafana" is South Africa). An item names an event when it names two or more
teams; two items name the same one when their sets are equal. One team alone is a team, not an event, so it never
joins two items. Pure text on the item's own names: no post, count or score is read, and nothing is looked up.
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

ALIASES = Path(__file__).parent / "event_aliases.yaml"
MIN_TEAMS = 2


def fold(text):
    """Lower case, accents removed, every run of other characters a single space, padded so words match whole."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    return " " + " ".join(re.findall(r"[a-z0-9]+", plain)) + " "


@lru_cache(maxsize=None)
def _names(path=ALIASES):
    """[(folded name, team)] longest name first, so "south africa" is read before any shorter name inside it."""
    teams = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("teams") or {}
    pairs = [(fold(name), team) for team, aliases in teams.items() for name in [team, *(aliases or [])]]
    return tuple(sorted(pairs, key=lambda p: (-len(p[0]), p[0])))


def teams_in(text, path=ALIASES):
    """The set of teams named in text. A name read is taken out of the text, so it is read once."""
    left, found = fold(text), set()
    for name, team in _names(path):
        if name in left:
            found.add(team)
            left = left.replace(name, " ")
    return frozenset(found)


def event_key(row, path=ALIASES):
    found = frozenset().union(*(teams_in(row.get(field), path) for field in ("title", "label", "canonical_key")))
    return found if len(found) >= MIN_TEAMS else None
