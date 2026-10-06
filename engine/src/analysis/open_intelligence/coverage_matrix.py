"""Product family coverage matrix evaluated over one retained collection window readback.

``evaluate_coverage_matrix`` reads the window record the collection window reader
retains (window, market strata, query limit, one row per market and route, where
a route is a pipeline connector key, the source kind and job, and a per market
control summary) and states, per market
and per product family, whether the family is covered, thin, stale, leaking,
missing or unmeasured. Unique contribution is read from the query column
``unique_qualified_observations``; it is never recomputed here, and topic group
classification never stands in for breadth. Rows are validated by the same
row validator the window reader uses, so the matrix never accepts a row the
window contract refuses. A fixture readback can never close live family
acceptance, and neither can code coverage, a zero that may hide something, or
foreign market leakage the writer cannot show.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from src.analysis.open_intelligence.coverage_window import (
    COLLECTION_WINDOW_COLUMNS,
    CONTROL_REASONS,
    CONTROL_STATES,
    HIDING_STATES,
    PIPELINE_ROUTE,
    RESERVED_ROUTES,
    UNRESOLVED_ROUTE,
    check_declared_routes,
    validate_window_rows,
)

PRODUCT_FAMILIES_PATH = (
    Path(__file__).resolve().parents[3] / "configs" / "coverage_product_families.json"
)
FAMILIES = (
    "news",
    "search",
    "social_conversation",
    "comments",
    "music_sounds",
    "apps",
    "creators",
    "events",
    "phrases",
    "unseeded_discovery",
)
FAMILY_STATES = ("covered", "thin", "stale", "leaking", "missing", "unmeasured")
NOT_IN_COLLECTION_WINDOW = "not_in_collection_window"
READBACK_ROW_COLUMNS = COLLECTION_WINDOW_COLUMNS
READBACK_FIELDS = (
    "window",
    "market_strata",
    "query_limit",
    "rows",
    "source",
    "controls",
    "zero_hides_nothing",
    "expected_sources",
    "known_quiet",
)
# Leakage is a share of this market's own raw rows and must stay within 0 and
# 1. Rows moved in from other markets are not among those raw rows, so they
# can outnumber them; that leak is certain yet has no share to report.
LEAKAGE_EXCEEDS_ROWS = "reassigned_rows_exceed_market_rows"
# The run writes each enriched row from its raw row under the same market
# (scripts/run_rss_now.py builds the enriched rows as ``{**base, ...}``), so a
# zero reassigned count on this writer is not evidence of no leakage.
LEAKAGE_NOT_SHOWN = "market_never_reassigned_by_writer"
# Window row refusals translated to the matrix's own refusal codes.
_ROW_REFUSALS = {
    "window_row_columns_invalid": "readback_row_columns_mismatch",
    "control_state_invalid": "readback_control_state_unknown",
    "window_field_invalid:expected": "readback_expected_invalid",
    "window_field_invalid:route": "readback_route_invalid",
    "window_row_duplicate": "readback_duplicate_route",
    "identity_arithmetic_invalid:classified_observations": "readback_identity_arithmetic",
    "identity_arithmetic_invalid:unique_qualified_observations": "readback_identity_arithmetic",
}
_ROW_REFUSAL_PREFIXES = {
    "window_count_invalid:": "readback_count_invalid",
    "window_timestamp_invalid:": "readback_timestamp_invalid",
}
_UNREADABLE_STATES = frozenset({"unexplained_zero", "reader_mismatch"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_KINDS = frozenset({"native_bigquery", "fixture"})


def load_product_families(path: Path = PRODUCT_FAMILIES_PATH) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_product_families(config)
    return config


def validate_product_families(config) -> None:
    """Refuse a config that does not place each route in at most one family or list."""
    if not isinstance(config, Mapping) or set(config) != {"families", "unassigned_routes"}:
        raise ValueError("product_families_config_invalid")
    families, unassigned = config["families"], config["unassigned_routes"]
    if not isinstance(families, Mapping) or set(families) != set(FAMILIES):
        raise ValueError("families_mismatch")
    if not isinstance(unassigned, Mapping):
        raise ValueError("product_families_config_invalid")
    owner: dict[str, str] = {}
    for name in FAMILIES:
        entry = families[name]
        if not isinstance(entry, Mapping):
            raise ValueError(f"family_entry_invalid:{name}")
        if set(entry) == {"routes"}:
            routes = entry["routes"]
            if (
                not isinstance(routes, list)
                or not routes
                or any(not isinstance(r, str) or not r for r in routes)
                or len(set(routes)) != len(routes)
            ):
                raise ValueError(f"family_routes_invalid:{name}")
            for route in routes:
                if route in RESERVED_ROUTES:
                    raise ValueError(f"route_reserved:{route}")
                if route in owner:
                    raise ValueError(f"family_route_shared:{route}")
                owner[route] = name
        elif set(entry) == {"measured_by", "reason"}:
            if entry["measured_by"] != NOT_IN_COLLECTION_WINDOW:
                raise ValueError(f"family_entry_invalid:{name}")
            if not isinstance(entry["reason"], str) or not entry["reason"].strip():
                raise ValueError(f"family_entry_invalid:{name}")
        else:
            raise ValueError(f"family_entry_invalid:{name}")
    for route, reason in unassigned.items():
        if route in RESERVED_ROUTES:
            raise ValueError(f"route_reserved:{route}")
        if not isinstance(route, str) or not route:
            raise ValueError("product_families_config_invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"unassigned_route_invalid:{route}")
        if route in owner:
            raise ValueError(f"family_route_unassigned_overlap:{route}")


def _timestamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("readback_timestamp_invalid") from None
    else:
        raise ValueError("readback_timestamp_invalid")
    if parsed.tzinfo is None:
        raise ValueError("readback_timestamp_invalid")
    return parsed


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _window_rows(rows) -> tuple[dict, ...]:
    """Validate rows with the window reader's own row validator."""
    for row in rows:
        if (
            isinstance(row, Mapping)
            and {"route", "control_state"} <= set(row)
            and (row["route"] == UNRESOLVED_ROUTE) != (row["control_state"] == "unresolved_route")
        ):
            raise ValueError("readback_unresolved_route_invalid")
    try:
        return validate_window_rows(rows)
    except ValueError as error:
        code = str(error)
        if code in _ROW_REFUSALS:
            raise ValueError(_ROW_REFUSALS[code]) from None
        for prefix, translated in _ROW_REFUSAL_PREFIXES.items():
            if code.startswith(prefix):
                raise ValueError(translated) from None
        raise ValueError(f"readback_rows_invalid:{code}") from None


