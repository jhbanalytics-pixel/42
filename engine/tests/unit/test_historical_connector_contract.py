"""Historical comparison facts never establish native capture authority."""

import copy
import importlib
from datetime import date, timedelta

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

D = "a" * 64
H = "2026-09-21T00:00:00.000000Z"
FIELDS = ("id", "platform", "url", "published_at", "endpoint", "source_family", "pipeline_run_id")


@pytest.fixture
def api():
    return importlib.import_module("src.analysis.open_intelligence.historical_connector_contract")


def facts():
    window = {
        "start": "2026-08-21T00:00:00.000000Z",
        "end_exclusive": "2026-09-09T00:00:00.000000Z",
    }
    policy = {
        "contract_version": "historical_connector_policy_v1",
        "project": "ogilvy-trends-v2",
        "source_dataset": "trends_v2_staging",
        "snapshot_dataset": "intelligence_42_sources_staging",
        "relations": ["enriched_content", "raw_content"],
        "market_scope": ["ke", "ng", "za"],
        "collection_window": window,
        "temporal_rules_digest": D,
        "identity_policy_digest": D,
        "allowed_purposes": ["historical_question_context"],
    }
    capture = "hc_" + "1" * 32
    bindings = [
        {
            "lane": lane,
            "source_table": f"ogilvy-trends-v2.trends_v2_staging.{lane}",
            "destination_table": f"ogilvy-trends-v2.intelligence_42_sources_staging.historical_connector_{capture}_{lane}",
            "snapshot_as_of": H,
            "source_schema_digest": D,
        }
        for lane in policy["relations"]
    ]
    profile = {
        "contract_version": "historical_connector_profile_v1",
        "profile_id": f"historical_connector_{capture}_v1",
        "capture_id": capture,
        "policy_digest": canonical_digest(policy),
        "client_scope_id": "42",
        "market_scope": policy["market_scope"],
        "snapshot_as_of": H,
        "relation_bindings": bindings,
        "projection_version": "native_id_bound_v1",
        "temporal_rules_digest": D,
        "identity_policy_digest": D,
    }
    cells = [
        {
            "market": market,
            "collection_date": (date(2026, 8, 21) + timedelta(days=day)).isoformat(),
            "state": "unavailable",
            "physical_row_count": None,
            "collected_min": None,
            "collected_max": None,
            "missing_fields": None,
            "evidence_ref": None,
            "reason_code": "coverage_not_read",
        }
        for market in policy["market_scope"]
        for day in range(19)
    ]
    coverage = {
        "contract_version": "historical_connector_coverage_v1",
        "profile_digest": canonical_digest(profile),
        "snapshot_as_of": H,
        "collection_window": window,
        "market_scope": policy["market_scope"],
        "relations": [
            {
                "lane": b["lane"],
                "source_table": b["source_table"],
                "destination_table": b["destination_table"],
                "source_schema_digest": D,
                "metadata_digest": D,
                "cells": copy.deepcopy(cells),
            }
            for b in bindings
        ],
    }
    return policy, profile, coverage


def measured(cell, count=0):
    cell.update(
        state="measured",
        physical_row_count=count,
        reason_code=None,
        missing_fields=dict.fromkeys(FIELDS, 0),
        evidence_ref={
            "project": "ogilvy-trends-v2",
            "location": "US",
            "job_id": "job_1-test",
            "job_digest": D,
            "sql_digest": D,
            "parameters_digest": D,
            "result_digest": D,
        },
    )
    if count:
        cell.update(
            collected_min=cell["collection_date"] + "T01:00:00.000000Z",
            collected_max=cell["collection_date"] + "T02:00:00.000000Z",
        )


def test_valid_facts_are_detached(api):
    p, r, c = facts()
    assert api.validate_policy(canonical_bytes(p)) == p
    assert api.validate_profile(r, policy=p, observed_at=H, expected_client_scope_id="42") == r
    checked = api.validate_coverage(c, policy=p, profile=r)
    checked["relations"][0]["cells"].clear()
    assert len(c["relations"][0]["cells"]) == 57


