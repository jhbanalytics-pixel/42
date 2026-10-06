"""Ask's history tools (BUILD.md 3.3): history, an item's earlier waves, and analogues, the cultural map items nearest
to it, each with its own past waves.

Waves come from v_item_waves (DATA.md, end of section 3.6). Moments come from calendar and the newest
calendar_analogues row per moment: a wave recurs on a moment when it peaked within 7 days of the moment's date or of
its analogue date last year (G8). Neighbours are ranked by exact cosine distance over stored cultural_map centroids in
plain SQL, with no VECTOR_SEARCH and no model call; an item with no centroid falls back to shared label words and the
result says so. Every read goes through sql_query and every wave, moment and neighbour carries the query_id of the
query that produced it (rule 5). Every value is a named scalar parameter.
"""

from __future__ import annotations

import re

from core.agent.context import Refused, RunContext
from core.agent.tools.sql_query import Warehouse, sql_query
from core.agent.tools.warehouse import _day, _match, _terms

MARKETS = ("ZA", "NG", "KE")
MAX_ITEMS = 5
MAX_K = 20
MOMENT_DAYS = 7      # G8: a peak within 7 days of a moment, or of the same date last year
RECURRING_PEAK = 8   # a wave counts for Recurring and Seasonal at 8 or more posts on its peak day (DATA.md 3.6)
MIN_WORD = 3         # label words shorter than this do not count as shared
WORDS = r"[\pL\pN]+"

FALLBACK = "so these analogues share words in their labels; they are not matched on meaning."
CENTROID_NOTES = {
    "no stored centroid": f"No stored centroid for this item, {FALLBACK}",
    "no centroid rows": f"No centroid rows to compare this item with, {FALLBACK}",
    "centroid query failed": f"The centroid query failed, {FALLBACK}",
}
CREATORS_NOTE = ("Creator items are left out: whether 42 may name a creator (page tier and not suppressed) cannot be "
                 "checked from the cultural map, so no creator is shown.")
TRUNCATED_NOTE = ("A query stopped at its first 500 rows, so some waves or items are missing. Do not read a missing "
                  "wave as no past waves; narrow with market or since.")
NO_ITEM_NOTE = ("No item in 42's cultural map matches this text, so these analogues share words with it in their "
                "labels; they are not matched on meaning.")


def _open_map(columns: str) -> str:
    """The newest open cultural_map row per item (DATA.md: joins to cultural_map filter valid_to IS NULL). Each query
    names only the columns it reads, so a column staging lacks breaks only the query that needs it."""
    return (f"SELECT m.item_id, {columns} FROM intelligence_42_core.cultural_map m WHERE m.valid_to IS NULL "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY m.item_id ORDER BY m.valid_from DESC) = 1")


def _market(ctx: RunContext, market) -> str | None:
    market = market or ctx.market
    if market is not None and market not in MARKETS:
        raise Refused(f"market must be one of {', '.join(MARKETS)}; got {market!r}.")
    return market


def _text(value) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _reason(e: Exception) -> str:
    first = str(e).splitlines()[0] if str(e) else type(e).__name__
    return " ".join(re.sub(r"https?://\S+", "", first).split())[:300]


def _in(item_ids: list[str], params: dict) -> str:
    names = []
    for i, item_id in enumerate(item_ids):
        params[f"item_{i}"] = item_id
        names.append(f"@item_{i}")
    return f"({', '.join(names)})"


def _pinned(result: dict) -> list[dict]:
    return [{**row, "query_id": result["query_id"]} for row in result["rows"]]


def _mark(ctx: RunContext, query_ids: list[str], tool: str) -> None:
    """Mark this tool's own query records. The model cannot set this field, so save_finding trusts item ids only from
    marked records, never from a sql_query the model wrote."""
    for query_id in query_ids:
        ctx.queries[query_id]["tool"] = tool


def _cut(out: dict, results: list[dict]) -> None:
    """Carry sql_query's truncated flag, so a list cut at 500 rows never reads as complete."""
    out["truncated"] = any(r.get("truncated") for r in results)
    if out["truncated"]:
        out["truncated_note"] = TRUNCATED_NOTE


