"""Initial inventory checks do not issue native admission authority."""

import copy
import importlib
from datetime import date, timedelta

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

LANES = ["event_ledger", "seed_candidates", "seed_graph", "trend_analysis", "trend_scores"]
PURPOSES = ["candidate_history", "derived_context", "first_seen", "prior_brief_score", "velocity"]
KEYS = {
    "event_ledger": ["ledger_id"],
    "seed_candidates": ["candidate_id"],
    "seed_graph": ["market", "term", "term_type", "platform", "trend_date"],
    "trend_analysis": ["market", "query_group", "trend_date"],
    "trend_scores": ["market", "query_group", "trend_date"],
}
D = "a" * 64
C0 = "2026-09-22T00:00:00.000000Z"
C1 = "2026-09-23T00:00:00.000000Z"
T = "2026-09-22T00:10:00.000000Z"
EMPTY = canonical_digest([])


def api():
    return importlib.import_module("src.analysis.open_intelligence.initial_history_admission")


def result_ref():
    return {
        "operation": "source_snapshot_capture",
        "consumption_id": "exc_" + D,
        "manifest_sha256": D,
        "result_id": "exr_" + D,
        "result_digest": D,
        "origin_registry_sha256": D,
        "resource_manifest_sha256": D,
    }


def object_ref():
    return {
        "uri": "gs://staging-evidence/initial/history.json",
        "generation": "12",
        "size_bytes": 3,
        "sha256": D,
        "created_at": C0,
    }


def query_ref():
    return {
        "job_project": "ogilvy-trends-v2",
        "job_location": "US",
        "job_id": "job_123",
        "query_ledger_result_ref": result_ref(),
    }


def fixture():
    recipes = []
    for lane in LANES:
        day = "proposed_date" if lane == "seed_candidates" else "trend_date"
        fields = sorted(
            set(
                KEYS[lane]
                + [day, "market"]
                + (
                    ["event_date", "row_count"]
                    if lane == "seed_graph"
                    else ["item_count"]
                    if lane == "trend_scores"
                    else []
                )
            )
        )
        numeric = (
            "row_count"
            if lane == "seed_graph"
            else "item_count"
            if lane == "trend_scores"
            else None
        )
        recipes.append(
            {
                "lane": lane,
                "date_field": day,
                "key_fields": KEYS[lane],
                "projection_fields": fields,
                "required_nonnull_fields": fields,
                "units": [{"field": numeric, "unit": "retained_count"}] if numeric else [],
                "duplicate_rule": "reject_duplicate_key",
                "row_encoding_version": "initial_history_scalar_v1",
            }
        )
    method = {
        "contract_version": "initial_history_method_v1",
        "method_id": "retained_v1",
        "source_sha": "b" * 40,
        "implementation_files": [{"path": "engine/src/scoring/velocity.py", "sha256": D}],
        "lane_recipes": recipes,
        "consumer_recipes": [
            {
                "purpose": purpose,
                "lane": lane,
                "template_id": purpose + "_v1",
                "template_sha256": D,
                "parameters": ["market"],
                "projection_version": "initial_history_scalar_v1",
            }
            for purpose, lane in zip(
                PURPOSES,
                ["seed_candidates", "event_ledger", "seed_graph", "trend_analysis", "trend_scores"],
                strict=True,
            )
        ],
        "scalar_encoding_version": "initial_history_scalar_v1",
    }
    policy = {
        "contract_version": "initial_history_policy_v1",
        "initialization_id": "ih_" + "1" * 32,
        "series_id": "retained_history_ih_" + "1" * 32 + "_v1",
        "project": "ogilvy-trends-v2",
        "product_dataset": "trends_v2_staging",
        "snapshot_result_ref": result_ref(),
        "first_product_date": "2026-09-21",
        "history_snapshot_as_of": C0,
        "market_scope": ["ke", "ng", "za"],
        "lanes": LANES,
        "method_ref": object_ref(),
        "allowed_purposes": PURPOSES,
        "comparison_basis": "retained_state_as_initial_condition",
    }
    policy["method_ref"]["sha256"] = canonical_digest(method)
    bindings, inventory, lanes = {}, {}, []
    for lane in LANES:
        binding = {
            "source_table": f"ogilvy-trends-v2.trends_v2_staging.{lane}",
            "destination_table": f"ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260921_{lane}",
            "snapshot_as_of": C0,
            "schema_digest": D,
            "metadata_digest": D,
        }
        facts = {
            "full_row_count": 0,
            "full_rows_digest": EMPTY,
            "eligible_row_count": 0,
            "eligible_rows_digest": EMPTY,
            "earliest_eligible_date": None,
        }
        cells = [
            {
                "market": market,
                "product_date": (date(2026, 8, 22) + timedelta(days=offset)).isoformat(),
                "state": "retained_empty",
                "row_count": 0,
                "rows_digest": EMPTY,
                "query_evidence_refs": [query_ref()],
                "reason_code": None,
            }
            for market in policy["market_scope"]
            for offset in range(30)
        ]
        bindings[lane], inventory[lane] = copy.deepcopy(binding), copy.deepcopy(facts)
        lanes.append(
            {"lane": lane, **binding, **facts, "query_evidence_refs": [query_ref()], "cells": cells}
        )
    evidence = {
        "contract_version": "initial_history_evidence_v1",
        "policy_digest": canonical_digest(policy),
        "snapshot_result_ref": result_ref(),
        "method_digest": canonical_digest(method),
        "coverage_start": "2026-08-22",
        "coverage_end_exclusive": "2026-09-21",
        "inventory_evidence": [query_ref()],
        "lanes": lanes,
        "measured_at": T,
    }
    return policy, method, evidence, bindings, inventory


