"""Check historical comparison facts without issuing native capture authority."""

import json
import re
from datetime import UTC, date, datetime, time, timedelta

from .brain_contract import canonical_digest
from .daily_execution_contracts import _digest, canonical_json_object
from .execution_approval import _parse_timestamp
from .general_question_request import _DATE, _identifier
from .production_snapshot_rows import NATIVE_ID_PROJECTIONS

PROJECT = "ogilvy-trends-v2"
LANES = ["enriched_content", "raw_content"]
MARKETS = ["ke", "ng", "za"]
WINDOW = {
    "start": "2026-08-21T00:00:00.000000Z",
    "end_exclusive": "2026-09-09T00:00:00.000000Z",
}
_CAPTURE = re.compile(r"hc_[0-9a-f]{32}\Z")
_JOB = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MISSING = {"id", "platform", "url", "published_at", "endpoint", "source_family", "pipeline_run_id"}
_REASONS = {
    "coverage_not_read",
    "coverage_query_failed",
    "coverage_budget_exhausted",
    "coverage_transport_incomplete",
    "coverage_evidence_unavailable",
}


def _require(condition, code="historical_contract_invalid"):
    if not condition:
        raise ValueError(code)


def _object(value, fields):
    try:
        if not isinstance(value, str | bytes):
            _json_types(value)
        parsed, raw = canonical_json_object(value, "historical_json_invalid")
        _require(set(parsed) == set(fields))
        return json.loads(raw)
    except (TypeError, UnicodeError, OverflowError) as exc:
        raise ValueError("historical_json_invalid") from exc


def _json_types(value):
    _require(type(value) in (dict, list, str, int, float, bool, type(None)))
    if type(value) is dict:
        _require(all(type(key) is str for key in value))
        for item in value.values():
            _json_types(item)
    elif type(value) is list:
        for item in value:
            _json_types(item)


def _record(value, fields):
    _require(type(value) is dict)
    return _object(value, fields)


def _stamp(value):
    return _parse_timestamp(value, "historical_timestamp_invalid")


def _hash(value):
    return _digest(value, "historical_digest_invalid")


def _day(value):
    _require(isinstance(value, str) and _DATE.fullmatch(value) is not None)
    return date.fromisoformat(value)


def validate_policy(value):
    """Return the fixed archive declaration with checked policy digest syntax."""
    policy = _object(
        value,
        {
            "contract_version",
            "project",
            "source_dataset",
            "snapshot_dataset",
            "relations",
            "market_scope",
            "collection_window",
            "temporal_rules_digest",
            "identity_policy_digest",
            "allowed_purposes",
        },
    )
    fixed = {
        "contract_version": "historical_connector_policy_v1",
        "project": PROJECT,
        "source_dataset": "trends_v2_staging",
        "snapshot_dataset": "intelligence_42_sources_staging",
        "relations": LANES,
        "market_scope": MARKETS,
        "collection_window": WINDOW,
        "allowed_purposes": ["historical_question_context"],
    }
    _require(all(policy[key] == expected for key, expected in fixed.items()))
    _hash(policy["temporal_rules_digest"])
    _hash(policy["identity_policy_digest"])
    return policy


def _profile(value, policy):
    profile = _object(
        value,
        {
            "contract_version",
            "profile_id",
            "capture_id",
            "policy_digest",
            "client_scope_id",
            "market_scope",
            "snapshot_as_of",
            "relation_bindings",
            "projection_version",
            "temporal_rules_digest",
            "identity_policy_digest",
        },
    )
    capture = profile["capture_id"]
    _require(isinstance(capture, str) and _CAPTURE.fullmatch(capture) is not None)
    _require(profile["contract_version"] == "historical_connector_profile_v1")
    _require(profile["profile_id"] == f"historical_connector_{capture}_v1")
    _require(profile["policy_digest"] == canonical_digest(policy))
    _identifier(profile["client_scope_id"])
    _require(profile["market_scope"] == MARKETS)
    _require(profile["projection_version"] in NATIVE_ID_PROJECTIONS)
    for key in ("temporal_rules_digest", "identity_policy_digest"):
        _require(profile[key] == policy[key])
    _stamp(profile["snapshot_as_of"])
    bindings = profile["relation_bindings"]
    _require(isinstance(bindings, list) and len(bindings) == len(LANES))
    for lane, binding in zip(LANES, bindings, strict=True):
        binding = _record(
            binding,
            {"lane", "source_table", "destination_table", "snapshot_as_of", "source_schema_digest"},
        )
        _require(binding["lane"] == lane)
        _require(binding["source_table"] == f"{PROJECT}.trends_v2_staging.{lane}")
        _require(
            binding["destination_table"]
            == f"{PROJECT}.intelligence_42_sources_staging.historical_connector_{capture}_{lane}"
        )
        _require(binding["snapshot_as_of"] == profile["snapshot_as_of"])
        _hash(binding["source_schema_digest"])
    return profile


