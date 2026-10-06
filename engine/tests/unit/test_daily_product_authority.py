from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence.daily_products import admit_execution

from tests.unit.test_open_intelligence_execution_approval_runtime import _consumed_authority


def receipt(authority, consumption):
    return {
        "business_attempt_id": "attempt-1",
        "execution": {
            "authority": authority,
            "consumption": consumption,
            "stage": "compose",
            "mode": "new_consume",
        },
    }


def test_forged_authority_cannot_authorize_product_effects():
    forged = SimpleNamespace(manifest=SimpleNamespace(limits={"max_model_calls": 100}))
    with pytest.raises(ValueError, match="identity_invalid"):
        admit_execution(receipt(forged, object()))


def test_real_zero_model_origin_refuses_legacy_products_before_io():
    authority, consumption = _consumed_authority("r3_apply")
    with pytest.raises(ValueError, match="products_model_budget_unavailable"):
        admit_execution(receipt(authority, consumption))


def test_consumption_must_be_the_identical_bound_object():
    authority, consumption = _consumed_authority("r3_apply")
    from copy import copy

    with pytest.raises(ValueError, match="identity_invalid"):
        admit_execution(receipt(authority, copy(consumption)))


def test_mutable_duplicate_authority_slots_do_not_fund_products():
    authority, consumption = _consumed_authority("r3_apply")
    authority.source_sha = "f" * 40
    authority.operation = "daily_composition_apply"
    with pytest.raises(ValueError, match="products_model_budget_unavailable"):
        admit_execution(receipt(authority, consumption))


def test_wrong_consumed_operation_refuses():
    authority, consumption = _consumed_authority("brain_read")
    with pytest.raises(ValueError, match="products_operation_invalid"):
        admit_execution(receipt(authority, consumption))


def test_replacing_frozen_manifest_slot_cannot_increase_approved_limits():
    from dataclasses import replace

    authority, consumption = _consumed_authority("r3_apply")
    authority.manifest = replace(
        authority.manifest,
        limits={**authority.manifest.limits, "max_model_calls": 100},
    )
    with pytest.raises(ValueError, match="products_model_budget_unavailable"):
        admit_execution(receipt(authority, consumption))


def test_approval_namespace_cannot_reuse_registered_digest_fields():
    from dataclasses import fields

    authority, consumption = _consumed_authority("r3_apply")
    authority.approval = SimpleNamespace(
        **{
            field.name: getattr(authority.approval, field.name)
            for field in fields(authority.approval)
        }
    )
    with pytest.raises(ValueError, match="products_approval_invalid"):
        admit_execution(receipt(authority, consumption))