def evidence_check(module, values, **kwargs):
    p, m, e, b, i = values
    kwargs.setdefault("eligible_rows", {lane: [] for lane in LANES})
    return module.validate_initial_history_evidence(
        e, policy=p, method=m, snapshot_bindings=b, inventory_facts=i, **kwargs
    )


def test_valid_complete_empty_inventory():
    module = api()
    p, m, e, _b, _i = values = fixture()
    assert (
        module.validate_initial_history_policy(
            p, expected_snapshot_result_ref=result_ref(), expected_method_generation="12"
        )
        == p
    )
    assert module.validate_initial_history_method(m, expected_digest=canonical_digest(m)) == m
    checked = evidence_check(module, values)
    assert checked == e
    checked["lanes"].clear()
    assert len(e["lanes"]) == 5


@pytest.mark.parametrize("generation", ["0", "01", " 1", "1\n", 1, True, "1.0", "\u0661"])
def test_generation_aliases_refused(generation):
    value = object_ref()
    value["generation"] = generation
    with pytest.raises(ValueError):
        api().validate_stored_object_ref(value, expected_generation=generation)


def test_native_generation_comparison_required():
    with pytest.raises(ValueError):
        api().validate_stored_object_ref(object_ref(), expected_generation="13")


@pytest.mark.parametrize(
    "field,value",
    [
        ("initialization_id", "ih_" + "A" * 32),
        ("series_id", "other"),
        ("project", "other"),
        ("lanes", LANES[:-1]),
        ("history_snapshot_as_of", T),
        ("comparison_basis", "completed"),
        ("extra", True),
    ],
)
def test_policy_mutations(field, value):
    p, *_ = fixture()
    p[field] = value
    with pytest.raises(ValueError):
        api().validate_initial_history_policy(
            p, expected_snapshot_result_ref=result_ref(), expected_method_generation="12"
        )


