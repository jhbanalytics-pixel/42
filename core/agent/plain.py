"""Plain words for what a strategist reads: no table, tool or field names and no check codes.

The checks and the stored verdicts keep their own wording (rule names, field names) for review; this module only
rewrites the text an answer shows under "What we do not know", its follow-ups and its research steps.
"""

from __future__ import annotations

import re

# Warehouse tables, tools and answer fields, as a reader would name them. Longest names first, so item_state_current
# is read before item_state.
NAMES = {
    "google_search_signals": "Google search terms",
    "v_item_state_current": "current item trends",
    "item_counter_daily": "daily item counters",
    "tvf_item_timeseries": "an item's daily series",
    "tvf_search_posts": "the stored post search",
    "post_observations": "post sightings",
    "post_enrichment": "post readings",
    "v_post_source_markets": "post source markets",
    "calendar_analogues": "past calendar moments",
    "collection_health": "collection health",
    "recall_findings": "saved findings",
    "rising_topics": "rising topics",
    "search_posts": "stored post search",
    "socialcrawl_call": "live source search",
    "save_finding": "saving a finding",
    "cultural_map": "the item map",
    "canonical_key": "item key",
    "item_daily": "daily item counts",
    "item_state": "item trends",
    "series_test": "trend tests",
    "post_items": "post links to items",
    "sql_query": "warehouse count",
    "watch_next": "what to watch next",
    "sound_id": "sound id",
    "so_what": "why it matters",
}
_DATASET = re.compile(r"`?(?:ogilvy-trends-v2\.)?intelligence_42_(?:core|agent)\.", re.I)
_NAME = re.compile(r"\b(" + "|".join(sorted(map(re.escape, NAMES), key=len, reverse=True)) + r")\b", re.I)
_CODE = re.compile(r"\s*\((?:K(?:10|[1-9])|critic)(?:,\s*(?:K(?:10|[1-9])|critic))*\)", re.I)
_SECTION = re.compile(r"^(so_what|watch_next) item \d+ removed\b", re.I)
_CLAIM = re.compile(r"^Claim [\w.-]+ (cut|removed)\b")


def text(value: str) -> str:
    """value with tables, tools and fields named in plain words and check codes dropped."""
    if not isinstance(value, str):
        return value
    out = _SECTION.sub(lambda m: f"A line on {NAMES[m.group(1).lower()]} was removed", value)
    out = _CLAIM.sub(lambda m: f"A claim was {m.group(1)}", out)
    out = _DATASET.sub("", out)
    out = _NAME.sub(lambda m: NAMES[m.group(1).lower()], out)
    out = _CODE.sub("", out)
    return out.replace("`", "")


def gap(entry):
    """One gap with each of its texts in plain words."""
    if not isinstance(entry, dict):
        return entry
    return {k: text(v) if k in ("what", "searched", "why") else v for k, v in entry.items()}


def answer(shown: dict) -> dict:
    """The answer as a reader sees it: gaps in plain words. Claims, numbers and evidence are left as checked."""
    if not isinstance(shown, dict) or not isinstance(shown.get("gaps"), list):
        return shown
    return {**shown, "gaps": [gap(g) for g in shown["gaps"]]}


# How a table reads when a step names what it counts from. Tables not here read by their name in plain words.
_TABLE_WORDS = {"posts": "stored posts", "creators": "creators", "calendar": "the calendar", "findings": "saved findings"}


def tables(sql: str) -> str:
    """The tables a query reads, in plain words joined with "and"; "" when the SQL does not parse or names none."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = [t for t in sqlglot.parse(sql or "", dialect="bigquery") if t is not None]
    except sqlglot.errors.SqlglotError:
        return ""
    ctes = {c.alias_or_name for t in trees for c in t.find_all(exp.CTE)}
    names = []
    for tree in trees:
        for table in tree.find_all(exp.Table):
            name = table.this.name if isinstance(table.this, exp.Func) else table.name
            if not name or (not table.db and name in ctes):
                continue
            word = _TABLE_WORDS.get(name.lower()) or text(name.lower())
            if word not in names:
                names.append(word)
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 2 else names)