def _find_items(ctx: RunContext, warehouse: Warehouse, query: str, limit: int) -> dict:
    """Open cultural_map items whose label, canonical key or an alias holds every term of one OR group of query."""
    params = {"limit": limit, "q": query}
    where = [_match(_terms(query, params), "(CONTAINS_SUBSTR(c.label, {0}) OR CONTAINS_SUBSTR(c.canonical_key, {0}) "
                                           "OR EXISTS (SELECT 1 FROM UNNEST(c.aliases) a WHERE CONTAINS_SUBSTR(a, {0})))")]
    sql = (f"WITH c AS ({_open_map('m.kind, m.label, m.canonical_key, m.aliases')}) "
           "SELECT c.item_id, c.kind, c.label FROM c "
           f"WHERE c.kind != 'creator' AND {' AND '.join(where)} "
           "ORDER BY IF(LOWER(c.label) = LOWER(@q), 0, 1), c.label, c.item_id LIMIT @limit")
    return sql_query(ctx, warehouse, sql, purpose=f"history: map items matching {query}", params=params)


def _waves(ctx: RunContext, warehouse: Warehouse, item_ids: list[str], market: str | None, since=None) -> dict:
    params = {}
    ids = _in(item_ids, params)
    where = [f"w.item_id IN {ids}"]
    if market:
        params["market"] = market
        where.append("w.market = @market")
    outer = ""
    if since is not None:
        params["since"] = since
        outer = "WHERE w.wave_end >= @since "
    sql = (
        "WITH w AS (SELECT w.item_id, w.market, w.wave_start, w.wave_end, w.peak_date, w.peak_posts, "
        "ROW_NUMBER() OVER (PARTITION BY w.item_id, w.market ORDER BY w.wave_start DESC) = 1 AS latest "
        f"FROM intelligence_42_core.v_item_waves w WHERE {' AND '.join(where)}), "
        f"cm AS ({_open_map('m.label')}) "
        "SELECT w.item_id, w.market, cm.label, w.wave_start, w.wave_end, w.peak_date, w.peak_posts, w.latest, "
        f"w.peak_posts >= {RECURRING_PEAK} AS counts_for_recurring, "
        f"w.peak_posts >= {RECURRING_PEAK} AND EXISTS (SELECT 1 FROM w e WHERE e.item_id = w.item_id "
        "AND e.market = w.market "
        f"AND e.peak_posts >= {RECURRING_PEAK} "
        f"AND ABS(DATE_DIFF(e.peak_date, DATE_SUB(w.peak_date, INTERVAL 1 YEAR), DAY)) <= {MOMENT_DAYS}) "
        "AS same_time_last_year "
        f"FROM w LEFT JOIN cm ON cm.item_id = w.item_id {outer}"
        "ORDER BY w.item_id, w.market, w.wave_start"
    )
    return sql_query(ctx, warehouse, sql, purpose=f"history: waves of {', '.join(item_ids)}", params=params)


def _moments(ctx: RunContext, warehouse: Warehouse, item_ids: list[str], market: str | None) -> dict:
    """Calendar moments within MOMENT_DAYS of a wave's peak, by the moment's date or its analogue date last year.
    linked says whether calendar.item_ids names the item; an unlinked moment only fell near the peak."""
    params = {}
    where = [f"w.item_id IN {_in(item_ids, params)}"]
    if market:
        params["market"] = market
        where.append("w.market = @market")
    sql = (
        "WITH w AS (SELECT w.item_id, w.market, w.peak_date FROM intelligence_42_core.v_item_waves w "
        f"WHERE {' AND '.join(where)}), "
        "an AS (SELECT a.moment_date, a.market, a.name, a.analogue_date, a.status "
        "FROM intelligence_42_core.calendar_analogues a "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY a.moment_date, a.market, a.name ORDER BY a.computed_at DESC) = 1) "
        "SELECT w.item_id, w.market, w.peak_date, c.moment_date, c.name, c.kind, an.analogue_date, "
        "an.status AS analogue_status, "
        f"IF(ABS(DATE_DIFF(w.peak_date, c.moment_date, DAY)) <= {MOMENT_DAYS}, 'moment_date', 'analogue_date') "
        "AS matched_on, "
        "EXISTS (SELECT 1 FROM UNNEST(c.item_ids) it WHERE it = w.item_id) AS linked "
        "FROM w JOIN intelligence_42_core.calendar c ON c.market = w.market "
        "LEFT JOIN an ON an.moment_date = c.moment_date AND an.market = c.market AND an.name = c.name "
        f"WHERE ABS(DATE_DIFF(w.peak_date, c.moment_date, DAY)) <= {MOMENT_DAYS} "
        f"OR ABS(DATE_DIFF(w.peak_date, an.analogue_date, DAY)) <= {MOMENT_DAYS} "
        "ORDER BY w.item_id, w.market, w.peak_date, c.moment_date, c.name"
    )
    return sql_query(ctx, warehouse, sql, purpose=f"history: moments near the peaks of {', '.join(item_ids)}",
                     params=params)