def test_policy_identity_conflict():
    p, *_ = fixture()
    previous = copy.deepcopy(p)
    p["method_ref"]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="initial_history_identity_conflict"):
        api().validate_initial_history_policy(
            p,
            expected_snapshot_result_ref=result_ref(),
            expected_method_generation="12",
            previous_policy=previous,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "keys",
        "lane",
        "duplicate",
        "scalar",
        "units",
        "projection",
        "file_path",
        "file_duplicate",
        "template",
        "purpose",
        "source",
        "digest",
    ],
)
def test_method_mutations(mutation):
    _, m, *_ = fixture()
    if mutation == "keys":
        m["lane_recipes"][2]["key_fields"] = ["term"]
    elif mutation == "lane":
        m["lane_recipes"].pop()
    elif mutation == "duplicate":
        m["lane_recipes"][0]["duplicate_rule"] = "deduplicate"
    elif mutation == "scalar":
        m["scalar_encoding_version"] = "other"
    elif mutation == "units":
        m["lane_recipes"][-1]["units"] = []
    elif mutation == "projection":
        m["lane_recipes"][2]["required_nonnull_fields"].remove("event_date")
    elif mutation == "file_path":
        m["implementation_files"][0]["path"] = "../outside.py"
    elif mutation == "file_duplicate":
        m["implementation_files"] *= 2
    elif mutation == "template":
        m["consumer_recipes"][0]["sql"] = "SELECT 1"
    elif mutation == "purpose":
        m["consumer_recipes"].pop()
    elif mutation == "source":
        m["source_sha"] = "b" * 39
    expected = "f" * 64 if mutation == "digest" else canonical_digest(m)
    with pytest.raises(ValueError):
        api().validate_initial_history_method(m, expected_digest=expected)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "state",
        "zero_digest",
        "bool",
        "reference",
        "binding",
        "count",
        "earliest",
        "date",
        "extra",
        "nested",
        "foreign_job",
        "result_id",
        "method",
    ],
)
def test_evidence_mutations(mutation):
    values = fixture()
    _p, _m, e, _b, _i = values
    lane, cell = e["lanes"][0], e["lanes"][0]["cells"][0]
    if mutation == "missing":
        lane["cells"].pop()
    elif mutation == "duplicate":
        lane["cells"][1] = copy.deepcopy(cell)
    elif mutation == "state":
        cell["state"] = "empty"
    elif mutation == "zero_digest":
        cell["rows_digest"] = D
    elif mutation == "bool":
        cell["row_count"] = False
    elif mutation == "reference":
        cell["query_evidence_refs"] = []
    elif mutation == "binding":
        lane["destination_table"] += "_other"
    elif mutation == "count":
        lane["eligible_row_count"] = 1
    elif mutation == "earliest":
        lane["earliest_eligible_date"] = "2026-08-22"
    elif mutation == "date":
        e["coverage_start"] = "2026-08-23"
    elif mutation == "extra":
        cell["completed"] = True
    elif mutation == "nested":
        lane["cells"][0] = canonical_bytes(cell).decode()
    elif mutation == "foreign_job":
        cell["query_evidence_refs"][0]["job_project"] = "other"
    elif mutation == "result_id":
        cell["query_evidence_refs"][0]["query_ledger_result_ref"]["result_id"] = "fake"
    else:
        e["method_digest"] = "f" * 64
    with pytest.raises(ValueError):
        evidence_check(api(), values)


def test_every_lane_destination_is_the_bridge_v3_clone_the_routine_writes():
    _p, _m, e, _b, _i = values = fixture()
    assert [lane["destination_table"] for lane in e["lanes"]] == [
        f"ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260921_{lane}" for lane in LANES
    ]
    assert evidence_check(api(), values) == e


@pytest.mark.parametrize(
    "destination",
    [
        # The collection dataset never holds a bridge clone the v3 routine writes.
        "ogilvy-trends-v2.intelligence_42_sources_staging.staging_bridge_v3_20260921_{lane}",
        "ogilvy-trends-v2.intelligence_42_sources_staging.snapshot_{lane}",
        "ogilvy-trends-v2.trends_v2_staging.snapshot_{lane}",
        "ogilvy-trends-v2.trends_v2_staging.{lane}",
        "ogilvy-trends-v2.trends_v2_staging_approvals.staging_bridge_v3_20260921_{lane}",
        "other-project.trends_v2_staging.staging_bridge_v3_20260921_{lane}",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_2026092_{lane}",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260921_{lane}_other",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260921_{other}",
        # The clone of another cutoff: the digits are the policy's first product date.
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_{lane}",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20200101_{lane}",
        "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_99999999_{lane}",
    ],
    ids=[
        "sources_bridge",
        "sources_snapshot",
        "product_snapshot",
        "product_source",
        "approvals",
        "project",
        "date_width",
        "suffix",
        "other_lane",
        "day_before",
        "other_year",
        "not_a_day",
    ],
)
def test_a_destination_outside_the_routine_bridge_clones_refuses(destination):
    values = fixture()
    _p, _m, e, b, _i = values
    lane = e["lanes"][0]
    table = destination.format(lane=lane["lane"], other=e["lanes"][1]["lane"])
    lane["destination_table"] = b[lane["lane"]]["destination_table"] = table
    with pytest.raises(ValueError, match=r"^initial_history_invalid$"):
        evidence_check(api(), values)


