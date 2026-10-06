"""Check initial retained-state comparison facts without issuing native authority."""

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .brain_contract import canonical_bytes, canonical_digest
from .capture_registry import _GENERATION, _TABLE
from .daily_execution_contracts import _digest, _instant, canonical_json_object
from .execution_approval import _format_timestamp, _parse_timestamp
from .general_question_request import _DATE, _identifier

LANES = ("event_ledger", "seed_candidates", "seed_graph", "trend_analysis", "trend_scores")
MARKETS = ("ke", "ng", "za")
PURPOSES = ("candidate_history", "derived_context", "first_seen", "prior_brief_score", "velocity")
KEYS = {
    "event_ledger": ["ledger_id"],
    "seed_candidates": ["candidate_id"],
    "seed_graph": ["market", "term", "term_type", "platform", "trend_date"],
    "trend_analysis": ["market", "query_group", "trend_date"],
    "trend_scores": ["market", "query_group", "trend_date"],
}
EMPTY_DIGEST = canonical_digest([])
_REASONS = {
    "missing_snapshot",
    "incomplete_inventory",
    "query_failed",
    "budget_exhausted",
    "invalid_rows",
    "duplicate_key",
    "method_mismatch",
    "evidence_unavailable",
}
_INVENTORY_FIELDS = {
    "full_row_count",
    "full_rows_digest",
    "eligible_row_count",
    "eligible_rows_digest",
    "earliest_eligible_date",
}
_BINDING_FIELDS = {
    "source_table",
    "destination_table",
    "snapshot_as_of",
    "schema_digest",
    "metadata_digest",
}


def _require(condition, code="initial_history_invalid"):
    if not condition:
        raise ValueError(code)


def _json_types(value):
    _require(type(value) in (dict, list, str, int, float, bool, type(None)))
    if type(value) is dict:
        _require(all(type(key) is str for key in value))
        for item in value.values():
            _json_types(item)
    elif type(value) is list:
        for item in value:
            _json_types(item)


def _object(value, fields):
    try:
        if not isinstance(value, str | bytes):
            _json_types(value)
        parsed, raw = canonical_json_object(value, "initial_history_json_invalid")
        _require(set(parsed) == set(fields))
        return json.loads(raw)
    except (TypeError, UnicodeError, OverflowError) as exc:
        raise ValueError("initial_history_json_invalid") from exc


def _record(value, fields):
    _require(type(value) is dict)
    return _object(value, fields)


def _hash(value):
    return _digest(value, "initial_history_digest_invalid")


def _stamp(value):
    return _parse_timestamp(value, "initial_history_timestamp_invalid")


def _day(value):
    _require(type(value) is str and _DATE.fullmatch(value) is not None)
    return date.fromisoformat(value)


def _count(value):
    _require(type(value) is int and value >= 0)
    return value


def _strings(value, *, ordered=False):
    _require(
        type(value) is list and all(type(item) is str and _TABLE.fullmatch(item) for item in value)
    )
    _require(len(set(value)) == len(value) and (ordered or sorted(value) == value))
    return value


def _result_ref(value):
    ref = _record(
        value,
        {
            "operation",
            "consumption_id",
            "manifest_sha256",
            "result_id",
            "result_digest",
            "origin_registry_sha256",
            "resource_manifest_sha256",
        },
    )
    _identifier(ref["operation"])
    _require(re.fullmatch(r"[a-z][a-z0-9_]*", ref["operation"]) is not None)
    for key, prefix in (("consumption_id", "exc_"), ("result_id", "exr_")):
        _require(
            type(ref[key]) is str and re.fullmatch(prefix + r"[0-9a-f]{64}", ref[key]) is not None
        )
    for key in (
        "manifest_sha256",
        "result_digest",
        "origin_registry_sha256",
        "resource_manifest_sha256",
    ):
        _hash(ref[key])
    return ref


def _stored_ref(value):
    ref = _record(value, {"uri", "generation", "size_bytes", "sha256", "created_at"})
    _identifier(ref["uri"])
    uri = urlsplit(ref["uri"])
    _require(
        uri.scheme == "gs"
        and bool(uri.netloc)
        and uri.path.startswith("/")
        and len(uri.path) > 1
        and not uri.query
        and not uri.fragment
    )
    _require(
        type(ref["generation"]) is str and _GENERATION.fullmatch(ref["generation"]) is not None
    )
    _count(ref["size_bytes"])
    _hash(ref["sha256"])
    _stamp(ref["created_at"])
    return ref


def validate_stored_object_ref(value, *, expected_generation):
    ref = _stored_ref(value)
    _require(
        type(expected_generation) is str and _GENERATION.fullmatch(expected_generation) is not None
    )
    _require(ref["generation"] == expected_generation, "initial_history_generation_mismatch")
    return ref


