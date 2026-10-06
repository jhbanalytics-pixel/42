"""Complete row batches produce evidence, never inferred native authority."""

import copy
import hashlib
import importlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest

from tests.unit.test_initial_history_admission import fixture, query_ref


def digest(rows):
    def encoded(row):
        return json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")

    ordered = sorted(rows, key=encoded)
    return hashlib.sha256(encoded(ordered)).hexdigest()


def inputs(empty=False):
    policy, method, _, bindings, _ = fixture()
    schemas, units, batches = {}, {}, {}
    for recipe in method["lane_recipes"]:
        lane = recipe["lane"]
        types = dict.fromkeys(recipe["projection_fields"], "STRING")
        for field in ("trend_date", "proposed_date", "event_date"):
            if field in types:
                types[field] = "DATE"
        for field in ("row_count", "item_count"):
            if field in types:
                types[field] = "INT64"
        if lane == "event_ledger":
            types.update(as_of="TIMESTAMP", entity_aliases="STRING")
            recipe["projection_fields"] += ["as_of", "entity_aliases"]
        schemas[lane] = [
            {
                "name": field,
                "type": kind,
                "mode": "REPEATED" if field == "entity_aliases" else "NULLABLE",
            }
            for field, kind in types.items()
        ]
        units[lane] = {unit["field"]: unit["unit"] for unit in recipe["units"]}
        rows = []
        if not empty:
            for day, market in (
                ("2026-08-01" if lane == "seed_graph" else "2026-09-20", "za"),
                ("2026-09-21", "ke"),
            ):
                row = {
                    field: 4 if kind == "INT64" else day if kind == "DATE" else f" {lane}-{day} é "
                    for field, kind in types.items()
                }
                row["market"] = market
                if lane == "event_ledger":
                    row.update(
                        as_of="2026-09-20T10:00:00.000000Z",
                        entity_aliases=[" second ", "first", " second "],
                    )
                rows.append(row)
        batches[lane] = {
            "query_evidence_ref": query_ref(),
            "snapshot_table": bindings[lane]["destination_table"],
            "pages": [
                {"page_token": None, "next_page_token": "next", "rows": rows[:1]},
                {"page_token": "next", "next_page_token": None, "rows": rows[1:]},
            ],
            "terminal": True,
            "expected_row_count": len(rows),
            "expected_rows_digest": digest(rows),
        }
    policy["method_ref"]["sha256"] = canonical_digest(method)
    return {
        "policy": policy,
        "method": method,
        "expected_method_digest": canonical_digest(method),
        "schemas": schemas,
        "expected_units": units,
        "snapshot_bindings": bindings,
        "lane_batches": batches,
        "measured_at": "2026-09-22T00:10:00.000000Z",
    }


def build(args):
    module = importlib.import_module("src.analysis.open_intelligence.initial_history_inventory")
    return module.build_initial_history_inventory(**args)


def test_nonempty_five_lane_inventory_and_old_graph():
    args = inputs()
    result = build(args)
    assert result["coverage_start"] == "2026-08-01"
    assert result["coverage_end_exclusive"] == "2026-09-21"
    for lane in result["lanes"]:
        raw = [row for page in args["lane_batches"][lane["lane"]]["pages"] for row in page["rows"]]
        assert lane["full_row_count"] == 2
        assert lane["eligible_row_count"] == 1
        assert lane["full_rows_digest"] == digest(raw)
        assert lane["eligible_rows_digest"] == digest(raw[:1])
        assert sum(cell["row_count"] for cell in lane["cells"]) == 1
        positive = [cell for cell in lane["cells"] if cell["state"] == "retained_rows"]
        assert len(positive) == 1
        assert positive[0]["rows_digest"] == digest(raw[:1])
        assert positive[0]["market"] == "za"


