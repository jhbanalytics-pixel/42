"""The daily contract revision: collection and compose run under their own operations.

The lead's bounds, recorded in docs/operations/iam-delta.md:

* ``source_collection`` carries the run credit cap the grant and consume routines hold
  collection to (620), no model calls, 1e9 billed bytes and no rows: the child reads the
  staging source dataset and the ingest job writes it under its own identity;
* ``daily_composition_apply`` carries no credits, up to 100 model calls (the funded
  product fixture's allowance, ``daily_product_authority_fixture.py``), 1e6 rows and
  1e10 billed bytes;
* ``collection_exposure_issue`` is bounded to 5e9 billed bytes (its first run issues
  2 + 2n statements for n = 84 at a 10 MiB minimum per referenced table, about 1.78e9)
  and 1e6 rows.

``r3_apply`` keeps its reviewed contract, so the R3 replay guard is not loosened; the
daily compose no longer reads it. Every path that takes the compose limits (the
derivation manifest, the product admission and through it the product writer's budget
and the dynamic meter) takes them from ``daily_composition_apply``.
"""

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence import daily_operation_map, execution_generations
from src.analysis.open_intelligence.daily_cost_policy import DailyCostPolicyRefusal
from src.analysis.open_intelligence.daily_dynamic_meter import DynamicMeter
from src.analysis.open_intelligence.daily_product_io import ProductBudget
from src.analysis.open_intelligence.daily_products import admit_execution

from tests.unit.daily_product_authority_fixture import funded_product_authority
from tests.unit.test_daily_derivation_authority import (
    LEASE,
    Derive,
    FakeObjectClient,
    authority,
    cost_policy,
    frame,
    grant,
    grant_row,
)

ENGINE = Path(__file__).resolve().parents[2]
CONTRACT = ENGINE / "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"
DAILY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
ORCHESTRATION = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
INT64_MAX = 2**63 - 1
COLLECTION_LIMITS = {
    "max_bytes_billed": 1_000_000_000,
    "max_credits": 620,
    "max_model_calls": 0,
    "max_rows_written": 0,
}
COMPOSITION_LIMITS = {
    "max_bytes_billed": 10_000_000_000,
    "max_credits": 0,
    "max_model_calls": 100,
    "max_rows_written": 1_000_000,
}
EXPOSURE_LIMITS = {
    "max_bytes_billed": 5_000_000_000,
    "max_credits": 0,
    "max_model_calls": 0,
    "max_rows_written": 1_000_000,
}


def registry():
    return execution_generations.active_generation().registry


def rules():
    return json.loads(CONTRACT.read_bytes())["operation_validation"]


def binding(operation):
    return daily_operation_map.child_binding(operation, registry=registry())


# The contract


def test_r3_apply_keeps_its_reviewed_rule():
    assert rules()["r3_apply"]["limits"] == {
        "max_bytes_billed": {"maximum": INT64_MAX, "minimum": 0},
        "max_credits": {"exact": 0},
        "max_model_calls": {"exact": 0},
        "max_rows_written": {"exact": 7},
    }


def test_the_daily_operations_share_the_r3_apply_template_with_their_own_bounds():
    apply = dict(rules()["r3_apply"])
    apply.pop("limits")
    apply.pop("arguments")
    for name, limits in (
        (
            "daily_composition_apply",
            {
                "max_bytes_billed": {"maximum": 10_000_000_000, "minimum": 0},
                "max_credits": {"exact": 0},
                "max_model_calls": {"maximum": 100, "minimum": 0},
                "max_rows_written": {"maximum": 1_000_000, "minimum": 0},
            },
        ),
        (
            "legacy_chain_replay",
            {
                "max_bytes_billed": {"maximum": 10_000_000_000, "minimum": 0},
                "max_credits": {"exact": 0},
                "max_model_calls": {"exact": 0},
                "max_rows_written": {"maximum": 1_000_000, "minimum": 0},
            },
        ),
    ):
        rule = dict(rules()[name])
        assert rule.pop("limits") == limits
        assert rule.pop("arguments")["kind"] == "prefix_pattern_v1"
        assert rule == apply
        assert rule["datasets"] == ["trends_v2_staging"]


def test_exposure_is_bounded_in_the_contract():
    assert rules()["collection_exposure_issue"]["limits"] == {
        "max_bytes_billed": {"maximum": 5_000_000_000, "minimum": 0},
        "max_credits": {"exact": 0},
        "max_model_calls": {"exact": 0},
        "max_rows_written": {"maximum": 1_000_000, "minimum": 0},
    }


def test_source_collection_writes_only_the_staging_source_dataset():
    rule = rules()["source_collection"]
    assert rule["datasets"] == ["intelligence_42_sources_staging"]
    assert rule["job_resource"] == DAILY_JOB
    assert rule["service_identity"] == ORCHESTRATION
    assert {"name": "BIGQUERY_DATASET", "value": "intelligence_42_sources_staging"} in rule[
        "environment"
    ]
    assert rule["limits"] == {
        "max_bytes_billed": {"maximum": 1_000_000_000, "minimum": 0},
        "max_credits": {"maximum": 620, "minimum": 0},
        "max_model_calls": {"exact": 0},
        "max_rows_written": {"exact": 0},
    }