def _policy(value):
    policy = _object(
        value,
        {
            "contract_version",
            "initialization_id",
            "series_id",
            "project",
            "product_dataset",
            "snapshot_result_ref",
            "first_product_date",
            "history_snapshot_as_of",
            "market_scope",
            "lanes",
            "method_ref",
            "allowed_purposes",
            "comparison_basis",
        },
    )
    identity = policy["initialization_id"]
    _require(type(identity) is str and re.fullmatch(r"ih_[0-9a-f]{32}", identity) is not None)
    _require(policy["series_id"] == f"retained_history_{identity}_v1")
    _require(policy["contract_version"] == "initial_history_policy_v1")
    _require(
        policy["project"] == "ogilvy-trends-v2" and policy["product_dataset"] == "trends_v2_staging"
    )
    _require(policy["lanes"] == list(LANES) and policy["market_scope"] == list(MARKETS))
    _require(
        policy["allowed_purposes"] == list(PURPOSES)
        and policy["comparison_basis"] == "retained_state_as_initial_condition"
    )
    _result_ref(policy["snapshot_result_ref"])
    _stored_ref(policy["method_ref"])
    try:
        cutoff = datetime.combine(
            _day(policy["first_product_date"]) + timedelta(days=1), time.min, UTC
        )
    except OverflowError as exc:
        raise ValueError("initial_history_date_invalid") from exc
    _require(_stamp(policy["history_snapshot_as_of"]) == cutoff)
    return policy


def validate_initial_history_policy(
    value, *, expected_snapshot_result_ref, expected_method_generation, previous_policy=None
):
    policy = _policy(value)
    _require(policy["snapshot_result_ref"] == _result_ref(expected_snapshot_result_ref))
    validate_stored_object_ref(policy["method_ref"], expected_generation=expected_method_generation)
    if previous_policy is not None:
        previous = _policy(previous_policy)
        if previous["initialization_id"] == policy["initialization_id"]:
            _require(
                canonical_bytes(previous) == canonical_bytes(policy),
                "initial_history_identity_conflict",
            )
    return policy


def _method(value):
    method = _object(
        value,
        {
            "contract_version",
            "method_id",
            "source_sha",
            "implementation_files",
            "lane_recipes",
            "consumer_recipes",
            "scalar_encoding_version",
        },
    )
    _require(
        method["contract_version"] == "initial_history_method_v1"
        and method["scalar_encoding_version"] == "initial_history_scalar_v1"
    )
    _identifier(method["method_id"])
    _require(
        type(method["source_sha"]) is str
        and re.fullmatch(r"[0-9a-f]{40}", method["source_sha"]) is not None
    )
    files = method["implementation_files"]
    _require(type(files) is list and bool(files))
    paths = []
    for value in files:
        pin = _record(value, {"path", "sha256"})
        path = _identifier(pin["path"])
        _require(
            not PurePosixPath(path).is_absolute()
            and not any(part in ("", ".", "..") for part in path.split("/"))
            and "\\" not in path
            and ":" not in path
        )
        _hash(pin["sha256"])
        paths.append(path)
    _require(paths == sorted(set(paths)))
    recipes = method["lane_recipes"]
    _require(type(recipes) is list and len(recipes) == len(LANES))
    for lane, value in zip(LANES, recipes, strict=True):
        recipe = _record(
            value,
            {
                "lane",
                "date_field",
                "key_fields",
                "projection_fields",
                "required_nonnull_fields",
                "units",
                "duplicate_rule",
                "row_encoding_version",
            },
        )
        day = "proposed_date" if lane == "seed_candidates" else "trend_date"
        _require(
            recipe["lane"] == lane
            and recipe["date_field"] == day
            and recipe["key_fields"] == KEYS[lane]
        )
        _require(
            recipe["duplicate_rule"] == "reject_duplicate_key"
            and recipe["row_encoding_version"] == "initial_history_scalar_v1"
        )
        projection = _strings(recipe["projection_fields"], ordered=True)
        required = _strings(recipe["required_nonnull_fields"], ordered=True)
        _require(
            set(required) <= set(projection) and set(KEYS[lane] + [day, "market"]) <= set(required)
        )
        units = recipe["units"]
        _require(type(units) is list)
        numerical = []
        for unit in units:
            unit = _record(unit, {"field", "unit"})
            _require(unit["field"] in projection and unit["field"] in required)
            _identifier(unit["unit"])
            numerical.append(unit["field"])
        _require(numerical == sorted(set(numerical)))
        if lane == "seed_graph":
            _require("event_date" in required and "row_count" in numerical)
        if lane == "trend_scores":
            _require("item_count" in numerical)
    consumers = method["consumer_recipes"]
    _require(type(consumers) is list and bool(consumers))
    keys = []
    for value in consumers:
        consumer = _record(
            value,
            {
                "purpose",
                "lane",
                "template_id",
                "template_sha256",
                "parameters",
                "projection_version",
            },
        )
        _require(consumer["purpose"] in PURPOSES and consumer["lane"] in LANES)
        _identifier(consumer["template_id"])
        _identifier(consumer["projection_version"])
        _hash(consumer["template_sha256"])
        _strings(consumer["parameters"])
        keys.append((consumer["purpose"], consumer["template_id"]))
    _require(keys == sorted(set(keys)) and {key[0] for key in keys} == set(PURPOSES))
    return method