def test_schema_fixture_matches_canonical_ddl_types():
    args = inputs()
    root = Path(__file__).parents[2] / "infra" / "bigquery_schemas"
    for lane, fields in args["schemas"].items():
        sql = (root / f"{lane}.sql").read_text(encoding="utf-8")
        for field in fields:
            kind = f"ARRAY<{field['type']}>" if field["mode"] == "REPEATED" else field["type"]
            assert re.search(r"\b" + field["name"] + r"\s+" + re.escape(kind) + r"(?=\s|,)", sql)


def test_complete_empty_is_measured_from_terminal_transport():
    result = build(inputs(empty=True))
    assert result["coverage_start"] == "2026-08-22"
    assert all(
        cell["state"] == "retained_empty" and cell["rows_digest"] == digest([])
        for lane in result["lanes"]
        for cell in lane["cells"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "terminal",
        "pages",
        "missing_final",
        "bad_token",
        "extra_page",
        "count",
        "bool_count",
        "digest",
        "snapshot",
        "query",
        "extra",
    ],
)
def test_transport_refusal(mutation):
    args = inputs()
    batch = args["lane_batches"]["trend_scores"]
    if mutation == "terminal":
        batch["terminal"] = False
    elif mutation == "pages":
        batch["pages"] = []
    elif mutation == "missing_final":
        batch["pages"].pop()
    elif mutation == "bad_token":
        batch["pages"][1]["page_token"] = "foreign"
    elif mutation == "extra_page":
        batch["pages"].append(copy.deepcopy(batch["pages"][-1]))
    elif mutation == "count":
        batch["expected_row_count"] += 1
    elif mutation == "bool_count":
        batch["expected_row_count"] = True
    elif mutation == "digest":
        batch["expected_rows_digest"] = "f" * 64
    elif mutation == "snapshot":
        batch["snapshot_table"] += "_other"
    elif mutation == "query":
        batch["query_evidence_ref"] = None
    else:
        batch["complete"] = True
    with pytest.raises(ValueError):
        build(args)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_market",
        "invalid_date",
        "null_count",
        "negative_count",
        "bool_count",
        "float_count",
        "extra_field",
        "duplicate",
        "missing_key",
        "null_event",
        "bad_repeat",
        "naive_timestamp",
        "bad_unicode",
    ],
)
def test_invalid_rows_refused_before_exclusion(mutation):
    args = inputs()
    lane = (
        "seed_graph"
        if mutation == "null_event"
        else "event_ledger"
        if mutation in ("bad_repeat", "naive_timestamp")
        else "trend_scores"
    )
    batch = args["lane_batches"][lane]
    row = batch["pages"][-1]["rows"][0]
    if mutation == "unknown_market":
        row["market"] = "us"
    elif mutation == "invalid_date":
        row["trend_date"] = "not-a-date"
    elif mutation == "null_count":
        row["item_count"] = None
    elif mutation == "negative_count":
        row["item_count"] = -1
    elif mutation == "bool_count":
        row["item_count"] = True
    elif mutation == "float_count":
        row["item_count"] = 4.0
    elif mutation == "extra_field":
        row["unexpected"] = 0
    elif mutation == "duplicate":
        batch["pages"][-1]["rows"].append(copy.deepcopy(row))
    elif mutation == "missing_key":
        row["query_group"] = ""
    elif mutation == "null_event":
        row["event_date"] = None
    elif mutation == "bad_repeat":
        row["entity_aliases"] = [None]
    elif mutation == "naive_timestamp":
        row["as_of"] = "2026-09-21T10:00:00"
    else:
        row["query_group"] = "\ud800"
    with pytest.raises(ValueError):
        build(args)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_field",
        "wrong_integer_type",
        "unit",
        "missing_unit",
        "method_pin",
        "duplicate_schema",
    ],
)
def test_method_schema_unit_refusal(mutation):
    args = inputs()
    if mutation == "missing_field":
        args["schemas"]["trend_scores"].pop()
    elif mutation == "wrong_integer_type":
        next(field for field in args["schemas"]["trend_scores"] if field["name"] == "item_count")[
            "type"
        ] = "FLOAT64"
    elif mutation == "unit":
        args["expected_units"]["trend_scores"]["item_count"] = "currency"
    elif mutation == "missing_unit":
        args["expected_units"]["trend_scores"] = {}
    elif mutation == "method_pin":
        args["expected_method_digest"] = "f" * 64
    else:
        args["schemas"]["trend_scores"].append(copy.deepcopy(args["schemas"]["trend_scores"][0]))
    with pytest.raises(ValueError):
        build(args)