def _native_rows_match(readback_rows, rows_bytes, digest) -> None:
    """Refuse a native readback its saved raw rows do not reproduce.

    ``rows_bytes`` are the raw rows file exactly as the window reader saved it.
    Their sha256 must equal the readback's ``output_sha256``, and the rows they
    hold, validated by the window reader's own row validator, must equal the
    readback's rows. A job id and a well formed digest alone prove nothing.
    """
    if not isinstance(rows_bytes, (bytes, bytearray)):
        raise ValueError("readback_native_rows_missing")
    if hashlib.sha256(rows_bytes).hexdigest() != digest:
        raise ValueError("readback_native_digest_mismatch")
    try:
        raw = json.loads(rows_bytes)
        if not isinstance(raw, list):
            raise ValueError("rows_not_list")
        saved = validate_window_rows(raw)
    except ValueError:
        raise ValueError("readback_native_rows_mismatch") from None
    if [dict(row) for row in saved] != [dict(row) for row in readback_rows]:
        raise ValueError("readback_native_rows_mismatch")


def validate_readback(readback, *, rows_bytes=None) -> dict:
    """Refuse a readback whose fields the matrix depends on are absent or inconsistent.

    A native readback also needs ``rows_bytes``, the raw rows file the reader
    saved beside it; its digest and rows are checked last, after every other
    field.
    """
    if not isinstance(readback, Mapping):
        raise ValueError("readback_not_mapping")
    for field in READBACK_FIELDS:
        if field not in readback:
            raise ValueError(f"readback_field_missing:{field}")
    window = readback["window"]
    if not isinstance(window, Mapping) or not {"start", "end"} <= set(window):
        raise ValueError("readback_window_invalid")
    try:
        start, end = _timestamp(window["start"]), _timestamp(window["end"])
    except ValueError:
        raise ValueError("readback_window_invalid") from None
    if start is None or end is None or not start < end:
        raise ValueError("readback_window_invalid")
    strata = readback["market_strata"]
    if (
        not isinstance(strata, (list, tuple))
        or not strata
        or any(not isinstance(m, str) or not m for m in strata)
        or len(set(strata)) != len(strata)
    ):
        raise ValueError("readback_strata_invalid")
    limit = readback["query_limit"]
    if not _count(limit) or limit == 0:
        raise ValueError("readback_query_limit_invalid")
    rows = readback["rows"]
    if not isinstance(rows, (list, tuple)):
        raise ValueError("readback_rows_invalid")
    if len(rows) > limit:
        raise ValueError("readback_rows_exceed_limit")
    source = readback["source"]
    if not isinstance(source, Mapping) or source.get("kind") not in _SOURCE_KINDS:
        raise ValueError("readback_source_kind_unknown")
    if source["kind"] == "native_bigquery":
        job_id, digest = source.get("job_id"), source.get("output_sha256")
        if not isinstance(job_id, str) or not job_id or not isinstance(digest, str):
            raise ValueError("readback_native_job_invalid")
        if not _DIGEST.match(digest):
            raise ValueError("readback_native_job_invalid")
    counted = {market: dict.fromkeys(CONTROL_STATES, 0) for market in strata}
    validated = _window_rows(rows)
    for row in validated:
        if row["market"] not in counted:
            raise ValueError("readback_market_not_in_strata")
        counted[row["market"]][row["control_state"]] += 1
    summary = readback["controls"]
    if not isinstance(summary, Mapping):
        raise ValueError("readback_controls_mismatch")
    for market in strata:
        reported = summary.get(market, {})
        if not isinstance(reported, Mapping):
            raise ValueError("readback_controls_mismatch")
        for state in CONTROL_STATES:
            if reported.get(state, 0) != counted[market][state]:
                raise ValueError("readback_controls_mismatch")
        if set(reported) - set(CONTROL_STATES):
            raise ValueError("readback_controls_mismatch")
    if set(summary) - set(strata):
        raise ValueError("readback_controls_mismatch")
    if not isinstance(readback["zero_hides_nothing"], bool):
        raise ValueError("readback_zero_hides_nothing_invalid")
    try:
        check_declared_routes(
            validated, strata, readback["expected_sources"], readback["known_quiet"]
        )
    except ValueError as error:
        raise ValueError(f"readback_declared_routes_invalid:{error}") from None
    if source["kind"] == "native_bigquery":
        _native_rows_match(validated, rows_bytes, source["output_sha256"])
    return {"window_start": start, "window_end": end, "kind": source["kind"]}