@pytest.mark.parametrize("target", ["binding", "relation", "cell", "missing", "evidence"])
def test_nested_json_strings_are_not_objects(api, target):
    p, r, c = facts()
    cell = c["relations"][0]["cells"][0]
    measured(cell)
    if target == "binding":
        r["relation_bindings"][0] = canonical_bytes(r["relation_bindings"][0]).decode()
        with pytest.raises(ValueError):
            api.validate_profile(r, policy=p, observed_at=H, expected_client_scope_id="42")
        return
    if target == "relation":
        c["relations"][0] = canonical_bytes(c["relations"][0]).decode()
    elif target == "cell":
        c["relations"][0]["cells"][0] = canonical_bytes(cell).decode()
    else:
        field = "missing_fields" if target == "missing" else "evidence_ref"
        cell[field] = canonical_bytes(cell[field]).decode()
    with pytest.raises(ValueError):
        api.validate_coverage(c, policy=p, profile=r)


@pytest.mark.parametrize(
    "key,value",
    [
        ("project", "other"),
        ("relations", ["raw_content"]),
        ("market_scope", ["za"]),
        ("contract_version", True),
        ("temporal_rules_digest", "A" * 64),
        ("extra", 1),
    ],
)
def test_policy_refuses_mutation(api, key, value):
    p, _, _ = facts()
    p[key] = value
    with pytest.raises(ValueError):
        api.validate_policy(p)


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{ "x":1}', '{"x":NaN}', '{"x":"\\ud800"}'])
def test_noncanonical_records_refused(api, payload):
    with pytest.raises(ValueError):
        api.validate_policy(payload)


@pytest.mark.parametrize(
    "key,value",
    [
        ("capture_id", "hc_" + "A" * 32),
        ("capture_id", "hc_" + "1" * 32 + "\n"),
        ("profile_id", "other"),
        ("policy_digest", "b" * 64),
        ("client_scope_id", "other"),
        ("market_scope", ["za"]),
        ("snapshot_as_of", "2026-09-22T00:00:00.000000Z"),
        ("projection_version", "unknown"),
        ("identity_policy_digest", "b" * 64),
        ("extra", 0),
    ],
)
def test_profile_refuses_mutation(api, key, value):
    p, r, _ = facts()
    r[key] = value
    with pytest.raises(ValueError):
        api.validate_profile(r, policy=p, observed_at=H, expected_client_scope_id="42")


@pytest.mark.parametrize(
    "key", ["source_table", "destination_table", "snapshot_as_of", "source_schema_digest", "lane"]
)
def test_binding_refuses_mutation(api, key):
    p, r, _ = facts()
    r["relation_bindings"][0][key] = "foreign"
    with pytest.raises(ValueError):
        api.validate_profile(r, policy=p, observed_at=H, expected_client_scope_id="42")


def test_inclusive_window_and_closed_rule(api):
    p, _, _ = facts()
    w = {"start": "2026-09-08", "end": "2026-09-08"}
    assert api.validate_requested_window(w, policy=p, request_as_of=H, closed=True) == {
        "start": "2026-09-08T00:00:00.000000Z",
        "end_exclusive": "2026-09-09T00:00:00.000000Z",
    }
    with pytest.raises(ValueError):
        api.validate_requested_window(
            w, policy=p, request_as_of="2026-09-08T23:00:00.000000Z", closed=True
        )


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-08-20", "2026-08-21"),
        ("2026-09-08", "2026-09-09"),
        ("2026-09-08", "2026-09-07"),
        ("20260821", "2026-09-08"),
        ("9999-12-31", "9999-12-31"),
    ],
)
def test_bad_window(api, start, end):
    p, _, _ = facts()
    with pytest.raises(ValueError):
        api.validate_requested_window(
            {"start": start, "end": end}, policy=p, request_as_of=H, closed=True
        )


@pytest.mark.parametrize("count", [0, 2])
def test_measured_cells(api, count):
    p, r, c = facts()
    measured(c["relations"][0]["cells"][0], count)
    assert api.validate_coverage(c, policy=p, profile=r) == c


@pytest.mark.parametrize(
    "key,value",
    [
        ("physical_row_count", 0),
        ("missing_fields", {}),
        ("reason_code", "unknown"),
        ("state", "complete"),
        ("collection_date", "2026-09-10"),
        ("market", "us"),
        ("extra", 1),
    ],
)
def test_unavailable_cell_refuses_mutation(api, key, value):
    p, r, c = facts()
    c["relations"][0]["cells"][0][key] = value
    with pytest.raises(ValueError):
        api.validate_coverage(c, policy=p, profile=r)


