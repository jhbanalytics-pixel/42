"""Derive retained inventory evidence from complete supplied native row batches."""

from datetime import timedelta

from .brain_contract import canonical_bytes, canonical_digest
from .execution_approval import _format_timestamp
from .initial_history_admission import (
    LANES,
    MARKETS,
    _aware,
    _count,
    _day,
    _hash,
    _policy,
    _queries,
    _record,
    _require,
    validate_initial_history_evidence,
    validate_initial_history_method,
)
from .production_snapshot_rows import _value
from .production_snapshot_tables import _schema


def _scalar(value, schema):
    if schema["mode"] == "REPEATED":
        _require(type(value) in (list, tuple), "invalid_rows")
        return [_scalar(item, {**schema, "mode": "REQUIRED"}) for item in value]
    if value is None:
        return _value(value, schema)
    if schema["type"] == "TIMESTAMP":
        return _format_timestamp(_aware(value), "invalid_rows")
    if schema["type"] in ("STRUCT", "RECORD"):
        _require(
            type(value) is dict and set(value) == {field["name"] for field in schema["fields"]},
            "invalid_rows",
        )
        return {field["name"]: _scalar(value[field["name"]], field) for field in schema["fields"]}
    return _value(value, schema)


def _numerical(schema):
    return schema["type"] in ("INT64", "FLOAT64") or any(
        _numerical(field) for field in schema["fields"]
    )


def _lane_schema(fields, recipe, expected_units):
    schema = {field["name"]: field for field in _schema({"fields": fields})["fields"]}
    projection = recipe["projection_fields"]
    _require(set(projection) <= set(schema), "method_mismatch")
    dates = {recipe["date_field"]} | ({"event_date"} if "event_date" in projection else set())
    for name in dates | set(recipe["key_fields"]) | {"market"}:
        expected = "DATE" if name in dates else "STRING"
        _require(
            schema[name]["type"] == expected and schema[name]["mode"] != "REPEATED",
            "method_mismatch",
        )
    units = {unit["field"]: unit["unit"] for unit in recipe["units"]}
    _require(type(expected_units) is dict and units == expected_units, "method_mismatch")
    _require(
        set(units) == {name for name in projection if _numerical(schema[name])}, "method_mismatch"
    )
    for field in ("item_count", "row_count"):
        if field in projection:
            _require(
                schema[field]["type"] == "INT64" and schema[field]["mode"] != "REPEATED",
                "method_mismatch",
            )
    return schema


def _rows(batch, recipe, schema, destination):
    _require(
        type(batch) is dict
        and set(batch)
        == {
            "query_evidence_ref",
            "snapshot_table",
            "pages",
            "terminal",
            "expected_row_count",
            "expected_rows_digest",
        },
        "incomplete_inventory",
    )
    _require(
        batch["terminal"] is True and batch["snapshot_table"] == destination, "incomplete_inventory"
    )
    _count(batch["expected_row_count"])
    _hash(batch["expected_rows_digest"])
    _queries([batch["query_evidence_ref"]])
    pages = batch["pages"]
    _require(type(pages) is list and bool(pages), "incomplete_inventory")
    expected_token, seen_tokens, rows, keys = None, set(), [], set()
    for index, page in enumerate(pages):
        _require(
            type(page) is dict and set(page) == {"page_token", "next_page_token", "rows"},
            "incomplete_inventory",
        )
        token, following = page["page_token"], page["next_page_token"]
        _require(token == expected_token and token not in seen_tokens, "incomplete_inventory")
        seen_tokens.add(token)
        if index == len(pages) - 1:
            _require(following is None, "incomplete_inventory")
        else:
            _require(type(following) is str and bool(following), "incomplete_inventory")
        expected_token = following
        _require(type(page["rows"]) is list, "incomplete_inventory")
        for raw in page["rows"]:
            _require(
                type(raw) is dict and set(raw) == set(recipe["projection_fields"]), "invalid_rows"
            )
            row = {
                field: _scalar(raw[field], schema[field]) for field in recipe["projection_fields"]
            }
            _require(
                all(row[field] is not None for field in recipe["required_nonnull_fields"]),
                "invalid_rows",
            )
            _require(row["market"] in MARKETS, "invalid_rows")
            _day(row[recipe["date_field"]])
            for field in recipe["key_fields"]:
                if type(row[field]) is str:
                    _require(bool(row[field].strip()), "invalid_rows")
            for field in ("item_count", "row_count"):
                if field in row:
                    _count(row[field])
            try:
                key = canonical_bytes([row[field] for field in recipe["key_fields"]])
                canonical_bytes(row)
            except (UnicodeError, ValueError, TypeError) as exc:
                raise ValueError("invalid_rows") from exc
            _require(key not in keys, "duplicate_key")
            keys.add(key)
            rows.append(row)
    rows.sort(key=canonical_bytes)
    _require(
        len(rows) == batch["expected_row_count"]
        and canonical_digest(rows) == batch["expected_rows_digest"],
        "incomplete_inventory",
    )
    return rows