def test_diagnostic_unknown_never_usable_zero():
    values = fixture()
    _p, _m, e, _b, i = values
    lane = e["lanes"][0]
    lane["cells"][0].update(
        state="unavailable",
        row_count=None,
        rows_digest=None,
        query_evidence_refs=[],
        reason_code="incomplete_inventory",
    )
    for key in i[lane["lane"]]:
        lane[key] = i[lane["lane"]][key] = None
    module = api()
    assert evidence_check(module, values, require_usable=False) == e
    with pytest.raises(ValueError, match="initial_history_unavailable"):
        evidence_check(module, values)


def times():
    return {
        "approval_at": T,
        "execution_completed_at": T,
        "policy_created_at": C0,
        "method_created_at": C0,
        "evidence_created_at": T,
    }


def test_continuing_series_positive_and_equal_completion():
    p, *_ = fixture()
    result = api().validate_continuing_series(
        p, availability_inputs=times(), request_as_of=T, dispatch_at=T, predecessor_available_at=C1
    )
    assert result == {"initialization_available_at": T, "successor_cutoff": C1}


@pytest.mark.parametrize(
    "case",
    [
        "late_init",
        "late_dispatch",
        "late_completion",
        "before_available",
        "before_snapshot",
        "missing_time",
    ],
)
def test_continuing_series_gate(case):
    p, *_ = fixture()
    kwargs = {
        "availability_inputs": times(),
        "request_as_of": T,
        "dispatch_at": T,
        "predecessor_available_at": C1,
    }
    if case == "late_init":
        kwargs["availability_inputs"]["execution_completed_at"] = C1
    elif case == "late_dispatch":
        kwargs["dispatch_at"] = C1
    elif case == "late_completion":
        kwargs["predecessor_available_at"] = "2026-09-23T00:00:00.000001Z"
    elif case == "before_available":
        kwargs["request_as_of"] = C0
    elif case == "before_snapshot":
        kwargs["availability_inputs"] = dict.fromkeys(times(), "2026-09-21T00:00:00.000000Z")
    else:
        del kwargs["availability_inputs"]["approval_at"]
    with pytest.raises(ValueError):
        api().validate_continuing_series(p, **kwargs)


@pytest.mark.parametrize("value", ['{"x":1,"x":2}', '{ "x":1}', '{"x":NaN}', '{"x":"\\ud800"}'])
def test_noncanonical_json(value):
    with pytest.raises(ValueError):
        api().validate_initial_history_method(value, expected_digest=D)


def score_rows(day="2026-08-22", market="ke", groups=("a", "b")):
    return sorted(
        (
            {"item_count": 1, "market": market, "query_group": group, "trend_date": day}
            for group in groups
        ),
        key=canonical_bytes,
    )


def retained_rows_values(rows=None):
    values = fixture()
    lane = values[2]["lanes"][-1]
    assert lane["lane"] == "trend_scores"
    rows = score_rows() if rows is None else rows
    cell = lane["cells"][0]
    cell.update(state="retained_rows", row_count=len(rows), rows_digest=canonical_digest(rows))
    facts = {
        "full_row_count": 3,
        "full_rows_digest": "f" * 64,
        "eligible_row_count": len(rows),
        "eligible_rows_digest": canonical_digest(rows),
        "earliest_eligible_date": "2026-08-22",
    }
    lane.update(facts)
    values[4][lane["lane"]] = facts
    eligible = {name: [] for name in LANES}
    eligible["trend_scores"] = copy.deepcopy(rows)
    return values, eligible


def test_positive_retained_rows_and_excluded_population():
    values, eligible = retained_rows_values()
    assert evidence_check(api(), values, eligible_rows=eligible) == values[2]


def test_cell_rows_digest_is_recomputed_not_shape_checked():
    values, eligible = retained_rows_values()
    lane = values[2]["lanes"][-1]
    # A well formed digest that is not the digest of the cell's rows refuses, even when the
    # lane facts agree with it.
    lane["cells"][0]["rows_digest"] = D
    with pytest.raises(ValueError):
        evidence_check(api(), values, eligible_rows=eligible)


@pytest.mark.parametrize("mutation", ["row_value", "extra_row", "missing_row", "other_cell"])
def test_cell_rows_must_be_the_rows_the_inventory_read(mutation):
    values, eligible = retained_rows_values()
    rows = eligible["trend_scores"]
    if mutation == "row_value":
        rows[0]["item_count"] = 2
    elif mutation == "extra_row":
        rows.extend(score_rows(groups=("c",)))
    elif mutation == "missing_row":
        rows.pop()
    else:
        rows[0]["trend_date"] = "2026-08-23"
    with pytest.raises(ValueError):
        evidence_check(api(), values, eligible_rows=eligible)