def _family_cell(entry, rows, *, market_unreadable, window_end, freshness_hours, leakage_limit):
    cell = {
        "state": None,
        "reason": None,
        "unique_qualified_contribution": None,
        "qualified_observations": None,
        "freshness_hours": None,
        "foreign_market_leakage": None,
        "foreign_market_leakage_reason": None,
        "missingness": [],
        "absent_routes": [],
        "unmeasured_routes": [],
        "supported_question_contribution": None,
        "supported_question_reason": "needs_question_evidence",
    }
    if "measured_by" in entry:
        cell.update(state="unmeasured", reason=entry["reason"])
        return cell
    present = [rows[route] for route in entry["routes"] if route in rows]
    cell["absent_routes"] = [route for route in entry["routes"] if route not in rows]
    cell["unique_qualified_contribution"] = sum(r["unique_qualified_observations"] for r in present)
    cell["qualified_observations"] = sum(r["qualified_observations"] for r in present)
    newest = [_timestamp(r["newest_published_at"]) for r in present]
    newest = [value for value in newest if value is not None]
    if newest:
        cell["freshness_hours"] = (window_end - max(newest)).total_seconds() / 3600
    physical = sum(r["physical_rows"] for r in present)
    reassigned = sum(r["reassigned_market_rows"] for r in present)
    if not physical:
        cell["foreign_market_leakage_reason"] = "no_rows"
    elif not reassigned:
        cell["foreign_market_leakage_reason"] = LEAKAGE_NOT_SHOWN
    elif reassigned > physical:
        cell["foreign_market_leakage_reason"] = LEAKAGE_EXCEEDS_ROWS
    else:
        cell["foreign_market_leakage"] = reassigned / physical
    cell["missingness"] = [
        {
            "route": r["route"],
            "control_state": r["control_state"],
            "recorded_failures": r["recorded_failures"],
        }
        for r in present
        if r["expected"] and (r["physical_rows"] == 0 or r["control_state"] != "emitting")
    ]
    cell["unmeasured_routes"] = [
        {"route": r["route"], "control_state": r["control_state"]}
        for r in present
        if r["control_state"] in _UNREADABLE_STATES
    ]
    if market_unreadable:
        cell.update(state="unmeasured", reason="market_reader_mismatch")
    elif cell["unmeasured_routes"]:
        cell.update(state="unmeasured", reason="route_not_readable")
    elif cell["qualified_observations"] == 0:
        cell.update(state="missing", reason="no_qualified_observations")
    elif cell["foreign_market_leakage_reason"] == LEAKAGE_EXCEEDS_ROWS:
        cell.update(state="leaking", reason=LEAKAGE_EXCEEDS_ROWS)
    elif cell["foreign_market_leakage"] is not None and (
        cell["foreign_market_leakage"] > leakage_limit
    ):
        cell.update(state="leaking", reason="foreign_market_leakage_above_limit")
    elif cell["freshness_hours"] is None:
        cell.update(state="stale", reason="freshness_unknown")
    elif cell["freshness_hours"] > freshness_hours:
        cell.update(state="stale", reason="freshness_above_limit")
    elif cell["unique_qualified_contribution"] == 0:
        cell.update(state="thin", reason="no_unique_qualified_contribution")
    else:
        cell["state"] = "covered"
    return cell