def history(ctx: RunContext, warehouse: Warehouse, item_id=None, query=None, market=None, since=None) -> dict:
    """An item's waves, oldest first, each with start, end, peak day and peak posts; latest marks the current or most
    recent wave, so the others are its earlier waves. query finds up to five map items by label instead of an id."""
    item_id, query = _text(item_id), _text(query)
    if not item_id and not query:
        raise Refused("Give an item_id or a query.")
    market = _market(ctx, market)
    since = None if since is None else _day(since, "since")

    out, query_ids, results = {}, [], []
    if item_id:
        item_ids = [item_id]
    else:
        found = _find_items(ctx, warehouse, query, MAX_ITEMS)
        query_ids.append(found["query_id"])
        results.append(found)
        out["items"] = _pinned(found)
        item_ids = [row["item_id"] for row in found["rows"]]
        if not item_ids:
            _mark(ctx, query_ids, "history")
            return {**out, "waves": [], "moments": [], "query_id": found["query_id"], "query_ids": query_ids,
                    "truncated": False, "note": f"No item in 42's cultural map matches {query!r}."}

    waves = _waves(ctx, warehouse, item_ids, market, since)
    query_ids.append(waves["query_id"])
    results.append(waves)
    out.update(waves=_pinned(waves), moments=[], query_id=waves["query_id"])
    try:
        moments = _moments(ctx, warehouse, item_ids, market)
    except Exception as e:  # calendar_analogues not built yet, or the byte cap: the waves still answer
        out.update(calendar="unavailable", calendar_reason=_reason(e))
    else:
        query_ids.append(moments["query_id"])
        results.append(moments)
        out["moments"] = _pinned(moments)
    out["query_ids"] = query_ids
    _mark(ctx, query_ids, "history")
    _cut(out, results)
    return out


def _nearest(ctx: RunContext, warehouse: Warehouse, item_id: str, k: int) -> dict:
    """Exact cosine distance from item_id's centroid to every other open centroid of the same length."""
    sql = (
        f"WITH m AS ({_open_map('m.kind, m.label, m.centroid')}), "
        "a AS (SELECT m.centroid FROM m WHERE m.item_id = @item_id AND ARRAY_LENGTH(m.centroid) > 0), "
        "c AS (SELECT m.item_id, m.kind, m.label, m.centroid FROM m "
        "WHERE m.item_id != @item_id AND ARRAY_LENGTH(m.centroid) > 0 AND m.kind != 'creator'), "
        "s AS (SELECT c.item_id, c.kind, c.label, 1 - SAFE_DIVIDE("
        "(SELECT SUM(x * a.centroid[OFFSET(i)]) FROM UNNEST(c.centroid) x WITH OFFSET i), "
        "SQRT((SELECT SUM(x * x) FROM UNNEST(c.centroid) x)) * SQRT((SELECT SUM(y * y) FROM UNNEST(a.centroid) y))"
        ") AS distance "
        "FROM c CROSS JOIN a WHERE ARRAY_LENGTH(c.centroid) = ARRAY_LENGTH(a.centroid)) "
        # A zero-length vector has no direction: SAFE_DIVIDE gives NULL, which would otherwise sort first.
        "SELECT s.item_id, s.kind, s.label, s.distance FROM s WHERE s.distance IS NOT NULL "
        "ORDER BY s.distance, s.item_id LIMIT @k"
    )
    return sql_query(ctx, warehouse, sql, purpose=f"analogues: nearest centroids to {item_id}",
                     params={"item_id": item_id, "k": k})


def _has_centroid(ctx: RunContext, warehouse: Warehouse, item_id: str) -> dict:
    sql = (f"WITH m AS ({_open_map('m.centroid')}) SELECT COUNT(*) AS n FROM m "
           "WHERE m.item_id = @item_id AND ARRAY_LENGTH(m.centroid) > 0")
    return sql_query(ctx, warehouse, sql, purpose=f"analogues: does {item_id} have a stored centroid",
                     params={"item_id": item_id})