def validate_profile(value, *, policy, observed_at, expected_client_scope_id):
    """Compare the profile to supplied policy, scope and clock facts only."""
    profile = _profile(value, validate_policy(policy))
    _identifier(expected_client_scope_id)
    _require(profile["client_scope_id"] == expected_client_scope_id)
    _require(_stamp(profile["snapshot_as_of"]) <= _stamp(observed_at))
    return profile


def validate_requested_window(value, *, policy, request_as_of, closed):
    """Translate inclusive planner dates to a bounded half-open UTC interval."""
    validate_policy(policy)
    window = _object(value, {"start", "end"})
    start, end = _day(window["start"]), _day(window["end"])
    _require(type(closed) is bool and 0 <= (end - start).days <= 365)
    as_of = _stamp(request_as_of).date()
    _require(end <= as_of and (not closed or end < as_of))
    try:
        lower = datetime.combine(start, time.min, UTC)
        upper = datetime.combine(end + timedelta(days=1), time.min, UTC)
    except OverflowError as exc:
        raise ValueError("historical_window_invalid") from exc
    _require(
        _stamp(WINDOW["start"]) <= lower < upper <= _stamp(WINDOW["end_exclusive"]),
        "historical_window_unavailable",
    )
    return {
        "start": lower.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "end_exclusive": upper.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def _evidence(value):
    evidence = _record(
        value,
        {
            "project",
            "location",
            "job_id",
            "job_digest",
            "sql_digest",
            "parameters_digest",
            "result_digest",
        },
    )
    _require(evidence["project"] == PROJECT and evidence["location"] == "US")
    _require(isinstance(evidence["job_id"], str) and _JOB.fullmatch(evidence["job_id"]) is not None)
    for key in ("job_digest", "sql_digest", "parameters_digest", "result_digest"):
        _hash(evidence[key])


def _cell(value, market, day, snapshot_as_of):
    cell = _record(
        value,
        {
            "market",
            "collection_date",
            "state",
            "physical_row_count",
            "collected_min",
            "collected_max",
            "missing_fields",
            "evidence_ref",
            "reason_code",
        },
    )
    _require(cell["market"] == market and cell["collection_date"] == day.isoformat())
    if cell["state"] == "unavailable":
        _require(isinstance(cell["reason_code"], str) and cell["reason_code"] in _REASONS)
        _require(
            all(
                cell[key] is None
                for key in (
                    "physical_row_count",
                    "collected_min",
                    "collected_max",
                    "missing_fields",
                    "evidence_ref",
                )
            )
        )
        return
    _require(cell["state"] == "measured" and cell["reason_code"] is None)
    count = cell["physical_row_count"]
    _require(type(count) is int and count >= 0)
    missing = _record(cell["missing_fields"], _MISSING)
    _require(all(type(value) is int and 0 <= value <= count for value in missing.values()))
    _evidence(cell["evidence_ref"])
    if count == 0:
        _require(cell["collected_min"] is None and cell["collected_max"] is None)
    else:
        first, last = _stamp(cell["collected_min"]), _stamp(cell["collected_max"])
        lower = datetime.combine(day, time.min, UTC)
        _require(lower <= first <= last < lower + timedelta(days=1))
        _require(last <= snapshot_as_of)


def validate_coverage(value, *, policy, profile):
    """Check an exhaustive physical coverage grid, without certifying query evidence."""
    policy = validate_policy(policy)
    profile = _profile(profile, policy)
    coverage = _object(
        value,
        {
            "contract_version",
            "profile_digest",
            "snapshot_as_of",
            "collection_window",
            "market_scope",
            "relations",
        },
    )
    _require(coverage["contract_version"] == "historical_connector_coverage_v1")
    _require(coverage["profile_digest"] == canonical_digest(profile))
    _require(coverage["snapshot_as_of"] == profile["snapshot_as_of"])
    _require(coverage["collection_window"] == WINDOW and coverage["market_scope"] == MARKETS)
    relations = coverage["relations"]
    _require(isinstance(relations, list) and len(relations) == len(LANES))
    first, end = _stamp(WINDOW["start"]).date(), _stamp(WINDOW["end_exclusive"]).date()
    grid = [
        (market, first + timedelta(days=offset))
        for market in MARKETS
        for offset in range((end - first).days)
    ]
    for relation, binding in zip(relations, profile["relation_bindings"], strict=True):
        relation = _record(
            relation,
            {
                "lane",
                "source_table",
                "destination_table",
                "source_schema_digest",
                "metadata_digest",
                "cells",
            },
        )
        _require(
            all(
                relation[key] == binding[key]
                for key in ("lane", "source_table", "destination_table", "source_schema_digest")
            )
        )
        _hash(relation["metadata_digest"])
        cells = relation["cells"]
        _require(isinstance(cells, list) and len(cells) == len(grid))
        for cell, (market, day) in zip(cells, grid, strict=True):
            _cell(cell, market, day, _stamp(profile["snapshot_as_of"]))
    return coverage