def evaluate_coverage_matrix(
    readback, families, *, window_end, freshness_hours=30, leakage_limit=0.0, rows_bytes=None
) -> dict:
    """Evaluate every family per market; ``families`` is the product families config.

    A native readback is evaluated only with ``rows_bytes``, the raw rows file
    saved with it, whose digest and rows must reproduce the readback.
    """
    if not isinstance(window_end, datetime) or window_end.tzinfo is None:
        raise ValueError("window_end_invalid")
    checked = validate_readback(readback, rows_bytes=rows_bytes)
    validate_product_families(families)
    family_entries, unassigned = families["families"], families["unassigned_routes"]
    mapped = {r for entry in family_entries.values() for r in entry.get("routes", ())}
    by_market: dict[str, dict[str, Mapping]] = {m: {} for m in readback["market_strata"]}
    for row in readback["rows"]:
        by_market[row["market"]][row["route"]] = row
    markets = {}
    for market, rows in by_market.items():
        # Only a market level mismatch unmeasures the whole market; a route
        # level mismatch unmeasures the families that route feeds.
        unreadable = any(
            r["control_reason"] == CONTROL_REASONS["reader_mismatch"] for r in rows.values()
        )
        cells = {
            name: _family_cell(
                family_entries[name],
                rows,
                market_unreadable=unreadable,
                window_end=window_end,
                freshness_hours=freshness_hours,
                leakage_limit=leakage_limit,
            )
            for name in FAMILIES
        }
        failures = [
            {
                "family": name,
                "state": value["state"],
                "reason": value["reason"],
                "missingness": value["missingness"],
                "absent_routes": value["absent_routes"],
                "unmeasured_routes": value["unmeasured_routes"],
            }
            for name, value in cells.items()
            if value["state"] != "covered"
        ]
        unresolved = rows.get(UNRESOLVED_ROUTE)
        markets[market] = {
            "families": cells,
            "failures": failures,
            "unmapped_routes": sorted(
                route
                for route in rows
                if route not in RESERVED_ROUTES and route not in mapped and route not in unassigned
            ),
            "unassigned_routes": [
                {"route": route, "reason": unassigned[route]}
                for route in sorted(rows)
                if route in unassigned
            ],
            "unresolved_rows_present": bool(unresolved and unresolved["physical_rows"]),
            "unresolved_rows": unresolved["physical_rows"] if unresolved else 0,
            "pipeline_failures": (
                rows[PIPELINE_ROUTE]["recorded_failures"] if PIPELINE_ROUTE in rows else 0
            ),
        }
    reasons = []
    if checked["kind"] != "native_bigquery":
        reasons.append("readback_not_native")
    if any(m["failures"] for m in markets.values()):
        reasons.append("family_not_covered")
    if any(m["unmapped_routes"] for m in markets.values()):
        reasons.append("unmapped_routes_present")
    if any(m["unresolved_rows_present"] for m in markets.values()):
        reasons.append("unresolved_rows_present")
    if any(m["pipeline_failures"] for m in markets.values()):
        reasons.append("pipeline_failures_recorded")
    hides = any(r["control_state"] in HIDING_STATES for r in readback["rows"])
    if readback["zero_hides_nothing"] is not True or hides:
        reasons.append("zero_hides_something")
    if any(
        value["foreign_market_leakage_reason"] == LEAKAGE_NOT_SHOWN
        for m in markets.values()
        for value in m["families"].values()
    ):
        reasons.append("foreign_market_leakage_unmeasured")
    return {
        "window": dict(readback["window"]),
        "source": dict(readback["source"]),
        "freshness_limit_hours": freshness_hours,
        "leakage_limit": leakage_limit,
        "markets": markets,
        "family_live_acceptance": "open" if reasons else "met",
        "acceptance_open_reasons": reasons,
    }
