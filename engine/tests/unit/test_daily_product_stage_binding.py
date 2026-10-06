import pytest
from src.analysis.open_intelligence import daily_stages
from src.analysis.open_intelligence.brain_contract import canonical_digest

from tests.unit.test_daily_product_completion import Objects, completion
from tests.unit.test_daily_stages import (
    CUTOFF,
    OPERATION_ID,
    Fixture,
    kernel_profile,
    manifest,
    receipt_for,
)


def test_succeeded_compose_reuse_runs_its_completion_validator(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.run()
    calls = []

    def revalidate(*, manifest, result):
        calls.append(manifest["operation_id"])
        raise ValueError("completion_missing")

    fixture.handlers["compose"].validate_reuse = revalidate
    with pytest.raises(ValueError, match="completion_missing"):
        fixture.run(deny=())
    assert calls == [OPERATION_ID]
    assert not any(sql.startswith("BEGIN TRANSACTION") for sql in fixture.warehouse.calls)


def test_dynamic_only_stage_cannot_certify_when_products_are_enabled(monkeypatch):
    from src.analysis.open_intelligence.daily_product_completion import CompletionStore

    fixture = Fixture(monkeypatch)
    fixture.run()
    fixture.clients["products"] = type("Products", (), {"completion": CompletionStore(Objects())})()
    handlers = daily_stages.build_stage_handlers(fixture.clients, profile=kernel_profile())
    result = handlers["certify"](
        manifest=manifest("certify", predecessor=fixture.record("compose")["output_digest"]),
        authority_receipt=receipt_for("certify"),
    )
    assert result["state"] == "failed"
    assert "product_completion_reference_invalid" in result["result_reference"]


def test_stage_digest_authenticates_products_before_certification(monkeypatch):
    from src.analysis.open_intelligence.daily_product_completion import CompletionStore

    fixture = Fixture(monkeypatch)
    fixture.run()
    record = completion()
    record["operation_id"] = OPERATION_ID
    record["cutoff_utc"] = CUTOFF.isoformat()
    record["dynamic_receipt_digest"] = fixture.record("compose")["output_digest"]
    # Awaiting release, the dynamic view outcome is bound to that same receipt.
    record["products"]["v_desk_dynamic_signals_v2"]["output_digest"] = record[
        "dynamic_receipt_digest"
    ]
    objects = Objects()
    store = CompletionStore(objects)
    store.record(record)
    compose = fixture.store.records[(OPERATION_ID, "compose")]
    compose["result_reference"] = "products-v1:" + OPERATION_ID
    compose["output_digest"] = canonical_digest(record)
    fixture.clients["products"] = type("Products", (), {"completion": store})()
    changed = dict(record)
    changed["policy_digest"] = "0" * 64
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    objects.values[next(iter(objects.values))] = (canonical_bytes(changed), 2)
    handlers = daily_stages.build_stage_handlers(fixture.clients, profile=kernel_profile())
    result = handlers["certify"](
        manifest=manifest("certify", predecessor=compose["output_digest"]),
        authority_receipt=receipt_for("certify"),
    )
    assert result["state"] == "failed"
    assert "completion_digest_differs" in result["result_reference"]