def test_lane_eligible_digest_is_recomputed_from_the_inventory_rows():
    values, eligible = retained_rows_values()
    lane = values[2]["lanes"][-1]
    # Lane facts, native facts and every cell agree with each other, but the lane's
    # eligible digest is not the digest of the rows the inventory read.
    other = canonical_digest(score_rows(groups=("x", "y")))
    lane["eligible_rows_digest"] = other
    values[4][lane["lane"]]["eligible_rows_digest"] = other
    with pytest.raises(ValueError):
        evidence_check(api(), values, eligible_rows=eligible)


def two_cell_values(first_count, second_count):
    first, second = score_rows(), score_rows(day="2026-08-23", groups=("c",))
    rows = sorted([*first, *second], key=canonical_bytes)
    values, eligible = retained_rows_values(rows=first)
    lane = values[2]["lanes"][-1]
    assert (lane["cells"][1]["market"], lane["cells"][1]["product_date"]) == ("ke", "2026-08-23")
    lane["cells"][0].update(row_count=first_count)
    lane["cells"][1].update(
        state="retained_rows", row_count=second_count, rows_digest=canonical_digest(second)
    )
    facts = dict(
        values[4][lane["lane"]],
        full_row_count=4,
        eligible_row_count=3,
        eligible_rows_digest=canonical_digest(rows),
    )
    lane.update(facts)
    values[4][lane["lane"]] = facts
    eligible["trend_scores"] = rows
    return values, eligible


def test_two_retained_cells_with_their_own_counts_are_admitted():
    values, eligible = two_cell_values(2, 1)
    assert evidence_check(api(), values, eligible_rows=eligible) == values[2]


def test_cell_row_count_must_equal_the_cell_rows():
    # Compensating miscounts: the lane total and every digest still agree, but each cell
    # claims a count other than the rows it holds.
    values, eligible = two_cell_values(1, 2)
    with pytest.raises(ValueError):
        evidence_check(api(), values, eligible_rows=eligible)


def test_rows_outside_the_coverage_grid_refuse():
    values = fixture()
    eligible = {name: [] for name in LANES}
    eligible["trend_scores"] = score_rows(day="2026-08-21")
    with pytest.raises(ValueError):
        evidence_check(api(), values, eligible_rows=eligible)


def test_rows_under_an_unavailable_cell_refuse():
    values = fixture()
    _p, _m, e, _b, i = values
    lane = e["lanes"][-1]
    lane["cells"][0].update(
        state="unavailable",
        row_count=None,
        rows_digest=None,
        query_evidence_refs=[],
        reason_code="incomplete_inventory",
    )
    for key in i[lane["lane"]]:
        lane[key] = i[lane["lane"]][key] = None
    eligible = {name: [] for name in LANES}
    assert evidence_check(api(), values, eligible_rows=eligible, require_usable=False) == e
    eligible["trend_scores"] = score_rows()
    with pytest.raises(ValueError):
        evidence_check(api(), values, eligible_rows=eligible, require_usable=False)


@pytest.mark.parametrize("value", [None, [], {"trend_scores": []}, {lane: {} for lane in LANES}])
def test_eligible_rows_are_required_for_every_lane(value):
    with pytest.raises(ValueError):
        evidence_check(api(), fixture(), eligible_rows=value)


def test_temporal_precision_loss_refuses():
    p, *_ = fixture()
    available = times()
    available["execution_completed_at"] = "2026-09-22T00:10:00,0000001Z"
    with pytest.raises(ValueError):
        api().validate_continuing_series(p, availability_inputs=available, request_as_of=T)


def test_temporal_offsets_normalize():
    p, *_ = fixture()
    available = times()
    available["execution_completed_at"] = "2026-09-22T02:10:00+02:00"
    checked = api().validate_continuing_series(p, availability_inputs=available, request_as_of=T)
    assert checked["initialization_available_at"] == T


@pytest.mark.parametrize(
    "operation", ["source snapshot", "source_snapshot;drop", "SOURCE", "source_snapshot\n"]
)
def test_result_operation_identifier_is_canonical(operation):
    p, *_ = fixture()
    expected = result_ref()
    p["snapshot_result_ref"]["operation"] = expected["operation"] = operation
    with pytest.raises(ValueError):
        api().validate_initial_history_policy(
            p, expected_snapshot_result_ref=expected, expected_method_generation="12"
        )
