"""Read statements and row admission over the pinned bridge v3 clone tables only.

Every statement names one clone table the pinned registry row derives for its cutoff, and
the loaded relation binding must name that same table; no live table is ever read. History
reads select only the (market, date) cells the completion set admitted and the loader
checked against native evidence. Every admitted row keeps its provenance, and every cell
in the window that holds no admitted outcome is a named limitation with its reason code.
"""

import copy
import re
from datetime import UTC, date, datetime, time, timedelta

from .brain_contract import canonical_digest
from .general_question_context_admission import LoadedBridgeSource
from .production_snapshot_tables import PARTITIONS
from .protected_context_registry import bridge_snapshot_tables
from .source_estate_bridge import COLLECTION_TABLES, HISTORY_TABLES, LANES
from .source_estate_bridge_evidence import RECORDED_HISTORY_LANES

_TABLE = re.compile(r"[a-z][a-z0-9-]*\.[a-z0-9_]+\.[a-z0-9_]+")
OUTSIDE_COMPLETION_SET = "outside_history_completion_set"


def _require(condition):
    if not condition:
        raise ValueError("bridge_query_invalid")


def partition_column(lane):
    _require(lane in LANES)
    return PARTITIONS.get(lane, "trend_date")


def _day(value):
    _require(type(value) is str)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("bridge_query_invalid") from error
    _require(parsed.isoformat() == value)
    return parsed


def _window(loaded, window_start, window_end):
    _require(type(loaded) is LoadedBridgeSource)
    start, end = _day(window_start), _day(window_end)
    _require(start <= end <= _day(loaded.registry_entry["cutoff_date"]))
    return start, end


def _table(loaded, lane):
    """The clone table for ``lane``: derived from the pinned row, and named by the binding."""
    cutoff = _day(loaded.registry_entry["cutoff_date"])
    expected = bridge_snapshot_tables(cutoff)[LANES.index(lane)]
    named = [
        relation["destination_table"]
        for source in (loaded.binding, loaded.profile)
        for relation in source["relation_bindings"]
        if relation["lane"] == lane
    ]
    _require(named == [expected, expected] and _TABLE.fullmatch(expected) is not None)
    return expected


def _strings(name, values):
    return {
        "name": name,
        "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
        "parameterValue": {"arrayValues": [{"value": value} for value in values]},
    }


