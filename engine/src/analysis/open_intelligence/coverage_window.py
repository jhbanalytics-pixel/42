"""Native collection window rows and the retained window record.

The collection window query computes every total in SQL, one row per market
and route over the union of the routes the window must account for and the
routes observed in it. A route is the pipeline connector key: raw rows carry
feed names, domains and channel titles as their source, so the query resolves
each row's route from a declared source map, then a declared platform map,
and keeps every row neither resolves under the route ``unresolved``. Errors
the pipeline recorded against the market rather than a connector key land on
the market level route ``pipeline``, which carries failures and never rows. ``validate_window_rows`` refuses rows whose columns,
counts, identity arithmetic or control states cannot hold, and
``window_readback`` binds the validated rows to the window, the strata, the
limit and the job that produced them. A fixture readback says so, and nothing
downstream may treat it as a native read.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

COLLECTION_WINDOW_QUERY_PATH = str(
    Path(__file__).resolve().parents[3]
    / "infra"
    / "bigquery_queries"
    / "open_intelligence_coverage_collection_window_v1.sql"
)
COLLECTION_WINDOW_PARAMETERS = (
    "window_start",
    "window_end",
    "market_strata",
    "source_routes",
    "platform_routes",
    "expected_sources",
    "known_quiet",
    "query_limit",
)
COLLECTION_WINDOW_ASSERTS = (
    "window_is_ordered",
    "market_strata_are_declared",
    "query_limit_is_positive",
    "route_maps_are_unambiguous",
    "declared_routes_are_named",
    "declared_routes_are_connector_keys",
    "known_quiet_routes_are_expected",
    "expected_markets_are_declared_strata",
    "every_stratum_has_an_expected_route",
    "every_market_is_a_declared_stratum",
    "every_pipeline_market_is_a_declared_stratum",
    "output_fits_the_query_limit",
)
COLLECTION_WINDOW_COLUMNS = (
    "market",
    "route",
    "expected",
    "physical_rows",
    "distinct_native_observations",
    "inferred_identity_observations",
    "rows_without_identity",
    "qualified_observations",
    "classified_observations",
    "unique_qualified_observations",
    "reassigned_market_rows",
    "rows_with_geo_method",
    "newest_published_at",
    "newest_collected_at",
    "rows_without_published_at",
    "recorded_failures",
    "pipeline_reported_rows",
    "pipeline_route_rows",
    "control_state",
    "control_reason",
    "unresolved_source_values",
)
UNRESOLVED_ROUTE = "unresolved"
# The market level route: errors recorded against the pipeline itself, or with
# no connector key, are counted here and never dropped.
PIPELINE_ROUTE = "pipeline"
RESERVED_ROUTES = frozenset({UNRESOLVED_ROUTE, PIPELINE_ROUTE})
# In the order the rules apply.
CONTROL_STATES = (
    "reader_mismatch",
    "unresolved_route",
    "emitting",
    "failed",
    "known_quiet",
    "unexplained_zero",
)
CONTROL_REASONS = {
    "reader_mismatch": "pipeline_rows_not_readable",
    "unresolved_route": "route_not_resolved",
    "emitting": None,
    "failed": "recorded_failure",
    "known_quiet": "declared_quiet",
    "unexplained_zero": "zero_without_decision_record",
}
# The pipeline counted rows for this very route, yet none are readable in
# raw_content: a reader mismatch on the route rather than the whole market.
ROUTE_READER_MISMATCH_REASON = "pipeline_route_rows_not_readable"
_STATE_REASONS = {state: frozenset({reason}) for state, reason in CONTROL_REASONS.items()} | {
    "reader_mismatch": frozenset({CONTROL_REASONS["reader_mismatch"], ROUTE_READER_MISMATCH_REASON})
}
CONNECTOR_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")
# States in which a zero may hide something the window cannot explain: rows
# with no resolvable route may belong to a route that reads as zero.
HIDING_STATES = frozenset({"reader_mismatch", "unresolved_route", "unexplained_zero"})
_COUNT_COLUMNS = (
    "physical_rows",
    "distinct_native_observations",
    "inferred_identity_observations",
    "rows_without_identity",
    "qualified_observations",
    "classified_observations",
    "unique_qualified_observations",
    "reassigned_market_rows",
    "rows_with_geo_method",
    "rows_without_published_at",
    "recorded_failures",
    "pipeline_reported_rows",
)
_TIMESTAMP_COLUMNS = ("newest_published_at", "newest_collected_at")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def control_state_for(
    *,
    physical_rows: int,
    recorded_failures: int,
    declared_quiet: bool,
    unresolved: bool,
    market_physical_rows: int,
    market_pipeline_rows: int,
    pipeline_route_rows: int | None = None,
) -> tuple[str, str | None]:
    """Return the control state and reason the SQL assigns to one route.

    ``pipeline_route_rows`` is what pipeline_runs counted for this route (None
    where the connector has no row column); a positive count with no raw rows
    is a reader mismatch on the route, ahead of failed and known quiet.
    """
    if market_pipeline_rows > 0 and market_physical_rows == 0:
        state = "reader_mismatch"
    elif unresolved:
        state = "unresolved_route"
    elif pipeline_route_rows and physical_rows == 0:
        return "reader_mismatch", ROUTE_READER_MISMATCH_REASON
    elif physical_rows > 0:
        state = "emitting"
    elif recorded_failures > 0:
        state = "failed"
    elif declared_quiet:
        state = "known_quiet"
    else:
        state = "unexplained_zero"
    return state, CONTROL_REASONS[state]


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"window_field_invalid:{field}")
    return value


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"window_count_invalid:{field}")
    return value


def _timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"window_timestamp_invalid:{field}") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"window_timestamp_invalid:{field}")
    return value.isoformat()


def _source_values(value: object, *, unresolved: bool) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("unresolved_source_values_invalid")
    values = list(value)
    if unresolved != bool(values) or len(set(values)) != len(values):
        raise ValueError("unresolved_source_values_invalid")
    if any(not isinstance(item, str) for item in values):
        raise ValueError("unresolved_source_values_invalid")
    return values


def _row(raw: object) -> dict:
    if not isinstance(raw, Mapping) or set(raw) != set(COLLECTION_WINDOW_COLUMNS):
        raise ValueError("window_row_columns_invalid")
    row = {
        "market": _text(raw["market"], "market"),
        "route": _text(raw["route"], "route"),
        "expected": raw["expected"],
    }
    if not isinstance(row["expected"], bool):
        raise ValueError("window_field_invalid:expected")
    for column in _COUNT_COLUMNS:
        row[column] = _count(raw[column], column)
    for column in _TIMESTAMP_COLUMNS:
        row[column] = _timestamp(raw[column], column)
    route_rows = raw["pipeline_route_rows"]
    row["pipeline_route_rows"] = (
        None if route_rows is None else _count(route_rows, "pipeline_route_rows")
    )
    state = raw["control_state"]
    if state not in CONTROL_STATES:
        raise ValueError("control_state_invalid")
    row["control_state"] = state
    row["control_reason"] = raw["control_reason"]
    if row["control_reason"] not in _STATE_REASONS[state]:
        raise ValueError("control_reason_invalid")
    unresolved = row["route"] == UNRESOLVED_ROUTE
    if unresolved != (state == "unresolved_route"):
        raise ValueError("control_state_inconsistent")
    # The unresolved route exists only for rows the window holds, and is never
    # a route anyone can expect.
    if unresolved and (
        row["expected"] or row["physical_rows"] == 0 or row["pipeline_route_rows"] is not None
    ):
        raise ValueError("unresolved_route_invalid")
    # The market level route holds recorded failures only: no rows, no
    # identities, never expected.
    if row["route"] == PIPELINE_ROUTE and (
        row["expected"]
        or row["recorded_failures"] == 0
        or row["pipeline_route_rows"] is not None
        or any(
            row[column]
            for column in _COUNT_COLUMNS
            if column not in ("recorded_failures", "pipeline_reported_rows")
        )
    ):
        raise ValueError("pipeline_route_invalid")
    row["unresolved_source_values"] = _source_values(
        raw["unresolved_source_values"], unresolved=unresolved
    )
    for column in ("classified_observations", "unique_qualified_observations"):
        if row[column] > row["qualified_observations"]:
            raise ValueError(f"identity_arithmetic_invalid:{column}")
    # Every distinct identity needs at least one row of its own, and rows with
    # no identity are counted apart from both identity kinds.
    identities = (
        row["distinct_native_observations"]
        + row["inferred_identity_observations"]
        + row["rows_without_identity"]
    )
    if row["physical_rows"] < identities:
        raise ValueError("identity_arithmetic_invalid:physical_rows")
    for column in ("rows_with_geo_method", "rows_without_published_at"):
        if row[column] > row["physical_rows"]:
            raise ValueError(f"identity_arithmetic_invalid:{column}")
    if state == "known_quiet" and not row["expected"]:
        raise ValueError("known_quiet_not_expected")
    return {column: row[column] for column in COLLECTION_WINDOW_COLUMNS}


def validate_window_rows(rows: Iterable[Mapping]) -> tuple[dict, ...]:
    """Validate collection window rows and return them as plain records.

    Timestamps come back as ISO 8601 text with an offset. The control state of
    each row is checked against its own numbers and its market's totals: a
    state the numbers decide exactly must match, and a zero without a failure
    may only be known quiet (on an expected route) or unexplained.
    """
    validated = tuple(_row(raw) for raw in rows)
    seen: set[tuple[str, str]] = set()
    route_rows: dict[str, int] = {}
    market_rows: dict[str, int] = {}
    market_pipeline: dict[str, int] = {}
    for row in validated:
        key = (row["market"], row["route"])
        if key in seen:
            raise ValueError("window_row_duplicate")
        seen.add(key)
        market = row["market"]
        market_rows[market] = market_rows.get(market, 0) + row["physical_rows"]
        route_rows[row["route"]] = route_rows.get(row["route"], 0) + row["physical_rows"]
        reported = market_pipeline.setdefault(market, row["pipeline_reported_rows"])
        if reported != row["pipeline_reported_rows"]:
            raise ValueError("pipeline_reported_rows_inconsistent")
    for row in validated:
        # A reassigned row is an enriched row of this market whose raw row, by
        # the same id, was collected under another market; its route resolves
        # from the same source and platform on both rows. So the reassigned
        # rows of a market and route cannot outnumber the raw rows the other
        # markets collected on that route. They can outnumber this market's
        # own raw rows, since rows moved in are not among them, so no same
        # market bound holds here; the matrix keeps leakage within 0 and 1.
        elsewhere = route_rows[row["route"]] - row["physical_rows"]
        if row["reassigned_market_rows"] > elsewhere:
            raise ValueError("identity_arithmetic_invalid:reassigned_market_rows")
        derived = control_state_for(
            physical_rows=row["physical_rows"],
            recorded_failures=row["recorded_failures"],
            declared_quiet=row["control_state"] == "known_quiet",
            unresolved=row["route"] == UNRESOLVED_ROUTE,
            market_physical_rows=market_rows[row["market"]],
            market_pipeline_rows=market_pipeline[row["market"]],
            pipeline_route_rows=row["pipeline_route_rows"],
        )
        if derived != (row["control_state"], row["control_reason"]):
            raise ValueError("control_state_inconsistent")
    return validated


def _window(window: object) -> tuple[datetime, datetime]:
    try:
        start, end = window
    except (TypeError, ValueError):
        raise ValueError("window_invalid") from None
    for value in (start, end):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("window_invalid")
    if not start < end:
        raise ValueError("window_invalid")
    return start, end


def _strata(market_strata: object) -> tuple[str, ...]:
    if isinstance(market_strata, str):
        raise ValueError("market_strata_invalid")
    try:
        strata = tuple(market_strata)
    except TypeError:
        raise ValueError("market_strata_invalid") from None
    if not strata or len(set(strata)) != len(strata):
        raise ValueError("market_strata_invalid")
    for market in strata:
        if not isinstance(market, str) or not market or market != market.strip():
            raise ValueError("market_strata_invalid")
    return strata


def _source(job_id: object, output_sha256: object) -> dict:
    if job_id is None:
        if output_sha256 is not None:
            raise ValueError("fixture_carries_no_digest")
        return {"kind": "fixture"}
    if not isinstance(job_id, str) or not job_id or job_id != job_id.strip():
        raise ValueError("job_id_invalid")
    if not isinstance(output_sha256, str) or _DIGEST.fullmatch(output_sha256) is None:
        raise ValueError("output_sha256_invalid")
    return {"kind": "native_bigquery", "job_id": job_id, "output_sha256": output_sha256}


def _declared_routes(value: object, strata: tuple[str, ...], code: str) -> list[list[str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(code)
    pairs: list[list[str]] = []
    for item in value:
        if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(code)
        market, route = item
        if market not in strata or not isinstance(route, str) or not route:
            raise ValueError(code)
        if route in RESERVED_ROUTES or CONNECTOR_KEY.match(route) is None:
            raise ValueError(code)
        pairs.append([market, route])
    if len({tuple(pair) for pair in pairs}) != len(pairs):
        raise ValueError(code)
    return pairs


def check_declared_routes(
    validated: Iterable[Mapping],
    strata: Iterable[str],
    expected_sources: object,
    known_quiet: object,
) -> tuple[list[list[str]], list[list[str]]]:
    """Check declared expected and known quiet routes against validated rows.

    Every expected route has its row and no other row claims to be expected;
    exactly the declared quiet routes that recorded nothing read as known
    quiet. Returns both declarations as ``[market, route]`` lists.
    """
    strata = tuple(strata)
    rows = tuple(validated)
    expected = _declared_routes(expected_sources, strata, "expected_sources_invalid")
    quiet = _declared_routes(known_quiet, strata, "known_quiet_invalid")
    expected_keys = {tuple(pair) for pair in expected}
    if any(tuple(pair) not in expected_keys for pair in quiet):
        raise ValueError("known_quiet_not_expected")
    if {(r["market"], r["route"]) for r in rows if r["expected"]} != expected_keys:
        raise ValueError("expected_routes_mismatch")
    quiet_keys = {tuple(pair) for pair in quiet}
    for row in rows:
        key = (row["market"], row["route"])
        if (row["control_state"] == "known_quiet") != (
            key in quiet_keys and row["control_state"] in ("known_quiet", "unexplained_zero")
        ):
            raise ValueError("known_quiet_mismatch")
    return expected, quiet


def _route_pairs(value: object, name: str) -> list[list[str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"route_maps_invalid:{name}")
    pairs: list[list[str]] = []
    for item in value:
        if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)):
            raise ValueError(f"route_maps_invalid:{name}")
        if len(item) != 2 or any(not isinstance(part, str) or not part for part in item):
            raise ValueError(f"route_maps_invalid:{name}")
        if item[1] in RESERVED_ROUTES:
            raise ValueError(f"route_maps_invalid:{name}")
        pairs.append([item[0], item[1]])
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError(f"route_maps_invalid:{name}")
    return pairs


def _route_maps(route_maps: object) -> dict | None:
    if route_maps is None:
        return None
    if not isinstance(route_maps, Mapping) or set(route_maps) != {
        "source_routes",
        "platform_routes",
    }:
        raise ValueError("route_maps_invalid")
    return {
        name: _route_pairs(route_maps[name], name) for name in ("source_routes", "platform_routes")
    }


def window_readback(
    rows: Iterable[Mapping],
    *,
    window: tuple[datetime, datetime],
    market_strata: Iterable[str],
    expected_sources: Iterable,
    known_quiet: Iterable,
    query_limit: int,
    job_id: str | None,
    output_sha256: str | None,
    route_maps: Mapping | None = None,
) -> dict:
    """Bind validated window rows to the read that produced them.

    ``job_id`` None marks a fixture; a native read names its job and the
    sha256 of the raw rows it saved. ``expected_sources`` and ``known_quiet``
    are the ``(market, route)`` pairs the read declared; they are retained as
    declared and must match the rows: every expected route has its row, no
    other row claims to be expected, and exactly the declared quiet routes
    that recorded nothing read as known quiet. ``route_maps`` records the source and
    platform maps the read resolved routes with, as ``[key, route]`` pairs.
    ``zero_hides_nothing`` is true only when no route is an unexplained zero,
    a reader mismatch or the unresolved route.
    """
    start, end = _window(window)
    strata = _strata(market_strata)
    if isinstance(query_limit, bool) or not isinstance(query_limit, int) or query_limit <= 0:
        raise ValueError("query_limit_invalid")
    source = _source(job_id, output_sha256)
    maps = _route_maps(route_maps)
    validated = validate_window_rows(rows)
    if len(validated) > query_limit:
        raise ValueError("window_rows_exceed_limit")
    if any(row["market"] not in strata for row in validated):
        raise ValueError("window_market_not_a_stratum")
    expected, quiet = check_declared_routes(validated, strata, expected_sources, known_quiet)
    controls = {market: dict.fromkeys(CONTROL_STATES, 0) for market in strata}
    for row in validated:
        if row["market"] not in controls:
            raise ValueError("window_market_not_a_stratum")
        controls[row["market"]][row["control_state"]] += 1
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "market_strata": list(strata),
        "expected_sources": expected,
        "known_quiet": quiet,
        "query_limit": query_limit,
        "source": source,
        "route_maps": maps,
        "rows": [dict(row) for row in validated],
        "controls": controls,
        "zero_hides_nothing": not any(row["control_state"] in HIDING_STATES for row in validated),
    }