def validate_initial_history_method(value, *, expected_digest):
    method = _method(value)
    _require(canonical_digest(method) == _hash(expected_digest), "initial_history_method_mismatch")
    return method


def _queries(value):
    _require(type(value) is list and bool(value))
    for item in value:
        ref = _record(item, {"job_project", "job_location", "job_id", "query_ledger_result_ref"})
        _require(ref["job_project"] == "ogilvy-trends-v2" and ref["job_location"] == "US")
        _require(
            type(ref["job_id"]) is str
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref["job_id"]) is not None
        )
        _result_ref(ref["query_ledger_result_ref"])


def _eligible_groups(rows, date_field):
    """Group the eligible rows the inventory read by (market, product date), each group
    in canonical row order, so every cell digest is recomputed from rows, not trusted."""
    _require(type(rows) is list and all(type(row) is dict for row in rows))
    try:
        ordered = sorted(rows, key=canonical_bytes)
    except (TypeError, UnicodeError, ValueError, OverflowError) as exc:
        raise ValueError("initial_history_json_invalid") from exc
    groups = {}
    for row in ordered:
        market, day = row.get("market"), row.get(date_field)
        _require(type(market) is str and type(day) is str)
        groups.setdefault((market, day), []).append(row)
    return ordered, groups


def validate_initial_history_evidence(
    value,
    *,
    policy,
    method,
    snapshot_bindings,
    inventory_facts,
    eligible_rows,
    require_usable=True,
):
    policy, method = _policy(policy), _method(method)
    _require(type(require_usable) is bool)
    _require(type(eligible_rows) is dict and set(eligible_rows) == set(LANES))
    date_fields = {recipe["lane"]: recipe["date_field"] for recipe in method["lane_recipes"]}
    evidence = _object(
        value,
        {
            "contract_version",
            "policy_digest",
            "snapshot_result_ref",
            "method_digest",
            "coverage_start",
            "coverage_end_exclusive",
            "inventory_evidence",
            "lanes",
            "measured_at",
        },
    )
    _require(evidence["contract_version"] == "initial_history_evidence_v1")
    _require(
        evidence["policy_digest"] == canonical_digest(policy)
        and evidence["snapshot_result_ref"] == policy["snapshot_result_ref"]
    )
    _require(
        evidence["method_digest"] == canonical_digest(method) == policy["method_ref"]["sha256"]
    )
    _require(_stamp(evidence["measured_at"]) >= _stamp(policy["history_snapshot_as_of"]))
    first, end = _day(evidence["coverage_start"]), _day(evidence["coverage_end_exclusive"])
    _require(end == _day(policy["first_product_date"]) and first < end)
    _queries(evidence["inventory_evidence"])
    bindings = _record(snapshot_bindings, LANES)
    native_facts = _record(inventory_facts, LANES)
    lanes = evidence["lanes"]
    _require(type(lanes) is list and len(lanes) == len(LANES))
    grid = [
        (market, first + timedelta(days=offset))
        for market in MARKETS
        for offset in range((end - first).days)
    ]
    earliest, unavailable = [], False
    for lane_name, value in zip(LANES, lanes, strict=True):
        lane = _record(
            value, {"lane", "query_evidence_refs", "cells"} | _BINDING_FIELDS | _INVENTORY_FIELDS
        )
        binding = _record(bindings[lane_name], _BINDING_FIELDS)
        facts = _record(native_facts[lane_name], _INVENTORY_FIELDS)
        _require(
            lane["lane"] == lane_name
            and all(lane[key] == binding[key] for key in _BINDING_FIELDS)
            and all(lane[key] == facts[key] for key in _INVENTORY_FIELDS)
        )
        _require(lane["snapshot_as_of"] == policy["history_snapshot_as_of"])
        _require(lane["source_table"] == f"ogilvy-trends-v2.trends_v2_staging.{lane_name}")
        # The history clones are the ones the v3 capture routine writes: the lane's bridge
        # snapshot in the product dataset, never a table in the collection dataset. The
        # routine names the clone for its cutoff and takes the history clone at the day
        # after it, and the policy's history snapshot is the day after its first product
        # date, so the name carries the first product date.
        day = _day(policy["first_product_date"])
        _require(
            lane["destination_table"]
            == f"ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_{day:%Y%m%d}_{lane_name}"
        )
        _hash(lane["schema_digest"])
        _hash(lane["metadata_digest"])
        _queries(lane["query_evidence_refs"])
        cells = lane["cells"]
        _require(type(cells) is list and len(cells) == len(grid))
        ordered, groups = _eligible_groups(eligible_rows[lane_name], date_fields[lane_name])
        _require(set(groups) <= {(market, day.isoformat()) for market, day in grid})
        count, dates, unknown = 0, [], False
        for cell_value, (market, day) in zip(cells, grid, strict=True):
            cell = _record(
                cell_value,
                {
                    "market",
                    "product_date",
                    "state",
                    "row_count",
                    "rows_digest",
                    "query_evidence_refs",
                    "reason_code",
                },
            )
            _require(cell["market"] == market and cell["product_date"] == day.isoformat())
            if cell["state"] == "unavailable":
                _require(
                    cell["row_count"] is None
                    and cell["rows_digest"] is None
                    and cell["query_evidence_refs"] == []
                )
                _require(type(cell["reason_code"]) is str and cell["reason_code"] in _REASONS)
                _require((market, day.isoformat()) not in groups)
                unknown = True
                continue
            _require(
                cell["state"] in ("retained_rows", "retained_empty") and cell["reason_code"] is None
            )
            rows = _count(cell["row_count"])
            _hash(cell["rows_digest"])
            _queries(cell["query_evidence_refs"])
            group = groups.get((market, day.isoformat()), [])
            _require(rows == len(group) and cell["rows_digest"] == canonical_digest(group))
            if cell["state"] == "retained_empty":
                _require(rows == 0 and cell["rows_digest"] == EMPTY_DIGEST)
            else:
                _require(rows > 0)
                dates.append(day)
            count += rows
        if unknown:
            unavailable = True
            _require(all(lane[key] is None for key in _INVENTORY_FIELDS))
            continue
        full, eligible = _count(lane["full_row_count"]), _count(lane["eligible_row_count"])
        _require(full >= eligible == count == len(ordered))
        _require(lane["eligible_rows_digest"] == canonical_digest(ordered))
        _hash(lane["full_rows_digest"])
        _hash(lane["eligible_rows_digest"])
        if eligible == 0:
            _require(
                lane["earliest_eligible_date"] is None
                and lane["eligible_rows_digest"] == EMPTY_DIGEST
            )
        else:
            observed_first = _day(lane["earliest_eligible_date"])
            _require(observed_first == min(dates))
            earliest.append(observed_first)
        if full == 0:
            _require(lane["full_rows_digest"] == EMPTY_DIGEST)
        if full == eligible:
            _require(lane["full_rows_digest"] == lane["eligible_rows_digest"])
    try:
        baseline = end - timedelta(days=30)
    except OverflowError as exc:
        raise ValueError("initial_history_date_invalid") from exc
    if unavailable:
        _require(first <= baseline)
        _require(not require_usable, "initial_history_unavailable")
    else:
        _require(first == min([baseline, *earliest]))
    return evidence