def build_initial_history_inventory(
    *,
    policy,
    method,
    expected_method_digest,
    schemas,
    expected_units,
    snapshot_bindings,
    lane_batches,
    measured_at,
):
    """Return checked evidence; supplied transport facts confer no native authority."""
    policy = _policy(policy)
    method = validate_initial_history_method(method, expected_digest=expected_method_digest)
    _require(policy["method_ref"]["sha256"] == canonical_digest(method), "method_mismatch")
    _require(type(schemas) is dict and set(schemas) == set(LANES), "method_mismatch")
    expected_units = _record(expected_units, LANES)
    bindings = _record(snapshot_bindings, LANES)
    _require(type(lane_batches) is dict and set(lane_batches) == set(LANES), "incomplete_inventory")
    end = _day(policy["first_product_date"])
    full_rows, eligible_rows, inventory_facts, query_refs = {}, {}, {}, {}
    earliest = []
    for recipe in method["lane_recipes"]:
        lane = recipe["lane"]
        binding = _record(
            bindings[lane],
            {
                "source_table",
                "destination_table",
                "snapshot_as_of",
                "schema_digest",
                "metadata_digest",
            },
        )
        schema = _lane_schema(schemas[lane], recipe, expected_units[lane])
        rows = _rows(lane_batches[lane], recipe, schema, binding["destination_table"])
        eligible = [row for row in rows if _day(row[recipe["date_field"]]) < end]
        first = min((_day(row[recipe["date_field"]]) for row in eligible), default=None)
        if first is not None:
            earliest.append(first)
        full_rows[lane], eligible_rows[lane] = rows, eligible
        inventory_facts[lane] = {
            "full_row_count": len(rows),
            "full_rows_digest": canonical_digest(rows),
            "eligible_row_count": len(eligible),
            "eligible_rows_digest": canonical_digest(eligible),
            "earliest_eligible_date": first.isoformat() if first is not None else None,
        }
        query_refs[lane] = lane_batches[lane]["query_evidence_ref"]
    try:
        start = min([end - timedelta(days=30), *earliest])
    except OverflowError as exc:
        raise ValueError("invalid_rows") from exc
    lanes = []
    for recipe in method["lane_recipes"]:
        lane = recipe["lane"]
        grouped = {}
        for row in eligible_rows[lane]:
            grouped.setdefault((row["market"], row[recipe["date_field"]]), []).append(row)
        cells = []
        for market in MARKETS:
            for offset in range((end - start).days):
                day = (start + timedelta(days=offset)).isoformat()
                rows = grouped.get((market, day), [])
                cells.append(
                    {
                        "market": market,
                        "product_date": day,
                        "state": "retained_rows" if rows else "retained_empty",
                        "row_count": len(rows),
                        "rows_digest": canonical_digest(rows),
                        "query_evidence_refs": [query_refs[lane]],
                        "reason_code": None,
                    }
                )
        lanes.append(
            {
                "lane": lane,
                **bindings[lane],
                **inventory_facts[lane],
                "query_evidence_refs": [query_refs[lane]],
                "cells": cells,
            }
        )
    unique_refs = {canonical_bytes(ref): ref for ref in query_refs.values()}
    evidence = {
        "contract_version": "initial_history_evidence_v1",
        "policy_digest": canonical_digest(policy),
        "snapshot_result_ref": policy["snapshot_result_ref"],
        "method_digest": canonical_digest(method),
        "coverage_start": start.isoformat(),
        "coverage_end_exclusive": end.isoformat(),
        "inventory_evidence": [unique_refs[key] for key in sorted(unique_refs)],
        "lanes": lanes,
        "measured_at": _format_timestamp(_aware(measured_at), "invalid_rows"),
    }
    return validate_initial_history_evidence(
        evidence,
        policy=policy,
        method=method,
        snapshot_bindings=bindings,
        inventory_facts=inventory_facts,
        eligible_rows=eligible_rows,
    )
