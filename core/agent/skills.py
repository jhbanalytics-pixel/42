"""Ask's skills: the SKILL.md files under core/skills (AGENT.md skills), and which one an ask uses.

L4's forms send an optional `skill` on POST /api/ask (contract.md section 15.2 on full-42-l4, core/api/skins.py
SKILLS): brand-implication for the brand lens, creator-read for creator brand-fit and context-pack for the creative
context pack. Anything else is refused, as f42-api refuses it with 400. spike-explain (FEATURES 28) is the jump a user
clicked in a chart, so its request must also carry the item_id, the market and the day of that jump. L4's app sends
that jump as spike: {item_id, market, date, series} with no skill (contract.md 14.1 on full-42-l4); a request may also
name skill "spike-explain" with item_id, market and day at the top level. creator-fit names one creator, so the ask
runs only for a creator 42 may name (contract.md 12.1 on full-42-l4).
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
NAMES = ("culture-read", "brand-lens", "creator-fit", "context-pack", "spike-explain", "investigation")
DEFAULT = "culture-read"
REQUEST_SKILLS = {"brand-implication": "brand-lens", "creator-read": "creator-fit", "context-pack": "context-pack",
                  "spike-explain": "spike-explain"}
MARKETS = ("ZA", "NG", "KE")
ITEM_ID = re.compile(r"[A-Za-z0-9_:.-]{1,200}")
SERIES = re.compile(r"[A-Za-z0-9_]{1,80}")  # as core/api/spike.py SERIES_RE on full-42-l4
SPIKE_KEYS = ("item_id", "market", "date", "series")
# Who 42 may name: every creators row behind the handle is at page tier now and none is suppressed (core/api/skins.py
# nameable_authors and core/api/people.py PAGE_TIERS on full-42-l4). The view is L1's, in core/schema/core.sql on
# full-42. The handle is matched as store.creator_key matches it there: X as x, trimmed, lower case, no @ or u/.
PAGE_TIERS = ("macro", "mega")
CREATOR_SQL = (
    "SELECT c.creator_id, c.tier, c.creator_id IN (SELECT s.creator_id FROM "
    "intelligence_42_core.v_suppressed_creators s) AS suppressed FROM intelligence_42_core.creators c "
    "WHERE CONCAT(IF(LOWER(TRIM(c.platform)) = 'twitter', 'x', LOWER(TRIM(c.platform))), ':', "
    "LOWER(REGEXP_REPLACE(TRIM(c.handle), r'^@*(u/)?', ''))) = @key")
# The platform words L4's creator fit form writes (PLATFORM_WORDS in app/frontend/src/ui/TrendCard.jsx there).
PLATFORM_WORDS = {"tiktok": "tiktok", "instagram": "instagram", "youtube": "youtube", "x": "x", "twitter": "x",
                  "reddit": "reddit", "facebook": "facebook", "telegram": "telegram", "threads": "threads",
                  "apple music": "apple_music", "news": "news"}
# L4's form asks "How well do the public posts of <handle> on <platform> fit <brand>? ..." (skills42.jsx there).
_CREATOR = re.compile(r"\bthe public posts of @?(?:u/)?([A-Za-z0-9_.-]{1,100}) on ("
                      + "|".join(sorted(PLATFORM_WORDS, key=len, reverse=True)) + r") fit ", re.I)
NOT_NAMEABLE = ("creator-fit runs only for a creator 42 may name (page tier, not suppressed), and this creator is not "
                "one, so the question was not researched")
UNREADABLE = ("Whether 42 may name this creator could not be checked, so creator-fit was not run and nothing was "
              "spent")


def load_skill(name) -> str:
    """The SKILL.md text of a known skill. Only the names in NAMES are read, so no path reaches the file system."""
    if not isinstance(name, str) or name not in NAMES:
        raise ValueError("skill must be one of " + ", ".join(NAMES))
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def skill_for(request: dict) -> str:
    """The skill an ask uses: spike-explain for L4's spike, culture-read when the request carries no skill, else the
    file L4's value names."""
    sent = request.get("skill")
    if request.get("spike") is not None:
        if sent not in (None, "spike-explain"):
            raise ValueError("a spike is explained by spike-explain; leave skill out or send spike-explain")
        sent = "spike-explain"
    if sent is None:
        return DEFAULT
    if not isinstance(sent, str) or sent not in REQUEST_SKILLS:
        raise ValueError("skill must be one of " + ", ".join(REQUEST_SKILLS) + ", or left out")
    if sent == "spike-explain":
        jump(request)
    return REQUEST_SKILLS[sent]


def jump(request: dict) -> dict | None:
    """The jump a spike-explain request names, {item_id, market, day} plus series when L4 sent one; None for any
    other request. L4's spike is read when present, else the top-level item_id, market and day."""
    spike = request.get("spike")
    if spike is None:
        if request.get("skill") != "spike-explain":
            return None
        found = {k: request.get(k) for k in ("item_id", "market", "day")}
    else:
        if not isinstance(spike, dict) or set(spike) - set(SPIKE_KEYS):
            raise ValueError("spike takes only item_id, market, date and series")
        found = {"item_id": spike.get("item_id"), "market": spike.get("market"), "day": spike.get("date")}
        if request.get("market") not in (None, found["market"]):
            raise ValueError("the spike's market must be the ask's market")
        series = spike.get("series")
        if series is not None:
            if not isinstance(series, str) or not SERIES.fullmatch(series):
                raise ValueError("spike series must be 1 to 80 letters, digits or _, or left out")
            found["series"] = series
    _jump(found)
    return found


def _jump(found: dict) -> None:
    """A jump is an item_id of ITEM_ID's characters, a market and a day as YYYY-MM-DD."""
    item_id, market, day = found["item_id"], found["market"], found["day"]
    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
        raise ValueError("spike-explain needs the item_id of the jump: 1 to 200 letters, digits, _, :, . or -")
    if market not in MARKETS:
        raise ValueError("spike-explain needs a market, one of " + ", ".join(MARKETS))
    wrong = ValueError(f"spike-explain needs the day of the jump as YYYY-MM-DD; got {day!r}")
    if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):  # not 2026-W39-4
        raise wrong
    try:
        date.fromisoformat(day)
    except ValueError:
        raise wrong from None


def creator_key(question: str) -> str:
    """The "platform:handle" key of the creator L4's creator fit form names in the question."""
    found = _CREATOR.search(question)
    if not found:
        raise ValueError("creator-fit needs the creator as the creator page names it: <handle> on <platform>")
    return f"{PLATFORM_WORDS[found.group(2).lower()]}:{found.group(1).lower()}"


def check_creator(question: str, warehouse, max_bytes_billed: int) -> str:
    """The creator's key when 42 may name them, read through the ask's warehouse; ValueError otherwise. A creator 42
    has no creators row for is not named, and a list that cannot be read names no one."""
    key = creator_key(question)
    try:
        rows = warehouse.run(CREATOR_SQL, {"key": key}, max_bytes_billed)
    except Exception:
        raise ValueError(UNREADABLE) from None
    if not rows or not all(r.get("tier") in PAGE_TIERS and r.get("suppressed") is False for r in rows):
        raise ValueError(NOT_NAMEABLE)
    return key