@pytest.mark.parametrize(
    "mutation",
    [
        "bool",
        "fraction",
        "negative",
        "missing",
        "bool_missing",
        "too_many_missing",
        "no_evidence",
        "reason",
        "wrong_day",
        "reverse",
        "job_unicode",
        "job_long",
        "digest",
        "extra_evidence",
    ],
)
def test_measured_cell_refuses_mutation(api, mutation):
    p, r, c = facts()
    cell = c["relations"][0]["cells"][0]
    measured(cell, 2)
    if mutation in {"bool", "fraction", "negative"}:
        cell["physical_row_count"] = {"bool": True, "fraction": 1.5, "negative": -1}[mutation]
    elif mutation == "missing":
        del cell["missing_fields"]["url"]
    elif mutation in {"bool_missing", "too_many_missing"}:
        cell["missing_fields"]["url"] = True if mutation == "bool_missing" else 3
    elif mutation == "no_evidence":
        cell["evidence_ref"] = None
    elif mutation == "reason":
        cell["reason_code"] = "coverage_not_read"
    elif mutation == "wrong_day":
        cell["collected_min"] = "2026-08-20T23:59:59.000000Z"
    elif mutation == "reverse":
        cell["collected_min"] = "2026-08-21T03:00:00.000000Z"
    elif mutation.startswith("job"):
        cell["evidence_ref"]["job_id"] = "é" if mutation == "job_unicode" else "a" * 129
    elif mutation == "digest":
        cell["evidence_ref"]["result_digest"] = "A" * 64
    else:
        cell["evidence_ref"]["complete"] = True
    with pytest.raises(ValueError):
        api.validate_coverage(c, policy=p, profile=r)


@pytest.mark.parametrize(
    "mutation", ["omit", "duplicate", "reorder", "lane", "profile", "metadata", "binding"]
)
def test_coverage_grid_and_binding(api, mutation):
    p, r, c = facts()
    cells = c["relations"][0]["cells"]
    if mutation == "omit":
        cells.pop()
    elif mutation == "duplicate":
        cells[1] = copy.deepcopy(cells[0])
    elif mutation == "reorder":
        cells.reverse()
    elif mutation == "lane":
        c["relations"].reverse()
    elif mutation == "profile":
        c["profile_digest"] = "b" * 64
    elif mutation == "metadata":
        c["relations"][0]["metadata_digest"] = None
    else:
        c["relations"][0]["destination_table"] += "_2"
    with pytest.raises(ValueError):
        api.validate_coverage(c, policy=p, profile=r)


@pytest.mark.parametrize(
    "mutation",
    [
        "tuple",
        "extra_window",
        "bad_utf8",
        "naive_clock",
        "zero_stamp",
        "zero_missing",
        "zero_no_evidence",
        "future_collection",
        "foreign_evidence",
        "bool_closed",
    ],
)
def test_boundary_types_and_zero_semantics(api, mutation):
    p, r, c = facts()

    def invoke():
        if mutation == "tuple":
            p["relations"] = tuple(p["relations"])
            api.validate_policy(p)
        elif mutation == "extra_window":
            p["collection_window"]["closed"] = True
            api.validate_policy(p)
        elif mutation == "bad_utf8":
            api.validate_policy(b"\xff")
        elif mutation == "naive_clock":
            api.validate_profile(
                r, policy=p, observed_at="2026-09-21T00:00:00", expected_client_scope_id="42"
            )
        elif mutation == "bool_closed":
            api.validate_requested_window(
                {"start": "2026-09-08", "end": "2026-09-08"}, policy=p, request_as_of=H, closed=1
            )
        else:
            cell = c["relations"][0]["cells"][0]
            measured(cell)
            if mutation == "zero_stamp":
                cell["collected_min"] = "2026-08-21T00:00:00.000000Z"
            elif mutation == "zero_missing":
                cell["missing_fields"]["url"] = 1
            elif mutation == "zero_no_evidence":
                cell["evidence_ref"] = None
            elif mutation == "foreign_evidence":
                cell["evidence_ref"]["project"] = "other"
            else:
                measured(cell, 2)
                r["snapshot_as_of"] = "2026-08-21T00:00:00.000000Z"
                for binding in r["relation_bindings"]:
                    binding["snapshot_as_of"] = r["snapshot_as_of"]
                c["snapshot_as_of"] = r["snapshot_as_of"]
                c["profile_digest"] = canonical_digest(r)
            api.validate_coverage(c, policy=p, profile=r)

    with pytest.raises(ValueError):
        invoke()