def _aware(value):
    try:
        if isinstance(value, str):
            _require(
                not any(
                    any(digit != "0" for digit in part[6:])
                    for part in re.findall(r"[.,](\d+)", value)
                ),
                "initial_history_timestamp_invalid",
            )
            value = _instant(value, "initial_history_timestamp_invalid")
        _require(
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("initial_history_timestamp_invalid") from exc


def validate_continuing_series(
    policy, *, availability_inputs, request_as_of, dispatch_at=None, predecessor_available_at=None
):
    policy = _policy(policy)
    fields = {
        "approval_at",
        "execution_completed_at",
        "policy_created_at",
        "method_created_at",
        "evidence_created_at",
    }
    _require(type(availability_inputs) is dict and set(availability_inputs) == fields)
    available = max(_aware(value) for value in availability_inputs.values())
    cutoff = _stamp(policy["history_snapshot_as_of"])
    try:
        successor = cutoff + timedelta(days=1)
    except OverflowError as exc:
        raise ValueError("initial_history_date_invalid") from exc
    _require(cutoff <= available < successor, "initial_history_continuing_window_missed")
    _require(_aware(request_as_of) >= available, "initial_history_not_available")
    if dispatch_at is not None:
        _require(
            available <= _aware(dispatch_at) < successor, "initial_history_dispatch_window_missed"
        )
    if predecessor_available_at is not None:
        _require(
            available <= _aware(predecessor_available_at) <= successor,
            "initial_history_successor_unavailable",
        )
    return {
        "initialization_available_at": _format_timestamp(
            available, "initial_history_timestamp_invalid"
        ),
        "successor_cutoff": _format_timestamp(successor, "initial_history_timestamp_invalid"),
    }