# The map and the bindings


def test_collection_binds_its_own_operation_with_the_credit_cap():
    collection = binding("daily_source_collection")
    assert collection.registry_operation == "source_collection"
    assert collection.job_resource == DAILY_JOB
    assert collection.service_identity == ORCHESTRATION
    assert collection.datasets == ("intelligence_42_sources_staging",)
    assert collection.unbounded_limits() == ()
    assert collection.bounded_limits() == COLLECTION_LIMITS


def test_compose_binds_daily_composition_apply_not_r3_apply():
    compose = binding("daily_composition_apply")
    assert compose.registry_operation == "daily_composition_apply"
    assert compose.datasets == ("trends_v2_staging",)
    assert compose.bounded_limits() == COMPOSITION_LIMITS


def test_exposure_is_bounded():
    assert binding("daily_collection_exposure_issue").bounded_limits() == EXPOSURE_LIMITS


def test_no_daily_operation_is_unmapped_or_unbounded():
    assert daily_operation_map.UNMAPPED_OPERATIONS == {}
    for operation in daily_operation_map.DAILY_REGISTRY_OPERATIONS:
        assert binding(operation).unbounded_limits() == ()


# The derivation manifest


def _prepared(stage, operation):
    value = grant(allowed_operations=sorted({*grant()["allowed_operations"], operation}))
    subject, _derive, _store, _objects = authority(
        grant_row=grant_row(value),
        policy=cost_policy((operation,)),
        producers={operation: lambda frame, binding: {}},
    )
    predecessor = ((None, None),) if stage == "collection" else ()
    prepared = subject.prepare(stage, frame(stage, *predecessor), lease=LEASE)
    return json.loads(prepared["canonical_manifest_json"])


def test_a_compose_derivation_carries_the_daily_composition_limits():
    manifest = _prepared("compose", "daily_composition_apply")
    assert manifest["registry_operation"] == "daily_composition_apply"
    assert manifest["limits"] == COMPOSITION_LIMITS
    assert manifest["datasets"] == ["trends_v2_staging"]


def test_a_collection_derivation_carries_the_source_collection_limits():
    manifest = _prepared("collection", "daily_source_collection")
    assert manifest["registry_operation"] == "source_collection"
    assert manifest["limits"] == COLLECTION_LIMITS
    assert manifest["datasets"] == ["intelligence_42_sources_staging"]


def test_an_unbounded_binding_still_refuses_before_any_effect():
    real = daily_operation_map.child_binding

    def unbounded(operation, *, registry):
        found = real(operation, registry=registry)
        if operation != "daily_composition_apply":
            return found
        rules = dict(found.limit_rules)
        rules["max_rows_written"] = MappingProxyType({"minimum": 0, "maximum": INT64_MAX})
        return replace(found, limit_rules=MappingProxyType(rules))

    operation = "daily_composition_apply"
    value = grant(allowed_operations=sorted({*grant()["allowed_operations"], operation}))
    objects = FakeObjectClient()
    derive = Derive(objects)
    # The parent's cost policy is the reviewed one; only the binding the parent resolves
    # carries the int64 sentinel, so the refusal is the parent's own.
    subject, _derive, _store, _objects = authority(
        objects=objects,
        derive=derive,
        grant_row=grant_row(value),
        policy=cost_policy(),
        producers={operation: lambda frame, binding: {}},
        binding_for=unbounded,
    )
    with pytest.raises(daily_operation_map.DailyOperationRefusal, match=r"^daily_limit_unbounded$"):
        subject.prepare("compose", frame("compose"), lease=LEASE)
    assert derive.calls == []
    assert objects.objects == {}


# The product admission, the product writer's budget and the dynamic meter


def test_the_product_admission_takes_the_committed_daily_composition_limits(tmp_path, monkeypatch):
    fx = funded_product_authority(tmp_path, monkeypatch, operation="daily_composition_apply")
    admitted = admit_execution(fx.receipt)
    assert admitted.manifest.operation == "daily_composition_apply"
    assert dict(admitted.manifest.limits) == COMPOSITION_LIMITS
    budget = ProductBudget(admitted.manifest.limits)
    assert budget.limits == COMPOSITION_LIMITS
    meter = DynamicMeter(object())
    meter.bind(budget)
    meter._reserve_rows(1_000_000)
    with pytest.raises(ValueError, match="dynamic_write_budget_exhausted"):
        meter._reserve_rows(1)


def test_the_product_admission_refuses_limits_past_the_daily_composition_bounds(
    tmp_path, monkeypatch
):
    at_bound = funded_product_authority(
        tmp_path / "at",
        monkeypatch,
        operation="daily_composition_apply",
        limits={"max_rows_written": 1_000_000},
    )
    assert admit_execution(at_bound.receipt).manifest.limits["max_rows_written"] == 1_000_000
    with pytest.raises(ValueError, match=r"^execution_approval_manifest_invalid$"):
        funded_product_authority(
            tmp_path / "over",
            monkeypatch,
            operation="daily_composition_apply",
            limits={"max_rows_written": 1_000_001},
        )