def _shared_words(ctx: RunContext, warehouse: Warehouse, item_id: str | None, text: str | None, k: int) -> dict:
    """Open map items ranked by how many whole words of text (or of item_id's label) their label shares."""
    params = {"k": k}
    if text:
        params["text"] = text
        phrase = "SELECT @text AS phrase"
    else:
        phrase = "SELECT m.label AS phrase FROM m WHERE m.item_id = @item_id"
    where = "WHERE m.kind != 'creator'"
    if item_id:
        params["item_id"] = item_id
        where += " AND m.item_id != @item_id"
    sql = (
        f"WITH m AS ({_open_map('m.kind, m.label')}), p AS ({phrase}), "
        f"t AS (SELECT DISTINCT word FROM p, UNNEST(REGEXP_EXTRACT_ALL(LOWER(IFNULL(p.phrase, '')), r'{WORDS}')) word "
        f"WHERE CHAR_LENGTH(word) >= {MIN_WORD}), "
        "s AS (SELECT m.item_id, m.kind, m.label, "
        f"(SELECT COUNT(DISTINCT cw) FROM UNNEST(REGEXP_EXTRACT_ALL(LOWER(IFNULL(m.label, '')), r'{WORDS}')) cw "
        f"WHERE cw IN (SELECT t.word FROM t)) AS shared_words FROM m {where}) "
        "SELECT s.item_id, s.kind, s.label, s.shared_words FROM s WHERE s.shared_words > 0 "
        "ORDER BY s.shared_words DESC, s.item_id LIMIT @k"
    )
    return sql_query(ctx, warehouse, sql, purpose=f"analogues: map items sharing label words with "
                                                  f"{text or item_id}", params=params)


def analogues(ctx: RunContext, warehouse: Warehouse, item_id=None, text=None, market=None, k=5) -> dict:
    """Up to k cultural map items nearest to item_id (or to the map item text best names), each with its past waves in
    market. method is centroid, or keyword when there is no centroid match, with centroid_status and a note saying
    why. Creator items are always left out (CREATORS_NOTE)."""
    item_id, text = _text(item_id), _text(text)
    if not item_id and not text:
        raise Refused("Give an item_id or a text.")
    market = _market(ctx, market)
    try:
        k = int(k)
    except (TypeError, ValueError):
        raise Refused(f"k must be a whole number; got {k!r}.") from None
    if k < 1:
        raise Refused("k must be at least 1.")
    k = min(k, MAX_K)

    out, query_ids, results = {}, [], []
    if not item_id:
        found = _find_items(ctx, warehouse, text, 1)
        query_ids.append(found["query_id"])
        item_id = found["rows"][0]["item_id"] if found["rows"] else None
        if found["rows"]:
            out.update(label=found["rows"][0]["label"], kind=found["rows"][0]["kind"])
    out["item_id"] = item_id

    nearest, status = None, None
    if item_id:
        try:
            nearest = _nearest(ctx, warehouse, item_id, k)
        except Exception as e:  # no centroid column yet, or the byte cap: shared label words still answer
            status = "centroid query failed"
            out["centroid_reason"] = _reason(e)
        else:
            query_ids.append(nearest["query_id"])
            if not nearest["rows"]:
                nearest = None
                has = _has_centroid(ctx, warehouse, item_id)
                query_ids.append(has["query_id"])
                status = "no centroid rows" if has["rows"] and has["rows"][0]["n"] else "no stored centroid"
    if nearest is not None:
        out["method"] = "centroid"
    else:
        nearest = _shared_words(ctx, warehouse, item_id, text, k)
        query_ids.append(nearest["query_id"])
        if item_id:
            out.update(method="keyword", centroid_status=status, note=CENTROID_NOTES[status])
        else:
            out.update(method="keyword", note=NO_ITEM_NOTE)
    results.append(nearest)
    out["creators_note"] = CREATORS_NOTE

    ranked = _pinned(nearest)
    if ranked:
        waves = _waves(ctx, warehouse, [row["item_id"] for row in ranked], market)
        query_ids.append(waves["query_id"])
        results.append(waves)
        by_item = {}
        for wave in _pinned(waves):
            by_item.setdefault(wave["item_id"], []).append(wave)
        for row in ranked:
            row["waves"] = by_item.get(row["item_id"], [])
    _mark(ctx, query_ids, "analogues")
    _cut(out, results)
    return {**out, "analogues": ranked, "query_id": nearest["query_id"], "query_ids": query_ids}