def _timestamp(name, value):
    return {
        "name": name,
        "parameterType": {"type": "TIMESTAMP"},
        "parameterValue": {"value": value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
    }


def _admitted(loaded, lane, start=None, end=None):
    return [
        cell
        for cell in loaded.cells
        if cell["lane"] == lane
        and cell["state"] == "completed"
        and (start is None or start <= _day(cell["product_date"]) <= end)
    ]


def bridge_history_query(loaded, lane, *, window_start, window_end):
    """The read of one recorded history lane's clone, or None when no cell is admitted."""
    start, end = _window(loaded, window_start, window_end)
    _require(lane in RECORDED_HISTORY_LANES)
    table = _table(loaded, lane)
    column = partition_column(lane)
    keys = sorted(
        f"{cell['market']}|{cell['product_date']}" for cell in _admitted(loaded, lane, start, end)
    )
    if not keys:
        return None
    sql = (
        f"SELECT * FROM `{table}`\n"
        "WHERE market IN UNNEST(@markets)\n"
        f"  AND CONCAT(market, '|', CAST({column} AS STRING)) IN UNNEST(@cell_keys)\n"
        f"ORDER BY market, {column}"
    )
    return {
        "sql": sql,
        "parameters": [
            _strings("markets", list(loaded.binding["market_scope"])),
            _strings("cell_keys", keys),
        ],
    }


def bridge_collection_query(loaded, lane, *, window_start, window_end):
    """The read of one collection lane's clone, inside the closed observation window."""
    start, end = _window(loaded, window_start, window_end)
    _require(lane in COLLECTION_TABLES)
    table = _table(loaded, lane)
    lower, upper = _collection_bounds(loaded, start, end)
    parameters = [
        _strings("markets", list(loaded.binding["market_scope"])),
        _timestamp("window_start", lower),
        _timestamp("window_end", upper),
    ]
    # A ledger capture reads only the rows its bound funded executions wrote, so rows other
    # runs wrote into the cloned table never take a place under the read's row cap.
    runs = ""
    if loaded.collection_row_runs is not None:
        _require(bool(loaded.collection_row_runs))
        runs = "  AND pipeline_run_id IN UNNEST(@run_ids)\n"
        parameters.append(_strings("run_ids", list(loaded.collection_row_runs)))
    sql = (
        f"SELECT * FROM `{table}`\n"
        "WHERE market IN UNNEST(@markets)\n"
        "  AND collected_at >= @window_start AND collected_at < @window_end\n"
        f"{runs}"
        "ORDER BY market, collected_at"
    )
    return {"sql": sql, "parameters": parameters}


def _collection_bounds(loaded, start, end):
    closed = datetime.fromisoformat(
        loaded.profile["observation_window_end"].replace("Z", "+00:00")
    ).astimezone(UTC)
    lower = datetime.combine(start, time.min, UTC)
    upper = min(datetime.combine(end + timedelta(days=1), time.min, UTC), closed)
    return lower, upper


def _base_provenance(loaded):
    return {
        "registry_entry_digest": canonical_digest(loaded.registry_entry),
        "result_id": loaded.binding["result_id"],
        "snapshot_digest": loaded.binding["snapshot_digest"],
    }


def admit_history_rows(loaded, lane, rows, *, window_start, window_end):
    """Attach each row to its admitted cell inside the read window.

    Only the cells the window read selects can hold a row, so a row for an admitted cell
    outside the window refuses exactly as a row outside every admitted cell does.
    """
    start, end = _window(loaded, window_start, window_end)
    _require(lane in RECORDED_HISTORY_LANES)
    _table(loaded, lane)
    _require(type(rows) is list)
    column = partition_column(lane)
    cells = {
        (cell["market"], cell["product_date"]): cell for cell in _admitted(loaded, lane, start, end)
    }
    counts = {}
    admitted = []
    for row in rows:
        _require(type(row) is dict)
        market, day = row.get("market"), row.get(column)
        _require(type(market) is str and type(day) is str)
        cell = cells.get((market, _day(day).isoformat()))
        _require(cell is not None)
        counts[(market, day)] = counts.get((market, day), 0) + 1
        _require(counts[(market, day)] <= cell["row_count"])
        admitted.append(
            {
                "row": copy.deepcopy(row),
                "provenance": {
                    **_base_provenance(loaded),
                    "completion_entry_digest": cell["completion_entry_digest"],
                    "available_at": cell["available_at"],
                },
            }
        )
    return admitted


def admit_collection_rows(loaded, lane, rows, *, window_start, window_end):
    """Attach each collection row to the capture; a row outside the read window refuses."""
    start, end = _window(loaded, window_start, window_end)
    _require(lane in COLLECTION_TABLES)
    _table(loaded, lane)
    _require(type(rows) is list)
    lower, upper = _collection_bounds(loaded, start, end)
    markets = set(loaded.binding["market_scope"])
    admitted = []
    for row in rows:
        _require(type(row) is dict and row.get("market") in markets)
        value = row.get("collected_at")
        _require(type(value) is str)
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("bridge_query_invalid") from error
        _require(instant.tzinfo is not None and instant.utcoffset() is not None)
        _require(lower <= instant.astimezone(UTC) < upper)
        admitted.append(
            {
                "row": copy.deepcopy(row),
                "provenance": {
                    **_base_provenance(loaded),
                    "collection_receipt_set_digest": loaded.binding[
                        "collection_receipt_set_digest"
                    ],
                    "available_at": loaded.binding["available_at"],
                },
            }
        )
    return admitted


def bridge_limitations(loaded, *, window_start, window_end):
    """Every history cell in the window and read scope that holds no admitted outcome."""
    start, end = _window(loaded, window_start, window_end)
    known = {
        (item["lane"], item["market"], item["product_date"])
        for item in (*loaded.cells, *loaded.limitations)
    }
    result = [
        copy.deepcopy(item)
        for item in loaded.limitations
        if start <= _day(item["product_date"]) <= end
    ]
    span = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    for lane in HISTORY_TABLES:
        for market in loaded.binding["market_scope"]:
            for day in span:
                key = (lane, market, day.isoformat())
                if key not in known:
                    result.append(
                        {
                            "lane": lane,
                            "market": market,
                            "product_date": day.isoformat(),
                            "reason_code": OUTSIDE_COMPLETION_SET,
                        }
                    )
    return sorted(result, key=lambda item: (item["lane"], item["market"], item["product_date"]))