def test_aware_timestamp_and_dates_use_canonical_scalar_encoding():
    args = inputs()
    batch = args["lane_batches"]["event_ledger"]
    row = batch["pages"][0]["rows"][0]
    row["as_of"] = datetime(2026, 9, 20, 12, tzinfo=timezone(timedelta(hours=2)))
    row["trend_date"] = date(2026, 9, 20)
    result = build(args)
    ledger = result["lanes"][0]
    assert ledger["full_rows_digest"] == batch["expected_rows_digest"]


def test_duplicate_refuses_even_with_matching_full_transport_digest():
    args = inputs()
    batch = args["lane_batches"]["seed_candidates"]
    duplicate = copy.deepcopy(batch["pages"][0]["rows"][0])
    batch["pages"][-1]["rows"].append(duplicate)
    rows = [row for page in batch["pages"] for row in page["rows"]]
    batch["expected_row_count"] = len(rows)
    batch["expected_rows_digest"] = digest(rows)
    with pytest.raises(ValueError, match="duplicate_key"):
        build(args)


def test_empty_inventory_still_requires_native_date_type():
    args = inputs(empty=True)
    next(field for field in args["schemas"]["trend_scores"] if field["name"] == "trend_date")[
        "type"
    ] = "STRING"
    with pytest.raises(ValueError, match="method_mismatch"):
        build(args)


@pytest.mark.parametrize(
    "field,kind,mode",
    [
        ("event_date", "STRING", "NULLABLE"),
        ("trend_date", "DATE", "REPEATED"),
        ("term", "STRING", "REPEATED"),
        ("market", "DATE", "NULLABLE"),
    ],
)
def test_empty_graph_keeps_scalar_key_and_event_date_types(field, kind, mode):
    args = inputs(empty=True)
    column = next(item for item in args["schemas"]["seed_graph"] if item["name"] == field)
    column.update(type=kind, mode=mode)
    with pytest.raises(ValueError, match="method_mismatch"):
        build(args)


@pytest.mark.parametrize("number", [42.5, float("nan"), float("inf"), True])
def test_float_projection_has_explicit_unit_and_finite_type(number):
    args = inputs()
    recipe = args["method"]["lane_recipes"][-1]
    recipe["required_nonnull_fields"] = list(recipe["required_nonnull_fields"])
    recipe["projection_fields"].append("trend_score")
    recipe["required_nonnull_fields"].append("trend_score")
    recipe["units"].append({"field": "trend_score", "unit": "retained_score"})
    args["schemas"]["trend_scores"].append(
        {"name": "trend_score", "type": "FLOAT64", "mode": "NULLABLE"}
    )
    args["expected_units"]["trend_scores"]["trend_score"] = "retained_score"
    args["expected_method_digest"] = args["policy"]["method_ref"]["sha256"] = canonical_digest(
        args["method"]
    )
    batch = args["lane_batches"]["trend_scores"]
    rows = [row for page in batch["pages"] for row in page["rows"]]
    for row in rows:
        row["trend_score"] = number
    if number == 42.5:
        batch["expected_rows_digest"] = digest(rows)
        assert build(args)["lanes"][-1]["full_rows_digest"] == digest(rows)
    else:
        with pytest.raises(ValueError):
            build(args)
